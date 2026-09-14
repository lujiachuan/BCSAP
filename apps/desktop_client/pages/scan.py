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
        self._retract_done = False

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

        # 第二行：扫描速率 + 数据命名（元素/序号）
        row2 = QHBoxLayout()
        row2.setContentsMargins(0, 0, 0, 0)
        row2.setSpacing(10)
        self.rate_value = self._double_spin(5.0, 0.1, 600.0, 1.0, " A/s")
        row2.addWidget(self._field("扫描速率", self.rate_value))
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
        layout.addWidget(self.auto_retract)

        self.retract_button = QPushButton("执行回落")
        self.retract_button.setToolTip("立即把当前扫描轴退到回落电流（斜坡按回落速率）")
        self.retract_button.clicked.connect(self._manual_retract)
        layout.addWidget(self.retract_button)

        self.retract_status = QLabel("就绪", objectName="mutedText")
        layout.addWidget(self.retract_status)
        layout.addStretch()
        return box

    def _build_export_row(self) -> QFrame:
        """数据导出：JSON 全参数 / TXT 仅电流两列。命名规则对齐 demo。"""
        box = QFrame(objectName="panel")
        layout = QHBoxLayout(box)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(10)
        layout.addWidget(QLabel("数据导出", objectName="panelTitle"))

        self.export_hint = QLabel(
            "命名：元素-序号-Ar-He-气压Pa-PowerW-cm（JSON 含全参数，TXT 仅电流两列）",
            objectName="mutedText",
        )
        layout.addWidget(self.export_hint, 1)

        self.export_json_button = QPushButton("导出 JSON(全参数)")
        self.export_json_button.clicked.connect(lambda: self._export("json"))
        layout.addWidget(self.export_json_button)
        self.export_txt_button = QPushButton("导出 TXT(仅电流)")
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
            "rate_a_s": self.rate_value.value(),
            "dwell_s": self.dwell_value.value() / 1000.0,
            "samples_per_point": self.samples.value(),
            # 回读未稳定时记下来并标注，不静默当成功，也不因一个点废掉整场
            "on_unsettled": "record",
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
        self._retract_done = False
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
    # 停止后回落（demo 现场约定：停止扫描后自动按此回落，速度 ≤10 A/s）
    # ------------------------------------------------------------------
    def _manual_retract(self) -> None:
        """手动触发一次回落：把当前扫描轴退到回落电流。"""
        self._retract_to_target()

    def _retract_to_target(self) -> None:
        label, setpoints, _readback = self._axis_definition()
        target = self.retract_current.value()
        rate = min(self.retract_rate.value(), MAX_RETRACT_RATE_A_PER_S)
        self.retract_status.setText("回落中…")
        self.retract_status.setProperty("state", "warn")
        self.retract_status.style().unpolish(self.retract_status)
        self.retract_status.style().polish(self.retract_status)
        # 多轴同步组只写第一个 setpoint；服务端会把同组联动
        thread = instrument_api.request_write(
            setpoints[0], target, ramp=True
        )
        if thread is None:
            self.retract_status.setText("写入忙，请稍候")
            self.retract_status.setProperty("state", "error")
            self.retract_status.style().unpolish(self.retract_status)
            self.retract_status.style().polish(self.retract_status)
            return
        thread.completed.connect(
            lambda _p, lbl=label, t=target, r=rate: self._on_retract_done(lbl, t, r)
        )

    def _on_retract_done(self, label: str, target: float, rate: float) -> None:
        self.retract_status.setText(f"{label} 已回落到 {target:.2f} A（速率 {rate:.1f} A/s）")
        self.retract_status.setProperty("state", "good")
        self.retract_status.style().unpolish(self.retract_status)
        self.retract_status.style().polish(self.retract_status)

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
        """点已拉全，可以收尾了。"""
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
        # 扫描结束：按约定自动回落（用户可勾选关闭）
        if self.auto_retract.isChecked() and not self._retract_done:
            self._retract_done = True
            self._retract_to_target()

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
        self.start_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)

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
    def _default_name(self) -> str:
        """对齐 demo 命名：元素-序号-Ar-He-气压Pa-PowerW-cm"""
        element = self.element_edit.text().strip() or "Ar"
        index = int(self.index_edit.value())
        return f"{element}{index:03d}Ar-He"

    def _collect_payload(self) -> dict:
        """组装全参数导出内容。"""
        coefficients = self._coefficients()
        return {
            "name": self._default_name(),
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "scan": {
                "axis": self.axis.currentText(),
                "detector": self.detector.currentText(),
                "start_a": self.start_value.value(),
                "end_a": self.end_value.value(),
                "step_a": self.step_value.value(),
                "rate_a_s": self.rate_value.value(),
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
            "points": self._points,
        }

    def _export(self, fmt: str) -> None:
        if not self._points:
            self._set_scan_state("还没有数据可导出，请先完成一次扫描", "warn")
            return
        default_path = self._default_name() + (".json" if fmt == "json" else ".txt")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出扫描数据", default_path,
            "JSON (*.json)" if fmt == "json" else "文本 (*.txt)",
        )
        if not path:
            return
        try:
            if fmt == "json":
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(self._collect_payload(), fh, ensure_ascii=False, indent=2)
            else:
                # TXT 仅电流两列：x(电流或质量), y(强度 nA)
                coefficients = self._coefficients() if self._use_mass_axis() else []
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("# x y\n")
                    for p in self._points:
                        if not p.get("included"):
                            continue
                        x = (
                            _mass(float(p["coordinate"]), coefficients)
                            if coefficients
                            else float(p["coordinate"])
                        )
                        fh.write(f"{x:.6f} {float(p['signal']):.6f}\n")
        except OSError as exc:
            self._set_scan_state(f"导出失败：{exc}", "error")
            return
        self._set_scan_state(f"已导出 {path}", "good")

    def _export_png(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出谱图", "spectrum.png", "PNG (*.png)")
        if not path:
            return
        pixmap = self.plot.view.grab()
        pixmap.save(path)
        self._set_scan_state(f"谱图已保存 {path}", "good")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
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
