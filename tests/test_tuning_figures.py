"""结果页科研图（Matplotlib 画布）的守门测试。

为什么单独测这一层：响应面矩阵的配色、色标量程、叠加墨色的可读性都不会让任何
业务流程报错，只会在现场"图看不清"或"两个渲染器画出的图对不上"。这里钉住四件事：

1. 预测均值用的是**同一支蓝绿色图**（viridis），不确定性用暖色（magma）——
   两套渲染器（Matplotlib 画布 / pyqtgraph 兼容路径）必须一致；
2. 色标量程是全矩阵共用的（各配对图之间颜色可比），并写明"其余参数固定在最优"；
3. 叠加元素（等高线/采样点/最优十字）带深色衬底：viridis 顶部接近亮黄，
   纯白线压上去几乎看不见；
4. 单图模式"适合窗口"真的不留滚动条，且视口尺寸变化后会重排。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea  # noqa: E402

from apps.desktop_client.science_canvas import (  # noqa: E402
    SURFACE_MEAN_CMAP,
    SURFACE_STD_CMAP,
    ScientificAnalysisCanvas,
)

PARAMS = ["magnet.m1.current_setpoint", "ion_optics.focus.voltage_setpoint"]


def surface_payload(grid: int = 16) -> dict:
    """两块配对面板：下三角均值、上三角标准差（与服务端返回形状一致）。"""
    xs = np.linspace(100.0, 260.0, grid)
    ys = np.linspace(1800.0, 2600.0, grid)
    xx, yy = np.meshgrid(xs, ys, indexing="ij")
    mean = 14.0 - 6.0 * np.exp(-(((xx - 205.0) / 40.0) ** 2 + ((yy - 2280.0) / 250.0) ** 2))
    std = 0.8 + 2.0 * np.exp(-(((xx - 150.0) / 60.0) ** 2 + ((yy - 2000.0) / 400.0) ** 2))
    return {
        "available": True,
        "params": PARAMS,
        "bounds": [[100.0, 260.0], [1800.0, 2600.0]],
        "pairs": {"0_1": {"mean": mean.tolist(), "std": std.tolist()}},
        "points": [[120.0, 1900.0], [230.0, 2500.0]],
        "best": [206.0, 2274.0],
        "note": "测试用响应面。",
    }


class SurfacePaletteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def render(self, **kwargs) -> ScientificAnalysisCanvas:
        canvas = ScientificAnalysisCanvas()
        canvas.resize(900, 700)
        canvas.show()
        canvas.surface(surface_payload(), ["磁铁1 (A)", "聚焦 (V)"], note="测试", **kwargs)
        self.app.processEvents()
        return canvas

    def images(self, canvas) -> list:
        return [axis.images[0] for axis in canvas.figure.axes if axis.images]

    def test_predicted_mean_uses_the_blue_green_palette(self) -> None:
        """预测均值 = viridis（中段即蓝绿 #21918c），不确定性 = magma。

        两个渲染器取同一对常量：同一个量在两条代码路径下用不同色图，是图上最容易
        看错的地方（兼容路径原来用 CET-D1 彩虹色）。
        """
        canvas = self.render()

        self.assertEqual(SURFACE_MEAN_CMAP, "viridis")
        self.assertEqual(SURFACE_STD_CMAP, "magma")
        maps = [image.get_cmap().name for image in self.images(canvas)]
        self.assertIn("viridis", maps)
        self.assertIn("magma", maps)

    def test_mean_palette_midpoint_is_actually_teal(self) -> None:
        """钉住"蓝绿"这个选型理由本身：中段绿蓝都远高于红，且两者接近。

        viridis 中点 = #21918c：R≈32、G≈144、B≈140——绿蓝同时高、彼此接近，
        就是所谓的蓝绿色；GnBu 之类更"淡"的蓝绿则是感知不均匀（见 science_canvas
        顶部的选型说明）。
        """
        from matplotlib import colormaps

        mid = [round(float(channel) * 255) for channel in colormaps[SURFACE_MEAN_CMAP](0.5)]

        self.assertGreater(mid[1] - mid[0], 60.0, mid)  # 绿 ≫ 红
        self.assertGreater(mid[2] - mid[0], 60.0, mid)  # 蓝 ≫ 红
        self.assertLess(abs(mid[1] - mid[2]), 30.0, mid)  # 绿蓝相当 = 蓝绿而非纯绿

    def test_contours_have_a_dark_halo_under_the_white_line(self) -> None:
        """白色等高线在 viridis 亮端几乎看不见，必须垫一层深色（两遍等高线）。"""
        from matplotlib.contour import ContourSet

        canvas = self.render()

        contours = [
            item
            for axis in canvas.figure.axes
            for item in axis.collections
            if isinstance(item, ContourSet)
        ]
        # 均值面板各画两遍：一遍深色衬底、一遍白线
        self.assertEqual(len(contours), 2, "均值面板应当有两层等高线")
        edges = [
            tuple(round(float(channel), 3) for channel in np.ravel(item.get_edgecolor())[:3])
            for item in contours
        ]
        self.assertIn((1.0, 1.0, 1.0), edges, "一层必须是白线")
        self.assertTrue(
            any(sum(colour) < 0.6 for colour in edges), f"另一层必须是深色衬底：{edges}"
        )

    def test_colorbar_carries_the_objective_label_and_shared_scale(self) -> None:
        canvas = self.render(value_label="FC1 束流 (nA)")

        labels = [
            axis.get_ylabel() for axis in canvas.figure.axes if axis.get_ylabel()
        ]
        self.assertTrue(
            any("GP 预测均值" in text and "FC1 束流 (nA)" in text for text in labels),
            labels,
        )
        self.assertTrue(any("不确定性" in text for text in labels), labels)
        # 全矩阵共用色标这件事必须写在图上，否则格子之间的颜色没法比
        title = canvas.figure._suptitle.get_text()
        self.assertIn("共用同一色标", title)

    def test_export_writes_svg_and_pdf(self) -> None:
        canvas = self.render()

        with tempfile.TemporaryDirectory() as folder:
            for suffix in (".png", ".svg", ".pdf"):
                path = Path(folder) / f"surface{suffix}"
                canvas.export(path)
                self.assertTrue(path.exists(), suffix)
                self.assertGreater(path.stat().st_size, 1000, suffix)

    def test_matrix_canvas_does_not_follow_the_viewport(self) -> None:
        """矩阵图按维数定尺寸、靠滚动区承载：不能因为视口小就把格子缩到看不清。"""
        canvas = self.render()

        self.assertFalse(canvas.auto_fit, "矩阵图不该跟随视口自动缩放")
        self.assertGreaterEqual(canvas.width(), 640)


class CanvasFitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_fit_leaves_no_scrollbar_in_a_short_viewport(self) -> None:
        """回归：原来下限卡在 0.45，"适合窗口"在 700px 高的窗口上仍留 9px 滚动条。"""
        scroll = QScrollArea()
        scroll.resize(540, 175)
        canvas = ScientificAnalysisCanvas()
        scroll.setWidget(canvas)
        canvas.attach_scroll_area(scroll)
        canvas.history([1, 2, 3], [8.0, 9.0, 8.5], [8.0, 9.0, 9.0],
                       baseline=8.0, y_label="FC1 (nA)", note="测试")
        scroll.resize(540, 175)
        self.app.processEvents()

        canvas.fit_to(scroll.viewport().width(), scroll.viewport().height())
        self.app.processEvents()

        self.assertLessEqual(scroll.verticalScrollBar().maximum(), 0)

    def test_fit_uses_the_available_area_on_a_large_viewport(self) -> None:
        canvas = ScientificAnalysisCanvas()
        canvas.resize(640, 400)
        canvas.show()
        canvas.history([1, 2], [8.0, 9.0], [8.0, 9.0], baseline=None,
                       y_label="FC1 (nA)", note="测试")
        self.app.processEvents()

        canvas.fit_to(900, 600)

        self.assertGreater(canvas.width(), 640)
        self.assertGreater(canvas.height(), 400)

    def test_manual_zoom_stops_following_the_viewport(self) -> None:
        canvas = ScientificAnalysisCanvas()
        canvas.resize(640, 400)
        canvas.show()

        canvas.fit_to(800, 500)
        self.assertTrue(canvas.auto_fit)

        canvas.zoom_canvas(1.15)

        self.assertFalse(canvas.auto_fit)

    def test_same_viewport_is_not_fitted_twice(self) -> None:
        """防循环：重排会改变滚动条、滚动条又改视口，同一尺寸只许排一次。"""
        canvas = ScientificAnalysisCanvas()
        canvas.resize(640, 400)
        canvas.show()

        canvas.fit_to(700, 400)
        first = canvas.width()
        canvas._canvas_scale = 99.0  # 故意打乱：同尺寸再 fit 不该改动它
        canvas.fit_to(700, 400)

        self.assertEqual(canvas.width(), first)
        self.assertEqual(canvas._canvas_scale, 99.0)


if __name__ == "__main__":
    unittest.main()
