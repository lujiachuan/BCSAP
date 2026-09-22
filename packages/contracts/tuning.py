"""调束任务的跨进程契约。

对应架构文档 6.6：优化算法只提出候选参数，执行层独立完成边界、最大单步、
变化速率与人工模式检查，再写设备并读回。**每轮保存候选值、实际读回、
目标测量、质量状态、算法版本和随机种子**——建议值与实际执行值必须分开记录，
不能把建议值当成已执行值（文档 9.4）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

# 调束模式。
#   confirm — 建议 → 人工确认 → 执行（第一版默认）
#   auto    — 全自动：候选生成后直接进执行层（需启动前校验束流保护已启用）
MODE_CONFIRM = "confirm"
MODE_AUTO = "auto"
SUPPORTED_MODES: tuple[str, ...] = (MODE_CONFIRM, MODE_AUTO)

# 优化引擎。
#   gp     — 自研 GpEiOptimizer（高斯过程 + 期望改进，仅 numpy）
#   tpe / cmaes / random / qmc / grid — Optuna 采样器（ask/tell 适配）
ENGINE_GP = "gp"
ENGINE_TPE = "tpe"
ENGINE_CMAES = "cmaes"
ENGINE_RANDOM = "random"
ENGINE_QMC = "qmc"
ENGINE_GRID = "grid"
SUPPORTED_ENGINES: tuple[str, ...] = (
    ENGINE_GP,
    ENGINE_TPE,
    ENGINE_CMAES,
    ENGINE_RANDOM,
    ENGINE_QMC,
    ENGINE_GRID,
)
ENGINE_LABELS: dict[str, str] = {
    ENGINE_GP: "GP + EI（自研）",
    ENGINE_TPE: "TPE（Optuna）",
    ENGINE_CMAES: "CMA-ES（Optuna）",
    ENGINE_RANDOM: "Random（Optuna）",
    ENGINE_QMC: "QMC（Optuna）",
    ENGINE_GRID: "Grid（Optuna）",
}

# 优化策略（改造报告 §5.2「两阶段优化策略」）
#   joint                 — 所有勾选变量一起做贝叶斯优化（第一版行为）
#   sequential_then_joint — 先逐个变量单独优化（其余保持不动），再在最优点附近联合微调
STRATEGY_JOINT = "joint"
STRATEGY_SEQUENTIAL = "sequential_then_joint"
SUPPORTED_STRATEGIES: tuple[str, ...] = (STRATEGY_JOINT, STRATEGY_SEQUENTIAL)
STRATEGY_LABELS: dict[str, str] = {
    STRATEGY_JOINT: "全部联合优化",
    STRATEGY_SEQUENTIAL: "逐参数 → 联合微调",
}

# 阶段名（状态接口回传给界面显示"现在在哪个阶段、正在调哪个参数"）
STAGE_SEQUENTIAL = "sequential"
STAGE_JOINT = "joint"
STAGE_LABELS: dict[str, str] = {
    STAGE_SEQUENTIAL: "逐参数优化",
    STAGE_JOINT: "联合微调",
}

# 调束结束后的处置动作（改造报告 §5.2「完成后设备状态」）
ACTION_APPLY_BEST = "apply_best"
ACTION_RESTORE_INITIAL = "restore_initial"
ACTION_SAFE_VALUES = "safe_values"
# 回到（复位用的）起始值：每参数的自定义起始值，未填则取范围中值
ACTION_START_VALUES = "start_values"
FINALIZE_ACTIONS: tuple[str, ...] = (
    ACTION_APPLY_BEST,
    ACTION_RESTORE_INITIAL,
    ACTION_START_VALUES,
    ACTION_SAFE_VALUES,
)

# 运行阶段（`TuningRunStatus.phase`）：界面据此显示"设备此刻正在做什么"。
# 与状态机 state 的分工：state 回答"这个任务还能不能继续"（几个稳定状态），
# phase 回答"这一轮走到哪一步了"（秒级变化）。把两者混成一个字段的后果是
# 操作员在"写入设备"和"等待确认"里看到同一个字样，分不清该不该动手。
PHASE_PROPOSING = "proposing"
PHASE_PRECHECKING = "prechecking"
PHASE_WRITING = "writing"
PHASE_SETTLING = "settling"
PHASE_HOLDING = "holding"
PHASE_MEASURING = "measuring"
PHASE_RECORDING = "recording"
PHASE_WAITING_CONFIRMATION = "waiting_confirmation"
PHASE_IDLE = "idle_between_iterations"
PHASE_LABELS: dict[str, str] = {
    PHASE_PROPOSING: "生成候选",
    PHASE_PRECHECKING: "执行前校验",
    PHASE_WRITING: "写入设备",
    PHASE_SETTLING: "等待回读稳定",
    PHASE_HOLDING: "保持",
    PHASE_MEASURING: "测量目标",
    PHASE_RECORDING: "记录本轮",
    PHASE_WAITING_CONFIRMATION: "等待人工确认",
    PHASE_IDLE: "轮次之间",
}

# 事件级别（过程日志按级别过滤；error 必须能在界面上一眼看到）
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"
EVENT_LEVELS: tuple[str, ...] = (LEVEL_INFO, LEVEL_WARN, LEVEL_ERROR)


class TuningVariable(BaseModel):
    """一个参与优化的可调参数。"""

    model_config = ConfigDict(strict=True)

    signal: str
    label: str = ""
    low: float
    high: float
    enabled: bool = True
    # 自定义起始值：开始前复位、结束后"回到起始值"都用它；None = 取范围中值
    start: float | None = None

    @field_validator("low", "high", "start", mode="before")
    @classmethod
    def _coerce_number(cls, value: object) -> object:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return float(value)
        return value

    def resolved_start(self) -> float:
        """本参数的起始值：填了用填的，没填用范围中值。"""
        if self.start is not None:
            return float(self.start)
        return (float(self.low) + float(self.high)) / 2.0


class TuningCatalogStage(BaseModel):
    """束线上的一段：从上游到下游排列，用来解释"目标的哪些上游参数该参与调束"。"""

    model_config = ConfigDict(strict=True)

    key: str
    label: str
    groups: list[str]


class TuningCatalogTarget(BaseModel):
    """可作为优化目标的束流测量。"""

    model_config = ConfigDict(strict=True)

    signal: str
    label: str
    unit: str
    group: str
    stage: str


class TuningCatalogVariable(BaseModel):
    """可作为优化变量的设备参数（含映射里配置的边界，供界面预填范围）。"""

    model_config = ConfigDict(strict=True)

    signal: str
    label: str
    unit: str
    group: str
    stage: str
    low: float | None = None
    high: float | None = None
    max_step: float | None = None

    @field_validator("low", "high", "max_step", mode="before")
    @classmethod
    def _coerce_number(cls, value: object) -> object:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return float(value)
        return value


class TuningCatalog(BaseModel):
    """调束可选项目录：由执行服务按**当前**映射与束线拓扑算出。

    界面不再自己从映射里"凡是可写的都当变量、凡是只读的都当目标"——那是把设备语义
    交给界面猜（改造报告 §5.2）。这里的 ``excluded`` 说明被排除的量与原因，
    界面上可以直接讲清楚"为什么某个参数不在列表里"。
    """

    model_config = ConfigDict(strict=True)

    targets: list[TuningCatalogTarget]
    variables: list[TuningCatalogVariable]
    stages: list[TuningCatalogStage]
    # 目标信号 → 该目标的上游可调变量信号（按束线顺序）
    upstream: dict[str, list[str]] = {}
    # 联动组合（如磁铁同步组）：信息性，本版不作为独立优化维度
    linked_sets: list[list[str]] = []
    # 排除原因 → 被排除的信号（例如 "磁铁速率是保护参数，不参与优化"）
    excluded: dict[str, list[str]] = {}


class TuningRecovery(BaseModel):
    """束流丢失保护的一次执行记录（审计用）。

    为什么要结构化记录：回退是**保护动作**，事后必须能回答"为什么回退、退到了哪、
    哪一路没退到位"。只写一句日志不够——现场复盘时要按信号逐个核对实际回读。
    """

    model_config = ConfigDict(strict=True)

    reason: str
    at: str
    ok: bool
    # 信号 → 回退后的**实际回读**（不是下发值）
    restored: dict[str, float] = {}
    detail: str = ""


class TuningInterlock(BaseModel):
    """一条应用层安全红线：监控 PV + 比较符 + 阈值。

    对应 demo/auto_scan 的 InterlockRule：在每轮写设备之前检查监控 PV，
    越界就拒绝本轮写入并终止任务（补充巡检，不替代硬件联锁）。
    """

    model_config = ConfigDict(strict=True)

    enabled: bool = True
    pv: str
    op: str = ">"
    threshold: float = 0.0


class TuningRunRequest(BaseModel):
    """一次调束任务的参数。"""

    model_config = ConfigDict(strict=True)

    target_signal: str
    variables: list[TuningVariable]
    mode: str = MODE_CONFIRM
    max_iterations: int = 20

    # ---- 两阶段策略（改造报告 §5.2）----
    # joint = 全部联合；sequential_then_joint = 先逐参数、再联合微调
    strategy: str = STRATEGY_JOINT
    # 阶段 1 每个变量用多少轮（原 demo 的"每变量调用次数"）
    calls_per_variable: int = 3
    # 阶段 2 的联合范围 = 该参数**原范围** × 这个比例（围绕阶段 1 的最优点居中）
    joint_frac: float = 0.2
    # 每次写完后额外保持的时间（秒）：稳定到位之后再"泡一会儿"，给慢变量留时间
    hold_s: float = 0.0
    # 回读稳定判据；不填则用映射里设定条目自己的 settle_tol/settle_timeout
    settle_tol: float | None = None
    settle_timeout_s: float = 20.0
    # 每轮目标测量重复次数，取中位数抗毛刺
    samples_per_point: int = 3
    # GP 观测噪声（归一化到目标量纲），物理量带噪时按实测填写
    noise: float = 1e-6
    seed: int = 0
    # 开始建模前先纯随机探索的轮数（仅 CMA-ES / TPE 生效；其余引擎忽略）
    n_startup_trials: int = 5

    # 优化引擎（见上 ENGINE_*）；gp 保留自研回退
    engine: str = ENGINE_GP
    # 收敛早停：连续多少轮没有产生**有意义**的改进即正常结束；0 = 不启用
    patience: int = 0
    # "有意义"的判据：目标提升不到这个比例（相对当前历史最优）就不算改进。
    # 0 = 任何刷新都算改进（与旧行为一致）。噪声大时调大，避免早停计数被毛刺清零。
    stall_fraction: float = 0.0
    # 连续多少轮"无效"（写入被拒 / 目标读不到 / 未稳定且无目标）就停止：
    # 无效轮次既没有信息也不该无限占用设备与机时，但它是**执行问题**而不是束流丢失，
    # 所以单独计数、单独收尾，不混进束流保护的连续异常计数。
    max_invalid_rounds: int = 3
    # 应用层联锁规则：每轮写设备前巡检，越界即停止本轮并终止任务
    interlocks: list[TuningInterlock] = []

    # ---- 束流丢失保护（改造报告 §5.2）----
    # 启动时会记录各路**实际回读**作为回退快照；没有快照就无法保证退得回去，
    # 所以取不到快照会直接拒绝启动（宁可不开，也不要开了之后退不回来）。
    # 绝对阈值：目标值 ≤ 它就判"束流接近零"；None = 不做绝对判据
    loss_absolute: float | None = None
    # 相对损失**软**阈值：低于启动前基线的这个比例即视为异常
    # （0.5 = 掉到基线一半以下）
    loss_relative: float = 0.5
    # 相对损失**硬**阈值：只有掉得更深才算"束流丢失"。None = 不启用硬阈值，
    # 行为与旧版一致（按 loss_relative 直接判丢失）。
    # 为什么要分层：优化器本来就会主动探索更差的点，一次 60% 的下探是**预期内**的
    # 采样，不该被当成束流丢失把整轮退回去；把软阈值设在 0.5、硬阈值设在 0.8，
    # 中间的区间就变成"仅告警、不中止、不计异常"，真正的丢失（掉到 20% 以下）照样触发。
    loss_relative_hard: float | None = None
    # 连续几次异常才触发保护：单点毛刺不该把刚调好的参数全退回去
    loss_strikes: int = 2
    # 触发后是否自动回退到启动前快照；False = 只标记异常并停下等人工处理
    auto_recover: bool = True
    # 束流丢失保护总开关：False = 完全不做异常判定（SIM/演示或确认不需要时关掉）
    loss_protection: bool = True

    # 开始前先把参与变量复位到起始值（每参数自定义 start，未填取范围中值）：
    # 默认 False = 从设备当前值开始；True = 每次同一起点，不继承上一次结束位置。
    reset_before_start: bool = False

    @field_validator("settle_tol", "noise", "loss_absolute", "loss_relative",
                     "loss_relative_hard", "joint_frac", "hold_s", "stall_fraction",
                     mode="before")
    @classmethod
    def _coerce_number(cls, value: object) -> object:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return float(value)
        return value

    def enabled_variables(self) -> list[TuningVariable]:
        return [variable for variable in self.variables if variable.enabled]


class TuningProposal(BaseModel):
    """等待人工确认的候选参数。"""

    model_config = ConfigDict(strict=True)

    iteration: int
    values: dict[str, float]
    # 本轮**真正会被写**的参数（两阶段策略下阶段 1 只调一个变量，
    # 其余保持不动——界面要能说清"这轮到底动什么"）
    active_signals: list[str] = []
    stage: str = ""
    predicted: float
    std: float
    expected_improvement: float
    at: str


class TuningIteration(BaseModel):
    """一轮的完整记录：建议 → 实际下发 → 实际回读 → 目标测量。

    ``at`` 是**这一轮记录写入时**的时间戳（轮末）。四段时间戳是分批加上的：
    ``proposed_at`` / ``applied_at`` / ``settled_at`` / ``measured_at``——
    只有轮末一个时间戳时，"这轮为什么花了 40 秒"是答不出来的（是写入慢、
    稳定慢，还是采样慢），现场复盘只能靠猜。老记录这些字段为空，界面按
    "未记录"呈现，不编造。
    """

    model_config = ConfigDict(strict=True)

    iteration: int
    proposed: dict[str, float]
    applied: dict[str, float]
    readback: dict[str, float]
    target: float | None
    objective: float | None
    quality: str
    detail: str | None = None
    at: str
    # 这一轮属于哪个阶段（``sequential`` / ``joint``）。两阶段策略下"第 3 轮"
    # 并不足以说明当时在做什么，而阶段信息只存在于运行内存里——不记进每一轮，
    # 事后复盘（导出日志、看响应曲线）就分不清哪几轮是逐参数、哪几轮是联合微调。
    # 老记录没有这个字段：按 None 读，不猜。
    stage: str | None = None

    # ---- 分批时间戳与耗时（新增，全部可选）----
    proposed_at: str | None = None
    applied_at: str | None = None
    settled_at: str | None = None
    measured_at: str | None = None
    # 本轮从"提出候选"到"测完目标"的墙钟耗时（毫秒）
    duration_ms: int | None = None
    # 总耗时的分段构成（秒）：写入（含斜坡步进）/ 等回读稳定 / 测量目标。
    # 现场问"这一轮为什么慢"时，只有总耗时是答不出来的——实测大头通常是
    # 逐路缓变写入的斜坡限速，而不是采样次数或保持时间。
    write_s: float | None = None
    settle_s: float | None = None
    measure_s: float | None = None
    # 目标重复采样：次数、标准差与原始样本。
    # 只给中位数不给离散度，无法判断一次"提升"是真提升还是噪声。
    sample_count: int = 0
    sample_spread: float | None = None
    samples: list[float] = []
    # 本轮卡在哪一步（``write_rejected`` / ``unsettled`` / ``read_failed``）；
    # None = 没有卡住。它比 quality 更具体：quality 说"这轮不可信"，
    # failure_step 说"不可信是因为回读没到位"。
    failure_step: str | None = None


class TuningEvent(BaseModel):
    """一条带毫秒时间戳的过程事件（过程日志的最小单位）。

    为什么要有独立的事件流：轮次记录是**结果**，事件流是**过程**——逐路写入是否
    被拒、等稳定花了多久、采样读到几个值、保护为什么触发、回退每路退到哪，
    这些在轮次记录里都被压成了一行 ``quality``。现场最常问的"当时到底发生了什么"
    只能由事件流回答。
    """

    model_config = ConfigDict(strict=True)

    run_id: str
    # 每个 run 内单调递增，从 1 开始；客户端按它增量拉取（since=seq）
    seq: int
    at: str
    level: str = LEVEL_INFO
    phase: str = ""
    # 属于第几轮（0 起）；与某轮无关的事件（启动、暂停、处置）为 None
    iteration: int | None = None
    message: str
    # 结构化附加信息（写入值、耗时、被拒原因等），界面按需展示
    data: dict[str, Any] = {}


class TuningEventsResponse(BaseModel):
    """增量事件响应。"""

    model_config = ConfigDict(strict=True)

    run_id: str
    events: list[TuningEvent]
    # 服务端当前最大 seq：客户端下次带 since=latest_seq 继续拉
    latest_seq: int


class TuningFinalizeRequest(BaseModel):
    """调束结束后的设备处置动作。

    三种动作都是**写设备的**，所以必须显式确认（``confirm=True``）——界面上是
    二次确认框，服务端再挡一道，避免一次误点就把刚调好的束流参数推走。
    """

    model_config = ConfigDict(strict=True)

    # apply_best：按最优轮的实际回读下发
    # restore_initial：退回本次任务启动前的快照
    # safe_values：每路退到映射里配置的下限（没有下限则 0）
    action: str
    confirm: bool = False


class TuningFinalizeResult(BaseModel):
    """处置动作的执行结果：逐路**实际回读**与失败原因。"""

    model_config = ConfigDict(strict=True)

    ok: bool
    action: str
    message: str
    applied: dict[str, float] = {}
    detail: str = ""
    at: str


class TuningRunStatus(BaseModel):
    """任务状态快照。轮次明细单独走 iterations 端点。"""

    model_config = ConfigDict(strict=True)

    run_id: str
    state: str
    mode: str
    target_signal: str
    max_iterations: int
    completed_iterations: int
    # 优化策略与当前所处阶段（"现在在逐参数还是在联合微调、正在调哪个参数"）
    strategy: str = ""
    stage: str = ""
    stage_variable: str = ""
    stage_index: int = 0
    stage_total: int = 0
    best_objective: float | None = None
    best_values: dict[str, float] = {}
    pending: TuningProposal | None = None
    message: str = ""
    algorithm: str | None = None
    algorithm_version: str | None = None
    seed: int | None = None
    engine: str = ""
    locked_groups: list[str] = []
    started_at: str | None = None
    finished_at: str | None = None
    # 启动前的实际状态：各路参与变量的回读 + 目标基线。
    # 「初始回读」必须是**启动前**的快照，不能拿第一轮执行后的回读充数——
    # 那样"相对提升了多少"会被算错（改造报告 §5.2）。
    snapshot: dict[str, float] = {}
    baseline_objective: float | None = None
    # 快照的备注：例如"目标基线读不到，相对损失判据不可用"。
    # 不能塞进 message——message 会被后续每一轮的状态文案覆盖掉。
    snapshot_note: str = ""
    # 当前连续异常次数（束流丢失保护的计数）
    anomalies: int = 0
    recovery: TuningRecovery | None = None
    # 结束后最近一次处置动作的结果（关掉界面再打开还能看到做过什么）
    finalize: TuningFinalizeResult | None = None

    # ---- 实时进度与保护状态（新增）----
    # 状态版本号：每次持久化 +1。客户端用它判断"这份状态比上次新"，
    # 而不是靠时间戳比较（现场机器与服务端时钟可能有偏差）。
    revision: int = 0
    # 当前阶段（见 PHASE_*）：state 说"能不能继续"，phase 说"此刻在做什么"
    phase: str = ""
    # 最近一轮的目标测量与历史最优轮次
    latest_objective: float | None = None
    best_iteration: int | None = None
    # 是否已提交过暂停 / 停止请求（写入中的那一轮会先收尾）
    pause_requested: bool = False
    stop_requested: bool = False
    # 束流丢失保护**是否真的有效**：自动模式启动校验、以及界面"保护已生效"的提示
    # 都依据它。只检查"配置了阈值"不够——基线读不到时相对判据根本不成立。
    protection_ready: bool = False
    protection_reasons: list[str] = []


class TuningIterationsResponse(BaseModel):
    """已完成的轮次。"""

    model_config = ConfigDict(strict=True)

    run_id: str
    iterations: list[TuningIteration]
    max_iterations: int


class TuningProgressResponse(BaseModel):
    """状态 + 新增轮次 + 事件序号：一次轮询拿齐，避免两次响应互相乱序。

    旧路径分开拉 status 与 iterations 时，"状态说已完成 5 轮、轮次列表还是 4 轮"
    这种撕裂会短暂出现；放进同一个响应里就只有一个时间点。客户端再把
    ``latest_seq`` 交给 ``/events?since=`` 补齐过程细节。
    """

    model_config = ConfigDict(strict=True)

    run_id: str
    status: TuningRunStatus
    # 只回传 ``after_iteration`` 之后的轮次（默认全量，便于客户端补齐）
    iterations: list[TuningIteration]
    latest_seq: int
