"""成组写入：批量校验、批量锁定、以及"部分成功"必须被说清楚。

现场语义（改造报告 §4.2）：成组动作不能由界面循环调单点接口完成——
第三台失败会留下"前两台已经动了"的中间状态。这里断言的就是：
**校验不过就一次都不写**、执行途中失败要**逐路报清**、成组期间**别人抢不到设备组**。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from apps.instrument_service.runtime import InstrumentRuntime
from apps.instrument_service.scan_store import ScanStore
from packages.contracts import SignalWriteRequest

MAGNETS = (1, 2, 3, 4)
SETPOINTS = [f"magnet.m{n}.current_setpoint" for n in MAGNETS]
READBACKS = [f"magnet.m{n}.current_readback" for n in MAGNETS]


def setpoint_entry(n: int) -> dict:
    return {
        "signal": f"magnet.m{n}.current_setpoint",
        "label": f"磁铁{n} 电流设定",
        "pv": f"BD:DipoleMagnet:{n:02d}:CurrentSet",
        "unit": "A",
        "writable": True,
        "required": False,
        "group": "磁铁电源",
        "role": "setpoint",
        "readback_signal": f"magnet.m{n}.current_readback",
        "min_value": 0.0,
        "max_value": 600.0,
        "max_step": 100.0,
        # 成组语义与速率限制无关；把速率放大让斜坡几乎不等待，
        # 否则每个用例都要为真实的斜坡间隔等上好几秒（速率限制另有专门用例）
        "max_rate": 1e6,
        "settle_tol": 0.5,
        "settle_timeout": 5.0,
        "tunable": True,
    }


def readback_entry(n: int) -> dict:
    return {
        "signal": f"magnet.m{n}.current_readback",
        "label": f"磁铁{n} 电流回读",
        "pv": f"BD:DipoleMagnet:{n:02d}:CurrentMonitor",
        "unit": "A",
        "writable": False,
        "required": False,
        "group": "磁铁电源",
        "role": "readback",
    }


CONFIG = {
    "version": 1,
    "entries": [setpoint_entry(n) for n in MAGNETS]
    + [readback_entry(n) for n in MAGNETS],
}


def batch(values: list[float]) -> list[SignalWriteRequest]:
    return [
        SignalWriteRequest(signal=signal, value=value, ramp=True)
        for signal, value in zip(SETPOINTS, values, strict=True)
    ]


class BatchWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        from apps.instrument_service.pv_health import create_simulated_gateway
        from packages.contracts import PvMappingConfig

        self.config = PvMappingConfig.model_validate(CONFIG)
        self.runtime = InstrumentRuntime(
            self.config,
            gateway_factory=create_simulated_gateway,
            store=ScanStore(Path(self._directory.name)),
        )

    def tearDown(self) -> None:
        self.runtime.close()
        self._directory.cleanup()

    def readback(self, n: int) -> float:
        return float(self.runtime.gateway().read(f"magnet.m{n}.current_readback").value)

    # ---------------- 整批成功 ----------------
    def test_all_members_are_written_and_reported(self) -> None:
        response = self.runtime.write_batch(batch([100.0, 100.0, 100.0, 100.0]), note="1~4 同步")

        self.assertTrue(response.ok, response.message)
        self.assertEqual((response.requested, response.accepted, response.rejected), (4, 4, 0))
        self.assertEqual([r.signal for r in response.results], SETPOINTS)
        self.assertIn("1~4 同步", response.message)
        for n in MAGNETS:
            self.assertEqual(self.readback(n), 100.0)

    # ---------------- 原子校验：一次都不写 ----------------
    def test_one_bad_member_blocks_the_whole_batch(self) -> None:
        """第 4 台超上限 → 整批不下发，前三台必须保持不动。"""
        before = [self.readback(n) for n in MAGNETS]

        response = self.runtime.write_batch(batch([100.0, 100.0, 100.0, 900.0]))

        self.assertFalse(response.ok)
        self.assertTrue(response.nothing_written)
        self.assertEqual(response.accepted, 0)
        self.assertIn("整批未下发", response.message)
        self.assertIn("超过上限", response.message)
        self.assertEqual([self.readback(n) for n in MAGNETS], before)

    def test_unknown_signal_blocks_the_whole_batch(self) -> None:
        response = self.runtime.write_batch(
            [
                SignalWriteRequest(signal=SETPOINTS[0], value=100.0),
                SignalWriteRequest(signal="magnet.m9.current_setpoint", value=100.0),
            ]
        )

        self.assertFalse(response.ok)
        self.assertTrue(response.nothing_written)
        self.assertIn("magnet.m9.current_setpoint", response.message)

    def test_read_only_member_blocks_the_whole_batch(self) -> None:
        response = self.runtime.write_batch(
            [
                SignalWriteRequest(signal=SETPOINTS[0], value=100.0),
                SignalWriteRequest(signal=READBACKS[0], value=1.0),
            ]
        )

        self.assertFalse(response.ok)
        self.assertTrue(response.nothing_written)
        self.assertIn("只读", response.message)

    def test_without_atomic_the_good_ones_still_go(self) -> None:
        """atomic=False 是显式选择：允许"能写的写、写不了的报出来"。"""
        response = self.runtime.write_batch(
            batch([100.0, 100.0, 100.0, 900.0]), atomic=False
        )

        self.assertFalse(response.ok)
        self.assertFalse(response.nothing_written)
        self.assertEqual(response.accepted, 3)
        self.assertEqual(response.rejected, 1)
        self.assertIn("部分成功", response.message)
        self.assertEqual(self.readback(1), 100.0)

    def test_empty_batch_is_rejected(self) -> None:
        response = self.runtime.write_batch([])

        self.assertFalse(response.ok)
        self.assertIn("至少要有一路", response.message)

    # ---------------- 抢不到设备组 ----------------
    def test_batch_is_refused_while_another_task_holds_the_group(self) -> None:
        self.runtime.locks.acquire({"磁铁电源"}, owner="scan-1")
        before = [self.readback(n) for n in MAGNETS]

        response = self.runtime.write_batch(batch([100.0, 100.0, 100.0, 100.0]))

        self.assertFalse(response.ok)
        self.assertTrue(response.nothing_written)
        self.assertIn("设备组当前不可用", response.message)
        self.assertEqual([self.readback(n) for n in MAGNETS], before)
        self.assertEqual(self.runtime.locks.held(), {"磁铁电源": "scan-1"})

    def test_batch_releases_the_group_afterwards(self) -> None:
        self.runtime.write_batch(batch([100.0, 100.0, 100.0, 100.0]))

        self.assertEqual(self.runtime.locks.held(), {})

    # ---------------- 执行途中失败：逐路报清 ----------------
    def test_runtime_failure_mid_batch_is_reported_per_signal(self) -> None:
        original = self.runtime.gateway()
        calls: list[str] = []

        class FailsOnThird:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def write(self, signal, value, command_id):
                calls.append(signal)
                if signal == SETPOINTS[2]:
                    raise OSError("CA 写入超时")
                return self._inner.write(signal, value, command_id)

        self.runtime._gateway = FailsOnThird(original)  # type: ignore[attr-defined]
        self.runtime._signals = None

        response = self.runtime.write_batch(batch([120.0, 120.0, 120.0, 120.0]))

        self.assertFalse(response.ok)
        self.assertFalse(response.nothing_written, "这是执行途中失败，不是整批没写")
        self.assertEqual(response.accepted, 3)
        self.assertEqual(response.rejected, 1)
        self.assertIn(SETPOINTS[2], response.message)
        self.assertIn("部分成功", response.message)
        # 前两台已经动了：必须逐路标明，不能只报一句"失败"
        self.assertTrue(response.results[0].accepted)
        self.assertFalse(response.results[2].accepted)


if __name__ == "__main__":
    unittest.main()
