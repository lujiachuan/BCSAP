"""调束任务状态机：建议 → 人工确认 → 执行层校验 → 写入 → 读回 → 记录。

架构依据（文档 6.6 / 9.4）：

* **优化算法只提出候选参数**。边界、最大单步、变化速率与「人工模式」检查
  全在执行层完成，任何模式都不能绕过；
* 第一版只开放「建议 → 人工确认执行」（``confirm``）。连续自动写入需要
  另行通过安全评审，因此这里**显式拒绝**其它模式，而不是留一个没人看管的开关；
* 每轮保存候选值、实际下发值、实际回读值、目标测量、质量状态、算法版本与
  随机种子——建议值不等于已执行值；
* 下发中断导致设备状态未知时进入 ``RECOVERY_REQUIRED`` 并**继续持有设备锁**，
  等人工确认。

关于联锁：文档明确「硬件联锁由硬件/IOC 承担，应用检查和角色权限不能替代它」。
本模块不假装实现了联锁。
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
from packages.contracts.tuning import (
    MODE_CONFIRM,
    SUPPORTED_MODES,
    TuningIteration,
    TuningIterationsResponse,
    TuningProposal,
    TuningRunRequest,
    TuningRunStatus,
    TuningVariable,
)
from packages.domain.tuning import TuningState, transition_tuning
from packages.optimizer import Dimension, GpEiOptimizer

from .device_locks import DeviceBusy, DeviceLockManager
from .signal_io import SignalWriteService, WriteRejected
from .tuning_store import TuningStore

TERMINAL_STATES = frozenset(
    {
        TuningState.COMPLETED,
        TuningState.ABORTED,
        TuningState.FAILED,
        TuningState.RECOVERY_REQUIRED,
    }
)

# 每轮目标量重复采样之间的间隔（秒）。目标量本身是瞬时读数，间隔只为避开
# 单次毛刺，不需要按设备稳定时间去等。
TARGET_SAMPLE_INTERVAL_S = 0.05


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TuningError(RuntimeError):
    """调束请求不可受理（参数、映射、模式或设备占用问题）。"""


@dataclass
class _Run:
    run_id: str
    request: TuningRunRequest
    dimensions: list[Dimension]
    optimizer: GpEiOptimizer
    state: TuningState = TuningState.DRAFT
    message: str = ""
    iterations: list[TuningIteration] = field(default_factory=list)
    pending: TuningProposal | None = None
    locked_groups: list[str] = field(default_factory=list)
    best_values: dict[str, float] = field(default_factory=dict)
    best_objective: float | None = None
    started_at: str | None = None
    finished_at: str | None = None
    # 用户在某一轮执行途中请求停止。等确认时可以直接停，
    # 正在写设备的那一轮必须等它收尾，所以用一个标记位传递意图。
    stopped: bool = False


class TuningService:
    """调束任务的生命周期管理。

    ``apply`` 是同步的：确认后要写设备、等稳定、再采样，操作员本来就在等这一轮
    的结果，异步反而让"确认了没有"变得含糊。
    """

    def __init__(
        self,
        signals_provider: Callable[[], SignalWriteService],
        locks: DeviceLockManager,
        store: TuningStore,
        *,
        sleep=time.sleep,
    ) -> None:
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
    def status(self, run_id: str) -> TuningRunStatus:
        run = self._require(run_id)
        with self._lock:
            return self._status_of(run)

    def iterations(self, run_id: str) -> TuningIterationsResponse:
        run = self._require(run_id)
        with self._lock:
            return TuningIterationsResponse(
                run_id=run_id,
                iterations=list(run.iterations),
                max_iterations=run.request.max_iterations,
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

    def _require(self, run_id: str) -> _Run:
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            raise TuningError(f"没有这个调束任务：{run_id}")
        return run

    def _status_of(self, run: _Run) -> TuningRunStatus:
        return TuningRunStatus(
            run_id=run.run_id,
            state=str(run.state),
            mode=run.request.mode,
            target_signal=run.request.target_signal,
            max_iterations=run.request.max_iterations,
            completed_iterations=len(run.iterations),
            best_objective=run.best_objective,
            best_values=dict(run.best_values),
            pending=run.pending,
            message=run.message,
            algorithm=run.optimizer.ALGORITHM,
            algorithm_version=run.optimizer.VERSION,
            seed=run.optimizer.seed,
            # 报「当前实际占着哪些组」，不是「曾经声明过哪些组」——
            # 终态后仍列出旧分组会让诊断端误以为设备还锁着
            locked_groups=self._held_by(run.run_id),
            started_at=run.started_at,
            finished_at=run.finished_at,
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
    def start(self, request: TuningRunRequest) -> TuningRunStatus:
        with self._lock:
            active = self.active_run_id()
            if active is not None:
                raise TuningError(f"已有调束任务在进行中：{active}")
            dimensions, groups = self._validate(request)
            run = _Run(
                run_id=str(uuid4()),
                request=request,
                dimensions=dimensions,
                optimizer=GpEiOptimizer(
                    dimensions, noise=request.noise, seed=request.seed
                ),
                state=TuningState.VALIDATING,
                message="参数校验通过，准备占用设备",
            )
            self._runs[run.run_id] = run

        self._store.create_run(
            run.run_id, request.target_signal, request.mode, request.max_iterations,
            [v.model_dump() for v in request.variables], _now(),
        )
        self._store.set_algorithm(
            run.run_id, run.optimizer.ALGORITHM, run.optimizer.VERSION, request.seed
        )
        self._persist_state(run)

        try:
            self._locks.acquire(groups, owner=run.run_id)
        except DeviceBusy as exc:
            self._finish_failed(run, str(exc))
            return self.status(run.run_id)

        run.locked_groups = sorted(groups)
        with self._lock:
            run.started_at = _now()
            self._to(run, TuningState.PREPARING, "占用设备组")
            self._to(run, TuningState.RUNNING, "开始调束")
        self._persist_state(run)
        self._propose_next(run)
        return self.status(run.run_id)

    def _validate(self, request: TuningRunRequest) -> tuple[list[Dimension], set[str]]:
        """校验模式、信号与范围；返回维度列表与需要占用的设备组。"""
        if request.mode not in SUPPORTED_MODES:
            raise TuningError(
                f"暂不支持调束模式 {request.mode!r}。按架构文档 6.6 的分阶段计划，"
                f"第一版只开放「建议→人工确认」（{MODE_CONFIRM}）；"
                "连续自动写入需另行通过安全评审。"
            )
        variables = request.enabled_variables()
        if not variables:
            raise TuningError("至少要启用一个可调参数")
        if request.max_iterations < 1:
            raise TuningError("最大迭代次数至少为 1")
        if request.samples_per_point < 1:
            raise TuningError("每轮目标采样次数至少为 1")

        groups: set[str] = set()
        dimensions: list[Dimension] = []
        for variable in variables:
            entry = self._entry(variable.signal)
            if not entry.writable:
                raise TuningError(f"参与调束的参数不可写：{variable.signal}")
            self._check_range(variable, entry)
            if entry.max_step is None or entry.max_step <= 0:
                raise TuningError(
                    f"{entry.label} 未配置 max_step：调束必须有最大单步约束，"
                    "否则一次大跳变可能直接毁掉束流"
                )
            if variable.low == variable.high:
                raise TuningError(f"参数 {variable.signal} 的范围退化成了一个点")
            dimensions.append(
                Dimension(variable.signal, float(variable.low), float(variable.high))
            )
            if entry.group:
                groups.add(entry.group)

        target = self._entry(request.target_signal)
        if target.writable:
            raise TuningError(f"目标量应为只读测量信号：{request.target_signal}")
        if target.group:
            groups.add(target.group)
        return dimensions, groups

    def _entry(self, signal: str):
        try:
            return self._signals.entry(signal)
        except WriteRejected as exc:
            raise TuningError(str(exc)) from exc

    @staticmethod
    def _check_range(variable: TuningVariable, entry) -> None:
        """界面给的 [low, high] 必须落在设备硬边界内。

        让优化器提出一个执行层必然拒绝的值属于自欺欺人：先把范围收进设备边界，
        候选才有意义。范围超出时直接拒绝启动，并指出是哪一侧、差多少。
        """
        if entry.min_value is not None and variable.low < entry.min_value:
            raise TuningError(
                f"{entry.label} 下限 {variable.low:g} 低于设备允许最小值 "
                f"{entry.min_value:g} {entry.unit}"
            )
        if entry.max_value is not None and variable.high > entry.max_value:
            raise TuningError(
                f"{entry.label} 上限 {variable.high:g} 超过设备允许最大值 "
                f"{entry.max_value:g} {entry.unit}"
            )

    # ------------------------------------------------------------------
    # 建议
    # ------------------------------------------------------------------
    def _propose_next(self, run: _Run) -> None:
        with self._lock:
            if run.state in TERMINAL_STATES:
                return
            if len(run.iterations) >= run.request.max_iterations:
                self._finish_completed(run)
                return
            proposal = run.optimizer.propose()
            run.pending = TuningProposal(
                iteration=len(run.iterations),
                values={
                    dimension.name: value
                    for dimension, value in zip(
                        run.dimensions, proposal.values, strict=True
                    )
                },
                predicted=proposal.predicted,
                std=proposal.std,
                expected_improvement=(
                    0.0 if proposal.expected_improvement == float("inf")
                    else proposal.expected_improvement
                ),
                at=_now(),
            )
            self._to(
                run,
                TuningState.AWAITING_CONFIRMATION,
                f"第 {proposal.iteration + 1} 轮候选已生成，等待人工确认",
            )
        self._persist_state(run)

    # ------------------------------------------------------------------
    # 确认并执行
    # ------------------------------------------------------------------
    def approve(self, run_id: str) -> TuningRunStatus:
        """人工确认当前候选：交给执行层校验并写入设备，然后测量目标。"""
        run = self._require(run_id)
        with self._lock:
            if run.state != TuningState.AWAITING_CONFIRMATION or run.pending is None:
                raise TuningError(
                    f"当前没有待确认的候选（状态为 {run.state}）"
                )
            proposal = run.pending
            self._to(run, TuningState.APPLYING, f"第 {proposal.iteration + 1} 轮写入设备")
        self._persist_state(run)

        iteration, state_unknown = self._apply(run, proposal)

        if state_unknown:
            self._finish_failed(
                run, "写入中断，设备实际状态未知，请人工确认后释放设备锁",
                state_unknown=True,
            )
            return self.status(run_id)

        with self._lock:
            run.pending = None
            run.iterations.append(iteration)
            if iteration.objective is not None:
                run.optimizer.observe(
                    [proposal.values[d.name] for d in run.dimensions],
                    iteration.objective,
                )
                if (
                    run.best_objective is None
                    or iteration.objective > run.best_objective
                ):
                    run.best_objective = iteration.objective
                    run.best_values = dict(iteration.readback)
                    self._store.set_best(
                        run.run_id, dict(iteration.readback), iteration.objective
                    )
        self._store.append_iteration(run.run_id, iteration)

        with self._lock:
            if run.stopped:
                self._finish_aborted(run)
                return self.status(run_id)
            self._to(run, TuningState.RUNNING, f"第 {iteration.iteration + 1} 轮完成")
        self._persist_state(run)
        self._propose_next(run)
        return self.status(run_id)

    def _apply(self, run: _Run, proposal: TuningProposal) -> tuple[TuningIteration, bool]:
        """执行一轮：写全部参数 → 等稳定 → 读回 → 采目标。返回 (记录, 状态是否未知)。"""
        request = run.request
        applied: dict[str, float] = {}
        readback: dict[str, float] = {}
        unsettled: list[str] = []

        for variable in request.enabled_variables():
            target_value = proposal.values[variable.signal]
            entry = self._entry(variable.signal)
            result = self._signals.write(
                SignalWriteRequest(signal=variable.signal, value=target_value, ramp=True)
            )
            if not result.accepted:
                return (
                    TuningIteration(
                        iteration=proposal.iteration,
                        proposed=dict(proposal.values),
                        applied=applied,
                        readback=readback,
                        target=None,
                        objective=None,
                        quality="write_rejected",
                        detail=f"{entry.label} 写入被拒：{result.reason}",
                        at=_now(),
                    ),
                    result.device_state_unknown,
                )
            applied[variable.signal] = float(
                result.applied if result.applied is not None else target_value
            )
            settled, actual = self._signals.wait_settled(
                entry,
                target_value,
                tol=request.settle_tol,
                timeout=request.settle_timeout_s,
                readback_signal=entry.readback_signal or None,
            )
            if actual is not None:
                readback[variable.signal] = float(actual)
            if not settled:
                # 未稳定仍如实记录：实际回读才是设备真实位置。但质量要标出来，
                # 否则事后会把「还没到位就测的目标值」当成可靠数据。
                unsettled.append(entry.label)

        samples: list[float] = []
        for index in range(request.samples_per_point):
            if index:
                self._sleep(TARGET_SAMPLE_INTERVAL_S)
            value = self._signals.read_value(request.target_signal)
            if value is not None:
                samples.append(value)

        if not samples:
            return (
                TuningIteration(
                    iteration=proposal.iteration,
                    proposed=dict(proposal.values),
                    applied=applied,
                    readback=readback,
                    target=None,
                    objective=None,
                    quality="read_failed",
                    detail=f"{request.target_signal} 无有效读数",
                    at=_now(),
                ),
                False,
            )

        objective = float(statistics.median(samples))
        return (
            TuningIteration(
                iteration=proposal.iteration,
                proposed=dict(proposal.values),
                applied=applied,
                readback=readback,
                target=objective,
                objective=objective,
                quality="ok" if not unsettled else "unsettled",
                detail=(
                    None
                    if not unsettled
                    else "回读未在超时内进入容差：" + "、".join(unsettled)
                ),
                at=_now(),
            ),
            False,
        )

    # ------------------------------------------------------------------
    # 终态
    # ------------------------------------------------------------------
    def stop(self, run_id: str) -> TuningRunStatus:
        run = self._require(run_id)
        with self._lock:
            if run.state in TERMINAL_STATES:
                return self._status_of(run)
            run.stopped = True
            # 等确认期间可以直接停；正在写入的那一轮要等它收尾
            if run.state == TuningState.AWAITING_CONFIRMATION:
                self._finish_aborted(run)
                return self._status_of(run)
            run.message = "已请求停止，等待当前轮收尾"
        self._persist_state(run)
        return self.status(run_id)

    def _finish_completed(self, run: _Run) -> None:
        self._locks.release(owner=run.run_id)
        with self._lock:
            run.pending = None
            self._to(run, TuningState.COMPLETED, f"完成 {len(run.iterations)} 轮")
            run.finished_at = _now()
        self._store.set_finished(run.run_id, run.finished_at)
        self._persist_state(run)

    def _finish_aborted(self, run: _Run) -> None:
        self._locks.release(owner=run.run_id)
        with self._lock:
            run.pending = None
            self._to(run, TuningState.STOP_REQUESTED, "收到停止请求")
            self._to(run, TuningState.ABORTED, "已停止")
            run.finished_at = _now()
        self._store.set_finished(run.run_id, run.finished_at)
        self._persist_state(run)

    def _finish_failed(self, run: _Run, message: str, *, state_unknown: bool = False) -> None:
        if not state_unknown:
            self._locks.release(owner=run.run_id)
        with self._lock:
            if run.state in TERMINAL_STATES:
                return
            self._to(run, TuningState.FAILED, message)
            if state_unknown:
                self._to(run, TuningState.RECOVERY_REQUIRED, message)
            run.pending = None
            run.finished_at = _now()
        self._store.set_finished(run.run_id, run.finished_at)
        self._persist_state(run)

    def acknowledge_recovery(self, run_id: str, note: str = "") -> TuningRunStatus:
        """人工确认设备状态后释放恢复锁。"""
        run = self._require(run_id)
        with self._lock:
            if run.state != TuningState.RECOVERY_REQUIRED:
                raise TuningError(f"任务不处于恢复待确认状态：{run.state}")
            self._locks.release(owner=run.run_id)
            suffix = f"（已人工确认：{note}）" if note else "（已人工确认）"
            run.message = run.message + suffix
        return self.status(run_id)

    # ------------------------------------------------------------------
    def _to(self, run: _Run, target: TuningState, message: str) -> None:
        run.state = transition_tuning(run.state, target)
        run.message = message

    def _persist_state(self, run: _Run) -> None:
        self._store.set_state(run.run_id, str(run.state), run.message)
