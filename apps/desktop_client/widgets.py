"""客户端共用的小型界面组件。"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QPointF, QRectF, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from apps.desktop_client.surfaces import GlassCard
from apps.desktop_client.theme import current_palette


class PageHeading(QWidget):
    """页面标题与说明。"""

    def __init__(self, title: str, description: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(QLabel(title, objectName="pageTitle"))
        description_label = QLabel(description, objectName="pageDescription")
        description_label.setWordWrap(True)
        layout.addWidget(description_label)


class ClickableLabel(QLabel):
    """支持双击交互的文本标签（用于状态卡查看明细）。"""

    doubleClicked = Signal()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        super().mouseDoubleClickEvent(event)
        self.doubleClicked.emit()


class Panel(QFrame):
    """L1 数据工作面面板：不透明，稳定承载图表 / 表格 / 表单。"""

    def __init__(self, title: str, subtitle: str = "") -> None:
        super().__init__(objectName="panel")
        self.body = QVBoxLayout()
        self.body.setContentsMargins(16, 8, 16, 16)
        self.body.setSpacing(10)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 14, 16, 6)
        header_layout.addWidget(QLabel(title, objectName="panelTitle"))
        header_layout.addStretch()
        if subtitle:
            header_layout.addWidget(QLabel(subtitle, objectName="mutedText"))
        outer.addWidget(header)
        body_widget = QWidget()
        body_widget.setLayout(self.body)
        outer.addWidget(body_widget, 1)


class SidebarStatusFooter(QWidget):
    """常驻侧边栏底部的服务状态区，切换页面时始终可见。

    services 是 (key, 名称, 状态文本, 语义状态) 元组序列，
    语义状态（good / warn / error / idle）对应主题调色板中的 status* 令牌。
    后续接入真实服务后可用 ``set_service`` 逐项刷新。
    """

    def __init__(
        self,
        services: Sequence[tuple[str, str, str, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("sidebarFooter")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 10)
        layout.setSpacing(5)
        self._dots: dict[str, QLabel] = {}
        self._values: dict[str, QLabel] = {}
        self._states: dict[str, str] = {}
        self._pulse_on = True
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(600)
        self._pulse_timer.timeout.connect(self._advance_pulse)
        for key, name, text, state in services:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(5)
            name_label = QLabel(name, objectName="footerName")
            dot_label = QLabel("●")
            dot_label.setFixedWidth(12)
            value_label = QLabel(text, objectName="footerValue")
            row_layout.addWidget(name_label)
            row_layout.addStretch()
            row_layout.addWidget(dot_label)
            row_layout.addWidget(value_label)
            layout.addWidget(row)
            self._dots[key] = dot_label
            self._values[key] = value_label
            self.set_service(key, state)

    def set_service(self, key: str, state: str, text: str | None = None) -> None:
        """更新某个服务的语义状态（改变圆点颜色）与可选的文本。"""
        self._states[key] = state
        dot = self._dots.get(key)
        if dot is not None:
            palette = current_palette()
            token = f"status{state.capitalize()}"
            color = palette.get(token, palette["statusIdle"])
            display_color = QColor(color)
            if state == "running" and not self._pulse_on:
                display_color.setAlphaF(0.58)
            css_color = display_color.name(QColor.NameFormat.HexArgb)
            dot.setStyleSheet(f"color: {css_color}; font-size: 11px;")
        if text is not None:
            value = self._values.get(key)
            if value is not None:
                value.setText(text)
        self._sync_pulse_timer()

    def refresh_theme(self) -> None:
        """主题切换后按新调色板重刷状态点颜色。"""
        for key, state in self._states.items():
            self.set_service(key, state)

    def refresh_motion(self) -> None:
        """外观偏好变化后立即启动或停止运行态脉冲。"""
        self._sync_pulse_timer()
        self.refresh_theme()

    def _sync_pulse_timer(self) -> None:
        reduce_motion = QSettings("SpectrumPlatform", "DesktopClient").value(
            "appearance/reduceMotion", False, type=bool
        )
        should_run = self.isVisible() and not reduce_motion and "running" in self._states.values()
        if should_run and not self._pulse_timer.isActive():
            self._pulse_timer.start()
        elif not should_run:
            self._pulse_timer.stop()
            self._pulse_on = True

    def _advance_pulse(self) -> None:
        self._pulse_on = not self._pulse_on
        for key, state in tuple(self._states.items()):
            if state == "running":
                self.set_service(key, state)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_pulse_timer()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._pulse_timer.stop()
        super().hideEvent(event)


class MetricCard(GlassCard):
    """L2 玻璃指标卡（工作台与结果页使用）。"""

    def __init__(self, label: str, value: str, detail: str = "", success: bool = False) -> None:
        super().__init__(object_name="metricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(5)
        layout.addWidget(QLabel(label, objectName="cardCaption"))
        value_name = "successValue" if success else "metricValue"
        self.value_label = QLabel(value, objectName=value_name)
        self.value_label.setMinimumWidth(
            QFontMetrics(self.value_label.font()).horizontalAdvance("−9999.99 μA")
        )
        layout.addWidget(self.value_label)
        self.detail_label: QLabel | None = None
        if detail:
            self.detail_label = QLabel(detail, objectName="mutedText")
            layout.addWidget(self.detail_label)


class LinePlot(QWidget):
    """不依赖额外绘图库的轻量曲线组件。"""

    def __init__(self, x_label: str, y_label: str) -> None:
        super().__init__()
        self.setMinimumHeight(280)
        self.x_label = x_label
        self.y_label = y_label
        self._x: list[float] = []
        self._y: list[float] = []

    def set_data(self, x: Sequence[float], y: Sequence[float]) -> None:
        self._x = list(x)
        self._y = list(y)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot = QRectF(56, 18, max(10, self.width() - 76), max(10, self.height() - 62))

        # 颜色取自当前主题调色板，主题切换后调用 update() 重绘即可
        palette = current_palette()
        grid_color = QColor(palette["plotGrid"])
        axis_color = QColor(palette["plotAxis"])
        line_color = QColor(palette["plotLine"])

        painter.setPen(QPen(grid_color, 1))
        for index in range(6):
            x = plot.left() + plot.width() * index / 5
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        for index in range(5):
            y = plot.top() + plot.height() * index / 4
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

        painter.setPen(axis_color)
        small_font = QFont(painter.font())
        small_font.setPointSize(8)
        painter.setFont(small_font)
        painter.drawText(
            QRectF(plot.left(), plot.bottom() + 17, plot.width(), 20),
            Qt.AlignmentFlag.AlignCenter,
            self.x_label,
        )
        painter.save()
        painter.translate(15, plot.center().y())
        painter.rotate(-90)
        painter.drawText(
            QRectF(-plot.height() / 2, -10, plot.height(), 20),
            Qt.AlignmentFlag.AlignCenter,
            self.y_label,
        )
        painter.restore()

        if len(self._x) < 2 or len(self._x) != len(self._y):
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "等待数据")
            return

        x_min, x_max = min(self._x), max(self._x)
        y_min, y_max = min(self._y), max(self._y)
        if x_min == x_max:
            x_max = x_min + 1
        if y_min == y_max:
            y_max = y_min + 1
        y_padding = (y_max - y_min) * 0.08
        y_min -= y_padding
        y_max += y_padding

        def point(index: int) -> QPointF:
            px = plot.left() + (self._x[index] - x_min) / (x_max - x_min) * plot.width()
            py = plot.bottom() - (self._y[index] - y_min) / (y_max - y_min) * plot.height()
            return QPointF(px, py)

        path = QPainterPath(point(0))
        for index in range(1, len(self._x)):
            path.lineTo(point(index))
        painter.setClipRect(plot)
        painter.setPen(QPen(line_color, 2.2))
        painter.drawPath(path)
        painter.setClipping(False)

        painter.setPen(axis_color)
        painter.drawText(QRectF(plot.left(), plot.bottom() + 1, 70, 16), f"{x_min:.2f}")
        painter.drawText(
            QRectF(plot.right() - 70, plot.bottom() + 1, 70, 16),
            Qt.AlignmentFlag.AlignRight,
            f"{x_max:.2f}",
        )
        painter.drawText(
            QRectF(2, plot.top() - 7, 48, 16), Qt.AlignmentFlag.AlignRight, f"{y_max:.1f}"
        )
        painter.drawText(
            QRectF(2, plot.bottom() - 8, 48, 16), Qt.AlignmentFlag.AlignRight, f"{y_min:.1f}"
        )
