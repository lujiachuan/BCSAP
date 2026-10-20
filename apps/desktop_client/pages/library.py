"""谱图库页：经 data_service showcase 读 cluster_ms。"""

from __future__ import annotations

from apps.desktop_client.pages.placeholder import PlaceholderPage
from apps.desktop_client.pages.registry import PageSpec

PAGE_SPEC = PageSpec(
    key="library",
    label="谱图库",
    icon="database",
    section="data",
    factory=lambda: PlaceholderPage(
        "谱图库",
        "数据走共享服务 Showcase 只读接口：GET /api/v1/showcase/spectra "
        "（已接本机 cluster_ms）。完整检索 UI 建设中；客户端不直连数据库。",
    ),
)
