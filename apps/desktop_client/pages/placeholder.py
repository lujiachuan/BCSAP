"""尚未进入实施阶段的通用占位页面。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from apps.desktop_client.pages.common import page_layout
from apps.desktop_client.surfaces import GlassCard
from apps.desktop_client.widgets import PageHeading


class PlaceholderPage(QWidget):
    """尚未进入实施阶段的页面。"""

    CAPABILITIES = {
        "样品管理": ("样品编号与批次", "实验备注与状态", "与实验任务关联"),
        "谱图库": ("按条件检索谱图", "查看版本与来源", "受控共享与引用"),
        "谱图分析": ("峰值与区间分析", "多谱图对比", "分析结果导出"),
        "任务与同步": ("后台任务进度", "失败任务重试", "本地与中央数据状态"),
    }

    def __init__(self, title: str, description: str) -> None:
        super().__init__()
        layout = page_layout(self)
        layout.addWidget(PageHeading(title, description))
        panel = GlassCard(object_name="contextCard")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(28, 28, 28, 28)
        message = QLabel("该模块已预留统一入口，当前版本暂不开放操作。", objectName="mutedText")
        message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        planned = self.CAPABILITIES.get(title, (description,))
        capabilities = QLabel(
            "计划能力\n" + "\n".join(f"• {item}" for item in planned)
        )
        capabilities.setAlignment(Qt.AlignmentFlag.AlignCenter)
        panel_layout.addStretch()
        panel_layout.addWidget(message)
        panel_layout.addWidget(capabilities)
        panel_layout.addStretch()
        layout.addWidget(panel, 1)
