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

