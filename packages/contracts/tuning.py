"""调束任务的跨进程契约。

对应架构文档 6.6：优化算法只提出候选参数，执行层独立完成边界、最大单步、
变化速率与人工模式检查，再写设备并读回。**每轮保存候选值、实际读回、
目标测量、质量状态、算法版本和随机种子**——建议值与实际执行值必须分开记录，
不能把建议值当成已执行值（文档 9.4）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

# 调束模式。第一版只开放「建议→人工确认」（文档 6.6 的分阶段计划）。
MODE_CONFIRM = "confirm"
SUPPORTED_MODES: tuple[str, ...] = (MODE_CONFIRM,)


class TuningVariable(BaseModel):
    """一个参与优化的可调参数。"""

    model_config = ConfigDict(strict=True)

    signal: str
    label: str = ""
    low: float
    high: float
    enabled: bool = True

    @field_validator("low", "high", mode="before")
    @classmethod
    def _coerce_number(cls, value: object) -> object:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return float(value)
        return value


class TuningRunRequest(BaseModel):
    """一次调束任务的参数。"""

    model_config = ConfigDict(strict=True)

    target_signal: str
    variables: list[TuningVariable]
    mode: str = MODE_CONFIRM
    max_iterations: int = 20
    # 回读稳定判据；不填则用映射里设定条目自己的 settle_tol/settle_timeout
    settle_tol: float | None = None
    settle_timeout_s: float = 20.0
    # 每轮目标测量重复次数，取中位数抗毛刺
    samples_per_point: int = 3
    # GP 观测噪声（归一化到目标量纲），物理量带噪时按实测填写
    noise: float = 1e-6
    seed: int = 0

    @field_validator("settle_tol", "noise", mode="before")
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


class TuningRunStatus(BaseModel):
    """任务状态快照。轮次明细单独走 iterations 端点。"""

    model_config = ConfigDict(strict=True)

    run_id: str
    state: str
    mode: str
    target_signal: str
    max_iterations: int
    completed_iterations: int
    best_objective: float | None = None
    best_values: dict[str, float] = {}
    pending: TuningProposal | None = None
    message: str = ""
    algorithm: str | None = None
    algorithm_version: str | None = None
    seed: int | None = None
    locked_groups: list[str] = []
    started_at: str | None = None
    finished_at: str | None = None


class TuningIterationsResponse(BaseModel):
    """已完成的轮次。"""

    model_config = ConfigDict(strict=True)

    run_id: str
    iterations: list[TuningIteration]
    max_iterations: int
