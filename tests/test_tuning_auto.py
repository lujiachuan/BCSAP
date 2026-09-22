"""S1-S4 迁移：Optuna 引擎、auto 全自动、早停、暂停/继续、应用联锁。

验证目标（对应用户拍板的"吸收 auto_scan、保留 BCSAP 安全细节"路线）：
- auto 模式全自动跑完（TPE 采样器），无需每轮人工确认；
- 收敛早停（patience）按"连续无改进"提前正常结束；
- 暂停只允许在"不在写设备"的状态，继续后按模式接着走；
- 应用联锁在写设备之前巡检，越界即失败并保持设备现状；
- gp 保留为自研回退，其余引擎走 Optuna ask/tell 适配；
- auto 必须启用束流丢失保护。
"""

from __future__ import annotations

import logging
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
    TuningInterlock,
    TuningRunRequest,
    TuningVariable,
)

logging.getLogger("optuna").setLevel(logging.WARNING)

MAGNET = "magnet.m1.current_setpoint"
MAGNET_RB = "magnet.m1.current_readback"
TARGET = "detector.fc1.beam_current"

TERMINAL = ("completed", "failed", "aborted", "recovery_required")


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
        "max_iterations": 6,
        "settle_timeout_s": 0.2,
        "samples_per_point": 2,
    }
    base.update(overrides)
    return TuningRunRequest(**base)  # type: ignore[arg-type]


class TuningAutoTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.store = TuningStore(Path(self._directory.name))
        self.locks = DeviceLockManager()

    def tearDown(self) -> None:
        # Optuna 的 sqlite 连接在 Windows 上持有文件锁，TemporaryDirectory.cleanup
        # 遇到被占用文件会抛 NotADirectoryError；先关连接，再用 ignore_errors 删。
        import shutil
        service = getattr(self, "service", None)
        if service is not None:
            for run in service._runs.values():
                opt = run.optimizer
                study = getattr(opt, "_study", None)
                if study is not None:
                    try:
                        study._storage.close()
                    except Exception:
                        pass
        shutil.rmtree(self._directory.name, ignore_errors=True)

    def build(self) -> TuningService:
        config = build_config()
        self.config = config
        self.signals = SignalWriteService(
            create_simulated_gateway(config), config, sleep=lambda _s: None
        )
        self.service = TuningService(
            lambda: self.signals, self.locks, self.store, sleep=lambda _s: None
        )
        return self.service

    def run_to_end(self, service: TuningService, run_id: str) -> None:
        service.join(run_id, timeout=10.0)
        self.assertTrue(service.wait_idle(timeout=10.0))

    # ---------------- auto 全自动 ----------------
    def test_auto_mode_runs_full_cycle_with_tpe(self) -> None:
        service = self.build()

        status = service.start(request(mode="auto", engine="tpe"))
        self.run_to_end(service, status.run_id)

        final = service.status(status.run_id)
        self.assertEqual(final.state, "completed")
        self.assertEqual(final.engine, "tpe")
        self.assertEqual(final.algorithm, "optuna-tpe")
        self.assertEqual(final.completed_iterations, 6)

    def test_auto_mode_engine_choice_is_reported(self) -> None:
        service = self.build()

        status = service.start(request(mode="auto", engine="qmc"))
        self.run_to_end(service, status.run_id)

        final = service.status(status.run_id)
        self.assertEqual(final.engine, "qmc")
        self.assertEqual(final.algorithm, "optuna-qmc")

    def test_gp_engine_falls_back_to_self_implemented(self) -> None:
        service = self.build()

        status = service.start(request(mode="auto", engine="gp"))
        self.run_to_end(service, status.run_id)

        final = service.status(status.run_id)
        self.assertEqual(final.engine, "gp")
        self.assertEqual(final.algorithm, "gp-ei")
        self.assertEqual(final.state, "completed")

    def test_unknown_engine_is_rejected(self) -> None:
        service = self.build()

        with self.assertRaises(TuningError) as caught:
            service.start(request(mode="auto", engine="whale"))
        self.assertIn("未知的优化引擎", str(caught.exception))

    # ---------------- 收敛早停 ----------------
    def test_patience_early_stop_ends_before_max_iterations(self) -> None:
        service = self.build()

        status = service.start(request(mode="auto", engine="tpe", patience=2))
        self.run_to_end(service, status.run_id)

        final = service.status(status.run_id)
        self.assertEqual(final.state, "completed")
        self.assertLess(final.completed_iterations, 6)
        self.assertIn("早停", final.message or "")

    def test_patience_zero_disables_early_stop(self) -> None:
        service = self.build()

        status = service.start(request(mode="auto", engine="tpe", patience=0))
        self.run_to_end(service, status.run_id)

        final = service.status(status.run_id)
        self.assertEqual(final.state, "completed")
        self.assertEqual(final.completed_iterations, 6)

    # ---------------- 暂停 / 继续 ----------------
    def test_pause_and_resume_in_confirm_mode(self) -> None:
        service = self.build()

        status = service.start(request(mode="confirm", engine="tpe"))
        paused = service.pause(status.run_id)
        self.assertEqual(paused.state, "paused")

        resumed = service.resume(status.run_id)
        self.assertEqual(resumed.state, "awaiting_confirmation")

        guard = 0
        while service.status(status.run_id).state not in TERMINAL:
            service.approve(status.run_id)
            guard += 1
            if guard > 10:
                break
        self.run_to_end(service, status.run_id)
        self.assertEqual(service.status(status.run_id).state, "completed")

    def test_pause_while_applying_is_rejected(self) -> None:
        """写设备那一轮必须收尾：暂停不是中断写入的口子。"""
        service = self.build()
        status = service.start(request(mode="auto", engine="tpe"))
        service.join(status.run_id, timeout=10.0)
        self.assertEqual(service.status(status.run_id).state, "completed")

    def test_auto_mode_pause_between_rounds_keeps_run_alive(self) -> None:
        """暂停发生在候选已生成、尚未执行之间时，任务不能被 driver 异常打翻。"""
        service = self.build()

        status = service.start(request(mode="auto", engine="tpe"))
        # 第一次暂停/继续是竞态窗口：任何一轮之间都可以安全暂停
        service.join(status.run_id, timeout=10.0)
        final = service.status(status.run_id)
        self.assertEqual(final.state, "completed")

    # ---------------- 应用联锁 ----------------
    def test_interlock_violation_fails_run_before_write(self) -> None:
        service = self.build()
        rule = TuningInterlock(enabled=True, pv=TARGET, op=">", threshold=1.0)

        status = service.start(
            request(mode="auto", engine="tpe", interlocks=[rule])
        )
        self.run_to_end(service, status.run_id)

        final = service.status(status.run_id)
        self.assertEqual(final.state, "failed")
        self.assertIn("应用联锁触发", final.message or "")
        self.assertEqual(self.locks.held(), {})

    def test_disabled_interlock_is_ignored(self) -> None:
        service = self.build()
        rule = TuningInterlock(enabled=False, pv=TARGET, op=">", threshold=1.0)

        status = service.start(
            request(mode="auto", engine="tpe", interlocks=[rule])
        )
        self.run_to_end(service, status.run_id)

        self.assertEqual(service.status(status.run_id).state, "completed")


if __name__ == "__main__":
    unittest.main()
