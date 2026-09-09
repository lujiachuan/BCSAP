"""基于 pyqtgraph 的专业曲线组件。

组件保留原始数据，显示层启用可视区裁剪和峰值保持型降采样；十字光标
读数限制在约 30Hz，避免鼠标移动占用 UI 主线程。
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from apps.desktop_client.theme import current_palette


class SpectrumPlot(QWidget):
    """扫谱与调束共用的可交互曲线视图。"""

    def __init__(self, x_label: str, y_label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(280)
        self._raw_x = np.empty(0, dtype=float)
        self._raw_y = np.empty(0, dtype=float)
        self._labels: list[pg.TextItem] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.view = pg.PlotWidget()
        self.view.setMenuEnabled(False)
        self.view.setMouseEnabled(x=True, y=True)
        self.view.showGrid(x=True, y=True, alpha=0.18)
        self.view.setLabel("bottom", x_label)
        self.view.setLabel("left", y_label)
        layout.addWidget(self.view, 1)

        actions = QHBoxLayout()
        actions.addStretch()
        reset = QPushButton("复位视图", objectName="plotToolButton")
        reset.setToolTip("恢复到完整数据范围")
        reset.clicked.connect(self.reset_view)
        actions.addWidget(reset)
        layout.addLayout(actions)

        self._curve = self.view.plot()
        self._curve.setClipToView(True)
        self._curve.setDownsampling(auto=True, method="peak")
        self._peaks = pg.ScatterPlotItem(size=9, symbol="t")
        self.view.addItem(self._peaks)

        self._cross_x = pg.InfiniteLine(angle=90, movable=False)
        self._cross_y = pg.InfiniteLine(angle=0, movable=False)
        self._cross_x.setVisible(False)
        self._cross_y.setVisible(False)
        self.view.addItem(self._cross_x, ignoreBounds=True)
        self.view.addItem(self._cross_y, ignoreBounds=True)
        self._readout = pg.TextItem(anchor=(0, 1))
        self._readout.setVisible(False)
        self.view.addItem(self._readout, ignoreBounds=True)
        self._mouse_proxy = pg.SignalProxy(
            self.view.scene().sigMouseMoved,
            rateLimit=30,
            slot=self._on_mouse_moved,
        )
        self.refresh_theme()

    def set_data(
        self,
        x: Sequence[float],
        y: Sequence[float],
        *,
        auto_range: bool = False,
    ) -> None:
        """更新显示数据，同时保留未经降采样的原始数组。"""
        x_data = np.asarray(x, dtype=float)
        y_data = np.asarray(y, dtype=float)
        if x_data.size != y_data.size:
            raise ValueError("x/y 数据长度必须一致")
        self._raw_x = x_data.copy()
        self._raw_y = y_data.copy()
        self._curve.setData(x=x_data, y=y_data, skipFiniteCheck=True)
        self._clear_peak_labels()
        self._peaks.setData([], [])
        if auto_range and x_data.size:
            self.reset_view()

    def raw_data(self) -> tuple[np.ndarray, np.ndarray]:
        """返回原始数据副本，导出逻辑不得读取显示层降采样结果。"""
        return self._raw_x.copy(), self._raw_y.copy()

    def set_ranges(
        self,
        x_min: float,
        x_max: float,
        y_min: float | None = None,
        y_max: float | None = None,
    ) -> None:
        self.view.setXRange(x_min, x_max, padding=0.01)
        if y_min is not None and y_max is not None:
            self.view.setYRange(y_min, y_max, padding=0.05)

    def reset_view(self) -> None:
        if self._raw_x.size:
            self.view.enableAutoRange()
            self.view.autoRange(padding=0.06)

    def annotate_peaks(self, maximum: int = 4) -> None:
        """标记最显著的局部峰值，数量受限以避免标签遮挡。"""
        self._clear_peak_labels()
        if self._raw_y.size < 3 or maximum <= 0:
            self._peaks.setData([], [])
            return
        candidates = np.flatnonzero(
            (self._raw_y[1:-1] > self._raw_y[:-2])
            & (self._raw_y[1:-1] >= self._raw_y[2:])
        ) + 1
        if not candidates.size:
            candidates = np.asarray([int(np.argmax(self._raw_y))])
        selected = candidates[np.argsort(self._raw_y[candidates])[-maximum:]]
        selected = selected[np.argsort(self._raw_x[selected])]
        tokens = current_palette()
        self._peaks.setData(
            x=self._raw_x[selected],
            y=self._raw_y[selected],
            brush=pg.mkBrush(tokens["accent"]),
            pen=pg.mkPen(tokens["surfacePanel"]),
        )
        for index in selected:
            label = pg.TextItem(
                f"{self._raw_x[index]:.2f}",
                color=tokens["textBase"],
                anchor=(0.5, 1.15),
            )
            label.setPos(float(self._raw_x[index]), float(self._raw_y[index]))
            self.view.addItem(label)
            self._labels.append(label)

    def refresh_theme(self) -> None:
        tokens = current_palette()
        self.view.setBackground(tokens["surfacePanel"])
        self._curve.setPen(pg.mkPen(tokens["plotLine"], width=2))
        cross_pen = pg.mkPen(tokens["plotAxis"], width=1, style=Qt.PenStyle.DashLine)
        self._cross_x.setPen(cross_pen)
        self._cross_y.setPen(cross_pen)
        for axis_name in ("bottom", "left"):
            axis = self.view.getAxis(axis_name)
            axis.setPen(pg.mkPen(tokens["plotAxis"]))
            axis.setTextPen(pg.mkPen(tokens["plotAxis"]))
        self._readout.setColor(tokens["textBase"])

    def _on_mouse_moved(self, event) -> None:
        position = event[0] if isinstance(event, (tuple, list)) else event
        plot_item = self.view.getPlotItem()
        if not plot_item.sceneBoundingRect().contains(position):
            self._set_crosshair_visible(False)
            return
        point = plot_item.vb.mapSceneToView(position)
        self._cross_x.setPos(point.x())
        self._cross_y.setPos(point.y())
        self._readout.setText(f"x {point.x():.3f}   y {point.y():.3f}")
        self._readout.setPos(point.x(), point.y())
        self._set_crosshair_visible(True)

    def _set_crosshair_visible(self, visible: bool) -> None:
        self._cross_x.setVisible(visible)
        self._cross_y.setVisible(visible)
        self._readout.setVisible(visible)

    def _clear_peak_labels(self) -> None:
        for label in self._labels:
            self.view.removeItem(label)
        self._labels.clear()


__all__ = ["SpectrumPlot"]
