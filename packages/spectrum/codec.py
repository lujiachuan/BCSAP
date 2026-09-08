"""第一版谱图 NPZ 二进制格式。"""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO

import numpy as np
from numpy.typing import ArrayLike, NDArray


FORMAT_VERSION = 1


class SpectrumFormatError(ValueError):
    """谱图数据不能按平台格式解释。"""


def encode_spectrum(x: ArrayLike, y: ArrayLike) -> bytes:
    """将一维 x/y 数组编码为不包含 Python 对象的 NPZ。"""

    x_array = np.asarray(x, dtype="<f8")
    y_array = np.asarray(y, dtype="<f8")
    _validate_arrays(x_array, y_array)

    buffer = BytesIO()
    np.savez(
        buffer,
        format_version=np.asarray([FORMAT_VERSION], dtype="<u2"),
        x=x_array,
        y=y_array,
    )
    return buffer.getvalue()


def decode_spectrum(payload: bytes) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """解码谱图，并拒绝对象数组和未知格式版本。"""

    try:
        with np.load(BytesIO(payload), allow_pickle=False) as archive:
            if set(archive.files) != {"format_version", "x", "y"}:
                raise SpectrumFormatError("谱图包字段不完整或包含未知字段")
            version = int(archive["format_version"][0])
            if version != FORMAT_VERSION:
                raise SpectrumFormatError(f"不支持的谱图格式版本：{version}")
            x_array = np.asarray(archive["x"], dtype="<f8")
            y_array = np.asarray(archive["y"], dtype="<f8")
    except SpectrumFormatError:
        raise
    except (OSError, ValueError, KeyError, IndexError) as exc:
        raise SpectrumFormatError("无法读取谱图二进制数据") from exc

    _validate_arrays(x_array, y_array)
    return x_array, y_array


def spectrum_checksum(payload: bytes) -> str:
    """返回用于上传校验和幂等判断的 SHA-256 十六进制值。"""

    return sha256(payload).hexdigest()


def _validate_arrays(x_array: NDArray[np.float64], y_array: NDArray[np.float64]) -> None:
    if x_array.ndim != 1 or y_array.ndim != 1:
        raise SpectrumFormatError("谱图 x/y 必须是一维数组")
    if x_array.size == 0:
        raise SpectrumFormatError("谱图不能为空")
    if x_array.size != y_array.size:
        raise SpectrumFormatError("谱图 x/y 点数不一致")
    if not np.isfinite(x_array).all() or not np.isfinite(y_array).all():
        raise SpectrumFormatError("谱图包含非有限数值")

