"""受控设备 PV 映射配置的加载、校验与持久化。

背景：PV 名原先写死在三个地方（执行服务的 pv_health、设置页示例表、调束页参数表），
改现场 PV 要改代码。本模块把「业务信号 → PV」变成**唯一配置源**：

* 由执行服务持有并校验（架构文档 6.2：启动时完整校验）；
* 客户端通过 ``/control/v1/pv-mapping`` 读写，不直接碰文件；
* 落盘为 JSON，位置可用 ``SPECTRUM_PV_MAPPING`` 覆盖（默认用户配置目录）。

安全边界：允许配置任意 PV 是现场接入的硬需求，但写入真实设备仍受
「网关模式 + 只读判定 + 业务参数边界」三重约束，见 README 的说明。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from packages.contracts import (
    PvMappingConfig,
    PvMappingEntry,
    PvMappingIssue,
)

CONFIG_VERSION = 1

# 业务信号键：小写点分，供执行服务内部引用，落库、日志、审计都用它。
_SIGNAL_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")
# EPICS PV 名：允许字母数字与 : . - _ [ ] $ + < >，禁止空白与空串。
_PV_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_:.\-\[\]$+<>]*$")

# 现场接入前的默认映射（与历史 CONTROLLED_SIGNALS 一致，补上中文显示名）。
DEFAULT_ENTRIES: tuple[PvMappingEntry, ...] = (
    PvMappingEntry(
        signal="quadrupole.q1.current",
        label="Q1 电流",
        pv="BL:Q1:ISET",
        unit="A",
        writable=True,
        required=True,
    ),
    PvMappingEntry(
        signal="quadrupole.q2.current",
        label="Q2 电流",
        pv="BL:Q2:ISET",
        unit="A",
        writable=True,
        required=True,
    ),
    PvMappingEntry(
        signal="einzel.voltage",
        label="Einzel 电压",
        pv="BL:EL:VSET",
        unit="kV",
        writable=True,
        required=True,
    ),
    PvMappingEntry(
        signal="steerer.x",
        label="X 偏转",
        pv="BL:STEER:X",
        unit="V",
        writable=True,
        required=True,
    ),
    PvMappingEntry(
        signal="steerer.y",
        label="Y 偏转",
        pv="BL:STEER:Y",
        unit="V",
        writable=True,
        required=True,
    ),
    PvMappingEntry(
        signal="source.voltage",
        label="Source 电压",
        pv="BL:SRC:VSET",
        unit="kV",
        writable=True,
        required=False,
    ),
    PvMappingEntry(
        signal="detector.current",
        label="探测器电流",
        pv="BL:DET:CURRENT",
        unit="uA",
        writable=False,
        required=True,
    ),
)

# 模拟网关的初值：仅开发/演示用，真实接入时不参与任何判定。
DEFAULT_SIMULATED_VALUES: dict[str, float] = {
    "quadrupole.q1.current": 1.842,
    "quadrupole.q2.current": -0.625,
    "einzel.voltage": 3.20,
    "steerer.x": 0.08,
    "steerer.y": -0.12,
    "source.voltage": 12.4,
    "detector.current": 8.31,
}


def default_config() -> PvMappingConfig:
    return PvMappingConfig(
        version=CONFIG_VERSION,
        gateway="simulated",
        ca_lib_dir="",
        entries=list(DEFAULT_ENTRIES),
    )


def config_path() -> Path:
    """配置文件位置，可用 ``SPECTRUM_PV_MAPPING`` 覆盖。"""
    override = os.environ.get("SPECTRUM_PV_MAPPING")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) if appdata else Path.home() / ".config"
    return root / "SpectrumPlatform" / "pv_mapping.json"


def load_config() -> PvMappingConfig:
    """读取配置；文件不存在或损坏时回退默认值（不抛异常，保证服务可启动）。"""
    path = config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_config()
    except (OSError, json.JSONDecodeError):
        return default_config()
    try:
        return PvMappingConfig.model_validate(raw)
    except Exception:  # noqa: BLE001  历史/手改坏文件一律回退默认值
        return default_config()


def save_config(config: PvMappingConfig) -> Path:
    """原子写盘：先写同目录临时文件再替换，避免中途失败留下半截配置。"""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config.model_dump(), ensure_ascii=False, indent=2) + "\n"
    handle, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".pv_mapping-", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return path


def validate_config(config: PvMappingConfig) -> list[PvMappingIssue]:
    """逐行校验，返回全部问题（不是遇到第一个就返回），供界面逐行标红。

    index 为 entries 行号，-1 表示整体配置问题。
    """
    issues: list[PvMappingIssue] = []

    if not config.entries:
        issues.append(
            PvMappingIssue(
                index=-1, field="entries", message="至少需要保留一条 PV 映射"
            )
        )

    seen_signals: dict[str, int] = {}
    seen_pvs: dict[str, int] = {}
    for index, entry in enumerate(config.entries):
        if not _SIGNAL_PATTERN.match(entry.signal):
            issues.append(
                PvMappingIssue(
                    index=index,
                    field="signal",
                    message="业务信号需为小写点分形式，如 quadrupole.q1.current",
                )
            )
        elif entry.signal in seen_signals:
            issues.append(
                PvMappingIssue(
                    index=index,
                    field="signal",
                    message=f"业务信号与第 {seen_signals[entry.signal] + 1} 行重复",
                )
            )
        else:
            seen_signals[entry.signal] = index

        if not entry.pv:
            issues.append(
                PvMappingIssue(index=index, field="pv", message="PV 名称不能为空")
            )
        elif not _PV_PATTERN.match(entry.pv):
            issues.append(
                PvMappingIssue(
                    index=index,
                    field="pv",
                    message="PV 名称含非法字符（不允许空格）",
                )
            )
        elif entry.pv in seen_pvs:
            issues.append(
                PvMappingIssue(
                    index=index,
                    field="pv",
                    message=f"PV 名称与第 {seen_pvs[entry.pv] + 1} 行重复",
                )
            )
        else:
            seen_pvs[entry.pv] = index

        if not entry.label.strip():
            issues.append(
                PvMappingIssue(index=index, field="label", message="设备参数名不能为空")
            )

    if config.gateway == "channel-access" and config.ca_lib_dir:
        candidate = Path(config.ca_lib_dir)
        directory = candidate.parent if candidate.suffix.lower() == ".dll" else candidate
        if not (directory / "ca.dll").is_file():
            issues.append(
                PvMappingIssue(
                    index=-1,
                    field="ca_lib_dir",
                    message=f"指定目录下没有 ca.dll：{directory}",
                )
            )

    return issues


def simulated_seed_values(config: PvMappingConfig) -> dict[str, tuple[float, str]]:
    """把映射翻译成模拟网关需要的 ``{signal: (初值, 单位)}``。"""
    return {
        entry.signal: (
            DEFAULT_SIMULATED_VALUES.get(entry.signal, 0.0),
            entry.unit,
        )
        for entry in config.entries
    }
