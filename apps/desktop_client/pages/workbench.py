"""工作台页面：以实验任务为中心的主入口页。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from apps.desktop_client.pages.common import page_layout, primary_button
from apps.desktop_client.pages.registry import PageSpec
from apps.desktop_client.surfaces import GlassCard
from apps.desktop_client.theme import current_palette
from apps.desktop_client.widgets import ClickableLabel, PageHeading, Panel


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


PAGE_SPEC = PageSpec(
    key="workbench",
    label="工作台",
    icon="dashboard",
    section="control",
    factory=WorkbenchPage,
)
