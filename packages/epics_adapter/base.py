"""EPICS 业务抽象，不包含具体 CA/PVA 依赖。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Reading:
    """带时间与质量信息的单个业务信号读数。"""

    signal: str
    value: float
    unit: str
    source_time: datetime
    received_time: datetime
    severity: int = 0
    connected: bool = True


class EpicsGateway(Protocol):
    """扫描和调束业务所依赖的最小 EPICS 接口。"""

    def connect(self) -> bool: ...

    def read(self, signal: str) -> Reading: ...

    def write(self, signal: str, value: float, command_id: UUID) -> Reading: ...

    def snapshot(self, signals: list[str]) -> dict[str, Reading]: ...

