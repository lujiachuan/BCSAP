"""调束页过程日志与增量轮询的界面侧行为（改进方案批次 2）。

为什么单独钉这一组：
- 过程日志面板是"现场复盘"的唯一入口，渲染错了（丢事件、混任务、时间戳不对）
  不会让任何业务流程报错；
- 增量轮询（``/progress`` + ``/events?since``）一旦退化，界面会重复刷屏或漏掉轮次；
- 断线时如果界面继续显示旧数据而不提示，操作员会照着过期读数做决定。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from apps.desktop_client import instrument_api, tuning_analysis
from apps.desktop_client.pages.tuning import TuningPage
from tests.test_tuning_layout import MAGNET, catalog, iteration, mapping


def event(
    seq: int,
    message: str,
    *,
    level: str = "info",
    phase: str = "writing",
    iteration_index: int | None = 0,
) -> dict:
    return {
        "run_id": "run-1",
        "seq": seq,
        "at": f"2026-09-22T09:0{seq}:41.204000+00:00",
        "level": level,
        "phase": phase,
        "iteration": iteration_index,
        "message": message,
        "data": {},
    }


class TuningProcessLogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        def _never(*_args, **_kwargs):
            return None

        self._patches = [
            mock.patch.object(instrument_api, "request_read", _never),
            mock.patch.object(instrument_api, "request_tuning_runs", _never),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)

    def make_page(self, run_id: str | None = "run-1") -> TuningPage:
        page = TuningPage()
        page.resize(1116, 699)
        page.show()
        page._mapping = mapping(2)
        page._catalog = catalog()
        page._build_variable_rows()
        page._build_targets()
        page.tabs.setTabEnabled(1, True)
        page.tabs.setTabEnabled(2, True)
        page._run_id = run_id
        return page

    # ---------------------------------------------------------------- 事件渲染
    def test_events_are_rendered_with_timestamp_level_phase_and_round(self) -> None:
        page = self.make_page()

        page._on_events(
            {
                "ok": True,
                "payload": {
                    "events": [
                        event(1, "写入 磁铁1 电流 = 180 A", phase="writing"),
                        event(2, "目标采样 1/2：读不到", level="warn", phase="measuring"),
                    ],
                    "latest_seq": 2,
                },
            }
        )

        text = page.event_log_view.toPlainText()
        lines = text.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^\[\d{2}:\d{2}:\d{2}\.\d{3}\] INFO\s+写入设备\s+第 1 轮")
        self.assertIn("WARN", lines[1])
        self.assertIn("测量目标", lines[1])
        self.assertEqual(page._last_seq, 2)

    def test_events_are_merged_incrementally_without_duplicates(self) -> None:
        page = self.make_page()

        page._on_events({"ok": True, "payload": {"events": [event(1, "A")], "latest_seq": 1}})
        page._on_events(
            {
                "ok": True,
                "payload": {"events": [event(1, "A"), event(2, "B")], "latest_seq": 2},
            }
        )

        self.assertEqual([item["seq"] for item in page._events], [1, 2])
        self.assertEqual(len(page.event_log_view.toPlainText().splitlines()), 2)

    def test_level_filter_hides_information_lines_only_from_the_view(self) -> None:
        page = self.make_page()
        page._on_events(
            {
                "ok": True,
                "payload": {
                    "events": [event(1, "普通信息"), event(2, "异常", level="warn")],
                    "latest_seq": 2,
                },
            }
        )

        page.log_level_combo.setCurrentIndex(page.log_level_combo.findData("warn"))
        self.app.processEvents()

        text = page.event_log_view.toPlainText()
        self.assertIn("异常", text)
        self.assertNotIn("普通信息", text)
        # 过滤只影响显示：导出的仍是全量
        self.assertEqual(len(page._events), 2)

    def test_falls_back_to_round_lines_when_the_service_has_no_events(self) -> None:
        """老执行服务没有事件流：至少要按轮次给出一份带时间戳的过程日志。"""
        page = self.make_page()
        page._on_iterations(
            {"ok": True, "payload": {"iterations": [iteration(0, {MAGNET: 180.0}, 8.5)]}}
        )

        text = page.event_log_view.toPlainText()
        self.assertIn("第 1 轮", text)
        self.assertIn("建议", text)
        self.assertIn("回读", text)

    # ---------------------------------------------------------------- 增量轮询
    def test_progress_merges_new_iterations_without_losing_old_ones(self) -> None:
        page = self.make_page()
        page._on_progress(
            {
                "ok": True,
                "payload": {
                    "status": {"state": "running", "completed_iterations": 1,
                               "max_iterations": 10},
                    "iterations": [iteration(0, {MAGNET: 180.0}, 8.5)],
                    "latest_seq": 3,
                },
            }
        )
        page._on_progress(
            {
                "ok": True,
                "payload": {
                    "status": {"state": "running", "completed_iterations": 2,
                               "max_iterations": 10},
                    "iterations": [iteration(1, {MAGNET: 190.0}, 9.5)],
                    "latest_seq": 7,
                },
            }
        )

        self.assertEqual([item["iteration"] for item in page._iterations], [0, 1])
        self.assertEqual(page.current_card.value_label.text(), "9.500")

    def test_progress_without_new_iterations_is_a_noop_for_the_curve(self) -> None:
        page = self.make_page()
        page._on_progress(
            {
                "ok": True,
                "payload": {
                    "status": {"state": "applying", "completed_iterations": 1,
                               "max_iterations": 10},
                    "iterations": [iteration(0, {MAGNET: 180.0}, 8.5)],
                    "latest_seq": 2,
                },
            }
        )

        page._on_progress(
            {
                "ok": True,
                "payload": {
                    "status": {"state": "applying", "completed_iterations": 1,
                               "max_iterations": 10},
                    "iterations": [],
                    "latest_seq": 4,
                },
            }
        )

        self.assertEqual(len(page._iterations), 1)
        # 事件游标只能由「事件响应」推进：progress 里的 latest_seq 只是"服务端有这么多"，
        # 用它推进游标会把还没拉下来的事件永久跳过。
        self.assertEqual(page._last_seq, 0)

    def test_poll_asks_the_service_for_progress_and_events_incrementally(self) -> None:
        page = self.make_page()
        page._events = [event(5, "旧事件")]
        page._last_seq = 5
        page._iterations = [iteration(2, {MAGNET: 180.0}, 8.5)]
        calls: list[tuple] = []

        class _Thread:
            def __init__(self, *_args, **_kwargs) -> None:
                class _Signal:
                    def connect(self, *_a, **_k) -> None:
                        return None

                self.completed = _Signal()

        def progress(run_id, after_iteration=-1, base_url=None):
            calls.append(("progress", run_id, after_iteration))
            return _Thread()

        def events(run_id, since=0, base_url=None):
            calls.append(("events", run_id, since))
            return _Thread()

        with mock.patch.object(instrument_api, "request_tuning_progress", progress), \
                mock.patch.object(instrument_api, "request_tuning_events", events):
            page._poll()

        self.assertIn(("progress", "run-1", 2), calls)
        self.assertIn(("events", "run-1", 5), calls)

    # ---------------------------------------------------------------- 断线
    def test_link_loss_is_announced_and_cleared(self) -> None:
        page = self.make_page()

        page._on_progress({"ok": False, "message": "连接超时"})
        self.assertTrue(page.link_note.isHidden())
        page._on_progress({"ok": False, "message": "连接超时"})
        self.assertFalse(page.link_note.isHidden())
        self.assertIn("失去连接", page.link_note.text())

        page._on_progress({"ok": True, "payload": {"status": {}, "iterations": []}})
        self.assertTrue(page.link_note.isHidden())

    # ---------------------------------------------------------------- 导出
    def test_export_includes_the_event_trace(self) -> None:
        page = self.make_page()
        page._iterations = [iteration(0, {MAGNET: 180.0}, 8.5)]
        page._events = [event(1, "写入 磁铁1 电流 = 180 A")]
        target = Path(self._directory.name) / "log.jsonl"
        with mock.patch(
            "apps.desktop_client.pages.tuning.QFileDialog.getSaveFileName",
            staticmethod(lambda *a, **k: (str(target), "")),
        ):
            page.export_tuning_log()

        records = [
            json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(records[0]["event"], "meta")
        self.assertEqual(records[0]["events"], 1)
        trace = [item for item in records if item.get("event") == "trace"]
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["seq"], 1)
        self.assertEqual(trace[0]["phase"], "writing")

    def test_result_log_view_prints_timestamps(self) -> None:
        page = self.make_page()
        page._on_iterations(
            {"ok": True, "payload": {"iterations": [iteration(0, {MAGNET: 180.0}, 8.5)]}}
        )

        text = page.log_view.toPlainText()
        self.assertRegex(text, r"\[\d{2}:\d{2}:\d{2}\.\d{3}\] \[第 1 轮")


class LogLineTests(unittest.TestCase):
    """`tuning_analysis.log_lines` 的时间戳与耗时补注。"""

    def test_timestamp_prefix_and_duration_are_added_when_available(self) -> None:
        record = iteration(3, {"m1": 180.0}, 8.5)
        record["at"] = "2026-09-22T09:03:41.204000+00:00"
        record["duration_ms"] = 3740
        record["sample_spread"] = 0.08
        record["sample_count"] = 3

        lines = tuning_analysis.log_lines([record], target_signal="FC1", timestamps=True)

        self.assertRegex(lines[0], r"^\[\d{2}:\d{2}:\d{2}\.\d{3}\] \[第 4 轮")
        self.assertIn("耗时 3.74s", lines[0])
        self.assertIn("σ 0.080", lines[0])
        self.assertIn("3 次采样", lines[0])

    def test_missing_timestamp_is_marked_not_invented(self) -> None:
        record = iteration(0, {"m1": 180.0}, 8.5)
        record["at"] = ""

        lines = tuning_analysis.log_lines([record], timestamps=True)

        self.assertTrue(lines[0].startswith("[--:--:--.---]"))


if __name__ == "__main__":
    unittest.main()
