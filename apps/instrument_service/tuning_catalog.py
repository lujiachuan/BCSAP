"""按当前 PV 映射与束线拓扑算出「调束可选项」目录。

为什么要有这一层：调束页原来直接遍历映射——凡是 ``writable`` 且 ``role=setpoint``
的都能当变量（于是磁铁的 ``current_rate_setpoint`` 也混进去了），凡是 ``role=readback``
的都能当目标（于是气压、电压回读也能被"最大化"）。这类错误不会报错，只会让优化器
朝没有物理意义的方向跑（改造报告 §5.2）。

现在：**能写 ≠ 该调，能读 ≠ 是目标**。设备档案给每条映射标 ``tunable`` / ``beam_target``，
这里只做汇总，并配上束线拓扑给出「目标的哪些上游参数该参与」。
"""

from __future__ import annotations

from packages.contracts import PvMappingConfig
from packages.contracts.tuning import (
    TuningCatalog,
    TuningCatalogStage,
    TuningCatalogTarget,
    TuningCatalogVariable,
)
from packages.domain import beamline

# 排除原因的说法要与界面提示一致，所以集中写在这里
REASON_RATE = "变化速率/保护类设定，不作为优化变量"
REASON_NOT_DECLARED = "设备档案未标记为可调（tunable）"
REASON_NOT_BEAM = "不是束流测量，不能作为优化目标"

# 变化速率/保护类设定**永远**不进变量列表——即使有人在映射文件里手改成了
# ``tunable=true``。把变化速率交给优化器去"优化"，等于让设备运动快慢变成搜索维度，
# 既没有物理意义，又让设备的实际运动不可预测。
NEVER_TUNABLE_SUFFIXES = (".current_rate_setpoint",)


def build_catalog(config: PvMappingConfig) -> TuningCatalog:
    """把映射汇总成调束目录（纯函数，便于测试与复用）。"""
    targets: list[TuningCatalogTarget] = []
    variables: list[TuningCatalogVariable] = []
    excluded: dict[str, list[str]] = {}

    for entry in config.entries:
        stage = beamline.stage_key_of_group(entry.group)
        if entry.beam_target and not entry.writable:
            targets.append(
                TuningCatalogTarget(
                    signal=entry.signal,
                    label=entry.label,
                    unit=entry.unit,
                    group=entry.group,
                    stage=stage,
                )
            )
        is_protected = entry.signal.endswith(NEVER_TUNABLE_SUFFIXES)
        if entry.tunable and entry.writable and not is_protected:
            variables.append(
                TuningCatalogVariable(
                    signal=entry.signal,
                    label=entry.label,
                    unit=entry.unit,
                    group=entry.group,
                    stage=stage,
                    low=entry.min_value,
                    high=entry.max_value,
                    max_step=entry.max_step,
                )
            )
            continue
        # 记下"本来像变量/目标但被排除"的量，界面可以直接解释原因
        if entry.writable and entry.role == "setpoint":
            reason = REASON_RATE if is_protected else REASON_NOT_DECLARED
            excluded.setdefault(reason, []).append(entry.signal)
        if not entry.writable and entry.role == "readback" and not entry.beam_target:
            excluded.setdefault(REASON_NOT_BEAM, []).append(entry.signal)

    variable_signals = [variable.signal for variable in variables]
    upstream: dict[str, list[str]] = {}
    for target in targets:
        groups = beamline.upstream_groups_of(target.group)
        upstream[target.signal] = [
            variable.signal
            for variable in variables
            if variable.group in groups
        ]

    return TuningCatalog(
        targets=targets,
        variables=variables,
        stages=[
            TuningCatalogStage(key=stage.key, label=stage.label, groups=list(stage.groups))
            for stage in beamline.STAGES
        ],
        upstream=upstream,
        linked_sets=beamline.linked_sets_for(variable_signals),
        excluded=excluded,
    )
