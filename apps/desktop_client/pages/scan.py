"""扫谱页面：驱动执行服务的真实扫描任务，实时显示采集到的谱图。

与旧版的三点不同（旧版在页面内用公式造数据）：

1. **界面不采集**。点「开始扫描」只是把参数提交给执行服务，真正的逐点流程
   （写设定值 → 等回读稳定 → 采样 → 落盘）在服务端跑；界面只轮询状态与增量点。
   这样关掉窗口不会中断实验，也符合「执行服务是硬件唯一入口」的架构约定。
2. **横坐标是实际回读值**。谱图的 x 是每个点设备**实际**到达的位置，
   不是下发过的设定值——设定值只是意图（文档 6.5）。
3. **暂停按钮不开放**。文档 9.3 明确「只有支持安全暂停和恢复后才开放暂停按钮」。
   扫磁铁电流时暂停意味着让磁场停在某个电流上，现场未确认前不提供该能力，
   按钮置灰并说明原因。

质量不合格的点（回读未稳定、探测掉线）由服务端标注并排除在谱图之外，
界面在状态条上如实显示，不把它们画成好数据。
"""

from __future__ import annotations

import json
from datetime import datetime

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from apps.desktop_client import instrument_api
from apps.desktop_client.pages.common import page_layout, primary_button
from apps.desktop_client.pages.registry import PageSpec
from apps.desktop_client.spectrum_plot import SpectrumPlot
from apps.desktop_client.widgets import PageHeading, Panel

# 扫描轴：单路或成组同步（与手动控制页的联动分组一致）
MAGNET_AXES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("磁铁1 电流", ("magnet.m1.current_setpoint",), "magnet.m1.current_readback"),
    ("磁铁2 电流", ("magnet.m2.current_setpoint",), "magnet.m2.current_readback"),
    ("磁铁3 电流", ("magnet.m3.current_setpoint",), "magnet.m3.current_readback"),
    ("磁铁4 电流", ("magnet.m4.current_setpoint",), "magnet.m4.current_readback"),
    (
        "磁铁1+2 同步",
        ("magnet.m1.current_setpoint", "magnet.m2.current_setpoint"),
        "magnet.m1.current_readback",
    ),
    (
        "磁铁3+4 同步",
        ("magnet.m3.current_setpoint", "magnet.m4.current_setpoint"),
        "magnet.m3.current_readback",
    ),
    (
        "磁铁1~4 同步",
        (
            "magnet.m1.current_setpoint",
            "magnet.m2.current_setpoint",
            "magnet.m3.current_setpoint",
            "magnet.m4.current_setpoint",
        ),
        "magnet.m1.current_readback",
    ),
)

DETECTORS: tuple[tuple[str, str], ...] = (
    ("FC1 法拉第杯", "detector.fc1.beam_current"),
    ("FC2 法拉第杯", "detector.fc2.beam_current"),
)

# demo PV清单.md 的 Mass-电流标定系数（暂定值，可改后用「应用」刷新显示）
DEFAULT_MASS_COEFFICIENTS = ("-1.08608", "0.02743", "0.01722", "-1.9947e-07")

# 与执行服务 MAX_SCAN_POINTS 保持一致，本地先拦一次省一次往返
MAX_POINTS = 20000
# 状态轮询间隔：扫谱是慢过程，400ms 足够，也不至于刷爆服务
POLL_INTERVAL_MS = 400
# 自动回落速率上限（demo 现场约定：停止扫描后回落速度 ≤10 A/s）
MAX_RETRACT_RATE_A_PER_S = 10.0

# 导出文件名的 Windows 非法字符：元素是自由文本框，操作员很可能写 "Al/Cu"。
# 不净化的话要到「保存」那一步才报错，那时目录都选好了，白折腾一次。
ILLEGAL_FILENAME_CHARS = '<>:"/\\|?*'
# 远小于 NTFS 单段 255：给序号、扩展名和用户自己加的后缀留余量
MAX_FILENAME_LEN = 120

# 导出时抓取的现场快照，按设备分组只为文件里好读。**只允许放
# apps/instrument_service/device_profiles.py 里真实存在的信号名**：服务端按名
# 逐条查映射，写进去一个不存在的名字会让整批读取失败（HTTP 500），一个读数都拿不回。
# 键是导出 JSON 里 device_snapshot 的分组名。
EXPORT_SNAPSHOT_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "gas",
        (
            "gas.ar.flow_setpoint",
            "gas.ar.flow_readback",
            "gas.he.flow_setpoint",
            "gas.he.flow_readback",
        ),
    ),
    ("vacuum", ("vacuum.chamber_pressure",)),
    (
        "sputter",
        (
            "sputter.power_setpoint",
            "sputter.power_readback",
            "sputter.arc_rate_readback",
        ),
    ),
    (
        "ion_optics",
        tuple(
            f"ion_optics.{key}.{suffix}"
            for key in ("focus", "drift")
            for suffix in ("voltage_setpoint", "voltage_readback")
        ),
    ),
    (
        "hv_array",
        tuple(
            f"hv_array.dw{channel:02d}.{suffix}"
            for channel in range(1, 14)
            for suffix in ("voltage_setpoint", "voltage_readback")
        ),
    ),
    (
        "hv_bd",
        tuple(
            f"hv_bd.{key}.{suffix}"
            for key in ("cylinder1", "cylinder2", "deflector1", "deflector2", "main")
            for suffix in ("voltage_setpoint", "voltage_readback")
        )
        # 主高压额外有电流设定/回读，其余 BD 只有一路电流回读，缺的那路不编
        + ("hv_bd.main.current_setpoint", "hv_bd.main.current_readback"),
    ),
    ("detector", ("detector.fc1.beam_current", "detector.fc2.beam_current")),
)

# 一次批量读要发出去的全部信号（展平，顺序稳定便于与文件里的字段对照）
EXPORT_SIGNALS: tuple[str, ...] = tuple(
    signal for _group, signals in EXPORT_SNAPSHOT_SIGNALS for signal in signals
)

TERMINAL_STATES = frozenset(
    {"completed", "aborted", "failed", "recovery_required"}
)
STATE_TEXT = {
    "draft": "待提交",
    "validating": "校验参数",
    "preparing": "准备设备",
    "running": "扫描进行中",
    "stop_requested": "停止中",
    "completing": "保存数据",
    "completed": "扫描完成",
    "aborted": "已停止",
    "failed": "扫描失败",
    "recovery_required": "需人工确认设备状态",
}


def _mass(value: float, coefficients: list[float]) -> float:
    """按 a0 + a1·I + a2·I² + a3·I³ 换算质量；仅用于显示。"""
    total = 0.0
    for power, coefficient in enumerate(coefficients):
        total += coefficient * (value**power)
    return total


def _reading_value(readings: dict[str, dict], signal: str) -> float | None:
    """取一路快照读数；未配置、未连接、值不是数一律给 None。

    导出侧只认「拿到的数」：读不到就如实留空，不能退回上一次的值——
    上一次可能是几分钟前另一个工况的数，写进文件会被当成这次扫描的条件。
    """
    value = (readings.get(signal) or {}).get("value")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _retract_failure_text(message: str) -> str:
    """回落失败原因：409 的错误体取不到时只剩状态码，这里补一句能照做的原因。"""
    text = str(message or "").strip() or "服务未返回原因"
    if "409" in text:
        return f"回落被拒：设备组被其他任务占用（{text}），请先停止或确认占用该设备组的任务"
    return f"回落失败：{text}"


def _applied_text(applied: dict) -> str:
    """把逐路回读压成一行（m1=0.00，m2=0.01）：现场靠它核对是哪一路没到位。"""
    parts: list[str] = []
    for signal, value in applied.items():
        try:
            number = f"{float(value):.2f}"
        except (TypeError, ValueError):
            number = str(value)
        # 信号名形如 <命名空间>.<设备>.<量>，取中间段当短名；取不到就原样显示
        pieces = str(signal).split(".")
        parts.append(f"{pieces[-2] if len(pieces) > 2 else signal}={number}")
    return "，".join(parts)


def _safe_filename(name: str) -> str:
    """净化 Windows 非法字符并限长；净化后为空时给个兜底名，不让导出抛异常。"""
    cleaned = "".join("_" if char in ILLEGAL_FILENAME_CHARS else char for char in name)
    cleaned = cleaned.strip().strip(".")
    return (cleaned or "scan")[:MAX_FILENAME_LEN]


class ScanPage(QWidget):
    """扫谱页：提交任务、跟踪进度、显示实时采集到的谱图。"""

    def __init__(self) -> None:
        super().__init__()
        self._run_id: str | None = None
        self._state = "idle"
        self._points: list[dict] = []
        self._since = 0
        self._status_in_flight = False
        self._points_in_flight = False
        self._start_in_flight = False
        # 终态与「点已拉全」是两件事：服务端把最后几个点写完才置终态，
        # 而上一次轮询可能发生在写入之前。必须补齐再停轮询。
        self._pending_terminal = False
        self._points_total = 0
        # 最近一次状态快照：导出要写采集起止时间，那是服务端记的，界面不能自己造
        self._status: dict = {}
        self._retract_in_flight = False
        self._export_in_flight = False
        self._export_fallback_used = False
        self._export_format = "json"

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)

        layout = page_layout(self)
        layout.addWidget(
            PageHeading(
                "扫谱控制",
                "参数提交给仪器执行服务执行，界面只跟踪进度。横坐标为磁铁电流的"
                "实际回读值；勾选质量换算仅为显示，落盘保存的始终是电流。",
            )
        )
        layout.addWidget(self._build_controls())
        layout.addWidget(self._build_retract_row())
        layout.addWidget(self._build_export_row())

        plot_panel = Panel("实时谱图", "曲线优先")
        self.plot = SpectrumPlot("磁铁电流 I / A", "法拉第杯电流 / nA")
        plot_panel.body.addWidget(self.plot, 1)
        plot_panel.body.addWidget(self._build_plot_tools())
        plot_panel.body.addWidget(self._build_status_bar())
        layout.addWidget(plot_panel, 1)
        self._update_scan_summary()

    # ------------------------------------------------------------------
    # 界面骨架
    # ------------------------------------------------------------------
    def _build_controls(self) -> QFrame:
        controls = QFrame(objectName="panel")
        outer = QVBoxLayout(controls)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.axis = QComboBox()
        self.axis.addItems([label for label, _signals, _rb in MAGNET_AXES])
        self.axis.setMinimumWidth(150)
        row.addWidget(self._field("扫描轴", self.axis))
        self.detector = QComboBox()
        self.detector.addItems([label for label, _signal in DETECTORS])
        row.addWidget(self._field("探测器", self.detector))

        # 电流量程取现场上限 600 A
        self.start_value = self._double_spin(100.0, 0.0, 600.0, 10.0, " A")
        self.end_value = self._double_spin(200.0, 0.0, 600.0, 10.0, " A")
        self.step_value = self._double_spin(5.0, 0.1, 100.0, 1.0, " A", 2)
        self.dwell_value = self._double_spin(200.0, 0.0, 60000.0, 50.0, " ms")
        for label, widget in (
            ("起点", self.start_value),
            ("终点", self.end_value),
            ("步长", self.step_value),
            ("驻留", self.dwell_value),
        ):
            row.addWidget(self._field(label, widget))

        self.samples = QSpinBox()
        self.samples.setRange(1, 50)
        self.samples.setValue(3)
        row.addWidget(self._field("每点采样", self.samples))

        self.start_button = primary_button("开始扫描")
        self.start_button.clicked.connect(self.start_scan)
        row.addWidget(self.start_button)
        outer.addLayout(row)

        # 第二行：数据命名（元素/序号）
        # 这里原来还有一个「扫描速率」输入框，但它的值从来没进过契约：`ScanRunRequest`
        # 里没有点间速率这一项，真正决定点间斜坡的是映射里磁铁条目的 `max_rate`
        # （回落速率另有一项，在下面的回落行里）。留一个改了不生效的控件比没有更糟——
        # 操作员会以为扫描速度变了（改造报告 §8.8 第 1 条）。
        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(10)
        self.element_edit = QLineEdit("Ar")
        self.element_edit.setMaximumWidth(90)
        row2.addWidget(self._field("元素", self.element_edit))
        self.index_edit = QSpinBox()
        self.index_edit.setRange(0, 9999)
        self.index_edit.setValue(1)
        row2.addWidget(self._field("序号", self.index_edit))

        # X 轴显示：电流 / 质量（单选，贴近 demo 心智）
        x_axis_box = QWidget()
        x_axis_layout = QHBoxLayout(x_axis_box)
        x_axis_layout.setContentsMargins(0, 0, 0, 0)
        x_axis_layout.setSpacing(8)
        x_axis_layout.addWidget(QLabel("X 轴", objectName="mutedText"))
        self.x_axis_group = QButtonGroup(self)
        self.rb_current = QRadioButton("电流 I (A)")
        self.rb_mass = QRadioButton("质量 Mass (u)")
        self.rb_current.setChecked(True)
        self.x_axis_group.addButton(self.rb_current)
        self.x_axis_group.addButton(self.rb_mass)
        self.rb_mass.toggled.connect(lambda _on: self._redraw())
        # 兼容旧测试/外部引用：mass_toggle 指向「质量」单选钮
        self.mass_toggle = self.rb_mass
        x_axis_layout.addWidget(self.rb_current)
        x_axis_layout.addWidget(self.rb_mass)
        row2.addWidget(x_axis_box)

        summary_row = QHBoxLayout()
        summary_row.setContentsMargins(0, 0, 0, 0)
        summary_row.setSpacing(10)
        self.mass_settings_button = QPushButton("展开质量标定")
        self.mass_settings_button.setCheckable(True)
        summary_row.addWidget(self.mass_settings_button)
        self.scan_summary = QLabel(objectName="mutedText")
        self.scan_summary.setWordWrap(True)
        summary_row.addWidget(self.scan_summary, 1)
        outer.addLayout(row2)
        outer.addLayout(summary_row)

        self.mass_details = QWidget()
        mass_row = QHBoxLayout(self.mass_details)
        mass_row.setContentsMargins(0, 2, 0, 0)
        mass_row.setSpacing(6)
        mass_row.addWidget(QLabel("Mass =", objectName="mutedText"))
        self.coefficient_edits: list[QLineEdit] = []
        powers = ("", "·I", "·I²", "·I³")
        for index, default in enumerate(DEFAULT_MASS_COEFFICIENTS):
            if index:
                mass_row.addWidget(QLabel("+", objectName="mutedText"))
            edit = QLineEdit(default)
            edit.setMaximumWidth(92)
            edit.editingFinished.connect(self._redraw)
            self.coefficient_edits.append(edit)
            mass_row.addWidget(edit)
            if powers[index]:
                mass_row.addWidget(QLabel(powers[index], objectName="mutedText"))
        apply_mass = QPushButton("应用", objectName="rowButton")
        apply_mass.setFixedWidth(50)
        apply_mass.clicked.connect(self._redraw)
        mass_row.addWidget(apply_mass)
        mass_row.addStretch()
        self.mass_details.setVisible(False)
        self.mass_settings_button.toggled.connect(self._toggle_mass_details)
        outer.addWidget(self.mass_details)
        for spin in (self.start_value, self.end_value, self.step_value, self.dwell_value):
            spin.valueChanged.connect(self._update_scan_summary)
        self.samples.valueChanged.connect(self._update_scan_summary)
        return controls

    def _build_retract_row(self) -> QFrame:
        """停止后回落：扫描结束后自动把磁铁电流退到安全值（demo 现场约定）。"""
        box = QFrame(objectName="panel")
        layout = QHBoxLayout(box)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(10)
        layout.addWidget(QLabel("停止后回落", objectName="panelTitle"))

        self.retract_current = self._double_spin(0.0, 0.0, 600.0, 1.0, " A")
        layout.addWidget(self._field("回落电流", self.retract_current))
        self.retract_rate = self._double_spin(2.0, 0.1, MAX_RETRACT_RATE_A_PER_S, 0.5, " A/s")
        layout.addWidget(self._field("回落速率", self.retract_rate))

        self.auto_retract = QCheckBox("扫描结束自动回落")
        self.auto_retract.setChecked(True)
        self.auto_retract.setToolTip(
            "由执行服务在任务完成前执行：先写各路速率、再写各路电流、逐路等回读到位。"
            "回落跟着任务走，界面关掉或崩溃都会照常退到位。"
        )
        layout.addWidget(self.auto_retract)

        self.retract_button = QPushButton("执行回落")
        self.retract_button.setToolTip(
            "立即把当前扫描轴的全部设定值退到回落电流（同一组磁铁一起退，按回落速率）"
        )
        self.retract_button.clicked.connect(self._manual_retract)
        layout.addWidget(self.retract_button)

        self.retract_status = QLabel("就绪", objectName="mutedText")
        layout.addWidget(self.retract_status)
        layout.addStretch()
        return box

    def _build_export_row(self) -> QFrame:
        """数据导出：JSON 含设备现场快照 / TXT 导出原始电流与束流。命名规则对齐 demo。"""
        box = QFrame(objectName="panel")
        layout = QHBoxLayout(box)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(10)
        layout.addWidget(QLabel("数据导出", objectName="panelTitle"))

        self.export_hint = QLabel(
            "命名：元素-序号-Ar-He-气压Pa-PowerW-cm（取不到的值用 - 占位）；"
            "JSON 含扫描参数、设备快照与点列；TXT 为原始电流+束流，选 Mass 时另加一列质量",
            objectName="mutedText",
        )
        layout.addWidget(self.export_hint, 1)

        self.export_json_button = QPushButton("导出 JSON(全参数)")
        self.export_json_button.clicked.connect(lambda: self._export("json"))
        layout.addWidget(self.export_json_button)
        self.export_txt_button = QPushButton("导出 TXT(电流+束流)")
        self.export_txt_button.clicked.connect(lambda: self._export("txt"))
        layout.addWidget(self.export_txt_button)
        return box

    def _build_plot_tools(self) -> QWidget:
        """Y 轴手动范围（留空=自动）+ 导出 PNG。"""
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(QLabel("Y 轴范围", objectName="mutedText"))
        self.ymin_edit = QLineEdit()
        self.ymin_edit.setPlaceholderText("留空=自动")
        self.ymin_edit.setMaximumWidth(80)
        self.ymax_edit = QLineEdit()
        self.ymax_edit.setPlaceholderText("留空=自动")
        self.ymax_edit.setMaximumWidth(80)
        layout.addWidget(self.ymin_edit)
        layout.addWidget(QLabel("~", objectName="mutedText"))
        layout.addWidget(self.ymax_edit)
        apply_yrange = QPushButton("应用", objectName="rowButton")
        apply_yrange.setFixedWidth(50)
        apply_yrange.clicked.connect(self._apply_yrange)
        layout.addWidget(apply_yrange)
        layout.addStretch()
        export_png = QPushButton("导出 PNG", objectName="plotToolButton")
        export_png.clicked.connect(self._export_png)
        layout.addWidget(export_png)
        return bar

    def _build_status_bar(self) -> QWidget:
        status = QWidget()
        row = QHBoxLayout(status)
        row.setContentsMargins(0, 3, 0, 0)
        row.setSpacing(12)
        self.state_label = QLabel("等待开始", objectName="mutedText")
        self.current_label = QLabel("当前 I：--")
        self.intensity_label = QLabel("当前强度：--")
        self.peak_label = QLabel("最大峰：--")
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(170)

        # 暂停按文档 9.3 关闭：未确认现场允许「让磁场停在某电流上」之前不提供
        self.pause_button = QPushButton("暂停")
        self.pause_button.setEnabled(False)
        self.pause_button.setToolTip(
            "暂停需要设备支持安全暂停与恢复（架构文档 9.3）。"
            "扫磁铁电流时暂停会让磁场停在当前电流上，现场确认可用前不开放；"
            "请使用「安全停止」，停止后会自动回落。"
        )
        self.acknowledge_button = QPushButton("确认设备状态")
        self.acknowledge_button.setVisible(False)
        self.acknowledge_button.setToolTip(
            "上一次扫描在下发中断中结束，设备实际状态未知，该设备组仍被锁定。"
            "现场核对后点击此处释放锁。"
        )
        self.acknowledge_button.clicked.connect(self._acknowledge)
        self.stop_button = QPushButton("安全停止", objectName="dangerButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_scan)

        for widget in (
            self.state_label,
            self.current_label,
            self.intensity_label,
            self.peak_label,
        ):
            row.addWidget(widget)
        row.addStretch()
        row.addWidget(self.progress)
        row.addWidget(self.acknowledge_button)
        row.addWidget(self.pause_button)
        row.addWidget(self.stop_button)
        return status

    def _toggle_mass_details(self, visible: bool) -> None:
        self.mass_details.setVisible(visible)
        self.mass_settings_button.setText("收起质量标定" if visible else "展开质量标定")

    @staticmethod
    def _field(label: str, widget: QWidget) -> QWidget:
        field = QWidget()
        layout = QVBoxLayout(field)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(QLabel(label, objectName="mutedText"))
        layout.addWidget(widget)
        return field

    @staticmethod
    def _double_spin(
        value: float,
        minimum: float,
        maximum: float,
        step: float,
        suffix: str,
        decimals: int = 2,
    ) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setSingleStep(step)
        spin.setSuffix(suffix)
        spin.setValue(value)
        return spin

    # ------------------------------------------------------------------
    # 参数
    # ------------------------------------------------------------------
    def _coefficients(self) -> list[float]:
        values: list[float] = []
        for edit in self.coefficient_edits:
            try:
                values.append(float(edit.text()))
            except ValueError:
                values.append(0.0)
        return values

    def _axis_definition(self) -> tuple[str, tuple[str, ...], str]:
        return MAGNET_AXES[max(0, self.axis.currentIndex())]

    def _build_request(self) -> dict:
        label, setpoints, readback = self._axis_definition()
        detector_label, detector_signal = DETECTORS[max(0, self.detector.currentIndex())]
        return {
            "axis": {
                "label": label,
                "setpoint_signals": list(setpoints),
                "readback_signal": readback,
                "mass_coefficients": self._coefficients(),
            },
            "detector_signal": detector_signal,
            "start": self.start_value.value(),
            "stop": self.end_value.value(),
            "step": self.step_value.value(),
            "dwell_s": self.dwell_value.value() / 1000.0,
            "samples_per_point": self.samples.value(),
            # 回读未稳定时记下来并标注，不静默当成功，也不因一个点废掉整场
            "on_unsettled": "record",
            # 回落属于设备安全收尾，交给服务端执行（文档 6.6）：客户端退出或崩溃时
            # 它照样把一组磁铁全部退到位，也不会只写第一路。auto=false 时服务端不回落，
            # 参数留着给「执行回落」按钮用同一套值。
            "retract": {
                "current_a": self.retract_current.value(),
                "rate_a_s": self.retract_rate.value(),
                "auto": self.auto_retract.isChecked(),
            },
        }

    def _planned_points(self) -> int:
        start = self.start_value.value()
        end = self.end_value.value()
        step = self.step_value.value()
        if step <= 0 or end == start:
            return 1 if end == start else 0
        if (end - start > 0) != (step > 0):
            return 0
        return int(abs(end - start) / abs(step)) + 1

    def _update_scan_summary(self, *_args) -> None:
        start = self.start_value.value()
        end = self.end_value.value()
        count = self._planned_points()
        if end <= start:
            message, state = "参数有误：终点必须大于起点。", "error"
        elif count < 2:
            message, state = "参数有误：扫描范围至少需要两个采集点。", "error"
        elif count > MAX_POINTS:
            message = f"预计 {count:,} 点，超过上限 {MAX_POINTS:,} 点，请增大步长。"
            state = "error"
        else:
            samples = self.samples.value()
            dwell = self.dwell_value.value() / 1000.0
            seconds = count * dwell
            duration = f"{seconds:.0f} 秒" if seconds < 60 else f"{seconds / 60:.1f} 分钟"
            message = (
                f"预计 {count:,} 点 · 驻留合计 {duration}（未含稳定等待，实际更久） · "
                f"{start:.2f}–{end:.2f} A · 每点 {samples} 次采样取中位数"
            )
            state = "idle"
        self.scan_summary.setText(message)
        self._apply_state_property(self.scan_summary, state)

    # ------------------------------------------------------------------
    # 启动 / 停止
    # ------------------------------------------------------------------
    def start_scan(self) -> None:
        if instrument_api.is_read_only():
            # 只读部署下按钮本来就是灰的；这里再挡一次是为了"按钮被别的代码
            # 重新启用"或程序化调用时也不发请求——服务端同样会 400（见 §8.9 P2.2）
            self._set_scan_state(
                "全局只读模式：执行服务禁用了所有写入，无法启动扫描。", "warn"
            )
            return
        count = self._planned_points()
        if self.end_value.value() <= self.start_value.value():
            self._set_scan_state("终点必须大于起点", "error")
            self.end_value.setFocus()
            return
        if count < 2:
            self._set_scan_state("扫描范围必须至少包含两个采集点，请减小步长", "error")
            self.step_value.setFocus()
            return
        if count > MAX_POINTS:
            self._set_scan_state(
                f"预计 {count:,} 点，超过上限 {MAX_POINTS:,} 点，请增大步长", "error"
            )
            self.step_value.setFocus()
            return
        if self._start_in_flight:
            return

        self._points = []
        self._since = 0
        self._run_id = None
        self.plot.set_data([], [])
        self.progress.setValue(0)
        self.acknowledge_button.setVisible(False)
        self._start_in_flight = True
        self.start_button.setEnabled(False)
        self._set_scan_state("正在提交扫描任务…", "warn")

        thread = instrument_api.request_scan_start(self._build_request())
        thread.completed.connect(self._on_started)

    def _on_started(self, payload: dict) -> None:
        self._start_in_flight = False
        if not payload.get("ok"):
            self._set_scan_state(f"提交失败：{payload.get('message', '')}", "error")
            self._reset_scan_buttons()
            return
        status = payload.get("payload") or {}
        self._run_id = status.get("run_id")
        if not self._run_id:
            self._set_scan_state("提交失败：服务未返回任务号", "error")
            self._reset_scan_buttons()
            return
        self.stop_button.setEnabled(True)
        self._apply_status(status)
        self._timer.start()

    def stop_scan(self) -> None:
        if not self._run_id:
            return
        self.stop_button.setEnabled(False)
        self._set_scan_state("已请求停止，等待当前点收尾", "warn")
        instrument_api.request_scan_stop(self._run_id)

    def _acknowledge(self) -> None:
        """现场核对设备状态后释放恢复锁。

        在确认之前该设备组一直被服务端锁着（文档 6.3），所以这个按钮是
        解除阻塞的唯一出口，不能省。
        """
        if not self._run_id:
            return
        self.acknowledge_button.setEnabled(False)
        thread = instrument_api.request_scan_acknowledge(
            self._run_id, note="操作员在界面确认"
        )
        thread.completed.connect(self._on_acknowledged)

    def _on_acknowledged(self, payload: dict) -> None:
        self.acknowledge_button.setEnabled(True)
        if not payload.get("ok"):
            self._set_scan_state(
                f"确认失败：{payload.get('message', '')}", "error"
            )
            return
        self.acknowledge_button.setVisible(False)
        self._set_scan_state("设备状态已确认，设备组锁已释放", "good")

    def is_operation_active(self) -> bool:
        """任务已提交且未到终态即为「进行中」。"""
        return self._run_id is not None and self._state not in TERMINAL_STATES

    def safe_stop(self) -> None:
        """退出前请求安全停止（服务端继续收尾，不随窗口消失）。"""
        if self.is_operation_active():
            self.stop_scan()

    # ------------------------------------------------------------------
    # 停止后回落（demo 现场约定：停止扫描后按此回落，速度 ≤10 A/s）
    # 执行整段都在服务端：客户端只发起、只如实转述结果（文档 6.6）。
    # ------------------------------------------------------------------
    def _manual_retract(self) -> None:
        """手动回落：把当前扫描轴的**全部**设定值一起退到回落电流。

        成组轴只写第一路会让同组其余磁铁停在扫描结束时的电流上，所以整组一起
        交给服务端；它逐路写速率、写电流，再逐路等回读进入容差。
        """
        if self._retract_in_flight:
            return
        if instrument_api.is_read_only():
            self._set_retract_status(
                "全局只读模式：执行服务禁用了所有写入，不提供磁铁回落。", "warn"
            )
            return
        label, setpoints, _readback = self._axis_definition()
        self._retract_in_flight = True
        self.retract_button.setEnabled(False)
        self._set_retract_status("回落中（等待各路回读到位）…", "warn")
        thread = instrument_api.request_magnet_retract(
            {
                "setpoint_signals": list(setpoints),
                "current_a": self.retract_current.value(),
                "rate_a_s": self.retract_rate.value(),
            }
        )
        thread.completed.connect(
            lambda payload, lbl=label: self._on_retract_done(lbl, payload)
        )

    def _on_retract_done(self, label: str, payload: dict) -> None:
        """按服务端结果落文案：只有它说「全部到位」才敢写已回落。

        有路没到位时端点仍是 200，只在 body 里给 ok=false；把这种响应当成功
        显示，等于让操作员以为磁场已经退到零。
        """
        self._retract_in_flight = False
        self.retract_button.setEnabled(not instrument_api.is_read_only())
        if not payload.get("ok"):
            self._set_retract_status(_retract_failure_text(payload.get("message")), "error")
            return
        result = payload.get("payload") or {}
        if not result.get("ok"):
            self._set_retract_status(
                f"{label} 回落未到位：{result.get('message') or '服务未说明哪一路没到位'}",
                "error",
            )
            return
        applied = _applied_text(result.get("applied") or {})
        self._set_retract_status(
            f"{label} {result.get('message') or '已回落'}{f'（{applied}）' if applied else ''}",
            "good",
        )

    def _set_retract_status(self, text: str, state: str) -> None:
        self.retract_status.setText(text)
        self._apply_state_property(self.retract_status, state)

    # ------------------------------------------------------------------
    # 轮询
    # ------------------------------------------------------------------
    def _poll(self) -> None:
        if not self._run_id:
            self._timer.stop()
            return
        if self._status_in_flight or self._points_in_flight:
            return
        self._status_in_flight = True
        thread = instrument_api.request_scan_status(self._run_id)
        thread.completed.connect(self._on_status)

    def _on_status(self, payload: dict) -> None:
        self._status_in_flight = False
        if not payload.get("ok"):
            self._set_scan_state(f"状态查询失败：{payload.get('message', '')}", "error")
            return
        self._apply_status(payload.get("payload") or {})

    def _apply_status(self, status: dict) -> None:
        self._status = dict(status)
        self._state = str(status.get("state", "idle"))
        total = int(status.get("total_points") or 0)
        self._points_total = total
        done = int(status.get("completed_points") or 0)
        if total:
            self.progress.setValue(round(done / total * 100))

        text = STATE_TEXT.get(self._state, self._state)
        message = status.get("message") or ""
        if message and self._state in TERMINAL_STATES:
            text = f"{text}：{message}"
        self._set_scan_state(text, self._state_tone())

        if message and self._state in TERMINAL_STATES:
            # 自动回落由服务端在收尾时做，结果只出现在状态文案里：界面转述它，
            # 不再自己宣布成功（客户端宣布过的「已回落」曾经是假的）。
            self._reflect_service_retract(self._state, message)

        if self._state == "recovery_required":
            self.acknowledge_button.setVisible(True)

        if self._state in TERMINAL_STATES:
            # 不能在这里直接收尾：此刻很可能还有点没拉回来（服务端在置终态之前
            # 才写完最后一批点）。先补齐，_on_points 里再真正结束。
            self._pending_terminal = True
            self._fetch_points()
            return

        self._fetch_points()

    def _fetch_points(self) -> None:
        if self._points_in_flight or not self._run_id:
            return
        self._points_in_flight = True
        thread = instrument_api.request_scan_points(self._run_id, self._since)
        thread.completed.connect(self._on_points)

    def _on_points(self, payload: dict) -> None:
        self._points_in_flight = False
        if payload.get("ok"):
            fresh = (payload.get("payload") or {}).get("points") or []
            if fresh:
                self._points.extend(fresh)
                self._since = max(int(p.get("index", 0)) for p in fresh) + 1
                self._redraw()

        if not self._pending_terminal:
            return
        # 还有没拉完的就再拉一轮，直到与服务端点数一致
        if self._points_total and len(self._points) < self._points_total:
            self._fetch_points()
            return
        self._finalize()

    def _finalize(self) -> None:
        """点已拉全，可以收尾了。

        这里**不触发回落**：自动回落由服务端在置终态之前完成（文档 6.6）。放在这里
        意味着关掉界面就不回落，而且客户端只写得到第一路。
        """
        self._pending_terminal = False
        self._timer.stop()
        self._reset_scan_buttons()
        if self._state == "completed":
            self.plot.annotate_peaks(4)
        skipped = len(self._points) - sum(1 for p in self._points if p.get("included"))
        if skipped:
            self._set_scan_state(
                f"{STATE_TEXT.get(self._state, self._state)}"
                f"（{skipped} 个点质量不合格，未画入曲线）",
                self._state_tone(),
            )

    def _reflect_service_retract(self, state: str, message: str) -> None:
        """把服务端的回落结论搬到回落状态条上；界面不执行回落，也不替它下结论。"""
        if "回落" not in message:
            return
        # 扫描成功完成时才可能是「已回落到…」；失败/需人工确认一律按异常显示
        self._set_retract_status(message, "good" if state == "completed" else "error")

    def _state_tone(self) -> str:
        if self._state == "completed":
            return "good"
        if self._state in {"failed", "recovery_required"}:
            return "error"
        if self._state in {"running", "stop_requested", "preparing", "completing"}:
            return "warn"
        return "idle"

    # ------------------------------------------------------------------
    # 绘图
    # ------------------------------------------------------------------
    def _use_mass_axis(self) -> bool:
        return self.rb_mass.isChecked()

    def _redraw(self) -> None:
        """按**合格**点绘图；不合质量要求的点不画进曲线。"""
        included = [p for p in self._points if p.get("included")]
        if not included:
            self.plot.set_data([], [])
            self.current_label.setText("当前 I：--")
            self.intensity_label.setText("当前强度：--")
            self.peak_label.setText("最大峰：--")
            return

        coefficients = self._coefficients() if self._use_mass_axis() else []
        xs = [
            _mass(float(p["coordinate"]), coefficients) if coefficients
            else float(p["coordinate"])
            for p in included
        ]
        ys = [float(p["signal"]) for p in included]
        self.plot.set_data(xs, ys)

        unit = "u" if coefficients else "A"
        self.current_label.setText(f"当前 I：{xs[-1]:.3f} {unit}")
        self.intensity_label.setText(f"当前强度：{ys[-1]:.3f} nA")
        peak = max(range(len(ys)), key=ys.__getitem__)
        self.peak_label.setText(f"{xs[peak]:.3f} {unit}")

        skipped = len(self._points) - len(included)
        if skipped:
            self.state_label.setToolTip(
                f"有 {skipped} 个点质量不合格（未稳定或探测掉线），未画入曲线"
            )

    def _apply_yrange(self) -> None:
        """手动锁 Y 轴范围；留空任一项则自动。"""
        try:
            ymin = float(self.ymin_edit.text()) if self.ymin_edit.text().strip() else None
            ymax = float(self.ymax_edit.text()) if self.ymax_edit.text().strip() else None
        except ValueError:
            self._set_scan_state("Y 轴范围必须是数字", "error")
            return
        if ymin is not None and ymax is not None and ymin >= ymax:
            self._set_scan_state("Y 轴下限必须小于上限", "error")
            return
        if self.plot._raw_x.size and ymin is not None and ymax is not None:
            xmin, xmax = float(self.plot._raw_x.min()), float(self.plot._raw_x.max())
            self.plot.set_ranges(xmin, xmax, ymin, ymax)
        else:
            self.plot.reset_view()

    def _reset_scan_buttons(self) -> None:
        # 只读部署下"开始扫描"始终压住：服务端会 400，界面不该给一个按得动的按钮
        self.start_button.setEnabled(not instrument_api.is_read_only())
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)

    def _apply_read_only(self) -> None:
        """只读部署下压住本页所有会写设备的控件，并把原因写在状态行上。

        「开始扫描」和「执行回落」都要动磁铁电源。服务端在只读模式下会把两者
        都拒掉，但让操作员按下去才知道被拒既慢又容易被误读成"参数填错了"。
        """
        if not instrument_api.is_read_only():
            return
        self.start_button.setEnabled(False)
        self.retract_button.setEnabled(False)
        self._set_scan_state(
            "全局只读模式：执行服务禁用了所有写入，本页只能查看。", "warn"
        )
        self._set_retract_status("全局只读模式：不提供磁铁回落。", "warn")

    def _apply_state_property(self, label: QLabel, state: str) -> None:
        label.setProperty("state", state)
        label.style().unpolish(label)
        label.style().polish(label)

    def _set_scan_state(self, text: str, state: str = "idle") -> None:
        self.state_label.setText(text)
        self._apply_state_property(self.state_label, state)

    # ------------------------------------------------------------------
    # 数据导出
    # ------------------------------------------------------------------
    def _default_name(self, readings: dict[str, dict] | None = None) -> str:
        """按 demo 命名：元素-序号-Ar流量-He流量-气压Pa-功率W-长度cm。

        取不到的值用 ``-`` 占位：名字是给人看的，不能因为某一路没连上或现场没配
        就抛异常、让整场数据导不出来。condense length 平台上没有对应信号（见
        ``_collect_payload`` 的 unavailable），所以这一段恒为占位。
        """
        readings = readings or {}
        ar = _reading_value(readings, "gas.ar.flow_setpoint")
        he = _reading_value(readings, "gas.he.flow_setpoint")
        pressure = _reading_value(readings, "vacuum.chamber_pressure")
        power = _reading_value(readings, "sputter.power_setpoint")
        parts = [
            self.element_edit.text().strip() or "Ar",
            f"{int(self.index_edit.value()):03d}",
            f"Ar{ar:.0f}" if ar is not None else "Ar-",
            # He 流量典型在 0.1 sccm 量级，取整会把值写成 0，等于丢了工况信息
            f"He{he:.2f}" if he is not None else "He-",
            # 气压是 ~1 Pa 量级的细调参数，1 位小数会让两次不同的工况撞成同一个名字
            f"{pressure:.2f}Pa" if pressure is not None else "-Pa",
            f"{power:.0f}W" if power is not None else "-W",
            "-cm",
        ]
        return _safe_filename("-".join(parts))

    def _collect_payload(
        self, readings: dict[str, dict] | None = None, snapshot_note: str = ""
    ) -> dict:
        """组装全参数导出内容；``readings`` 是导出瞬间的现场快照读数。"""
        readings = readings or {}
        snapshot = {
            group: {signal: _reading_value(readings, signal) for signal in signals}
            for group, signals in EXPORT_SNAPSHOT_SIGNALS
        }
        coefficients = self._coefficients()
        return {
            "name": self._default_name(readings),
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "scan": {
                "axis": self.axis.currentText(),
                "detector": self.detector.currentText(),
                "start_a": self.start_value.value(),
                "end_a": self.end_value.value(),
                "step_a": self.step_value.value(),
                # 点间斜坡速率**不在扫描契约里**（`ScanRunRequest` 没有这一项）：真正
                # 生效的是映射里磁铁条目的 `max_rate`，而本页不读映射。与其把一个界面
                # 上的数字写成"本次扫描速率"，不如写 null 并把来源说明白——编出来的数
                # 会被当成实测条件拿去做分析（改造报告 §8.8 第 1 条）。
                "rate_a_s": None,
                "rate_source": "映射里磁铁条目的 max_rate（本页不读映射，故不在此记录具体值）",
                "dwell_ms": self.dwell_value.value(),
                "samples_per_point": self.samples.value(),
                "mass_coefficients": coefficients,
                "x_axis": "mass" if self._use_mass_axis() else "current",
            },
            "retract": {
                "current_a": self.retract_current.value(),
                "rate_a_s": self.retract_rate.value(),
                "auto": self.auto_retract.isChecked(),
            },
            # 设备现场快照：值来自导出那一刻的批量回读，读不到的一律 null
            "device_snapshot": snapshot,
            # 采集起止时间是服务端任务记录里的，界面只做搬运，不按本地时间造
            "acquisition": {
                "run_id": self._run_id,
                "state": self._state,
                "started_at": self._status.get("started_at"),
                "finished_at": self._status.get("finished_at"),
                "point_count": len(self._points),
            },
            # 原 demo 由 LabVIEW 推送/计算的量：平台没有这条数据链路，如实写 null，
            # 不按旧字段名编数——编出来的数会被当成实测条件拿去做分析。
            "labview": {
                "source": None,
                "latest_value": None,
                "point_count": None,
                "condense_length_cm": None,
            },
            "unavailable": {
                "note": snapshot_note,
                "signals": [
                    signal
                    for signals in snapshot.values()
                    for signal, value in signals.items()
                    if value is None
                ],
                "not_migrated": [
                    "labview.source / labview.latest_value / labview.point_count："
                    "原 demo 由 LabVIEW 推送，平台未迁移该数据链路",
                    "labview.condense_length_cm：原 demo 由 LabVIEW 计算，平台无对应信号",
                ],
            },
            "points": self._points,
        }

    def _export(self, fmt: str) -> None:
        """导出：先取一次现场快照，再让用户选路径落盘。

        快照得先读：默认文件名要写进实际 Ar/He 流量、气压和功率，没读到就只能占位。
        """
        if not self._points:
            self._set_scan_state("还没有数据可导出，请先完成一次扫描", "warn")
            return
        if self._export_in_flight:
            self._set_scan_state("上一次导出还在读取设备快照，请稍候", "warn")
            return
        self._export_in_flight = True
        self._export_fallback_used = False
        self._export_format = fmt
        self._set_scan_state("正在读取设备快照，随后请选择保存位置…", "warn")
        thread = instrument_api.request_read(signals=list(EXPORT_SIGNALS))
        thread.completed.connect(self._on_export_snapshot)

    def _on_export_snapshot(self, payload: dict) -> None:
        """快照到手（或读失败）后选路径落盘。

        快照读失败**不阻断**导出：点已经采完在内存里，因为设备没连上就不给导出，
        等于让现场丢掉一场扫描；缺的字段按 null 记下并把失败原因写进 unavailable。

        按信号名读失败（最常见的原因：某一路已被从 PV 映射里删掉，服务端直接
        400）时再读一次**全量**兜底——为了缺一路就让整份工况快照变空，等于把
        这轮实验条件记录丢掉。
        """
        if payload.get("ok"):
            self._finish_export(
                instrument_api.readings_by_signal(payload.get("payload")), ""
            )
            return
        reason = payload.get("message") or "服务未返回原因"
        if self._export_fallback_used:
            self._finish_export({}, f"设备快照读取失败：{reason}")
            return
        self._export_fallback_used = True
        self._set_scan_state(f"按信号读取快照失败（{reason}），正在回退为全量读取…", "warn")
        retry = instrument_api.request_read()
        retry.completed.connect(
            lambda payload, why=reason: self._on_export_fallback(payload, why)
        )

    def _on_export_fallback(self, payload: dict, reason: str) -> None:
        if payload.get("ok"):
            self._finish_export(
                instrument_api.readings_by_signal(payload.get("payload")),
                f"按信号读取快照失败（{reason}），已回退为全量读取："
                "标为 null 的信号不在当前 PV 映射里",
            )
            return
        self._finish_export({}, f"设备快照读取失败：{reason}")

    def _finish_export(self, readings: dict[str, dict], note: str) -> None:
        """快照已定（可能为空）→ 选路径 → 落盘。"""
        self._export_in_flight = False
        fmt = self._export_format
        suffix = ".json" if fmt == "json" else ".txt"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出扫描数据",
            self._default_name(readings) + suffix,
            "JSON (*.json)" if fmt == "json" else "文本 (*.txt)",
        )
        if not path:
            self._set_scan_state("已取消导出", "idle")
            return
        try:
            if fmt == "json":
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(
                        self._collect_payload(readings, note), fh,
                        ensure_ascii=False, indent=2,
                    )
            else:
                self._write_txt(path)
        except OSError as exc:
            self._set_scan_state(f"导出失败：{exc}", "error")
            return
        self._set_scan_state(f"已导出 {path}", "good")

    def _write_txt(self, path: str) -> None:
        """TXT：永远导出原始坐标（磁铁电流）与束流强度。

        选 Mass 只是**多一列**换算后的质量：质量依赖界面上的标定系数，改系数或换
        机器后同一份数据会算出别的值，原始电流才是可追溯的记录（文档 6.4）。
        质量不合格的点也照常导出并用 quality 列标出来——静默丢掉会让文件里的点数
        和谱图对不上，事后无从判断是哪几个点被排除了。
        """
        use_mass = self._use_mass_axis()
        coefficients = self._coefficients()
        excluded = sum(1 for p in self._points if not p.get("included"))
        columns = ["current_A", "beam_nA"]
        if use_mass:
            columns.append("mass_u")
        columns.append("quality")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# " + "\t".join(columns) + "\n")
            fh.write(
                f"# 共 {len(self._points)} 点，其中 {excluded} 点质量不合格"
                "（quality 非 ok，未画入曲线，仍保留在本文件里）\n"
            )
            for p in self._points:
                current = float(p["coordinate"])
                row = [f"{current:.6f}", f"{float(p['signal']):.6f}"]
                if use_mass:
                    row.append(f"{_mass(current, coefficients):.6f}")
                row.append(str(p.get("quality") or "unknown"))
                fh.write("\t".join(row) + "\n")

    def _export_png(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出谱图", "spectrum.png", "PNG (*.png)")
        if not path:
            return
        pixmap = self.plot.view.grab()
        pixmap.save(path)
        self._set_scan_state(f"谱图已保存 {path}", "good")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._apply_read_only()
        # 页面重新可见时补一次查询：任务在服务端继续跑，界面不该落后
        if self.is_operation_active():
            self._poll()
            self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        # 任务在服务端继续跑，隐藏页面时停掉轮询省资源；
        # 回到页面时由 showEvent 补一次查询把进度追平，不会漏掉终态。
        self._timer.stop()


PAGE_SPEC = PageSpec(
    key="scan",
    label="扫谱",
    icon="scan",
    section="control",
    factory=ScanPage,
)
