"""手动控制主面板配置的加载、校验与原子持久化。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from packages.contracts import ManualCard, ManualDashboardConfig, PvMappingConfig

CONFIG_VERSION = 1
TREND_CARD_ID = "system.beam-trend"


def config_path() -> Path:
    override = os.environ.get("SPECTRUM_MANUAL_DASHBOARD")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) if appdata else Path.home() / ".config"
    return root / "SpectrumPlatform" / "manual_dashboard.json"


def _device_id(entry) -> str:
    explicit = entry.device_id.strip()
    return explicit or entry.signal.rsplit(".", 1)[0]


def _displayable_device_ids(mapping: PvMappingConfig) -> set[str]:
    return {
        _device_id(entry)
        for entry in mapping.entries
        if not entry.signal.startswith("detector.")
    }


def default_config(mapping: PvMappingConfig) -> ManualDashboardConfig:
    """从现有映射生成一次初始卡片库；之后标题可独立于设备锁分组修改。"""
    grouped: dict[str, list[str]] = {}
    order: dict[str, int] = {}
    assigned: set[str] = set()
    for entry in sorted(mapping.entries, key=lambda item: item.display_order):
        if not entry.visible or entry.signal.startswith("detector."):
            continue
        group = entry.group.strip() or "未分组"
        device_id = _device_id(entry)
        if device_id in assigned:
            continue
        assigned.add(device_id)
        grouped.setdefault(group, []).append(device_id)
        order.setdefault(group, entry.display_order)
    cards = [
        ManualCard(
            card_id=TREND_CARD_ID,
            title="束流电流趋势",
            device_ids=[],
            visible=True,
            display_order=-1,
            system=True,
        )
    ]
    for group, device_ids in grouped.items():
        digest = hashlib.sha1(group.encode("utf-8")).hexdigest()[:12]
        cards.append(
            ManualCard(
                card_id=f"device.{digest}",
                title=group,
                device_ids=device_ids,
                visible=True,
                display_order=order[group],
            )
        )
    return ManualDashboardConfig(version=CONFIG_VERSION, cards=cards)


def validate_config(config: ManualDashboardConfig, mapping: PvMappingConfig) -> list[str]:
    issues: list[str] = []
    known = _displayable_device_ids(mapping)
    seen_cards: set[str] = set()
    seen_devices: set[str] = set()
    trend_cards = 0
    for card in config.cards:
        if not card.card_id.strip():
            issues.append("卡片 ID 不能为空")
        elif card.card_id in seen_cards:
            issues.append(f"卡片 ID 重复：{card.card_id}")
        seen_cards.add(card.card_id)
        if not card.title.strip():
            issues.append(f"卡片 {card.card_id} 的标题不能为空")
        if card.system:
            trend_cards += int(card.card_id == TREND_CARD_ID)
            if card.card_id != TREND_CARD_ID or card.device_ids:
                issues.append("系统趋势卡片配置无效")
            continue
        if not card.device_ids:
            issues.append(f"卡片“{card.title}”至少需要一个设备")
        for device_id in card.device_ids:
            if device_id not in known:
                issues.append(f"卡片“{card.title}”引用了不存在的设备：{device_id}")
            if device_id in seen_devices:
                issues.append(f"设备不能同时属于多张卡片：{device_id}")
            seen_devices.add(device_id)
    if trend_cards != 1:
        issues.append("必须且只能保留一张系统趋势卡片")
    return issues


def load_config(mapping: PvMappingConfig) -> ManualDashboardConfig:
    path = config_path()
    try:
        config = ManualDashboardConfig.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return default_config(mapping)
    # PV 映射删除设备后只清理对应引用，不重置用户其余卡片、标题和排序。
    known = _displayable_device_ids(mapping)
    cards: list[ManualCard] = []
    for card in config.cards:
        if card.system:
            cards.append(card)
            continue
        device_ids = [device_id for device_id in card.device_ids if device_id in known]
        if device_ids:
            cards.append(card.model_copy(update={"device_ids": device_ids}))
    config = config.model_copy(update={"cards": cards})
    return config if not validate_config(config, mapping) else default_config(mapping)


def save_config(config: ManualDashboardConfig, mapping: PvMappingConfig) -> Path:
    issues = validate_config(config, mapping)
    if issues:
        raise ValueError("；".join(issues))
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(config.model_dump(), ensure_ascii=False, indent=2) + "\n"
    handle, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".manual-dashboard-", suffix=".tmp"
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
