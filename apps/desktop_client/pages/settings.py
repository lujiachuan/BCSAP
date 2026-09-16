"""系统设置页面：服务连接、PV 映射、日志与权限、外观。"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import QSettings, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
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

from apps.desktop_client import instrument_api
from apps.desktop_client.initialization import (
    CACHE_SETTINGS_KEY,
    CacheUnavailable,
    cache_root_from_settings,
    default_cache_root,
    ensure_cache_root,
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
# 设备访问统一走真实 EPICS Channel Access，没有模式开关；无 IOC 时健康检查会如实报未连接。
# 表格列：设备参数 / 业务信号 / PV 名称 / 单位 / 可写 / 必需 + 两列**检测结果**
# （连接 / 当前值）：后两列是 caget 的现场快照，只读、不参与保存。
PV_COLUMNS = ("设备参数", "业务信号", "PV 名称", "单位", "可写", "必需", "连接", "当前值")
# 业务信号列：行的身份，也是「未显示的安全字段」的载体（保存时按行合并回去）
PV_SIGNAL_COLUMN = 1
PV_STATUS_COLUMN = 6
PV_VALUE_COLUMN = 7

# 表格上方的"只看"筛选：现场最常用的三种
PV_FILTERS = (
    ("all", "全部"),
    ("down", "只看未连接"),
    ("writable", "只看可写"),
    ("required", "只看必需"),
    ("untested", "只看未测"),
)

# caget 检测的超时：上百路 PV 在真机上逐个读可能要好几秒，8 s 的轮询超时不够用
PV_PROBE_TIMEOUT_S = 30.0

# 条目数骤减的拦截阈值（与执行服务 pv_mapping.shrink_warning 同一套数字）：
# 现有条目不少于 10 条、而新映射不到一半时，先提醒一次，再点一次才真的存。
PV_SHRINK_MIN_ENTRIES = 10
PV_SHRINK_RATIO = 0.5

# 角色只作只读提示用；控件生成仍在手动页里按 role 决定
_ROLE_LABELS = {
    "setpoint": "设定值",
    "toggle": "开关",
    "pulse": "脉冲",
    "readback": "只读测量",
}

LOG_LEVELS = (("调试", "debug"), ("信息", "info"), ("警告", "warning"), ("错误", "error"))


def _value_text(value: object) -> str:
    """安全参数的显示文本；None 表示该项不校验，用「—」而不是 0。"""
    if value is None or value == "":
        return "—"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _entry_detail(entry: dict | None) -> str:
    """把表格里不显示的安全字段整理成一行只读说明（挂在业务信号单元格的提示里）。

    这六个字段由执行服务强制执行，界面上不开放编辑；这里至少让操作员看得见
    「这条映射到底有没有边界、配对和稳定判据」。
    """
    if not entry:
        return "新行：未设置设备参数（分组/角色/回读配对/边界/限速/稳定判据），保存后按默认值生效。"
    role = str(entry.get("role") or "")
    low, high = entry.get("min_value"), entry.get("max_value")
    unit = str(entry.get("unit") or "")
    if low is None and high is None:
        bounds = "—"
    else:
        bounds = f"{_value_text(low)} ~ {_value_text(high)}" + (f" {unit}" if unit else "")
    tol, timeout = entry.get("settle_tol"), entry.get("settle_timeout")
    if tol is None and timeout is None:
        settle = "—"
    else:
        settle = f"{_value_text(tol)} / {_value_text(timeout)} s"
    return " · ".join(
        (
            f"分组：{entry.get('group') or '（未分组）'}",
            f"角色：{_ROLE_LABELS.get(role, role or '未归类')}",
            f"回读配对：{entry.get('readback_signal') or '—'}",
            f"边界：{bounds}",
            f"单步上限：{_value_text(entry.get('max_step'))}",
            f"速率上限：{_value_text(entry.get('max_rate'))}",
            f"稳定判据：{settle}",
            "（以上字段保存时原样保留）",
        )
    )


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


def _prepare_cache_root(value: str) -> tuple[Path | None, str]:
    """校验本地数据目录：必须是非空绝对路径，且真的能建、能写。

    校验用执行同步时的同一段逻辑（`ensure_cache_root`），不另写一份——
    否则会出现"设置里说可以、同步时才失败"的分裂。
    """
    text = value.strip()
    if not text:
        return None, "本地数据目录不能为空；点「用默认目录」可填回默认值。"
    path = Path(text)
    if not path.is_absolute():
        return None, "请输入绝对路径（例如 D:\\谱图数据\\client_cache）。"
    try:
        return ensure_cache_root(path), ""
    except CacheUnavailable as exc:
        return None, str(exc)


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

    def __init__(self, settings: QSettings | None = None) -> None:
        super().__init__()
        # settings 只在测试里注入（临时 INI 文件）；运行时统一用本机偏好。
        self._settings = settings or QSettings("SpectrumPlatform", "DesktopClient")
        self._probe_thread: ConnectionProbeThread | None = None
        self._pv_request: PvMappingRequestThread | None = None
        self._pv_loaded = False
        self._pv_config_version = 1
        # PV 检测（caget）状态：进行中标志、在飞请求、本次检测的行、上次检测时刻
        self._pv_probe_in_flight = False
        self._pv_probe_request = None
        self._pv_probe_rows: list[int] = []
        self._pv_probe_started = 0.0
        self._pv_last_probe = ""
        self._pv_pending_reprobe = False
        # 已载入的条目数（判断"这次保存是不是把映射存残了"的基准）与确认状态
        self._pv_loaded_count = 0
        self._pv_shrink_confirmed = False
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

        layout.addWidget(self._build_cache_panel())

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

    def _build_cache_panel(self) -> Panel:
        """本地数据目录：中央数据镜像到哪，默认值直接写在界面上。"""
        panel = Panel("本地数据目录", "中央数据的本地只读镜像，可随时重建")
        self.cache_root = QLineEdit()
        self.cache_root.setToolTip("填绝对路径；换目录后下次同步会重新下载到新目录")
        default = default_cache_root()
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_cache_root)
        open_button = QPushButton("打开目录")
        open_button.clicked.connect(self._open_cache_root)
        use_default = QPushButton("用默认目录")
        use_default.clicked.connect(self._use_default_cache_root)
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self.cache_root, 1)
        row.addWidget(browse)
        row.addWidget(open_button)
        row.addWidget(use_default)
        form = QFormLayout()
        form.addRow("镜像目录", holder)
        panel.body.addLayout(form)
        hint = QLabel(
            f"默认目录：{default}。保存时会自动创建并检查可写；换目录不会删除旧目录里的"
            "内容，新目录在下次同步（重启客户端，或重试「同步中央数据」）时生效。"
        )
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        panel.body.addWidget(hint)
        self.cache_root.setText(str(cache_root_from_settings(self._settings)))
        return panel

    def _use_default_cache_root(self) -> None:
        self.cache_root.setText(str(default_cache_root()))
        self._set_feedback("idle", "已填入默认目录，点击“保存设置”生效。")

    def _browse_cache_root(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择本地数据目录", self.cache_root.text().strip() or str(Path.home())
        )
        if directory:
            self.cache_root.setText(str(Path(directory)))

    def _open_cache_root(self) -> None:
        text = self.cache_root.text().strip()
        path, problem = _prepare_cache_root(text)
        if path is None:
            self._flash_feedback(False, problem)
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self._flash_feedback(False, f"打不开目录：{path}")
            return
        self._set_feedback("good", f"已在资源管理器中打开 {path}")

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
        cache_root, problem = _prepare_cache_root(self.cache_root.text())
        self._settings.setValue("service/dataUrl", data_url)
        self._settings.setValue("service/instrumentUrl", instrument_url)
        if cache_root is None:
            # 目录不可用（选了离线网盘、打错盘符）不该连服务地址都存不下去：
            # 能存的先存，再明确说清哪一项没通过、当前生效的仍是哪个目录。
            self._flash_feedback(
                False, f"服务地址已保存；本地数据目录未保存：{problem}"
            )
            self.cache_root.setFocus()
            return
        self._settings.setValue(CACHE_SETTINGS_KEY, str(cache_root))
        self.cache_root.setText(str(cache_root))
        self._flash_feedback(True, f"已保存服务地址与本地数据目录（{cache_root}）。")

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
        # 双击一行 = 只重读这一路（现场排查单个通道时不必把 128 路全读一遍）
        self.pv_table.cellDoubleClicked.connect(self._on_pv_cell_double_clicked)

        # 工具行：查找 + 筛选 + 检测 + 复制。检测只读**当前可见行**：
        # 上百路 PV 全读一遍在真机上要好几秒，先筛再读才是现场想要的顺序。
        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.pv_search = QLineEdit()
        self.pv_search.setPlaceholderText("查找：设备参数 / 业务信号 / PV 名，可只输一段")
        self.pv_search.setClearButtonEnabled(True)
        self.pv_search.textChanged.connect(lambda _text: self._apply_pv_filter())
        tools.addWidget(self.pv_search, 2)
        self.pv_filter = QComboBox()
        for key, label in PV_FILTERS:
            self.pv_filter.addItem(label, key)
        self.pv_filter.currentIndexChanged.connect(lambda _index: self._apply_pv_filter())
        tools.addWidget(self.pv_filter)
        self.pv_probe_button = QPushButton("测试连接并读取")
        self.pv_probe_button.setToolTip(
            "对当前**可见行**做一次 caget：连接状态 + 当前值（没连上的如实标出来，"
            "不会因为一路连不上就说整张表不可用）"
        )
        self.pv_probe_button.clicked.connect(lambda: self._probe_pvs())
        tools.addWidget(self.pv_probe_button)
        self.pv_copy_button = QPushButton("复制选中行")
        self.pv_copy_button.setToolTip("把选中行的「设备参数 / 业务信号 / PV 名」贴到剪贴板")
        self.pv_copy_button.clicked.connect(self._copy_pv_rows)
        tools.addWidget(self.pv_copy_button)
        panel.body.addLayout(tools)
        self.pv_summary = QLabel("", objectName="mutedText")
        self.pv_summary.setWordWrap(True)
        panel.body.addWidget(self.pv_summary)
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
            "是否判定设备不可用。写入仍受参数边界与设备联锁约束。"
            "本机没有 IOC 时可先启动仓库里 sim/ 的模拟 IOC 联调。"
            "\n「连接 / 当前值」两列是点「测试连接并读取」后的现场快照（caget），"
            "只读、不参与保存；双击某一行可以只重读那一路。"
            "\n其余安全字段（分组、角色、回读配对、边界、最大单步、最大速率、稳定判据）"
            "由执行服务持有，保存时按行原样保留（悬停「业务信号」可查看）。"
            "**某一路连不上不影响扫谱/调束入口**：各自只用得到自己那几路，缺哪一路"
            "由执行服务在启动时点名拒绝。"
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

    def _start_pv_request(self, payload: dict | None, confirm_shrink: bool = False) -> None:
        self.pv_save_button.setEnabled(False)
        self._pv_request = PvMappingRequestThread(
            self._instrument_base_url(), payload, confirm_shrink=confirm_shrink
        )
        self._pv_request.completed.connect(self._finish_pv_request)
        self._pv_request.finished.connect(self._release_pv_request)
        self._pv_request.start()

    def _release_pv_request(self) -> None:
        # 只读部署下不给"保存映射"：服务端 PUT 会 400，按钮不该看起来能按
        self.pv_save_button.setEnabled(not instrument_api.is_read_only())
        if self._pv_request is not None:
            self._pv_request.deleteLater()
            self._pv_request = None

    def _finish_pv_request(self, result: dict) -> None:
        if not result["ok"]:
            self._mark_pv_issues(result["issues"])
            if result["issues"]:
                self._set_pv_feedback("error", result["message"])
            else:
                # 拿不到映射时要讲清楚原因：表格为空不等于「没有配置」。
                self._set_pv_feedback(
                    "error",
                    f"读不到 PV 映射：{self._instrument_base_url()} 上的仪器执行服务不可达。"
                    "请先启动执行服务（或本机调试用的 sim 模拟 IOC）后点“重新载入”；"
                    "服务启动后映射会自动从这里载入，不会丢失。",
                )
            return
        self._render_pv_mapping(result["config"])
        self._pv_loaded = True
        if self._pv_pending_reprobe:
            # 映射刚改过：PV 名可能变了，原来的"已连接/当前值"对新名字不再成立，
            # 自动重测一次；失败也不影响保存结果（提示里会说）。
            self._pv_pending_reprobe = False
            self._probe_pvs()

    def _render_pv_mapping(self, config: dict) -> None:
        entries = config.get("entries", [])
        self._pv_config_version = int(config.get("version", 1))
        self._pv_loaded_count = len(entries)
        self._pv_shrink_confirmed = False
        self.pv_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self._set_pv_text(row, 0, entry.get("label", ""))
            self._set_pv_text(row, PV_SIGNAL_COLUMN, entry.get("signal", ""))
            self._set_pv_text(row, 2, entry.get("pv", ""))
            self._set_pv_text(row, 3, entry.get("unit", ""))
            self._set_pv_flag(row, 4, bool(entry.get("writable", True)))
            self._set_pv_flag(row, 5, bool(entry.get("required", True)))
            self._remember_pv_entry(row, entry)
        for row in range(self.pv_table.rowCount()):
            self._clear_pv_row_marks(row)
            self._reset_pv_probe_cells(row)
        self._set_pv_feedback("good", f"已载入 {len(entries)} 条映射。")
        self.pv_save_button.setEnabled(not instrument_api.is_read_only())
        self._apply_pv_filter()

    # ---- PV 映射：查找 / 筛选 / 检测（caget）----

    def _pv_filter_key(self) -> str:
        return str(self.pv_filter.currentData() or "all")

    def _row_matches_filter(self, row: int) -> bool:
        """一行是否该显示：先按"只看"筛，再按关键字匹配三个可读列。"""
        key = self._pv_filter_key()
        status = self._pv_text(row, PV_STATUS_COLUMN)
        if key == "down" and status != "未连接":
            return False
        if key == "untested" and status not in ("", "未测"):
            return False
        if key == "writable" and not self._pv_flag(row, 4):
            return False
        if key == "required" and not self._pv_flag(row, 5):
            return False
        needle = self.pv_search.text().strip().lower()
        if not needle:
            return True
        haystack = " ".join(
            self._pv_text(row, column) for column in (0, PV_SIGNAL_COLUMN, 2)
        ).lower()
        return needle in haystack

    def _apply_pv_filter(self) -> None:
        """按查找词与"只看"隐藏行，并刷新汇总。

        用 ``setRowHidden`` 而不是重建表格：编辑到一半的单元格（尤其是新增行的草稿）
        不能被筛选动作弄丢。
        """
        total = self.pv_table.rowCount()
        visible = 0
        for row in range(total):
            keep = self._row_matches_filter(row)
            self.pv_table.setRowHidden(row, not keep)
            visible += int(keep)
        self._refresh_pv_summary(visible)

    def _refresh_pv_summary(self, visible: int | None = None) -> None:
        total = self.pv_table.rowCount()
        if visible is None:
            visible = sum(
                1 for row in range(total) if not self.pv_table.isRowHidden(row)
            )
        tested = sum(
            1
            for row in range(total)
            if self._pv_text(row, PV_STATUS_COLUMN) in ("已连接", "未连接")
        )
        connected = sum(
            1
            for row in range(total)
            if self._pv_text(row, PV_STATUS_COLUMN) == "已连接"
        )
        parts = [f"共 {total} 行", f"当前可见 {visible} 行"]
        if tested:
            parts.append(
                f"已检测 {tested}：{connected} 已连接 / {tested - connected} 未连接"
            )
        else:
            parts.append("还没检测过：点「测试连接并读取」")
        untested = total - tested
        if tested and untested:
            parts.append(f"未测 {untested} 行")
        if self._pv_last_probe:
            parts.append(f"上次检测 {self._pv_last_probe}")
        self.pv_summary.setText(" · ".join(parts))

    def _visible_pv_signals(self, rows: list[int] | None = None) -> list[tuple[int, str]]:
        """可见行里**填了业务信号**的那些（新加的空行不参与检测）。"""
        picked: list[tuple[int, str]] = []
        candidates = rows if rows is not None else range(self.pv_table.rowCount())
        for row in candidates:
            if rows is None and self.pv_table.isRowHidden(row):
                continue
            signal = self._pv_text(row, PV_SIGNAL_COLUMN)
            if signal:
                picked.append((row, signal))
        return picked

    def _probe_pvs(self, rows: list[int] | None = None) -> None:
        """对可见行（或指定行）做一次 caget：连接状态 + 当前值。"""
        if self._pv_probe_in_flight:
            self._set_pv_feedback("warn", "上一次检测还没回来，请稍候。")
            return
        picked = self._visible_pv_signals(rows)
        if not picked:
            self._set_pv_feedback("warn", "当前没有可检测的行（填上业务信号，或放宽筛选）。")
            return
        self._pv_probe_rows = [row for row, _signal in picked]
        self._pv_probe_in_flight = True
        self.pv_probe_button.setEnabled(False)
        self.pv_probe_button.setText(f"检测中（{len(picked)} 路）…")
        self._pv_probe_started = time.monotonic()
        request = instrument_api.request_read(
            signals=[signal for _row, signal in picked],
            timeout=PV_PROBE_TIMEOUT_S,
        )
        request.completed.connect(self._on_pv_probe)
        self._pv_probe_request = request

    def _on_pv_probe(self, payload: dict) -> None:
        self._pv_probe_in_flight = False
        self._pv_probe_request = None
        self.pv_probe_button.setEnabled(True)
        self.pv_probe_button.setText("测试连接并读取")
        elapsed = time.monotonic() - self._pv_probe_started
        if not payload.get("ok"):
            # 整批读失败（服务不可达/超时）时**不猜**每一路的连接状态：
            # 把原因写在提示里，表格里的状态保持"未测"。
            for row in self._pv_probe_rows:
                self._set_pv_probe_cells(row, "未测", "", str(payload.get("message", "")))
            self._set_pv_feedback("error", f"检测失败：{payload.get('message', '')}")
            self._pv_last_probe = ""
            self._refresh_pv_summary()
            return

        readings = instrument_api.readings_by_signal(payload.get("payload"))
        missing = 0
        for row in self._pv_probe_rows:
            signal = self._pv_text(row, PV_SIGNAL_COLUMN)
            reading = readings.get(signal)
            if reading is None:
                missing += 1
                self._set_pv_probe_cells(row, "未测", "", "执行服务没有返回这一路")
                continue
            connected = bool(reading.get("connected"))
            detail = str(reading.get("detail") or "")
            value = reading.get("value")
            unit = str(reading.get("unit") or self._pv_text(row, 3))
            if connected and value is not None:
                text = f"{float(value):.6g} {unit}".strip()
            else:
                text = "—"
            note = detail or ("PV 未连接" if not connected else "")
            self._set_pv_probe_cells(
                row, "已连接" if connected else "未连接", text, note
            )
        self._pv_last_probe = time.strftime("%H:%M:%S")
        connected_count = sum(
            1
            for row in self._pv_probe_rows
            if self._pv_text(row, PV_STATUS_COLUMN) == "已连接"
        )
        self._set_pv_feedback(
            "good" if connected_count == len(self._pv_probe_rows) else "warn",
            f"已检测 {len(self._pv_probe_rows)} 路：{connected_count} 已连接 / "
            f"{len(self._pv_probe_rows) - connected_count} 未连接"
            + (f"（{missing} 路服务未返回）" if missing else "")
            + f" · 耗时 {elapsed:.1f} s。"
            "没连上的只要不是本次要用的那几路，不影响扫谱与调束。",
        )
        self._apply_pv_filter()

    def _on_pv_cell_double_clicked(self, row: int, _column: int) -> None:
        self._probe_pvs(rows=[row])

    def _reset_pv_probe_cells(self, row: int) -> None:
        self._set_pv_probe_cells(row, "", "", "还没检测：点「测试连接并读取」")

    def _set_pv_probe_cells(
        self, row: int, status: str, value: str, note: str = ""
    ) -> None:
        """写「连接 / 当前值」两列：文本 + 颜色 + 明细提示（都不参与保存）。"""
        tokens = current_palette()
        for column, text in ((PV_STATUS_COLUMN, status), (PV_VALUE_COLUMN, value)):
            item = QTableWidgetItem(text)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            if note:
                item.setToolTip(note)
            self.pv_table.setItem(row, column, item)
        color = {
            "已连接": tokens["statusGood"],
            "未连接": tokens["statusError"],
        }.get(status)
        if color:
            for column in (PV_STATUS_COLUMN, PV_VALUE_COLUMN):
                item = self.pv_table.item(row, column)
                if item is not None:
                    item.setForeground(QColor(color))

    def _copy_pv_rows(self) -> None:
        rows = sorted({index.row() for index in self.pv_table.selectedIndexes()})
        if not rows:
            self._set_pv_feedback("warn", "先选中要复制的行。")
            return
        lines = [
            "\t".join(
                self._pv_text(row, column)
                for column in (0, PV_SIGNAL_COLUMN, 2)
            )
            for row in rows
        ]
        QApplication.clipboard().setText("\n".join(lines))
        self._set_pv_feedback(
            "good", f"已复制 {len(lines)} 行（设备参数 / 业务信号 / PV 名）。"
        )

    # ---- PV 映射：未显示的安全字段 ----

    def _remember_pv_entry(self, row: int, entry: dict | None) -> None:
        """把服务端返回的**完整**条目挂到该行的业务信号单元格上。

        表格只显示 6 个常用字段，其余安全字段（分组/角色/回读配对/边界/最大单步/
        最大速率/稳定判据）靠这份原始条目在保存时合并回去。
        """
        item = self.pv_table.item(row, PV_SIGNAL_COLUMN)
        if item is None:
            return
        item.setData(Qt.ItemDataRole.UserRole, dict(entry) if entry else None)
        item.setToolTip(_entry_detail(entry))

    def _pv_row_entry(self, row: int) -> dict | None:
        item = self.pv_table.item(row, PV_SIGNAL_COLUMN)
        if item is None:
            return None
        stored = item.data(Qt.ItemDataRole.UserRole)
        return dict(stored) if isinstance(stored, dict) else None

    # ---- PV 映射：表格增删改 ----

    def _add_pv_row(self) -> None:
        row = self.pv_table.rowCount()
        self.pv_table.insertRow(row)
        self._set_pv_text(row, 0, "新参数")
        self._set_pv_text(row, PV_SIGNAL_COLUMN, "")
        self._set_pv_text(row, 2, "")
        self._set_pv_text(row, 3, "")
        self._set_pv_flag(row, 4, True)
        self._set_pv_flag(row, 5, True)
        self._remember_pv_entry(row, None)
        self._reset_pv_probe_cells(row)
        self.pv_table.setCurrentCell(row, PV_SIGNAL_COLUMN)
        self.pv_table.editItem(self.pv_table.item(row, PV_SIGNAL_COLUMN))
        self._refresh_pv_summary()

    def _remove_pv_rows(self) -> None:
        rows = sorted({index.row() for index in self.pv_table.selectedIndexes()})
        if not rows:
            self._set_pv_feedback("error", "请先在表格中选择要删除的行。")
            return
        for row in reversed(rows):
            self.pv_table.removeRow(row)
        self._set_pv_feedback("idle", f"已删除 {len(rows)} 行，点击“保存映射”生效。")
        self._refresh_pv_summary()

    def _shrink_warning(self, payload: dict) -> str:
        """条目数骤减的提醒文案（空串表示正常）。

        与执行服务同一条策略（服务端也会拒），这里先拦一道是为了**把话说在操作员
        眼前**、也省一次白跑的请求：保存一整份映射时条目数腰斩，几乎总是"表格只
        显示了一部分"或"传错了文件"，而不是真的想删掉一半设备。
        """
        current = self._pv_loaded_count
        proposed = len(payload.get("entries") or [])
        if current < PV_SHRINK_MIN_ENTRIES or proposed >= current * PV_SHRINK_RATIO:
            return ""
        return (
            f"新映射只有 {proposed} 条，而当前是 {current} 条（不足一半）："
            "这通常意味着把残缺的一份表存了回来，存下去会让没列出的设备全部失去映射。"
        )

    def _save_pv_mapping(self) -> None:
        if instrument_api.is_read_only():
            # 只读部署下执行服务会 400（映射决定写入边界，改它等于改安全配置）
            self._set_pv_feedback(
                "warn", "全局只读模式：执行服务禁用了所有写入，PV 映射不可保存。"
            )
            return
        payload = self._collect_pv_mapping()
        warning = self._shrink_warning(payload)
        if warning and not self._pv_shrink_confirmed:
            # 第一次只提醒；再点一次才真的存（并要求服务端带 confirm_shrink）
            self._pv_shrink_confirmed = True
            self._set_pv_feedback(
                "warn", warning + " 确认要这样保存，请再点一次「保存映射」。"
            )
            return
        self._clear_all_pv_marks()
        self._pv_pending_reprobe = True
        self._set_pv_feedback("idle", "正在保存…")
        self._start_pv_request(payload=payload, confirm_shrink=self._pv_shrink_confirmed)
        self._pv_shrink_confirmed = False

    def _collect_pv_mapping(self) -> dict:
        """写回配置：表格里显示的 6 个字段来自表格，其余安全字段按行原样保留。

        只按表格重建条目会静默丢掉 group / role / readback_signal / min_value /
        max_value / max_step / max_rate / settle_tol / settle_timeout——这些字段在
        契约里都有默认值，Pydantic 不会报错，于是"保存一次"就等于把写入边界、
        单步/速率保护和稳定判据全部清空（分组与角色丢失还会让手动页控件退化、
        调束因缺 max_step 拒绝启动）。回归测试见
        ``tests/test_settings_page.py::PvMappingSafetyFieldTests``。
        """
        entries = []
        for row in range(self.pv_table.rowCount()):
            original = self._pv_row_entry(row) or {}
            signal = self._pv_text(row, PV_SIGNAL_COLUMN)
            entry = dict(original)
            if original and signal != str(original.get("signal") or ""):
                # 业务信号就是这一行的身份：改了名字还沿用旧的回读配对会指向别的
                # 设备通道，必须清空；边界/限速/稳定判据属于同类通道，继续沿用。
                entry["readback_signal"] = ""
            entry.update(
                {
                    "label": self._pv_text(row, 0),
                    "signal": signal,
                    "pv": self._pv_text(row, 2),
                    "unit": self._pv_text(row, 3),
                    "writable": self._pv_flag(row, 4),
                    "required": self._pv_flag(row, 5),
                }
            )
            entries.append(entry)
        return {
            "version": self._pv_config_version,
            "entries": entries,
        }

    # ---- PV 映射：单元格读写与标红 ----

    def _pv_text(self, row: int, column: int) -> str:
        item = self.pv_table.item(row, column)
        return item.text().strip() if item is not None else ""

    def _set_pv_text(self, row: int, column: int, value: str) -> None:
        item = QTableWidgetItem(value)
        # 业务信号列上挂着「未显示的安全字段」原始条目：程序化改写单元格时不能
        # 顺手把它丢掉（渲染时紧接着的 _remember_pv_entry 会覆盖成新值）。
        if column == PV_SIGNAL_COLUMN:
            previous = self.pv_table.item(row, column)
            if previous is not None:
                item.setData(
                    Qt.ItemDataRole.UserRole, previous.data(Qt.ItemDataRole.UserRole)
                )
        self.pv_table.setItem(row, column, item)

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
        # 业务信号列的提示是「未显示的安全字段」的只读出口，清标红时不能一起抹掉
        item = self.pv_table.item(row, PV_SIGNAL_COLUMN)
        if item is not None:
            item.setToolTip(_entry_detail(self._pv_row_entry(row)))

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
