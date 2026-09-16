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
    # 设备写保护（部署只读或配置损坏保护）；客户端据此禁用常规写入入口。
    read_only: bool = False
    # configuration 表示映射损坏触发的保护，可在设置页修复；deployment 不可绕过。
    read_only_reason: Literal["deployment", "configuration"] | None = None
