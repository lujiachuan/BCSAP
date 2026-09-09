"""谱图库页：已预留入口，当前为占位页。独立文件便于后续单独实现。"""

from __future__ import annotations

from apps.desktop_client.pages.placeholder import PlaceholderPage
from apps.desktop_client.pages.registry import PageSpec

PAGE_SPEC = PageSpec(
    key="library",
    label="谱图库",
    icon="database",
    section="data",
    factory=lambda: PlaceholderPage("谱图库", "检索与多人访问界面按计划暂缓建设。"),
)
