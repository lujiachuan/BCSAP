"""扫谱页面：曲线优先、顶部控制条和底部状态条。"""

from __future__ import annotations

import math
import random

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from apps.desktop_client.pages.common import page_layout, primary_button
from apps.desktop_client.pages.registry import PageSpec
from apps.desktop_client.spectrum_plot import SpectrumPlot
from apps.desktop_client.widgets import PageHeading, Panel


class ScanPage(QWidget):
    """曲线优先、顶部控制条和底部状态条的扫谱页面。"""

    MAX_SIMULATED_POINTS = 50_000

    def __init__(self) -> None:
        super().__init__()
        self._x: list[float] = []
        self._y: list[float] = []
        self._cursor = 0
        self._paused = False
        self._timer = QTimer(self)
        self._timer.setInterval(70)
        self._timer.timeout.connect(self._append_scan_data)

        layout = page_layout(self)
        layout.addWidget(
            PageHeading("扫谱控制", "配置扫描范围并实时观察质谱曲线。当前使用模拟数据。")
        )
        layout.addWidget(self._build_controls())

        plot_panel = Panel("实时谱图", "曲线优先")
        self.plot = SpectrumPlot("质荷比 m/z", "离子强度 / a.u.")
        plot_panel.body.addWidget(self.plot, 1)
        plot_panel.body.addWidget(self._build_status_bar())
        layout.addWidget(plot_panel, 1)
        self._update_scan_summary()

    def _build_controls(self) -> QFrame:
        controls = QFrame(objectName="panel")
        outer = QVBoxLayout(controls)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        self.sample = QComboBox()
        self.sample.addItems(("Ar 标准样品 / S-20260907-03", "未指定样品"))
        self.sample.setMinimumWidth(230)
        row.addWidget(self._field("样品", self.sample), 2)

        self.start_value = self._double_spin(10.0, 0.0, 1000.0, 0.1, " amu")
        self.end_value = self._double_spin(120.0, 0.0, 1000.0, 0.1, " amu")
        self.step_value = self._double_spin(0.01, 0.001, 10.0, 0.01, " amu", 3)
        self.dwell_value = self._double_spin(20.0, 1.0, 10000.0, 1.0, " ms")
        for label, widget in (
            ("起点", self.start_value),
            ("终点", self.end_value),
            ("步长", self.step_value),
            ("驻留", self.dwell_value),
        ):
            row.addWidget(self._field(label, widget))

        self.start_button = primary_button("开始扫描")
        self.start_button.clicked.connect(self.start_scan)
        row.addWidget(self.start_button)
        outer.addLayout(row)
        self.scan_summary = QLabel(objectName="mutedText")
        self.scan_summary.setWordWrap(True)
        outer.addWidget(self.scan_summary)
        for spin in (self.start_value, self.end_value, self.step_value, self.dwell_value):
            spin.valueChanged.connect(self._update_scan_summary)
        return controls

    def _build_status_bar(self) -> QWidget:
        status = QWidget()
        row = QHBoxLayout(status)
        row.setContentsMargins(0, 3, 0, 0)
        row.setSpacing(12)
        self.state_label = QLabel("等待开始", objectName="mutedText")
        self.current_label = QLabel("当前 m/z：--")
        self.intensity_label = QLabel("当前强度：--")
        self.peak_label = QLabel("最大峰：--")
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(170)
        self.pause_button = QPushButton("暂停")
        self.stop_button = QPushButton("安全停止", objectName="dangerButton")
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.pause_button.clicked.connect(self.toggle_pause)
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
        row.addWidget(self.pause_button)
        row.addWidget(self.stop_button)
        return status

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

    def start_scan(self) -> None:
        start = self.start_value.value()
        end = self.end_value.value()
        if end <= start:
            self._set_scan_state("终点必须大于起点", "error")
            self.end_value.setFocus()
            return
        count = int((end - start) / self.step_value.value()) + 1
        if count < 2:
            self._set_scan_state("扫描范围必须至少包含两个采集点，请减小步长", "error")
            self.step_value.setFocus()
            return
        if count > self.MAX_SIMULATED_POINTS:
            self._set_scan_state(
                f"预计 {count:,} 点，超过模拟上限 {self.MAX_SIMULATED_POINTS:,} 点，请增大步长",
                "error",
            )
            self.step_value.setFocus()
            return
        increment = (end - start) / (count - 1)
        self._x = [start + increment * index for index in range(count)]
        self._y = [self._signal(value) for value in self._x]
        self._cursor = 0
        self._paused = False
        self.plot.set_data([], [])
        self.plot.set_ranges(start, end, 0.0, max(self._y) * 1.10)
        self.progress.setValue(0)
        self._set_scan_state("扫描进行中")
        self.start_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        self._timer.start()

    @staticmethod
    def _signal(x_value: float) -> float:
        peaks = ((40.0, 92.0, 0.8), (68.0, 70.0, 1.3), (84.0, 42.0, 0.9), (112.0, 55.0, 1.1))
        signal = 3.0 + 0.6 * math.sin(x_value * 0.7)
        for center, height, width in peaks:
            signal += height * math.exp(-((x_value - center) ** 2) / (2 * width**2))
        return max(0.0, signal + random.uniform(-0.7, 0.7))

    def _append_scan_data(self) -> None:
        self._cursor = min(len(self._x), self._cursor + max(20, len(self._x) // 60))
        visible_x = self._x[: self._cursor]
        visible_y = self._y[: self._cursor]
        self.plot.set_data(visible_x, visible_y)
        self.progress.setValue(round(self._cursor / len(self._x) * 100))
        self.current_label.setText(f"当前 m/z：{visible_x[-1]:.2f}")
        self.intensity_label.setText(f"当前强度：{visible_y[-1]:.1f}")
        peak_index = max(range(len(visible_y)), key=visible_y.__getitem__)
        self.peak_label.setText(f"最大峰：{visible_x[peak_index]:.2f}")
        if self._cursor == len(self._x):
            self._finish_scan()

    def toggle_pause(self) -> None:
        self._paused = not self._paused
        if self._paused:
            self._timer.stop()
            self._set_scan_state("扫描已暂停", "warn")
            self.pause_button.setText("继续")
        else:
            self._timer.start()
            self._set_scan_state("扫描进行中")
            self.pause_button.setText("暂停")

    def stop_scan(self) -> None:
        self._timer.stop()
        self._set_scan_state("已安全停止，当前数据已保留", "warn")
        self._reset_scan_buttons()

    def _finish_scan(self) -> None:
        self._timer.stop()
        self.plot.annotate_peaks(4)
        self._set_scan_state("扫描完成，等待保存服务接入", "good")
        self._reset_scan_buttons()

    def _reset_scan_buttons(self) -> None:
        self.start_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.pause_button.setText("暂停")
        self.stop_button.setEnabled(False)

    def _update_scan_summary(self, *_args) -> None:
        start = self.start_value.value()
        end = self.end_value.value()
        if end <= start:
            self.scan_summary.setText("参数有误：终点必须大于起点。")
            self.scan_summary.setProperty("state", "error")
        else:
            count = int((end - start) / self.step_value.value()) + 1
            if count < 2:
                self.scan_summary.setText("参数有误：扫描范围必须至少包含两个采集点。")
                self.scan_summary.setProperty("state", "error")
            else:
                seconds = count * self.dwell_value.value() / 1000
                duration = f"{seconds:.0f} 秒" if seconds < 60 else f"{seconds / 60:.1f} 分钟"
                suffix = (
                    f" · 超过模拟上限 {self.MAX_SIMULATED_POINTS:,} 点"
                    if count > self.MAX_SIMULATED_POINTS
                    else " · 模拟播放将加速展示"
                )
                self.scan_summary.setText(
                    f"预计 {count:,} 点 · 设备采集预计 {duration} · "
                    f"{start:.2f}–{end:.2f} amu{suffix}"
                )
                self.scan_summary.setProperty(
                    "state", "error" if count > self.MAX_SIMULATED_POINTS else "idle"
                )
        self.scan_summary.style().unpolish(self.scan_summary)
        self.scan_summary.style().polish(self.scan_summary)

    def _set_scan_state(self, text: str, state: str = "idle") -> None:
        self.state_label.setText(text)
        self.state_label.setProperty("state", state)
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)


PAGE_SPEC = PageSpec(
    key="scan",
    label="扫谱",
    icon="scan",
    section="control",
    factory=ScanPage,
)
