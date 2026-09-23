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

import json
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
    ACTION_START_VALUES,
    ENGINE_GP,
    FINALIZE_ACTIONS,
    LEVEL_ERROR,
    LEVEL_INFO,
    LEVEL_WARN,
    MODE_AUTO,
    PHASE_HOLDING,
    PHASE_IDLE,
    PHASE_MEASURING,
    PHASE_PRECHECKING,
    PHASE_PROPOSING,
    PHASE_RECORDING,
    PHASE_SETTLING,
    PHASE_WAITING_CONFIRMATION,
    PHASE_WRITING,
    STAGE_JOINT,
    STAGE_LABELS,
    STAGE_SEQUENTIAL,
    STRATEGY_SEQUENTIAL,
    SUPPORTED_ENGINES,
    SUPPORTED_MODES,
    SUPPORTED_STRATEGIES,
    TuningEvent,
    TuningEventsResponse,
    TuningFinalizeResult,
    TuningIteration,
    TuningIterationsResponse,
    TuningProgressResponse,
    TuningProposal,
    TuningRecovery,
    TuningRunRequest,
    TuningRunStatus,
    TuningVariable,
)
from packages.domain import beamline
from packages.domain.tuning import TuningState, transition_tuning
from packages.optimizer import Dimension, GpEiOptimizer
from packages.optimizer.optuna_adapter import OPTUNA_ENGINES, OptunaAsker

from .device_locks import DeviceBusy, DeviceLockManager
from .interlocks import InterlockViolation, check_interlocks
from .signal_io import SignalWriteService, WriteRejected
from .tuning_catalog import NEVER_TUNABLE_SUFFIXES
from .tuning_store import TuningStore

# 处置动作的中文名（返回给界面直接用，避免两处各写一套）
FINALIZE_LABELS = {
    ACTION_APPLY_BEST: "应用最优参数",
    ACTION_RESTORE_INITIAL: "恢复启动前参数",
    ACTION_START_VALUES: "回到起始值",
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


def build_optimizer(
    dimensions: list[Dimension],
    request: TuningRunRequest,
    *,
    storage: str | None = None,
    study_name: str | None = None,
    pruner: str | None = None,
):
    """按请求的引擎构造优化器：gp 走自研，其余走 Optuna ask/tell 适配。

    接口统一为 propose/observe（GpEiOptimizer 与 OptunaAsker 同构），
    状态机里"提候选 → 人工门/自动门 → 执行 → 回报观测"的循环代码不用改。

    storage/study_name 给 Optuna 侧持久化（sqlite），事后可挂 dashboard、
    算参数重要性；gp 侧不使用。
    """
    if request.engine == ENGINE_GP or request.engine not in OPTUNA_ENGINES:
        return GpEiOptimizer(dimensions, noise=request.noise, seed=request.seed)
    return OptunaAsker(
        dimensions, noise=request.noise, seed=request.seed, engine=request.engine,
        storage=storage, study_name=study_name, pruner=pruner,
        n_startup_trials=request.n_startup_trials,
    )

# 每轮目标量重复采样之间的间隔（秒）。目标量本身是瞬时读数，间隔只为避开
# 单次毛刺，不需要按设备稳定时间去等。
TARGET_SAMPLE_INTERVAL_S = 0.05

# 写入报错后、准备重试同一个目标值之前的退避时间（秒）。
# 命令行网关的瞬时超时多来自"起进程 + 建 CA 连接"这一步慢，隔一下再试比立刻重试有效。
WRITE_RETRY_BACKOFF_S = 0.3

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
    # 连续"没有产生新的历史最优"的轮数（收敛早停的计数）
    no_improvement: int = 0
    # ---- 批次 3：有效轮次与无效轮次守卫 ----
    # 真的测到目标且回读到位的轮数（阶段推进与早停都只看它）
    valid_rounds: int = 0
    # 连续无效轮数（写入被拒 / 目标读不到 / 未稳定）；与束流异常分开计数
    invalid_streak: int = 0
    # 最近一次"软阈值区间"的说明（供事件日志使用；不进异常计数）
    last_soft_warning: str = ""
    # 本轮结果喂给优化器**之前**它已有多少观测。判断"模型是否事先预期了这次下探"
    # 必须用它：本轮结果一旦喂进去，模型"预测"什么都成了事后诸葛。
    prior_observations: int = 0
    # 全自动模式的后台驱动线程（测试用 join 等它彻底结束，避免写库竞态）
    driver: threading.Thread | None = None
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
    # Optuna study 名（= run_id），供分析 API / dashboard 定位持久化 study
    optuna_study_name: str = ""
    # ---- 实时进度（第二批：事件流 / 阶段 / 版本号）----
    # 每持久化一次 +1：客户端据此判断"这份状态比上次新"，不比时钟
    revision: int = 0
    # 当前阶段（PHASE_*）：state 说"还能不能继续"，phase 说"此刻在做什么"
    phase: str = ""
    # 事件序号（每 run 单调递增，与落库的 seq 一致）
    event_seq: int = 0
    latest_objective: float | None = None
    best_iteration: int | None = None
    # 束流丢失保护是否**真的有效**，以及原因（自动模式启动校验与界面提示都看它）
    protection_reasons: list[str] = field(default_factory=list)


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
        # Optuna study 持久化库（与 tuning_spool.sqlite3 同目录）。
        # 传 None 给 gp 引擎；Optuna 引擎用它跨会话保留 trials，事后算重要性/挂 dashboard。
        self._optuna_storage_url = (
            f"sqlite:///{store.directory / 'tuning_studies.sqlite3'}"
        )

    @property
    def _signals(self) -> SignalWriteService:
        return self._signals_provider()

    @property
    def optuna_storage_url(self) -> str:
        """Optuna study 持久化库 URL（供分析 API / dashboard 定位）。"""
        return self._optuna_storage_url

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def status(self, run_id: str) -> TuningRunStatus:
        run = self._require(run_id)
        with self._lock:
            return self._status_of(run)

    def iterations(self, run_id: str) -> TuningIterationsResponse:
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                return TuningIterationsResponse(
                    run_id=run_id,
                    iterations=list(run.iterations),
                    max_iterations=run.request.max_iterations,
                )
        # 内存没有（服务重启后）：从暂存库读历史轮次
        stored = self._store.load_iterations(run_id)
        run_row = self._store.load_run(run_id)
        max_iters = int(run_row["max_iterations"]) if run_row else 0
        return TuningIterationsResponse(
            run_id=run_id,
            iterations=stored,
            max_iterations=max_iters,
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
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if all(run.state in TERMINAL_STATES for run in self._runs.values()):
                    return True
            time.sleep(0.02)
        return False

    def join(self, run_id: str, timeout: float = 30.0) -> None:
        """等待任务的驱动线程彻底结束（含收尾写库），测试与 CLI 场景用。"""
        run = self._require(run_id)
        thread = run.driver
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def _require(self, run_id: str) -> _Run:
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            run = self._recover_terminal_run(run_id)
        if run is None:
            raise TuningError(f"没有这个调束任务：{run_id}")
        return run

    def _recover_terminal_run(self, run_id: str) -> _Run | None:
        """服务重启后，内存 _Run 已丢，但终态任务可能还要做设备处置。

        从 spool 重建只含处置所需字段的 _Run；非终态不重建。
        """
        import traceback
        try:
            row = self._store.load_run(run_id)
            if row is None:
                return None
            state = TuningState(row["state"])
            if state not in TERMINAL_STATES:
                return None
            variables = [
                TuningVariable(**v)
                for v in json.loads(row["variables_json"] or "[]")
            ]
            request = TuningRunRequest(
                target_signal=row["target_signal"],
                variables=variables,
                mode=row["mode"],
                max_iterations=int(row["max_iterations"] or 1),
            )
            groups = {
                self._entry(v.signal).group
                for v in variables
                if v.enabled
            }
            run = _Run(
                run_id=run_id,
                request=request,
                dimensions=[],
                optimizer=None,  # type: ignore[arg-type]
                state=state,
                message=row["message"] or "",
                locked_groups=sorted(g for g in groups if g),
            )
            if row["best_values_json"]:
                try:
                    run.best_values = {
                        k: float(v)
                        for k, v in json.loads(row["best_values_json"]).items()
                    }
                except (ValueError, TypeError):
                    pass
            if row["snapshot_json"]:
                try:
                    run.snapshot = {
                        k: float(v)
                        for k, v in json.loads(row["snapshot_json"]).items()
                    }
                except (ValueError, TypeError):
                    pass
            with self._lock:
                self._runs[run_id] = run
            return run
        except Exception:
            traceback.print_exc()
            return None

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
            engine=run.request.engine,
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
            revision=run.revision,
            phase=run.phase,
            latest_objective=run.latest_objective,
            best_iteration=run.best_iteration,
            pause_requested=run.state == TuningState.PAUSED,
            stop_requested=run.stopped,
            protection_ready=self._protection_ready(run),
            protection_reasons=list(run.protection_reasons),
        )

    # ------------------------------------------------------------------
    # 过程事件流（第二批）
    # ------------------------------------------------------------------
    def _emit(
        self,
        run: _Run,
        message: str,
        *,
        level: str = LEVEL_INFO,
        phase: str | None = None,
        iteration: int | None = None,
        **data: object,
    ) -> TuningEvent:
        """记一条带毫秒时间戳的过程事件，并推进状态版本号。

        事件是"过程"，轮次记录是"结果"。**任何会写设备的动作都必须先有事件**：
        现场复盘时最先被问的就是"这一步到底做了没有"。
        """
        with self._lock:
            run.event_seq += 1
            seq = run.event_seq
            if phase is not None:
                run.phase = phase
            run.revision += 1
            current_phase = run.phase
        event = TuningEvent(
            run_id=run.run_id,
            seq=seq,
            at=_now(),
            level=level,
            phase=current_phase,
            iteration=iteration,
            message=message,
            data={key: value for key, value in data.items() if value is not None},
        )
        try:
            self._store.append_event(event)
        except Exception:  # noqa: BLE001 —— 事件落库失败不该中断调束本身
            pass
        return event

    def _protection_ready(self, run: _Run) -> bool:
        """束流丢失保护此刻是否**真的**有效（关掉总开关即视为不需要保护 → True）。"""
        if not run.request.loss_protection:
            return True
        absolute = run.request.loss_absolute
        relative = run.request.loss_relative
        has_absolute = absolute is not None
        has_relative = relative is not None and relative > 0 and run.baseline_objective is not None
        return has_absolute or has_relative

    def _refresh_protection_reasons(self, run: _Run) -> list[str]:
        """把"保护为什么有效/无效"写成可展示的理由（界面直接转述）。"""
        reasons: list[str] = []
        request = run.request
        if not request.loss_protection:
            reasons.append("束流丢失保护已关闭：本轮不做异常判定，也不会自动回退")
        else:
            if request.loss_absolute is not None:
                reasons.append(f"绝对阈值 {request.loss_absolute:g} 已生效")
            else:
                reasons.append("未设置绝对阈值")
            if request.loss_relative and request.loss_relative > 0:
                if run.baseline_objective is not None:
                    reasons.append(
                        f"相对判据已生效（低于基线 {run.baseline_objective:g} 的 "
                        f"{request.loss_relative:.0%} 即判异常）"
                    )
                else:
                    reasons.append("相对判据不可用：启动前基线读不到")
            else:
                reasons.append("相对损失阈值未启用")
        with self._lock:
            run.protection_reasons = reasons
        return reasons

    def events(self, run_id: str, since: int = 0, limit: int = 2000) -> TuningEventsResponse:
        """增量拉取过程事件（``since`` = 客户端已收到的最大 seq）。"""
        self._require(run_id)  # 不存在的 run 明确报错，不返回空列表让人以为"还没开始"
        events = self._store.load_events(run_id, since=int(since), limit=int(limit))
        return TuningEventsResponse(
            run_id=run_id,
            events=events,
            latest_seq=self.latest_event_seq(run_id),
        )

    def latest_event_seq(self, run_id: str) -> int:
        """当前最大事件序号：内存里的计数与落库取大，避免重启后序号回退。"""
        stored = self._store.latest_event_seq(run_id)
        with self._lock:
            run = self._runs.get(run_id)
            memory = run.event_seq if run is not None else 0
        return max(stored, memory)

    def progress(self, run_id: str, after_iteration: int = -1) -> TuningProgressResponse:
        """状态 + 新增轮次 + 事件序号一次返回（客户端一次轮询拿齐，不会互相乱序）。"""
        status = self.status(run_id)
        response = self.iterations(run_id)
        iterations = [
            item
            for item in response.iterations
            if int(getattr(item, "iteration", 0)) > int(after_iteration)
        ]
        return TuningProgressResponse(
            run_id=run_id,
            status=status,
            iterations=iterations,
            latest_seq=self.latest_event_seq(run_id),
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
            run_id = str(uuid4())
            run = _Run(
                run_id=run_id,
                request=request,
                dimensions=dimensions,
                optimizer=build_optimizer(
                    dimensions, request,
                    storage=self._optuna_storage_url,
                    study_name=f"run_{run_id}",
                ),
                state=TuningState.VALIDATING,
                message="参数校验通过，准备占用设备",
            )
            run.optuna_study_name = f"run_{run_id}"
            self._runs[run.run_id] = run

        self._store.create_run(
            run.run_id, request.target_signal, request.mode, request.max_iterations,
            [v.model_dump() for v in request.variables], _now(),
        )
        self._store.set_algorithm(
            run.run_id, run.optimizer.ALGORITHM, run.optimizer.VERSION, request.seed
        )
        self._persist_state(run)
        self._emit(
            run,
            f"任务创建：{len(request.enabled_variables())} 个变量 · 最多 "
            f"{request.max_iterations} 轮 · 引擎 {run.optimizer.ALGORITHM} "
            f"{run.optimizer.VERSION}",
            level=LEVEL_INFO,
            phase=PHASE_IDLE,
            engine=run.optimizer.ALGORITHM,
            engine_version=run.optimizer.VERSION,
            seed=request.seed,
            mode=request.mode,
            strategy=request.strategy,
            target=request.target_signal,
            variables=[v.signal for v in request.enabled_variables()],
        )

        try:
            self._locks.acquire(groups, owner=run.run_id)
        except DeviceBusy as exc:
            self._emit(run, f"设备组被占用，任务拒绝启动：{exc}", level=LEVEL_ERROR)
            self._finish_failed(run, str(exc))
            return self.status(run.run_id)

        run.locked_groups = sorted(groups)
        with self._lock:
            run.started_at = _now()
            self._to(run, TuningState.PREPARING, "占用设备组")
        self._emit(
            run, f"已占用设备组：{'、'.join(sorted(groups)) or '（无）'}",
            phase=PHASE_PRECHECKING, groups=sorted(groups),
        )
        # 启动前快照：**必须先取到**再进入 RUNNING。取不到就等于退不回去，
        # 宁可不开（改造报告 §5.2 第一条）。
        # 顺序很关键：快照必须在"开始前复位"**之前**取。否则快照记的是复位后的值，
        # 现场看到的"恢复启动前参数"实际退回到的是复位点，而不是任务真正开始前的位置。
        problem = self._capture_snapshot(run)
        if problem is not None:
            self._emit(run, problem, level=LEVEL_ERROR)
            self._finish_failed(run, problem)
            return self.status(run.run_id)
        # 开始前复位：把参与变量写到起始值（自定义 start，未填取范围中值），
        # 保证每次同一起点、不继承上一次结果。
        if request.reset_before_start:
            reset_targets = {
                variable.signal: variable.resolved_start()
                for variable in request.enabled_variables()
            }
            self._emit(
                run,
                "开始前复位到起始值：" + "、".join(
                    f"{signal}={value:g}" for signal, value in reset_targets.items()
                ),
                phase=PHASE_WRITING,
                targets=reset_targets,
            )
            reset_applied, reset_problems = self._write_and_settle(run, reset_targets)
            if reset_problems:
                self._emit(
                    run, "开始前复位未完成：" + "；".join(reset_problems),
                    level=LEVEL_ERROR,
                )
                self._finish_failed(run, "开始前复位未完成：" + "；".join(reset_problems))
                return self.status(run.run_id)
            with self._lock:
                # 复位后设备停在新位置：两阶段策略的"保持值"从这里重新起算
                # （快照仍然保留复位前的值，供"恢复启动前参数"使用）
                run.held_values = {
                    signal: float(reset_applied.get(signal, target))
                    for signal, target in reset_targets.items()
                }
            self._emit(
                run,
                "复位完成：设备现停在各起始值（启动前快照仍保留，可随时退回）",
                phase=PHASE_PRECHECKING,
                applied=dict(reset_applied),
            )
        # auto 模式二次保护：基线读不到时相对判据无效，必须有绝对阈值兜底
        if request.mode == MODE_AUTO and run.baseline_objective is None:
            if request.loss_absolute is None:
                self._emit(
                    run,
                    "启动基线读取失败且未配置绝对阈值，自动模式拒绝启动",
                    level=LEVEL_ERROR,
                )
                self._finish_failed(
                    run,
                    "启动基线读取失败且未配置绝对束流保护阈值，自动模式拒绝启动。"
                    "请确认设备可读，或设置 loss_absolute。",
                )
                return self.status(run.run_id)
        with self._lock:
            self._to(run, TuningState.RUNNING, "开始调束")
        self._persist_state(run)
        self._emit(
            run,
            "保护状态：" + ("；".join(run.protection_reasons) or "未记录"),
            level=LEVEL_INFO if self._protection_ready(run) else LEVEL_WARN,
            phase=PHASE_IDLE,
            protection_ready=self._protection_ready(run),
        )
        if request.mode == MODE_AUTO:
            # 全自动：后台线程驱动整条循环（每轮含写设备/采样，秒级），
            # start 立即返回，客户端轮询状态与进度。
            self._spawn_auto_driver(run)
        else:
            self._propose_next(run)
        return self.status(run.run_id)

    def _spawn_auto_driver(self, run: _Run) -> None:
        """启动全自动模式的后台驱动线程（循环，不是递归）。

        start / resume 请求必须立即返回（客户端要轮询状态与进度），而每轮要写设备、
        等稳定、采样，耗时秒级——放进请求里会让 HTTP 挂起整个任务时长。
        线程与 stop() / pause() 通过 RLock 与 stopped 标志协作；任何异常
        都收尾为 FAILED（驱动线程兜底，不让异常无声消失）。

        为什么改成循环：旧实现在 ``_propose_next`` 与 ``_execute_pending`` 之间互相调用，
        每轮往调用栈上压两层帧。200 轮的 auto 任务要压 400+ 帧，再叠加 Python 默认
        1000 帧上限，属于"跑得越久越接近崩"的隐患；循环写法与轮次多少无关。

        **同时只允许一个驱动线程**：两个驱动各跑各的循环时，日志会出现
        "第 6 轮候选 → 第 5 轮开始执行"这种错位（各自 propose/execute 相互穿插），
        而且同一轮可能被写两次。这里显式拦住（重复的 /resume 也会走到这里）。
        """
        existing = run.driver
        if existing is not None and existing.is_alive():
            self._emit(
                run,
                f"已有驱动线程在运行（{existing.name}），忽略重复的驱动请求",
                level=LEVEL_WARN,
            )
            return
        thread = threading.Thread(
            target=self._auto_driver_loop, args=(run,), daemon=True
        )
        with self._lock:
            run.driver = thread
        thread.start()
        self._emit(run, f"自动调束驱动已启动（{thread.name}）", phase=PHASE_IDLE)

    def _auto_driver_loop(self, run: _Run) -> None:
        """全自动主循环：提候选 → 执行一轮 → 再提候选，直到终态 / 暂停 / 出错。"""
        try:
            while True:
                with self._lock:
                    if run.state in TERMINAL_STATES or run.stopped:
                        return
                    if run.state == TuningState.PAUSED:
                        # 暂停：参数冻结，候选先不提（resume 会重新拉起驱动）
                        return
                if run.pending is None:
                    self._propose_next(run)
                    with self._lock:
                        if run.state in TERMINAL_STATES or run.stopped:
                            return
                        if run.pending is None:
                            return  # 暂停或没提出候选（例如已到轮次上限但未收尾）
                status = self._execute_pending(
                    run.run_id, require_awaiting=False, chain=False
                )
                if status.state in TERMINAL_STATES:
                    return
        except Exception as exc:  # noqa: BLE001 —— 驱动线程兜底
            self._finish_failed(run, f"自动调束驱动异常：{exc}")
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
                self._emit(
                    run,
                    f"启动前快照读不到 {entry.label} 的实际回读",
                    level=LEVEL_WARN,
                    phase=PHASE_PRECHECKING,
                    signal=variable.signal,
                )
                continue
            snapshot[variable.signal] = float(value)
            self._emit(
                run,
                f"启动前快照：{entry.label} = {float(value):g} {entry.unit}",
                phase=PHASE_PRECHECKING,
                signal=variable.signal,
                value=float(value),
            )
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
        self._emit(
            run,
            (
                f"目标基线 {run.request.target_signal} = {float(baseline):g}"
                if baseline is not None
                else f"目标基线读不到：{run.request.target_signal}（相对损失判据不可用）"
            ),
            level=LEVEL_INFO if baseline is not None else LEVEL_WARN,
            phase=PHASE_PRECHECKING,
            baseline=None if baseline is None else float(baseline),
            snapshot=snapshot,
        )
        self._refresh_protection_reasons(run)
        return None

    @staticmethod
    def _auto_protection_ok(request: TuningRunRequest) -> bool:
        """全自动模式的前置安全：至少一个束流丢失判据有效。"""
        return request.loss_absolute is not None or (
            request.loss_relative is not None and request.loss_relative > 0
        )

    def _validate(self, request: TuningRunRequest) -> tuple[list[Dimension], set[str]]:
        """校验模式、信号与范围；返回维度列表与需要占用的设备组。"""
        if request.mode not in SUPPORTED_MODES:
            raise TuningError(
                f"暂不支持调束模式 {request.mode!r}，支持：{'、'.join(SUPPORTED_MODES)}"
            )
        if request.engine not in SUPPORTED_ENGINES:
            raise TuningError(
                f"未知的优化引擎 {request.engine!r}，支持：{'、'.join(SUPPORTED_ENGINES)}"
            )
        if request.mode == MODE_AUTO and not self._auto_protection_ok(request):
            raise TuningError(
                "自动模式（全自动写入）必须启用束流丢失保护：绝对归零阈值或"
                "相对损失阈值（loss_relative>0）至少一个有效，"
                "否则无人确认的自动写入没有兜底。"
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
            if not entry.tunable:
                raise TuningError(f"设备档案未允许该参数参与调束：{variable.signal}")
            if entry.signal.endswith(NEVER_TUNABLE_SUFFIXES):
                raise TuningError(f"保护/速率参数禁止参与调束：{variable.signal}")
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
        if not target.beam_target:
            raise TuningError(f"设备档案未允许该信号作为调束目标：{request.target_signal}")
        disallowed = [
            variable.signal
            for variable in variables
            if not beamline.is_upstream_of(
                self._entry(variable.signal).group, target.group
            )
        ]
        if disallowed:
            raise TuningError(
                "以下参数不在目标上游，禁止参与调束：" + "、".join(disallowed)
            )
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
        # 采样 Study 按阶段命名：逐变量阶段各一个，联合阶段一个；
        # 避免 TPE 因 search space 动态变化出问题。
        sample_study = f"run_{run.run_id}__sample__{stage}"
        if stage == STAGE_SEQUENTIAL and active_signals:
            sample_study += f"_{active_signals[0]}"
        optimizer = build_optimizer(
            dimensions, run.request,
            storage=self._optuna_storage_url,
            study_name=sample_study,
        )
        # 持久化 study 已含历史 trials（load_if_exists），不再重复 seed_history；
        # 内存 study 或首次创建时，把已观测投影到新维度接续记忆。
        if optimizer.n_observed == 0:
            history: list[tuple[list[float], float]] = []
            dropped = 0
            for iteration in run.iterations:
                if iteration.objective is None:
                    continue
                # **用设备实际回读**，而不是算法建议值：优化器学到的应该是
                # "设备在这个位置上的表现"。建议值与实际位置不一致时（斜坡限制、
                # 未到位、被钳位），拿建议值回灌等于喂给模型一对错配的 (x, y)。
                values = []
                for name in names:
                    raw = (iteration.readback or {}).get(name)
                    if raw is None:
                        raw = iteration.proposed.get(name)
                    if raw is None:
                        break
                    values.append(float(raw))
                if len(values) != len(names):
                    dropped += 1
                    continue
                history.append((values, iteration.objective))
            # 越界的观测**夹到当前范围的边界**而不是丢掉：
            # 设备当时的位置可能本来就在"设定范围"之外（操作员收窄了搜索区间），
            # 那条观测仍然带着真实响应面的信息；而 Optuna 的 create_trial 不接受
            # 落在分布之外的参数，所以必须夹。夹了多少条会写进事件日志。
            bounds = {d.name: (float(d.low), float(d.high)) for d in dimensions}
            clean: list[tuple[list[float], float]] = []
            clamped = 0
            for values, obj in history:
                adjusted: list[float] = []
                touched = False
                for name, value in zip(names, values):
                    low, high = bounds[name]
                    if value < low:
                        adjusted.append(low)
                        touched = True
                    elif value > high:
                        adjusted.append(high)
                        touched = True
                    else:
                        adjusted.append(value)
                clean.append((adjusted, obj))
                clamped += 1 if touched else 0
            if clean:
                optimizer.seed_history(clean)
            self._emit(
                run,
                f"阶段切换：{stage}"
                + (
                    f"（{active_signals[0]}）" if stage == STAGE_SEQUENTIAL and active_signals else ""
                )
                + f"；接续 {len(clean)} 条历史观测（按实际回读投影，"
                f"{clamped} 条被夹到当前范围边界，{dropped} 条因缺回读未接续）",
                phase=PHASE_PROPOSING,
                stage=stage,
                seeded=len(clean),
                clamped=clamped,
                dropped=dropped,
            )
        run.optimizer = optimizer

    # ------------------------------------------------------------------
    # 建议
    # ------------------------------------------------------------------
    def _propose_next(self, run: _Run) -> None:
        schedule_execute = False
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
        self._emit(
            run,
            f"第 {proposal.iteration + 1} 轮候选已生成（{stage_text}）："
            + "、".join(f"{name}={value:g}" for name, value in values.items())
            + (
                f"；预测 {proposal.predicted:.3f} ± {proposal.std:.3f}"
                if proposal.std
                else f"；预测 {proposal.predicted:.3f}"
            ),
            phase=PHASE_PROPOSING,
            iteration=proposal.iteration,
            active_signals=active,
            values=values,
            predicted=proposal.predicted,
            std=proposal.std,
            stage=run.stage,
        )
        with self._lock:
            if run.request.mode == MODE_AUTO:
                # 全自动：不进"等待人工确认"。暂停中候选先挂着（不迁移状态，
                # 等 resume 再执行）。执行由驱动线程的循环负责（见 _auto_driver_loop）——
                # 这里**不再**直接调 _execute_pending，否则又会变成互相递归。
                if run.state == TuningState.PAUSED:
                    self._persist_state(run)
                    return self.status(run.run_id)
                self._persist_state(run)
            else:
                self._to(
                    run,
                    TuningState.AWAITING_CONFIRMATION,
                    f"第 {proposal.iteration + 1} 轮候选已生成（{stage_text}），等待人工确认",
                )
        if run.request.mode != MODE_AUTO:
            self._emit(
                run,
                f"等待人工确认第 {proposal.iteration + 1} 轮候选",
                phase=PHASE_WAITING_CONFIRMATION,
                iteration=proposal.iteration,
            )
        self._persist_state(run)

    # ------------------------------------------------------------------
    # 确认并执行
    # ------------------------------------------------------------------
    def approve(self, run_id: str) -> TuningRunStatus:
        """人工确认当前候选：交给执行层校验并写入设备，然后测量目标。"""
        return self._execute_pending(run_id, require_awaiting=True)

    def _execute_pending(
        self,
        run_id: str,
        *,
        require_awaiting: bool = True,
        chain: bool = True,
    ) -> TuningRunStatus:
        """执行当前候选：联锁巡检 → 执行层写入 → 读回 → 采目标 → 记录 → 下一轮。

        ``chain=True``（confirm 模式与 /approve）：本轮结束后顺手提出下一轮候选；
        ``chain=False``（auto 模式驱动循环）：只执行这一轮，由循环继续推进——
        避免"提议 ↔ 执行"互相调用把调用栈越压越深。
        """
        run = self._require(run_id)
        with self._lock:
            if run.pending is None:
                raise TuningError("当前没有待确认的候选")
            if require_awaiting and run.state != TuningState.AWAITING_CONFIRMATION:
                raise TuningError(
                    f"当前没有待确认的候选（状态为 {run.state}）"
                )
            if not require_awaiting and run.state not in (
                TuningState.RUNNING,
                TuningState.AWAITING_CONFIRMATION,
            ):
                raise TuningError(f"当前状态不能执行候选（{run.state}）")
            proposal = run.pending
            self._to(run, TuningState.APPLYING, f"第 {proposal.iteration + 1} 轮写入设备")
        self._persist_state(run)
        self._emit(
            run,
            f"第 {proposal.iteration + 1} 轮开始执行"
            + (
                f"（本轮调整 {'、'.join(proposal.active_signals)}）"
                if proposal.active_signals
                else ""
            ),
            phase=PHASE_PRECHECKING,
            iteration=proposal.iteration,
            active_signals=list(proposal.active_signals or []),
        )

        # 应用层联锁：写设备之前巡检，越界即终止本轮（设备保持现状，锁交人工）
        try:
            check_interlocks(
                run.request.interlocks,
                self._signals.read_value,
            )
        except InterlockViolation as exc:
            self._emit(
                run, f"应用层联锁越界，本轮终止：{exc}", level=LEVEL_ERROR,
                phase=PHASE_PRECHECKING, iteration=proposal.iteration,
            )
            self._finish_failed(run, str(exc))
            return self.status(run_id)

        iteration, state_unknown = self._apply(run, proposal)

        if state_unknown:
            self._emit(
                run, "写入中断，设备实际状态未知，请人工确认后释放设备锁",
                level=LEVEL_ERROR, phase=PHASE_WRITING, iteration=proposal.iteration,
            )
            self._finish_failed(
                run, "写入中断，设备实际状态未知，请人工确认后释放设备锁",
                state_unknown=True,
            )
            return self.status(run_id)

        with self._lock:
            run.pending = None
            run.iterations.append(iteration)
            run.latest_objective = iteration.objective
            quality = getattr(iteration, "quality", "ok")
            # 有效轮次 = 真的测到目标且回读到位。阶段推进、早停计数、无效轮次守卫
            # 都只看它：把"写入被拒/读不到目标"的轮次也算成一轮进展，会让一个变量在
            # 一次有效测量都没有的情况下被判定"调完了"。
            scored = iteration.objective is not None and quality == "ok"
            # 先把"本轮之前的观测数"记下来，再喂本轮结果：区分"模型事先预期了这次下探"
            # 还是"看到结果之后才觉得正常"，靠的正是这个先后顺序。
            run.prior_observations = int(getattr(run.optimizer, "n_observed", 0) or 0)
            if scored:
                run.valid_rounds += 1
                run.invalid_streak = 0
            else:
                run.invalid_streak += 1
            if iteration.objective is not None and quality == "ok":
                ob_kwargs = {}
                if hasattr(run.optimizer, "study"):  # OptunaAsker 才有 user_attrs
                    ob_kwargs["user_attrs"] = {
                        "run_id": run.run_id,
                        "iteration": getattr(iteration, "iteration", 0),
                        "quality": quality,
                        "readback": dict(iteration.readback or {}),
                    }
                run.optimizer.observe(
                    [proposal.values[d.name] for d in run.optimizer.dimensions],
                    iteration.objective,
                    **ob_kwargs,
                )                # "有意义"的改进：提升幅度要超过 stall_fraction（相对当前最优）。
                # stall_fraction=0 时任何刷新都算改进（与旧行为一致）。
                threshold = run.best_objective
                meaningful = (
                    run.best_objective is None
                    or threshold is None
                    or iteration.objective
                    > threshold * (1.0 + max(0.0, run.request.stall_fraction))
                )
                if run.best_objective is None or iteration.objective > run.best_objective:
                    run.best_objective = iteration.objective
                    run.best_values = dict(iteration.readback)
                    run.best_iteration = iteration.iteration
                    self._store.set_best(
                        run.run_id, dict(iteration.readback), iteration.objective
                    )
                if meaningful:
                    run.no_improvement = 0
                else:
                    run.no_improvement += 1
            else:
                # 失败/未稳定/写入拒绝：标 FAIL，不喂优化器，不计历史最优
                fail = getattr(run.optimizer, "fail_trial", None)
                if callable(fail):
                    fail()
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
            if run.stage == STAGE_SEQUENTIAL and scored:
                # 只有真的测到目标才推进阶段：否则"这个变量调完了"是假的
                run.stage_rounds += 1
            improved = run.best_iteration == iteration.iteration
            best_objective = run.best_objective
            no_improvement = run.no_improvement
            valid_rounds = run.valid_rounds
            invalid_streak = run.invalid_streak
        self._store.append_iteration(run.run_id, iteration)
        self._emit(
            run,
            f"第 {iteration.iteration + 1} 轮记录完成："
            + (
                f"目标 {iteration.objective:.4f}"
                if iteration.objective is not None
                else "目标未取得"
            )
            + f"，质量 {iteration.quality}"
            + (f"，刷新历史最优（{best_objective:.4f}）" if improved else "")
            + (
                f"，耗时 {iteration.duration_ms / 1000:.2f}s"
                + (
                    f"（写入 {iteration.write_s:.2f}s · 稳定 {iteration.settle_s:.2f}s"
                    f" · 测量 {iteration.measure_s:.2f}s）"
                    if iteration.write_s is not None
                    else ""
                )
                if iteration.duration_ms
                else ""
            )
            + f"；累计有效 {valid_rounds}/{len(run.iterations)} 轮",
            level=LEVEL_INFO if scored else LEVEL_WARN,
            phase=PHASE_RECORDING,
            iteration=iteration.iteration,
            objective=iteration.objective,
            quality=iteration.quality,
            is_best=bool(improved),
            duration_ms=iteration.duration_ms,
            write_s=iteration.write_s,
            settle_s=iteration.settle_s,
            measure_s=iteration.measure_s,
            sample_count=iteration.sample_count,
            sample_spread=iteration.sample_spread,
            failure_step=iteration.failure_step,
            detail=iteration.detail,
            valid_rounds=valid_rounds,
        )

        reason = self._guard_reason(run, proposal, iteration)
        with self._lock:
            run.anomalies = 0 if reason is None else run.anomalies + 1
            strikes = max(1, run.request.loss_strikes)
            tripped = reason is not None and run.anomalies >= strikes
            anomaly_count = run.anomalies
            soft_warning = run.last_soft_warning
            run.last_soft_warning = ""
        if soft_warning:
            # 软阈值区间（探索下探）：只在日志里说明，不计数、不中止、不回退
            self._emit(
                run, soft_warning, level=LEVEL_WARN, phase=PHASE_RECORDING,
                iteration=iteration.iteration, soft_warning=True,
            )
        if reason is not None:
            self._emit(
                run,
                f"束流异常计数 {anomaly_count}/{strikes}：{reason}",
                level=LEVEL_WARN,
                phase=PHASE_RECORDING,
                iteration=iteration.iteration,
                anomalies=anomaly_count,
                strikes=strikes,
            )
        if tripped:
            if not run.request.auto_recover:
                # 不自动回退：把异常摆到台面上，让操作员决定（设备锁仍握着）
                self._emit(
                    run, f"束流异常连续 {anomaly_count} 轮且未启用自动回退：停止流程等人工处理",
                    level=LEVEL_ERROR, phase=PHASE_RECORDING,
                )
                self._finish_failed(
                    run,
                    f"束流异常已连续 {run.anomalies} 轮：{reason}；"
                    "已按配置停止自动流程，参数保持现状，请人工处理",
                    state_unknown=True,
                )
                return self.status(run_id)
            self._recover(run, reason)
            return self.status(run_id)

        # 连续无效轮次守卫：执行层/测量一直在失败时，继续跑只是在烧机时。
        # 它与束流保护分开：写入被拒是执行问题，不是束流丢失，收尾方式也不同。
        limit = max(1, int(run.request.max_invalid_rounds))
        if invalid_streak >= limit:
            self._emit(
                run,
                f"连续 {invalid_streak} 轮无效（写入被拒 / 目标读不到 / 未稳定），停止任务",
                level=LEVEL_ERROR,
                phase=PHASE_IDLE,
                invalid_streak=invalid_streak,
                limit=limit,
            )
            self._finish_failed(
                run,
                f"连续 {invalid_streak} 轮没有产生有效测量（最近一轮："
                f"{iteration.detail or iteration.quality}）；已停止任务，"
                "参数保持现状，请检查执行层与目标信号",
            )
            return self.status(run_id)

        # 收敛早停：连续 patience 轮没有**有意义**的改进即正常结束
        if run.request.patience > 0 and run.no_improvement >= run.request.patience:
            self._emit(
                run,
                f"连续 {no_improvement} 轮无有意义改进"
                f"（阈值 {run.request.stall_fraction:.1%}），触发收敛早停",
                phase=PHASE_IDLE, counter=no_improvement, patience=run.request.patience,
            )
            self._finish_completed(
                run,
                f"连续 {run.no_improvement} 轮无显著改进，收敛早停"
                f"（共 {len(run.iterations)} 轮，其中有效 {valid_rounds} 轮）",
            )
            return self.status(run_id)

        with self._lock:
            if run.stopped:
                self._finish_aborted(run)
                return self.status(run_id)
            self._to(run, TuningState.RUNNING, f"第 {iteration.iteration + 1} 轮完成")
        self._persist_state(run)
        if chain:
            self._propose_next(run)
        return self.status(run_id)

    def _write_with_confirmation(
        self,
        run: _Run,
        entry,
        signal: str,
        target: float,
        *,
        iteration: int | None = None,
        tol: float | None = None,
    ) -> tuple[str, float | None, str, float, int]:
        """写一路，并在**写入报错时用回读判断"能不能安全重试"**。

        返回 ``(status, actual, detail, seconds, ramp_steps)``，status 取值：

        * ``"ok"``：写入被接受，或写入报错但回读已经在容差内（响应丢了、值其实写进去了）；
        * ``"rejected"``：写入没成功，但**确认设备仍停在原值**——状态是清楚的，
          这一路按"写入被拒"处理，任务可以继续（由连续无效轮次守卫兜底）；
        * ``"unknown"``：回读既不是原值也不是目标，或根本读不到——**设备状态未知**，
          必须停下并保留设备锁交人工确认（旧行为）。

        为什么值得这么做：命令行网关每次操作都要起一个进程 + 建 CA 连接，
        68 轮下来上千次调用，偶尔一次连接慢就会撞上 3 秒超时。旧实现把这种
        **瞬时超时**直接升级成"设备状态未知、任务终止"，现场表现为"跑了一小时
        突然停了，参数还锁着"。有回读判据之后，绝大多数瞬时抖动可以在原地消化掉，
        而真正的不确定（值变了但不知道变成什么）仍然坚决停下来。
        """
        attempts = max(1, int(getattr(run.request, "write_retries", 0) or 0) + 1)
        tolerance = tol if tol is not None else (entry.settle_tol or 0.0)
        last_reason = "写入未成功"
        spent = 0.0
        for attempt in range(attempts):
            started = time.monotonic()
            result = self._signals.write(
                SignalWriteRequest(signal=signal, value=float(target), ramp=True)
            )
            elapsed = time.monotonic() - started
            spent += elapsed
            if result.accepted:
                return (
                    "ok",
                    float(result.applied) if result.applied is not None else float(target),
                    "",
                    spent,
                    len(result.ramp_steps or []),
                )

            last_reason = str(result.reason or "写入未成功")
            steps = len(result.ramp_steps or [])
            previous = result.previous
            actual = self._signals.readback(entry)
            if actual is not None and tolerance > 0 and abs(actual - float(target)) <= tolerance:
                self._emit(
                    run,
                    f"{entry.label} 写入报错但回读已在目标容差内（{float(actual):g}）："
                    "判定为响应丢失、实际已生效，按成功处理",
                    level=LEVEL_WARN,
                    phase=PHASE_WRITING,
                    iteration=iteration,
                    signal=signal,
                    target=float(target),
                    readback=float(actual),
                    write_error=last_reason,
                )
                return "ok", float(actual), last_reason, spent, steps
            if actual is None:
                self._emit(
                    run,
                    f"{entry.label} 写入报错且回读读不到：设备状态未知，不再重试",
                    level=LEVEL_ERROR,
                    phase=PHASE_WRITING,
                    iteration=iteration,
                    signal=signal,
                    write_error=last_reason,
                )
                return "unknown", None, last_reason, spent, steps
            unchanged = previous is not None and abs(float(actual) - float(previous)) <= max(
                tolerance, 1e-9
            )
            if not unchanged:
                self._emit(
                    run,
                    f"{entry.label} 写入报错，且回读既不是原值({previous})也不是目标"
                    f"（当前 {float(actual):g}）：设备状态未知，不再重试",
                    level=LEVEL_ERROR,
                    phase=PHASE_WRITING,
                    iteration=iteration,
                    signal=signal,
                    write_error=last_reason,
                    readback=float(actual),
                )
                return "unknown", float(actual), last_reason, spent, steps
            if attempt + 1 < attempts:
                self._emit(
                    run,
                    f"{entry.label} 写入报错（{last_reason}），但回读仍为原值 "
                    f"{float(actual):g}：设备未动，重试 {attempt + 1}/{attempts - 1}",
                    level=LEVEL_WARN,
                    phase=PHASE_WRITING,
                    iteration=iteration,
                    signal=signal,
                    write_error=last_reason,
                    attempt=attempt + 1,
                )
                self._sleep(WRITE_RETRY_BACKOFF_S)
        self._emit(
            run,
            f"{entry.label} 重试 {attempts - 1} 次仍未成功（{last_reason}），"
            "设备确认停在原位：本轮按写入未生效处理",
            level=LEVEL_WARN,
            phase=PHASE_WRITING,
            iteration=iteration,
            signal=signal,
            write_error=last_reason,
        )
        return "rejected", float(target), last_reason, spent, steps

    def _guard_reason(
        self, run: _Run, proposal: TuningProposal, iteration: TuningIteration
    ) -> str | None:
        """这一轮算不算"束流异常"？返回原因文本，None 表示正常。

        判据分三层（改造报告 §5.2 + 批次 3 的 D2/D3 修正）：

        1. **硬丢失**：目标读不到，或目标 ≤ 绝对阈值，或相对基线下降超过
           ``loss_relative_hard``（配了才用）→ 计异常，连续 ``loss_strikes`` 次触发保护；
        2. **软告警**：下降超过 ``loss_relative`` 但没到硬阈值 → **只警告**。
           优化器本来就会主动去试更差的点，一次 60% 的下探是预期内的采样，
           不该被当成束流丢失把整轮退回去（旧实现就是这么干的）；
        3. **乐观采样**：实测值与模型预测（``predicted ± std``）吻合的下探同样只警告——
           模型说"这里会低"，测出来真的低，属于正常探索。

        两个刻意的例外：
        * ``write_rejected``（写入被拒）**不计**束流异常：那是执行层拒绝了这次写入，
          设备根本没动，与束流无关；它由"连续无效轮次"守卫处理；
        * ``loss_protection=False`` 时整段跳过（SIM/演示）。
        """
        run.last_soft_warning = ""
        if not run.request.loss_protection:
            return None
        # 写入被拒：设备没动，谈不上束流丢失
        if iteration.quality == "write_rejected":
            return None
        if iteration.objective is None:
            return f"本轮没有有效目标读数（{iteration.detail or '原因未记录'}）"
        absolute = run.request.loss_absolute
        if absolute is not None and iteration.objective <= absolute:
            return f"目标 {iteration.objective:g} 已低于绝对阈值 {absolute:g}"
        baseline = run.baseline_objective
        if baseline is None or baseline <= 0:
            return None

        drop = (baseline - iteration.objective) / baseline
        soft = float(run.request.loss_relative or 0.0)
        hard = run.request.loss_relative_hard
        hard_floor = baseline * (1.0 - float(hard)) if hard is not None else None
        deep_loss = hard_floor is not None and iteration.objective < hard_floor
        over_soft = drop > soft if soft > 0 else False
        if not over_soft and not deep_loss:
            return None
        if hard_floor is not None and not deep_loss:
            # 软硬阈值之间的区间：允许，但要留痕
            run.last_soft_warning = (
                f"目标较基线下降 {drop:.0%}（软阈值 {soft:.0%}，硬阈值 {hard:.0%}）："
                "判断为探索下探，仅告警、不计异常、不回退"
            )
            return None
        if hard_floor is None and over_soft and self._expects_the_dip(
            run, proposal, iteration, baseline, soft
        ):
            run.last_soft_warning = (
                f"目标较基线下降 {drop:.0%}，但落在模型预测 "
                f"{proposal.predicted:.3f} ± {proposal.std:.3f} 内："
                "判断为预期内的探索下探，仅告警"
            )
            return None
        if hard_floor is not None:
            return (
                f"目标比启动前基线 {baseline:g} 下降超过 {float(hard):.0%}"
                f"（本轮 {iteration.objective:g}，低于硬阈值 {hard_floor:g}）"
            )
        return (
            f"目标比启动前基线 {baseline:g} 下降超过 "
            f"{soft:.0%}（本轮 {iteration.objective:g}）"
        )

    @staticmethod
    def _expects_the_dip(
        run: _Run,
        proposal: TuningProposal,
        iteration: TuningIteration,
        baseline: float,
        soft: float,
        *,
        sigmas: float = 3.0,
    ) -> bool:
        """模型是否**在事先就预期了**这次下探（用于把探索下探与束流丢失分开）。

        三个条件缺一不可，否则"宽容"会退化成"没有保护"：

        1. 模型**在本轮结果之前**已有观测（``run.prior_observations > 0``）：首轮的先验
           预测没有任何信息量，它"预测"出任何值都不构成"预期内的下探"；
        2. 模型自己就认为这里会低于软阈值（``predicted < baseline*(1-soft)``）：
           只有"模型说会低、测出来真的低"才叫探索；
        3. 预测的不确定度相对基线不大（``std <= 25% 基线``）：不确定度极大时
           任何实测值都落在 3σ 里，等于无条件放行。
        """
        if run.prior_observations <= 0:
            return False
        if proposal.std <= 0 or iteration.objective is None:
            return False
        if proposal.predicted >= baseline * (1.0 - soft):
            return False
        if proposal.std > 0.25 * abs(baseline):
            return False
        return abs(iteration.objective - proposal.predicted) <= sigmas * proposal.std

    def _wait_all_settled(
        self,
        run: _Run,
        specs: list[tuple],
        *,
        iteration: int | None = None,
        timeout: float = 20.0,
    ) -> dict[str, tuple[bool, float | None, float]]:
        """并行等所有信号回读进入各自容差；返回 signal → (是否稳定, 最后回读, 耗时)。

        一轮轮询同时读所有未到位信号，到位即摘除；每路按自己的容差与同一超时判定。
        与逐路 ``wait_settled`` 的区别：稳定等待耗时为 **max(各路到位时间)**，
        而不是各路之和——多参数同时写、同时等，正是"同时写入"省时间的核心。
        容差为 None 的路（无稳定判据）读一次当前值即视为已稳定
        （与 ``wait_settled`` 对 ``tolerance is None`` 的行为一致）。
        """
        pending: list[dict] = []
        results: dict[str, tuple[bool, float | None, float]] = {}
        for variable, entry, target, tol in specs:
            tolerance = tol if tol is not None else (entry.settle_tol or None)
            if tolerance is None:
                results[variable.signal] = (
                    True, self._signals.readback(entry), 0.0
                )
                continue
            pending.append({
                "signal": variable.signal,
                "entry": entry,
                "target": float(target),
                "tol": float(tolerance),
                "deadline": time.monotonic() + timeout,
                "started": time.monotonic(),
            })
        while pending:
            for item in list(pending):
                last = self._signals.readback(item["entry"])
                elapsed = time.monotonic() - item["started"]
                if last is not None and abs(float(last) - item["target"]) <= item["tol"]:
                    results[item["signal"]] = (True, float(last), elapsed)
                    pending.remove(item)
                elif time.monotonic() >= item["deadline"]:
                    results[item["signal"]] = (
                        False, None if last is None else float(last), elapsed
                    )
                    pending.remove(item)
            if pending:
                self._sleep(0.2)
        return results

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
            # 保护性写入同样走"回读确认 + 有限重试"：回退/收尾是最不能半途而废的动作，
            # 一次瞬时超时就把"退不回去"报到人工面前，代价比多试两次高得多。
            variable_tol = (
                variable.settle_tol
                if variable.settle_tol is not None else run.request.settle_tol
            )
            (
                status, actual_value, write_error, _spent, ramp_steps,
            ) = self._write_with_confirmation(
                run, entry, signal, float(want), tol=variable_tol
            )
            if status != "ok":
                problems.append(f"{entry.label} 写入未生效：{write_error}")
                continue
            settled, actual = self._signals.wait_settled(
                entry,
                float(want),
                tol=variable_tol,
                timeout=run.request.settle_timeout_s,
                readback_signal=entry.readback_signal or None,
            )
            if actual is not None:
                applied[signal] = float(actual)
            if not settled:
                current = actual if actual is not None else "读不到"
                problems.append(f"{entry.label} 未进入容差（当前 {current}）")
            self._emit(
                run,
                (
                    f"保护性写入 {entry.label} = {float(want):g}"
                    + (f"（斜坡 {ramp_steps} 步）" if ramp_steps > 1 else "")
                    + f"，回读 {applied.get(signal, float('nan')):g}"
                    if settled
                    else f"保护性写入 {entry.label} 后未进入容差（当前 {actual}）"
                ),
                level=LEVEL_INFO if settled else LEVEL_WARN,
                phase=PHASE_SETTLING,
                signal=signal,
                target=float(want),
                readback=applied.get(signal),
                settled=bool(settled),
            )
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
        self._emit(
            run,
            (
                f"束流丢失保护回退完成：{len(applied)} 路已按启动前快照复位"
                f"（{'、'.join(f'{k}={v:g}' for k, v in sorted(applied.items()))}）"
                if recovery.ok
                else f"束流丢失保护回退未完成：{'；'.join(problems)}"
            ),
            level=LEVEL_INFO if recovery.ok else LEVEL_ERROR,
            phase=PHASE_IDLE,
            reason=reason,
            restored=applied,
            detail="；".join(problems) or None,
        )

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

        targets, sources = self._finalize_targets(run, action)
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
            # 逐路的写入依据一起返回：现场要能核对"这个值是复现最优轮，还是快照兜底"
            detail="；".join(
                [*(f"{signal} ← {source}" for signal, source in sorted(sources.items())),
                 *problems]
            ),
            at=_now(),
        )
        self._store.set_finalize(run.run_id, result)
        with self._lock:
            run.finalize = result
        self._emit(
            run,
            (
                f"设备处置「{FINALIZE_LABELS[action]}」完成："
                + "、".join(f"{k}={v:g}" for k, v in sorted(applied.items()))
                if not problems
                else f"设备处置「{FINALIZE_LABELS[action]}」未完成：{'；'.join(problems)}"
            ),
            level=LEVEL_INFO if not problems else LEVEL_ERROR,
            phase=PHASE_IDLE,
            action=action,
            applied=applied,
            sources=sources,
            detail="；".join(problems) or None,
        )
        return result

    def _finalize_targets(
        self, run: _Run, action: str
    ) -> tuple[dict[str, float], dict[str, str]]:
        """算出这次动作每路要写到的值与**来源**；缺依据时明确拒绝，不猜。

        返回值第二项是"每路的依据"（``复现最优轮`` / ``启动前快照`` /
        ``起始值`` / ``安全值`` / ``映射下限``），会写进处置记录与事件日志——
        现场复盘时要能回答"这一路的值是从哪来的"。
        """
        if action == ACTION_APPLY_BEST:
            if not run.best_values:
                raise TuningError("这次任务还没有可用的最优轮，无法应用最优参数")
            targets: dict[str, float] = {}
            sources: dict[str, str] = {}
            last_readback: dict[str, float] = {}
            for item in run.iterations:
                for signal, value in (item.readback or {}).items():
                    last_readback[signal] = float(value)
            missing: list[str] = []
            for variable in run.request.enabled_variables():
                signal = variable.signal
                if signal in run.best_values:
                    targets[signal] = float(run.best_values[signal])
                    sources[signal] = "复现最优轮"
                elif signal in run.snapshot:
                    # 最优轮那一轮没读到这一路：用启动前快照兜底，并如实标注来源。
                    # 旧实现直接判"没有目标值"，于是**先写成功的几路已经动过了**，
                    # 再报"未完成"——现场看到的是一次半途而废的处置。
                    targets[signal] = float(run.snapshot[signal])
                    sources[signal] = "启动前快照（最优轮缺该路回读）"
                elif signal in last_readback:
                    targets[signal] = float(last_readback[signal])
                    sources[signal] = "最后一轮回读（最优轮与快照都缺）"
                else:
                    missing.append(signal)
            if missing:
                raise TuningError(
                    "最优轮、启动前快照与历史回读里都没有这些参数的值，"
                    f"无法安全地应用最优参数：{'、'.join(missing)}"
                )
            return targets, sources
        if action == ACTION_RESTORE_INITIAL:
            if not run.snapshot:
                raise TuningError(
                    "这次任务没有启动前快照（可能是旧版本任务），无法恢复初始参数"
                )
            return (
                {signal: float(value) for signal, value in run.snapshot.items()},
                {signal: "启动前快照" for signal in run.snapshot},
            )
        if action == ACTION_START_VALUES:
            targets = {
                variable.signal: variable.resolved_start()
                for variable in run.request.enabled_variables()
            }
            return targets, {signal: "起始值" for signal in targets}
        # 安全值：**优先用映射里的 safe_value**；没有就退到下限并如实标注；
        # 两者都没有时拒绝执行——静默按 0 下发是"看起来做了保护动作"。
        targets = {}
        sources = {}
        missing = []
        for variable in run.request.enabled_variables():
            entry = self._entry(variable.signal)
            safe = getattr(entry, "safe_value", None)
            if safe is not None:
                targets[variable.signal] = float(safe)
                sources[variable.signal] = "安全值"
            elif entry.min_value is not None:
                targets[variable.signal] = float(entry.min_value)
                sources[variable.signal] = "映射下限（未配置安全值）"
            else:
                missing.append(variable.signal)
        if missing:
            raise TuningError(
                "以下参数既没有配置安全值、也没有配置下限，无法确定要退到哪里："
                + "、".join(missing)
                + "。请在 PV 映射里为它们配置 safe_value 或 min_value。"
            )
        return targets, sources

    def _apply(self, run: _Run, proposal: TuningProposal) -> tuple[TuningIteration, bool]:
        """执行一轮：写**本轮激活的**参数 → 等稳定 → 读回 → 采目标。

        阶段 1（逐参数）只写一个变量，其余按"保持不动"处理——**不写它们**：
        对同一台设备重复写同样的值既没有意义，又会白白增加设备动作与等待时间。
        但这些变量的实际回读照样记下来，事后看得到"当时它们在哪"。

        这一轮同时是**过程日志的主要来源**：逐路写入结果、每路稳定等待耗时、
        逐次采样读数、耗时统计都在这里落成事件；四个时间点（proposed/applied/
        settled/measured）写进轮次记录，事后能回答"这轮时间花在哪一段"。
        """
        request = run.request
        active = set(proposal.active_signals or [v.signal for v in request.enabled_variables()])
        applied: dict[str, float] = {}
        readback: dict[str, float] = {}
        unsettled: list[str] = []
        proposed_at = proposal.at
        started = time.monotonic()
        # 分段计时：写入（含斜坡步进）/ 等稳定 / 测量各花多少时间。
        # 现场问"这一轮为什么慢"时，只有总耗时是答不出来的——本轮实测表明
        # 大头通常是**逐路缓变写入的斜坡限速**，而不是采样或保持时间。
        write_s = 0.0
        settle_s = 0.0
        measure_s = 0.0

        # 阶段 1：把本轮激活的变量全部写入（写请求是斜坡缓变、同步下发，
        # 逐路连续执行但**不等稳定**）；稳定确认统一放到阶段 2 并行等待。
        # 原来"写 A → 等 A 稳定 → 写 B → 等 B 稳定"，稳定耗时被逐路累加；
        # 改成"写 A → 写 B → 同时等 A/B 稳定"后，稳定等待从 sum 变 max，
        # 这就是同时写入省下来的时间。
        write_targets: list[tuple] = []
        for variable in request.enabled_variables():
            entry = self._entry(variable.signal)
            if variable.signal not in active:
                value = self._signals.readback(entry)
                if value is not None:
                    readback[variable.signal] = float(value)
                    self._emit(
                        run,
                        f"{entry.label} 本轮不调整，保持回读 {float(value):g} {entry.unit}",
                        phase=PHASE_WRITING,
                        iteration=proposal.iteration,
                        signal=variable.signal,
                        readback=float(value),
                        held=True,
                    )
                continue
            target_value = proposal.values[variable.signal]
            # 每参数回读容差：参数自己配了用参数值，否则回落请求级/映射默认
            variable_tol = (
                variable.settle_tol
                if variable.settle_tol is not None else request.settle_tol
            )
            (
                status, actual_value, write_error, write_elapsed, ramp_steps,
            ) = self._write_with_confirmation(
                run,
                entry,
                variable.signal,
                float(target_value),
                iteration=proposal.iteration,
                tol=variable_tol,
            )
            write_s += write_elapsed
            if status != "ok":
                unknown = status == "unknown"
                detail = (
                    f"{entry.label} 写入未生效（{write_error}）"
                    + (
                        "，且设备状态未知"
                        if unknown
                        else "，已回读确认设备停在原位"
                    )
                )
                self._emit(
                    run,
                    detail,
                    level=LEVEL_ERROR if unknown else LEVEL_WARN,
                    phase=PHASE_WRITING,
                    iteration=proposal.iteration,
                    signal=variable.signal,
                    target=float(target_value),
                    reason=write_error,
                    device_state_unknown=unknown,
                )
                return (
                    TuningIteration(
                        iteration=proposal.iteration,
                        proposed=dict(proposal.values),
                        applied=applied,
                        readback=readback,
                        target=None,
                        objective=None,
                        quality="write_rejected",
                        detail=detail,
                        at=_now(),
                        stage=proposal.stage,
                        proposed_at=proposed_at,
                        applied_at=None,
                        failure_step="write_rejected",
                        duration_ms=int((time.monotonic() - started) * 1000),
                        write_s=round(write_s, 3),
                        settle_s=round(settle_s, 3),
                        measure_s=round(measure_s, 3),
                    ),
                    unknown,
                )
            applied[variable.signal] = float(
                actual_value if actual_value is not None else target_value
            )
            self._emit(
                run,
                f"写入 {entry.label} = {applied[variable.signal]:g} {entry.unit}"
                + (
                    f"（斜坡 {ramp_steps} 步，耗时 {write_elapsed:.2f}s）"
                    if ramp_steps > 1
                    else f"（单步写入，耗时 {write_elapsed:.2f}s）"
                )
                + (f"，{write_error}" if write_error else ""),
                phase=PHASE_WRITING,
                iteration=proposal.iteration,
                signal=variable.signal,
                target=float(target_value),
                applied=applied[variable.signal],
                write_s=round(write_elapsed, 3),
            )
            write_targets.append((variable, entry, float(target_value), variable_tol))

        # 阶段 2：统一并行等所有已写入变量回读进入各自容差。
        # 每路有独立的到位判定，但轮询共享同一轮：稳定耗时 = max(各路到位时间)。
        if write_targets:
            settle_started = time.monotonic()
            settled_results = self._wait_all_settled(
                run,
                write_targets,
                iteration=proposal.iteration,
                timeout=request.settle_timeout_s,
            )
            settle_s = time.monotonic() - settle_started
            for variable, entry, target_value, variable_tol in write_targets:
                settled, actual, settle_elapsed = settled_results[variable.signal]
                if actual is not None:
                    readback[variable.signal] = float(actual)
                self._emit(
                    run,
                    (
                        f"{entry.label} 回读到位 {float(actual):g} {entry.unit}"
                        if settled and actual is not None
                        else (
                            f"{entry.label} 未在 {request.settle_timeout_s:g}s 内进入容差"
                            f"（当前 {float(actual):g}）" if actual is not None
                            else f"{entry.label} 回读读不到"
                        )
                    )
                    + f"（等待 {settle_elapsed:.2f}s）",
                    level=LEVEL_INFO if settled else LEVEL_WARN,
                    phase=PHASE_SETTLING,
                    iteration=proposal.iteration,
                    signal=variable.signal,
                    settled=bool(settled),
                    readback=None if actual is None else float(actual),
                    seconds=round(settle_elapsed, 3),
                )
                if not settled:
                    # 未稳定仍如实记录：实际回读才是设备真实位置。但质量要标出来，
                    # 否则事后会把「还没到位就测的目标值」当成可靠数据。
                    unsettled.append(entry.label)

        applied_at = _now()
        samples: list[float] = []
        measure_started = time.monotonic()
        # 「保持时间」：稳定判据过了之后再等一会儿——有些量（气路、真空）到位后
        # 仍然在缓慢变化，不等就是把过渡态当成稳态测进目标里。
        if request.hold_s:
            self._emit(
                run, f"保持 {request.hold_s:g}s 后测量目标", phase=PHASE_HOLDING,
                iteration=proposal.iteration, hold_s=request.hold_s,
            )
            self._sleep(request.hold_s)
        for index in range(request.samples_per_point):
            if index:
                self._sleep(TARGET_SAMPLE_INTERVAL_S)
            value = self._signals.read_value(request.target_signal)
            if value is not None:
                samples.append(float(value))
            self._emit(
                run,
                (
                    f"目标采样 {index + 1}/{request.samples_per_point}："
                    f"{run.request.target_signal} = {float(value):g}"
                    if value is not None
                    else f"目标采样 {index + 1}/{request.samples_per_point}：读不到"
                ),
                level=LEVEL_INFO if value is not None else LEVEL_WARN,
                phase=PHASE_MEASURING,
                iteration=proposal.iteration,
                sample_index=index,
                value=None if value is None else float(value),
            )
        measure_s = time.monotonic() - measure_started
        measured_at = _now()

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
                    proposed_at=proposed_at,
                    applied_at=applied_at,
                    settled_at=applied_at,
                    measured_at=measured_at,
                    failure_step="read_failed",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    write_s=round(write_s, 3),
                    settle_s=round(settle_s, 3),
                    measure_s=round(measure_s, 3),
                ),
                False,
            )

        objective = float(statistics.median(samples))
        spread = float(statistics.pstdev(samples)) if len(samples) > 1 else 0.0
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
                proposed_at=proposed_at,
                applied_at=applied_at,
                settled_at=applied_at,
                measured_at=measured_at,
                duration_ms=int((time.monotonic() - started) * 1000),
                sample_count=len(samples),
                sample_spread=spread,
                samples=samples,
                failure_step="unsettled" if unsettled else None,
                write_s=round(write_s, 3),
                settle_s=round(settle_s, 3),
                measure_s=round(measure_s, 3),
            ),
            False,
        )

    # ------------------------------------------------------------------
    # 终态
    # ------------------------------------------------------------------
    def pause(self, run_id: str) -> TuningRunStatus:
        """暂停任务：只在"不在写设备"的状态可暂停，参数保持现状。"""
        run = self._require(run_id)
        with self._lock:
            if run.state not in (
                TuningState.RUNNING,
                TuningState.AWAITING_CONFIRMATION,
            ):
                raise TuningError(
                    f"当前状态不能暂停（{run.state}）；写设备中请等本轮收尾"
                )
            self._to(run, TuningState.PAUSED, "已暂停（参数冻结，等待继续）")
        self._emit(run, "任务已暂停：参数冻结，等待继续", phase=PHASE_IDLE)
        self._persist_state(run)
        return self.status(run_id)

    def resume(self, run_id: str) -> TuningRunStatus:
        """继续暂停的任务：有挂起候选则按模式继续执行 / 等待确认。"""
        run = self._require(run_id)
        with self._lock:
            if run.state != TuningState.PAUSED:
                raise TuningError(f"任务不处于暂停状态（{run.state}）")
            self._to(run, TuningState.RUNNING, "继续调束")
        self._emit(run, "任务已继续", phase=PHASE_IDLE)
        self._persist_state(run)
        if run.request.mode == MODE_AUTO:
            # 全自动：继续执行必须回到**驱动线程**里跑循环。
            # 在 HTTP 线程上直接执行一轮会把请求挂住整个任务时长（而且循环会一直跑下去）。
            self._spawn_auto_driver(run)
            return self.status(run_id)
        if run.pending is not None:
            with self._lock:
                self._to(
                    run, TuningState.AWAITING_CONFIRMATION, "候选已生成，等待人工确认"
                )
            self._emit(
                run,
                f"挂起候选（第 {run.pending.iteration + 1} 轮）继续等待人工确认",
                phase=PHASE_WAITING_CONFIRMATION,
                iteration=run.pending.iteration,
            )
            self._persist_state(run)
        return self.status(run_id)

    def stop(self, run_id: str) -> TuningRunStatus:
        run = self._require(run_id)
        with self._lock:
            if run.state in TERMINAL_STATES:
                return self._status_of(run)
            run.stopped = True
            # 等确认 / 暂停期间可以直接停；正在写入的那一轮要等它收尾
            if run.state in (
                TuningState.AWAITING_CONFIRMATION,
                TuningState.PAUSED,
            ):
                self._finish_aborted(run)
                return self._status_of(run)
            run.message = "已请求停止，等待当前轮收尾"
        self._emit(
            run, "收到停止请求：等当前这一轮收尾后停止（不会中断正在写设备的一步）",
            level=LEVEL_WARN,
        )
        self._persist_state(run)
        return self.status(run_id)

    def _finish_completed(self, run: _Run, reason: str | None = None) -> None:
        self._locks.release(owner=run.run_id)
        with self._lock:
            run.pending = None
            self._to(
                run, TuningState.COMPLETED, reason or f"完成 {len(run.iterations)} 轮"
            )
            run.finished_at = _now()
            iterations = len(run.iterations)
            best = run.best_objective
        self._store.set_finished(run.run_id, run.finished_at)
        self._emit(
            run,
            f"调束完成：共 {iterations} 轮"
            + (f"，历史最优 {best:.4f}" if best is not None else "，没有有效目标测量"),
            phase=PHASE_IDLE, iterations=iterations, best_objective=best,
        )
        self._persist_state(run)

    def _finish_aborted(self, run: _Run, reason: str | None = None) -> None:
        """收尾为「已停止」。``reason`` 给了就用它——保护动作触发的收尾必须
        在状态文案里说清"为什么停"，否则界面上只剩一句"已停止"。"""
        final = reason or "已停止"
        self._locks.release(owner=run.run_id)
        with self._lock:
            run.pending = None
            drop = getattr(run.optimizer, "drop_pending_trial", None)
            if callable(drop):
                drop()
            self._to(run, TuningState.STOP_REQUESTED, "收到停止请求")
            self._to(run, TuningState.ABORTED, final)
            run.finished_at = _now()
        self._store.set_finished(run.run_id, run.finished_at)
        self._emit(run, f"任务结束（已停止）：{final}", level=LEVEL_WARN, phase=PHASE_IDLE)
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
        self._emit(
            run,
            (
                f"任务失败并进入「需人工确认设备状态」：{message}"
                if state_unknown
                else f"任务失败：{message}"
            ),
            level=LEVEL_ERROR,
            phase=PHASE_IDLE,
            state_unknown=state_unknown,
        )
        self._persist_state(run)

    def acknowledge_recovery(
        self, run_id: str, note: str = "", *, resume: bool = False
    ) -> TuningRunStatus:
        """人工确认设备状态：释放恢复锁；``resume=True`` 时**带着新的实际回读继续调束**。

        为什么要有 ``resume``：旧实现只释放设备锁，任务停在终态——现场表现为
        "跑了一小时突然停了，确认完发现不会继续，只能重开一轮"。
        继续的前提是**先把地面真值重新建立起来**，所以顺序是：

        1. 重新申请设备组（结束后锁已交还，别人可能正在用；占用则拒绝继续）；
        2. 逐路重读参与变量（带重试）——任一路读不到就**拒绝继续**并保持"需人工确认"，
           不知道设备在哪就继续调，等于拿旧认知写新设备；
        3. 用新回读更新"保持值"起点、清零异常计数与停止标记；
           **启动前快照不动**——它是回退锚点，不能被现场值覆盖；
        4. 回到 RUNNING，按模式继续：auto 交回驱动线程；confirm 重新提候选等确认。

        被打断的那一轮**没有记录**（写入未确认就不落库），事件流里保留了它的候选值，
        事后能对上账。
        """
        run = self._require(run_id)
        groups = set(run.locked_groups)
        owner = run.run_id
        with self._lock:
            if run.state != TuningState.RECOVERY_REQUIRED:
                raise TuningError(f"任务不处于恢复待确认状态：{run.state}")
            if resume and run.stopped:
                raise TuningError("任务已收到过停止请求，不能继续；请按「确认并停止」处理")
        if not resume:
            with self._lock:
                self._locks.release(owner=owner)
                suffix = f"（已人工确认：{note}）" if note else "（已人工确认）"
                run.message = run.message + suffix
            self._emit(
                run, f"操作员已确认设备状态{suffix}，设备锁已释放（任务保持终止）",
                note=note or None,
            )
            self._persist_state(run)
            return self.status(run_id)

        # ---- 继续：先建立新的地面真值，再恢复运行 ----
        try:
            self._locks.acquire(groups, owner=owner)
        except DeviceBusy as exc:
            raise TuningError(f"设备组当前不可用，无法继续调束：{exc}") from exc
        fresh: dict[str, float] = {}
        missing: list[str] = []
        for variable in run.request.enabled_variables():
            entry = self._entry(variable.signal)
            value = self._read_with_retry(lambda e=entry: self._signals.readback(e))
            if value is None:
                missing.append(entry.label)
                continue
            fresh[variable.signal] = float(value)
        if missing:
            self._locks.release(owner=owner)
            raise TuningError(
                "以下参数仍然读不到实际回读，无法确认设备状态，不能继续："
                + "、".join(missing)
                + "。请先排除读取故障，再重新确认。"
            )
        baseline_now = self._read_with_retry(
            lambda: self._signals.read_value(run.request.target_signal)
        )
        with self._lock:
            run.held_values.update(fresh)
            run.anomalies = 0
            run.invalid_streak = 0
            run.stopped = False
            run.finished_at = None
            run.phase = PHASE_IDLE
            self._to(run, TuningState.RUNNING, "操作员确认设备状态后继续调束")
        self._emit(
            run,
            "操作员确认设备状态（"
            + (note or "无备注")
            + "）→ 已重读全部参与变量建立新起点："
            + "、".join(f"{signal}={value:g}" for signal, value in sorted(fresh.items()))
            + (
                f"；当前目标 {float(baseline_now):g}"
                if baseline_now is not None
                else "；当前目标读不到"
            )
            + "。启动前快照保持不变（仍是回退锚点）；被中断的那一轮没有记录，"
            "将从当前实际位置重新提候选。",
            level=LEVEL_WARN,
            phase=PHASE_IDLE,
            note=note or None,
            snapshot=fresh,
            target_now=None if baseline_now is None else float(baseline_now),
        )
        self._persist_state(run)
        if run.request.mode == MODE_AUTO:
            self._spawn_auto_driver(run)
            return self.status(run_id)
        with self._lock:
            run.pending = None  # 被中断的候选不复活：从当前实际位置重新提
        self._propose_next(run)
        return self.status(run_id)

    # ------------------------------------------------------------------
    def _to(self, run: _Run, target: TuningState, message: str) -> None:
        run.state = transition_tuning(run.state, target)
        run.message = message

    def _persist_state(self, run: _Run) -> None:
        self._store.set_state(run.run_id, str(run.state), run.message)
