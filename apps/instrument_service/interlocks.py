"""应用层安全联锁：监控 PV 越界巡检（移植自 demo/auto_scan 的 safety.py）。

边界声明：硬件联锁由硬件/IOC 承担，应用检查不能替代它（架构文档 6.6）。
这里的规则是**补充巡检**：在每轮写设备之前检查一批"红线 PV"，越界就拒绝本轮
写入并终止任务，把设备状态明确留给人工确认。
"""

from __future__ import annotations

from collections.abc import Callable

from packages.contracts.tuning import TuningInterlock


class InterlockViolation(RuntimeError):
    """应用层安全互锁触发：监控 PV 越过红线。"""

    def __init__(self, pv: str, value: float, op: str, threshold: float) -> None:
        super().__init__(
            f"应用联锁触发：{pv} 实时值 {value:g} 越过红线 {op} {threshold:g}，"
            "已停止本轮写入"
        )
        self.pv = pv
        self.value = value
        self.op = op
        self.threshold = threshold


def check_interlocks(
    rules: list[TuningInterlock],
    read: Callable[[str], float | None],
    log: Callable[[str], None] | None = None,
) -> None:
    """巡检全部启用的联锁；越界 → 记日志并抛 InterlockViolation。

    ``read`` 按信号/PV 读实时值，读不到（None）则跳过——联锁宁可漏报也不
    拿"读不到"当越界（与束流保护同一口径：不拿猜测的数字当安全边界）。
    """
    for rule in rules:
        if not rule.enabled or not rule.pv:
            continue
        value = read(rule.pv)
        if value is None:
            continue
        violated = False
        if rule.op == ">" and value > rule.threshold:
            violated = True
        elif rule.op == "<" and value < rule.threshold:
            violated = True
        elif rule.op == ">=" and value >= rule.threshold:
            violated = True
        elif rule.op == "<=" and value <= rule.threshold:
            violated = True
        if violated:
            if log is not None:
                log(f"联锁越界：{rule.pv} = {value:g}（红线 {rule.op} {rule.threshold:g}）")
            raise InterlockViolation(rule.pv, value, rule.op, rule.threshold)
