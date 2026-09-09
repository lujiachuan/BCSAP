"""系统设置页面：服务连接、PV 映射、日志与权限、外观。"""

from __future__ import annotations

from urllib.parse import urlparse

from PySide6.QtCore import QSettings, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from apps.desktop_client.pages.common import page_layout
from apps.desktop_client.pages.registry import PageSpec
from apps.desktop_client.theme import theme_name
from apps.desktop_client.widgets import PageHeading, Panel

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


PAGE_SPEC = PageSpec(
    key="settings",
    label="系统设置",
    icon="settings",
    section="data",
    factory=SystemSettingsPage,
)
