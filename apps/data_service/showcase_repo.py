"""Showcase 只读查询：现网 cluster_ms。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from .pg import pg_conn, ping, resolve_dsn

# 与 pdf_watch / web_manager 分类对齐的精简表（不 import 旁路包）
_FINGERPRINT = frozenset(
    {"Ag", "Cu", "Ga", "Cl", "Br", "Eu", "Sb", "Re", "Ir", "Tl", "Li"}
)
_MULTI = frozenset({"Ni", "Mo", "W", "Fe", "Zn", "Ti", "Cr", "Pt"})
_MONO = frozenset(
    {
        "Au",
        "Al",
        "Co",
        "As",
        "Mn",
        "Sc",
        "Y",
        "Nb",
        "Rh",
        "Tb",
        "Ho",
        "Tm",
        "Ta",
        "Bi",
        "F",
        "Na",
        "P",
        "V",
    }
)

_SIDE_DIR = Path(__file__).resolve().parents[3] / "annotate_data" / "_showcase_axis_safe"

_METHOD_TO_POLICY = (
    ("multi_envelope", "showcase_multi_envelope"),
    ("ni_envelope", "showcase_multi_envelope"),
    ("dual_fingerprint", "use_safe_library_resid"),
    ("leftmost", "use_safe_library_resid"),
    ("mono", "use_safe_library_resid"),
)


def _dicts(cur) -> list[dict[str, Any]]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _metal_from_element_set(element_set: str | None) -> Optional[str]:
    raw = (element_set or "").strip()
    if not raw:
        return None
    # 「Cu、O、H」或「Ag,O,H」
    parts = re.split(r"[,，、\s]+", raw)
    skip = {"O", "H", "Ar", "He", "Ne", "Kr", "N", "Xe"}
    for p in parts:
        tok = p.strip()
        if tok and tok[0].isupper() and tok not in skip:
            return tok
    return None


def isotope_class(metal: str | None) -> str:
    if not metal:
        return "unknown"
    if metal == "Xe":
        return "xe"
    if metal in _FINGERPRINT:
        return "dual"
    if metal in _MULTI:
        return "multi"
    if metal in _MONO:
        return "mono"
    return "unknown"


def _axis_sidecar(spectrum_id: int) -> dict[str, Any]:
    path = _SIDE_DIR / f"{int(spectrum_id)}.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _policy_from_method(method: str | None) -> str:
    m = (method or "").lower()
    for needle, policy in _METHOD_TO_POLICY:
        if needle in m:
            return policy
    return "unconfigured"


def list_spectra(
    *,
    q: str | None = None,
    element_set: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    where = " WHERE 1=1"
    params: list[Any] = []
    if q:
        where += """ AND (s.name ILIKE %s OR COALESCE(s.sample_name,'') ILIKE %s
                   OR COALESCE(s.element_set,'') ILIKE %s OR COALESCE(s.notes,'') ILIKE %s)"""
        kw = f"%{q.strip()}%"
        params.extend([kw, kw, kw, kw])
    if element_set:
        where += " AND COALESCE(s.element_set,'') ILIKE %s"
        params.append(f"%{element_set.strip()}%")
    if status:
        where += " AND COALESCE(s.status,'') = %s"
        params.append(status.strip())

    count_sql = f"SELECT COUNT(*) FROM spectra s{where}"
    sql = f"""
        SELECT s.id, s.name, s.sample_name, s.element_set, s.status,
               COALESCE(s.annotation_count, 0) AS annotation_count,
               COALESCE(s.peak_count, 0) AS peak_count
        FROM spectra s
        {where}
        ORDER BY s.updated_at DESC NULLS LAST, s.id DESC
        LIMIT %s OFFSET %s
    """
    list_params = list(params) + [limit, offset]

    with pg_conn() as conn:
        cur = conn.cursor()
        cur.execute(count_sql, params)
        total = int(cur.fetchone()[0])
        cur.execute(sql, list_params)
        rows = _dicts(cur)

    items = []
    for r in rows:
        metal = _metal_from_element_set(r.get("element_set"))
        items.append(
            {
                "spectrum_id": int(r["id"]),
                "name": str(r.get("name") or ""),
                "metal": metal,
                "sample_name": r.get("sample_name"),
                "element_set": r.get("element_set"),
                "status": r.get("status"),
                "isotope_class": isotope_class(metal),
                "n_annotations": int(r.get("annotation_count") or 0),
                "peak_count": int(r.get("peak_count") or 0),
            }
        )
    return {
        "items": items,
        "total": total,
        "detail": f"cluster_ms 只读 · DSN {ping().get('dsn_hint')}",
    }


def get_spectrum_meta(spectrum_id: int) -> Optional[dict[str, Any]]:
    sid = int(spectrum_id)
    with pg_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, name, sample_name, element_set, status,
                   COALESCE(annotation_count, 0) AS annotation_count
            FROM spectra WHERE id=%s
            """,
            (sid,),
        )
        rows = _dicts(cur)
        if not rows:
            return None
        row = rows[0]

        cur.execute(
            """
            SELECT metrics_json, segments_json
            FROM spectrum_ag_calibrations
            WHERE spectrum_id=%s
            ORDER BY id DESC LIMIT 1
            """,
            (sid,),
        )
        cal_rows = _dicts(cur)
        metrics: dict[str, Any] = {}
        if cal_rows:
            mj = cal_rows[0].get("metrics_json")
            if isinstance(mj, str):
                try:
                    mj = json.loads(mj)
                except Exception:
                    mj = {}
            if isinstance(mj, dict):
                metrics = mj

        cur.execute(
            """
            SELECT peak_mass, formula, err, rank_in_peak, peak_height
            FROM spectrum_annotations
            WHERE spectrum_id=%s AND lane='working'
              AND COALESCE(superseded, false)=false
              AND rank_in_peak=1
            ORDER BY peak_mass
            LIMIT 800
            """,
            (sid,),
        )
        ann_rows = _dicts(cur)

    # 列表与详情一致：主金属取元素集第一个金属（MoAg → Mo）。
    # calibration metrics 里的 metal 可能是双同位素锁（Ag），只作校正线索。
    metal = _metal_from_element_set(row.get("element_set"))
    cal_metal = metrics.get("metal") if isinstance(metrics.get("metal"), str) else None
    if metal is None:
        metal = cal_metal
    method = metrics.get("method")
    side = _axis_sidecar(sid)
    policy = side.get("policy") or _policy_from_method(method if isinstance(method, str) else None)
    hint = side.get("mono_hint") or side.get("hint") or metrics.get("hint")
    if cal_metal and metal and cal_metal != metal and not hint:
        hint = f"校正锁金属={cal_metal}"

    peaks = [
        {
            "peak_mass": float(a["peak_mass"]),
            "formula": a.get("formula"),
            "err": None if a.get("err") is None else float(a["err"]),
            "rank_in_peak": int(a.get("rank_in_peak") or 1),
            "intensity": None
            if a.get("peak_height") is None
            else float(a["peak_height"]),
        }
        for a in ann_rows
    ]

    return {
        "spectrum_id": sid,
        "name": str(row.get("name") or ""),
        "metal": metal if isinstance(metal, str) else None,
        "sample_name": row.get("sample_name"),
        "element_set": row.get("element_set"),
        "status": row.get("status"),
        "isotope_class": isotope_class(metal if isinstance(metal, str) else None),
        "axis_policy": policy if isinstance(policy, str) else "unconfigured",
        "axis_hint": hint if isinstance(hint, str) else None,
        "calibration_method": method if isinstance(method, str) else None,
        "peaks": peaks,
        "detail": f"working 标注 {len(peaks)} 条 · DSN {ping().get('dsn_hint')}",
    }


def module_status() -> dict[str, Any]:
    info = ping()
    if info.get("ok"):
        return {
            "status": "ready",
            "detail": f"已连接 {info.get('dsn_hint')}，spectra={info.get('n_spectra')}",
            "postgres_configured": True,
            "algorithms_vendored": False,
            "n_spectra": info.get("n_spectra"),
            "dsn_hint": info.get("dsn_hint"),
        }
    return {
        "status": "degraded",
        "detail": info.get("error") or "PostgreSQL 不可达",
        "postgres_configured": False,
        "algorithms_vendored": False,
        "n_spectra": None,
        "dsn_hint": info.get("dsn_hint") or resolve_dsn(),
    }
