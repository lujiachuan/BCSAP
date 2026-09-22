"""供开发和自动化验证使用的内存 EPICS 实现。"""

import math
from datetime import UTC, datetime
from threading import RLock
from uuid import UUID

from .base import Reading

# 束流最优工作点（SIM 物理模型）：
# 注：195/2500/2500/5100 是各参数的量程上限，不是最优；
# 把最优放在量程中部，让 Optuna 在 [0, 上限] 内真的能找到峰。
# DW1 skim: 0-200V，最优 100V
# DW2 引出1: 0-2500V，最优 1500V
# DW3 引出2: 0-2500V，最优 1500V
# DW4 通道4: 0-5100V，最优 3000V
_BEST = {
    "hv_array.dw01.voltage_setpoint": 100.0,
    "hv_array.dw02.voltage_setpoint": 1500.0,
    "hv_array.dw03.voltage_setpoint": 1500.0,
    "hv_array.dw04.voltage_setpoint": 3000.0,
}
# σ 取量程约 40%：4 个高斯相乘时顶部曲率是各维之和，
# σ 太窄会出现"顶点 12、旁边点就 4"的尖峰；放宽后峰附近平缓回落。
_SIGMA = {
    "hv_array.dw01.voltage_setpoint": 75.0,      # 量程 200V
    "hv_array.dw02.voltage_setpoint": 1000.0,    # 量程 2500V
    "hv_array.dw03.voltage_setpoint": 1000.0,    # 量程 2500V
    "hv_array.dw04.voltage_setpoint": 2000.0,    # 量程 5100V
}
_PEAK_BEAM = 12.0  # nA，最优点束流


def _compute_fc1_beam(values: dict[str, tuple[float, str]]) -> float:
    """根据当前 DW 电压算 fc1 束流。

    各参数高斯衰减取**几何平均**（乘积开 n 次方）：若直接相乘，4 个维度哪怕
    只各偏一点，衰减也会 4 次叠加，峰形变成"顶点 12、旁边就 4"的尖锥；
    几何平均后任意方向的截面都是同一条缓高斯，峰附近慢慢回落。
    """
    n = len(_BEST)
    log_sum = 0.0
    for sig, best in _BEST.items():
        v = values.get(sig, (best, ""))[0]
        sigma = _SIGMA[sig]
        log_sum += -0.5 * ((v - best) / sigma) ** 2
    return _PEAK_BEAM * math.exp(log_sum / n)


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
            if signal == "detector.fc1.beam_current":
                value = _compute_fc1_beam(self._values)
                unit = self._values[signal][1] if signal in self._values else "nA"
            else:
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
