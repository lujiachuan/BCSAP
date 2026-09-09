"""桌面客户端设计令牌 v2（M0 起单一事实来源）。

结构
----
- ``LIGHT`` / ``DARK``：颜色令牌（扁平 dict，hex 或 rgba() 字符串）。
  保留 v1 全部键名（theme.QSS 模板与旧代码引用不变），并新增：
  表面体系（surface* / glass*）、强调色体系（accentSolid/accentSolidHover/accentGlow）、
  语义弱底色（status*Bg）、环境光（ambientA/B/C）、危险底色（dangerBg）等。
- ``METERS``：非颜色尺度（字号、圆角、间距、动效时长）——浅/深主题共用。
- 对比度硬门槛（M0 已修复，见 tools/contrast_audit.py）：
  正文级文本 ≥ 4.5:1、大字号与非文本图形 ≥ 3:1；半透明表面按最差环境光背景合成核算。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 浅色主题
# ---------------------------------------------------------------------------
LIGHT = {
    # ---- 基底与表面体系（L0 页面基底由 AmbientCanvas 自绘，此处仅兜底色）----
    "canvas": "#eef2f6",
    "surfaceCanvas": "#eef2f6",
    "surfacePanel": "#ffffff",
    "panelBg": "#ffffff",
    "panelBorder": "#d7dee7",
    "tableHeaderBg": "#f6f8fa",
    "tableHeaderText": "#5e6f80",
    "gridColor": "#e5ebef",
    "tableAltBg": "#f7f9fb",
    "tableHoverBg": "#eef4fa",
    "tableSelectionBg": "#e5f2f7",
    "tableSelectionText": "#1b2731",

    # ---- 文本 ---- 
    "textBase": "#17212b",
    "muted": "#59697b",
    "headText": "#1c2936",
    "placeholder": "#8a97a6",
    "dangerText": "#a23e3e",

    # ---- 侧栏与导航 ----
    "sidebarBg": "#f2f5f9",
    "sidebarBorder": "#d8dfe8",
    "headBorder": "#e1e7ee",
    "navHover": "#e5ecf3",
    "navText": "#3e4c5a",
    "navMuted": "#5f7185",
    "navSelectedBg": "#e3f0fc",
    "navSelectedText": "#0b5ca4",
    "iconBtnHover": "#e0e8f0",
    "iconDefault": "#5d7083",

    # ---- 强调色体系 ----
    "accent": "#0f6cbd",
    "accentHover": "#0b5ca4",
    "accentSolid": "#0f6cbd",
    "accentSolidHover": "#0b5ca4",
    "accentSoft": "#e5f2f7",
    "accentGlow": "#cfe2f6",
    "onAccent": "#ffffff",
    "focusRing": "#0f6cbd",

    # ---- 控件 ----
    "buttonBg": "#ffffff",
    "buttonBorder": "#cfd9e0",
    "buttonHoverBg": "#f1f6fb",
    "disabledText": "#9aa7b0",
    "disabledBg": "#eef2f4",
    "inputBg": "#ffffff",
    "inputBorder": "#cfd9e0",
    "tabBg": "#ffffff",
    "tabBorder": "#dce4ea",
    "tabSelectedBg": "#e8f2fb",
    "progressTrack": "#e8eef2",
    "statusBarBg": "#ffffff",
    "separator": "#d7dee7",
    "noticeBg": "#e8f4ee",
    "dangerBg": "#fbe9e9",

    # ---- 语义状态（文字/图标 + 弱底成组）----
    "statusGood": "#17825b",
    "statusWarn": "#8f6400",
    "statusError": "#c24343",
    "statusIdle": "#6b7e93",
    "statusGoodBg": "#e2f3ea",
    "statusWarnBg": "#fdf3dd",
    "statusErrorBg": "#fbeaea",
    "statusIdleBg": "#eef1f5",

    # ---- 滚动条 ----
    "scrollbarHandle": "#c3cdd8",
    "scrollbarHandleHover": "#9fb2c4",

    # ---- 玻璃（L2 半透明表面；同主题另有 alpha 分量供绘制使用）----
    "glassTint": "#ffffff",
    "glassTintHover": "#ffffff",
    "glassAlpha": 0.66,
    "glassAlphaHover": 0.84,
    "glassStroke": "rgba(109, 134, 165, 0.38)",
    "glassHighlight": "rgba(255, 255, 255, 0.95)",
    "glassRing": "rgba(64, 90, 120, 0.16)",

    # ---- 环境光（L0）----
    "ambientA": "#f4f8fc",
    "ambientB": "#e9f0f7",
    "ambientC": "#e0e9f2",

    # ---- 图表（pyqtgraph 阶段前的占位用色，M2 重调）----
    "plotGrid": "#d7dee7",
    "plotAxis": "#5f7285",
    "plotLine": "#0f6cbd",
}

# ---------------------------------------------------------------------------
# 深色主题
# ---------------------------------------------------------------------------
DARK = {
    # ---- 基底与表面体系 ----
    "canvas": "#161d26",
    "surfaceCanvas": "#161d26",
    "surfacePanel": "#1d2733",
    "panelBg": "#1d2733",
    "panelBorder": "#2e3c4b",
    "tableHeaderBg": "#212d3a",
    "tableHeaderText": "#9db0c0",
    "gridColor": "#2b3947",
    "tableAltBg": "#222e3b",
    "tableHoverBg": "#283646",
    "tableSelectionBg": "#1d4163",
    "tableSelectionText": "#dbe9f5",

    # ---- 文本 ----
    "textBase": "#dce5ee",
    "muted": "#8c9bab",
    "headText": "#e9f0f7",
    "placeholder": "#64758a",
    "dangerText": "#f08a8a",

    # ---- 侧栏与导航 ----
    "sidebarBg": "#10161d",
    "sidebarBorder": "#26313d",
    "headBorder": "#26313d",
    "navHover": "#1c2836",
    "navText": "#b8c5d1",
    "navMuted": "#7b8fa3",
    "navSelectedBg": "#1c4468",
    "navSelectedText": "#ffffff",
    "iconBtnHover": "#1f2c3a",
    "iconDefault": "#8fa3b8",

    # ---- 强调色体系 ----
    "accent": "#58a8ec",
    "accentHover": "#6bb2f0",
    "accentSolid": "#2e74bd",
    "accentSolidHover": "#2766a6",
    "accentSoft": "#1e3f5e",
    "accentGlow": "#2f77c2",
    "onAccent": "#ffffff",
    "focusRing": "#58a8ec",

    # ---- 控件 ----
    "buttonBg": "#263443",
    "buttonBorder": "#3b4a5a",
    "buttonHoverBg": "#2e4052",
    "disabledText": "#64758a",
    "disabledBg": "#232e3a",
    "inputBg": "#141c27",
    "inputBorder": "#34424f",
    "tabBg": "#1c2632",
    "tabBorder": "#3a4a5a",
    "tabSelectedBg": "#1e4163",
    "progressTrack": "#2c3a48",
    "statusBarBg": "#1d2733",
    "separator": "#2b3947",
    "noticeBg": "#1c3527",
    "dangerBg": "#3a1d24",

    # ---- 语义状态 ----
    "statusGood": "#3ecf8e",
    "statusWarn": "#e3b23c",
    "statusError": "#f07b7b",
    "statusIdle": "#71849a",
    "statusGoodBg": "#16301f",
    "statusWarnBg": "#3a2d0e",
    "statusErrorBg": "#3a1c22",
    "statusIdleBg": "#232c36",

    # ---- 滚动条 ----
    "scrollbarHandle": "#3a4a5c",
    "scrollbarHandleHover": "#54708c",

    # ---- 玻璃 ----
    "glassTint": "#1b2633",
    "glassTintHover": "#27364a",
    "glassAlpha": 0.60,
    "glassAlphaHover": 0.72,
    "glassStroke": "rgba(255, 255, 255, 0.10)",
    "glassHighlight": "rgba(255, 255, 255, 0.12)",
    "glassRing": "rgba(0, 0, 0, 0.30)",

    # ---- 环境光 ----
    "ambientA": "#1a2533",
    "ambientB": "#121b28",
    "ambientC": "#0f1723",

    # ---- 图表占位 ----
    "plotGrid": "#2f3d4c",
    "plotAxis": "#93a4b4",
    "plotLine": "#4da3ff",
}

# ---------------------------------------------------------------------------
# 非颜色尺度（两主题共用）
# ---------------------------------------------------------------------------
METERS = {
    "font": {"caption": 11, "body": 13, "emphasis": 14, "subtitle": 16, "title": 20, "display": 26},
    "radius": {"control": 8, "card": 12, "panel": 16, "dialog": 20},
    "space": {"s1": 4, "s2": 8, "s3": 12, "s4": 16, "s5": 20, "s6": 24},
    "motion": {"fast": 120, "base": 180, "slow": 260},
}

PALETTE_KEYS = tuple(LIGHT)
assert set(LIGHT) == set(DARK), "light/dark 颜色令牌集合必须一致"
