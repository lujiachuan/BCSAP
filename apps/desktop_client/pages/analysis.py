"""谱图分析页：元数据与标注经 data_service showcase 读库。"""

from __future__ import annotations

from apps.desktop_client.pages.placeholder import PlaceholderPage
from apps.desktop_client.pages.registry import PageSpec

PAGE_SPEC = PageSpec(
    key="analysis",
    label="谱图分析",
    icon="analysis",
    section="data",
    factory=lambda: PlaceholderPage(
        "谱图分析",
        "GET /api/v1/showcase/spectra/{id} 返回 working 标注与校正摘要（已接 cluster_ms）。"
        "重标注/质量轴写回仍在旁路 web_manager；算法迁入 packages/spectrum 后由 data_service 承接。",
    ),
)