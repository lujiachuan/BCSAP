"""Windows 原生标题栏着色（由主题令牌驱动，M0 接入主线）。

通过 DWM 窗口属性把原生标题栏、边框与文字颜色同步为当前主题：
- 深色主题：开启沉浸式暗色标题栏（浅色字形），标题栏/边框涂成侧栏同色；
- 浅色主题：关闭沉浸式暗色，标题栏涂成侧栏同色，消除与内容区的割裂感。

不同 Windows 版本支持程度不同：较老版本对不受支持的属性返回 E_INVALIDARG，
调用方（main._apply_native_chrome）捕获异常后静默回退系统默认外观。
非 Windows 平台调用 ``apply_native_chrome`` 是无操作。
"""

from __future__ import annotations

import ctypes
import sys

# dwmapi.h 中的 DWM 窗口属性常量
_DWMWA_USE_IMMERSIVE_DARK_MODE_20 = 20  # Windows 10 2004+
_DWMWA_USE_IMMERSIVE_DARK_MODE_19 = 19  # Windows 10 1709-1909
_DWMWA_BORDER_COLOR = 34  # Windows 11 22H2+
_DWMWA_CAPTION_COLOR = 35  # Windows 11 22H2+
_DWMWA_TEXT_COLOR = 36  # Windows 11 22H2+


def colorref(hex_color: str) -> int:
    """把 '#rrggbb' 转成 DWM 的 COLORREF（0x00bbggrr）。"""
    value = hex_color.lstrip("#")
    return int(value[4:6] + value[2:4] + value[0:2], 16)


def _relative_luminance(hex_color: str) -> float:
    rgb = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _set_attribute(hwnd: int, attribute: int, value: int) -> bool:
    """设置单个 DWM 窗口属性，返回是否成功。"""
    try:
        dwmapi = ctypes.windll.dwmapi
    except (AttributeError, OSError):
        return False
    data = ctypes.c_int(value)
    result = dwmapi.DwmSetWindowAttribute(
        ctypes.c_void_p(hwnd), attribute, ctypes.byref(data), ctypes.sizeof(data)
    )
    return result == 0


def apply_native_chrome(window, palette: dict[str, str] | None = None) -> None:
    """让窗口原生标题栏与当前主题同色；仅 Windows 生效，可重复调用。

    palette 传当前主题颜色令牌（ui_tokens LIGHT/DARK），
    缺省时按旧默认深色侧栏处理（兼容直接调用）。
    """
    if sys.platform != "win32":
        return
    tokens = palette or {
        "sidebarBg": "#10161d",
        "sidebarBorder": "#26313d",
        "headText": "#e9f0f7",
        "canvas": "#161d26",
    }
    try:
        hwnd = int(window.winId())
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return

    sidebar_color = tokens["sidebarBg"]
    border_color = tokens["sidebarBorder"]
    text_color = tokens["headText"]
    dark_theme = _relative_luminance(sidebar_color) < 0.3

    # 1) 沉浸式暗色标题栏：深色主题用浅色字形，浅色主题关闭（退回系统深字）。
    dark_mode = 1 if dark_theme else 0
    if not _set_attribute(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE_20, dark_mode):
        _set_attribute(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE_19, dark_mode)

    # 2) Windows 11 22H2+：标题栏、边框与文字直接涂成主题色。
    _set_attribute(hwnd, _DWMWA_CAPTION_COLOR, colorref(sidebar_color))
    _set_attribute(hwnd, _DWMWA_BORDER_COLOR, colorref(border_color))
    _set_attribute(hwnd, _DWMWA_TEXT_COLOR, colorref(text_color))
