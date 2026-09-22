"""调束逻辑修复的针对性测试（改进方案批次 3：D1–D16）。

每条都对着一个**具体缺陷**，而不是笼统的"跑一遍看看"：

* D1 快照必须早于"开始前复位"——否则"恢复启动前参数"退的是复位点；
* D2/D3 保护判据分层——探索下探与写入被拒不许算成束流丢失；
* D4 阶段推进只看有效轮；
* D5 连续无效轮次要有上限；
* D11/D16 设备处置的取值来源必须可查、不许静默按 0 下发；
* D15 每轮耗时用实测中位数，而不是"稳定超时"这个上限。
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
from packages.contracts import PvMappingConfig
from packages.contracts.tuning import (
    ACTION_APPLY_BEST,
    ACTION_SAFE_VALUES,
    TuningIteration,
    TuningProposal,
    TuningVariable,
)
from tests.test_tuning_service import (
    MAGNET,
    MAGNET_RB,
    TARGET,
    build_config,
    entry,
    request,
)

FOCUS = "ion_optics.focus.voltage_setpoint"
FOCUS_RB = "ion_optics.focus.voltage_readback"


def two_variable_config() -> PvMappingConfig:
    """两路可调变量（磁铁 + 聚焦），用于阶段推进类用例。"""
    return PvMappingConfig(
        version=1,
        entries=[
            entry(
                MAGNET, "BD:DipoleMagnet:01:CurrentSet", "A",
                writable=True, role="setpoint", readback_signal=MAGNET_RB,
                min_value=0.0, max_value=600.0, max_step=100.0,
                settle_tol=0.5, settle_timeout=1.0,
            ),
            entry(MAGNET_RB, "BD:DipoleMagnet:01:CurrentMonitor", "A"),
            entry(
                FOCUS, "Part1:JM_POWER:03:SET_VOL", "V",
                writable=True, role="setpoint", readback_signal=FOCUS_RB,
                min_value=0.0, max_value=4000.0, max_step=200.0,
                settle_tol=5.0, settle_timeout=1.0, group="聚焦/漂移管",
            ),
            entry(FOCUS_RB, "Part1:JM_POWER:03:READ_VOL", "V", group="聚焦/漂移管"),
            entry(
                TARGET, "BD:FC:01:BeamCurrent", "nA",
                group="束流探测", required=True,
            ),
        ],
    )


class _ServiceHarness(unittest.TestCase):
    """可控制目标读数的 TuningService（SIM 的 fc1 是算出来的，写 `_values` 不生效）。"""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.directory = Path(self._directory.name)
        self.locks = DeviceLockManager()

    def tearDown(self) -> None:
        import shutil

        for run in getattr(self, "_service", None) and self._service._runs.values() or []:
            study = getattr(run.optimizer, "_study", None)
            if study is not None:
                try:
                    study._storage.close()
                except Exception:  # noqa: BLE001
                    pass
        shutil.rmtree(self.directory, ignore_errors=True)

    def build(self, config=None, *, target: float | None = 10.0) -> TuningService:
        self.config = config or build_config()
        self.gateway = create_simulated_gateway(self.config)
        self._target_value = target
        gateway = self.gateway
        real_read = gateway.read

        def read(signal: str):
            if signal == TARGET:
                if self._target_value is None:
                    raise KeyError(f"未配置模拟信号：{signal}")
                return gateway._reading(signal, float(self._target_value), "nA")
            return real_read(signal)

        gateway.read = read  # type: ignore[method-assign]
        self.signals = SignalWriteService(gateway, self.config, sleep=lambda _s: None)
        self._service = TuningService(
            lambda: self.signals, self.locks, TuningStore(self.directory),
            sleep=lambda _s: None,
        )
        return self._service

    def set_target(self, value: float | None) -> None:
        self._target_value = None if value is None else float(value)

    def set_device(self, value: float) -> None:
        self.gateway._values[MAGNET_RB] = (value, "A")


class SnapshotOrderTests(_ServiceHarness):
    """D1：启动前快照必须先于"开始前复位"。"""

    def test_snapshot_is_taken_before_the_reset_write(self) -> None:
        service = self.build()
        self.set_device(150.0)  # 任务开始前设备在 150
        variables = [
            TuningVariable(signal=MAGNET, label="磁铁1", low=100.0, high=200.0, start=120.0)
        ]

        status = service.start(
            request(variables=variables, reset_before_start=True, loss_protection=False)
        )

        # 快照 = 复位**之前**的实际回读
        self.assertEqual(status.snapshot, {MAGNET: 150.0})
        # 设备已经被复位到起始值
        self.assertAlmostEqual(float(self.gateway.read(MAGNET_RB).value), 120.0, places=3)
        messages = [event.message for event in service.events(status.run_id).events]
        self.assertTrue(any("启动前快照" in text for text in messages))
        self.assertTrue(any("复位完成" in text for text in messages))

    def test_restore_initial_returns_to_the_true_pre_run_position(self) -> None:
        """复位之后再"恢复启动前参数"，必须回到 150 而不是复位点 120。"""
        service = self.build()
        self.set_device(150.0)
        variables = [
            TuningVariable(signal=MAGNET, label="磁铁1", low=100.0, high=200.0, start=120.0)
        ]
        status = service.start(
            request(
                variables=variables, reset_before_start=True, loss_protection=False,
                max_iterations=1,
            )
        )
        service.approve(status.run_id)
        service.stop(status.run_id)

        result = service.finalize(status.run_id, "restore_initial", confirm=True)

        self.assertTrue(result.ok, result.detail)
        self.assertAlmostEqual(float(self.gateway.read(MAGNET_RB).value), 150.0, places=3)


class ProtectionLayeringTests(_ServiceHarness):
    """D2/D3：软/硬阈值分层，写入被拒不进异常计数。"""

    def _guard(self, service, run_id, *, objective, quality="ok", predicted=0.0, std=1.0):
        run = service._require(run_id)
        iteration = TuningIteration(
            iteration=0, proposed={MAGNET: 150.0}, applied={MAGNET: 150.0},
            readback={MAGNET: 150.0}, target=objective, objective=objective,
            quality=quality, at="2026-09-22T10:00:00.000000+00:00",
        )
        proposal = TuningProposal(
            iteration=0, values={MAGNET: 150.0}, predicted=predicted, std=std,
            expected_improvement=0.0, at="2026-09-22T10:00:00.000000+00:00",
        )
        return service._guard_reason(run, proposal, iteration), run

    def test_shallow_dip_between_soft_and_hard_is_only_a_warning(self) -> None:
        service = self.build()
        self.set_device(150.0)
        self.set_target(10.0)
        status = service.start(
            request(loss_relative=0.5, loss_relative_hard=0.8, loss_strikes=1)
        )

        reason, run = self._guard(service, status.run_id, objective=4.0)  # 下降 60%

        self.assertIsNone(reason, "软硬阈值之间的下探不该判为丢失")
        self.assertIn("仅告警", run.last_soft_warning)
        self.assertIn("探索下探", run.last_soft_warning)

    def test_deep_drop_below_the_hard_threshold_trips(self) -> None:
        service = self.build()
        self.set_device(150.0)
        self.set_target(10.0)
        status = service.start(
            request(loss_relative=0.5, loss_relative_hard=0.8, loss_strikes=1)
        )

        reason, _run = self._guard(service, status.run_id, objective=1.5)  # 下降 85%

        self.assertIsNotNone(reason)
        self.assertIn("硬阈值", reason)

    def test_write_rejected_round_is_not_a_beam_anomaly(self) -> None:
        service = self.build()
        self.set_device(150.0)
        self.set_target(0.5)  # 目标很低，若无差别处理就会判丢失
        status = service.start(request(loss_absolute=1.0, loss_strikes=1))

        reason, _run = self._guard(
            service, status.run_id, objective=None, quality="write_rejected"
        )

        self.assertIsNone(reason, "写入被拒是执行层问题，设备根本没动，不该算束流丢失")

    def test_expected_dip_matching_the_model_prediction_is_only_a_warning(self) -> None:
        """模型**事先**说这里会低、测出来真的低：这是探索，不是丢失。"""
        service = self.build()
        self.set_device(150.0)
        self.set_target(10.0)
        status = service.start(request(loss_relative=0.5, loss_strikes=1))
        run = service._require(status.run_id)
        run.prior_observations = 3  # 模型已有观测

        reason, run = self._guard(
            service, status.run_id, objective=4.0, predicted=4.2, std=0.3
        )

        self.assertIsNone(reason)
        self.assertIn("模型预测", run.last_soft_warning)

    def test_first_round_never_uses_the_prediction_exemption(self) -> None:
        """首轮先验没有信息量：它"预测"出任何值都不构成预期内的下探。"""
        service = self.build()
        self.set_device(150.0)
        self.set_target(10.0)
        status = service.start(request(loss_relative=0.5, loss_strikes=1))

        reason, _run = self._guard(
            service, status.run_id, objective=4.0, predicted=4.2, std=0.3
        )

        self.assertIsNotNone(reason, "首轮没有观测，不能用模型预测给自己开脱")


class ValidRoundTests(_ServiceHarness):
    """D4/D5：阶段推进只看有效轮；连续无效轮次有上限。"""

    def test_invalid_round_does_not_advance_the_stage(self) -> None:
        service = self.build(two_variable_config())
        self.set_device(150.0)
        self.set_target(10.0)
        variables = [
            TuningVariable(signal=MAGNET, label="磁铁1", low=100.0, high=200.0),
            TuningVariable(signal=FOCUS, label="聚焦", low=0.0, high=4000.0),
        ]
        status = service.start(
            request(
                variables=variables, strategy="sequential_then_joint",
                calls_per_variable=2, max_iterations=12, loss_protection=False,
            )
        )
        run = service._require(status.run_id)

        service.approve(status.run_id)  # 第 1 轮有效
        self.assertEqual(run.stage_rounds, 1)

        self.set_target(None)  # 第 2 轮读不到目标 → 无效
        service.approve(status.run_id)
        self.assertEqual(run.stage_rounds, 1, "无效轮不该被算成阶段进展")
        self.assertEqual(run.invalid_streak, 1)

        self.set_target(11.0)  # 第 3 轮有效
        service.approve(status.run_id)
        self.assertEqual(run.stage_rounds, 2)
        self.assertEqual(run.invalid_streak, 0)

    def test_consecutive_invalid_rounds_stop_the_task(self) -> None:
        service = self.build()
        self.set_device(150.0)
        self.set_target(None)  # 一直读不到目标
        status = service.start(
            request(
                loss_protection=False, max_invalid_rounds=2, max_iterations=10,
            )
        )

        service.approve(status.run_id)
        after = service.approve(status.run_id)

        self.assertEqual(after.state, "failed")
        self.assertIn("有效测量", after.message)
        self.assertEqual(
            len(service.events(status.run_id).events) > 0, True,
        )
        messages = " ".join(
            event.message for event in service.events(status.run_id).events
        )
        self.assertIn("连续 2 轮无效", messages)


class FinalizeSourceTests(_ServiceHarness):
    """D11/D16：处置取值来源可查；没有依据时明确拒绝。"""

    def test_apply_best_falls_back_to_snapshot_and_records_the_source(self) -> None:
        service = self.build()
        self.set_device(150.0)
        self.set_target(10.0)
        status = service.start(request(loss_protection=False, max_iterations=2))
        service.approve(status.run_id)
        service.stop(status.run_id)
        run = service._require(status.run_id)
        # 人为制造"最优轮缺这一路回读"：只剩启动前快照可依
        run.best_values = {MAGNET_RB: 150.0}

        targets, sources = service._finalize_targets(run, ACTION_APPLY_BEST)

        self.assertEqual(targets, {MAGNET: 150.0})
        self.assertIn("启动前快照", sources[MAGNET])
        result = service.finalize(status.run_id, ACTION_APPLY_BEST, confirm=True)
        self.assertTrue(result.ok, result.detail)
        self.assertIn("启动前快照", result.detail)

    def test_safe_values_prefers_the_configured_safe_value(self) -> None:
        config = build_config()
        for item in config.entries:
            if item.signal == MAGNET:
                item.safe_value = 42.0
        service = self.build(config)
        self.set_device(150.0)
        self.set_target(10.0)
        status = service.start(request(loss_protection=False, max_iterations=1))
        service.approve(status.run_id)
        service.stop(status.run_id)

        result = service.finalize(status.run_id, ACTION_SAFE_VALUES, confirm=True)

        self.assertTrue(result.ok, result.detail)
        self.assertAlmostEqual(float(self.gateway.read(MAGNET_RB).value), 42.0, places=3)
        self.assertIn("安全值", result.detail)

    def test_safe_values_refuses_when_there_is_no_basis_at_all(self) -> None:
        """既没有 safe_value、也没有下限：拒绝执行，而不是静默按 0 下发。"""
        config = build_config()
        config.entries = [
            item.model_copy(update={"min_value": None, "safe_value": None})
            if item.signal == MAGNET else item
            for item in config.entries
        ]
        service = self.build(config)
        self.set_device(150.0)
        self.set_target(10.0)
        status = service.start(request(loss_protection=False, max_iterations=1))
        service.approve(status.run_id)
        service.stop(status.run_id)

        with self.assertRaises(TuningError) as ctx:
            service.finalize(status.run_id, ACTION_SAFE_VALUES, confirm=True)

        self.assertIn("安全值", str(ctx.exception))


class BudgetReportTests(_ServiceHarness):
    """D5：轮次口径——有效轮与总轮分别可查。"""

    def test_completion_message_reports_valid_rounds(self) -> None:
        service = self.build()
        self.set_device(150.0)
        self.set_target(10.0)
        status = service.start(
            request(loss_protection=False, max_iterations=2, patience=0)
        )
        service.approve(status.run_id)
        service.approve(status.run_id)

        after = service.status(status.run_id)

        self.assertEqual(after.state, "completed")
        messages = " ".join(
            event.message for event in service.events(status.run_id).events
        )
        self.assertIn("累计有效", messages)


if __name__ == "__main__":
    unittest.main()
