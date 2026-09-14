"""束线拓扑：设备段顺序、目标与上游变量的对应关系。

**这是可配置的工程假设，不是物理定论。** 顺序取自 ``device_profiles.GROUP_ORDER``
（台账与界面既有的排列：气路 → 溅射 → 真空 → 聚焦/漂移 → DW → BD → 磁铁 → 束流探测），
探测器归属按台账里的「束流探测」组。现场核对过束线实际布置后，**只需要改这个文件**
（或改设备档案里的 ``tunable`` / ``beam_target`` 标记），界面与执行层不用动。

为什么需要它：调束「按目标自动勾选上游参数」是原 demo 的核心便利，但**不能靠猜**——
先把束线上的先后顺序显式写下来，"FC1 的上游是哪些设备"就是一个可复核的查询，
而不是界面里的隐式约定（见改造报告 §5.2）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BeamStage:
    """束线上的一段。``groups`` 是这一段涉及的设备组（与映射里的 group 同名）。"""

    key: str
    label: str
    groups: tuple[str, ...]


# 从上游到下游排列。束流探测放在最后：目标是探测器读数，上游就是它前面的一切。
STAGES: tuple[BeamStage, ...] = (
    BeamStage("gas", "气体流量", ("气体流量",)),
    BeamStage("sputter", "溅射电源", ("溅射电源",)),
    BeamStage("vacuum", "腔体气压", ("腔体气压",)),
    BeamStage("ion_optics", "聚焦/漂移管", ("聚焦/漂移管",)),
    BeamStage("dw", "高压阵列 DW", ("高压阵列 DW",)),
    BeamStage("bd", "新高压电源 BD", ("新高压电源 BD",)),
    BeamStage("magnet", "磁铁电源", ("磁铁电源",)),
    BeamStage("detector", "束流探测", ("束流探测",)),
)

# 束流探测之外的一切都是"可调的上游"。这里显式列出来，是为了让"探测器不是变量"
# 这件事有唯一出处，而不是散在界面的过滤条件里。
DETECTOR_GROUP = "束流探测"

# 联动组合（磁铁同步组）：信息性数据，供界面提示。
# **本版不把它们当作一个优化维度**：成组调束需要服务端的原子批量下发，
# 否则一轮里分 4 次写会出现"部分成功"的中间状态（改造报告 §5.2）。
LINKED_VARIABLE_SETS: tuple[tuple[str, ...], ...] = (
    ("magnet.m1.current_setpoint", "magnet.m2.current_setpoint"),
    ("magnet.m3.current_setpoint", "magnet.m4.current_setpoint"),
    (
        "magnet.m1.current_setpoint",
        "magnet.m2.current_setpoint",
        "magnet.m3.current_setpoint",
        "magnet.m4.current_setpoint",
    ),
)


def stage_of_group(group: str) -> BeamStage | None:
    """设备组属于哪一段；没登记过的组返回 None（自定义组不会被当成上游）。"""
    for stage in STAGES:
        if group in stage.groups:
            return stage
    return None


def stage_key_of_group(group: str) -> str:
    stage = stage_of_group(group)
    return stage.key if stage else ""


def upstream_groups_of(group: str) -> tuple[str, ...]:
    """该组之前（**不含自身**）的所有设备组。

    目标在探测器上时，这些就是"按位置应该先调"的设备组；未知组返回空元组——
    "不知道它在束线哪一段"时不要假装知道，界面会显示"未登记"，由现场补拓扑。
    """
    stage = stage_of_group(group)
    if stage is None:
        return ()
    index = STAGES.index(stage)
    return tuple(g for earlier in STAGES[:index] for g in earlier.groups)


def is_upstream_of(candidate_group: str, target_group: str) -> bool:
    """``candidate_group`` 是否在 ``target_group`` 上游（探测器的上游就是全部可调组）。"""
    return candidate_group in upstream_groups_of(target_group)


def linked_sets_for(signals: list[str]) -> list[list[str]]:
    """给定信号里涉及的联动组合（信息性，供界面提示）。"""
    chosen = set(signals)
    return [
        list(linked)
        for linked in LINKED_VARIABLE_SETS
        if len(chosen & set(linked)) >= 2
    ]
