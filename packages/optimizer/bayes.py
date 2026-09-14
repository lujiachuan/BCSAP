"""贝叶斯优化：高斯过程回归 + 期望改进（EI）候选生成。

架构文档要求 ``packages/optimizer`` 不依赖 PySide6 / FastAPI / SQLAlchemy /
具体 EPICS 库——它只做一件事：**根据历史观测提出下一个候选参数**。
边界、单步、速率、联锁检查都在执行层做，优化器无权绕过。

为什么连 sklearn / scipy 也不用：项目核心依赖只有 numpy，而现场机器经常装不上
sklearn/scipy（本机 PyPI 不通，实测三者皆缺）。GP 回归是 n 很小的稠密线性代数，
EI 只需要标准正态的 CDF/PDF，用 numpy + math 自己写反而更容易审计，
也避免"优化器装不上 → 自动调束用不了"这种与算法无关的故障。

接口刻意做成**一次一步**（``propose`` / ``observe``）而不是 ``minimize(f, n)``：
执行层需要每轮都做校验、写设备、读回、必要时等人工确认，
把整个循环塞进优化器就没法插这些步骤了。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

_SQRT_2PI = math.sqrt(2.0 * math.pi)
# 协方差矩阵的对角抖动：观测重复点或噪声为 0 时保证可解
_JITTER = 1e-10


@dataclass(frozen=True, slots=True)
class Dimension:
    """一个可调参数的取值范围。"""

    name: str
    low: float
    high: float

    def __post_init__(self) -> None:
        if not self.high > self.low:
            raise ValueError(f"维度 {self.name} 的上限必须大于下限：{self.low}..{self.high}")

    @property
    def span(self) -> float:
        return self.high - self.low

    def clip(self, value: float) -> float:
        return min(max(float(value), self.low), self.high)

    def normalize(self, value: float) -> float:
        return (self.clip(value) - self.low) / self.span

    def denormalize(self, unit_value: float) -> float:
        return self.low + min(max(float(unit_value), 0.0), 1.0) * self.span


@dataclass(frozen=True, slots=True)
class Proposal:
    """一次候选建议，含预测均值/标准差与期望改进，供监控界面展示。"""

    values: tuple[float, ...]
    predicted: float
    std: float
    expected_improvement: float
    iteration: int


class GpEiOptimizer:
    """高斯过程 + 期望改进的候选生成器（最大化目标）。

    内部把参数归一化到 ``[0, 1]^d`` 再算核，避免不同量纲（sccm / V / A）
    混在一起时长度尺度失去意义。
    """

    ALGORITHM = "gp-ei"
    VERSION = "1.0"

    def __init__(
        self,
        dimensions: list[Dimension] | tuple[Dimension, ...],
        *,
        noise: float = 1e-6,
        length_scale: float = 0.3,
        xi: float = 0.0,
        seed: int = 0,
        candidate_samples: int = 512,
        local_rounds: int = 4,
        local_shrink: float = 0.25,
    ) -> None:
        if not dimensions:
            raise ValueError("至少需要一个可调维度")
        self.dimensions = tuple(dimensions)
        self.noise = max(float(noise), 0.0)
        self.length_scale = max(float(length_scale), 1e-6)
        self.xi = float(xi)
        self.seed = int(seed)
        self.candidate_samples = max(int(candidate_samples), 1)
        self.local_rounds = max(int(local_rounds), 0)
        self.local_shrink = float(local_shrink)

        self._rng = np.random.default_rng(self.seed)
        self._x: list[list[float]] = []      # 归一化后的历史参数
        self._y: list[float] = []            # 对应的目标值
        self._cache: tuple[np.ndarray, np.ndarray] | None = None

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------
    def observe(self, values: tuple[float, ...] | list[float], objective: float) -> None:
        """记录一次真实观测量（目标值按**最大化**解释）。"""
        if len(values) != len(self.dimensions):
            raise ValueError(
                f"参数个数与维度不符：期望 {len(self.dimensions)}，收到 {len(values)}"
            )
        if not math.isfinite(float(objective)):
            raise ValueError("目标值不是有限数，不能作为观测记录")
        self._x.append([float(value) for value in values])
        self._y.append(float(objective))
        self._cache = None

    @property
    def n_observed(self) -> int:
        return len(self._y)

    @property
    def observations(self) -> tuple[tuple[tuple[float, ...], float], ...]:
        return tuple(
            (tuple(values), objective)
            for values, objective in zip(self._x, self._y, strict=True)
        )

    @property
    def best(self) -> tuple[tuple[float, ...], float] | None:
        """历史最优（参数, 目标值）。注意：历史最优不代表**现在**仍然安全。"""
        if not self._y:
            return None
        index = max(range(len(self._y)), key=self._y.__getitem__)
        return tuple(self._x[index]), self._y[index]

    def state(self) -> dict[str, Any]:
        """可持久化的状态：算法版本、种子、维度与全部观测。

        文档要求每轮保存算法版本与随机种子，否则结果无法复现。
        """
        return {
            "algorithm": self.ALGORITHM,
            "version": self.VERSION,
            "seed": self.seed,
            "noise": self.noise,
            "length_scale": self.length_scale,
            "xi": self.xi,
            "dimensions": [
                {"name": d.name, "low": d.low, "high": d.high} for d in self.dimensions
            ],
            "observations": [
                {"values": list(values), "objective": objective}
                for values, objective in self.observations
            ],
        }

    # ------------------------------------------------------------------
    # 建议
    # ------------------------------------------------------------------
    def propose(self) -> Proposal:
        """给出下一个候选参数（已夹在范围内）。"""
        if not self._y:
            # 没有观测时给范围中心：比随机点更容易被现场接受，也便于复现
            center = tuple(d.denormalize(0.5) for d in self.dimensions)
            return Proposal(
                values=center,
                predicted=0.0,
                std=1.0,
                expected_improvement=float("inf"),
                iteration=0,
            )

        mean_fn, std_fn = self._posterior()
        best_observed = max(self._y)

        candidates = self._random_candidates()
        best_unit = None
        best_ei = -math.inf
        for unit in candidates:
            ei = self._expected_improvement(unit, mean_fn, std_fn, best_observed)
            if ei > best_ei:
                best_ei, best_unit = ei, unit

        # 局部细化：随机采样在维度稍高时很粗，围绕当前最优候选做几轮收缩扰动
        if best_unit is not None:
            radius = self.local_shrink
            for _ in range(self.local_rounds):
                neighbours = np.clip(
                    best_unit
                    + self._rng.uniform(-radius, radius, size=len(self.dimensions)),
                    0.0, 1.0,
                )
                for unit in [neighbours, *self._perturb_around(best_unit, radius)]:
                    ei = self._expected_improvement(unit, mean_fn, std_fn, best_observed)
                    if ei > best_ei:
                        best_ei, best_unit = ei, unit
                radius *= self.local_shrink

        assert best_unit is not None  # candidate_samples >= 1 保证非空
        mean, std = mean_fn(best_unit), std_fn(best_unit)
        return Proposal(
            values=tuple(
                d.denormalize(u) for d, u in zip(self.dimensions, best_unit, strict=True)
            ),
            predicted=float(mean),
            std=float(std),
            expected_improvement=float(best_ei),
            iteration=len(self._y),
        )

    def _random_candidates(self) -> np.ndarray:
        return self._rng.uniform(0.0, 1.0, size=(self.candidate_samples, len(self.dimensions)))

    def _perturb_around(self, base: np.ndarray, radius: float) -> list[np.ndarray]:
        """沿各坐标轴各推一步：维度高时比纯随机更容易命中脊线。"""
        out: list[np.ndarray] = []
        for axis in range(len(self.dimensions)):
            for sign in (-1.0, 1.0):
                point = base.copy()
                point[axis] = min(max(point[axis] + sign * radius, 0.0), 1.0)
                out.append(point)
        return out

    # ------------------------------------------------------------------
    # GP 回归
    # ------------------------------------------------------------------
    def _kernel(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        """RBF 核，输入是归一化坐标。"""
        scaled = (a[:, None, :] - b[None, :, :]) / self.length_scale
        return np.exp(-0.5 * np.sum(scaled**2, axis=-1))

    def _cache_state(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """缓存 ``(归一化观测, K^-1 y, K^-1)``。

        三个都要缓存：后验方差是 ``1 - kᵀ K⁻¹ k``，若每次评估候选点都重新求逆，
        512 个候选 × O(n³) 会让一次 propose 慢到不可用。
        """
        if self._cache is None:
            unit = np.asarray(
                [
                    [d.normalize(v) for d, v in zip(self.dimensions, row, strict=True)]
                    for row in self._x
                ],
                dtype=float,
            )
            y = np.asarray(self._y, dtype=float)
            k = self._kernel(unit, unit) + np.eye(len(y)) * (self.noise + _JITTER)
            k_inv = np.linalg.inv(k)
            self._cache = (unit, k_inv @ y, k_inv)
        return self._cache

    def _posterior(self):
        unit, alpha, k_inv = self._cache_state()

        def mean_fn(point: np.ndarray) -> float:
            k = self._kernel(point.reshape(1, -1), unit)[0]
            return float(k @ alpha)

        def var_fn(point: np.ndarray) -> float:
            k = self._kernel(point.reshape(1, -1), unit)[0]
            # k(x,x) = 1（RBF），减去已被观测解释的方差
            return float(max(1.0 - k @ (k_inv @ k), 0.0))

        def std_fn(point: np.ndarray) -> float:
            return math.sqrt(var_fn(point))

        return mean_fn, std_fn

    def _expected_improvement(
        self, point: np.ndarray, mean_fn, std_fn, best_observed: float
    ) -> float:
        """最大化场景下的 EI。"""
        mean = mean_fn(point)
        improvement = mean - best_observed - self.xi
        sigma = std_fn(point)
        if sigma <= 1e-12:
            return max(0.0, improvement)
        z = improvement / sigma
        cdf = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        pdf = math.exp(-0.5 * z * z) / _SQRT_2PI
        return improvement * cdf + sigma * pdf
