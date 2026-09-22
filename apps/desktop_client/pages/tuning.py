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

from PySide6.QtCore import Qt, QTimer, QUrl, QRectF
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
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

from apps.desktop_client import instrument_api, plot_style, tuning_analysis, tuning_config
from apps.desktop_client.components import (
    CompactForm,
    FoldSection,
    RangeBar,
    StatusStrip,
    mark_compact,
)
from apps.desktop_client.pages.common import page_layout, primary_button
from apps.desktop_client.pages.registry import PageSpec
from apps.desktop_client.pv_mapping_api import PvMappingRequestThread
from apps.desktop_client.spectrum_plot import SpectrumPlot
from apps.desktop_client.theme import current_palette
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

# 阶段名 → 界面文案（与执行服务 packages/contracts/tuning.PHASE_LABELS 对齐；
# 客户端自己写一遍中文名，不 import 服务端实现）
PHASE_TEXT = {
    "proposing": "生成候选",
    "prechecking": "执行前校验",
    "writing": "写入设备",
    "settling": "等待稳定",
    "holding": "保持",
    "measuring": "测量目标",
    "recording": "记录本轮",
    "waiting_confirmation": "等待确认",
    "idle_between_iterations": "轮次之间",
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
        # 过程事件流（第二批）：``_events`` 是已收到的事件，``_last_seq`` 是增量游标
        self._events: list[dict] = []
        self._last_seq = 0
        # 断线检测：连续失败次数 + 最后一次成功取数的时间（本地时钟）
        self._link_failures = 0
        self._last_success_monotonic: float | None = None
        # 回读刷新节流（运行中每 2s 一次）与保护状态（服务端回传的结论）
        self._last_readback_at = 0.0
        self._last_seen_latest_seq = 0
        self._protection_reasons: list[str] = []
        self._adopt_attempted = False
        # 老路径增量拉取的重拉守卫（见 _fetch_iterations）
        self._stale_iteration_fetches = 0
        self._iteration_fetch_baseline = 0
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
                "优化器只提候选：确认后由执行层做边界/单步/速率校验并写入设备，"
                "等读回稳定再测目标；建议值、下发值、回读值分开记录。",
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
        """参数配置页：左栏变量与范围标尺，右栏紧凑参数网格，页脚常驻开始按钮。

        布局目标（1320×820 实测）：
        右栏原本是"一行一个字段"的竖排表单（需要 810px，而视口只有 375px，必须滚轮），
        现在改成两列网格 + 三段折叠卡（约 480px），配合页脚状态带，整页不再需要滚动。
        """
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 8, 0, 0)
        outer.setSpacing(8)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # ---------------- 左栏：优化变量 + 范围概况 ----------------
        variables = Panel("优化变量", "勾选参与调束的参数；范围须落在设备边界内")
        head_row = QHBoxLayout()
        head_row.setContentsMargins(0, 0, 0, 0)
        head_row.setSpacing(6)
        self.variable_count_label = QLabel("正在读取设备参数…", objectName="mutedText")
        head_row.addWidget(self.variable_count_label, 1)
        self.select_all_button = QPushButton("全选", objectName="plotToolButton")
        self.select_none_button = QPushButton("取消全选", objectName="plotToolButton")
        self.select_all_button.clicked.connect(lambda: self._set_all_variables(True))
        self.select_none_button.clicked.connect(lambda: self._set_all_variables(False))
        head_row.addWidget(self.select_all_button)
        head_row.addWidget(self.select_none_button)
        variables.body.addLayout(head_row)

        self.parameter_table = QTableWidget(0, 6)
        self.parameter_table.setHorizontalHeaderLabels(
            ["启用", "设备参数", "下限", "上限", "起始值", "当前回读"]
        )
        self.parameter_table.verticalHeader().setVisible(False)
        self.parameter_table.verticalHeader().setDefaultSectionSize(28)
        self.parameter_table.setAlternatingRowColors(True)
        self.parameter_table.itemChanged.connect(self._on_variable_item_changed)
        table_header = self.parameter_table.horizontalHeader()
        table_header.setFixedHeight(26)
        table_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        table_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in (2, 3, 4, 5):
            table_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
        self.parameter_table.setColumnWidth(0, 46)
        self.parameter_table.setColumnWidth(2, 88)
        self.parameter_table.setColumnWidth(3, 88)
        self.parameter_table.setColumnWidth(4, 88)
        self.parameter_table.setColumnWidth(5, 104)
        variables.body.addWidget(self.parameter_table, 2)

        # 范围概况：横向标尺（设备边界 ┃ 设定区间 ┃ 起始值 ┃ 当前回读）。
        # 它填掉了左栏原来的空白，也把"范围贴边/越界"这类最常见的误配变成看得见的图形。
        # 变量多时标尺自己滚动（上限 7 行左右），不跟变量表抢整页高度。
        self.range_section = FoldSection(
            "范围概况", summary="设备边界 ┃ 设定区间 ┃ 起始值 ┃ 当前回读"
        )
        range_host = QWidget()
        self.range_rows = QGridLayout(range_host)
        self.range_rows.setContentsMargins(0, 0, 0, 0)
        self.range_rows.setHorizontalSpacing(8)
        self.range_rows.setVerticalSpacing(2)
        self.range_rows.setColumnStretch(1, 1)
        self.range_scroll = QScrollArea(objectName="rangeScroll")
        self.range_scroll.setWidgetResizable(True)
        self.range_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.range_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.range_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.range_scroll.setWidget(range_host)
        self.range_scroll.setMaximumHeight(190)
        self.range_section.add_widget(self.range_scroll)
        self.range_empty_label = QLabel("勾选变量后在这里显示范围标尺", objectName="mutedText")
        self.range_section.add_widget(self.range_empty_label)
        variables.body.addWidget(self.range_section, 1)

        self.variable_hint = QLabel("正在读取设备参数…", objectName="mutedText")
        self.variable_hint.setWordWrap(True)
        variables.body.addWidget(self.variable_hint)
        splitter.addWidget(variables)

        # ---------------- 右栏：三张紧凑参数卡 ----------------
        strategy = Panel("目标与策略", "算法、搜索策略与安全保护")
        self.setting_sections: dict[str, FoldSection] = {}

        # ---- 卡 1：目标与算法 ----
        algo = FoldSection("目标与算法")
        self.setting_sections["algorithm"] = algo
        algo_form = CompactForm()
        self.target = QComboBox()
        self.target.currentIndexChanged.connect(self._on_target_changed)
        algo_form.add("优化目标", self.target, key="target")

        self.engine_combo = QComboBox()
        for key, label in ENGINE_CHOICES:
            self.engine_combo.addItem(label, key)
        self.engine_combo.setToolTip(
            "CMA-ES 推荐用于连续联合调参；其余为 Optuna 采样器"
            "（TPE/CMA-ES/QMC/随机/网格），同一份历史观测可复用。"
        )
        algo_form.add("优化引擎", self.engine_combo, key="engine")

        self.mode_combo = QComboBox()
        for key, label in MODE_CHOICES:
            self.mode_combo.addItem(label, key)
        self.mode_combo.setToolTip(
            "auto：候选由执行服务自动写入（每轮仍过边界/单步/速率校验与束流保护）；"
            "confirm：每轮等你点「确认并执行本轮」。auto 必须启用束流丢失保护。"
        )
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        algo_form.add("调束模式", self.mode_combo, key="mode")

        self.strategy = QComboBox()
        for key, label in STRATEGY_CHOICES:
            self.strategy.addItem(label, key)
        self.strategy.setToolTip(
            "全部联合：所有勾选变量一起优化；"
            "逐参数 → 联合微调：先一个变量一个变量调（其余保持不动），"
            "再在最优点附近联合微调"
        )
        algo_form.add("优化策略", self.strategy, key="strategy")

        self.calls_spin = QSpinBox()
        self.calls_spin.setRange(1, 50)
        self.calls_spin.setValue(3)
        self.calls_spin.setToolTip("逐参数阶段每个变量用几轮")
        algo_form.add("逐参数轮次", mark_compact(self.calls_spin), key="calls")
        self.calls_label = algo_form.labels["calls"]

        self.joint_frac_spin = QDoubleSpinBox()
        self.joint_frac_spin.setRange(1.0, 100.0)
        self.joint_frac_spin.setSingleStep(5.0)
        self.joint_frac_spin.setValue(20.0)
        self.joint_frac_spin.setSuffix(" %")
        self.joint_frac_spin.setToolTip("联合微调范围（占各参数原范围）")
        algo_form.add("联合微调范围", mark_compact(self.joint_frac_spin), key="joint_frac")
        self.joint_frac_label = algo_form.labels["joint_frac"]

        self.iterations_spin = QSpinBox()
        self.iterations_spin.setRange(1, 200)
        self.iterations_spin.setValue(15)
        algo_form.add("最大轮次", mark_compact(self.iterations_spin), key="iterations")

        self.auto_hint = QLabel("", objectName="mutedText")
        self.auto_hint.setWordWrap(True)
        self.auto_hint.setVisible(False)
        algo.add_widget(algo_form)
        algo.add_widget(self.auto_hint)
        strategy.body.addWidget(algo)

        # ---- 卡 2：采样与稳定 ----
        sampling = FoldSection("采样与稳定")
        self.setting_sections["sampling"] = sampling
        sample_form = CompactForm()

        self.settle_spin = QDoubleSpinBox()
        self.settle_spin.setRange(0.5, 120.0)
        self.settle_spin.setSingleStep(0.5)
        self.settle_spin.setValue(20.0)
        self.settle_spin.setSuffix(" s")
        sample_form.add("回读稳定超时", mark_compact(self.settle_spin), key="settle")

        self.samples_spin = QSpinBox()
        self.samples_spin.setRange(1, 20)
        self.samples_spin.setValue(3)
        self.samples_spin.setToolTip("每轮目标采样次数，取中位数抗毛刺")
        sample_form.add("目标采样次数", mark_compact(self.samples_spin), key="samples")

        self.hold_spin = QDoubleSpinBox()
        self.hold_spin.setRange(0.0, 600.0)
        self.hold_spin.setSingleStep(1.0)
        self.hold_spin.setValue(0.0)
        self.hold_spin.setSuffix(" s")
        self.hold_spin.setToolTip("每轮写完后额外保持，给慢变量留时间")
        sample_form.add("每轮保持时间", mark_compact(self.hold_spin), key="hold")

        self.patience_spin = QSpinBox()
        self.patience_spin.setRange(0, 50)
        self.patience_spin.setValue(5)
        self.patience_spin.setToolTip(
            "收敛早停：连续这么多轮没有产生新的历史最优就正常结束（0 = 不启用）。"
            "省掉后半段原地踏步的轮次，适合现场时间紧张时使用。"
        )
        sample_form.add("收敛早停", mark_compact(self.patience_spin), key="patience")

        self.stall_fraction_form_spin = QDoubleSpinBox()
        self.stall_fraction_form_spin.setRange(0.1, 50.0)
        self.stall_fraction_form_spin.setSingleStep(0.5)
        self.stall_fraction_form_spin.setValue(5.0)
        self.stall_fraction_form_spin.setSuffix(" %")
        self.stall_fraction_form_spin.setDecimals(1)
        self.stall_fraction_form_spin.setToolTip(
            "两个用途：①过程建议里「近 10 轮目标提升不足 X%」的 X；"
            "②收敛早停的「有意义改进」门槛——提升不到 X% 不算改进，"
            "不会把连续无改进的计数清零（噪声大时调大，免得毛刺一直重置早停计数）。"
        )
        self.stall_fraction_form_spin.valueChanged.connect(lambda _v: self._refresh_advice())
        sample_form.add("停滞阈值", mark_compact(self.stall_fraction_form_spin), key="stall")

        self.startup_spin = QSpinBox()
        self.startup_spin.setRange(0, 100)
        self.startup_spin.setValue(5)
        self.startup_spin.setToolTip(
            "开始建模前先纯随机探索的轮数；仅 CMA-ES、TPE 生效，"
            "Random/QMC/Grid/GP 不受影响。"
        )
        sample_form.add("随机探索轮次", mark_compact(self.startup_spin), key="startup")

        self.seed_spin = QSpinBox()
        self.seed_spin.setRange(0, 999999)
        self.seed_spin.setValue(0)
        self.seed_spin.setToolTip(
            "随机种子：同一份配置 + 同一种子可复现同一串候选；换种子会换探索路径"
        )
        sample_form.add("随机种子", mark_compact(self.seed_spin), key="seed")

        self.random_seed_check = QCheckBox("每次使用随机种子（忽略上面的固定种子）")
        self.random_seed_check.setChecked(False)
        self.random_seed_check.setToolTip(
            "勾选后每次开始调束都用新的随机种子，第一轮点不再重复；"
            "不勾选则用上面的固定种子，路径可复现。"
        )
        self.random_seed_check.toggled.connect(
            lambda on: self.seed_spin.setEnabled(not on)
        )
        sample_form.add_full(self.random_seed_check)
        sampling.add_widget(sample_form)
        strategy.body.addWidget(sampling)

        # ---- 卡 3：安全保护与收尾 ----
        guard = FoldSection("安全保护与收尾")
        self.setting_sections["guard"] = guard
        guard_form = CompactForm()

        self.loss_protection_check = QCheckBox("启用束流丢失保护")
        self.loss_protection_check.setChecked(True)
        self.loss_protection_check.setToolTip(
            "关掉后执行服务完全不做束流异常判定，也不会自动回退；"
            "SIM 演示或确认不需要该保护时使用。"
        )
        guard_form.add_full(self.loss_protection_check)

        loss_row = QHBoxLayout()
        loss_row.setContentsMargins(0, 0, 0, 0)
        loss_row.setSpacing(6)
        self.loss_absolute_check = QCheckBox("绝对归零阈值：目标低于")
        self.loss_absolute_spin = QDoubleSpinBox()
        self.loss_absolute_spin.setDecimals(3)
        self.loss_absolute_spin.setRange(0.0, 1e6)
        self.loss_absolute_spin.setValue(0.0)
        mark_compact(self.loss_absolute_spin)
        self.loss_absolute_spin.setMaximumWidth(110)
        self.loss_absolute_check.setChecked(False)
        self.loss_absolute_spin.setEnabled(False)
        self.loss_absolute_check.toggled.connect(self.loss_absolute_spin.setEnabled)
        loss_row.addWidget(self.loss_absolute_check)
        loss_row.addWidget(self.loss_absolute_spin)
        loss_row.addStretch()
        loss_host = QWidget()
        loss_host.setLayout(loss_row)
        guard_form.add_full(loss_host)

        self.loss_relative_spin = QDoubleSpinBox()
        self.loss_relative_spin.setRange(0.0, 95.0)
        self.loss_relative_spin.setSingleStep(5.0)
        self.loss_relative_spin.setValue(50.0)
        self.loss_relative_spin.setSuffix(" %")
        self.loss_relative_spin.setToolTip("目标低于启动前基线的这个比例即判为束流丢失")
        guard_form.add(
            "相对损失阈值", mark_compact(self.loss_relative_spin), key="loss_relative"
        )

        self.loss_strikes_spin = QSpinBox()
        self.loss_strikes_spin.setRange(1, 20)
        self.loss_strikes_spin.setValue(2)
        self.loss_strikes_spin.setToolTip("连续几次异常才触发保护，单点毛刺不该把参数全退回去")
        guard_form.add(
            "连续异常次数", mark_compact(self.loss_strikes_spin), key="loss_strikes"
        )

        # 硬性丢失阈值：只在这个深度以下才算"束流丢失"。0 = 不启用（行为与旧版一致）。
        # 它的意义是给优化器的探索下探留出允许区间：软阈值（相对损失）与硬阈值之间
        # 只告警、不计数、不回退，避免一次预期内的下探把整轮参数退回去。
        self.loss_hard_spin = QDoubleSpinBox()
        self.loss_hard_spin.setRange(0.0, 95.0)
        self.loss_hard_spin.setSingleStep(5.0)
        self.loss_hard_spin.setValue(0.0)
        self.loss_hard_spin.setSuffix(" %")
        self.loss_hard_spin.setSpecialValueText("不启用")
        self.loss_hard_spin.setToolTip(
            "掉到基线以下多少才算「真的丢了束流」。设成 80% 时：\n"
            "· 下降超过「相对损失阈值」但没到 80% → 只告警（判断为探索下探，不计异常、不回退）\n"
            "· 下降超过 80% → 算丢失，连续命中「连续异常次数」就触发保护\n"
            "留空（不启用）时按旧的单阈值行为处理"
        )
        guard_form.add(
            "硬性丢失阈值", mark_compact(self.loss_hard_spin), key="loss_hard"
        )

        self.auto_recover_check = QCheckBox("触发保护时自动退回启动前参数")
        self.auto_recover_check.setChecked(True)
        self.auto_recover_check.setToolTip(
            "触发保护时由执行服务按启动前快照逐路退回（走执行层斜坡）；"
            "不勾选则只标记异常并停下，等人工处理"
        )
        guard_form.add_full(self.auto_recover_check)

        self.reset_before_start_check = QCheckBox("开始前复位到起始值（不继承上一次结果）")
        self.reset_before_start_check.setChecked(False)
        self.reset_before_start_check.setToolTip(
            "勾选后，开始调束会先把参与变量经执行层写到“起始值”列（默认为范围中值）再寻优，"
            "保证每次起点一致；不勾选则从设备当前值（可能是上次最优）开始。\n"
            "注意：启动前快照始终在复位**之前**记录，所以「恢复启动前参数」退回到的是"
            "任务真正开始前的位置，而不是复位点。"
        )
        guard_form.add_full(self.reset_before_start_check)

        self.max_invalid_spin = QSpinBox()
        self.max_invalid_spin.setRange(1, 20)
        self.max_invalid_spin.setValue(3)
        self.max_invalid_spin.setToolTip(
            "连续这么多轮没有产生有效测量（写入被拒 / 目标读不到）就停止任务。"
            "它与束流保护分开计数：写入被拒是执行问题，不是束流丢失。"
        )
        guard_form.add(
            "连续无效轮次上限", mark_compact(self.max_invalid_spin), key="max_invalid"
        )

        # 写入重试（默认 2 次）：只在**回读确认设备仍停在原值**时才重试同一个目标，
        # 因此不会把"可能已经写进去了"的一步重复下发；回读读不到或值变了仍按状态未知停下。
        self.write_retries_spin = QSpinBox()
        self.write_retries_spin.setRange(0, 5)
        self.write_retries_spin.setValue(2)
        self.write_retries_spin.setToolTip(
            "写入报错（例如 EPICS 命令超时）后最多再试几次。\n"
            "每次都会先回读确认：设备仍停在原值才重试（重写同一值是幂等的）；"
            "若回读已在目标值内，按成功处理；若读不到或值变了，一律按「设备状态未知」停下。\n"
            "0 = 不重试（一次超时就停下等人工确认）"
        )
        guard_form.add(
            "写入重试次数", mark_compact(self.write_retries_spin), key="write_retries"
        )

        self.finalize_pref = QComboBox()
        self.finalize_pref.addItem("不预设", "")
        for action, label in FINALIZE_LABELS.items():
            self.finalize_pref.addItem(label, action)
        self.finalize_pref.setToolTip(
            "保存进调束配置：任务结束时在结果页提示「要处置先考虑哪一种」"
        )
        guard_form.add("完成后处置建议", self.finalize_pref, key="finalize")

        # 总开关联动子控件
        self._loss_protection_widgets = [
            self.loss_absolute_check, self.loss_absolute_spin,
            self.loss_relative_spin, self.loss_hard_spin, self.loss_strikes_spin,
            self.auto_recover_check,
        ]

        def _toggle_loss_protection(on: bool) -> None:
            for widget in self._loss_protection_widgets:
                widget.setEnabled(on)
            if on:
                # 恢复绝对阈值子联动
                self.loss_absolute_spin.setEnabled(self.loss_absolute_check.isChecked())

        self.loss_protection_check.toggled.connect(_toggle_loss_protection)
        guard.add_widget(guard_form)
        guard.toggled.connect(lambda _on: self._refresh_guard_summary())
        strategy.body.addWidget(guard)
        strategy.body.addStretch()

        # ---------------- 页脚状态带：常驻不滚动 ----------------
        self.status_strip = StatusStrip()
        self.run_plan_label = QLabel("正在生成运行计划…", objectName="mutedText")
        self.run_plan_label.setWordWrap(True)
        self.preflight_label = QLabel("启动检查：请选择目标和优化变量", objectName="mutedText")
        self.preflight_label.setWordWrap(True)
        self.config_note = QLabel("", objectName="mutedText")
        self.config_note.setWordWrap(True)
        # 全局只读部署：按钮压住的同时必须说明原因，否则操作员会反复怀疑参数
        self.read_only_note = QLabel("", objectName="mutedText")
        self.read_only_note.setWordWrap(True)
        self.read_only_note.setVisible(False)
        for widget in (
            self.run_plan_label, self.preflight_label, self.config_note, self.read_only_note,
        ):
            self.status_strip.add_note(widget)

        self.advanced_button = QPushButton("高级参数")
        self.advanced_button.setCheckable(True)
        self.advanced_button.setToolTip("随机种子、早停、异常计数与配置保存等低频项")
        self.advanced_button.toggled.connect(self._toggle_advanced_options)
        self.status_strip.add_action(self.advanced_button)

        self.save_config_button = QPushButton("保存配置")
        self.save_config_button.setToolTip(
            "把目标、勾选变量与范围、策略、轮次、稳定超时、采样次数、束流丢失保护"
            "和完成后处置存到本机，下次打开自动载回"
        )
        self.save_config_button.clicked.connect(self.save_tuning_config)
        self.status_strip.add_action(self.save_config_button)
        self.load_config_button = QPushButton("载入配置")
        self.load_config_button.setToolTip("从本机配置重新载入（覆盖当前界面上的设置）")
        self.load_config_button.clicked.connect(self.load_tuning_config)
        self.status_strip.add_action(self.load_config_button)

        self.start_button = primary_button("开始自动调束")
        self.start_button.setToolTip(
            "范围必须落在设备允许区间内，且参数需配置最大单步；否则启动时会被拒绝——"
            "让优化器提出一个必然写不进去的值没有意义。"
        )
        self.start_button.clicked.connect(self.start_tuning)
        self.status_strip.add_action(self.start_button)
        splitter.addWidget(strategy)

        # 高级项：默认收起（低频，且展开后仍在同一网格里，不额外拉长页面）
        self._sequential_only_widgets = [
            self.calls_spin, self.calls_label,
            self.joint_frac_spin, self.joint_frac_label,
        ]
        self._advanced_widgets = [
            self.seed_spin, self.random_seed_check, self.startup_spin,
            self.patience_spin, self.loss_strikes_spin, self.finalize_pref,
            self.save_config_button, self.load_config_button, self.config_note,
        ]
        for form, keys in (
            (sample_form, ("startup", "seed")),
            (guard_form, ("loss_strikes", "finalize")),
        ):
            for key in keys:
                self._advanced_widgets.append(form.fields[key])
                self._advanced_widgets.append(form.labels[key])
        self._advanced_visible = False
        self.strategy.currentIndexChanged.connect(self._toggle_strategy_options)
        self._toggle_advanced_options(False)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([640, 476])
        outer.addWidget(splitter, 1)
        outer.addWidget(self.status_strip)

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
        self.loss_absolute_spin.valueChanged.connect(self._update_run_plan)
        # 长文案下拉框不该把面板最小宽度撑大：按"最小可见字符数"算尺寸，
        # 否则「CMA-ES（推荐：连续联合调参）」这类条目会把左栏挤窄。
        for combo in (
            self.target, self.engine_combo, self.mode_combo, self.strategy,
            self.finalize_pref,
        ):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(11)
        # 保护卡收起时靠摘要说明当前配置，所以每个相关控件都要刷新摘要
        for widget in (
            self.loss_protection_check, self.auto_recover_check,
        ):
            widget.toggled.connect(lambda _on: self._refresh_guard_summary())
        for widget in (
            self.loss_absolute_spin, self.loss_relative_spin, self.loss_hard_spin,
            self.loss_strikes_spin,
        ):
            widget.valueChanged.connect(lambda _value: self._refresh_guard_summary())
        self.loss_absolute_check.toggled.connect(
            lambda _on: self._refresh_guard_summary()
        )
        self._refresh_guard_summary()
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
        """逐参数专属配置：选了"逐参数 → 联合微调"就必须可见。

        这两个旋钮是策略本身的参数（每变量几轮、微调范围），不是"高级选项"：
        选了逐参数却看不到它们，等于让人签一份看不见的合同。
        """
        show = self._strategy_is_sequential()
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
        """运行监控页：状态条 + 指标带 + 曲线 + 实时过程日志（+ 候选确认条）。

        布局目标：曲线拿到页签内的主要高度（原来被 4 张 92px 指标卡、最近轮次表
        和候选区压到最小 280px），过程日志与轮次表并排放进右栏页签，不再各占一行。
        """
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 8, 0, 0)
        outer.setSpacing(8)

        # ---------------- 状态条：一行说完"现在在做什么" ----------------
        bar = QFrame(objectName="panel")
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 8, 14, 8)
        row.setSpacing(10)
        self.tuning_state = QLabel("尚未开始")
        self.iteration_label = QLabel("已完成 0 轮", objectName="mutedText")
        self.acknowledge_button = QPushButton("确认设备状态并继续")
        self.acknowledge_button.setToolTip(
            "先由执行服务重读各路实际回读（建立新的起点），再从当前实际位置继续调束；"
            "任何一路读不到都会拒绝继续。被打断的那一轮没有记录，会重新提候选。"
        )
        self.acknowledge_button.setVisible(False)
        self.acknowledge_button.clicked.connect(lambda: self._acknowledge(resume=True))
        self.acknowledge_stop_button = QPushButton("确认并停止")
        self.acknowledge_stop_button.setToolTip(
            "确认设备状态、释放设备锁，任务保持终止（不再继续调束）"
        )
        self.acknowledge_stop_button.setVisible(False)
        self.acknowledge_stop_button.clicked.connect(
            lambda: self._acknowledge(resume=False)
        )
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
        # 断线提示：连不上执行服务时必须说出来，不能让界面继续装作"数据是实时的"
        self.link_note = QLabel("", objectName="mutedText")
        self.link_note.setVisible(False)
        row.addWidget(self.link_note)
        row.addStretch()
        row.addWidget(self.acknowledge_button)
        row.addWidget(self.acknowledge_stop_button)
        row.addWidget(self.tuning_pause_button)
        row.addWidget(self.tuning_resume_button)
        row.addWidget(self.tuning_stop_button)
        outer.addWidget(bar)

        # ---------------- 指标带：压扁的 4 张卡（对象名/属性保持不变）----------------
        metrics = QHBoxLayout()
        metrics.setSpacing(8)
        self.current_card = MetricCard("本轮测量", "--")
        self.best_card = MetricCard("历史最优", "--", "", success=True)
        self.gain_card = MetricCard("提升量", "--")
        self.eta_card = MetricCard("预计剩余", "--")
        for card in (self.current_card, self.best_card, self.gain_card, self.eta_card):
            card.setMaximumHeight(58)
            card.setMinimumHeight(52)
            card.setStyleSheet(
                "QLabel#cardCaption { font-size: 11px; }"
                "QLabel#metricValue, QLabel#successValue { font-size: 17px; }"
            )
            layout = card.layout()
            if layout is not None:
                layout.setContentsMargins(12, 6, 12, 6)
                layout.setSpacing(2)
            metrics.addWidget(card, 1)
        outer.addLayout(metrics)

        # ---------------- 主体：左曲线 / 右过程日志与轮次 ----------------
        body = QSplitter(Qt.Orientation.Horizontal)
        body.setChildrenCollapsible(False)

        plot_panel = Panel(
            "目标量与响应曲线",
            "可切换：目标量收敛（x = 轮次）/ 单变量响应（x = 实际回读）",
        )
        plot_tool_row = QHBoxLayout()
        plot_tool_row.setContentsMargins(0, 0, 0, 0)
        plot_tool_row.setSpacing(6)
        plot_tool_row.addWidget(QLabel("视图", objectName="mutedText"))
        # 视图选择用**下拉**：变量多的时候按钮会铺满一整行、放不下也点不准。
        # 下拉只有一个控件宽度，长标签走 elide + tooltip，条目再多也不影响布局。
        self.plot_choice = QComboBox()
        mark_compact(self.plot_choice)
        self.plot_choice.setMinimumWidth(240)
        self.plot_choice.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.plot_choice.setMinimumContentsLength(18)
        self.plot_choice.setToolTip("切换过程视图：收敛曲线 / 单个变量的响应曲线")
        # 索引变化就重画：不再维护"按钮选中态"与"真实视图"两套状态
        self.plot_choice.currentIndexChanged.connect(lambda _index: self._render_plot())
        plot_tool_row.addWidget(self.plot_choice, 1)
        self.btn_export_plot = QPushButton("导出图", objectName="plotToolButton")
        self.btn_export_plot.setToolTip("导出当前曲线（PNG / SVG / PDF）")
        self.btn_export_plot.clicked.connect(lambda: self._export_plot(self.tuning_plot, "收敛曲线"))
        plot_tool_row.addWidget(self.btn_export_plot)
        plot_panel.body.addLayout(plot_tool_row)
        self.tuning_plot = SpectrumPlot("轮次", "目标量")
        self.tuning_plot.setMinimumHeight(260)
        self.tuning_plot.use_science_style(x_label="轮次", y_label="目标量", legend=True)
        plot_panel.body.addWidget(self.tuning_plot, 1)
        self.response_note = QLabel("", objectName="mutedText")
        self.response_note.setWordWrap(True)
        plot_panel.body.addWidget(self.response_note)
        body.addWidget(plot_panel)

        side = QTabWidget()
        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 6, 0, 0)
        log_layout.setSpacing(4)
        log_tool = QHBoxLayout()
        log_tool.setContentsMargins(0, 0, 0, 0)
        log_tool.setSpacing(4)
        log_tool.addWidget(QLabel("级别", objectName="mutedText"))
        self.log_level_combo = QComboBox()
        for key, label in (("all", "全部"), ("warn", "警告以上"), ("error", "仅错误")):
            self.log_level_combo.addItem(label, key)
        mark_compact(self.log_level_combo)
        self.log_level_combo.setMinimumWidth(96)
        self.log_level_combo.currentIndexChanged.connect(
            lambda _index: self._render_event_log()
        )
        log_tool.addWidget(self.log_level_combo)
        self.log_follow_check = QCheckBox("跟随最新")
        self.log_follow_check.setChecked(True)
        self.log_follow_check.toggled.connect(
            lambda _on: self._render_event_log()
        )
        log_tool.addWidget(self.log_follow_check)
        log_tool.addStretch()
        self.log_export_button = QPushButton("导出日志", objectName="plotToolButton")
        self.log_export_button.clicked.connect(self.export_tuning_log)
        log_tool.addWidget(self.log_export_button)
        log_layout.addLayout(log_tool)
        self.event_log_view = QPlainTextEdit()
        self.event_log_view.setReadOnly(True)
        self.event_log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.event_log_view.setPlaceholderText(
            "过程日志：一行一个事件（毫秒时间戳 · 级别 · 阶段 · 轮次 · 内容）"
        )
        log_layout.addWidget(self.event_log_view, 1)
        side.addTab(log_panel, "过程日志")

        recent_panel = QWidget()
        recent_layout = QVBoxLayout(recent_panel)
        recent_layout.setContentsMargins(0, 6, 0, 0)
        recent_layout.setSpacing(4)
        # 只显示最近几轮：精确读数看这张表，趋势看左边的曲线
        self.recent_table = QTableWidget(0, 5)
        self.recent_table.setHorizontalHeaderLabels(["轮次", "阶段", "目标值", "质量", "结果"])
        self.recent_table.verticalHeader().setVisible(False)
        self.recent_table.verticalHeader().setDefaultSectionSize(26)
        self.recent_table.horizontalHeader().setFixedHeight(26)
        self.recent_table.setAlternatingRowColors(True)
        recent_layout.addWidget(self.recent_table, 1)
        side.addTab(recent_panel, "最近轮次")

        proposal_panel = QWidget()
        proposal_layout = QVBoxLayout(proposal_panel)
        proposal_layout.setContentsMargins(0, 6, 0, 0)
        proposal_layout.setSpacing(4)
        self.proposal_table = QTableWidget(0, 4)
        self.proposal_table.setHorizontalHeaderLabels(["参数", "当前回读", "建议值", "变化"])
        self.proposal_table.verticalHeader().setVisible(False)
        self.proposal_table.verticalHeader().setDefaultSectionSize(26)
        self.proposal_table.horizontalHeader().setFixedHeight(26)
        self.proposal_table.setAlternatingRowColors(True)
        proposal_layout.addWidget(self.proposal_table, 1)
        side.addTab(proposal_panel, "候选明细")
        self.monitor_side_tabs = side
        body.addWidget(side)
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 2)
        body.setSizes([640, 470])
        outer.addWidget(body, 1)

        # ---------------- 候选确认条（auto 模式下整条隐藏）----------------
        confirm = QFrame(objectName="noticePanel")
        self.proposal_panel = confirm
        confirm_layout = QHBoxLayout(confirm)
        confirm_layout.setContentsMargins(14, 8, 14, 8)
        confirm_layout.setSpacing(10)
        self.proposal_label = QLabel("等待候选…", objectName="mutedText")
        self.proposal_label.setWordWrap(True)
        self.approve_button = primary_button("确认并执行本轮")
        self.approve_button.setEnabled(False)
        self.approve_button.clicked.connect(self._approve)
        confirm_layout.addWidget(self.proposal_label, 1)
        confirm_layout.addWidget(self.approve_button)
        outer.addWidget(confirm)

        # ---------------- 只在需要时出现的提示行 ----------------
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
        return tab

    # ------------------------------------------------------------------
    # 页签 3：结果确认
    # ------------------------------------------------------------------
    def _result_tab(self) -> QWidget:
        """结果与处置页：顶部摘要 + 左分析 / 右明细 + 固定处置栏。

        布局目标：原来 7 个区块竖排（内容 1690px，视口 691px，要滚近 1000px），
        现在把 参数变化 / Trial 明细 / 调束日志 收进右侧页签容器，
        竖滚动条归零，分析图拿到属于自己的高度（科学图的读数与标注需要空间）。
        """
        tab = QWidget()
        root = QVBoxLayout(tab)
        root.setContentsMargins(0, 8, 0, 0)
        root.setSpacing(8)

        notice = QFrame(objectName="noticePanel")
        notice_row = QHBoxLayout(notice)
        notice_row.setContentsMargins(14, 8, 14, 8)
        self.result_notice = QLabel("尚未完成任何调束")
        self.result_detail = QLabel("", objectName="mutedText")
        notice_row.addWidget(self.result_notice)
        notice_row.addStretch()
        notice_row.addWidget(self.result_detail)
        root.addWidget(notice)

        summary = QHBoxLayout()
        summary.setSpacing(8)
        self.result_baseline_card = MetricCard("启动基线", "--")
        self.result_best_card = MetricCard("最佳目标", "--", "", success=True)
        self.result_gain_card = MetricCard("相对提升", "--")
        self.result_quality_card = MetricCard("有效轮次", "0 / 0")
        for card in (
            self.result_baseline_card, self.result_best_card,
            self.result_gain_card, self.result_quality_card,
        ):
            card.setMaximumHeight(58)
            card.setMinimumHeight(52)
            card.setStyleSheet(
                "QLabel#cardCaption { font-size: 11px; }"
                "QLabel#metricValue, QLabel#successValue { font-size: 17px; }"
            )
            layout = card.layout()
            if layout is not None:
                layout.setContentsMargins(12, 6, 12, 6)
                layout.setSpacing(2)
            summary.addWidget(card, 1)
        root.addLayout(summary)

        body = QSplitter(Qt.Orientation.Horizontal)
        body.setChildrenCollapsible(False)

        # ---------------- 左：结果分析 ----------------
        analysis = Panel("结果分析", "优化历史、参数重要性、切片、轮廓与响应面矩阵")
        analysis_toolbar = QHBoxLayout()
        analysis_toolbar.setContentsMargins(0, 0, 0, 0)
        analysis_toolbar.setSpacing(6)
        # 分析视图也用**下拉**：原来把每个"切片 · 参数名"做成按钮塞进一条 30px 高的
        # 横向滚动区，条目一多就被滚动条压住看不见（变量十几个时尤其明显）。
        # 下拉只有一个控件宽度，条目再多也不影响布局。
        analysis_toolbar.addWidget(QLabel("分析视图", objectName="mutedText"))
        self.analysis_choice_combo = QComboBox()
        mark_compact(self.analysis_choice_combo)
        self.analysis_choice_combo.setMinimumWidth(240)
        self.analysis_choice_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.analysis_choice_combo.setMinimumContentsLength(18)
        self.analysis_choice_combo.setToolTip(
            "优化历史 / 参数重要性 / 各参数切片 / 响应面矩阵；变量多时用它切换"
        )
        self.analysis_choice_combo.currentIndexChanged.connect(
            lambda _index: self._on_analysis_choice_changed()
        )
        analysis_toolbar.addWidget(self.analysis_choice_combo, 1)
        self.btn_export_analysis = QPushButton("导出图", objectName="plotToolButton")
        self.btn_export_analysis.setToolTip("导出当前分析图（PNG / SVG / PDF）")
        self.btn_export_analysis.clicked.connect(self._export_current_analysis)
        analysis_toolbar.addWidget(self.btn_export_analysis)
        self.open_dashboard_button = QPushButton("浏览器深度分析（英文）")
        self.open_dashboard_button.setObjectName("plotToolButton")
        self.open_dashboard_button.setToolTip(
            "常用分析（爬山图/参数重要性/多维切片/Trial 明细）已在左侧内嵌、"
            "中文展示；浏览器版为 optuna-dashboard，另提供等高线、平行坐标等"
            "深度交互，界面为英文。"
        )
        self.open_dashboard_button.clicked.connect(self._open_dashboard)
        analysis_toolbar.addWidget(self.open_dashboard_button)
        analysis.body.addLayout(analysis_toolbar)
        self.analysis_current_choice = "history"
        self._rebuild_analysis_choices([("优化历史", "history")])
        self.analysis_plot = SpectrumPlot("轮次", "目标量")
        self.analysis_plot.setMinimumHeight(240)
        self.analysis_plot.use_science_style(x_label="轮次", y_label="目标量")
        analysis.body.addWidget(self.analysis_plot, 1)
        # 响应面矩阵：n×n 配对图，用可滚动容器装，格子按视口自适应
        self.surface_container = QScrollArea()
        self.surface_container.setWidgetResizable(True)
        self.surface_container_widget = QWidget()
        self.surface_grid = QGridLayout(self.surface_container_widget)
        self.surface_grid.setSpacing(4)
        self.surface_container.setWidget(self.surface_container_widget)
        self.surface_container.setVisible(False)
        analysis.body.addWidget(self.surface_container, 1)
        self.analysis_note = QLabel(
            "任务结束后自动加载 Optuna 分析；优化历史始终可由本地轮次生成。",
            objectName="mutedText",
        )
        self.analysis_note.setWordWrap(True)
        analysis.body.addWidget(self.analysis_note)
        body.addWidget(analysis)

        # ---------------- 右：明细（参数变化 / Trial / 日志）----------------
        detail = QTabWidget()
        changes_page = QWidget()
        changes_layout = QVBoxLayout(changes_page)
        changes_layout.setContentsMargins(0, 6, 0, 0)
        changes_layout.setSpacing(4)
        self.changes_table = QTableWidget(0, 5)
        self.changes_table.setHorizontalHeaderLabels(
            ["参数", "启动前", "最优轮", "最后一轮", "当前"]
        )
        self.changes_table.verticalHeader().setVisible(False)
        self.changes_table.verticalHeader().setDefaultSectionSize(26)
        self.changes_table.horizontalHeader().setFixedHeight(26)
        self.changes_table.setAlternatingRowColors(True)
        changes_layout.addWidget(self.changes_table, 1)
        self.change_note = QLabel("", objectName="mutedText")
        self.change_note.setWordWrap(True)
        changes_layout.addWidget(self.change_note)
        detail.addTab(changes_page, "参数变化")

        trials_page = QWidget()
        trials_layout = QVBoxLayout(trials_page)
        trials_layout.setContentsMargins(0, 6, 0, 0)
        self.analysis_trial_table = QTableWidget(0, 3)
        self.analysis_trial_table.setHorizontalHeaderLabels(["Trial", "状态", "目标值"])
        self.analysis_trial_table.verticalHeader().setVisible(False)
        self.analysis_trial_table.verticalHeader().setDefaultSectionSize(26)
        self.analysis_trial_table.horizontalHeader().setFixedHeight(26)
        self.analysis_trial_table.setAlternatingRowColors(True)
        trials_layout.addWidget(self.analysis_trial_table, 1)
        detail.addTab(trials_page, "Trial 明细")

        log_page = QWidget()
        log_layout = QVBoxLayout(log_page)
        log_layout.setContentsMargins(0, 6, 0, 0)
        log_layout.setSpacing(4)
        # 调束日志：建议 → 下发 → 回读 → 目标，一轮一行（带时间戳）
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        log_layout.addWidget(self.log_view, 1)
        log_row = QHBoxLayout()
        log_row.setContentsMargins(0, 0, 0, 0)
        self.export_log_button = QPushButton("导出日志（JSONL）", objectName="plotToolButton")
        self.export_log_button.setToolTip(
            "一轮一行导出，含本次任务的目标量、算法版本、种子、基线与过程建议，"
            "便于事后用脚本复盘"
        )
        self.export_log_button.clicked.connect(self.export_tuning_log)
        log_row.addWidget(self.export_log_button)
        self.log_note = QLabel("", objectName="mutedText")
        self.log_note.setWordWrap(True)
        log_row.addWidget(self.log_note, 1)
        log_layout.addLayout(log_row)
        detail.addTab(log_page, "调束日志")
        self.result_detail_tabs = detail
        body.addWidget(detail)
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 2)
        body.setSizes([640, 470])
        root.addWidget(body, 1)

        # ---------------- 底部：结束后设备处置（固定，不随分析滚动）----------------
        # 任务结束时设备停在**最后一轮**参数上，而那不一定是最优轮，所以四种收尾
        # 方式要由操作员明确选，且都要二次确认。
        disposal = Panel("结束后设备处置", "四种动作都会写设备，执行前需二次确认")
        disposal_row = QHBoxLayout()
        disposal_row.setContentsMargins(0, 0, 0, 0)
        disposal_row.setSpacing(6)
        self.finalize_buttons: dict[str, QPushButton] = {}
        for action, label in FINALIZE_LABELS.items():
            button = QPushButton(label)
            button.setEnabled(False)
            button.clicked.connect(
                lambda _checked=False, a=action: self.finalize_tuning(a)
            )
            disposal_row.addWidget(button)
            self.finalize_buttons[action] = button
        self.finalize_label = QLabel("", objectName="mutedText")
        self.finalize_label.setWordWrap(True)
        disposal_row.addWidget(self.finalize_label, 1)
        disposal.body.addLayout(disposal_row)
        note = QLabel(
            "「应用最优参数」按最优轮下发；「恢复启动前参数」用启动前快照；"
            "「回到起始值」写到“起始值”列；「回安全值」退到安全值或配置下限。"
            "四种都走执行层（含单步与速率约束），并逐路等回读到位后才报成功。",
            objectName="mutedText",
        )
        note.setWordWrap(True)
        disposal.body.addWidget(note)
        root.addWidget(disposal)
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
        # 范围概况每次重建：映射刷新时变量集合可能变，旧标尺必须清掉
        while self.range_rows.count():
            item = self.range_rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.range_empty_label.setVisible(not candidates)
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
                mark_compact(spin)  # 32px → 22px：表格行高随之降到 28px
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
            mark_compact(start_spin)
            low_spin.valueChanged.connect(self._update_run_plan)
            high_spin.valueChanged.connect(self._update_run_plan)
            self.parameter_table.setCellWidget(row, 2, low_spin)
            self.parameter_table.setCellWidget(row, 3, high_spin)
            self.parameter_table.setCellWidget(row, 4, start_spin)
            readback = QLabel("--")
            readback.setObjectName("mutedText")
            self.parameter_table.setCellWidget(row, 5, readback)

            # 左栏下方的范围标尺：同一份数据换一种画法——"设备边界 / 设定区间 /
            # 起始值 / 当前回读"叠在一条线上，贴边与越界一眼可见（调束最常见的误配）。
            range_label = QLabel(
                str(entry.get("label", entry.get("signal"))), objectName="mutedText"
            )
            range_bar = RangeBar()
            range_text = QLabel("", objectName="mutedText")
            range_text.setMinimumWidth(186)
            self.range_rows.addWidget(range_label, row, 0)
            self.range_rows.addWidget(range_bar, row, 1)
            self.range_rows.addWidget(range_text, row, 2)
            self._rows.append(
                {
                    "entry": entry,
                    "check": check,
                    "low": low_spin,
                    "high": high_spin,
                    "start": start_spin,
                    "readback": readback,
                    "range_bar": range_bar,
                    "range_text": range_text,
                }
            )
            for spin in (low_spin, high_spin, start_spin):
                spin.valueChanged.connect(
                    lambda _value, index=row: self._refresh_range_row(index)
                )
        self._refresh_range_rows()
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
        if hasattr(self, "variable_count_label"):
            self.variable_count_label.setText(
                f"{len(self._rows)} 个可调参数 · {usable} 个可调束 · 已勾选 {checked}"
            )

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
                row["_readback_value"] = float(reading["value"])
            else:
                row["readback"].setText("未连接")
                row["_readback_value"] = None
        self._refresh_range_rows()

    # ------------------------------------------------------------------
    # 范围标尺与配置摘要
    # ------------------------------------------------------------------
    @staticmethod
    def _readback_value(row: dict) -> float | None:
        """该变量当前的数值回读（回读列文本是主要来源，取不到就用缓存值）。"""
        cached = row.get("_readback_value")
        if isinstance(cached, float):
            return cached
        text = row["readback"].text()
        try:
            return float(text.split()[0])
        except (ValueError, IndexError):
            return None

    def _refresh_range_row(self, index: int) -> None:
        if not (0 <= index < len(self._rows)):
            return
        row = self._rows[index]
        entry = row["entry"]
        unit = str(entry.get("unit", ""))
        readback = self._readback_value(row)
        row["range_bar"].set_values(
            low=float(row["low"].value()),
            high=float(row["high"].value()),
            start=float(row["start"].value()),
            readback=readback,
            device_low=entry.get("min_value"),
            device_high=entry.get("max_value"),
            unit=unit,
        )
        low = float(row["low"].value())
        high = float(row["high"].value())
        text = f"{low:g} – {high:g} {unit}".strip()
        if readback is not None:
            marker = "✓" if low <= readback <= high else "⚠"
            text += f" · 当前 {readback:g} {marker}"
        row["range_text"].setText(text)
        row["range_text"].setProperty(
            "state", "" if readback is None or low <= readback <= high else "warn"
        )
        row["range_text"].style().unpolish(row["range_text"])
        row["range_text"].style().polish(row["range_text"])

    def _refresh_range_rows(self) -> None:
        for index in range(len(self._rows)):
            self._refresh_range_row(index)

    def _refresh_guard_summary(self) -> None:
        """折叠状态下也要写清"当前保护是什么配置"，否则收起等于把信息藏了。"""
        section = getattr(self, "setting_sections", {}).get("guard")
        if section is None:
            return
        if not self.loss_protection_check.isChecked():
            section.set_summary("保护已关闭（不做束流异常判定）")
            return
        parts: list[str] = []
        if self.loss_absolute_check.isChecked():
            parts.append(f"绝对阈值 {self.loss_absolute_spin.value():g}")
        if self.loss_relative_spin.value() > 0:
            parts.append(f"相对损失 {self.loss_relative_spin.value():g}%")
        if self.loss_hard_spin.value() > 0:
            parts.append(f"硬性丢失 {self.loss_hard_spin.value():g}%（其间仅告警）")
        parts.append(f"连续 {self.loss_strikes_spin.value()} 次触发")
        parts.append("自动回退" if self.auto_recover_check.isChecked() else "仅告警")
        section.set_summary(" · ".join(parts))

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
            "loss_relative_hard": (
                self.loss_hard_spin.value() if self.loss_hard_spin.value() > 0 else None
            ),
            "loss_strikes": self.loss_strikes_spin.value(),
            "max_invalid_rounds": self.max_invalid_spin.value(),
            "write_retries": self.write_retries_spin.value(),
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
            ("max_invalid_rounds", self.max_invalid_spin),
            ("write_retries", self.write_retries_spin),
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
        hard = config.get("loss_relative_hard")
        # 0 = 不启用（specialValueText 显示"不启用"）
        self.loss_hard_spin.setValue(0.0 if hard is None else float(hard))
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
        self._refresh_guard_summary()
        self._refresh_range_rows()
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
        self._log_lines_written = 0
        # 新一轮：事件游标与断线计数都要归零，否则会把上一次任务的事件混进新日志
        self._events = []
        self._last_seq = 0
        self._link_failures = 0
        self._last_success_monotonic = None
        self._stale_iteration_fetches = 0
        self._iteration_fetch_baseline = 0
        if hasattr(self, "link_note"):
            self.link_note.setVisible(False)
        if hasattr(self, "event_log_view"):
            self.event_log_view.clear()
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
                # 0 = 不启用硬阈值（服务端按旧的单阈值行为处理）
                "loss_relative_hard": (
                    self.loss_hard_spin.value() / 100.0
                    if self.loss_hard_spin.value() > 0
                    else None
                ),
                "loss_strikes": self.loss_strikes_spin.value(),
                "max_invalid_rounds": self.max_invalid_spin.value(),
                "write_retries": self.write_retries_spin.value(),
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

    def _acknowledge(self, resume: bool = True) -> None:
        """操作员确认设备状态：``resume=True`` 让服务端重读现场后继续调束。"""
        if not self._run_id:
            return
        self.acknowledge_button.setEnabled(False)
        self.acknowledge_stop_button.setEnabled(False)
        thread = instrument_api.request_tuning_acknowledge(
            self._run_id, note="操作员在界面确认", resume=resume
        )
        thread.completed.connect(self._on_acknowledged)

    def _on_acknowledged(self, payload: dict) -> None:
        self.acknowledge_button.setEnabled(True)
        self.acknowledge_stop_button.setEnabled(True)
        if not payload.get("ok"):
            # 读不到回读 / 设备组被占用都会走到这里：原样转述，绝不假装继续了
            self._set_recovery_note(
                "⚠ 确认未生效："
                + str(payload.get("message") or "原因未返回")
                + "；任务仍处于「需人工确认设备状态」，请处理后重试。",
                "warn",
            )
            return
        status = payload.get("payload") or {}
        self._apply_status(status)
        if str(status.get("state")) not in TERMINAL_STATES:
            # 已经回到运行/待确认：恢复轮询，界面立刻接上
            self._set_recovery_note(
                "✅ 已确认设备状态：执行服务重读各路回读后从当前位置继续调束", "good"
            )
            self._poll()
            self._timer.start()
        else:
            self.acknowledge_button.setVisible(False)
            self.acknowledge_stop_button.setVisible(False)

    def _set_recovery_note(self, text: str, state: str) -> None:
        """把人工介入的结论写到监控页那条提示上（页面没有 toast 通道）。"""
        self.recovery_label.setText(text)
        self.recovery_label.setProperty("state", state)
        self.recovery_label.style().unpolish(self.recovery_label)
        self.recovery_label.style().polish(self.recovery_label)
        self.recovery_label.setVisible(True)

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
        """一次轮询取三样东西：状态 + 新增轮次 + 新增事件。

        用 ``/progress`` 而不是分开拉 status/iterations：两个响应各有各的时间点，
        "状态说 5 轮、列表还是 4 轮"这种撕裂会短暂出现在界面上。事件用 ``since``
        增量拉，避免每 700ms 重传整份过程日志。
        """
        if not self._run_id:
            self._timer.stop()
            return
        if self._status_in_flight:
            return
        self._status_in_flight = True
        last_iteration = -1
        if self._iterations:
            try:
                last_iteration = int(self._iterations[-1].get("iteration", -1))
            except (TypeError, ValueError):
                last_iteration = -1
        try:
            thread = instrument_api.request_tuning_progress(
                self._run_id, after_iteration=last_iteration
            )
            thread.completed.connect(self._on_progress)
        except AttributeError:  # 老客户端/被替换过的模块：退回旧的两段式轮询
            thread = instrument_api.request_tuning_status(self._run_id)
            thread.completed.connect(self._on_status)
        self._fetch_events()

    def _fetch_events(self) -> None:
        if not self._run_id or not hasattr(instrument_api, "request_tuning_events"):
            return
        try:
            thread = instrument_api.request_tuning_events(self._run_id, since=self._last_seq)
            thread.completed.connect(self._on_events)
        except Exception:  # noqa: BLE001 —— 事件拉取失败不该让轮询停摆
            return

    def _on_progress(self, payload: dict) -> None:
        self._status_in_flight = False
        if not payload.get("ok"):
            self._note_link_loss(payload.get("message") or "请求失败")
            return
        self._note_link_ok()
        body = payload.get("payload") or {}
        self._last_seen_latest_seq = int(body.get("latest_seq") or 0)
        self._apply_status(body.get("status") or {})
        new_records = list(body.get("iterations") or [])
        if new_records:
            # 增量合并：只有比本地更新的轮次才追加，避免覆盖已渲染的曲线
            known = {int(item.get("iteration", -1)) for item in self._iterations}
            self._iterations.extend(
                item for item in new_records
                if int(item.get("iteration", -1)) not in known
            )
            self._iterations.sort(key=lambda item: int(item.get("iteration", 0)))
            self._last_completed = len(self._iterations)
            self._refresh_iteration_views()

    def _on_status(self, payload: dict) -> None:
        self._status_in_flight = False
        if not payload.get("ok"):
            self._note_link_loss(payload.get("message") or "请求失败")
            return
        self._note_link_ok()
        self._apply_status(payload.get("payload") or {})
        # 运行中也必须增量拉轮次；旧逻辑只在结束/人工确认后拉，
        # 因此“本轮测量 / 历史最优”会看起来到结束才刷新。
        self._fetch_iterations()

    def _on_events(self, payload: dict) -> None:
        if not payload.get("ok"):
            return
        body = payload.get("payload") or {}
        incoming = list(body.get("events") or [])
        if incoming:
            known = {int(event.get("seq", -1)) for event in self._events}
            self._events.extend(
                event for event in incoming if int(event.get("seq", -1)) not in known
            )
            self._events.sort(key=lambda event: int(event.get("seq", 0)))
            self._render_event_log()
        latest = body.get("latest_seq")
        if latest is not None:
            self._last_seq = max(self._last_seq, int(latest))

    def _render_event_log(self) -> None:
        """把服务端事件渲染进过程日志面板（级别过滤 + 跟随最新）。

        面板是"过程"的窗口：一行一个事件，带毫秒时间戳、级别、阶段与轮次。
        级别过滤只影响显示，不影响已收下的事件（导出仍然是全量）。
        老执行服务没有事件流时退回"按轮次渲染"（轮末时间戳），并在首行说明。
        """
        view = getattr(self, "event_log_view", None)
        if view is None:
            return
        if not self._events:
            self._fill_process_log()
            return
        threshold = self._log_level_filter()
        lines = [
            self._format_event_line(event)
            for event in self._events
            if self._level_rank(str(event.get("level") or "info")) >= threshold
        ]
        if not lines:
            lines = ["（当前过滤条件下没有事件：把级别放宽到「全部」）"]
        text = "\n".join(lines)
        if text != view.toPlainText():
            view.setPlainText(text)
        self._log_lines_written = -1
        if getattr(self, "log_follow_check", None) is not None and self.log_follow_check.isChecked():
            scrollbar = view.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def _log_level_filter(self) -> int:
        combo = getattr(self, "log_level_combo", None)
        if combo is None:
            return 0
        data = combo.currentData()
        return {"all": 0, "warn": 1, "error": 2}.get(str(data), 0)

    @staticmethod
    def _level_rank(level: str) -> int:
        return {"info": 0, "warn": 1, "error": 2}.get(level.lower(), 0)

    def _format_event_line(self, event: dict) -> str:
        """事件行：``[时间] 级别 阶段 轮次 内容``（列宽固定，方便竖直扫读）。"""
        level = str(event.get("level") or "info").upper()
        phase = PHASE_TEXT.get(str(event.get("phase") or ""), str(event.get("phase") or ""))
        iteration = event.get("iteration")
        where = f"第 {int(iteration) + 1} 轮" if isinstance(iteration, int) else "－"
        return (
            f"[{tuning_analysis.format_local_time(event.get('at'))}] "
            f"{level:<5s} {phase:<8s} {where:<6s} {event.get('message', '')}"
        )

    def _seconds_per_round(self) -> float:
        """每轮耗时估计：**优先用实测中位耗时**，拿不到才退回静态公式。

        静态公式（稳定超时 + 保持时间）把"超时上限"当成了"必然耗时"，
        通常高估好几倍；跑起来之后没有任何理由继续用猜的。
        """
        measured = sorted(
            float(item["duration_ms"]) / 1000.0
            for item in self._iterations
            if isinstance(item.get("duration_ms"), (int, float))
            and float(item["duration_ms"]) > 0
        )
        if measured:
            middle = len(measured) // 2
            if len(measured) % 2:
                return measured[middle]
            return (measured[middle - 1] + measured[middle]) / 2.0
        return (
            self.settle_spin.value() + self.hold_spin.value()
            + max(0, self.samples_spin.value() - 1) * 0.05
        )

    def _maybe_refresh_readbacks(self) -> None:
        """运行中定时刷新变量回读（默认 2s）：结果页的"当前"列必须反映设备此刻位置。

        旧实现只在载入映射/切目标时读一次，于是"当前"列停在启动前的值，
        看起来像设备根本没动。
        """
        import time as _time

        now = _time.monotonic()
        if now - self._last_readback_at < 2.0:
            return
        self._last_readback_at = now
        self._refresh_readbacks()

    def _maybe_fetch_events(self) -> None:
        """事件拉取的兜底：只在本地游标落后于服务端时再拉一次。"""
        if self._last_seq < self._last_seen_latest_seq:
            self._fetch_events()

    def _note_link_loss(self, reason: str) -> None:
        """轮询失败要说出来：断线时界面不能继续装作数据是实时的。"""
        self._link_failures += 1
        if self._link_failures < 2:
            return
        note = getattr(self, "link_note", None)
        if note is None:
            return
        last = self._last_success_monotonic
        stamp = "--:--:--"
        if last is not None:
            from datetime import datetime

            stamp = datetime.fromtimestamp(last).strftime("%H:%M:%S")
        note.setText(f"⚠ 与执行服务失去连接（{reason}）；最后可信数据 {stamp}")
        note.setProperty("state", "warn")
        note.style().unpolish(note)
        note.style().polish(note)
        note.setVisible(True)

    def _note_link_ok(self) -> None:
        import time as _time

        self._link_failures = 0
        self._last_success_monotonic = _time.time()
        note = getattr(self, "link_note", None)
        if note is not None and not note.isHidden():
            note.setVisible(False)

    def _refresh_iteration_views(self) -> None:
        """轮次变化后统一刷新所有派生视图（曲线 / 表 / 日志 / 结果摘要）。"""
        self._redraw()
        self._fill_changes()
        self._refresh_plot_choices()
        self._refresh_advice()
        self._fill_log()
        self._fill_recent_iterations()
        self._update_result_summary()
        self._fill_process_log()
        self._refresh_result_analysis_choices()
        self._render_result_analysis()
        self._fill_analysis_trials()

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
        self.eta_card.value_label.setText(
            "已结束" if self._state in TERMINAL_STATES
            else self._format_duration(remaining * self._seconds_per_round()).removeprefix("约 ")
        )
        self._last_completed = max(self._last_completed, completed)
        self.iteration_label.setText(
            f"已完成 {completed} / {status.get('max_iterations', 0)} 轮{self._stage_suffix(status)}"
        )
        # 保护是否真的有效：服务端算好的结论直接展示，不让操作员从阈值反推
        protection = status.get("protection_reasons")
        if protection:
            self._protection_reasons = [str(item) for item in protection]
        if self._state not in TERMINAL_STATES:
            self._maybe_refresh_readbacks()
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
            # 两个出口都摆出来：继续（重读现场后接着调）/ 确认并停止
            self.acknowledge_button.setVisible(True)
            self.acknowledge_stop_button.setVisible(True)
        else:
            self.acknowledge_button.setVisible(False)
            self.acknowledge_stop_button.setVisible(False)

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
            # 没有回退记录、但停在"需人工确认"：这是**写入中断**（例如 EPICS 命令超时）。
            # 界面必须把"为什么停、现在设备在哪不确定、下一步点哪里"说清楚，
            # 否则现场只会看到一句"需人工确认设备状态"然后干等。
            if self._state == "recovery_required":
                self.recovery_label.setText(
                    "⚠ 写入中断，设备实际状态未知，任务已暂停等人工确认："
                    + (self.tuning_state.text() or "")
                    + "。请先在设备侧确认这一路参数的实际位置，然后选"
                    "「确认设备状态并继续」（执行服务会逐路重读、建立新起点后继续调束）"
                    "或「确认并停止」（释放设备锁、任务结束）。"
                )
                self.recovery_label.setProperty("state", "error")
                self.recovery_label.style().unpolish(self.recovery_label)
                self.recovery_label.style().polish(self.recovery_label)
                self.recovery_label.setVisible(True)
                return
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
        """增量拉取（老路径的兜底）：只在服务端轮数比本地多时请求，防并发。

        这里加了一道"重拉守卫"：如果服务端的 ``completed_iterations`` 比实际能取回的
        轮次记录多（例如服务重启后内存丢了、spool 里少几条），``local < completed``
        会永远成立——旧实现会每 700ms 全量重拉一次，白刷一整天。连续几次拉不到新数据
        就停下来，并把原因写进结果摘要，而不是继续无声地请求。
        """
        if not self._run_id:
            return
        if getattr(self, "_iterations_in_flight", False):
            return
        completed = int(getattr(self, "_last_completed", 0))
        local = len(self._iterations)
        if local >= completed:
            return
        if getattr(self, "_stale_iteration_fetches", 0) >= 3:
            return
        self._iterations_in_flight = True
        self._iteration_fetch_baseline = local
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
        if len(self._iterations) <= getattr(self, "_iteration_fetch_baseline", 0):
            self._stale_iteration_fetches = getattr(self, "_stale_iteration_fetches", 0) + 1
            if self._stale_iteration_fetches == 3:
                self._set_response_note(
                    "轮次记录与服务端的计数对不上（服务端报的轮数多于可取回的记录）："
                    "已停止重复拉取，界面显示的是实际拿到的轮次"
                )
        else:
            self._stale_iteration_fetches = 0
        self._refresh_iteration_views()

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
        """过程页视图列表：收敛 + 每个出现过的变量（下拉，变量多也不挤）。

        变量多时条目会很长，所以每项的 tooltip 写全名，显示交给下拉自己 elide。
        """
        variables = tuning_analysis.curve_variables(self._iterations)
        current = self.plot_choice.currentData() or ""
        self.plot_choice.blockSignals(True)
        self.plot_choice.clear()
        self.plot_choice.addItem("收敛（目标量 vs 轮次）", "")
        for signal in variables:
            label = self._row_label(signal)
            self.plot_choice.addItem(label, signal)
            self.plot_choice.setItemData(
                self.plot_choice.count() - 1, label, Qt.ItemDataRole.ToolTipRole
            )
        index = self.plot_choice.findData(current)
        self.plot_choice.setCurrentIndex(max(index, 0))
        self.plot_choice.blockSignals(False)

    def _select_view(self, data: str) -> None:
        """切换过程页视图；索引变化即重画（下拉是唯一事实来源）。"""
        index = self.plot_choice.findData(data)
        if index == self.plot_choice.currentIndex():
            self._render_plot()
        else:
            self.plot_choice.setCurrentIndex(index)

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
        """画收敛（爬山）曲线或某个变量的响应曲线。

        收敛视图走 :meth:`SpectrumPlot.plot_trials`：实测散点 + 误差棒 +
        best-so-far 阶梯线 + 基线参考线，轴"只扩不缩"——新轮次出现时曲线是
        "长出来"的，而不是整屏重新缩放（原来那种跳动感主要来自这里）。
        """
        best = best if best is not None else self._best_objective()
        signal = self.plot_choice.currentData() or ""
        usable = [it for it in self._iterations if it.get("objective") is not None]

        if not signal:
            if not usable:
                self.tuning_plot.clear_trials()
                self._set_response_note("")
                return
            xs = [float(it["iteration"]) + 1 for it in usable]
            ys = [float(it["objective"]) for it in usable]
            running: list[float] = []
            current_best: float | None = None
            for value in ys:
                current_best = value if current_best is None else max(current_best, value)
                running.append(current_best)
            errors = [self._objective_error(it) for it in usable]
            self.tuning_plot.plot_trials(
                xs,
                ys,
                best=running,
                errors=errors if any(error > 0 for error in errors) else None,
                baseline=self._baseline,
                best_text=self._best_point_text(xs, ys),
                x_label="轮次",
                y_label=self._objective_axis_label(),
                source=self._plot_source_note(),
            )
            self._set_response_note("")
            return

        xs, ys = tuning_analysis.response_curve(self._iterations, signal)
        self.tuning_plot.set_axis_labels(self._row_label(signal), self._objective_axis_label())
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

    # ------------------------------------------------------------------
    # 图的标注 / 导出 / 过程日志
    # ------------------------------------------------------------------
    def _objective_axis_label(self) -> str:
        """纵轴标签：带单位的目标量名称（科学图必须写清量纲）。"""
        signal = str(self.target.currentData() or "")
        if not signal:
            return "目标量"
        for entry in self._mapping:
            if entry.get("signal") == signal:
                unit = str(entry.get("unit") or "")
                label = str(entry.get("label") or signal)
                return f"{label} ({unit})" if unit else label
        return signal

    @staticmethod
    def _objective_error(record: dict) -> float:
        """本轮目标测量的离散度（服务端给出采样离散度时才有值）。

        只有中位数而没有离散度是"看着精确"的假象：现场要判断一个改进是真提升
        还是噪声，必须看到误差棒。字段缺失时返回 0（不画误差棒，也不编一个数）。
        """
        for key in ("sample_spread", "objective_std"):
            value = record.get(key)
            if isinstance(value, (int, float)) and float(value) > 0:
                return float(value)
        samples = record.get("samples")
        if isinstance(samples, (list, tuple)) and len(samples) >= 2:
            try:
                import statistics

                return float(statistics.pstdev([float(item) for item in samples]))
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def _best_point_text(self, xs: list[float], ys: list[float]) -> str:
        if not ys:
            return ""
        index = max(range(len(ys)), key=ys.__getitem__)
        return f"最优 {ys[index]:.3f} @ 第 {xs[index]:g} 轮"

    def _plot_source_note(self) -> str:
        """图注：这张图是哪一次、什么算法、多少有效点画的。"""
        usable = sum(1 for it in self._iterations if it.get("objective") is not None)
        parts = []
        if self._algorithm:
            parts.append(str(self._algorithm))
        if self._seed is not None:
            parts.append(f"种子 {self._seed}")
        parts.append(f"有效 {usable}/{len(self._iterations)} 轮")
        if self._run_id:
            parts.append(f"run {str(self._run_id)[:8]}")
        return " · ".join(parts)

    def _export_plot(self, plot: SpectrumPlot, title: str) -> None:
        """导出当前图（PNG 高清 / SVG / PDF 矢量）。"""
        suggested = f"{title}-{str(self._run_id or 'run')[:8]}.png"
        chosen, _filter = QFileDialog.getSaveFileName(
            self,
            f"导出{title}",
            suggested,
            "PNG 图片 (*.png);;SVG 矢量 (*.svg);;PDF 文档 (*.pdf)",
        )
        if not chosen:
            return
        try:
            path = plot_style.export_plot(plot.view, chosen, scale=3)
        except Exception as exc:  # noqa: BLE001 —— 导出失败要说清楚，不能静默
            QMessageBox.critical(self, "导出失败", f"{exc}")
            return
        self._set_response_note(f"已导出：{path}")

    def _export_current_analysis(self) -> None:
        """导出当前分析视图：响应面矩阵是控件（多图容器），其余是单张曲线。"""
        if getattr(self, "analysis_current_choice", "") == "surface":
            self._export_widget(self.surface_container_widget, "响应面矩阵")
        else:
            self._export_plot(self.analysis_plot, "结果分析")

    def _export_widget(self, widget, title: str) -> None:
        chosen, _filter = QFileDialog.getSaveFileName(
            self,
            f"导出{title}",
            f"{title}-{str(self._run_id or 'run')[:8]}.png",
            "PNG 图片 (*.png);;SVG 矢量 (*.svg);;PDF 文档 (*.pdf)",
        )
        if not chosen:
            return
        try:
            path = plot_style.export_plot(widget, chosen, scale=2)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导出失败", f"{exc}")
            return
        self._set_response_note(f"已导出：{path}")

    def _format_event_time(self, raw: object) -> str:
        """把 ISO(UTC) 时间戳转成本地 ``HH:MM:SS.mmm``；无效值原样返回。"""
        text = str(raw or "")
        if not text:
            return "--:--:--.---"
        try:
            from datetime import datetime

            moment = datetime.fromisoformat(text)
            if moment.tzinfo is not None:
                moment = moment.astimezone()
            return moment.strftime("%H:%M:%S.") + f"{moment.microsecond // 1000:03d}"
        except (TypeError, ValueError):
            return text[:12]

    def _process_log_lines(self) -> list[str]:
        """过程日志行：一轮一行，带毫秒时间戳与级别。

        （第二批会把服务端事件流接进来，这里先用轮次记录给出同样的四段链路：
        建议 → 下发 → 回读 → 目标，并带上轮末时间戳。）
        """
        lines: list[str] = []
        for record in self._iterations:
            stage = tuning_analysis.stage_of(record)
            quality = str(record.get("quality") or "ok")
            level = "INFO" if quality == "ok" else "WARN"
            objective = record.get("objective")
            detail = str(record.get("detail") or "")
            lines.append(
                f"[{self._format_event_time(record.get('at'))}] {level:4s} "
                f"第 {int(record.get('iteration', 0)) + 1} 轮 · {stage} · "
                f"建议 {tuning_analysis._pairs(record.get('proposed') or {})} → "
                f"下发 {tuning_analysis._pairs(record.get('applied') or {})} → "
                f"回读 {tuning_analysis._pairs(record.get('readback') or {})} → "
                f"目标 " + ("--" if objective is None else f"{float(objective):.4f}")
                + (f"（{quality}：{detail}）" if detail else ("" if quality == "ok" else f"（{quality}）"))
            )
        for text in self._current_advice():
            lines.append(f"[{self._format_event_time(None)}] WARN 建议 · {text}")
        return lines

    def _fill_process_log(self) -> None:
        """把过程日志写进监控页的日志面板（增量追加，不整屏重排）。"""
        view = getattr(self, "event_log_view", None)
        if view is None:
            return
        lines = self._process_log_lines()
        if not lines:
            if not view.toPlainText():
                view.setPlainText("（还没有轮次记录）")
            return
        if len(lines) == getattr(self, "_log_lines_written", 0):
            return
        if len(lines) < getattr(self, "_log_lines_written", 0):
            # 轮次减少（换任务/复位）：整屏重画
            view.setPlainText("\n".join(lines))
        else:
            new_lines = lines[getattr(self, "_log_lines_written", 0):]
            if view.toPlainText().startswith("（还没有"):
                view.clear()
            view.appendPlainText("\n".join(new_lines))
        self._log_lines_written = len(lines)
        if getattr(self, "log_follow_check", None) is not None and self.log_follow_check.isChecked():
            scrollbar = view.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

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

    def _rebuild_analysis_choices(self, choices: list[tuple[str, str]]) -> None:
        """把分析视图选项灌进下拉（变量多时不再被一排按钮挤爆）。"""
        combo = getattr(self, "analysis_choice_combo", None)
        if combo is None:
            return
        current = getattr(self, "analysis_current_choice", "history") or "history"
        available = [data for _label, data in choices]
        if current not in available and available:
            # 上一轮的选择可能在新任务里已不存在（变量变了或数据不足）：
            # 回退到第一个可用的 **data**，而不是按钮文本——文本会让渲染分支永远对不上，
            # 于是"看起来切了视图，其实还是旧图"。
            current = available[0]
            self.analysis_current_choice = current
        combo.blockSignals(True)
        combo.clear()
        for label, data in choices:
            combo.addItem(label, data)
            combo.setItemData(combo.count() - 1, label, Qt.ItemDataRole.ToolTipRole)
        index = combo.findData(current)
        combo.setCurrentIndex(max(index, 0))
        combo.blockSignals(False)

    def _on_analysis_choice_changed(self) -> None:
        combo = getattr(self, "analysis_choice_combo", None)
        if combo is None:
            return
        self._set_analysis_choice(str(combo.currentData() or "history"))

    # ------------------------------------------------------------------
    # 响应面矩阵
    # ------------------------------------------------------------------
    def _surface_cell_size(self, n: int) -> int:
        """格子边长随视口自适应：变量多时缩小而不是溢出（原来固定 220×170 必然溢出）。"""
        viewport = self.surface_container.viewport()
        width = max(viewport.width(), 520)
        height = max(viewport.height(), 300)
        # 额外留一列给色条、一行给标签
        usable_w = width - 70 - 6 * (n + 1)
        usable_h = height - 26 - 6 * (n + 1)
        size = min(usable_w / max(1, n), usable_h / max(1, n))
        return int(max(104, min(200, size)))

    def _colorbar_strip(self, cmap, vmin: float, vmax: float, title: str, height: int) -> QWidget:
        """手绘色条：与矩阵使用同一张查找表，量程写在两端。

        没有色条的响应面只能看"形状"，看不出"差多少"——而且两个三角各有一套
        量程（预测值 / 不确定性），必须各自标出来，否则颜色会被误读成同一个尺度。
        """
        import numpy as np
        import pyqtgraph as pg

        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(QLabel(title, objectName="mutedText"))
        upper = QLabel(f"{vmax:.4g}", objectName="mutedText")
        layout.addWidget(upper)
        strip = pg.PlotWidget()
        strip.setFixedSize(46, max(60, height - 56))
        image = pg.ImageItem(np.linspace(0.0, 1.0, 256).reshape(256, 1))
        image.setLookupTable(cmap.getLookupTable(256))
        image.setRect(QRectF(0.0, 0.0, 1.0, 1.0))
        strip.addItem(image)
        strip.hideAxis("bottom")
        strip.hideAxis("left")
        strip.setXRange(0.0, 1.0, padding=0.0)
        strip.setYRange(0.0, 1.0, padding=0.0)
        layout.addWidget(strip)
        layout.addWidget(QLabel(f"{vmin:.4g}", objectName="mutedText"))
        layout.addStretch()
        return host

    def _render_surface(self) -> None:
        """响应面矩阵：左下=GP 预测均值（含等高线），右上=不确定性。

        与旧版的区别：格子按视口自适应、两个三角各配一条色条、叠加等高线与最优
        参数十字、双击可放大单格。旧版每格固定 220×170 且各画各的量程——
        变量一多就溢出，而且颜色无法横向比较。
        """
        import numpy as np
        import pyqtgraph as pg

        data = (self._analysis or {}).get("surface") or {}
        params = data.get("params") or []
        bounds = data.get("bounds") or []
        pairs = data.get("pairs") or {}
        points = data.get("points") or []
        best_params = data.get("best") or []
        grid = int(data.get("grid") or 0)
        n = len(params)
        while self.surface_grid.count():
            item = self.surface_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if n < 2:
            self.analysis_note.setText((data.get("reason") or "暂无可画的响应面数据"))
            return

        labels = [self._row_label(name) for name in params]
        cmap_mean = pg.colormap.get("viridis")
        cmap_std = pg.colormap.get("plasma")
        lut_mean = cmap_mean.getLookupTable(256)
        lut_std = cmap_std.getLookupTable(256)
        cell = self._surface_cell_size(n)

        # 统一量程：同一三角内所有格子共用一个色标（否则颜色不可比）
        means = [np.asarray(pair["mean"], dtype=float) for pair in pairs.values()]
        stds = [np.asarray(pair["std"], dtype=float) for pair in pairs.values()]
        mean_range = (
            (float(min(arr.min() for arr in means)), float(max(arr.max() for arr in means)))
            if means else (0.0, 1.0)
        )
        std_range = (
            (float(min(arr.min() for arr in stds)), float(max(arr.max() for arr in stds)))
            if stds else (0.0, 1.0)
        )

        for row in range(n):
            for column in range(n):
                if row == column:
                    lab = QLabel(labels[row], alignment=Qt.AlignmentFlag.AlignCenter)
                    lab.setFixedSize(cell, cell)
                    lab.setWordWrap(True)
                    self.surface_grid.addWidget(lab, row, column)
                    continue
                i, j = min(row, column), max(row, column)
                lo_x, hi_x = bounds[column]
                lo_y, hi_y = bounds[row]
                is_mean = row > column
                pair = pairs.get(f"{i}_{j}")
                pw = pg.PlotWidget()
                pw.setFixedSize(cell, cell)
                pw.setXRange(lo_x, hi_x, padding=0.0)
                pw.setYRange(lo_y, hi_y, padding=0.0)
                pw.showGrid(x=False, y=False)
                pw.getAxis("bottom").setStyle(showValues=(row == n - 1), tickLength=-4)
                pw.getAxis("left").setStyle(showValues=(column == 0), tickLength=-4)
                pw.setMouseEnabled(x=False, y=False)
                if pair:
                    arr = np.asarray(pair["mean" if is_mean else "std"], dtype=float)
                    low, high = mean_range if is_mean else std_range
                    if high <= low:
                        high = low + 1e-12
                    image = pg.ImageItem((arr - low) / (high - low))
                    image.setRect(QRectF(lo_x, lo_y, hi_x - lo_x, hi_y - lo_y))
                    image.setOpts(origin="lower", axisOrder="row-major")
                    image.setLookupTable(lut_mean if is_mean else lut_std)
                    image.setLevels((0.0, 1.0))
                    pw.addItem(image)
                    if is_mean and grid >= 3:
                        # 等高线：把"坡"画出来，比纯色块更容易读出峰值位置
                        try:
                            levels = np.linspace(arr.min(), arr.max(), 7)[1:-1]
                            for level in levels:
                                curve = pg.IsocurveItem(level=float(level), pen=pg.mkPen((255, 255, 255, 90), width=1))
                                curve.setData(arr)
                                from PySide6.QtGui import QTransform

                                curve.setTransform(
                                    QTransform()
                                    .translate(lo_x, lo_y)
                                    .scale(
                                        (hi_x - lo_x) / max(1, arr.shape[1] - 1),
                                        (hi_y - lo_y) / max(1, arr.shape[0] - 1),
                                    )
                                )
                                pw.addItem(curve)
                        except Exception:
                            pass  # 等高线是增强项：画不出来也要留下面板
                sx = [point[j] for point in points]
                sy = [point[i] for point in points]
                pw.addItem(pg.ScatterPlotItem(
                    x=sx, y=sy, size=4, symbol="o",
                    brush=pg.mkBrush(255, 255, 255, 170), pen=None,
                ))
                if len(best_params) > max(i, j):
                    pw.addItem(pg.InfiniteLine(
                        pos=float(best_params[j]), angle=90,
                        pen=pg.mkPen((255, 255, 255, 150), width=1, style=Qt.PenStyle.DashLine),
                    ))
                    pw.addItem(pg.InfiniteLine(
                        pos=float(best_params[i]), angle=0,
                        pen=pg.mkPen((255, 255, 255, 150), width=1, style=Qt.PenStyle.DashLine),
                    ))
                if pair:
                    pw.setToolTip(
                        f"{labels[column]} × {labels[row]}\n"
                        f"{'GP 预测均值' if is_mean else '预测不确定性（std）'}\n"
                        f"范围 [{arr.min():.4g}, {arr.max():.4g}]\n"
                        "双击放大（其余维度固定在最优值）"
                    )
                    pw.scene().sigMouseClicked.connect(
                        lambda _event, a=i, b=j, mean=is_mean: self._open_surface_pair(a, b, mean)
                    )
                self.surface_grid.addWidget(pw, row, column)

        # 两条色条：均值（左下三角）与不确定性（右上三角）
        self.surface_grid.addWidget(
            self._colorbar_strip(cmap_mean, mean_range[0], mean_range[1], "预测均值", cell),
            n - 1, n,
        )
        self.surface_grid.addWidget(
            self._colorbar_strip(cmap_std, std_range[0], std_range[1], "不确定性", cell),
            0, n,
        )
        self.analysis_note.setText(
            "左下三角 = GP 预测均值（含等高线，色条在右侧），右上三角 = 不确定性"
            "（暖色=未探索），白点 = 实际采样点，虚线十字 = 当前最优点；"
            "双击任一格放大。多个采样阶段混在一个 study 里时，矩阵只应作为趋势参考。"
            + (data.get("note") or "")
        )

    def _open_surface_pair(self, i: int, j: int, is_mean: bool) -> None:
        """单格放大：小矩阵里读不出细节，放大后能看清峰位与陡峭程度。"""
        import numpy as np
        import pyqtgraph as pg

        data = (self._analysis or {}).get("surface") or {}
        pair = (data.get("pairs") or {}).get(f"{i}_{j}")
        params = data.get("params") or []
        bounds = data.get("bounds") or []
        if not pair or len(bounds) <= max(i, j):
            return
        arr = np.asarray(pair["mean" if is_mean else "std"], dtype=float)
        dialog = QDialog(self)
        dialog.setWindowTitle(
            f"响应面 · {self._row_label(params[j])} × {self._row_label(params[i])}"
        )
        layout = QVBoxLayout(dialog)
        plot = pg.PlotWidget()
        plot.setMinimumSize(560, 420)
        plot_style.apply_science_style(
            plot,
            x_label=self._row_label(params[j]),
            y_label=self._row_label(params[i]),
            mirrored_axes=False,
        )
        image = pg.ImageItem(arr)
        lo_x, hi_x = bounds[j]
        lo_y, hi_y = bounds[i]
        image.setRect(QRectF(lo_x, lo_y, hi_x - lo_x, hi_y - lo_y))
        image.setOpts(origin="lower", axisOrder="row-major")
        cmap = pg.colormap.get("viridis" if is_mean else "plasma")
        image.setLookupTable(cmap.getLookupTable(256))
        plot.addItem(image)
        bar = pg.ColorBarItem(
            values=(float(arr.min()), float(arr.max())),
            colorMap=cmap,
            label="GP 预测均值" if is_mean else "预测不确定性 (std)",
        )
        bar.setImageItem(image, insert_in=plot.getPlotItem())
        layout.addWidget(plot)
        close = QPushButton("关闭")
        close.clicked.connect(dialog.accept)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close)
        layout.addLayout(row)
        dialog.exec()

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
        surface = (self._analysis or {}).get("surface") or {}
        if surface.get("available"):
            choices.append(("响应面矩阵", "surface"))
        self._rebuild_analysis_choices(choices)

    def _clear_result_analysis_items(self) -> None:
        for item in self._analysis_items:
            self.analysis_plot.view.removeItem(item)
        self._analysis_items.clear()
        self.analysis_plot.set_series([])
        self.analysis_plot.clear_trials()
        for axis in ("bottom", "left"):
            self.analysis_plot.view.getAxis(axis).setTicks(None)
        self.analysis_plot.view.getAxis("bottom").setLabel("轮次")
        self.analysis_plot.view.getAxis("left").setLabel(self._objective_axis_label())

    def _render_result_analysis(self, *_args) -> None:
        """渲染历史 / 重要性 / 切片（论文式样式：轴标签带单位、标注最优、图注写来源）。"""
        import numpy as np
        import pyqtgraph as pg

        self._clear_result_analysis_items()
        choice = getattr(self, "analysis_current_choice", "history") or "history"
        is_surface = choice == "surface"
        self.analysis_plot.setVisible(not is_surface)
        self.surface_container.setVisible(is_surface)
        if is_surface:
            self._render_surface()
            return

        if choice == "importance":
            values = ((self._analysis or {}).get("importance") or {}).get("values") or {}
            names = list(values)
            scores = [float(values[name]) for name in names]
            if not scores:
                # 数据不足时不要画一张空图配一段"已计算 0 个参数"的话：
                # 直接说清为什么没有，比留白更省操作员的时间。
                reason = ((self._analysis or {}).get("importance") or {}).get("reason")
                self.analysis_note.setText(
                    reason or "还没有重要性数据：有效 Trial 不足或分析尚未加载。"
                )
                return
            order = np.argsort(scores)
            ranked = [names[index] for index in order]
            ranked_scores = [scores[index] for index in order]
            # 横向条形图：参数名写在纵轴刻度上，长的中文名不会被斜着挤
            bars = pg.BarGraphItem(
                x0=[0.0] * len(ranked),
                y=[float(index) for index in range(len(ranked))],
                width=ranked_scores,
                height=0.62,
                brush=plot_style.series_style()["model"]["pen"].color(),
                pen=pg.mkPen(current_palette()["surfacePanel"], width=1),
            )
            self.analysis_plot.view.addItem(bars)
            self._analysis_items.append(bars)
            for index, (name, score) in enumerate(zip(ranked, ranked_scores)):
                label = pg.TextItem(f"{score:.3f}", color=current_palette()["textBase"],
                                    anchor=(0.0, 0.5))
                label.setPos(score, float(index))
                self.analysis_plot.view.addItem(label)
                self._analysis_items.append(label)
            self.analysis_plot.view.getAxis("left").setTicks(
                [[(index, self._row_label(name)) for index, name in enumerate(ranked)]]
            )
            self.analysis_plot.view.setXRange(0.0, max(ranked_scores) * 1.18, padding=0.0)
            self.analysis_plot.view.setYRange(-0.6, len(ranked) - 0.4, padding=0.0)
            self.analysis_plot.view.setLabel("bottom", "重要性（FANOVA）")
            self.analysis_plot.view.setLabel("left", "参数")
            count = ((self._analysis or {}).get("n_complete") or 0)
            method = ((self._analysis or {}).get("importance") or {}).get("method", "")
            trusted = count >= max(10, 3 * max(1, len(names)))
            self.analysis_note.setText(
                f"基于 {count} 个有效 Trial 计算 {len(names)} 个参数的重要性"
                f"（{method or '未记录方法'}）。"
                + ("" if trusted else "有效 Trial 数偏少，排名只能作为方向参考，不要当作定论。")
            )
            return

        if choice.startswith("slice:"):
            name = choice.split(":", 1)[1]
            points = ((self._analysis or {}).get("slice") or {}).get(name) or []
            xs = np.asarray([float(point[0]) for point in points], dtype=float)
            ys = np.asarray([float(point[1]) for point in points], dtype=float)
            if xs.size:
                style = plot_style.series_style()
                scatter = pg.ScatterPlotItem(
                    x=xs, y=ys, size=7, symbol="o",
                    brush=style["current"]["brush"],
                    pen=pg.mkPen(current_palette()["surfacePanel"], width=0.8),
                )
                self.analysis_plot.view.addItem(scatter)
                self._analysis_items.append(scatter)
                # 局部均值 + 离散带：单看散点很难判断"是趋势还是噪声"
                window = max(2, min(5, xs.size // 4 or 2))
                if xs.size >= 3:
                    smooth_x, smooth_y, band = self._rolling_summary(xs, ys, window)
                    mean_curve = self.analysis_plot.view.plot(
                        smooth_x, smooth_y, pen=style["model"]["pen"]
                    )
                    self._analysis_items.append(mean_curve)
                    upper = self.analysis_plot.view.plot(smooth_x, smooth_y + band,
                                                         pen=pg.mkPen(None))
                    band_color = style["model"]["pen"].color().lighter(160)
                    band_color.setAlpha(50)
                    lower = self.analysis_plot.view.plot(
                        smooth_x, smooth_y - band,
                        pen=pg.mkPen(None), fillLevel=0.0,
                        brush=pg.mkBrush(band_color),
                    )
                    self._analysis_items.extend([upper, lower])
                best_index = int(np.argmax(ys))
                marker, label = plot_style.best_point_marker(
                    float(xs[best_index]), float(ys[best_index]),
                    f"最优 {ys[best_index]:.3f} @ {xs[best_index]:g}",
                )
                self.analysis_plot.view.addItem(marker)
                self._analysis_items.append(marker)
                if label is not None:
                    self.analysis_plot.view.addItem(label)
                    self._analysis_items.append(label)
                self.analysis_plot.view.addItem(
                    pg.InfiniteLine(
                        pos=float(xs[best_index]), angle=90,
                        pen=pg.mkPen(current_palette()["plotBest"], width=1,
                                     style=Qt.PenStyle.DashLine),
                    )
                )
            self.analysis_plot.view.setLabel("bottom", self._row_label(name))
            self.analysis_plot.view.setLabel("left", self._objective_axis_label())
            self.analysis_note.setText(
                f"共 {len(points)} 个切片样本点；橙线为局部均值（窗口 {max(2, min(5, len(points) // 4 or 2))} 点），"
                "淡带为局部离散度。切片用的是算法建议值，不是设备回读。"
            )
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
        if not ys:
            self.analysis_note.setText("还没有可用轮次：任务跑起来后这里会显示爬山图。")
            return
        errors = [self._objective_error(it) for it in (self._iterations or [])]
        usable_count = len(ys)
        # 误差棒只在"逐轮记录与图上的点一一对应"时才画：分析数据来自 Optuna trial
        # 投影时长度可能不等于本地轮次，硬拼会把误差棒挂到错误的点上。
        error_bars = errors if len(errors) == usable_count and any(errors) else None
        self.analysis_plot.plot_trials(
            xs,
            ys,
            best=best,
            errors=error_bars,
            baseline=self._baseline,
            best_text=self._best_point_text(xs, ys),
            x_label="Trial / 轮次",
            y_label=self._objective_axis_label(),
            source=self._plot_source_note(),
        )
        self.analysis_note.setText(
            f"共 {usable_count} 个有效点：散点为单轮测量"
            + ("（带误差棒）" if error_bars else "（服务端未提供采样离散度，暂不画误差棒）")
            + "，阶梯线为历史最优；横线为启动前基线（如果读到）。"
            + f"  {self._plot_source_note()}"
        )

    @staticmethod
    def _rolling_summary(
        xs, ys, window: int
    ) -> tuple["np.ndarray", "np.ndarray", "np.ndarray"]:
        """按 x 排序后的局部均值与离散度（切片图的趋势线）。

        不用 GP 拟合：客户端不该为了画一张趋势线引入重量级依赖；局部均值能诚实
        表达"附近这几点的平均水平"，窗口大小写进图注，不冒充模型预测。

        返回 numpy 数组而不是 list：调用方要算 ``mean ± band``，
        list 的 ``+`` 是拼接，会把两个长度 3 的序列拼成长度 6 再把图画崩。
        """
        import numpy as np

        order = np.argsort(xs)
        sorted_x = np.asarray(xs, dtype=float)[order]
        sorted_y = np.asarray(ys, dtype=float)[order]
        mean_x: list[float] = []
        mean_y: list[float] = []
        band: list[float] = []
        for index in range(sorted_x.size):
            lo = max(0, index - window // 2)
            hi = min(sorted_x.size, lo + window)
            window_y = sorted_y[lo:hi]
            mean_x.append(float(sorted_x[index]))
            mean_y.append(float(window_y.mean()))
            band.append(float(window_y.std()) if window_y.size > 1 else 0.0)
        return (
            np.asarray(mean_x, dtype=float),
            np.asarray(mean_y, dtype=float),
            np.asarray(band, dtype=float),
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
        """把轮次记录与建议渲染成日志文本（带毫秒时间戳、耗时与采样离散度）。"""
        lines = tuning_analysis.summary_lines(
            self._iterations,
            target_signal=str(self.target.currentData() or ""),
            best_iteration=self._best_iteration(),
        )
        lines += tuning_analysis.log_lines(
            self._iterations,
            target_signal=str(self.target.currentData() or ""),
            advice_lines=self._current_advice(),
            timestamps=True,
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
                "events": len(self._events),
            }
        ]
        records += [dict(record) for record in self._iterations]
        # 过程事件全量导出：只导轮次记录的话，"为什么这轮慢/为什么被拒"复盘不了
        records += [
            {"event": "trace", **event} for event in self._events
        ]
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
    # 认领在跑的任务（客户端重启后仍能看到并停止它）
    # ------------------------------------------------------------------
    def _adopt_active_run(self) -> None:
        """查一次任务列表；发现非终态任务就接管（含启动轮询与停止入口）。

        不接管会怎样：客户端重启后 `_run_id` 为空，界面显示"尚未开始"，
        而设备正被一个看不见的任务写着——既没有进度也没有停止按钮。
        """
        if self._run_id is not None or self._adopt_attempted:
            return
        if not hasattr(instrument_api, "request_tuning_runs"):
            return
        self._adopt_attempted = True
        try:
            thread = instrument_api.request_tuning_runs()
            thread.completed.connect(self._on_runs_listed)
        except Exception:  # noqa: BLE001 —— 认领失败不该影响页面本身
            return

    def _on_runs_listed(self, payload: dict) -> None:
        if not payload.get("ok"):
            return
        runs = list((payload.get("payload") or {}).get("runs") or [])
        active = next(
            (run for run in runs if str(run.get("state")) not in TERMINAL_STATES),
            None,
        )
        if active is None:
            return
        self._run_id = str(active.get("run_id"))
        self._state = str(active.get("state") or "running")
        self.tabs.setTabEnabled(1, True)
        self.tabs.setTabEnabled(2, True)
        self.tabs.setCurrentIndex(1)
        self.tuning_pause_button.setEnabled(True)
        self.tuning_stop_button.setEnabled(True)
        self.tuning_state.setText(
            f"{STATE_TEXT.get(self._state, self._state)} · 由其它会话启动，本界面已接管"
        )
        self.start_button.setEnabled(False)
        # 立刻补齐一次状态、轮次与事件，界面不会停在"尚未开始"
        self._poll()
        self._timer.start()

    # ------------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._apply_read_only()
        if self.is_operation_active():
            self._poll()
            self._timer.start()
        else:
            self._adopt_active_run()

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
