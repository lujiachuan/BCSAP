"""受控设备 PV 映射契约：业务信号 → EPICS PV。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class PvMappingEntry(BaseModel):
    """一条“业务信号 → PV”映射。

    signal 是执行服务内部引用的稳定键（如 ``quadrupole.q1.current``），
    pv 是现场 IOC 上真实的 EPICS PV 名（如 ``BL:Q1:ISET``）。
    """

    model_config = ConfigDict(strict=True)

    signal: str
    label: str
    pv: str
    unit: str
    writable: bool
    required: bool


class PvMappingConfig(BaseModel):
    """PV 映射与网关配置，由执行服务持有并持久化。"""

    model_config = ConfigDict(strict=True)

    version: int
    gateway: Literal["simulated", "channel-access"]
    ca_lib_dir: str
    entries: list[PvMappingEntry]


class PvMappingIssue(BaseModel):
    """一条校验问题，index 为行号（-1 表示整体配置）。"""

    model_config = ConfigDict(strict=True)

    index: int
    field: str
    message: str


class PvMappingValidationError(BaseModel):
    """校验失败响应体。"""

    model_config = ConfigDict(strict=True)

    config_version: int
    issues: list[PvMappingIssue]
