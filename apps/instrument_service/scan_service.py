"""扫谱任务状态机与逐点采集流程。

架构依据（文档 6.4 状态机 / 6.5 逐点流程 / 9.2 稳定性 / 9.3 停止语义）：

每个点的顺序固定为
    计算目标值 → 边界检查 → 写设定值 → 等读回稳定 → 按积分规则读探测信号
    → 保存实际坐标/信号/时间/质量 → 发布进度

三条不可让步的规则：

1. **写完成不等于稳定**（9.2）。每点都等回读进入容差才采样；未稳定时按
   ``on_unsettled`` 决定「记下并标注」还是「判失败」，不静默当成功。
2. **响应丢失 = 结果未知**（9.3）。写入抛错时设备可能已经动了，此时进入
   ``RECOVERY_REQUIRED`` 而不是 ``FAILED``，并且**不自动重试**。
3. **横坐标存实际读回值**（6.5），不是下发过的设定值。

同一时刻只允许一个扫谱任务（运行中判定 + 设备组锁），避免两个任务争抢同一路磁场。
"""

from __future__ import annotations

import statistics
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from packages.contracts import SignalWriteRequest
from packages.contracts.scan import (
    ScanPoint,
    ScanPointsResponse,
    ScanRunRequest,
    ScanRunStatus,
)
from packages.domain.scan import ScanState, transition_scan

from .device_locks import DeviceBusy, DeviceLockManager
from .retract import RetractOutcome, retract_signals
from .scan_store import ScanStore, StoredSpectrum
from .signal_io import SignalWriteService, WriteRejected

# 点数上限：参数算错时宁可拒绝，也不要让服务去跑一场几小时的扫描
MAX_SCAN_POINTS = 20000
# 进入 RUNNING 前的连接探测：冷启动时 CA 首次读取可能尚未建连而失败，
# 直接开跑会让任务在第 1 点以「当前值读取失败」告终，看不出是连接问题
PREPARE_ATTEMPTS = 3
PREPARE_RETRY_DELAY = 0.5
# 终态：不再推进，也不再占锁
TERMINAL_STATES = frozenset(
    {ScanState.COMPLETED, ScanState.ABORTED, ScanState.FAILED, ScanState.RECOVERY_REQUIRED}
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ScanError(RuntimeError):
    """扫谱请求不可受理（参数、映射或设备占用问题）。"""


@dataclass
class _Run:
    """一个扫谱任务的运行态。"""

    run_id: str
    request: ScanRunRequest
    targets: list[float]
    state: ScanState = ScanState.DRAFT
    message: str = ""
    points: list[ScanPoint] = field(default_factory=list)
    locked_groups: list[str] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    spectrum: StoredSpectrum | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class ScanService:
    """扫谱任务的生命周期管理与逐点执行。"""

    def __init__(
        self,
        signals_provider: Callable[[], SignalWriteService],
        locks: DeviceLockManager,
        store: ScanStore,
        *,
        sleep=time.sleep,
    ) -> None:
        """``signals_provider`` 而不是信号服务实例：PV 映射热更新会换掉网关与
        读写服务，但进行中的扫谱任务不能因此被换掉——运行中的任务应当继续用
        它启动时的那套映射，而新任务用新映射。"""
        self._signals_provider = signals_provider
        self._locks = locks
        self._store = store
        self._sleep = sleep
        self._lock = threading.RLock()
        self._runs: dict[str, _Run] = {}

    @property
    def _signals(self) -> SignalWriteService:
        return self._signals_provider()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def status(self, run_id: str) -> ScanRunStatus:
        run = self._require(run_id)
        with self._lock:
            return self._status_of(run)

    def points(self, run_id: str, since: int = 0) -> ScanPointsResponse:
        run = self._require(run_id)
        with self._lock:
            fresh = [p for p in run.points if p.index >= since]
            return ScanPointsResponse(
                run_id=run_id, points=fresh, total_points=len(run.targets)
            )

    def active_run_id(self) -> str | None:
        with self._lock:
            for run_id, run in self._runs.items():
                if run.state not in TERMINAL_STATES:
                    return run_id
        return None

    def run_ids(self) -> list[str]:
        with self._lock:
            return list(self._runs)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """等待全部任务线程**真正结束**。

        状态进入终态 ≠ 线程已退出：终态是在线程内部设置的，之后它还要写一次
        数据库。只等状态就去看文件（清理暂存目录、备份、关服务）会撞上
        「文件仍被占用」——Win32 上尤其明显。
        """
        with self._lock:
            threads = [run.thread for run in self._runs.values() if run.thread]
        deadline = time.monotonic() + timeout
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)
        return not any(thread.is_alive() for thread in threads)

    def _require(self, run_id: str) -> _Run:
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            raise ScanError(f"没有这个扫谱任务：{run_id}")
        return run

    def _status_of(self, run: _Run) -> ScanRunStatus:
        spectrum = run.spectrum
        return ScanRunStatus(
            run_id=run.run_id,
            state=str(run.state),
            label=run.request.axis.label,
            detector_signal=run.request.detector_signal,
            total_points=len(run.targets),
            completed_points=len(run.points),
            message=run.message,
            started_at=run.started_at,
            finished_at=run.finished_at,
            spectrum_id=spectrum.spectrum_id if spectrum else None,
            spectrum_path=str(spectrum.path) if spectrum else None,
            sha256=spectrum.sha256 if spectrum else None,
            point_count=spectrum.point_count if spectrum else None,
            # 报「当前实际占着哪些组」，不是「曾经声明过哪些组」：
            # 终态后仍然列出旧分组，会让诊断端以为设备还锁着而不敢启动新任务
            locked_groups=self._held_by(run.run_id),
        )

    def _held_by(self, run_id: str) -> list[str]:
        return sorted(
            group
            for group, owner in self._locks.held().items()
            if owner == run_id
        )

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    def start(self, request: ScanRunRequest) -> ScanRunStatus:
        """校验参数与设备占用，然后启动后台执行线程。"""
        with self._lock:
            active = self.active_run_id()
            if active is not None:
                raise ScanError(f"已有扫谱任务在进行中：{active}")
            targets = self._validated_targets(request)
            run = _Run(
                run_id=str(uuid4()),
                request=request,
                targets=targets,
                state=ScanState.VALIDATING,
                message="参数校验通过，准备占用设备",
            )
            self._runs[run.run_id] = run

        self._store.create_run(
            run.run_id, request.axis.label, request.detector_signal,
            request.axis.model_dump(), _now(),
        )
        self._store.set_state(run.run_id, str(run.state))

        run.thread = threading.Thread(
            target=self._run_loop, args=(run,), name=f"scan-{run.run_id[:8]}", daemon=True
        )
        run.thread.start()
        return self.status(run.run_id)

    def _channel_readbacks(self, axis) -> list[str]:
        """每一路设定信号用来判到位的回读信号。

        取映射里该设定条目自己的 ``readback_signal``；没配就退到扫描轴的
        ``readback_signal``，最后才退到该条目自己的 PV（与
        ``SignalWriteService.readback`` 同一约定）。

        **不逐路配回读就看不全**：几路共用同一个来源时，``readbacks`` 里只剩一个键、
        偏差恒为 0，等于没有逐路信息——真实映射（设备档案）给四台磁铁各自配了
        ``readback_signal``，所以成组扫描天然能逐路核对。
        """
        return [
            self._signals.entry(signal).readback_signal
            or axis.readback_signal
            or signal
            for signal in axis.setpoint_signals
        ]

    def _validated_targets(self, request: ScanRunRequest) -> list[float]:
        """参数校验：信号必须存在、设定信号必须可写、点数不能离谱。"""
        if not request.axis.setpoint_signals:
            raise ScanError("扫描轴至少要有一路设定信号")
        try:
            allowed_readbacks: set[str] = set()
            for signal in request.axis.setpoint_signals:
                entry = self._signals.entry(signal)
                if not entry.writable:
                    raise ScanError(f"扫描设定信号不可写：{signal}")
                if not entry.scan_axis:
                    raise ScanError(f"设备档案未允许该信号作为扫描轴：{signal}")
                allowed_readbacks.add(entry.readback_signal or signal)
            self._signals.entry(request.axis.readback_signal)
            if request.axis.readback_signal not in allowed_readbacks:
                raise ScanError(
                    "扫描坐标回读必须来自所选扫描轴："
                    f"{request.axis.readback_signal}"
                )
            detector = self._signals.entry(request.detector_signal)
            if not detector.scan_detector or detector.writable:
                raise ScanError(
                    f"设备档案未允许该信号作为扫描探测器：{request.detector_signal}"
                )
        except WriteRejected as exc:
            raise ScanError(str(exc)) from exc
        if request.samples_per_point < 1:
            raise ScanError("每点采样次数至少为 1")
        if request.dwell_s < 0:
            raise ScanError("驻留时间不能为负")
        if request.on_unsettled not in {"record", "fail"}:
            raise ScanError("on_unsettled 只能是 record 或 fail")
        # 稳定判据必须存在：没有判据时 wait_settled 会直接判「已稳定」，
        # 于是每个点都在没到位的情况下被采样，谱图整体错位且看不出来。
        # 成组扫描要**逐路**都有判据：只要有一路缺判据，那一路就等于不检查，
        # 而它恰恰是用来看守"其余磁铁有没有跟上"的。
        if request.settle_tol is None:
            missing = [
                signal
                for signal in request.axis.setpoint_signals
                if self._signals.entry(signal).settle_tol is None
            ]
            if missing:
                raise ScanError(
                    "扫描轴缺少回读稳定判据（settle_tol）：无法判断设备是否到位。"
                    f"缺少判据的设定信号：{'、'.join(missing)}。"
                    "请在设备配置的设定条目上配置 settle_tol，或在请求里显式给出。"
                )
        try:
            return request.targets(limit=MAX_SCAN_POINTS)
        except ValueError as exc:
            raise ScanError(str(exc)) from exc

    # ------------------------------------------------------------------
    # 逐点执行
    # ------------------------------------------------------------------
    def _run_loop(self, run: _Run) -> None:
        try:
            with self._lock:
                self._to(run, ScanState.PREPARING, "申请设备控制权")

            groups = self._groups_for(run.request)
            try:
                self._locks.acquire(groups, owner=run.run_id)
            except DeviceBusy as exc:
                # 拿不到设备就不启动（文档 6.3），保持 FAILED 终态即可：
                # 任务从未开始，设备也没被这个任务动过
                self._finish_failed(run, str(exc))
                return
            run.locked_groups = sorted(groups)

            # 连接探测：文档 6.4 的 PREPARING → FAILED 就是为「连接或准备失败」准备的
            problem = self._prepare(run.request)
            if problem is not None:
                self._finish_failed(run, problem)
                return

            with self._lock:
                run.started_at = _now()
                self._to(run, ScanState.RUNNING, "采集中")
            self._store.set_state(run.run_id, str(run.state))

            for index, target in enumerate(run.targets):
                if run.stop_event.is_set():
                    self._finish_aborted(run)
                    return
                point = self._acquire_point(run, index, target)
                if point is None:
                    return  # 已进入终态
                with self._lock:
                    run.points.append(point)
                self._store.append_point(run.run_id, point)

            with self._lock:
                self._to(run, ScanState.COMPLETING, "本地持久化")
            self._store.set_state(run.run_id, str(run.state))
            self._finish_completed(run)
        except Exception as exc:  # noqa: BLE001  未预期异常也必须落到明确终态
            with self._lock:
                if run.state in TERMINAL_STATES:
                    return
                state = self._next_state(
                    run, ScanState.FAILED, ScanState.RECOVERY_REQUIRED
                )
            problem = self._store_terminal_state(run, state)
            with self._lock:
                self._to(run, ScanState.FAILED, f"任务异常：{exc}")
                self._to(
                    run, ScanState.RECOVERY_REQUIRED,
                    "异常中止，设备实际状态需人工确认",
                )
                run.finished_at = _now()
                self._note_store_problem(run, problem)
        finally:
            # RECOVERY_REQUIRED 时**故意不释放锁**：设备实际状态未知，
            # 放行下一个任务等于让它在未知状态上继续驱动磁场。
            # 由人工确认后调用 acknowledge_recovery 释放（文档 6.3）。
            if run.state != ScanState.RECOVERY_REQUIRED:
                self._locks.release(owner=run.run_id)

    def _run_signals(self, request: ScanRunRequest) -> list[str]:
        """任务要读写的全部信号：各路设定、**各路回读**、探测器（去重保序）。

        成组扫描时回读不止一路，连接探测与设备组都要覆盖到它们：只探测第一路
        回读的话，别的磁铁回读掉线要等到逐点等稳定时才发现（那时已经在写了）。
        """
        signals = [
            *request.axis.setpoint_signals,
            *self._channel_readbacks(request.axis),
            request.detector_signal,
        ]
        return list(dict.fromkeys(signals))

    def _prepare(self, request: ScanRunRequest) -> str | None:
        """进入 RUNNING 前确认要用的信号都读得到；返回 None 表示就绪。

        带重试：CA 首次读取在连接刚建立时可能失败一次，这是环境特性而不是
        设备故障。但**不能**把它当成「读不到就继续跑」——探测不过就按
        PREPARING → FAILED 结束，并且没有任何点被写过，设备是干净的。
        """
        needed = self._run_signals(request)
        last = ""
        for attempt in range(PREPARE_ATTEMPTS):
            snapshot = self._signals.read_snapshot(needed)
            broken = [r for r in snapshot.readings if not r.connected]
            if not broken:
                return None
            last = "；".join(
                f"{r.signal}（{r.detail or '未连接'}）" for r in broken
            )
            if attempt < PREPARE_ATTEMPTS - 1:
                self._sleep(PREPARE_RETRY_DELAY)
        return f"设备连接未就绪，任务未开始（未写入任何设定值）：{last}"

    def _groups_for(self, request: ScanRunRequest) -> set[str]:
        """任务需要的设备组：扫描设定信号所在组 + 各路回读/探测器所在组。"""
        groups: set[str] = set()
        for signal in self._run_signals(request):
            group = self._signals.entry(signal).group
            if group:
                groups.add(group)
        return groups

    def _acquire_point(self, run: _Run, index: int, target: float) -> ScanPoint | None:
        """采一个点。返回 None 表示任务已进入终态、不应继续。"""
        request = run.request
        axis = request.axis

        # 1) 写设定值（执行层做边界/单步/速率校验）
        for signal in axis.setpoint_signals:
            result = self._signals.write(
                SignalWriteRequest(signal=signal, value=target, ramp=True)
            )
            if not result.accepted:
                self._finish_failed(
                    run,
                    f"第 {index + 1} 点写入 {signal} 被拒：{result.reason}",
                    state_unknown=result.device_state_unknown,
                )
                return None

        # 2) 等读回稳定（写完成 ≠ 设备稳定）
        #    成组扫描**逐路**判到位：只等第一路时，"第一台到位、其余卡住"会被判成
        #    好点——坐标与偏差全来自第一台，谱图看起来正常而磁场是歪的。
        #    稳定容差取自各路的**设定条目**（设备属性），回读来源用该条目自己的回读。
        readbacks: dict[str, float] = {}
        unsettled: list[str] = []
        for signal, source in zip(
            axis.setpoint_signals, self._channel_readbacks(axis), strict=True
        ):
            settled, value = self._signals.wait_settled(
                self._signals.entry(signal),
                target,
                tol=request.settle_tol,
                timeout=request.settle_timeout_s,
                readback_signal=source,
            )
            if value is not None:
                readbacks[source] = float(value)
            if not settled:
                unsettled.append(source)

        coordinate = readbacks.get(axis.readback_signal)
        if coordinate is None:
            # 坐标来源那一路读不到：退用第一路有效读数，最后才退用目标值
            coordinate = next(iter(readbacks.values()), None)

        if unsettled and request.on_unsettled == "fail":
            self._finish_failed(
                run, f"第 {index + 1} 点回读未稳定（{'、'.join(unsettled)}）"
            )
            return None

        # 3) 按积分规则读探测信号：重复采样取中位数抗单次毛刺
        samples: list[float] = []
        for _ in range(request.samples_per_point):
            value = self._signals.read_value(request.detector_signal)
            if value is not None:
                samples.append(value)
            if request.dwell_s:
                self._sleep(request.dwell_s / request.samples_per_point)

        with self._lock:
            run.message = f"采集第 {index + 1}/{len(run.targets)} 点"

        detail = self._point_detail(readbacks, unsettled)

        if not samples:
            # 探测读不到：记下这个点但不进谱图——把 0 当成真实强度会污染分析
            return ScanPoint(
                index=index,
                target=target,
                coordinate=float(coordinate if coordinate is not None else target),
                signal=0.0,
                quality="read_failed",
                at=_now(),
                coordinate_spread=self._spread(readbacks),
                readback_values=readbacks,
                included=False,
                detail=f"{request.detector_signal} 无有效读数"
                + (f"；{detail}" if detail else ""),
            )

        return ScanPoint(
            index=index,
            target=target,
            coordinate=float(coordinate if coordinate is not None else target),
            signal=float(statistics.median(samples)),
            quality="ok" if not unsettled else "unsettled",
            at=_now(),
            coordinate_spread=self._spread(readbacks),
            readback_values=readbacks,
            detail=detail,
        )

    @staticmethod
    def _point_detail(readbacks: dict[str, float], unsettled: list[str]) -> str | None:
        """点明细：未到位的路 + 各路实际回读（成组时一眼看出是哪台没跟上）。"""
        parts: list[str] = []
        if unsettled:
            parts.append(f"未进入容差：{'、'.join(unsettled)}")
        if len(readbacks) > 1:
            parts.append(
                "各路回读 " + "、".join(f"{name}={value:g}" for name, value in readbacks.items())
            )
        return "；".join(parts) or None

    @staticmethod
    def _spread(readbacks: dict[str, float]) -> float:
        """成组扫描时各路**实际回读**的最大差值；单路或不足两路有效读数时为 0。

        以前这里读的是各路**设定信号**的 PV（即 ``CurrentSet``），不是回读
        （``CurrentMonitor``）：设定值由我们自己写下去，天然一致，于是偏差恒为 0，
        这个字段等于没有。
        """
        if len(readbacks) < 2:
            return 0.0
        values = list(readbacks.values())
        return float(max(values) - min(values))

    # ------------------------------------------------------------------
    # 终态
    # ------------------------------------------------------------------
    def _store_terminal_state(self, run: _Run, state: ScanState) -> str:
        """先把终态写进数据库，返回错误文本（空串表示成功）。

        **顺序不能反**：``status()`` 只读内存，数据库状态才是"任务已完成"的持久
        事实（上传、清理、备份都按它走）。先发布内存再写库，会留下一个"内存已完成、
        库里还是 completing"的窗口——测试与任何直读 SQLite 的组件都会读到旧状态
        （2026-09-14 的 ``test_points_are_persisted_in_store`` 就是撞在这个窗口上）。

        写库失败时**仍然要发布内存终态**：否则任务永远不算结束、设备组锁也不释放。
        不一致会写进 message，便于现场发现。
        """
        try:
            self._store.set_state(run.run_id, str(state))
        except Exception as exc:  # noqa: BLE001  写库失败不能让任务卡在非终态
            return f"（数据库状态未更新：{exc}）"
        return ""

    @staticmethod
    def _note_store_problem(run: _Run, problem: str) -> None:
        if problem:
            run.message = f"{run.message}{problem}"

    @staticmethod
    def _next_state(run: _Run, target: ScanState, *then: ScanState) -> ScanState:
        """按转换表算出将要到达的终态，**不改内存状态**。

        为了"先入库、后发布"，终态必须在写库之前就知道；直接调 ``_to`` 会把
        内存状态提前改掉，窗口就又回来了。转换是否合法仍然由 ``transition_scan``
        判定，与 ``_to`` 用同一张表。
        """
        state = transition_scan(run.state, target)
        for step in then:
            state = transition_scan(state, step)
        return state

    def _finish_failed(self, run: _Run, message: str, *, state_unknown: bool = False) -> None:
        # 设备状态明确时才交还设备，且**先释放再公布终态**：
        # 否则客户端刚看到 failed 就发起新任务，会撞上自己还占着的设备组
        if not state_unknown:
            self._locks.release(owner=run.run_id)
        with self._lock:
            if run.state in TERMINAL_STATES:
                return
            state = self._next_state(
                run,
                ScanState.FAILED,
                *((ScanState.RECOVERY_REQUIRED,) if state_unknown else ()),
            )
        problem = self._store_terminal_state(run, state)
        with self._lock:
            self._to(run, ScanState.FAILED, message)
            if state_unknown:
                # 设备可能已经动了：不能按 FAILED 收尾，必须人工确认
                self._to(
                    run, ScanState.RECOVERY_REQUIRED,
                    f"{message}（下发中断，设备实际状态未知，请人工确认）",
                )
            run.finished_at = _now()
            self._note_store_problem(run, problem)

    def _finish_aborted(self, run: _Run) -> None:
        # 安全停止与正常完成使用同一回落策略；失败时保留锁等待人工确认。
        outcome = self._retract(run)
        if outcome is not None and not outcome.ok:
            self._finish_failed(run, f"停止后的回落失败：{outcome.message}", state_unknown=True)
            return
        self._locks.release(owner=run.run_id)
        with self._lock:
            state = run.state
            if state == ScanState.RUNNING:
                state = transition_scan(state, ScanState.STOP_REQUESTED)
            if state == ScanState.STOP_REQUESTED:
                state = transition_scan(state, ScanState.ABORTED)
        problem = self._store_terminal_state(run, state)
        with self._lock:
            if run.state == ScanState.RUNNING:
                self._to(run, ScanState.STOP_REQUESTED, "收到停止请求")
            if run.state == ScanState.STOP_REQUESTED:
                suffix = f"；{outcome.message}" if outcome is not None else ""
                self._to(run, ScanState.ABORTED, f"已安全停止{suffix}")
            run.finished_at = _now()
        # 停止前的点也是真实数据，尽力保存；保存失败不改判终态
        try:
            stored = self._persist_points(run)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                run.message = f"{run.message}；部分数据未能保存：{exc}{problem}"
            return
        with self._lock:
            run.spectrum = stored
            run.message = f"{run.message}，保存了 {stored.point_count} 点{problem}"

    def _finish_completed(self, run: _Run) -> None:
        try:
            stored = self._persist_points(run)
        except Exception as exc:  # noqa: BLE001  持久化失败不能让任务看起来成功
            self._finish_failed(run, f"本地持久化失败：{exc}")
            return
        with self._lock:
            run.spectrum = stored  # 数据先落定：回落失败也不该让谱图找不到
        # 设备安全收尾：谱图已经保存，接着把磁场退到安全值，再宣布完成
        outcome = self._retract(run)
        if outcome is not None and not outcome.ok:
            # 回落没到位时设备可能停在半路：保留设备锁、转恢复待确认
            self._finish_failed(
                run, f"回落失败：{outcome.message}", state_unknown=True
            )
            return
        # 数据已落盘、设备已回到安全值 → 交还设备
        self._locks.release(owner=run.run_id)
        # 先入库、后发布内存状态（见 _store_terminal_state）
        problem = self._store_terminal_state(run, ScanState.COMPLETED)
        suffix = f"；{outcome.message}" if outcome is not None else ""
        with self._lock:
            run.spectrum = stored
            self._to(
                run,
                ScanState.COMPLETED,
                f"完成，已保存 {stored.point_count} 点{suffix}",
            )
            run.finished_at = _now()
            self._note_store_problem(run, problem)

    def _retract(self, run: _Run) -> RetractOutcome | None:
        """完成后回落到安全值；请求里没带 retract（或 auto=False）时不做。

        由**服务端**做而不是客户端：客户端退出/崩溃时不回落等于安全收尾丢失；
        成组轴也必须每一路都写、每一路都等到位（报告 6.6）。
        """
        spec = run.request.retract
        if spec is None or not spec.auto:
            return None
        setpoints = list(run.request.axis.setpoint_signals)
        with self._lock:
            run.message = f"正在回落到 {spec.current_a:g}"
        return retract_signals(
            self._signals, setpoints, spec.current_a, spec.rate_a_s, timeout_s=None
        )

    def acknowledge_recovery(self, run_id: str, note: str = "") -> ScanRunStatus:
        """人工确认设备状态后释放恢复锁（文档 6.3：恢复动作由明确策略决定）。

        这是 ``RECOVERY_REQUIRED`` 的唯一出口：在那之前该任务的设备组一直锁着，
        防止别的任务在未知状态下继续驱动同一批设备。
        """
        run = self._require(run_id)
        with self._lock:
            if run.state != ScanState.RECOVERY_REQUIRED:
                raise ScanError(f"任务不处于恢复待确认状态：{run.state}")
            self._locks.release(owner=run.run_id)
            suffix = f"（已人工确认：{note}）" if note else "（已人工确认）"
            run.message = run.message + suffix
        return self.status(run_id)

    def _persist_points(self, run: _Run) -> StoredSpectrum:
        """把**合格**点写成 NPZ（原子发布）并回写元数据。"""
        included = [p for p in run.points if p.included]
        if not included:
            raise ScanError("没有任何有效点，未生成谱图")
        return self._store.publish_spectrum(
            run.run_id,
            [p.coordinate for p in included],
            [p.signal for p in included],
        )

    # ------------------------------------------------------------------
    # 停止
    # ------------------------------------------------------------------
    def stop(self, run_id: str) -> ScanRunStatus:
        """请求停止。收尾由执行线程完成（它可能正卡在一次写入或采样里）。"""
        run = self._require(run_id)
        with self._lock:
            if run.state in TERMINAL_STATES:
                return self._status_of(run)
            run.message = "已请求停止，等待当前点收尾"
        run.stop_event.set()
        return self.status(run_id)

    # ------------------------------------------------------------------
    # 状态转换
    # ------------------------------------------------------------------
    def _to(self, run: _Run, target: ScanState, message: str) -> None:
        """按合法转换表推进状态；非法转换说明代码有 bug，直接抛出让测试抓到。"""
        run.state = transition_scan(run.state, target)
        run.message = message
