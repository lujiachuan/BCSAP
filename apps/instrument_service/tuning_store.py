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

from packages.contracts.tuning import TuningIteration

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
    finished_at   TEXT
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
    PRIMARY KEY (run_id, iteration)
);
"""


class TuningStore:
    """调束记录的本地暂存。"""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory else default_store_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._db_path = self.directory / "tuning_spool.sqlite3"
        with self._session() as connection:
            connection.executescript(SCHEMA)

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

    def append_iteration(self, run_id: str, iteration: TuningIteration) -> None:
        with self._session() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO tuning_iterations"
                " (run_id, iteration, proposed_json, applied_json, readback_json,"
                "  target, objective, quality, detail, at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            )
            for row in rows
        ]

    def load_run(self, run_id: str) -> sqlite3.Row | None:
        with self._session() as connection:
            return connection.execute(
                "SELECT * FROM tuning_runs WHERE run_id = ?", (run_id,)
            ).fetchone()

    def db_path(self) -> Path:
        return self._db_path


def default_tuning_dir() -> Path:
    """与扫谱共用暂存目录，可用 ``SPECTRUM_SCAN_STORE`` 一并覆盖。"""
    override = os.environ.get("SPECTRUM_SCAN_STORE")
    if override:
        return Path(override)
    return default_store_dir()
