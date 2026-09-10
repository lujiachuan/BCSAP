"""PV 映射的客户端访问入口。

映射是执行服务持有的受控配置，客户端只通过 API 读写：
系统设置页用它做增删改，自动调束页用它把 PV 名显示成现场真实值。
两个页面共用本模块，避免页面之间互相 import。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from PySide6.QtCore import QThread, Signal

PV_MAPPING_PATH = "/control/v1/pv-mapping"


class PvMappingRequestThread(QThread):
    """在线程中读写执行服务的 PV 映射，避免阻塞 Qt 主线程。

    ``payload`` 为 None 表示 GET，否则为 PUT 的配置体。
    结果统一用 ``completed`` 回传一个字典，界面只处理一种回调形状。
    """

    completed = Signal(object)

    def __init__(self, base_url: str, payload: dict | None, parent=None) -> None:
        super().__init__(parent)
        self._base_url = base_url
        self._payload = payload

    def run(self) -> None:
        url = self._base_url.rstrip("/") + PV_MAPPING_PATH
        try:
            if self._payload is None:
                request = urllib.request.Request(url)
            else:
                body = json.dumps(self._payload, ensure_ascii=False).encode("utf-8")
                request = urllib.request.Request(
                    url,
                    data=body,
                    method="PUT",
                    headers={"Content-Type": "application/json"},
                )
            with urllib.request.urlopen(request, timeout=5.0) as response:
                config = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            message, issues = self._decode_error(exc)
            self.completed.emit(
                {"ok": False, "config": None, "message": message, "issues": issues}
            )
        except Exception as exc:  # noqa: BLE001  连接类错误统一展示
            self.completed.emit(
                {"ok": False, "config": None, "message": str(exc), "issues": []}
            )
        else:
            self.completed.emit(
                {"ok": True, "config": config, "message": "", "issues": []}
            )

    @staticmethod
    def _decode_error(exc) -> tuple[str, list]:
        """把校验失败的 400 响应拆成「整体消息 + 逐行问题」。"""
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001  非 JSON 错误体
            return f"HTTP {exc.code}", []
        detail = payload.get("detail")
        if isinstance(detail, dict):
            return "配置校验未通过，请修正标红项后重试。", list(detail.get("issues", []))
        return str(detail or f"HTTP {exc.code}"), []


def pv_by_signal(config: dict | None) -> dict[str, str]:
    """把映射配置整理成 ``{业务信号: PV 名}``，忽略缺字段的行。"""
    if not config:
        return {}
    return {
        str(entry["signal"]): str(entry["pv"])
        for entry in config.get("entries", [])
        if entry.get("signal") and entry.get("pv")
    }
