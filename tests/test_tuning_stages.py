"""两阶段优化策略：逐参数 → 联合微调。

断言围绕三件事：阶段 1 **只动一个变量**（其余真的不被写）、
阶段 2 **在最优点附近收窄**（不是把整段量程再搜一遍）、
以及阶段切换时**已有观测不丢**（换维度的优化器要接续，否则每阶段都在重新瞎猜）。
"""

from __future__ import annotations

import sqlite3
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
    TuningIteration,
    TuningRunRequest,
    TuningVariable,
)
from tests.test_tuning_service import MAGNET, MAGNET_RB, TARGET, entry

FOCUS = "ion_optics.focus.voltage_setpoint"
FOCUS_RB = "ion_optics.focus.voltage_readback"


def two_variable_config() -> PvMappingConfig:
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
                group="聚焦/漂移管", min_value=0.0, max_value=4000.0, max_step=500.0,
                settle_tol=5.0, settle_timeout=1.0,
            ),
            entry(
                FOCUS_RB, "Part1:JM_POWER:03:R_VOL", "V",
                group="聚焦/漂移管",
            ),
            entry(TARGET, "BD:FC:01:BeamCurrent", "nA", group="束流探测", required=True),
        ],
    )


def two_variable_request(**overrides: object) -> TuningRunRequest:
    base = {
        "target_signal": TARGET,
        "variables": [
            TuningVariable(signal=MAGNET, label="磁铁1 电流", low=100.0, high=300.0),
            TuningVariable(signal=FOCUS, label="聚焦电压", low=1000.0, high=3000.0),
        ],
        "max_iterations": 10,
        "settle_timeout_s": 0.2,
        "samples_per_point": 1,
    }
    base.update(overrides)
    return TuningRunRequest(**base)  # type: ignore[arg-type]


class StageStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.store = TuningStore(Path(self._directory.name))
        self.locks = DeviceLockManager()
        self.config = two_variable_config()
        self.gateway = create_simulated_gateway(self.config)
        self.slept: list[float] = []
        self.signals = SignalWriteService(self.gateway, self.config, sleep=lambda _s: None)
        self.service = TuningService(
            lambda: self.signals, self.locks, self.store, sleep=self.slept.append
        )
        self.writes: list[str] = []
        original = self.gateway.write

        def recording_write(signal, value, command_id):
            self.writes.append(signal)
            return original(signal, value, command_id)

        self.gateway.write = recording_write  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self._directory.cleanup()

    def run_round(self, run_id: str) -> dict:
        """确认一轮并返回该轮记录（从服务端状态里取）。"""
        self.service.approve(run_id)
        return self.service.status(run_id).pending and {} or self.service._require(
            run_id
        ).iterations[-1].model_dump()

    def test_joint_strategy_is_a_single_stage_over_every_variable(self) -> None:
        run_id = self.service.start(two_variable_request()).run_id
        run = self.service._require(run_id)

        self.assertEqual(run.stage, "joint")
        self.assertEqual(sorted(run.active_signals), sorted([MAGNET, FOCUS]))
        status = self.service.status(run_id)
        self.assertEqual(status.strategy, "joint")
        self.assertEqual(status.stage, "joint")

    def test_sequential_strategy_tunes_one_variable_at_a_time(self) -> None:
        run_id = self.service.start(
            two_variable_request(
                strategy="sequential_then_joint", calls_per_variable=2, max_iterations=8
            )
        ).run_id

        seen: list[tuple[str, list[str], str]] = []
        for _ in range(6):
            run = self.service._require(run_id)
            status = self.service._status_of(run)
            seen.append((run.stage, list(run.active_signals), status.stage_variable))
            self.service.approve(run_id)

        # 前 2 轮调磁铁、接着 2 轮调聚焦、之后进入联合
        self.assertEqual([item[1] for item in seen[:4]],
                         [[MAGNET], [MAGNET], [FOCUS], [FOCUS]])
        self.assertEqual(seen[0][0], "sequential")
        self.assertEqual(seen[0][2], MAGNET)
        self.assertEqual(seen[3][2], FOCUS)
        self.assertEqual(seen[4][0], "joint")
        self.assertEqual(sorted(seen[4][1]), sorted([MAGNET, FOCUS]))

    def test_stage_one_does_not_write_the_held_variable(self) -> None:
        run_id = self.service.start(
            two_variable_request(strategy="sequential_then_joint", calls_per_variable=1,
                                 max_iterations=6)
        ).run_id
        self.writes.clear()

        self.service.approve(run_id)  # 阶段 1：只调磁铁

        # 斜坡会把一次写入拆成多步，所以只断言"写到的是哪一路"，不断言次数
        self.assertIn(MAGNET, self.writes)
        self.assertNotIn(FOCUS, self.writes, "阶段 1 不该动保持不动的变量")

    def test_stage_one_records_the_held_variable_readback(self) -> None:
        """不写不等于不记：事后要能看到"当时另一个参数在哪"。"""
        run_id = self.service.start(
            two_variable_request(strategy="sequential_then_joint", calls_per_variable=1,
                                 max_iterations=6)
        ).run_id

        iteration = self.run_round(run_id)

        self.assertEqual(list(iteration["applied"]), [MAGNET])
        self.assertIn(FOCUS, iteration["readback"])

    def test_each_iteration_records_which_stage_it_belongs_to(self) -> None:
        """每一轮都记下阶段：只写"第 3 轮"的话，事后（导出日志/响应曲线）
        分不清那几轮是逐参数还是联合微调。（改造报告 §8.9 P2.4）"""
        run_id = self.service.start(
            two_variable_request(strategy="sequential_then_joint", calls_per_variable=1,
                                 max_iterations=6)
        ).run_id

        stages = [self.run_round(run_id)["stage"] for _ in range(3)]

        self.assertEqual(stages, ["sequential", "sequential", "joint"])

    def test_stage_two_bounds_are_narrowed_around_the_stage_one_best(self) -> None:
        run_id = self.service.start(
            two_variable_request(strategy="sequential_then_joint", calls_per_variable=1,
                                 joint_frac=0.2, max_iterations=6)
        ).run_id
        for _ in range(2):  # 阶段 1 走完（2 个变量各 1 轮）
            self.service.approve(run_id)
        run = self.service._require(run_id)

        dimensions = {d.name: d for d in run.optimizer.dimensions}

        self.assertEqual(run.stage, "joint")
        # 每个变量都被收窄到原范围的 20%，且落在原范围之内
        for name, (low, high) in (
            (MAGNET, (100.0, 300.0)),
            (FOCUS, (1000.0, 3000.0)),
        ):
            dimension = dimensions[name]
            self.assertLessEqual(dimension.span, (high - low) * 0.2 + 1e-9)
            self.assertGreaterEqual(dimension.low, low)
            self.assertLessEqual(dimension.high, high)
            # 收窄区间必须包含该变量**自己**的最优点（联合微调围绕它做）
            center = run.variable_best[name][0]
            self.assertLessEqual(dimension.low, center)
            self.assertGreaterEqual(dimension.high, center)

    def test_stage_two_centers_on_each_variables_own_best_point(self) -> None:
        """报告要求围绕**各最优点**收窄，而不是围绕最后一轮停在哪。"""
        run_id = self.service.start(
            two_variable_request(strategy="sequential_then_joint", calls_per_variable=3,
                                 joint_frac=0.1, max_iterations=12)
        ).run_id
        for _ in range(6):  # 2 个变量各 3 轮
            self.service.approve(run_id)
        run = self.service._require(run_id)

        for name in (MAGNET, FOCUS):
            best_value, _objective = run.variable_best[name]
            dimension = {d.name: d for d in run.optimizer.dimensions}[name]
            self.assertLessEqual(dimension.low, best_value)
            self.assertGreaterEqual(dimension.high, best_value)

    def test_stage_two_keeps_the_observations_from_stage_one(self) -> None:
        run_id = self.service.start(
            two_variable_request(strategy="sequential_then_joint", calls_per_variable=2,
                                 max_iterations=8)
        ).run_id
        for _ in range(4):
            self.service.approve(run_id)

        run = self.service._require(run_id)

        self.assertEqual(run.stage, "joint")
        self.assertEqual(run.optimizer.n_observed, 4, "换阶段的优化器要接续已有观测")

    def test_sequential_without_room_for_stage_two_is_refused(self) -> None:
        with self.assertRaises(TuningError) as caught:
            self.service.start(
                two_variable_request(strategy="sequential_then_joint",
                                     calls_per_variable=3, max_iterations=6)
            )

        self.assertIn("联合微调阶段没有轮次可用", str(caught.exception))

    def test_invalid_strategy_parameters_are_rejected(self) -> None:
        cases = (
            {"strategy": "magic"},
            {"calls_per_variable": 0},
            {"joint_frac": 0.0},
            {"joint_frac": 1.5},
            {"hold_s": -1.0},
        )
        for overrides in cases:
            with self.subTest(**overrides):
                with self.assertRaises(TuningError):
                    self.service.start(two_variable_request(**overrides))

    def test_hold_time_is_waited_before_sampling(self) -> None:
        self.slept.clear()

        run_id = self.service.start(two_variable_request(hold_s=0.75)).run_id
        self.service.approve(run_id)

        self.assertIn(0.75, self.slept)

    def test_status_reports_stage_position(self) -> None:
        run_id = self.service.start(
            two_variable_request(strategy="sequential_then_joint", calls_per_variable=1,
                                 max_iterations=6)
        ).run_id

        first = self.service.status(run_id)
        self.service.approve(run_id)
        second = self.service.status(run_id)

        self.assertEqual((first.stage, first.stage_index, first.stage_total),
                         ("sequential", 1, 2))
        self.assertEqual((second.stage, second.stage_index, second.stage_total),
                         ("sequential", 2, 2))
        self.assertEqual(second.stage_variable, FOCUS)

    def test_snapshot_read_is_retried_before_refusing_to_start(self) -> None:
        """冷启动首次读失败不该白拒一次：退避重试后再判断。"""
        original = self.signals._gateway
        calls: list[str] = []

        class Flaky:
            def __init__(self, inner):
                self._inner = inner
                self._failed = False

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def read(self, signal):
                if not self._failed:
                    self._failed = True
                    calls.append(signal)
                    raise OSError("CA 首次建连失败")
                return self._inner.read(signal)

        self.signals._gateway = Flaky(original)

        status = self.service.start(two_variable_request())

        self.assertEqual(status.state, "awaiting_confirmation", status.message)
        self.assertTrue(status.snapshot, "重试之后应当拿到快照")
        self.assertTrue(calls, "第一次读确实失败了（否则这个用例没验证到重试）")


class IterationStageStoreTests(unittest.TestCase):
    """阶段的落盘与老库迁移：历史轮次没有这一列时读出来是"未记录"，不猜。"""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name)

    @staticmethod
    def record(index: int, stage: str | None) -> TuningIteration:
        return TuningIteration(
            iteration=index,
            proposed={MAGNET: 150.0},
            applied={MAGNET: 150.0},
            readback={MAGNET: 150.0, MAGNET_RB: 150.0},
            target=8.0,
            objective=8.0,
            quality="ok",
            at="2026-09-14T00:00:00+00:00",
            stage=stage,
        )

    def create_run(self, store: TuningStore) -> None:
        store.create_run(
            "run-1",
            target_signal=TARGET,
            mode="confirm",
            max_iterations=3,
            variables=[{"signal": MAGNET, "low": 100.0, "high": 300.0}],
            created_at="2026-09-14T00:00:00+00:00",
        )

    def test_stage_survives_a_store_round_trip(self) -> None:
        store = TuningStore(self.path)
        self.create_run(store)

        store.append_iteration("run-1", self.record(0, "sequential"))
        store.append_iteration("run-1", self.record(1, "joint"))

        loaded = store.load_iterations("run-1")
        self.assertEqual([item.stage for item in loaded], ["sequential", "joint"])

    def test_old_database_gets_the_column_and_reads_back_unknown(self) -> None:
        """现场已有库没有 stage 列：迁移补列，老轮次按 None（未记录）读。"""
        store = TuningStore(self.path)
        self.create_run(store)
        store.append_iteration("run-1", self.record(0, "joint"))
        # 注意 with sqlite3.connect(...) 只提交事务、**不关连接**：Windows 上文件被
        # 连接占着删不掉，临时目录清理会炸。这里显式关。
        connection = sqlite3.connect(self.path / "tuning_spool.sqlite3")
        try:
            connection.execute("ALTER TABLE tuning_iterations DROP COLUMN stage")
            connection.commit()
        finally:
            connection.close()

        reopened = TuningStore(self.path)

        loaded = reopened.load_iterations("run-1")
        self.assertEqual(len(loaded), 1)
        self.assertIsNone(loaded[0].stage)


if __name__ == "__main__":
    unittest.main()
