"""论文式绘图样式与矢量导出（结果分析 / 运行监控共用）。

为什么需要这一层
----------------
调束页的图原本各写各的：爬山图是一条 2px 蓝线加 18% 透明网格，重要性的柱子固定
浅蓝色，响应面每格一套独立色标——单个看都能读，但拼在一起不像同一套工具出的图，
也没法直接放进实验记录或报告。

这一层把"科研风"落成可复用的规则，而不是逐个图去调样式：

* 坐标轴：四边框 + 刻度线**向内**、次刻度、刻度字号统一、轴标签带单位；
* 网格：主轴淡（α≈0.12），另有零线/基线加粗一档；
* 图例：无边框、半透明底、按内容自适应位置；
* 配色：全部取主题令牌（深浅主题自动跟随），响应面用 ``viridis``（预测均值）
  与 ``plasma``（不确定性）；
* 标注：最优点、阈值线、阶段分界、数据来源；
* 导出：PNG（高清位图）/ SVG / PDF（矢量，可直接进报告）。

对外只暴露三个入口：:func:`apply_science_style`、:func:`series_style`、
:func:`export_plot`。页面负责画什么，这里负责"看起来像同一套工具出的图"。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtSvg import QSvgGenerator

from apps.desktop_client.theme import current_palette

# 刻度字号/长度：比 pyqtgraph 默认大一点，投屏与截图里都读得清
TICK_FONT_POINT_SIZE = 9
TICK_LENGTH = -6  # 负值 = 刻度线向内
GRID_ALPHA = 0.12
AXIS_LABEL_FONT_POINT_SIZE = 10


def tick_font() -> QFont:
    font = QFont("Microsoft YaHei UI")
    font.setPointSize(TICK_FONT_POINT_SIZE)
    return font


def apply_science_style(
    plot: pg.PlotItem | pg.PlotWidget,
    *,
    x_label: str | None = None,
    y_label: str | None = None,
    mirrored_axes: bool = True,
    grid: bool = True,
) -> pg.PlotItem:
    """把一张 pyqtgraph 图调成论文式外观；返回底层 PlotItem。

    ``mirrored_axes=True`` 时打开上/右边框刻度（不显示数值），形成"四边封闭"的
    坐标框——这是期刊图最常见的形态，也让读者不必依赖网格线对齐。
    """
    item = plot.getPlotItem() if isinstance(plot, pg.PlotWidget) else plot
    tokens = current_palette()

    item.showGrid(x=grid, y=grid, alpha=GRID_ALPHA)
    item.setContentsMargins(6, 6, 6, 6)
    axis_pen = pg.mkPen(tokens["plotAxis"], width=1)
    for name in ("bottom", "left"):
        axis = item.getAxis(name)
        axis.setPen(axis_pen)
        axis.setTextPen(pg.mkPen(tokens["plotAxis"]))
        axis.setStyle(tickFont=tick_font(), tickLength=TICK_LENGTH, tickTextOffset=4)
    if mirrored_axes:
        for name in ("top", "right"):
            item.showAxis(name)
            axis = item.getAxis(name)
            axis.setPen(axis_pen)
            axis.setStyle(showValues=False, tickFont=tick_font(), tickLength=TICK_LENGTH)
            axis.linkToView(item.getViewBox())
    if x_label is not None:
        item.setLabel("bottom", x_label)
    if y_label is not None:
        item.setLabel("left", y_label)
    for name in ("bottom", "left", "top", "right"):
        label = item.getAxis(name).label
        if label is not None and label.isVisible():
            font = label.font()
            font.setPointSize(AXIS_LABEL_FONT_POINT_SIZE)
            label.setFont(font)
    item.setMouseEnabled(x=True, y=True)
    item.setMenuEnabled(False)
    return item


def style_legend(plot: pg.PlotItem | pg.PlotWidget) -> None:
    """图例：无边框 + 半透明底，避免遮住数据又保证可读。"""
    item = plot.getPlotItem() if isinstance(plot, pg.PlotWidget) else plot
    tokens = current_palette()
    legend = item.legend
    if legend is None:
        legend = item.addLegend(offset=(-12, 12))
    legend.setBrush(pg.mkBrush(QColor(tokens["surfacePanel"]).lighter(104)))
    legend.setPen(pg.mkPen(tokens["panelBorder"]))
    for _sample, label in legend.items:
        if label is not None:
            label.setText(label.text, color=tokens["textBase"])


def series_style() -> dict[str, dict]:
    """各类曲线的统一画笔/符号（颜色取主题令牌，深浅主题自动跟随）。

    角色划分对应调束图的语义，而不是"第几条线"：

    * ``current``：本轮实测点（散点 + 细线，带误差棒）；
    * ``best``：历史最优（阶梯线，视觉上只升不降）；
    * ``baseline``：启动前基线（点划线，作为"提升了多少"的参照）；
    * ``target``：追踪目标值（虚线）；
    * ``model``：模型均值/拟合（实线）；
    * ``band``：不确定性带（半透明填充）。
    """
    tokens = current_palette()
    return {
        "current": {
            "pen": pg.mkPen(tokens["plotLine"], width=1.4),
            "brush": pg.mkBrush(tokens["plotLine"]),
            "symbol_size": 6,
        },
        "best": {
            "pen": pg.mkPen(tokens["plotBest"], width=2.2),
        },
        "baseline": {
            "pen": pg.mkPen(tokens["statusIdle"], width=1.3, style=Qt.PenStyle.DashDotLine),
        },
        "target": {
            "pen": pg.mkPen(tokens["statusWarn"], width=1.3, style=Qt.PenStyle.DashLine),
        },
        "model": {
            "pen": pg.mkPen(tokens["plotLineAlt"], width=1.8),
        },
        "band": {
            "brush": pg.mkBrush(QColor(tokens["plotLineAlt"]).lighter(120).setAlpha(60)),
        },
    }


def error_bar_item(
    xs: Sequence[float],
    ys: Sequence[float],
    errors: Sequence[float],
    *,
    color: str | None = None,
    width: int = 5,
) -> pg.ErrorBarItem:
    """误差棒：多次采样测量的离散度必须画出来，只给中位数是不诚实的。"""
    import numpy as np

    tokens = current_palette()
    pen = pg.mkPen(color or tokens["plotLine"], width=1.2)
    return pg.ErrorBarItem(
        x=np.asarray(xs, dtype=float),
        y=np.asarray(ys, dtype=float),
        top=np.asarray(errors, dtype=float),
        bottom=np.asarray(errors, dtype=float),
        beam=width,
        pen=pen,
        alpha=0.9,
    )


def reference_line(
    value: float,
    *,
    role: str = "baseline",
    label: str = "",
    angle: float = 0.0,
) -> pg.InfiniteLine:
    """基线/目标线：语义色 + 可选标签（标签贴在图的左上角，不压数据）。"""
    tokens = current_palette()
    style = series_style()[role]
    line = pg.InfiniteLine(
        pos=float(value),
        angle=angle,
        movable=False,
        pen=style["pen"],
        label=label or None,
        labelOpts={
            "position": 0.02,
            "color": tokens["textBase"],
            "fill": QColor(tokens["surfacePanel"]).lighter(104),
            "movable": False,
        }
        if label
        else None,
    )
    return line


def best_point_marker(
    x: float,
    y: float,
    text: str = "",
    *,
    color: str | None = None,
) -> tuple[pg.ScatterPlotItem, pg.TextItem | None]:
    """最优点：实心点 + 无边框文字标注。"""
    tokens = current_palette()
    scatter = pg.ScatterPlotItem(
        x=[float(x)],
        y=[float(y)],
        size=11,
        symbol="o",
        brush=pg.mkBrush(color or tokens["plotBest"]),
        pen=pg.mkPen(tokens["surfacePanel"], width=1.4),
    )
    if not text:
        return scatter, None
    label = pg.TextItem(text, color=tokens["textBase"], anchor=(0.5, 1.6), border=None)
    label.setPos(float(x), float(y))
    return scatter, label


def source_note(
    plot: pg.PlotItem | pg.PlotWidget,
    text: str,
) -> None:
    """在图的右下角标注数据来源（study / 算法 / 种子 / 有效轮次）。

    评审时最常见的追问是"这张图是哪一次、哪个算法、多少个有效点画的"，
    把它写在图上比写在旁边的说明文字更可靠。
    """
    item = plot.getPlotItem() if isinstance(plot, pg.PlotWidget) else plot
    tokens = current_palette()
    note = pg.TextItem(text, color=tokens["muted"], anchor=(1.0, 1.0))
    note.setPos(1.0, 0.0)
    note.setParentItem(item.getViewBox())
    note.setZValue(-10)


# ---------------------------------------------------------------------------
# 导出：位图给截图/汇报，矢量给论文与实验记录
# ---------------------------------------------------------------------------
def export_plot(
    target: object,
    path: str | Path,
    *,
    scale: int = 3,
    background: str | None = None,
) -> Path:
    """把一张图（PlotWidget / PlotItem / 任意 QWidget）导出为 PNG / SVG / PDF。

    * PNG：按 ``scale`` 倍超采样重绘，等效高 DPI 位图；
    * SVG / PDF：走矢量绘制，放大不失真，可直接进报告。

    ``target`` 是普通 QWidget（例如响应面矩阵那种多图容器）时按控件重绘，
    是图形场景里的图时按场景重绘——两条路径都保持"所见即所得"。
    """
    out = Path(path)
    suffix = out.suffix.lower()
    if suffix not in {".png", ".svg", ".pdf"}:
        raise ValueError(f"不支持的导出格式：{out.suffix}（支持 .png/.svg/.pdf）")

    scene = None
    widget = None
    if isinstance(target, pg.PlotWidget):
        scene = target.scene()
        widget = target
    elif isinstance(target, pg.PlotItem):
        scene = target.scene()
    elif hasattr(target, "scene") and hasattr(target, "width"):
        scene = target.scene()
        widget = target
    else:
        widget = target  # 普通 QWidget：走控件重绘
    if not hasattr(widget, "width") and scene is not None:
        widget = scene.views()[0] if scene.views() else None

    tokens = current_palette()
    fill = QColor(background or tokens["surfacePanel"])
    size = _source_rect(scene, widget)

    if suffix == ".png":
        if widget is None and scene is None:
            raise ValueError("PNG 导出需要一个可见的控件或图形视图")
        factor = max(1, int(scale))
        image = QImage(
            int(size.width()) * factor,
            int(size.height()) * factor,
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        image.fill(fill)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(factor, factor)
        _render(scene, widget, painter, fill)
        painter.end()
        if not image.save(str(out)):
            raise OSError(f"PNG 写入失败：{out}")
        return out

    if suffix == ".svg":
        from PySide6.QtCore import QSize

        generator = QSvgGenerator()
        generator.setFileName(str(out))
        generator.setSize(QSize(int(size.width()), int(size.height())))
        generator.setViewBox(QRectF(0, 0, size.width(), size.height()))
        painter = QPainter(generator)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if scene is not None:
            painter.translate(-size.left(), -size.top())
        _render(scene, widget, painter, fill)
        painter.end()
        return out

    # PDF：A4 横向、按比例居中铺满可打印区（矢量）
    from PySide6.QtGui import QPageSize, QPdfWriter

    writer = QPdfWriter(str(out))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageOrientation(_landscape())
    writer.setResolution(300)
    painter = QPainter(writer)
    if not painter.isActive():
        raise OSError(f"PDF 写入失败：{out}")
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    page = writer.pageLayout().paintRectPixels(writer.resolution())
    factor = min(
        page.width() / max(1.0, size.width()), page.height() / max(1.0, size.height())
    )
    painter.translate(
        page.x() + (page.width() - size.width() * factor) / 2.0,
        page.y() + (page.height() - size.height() * factor) / 2.0,
    )
    painter.scale(factor, factor)
    painter.translate(-size.left(), -size.top())
    _render(scene, widget, painter, fill)
    painter.end()
    return out


def _landscape():
    from PySide6.QtGui import QPageLayout

    return QPageLayout.Orientation.Landscape


def _source_rect(scene, widget) -> QRectF:
    if scene is not None:
        rect = scene.sceneRect()
        if not rect.isEmpty():
            return QRectF(rect)
    if widget is not None:
        return QRectF(0.0, 0.0, float(widget.width()), float(widget.height()))
    raise ValueError("无法导出的目标：既不是控件也不在图形场景里")


def _render(scene, widget, painter: QPainter, fill: QColor) -> None:
    """把场景或控件画到 painter 上（填充背景，保持所见即所得）。"""
    if scene is not None:
        rect = scene.sceneRect()
        painter.fillRect(rect, fill)
        scene.render(painter, QRectF(rect), rect)
        return
    if widget is not None:
        painter.fillRect(QRectF(0, 0, widget.width(), widget.height()), fill)
        widget.render(painter)
        return
    raise ValueError("无法导出的目标：既不是控件也不在图形场景里")


__all__ = [
    "apply_science_style",
    "best_point_marker",
    "error_bar_item",
    "export_plot",
    "reference_line",
    "series_style",
    "source_note",
    "style_legend",
    "tick_font",
]
