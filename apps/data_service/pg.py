"""PostgreSQL 连接（Showcase / 中央数据只读入口）。

DSN 优先级：
1. SPECTRUM_PG_DSN / MS_PG_DSN
2. 本仓或上级目录 db_config.json 的 pg_dsn
3. 本机默认 cluster_ms:5433
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DSN = (
    "host=127.0.0.1 port=5433 dbname=cluster_ms user=cluster password=cluster"
)

_ROOT = Path(__file__).resolve().parents[2]
_PROJECT = Path(__file__).resolve().parents[3]


def resolve_dsn() -> str:
    for key in ("SPECTRUM_PG_DSN", "MS_PG_DSN"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return raw
    for cfg in (_ROOT / "db_config.json", _PROJECT / "db_config.json"):
        if not cfg.is_file():
            continue
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            dsn = (data.get("pg_dsn") or "").strip()
            if dsn:
                return dsn
        except Exception:
            continue
    return DEFAULT_DSN


def _connect(dsn: str | None = None):
    target = dsn or resolve_dsn()
    try:
        import psycopg2

        return psycopg2.connect(target)
    except ImportError:
        pass
    try:
        import psycopg

        return psycopg.connect(target)
    except ImportError as e:
        raise RuntimeError(
            "需要 psycopg2 或 psycopg：pip install -e '.[database]' 或 psycopg2-binary"
        ) from e


@contextmanager
def pg_conn(*, dsn: str | None = None) -> Iterator[Any]:
    conn = _connect(dsn)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def ping() -> dict[str, Any]:
    """探测库是否可达；成功时带回 spectra 行数。"""
    dsn = resolve_dsn()
    try:
        with pg_conn(dsn=dsn) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM spectra")
            n = int(cur.fetchone()[0])
        return {"ok": True, "dsn_hint": _dsn_hint(dsn), "n_spectra": n}
    except Exception as e:
        return {
            "ok": False,
            "dsn_hint": _dsn_hint(dsn),
            "error": f"{type(e).__name__}: {e}",
        }


def _dsn_hint(dsn: str) -> str:
    """日志用不含密码的摘要。"""
    parts: dict[str, str] = {}
    for tok in dsn.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            parts[k] = v
    host = parts.get("host", "?")
    port = parts.get("port", "?")
    db = parts.get("dbname", "?")
    user = parts.get("user", "?")
    return f"{user}@{host}:{port}/{db}"
