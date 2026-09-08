"""使用 QPainter 绘制导航栏矢量图标。"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

# 浅色侧栏下的图标配色：常态为蓝灰，当前页为品牌蓝。
DEFAULT_ICON_COLOR = "#5d7083"
ACTIVE_ICON_COLOR = "#0f6cbd"


def make_nav_icon(name: str, color: str = DEFAULT_ICON_COLOR) -> QIcon:
    """生成适合浅色侧栏的线性图标。"""

    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), 3.2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if name == "dashboard":
        for rect in (
            QRectF(8, 8, 13, 13),
            QRectF(27, 8, 13, 13),
            QRectF(8, 27, 13, 13),
            QRectF(27, 27, 13, 13),
        ):
            painter.drawRoundedRect(rect, 2, 2)
    elif name == "sample":
        path = QPainterPath(QPointF(19, 8))
        path.lineTo(29, 8)
        path.moveTo(21, 8)
        path.lineTo(21, 20)
        path.lineTo(11, 37)
        path.quadTo(10, 40, 15, 40)
        path.lineTo(33, 40)
        path.quadTo(38, 40, 37, 37)
        path.lineTo(27, 20)
        path.lineTo(27, 8)
        painter.drawPath(path)
        painter.drawLine(16, 31, 32, 31)
    elif name == "scan":
        painter.drawLine(7, 37, 41, 37)
        path = QPainterPath(QPointF(7, 32))
        path.lineTo(14, 31)
        path.lineTo(19, 25)
        path.lineTo(24, 9)
        path.lineTo(29, 29)
        path.lineTo(34, 21)
        path.lineTo(41, 30)
        painter.drawPath(path)
    elif name == "tuning":
        for y, knob in ((12, 18), (24, 31), (36, 14)):
            painter.drawLine(8, y, 40, y)
            painter.drawEllipse(QPointF(knob, y), 4, 4)
    elif name == "database":
        painter.drawEllipse(QRectF(9, 8, 30, 10))
        painter.drawArc(QRectF(9, 15, 30, 10), 180 * 16, 180 * 16)
        painter.drawArc(QRectF(9, 25, 30, 10), 180 * 16, 180 * 16)
        painter.drawArc(QRectF(9, 31, 30, 10), 180 * 16, 180 * 16)
        painter.drawLine(9, 13, 9, 36)
        painter.drawLine(39, 13, 39, 36)
    elif name == "analysis":
        painter.drawLine(9, 8, 9, 39)
        painter.drawLine(9, 39, 41, 39)
        path = QPainterPath(QPointF(12, 32))
        path.lineTo(19, 24)
        path.lineTo(26, 28)
        path.lineTo(38, 12)
        painter.drawPath(path)
    elif name == "sync":
        painter.drawArc(QRectF(9, 9, 30, 30), 35 * 16, 135 * 16)
        painter.drawArc(QRectF(9, 9, 30, 30), 215 * 16, 135 * 16)
        painter.drawLine(34, 9, 40, 11)
        painter.drawLine(39, 11, 37, 17)
        painter.drawLine(14, 39, 8, 37)
        painter.drawLine(9, 37, 11, 31)
    elif name == "settings":
        painter.drawEllipse(QRectF(17, 17, 14, 14))
        painter.drawEllipse(QRectF(21, 21, 6, 6))
        for index in range(8):
            angle = index * math.pi / 4
            painter.drawLine(
                QPointF(24 + math.cos(angle) * 10, 24 + math.sin(angle) * 10),
                QPointF(24 + math.cos(angle) * 17, 24 + math.sin(angle) * 17),
            )

    painter.end()
    return QIcon(pixmap)
