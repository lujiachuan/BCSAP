"""样品管理页：已预留入口，当前为占位页。独立文件便于后续单独实现。"""

from __future__ import annotations

from apps.desktop_client.pages.placeholder import PlaceholderPage
from apps.desktop_client.pages.registry import PageSpec

PAGE_SPEC = PageSpec(
    key="samples",
    label="样品管理",
    icon="sample",
    section="control",
    factory=lambda: PlaceholderPage("样品管理", "维护样品编号、类型、批次和实验备注。"),
)
