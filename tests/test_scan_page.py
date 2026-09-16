"""扫谱页：参数校验、质量换算只影响显示、以及终态后的补齐逻辑。

重点覆盖「终态 ≠ 数据已拉全」这个坑：服务端把最后几个点写完才置终态，
页面若一看到终态就收尾，用户看到的谱图会比实际保存的少点。

另外覆盖两块改造（报告 6.4 导出 / 6.6 回落）：
回落由服务端执行、页面只如实转述结果；导出始终写原始电流，缺的信号写 null。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

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


def readings(values: dict[str, float]) -> dict[str, dict]:
    """造一份批量读结果（``readings_by_signal`` 的输出形状）。"""
    return {
        signal: {"signal": signal, "value": value, "connected": True}
        for signal, value in values.items()
    }


def all_readings(value: float = 1.5) -> dict[str, dict]:
    """页面会去读的每个信号都给一个值，用来验证「拿得到的都写进 JSON」。"""
    return readings({signal: value for signal in scan_module.EXPORT_SIGNALS})


def signal_index(label: str) -> int:
    return next(
        i for i, (name, _s, _r) in enumerate(scan_module.MAGNET_AXES) if name == label
    )


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

    def test_start_is_refused_when_range_is_zero(self) -> None:
        # 起止相同，无论步长如何都只会生成 1 个点
        self.page.start_value.setValue(100.0)
        self.page.end_value.setValue(100.0)
        self.page.step_value.setValue(10.0)

        self.page.start_scan()

        self.assertIn("终点必须大于起点", self.page.state_label.text())
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


class _PageHarness(unittest.TestCase):
    """新用例的公共脚手架：一个干净页面 + 把请求函数换成桩。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.page = scan_module.ScanPage()

    def tearDown(self) -> None:
        self.page.deleteLater()

    def _stub(self, name: str, replacement) -> None:
        """替换 instrument_api 里的请求函数：页面只通过它联网，桩不碰网络。"""
        original = getattr(instrument_api, name)
        setattr(instrument_api, name, replacement)
        self.addCleanup(setattr, instrument_api, name, original)

    def _stub_module(self, name: str, replacement) -> None:
        """替换 scan 模块里的模块级名字（例如保存对话框）。"""
        original = getattr(scan_module, name)
        setattr(scan_module, name, replacement)
        self.addCleanup(setattr, scan_module, name, original)

    def _temp_path(self, suffix: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name) / f"export{suffix}"

    def _choose(self, path: Path) -> list[str]:
        """替掉保存对话框：记下它建议的默认文件名，直接返回给定路径。"""
        suggested: list[str] = []

        class _Dialog:
            @staticmethod
            def getSaveFileName(_parent, _title, default, _filters):
                suggested.append(default)
                return str(path), ""

        self._stub_module("QFileDialog", _Dialog)
        return suggested


class RetractWiringTests(_PageHarness):
    """报告 6.6：回落由执行服务完成，页面只发起请求并如实转述结果。"""

    def test_scan_request_carries_the_retract_spec(self) -> None:
        self.page.retract_current.setValue(0.0)
        self.page.retract_rate.setValue(2.5)
        self.page.auto_retract.setChecked(True)

        retract = self.page._build_request()["retract"]

        self.assertEqual(retract["current_a"], 0.0)
        self.assertEqual(retract["rate_a_s"], 2.5)
        self.assertIs(retract["auto"], True)

    def test_unchecked_auto_retract_still_sends_the_same_parameters(self) -> None:
        """不勾自动也要带 spec：手动按钮与服务端收尾共用同一套回落参数。"""
        self.page.auto_retract.setChecked(False)
        self.page.retract_current.setValue(0.0)

        retract = self.page._build_request()["retract"]

        self.assertIs(retract["auto"], False)
        self.assertEqual(retract["current_a"], 0.0)

    def test_terminal_run_does_not_trigger_any_client_side_write(self) -> None:
        """自动回落归服务端：客户端一写，关掉界面就不回落，而且只写得到第一路。"""
        calls: list[str] = []
        self._stub(
            "request_write",
            lambda *a, **k: calls.append("write") or _FakeThread(),
        )
        self._stub(
            "request_magnet_retract",
            lambda *a, **k: calls.append("retract") or _FakeThread(),
        )
        self._stub("request_scan_points", lambda *a, **k: _FakeThread())
        self.page.auto_retract.setChecked(True)
        self.page._run_id = "run-1"
        self.page._state = "completed"
        self.page._pending_terminal = True
        self.page._points_total = 1
        self.page._points = [point(0, 100.0)]

        self.page._on_points({"ok": True, "payload": {"points": []}})

        self.assertEqual(calls, [], "终态后客户端不得再发写入或回落请求")
        self.assertFalse(self.page._pending_terminal)
        self.assertTrue(self.page.start_button.isEnabled())

    def test_retract_result_is_reported_from_the_service_message(self) -> None:
        self._stub("request_scan_points", lambda *a, **k: _FakeThread())
        self.page._run_id = "run-1"

        self.page._apply_status(
            {
                "state": "completed",
                "total_points": 1,
                "completed_points": 1,
                "message": "已回落到 0，4 路回读已到位",
            }
        )

        self.assertIn("已回落到 0", self.page.retract_status.text())
        self.assertEqual(self.page.retract_status.property("state"), "good")

    def test_service_reports_a_failed_retract_the_page_says_so(self) -> None:
        """服务端没能退到位时，界面必须显示失败，而不是继承上一次的成功文案。"""
        self._stub("request_scan_points", lambda *a, **k: _FakeThread())
        self.page._run_id = "run-1"

        self.page._apply_status(
            {
                "state": "recovery_required",
                "total_points": 1,
                "completed_points": 1,
                "message": "回落未到位：磁铁3 回读仍为 118.40 A，设备组待人工确认",
            }
        )

        text = self.page.retract_status.text()
        self.assertNotIn("已回落", text)
        self.assertIn("未到位", text)
        self.assertEqual(self.page.retract_status.property("state"), "error")

    def test_manual_retract_sends_every_setpoint_of_the_selected_axis(self) -> None:
        requests: list[dict] = []
        self._stub(
            "request_magnet_retract",
            lambda request, base_url=None: requests.append(request) or _FakeThread(),
        )
        self.page.axis.setCurrentIndex(signal_index("磁铁1~4 同步"))
        self.page.retract_current.setValue(0.0)
        self.page.retract_rate.setValue(3.0)

        self.page._manual_retract()

        self.assertEqual(len(requests), 1)
        self.assertEqual(
            requests[0]["setpoint_signals"],
            [f"magnet.m{n}.current_setpoint" for n in range(1, 5)],
        )
        self.assertEqual(requests[0]["current_a"], 0.0)
        self.assertEqual(requests[0]["rate_a_s"], 3.0)
        # 请求在飞时按钮禁用：回落是设备动作，连点不该叠加下发
        self.assertFalse(self.page.retract_button.isEnabled())

    def test_retract_in_flight_blocks_a_second_request(self) -> None:
        requests: list[dict] = []
        self._stub(
            "request_magnet_retract",
            lambda request, base_url=None: requests.append(request) or _FakeThread(),
        )

        self.page._manual_retract()
        self.page._manual_retract()

        self.assertEqual(len(requests), 1)

    def test_success_is_shown_only_when_the_service_confirms_every_readback(self) -> None:
        self._stub("request_magnet_retract", lambda *a, **k: _FakeThread())
        self.page._manual_retract()

        self.page._on_retract_done(
            "磁铁1~4 同步",
            {
                "ok": True,
                "payload": {
                    "ok": True,
                    "message": "已回落到 0，4 路回读已到位",
                    "applied": {
                        "magnet.m1.current_readback": 0.0,
                        "magnet.m2.current_readback": 0.02,
                    },
                },
            },
        )

        text = self.page.retract_status.text()
        self.assertIn("已回落到 0", text)
        self.assertIn("m1=0.00", text)  # 逐路回读一并给出，现场据此核对
        self.assertEqual(self.page.retract_status.property("state"), "good")
        self.assertTrue(self.page.retract_button.isEnabled())

    def test_retract_that_did_not_reach_the_target_never_reads_as_success(self) -> None:
        self._stub("request_magnet_retract", lambda *a, **k: _FakeThread())
        self.page._manual_retract()

        # 端点用 200 + ok=false 表达「有一路没到位」
        self.page._on_retract_done(
            "磁铁1~4 同步",
            {
                "ok": True,
                "payload": {
                    "ok": False,
                    "message": "磁铁3 回读未进入容差（当前 118.40 A）",
                    "applied": {"magnet.m1.current_readback": 0.0},
                },
            },
        )

        text = self.page.retract_status.text()
        self.assertNotIn("已回落", text)
        self.assertIn("未到位", text)
        self.assertIn("磁铁3", text)
        self.assertEqual(self.page.retract_status.property("state"), "error")

    def test_occupied_device_group_reports_an_actionable_reason(self) -> None:
        self._stub("request_magnet_retract", lambda *a, **k: _FakeThread())
        self.page._manual_retract()

        # 409 的错误体取不到时只剩状态码，界面必须自己补一句能照做的原因
        self.page._on_retract_done(
            "磁铁1 电流", {"ok": False, "message": "HTTP 409", "payload": None}
        )

        text = self.page.retract_status.text()
        self.assertNotIn("已回落", text)
        self.assertIn("设备组被其他任务占用", text)
        self.assertEqual(self.page.retract_status.property("state"), "error")

    def test_retract_refusal_body_is_shown_verbatim(self) -> None:
        self._stub("request_magnet_retract", lambda *a, **k: _FakeThread())
        self.page._manual_retract()

        self.page._on_retract_done(
            "磁铁1 电流",
            {
                "ok": False,
                "message": "设备组「磁铁电源」被 retract-7 占用",
                "payload": None,
            },
        )

        text = self.page.retract_status.text()
        self.assertIn("retract-7", text)
        self.assertNotIn("已回落", text)


class ExportSnapshotTests(_PageHarness):
    """报告 6.4：JSON 补上平台真拿得到的字段，拿不到的一律 null 加说明。"""

    def test_json_carries_every_snapshot_group_the_platform_really_reads(self) -> None:
        self.page._points = [point(0, 100.0)]

        payload = self.page._collect_payload(all_readings(2.0))
        snapshot = payload["device_snapshot"]

        self.assertEqual(snapshot["gas"]["gas.ar.flow_readback"], 2.0)
        self.assertEqual(snapshot["gas"]["gas.he.flow_setpoint"], 2.0)
        self.assertEqual(snapshot["vacuum"]["vacuum.chamber_pressure"], 2.0)
        self.assertEqual(snapshot["sputter"]["sputter.power_readback"], 2.0)
        self.assertEqual(snapshot["sputter"]["sputter.arc_rate_readback"], 2.0)
        self.assertEqual(snapshot["ion_optics"]["ion_optics.focus.voltage_readback"], 2.0)
        self.assertEqual(snapshot["hv_array"]["hv_array.dw13.voltage_readback"], 2.0)
        self.assertEqual(snapshot["hv_bd"]["hv_bd.main.current_readback"], 2.0)
        self.assertEqual(snapshot["detector"]["detector.fc1.beam_current"], 2.0)
        self.assertEqual(snapshot["detector"]["detector.fc2.beam_current"], 2.0)
        self.assertEqual(payload["unavailable"]["signals"], [])

    def test_signals_the_service_cannot_read_are_null_and_listed(self) -> None:
        payload = self.page._collect_payload(readings({"gas.ar.flow_readback": 100.0}))

        self.assertEqual(
            payload["device_snapshot"]["gas"]["gas.ar.flow_readback"], 100.0
        )
        self.assertIsNone(
            payload["device_snapshot"]["sputter"]["sputter.power_readback"]
        )
        self.assertIn("sputter.power_readback", payload["unavailable"]["signals"])

    def test_labview_fields_are_explicitly_unavailable_not_invented(self) -> None:
        payload = self.page._collect_payload(all_readings())

        self.assertIsNone(payload["labview"]["source"])
        self.assertIsNone(payload["labview"]["latest_value"])
        self.assertIsNone(payload["labview"]["point_count"])
        self.assertIsNone(payload["labview"]["condense_length_cm"])
        self.assertTrue(
            payload["unavailable"]["not_migrated"], "未迁移的字段必须说明原因"
        )

    def test_acquisition_times_come_from_the_service_status(self) -> None:
        self._stub("request_scan_points", lambda *a, **k: _FakeThread())
        self.page._run_id = "run-1"
        self.page._apply_status(
            {
                "state": "running",
                "total_points": 2,
                "completed_points": 1,
                "started_at": "2026-09-11T00:00:00+00:00",
            }
        )
        self.page._apply_status(
            {
                "state": "completed",
                "total_points": 2,
                "completed_points": 2,
                "started_at": "2026-09-11T00:00:00+00:00",
                "finished_at": "2026-09-11T00:10:00+00:00",
            }
        )

        acquisition = self.page._collect_payload()["acquisition"]

        self.assertEqual(acquisition["started_at"], "2026-09-11T00:00:00+00:00")
        self.assertEqual(acquisition["finished_at"], "2026-09-11T00:10:00+00:00")
        self.assertEqual(acquisition["state"], "completed")
        self.assertEqual(acquisition["run_id"], "run-1")

    def test_export_reads_a_device_snapshot_before_writing(self) -> None:
        self.page._points = [point(0, 100.0)]
        asked: list[list[str]] = []
        self._stub(
            "request_read",
            lambda base_url=None, signals=None: (
                asked.append(list(signals or [])) or _FakeThread()
            ),
        )

        self.page._export("json")

        self.assertEqual(len(asked), 1)
        self.assertIn("sputter.power_readback", asked[0])
        self.assertIn("vacuum.chamber_pressure", asked[0])
        self.assertIn("detector.fc2.beam_current", asked[0])
        self.assertIn("设备快照", self.page.state_label.text())

    def test_a_second_export_while_one_is_in_flight_is_refused(self) -> None:
        self.page._points = [point(0, 100.0)]
        asked: list[list[str]] = []
        self._stub(
            "request_read",
            lambda base_url=None, signals=None: (
                asked.append(list(signals or [])) or _FakeThread()
            ),
        )

        self.page._export("json")
        self.page._export("json")

        self.assertEqual(len(asked), 1, "在飞的导出没回来前不该再发一次读")
        self.assertIn("上一次导出", self.page.state_label.text())


class ExportWriteTests(_PageHarness):
    """导出真正落盘的内容：文件名、TXT 列语义、快照读失败也不能丢点。"""

    def test_json_file_uses_the_demo_default_name_and_keeps_the_snapshot(self) -> None:
        target = self._temp_path(".json")
        suggested = self._choose(target)
        self.page._points = [point(0, 100.0)]
        self.page.element_edit.setText("Ar")
        self.page.index_edit.setValue(7)
        self._stub("request_read", lambda *a, **k: _FakeThread())

        self.page._export("json")
        self.page._on_export_snapshot(
            {
                "ok": True,
                "payload": {
                    "readings": [
                        {"signal": "gas.ar.flow_setpoint", "value": 100.0, "connected": True},
                        {"signal": "gas.he.flow_setpoint", "value": 0.18, "connected": True},
                        {"signal": "vacuum.chamber_pressure", "value": 0.9765, "connected": True},
                        {"signal": "sputter.power_setpoint", "value": 35.0, "connected": True},
                    ]
                },
            }
        )

        document = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(document["name"], "Ar-007-Ar100-He0.18-0.98Pa-35W--cm")
        self.assertEqual(
            document["device_snapshot"]["gas"]["gas.ar.flow_setpoint"], 100.0
        )
        self.assertIsNone(
            document["device_snapshot"]["sputter"]["sputter.arc_rate_readback"]
        )
        self.assertEqual(len(document["points"]), 1)
        # 对话框里建议的名字与文件里记的名字是同一个
        self.assertEqual(suggested, [document["name"] + ".json"])
        self.assertIn("已导出", self.page.state_label.text())

    def test_export_records_no_fake_scan_rate(self) -> None:
        """点间速率不在契约里：导出不能拿界面上的数字冒充"本次扫描速率"。

        原来导出写的是界面上那个「扫描速率」输入框的值，而它从来没进过
        `ScanRunRequest`——真正生效的是映射里磁铁的 `max_rate`。现在写 null 并说明来源。
        """
        target = self._temp_path(".json")
        self._choose(target)
        self.page._points = [point(0, 100.0)]
        self._stub("request_read", lambda *a, **k: _FakeThread())

        self.page._export("json")
        self.page._on_export_snapshot({"ok": True, "payload": {"readings": []}})

        scan = json.loads(target.read_text(encoding="utf-8"))["scan"]
        self.assertIsNone(scan["rate_a_s"])
        self.assertIn("max_rate", scan["rate_source"])
        # 回落速率是另一回事：它真的进契约、真的生效，必须照常记录
        self.assertEqual(scan["samples_per_point"], self.page.samples.value())


class CondenseLengthTests(_PageHarness):
    """冷凝管长度：原 demo 由 LabVIEW 给，平台没有对应信号，改由界面手填。

    关键是"没填就是没填"——不能用 0 或别的数字冒充一段实测工况（报告 §8.8 第 4 条）。
    """

    def export(self, length: str = "") -> dict:
        target = self._temp_path(".json")
        self._choose(target)
        self.page._points = [point(0, 100.0)]
        self.page.condense_length_edit.setText(length)
        self._stub("request_read", lambda *a, **k: _FakeThread())

        self.page._export("json")
        self.page._on_export_snapshot({"ok": True, "payload": {"readings": []}})

        return json.loads(target.read_text(encoding="utf-8"))

    def test_empty_length_keeps_the_placeholder_in_the_name(self) -> None:
        document = self.export()

        self.assertTrue(document["name"].endswith("-cm"), document["name"])
        self.assertIsNone(document["scan"]["condense_length_cm"])

    def test_filled_length_goes_into_the_name_and_the_record(self) -> None:
        document = self.export("12.5")

        self.assertTrue(document["name"].endswith("12.5cm"), document["name"])
        self.assertEqual(document["scan"]["condense_length_cm"], 12.5)

    def test_length_source_is_recorded_next_to_the_value(self) -> None:
        """手填值必须带着来源：否则事后会被当成实测工况。"""
        document = self.export("20")

        self.assertIn("手填", document["scan"]["condense_length_source"])
        self.assertTrue(
            any("condense_length_cm" in note for note in document["unavailable"]["not_migrated"])
        )

    def test_labview_length_stays_null(self) -> None:
        """LabVIEW 那条链路仍然没接：labview 段不能因为手填了就凭空有值。"""
        document = self.export("20")

        self.assertIsNone(document["labview"]["condense_length_cm"])

    def test_partial_input_does_not_break_the_export(self) -> None:
        """校验器允许"-"这类中间态：解析不出数字就按没填处理，而不是抛异常。"""
        document = self.export("-")

        self.assertTrue(document["name"].endswith("-cm"), document["name"])
        self.assertIsNone(document["scan"]["condense_length_cm"])

    def test_zero_is_treated_as_not_filled(self) -> None:
        """0 cm 冷凝管不存在：宁可当没填，也不要写一个物理上不可能的工况。"""
        document = self.export("0")

        self.assertTrue(document["name"].endswith("-cm"), document["name"])
        self.assertIsNone(document["scan"]["condense_length_cm"])

    def test_failed_snapshot_read_still_exports_the_points(self) -> None:
        """设备没连上不该让现场丢掉一场扫描：点照常导出，缺的字段写 null + 原因。"""
        target = self._temp_path(".json")
        self._choose(target)
        self.page._points = [point(0, 100.0)]
        self._stub("request_read", lambda *a, **k: _FakeThread())

        self.page._export("json")
        # 按信号读失败 → 自动回退全量读 → 全量也失败 → 才按"快照为空"导出
        self.page._on_export_snapshot({"ok": False, "message": "连接被拒", "payload": None})
        self.page._on_export_fallback({"ok": False, "message": "连接被拒"}, "连接被拒")

        document = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(len(document["points"]), 1)
        self.assertIsNone(document["device_snapshot"]["gas"]["gas.ar.flow_readback"])
        self.assertIn("连接被拒", document["unavailable"]["note"])

    def test_snapshot_read_falls_back_to_the_full_mapping(self) -> None:
        """某一路被从映射里删掉时（服务端 400）不能整份快照变空：回退全量读。"""
        target = self._temp_path(".json")
        self._choose(target)
        self.page._points = [point(0, 100.0)]
        reads: list[list[str] | None] = []

        def fake_read(*_args, **kwargs):
            reads.append(kwargs.get("signals"))
            return _FakeThread()

        self._stub("request_read", fake_read)

        self.page._export("json")
        self.page._on_export_snapshot(
            {"ok": False, "message": "未配置的业务信号：sputter.arc_rate_readback"}
        )
        self.page._on_export_fallback(
            {
                "ok": True,
                "payload": {
                    "readings": [
                        {"signal": "gas.ar.flow_setpoint", "value": 100.0, "connected": True},
                        {"signal": "vacuum.chamber_pressure", "value": 0.98, "connected": True},
                    ]
                },
            },
            "未配置的业务信号：sputter.arc_rate_readback",
        )

        self.assertEqual(len(reads), 2, "第一次按信号读，第二次必须回退为全量读")
        self.assertIsNone(reads[1], "全量读就是不带 signals")
        document = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(document["device_snapshot"]["gas"]["gas.ar.flow_setpoint"], 100.0)
        self.assertIn("回退为全量读取", document["unavailable"]["note"])

    def test_txt_keeps_raw_current_and_beam_when_mass_is_selected(self) -> None:
        bad = point(1, 120.0)
        bad["quality"] = "unsettled"
        bad["included"] = False
        self.page._points = [point(0, 100.0), bad]
        self.page.rb_mass.setChecked(True)
        target = self._temp_path(".txt")

        self.page._write_txt(str(target))

        lines = target.read_text(encoding="utf-8").splitlines()
        self.assertIn("current_A", lines[0])
        self.assertIn("beam_nA", lines[0])
        self.assertIn("mass_u", lines[0])
        rows = [line for line in lines if not line.startswith("#")]
        self.assertEqual(len(rows), 2, "质量不合格的点也要导出，不能静默丢掉")
        first = rows[0].split("\t")
        self.assertEqual(float(first[0]), 100.0, "第一列永远是原始磁铁电流")
        self.assertEqual(float(first[1]), 8.31)
        self.assertAlmostEqual(
            float(first[2]), scan_module._mass(100.0, self.page._coefficients()), places=5
        )
        self.assertEqual(first[3], "ok")
        second = rows[1].split("\t")
        self.assertEqual(float(second[0]), 120.0)
        self.assertEqual(second[3], "unsettled")
        self.assertIn("1 点质量不合格", lines[1])

    def test_txt_without_mass_selection_has_no_mass_column(self) -> None:
        self.page._points = [point(0, 100.0)]
        target = self._temp_path(".txt")

        self.page._write_txt(str(target))

        header = target.read_text(encoding="utf-8").splitlines()[0]
        self.assertIn("current_A", header)
        self.assertNotIn("mass_u", header)


class ExportNameTests(_PageHarness):
    """默认文件名：对齐 demo 命名；缺值和非法字符都不能让它抛异常。"""

    def test_name_carries_element_index_gas_pressure_power_and_length(self) -> None:
        self.page.element_edit.setText("Ar")
        self.page.index_edit.setValue(7)

        name = self.page._default_name(
            readings(
                {
                    "gas.ar.flow_setpoint": 100.0,
                    "gas.he.flow_setpoint": 0.18,
                    "vacuum.chamber_pressure": 0.9765,
                    "sputter.power_setpoint": 35.0,
                }
            )
        )

        self.assertTrue(name.startswith("Ar-007-Ar100-"), name)
        self.assertIn("He0.18", name)
        self.assertIn("0.98Pa", name)
        self.assertIn("35W", name)
        self.assertIn("cm", name)

    def test_missing_readings_become_placeholders_instead_of_failing(self) -> None:
        name = self.page._default_name({})

        self.assertNotIn("None", name)
        self.assertIn("-Pa", name)
        self.assertIn("-W", name)
        self.assertIn("-cm", name)  # condense length 平台没有信号，恒为占位

    def test_illegal_windows_characters_are_sanitized_and_length_is_capped(self) -> None:
        self.page.element_edit.setText('Al/Cu:2*?"<>|\\' + "x" * 300)

        name = self.page._default_name({})

        self.assertTrue(name.startswith("Al_Cu_2_"), name)
        for char in '<>:"/\\|?*':
            self.assertNotIn(char, name)
        self.assertLessEqual(len(name), scan_module.MAX_FILENAME_LEN)


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
