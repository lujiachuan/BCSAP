"""客户端滚动缓冲：给顶栏与趋势图保存最近一段时间的信号采样。

为什么在客户端做
----------------
执行服务**没有任何时序库**（`scan_store`/`tuning_store` 只存每个扫描点、每轮迭代
的聚合结果），也没有推送通道——只能轮询。而手动控制页本来就在 1 Hz 拉全量快照，
顶栏那两路束流电流已经在每次响应里到手了，所以只要在客户端把它们留下来就够，
不需要新端点、不需要改契约。

容量：2 min @1 Hz = 120 点；默认保留 30 min = 1800 点/路，三路也就几 MB 以内。

关键约定
--------
* **时间戳来自服务端**（``received_time``），不是本地 ``now()``：一次 128 路快照是
  128 次串行 CA 读，耗时与抖动都不小，用本地时间会把读取耗时算进时间轴。
* **缺口就是缺口**：某次采样缺失（轮询失败、信号掉线）时不补点，留给绘图层断线。
  把缺失当 0 画出来会把"读取中断"显示成"电流平稳"，那是最坏的一种误读。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable

# 默认保留 30 分钟（对齐 demo 的趋势图保留时长）
DEFAULT_KEEP_S = 1800.0
# 单路最多留这么多点，防止有人把轮询周期调到很小时吃爆内存
DEFAULT_MAX_POINTS = 20000


class TrendBuffer:
    """按信号名保存 ``(时间戳, 值)`` 的滚动缓冲。"""

    def __init__(
        self,
        keep_s: float = DEFAULT_KEEP_S,
        max_points: int = DEFAULT_MAX_POINTS,
    ) -> None:
        self.keep_s = float(keep_s)
        self.max_points = int(max_points)
        self._series: dict[str, deque[tuple[float, float]]] = {}
        # 每次 push 都裁剪会白跑；按时间攒一下再裁
        self._last_trim = 0.0
        self._trim_interval_s = max(1.0, self.keep_s / 60.0)

    # ------------------------------------------------------------------
    def push(self, signal: str, value: float | None, stamp: float | None = None) -> bool:
        """写入一个采样点。

        ``value`` 为 None、或非有限值时**不写**（那代表这一拍没读到，是缺口）。
        时间戳必须单调不倒退，否则会破坏绘图与统计。
        """
        if value is None:
            return False
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        if number != number or number in (float("inf"), float("-inf")):
            return False

        moment = time.time() if stamp is None else float(stamp)
        series = self._series.setdefault(signal, deque(maxlen=self.max_points))
        if series and moment <= series[-1][0]:
            # 同一拍重复到达或时间戳倒退：丢掉，避免图上出现回折
            return False
        series.append((moment, number))
        self._maybe_trim(moment)
        return True

    def _maybe_trim(self, now: float) -> None:
        if now - self._last_trim < self._trim_interval_s:
            return
        self._last_trim = now
        self.trim(now)

    def trim(self, now: float | None = None) -> None:
        """丢掉超过 ``keep_s`` 的旧点。"""
        moment = time.time() if now is None else now
        cutoff = moment - self.keep_s
        for signal, series in list(self._series.items()):
            while series and series[0][0] < cutoff:
                series.popleft()
            if not series:
                del self._series[signal]

    # ------------------------------------------------------------------
    def window(
        self, signal: str, seconds: float, now: float | None = None
    ) -> list[tuple[float, float]]:
        """返回最近 ``seconds`` 秒内的点，按时间升序。"""
        series = self._series.get(signal)
        if not series:
            return []
        moment = time.time() if now is None else now
        cutoff = moment - float(seconds)
        return [(t, v) for t, v in series if t >= cutoff]

    def latest(self, signal: str) -> tuple[float, float] | None:
        series = self._series.get(signal)
        return series[-1] if series else None

    def value_at_or_before(self, signal: str, moment: float) -> float | None:
        """``moment`` 之前（含）最近的一个值，用于算变化量。"""
        series = self._series.get(signal)
        if not series:
            return None
        found: float | None = None
        for stamp, value in series:
            if stamp <= moment:
                found = value
            else:
                break
        return found

    def delta(self, signal: str, seconds: float, now: float | None = None) -> float | None:
        """相对 ``seconds`` 秒前的变化量；没有可比的历史返回 None。"""
        latest = self.latest(signal)
        if latest is None:
            return None
        moment = time.time() if now is None else now
        past = self.value_at_or_before(signal, moment - float(seconds))
        if past is None:
            return None
        return latest[1] - past

    def stats(self, signal: str, seconds: float, now: float | None = None) -> dict | None:
        """窗口内的统计量：当前/最小/最大/均值/峰峰值/标准差/点数。

        束流稳定性靠这几个数判断，绘图之外基本零成本。
        """
        points = self.window(signal, seconds, now)
        if not points:
            return None
        values = [v for _t, v in points]
        count = len(values)
        mean = sum(values) / count
        variance = sum((v - mean) ** 2 for v in values) / count
        return {
            "count": count,
            "latest": values[-1],
            "min": min(values),
            "max": max(values),
            "mean": mean,
            "range": max(values) - min(values),
            "std": variance**0.5,
            "span_s": points[-1][0] - points[0][0],
        }

    def signals(self) -> list[str]:
        return sorted(self._series)

    def signal_points(self, signal: str) -> int:
        return len(self._series.get(signal, ()))

    def clear(self, signals: Iterable[str] | None = None) -> None:
        if signals is None:
            self._series.clear()
            return
        for signal in signals:
            self._series.pop(signal, None)


__all__ = ["DEFAULT_KEEP_S", "DEFAULT_MAX_POINTS", "TrendBuffer"]
