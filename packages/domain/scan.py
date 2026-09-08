"""扫谱任务状态与合法转换。"""

from enum import StrEnum


class ScanState(StrEnum):
    """扫谱任务的持久化业务状态。"""

    DRAFT = "draft"
    VALIDATING = "validating"
    PREPARING = "preparing"
    RUNNING = "running"
    STOP_REQUESTED = "stop_requested"
    COMPLETING = "completing"
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


ALLOWED_TRANSITIONS: dict[ScanState, frozenset[ScanState]] = {
    ScanState.DRAFT: frozenset({ScanState.VALIDATING}),
    ScanState.VALIDATING: frozenset({ScanState.PREPARING, ScanState.FAILED}),
    ScanState.PREPARING: frozenset({ScanState.RUNNING, ScanState.FAILED}),
    ScanState.RUNNING: frozenset(
        {ScanState.STOP_REQUESTED, ScanState.COMPLETING, ScanState.FAILED}
    ),
    ScanState.STOP_REQUESTED: frozenset(
        {ScanState.ABORTED, ScanState.RECOVERY_REQUIRED}
    ),
    ScanState.COMPLETING: frozenset({ScanState.COMPLETED, ScanState.FAILED}),
    ScanState.FAILED: frozenset({ScanState.RECOVERY_REQUIRED}),
    ScanState.COMPLETED: frozenset(),
    ScanState.ABORTED: frozenset(),
    ScanState.RECOVERY_REQUIRED: frozenset(),
}


class InvalidScanTransition(ValueError):
    """请求了不允许的扫谱状态转换。"""


def transition_scan(current: ScanState, target: ScanState) -> ScanState:
    """检查并返回合法的下一个状态。"""

    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidScanTransition(f"不允许从 {current.value} 转换到 {target.value}")
    return target

