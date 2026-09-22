"""调束任务状态与合法转换。

比扫谱多一个 ``AWAITING_CONFIRMATION``：架构文档 6.6 要求第一版真实接入
保留「建议 → 人工确认执行」模式，所以「算法给了候选、等人点确认」是一个
必须显式建模的稳定状态，而不是夹在某个函数里的临时变量——它可能持续几分钟，
期间状态接口必须能如实回答"在等谁"。
"""

from enum import StrEnum


class TuningState(StrEnum):
    """调束任务的持久化业务状态。"""

    DRAFT = "draft"
    VALIDATING = "validating"
    PREPARING = "preparing"
    RUNNING = "running"
    # 已有候选，等待人工确认后才写设备
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    # 正在写入并等待读回稳定
    APPLYING = "applying"
    # 用户暂停：参数冻结，等待继续（只在不写设备时进入）
    PAUSED = "paused"
    STOP_REQUESTED = "stop_requested"
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


ALLOWED_TRANSITIONS: dict[TuningState, frozenset[TuningState]] = {
    TuningState.DRAFT: frozenset({TuningState.VALIDATING}),
    TuningState.VALIDATING: frozenset({TuningState.PREPARING, TuningState.FAILED}),
    TuningState.PREPARING: frozenset({TuningState.RUNNING, TuningState.FAILED}),
    TuningState.RUNNING: frozenset(
        {
            TuningState.AWAITING_CONFIRMATION,
            # auto 模式：候选生成后不经过等待确认，直接进写设备
            TuningState.APPLYING,
            TuningState.PAUSED,
            TuningState.STOP_REQUESTED,
            TuningState.COMPLETED,
            TuningState.FAILED,
        }
    ),
    # 等确认期间用户可以直接停止，或确认后进入写入
    TuningState.AWAITING_CONFIRMATION: frozenset(
        {TuningState.APPLYING, TuningState.PAUSED, TuningState.STOP_REQUESTED,
         TuningState.COMPLETED, TuningState.FAILED}
    ),
    # 暂停后可继续（RUNNING），也可直接停止
    TuningState.PAUSED: frozenset(
        {TuningState.RUNNING, TuningState.STOP_REQUESTED}
    ),
    TuningState.APPLYING: frozenset(
        {TuningState.RUNNING, TuningState.STOP_REQUESTED, TuningState.COMPLETED,
         TuningState.FAILED}
    ),
    TuningState.STOP_REQUESTED: frozenset(
        {TuningState.ABORTED, TuningState.RECOVERY_REQUIRED}
    ),
    TuningState.FAILED: frozenset({TuningState.RECOVERY_REQUIRED}),
    TuningState.COMPLETED: frozenset(),
    TuningState.ABORTED: frozenset(),
    TuningState.RECOVERY_REQUIRED: frozenset(),
}


class InvalidTuningTransition(ValueError):
    """请求了不允许的调束状态转换。"""


def transition_tuning(current: TuningState, target: TuningState) -> TuningState:
    """检查并返回合法的下一个状态。"""

    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTuningTransition(
            f"不允许从 {current.value} 转换到 {target.value}"
        )
    return target
