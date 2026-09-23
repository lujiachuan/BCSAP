"""调束页三页签的布局守门测试（批次 1：界面紧凑化）。

守门的意义：这些断言对应的是**用户直接感受到的缺陷**——参数配置页要滚轮才能看到
开始按钮、结果页要滚近 1000px、过程页曲线被挤成一条缝。它们不会让功能失效，
所以不会被普通功能测试发现，只有把"1320×820 下必须零滚动"钉成断言才守得住。

尺寸取客户端默认窗口（1320×820）下的**页签可用区**：1226×699。为了让页签区正好是这个
尺寸，页面本身要建成 1266×799（页面标题 + 页签栏 + 页边距占掉 40×100）。直接把页面设成
1116×699 会让页签区只有 578px 高、比真实窗口矮 121px，于是"配置页零滚动"这条件在测试里
成立、在现场却不成立——现场会变成控件被按扁重叠。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QScrollArea,
    QTabWidget,
    QWidget,
)

from apps.desktop_client import instrument_api
from apps.desktop_client.components import CompactForm
from apps.desktop_client.pages.tuning import TuningPage

MAGNET = "magnet.m1.current_setpoint"
FOCUS = "ion_optics.focus.voltage_setpoint"

# 默认窗口 1320×820 下的实测值（tools/ui_snapshot 的探针复核过）
TAB_WIDTH = 1226
TAB_HEIGHT = 699
PAGE_WIDTH = 1266
PAGE_HEIGHT = 799


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
        # 页面 show() 会尝试认领在跑的任务、并（若在跑）轮询进度：单测里全部换成桩，
        # 既不连本机服务，也不让用例结果取决于现场是否有任务在跑。
        def _never(*_args, **_kwargs):
            return None

        self._patches = [
            mock.patch.object(instrument_api, "request_read", _never),
            mock.patch.object(instrument_api, "request_tuning_runs", _never),
            mock.patch.object(instrument_api, "request_tuning_progress", _never),
            mock.patch.object(instrument_api, "request_tuning_events", _never),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_page(self, count: int = 2, records: int = 24) -> TuningPage:
        page = TuningPage()
        page.resize(PAGE_WIDTH, PAGE_HEIGHT)
        page.show()
        page._mapping = mapping(count)
        page._catalog = catalog()
        page._build_variable_rows()
        page._build_targets()
        page._update_variable_hint()
        # 页脚备注与保护卡摘要也要按真实内容算：它们是常驻页脚，文案一长就把
        # 表单区顶矮，而"装不下"在这套布局里表现为滚动条（甚至控件被压扁）。
        page._update_run_plan()
        page._refresh_guard_summary()
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
        # 先核对页签区尺寸：几何常量一旦和真实窗口脱节，"零滚动"这类断言就会失真
        content = page.tabs.currentWidget()
        self.assertEqual((content.width(), content.height()), (TAB_WIDTH, TAB_HEIGHT))
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

    def test_configuration_tab_never_squeezes_controls_into_each_other(self) -> None:
        """比"要不要滚动"更硬的一条：控件不许被压到重叠、也不许低于最小高度。

        高度不够时 Qt **不会**滚动，而是把布局项压到最小高度以下——现场就是这个
        后果：安全卡表单从 229px 被压到 163px，两行输入框重叠 3px，绝对阈值框只剩
        15px 高（点都点不到）。"零滚动"这条断言当时是绿的，因为它只数滚动条。
        """
        page = self.make_page(count=12)
        page.tabs.setCurrentIndex(0)
        self.app.processEvents()
        host = page.tabs.widget(0)

        # 只量"表单整体"是否被压扁：Qt 内部（如 QSpinBox 里的 QLineEdit）本来就可能
        # 比自己的 sizeHint 矮，逐控件比 minimumSizeHint 会全是噪声。
        squeezed = [
            f"{form.height()} < {form.minimumSizeHint().height()}"
            for form in host.findChildren(CompactForm)
            if form.isVisible() and form.minimumSizeHint().height() > form.height() + 1
        ]
        self.assertEqual(squeezed, [], "表单被压到最小高度以下（控件会重叠/点不到）")
        self.assertEqual(self.overlapping_pairs(host), [], "同层控件重叠")

    @staticmethod
    def overlapping_pairs(widget) -> list[str]:
        """同一父控件下可见兄弟控件的重叠对（重叠 > 2px 才算，忽略像素取整）。"""
        out: list[str] = []
        parents = {
            child.parentWidget()
            for child in widget.findChildren(QWidget)
            if child.parentWidget() is not None
        }
        for parent in parents:
            children = [
                child for child in parent.children()
                if isinstance(child, QWidget) and child.isVisible()
                and not child.isWindow() and child.width() > 1 and child.height() > 1
            ]
            for index, first in enumerate(children):
                for second in children[index + 1:]:
                    shared = first.geometry().intersected(second.geometry())
                    if shared.width() > 2 and shared.height() > 2:
                        out.append(
                            f"{type(first).__name__}({first.objectName() or '-'}) ∩ "
                            f"{type(second).__name__}({second.objectName() or '-'}) "
                            f"= {shared.width()}x{shared.height()}"
                        )
        return out

    def test_all_options_visible_still_fits(self) -> None:
        """高级项不再折叠（默认全展开），整页仍不溢出、低频项可见。"""
        page = self.make_page(count=4)

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

    def test_monitor_tab_keeps_log_rounds_and_candidates_in_one_tab_strip(self) -> None:
        """过程日志 / 轮次明细 / 本轮候选在同一容器里用页签分开。

        三者并排或上下堆叠都会把彼此压到只剩几行；收进页签后每块都拿到整列高度。
        """
        page = self.make_page(records=24)
        page._pending = {
            "iteration": 24,
            "predicted": 12.9,
            "std": 0.4,
            "values": {MAGNET: 205.5},
            "active_signals": [MAGNET],
        }
        page._apply_status(
            {"state": "awaiting_confirmation", "mode": "confirm",
             "completed_iterations": 24, "max_iterations": 40, "pending": page._pending}
        )

        tabs = page.monitor_detail_tabs
        self.assertIsInstance(tabs, QTabWidget)
        self.assertEqual(tabs.count(), 3)
        self.assertEqual(tabs.tabText(0), "过程日志")
        self.assertEqual(tabs.tabText(1), "轮次明细 (24)")
        self.assertIn("●", tabs.tabText(2), "有待确认候选时标签必须自带信号")
        # 三块内容都真的在这个页签容器里
        self.assertTrue(tabs.widget(0).isAncestorOf(page.event_log_view))
        self.assertTrue(tabs.widget(1).isAncestorOf(page.recent_table))
        self.assertTrue(tabs.widget(2).isAncestorOf(page.proposal_table))
        # 页签栏不许溢出（溢出 = 后面的页签被滚动箭头藏起来，等于不存在）
        # 先切到这一页让布局真正算尺寸：没显示过的页，页签栏宽度还是默认值
        page.tabs.setCurrentIndex(1)
        self.app.processEvents()
        bar = tabs.tabBar()
        total = sum(bar.tabRect(i).width() for i in range(bar.count()))
        self.assertLessEqual(total, bar.width())

    def test_candidate_tab_badge_clears_when_nothing_is_pending(self) -> None:
        page = self.make_page(records=4)
        page._pending = None
        page._apply_status(
            {"state": "running", "mode": "confirm", "completed_iterations": 4,
             "max_iterations": 40, "pending": None}
        )

        self.assertEqual(page.monitor_detail_tabs.tabText(2), "本轮候选")

    def test_result_tab_tab_strip_fits_four_tabs(self) -> None:
        """结果页右栏 4 个页签同样不许溢出（4×150px 的最小宽度放不进 470px 一栏）。"""
        page = self.make_page(records=24)
        page.tabs.setCurrentIndex(2)
        self.app.processEvents()

        bar = page.result_detail_tabs.tabBar()
        total = sum(bar.tabRect(i).width() for i in range(bar.count()))
        self.assertEqual(bar.count(), 4)
        self.assertLessEqual(total, bar.width())

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
        self.assertEqual(page.result_detail_tabs.count(), 4)
        self.assertEqual(page.result_detail_tabs.tabText(0), "结论摘要")
        self.assertGreaterEqual(page.analysis_plot.height(), 240)

    def test_result_surface_view_renders_with_the_blue_green_palette(self) -> None:
        """结果页走的是 Matplotlib 画布：预测均值必须是同一支蓝绿色图（viridis）。

        页面上有过两套响应面渲染（Matplotlib 画布 / pyqtgraph 兼容路径），色图一旦
        分叉，同一个"预测均值"在两条路径下颜色不一样——看图的人只会看错。
        """
        page = self.make_page(records=24)
        params = [MAGNET, FOCUS]
        page._analysis = {
            "importance": {"values": {MAGNET: 0.7, FOCUS: 0.3}},
            "slice": {},
            "n_complete": 24,
            "history": {"points": []},
            "surface": {
                "available": True,
                "params": params,
                "bounds": [[100.0, 260.0], [1800.0, 2600.0]],
                "pairs": {
                    "0_1": {
                        "mean": [[1.0, 2.0], [3.0, 4.0]],
                        "std": [[0.1, 0.2], [0.3, 0.4]],
                    }
                },
                "points": [[120.0, 1900.0]],
                "best": [206.0, 2274.0],
                "note": "",
            },
        }
        page._refresh_result_analysis_choices()
        page._set_analysis_choice("surface")
        self.app.processEvents()

        names = {
            image.get_cmap().name
            for axis in page.analysis_canvas.figure.axes
            for image in axis.images
        }
        self.assertIn("viridis", names)
        self.assertIn("magma", names)
        self.assertIn("共用同一色标", page.analysis_note.text())

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
        combo = page.analysis_choice_combo
        choices = [combo.itemText(index) for index in range(combo.count())]
        self.assertIn("参数重要性", choices)
        self.assertIn(f"切片 · {page._row_label(MAGNET)}", choices)

        page._set_analysis_choice("importance")
        self.assertIn("重要性", page.analysis_note.text())
        page._set_analysis_choice(f"slice:{MAGNET}")
        self.assertIn("切片", page.analysis_note.text())

    def test_view_selectors_are_dropdowns_not_button_rows(self) -> None:
        """视图选择必须是下拉：按钮排在变量多时会被挤爆、被滚动条压住看不见。"""
        page = self.make_page(records=24)

        self.assertIsInstance(page.plot_choice, QComboBox)
        self.assertFalse(page.plot_choice.isHidden())
        self.assertIsInstance(page.analysis_choice_combo, QComboBox)
        self.assertFalse(page.analysis_choice_combo.isHidden())
        # 下拉宽度只占一个控件，不再需要横向滚动区
        self.assertEqual(page.findChildren(QComboBox) and True, True)
        self.assertFalse(hasattr(page, "analysis_scroll"))

    def test_process_view_dropdown_lists_every_variable(self) -> None:
        page = self.make_page(records=8)

        data = [
            page.plot_choice.itemData(index)
            for index in range(page.plot_choice.count())
        ]

        self.assertEqual(data[0], "")
        self.assertIn(MAGNET, data)
        self.assertIn(FOCUS, data)

    def test_analysis_choice_falls_back_to_data_not_button_text(self) -> None:
        """回归：回退时曾把按钮**文本**写进 analysis_current_choice，导致视图对不上。"""
        page = self.make_page(records=3)

        page._analysis_current_choice = "slice:不存在的参数"
        page.analysis_current_choice = "slice:不存在的参数"
        page._rebuild_analysis_choices([("优化历史", "history")])

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
