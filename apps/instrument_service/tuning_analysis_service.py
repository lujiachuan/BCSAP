"""调束结果分析：从持久化 Optuna study 提取 history / importance / slice 数据。

设计：
- 服务端不嵌 plotly（PyQt 桌面端集成成本高），只返回**结构化数据**，前端用 pyqtgraph 画。
- 每个 run 一个 study（study_name = "run_{run_id}"），从 sqlite storage 打开。
- 数据量小（几十轮），直接全量返回，不分页。

**范围（批次 3 的 D13 修正）**：采样 Study 按阶段拆分（逐参数 / 联合微调），
逐参数阶段每个变量各一个 search space。把它们合并起来算重要性/响应面，
等于把"单变量切片"和"四维联合"的数据混在一张图上——峰位是假的，重要性排名也不可信。
所以分析默认**只取联合阶段**（`scope="joint"`），拿不到时才退回合并数据并显式标注
`scope="mixed"` 与原因，让看图的人自己决定要不要用。

参数值优先取**设备实际回读**（trial 的 ``user_attrs.readback``）：分析应该反映
"设备当时在哪"，而不是"算法建议了哪"。
"""

from __future__ import annotations

from typing import Any

import optuna
from optuna.trial import TrialState


def open_study(storage_url: str, run_id: str) -> optuna.Study | None:
    """打开指定 run 的分析 Study；不存在或无 COMPLETE trial 返回 None。

    采样 Study 按阶段拆分（run_{id}__sample__seq_xxx / joint），分析层优先只读联合阶段。
    """
    # 先试旧版单 Study（向后兼容）
    for name in (f"run_{run_id}",):
        try:
            study = optuna.load_study(study_name=name, storage=storage_url)
            if study.trials:
                return study
        except (KeyError, ValueError):
            pass
    summaries = optuna.get_all_study_summaries(storage_url)
    all_names = [s.study_name for s in summaries]
    joint = f"run_{run_id}__sample__joint"
    if joint in all_names:
        study = optuna.load_study(study_name=joint, storage=storage_url)
        if study.trials:
            return study
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


def _is_joint(study: optuna.Study) -> bool:
    """这个 Study 是不是联合阶段（或旧版单 Study）——用于标注分析范围。"""
    return "__sample__" not in study.study_name or study.study_name.endswith("__sample__joint")


def _param_value(trial: optuna.trial.FrozenTrial, name: str) -> float | None:
    """参数取值优先用**设备实际回读**，缺失才退回算法建议值。"""
    readback = trial.user_attrs.get("readback") or {}
    raw = readback.get(name, trial.params.get(name))
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


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
    """一维切片：每个参数的 (x, y) 点列表，前端散点 + 最优值竖线。

    x 取**设备实际回读**（缺失才退回算法建议值）：切片要看的是"设备在这个位置上
    的表现"，不是"算法想让设备去哪"。
    """
    completed = _completed_trials(study)
    if not completed:
        return {}
    param_names = sorted({k for t in completed for k in t.params})
    out: dict[str, list[list[float]]] = {}
    for name in param_names:
        pts: list[list[float]] = []
        for t in completed:
            v = _param_value(t, name)
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
        row.update(
            {
                name: value
                for name in t.params
                if (value := _param_value(t, name)) is not None
            }
        )
        rows.append(row)
    return rows


def surface_data(study: optuna.Study) -> dict[str, Any]:
    """响应面矩阵：事后用 GP 拟合全部采样点，返回两两参数的预测均值/不确定性网格。

    左下三角画预测均值、右上三角画不确定性（std），固定其余维为最优点。
    样本太少或拟合失败时 available=False 并说明原因。
    """
    completed = _completed_trials(study)
    if len(completed) < 5:
        return {"available": False, "reason": f"有效轮次 {len(completed)} 个，少于 5，不足以拟合响应面"}
    param_names = sorted({k for t in completed for k in t.params})
    if len(param_names) < 2:
        return {"available": False, "reason": "参数少于 2 个"}

    import numpy as np
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import RBF, WhiteKernel, ConstantKernel

    # 采样点取实际回读；缺回读的 trial 不参与拟合（宁可少几个点，也不要错配的 x）
    samples = [
        ([_param_value(t, n) for n in param_names], float(t.value))
        for t in completed
    ]
    samples = [(x, y) for x, y in samples if all(v is not None for v in x)]
    if len(samples) < 5:
        return {
            "available": False,
            "reason": f"带回读的采样点只有 {len(samples)} 个，少于 5，不足以拟合响应面",
        }
    X = np.asarray([x for x, _y in samples], dtype=float)
    y = np.asarray([_y for _x, _y in samples], dtype=float)
    lo = X.min(axis=0)
    hi = X.max(axis=0)
    span = hi - lo
    span[span == 0] = 1.0
    Xs = (X - lo) / span
    try:
        kernel = (
            ConstantKernel(1.0, (1e-2, 1e2))
            * RBF(length_scale=[0.3] * X.shape[1], length_scale_bounds=(1e-2, 1e2))
            + WhiteKernel(noise_level=1e-3, noise_level_bounds=(1e-5, 1e1))
        )
        gp = GaussianProcessRegressor(kernel=kernel, normalize_y=True,
                                      n_restarts_optimizer=2, random_state=0)
        gp.fit(Xs, y)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"GP 拟合失败：{exc}"}

    best = {
        name: _param_value(max(completed, key=lambda t: float(t.value or float("-inf"))), name)
        for name in param_names
    }
    best = {k: v for k, v in best.items() if v is not None} or dict(study.best_params)
    best_s = np.array([
        (float(best.get(nm, (lo[k] + hi[k]) / 2.0)) - lo[k]) / span[k]
        for k, nm in enumerate(param_names)
    ])
    grid = 25
    g = np.linspace(0.0, 1.0, grid)
    ii, jj = np.meshgrid(g, g, indexing="ij")
    flat_i = ii.ravel()
    flat_j = jj.ravel()
    n = len(param_names)
    pairs: dict[str, Any] = {}
    for i in range(n):
        for j in range(i + 1, n):
            rows = np.tile(best_s, (grid * grid, 1))
            rows[:, i] = flat_i
            rows[:, j] = flat_j
            mu, sd = gp.predict(rows, return_std=True)
            pairs[f"{i}_{j}"] = {
                "i": i, "j": j,
                "mean": mu.reshape(grid, grid).tolist(),
                "std": sd.reshape(grid, grid).tolist(),
            }
    return {
        "available": True,
        "params": param_names,
        "bounds": [[float(lo[k]), float(hi[k])] for k in range(n)],
        "best": [
            float(best.get(nm, (lo[k] + hi[k]) / 2.0))
            for k, nm in enumerate(param_names)
        ],
        "points": X.tolist(),
        "point_values": y.tolist(),
        "pairs": pairs,
        "grid": grid,
    }


def analyze(storage_url: str, run_id: str) -> dict[str, Any] | None:
    """一站式分析结果：history + importance + slice + trials + best。

    ``scope`` 说明这份数据来自哪个范围：``joint``（只有联合阶段的 trial，可信）
    或 ``mixed``（各阶段合并，仅作趋势参考——逐参数阶段的单变量 trial 与联合阶段的
    多维 trial 混在一起，重要性与响应面的排名会失真）。
    """
    study = open_study(storage_url, run_id)
    if study is None:
        return None
    joint = _is_joint(study)
    return {
        "study_name": study.study_name,
        "scope": "joint" if joint else "mixed",
        "scope_note": (
            ""
            if joint
            else "同一次任务里的逐参数阶段与联合阶段被合并计算：重要性与响应面"
            "只应作为趋势参考，不要据此下结论"
        ),
        "n_trials": len(study.trials),
        "n_complete": len(_completed_trials(study)),
        "best_value": float(study.best_value) if study.best_trials else None,
        "best_params": {k: float(v) for k, v in study.best_params.items()}
        if study.best_trials else {},
        "history": history(study),
        "importance": importance(study),
        "slice": slice_data(study),
        "trials": trials_table(study),
        "surface": surface_data(study),
    }
