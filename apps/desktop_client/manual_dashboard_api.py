"""手动控制主面板配置的异步客户端。"""

from __future__ import annotations

import atexit
import json
import urllib.error
import urllib.request

from PySide6.QtCore import QThread, Signal

MANUAL_DASHBOARD_PATH = "/control/v1/manual-dashboard"
_RUNNING: set["ManualDashboardRequestThread"] = set()


class ManualDashboardRequestThread(QThread):
    completed = Signal(object)

    def __init__(self, base_url: str, payload: dict | None) -> None:
        super().__init__()
        self._base_url = base_url
        self._payload = payload
        _RUNNING.add(self)
        self.finished.connect(lambda: _RUNNING.discard(self))

    def run(self) -> None:
        url = self._base_url.rstrip("/") + MANUAL_DASHBOARD_PATH
        try:
            if self._payload is None:
                request = urllib.request.Request(url)
            else:
                request = urllib.request.Request(
                    url,
                    data=json.dumps(self._payload, ensure_ascii=False).encode("utf-8"),
                    method="PUT",
                    headers={"Content-Type": "application/json"},
                )
            with urllib.request.urlopen(request, timeout=5.0) as response:
                config = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail")
            except Exception:  # noqa: BLE001
                detail = f"HTTP {exc.code}"
            self.completed.emit({"ok": False, "message": str(detail), "config": None})
        except Exception as exc:  # noqa: BLE001
            self.completed.emit({"ok": False, "message": str(exc), "config": None})
        else:
            self.completed.emit({"ok": True, "message": "", "config": config})


def _drain() -> None:
    for thread in list(_RUNNING):
        if thread.isRunning():
            thread.wait(8000)


atexit.register(_drain)
