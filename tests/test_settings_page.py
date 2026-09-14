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


if __name__ == "__main__":
    unittest.main()
