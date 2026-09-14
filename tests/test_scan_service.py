"""扫谱状态机与本地暂存：逐点流程、失败语义、设备锁与持久化。

这些用例覆盖的是**设备安全相关**的行为，不是普通功能：
写被拒时设备是否真的没动、下发中断是否被识别成「结果未知」、
停止是否真的停在安全处、锁是否在异常路径上也释放。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from apps.instrument_service.device_locks import DeviceBusy, DeviceLockManager
from apps.instrument_service.pv_health import create_simulated_gateway
from apps.instrument_service.runtime import InstrumentRuntime
from apps.instrument_service.scan_service import MAX_SCAN_POINTS, ScanError, ScanService
from apps.instrument_service.scan_store import ScanStore
from apps.instrument_service.signal_io import SignalWriteService
from packages.contracts import (
    PvMappingConfig,
    PvMappingEntry,
    ScanAxis,
    ScanRunRequest,
)
from packages.spectrum.codec import decode_spectrum

TERMINAL = {"completed", "aborted", "failed", "recovery_required"}


def entry(
    signal: str,
    pv: str,
    unit: str,
    *,
    writable: bool = True,
    group: str = "磁铁电源",
    role: str = "setpoint",
    readback_signal: str = "",
    **safety: object,
) -> PvMappingEntry:
    return PvMappingEntry(
        signal=signal, label=signal, pv=pv, unit=unit, writable=writable,
        required=False, group=group, role=role, readback_signal=readback_signal,
        **safety,  # type: ignore[arg-type]
    )


def build_config(*, coupled: bool = True, max_current: float = 600.0) -> PvMappingConfig:
    """一路磁铁设定 + 回读 + 探测器。coupled=False 时回读不跟随设定。"""
    return PvMappingConfig(
        version=1,
        entries=[
            entry(
                "magnet.m1.current_setpoint", "BD:DipoleMagnet:01:CurrentSet", "A",
                readback_signal="magnet.m1.current_readback" if coupled else "",
                min_value=0.0, max_value=max_current, max_step=100.0,
                settle_tol=0.5, settle_timeout=1.0,
            ),
            entry(
                "magnet.m1.current_readback", "BD:DipoleMagnet:01:CurrentMonitor", "A",
                writable=False, role="readback",
            ),
            entry(
                "detector.fc1.beam_current", "BD:FC:01:BeamCurrent", "nA",
                writable=False, group="束流探测", role="readback",
            ),
        ],
    )


def axis(signals: list[str] | None = None) -> ScanAxis:
    return ScanAxis(
        label="磁铁1 电流",
        setpoint_signals=signals or ["magnet.m1.current_setpoint"],
        readback_signal="magnet.m1.current_readback",
    )


class ScanServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.store = ScanStore(Path(self._directory.name))
        self.locks = DeviceLockManager()

    def tearDown(self) -> None:
        # 收尾还在跑的任务：守护线程会继续往 SQLite 写点，Windows 上表现为
        # 临时目录 PermissionError（文件被占用），掩盖真正的断言失败
        self._drain()
        self._directory.cleanup()

    def _drain(self) -> None:
        """停掉在跑的任务，并等线程真正结束再删临时目录。

        只等状态是不够的：终态是在线程内部设置的，线程之后还要写一次数据库，
        此时删目录会撞上 Win32 的「文件被占用」。
        """
        service = getattr(self, "service", None)
        if service is None:
            return
        for run_id in service.run_ids():
            if service.status(run_id).state not in TERMINAL:
                service.stop(run_id)
        service.wait_idle(20.0)

    def build(
        self, config: PvMappingConfig | None = None
    ) -> tuple[ScanService, SignalWriteService]:
        self.config = config or build_config()
        signals = SignalWriteService(
            create_simulated_gateway(self.config), self.config, sleep=lambda _s: None
        )
        service = ScanService(
            lambda: signals, self.locks, self.store, sleep=lambda _s: None
        )
        self.service = service
        return service, signals

    @staticmethod
    def wait(service: ScanService, run_id: str, timeout: float = 20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = service.status(run_id)
            if status.state in TERMINAL:
                return status
            time.sleep(0.02)
        raise AssertionError(f"任务未在 {timeout}s 内结束：{service.status(run_id).state}")

    @staticmethod
    def request(**overrides: object) -> ScanRunRequest:
        base = {
            "axis": axis(),
            "detector_signal": "detector.fc1.beam_current",
            "start": 100.0,
            "stop": 140.0,
            "step": 10.0,
            "dwell_s": 0.0,
            "samples_per_point": 3,
            "settle_timeout_s": 0.3,
        }
        base.update(overrides)
        return ScanRunRequest(**base)  # type: ignore[arg-type]

    # ---------------- 正常完成 ----------------
    def test_scan_completes_and_records_actual_readback(self) -> None:
        service, signals = self.build()

        status = self.wait(service, service.start(self.request()).run_id)

        self.assertEqual(status.state, "completed")
        self.assertEqual(status.completed_points, 5)
        points = service.points(status.run_id).points
        # 横坐标必须是实际回读值，而不是下发过的设定值
        self.assertEqual([p.coordinate for p in points], [100.0, 110.0, 120.0, 130.0, 140.0])
        self.assertEqual([p.target for p in points], [100.0, 110.0, 120.0, 130.0, 140.0])
        self.assertTrue(all(p.quality == "ok" for p in points))
        self.assertTrue(all(p.included for p in points))

    def test_completed_run_publishes_spectrum_with_checksum(self) -> None:
        service, _ = self.build()

        status = self.wait(service, service.start(self.request()).run_id)

        self.assertIsNotNone(status.spectrum_id)
        self.assertEqual(status.point_count, 5)
        payload = Path(status.spectrum_path).read_bytes()
        x, y = decode_spectrum(payload)
        self.assertEqual(list(x), [100.0, 110.0, 120.0, 130.0, 140.0])
        self.assertEqual(len(y), 5)
        from packages.spectrum.codec import spectrum_checksum

        self.assertEqual(spectrum_checksum(payload), status.sha256)

    def test_points_are_persisted_in_store(self) -> None:
        service, _ = self.build()
        status = self.wait(service, service.start(self.request()).run_id)

        rows = self.store.load_points(status.run_id)

        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["quality"], "ok")
        self.assertEqual(self.store.load_run(status.run_id)["state"], "completed")

    def test_large_sweep_is_ramped_by_execution_layer(self) -> None:
        """单点跨度超过 max_step=100 时必须分步，不能一步跳到目标。"""
        service, signals = self.build()

        status = self.wait(
            service,
            service.start(self.request(start=100.0, stop=400.0, step=300.0)).run_id,
        )

        self.assertEqual(status.state, "completed")
        self.assertEqual(signals.read_value("magnet.m1.current_setpoint"), 400.0)

    def test_single_point_sweep_is_allowed(self) -> None:
        service, _ = self.build()

        status = self.wait(service, service.start(self.request(start=120.0, stop=120.0)).run_id)

        self.assertEqual(status.state, "completed")
        self.assertEqual(status.total_points, 1)

    # ---------------- 失败语义 ----------------
    def test_write_beyond_bounds_fails_and_device_untouched(self) -> None:
        """越界目标点：任务失败，且设定值不能停在越界值上。"""
        service, signals = self.build()

        status = self.wait(
            service,
            service.start(self.request(start=100.0, stop=700.0, step=600.0)).run_id,
        )

        self.assertEqual(status.state, "failed")
        self.assertIn("被拒", status.message)
        self.assertLessEqual(signals.read_value("magnet.m1.current_setpoint"), 600.0)

    def test_unsettled_readback_fails_when_configured_to_fail(self) -> None:
        """回读不跟随设定 → 永远不稳定；on_unsettled=fail 必须判失败。"""
        service, _ = self.build(build_config(coupled=False))

        status = self.wait(
            service,
            service.start(self.request(on_unsettled="fail", settle_timeout_s=0.05)).run_id,
        )

        self.assertEqual(status.state, "failed")
        self.assertIn("未稳定", status.message)

    def test_unsettled_readback_is_recorded_and_flagged_when_configured(self) -> None:
        """on_unsettled=record：点要记下来，但 quality 必须如实标注。"""
        service, _ = self.build(build_config(coupled=False))

        status = self.wait(
            service,
            service.start(self.request(on_unsettled="record", settle_timeout_s=0.05)).run_id,
        )

        self.assertEqual(status.state, "completed")
        points = service.points(status.run_id).points
        self.assertTrue(all(p.quality == "unsettled" for p in points))
        self.assertTrue(all(p.detail for p in points))

    def test_unreadable_detector_fails_fast_in_preparing(self) -> None:
        """探测器一开始就读不到：应当在准备阶段就拒绝，而不是空扫一遍再报错。

        「信号已配置但读不到」（PV 掉线）与「映射里没有这个信号」是两回事，
        后者属于参数错误，在更早的校验里就被拒掉了。
        """
        from packages.epics_adapter import SimulatedEpicsGateway

        config = build_config()
        gateway = SimulatedEpicsGateway(
            {
                "magnet.m1.current_setpoint": (600.0, "A"),
                "magnet.m1.current_readback": (111.0, "A"),
                # detector.fc1.beam_current 故意不提供 → 读取失败
            },
            coupling={"magnet.m1.current_setpoint": "magnet.m1.current_readback"},
        )
        signals = SignalWriteService(gateway, config, sleep=lambda _s: None)
        service = ScanService(
            lambda: signals, self.locks, self.store, sleep=lambda _s: None
        )
        self.service = service

        status = self.wait(service, service.start(self.request()).run_id)

        self.assertEqual(status.state, "failed")
        self.assertIn("设备连接未就绪", status.message)
        self.assertEqual(status.completed_points, 0)

    def test_detector_dropout_mid_scan_excludes_points(self) -> None:
        """探测器在扫描中途掉线：该点标 read_failed 且**不进谱图**。

        把 0 当成真实强度写进 x/y 会污染分析结果，所以掉线点在点表里可追溯，
        但被排除在数组之外。
        """
        from packages.epics_adapter import SimulatedEpicsGateway

        config = build_config()
        inner = SimulatedEpicsGateway(
            {
                "magnet.m1.current_setpoint": (600.0, "A"),
                "magnet.m1.current_readback": (111.0, "A"),
                "detector.fc1.beam_current": (8.31, "nA"),
            },
            coupling={"magnet.m1.current_setpoint": "magnet.m1.current_readback"},
        )
        calls = {"n": 0}

        class FlakyDetector:
            """准备阶段读得到，之后掉线。"""

            def __getattr__(self, name):
                return getattr(inner, name)

            def read(self, signal):
                if signal == "detector.fc1.beam_current":
                    calls["n"] += 1
                    if calls["n"] > 1:
                        raise OSError("PV 掉线")
                return inner.read(signal)

        signals = SignalWriteService(FlakyDetector(), config, sleep=lambda _s: None)
        service = ScanService(
            lambda: signals, self.locks, self.store, sleep=lambda _s: None
        )
        self.service = service

        status = self.wait(service, service.start(self.request()).run_id)

        points = service.points(status.run_id).points
        self.assertTrue(all(p.quality == "read_failed" for p in points))
        self.assertTrue(all(not p.included for p in points))
        # 没有合格点 → 不能生成谱图，也不能报 completed
        self.assertEqual(status.state, "failed")
        self.assertIn("没有任何有效点", status.message)

    def test_write_failure_reports_device_state_unknown(self) -> None:
        """写入抛错 → 设备可能已动，必须落到 recovery_required 而不是 failed。"""
        config = build_config()
        service, signals = self.build(config)

        class Exploding:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def write(self, signal, value, command_id):
                raise OSError("CA 写入超时")

        signals._gateway = Exploding(signals._gateway)

        status = self.wait(service, service.start(self.request()).run_id)

        self.assertEqual(status.state, "recovery_required")
        self.assertIn("状态未知", status.message)

    def test_recovery_required_keeps_the_device_locked(self) -> None:
        """设备状态未知时不能交还设备：否则下一个任务会在未知状态上继续驱动磁场。"""
        service, signals = self.build(build_config())

        class Exploding:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def write(self, signal, value, command_id):
                raise OSError("CA 写入超时")

        signals._gateway = Exploding(signals._gateway)
        status = self.wait(service, service.start(self.request()).run_id)

        self.assertEqual(status.state, "recovery_required")
        self.assertNotEqual(self.locks.held(), {})

    def test_acknowledge_recovery_releases_the_lock(self) -> None:
        service, signals = self.build(build_config())

        class Exploding:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def write(self, signal, value, command_id):
                raise OSError("CA 写入超时")

        signals._gateway = Exploding(signals._gateway)
        status = self.wait(service, service.start(self.request()).run_id)

        service.acknowledge_recovery(status.run_id, note="现场确认磁场已回零")

        self.assertEqual(self.locks.held(), {})
        self.assertIn("已人工确认", service.status(status.run_id).message)

    def test_acknowledge_on_a_healthy_run_is_rejected(self) -> None:
        service, _ = self.build()
        finished = self.wait(service, service.start(self.request()).run_id)

        with self.assertRaises(ScanError):
            service.acknowledge_recovery(finished.run_id)

    # ---------------- 停止 ----------------
    def test_stop_moves_run_to_aborted(self) -> None:
        service, _ = self.build()

        started = service.start(
            self.request(start=0.0, stop=500.0, step=1.0, dwell_s=0.01)
        )
        service.stop(started.run_id)
        status = self.wait(service, started.run_id)

        self.assertIn(status.state, {"aborted", "completed"})
        if status.state == "aborted":
            self.assertLess(status.completed_points, status.total_points)

    def test_stopping_a_finished_run_is_a_noop(self) -> None:
        service, _ = self.build()
        finished = self.wait(service, service.start(self.request()).run_id)

        again = service.stop(finished.run_id)

        self.assertEqual(again.state, "completed")

    # ---------------- 设备锁 ----------------
    def test_concurrent_scan_is_rejected(self) -> None:
        service, _ = self.build()
        service.start(self.request(start=0.0, stop=500.0, step=1.0, dwell_s=0.01))

        with self.assertRaises(ScanError):
            service.start(self.request())

    def test_locked_device_group_blocks_new_scan(self) -> None:
        service, _ = self.build()
        self.locks.acquire({"磁铁电源"}, owner="调束任务")

        status = self.wait(service, service.start(self.request()).run_id)

        self.assertEqual(status.state, "failed")
        self.assertIn("设备组不可用", status.message)

    def test_locks_are_released_after_run(self) -> None:
        service, _ = self.build()

        status = self.wait(service, service.start(self.request()).run_id)

        self.assertEqual(self.locks.held(), {})
        # 状态里也不得再报占用
        self.assertEqual(status.locked_groups, [])

    def test_locks_are_released_even_when_run_fails(self) -> None:
        service, _ = self.build()

        self.wait(
            service,
            service.start(self.request(start=100.0, stop=700.0, step=600.0)).run_id,
        )

        self.assertEqual(self.locks.held(), {})

    # ---------------- 参数校验 ----------------
    def test_unknown_signal_is_rejected_before_starting(self) -> None:
        service, _ = self.build()

        with self.assertRaises(ScanError):
            service.start(self.request(axis=axis(["no.such.setpoint"])))

    def test_read_only_signal_cannot_be_the_sweep_axis(self) -> None:
        service, _ = self.build()

        with self.assertRaises(ScanError):
            service.start(self.request(axis=axis(["detector.fc1.beam_current"])))

    def test_step_direction_mismatch_is_rejected(self) -> None:
        service, _ = self.build()

        with self.assertRaises(ScanError):
            service.start(self.request(start=100.0, stop=140.0, step=-10.0))

    def test_excessive_point_count_is_rejected(self) -> None:
        service, _ = self.build()

        with self.assertRaises(ScanError) as caught:
            service.start(self.request(start=0.0, stop=500.0, step=0.0001))

        self.assertIn(str(MAX_SCAN_POINTS), str(caught.exception))

    def test_unreadable_device_fails_in_preparing_without_writing(self) -> None:
        """设备连不上时应在准备阶段就失败，且一个点都没写（设备保持干净）。"""
        from packages.epics_adapter import SimulatedEpicsGateway

        config = build_config()
        signals = SignalWriteService(
            SimulatedEpicsGateway({}), config, sleep=lambda _s: None
        )
        service = ScanService(
            lambda: signals, self.locks, self.store, sleep=lambda _s: None
        )
        self.service = service

        status = self.wait(service, service.start(self.request()).run_id)

        self.assertEqual(status.state, "failed")
        self.assertIn("设备连接未就绪", status.message)
        self.assertEqual(status.completed_points, 0)
        self.assertEqual(self.locks.held(), {})

    def test_unknown_run_id_is_reported(self) -> None:
        service, _ = self.build()

        with self.assertRaises(ScanError):
            service.status("no-such-run")


class DeviceLockTests(unittest.TestCase):
    def test_acquire_is_all_or_nothing(self) -> None:
        """拿不齐就一个都不拿——半个任务占着设备比拿不到更糟。"""
        locks = DeviceLockManager()
        locks.acquire({"磁场电源"}, owner="a")

        with self.assertRaises(DeviceBusy):
            locks.acquire({"磁场电源", "探测器"}, owner="b")

        self.assertEqual(locks.held(), {"磁场电源": "a"})

    def test_same_owner_can_reacquire(self) -> None:
        locks = DeviceLockManager()
        locks.acquire({"磁场电源"}, owner="a")

        locks.acquire({"磁场电源"}, owner="a")

        self.assertEqual(locks.held(), {"磁场电源": "a"})

    def test_release_only_frees_own_groups(self) -> None:
        locks = DeviceLockManager()
        locks.acquire({"磁场电源"}, owner="a")
        locks.acquire({"探测器"}, owner="b")

        locks.release("a")

        self.assertEqual(locks.held(), {"探测器": "b"})

    def test_empty_group_set_is_a_noop(self) -> None:
        locks = DeviceLockManager()
        locks.acquire([], owner="a")
        locks.acquire([""], owner="a")

        self.assertEqual(locks.held(), {})


class RuntimeScanWiringTests(unittest.TestCase):
    """运行时把锁与暂存做成长期对象，扫谱服务按需取当前映射。"""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_runtime_exposes_shared_locks_and_store(self) -> None:
        from apps.instrument_service.pv_health import create_simulated_gateway

        runtime = InstrumentRuntime(
            build_config(),
            gateway_factory=create_simulated_gateway,
            store=ScanStore(Path(self._directory.name)),
        )
        self.addCleanup(runtime.close)

        self.assertIs(runtime.scan, runtime.scan)
        self.assertIs(runtime.locks, runtime.locks)
        self.assertIs(runtime.store, runtime.store)

    def test_scan_service_survives_config_hot_update(self) -> None:
        """改映射不能让进行中的任务被换掉（否则状态查询会 404）。"""
        from apps.instrument_service.pv_health import create_simulated_gateway

        runtime = InstrumentRuntime(
            build_config(),
            gateway_factory=create_simulated_gateway,
            store=ScanStore(Path(self._directory.name)),
        )
        self.addCleanup(runtime.close)
        before = runtime.scan

        runtime.apply(build_config())

        self.assertIs(runtime.scan, before)


if __name__ == "__main__":
    unittest.main()
