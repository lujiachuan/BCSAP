"""客户端启动初始化、PV 检查和中央数据本地镜像。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QThread, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from packages.spectrum import decode_spectrum, spectrum_checksum


class LocalDataCache:
    """中央数据的可重建只读缓存，不承载仪器待上传数据。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.spectra_dir = root / "spectra"
        self.spectra_dir.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(root / "cache.sqlite")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sync_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS records (
                entity TEXT NOT NULL,
                id TEXT NOT NULL,
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (entity, id)
            );
            CREATE TABLE IF NOT EXISTS spectra (
                id TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                point_count INTEGER NOT NULL,
                byte_length INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                path TEXT NOT NULL
            );
            """
        )

    def cursor(self) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM sync_state WHERE key = 'cursor'"
        ).fetchone()
        return str(row[0]) if row else None

    def apply_records(self, records: list[dict], deleted: list[dict]) -> None:
        with self.connection:
            for record in records:
                self.connection.execute(
                    """
                    INSERT INTO records(entity, id, version, updated_at, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(entity, id) DO UPDATE SET
                        version = excluded.version,
                        updated_at = excluded.updated_at,
                        payload_json = excluded.payload_json
                    """,
                    (
                        record["entity"],
                        record["id"],
                        record["version"],
                        record["updated_at"],
                        json.dumps(record["payload"], ensure_ascii=False),
                    ),
                )
            for item in deleted:
                self.connection.execute(
                    "DELETE FROM records WHERE entity = ? AND id = ?",
                    (item["entity"], item["id"]),
                )

    def has_spectrum(self, spectrum_id: str, checksum: str) -> bool:
        row = self.connection.execute(
            "SELECT path, sha256 FROM spectra WHERE id = ?", (spectrum_id,)
        ).fetchone()
        return bool(row and row[1] == checksum and (self.root / row[0]).is_file())

    def save_spectrum(self, metadata: dict, payload: bytes) -> None:
        checksum = spectrum_checksum(payload)
        if checksum != metadata["sha256"]:
            raise ValueError(f"谱图 {metadata['id']} 校验值不一致")
        x_values, _y_values = decode_spectrum(payload)
        if len(x_values) != metadata["point_count"]:
            raise ValueError(f"谱图 {metadata['id']} 点数不一致")

        relative_path = Path("spectra") / f"{checksum}.npz"
        final_path = self.root / relative_path
        temporary_path = final_path.with_suffix(".npz.part")
        temporary_path.write_bytes(payload)
        os.replace(temporary_path, final_path)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO spectra(
                    id, version, updated_at, point_count, byte_length, sha256, path
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    version = excluded.version,
                    updated_at = excluded.updated_at,
                    point_count = excluded.point_count,
                    byte_length = excluded.byte_length,
                    sha256 = excluded.sha256,
                    path = excluded.path
                """,
                (
                    metadata["id"],
                    metadata["version"],
                    metadata["updated_at"],
                    metadata["point_count"],
                    metadata["byte_length"],
                    checksum,
                    relative_path.as_posix(),
                ),
            )

    def set_cursor(self, cursor: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO sync_state(key, value) VALUES ('cursor', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (cursor,),
            )

    def close(self) -> None:
        self.connection.close()


class InitializationWorker(QThread):
    """顺序完成依赖检查，在关键检查结束后允许界面先进入。"""

    stepChanged = Signal(str, str, str)
    serviceChanged = Signal(str, str, str)
    essentialReady = Signal()
    syncProgress = Signal(int, int)
    completed = Signal(bool, str)

    def __init__(
        self,
        data_url: str,
        instrument_url: str,
        parent=None,
        cache_root: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.data_url = data_url.rstrip("/")
        self.instrument_url = instrument_url.rstrip("/")
        self.cache_root = cache_root

    def run(self) -> None:
        essential_emitted = False
        try:
            self.stepChanged.emit("config", "good", "本机配置已加载")
            instrument_ok = self._check_instrument()
            if self.isInterruptionRequested():
                return
            data_ok = self._check_data_service()
            self.essentialReady.emit()
            essential_emitted = True
            if self.isInterruptionRequested():
                return
            if data_ok:
                self._sync_data()
            else:
                self.stepChanged.emit("sync", "warn", "已跳过，继续使用本地缓存")
            self.completed.emit(instrument_ok and data_ok, "启动初始化已完成")
        except Exception as exc:
            self.stepChanged.emit("sync", "error", str(exc))
            self.completed.emit(False, f"初始化失败：{exc}")
        finally:
            if not essential_emitted:
                self.essentialReady.emit()

    def _check_instrument(self) -> bool:
        self.stepChanged.emit("instrument", "running", "正在连接…")
        try:
            _request_json(self.instrument_url + "/control/v1/status")
        except Exception as exc:
            self.stepChanged.emit("instrument", "error", f"不可达：{exc}")
            self.stepChanged.emit("pv", "warn", "已跳过")
            self.serviceChanged.emit("instrument", "error", "不可达")
            self.serviceChanged.emit("epics", "idle", "未检查")
            return False

        self.stepChanged.emit("instrument", "good", "仪器执行服务可达")
        self.serviceChanged.emit("instrument", "good", "就绪")
        self.stepChanged.emit("pv", "running", "正在检查受控 PV…")
        try:
            result = _request_json(self.instrument_url + "/control/v1/pvs/health")
            summary = result["summary"]
            total = int(summary["total"])
            connected = int(summary["connected"])
            required_failed = int(summary["required_failed"])
            state = "good" if required_failed == 0 else "error"
            self.stepChanged.emit("pv", state, f"{connected} / {total} PV 已连接")
            self.serviceChanged.emit("epics", state, f"{connected} / {total}")
            return required_failed == 0
        except Exception as exc:
            self.stepChanged.emit("pv", "error", f"检查失败：{exc}")
            self.serviceChanged.emit("epics", "error", "检查失败")
            return False

    def _check_data_service(self) -> bool:
        self.stepChanged.emit("data", "running", "正在连接…")
        try:
            result = _request_json(self.data_url + "/api/v1/health/ready")
            status = str(result.get("status", "unavailable"))
            if status == "unavailable":
                raise ConnectionError(result.get("detail") or "服务未就绪")
            state = "good" if status == "ready" else "warn"
            text = "正常" if status == "ready" else "可达 · 降级"
            self.stepChanged.emit("data", state, text)
            self.serviceChanged.emit("data", state, text)
            return True
        except Exception as exc:
            self.stepChanged.emit("data", "error", f"不可达：{exc}")
            self.serviceChanged.emit("data", "error", "不可达")
            return False

    def _sync_data(self) -> None:
        self.stepChanged.emit("sync", "running", "正在读取同步目录…")
        cache_root = self.cache_root
        if cache_root is None:
            cache_root = Path(
                QStandardPaths.writableLocation(
                    QStandardPaths.StandardLocation.AppLocalDataLocation
                )
            ) / "client_cache"
        cache = LocalDataCache(cache_root)
        try:
            manifest = _request_json(self.data_url + "/api/v1/sync/manifest")
            cursor = cache.cursor()
            query = "" if cursor is None else "?" + urllib.parse.urlencode({"cursor": cursor})
            changes = _request_json(self.data_url + "/api/v1/sync/changes" + query)
            records = list(changes.get("records", []))
            deleted = list(changes.get("deleted", []))
            spectra = list(changes.get("spectra", []))
            cache.apply_records(records, deleted)

            missing = [
                item
                for item in spectra
                if not cache.has_spectrum(str(item["id"]), str(item["sha256"]))
            ]
            required_bytes = sum(int(item["byte_length"]) for item in missing)
            if shutil.disk_usage(cache_root).free < required_bytes + 100_000_000:
                raise OSError("本地磁盘空间不足，无法完成数据同步")

            total = len(missing)
            self.syncProgress.emit(0, total)
            for index, metadata in enumerate(missing, start=1):
                if self.isInterruptionRequested():
                    self.stepChanged.emit("sync", "warn", "同步已取消")
                    return
                payload = _request_bytes(self.data_url + str(metadata["download_path"]))
                cache.save_spectrum(metadata, payload)
                self.syncProgress.emit(index, total)
                self.stepChanged.emit("sync", "running", f"正在下载谱图 {index} / {total}")

            cache.set_cursor(str(changes.get("next_cursor", manifest["cursor"])))
            detail = f"同步完成 · 新增或更新 {len(records)} 条记录、{total} 条谱图"
            self.stepChanged.emit("sync", "good", detail)
        finally:
            cache.close()


class InitializationPage(QWidget):
    """应用启动期间显示的非阻塞初始化进度页。"""

    STEP_LABELS = (
        ("config", "加载本机配置"),
        ("instrument", "检查仪器执行服务"),
        ("pv", "检查受控 PV"),
        ("data", "检查数据服务"),
        ("sync", "同步中央数据"),
    )

    def __init__(self) -> None:
        super().__init__(objectName="pageRoot")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 42, 48, 42)
        layout.setSpacing(18)
        layout.addWidget(QLabel("正在初始化", objectName="pageTitle"))
        layout.addWidget(
            QLabel("正在检查服务、设备连接并更新本地数据。", objectName="pageDescription")
        )

        panel = QFrame(objectName="panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(22, 20, 22, 20)
        panel_layout.setSpacing(14)
        self.step_values: dict[str, QLabel] = {}
        for key, label in self.STEP_LABELS:
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            row.addStretch()
            value = QLabel("等待", objectName="mutedText")
            row.addWidget(value)
            panel_layout.addLayout(row)
            self.step_values[key] = value
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        panel_layout.addWidget(self.progress)
        layout.addWidget(panel)
        layout.addStretch()

    def reset(self) -> None:
        for value in self.step_values.values():
            value.setText("等待")
            value.setProperty("state", "idle")
        self.progress.setVisible(False)

    def set_step(self, key: str, state: str, detail: str) -> None:
        value = self.step_values.get(key)
        if value is None:
            return
        prefix = {"running": "↻", "good": "✓", "warn": "!", "error": "×"}.get(state, "○")
        value.setText(f"{prefix}  {detail}")
        value.setProperty("state", "warn" if state == "running" else state)
        value.style().unpolish(value)
        value.style().polish(value)

    def set_sync_progress(self, current: int, total: int) -> None:
        self.progress.setVisible(total > 0)
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(current)


def _request_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=3.0) as response:
        return json.loads(response.read().decode("utf-8"))


def _request_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=5.0) as response:
        return response.read()
