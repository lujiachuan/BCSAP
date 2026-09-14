"""扫谱页：参数校验、质量换算只影响显示、以及终态后的补齐逻辑。

重点覆盖「终态 ≠ 数据已拉全」这个坑：服务端把最后几个点写完才置终态，
页面若一看到终态就收尾，用户看到的谱图会比实际保存的少点。
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from apps.desktop_client import instrument_api
from apps.desktop_client.pages import scan as scan_module
from apps.desktop_client.pages.registry import page_specs


def point(index: int, coordinate: float) -> dict:
    return {
        "index": index,
        "target": coordinate,
        "coordinate": coordinate,
        "signal": 8.31,
        "quality": "ok",
        "at": "2026-09-11T00:00:00+00:00",
        "coordinate_spread": 0.0,
        "detail": None,
        "included": True,
    }


class MassConversionTests(unittest.TestCase):
    def test_mass_polynomial_matches_manual_computation(self) -> None:
        coefficients = [-1.08608, 0.02743, 0.01722, -1.9947e-07]
        value = 150.0
        expected = (
            coefficients[0]
            + coefficients[1] * value
            + coefficients[2] * value**2
            + coefficients[3] * value**3
        )

        self.assertAlmostEqual(scan_module._mass(value, coefficients), expected, places=9)

    def test_mass_of_zero_current_is_the_constant_term(self) -> None:
        self.assertAlmostEqual(scan_module._mass(0.0, [-1.08608, 0.3]), -1.08608, places=9)


class ScanPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.page = scan_module.ScanPage()

    def tearDown(self) -> None:
        self.page.deleteLater()

    def test_page_is_registered_in_control_section(self) -> None:
        specs = {spec.key: spec for spec in page_specs()}

        self.assertEqual(specs["scan"].label, "扫谱")
        self.assertEqual(specs["scan"].section, "control")

    def test_pause_button_is_disabled_per_architecture_note(self) -> None:
        """文档 9.3：只有支持安全暂停和恢复后才开放暂停按钮。"""
        self.assertFalse(self.page.pause_button.isEnabled())
        self.assertIn("9.3", self.page.pause_button.toolTip())

    def test_axis_options_cover_single_and_grouped_sweeps(self) -> None:
        labels = [label for label, _signals, _rb in scan_module.MAGNET_AXES]

        self.assertIn("磁铁1 电流", labels)
        self.assertIn("磁铁1~4 同步", labels)

    def test_grouped_axis_sends_all_setpoints_and_one_readback(self) -> None:
        index = next(
            i for i, (label, _s, _r) in enumerate(scan_module.MAGNET_AXES)
            if label == "磁铁1~4 同步"
        )
        self.page.axis.setCurrentIndex(index)

        request = self.page._build_request()

        self.assertEqual(len(request["axis"]["setpoint_signals"]), 4)
        self.assertEqual(request["axis"]["readback_signal"], "magnet.m1.current_readback")

    def test_request_asks_service_to_record_unsettled_instead_of_failing(self) -> None:
        """一个点没稳定不该废掉整场，但必须被标注而不是当成功。"""
        self.assertEqual(self.page._build_request()["on_unsettled"], "record")

    # ---------------- 参数校验 ----------------
    def test_planned_points_counts_both_ends(self) -> None:
        self.page.start_value.setValue(100.0)
        self.page.end_value.setValue(140.0)
        self.page.step_value.setValue(20.0)

        self.assertEqual(self.page._planned_points(), 3)

    def test_summary_rejects_end_not_greater_than_start(self) -> None:
        self.page.start_value.setValue(200.0)
        self.page.end_value.setValue(100.0)

        self.page._update_scan_summary()

        self.assertIn("终点必须大于起点", self.page.scan_summary.text())

    def test_start_is_refused_when_range_has_one_point(self) -> None:
        # 终点大于起点，但按步长只落得下 1 个点
        self.page.start_value.setValue(100.0)
        self.page.end_value.setValue(100.5)
        self.page.step_value.setValue(10.0)

        self.page.start_scan()

        self.assertIn("两个采集点", self.page.state_label.text())
        self.assertIsNone(self.page._run_id)

    def test_start_is_refused_above_point_limit(self) -> None:
        """控件步长下限 0.1 A 让这个上限在界面上够不着，所以放宽下限来验证守卫本身。"""
        self.page.step_value.setMinimum(0.001)
        self.page.start_value.setValue(0.0)
        self.page.end_value.setValue(600.0)
        # 控件是 2 位小数：0.01 A × 600 A 量程 = 60001 点，超过上限
        self.page.step_value.setValue(0.01)

        self.page.start_scan()

        self.assertIn(f"{scan_module.MAX_POINTS:,}", self.page.state_label.text())
        self.assertIsNone(self.page._run_id)

    def test_finest_allowed_step_stays_within_limit(self) -> None:
        """正常操作范围内不该触到上限：600 A / 0.1 A 只有 6001 点。"""
        self.page.start_value.setValue(0.0)
        self.page.end_value.setValue(600.0)
        self.page.step_value.setValue(self.page.step_value.minimum())

        self.assertLess(self.page._planned_points(), scan_module.MAX_POINTS)

    # ---------------- 终态后的补齐 ----------------
    def test_terminal_status_does_not_finish_before_points_are_drained(self) -> None:
        """终态时若点还没拉全，页面必须继续拉，不能立刻收尾。"""
        fetched: list[int] = []
        self.page._run_id = "run-1"
        self.page._timer.start()
        self.page._since = 1
        # 只有 1 个点，而服务端说共 3 个
        self.page._points = [point(0, 100.0)]

        original = instrument_api.request_scan_points
        instrument_api.request_scan_points = (  # type: ignore[assignment]
            lambda run_id, since=0, base_url=None: fetched.append(since) or _FakeThread()
        )
        self.addCleanup(setattr, instrument_api, "request_scan_points", original)

        self.page._apply_status(
            {"state": "completed", "total_points": 3, "completed_points": 3}
        )

        self.assertTrue(fetched, "终态时应发起补齐请求")
        self.assertTrue(self.page._timer.isActive(), "点没拉全前不得停止轮询")
        self.assertTrue(self.page._pending_terminal)

    def test_finalize_runs_after_points_are_complete(self) -> None:
        self.page._run_id = "run-1"
        self.page._state = "completed"
        self.page._pending_terminal = True
        self.page._points_total = 3
        self.page._points = [point(0, 100.0), point(1, 120.0), point(2, 140.0)]
        self.page._timer.start()

        self.page._on_points({"ok": True, "payload": {"points": []}})

        self.assertFalse(self.page._pending_terminal)
        self.assertFalse(self.page._timer.isActive())
        self.assertTrue(self.page.start_button.isEnabled())

    def test_skipped_points_are_reported_not_hidden(self) -> None:
        """质量不合格的点不进曲线，但必须在状态条上说明，不能悄无声息。"""
        self.page._run_id = "run-1"
        self.page._state = "completed"
        self.page._pending_terminal = True
        self.page._points_total = 2
        bad = point(1, 120.0)
        bad["quality"] = "unsettled"
        bad["included"] = False
        self.page._points = [point(0, 100.0), bad]

        self.page._on_points({"ok": True, "payload": {"points": []}})

        self.assertIn("质量不合格", self.page.state_label.text())

    def test_redraw_skips_excluded_points(self) -> None:
        """被排除的点不参与绘图：当前值取最后一个**合格**点。"""
        bad = point(1, 120.0)
        bad["included"] = False
        self.page._points = [point(0, 100.0), bad, point(2, 140.0)]

        self.page._redraw()

        # 排除点夹在中间，最后一个合格点是 140（若把不合格点也算进来会看到别的值）
        self.assertIn("140.000", self.page.current_label.text())
        self.assertIn("100.000", self.page.peak_label.text())

    def test_mass_toggle_only_changes_display_unit(self) -> None:
        self.page._points = [point(1, 120.0)]
        self.page._redraw()
        self.assertTrue(self.page.current_label.text().endswith("A"))

        self.page.mass_toggle.setChecked(True)
        self.page._redraw()
        self.assertTrue(self.page.current_label.text().endswith("u"))
        # 请求里带的仍是电流参数与系数，落盘由服务端决定
        self.assertEqual(
            self.page._build_request()["axis"]["readback_signal"],
            "magnet.m1.current_readback",
        )

    def test_operation_active_only_until_terminal(self) -> None:
        self.assertFalse(self.page.is_operation_active())

        self.page._run_id = "run-1"
        self.page._state = "running"
        self.assertTrue(self.page.is_operation_active())

        self.page._state = "completed"
        self.assertFalse(self.page.is_operation_active())


class _FakeThread:
    """替代真实的请求线程：只记录调用，不联网。"""

    def __init__(self) -> None:
        self.completed = _SignalStub()

    def start(self) -> None:  # pragma: no cover - 桩
        return None


class _SignalStub:
    def connect(self, _slot) -> None:  # pragma: no cover - 桩
        return None


if __name__ == "__main__":
    unittest.main()
