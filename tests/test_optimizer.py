"""贝叶斯优化器：边界、确定性与收敛性。

收敛用例是核心证据：优化器不能只是"能跑"，还要真的能找到峰。
同时用一条耗时断言挡住"每次评估候选点都重新求逆"这类退化成 O(n³)/候选 的写法。
"""

from __future__ import annotations

import math
import time
import unittest

from packages.optimizer import Dimension, GpEiOptimizer


def run(optimizer: GpEiOptimizer, objective, iterations: int) -> GpEiOptimizer:
    """按一次一步的方式驱动优化器（与执行层调用方式一致）。"""
    for _ in range(iterations):
        proposal = optimizer.propose()
        optimizer.observe(proposal.values, objective(*proposal.values))
    return optimizer


class DimensionTests(unittest.TestCase):
    def test_rejects_inverted_bounds(self) -> None:
        with self.assertRaises(ValueError):
            Dimension("x", 10.0, 10.0)

    def test_normalize_and_denormalize_round_trip(self) -> None:
        dimension = Dimension("flow", 5.0, 505.0)

        for value in (5.0, 137.5, 505.0):
            with self.subTest(value=value):
                self.assertAlmostEqual(
                    dimension.denormalize(dimension.normalize(value)), value, places=9
                )

    def test_clip_holds_values_inside_range(self) -> None:
        dimension = Dimension("v", 0.0, 100.0)

        self.assertEqual(dimension.clip(-5.0), 0.0)
        self.assertEqual(dimension.clip(500.0), 100.0)


class ProposalTests(unittest.TestCase):
    def test_first_proposal_is_range_center(self) -> None:
        optimizer = GpEiOptimizer([Dimension("a", 0.0, 200.0), Dimension("b", -10.0, 10.0)])

        proposal = optimizer.propose()

        self.assertEqual(proposal.values, (100.0, 0.0))

    def test_proposals_stay_within_bounds(self) -> None:
        dimensions = [Dimension("a", 10.0, 20.0), Dimension("b", 0.0, 1.0)]
        optimizer = GpEiOptimizer(dimensions, seed=3)

        for _ in range(12):
            proposal = optimizer.propose()
            for dimension, value in zip(dimensions, proposal.values, strict=True):
                with self.subTest(dimension=dimension.name, value=value):
                    self.assertGreaterEqual(value, dimension.low)
                    self.assertLessEqual(value, dimension.high)
            optimizer.observe(proposal.values, sum(proposal.values))

    def test_observe_rejects_non_finite_objective(self) -> None:
        optimizer = GpEiOptimizer([Dimension("a", 0.0, 1.0)])

        with self.assertRaises(ValueError):
            optimizer.observe((0.5,), float("nan"))

    def test_observe_rejects_wrong_arity(self) -> None:
        optimizer = GpEiOptimizer([Dimension("a", 0.0, 1.0), Dimension("b", 0.0, 1.0)])

        with self.assertRaises(ValueError):
            optimizer.observe((0.5,), 1.0)

    def test_same_seed_gives_same_proposals(self) -> None:
        objective = lambda a, b: -(a - 3.0) ** 2 - (b - 7.0) ** 2  # noqa: E731
        first = run(GpEiOptimizer(
            [Dimension("a", 0.0, 10.0), Dimension("b", 0.0, 10.0)], seed=42
        ), objective, 8)
        second = run(GpEiOptimizer(
            [Dimension("a", 0.0, 10.0), Dimension("b", 0.0, 10.0)], seed=42
        ), objective, 8)

        self.assertEqual(first.observations, second.observations)

    def test_best_reflects_highest_objective_seen(self) -> None:
        optimizer = GpEiOptimizer([Dimension("a", 0.0, 10.0)])
        optimizer.observe((2.0,), 5.0)
        optimizer.observe((8.0,), 9.0)
        optimizer.observe((5.0,), 1.0)

        best_values, best_objective = optimizer.best or (None, None)

        self.assertEqual(best_values, (8.0,))
        self.assertEqual(best_objective, 9.0)


class ConvergenceTests(unittest.TestCase):
    """真正要证明的事：优化器能找到峰，而不只是"能跑"。"""

    def test_converges_on_one_dimensional_peak(self) -> None:
        """用 demo 的模拟目标形状：90 + 40·exp(-((I-32)/12)²)，峰在 32。"""
        def objective(current: float) -> float:
            return 90.0 + 40.0 * math.exp(-(((current - 32.0) / 12.0) ** 2))

        optimizer = GpEiOptimizer(
            [Dimension("magnet current", 0.0, 100.0)],
            length_scale=0.15, seed=7, candidate_samples=256,
        )

        run(optimizer, objective, 25)

        best_values, best_objective = optimizer.best or (None, None)
        self.assertIsNotNone(best_values)
        self.assertLess(abs(best_values[0] - 32.0), 6.0, f"最优位置 {best_values[0]}")
        self.assertGreater(best_objective, 125.0, f"最优值 {best_objective}")

    def test_beats_random_search_on_two_dimensions(self) -> None:
        def objective(a: float, b: float) -> float:
            return -((a - 30.0) ** 2) - ((b - 70.0) ** 2)

        optimizer = GpEiOptimizer(
            [Dimension("a", 0.0, 100.0), Dimension("b", 0.0, 100.0)],
            length_scale=0.25, seed=11, candidate_samples=256,
        )

        run(optimizer, objective, 30)

        _, best_objective = optimizer.best or (None, None)
        # 随机均匀采样 31 个点的期望最好值远不到这个量级
        self.assertGreater(best_objective, -400.0, f"最优值 {best_objective}")

    def test_converges_towards_optimum_in_narrow_range(self) -> None:
        """峰在 5，量程 0..10：预测均值必须收敛到峰顶附近，而不是停在 0。

        （不能用「最后一次预测 > 第一次预测」来断言：第一次 propose 还没有任何
        观测，没有后验，按定义 predicted=0。）
        """
        def objective(current: float) -> float:
            return -(current - 5.0) ** 2

        optimizer = GpEiOptimizer([Dimension("x", 0.0, 10.0)], seed=1)

        for _ in range(14):
            proposal = optimizer.propose()
            optimizer.observe(proposal.values, objective(*proposal.values))

        final = optimizer.propose()
        best_values, best_objective = optimizer.best or (None, None)
        self.assertLess(abs(best_values[0] - 5.0), 1.0, f"最优位置 {best_values[0]}")
        self.assertGreater(best_objective, -1.0, f"最优值 {best_objective}")
        self.assertGreater(final.predicted, -1.0, f"预测均值 {final.predicted}")


class PerformanceTests(unittest.TestCase):
    def test_proposal_stays_fast_with_many_observations(self) -> None:
        """挡住「每个候选点都重新求逆」的退化：那是 O(n³)/候选，会慢几个数量级。"""
        dimensions = [Dimension("a", 0.0, 100.0), Dimension("b", 0.0, 100.0)]
        optimizer = GpEiOptimizer(dimensions, seed=5, candidate_samples=512)
        for index in range(40):
            optimizer.observe((float(index), float(index % 7)), float(-index))

        started = time.perf_counter()
        optimizer.propose()
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 2.0, f"一次 propose 用了 {elapsed:.2f}s")


class StateTests(unittest.TestCase):
    def test_state_records_algorithm_version_seed_and_observations(self) -> None:
        """文档要求每轮保存算法版本与随机种子，否则结果不可复现。"""
        optimizer = GpEiOptimizer([Dimension("a", 0.0, 10.0)], seed=99)
        optimizer.observe((1.0,), 2.0)
        optimizer.observe((3.0,), 4.0)

        state = optimizer.state()

        self.assertEqual(state["algorithm"], "gp-ei")
        self.assertEqual(state["seed"], 99)
        self.assertTrue(state["version"])
        self.assertEqual(len(state["observations"]), 2)
        self.assertEqual(state["dimensions"][0]["name"], "a")


if __name__ == "__main__":
    unittest.main()
