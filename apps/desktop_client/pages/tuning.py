"""自动调束页：建议 → 人工确认 → 执行，全过程跟踪。

与旧版的根本区别：旧版在页面里用一条公式加噪声"演"出收敛曲线，从不碰设备。
现在页面对接执行服务，走架构文档 6.6 规定的链路：

    优化器提候选 → 人工确认 → 执行层校验(边界/单步/速率) → 写设备
    → 等读回稳定 → 测目标 → 记录本轮

两个刻意的设计：

* **模式固定为「建议 → 人工确认」**。第一版不提供连续自动写入（文档 6.6 的
  分阶段计划），界面只展示模式、不给切换开关——避免把"要不要自动写设备"
  做成一个随手可点的复选框。
* **候选值不等于已执行值**。每轮分别记录建议值、实际下发值、实际回读值与
  目标测量；只显示建议值会让人误以为设备已经动过了（文档 9.4）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from apps.desktop_client import instrument_api
from apps.desktop_client.pages.common import page_layout, primary_button
from apps.desktop_client.pages.registry import PageSpec
from apps.desktop_client.pv_mapping_api import PvMappingRequestThread
from apps.desktop_client.spectrum_plot import SpectrumPlot
from apps.desktop_client.widgets import MetricCard, PageHeading, Panel

POLL_INTERVAL_MS = 700
DEFAULT_TARGET = "detector.fc1.beam_current"
TERMINAL_STATES = frozenset({"completed", "aborted", "failed", "recovery_required"})
STATE_TEXT = {
    "draft": "待提交",
    "validating": "校验参数",
    "preparing": "申请设备",
    "running": "调束进行中",
    "awaiting_confirmation": "等待人工确认候选",
    "applying": "写入设备并等待稳定",
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
        self._mapping: list[dict] = []
        self._rows: list[dict] = []
        self._run_id: str | None = None
        self._state = "idle"
        self._pending: dict | None = None
        self._iterations: list[dict] = []
        self._status_in_flight = False
        self._read_in_flight = False
        self._best_line = None

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
        self.parameter_table = QTableWidget(0, 6)
        self.parameter_table.setHorizontalHeaderLabels(
            ["启用", "设备参数", "PV", "下限", "上限", "当前回读"]
        )
        self.parameter_table.verticalHeader().setVisible(False)
        variables.body.addWidget(self.parameter_table, 1)
        self.variable_hint = QLabel("正在读取设备参数…", objectName="mutedText")
        self.variable_hint.setWordWrap(True)
        variables.body.addWidget(self.variable_hint)
        splitter.addWidget(variables)

        strategy = Panel("目标与策略", "贝叶斯优化（GP + EI）")
        form = QVBoxLayout()
        form.setSpacing(8)

        form.addWidget(QLabel("优化目标（最大化）", objectName="mutedText"))
        self.target = QComboBox()
        self.target.currentIndexChanged.connect(lambda _i: self._refresh_readbacks())
        form.addWidget(self.target)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("模式", objectName="mutedText"))
        mode_label = QLabel("建议 → 人工确认（第一版固定）")
        mode_label.setObjectName("modeChip")
        mode_row.addWidget(mode_label)
        mode_row.addStretch()
        form.addLayout(mode_row)

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

        for widget in (self.iterations_spin, self.settle_spin, self.samples_spin):
            widget.setMaximumWidth(170)

        form.addStretch()
        hint = QLabel(
            "范围必须落在设备允许区间内，且参数需配置最大单步；否则启动时会被拒绝——"
            "让优化器提出一个必然写不进去的值没有意义。",
            objectName="mutedText",
        )
        hint.setWordWrap(True)
        form.addWidget(hint)
        self.start_button = primary_button("开始自动调束")
        self.start_button.clicked.connect(self.start_tuning)
        form.addWidget(self.start_button)
        strategy.body.addLayout(form)
        splitter.addWidget(strategy)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([980, 300])
        outer.addWidget(splitter)
        return tab

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
        self.tuning_stop_button = QPushButton("停止", objectName="dangerButton")
        self.tuning_stop_button.setEnabled(False)
        self.tuning_stop_button.clicked.connect(self.stop_tuning)
        row.addWidget(self.tuning_state)
        row.addWidget(self.iteration_label)
        row.addStretch()
        row.addWidget(self.acknowledge_button)
        row.addWidget(self.tuning_stop_button)
        outer.addWidget(bar)

        # 指标横排成一条紧凑信息带，避免占用曲线右侧整列空间。
        metrics = QHBoxLayout()
        metrics.setSpacing(10)
        self.current_card = MetricCard("本轮测量", "--")
        self.best_card = MetricCard("历史最优", "--", "", success=True)
        self.gain_card = MetricCard("相对提升", "--")
        for card in (self.current_card, self.best_card, self.gain_card):
            card.setMaximumHeight(92)
            metrics.addWidget(card, 1)
        outer.addLayout(metrics)

        plot_panel = Panel("目标量收敛", "x = 轮次，y = 目标测量")
        self.tuning_plot = SpectrumPlot("轮次", "目标量")
        plot_panel.body.addWidget(self.tuning_plot, 1)
        outer.addWidget(plot_panel, 1)

        confirm = QFrame(objectName="noticePanel")
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
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 8, 0, 0)
        outer.setSpacing(10)

        notice = QFrame(objectName="noticePanel")
        notice_row = QHBoxLayout(notice)
        notice_row.setContentsMargins(14, 10, 14, 10)
        self.result_notice = QLabel("尚未完成任何调束")
        self.result_detail = QLabel("", objectName="mutedText")
        notice_row.addWidget(self.result_notice)
        notice_row.addStretch()
        notice_row.addWidget(self.result_detail)
        outer.addWidget(notice)

        # 必须是实例属性：旧版把它写成局部变量，真实结果根本回填不进去
        self.changes_table = QTableWidget(0, 4)
        self.changes_table.setHorizontalHeaderLabels(
            ["参数", "初始回读", "最优回读", "变化"]
        )
        self.changes_table.verticalHeader().setVisible(False)
        changes = Panel("参数变化", "来自每轮的实际回读，不用建议值")
        changes.body.addWidget(self.changes_table, 1)
        outer.addWidget(changes, 1)
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

    def _build_variable_rows(self) -> None:
        candidates = [
            entry
            for entry in self._mapping
            if entry.get("writable") and entry.get("role") == "setpoint"
        ]
        self.parameter_table.setRowCount(len(candidates))
        self._rows = []
        for row, entry in enumerate(candidates):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            check.setCheckState(Qt.CheckState.Unchecked)
            self.parameter_table.setItem(row, 0, check)
            self.parameter_table.setItem(
                row, 1, QTableWidgetItem(str(entry.get("label", entry.get("signal"))))
            )
            self.parameter_table.setItem(
                row, 2, QTableWidgetItem(str(entry.get("pv", "")))
            )

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
            self.parameter_table.setCellWidget(row, 3, low_spin)
            self.parameter_table.setCellWidget(row, 4, high_spin)
            readback = QLabel("--")
            readback.setObjectName("mutedText")
            self.parameter_table.setCellWidget(row, 5, readback)
            self._rows.append(
                {
                    "entry": entry,
                    "check": check,
                    "low": low_spin,
                    "high": high_spin,
                    "readback": readback,
                }
            )
        self.parameter_table.resizeColumnsToContents()
        usable = sum(1 for r in self._rows if r["entry"].get("max_step"))
        self.variable_hint.setProperty("state", "")
        self.variable_hint.setText(
            f"共 {len(self._rows)} 个可调参数，其中 {usable} 个配置了最大单步、可用于调束。"
            "勾选并设定范围后开始。"
        )
        self.variable_hint.style().unpolish(self.variable_hint)
        self.variable_hint.style().polish(self.variable_hint)

    def _build_targets(self) -> None:
        self.target.clear()
        for entry in self._mapping:
            if entry.get("writable") or entry.get("role") != "readback":
                continue
            self.target.addItem(
                f"{entry.get('label', entry.get('signal'))}（{entry.get('unit', '')}）",
                str(entry.get("signal")),
            )
        for index in range(self.target.count()):
            if self.target.itemData(index) == DEFAULT_TARGET:
                self.target.setCurrentIndex(index)
                break

    def _refresh_readbacks(self) -> None:
        if self._read_in_flight or not self._rows:
            return
        signals = [r["entry"]["signal"] for r in self._rows]
        target = self.target.currentData()
        if target:
            signals.append(target)
        self._read_in_flight = True
        thread = instrument_api.request_read(signals=signals)
        thread.completed.connect(self._on_readbacks)

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
                    "enabled": True,
                }
            )
        return selected

    def start_tuning(self) -> None:
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

        self._iterations = []
        self.tuning_plot.set_data([], [])
        self.start_button.setEnabled(False)
        self._status_in_flight = True
        thread = instrument_api.request_tuning_start(
            {
                "target_signal": target,
                "variables": variables,
                "mode": "confirm",
                "max_iterations": self.iterations_spin.value(),
                "settle_timeout_s": self.settle_spin.value(),
                "samples_per_point": self.samples_spin.value(),
            }
        )
        thread.completed.connect(self._on_started)

    def _complain(self, message: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("无法开始调束")
        box.setText(message)
        box.exec()

    def _on_started(self, payload: dict) -> None:
        self._status_in_flight = False
        if not payload.get("ok"):
            self.start_button.setEnabled(True)
            self._complain(f"启动失败：{payload.get('message', '')}")
            return
        status = payload.get("payload") or {}
        self._run_id = status.get("run_id")
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
            short = signal.split(".")[-1]
            self.proposal_table.setItem(row, 0, QTableWidgetItem(short))
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
        self.approve_button.setEnabled(False)
        self.proposal_label.setText("正在写入设备并等待读回稳定…")
        thread = instrument_api.request_tuning_approve(self._run_id)
        thread.completed.connect(self._on_approved)

    def _on_approved(self, payload: dict) -> None:
        if not payload.get("ok"):
            self.proposal_label.setText(f"执行失败：{payload.get('message', '')}")
            self.approve_button.setEnabled(True)
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

    def _apply_status(self, status: dict) -> None:
        self._state = str(status.get("state", "idle"))
        completed = int(status.get("completed_iterations") or 0)
        self.iteration_label.setText(
            f"已完成 {completed} / {status.get('max_iterations', 0)} 轮"
        )
        text = STATE_TEXT.get(self._state, self._state)
        message = status.get("message") or ""
        if message:
            text = f"{text} · {message}"
        self.tuning_state.setText(text)

        self._pending = status.get("pending")
        if self._pending:
            self.proposal_label.setText(
                f"第 {int(self._pending['iteration']) + 1} 轮候选"
                f"（预测 {float(self._pending['predicted']):.3f}"
                f" ± {float(self._pending['std']):.3f}）"
            )
            self._fill_proposal_table(self._pending)
            self.proposal_table.setVisible(True)
            self.approve_button.setEnabled(True)
        else:
            self.proposal_label.setText(
                "本轮已执行，正在生成下一轮候选…"
                if self._state not in TERMINAL_STATES
                else "无待确认候选。"
            )
            self.approve_button.setEnabled(False)

        if self._state == "recovery_required":
            self.acknowledge_button.setVisible(True)

        if self._state in TERMINAL_STATES:
            self._timer.stop()
            self.start_button.setEnabled(True)
            self.tuning_stop_button.setEnabled(False)
            self.approve_button.setEnabled(False)
            self.tabs.setTabEnabled(2, True)
            if self._state == "completed":
                self.result_notice.setText("调束已完成")
                self.tabs.setCurrentIndex(2)
            elif self._state == "aborted":
                self.result_notice.setText("调束已停止")
            else:
                self.result_notice.setText(text)
            self._fetch_iterations()

    def _fetch_iterations(self) -> None:
        if not self._run_id:
            return
        thread = instrument_api.request_tuning_iterations(self._run_id)
        thread.completed.connect(self._on_iterations)

    def _on_iterations(self, payload: dict) -> None:
        if not payload.get("ok"):
            return
        self._iterations = list(
            (payload.get("payload") or {}).get("iterations") or []
        )
        self._redraw()
        self._fill_changes()

    def _redraw(self) -> None:
        usable = [it for it in self._iterations if it.get("objective") is not None]
        if not usable:
            self.tuning_plot.set_data([], [])
            return
        xs = [float(it["iteration"]) + 1 for it in usable]
        ys = [float(it["objective"]) for it in usable]
        self.tuning_plot.set_data(xs, ys)
        self.current_card.value_label.setText(f"{ys[-1]:.3f}")
        best = max(ys)
        self.best_card.value_label.setText(f"{best:.3f}")
        self.gain_card.value_label.setText(f"{best - ys[0]:+.3f}")
        self.result_detail.setText(
            f"共 {len(self._iterations)} 轮，其中 {len(usable)} 轮有有效目标测量"
        )
        self._show_best_line(best)

    def _show_best_line(self, best: float) -> None:
        """在收敛曲线上画一条虚线表示历史最佳，一眼看出是否还在提升。"""

        import pyqtgraph as pg

        from apps.desktop_client.theme import current_palette

        if not hasattr(self, "_best_line") or self._best_line is None:
            # 注意：InfiniteLine 不接受 dash= 关键字（pyqtgraph 会抛 TypeError）。
            # 虚线由下面 setPen 的 DashLine 样式给，dash 参数是多余的。
            self._best_line = pg.InfiniteLine(angle=0, movable=False)
            self.tuning_plot.view.addItem(self._best_line, ignoreBounds=True)
        self._best_line.setPos(best)
        tokens = current_palette()
        self._best_line.setPen(
            pg.mkPen(tokens["statusGood"], width=1.5, style=Qt.PenStyle.DashLine)
        )

    def _fill_changes(self) -> None:
        """参数变化表只用**实际回读值**：建议值不代表设备真的到过那里。"""
        usable = [it for it in self._iterations if it.get("objective") is not None]
        if not usable:
            self.changes_table.setRowCount(0)
            return
        first = self._iterations[0].get("readback") or {}
        best = max(usable, key=lambda it: float(it["objective"])).get("readback") or {}
        keys = sorted(set(first) | set(best))
        self.changes_table.setRowCount(len(keys))
        for row, key in enumerate(keys):
            start = first.get(key)
            end = best.get(key)
            self.changes_table.setItem(row, 0, QTableWidgetItem(key))
            self.changes_table.setItem(
                row, 1, QTableWidgetItem("--" if start is None else f"{start:.3f}")
            )
            self.changes_table.setItem(
                row, 2, QTableWidgetItem("--" if end is None else f"{end:.3f}")
            )
            delta = "--" if start is None or end is None else f"{end - start:+.3f}"
            self.changes_table.setItem(row, 3, QTableWidgetItem(delta))
        self.changes_table.resizeColumnsToContents()

    # ------------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
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
