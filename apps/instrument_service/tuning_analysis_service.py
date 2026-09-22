"""调束结果分析：从持久化 Optuna study 提取 history / importance / slice 数据。

设计：
- 服务端不嵌 plotly（PyQt 桌面端集成成本高），只返回**结构化数据**，前端用 pyqtgraph 画。
- 每个 run 一个 study（study_name = "run_{run_id}"），从 sqlite storage 打开。
- 数据量小（几十轮），直接全量返回，不分页。
"""

from __future__ import annotations

from typing import Any

import optuna
from optuna.trial import TrialState


def open_study(storage_url: str, run_id: str) -> optuna.Study | None:
    """打开指定 run 的分析 Study；不存在或无 COMPLETE trial 返回 None。

    采样 Study 按阶段拆分（run_{id}__sample__seq_xxx / joint），分析层合并读。
    """
    # 先试旧版单 Study（向后兼容）
    for name in (f"run_{run_id}",):
        try:
            study = optuna.load_study(study_name=name, storage=storage_url)
            if study.trials:
                return study
        except (KeyError, ValueError):
            pass
    # 合并所有采样 Study：找 storage 里所有 run_{id}__sample__* 的 study
    summaries = optuna.get_all_study_summaries(storage_url)
    all_names = [s.study_name for s in summaries]
    sample_names = sorted(n for n in all_names if n.startswith(f"run_{run_id}__sample__"))
    if not sample_names:
        return None
    if len(sample_names) == 1:
        merged = optuna.load_study(study_name=sample_names[0], storage=storage_url)
        if not merged.trials:
            return None
        return merged
    # 多个采样 Study：新建一个内存合并 Study（add_trial 跨 study 会有 number 冲突，
    # 所以用 create_study + 逐个 add_trial 到独立内存 study）
    merged = optuna.create_study(direction="maximize")
    for other in sample_names:
        other_study = optuna.load_study(study_name=other, storage=storage_url)
        for t in other_study.trials:
            if t.state == optuna.trial.TrialState.COMPLETE and t.value is not None:
                # 复制成新 number 的 trial：add_trial 要求同 study 内 number 唯一，
                # 内存空 study 会自动重排
                merged.add_trial(t)
    if not merged.trials:
        return None
    return merged


def _completed_trials(study: optuna.Study) -> list[optuna.trial.FrozenTrial]:
    return [t for t in study.trials if t.state == TrialState.COMPLETE and t.value is not None]


def history(study: optuna.Study) -> dict[str, list[Any]]:
    """爬山图数据：每轮 trial_number / value / 截至该轮的 best。"""
    rows = []
    best_so_far: float | None = None
    for t in sorted(study.trials, key=lambda x: x.number):
        if t.state != TrialState.COMPLETE or t.value is None:
            continue
        if best_so_far is None or t.value > best_so_far:
            best_so_far = t.value
        rows.append({
            "trial": t.number,
            "value": float(t.value),
            "best": float(best_so_far),
        })
    return {"points": rows}


def importance(study: optuna.Study) -> dict[str, Any]:
    """参数重要性（FANOVA）。trial 太少时 Optuna 会抛错，降级返回空。"""
    completed = _completed_trials(study)
    if len(completed) < 3:
        return {"method": "none", "values": {}, "reason": "有效 trial 少于 3 个，不计算重要性"}
    try:
        scores = optuna.importance.get_param_importances(study)
    except (ValueError, RuntimeError) as exc:
        return {"method": "none", "values": {}, "reason": f"重要性计算失败：{exc}"}
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    return {
        "method": "fanova",
        "values": {k: float(v) for k, v in ordered},
    }


def slice_data(study: optuna.Study) -> dict[str, list[list[float]]]:
    """一维切片：每个参数的 (x, y) 点列表，前端散点 + 最优值竖线。"""
    completed = _completed_trials(study)
    if not completed:
        return {}
    param_names = sorted({k for t in completed for k in t.params})
    out: dict[str, list[list[float]]] = {}
    for name in param_names:
        pts: list[list[float]] = []
        for t in completed:
            v = t.params.get(name)
            if v is not None:
                pts.append([float(v), float(t.value)])
        pts.sort(key=lambda p: p[0])
        out[name] = pts
    return out


def trials_table(study: optuna.Study) -> list[dict[str, Any]]:
    """全部 trial 的参数 + 目标值，前端画 contour 散点 / 平行坐标用。"""
    rows: list[dict[str, Any]] = []
    for t in study.trials:
        row: dict[str, Any] = {
            "number": t.number,
            "state": t.state.name,
            "value": float(t.value) if t.value is not None else None,
        }
        row.update({k: float(v) for k, v in t.params.items()})
        rows.append(row)
    return rows


def analyze(storage_url: str, run_id: str) -> dict[str, Any] | None:
    """一站式分析结果：history + importance + slice + trials + best。"""
    study = open_study(storage_url, run_id)
    if study is None:
        return None
    return {
        "study_name": study.study_name,
        "n_trials": len(study.trials),
        "n_complete": len(_completed_trials(study)),
        "best_value": float(study.best_value) if study.best_trials else None,
        "best_params": {k: float(v) for k, v in study.best_params.items()}
        if study.best_trials else {},
        "history": history(study),
        "importance": importance(study),
        "slice": slice_data(study),
        "trials": trials_table(study),
    }
