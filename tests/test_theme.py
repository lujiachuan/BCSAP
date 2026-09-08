"""主题调色板与样式表生成的纯逻辑验证。"""

import unittest

from apps.desktop_client.theme import PALETTES, THEME_NAMES, build_stylesheet


class ThemePaletteTests(unittest.TestCase):
    def test_palettes_share_all_tokens(self) -> None:
        keys = {name: set(palette) for name, palette in PALETTES.items()}
        base = keys["light"]
        for name, key_set in keys.items():
            self.assertEqual(base, key_set, f"{name} 缺少或多出颜色令牌")

    def test_stylesheet_builds_without_leftover_tokens(self) -> None:
        for name in THEME_NAMES:
            css = build_stylesheet(PALETTES[name])
            self.assertNotIn("$", css, f"{name} 样式表存在未替换的令牌")
            self.assertIn("QFrame#sidebar", css)
            self.assertIn("QFrame#panel", css)

    def test_dark_and_light_differ(self) -> None:
        self.assertNotEqual(PALETTES["light"]["canvas"], PALETTES["dark"]["canvas"])
        self.assertNotEqual(PALETTES["light"]["panelBg"], PALETTES["dark"]["panelBg"])


if __name__ == "__main__":
    unittest.main()
