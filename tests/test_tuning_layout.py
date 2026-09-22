"""调束页三页签的布局守门测试（批次 1：界面紧凑化）。

守门的意义：这些断言对应的是**用户直接感受到的缺陷**——参数配置页要滚轮才能看到
开始按钮、结果页要滚近 1000px、过程页曲线被挤成一条缝。它们不会让功能失效，
所以不会被普通功能测试发现，只有把"1320×820 下必须零滚动"钉成断言才守得住。

尺寸取客户端默认窗口（1320×820）下的页签可用区：1116×699。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QScrollArea, QTabWidget

from apps.desktop_client import instrument_api
from apps.desktop_client.pages.tuning import TuningPage

MAGNET = "magnet.m1.current_setpoint"
FOCUS = "ion_optics.focus.voltage_setpoint"

# 默认窗口 1320×820 下，调束页页签区的实测可用尺寸
TAB_WIDTH = 1116
TAB_HEIGHT = 699


def _entry(signal: str, label: str, unit: str, group: str, **extra: object) -> dict:
    base = {
        "signal": signal,
        "label": label,
        "pv": "PV:" + signal,
        "unit": unit,
        "writable": True,
        "required": False,
        "group": group,
        "role": "setpoint",
        "readback_signal": signal.replace("setpoint", "readback"),
        "min_value": 0.0,
        "max_value": 600.0,
        "max_step": 20.0,
        "tunable": True,
    }
    base.update(extra)
    return base


def mapping(count: int = 2) -> list[dict]:
    entries = [
        _entry(MAGNET, "磁铁1 电流设定", "A", "磁铁电源"),
        _entry(FOCUS, "聚焦电压设定", "V", "聚焦电源", max_value=4000.0),
    ]
    for index in range(3, count + 1):
        entries.append(
            _entry(
                f"magnet.m{index}.current_setpoint",
                f"磁铁{index} 电流设定",
                "A",
                "磁铁电源",
            )
        )
    entries.append(
        {
            "signal": "detector.fc1.beam_current",
            "label": "FC1 束流电流",
            "pv": "PV:detector.fc1.beam_current",
            "unit": "nA",
            "writable": False,
            "required": True,
            "group": "束流探测",
            "role": "readback",
            "beam_target": True,
        }
    )
    return entries


def catalog() -> dict:
    upstream = [entry["signal"] for entry in mapping(12) if entry.get("tunable")]
    return {
        "targets": [
            {
                "signal": "detector.fc1.beam_current",
                "label": "FC1 束流电流",
                "unit": "nA",
                "group": "束流探测",
                "stage": "detector",
            }
        ],
        "variables": [],
        "stages": [{"key": "detector", "label": "束流探测", "groups": ["束流探测"]}],
        "upstream": {"detector.fc1.beam_current": upstream},
        "linked_sets": [],
        "excluded": {},
    }


def iteration(index: int, values: dict, objective: float) -> dict:
    """形状与 /tuning/runs/{id}/iterations 一致的合成轮次。"""
    return {
        "iteration": index,
        "proposed": dict(values),
        "applied": dict(values),
        "readback": dict(values),
        "target": objective,
        "objective": objective,
        "quality": "ok",
        "detail": None,
        "at": f"2026-09-21T08:{index:02d}:41.204000+00:00",
        "stage": "joint",
    }


class TuningLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._read_patch = mock.patch.object(
            instrument_api, "request_read", lambda *a, **k: None
        )
        self._read_patch.start()
        self.addCleanup(self._read_patch.stop)

    def make_page(self, count: int = 2, records: int = 24) -> TuningPage:
        page = TuningPage()
        page.resize(TAB_WIDTH, TAB_HEIGHT)
        page.show()
        page._mapping = mapping(count)
        page._catalog = catalog()
        page._build_variable_rows()
        page._build_targets()
        page._update_variable_hint()
        page.tabs.setTabEnabled(1, True)
        page.tabs.setTabEnabled(2, True)
        values = {MAGNET: 180.0, FOCUS: 2100.0}
        page._apply_status(
            {
                "state": "completed",
                "run_id": "layout-run",
                "strategy": "joint",
                "stage": "joint",
                "completed_iterations": records,
                "max_iterations": 40,
                "snapshot": {MAGNET: 175.0, FOCUS: 2123.0},
                "baseline_objective": 8.31,
                "algorithm": "gp-ei/1.0",
                "seed": 0,
            }
        )
        page._on_iterations(
            {
                "ok": True,
                "payload": {
                    "iterations": [
                        iteration(index, values, 8.3 + index * 0.2)
                        for index in range(records)
                    ]
                },
            }
        )
        self.app.processEvents()
        return page

    @staticmethod
    def scrolls_needing_scroll(widget) -> list[str]:
        """哪些滚动区在当前尺寸下真的需要竖向滚动（要求是"无"）。"""
        needed: list[str] = []
        for area in widget.findChildren(QScrollArea):
            if area.verticalScrollBar().maximum() > 0:
                needed.append(area.objectName() or "unnamed")
        return needed

    # ---------------------------------------------------------------- 页签 1
    def test_configuration_tab_fits_without_scrolling(self) -> None:
        page = self.make_page(count=2)

        self.assertEqual(self.scrolls_needing_scroll(page.tabs.widget(0)), [])

    def test_configuration_tab_fits_with_twelve_variables(self) -> None:
        """变量多是最坏情形：表格与范围标尺都要给地方，且整页仍不许滚动。"""
        page = self.make_page(count=12)

        self.assertEqual(self.scrolls_needing_scroll(page.tabs.widget(0)), [])
        self.assertEqual(page.parameter_table.rowCount(), 12)
        self.assertGreater(page.parameter_table.height(), 150)

    def test_expanding_advanced_options_still_fits(self) -> None:
        page = self.make_page(count=4)

        page.advanced_button.setChecked(True)
        self.app.processEvents()

        self.assertEqual(self.scrolls_needing_scroll(page.tabs.widget(0)), [])
        self.assertTrue(page.seed_spin.isVisible() or not page.seed_spin.isHidden())

    def test_range_overview_has_one_row_per_variable(self) -> None:
        page = self.make_page(count=4)

        self.assertEqual(page.range_rows.count(), 3 * 4)

    def test_footer_keeps_the_start_button_out_of_the_scroll_area(self) -> None:
        """开始按钮在页签里、不在滚动容器里：展开高级参数也压不掉它。"""
        page = self.make_page(count=6)

        self.assertIs(page.start_button.parentWidget(), page.status_strip)
        self.assertFalse(
            any(
                page.start_button.isAncestorOf(area)
                for area in page.tabs.widget(0).findChildren(QScrollArea)
            )
        )

    # ---------------------------------------------------------------- 页签 2
    def test_monitor_tab_gives_the_chart_the_main_height(self) -> None:
        page = self.make_page(records=24)

        self.assertEqual(self.scrolls_needing_scroll(page.tabs.widget(1)), [])
        self.assertGreaterEqual(page.tuning_plot.height(), 320)

    def test_monitor_tab_shows_a_timestamped_process_log(self) -> None:
        page = self.make_page(records=5)

        lines = page.event_log_view.toPlainText().splitlines()
        self.assertEqual(len(lines), 5)
        # 时间戳按本地时区显示，所以只钉格式（毫秒三位）不看具体时刻
        self.assertRegex(lines[0], r"^\[\d{2}:\d{2}:\d{2}\.\d{3}\] INFO 第 1 轮")
        self.assertIn("建议", lines[0])
        self.assertIn("下发", lines[0])
        self.assertIn("回读", lines[0])
        self.assertIn("目标", lines[0])

    def test_process_log_is_cleared_when_a_new_run_starts(self) -> None:
        page = self.make_page(records=3)
        self.assertTrue(page.event_log_view.toPlainText())
        page._rows[0]["check"].setCheckState(Qt.CheckState.Checked)

        class _Thread:
            def __init__(self) -> None:
                class _Signal:
                    def connect(self, *_args, **_kwargs) -> None:
                        return None

                self.completed = _Signal()

        page.save_tuning_config = lambda announce=True: None  # type: ignore[method-assign]
        with mock.patch.object(
            instrument_api, "request_tuning_start", lambda *a, **k: _Thread()
        ):
            page.start_tuning()

        self.assertEqual(page.event_log_view.toPlainText(), "")

    # ---------------------------------------------------------------- 页签 3
    def test_result_tab_keeps_details_in_tabs_and_fits(self) -> None:
        page = self.make_page(records=24)

        self.assertEqual(self.scrolls_needing_scroll(page.tabs.widget(2)), [])
        self.assertIsInstance(page.result_detail_tabs, QTabWidget)
        self.assertEqual(page.result_detail_tabs.count(), 3)
        self.assertGreaterEqual(page.analysis_plot.height(), 240)

    def test_result_analysis_switches_between_views(self) -> None:
        page = self.make_page(records=24)
        page._analysis = {
            "importance": {"method": "fanova", "values": {MAGNET: 0.7, FOCUS: 0.3}},
            "slice": {MAGNET: [[100.0, 8.0], [200.0, 9.0], [300.0, 8.6]]},
            "n_complete": 24,
            "history": {"points": []},
            "surface": {"available": False, "reason": "样本不足"},
        }

        page._refresh_result_analysis_choices()
        choices = [
            button.text() for button in page._analysis_buttons
        ]
        self.assertIn("参数重要性", choices)
        self.assertIn(f"切片 · {page._row_label(MAGNET)}", choices)

        page._set_analysis_choice("importance")
        self.assertIn("重要性", page.analysis_note.text())
        page._set_analysis_choice(f"slice:{MAGNET}")
        self.assertIn("切片", page.analysis_note.text())

    def test_analysis_choice_falls_back_to_data_not_button_text(self) -> None:
        """回归：回退时曾把按钮**文本**写进 analysis_current_choice，导致视图对不上。"""
        page = self.make_page(records=3)

        page._analysis_current_choice = "slice:不存在的参数"
        page.analysis_current_choice = "slice:不存在的参数"
        page._rebuild_analysis_buttons([("优化历史", "history")])

        self.assertEqual(page.analysis_current_choice, "history")

    def test_terminal_state_keeps_disposal_bar_visible(self) -> None:
        page = self.make_page(records=3)

        self.assertEqual(
            list(page.finalize_buttons),
            ["apply_best", "restore_initial", "start_values", "safe_values"],
        )
        # 处置按钮在页签内的固定区：不落进任何滚动容器（否则任务结束时可能看不见）
        areas = page.tabs.widget(2).findChildren(QScrollArea)
        for button in page.finalize_buttons.values():
            self.assertFalse(
                any(area.isAncestorOf(button) for area in areas),
                f"{button.text()} 被放进了滚动容器",
            )


if __name__ == "__main__":
    unittest.main()
