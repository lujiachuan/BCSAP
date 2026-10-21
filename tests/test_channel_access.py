"""真实 EPICS 通道访问的集成测试（对本地 softIoc 做端到端往返）。

需要本机同时具备：
* EPICS base 的 ``softIoc.exe``（用 EPICS_BASE/EPICS_HOST_ARCH 定位）
* 可加载的 ``ca.dll``（EPICS base 的 mingw 构建常因缺 libgcc 无法加载，
  此时会自动回退到同盘的官方 CA 分发目录）

两者缺一时整组跳过，因此在没有 EPICS 的机器上不会失败。

安全说明：测试把 ``EPICS_CA_ADDR_LIST`` 指向 127.0.0.1，且用**独立的 CA 服务
端口**（``TEST_CA_PORT``），因此既不会碰到任何真实束线设备，也不会误连开发机上
已经跑着的模拟 IOC。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from apps.instrument_service import pv_mapping
from apps.instrument_service.runtime import InstrumentRuntime
from packages.contracts import PvMappingConfig, PvMappingEntry
from packages.epics_adapter import (
    ChannelAccessGateway,
    ca_library_candidates,
    load_ca_library,
)

PV_Q1 = "BL:Q1:ISET"
PV_Q2 = "BL:Q2:ISET"
PV_DET = "BL:DET:CURRENT"
PV_MISSING = "BL:NO:SUCH:PV"

# 测试专用 CA 服务端口。**不能沿用默认 5064**：开发机上常常已经跑着一个模拟
# IOC（``sim/ioc.db``，提供的是 Part1:*/BD:* 记录）。两者共用端口时，本测试会
# 静默连到那个 IOC 上，然后因为找不到 BL:* 记录而报出一堆与代码无关的失败。
TEST_CA_PORT = "5164"

# 记录初始值，用于断言读到的是 IOC 里的真值而不是默认值
Q1_INITIAL = 1.842
DET_INITIAL = 8.31

TEST_DB = f"""record(ai, "{PV_Q1}") {{
    field(VAL, "{Q1_INITIAL}")
    field(EGU, "A")
    field(PREC, "3")
}}
record(ao, "{PV_Q2}") {{
    field(VAL, "0")
    field(EGU, "A")
    field(PREC, "3")
}}
record(ai, "{PV_DET}") {{
    field(VAL, "{DET_INITIAL}")
    field(EGU, "uA")
    field(PREC, "3")
}}
"""


def _soft_ioc() -> Path | None:
    override = os.environ.get("SPECTRUM_TEST_SOFTIOC")
    if override and Path(override).is_file():
        return Path(override)
    base = os.environ.get("EPICS_BASE")
    arch = os.environ.get("EPICS_HOST_ARCH")
    if not base:
        return None
    candidates = []
    if arch:
        candidates.append(Path(base) / "bin" / arch / "softIoc.exe")
    candidates.append(Path(base) / "bin" / "softIoc.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


SOFT_IOC = _soft_ioc()


def require_ca_library(test: unittest.TestCase) -> None:
    """健康检查会把 ca.dll 加载失败降级成逐项 detail，因此需单独探测一次以便跳过。"""
    try:
        load_ca_library(None)
    except ConnectionError as exc:
        test.skipTest(f"本机没有可加载的 ca.dll：{exc}")


@unittest.skipUnless(SOFT_IOC is not None, "本机没有 EPICS softIoc.exe")
class ChannelAccessIntegrationTests(unittest.TestCase):
    ioc: subprocess.Popen
    gateway: ChannelAccessGateway
    _directory: tempfile.TemporaryDirectory
    _env_backup: dict[str, str | None]

    @classmethod
    def setUpClass(cls) -> None:
        cls._directory = tempfile.TemporaryDirectory()
        db_path = Path(cls._directory.name) / "test.db"
        db_path.write_text(TEST_DB, encoding="utf-8")

        cls._env_backup = {
            key: os.environ.get(key)
            for key in (
                "EPICS_CA_ADDR_LIST",
                "EPICS_CA_AUTO_ADDR_LIST",
                "EPICS_CA_SERVER_PORT",
            )
        }
        os.environ["EPICS_CA_ADDR_LIST"] = "127.0.0.1"
        os.environ["EPICS_CA_AUTO_ADDR_LIST"] = "NO"
        # 客户端与服务端在同一个进程环境里，设一次即可：子进程 IOC 用它绑定，
        # 本进程的 CA 客户端用它作为搜索目标端口。
        os.environ["EPICS_CA_SERVER_PORT"] = TEST_CA_PORT

        # softIoc 一旦读到 stdin EOF 就退出，必须持有它的 stdin 管道；
        # 同时加 -S 不起交互 shell，避免依赖 stdin 存活。
        cls.ioc = subprocess.Popen(
            [str(SOFT_IOC), "-S", "-d", str(db_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(4.0)
        if cls.ioc.poll() is not None:
            cls._restore_env()
            cls._directory.cleanup()
            raise unittest.SkipTest("本地 softIoc 启动后立即退出")
        cls._require_test_ioc()

    @classmethod
    def _require_test_ioc(cls) -> None:
        """确认连到的确实是本测试自己起的 IOC，否则带原因跳过而不是报假失败。"""
        try:
            gateway = ChannelAccessGateway(
                paths={"q1": PV_Q1}, units={"q1": "A"}, connect_timeout=5.0
            )
            gateway.connect()
        except ConnectionError as exc:
            raise unittest.SkipTest(f"本机没有可加载的 ca.dll：{exc}") from exc
        try:
            reading = gateway.read("q1")
        except Exception as exc:  # noqa: BLE001  连不上就是环境问题，跳过
            raise unittest.SkipTest(
                f"测试 IOC 不可达（CA 端口 {TEST_CA_PORT}）：{exc}"
            ) from exc
        finally:
            gateway.close()
        if not reading.connected:
            raise unittest.SkipTest(
                f"CA 端口 {TEST_CA_PORT} 上读不到 {PV_Q1}："
                "该端口可能已被其它 IOC 占用，测试无法隔离运行"
            )

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "ioc") and cls.ioc.poll() is None:
            cls.ioc.terminate()
            try:
                cls.ioc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.ioc.kill()
        cls._restore_env()
        cls._directory.cleanup()

    @classmethod
    def _restore_env(cls) -> None:
        for key, value in cls._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _new_gateway(self, paths: dict[str, str], units: dict[str, str] | None = None):
        gateway = ChannelAccessGateway(
            paths=paths, units=units or {}, lib_dir=None, connect_timeout=10.0
        )
        try:
            gateway.connect()
        except ConnectionError as exc:
            gateway.close()
            self.skipTest(f"本机没有可加载的 ca.dll：{exc}")
        return gateway

    def test_read_returns_value_from_real_ioc(self) -> None:
        gateway = self._new_gateway({"q1": PV_Q1, "det": PV_DET}, {"q1": "A", "det": "uA"})
        self.addCleanup(gateway.close)

        q1 = gateway.read("q1")
        det = gateway.read("det")

        self.assertTrue(q1.connected)
        self.assertAlmostEqual(q1.value, Q1_INITIAL, places=6)
        self.assertEqual(q1.unit, "A")          # 单位来自受控配置
        self.assertGreater(q1.source_time.year, 2000)  # EPICS 纪元已换算
        self.assertEqual(det.unit, "uA")
        self.assertAlmostEqual(det.value, DET_INITIAL, places=6)

    def test_write_then_read_round_trip(self) -> None:
        gateway = self._new_gateway({"q2": PV_Q2}, {"q2": "A"})
        self.addCleanup(gateway.close)

        target = -0.875
        gateway.write("q2", target, uuid.uuid4())

        self.assertAlmostEqual(gateway.read("q2").value, target, places=6)

    def test_snapshot_reads_every_signal(self) -> None:
        gateway = self._new_gateway({"q1": PV_Q1, "q2": PV_Q2})
        self.addCleanup(gateway.close)

        snapshot = gateway.snapshot(["q1", "q2"])

        self.assertEqual(set(snapshot), {"q1", "q2"})
        self.assertTrue(all(reading.connected for reading in snapshot.values()))

    def test_missing_pv_is_reported_as_disconnected_without_raising(self) -> None:
        gateway = self._new_gateway({"q1": PV_Q1, "missing": PV_MISSING})
        self.addCleanup(gateway.close)

        reading = gateway.read("missing")

        self.assertFalse(reading.connected)
        self.assertTrue(gateway.read("q1").connected)

    def test_write_to_missing_pv_raises_connection_error(self) -> None:
        gateway = self._new_gateway({"missing": PV_MISSING})
        self.addCleanup(gateway.close)

        with self.assertRaises(ConnectionError):
            gateway.write("missing", 1.0, uuid.uuid4())

    def test_unknown_signal_rejected(self) -> None:
        gateway = self._new_gateway({"q1": PV_Q1})
        self.addCleanup(gateway.close)

        with self.assertRaises(KeyError):
            gateway.read("not.configured")

    def test_health_check_reports_per_pv_state(self) -> None:
        config = PvMappingConfig(
            version=1,
            entries=[
                PvMappingEntry(
                    signal="quadrupole.q1.current",
                    label="Q1 电流",
                    pv=PV_Q1,
                    unit="A",
                    writable=True,
                    required=True,
                ),
                PvMappingEntry(
                    signal="detector.current",
                    label="探测器电流",
                    pv=PV_MISSING,
                    unit="uA",
                    writable=False,
                    required=False,
                ),
            ],
        )
        runtime = InstrumentRuntime(config)
        self.addCleanup(runtime.close)
        require_ca_library(self)

        health = runtime.check_health()

        self.assertEqual(health.summary.total, 2)
        self.assertEqual(health.summary.connected, 1)
        by_signal = {item.signal: item for item in health.items}
        self.assertTrue(by_signal["quadrupole.q1.current"].connected)
        self.assertTrue(by_signal["quadrupole.q1.current"].writable)
        self.assertFalse(by_signal["detector.current"].connected)
        # 只有非必需 PV 掉线 → degraded，而不是整机不可用
        self.assertEqual(health.status, "degraded")

    def test_editing_mapping_changes_which_pv_is_used(self) -> None:
        """核心诉求：改映射里的 PV 名，健康检查就去读新的 PV。"""
        original = pv_mapping.default_config()
        runtime = InstrumentRuntime(original)
        self.addCleanup(runtime.close)
        require_ca_library(self)

        updated = original.model_copy(
            update={
                "entries": [
                    PvMappingEntry(
                        signal="quadrupole.q1.current",
                        label="Q1 电流",
                        pv=PV_Q1,
                        unit="A",
                        writable=True,
                        required=True,
                    )
                ],
            }
        )
        runtime.apply(updated)

        health = runtime.check_health()

        self.assertEqual(len(health.items), 1)
        self.assertEqual(health.items[0].pv, PV_Q1)
        self.assertTrue(health.items[0].connected)


class CaLibraryCandidateTests(unittest.TestCase):
    def test_path_directory_with_ca_dll_is_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "ca.dll").touch()
            with mock.patch.dict(os.environ, {"PATH": directory}, clear=True):
                self.assertIn(Path(directory), ca_library_candidates())


@unittest.skipUnless(SOFT_IOC is not None, "本机没有 EPICS softIoc.exe")
class CaLibraryDiscoveryTests(unittest.TestCase):
    def test_discovery_finds_a_loadable_ca_library(self) -> None:
        """mingw 构建的 ca.dll 加载失败时必须继续尝试后续候选。"""
        try:
            _library, directory = load_ca_library(None)
        except ConnectionError as exc:
            self.skipTest(f"本机没有可加载的 ca.dll：{exc}")

        self.assertTrue((Path(directory) / "ca.dll").is_file())

    def test_missing_configured_directory_reports_all_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(ConnectionError) as caught:
                load_ca_library(empty)

        self.assertIn("ca.dll", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
