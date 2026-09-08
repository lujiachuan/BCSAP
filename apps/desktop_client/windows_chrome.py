"""预留工具（当前未接入）：Windows 原生标题栏着色。

当前客户端采用“全浅色一体化”设计，标题栏使用系统默认浅色，主窗口不再调用
本模块。若未来切换为整体深色主题，可把标题栏/边框涂成页面主题色，保留原生
窗口的全部行为（拖拽、贴边分屏、双击最大化、系统菜单、圆角等）。非 Windows
平台调用 ``apply_native_chrome`` 是无操作。

- 沉浸式暗色（DWMWA_USE_IMMERSIVE_DARK_MODE）：标题栏文字与按钮使用浅色字形；
- 标题栏/边框颜色（DWMWA_CAPTION_COLOR / DWMWA_BORDER_COLOR，Windows 11 22H2+）：
  把标题栏涂成与侧栏一致的主题色，消除“浅色标题栏压在深色侧栏上方”的割裂感；
- 文字颜色（DWMWA_TEXT_COLOR）：与侧栏文字颜色保持一致。

不同 Windows 版本支持程度不同：较老的版本对不受支持的属性返回 E_INVALIDARG，
此处静默忽略，退回系统默认外观。
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

# 与 apps.desktop_client.theme 中侧边栏保持一致
SIDEBAR_COLOR = "#152a3a"
SIDEBAR_TEXT_COLOR = "#dce7ef"


def colorref(hex_color: str) -> int:
    """把 '#rrggbb' 转成 DWM 的 COLORREF（0x00bbggrr）。"""
    value = hex_color.lstrip("#")
    return int(value[4:6] + value[2:4] + value[0:2], 16)


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


def apply_native_chrome(window) -> None:
    """让窗口原生标题栏与侧边栏同色；仅 Windows 生效，可重复调用。"""
    if sys.platform != "win32":
        return
    try:
        hwnd = int(window.winId())
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return

    # 1) 深色标题栏：文字与窗口按钮使用浅色字形。
    #    新构建优先用常量 20，老版本退回常量 19。
    if not _set_attribute(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE_20, 1):
        _set_attribute(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE_19, 1)

    # 2) Windows 11 22H2+：标题栏、边框与文字直接涂成侧栏配色。
    _set_attribute(hwnd, _DWMWA_CAPTION_COLOR, colorref(SIDEBAR_COLOR))
    _set_attribute(hwnd, _DWMWA_BORDER_COLOR, colorref(SIDEBAR_COLOR))
    _set_attribute(hwnd, _DWMWA_TEXT_COLOR, colorref(SIDEBAR_TEXT_COLOR))
