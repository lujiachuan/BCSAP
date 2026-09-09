"""客户端状态单一来源：AppStatusModel。

模型只管理状态并发布变化，不直接操作控件。初始化任务 / 后台同步 / 用户操作
把结果写入模型；侧栏、工作台、初始化页、业务页订阅后自行取读（M1 起统一入口）。

状态语义（与方案 §3.1 对齐）：
- ``state`` 取 good / warn / error / idle / running；
- 服务键：data（数据服务）、instrument（仪器执行服务）、epics（PV）、cache（本地数据）；
- 初始化步骤键：config / instrument / pv / data / sync；
- 操作资格由服务状态推导（页面不自行拼条件），并给出可展示的阻塞原因。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QObject, Signal


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


class AppStatusModel(QObject):
    """状态模型：任何状态变化统一经 ``updated`` 通知订阅方。"""

    updated = Signal()

    SERVICE_KEYS = ("data", "instrument", "epics", "cache")
    STEP_KEYS = ("config", "instrument", "pv", "data", "sync")

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.phase = "starting"  # starting / checking / entered / finished
        self._services: dict[str, dict[str, str]] = {}
        for key in self.SERVICE_KEYS:
            self._services[key] = {"state": "idle", "text": "", "detail": "", "at": ""}
        self._steps: dict[str, dict[str, str]] = {}
        for key in self.STEP_KEYS:
            self._steps[key] = {"state": "idle", "detail": "", "at": ""}

        self.pv_total = 0
        self.pv_connected = 0
        self.pv_details: list[str] = []
        self.sync_current = 0
        self.sync_total = 0
        self.sync_determinate = True
        self.essential_ready = False

    # ------------------------------------------------------------------ 写入

    def set_service(
        self, key: str, state: str, text: str = "", detail: str = ""
    ) -> None:
        if key not in self._services:
            return
        self._services[key].update(
            {"state": state, "text": text, "detail": detail, "at": _now()}
        )
        self.updated.emit()

    def set_step(self, key: str, state: str, detail: str) -> None:
        if key not in self._steps:
            return
        self._steps[key].update({"state": state, "detail": detail, "at": _now()})
        self.updated.emit()

    def set_pv(self, connected: int, total: int, details: list[str] | None = None) -> None:
        self.pv_connected = connected
        self.pv_total = total
        if details is not None:
            self.pv_details = details
        self.updated.emit()

    def set_sync_progress(
        self, current: int, total: int, determinate: bool = True
    ) -> None:
        self.sync_current = current
        self.sync_total = total
        self.sync_determinate = determinate
        self.updated.emit()

    def mark_essential_ready(self) -> None:
        self.essential_ready = True
        if self.phase == "starting":
            self.phase = "entered"
        self.updated.emit()

    def mark_finished(self) -> None:
        self.phase = "finished"
        self.updated.emit()

    # ------------------------------------------------------------------ 读取

    def service(self, key: str) -> dict[str, str]:
        return self._services[key]

    def step(self, key: str) -> dict[str, str]:
        return self._steps[key]

    def failed_service_keys(self) -> list[str]:
        return [
            key
            for key in self.SERVICE_KEYS
            if self._services[key]["state"] in {"error", "warn"}
            and key in {"data", "instrument", "epics"}
        ]

    @property
    def instrument_ready(self) -> bool:
        return self._services["instrument"]["state"] == "good"

    @property
    def pv_ready(self) -> bool:
        return self._services["epics"]["state"] == "good"

    @property
    def data_ready(self) -> bool:
        return self._services["data"]["state"] == "good"

    @property
    def can_control(self) -> bool:
        return self.instrument_ready and self.pv_ready

    @property
    def blocking_reasons(self) -> list[str]:
        reasons: list[str] = []
        if not self.instrument_ready:
            state = self._services["instrument"]["state"]
            reasons.append("仪器执行服务" + ("未就绪" if state == "error" else "未检查/连接中"))
        if not self.pv_ready:
            state = self._services["epics"]["state"]
            reasons.append("关键 PV" + ("未连接" if state == "error" else "未确认/检查中"))
        return reasons

    @property
    def can_sync_continue(self) -> bool:
        """数据服务可达即可继续同步；与进入主界面解耦。"""
        return self.data_ready and self._steps["sync"]["state"] == "running"

    def reset(self) -> None:
        for key in self._services:
            self._services[key] = {"state": "idle", "text": "", "detail": "", "at": ""}
        for key in self._steps:
            self._steps[key] = {"state": "idle", "detail": "", "at": ""}
        self.pv_total = 0
        self.pv_connected = 0
        self.pv_details = []
        self.sync_current = 0
        self.sync_total = 0
        self.essential_ready = False
        self.phase = "starting"
        self.updated.emit()
