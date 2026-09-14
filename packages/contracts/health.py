"""服务健康状态契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ServiceStatus(BaseModel):
    """客户端和服务共同使用的健康状态。"""

    model_config = ConfigDict(strict=True)

    service: str
    status: Literal["ready", "degraded", "unavailable"]
    version: str
    detail: str | None = None
    # 全局只读部署模式（改造报告 §4.2）：执行服务按部署参数禁用了所有写入。
    # 客户端据此不给出"能按但按不动"的按钮，并在界面上说明原因。
    read_only: bool = False

