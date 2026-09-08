"""模拟阶段的数据同步目录；后续由数据库查询实现替换。"""

import math

import numpy as np

from packages.spectrum import encode_spectrum, spectrum_checksum

SYNC_CURSOR = "demo-20260908-1"
UPDATED_AT = "2026-09-08T08:00:00+00:00"

EXPERIMENTS = (
    {
        "id": "EXP-20260907-017",
        "sample": "Cu-Ar-022",
        "type": "磁场扫谱",
        "status": "已归档",
        "completed_at": "2026-09-07T13:48:00+08:00",
    },
    {
        "id": "EXP-20260907-016",
        "sample": "Blank-006",
        "type": "磁场扫谱",
        "status": "已归档",
        "completed_at": "2026-09-07T11:26:00+08:00",
    },
    {
        "id": "EXP-20260906-042",
        "sample": "Cu-Ar-021",
        "type": "自动调束",
        "status": "已完成",
        "completed_at": "2026-09-06T17:32:00+08:00",
    },
)


def _build_spectrum(scale: float) -> bytes:
    x = np.linspace(10.0, 120.0, 2_001)
    y = np.full_like(x, 3.0)
    for center, height, width in (
        (40.0, 92.0, 0.8),
        (68.0, 70.0, 1.3),
        (84.0, 42.0, 0.9),
        (112.0, 55.0, 1.1),
    ):
        y += scale * height * np.exp(-((x - center) ** 2) / (2 * math.pow(width, 2)))
    return encode_spectrum(x, y)


SPECTRUM_CONTENT = {
    "SPEC-20260907-017": _build_spectrum(1.0),
    "SPEC-20260907-016": _build_spectrum(0.12),
    "SPEC-20260906-042": _build_spectrum(0.82),
}


def sync_records() -> list[dict]:
    return [
        {
            "entity": "experiment",
            "id": experiment["id"],
            "version": 1,
            "updated_at": UPDATED_AT,
            "payload": experiment,
        }
        for experiment in EXPERIMENTS
    ]


def sync_spectra() -> list[dict]:
    rows = []
    for spectrum_id, payload in SPECTRUM_CONTENT.items():
        rows.append(
            {
                "id": spectrum_id,
                "version": 1,
                "updated_at": UPDATED_AT,
                "point_count": 2_001,
                "byte_length": len(payload),
                "sha256": spectrum_checksum(payload),
                "download_path": f"/api/v1/spectra/{spectrum_id}/content",
            }
        )
    return rows
