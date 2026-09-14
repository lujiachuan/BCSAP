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
    ACTION_APPLY_BEST,
    ACTION_RESTORE_INITIAL,
    FINALIZE_ACTIONS,
    MODE_CONFIRM,
    STAGE_JOINT,
    STAGE_LABELS,
    STAGE_SEQUENTIAL,
    STRATEGY_SEQUENTIAL,
    SUPPORTED_MODES,
    SUPPORTED_STRATEGIES,
    TuningFinalizeResult,
    TuningIteration,
    TuningIterationsResponse,
    TuningProposal,
    TuningRecovery,
    TuningRunRequest,
    TuningRunStatus,
    TuningVariable,
)
from packages.domain.tuning import TuningState, transition_tuning
from packages.optimizer import Dimension, GpEiOptimizer

from .device_locks import DeviceBusy, DeviceLockManager
from .signal_io import SignalWriteService, WriteRejected
from .tuning_store import TuningStore

# 处置动作的中文名（返回给界面直接用，避免两处各写一套）
FINALIZE_LABELS = {
    ACTION_APPLY_BEST: "应用最优参数",
    ACTION_RESTORE_INITIAL: "恢复启动前参数",
    "safe_values": "回安全值",
}

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

# 启动前快照的读取重试：冷启动时 CA 的**第一次**访问要建连，可能失败一次。
# 这与扫谱的连接探测是同一个道理——把一次握手失败当成"读不到"，会让任务在
# 冷启动时白拒一次（实测踩到过：紧接着手工读同一路是好的）。
SNAPSHOT_ATTEMPTS = 3
SNAPSHOT_RETRY_DELAY = 0.5


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
    # 启动前快照：各路参与变量的实际回读 + 目标基线（束流丢失保护靠它回退）
    snapshot: dict[str, float] = field(default_factory=dict)
    baseline_objective: float | None = None
    snapshot_note: str = ""
    # 连续异常计数与回退记录
    anomalies: int = 0
    recovery: TuningRecovery | None = None
    # 结束后的处置（应用最优/恢复初始/回安全值）最近一次结果
    finalize: TuningFinalizeResult | None = None
    # ---- 两阶段策略的运行态 ----
    # 当前阶段（sequential / joint）与该阶段已用轮次
    stage: str = ""
    stage_rounds: int = 0
    # 当前这一轮真正会写的信号（阶段 1 只写一个变量，其余保持不动）
    active_signals: list[str] = field(default_factory=list)
    # 未被激活的变量保持的值（初始取启动前快照，每轮按实际回读更新）
    held_values: dict[str, float] = field(default_factory=dict)
    # 每个变量**自己**的最优点（信号 → (值, 当时的目标值)）：
    # 联合微调要围绕它收窄，而不是围绕"最后一轮停在哪"
    variable_best: dict[str, tuple[float, float]] = field(default_factory=dict)


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
            snapshot=dict(run.snapshot),
            baseline_objective=run.baseline_objective,
            snapshot_note=run.snapshot_note,
            strategy=run.request.strategy,
            stage=run.stage,
            stage_variable=(
                run.active_signals[0]
                if run.stage == STAGE_SEQUENTIAL and run.active_signals
                else ""
            ),
            stage_index=self._stage_index(run),
            stage_total=self._stage_total(run),
            anomalies=run.anomalies,
            recovery=run.recovery,
            finalize=run.finalize,
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
        # 启动前快照：**必须先取到**再进入 RUNNING。取不到就等于退不回去，
        # 宁可不开（改造报告 §5.2 第一条）。
        problem = self._capture_snapshot(run)
        if problem is not None:
            self._finish_failed(run, problem)
            return self.status(run.run_id)
        with self._lock:
            self._to(run, TuningState.RUNNING, "开始调束")
        self._persist_state(run)
        self._propose_next(run)
        return self.status(run.run_id)

    def _read_with_retry(self, read: Callable[[], float | None]) -> float | None:
        """读一次现场值，失败就退避重试几次再认输。

        冷启动时 CA 第一次访问要建连，可能失败一次——那是环境特性而不是设备故障，
        直接当成"读不到"会让任务白拒（取不到快照是不允许启动的）。
        """
        value: float | None = None
        for attempt in range(SNAPSHOT_ATTEMPTS):
            value = read()
            if value is not None:
                return value
            if attempt < SNAPSHOT_ATTEMPTS - 1:
                self._sleep(SNAPSHOT_RETRY_DELAY)
        return value

    def _capture_snapshot(self, run: _Run) -> str | None:
        """记录启动前各路实际回读与目标基线；返回错误文本（None 表示成功）。

        - 任何一路参与变量读不到 → 拒绝启动：**没有快照就没有安全的退路**，
          中途出问题时无法把它退回去。
        - 目标基线读不到不算致命：此时只能按绝对阈值做保护，界面上会说明。
        """
        snapshot: dict[str, float] = {}
        missing: list[str] = []
        for variable in run.request.enabled_variables():
            entry = self._entry(variable.signal)
            value = self._read_with_retry(lambda e=entry: self._signals.readback(e))
            if value is None:
                missing.append(entry.label)
                continue
            snapshot[variable.signal] = float(value)
        if missing:
            return (
                "启动前快照取不到实际回读，已拒绝启动（没有快照就无法在束流异常时"
                f"退回原位）：{'、'.join(missing)}"
            )

        baseline = self._read_with_retry(
            lambda: self._signals.read_value(run.request.target_signal)
        )
        with self._lock:
            run.snapshot = snapshot
            # 两阶段策略的"保持值"从快照出发：阶段 1 调第一个变量时，
            # 其余变量就停在启动前的位置上。
            run.held_values = dict(snapshot)
            run.baseline_objective = None if baseline is None else float(baseline)
            run.snapshot_note = (
                ""
                if baseline is not None
                else (
                    f"启动前快照已记录 {len(snapshot)} 路；"
                    f"但 {run.request.target_signal} 基线读不到，"
                    "束流丢失保护只能按绝对阈值判断（相对损失判据不可用）"
                )
            )
        self._store.set_snapshot(
            run.run_id, snapshot, None if baseline is None else float(baseline)
        )
        return None

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
        # 两阶段策略的参数校验：轮次/比例/保持时间的取值必须自洽，
        # 否则会得到一个"阶段 1 跑不完"或"联合范围退化成一个点"的任务。
        if request.strategy not in SUPPORTED_STRATEGIES:
            raise TuningError(
                f"未知的优化策略 {request.strategy!r}，支持："
                f"{'、'.join(SUPPORTED_STRATEGIES)}"
            )
        if request.calls_per_variable < 1:
            raise TuningError("每个变量的调用轮次至少为 1")
        if not 0.0 < request.joint_frac <= 1.0:
            raise TuningError("联合微调范围比例必须落在 (0, 1] 之间")
        if request.hold_s < 0:
            raise TuningError("保持时间不能为负")
        if request.strategy == STRATEGY_SEQUENTIAL:
            needed = request.calls_per_variable * len(variables)
            if needed >= request.max_iterations:
                raise TuningError(
                    f"逐参数阶段需要 {needed} 轮（{len(variables)} 个变量 × "
                    f"{request.calls_per_variable} 轮），已占满最大轮次 "
                    f"{request.max_iterations}，联合微调阶段没有轮次可用；"
                    "请增大最大轮次或减少每变量轮次"
                )

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
    # 两阶段策略（改造报告 §5.2）
    # ------------------------------------------------------------------
    def _stage1_rounds(self, run: _Run) -> int:
        """阶段 1 的总轮次 = 每个变量轮次 × 变量个数。"""
        return max(1, run.request.calls_per_variable) * len(run.request.enabled_variables())

    def _stage_name(self, run: _Run) -> str:
        if run.request.strategy != STRATEGY_SEQUENTIAL:
            return STAGE_JOINT
        if run.stage == STAGE_JOINT:  # 已经进过联合阶段就不再回退
            return STAGE_JOINT
        return (
            STAGE_SEQUENTIAL
            if run.stage_rounds < self._stage1_rounds(run)
            else STAGE_JOINT
        )

    def _stage_index(self, run: _Run) -> int:
        """阶段 1 里正在调第几个变量（从 1 起）；联合阶段返回 0。"""
        if self._stage_name(run) != STAGE_SEQUENTIAL:
            return 0
        per = max(1, run.request.calls_per_variable)
        return min(len(run.request.enabled_variables()), run.stage_rounds // per + 1)

    def _stage_total(self, run: _Run) -> int:
        if self._stage_name(run) != STAGE_SEQUENTIAL:
            return 0
        return len(run.request.enabled_variables())

    def _active_variables(self, run: _Run) -> list[TuningVariable]:
        """当前阶段真正参与优化的变量：阶段 1 是单个，联合阶段是全部。"""
        variables = run.request.enabled_variables()
        if self._stage_name(run) != STAGE_SEQUENTIAL:
            return variables
        per = max(1, run.request.calls_per_variable)
        index = min(len(variables) - 1, run.stage_rounds // per)
        return [variables[index]]

    def _dimension_for(self, run: _Run, variable: TuningVariable) -> Dimension:
        """该变量在当前阶段的取值范围。

        联合微调阶段围绕**阶段 1 的最优值**收窄到原范围的 ``joint_frac``：
        在整段量程上再搜一遍没有意义，微调本来就该是"在最优点附近找更细的解"。
        """
        if (
            run.request.strategy != STRATEGY_SEQUENTIAL
            or self._stage_name(run) != STAGE_JOINT
        ):
            return Dimension(variable.signal, float(variable.low), float(variable.high))
        center = run.held_values.get(variable.signal)
        best = run.variable_best.get(variable.signal)
        if best is not None:
            center = best[0]  # 优先围绕该变量**自己**的最优点收窄
        if center is None:
            center = (float(variable.low) + float(variable.high)) / 2.0
        frac = max(0.0, min(1.0, float(run.request.joint_frac)))
        half = (float(variable.high) - float(variable.low)) * frac / 2.0
        low = max(float(variable.low), center - half)
        high = min(float(variable.high), center + half)
        if high <= low:  # 收窄后退化成一个点：退回原范围，别给优化器一个无效维度
            return Dimension(variable.signal, float(variable.low), float(variable.high))
        return Dimension(variable.signal, low, high)

    def _sync_stage(self, run: _Run) -> None:
        """把优化器切到"当前阶段 + 当前激活变量"。

        切换时必须**重建优化器**：阶段 1 是单维、阶段 2 是全维，旧实例的归一化与
        观测都按旧维度算；混用会让长度尺度失去意义，候选就变成随机数。
        已有观测会按新维度**投影接续**——GP 见过的东西不该因为换阶段就忘掉。
        """
        active = self._active_variables(run)
        stage = self._stage_name(run)
        active_signals = [variable.signal for variable in active]
        if run.active_signals == active_signals and run.stage == stage:
            return
        if stage == STAGE_JOINT and run.stage == STAGE_SEQUENTIAL:
            run.stage_rounds = 0
        run.stage = stage
        run.active_signals = active_signals
        dimensions = [self._dimension_for(run, variable) for variable in active]
        names = [dimension.name for dimension in dimensions]
        optimizer = GpEiOptimizer(
            dimensions, noise=run.request.noise, seed=run.request.seed
        )
        for iteration in run.iterations:
            if iteration.objective is None:
                continue
            try:
                optimizer.observe(
                    [iteration.proposed[name] for name in names], iteration.objective
                )
            except (KeyError, ValueError, TypeError):
                # 老记录缺这一路（或维度不匹配）时跳过，不让它挡住后面的建议
                continue
        run.optimizer = optimizer

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
            self._sync_stage(run)
            active = list(run.active_signals)
            proposal = run.optimizer.propose()
            # 未激活的变量按"保持不动"填值：候选表与执行记录都要看得到完整一组参数
            values = {variable.signal: run.held_values.get(variable.signal, variable.low)
                      for variable in run.request.enabled_variables()}
            values.update(
                {
                    dimension.name: value
                    for dimension, value in zip(
                        run.optimizer.dimensions, proposal.values, strict=True
                    )
                }
            )
            run.pending = TuningProposal(
                iteration=len(run.iterations),
                values=values,
                active_signals=active,
                stage=run.stage,
                predicted=proposal.predicted,
                std=proposal.std,
                expected_improvement=(
                    0.0 if proposal.expected_improvement == float("inf")
                    else proposal.expected_improvement
                ),
                at=_now(),
            )
            stage_text = (
                f"{STAGE_LABELS[run.stage]}：{active[0]}"
                if run.stage == STAGE_SEQUENTIAL and active
                else STAGE_LABELS.get(run.stage, run.stage)
            )
            self._to(
                run,
                TuningState.AWAITING_CONFIRMATION,
                f"第 {proposal.iteration + 1} 轮候选已生成（{stage_text}），等待人工确认",
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
                    [proposal.values[d.name] for d in run.optimizer.dimensions],
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
            # 非激活变量下一轮要继续"保持"的值 = 设备此刻的实际位置；
            # 阶段 1 逐变量推进时，后一个变量的起点就是前一个停下时的位置。
            for signal in run.active_signals:
                value = iteration.readback.get(signal)
                if value is None:
                    continue
                run.held_values[signal] = float(value)
                # 同时记这个变量**自己**的最优点：联合微调围绕它收窄
                if iteration.objective is None:
                    continue
                previous = run.variable_best.get(signal)
                if previous is None or iteration.objective > previous[1]:
                    run.variable_best[signal] = (float(value), iteration.objective)
            if run.stage == STAGE_SEQUENTIAL:
                run.stage_rounds += 1
        self._store.append_iteration(run.run_id, iteration)

        reason = self._guard_reason(run, iteration)
        with self._lock:
            run.anomalies = 0 if reason is None else run.anomalies + 1
            strikes = max(1, run.request.loss_strikes)
            tripped = reason is not None and run.anomalies >= strikes
        if tripped:
            if not run.request.auto_recover:
                # 不自动回退：把异常摆到台面上，让操作员决定（设备锁仍握着）
                self._finish_aborted(
                    run,
                    f"束流异常已连续 {run.anomalies} 轮：{reason}；"
                    "已按配置停止自动流程，参数保持现状，请人工处理",
                )
                return self.status(run_id)
            self._recover(run, reason)
            return self.status(run_id)

        with self._lock:
            if run.stopped:
                self._finish_aborted(run)
                return self.status(run_id)
            self._to(run, TuningState.RUNNING, f"第 {iteration.iteration + 1} 轮完成")
        self._persist_state(run)
        self._propose_next(run)
        return self.status(run_id)

    def _guard_reason(self, run: _Run, iteration: TuningIteration) -> str | None:
        """这一轮算不算"束流异常"？返回原因文本，None 表示正常。

        三条判据（改造报告 §5.2）：目标读不到 / 低于绝对阈值 / 相对启动前基线
        下降过多。相对判据在拿不到基线时不参与判断——**不拿猜测的数字当安全边界**。
        """
        if iteration.objective is None:
            return f"本轮没有有效目标读数（{iteration.detail or '原因未记录'}）"
        absolute = run.request.loss_absolute
        if absolute is not None and iteration.objective <= absolute:
            return f"目标 {iteration.objective:g} 已低于绝对阈值 {absolute:g}"
        baseline = run.baseline_objective
        if baseline is not None and baseline > 0:
            floor = baseline * (1.0 - run.request.loss_relative)
            if iteration.objective < floor:
                return (
                    f"目标比启动前基线 {baseline:g} 下降超过 "
                    f"{run.request.loss_relative:.0%}（本轮 {iteration.objective:g}）"
                )
        return None

    def _write_and_settle(
        self, run: _Run, targets: dict[str, float]
    ) -> tuple[dict[str, float], list[str]]:
        """逐路写目标值并等回读到位；返回（信号→实际回读, 问题清单）。

        回退与"结束后处置"共用这一段：两者都是**保护性写设备**，都得走执行层
        （``ramp=True``，边界/单步/速率约束照样生效），都必须逐路确认到位，
        否则"以为退回去了"只是写请求成功而已。
        """
        applied: dict[str, float] = {}
        problems: list[str] = []
        for variable in run.request.enabled_variables():
            signal = variable.signal
            want = targets.get(signal)
            if want is None:
                problems.append(f"{signal} 没有目标值")
                continue
            entry = self._entry(signal)
            result = self._signals.write(
                SignalWriteRequest(signal=signal, value=float(want), ramp=True)
            )
            if not result.accepted:
                problems.append(f"{entry.label} 写入被拒：{result.reason}")
                continue
            settled, actual = self._signals.wait_settled(
                entry,
                float(want),
                tol=run.request.settle_tol,
                timeout=run.request.settle_timeout_s,
                readback_signal=entry.readback_signal or None,
            )
            if actual is not None:
                applied[signal] = float(actual)
            if not settled:
                current = actual if actual is not None else "读不到"
                problems.append(f"{entry.label} 未进入容差（当前 {current}）")
        return applied, problems

    def _recover(self, run: _Run, reason: str) -> None:
        """束流丢失保护：经**执行层**把各路退回启动前快照。

        任一路退不到位就不算成功，按"设备状态未知"收尾并保留设备锁，
        交人工确认（改造报告 §5.2 第 4、5 条）。
        """
        applied, problems = self._write_and_settle(run, dict(run.snapshot))

        recovery = TuningRecovery(
            reason=reason,
            at=_now(),
            ok=not problems,
            restored=applied,
            detail="；".join(problems),
        )
        with self._lock:
            run.recovery = recovery
        self._store.set_recovery(run.run_id, recovery)

        if recovery.ok:
            self._finish_aborted(
                run,
                f"束流丢失保护已触发（{reason}）；"
                f"已按启动前快照退回 {len(applied)} 路参数",
            )
            return
        self._finish_failed(
            run,
            f"束流丢失保护回退未完成（{reason}）：{'；'.join(problems)}",
            state_unknown=True,
        )

    # ------------------------------------------------------------------
    # 结束后的设备处置（改造报告 §5.2「完成后设备状态」）
    # ------------------------------------------------------------------
    def finalize(
        self, run_id: str, action: str, *, confirm: bool = False
    ) -> TuningFinalizeResult:
        """按最优轮 / 启动前快照 / 安全值处置设备。

        任务结束时设备停在**最后一轮**参数上，而最后一轮不一定是历史最优轮，
        操作员需要能明确地选一种收尾方式。三种动作都会写设备，因此：
        必须显式确认、必须重新申请设备组（结束后锁已交还，别人可能正在用）、
        必须逐路确认到位并把实际回读返回。
        """
        run = self._require(run_id)
        if action not in FINALIZE_ACTIONS:
            raise TuningError(f"未知的处置动作：{action}")
        if not confirm:
            raise TuningError("处置动作会写设备，必须在界面上二次确认后再执行")
        with self._lock:
            if run.state not in TERMINAL_STATES:
                raise TuningError(f"任务还没结束（当前 {run.state}），不能处置设备")
            if run.state == TuningState.RECOVERY_REQUIRED:
                raise TuningError(
                    "任务处于恢复待确认状态：设备实际状态未知，"
                    "请先确认设备状态再处置"
                )

        targets = self._finalize_targets(run, action)
        groups = set(run.locked_groups)
        owner = f"finalize-{run.run_id}"
        try:
            self._locks.acquire(groups, owner=owner)
        except DeviceBusy as exc:
            raise TuningError(
                f"设备组当前不可用，处置动作未执行：{exc}"
            ) from exc
        try:
            applied, problems = self._write_and_settle(run, targets)
        finally:
            self._locks.release(owner=owner)

        result = TuningFinalizeResult(
            ok=not problems,
            action=action,
            message=(
                f"{FINALIZE_LABELS[action]}完成（{len(applied)} 路已到位）"
                if not problems
                else f"{FINALIZE_LABELS[action]}未完成"
            ),
            applied=applied,
            detail="；".join(problems),
            at=_now(),
        )
        self._store.set_finalize(run.run_id, result)
        with self._lock:
            run.finalize = result
        return result

    def _finalize_targets(self, run: _Run, action: str) -> dict[str, float]:
        """算出这次动作每路要写到的值；缺依据时明确拒绝，不猜。"""
        if action == ACTION_APPLY_BEST:
            if not run.best_values:
                raise TuningError("这次任务还没有可用的最优轮，无法应用最优参数")
            return {signal: float(value) for signal, value in run.best_values.items()}
        if action == ACTION_RESTORE_INITIAL:
            if not run.snapshot:
                raise TuningError(
                    "这次任务没有启动前快照（可能是旧版本任务），无法恢复初始参数"
                )
            return {signal: float(value) for signal, value in run.snapshot.items()}
        # 安全值：每路退到映射里配置的下限；没有下限按 0（与"全部关断"同一口径）
        targets: dict[str, float] = {}
        for variable in run.request.enabled_variables():
            entry = self._entry(variable.signal)
            targets[variable.signal] = (
                float(entry.min_value) if entry.min_value is not None else 0.0
            )
        return targets

    def _apply(self, run: _Run, proposal: TuningProposal) -> tuple[TuningIteration, bool]:
        """执行一轮：写**本轮激活的**参数 → 等稳定 → 读回 → 采目标。

        阶段 1（逐参数）只写一个变量，其余按"保持不动"处理——**不写它们**：
        对同一台设备重复写同样的值既没有意义，又会白白增加设备动作与等待时间。
        但这些变量的实际回读照样记下来，事后看得到"当时它们在哪"。
        """
        request = run.request
        active = set(proposal.active_signals or [v.signal for v in request.enabled_variables()])
        applied: dict[str, float] = {}
        readback: dict[str, float] = {}
        unsettled: list[str] = []

        for variable in request.enabled_variables():
            entry = self._entry(variable.signal)
            if variable.signal not in active:
                value = self._signals.readback(entry)
                if value is not None:
                    readback[variable.signal] = float(value)
                continue
            target_value = proposal.values[variable.signal]
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
                        stage=proposal.stage,
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
        # 「保持时间」：稳定判据过了之后再等一会儿——有些量（气路、真空）到位后
        # 仍然在缓慢变化，不等就是把过渡态当成稳态测进目标里。
        if request.hold_s:
            self._sleep(request.hold_s)
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
                    stage=proposal.stage,
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
                stage=proposal.stage,
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

    def _finish_aborted(self, run: _Run, reason: str | None = None) -> None:
        """收尾为「已停止」。``reason`` 给了就用它——保护动作触发的收尾必须
        在状态文案里说清"为什么停"，否则界面上只剩一句"已停止"。"""
        final = reason or "已停止"
        self._locks.release(owner=run.run_id)
        with self._lock:
            run.pending = None
            self._to(run, TuningState.STOP_REQUESTED, "收到停止请求")
            self._to(run, TuningState.ABORTED, final)
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
