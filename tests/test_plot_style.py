"""绘图样式与流式渲染的可独立回归测试（批次 1：图表科研风）。

为什么单独测这一层：图表的问题（轴跳变、误差棒挂错点、导出失败）不会让任何业务流程
报错，只会在现场"看起来不对"。这里把三条底线钉住：

1. 论文式样式真的生效（四边刻度、内置刻度线、主题配色）；
2. 增量追加时 y 轴只扩不缩——这是"曲线不再整屏跳"的机制本身；
3. PNG / SVG / PDF 三种导出都能落盘且非空（矢量导出是"能进报告"的前提）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402
import pyqtgraph as pg  # noqa: E402

from apps.desktop_client import plot_style  # noqa: E402
from apps.desktop_client.spectrum_plot import SpectrumPlot  # noqa: E402


class PlotStyleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_science_style_enables_mirrored_axes_and_inward_ticks(self) -> None:
        plot = pg.PlotWidget()

        plot_style.apply_science_style(plot, x_label="轮次", y_label="目标量 (nA)")

        self.assertTrue(plot.getAxis("top").isVisible())
        self.assertTrue(plot.getAxis("right").isVisible())
        self.assertLess(plot.getAxis("bottom").style["tickLength"], 0)  # 向内
        self.assertEqual(plot.getAxis("bottom").labelText, "轮次")

    def test_error_bars_accept_plain_lists(self) -> None:
        """回归：ErrorBarItem 内部做数组减法，传 list 会抛 TypeError。"""
        item = plot_style.error_bar_item([1, 2, 3], [4.0, 5.0, 6.0], [0.1, 0.1, 0.1])

        self.assertIsInstance(item, pg.ErrorBarItem)

    def test_series_style_covers_every_semantic_role(self) -> None:
        style = plot_style.series_style()

        for role in ("current", "best", "baseline", "target", "model"):
            self.assertIn(role, style)

    def test_export_writes_png_svg_and_pdf(self) -> None:
        plot = pg.PlotWidget()
        plot.resize(480, 320)
        plot.show()
        plot_style.apply_science_style(plot, x_label="x", y_label="y")
        plot.plot([0, 1, 2], [0.5, 1.5, 1.0])
        self.app.processEvents()

        with tempfile.TemporaryDirectory() as folder:
            for suffix in (".png", ".svg", ".pdf"):
                path = Path(folder) / f"figure{suffix}"
                plot_style.export_plot(plot, path, scale=2)
                self.assertTrue(path.exists(), suffix)
                self.assertGreater(path.stat().st_size, 500, suffix)

    def test_export_rejects_unknown_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                plot_style.export_plot(pg.PlotWidget(), Path(folder) / "figure.txt")


class StreamingPlotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def make_plot(self) -> SpectrumPlot:
        plot = SpectrumPlot("轮次", "目标量")
        plot.resize(600, 320)
        plot.show()
        plot.use_science_style(x_label="轮次", y_label="目标量")
        return plot

    def test_plot_trials_keeps_raw_data_for_export_and_tests(self) -> None:
        plot = self.make_plot()

        plot.plot_trials([1, 2, 3], [8.0, 9.0, 8.5], best=[8.0, 9.0, 9.0], baseline=7.5)

        xs, ys = plot.raw_data()
        self.assertEqual(list(xs), [1.0, 2.0, 3.0])
        self.assertEqual(list(ys), [8.0, 9.0, 8.5])

    def test_appended_points_never_shrink_the_y_range(self) -> None:
        """轴稳定：新点落在当前窗口内时不许动轴，超出时只扩不缩。"""
        plot = self.make_plot()
        for index in range(1, 6):
            plot.append_trial(index, 10.0 + index, best=10.0 + index)
        plot._flush_trial_update()
        before = plot.view.getViewBox().viewRange()[1]

        # 明显更低但仍在窗口内的点：轴必须保持不动
        plot.append_trial(6, 11.0, best=15.0)
        plot._flush_trial_update()
        self.assertEqual(plot.view.getViewBox().viewRange()[1], before)

        # 超出窗口的点：只扩不缩
        plot.append_trial(7, 40.0, best=40.0)
        plot._flush_trial_update()
        after = plot.view.getViewBox().viewRange()[1]
        self.assertLessEqual(after[0], before[0])
        self.assertGreaterEqual(after[1], before[1])

    def test_follow_mode_tracks_the_latest_rounds(self) -> None:
        plot = self.make_plot()
        plot.set_follow_latest(True, window=4)
        for index in range(1, 13):
            plot.append_trial(index, 10.0 + index, best=10.0 + index)
        plot._flush_trial_update()

        x_range = plot.view.getViewBox().viewRange()[0]
        self.assertGreater(x_range[0], 1.0)
        self.assertGreaterEqual(x_range[1], 12.0)

    def test_reset_view_restores_auto_range(self) -> None:
        plot = self.make_plot()
        plot.append_trial(1, 10.0, best=10.0)
        plot._flush_trial_update()

        plot.reset_view()

        self.assertIsNone(plot._y_bounds)


if __name__ == "__main__":
    unittest.main()
