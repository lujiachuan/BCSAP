"""系统设置页面：服务连接、PV 映射、日志与权限、外观。"""

from __future__ import annotations

from urllib.parse import urlparse

from PySide6.QtCore import QSettings, Qt, QThread, Signal
from PySide6.QtGui import QColor
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
from apps.desktop_client.pv_mapping_api import PvMappingRequestThread
from apps.desktop_client.theme import current_palette, theme_name
from apps.desktop_client.widgets import PageHeading, Panel

# 系统设置页使用的默认服务地址（开发基线，对应 README 的启动方式）。
DEFAULT_SERVICE_URLS = {
    "data": "http://127.0.0.1:8000",
    "instrument": "http://127.0.0.1:8765",
}

# 受控设备 PV 映射（业务信号 → 真实 EPICS PV）。
# 方案文档 6.2：映射是执行服务持有的受控配置，客户端只通过 API 读写，
# 不直接访问 IOC；保存由执行服务校验并立即生效。
PV_GATEWAYS = (
    ("模拟 EPICS（无 IOC 的开发/演示）", "simulated"),
    ("真实 EPICS 通道访问（CA）", "channel-access"),
)
# 表格列：设备参数 / 业务信号 / PV 名称 / 单位 / 可写 / 必需
PV_COLUMNS = ("设备参数", "业务信号", "PV 名称", "单位", "可写", "必需")

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
        self._pv_request: PvMappingRequestThread | None = None
        self._pv_loaded = False
        self._pv_config_version = 1
        layout = page_layout(self)
        layout.addWidget(
            PageHeading("系统设置", "配置服务连接、编辑 PV 映射、设定日志级别与外观。")
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
        """切到本页时同步主题单选钮，并在首次显示时拉取 PV 映射。"""
        super().showEvent(event)
        self._sync_theme_radio()
        self._load_pv_mapping()

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

        panel = Panel("业务信号 → PV 映射", "执行服务受控配置 · 保存后立即生效")
        form = QFormLayout()
        self.pv_gateway = QComboBox()
        for display, key in PV_GATEWAYS:
            self.pv_gateway.addItem(display, key)
        self.pv_gateway.setToolTip(
            "模拟模式不连任何设备；真实模式通过 Channel Access 读写现场 IOC。"
        )
        form.addRow("网关模式", self.pv_gateway)
        self.pv_ca_lib_dir = QLineEdit()
        self.pv_ca_lib_dir.setPlaceholderText(
            "留空自动发现；也可填 ca.dll 所在目录"
        )
        form.addRow("CA 库目录", self.pv_ca_lib_dir)
        panel.body.addLayout(form)

        self.pv_table = QTableWidget(0, len(PV_COLUMNS))
        self.pv_table.setHorizontalHeaderLabels(PV_COLUMNS)
        header = self.pv_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.pv_table.verticalHeader().setVisible(False)
        self.pv_table.setAlternatingRowColors(True)
        self.pv_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.pv_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        panel.body.addWidget(self.pv_table)

        actions = QHBoxLayout()
        self.pv_feedback = QLabel("", objectName="mutedText")
        self.pv_feedback.setWordWrap(True)
        actions.addWidget(self.pv_feedback, 1)
        add_row = QPushButton("新增行")
        add_row.clicked.connect(self._add_pv_row)
        remove_row = QPushButton("删除选中行")
        remove_row.clicked.connect(self._remove_pv_rows)
        reload_button = QPushButton("重新载入")
        reload_button.clicked.connect(lambda: self._load_pv_mapping(force=True))
        self.pv_save_button = QPushButton("保存映射", objectName="primaryButton")
        self.pv_save_button.clicked.connect(self._save_pv_mapping)
        for button in (add_row, remove_row, reload_button):
            actions.addWidget(button)
        actions.addWidget(self.pv_save_button)
        panel.body.addLayout(actions)

        note = QLabel(
            "说明：这里的 PV 就是现场 IOC 上的真实 PV 名，保存后健康检查、调束与扫谱"
            "都会按新映射执行；「可写」决定允许下发设定值，「必需」决定该 PV 掉线时"
            "是否判定设备不可用。真实模式下写入仍受参数边界与设备联锁约束。"
        )
        note.setWordWrap(True)
        panel.body.addWidget(note)
        layout.addWidget(panel, 1)
        return tab

    # ---- PV 映射：载入与渲染 ----

    def _instrument_base_url(self) -> str:
        """优先用界面上未保存的输入，其次用已保存的服务地址。"""
        typed = self.instrument_url.text().strip()
        if typed:
            return typed
        return str(
            self._settings.value("service/instrumentUrl", DEFAULT_SERVICE_URLS["instrument"])
        )

    def _load_pv_mapping(self, force: bool = False) -> None:
        """从执行服务拉取当前映射；默认只在首次显示时拉，避免覆盖未保存的编辑。"""
        if self._pv_request is not None and self._pv_request.isRunning():
            return
        if self._pv_loaded and not force:
            return
        self._set_pv_feedback("idle", "正在读取 PV 映射…")
        self._start_pv_request(payload=None)

    def _start_pv_request(self, payload: dict | None) -> None:
        self.pv_save_button.setEnabled(False)
        self._pv_request = PvMappingRequestThread(self._instrument_base_url(), payload, self)
        self._pv_request.completed.connect(self._finish_pv_request)
        self._pv_request.finished.connect(self._release_pv_request)
        self._pv_request.start()

    def _release_pv_request(self) -> None:
        self.pv_save_button.setEnabled(True)
        if self._pv_request is not None:
            self._pv_request.deleteLater()
            self._pv_request = None

    def _finish_pv_request(self, result: dict) -> None:
        if not result["ok"]:
            self._mark_pv_issues(result["issues"])
            self._set_pv_feedback("error", result["message"])
            return
        self._render_pv_mapping(result["config"])
        self._pv_loaded = True

    def _render_pv_mapping(self, config: dict) -> None:
        entries = config.get("entries", [])
        self._pv_config_version = int(config.get("version", 1))
        self.pv_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self._set_pv_text(row, 0, entry.get("label", ""))
            self._set_pv_text(row, 1, entry.get("signal", ""))
            self._set_pv_text(row, 2, entry.get("pv", ""))
            self._set_pv_text(row, 3, entry.get("unit", ""))
            self._set_pv_flag(row, 4, bool(entry.get("writable", True)))
            self._set_pv_flag(row, 5, bool(entry.get("required", True)))
        for row in range(self.pv_table.rowCount()):
            self._clear_pv_row_marks(row)
        index = self.pv_gateway.findData(config.get("gateway", "simulated"))
        if index >= 0:
            self.pv_gateway.setCurrentIndex(index)
        self.pv_ca_lib_dir.setText(str(config.get("ca_lib_dir", "")))
        self._set_pv_feedback(
            "good" if config.get("gateway") == "channel-access" else "idle",
            f"已载入 {len(entries)} 条映射（网关：{self._pv_gateway_label()}）。",
        )
        self.pv_save_button.setEnabled(True)

    def _pv_gateway_label(self) -> str:
        return "真实 EPICS" if self.pv_gateway.currentData() == "channel-access" else "模拟"

    # ---- PV 映射：表格增删改 ----

    def _add_pv_row(self) -> None:
        row = self.pv_table.rowCount()
        self.pv_table.insertRow(row)
        self._set_pv_text(row, 0, "新参数")
        self._set_pv_text(row, 1, "")
        self._set_pv_text(row, 2, "")
        self._set_pv_text(row, 3, "")
        self._set_pv_flag(row, 4, True)
        self._set_pv_flag(row, 5, True)
        self.pv_table.setCurrentCell(row, 1)
        self.pv_table.editItem(self.pv_table.item(row, 1))

    def _remove_pv_rows(self) -> None:
        rows = sorted({index.row() for index in self.pv_table.selectedIndexes()})
        if not rows:
            self._set_pv_feedback("error", "请先在表格中选择要删除的行。")
            return
        for row in reversed(rows):
            self.pv_table.removeRow(row)
        self._set_pv_feedback("idle", f"已删除 {len(rows)} 行，点击“保存映射”生效。")

    def _save_pv_mapping(self) -> None:
        self._clear_all_pv_marks()
        payload = self._collect_pv_mapping()
        self._set_pv_feedback("idle", "正在保存…")
        self._start_pv_request(payload=payload)

    def _collect_pv_mapping(self) -> dict:
        entries = []
        for row in range(self.pv_table.rowCount()):
            entries.append(
                {
                    "label": self._pv_text(row, 0),
                    "signal": self._pv_text(row, 1),
                    "pv": self._pv_text(row, 2),
                    "unit": self._pv_text(row, 3),
                    "writable": self._pv_flag(row, 4),
                    "required": self._pv_flag(row, 5),
                }
            )
        return {
            "version": self._pv_config_version,
            "gateway": self.pv_gateway.currentData(),
            "ca_lib_dir": self.pv_ca_lib_dir.text().strip(),
            "entries": entries,
        }

    # ---- PV 映射：单元格读写与标红 ----

    def _pv_text(self, row: int, column: int) -> str:
        item = self.pv_table.item(row, column)
        return item.text().strip() if item is not None else ""

    def _set_pv_text(self, row: int, column: int, value: str) -> None:
        self.pv_table.setItem(row, column, QTableWidgetItem(value))

    def _pv_flag(self, row: int, column: int) -> bool:
        item = self.pv_table.item(row, column)
        return item is not None and item.checkState() == Qt.CheckState.Checked

    def _set_pv_flag(self, row: int, column: int, checked: bool) -> None:
        item = QTableWidgetItem()
        item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.pv_table.setItem(row, column, item)

    def _mark_pv_issues(self, issues: list) -> None:
        """按校验结果逐行标红；整体性问题（index<0）只显示在提示里。"""
        self._clear_all_pv_marks()
        fields = {name: index for index, name in enumerate(("label", "signal", "pv", "unit"))}
        brush = QColor(current_palette()["dangerBg"])
        messages: list[str] = []
        for issue in issues:
            row = int(issue.get("index", -1))
            field = str(issue.get("field", ""))
            message = str(issue.get("message", ""))
            messages.append(f"第 {row + 1} 行：{message}" if row >= 0 else message)
            if row < 0 or field not in fields:
                continue
            item = self.pv_table.item(row, fields[field])
            if item is not None:
                item.setBackground(brush)
                item.setToolTip(message)
        self._set_pv_feedback("error", " ".join(messages))

    def _clear_all_pv_marks(self) -> None:
        for row in range(self.pv_table.rowCount()):
            self._clear_pv_row_marks(row)

    def _clear_pv_row_marks(self, row: int) -> None:
        for column in range(self.pv_table.columnCount()):
            item = self.pv_table.item(row, column)
            if item is not None:
                item.setBackground(Qt.GlobalColor.transparent)
                item.setToolTip("")

    def _set_pv_feedback(self, state: str, message: str) -> None:
        self.pv_feedback.setText(message)
        self.pv_feedback.setProperty("state", state)
        self.pv_feedback.style().unpolish(self.pv_feedback)
        self.pv_feedback.style().polish(self.pv_feedback)

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
