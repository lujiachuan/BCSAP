# -*- coding: utf-8 -*-
"""
贝叶斯优化器（独立模块）
Bayesian Optimization: Gaussian Process + Expected Improvement

双后端：
  1. scikit-optimize (skopt) —— 优先使用，经过充分验证
  2. 自包含 GP + EI —— skopt 不可用时自动降级，仅依赖 numpy/scipy/sklearn

用法：
    from bayesian_optimizer import BayesianOptimizer

    opt = BayesianOptimizer(
        param_names=["x1", "x2", "x3"],
        bounds=[(-5, 5), (-5, 5), (-5, 5)],
        n_initial=5,      # 初始随机采样点数
        xi=0.01,           # 探索系数，越大越倾向探索
        seed=42,
    )

    for i in range(30):
        x = opt.suggest()          # 建议下一个采样点
        y = your_objective(x)      # 评估目标函数（越大越好）
        opt.observe(x, y)          # 记录观测值

    print("最优值:", opt.best_value)
    print("最优参数:", opt.best_x)
"""

from typing import List, Optional, Tuple

import numpy as np

# ---------- 后端检测 ----------
try:
    from skopt import Optimizer as _SkOptOptimizer
    from skopt.space import Real as _SkReal
    _HAS_SKOPT = True
except Exception:
    _HAS_SKOPT = False

from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import (
    Matern, ConstantKernel, WhiteKernel,
)
from scipy.optimize import minimize
from scipy.stats import norm


class BayesianOptimizer:
    """
    高斯过程回归 + Expected Improvement 采集函数的贝叶斯优化器。

    目标：最大化目标函数值（如探测器电流）。
    内部对 y 取负以适配最小化框架。

    Parameters
    ----------
    param_names : list of str
        待优化参数名称列表。
    bounds : list of (float, float)
        每个参数的搜索区间 [min, max]，与 param_names 一一对应。
    seed : int, default=42
        随机种子，保证可复现。
    xi : float, default=0.01
        EI 采集函数的探索-利用权衡参数。
        xi 越大 → 越倾向探索未知区域；
        xi 越小 → 越倾向利用当前最优附近。
    n_initial : int, default=5
        初始随机采样点数。建议 ≥ 维度数，至少 3。
        点数过少会导致 GP 模型信号不足。
    """

    def __init__(
        self,
        param_names: List[str],
        bounds: List[Tuple[float, float]],
        seed: int = 42,
        xi: float = 0.01,
        n_initial: int = 5,
    ):
        if len(param_names) != len(bounds):
            raise ValueError(
                f"param_names 长度 ({len(param_names)}) 与 bounds 长度 "
                f"({len(bounds)}) 不一致"
            )
        self.names = list(param_names)
        self.bounds = np.array(bounds, dtype=float)
        self.dim = len(param_names)
        self.seed = seed
        self.rng = np.random.RandomState(seed)
        self.xi = xi
        self.n_initial = n_initial

        # 观测历史
        self.X: List[np.ndarray] = []
        self.Y: List[float] = []

        # GP 模型（自包含后端使用）
        self.gp: Optional[GaussianProcessRegressor] = None

        # skopt 后端
        self._use_skopt = _HAS_SKOPT
        self._skopt_opt = None
        if self._use_skopt:
            try:
                space = [
                    _SkReal(b[0], b[1], name=n)
                    for n, b in zip(param_names, bounds)
                ]
                self._skopt_opt = _SkOptOptimizer(
                    dimensions=space,
                    base_estimator="GP",
                    acq_func="EI",
                    acq_optimizer="auto",
                    random_state=seed,
                    n_initial_points=n_initial,
                )
            except Exception:
                self._use_skopt = False
                self._skopt_opt = None

    # ------------------------------------------------------------------
    # 归一化 / 反归一化（自包含后端使用）
    # ------------------------------------------------------------------
    def _normalize(self, x: np.ndarray) -> np.ndarray:
        """将参数从原始空间映射到 [0, 1]^d。"""
        return (x - self.bounds[:, 0]) / (
            self.bounds[:, 1] - self.bounds[:, 0] + 1e-12
        )

    def _denormalize(self, xn: np.ndarray) -> np.ndarray:
        """将 [0, 1]^d 映射回原始参数空间。"""
        return xn * (self.bounds[:, 1] - self.bounds[:, 0]) + self.bounds[:, 0]

    # ------------------------------------------------------------------
    # 核心接口
    # ------------------------------------------------------------------
    def suggest(self) -> np.ndarray:
        """
        建议下一个采样点。

        - 前 n_initial 次：随机采样（探索空间）
        - 之后：拟合 GP，最大化 EI 采集函数

        Returns
        -------
        x : ndarray, shape (d,)
            建议的参数值（原始空间）。
        """
        # ---- skopt 后端 ----
        if self._use_skopt and self._skopt_opt is not None:
            x = self._skopt_opt.ask()
            return np.array(x, dtype=float)

        # ---- 自包含后端 ----
        if len(self.X) < self.n_initial:
            # 初始随机点
            xn = self.rng.rand(self.dim)
            return self._denormalize(xn)

        self._fit_gp()
        return self._optimize_acquisition()

    def observe(self, x: np.ndarray, y: float) -> None:
        """
        记录一次观测值。

        Parameters
        ----------
        x : array_like, shape (d,)
            本次评估的参数值。
        y : float
            本次评估的目标函数值（越大越好）。
        """
        x = np.array(x, dtype=float)
        self.X.append(x)
        self.Y.append(float(y))

        if self._use_skopt and self._skopt_opt is not None:
            try:
                # 注意：skopt 的 tell 需要将单点包裹为列表
                self._skopt_opt.tell([list(x)], [-float(y)])
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 自包含 GP 后端
    # ------------------------------------------------------------------
    def _fit_gp(self) -> None:
        """用已有观测拟合高斯过程回归模型。"""
        Xn = np.array([self._normalize(x) for x in self.X])
        # 目标最大化 → 拟合 -y（最小值对应最大目标值）
        Y = -np.array(self.Y, dtype=float)

        kernel = (
            ConstantKernel(1.0, (1e-3, 1e3))
            * Matern(
                length_scale=np.ones(self.dim),
                length_scale_bounds=(1e-2, 1e2),
                nu=2.5,
            )
            + WhiteKernel(
                noise_level=1e-5,
                noise_level_bounds=(1e-10, 1e-1),
            )
        )
        self.gp = GaussianProcessRegressor(
            kernel=kernel,
            normalize_y=True,
            n_restarts_optimizer=5,
            random_state=self.seed,
        )
        self.gp.fit(Xn, Y)

    def _expected_improvement(self, xn: np.ndarray) -> float:
        """
        Expected Improvement 采集函数。

        针对最小化 -y（即最大化 y）计算 EI。
        EI(x) = E[max(f_best - f(x), 0)]

        Parameters
        ----------
        xn : ndarray, shape (d,)
            归一化空间中的点。

        Returns
        -------
        ei : float
            该点的 EI 值（越大越值得采样）。
        """
        xn = np.atleast_2d(xn)
        mu, sigma = self.gp.predict(xn, return_std=True)
        mu = mu[0]
        sigma = max(sigma[0], 1e-9)

        # 当前最优目标值（最大 y → 最小 -y）
        f_best = -np.max(self.Y)

        # 改进量 = f_best - mu(x) - xi
        improvement = f_best - mu - self.xi
        Z = improvement / sigma
        ei = improvement * norm.cdf(Z) + sigma * norm.pdf(Z)
        return ei

    def _optimize_acquisition(self) -> np.ndarray:
        """
        多起点 L-BFGS-B 优化 EI 采集函数，找到下一个最值得采样的点。

        Returns
        -------
        x : ndarray, shape (d,)
            原始空间中的建议点。
        """
        best_xn = None
        best_ei = -np.inf
        n_starts = 50
        x0_list = self.rng.rand(n_starts, self.dim)

        for x0 in x0_list:
            res = minimize(
                lambda x: -self._expected_improvement(x),
                x0,
                method="L-BFGS-B",
                bounds=[(0.0, 1.0)] * self.dim,
            )
            if -res.fun > best_ei:
                best_ei = -res.fun
                best_xn = res.x

        if best_xn is None:
            best_xn = self.rng.rand(self.dim)

        return self._denormalize(best_xn)

    # ------------------------------------------------------------------
    # 查询属性
    # ------------------------------------------------------------------
    @property
    def best_value(self) -> float:
        """当前观测到的最优目标值。"""
        return max(self.Y) if self.Y else float("nan")

    @property
    def best_x(self) -> Optional[np.ndarray]:
        """当前最优目标值对应的参数。"""
        if not self.Y:
            return None
        return self.X[int(np.argmax(self.Y))]

    @property
    def n_evaluated(self) -> int:
        """已评估的点数。"""
        return len(self.Y)

    @property
    def backend(self) -> str:
        """当前使用的后端名称。"""
        return "skopt" if self._use_skopt else "gp-ei (self-contained)"

    def __repr__(self) -> str:
        return (
            f"BayesianOptimizer(dim={self.dim}, n_evaluated={self.n_evaluated}, "
            f"backend={self.backend}, best={self.best_value:.4g})"
        )


# ======================================================================
# 演示：最大化一个已知峰值的高斯函数
# ======================================================================
if __name__ == "__main__":
    def objective(x):
        """3 维高斯函数，峰值在 (1.0, -2.0, 0.5)，峰值 10.0。"""
        return 10.0 * np.exp(
            -0.5 * ((x[0] - 1.0) ** 2 + (x[1] + 2.0) ** 2 + (x[2] - 0.5) ** 2)
        )

    opt = BayesianOptimizer(
        param_names=["x1", "x2", "x3"],
        bounds=[(-5, 5), (-5, 5), (-5, 5)],
        n_initial=5,
        xi=0.01,
        seed=42,
    )
    print(f"后端: {opt.backend}")
    print(f"{'迭代':>4}  {'x1':>8}  {'x2':>8}  {'x3':>8}  {'目标值':>10}  {'最优':>10}")
    print("-" * 65)

    for i in range(25):
        x = opt.suggest()
        y = objective(x)
        opt.observe(x, y)
        print(
            f"{i+1:>4}  {x[0]:>+8.3f}  {x[1]:>+8.3f}  {x[2]:>+8.3f}"
            f"  {y:>10.4f}  {opt.best_value:>10.4f}"
        )

    print("-" * 65)
    bx = opt.best_x
    print(f"最优参数: x1={bx[0]:+.4f}, x2={bx[1]:+.4f}, x3={bx[2]:+.4f}")
    print(f"最优值:   {opt.best_value:.4f}  (理论峰值 10.0)")
    print(f"理论最优: x1=+1.0000, x2=-2.0000, x3=+0.5000")
