"""共享数据服务入口。"""


def main() -> None:
    """启动开发数据服务。"""

    import uvicorn

    uvicorn.run(
        "apps.data_service.app:app",
        host="127.0.0.1",
        port=8000,
        log_level="info",
    )


if __name__ == "__main__":
    main()

