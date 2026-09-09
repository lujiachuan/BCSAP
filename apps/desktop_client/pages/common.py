"""各业务页面共用的布局小助手（不含具体页面）。"""

from __future__ import annotations

from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget


def page_layout(page: QWidget) -> QVBoxLayout:
    """创建统一的页面边距。"""

    page.setObjectName("pageRoot")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(18, 16, 18, 18)
    layout.setSpacing(12)
    return layout


def primary_button(text: str) -> QPushButton:
    button = QPushButton(text, objectName="primaryButton")
    return button
