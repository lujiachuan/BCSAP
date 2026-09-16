"""扫谱状态机与本地暂存：逐点流程、失败语义、设备锁与持久化。

这些用例覆盖的是**设备安全相关**的行为，不是普通功能：
写被拒时设备是否真的没动、下发中断是否被识别成「结果未知」、
停止是否真的停在安全处、锁是否在异常路径上也释放。
"""

from __future__ import annotations

import json
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
    RetractSpec,
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
        scan_axis=(writable and role == "setpoint" and signal.endswith(".current_setpoint")),
        scan_detector=(not writable and signal.endswith(".beam_current")),
        **safety,  # type: ignore[arg-type]
    )


def build_config(*, max_current: float = 600.0) -> PvMappingConfig:
    """一路磁铁设定 + 回读 + 探测器。"""
    return PvMappingConfig(
        version=1,
        entries=[
            entry(
                "magnet.m1.current_setpoint", "BD:DipoleMagnet:01:CurrentSet", "A",
                readback_signal="magnet.m1.current_readback",
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


MAGNETS = (1, 2, 3, 4)


def build_group_config() -> PvMappingConfig:
    """四台磁铁成组扫描：每台都有**自己的**回读、速率与稳定判据。"""
    entries: list[PvMappingEntry] = []
    for n in MAGNETS:
        entries += [
            entry(
                f"magnet.m{n}.current_setpoint", f"BD:DipoleMagnet:{n:02d}:CurrentSet", "A",
                readback_signal=f"magnet.m{n}.current_readback",
                rate_signal=f"magnet.m{n}.current_rate_setpoint",
                min_value=0.0, max_value=600.0, max_step=100.0,
                settle_tol=0.5, settle_timeout=1.0,
            ),
            entry(
                f"magnet.m{n}.current_rate_setpoint", f"BD:DipoleMagnet:{n:02d}:CurrentRateSet",
                "A/s", min_value=0.0, max_value=10.0, max_step=1.0,
            ),
            entry(
                f"magnet.m{n}.current_readback", f"BD:DipoleMagnet:{n:02d}:CurrentMonitor",
                "A", writable=False, role="readback",
            ),
        ]
    entries.append(
        entry(
            "detector.fc1.beam_current", "BD:FC:01:BeamCurrent", "nA",
            writable=False, group="束流探测", role="readback",
        )
    )
    return PvMappingConfig(version=1, entries=entries)


def group_axis() -> ScanAxis:
    return ScanAxis(
        label="磁铁1~4 同步",
        setpoint_signals=[f"magnet.m{n}.current_setpoint" for n in MAGNETS],
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
        service, signals = self.build(build_config())
        signals._gateway._coupling = {}

        status = self.wait(
            service,
            service.start(self.request(on_unsettled="fail", settle_timeout_s=0.05)).run_id,
        )

        self.assertEqual(status.state, "failed")
        self.assertIn("未稳定", status.message)

    def test_unsettled_readback_is_recorded_and_flagged_when_configured(self) -> None:
        """on_unsettled=record：点要记下来，但 quality 必须如实标注。"""
        service, signals = self.build(build_config())
        signals._gateway._coupling = {}

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

    # ---------------- 成组扫描：逐路判到位 ----------------
    def group_request(self, **overrides: object) -> ScanRunRequest:
        base = {
            "axis": group_axis(),
            "detector_signal": "detector.fc1.beam_current",
            "start": 100.0,
            "stop": 120.0,
            "step": 10.0,
            "dwell_s": 0.0,
            "samples_per_point": 1,
            "settle_timeout_s": 0.05,
            "on_unsettled": "record",
        }
        base.update(overrides)
        return ScanRunRequest(**base)  # type: ignore[arg-type]

    def build_group(self):
        """四台磁铁成组，但只有 m1 的模拟电源跟随设定——其余三台卡在原值。

        这是"第一台到位、其余没跟上"的现场形态：只看第一路回读的实现会把它
        记成好点（坐标、偏差、质量全部来自第一台）。
        """
        service, signals = self.build(build_group_config())
        signals._gateway._coupling = {
            "magnet.m1.current_setpoint": "magnet.m1.current_readback"
        }
        return service, signals

    def test_group_scan_fails_when_any_magnet_does_not_settle(self) -> None:
        service, _ = self.build_group()

        status = self.wait(
            service, service.start(self.group_request(on_unsettled="fail")).run_id
        )

        self.assertEqual(status.state, "failed")
        self.assertIn("magnet.m2.current_readback", status.message)

    def test_group_scan_marks_the_point_unsettled_and_names_the_channel(self) -> None:
        service, _ = self.build_group()

        status = self.wait(service, service.start(self.group_request()).run_id)

        point = service.points(status.run_id).points[0]
        self.assertEqual(point.quality, "unsettled")
        self.assertIn("未进入容差", point.detail)
        self.assertIn("magnet.m2.current_readback", point.detail)

    def test_group_scan_records_every_readback_and_a_real_spread(self) -> None:
        """偏差必须来自各路**实际回读**；以前这里读的是设定信号，恒为 0。"""
        service, _ = self.build_group()

        status = self.wait(service, service.start(self.group_request()).run_id)

        point = service.points(status.run_id).points[0]
        self.assertEqual(
            sorted(point.readback_values),
            [f"magnet.m{n}.current_readback" for n in MAGNETS],
        )
        self.assertEqual(point.readback_values["magnet.m1.current_readback"], 100.0)
        self.assertGreater(point.coordinate_spread, 0.0)
        # 坐标仍取扫描轴指定的那一路
        self.assertEqual(point.coordinate, 100.0)

    def test_group_scan_persists_every_readback(self) -> None:
        service, _ = self.build_group()

        status = self.wait(service, service.start(self.group_request()).run_id)

        row = self.store.load_points(status.run_id)[0]
        stored = json.loads(row["readbacks_json"])
        self.assertEqual(len(stored), len(MAGNETS))
        self.assertIn("magnet.m4.current_readback", stored)

    def test_group_axis_needs_a_tolerance_on_every_channel(self) -> None:
        """任一路缺稳定判据就必须拒绝启动：缺判据的那一路等于不检查。

        以前只校验"某一路有判据"（any），于是第二路起可以完全没有判据，
        却仍然逐个点记成 ok。
        """
        config = build_group_config()
        entries = [
            item.model_copy(update={"settle_tol": None})
            if item.signal == "magnet.m3.current_setpoint"
            else item
            for item in config.entries
        ]
        service, _ = self.build(PvMappingConfig(version=1, entries=entries))

        with self.assertRaises(ScanError) as caught:
            service.start(self.group_request(settle_timeout_s=1.0))

        self.assertIn("magnet.m3.current_setpoint", str(caught.exception))

    def test_prepare_probes_every_readback(self) -> None:
        """某一路回读不可达时必须在写任何设定值之前就判失败。"""
        service, signals = self.build_group()
        before = signals._gateway.read("magnet.m1.current_setpoint").value
        signals._gateway._values.pop("magnet.m3.current_readback")

        status = self.wait(service, service.start(self.group_request()).run_id)

        self.assertEqual(status.state, "failed")
        self.assertIn("magnet.m3.current_readback", status.message)
        self.assertEqual(
            signals._gateway.read("magnet.m1.current_setpoint").value, before
        )

    # ---------------- 完成后回落（服务端安全收尾） ----------------
    def test_completed_run_retracts_every_magnet_and_writes_the_rate_first(self) -> None:
        """回落要把速率和电流都下发到**每一台**磁铁，且速率先于电流。"""
        service, signals = self.build(build_group_config())
        calls: list[tuple[str, float]] = []
        original_write = signals._gateway.write

        def recording_write(signal, value, command_id):
            calls.append((signal, float(value)))
            return original_write(signal, value, command_id)

        signals._gateway.write = recording_write

        status = self.wait(
            service,
            service.start(
                self.group_request(
                    retract=RetractSpec(current_a=0.0, rate_a_s=2.0)
                )
            ).run_id,
        )

        self.assertEqual(status.state, "completed")
        rates = [s for s, _v in calls if s.endswith("current_rate_setpoint")]
        self.assertEqual(
            sorted(rates),
            sorted(f"magnet.m{n}.current_rate_setpoint" for n in MAGNETS),
        )
        # 每一台都收到了回落电流 0（扫描目标是 100/110/120，不会混进去）
        retract_writes = [
            index
            for index, (signal, value) in enumerate(calls)
            if signal.endswith("current_setpoint") and value == 0.0
        ]
        self.assertEqual(len(retract_writes), len(MAGNETS))
        # 速率必须先于同一次回落的电流写下去
        rate_indexes = [
            index for index, (signal, _value) in enumerate(calls)
            if signal.endswith("current_rate_setpoint")
        ]
        self.assertLess(max(rate_indexes), min(retract_writes))
        self.assertIn("已回落到 0", status.message)
        for n in MAGNETS:
            self.assertEqual(
                signals._gateway.read(f"magnet.m{n}.current_readback").value, 0.0
            )

    def test_retract_runs_without_any_client_polling(self) -> None:
        """客户端退出/崩溃都不该影响回落：回落由服务端线程自己完成。"""
        service, signals = self.build(build_group_config())

        run_id = service.start(
            self.group_request(retract=RetractSpec(current_a=0.0, rate_a_s=2.0))
        ).run_id
        # 只等线程真正结束，不读状态、不轮询点
        service.wait_idle(20.0)

        self.assertEqual(service.status(run_id).state, "completed")
        for n in MAGNETS:
            self.assertEqual(
                signals._gateway.read(f"magnet.m{n}.current_readback").value, 0.0
            )

    def test_retract_not_settling_keeps_the_device_locked(self) -> None:
        """回落没到位 = 设备可能停在半路：必须保留锁并要求人工确认。"""
        service, signals = self.build(build_group_config())
        # 只让 m1 跟随：电流写下去后 m2~m4 的回读不动，回落等不到位
        signals._gateway._coupling = {
            "magnet.m1.current_setpoint": "magnet.m1.current_readback"
        }

        status = self.wait(
            service,
            service.start(
                self.group_request(retract=RetractSpec(current_a=0.0, rate_a_s=2.0))
            ).run_id,
        )

        self.assertEqual(status.state, "recovery_required")
        self.assertIn("回落", status.message)
        self.assertNotEqual(self.locks.held(), {})
        # 数据已经落盘：谱图不该因为回落失败而消失
        self.assertIsNotNone(status.spectrum_id)

    def test_without_a_retract_spec_nothing_is_written_after_the_scan(self) -> None:
        service, signals = self.build(build_group_config())
        calls: list[str] = []
        original_write = signals._gateway.write

        def recording_write(signal, value, command_id):
            calls.append(signal)
            return original_write(signal, value, command_id)

        signals._gateway.write = recording_write

        status = self.wait(
            service, service.start(self.group_request(samples_per_point=1)).run_id
        )

        self.assertEqual(status.state, "completed")
        self.assertFalse([s for s in calls if s.endswith("CurrentRateSet")])

    def test_retract_can_be_disabled_per_run(self) -> None:
        service, signals = self.build(build_group_config())

        status = self.wait(
            service,
            service.start(
                self.group_request(
                    retract=RetractSpec(current_a=0.0, rate_a_s=2.0, auto=False)
                )
            ).run_id,
        )

        self.assertEqual(status.state, "completed")
        # 没回落：最后一点的目标值仍留在设备上
        self.assertEqual(
            signals._gateway.read("magnet.m1.current_readback").value, 120.0
        )

    # ---------------- 手动成组回落（走运行时，与端点同一入口） ----------------
    def test_runtime_retract_writes_every_magnet_and_waits_for_readback(self) -> None:
        runtime = InstrumentRuntime(
            build_group_config(),
            gateway_factory=create_simulated_gateway,
            store=self.store,
        )
        try:
            outcome = runtime.retract_magnets(
                [f"magnet.m{n}.current_setpoint" for n in MAGNETS], 0.0, 2.0, timeout_s=1.0
            )
            self.assertTrue(outcome.ok, outcome.message)
            self.assertEqual(len(outcome.applied), len(MAGNETS))
            for n in MAGNETS:
                self.assertEqual(
                    runtime.gateway().read(f"magnet.m{n}.current_readback").value, 0.0
                )
        finally:
            runtime.close()

    def test_runtime_retract_is_refused_while_a_task_holds_the_magnets(self) -> None:
        """别人占着磁铁组时不许偷写：回落也是设备动作，不能绕过设备锁。"""
        runtime = InstrumentRuntime(
            build_group_config(),
            gateway_factory=create_simulated_gateway,
            store=self.store,
        )
        try:
            runtime.locks.acquire({"磁铁电源"}, owner="scan-1")

            with self.assertRaises(DeviceBusy):
                runtime.retract_magnets(
                    [f"magnet.m{n}.current_setpoint" for n in MAGNETS], 0.0, 2.0
                )
        finally:
            runtime.close()

    # ---------------- 终态发布顺序 ----------------
    def test_terminal_state_is_in_the_store_before_status_publishes_it(self) -> None:
        """终态必须"先入库、后发布内存"。

        写库的这一刻内存状态还不该是 completed——反过来就会出现"内存已完成、
        库里还是 completing"的窗口（2026-09-14 的
        ``test_points_are_persisted_in_store`` 就撞在这个窗口上）。
        """
        service, _ = self.build()
        original_set_state = self.store.set_state
        observed: list[str] = []

        def probing_set_state(run_id: str, state: str) -> None:
            if state == "completed":
                observed.append(service.status(run_id).state)
            original_set_state(run_id, state)

        self.store.set_state = probing_set_state  # type: ignore[method-assign]

        status = self.wait(service, service.start(self.request()).run_id)

        self.assertEqual(status.state, "completed")
        self.assertEqual(observed, ["completing"])

    def test_store_failure_still_publishes_the_terminal_state(self) -> None:
        """写库失败不能让任务永远停在非终态：内存终态照发、且说明不一致。"""
        service, _ = self.build()
        original_set_state = self.store.set_state

        def failing_set_state(run_id: str, state: str) -> None:
            if state == "completed":
                raise OSError("磁盘已满")
            original_set_state(run_id, state)

        self.store.set_state = failing_set_state  # type: ignore[method-assign]

        status = self.wait(service, service.start(self.request()).run_id)

        self.assertEqual(status.state, "completed")
        self.assertIn("数据库状态未更新", status.message)
        self.assertEqual(self.locks.held(), {})

    def test_aborted_run_also_stores_the_state_before_publishing_it(self) -> None:
        service, _ = self.build()
        original_set_state = self.store.set_state
        observed: list[str] = []

        def probing_set_state(run_id: str, state: str) -> None:
            if state == "aborted":
                observed.append(service.status(run_id).state)
            original_set_state(run_id, state)

        self.store.set_state = probing_set_state  # type: ignore[method-assign]
        started = service.start(
            self.request(start=0.0, stop=500.0, step=1.0, dwell_s=0.01)
        )
        service.stop(started.run_id)
        status = self.wait(service, started.run_id)

        if status.state == "aborted":
            self.assertEqual(observed, ["running"])

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

    def test_safe_stop_uses_the_same_retract_policy_as_completion(self) -> None:
        service, signals = self.build(build_group_config())
        started = service.start(
            self.group_request(
                start=100.0,
                stop=500.0,
                step=1.0,
                dwell_s=0.01,
                retract=RetractSpec(current_a=0.0, rate_a_s=2.0),
            )
        )

        service.stop(started.run_id)
        status = self.wait(service, started.run_id)

        self.assertEqual(status.state, "aborted")
        self.assertIn("安全停止", status.message)
        for n in MAGNETS:
            self.assertEqual(
                signals._gateway.read(f"magnet.m{n}.current_readback").value,
                0.0,
            )

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

    def test_writable_signal_without_scan_capability_is_rejected(self) -> None:
        config = build_config()
        config.entries[0] = config.entries[0].model_copy(update={"scan_axis": False})
        service, _ = self.build(config)

        with self.assertRaises(ScanError) as caught:
            service.start(self.request())

        self.assertIn("未允许", str(caught.exception))

    def test_detector_without_scan_capability_is_rejected(self) -> None:
        config = build_config()
        config.entries[-1] = config.entries[-1].model_copy(update={"scan_detector": False})
        service, _ = self.build(config)

        with self.assertRaises(ScanError) as caught:
            service.start(self.request())

        self.assertIn("扫描探测器", str(caught.exception))

    def test_non_divisible_step_still_includes_requested_stop(self) -> None:
        targets = self.request(start=0.0, stop=1.0, step=0.6).targets()

        self.assertEqual(targets, [0.0, 0.6, 1.0])

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
