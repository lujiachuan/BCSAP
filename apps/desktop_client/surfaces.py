"""L0 / L2 表面原语：环境光画布与玻璃卡片。

- ``AmbientCanvas``：页面级 L0 基底，自绘垂直渐变环境光 + 两团低饱和弥散光晕。
  内容区透明后由它统一提供氛围，避免每个页面重复铺设纯色画布。
- ``GlassCard``：L2 半透明玻璃面（强调卡 / 状态卡 / 指标卡 / Hero）。
  绘制顺序：柔和外圈光晕 → 半透明填充 → 顶部高光带 → 1px 描边。
  不使用 QGraphicsDropShadowEffect（避免大面积实时特效），阴影/光晕均为静态绘制。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import QFrame, QWidget

from apps.desktop_client.theme import current_palette
from apps.desktop_client.ui_tokens import METERS


def token_color(value: str | int | float) -> QColor:
    """把令牌值解析为 QColor：支持 '#rrggbb' 与 'rgba(r,g,b,a)'。"""
    if isinstance(value, (int, float)):
        return QColor(int(value))
    text = str(value).strip()
    if text.startswith("rgba"):
        inner = text[text.index("(") + 1 : text.rindex(")")]
        parts = [float(part.strip()) for part in inner.split(",")]
        r, g, b = (int(parts[index]) for index in range(3))
        alpha = parts[3] if len(parts) > 3 else 1.0
        color = QColor(r, g, b)
        color.setAlphaF(alpha)
        return color
    return QColor(text)


def _rounded_rect(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


class AmbientCanvas(QWidget):
    """L0 环境光画布：可作为主窗口/内容舞台的底层。"""

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        tokens = current_palette()

        base = QLinearGradient(0, 0, rect.width(), rect.height())
        base.setColorAt(0.0, QColor(tokens["ambientA"]))
        base.setColorAt(0.55, QColor(tokens["ambientB"]))
        base.setColorAt(1.0, QColor(tokens["ambientC"]))
        painter.fillRect(rect, base)

        self._paint_glow(
            painter,
            rect.width() * 0.88,
            rect.height() * 0.08,
            max(rect.width(), rect.height()) * 0.62,
            QColor(tokens["ambientA"]),
            0.55,
        )
        glow_color = QColor(tokens["accent"])
        glow_color.setAlphaF(0.10)
        self._paint_glow(
            painter,
            rect.width() * 0.10,
            rect.height() * 1.02,
            max(rect.width(), rect.height()) * 0.55,
            glow_color,
            0.9,
        )
        painter.end()

    @staticmethod
    def _paint_glow(
        painter: QPainter, cx: float, cy: float, radius: float, color: QColor, peak: float
    ) -> None:
        glow = QRadialGradient(cx, cy, radius)
        glow.setColorAt(0.0, color)
        glow.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
        painter.fillRect(
            QRectF(cx - radius, cy - radius, radius * 2, radius * 2),
            glow,
        )


class GlassCard(QFrame):
    """L2 半透明玻璃卡片（自绘，不依赖 QSS 背景）。"""

    def __init__(self, object_name: str = "glassCard", radius: str = "card") -> None:
        super().__init__()
        self.setObjectName(object_name)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self._radius_token = radius

    @property
    def _radius(self) -> float:
        return float(METERS["radius"].get(self._radius_token, METERS["radius"]["card"]))

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        tokens = current_palette()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = self._radius
        path = _rounded_rect(rect, radius)

        # 1) 柔和外圈光晕（三层描边近似阴影）
        ring = token_color(tokens["glassRing"])
        for width, alpha_factor in ((9, 0.22), (5, 0.38), (2, 0.62)):
            color = QColor(ring)
            color.setAlphaF(color.alphaF() * alpha_factor)
            painter.setPen(QPen(color, width))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

        # 2) 半透明填充
        fill = token_color(tokens["glassTint"])
        hover = self.underMouse()
        fill.setAlphaF(tokens["glassAlphaHover"] if hover else tokens["glassAlpha"])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawPath(path)

        # 3) 顶部高光带（玻璃质感）
        painter.save()
        painter.setClipPath(path)
        sheen = token_color(tokens["glassHighlight"])
        sheen.setAlphaF(sheen.alphaF() * (0.55 if hover else 0.40))
        top = QLinearGradient(0, rect.top(), 0, rect.top() + rect.height() * 0.42)
        top.setColorAt(0.0, sheen)
        top.setColorAt(1.0, QColor(sheen.red(), sheen.green(), sheen.blue(), 0))
        painter.fillRect(rect, top)
        painter.restore()

        # 4) 描边
        painter.setPen(QPen(token_color(tokens["glassStroke"]), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.end()
