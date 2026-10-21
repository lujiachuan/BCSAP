"""桌面客户端复用组件（只放跨页面可复用的布局/绘图小件）。"""

from apps.desktop_client.components.compact import (
    CompactForm,
    FoldSection,
    RangeBar,
    StatusStrip,
    mark_compact,
    muted_label,
)

__all__ = [
    "CompactForm",
    "FoldSection",
    "RangeBar",
    "StatusStrip",
    "mark_compact",
    "muted_label",
]
