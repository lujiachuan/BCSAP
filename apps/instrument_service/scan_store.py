"""扫描结果的本地暂存：SQLite 元数据 + NPZ 曲线。

架构依据（文档 7.3）：

* 执行服务在本地为每个实验生成 UUID，先写 SQLite 元数据和任务状态；
* 曲线写入**临时文件**，关闭并校验后再**原子重命名**发布——
  绝不能让人读到写到一半的 NPZ；
* SQLite 里保存文件相对路径、长度、SHA-256 与上传状态；
* ``COMPLETED`` 只表示本地数据已完整持久化，不要求中央上传已完成。

只存**质量合格**的点进 x/y 数组；不合格的点留在 SQLite 点表里可追溯，
但混进谱图会污染分析结果（文档 9.2：保存时间及质量信息）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from packages.contracts.scan import ScanPoint
from packages.spectrum.codec import encode_spectrum, spectrum_checksum

SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_runs (
    run_id        TEXT PRIMARY KEY,
    label         TEXT NOT NULL,
    state         TEXT NOT NULL,
    detector      TEXT NOT NULL,
    axis_json     TEXT NOT NULL,
    meta_json     TEXT NOT NULL,
    spectrum_id   TEXT,
    spectrum_path TEXT,
    byte_length   INTEGER,
    sha256        TEXT,
    point_count   INTEGER,
    upload_state  TEXT NOT NULL DEFAULT 'pending',
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_points (
    run_id     TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    target     REAL NOT NULL,
    coordinate REAL NOT NULL,
    signal     REAL NOT NULL,
    quality    TEXT NOT NULL,
    included   INTEGER NOT NULL,
    spread     REAL NOT NULL DEFAULT 0,
    detail     TEXT,
    at         TEXT NOT NULL,
    -- 成组扫描时每一路回读信号的实际读数（JSON 对象：回读信号名 → 值）
    readbacks_json TEXT,
    PRIMARY KEY (run_id, idx)
);
"""

# 建表语句里的列对**已存在**的表无效，现场 %LOCALAPPDATA% 下已有老 spool 库，
# 所以新增列必须显式补。表名/列名都是本文件里的常量，不做用户输入拼接。
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("scan_points", "readbacks_json", "TEXT"),
)


@dataclass(frozen=True, slots=True)
class StoredSpectrum:
    """已发布谱图的落盘信息。"""

    spectrum_id: str
    path: Path
    byte_length: int
    sha256: str
    point_count: int


class StoreUnavailable(RuntimeError):
    """暂存目录不可用（建不了目录 / 不可写）。"""


def default_store_dir() -> Path:
    """暂存目录，可用 ``SPECTRUM_SCAN_STORE`` 覆盖。"""
    override = os.environ.get("SPECTRUM_SCAN_STORE")
    if override:
        return Path(override)
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    base = Path(root) if root else Path.home() / ".local" / "share"
    return base / "SpectrumPlatform" / "spool"


class ScanStore:
    """扫描结果的本地暂存。线程安全由 SQLite 连接 + 每次调用新建连接保证。"""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory else default_store_dir()
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._db_path = self.directory / "scan_spool.sqlite3"
            with self._session() as connection:
                connection.executescript(SCHEMA)
                self._migrate(connection)
        except OSError as exc:
            # 裸 PermissionError 冒到界面上毫无线索：现场需要知道是哪个目录、
            # 以及怎么改。暂存目录不可写时扫描无法安全保存，宁可明确拒绝。
            raise StoreUnavailable(
                f"暂存目录不可用：{self.directory}（{exc}）。"
                "请确认该目录可写，或用环境变量 SPECTRUM_SCAN_STORE 指定另一个目录。"
            ) from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """给现场已有的库补新增列（幂等：已存在就跳过）。

        ``CREATE TABLE IF NOT EXISTS`` 对已存在的表什么都不做，所以新列只能这样补。
        直接删库重建等于丢掉现场还没上传的谱图，不能这么干。
        """
        for table, column, column_type in MIGRATIONS:
            existing = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
                )

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """开一个连接、提交事务、并**确保关闭**。

        sqlite3 连接的上下文管理器只管提交/回滚，**不会关闭连接**。在 Windows 上
        泄漏的连接会一直占住数据库文件句柄：临时目录删不掉、备份拷不走、
        数据库文件无法替换——排查起来很不直观。
        """
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # 元数据
    # ------------------------------------------------------------------
    def create_run(
        self, run_id: str, label: str, detector: str, axis: dict, created_at: str
    ) -> None:
        with self._session() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO scan_runs"
                " (run_id, label, state, detector, axis_json, meta_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, label, "draft", detector,
                 json.dumps(axis, ensure_ascii=False), "{}", created_at),
            )

    def set_state(self, run_id: str, state: str) -> None:
        with self._session() as connection:
            connection.execute(
                "UPDATE scan_runs SET state = ? WHERE run_id = ?", (state, run_id)
            )

    def set_meta(self, run_id: str, meta: dict) -> None:
        with self._session() as connection:
            connection.execute(
                "UPDATE scan_runs SET meta_json = ? WHERE run_id = ?",
                (json.dumps(meta, ensure_ascii=False), run_id),
            )

    def append_point(self, run_id: str, point: ScanPoint) -> None:
        with self._session() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO scan_points"
                " (run_id, idx, target, coordinate, signal, quality, included,"
                "  spread, detail, at, readbacks_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, point.index, point.target, point.coordinate, point.signal,
                 point.quality, 1 if point.included else 0, point.coordinate_spread,
                 point.detail, point.at,
                 json.dumps(point.readback_values, ensure_ascii=False)
                 if point.readback_values else None),
            )

    # ------------------------------------------------------------------
    # 曲线
    # ------------------------------------------------------------------
    def publish_spectrum(
        self, run_id: str, x: list[float], y: list[float]
    ) -> StoredSpectrum:
        """校验后原子发布 NPZ，并把路径/长度/SHA-256 写回元数据。"""
        payload = encode_spectrum(x, y)
        checksum = spectrum_checksum(payload)
        spectrum_id = checksum[:16]
        relative = Path("spectra") / f"{spectrum_id}.npz"
        target = self.directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        # 先写同目录临时文件 → fsync → 原子替换，读者永远看不到半截文件
        handle, temp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=".spectrum-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, target)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

        with self._session() as connection:
            connection.execute(
                "UPDATE scan_runs SET spectrum_id = ?, spectrum_path = ?,"
                " byte_length = ?, sha256 = ?, point_count = ? WHERE run_id = ?",
                (spectrum_id, str(relative), len(payload), checksum, len(x), run_id),
            )
        return StoredSpectrum(
            spectrum_id=spectrum_id,
            path=target,
            byte_length=len(payload),
            sha256=checksum,
            point_count=len(x),
        )

    def load_points(self, run_id: str) -> list[sqlite3.Row]:
        with self._session() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM scan_points WHERE run_id = ? ORDER BY idx", (run_id,)
                )
            )

    def load_run(self, run_id: str) -> sqlite3.Row | None:
        with self._session() as connection:
            return connection.execute(
                "SELECT * FROM scan_runs WHERE run_id = ?", (run_id,)
            ).fetchone()

    def spectrum_path(self, run_id: str) -> Path | None:
        row = self.load_run(run_id)
        if row is None or not row["spectrum_path"]:
            return None
        return self.directory / row["spectrum_path"]
