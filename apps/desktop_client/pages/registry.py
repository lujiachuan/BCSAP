"""页面注册表：每个侧边栏入口页面的自述与装配数据。

各页面模块只在这里注册一次（由 pages 包 __init__ 统一聚合），
主窗口不感知具体页面类，只按注册表装配侧栏与堆叠页面。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtWidgets import QWidget

# 侧边栏分区（沿用历史 NAVIGATION 分组）。元组顺序即侧栏展示顺序。
SECTIONS: tuple[tuple[str, str], ...] = (
    ("实验控制", "control"),
    ("数据与系统", "data"),
)


@dataclass(frozen=True, slots=True)
class PageSpec:
    """一个侧边栏入口页面的描述：身份、所属分区与构造方式。"""

    key: str
    label: str
    icon: str
    section: str
    factory: Callable[[], QWidget]


_registry: dict[str, PageSpec] = {}


def register(spec: PageSpec) -> None:
    """登记一个页面；重复 key 视为装配错误，立即失败。"""

    if spec.key in _registry:
        raise ValueError(f"页面 key 重复注册：{spec.key!r}")
    _registry[spec.key] = spec


def page_specs() -> tuple[PageSpec, ...]:
    """按注册顺序返回全部页面描述（顺序由 pages/__init__.py 的聚合列表决定）。"""

    return tuple(_registry.values())
