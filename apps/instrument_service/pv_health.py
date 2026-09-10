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
    """按配置的网关模式创建网关。

    ``channel-access`` 为真实 EPICS 通道访问；``simulated`` 为内存模拟实现，
    供无 IOC 的开发机与自动化测试使用。
    """
    if config.gateway == "channel-access":
        return ChannelAccessGateway(
            paths={entry.signal: entry.pv for entry in config.entries},
            units={entry.signal: entry.unit for entry in config.entries},
            lib_dir=config.ca_lib_dir or None,
        )
    return SimulatedEpicsGateway(pv_mapping.simulated_seed_values(config))


def create_simulated_gateway(config: PvMappingConfig | None = None) -> SimulatedEpicsGateway:
    """按映射创建模拟网关（测试与演示用）。"""
    target = config or pv_mapping.default_config()
    return SimulatedEpicsGateway(pv_mapping.simulated_seed_values(target))


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
        config_version=f"{config.gateway}:{len(config.entries)}",
        summary=PvHealthSummary(
            total=len(items),
            connected=connected_count,
            required_failed=required_failed,
            optional_failed=optional_failed,
        ),
        items=items,
    )
