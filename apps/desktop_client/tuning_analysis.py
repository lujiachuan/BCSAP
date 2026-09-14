"""调束过程的分析：单变量响应曲线、启发式建议、日志行（改造报告 §5.2「辅助分析能力」）。

三件事都只依赖**已经拿到的轮次记录**（``/tuning/runs/{id}/iterations``），所以在客户端
算，不新增服务端接口——服务端已经如实记下每轮的建议值/下发值/回读值/目标值，界面不该
再要一份"服务器算好的结论"（结论的规则会随现场经验改，放在界面里改一次就能用）。

规则沿用原 demo 的 ``TunerMonitor``（R1 贴边 / R2 停滞 / R3 抖动 / R4 目标不抬头），
每条规则**只触发一次**并按固定顺序输出，避免刷屏；每条都写明"看到什么、建议做什么"，
不做没有依据的推断。

**边界（写清楚免得被当成诊断）**：这些是启发式提示，不是结论。贴边不等于最优在范围外，
停滞也不等于到了物理上限——它们只是把"值得看一眼的地方"指出来。
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence

# 贴边判定：落在范围两端 3% 以内算"贴近"
EDGE_FRACTION = 0.03
# 连续多少轮里贴边 >=2 次才提示
EDGE_HITS = 2
EDGE_WINDOW = 3
# 至少要有这么多轮才做停滞/抖动判断（轮次太少时这些统计没有意义）
MIN_ROUNDS_FOR_TREND = 10
MIN_ROUNDS_FOR_JITTER = 8
# 停滞阈值：最近 5 轮的最优相对最早 5 轮提升不足 5%
STALL_FRACTION = 0.05
# 抖动阈值：相邻轮差值超过中位差值的 3 倍
JITTER_RATIO = 3.0
# 变量多到这个数就建议降维（原 demo 的阈值）
MANY_VARIABLES = 6

STAGE_TEXT = {"sequential": "逐参数优化", "joint": "联合微调"}


def _objective(record: Mapping) -> float | None:
    value = record.get("objective")
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # 丢掉 NaN


def response_curve(
    iterations: Sequence[Mapping],
    signal: str,
    *,
    field: str = "readback",
) -> tuple[list[float], list[float]]:
    """某个变量的「参数值—目标」散点，按参数值排序。

    x 取该轮的**实际回读**（拿不到就退回实际下发值、再退回建议值）：优化器建议了
    多少并不重要，设备当时到底在哪才是响应曲线的横坐标。y 取该轮的目标测量；
    没有目标测量（写入被拒、读不到目标）的轮次不参与画图——把 None 当 0 画，
    曲线会凭空多出几个"谷底"。
    """
    points: list[tuple[float, float]] = []
    for record in iterations:
        objective = _objective(record)
        if objective is None:
            continue
        value = None
        for key in (field, "applied", "proposed"):
            raw = (record.get(key) or {}).get(signal)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            break
        if value is None:
            continue
        points.append((value, objective))
    points.sort(key=lambda item: item[0])
    return [x for x, _y in points], [y for _x, y in points]


def curve_variables(iterations: Sequence[Mapping]) -> list[str]:
    """轮次记录里出现过的所有变量名（按首次出现顺序）。"""
    seen: list[str] = []
    for record in iterations:
        for key in ("readback", "applied", "proposed"):
            for signal in (record.get(key) or {}):
                if signal not in seen:
                    seen.append(str(signal))
    return seen


def best_point(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float] | None:
    """响应曲线上的最优点（目标最大；多个并列取参数值小的那个）。"""
    if not xs or len(xs) != len(ys):
        return None
    best = max(range(len(ys)), key=lambda index: (ys[index], -xs[index]))
    return float(xs[best]), float(ys[best])


def stage_of(record: Mapping) -> str:
    """这一轮的阶段文案（老记录没有阶段字段 → 如实说"未记录"）。"""
    stage = record.get("stage")
    return STAGE_TEXT.get(str(stage), "未记录阶段")


def startup_advice(variables: Sequence[str]) -> list[str]:
    """开跑前的建议（原 demo 的 R5：参与变量过多）。"""
    if len(variables) >= MANY_VARIABLES:
        return [
            f"参与变量 {len(variables)} 个较多：建议用「逐参数优化 → 联合微调」，"
            "或分批勾选降低维度——维度一高，同样的轮次摊到每个参数上就不够看出趋势了"
        ]
    return []


def advice(
    iterations: Sequence[Mapping],
    ranges: Mapping[str, tuple[float, float]] | None = None,
) -> list[str]:
    """按轮次记录给过程建议（每条最多出现一次）。"""
    out: list[str] = []
    rounds = [r for r in iterations if _objective(r) is not None]
    count = len(rounds)

    # R1 贴边：最近 3 轮里 >=2 轮贴在同一侧边界
    for signal, (low, high) in (ranges or {}).items():
        span = float(high) - float(low)
        if span <= 0:
            continue
        recent = rounds[-EDGE_WINDOW:]
        if count < 5 or not recent:
            continue
        hits = 0
        side = ""
        for record in recent:
            values = record.get("readback") or record.get("applied") or {}
            raw = values.get(signal)
            if raw is None:
                continue
            value = float(raw)
            if value >= float(high) - EDGE_FRACTION * span:
                hits += 1
                side = "上边界"
            elif value <= float(low) + EDGE_FRACTION * span:
                hits += 1
                side = "下边界"
        if hits >= EDGE_HITS and side:
            out.append(
                f"{signal} 最近 {len(recent)} 轮里 {hits} 轮贴近{side}："
                f"真正的最优可能在 [{low:g}, {high:g}] 之外，考虑放宽该变量的范围"
            )

    objectives = [_objective(r) for r in rounds]
    values = [float(value) for value in objectives if value is not None]

    # R2 停滞：最近 5 轮相对最早 5 轮几乎没有提升
    if count >= MIN_ROUNDS_FOR_TREND and values:
        first = max(values[:5])
        last = max(values[-5:])
        reference = max(abs(first), 1e-9)
        if (last - first) < STALL_FRACTION * reference:
            out.append(
                "近 10 轮目标提升不足 5%：可增加每变量轮次，或确认装置是否已稳定、"
                "是否已经到达物理上限"
            )

    # R3 抖动：相邻轮差值远大于整体中位差
    if count >= MIN_ROUNDS_FOR_JITTER and len(values) >= 2:
        diffs = sorted(abs(values[i] - values[i - 1]) for i in range(1, len(values)))
        median = statistics.median(diffs)
        if median > 0 and abs(values[-1] - values[-2]) > JITTER_RATIO * median:
            out.append(
                "目标值相邻轮抖动明显大于整体波动：可加大回读稳定超时或每轮保持时间，"
                "必要时提高观测噪声，避免把过渡态当成测量结果"
            )

    # R4 目标一直不抬头
    if count >= 5 and values and all(value <= 0 for value in values):
        out.append(
            "目标量始终 ≤ 0：确认优化目标选得对不对、装置是否出束、"
            "束流是否打在法拉第杯上"
        )

    return out


def log_lines(
    iterations: Sequence[Mapping],
    *,
    target_signal: str = "",
    advice_lines: Sequence[str] = (),
) -> list[str]:
    """调束日志：一轮一行（阶段 → 建议 → 下发 → 回读 → 目标）。

    文案照原 demo 的日志区（"建议 → 写入 → 回读 → 目标电流"）：事后复盘时，
    这四列必须都能看到，只有建议值看不出设备到底动了多少。
    """
    lines: list[str] = []
    for record in iterations:
        objective = _objective(record)
        lines.append(
            f"[第 {int(record.get('iteration', 0)) + 1} 轮·{stage_of(record)}] "
            f"建议 {_pairs(record.get('proposed') or {})} → "
            f"下发 {_pairs(record.get('applied') or {})} → "
            f"回读 {_pairs(record.get('readback') or {})} → "
            f"{target_signal or '目标'} "
            + ("--" if objective is None else f"{objective:.4f}")
            + _quality_note(record)
        )
    if advice_lines:
        lines.append("")
        lines.extend(f"[建议] {text}" for text in advice_lines)
    return lines


def _quality_note(record: Mapping) -> str:
    """这一轮的质量说明：``ok`` 不写，别的（未稳定/写入被拒/读不到）必须写。"""
    quality = str(record.get("quality") or "ok")
    detail = str(record.get("detail") or "")
    if detail:
        return f"（{quality}：{detail}）"
    return "" if quality == "ok" else f"（{quality}）"


def _pairs(values: Mapping) -> str:
    if not values:
        return "—"
    return "、".join(f"{signal}={float(value):g}" for signal, value in values.items())


def summary_lines(
    iterations: Sequence[Mapping],
    *,
    target_signal: str = "",
    best_iteration: int | None = None,
) -> list[str]:
    """导出日志的开头几行：这次调束的规模与结论（便于只读日志的人快速定位）。"""
    objectives = [(_objective(r), int(r.get("iteration", 0))) for r in iterations]
    scored = [(value, index) for value, index in objectives if value is not None]
    lines = [
        f"# 调束日志：共 {len(iterations)} 轮，其中 {len(scored)} 轮有目标测量",
        f"# 目标量：{target_signal or '（未记录）'}",
    ]
    if scored:
        best = max(scored, key=lambda item: item[0])
        lines.append(
            f"# 历史最优：第 {best[1] + 1} 轮 {best[0]:.4f}"
            + (f"（结果页记录的最优轮：第 {best_iteration + 1} 轮）"
               if best_iteration is not None and best_iteration != best[1] else "")
        )
    stages: list[str] = []
    for record in iterations:
        text = stage_of(record)
        if not stages or stages[-1] != text:
            stages.append(text)
    lines.append(
        "# 阶段顺序：" + (" → ".join(stages) if stages else "（还没有轮次）")
    )
    return lines
