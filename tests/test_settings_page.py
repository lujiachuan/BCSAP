"""系统设置页的本地数据目录配置：默认值、校验、保存往返。

用例注入临时 INI 作为 QSettings：默认的注册表落在用户配置里，测试既不该
污染现场设置（受限环境下注册表写入还会被静默丢弃）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QPushButton

from apps.desktop_client.initialization import (
    CACHE_SETTINGS_KEY,
    InitializationWorker,
    cache_root_from_settings,
    default_cache_root,
)
from apps.desktop_client.pages import SystemSettingsPage
from apps.desktop_client.pages.settings import PV_SIGNAL_COLUMN

DEFAULT_SERVICE_URLS = {
    "data": "http://127.0.0.1:8000",
    "instrument": "http://127.0.0.1:8765",
}


class CacheDirSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings_path = self.root / "settings.ini"
        self.settings = QSettings(str(self.settings_path), QSettings.Format.IniFormat)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_page(self) -> SystemSettingsPage:
        return SystemSettingsPage(self.settings)

    def test_first_open_shows_the_default_directory(self) -> None:
        """没配置过也要有落点：输入框里填的就是默认目录。"""
        page = self.make_page()

        self.assertEqual(page.cache_root.text(), str(default_cache_root()))
        self.assertEqual(cache_root_from_settings(self.settings), default_cache_root())

    def test_saving_creates_the_directory_and_persists_it(self) -> None:
        page = self.make_page()
        chosen = self.root / "谱图数据" / "client_cache"
        page.cache_root.setText(str(chosen))

        page._save_service_urls()

        self.assertTrue(chosen.is_dir())
        self.assertEqual(page.feedback_label.property("state"), "good")
        self.assertIn(str(chosen), page.feedback_label.text())

        reopened = self.make_page()
        self.assertEqual(reopened.cache_root.text(), str(chosen))
        self.assertEqual(cache_root_from_settings(self.settings), chosen)

    def test_relative_path_is_rejected(self) -> None:
        page = self.make_page()
        page.cache_root.setText("client_cache")

        page._save_service_urls()

        self.assertEqual(page.feedback_label.property("state"), "error")
        self.assertIn("绝对路径", page.feedback_label.text())
        self.assertFalse((Path.cwd() / "client_cache").exists())

    def test_empty_path_is_rejected(self) -> None:
        page = self.make_page()
        page.cache_root.setText("   ")

        page._save_service_urls()

        self.assertEqual(page.feedback_label.property("state"), "error")
        self.assertIn("不能为空", page.feedback_label.text())

    def test_path_taken_by_a_file_is_rejected_with_the_directory_name(self) -> None:
        page = self.make_page()
        blocker = self.root / "occupied"
        blocker.write_text("not a directory", encoding="utf-8")
        page.cache_root.setText(str(blocker))

        page._save_service_urls()

        self.assertEqual(page.feedback_label.property("state"), "error")
        self.assertIn(str(blocker), page.feedback_label.text())

    def test_service_urls_still_save_when_the_directory_is_bad(self) -> None:
        """地址与目录同一个保存按钮：目录不通过不能连带地址都存不下去。"""
        page = self.make_page()
        blocker = self.root / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        page.cache_root.setText(str(blocker))

        page._save_service_urls()

        self.assertEqual(page.feedback_label.property("state"), "error")
        self.assertIn("服务地址已保存", page.feedback_label.text())
        self.assertEqual(
            str(self.settings.value("service/dataUrl")), DEFAULT_SERVICE_URLS["data"]
        )
        self.assertEqual(
            str(self.settings.value("service/instrumentUrl")),
            DEFAULT_SERVICE_URLS["instrument"],
        )
        # 坏目录不写进偏好：下次启动仍用上一个可用目录
        self.assertEqual(cache_root_from_settings(self.settings), default_cache_root())

    def test_use_default_directory_button_fills_the_default_back(self) -> None:
        page = self.make_page()
        page.cache_root.setText(str(self.root / "elsewhere"))

        page._use_default_cache_root()

        self.assertEqual(page.cache_root.text(), str(default_cache_root()))

    def test_field_and_buttons_share_the_row_without_overlapping(self) -> None:
        """默认目录很长，输入框不能被三个按钮挤没（实测 1500px 下留 914px）。"""
        page = self.make_page()
        page.resize(1500, 780)
        page.show()
        self.app.processEvents()
        page.settings_stack.setCurrentIndex(0)
        self.app.processEvents()

        buttons = page.cache_root.parentWidget().findChildren(QPushButton)

        self.assertEqual(
            [button.text() for button in buttons], ["浏览…", "打开目录", "用默认目录"]
        )
        self.assertGreaterEqual(page.cache_root.width(), 200)
        for button in buttons:
            self.assertFalse(page.cache_root.geometry().intersects(button.geometry()))
        page.hide()

    def test_saved_directory_is_used_by_the_initialization_worker(self) -> None:
        """设置里选的目录要真的传给初始化任务，而不是只写进偏好。"""
        chosen = self.root / "chosen"
        self.settings.setValue(CACHE_SETTINGS_KEY, str(chosen))
        self.settings.sync()

        worker = InitializationWorker(
            "http://data",
            "http://instrument",
            None,
            cache_root=cache_root_from_settings(self.settings),
        )

        self.assertEqual(worker.cache_root, chosen)


def mapping_entry(signal: str, **overrides: object) -> dict:
    """一条带完整安全字段的映射（形状与执行服务 GET 返回的一致）。"""
    entry = {
        "signal": signal,
        "label": signal,
        "pv": "PV:" + signal,
        "unit": "sccm",
        "writable": True,
        "required": True,
        "group": "气体流量",
        "readback_signal": signal.replace("_setpoint", "_readback"),
        "rate_signal": "",
        "role": "setpoint",
        "tunable": True,
        "beam_target": False,
        "min_value": 0.0,
        "max_value": 500.0,
        "max_step": 10.0,
        "max_rate": 5.0,
        "settle_tol": 2.0,
        "settle_timeout": 30.0,
    }
    entry.update(overrides)
    return entry


SAFETY_FIELDS = (
    "group",
    "role",
    "readback_signal",
    "rate_signal",
    "tunable",
    "beam_target",
    "min_value",
    "max_value",
    "max_step",
    "max_rate",
    "settle_tol",
    "settle_timeout",
)


class PvMappingSafetyFieldTests(CacheDirSettingsTests):
    """保存 PV 映射不能丢掉界面没显示的**安全字段**。

    契约里这些字段都有默认值，缺字段不会报错——所以"只按表格重建条目"的写法
    会静默清空写入边界、单步/速率保护和稳定判据，分组与角色丢失还会让手动页控件
    退化、调束因缺 max_step 拒绝启动。
    """

    def loaded_page(self, *entries: dict) -> SystemSettingsPage:
        page = self.make_page()
        page._render_pv_mapping({"version": 1, "entries": list(entries)})
        return page

    def test_visible_only_save_keeps_every_safety_field(self) -> None:
        original = mapping_entry("gas.ar.flow_setpoint")
        page = self.loaded_page(original)

        collected = page._collect_pv_mapping()["entries"][0]

        for field in SAFETY_FIELDS:
            self.assertEqual(collected[field], original[field], field)

    def test_editing_a_displayed_field_keeps_the_hidden_ones(self) -> None:
        page = self.loaded_page(mapping_entry("gas.ar.flow_setpoint"))
        page._set_pv_text(0, 2, "Part1:Flow_W:CS200A:Setpoint")  # 改 PV 名
        page._set_pv_flag(0, 4, False)  # 改成只读

        collected = page._collect_pv_mapping()["entries"][0]

        self.assertEqual(collected["pv"], "Part1:Flow_W:CS200A:Setpoint")
        self.assertFalse(collected["writable"])
        self.assertEqual(collected["max_step"], 10.0)
        self.assertEqual(collected["settle_tol"], 2.0)
        self.assertEqual(collected["readback_signal"], "gas.ar.flow_readback")

    def test_the_payload_only_carries_contract_fields(self) -> None:
        """合并后的条目必须是契约字段，不能把界面控件带出的额外键发上去。"""
        from packages.contracts import PvMappingEntry

        page = self.loaded_page(mapping_entry("gas.ar.flow_setpoint"))

        collected = page._collect_pv_mapping()["entries"][0]

        self.assertEqual(set(collected), set(PvMappingEntry.model_fields))

    def test_renaming_the_signal_drops_the_stale_readback_pairing(self) -> None:
        """业务信号就是行身份：改名后旧的回读配对会指向别的通道，必须清空。"""
        page = self.loaded_page(mapping_entry("gas.ar.flow_setpoint"))
        page._set_pv_text(0, PV_SIGNAL_COLUMN, "gas.ne.flow_setpoint")

        collected = page._collect_pv_mapping()["entries"][0]

        self.assertEqual(collected["signal"], "gas.ne.flow_setpoint")
        self.assertEqual(collected["readback_signal"], "")
        self.assertEqual(collected["group"], "气体流量")
        self.assertEqual(collected["max_value"], 500.0)

    def test_editing_the_signal_cell_in_place_keeps_the_hidden_fields(self) -> None:
        """操作员在表格里直接改单元格（编辑器写回原 item）也要留住安全字段。"""
        page = self.loaded_page(mapping_entry("gas.ar.flow_setpoint"))
        page.pv_table.item(0, PV_SIGNAL_COLUMN).setText("gas.ne.flow_setpoint")

        collected = page._collect_pv_mapping()["entries"][0]

        self.assertEqual(collected["readback_signal"], "")
        self.assertEqual(collected["max_step"], 10.0)
        self.assertEqual(collected["settle_timeout"], 30.0)

    def test_configured_rate_signal_survives_editing_a_visible_field(self) -> None:
        """磁铁的速率配对（`CurrentRateSet`）同样不能因为保存而丢——丢了以后
        成组回落就静默不下发速率，只按 max_step 慢慢爬。"""
        page = self.loaded_page(
            mapping_entry(
                "magnet.m1.current_setpoint",
                rate_signal="magnet.m1.current_rate_setpoint",
                readback_signal="magnet.m1.current_readback",
            )
        )
        page._set_pv_text(0, 2, "BD:DipoleMagnet:01:CurrentSet")

        collected = page._collect_pv_mapping()["entries"][0]

        self.assertEqual(collected["rate_signal"], "magnet.m1.current_rate_setpoint")
        self.assertEqual(collected["readback_signal"], "magnet.m1.current_readback")

    def test_new_rows_carry_no_safety_fields(self) -> None:
        page = self.make_page()
        page._add_pv_row()
        page._set_pv_text(0, PV_SIGNAL_COLUMN, "quadrupole.q3.current")
        page._set_pv_text(0, 2, "BL:Q3:ISET")

        collected = page._collect_pv_mapping()["entries"][0]

        self.assertEqual(
            set(collected), {"label", "signal", "pv", "unit", "writable", "required"}
        )

    def test_hover_shows_the_hidden_fields_and_survives_clear_marks(self) -> None:
        page = self.loaded_page(mapping_entry("gas.ar.flow_setpoint"))

        tooltip = page.pv_table.item(0, PV_SIGNAL_COLUMN).toolTip()
        self.assertIn("分组：气体流量", tooltip)
        self.assertIn("回读配对：gas.ar.flow_readback", tooltip)
        self.assertIn("边界：0 ~ 500 sccm", tooltip)
        self.assertIn("单步上限：10", tooltip)
        self.assertIn("稳定判据：2 / 30 s", tooltip)

        page._clear_pv_row_marks(0)

        self.assertIn("分组：气体流量", page.pv_table.item(0, PV_SIGNAL_COLUMN).toolTip())

    def test_row_marks_restore_the_safety_tooltip_after_an_error(self) -> None:
        page = self.loaded_page(mapping_entry("gas.ar.flow_setpoint"))
        page._mark_pv_issues(
            [{"index": 0, "field": "signal", "message": "业务信号重复"}]
        )

        self.assertEqual(page.pv_table.item(0, PV_SIGNAL_COLUMN).toolTip(), "业务信号重复")

        page._clear_all_pv_marks()

        self.assertIn("分组：气体流量", page.pv_table.item(0, PV_SIGNAL_COLUMN).toolTip())


if __name__ == "__main__":
    unittest.main()
