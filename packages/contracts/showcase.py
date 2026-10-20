"""团簇谱 showcase 契约（谱图库 / 标注 / 质量轴校正摘要）。"""

from __future__ import annotations

from typing import Literal

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
