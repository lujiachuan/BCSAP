"""测试期护栏：任何用例都不许真的去**改**本机开发服务上的东西。

**为什么需要这个**：2026-09-16 踩过一次真实的坑——设置页的一个新用例调用了
``_save_pv_mapping()``，但只把"读信号"换成了桩，没换 ``PvMappingRequestThread``；
那条用例**真的向 127.0.0.1:8765 发了 PUT**，把现场 128 条 PV 映射覆盖成了 3 条测试
数据（`PV:gas.ar.flow_setpoint` 这种假 PV），客户端随即报"EPICS 检查失败"。

护栏边界（刻意只拦"会改东西"的请求）：

* 只对**本机开发服务端口**生效（8765 仪器执行服务 / 8767 只读实例 / 8000 数据服务）；
* 只拦**写类**请求（映射保存、单点/成组写入、回落、扫谱/调束的启动与确认、停止）；
* 读取类（状态、快照 `POST /signals/read`、健康检查、目录、任务查询）一律放行——
  很多用例就是靠"页面自己发一次读请求然后失败"来验证降级路径的，拦掉它们只会
  把 90 多个无关用例弄红，反而掩盖真正危险的那一类。

拦住之后**还要让用例失败**：请求线程里的异常会被它自己吞掉（转成 ``ok=False``），
所以违规会先记下来，在用例结束时断言为空——错在"连了不该连的服务"，就要在这里红，
而不是悄悄少写一次配置。
"""

from __future__ import annotations

import importlib
import urllib.request
from urllib.parse import urlparse

import pytest

DEV_SERVICE_PORTS = {"8765", "8767", "8000"}

# 会改配置或改设备的端点（前缀匹配）
GUARDED_WRITE_PATHS = (
    "/control/v1/pv-mapping",
    "/control/v1/manual-dashboard",
    "/control/v1/signals/write",
    "/control/v1/signals/write-batch",
    "/control/v1/magnets/retract",
    "/control/v1/scan/runs",
    "/control/v1/tuning/runs",
    "/control/v1/recovery",
)
WRITE_METHODS = {"PUT", "POST", "PATCH", "DELETE"}

_violations: list[str] = []


def _describe(url: object) -> tuple[str, str, str]:
    """返回 (端口, 路径, 方法)。"""
    if isinstance(url, str):
        target, method = url, "GET"
    else:
        target = str(getattr(url, "full_url", url))
        method = str(getattr(url, "method", "") or "GET").upper()
    try:
        parsed = urlparse(target)
    except Exception:  # noqa: BLE001  解析不了就当不是开发服务
        return "", "", method
    return str(parsed.port or ""), parsed.path or "", method


def _is_guarded(url: object) -> bool:
    port, path, method = _describe(url)
    if port not in DEV_SERVICE_PORTS:
        return False
    if method not in WRITE_METHODS:
        return False
    if path == "/control/v1/signals/read":
        return False  # 读快照：不改任何东西
    return any(path.startswith(prefix) for prefix in GUARDED_WRITE_PATHS)


@pytest.fixture(autouse=True)
def forbid_live_dev_service_writes(monkeypatch: pytest.MonkeyPatch):
    """拦住指向本机开发服务的写请求，并在用例结束时报告违规。"""
    _violations.clear()
    real_urlopen = urllib.request.urlopen

    def guarded(url, *args, **kwargs):
        if _is_guarded(url):
            _, path, method = _describe(url)
            _violations.append(f"{method} {path}")
            raise RuntimeError(
                f"测试里不许对真实开发服务发写请求：{method} {path}。"
                "请把请求换成桩（例如把 PvMappingRequestThread / instrument_api.request_* "
                "替换成假线程），否则会改动现场配置或设备。"
            )
        return real_urlopen(url, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", guarded)
    yield
    assert not _violations, (
        "以下写请求打到了真实开发服务（会改动现场配置或设备）："
        + "；".join(_violations)
    )


@pytest.fixture(autouse=True)
def block_tuning_read_requests(monkeypatch: pytest.MonkeyPatch):
    """调束页**构造时**会真的去拉映射/目录/能力：单测里一律换成"永不完成"的假线程。

    为什么必须拦：本机常常正跑着一个执行服务（现场联调时就是如此）。真发请求会让
    `_on_mapping` 在**用例中途**回调，把测试自己塞进去的 4 路映射覆盖成现场的 29 路，
    于是"范围标尺应有 4 行"这类断言时好时坏——2026-09-22 实测踩到过
    （87 != 12）。测试的结果不该取决于现场此刻有没有服务在跑。
    """
    from apps.desktop_client import instrument_api

    class _NeverFinishes:
        """假请求线程：connect 之后永远不回调，页面停在"已发出、还没回来"。"""

        def __init__(self, *_args, **_kwargs) -> None:
            self.completed = _SignalStub()
            self.finished = _SignalStub()

        def start(self) -> None:
            return None

        def isRunning(self) -> bool:  # noqa: N802
            return False

        def deleteLater(self) -> None:  # noqa: N802
            return None

    class _SignalStub:
        def connect(self, *_args, **_kwargs) -> None:
            return None

    def fake(*_args, **_kwargs) -> _NeverFinishes:
        return _NeverFinishes()

    for name in (
        "request_tuning_catalog",
        "request_tuning_capabilities",
        "request_tuning_runs",
        "request_tuning_status",
        "request_tuning_iterations",
        "request_tuning_progress",
        "request_tuning_events",
    ):
        if hasattr(instrument_api, name):
            monkeypatch.setattr(instrument_api, name, fake, raising=False)
    for module_name in ("tuning", "manual", "settings"):
        try:
            module = importlib.import_module(
                f"apps.desktop_client.pages.{module_name}"
            )
        except ImportError:  # pragma: no cover - 模块缺失时不该让所有用例挂掉
            continue
        if hasattr(module, "PvMappingRequestThread"):
            monkeypatch.setattr(module, "PvMappingRequestThread", fake, raising=False)
