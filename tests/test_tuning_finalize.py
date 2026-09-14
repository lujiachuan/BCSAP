"""结束后设备处置：应用最优 / 恢复启动前 / 回安全值。

三种动作都会写设备，所以断言围绕三件事：
**必须有依据**（没有最优轮/没有快照就明确拒绝，不猜）、
**必须真的到位**（看设备实际回读，不只看返回 ok）、
**必须留痕**（结果落库，含逐路实际回读）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from apps.instrument_service.device_locks import DeviceLockManager
from apps.instrument_service.pv_health import create_simulated_gateway
from apps.instrument_service.signal_io import SignalWriteService
from apps.instrument_service.tuning_service import TuningError, TuningService
from apps.instrument_service.tuning_store import TuningStore
from packages.contracts import SignalWriteRequest
from tests.test_tuning_service import MAGNET, MAGNET_RB, TARGET, build_config, request

SAFE_MAGNET = 0.0  # 测试映射里磁铁的下限


class TuningFinalizeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.store = TuningStore(Path(self._directory.name))
        self.locks = DeviceLockManager()
        self.config = build_config()
        self.gateway = create_simulated_gateway(self.config)
        self.signals = SignalWriteService(self.gateway, self.config, sleep=lambda _s: None)
        self.service = TuningService(
            lambda: self.signals, self.locks, self.store, sleep=lambda _s: None
        )

    def tearDown(self) -> None:
        self._directory.cleanup()

    def run_one_round(self) -> str:
        """启动并把唯一一轮确认执行完，返回 run_id。"""
        self.gateway._values[MAGNET_RB] = (150.0, "A")
        self.gateway._values[TARGET] = (10.0, "nA")
        run_id = self.service.start(request(max_iterations=1)).run_id
        self.service.approve(run_id)
        return run_id

    # ---------------- 必须有依据 ----------------
    def test_confirm_is_required(self) -> None:
        run_id = self.run_one_round()

        with self.assertRaises(TuningError) as caught:
            self.service.finalize(run_id, "apply_best", confirm=False)

        self.assertIn("二次确认", str(caught.exception))

    def test_unknown_action_is_rejected(self) -> None:
        run_id = self.run_one_round()

        with self.assertRaises(TuningError):
            self.service.finalize(run_id, "explode", confirm=True)

    def test_action_on_a_running_task_is_rejected(self) -> None:
        self.gateway._values[TARGET] = (10.0, "nA")
        run_id = self.service.start(request()).run_id  # 停在等待确认

        with self.assertRaises(TuningError) as caught:
            self.service.finalize(run_id, "safe_values", confirm=True)

        self.assertIn("还没结束", str(caught.exception))

    def test_action_while_recovery_is_pending_is_rejected(self) -> None:
        """设备实际状态未知时不许再写：必须先人工确认。"""
        run_id = self.run_one_round()
        self.service._locks.acquire({"磁铁电源"}, owner=run_id)
        run = self.service._require(run_id)
        run.state = type(run.state).RECOVERY_REQUIRED

        with self.assertRaises(TuningError) as caught:
            self.service.finalize(run_id, "safe_values", confirm=True)

        self.assertIn("恢复待确认", str(caught.exception))

    def test_apply_best_without_a_best_round_is_refused(self) -> None:
        self.gateway._values[TARGET] = (10.0, "nA")
        run_id = self.service.start(request()).run_id
        self.service.stop(run_id)  # 一轮都没跑就停

        with self.assertRaises(TuningError) as caught:
            self.service.finalize(run_id, "apply_best", confirm=True)

        self.assertIn("最优轮", str(caught.exception))

    # ---------------- 真的到位 ----------------
    def test_restore_initial_puts_the_device_back_and_reports_readbacks(self) -> None:
        run_id = self.run_one_round()
        # 先把设备挪到别处（不能假设候选一定偏离快照：无观测时 GP+EI 可能正好落在中点）
        self.signals.write(SignalWriteRequest(signal=MAGNET, value=SAFE_MAGNET))
        self.assertEqual(self.gateway.read(MAGNET_RB).value, SAFE_MAGNET)

        result = self.service.finalize(run_id, "restore_initial", confirm=True)

        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.applied[MAGNET], 150.0)
        self.assertEqual(self.gateway.read(MAGNET_RB).value, 150.0)

    def test_apply_best_writes_the_best_round_values(self) -> None:
        run_id = self.run_one_round()
        best = self.service.status(run_id).best_values
        self.assertTrue(best)
        # 先把设备挪开，确认"应用最优"真的把它写回来
        self.signals.write(SignalWriteRequest(signal=MAGNET, value=SAFE_MAGNET))

        result = self.service.finalize(run_id, "apply_best", confirm=True)

        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.applied[MAGNET], best[MAGNET])
        self.assertEqual(self.gateway.read(MAGNET_RB).value, best[MAGNET])

    def test_safe_values_go_to_the_mapping_lower_bound(self) -> None:
        run_id = self.run_one_round()

        result = self.service.finalize(run_id, "safe_values", confirm=True)

        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.applied[MAGNET], SAFE_MAGNET)
        self.assertEqual(self.gateway.read(MAGNET_RB).value, SAFE_MAGNET)

    def test_device_is_released_after_the_action(self) -> None:
        """处置动作自己申请、自己释放设备组：不能把锁留在服务里。"""
        run_id = self.run_one_round()

        self.service.finalize(run_id, "safe_values", confirm=True)

        self.assertEqual(self.locks.held(), {})

    def test_action_is_refused_while_another_task_holds_the_device(self) -> None:
        run_id = self.run_one_round()
        self.locks.acquire({"磁铁电源"}, owner="someone-else")

        with self.assertRaises(TuningError) as caught:
            self.service.finalize(run_id, "safe_values", confirm=True)

        self.assertIn("设备组当前不可用", str(caught.exception))
        self.assertEqual(self.locks.held(), {"磁铁电源": "someone-else"})

    def test_unsettled_readback_is_reported_as_a_failure_not_a_success(self) -> None:
        run_id = self.run_one_round()
        self.gateway._coupling = {}  # 回读不再跟随
        self.gateway._values[MAGNET_RB] = (120.0, "A")

        result = self.service.finalize(run_id, "restore_initial", confirm=True)

        self.assertFalse(result.ok)
        self.assertIn("未进入容差", result.detail)
        self.assertIn("未完成", result.message)

    # ---------------- 留痕 ----------------
    def test_result_is_persisted_and_visible_in_status(self) -> None:
        run_id = self.run_one_round()

        self.service.finalize(run_id, "safe_values", confirm=True)

        row = self.store.load_run(run_id)
        stored = json.loads(row["finalize_json"])
        self.assertEqual(stored["action"], "safe_values")
        self.assertTrue(stored["ok"])
        self.assertEqual(stored["applied"][MAGNET], SAFE_MAGNET)
        self.assertEqual(self.service.status(run_id).finalize.action, "safe_values")

    def test_a_second_action_overwrites_the_record_with_the_latest(self) -> None:
        """操作员可能先回安全值、再决定恢复初始：记录要反映最后做的事。"""
        run_id = self.run_one_round()

        self.service.finalize(run_id, "safe_values", confirm=True)
        self.service.finalize(run_id, "restore_initial", confirm=True)

        stored = json.loads(self.store.load_run(run_id)["finalize_json"])
        self.assertEqual(stored["action"], "restore_initial")
        self.assertEqual(self.gateway.read(MAGNET_RB).value, 150.0)


if __name__ == "__main__":
    unittest.main()
