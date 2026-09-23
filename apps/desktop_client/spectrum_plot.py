"""基于 pyqtgraph 的专业曲线组件。

组件保留原始数据，显示层启用可视区裁剪和峰值保持型降采样；十字光标
读数限制在约 30Hz，避免鼠标移动占用 UI 主线程。

两种用法：

* 单曲线（扫谱、调束）：``set_data(x, y)``；
* 多曲线紧凑版（手动页的束流电流趋势）：``compact=True`` 压扁最小高度，
  ``value_formatter`` 定制十字读数文字，``set_series([(x, y), (x2, y2)])``
  叠加第二条曲线与"历史最优"线。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from apps.desktop_client import plot_style
from apps.desktop_client.theme import current_palette


class SpectrumPlot(QWidget):
    """扫谱与调束共用的可交互曲线视图。"""

    def __init__(
        self,
        x_label: str,
        y_label: str,
        parent: QWidget | None = None,
        *,
        compact: bool = False,
        value_formatter: Callable[[float, float], str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setMinimumHeight(150 if compact else 280)
        self._compact = compact
        self._value_formatter = value_formatter
        self._raw_x = np.empty(0, dtype=float)
        self._raw_y = np.empty(0, dtype=float)
        self._labels: list[pg.TextItem] = []
        self._extra_curves: list[pg.PlotDataItem] = []
        # ---- 调束过程图的附加图元（语义化：本轮散点 / 历史最优 / 基线 / 目标）----
        self._trial_x: np.ndarray = np.empty(0, dtype=float)
        self._trial_y: np.ndarray = np.empty(0, dtype=float)
        self._trial_errors: np.ndarray | None = None
        self._trial_best: np.ndarray | None = None
        self._trial_scatter: pg.ScatterPlotItem | None = None
        self._trial_best_curve: pg.PlotDataItem | None = None
        self._trial_error_item: pg.ErrorBarItem | None = None
        self._trial_annotations: list[object] = []
        self._reference_lines: dict[str, pg.InfiniteLine] = {}
        self._science_ready = False
        # y 轴"只扩不缩"区间与 x 轴跟随窗口：轴稳定是"看起来流畅"的关键
        self._y_bounds: tuple[float, float] | None = None
        self._follow_window: int | None = None
        self._follow_latest = False
        # 增量追加合并到 10Hz：取数节奏（轮询）与渲染节奏解耦
        self._stream_timer = QTimer(self)
        self._stream_timer.setSingleShot(True)
        self._stream_timer.setInterval(100)
        self._stream_timer.timeout.connect(self._flush_trial_update)
        self._trial_dirty = False

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

    def set_series(
        self,
        series: Sequence[tuple[Sequence[float], Sequence[float]]],
        *,
        alternative: int = 1,
        best: Sequence[float] | None = None,
        auto_range_one_sided: bool = True,
    ) -> None:
        """画多条曲线：第 0 条用主色，第 ``alternative`` 条用副色，``best`` 用最优色。

        任何一条为空都不影响其它条；点数不一致也允许（趋势窗口里各路的采样
        时刻可能差一拍）。
        """
        tokens = current_palette()
        if not series:
            self.set_data([], [])
            self._clear_extra_curves()
            return
        self.set_data(series[0][0], series[0][1])

        pens = [pg.mkPen(tokens["plotLineAlt"], width=1.6)]
        if best is not None:
            pens.append(pg.mkPen(tokens["plotBest"], width=1.2, style=Qt.PenStyle.DashLine))
        while len(self._extra_curves) < len(pens):
            curve = self.view.plot()
            curve.setClipToView(True)
            self._extra_curves.append(curve)
        payload: list[tuple[Sequence[float], Sequence[float]]] = list(series[1:])
        if best is not None:
            payload = [*payload, (series[0][0], best)]
        for index, curve in enumerate(self._extra_curves):
            if index < len(payload):
                xs, ys = payload[index]
                curve.setPen(pens[index])
                curve.setData(x=xs, y=ys, skipFiniteCheck=True)
                curve.setVisible(True)
            else:
                curve.setData([], [])
                curve.setVisible(False)
        if auto_range_one_sided:
            self.view.enableAutoRange(axis="y")

    def _clear_extra_curves(self) -> None:
        for curve in self._extra_curves:
            curve.setData([], [])
            curve.setVisible(False)

    # ------------------------------------------------------------------
    # 调束过程图：本轮散点 + 误差棒 + best-so-far + 基线/目标参考线
    # ------------------------------------------------------------------
    def use_science_style(
        self,
        *,
        x_label: str | None = None,
        y_label: str | None = None,
        legend: bool = False,
    ) -> None:
        """切到论文式外观（四边刻度、刻度向内、淡网格、主题配色）。"""
        plot_style.apply_science_style(self.view, x_label=x_label, y_label=y_label)
        if legend:
            plot_style.style_legend(self.view)
        self._science_ready = True

    def set_axis_labels(self, x_label: str | None = None, y_label: str | None = None) -> None:
        if x_label is not None:
            self.view.setLabel("bottom", x_label)
        if y_label is not None:
            self.view.setLabel("left", y_label)

    def set_follow_latest(self, follow: bool, *, window: int | None = None) -> None:
        """x 轴是否跟随最新轮次；``window`` 给定时只显示最近 N 个点。"""
        self._follow_latest = bool(follow)
        self._follow_window = window
        self._reset_axis_bounds()

    def export(self, path, *, scale: int = 3):
        """导出当前视图（PNG 高清 / SVG / PDF 矢量）。"""
        return plot_style.export_plot(self.view, path, scale=scale)

    def clear_trials(self) -> None:
        self._trial_x = np.empty(0, dtype=float)
        self._trial_y = np.empty(0, dtype=float)
        self._trial_errors = None
        self._trial_best = None
        self._raw_x = np.empty(0, dtype=float)
        self._raw_y = np.empty(0, dtype=float)
        self._curve.setData([], [])
        if self._trial_scatter is not None:
            self._trial_scatter.setData([], [])
        if self._trial_best_curve is not None:
            self._trial_best_curve.setData([], [])
        if self._trial_error_item is not None:
            self._trial_error_item.setVisible(False)
        for line in self._reference_lines.values():
            line.setVisible(False)
        self._clear_trial_annotations()
        self._reset_axis_bounds()

    def plot_trials(
        self,
        xs: Sequence[float],
        ys: Sequence[float],
        *,
        best: Sequence[float] | None = None,
        errors: Sequence[float] | None = None,
        baseline: float | None = None,
        target: float | None = None,
        best_text: str = "",
        x_label: str | None = None,
        y_label: str | None = None,
        source: str = "",
        mark_best: bool = True,
    ) -> None:
        """画"爬山图"语义的一组数据。

        与 :meth:`set_data` 的区别不是画法，而是**轴策略与图元生命周期**：
        数据每次轮询都会整体重算，但曲线对象只建一次，y 轴只扩不缩
        （带迟滞），所以新轮次出现时画面是"长出来"的，而不是整屏重新缩放。
        """
        x_arr = np.asarray(xs, dtype=float)
        y_arr = np.asarray(ys, dtype=float)
        if x_arr.size != y_arr.size:
            raise ValueError("x/y 数据长度必须一致")
        self._trial_x = x_arr
        self._trial_y = y_arr
        self._trial_errors = (
            None if errors is None else np.asarray(errors, dtype=float)
        )
        self._trial_best = None if best is None else np.asarray(best, dtype=float)
        self._raw_x = x_arr.copy()
        self._raw_y = y_arr.copy()

        if x_label is not None or y_label is not None:
            self.set_axis_labels(x_label, y_label)
        if not self._science_ready:
            self.use_science_style()

        style = plot_style.series_style()
        self._apply_trial_items(style, baseline, target, best_text, source, mark_best)
        self._update_axis_ranges()

    def append_trial(
        self,
        x: float,
        y: float,
        *,
        best: float | None = None,
        error: float | None = None,
        baseline: float | None = None,
        target: float | None = None,
        best_text: str = "",
        source: str = "",
    ) -> None:
        """增量追加一轮（不重建已有图元），并把重绘合并到 10Hz。

        服务端轮询是 700ms 一次，但一次可能补回多轮；这里把同一批落地合并成
        一次重绘，避免"一个点一次重画"造成的闪烁。
        """
        self._trial_x = np.append(self._trial_x, float(x))
        self._trial_y = np.append(self._trial_y, float(y))
        if error is not None:
            errors = (
                np.zeros(self._trial_x.size - 1, dtype=float)
                if self._trial_errors is None
                else self._trial_errors
            )
            self._trial_errors = np.append(errors, float(error))
        if best is not None:
            best_series = (
                self._trial_best
                if self._trial_best is not None
                else np.maximum.accumulate(self._trial_y[:-1])
            )
            self._trial_best = np.append(best_series, float(best))
        self._raw_x = self._trial_x.copy()
        self._raw_y = self._trial_y.copy()
        self._pending_meta = {
            "baseline": baseline,
            "target": target,
            "best_text": best_text,
            "source": source,
        }
        self._trial_dirty = True
        if not self._stream_timer.isActive():
            self._stream_timer.start()

    def _flush_trial_update(self) -> None:
        if not self._trial_dirty:
            return
        self._trial_dirty = False
        meta = getattr(self, "_pending_meta", {})
        style = plot_style.series_style()
        self._apply_trial_items(
            style,
            meta.get("baseline"),
            meta.get("target"),
            meta.get("best_text", ""),
            meta.get("source", ""),
            True,
        )
        self._update_axis_ranges()

    # ------------------------------------------------------------------ 内部
    def _apply_trial_items(
        self,
        style: dict,
        baseline: float | None,
        target: float | None,
        best_text: str,
        source: str,
        mark_best: bool,
    ) -> None:
        xs, ys = self._trial_x, self._trial_y
        self._curve.setPen(style["current"]["pen"])
        self._curve.setData(x=xs, y=ys, skipFiniteCheck=True)
        if self._trial_scatter is None:
            self._trial_scatter = pg.ScatterPlotItem(
                size=style["current"]["symbol_size"],
                symbol="o",
                brush=style["current"]["brush"],
                pen=pg.mkPen(current_palette()["surfacePanel"], width=1.0),
            )
            self.view.addItem(self._trial_scatter)
        self._trial_scatter.setData(x=xs, y=ys)

        if self._trial_errors is not None and self._trial_errors.size == ys.size:
            if self._trial_error_item is None:
                self._trial_error_item = plot_style.error_bar_item(xs, ys, self._trial_errors)
                self.view.addItem(self._trial_error_item)
            else:
                self._trial_error_item.setData(
                    x=xs,
                    y=ys,
                    top=self._trial_errors,
                    bottom=self._trial_errors,
                )
            self._trial_error_item.setVisible(True)
        elif self._trial_error_item is not None:
            self._trial_error_item.setVisible(False)

        if self._trial_best is not None:
            if self._trial_best_curve is None:
                self._trial_best_curve = self.view.plot(
                    pen=style["best"]["pen"], stepMode="right"
                )
            self._trial_best_curve.setData(x=xs, y=self._trial_best, skipFiniteCheck=True)

        self._sync_reference("baseline", baseline)
        self._sync_reference("target", target)

        self._clear_trial_annotations()
        if mark_best and ys.size:
            finite = np.isfinite(ys)
            if finite.any():
                index = int(np.nanargmax(ys))
                text = best_text or f"{ys[index]:.3f} @ {xs[index]:g}"
                marker, label = plot_style.best_point_marker(xs[index], ys[index], text)
                self.view.addItem(marker)
                self._trial_annotations.append(marker)
                if label is not None:
                    self.view.addItem(label)
                    self._trial_annotations.append(label)

    def _sync_reference(self, role: str, value: float | None) -> None:
        line = self._reference_lines.get(role)
        if value is None:
            if line is not None:
                line.setVisible(False)
            return
        if line is None:
            line = plot_style.reference_line(float(value), role=role)
            self.view.addItem(line, ignoreBounds=True)
            self._reference_lines[role] = line
        line.setPos(float(value))
        line.setVisible(True)

    def _clear_trial_annotations(self) -> None:
        for item in self._trial_annotations:
            self.view.removeItem(item)
        self._trial_annotations.clear()

    def _update_axis_ranges(self) -> None:
        xs, ys = self._trial_x, self._trial_y
        if ys.size == 0:
            return
        values = ys[np.isfinite(ys)]
        if values.size == 0:
            return
        lo, hi = float(values.min()), float(values.max())
        for extra in (self._trial_best,):
            if extra is not None and extra.size:
                finite = extra[np.isfinite(extra)]
                if finite.size:
                    lo = min(lo, float(finite.min()))
                    hi = max(hi, float(finite.max()))
        if self._trial_errors is not None and self._trial_errors.size:
            span_err = float(np.nanmax(np.abs(self._trial_errors)))
            lo -= span_err
            hi += span_err
        for role, line in self._reference_lines.items():
            if line.isVisible():
                position = float(line.value())
                # 目标值可能离数据很远（追踪模式），把它计入范围会把曲线压成一条线
                if role == "baseline":
                    lo = min(lo, position)
                    hi = max(hi, position)
        span = max(hi - lo, 1e-9)
        pad = 0.08 * span
        lo, hi = lo - pad, hi + pad
        if self._y_bounds is not None:
            cur_lo, cur_hi = self._y_bounds
            if lo >= cur_lo and hi <= cur_hi:
                pass  # 仍在当前窗口内：不动轴（这就是"平滑"的来源）
            else:
                lo, hi = min(lo, cur_lo), max(hi, cur_hi)
                self._y_bounds = (lo, hi)
        else:
            self._y_bounds = (lo, hi)
        if self._y_bounds is not None:
            self.view.disableAutoRange()
            self.view.setYRange(self._y_bounds[0], self._y_bounds[1], padding=0)

        count = int(xs.size)
        if self._follow_latest:
            window = max(2, int(self._follow_window or 20))
            left = max(0.5, count - window + 0.5)
            right = max(float(xs[-1]) if count else 1.0, count) + 0.5
        else:
            left = 0.5
            right = max(float(xs[-1]) if count else 1.0, count) + 0.5
        self.view.disableAutoRange()
        self.view.setXRange(left, right, padding=0)

    def _reset_axis_bounds(self) -> None:
        self._y_bounds = None
        self.view.enableAutoRange()

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
        self._y_bounds = None
        if self._raw_x.size:
            self.view.enableAutoRange()
            self.view.autoRange(padding=0.06)

    def mark_best(self, x: float, y: float, text: str = "") -> None:
        """标出曲线上的一个点（调束响应曲线的"最优采样"）。

        和 ``annotate_peaks`` 的分工：那个找**局部峰值**，判断依据是"比左右都高"，
        而响应曲线的采样点按参数值排序、点数不多，全局最高点通常不构成局部峰。
        这里直接标调用方指定的点，不在显示层替调用方判断"哪个点最好"。
        """
        self._clear_peak_labels()
        tokens = current_palette()
        self._peaks.setData(
            x=[float(x)],
            y=[float(y)],
            brush=pg.mkBrush(tokens["plotBest"]),
            pen=pg.mkPen(tokens["surfacePanel"]),
        )
        if text:
            label = pg.TextItem(text, color=tokens["textBase"], anchor=(0.5, 1.5))
            label.setPos(float(x), float(y))
            self.view.addItem(label)
            self._labels.append(label)

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
        if self._extra_curves:
            self._extra_curves[0].setPen(pg.mkPen(tokens["plotLineAlt"], width=1.6))
        if len(self._extra_curves) > 1:
            self._extra_curves[1].setPen(
                pg.mkPen(tokens["plotBest"], width=1.2, style=Qt.PenStyle.DashLine)
            )
        # 调束过程图的图元：主题切换后按新令牌重刷（否则深色下的散点边框会发白）
        if self._trial_scatter is not None or self._trial_best_curve is not None:
            style = plot_style.series_style()
            if self._trial_scatter is not None:
                self._trial_scatter.setBrush(style["current"]["brush"])
                self._trial_scatter.setPen(pg.mkPen(tokens["surfacePanel"], width=1.0))
            if self._trial_best_curve is not None:
                self._trial_best_curve.setPen(style["best"]["pen"])
            for role, line in self._reference_lines.items():
                line.setPen(plot_style.series_style()[role]["pen"])
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
        if self._value_formatter is not None:
            self._readout.setText(self._value_formatter(point.x(), point.y()))
        else:
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
