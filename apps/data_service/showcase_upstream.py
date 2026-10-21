"""Showcase 上游（web_manager :8787）代理。

环境变量 ``SHOWCASE_UPSTREAM``，默认 ``http://172.16.12.143:8787``。
桌面只打 data_service；写路径与曲线/图片等统一经此转发，保证规则与现网一致。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_UPSTREAM = "http://172.16.12.143:8787"
_DEFAULT_TIMEOUT = 60.0


def upstream_base() -> str:
    return (os.environ.get("SHOWCASE_UPSTREAM") or DEFAULT_UPSTREAM).rstrip("/")


def upstream_configured() -> bool:
    return bool(upstream_base())


def _url(path: str, query: dict[str, Any] | None = None) -> str:
    base = upstream_base()
    if not path.startswith("/"):
        path = "/" + path
    if query:
        cleaned = {k: v for k, v in query.items() if v is not None and v != ""}
        if cleaned:
            return f"{base}{path}?{urllib.parse.urlencode(cleaned)}"
    return f"{base}{path}"


def proxy_json(
    method: str,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    body: dict[str, Any] | bytes | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[int, Any]:
    """转发 JSON；返回 (status_code, parsed_json_or_text)。"""
    data: bytes | None = None
    headers = {"Accept": "application/json"}
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
            headers["Content-Type"] = "application/octet-stream"
        else:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        _url(path, query),
        data=data,
        headers=headers,
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "json" in ctype or raw[:1] in (b"{", b"["):
                try:
                    return int(resp.status), json.loads(raw.decode("utf-8"))
                except Exception:
                    return int(resp.status), raw.decode("utf-8", errors="replace")
            return int(resp.status), raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {"detail": raw.decode("utf-8", errors="replace") or str(exc.reason)}
        return int(exc.code), payload
    except Exception as exc:  # noqa: BLE001
        return 502, {"detail": f"上游不可达 ({upstream_base()}): {type(exc).__name__}: {exc}"}


def proxy_bytes(
    path: str,
    *,
    query: dict[str, Any] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[int, bytes, str]:
    """转发二进制；返回 (status, body, content_type)。"""
    req = urllib.request.Request(_url(path, query), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (
                int(resp.status),
                resp.read(),
                resp.headers.get("Content-Type") or "application/octet-stream",
            )
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(), "application/octet-stream"
    except Exception as exc:  # noqa: BLE001
        msg = f"上游不可达 ({upstream_base()}): {type(exc).__name__}: {exc}"
        return 502, msg.encode("utf-8"), "text/plain; charset=utf-8"


def normalize_spectrum_row(row: dict[str, Any]) -> dict[str, Any]:
    """把上游列表行规范成 BCSAP ShowcaseSpectrumSummary 字段。"""
    sid = int(row.get("spectrum_id") or row.get("id") or 0)
    element_set = row.get("element_set")
    metal = row.get("metal")
    if not metal and element_set:
        from .showcase_repo import _metal_from_element_set, isotope_class

        metal = _metal_from_element_set(str(element_set))
    else:
        from .showcase_repo import isotope_class

    return {
        "spectrum_id": sid,
        "name": str(row.get("name") or ""),
        "metal": metal if isinstance(metal, str) else None,
        "sample_name": row.get("sample_name"),
        "element_set": element_set,
        "status": row.get("status"),
        "isotope_class": row.get("isotope_class")
        or isotope_class(metal if isinstance(metal, str) else None),
        "n_annotations": int(
            row.get("n_annotations")
            or row.get("annotation_count")
            or 0
        ),
        "peak_count": int(row.get("peak_count") or 0),
        "score": row.get("score"),
        "rank": row.get("rank"),
        "chip": row.get("chip"),
    }
