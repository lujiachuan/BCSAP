"""客户端启动初始化、PV 检查和中央数据本地镜像。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QThread, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
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
    """按目标子集顺序完成依赖检查；支持单项重试（不重复已成功的检查）。"""

    stepChanged = Signal(str, str, str)
    serviceChanged = Signal(str, str, str)
    pvStatus = Signal(int, int, list)
    essentialReady = Signal()
    syncProgress = Signal(int, int)
    completed = Signal(bool, str)

    def __init__(
        self,
        data_url: str,
        instrument_url: str,
        parent=None,
        cache_root: Path | None = None,
        targets: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(parent)
        self.data_url = data_url.rstrip("/")
        self.instrument_url = instrument_url.rstrip("/")
        self.cache_root = cache_root
        # targets 是 ("instrument", "data") 的子集；缺省两者都检查。
        self._targets = targets or ("instrument", "data")

    def run(self) -> None:
        essential_emitted = False
        try:
            self.stepChanged.emit("config", "good", "本机配置已加载")
            all_ok = True
            want_instrument = "instrument" in self._targets
            want_data = "data" in self._targets
            data_ok = False
            if want_instrument and want_data:
                # 两个地址互不依赖，并行探测；仪器任务内部在可达后继续检查 PV。
                # 最大并发固定为 2，避免启动阶段制造额外线程压力。
                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="startup") as pool:
                    instrument_future = pool.submit(self._check_instrument)
                    data_future = pool.submit(self._check_data_service)
                    instrument_ok = instrument_future.result()
                    data_ok = data_future.result()
                all_ok = instrument_ok and data_ok
            else:
                if want_instrument:
                    all_ok &= self._check_instrument()
                if want_data:
                    data_ok = self._check_data_service()
                    all_ok &= data_ok
            if self.isInterruptionRequested():
                return
            # 必要条件（可进入主界面的检查）已完成：允许先进入，同步继续后台执行。
            self.essentialReady.emit()
            essential_emitted = True
            if self.isInterruptionRequested():
                return
            if want_data:
                if data_ok:
                    self._sync_data()
                else:
                    self.stepChanged.emit("sync", "warn", "已跳过，继续使用本地缓存")
            scope = "启动初始化" if len(self._targets) == 2 else "单项重试"
            message = f"{scope}已完成"
            if not all_ok:
                message += "（存在失败项，见上方状态）"
            self.completed.emit(all_ok, message)
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
        except Exception as exc:  # 连接类错误统一展示，不区分细节
            self.stepChanged.emit("instrument", "error", f"不可达：{exc}")
            self.stepChanged.emit("pv", "warn", "已跳过")
            self.serviceChanged.emit("instrument", "error", "不可达")
            self.serviceChanged.emit("epics", "idle", "未检查")
            self.pvStatus.emit(0, 0, ["仪器执行服务不可达，未执行 PV 检查。"])
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
            details = _collect_pv_details(result)
            self.pvStatus.emit(connected, total, details)
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
    """应用启动期间显示的非阻塞初始化进度页（M1：步骤行重试 + 进入/设置操作）。"""

    retryRequested = Signal(str)
    enterRequested = Signal()
    settingsRequested = Signal()

    STEP_LABELS = (
        ("config", "加载本机配置"),
        ("instrument", "检查仪器执行服务"),
        ("pv", "检查受控 PV"),
        ("data", "检查数据服务"),
        ("sync", "同步中央数据"),
    )

    # 步骤失败时对应重试的 Worker 目标（pv 重试会连带检查仪器服务）
    RETRY_TARGETS = {
        "instrument": "instrument",
        "pv": "instrument",
        "data": "data",
        "sync": "data",
    }

    _PREFIX = {"running": "↻", "good": "✓", "warn": "!", "error": "×"}

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
        panel_layout.setContentsMargins(24, 20, 24, 20)
        panel_layout.setSpacing(12)
        self.step_values: dict[str, QLabel] = {}
        self.retry_buttons: dict[str, QPushButton] = {}
        for key, label in self.STEP_LABELS:
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            row.addStretch()
            value = QLabel("等待", objectName="mutedText")
            value.setMinimumWidth(220)
            row.addWidget(value)
            retry = QPushButton("重试", objectName="inlineRetry")
            retry.setVisible(False)
            retry.clicked.connect(lambda _checked=False, k=key: self.retryRequested.emit(k))
            row.addWidget(retry)
            panel_layout.addLayout(row)
            self.step_values[key] = value
            self.retry_buttons[key] = retry
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        panel_layout.addWidget(self.progress)
        layout.addWidget(panel)

        self.actions = QWidget()
        actions_layout = QHBoxLayout(self.actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        self.action_note = QLabel("", objectName="mutedText")
        self.action_note.setWordWrap(True)
        actions_layout.addWidget(self.action_note, 1)
        settings = QPushButton("打开系统设置")
        settings.clicked.connect(self.settingsRequested.emit)
        actions_layout.addWidget(settings)
        self.enter_button = QPushButton("进入工作台", objectName="primaryButton")
        self.enter_button.clicked.connect(self.enterRequested.emit)
        actions_layout.addWidget(self.enter_button)
        self.actions.setVisible(False)
        layout.addWidget(self.actions)
        layout.addStretch()

    def reset(self) -> None:
        for value in self.step_values.values():
            value.setText("等待")
            value.setProperty("state", "idle")
        for button in self.retry_buttons.values():
            button.setVisible(False)
            button.setEnabled(True)
        self.progress.setVisible(False)
        self.actions.setVisible(False)

    def set_step(self, key: str, state: str, detail: str) -> None:
        value = self.step_values.get(key)
        if value is None:
            return
        prefix = self._PREFIX.get(state, "○")
        value.setText(f"{prefix}  {detail}")
        value.setProperty("state", "warn" if state == "running" else state)
        value.style().unpolish(value)
        value.style().polish(value)
        retry = self.retry_buttons.get(key)
        if retry is not None:
            retry.setVisible(state == "error" and key in self.RETRY_TARGETS)

    def set_retry_enabled(self, enabled: bool) -> None:
        """初始化任务运行期间禁点重试，结束后放开。"""
        for button in self.retry_buttons.values():
            button.setEnabled(enabled)

    def set_sync_progress(self, current: int, total: int) -> None:
        self.progress.setVisible(total > 0)
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(current)

    def set_actions(self, mode: str) -> None:
        """mode: hidden / ready / degraded / offline"""
        if mode == "hidden":
            self.actions.setVisible(False)
            return
        if mode == "ready":
            note, text = "全部关键检查完成，可进入主界面。", "进入工作台"
        elif mode == "degraded":
            note, text = (
                "部分服务不可用，可离线进入（扫谱与自动调束将保持禁用）。",
                "离线进入",
            )
        else:  # offline
            note = "服务当前均不可达，将以离线模式进入（业务页可浏览，控制禁用）。"
            text = "进入（离线）"
        self.action_note.setText(note)
        self.enter_button.setText(text)
        self.actions.setVisible(True)


def _collect_pv_details(result: dict) -> list[str]:
    """把 PV 健康接口结果整理为可展示的失败明细（名称 + 原因）。"""
    raw = result.get("details")
    if not isinstance(raw, list) or not raw:
        summary = result.get("summary", {})
        failed = int(summary.get("required_failed", 0))
        if failed:
            return [f"{failed} 个必需 PV 未连接（接口未提供逐项明细）"]
        return []
    lines: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("pv") or "?")
        ok = bool(item.get("ok") or item.get("connected") or item.get("state") == "connected")
        if ok and not item.get("error") and not item.get("detail"):
            lines.append(f"{name}：已连接")
        else:
            error = str(item.get("error") or item.get("detail") or "未连接")
            lines.append(f"{name}：{error}")
    return lines


def _request_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=3.0) as response:
        return json.loads(response.read().decode("utf-8"))


def _request_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=5.0) as response:
        return response.read()
