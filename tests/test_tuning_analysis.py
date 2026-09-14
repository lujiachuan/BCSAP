"""调束辅助分析：单变量响应曲线、过程建议、日志查看与导出（改造报告 §8.9 P2.4）。

分两层：
* **纯函数层**（``apps/desktop_client/tuning_analysis.py``）：曲线取哪个值、建议在什么
  条件下才给、日志一行里有哪些列——这些规则要能在没有界面、没有设备的情况下逐条验；
* **页面层**：曲线选择器、建议区、日志区、导出按钮，以及新增的噪声/种子是否真的
  随任务下发并存进了调束配置。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from apps.desktop_client import instrument_api, tuning_analysis, tuning_config
from apps.desktop_client.pages import tuning as tuning_module
from apps.desktop_client.pages.tuning import TuningPage
from tests.test_tuning_config import CATALOG, MAPPING, _FakeThread, beam, entry

MAGNET = "magnet.m1.current_setpoint"
FOCUS = "focus.v1.voltage_setpoint"


def iteration(
    index: int,
    values: dict[str, float] | None = None,
    objective: float | None = 8.0,
    *,
    stage: str | None = "joint",
    quality: str = "ok",
    detail: str | None = None,
) -> dict:
    values = values if values is not None else {MAGNET: 150.0}
    return {
        "iteration": index,
        "proposed": dict(values),
        "applied": dict(values),
        "readback": dict(values),
        "target": objective,
        "objective": objective,
        "quality": quality,
        "detail": detail,
        "at": "2026-09-14T00:00:00+00:00",
        "stage": stage,
    }


class ResponseCurveTests(unittest.TestCase):
    def test_x_uses_the_actual_readback_not_the_proposal(self) -> None:
        """横坐标是设备当时在哪，不是优化器建议了什么。"""
        records = [
            {
                "iteration": 0,
                "proposed": {MAGNET: 150.0},
                "applied": {MAGNET: 152.0},
                "readback": {MAGNET: 149.7},
                "objective": 8.0,
            },
            {
                "iteration": 1,
                "proposed": {MAGNET: 160.0},
                "applied": {MAGNET: 160.0},
                "readback": {MAGNET: 159.5},
                "objective": 9.0,
            },
        ]

        xs, ys = tuning_analysis.response_curve(records, MAGNET)

        self.assertEqual(xs, [149.7, 159.5])
        self.assertEqual(ys, [8.0, 9.0])

    def test_points_are_sorted_by_parameter_value(self) -> None:
        records = [
            iteration(0, {MAGNET: 200.0}, 7.0),
            iteration(1, {MAGNET: 100.0}, 9.0),
            iteration(2, {MAGNET: 150.0}, 8.0),
        ]

        xs, _ys = tuning_analysis.response_curve(records, MAGNET)

        self.assertEqual(xs, [100.0, 150.0, 200.0])

    def test_rounds_without_a_measurement_are_skipped(self) -> None:
        """写入被拒/读不到目标的轮次不能当 0 画进去，否则曲线凭空多出谷底。"""
        records = [
            iteration(0, {MAGNET: 150.0}, 8.0),
            iteration(1, {MAGNET: 151.0}, None, quality="write_rejected"),
            iteration(2, {MAGNET: 152.0}, 9.0),
        ]

        xs, ys = tuning_analysis.response_curve(records, MAGNET)

        self.assertEqual(len(xs), 2)
        self.assertEqual(ys, [8.0, 9.0])

    def test_falls_back_to_applied_then_proposed(self) -> None:
        """只记录到下发值（回读缺失）时也要能画，但要按 readback→applied→proposed 的顺序。"""
        records = [
            {"iteration": 0, "proposed": {MAGNET: 1.0}, "applied": {}, "readback": {},
             "objective": 5.0},
            {"iteration": 1, "proposed": {MAGNET: 2.0}, "objective": 6.0},
        ]

        xs, _ys = tuning_analysis.response_curve(records, MAGNET)

        self.assertEqual(xs, [1.0, 2.0])

    def test_unknown_signal_yields_nothing(self) -> None:
        xs, ys = tuning_analysis.response_curve([iteration(0)], "magnet.m9.x")

        self.assertEqual((xs, ys), ([], []))

    def test_variables_are_listed_in_first_seen_order(self) -> None:
        records = [
            iteration(0, {MAGNET: 1.0, FOCUS: 2.0}),
            iteration(1, {FOCUS: 3.0}),
        ]

        self.assertEqual(tuning_analysis.curve_variables(records), [MAGNET, FOCUS])

    def test_best_point_is_the_highest_objective(self) -> None:
        best = tuning_analysis.best_point([100.0, 150.0, 200.0], [7.0, 9.0, 8.0])

        self.assertEqual(best, (150.0, 9.0))

    def test_best_point_breaks_ties_towards_the_smaller_value(self) -> None:
        self.assertEqual(
            tuning_analysis.best_point([200.0, 100.0], [9.0, 9.0]), (100.0, 9.0)
        )

    def test_best_point_of_nothing_is_none(self) -> None:
        self.assertIsNone(tuning_analysis.best_point([], []))


class AdviceTests(unittest.TestCase):
    """建议只在有依据时出现，且每条规则最多一条。"""

    def test_no_advice_without_enough_rounds(self) -> None:
        self.assertEqual(tuning_analysis.advice([], {MAGNET: (0.0, 100.0)}), [])
        self.assertEqual(
            tuning_analysis.advice([iteration(i) for i in range(4)], {}), []
        )

    def test_edge_hugging_points_out_which_variable_and_side(self) -> None:
        records = [
            iteration(0, {MAGNET: 50.0}, 5.0),
            iteration(1, {MAGNET: 50.0}, 5.0),
            iteration(2, {MAGNET: 99.5}, 6.0),
            iteration(3, {MAGNET: 99.6}, 6.5),
            iteration(4, {MAGNET: 99.8}, 7.0),
        ]

        lines = tuning_analysis.advice(records, {MAGNET: (0.0, 100.0)})

        self.assertEqual(len(lines), 1)
        self.assertIn(MAGNET, lines[0])
        self.assertIn("上边界", lines[0])
        self.assertIn("放宽", lines[0])

    def test_a_single_round_near_the_edge_is_not_enough(self) -> None:
        records = [iteration(i, {MAGNET: 50.0}, 5.0) for i in range(4)]
        records.append(iteration(4, {MAGNET: 99.9}, 6.0))

        self.assertEqual(tuning_analysis.advice(records, {MAGNET: (0.0, 100.0)}), [])

    def test_stagnation_is_reported_after_ten_rounds(self) -> None:
        records = [iteration(i, {MAGNET: 50.0}, 8.0) for i in range(10)]

        lines = tuning_analysis.advice(records, {})

        self.assertTrue(any("提升不足" in line for line in lines), lines)

    def test_progress_is_not_reported_as_stagnation(self) -> None:
        records = [
            iteration(i, {MAGNET: 50.0}, 5.0 + 0.5 * i) for i in range(10)
        ]

        self.assertEqual(tuning_analysis.advice(records, {}), [])

    def test_jitter_is_reported_when_one_jump_dwarfs_the_rest(self) -> None:
        objectives = [8.0, 8.1, 8.0, 8.1, 8.0, 8.1, 8.0, 8.1, 12.0]
        records = [
            iteration(i, {MAGNET: 50.0}, value)
            for i, value in enumerate(objectives)
        ]

        lines = tuning_analysis.advice(records, {})

        self.assertTrue(any("抖动" in line for line in lines), lines)

    def test_objective_that_never_lifts_is_reported(self) -> None:
        records = [iteration(i, {MAGNET: 50.0}, 0.0) for i in range(6)]

        lines = tuning_analysis.advice(records, {})

        self.assertTrue(any("始终 ≤ 0" in line for line in lines), lines)

    def test_startup_advice_only_when_there_are_many_variables(self) -> None:
        self.assertEqual(tuning_analysis.startup_advice(["a", "b", "c"]), [])
        lines = tuning_analysis.startup_advice([f"v{i}" for i in range(6)])
        self.assertEqual(len(lines), 1)
        self.assertIn("6 个", lines[0])


class LogTests(unittest.TestCase):
    def test_one_line_per_round_with_all_four_columns(self) -> None:
        lines = tuning_analysis.log_lines(
            [iteration(0, {MAGNET: 150.0}, 8.25)], target_signal="detector.fc1.beam_current"
        )

        self.assertEqual(len(lines), 1)
        self.assertIn("第 1 轮", lines[0])
        self.assertIn("建议", lines[0])
        self.assertIn("下发", lines[0])
        self.assertIn("回读", lines[0])
        self.assertIn("detector.fc1.beam_current 8.2500", lines[0])

    def test_stage_is_written_per_round(self) -> None:
        lines = tuning_analysis.log_lines(
            [iteration(0, stage="sequential"), iteration(1, stage="joint")]
        )

        self.assertIn("逐参数优化", lines[0])
        self.assertIn("联合微调", lines[1])

    def test_old_records_say_the_stage_was_not_recorded(self) -> None:
        """老库里的历史轮次没有阶段列：如实写"未记录"，不猜成联合微调。"""
        record = iteration(0, stage=None)

        self.assertEqual(tuning_analysis.stage_of(record), "未记录阶段")
        self.assertIn("未记录阶段", tuning_analysis.log_lines([record])[0])

    def test_quality_problem_is_not_hidden(self) -> None:
        record = iteration(0, quality="write_rejected", detail="磁铁1 写入被拒：越界")

        line = tuning_analysis.log_lines([record])[0]

        self.assertIn("write_rejected", line)
        self.assertIn("越界", line)

    def test_advice_lines_are_appended(self) -> None:
        lines = tuning_analysis.log_lines([iteration(0)], advice_lines=["注意边界"])

        self.assertIn("[建议] 注意边界", lines)

    def test_summary_counts_rounds_and_finds_the_best(self) -> None:
        records = [
            iteration(0, objective=5.0),
            iteration(1, objective=None, quality="read_failed"),
            iteration(2, objective=9.0),
        ]

        lines = tuning_analysis.summary_lines(records, target_signal="FC1")

        text = "\n".join(lines)
        self.assertIn("共 3 轮", text)
        self.assertIn("2 轮有目标测量", text)
        self.assertIn("第 3 轮 9.0000", text)
        self.assertIn("FC1", text)


class TuningAnalysisPageTests(unittest.TestCase):
    """页面层：曲线选择器、建议区、日志区、导出，以及噪声/种子。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.config_path = Path(self._directory.name) / "tuning_config.json"
        self._env = mock.patch.dict(
            os.environ, {tuning_config.CONFIG_ENV_VAR: str(self.config_path)}
        )
        self._env.start()
        self.addCleanup(self._env.stop)

        self.requests: list[dict] = []
        self._stub(instrument_api, "request_read", lambda *a, **k: _FakeThread())
        self._stub(instrument_api, "request_tuning_catalog", lambda *a, **k: _FakeThread())
        self._stub(
            instrument_api,
            "request_tuning_start",
            lambda request, base_url=None: self.requests.append(request) or _FakeThread(),
        )
        self._stub(instrument_api, "request_tuning_iterations", lambda *a, **k: _FakeThread())
        self._stub_module(
            "PvMappingRequestThread", lambda url, payload=None: _FakeThread()
        )

    def _stub(self, target, name: str, replacement) -> None:
        original = getattr(target, name)
        setattr(target, name, replacement)
        self.addCleanup(setattr, target, name, original)

    def _stub_module(self, name: str, replacement) -> None:
        original = getattr(tuning_module, name)
        setattr(tuning_module, name, replacement)
        self.addCleanup(setattr, tuning_module, name, original)

    def make_page(self, mapping: list[dict] | None = None) -> TuningPage:
        page = TuningPage()
        self.addCleanup(page.deleteLater)
        page._on_mapping({"ok": True, "config": {"entries": list(mapping or MAPPING)}})
        page._on_catalog({"ok": True, "payload": CATALOG})
        return page

    def feed(self, page: TuningPage, records: list[dict]) -> None:
        page._on_iterations({"ok": True, "payload": {"iterations": records}})

    # ---------------- 曲线选择器 ----------------
    def test_plot_choices_cover_convergence_and_every_variable(self) -> None:
        page = self.make_page()

        self.feed(page, [iteration(0, {MAGNET: 150.0, FOCUS: 20.0}, 8.0)])

        data = [
            page.plot_choice.itemData(index) for index in range(page.plot_choice.count())
        ]
        self.assertEqual(data, ["", MAGNET, FOCUS])
        self.assertEqual(page.plot_choice.currentData(), "")

    def test_switching_a_variable_draws_its_response_curve(self) -> None:
        page = self.make_page()
        self.feed(
            page,
            [
                iteration(0, {MAGNET: 200.0}, 7.0),
                iteration(1, {MAGNET: 100.0}, 9.0),
            ],
        )

        page.plot_choice.setCurrentIndex(page.plot_choice.findData(MAGNET))

        xs, ys = page.tuning_plot.raw_data()
        self.assertEqual(list(xs), [100.0, 200.0])
        self.assertEqual(list(ys), [9.0, 7.0])
        self.assertIn("响应曲线：已采样 2 点", page.response_note.text())
        self.assertIn("最优目标 9.000", page.response_note.text())
        # 范围来自界面上的设定值（MAPPING 里是 0..100）
        self.assertIn("[0, 100]", page.response_note.text())

    def test_convergence_mode_still_plots_rounds(self) -> None:
        page = self.make_page()
        self.feed(page, [iteration(0, objective=7.0), iteration(1, objective=9.0)])
        page.plot_choice.setCurrentIndex(page.plot_choice.findData(MAGNET))

        page.plot_choice.setCurrentIndex(0)

        xs, _ys = page.tuning_plot.raw_data()
        self.assertEqual(list(xs), [1.0, 2.0])
        self.assertEqual(page.response_note.text(), "")

    def test_selection_survives_a_refresh(self) -> None:
        """轮询会反复重建下拉：选中的曲线不能在轮询里被重置回收敛。"""
        page = self.make_page()
        self.feed(page, [iteration(0, {MAGNET: 150.0}, 8.0)])
        page.plot_choice.setCurrentIndex(page.plot_choice.findData(MAGNET))

        self.feed(page, [iteration(0, {MAGNET: 150.0}, 8.0), iteration(1, {MAGNET: 160.0}, 9.0)])

        self.assertEqual(page.plot_choice.currentData(), MAGNET)

    def test_a_new_variable_appears_in_the_list(self) -> None:
        page = self.make_page()
        self.feed(page, [iteration(0, {MAGNET: 150.0}, 8.0)])

        self.feed(
            page,
            [iteration(0, {MAGNET: 150.0, FOCUS: 20.0}, 8.0)],
        )

        data = [
            page.plot_choice.itemData(index) for index in range(page.plot_choice.count())
        ]
        self.assertIn(FOCUS, data)

    # ---------------- 建议区 ----------------
    def test_advice_area_stays_hidden_until_there_is_advice(self) -> None:
        page = self.make_page()

        self.feed(page, [iteration(0, objective=8.0)])

        # 页面本身没 show()，所以查 isHidden() 而不是 isVisible()
        self.assertTrue(page.advice_label.isHidden())
        self.assertEqual(page.advice_label.text(), "")

    def test_advice_area_shows_what_was_found(self) -> None:
        page = self.make_page()

        self.feed(page, [iteration(i, {MAGNET: 50.0}, 0.0) for i in range(6)])

        self.assertFalse(page.advice_label.isHidden())
        self.assertIn("始终 ≤ 0", page.advice_label.text())

    def test_startup_advice_appears_when_many_variables_are_selected(self) -> None:
        mapping = [
            entry(f"magnet.m{n}.current_setpoint", "磁铁电源") for n in range(1, 7)
        ] + [beam()]
        page = self.make_page(mapping)
        for row in page._rows:
            row["check"].setCheckState(tuning_module.Qt.CheckState.Checked)

        page.start_tuning()

        self.assertFalse(page.advice_label.isHidden())
        self.assertIn("6 个", page.advice_label.text())

    # ---------------- 日志区 ----------------
    def test_log_view_lists_one_line_per_round(self) -> None:
        page = self.make_page()

        self.feed(page, [iteration(0, objective=7.0), iteration(1, objective=9.0)])

        text = page.log_view.toPlainText()
        self.assertIn("第 1 轮", text)
        self.assertIn("第 2 轮", text)
        self.assertIn("共 2 轮", text)
        self.assertTrue(page.log_view.isReadOnly())

    def test_log_view_is_reset_when_a_new_run_starts(self) -> None:
        page = self.make_page()
        self.feed(page, [iteration(0, objective=7.0)])

        row = next(r for r in page._rows if r["entry"]["signal"] == MAGNET)
        row["check"].setCheckState(tuning_module.Qt.CheckState.Checked)
        page.start_tuning()

        self.assertIn("还没有轮次记录", page.log_view.toPlainText())

    def test_export_writes_one_json_object_per_line(self) -> None:
        page = self.make_page()
        page._run_id = "run-1"
        self._stub_module(
            "QFileDialog",
            type("D", (), {"getSaveFileName": staticmethod(
                lambda *a, **k: (str(self.config_path.parent / "log.jsonl"), "")
            )}),
        )
        self.feed(
            page,
            [iteration(0, objective=7.0), iteration(1, objective=9.0, quality="unsettled")],
        )

        page.export_tuning_log()

        lines = [
            json.loads(line)
            for line in (self.config_path.parent / "log.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(lines[0]["event"], "meta")
        self.assertEqual(lines[0]["run_id"], "run-1")
        self.assertEqual(lines[0]["iterations"], 2)
        self.assertEqual(lines[-1]["event"], "end")
        self.assertEqual(lines[-1]["best_iteration"], 1)
        self.assertEqual(lines[1]["iteration"], 0)
        self.assertIn("已导出", page.log_note.text())

    def test_export_without_rounds_only_reports(self) -> None:
        page = self.make_page()

        page.export_tuning_log()

        self.assertIn("还没有轮次记录", page.log_note.text())

    def test_cancelled_export_writes_nothing(self) -> None:
        page = self.make_page()
        self._stub_module(
            "QFileDialog",
            type("D", (), {"getSaveFileName": staticmethod(lambda *a, **k: ("", ""))}),
        )
        self.feed(page, [iteration(0, objective=7.0)])

        page.export_tuning_log()

        self.assertIn("已取消", page.log_note.text())

    # ---------------- 高级参数 ----------------
    def test_noise_and_seed_are_sent_with_the_run(self) -> None:
        page = self.make_page()
        row = next(r for r in page._rows if r["entry"]["signal"] == MAGNET)
        row["check"].setCheckState(tuning_module.Qt.CheckState.Checked)
        page.noise_spin.setValue(0.5)
        page.seed_spin.setValue(7)

        page.start_tuning()

        self.assertEqual(self.requests[0]["noise"], 0.5)
        self.assertEqual(self.requests[0]["seed"], 7)

    def test_noise_and_seed_are_saved_and_restored(self) -> None:
        page = self.make_page()
        page.noise_spin.setValue(0.25)
        page.seed_spin.setValue(11)
        page.save_tuning_config()

        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["noise"], 0.25)
        self.assertEqual(saved["seed"], 11)

        page.noise_spin.setValue(1.0)
        page.seed_spin.setValue(0)
        page.load_tuning_config()

        self.assertEqual(page.noise_spin.value(), 0.25)
        self.assertEqual(page.seed_spin.value(), 11)


if __name__ == "__main__":
    unittest.main()
