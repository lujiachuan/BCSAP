"""桌面客户端的浅色 / 深色主题。

主题由「调色板（颜色令牌）+ QSS 模板」组成：所有界面颜色从调色板令牌取值，
新增主题只需补充一组令牌。切换主题时应用新的 QSS 与 QPalette，即可让 QSS
覆盖到的组件与未覆盖的系统组件（复选框、滚动条、下拉箭头等）都跟随配色。
"""

from __future__ import annotations

from string import Template

from PySide6.QtGui import QColor, QPalette

LIGHT = {
    "canvas": "#eef2f6",
    "sidebarBg": "#f2f5f9",
    "sidebarBorder": "#d8dfe8",
    "headBorder": "#e1e7ee",
    "headText": "#1c2936",
    "iconBtnHover": "#e0e8f0",
    "navHover": "#e5ecf3",
    "navText": "#3e4c5a",
    "navMuted": "#97a5b1",
    "navSelectedBg": "#dbeaf7",
    "navSelectedText": "#0f6cbd",
    "textBase": "#17212b",
    "muted": "#647181",
    "panelBg": "#ffffff",
    "panelBorder": "#d7dee7",
    "noticeBg": "#e8f4ee",
    "accent": "#0f6cbd",
    "accentHover": "#0b5ca4",
    "accentSoft": "#e5f2f7",
    "onAccent": "#ffffff",
    "buttonBg": "#ffffff",
    "buttonBorder": "#cfd9e0",
    "buttonHoverBg": "#f1f6fb",
    "dangerText": "#a23e3e",
    "disabledText": "#9aa7b0",
    "disabledBg": "#eef2f4",
    "inputBg": "#ffffff",
    "inputBorder": "#cfd9e0",
    "tableHeaderBg": "#f6f8fa",
    "tableHeaderText": "#62727e",
    "gridColor": "#e5ebef",
    "tableAltBg": "#f7f9fb",
    "tableSelectionBg": "#e5f2f7",
    "tableSelectionText": "#1b2731",
    "tabBg": "#ffffff",
    "tabBorder": "#dce4ea",
    "tabSelectedBg": "#e8f2fb",
    "progressTrack": "#e8eef2",
    "statusBarBg": "#ffffff",
    "separator": "#d7dee7",
    "plotGrid": "#d7dee7",
    "plotAxis": "#667784",
    "plotLine": "#0f6cbd",
    "iconDefault": "#5d7083",
    "statusGood": "#17825b",
    "statusWarn": "#b3800f",
    "statusError": "#c24343",
    "statusIdle": "#94a2ae",
}

DARK = {
    "canvas": "#161d26",
    "sidebarBg": "#10161d",
    "sidebarBorder": "#26313d",
    "headBorder": "#26313d",
    "headText": "#e9f0f7",
    "iconBtnHover": "#1f2c3a",
    "navHover": "#1c2836",
    "navText": "#b8c5d1",
    "navMuted": "#64798c",
    "navSelectedBg": "#1c4468",
    "navSelectedText": "#ffffff",
    "textBase": "#dce5ee",
    "muted": "#8c9bab",
    "panelBg": "#1d2733",
    "panelBorder": "#2e3c4b",
    "noticeBg": "#1c3527",
    "accent": "#3f8fd9",
    "accentHover": "#529de3",
    "accentSoft": "#1e3f5e",
    "onAccent": "#ffffff",
    "buttonBg": "#263443",
    "buttonBorder": "#3b4a5a",
    "buttonHoverBg": "#2e4052",
    "dangerText": "#f08a8a",
    "disabledText": "#64758a",
    "disabledBg": "#232e3a",
    "inputBg": "#141c27",
    "inputBorder": "#34424f",
    "tableHeaderBg": "#212d3a",
    "tableHeaderText": "#9db0c0",
    "gridColor": "#2b3947",
    "tableAltBg": "#222e3b",
    "tableSelectionBg": "#1d4163",
    "tableSelectionText": "#dbe9f5",
    "tabBg": "#1c2632",
    "tabBorder": "#3a4a5a",
    "tabSelectedBg": "#1e4163",
    "progressTrack": "#2c3a48",
    "statusBarBg": "#1d2733",
    "separator": "#2b3947",
    "plotGrid": "#2f3d4c",
    "plotAxis": "#93a4b4",
    "plotLine": "#4da3ff",
    "iconDefault": "#8fa3b8",
    "statusGood": "#3ecf8e",
    "statusWarn": "#e3b23c",
    "statusError": "#f07b7b",
    "statusIdle": "#71849a",
}

PALETTES = {"light": LIGHT, "dark": DARK}
THEME_NAMES = tuple(PALETTES)

_STYLE_TEMPLATE = Template(
    """
* { font-family: "Microsoft YaHei UI"; font-size: 13px; color: $textBase; }
QWidget#appShell, QStackedWidget#pages, QWidget#pageRoot { background: $canvas; }

/* ---- 左侧导航 ---- */
QFrame#sidebar { background: $sidebarBg; border-right: 1px solid $sidebarBorder; }
QWidget#sidebarHead { background: transparent; border-bottom: 1px solid $headBorder; min-height: 48px; max-height: 48px; }
QLabel#sidebarLabel { color: $headText; font-size: 13px; font-weight: 600; }
QPushButton#sidebarButton, QPushButton#themeButton { min-width: 32px; max-width: 32px; min-height: 30px; max-height: 30px; padding: 0; color: $navText; background: transparent; border: none; font-size: 17px; border-radius: 6px; }
QPushButton#sidebarButton:hover, QPushButton#themeButton:hover { background: $iconBtnHover; }
QListWidget#navigation { background: transparent; border: none; padding: 4px 8px 12px 8px; outline: none; }
QListWidget#navigation::item { min-height: 40px; padding-left: 10px; border-radius: 7px; color: $navText; }
QListWidget#navigation::item:hover { background: $navHover; }
QListWidget#navigation::item:selected { background: $navSelectedBg; color: $navSelectedText; font-weight: 600; }
QListWidget#navigation::item:disabled { min-height: 26px; padding-top: 8px; color: $navMuted; font-size: 11px; }
QListWidget#navigation[collapsed="true"] { padding-left: 6px; padding-right: 6px; }
QListWidget#navigation[collapsed="true"]::item { padding-left: 0; padding-right: 0; }

QWidget#sidebarFooter { border-top: 1px solid $headBorder; }
QLabel#footerName { color: $muted; font-size: 11px; }
QLabel#footerValue { color: $navText; font-size: 12px; }

/* ---- 页面通用 ---- */
QLabel#pageTitle { font-size: 19px; font-weight: 600; }
QLabel#pageDescription, QLabel#mutedText { color: $muted; }
QLabel#mutedText[state="good"] { color: $statusGood; }
QLabel#mutedText[state="warn"] { color: $statusWarn; }
QLabel#mutedText[state="error"] { color: $statusError; }
QFrame#panel, QFrame#metricCard, QFrame#noticePanel, QFrame#serviceStrip { background: $panelBg; border: 1px solid $panelBorder; border-radius: 7px; }
QFrame#noticePanel { background: $noticeBg; }
QFrame#sepLine { color: $separator; }
QLabel#panelTitle { font-size: 14px; font-weight: 600; }
QLabel#metricValue { font-size: 22px; font-weight: 600; color: $accent; }
QLabel#successValue { font-size: 22px; font-weight: 600; color: $statusGood; }
QLabel#serviceValue { font-size: 14px; font-weight: 600; }
QLabel#goodStatus { color: $statusGood; font-weight: 600; }
QLabel#warnStatus { color: $statusWarn; font-weight: 600; }
QLabel#errorStatus { color: $statusError; font-weight: 600; }

QPushButton { min-height: 32px; padding: 0 13px; background: $buttonBg; color: $textBase; border: 1px solid $buttonBorder; border-radius: 6px; }
QPushButton:hover { background: $buttonHoverBg; border-color: $accentHover; }
QPushButton:focus { border: 2px solid $accent; }
QPushButton#primaryButton { background: $accent; color: $onAccent; border-color: $accent; }
QPushButton#primaryButton:hover { background: $accentHover; border-color: $accentHover; }
QPushButton#dangerButton { color: $dangerText; }
QPushButton:disabled { color: $disabledText; background: $disabledBg; border-color: $disabledBg; }

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit { min-height: 32px; background: $inputBg; color: $textBase; border: 1px solid $inputBorder; border-radius: 6px; padding: 0 8px; selection-background-color: $accentSoft; selection-color: $textBase; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTextEdit:focus { border: 1px solid $accent; }
QComboBox QAbstractItemView { background: $inputBg; color: $textBase; border: 1px solid $inputBorder; selection-background-color: $accentSoft; selection-color: $textBase; }

QHeaderView::section { background: $tableHeaderBg; color: $tableHeaderText; border: none; border-bottom: 1px solid $panelBorder; padding: 9px; }
QTableWidget { background: $panelBg; color: $textBase; border: none; gridline-color: $gridColor; selection-background-color: $tableSelectionBg; selection-color: $tableSelectionText; }
QTableWidget::item:alternate { background: $tableAltBg; }

QTabWidget::pane { border: none; top: -1px; }
QTabBar::tab { min-width: 150px; min-height: 36px; background: $tabBg; color: $muted; border: 1px solid $tabBorder; padding: 0 14px; }
QTabBar::tab:selected { background: $tabSelectedBg; color: $accent; font-weight: 600; }
QTabBar::tab:focus { border: 2px solid $accent; }

QScrollArea#pageScroll,
QScrollArea#pageScroll > QWidget > QWidget { background: $canvas; border: none; }

QProgressBar { min-height: 7px; max-height: 7px; border: none; border-radius: 3px; background: $progressTrack; text-align: center; }
QProgressBar::chunk { background: $accent; border-radius: 3px; }

QStatusBar { background: $statusBarBg; border-top: 1px solid $panelBorder; color: $muted; }

QToolTip { background: $panelBg; color: $textBase; border: 1px solid $panelBorder; padding: 4px; }
"""
)

# 兼容旧引用的浅色样式表（新代码请走 apply_theme / build_stylesheet）。
APP_STYLE = _STYLE_TEMPLATE.substitute(LIGHT)

_current = "light"


def build_stylesheet(palette: dict[str, str] | None = None) -> str:
    """由调色板生成 QSS；缺省使用当前主题。"""
    return _STYLE_TEMPLATE.substitute(palette or PALETTES[_current])


def theme_name() -> str:
    """当前主题名（light / dark）。"""
    return _current


def current_palette() -> dict[str, str]:
    """当前主题的颜色令牌表。"""
    return PALETTES[_current]


def apply_theme(app, name: str) -> str:
    """切换主题：更新当前令牌、QSS 与系统 QPalette，返回实际生效的主题名。"""
    global _current
    if name not in PALETTES:
        name = "light"
    _current = name
    palette = PALETTES[name]
    app.setStyleSheet(_STYLE_TEMPLATE.substitute(palette))
    app.setPalette(_make_qpalette(palette))
    return name


def _make_qpalette(palette: dict[str, str]) -> QPalette:
    """把颜色令牌落到 QPalette，让未写 QSS 的系统组件也跟随主题。"""
    qp = QPalette()
    qp.setColor(QPalette.ColorRole.Window, QColor(palette["canvas"]))
    qp.setColor(QPalette.ColorRole.WindowText, QColor(palette["textBase"]))
    qp.setColor(QPalette.ColorRole.Base, QColor(palette["inputBg"]))
    qp.setColor(QPalette.ColorRole.AlternateBase, QColor(palette["tableAltBg"]))
    qp.setColor(QPalette.ColorRole.Text, QColor(palette["textBase"]))
    qp.setColor(QPalette.ColorRole.Button, QColor(palette["buttonBg"]))
    qp.setColor(QPalette.ColorRole.ButtonText, QColor(palette["textBase"]))
    qp.setColor(QPalette.ColorRole.Highlight, QColor(palette["accent"]))
    qp.setColor(QPalette.ColorRole.HighlightedText, QColor(palette["onAccent"]))
    qp.setColor(QPalette.ColorRole.Link, QColor(palette["accent"]))
    qp.setColor(QPalette.ColorRole.PlaceholderText, QColor(palette["muted"]))
    qp.setColor(QPalette.ColorRole.ToolTipBase, QColor(palette["panelBg"]))
    qp.setColor(QPalette.ColorRole.ToolTipText, QColor(palette["textBase"]))
    qp.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(palette["disabledText"]))
    qp.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(palette["disabledText"]))
    qp.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(palette["disabledText"]))
    return qp
