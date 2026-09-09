"""客户端的第一阶段业务页面。"""

from __future__ import annotations

import math
import random
from urllib.parse import urlparse

from PySide6.QtCore import QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from apps.desktop_client.spectrum_plot import SpectrumPlot
from apps.desktop_client.surfaces import GlassCard
from apps.desktop_client.theme import current_palette, theme_name
from apps.desktop_client.widgets import (
    ClickableLabel,
    MetricCard,
    PageHeading,
    Panel,
)


def page_layout(page: QWidget) -> QVBoxLayout:
    """创建统一的页面边距。"""

    page.setObjectName("pageRoot")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(18, 16, 18, 18)
    layout.setSpacing(12)
    return layout


def primary_button(text: str) -> QPushButton:
    button = QPushButton(text, objectName="primaryButton")
    return button


class PlaceholderPage(QWidget):
    """尚未进入实施阶段的页面。"""

    CAPABILITIES = {
        "样品管理": ("样品编号与批次", "实验备注与状态", "与实验任务关联"),
        "谱图库": ("按条件检索谱图", "查看版本与来源", "受控共享与引用"),
        "谱图分析": ("峰值与区间分析", "多谱图对比", "分析结果导出"),
        "任务与同步": ("后台任务进度", "失败任务重试", "本地与中央数据状态"),
    }

    def __init__(self, title: str, description: str) -> None:
        super().__init__()
        layout = page_layout(self)
        layout.addWidget(PageHeading(title, description))
        panel = GlassCard(object_name="contextCard")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(28, 28, 28, 28)
        message = QLabel("该模块已预留统一入口，当前版本暂不开放操作。", objectName="mutedText")
        message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        planned = self.CAPABILITIES.get(title, (description,))
        capabilities = QLabel(
            "计划能力\n" + "\n".join(f"• {item}" for item in planned)
        )
        capabilities.setAlignment(Qt.AlignmentFlag.AlignCenter)
        panel_layout.addStretch()
        panel_layout.addWidget(message)
        panel_layout.addWidget(capabilities)
        panel_layout.addStretch()
        layout.addWidget(panel, 1)


class WorkbenchPage(QWidget):
    """以实验任务为中心的工作台（M0 表面样板：Hero + 服务状态卡 + 快捷操作 + 最近实验）。"""

    navigateRequested = Signal(str)
    initializationRequested = Signal()
    serviceCardRequested = Signal(str)

    # (key, 服务名, 初始文本, 初始语义)
    SERVICES = (
        ("data", "数据服务", "待连接", "idle"),
        ("instrument", "仪器执行服务", "待连接", "idle"),
        ("epics", "EPICS", "未确认", "idle"),
        ("cache", "本地数据", "待同步", "idle"),
    )

    def __init__(self) -> None:
        super().__init__()
        self._service_states: dict[str, str] = {}
        self._control_enabled = False
        layout = page_layout(self)

        # ---- 标题行：标题 + 设备状态胶囊 + 操作员 + 重新检查 ----
        heading_row = QHBoxLayout()
        heading_row.addWidget(PageHeading("工作台", "上午好，操作员。请先确认设备与服务状态。"))
        heading_row.addStretch()
        self.device_status = QLabel("●  正在检查设备与关键服务", objectName="warnStatus")
        heading_row.addWidget(self.device_status)
        heading_row.addSpacing(18)
        heading_row.addWidget(QLabel("操作员  本机", objectName="mutedText"))
        recheck = QPushButton("重新检查")
        recheck.clicked.connect(self.initializationRequested.emit)
        heading_row.addWidget(recheck)
        layout.addLayout(heading_row)

        # ---- Hero（L2 玻璃卡）：模式、可执行性与下一步操作 ----
        hero = GlassCard(object_name="heroCard", radius="panel")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(18, 14, 18, 14)
        hero_layout.setSpacing(8)
        action_row = QHBoxLayout()
        mode_chip = QLabel("●  模拟运行", objectName="modeChip")
        action_row.addWidget(mode_chip)
        action_row.addStretch()
        self.scan_button = primary_button("开始扫谱")
        self.scan_button.clicked.connect(lambda: self.navigateRequested.emit("scan"))
        self.tuning_button = QPushButton("开始自动调束")
        self.tuning_button.clicked.connect(lambda: self.navigateRequested.emit("tuning"))
        self.scan_button.setEnabled(False)
        self.tuning_button.setEnabled(False)
        action_row.addWidget(self.scan_button)
        action_row.addWidget(self.tuning_button)
        hero_layout.addLayout(action_row)
        self.hero_title = QLabel("正在检查设备与关键服务…", objectName="heroTitle")
        hero_layout.addWidget(self.hero_title)
        self.hero_caption = QLabel(
            "就绪后可执行扫谱与自动调束；详情见下方服务状态。", objectName="mutedText"
        )
        self.hero_caption.setWordWrap(True)
        hero_layout.addWidget(self.hero_caption)
        hero_layout.addWidget(
            QLabel(
                "已选样品 Cu-Ar-023 · 最近扫谱配置 磁场扫描 A-12 · 探测器 Faraday FC3",
                objectName="mutedText",
            )
        )
        layout.addWidget(hero)

        # ---- 服务状态卡（L2 玻璃卡 × 4）----
        services_row = QHBoxLayout()
        services_row.setSpacing(10)
        self.service_values: dict[str, QLabel] = {}
        for key, name, text, state in self.SERVICES:
            card = GlassCard(object_name="serviceCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(14, 11, 14, 11)
            card_layout.setSpacing(4)
            card_layout.addWidget(QLabel(name, objectName="cardCaption"))
            value_label = ClickableLabel(text, objectName="serviceValue")
            value_label.setToolTip("双击查看该服务状态明细")
            value_label.doubleClicked.connect(
                lambda _key=key: self.serviceCardRequested.emit(_key)
            )
            card_layout.addWidget(value_label)
            self.service_values[key] = value_label
            services_row.addWidget(card, 1)
        layout.addLayout(services_row)

        # ---- 主体：最近实验（L1 数据面） + 快捷操作（L2 玻璃卡） ----
        body = QHBoxLayout()
        body.setSpacing(10)
        recent = Panel("最近实验")
        recent_actions = QHBoxLayout()
        recent_actions.addStretch()
        view_all = QPushButton("查看全部")
        view_all.clicked.connect(lambda: self.navigateRequested.emit("library"))
        recent_actions.addWidget(view_all)
        recent.body.addLayout(recent_actions)
        actions = QTableWidget(3, 6)
        actions.setHorizontalHeaderLabels(("实验编号", "样品", "类型", "点数", "状态", "完成时间"))
        rows = (
            ("EXP-20260907-017", "Cu-Ar-022", "磁场扫谱", "20,001", "已归档", "13:48"),
            ("EXP-20260907-016", "Blank-006", "磁场扫谱", "15,001", "已归档", "11:26"),
            ("EXP-20260906-042", "Cu-Ar-021", "自动调束", "30 轮", "已完成", "昨天 17:32"),
        )
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 3:  # 数值列右对齐
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                actions.setItem(row, column, item)
        actions.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        actions.verticalHeader().setVisible(False)
        actions.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        actions.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        actions.setAlternatingRowColors(True)
        recent.body.addWidget(actions)
        body.addWidget(recent, 2)

        quick = GlassCard(object_name="quickCard")
        quick_layout = QVBoxLayout(quick)
        quick_layout.setContentsMargins(16, 14, 16, 16)
        quick_layout.setSpacing(10)
        quick_layout.addWidget(QLabel("快捷操作", objectName="panelTitle"))
        for text, target in (
            ("登记新样品", "samples"),
            ("导入历史谱图", "library"),
            ("检索谱图库", "library"),
        ):
            button = QPushButton(text)
            button.setMinimumHeight(38)
            button.clicked.connect(
                lambda _checked=False, key=target: self.navigateRequested.emit(key)
            )
            quick_layout.addWidget(button)
        quick_layout.addStretch()
        body.addWidget(quick, 1)
        layout.addLayout(body, 1)

    # ---------- 状态更新（对外 API，供 main 接线） ----------

    def set_service_status(self, key: str, state: str, text: str) -> None:
        self._service_states[key] = state
        value = self.service_values.get(key)
        if value is None:
            return
        color = self._state_color(state)
        symbol = "●" if state in {"good", "warn", "error"} else "○"
        value.setText(f"{symbol}  {text}")
        value.setStyleSheet(f"color: {color};")
        if key == "epics":
            self._refresh_device_chip(state)
        self._refresh_hero()

    def set_control_enabled(self, enabled: bool) -> None:
        self._control_enabled = enabled
        reason = "" if enabled else "仪器服务或关键 PV 未就绪，暂不可执行扫谱与调束"
        for button in (self.scan_button, self.tuning_button):
            button.setEnabled(enabled)
            button.setToolTip(reason)
        self._refresh_hero()

    # ---------- 内部呈现 ----------

    def _refresh_device_chip(self, state: str) -> None:
        if state == "good":
            text, object_name = "●  设备就绪", "goodStatus"
        elif state == "error":
            text, object_name = "●  设备不可用于控制", "errorStatus"
        else:
            text, object_name = "●  设备状态待确认", "warnStatus"
        self.device_status.setText(text)
        self.device_status.setObjectName(object_name)
        self.device_status.style().unpolish(self.device_status)
        self.device_status.style().polish(self.device_status)

    def _refresh_hero(self) -> None:
        instrument = self._service_states.get("instrument")
        epics = self._service_states.get("epics")
        if self._control_enabled and instrument == "good" and epics == "good":
            self.hero_title.setText("设备与关键服务就绪")
            self.hero_caption.setText("可执行扫谱与自动调束；数据服务同步状态见下方状态卡。")
            return
        if instrument == "error" or epics == "error":
            blocked: list[str] = []
            if instrument != "good":
                blocked.append("仪器执行服务未就绪")
            if epics != "good":
                blocked.append("关键 PV 未连接")
            self.hero_title.setText("暂不可执行扫谱与调束")
            self.hero_caption.setText(
                "阻塞原因：" + "、".join(blocked) + "。请先处理下方服务状态或点击“重新检查”。"
            )
            return
        self.hero_title.setText("正在检查设备与关键服务…")
        self.hero_caption.setText("完成检查后即可执行扫谱与自动调束。")

    @staticmethod
    def _state_color(state: str) -> str:
        token = {
            "good": "statusGood",
            "warn": "statusWarn",
            "error": "statusError",
        }.get(state, "muted")
        return current_palette()[token]


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


class TuningPage(QWidget):
    """F1 参数表、G1 收敛监控和 H1 前后对比的自动调束页面。"""

    PARAMETERS = (
        ("Q1 电流", "BL:Q1:ISET", "1.842 A", "1.60", "2.10", "0.01"),
        ("Q2 电流", "BL:Q2:ISET", "-0.625 A", "-0.90", "-0.40", "0.01"),
        ("Einzel 电压", "BL:EL:VSET", "3.20 kV", "2.80", "3.60", "0.02"),
        ("X 偏转", "BL:STEER:X", "0.08 V", "-0.50", "0.50", "0.01"),
        ("Y 偏转", "BL:STEER:Y", "-0.12 V", "-0.50", "0.50", "0.01"),
        ("Source 电压", "BL:SRC:VSET", "12.4 kV", "11.5", "13.0", "0.05"),
    )

    def __init__(self) -> None:
        super().__init__()
        self._iteration = 0
        self._target_iterations = 40
        self._values: list[float] = []
        self._timer = QTimer(self)
        self._timer.setInterval(180)
        self._timer.timeout.connect(self._next_iteration)

        layout = page_layout(self)
        layout.addWidget(
            PageHeading("自动调束", "按参数配置、运行监控、结果确认三个阶段完成优化。")
        )
        self.tabs = QTabWidget()
        self.tabs.addTab(self._configuration_tab(), "1  参数配置")
        self.tabs.addTab(self._monitor_tab(), "2  运行监控")
        self.tabs.addTab(self._result_tab(), "3  结果确认")
        self.tabs.setTabEnabled(1, False)
        self.tabs.setTabEnabled(2, False)
        layout.addWidget(self.tabs, 1)

    def _configuration_tab(self) -> QWidget:
        tab = QWidget()
        layout = QHBoxLayout(tab)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(14)

        variables = Panel("优化变量", "已选择 5 / 6")
        self.parameter_table = QTableWidget(len(self.PARAMETERS), 7)
        self.parameter_table.setHorizontalHeaderLabels(
            ("启用", "设备参数", "当前值", "下限", "上限", "步长", "PV")
        )
        for row, values in enumerate(self.PARAMETERS):
            enabled = QTableWidgetItem()
            enabled.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            enabled.setCheckState(Qt.CheckState.Checked if row < 5 else Qt.CheckState.Unchecked)
            self.parameter_table.setItem(row, 0, enabled)
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                if column in (1, 2, 6):
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.parameter_table.setItem(row, column, item)
        header = self.parameter_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.parameter_table.verticalHeader().setVisible(False)
        self.parameter_table.setAlternatingRowColors(True)
        variables.body.addWidget(self.parameter_table)

        strategy = Panel("策略与保护", "贝叶斯优化")
        form = QFormLayout()
        objective = QComboBox()
        objective.addItem("最大化束流强度")
        self.iterations = QSpinBox()
        self.iterations.setRange(5, 200)
        self.iterations.setValue(40)
        self.settle = QDoubleSpinBox()
        self.settle.setRange(0.1, 30.0)
        self.settle.setValue(1.5)
        self.settle.setSuffix(" s")
        form.addRow("优化目标", objective)
        form.addRow("最大迭代", self.iterations)
        form.addRow("稳定等待", self.settle)
        strategy.body.addLayout(form)
        self.safety_check = QCheckBox("异常时停止并恢复安全值")
        self.safety_check.setChecked(True)
        snapshot_check = QCheckBox("开始前保存参数快照")
        snapshot_check.setChecked(True)
        strategy.body.addWidget(self.safety_check)
        strategy.body.addWidget(snapshot_check)
        hint = QLabel("边界检查通过，已启用参数均处于设备允许范围内。")
        hint.setWordWrap(True)
        strategy.body.addWidget(hint)
        strategy.body.addStretch()
        start = primary_button("开始自动调束")
        start.clicked.connect(self.start_tuning)
        strategy.body.addWidget(start)

        layout.addWidget(variables, 3)
        layout.addWidget(strategy, 1)
        return tab

    def _monitor_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(14)

        status = QFrame(objectName="panel")
        status_row = QHBoxLayout(status)
        status_row.setContentsMargins(14, 11, 14, 11)
        self.tuning_state = QLabel("准备运行")
        self.iteration_label = QLabel("第 0 / 40 次迭代", objectName="mutedText")
        self.tuning_progress = QProgressBar()
        self.tuning_progress.setTextVisible(False)
        self.tuning_progress.setFixedWidth(220)
        self.tuning_stop_button = QPushButton("安全停止", objectName="dangerButton")
        self.tuning_stop_button.setEnabled(False)
        self.tuning_stop_button.clicked.connect(self.stop_tuning)
        status_row.addWidget(self.tuning_state)
        status_row.addWidget(self.iteration_label)
        status_row.addWidget(self.tuning_progress)
        status_row.addStretch()
        status_row.addWidget(self.tuning_stop_button)
        layout.addWidget(status)

        body = QHBoxLayout()
        body.setSpacing(14)
        convergence = Panel("目标量收敛", "束流强度 / μA")
        self.tuning_plot = SpectrumPlot("迭代次数", "束流强度 / μA")
        convergence.body.addWidget(self.tuning_plot)

        metrics = QVBoxLayout()
        self.current_card = MetricCard("当前值", "--", "实时测量")
        self.best_card = MetricCard("最佳值", "--", "初始值 8.31 μA", True)
        self.gain_card = MetricCard("相对提升", "--", "相对初始值", True)
        metrics.addWidget(self.current_card)
        metrics.addWidget(self.best_card)
        metrics.addWidget(self.gain_card)
        metrics.addStretch()
        body.addWidget(convergence, 3)
        body.addLayout(metrics, 1)
        layout.addLayout(body, 1)
        return tab

    def _result_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(14)

        notice = QFrame(objectName="noticePanel")
        notice_layout = QHBoxLayout(notice)
        self.result_notice = QLabel("优化结果待生成")
        notice_layout.addWidget(self.result_notice)
        notice_layout.addStretch()
        self.result_detail = QLabel("", objectName="mutedText")
        notice_layout.addWidget(self.result_detail)
        layout.addWidget(notice)

        comparison = QHBoxLayout()
        comparison.setSpacing(14)
        comparison.addWidget(MetricCard("优化前", "8.31 μA", "任务开始时的稳定测量值"))
        self.result_card = MetricCard("最佳结果", "--", "等待任务完成", True)
        comparison.addWidget(self.result_card)
        layout.addLayout(comparison)

        body = QHBoxLayout()
        body.setSpacing(14)
        changes = Panel("参数变化", "初始值 → 最佳值")
        changes_table = QTableWidget(5, 3)
        changes_table.setHorizontalHeaderLabels(("参数", "优化前", "最佳值"))
        values = (
            ("Q1 电流", "1.842 A", "1.981 A"),
            ("Q2 电流", "-0.625 A", "-0.706 A"),
            ("Einzel 电压", "3.20 kV", "3.41 kV"),
            ("X 偏转", "0.08 V", "0.15 V"),
            ("Y 偏转", "-0.12 V", "-0.07 V"),
        )
        for row, row_values in enumerate(values):
            for column, value in enumerate(row_values):
                changes_table.setItem(row, column, QTableWidgetItem(value))
        changes_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        changes_table.verticalHeader().setVisible(False)
        changes_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        changes.body.addWidget(changes_table)

        record = Panel("确认记录", "随任务保存")
        self.reviewed_checkbox = QCheckBox("已核对设备回读值与安全边界")
        self.reviewed_checkbox.setChecked(False)
        record.body.addWidget(self.reviewed_checkbox)
        record.body.addWidget(QLabel("实验备注", objectName="mutedText"))
        record.body.addWidget(QTextEdit("束流稳定，采用最佳参数。"))
        self.apply_status = QLabel("尚未应用结果", objectName="mutedText")
        record.body.addWidget(self.apply_status)
        body.addWidget(changes, 2)
        body.addWidget(record, 1)
        layout.addLayout(body, 1)

        actions = QHBoxLayout()
        actions.addStretch()
        restore = QPushButton("恢复优化前参数", objectName="dangerButton")
        restore.clicked.connect(self.restore_parameters)
        save = QPushButton("保存结果但不应用")
        save.clicked.connect(lambda: self.apply_status.setText("结果已保存（模拟）"))
        self.apply_button = primary_button("应用最佳参数并完成")
        self.apply_button.setEnabled(False)
        self.reviewed_checkbox.toggled.connect(self.apply_button.setEnabled)
        self.apply_button.clicked.connect(self.apply_best_parameters)
        actions.addWidget(restore)
        actions.addWidget(save)
        actions.addWidget(self.apply_button)
        layout.addLayout(actions)
        return tab

    def start_tuning(self) -> None:
        enabled_count = self._validate_tuning_parameters()
        if enabled_count is None:
            return
        self._target_iterations = self.iterations.value()
        estimated_seconds = self._target_iterations * self.settle.value()
        answer = QMessageBox.question(
            self,
            "确认开始自动调束",
            f"将优化 {enabled_count} 个参数，共 {self._target_iterations} 次迭代，"
            f"预计稳定等待 {estimated_seconds:.0f} 秒。\n\n"
            "当前为模拟运行，不会写入真实设备。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._iteration = 0
        self._values = []
        self.reviewed_checkbox.setChecked(False)
        self.apply_status.setText("尚未应用结果")
        self.tuning_plot.set_data([], [])
        self.tuning_plot.set_ranges(1, self._target_iterations, 0.0, 22.0)
        self.tuning_progress.setValue(0)
        self.tabs.setTabEnabled(1, True)
        self.tabs.setTabEnabled(2, False)
        self.tabs.setTabEnabled(0, False)
        self.tabs.setCurrentIndex(1)
        self.tuning_state.setText("正在优化")
        self.tuning_stop_button.setEnabled(True)
        self.iteration_label.setText(f"第 0 / {self._target_iterations} 次迭代")
        self._timer.setInterval(round(self.settle.value() * 1000))
        self._timer.start()

    def _next_iteration(self) -> None:
        self._iteration += 1
        trend = 8.31 + 10.9 * (1 - math.exp(-self._iteration / 10))
        value = trend + 0.7 * math.sin(self._iteration * 1.7) + random.uniform(-0.25, 0.25)
        if self._iteration == min(36, self._target_iterations):
            value = 19.08
        self._values.append(value)
        self.tuning_plot.set_data(range(1, self._iteration + 1), self._values)
        best = max(self._values)
        self.current_card.value_label.setText(f"{value:.2f} μA")
        self.best_card.value_label.setText(f"{best:.2f} μA")
        self.gain_card.value_label.setText(f"+{(best / 8.31 - 1) * 100:.1f}%")
        self.iteration_label.setText(
            f"第 {self._iteration} / {self._target_iterations} 次迭代"
        )
        self.tuning_progress.setValue(round(self._iteration / self._target_iterations * 100))
        if self._iteration >= self._target_iterations:
            self._timer.stop()
            self.tuning_state.setText("优化正常完成")
            self.tuning_stop_button.setEnabled(False)
            best_index = max(range(len(self._values)), key=self._values.__getitem__)
            best = self._values[best_index]
            gain = (best / 8.31 - 1) * 100
            self.result_notice.setText("优化正常完成 · 无安全告警（模拟）")
            self.result_detail.setText(f"最佳结果出现在第 {best_index + 1} 次迭代")
            self.result_card.value_label.setText(f"{best:.2f} μA")
            self.tuning_plot.annotate_peaks(1)
            if self.result_card.detail_label is not None:
                self.result_card.detail_label.setText(f"提升 {gain:.1f}%")
            self.tabs.setTabEnabled(2, True)
            self.tabs.setTabEnabled(0, True)
            self.tabs.setCurrentIndex(2)

    def stop_tuning(self) -> None:
        if not self._timer.isActive():
            return
        self._timer.stop()
        self.tuning_state.setText("已安全停止，未应用参数")
        self.tuning_stop_button.setEnabled(False)
        self.tabs.setTabEnabled(0, True)

    def _validate_tuning_parameters(self) -> int | None:
        enabled_count = 0
        for row in range(self.parameter_table.rowCount()):
            enabled = self.parameter_table.item(row, 0).checkState() == Qt.CheckState.Checked
            if not enabled:
                continue
            enabled_count += 1
            try:
                lower = float(self.parameter_table.item(row, 3).text())
                upper = float(self.parameter_table.item(row, 4).text())
                step = float(self.parameter_table.item(row, 5).text())
            except ValueError:
                QMessageBox.warning(self, "参数格式错误", f"第 {row + 1} 行包含无效数字。")
                self.parameter_table.setCurrentCell(row, 3)
                return None
            if lower >= upper or step <= 0:
                QMessageBox.warning(
                    self,
                    "参数范围错误",
                    f"第 {row + 1} 行必须满足下限 < 上限，且步长大于 0。",
                )
                self.parameter_table.setCurrentCell(row, 3)
                return None
        if enabled_count == 0:
            QMessageBox.warning(self, "没有优化变量", "请至少启用一个设备参数。")
            return None
        return enabled_count

    def restore_parameters(self) -> None:
        answer = QMessageBox.question(
            self,
            "确认恢复参数",
            "将恢复任务开始前的参数快照。当前为模拟操作，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.apply_status.setText("已恢复优化前参数（模拟）")

    def apply_best_parameters(self) -> None:
        answer = QMessageBox.question(
            self,
            "确认应用最佳参数",
            "将应用表格中的最佳参数。当前为模拟操作，是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.apply_status.setText("最佳参数已应用（模拟）")


# 系统设置页使用的默认服务地址（开发基线，对应 README 的启动方式）。
DEFAULT_SERVICE_URLS = {
    "data": "http://127.0.0.1:8000",
    "instrument": "http://127.0.0.1:8765",
}

# PV 映射预览表（模拟阶段示例，与自动调束参数页一致）。
# 方案文档 6.2：真实阶段由受控设备配置提供“业务信号 → PV”映射，
# 执行服务启动时完整校验；运行中不允许普通用户随意输入任意 PV 名。
PV_MAPPING = (
    ("Q1 电流", "BL:Q1:ISET", "A", "设定与读回"),
    ("Q2 电流", "BL:Q2:ISET", "A", "设定与读回"),
    ("Einzel 电压", "BL:EL:VSET", "kV", "设定与读回"),
    ("X 偏转", "BL:STEER:X", "V", "设定与读回"),
    ("Y 偏转", "BL:STEER:Y", "V", "设定与读回"),
    ("Source 电压", "BL:SRC:VSET", "kV", "设定与读回"),
)

LOG_LEVELS = (("调试", "debug"), ("信息", "info"), ("警告", "warning"), ("错误", "error"))


def _valid_service_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def _probe_service(base_url: str, path: str) -> tuple[bool, str]:
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=0.9) as response:
            if response.status != 200:
                return False, f"HTTP {response.status}"
            payload = json.loads(response.read().decode("utf-8"))
            status = payload.get("status")
            if isinstance(status, str):
                return True, f"status={status}"
            return False, "缺少 status 字段"
    except Exception as exc:  # 连接类错误统一展示，不区分细节
        return False, str(exc)


class ConnectionProbeThread(QThread):
    """在线程中顺序探测服务，避免阻塞 Qt 主线程。"""

    probeFinished = Signal(bool, str)

    def __init__(self, probes: tuple[tuple[str, str, str], ...], parent=None) -> None:
        super().__init__(parent)
        self._probes = probes

    def run(self) -> None:
        for name, base_url, path in self._probes:
            ok, message = _probe_service(base_url, path)
            if not ok:
                self.probeFinished.emit(False, f"{name} 不可达：{message}")
                return
        self.probeFinished.emit(True, "两个服务均响应正常。")


class SystemSettingsPage(QWidget):
    """系统设置：服务连接、PV 映射、日志与权限、外观。

    对应方案文档中的配置边界：客户端不直接访问 PostgreSQL 或任意 PV，
    只保存本机连接偏好；设备配置（PV 映射）在真实接入阶段由受控配置提供。
    """

    themeChanged = Signal(str)
    motionPreferenceChanged = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._settings = QSettings("SpectrumPlatform", "DesktopClient")
        self._probe_thread: ConnectionProbeThread | None = None
        layout = page_layout(self)
        layout.addWidget(
            PageHeading("系统设置", "配置服务连接、查看 PV 映射、设定日志级别与外观。")
        )
        section_names = ("服务与连接", "PV 映射", "日志与权限", "外观")
        self.settings_selector = QComboBox(objectName="settingsSelector")
        self.settings_selector.addItems(section_names)
        self.settings_selector.setVisible(False)
        layout.addWidget(self.settings_selector)

        content = QWidget()
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(14)
        self.settings_nav = QListWidget(objectName="settingsNavigation")
        self.settings_nav.setFixedWidth(180)
        for name in section_names:
            self.settings_nav.addItem(QListWidgetItem(name))
        self.settings_nav.setCurrentRow(0)
        self.settings_stack = QStackedWidget(objectName="settingsStack")
        for page in (
            self._build_service_tab(),
            self._build_pv_tab(),
            self._build_log_tab(),
            self._build_appearance_tab(),
        ):
            self.settings_stack.addWidget(page)
        self.settings_nav.currentRowChanged.connect(self._select_settings_section)
        self.settings_selector.currentIndexChanged.connect(self._select_settings_section)
        content_layout.addWidget(self.settings_nav)
        content_layout.addWidget(self.settings_stack, 1)
        layout.addWidget(content, 1)

    def resizeEvent(self, event) -> None:  # noqa: N802
        """窄窗口使用顶部选择器，避免主侧栏与设置侧栏同时挤压内容。"""
        super().resizeEvent(event)
        compact = self.width() < 900
        self.settings_nav.setVisible(not compact)
        self.settings_selector.setVisible(compact)

    def _select_settings_section(self, index: int) -> None:
        if index < 0:
            return
        self.settings_stack.setCurrentIndex(index)
        if self.settings_nav.currentRow() != index:
            self.settings_nav.setCurrentRow(index)
        if self.settings_selector.currentIndex() != index:
            self.settings_selector.setCurrentIndex(index)

    def showEvent(self, event) -> None:  # noqa: N802
        """切到本页时把主题单选钮与当前主题同步。"""
        super().showEvent(event)
        self._sync_theme_radio()

    def sync_theme_radio(self) -> None:
        """外部（侧栏按钮）切换主题后同步页内单选钮状态。"""
        self._sync_theme_radio()

    # ---------- 服务与连接 ----------

    def _build_service_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(14)

        panel = Panel("服务地址", "客户端只通过 API 访问两个本机/内网服务")
        form = QFormLayout()
        self.data_url = QLineEdit()
        self.instrument_url = QLineEdit()
        form.addRow("数据服务地址", self.data_url)
        form.addRow("仪器执行地址", self.instrument_url)
        panel.body.addLayout(form)
        hint = QLabel("开发基线：数据服务 127.0.0.1:8000，仪器执行 127.0.0.1:8765。")
        hint.setWordWrap(True)
        panel.body.addWidget(hint)
        layout.addWidget(panel)

        actions = QHBoxLayout()
        self.feedback_label = QLabel("", objectName="mutedText")
        self.feedback_label.setWordWrap(True)
        actions.addWidget(self.feedback_label, 1)
        restore_defaults = QPushButton("恢复默认")
        restore_defaults.clicked.connect(self._restore_default_urls)
        self.test_button = QPushButton("测试连接")
        self.test_button.clicked.connect(self._test_connections)
        save_button = QPushButton("保存设置", objectName="primaryButton")
        save_button.clicked.connect(self._save_service_urls)
        actions.addWidget(restore_defaults)
        actions.addWidget(self.test_button)
        actions.addWidget(save_button)
        layout.addLayout(actions)
        layout.addStretch()

        self.data_url.setText(
            str(self._settings.value("service/dataUrl", DEFAULT_SERVICE_URLS["data"]))
        )
        self.instrument_url.setText(
            str(self._settings.value("service/instrumentUrl", DEFAULT_SERVICE_URLS["instrument"]))
        )
        return tab

    def _save_service_urls(self) -> None:
        data_url = self.data_url.text().strip()
        instrument_url = self.instrument_url.text().strip()
        if not _valid_service_url(data_url):
            self._flash_feedback(False, "数据服务地址无效，请输入 http(s)://主机:端口。")
            self.data_url.setFocus()
            return
        if not _valid_service_url(instrument_url):
            self._flash_feedback(False, "仪器执行地址无效，请输入 http(s)://主机:端口。")
            self.instrument_url.setFocus()
            return
        self._settings.setValue("service/dataUrl", data_url)
        self._settings.setValue("service/instrumentUrl", instrument_url)
        self._flash_feedback(True, "已保存服务地址（本机连接偏好）。")

    def _restore_default_urls(self) -> None:
        self.data_url.setText(DEFAULT_SERVICE_URLS["data"])
        self.instrument_url.setText(DEFAULT_SERVICE_URLS["instrument"])
        self._flash_feedback(True, "已恢复开发默认值，点击“保存设置”生效。")

    def _test_connections(self) -> None:
        """在线程中探测两个服务的存活接口。"""
        if self._probe_thread is not None and self._probe_thread.isRunning():
            return
        probes = (
            ("数据服务", self.data_url.text().strip(), "/api/v1/health/live"),
            ("仪器执行服务", self.instrument_url.text().strip(), "/control/v1/status"),
        )
        for name, base_url, _path in probes:
            if not _valid_service_url(base_url):
                self._flash_feedback(False, f"{name}地址无效，请先修正。")
                return
        self.test_button.setEnabled(False)
        self.test_button.setText("正在测试…")
        self._set_feedback("idle", "正在连接两个服务…")
        self._probe_thread = ConnectionProbeThread(probes, self)
        self._probe_thread.probeFinished.connect(self._flash_feedback)
        self._probe_thread.finished.connect(self._finish_connection_test)
        self._probe_thread.start()

    def _finish_connection_test(self) -> None:
        self.test_button.setEnabled(True)
        self.test_button.setText("测试连接")
        if self._probe_thread is not None:
            self._probe_thread.deleteLater()
            self._probe_thread = None

    def _flash_feedback(self, ok: bool, message: str) -> None:
        self._set_feedback("good" if ok else "error", message)

    def _set_feedback(self, state: str, message: str) -> None:
        self.feedback_label.setText(message)
        self.feedback_label.setProperty("state", state)
        self.feedback_label.style().unpolish(self.feedback_label)
        self.feedback_label.style().polish(self.feedback_label)

    # ---------- PV 映射 ----------

    def _build_pv_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(14)

        panel = Panel("业务信号 → PV 映射（示例）", "自动调束参数")
        table = QTableWidget(len(PV_MAPPING), 4)
        table.setHorizontalHeaderLabels(("设备参数", "PV 名称", "单位", "用途"))
        for row, (name, pv, unit, usage) in enumerate(PV_MAPPING):
            for column, value in enumerate((name, pv, unit, usage)):
                table.setItem(row, column, QTableWidgetItem(value))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setAlternatingRowColors(True)
        panel.body.addWidget(table)
        note = QLabel(
            "说明：本表为模拟阶段的示例 PV，当前不会写入真实设备。按方案文档 6.2，"
            "真实接入时由受控设备配置提供“业务信号 → PV”映射并在执行服务启动时完整校验；"
            "运行中不允许普通用户随意输入任意 PV 名。扫谱采集 PV 清单待设备联调确定。"
        )
        note.setWordWrap(True)
        panel.body.addWidget(note)
        layout.addWidget(panel, 1)
        return tab

    # ---------- 日志与权限 ----------

    def _build_log_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(14)

        log_panel = Panel("日志")
        form = QFormLayout()
        self.log_level = QComboBox()
        for display, _key in LOG_LEVELS:
            self.log_level.addItem(display)
        self.log_level.currentIndexChanged.connect(self._save_log_level)
        form.addRow("记录级别", self.log_level)
        log_panel.body.addLayout(form)
        hint = QLabel("先保存级别偏好；客户端日志组件接入后按该级别输出。")
        hint.setWordWrap(True)
        log_panel.body.addWidget(hint)
        layout.addWidget(log_panel)

        access_panel = Panel("操作权限")
        current_user = QLabel("当前会话：操作员 · 本机（演示账号）", objectName="serviceValue")
        access_panel.body.addWidget(current_user)
        note = QLabel(
            "说明：数据服务接入登录认证后，按角色（操作员 / 分析员 / 管理员）下发访问矩阵，"
            "本页将展示实际权限；分析端不授予硬件控制权限，数据库凭据不进入客户端。"
        )
        note.setWordWrap(True)
        access_panel.body.addWidget(note)
        layout.addWidget(access_panel)
        layout.addStretch()

        saved = str(self._settings.value("logging/level", "info"))
        for index, (_display, key) in enumerate(LOG_LEVELS):
            if key == saved:
                self.log_level.setCurrentIndex(index)
                break
        return tab

    def _save_log_level(self, index: int) -> None:
        self._settings.setValue("logging/level", LOG_LEVELS[index][1])

    # ---------- 外观 ----------

    def _build_appearance_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(14)

        panel = Panel("外观", "切换即时生效，重启后保持")
        self._theme_radio_group = QButtonGroup(self)
        self._theme_radios: list[tuple[QRadioButton, str]] = []
        for index, (label, name) in enumerate(
            (("浅色（默认）", "light"), ("深色", "dark"))
        ):
            radio = QRadioButton(label)
            self._theme_radio_group.addButton(radio, index)
            panel.body.addWidget(radio)
            self._theme_radios.append((radio, name))
        self._theme_radio_group.idClicked.connect(self._emit_theme_change)
        self.reduce_motion = QCheckBox("减少动态效果")
        self.reduce_motion.setChecked(
            self._settings.value("appearance/reduceMotion", False, type=bool)
        )
        self.reduce_motion.setToolTip("关闭循环脉冲、扫光和页面过渡")
        self.reduce_motion.toggled.connect(self._set_reduce_motion)
        panel.body.addWidget(self.reduce_motion)
        note = QLabel("说明：外观偏好属于用户级设置，保存在本机用户配置中。")
        note.setWordWrap(True)
        panel.body.addWidget(note)
        layout.addWidget(panel)
        layout.addStretch()

        self._sync_theme_radio()
        return tab

    def _emit_theme_change(self, button_id: int) -> None:
        self.themeChanged.emit(self._theme_radios[button_id][1])

    def _set_reduce_motion(self, reduced: bool) -> None:
        self._settings.setValue("appearance/reduceMotion", reduced)
        self.motionPreferenceChanged.emit(reduced)

    def _sync_theme_radio(self) -> None:
        current = theme_name()
        for radio, name in self._theme_radios:
            radio.setChecked(name == current)
