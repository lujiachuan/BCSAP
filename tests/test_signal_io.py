"""受控信号读写执行层：边界/单步/速率校验、斜坡、幂等与接口行为。

这些用例覆盖的都是**安全约束**，不是普通功能：任何一条放宽都会让越界值
直接下发到高压设备。因此断言写成「设备未被改动」，而不只是「返回了拒绝」。
"""

import unittest

from apps.instrument_service import pv_mapping
from apps.instrument_service.app import create_app
from apps.instrument_service.pv_health import create_simulated_gateway
from apps.instrument_service.runtime import InstrumentRuntime
from apps.instrument_service.signal_io import MAX_RAMP_STEPS, SignalWriteService, WriteRejected
from packages.contracts import (
    PvMappingConfig,
    PvMappingEntry,
    SignalSnapshotRequest,
    SignalWriteRequest,
)
from packages.epics_adapter import SimulatedEpicsGateway


def entry(
    signal: str = "gas.ar.flow_setpoint",
    label: str = "Ar 流量设定",
    pv: str = "Part1:Flow_W:CS200A:Setpoint",
    unit: str = "sccm",
    writable: bool = True,
    required: bool = False,
    **safety: object,
) -> PvMappingEntry:
    return PvMappingEntry(
        signal=signal,
        label=label,
        pv=pv,
        unit=unit,
        writable=writable,
        required=required,
        **safety,  # type: ignore[arg-type]
    )


def route_endpoint(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"未找到路由 {method} {path}")


class SignalWriteServiceTests(unittest.TestCase):
    """执行层校验：用内存网关，杜绝碰到真实 IOC。"""

    def setUp(self) -> None:
        # min/max 0..500、最大单步 50、速率 500/s —— 与现场气体配置同量级
        self.config = PvMappingConfig(
            version=1,
            entries=[
                entry(min_value=0.0, max_value=500.0, max_step=50.0, max_rate=500.0),
                entry(
                    signal="gas.ar.flow_readback",
                    label="Ar 瞬时流量",
                    pv="Part1:Flow_R:CS200A:InstantSCCM",
                    writable=False,
                    readback_signal="",
                ),
            ],
        )
        self.delays: list[float] = []
        # 显式给定初值，不依赖设备配置档里的现场观测值——否则断言会随
        # device_profiles.SIMULATED_VALUES 变动而失效。
        self.gateway = SimulatedEpicsGateway(
            {
                "gas.ar.flow_setpoint": (0.0, "sccm"),
                "gas.ar.flow_readback": (0.0, "sccm"),
            }
        )
        self.service = SignalWriteService(
            self.gateway,
            self.config,
            sleep=self.delays.append,
        )

    def write(self, value: float, **kwargs: object):
        return self.service.write(
            SignalWriteRequest(signal="gas.ar.flow_setpoint", value=value, **kwargs)  # type: ignore[arg-type]
        )

    # ---------------- 读取 ----------------
    def test_snapshot_returns_both_signals(self) -> None:
        snapshot = self.service.read_snapshot([])

        self.assertEqual(len(snapshot.readings), 2)
        self.assertTrue(all(r.connected for r in snapshot.readings))

    def test_unreadable_signal_is_marked_not_connected(self) -> None:
        """网关缺该信号时只把这一项标未连接，不能让整批读取失败。"""
        service = SignalWriteService(SimulatedEpicsGateway({}), self.config)

        snapshot = service.read_snapshot([])

        self.assertEqual([r.connected for r in snapshot.readings], [False, False])
        self.assertTrue(all(r.detail for r in snapshot.readings))

    # ---------------- 边界 ----------------
    def test_write_within_bounds_is_applied(self) -> None:
        result = self.write(120.0)

        self.assertTrue(result.accepted)
        self.assertEqual(result.applied, 120.0)
        self.assertEqual(result.previous, 0.0)
        self.assertEqual(self.service.read_value("gas.ar.flow_setpoint"), 120.0)

    def test_write_above_maximum_is_rejected_and_device_untouched(self) -> None:
        result = self.write(600.0)

        self.assertFalse(result.accepted)
        self.assertIn("超过上限", result.reason)
        self.assertEqual(self.service.read_value("gas.ar.flow_setpoint"), 0.0)

    def test_write_below_minimum_is_rejected_and_device_untouched(self) -> None:
        result = self.write(-5.0)

        self.assertFalse(result.accepted)
        self.assertIn("低于下限", result.reason)
        self.assertEqual(self.service.read_value("gas.ar.flow_setpoint"), 0.0)

    def test_read_only_signal_is_rejected(self) -> None:
        result = self.service.write(
            SignalWriteRequest(signal="gas.ar.flow_readback", value=1.0)
        )

        self.assertFalse(result.accepted)
        self.assertIn("只读", result.reason)

    def test_non_finite_value_is_rejected(self) -> None:
        self.assertFalse(self.write(float("nan")).accepted)
        self.assertFalse(self.write(float("inf")).accepted)

    def test_unknown_signal_raises(self) -> None:
        with self.assertRaises(WriteRejected):
            self.service.write(SignalWriteRequest(signal="nope.nope", value=1.0))

    def test_gateway_is_connected_before_first_read(self) -> None:
        """CA 网关不会自己建连：不先 connect() 读会报「网关尚未连接」。

        这条挡住了「服务起来后没跑过健康检查，第一次读就失败」的故障。
        """
        from packages.epics_adapter import SimulatedEpicsGateway

        class ConnectTracking(SimulatedEpicsGateway):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.connects = 0

            def connect(self) -> bool:
                self.connects += 1
                return True

        gateway = ConnectTracking({"gas.ar.flow_setpoint": (0.0, "sccm")})
        service = SignalWriteService(gateway, self.config, sleep=lambda _s: None)

        snapshot = service.read_snapshot(["gas.ar.flow_setpoint"])

        self.assertTrue(snapshot.readings[0].connected)
        self.assertGreaterEqual(gateway.connects, 1)

    def test_write_connects_before_validating_step(self) -> None:
        """写入要先读当前值来校验变化量，因此同样必须先建连。"""
        from packages.epics_adapter import SimulatedEpicsGateway

        class ConnectTracking(SimulatedEpicsGateway):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.connects = 0

            def connect(self) -> bool:
                self.connects += 1
                return True

        gateway = ConnectTracking({"gas.ar.flow_setpoint": (0.0, "sccm")})
        service = SignalWriteService(gateway, self.config, sleep=lambda _s: None)

        result = service.write(
            SignalWriteRequest(signal="gas.ar.flow_setpoint", value=30.0)
        )

        self.assertTrue(result.accepted)
        self.assertGreaterEqual(gateway.connects, 1)

    # ---------------- 单步与斜坡 ----------------
    def test_jump_beyond_max_step_without_ramp_is_rejected(self) -> None:
        result = self.write(400.0, ramp=False)

        self.assertFalse(result.accepted)
        self.assertIn("最大单步", result.reason)
        self.assertEqual(self.service.read_value("gas.ar.flow_setpoint"), 0.0)

    def test_jump_beyond_max_step_with_ramp_is_split(self) -> None:
        result = self.write(400.0)

        self.assertTrue(result.accepted)
        self.assertEqual(len(result.ramp_steps), 8)  # 400 / 50
        self.assertEqual(result.applied, 400.0)
        self.assertEqual(self.service.read_value("gas.ar.flow_setpoint"), 400.0)

    def test_every_ramp_step_respects_max_step(self) -> None:
        result = self.write(500.0)

        deltas = [
            abs(b - a)
            for a, b in zip([result.previous, *result.ramp_steps], result.ramp_steps)
        ]
        self.assertTrue(all(delta <= 50.0 + 1e-9 for delta in deltas), deltas)

    def test_ramp_steps_are_monotonic_towards_target(self) -> None:
        result = self.write(200.0)

        self.assertEqual(result.ramp_steps, sorted(result.ramp_steps))

    def test_step_delay_follows_max_rate(self) -> None:
        """每步变化 50 sccm、速率 500/s —— 间隔应为 0.1 s，且只在步间等待。"""
        self.write(150.0)  # 3 步

        self.assertEqual(len(self.delays), 2)
        for delay in self.delays:
            self.assertAlmostEqual(delay, 0.1, places=9)

    def test_single_step_write_does_not_sleep(self) -> None:
        self.write(30.0)

        self.assertEqual(self.delays, [])

    def test_excessive_step_count_is_rejected(self) -> None:
        """max_step 极小时步数会爆炸；宁可拒绝也不放宽单步约束。"""
        config = PvMappingConfig(
            version=1,
            entries=[entry(min_value=0.0, max_value=500.0, max_step=0.001)],
        )
        service = SignalWriteService(
            create_simulated_gateway(config), config, sleep=lambda _s: None
        )

        result = service.write(SignalWriteRequest(signal="gas.ar.flow_setpoint", value=500.0))

        self.assertFalse(result.accepted)
        self.assertIn(str(MAX_RAMP_STEPS), result.reason)

    # ---------------- 干跑与幂等 ----------------
    def test_dry_run_validates_without_writing(self) -> None:
        result = self.write(400.0, dry_run=True)

        self.assertTrue(result.accepted)
        self.assertIsNone(result.applied)
        self.assertEqual(self.service.read_value("gas.ar.flow_setpoint"), 0.0)

    def test_dry_run_still_reports_bounds_violation(self) -> None:
        self.assertFalse(self.write(900.0, dry_run=True).accepted)

    def test_same_command_id_does_not_write_twice(self) -> None:
        command = "11111111-2222-3333-4444-555555555555"
        first = self.write(100.0, command_id=command)
        second = self.write(300.0, command_id=command)

        self.assertEqual(first.applied, second.applied)
        self.assertEqual(self.service.read_value("gas.ar.flow_setpoint"), 100.0)

    def test_rejected_command_id_is_not_cached(self) -> None:
        """被拒绝的请求不该占据幂等位，否则修正参数重试会被旧结果挡住。"""
        command = "11111111-2222-3333-4444-555555555555"
        self.assertFalse(self.write(900.0, command_id=command).accepted)

        self.assertTrue(self.write(100.0, command_id=command).accepted)


class SettleTests(unittest.TestCase):
    """稳定判据：写入完成不等于设备稳定（架构文档 9.2）。"""

    @staticmethod
    def _service(**safety: object) -> SignalWriteService:
        config = PvMappingConfig(version=1, entries=[entry(**safety)])
        gateway = SimulatedEpicsGateway({"gas.ar.flow_setpoint": (0.0, "sccm")})
        return SignalWriteService(gateway, config, sleep=lambda _s: None)

    def test_wait_settled_returns_true_when_within_tolerance(self) -> None:
        service = self._service(
            min_value=0.0, max_value=500.0, settle_tol=5.0, settle_timeout=1.0
        )
        # 模拟网关写设定值即等于回读值，所以写入后必然落在容差内
        service.write(SignalWriteRequest(signal="gas.ar.flow_setpoint", value=100.0))

        settled, value = service.wait_settled(
            service.entry("gas.ar.flow_setpoint"), 100.0
        )

        self.assertTrue(settled)
        self.assertEqual(value, 100.0)

    def test_wait_settled_times_out_without_tolerance_met(self) -> None:
        service = self._service(
            min_value=0.0, max_value=500.0, settle_tol=1.0, settle_timeout=0.0
        )

        settled, value = service.wait_settled(
            service.entry("gas.ar.flow_setpoint"), 300.0
        )

        self.assertFalse(settled)
        self.assertEqual(value, 0.0)

    def test_no_tolerance_configured_counts_as_settled(self) -> None:
        service = self._service()

        settled, value = service.wait_settled(
            service.entry("gas.ar.flow_setpoint"), 123.0
        )

        self.assertTrue(settled)
        self.assertEqual(value, 0.0)


class SignalApiTests(unittest.TestCase):
    """接口层：读、写、以及「拒绝也返回 200 + accepted=False」的约定。"""

    def setUp(self) -> None:
        self.config = PvMappingConfig(
            version=1,
            entries=[
                entry(min_value=0.0, max_value=500.0, max_step=50.0),
                entry(
                    signal="detector.fc1.beam_current",
                    label="FC1 束流电流",
                    pv="BD:FC:01:BeamCurrent",
                    unit="nA",
                    writable=False,
                    required=True,
                ),
            ],
        )
        self.runtime = InstrumentRuntime(
            self.config, gateway_factory=create_simulated_gateway
        )
        self.app = create_app(self.runtime)
        self.read = route_endpoint(self.app, "/control/v1/signals/read", "POST")
        self.write = route_endpoint(self.app, "/control/v1/signals/write", "POST")

    def tearDown(self) -> None:
        self.runtime.close()

    def test_read_endpoint_returns_requested_signals(self) -> None:
        snapshot = self.read(
            SignalSnapshotRequest(signals=["detector.fc1.beam_current"])
        )

        self.assertEqual(len(snapshot.readings), 1)
        self.assertEqual(snapshot.readings[0].signal, "detector.fc1.beam_current")

    def test_read_endpoint_with_empty_list_returns_all(self) -> None:
        self.assertEqual(len(self.read(SignalSnapshotRequest()).readings), 2)

    def test_write_endpoint_applies_valid_value(self) -> None:
        result = self.write(
            SignalWriteRequest(signal="gas.ar.flow_setpoint", value=100.0)
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.applied, 100.0)

    def test_write_endpoint_reports_rejection_without_http_error(self) -> None:
        result = self.write(
            SignalWriteRequest(signal="gas.ar.flow_setpoint", value=900.0)
        )

        self.assertFalse(result.accepted)
        self.assertIn("超过上限", result.reason)

    def test_write_endpoint_rejects_read_only_signal(self) -> None:
        result = self.write(
            SignalWriteRequest(signal="detector.fc1.beam_current", value=1.0)
        )

        self.assertFalse(result.accepted)

    def test_mapping_change_rebuilds_signal_service(self) -> None:
        """热更新映射后，读写服务必须跟着换，否则会继续按旧映射写设备。"""
        original = self.runtime.signals
        self.runtime.apply(self.config)

        self.assertIsNot(self.runtime.signals, original)


class DefaultDeviceProfileTests(unittest.TestCase):
    """默认设备配置必须是团簇源，且能直接通过映射校验。"""

    def test_default_config_is_valid(self) -> None:
        self.assertEqual(pv_mapping.validate_config(pv_mapping.default_config()), [])

    def test_default_covers_cluster_source_groups(self) -> None:
        from apps.instrument_service import device_profiles

        groups = {e.group for e in pv_mapping.default_config().entries}
        self.assertEqual(groups, set(device_profiles.GROUP_ORDER))

    def test_on_site_limits_are_tighter_than_code_defaults(self) -> None:
        """现场把 JM/DW 其余通道收到 5100 V、主高压收到 50 kV —— 配置里必须是收紧后的值。"""
        entries = {e.signal: e for e in pv_mapping.default_config().entries}

        self.assertEqual(entries["ion_optics.focus.voltage_setpoint"].max_value, 5100.0)
        self.assertEqual(entries["hv_array.dw05.voltage_setpoint"].max_value, 5100.0)
        self.assertEqual(entries["hv_array.dw01.voltage_setpoint"].max_value, 200.0)
        self.assertEqual(entries["hv_array.dw02.voltage_setpoint"].max_value, 2500.0)
        self.assertEqual(entries["hv_bd.main.voltage_setpoint"].max_value, 50.0)
        self.assertEqual(entries["magnet.m1.current_rate_setpoint"].max_value, 10.0)

    def test_every_writable_signal_has_bounds(self) -> None:
        """可写信号必须有边界，否则等于允许越界写高压。"""
        unbounded = [
            e.signal
            for e in pv_mapping.default_config().entries
            if e.writable and e.max_value is None
        ]

        self.assertEqual(unbounded, [])

    def test_setpoints_are_paired_with_readback(self) -> None:
        entries = {e.signal: e for e in pv_mapping.default_config().entries}

        self.assertEqual(
            entries["gas.ar.flow_setpoint"].readback_signal, "gas.ar.flow_readback"
        )
        self.assertEqual(
            entries["magnet.m1.current_setpoint"].readback_signal,
            "magnet.m1.current_readback",
        )


if __name__ == "__main__":
    unittest.main()
