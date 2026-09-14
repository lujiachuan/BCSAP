"""PV 映射的客户端访问入口。

映射是执行服务持有的受控配置，客户端只通过 API 读写：
系统设置页用它做增删改，自动调束页用它把 PV 名显示成现场真实值。
两个页面共用本模块，避免页面之间互相 import。
"""

from __future__ import annotations

import atexit
import json
import urllib.error
import urllib.request

from PySide6.QtCore import QThread, Signal

PV_MAPPING_PATH = "/control/v1/pv-mapping"

# 正在运行的请求线程。必须在这里保活：线程若挂在页面上，页面先销毁（关闭设置页、
# 或测试里创建后即释放）时 Qt 会报「QThread: Destroyed while thread is still running」，
# 而线程此时正阻塞在网络调用上，无法取消，只能等它自己跑完。
_RUNNING: set[PvMappingRequestThread] = set()


class PvMappingRequestThread(QThread):
    """在线程中读写执行服务的 PV 映射，避免阻塞 Qt 主线程。

    ``payload`` 为 None 表示 GET，否则为 PUT 的配置体。
    结果统一用 ``completed`` 回传一个字典，界面只处理一种回调形状。

    故意**不挂父对象**、改由模块级 ``_RUNNING`` 保活，理由见上面。
    """

    completed = Signal(object)

    def __init__(self, base_url: str, payload: dict | None) -> None:
        super().__init__()
        self._base_url = base_url
        self._payload = payload
        _RUNNING.add(self)
        self.finished.connect(self._forget)

    def _forget(self) -> None:
        _RUNNING.discard(self)

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


_SHUTDOWN_WAIT_S = 8.0


def _drain_running_requests() -> None:
    """解释器退出前等在跑的请求收尾。

    请求线程无法取消（阻塞在 urllib 调用里），若不在这里等它结束，解释器退出时
    仍存活的线程会被销毁，Qt 报「QThread: Destroyed while thread is still running」。
    """
    for thread in list(_RUNNING):
        if thread.isRunning():
            thread.wait(int(_SHUTDOWN_WAIT_S * 1000))


atexit.register(_drain_running_requests)


def pv_by_signal(config: dict | None) -> dict[str, str]:
    """把映射配置整理成 ``{业务信号: PV 名}``，忽略缺字段的行。"""
    if not config:
        return {}
    return {
        str(entry["signal"]): str(entry["pv"])
        for entry in config.get("entries", [])
        if entry.get("signal") and entry.get("pv")
    }
