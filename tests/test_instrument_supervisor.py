"""instrument_supervisor 的纯逻辑测试（不启动 Qt/GUI）。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from apps.desktop_client.instrument_supervisor import (
    InstrumentServiceSupervisor,
    find_sibling_service_exe,
)


class FindSiblingServiceExeTests(unittest.TestCase):
    def test_parent_layout_operator_folder(self) -> None:
        """操作电脑单文件夹交付：服务与客户端并列在同一个交付根下。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client_dir = root / "spectrum-client"
            service_exe = (
                root / "spectrum-instrument-service" / "spectrum-instrument-service.exe"
            )
            client_dir.mkdir()
            service_exe.parent.mkdir(parents=True)
            service_exe.write_bytes(b"")
            self.assertEqual(find_sibling_service_exe(client_dir), service_exe)

    def test_nested_layout_service_inside_client_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client_dir = root / "spectrum-client"
            service_exe = client_dir / "spectrum-instrument-service" / "spectrum-instrument-service.exe"
            client_dir.mkdir()
            service_exe.parent.mkdir(parents=True)
            service_exe.write_bytes(b"")
            self.assertEqual(find_sibling_service_exe(client_dir), service_exe)

    def test_missing_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client_dir = Path(tmp) / "spectrum-client"
            client_dir.mkdir()
            self.assertIsNone(find_sibling_service_exe(client_dir))


class ServiceAliveTests(unittest.TestCase):
    def test_unreachable_port_returns_false(self) -> None:
        self.assertFalse(InstrumentServiceSupervisor.service_alive("http://127.0.0.1:1"))


class EnsureStartedTests(unittest.TestCase):
    def test_no_sibling_and_not_frozen_does_nothing(self) -> None:
        """检索电脑/开发环境：无服务 exe 时 ensure_started 应返回 False 且不拉起。"""
        supervisor = InstrumentServiceSupervisor()
        self.assertFalse(supervisor.ensure_started(service_url="http://127.0.0.1:1"))
        self.assertFalse(supervisor._started_by_us)

    def test_env_override_points_to_missing_exe_does_nothing(self) -> None:
        os.environ["SPECTRUM_INSTRUMENT_EXE"] = r"C:\definitely\missing.exe"
        try:
            supervisor = InstrumentServiceSupervisor()
            self.assertFalse(supervisor.ensure_started(service_url="http://127.0.0.1:1"))
        finally:
            os.environ.pop("SPECTRUM_INSTRUMENT_EXE", None)


if __name__ == "__main__":
    unittest.main()
