"""供开发和自动化验证使用的内存 EPICS 实现。"""

from datetime import UTC, datetime
from threading import RLock
from uuid import UUID

from .base import Reading


class SimulatedEpicsGateway:
    """保存业务信号当前值的线程安全模拟网关。

    ``coupling`` 给出「设定信号 → 随动回读信号」的对应关系：写入设定值时，
    被耦合的回读信号同步跟随。没有这层耦合，模拟网关就是个扁平键值表，
    回读永远不跟着设定变——于是每一次等稳定的判断都会超时，
    开发与测试阶段就没法验证稳定判据那段逻辑。
    """

    def __init__(
        self,
        values: dict[str, tuple[float, str]] | None = None,
        coupling: dict[str, str] | None = None,
    ) -> None:
        self._values = dict(values or {})
        self._coupling = dict(coupling or {})
        self._lock = RLock()

    def connect(self) -> bool:
        return True

    def read(self, signal: str) -> Reading:
        with self._lock:
            try:
                value, unit = self._values[signal]
            except KeyError as exc:
                raise KeyError(f"未配置模拟信号：{signal}") from exc
        return self._reading(signal, value, unit)

    def write(self, signal: str, value: float, command_id: UUID) -> Reading:
        del command_id
        with self._lock:
            try:
                unit = self._values[signal][1]
            except KeyError as exc:
                raise KeyError(f"未配置模拟信号：{signal}") from exc
            self._values[signal] = (float(value), unit)
            follower = self._coupling.get(signal)
            if follower and follower in self._values:
                self._values[follower] = (float(value), self._values[follower][1])
        return self._reading(signal, float(value), unit)

    def snapshot(self, signals: list[str]) -> dict[str, Reading]:
        return {signal: self.read(signal) for signal in signals}

    @staticmethod
    def _reading(signal: str, value: float, unit: str) -> Reading:
        now = datetime.now(UTC)
        return Reading(
            signal=signal,
            value=value,
            unit=unit,
            source_time=now,
            received_time=now,
        )
