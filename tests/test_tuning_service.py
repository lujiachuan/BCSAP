"""调束状态机：确认模式、执行层校验、失败语义与设备锁。

覆盖的都是安全相关行为：模式不能绕过执行层、范围必须在设备硬边界内、
写明中断要按「结果未知」处理并继续占着设备、停止后锁必须释放。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from apps.instrument_service.device_locks import DeviceLockManager
from apps.instrument_service.pv_health import create_simulated_gateway
from apps.instrument_service.signal_io import SignalWriteService
from apps.instrument_service.tuning_service import TuningError, TuningService
from apps.instrument_service.tuning_store import TuningStore
from packages.contracts import (
    PvMappingConfig,
    PvMappingEntry,
    TuningRunRequest,
    TuningVariable,
)

MAGNET = "magnet.m1.current_setpoint"
MAGNET_RB = "magnet.m1.current_readback"
TARGET = "detector.fc1.beam_current"


def entry(signal: str, pv: str, unit: str, **kwargs: object) -> PvMappingEntry:
    base = {
        "signal": signal, "label": signal, "pv": pv, "unit": unit,
        "writable": False, "required": False, "group": "磁铁电源",
        "role": "readback", "readback_signal": "",
    }
    base.update(kwargs)
    base.setdefault("tunable", bool(base["writable"] and base["role"] == "setpoint"))
    base.setdefault("beam_target", signal.endswith(".beam_current"))
    return PvMappingEntry(**base)  # type: ignore[arg-type]


def build_config(*, max_step: float | None = 100.0, max_value: float = 600.0):
    return PvMappingConfig(
        version=1,
        entries=[
            entry(
                MAGNET, "BD:DipoleMagnet:01:CurrentSet", "A",
                writable=True, role="setpoint", readback_signal=MAGNET_RB,
                min_value=0.0, max_value=max_value, max_step=max_step,
                settle_tol=0.5, settle_timeout=1.0,
            ),
            entry(MAGNET_RB, "BD:DipoleMagnet:01:CurrentMonitor", "A"),
            entry(
                TARGET, "BD:FC:01:BeamCurrent", "nA",
                group="束流探测", required=True,
            ),
        ],
    )


def request(**overrides: object) -> TuningRunRequest:
    base = {
        "target_signal": TARGET,
        "variables": [
            TuningVariable(signal=MAGNET, label="磁铁1 电流", low=100.0, high=200.0)
        ],
        "max_iterations": 3,
        "settle_timeout_s": 0.2,
        "samples_per_point": 2,
    }
    base.update(overrides)
    return TuningRunRequest(**base)  # type: ignore[arg-type]


class TuningServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.store = TuningStore(Path(self._directory.name))
        self.locks = DeviceLockManager()

    def tearDown(self) -> None:
        self._directory.cleanup()

    def build(self, config: PvMappingConfig | None = None) -> TuningService:
        self.config = config or build_config()
        self.signals = SignalWriteService(
            create_simulated_gateway(self.config), self.config, sleep=lambda _s: None
        )
        self.service = TuningService(
            lambda: self.signals, self.locks, self.store, sleep=lambda _s: None
        )
        return self.service

    # ---------------- 确认模式 ----------------
    def test_start_waits_for_confirmation_and_proposes_candidate(self) -> None:
        service = self.build()

        status = service.start(request())

        self.assertEqual(status.state, "awaiting_confirmation")
        self.assertIsNotNone(status.pending)
        value = status.pending.values[MAGNET]
        self.assertGreaterEqual(value, 100.0)
        self.assertLessEqual(value, 200.0)

    def test_start_records_algorithm_version_and_seed(self) -> None:
        """文档要求保存算法版本与随机种子，否则调束结果不可复现。"""
        service = self.build()

        status = service.start(request(seed=17))

        self.assertEqual(status.algorithm, "gp-ei")
        self.assertTrue(status.algorithm_version)
        self.assertEqual(status.seed, 17)

    def test_approve_writes_device_and_records_iteration(self) -> None:
        service = self.build()
        status = service.start(request())

        after = service.approve(status.run_id)

        self.assertEqual(after.completed_iterations, 1)
        iterations = service.iterations(status.run_id).iterations
        self.assertEqual(len(iterations), 1)
        record = iterations[0]
        # 候选、实际下发、实际回读、目标测量都要分开记录
        self.assertIn(MAGNET, record.proposed)
        self.assertIn(MAGNET, record.applied)
        self.assertIn(MAGNET, record.readback)
        self.assertIsNotNone(record.objective)
        self.assertEqual(record.quality, "ok")
        # 写设备之后，设定值应等于实际下发值
        self.assertAlmostEqual(
            self.signals.read_value(MAGNET), record.applied[MAGNET], places=6
        )

    def test_approve_then_next_proposal_until_completed(self) -> None:
        service = self.build()
        run_id = service.start(request(max_iterations=2)).run_id

        service.approve(run_id)
        mid = service.status(run_id)
        self.assertEqual(mid.state, "awaiting_confirmation")

        final = service.approve(run_id)
        self.assertEqual(final.state, "completed")
        self.assertEqual(final.completed_iterations, 2)
        self.assertEqual(self.locks.held(), {})

    def test_approve_without_pending_candidate_is_rejected(self) -> None:
        service = self.build()
        run_id = service.start(request(max_iterations=1)).run_id
        service.approve(run_id)

        with self.assertRaises(TuningError):
            service.approve(run_id)

    def test_iterations_are_persisted(self) -> None:
        service = self.build()
        run_id = service.start(request()).run_id
        service.approve(run_id)

        stored = self.store.load_iterations(run_id)

        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].quality, "ok")
        run_row = self.store.load_run(run_id)
        self.assertEqual(run_row["algorithm"], "gp-ei")

    # ---------------- 模式 ----------------
    def test_only_confirm_mode_is_accepted(self) -> None:
        """连续自动写入需另行通过安全评审，第一版必须显式拒绝。"""
        service = self.build()

        for mode in ("auto", "continuous", "whatever"):
            with self.subTest(mode=mode):
                with self.assertRaises(TuningError) as caught:
                    service.start(request(mode=mode))
                self.assertIn("6.6", str(caught.exception))

    # ---------------- 执行层校验 ----------------
    def test_variable_range_above_device_limit_is_rejected(self) -> None:
        service = self.build(build_config(max_value=150.0))

        with self.assertRaises(TuningError) as caught:
            service.start(request())

        self.assertIn("超过设备允许最大值", str(caught.exception))

    def test_variable_range_below_device_limit_is_rejected(self) -> None:
        config = build_config()
        config.entries[0] = config.entries[0].model_copy(update={"min_value": 120.0})
        service = self.build(config)

        with self.assertRaises(TuningError) as caught:
            service.start(request())

        self.assertIn("低于设备允许最小值", str(caught.exception))

    def test_missing_max_step_is_rejected(self) -> None:
        """没有最大单步约束的调束可能一次大跳变毁掉束流。"""
        service = self.build(build_config(max_step=None))

        with self.assertRaises(TuningError) as caught:
            service.start(request())

        self.assertIn("max_step", str(caught.exception))

    def test_read_only_signal_cannot_be_a_variable(self) -> None:
        service = self.build()

        with self.assertRaises(TuningError):
            service.start(
                request(variables=[TuningVariable(signal=TARGET, low=0.0, high=1.0)])
            )

    def test_variable_without_tuning_capability_is_rejected(self) -> None:
        config = build_config()
        config.entries[0] = config.entries[0].model_copy(update={"tunable": False})
        service = self.build(config)

        with self.assertRaises(TuningError) as caught:
            service.start(request())

        self.assertIn("未允许", str(caught.exception))

    def test_target_without_beam_capability_is_rejected(self) -> None:
        config = build_config()
        config.entries[-1] = config.entries[-1].model_copy(update={"beam_target": False})
        service = self.build(config)

        with self.assertRaises(TuningError) as caught:
            service.start(request())

        self.assertIn("调束目标", str(caught.exception))

    def test_writable_signal_cannot_be_the_target(self) -> None:
        service = self.build()

        with self.assertRaises(TuningError) as caught:
            service.start(request(target_signal=MAGNET))

        self.assertIn("只读", str(caught.exception))

    def test_unknown_signal_is_rejected(self) -> None:
        service = self.build()

        with self.assertRaises(TuningError):
            service.start(request(target_signal="no.such.signal"))

    def test_empty_variable_set_is_rejected(self) -> None:
        service = self.build()

        with self.assertRaises(TuningError):
            service.start(request(variables=[]))

    # ---------------- 失败语义 ----------------
    def test_write_failure_marks_recovery_required_and_keeps_lock(self) -> None:
        service = self.build()
        run_id = service.start(request()).run_id

        class Exploding:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def write(self, signal, value, command_id):
                raise OSError("CA 写入超时")

        self.signals._gateway = Exploding(self.signals._gateway)

        status = service.approve(run_id)

        self.assertEqual(status.state, "recovery_required")
        self.assertNotEqual(self.locks.held(), {})

    def test_acknowledge_releases_recovery_lock(self) -> None:
        service = self.build()
        run_id = service.start(request()).run_id

        class Exploding:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def write(self, signal, value, command_id):
                raise OSError("CA 写入超时")

        self.signals._gateway = Exploding(self.signals._gateway)
        service.approve(run_id)

        service.acknowledge_recovery(run_id, note="现场确认磁场已回零")

        self.assertEqual(self.locks.held(), {})

    def test_unreadable_target_is_recorded_as_read_failed(self) -> None:
        from packages.epics_adapter import SimulatedEpicsGateway

        config = build_config()
        gateway = SimulatedEpicsGateway(
            {MAGNET: (150.0, "A"), MAGNET_RB: (150.0, "A")},
            coupling={MAGNET: MAGNET_RB},
        )
        signals = SignalWriteService(gateway, config, sleep=lambda _s: None)
        service = TuningService(
            lambda: signals, self.locks, self.store, sleep=lambda _s: None
        )
        run_id = service.start(request()).run_id

        service.approve(run_id)

        record = service.iterations(run_id).iterations[0]
        self.assertEqual(record.quality, "read_failed")
        self.assertIsNone(record.objective)

    # ---------------- 停止 ----------------
    def test_stop_while_awaiting_confirmation_aborts_and_frees_locks(self) -> None:
        service = self.build()
        run_id = service.start(request()).run_id

        status = service.stop(run_id)

        self.assertEqual(status.state, "aborted")
        self.assertIsNone(status.pending)
        self.assertEqual(self.locks.held(), {})

    def test_stop_after_completion_is_a_noop(self) -> None:
        service = self.build()
        run_id = service.start(request(max_iterations=1)).run_id
        service.approve(run_id)

        self.assertEqual(service.stop(run_id).state, "completed")

    # ---------------- 并发与锁 ----------------
    def test_second_run_is_rejected_while_one_is_active(self) -> None:
        service = self.build()
        service.start(request())

        with self.assertRaises(TuningError):
            service.start(request())

    def test_scan_lock_blocks_tuning(self) -> None:
        service = self.build()
        self.locks.acquire({"磁铁电源"}, owner="扫谱任务")

        status = service.start(request())

        self.assertEqual(status.state, "failed")
        self.assertIn("设备组不可用", status.message)

    def test_locks_are_held_while_awaiting_confirmation(self) -> None:
        """等确认期间设备仍被占用：否则扫谱可以在调束中途插进来动同一路磁场。"""
        service = self.build()

        status = service.start(request())

        self.assertNotEqual(self.locks.held(), {})
        # 状态里也应如实反映「正占着」
        self.assertTrue(status.locked_groups)

    def test_locked_groups_is_empty_after_completion(self) -> None:
        """终态后不得再报占用：否则诊断端会以为设备还锁着而不敢启动新任务。"""
        service = self.build()
        run_id = service.start(request(max_iterations=1)).run_id
        self.assertTrue(service.status(run_id).locked_groups)

        final = service.approve(run_id)

        self.assertEqual(final.state, "completed")
        self.assertEqual(final.locked_groups, [])
        self.assertEqual(self.locks.held(), {})


if __name__ == "__main__":
    unittest.main()
