"""客户端滚动缓冲：窗口裁剪、缺口不补点、变化量与统计量。"""

from __future__ import annotations

import unittest

from apps.desktop_client.trend_buffer import TrendBuffer


class PushTests(unittest.TestCase):
    def setUp(self) -> None:
        self.buffer = TrendBuffer(keep_s=120.0)
        self.signal = "detector.fc1.beam_current"

    def test_push_appends_and_latest_returns_it(self) -> None:
        self.assertTrue(self.buffer.push(self.signal, 12.5, 1000.0))

        self.assertEqual(self.buffer.latest(self.signal), (1000.0, 12.5))

    def test_none_is_a_gap_not_a_zero(self) -> None:
        """读不到值时绝不能写成 0——那会把"读取中断"画成"电流平稳"。"""
        self.assertFalse(self.buffer.push(self.signal, None, 1000.0))

        self.assertEqual(self.buffer.window(self.signal, 60.0, 1000.0), [])

    def test_non_finite_values_are_rejected(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                self.assertFalse(self.buffer.push(self.signal, value, 1000.0))

    def test_non_numeric_value_is_rejected(self) -> None:
        self.assertFalse(self.buffer.push(self.signal, "abc"))  # type: ignore[arg-type]

    def test_time_must_move_forward(self) -> None:
        """时间戳倒退或同一拍重复到达要丢掉，否则图上会回折。"""
        self.assertTrue(self.buffer.push(self.signal, 1.0, 1000.0))

        self.assertFalse(self.buffer.push(self.signal, 2.0, 1000.0))
        self.assertFalse(self.buffer.push(self.signal, 2.0, 999.0))
        self.assertEqual(self.buffer.signal_points(self.signal), 1)

    def test_default_timestamp_is_now(self) -> None:
        self.assertTrue(self.buffer.push(self.signal, 1.0))

        self.assertIsNotNone(self.buffer.latest(self.signal))


class WindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.buffer = TrendBuffer(keep_s=120.0)
        self.signal = "s"

    def fill(self, count: int, step: float = 1.0, start: float = 0.0) -> None:
        for index in range(count):
            self.buffer.push(self.signal, float(index), start + index * step)

    def test_window_keeps_only_the_recent_span(self) -> None:
        self.fill(100)  # 0..99 秒

        points = self.buffer.window(self.signal, 10.0, 99.0)

        self.assertEqual([t for t, _ in points], [89.0 + i for i in range(11)])

    def test_window_of_an_unknown_signal_is_empty(self) -> None:
        self.assertEqual(self.buffer.window("nope", 60.0), [])

    def test_trim_drops_points_past_keep_s(self) -> None:
        self.fill(100)
        self.buffer.keep_s = 5.0

        self.buffer.trim(99.0)

        self.assertEqual(self.buffer.signal_points(self.signal), 6)

    def test_trim_removes_the_signal_when_it_empties(self) -> None:
        self.fill(3)
        self.buffer.trim(1000.0)

        self.assertEqual(self.buffer.signals(), [])

    def test_max_points_is_enforced(self) -> None:
        buffer = TrendBuffer(keep_s=10_000.0, max_points=5)
        for index in range(20):
            buffer.push(self.signal, float(index), float(index))

        self.assertEqual(buffer.signal_points(self.signal), 5)

    def test_clear_removes_everything_or_one_signal(self) -> None:
        self.buffer.push("a", 1.0, 1.0)
        self.buffer.push("b", 2.0, 2.0)

        self.buffer.clear(["a"])

        self.assertEqual(self.buffer.signals(), ["b"])


class DeltaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.buffer = TrendBuffer(keep_s=600.0)
        self.signal = "s"

    def test_delta_is_against_the_value_at_or_before_the_cutoff(self) -> None:
        for index in range(61):
            self.buffer.push(self.signal, float(index), float(index))

        delta = self.buffer.delta(self.signal, 30.0, 60.0)

        self.assertEqual(delta, 30.0)  # 60 - 30

    def test_delta_needs_history(self) -> None:
        self.buffer.push(self.signal, 5.0, 100.0)

        self.assertIsNone(self.buffer.delta(self.signal, 30.0, 100.0))

    def test_delta_of_an_unknown_signal_is_none(self) -> None:
        self.assertIsNone(self.buffer.delta("nope", 30.0))

    def test_value_at_or_before_picks_the_last_older_point(self) -> None:
        for index in range(5):
            self.buffer.push(self.signal, float(index), float(index))

        self.assertEqual(self.buffer.value_at_or_before(self.signal, 2.7), 2.0)

    def test_value_at_or_before_with_nothing_older_is_none(self) -> None:
        self.buffer.push(self.signal, 1.0, 10.0)

        self.assertIsNone(self.buffer.value_at_or_before(self.signal, 5.0))


class StatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.buffer = TrendBuffer(keep_s=600.0)
        self.signal = "s"

    def test_stats_over_the_window(self) -> None:
        for value in (10.0, 12.0, 14.0, 16.0):
            self.buffer.push(self.signal, value, value)
        # 时间戳 == 值：0..16 秒

        stats = self.buffer.stats(self.signal, 100.0, 16.0)

        self.assertIsNotNone(stats)
        assert stats is not None
        self.assertEqual(stats["count"], 4)
        self.assertEqual(stats["latest"], 16.0)
        self.assertEqual(stats["min"], 10.0)
        self.assertEqual(stats["max"], 16.0)
        self.assertEqual(stats["mean"], 13.0)
        self.assertEqual(stats["range"], 6.0)
        self.assertAlmostEqual(stats["std"], 2.2360679, places=6)
        self.assertEqual(stats["span_s"], 6.0)

    def test_stats_is_none_without_data(self) -> None:
        self.assertIsNone(self.buffer.stats(self.signal, 60.0))

    def test_stats_ignores_points_outside_the_window(self) -> None:
        for index in range(100):
            self.buffer.push(self.signal, float(index), float(index))

        stats = self.buffer.stats(self.signal, 10.0, 99.0)

        assert stats is not None
        self.assertEqual(stats["count"], 11)
        self.assertEqual(stats["min"], 89.0)


if __name__ == "__main__":
    unittest.main()
