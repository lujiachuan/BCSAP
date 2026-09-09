"""M2–M4 UI 升级的可独立回归测试。"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QSettings, QSize
from PySide6.QtWidgets import QApplication

from apps.desktop_client.motion import PageTransitionController
from apps.desktop_client.nav_icons import make_nav_icon, make_symbol
from apps.desktop_client.pages import SystemSettingsPage
from apps.desktop_client.spectrum_plot import SpectrumPlot


class UiUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_icons_render_at_requested_logical_size(self) -> None:
        for icon in (make_nav_icon("scan"), make_symbol("menu"), make_symbol("moon")):
            pixmap = icon.pixmap(QSize(22, 22))
            self.assertFalse(pixmap.isNull())
            self.assertEqual(pixmap.deviceIndependentSize().toSize(), QSize(22, 22))

    def test_icons_keep_transparent_safe_margin(self) -> None:
        for icon, size in ((make_nav_icon("scan"), 22), (make_symbol("menu"), 15)):
            image = icon.pixmap(QSize(size, size)).toImage()
            occupied = [
                (x, y)
                for y in range(image.height())
                for x in range(image.width())
                if image.pixelColor(x, y).alpha() > 0
            ]
            xs = [point[0] for point in occupied]
            ys = [point[1] for point in occupied]
            self.assertGreaterEqual(min(xs), 1)
            self.assertGreaterEqual(min(ys), 1)
            self.assertLessEqual(max(xs), size - 2)
            self.assertLessEqual(max(ys), size - 2)

    def test_spectrum_plot_keeps_raw_data_and_marks_peaks(self) -> None:
        plot = SpectrumPlot("x", "y")
        plot.set_data([1, 2, 3, 4, 5], [0, 3, 1, 4, 0])
        raw_x, raw_y = plot.raw_data()
        self.assertEqual(raw_x.tolist(), [1, 2, 3, 4, 5])
        self.assertEqual(raw_y.tolist(), [0, 3, 1, 4, 0])
        plot.annotate_peaks(2)
        self.assertEqual(len(plot._labels), 2)

    def test_spectrum_plot_accepts_150k_points(self) -> None:
        plot = SpectrumPlot("x", "y")
        x_values = np.linspace(0.0, 1000.0, 150_000)
        plot.set_data(x_values, np.sin(x_values))
        raw_x, raw_y = plot.raw_data()
        self.assertEqual(raw_x.size, 150_000)
        self.assertEqual(raw_y.size, 150_000)

    def test_reduced_motion_skips_page_effect(self) -> None:
        settings = QSettings("SpectrumPlatformTests", "UiUpgrade")
        settings.setValue("appearance/reduceMotion", True)
        controller = PageTransitionController(settings)
        page = SystemSettingsPage()
        controller.fade_in(page)
        self.assertIsNone(page.graphicsEffect())

    def test_page_effect_can_finish_cleanly(self) -> None:
        settings = QSettings("SpectrumPlatformTests", "UiUpgradeMotion")
        settings.setValue("appearance/reduceMotion", False)
        controller = PageTransitionController(settings)
        page = SystemSettingsPage()
        page.show()
        controller.fade_in(page)
        self.assertIsNotNone(page.graphicsEffect())
        controller.stop()
        self.assertIsNone(page.graphicsEffect())

    def test_settings_navigation_switches_responsively(self) -> None:
        page = SystemSettingsPage()
        page.resize(820, 600)
        page.show()
        self.app.processEvents()
        self.assertTrue(page.settings_selector.isVisible())
        page.settings_selector.setCurrentIndex(3)
        self.assertEqual(page.settings_stack.currentIndex(), 3)


if __name__ == "__main__":
    unittest.main()
