"""束流丢失保护：启动前快照、异常判据、连续计数、回退与审计。

这些是**安全行为**：保护动作要么真的把设备退回去，要么明确告诉人"没退成功、请确认"。
所以断言不只看状态字符串，还要看设备实际回读与落库的审计记录。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from apps.instrument_service.device_locks import DeviceLockManager
from apps.instrument_service.pv_health import create_simulated_gateway
from apps.instrument_service.signal_io import SignalWriteService
from apps.instrument_service.tuning_service import TuningService
from apps.instrument_service.tuning_store import TuningStore
from tests.test_tuning_service import MAGNET, MAGNET_RB, TARGET, build_config, request


class TuningGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.store = TuningStore(Path(self._directory.name))
        self.locks = DeviceLockManager()

    def tearDown(self) -> None:
        self._directory.cleanup()

    def build(self) -> TuningService:
        self.config = build_config()
        self.gateway = create_simulated_gateway(self.config)
        self.signals = SignalWriteService(self.gateway, self.config, sleep=lambda _s: None)
        self.service = TuningService(
            lambda: self.signals, self.locks, self.store, sleep=lambda _s: None
        )
        return self.service

    def set_magnet(self, value: float) -> None:
        self.gateway._values[MAGNET_RB] = (value, "A")

    def set_target(self, value: float | None) -> None:
        """把目标（束流）摆到指定值；None = 这一路读不到。"""
        if value is None:
            self.gateway._values.pop(TARGET, None)
        else:
            self.gateway._values[TARGET] = (value, "nA")

    # ---------------- 启动前快照 ----------------
    def test_start_records_pre_run_snapshot_and_baseline(self) -> None:
        service = self.build()
        self.set_magnet(150.0)
        self.set_target(10.0)

        status = service.start(request())

        self.assertEqual(status.snapshot, {MAGNET: 150.0})
        self.assertEqual(status.baseline_objective, 10.0)

    def test_snapshot_is_not_polluted_by_the_first_iteration(self) -> None:
        """「初始回读」必须是启动前的值，不是第一轮执行后的回读。"""
        service = self.build()
        self.set_magnet(150.0)
        self.set_target(10.0)
        status = service.start(request())

        self.service.approve(status.run_id)

        after = service.status(status.run_id)
        self.assertEqual(after.snapshot, {MAGNET: 150.0})
        self.assertEqual(after.baseline_objective, 10.0)

    def test_start_is_refused_when_a_readback_cannot_be_read(self) -> None:
        """取不到快照就不开：没有快照就没有安全的退路。"""
        service = self.build()
        before = self.gateway.read(MAGNET).value
        self.gateway._values.pop(MAGNET_RB)

        status = service.start(request())

        self.assertEqual(status.state, "failed")
        self.assertIn("快照", status.message)
        self.assertEqual(self.gateway.read(MAGNET).value, before, "拒绝启动时不该动设备")

    def test_missing_baseline_still_starts_and_says_so(self) -> None:
        service = self.build()
        self.set_magnet(150.0)
        self.set_target(None)

        status = service.start(request())

        self.assertEqual(status.state, "awaiting_confirmation")
        self.assertIsNone(status.baseline_objective)
        self.assertIn("基线", status.snapshot_note)
        self.assertIn("绝对阈值", status.snapshot_note)

    # ---------------- 触发保护并回退 ----------------
    def test_absolute_loss_triggers_a_real_rollback(self) -> None:
        service = self.build()
        self.set_magnet(150.0)
        self.set_target(10.0)
        status = service.start(request(loss_absolute=1.0, loss_strikes=1))

        self.set_target(0.2)  # 本轮目标掉到近零
        after = service.approve(status.run_id)

        self.assertEqual(after.state, "aborted")
        self.assertIsNotNone(after.recovery)
        self.assertTrue(after.recovery.ok)
        self.assertIn("绝对阈值", after.recovery.reason)
        self.assertIn("保护", after.message)
        self.assertEqual(after.recovery.restored[MAGNET], 150.0)
        # 关键：设备真的退回去了，不只是"记了一笔"
        self.assertEqual(self.gateway.read(MAGNET_RB).value, 150.0)

    def test_relative_loss_triggers_a_rollback(self) -> None:
        service = self.build()
        self.set_magnet(150.0)
        self.set_target(10.0)
        status = service.start(request(loss_relative=0.5, loss_strikes=1))

        self.set_target(3.0)  # 低于基线一半
        after = service.approve(status.run_id)

        self.assertEqual(after.state, "aborted")
        self.assertTrue(after.recovery.ok)
        self.assertIn("基线", after.recovery.reason)

    def test_a_round_without_a_valid_target_counts_as_anomaly(self) -> None:
        service = self.build()
        self.set_magnet(150.0)
        self.set_target(10.0)
        status = service.start(request(loss_strikes=1))

        self.set_target(None)
        after = service.approve(status.run_id)

        self.assertIsNotNone(after.recovery)
        self.assertIn("没有有效目标读数", after.recovery.reason)

    def test_one_bad_round_is_not_enough_with_two_strikes(self) -> None:
        """单点毛刺不该把刚调好的参数全退回去。"""
        service = self.build()
        self.set_magnet(150.0)
        self.set_target(10.0)
        status = service.start(request(loss_absolute=1.0, loss_strikes=2))

        self.set_target(0.2)
        first = service.approve(status.run_id)

        self.assertEqual(first.state, "awaiting_confirmation")
        self.assertEqual(first.anomalies, 1)
        self.assertIsNone(first.recovery)

        second = service.approve(status.run_id)
        self.assertEqual(second.state, "aborted")
        self.assertTrue(second.recovery.ok)

    def test_a_good_round_resets_the_anomaly_counter(self) -> None:
        service = self.build()
        self.set_magnet(150.0)
        self.set_target(10.0)
        status = service.start(request(loss_absolute=1.0, loss_strikes=2))

        self.set_target(0.2)
        first = service.approve(status.run_id)
        self.assertEqual(first.anomalies, 1)

        self.set_target(10.0)  # 恢复正常
        second = service.approve(status.run_id)

        self.assertEqual(second.state, "awaiting_confirmation")
        self.assertEqual(second.anomalies, 0)

    def test_auto_recover_off_stops_and_waits_for_a_human(self) -> None:
        service = self.build()
        self.set_magnet(150.0)
        self.set_target(10.0)
        status = service.start(
            request(loss_absolute=1.0, loss_strikes=1, auto_recover=False)
        )

        self.set_target(0.2)
        after = service.approve(status.run_id)

        self.assertEqual(after.state, "aborted")
        self.assertIsNone(after.recovery, "不自动回退时不该有回退记录")
        self.assertIn("人工处理", after.message)

    def test_rollback_that_never_settles_keeps_the_device_and_asks_for_confirmation(self) -> None:
        service = self.build()
        self.set_magnet(150.0)
        self.set_target(10.0)
        status = service.start(request(loss_absolute=1.0, loss_strikes=1))

        # 回读不再跟随设定，而且当前值已经不等于快照 → 回退永远等不到容差
        self.gateway._coupling = {}
        self.set_magnet(120.0)
        self.set_target(0.2)
        after = service.approve(status.run_id)

        self.assertEqual(after.state, "recovery_required")
        self.assertIsNotNone(after.recovery)
        self.assertFalse(after.recovery.ok)
        self.assertIn("未进入容差", after.recovery.detail)
        self.assertNotEqual(self.locks.held(), {}, "退不回去时必须保留设备锁")

    # ---------------- 审计 ----------------
    def test_snapshot_and_rollback_are_persisted_for_audit(self) -> None:
        service = self.build()
        self.set_magnet(150.0)
        self.set_target(10.0)
        status = service.start(request(loss_absolute=1.0, loss_strikes=1))

        self.set_target(0.2)
        service.approve(status.run_id)

        row = self.store.load_run(status.run_id)
        snapshot = json.loads(row["snapshot_json"])
        recovery = json.loads(row["recovery_json"])
        self.assertEqual(snapshot[MAGNET], 150.0)
        self.assertEqual(row["baseline_objective"], 10.0)
        self.assertIn("绝对阈值", recovery["reason"])
        self.assertTrue(recovery["ok"])
        self.assertEqual(recovery["restored"][MAGNET], 150.0)


if __name__ == "__main__":
    unittest.main()
