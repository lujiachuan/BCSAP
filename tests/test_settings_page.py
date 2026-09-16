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

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtWidgets import QApplication, QPushButton

from apps.desktop_client import instrument_api
from apps.desktop_client.initialization import (
    CACHE_SETTINGS_KEY,
    InitializationWorker,
    cache_root_from_settings,
    default_cache_root,
)
from apps.desktop_client.pages import SystemSettingsPage
from apps.desktop_client.pages import settings as settings_module
from apps.desktop_client.pages.settings import (
    PV_PROBE_TIMEOUT_S,
    PV_SIGNAL_COLUMN,
    PV_STATUS_COLUMN,
    PV_VALUE_COLUMN,
)

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
        # 设置页自己就会发映射请求（显示时拉一次、保存时 PUT）：测试里一律换成假线程。
        # **不换就会真的打到 127.0.0.1:8765**——2026-09-16 踩过：一条新用例忘了换，
        # 把现场 128 条映射覆盖成了 3 条测试数据。tests/conftest.py 另有一道护栏，
        # 这里换桩是"不该发生的事根本不发生"。
        self.mapping_requests: list[dict] = []
        original = settings_module.PvMappingRequestThread

        def fake_thread(base_url, payload=None, confirm_shrink=False):
            self.mapping_requests.append(
                {
                    "base_url": base_url,
                    "payload": payload,
                    "confirm_shrink": confirm_shrink,
                }
            )
            return _NeverEnds()

        settings_module.PvMappingRequestThread = fake_thread
        self.addCleanup(
            setattr, settings_module, "PvMappingRequestThread", original
        )

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


class PvProbeTests(CacheDirSettingsTests):
    """PV 映射页的连接状态 / 当前值 / 查找 / 筛选（现场要求：看得见每一路通不通）。

    关键约束：检测结果只读、不参与保存；**某一路连不上不该让整张表看起来不可用**，
    失败的批次也不能替操作员猜"是连不上还是没读到"。
    """

    def setUp(self) -> None:
        super().setUp()
        self.reads: list[dict] = []

    def loaded_page(self, *entries: dict) -> SystemSettingsPage:
        page = self.make_page()
        self.stub_reads(page)
        page._render_pv_mapping({"version": 1, "entries": list(entries)})
        return page

    def stub_reads(self, page: SystemSettingsPage) -> None:
        """把读请求换成记录参数、不联网的桩。"""
        original = instrument_api.request_read

        def fake_read(*_args, **kwargs):
            self.reads.append(dict(kwargs))
            return _NeverEnds()

        instrument_api.request_read = fake_read
        self.addCleanup(setattr, instrument_api, "request_read", original)

    def three_entries(self) -> list[dict]:
        return [
            mapping_entry("gas.ar.flow_setpoint"),
            mapping_entry("magnet.m1.current_setpoint", required=False),
            mapping_entry(
                "detector.fc1.beam_current", writable=False, required=True, unit="nA"
            ),
        ]

    # ---------------- 列与"不参与保存" ----------------
    def test_probe_columns_are_present_and_read_only(self) -> None:
        page = self.loaded_page(*self.three_entries())

        headers = [
            page.pv_table.horizontalHeaderItem(index).text()
            for index in range(page.pv_table.columnCount())
        ]
        self.assertEqual(headers[-2:], ["连接", "当前值"])
        for row in range(page.pv_table.rowCount()):
            for column in (PV_STATUS_COLUMN, PV_VALUE_COLUMN):
                item = page.pv_table.item(row, column)
                self.assertFalse(
                    item.flags() & Qt.ItemFlag.ItemIsEditable, f"{row}/{column} 不该可编辑"
                )

    def test_probe_columns_never_reach_the_saved_payload(self) -> None:
        page = self.loaded_page(*self.three_entries())
        page._probe_pvs()
        page._on_pv_probe(
            {
                "ok": True,
                "payload": {
                    "readings": [
                        {"signal": "gas.ar.flow_setpoint", "value": 100.0,
                         "unit": "sccm", "connected": True},
                    ]
                },
            }
        )

        entries = page._collect_pv_mapping()["entries"]

        for entry in entries:
            self.assertNotIn("连接", entry)
            self.assertNotIn("当前值", entry)
            self.assertNotIn("status", entry)
            self.assertNotIn("value", entry)

    # ---------------- 检测行为 ----------------
    def test_probe_only_asks_for_visible_rows(self) -> None:
        """上百路全读在真机上要好几秒：先筛再读，只读当前可见行。"""
        page = self.loaded_page(*self.three_entries())

        page.pv_filter.setCurrentIndex(1)  # 只看未连接 → 还没测过，一行都不显示
        page._probe_pvs()
        self.assertEqual(self.reads, [], "没有可见行时不该发请求")

        page.pv_search.setText("magnet")
        page.pv_filter.setCurrentIndex(0)
        page._probe_pvs()

        self.assertEqual(len(self.reads), 1)
        self.assertEqual(self.reads[0]["signals"], ["magnet.m1.current_setpoint"])
        self.assertEqual(self.reads[0]["timeout"], PV_PROBE_TIMEOUT_S)

    def test_probe_writes_status_value_and_reason_per_row(self) -> None:
        page = self.loaded_page(*self.three_entries())
        page._probe_pvs()

        page._on_pv_probe(
            {
                "ok": True,
                "payload": {
                    "readings": [
                        {"signal": "gas.ar.flow_setpoint", "value": 120.5,
                         "unit": "sccm", "connected": True},
                        {"signal": "magnet.m1.current_setpoint", "value": None,
                         "unit": "A", "connected": False, "detail": "PV 未连接"},
                        # detector 这一路服务没返回：不能当成"没连上"，要标"未测"
                    ]
                },
            }
        )

        self.assertEqual(page._pv_text(0, PV_STATUS_COLUMN), "已连接")
        self.assertIn("120.5", page._pv_text(0, PV_VALUE_COLUMN))
        self.assertEqual(page._pv_text(1, PV_STATUS_COLUMN), "未连接")
        self.assertEqual(page._pv_text(1, PV_VALUE_COLUMN), "—")
        self.assertIn("PV 未连接", page.pv_table.item(1, PV_STATUS_COLUMN).toolTip())
        self.assertEqual(page._pv_text(2, PV_STATUS_COLUMN), "未测")
        # 汇总与提示：一路连不上不代表整张表不能用；"未测"要单独计数
        self.assertIn("1 已连接", page.pv_summary.text())
        self.assertIn("1 未连接", page.pv_summary.text())
        self.assertIn("未测 1 行", page.pv_summary.text())
        self.assertIn("不影响扫谱与调束", page.pv_feedback.text())

    def test_probe_failure_marks_rows_untested_instead_of_guessing(self) -> None:
        page = self.loaded_page(*self.three_entries())
        page._probe_pvs()

        page._on_pv_probe({"ok": False, "message": "连接被拒绝"})

        for row in range(3):
            self.assertEqual(page._pv_text(row, PV_STATUS_COLUMN), "未测")
        self.assertEqual(page.pv_feedback.property("state"), "error")
        self.assertIn("连接被拒绝", page.pv_feedback.text())

    def test_render_resets_probe_cells(self) -> None:
        """重新载入映射后旧的"已连接/当前值"不再成立，必须清掉。"""
        page = self.loaded_page(*self.three_entries())
        page._probe_pvs()
        page._on_pv_probe(
            {
                "ok": True,
                "payload": {
                    "readings": [
                        {"signal": "gas.ar.flow_setpoint", "value": 1.0,
                         "unit": "sccm", "connected": True},
                    ]
                },
            }
        )

        page._render_pv_mapping({"version": 2, "entries": [mapping_entry("gas.he.flow_setpoint")]})

        self.assertEqual(page._pv_text(0, PV_STATUS_COLUMN), "")
        self.assertEqual(page._pv_text(0, PV_VALUE_COLUMN), "")

    def test_saving_reloads_and_reprobes(self) -> None:
        page = self.loaded_page(*self.three_entries())
        page._save_pv_mapping()

        page._finish_pv_request(
            {"ok": True, "config": {"version": 1, "entries": self.three_entries()},
             "message": "", "issues": []}
        )

        self.assertEqual(len(self.reads), 1, "保存之后应自动重测一次")

    def test_double_click_probes_only_that_row(self) -> None:
        page = self.loaded_page(*self.three_entries())

        page._on_pv_cell_double_clicked(1, PV_SIGNAL_COLUMN)

        self.assertEqual(self.reads[0]["signals"], ["magnet.m1.current_setpoint"])

    # ---------------- 查找与筛选 ----------------
    def test_search_matches_label_signal_and_pv(self) -> None:
        page = self.loaded_page(
            mapping_entry("gas.ar.flow_setpoint", label="Ar 流量设定", pv="Part1:Flow:Ar:SP"),
            mapping_entry("magnet.m1.current_setpoint", label="磁铁1 电流设定",
                          pv="BD:DipoleMagnet:01:CurrentSet"),
        )

        def visible() -> list[int]:
            return [
                row
                for row in range(page.pv_table.rowCount())
                if not page.pv_table.isRowHidden(row)
            ]

        page.pv_search.setText("ar 流量")
        self.assertEqual(visible(), [0], "按设备参数（标题）匹配")
        page.pv_search.setText("DipoleMagnet")
        self.assertEqual(visible(), [1], "按 PV 名匹配")
        page.pv_search.setText("gas.ar")
        self.assertEqual(visible(), [0], "按业务信号匹配")
        page.pv_search.setText("")
        self.assertEqual(visible(), [0, 1], "清空后全显示")

    def test_filter_only_unconnected(self) -> None:
        page = self.loaded_page(*self.three_entries())
        page._probe_pvs()  # 先发一次检测：结果只会写进"本次检测的行"
        page._on_pv_probe(
            {
                "ok": True,
                "payload": {
                    "readings": [
                        {"signal": "gas.ar.flow_setpoint", "value": 1.0,
                         "unit": "sccm", "connected": True},
                        {"signal": "magnet.m1.current_setpoint", "value": None,
                         "unit": "A", "connected": False, "detail": "PV 未连接"},
                        {"signal": "detector.fc1.beam_current", "value": 8.3,
                         "unit": "nA", "connected": True},
                    ]
                },
            }
        )

        page.pv_filter.setCurrentIndex(1)  # 只看未连接

        hidden = [
            row for row in range(page.pv_table.rowCount())
            if page.pv_table.isRowHidden(row)
        ]
        self.assertEqual(hidden, [0, 2])

    def test_filter_only_required_follows_the_flag(self) -> None:
        page = self.loaded_page(*self.three_entries())

        page.pv_filter.setCurrentIndex(3)  # 只看必需

        visible = [
            row for row in range(page.pv_table.rowCount())
            if not page.pv_table.isRowHidden(row)
        ]
        self.assertEqual(visible, [0, 2])

    def test_copy_selected_rows_puts_three_columns_on_the_clipboard(self) -> None:
        page = self.loaded_page(*self.three_entries())
        page.pv_table.selectRow(1)

        page._copy_pv_rows()

        text = QApplication.clipboard().text()
        self.assertIn("magnet.m1.current_setpoint", text)
        self.assertIn("PV:magnet.m1.current_setpoint", text)

    def test_copy_without_selection_only_reports(self) -> None:
        page = self.loaded_page(*self.three_entries())
        page.pv_table.clearSelection()

        page._copy_pv_rows()

        self.assertIn("先选中", page.pv_feedback.text())

    # ---------------- 条目数骤减的拦截 ----------------
    def many_entries(self, count: int = 12) -> list[dict]:
        return [mapping_entry(f"gas.s{index}.flow_setpoint") for index in range(count)]

    def test_saving_a_truncated_table_asks_once_before_writing(self) -> None:
        """整份映射存成一小撮是最容易犯且后果最重的错：先提醒，再点一次才存。

        实测缘由见 tests/conftest.py 顶部注释（一条用例真把 128 条存成了 3 条）。
        """
        page = self.loaded_page(*self.many_entries(12))
        for row in reversed(range(5, 12)):  # 12 → 5 条，不足一半（倒序删，否则索引会移位）
            page.pv_table.removeRow(row)

        page._save_pv_mapping()  # 第一次：只提醒

        self.assertEqual(self.mapping_requests, [], "第一次不该真的发请求")
        self.assertEqual(page.pv_feedback.property("state"), "warn")
        self.assertIn("不足一半", page.pv_feedback.text())
        self.assertIn("再点一次", page.pv_feedback.text())

        page._save_pv_mapping()  # 第二次：带着确认去存

        self.assertEqual(len(self.mapping_requests), 1)
        self.assertTrue(self.mapping_requests[0]["confirm_shrink"])
        self.assertEqual(len(self.mapping_requests[0]["payload"]["entries"]), 5)

    def test_normal_shrink_does_not_ask(self) -> None:
        """从 12 条改成 8 条是正常的删减，不该拿确认框烦人。"""
        page = self.loaded_page(*self.many_entries(12))
        for row in reversed(range(8, 12)):  # 12 → 8 条
            page.pv_table.removeRow(row)

        page._save_pv_mapping()

        self.assertEqual(len(self.mapping_requests), 1)
        self.assertFalse(self.mapping_requests[0]["confirm_shrink"])

    def test_tiny_mapping_is_not_guarded(self) -> None:
        """本来就只有几行时（现场小规模联调）不该套这条。"""
        page = self.loaded_page(*self.three_entries())
        for row in reversed(range(1, 3)):  # 3 → 1 条
            page.pv_table.removeRow(row)

        page._save_pv_mapping()

        self.assertEqual(len(self.mapping_requests), 1)
        self.assertFalse(self.mapping_requests[0]["confirm_shrink"])

    def test_confirmation_state_resets_after_a_successful_save(self) -> None:
        """确认只对那一次生效：存完再裁一次仍要重新确认。"""
        page = self.loaded_page(*self.many_entries(24))
        for row in reversed(range(10, 24)):  # 24 → 10 条
            page.pv_table.removeRow(row)
        page._save_pv_mapping()
        page._save_pv_mapping()
        self.assertEqual(len(self.mapping_requests), 1)

        page._finish_pv_request(
            {
                "ok": True,
                "config": {"version": 1, "entries": self.many_entries(10)},
                "message": "",
                "issues": [],
            }
        )
        for row in reversed(range(4, 10)):  # 10 → 4 条，又不足一半
            page.pv_table.removeRow(row)
        page._save_pv_mapping()

        self.assertEqual(len(self.mapping_requests), 1, "确认过的那次不该被复用")
        self.assertIn("不足一半", page.pv_feedback.text())


class PvProbeServicePayloadTests(CacheDirSettingsTests):
    """用**真服务端**（内存网关 → FastAPI 路由）返回的 payload 渲染页面。

    手写读数容易"照着页面实现写"，这条走真实契约：`POST /control/v1/signals/read`
    返回什么，页面就得能显示成什么（字段名、单位、connected 语义一变就红）。
    """

    def test_real_service_payload_renders_status_and_value(self) -> None:
        import tempfile
        from pathlib import Path

        from apps.instrument_service.app import create_app
        from apps.instrument_service.pv_health import create_simulated_gateway
        from apps.instrument_service.runtime import InstrumentRuntime
        from apps.instrument_service.scan_store import ScanStore
        from packages.contracts import PvMappingConfig, SignalSnapshotRequest
        from tests.test_signal_io import route_endpoint

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = PvMappingConfig.model_validate(
            {
                "version": 1,
                "entries": [
                    {
                        "signal": "gas.ar.flow_setpoint",
                        "label": "Ar 流量设定",
                        "pv": "Part1:Flow_S:CS200A:Setpoint",
                        "unit": "sccm",
                        "writable": True,
                        "required": True,
                        "min_value": 0.0,
                        "max_value": 500.0,
                        "max_step": 50.0,
                        "readback_signal": "gas.ar.flow_readback",
                    },
                    {
                        "signal": "gas.ar.flow_readback",
                        "label": "Ar 瞬时流量",
                        "pv": "Part1:Flow_R:CS200A:InstantSCCM",
                        "unit": "sccm",
                        "writable": False,
                        "required": True,
                    },
                ],
            }
        )
        runtime = InstrumentRuntime(
            config,
            gateway_factory=create_simulated_gateway,
            store=ScanStore(Path(directory.name)),
        )
        self.addCleanup(runtime.close)
        endpoint = route_endpoint(create_app(runtime), "/control/v1/signals/read", "POST")
        snapshot = endpoint(
            SignalSnapshotRequest(
                signals=["gas.ar.flow_setpoint", "gas.ar.flow_readback"]
            )
        ).model_dump()

        page = self.make_page()
        original = instrument_api.request_read
        instrument_api.request_read = lambda *a, **k: _NeverEnds()
        self.addCleanup(setattr, instrument_api, "request_read", original)
        page._render_pv_mapping({"version": 1, "entries": config.model_dump()["entries"]})
        page._probe_pvs()

        page._on_pv_probe({"ok": True, "payload": snapshot})

        self.assertEqual(page._pv_text(0, PV_STATUS_COLUMN), "已连接")
        self.assertEqual(page._pv_text(1, PV_STATUS_COLUMN), "已连接")
        # 值来自模拟网关的种子值，带映射里的单位
        self.assertIn("sccm", page._pv_text(0, PV_VALUE_COLUMN))
        self.assertIn("2 已连接", page.pv_summary.text())


class _NeverEnds(QObject):
    """假请求线程：connect 之后不回调（检测结果由测试直接喂给页面）。"""

    completed = Signal(object)
    finished = Signal()

    def start(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
