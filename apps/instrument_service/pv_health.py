"""受控 PV 健康检查：按当前 PV 映射配置逐项探测。

PV 清单不再写死在本模块，而是来自 ``pv_mapping`` 持有的受控配置
（架构文档 6.2：执行服务启动时完整校验，客户端不直连 IOC）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter

from packages.contracts import (
    PvHealthItem,
    PvHealthResponse,
    PvHealthSummary,
    PvMappingConfig,
)
from packages.epics_adapter import (
    ChannelAccessGateway,
    EpicsGateway,
    SimulatedEpicsGateway,
)

from . import pv_mapping


def create_gateway(config: PvMappingConfig) -> EpicsGateway:
    """按受控映射创建真实 EPICS Channel Access 网关。

    设备访问只有这一条路径：没有可用 IOC 时健康检查会如实报未连接，不会退化成模拟。
    开发/联调请起 ``sim/`` 下的本地模拟 IOC。

    ``ca.dll`` 的定位由 ``packages.epics_adapter`` 自动完成（环境变量
    ``SPECTRUM_CA_LIB_DIR`` 可覆盖）；加载失败的原因会逐项写进健康明细。
    """
    return ChannelAccessGateway(
        paths={entry.signal: entry.pv for entry in config.entries},
        units={entry.signal: entry.unit for entry in config.entries},
    )


def create_simulated_gateway(config: PvMappingConfig | None = None) -> SimulatedEpicsGateway:
    """按映射创建模拟网关（测试与演示用）。

    同时按映射里的 ``readback_signal`` 建立「设定 → 回读」耦合，让模拟设备
    表现得像一个真的会跟随的电源，而不是一张扁平键值表。
    """
    target = config or pv_mapping.default_config()
    coupling = {
        entry.signal: entry.readback_signal
        for entry in target.entries
        if entry.readback_signal
    }
    return SimulatedEpicsGateway(
        pv_mapping.simulated_seed_values(target), coupling=coupling
    )


def check_pv_health(
    gateway: EpicsGateway, config: PvMappingConfig
) -> PvHealthResponse:
    """探测配置中的每个 PV，返回逐项明细与汇总。

    网关整体不可用（例如 ca.dll 加载失败）时不再抛异常，而是把失败原因
    写进每一项的 detail，让界面能显示「为什么全红」。
    """
    items: list[PvHealthItem] = []
    required_failed = 0
    optional_failed = 0

    connect_error: str | None = None
    try:
        gateway.connect()
    except Exception as exc:  # noqa: BLE001  连接层异常统一转为逐项明细
        connect_error = str(exc)

    for entry in config.entries:
        started = perf_counter()
        try:
            if connect_error is not None:
                raise ConnectionError(connect_error)
            reading = gateway.read(entry.signal)
            connected = reading.connected
            detail = None if connected else "PV 未连接"
            severity = reading.severity
        except (ConnectionError, KeyError, TimeoutError, PermissionError) as exc:
            connected = False
            detail = str(exc)
            severity = None
        latency_ms = round((perf_counter() - started) * 1000, 2)
        if not connected:
            if entry.required:
                required_failed += 1
            else:
                optional_failed += 1
        items.append(
            PvHealthItem(
                signal=entry.signal,
                pv=entry.pv,
                required=entry.required,
                connected=connected,
                readable=connected,
                writable=connected and entry.writable,
                severity=severity,
                latency_ms=latency_ms,
                detail=detail,
            )
        )

    connected_count = sum(item.connected for item in items)
    if required_failed:
        status = "unavailable"
    elif optional_failed:
        status = "degraded"
    else:
        status = "ready"
    return PvHealthResponse(
        status=status,
        checked_at=datetime.now(UTC).isoformat(),
        config_version=f"pv-mapping:{len(config.entries)}",
        summary=PvHealthSummary(
            total=len(items),
            connected=connected_count,
            required_failed=required_failed,
            optional_failed=optional_failed,
        ),
        items=items,
    )
