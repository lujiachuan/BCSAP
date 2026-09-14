"""成组回落：把一批设定信号退回安全值，并确认真的到位。

架构依据（报告 6.6）：回落属于**设备安全收尾**，必须由执行服务做，理由有三条：

1. 客户端退出/崩溃时也必须回落——放在界面里就等于"关掉窗口就不回落"；
2. 成组轴必须**每一路都写**：只写第一路会让其余磁铁停在扫描结束时的电流上；
3. 速率必须真的下发到设备（映射里的 ``rate_signal``），并且要等回读到位再宣布
   完成——写请求返回的 ``readback`` 只是写后一次瞬时读，不是"已到位"。

失败语义：任一路写被拒/超时/未在容差内 → 返回 ``ok=False``，由调用方决定是
保留设备锁转入恢复待确认，还是仅记录异常。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from packages.contracts import SignalWriteRequest

from .signal_io import SignalWriteService


@dataclass(frozen=True, slots=True)
class RetractOutcome:
    """回落结果。``applied`` 是逐路回读信号的实际值。"""

    ok: bool
    message: str
    applied: dict[str, float] = field(default_factory=dict)
    # 写入中断导致设备状态未知：调用方应保留设备锁并要求人工确认
    state_unknown: bool = False


def retract_signals(
    signals: SignalWriteService,
    setpoint_signals: Sequence[str],
    current_a: float,
    rate_a_s: float | None,
    *,
    timeout_s: float | None,
) -> RetractOutcome:
    """把 ``setpoint_signals`` 全部退到 ``current_a``。

    ``timeout_s=None`` 表示逐路用**映射条目自己的** ``settle_timeout``（设备属性，
    磁铁是 60 s）——回落是安全动作，宁可等设备自己的超时，也不要因为界面上的
    一个短超时而误判"没到位"。

    顺序固定：**先写各路速率，再写各路电流，最后逐路等到位**。
    顺序反了会出现"电流已经在退、设备还在按旧速率走"的中间状态；速率写失败不
    阻止电流回落（退到安全值更重要），但会记在结果里。
    """
    if not setpoint_signals:
        return RetractOutcome(ok=False, message="没有指定要回落的设定信号")

    applied: dict[str, float] = {}
    notes: list[str] = []
    state_unknown = False

    # 1) 速率：只在映射配了 rate_signal 时下发
    if rate_a_s is not None:
        for signal in setpoint_signals:
            try:
                entry = signals.entry(signal)
            except Exception as exc:  # noqa: BLE001  映射缺失按失败记录，不中断回落
                notes.append(f"{signal}：{exc}")
                continue
            rate_signal = entry.rate_signal
            if not rate_signal:
                continue
            # ramp=True：速率条目在映射里同样带 max_step（如 1 A/s），一步写到位会被
            # 执行层按边界拒掉；让执行层按它自己的规则分步，而不是绕过校验。
            result = signals.write(
                SignalWriteRequest(signal=rate_signal, value=float(rate_a_s), ramp=True)
            )
            if not result.accepted:
                notes.append(f"速率 {rate_signal} 被拒：{result.reason}")
                state_unknown = state_unknown or result.device_state_unknown

    # 2) 电流：每一路都要写
    rejected: list[str] = []
    for signal in setpoint_signals:
        result = signals.write(
            SignalWriteRequest(signal=signal, value=float(current_a), ramp=True)
        )
        if not result.accepted:
            rejected.append(f"{signal}（{result.reason}）")
            state_unknown = state_unknown or result.device_state_unknown
    if rejected:
        return RetractOutcome(
            ok=False,
            message="回落写入被拒：" + "；".join(rejected + notes),
            applied=applied,
            state_unknown=state_unknown,
        )

    # 3) 等到位：逐路等回读进入各自容差，任一路不到位就不算完成
    unsettled: list[str] = []
    for signal in setpoint_signals:
        entry = signals.entry(signal)
        settled, value = signals.wait_settled(
            entry, float(current_a), timeout=timeout_s
        )
        source = entry.readback_signal or entry.signal
        if value is not None:
            applied[source] = float(value)
        if not settled:
            unsettled.append(source)

    if unsettled:
        return RetractOutcome(
            ok=False,
            message="回落未到位：" + "、".join(unsettled),
            applied=applied,
        )
    detail = f"，{len(applied)} 路回读已到位" if applied else ""
    if notes:
        detail += "（" + "；".join(notes) + "）"
    return RetractOutcome(ok=True, message=f"已回落到 {current_a:g}{detail}", applied=applied)
