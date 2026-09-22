"""调束记录的本地暂存：``tuning_runs`` + ``tuning_iterations``。

架构文档 7.1 把这两张表列为核心表，7.3 要求先落本地 SQLite 元数据。
这里保存的是**每一轮的完整链路**（候选 → 实际下发 → 实际回读 → 目标测量
→ 质量状态），以及算法版本与随机种子——没有这些就无法复现一次调束，
也无法事后判断"到底是算法没找对，还是设备没跟上"。

与扫谱共用同一个暂存目录，但独立建库文件，两个模块互不依赖对方表结构。
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from packages.contracts.tuning import (
    TuningFinalizeResult,
    TuningIteration,
    TuningRecovery,
)

from .scan_store import default_store_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS tuning_runs (
    run_id        TEXT PRIMARY KEY,
    target_signal TEXT NOT NULL,
    mode          TEXT NOT NULL,
    state         TEXT NOT NULL,
    max_iterations INTEGER NOT NULL,
    algorithm     TEXT,
    algorithm_version TEXT,
    seed          INTEGER,
    variables_json TEXT NOT NULL,
    best_values_json TEXT,
    best_objective REAL,
    message       TEXT,
    created_at    TEXT NOT NULL,
    finished_at   TEXT,
    -- 启动前快照（各路实际回读）与目标基线；回退审计记录
    snapshot_json TEXT,
    baseline_objective REAL,
    recovery_json TEXT,
    -- 结束后的处置动作结果（应用最优/恢复初始/回安全值）
    finalize_json TEXT
);
CREATE TABLE IF NOT EXISTS tuning_iterations (
    run_id     TEXT NOT NULL,
    iteration  INTEGER NOT NULL,
    proposed_json TEXT NOT NULL,
    applied_json  TEXT NOT NULL,
    readback_json TEXT NOT NULL,
    target     REAL,
    objective  REAL,
    quality    TEXT NOT NULL,
    detail     TEXT,
    at         TEXT NOT NULL,
    stage      TEXT,
    PRIMARY KEY (run_id, iteration)
);
"""

# 建表语句对已存在的表无效，现场已有老库，新增列必须显式补（幂等）
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("tuning_runs", "snapshot_json", "TEXT"),
    ("tuning_runs", "baseline_objective", "REAL"),
    ("tuning_runs", "recovery_json", "TEXT"),
    ("tuning_runs", "finalize_json", "TEXT"),
    # 每一轮属于哪个阶段（逐参数/联合）：老库里的历史轮次没有这一列，读出来是 NULL，
    # 按"当时不知道"呈现，不猜成联合
    ("tuning_iterations", "stage", "TEXT"),
)


class TuningStore:
    """调束记录的本地暂存。"""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory else default_store_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._db_path = self.directory / "tuning_spool.sqlite3"
        with self._session() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """给现场已有库补新增列（幂等）：删库重建会丢掉还没上传的调束记录。"""
        for table, column, column_type in MIGRATIONS:
            existing = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """开连接、提交、**确保关闭**（sqlite3 的上下文管理器不关连接）。"""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # ------------------------------------------------------------------
    def create_run(
        self,
        run_id: str,
        target_signal: str,
        mode: str,
        max_iterations: int,
        variables: list[dict],
        created_at: str,
    ) -> None:
        with self._session() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO tuning_runs"
                " (run_id, target_signal, mode, state, max_iterations,"
                "  variables_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, target_signal, mode, "draft", max_iterations,
                 json.dumps(variables, ensure_ascii=False), created_at),
            )

    def set_state(self, run_id: str, state: str, message: str = "") -> None:
        with self._session() as connection:
            connection.execute(
                "UPDATE tuning_runs SET state = ?, message = ? WHERE run_id = ?",
                (state, message, run_id),
            )

    def set_algorithm(
        self, run_id: str, algorithm: str, version: str, seed: int
    ) -> None:
        with self._session() as connection:
            connection.execute(
                "UPDATE tuning_runs SET algorithm = ?, algorithm_version = ?,"
                " seed = ? WHERE run_id = ?",
                (algorithm, version, seed, run_id),
            )

    def set_best(self, run_id: str, values: dict[str, float], objective: float) -> None:
        with self._session() as connection:
            connection.execute(
                "UPDATE tuning_runs SET best_values_json = ?, best_objective = ?"
                " WHERE run_id = ?",
                (json.dumps(values, ensure_ascii=False), objective, run_id),
            )

    def set_finished(self, run_id: str, finished_at: str) -> None:
        with self._session() as connection:
            connection.execute(
                "UPDATE tuning_runs SET finished_at = ? WHERE run_id = ?",
                (finished_at, run_id),
            )

    def set_snapshot(
        self, run_id: str, snapshot: dict[str, float], baseline: float | None
    ) -> None:
        """保存启动前快照与目标基线：回退要靠它，复盘也要靠它。"""
        with self._session() as connection:
            connection.execute(
                "UPDATE tuning_runs SET snapshot_json = ?, baseline_objective = ?"
                " WHERE run_id = ?",
                (json.dumps(snapshot, ensure_ascii=False), baseline, run_id),
            )

    def set_recovery(self, run_id: str, recovery: TuningRecovery) -> None:
        """保存束流丢失保护的执行记录（原因、是否成功、逐路实际回读）。"""
        with self._session() as connection:
            connection.execute(
                "UPDATE tuning_runs SET recovery_json = ? WHERE run_id = ?",
                (recovery.model_dump_json(), run_id),
            )

    def set_finalize(self, run_id: str, result: TuningFinalizeResult) -> None:
        """保存结束后处置动作的结果：事后要能回答"最后把设备放到了哪"。"""
        with self._session() as connection:
            connection.execute(
                "UPDATE tuning_runs SET finalize_json = ? WHERE run_id = ?",
                (result.model_dump_json(), run_id),
            )

    def append_iteration(self, run_id: str, iteration: TuningIteration) -> None:
        with self._session() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO tuning_iterations"
                " (run_id, iteration, proposed_json, applied_json, readback_json,"
                "  target, objective, quality, detail, at, stage)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    iteration.iteration,
                    json.dumps(iteration.proposed, ensure_ascii=False),
                    json.dumps(iteration.applied, ensure_ascii=False),
                    json.dumps(iteration.readback, ensure_ascii=False),
                    iteration.target,
                    iteration.objective,
                    iteration.quality,
                    iteration.detail,
                    iteration.at,
                    iteration.stage,
                ),
            )

    def load_iterations(self, run_id: str) -> list[TuningIteration]:
        with self._session() as connection:
            rows = list(
                connection.execute(
                    "SELECT * FROM tuning_iterations WHERE run_id = ? ORDER BY iteration",
                    (run_id,),
                )
            )
        return [
            TuningIteration(
                iteration=int(row["iteration"]),
                proposed=json.loads(row["proposed_json"]),
                applied=json.loads(row["applied_json"]),
                readback=json.loads(row["readback_json"]),
                target=row["target"],
                objective=row["objective"],
                quality=str(row["quality"]),
                detail=row["detail"],
                at=str(row["at"]),
                stage=row["stage"],
            )
            for row in rows
        ]

    def load_run(self, run_id: str) -> sqlite3.Row | None:
        with self._session() as connection:
            return connection.execute(
                "SELECT * FROM tuning_runs WHERE run_id = ?", (run_id,)
            ).fetchone()

    def incomplete_runs(self) -> list[sqlite3.Row]:
        """返回上次进程未正常收尾的任务，供启动恢复屏障使用。"""
        with self._session() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM tuning_runs "
                    "WHERE state NOT IN ('completed', 'aborted', 'failed')"
                )
            )

    def list_runs(self, limit: int = 50) -> list[sqlite3.Row]:
        """历史 run 列表（按开始时间倒序，最新在前）。"""
        with self._session() as connection:
            return list(
                connection.execute(
                    "SELECT run_id, created_at, finished_at, state, message,"
                    " algorithm, best_objective, max_iterations, mode"
                    " FROM tuning_runs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            )

    def db_path(self) -> Path:
        return self._db_path


def default_tuning_dir() -> Path:
    """与扫谱共用暂存目录，可用 ``SPECTRUM_SCAN_STORE`` 一并覆盖。"""
    override = os.environ.get("SPECTRUM_SCAN_STORE")
    if override:
        return Path(override)
    return default_store_dir()
