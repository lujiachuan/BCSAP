"""使用 QPainter 绘制导航栏矢量图标与侧栏符号按钮图标。

M1 起按窗口 devicePixelRatio 创建物理像素画布，避免高 DPI 下发虚；
☾/☀/☰ 等文本字形按钮改由矢量绘制（不依赖字体码位）。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPainterPath, QPen, QPixmap

# 兜底图标配色（运行时由主题令牌重绘，见 main._refresh_nav_icons）。
DEFAULT_ICON_COLOR = "#5d7083"
ACTIVE_ICON_COLOR = "#0f6cbd"

_LOGICAL_CANVAS = 48


def _device_ratio() -> float:
    try:
        app = QGuiApplication.instance()
        if app is not None and app.primaryScreen() is not None:
            return float(app.primaryScreen().devicePixelRatio())
    except Exception:  # noqa: BLE001
        pass
    return 1.0


def _begin_painter(color: str, content_scale: float = 1.0) -> tuple[QPixmap, QPainter]:
    ratio = max(1.0, _device_ratio())
    pixmap = QPixmap(int(_LOGICAL_CANVAS * ratio), int(_LOGICAL_CANVAS * ratio))
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(ratio)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    # QPixmap 设置 DPR 后，QPainter 已以逻辑像素为坐标系；再次按 DPR
    # scale 会造成二次缩放，图形被放大并裁切。这里只微调内容占比。
    if content_scale != 1.0:
        painter.translate(_LOGICAL_CANVAS / 2, _LOGICAL_CANVAS / 2)
        painter.scale(content_scale, content_scale)
        painter.translate(-_LOGICAL_CANVAS / 2, -_LOGICAL_CANVAS / 2)
    pen = QPen(QColor(color), 2.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    return pixmap, painter


def _finish_painter(painter: QPainter, pixmap: QPixmap) -> QIcon:
    painter.end()
    return QIcon(pixmap)


def make_nav_icon(name: str, color: str = DEFAULT_ICON_COLOR) -> QIcon:
    """生成导航线性图标（DPR 感知）。"""

    # QIcon 会按目标槽位生成 pixmap；保留约 20% 安全边距，避免线帽贴边。
    pixmap, painter = _begin_painter(color, 0.96)

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

    return _finish_painter(painter, pixmap)


def make_symbol(name: str, color: str = DEFAULT_ICON_COLOR) -> QIcon:
    """生成侧栏符号按钮（sun / moon / menu）矢量图标。"""
    # 菜单横线天然较宽，太阳/月亮更接近方形，分别校准视觉占比。
    symbol_scale = {"menu": 0.90, "sun": 0.95, "moon": 0.98}.get(name, 0.95)
    pixmap, painter = _begin_painter(color, symbol_scale)

    if name == "menu":
        painter.drawLine(9, 17, 39, 17)
        painter.drawLine(9, 24, 39, 24)
        painter.drawLine(9, 31, 39, 31)
    elif name == "sun":
        painter.drawEllipse(QPointF(24, 24), 7, 7)
        for index in range(8):
            angle = index * math.pi / 4
            painter.drawLine(
                QPointF(24 + math.cos(angle) * 12, 24 + math.sin(angle) * 12),
                QPointF(24 + math.cos(angle) * 17, 24 + math.sin(angle) * 17),
            )
    elif name == "moon":
        # 实心圆 + 位移圆做 Clear 打洞 → 月牙
        painter.setBrush(QColor(color))
        painter.drawEllipse(QRectF(9, 9, 30, 30))
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_Clear
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(17, 6, 30, 30))
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceOver
        )

    return _finish_painter(painter, pixmap)
