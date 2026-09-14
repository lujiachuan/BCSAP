"""启动初始化所需的服务契约与本地缓存验证。"""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QSettings

from apps.data_service.app import create_app as create_data_app
from apps.data_service.sync_catalog import (
    SPECTRUM_CONTENT,
    sync_records,
    sync_spectra,
)
from apps.desktop_client.initialization import (
    CACHE_ENV_VAR,
    CACHE_SETTINGS_KEY,
    CacheUnavailable,
    InitializationWorker,
    LocalDataCache,
    cache_root_from_settings,
    default_cache_root,
    ensure_cache_root,
)
from apps.instrument_service.app import create_app as create_instrument_app
from apps.instrument_service.pv_health import check_pv_health, create_simulated_gateway
from apps.instrument_service.pv_mapping import default_config
from packages.spectrum import encode_spectrum, spectrum_checksum


class InitializationApiTests(unittest.TestCase):
    def test_instrument_service_reports_all_controlled_pvs(self) -> None:
        config = default_config()
        payload = check_pv_health(create_simulated_gateway(config), config)

        self.assertEqual(payload.status, "ready")
        self.assertEqual(payload.summary.connected, payload.summary.total)
        self.assertEqual(payload.summary.required_failed, 0)

    def test_data_service_supports_full_then_incremental_sync(self) -> None:
        records = sync_records()
        spectra = sync_spectra()

        self.assertEqual(len(records), 3)
        self.assertEqual(len(spectra), 3)
        for metadata in spectra:
            content = SPECTRUM_CONTENT[metadata["id"]]
            self.assertEqual(metadata["sha256"], spectrum_checksum(content))

    def test_apps_expose_initialization_routes(self) -> None:
        data_paths = {route.path for route in create_data_app().routes}
        instrument_paths = {route.path for route in create_instrument_app().routes}

        self.assertIn("/api/v1/sync/manifest", data_paths)
        self.assertIn("/api/v1/sync/changes", data_paths)
        self.assertIn("/control/v1/pvs/health", instrument_paths)
        # 成组回落必须走执行服务（客户端只写第一路 = 其余磁铁停在原地）
        self.assertIn("/control/v1/magnets/retract", instrument_paths)


class CacheRootConfigTests(unittest.TestCase):
    """本地镜像目录：有默认值、可配置、不可用时报出目录名与改法。"""

    def test_env_override_wins(self) -> None:
        with mock.patch.dict(
            os.environ,
            {CACHE_ENV_VAR: r"D:\data\cache", "LOCALAPPDATA": r"C:\ignored"},
            clear=True,
        ):
            self.assertEqual(default_cache_root(), Path(r"D:\data\cache"))

    def test_default_is_under_localappdata(self) -> None:
        with mock.patch.dict(
            os.environ, {"LOCALAPPDATA": r"C:\Users\tester\AppData\Local"}, clear=True
        ):
            self.assertEqual(
                default_cache_root(),
                Path(r"C:\Users\tester\AppData\Local\SpectrumPlatform\client_cache"),
            )

    def test_settings_value_overrides_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = QSettings(
                str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat
            )
            chosen = Path(directory) / "chosen"

            self.assertEqual(cache_root_from_settings(settings), default_cache_root())

            settings.setValue(CACHE_SETTINGS_KEY, str(chosen))
            settings.sync()

            self.assertEqual(cache_root_from_settings(settings), chosen)

    def test_ensure_cache_root_creates_the_directory_without_leaving_probes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "a" / "b"

            self.assertEqual(ensure_cache_root(target), target)

            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])

    def test_missing_cache_root_reports_how_to_change_it(self) -> None:
        """目录位置被一个文件占住时必须说清是哪个目录、去哪儿改。"""
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "blocked"
            blocker.write_text("not a directory", encoding="utf-8")

            with self.assertRaises(CacheUnavailable) as caught:
                LocalDataCache(blocker)

            message = str(caught.exception)
            self.assertIn(str(blocker), message)
            self.assertIn("系统设置", message)


class LocalDataCacheTests(unittest.TestCase):
    def test_spectrum_is_verified_and_published_atomically(self) -> None:
        payload = encode_spectrum([1.0, 2.0], [3.0, 4.0])
        checksum = spectrum_checksum(payload)
        metadata = {
            "id": "spectrum-1",
            "version": 1,
            "updated_at": "2026-09-08T08:00:00+00:00",
            "point_count": 2,
            "byte_length": len(payload),
            "sha256": checksum,
        }
        with tempfile.TemporaryDirectory() as directory:
            cache = LocalDataCache(Path(directory))
            cache.save_spectrum(metadata, payload)
            cache.set_cursor("cursor-1")

            self.assertTrue(cache.has_spectrum("spectrum-1", checksum))
            self.assertEqual(cache.cursor(), "cursor-1")
            self.assertEqual(list(Path(directory).rglob("*.part")), [])
            cache.close()


class InitializationPageRetryTests(unittest.TestCase):
    """初始化步骤的「重试」入口：跳过与失败都要能重来，不能只能重启客户端。"""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def make_page(self):
        from apps.desktop_client.initialization import InitializationPage

        return InitializationPage()

    def test_skipped_sync_step_offers_retry(self) -> None:
        page = self.make_page()

        page.set_step("sync", "warn", "已跳过，继续使用本地缓存")

        self.assertFalse(page.retry_buttons["sync"].isHidden())
        # config 步骤没有重试目标，永远不给重试
        self.assertTrue(page.retry_buttons["config"].isHidden())

    def test_running_step_hides_retry(self) -> None:
        page = self.make_page()
        page.set_step("sync", "error", "失败")

        page.set_step("sync", "running", "正在下载谱图 1 / 3")

        self.assertTrue(page.retry_buttons["sync"].isHidden())

    def test_failed_sync_step_offers_retry(self) -> None:
        page = self.make_page()

        page.set_step("sync", "error", "本地数据目录不可用")

        self.assertFalse(page.retry_buttons["sync"].isHidden())


class InitializationWorkerTests(unittest.TestCase):
    def test_service_checks_start_in_parallel(self) -> None:
        worker = InitializationWorker("http://data", "http://instrument")
        barrier = threading.Barrier(2)
        overlapped: list[bool] = []

        def check() -> bool:
            try:
                barrier.wait(timeout=1.0)
            except threading.BrokenBarrierError:
                overlapped.append(False)
            else:
                overlapped.append(True)
            return True

        worker._check_instrument = check  # type: ignore[method-assign]
        worker._check_data_service = check  # type: ignore[method-assign]
        worker._sync_data = lambda: None  # type: ignore[method-assign]
        worker.run()

        self.assertEqual(overlapped, [True, True])


if __name__ == "__main__":
    unittest.main()
