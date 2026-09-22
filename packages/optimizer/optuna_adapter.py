"""Optuna 适配器：把 Optuna 的 ask/tell 包成与 GpEiOptimizer 相同的接口。

为什么有这一层：调束循环（propose → 执行层校验/写设备/读回 → observe）由
``tuning_service`` 的状态机驱动，优化器只是循环里被调用的一颗棋子。直接换
Optuna 的 ``study.optimize``（黑盒）会把整段循环塞进 objective，状态机就废了；
用 ask/tell 包成 ``propose`` / ``observe``，状态机一行不用改。

TPE / CMA-ES 等采样器不提供 GP 那样的后验预测与期望改进，
所以 ``Proposal.predicted / std / expected_improvement`` 在非 gp-ei 引擎下
是占位（predicted 用当前最优，std/EI 记 0），界面按算法名决定是否展示。
"""

from __future__ import annotations

from typing import Any

import optuna
from optuna.distributions import FloatDistribution

from packages.optimizer.bayes import Dimension, Proposal

ENGINE_TPE = "tpe"
ENGINE_CMAES = "cmaes"
ENGINE_RANDOM = "random"
ENGINE_QMC = "qmc"
ENGINE_GRID = "grid"

# Optuna 侧支持的引擎（不含自研 gp）
OPTUNA_ENGINES: tuple[str, ...] = (
    ENGINE_TPE,
    ENGINE_CMAES,
    ENGINE_RANDOM,
    ENGINE_QMC,
    ENGINE_GRID,
)

# TPE / CMA-ES 在 n_startup_trials 内退化为随机采样（与 auto_scan 的 engines 一致）
_STARTUP = 5


def _build_sampler(engine: str, seed: int, search_space: dict[str, Any],
                   startup: int = _STARTUP):
    if engine == ENGINE_CMAES:
        return optuna.samplers.CmaEsSampler(n_startup_trials=startup, seed=seed)
    if engine == ENGINE_RANDOM:
        return optuna.samplers.RandomSampler(seed=seed)
    if engine == ENGINE_QMC:
        return optuna.samplers.QMCSampler(seed=seed)
    if engine == ENGINE_GRID:
        return optuna.samplers.GridSampler(search_space)
    return optuna.samplers.TPESampler(n_startup_trials=startup, seed=seed)


class OptunaAsker:
    """把 Optuna ask/tell 包成 propose/observe，替掉自研 GP（接口对齐）。"""

    ALGORITHM_PREFIX = "optuna"

    def __init__(
        self,
        dimensions: list[Dimension] | tuple[Dimension, ...],
        *,
        seed: int = 0,
        engine: str = ENGINE_CMAES,
        noise: float = 1e-6,  # 兼容 GpEiOptimizer 签名；TPE 系列不使用
        storage: str | None = None,  # "sqlite:///path.db"；None = 内存 study
        study_name: str | None = None,
        pruner: str | None = None,  # "median" / "none"；ask/tell 单轮评估下仅记录
        n_startup_trials: int = _STARTUP,  # CMA/TPE 建模前的随机探索轮数
    ) -> None:
        if not dimensions:
            raise ValueError("至少需要一个可调维度")
        if engine not in OPTUNA_ENGINES:
            raise ValueError(
                f"未知的 Optuna 引擎 {engine!r}，支持：{'、'.join(OPTUNA_ENGINES)}"
            )
        self.dimensions = tuple(dimensions)
        self.seed = int(seed)
        self.n_startup_trials = int(n_startup_trials)
        self._engine = engine
        self._search_space: dict[str, FloatDistribution] = {
            d.name: FloatDistribution(float(d.low), float(d.high))
            for d in self.dimensions
        }
        # 剪枝器：ask/tell 单轮评估没有 intermediate values，MedianPruner 实际不触发，
        # 但保留接口——未来接多轮评估（如每轮多次采样）时直接生效。
        pruner_obj: optuna.pruners.BasePruner | None = None
        if pruner == "median":
            pruner_obj = optuna.pruners.MedianPruner()
        elif pruner in (None, "", "none"):
            pruner_obj = None
        else:
            raise ValueError(f"未知 pruner {pruner!r}")
        self._study = optuna.create_study(
            direction="maximize",
            sampler=_build_sampler(
                engine, self.seed, self._search_space, self.n_startup_trials),
            pruner=pruner_obj,
            storage=storage,
            study_name=study_name or "tuning",
            load_if_exists=storage is not None,
        )
        self._trial: optuna.Trial | None = None

    # ---- 与 GpEiOptimizer 对齐的只读属性 ----
    @property
    def ALGORITHM(self) -> str:  # noqa: N802
        return f"{self.ALGORITHM_PREFIX}-{self._engine}"

    @property
    def VERSION(self) -> str:  # noqa: N802
        return "1.0"

    @property
    def n_observed(self) -> int:
        return len(self._study.trials)

    @property
    def study(self) -> optuna.Study:
        """暴露 study 给分析层（importance / slice / dashboard）。"""
        return self._study

    # ---- 观测 ----
    def seed_history(self, observations: list[tuple[list[float], float]]) -> None:
        """把历史观测注入 study（阶段切换重建后接续记忆）。

        Optuna 不能凭空 tell，只能通过 ``add_trial(create_trial(...))`` 回填；
        TPE/CMA-ES 会把它们当作已评估点，冷启动轮数因此少浪费几轮。
        """
        for values, objective in observations:
            params = {
                d.name: float(values[index])
                for index, d in enumerate(self.dimensions)
            }
            trial = optuna.trial.create_trial(
                params=params,
                distributions=self._search_space,
                value=float(objective),
            )
            self._study.add_trial(trial)

    def observe(
        self,
        values: list[float],
        objective: float,
        user_attrs: dict[str, Any] | None = None,
    ) -> None:
        if self._trial is None:
            raise RuntimeError("observe 前必须先 propose（没有待回报的 trial）")
        if user_attrs:
            for k, v in user_attrs.items():
                self._trial.set_user_attr(k, v)
        self._study.tell(self._trial, float(objective))
        self._trial = None

    def fail_trial(self) -> None:
        """把当前 ask 到的 trial 标记为 FAIL（读取失败/写入拒绝/联锁）。

        不喂 objective，TPE 不会学这条坏点；但 Dashboard 里能看到失败原因。
        """
        if self._trial is None:
            return
        self._study.tell(self._trial, state=optuna.trial.TrialState.FAIL)
        self._trial = None

    def drop_pending_trial(self) -> None:
        """任务停止时丢弃未决 trial（标 FAIL，不告诉 Dashboard 有僵尸 RUNNING）。"""
        self.fail_trial()

    # ---- 建议 ----
    def propose(self) -> Proposal:
        self._trial = self._study.ask()
        values = tuple(
            float(self._trial.suggest_float(d.name, d.low, d.high))
            for d in self.dimensions
        )
        best = self._study.best_value if self._study.best_trials else 0.0
        return Proposal(
            values=values,
            predicted=best,
            std=0.0,
            expected_improvement=0.0,
            iteration=len(self._study.trials),
        )

    # ---- 持久化（供导出/诊断）----
    def state(self) -> dict[str, Any]:
        """与 GpEiOptimizer.state 形状对齐：算法版本、种子、维度与全部观测。"""
        return {
            "algorithm": self.ALGORITHM,
            "version": self.VERSION,
            "seed": self.seed,
            "noise": 0.0,  # TPE 系列不使用观测噪声
            "length_scale": 0.0,
            "xi": 0.0,
            "dimensions": [
                {"name": d.name, "low": d.low, "high": d.high} for d in self.dimensions
            ],
            "observations": [
                {"values": list(t.params.values()), "objective": t.value}
                for t in self._study.trials
                if t.value is not None
            ],
        }
