"""仪器执行服务入口。"""


def main() -> None:
    """在本机回环地址启动开发服务。"""

    import uvicorn

    uvicorn.run(
        "apps.instrument_service.app:app",
        host="127.0.0.1",
        port=8765,
        log_level="info",
    )


if __name__ == "__main__":
    main()

