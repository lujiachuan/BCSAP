"""Windows 原生标题栏着色工具的纯逻辑验证。"""

import unittest

from apps.desktop_client.windows_chrome import apply_native_chrome, colorref


class ColorRefTests(unittest.TestCase):
    def test_colorref_packing(self) -> None:
        # '#rrggbb' 需要打包成 COLORREF 的 0x00bbggrr
        self.assertEqual(colorref("#152a3a"), 0x003A2A15)
        self.assertEqual(colorref("#dce7ef"), 0x00EFE7DC)

    def test_colorref_without_hash(self) -> None:
        self.assertEqual(colorref("0f6cbd"), 0x00BD6C0F)


class ApplyChromeTests(unittest.TestCase):
    def test_invalid_window_is_ignored(self) -> None:
        # 传入没有 winId 的对象不应抛出任何异常
        apply_native_chrome(object())
        apply_native_chrome(None)

    def test_returns_none(self) -> None:
        self.assertIsNone(apply_native_chrome(object()))


if __name__ == "__main__":
    unittest.main()
