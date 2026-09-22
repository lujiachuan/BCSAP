"""调束页配置的保存与恢复（改造报告 §5.2「配置持久化」/ §8.9 P2.3）。

原 demo 把贝叶斯页的配置写进 ``bayes_config.json``：启动时自动载回，开跑前再存一次，
另有一个「保存配置」按钮。这里保持同一套使用习惯，但补上 demo 没有的三件事：

1. **坏文件不能拖垮页面**：文件被手改坏、字段类型不对时逐项丢弃并说明原因，
   而不是抛异常让调束页打不开；
2. **配置里有、当前映射里没有的信号要点名**：换过设备或改过 PV 映射之后，
   静默忽略等于让操作员以为"上次的变量都还在"；
3. **落点可控**：默认 ``%LOCALAPPDATA%\\SpectrumPlatform\\tuning_config.json``，
   ``SPECTRUM_TUNING_CONFIG`` 可覆盖（与 ``SPECTRUM_CLIENT_CACHE`` 同一套约定），
   测试也用它，免得把配置写进真实用户目录。

保存的是**下次从哪儿开始**：优化目标、勾选变量及其范围、优化策略与轮次分配、最大轮次、
稳定超时、每轮采样次数、束流丢失保护阈值、完成后处置偏好。不保存任何会写设备的即时状态；
写不写、写多少仍由执行层在启动时按当前映射校验。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_VERSION = 1
CONFIG_ENV_VAR = "SPECTRUM_TUNING_CONFIG"
CONFIG_FILE_NAME = "tuning_config.json"

# 数值字段 → (类型, 界面文案)。类型只用 int / float / bool：
# JSON 里 3 和 3.0 都可能出现，整数位单独放宽处理。
_NUMERIC_FIELDS: dict[str, str] = {
    "max_iterations": "最大轮次",
    "settle_timeout_s": "回读稳定超时",
    "samples_per_point": "每轮目标采样次数",
    "calls_per_variable": "逐参数阶段轮次",
    "joint_frac": "联合微调范围",
    "hold_s": "每轮写完后额外保持",
    "loss_relative": "相对损失阈值",
    "loss_strikes": "连续异常次数",
    "seed": "随机种子",
    "n_startup_trials": "随机探索轮次",
    "patience": "收敛早停轮数",
}


def default_config_path() -> Path:
    """默认落点：``%LOCALAPPDATA%\\SpectrumPlatform\\tuning_config.json``。

    放在 ``client_cache`` **旁边**而不是里面：缓存目录是可重建的中央数据镜像，
    操作员换目录或清缓存不该把调束配置一起弄丢。
    """
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override)
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    base = Path(root) if root else Path.home() / ".local" / "share"
    return base / "SpectrumPlatform" / CONFIG_FILE_NAME


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def sanitize(raw: object, *, strategies: tuple[str, ...] = (),
             actions: tuple[str, ...] = (), engines: tuple[str, ...] = (),
             modes: tuple[str, ...] = ()) -> tuple[dict, list[str]]:
    """把读到的（可能是手改坏的）配置逐项规范化。

    返回 ``(配置, 丢弃说明)``。丢弃说明是给界面看的：哪一项是什么值、为什么没用它。
    范围只做"数字"这一层校验——上下限是否落在设备允许区间由执行层在启动时判定，
    这里不许把配置悄悄夹到边界上（那会让操作员以为自己的范围被接受了）。
    """
    notes: list[str] = []
    if not isinstance(raw, dict):
        return {}, ["配置内容不是一份 JSON 对象，已整体忽略"]

    config: dict = {"version": CONFIG_VERSION}

    target = raw.get("target_signal")
    if isinstance(target, str) and target.strip():
        config["target_signal"] = target.strip()

    variables: list[dict] = []
    for item in raw.get("variables") or []:
        if not isinstance(item, dict):
            notes.append(f"变量项不是对象，已忽略：{item!r}")
            continue
        signal = item.get("signal")
        if not isinstance(signal, str) or not signal.strip():
            notes.append(f"变量项缺少信号名，已忽略：{item!r}")
            continue
        entry: dict = {"signal": signal.strip()}
        if "enabled" in item:
            entry["enabled"] = bool(item.get("enabled"))
        for key in ("low", "high", "start"):
            if key not in item:
                continue
            value = _as_float(item.get(key))
            if value is None:
                notes.append(f"{signal} 的 {key} 不是数字（{item.get(key)!r}），按映射默认范围处理")
                continue
            entry[key] = value
        variables.append(entry)
    if variables:
        config["variables"] = variables

    strategy = raw.get("strategy")
    if isinstance(strategy, str) and strategy:
        if strategies and strategy not in strategies:
            notes.append(f"优化策略 {strategy!r} 不认识，已用页面默认值")
        else:
            config["strategy"] = strategy
    elif strategy is not None:
        notes.append(f"优化策略不是字符串（{strategy!r}），已用页面默认值")

    engine = raw.get("engine")
    if isinstance(engine, str) and engine:
        if engines and engine not in engines:
            notes.append(f"优化引擎 {engine!r} 不认识，已用页面默认值")
        else:
            config["engine"] = engine
    elif engine is not None:
        notes.append(f"优化引擎不是字符串（{engine!r}），已用页面默认值")

    mode = raw.get("mode")
    if isinstance(mode, str) and mode:
        if modes and mode not in modes:
            notes.append(f"调束模式 {mode!r} 不认识，已用页面默认值")
        else:
            config["mode"] = mode
    elif mode is not None:
        notes.append(f"调束模式不是字符串（{mode!r}），已用页面默认值")

    for key, label in _NUMERIC_FIELDS.items():
        if key not in raw:
            continue
        value = _as_float(raw.get(key))
        if value is None:
            notes.append(f"{label}不是数字（{raw.get(key)!r}），已用页面默认值")
            continue
        config[key] = value

    if "loss_absolute" in raw:
        value = _as_float(raw.get("loss_absolute"))
        if value is None and raw.get("loss_absolute") is not None:
            notes.append(f"绝对归零阈值不是数字（{raw.get('loss_absolute')!r}），已按未启用处理")
        config["loss_absolute"] = value

    if "auto_recover" in raw:
        config["auto_recover"] = bool(raw.get("auto_recover"))

    if "reset_before_start" in raw:
        config["reset_before_start"] = bool(raw.get("reset_before_start"))

    if "random_seed" in raw:
        config["random_seed"] = bool(raw.get("random_seed"))

    action = raw.get("finalize_action")
    if action in (None, ""):
        pass
    elif isinstance(action, str) and action:
        if actions and action not in actions:
            notes.append(f"完成后处置 {action!r} 不认识，已用页面默认值")
        else:
            config["finalize_action"] = action
    else:
        notes.append(f"完成后处置不是字符串（{action!r}），已用页面默认值")

    if len(config) == 1:
        # 除了 version 一个字段都没认出来（空文件、或整份被手改成别的结构）：
        # 当成"没有可用配置"，而不是"载入了一份 0 变量的配置"
        return {}, ["配置里没有任何可识别的字段"]
    return config, notes


def load(path: Path | None = None, *, strategies: tuple[str, ...] = (),
         actions: tuple[str, ...] = (), engines: tuple[str, ...] = (),
         modes: tuple[str, ...] = ()) -> tuple[dict | None, str]:
    """读配置文件。

    返回 ``(配置, 说明)``：**没有文件时返回 ``(None, "")``**——"从没用过"是正常状态，
    不该在界面上报错。文件存在但读不动时返回 ``(None, 原因)``，由界面如实展示。
    """
    target = path or default_config_path()
    if not target.exists():
        return None, ""
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"调束配置读不出来（{target}）：{exc}"
    config, notes = sanitize(
        raw, strategies=strategies, actions=actions, engines=engines, modes=modes
    )
    if not config:
        return None, f"调束配置内容无效（{target}）：" + ("；".join(notes) or "空文件")
    detail = f"调束配置 {target} 里有 {len(notes)} 项被忽略：" + "；".join(notes)
    return config, (detail if notes else "")


def save(config: dict, path: Path | None = None) -> Path:
    """写配置文件（原子替换：先写临时文件再改名，避免半个 JSON 留在盘上）。"""
    target = path or default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(config)
    payload["version"] = CONFIG_VERSION
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, target)
    return target


def missing_signals(config: dict, known: set[str]) -> list[str]:
    """配置里有、当前映射里没有的信号（换设备/改映射之后必须点名）。"""
    return [
        str(item.get("signal"))
        for item in config.get("variables") or []
        if str(item.get("signal") or "") not in known
    ]


def describe(config: dict) -> str:
    """一句话摘要，用在保存/载入后的界面反馈里。"""
    variables = config.get("variables") or []
    enabled = sum(1 for item in variables if item.get("enabled", True))
    parts = [f"{enabled} 个变量（共记录 {len(variables)} 个）"]
    if config.get("target_signal"):
        parts.append(f"目标 {config['target_signal']}")
    if config.get("strategy"):
        parts.append(f"策略 {config['strategy']}")
    if config.get("engine"):
        parts.append(f"引擎 {config['engine']}")
    if config.get("mode"):
        parts.append(f"模式 {config['mode']}")
    if config.get("max_iterations") is not None:
        parts.append(f"最大 {config['max_iterations']:g} 轮")
    if config.get("patience") is not None:
        parts.append(f"早停 {config['patience']:g} 轮")
    return "、".join(parts)
