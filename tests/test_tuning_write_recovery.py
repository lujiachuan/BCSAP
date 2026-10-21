"""写入中断与"确认后继续"（现场反馈 2026-09-22）。

现场症状：跑到第 68 轮，DW4 的 caput 撞上 3 秒超时 → 整轮被判"设备状态未知" → 任务终止、
设备锁还握着；点「确认设备状态」只释放了锁，不会继续跑。

这三条是本节要钉住的底线：
1. **瞬时写入失败先回读确认**，只重试"设备确实没动"的情况（重写同一值是幂等的），
   回读已到位按成功处理，回读读不到或值变了仍然坚决停下——绝不拿旧认知继续写设备；
2. 重试用尽但设备确认停在原位时，这一轮按"写入未生效"记，**任务继续**而不是整任务终止；
3. 人工确认后**能继续**：服务端先逐路重读建立新起点，再回到运行；读不到就拒绝继续。
"""

from __future__ import annotations

import unittest

from apps.instrument_service.signal_io import SignalWriteResult
from apps.instrument_service.tuning_service import TuningError
from packages.contracts.tuning import TuningRunRequest, TuningVariable
from tests.test_tuning_logic_fixes import MAGNET, MAGNET_RB, _ServiceHarness


class _FlakySignalService:
    """包一层 SignalWriteService：让指定信号的写入按脚本失败（模拟 caput 超时）。

    ``corrupt_readback`` 用来模拟"命令可能已经生效了一部分"：失败的同时把回读
    改成第三个值，于是执行层既不能认定"设备没动"，也不能认定"已经到位"——
    这正是必须停下等人工确认的情形。
    """

    def __init__(
        self,
        inner,
        *,
        fail_signal: str,
        failures: int,
        corrupt_readback: tuple[str, float] | None = None,
    ):
        self._inner = inner
        self._fail_signal = fail_signal
        self._failures = failures
        self._corrupt_readback = corrupt_readback
        self.write_calls: list[str] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def write(self, request):
        self.write_calls.append(request.signal)
        if request.signal != self._fail_signal or self._failures <= 0:
            return self._inner.write(request)
        self._failures -= 1
        entry = self._inner.entry(request.signal)
        previous = self._inner.readback(entry)
        if self._corrupt_readback is not None:
            signal, value = self._corrupt_readback
            gateway = getattr(self._inner, "_gateway", None)
            if gateway is not None:
                gateway._values[signal] = (float(value), entry.unit)
        return SignalWriteResult(
            signal=request.signal,
            pv=entry.pv,
            unit=entry.unit,
            accepted=False,
            requested=float(request.value),
            previous=previous,
            ramp_steps=[],
            reason="写入设备失败：EPICS 命令执行超时（3 秒）",
            command_id=request.command_id,
            dry_run=request.dry_run,
            device_state_unknown=True,
            finished_at="2026-09-22T12:00:00.000000+00:00",
        )


def request_with_magnet(**overrides) -> TuningRunRequest:
    base = {
        "target_signal": "detector.fc1.beam_current",
        "variables": [
            TuningVariable(signal=MAGNET, label="磁铁1 电流", low=100.0, high=200.0)
        ],
        "max_iterations": 3,
        "settle_timeout_s": 0.2,
        "samples_per_point": 2,
    }
    base.update(overrides)
    return TuningRunRequest(**base)  # type: ignore[arg-type]


class WriteRetryTests(_ServiceHarness):
    """瞬时写入失败的处理策略。

    注意：用例把设备初始值设成 120 A（范围 [100,200] 的中点提案是 150 A），
    两者不同才能走到"回读未到位"的分支——若设备已经等于提案值，
    会先命中"回读已在目标容差内 = 响应丢失"这条更早的判据。
    """

    def _request(self, **overrides) -> TuningRunRequest:
        base = dict(
            loss_protection=False,
            max_iterations=3,
            samples_per_point=1,
            settle_timeout_s=1.0,
        )
        base.update(overrides)
        return request_with_magnet(**base)

    def _start(self, service, **overrides):
        self.set_device(120.0)
        self.set_target(10.0)
        return service.start(self._request(**overrides))

    def test_timeout_then_unchanged_readback_retries_and_succeeds(self) -> None:
        service = self.build()
        status = self._start(service, write_retries=2)
        flaky = _FlakySignalService(self.signals, fail_signal=MAGNET, failures=1)
        service._signals_provider = lambda: flaky

        after = service.approve(status.run_id)

        self.assertEqual(after.state, "awaiting_confirmation", "重试成功后应正常进入下一轮")
        record = service.iterations(status.run_id).iterations[0]
        self.assertEqual(record.quality, "ok")
        messages = " ".join(event.message for event in service.events(status.run_id).events)
        self.assertIn("设备未动，重试 1/2", messages)

    def test_timeout_with_readback_at_target_is_treated_as_success(self) -> None:
        service = self.build()
        status = self._start(service, write_retries=2)
        run = service._require(status.run_id)
        target = run.pending.values[MAGNET]
        # 设备其实已经写进去了（响应丢了）
        self.gateway._values[MAGNET_RB] = (float(target), "A")
        flaky = _FlakySignalService(self.signals, fail_signal=MAGNET, failures=1)
        service._signals_provider = lambda: flaky

        after = service.approve(status.run_id)

        self.assertEqual(after.state, "awaiting_confirmation")
        record = service.iterations(status.run_id).iterations[0]
        self.assertEqual(record.quality, "ok")
        messages = " ".join(event.message for event in service.events(status.run_id).events)
        self.assertIn("判定为响应丢失", messages)
        self.assertEqual(flaky.write_calls.count(MAGNET), 1, "已在目标值内就不该再写一次")

    def test_timeout_with_unknown_readback_still_stops_for_a_human(self) -> None:
        service = self.build()
        status = self._start(service, write_retries=2)
        # 失败的同时回读变成第三个值（既不是原值也不是目标）→ 设备状态未知
        flaky = _FlakySignalService(
            self.signals, fail_signal=MAGNET, failures=1,
            corrupt_readback=(MAGNET_RB, 999.0),
        )
        service._signals_provider = lambda: flaky

        after = service.approve(status.run_id)

        self.assertEqual(after.state, "recovery_required")
        self.assertTrue(after.locked_groups, "状态未知时必须保留设备锁")
        messages = " ".join(event.message for event in service.events(status.run_id).events)
        self.assertIn("设备状态未知", messages)

    def test_retries_exhausted_with_device_unchanged_keeps_the_run_going(self) -> None:
        """重试用尽但设备确认没动：本轮记 write_rejected，**任务不终止**。"""
        service = self.build()
        status = self._start(service, write_retries=1)
        flaky = _FlakySignalService(self.signals, fail_signal=MAGNET, failures=9)
        service._signals_provider = lambda: flaky

        after = service.approve(status.run_id)

        self.assertEqual(after.state, "awaiting_confirmation", "瞬时写入失败不该终止整个任务")
        record = service.iterations(status.run_id).iterations[0]
        self.assertEqual(record.quality, "write_rejected")
        self.assertIn("已回读确认设备停在原位", record.detail or "")
        self.assertNotEqual(self.locks.held(), {}, "任务继续时设备锁仍应握在手里")

    def test_retry_can_be_switched_off(self) -> None:
        service = self.build()
        status = self._start(service, write_retries=0)
        flaky = _FlakySignalService(self.signals, fail_signal=MAGNET, failures=1)
        service._signals_provider = lambda: flaky

        after = service.approve(status.run_id)

        self.assertEqual(after.state, "awaiting_confirmation")
        record = service.iterations(status.run_id).iterations[0]
        self.assertEqual(record.quality, "write_rejected")
        self.assertEqual(flaky.write_calls.count(MAGNET), 1, "关闭重试后只写一次")


class AcknowledgeResumeTests(_ServiceHarness):
    """确认设备状态之后能不能继续。"""

    def _stuck_run(self, service):
        """制造一个"写入中断 → 需人工确认"的任务。"""
        self.set_device(120.0)
        self.set_target(10.0)
        status = service.start(
            request_with_magnet(
                loss_protection=False, max_iterations=5, samples_per_point=1,
                write_retries=0, settle_timeout_s=1.0,
            )
        )
        flaky = _FlakySignalService(
            self.signals, fail_signal=MAGNET, failures=1,
            corrupt_readback=(MAGNET_RB, 999.0),
        )
        service._signals_provider = lambda: flaky
        after = service.approve(status.run_id)
        assert after.state == "recovery_required", after.state
        return status.run_id

    def test_acknowledge_without_resume_only_releases_the_lock(self) -> None:
        service = self.build()
        run_id = self._stuck_run(service)

        after = service.acknowledge_recovery(run_id, note="现场看过")

        self.assertEqual(after.state, "recovery_required")
        self.assertEqual(self.locks.held(), {})

    def test_acknowledge_with_resume_reads_fresh_state_and_continues(self) -> None:
        service = self.build()
        run_id = self._stuck_run(service)
        # 现场把设备摆到一个新的位置（既不是原值也不是目标）
        self.gateway._values[MAGNET_RB] = (170.0, "A")
        service._signals_provider = lambda: self.signals  # 写入恢复正常

        after = service.acknowledge_recovery(run_id, note="现场已确认", resume=True)

        self.assertIn(after.state, ("running", "awaiting_confirmation"))
        self.assertTrue(self.locks.held(), "继续调束必须重新握住设备锁")
        run = service._require(run_id)
        self.assertEqual(run.held_values[MAGNET], 170.0, "新的起点应是当前实际回读")
        self.assertEqual(run.anomalies, 0)
        self.assertIsNone(run.finished_at)
        messages = " ".join(event.message for event in service.events(run_id).events)
        self.assertIn("重读全部参与变量建立新起点", messages)
        self.assertIn("170", messages)

    def test_resume_is_refused_when_a_readback_is_still_unavailable(self) -> None:
        service = self.build()
        run_id = self._stuck_run(service)
        self.gateway._values.pop(MAGNET_RB, None)

        with self.assertRaises(TuningError) as caught:
            service.acknowledge_recovery(run_id, resume=True)

        self.assertIn("不能继续", str(caught.exception))
        after = service.status(run_id)
        self.assertEqual(after.state, "recovery_required")
        self.assertEqual(self.locks.held(), {}, "拒绝继续时不该把锁又拿在手里不放")

    def test_resume_keeps_the_pre_run_snapshot_as_the_rollback_anchor(self) -> None:
        service = self.build()
        run_id = self._stuck_run(service)
        before = dict(service.status(run_id).snapshot)
        self.gateway._values[MAGNET_RB] = (170.0, "A")
        service._signals_provider = lambda: self.signals

        after = service.acknowledge_recovery(run_id, resume=True)

        self.assertEqual(after.snapshot, before, "启动前快照是回退锚点，不能被现场值覆盖")


if __name__ == "__main__":
    unittest.main()
