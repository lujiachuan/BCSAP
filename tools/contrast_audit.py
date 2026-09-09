"""UI 令牌对比度核算工具（M0 交付物，随令牌调整随时回归）。

用法（仓库根目录）::

    python tools/contrast_audit.py

规则：
- 正文级文本 ≥ 4.5:1；大字号 / 必要非文本图形 ≥ 3:1；
- 玻璃（L2）表面的文本按「玻璃填充 alpha 合成在环境光最差底色之上」的最坏结果核算；
- 任一不达标以退出码 1 结束（供 CI/提交钩子使用）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.desktop_client.ui_tokens import DARK, LIGHT  # noqa: E402

# 半透明表面对比度验收用“最差合成背景”：浅色取最亮环境光、深色取最亮环境光（均取 A）。
_GLASS_WORST_BG = {"light": "ambientA", "dark": "ambientA"}


def parse_color(value: str) -> tuple[int, int, int, float]:
    """解析 '#rrggbb' 或 'rgba(r,g,b,a)' 为 (r,g,b,alpha)。"""
    text = str(value).strip()
    if text.startswith("rgba"):
        inner = text[text.index("(") + 1 : text.rindex(")")]
        parts = [float(p.strip()) for p in inner.split(",")]
        return int(parts[0]), int(parts[1]), int(parts[2]), (parts[3] if len(parts) > 3 else 1.0)
    return int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16), 1.0


def _srgb(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def luminance_of_rgba(color: tuple[int, int, int, float]) -> float:
    r, g, b, _alpha = color
    return 0.2126 * _srgb(r / 255) + 0.7152 * _srgb(g / 255) + 0.0722 * _srgb(b / 255)


def composite_over(fg: tuple[int, int, int, float], bg: tuple[int, int, int, float]) -> tuple[int, int, int, float]:
    """前景按 alpha 合成到不透明背景上。"""
    alpha = fg[3]
    return (
        round(fg[0] * alpha + bg[0] * (1 - alpha)),
        round(fg[1] * alpha + bg[1] * (1 - alpha)),
        round(fg[2] * alpha + bg[2] * (1 - alpha)),
        1.0,
    )


def contrast(fg: tuple[int, int, int, float], bg: tuple[int, int, int, float]) -> float:
    top, bottom = luminance_of_rgba(fg), luminance_of_rgba(bg)
    hi, lo = max(top, bottom), min(top, bottom)
    return (hi + 0.05) / (lo + 0.05)


def _hex_alpha(token: object, alpha_key: str) -> tuple[int, int, int, float]:
    value = token if isinstance(token, str) else token["color"]
    return parse_color(value)


def resolve(tokens: dict, spec: str) -> tuple[int, int, int, float]:
    """spec：token 名，或 'glass'（玻璃填充 alpha 合成到环境光最差底）。"""
    if spec == "glass":
        tint = parse_color(tokens["glassTint"])
        tint = (tint[0], tint[1], tint[2], float(tokens["glassAlpha"]))
        bg = parse_color(tokens[_GLASS_WORST_BG["light" if tokens is LIGHT else "dark"]])
        return composite_over(tint, bg)
    return parse_color(tokens[spec])


def audit_theme(name: str, tokens: dict) -> tuple[list[str], int]:
    """返回 (未达标说明列表, 通过条数)。"""
    pairs = [
        # (说明, 前景, 背景, 门槛档)  门槛：normal=4.5 / large=3.0
        ("正文 @ 面板底", "textBase", "surfacePanel", 4.5),
        ("次要文字 @ 面板底", "muted", "surfacePanel", 4.5),
        ("次要文字 @ 页面画布(L0最差)", "muted", "ambientC", 4.5),
        ("侧栏文字 @ 侧栏底", "navText", "sidebarBg", 4.5),
        ("侧栏分组文字 @ 侧栏底", "navMuted", "sidebarBg", 4.5),
        ("选中项文字 @ 选中底", "navSelectedText", "navSelectedBg", 4.5),
        ("主按钮白字 @ 强调底", "onAccent", "accentSolid", 4.5),
        ("主按钮悬停白字 @ 强调底", "onAccent", "accentSolidHover", 4.5),
        ("强调文字(链接) @ 面板底", "accent", "surfacePanel", 4.5),
        ("表头文字 @ 表头底", "tableHeaderText", "tableHeaderBg", 4.5),
        ("成功色 @ 面板底", "statusGood", "surfacePanel", 4.5),
        ("警告色 @ 面板底", "statusWarn", "surfacePanel", 4.5),
        ("错误色 @ 面板底", "statusError", "surfacePanel", 4.5),
        ("占位文字 @ 输入框底", "muted", "inputBg", 4.5),
        ("底部服务名 @ 侧栏底", "muted", "sidebarBg", 4.5),
        ("侧栏标题 @ 侧栏底", "headText", "sidebarBg", 4.5),
        ("玻璃卡正文(最差底)", "textBase", "glass", 4.5),
        ("玻璃卡次要文字(最差底)", "muted", "glass", 4.5),
        ("玻璃卡强调数字(最差底)", "accent", "glass", 4.5),
        ("玻璃卡成功数字(最差底)", "statusGood", "glass", 4.5),
    ]
    failures: list[str] = []
    passed = 0
    print(f"===== 主题：{'浅色' if name == 'light' else '深色'} =====")
    for label, fg_spec, bg_spec, threshold in pairs:
        fg = resolve(tokens, fg_spec)
        bg = resolve(tokens, bg_spec)
        ratio = contrast(fg, bg)
        ok = ratio >= threshold
        mark = "通过" if ok else "不通过"
        print(f"  {mark}  {ratio:5.2f}:1  {label}")
        if ok:
            passed += 1
        else:
            failures.append(f"[{name}] {label} = {ratio:.2f}:1（需 ≥{threshold}:1）")
    return failures, passed


def main() -> int:
    all_failures: list[str] = []
    total = 0
    for name, tokens in (("light", LIGHT), ("dark", DARK)):
        failures, passed = audit_theme(name, tokens)
        all_failures.extend(failures)
        total += passed
    print(f"\n通过 {total} 条；不达标 {len(all_failures)} 条")
    for failure in all_failures:
        print("  FAIL " + failure)
    return 1 if all_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
