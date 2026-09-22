"""自动调束页：建议 → 人工确认 → 执行，全过程跟踪。

与旧版的根本区别：旧版在页面里用一条公式加噪声"演"出收敛曲线，从不碰设备。
现在页面对接执行服务，走架构文档 6.6 规定的链路：

    优化器提候选 → 人工确认 → 执行层校验(边界/单步/速率) → 写设备
    → 等读回稳定 → 测目标 → 记录本轮

两个刻意的设计：

* **候选值不等于已执行值**。每轮分别记录建议值、实际下发值、实际回读值与
  目标测量；只显示建议值会让人误以为设备已经动过了（文档 9.4）。
* **自动模式必须带束流保护**。auto 模式（全自动写入）在服务端启动校验里
  强制要求绝对/相对损失阈值至少一个有效；界面在切换模式时同步提示。
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from apps.desktop_client import instrument_api, tuning_analysis, tuning_config
from apps.desktop_client.pages.common import page_layout, primary_button
from apps.desktop_client.pages.registry import PageSpec
from apps.desktop_client.pv_mapping_api import PvMappingRequestThread
from apps.desktop_client.spectrum_plot import SpectrumPlot
from apps.desktop_client.widgets import MetricCard, PageHeading, Panel

POLL_INTERVAL_MS = 700
DEFAULT_TARGET = "detector.fc1.beam_current"
DASHBOARD_URL = "http://127.0.0.1:8001/"

# 结束后的三种处置动作（与执行服务的动作名一一对应）。
# 界面自己写一遍中文名，不 import 执行服务的模块——客户端不该依赖服务端实现。
FINALIZE_LABELS = {
    "apply_best": "应用最优参数",
    "restore_initial": "恢复启动前参数",
    "start_values": "回到起始值",
    "safe_values": "回安全值",
}

# 优化策略（键名与执行服务一致）
STRATEGY_CHOICES = (
    ("joint", "全部变量联合优化"),
    ("sequential_then_joint", "逐参数优化 → 联合微调"),
)

# 阶段名 → 界面文案
STAGE_TEXT = {"sequential": "逐参数优化", "joint": "联合微调"}

# 优化引擎（键名与执行服务一致）。界面自己写中文名，不 import 服务端实现。
# 默认 CMA-ES：自动调束的响应面是连续、光滑、单峰、需要多参数联合对准的类型，
# 这正是 CMA-ES 的主场（实测同一 SIM 响应面 80 轮：CMA-ES 均值 11.65 nA，
# TPE 50 轮均值仅 9.75 nA）。TPE 更适合离散/条件参数或响应面形状未知的场景，保留可选。
ENGINE_CHOICES = (
    ("cmaes", "CMA-ES（推荐：连续联合调参）"),
    ("tpe", "TPE（离散/条件参数或形状未知）"),
    ("qmc", "QMC（Optuna）"),
    ("random", "Random（Optuna）"),
    ("grid", "Grid（Optuna）"),
)

# 调束模式：confirm 每轮人工确认；auto 全自动（候选直接进执行层）
MODE_CHOICES = (
    ("auto", "全自动（无需确认）"),
    ("confirm", "建议 → 人工确认"),
)

# 老版执行服务不带 tunable / beam_target 标记时的**过渡**回退规则。
# 权威规则在设备档案（`apps/instrument_service/device_profiles.py`）里，这里只是
# 为了"服务端比客户端旧"时不至于整个页面变成 0 行——那种静默失效比报错更难查。
# 走回退时界面上会明确写出来。
_FALLBACK_TUNABLE_SUFFIXES = (
    ".flow_setpoint",
    ".power_setpoint",
    ".power_5k_setpoint",
    ".voltage_setpoint",
    ".current_setpoint",
)
_FALLBACK_NOT_TUNABLE_SUFFIXES = (".current_rate_setpoint",)
_FALLBACK_TARGET_SUFFIXES = (".beam_current",)


def _fallback_tunable(entry: dict) -> bool:
    signal = str(entry.get("signal") or "")
    if not entry.get("writable") or entry.get("role") != "setpoint":
        return False
    if signal.endswith(_FALLBACK_NOT_TUNABLE_SUFFIXES):
        return False
    return signal.endswith(_FALLBACK_TUNABLE_SUFFIXES)


def _fallback_beam_target(entry: dict) -> bool:
    return str(entry.get("signal") or "").endswith(_FALLBACK_TARGET_SUFFIXES)
TERMINAL_STATES = frozenset({"completed", "aborted", "failed", "recovery_required"})
STATE_TEXT = {
    "draft": "待提交",
    "validating": "校验参数",
    "preparing": "申请设备",
    "running": "调束进行中",
    "awaiting_confirmation": "等待人工确认候选",
    "applying": "写入设备并等待稳定",
    "paused": "已暂停（参数冻结）",
    "stop_requested": "停止中",
    "completed": "调束完成",
    "aborted": "已停止",
    "failed": "调束失败",
    "recovery_required": "需人工确认设备状态",
}


class TuningPage(QWidget):
    """自动调束页（建议 → 人工确认）。"""

    def __init__(self) -> None:
        super().__init__()
        self._last_completed = 0
        self._iterations_in_flight = False
        self._mapping: list[dict] = []
        self._rows: list[dict] = []
        self._catalog: dict = {}
        self._catalog_request = None
        self._used_fallback = False
        # 启动前快照 / 目标基线 / 回退记录（由状态接口回传）
        self._snapshot: dict = {}
        self._baseline: float | None = None
        self._recovery: dict | None = None
        self._snapshot_note = ""
        self._run_id: str | None = None
        self._state = "idle"
        self._pending: dict | None = None
        self._iterations: list[dict] = []
        self._analysis: dict | None = None  # 结束后从 /analysis 拉的 Optuna 分析结果
        self._analysis_items: list[object] = []
        self._dashboard_url = DASHBOARD_URL
        self._status_in_flight = False
        self._read_in_flight = False
        self._best_line = None
        # 本机保存的调束配置只在第一次建好变量行之后载入一次（见 _maybe_apply_saved_config）
        self._config_applied = False
        # 开跑前的建议（例如变量过多）与算法信息：导出日志要带上，便于复盘
        self._startup_advice: list[str] = []
        self._algorithm = ""
        self._seed: int | None = None
        self._mode = "confirm"

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)

        layout = page_layout(self)
        layout.addWidget(
            PageHeading(
                "自动调束",
                "优化器只提出候选参数；确认后才由执行服务做边界/最大单步/变化速率"
                "校验并写入设备，再等读回稳定、测量目标。每轮的建议值、实际下发值、"
                "实际回读值分开记录。",
            )
        )
        self.tabs = QTabWidget()
        self.tabs.addTab(self._configuration_tab(), "1 参数配置")
        self.tabs.addTab(self._monitor_tab(), "2 运行监控")
        self.tabs.addTab(self._result_tab(), "3 结果确认")
        self.tabs.setTabEnabled(1, False)
        self.tabs.setTabEnabled(2, False)
        layout.addWidget(self.tabs, 1)

        self._load_mapping()

    # ------------------------------------------------------------------
    # 页签 1：参数配置
    # ------------------------------------------------------------------
    def _configuration_tab(self) -> QWidget:
        tab = QWidget()
        outer = QHBoxLayout(tab)
        outer.setContentsMargins(0, 8, 0, 0)
        outer.setSpacing(0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        variables = Panel("优化变量", "勾选参与调束的参数并设定范围")
        select_row = QHBoxLayout()
        select_row.addStretch()
        self.select_all_button = QPushButton("全选")
        self.select_none_button = QPushButton("取消全选")
        for btn in (self.select_all_button, self.select_none_button):
            btn.setFlat(True)
            select_row.addWidget(btn)
        self.select_all_button.clicked.connect(lambda: self._set_all_variables(True))
        self.select_none_button.clicked.connect(lambda: self._set_all_variables(False))
        variables.body.addLayout(select_row)
        self.parameter_table = QTableWidget(0, 6)
        self.parameter_table.setHorizontalHeaderLabels(
            ["启用", "设备参数", "下限", "上限", "起始值", "当前回读"]
        )
        self.parameter_table.verticalHeader().setVisible(False)
        self.parameter_table.itemChanged.connect(self._on_variable_item_changed)
        variables.body.addWidget(self.parameter_table, 1)
        self.variable_hint = QLabel("正在读取设备参数…", objectName="mutedText")
        self.variable_hint.setWordWrap(True)
        variables.body.addWidget(self.variable_hint)
        splitter.addWidget(variables)

        strategy = Panel("目标与策略", "优化算法与搜索策略")
        form_host = QWidget()
        form_host.setAutoFillBackground(False)
        form = QVBoxLayout(form_host)
        form.setSpacing(8)

        form.addWidget(QLabel("优化目标（最大化）", objectName="mutedText"))
        self.target = QComboBox()
        self.target.currentIndexChanged.connect(self._on_target_changed)
        form.addWidget(self.target)

        form.addWidget(QLabel("调束模式", objectName="mutedText"))
        self.mode_combo = QComboBox()
        for key, label in MODE_CHOICES:
            self.mode_combo.addItem(label, key)
        self.mode_combo.setToolTip(
            "auto：候选由执行服务自动写入（每轮仍过边界/单步/速率校验与束流保护）；"
            "confirm：每轮等你点「确认并执行本轮」。auto 必须启用束流丢失保护。"
        )
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addWidget(self.mode_combo)
        self.auto_hint = QLabel("", objectName="mutedText")
        self.auto_hint.setWordWrap(True)
        self.auto_hint.setVisible(False)
        form.addWidget(self.auto_hint)

        form.addWidget(QLabel("优化引擎", objectName="mutedText"))
        self.engine_combo = QComboBox()
        for key, label in ENGINE_CHOICES:
            self.engine_combo.addItem(label, key)
        self.engine_combo.setToolTip(
            "CMA-ES 推荐用于连续联合调参；其余为 Optuna 采样器"
            "（TPE/CMA-ES/QMC/随机/网格），同一份历史观测可复用。"
        )
        form.addWidget(self.engine_combo)

        # ---- 两阶段策略（改造报告 §5.2）----
        # 逐参数阶段容易把每个变量都推到各自的局部最优，联合微调再在这些最优点
        # 附近一起收一收；两者的轮次分配要现场可调。
        form.addWidget(QLabel("优化策略", objectName="mutedText"))
        self.strategy = QComboBox()
        for key, label in STRATEGY_CHOICES:
            self.strategy.addItem(label, key)
        self.strategy.setToolTip(
            "全部联合：所有勾选变量一起优化；"
            "逐参数 → 联合微调：先一个变量一个变量调（其余保持不动），"
            "再在最优点附近联合微调"
        )
        form.addWidget(self.strategy)

        self.calls_spin = QSpinBox()
        self.calls_spin.setRange(1, 50)
        self.calls_spin.setValue(3)
        self.calls_label = QLabel("逐参数阶段：每个变量用几轮", objectName="mutedText")
        form.addWidget(self.calls_label)
        form.addWidget(self.calls_spin)

        self.joint_frac_spin = QDoubleSpinBox()
        self.joint_frac_spin.setRange(1.0, 100.0)
        self.joint_frac_spin.setSingleStep(5.0)
        self.joint_frac_spin.setValue(20.0)
        self.joint_frac_spin.setSuffix(" %")
        self.joint_frac_label = QLabel("联合微调范围（占各参数原范围）", objectName="mutedText")
        form.addWidget(self.joint_frac_label)
        form.addWidget(self.joint_frac_spin)

        self.hold_spin = QDoubleSpinBox()
        self.hold_spin.setRange(0.0, 600.0)
        self.hold_spin.setSingleStep(1.0)
        self.hold_spin.setValue(0.0)
        self.hold_spin.setSuffix(" s")
        form.addWidget(QLabel("每轮写完后额外保持", objectName="mutedText"))
        form.addWidget(self.hold_spin)

        self.iterations_spin = QSpinBox()
        self.iterations_spin.setRange(1, 200)
        self.iterations_spin.setValue(15)
        form.addWidget(QLabel("最大轮次", objectName="mutedText"))
        form.addWidget(self.iterations_spin)

        self.settle_spin = QDoubleSpinBox()
        self.settle_spin.setRange(0.5, 120.0)
        self.settle_spin.setSingleStep(0.5)
        self.settle_spin.setValue(20.0)
        self.settle_spin.setSuffix(" s")
        form.addWidget(QLabel("回读稳定超时", objectName="mutedText"))
        form.addWidget(self.settle_spin)

        self.samples_spin = QSpinBox()
        self.samples_spin.setRange(1, 20)
        self.samples_spin.setValue(3)
        form.addWidget(QLabel("每轮目标采样次数", objectName="mutedText"))
        form.addWidget(self.samples_spin)

        # ---- 高级参数（原 demo 的"调参参数"区）----
        # 只开放**执行服务真的会用**的两个：观测噪声进 GP 的核，种子决定随机探索
        # 路径（同一份配置 + 同一种子可复现）。原 demo 的"初始采样数"没有对应物：
        # 平台的第一个候选固定取范围中心（见 packages/optimizer/bayes.py），
        # 不做初始随机撒点——给一个不受任何代码影响的输入框只会误导操作员。
        self.seed_spin = QSpinBox()
        self.seed_spin.setRange(0, 999999)
        self.seed_spin.setValue(0)
        self.seed_spin.setToolTip(
            "随机种子：同一份配置 + 同一种子可复现同一串候选；换种子会换探索路径"
        )
        form.addWidget(QLabel("随机种子", objectName="mutedText"))
        form.addWidget(self.seed_spin)

        # 每次使用随机种子：勾选后忽略固定 seed_spin，开始时现场生成
        self.random_seed_check = QCheckBox("每次使用随机种子（忽略上面的固定种子）")
        self.random_seed_check.setChecked(False)
        self.random_seed_check.setToolTip(
            "勾选后每次开始调束都用新的随机种子，第一轮点不再重复；"
            "不勾选则用上面的固定种子，路径可复现。"
        )
        self.random_seed_check.toggled.connect(
            lambda on: self.seed_spin.setEnabled(not on)
        )
        form.addWidget(self.random_seed_check)

        # 随机探索轮次（CMA-ES / TPE 建模前的纯随机轮数）
        self.startup_spin = QSpinBox()
        self.startup_spin.setRange(0, 100)
        self.startup_spin.setValue(5)
        self.startup_spin.setToolTip(
            "开始建模前先纯随机探索的轮数；仅 CMA-ES、TPE 生效，"
            "Random/QMC/Grid/GP 不受影响。"
        )
        form.addWidget(QLabel("随机探索轮次（仅 CMA-ES / TPE）", objectName="mutedText"))
        form.addWidget(self.startup_spin)

        self.patience_spin = QSpinBox()
        self.patience_spin.setRange(0, 50)
        self.patience_spin.setValue(5)
        self.patience_spin.setToolTip(
            "收敛早停：连续这么多轮没有产生新的历史最优就正常结束（0 = 不启用）。"
            "省掉后半段原地踏步的轮次，适合现场时间紧张时使用。"
        )
        form.addWidget(QLabel("收敛早停（连续无改进轮数，0=不启用）", objectName="mutedText"))
        form.addWidget(self.patience_spin)

        # ---- 束流丢失保护（改造报告 §5.2）----
        # 总开关：SIM/演示或确认不需要时可整个关掉
        self.loss_protection_check = QCheckBox("启用束流丢失保护")
        self.loss_protection_check.setChecked(True)
        self.loss_protection_check.setToolTip(
            "关掉后执行服务完全不做束流异常判定，也不会自动回退；"
            "SIM 演示或确认不需要该保护时使用。"
        )
        form.addWidget(self.loss_protection_check)
        # 阈值必须现场可调：FC 量级、束流工况各不相同，写死在代码里等于没有保护。
        loss_row = QHBoxLayout()
        self.loss_absolute_check = QCheckBox("目标低于")
        self.loss_absolute_spin = QDoubleSpinBox()
        self.loss_absolute_spin.setDecimals(3)
        self.loss_absolute_spin.setRange(0.0, 1e6)
        self.loss_absolute_spin.setValue(0.0)
        self.loss_absolute_spin.setMaximumWidth(110)
        self.loss_absolute_check.setChecked(False)
        self.loss_absolute_spin.setEnabled(False)
        self.loss_absolute_check.toggled.connect(self.loss_absolute_spin.setEnabled)
        loss_row.addWidget(self.loss_absolute_check)
        loss_row.addWidget(self.loss_absolute_spin)
        loss_row.addStretch()
        form.addWidget(QLabel("绝对归零阈值（束流接近零）", objectName="mutedText"))
        form.addLayout(loss_row)

        self.loss_relative_spin = QDoubleSpinBox()
        self.loss_relative_spin.setRange(0.0, 95.0)
        self.loss_relative_spin.setSingleStep(5.0)
        self.loss_relative_spin.setValue(50.0)
        self.loss_relative_spin.setSuffix(" %")
        form.addWidget(QLabel("相对损失阈值（低于启动前基线多少算丢失）", objectName="mutedText"))
        form.addWidget(self.loss_relative_spin)

        self.loss_strikes_spin = QSpinBox()
        self.loss_strikes_spin.setRange(1, 20)
        self.loss_strikes_spin.setValue(2)
        form.addWidget(QLabel("连续异常次数才触发保护", objectName="mutedText"))
        form.addWidget(self.loss_strikes_spin)

        self.auto_recover_check = QCheckBox("自动退回启动前参数")
        self.auto_recover_check.setChecked(True)
        self.auto_recover_check.setToolTip(
            "触发保护时由执行服务按启动前快照逐路退回（走执行层斜坡）；"
            "不勾选则只标记异常并停下，等人工处理"
        )
        form.addWidget(self.auto_recover_check)

        # 总开关联动子控件
        self._loss_protection_widgets = [
            self.loss_absolute_check, self.loss_absolute_spin,
            self.loss_relative_spin, self.loss_strikes_spin,
            self.auto_recover_check,
        ]
        def _toggle_loss_protection(on: bool) -> None:
            for w in self._loss_protection_widgets:
                w.setEnabled(on)
            if on:
                # 恢复绝对阈值子联动
                self.loss_absolute_spin.setEnabled(self.loss_absolute_check.isChecked())
        self.loss_protection_check.toggled.connect(_toggle_loss_protection)

        # 开始前复位：每次从安全起始点（各参数下限）开始，不继承上一次结果
        self.reset_before_start_check = QCheckBox("开始前复位到起始值（不继承上一次结果）")
        self.reset_before_start_check.setChecked(False)
        self.reset_before_start_check.setToolTip(
            "勾选后，开始调束会先把参与变量经执行层写到“起始值”列（默认为范围中值）再寻优，"
            "保证每次起点一致；不勾选则从设备当前值（可能是上次最优）开始。"
        )
        form.addWidget(self.reset_before_start_check)

        # ---- 停滞阈值（过程建议「近 10 轮提升不足 X%」的 X）----
        self.stall_fraction_form_spin = QDoubleSpinBox()
        self.stall_fraction_form_spin.setRange(0.1, 50.0)
        self.stall_fraction_form_spin.setSingleStep(0.5)
        self.stall_fraction_form_spin.setValue(5.0)
        self.stall_fraction_form_spin.setSuffix(" %")
        self.stall_fraction_form_spin.setDecimals(1)
        self.stall_fraction_form_spin.setMaximumWidth(170)
        self.stall_fraction_form_spin.setToolTip(
            "过程建议里「近 10 轮目标提升不足 X%」的 X。"
            "噪声大时调大（避免误报），要更早报警时调小。"
        )
        self.stall_fraction_form_spin.valueChanged.connect(lambda _v: self._refresh_advice())
        form.addWidget(QLabel("停滞阈值（近 10 轮提升不足多少算停滞）", objectName="mutedText"))
        form.addWidget(self.stall_fraction_form_spin)

        for widget in (
            self.iterations_spin, self.settle_spin, self.samples_spin,
            self.loss_relative_spin, self.loss_strikes_spin,
            self.seed_spin,
        ):
            widget.setMaximumWidth(170)

        # ---- 完成后处置偏好（改造报告 §5.2「完成后设备状态」/ §8.9 P2.3）----
        # 只做**预选建议**，绝不在任务结束时自动写设备：多写一次设备就多一次
        # 无人确认的动作，与"建议 → 人工确认"的第一版边界冲突。
        form.addWidget(QLabel("完成后处置建议（仅提示，执行仍需二次确认）", objectName="mutedText"))
        self.finalize_pref = QComboBox()
        self.finalize_pref.addItem("不预设", "")
        for action, label in FINALIZE_LABELS.items():
            self.finalize_pref.addItem(label, action)
        self.finalize_pref.setToolTip(
            "保存进调束配置：任务结束时在结果页提示「要处置先考虑哪一种」"
        )
        form.addWidget(self.finalize_pref)

        # ---- 配置保存与恢复（原 demo 的 bayes_config.json）----
        config_row = QHBoxLayout()
        self.save_config_button = QPushButton("保存调束配置")
        self.save_config_button.setToolTip(
            "把目标、勾选变量与范围、策略、轮次、稳定超时、采样次数、束流丢失保护"
            "和完成后处置存到本机，下次打开自动载回"
        )
        self.save_config_button.clicked.connect(self.save_tuning_config)
        config_row.addWidget(self.save_config_button)
        self.load_config_button = QPushButton("载入上次配置")
        self.load_config_button.setToolTip("从本机配置重新载入（覆盖当前界面上的设置）")
        self.load_config_button.clicked.connect(self.load_tuning_config)
        config_row.addWidget(self.load_config_button)
        config_row.addStretch()
        form.addLayout(config_row)
        self.config_note = QLabel("", objectName="mutedText")
        self.config_note.setWordWrap(True)
        form.addWidget(self.config_note)

        # 默认只保留启动任务必需的参数；低频策略与保护项按需展开，
        # 让 1320×820 下的开始按钮始终留在当前页内。
        advanced_label_texts = {
            "随机种子",
            "随机探索轮次（仅 CMA-ES / TPE）",
            "收敛早停（连续无改进轮数，0=不启用）",
            "绝对归零阈值（束流接近零）",
            "相对损失阈值（低于启动前基线多少算丢失）",
            "连续异常次数才触发保护",
            "完成后处置建议（仅提示，执行仍需二次确认）",
        }
        # 逐参数策略专属：只有选「逐参数 → 联合」时才可能显示
        self._sequential_only_widgets = [
            self.calls_spin, self.calls_label,
            self.joint_frac_spin, self.joint_frac_label,
        ]
        self._advanced_widgets = [
            self.seed_spin,
            self.random_seed_check, self.startup_spin,
            self.patience_spin,
            self.loss_absolute_check, self.loss_absolute_spin,
            self.loss_relative_spin, self.loss_strikes_spin,
            self.auto_recover_check, self.finalize_pref,
            self.save_config_button, self.load_config_button, self.config_note,
        ]
        self._advanced_widgets.extend(
            label
            for label in strategy.findChildren(QLabel)
            if label.text() in advanced_label_texts
        )
        self._advanced_visible = False
        self.strategy.currentIndexChanged.connect(self._toggle_strategy_options)
        self.advanced_button = QPushButton("展开高级参数")
        self.advanced_button.setCheckable(True)
        self.advanced_button.toggled.connect(self._toggle_advanced_options)
        self._toggle_advanced_options(False)

        form.addStretch()
        # 表单整体放进滚动区：展开高级参数后内容超高时滚动，而不是把控件压扁挤在一起。
        # 1320×820 下开始按钮始终留在页内，靠下方固定区保证（与表单本身解耦）。
        form_scroll = QScrollArea(objectName="groupScroll")
        form_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        form_scroll.setWidgetResizable(True)
        form_scroll.viewport().setAutoFillBackground(False)
        form_scroll.setWidget(form_host)
        strategy.body.addWidget(form_scroll, 1)

        # 固定区：展开/收起开关、提示、开始按钮始终可见，不随表单滚动
        strategy.body.addWidget(self.advanced_button)

        self.run_plan_label = QLabel("正在生成运行计划…", objectName="mutedText")
        self.run_plan_label.setWordWrap(True)
        strategy.body.addWidget(self.run_plan_label)
        self.preflight_label = QLabel("启动检查：请选择目标和优化变量", objectName="mutedText")
        self.preflight_label.setWordWrap(True)
        strategy.body.addWidget(self.preflight_label)

        hint = QLabel(
            "范围必须落在设备允许区间内，且参数需配置最大单步；否则启动时会被拒绝——"
            "让优化器提出一个必然写不进去的值没有意义。",
            objectName="mutedText",
        )
        hint.setWordWrap(True)
        strategy.body.addWidget(hint)
        # 全局只读部署：按钮压住的同时必须说明原因，否则操作员会反复怀疑参数
        self.read_only_note = QLabel("", objectName="mutedText")
        self.read_only_note.setWordWrap(True)
        self.read_only_note.setVisible(False)
        strategy.body.addWidget(self.read_only_note)
        self.start_button = primary_button("开始自动调束")
        self.start_button.clicked.connect(self.start_tuning)
        strategy.body.addWidget(self.start_button)
        splitter.addWidget(strategy)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([980, 300])
        outer.addWidget(splitter)
        for widget in (
            self.strategy, self.mode_combo, self.engine_combo,
            self.calls_spin, self.iterations_spin, self.settle_spin,
            self.samples_spin, self.hold_spin, self.loss_relative_spin,
        ):
            if isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self._update_run_plan)
            else:
                widget.valueChanged.connect(self._update_run_plan)
        self.loss_absolute_check.toggled.connect(self._update_run_plan)
        self._update_run_plan()
        return tab

    def _toggle_advanced_options(self, visible: bool) -> None:
        self._advanced_visible = bool(visible)
        for widget in self._advanced_widgets:
            widget.setVisible(visible)
        self._toggle_strategy_options()
        self.advanced_button.setText("收起高级参数" if visible else "展开高级参数")

    def _strategy_is_sequential(self) -> bool:
        return self.strategy.currentData() == "sequential_then_joint"

    def _toggle_strategy_options(self, *_args) -> None:
        """逐参数专属配置：高级已展开且策略选了逐参数时才显示。"""
        show = self._advanced_visible and self._strategy_is_sequential()
        for widget in self._sequential_only_widgets:
            widget.setVisible(show)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        minutes = max(0, round(seconds / 60))
        if minutes < 1:
            return "不足 1 分钟"
        if minutes < 60:
            return f"约 {minutes} 分钟"
        return f"约 {minutes // 60} 小时 {minutes % 60} 分钟"

    def _set_all_variables(self, checked: bool) -> None:
        """一键全选/取消全选；只改勾选列，范围与回读保持不动。"""
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in getattr(self, "_rows", []):
            if row["check"].checkState() != state:
                row["check"].setCheckState(state)
        self._update_variable_hint()
        self._update_run_plan()

    def _on_variable_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        self._update_variable_hint()
        self._update_run_plan()

    def _update_run_plan(self, *_args) -> None:
        """用现有配置生成紧凑计划与启动前检查，不复制服务端安全校验。"""
        if not hasattr(self, "run_plan_label"):
            return
        selected = [
            row for row in self._rows
            if row["check"].checkState() == Qt.CheckState.Checked
        ]
        total = self.iterations_spin.value()
        sequential = (
            self.calls_spin.value() * len(selected)
            if self.strategy.currentData() == "sequential_then_joint"
            else 0
        )
        joint = max(0, total - sequential) if sequential else total
        seconds = total * (
            self.settle_spin.value() + self.hold_spin.value()
            + max(0, self.samples_spin.value() - 1) * 0.05
        )
        plan = [
            f"运行计划：{len(selected)} 个变量 · {total} 轮",
            f"引擎 {self.engine_combo.currentText()}",
        ]
        if sequential:
            plan.append(f"逐参数 {sequential} 轮 + 联合 {joint} 轮")
        plan.append(f"预计至少 {self._format_duration(seconds)}")
        self.run_plan_label.setText("；".join(plan))

        checks = {
            "目标": bool(self.target.currentData()),
            "变量": bool(selected),
            "范围": bool(selected) and all(
                row["low"].value() < row["high"].value() for row in selected
            ),
            "最大单步": bool(selected) and all(
                row["entry"].get("max_step") for row in selected
            ),
            "轮次预算": not sequential or sequential < total,
        }
        if self.mode_combo.currentData() == "auto":
            checks["束流保护"] = self.loss_absolute_check.isChecked() or (
                self.loss_relative_spin.value() > 0
            )
        passed = sum(checks.values())
        pending = "、".join(name for name, ok in checks.items() if not ok)
        self.preflight_label.setText(
            f"启动检查：{passed}/{len(checks)} 项通过"
            + (f"；待处理：{pending}" if pending else "；可以提交服务端最终校验")
        )
        state = "good" if passed == len(checks) else "warn"
        self.preflight_label.setProperty("state", state)
        self.preflight_label.style().unpolish(self.preflight_label)
        self.preflight_label.style().polish(self.preflight_label)

    def _on_mode_changed(self, _index: int) -> None:
        """切换模式时同步提示：auto 必须启用束流丢失保护（服务端也会拒绝）。"""
        auto = self.mode_combo.currentData() == "auto"
        if auto:
            self.auto_hint.setText(
                "全自动模式：每轮候选由执行服务直接写入，仍需启用束流丢失保护"
                "（勾选「目标低于」或把相对损失阈值设 > 0），否则启动会被拒绝；"
                "运行中可随时暂停 / 停止。"
            )
            self.auto_hint.setVisible(True)
        else:
            self.auto_hint.setVisible(False)
        self._update_run_plan()

    # ------------------------------------------------------------------
    # 页签 2：运行监控
    # ------------------------------------------------------------------
    def _monitor_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 8, 0, 0)
        outer.setSpacing(10)

        bar = QFrame(objectName="panel")
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 10, 14, 10)
        self.tuning_state = QLabel("尚未开始")
        self.iteration_label = QLabel("已完成 0 轮", objectName="mutedText")
        self.acknowledge_button = QPushButton("确认设备状态")
        self.acknowledge_button.setVisible(False)
        self.acknowledge_button.clicked.connect(self._acknowledge)
        self.tuning_pause_button = QPushButton("暂停")
        self.tuning_pause_button.setEnabled(False)
        self.tuning_pause_button.setToolTip("不在写设备时暂停任务，参数保持现状")
        self.tuning_pause_button.clicked.connect(self.pause_tuning)
        self.tuning_resume_button = QPushButton("继续")
        self.tuning_resume_button.setEnabled(False)
        self.tuning_resume_button.setVisible(False)
        self.tuning_resume_button.clicked.connect(self.resume_tuning)
        self.tuning_stop_button = QPushButton("停止", objectName="dangerButton")
        self.tuning_stop_button.setEnabled(False)
        self.tuning_stop_button.clicked.connect(self.stop_tuning)
        row.addWidget(self.tuning_state)
        row.addWidget(self.iteration_label)
        row.addStretch()
        row.addWidget(self.acknowledge_button)
        row.addWidget(self.tuning_pause_button)
        row.addWidget(self.tuning_resume_button)
        row.addWidget(self.tuning_stop_button)
        outer.addWidget(bar)

        # 束流丢失保护的执行结果：只在真的触发过时出现，逐路列实际回读
        self.recovery_label = QLabel("", objectName="mutedText")
        self.recovery_label.setWordWrap(True)
        self.recovery_label.setVisible(False)
        outer.addWidget(self.recovery_label)

        # 过程建议（原 demo 的监控建议区）：把"值得看一眼的地方"指出来，不下结论
        self.advice_label = QLabel("", objectName="mutedText")
        self.advice_label.setWordWrap(True)
        self.advice_label.setVisible(False)
        outer.addWidget(self.advice_label)

        # 启动前快照：操作员的参照点（"从哪儿出发的"），不给就说明为什么没给
        self.snapshot_label = QLabel("", objectName="mutedText")
        self.snapshot_label.setWordWrap(True)
        self.snapshot_label.setVisible(False)
        outer.addWidget(self.snapshot_label)

        # 指标横排成一条紧凑信息带，避免占用曲线右侧整列空间。
        metrics = QHBoxLayout()
        metrics.setSpacing(10)
        self.current_card = MetricCard("本轮测量", "--")
        self.best_card = MetricCard("历史最优", "--", "", success=True)
        self.gain_card = MetricCard("提升量", "--")
        self.eta_card = MetricCard("预计剩余", "--")
        for card in (self.current_card, self.best_card, self.gain_card, self.eta_card):
            card.setMaximumHeight(92)
            metrics.addWidget(card, 1)
        outer.addLayout(metrics)

        plot_panel = Panel(
            "目标量与响应曲线",
            "可切换：目标量收敛（x = 轮次）/ 单变量响应（x = 实际回读）",
        )
        # 原 demo 用同一个画布 + 一个下拉切换"收敛 / 某个变量的响应曲线"：
        # 响应曲线是分析用的，不该另开一屏把实时监控挤掉。
        plot_choice_row = QHBoxLayout()
        plot_choice_row.addWidget(QLabel("视图", objectName="mutedText"))
        self.plot_button_row = QHBoxLayout()
        self.plot_button_row.setSpacing(6)
        plot_choice_row.addLayout(self.plot_button_row, 1)
        plot_panel.body.addLayout(plot_choice_row)
        # 兼容旧引用：_render_plot 读 currentData()，用一个隐藏的 QComboBox 存当前选择
        from PySide6.QtWidgets import QComboBox as _QCB
        self.plot_choice = _QCB()
        self.plot_choice.hide()
        self.tuning_plot = SpectrumPlot("轮次", "目标量")
        plot_panel.body.addWidget(self.tuning_plot, 1)
        self.response_note = QLabel("", objectName="mutedText")
        self.response_note.setWordWrap(True)
        plot_panel.body.addWidget(self.response_note)
        outer.addWidget(plot_panel, 1)

        recent = Panel("最近轮次", "建议值与设备实际结果分开记录")
        self.recent_table = QTableWidget(0, 5)
        self.recent_table.setHorizontalHeaderLabels(
            ["轮次", "阶段", "目标值", "质量", "结果"]
        )
        self.recent_table.verticalHeader().setVisible(False)
        self.recent_table.setMaximumHeight(150)
        recent.body.addWidget(self.recent_table)
        outer.addWidget(recent)

        confirm = QFrame(objectName="noticePanel")
        self.proposal_panel = confirm
        confirm_layout = QVBoxLayout(confirm)
        confirm_layout.setContentsMargins(14, 10, 14, 10)
        confirm_row = QHBoxLayout()
        confirm_row.setContentsMargins(0, 0, 0, 0)
        self.proposal_label = QLabel("等待候选…", objectName="mutedText")
        self.proposal_label.setWordWrap(True)
        self.approve_button = primary_button("确认并执行本轮")
        self.approve_button.setEnabled(False)
        self.approve_button.clicked.connect(self._approve)
        confirm_row.addWidget(self.proposal_label, 1)
        confirm_row.addWidget(self.approve_button)
        confirm_layout.addLayout(confirm_row)

        # 候选对比表：当前回读 vs 本轮建议值
        self.proposal_table = QTableWidget(0, 4)
        self.proposal_table.setHorizontalHeaderLabels(
            ["参数", "当前回读", "建议值", "变化"]
        )
        self.proposal_table.verticalHeader().setVisible(False)
        self.proposal_table.setMaximumHeight(140)
        self.proposal_table.setVisible(False)
        confirm_layout.addWidget(self.proposal_table)
        outer.addWidget(confirm)
        return tab

    # ------------------------------------------------------------------
    # 页签 3：结果确认
    # ------------------------------------------------------------------
    def _result_tab(self) -> QWidget:
        tab = QWidget()
        root = QVBoxLayout(tab)
        root.setContentsMargins(0, 8, 0, 0)
        scroll = QScrollArea(objectName="groupScroll")
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        host = QWidget()
        outer = QVBoxLayout(host)
        outer.setContentsMargins(0, 8, 0, 0)
        outer.setSpacing(10)
        scroll.setWidget(host)
        root.addWidget(scroll)

        notice = QFrame(objectName="noticePanel")
        notice_row = QHBoxLayout(notice)
        notice_row.setContentsMargins(14, 10, 14, 10)
        self.result_notice = QLabel("尚未完成任何调束")
        self.result_detail = QLabel("", objectName="mutedText")
        notice_row.addWidget(self.result_notice)
        notice_row.addStretch()
        notice_row.addWidget(self.result_detail)
        outer.addWidget(notice)

        summary = QHBoxLayout()
        summary.setSpacing(10)
        self.result_baseline_card = MetricCard("启动基线", "--")
        self.result_best_card = MetricCard("最佳目标", "--", "", success=True)
        self.result_gain_card = MetricCard("相对提升", "--")
        self.result_quality_card = MetricCard("有效轮次", "0 / 0")
        for card in (
            self.result_baseline_card, self.result_best_card,
            self.result_gain_card, self.result_quality_card,
        ):
            card.setMaximumHeight(92)
            summary.addWidget(card, 1)
        outer.addLayout(summary)

        analysis = Panel("结果分析", "优化历史、参数重要性、切片与 Trial 明细")
        analysis_toolbar = QHBoxLayout()
        analysis_toolbar.addWidget(QLabel("分析视图", objectName="mutedText"))
        # 按钮组：直接点切换，不再塞下拉里
        self.analysis_button_group = QButtonGroup(self)
        self.analysis_button_group.setExclusive(True)
        self.analysis_buttons_row = QHBoxLayout()
        self.analysis_buttons_row.setSpacing(4)
        # 变量很多时按钮会溢出：放进可横向滚动条，仍是一键直接切换
        buttons_host = QWidget()
        buttons_host.setLayout(self.analysis_buttons_row)
        buttons_host.setFixedHeight(30)
        self.analysis_scroll = QScrollArea()
        self.analysis_scroll.setWidget(buttons_host)
        self.analysis_scroll.setWidgetResizable(False)
        self.analysis_scroll.setFixedHeight(34)
        self.analysis_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.analysis_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.analysis_scroll.setFrameShape(QFrame.NoFrame)
        analysis_toolbar.addWidget(self.analysis_scroll, 1)
        self.open_dashboard_button = QPushButton("浏览器深度分析（英文）")
        self.open_dashboard_button.setFlat(True)
        self.open_dashboard_button.setToolTip(
            "常用分析（爬山图/参数重要性/多维切片/Trial 明细）已在左侧内嵌、"
            "中文展示；浏览器版为 optuna-dashboard，另提供等高线、平行坐标等"
            "深度交互，界面为英文。"
        )
        self.open_dashboard_button.clicked.connect(self._open_dashboard)
        analysis_toolbar.addWidget(self.open_dashboard_button)
        analysis.body.addLayout(analysis_toolbar)
        self.analysis_current_choice = "history"
        self._rebuild_analysis_buttons([("优化历史", "history")])
        self.analysis_plot = SpectrumPlot("轮次", "目标量")
        analysis.body.addWidget(self.analysis_plot)
        self.analysis_note = QLabel(
            "任务结束后自动加载 Optuna 分析；优化历史始终可由本地轮次生成。",
            objectName="mutedText",
        )
        self.analysis_note.setWordWrap(True)
        analysis.body.addWidget(self.analysis_note)
        self.analysis_trial_table = QTableWidget(0, 3)
        self.analysis_trial_table.setHorizontalHeaderLabels(["Trial", "状态", "目标值"])
        self.analysis_trial_table.verticalHeader().setVisible(False)
        self.analysis_trial_table.setMaximumHeight(180)
        analysis.body.addWidget(self.analysis_trial_table)
        outer.addWidget(analysis)

        # 必须是实例属性：旧版把它写成局部变量，真实结果根本回填不进去
        self.changes_table = QTableWidget(0, 5)
        self.changes_table.setHorizontalHeaderLabels(
            ["参数", "启动前", "最优轮", "最后一轮", "当前"]
        )
        self.changes_table.verticalHeader().setVisible(False)
        changes = Panel("参数变化", "来自每轮的实际回读，不用建议值")
        changes.body.addWidget(self.changes_table, 1)
        self.change_note = QLabel("", objectName="mutedText")
        self.change_note.setWordWrap(True)
        changes.body.addWidget(self.change_note)
        outer.addWidget(changes, 1)

        # 调束日志（原 demo 的日志区）：建议 → 下发 → 回读 → 目标，一行一轮。
        # 查看与导出用同一份文本，导出的 JSONL 另加 meta/advice/end 三类行。
        log_panel = Panel("调束日志", "一行一轮：阶段 · 建议 → 下发 → 回读 → 目标")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(150)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        log_panel.body.addWidget(self.log_view)
        log_row = QHBoxLayout()
        self.export_log_button = QPushButton("导出日志（JSONL）")
        self.export_log_button.setToolTip(
            "一轮一行导出，含本次任务的目标量、算法版本、种子、基线与过程建议，"
            "便于事后用脚本复盘"
        )
        self.export_log_button.clicked.connect(self.export_tuning_log)
        log_row.addWidget(self.export_log_button)
        self.log_note = QLabel("", objectName="mutedText")
        self.log_note.setWordWrap(True)
        log_row.addWidget(self.log_note, 1)
        log_panel.body.addLayout(log_row)
        outer.addWidget(log_panel)

        # 结束后的设备处置（改造报告 §5.2）：任务结束时设备停在**最后一轮**参数上，
        # 而那不一定是最优轮，所以三种收尾方式要由操作员明确选，且都要二次确认。
        disposal = Panel("结束后设备处置", "三种动作都会写设备，执行前需二次确认")
        disposal_row = QHBoxLayout()
        self.finalize_buttons: dict[str, QPushButton] = {}
        for action, label in FINALIZE_LABELS.items():
            button = QPushButton(label)
            button.setEnabled(False)
            button.clicked.connect(
                lambda _checked=False, a=action: self.finalize_tuning(a)
            )
            disposal_row.addWidget(button)
            self.finalize_buttons[action] = button
        disposal_row.addStretch()
        disposal.body.addLayout(disposal_row)
        note = QLabel(
            "「应用最优参数」按最优轮下发；「恢复启动前参数」用启动前快照；"
            "「回到起始值」写到“起始值”列；「回安全值」退到配置下限。"
            "四种都走执行层（含单步与速率约束），并逐路等回读到位后才报成功。",
            objectName="mutedText",
        )
        note.setWordWrap(True)
        disposal.body.addWidget(note)
        self.finalize_label = QLabel("", objectName="mutedText")
        self.finalize_label.setWordWrap(True)
        disposal.body.addWidget(self.finalize_label)
        outer.addWidget(disposal)
        return tab

    # ------------------------------------------------------------------
    # 设备参数载入
    # ------------------------------------------------------------------
    def _load_mapping(self) -> None:
        self._request = PvMappingRequestThread(
            instrument_api.instrument_base_url(), None
        )
        self._request.completed.connect(self._on_mapping)
        self._request.start()
        # 目录（目标/变量白名单 + 束线拓扑）是可选增强：拿不到就退回映射里的
        # tunable / beam_target 标记，界面照样能过滤，只是没有"按位置自动勾选上游"。
        self._catalog_request = instrument_api.request_tuning_catalog()
        self._catalog_request.completed.connect(self._on_catalog)
        self._capabilities_request = instrument_api.request_tuning_capabilities()
        self._capabilities_request.completed.connect(self._on_capabilities)

    def _on_capabilities(self, payload: dict) -> None:
        if not payload.get("ok"):
            return
        data = payload.get("payload") or {}
        dashboard = data.get("dashboard") or {}
        self._dashboard_url = str(dashboard.get("url") or DASHBOARD_URL)
        available = bool(dashboard.get("available"))
        self.open_dashboard_button.setEnabled(available)
        if available:
            self.open_dashboard_button.setToolTip(
                "常用分析已在左侧内嵌、中文展示；浏览器版（英文）："
                + self._dashboard_url
            )
        else:
            self.open_dashboard_button.setToolTip(
                "optuna-dashboard 未安装或未成功启动"
            )

    def _on_catalog(self, payload: dict) -> None:
        if not payload.get("ok"):
            self._catalog = {}
            self._update_variable_hint()
            self._maybe_apply_saved_config()
            return
        self._catalog = payload.get("payload") or {}
        self._update_variable_hint()
        # 按拓扑自动勾选上游参数是"没保存过配置时"的便利；保存过就以保存的勾选为准，
        # 否则目录晚到一次就把操作员上次的变量选择冲掉
        if not self._config_applied:
            self._select_upstream_of_current_target()
        self._maybe_apply_saved_config()

    def _on_mapping(self, payload: dict) -> None:
        if not payload.get("ok"):
            self.variable_hint.setText(
                f"读取设备参数失败：{payload.get('message', '')}。请确认执行服务已启动。"
            )
            self.variable_hint.setProperty("state", "error")
            return
        self._mapping = list((payload.get("config") or {}).get("entries", []))
        self._build_variable_rows()
        self._build_targets()
        self._refresh_readbacks()
        self._maybe_apply_saved_config()

    def _flags_present(self) -> bool:
        """整份映射里**有没有**调束标记。

        有标记（新服务端）时：**没标记的那条就是不参与**——这是设备档案的默认值，
        不能因为一条没标就把整页退回命名规则去猜。
        一条都没有（老服务端）时才回退，并在提示里说明。
        """
        return any(
            "tunable" in entry or "beam_target" in entry for entry in self._mapping
        )

    def _tunable(self, entry: dict) -> bool:
        """可调变量标记；服务端没带这个字段时按命名规则回退（并记下走了回退）。"""
        if "tunable" in entry:
            return bool(entry.get("tunable"))
        if self._flags_present():
            return False
        self._used_fallback = True
        return _fallback_tunable(entry)

    def _beam_target(self, entry: dict) -> bool:
        """束流目标标记；服务端没带这个字段时按命名规则回退。"""
        if "beam_target" in entry:
            return bool(entry.get("beam_target"))
        if self._flags_present():
            return False
        self._used_fallback = True
        return _fallback_beam_target(entry)

    def _build_variable_rows(self) -> None:
        # 只列**设备档案标记为可调**的设定量：能写不等于该拿去优化——磁铁的
        # current_rate_setpoint 是保护参数，气压/电压回读根本不是设定量。
        candidates = [
            entry
            for entry in self._mapping
            if entry.get("writable")
            and entry.get("role") == "setpoint"
            and self._tunable(entry)
        ]
        self.parameter_table.setRowCount(len(candidates))
        self._rows = []
        for row, entry in enumerate(candidates):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(Qt.CheckState.Unchecked)
            self.parameter_table.setItem(row, 0, check)
            # 只显示设备参数的**标题**（label）：PV 名对操作员没有信息量，还把表格挤窄。
            # 想核对 PV 的走「系统设置 → PV 映射」（那里有连接状态与当前值），
            # 表格里保留成 tooltip 只是顺手，不作为主要入口。
            name = QTableWidgetItem(str(entry.get("label", entry.get("signal"))))
            name.setToolTip(f"业务信号：{entry.get('signal', '')}\nPV：{entry.get('pv', '')}")
            self.parameter_table.setItem(row, 1, name)

            low = entry.get("min_value")
            high = entry.get("max_value")
            low_spin = QDoubleSpinBox()
            high_spin = QDoubleSpinBox()
            for spin in (low_spin, high_spin):
                spin.setDecimals(2)
                spin.setRange(
                    float(low) if low is not None else -1e9,
                    float(high) if high is not None else 1e9,
                )
            low_spin.setValue(float(low) if low is not None else 0.0)
            high_spin.setValue(float(high) if high is not None else 0.0)
            # 起始值：默认范围中值，可改；开始前复位、结束后"回到起始值"都用它
            start_spin = QDoubleSpinBox()
            start_spin.setDecimals(2)
            start_spin.setRange(
                float(low) if low is not None else -1e9,
                float(high) if high is not None else 1e9,
            )
            midpoint = (
                (float(low) + float(high)) / 2.0
                if low is not None and high is not None else 0.0
            )
            start_spin.setValue(midpoint)
            low_spin.valueChanged.connect(self._update_run_plan)
            high_spin.valueChanged.connect(self._update_run_plan)
            self.parameter_table.setCellWidget(row, 2, low_spin)
            self.parameter_table.setCellWidget(row, 3, high_spin)
            self.parameter_table.setCellWidget(row, 4, start_spin)
            readback = QLabel("--")
            readback.setObjectName("mutedText")
            self.parameter_table.setCellWidget(row, 5, readback)
            self._rows.append(
                {
                    "entry": entry,
                    "check": check,
                    "low": low_spin,
                    "high": high_spin,
                    "start": start_spin,
                    "readback": readback,
                }
            )
        self.parameter_table.resizeColumnsToContents()
        self._update_variable_hint()
        self._update_run_plan()

    def _build_targets(self) -> None:
        self.target.clear()
        for entry in self._mapping:
            # 只有设备档案标记为束流测量的量能当目标：原来凡是只读信号都能选，
            # 于是气压、电压回读也能被"最大化"（改造报告 §5.2）。
            if not self._beam_target(entry) or entry.get("writable"):
                continue
            self.target.addItem(
                f"{entry.get('label', entry.get('signal'))}（{entry.get('unit', '')}）",
                str(entry.get("signal")),
            )
        for index in range(self.target.count()):
            if self.target.itemData(index) == DEFAULT_TARGET:
                self.target.setCurrentIndex(index)
                break

    def _select_upstream_of_current_target(self) -> None:
        """按束线拓扑勾选当前目标的上游参数（原 demo 的「探测器联动」）。

        上游关系来自执行服务算出的目录（拓扑在 `packages/domain/beamline.py`），
        界面不自己按分组顺序猜。拿不到目录时不做联动，但也不会乱勾。
        """
        target = self.target.currentData()
        upstream = list((self._catalog.get("upstream") or {}).get(str(target), []))
        if not upstream:
            return
        wanted = set(upstream)
        changed = 0
        for row in self._rows:
            signal = str(row["entry"].get("signal"))
            check = row["check"]
            if signal in wanted:
                if check.checkState() != Qt.CheckState.Checked:
                    check.setCheckState(Qt.CheckState.Checked)
                    changed += 1
            elif check.checkState() == Qt.CheckState.Checked:
                check.setCheckState(Qt.CheckState.Unchecked)
        if changed:
            self._update_variable_hint()
            self._refresh_readbacks()

    def _on_target_changed(self, _index: int) -> None:
        self._refresh_readbacks()
        self._select_upstream_of_current_target()
        self._update_run_plan()

    def _update_variable_hint(self) -> None:
        """把"为什么只列出这些参数"讲清楚，而不是让操作员猜。"""
        usable = sum(1 for row in self._rows if row["entry"].get("max_step"))
        checked = sum(
            1 for row in self._rows if row["check"].checkState() == Qt.CheckState.Checked
        )
        parts = [
            f"共 {len(self._rows)} 个可调参数（设备档案标记为可调），"
            f"其中 {usable} 个配置了最大单步、可用于调束；已勾选 {checked} 个。"
        ]
        excluded = self._catalog.get("excluded") or {}
        if excluded:
            summary = "；".join(
                f"{reason} {len(signals)} 路" for reason, signals in excluded.items()
            )
            parts.append(f"未列入：{summary}")
        target = self.target.currentData()
        upstream = list((self._catalog.get("upstream") or {}).get(str(target), []))
        if upstream:
            parts.append(f"当前目标的上游参数 {len(upstream)} 个已自动勾选（束线拓扑算出的）")
        linked = self._catalog.get("linked_sets") or []
        if linked:
            parts.append(
                f"联动组 {len(linked)} 个（如磁铁同步组）：本版按单路独立下发，"
                "成组调束需要服务端原子批量动作，先别把它们当同一个优化维度"
            )
        if self._used_fallback:
            parts.append(
                "注意：执行服务没提供调束标记（版本较旧），已按命名规则回退过滤，"
                "建议重启执行服务后再调束"
            )
        self.variable_hint.setProperty("state", "")
        self.variable_hint.setText(" ".join(parts))
        self.variable_hint.style().unpolish(self.variable_hint)
        self.variable_hint.style().polish(self.variable_hint)

    def _refresh_readbacks(self) -> None:
        if self._read_in_flight or not self._rows:
            return
        signals = [r["entry"]["signal"] for r in self._rows]
        target = self.target.currentData()
        if target:
            signals.append(target)
        self._read_in_flight = True
        instrument_api.request_read(
            signals=signals,
            on_completed=self._on_readbacks,
        )

    def _on_readbacks(self, payload: dict) -> None:
        self._read_in_flight = False
        if not payload.get("ok"):
            return
        readings = instrument_api.readings_by_signal(payload.get("payload"))
        for row in self._rows:
            reading = readings.get(row["entry"]["signal"])
            if reading and reading.get("connected") and reading.get("value") is not None:
                row["readback"].setText(
                    f"{float(reading['value']):.3f} {row['entry'].get('unit', '')}"
                )
            else:
                row["readback"].setText("未连接")

    # ------------------------------------------------------------------
    # 配置保存与恢复（改造报告 §8.9 P2.3）
    # ------------------------------------------------------------------
    def tuning_config_payload(self) -> dict:
        """把当前界面上的配置收成一份可落盘的字典。

        变量范围按**界面上的值**记录（不是映射边界）：操作员收窄范围正是要保存的东西。
        没勾选的变量也一起存（``enabled=false``），下次打开时它们还在表里、只是没被选中。
        """
        return {
            "target_signal": self.target.currentData() or "",
            "variables": [
                {
                    "signal": row["entry"]["signal"],
                    "enabled": row["check"].checkState() == Qt.CheckState.Checked,
                    "low": float(row["low"].value()),
                    "high": float(row["high"].value()),
                    "start": float(row["start"].value()),
                }
                for row in self._rows
            ],
            "strategy": self.strategy.currentData() or "",
            "engine": self.engine_combo.currentData() or "cmaes",
            "mode": self.mode_combo.currentData() or "confirm",
            "patience": self.patience_spin.value(),
            "calls_per_variable": self.calls_spin.value(),
            "joint_frac": self.joint_frac_spin.value(),
            "hold_s": self.hold_spin.value(),
            "max_iterations": self.iterations_spin.value(),
            "settle_timeout_s": self.settle_spin.value(),
            "samples_per_point": self.samples_spin.value(),
            "loss_absolute": (
                self.loss_absolute_spin.value() if self.loss_absolute_check.isChecked() else None
            ),
            "loss_relative": self.loss_relative_spin.value(),
            "loss_strikes": self.loss_strikes_spin.value(),
            "auto_recover": self.auto_recover_check.isChecked(),
            "loss_protection": self.loss_protection_check.isChecked(),
            "reset_before_start": self.reset_before_start_check.isChecked(),
            "stall_fraction": self.stall_fraction_form_spin.value() / 100.0,
            "seed": self.seed_spin.value(),
            "random_seed": self.random_seed_check.isChecked(),
            "n_startup_trials": self.startup_spin.value(),
            "finalize_action": self.finalize_pref.currentData() or "",
        }

    def save_tuning_config(self, announce: bool = True) -> None:
        """存配置。写不进去（目录不可写）只提示，不影响调束本身。"""
        try:
            path = tuning_config.save(self.tuning_config_payload())
        except OSError as exc:
            self._set_config_note(f"调束配置保存失败：{exc}", "error")
            return
        if announce:
            summary = tuning_config.describe(self.tuning_config_payload())
            self._set_config_note(f"已保存调束配置（{summary}）→ {path}", "good")

    def load_tuning_config(self) -> bool:
        """从本机配置恢复界面设置；返回**是否真的载入了**（没有文件时 False）。"""
        config, note = tuning_config.load(
            strategies=tuple(key for key, _label in STRATEGY_CHOICES),
            actions=tuple(FINALIZE_LABELS),
        )
        if config is None:
            self._set_config_note(
                note or "还没有保存过调束配置，改好参数后点「保存调束配置」。", "idle"
            )
            return False
        missing = self._apply_tuning_config(config)
        parts = [f"已载入调束配置（{tuning_config.describe(config)}）"]
        if missing:
            parts.append("以下信号当前映射里没有，已跳过：" + "、".join(missing))
        if note:
            parts.append(note)
        self._set_config_note("；".join(parts), "warn" if (missing or note) else "good")
        return True

    def _apply_tuning_config(self, config: dict) -> list[str]:
        """把配置写回控件；返回"配置里有、当前映射里没有"的信号名。

        **顺序有讲究**：先设目标，再设变量。设目标会触发 ``_on_target_changed`` →
        按束线拓扑自动勾选上游参数；先设变量就会被那次联动覆盖掉。
        """
        target = str(config.get("target_signal") or "")
        for index in range(self.target.count()):
            if self.target.itemData(index) == target:
                self.target.setCurrentIndex(index)
                break

        by_signal = {row["entry"]["signal"]: row for row in self._rows}
        for item in config.get("variables") or []:
            row = by_signal.get(str(item.get("signal") or ""))
            if row is None:
                continue
            row["check"].setCheckState(
                Qt.CheckState.Checked
                if item.get("enabled", True)
                else Qt.CheckState.Unchecked
            )
            for key, widget in (("low", row["low"]), ("high", row["high"]),
                                ("start", row["start"])):
                if item.get(key) is not None:
                    # QDoubleSpinBox 会按自己的范围夹：配置越界时这里就是第一道提示，
                    # 真正能不能写仍由执行层在启动时判定
                    widget.setValue(float(item[key]))

        strategy = config.get("strategy")
        if strategy:
            index = self.strategy.findData(strategy)
            if index >= 0:
                self.strategy.setCurrentIndex(index)

        engine = config.get("engine")
        if engine:
            index = self.engine_combo.findData(engine)
            if index >= 0:
                self.engine_combo.setCurrentIndex(index)
        mode = config.get("mode")
        if mode:
            index = self.mode_combo.findData(mode)
            if index >= 0:
                self.mode_combo.setCurrentIndex(index)
        for key, widget in (
            ("calls_per_variable", self.calls_spin),
            ("max_iterations", self.iterations_spin),
            ("samples_per_point", self.samples_spin),
            ("loss_strikes", self.loss_strikes_spin),
            ("seed", self.seed_spin),
            ("n_startup_trials", self.startup_spin),
            ("patience", self.patience_spin),
        ):
            if config.get(key) is not None:
                widget.setValue(int(round(float(config[key]))))
        for key, widget in (
            ("joint_frac", self.joint_frac_spin),
            ("hold_s", self.hold_spin),
            ("settle_timeout_s", self.settle_spin),
            ("loss_relative", self.loss_relative_spin),
        ):
            if config.get(key) is not None:
                widget.setValue(float(config[key]))

        absolute = config.get("loss_absolute")
        self.loss_absolute_check.setChecked(absolute is not None)
        if absolute is not None:
            self.loss_absolute_spin.setValue(float(absolute))
        if config.get("auto_recover") is not None:
            self.auto_recover_check.setChecked(bool(config["auto_recover"]))
        if config.get("loss_protection") is not None:
            self.loss_protection_check.setChecked(bool(config["loss_protection"]))
        if config.get("reset_before_start") is not None:
            self.reset_before_start_check.setChecked(bool(config["reset_before_start"]))
        if config.get("random_seed") is not None:
            self.random_seed_check.setChecked(bool(config["random_seed"]))
        stall = config.get("stall_fraction")
        if stall is not None:
            # 存的是小数（0.05），表单是百分数（5）
            self.stall_fraction_form_spin.setValue(float(stall) * 100.0)

        action = str(config.get("finalize_action") or "")
        index = self.finalize_pref.findData(action)
        if index >= 0:
            self.finalize_pref.setCurrentIndex(index)

        self._update_variable_hint()
        return tuning_config.missing_signals(config, set(by_signal))

    def _set_config_note(self, text: str, state: str) -> None:
        self.config_note.setText(text)
        self.config_note.setProperty("state", state)
        self.config_note.style().unpolish(self.config_note)
        self.config_note.style().polish(self.config_note)

    def _maybe_apply_saved_config(self) -> None:
        """映射与目录都到齐、变量行建好之后，把上次的配置载回来一次。

        只有**真的载入了**才置位：文件不存在或读不动时，按拓扑自动勾选上游参数
        那套便利仍然保留（否则"从没保存过配置"的现场会连自动勾选都没了）。
        置位之后不再载入——后台再刷一次映射不该把操作员正在编辑的东西冲掉。
        """
        if self._config_applied or not self._rows:
            return
        self._config_applied = self.load_tuning_config()

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    def _selected_variables(self) -> list[dict]:
        selected = []
        for row in self._rows:
            if row["check"].checkState() != Qt.CheckState.Checked:
                continue
            entry = row["entry"]
            selected.append(
                {
                    "signal": entry["signal"],
                    "label": entry.get("label", entry["signal"]),
                    "low": float(row["low"].value()),
                    "high": float(row["high"].value()),
                    "start": float(row["start"].value()),
                    "enabled": True,
                }
            )
        return selected

    def start_tuning(self) -> None:
        if not self._writes_allowed():
            # 不弹模态框：只读是部署状态而不是操作失误，写在页面上即可（也便于自检）
            self.read_only_note.setVisible(True)
            return
        variables = self._selected_variables()
        if not variables:
            self._complain("请至少勾选一个参与调束的参数。")
            return
        by_signal = {r["entry"]["signal"]: r["entry"] for r in self._rows}
        missing = [
            v["label"] for v in variables if not by_signal[v["signal"]].get("max_step")
        ]
        if missing:
            self._complain(
                "以下参数未配置最大单步，不能用于调束（一次大跳变可能毁掉束流）："
                + "、".join(missing)
            )
            return
        target = self.target.currentData()
        if not target:
            self._complain("请选择优化目标。")
            return

        if self.mode_combo.currentData() == "auto":
            protection = self.loss_absolute_check.isChecked() or (
                self.loss_relative_spin.value() > 0
            )
            if not protection:
                self._complain(
                    "全自动模式必须启用束流丢失保护：勾选「目标低于」设一个绝对阈值，"
                    "或把「相对损失阈值」设成大于 0——否则无人确认的自动写入没有兜底。"
                )
                return

        self._iterations = []
        self._last_completed = 0
        self._iterations_in_flight = False
        self._algorithm = ""
        self._seed = None
        self._analysis = None  # 结束后从 /analysis 拉的 Optuna 分析结果
        self._snapshot = {}
        self._baseline = None
        self._recovery = None
        self.recent_table.setRowCount(0)
        self.changes_table.setRowCount(0)
        self.analysis_trial_table.setRowCount(0)
        self._refresh_result_analysis_choices()
        self._render_result_analysis()
        self._update_result_summary()
        # 开跑前的建议（原 demo 的 R5）：变量太多时"每变量的轮次不够看出趋势"这件事
        # 必须在开跑前说，跑完再说就只是事后诸葛。
        self._startup_advice = tuning_analysis.startup_advice(
            [v["signal"] for v in variables]
        )
        self.tuning_plot.set_data([], [])
        self._refresh_plot_choices()
        self._refresh_advice()
        self._fill_log()
        self.start_button.setEnabled(False)
        self._status_in_flight = True
        # 开跑前把当前配置存一份（原 demo 同样在启动前保存）：跑完之后的"上次配置"
        # 才是这一轮真正用的那一套，而不是中途编辑过的界面状态。写不进去不拦启动。
        self.save_tuning_config(announce=False)
        thread = instrument_api.request_tuning_start(
            {
                "target_signal": target,
                "variables": variables,
                "mode": self.mode_combo.currentData() or "confirm",
                "engine": self.engine_combo.currentData() or "cmaes",
                "patience": self.patience_spin.value(),
                "max_iterations": self.iterations_spin.value(),
                "settle_timeout_s": self.settle_spin.value(),
                "samples_per_point": self.samples_spin.value(),
                # 两阶段策略（改造报告 §5.2）
                "strategy": self.strategy.currentData() or "joint",
                "calls_per_variable": self.calls_spin.value(),
                "joint_frac": self.joint_frac_spin.value() / 100.0,
                "hold_s": self.hold_spin.value(),
                "seed": self._effective_seed(),
                "n_startup_trials": self.startup_spin.value(),
                # 束流丢失保护：阈值随任务一起下发，由执行服务在每轮结束后判定
                "loss_absolute": (
                    self.loss_absolute_spin.value()
                    if self.loss_absolute_check.isChecked()
                    else None
                ),
                "loss_relative": self.loss_relative_spin.value() / 100.0,
                "loss_strikes": self.loss_strikes_spin.value(),
                "loss_protection": self.loss_protection_check.isChecked(),
                "stall_fraction": self.stall_fraction_form_spin.value() / 100.0,
                "auto_recover": self.auto_recover_check.isChecked(),
            }
        )
        thread.completed.connect(self._on_started)

    def _effective_seed(self) -> int:
        """本次实际使用的种子：勾选随机则现场生成，否则用固定值。"""
        if self.random_seed_check.isChecked():
            import random
            return random.randint(0, 2**31 - 1)
        return int(self.seed_spin.value())

    def _complain(self, message: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("无法开始调束")
        box.setText(message)
        box.exec()

    def _on_started(self, payload: dict) -> None:
        self._status_in_flight = False
        if not payload.get("ok"):
            self.start_button.setEnabled(self._writes_allowed())
            self._complain(f"启动失败：{payload.get('message', '')}")
            return
        status = payload.get("payload") or {}
        self._run_id = status.get("run_id")
        self._algorithm = str(
            status.get("algorithm_version") or status.get("algorithm") or ""
        )
        self._seed = status.get("seed")
        self.tabs.setTabEnabled(1, True)
        self.tabs.setCurrentIndex(1)
        self.tuning_stop_button.setEnabled(True)
        self._apply_status(status)
        self._timer.start()

    # ------------------------------------------------------------------
    # 候选对比表
    # ------------------------------------------------------------------
    def _fill_proposal_table(self, pending: dict) -> None:
        """把本轮候选填成「当前回读 | 建议值 | 变化」对比表。"""
        suggestions = pending.get("values") or {}
        current = self._current_readbacks()
        self.proposal_table.setRowCount(len(suggestions))
        for row, (signal, value) in enumerate(suggestions.items()):
            self.proposal_table.setItem(row, 0, QTableWidgetItem(self._row_label(signal)))
            cur = current.get(signal)
            self.proposal_table.setItem(
                row, 1, QTableWidgetItem("--" if cur is None else f"{cur:.3f}")
            )
            self.proposal_table.setItem(row, 2, QTableWidgetItem(f"{float(value):.3f}"))
            delta = "--" if cur is None else f"{float(value) - cur:+.3f}"
            self.proposal_table.setItem(row, 3, QTableWidgetItem(delta))
        self.proposal_table.resizeColumnsToContents()

    def _current_readbacks(self) -> dict[str, float]:
        """从变量行的「当前回读」单元格取最新值。"""
        out: dict[str, float] = {}
        for row in self._rows:
            signal = row["entry"]["signal"]
            text = row["readback"].text()
            try:
                out[signal] = float(text.split()[0])
            except (ValueError, IndexError):
                continue
        return out

    # ------------------------------------------------------------------
    # 确认 / 停止
    # ------------------------------------------------------------------
    def _approve(self) -> None:
        if not self._run_id:
            return
        if not self._writes_allowed():
            self.proposal_label.setText("全局只读模式：执行服务禁用了所有写入，无法下发候选。")
            return
        self.approve_button.setEnabled(False)
        self.proposal_label.setText("正在写入设备并等待读回稳定…")
        thread = instrument_api.request_tuning_approve(self._run_id)
        thread.completed.connect(self._on_approved)

    def _on_approved(self, payload: dict) -> None:
        if not payload.get("ok"):
            self.proposal_label.setText(f"执行失败：{payload.get('message', '')}")
            self.approve_button.setEnabled(self._writes_allowed())
            return
        self._apply_status(payload.get("payload") or {})
        self._fetch_iterations()

    def pause_tuning(self) -> None:
        if not self._run_id:
            return
        self.tuning_pause_button.setEnabled(False)
        thread = instrument_api.request_tuning_pause(self._run_id)
        thread.completed.connect(self._on_flow_result)

    def resume_tuning(self) -> None:
        if not self._run_id:
            return
        self.tuning_resume_button.setEnabled(False)
        thread = instrument_api.request_tuning_resume(self._run_id)
        thread.completed.connect(self._on_flow_result)

    def _on_flow_result(self, payload: dict) -> None:
        """暂停 / 继续的返回：直接按新状态刷界面（失败时交给下一轮轮询纠正）。"""
        if not payload.get("ok"):
            return
        self._apply_status(payload.get("payload") or {})
        self._fetch_iterations()

    def stop_tuning(self) -> None:
        if not self._run_id:
            return
        self.tuning_stop_button.setEnabled(False)
        instrument_api.request_tuning_stop(self._run_id)

    def _acknowledge(self) -> None:
        if not self._run_id:
            return
        thread = instrument_api.request_tuning_acknowledge(
            self._run_id, note="操作员在界面确认"
        )
        thread.completed.connect(lambda _p: self.acknowledge_button.setVisible(False))

    # ------------------------------------------------------------------
    # 结束后的设备处置
    # ------------------------------------------------------------------
    def _ask_finalize_confirm(self, label: str) -> bool:
        """二次确认。测试与自检会替换掉这个方法（offscreen 下模态框没人点）。"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("确认处置设备")
        box.setText(f"即将{label}：会写设备并等待回读到位。确认执行？")
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def finalize_tuning(self, action: str) -> None:
        """应用最优 / 恢复启动前 / 回安全值：确认后由执行服务批量执行。"""
        if not self._run_id:
            return
        label = FINALIZE_LABELS.get(action, action)
        if not self._writes_allowed():
            self._set_finalize_status(
                "全局只读模式：执行服务禁用了所有写入，设备处置不可用。", "warn"
            )
            return
        if not self._ask_finalize_confirm(label):
            self._set_finalize_status(f"{label}已取消。", "idle")
            return
        for button in self.finalize_buttons.values():
            button.setEnabled(False)
        self._set_finalize_status(f"{label}中（等待各路回读到位）…", "warn")
        thread = instrument_api.request_tuning_finalize(self._run_id, action)
        thread.completed.connect(
            lambda payload, a=action, text=label: self._on_finalized(a, text, payload)
        )

    def _on_finalized(self, action: str, label: str, payload: dict) -> None:
        for button in self.finalize_buttons.values():
            button.setEnabled(self._writes_allowed())
        if not payload.get("ok"):
            # 服务端拒绝（未确认/任务未结束/设备组被占）：原样转述，绝不报成功
            self._set_finalize_status(
                f"{label}未执行：{payload.get('message') or '服务未说明原因'}", "error"
            )
            return
        result = payload.get("payload") or {}
        applied = result.get("applied") or {}
        values = "、".join(f"{k}={v:.3f}" for k, v in sorted(applied.items()))
        if not result.get("ok"):
            self._set_finalize_status(
                f"{label}未完成：{result.get('detail') or result.get('message') or '原因未返回'}"
                + (f"（已到位 {values}）" if values else ""),
                "error",
            )
            return
        self._set_finalize_status(
            f"{result.get('message') or label + '完成'}"
            + (f"：{values}" if values else ""),
            "good",
        )

    def _render_finalize(self, result: dict | None) -> None:
        """把服务端记住的处置结果显示出来（关掉界面再打开也能看到做过什么）。"""
        if not result:
            return
        label = FINALIZE_LABELS.get(str(result.get("action")), result.get("action"))
        applied = result.get("applied") or {}
        values = "、".join(f"{k}={v:.3f}" for k, v in sorted(applied.items()))
        if result.get("ok"):
            self._set_finalize_status(
                f"{label}已完成" + (f"：{values}" if values else ""), "good"
            )
        else:
            self._set_finalize_status(
                f"{label}未完成：{result.get('detail') or '原因未返回'}", "error"
            )

    def _set_finalize_status(self, text: str, state: str) -> None:
        self.finalize_label.setText(text)
        self.finalize_label.setProperty("state", state)
        self.finalize_label.style().unpolish(self.finalize_label)
        self.finalize_label.style().polish(self.finalize_label)

    def is_operation_active(self) -> bool:
        return self._run_id is not None and self._state not in TERMINAL_STATES

    def safe_stop(self) -> None:
        if self.is_operation_active():
            self.stop_tuning()

    # ------------------------------------------------------------------
    # 轮询
    # ------------------------------------------------------------------
    def _poll(self) -> None:
        if not self._run_id:
            self._timer.stop()
            return
        if self._status_in_flight:
            return
        self._status_in_flight = True
        thread = instrument_api.request_tuning_status(self._run_id)
        thread.completed.connect(self._on_status)

    def _on_status(self, payload: dict) -> None:
        self._status_in_flight = False
        if not payload.get("ok"):
            return
        self._apply_status(payload.get("payload") or {})
        # 运行中也必须增量拉轮次；旧逻辑只在结束/人工确认后拉，
        # 因此“本轮测量 / 历史最优”会看起来到结束才刷新。
        self._fetch_iterations()

    def _apply_status(self, status: dict) -> None:
        self._state = str(status.get("state", "idle"))
        # 算法与种子：导出日志要带上（结果能不能复现全靠这两个）
        self._algorithm = str(
            status.get("algorithm_version") or status.get("algorithm") or self._algorithm
        )
        if status.get("seed") is not None:
            self._seed = status.get("seed")
        # 启动前快照 / 基线 / 回退记录：结果页与监控条都要用，统一在这里收下
        self._snapshot = dict(status.get("snapshot") or {})
        baseline = status.get("baseline_objective")
        self._baseline = None if baseline is None else float(baseline)
        self._recovery = status.get("recovery")
        self._snapshot_note = str(status.get("snapshot_note") or "")
        completed = int(status.get("completed_iterations") or 0)
        remaining = max(0, int(status.get("max_iterations") or 0) - completed)
        remaining_s = remaining * (
            self.settle_spin.value() + self.hold_spin.value()
            + max(0, self.samples_spin.value() - 1) * 0.05
        )
        self.eta_card.value_label.setText(
            "已结束" if self._state in TERMINAL_STATES
            else self._format_duration(remaining_s).removeprefix("约 ")
        )
        self._last_completed = max(self._last_completed, completed)
        self.iteration_label.setText(
            f"已完成 {completed} / {status.get('max_iterations', 0)} 轮{self._stage_suffix(status)}"
        )
        text = STATE_TEXT.get(self._state, self._state)
        message = status.get("message") or ""
        if message:
            text = f"{text} · {message}"
        self.tuning_state.setText(text)

        self._pending = status.get("pending")
        self._mode = str(status.get("mode") or self._mode)
        self._render_recovery()
        self._render_snapshot()
        auto = self._mode == "auto"
        self.proposal_panel.setVisible(not auto)
        if self._pending:
            active = list(self._pending.get("active_signals") or [])
            checked = {
                row["entry"]["signal"]
                for row in self._rows
                if row["check"].checkState() == Qt.CheckState.Checked
            }
            # 只有"本轮动的"比"操作员勾选的"少时才点名——两阶段策略下阶段 1
            # 只调一个变量，这一点必须在候选文案里说清，否则会被当成"全都动了"。
            if active and set(active) != checked:
                scope = f"本轮调整：{'、'.join(active)}"
            else:
                scope = "本轮调整：全部选中参数"
            prefix = "自动执行中" if auto else "待确认候选"
            self.proposal_label.setText(
                f"第 {int(self._pending['iteration']) + 1} 轮{prefix}（{scope}；"
                f"预测 {float(self._pending['predicted']):.3f}"
                f" ± {float(self._pending['std']):.3f}）"
            )
            self._fill_proposal_table(self._pending)
            self.proposal_table.setVisible(True)
            # auto 模式的候选由服务端直接执行，按钮不给点（仅供查看与导出）
            self.approve_button.setEnabled(
                False if auto else self._writes_allowed()
            )
        else:
            if self._state == "paused":
                self.proposal_label.setText("已暂停：参数冻结，可「继续」或「停止」。")
            else:
                self.proposal_label.setText(
                    "本轮已执行，正在生成下一轮候选…"
                    if self._state not in TERMINAL_STATES
                    else "无待确认候选。"
                )
            self.approve_button.setEnabled(False)

        # 暂停 / 继续按钮：只在不写设备的状态可用（服务端同样会拒绝）
        self.tuning_pause_button.setEnabled(
            self._state in ("running", "awaiting_confirmation")
        )
        self.tuning_resume_button.setEnabled(self._state == "paused")
        self.tuning_resume_button.setVisible(self._state == "paused")

        if self._state == "recovery_required":
            self.acknowledge_button.setVisible(True)

        if self._state in TERMINAL_STATES:
            self._timer.stop()
            self.start_button.setEnabled(self._writes_allowed())
            self.tuning_stop_button.setEnabled(False)
            self.tuning_pause_button.setEnabled(False)
            self.tuning_resume_button.setEnabled(False)
            self.tuning_resume_button.setVisible(False)
            self.approve_button.setEnabled(False)
            self.tabs.setTabEnabled(2, True)
            # 结束后才能处置设备；恢复待确认状态下设备实际状态未知，必须先确认
            for button in self.finalize_buttons.values():
                button.setEnabled(
                    self._state != "recovery_required" and self._writes_allowed()
                )
            self._render_finalize(status.get("finalize"))
            # 任何终态都尝试拉分析（failed/aborted 也可能已跑了几轮，有数据可看）
            self._load_analysis()
            if self._state == "completed":
                self.result_notice.setText("调束已完成")
                self.tabs.setCurrentIndex(2)
                # 保存的"完成后处置"只是**建议**：写在处置面板上，按钮仍要人点
                preferred = self.finalize_pref.currentData()
                if preferred and self._writes_allowed():
                    self._set_finalize_status(
                        f"按保存的调束配置建议先考虑：{FINALIZE_LABELS.get(preferred, preferred)}"
                        "（仍需点按钮并二次确认）",
                        "idle",
                    )
            elif self._state == "aborted":
                self.result_notice.setText("调束已停止")
                self.tabs.setCurrentIndex(2)
            else:
                self.result_notice.setText(text)
                if self._state == "failed":
                    self.tabs.setCurrentIndex(2)
            self._fetch_iterations()
        self._update_result_summary()

    def _stage_suffix(self, status: dict) -> str:
        """把"现在在哪个阶段、正在调哪个参数"写进轮次标签。

        两阶段策略下"调了 3 轮"是不够的信息：操作员要知道这 3 轮是在逐参数
        还是在联合微调、以及正在动哪个参数。
        """
        stage = str(status.get("stage") or "")
        if stage not in STAGE_TEXT:
            return ""
        if stage == "sequential":
            index = int(status.get("stage_index") or 0)
            total = int(status.get("stage_total") or 0)
            variable = status.get("stage_variable") or ""
            return f" · 阶段：逐参数 {index}/{total}（{variable}）"
        return " · 阶段：联合微调"

    def _render_recovery(self) -> None:
        """展示束流丢失保护的执行结果：原因 + 逐路实际回读。

        这一段必须自己站出来讲清楚——回退是保护动作，操作员要立刻知道
        "为什么退、退到哪、有没有全部退到位"，而不是只看到一句状态文案。
        """
        recovery = self._recovery
        if not recovery:
            self.recovery_label.setVisible(False)
            return
        restored = recovery.get("restored") or {}
        parts = [
            f"⚠ 束流丢失保护：{recovery.get('reason', '')}",
            f"（{recovery.get('at', '')}）",
            "已退回启动前参数" if recovery.get("ok") else "**回退未完成**",
        ]
        if restored:
            parts.append(
                "逐路实际回读：" + "、".join(f"{k}={v:.3f}" for k, v in sorted(restored.items()))
            )
        if recovery.get("detail"):
            parts.append(str(recovery["detail"]))
        self.recovery_label.setText(" ".join(parts))
        self.recovery_label.setProperty("state", "good" if recovery.get("ok") else "error")
        self.recovery_label.style().unpolish(self.recovery_label)
        self.recovery_label.style().polish(self.recovery_label)
        self.recovery_label.setVisible(True)

    def _render_snapshot(self) -> None:
        """展示启动前快照与目标基线——这是"相对提升了多少"的参照点。"""
        if not self._snapshot and not self._snapshot_note:
            self.snapshot_label.setVisible(False)
            return
        parts: list[str] = []
        if self._snapshot:
            values = "、".join(f"{k}={v:.3f}" for k, v in sorted(self._snapshot.items()))
            parts.append(f"启动前快照（{len(self._snapshot)} 路）：{values}")
        if self._baseline is not None:
            parts.append(f"目标基线 {self._baseline:.3f}")
        if self._snapshot_note:
            parts.append(self._snapshot_note)
        self.snapshot_label.setText(" ".join(parts))
        self.snapshot_label.setVisible(True)

    def _fetch_iterations(self) -> None:
        """增量拉取：只有服务端 completed_iterations 比本地多才请求，防并发。"""
        if not self._run_id:
            return
        if getattr(self, "_iterations_in_flight", False):
            return
        completed = int(getattr(self, "_last_completed", 0))
        local = len(self._iterations)
        if local >= completed:
            return
        self._iterations_in_flight = True
        thread = instrument_api.request_tuning_iterations(self._run_id)
        thread.completed.connect(self._on_iterations)

    def _on_iterations(self, payload: dict) -> None:
        self._iterations_in_flight = False
        if not payload.get("ok"):
            return
        self._iterations = list(
            (payload.get("payload") or {}).get("iterations") or []
        )
        self._last_completed = len(self._iterations)
        self._redraw()
        self._fill_changes()
        self._refresh_plot_choices()
        self._refresh_advice()
        self._fill_log()
        self._fill_recent_iterations()
        self._update_result_summary()
        self._refresh_result_analysis_choices()
        self._render_result_analysis()
        self._fill_analysis_trials()

    def _fill_recent_iterations(self) -> None:
        """用最近 5 轮表格补足曲线的精确读数。"""
        rows = self._iterations[-5:]
        self.recent_table.setRowCount(len(rows))
        best: float | None = None
        best_flags: dict[int, bool] = {}
        for record in self._iterations:
            value = record.get("objective")
            if value is None:
                continue
            number = float(value)
            improved = best is None or number > best
            best = number if improved else best
            best_flags[id(record)] = improved
        for row, record in enumerate(rows):
            value = record.get("objective")
            quality = str(record.get("quality") or "--")
            cells = (
                str(int(record.get("iteration", row)) + 1),
                tuning_analysis.stage_of(record),
                "--" if value is None else f"{float(value):.3f}",
                quality,
                "刷新最优" if best_flags.get(id(record)) else "已完成",
            )
            for column, text in enumerate(cells):
                self.recent_table.setItem(row, column, QTableWidgetItem(text))
        self.recent_table.resizeColumnsToContents()

    def _update_result_summary(self) -> None:
        """结果页摘要始终从基线和已拉取轮次派生。"""
        usable = [it for it in self._iterations if it.get("objective") is not None]
        baseline = self._baseline
        best = max((float(it["objective"]) for it in usable), default=None)
        self.result_baseline_card.value_label.setText(
            "--" if baseline is None else f"{baseline:.3f}"
        )
        self.result_best_card.value_label.setText(
            "--" if best is None else f"{best:.3f}"
        )
        if baseline is None or best is None or baseline == 0:
            gain = "--"
        else:
            gain = f"{(best - baseline) / abs(baseline) * 100:+.1f}%"
        self.result_gain_card.value_label.setText(gain)
        self.result_quality_card.value_label.setText(
            f"{len(usable)} / {len(self._iterations)}"
        )

    # ------------------------------------------------------------------
    # 过程视图：收敛曲线 / 单变量响应曲线 / 过程建议 / 日志
    # ------------------------------------------------------------------
    def _refresh_plot_choices(self) -> None:
        """过程页只保留收敛与变量响应，事后分析统一放在结果页。"""
        variables = tuning_analysis.curve_variables(self._iterations)
        current = self.plot_choice.currentData() or ""
        # 清空旧按钮
        while self.plot_button_row.count():
            item = self.plot_button_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.plot_choice.blockSignals(True)
        self.plot_choice.clear()
        from PySide6.QtWidgets import QPushButton
        def _add_btn(label: str, data: str) -> None:
            self.plot_choice.addItem(label, data)
            btn = QPushButton(label, checkable=True, objectName="plotToolButton")
            btn.setChecked(data == current)
            btn.clicked.connect(lambda _checked=False, d=data: self._select_view(d))
            self.plot_button_row.addWidget(btn)
        _add_btn("收敛", "")
        for signal in variables:
            _add_btn(self._row_label(signal), signal)
        index = self.plot_choice.findData(current)
        self.plot_choice.setCurrentIndex(max(index, 0))
        self.plot_choice.blockSignals(False)

    def _select_view(self, data: str) -> None:
        self.plot_choice.setCurrentIndex(self.plot_choice.findData(data))
        self._render_plot()

    def _row_label(self, signal: str) -> str:
        for row in self._rows:
            if row["entry"]["signal"] == signal:
                return f"{row['entry'].get('label', signal)}（{row['entry'].get('unit', '')}）"
        return signal

    def _redraw(self) -> None:
        """指标卡与曲线一起刷新（两者都只依赖轮次记录）。"""
        usable = [it for it in self._iterations if it.get("objective") is not None]
        if not usable:
            self.tuning_plot.set_data([], [])
            self._set_response_note("")
            return
        ys = [float(it["objective"]) for it in usable]
        self.current_card.value_label.setText(f"{ys[-1]:.3f}")
        best = max(ys)
        self.best_card.value_label.setText(f"{best:.3f}")
        # 相对提升的基准优先用**启动前基线**：拿第一轮当基准会把调束自己的
        # 第一步算进"提升"里，看起来永远比实际好看一点。
        reference = self._baseline if self._baseline is not None else ys[0]
        self.gain_card.value_label.setText(f"{best - reference:+.3f}")
        self.result_detail.setText(
            f"共 {len(self._iterations)} 轮，其中 {len(usable)} 轮有有效目标测量"
        )
        self._render_plot(best)

    def _render_plot(self, best: float | None = None) -> None:
        """按下拉选择画收敛曲线或某个变量的响应曲线。"""
        best = best if best is not None else self._best_objective()
        signal = self.plot_choice.currentData() or ""
        if not signal:
            usable = [it for it in self._iterations if it.get("objective") is not None]
            if not usable:
                self.tuning_plot.set_data([], [])
                self._set_response_note("")
                return
            xs = [float(it["iteration"]) + 1 for it in usable]
            ys = [float(it["objective"]) for it in usable]
            self.tuning_plot.view.setLabel("bottom", "轮次")
            self.tuning_plot.view.setLabel("left", "目标量")
            self.tuning_plot.set_data(xs, ys)
            if best is not None:
                self._show_best_line(best)
            self._set_response_note("")
            return

        xs, ys = tuning_analysis.response_curve(self._iterations, signal)
        self.tuning_plot.view.setLabel("bottom", self._row_label(signal))
        self.tuning_plot.view.setLabel("left", "目标量")
        self.tuning_plot.set_data(xs, ys)
        self._hide_best_line()
        peak = tuning_analysis.best_point(xs, ys)
        if peak is not None:
            self.tuning_plot.mark_best(
                peak[0], peak[1], f"最优 {peak[1]:.3f} @ {peak[0]:g}"
            )
        span = self._configured_range(signal)
        detail = f"，设定范围 [{span[0]:g}, {span[1]:g}]" if span else ""
        self._set_response_note(
            f"响应曲线：已采样 {len(xs)} 点{detail}"
            + (
                f"；该参数最优目标 {peak[1]:.3f}（在 {peak[0]:g} 处）"
                if peak
                else "；还没有可用的采样点"
            )
            + "。曲线只反映已采过的点，不表示范围外的情况。"
        )

    def _load_analysis(self) -> None:
        """结束后从服务端拉 Optuna 分析数据（importance / slice / history）。"""
        if not self._run_id or self._analysis is not None:
            return
        try:
            thread = instrument_api.request_tuning_analysis(self._run_id)
            thread.completed.connect(self._on_analysis_ready)
        except Exception as exc:
            self.analysis_note.setText(f"Optuna 分析加载失败：{exc}")

    def _on_analysis_ready(self, payload: dict) -> None:
        if not payload.get("ok"):
            self.analysis_note.setText(
                f"Optuna 分析暂不可用：{payload.get('message', '未知错误')}"
                "；优化历史仍使用本地轮次展示。"
            )
            self._render_result_analysis()
            return
        self._analysis = payload.get("payload") or {}
        self._refresh_result_analysis_choices()
        self._render_result_analysis()
        self._fill_analysis_trials()

    def _rebuild_analysis_buttons(self, choices: list[tuple[str, str]]) -> None:
        """把分析视图选项做成一排可点按钮（不再用下拉）。"""
        # 清掉旧按钮
        for button in getattr(self, "_analysis_buttons", []):
            self.analysis_button_group.removeButton(button)
            self.analysis_buttons_row.removeWidget(button)
            button.deleteLater()
        self._analysis_buttons = []
        current = getattr(self, "analysis_current_choice", "history")
        picked = False
        for label, data in choices:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.setMaximumHeight(26)
            btn.clicked.connect(lambda _checked=False, d=data: self._set_analysis_choice(d))
            self.analysis_button_group.addButton(btn)
            self.analysis_buttons_row.addWidget(btn)
            self._analysis_buttons.append(btn)
            if data == current:
                btn.setChecked(True)
                picked = True
        if not picked and self._analysis_buttons:
            self._analysis_buttons[0].setChecked(True)
            self.analysis_current_choice = self._analysis_buttons[0].text()

    def _set_analysis_choice(self, data: str) -> None:
        self.analysis_current_choice = data
        self._render_result_analysis()

    def _refresh_result_analysis_choices(self) -> None:
        """根据 Optuna 返回数据生成结果页分析入口（按钮组）。"""
        choices = [("优化历史", "history")]
        importance = (self._analysis or {}).get("importance", {})
        if importance.get("values"):
            choices.append(("参数重要性", "importance"))
        for name in (self._analysis or {}).get("slice", {}):
            choices.append((f"切片 · {self._row_label(name)}", f"slice:{name}"))
        self._rebuild_analysis_buttons(choices)

    def _clear_result_analysis_items(self) -> None:
        for item in self._analysis_items:
            self.analysis_plot.view.removeItem(item)
        self._analysis_items.clear()
        self.analysis_plot.set_series([])
        self.analysis_plot.view.getAxis("bottom").setTicks(None)

    def _render_result_analysis(self, *_args) -> None:
        """在结果页渲染历史、重要性或单参数切片。"""
        import pyqtgraph as pg

        self._clear_result_analysis_items()
        choice = getattr(self, "analysis_current_choice", "history") or "history"
        if choice == "importance":
            values = ((self._analysis or {}).get("importance") or {}).get("values") or {}
            names = list(values)
            scores = [float(values[name]) for name in names]
            xs = list(range(len(names)))
            if scores:
                item = pg.BarGraphItem(x=xs, height=scores, width=0.65, brush="#8BC8EA")
                self.analysis_plot.view.addItem(item)
                self._analysis_items.append(item)
            self.analysis_plot.view.setLabel("bottom", "参数")
            self.analysis_plot.view.setLabel("left", "重要性（FANOVA）")
            self.analysis_plot.view.getAxis("bottom").setTicks(
                [[(i, self._row_label(name)) for i, name in enumerate(names)]]
            )
            self.analysis_note.setText(
                f"已基于有效 Trial 计算 {len(names)} 个参数的重要性。"
            )
            return
        if choice.startswith("slice:"):
            name = choice.split(":", 1)[1]
            points = ((self._analysis or {}).get("slice") or {}).get(name) or []
            xs = [float(point[0]) for point in points]
            ys = [float(point[1]) for point in points]
            if xs:
                item = pg.ScatterPlotItem(
                    x=xs, y=ys, size=9, symbol="o", brush="#3b82f6", pen=None
                )
                self.analysis_plot.view.addItem(item)
                self._analysis_items.append(item)
            self.analysis_plot.view.setLabel("bottom", self._row_label(name))
            self.analysis_plot.view.setLabel("left", "目标量")
            self.analysis_note.setText(f"共 {len(points)} 个切片样本点。")
            return

        points = ((self._analysis or {}).get("history") or {}).get("points") or []
        if points:
            xs = [float(point["trial"]) + 1 for point in points]
            ys = [float(point["value"]) for point in points]
            best = [float(point["best"]) for point in points]
        else:
            usable = [it for it in self._iterations if it.get("objective") is not None]
            xs = [float(it.get("iteration", i)) + 1 for i, it in enumerate(usable)]
            ys = [float(it["objective"]) for it in usable]
            running: float | None = None
            best = []
            for value in ys:
                running = value if running is None else max(running, value)
                best.append(running)
        self.analysis_plot.view.setLabel("bottom", "Trial / 轮次")
        self.analysis_plot.view.setLabel("left", "目标量")
        self.analysis_plot.set_series([(xs, ys)], best=best)
        self.analysis_note.setText(
            f"共 {len(ys)} 个有效点；实线为本轮测量，虚线为历史最优。"
        )

    def _fill_analysis_trials(self) -> None:
        """展示 Optuna Trial 明细，无分析数据时回退到本地轮次。"""
        trials = list((self._analysis or {}).get("trials") or [])
        if trials:
            params = sorted(
                {key for row in trials for key in row}
                - {"number", "state", "value"}
            )
            headers = ["Trial", "状态", "目标值", *[self._row_label(p) for p in params]]
            self.analysis_trial_table.setColumnCount(len(headers))
            self.analysis_trial_table.setHorizontalHeaderLabels(headers)
            self.analysis_trial_table.setRowCount(len(trials))
            for row, trial in enumerate(trials):
                values = [
                    trial.get("number", "--"),
                    trial.get("state", "--"),
                    trial.get("value", "--"),
                    *[trial.get(param, "--") for param in params],
                ]
                for column, value in enumerate(values):
                    text = f"{value:.3f}" if isinstance(value, float) else str(value)
                    self.analysis_trial_table.setItem(row, column, QTableWidgetItem(text))
        else:
            self.analysis_trial_table.setColumnCount(3)
            self.analysis_trial_table.setHorizontalHeaderLabels(["Trial", "状态", "目标值"])
            self.analysis_trial_table.setRowCount(len(self._iterations))
            for row, record in enumerate(self._iterations):
                value = record.get("objective")
                cells = (
                    str(int(record.get("iteration", row)) + 1),
                    str(record.get("quality") or "--"),
                    "--" if value is None else f"{float(value):.3f}",
                )
                for column, text in enumerate(cells):
                    self.analysis_trial_table.setItem(row, column, QTableWidgetItem(text))
        self.analysis_trial_table.resizeColumnsToContents()

    def _configured_range(self, signal: str) -> tuple[float, float] | None:
        """界面上给这个变量设的范围（响应曲线的参照系）。"""
        for row in self._rows:
            if row["entry"]["signal"] == signal:
                return float(row["low"].value()), float(row["high"].value())
        return None

    def _best_objective(self) -> float | None:
        values = [
            float(it["objective"])
            for it in self._iterations
            if it.get("objective") is not None
        ]
        return max(values) if values else None

    def _set_response_note(self, text: str) -> None:
        self.response_note.setText(text)
        self.response_note.setVisible(bool(text))

    def _stall_fraction(self) -> float:
        return float(self.stall_fraction_form_spin.value()) / 100.0

    def _refresh_advice(self) -> None:
        """过程建议：开跑前的（变量数）+ 按轮次统计出来的。"""
        lines = list(self._startup_advice) + tuning_analysis.advice(
            self._iterations, self._variable_ranges(),
            stall_fraction=self._stall_fraction(),
        )
        self.advice_label.setText("；".join(lines))
        self.advice_label.setVisible(bool(lines))

    def _variable_ranges(self) -> dict[str, tuple[float, float]]:
        return {
            row["entry"]["signal"]: (
                float(row["low"].value()),
                float(row["high"].value()),
            )
            for row in self._rows
        }

    def _current_advice(self) -> list[str]:
        lines = list(self._startup_advice) + tuning_analysis.advice(
            self._iterations, self._variable_ranges(),
            stall_fraction=self._stall_fraction(),
        )
        return lines

    def _fill_log(self) -> None:
        """把轮次记录与建议渲染成日志文本（原 demo 的调束日志区）。"""
        lines = tuning_analysis.summary_lines(
            self._iterations,
            target_signal=str(self.target.currentData() or ""),
            best_iteration=self._best_iteration(),
        )
        lines += tuning_analysis.log_lines(
            self._iterations,
            target_signal=str(self.target.currentData() or ""),
            advice_lines=self._current_advice(),
        )
        if not self._iterations:
            lines.append("（还没有轮次记录）")
        self.log_view.setPlainText("\n".join(lines))

    def _best_iteration(self) -> int | None:
        best: tuple[float, int] | None = None
        for record in self._iterations:
            if record.get("objective") is None:
                continue
            value = float(record["objective"])
            if best is None or value > best[0]:
                best = (value, int(record.get("iteration", 0)))
        return None if best is None else best[1]

    def _open_dashboard(self) -> None:
        """在默认浏览器打开本机 optuna-dashboard。"""
        QDesktopServices.openUrl(QUrl(self._dashboard_url))

    def export_tuning_log(self) -> None:
        """导出调束日志（JSONL：一轮一行，便于事后用脚本复盘）。"""
        if not self._iterations:
            self._set_log_note("还没有轮次记录可导出。", "warn")
            return
        run_id = self._run_id or "未记录任务号"
        chosen, _filter = QFileDialog.getSaveFileName(
            self,
            "导出调束日志",
            f"tuning-{run_id}.jsonl",
            "JSON Lines (*.jsonl);;文本 (*.txt)",
        )
        if not chosen:
            self._set_log_note("已取消导出。", "idle")
            return
        path = Path(chosen)
        advice = self._current_advice()
        records: list[dict] = [
            {
                "event": "meta",
                "run_id": self._run_id,
                "target_signal": self.target.currentData() or "",
                "state": self._state,
                "algorithm": self._algorithm,
                "seed": self._seed,
                "baseline_objective": self._baseline,
                "iterations": len(self._iterations),
            }
        ]
        records += [dict(record) for record in self._iterations]
        records += [{"event": "advice", "text": text} for text in advice]
        records.append(
            {
                "event": "end",
                "state": self._state,
                "iterations": len(self._iterations),
                "best_iteration": self._best_iteration(),
            }
        )
        try:
            path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            self._set_log_note(f"导出失败：{exc}", "error")
            return
        self._set_log_note(f"已导出 {len(records)} 行 → {path}", "good")

    def _set_log_note(self, text: str, state: str) -> None:
        self.log_note.setText(text)
        self.log_note.setProperty("state", state)
        self.log_note.style().unpolish(self.log_note)
        self.log_note.style().polish(self.log_note)

    def _hide_best_line(self) -> None:
        """响应曲线模式下不画"历史最佳"横线：那条线是收敛曲线的参照系。"""
        if self._best_line is not None:
            self._best_line.setVisible(False)

    def _show_best_line(self, best: float) -> None:
        """在收敛曲线上画一条虚线表示历史最佳，一眼看出是否还在提升。"""

        import pyqtgraph as pg

        from apps.desktop_client.theme import current_palette

        if not hasattr(self, "_best_line") or self._best_line is None:
            # 注意：InfiniteLine 不接受 dash= 关键字（pyqtgraph 会抛 TypeError）。
            # 虚线由下面 setPen 的 DashLine 样式给，dash 参数是多余的。
            self._best_line = pg.InfiniteLine(angle=0, movable=False)
            self.tuning_plot.view.addItem(self._best_line, ignoreBounds=True)
        self._best_line.setVisible(True)
        self._best_line.setPos(best)
        tokens = current_palette()
        self._best_line.setPen(
            pg.mkPen(tokens["statusGood"], width=1.5, style=Qt.PenStyle.DashLine)
        )

    def _fill_changes(self) -> None:
        """参数变化表只用**实际回读值**，且起点必须是**启动前快照**。

        以前拿的是第一轮执行后的回读：那一轮的参数已经被写过，"变化了多少"
        从算法动过手之后才开始算，等于把调束自己的第一步当成了起点。
        """
        usable = [it for it in self._iterations if it.get("objective") is not None]
        if not usable:
            self.changes_table.setRowCount(0)
            return
        if self._snapshot:
            first = dict(self._snapshot)
        else:
            first = self._iterations[0].get("readback") or {}
        best = max(usable, key=lambda it: float(it["objective"])).get("readback") or {}
        last = self._iterations[-1].get("readback") or {} if self._iterations else {}
        current = self._current_readbacks()
        keys = sorted(set(first) | set(best) | set(last) | set(current))
        self.changes_table.setRowCount(len(keys))
        for row, key in enumerate(keys):
            start = first.get(key)
            end = best.get(key)
            last_v = last.get(key)
            current_v = current.get(key)
            self.changes_table.setItem(row, 0, QTableWidgetItem(self._row_label(key)))
            self.changes_table.setItem(
                row, 1, QTableWidgetItem("--" if start is None else f"{start:.3f}")
            )
            self.changes_table.setItem(
                row, 2, QTableWidgetItem("--" if end is None else f"{end:.3f}")
            )
            self.changes_table.setItem(
                row, 3, QTableWidgetItem("--" if last_v is None else f"{last_v:.3f}")
            )
            self.changes_table.setItem(
                row, 4, QTableWidgetItem("--" if current_v is None else f"{current_v:.3f}")
            )
        self.changes_table.resizeColumnsToContents()
        self.change_note.setText(
            "起点 = 启动前快照（执行服务在占用设备后立即记录的实际回读）"
            if self._snapshot
            else "起点 = 第一轮回读（本次没有拿到启动前快照）"
        )
        self.change_note.setText(
            self.change_note.text() + "；“当前”来自页面最近一次设备回读。"
        )

    # ------------------------------------------------------------------
    # 全局只读部署模式（改造报告 §8.9 P2.2）
    # ------------------------------------------------------------------
    def _writes_allowed(self) -> bool:
        """只读部署下所有会写设备的动作都不可用（执行服务同样会拒绝）。"""
        return not instrument_api.is_read_only()

    def _apply_read_only(self) -> None:
        """压住开始调束 + 确认候选 + 三种设备处置，并说明原因。

        这四类动作都会写设备：调束本身写变量，``/approve`` 写候选值，收尾动作批量
        下发最优/初始/安全值。只读部署下执行服务全部拒绝，界面先把入口锁住。
        """
        if self._writes_allowed():
            return
        self.start_button.setEnabled(False)
        self.read_only_note.setText(
            "全局只读模式：执行服务按部署参数禁用了所有写入，调束与设备处置都不可用。"
        )
        self.read_only_note.setVisible(True)
        self.approve_button.setEnabled(False)
        for button in self.finalize_buttons.values():
            button.setEnabled(False)

    # ------------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._apply_read_only()
        if self.is_operation_active():
            self._poll()
            self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()


PAGE_SPEC = PageSpec(
    key="tuning",
    label="自动调束",
    icon="tuning",
    section="control",
    factory=TuningPage,
)
