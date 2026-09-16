"""AppStatusModel 纯逻辑验证（无需 QApplication）。"""

import unittest

from apps.desktop_client.status_model import AppStatusModel


class AppStatusModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = AppStatusModel()

    def test_service_state_and_updated(self) -> None:
        calls: list[bool] = []
        self.model.updated.connect(lambda: calls.append(True))
        self.model.set_service("data", "good", "正常")
        self.assertEqual(self.model.service("data")["state"], "good")
        self.assertTrue(self.model.service("data")["at"])
        self.assertEqual(calls, [True])

    def test_eligibility_depends_on_instrument_only(self) -> None:
        """能不进控制页只看**仪器执行服务是否可达**。

        PV 连不全是**提示**不是拦路：现场上百路 PV 很难全可达，而扫谱/调束各自只用
        得到自己那几路（现场反馈，2026-09-14）。所以这里断言"有 PV 未连接也能进"。
        """
        self.assertFalse(self.model.can_control)
        self.model.set_service("instrument", "good", "就绪")
        self.assertTrue(self.model.can_control, "服务可达就该能进控制页")
        self.assertEqual(self.model.blocking_reasons, [])

        # PV 未连接：仍然能进，但要有提示
        self.model.set_service("epics", "warn", "120 / 128")
        self.model.set_pv(120, 128, ["Part1:Sputtering：未连接"], required_failed=1)
        self.assertTrue(self.model.can_control)
        self.assertEqual(self.model.blocking_reasons, [])
        self.assertEqual(self.model.warnings and len(self.model.warnings), 1)
        self.assertIn("120 / 128", self.model.pv_note)
        self.assertIn("必需", self.model.pv_note)

    def test_pv_problems_do_not_block_control(self) -> None:
        """只有仪器服务不可达才算阻塞。"""
        self.model.set_service("instrument", "good", "就绪")
        self.model.set_service("epics", "error", "0 / 128")
        self.model.set_pv(0, 128, ["全部未连接"], required_failed=2)

        self.assertTrue(self.model.can_control)
        self.assertEqual(self.model.blocking_reasons, [])

        self.model.set_service("instrument", "error", "不可达")
        self.assertFalse(self.model.can_control)
        self.assertTrue(any("仪器执行服务" in r for r in self.model.blocking_reasons))

    def test_blocking_reasons_present_when_unavailable(self) -> None:
        self.model.set_service("instrument", "error", "不可达")
        reasons = self.model.blocking_reasons
        self.assertTrue(any("仪器执行服务" in r for r in reasons))

    def test_failed_service_keys_exclude_cache(self) -> None:
        self.model.set_service("data", "error", "不可达")
        self.model.set_service("instrument", "warn", "降级")
        self.model.set_service("cache", "error", "不可写")
        failed = self.model.failed_service_keys()
        self.assertEqual(sorted(failed), ["data", "instrument"])

    def test_pv_and_sync_progress(self) -> None:
        self.model.set_pv(7, 12, ["BL:Q1:ISET：未连接"])
        self.assertEqual(self.model.pv_connected, 7)
        self.model.set_sync_progress(3, 40)
        self.assertTrue(self.model.sync_determinate)
        self.model.reset()
        self.assertEqual(self.model.service("data")["state"], "idle")
        self.assertFalse(self.model.essential_ready)


if __name__ == "__main__":
    unittest.main()
