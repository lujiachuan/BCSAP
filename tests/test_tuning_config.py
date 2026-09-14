"""调束配置保存与恢复（改造报告 §5.2「配置持久化」/ §8.9 P2.3）。

原 demo 的 ``bayes_config.json`` 只有"存"和"读"，文件坏了就静默什么都不恢复。这里
两侧都要立住：**文件层**（坏 JSON、坏字段类型、未知策略、越界值各是什么下场）与
**页面层**（保存→重开→界面真的回到那一套；配置里有、映射里没有的信号要点名）。
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

from apps.desktop_client import instrument_api, tuning_config
from apps.desktop_client.pages import tuning as tuning_module
from apps.desktop_client.pages.tuning import TuningPage

FINALIZE_KEYS = ("apply_best", "restore_initial", "safe_values")


def entry(signal: str, group: str, **extra: object) -> dict:
    base = {
        "signal": signal,
        "label": signal,
        "pv": "PV:" + signal,
        "unit": "A",
        "writable": True,
        "required": False,
        "group": group,
        "role": "setpoint",
        "min_value": 0.0,
        "max_value": 100.0,
        "max_step": 10.0,
        "tunable": True,
    }
    base.update(extra)
    return base


def beam(signal: str = "detector.fc1.beam_current") -> dict:
    return {
        "signal": signal,
        "label": "FC1 束流电流",
        "pv": "BD:FC:01:BeamCurrent",
        "unit": "nA",
        "writable": False,
        "required": True,
        "group": "束流探测",
        "role": "readback",
        "beam_target": True,
    }


MAPPING = [
    entry("magnet.m1.current_setpoint", "磁铁电源"),
    entry("magnet.m2.current_setpoint", "磁铁电源"),
    entry("focus.v1.voltage_setpoint", "聚焦电源"),
    beam(),
]

CATALOG = {
    "targets": [
        {"signal": "detector.fc1.beam_current", "label": "FC1", "unit": "nA",
         "group": "束流探测", "stage": "detector"}
    ],
    "variables": [
        {"signal": "magnet.m1.current_setpoint", "label": "磁铁1", "unit": "A",
         "group": "磁铁电源", "stage": "magnet", "low": 0.0, "high": 100.0,
         "max_step": 10.0},
        {"signal": "magnet.m2.current_setpoint", "label": "磁铁2", "unit": "A",
         "group": "磁铁电源", "stage": "magnet", "low": 0.0, "high": 100.0,
         "max_step": 10.0},
        {"signal": "focus.v1.voltage_setpoint", "label": "聚焦电压", "unit": "V",
         "group": "聚焦电源", "stage": "focus", "low": 0.0, "high": 100.0,
         "max_step": 10.0},
    ],
    "stages": [
        {"key": "magnet", "label": "磁铁电源", "groups": ["磁铁电源"]},
        {"key": "focus", "label": "聚焦电源", "groups": ["聚焦电源"]},
        {"key": "detector", "label": "束流探测", "groups": ["束流探测"]},
    ],
    # 目标的上游 = 磁铁 1、2（不含聚焦电压）：拓扑联动只勾这两个
    "upstream": {
        "detector.fc1.beam_current": [
            "magnet.m1.current_setpoint",
            "magnet.m2.current_setpoint",
        ]
    },
    "linked_sets": [["magnet.m1.current_setpoint", "magnet.m2.current_setpoint"]],
    "excluded": {},
}


def sample_config() -> dict:
    return {
        "version": 1,
        "target_signal": "detector.fc1.beam_current",
        "variables": [
            {"signal": "magnet.m1.current_setpoint", "enabled": True, "low": 10.0, "high": 60.0},
            {"signal": "magnet.m2.current_setpoint", "enabled": False, "low": 0.0, "high": 100.0},
            {"signal": "focus.v1.voltage_setpoint", "enabled": True, "low": 5.0, "high": 40.0},
        ],
        "strategy": "sequential_then_joint",
        "calls_per_variable": 4,
        "joint_frac": 15.0,
        "hold_s": 3.0,
        "max_iterations": 12,
        "settle_timeout_s": 30.0,
        "samples_per_point": 5,
        "loss_absolute": 2.5,
        "loss_relative": 40.0,
        "loss_strikes": 3,
        "auto_recover": False,
        "finalize_action": "restore_initial",
    }


class TuningConfigFileTests(unittest.TestCase):
    """文件层：存得住、读得回，坏内容只丢坏的那一项。"""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "tuning_config.json"

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_round_trip_keeps_every_field(self) -> None:
        tuning_config.save(sample_config(), self.path)

        loaded, note = tuning_config.load(
            self.path, strategies=("joint", "sequential_then_joint"),
            actions=FINALIZE_KEYS,
        )

        self.assertEqual(note, "")
        self.assertEqual(loaded, sample_config())

    def test_version_is_stamped_on_save(self) -> None:
        tuning_config.save({"max_iterations": 5}, self.path)

        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["version"],
            tuning_config.CONFIG_VERSION,
        )

    def test_no_file_is_not_an_error(self) -> None:
        """从没用过配置是正常状态：返回 None 但不给错误文案。"""
        loaded, note = tuning_config.load(self.path)

        self.assertIsNone(loaded)
        self.assertEqual(note, "")

    def test_broken_json_is_reported(self) -> None:
        self.path.write_text("{坏掉的 json", encoding="utf-8")

        loaded, note = tuning_config.load(self.path)

        self.assertIsNone(loaded)
        self.assertIn("读不出来", note)
        self.assertIn(str(self.path), note)

    def test_wrong_field_types_are_dropped_one_by_one(self) -> None:
        """一个字段坏掉不该拖走整份配置：只丢坏的，其余照用。"""
        raw = sample_config()
        raw["max_iterations"] = "十二"
        raw["variables"][0]["low"] = "十"

        config, notes = tuning_config.sanitize(
            raw, strategies=("joint", "sequential_then_joint"), actions=FINALIZE_KEYS
        )

        self.assertNotIn("max_iterations", config)
        self.assertNotIn("low", config["variables"][0])
        self.assertEqual(config["variables"][0]["high"], 60.0)
        self.assertEqual(config["samples_per_point"], 5.0)
        self.assertTrue(any("最大轮次" in n for n in notes), notes)
        self.assertTrue(any("low" in n for n in notes), notes)

    def test_unknown_strategy_and_action_fall_back_to_page_defaults(self) -> None:
        raw = sample_config()
        raw["strategy"] = "全都要"
        raw["finalize_action"] = "重启设备"

        config, notes = tuning_config.sanitize(
            raw, strategies=("joint", "sequential_then_joint"), actions=FINALIZE_KEYS
        )

        self.assertNotIn("strategy", config)
        self.assertNotIn("finalize_action", config)
        self.assertEqual(len(notes), 2)

    def test_unknown_extra_keys_are_ignored(self) -> None:
        raw = sample_config()
        raw["device_address"] = "pv://现场"
        raw["variables"][0]["pv"] = "BD:DipoleMagnet:01:CurrentSet"

        config, _notes = tuning_config.sanitize(raw, actions=FINALIZE_KEYS)

        self.assertNotIn("device_address", config)
        self.assertNotIn("pv", config["variables"][0])

    def test_range_is_not_silently_clamped(self) -> None:
        """越界范围原样保留：夹到边界会让操作员以为自己的范围被接受了。"""
        raw = sample_config()
        raw["variables"][0]["high"] = 9999.0

        config, _notes = tuning_config.sanitize(raw, actions=FINALIZE_KEYS)

        self.assertEqual(config["variables"][0]["high"], 9999.0)

    def test_non_object_payload_is_refused(self) -> None:
        config, notes = tuning_config.sanitize([1, 2, 3])

        self.assertEqual(config, {})
        self.assertTrue(notes)

    def test_variable_without_a_signal_is_dropped(self) -> None:
        config, notes = tuning_config.sanitize(
            {"variables": [{"enabled": True}, {"signal": "a.b", "enabled": True}]}
        )

        self.assertEqual([v["signal"] for v in config["variables"]], ["a.b"])
        self.assertTrue(notes)

    def test_save_leaves_no_temporary_file(self) -> None:
        tuning_config.save(sample_config(), self.path)

        leftovers = [p.name for p in Path(self._directory.name).iterdir()]
        self.assertEqual(leftovers, ["tuning_config.json"])

    def test_missing_signals_name_what_is_gone(self) -> None:
        missing = tuning_config.missing_signals(
            sample_config(), {"magnet.m1.current_setpoint"}
        )

        self.assertEqual(
            sorted(missing),
            ["focus.v1.voltage_setpoint", "magnet.m2.current_setpoint"],
        )

    def test_describe_counts_enabled_variables(self) -> None:
        text = tuning_config.describe(sample_config())

        self.assertIn("2 个变量", text)
        self.assertIn("sequential_then_joint", text)
        self.assertIn("12", text)

    def test_environment_moves_the_default_path(self) -> None:
        with mock.patch.dict(
            os.environ, {tuning_config.CONFIG_ENV_VAR: str(self.path)}
        ):
            self.assertEqual(tuning_config.default_config_path(), self.path)


class TuningPageConfigTests(unittest.TestCase):
    """页面层：保存 → 重开 → 界面回到那一套；映射变了要点名。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "tuning_config.json"
        self._env = mock.patch.dict(
            os.environ, {tuning_config.CONFIG_ENV_VAR: str(self.path)}
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._directory.cleanup)

        self.requests: list[str] = []
        for name in ("request_read", "request_tuning_catalog"):
            self._stub(name, lambda *a, **k: _FakeThread())
        for name in ("request_tuning_start", "request_tuning_iterations"):
            self._stub(
                name,
                lambda *a, _name=name, **k: self.requests.append(_name) or _FakeThread(),
            )
        self._stub_module(
            "PvMappingRequestThread", lambda url, payload=None: _FakeThread()
        )

    def _stub(self, name: str, replacement) -> None:
        original = getattr(instrument_api, name)
        setattr(instrument_api, name, replacement)
        self.addCleanup(setattr, instrument_api, name, original)

    def _stub_module(self, name: str, replacement) -> None:
        original = getattr(tuning_module, name)
        setattr(tuning_module, name, replacement)
        self.addCleanup(setattr, tuning_module, name, original)

    def make_page(self, mapping: list[dict] | None = None) -> TuningPage:
        """建页并喂映射 + 目录：走的是真实回调（含"自动载入上次配置"）。"""
        page = TuningPage()
        self.addCleanup(page.deleteLater)
        page._on_mapping({"ok": True, "config": {"entries": list(mapping or MAPPING)}})
        page._on_catalog({"ok": True, "payload": CATALOG})
        return page

    def row(self, page: TuningPage, signal: str) -> dict:
        return next(r for r in page._rows if r["entry"]["signal"] == signal)

    def checked(self, page: TuningPage) -> list[str]:
        return [
            r["entry"]["signal"]
            for r in page._rows
            if r["check"].checkState() == Qt.CheckState.Checked
        ]

    # ---------------- 保存 ----------------
    def test_saving_writes_the_current_setup(self) -> None:
        page = self.make_page()
        row = self.row(page, "focus.v1.voltage_setpoint")
        row["check"].setCheckState(Qt.CheckState.Checked)
        row["low"].setValue(5.0)
        row["high"].setValue(40.0)
        page.calls_spin.setValue(4)
        page.iterations_spin.setValue(12)
        page.hold_spin.setValue(3.0)
        page.finalize_pref.setCurrentIndex(page.finalize_pref.findData("safe_values"))

        page.save_tuning_config()

        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["strategy"], page.strategy.currentData())
        self.assertEqual(saved["calls_per_variable"], 4)
        self.assertEqual(saved["max_iterations"], 12)
        self.assertEqual(saved["hold_s"], 3.0)
        self.assertEqual(saved["finalize_action"], "safe_values")
        variables = {v["signal"]: v for v in saved["variables"]}
        self.assertEqual(variables["focus.v1.voltage_setpoint"]["high"], 40.0)
        self.assertIn("已保存调束配置", page.config_note.text())

    def test_saving_a_broken_directory_only_reports(self) -> None:
        """写不进去不该影响调束：只说清楚，不抛异常。"""
        page = self.make_page()
        self._env.stop()
        with mock.patch.dict(
            os.environ,
            {tuning_config.CONFIG_ENV_VAR: str(self._directory.name + "/nul/x.json")},
        ):
            page.save_tuning_config()

        self.assertIn("保存失败", page.config_note.text())

    # ---------------- 自动载入 ----------------
    def test_page_restores_the_saved_setup_on_open(self) -> None:
        tuning_config.save(sample_config(), self.path)

        page = self.make_page()

        self.assertEqual(
            sorted(self.checked(page)),
            ["focus.v1.voltage_setpoint", "magnet.m1.current_setpoint"],
        )
        self.assertEqual(self.row(page, "magnet.m1.current_setpoint")["high"].value(), 60.0)
        self.assertEqual(self.row(page, "magnet.m1.current_setpoint")["low"].value(), 10.0)
        self.assertEqual(page.strategy.currentData(), "sequential_then_joint")
        self.assertEqual(page.calls_spin.value(), 4)
        self.assertEqual(page.joint_frac_spin.value(), 15.0)
        self.assertEqual(page.hold_spin.value(), 3.0)
        self.assertEqual(page.iterations_spin.value(), 12)
        self.assertEqual(page.settle_spin.value(), 30.0)
        self.assertEqual(page.samples_spin.value(), 5)
        self.assertTrue(page.loss_absolute_check.isChecked())
        self.assertEqual(page.loss_absolute_spin.value(), 2.5)
        self.assertEqual(page.loss_relative_spin.value(), 40.0)
        self.assertEqual(page.loss_strikes_spin.value(), 3)
        self.assertFalse(page.auto_recover_check.isChecked())
        self.assertEqual(page.finalize_pref.currentData(), "restore_initial")
        self.assertIn("已载入调束配置", page.config_note.text())

    def test_saved_checks_win_over_topology_auto_selection(self) -> None:
        """目录里"上游 = 磁铁 1、2"，但保存的配置只勾了聚焦电压：以保存的为准。"""
        config = sample_config()
        config["variables"] = [
            {"signal": "magnet.m1.current_setpoint", "enabled": False},
            {"signal": "magnet.m2.current_setpoint", "enabled": False},
            {"signal": "focus.v1.voltage_setpoint", "enabled": True},
        ]
        tuning_config.save(config, self.path)

        page = self.make_page()

        self.assertEqual(self.checked(page), ["focus.v1.voltage_setpoint"])

    def test_without_a_saved_config_topology_still_auto_selects(self) -> None:
        page = self.make_page()

        self.assertEqual(
            sorted(self.checked(page)),
            ["magnet.m1.current_setpoint", "magnet.m2.current_setpoint"],
        )
        self.assertIn("还没有保存过调束配置", page.config_note.text())

    def test_config_is_applied_only_once(self) -> None:
        """后台再刷一次映射不该把操作员当前改动冲掉。"""
        tuning_config.save(sample_config(), self.path)
        page = self.make_page()
        self.row(page, "magnet.m2.current_setpoint")["check"].setCheckState(
            Qt.CheckState.Checked
        )

        page._on_mapping({"ok": True, "config": {"entries": list(MAPPING)}})

        self.assertIn("magnet.m2.current_setpoint", self.checked(page))

    def test_signals_missing_from_the_mapping_are_named(self) -> None:
        config = sample_config()
        config["variables"].append(
            {"signal": "magnet.m9.current_setpoint", "enabled": True}
        )
        tuning_config.save(config, self.path)

        page = self.make_page()

        self.assertIn("magnet.m9.current_setpoint", page.config_note.text())
        self.assertIn("已跳过", page.config_note.text())

    def test_config_without_recognizable_fields_is_refused(self) -> None:
        """空对象/整份被改成别的结构：当成"没有可用配置"，而不是"0 个变量的配置"。"""
        self.path.write_text("{}", encoding="utf-8")

        page = self.make_page()

        self.assertIn("内容无效", page.config_note.text())
        self.assertTrue(page._rows)

    def test_target_is_restored_before_variables(self) -> None:
        """设目标会触发按拓扑自动勾选：先设变量就会被那次联动覆盖。"""
        config = sample_config()
        config["target_signal"] = "detector.fc1.beam_current"
        config["variables"] = [
            {"signal": "magnet.m1.current_setpoint", "enabled": False},
            {"signal": "magnet.m2.current_setpoint", "enabled": False},
            {"signal": "focus.v1.voltage_setpoint", "enabled": True},
        ]
        tuning_config.save(config, self.path)

        page = self.make_page()

        self.assertEqual(page.target.currentData(), "detector.fc1.beam_current")
        self.assertEqual(self.checked(page), ["focus.v1.voltage_setpoint"])

    # ---------------- 载入按钮 / 开跑前自动保存 ----------------
    def test_load_button_reloads_from_disk(self) -> None:
        page = self.make_page()
        page.calls_spin.setValue(9)
        tuning_config.save(sample_config(), self.path)

        page.load_tuning_config()

        self.assertEqual(page.calls_spin.value(), 4)
        self.assertIn("已载入调束配置", page.config_note.text())

    def test_starting_saves_the_config_silently(self) -> None:
        page = self.make_page()
        self.row(page, "focus.v1.voltage_setpoint")["check"].setCheckState(
            Qt.CheckState.Checked
        )
        self.row(page, "magnet.m2.current_setpoint")["check"].setCheckState(
            Qt.CheckState.Unchecked
        )

        page.start_tuning()

        self.assertEqual(self.requests, ["request_tuning_start"])
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            [v["signal"] for v in saved["variables"] if v["enabled"]],
            ["magnet.m1.current_setpoint", "focus.v1.voltage_setpoint"],
        )
        # 静默保存：不在界面上刷"已保存"，避免每跑一轮就多一行噪音
        self.assertNotIn("已保存调束配置", page.config_note.text())

    def test_a_finished_run_suggests_the_saved_disposal(self) -> None:
        """配置里的"完成后处置"只是提示：按钮仍要人点、仍要二次确认。"""
        tuning_config.save(sample_config(), self.path)
        page = self.make_page()

        page._apply_status({"state": "completed"})

        self.assertIn("恢复启动前参数", page.finalize_label.text())
        self.assertIn("仍需点按钮并二次确认", page.finalize_label.text())
        for button in page.finalize_buttons.values():
            self.assertTrue(button.isEnabled())


class _FakeSignal:
    def __init__(self, payload: dict | None = None) -> None:
        self._payload = payload
        self._callbacks: list = []

    def connect(self, callback, *_args, **_kwargs) -> None:
        self._callbacks.append(callback)
        if self._payload is not None:
            callback(self._payload)


class _FakeThread:
    def __init__(self, *_args, completed_payload: dict | None = None, **_kwargs) -> None:
        self.completed = _FakeSignal(completed_payload)
        self.finished = _FakeSignal(None)

    def start(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
