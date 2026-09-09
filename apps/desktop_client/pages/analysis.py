"""谱图分析页：已预留入口，当前为占位页。独立文件便于后续单独实现。"""

from __future__ import annotations

from apps.desktop_client.pages.placeholder import PlaceholderPage
from apps.desktop_client.pages.registry import PageSpec

PAGE_SPEC = PageSpec(
    key="analysis",
    label="谱图分析",
    icon="analysis",
    section="data",
    factory=lambda: PlaceholderPage("谱图分析", "分析工具将在谱图库需求明确后一起设计。"),
)
