"""桌面客户端的浅色 / 深色主题（令牌 v2 装配层）。

设计令牌位于 ``ui_tokens``（颜色 + 非颜色尺度）。本模块负责：
1. 把令牌组装成 QSS（分段模板：基础 / 侧栏 / 表面体系 / 控件 / 数据 / 浮层）；
2. 提供 ``apply_theme`` / ``theme_name`` / ``current_palette`` 等既有 API；
3. 由颜色令牌生成系统 QPalette。

M0 视觉约定
-----------
- L0 页面基底由 ``AmbientCanvas`` 自绘，页面/滚动容器透明，不再画纯色 canvas；
- L1 数据工作面（Panel/表格/表单/图表容器）不透明，保证对比度与连续阅读；
- L2 强调与浮层（Hero/状态卡/指标卡）由 ``GlassCard`` 自绘半透明+高光描边；
- 焦点统一为 1px 边框换色，不改变控件几何。
"""

from __future__ import annotations

from string import Template

from PySide6.QtGui import QColor, QPalette

from apps.desktop_client.ui_tokens import DARK, LIGHT, METERS, PALETTE_KEYS

PALETTES = {"light": LIGHT, "dark": DARK}
THEME_NAMES = tuple(PALETTES)
assert set(PALETTE_KEYS) == set(DARK) == set(LIGHT)

# 兼容旧引用：新代码请走 ui_tokens。
assert LIGHT is PALETTES["light"]
assert DARK is PALETTES["dark"]

# ---------------------------------------------------------------------------
# QSS 模板（颜色占位符来自令牌；尺度占位符来自 METERS 的 px 化整数）
# ---------------------------------------------------------------------------
_STYLE_TEMPLATE = Template(
    """
/* ================= 基础 ================= */
* { font-family: "Microsoft YaHei UI"; font-size: ${fontBody}px; color: $textBase; }
QWidget#appShell { background: transparent; }
QStackedWidget#pages { background: transparent; }
QWidget#pageRoot { background: transparent; }
QScrollArea#pageScroll { background: transparent; border: none; }
QScrollArea#pageScroll QWidget#qt_scrollarea_viewport { background: transparent; }

/* ================= 左侧导航 ================= */
QFrame#sidebar { background: $sidebarBg; border-right: 1px solid $sidebarBorder; }
QWidget#sidebarHead { background: transparent; border-bottom: 1px solid $headBorder;
                      min-height: 48px; max-height: 48px; }
QLabel#sidebarLabel { color: $headText; font-size: ${fontSubtitle}px; font-weight: 600; }
QPushButton#sidebarButton, QPushButton#themeButton {
    min-width: 32px; max-width: 32px; min-height: 30px; max-height: 30px; padding: 0;
    color: $navText; background: transparent; border: none; font-size: 17px;
    border-radius: ${radiusControl}px; }
QPushButton#sidebarButton:hover, QPushButton#themeButton:hover { background: $iconBtnHover; }
QPushButton#sidebarButton:focus, QPushButton#themeButton:focus { border: none; }

QListWidget#navigation { background: transparent; border: none; padding: 4px 8px 12px 8px;
                         outline: none; }
QListWidget#navigation::item { min-height: 40px; padding-left: 10px;
                               border-radius: ${radiusControl}px; color: $navText; }
QListWidget#navigation::item:hover { background: $navHover; }
QListWidget#navigation::item:selected { background: $navSelectedBg; color: $navSelectedText;
                                        font-weight: 600; }
QListWidget#navigation::item:focus { border: none; }
QListWidget#navigation::item:disabled { min-height: 26px; padding-top: 8px;
                                        color: $navMuted; font-size: ${fontCaption}px;
                                        font-weight: 600; letter-spacing: 1px; }
QListWidget#navigation[collapsed="true"] { padding-left: 6px; padding-right: 6px; }
QListWidget#navigation[collapsed="true"]::item { padding-left: 0; padding-right: 0; }

QWidget#sidebarFooter { border-top: 1px solid $headBorder; }
QLabel#footerName { color: $muted; font-size: ${fontCaption}px; }
QLabel#footerValue { color: $navText; font-size: ${fontCaption}px; }

/* ================= 表面体系 ================= */
QLabel#pageTitle { font-size: ${fontTitle}px; font-weight: 600; }
QLabel#pageDescription, QLabel#mutedText { color: $muted; }
QLabel#mutedText[state="good"] { color: $statusGood; }
QLabel#mutedText[state="warn"] { color: $statusWarn; }
QLabel#mutedText[state="error"] { color: $statusError; }

/* L1 数据工作面：不透明 */
QFrame#panel { background: $surfacePanel; border: 1px solid $panelBorder;
               border-radius: ${radiusPanel}px; }
QFrame#noticePanel { background: $noticeBg; border: 1px solid $panelBorder;
                     border-radius: ${radiusCard}px; }
QFrame#sepLine { color: $separator; }

/* L2 玻璃面（由 GlassCard 自绘，QSS 不画背景避免双重绘制） */
QFrame#glassCard, QFrame#serviceCard, QFrame#heroCard, QFrame#contextCard,
QFrame#quickCard, QFrame#metricCard { border: none; background: transparent; }

QLabel#panelTitle { font-size: ${fontEmphasis}px; font-weight: 600; }
QLabel#metricValue, QLabel#successValue { font-size: ${fontDisplay}px; font-weight: 600; }
QLabel#metricValue { color: $accent; }
QLabel#successValue { color: $statusGood; }
QLabel#serviceValue { font-size: ${fontEmphasis}px; font-weight: 600; }
QLabel#cardCaption { color: $muted; font-size: ${fontCaption}px; }
QLabel#heroTitle { font-size: ${fontSubtitle}px; font-weight: 600; }
QLabel#heroValue { font-size: ${fontTitle}px; font-weight: 600; }
QLabel#goodStatus { color: $statusGood; font-weight: 600; }
QLabel#warnStatus { color: $statusWarn; font-weight: 600; }
QLabel#errorStatus { color: $statusError; font-weight: 600; }
QLabel#modeChip { background: $accentSoft; color: $accent; font-weight: 600;
                  font-size: ${fontCaption}px;
                  border-radius: ${radiusControl}px; padding: 3px 10px; }

/* ================= 按钮 ================= */
QPushButton { min-height: 32px; padding: 0 14px; background: $buttonBg; color: $textBase;
              border: 1px solid $buttonBorder; border-radius: ${radiusControl}px; }
QPushButton:hover { background: $buttonHoverBg; border-color: $accentHover; }
QPushButton:pressed { background: $disabledBg; }
QPushButton:focus { border: 1px solid $focusRing; }
QPushButton#primaryButton { background: $accentSolid; color: $onAccent;
                            border: 1px solid $accentSolid; font-weight: 600; }
QPushButton#primaryButton:hover { background: $accentSolidHover; border-color: $accentSolidHover; }
QPushButton#primaryButton:pressed { background: $accentSolidHover; }
QPushButton#primaryButton:disabled { color: $disabledText; background: $disabledBg;
                                     border-color: $disabledBg; }
QPushButton#dangerButton { color: $dangerText; }
QPushButton#dangerButton:hover { background: $dangerBg; border-color: $dangerText; }
QPushButton#inlineRetry { min-height: 22px; max-height: 22px; padding: 0 8px;
                          background: transparent; border: none; color: $accent;
                          font-size: ${fontCaption}px; }
QPushButton#inlineRetry:hover { color: $accentHover; text-decoration: underline; }
QPushButton#inlineRetry:focus { border: none; }
QPushButton#plotToolButton { min-height: 26px; max-height: 26px; padding: 0 10px;
                             color: $muted; background: transparent; }
QPushButton:disabled { color: $disabledText; background: $disabledBg;
                       border-color: $disabledBg; }

/* ================= 输入与选择 ================= */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit {
    min-height: 32px; background: $inputBg; color: $textBase;
    border: 1px solid $inputBorder; border-radius: ${radiusControl}px;
    padding: 0 8px; selection-background-color: $accentSoft; selection-color: $textBase; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTextEdit:focus {
    border: 1px solid $focusRing; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QTextEdit:disabled { background: $disabledBg; color: $disabledText; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox::down-arrow { image: none; border-left: 4px solid transparent;
                        border-right: 4px solid transparent;
                        border-top: 5px solid $muted; margin-right: 6px; }
QComboBox QAbstractItemView { background: $surfacePanel; color: $textBase;
                              border: 1px solid $panelBorder; border-radius: ${radiusControl}px;
                              padding: 4px; selection-background-color: $accentSoft;
                              selection-color: $textBase; outline: none; }

/* ================= 数据区：表格 / 滚动条 ================= */
QHeaderView::section { background: $tableHeaderBg; color: $tableHeaderText; border: none;
                       border-bottom: 1px solid $panelBorder; padding: 8px 10px;
                       font-weight: 600; }
QTableWidget { background: $surfacePanel; color: $textBase; border: none;
               gridline-color: $gridColor;
               selection-background-color: $tableSelectionBg;
               selection-color: $tableSelectionText; }
QTableWidget::item { padding: 4px 6px; }
QTableWidget::item:hover { background: $tableHoverBg; }
QTableWidget::item:selected { background: $tableSelectionBg; color: $tableSelectionText; }
QTableWidget::item:alternate { background: $tableAltBg; }
QTableCornerButton::section { background: $tableHeaderBg; border: none; }

QListWidget#settingsNavigation { background: $surfacePanel; border: 1px solid $panelBorder;
                                  border-radius: ${radiusPanel}px; padding: 8px; outline: none; }
QListWidget#settingsNavigation::item { min-height: 38px; padding: 0 12px;
                                       border-radius: ${radiusControl}px; color: $muted; }
QListWidget#settingsNavigation::item:hover { background: $tableHoverBg; color: $textBase; }
QListWidget#settingsNavigation::item:selected { background: $accentSoft; color: $accent;
                                                font-weight: 600; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $scrollbarHandle; border-radius: 4px;
                              min-height: 28px; }
QScrollBar::handle:vertical:hover { background: $scrollbarHandleHover; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: $scrollbarHandle; border-radius: 4px;
                                min-width: 28px; }
QScrollBar::handle:horizontal:hover { background: $scrollbarHandleHover; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* ================= 标签页 / 进度 / 状态栏 ================= */
QTabWidget::pane { border: none; top: -1px; }
QTabBar::tab { min-width: 150px; min-height: 36px; background: $tabBg; color: $muted;
               border: 1px solid $tabBorder; border-bottom: none;
               border-top-left-radius: ${radiusControl}px;
               border-top-right-radius: ${radiusControl}px; padding: 0 14px; }
QTabBar::tab:hover { color: $textBase; }
QTabBar::tab:selected { background: $tabSelectedBg; color: $accent; font-weight: 600; }
QTabBar::tab:focus { border: 1px solid $focusRing; border-bottom: none; }
QTabBar::tab:disabled { color: $disabledText; background: $disabledBg; }

QProgressBar { min-height: 7px; max-height: 7px; border: none; border-radius: 4px;
               background: $progressTrack; text-align: center; }
QProgressBar::chunk { background: $accentSolid; border-radius: 4px; }

QStatusBar { background: $statusBarBg; border-top: 1px solid $panelBorder; color: $muted; }

/* ================= 浮层：菜单 / 提示 ================= */
QToolTip { background: $surfacePanel; color: $textBase; border: 1px solid $panelBorder;
           border-radius: ${radiusControl}px; padding: 5px 8px; }
QMenu { background: $surfacePanel; color: $textBase; border: 1px solid $panelBorder;
        border-radius: ${radiusControl}px; padding: 4px; }
QMenu::item { padding: 6px 22px 6px 12px; border-radius: 6px; }
QMenu::item:selected { background: $accentSoft; color: $accent; }
QMenu::separator { height: 1px; background: $separator; margin: 4px 8px; }
"""
)

# 旧样式表常量 APP_STYLE 已随令牌 v2 移除，新代码请走 apply_theme / build_stylesheet。


def _qss_substitutions(palette: dict[str, str]) -> dict[str, object]:
    """颜色令牌 + px 化尺度令牌，供 QSS 模板替换。"""
    merged: dict[str, object] = dict(palette)
    font = METERS["font"]
    for key, value in font.items():
        merged[f"font{key.capitalize()}"] = value
    for key, value in METERS["radius"].items():
        merged[f"radius{key.capitalize()}"] = value
    return merged


def build_stylesheet(palette: dict[str, str] | None = None) -> str:
    """由调色板生成 QSS；缺省使用当前主题。"""
    tokens = palette or PALETTES[_current]
    return _STYLE_TEMPLATE.substitute(_qss_substitutions(tokens))


_current = "light"


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
    app.setStyleSheet(build_stylesheet(palette))
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
    # 选中（含输入内选区、未写 QSS 的列表）统一使用实体强调色，保证白字对比度。
    qp.setColor(QPalette.ColorRole.Highlight, QColor(palette["accentSolid"]))
    qp.setColor(QPalette.ColorRole.HighlightedText, QColor(palette["onAccent"]))
    qp.setColor(QPalette.ColorRole.Link, QColor(palette["accent"]))
    qp.setColor(QPalette.ColorRole.PlaceholderText, QColor(palette["muted"]))
    qp.setColor(QPalette.ColorRole.ToolTipBase, QColor(palette["panelBg"]))
    qp.setColor(QPalette.ColorRole.ToolTipText, QColor(palette["textBase"]))
    disabled_text = QColor(palette["disabledText"])
    disabled_bg = QColor(palette["disabledBg"])
    qp.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled_text)
    qp.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled_text)
    qp.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled_text)
    qp.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight, disabled_bg)
    qp.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.HighlightedText, disabled_text)
    return qp
