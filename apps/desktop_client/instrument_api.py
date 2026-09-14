"""受控信号读写的客户端访问入口。

架构前提（架构文档 6.2）：**执行服务是硬件写入的唯一入口**，界面不直连 IOC。
所有读值/写值都经本模块 POST 到执行服务，由服务端执行层完成边界、单步、
变化速率校验（``apps/instrument_service/signal_io.py``）。

**写操作全局串行**。两条写入若并发下发，执行层会各自基于自己读到的「当前值」
规划斜坡，实际下发序列交错成谁也不是的曲线——对高压设备是真实风险。因此同一
时刻只允许一个写请求在飞；忙时 ``request_write`` 立刻返回 None，由调用方显示
「上一次写入未完成」，而不是排队（排队会让操作员以为自己点的那次还没轮到）。
"""

from __future__ import annotations

import atexit
import json
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

from PySide6.QtCore import QSettings, QThread, Signal

SIGNALS_READ_PATH = "/control/v1/signals/read"
SIGNALS_WRITE_PATH = "/control/v1/signals/write"
SCAN_RUNS_PATH = "/control/v1/scan/runs"
TUNING_RUNS_PATH = "/control/v1/tuning/runs"

DEFAULT_INSTRUMENT_URL = "http://127.0.0.1:8765"

# 读请求超时：128 路快照在本地回环上远快于此
READ_TIMEOUT_S = 8.0
# 写请求超时：带斜坡的写入要按 max_rate 分步等待，必须留足
WRITE_TIMEOUT_S = 120.0
# 扫谱启动：服务端只做校验并立即返回，不会等整场扫完
SCAN_START_TIMEOUT_S = 20.0
# 扫谱状态/点查询：轮询用短超时，避免一次卡顿拖住整个轮询
SCAN_POLL_TIMEOUT_S = 10.0
# 调束确认执行：服务端同步完成「写设备 → 等稳定 → 采目标」，必须留足
TUNING_APPLY_TIMEOUT_S = 180.0

# 正在运行的请求线程。挂模块级集合而不是父页面：页面先销毁时 Qt 会报
# 「QThread: Destroyed while thread is still running」，而这些线程阻塞在网络
# 调用里无法取消，只能等它跑完（与 pv_mapping_api 同一处理）。
_RUNNING: set[QThread] = set()
_write_busy = False


def instrument_base_url() -> str:
    """执行服务地址：取系统设置里保存的值，未配置则用本机默认端口。"""
    settings = QSettings("SpectrumPlatform", "DesktopClient")
    return str(settings.value("service/instrumentUrl", DEFAULT_INSTRUMENT_URL)).rstrip("/")


class _JsonRequestThread(QThread):
    """发起一个 JSON 请求并回传解析结果，避免阻塞 Qt 主线程。"""

    completed = Signal(object)

    def __init__(
        self,
        url: str,
        payload: dict | None,
        timeout: float,
        method: str = "POST",
    ) -> None:
        super().__init__()
        self._url = url
        self._payload = payload
        self._timeout = timeout
        self._method = method
        _RUNNING.add(self)
        self.finished.connect(lambda: _RUNNING.discard(self))

    def run(self) -> None:
        data = None
        if self._payload is not None:
            data = json.dumps(self._payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=data,
            method=self._method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            self.completed.emit(
                {"ok": False, "message": self._http_detail(exc), "payload": None}
            )
        except Exception as exc:  # noqa: BLE001  连接类错误统一展示给操作员
            self.completed.emit(
                {"ok": False, "message": str(exc), "payload": None}
            )
        else:
            self.completed.emit({"ok": True, "message": "", "payload": payload})

    @staticmethod
    def _http_detail(exc) -> str:
        """取出 FastAPI 的错误明细，拿不到就退回状态码。"""
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001  非 JSON 错误体
            return f"HTTP {exc.code}"
        detail = payload.get("detail")
        if isinstance(detail, list):
            # 422 校验错误：拼出前几条字段说明，便于定位
            parts = [
                f"{'.'.join(str(x) for x in item.get('loc', []))}: {item.get('msg', '')}"
                for item in detail[:3]
                if isinstance(item, dict)
            ]
            return "请求校验失败：" + "；".join(parts) if parts else f"HTTP {exc.code}"
        return str(detail or f"HTTP {exc.code}")


class SignalReadThread(_JsonRequestThread):
    """批量读取受控信号快照。"""

    def __init__(self, base_url: str, signals: list[str] | None = None) -> None:
        super().__init__(
            base_url.rstrip("/") + SIGNALS_READ_PATH,
            {"signals": list(signals or [])},
            READ_TIMEOUT_S,
        )


class SignalWriteThread(_JsonRequestThread):
    """写入单个受控信号。"""

    def __init__(
        self,
        base_url: str,
        signal: str,
        value: float,
        *,
        ramp: bool = True,
        dry_run: bool = False,
    ) -> None:
        super().__init__(
            base_url.rstrip("/") + SIGNALS_WRITE_PATH,
            {
                "signal": signal,
                "value": float(value),
                # 每次操作一个 UUID：服务端据此做幂等，重复投递不会写两次设备
                "command_id": str(uuid4()),
                "ramp": bool(ramp),
                "dry_run": bool(dry_run),
            },
            WRITE_TIMEOUT_S,
        )


def is_write_busy() -> bool:
    """是否有写请求仍在飞。"""
    return _write_busy


# ----------------------------------------------------------------------
# 通用请求助手：调束端点较多，逐一写线程类没有信息量
# ----------------------------------------------------------------------
def _get(path: str, base_url: str | None = None, timeout: float = SCAN_POLL_TIMEOUT_S):
    thread = _JsonRequestThread(
        (base_url or instrument_base_url()).rstrip("/") + path, None, timeout, "GET"
    )
    thread.start()
    return thread


def _post(path: str, payload: dict, base_url: str | None = None, timeout: float = 20.0):
    thread = _JsonRequestThread(
        (base_url or instrument_base_url()).rstrip("/") + path, payload, timeout, "POST"
    )
    thread.start()
    return thread


# ----------------------------------------------------------------------
# 调束任务（架构文档 6.6：建议 → 人工确认）
# ----------------------------------------------------------------------
def request_tuning_start(request: dict, base_url: str | None = None):
    return _post(TUNING_RUNS_PATH, request, base_url, timeout=SCAN_START_TIMEOUT_S)


def request_tuning_status(run_id: str, base_url: str | None = None):
    return _get(f"{TUNING_RUNS_PATH}/{run_id}", base_url)


def request_tuning_iterations(run_id: str, base_url: str | None = None):
    return _get(f"{TUNING_RUNS_PATH}/{run_id}/iterations", base_url)


def request_tuning_approve(run_id: str, base_url: str | None = None):
    """确认候选并执行一轮（写设备 + 等稳定 + 采目标），超时要比普通请求长。"""
    return _post(
        f"{TUNING_RUNS_PATH}/{run_id}/approve", {}, base_url,
        timeout=TUNING_APPLY_TIMEOUT_S,
    )


def request_tuning_stop(run_id: str, base_url: str | None = None):
    return _post(
        f"{TUNING_RUNS_PATH}/{run_id}/stop", {}, base_url,
        timeout=SCAN_START_TIMEOUT_S,
    )


def request_tuning_acknowledge(run_id: str, note: str = "", base_url: str | None = None):
    suffix = f"?note={urllib.parse.quote(note)}" if note else ""
    return _post(
        f"{TUNING_RUNS_PATH}/{run_id}/acknowledge{suffix}", {}, base_url,
        timeout=SCAN_START_TIMEOUT_S,
    )


# ----------------------------------------------------------------------
# 扫谱任务
# ----------------------------------------------------------------------
class ScanStartThread(_JsonRequestThread):
    """创建并启动一次扫谱。"""

    def __init__(self, base_url: str, request: dict) -> None:
        super().__init__(
            base_url.rstrip("/") + SCAN_RUNS_PATH,
            request,
            SCAN_START_TIMEOUT_S,
            method="POST",
        )


class ScanStopThread(_JsonRequestThread):
    """请求停止扫谱。"""

    def __init__(self, base_url: str, run_id: str) -> None:
        super().__init__(
            f"{base_url.rstrip('/')}{SCAN_RUNS_PATH}/{run_id}/stop",
            {},
            SCAN_START_TIMEOUT_S,
            method="POST",
        )


class ScanAcknowledgeThread(_JsonRequestThread):
    """人工确认设备状态，释放恢复锁。"""

    def __init__(self, base_url: str, run_id: str, note: str = "") -> None:
        suffix = f"?note={urllib.parse.quote(note)}" if note else ""
        super().__init__(
            f"{base_url.rstrip('/')}{SCAN_RUNS_PATH}/{run_id}/acknowledge{suffix}",
            {},
            SCAN_START_TIMEOUT_S,
            method="POST",
        )


class ScanStatusThread(_JsonRequestThread):
    """查询扫谱状态。"""

    def __init__(self, base_url: str, run_id: str) -> None:
        super().__init__(
            f"{base_url.rstrip('/')}{SCAN_RUNS_PATH}/{run_id}",
            None,
            SCAN_POLL_TIMEOUT_S,
            method="GET",
        )


class ScanPointsThread(_JsonRequestThread):
    """增量拉取已完成的扫描点。"""

    def __init__(self, base_url: str, run_id: str, since: int = 0) -> None:
        super().__init__(
            f"{base_url.rstrip('/')}{SCAN_RUNS_PATH}/{run_id}/points?since={int(since)}",
            None,
            SCAN_POLL_TIMEOUT_S,
            method="GET",
        )


def request_scan_start(request: dict, base_url: str | None = None) -> ScanStartThread:
    thread = ScanStartThread(base_url or instrument_base_url(), request)
    thread.start()
    return thread


def request_scan_stop(run_id: str, base_url: str | None = None) -> ScanStopThread:
    thread = ScanStopThread(base_url or instrument_base_url(), run_id)
    thread.start()
    return thread


def request_scan_acknowledge(
    run_id: str, note: str = "", base_url: str | None = None
) -> ScanAcknowledgeThread:
    thread = ScanAcknowledgeThread(base_url or instrument_base_url(), run_id, note)
    thread.start()
    return thread


def request_scan_status(run_id: str, base_url: str | None = None) -> ScanStatusThread:
    thread = ScanStatusThread(base_url or instrument_base_url(), run_id)
    thread.start()
    return thread


def request_scan_points(
    run_id: str, since: int = 0, base_url: str | None = None
) -> ScanPointsThread:
    thread = ScanPointsThread(base_url or instrument_base_url(), run_id, since)
    thread.start()
    return thread


def request_read(
    base_url: str | None = None, signals: list[str] | None = None
) -> SignalReadThread:
    """启动一次快照读取；调用方负责避免重复发起（页面用「在飞就跳过」策略）。"""
    thread = SignalReadThread(base_url or instrument_base_url(), signals)
    thread.start()
    return thread


def request_write(
    signal: str,
    value: float,
    *,
    base_url: str | None = None,
    ramp: bool = True,
    dry_run: bool = False,
) -> SignalWriteThread | None:
    """启动一次写入；已有写入在飞时返回 None（写入必须串行，见模块说明）。"""
    global _write_busy
    if _write_busy:
        return None
    _write_busy = True
    thread = SignalWriteThread(
        base_url or instrument_base_url(), signal, value, ramp=ramp, dry_run=dry_run
    )
    thread.finished.connect(_release_write)
    thread.start()
    return thread


def _release_write() -> None:
    global _write_busy
    _write_busy = False


def write_busy_message() -> str:
    return "上一次写入尚未完成，请等待其结束再操作（写入按顺序下发，不排队）。"


_SHUTDOWN_WAIT_S = 8.0


def _drain_running_requests() -> None:
    """解释器退出前等在跑的请求收尾，避免 Qt 报线程被销毁。"""
    for thread in list(_RUNNING):
        if thread.isRunning():
            thread.wait(int(_SHUTDOWN_WAIT_S * 1000))


atexit.register(_drain_running_requests)


def readings_by_signal(snapshot: dict | None) -> dict[str, dict]:
    """把快照响应整理成 ``{业务信号: 读数}``。"""
    if not snapshot:
        return {}
    return {
        str(item["signal"]): item
        for item in snapshot.get("readings", [])
        if item.get("signal")
    }
