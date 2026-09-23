"""调束页（及后续同类密集表单）共用的紧凑布局组件。

为什么单独抽一层
----------------
v1 的参数配置页右栏是"一行一个字段"的竖排表单：20 个字段各占约 56px，
在 1320×820 下需要 810px，而可用视口只有 375px——不滚轮根本看不到开始按钮。
这里提供的三件套把同样的信息压进一屏：

* :class:`CompactForm`：两列表单网格（一行两组「标签 + 字段」），字段走主题里
  已有的 ``#rowInput`` 22px 紧凑样式，行高从 56px 降到 28px；
* :class:`FoldSection`：可折叠分区卡，折叠时留一行标题 + 摘要
  （例如"相对损失 50% · 连续 2 次触发"），需要时再展开；
* :class:`StatusStrip`：常驻页脚状态带（运行计划 / 启动检查 / 主操作），
  不随表单滚动，保证"开始"按钮永远在页内。

另有 :class:`RangeBar`：把"设备边界 ┃ 设定区间 ┃ 起始值 ┃ 当前回读"画成一条
横向标尺，用来填掉参数配置页左栏大片空白，并让"范围是否贴边/是否越界"一眼可见。

组件只使用主题令牌与既有 objectName 约定，不改变其它页面的观感。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from apps.desktop_client.theme import current_palette

# 主题里紧凑输入样式的 objectName（min-height/max-height 22px）
COMPACT_INPUT_NAME = "rowInput"


def mark_compact(widget: QWidget) -> QWidget:
    """给输入类控件套上紧凑样式（22px 行高）。

    字段较多时逐个 ``setObjectName("rowInput")`` 容易漏，统一走这个函数；
    返回值就是传入的控件，便于链式写法。
    """
    widget.setObjectName(COMPACT_INPUT_NAME)
    if widget.minimumHeight() > 22:
        widget.setMinimumHeight(22)
    widget.setMaximumHeight(22)
    return widget


def muted_label(text: str = "", *, wrap: bool = False) -> QLabel:
    """统一的弱化说明文字。"""
    label = QLabel(text, objectName="mutedText")
    label.setWordWrap(wrap)
    return label


class CompactForm(QWidget):
    """两列表单网格：一行两组「标签 + 字段」。

    ``span=True`` 的条目独占一整行（例如"束流丢失保护"这类横向组合控件）。
    字段是否紧凑由调用方决定（一般用 :func:`mark_compact`）；组件不擅自改控件样式，
    否则复用到别处时会与页面既有的紧凑规则打架。
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        label_width: int = 0,
        vertical_spacing: int = 6,
    ) -> None:
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(vertical_spacing)
        self._grid.setColumnStretch(1, 1)
        self._grid.setColumnStretch(3, 1)
        self._slot = 0
        self._row = 0
        self._label_width = label_width
        # key → (标签控件, 字段控件)：调用方按 key 收起/展开或改提示
        self.fields: dict[str, QWidget] = {}
        self.labels: dict[str, QLabel] = {}
        self._pairs: list[tuple[QLabel, QWidget]] = []

    # ---------------------------------------------------------------- 添加
    def add(
        self,
        label: str,
        field: QWidget,
        *,
        key: str = "",
        tooltip: str = "",
        span: bool = False,
    ) -> QWidget:
        """加一组「标签 + 字段」；``span=True`` 时独占一行。"""
        text_label = QLabel(label, objectName="mutedText")
        if self._label_width:
            text_label.setMinimumWidth(self._label_width)
        if tooltip:
            text_label.setToolTip(tooltip)
            field.setToolTip(tooltip)
        if span:
            if self._slot:  # 上一行只填了一组：先收尾
                self._slot = 0
                self._row += 1
            self._grid.addWidget(text_label, self._row, 0)
            self._grid.addWidget(field, self._row, 1, 1, 3)
            self._row += 1
        else:
            column = 0 if self._slot == 0 else 2
            self._grid.addWidget(text_label, self._row, column)
            self._grid.addWidget(field, self._row, column + 1)
            if self._slot == 1:
                self._row += 1
                self._slot = 0
            else:
                self._slot = 1
        self._pairs.append((text_label, field))
        if key:
            self.fields[key] = field
            self.labels[key] = text_label
        return field

    def add_full(self, widget: QWidget) -> QWidget:
        """加一个横跨整行的控件（复选框、按钮组、说明文字）。"""
        if self._slot:
            self._slot = 0
            self._row += 1
        self._grid.addWidget(widget, self._row, 0, 1, 4)
        self._row += 1
        return widget

    def add_stretch_row(self) -> None:
        """收尾：把当前这一行没填满的一组让开，后续内容另起一行。"""
        if self._slot:
            self._slot = 0
            self._row += 1

    def row_count(self) -> int:
        return self._row


class FoldSection(QFrame):
    """可折叠分区卡：标题行（含摘要）+ 内容区。

    折叠时只保留标题与摘要。折叠不是"隐藏信息"：摘要会把最该看到的一条结论写出来
    （例如束流保护的当前阈值），所以收起状态下操作员仍然知道自己在什么配置下启动。
    """

    toggled = Signal(bool)

    def __init__(
        self,
        title: str,
        *,
        summary: str = "",
        expanded: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, objectName="foldSection")
        outer = QVBoxLayout(self)
        # 上下留白各收 1~2px：调束页右栏的高度是硬约束（内容装不下时 Qt 会把控件
        # 压重叠而不是滚动），三张卡各让出几个像素，正好补平缺口。
        outer.setContentsMargins(10, 5, 10, 6)
        outer.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self.toggle_button = QPushButton(objectName="foldHeader")
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(expanded)
        self.toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_button.toggled.connect(self._on_toggled)
        header.addWidget(self.toggle_button)
        self.summary_label = QLabel(summary, objectName="foldSummary")
        self.summary_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(self.summary_label, 1)
        outer.addLayout(header)

        self._body_host = QWidget()
        self.body = QVBoxLayout(self._body_host)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(6)
        self._body_host.setVisible(expanded)
        outer.addWidget(self._body_host)

        self._title = title
        self._apply_title()

    # ---------------------------------------------------------------- 状态
    def _on_toggled(self, checked: bool) -> None:
        self._body_host.setVisible(bool(checked))
        self._apply_title()
        self.toggled.emit(bool(checked))

    def _apply_title(self) -> None:
        arrow = "▾" if self.toggle_button.isChecked() else "▸"
        self.toggle_button.setText(f"{arrow} {self._title}")

    def is_expanded(self) -> bool:
        return bool(self.toggle_button.isChecked())

    def set_expanded(self, expanded: bool) -> None:
        self.toggle_button.setChecked(bool(expanded))

    def set_summary(self, text: str) -> None:
        self.summary_label.setText(text)
        self.summary_label.setVisible(bool(text))

    def add_widget(self, widget: QWidget) -> QWidget:
        self.body.addWidget(widget)
        return widget

    def add_layout(self, layout) -> None:
        self.body.addLayout(layout)


class StatusStrip(QFrame):
    """常驻页脚状态带：左侧运行计划，中间启动检查，右侧主操作。

    与表单解耦的固定区：表单区（=字段多的时候唯一允许滚动的地方）再高，
    这里也始终留在页内——"开始"按钮不该因为展开高级参数而消失。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, objectName="statusStrip")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(10)
        self.note_area = QVBoxLayout()
        self.note_area.setContentsMargins(0, 0, 0, 0)
        self.note_area.setSpacing(2)
        layout.addLayout(self.note_area, 1)
        self._actions = QHBoxLayout()
        self._actions.setContentsMargins(0, 0, 0, 0)
        self._actions.setSpacing(6)
        layout.addLayout(self._actions)

    def add_note(self, widget: QWidget) -> QWidget:
        """加一行说明（运行计划 / 启动检查 / 配置提示）。"""
        self.note_area.addWidget(widget)
        return widget

    def add_action(self, widget: QWidget) -> QWidget:
        self._actions.addWidget(widget)
        return widget


class RangeBar(QWidget):
    """变量范围条：设备边界 ┃ 设定区间 ┃ 起始值 ┃ 当前回读。

    参数配置页左栏原本有大片空白：表格只有 2 行、区高 563px。把这条标尺放进表格，
    既填掉了空白，也让"范围是否贴边、当前值是否在范围内"一眼可见——贴边是调束里
    最常见的误配（真正的最优可能在范围之外）。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(18)
        self.setMinimumWidth(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._low: float | None = None
        self._high: float | None = None
        self._start: float | None = None
        self._readback: float | None = None
        self._device_low: float | None = None
        self._device_high: float | None = None
        self._unit = ""

    def set_values(
        self,
        *,
        low: float | None,
        high: float | None,
        start: float | None = None,
        readback: float | None = None,
        device_low: float | None = None,
        device_high: float | None = None,
        unit: str = "",
    ) -> None:
        self._low = low
        self._high = high
        self._start = start
        self._readback = readback
        self._device_low = device_low
        self._device_high = device_high
        self._unit = unit
        self._refresh_tooltip()
        self.update()

    def _refresh_tooltip(self) -> None:
        def fmt(value: float | None) -> str:
            return "--" if value is None else f"{value:g}"

        parts = [f"设定区间 [{fmt(self._low)}, {fmt(self._high)}] {self._unit}".strip()]
        if self._device_low is not None or self._device_high is not None:
            parts.append(f"设备边界 [{fmt(self._device_low)}, {fmt(self._device_high)}]")
        parts.append(f"起始值 {fmt(self._start)}")
        parts.append(f"当前回读 {fmt(self._readback)}")
        self.setToolTip("；".join(parts))

    # ---------------------------------------------------------------- 绘制
    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        palette = current_palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        height = self.height()
        mid = height / 2.0
        track = QRectF(1.0, mid - 3.0, max(1.0, self.width() - 2.0), 6.0)

        lo = self._low
        hi = self._high
        if lo is None or hi is None or hi <= lo:
            painter.setPen(QPen(QColor(palette["plotAxis"]), 1))
            painter.drawLine(QPointF(2.0, mid), QPointF(self.width() - 2.0, mid))
            painter.end()
            return

        device_low = self._device_low if self._device_low is not None else lo
        device_high = self._device_high if self._device_high is not None else hi
        span = max(device_high - device_low, hi - lo, 1e-9)
        device_low = min(device_low, lo)
        device_high = max(device_high, hi)
        span = max(device_high - device_low, 1e-9)

        def pos(value: float) -> float:
            ratio = (value - device_low) / span
            ratio = min(max(ratio, 0.0), 1.0)
            return track.left() + ratio * track.width()

        # 设备边界（整条）→ 设定区间（强调带）→ 起始值（竖线）→ 当前回读（圆点）
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(palette["statusIdleBg"])))
        painter.drawRoundedRect(track, 3.0, 3.0)
        band = QRectF(pos(lo), track.top(), max(2.0, pos(hi) - pos(lo)), track.height())
        painter.setBrush(QBrush(QColor(palette["accentGlow"])))
        painter.drawRoundedRect(band, 3.0, 3.0)

        painter.setPen(QPen(QColor(palette["plotAxis"]), 1, Qt.PenStyle.DashLine))
        start_x = pos(self._start) if self._start is not None else None
        if start_x is not None:
            painter.drawLine(QPointF(start_x, mid - 7.0), QPointF(start_x, mid + 7.0))

        if self._readback is not None:
            inside = lo <= self._readback <= hi
            color = QColor(palette["statusGood"] if inside else palette["statusWarn"])
            painter.setPen(QPen(color, 1.4))
            painter.setBrush(QBrush(color))
            painter.drawEllipse(QPointF(pos(self._readback), mid), 3.2, 3.2)
        painter.end()


__all__ = [
    "COMPACT_INPUT_NAME",
    "CompactForm",
    "FoldSection",
    "RangeBar",
    "StatusStrip",
    "mark_compact",
    "muted_label",
]
