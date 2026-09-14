"""仪器执行服务入口。"""

from __future__ import annotations

import json
import socket
import sys
import urllib.request

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8765


def _port_available(host: str, port: int) -> bool:
    """检测端口能否绑定。

    注意不要用 ``Get-NetTCPConnection`` 之类的工具判断——它们在权限受限时会**静默返回空**，
    看起来像端口空闲，实际有进程在监听。直接试绑才可靠。
    """
    probe = socket.socket()
    try:
        probe.bind((host, port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def _describe_occupant(host: str, port: int) -> str:
    """占用者是不是我们自己的服务。"""
    url = f"http://{host}:{port}/control/v1/status"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001  探测失败就按「被别的程序占用」处理
        return "占用该端口的不是仪器执行服务，可能是其它程序。"
    if payload.get("service") == "instrument-service":
        detail = payload.get("detail", "")
        return (
            f"已有一个仪器执行服务在运行（version={payload.get('version')}）。\n"
            f"       它的状态描述：{detail}\n"
            "       如果这个实例是改代码之前启动的，它跑的是旧代码，需要先停掉再启动，"
            "否则客户端会连到旧端点（例如 /control/v1/pv-mapping 返回 404）。"
        )
    return "占用该端口的不是仪器执行服务，可能是其它程序。"


def main() -> None:
    """在本机回环地址启动开发服务。"""
    import uvicorn

    if not _port_available(_DEFAULT_HOST, _DEFAULT_PORT):
        print(
            f"[错误] {_DEFAULT_HOST}:{_DEFAULT_PORT} 已被占用，执行服务无法启动。",
            file=sys.stderr,
        )
        print(f"       {_describe_occupant(_DEFAULT_HOST, _DEFAULT_PORT)}", file=sys.stderr)
        print(
            "       查看占用者：netstat -ano | findstr :8765　（末列是 PID，再用任务管理器结束）\n"
            "       提示：客户端启动时会自动拉起执行服务，通常不需要手工再起一个。",
            file=sys.stderr,
        )
        raise SystemExit(1)

    uvicorn.run(
        "apps.instrument_service.app:app",
        host=_DEFAULT_HOST,
        port=_DEFAULT_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
