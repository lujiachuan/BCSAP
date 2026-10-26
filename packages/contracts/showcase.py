"""团簇谱 showcase 契约（谱图库 / 标注 / 质量轴校正摘要）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


AxisPolicy = Literal[
    "showcase_mono_ladder",
    "showcase_multi_envelope",
    "showcase_atom_shift",
    "showcase_alni_soft_shift",
    "use_safe_library_resid",
    "block_unsafe_resid",
    "no_resid",
    "unconfigured",
]

IsotopeClass = Literal["mono", "dual", "multi", "xe", "unknown"]


class ShowcaseStatus(BaseModel):
    """Showcase 模块就绪状态。"""

    model_config = ConfigDict(strict=True)

    service: str = "data-service-showcase"
    status: Literal["ready", "degraded", "unavailable"]
    detail: str | None = None
    postgres_configured: bool = False
    algorithms_vendored: bool = False
    n_spectra: int | None = None
    dsn_hint: str | None = None
    upstream: str | None = None
    upstream_ok: bool | None = None


class ShowcaseSpectrumSummary(BaseModel):
    """谱图库列表行。"""

    model_config = ConfigDict(strict=True)

    spectrum_id: int
    name: str
    metal: str | None = None
    sample_name: str | None = None
    element_set: str | None = None
    status: str | None = None
    isotope_class: IsotopeClass = "unknown"
    n_annotations: int = 0
    peak_count: int = 0
    score: float | None = None
    rank: int | None = None
    chip: str | None = None


class ShowcaseSpectrumList(BaseModel):
    """谱图目录。"""

    model_config = ConfigDict(strict=True)

    items: list[ShowcaseSpectrumSummary] = Field(default_factory=list)
    total: int = 0
    detail: str | None = None


class ShowcasePeakAnnotation(BaseModel):
    """单峰标注（working lane）。"""

    model_config = ConfigDict(strict=True)

    peak_mass: float
    formula: str | None = None
    err: float | None = None
    rank_in_peak: int = 1
    intensity: float | None = None


class ShowcaseSpectrumMeta(BaseModel):
    """单条谱图元数据 + 轴策略摘要。"""

    model_config = ConfigDict(strict=True)

    spectrum_id: int
    name: str
    metal: str | None = None
    sample_name: str | None = None
    element_set: str | None = None
    status: str | None = None
    isotope_class: IsotopeClass = "unknown"
    axis_policy: AxisPolicy = "unconfigured"
    axis_hint: str | None = None
    calibration_method: str | None = None
    peaks: list[ShowcasePeakAnnotation] = Field(default_factory=list)
    detail: str | None = None
    power: str | None = None
    ar_flow: str | None = None
    he_flow: str | None = None
    pressure: str | None = None
    condensation_distance: str | None = None
    notes: str | None = None


class ShowcaseCurve(BaseModel):
    """谱曲线（可降采样）。"""

    model_config = ConfigDict(strict=True)

    spectrum_id: int
    name: str | None = None
    x: list[float] = Field(default_factory=list)
    y: list[float] = Field(default_factory=list)
    n_points: int = 0
    n_full: int | None = None
    downsampled: bool = False
    lane: str | None = None
    detail: str | None = None


class ShowcaseImageItem(BaseModel):
    """谱图附属图片（长图 / 分幅）。"""

    model_config = ConfigDict(strict=True)

    id: int | None = None
    name: str
    kind: str | None = None
    mime: str | None = None
    byte_size: int | None = None
    rel_path: str | None = None
    url: str | None = None
    created_at: str | None = None


class ShowcaseImageList(BaseModel):
    model_config = ConfigDict(strict=True)

    spectrum_id: int
    items: list[ShowcaseImageItem] = Field(default_factory=list)
    detail: str | None = None


class ShowcaseFacets(BaseModel):
    """筛选分面。"""

    model_config = ConfigDict(strict=True)

    statuses: list[dict[str, Any]] = Field(default_factory=list)
    element_sets: list[dict[str, Any]] = Field(default_factory=list)
    detail: str | None = None


class ShowcaseGood3(BaseModel):
    """Good3 / 质量排序结果。"""

    model_config = ConfigDict(strict=True)

    ok: bool = True
    updated_at: str | None = None
    weights: dict[str, float] = Field(default_factory=dict)
    elements: list[str] = Field(default_factory=list)
    items: list[ShowcaseSpectrumSummary] = Field(default_factory=list)
    detail: str | None = None


class ShowcaseJobAccepted(BaseModel):
    """异步写任务受理。"""

    model_config = ConfigDict(strict=True)

    ok: bool = True
    async_job: bool = True
    job_id: str | None = None
    spectrum_id: int | None = None
    detail: str | None = None
    raw: dict[str, Any] | None = None
