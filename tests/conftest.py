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
