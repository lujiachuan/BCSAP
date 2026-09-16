"""PV 映射配置的校验、持久化与接口行为。"""

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fastapi import HTTPException
from PySide6.QtCore import QSettings

from apps.desktop_client.pages import SystemSettingsPage
from apps.instrument_service import pv_mapping
from apps.instrument_service.app import create_app
from apps.instrument_service.runtime import InstrumentRuntime
from packages.contracts import PvMappingConfig, PvMappingEntry


def entry(
    signal: str = "quadrupole.q1.current",
    label: str = "Q1 电流",
    pv: str = "BL:Q1:ISET",
    unit: str = "A",
    writable: bool = True,
    required: bool = True,
) -> PvMappingEntry:
    return PvMappingEntry(
        signal=signal,
        label=label,
        pv=pv,
        unit=unit,
        writable=writable,
        required=required,
    )


def config_with(*entries: PvMappingEntry) -> PvMappingConfig:
    return PvMappingConfig(version=1, entries=list(entries))


def route_endpoint(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", ()):
            return route.endpoint
    raise AssertionError(f"未找到路由 {method} {path}")


class ValidationTests(unittest.TestCase):
    def test_default_config_has_no_issues(self) -> None:
        self.assertEqual(pv_mapping.validate_config(pv_mapping.default_config()), [])

    def test_duplicate_pv_and_signal_are_reported_per_row(self) -> None:
        config = config_with(
            entry(),
            entry(signal="quadrupole.q2.current", label="Q2 电流", pv="BL:Q1:ISET"),
        )
        issues = pv_mapping.validate_config(config)

        fields = {(issue.index, issue.field) for issue in issues}
        self.assertIn((1, "pv"), fields)
        self.assertNotIn((0, "pv"), fields)
        self.assertIn("重复", next(i.message for i in issues if i.index == 1))

    def test_duplicate_signal_is_reported(self) -> None:
        config = config_with(entry(), entry(pv="BL:Q2:ISET"))
        issues = pv_mapping.validate_config(config)

        self.assertEqual([(i.index, i.field) for i in issues], [(1, "signal")])

    def test_pv_name_with_space_or_bad_signal_is_rejected(self) -> None:
        config = config_with(
            entry(pv="BL:Q1 ISET"),
            entry(signal="BadSignal", label="坏信号", pv="BL:Q2:ISET"),
            entry(signal="", label="空信号", pv="BL:Q3:ISET"),
        )
        issues = pv_mapping.validate_config(config)

        self.assertIn((0, "pv"), {(i.index, i.field) for i in issues})
        self.assertIn((1, "signal"), {(i.index, i.field) for i in issues})
        self.assertIn((2, "signal"), {(i.index, i.field) for i in issues})

    def test_empty_entries_and_blank_label_are_rejected(self) -> None:
        self.assertEqual(
            [(i.index, i.field) for i in pv_mapping.validate_config(config_with())],
            [(-1, "entries")],
        )
        blank = config_with(entry(label="   "))
        self.assertIn((0, "label"), {(i.index, i.field) for i in pv_mapping.validate_config(blank)})

    def test_pv_pattern_accepts_realistic_epics_names(self) -> None:
        for pv in ("BL:Q1:ISET", "SR:DCCT:Current", "BL:STEER:X", "PV[0]", "A-B_C.1"):
            with self.subTest(pv=pv):
                issues = pv_mapping.validate_config(config_with(entry(pv=pv)))
                self.assertEqual(issues, [])


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self._previous = os.environ.get("SPECTRUM_PV_MAPPING")
        os.environ["SPECTRUM_PV_MAPPING"] = str(
            Path(self._directory.name) / "pv_mapping.json"
        )

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop("SPECTRUM_PV_MAPPING", None)
        else:
            os.environ["SPECTRUM_PV_MAPPING"] = self._previous
        self._directory.cleanup()

    def test_round_trip_preserves_entries(self) -> None:
        config = config_with(
            entry(),
            entry(signal="steerer.x", label="X 偏转", pv="BL:STEER:X", unit="V"),
        )
        path = pv_mapping.save_config(config)

        self.assertTrue(path.is_file())
        self.assertEqual(pv_mapping.load_config(), config)

    def test_missing_file_returns_defaults(self) -> None:
        self.assertEqual(pv_mapping.load_config(), pv_mapping.default_config())

    def test_corrupted_file_returns_defaults_instead_of_raising(self) -> None:
        Path(os.environ["SPECTRUM_PV_MAPPING"]).write_text("{ not json", encoding="utf-8")
        self.assertEqual(pv_mapping.load_config(), pv_mapping.default_config())

    def test_file_with_wrong_shape_returns_defaults(self) -> None:
        Path(os.environ["SPECTRUM_PV_MAPPING"]).write_text(
            json.dumps({"version": 1, "entries": "不是列表"}), encoding="utf-8"
        )
        self.assertEqual(pv_mapping.load_config(), pv_mapping.default_config())

    def test_empty_entries_file_returns_defaults(self) -> None:
        """空清单会让健康检查报「ready / 0 项」，属于不可用配置，应回退默认映射。"""
        Path(os.environ["SPECTRUM_PV_MAPPING"]).write_text(
            json.dumps({"version": 1, "entries": []}), encoding="utf-8"
        )
        config = pv_mapping.load_config()

        self.assertEqual(config, pv_mapping.default_config())
        self.assertTrue(config.entries)

    def test_config_written_by_previous_schema_still_loads(self) -> None:
        """旧版本写过 gateway / ca_lib_dir 字段，现在已移除，应忽略多余字段照常读入。"""
        Path(os.environ["SPECTRUM_PV_MAPPING"]).write_text(
            json.dumps(
                {
                    "version": 1,
                    "gateway": "channel-access",
                    "ca_lib_dir": r"D:\EPICS\CA-3.15.6-windows-x64",
                    "entries": [entry(pv="SR:Q1:Current").model_dump()],
                }
            ),
            encoding="utf-8",
        )
        config = pv_mapping.load_config()

        self.assertEqual(len(config.entries), 1)
        self.assertEqual(config.entries[0].pv, "SR:Q1:Current")

    def test_save_leaves_no_temp_files(self) -> None:
        pv_mapping.save_config(config_with(entry()))
        leftovers = list(Path(self._directory.name).glob(".pv_mapping-*"))
        self.assertEqual(leftovers, [])


    def test_rate_signal_must_exist_and_be_writable(self) -> None:
        """速率配错不会当场报错，只会在成组回落时静默不下发速率——必须拦住。"""
        missing = config_with(
            entry().model_copy(update={"rate_signal": "magnet.m1.current_rate_setpoint"})
        )
        issues = pv_mapping.validate_config(missing)

        self.assertIn((0, "rate_signal"), {(i.index, i.field) for i in issues})
        self.assertIn("不在映射里", issues[0].message)

        readonly = config_with(
            entry().model_copy(update={"rate_signal": "steerer.x"}),
            entry(signal="steerer.x", label="X 偏转", pv="BL:STEER:X", writable=False),
        )
        issues = pv_mapping.validate_config(readonly)

        self.assertIn((0, "rate_signal"), {(i.index, i.field) for i in issues})
        self.assertIn("不可写", " ".join(i.message for i in issues))

    def test_default_config_rate_signals_are_valid(self) -> None:
        """真实设备档案里的 rate_signal 必须指向存在且可写的信号。"""
        self.assertEqual(pv_mapping.validate_config(pv_mapping.default_config()), [])


class MappingApiTests(unittest.TestCase):
    """接口层测试：PUT/GET 与健康检查的联动。

    设备访问现在统一走真实 CA，所以这里把 CA 钉死在 127.0.0.1 + 不做广播发现，
    确保测试**永远不会碰到现场真实 IOC**（本机没有模拟 IOC 时只是全部报未连接）。
    """

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self._previous = {
            key: os.environ.get(key)
            for key in ("SPECTRUM_PV_MAPPING", "EPICS_CA_ADDR_LIST", "EPICS_CA_AUTO_ADDR_LIST")
        }
        os.environ["SPECTRUM_PV_MAPPING"] = str(
            Path(self._directory.name) / "pv_mapping.json"
        )
        os.environ["EPICS_CA_ADDR_LIST"] = "127.0.0.1"
        os.environ["EPICS_CA_AUTO_ADDR_LIST"] = "NO"
        self.runtime = InstrumentRuntime(pv_mapping.default_config())
        self.app = create_app(self.runtime)
        self.get_mapping = route_endpoint(self.app, "/control/v1/pv-mapping", "GET")
        self.put_mapping = route_endpoint(self.app, "/control/v1/pv-mapping", "PUT")

    def tearDown(self) -> None:
        self.runtime.close()
        for key, value in self._previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._directory.cleanup()

    def test_put_persists_and_takes_effect_immediately(self) -> None:
        updated = config_with(
            entry(signal="quadrupole.q1.current", label="Q1 电流", pv="SR:Q1:Current")
        )
        # 这一条就是要把 128 条换成 1 条：按新加的骤减保护必须显式确认
        saved = self.put_mapping(updated, confirm_shrink=True)

        self.assertEqual(saved.entries[0].pv, "SR:Q1:Current")
        self.assertEqual(self.get_mapping().entries[0].pv, "SR:Q1:Current")
        self.assertEqual(pv_mapping.load_config().entries[0].pv, "SR:Q1:Current")

    def test_put_rejects_invalid_config_with_row_level_issues(self) -> None:
        invalid = config_with(entry(pv="BL:Q1 ISET"))

        with self.assertRaises(HTTPException) as caught:
            self.put_mapping(invalid)

        self.assertEqual(caught.exception.status_code, 400)
        issues = caught.exception.detail["issues"]
        self.assertEqual(issues[0]["index"], 0)
        self.assertEqual(issues[0]["field"], "pv")
        # 校验失败不得写盘
        self.assertEqual(pv_mapping.load_config(), pv_mapping.default_config())

    def test_added_row_becomes_visible_in_health_check(self) -> None:
        added = config_with(
            entry(
                signal="quadrupole.q3.current",
                label="Q3 电流",
                pv="BL:Q3:ISET",
                unit="A",
                required=False,
            )
        )
        self.put_mapping(added, confirm_shrink=True)

        health = route_endpoint(self.app, "/control/v1/pvs/health", "GET")()
        self.assertEqual(health.summary.total, 1)
        self.assertEqual(health.items[0].signal, "quadrupole.q3.current")
        self.assertEqual(health.items[0].pv, "BL:Q3:ISET")

    def test_deleted_row_disappears_from_health_check(self) -> None:
        trimmed = config_with(
            entry(),
            entry(signal="steerer.x", label="X 偏转", pv="BL:STEER:X", unit="V"),
        )
        self.put_mapping(trimmed, confirm_shrink=True)

        health = route_endpoint(self.app, "/control/v1/pvs/health", "GET")()
        self.assertEqual(
            [item.signal for item in health.items],
            ["quadrupole.q1.current", "steerer.x"],
        )

    def test_sudden_shrink_is_refused_until_explicitly_confirmed(self) -> None:
        """把 128 条存成几条要显式确认：映射存残了会让没列出的设备全部失去映射。

        实测缘由：一个用例忘了把请求换成桩，直接向运行中的执行服务 PUT 了 3 条测试
        数据，把现场映射覆盖掉了（2026-09-16）。所以这条保护放在**服务端**，
        任何客户端（含脚本）都绕不过去。
        """
        tiny = config_with(entry())

        with self.assertRaises(HTTPException) as caught:
            self.put_mapping(tiny)

        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("不足一半", str(caught.exception.detail))
        self.assertIn("confirm_shrink", str(caught.exception.detail))
        # 没确认就不得换内存里的配置
        self.assertEqual(len(self.get_mapping().entries), 128)

        self.put_mapping(tiny, confirm_shrink=True)

        self.assertEqual(len(self.get_mapping().entries), 1)

    def test_small_mapping_is_not_guarded(self) -> None:
        """本来就只有几条时不套这条保护（单设备台架 / 小规模联调）。"""
        small = config_with(
            entry(), entry(signal="steerer.y", label="Y 偏转", pv="BL:STEER:Y", unit="V")
        )
        self.runtime.apply(small)

        self.put_mapping(config_with(entry()))

        self.assertEqual(len(self.get_mapping().entries), 1)

    def test_shrink_threshold_matches_the_policy(self) -> None:
        """阈值本身也钉住：现有条目少于 10 条不套；砍掉一半以上才拦。"""
        self.assertIsNone(pv_mapping.shrink_warning(128, 65))
        self.assertIsNotNone(pv_mapping.shrink_warning(128, 60))
        self.assertIsNone(pv_mapping.shrink_warning(9, 1))
        self.assertIsNone(pv_mapping.shrink_warning(10, 5))
        self.assertIsNotNone(pv_mapping.shrink_warning(10, 4))


class SettingsPageRoundTripTests(unittest.TestCase):
    """设置页「保存映射」的整链回归：真实默认映射走一遍表格，安全字段不能丢。

    报告 §7.1 把这条列为 P0 正确性问题：表格只显示 6 个常用字段，其余安全字段
    （分组/角色/回读配对/边界/最大单步/最大速率/稳定判据）必须按行合并回去。
    这里跑**真实默认映射**（128 条）而不是手写两条，避免只覆盖到某一类字段。
    """

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self._previous = {
            key: os.environ.get(key)
            for key in ("SPECTRUM_PV_MAPPING", "EPICS_CA_ADDR_LIST", "EPICS_CA_AUTO_ADDR_LIST")
        }
        os.environ["SPECTRUM_PV_MAPPING"] = str(
            Path(self._directory.name) / "pv_mapping.json"
        )
        os.environ["EPICS_CA_ADDR_LIST"] = "127.0.0.1"
        os.environ["EPICS_CA_AUTO_ADDR_LIST"] = "NO"
        self.runtime = InstrumentRuntime(pv_mapping.default_config())
        self.app_instance = create_app(self.runtime)
        self.get_mapping = route_endpoint(
            self.app_instance, "/control/v1/pv-mapping", "GET"
        )
        self.put_mapping = route_endpoint(
            self.app_instance, "/control/v1/pv-mapping", "PUT"
        )

    def tearDown(self) -> None:
        self.runtime.close()
        for key, value in self._previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._directory.cleanup()

    def make_page(self) -> SystemSettingsPage:
        settings = QSettings(
            str(Path(self._directory.name) / "settings.ini"),
            QSettings.Format.IniFormat,
        )
        return SystemSettingsPage(settings)

    def test_saving_the_untouched_mapping_changes_nothing(self) -> None:
        """操作员啥都不改就点保存：落盘结果必须与加载前**逐字段**一致。"""
        original = pv_mapping.default_config()
        page = self.make_page()
        page._render_pv_mapping(original.model_dump())

        self.put_mapping(PvMappingConfig.model_validate(page._collect_pv_mapping()))

        saved = self.get_mapping()
        self.assertEqual(len(saved.entries), len(original.entries))
        for before, after in zip(original.entries, saved.entries):
            self.assertEqual(after, before, before.signal)

    def test_safety_fields_survive_editing_a_visible_field(self) -> None:
        original = pv_mapping.default_config()
        page = self.make_page()
        page._render_pv_mapping(original.model_dump())
        page._set_pv_text(0, 2, "Part1:Flow_W:CS200A:Setpoint")

        self.put_mapping(PvMappingConfig.model_validate(page._collect_pv_mapping()))

        saved = self.get_mapping()
        self.assertEqual(saved.entries[0].pv, "Part1:Flow_W:CS200A:Setpoint")
        self.assertEqual(saved.entries[0].max_step, original.entries[0].max_step)
        self.assertEqual(saved.entries[0].settle_tol, original.entries[0].settle_tol)
        self.assertEqual(saved.entries[0].group, original.entries[0].group)
        self.assertEqual(saved.entries[0].role, original.entries[0].role)

    def test_service_does_not_backfill_missing_safety_fields(self) -> None:
        """确认服务端不会替客户端兜底：只发 6 个字段时安全参数确实会消失。

        这条是上面两个用例存在的理由——如果哪天服务端改成"缺字段就沿用旧值"，
        这里会失败，提醒可以把合并逻辑简化掉。
        """
        original = pv_mapping.default_config()
        trimmed = {
            "version": original.version,
            "entries": [
                {
                    key: value
                    for key, value in item.model_dump().items()
                    if key in ("label", "signal", "pv", "unit", "writable", "required")
                }
                for item in original.entries
            ],
        }

        self.put_mapping(PvMappingConfig.model_validate(trimmed))

        saved = self.get_mapping()
        self.assertEqual(saved.entries[0].max_step, None)
        self.assertEqual(saved.entries[0].group, "")
        self.assertNotEqual(saved, original)


if __name__ == "__main__":
    unittest.main()
