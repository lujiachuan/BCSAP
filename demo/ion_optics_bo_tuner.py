# -*- coding: utf-8 -*-
"""
离子光学透镜贝叶斯优化调参软件
Ion Optics Bayesian Optimization Tuner

功能：
  - 22 个透镜/电极参数，每个可独立勾选启用并设置调节范围 [min, max]
  - 5 个探测器 FC1~FC5，选定后自动按束流路径勾选该探测器上游透镜
  - 贝叶斯优化（高斯过程 + Expected Improvement）迭代寻优
  - 白色现代图形界面，含收敛曲线、历史记录、配置保存/加载
  - 支持手动读数模式（实验室通用）与可选串口自动读数接口

作者：左泽文 / 南京原子制造研究所 离子源二组
"""

import os
import sys
import json
import time
import threading
import traceback
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Tuple

import numpy as np

# ---------- 贝叶斯优化后端 ----------
try:
    from skopt import Optimizer as SkOptOptimizer
    from skopt.space import Real
    _HAS_SKOPT = True
except Exception:
    _HAS_SKOPT = False

from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import Matern, ConstantKernel, WhiteKernel
from scipy.optimize import minimize
from scipy.stats import norm

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

# 中文字体
matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS"]
matplotlib.rcParams["axes.unicode_minus"] = False


# =====================================================================
# 透镜定义与束流路径
# =====================================================================
# 按束流经过顺序排列（从源到最终探测器）
LENS_ORDER = [
    "skim",
    "引出1", "引出2",
    "汇聚",
    "前偏转x+", "前偏转x-", "前偏转y+", "前偏转y-",
    "三圆筒",
    "后偏转x+", "后偏转x-", "后偏转y+", "后偏转y-",
    "漂移管",
    "加速电压",
    "加速后三圆筒1", "加速后三圆筒2",
    "电偏转1", "电偏转2",
    "第一组电磁场电流",
    "第二组电磁场电流",
]

# 每个透镜的默认调节范围 [V 或 A]，可在界面修改
DEFAULT_RANGES = {
    "skim":               (-500.0,   500.0),
    "引出1":              (-2000.0, 2000.0),
    "引出2":              (-2000.0, 2000.0),
    "汇聚":               (-3000.0, 3000.0),
    "前偏转x+":           (-500.0,   500.0),
    "前偏转x-":           (-500.0,   500.0),
    "前偏转y+":           (-500.0,   500.0),
    "前偏转y-":           (-500.0,   500.0),
    "三圆筒":             (-3000.0, 3000.0),
    "后偏转x+":           (-500.0,   500.0),
    "后偏转x-":           (-500.0,   500.0),
    "后偏转y+":           (-500.0,   500.0),
    "后偏转y-":           (-500.0,   500.0),
    "漂移管":             (-1000.0, 1000.0),
    "加速电压":           (0.0,    20000.0),
    "加速后三圆筒1":      (-5000.0, 5000.0),
    "加速后三圆筒2":      (-5000.0, 5000.0),
    "电偏转1":            (-2000.0, 2000.0),
    "电偏转2":            (-2000.0, 2000.0),
    "第一组电磁场电流":   (0.0,      10.0),
    "第二组电磁场电流":   (0.0,      10.0),
}

# 探测器位置：FC 位于哪个透镜"之后"
# key=探测器名, value=该探测器在 LENS_ORDER 中的后一个索引（即上游透镜为 0..pos_idx-1）
DETECTOR_POSITION = {
    "FC1": "加速后三圆筒2",   # 加速后三圆筒2 的束流焦点处
    "FC2": "电偏转1",          # 电偏转1 之后
    "FC3": "电偏转2",          # 电偏转2 之后
    "FC4": "第一组电磁场电流", # 第一组电磁场质选之后
    "FC5": "第二组电磁场电流", # 第二组电磁场质选之后
}

DETECTOR_DESC = {
    "FC1": "加速后三圆筒2 束流焦点处（理论设计）",
    "FC2": "电偏转1 之后",
    "FC3": "电偏转2 之后",
    "FC4": "第一组电磁场质选之后",
    "FC5": "第二组电磁场质选之后",
}


def upstream_lenses(detector: str) -> List[str]:
    """返回该探测器上游的所有透镜（含其前一个电极），下游透镜不影响该点束流。"""
    after = DETECTOR_POSITION[detector]
    idx = LENS_ORDER.index(after)
    return LENS_ORDER[: idx + 1]


# =====================================================================
# 贝叶斯优化器（自包含 GP + EI，不依赖 skopt 也可运行）
# =====================================================================
class BayesianOptimizer:
    """
    高斯过程回归 + Expected Improvement 采集函数的贝叶斯优化器。
    目标：最大化探测器电流（内部取负用于最小化框架）。
    """

    def __init__(self, param_names: List[str], bounds: List[Tuple[float, float]],
                 seed: int = 42, xi: float = 0.01, n_initial: int = 5):
        self.names = param_names
        self.bounds = np.array(bounds, dtype=float)
        self.dim = len(param_names)
        self.seed = seed
        self.rng = np.random.RandomState(seed)
        self.xi = xi                  # EI 探索-利用权衡，越大越探索
        self.n_initial = n_initial    # 初始随机采样点数

        self.X: List[np.ndarray] = []
        self.Y: List[float] = []      # 存储观测值（电流，越大越好）
        self.gp: Optional[GaussianProcessRegressor] = None
        self._use_skopt = _HAS_SKOPT
        self._skopt_opt = None
        if self._use_skopt:
            try:
                space = [Real(b[0], b[1], name=n) for n, b in zip(param_names, bounds)]
                self._skopt_opt = SkOptOptimizer(
                    dimensions=space,
                    base_estimator="GP",
                    acq_func="EI",
                    acq_optimizer="auto",
                    random_state=seed,
                    n_initial_points=n_initial,
                )
            except Exception:
                self._use_skopt = False
                self._skopt_opt = None

    # ---- 归一化 ----
    def _normalize(self, x: np.ndarray) -> np.ndarray:
        return (x - self.bounds[:, 0]) / (self.bounds[:, 1] - self.bounds[:, 0] + 1e-12)

    def _denormalize(self, xn: np.ndarray) -> np.ndarray:
        return xn * (self.bounds[:, 1] - self.bounds[:, 0]) + self.bounds[:, 0]

    # ---- 建议下一个采样点 ----
    def suggest(self) -> np.ndarray:
        if self._use_skopt and self._skopt_opt is not None:
            x = self._skopt_opt.ask()
            return np.array(x, dtype=float)

        # 初始点：随机采样（拉丁超立方风格）
        if len(self.X) < self.n_initial:
            xn = self.rng.rand(self.dim)
            return self._denormalize(xn)

        # 拟合 GP
        self._fit_gp()
        # 优化 EI
        best_x = self._optimize_acquisition()
        return best_x

    def _fit_gp(self):
        Xn = np.array([self._normalize(x) for x in self.X])
        # 目标最大化 → 对 y 取负，GP 拟合 -y（这样最小值对应最大电流）
        Y = -np.array(self.Y, dtype=float)
        kernel = (ConstantKernel(1.0, (1e-3, 1e3))
                  * Matern(length_scale=np.ones(self.dim), length_scale_bounds=(1e-2, 1e2), nu=2.5)
                  + WhiteKernel(noise_level=1e-5, noise_level_bounds=(1e-10, 1e-1)))
        self.gp = GaussianProcessRegressor(
            kernel=kernel, normalize_y=True, n_restarts_optimizer=5,
            random_state=self.seed,
        )
        self.gp.fit(Xn, Y)

    def _expected_improvement(self, xn: np.ndarray) -> float:
        """EI 采集函数（针对最小化 -y，即最大化 y）。"""
        xn = np.atleast_2d(xn)
        mu, sigma = self.gp.predict(xn, return_std=True)
        mu = mu[0]; sigma = max(sigma[0], 1e-9)
        # 当前最优（最小 -y = 最大 y）
        f_best = -np.max(self.Y)
        improvement = f_best - mu - self.xi
        Z = improvement / sigma
        ei = improvement * norm.cdf(Z) + sigma * norm.pdf(Z)
        return ei

    def _optimize_acquisition(self) -> np.ndarray:
        best_xn = None
        best_ei = -np.inf
        # 多起点局部优化
        starts = 50
        x0_list = self.rng.rand(starts, self.dim)
        for x0 in x0_list:
            res = minimize(
                lambda x: -self._expected_improvement(x),
                x0, method="L-BFGS-B", bounds=[(0.0, 1.0)] * self.dim,
            )
            if -res.fun > best_ei:
                best_ei = -res.fun
                best_xn = res.x
        if best_xn is None:
            best_xn = self.rng.rand(self.dim)
        return self._denormalize(best_xn)

    # ---- 记录观测 ----
    def observe(self, x: np.ndarray, y: float):
        x = np.array(x, dtype=float)
        self.X.append(x)
        self.Y.append(float(y))
        if self._use_skopt and self._skopt_opt is not None:
            try:
                self._skopt_opt.tell([list(x)], [-float(y)])
            except Exception:
                pass

    @property
    def best_value(self) -> float:
        return max(self.Y) if self.Y else float("nan")

    @property
    def best_x(self) -> Optional[np.ndarray]:
        if not self.Y:
            return None
        return self.X[int(np.argmax(self.Y))]


# =====================================================================
# 数据类：透镜配置 / 优化记录
# =====================================================================
@dataclass
class LensConfig:
    name: str
    enabled: bool = True
    vmin: float = 0.0
    vmax: float = 1.0
    current: float = 0.0   # 当前设定值

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


@dataclass
class OptRecord:
    iteration: int
    detector: str
    values: Dict[str, float]
    current: float          # 探测器测得电流
    timestamp: str = ""


# =====================================================================
# 主应用
# =====================================================================
class IonOpticsTunerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("离子光学透镜贝叶斯优化调参软件  v1.0")
        self.root.geometry("1400x880")
        self.root.minsize(1200, 720)

        # 白色主题
        self._setup_style()

        # 状态
        self.lens_configs: Dict[str, LensConfig] = {}
        for name in LENS_ORDER:
            lo, hi = DEFAULT_RANGES[name]
            self.lens_configs[name] = LensConfig(
                name=name, enabled=True, vmin=lo, vmax=hi, current=(lo + hi) / 2.0
            )

        self.selected_detector = tk.StringVar(value="FC1")
        self.optimizer: Optional[BayesianOptimizer] = None
        self.records: List[OptRecord] = []
        self.iteration = 0
        self.running = False
        self._pending_x: Optional[np.ndarray] = None
        self._pending_names: Optional[List[str]] = None

        # 串口自动读数（可选）
        self.serial_enabled = tk.BooleanVar(value=False)
        self.serial_port = tk.StringVar(value="COM3")
        self.serial_baud = tk.IntVar(value=9600)
        self._ser = None

        self._build_ui()
        self._refresh_plot()

    # ---------- 样式 ----------
    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        bg = "#ffffff"
        fg = "#222222"
        accent = "#2d6cdf"
        style.configure(".", background=bg, foreground=fg, fieldbackground=bg, bordercolor="#dddddd")
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("TLabelFrame", background=bg, foreground=fg)
        style.configure("TLabelframe", background=bg)
        style.configure("TLabelframe.Label", background=bg, foreground=fg, font=("Microsoft YaHei", 10, "bold"))
        style.configure("TButton", background=bg, foreground=fg, padding=6, borderwidth=1)
        style.map("TButton", background=[("active", "#e8f0fe")])
        style.configure("Accent.TButton", background=accent, foreground="white", padding=8, borderwidth=0, font=("Microsoft YaHei", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#1e56c7"), ("disabled", "#a0b8e0")])
        style.configure("TCheckbutton", background=bg, foreground=fg)
        style.configure("TRadiobutton", background=bg, foreground=fg)
        style.configure("TEntry", fieldbackground=bg, foreground=fg)
        style.configure("TCombobox", fieldbackground=bg, foreground=fg)
        style.configure("Treeview", background=bg, fieldbackground=bg, foreground=fg, rowheight=24)
        style.configure("Treeview.Heading", background="#f2f4f8", foreground=fg, font=("Microsoft YaHei", 9, "bold"))
        style.map("Treeview", background=[("selected", "#cfe0ff")], foreground=[("selected", fg)])
        style.configure("TNotebook", background=bg)
        style.configure("TNotebook.Tab", background="#f2f4f8", padding=[12, 6])
        style.map("TNotebook.Tab", background=[("selected", bg)])
        self.root.configure(bg=bg)

    # ---------- UI 构建 ----------
    def _build_ui(self):
        # 顶部标题栏
        top = ttk.Frame(self.root, padding=(12, 8))
        top.pack(fill=tk.X)
        ttk.Label(top, text="离子光学透镜贝叶斯优化调参系统",
                  font=("Microsoft YaHei", 14, "bold")).pack(side=tk.LEFT)
        ttk.Label(top, text="南京原子制造研究所 · 离子源二组",
                  font=("Microsoft YaHei", 9), foreground="#666666").pack(side=tk.LEFT, padx=12)

        # 主体：左右分栏
        main = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        # ===== 左侧：透镜参数表 =====
        left = ttk.Frame(main)
        main.add(left, weight=3)
        self._build_lens_panel(left)

        # ===== 右侧：控制 + 图表 + 记录 =====
        right = ttk.Frame(main)
        main.add(right, weight=4)
        self._build_control_panel(right)

        # 底部状态栏
        self.status_var = tk.StringVar(value="就绪")
        status = ttk.Frame(self.root, padding=(10, 4))
        status.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Label(status, textvariable=self.status_var, foreground="#555555",
                  font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)

    def _build_lens_panel(self, parent):
        lf = ttk.LabelFrame(parent, text=" 透镜参数（按束流顺序） ", padding=8)
        lf.pack(fill=tk.BOTH, expand=True)

        # 表头说明
        tip = ttk.Frame(lf)
        tip.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(tip, text="勾选 = 参与优化；范围 = 搜索区间；当前 = 设定值",
                  font=("Microsoft YaHei", 9), foreground="#666").pack(side=tk.LEFT)

        # 表格
        cols = ("idx", "name", "enabled", "vmin", "vmax", "current")
        tree = ttk.Treeview(lf, columns=cols, show="headings", height=22, selectmode="none")
        tree.heading("idx", text="#")
        tree.heading("name", text="透镜名称")
        tree.heading("enabled", text="启用")
        tree.heading("vmin", text="最小值")
        tree.heading("vmax", text="最大值")
        tree.heading("current", text="当前值")
        tree.column("idx", width=36, anchor=tk.CENTER)
        tree.column("name", width=150, anchor=tk.W)
        tree.column("enabled", width=50, anchor=tk.CENTER)
        tree.column("vmin", width=90, anchor=tk.E)
        tree.column("vmax", width=90, anchor=tk.E)
        tree.column("current", width=100, anchor=tk.E)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        sb = ttk.Scrollbar(lf, orient=tk.VERTICAL, command=tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        tree.configure(yscrollcommand=sb.set)

        self.lens_tree = tree
        # 双击编辑
        tree.bind("<Double-1>", self._on_lens_double_click)
        self._populate_lens_tree()

        # 底部按钮
        btns = ttk.Frame(parent)
        btns.pack(fill=tk.X, pady=6)
        ttk.Button(btns, text="全选", command=lambda: self._set_all_enabled(True)).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="全不选", command=lambda: self._set_all_enabled(False)).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="恢复默认范围", command=self._reset_ranges).pack(side=tk.LEFT, padx=2)

    def _populate_lens_tree(self):
        self.lens_tree.delete(*self.lens_tree.get_children())
        for i, name in enumerate(LENS_ORDER, 1):
            cfg = self.lens_configs[name]
            self.lens_tree.insert("", tk.END, iid=name, values=(
                i, name, "✓" if cfg.enabled else "",
                f"{cfg.vmin:.3g}", f"{cfg.vmax:.3g}", f"{cfg.current:.4g}"
            ))

    def _set_all_enabled(self, val: bool):
        for name in LENS_ORDER:
            self.lens_configs[name].enabled = val
        self._populate_lens_tree()

    def _reset_ranges(self):
        for name in LENS_ORDER:
            lo, hi = DEFAULT_RANGES[name]
            cfg = self.lens_configs[name]
            cfg.vmin, cfg.vmax = lo, hi
            cfg.current = (lo + hi) / 2.0
        self._populate_lens_tree()

    def _on_lens_double_click(self, event):
        region = self.lens_tree.identify("region", event.x, event.y)
        col = self.lens_tree.identify_column(event.x)
        item = self.lens_tree.identify_row(event.y)
        if not item:
            return
        name = item
        cfg = self.lens_configs[name]
        col_idx = int(col.replace("#", "")) - 1 if col else -1
        # col 0=idx,1=name,2=enabled,3=vmin,4=vmax,5=current
        if col_idx == 2:
            cfg.enabled = not cfg.enabled
            self._populate_lens_tree()
            return
        if col_idx in (3, 4, 5):
            field_map = {3: "vmin", 4: "vmax", 5: "current"}
            field = field_map[col_idx]
            self._edit_lens_value(name, field)

    def _edit_lens_value(self, name: str, field: str):
        cfg = self.lens_configs[name]
        cur = getattr(cfg, field)
        dlg = tk.Toplevel(self.root)
        dlg.title(f"编辑 {name} - {field}")
        dlg.geometry("320x140")
        dlg.transient(self.root)
        dlg.grab_set()
        dlg.configure(bg="white")
        ttk.Label(dlg, text=f"{name} / {field}", font=("Microsoft YaHei", 10, "bold")).pack(pady=(14, 4))
        var = tk.StringVar(value=f"{cur:.6g}")
        ent = ttk.Entry(dlg, textvariable=var, justify=tk.CENTER, font=("Consolas", 12))
        ent.pack(padx=20, pady=6, fill=tk.X)
        ent.focus_set()
        ent.select_range(0, tk.END)

        def ok():
            try:
                v = float(var.get())
                setattr(cfg, field, v)
                # 合理性校验
                if cfg.vmin >= cfg.vmax:
                    messagebox.showwarning("范围错误", f"{name} 的最小值必须小于最大值", parent=dlg)
                    return
                dlg.destroy()
                self._populate_lens_tree()
            except ValueError:
                messagebox.showerror("输入错误", "请输入有效数字", parent=dlg)

        ttk.Button(dlg, text="确定", command=ok, style="Accent.TButton").pack(pady=8)
        dlg.bind("<Return>", lambda e: ok())
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    # ---------- 右侧控制面板 ----------
    def _build_control_panel(self, parent):
        nb = ttk.Notebook(parent)
        nb.pack(fill=tk.BOTH, expand=True)

        tab_opt = ttk.Frame(nb, padding=8)
        tab_hist = ttk.Frame(nb, padding=8)
        tab_cfg = ttk.Frame(nb, padding=8)
        nb.add(tab_opt, text="  优化控制  ")
        nb.add(tab_hist, text="  历史记录  ")
        nb.add(tab_cfg, text="  配置/串口  ")

        self._build_opt_tab(tab_opt)
        self._build_hist_tab(tab_hist)
        self._build_cfg_tab(tab_cfg)

    def _build_opt_tab(self, parent):
        # ---- 探测器选择 ----
        lf_det = ttk.LabelFrame(parent, text=" 探测器选择 ", padding=8)
        lf_det.pack(fill=tk.X, pady=(0, 8))
        det_frame = ttk.Frame(lf_det)
        det_frame.pack(fill=tk.X)
        for i, fc in enumerate(["FC1", "FC2", "FC3", "FC4", "FC5"]):
            ttk.Radiobutton(det_frame, text=fc, value=fc, variable=self.selected_detector,
                            command=self._on_detector_change).grid(row=0, column=i, padx=8, pady=2)
        self.detector_desc_var = tk.StringVar()
        ttk.Label(lf_det, textvariable=self.detector_desc_var, foreground="#444",
                  font=("Microsoft YaHei", 9)).pack(anchor=tk.W, pady=(4, 0))
        ttk.Button(lf_det, text="按探测器位置自动勾选上游透镜",
                   command=self._auto_select_by_detector, style="Accent.TButton").pack(anchor=tk.W, pady=(6, 0))
        self._on_detector_change()

        # ---- 优化参数 ----
        lf_param = ttk.LabelFrame(parent, text=" 贝叶斯优化参数 ", padding=8)
        lf_param.pack(fill=tk.X, pady=(0, 8))
        grid = ttk.Frame(lf_param)
        grid.pack(fill=tk.X)
        ttk.Label(grid, text="总迭代次数:").grid(row=0, column=0, sticky=tk.W, padx=4, pady=3)
        self.n_iter_var = tk.IntVar(value=30)
        ttk.Spinbox(grid, from_=5, to=500, textvariable=self.n_iter_var, width=8).grid(row=0, column=1, sticky=tk.W, padx=4)

        ttk.Label(grid, text="初始随机点:").grid(row=0, column=2, sticky=tk.W, padx=4)
        self.n_init_var = tk.IntVar(value=5)
        ttk.Spinbox(grid, from_=1, to=20, textvariable=self.n_init_var, width=8).grid(row=0, column=3, sticky=tk.W, padx=4)

        ttk.Label(grid, text="探索系数 ξ:").grid(row=1, column=0, sticky=tk.W, padx=4, pady=3)
        self.xi_var = tk.DoubleVar(value=0.01)
        ttk.Spinbox(grid, from_=0.0, to=1.0, increment=0.01, textvariable=self.xi_var, width=8, format="%.3f").grid(row=1, column=1, sticky=tk.W, padx=4)

        ttk.Label(grid, text="随机种子:").grid(row=1, column=2, sticky=tk.W, padx=4)
        self.seed_var = tk.IntVar(value=42)
        ttk.Spinbox(grid, from_=0, to=99999, textvariable=self.seed_var, width=8).grid(row=1, column=3, sticky=tk.W, padx=4)

        ttk.Label(grid, text="读数稳定等待(s):").grid(row=2, column=0, sticky=tk.W, padx=4, pady=3)
        self.settle_var = tk.DoubleVar(value=1.0)
        ttk.Spinbox(grid, from_=0.0, to=60.0, increment=0.5, textvariable=self.settle_var, width=8).grid(row=2, column=1, sticky=tk.W, padx=4)

        # ---- 运行控制 ----
        lf_run = ttk.LabelFrame(parent, text=" 运行控制 ", padding=8)
        lf_run.pack(fill=tk.X, pady=(0, 8))
        run_bar = ttk.Frame(lf_run)
        run_bar.pack(fill=tk.X)
        self.btn_start = ttk.Button(run_bar, text="▶ 开始优化", command=self.start_optimization, style="Accent.TButton")
        self.btn_start.pack(side=tk.LEFT, padx=4)
        self.btn_stop = ttk.Button(run_bar, text="■ 停止", command=self.stop_optimization, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=4)
        ttk.Button(run_bar, text="单步建议", command=self.single_step).pack(side=tk.LEFT, padx=4)
        ttk.Button(run_bar, text="重置优化器", command=self.reset_optimizer).pack(side=tk.LEFT, padx=4)

        # 进度
        self.progress_var = tk.StringVar(value="迭代: 0 / 0   最优电流: --")
        ttk.Label(lf_run, textvariable=self.progress_var, font=("Microsoft YaHei", 10, "bold"),
                  foreground="#2d6cdf").pack(anchor=tk.W, pady=(6, 0))
        self.prog_bar = ttk.Progressbar(lf_run, mode="determinate")
        self.prog_bar.pack(fill=tk.X, pady=(4, 0))

        # ---- 当前建议 ----
        lf_sug = ttk.LabelFrame(parent, text=" 当前建议参数（请手动设置到电源，或启用串口自动输出） ", padding=8)
        lf_sug.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
        sug_frame = ttk.Frame(lf_sug)
        sug_frame.pack(fill=tk.BOTH, expand=True)
        self.suggestion_text = tk.Text(sug_frame, height=6, font=("Consolas", 10),
                                        bg="#fafbfc", relief=tk.FLAT, wrap=tk.WORD)
        self.suggestion_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb2 = ttk.Scrollbar(sug_frame, orient=tk.VERTICAL, command=self.suggestion_text.yview)
        sb2.pack(side=tk.RIGHT, fill=tk.Y)
        self.suggestion_text.configure(yscrollcommand=sb2.set)
        self.suggestion_text.insert(tk.END, "（尚未开始，点击「开始优化」或「单步建议」）")
        self.suggestion_text.configure(state=tk.DISABLED)

        # 手动读数
        read_bar = ttk.Frame(lf_sug)
        read_bar.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(read_bar, text="实测电流 (A):").pack(side=tk.LEFT)
        self.manual_reading_var = tk.StringVar(value="")
        ttk.Entry(read_bar, textvariable=self.manual_reading_var, width=14, font=("Consolas", 11)).pack(side=tk.LEFT, padx=6)
        self.btn_confirm = ttk.Button(read_bar, text="确认读数并继续", command=self.confirm_reading, state=tk.DISABLED)
        self.btn_confirm.pack(side=tk.LEFT, padx=4)

        # ---- 收敛曲线 ----
        lf_plot = ttk.LabelFrame(parent, text=" 收敛曲线 ", padding=4)
        lf_plot.pack(fill=tk.BOTH, expand=True)
        self.fig = Figure(figsize=(6, 3.2), dpi=100, facecolor="white")
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=lf_plot)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        toolbar = NavigationToolbar2Tk(self.canvas, lf_plot)
        toolbar.update()

    def _build_hist_tab(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(top, text="导出 CSV", command=self.export_csv).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="清空记录", command=self.clear_history).pack(side=tk.LEFT, padx=2)
        self.best_var = tk.StringVar(value="最优: --")
        ttk.Label(top, textvariable=self.best_var, foreground="#2d6cdf",
                  font=("Microsoft YaHei", 10, "bold")).pack(side=tk.RIGHT, padx=8)

        cols = ("iter", "detector", "current", "best", "time")
        self.hist_tree = ttk.Treeview(parent, columns=cols, show="headings", height=20)
        for c, t, w in [("iter", "#", 50), ("detector", "探测器", 80),
                         ("current", "实测电流(A)", 130), ("best", "历史最优(A)", 130),
                         ("time", "时间", 160)]:
            self.hist_tree.heading(c, text=t)
            self.hist_tree.column(c, width=w, anchor=tk.CENTER)
        self.hist_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.hist_tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.hist_tree.configure(yscrollcommand=sb.set)
        self.hist_tree.bind("<<TreeviewSelect>>", self._on_hist_select)

        # 详情
        lf = ttk.LabelFrame(parent, text=" 选中迭代的完整参数 ", padding=6)
        lf.pack(fill=tk.BOTH, expand=False, pady=(6, 0))
        self.hist_detail = tk.Text(lf, height=6, font=("Consolas", 9), bg="#fafbfc", relief=tk.FLAT, wrap=tk.WORD)
        self.hist_detail.pack(fill=tk.BOTH, expand=True)

    def _build_cfg_tab(self, parent):
        # 配置保存加载
        lf1 = ttk.LabelFrame(parent, text=" 配置文件 ", padding=8)
        lf1.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(lf1, text="保存配置", command=self.save_config).pack(side=tk.LEFT, padx=4)
        ttk.Button(lf1, text="加载配置", command=self.load_config).pack(side=tk.LEFT, padx=4)
        ttk.Label(lf1, text="（含透镜启用状态、范围、优化参数、历史记录）",
                  foreground="#666", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT, padx=10)

        # 串口设置
        lf2 = ttk.LabelFrame(parent, text=" 串口自动读数（可选，用于 pA 计 / 源表） ", padding=8)
        lf2.pack(fill=tk.X, pady=(0, 8))
        grid = ttk.Frame(lf2)
        grid.pack(fill=tk.X)
        ttk.Checkbutton(grid, text="启用串口自动读数", variable=self.serial_enabled).grid(row=0, column=0, padx=4, pady=3, sticky=tk.W)
        ttk.Label(grid, text="端口:").grid(row=0, column=1, padx=4)
        ttk.Entry(grid, textvariable=self.serial_port, width=8).grid(row=0, column=2, padx=4)
        ttk.Label(grid, text="波特率:").grid(row=0, column=3, padx=4)
        ttk.Combobox(grid, textvariable=self.serial_baud, values=[9600, 19200, 38400, 57600, 115200], width=8).grid(row=0, column=4, padx=4)
        ttk.Label(grid, text="读数命令(可选):").grid(row=1, column=0, padx=4, pady=3, sticky=tk.W)
        self.serial_cmd_var = tk.StringVar(value="READ?")
        ttk.Entry(grid, textvariable=self.serial_cmd_var, width=20).grid(row=1, column=1, columnspan=2, padx=4, sticky=tk.W)
        ttk.Label(lf2, text="说明：启用后每次建议参数后自动等待稳定时间并读取电流；\n"
                             "未启用时为手动模式，在「实测电流」框输入后点「确认读数并继续」。",
                  foreground="#555", font=("Microsoft YaHei", 9), justify=tk.LEFT).pack(anchor=tk.W, pady=(6, 0))

        # 关于
        lf3 = ttk.LabelFrame(parent, text=" 关于 ", padding=8)
        lf3.pack(fill=tk.BOTH, expand=True)
        about = (
            "离子光学透镜贝叶斯优化调参软件 v1.0\n\n"
            "算法：高斯过程回归 (Matern 5/2 核) + Expected Improvement 采集函数\n"
            "后端：scikit-optimize（若已安装）/ 自包含 scikit-learn GP 实现\n\n"
            "透镜顺序：skim → 引出1/2 → 汇聚 → 前偏转(x±,y±) → 三圆筒 → 后偏转(x±,y±)\n"
            "          → 漂移管 → 加速电压 → 加速后三圆筒1/2 → 电偏转1/2 → 第一/二组电磁场电流\n\n"
            "探测器 FC1~FC5 沿束线依次布置，选定探测器后可自动勾选其上游透镜参与优化。\n"
            "偏转透镜 (x±, y±) 成对调节束流上下左右；建议在优化前先粗调使束流命中探测器。"
        )
        ttk.Label(lf3, text=about, justify=tk.LEFT, font=("Microsoft YaHei", 9),
                  foreground="#333").pack(anchor=tk.W)

    # ---------- 探测器联动 ----------
    def _on_detector_change(self):
        fc = self.selected_detector.get()
        self.detector_desc_var.set(f"{fc}: {DETECTOR_DESC[fc]}")

    def _auto_select_by_detector(self):
        fc = self.selected_detector.get()
        upstream = set(upstream_lenses(fc))
        for name in LENS_ORDER:
            self.lens_configs[name].enabled = (name in upstream)
        self._populate_lens_tree()
        self._set_status(f"已按 {fc} 位置自动勾选上游透镜（共 {len(upstream)} 个）")

    # ---------- 优化核心 ----------
    def _active_lenses(self) -> List[str]:
        return [n for n in LENS_ORDER if self.lens_configs[n].enabled]

    def _build_optimizer(self) -> Optional[BayesianOptimizer]:
        names = self._active_lenses()
        if not names:
            messagebox.showwarning("无参数", "请至少勾选一个透镜参与优化")
            return None
        bounds = [(self.lens_configs[n].vmin, self.lens_configs[n].vmax) for n in names]
        opt = BayesianOptimizer(
            param_names=names, bounds=bounds,
            seed=self.seed_var.get(), xi=self.xi_var.get(),
            n_initial=self.n_init_var.get(),
        )
        return opt

    def start_optimization(self):
        if self.running:
            return
        self.optimizer = self._build_optimizer()
        if self.optimizer is None:
            return
        self.records = []
        self.iteration = 0
        self.running = True
        self._update_run_buttons()
        self.prog_bar["maximum"] = self.n_iter_var.get()
        self.prog_bar["value"] = 0
        self._clear_hist()
        self._set_status("优化已启动，正在生成第一个建议...")
        # 启动后台线程
        t = threading.Thread(target=self._optimization_loop, daemon=True)
        t.start()

    def stop_optimization(self):
        self.running = False
        self._set_status("已请求停止，当前迭代完成后结束")
        self._update_run_buttons()

    def reset_optimizer(self):
        if self.running:
            messagebox.showinfo("运行中", "请先停止优化再重置")
            return
        self.optimizer = None
        self.records = []
        self.iteration = 0
        self._pending_x = None
        self._pending_names = None
        self._clear_hist()
        self._set_suggestion("（已重置，点击「开始优化」或「单步建议」）")
        self.progress_var.set("迭代: 0 / 0   最优电流: --")
        self.prog_bar["value"] = 0
        self.best_var.set("最优: --")
        self._refresh_plot()
        self._set_status("优化器已重置")

    def single_step(self):
        if self.running:
            messagebox.showinfo("运行中", "自动优化正在运行，请先停止")
            return
        if self.optimizer is None:
            self.optimizer = self._build_optimizer()
            if self.optimizer is None:
                return
        self._do_one_suggestion()

    def _do_one_suggestion(self):
        """生成一个建议点，等待读数（手动或串口）。"""
        if self.optimizer is None:
            return
        x = self.optimizer.suggest()
        names = self.optimizer.names
        self._pending_x = x
        self._pending_names = names
        # 更新当前值
        for n, v in zip(names, x):
            self.lens_configs[n].current = float(v)
        self._populate_lens_tree()
        # 显示建议
        lines = [f"迭代 #{self.iteration + 1}  探测器: {self.selected_detector.get()}"]
        lines.append("-" * 50)
        for n, v in zip(names, x):
            lines.append(f"  {n:<16s} = {v: .5g}")
        self._set_suggestion("\n".join(lines))
        self.btn_confirm.configure(state=tk.NORMAL)
        self.manual_reading_var.set("")
        self._set_status(f"已生成建议 #{self.iteration + 1}，请设置参数并读取电流")

        # 串口自动读数
        if self.serial_enabled.get():
            t = threading.Thread(target=self._serial_read_and_confirm, daemon=True)
            t.start()

    def confirm_reading(self):
        if self._pending_x is None:
            return
        raw = self.manual_reading_var.get().strip()
        if not raw:
            messagebox.showwarning("无读数", "请输入实测电流")
            return
        try:
            current = float(raw)
        except ValueError:
            messagebox.showerror("格式错误", "电流必须为数字")
            return
        self._record_observation(current)
        self.btn_confirm.configure(state=tk.DISABLED)

    def _serial_read_and_confirm(self):
        try:
            import serial
        except ImportError:
            self.root.after(0, lambda: messagebox.showerror("缺少依赖", "请先 pip install pyserial"))
            return
        try:
            time.sleep(self.settle_var.get())
            if self._ser is None:
                self._ser = serial.Serial(self.serial_port.get(), self.serial_baud.get(), timeout=2)
            cmd = (self.serial_cmd_var.get().strip() + "\n").encode()
            self._ser.write(cmd)
            resp = self._ser.readline().decode(errors="ignore").strip()
            current = float(resp)
            self.root.after(0, lambda: self.manual_reading_var.set(f"{current:.6g}"))
            self.root.after(0, self._record_observation, current)
        except Exception as e:
            self.root.after(0, lambda: self._set_status(f"串口读数失败: {e}"))

    def _record_observation(self, current: float):
        if self._pending_x is None or self.optimizer is None:
            return
        x = self._pending_x
        names = self._pending_names
        self.optimizer.observe(x, current)
        self.iteration += 1
        rec = OptRecord(
            iteration=self.iteration,
            detector=self.selected_detector.get(),
            values={n: float(v) for n, v in zip(names, x)},
            current=float(current),
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.records.append(rec)
        self._add_hist_row(rec)
        best = self.optimizer.best_value
        self.progress_var.set(f"迭代: {self.iteration} / {self.n_iter_var.get()}   最优电流: {best:.5g} A")
        self.best_var.set(f"最优: {best:.5g} A  (迭代 #{int(np.argmax(self.optimizer.Y)) + 1})")
        self.prog_bar["value"] = self.iteration
        self._refresh_plot()
        self._pending_x = None
        self._pending_names = None
        self._set_status(f"迭代 #{self.iteration} 完成，电流 {current:.5g} A，最优 {best:.5g} A")

        # 自动继续
        if self.running and self.iteration < self.n_iter_var.get():
            self.root.after(300, self._do_one_suggestion)
        elif self.running and self.iteration >= self.n_iter_var.get():
            self.running = False
            self._update_run_buttons()
            self._set_status(f"优化完成！共 {self.iteration} 次迭代，最优电流 {best:.5g} A")
            self._show_best_result()

    def _optimization_loop(self):
        """后台线程仅触发第一步，后续由 confirm_reading → root.after 链式推进。"""
        self.root.after(0, self._do_one_suggestion)

    def _show_best_result(self):
        if self.optimizer is None:
            return
        best_x = self.optimizer.best_x
        names = self.optimizer.names
        lines = ["最优参数配置：", "=" * 40]
        for n, v in zip(names, best_x):
            lines.append(f"  {n:<16s} = {v: .5g}")
        lines.append("-" * 40)
        lines.append(f"最优电流 = {self.optimizer.best_value:.5g} A")
        messagebox.showinfo("优化完成", "\n".join(lines))

    def _update_run_buttons(self):
        if self.running:
            self.btn_start.configure(state=tk.DISABLED)
            self.btn_stop.configure(state=tk.NORMAL)
        else:
            self.btn_start.configure(state=tk.NORMAL)
            self.btn_stop.configure(state=tk.DISABLED)

    # ---------- 建议文本 ----------
    def _set_suggestion(self, text: str):
        self.suggestion_text.configure(state=tk.NORMAL)
        self.suggestion_text.delete("1.0", tk.END)
        self.suggestion_text.insert(tk.END, text)
        self.suggestion_text.configure(state=tk.DISABLED)

    # ---------- 历史记录 ----------
    def _clear_hist(self):
        self.hist_tree.delete(*self.hist_tree.get_children())
        self.hist_detail.delete("1.0", tk.END)

    def _add_hist_row(self, rec: OptRecord):
        best = max(r.current for r in self.records)
        self.hist_tree.insert("", tk.END, iid=str(rec.iteration), values=(
            rec.iteration, rec.detector, f"{rec.current:.5g}", f"{best:.5g}", rec.timestamp
        ))
        self.hist_tree.yview_moveto(1.0)

    def _on_hist_select(self, event):
        sel = self.hist_tree.selection()
        if not sel:
            return
        it = int(sel[0])
        rec = next((r for r in self.records if r.iteration == it), None)
        if rec is None:
            return
        lines = [f"迭代 #{rec.iteration}  探测器: {rec.detector}  电流: {rec.current:.5g} A  ({rec.timestamp})", ""]
        for n, v in rec.values.items():
            lines.append(f"  {n:<16s} = {v: .5g}")
        self.hist_detail.delete("1.0", tk.END)
        self.hist_detail.insert(tk.END, "\n".join(lines))

    def export_csv(self):
        if not self.records:
            messagebox.showinfo("无数据", "暂无优化记录可导出")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile=f"bo_tuning_{time.strftime('%Y%m%d_%H%M%S')}.csv",
        )
        if not path:
            return
        all_names = LENS_ORDER
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write("iteration,detector,current_A,best_A,timestamp," + ",".join(all_names) + "\n")
            best = -np.inf
            for rec in self.records:
                best = max(best, rec.current)
                vals = [f"{rec.values.get(n, ''):.6g}" if n in rec.values else "" for n in all_names]
                f.write(f"{rec.iteration},{rec.detector},{rec.current:.6g},{best:.6g},{rec.timestamp},"
                        + ",".join(vals) + "\n")
        self._set_status(f"已导出 {len(self.records)} 条记录到 {path}")

    def clear_history(self):
        if not messagebox.askyesno("确认", "确定清空所有历史记录？（优化器状态保留）"):
            return
        self.records = []
        self._clear_hist()
        self.best_var.set("最优: --")
        self._refresh_plot()

    # ---------- 收敛曲线 ----------
    def _refresh_plot(self):
        self.ax.clear()
        if self.records:
            iters = [r.iteration for r in self.records]
            currents = [r.current for r in self.records]
            best_so_far = np.maximum.accumulate(currents)
            self.ax.plot(iters, currents, "o-", color="#2d6cdf", markersize=4, label="实测电流", alpha=0.8)
            self.ax.plot(iters, best_so_far, "s-", color="#e67e22", markersize=3, label="历史最优", linewidth=2)
            self.ax.set_xlabel("迭代次数")
            self.ax.set_ylabel("探测器电流 (A)")
            self.ax.set_title(f"贝叶斯优化收敛曲线  ({self.selected_detector.get()})")
            self.ax.legend(loc="best", fontsize=9)
            self.ax.grid(True, alpha=0.3)
        else:
            self.ax.text(0.5, 0.5, "暂无数据\n开始优化后显示收敛曲线",
                          ha="center", va="center", transform=self.ax.transAxes, color="#999")
            self.ax.set_xticks([]); self.ax.set_yticks([])
        self.fig.tight_layout()
        self.canvas.draw_idle()

    # ---------- 配置保存/加载 ----------
    def save_config(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("JSON", "*.json")],
            initialfile="ion_optics_config.json",
        )
        if not path:
            return
        data = {
            "lenses": {n: c.to_dict() for n, c in self.lens_configs.items()},
            "detector": self.selected_detector.get(),
            "n_iter": self.n_iter_var.get(),
            "n_init": self.n_init_var.get(),
            "xi": self.xi_var.get(),
            "seed": self.seed_var.get(),
            "settle": self.settle_var.get(),
            "serial": {
                "enabled": self.serial_enabled.get(),
                "port": self.serial_port.get(),
                "baud": self.serial_baud.get(),
                "cmd": self.serial_cmd_var.get(),
            },
            "records": [asdict(r) for r in self.records],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self._set_status(f"配置已保存到 {path}")

    def load_config(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for n, d in data.get("lenses", {}).items():
                if n in self.lens_configs:
                    self.lens_configs[n] = LensConfig.from_dict(d)
            self.selected_detector.set(data.get("detector", "FC1"))
            self._on_detector_change()
            self.n_iter_var.set(data.get("n_iter", 30))
            self.n_init_var.set(data.get("n_init", 3))
            self.xi_var.set(data.get("xi", 0.01))
            self.seed_var.set(data.get("seed", 42))
            self.settle_var.set(data.get("settle", 1.0))
            s = data.get("serial", {})
            self.serial_enabled.set(s.get("enabled", False))
            self.serial_port.set(s.get("port", "COM3"))
            self.serial_baud.set(s.get("baud", 9600))
            self.serial_cmd_var.set(s.get("cmd", "READ?"))
            self.records = [OptRecord(**r) for r in data.get("records", [])]
            self._clear_hist()
            for rec in self.records:
                self._add_hist_row(rec)
            self.iteration = len(self.records)
            self._populate_lens_tree()
            self._refresh_plot()
            if self.records:
                best = max(r.current for r in self.records)
                self.best_var.set(f"最优: {best:.5g} A")
            self._set_status(f"配置已从 {path} 加载")
        except Exception as e:
            messagebox.showerror("加载失败", f"{e}\n{traceback.format_exc()}")

    # ---------- 状态栏 ----------
    def _set_status(self, msg: str):
        self.status_var.set(msg)


# =====================================================================
# 入口
# =====================================================================
def main():
    root = tk.Tk()
    app = IonOpticsTunerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
