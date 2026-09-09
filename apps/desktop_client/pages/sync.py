"""任务与同步页：已预留入口，当前为占位页。独立文件便于后续单独实现。"""

from __future__ import annotations

from apps.desktop_client.pages.placeholder import PlaceholderPage
from apps.desktop_client.pages.registry import PageSpec

PAGE_SPEC = PageSpec(
    key="sync",
    label="任务与同步",
    icon="sync",
    section="data",
    factory=lambda: PlaceholderPage("任务与同步", "显示本机任务、待上传数据和服务同步状态。"),
)
