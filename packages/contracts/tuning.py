"""调束任务的跨进程契约。

对应架构文档 6.6：优化算法只提出候选参数，执行层独立完成边界、最大单步、
变化速率与人工模式检查，再写设备并读回。**每轮保存候选值、实际读回、
目标测量、质量状态、算法版本和随机种子**——建议值与实际执行值必须分开记录，
不能把建议值当成已执行值（文档 9.4）。
"""

from __future__ import annotations

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
    # 收敛早停：连续多少轮没有产生新的历史最优即正常结束；0 = 不启用
    patience: int = 0
    # 应用层联锁规则：每轮写设备前巡检，越界即停止本轮并终止任务
    interlocks: list[TuningInterlock] = []

    # ---- 束流丢失保护（改造报告 §5.2）----
    # 启动时会记录各路**实际回读**作为回退快照；没有快照就无法保证退得回去，
    # 所以取不到快照会直接拒绝启动（宁可不开，也不要开了之后退不回来）。
    # 绝对阈值：目标值 ≤ 它就判"束流接近零"；None = 不做绝对判据
    loss_absolute: float | None = None
    # 相对损失阈值：低于启动前基线的这个比例即判异常（0.5 = 掉到基线一半以下）
    loss_relative: float = 0.5
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
                     "joint_frac", "hold_s", mode="before")
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
    """一轮的完整记录：建议 → 实际下发 → 实际回读 → 目标测量。"""

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


class TuningIterationsResponse(BaseModel):
    """已完成的轮次。"""

    model_config = ConfigDict(strict=True)

    run_id: str
    iterations: list[TuningIteration]
    max_iterations: int
