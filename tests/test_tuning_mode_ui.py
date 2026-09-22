"""S5 客户端界面：引擎/模式/patience 控件、auto 保护、暂停继续、auto pending UI。

覆盖点：默认模式与引擎（auto + TPE，对应"全改、用更好的"决策）；auto 模式在
界面侧就拦下"没开束流保护"的启动；confirm/auto 两种模式在状态回填后的按钮
行为；暂停状态的继续按钮可见性。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from apps.desktop_client import instrument_api
from apps.desktop_client.pages.tuning import TuningPage


class _SignalStub:
    def __init__(self, payload: dict | None = None) -> None:
        self._payload = payload
        self._callbacks: list = []

    def connect(self, callback, *_args, **_kwargs) -> None:
        self._callbacks.append(callback)
        if self._payload is not None:
            callback(self._payload)


class _FakeThread:
    def __init__(self, *_args, completed_payload: dict | None = None, **_kwargs) -> None:
        self.completed = _SignalStub(completed_payload)

    def start(self) -> None:
        return None


def marked_mapping() -> list[dict]:
    return [
        {
            "signal": "magnet.m1.current_setpoint", "label": "磁铁1 电流",
            "unit": "A", "writable": True, "role": "setpoint",
            "readback_signal": "magnet.m1.current_readback",
            "tunable": True, "beam_target": False,
            "min_value": 0.0, "max_value": 600.0, "max_step": 10.0,
        },
        {
            "signal": "detector.fc1.beam_current", "label": "FC1", "unit": "nA",
            "writable": False, "role": "readback", "tunable": False,
            "beam_target": True,
        },
    ]


class TuningModeEngineUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._read_patch = mock.patch.object(
            instrument_api, "request_read", lambda *a, **k: _FakeThread()
        )
        self._read_patch.start()

    def tearDown(self) -> None:
        self._read_patch.stop()
        patch = getattr(self, "_start_patch", None)
        if patch is not None:
            patch.stop()

    def ready_page(self) -> TuningPage:
        page = TuningPage()
        page._mapping = marked_mapping()
        page._build_variable_rows()
        page._build_targets()
        page._rows[0]["check"].setCheckState(Qt.CheckState.Checked)
        page.loss_relative_spin.setValue(50.0)
        return page

    def capture_start(self, page: TuningPage) -> dict:
        captured: dict = {}

        def fake(request: dict, base_url=None):  # noqa: ANN001
            captured.update(request)
            return _FakeThread()

        self._start_patch = mock.patch.object(
            instrument_api, "request_tuning_start", fake
        )
        self._start_patch.start()
        return captured

    # ---------------- 默认值与请求体 ----------------
    def test_default_mode_is_auto_and_engine_is_cmaes(self) -> None:
        page = self.ready_page()

        self.assertEqual(page.mode_combo.currentData(), "auto")
        self.assertEqual(page.engine_combo.currentData(), "cmaes")

    def test_start_sends_engine_mode_patience(self) -> None:
        page = self.ready_page()
        page.engine_combo.setCurrentIndex(
            page.engine_combo.findData("cmaes")
        )
        page.patience_spin.setValue(7)
        captured = self.capture_start(page)

        page.start_tuning()

        self.assertEqual(captured["mode"], "auto")
        self.assertEqual(captured["engine"], "cmaes")
        self.assertEqual(captured["patience"], 7)

    # ---------------- auto 保护 ----------------
    def test_auto_mode_without_beam_protection_is_blocked_locally(self) -> None:
        page = self.ready_page()
        page.loss_absolute_check.setChecked(False)
        page.loss_relative_spin.setValue(0.0)
        captured = self.capture_start(page)

        with mock.patch.object(page, "_complain") as complain:
            page.start_tuning()

        complain.assert_called_once()
        self.assertIn("束流丢失保护", complain.call_args[0][0])
        self.assertEqual(captured, {})

    def test_auto_mode_with_absolute_threshold_starts(self) -> None:
        page = self.ready_page()
        page.loss_relative_spin.setValue(0.0)
        page.loss_absolute_check.setChecked(True)
        page.loss_absolute_spin.setValue(1.0)
        captured = self.capture_start(page)

        page.start_tuning()

        self.assertEqual(captured["mode"], "auto")
        self.assertEqual(captured["loss_absolute"], 1.0)

    # ---------------- 状态回填后的按钮行为 ----------------
    def _enable_monitor_tab(self, page: TuningPage) -> None:
        """真实运行时 _on_started 会先启用监控页；测试要模拟这一步，
        否则按钮在禁用的 tab 里 isEnabled() 恒为 False（Qt 沿父链计算）。"""
        page.tabs.setTabEnabled(1, True)

    def test_auto_mode_pending_disables_approve_button(self) -> None:
        page = self.ready_page()
        page._run_id = "run-1"
        self._enable_monitor_tab(page)

        page._apply_status(
            {
                "state": "running",
                "mode": "auto",
                "completed_iterations": 1,
                "max_iterations": 6,
                "pending": {
                    "iteration": 1,
                    "values": {"magnet.m1.current_setpoint": 150.0},
                    "predicted": 8.0,
                    "std": 0.5,
                    "active_signals": ["magnet.m1.current_setpoint"],
                },
            }
        )

        self.assertFalse(page.approve_button.isEnabled())
        self.assertIn("自动执行中", page.proposal_label.text())

    def test_confirm_mode_pending_enables_approve_button(self) -> None:
        page = self.ready_page()
        page._run_id = "run-1"
        self._enable_monitor_tab(page)

        page._apply_status(
            {
                "state": "awaiting_confirmation",
                "mode": "confirm",
                "completed_iterations": 1,
                "max_iterations": 6,
                "pending": {
                    "iteration": 1,
                    "values": {"magnet.m1.current_setpoint": 150.0},
                    "predicted": 8.0,
                    "std": 0.5,
                    "active_signals": ["magnet.m1.current_setpoint"],
                },
            }
        )

        self.assertTrue(page.approve_button.isEnabled())
        self.assertIn("待确认候选", page.proposal_label.text())

    def test_paused_state_shows_resume_button_and_disables_pause(self) -> None:
        page = self.ready_page()
        page._run_id = "run-1"
        self._enable_monitor_tab(page)

        page._apply_status(
            {
                "state": "paused",
                "mode": "auto",
                "completed_iterations": 2,
                "max_iterations": 6,
            }
        )

        # isVisible() 要求窗口已 show（offscreen 下恒 False），用 isHidden 断言可见性
        self.assertFalse(page.tuning_resume_button.isHidden())
        self.assertTrue(page.tuning_resume_button.isEnabled())
        self.assertFalse(page.tuning_pause_button.isEnabled())
        self.assertIn("已暂停", page.proposal_label.text())

    def test_running_state_enables_pause_button(self) -> None:
        page = self.ready_page()
        page._run_id = "run-1"
        self._enable_monitor_tab(page)

        page._apply_status(
            {
                "state": "running",
                "mode": "auto",
                "completed_iterations": 1,
                "max_iterations": 6,
            }
        )

        self.assertTrue(page.tuning_pause_button.isEnabled())
        self.assertFalse(page.tuning_resume_button.isVisible())

    def test_terminal_state_disables_pause_and_resume(self) -> None:
        page = self.ready_page()
        page._run_id = "run-1"
        self._enable_monitor_tab(page)

        page._apply_status(
            {
                "state": "completed",
                "mode": "auto",
                "completed_iterations": 3,
                "max_iterations": 6,
            }
        )

        self.assertFalse(page.tuning_pause_button.isEnabled())
        self.assertFalse(page.tuning_resume_button.isEnabled())
        self.assertFalse(page.tuning_resume_button.isVisible())


if __name__ == "__main__":
    unittest.main()
