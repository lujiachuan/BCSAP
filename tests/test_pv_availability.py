"""PV 可达性与控制页入口策略。

现场反馈（2026-09-14）：**不能因为一路 PV 没连上就把扫谱/调束入口锁掉**——现场上百路
PV 很难保证全可达，而每次操作只用得到自己那几路。这里把新口径钉住：

* 入口只由**仪器执行服务是否可达**决定；
* PV 连不全是**提示**（状态卡、初始化步骤、侧栏提示、设置页逐行明细），不是拦路；
* 缺到本次要用的那几路时，由执行层在启动时点名拒绝（那条链路另有测试）。
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QApplication

from apps.desktop_client import initialization as initialization_module
from apps.desktop_client import instrument_api
from apps.desktop_client.pages import manual as manual_module
from apps.desktop_client.pages import settings as settings_module
from apps.desktop_client.pages import tuning as tuning_module
from apps.desktop_client.pages.workbench import WorkbenchPage


class _FakeThread(QObject):
    """假请求线程：不联网、不回调（页面构造期的请求全部换成它）。"""

    completed = Signal(object)
    finished = Signal()

    def start(self) -> None:
        return None

    def isRunning(self) -> bool:
        return False

    def deleteLater(self) -> None:
        return None


def _fake_thread(*_args, **_kwargs) -> _FakeThread:
    return _FakeThread()


class InitializationToleranceTests(unittest.TestCase):
    """初始化检查：PV 没连全不判失败，只报 warn。"""

    def setUp(self) -> None:
        self.original = initialization_module._request_json
        self.addCleanup(
            setattr, initialization_module, "_request_json", self.original
        )

    def run_check(self, health: dict) -> tuple[bool, list[tuple[str, str, str]]]:
        steps: list[tuple[str, str, str]] = []
        services: list[tuple[str, str, str]] = []
        pv_status: list[tuple] = []

        def fake_request(url: str, timeout: float = 3.0):
            if url.endswith("/control/v1/status"):
                return {"service": "instrument-service", "read_only": False}
            if url.endswith("/control/v1/pvs/health"):
                return health
            raise AssertionError(f"未预期的请求：{url}")

        initialization_module._request_json = fake_request
        worker = initialization_module.InitializationWorker(
            "http://127.0.0.1:1", "http://127.0.0.1:8767", targets=("instrument",)
        )
        worker.stepChanged.connect(lambda k, s, t: steps.append((k, s, t)))
        worker.serviceChanged.connect(lambda k, s, t: services.append((k, s, t)))
        worker.pvStatus.connect(lambda *args: pv_status.append(args))

        reachable = worker._check_instrument()
        return reachable, steps, services, pv_status

    def test_missing_required_pvs_do_not_fail_the_startup_check(self) -> None:
        reachable, steps, services, pv_status = self.run_check(
            {
                "status": "unavailable",
                "summary": {
                    "total": 128,
                    "connected": 120,
                    "required_failed": 2,
                    "optional_failed": 6,
                },
                "items": [
                    {
                        "signal": "vacuum.chamber_pressure",
                        "pv": "Part1:Sputtering",
                        "required": True,
                        "connected": False,
                        "detail": "PV 未连接",
                    }
                ],
            }
        )

        self.assertTrue(reachable, "服务可达就该继续启动，不能因为 PV 没连全就整体失败")
        pv_step = [item for item in steps if item[0] == "pv"]
        self.assertEqual(pv_step[-1][1], "warn", "PV 问题报 warn，不报 error")
        self.assertIn("120 / 128", pv_step[-1][2])
        self.assertIn("2 路标记为必需未连接", pv_step[-1][2])
        epics = [item for item in services if item[0] == "epics"]
        self.assertEqual(epics[-1][1], "warn")
        self.assertEqual(pv_status[-1][0], 120)
        self.assertEqual(pv_status[-1][3], 2, "必需未连接数要带给界面")

    def test_all_pvs_connected_is_clean(self) -> None:
        reachable, steps, services, _pv = self.run_check(
            {
                "status": "ready",
                "summary": {
                    "total": 128,
                    "connected": 128,
                    "required_failed": 0,
                    "optional_failed": 0,
                },
                "items": [],
            }
        )

        self.assertTrue(reachable)
        self.assertEqual([item for item in steps if item[0] == "pv"][-1][1], "good")
        self.assertEqual([item for item in services if item[0] == "epics"][-1][1], "good")

    def test_health_request_failure_still_allows_control(self) -> None:
        """健康检查本身发不出去（服务半死/超时）也不锁入口：那是"看不到状态"，
        不是"设备不可控制"——扫谱/调束启动时仍会按名字校验每一路。"""
        steps: list[tuple[str, str, str]] = []

        def fake_request(url: str, timeout: float = 3.0):
            if url.endswith("/control/v1/status"):
                return {"service": "instrument-service", "read_only": False}
            raise TimeoutError("读超时")

        initialization_module._request_json = fake_request
        worker = initialization_module.InitializationWorker(
            "http://127.0.0.1:1", "http://127.0.0.1:8767", targets=("instrument",)
        )
        worker.stepChanged.connect(lambda k, s, t: steps.append((k, s, t)))

        reachable = worker._check_instrument()

        self.assertTrue(reachable)
        pv_step = [item for item in steps if item[0] == "pv"][-1]
        self.assertEqual(pv_step[1], "error")
        self.assertIn("检查失败", pv_step[2])
        instrument_step = [item for item in steps if item[0] == "instrument"][-1]
        self.assertEqual(instrument_step[1], "good")


class WorkbenchPvWordingTests(unittest.TestCase):
    """工作台文案：PV 没连全是"提示"，不是"暂不可执行"。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def make_page(self) -> WorkbenchPage:
        page = WorkbenchPage()
        self.addCleanup(page.deleteLater)
        return page

    def test_partial_pv_shows_a_hint_but_keeps_the_buttons(self) -> None:
        page = self.make_page()
        page.set_service_status("instrument", "good", "就绪")
        page.set_service_status("epics", "warn", "120 / 128")

        page.set_control_enabled(True)

        self.assertTrue(page.scan_button.isEnabled())
        self.assertTrue(page.tuning_button.isEnabled())
        self.assertIn("部分 PV 未连接", page.hero_title.text())
        self.assertIn("点名拒绝", page.hero_caption.text())
        self.assertIn("部分 PV 未连接", page.device_status.text())

    def test_instrument_unreachable_still_blocks(self) -> None:
        page = self.make_page()
        page.set_service_status("instrument", "error", "不可达")
        page.set_service_status("epics", "warn", "0 / 128")

        page.set_control_enabled(False)

        self.assertFalse(page.scan_button.isEnabled())
        self.assertFalse(page.tuning_button.isEnabled())
        self.assertIn("暂不可执行扫谱与调束", page.hero_title.text())
        self.assertIn("仪器执行服务未就绪", page.hero_caption.text())
        self.assertIn("仪器执行服务未就绪", page.scan_button.toolTip())

    def test_everything_connected_keeps_the_old_wording(self) -> None:
        page = self.make_page()
        page.set_service_status("instrument", "good", "就绪")
        page.set_service_status("epics", "good", "128 / 128")

        page.set_control_enabled(True)

        self.assertEqual(page.hero_title.text(), "设备与关键服务就绪")
        self.assertEqual(page.device_status.text(), "●  设备就绪")


class NavigationEligibilityTests(unittest.TestCase):
    """整窗级：PV 未连接时扫谱/调束入口必须仍然可点。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        import apps.desktop_client.main as main_module

        self.main_module = main_module
        # 屏蔽真实初始化与页面构造期的联网请求（与 tools/ui_snapshot.py 同一套做法）
        self._patch(main_module.MainWindow, "_start_initialization", lambda self: None)
        for module in (manual_module, tuning_module, settings_module):
            self._patch(module, "PvMappingRequestThread", _fake_thread)
        for name in (
            "request_read",
            "request_tuning_catalog",
            "request_scan_status",
            "request_scan_points",
            "request_pv_health",
        ):
            if hasattr(instrument_api, name):
                self._patch(instrument_api, name, _fake_thread)

    def _patch(self, target, name: str, replacement) -> None:
        original = getattr(target, name)
        setattr(target, name, replacement)
        self.addCleanup(setattr, target, name, original)

    def make_window(self):
        window = self.main_module.MainWindow()
        self.addCleanup(window.deleteLater)
        return window

    def eligible(self, window, key: str) -> bool:
        row = window._row_of_key[key]
        item = window.navigation.item(row)
        return bool(item.flags() & Qt.ItemFlag.ItemIsEnabled)

    def test_pages_stay_clickable_when_some_pvs_are_missing(self) -> None:
        window = self.make_window()
        window._model.set_service("instrument", "good", "就绪")
        window._model.set_service("epics", "warn", "120 / 128")
        window._model.set_pv(120, 128, ["Part1:Sputtering：未连接"], required_failed=2)

        window._apply_control_eligibility()

        for key in ("manual", "scan", "tuning"):
            self.assertTrue(self.eligible(window, key), f"{key} 不该因为 PV 未连全被锁")
        tooltip = window.navigation.item(window._row_of_key["scan"]).toolTip()
        self.assertIn("受控 PV", tooltip, "提示里要说清是 PV 连接问题")

    def test_pages_locked_only_when_the_service_is_unreachable(self) -> None:
        window = self.make_window()
        window._model.set_service("instrument", "error", "不可达")
        window._model.set_service("epics", "idle", "未检查")

        window._apply_control_eligibility()

        for key in ("manual", "scan", "tuning"):
            self.assertFalse(self.eligible(window, key))
        tooltip = window.navigation.item(window._row_of_key["scan"]).toolTip()
        self.assertIn("仪器执行服务未就绪", tooltip)
        self.assertIn("控制页需要执行服务", tooltip)

    def test_waiting_for_the_check_reads_as_not_yet_checked(self) -> None:
        """还没检查完（idle）与"服务不可达"（error）文案要分得开。"""
        window = self.make_window()
        window._model.set_service("instrument", "idle", "")

        window._apply_control_eligibility()

        tooltip = window.navigation.item(window._row_of_key["scan"]).toolTip()
        self.assertIn("未检查/连接中", tooltip)


if __name__ == "__main__":
    unittest.main()
