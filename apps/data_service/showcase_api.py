"""团簇谱 showcase 只读 API：直连现网 cluster_ms。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from packages.contracts import (
    ShowcaseSpectrumList,
    ShowcaseSpectrumMeta,
    ShowcaseSpectrumSummary,
    ShowcaseStatus,
    ShowcasePeakAnnotation,
)

from . import showcase_repo

router = APIRouter(prefix="/api/v1/showcase", tags=["showcase"])


@router.get("/status", response_model=ShowcaseStatus)
def showcase_status() -> ShowcaseStatus:
    raw = showcase_repo.module_status()
    return ShowcaseStatus(**raw)


@router.get("/spectra", response_model=ShowcaseSpectrumList)
def list_spectra(
    q: str | None = Query(default=None, description="名称/样品/元素集关键字"),
    element_set: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> ShowcaseSpectrumList:
    try:
        raw = showcase_repo.list_spectra(q=q, element_set=element_set, limit=limit)
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"PostgreSQL 不可用: {type(e).__name__}: {e}",
        ) from e
    items = [ShowcaseSpectrumSummary(**row) for row in raw["items"]]
    return ShowcaseSpectrumList(
        items=items,
        total=int(raw.get("total") or len(items)),
        detail=raw.get("detail"),
    )


@router.get("/spectra/{spectrum_id}", response_model=ShowcaseSpectrumMeta)
def get_spectrum_meta(spectrum_id: int) -> ShowcaseSpectrumMeta:
    try:
        raw = showcase_repo.get_spectrum_meta(spectrum_id)
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"PostgreSQL 不可用: {type(e).__name__}: {e}",
        ) from e
    if raw is None:
        raise HTTPException(status_code=404, detail=f"谱图不存在: {spectrum_id}")
    peaks = [ShowcasePeakAnnotation(**p) for p in raw.get("peaks") or []]
    body = {k: v for k, v in raw.items() if k != "peaks"}
    return ShowcaseSpectrumMeta(**body, peaks=peaks)
