"""受控 PV 清单及只读健康检查。"""

from datetime import UTC, datetime
from time import perf_counter

from packages.contracts import (
    PvHealthItem,
    PvHealthResponse,
    PvHealthSummary,
)
from packages.epics_adapter import SimulatedEpicsGateway

CONFIG_VERSION = "simulated-device-1"

CONTROLLED_SIGNALS = (
    ("quadrupole.q1.current", "BL:Q1:ISET", 1.842, "A", True, True),
    ("quadrupole.q2.current", "BL:Q2:ISET", -0.625, "A", True, True),
    ("einzel.voltage", "BL:EL:VSET", 3.20, "kV", True, True),
    ("steerer.x", "BL:STEER:X", 0.08, "V", True, True),
    ("steerer.y", "BL:STEER:Y", -0.12, "V", True, True),
    ("source.voltage", "BL:SRC:VSET", 12.4, "kV", False, True),
    ("detector.current", "BL:DET:CURRENT", 8.31, "uA", True, False),
)


def create_simulated_gateway() -> SimulatedEpicsGateway:
    values = {signal: (value, unit) for signal, _pv, value, unit, _required, _writable in CONTROLLED_SIGNALS}
    return SimulatedEpicsGateway(values)


def check_pv_health(gateway: SimulatedEpicsGateway) -> PvHealthResponse:
    items: list[PvHealthItem] = []
    required_failed = 0
    optional_failed = 0

    gateway_connected = gateway.connect()
    for signal, pv, _value, _unit, required, writable in CONTROLLED_SIGNALS:
        started = perf_counter()
        try:
            if not gateway_connected:
                raise ConnectionError("EPICS 网关不可用")
            reading = gateway.read(signal)
            connected = reading.connected
            detail = None if connected else "PV 未连接"
            severity = reading.severity
        except (ConnectionError, KeyError) as exc:
            connected = False
            detail = str(exc)
            severity = None
        latency_ms = round((perf_counter() - started) * 1000, 2)
        if not connected:
            if required:
                required_failed += 1
            else:
                optional_failed += 1
        items.append(
            PvHealthItem(
                signal=signal,
                pv=pv,
                required=required,
                connected=connected,
                readable=connected,
                writable=connected and writable,
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
        config_version=CONFIG_VERSION,
        summary=PvHealthSummary(
            total=len(items),
            connected=connected_count,
            required_failed=required_failed,
            optional_failed=optional_failed,
        ),
        items=items,
    )
