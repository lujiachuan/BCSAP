"""启动初始化、PV 健康和数据同步契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class PvHealthItem(BaseModel):
    model_config = ConfigDict(strict=True)

    signal: str
    pv: str
    required: bool
    connected: bool
    readable: bool
    writable: bool
    severity: int | None = None
    latency_ms: float | None = None
    detail: str | None = None


class PvHealthSummary(BaseModel):
    model_config = ConfigDict(strict=True)

    total: int
    connected: int
    required_failed: int
    optional_failed: int


class PvHealthResponse(BaseModel):
    model_config = ConfigDict(strict=True)

    status: Literal["ready", "degraded", "unavailable"]
    checked_at: str
    config_version: str
    summary: PvHealthSummary
    items: list[PvHealthItem]


class SyncManifest(BaseModel):
    model_config = ConfigDict(strict=True)

    cursor: str
    record_count: int
    spectrum_count: int
    total_bytes: int


class SyncRecord(BaseModel):
    model_config = ConfigDict(strict=True)

    entity: str
    id: str
    version: int
    updated_at: str
    payload: dict[str, str | int | float | bool | None]


class SyncSpectrum(BaseModel):
    model_config = ConfigDict(strict=True)

    id: str
    version: int
    updated_at: str
    point_count: int
    byte_length: int
    sha256: str
    download_path: str


class SyncChanges(BaseModel):
    model_config = ConfigDict(strict=True)

    next_cursor: str
    has_more: bool
    records: list[SyncRecord]
    spectra: list[SyncSpectrum]
    deleted: list[dict[str, str]]
