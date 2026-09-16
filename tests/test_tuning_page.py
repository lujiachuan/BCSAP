"""调束页：目标/变量白名单与束线拓扑联动的界面侧行为。

覆盖点：只列出设备档案标记过的量；服务端较旧（没有标记字段）时按命名规则回退
**并在界面上说明**，而不是静默变成 0 行；选目标时按拓扑自动勾选上游参数。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from apps.desktop_client import instrument_api
from apps.desktop_client.pages.tuning import TuningPage


class _SignalStub:
    def __init__(self, payload: dict | None = None) -> None:
        self._payload = payload
        self._callbacks: list = []

    def connect(self, callback, *_args, **_kwargs) -> None:
        self._callbacks.append(callback)
        if self._payload is not None:
            # 页面在拿到线程之后才 connect，所以"完成"要在 connect 时补发
            callback(self._payload)


class _FakeThread:
    """替掉真实的请求线程：单测不该连服务，也不该留下 QThread 影响退出。

    ``completed_payload`` 给了就在 connect 时立刻回调（模拟请求已完成）。
    """

    def __init__(self, *_args, completed_payload: dict | None = None, **_kwargs) -> None:
        self.completed = _SignalStub(completed_payload)

    def start(self) -> None:
        return None


def entry(signal: str, group: str, *, role: str = "setpoint", writable: bool = True,
          **extra: object) -> dict:
    base = {
        "signal": signal,
        "label": signal,
        "pv": "PV:" + signal,
        "unit": "A",
        "writable": writable,
        "required": False,
        "group": group,
        "role": role,
        "min_value": 0.0,
        "max_value": 100.0,
        "max_step": 10.0,
    }
    base.update(extra)
    return base


def marked_mapping() -> list[dict]:
    """带标记的映射（新服务端形状）。"""
    return [
        entry("magnet.m1.current_setpoint", "磁铁电源", tunable=True),
        entry("magnet.m1.current_rate_setpoint", "磁铁电源", tunable=False),
        entry("magnet.m1.switch", "磁铁电源", role="toggle", tunable=False,
              min_value=0.0, max_value=1.0),
        entry("detector.fc1.beam_current", "束流探测", role="readback",
              writable=False, beam_target=True),
        entry("vacuum.chamber_pressure", "腔体气压", role="readback",
              writable=False, beam_target=False),
    ]


CATALOG = {
    "targets": [
        {"signal": "detector.fc1.beam_current", "label": "FC1", "unit": "nA",
         "group": "束流探测", "stage": "detector"}
    ],
    "variables": [
        {"signal": "magnet.m1.current_setpoint", "label": "磁铁1 电流", "unit": "A",
         "group": "磁铁电源", "stage": "magnet", "low": 0.0, "high": 100.0,
         "max_step": 10.0}
    ],
    "stages": [{"key": "magnet", "label": "磁铁电源", "groups": ["磁铁电源"]},
               {"key": "detector", "label": "束流探测", "groups": ["束流探测"]}],
    "upstream": {"detector.fc1.beam_current": ["magnet.m1.current_setpoint"]},
    "linked_sets": [["magnet.m1.current_setpoint", "magnet.m2.current_setpoint"]],
    "excluded": {"变化速率/保护类设定，不作为优化变量": ["magnet.m1.current_rate_setpoint"],
                 "不是束流测量，不能作为优化目标": ["vacuum.chamber_pressure"]},
}


class TuningPageCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        # 页面在填充目标时会去读回读值：单测里换成假线程，既不连服务也不留线程
        self._read_patch = mock.patch.object(
            instrument_api, "request_read", lambda *a, **k: _FakeThread()
        )
        self._read_patch.start()

    def tearDown(self) -> None:
        self._read_patch.stop()

    def make_page(self, mapping: list[dict], catalog: dict | None = None) -> TuningPage:
        page = TuningPage()
        page._mapping = mapping
        page._catalog = dict(catalog or {})
        page._build_variable_rows()
        page._build_targets()
        page._update_variable_hint()
        return page

    def signals(self, page: TuningPage) -> list[str]:
        return [row["entry"]["signal"] for row in page._rows]

    def test_only_declared_tunable_setpoints_become_variables(self) -> None:
        page = self.make_page(marked_mapping())

        self.assertEqual(self.signals(page), ["magnet.m1.current_setpoint"])

    def test_parameter_table_shows_titles_not_pv_names(self) -> None:
        """表格只显示设备参数的**标题**：PV 名对操作员没有信息量，还把表挤窄。

        要核对 PV 的走「系统设置 → PV 映射」（那里有连接状态与当前值）；
        表格里只把业务信号与 PV 留在 tooltip 上，方便顺手看一眼。
        """
        page = self.make_page(marked_mapping())

        headers = [
            page.parameter_table.horizontalHeaderItem(index).text()
            for index in range(page.parameter_table.columnCount())
        ]
        self.assertEqual(headers, ["启用", "设备参数", "下限", "上限", "当前回读"])
        self.assertNotIn("PV", headers)

        cell = page.parameter_table.item(0, 1)
        self.assertEqual(cell.text(), "magnet.m1.current_setpoint")
        self.assertIn("业务信号：magnet.m1.current_setpoint", cell.toolTip())
        self.assertIn("PV：PV:magnet.m1.current_setpoint", cell.toolTip())
        # 范围与回读列都跟着左移了一格：控件还在原来的位置上
        self.assertIsNotNone(page.parameter_table.cellWidget(0, 2))
        self.assertIsNotNone(page.parameter_table.cellWidget(0, 3))
        self.assertIsNotNone(page.parameter_table.cellWidget(0, 4))

    def test_rate_setpoint_and_switch_are_not_offered(self) -> None:
        page = self.make_page(marked_mapping())

        self.assertNotIn("magnet.m1.current_rate_setpoint", self.signals(page))
        self.assertNotIn("magnet.m1.switch", self.signals(page))

    def test_only_declared_beam_targets_are_offered(self) -> None:
        page = self.make_page(marked_mapping())
        targets = [page.target.itemData(i) for i in range(page.target.count())]

        self.assertEqual(targets, ["detector.fc1.beam_current"])
        self.assertNotIn("vacuum.chamber_pressure", targets)

    def test_older_service_falls_back_by_name_and_says_so(self) -> None:
        """老服务端不带标记字段：按命名规则回退，并在提示里写清楚，不能变成 0 行。"""
        legacy = [
            entry("magnet.m1.current_setpoint", "磁铁电源"),
            entry("magnet.m1.current_rate_setpoint", "磁铁电源"),
            entry("detector.fc1.beam_current", "束流探测", role="readback",
                  writable=False),
        ]
        page = self.make_page(legacy)

        self.assertEqual(self.signals(page), ["magnet.m1.current_setpoint"])
        self.assertTrue(page._used_fallback)
        self.assertIn("回退", page.variable_hint.text())

    def test_selecting_a_target_checks_its_upstream_variables(self) -> None:
        mapping = marked_mapping() + [
            entry("magnet.m2.current_setpoint", "磁铁电源", tunable=True)
        ]
        page = self.make_page(mapping, CATALOG)

        page._select_upstream_of_current_target()

        checked = {
            row["entry"]["signal"]
            for row in page._rows
            if row["check"].checkState() == Qt.CheckState.Checked
        }
        self.assertEqual(checked, {"magnet.m1.current_setpoint"})
        self.assertIn("已自动勾选", page.variable_hint.text())

    def test_hint_explains_exclusions_and_linked_groups(self) -> None:
        page = self.make_page(marked_mapping(), CATALOG)

        hint = page.variable_hint.text()

        self.assertIn("未列入", hint)
        self.assertIn("保护类设定", hint)
        self.assertIn("联动组", hint)
        self.assertIn("同一个优化维度", hint)
        self.assertNotIn("回退过滤", hint, "服务端给了标记就不该提示回退")

    def test_missing_catalog_still_filters_and_skips_autoselect(self) -> None:
        """拿不到目录（老服务端）不联动，但白名单过滤照旧生效。"""
        page = self.make_page(marked_mapping(), None)

        page._select_upstream_of_current_target()

        self.assertEqual(
            [row["check"].checkState() for row in page._rows],
            [Qt.CheckState.Unchecked],
        )


class TuningGuardDisplayTests(unittest.TestCase):
    """束流丢失保护在界面上的三件事：起点、状态、逐路回退结果。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._read_patch = mock.patch.object(
            instrument_api, "request_read", lambda *a, **k: _FakeThread()
        )
        self._read_patch.start()

    def tearDown(self) -> None:
        self._read_patch.stop()

    def make_page(self) -> TuningPage:
        return TuningPage()

    def test_change_table_starts_from_the_pre_run_snapshot(self) -> None:
        """起点必须是启动前快照：拿第一轮回读当起点会把调束自己的第一步算进去。"""
        page = self.make_page()
        page._snapshot = {"magnet.m1.current_setpoint": 150.0}
        page._iterations = [
            {
                "iteration": 0,
                "proposed": {}, "applied": {}, "target": 9.0, "objective": 9.0,
                "quality": "ok",
                "at": "", "readback": {"magnet.m1.current_setpoint": 180.0},
            },
            {
                "iteration": 1,
                "proposed": {}, "applied": {}, "target": 12.0, "objective": 12.0,
                "quality": "ok",
                "at": "", "readback": {"magnet.m1.current_setpoint": 200.0},
            },
        ]

        page._fill_changes()

        self.assertEqual(page.changes_table.item(0, 1).text(), "150.000")
        self.assertEqual(page.changes_table.item(0, 2).text(), "200.000")
        self.assertEqual(page.changes_table.item(0, 3).text(), "+50.000")
        self.assertIn("启动前快照", page.change_note.text())

    def test_change_note_says_when_there_is_no_snapshot(self) -> None:
        page = self.make_page()
        page._iterations = [
            {
                "iteration": 0, "proposed": {}, "applied": {}, "target": 9.0,
                "objective": 9.0, "quality": "ok", "at": "",
                "readback": {"magnet.m1.current_setpoint": 180.0},
            }
        ]

        page._fill_changes()

        self.assertIn("没有拿到启动前快照", page.change_note.text())

    def test_gain_card_uses_the_baseline_when_available(self) -> None:
        page = self.make_page()
        page._baseline = 10.0
        page._iterations = [
            {
                "iteration": 0, "proposed": {}, "applied": {}, "target": 8.0,
                "objective": 8.0, "quality": "ok", "at": "", "readback": {},
            },
            {
                "iteration": 1, "proposed": {}, "applied": {}, "target": 12.0,
                "objective": 12.0, "quality": "ok", "at": "", "readback": {},
            },
        ]

        page._redraw()

        self.assertEqual(page.gain_card.value_label.text(), "+2.000")

    def test_recovery_block_lists_the_reason_and_every_readback(self) -> None:
        page = self.make_page()
        page._recovery = {
            "reason": "目标 0.2 已低于绝对阈值 1",
            "at": "2026-09-14T04:00:00+00:00",
            "ok": True,
            "restored": {"magnet.m1.current_setpoint": 150.0},
            "detail": "",
        }

        page._render_recovery()

        text = page.recovery_label.text()
        self.assertFalse(page.recovery_label.isHidden())
        self.assertIn("绝对阈值", text)
        self.assertIn("已退回启动前参数", text)
        self.assertIn("magnet.m1.current_setpoint=150.000", text)
        self.assertEqual(page.recovery_label.property("state"), "good")

    def test_failed_recovery_is_shown_as_an_error_not_a_success(self) -> None:
        page = self.make_page()
        page._recovery = {
            "reason": "目标掉到近零",
            "at": "",
            "ok": False,
            "restored": {"magnet.m1.current_setpoint": 120.0},
            "detail": "磁铁1 电流 回退后未进入容差（当前 120.0）",
        }

        page._render_recovery()

        self.assertIn("回退未完成", page.recovery_label.text())
        self.assertIn("未进入容差", page.recovery_label.text())
        self.assertEqual(page.recovery_label.property("state"), "error")

    def test_status_carries_snapshot_baseline_and_note(self) -> None:
        page = self.make_page()

        page._apply_status(
            {
                "state": "running",
                "snapshot": {"magnet.m1.current_setpoint": 150.0},
                "baseline_objective": 10.0,
                "snapshot_note": "",
                "completed_iterations": 1,
                "max_iterations": 5,
            }
        )

        self.assertEqual(page._snapshot, {"magnet.m1.current_setpoint": 150.0})
        self.assertEqual(page._baseline, 10.0)
        self.assertIn("启动前快照", page.snapshot_label.text())
        self.assertIn("目标基线 10.000", page.snapshot_label.text())

    def test_snapshot_note_is_surfaced_when_the_baseline_is_missing(self) -> None:
        page = self.make_page()

        page._apply_status(
            {
                "state": "running",
                "snapshot": {"magnet.m1.current_setpoint": 150.0},
                "baseline_objective": None,
                "snapshot_note": "目标基线读不到，只能按绝对阈值判断",
                "completed_iterations": 0,
                "max_iterations": 5,
            }
        )

        self.assertIn("只能按绝对阈值判断", page.snapshot_label.text())

    def test_loss_thresholds_are_sent_with_the_start_request(self) -> None:
        page = self.make_page()
        page._mapping = marked_mapping()
        page._build_variable_rows()
        page._build_targets()
        page._rows[0]["check"].setCheckState(Qt.CheckState.Checked)
        page.loss_absolute_check.setChecked(True)
        page.loss_absolute_spin.setValue(0.5)
        page.loss_relative_spin.setValue(35.0)
        page.loss_strikes_spin.setValue(3)
        page.auto_recover_check.setChecked(False)
        captured: dict = {}

        def fake_start(request: dict, base_url=None):  # noqa: ANN001
            captured.update(request)
            return _FakeThread()

        with mock.patch.object(instrument_api, "request_tuning_start", fake_start):
            page.start_tuning()

        self.assertEqual(captured["loss_absolute"], 0.5)
        self.assertAlmostEqual(captured["loss_relative"], 0.35)
        self.assertEqual(captured["loss_strikes"], 3)
        self.assertFalse(captured["auto_recover"])

    def test_absolute_threshold_is_omitted_when_not_enabled(self) -> None:
        page = self.make_page()
        page._mapping = marked_mapping()
        page._build_variable_rows()
        page._build_targets()
        page._rows[0]["check"].setCheckState(Qt.CheckState.Checked)
        captured: dict = {}

        def fake_start(request: dict, base_url=None):  # noqa: ANN001
            captured.update(request)
            return _FakeThread()

        with mock.patch.object(instrument_api, "request_tuning_start", fake_start):
            page.start_tuning()

        self.assertIsNone(captured["loss_absolute"])

    def test_absolute_spin_only_editable_when_enabled(self) -> None:
        page = self.make_page()

        self.assertFalse(page.loss_absolute_spin.isEnabled())

        page.loss_absolute_check.setChecked(True)

        self.assertTrue(page.loss_absolute_spin.isEnabled())


class TuningFinalizeUiTests(unittest.TestCase):
    """结束后处置：三个按钮、二次确认、以及只有服务端说成功才算成功。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def make_page(self, *, confirm: bool = True, run_id: str | None = "run-1") -> TuningPage:
        page = TuningPage()
        page._run_id = run_id
        page._ask_finalize_confirm = lambda label: confirm  # type: ignore[method-assign]
        return page

    def capture(self, page: TuningPage, outcome: dict) -> list[tuple[str, str]]:
        """替换请求函数，记录 (run_id, action)，并立刻回调给定结果。"""
        sent: list[tuple[str, str]] = []

        def fake(run_id: str, action: str, base_url=None):  # noqa: ANN001
            sent.append((run_id, action))
            return _FakeThread(completed_payload=outcome)

        self._patch = mock.patch.object(instrument_api, "request_tuning_finalize", fake)
        self._patch.start()
        return sent

    def tearDown(self) -> None:
        patch = getattr(self, "_patch", None)
        if patch is not None:
            patch.stop()

    def test_three_actions_are_offered_and_disabled_until_a_run_exists(self) -> None:
        page = self.make_page(run_id=None)

        self.assertEqual(
            list(page.finalize_buttons), ["apply_best", "restore_initial", "safe_values"]
        )
        self.assertTrue(all(not b.isEnabled() for b in page.finalize_buttons.values()))

    def test_cancelled_confirmation_sends_nothing(self) -> None:
        page = self.make_page(confirm=False)
        sent = self.capture(page, {"ok": True, "payload": {}})

        page.finalize_tuning("apply_best")

        self.assertEqual(sent, [])
        self.assertIn("已取消", page.finalize_label.text())

    def test_confirmed_action_is_sent_with_the_run_id(self) -> None:
        page = self.make_page()
        sent = self.capture(
            page,
            {
                "ok": True,
                "payload": {
                    "ok": True,
                    "action": "restore_initial",
                    "message": "恢复启动前参数完成（1 路已到位）",
                    "applied": {"magnet.m1.current_setpoint": 150.0},
                },
            },
        )

        page.finalize_tuning("restore_initial")

        self.assertEqual(sent, [("run-1", "restore_initial")])
        self.assertIn("恢复启动前参数完成", page.finalize_label.text())
        self.assertIn("magnet.m1.current_setpoint=150.000", page.finalize_label.text())
        self.assertEqual(page.finalize_label.property("state"), "good")

    def test_service_level_failure_is_shown_as_an_error(self) -> None:
        """动作写不进去时 ok=false：绝不能显示成成功。"""
        page = self.make_page()
        self.capture(
            page,
            {
                "ok": True,
                "payload": {
                    "ok": False,
                    "action": "safe_values",
                    "message": "回安全值未完成",
                    "applied": {"magnet.m1.current_setpoint": 40.0},
                    "detail": "磁铁1 电流 未进入容差（当前 40.0）",
                },
            },
        )

        page.finalize_tuning("safe_values")

        text = page.finalize_label.text()
        self.assertIn("未完成", text)
        self.assertIn("未进入容差", text)
        self.assertNotIn("已完成", text)
        self.assertEqual(page.finalize_label.property("state"), "error")

    def test_http_level_failure_is_shown_with_the_reason(self) -> None:
        page = self.make_page()
        self.capture(page, {"ok": False, "message": "设备组当前不可用：磁铁电源（被 x 占用）"})

        page.finalize_tuning("apply_best")

        self.assertIn("未执行", page.finalize_label.text())
        self.assertIn("设备组当前不可用", page.finalize_label.text())
        self.assertEqual(page.finalize_label.property("state"), "error")

    def test_buttons_are_disabled_while_recovery_is_pending(self) -> None:
        page = self.make_page()

        page._apply_status(
            {
                "state": "recovery_required",
                "snapshot": {}, "samples_per_point": 1,
                "completed_iterations": 1, "max_iterations": 3,
            }
        )

        self.assertTrue(
            all(not b.isEnabled() for b in page.finalize_buttons.values()),
            "设备状态未知时不能处置设备",
        )

    def test_buttons_are_enabled_after_a_completed_run(self) -> None:
        page = self.make_page()

        page._apply_status(
            {
                "state": "completed",
                "snapshot": {}, "completed_iterations": 2, "max_iterations": 2,
            }
        )

        self.assertTrue(all(b.isEnabled() for b in page.finalize_buttons.values()))

    def test_stored_result_is_rendered_on_status(self) -> None:
        """关掉界面再打开也能看到上次做过什么处置。"""
        page = self.make_page()

        page._apply_status(
            {
                "state": "completed",
                "snapshot": {}, "completed_iterations": 2, "max_iterations": 2,
                "finalize": {
                    "ok": True,
                    "action": "apply_best",
                    "message": "应用最优参数完成（1 路已到位）",
                    "applied": {"magnet.m1.current_setpoint": 180.0},
                    "detail": "",
                    "at": "2026-09-14T05:00:00+00:00",
                },
            }
        )

        self.assertIn("应用最优参数已完成", page.finalize_label.text())
        self.assertIn("magnet.m1.current_setpoint=180.000", page.finalize_label.text())


class TuningStrategyUiTests(unittest.TestCase):
    """两阶段策略在界面上的三件事：选择、参数下发、阶段可见。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._read_patch = mock.patch.object(
            instrument_api, "request_read", lambda *a, **k: _FakeThread()
        )
        self._read_patch.start()

    def tearDown(self) -> None:
        self._read_patch.stop()
        patch = getattr(self, "_start_patch", None)
        if patch is not None:
            patch.stop()

    def ready_page(self) -> TuningPage:
        page = TuningPage()
        page._mapping = marked_mapping()
        page._build_variable_rows()
        page._build_targets()
        page._rows[0]["check"].setCheckState(Qt.CheckState.Checked)
        return page

    def capture_start(self, page: TuningPage) -> dict:
        captured: dict = {}

        def fake(request: dict, base_url=None):  # noqa: ANN001
            captured.update(request)
            return _FakeThread()

        self._start_patch = mock.patch.object(
            instrument_api, "request_tuning_start", fake
        )
        self._start_patch.start()
        return captured

    def test_default_strategy_is_full_joint(self) -> None:
        page = self.ready_page()
        captured = self.capture_start(page)

        page.start_tuning()

        self.assertEqual(captured["strategy"], "joint")
        self.assertEqual(captured["joint_frac"], 0.2)
        self.assertEqual(captured["calls_per_variable"], 3)
        self.assertEqual(captured["hold_s"], 0.0)

    def test_sequential_strategy_and_its_parameters_are_sent(self) -> None:
        page = self.ready_page()
        page.strategy.setCurrentIndex(1)  # 逐参数 → 联合微调
        page.calls_spin.setValue(4)
        page.joint_frac_spin.setValue(35.0)
        page.hold_spin.setValue(2.5)
        captured = self.capture_start(page)

        page.start_tuning()

        self.assertEqual(captured["strategy"], "sequential_then_joint")
        self.assertEqual(captured["calls_per_variable"], 4)
        self.assertAlmostEqual(captured["joint_frac"], 0.35)
        self.assertEqual(captured["hold_s"], 2.5)

    def test_stage_position_is_shown_in_the_iteration_label(self) -> None:
        page = TuningPage()

        page._apply_status(
            {
                "state": "awaiting_confirmation",
                "completed_iterations": 2,
                "max_iterations": 9,
                "strategy": "sequential_then_joint",
                "stage": "sequential",
                "stage_index": 1,
                "stage_total": 3,
                "stage_variable": "magnet.m1.current_setpoint",
            }
        )

        text = page.iteration_label.text()
        self.assertIn("已完成 2 / 9 轮", text)
        self.assertIn("逐参数 1/3", text)
        self.assertIn("magnet.m1.current_setpoint", text)

    def test_joint_stage_is_shown_too(self) -> None:
        page = TuningPage()

        page._apply_status(
            {
                "state": "awaiting_confirmation",
                "completed_iterations": 6,
                "max_iterations": 9,
                "stage": "joint",
            }
        )

        self.assertIn("联合微调", page.iteration_label.text())

    def test_proposal_scope_names_the_active_variable(self) -> None:
        page = TuningPage()
        page._mapping = marked_mapping()
        page._build_variable_rows()
        page._build_targets()

        page._apply_status(
            {
                "state": "awaiting_confirmation",
                "completed_iterations": 0,
                "max_iterations": 6,
                "stage": "sequential",
                "pending": {
                    "iteration": 0,
                    "values": {"magnet.m1.current_setpoint": 200.0},
                    "active_signals": ["magnet.m1.current_setpoint"],
                    "stage": "sequential",
                    "predicted": 9.0,
                    "std": 0.5,
                    "expected_improvement": 0.1,
                    "at": "",
                },
            }
        )

        text = page.proposal_label.text()
        self.assertIn("本轮调整：magnet.m1.current_setpoint", text)
        self.assertNotIn("全部选中参数", text)


if __name__ == "__main__":
    unittest.main()
