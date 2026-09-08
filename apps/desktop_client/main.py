"""统一 PySide6 桌面客户端入口。"""

from __future__ import annotations

import sys

from PySide6.QtCore import (
    QByteArray,
    QEasingCurve,
    QSettings,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from apps.desktop_client.initialization import InitializationPage, InitializationWorker
from apps.desktop_client.nav_icons import make_nav_icon
from apps.desktop_client.pages import (
    DEFAULT_SERVICE_URLS,
    PlaceholderPage,
    ScanPage,
    SystemSettingsPage,
    TuningPage,
    WorkbenchPage,
)
from apps.desktop_client.theme import apply_theme, current_palette, theme_name
from apps.desktop_client.widgets import LinePlot, SidebarStatusFooter

NAVIGATION = (
    ("实验控制", None, ""),
    ("工作台", "workbench", "dashboard"),
    ("样品管理", "samples", "sample"),
    ("扫谱", "scan", "scan"),
    ("自动调束", "tuning", "tuning"),
    ("数据与系统", None, ""),
    ("谱图库", "library", "database"),
    ("谱图分析", "analysis", "analysis"),
    ("任务与同步", "sync", "sync"),
    ("系统设置", "settings", "settings"),
)

# 侧边栏底部常驻服务状态（跨页面可见）；接入真实服务后改用事件刷新。
SIDEBAR_SERVICES = (
    ("data", "数据服务", "待连接", "idle"),
    ("instrument", "仪器执行服务", "待连接", "idle"),
    ("epics", "EPICS", "未确认", "idle"),
)

_SIDEBAR_EXPANDED = 168
_SIDEBAR_COLLAPSED = 58
_ANIMATION_MS = 170


class MainWindow(QMainWindow):
    """平台主窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("谱图与束流控制平台")
        self.setWindowIcon(make_nav_icon("scan", current_palette()["accent"]))
        self.resize(1320, 820)
        self.setMinimumSize(1080, 680)

        self._settings = QSettings("SpectrumPlatform", "DesktopClient")
        self._sidebar_collapsed = False
        self._start_maximized = False

        shell = QWidget(objectName="appShell")
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        self.sidebar = QFrame(objectName="sidebar")
        self.sidebar.setFixedWidth(_SIDEBAR_EXPANDED)
        sidebar_layout = QVBoxLayout(self.sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)
        sidebar_head = QWidget(objectName="sidebarHead")
        sidebar_head_layout = QHBoxLayout(sidebar_head)
        sidebar_head_layout.setContentsMargins(12, 8, 10, 8)
        self.sidebar_label = QLabel("控制台", objectName="sidebarLabel")
        self.theme_button = QPushButton("☾", objectName="themeButton")
        self.theme_button.setAccessibleName("切换界面主题")
        self.theme_button.clicked.connect(self._toggle_theme)
        self.sidebar_button = QPushButton("☰", objectName="sidebarButton")
        self.sidebar_button.setAccessibleName("收起或展开侧栏")
        self.sidebar_button.setToolTip("收起侧栏")
        self.sidebar_button.clicked.connect(self._toggle_sidebar)
        sidebar_head_layout.addWidget(self.sidebar_label)
        sidebar_head_layout.addStretch()
        sidebar_head_layout.addWidget(self.theme_button)
        sidebar_head_layout.addWidget(self.sidebar_button)
        sidebar_layout.addWidget(sidebar_head)

        self.navigation = QListWidget(objectName="navigation")
        self.navigation.setIconSize(QSize(22, 22))
        self.navigation.setProperty("collapsed", False)
        self.navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 保留键盘可达：Tab 聚焦后可用方向键切换页面，焦点框由样式表隐藏。
        self.navigation.setFocusPolicy(Qt.FocusPolicy.WheelFocus)
        sidebar_layout.addWidget(self.navigation, 1)

        self.status_footer = SidebarStatusFooter(SIDEBAR_SERVICES)
        sidebar_layout.addWidget(self.status_footer)
        shell_layout.addWidget(self.sidebar)

        self.pages = QStackedWidget(objectName="pages")
        shell_layout.addWidget(self.pages, 1)
        self.setCentralWidget(shell)

        self._page_rows: dict[int, int] = {}
        self._navigation_items: list[tuple[QListWidgetItem, str, str, bool]] = []
        self._settings_page: SystemSettingsPage | None = None
        self._workbench_page: WorkbenchPage | None = None
        self._init_worker: InitializationWorker | None = None
        self._instrument_ready = False
        self._pv_ready = False
        self._data_state = "idle"
        self._data_text = "待连接"

        self.initialization_page = InitializationPage()
        self._initialization_index = self.pages.addWidget(self.initialization_page)

        self._sidebar_animation = QVariantAnimation(self)
        self._sidebar_animation.setDuration(_ANIMATION_MS)
        self._sidebar_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._sidebar_animation.valueChanged.connect(
            lambda value: self.sidebar.setFixedWidth(int(value))
        )

        toggle_shortcut = QShortcut(QKeySequence("Ctrl+B"), self)
        toggle_shortcut.activated.connect(self._toggle_sidebar)

        self._add_pages()
        self.navigation.currentRowChanged.connect(self._show_page)
        self._restore_preferences()
        self._sync_theme_button()
        QTimer.singleShot(0, self._start_initialization)

    def closeEvent(self, event) -> None:  # noqa: N802
        """退出前记住窗口几何、侧栏状态与当前页面。"""
        if self._init_worker is not None and self._init_worker.isRunning():
            self._init_worker.requestInterruption()
            self._init_worker.wait(5_500)
        self._settings.setValue("windowGeometry", self.saveGeometry())
        self._settings.setValue("windowMaximized", self.isMaximized())
        self._settings.setValue("sidebarCollapsed", self._sidebar_collapsed)
        self._settings.setValue("lastPageRow", self.navigation.currentRow())
        super().closeEvent(event)

    def _restore_preferences(self) -> None:
        """恢复上次会话的窗口几何、侧栏折叠状态与所在页面。"""
        geometry = self._settings.value("windowGeometry")
        if isinstance(geometry, (QByteArray, bytes)):
            self.restoreGeometry(QByteArray(geometry))
        self._start_maximized = bool(self._settings.value("windowMaximized", False, type=bool))

        if bool(self._settings.value("sidebarCollapsed", False, type=bool)):
            self._sidebar_collapsed = True
            self.sidebar.setFixedWidth(_SIDEBAR_COLLAPSED)
            self._sync_collapsed_state()

        saved_row = int(self._settings.value("lastPageRow", 1))
        row = saved_row if saved_row in self._page_rows else 1
        self.navigation.setCurrentRow(row)

    def _add_pages(self) -> None:
        page_factories = {
            "samples": lambda: PlaceholderPage(
                "样品管理", "维护样品编号、类型、批次和实验备注。"
            ),
            "scan": ScanPage,
            "tuning": TuningPage,
            "library": lambda: PlaceholderPage(
                "谱图库", "检索与多人访问界面按计划暂缓建设。"
            ),
            "analysis": lambda: PlaceholderPage(
                "谱图分析", "分析工具将在谱图库需求明确后一起设计。"
            ),
            "sync": lambda: PlaceholderPage(
                "任务与同步", "显示本机任务、待上传数据和服务同步状态。"
            ),
        }

        for label, page_key, symbol in NAVIGATION:
            item = QListWidgetItem(label)
            item.setToolTip(label)
            if page_key is None:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                item.setData(Qt.ItemDataRole.UserRole, "section")
                self.navigation.addItem(item)
                self._navigation_items.append((item, label, symbol, True))
                continue
            self.navigation.addItem(item)
            item.setIcon(make_nav_icon(symbol))
            self._navigation_items.append((item, label, symbol, False))
            if page_key == "settings":
                page = SystemSettingsPage()
                page.themeChanged.connect(self._set_theme)
                self._settings_page = page
            elif page_key == "workbench":
                page = WorkbenchPage()
                page.navigateRequested.connect(self._navigate_to)
                page.initializationRequested.connect(self._start_initialization)
                self._workbench_page = page
            else:
                page = page_factories[page_key]()
            scroll = QScrollArea(objectName="pageScroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(page)
            page_index = self.pages.addWidget(scroll)
            self._page_rows[self.navigation.count() - 1] = page_index

    def _navigate_to(self, page_key: str) -> None:
        """处理工作台快捷入口，复用左侧导航的页面映射。"""
        for row, (_label, key, _symbol) in enumerate(NAVIGATION):
            if key == page_key:
                self.navigation.setCurrentRow(row)
                return

    def _start_initialization(self) -> None:
        if self._init_worker is not None and self._init_worker.isRunning():
            return
        self.initialization_page.reset()
        self.pages.setCurrentIndex(self._initialization_index)
        self.navigation.setEnabled(False)
        self._instrument_ready = False
        self._pv_ready = False
        for key, text in (("data", "待连接"), ("instrument", "待连接"), ("epics", "未检查")):
            self._update_startup_service(key, "idle", text)
        if self._workbench_page is not None:
            self._workbench_page.set_control_enabled(False)
        data_url = str(
            self._settings.value("service/dataUrl", DEFAULT_SERVICE_URLS["data"])
        )
        instrument_url = str(
            self._settings.value(
                "service/instrumentUrl", DEFAULT_SERVICE_URLS["instrument"]
            )
        )
        worker = InitializationWorker(data_url, instrument_url, self)
        worker.stepChanged.connect(self._update_initialization_step)
        worker.serviceChanged.connect(self._update_startup_service)
        worker.syncProgress.connect(self.initialization_page.set_sync_progress)
        worker.essentialReady.connect(self._initialization_essential_ready)
        worker.finished.connect(self._initialization_worker_finished)
        self._init_worker = worker
        worker.start()

    def _update_initialization_step(self, key: str, state: str, detail: str) -> None:
        self.initialization_page.set_step(key, state, detail)
        if key == "sync" and self._workbench_page is not None:
            display_state = "warn" if state == "running" else state
            self._workbench_page.set_service_status("cache", display_state, detail)

    def _update_startup_service(self, key: str, state: str, text: str) -> None:
        self.status_footer.set_service(key, state, text)
        if self._workbench_page is not None:
            self._workbench_page.set_service_status(key, state, text)
        if key == "instrument":
            self._instrument_ready = state == "good"
        elif key == "epics":
            self._pv_ready = state == "good"
        elif key == "data":
            self._data_state = state
            self._data_text = text

    def _initialization_essential_ready(self) -> None:
        self.navigation.setEnabled(True)
        control_enabled = self._instrument_ready and self._pv_ready
        for row, (label, key, _symbol) in enumerate(NAVIGATION):
            if key not in {"scan", "tuning"}:
                continue
            item = self.navigation.item(row)
            item.setFlags(
                item.flags() | Qt.ItemFlag.ItemIsEnabled
                if control_enabled
                else item.flags() & ~Qt.ItemFlag.ItemIsEnabled
            )
            item.setToolTip(
                label if control_enabled else "仪器服务或关键 PV 未就绪，暂不可用"
            )
        if self._workbench_page is not None:
            self._workbench_page.set_control_enabled(control_enabled)
        row = self.navigation.currentRow()
        if not control_enabled and row in (3, 4):
            row = 1
            self.navigation.setCurrentRow(row)
        self._show_page(row)

    def _initialization_worker_finished(self) -> None:
        if self._init_worker is not None:
            self._init_worker.deleteLater()
            self._init_worker = None

    def _show_page(self, row: int) -> None:
        page_index = self._page_rows.get(row)
        if page_index is not None:
            self.pages.setCurrentIndex(page_index)
        self._refresh_nav_icons()

    def _refresh_nav_icons(self) -> None:
        """当前页图标用主题强调色，其余用主题默认图标色，让选中态更清晰。"""
        palette = current_palette()
        active_color = palette["accent"]
        default_color = palette["iconDefault"]
        current_row = self.navigation.currentRow()
        for item, label, symbol, is_section in self._navigation_items:
            if is_section or not symbol:
                continue
            color = active_color if self.navigation.row(item) == current_row else default_color
            item.setIcon(make_nav_icon(symbol, color))

    def _toggle_theme(self) -> None:
        """侧栏快捷按钮：在浅色 / 深色主题间切换。"""
        target = "dark" if theme_name() == "light" else "light"
        self._set_theme(target)

    def _set_theme(self, name: str) -> None:
        """应用指定主题并同步所有依赖主题的控件；入口包括侧栏按钮与系统设置页。"""
        if name == theme_name():
            return
        app = QApplication.instance()
        apply_theme(app, name)
        self._settings.setValue("theme", name)
        self._sync_theme_button()
        self._refresh_nav_icons()
        self.status_footer.refresh_theme()
        if self._settings_page is not None:
            self._settings_page.sync_theme_radio()
        for plot in self.pages.findChildren(LinePlot):
            plot.update()
        # 让 QSS/QPalette 变化完整地重刷到所有组件
        for widget in app.allWidgets():
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()

    def _sync_theme_button(self) -> None:
        dark = theme_name() == "dark"
        self.theme_button.setText("☀" if dark else "☾")
        self.theme_button.setToolTip("切换到浅色主题" if dark else "切换到深色主题")

    def _toggle_sidebar(self) -> None:
        self._sidebar_collapsed = not self._sidebar_collapsed
        self._sync_collapsed_state()
        self._settings.setValue("sidebarCollapsed", self._sidebar_collapsed)
        target = _SIDEBAR_COLLAPSED if self._sidebar_collapsed else _SIDEBAR_EXPANDED
        self._sidebar_animation.stop()
        self._sidebar_animation.setStartValue(self.sidebar.width())
        self._sidebar_animation.setEndValue(target)
        self._sidebar_animation.start()

    def _sync_collapsed_state(self) -> None:
        collapsed = self._sidebar_collapsed
        self.sidebar_label.setVisible(not collapsed)
        self.theme_button.setVisible(not collapsed)
        self.sidebar_button.setToolTip("展开侧栏" if collapsed else "收起侧栏")
        self.status_footer.setVisible(not collapsed)
        self.navigation.setProperty("collapsed", collapsed)
        self.navigation.style().unpolish(self.navigation)
        self.navigation.style().polish(self.navigation)
        for item, label, symbol, is_section in self._navigation_items:
            item.setHidden(collapsed and is_section)
            if not is_section:
                item.setText("" if collapsed else label)
                alignment = (
                    Qt.AlignmentFlag.AlignCenter
                    if collapsed
                    else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                item.setTextAlignment(alignment)


def main() -> int:
    """启动桌面客户端。"""

    app = QApplication(sys.argv)
    app.setApplicationName("谱图与束流控制平台")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    # 启动时恢复上次选择的主题（浅色 / 深色）
    settings = QSettings("SpectrumPlatform", "DesktopClient")
    apply_theme(app, str(settings.value("theme", "light")))
    window = MainWindow()
    if window._start_maximized:
        window.showMaximized()
    else:
        window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
