"""自动调束页面：F1 参数表、G1 收敛监控和 H1 前后对比。"""

from __future__ import annotations

import math
import random

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from apps.desktop_client.pages.common import page_layout, primary_button
from apps.desktop_client.pages.registry import PageSpec
from apps.desktop_client.spectrum_plot import SpectrumPlot
from apps.desktop_client.widgets import MetricCard, PageHeading, Panel


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


PAGE_SPEC = PageSpec(
    key="tuning",
    label="自动调束",
    icon="tuning",
    section="control",
    factory=TuningPage,
)
