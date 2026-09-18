"""EPICS 命令行后备网关验证。"""

import subprocess
import unittest
from uuid import uuid4

from packages.epics_adapter import CommandLineEpicsGateway, FailoverEpicsGateway


class _UnavailableGateway:
    def connect(self) -> bool:
        raise ConnectionError("ca.dll 位数不兼容")

    def close(self) -> None:
        return None


class CommandLineEpicsGatewayTests(unittest.TestCase):
    def test_reads_with_caget_and_writes_with_caput(self) -> None:
        calls: list[list[str]] = []

        def runner(args, **_kwargs):
            calls.append(args)
            output = "20\n" if args[0] == "caget.exe" else ""
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

        gateway = CommandLineEpicsGateway(
            {"gas.ar.flow_setpoint": "Part1:Flow_W:CS200A:Setpoint"},
            {"gas.ar.flow_setpoint": "sccm"},
            caget_path="caget.exe",
            caput_path="caput.exe",
            runner=runner,
        )

        reading = gateway.read("gas.ar.flow_setpoint")
        written = gateway.write("gas.ar.flow_setpoint", 21.0, uuid4())

        self.assertEqual(reading.value, 20.0)
        self.assertEqual(reading.unit, "sccm")
        self.assertEqual(written.value, 21.0)
        self.assertEqual(calls[0], ["caget.exe", "-t", "-n", "Part1:Flow_W:CS200A:Setpoint"])
        self.assertEqual(calls[1], ["caput.exe", "-t", "Part1:Flow_W:CS200A:Setpoint", "21"])

    def test_snapshot_reads_multiple_pvs_in_one_process(self) -> None:
        calls: list[list[str]] = []

        def runner(args, **_kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(
                args, 0, stdout="PV:ONE 20\nPV:TWO 21\n", stderr=""
            )

        gateway = CommandLineEpicsGateway(
            {"one": "PV:ONE", "two": "PV:TWO"},
            caget_path="caget.exe",
            caput_path="caput.exe",
            runner=runner,
        )

        readings = gateway.snapshot(["one", "two"])

        self.assertEqual(readings["one"].value, 20.0)
        self.assertEqual(readings["two"].value, 21.0)
        self.assertEqual(calls, [["caget.exe", "-n", "PV:ONE", "PV:TWO"]])

    def test_snapshot_keeps_connected_values_when_one_pv_is_missing(self) -> None:
        gateway = CommandLineEpicsGateway(
            {"one": "PV:ONE", "missing": "PV:MISSING", "two": "PV:TWO"},
            caget_path="caget.exe",
            caput_path="caput.exe",
            runner=lambda args, **_kwargs: subprocess.CompletedProcess(
                args,
                1,
                stdout="PV:ONE 20\nPV:TWO 21\n",
                stderr="Channel connect timed out: 'PV:MISSING' not found.\n",
            ),
        )

        readings = gateway.snapshot(["one", "missing", "two"])

        self.assertTrue(readings["one"].connected)
        self.assertFalse(readings["missing"].connected)
        self.assertTrue(readings["two"].connected)

    def test_snapshot_falls_back_per_signal_when_batch_returns_nothing(self) -> None:
        """真机 caget 实测行为：批量里只要有一路离线，整批一个值都不返回、
        只报 ``some PV(s) not found``。此时必须分组隔离坏路，不能串行启动
        与 PV 数量相同的单路进程。"""

        calls: list[list[str]] = []

        def runner(args, **_kwargs):
            calls.append(args)
            requested = args[2:]
            if "PV:MISSING" in requested:
                return subprocess.CompletedProcess(
                    args,
                    1,
                    stdout="",
                    stderr="Channel connect timed out: some PV(s) not found.\n",
                )
            values = {"PV:ONE": 20, "PV:TWO": 21}
            return subprocess.CompletedProcess(
                args, 0,
                stdout="".join(f"{pv} {values[pv]}\n" for pv in requested),
                stderr="",
            )

        gateway = CommandLineEpicsGateway(
            {"one": "PV:ONE", "missing": "PV:MISSING", "two": "PV:TWO"},
            caget_path="caget.exe",
            caput_path="caput.exe",
            runner=runner,
        )

        readings = gateway.snapshot(["one", "missing", "two"])

        self.assertEqual(readings["one"].value, 20.0)
        self.assertTrue(readings["one"].connected)
        self.assertFalse(readings["missing"].connected)
        self.assertEqual(readings["two"].value, 21.0)
        self.assertTrue(readings["two"].connected)
        self.assertLess(len(calls), 10)
        self.assertIn("not found", readings["missing"].detail)

    def test_all_missing_snapshot_has_a_hard_process_limit(self) -> None:
        calls: list[list[str]] = []

        def runner(args, **_kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(
                args, 1, stdout="", stderr="some PV(s) not found"
            )

        paths = {f"s{index}": f"PV:{index}" for index in range(128)}
        gateway = CommandLineEpicsGateway(
            paths,
            caget_path="caget.exe",
            caput_path="caput.exe",
            runner=runner,
        )

        readings = gateway.snapshot(list(paths))

        self.assertEqual(len(calls), gateway.MAX_SNAPSHOT_COMMANDS)
        self.assertTrue(all(not reading.connected for reading in readings.values()))
        self.assertTrue(all(reading.detail for reading in readings.values()))

    def test_snapshot_cache_reuses_a_recent_batch(self) -> None:
        calls: list[list[str]] = []
        now = [100.0]

        def runner(args, **_kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(
                args, 0, stdout="PV:ONE 20\nPV:TWO 21\n", stderr=""
            )

        gateway = CommandLineEpicsGateway(
            {"one": "PV:ONE", "two": "PV:TWO"},
            caget_path="caget.exe",
            caput_path="caput.exe",
            runner=runner,
            clock=lambda: now[0],
        )

        gateway.snapshot(["one", "two"])
        gateway.snapshot(["one", "two"])
        self.assertEqual(len(calls), 1)

        now[0] += gateway.SNAPSHOT_CACHE_S + 0.01
        gateway.snapshot(["one", "two"])
        self.assertEqual(len(calls), 2)

    def test_offline_signal_is_isolated_until_its_retry_window(self) -> None:
        calls: list[list[str]] = []
        now = [100.0]
        missing_available = [False]
        values = {"PV:ONE": 20, "PV:MISSING": 9, "PV:TWO": 21}

        def runner(args, **_kwargs):
            calls.append(args)
            requested = args[2:]
            if "PV:MISSING" in requested and not missing_available[0]:
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="some PV(s) not found"
                )
            return subprocess.CompletedProcess(
                args, 0,
                stdout="".join(f"{pv} {values[pv]}\n" for pv in requested),
                stderr="",
            )

        gateway = CommandLineEpicsGateway(
            {"one": "PV:ONE", "missing": "PV:MISSING", "two": "PV:TWO"},
            caget_path="caget.exe",
            caput_path="caput.exe",
            runner=runner,
            clock=lambda: now[0],
        )

        first = gateway.snapshot(["one", "missing", "two"])
        self.assertFalse(first["missing"].connected)
        first_call_count = len(calls)

        now[0] += gateway.SNAPSHOT_CACHE_S + 0.01
        second = gateway.snapshot(["one", "missing", "two"])
        self.assertFalse(second["missing"].connected)
        self.assertEqual(
            calls[first_call_count:],
            [["caget.exe", "-n", "PV:ONE", "PV:TWO"]],
        )

        missing_available[0] = True
        now[0] += gateway.OFFLINE_RETRY_S + 0.01
        recovered = gateway.snapshot(["one", "missing", "two"])
        self.assertTrue(recovered["missing"].connected)
        self.assertEqual(recovered["missing"].value, 9.0)

    def test_successful_write_invalidates_snapshot_cache(self) -> None:
        calls: list[list[str]] = []

        def runner(args, **_kwargs):
            calls.append(args)
            output = "PV:ONE 20\n" if args[0] == "caget.exe" else ""
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

        gateway = CommandLineEpicsGateway(
            {"one": "PV:ONE"},
            caget_path="caget.exe",
            caput_path="caput.exe",
            runner=runner,
        )

        gateway.snapshot(["one"])
        gateway.write("one", 21.0, uuid4())
        gateway.snapshot(["one"])

        self.assertEqual([call[0] for call in calls], [
            "caget.exe", "caput.exe", "caget.exe",
        ])

    def test_falls_back_only_when_primary_connect_fails(self) -> None:
        fallback = CommandLineEpicsGateway(
            {"signal": "PV:ONE"},
            caget_path="caget.exe",
            caput_path="caput.exe",
            runner=lambda args, **_kwargs: subprocess.CompletedProcess(
                args, 0, stdout="3.5\n", stderr=""
            ),
        )
        gateway = FailoverEpicsGateway(_UnavailableGateway(), fallback, "signal")

        self.assertTrue(gateway.connect())
        self.assertEqual(gateway.read("signal").value, 3.5)


class CreateGatewayBackendTests(unittest.TestCase):
    """执行服务生产网关固定只用 caget/caput。"""

    @staticmethod
    def _config():
        from packages.contracts import PvMappingConfig, PvMappingEntry

        return PvMappingConfig(
            version=1,
            entries=[
                PvMappingEntry(
                    signal="one",
                    label="一号",
                    pv="PV:ONE",
                    unit="",
                    writable=True,
                    required=True,
                )
            ],
        )

    def test_default_backend_is_command_line_only(self) -> None:
        from apps.instrument_service.pv_health import create_gateway

        gateway = create_gateway(self._config())

        self.assertIsInstance(gateway, CommandLineEpicsGateway)


if __name__ == "__main__":
    unittest.main()
