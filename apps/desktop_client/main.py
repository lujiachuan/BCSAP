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
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from apps.desktop_client.initialization import InitializationPage, InitializationWorker
from apps.desktop_client.motion import PageTransitionController
from apps.desktop_client.nav_icons import make_nav_icon, make_symbol
from apps.desktop_client.pages import DEFAULT_SERVICE_URLS
from apps.desktop_client.pages.registry import SECTIONS, page_specs
from apps.desktop_client.spectrum_plot import SpectrumPlot
from apps.desktop_client.status_model import AppStatusModel
from apps.desktop_client.surfaces import AmbientCanvas
from apps.desktop_client.theme import apply_theme, current_palette, theme_name
from apps.desktop_client.widgets import SidebarStatusFooter

# 页面元数据集中在各 pages/ 模块的 PAGE_SPEC，主窗口只按注册表装配。
_PAGE_SPEC_BY_KEY = {spec.key: spec for spec in page_specs()}


def _nav_rows() -> list[tuple[bool, str, str, str]]:
    """由页面注册表生成侧栏行：(是否分区标题, 标签, 页面 key, 图标名)。"""

    rows: list[tuple[bool, str, str, str]] = []
    for section_label, section_key in SECTIONS:
        rows.append((True, section_label, "", ""))
        for spec in page_specs():
            if spec.section == section_key:
                rows.append((False, spec.label, spec.key, spec.icon))
    return rows

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
        self._model = AppStatusModel(self)
        self._model.updated.connect(self._on_model_updated)
        self._motion = PageTransitionController(self._settings, self)
        self._sidebar_collapsed = False
        self._start_maximized = False

        shell = AmbientCanvas()
        shell.setObjectName("appShell")
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
        self.theme_button = QPushButton(objectName="themeButton")
        self.theme_button.setIconSize(QSize(15, 15))
        self.theme_button.setAccessibleName("切换界面主题")
        self.theme_button.clicked.connect(self._toggle_theme)
        self.sidebar_button = QPushButton(objectName="sidebarButton")
        self.sidebar_button.setIconSize(QSize(15, 15))
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
        self._row_of_key: dict[str, int] = {}
        self._navigation_items: list[tuple[QListWidgetItem, str, str, bool]] = []
        self._settings_page: QWidget | None = None
        self._workbench_page: QWidget | None = None
        self._pages_by_key: dict[str, QWidget] = {}
        self._init_worker: InitializationWorker | None = None
        self._sync_in_progress = False

        self.initialization_page = InitializationPage()
        self._initialization_index = self.pages.addWidget(self.initialization_page)
        self.initialization_page.retryRequested.connect(self._retry_service)
        self.initialization_page.enterRequested.connect(self._enter_workbench)
        self.initialization_page.settingsRequested.connect(self._open_settings_from_init)

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
        self._sync_chrome_icons()
        QTimer.singleShot(0, self._start_initialization)

    def closeEvent(self, event) -> None:  # noqa: N802
        """退出前确认进行中的任务，再记住窗口几何、侧栏状态与当前页面。"""
        if not self._confirm_exit_while_active(event):
            return
        if self._init_worker is not None and self._init_worker.isRunning():
            self._init_worker.requestInterruption()
            self._init_worker.wait(5_500)
        self._settings.setValue("windowGeometry", self.saveGeometry())
        self._settings.setValue("windowMaximized", self.isMaximized())
        self._settings.setValue("sidebarCollapsed", self._sidebar_collapsed)
        self._settings.setValue("lastPageRow", self.navigation.currentRow())
        super().closeEvent(event)

    _ACTIVE_PAGE_NAMES = {"scan": "扫谱", "tuning": "自动调束"}

    def _operation_active(self, key: str) -> bool:
        """询问页面实例是否有进行中的任务（扫谱/调束各自实现）。"""
        page = self._pages_by_key.get(key)
        if page is None:
            return False
        checker = getattr(page, "is_operation_active", None)
        return bool(checker is not None and checker())

    def _confirm_exit_while_active(self, event) -> bool:
        """扫谱/调束进行中退出时弹确认；返回 False 表示用户取消退出。"""
        active_names = [
            name
            for key, name in self._ACTIVE_PAGE_NAMES.items()
            if self._operation_active(key)
        ]
        if not active_names:
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("任务仍在进行")
        box.setText(
            f"当前仍有“{'、'.join(active_names)}”正在进行。\n\n"
            "退出将中断当前任务并停止采集。确定停止并退出吗？"
        )
        cancel_button = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.addButton("停止并退出", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(cancel_button)
        box.setEscapeButton(cancel_button)
        box.exec()
        if box.clickedButton() is cancel_button:
            event.ignore()
            return False
        for key in self._ACTIVE_PAGE_NAMES:
            page = self._pages_by_key.get(key)
            if page is not None and getattr(page, "safe_stop", None) is not None:
                page.safe_stop()
        return True

    def showEvent(self, event) -> None:  # noqa: N802
        """首次显示后按主题给原生标题栏着色（失败自动回退系统外观）。"""
        super().showEvent(event)
        self._apply_native_chrome()

    def _apply_native_chrome(self) -> None:
        """把原生标题栏/边框颜色同步为当前主题（仅 Windows，可重复调用）。"""
        if sys.platform != "win32":
            return
        try:
            from apps.desktop_client import windows_chrome

            windows_chrome.apply_native_chrome(self, current_palette())
        except Exception:  # noqa: BLE001  DWM 能力差异时静默回退系统标题栏
            return

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
        """按页面注册表生成侧栏与堆叠页面；页面内容在各自 pages/ 模块内。"""
        for row, (is_section, label, key, symbol) in enumerate(_nav_rows()):
            item = QListWidgetItem(label)
            item.setToolTip(label)
            if is_section:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                item.setData(Qt.ItemDataRole.UserRole, "section")
                self.navigation.addItem(item)
                self._navigation_items.append((item, label, "", True))
                continue
            self.navigation.addItem(item)
            item.setIcon(make_nav_icon(symbol))
            self._navigation_items.append((item, label, symbol, False))
            page = _PAGE_SPEC_BY_KEY[key].factory()
            self._pages_by_key[key] = page
            if key == "settings":
                page.themeChanged.connect(self._set_theme)
                page.motionPreferenceChanged.connect(self._on_motion_preference_changed)
                self._settings_page = page
            elif key == "workbench":
                page.navigateRequested.connect(self._navigate_to)
                page.initializationRequested.connect(self._start_initialization)
                page.serviceCardRequested.connect(self._show_service_details)
                self._workbench_page = page
            scroll = QScrollArea(objectName="pageScroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.viewport().setAutoFillBackground(False)
            scroll.setWidget(page)
            page_index = self.pages.addWidget(scroll)
            self._page_rows[row] = page_index
            self._row_of_key[key] = row

    def _navigate_to(self, page_key: str) -> None:
        """处理工作台快捷入口，复用左侧导航的页面映射。"""
        row = self._row_of_key.get(page_key)
        if row is not None:
            self.navigation.setCurrentRow(row)

    # ------------------------------------------------------------------
    # 初始化与状态接线（AppStatusModel 单一状态源）
    # ------------------------------------------------------------------

    _SERVICE_NAMES = {
        "data": "数据服务",
        "instrument": "仪器执行服务",
        "epics": "EPICS 连接",
        "cache": "本地数据",
    }

    def _start_initialization(self) -> None:
        """启动 / 工作台“重新检查”：完整重查全部服务。"""
        self._run_initialization(("instrument", "data"), reset_page=True)

    def _retry_service(self, step_key: str) -> None:
        """初始化页单步失败“重试”：只重跑相关目标，不重复已成功的检查。"""
        target = InitializationPage.RETRY_TARGETS.get(step_key)
        if target is None or self._worker_busy():
            return
        self._run_initialization((target,), reset_page=False)

    def _worker_busy(self) -> bool:
        return self._init_worker is not None and self._init_worker.isRunning()

    def _run_initialization(
        self, targets: tuple[str, ...], reset_page: bool
    ) -> None:
        if self._worker_busy():
            return
        if reset_page:
            self._model.reset()
            self.initialization_page.reset()
            self.pages.setCurrentIndex(self._initialization_index)
            self.navigation.setEnabled(False)
            defaults = {
                "data": ("idle", "待连接"),
                "instrument": ("idle", "待连接"),
                "epics": ("idle", "未检查"),
                "cache": ("idle", "待同步"),
            }
            for key, (state, text) in defaults.items():
                self._model.set_service(key, state, text)
            self._sync_service_views()
            if self._workbench_page is not None:
                self._workbench_page.set_control_enabled(False)
        self.initialization_page.set_retry_enabled(False)
        data_url = str(
            self._settings.value("service/dataUrl", DEFAULT_SERVICE_URLS["data"])
        )
        instrument_url = str(
            self._settings.value(
                "service/instrumentUrl", DEFAULT_SERVICE_URLS["instrument"]
            )
        )
        worker = InitializationWorker(data_url, instrument_url, self, targets=targets)
        worker.stepChanged.connect(self._on_init_step)
        worker.serviceChanged.connect(self._on_service)
        worker.pvStatus.connect(self._on_pv_status)
        worker.syncProgress.connect(self._on_sync_progress)
        worker.essentialReady.connect(self._on_essential_ready)
        worker.completed.connect(self._on_init_completed)
        worker.finished.connect(self._on_worker_finished)
        self._init_worker = worker
        worker.start()

    def _on_init_step(self, key: str, state: str, detail: str) -> None:
        self._model.set_step(key, state, detail)
        self.initialization_page.set_step(key, state, detail)
        if key == "sync":
            self._sync_in_progress = state == "running"
            display_state = "warn" if state == "running" else state
            self._model.set_service("cache", display_state, detail or "待同步")
            self._sync_one_service_view("cache")

    def _on_service(self, key: str, state: str, text: str) -> None:
        self._model.set_service(key, state, text)
        self._sync_one_service_view(key)

    def _on_pv_status(self, connected: int, total: int, details: list) -> None:
        self._model.set_pv(connected, total, details)

    def _on_sync_progress(self, current: int, total: int) -> None:
        self._model.set_sync_progress(current, total)
        self.initialization_page.set_sync_progress(current, total)

    def _sync_one_service_view(self, key: str) -> None:
        service = self._model.service(key)
        state, text = service["state"], service["text"]
        if key != "cache":
            self.status_footer.set_service(key, state, text or "—")
        if self._workbench_page is not None:
            self._workbench_page.set_service_status(key, state, text or "—")

    def _sync_service_views(self) -> None:
        for key in self._model.SERVICE_KEYS:
            self._sync_one_service_view(key)

    def _on_essential_ready(self) -> None:
        """必要条件检查完成：解锁导航，允许先进入，同步可继续后台运行。"""
        self._model.mark_essential_ready()
        self.navigation.setEnabled(True)
        self._apply_control_eligibility()
        self._refresh_init_actions()

    def _on_init_completed(self, ok: bool, message: str) -> None:  # noqa: ARG002
        self._model.mark_finished()
        self._refresh_init_actions()

    def _on_worker_finished(self) -> None:
        if self._init_worker is not None:
            self._init_worker.deleteLater()
            self._init_worker = None
        self.initialization_page.set_retry_enabled(True)
        self._apply_control_eligibility()
        self._refresh_init_actions()

    def _on_model_updated(self) -> None:
        # 预留：未来动画/角标等订阅入口；当前各视图更新由具体 handler 显式驱动。
        return

    # ---------- 进入判定与操作资格 ----------

    def _apply_control_eligibility(self) -> None:
        """扫谱/调束入口启用与否由模型推导，页面不自行拼条件。"""
        can_control = self._model.can_control
        for page_key in ("scan", "tuning"):
            row = self._row_of_key.get(page_key)
            if row is None:
                continue
            item = self.navigation.item(row)
            if can_control:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEnabled)
                item.setToolTip(_PAGE_SPEC_BY_KEY[page_key].label)
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setToolTip("仪器服务或关键 PV 未就绪，暂不可用")
        if self._workbench_page is not None:
            self._workbench_page.set_control_enabled(can_control)
        row = self.navigation.currentRow()
        scan_rows = {
            self._row_of_key[key]
            for key in ("scan", "tuning")
            if key in self._row_of_key
        }
        if not can_control and row in scan_rows:
            workbench_row = self._row_of_key.get("workbench", 1)
            self.navigation.setCurrentRow(workbench_row)
            row = workbench_row
        self._show_page(row)

    def _refresh_init_actions(self) -> None:
        """按模型推导初始化页操作行：进入 / 降级进入 / 离线进入。"""
        if not self._model.essential_ready:
            self.initialization_page.set_actions("hidden")
            return
        instrument = self._model.service("instrument")["state"]
        data = self._model.service("data")["state"]
        if self._model.can_control and self._model.data_ready:
            mode = "ready"
        elif instrument == "error" and data == "error":
            mode = "offline"
        else:
            mode = "degraded"
        self.initialization_page.set_actions(mode)

    def _enter_workbench(self) -> None:
        self._navigate_to("workbench")

    def _open_settings_from_init(self) -> None:
        self._navigate_to("settings")

    # ---------- 服务明细（工作台状态卡双击查看） ----------

    def _show_service_details(self, key: str) -> None:
        if key == "epics":
            title = "PV 连接明细"
            svc = self._model.service("epics")
            lines = [f"已连接 {self._model.pv_connected} / {self._model.pv_total}"]
            if svc["at"]:
                lines.append(f"最近检查：{svc['at']}")
            if self._model.pv_details:
                lines.append("")
                lines.append("状态明细：")
                lines.extend(f"· {detail}" for detail in self._model.pv_details)
            QMessageBox.information(self, title, "\n".join(lines))
            return
        svc = self._model.service(key)
        name = self._SERVICE_NAMES.get(key, key)
        lines = [f"当前：{svc['text'] or '—'}", f"状态时间：{svc['at'] or '—'}"]
        if svc["detail"]:
            lines.append(f"说明：{svc['detail']}")
        QMessageBox.information(self, f"{name}状态", "\n".join(lines))

    def _show_page(self, row: int) -> None:
        page_index = self._page_rows.get(row)
        if page_index is not None:
            self.pages.setCurrentIndex(page_index)
            current = self.pages.currentWidget()
            if current is not None:
                self._motion.fade_in(current)
        self._refresh_nav_icons()

    def _on_motion_preference_changed(self, reduced: bool) -> None:
        self._motion.set_reduced(reduced)
        self.status_footer.refresh_motion()

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
        """应用指定主题并同步依赖主题的控件；入口包括侧栏按钮与系统设置页。"""
        if name == theme_name():
            return
        app = QApplication.instance()
        apply_theme(app, name)
        self._settings.setValue("theme", name)
        self._sync_theme_button()
        self._sync_chrome_icons()
        self._refresh_nav_icons()
        self.status_footer.refresh_theme()
        if self._settings_page is not None:
            self._settings_page.sync_theme_radio()
        for plot in self.pages.findChildren(SpectrumPlot):
            plot.refresh_theme()
        # setStyleSheet/setPalette 会触发全量重刷；这里仅对自绘控件补一次 update，
        # 避免全树 unpolish/polish 造成的切换开销（M1 性能重构）。
        for widget in app.allWidgets():
            widget.update()
        self._sync_service_views()
        self._apply_native_chrome()

    def _sync_theme_button(self) -> None:
        dark = theme_name() == "dark"
        icon = make_symbol("sun" if dark else "moon", current_palette()["navText"])
        self.theme_button.setIcon(icon)
        self.theme_button.setToolTip("切换到浅色主题" if dark else "切换到深色主题")

    def _sync_chrome_icons(self) -> None:
        """侧栏头部的菜单/主题按钮图标颜色跟随主题。"""
        color = current_palette()["navText"]
        self.sidebar_button.setIcon(make_symbol("menu", color))

    def _toggle_sidebar(self) -> None:
        self._sidebar_collapsed = not self._sidebar_collapsed
        self._sync_collapsed_state()
        self._settings.setValue("sidebarCollapsed", self._sidebar_collapsed)
        target = _SIDEBAR_COLLAPSED if self._sidebar_collapsed else _SIDEBAR_EXPANDED
        self._sidebar_animation.stop()
        if self._motion.reduced:
            self.sidebar.setFixedWidth(target)
            return
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
    # 操作电脑单文件夹交付：探测并拉起同目录分发的本机执行服务
    # （找不到服务 exe 的检索电脑/开发环境会自动跳过，见 instrument_supervisor）。
    from apps.desktop_client.instrument_supervisor import InstrumentServiceSupervisor

    supervisor = InstrumentServiceSupervisor()
    supervisor.ensure_started()
    window = MainWindow()
    if window._start_maximized:
        window.showMaximized()
    else:
        window.show()
    try:
        return app.exec()
    finally:
        supervisor.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
