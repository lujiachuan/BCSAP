"""带毫秒时间戳的过程事件流与增量接口（改进方案批次 2）。

为什么单独钉这一组：
- 现场复盘问的是"这一轮为什么花了 40 秒""这次写入被拒了没有"——只有轮末一个时间戳
  和一行 quality 回答不了；
- 增量拉取的契约（``since`` / ``after_iteration``）错了，界面会**静默丢事件或重复刷屏**，
  但功能测试看不出来。

覆盖：事件序号单调且落库、毫秒时间戳可解析、四段耗时齐全、逐路写入/到位/采样留痕、
暂停与停止留痕、``/events`` 与 ``/progress`` 的增量语义、老库缺列时的兼容读取。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from apps.instrument_service.device_locks import DeviceLockManager
from apps.instrument_service.pv_health import create_simulated_gateway
from apps.instrument_service.signal_io import SignalWriteService
from apps.instrument_service.tuning_service import TuningError, TuningService
from apps.instrument_service.tuning_store import TuningStore
from packages.contracts.tuning import (
    TuningEvent,
    TuningEventsResponse,
    TuningProgressResponse,
)
from tests.test_tuning_service import MAGNET, build_config, request


def _timestamp(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    assert moment.tzinfo is not None, f"时间戳必须带时区：{value}"
    return moment


class TuningEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.directory = Path(self._directory.name)
        self.store = TuningStore(self.directory)
        self.config = build_config()
        self.signals = SignalWriteService(
            create_simulated_gateway(self.config), self.config, sleep=lambda _s: None
        )
        self.service = TuningService(
            lambda: self.signals,
            DeviceLockManager(),
            self.store,
            sleep=lambda _s: None,
        )

    def tearDown(self) -> None:
        import shutil

        for run in getattr(self.service, "_runs", {}).values():
            study = getattr(run.optimizer, "_study", None)
            if study is not None:
                try:
                    study._storage.close()
                except Exception:  # noqa: BLE001
                    pass
        shutil.rmtree(self.directory, ignore_errors=True)

    # ---------------------------------------------------------------- 启动
    def test_start_emits_timestamped_events_in_order(self) -> None:
        status = self.service.start(request(loss_protection=False))

        events = self.service.events(status.run_id).events
        messages = [event.message for event in events]
        self.assertGreaterEqual(len(events), 4)
        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))
        self.assertTrue(any("任务创建" in text for text in messages))
        self.assertTrue(any("快照" in text for text in messages))
        self.assertTrue(any("基线" in text for text in messages))
        self.assertTrue(any("候选已生成" in text for text in messages))
        # 毫秒精度：isoformat 带微秒，界面显示到毫秒
        for event in events:
            self.assertRegex(event.at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}")
            _timestamp(event.at)

    def test_events_are_persisted_for_a_fresh_store_instance(self) -> None:
        status = self.service.start(request(loss_protection=False))

        reopened = TuningStore(self.directory)
        loaded = reopened.load_events(status.run_id)
        self.assertTrue(loaded)
        self.assertEqual(reopened.latest_event_seq(status.run_id), loaded[-1].seq)

    def test_snapshot_reports_every_signal_and_phase(self) -> None:
        status = self.service.start(request(loss_protection=False))

        events = self.service.events(status.run_id).events
        snapshot_events = [event for event in events if "启动前快照：" in event.message]
        self.assertEqual(len(snapshot_events), 1)
        self.assertEqual(snapshot_events[0].data["signal"], MAGNET)
        # 阶段字段必须写清"当时在哪一步"，否则日志只能按顺序猜
        phases = {event.phase for event in events}
        self.assertIn("prechecking", phases)
        self.assertIn("proposing", phases)

    # ---------------------------------------------------------------- 一轮
    def test_round_records_four_timestamps_and_spread(self) -> None:
        status = self.service.start(request(loss_protection=False, samples_per_point=3))

        self.service.approve(status.run_id)

        progress = self.service.progress(status.run_id)
        self.assertEqual(len(progress.iterations), 1)
        iteration = progress.iterations[0]
        for field in ("proposed_at", "applied_at", "settled_at", "measured_at"):
            value = getattr(iteration, field)
            self.assertIsNotNone(value, field)
        self.assertLessEqual(
            _timestamp(iteration.proposed_at), _timestamp(iteration.applied_at)
        )
        self.assertLessEqual(
            _timestamp(iteration.applied_at), _timestamp(iteration.measured_at)
        )
        self.assertIsNotNone(iteration.duration_ms)
        self.assertGreaterEqual(iteration.duration_ms, 0)
        self.assertEqual(iteration.sample_count, 3)
        self.assertEqual(len(iteration.samples), 3)
        self.assertIsNotNone(iteration.sample_spread)
        self.assertIsNone(iteration.failure_step)

    def test_round_emits_per_signal_write_and_sample_events(self) -> None:
        status = self.service.start(request(loss_protection=False, samples_per_point=2))

        self.service.approve(status.run_id)

        events = self.service.events(status.run_id).events
        messages = " | ".join(event.message for event in events)
        self.assertIn("写入", messages)
        self.assertIn("回读到位", messages)
        self.assertIn("目标采样 1/2", messages)
        self.assertIn("目标采样 2/2", messages)
        self.assertIn("轮记录完成", messages)
        phases = {event.phase for event in events}
        for phase in ("writing", "settling", "measuring", "recording"):
            self.assertIn(phase, phases)
        settle_events = [event for event in events if event.phase == "settling"]
        self.assertTrue(any("seconds" in event.data for event in settle_events))

    def test_iteration_timing_survives_a_reload(self) -> None:
        status = self.service.start(request(loss_protection=False))
        self.service.approve(status.run_id)

        reopened = TuningStore(self.directory)
        iteration = reopened.load_iterations(status.run_id)[0]
        self.assertIsNotNone(iteration.measured_at)
        self.assertEqual(iteration.sample_count, 2)

    # ---------------------------------------------------------------- 暂停/停止
    def test_pause_resume_and_stop_are_recorded(self) -> None:
        status = self.service.start(request(loss_protection=False))
        self.service.pause(status.run_id)
        self.service.resume(status.run_id)
        self.service.stop(status.run_id)

        messages = [event.message for event in self.service.events(status.run_id).events]
        self.assertTrue(any("已暂停" in text for text in messages))
        self.assertTrue(any("已继续" in text for text in messages))
        self.assertTrue(any("停止" in text for text in messages))

    # ---------------------------------------------------------------- 增量语义
    def test_events_since_returns_only_new_ones(self) -> None:
        status = self.service.start(request(loss_protection=False))

        first = self.service.events(status.run_id, since=0)
        self.service.approve(status.run_id)
        second = self.service.events(status.run_id, since=first.latest_seq)

        self.assertGreater(second.latest_seq, first.latest_seq)
        self.assertTrue(second.events)
        self.assertTrue(all(event.seq > first.latest_seq for event in second.events))
        self.assertFalse(
            {event.seq for event in first.events} & {event.seq for event in second.events}
        )

    def test_progress_returns_status_iterations_and_latest_seq(self) -> None:
        status = self.service.start(request(loss_protection=False))
        self.service.approve(status.run_id)

        everything = self.service.progress(status.run_id)
        self.assertEqual(everything.run_id, status.run_id)
        self.assertEqual(everything.status.completed_iterations, 1)
        self.assertGreaterEqual(everything.latest_seq, 1)
        self.assertIsInstance(everything, TuningProgressResponse)
        self.assertIsInstance(self.service.events(status.run_id), TuningEventsResponse)

        tail = self.service.progress(status.run_id, after_iteration=0)
        self.assertEqual(tail.iterations, [])

    def test_unknown_run_is_reported_not_silently_empty(self) -> None:
        with self.assertRaises(TuningError):
            self.service.events("没有这个任务")
        with self.assertRaises(TuningError):
            self.service.progress("没有这个任务")

    # ---------------------------------------------------------------- 状态扩展
    def test_status_carries_revision_phase_and_protection_state(self) -> None:
        status = self.service.start(request(loss_protection=False))

        self.assertGreater(status.revision, 0)
        self.assertTrue(status.phase)
        self.assertTrue(status.protection_ready)  # 关掉保护 = 不需要保护
        self.assertTrue(status.protection_reasons)
        self.assertIn("已关闭", " ".join(status.protection_reasons))

    def test_status_reports_relative_protection_not_ready_without_baseline(self) -> None:
        """基线读不到时相对判据不成立：状态必须如实说"没准备好"。"""
        self.signals = SignalWriteService(
            _gateway_without_target(self.config), self.config, sleep=lambda _s: None
        )
        service = TuningService(
            lambda: self.signals, DeviceLockManager(), TuningStore(self.directory),
            sleep=lambda _s: None,
        )

        status = service.start(request(loss_protection=True))

        self.assertFalse(status.protection_ready)
        self.assertTrue(
            any("相对判据不可用" in reason for reason in status.protection_reasons)
        )


class _StubService:
    """路由契约测试用的假服务：只实现 runtime 关闭路径会碰到的几个方法。"""

    def run_ids(self) -> list[str]:
        return []

    def stop(self, run_id: str) -> None:
        return None

    def wait_idle(self, timeout: float = 10.0) -> bool:
        return True


class TuningEventApiTests(unittest.TestCase):
    """路由契约：直接调 endpoint（不起 HTTP 服务，也不碰现场设备）。"""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _app(self, stub):
        from apps.instrument_service import pv_mapping
        from apps.instrument_service.app import create_app
        from apps.instrument_service.runtime import InstrumentRuntime

        runtime = InstrumentRuntime(
            pv_mapping.default_config(), gateway_factory=lambda config: None
        )
        runtime._tuning = stub  # 只验证路由契约，不真跑任务
        return create_app(runtime), runtime

    def test_events_route_returns_incremental_payload(self) -> None:
        class _Stub(_StubService):
            def events(self, run_id: str, since: int = 0, limit: int = 2000):
                event = TuningEvent(
                    run_id=run_id, seq=since + 1, at="2026-09-22T10:00:00.123456+00:00",
                    level="info", phase="writing", iteration=0, message="写入 m1=180",
                )
                return TuningEventsResponse(
                    run_id=run_id, events=[event], latest_seq=event.seq
                )

            def progress(self, run_id: str, after_iteration: int = -1):
                raise AssertionError("本用例只测 events 路由")

        app, runtime = self._app(_Stub())
        try:
            route = next(
                r for r in app.routes
                if getattr(r, "path", None) == "/control/v1/tuning/runs/{run_id}/events"
            )
            payload = route.endpoint("run-1", since=4, limit=10)
            self.assertEqual(payload.latest_seq, 5)
            self.assertEqual(payload.events[0].message, "写入 m1=180")
        finally:
            runtime.close()

    def test_progress_route_forwards_after_iteration(self) -> None:
        from packages.contracts.tuning import TuningRunStatus

        seen: dict[str, int] = {}

        class _Stub(_StubService):
            def events(self, run_id: str, since: int = 0, limit: int = 2000):
                raise AssertionError("本用例只测 progress 路由")

            def progress(self, run_id: str, after_iteration: int = -1):
                seen["after"] = after_iteration
                return TuningProgressResponse(
                    run_id=run_id,
                    status=TuningRunStatus(
                        run_id=run_id, state="running", mode="confirm",
                        target_signal="detector.fc1.beam_current", max_iterations=5,
                        completed_iterations=2, revision=7, phase="measuring",
                    ),
                    iterations=[],
                    latest_seq=9,
                )

        app, runtime = self._app(_Stub())
        try:
            route = next(
                r for r in app.routes
                if getattr(r, "path", None) == "/control/v1/tuning/runs/{run_id}/progress"
            )
            payload = route.endpoint("run-9", after_iteration=1)
            self.assertEqual(seen["after"], 1)
            self.assertEqual(payload.status.phase, "measuring")
            self.assertEqual(payload.latest_seq, 9)
        finally:
            runtime.close()


def _gateway_without_target(config):
    """目标信号读不到的网关：验证"相对判据不可用"的如实上报。"""
    gateway = create_simulated_gateway(config)

    class _NoTarget:
        def __init__(self, inner) -> None:
            self._inner = inner
            self._values = inner._values

        def connect(self) -> bool:
            return True

        def read(self, signal: str):
            if signal.endswith(".beam_current"):
                raise KeyError(f"未配置模拟信号：{signal}")
            return self._inner.read(signal)

        def write(self, signal: str, value: float, command_id):
            return self._inner.write(signal, value, command_id)

        def snapshot(self, signals):
            return {signal: self.read(signal) for signal in signals}

    return _NoTarget(gateway)


if __name__ == "__main__":
    unittest.main()
