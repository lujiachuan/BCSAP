"""启动初始化所需的服务契约与本地缓存验证。"""

import tempfile
import unittest
from pathlib import Path

from apps.data_service.app import create_app as create_data_app
from apps.data_service.sync_catalog import (
    SPECTRUM_CONTENT,
    sync_records,
    sync_spectra,
)
from apps.desktop_client.initialization import LocalDataCache
from apps.instrument_service.app import create_app as create_instrument_app
from apps.instrument_service.pv_health import check_pv_health, create_simulated_gateway
from packages.spectrum import encode_spectrum, spectrum_checksum


class InitializationApiTests(unittest.TestCase):
    def test_instrument_service_reports_all_controlled_pvs(self) -> None:
        payload = check_pv_health(create_simulated_gateway())

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


if __name__ == "__main__":
    unittest.main()
