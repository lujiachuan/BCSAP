"""调束页在批次 3 逻辑修复后的界面侧行为（D6 / D7 / D10 / D15 / D16 的界面部分）。

守三件事：
1. 新阈值必须真的随请求下发、并随配置存取（否则界面上有的旋钮只是装饰）；
2. 客户端重启后要能**认领**正在运行的任务——看不见的任务既停不掉也读不到进度；
3. ETA 与"当前回读"要反映实测数据，而不是启动时的静态估计。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from apps.desktop_client import instrument_api
from apps.desktop_client.pages.tuning import TuningPage
from tests.test_tuning_layout import MAGNET, catalog, iteration, mapping


class _SignalStub:
    def __init__(self) -> None:
        self._callbacks: list = []

    def connect(self, callback, *_args, **_kwargs) -> None:
        self._callbacks.append(callback)


class _FakeThread:
    def __init__(self) -> None:
        self.completed = _SignalStub()

    def start(self) -> None:
        return None


class TuningClientFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        # 页面 show() 会尝试认领在跑的任务：单测里一律换成假线程，
        # 既不连本机服务，也不会让用例的时序取决于现场是否有任务在跑。
        self._network_patches = [
            mock.patch.object(instrument_api, "request_read", lambda *a, **k: None),
            mock.patch.object(
                instrument_api, "request_tuning_runs", lambda *a, **k: _FakeThread()
            ),
            mock.patch.object(
                instrument_api, "request_tuning_progress", lambda *a, **k: _FakeThread()
            ),
            mock.patch.object(
                instrument_api, "request_tuning_events", lambda *a, **k: _FakeThread()
            ),
        ]
        for patcher in self._network_patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)

    def make_page(self, *, run_id: str | None = None) -> TuningPage:
        page = TuningPage()
        page.resize(1116, 699)
        page.show()
        page._mapping = mapping(2)
        page._catalog = catalog()
        page._build_variable_rows()
        page._build_targets()
        page._run_id = run_id
        return page

    # ---------------------------------------------------------------- D7 下发
    def test_start_payload_carries_layering_and_budget_fields(self) -> None:
        page = self.make_page()
        page._rows[0]["check"].setCheckState(Qt.CheckState.Checked)
        page.loss_relative_spin.setValue(50.0)
        page.loss_hard_spin.setValue(80.0)
        page.max_invalid_spin.setValue(4)
        page.stall_fraction_form_spin.setValue(2.5)
        captured: dict = {}

        def fake_start(request: dict, base_url=None):  # noqa: ANN001
            captured.update(request)
            return _FakeThread()

        with mock.patch.object(instrument_api, "request_tuning_start", fake_start):
            page.start_tuning()

        self.assertAlmostEqual(captured["loss_relative"], 0.5)
        self.assertAlmostEqual(captured["loss_relative_hard"], 0.8)
        self.assertEqual(captured["max_invalid_rounds"], 4)
        self.assertAlmostEqual(captured["stall_fraction"], 0.025)

    def test_hard_threshold_is_omitted_when_set_to_disabled(self) -> None:
        page = self.make_page()
        page._rows[0]["check"].setCheckState(Qt.CheckState.Checked)
        captured: dict = {}

        def fake_start(request: dict, base_url=None):  # noqa: ANN001
            captured.update(request)
            return _FakeThread()

        with mock.patch.object(instrument_api, "request_tuning_start", fake_start):
            page.start_tuning()

        self.assertIsNone(captured["loss_relative_hard"])

    def test_thresholds_round_trip_through_the_saved_config(self) -> None:
        page = self.make_page()
        page.loss_hard_spin.setValue(75.0)
        page.max_invalid_spin.setValue(5)
        page.stall_fraction_form_spin.setValue(3.0)

        payload = page.tuning_config_payload()
        # 阈值按界面单位（%）存，停滞阈值按分数存——沿用既有配置文件的约定
        self.assertAlmostEqual(payload["loss_relative_hard"], 75.0)
        self.assertEqual(payload["max_invalid_rounds"], 5)
        self.assertAlmostEqual(payload["stall_fraction"], 0.03)

        page.loss_hard_spin.setValue(0.0)
        page.max_invalid_spin.setValue(1)
        page._apply_tuning_config(payload)

        self.assertAlmostEqual(page.loss_hard_spin.value(), 75.0)
        self.assertEqual(page.max_invalid_spin.value(), 5)

    def test_guard_summary_mentions_the_hard_threshold(self) -> None:
        page = self.make_page()

        page.loss_hard_spin.setValue(80.0)
        self.app.processEvents()

        summary = page.setting_sections["guard"].summary_label.text()
        self.assertIn("硬性丢失 80%", summary)
        self.assertIn("仅告警", summary)

    # ---------------------------------------------------------------- D15 ETA
    def test_eta_uses_the_measured_median_round_duration(self) -> None:
        page = self.make_page(run_id="run-1")
        page._iterations = [
            {**iteration(0, {MAGNET: 180.0}, 8.5), "duration_ms": 20000},
            {**iteration(1, {MAGNET: 190.0}, 9.5), "duration_ms": 24000},
            {**iteration(2, {MAGNET: 195.0}, 9.9), "duration_ms": 22000},
        ]

        self.assertAlmostEqual(page._seconds_per_round(), 22.0)

        page._apply_status({"state": "running", "completed_iterations": 3,
                            "max_iterations": 5})

        self.assertIn("分钟", page.eta_card.value_label.text())

    def test_eta_falls_back_to_the_static_estimate_without_measurements(self) -> None:
        page = self.make_page(run_id="run-1")
        page.settle_spin.setValue(20.0)
        page.hold_spin.setValue(0.0)

        self.assertAlmostEqual(page._seconds_per_round(), 20.1, places=2)

    # ---------------------------------------------------------------- D10 回读
    def test_readbacks_are_refreshed_but_throttled(self) -> None:
        page = self.make_page(run_id="run-1")
        calls: list[int] = []
        with mock.patch.object(page, "_refresh_readbacks", lambda: calls.append(1)):
            page._maybe_refresh_readbacks()
            page._maybe_refresh_readbacks()
            page._last_readback_at = 0.0
            page._maybe_refresh_readbacks()

        self.assertEqual(len(calls), 2, "同一秒内不该反复读设备")

    # ---------------------------------------------------------------- D6 认领
    def test_adopts_a_run_started_by_another_session(self) -> None:
        page = self.make_page()

        with mock.patch.object(page, "_poll") as poll:
            page._on_runs_listed(
                {
                    "ok": True,
                    "payload": {
                        "runs": [
                            {"run_id": "old-run", "state": "completed"},
                            {"run_id": "live-run", "state": "awaiting_confirmation"},
                        ]
                    },
                }
            )

        self.assertEqual(page._run_id, "live-run")
        self.assertTrue(page.tabs.isTabEnabled(1))
        self.assertTrue(page.tuning_stop_button.isEnabled())
        self.assertIn("接管", page.tuning_state.text())
        poll.assert_called_once()
        page._timer.stop()

    def test_does_not_adopt_a_finished_run(self) -> None:
        page = self.make_page()

        page._on_runs_listed(
            {"ok": True, "payload": {"runs": [{"run_id": "old-run", "state": "completed"}]}}
        )

        self.assertIsNone(page._run_id)

    def test_adoption_is_attempted_only_once(self) -> None:
        page = self.make_page()
        page._adopt_attempted = False  # show() 已经试过一次
        stub = mock.MagicMock(return_value=_FakeThread())
        with mock.patch.object(instrument_api, "request_tuning_runs", stub):
            page._adopt_active_run()
            page._adopt_active_run()

        self.assertEqual(stub.call_count, 1)


if __name__ == "__main__":
    unittest.main()
