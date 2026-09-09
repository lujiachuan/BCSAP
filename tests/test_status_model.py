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

    def test_eligibility_requires_instrument_and_pv(self) -> None:
        self.assertFalse(self.model.can_control)
        self.model.set_service("instrument", "good", "就绪")
        self.assertFalse(self.model.can_control)
        self.model.set_service("epics", "good", "12 / 12")
        self.assertTrue(self.model.can_control)
        self.assertEqual(self.model.blocking_reasons, [])

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
