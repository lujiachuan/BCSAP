"""桌面页面中可独立验证的输入规则。"""

import unittest

from apps.desktop_client.pages import _valid_service_url


class ServiceUrlTests(unittest.TestCase):
    def test_accepts_http_service_addresses(self) -> None:
        self.assertTrue(_valid_service_url("http://127.0.0.1:8000"))
        self.assertTrue(_valid_service_url("https://spectrum.example.test/api"))

    def test_rejects_unsafe_or_incomplete_addresses(self) -> None:
        for value in (
            "",
            "127.0.0.1:8000",
            "ftp://127.0.0.1",
            "http://user:secret@127.0.0.1",
            "http://127.0.0.1?token=secret",
        ):
            self.assertFalse(_valid_service_url(value), value)


if __name__ == "__main__":
    unittest.main()
