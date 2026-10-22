"""团簇谱 showcase API：读优先本机 PG，写与曲线/图片/排序代理上游 web_manager。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response

from packages.contracts import (
    ShowcaseCurve,
    ShowcaseFacets,
    ShowcaseGood3,
    ShowcaseImageItem,
    ShowcaseImageList,
    ShowcaseJobAccepted,
    ShowcasePeakAnnotation,
    ShowcaseSpectrumList,
    ShowcaseSpectrumMeta,
    ShowcaseSpectrumSummary,
    ShowcaseStatus,
)

from . import showcase_repo
from . import showcase_upstream as upstream

router = APIRouter(prefix="/api/v1/showcase", tags=["showcase"])


def _raise_upstream(status: int, payload: Any) -> None:
    detail: Any
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("error") or payload
    else:
        detail = payload
    raise HTTPException(status_code=status if status >= 400 else 502, detail=detail)


def _summary_items(rows: list[dict[str, Any]]) -> list[ShowcaseSpectrumSummary]:
    out: list[ShowcaseSpectrumSummary] = []
    for row in rows:
        norm = upstream.normalize_spectrum_row(row)
        # drop extra keys not in model
        data = {
            k: norm[k]
            for k in (
                "spectrum_id",
                "name",
                "metal",
                "sample_name",
                "element_set",
                "status",
                "isotope_class",
                "n_annotations",
                "peak_count",
                "score",
                "rank",
                "chip",
            )
            if k in norm
        }
        out.append(ShowcaseSpectrumSummary(**data))
    return out


@router.get("/status", response_model=ShowcaseStatus)
def showcase_status() -> ShowcaseStatus:
    raw = showcase_repo.module_status()
    up = upstream.upstream_base()
    code, payload = upstream.proxy_json("GET", "/api/spectra", query={"limit": 1}, timeout=5.0)
    upstream_ok = code == 200
    base_detail = str(raw.get("detail") or "")
    if upstream_ok:
        detail = f"{base_detail} · 上游 {up} 可达".strip(" ·")
    else:
        detail = f"{base_detail} · 上游 {up} 不可达".strip(" ·")
    fields = {
        k: v
        for k, v in raw.items()
        if k in ShowcaseStatus.model_fields and k not in {"detail", "upstream", "upstream_ok"}
    }
    return ShowcaseStatus(
        **fields,
        detail=detail,
        upstream=up,
        upstream_ok=upstream_ok,
    )


@router.get("/facets", response_model=ShowcaseFacets)
def showcase_facets() -> ShowcaseFacets:
    code, payload = upstream.proxy_json("GET", "/api/search/facets")
    if code >= 400 or not isinstance(payload, dict):
        _raise_upstream(code, payload)
    return ShowcaseFacets(
        statuses=list(payload.get("statuses") or []),
        element_sets=list(payload.get("element_sets") or []),
        detail=f"via {upstream.upstream_base()}",
    )


@router.get("/spectra", response_model=ShowcaseSpectrumList)
def list_spectra(
    q: str | None = Query(default=None, description="名称/样品/元素集关键字"),
    element_set: str | None = Query(default=None),
    status: str | None = Query(default=None, description="队列状态，如 annotated/imported"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ShowcaseSpectrumList:
    # 上游列表支持更完整筛选；PG 作回退
    query: dict[str, Any] = {"limit": limit, "offset": offset}
    if q:
        query["q"] = q
    if element_set:
        query["element_set"] = element_set
    if status:
        query["status"] = status
    code, payload = upstream.proxy_json("GET", "/api/spectra", query=query)
    if code == 200 and isinstance(payload, dict):
        items = _summary_items(list(payload.get("items") or []))
        total = int(payload.get("total") or len(items))
        return ShowcaseSpectrumList(
            items=items,
            total=total,
            detail=f"上游目录 · {upstream.upstream_base()}",
        )
    try:
        raw = showcase_repo.list_spectra(
            q=q, element_set=element_set, status=status, limit=limit, offset=offset
        )
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"PostgreSQL 与上游均不可用: {type(e).__name__}: {e}; upstream={payload}",
        ) from e
    items = [ShowcaseSpectrumSummary(**row) for row in raw["items"]]
    return ShowcaseSpectrumList(
        items=items,
        total=int(raw.get("total") or len(items)),
        detail=raw.get("detail"),
    )


@router.get("/spectra/rank", response_model=ShowcaseSpectrumList)
def rank_spectra(
    limit: int = Query(default=50, ge=1, le=500),
    element: str | None = Query(default=None),
) -> ShowcaseSpectrumList:
    query: dict[str, Any] = {"limit": limit}
    if element:
        query["element"] = element
    code, payload = upstream.proxy_json("GET", "/api/spectra/rank", query=query)
    if code >= 400 or not isinstance(payload, dict):
        _raise_upstream(code, payload)
    items = _summary_items(list(payload.get("items") or []))
    return ShowcaseSpectrumList(
        items=items,
        total=int(payload.get("total") or len(items)),
        detail="质量排序 · 上游",
    )


@router.get("/spectra/good3", response_model=ShowcaseGood3)
def good3_spectra(element: str | None = Query(default=None)) -> ShowcaseGood3:
    query = {"element": element} if element else None
    code, payload = upstream.proxy_json("GET", "/api/spectra/good3", query=query)
    if code >= 400 or not isinstance(payload, dict):
        _raise_upstream(code, payload)
    items = _summary_items(list(payload.get("items") or []))
    weights = payload.get("weights") or {}
    weight_f = {
        str(k): float(v)
        for k, v in weights.items()
        if isinstance(v, (int, float))
    }
    return ShowcaseGood3(
        ok=bool(payload.get("ok", True)),
        updated_at=str(payload.get("updated_at")) if payload.get("updated_at") else None,
        weights=weight_f,
        elements=[str(x) for x in (payload.get("elements") or [])],
        items=items,
        detail="Good3 · 上游",
    )


@router.get("/spectra/{spectrum_id}", response_model=ShowcaseSpectrumMeta)
def get_spectrum_meta(spectrum_id: int) -> ShowcaseSpectrumMeta:
    try:
        raw = showcase_repo.get_spectrum_meta(spectrum_id)
    except Exception:
        raw = None
    if raw is None:
        code, payload = upstream.proxy_json("GET", f"/api/spectra/{spectrum_id}")
        if code >= 400 or not isinstance(payload, dict):
            _raise_upstream(code if code >= 400 else 404, payload)
        # 上游详情字段与 meta 不完全一致：尽量映射
        from .showcase_repo import _metal_from_element_set, isotope_class

        el = payload.get("element_set")
        metal = _metal_from_element_set(str(el) if el else None)
        peaks_raw = payload.get("peaks") or []
        peaks = []
        for p in peaks_raw[:800]:
            if not isinstance(p, dict):
                continue
            mass = p.get("peak_mass") if p.get("peak_mass") is not None else p.get("mass")
            if mass is None:
                continue
            peaks.append(
                ShowcasePeakAnnotation(
                    peak_mass=float(mass),
                    formula=p.get("formula"),
                    err=None if p.get("err") is None else float(p["err"]),
                    rank_in_peak=int(p.get("rank_in_peak") or 1),
                    intensity=(
                        None
                        if p.get("intensity") is None and p.get("peak_height") is None
                        else float(p.get("intensity") or p.get("peak_height"))
                    ),
                )
            )
        return ShowcaseSpectrumMeta(
            spectrum_id=int(payload.get("id") or spectrum_id),
            name=str(payload.get("name") or ""),
            metal=metal,
            sample_name=payload.get("sample_name"),
            element_set=el if isinstance(el, str) else None,
            status=payload.get("status"),
            isotope_class=isotope_class(metal),
            peaks=peaks,
            power=str(payload["power"]) if payload.get("power") not in (None, "") else None,
            ar_flow=str(payload["ar_flow"]) if payload.get("ar_flow") not in (None, "") else None,
            he_flow=str(payload["he_flow"]) if payload.get("he_flow") not in (None, "") else None,
            pressure=str(payload["pressure"]) if payload.get("pressure") not in (None, "") else None,
            condensation_distance=(
                str(payload["condensation_distance"])
                if payload.get("condensation_distance") not in (None, "")
                else None
            ),
            notes=str(payload["notes"]) if payload.get("notes") not in (None, "") else None,
            detail="上游详情",
        )
    peaks = [ShowcasePeakAnnotation(**p) for p in raw.get("peaks") or []]
    body = {k: v for k, v in raw.items() if k != "peaks"}
    return ShowcaseSpectrumMeta(**body, peaks=peaks)


@router.get("/spectra/{spectrum_id}/curve", response_model=ShowcaseCurve)
def get_spectrum_curve(spectrum_id: int) -> ShowcaseCurve:
    code, payload = upstream.proxy_json("GET", f"/api/spectra/{spectrum_id}/curve")
    if code >= 400 or not isinstance(payload, dict):
        _raise_upstream(code, payload)
    x = [float(v) for v in (payload.get("x") or [])]
    y = [float(v) for v in (payload.get("y") or [])]
    return ShowcaseCurve(
        spectrum_id=int(payload.get("id") or spectrum_id),
        name=str(payload.get("name")) if payload.get("name") else None,
        x=x,
        y=y,
        n_points=int(payload.get("n_points") or len(x)),
        n_full=None if payload.get("n_full") is None else int(payload["n_full"]),
        downsampled=bool(payload.get("downsampled")),
        lane=str(payload.get("lane")) if payload.get("lane") else None,
        detail="上游曲线",
    )


@router.get("/spectra/{spectrum_id}/images", response_model=ShowcaseImageList)
def list_spectrum_images(spectrum_id: int) -> ShowcaseImageList:
    code, payload = upstream.proxy_json("GET", f"/api/spectra/{spectrum_id}/images")
    if code >= 400 or not isinstance(payload, dict):
        _raise_upstream(code, payload)
    items: list[ShowcaseImageItem] = []
    names_seen: set[str] = set()
    for row in payload.get("items") or []:
        if not isinstance(row, dict):
            continue
        # 把上游相对 URL 改写为 data_service 代理路径，便于桌面同源拉取
        name = str(row.get("name") or "")
        if name:
            names_seen.add(name)
        proxy_url = f"/api/v1/showcase/spectra/{spectrum_id}/images/{name}" if name else None
        items.append(
            ShowcaseImageItem(
                id=None if row.get("id") is None else int(row["id"]),
                name=name,
                kind=str(row["kind"]) if row.get("kind") else None,
                mime=str(row["mime"]) if row.get("mime") else None,
                byte_size=None if row.get("byte_size") is None else int(row["byte_size"]),
                rel_path=str(row["rel_path"]) if row.get("rel_path") else None,
                url=proxy_url or (str(row["url"]) if row.get("url") else None),
                created_at=str(row["created_at"]) if row.get("created_at") else None,
            )
        )
    # peaks_*_long 常只在 annotate_data，不进 spectrum_images；用 Range 探活后补进目录
    peaks_name = f"peaks_{spectrum_id}_long.png"
    if peaks_name not in names_seen and upstream.proxy_exists(
        f"/api/annotate/images/{spectrum_id}/{peaks_name}"
    ):
        items.append(
            ShowcaseImageItem(
                id=None,
                name=peaks_name,
                kind="peaks_long",
                mime="image/png",
                byte_size=None,
                rel_path=None,
                url=f"/api/v1/showcase/spectra/{spectrum_id}/images/{peaks_name}",
                created_at=None,
            )
        )
    return ShowcaseImageList(spectrum_id=spectrum_id, items=items, detail="上游图片目录")


@router.get("/spectra/{spectrum_id}/images/{filename}")
def get_spectrum_image(spectrum_id: int, filename: str) -> Response:
    # 上游 annotate 图片路径
    path = f"/api/annotate/images/{spectrum_id}/{filename}"
    code, body, ctype = upstream.proxy_bytes(path)
    if code >= 400:
        raise HTTPException(status_code=code, detail=body.decode("utf-8", errors="replace"))
    return Response(content=body, media_type=ctype or "image/png")


# ---- 写路径：一律代理上游 ----


@router.post("/spectra/good3/refresh")
def good3_refresh(body: dict[str, Any] | None = Body(default=None)) -> Any:
    code, payload = upstream.proxy_json(
        "POST", "/api/spectra/good3/refresh", body=body or {}
    )
    if code >= 400:
        _raise_upstream(code, payload)
    return payload


@router.post("/spectra/infer-elements")
def infer_elements(body: dict[str, Any] | None = Body(default=None)) -> Any:
    code, payload = upstream.proxy_json(
        "POST", "/api/spectra/infer-elements", body=body or {}
    )
    if code >= 400:
        _raise_upstream(code, payload)
    return payload


@router.post("/spectra/scan-element-suspects")
def scan_element_suspects(body: dict[str, Any] | None = Body(default=None)) -> Any:
    code, payload = upstream.proxy_json(
        "POST", "/api/spectra/scan-element-suspects", body=body or {}
    )
    if code >= 400:
        _raise_upstream(code, payload)
    return payload


@router.post("/spectra/{spectrum_id}/annotate", response_model=ShowcaseJobAccepted)
def annotate_spectrum(
    spectrum_id: int, body: dict[str, Any] | None = Body(default=None)
) -> ShowcaseJobAccepted:
    payload_in = dict(body or {})
    payload_in.setdefault("async", True)
    payload_in.setdefault("draw", True)
    code, payload = upstream.proxy_json(
        "POST", f"/api/spectra/{spectrum_id}/annotate", body=payload_in, timeout=120.0
    )
    if code >= 400 or not isinstance(payload, dict):
        _raise_upstream(code, payload)
    return ShowcaseJobAccepted(
        ok=bool(payload.get("ok", True)),
        async_job=bool(payload.get("async", True)),
        job_id=str(payload["job_id"]) if payload.get("job_id") else None,
        spectrum_id=int(payload.get("spectrum_id") or spectrum_id),
        detail="annotate 已提交上游",
        raw=payload,
    )


@router.post("/spectra/{spectrum_id}/peaks/confirm")
def peaks_confirm(
    spectrum_id: int, body: dict[str, Any] | None = Body(default=None)
) -> Any:
    code, payload = upstream.proxy_json(
        "POST",
        f"/api/spectra/{spectrum_id}/peaks/confirm",
        body=body or {},
        timeout=120.0,
    )
    if code >= 400:
        _raise_upstream(code, payload)
    return payload


@router.get("/annotate/jobs/{job_id}")
def annotate_job(job_id: str) -> Any:
    code, payload = upstream.proxy_json("GET", f"/api/annotate/jobs/{job_id}")
    if code >= 400:
        _raise_upstream(code, payload)
    return payload


@router.get("/peaks/jobs/{job_id}")
def peaks_job(job_id: str) -> Any:
    code, payload = upstream.proxy_json("GET", f"/api/peaks/jobs/{job_id}")
    if code >= 400:
        _raise_upstream(code, payload)
    return payload


@router.get("/correct/tools")
def correct_tools() -> Any:
    code, payload = upstream.proxy_json("GET", "/api/correct/tools")
    if code >= 400:
        _raise_upstream(code, payload)
    return payload


@router.post("/correct/propose")
def correct_propose(body: dict[str, Any] | None = Body(default=None)) -> Any:
    code, payload = upstream.proxy_json(
        "POST", "/api/correct/propose", body=body or {}, timeout=120.0
    )
    if code >= 400:
        _raise_upstream(code, payload)
    return payload


@router.post("/correct/run")
def correct_run(body: dict[str, Any] | None = Body(default=None)) -> Any:
    code, payload = upstream.proxy_json(
        "POST", "/api/correct/run", body=body or {}, timeout=180.0
    )
    if code >= 400:
        _raise_upstream(code, payload)
    return payload


@router.get("/correct/jobs/{job_id}")
def correct_job(job_id: str) -> Any:
    code, payload = upstream.proxy_json("GET", f"/api/correct/jobs/{job_id}")
    if code >= 400:
        _raise_upstream(code, payload)
    return payload


@router.get("/export/pack")
def export_pack(request: Request) -> Response:
    # 透传查询串
    qs = dict(request.query_params)
    path = "/api/export/pack"
    if qs:
        from urllib.parse import urlencode

        path = f"{path}?{urlencode(qs)}"
    code, body, ctype = upstream.proxy_bytes(path, timeout=300.0)
    if code >= 400:
        raise HTTPException(
            status_code=code, detail=body.decode("utf-8", errors="replace")
        )
    return Response(
        content=body,
        media_type=ctype or "application/zip",
        headers={"Content-Disposition": "attachment; filename=showcase_pack.zip"},
    )


@router.post("/proxy/{path:path}")
async def proxy_post_catchall(path: str, request: Request) -> Any:
    """兜底：桌面可把未单独封装的上游 POST 经此转发（path 相对 /api/）。"""
    import json

    raw = await request.body()
    body: Any = {}
    if raw:
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            raise HTTPException(status_code=400, detail="请求体需为 JSON") from None
    code, payload = upstream.proxy_json("POST", f"/api/{path}", body=body)
    if code >= 400:
        _raise_upstream(code, payload)
    return payload
