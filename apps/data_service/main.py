"""共享数据服务入口。"""

from __future__ import annotations

import os


def main() -> None:
    """启动开发数据服务。

    默认监听 ``0.0.0.0:8000``，便于同网段 Windows 客户端连接。
    可用环境变量 ``DATA_SERVICE_HOST`` / ``DATA_SERVICE_PORT`` 覆盖。
    """

    import uvicorn

    host = os.environ.get("DATA_SERVICE_HOST", "0.0.0.0")
    port = int(os.environ.get("DATA_SERVICE_PORT", "8000"))
    uvicorn.run(
        "apps.data_service.app:app",
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()

