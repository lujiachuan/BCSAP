"""谱图分析：左侧筛选选谱 + DeepSeek 解读。"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from apps.desktop_client.pages.common import page_layout, primary_button
from apps.desktop_client.pages.registry import PageSpec
from apps.desktop_client.pages.settings import DEFAULT_SERVICE_URLS
from apps.desktop_client.widgets import PageHeading, Panel

_RUNNING: set[QThread] = set()
_LIST_HEADERS = ("ID", "名称", "金属", "元素集", "状态", "峰数")
_DS_URL = "https://api.deepseek.com/chat/completions"
_DS_MODEL = "deepseek-chat"


def _data_base_url() -> str:
    settings = QSettings("SpectrumPlatform", "DesktopClient")
    return str(
        settings.value("service/dataUrl", DEFAULT_SERVICE_URLS["data"])
    ).rstrip("/")


def _load_deepseek_key() -> str:
    """优先 QSettings / 环境变量，其次本地「密码」文件（不入库）。"""
    settings = QSettings("SpectrumPlatform", "DesktopClient")
    saved = str(settings.value("ai/deepseekApiKey", "") or "").strip()
    if saved:
        return saved
    env = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env:
        return env
    # 工作区旁的本地密码文件
    for base in (Path.cwd(), Path.cwd().parent, Path(__file__).resolve().parents[4]):
        path = base / "密码"
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = re.search(r"key\s*:\s*(sk-[A-Za-z0-9]+)", text)
        if m:
            key = m.group(1)
            settings.setValue("ai/deepseekApiKey", key)
            return key
    return ""


class _JsonThread(QThread):
    ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        url: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._url = url
        self._method = method.upper()
        self._body = body
        self._headers = headers or {}
        self._timeout = timeout

    def run(self) -> None:
        try:
            data = None
            headers = {"Accept": "application/json", **self._headers}
            if self._body is not None:
                data = json.dumps(self._body, ensure_ascii=False).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            req = urllib.request.Request(
                self._url, data=data, headers=headers, method=self._method
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
            self.ok.emit(json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            self.failed.emit(f"HTTP {exc.code}: {body or exc.reason}")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class AnalysisPage(QWidget):
    """左：筛选列表；右上：谱摘要；右下：DeepSeek 分析问答。"""

    def __init__(self) -> None:
        super().__init__()
        self._selected_id = 0
        self._detail: dict[str, Any] = {}
        self._history: list[dict[str, str]] = []

        layout = page_layout(self)
        layout.addWidget(
            PageHeading(
                "谱图分析",
                "先在左侧筛选并选中一条谱，再用 DeepSeek 解读峰表与校正信息。",
            )
        )

        # 筛选条
        bar = QHBoxLayout()
        self._q = QLineEdit()
        self._q.setPlaceholderText("元素 / 关键字，如 Cu、Ag …")
        self._q.setMinimumWidth(220)
        self._q.setMinimumHeight(34)
        self._q.returnPressed.connect(self.refresh_list)
        self._only_good3 = QCheckBox("只看 Good3")
        self._only_good3.stateChanged.connect(self.refresh_list)
        self._btn_refresh = primary_button("刷新列表")
        self._btn_refresh.clicked.connect(self.refresh_list)
        bar.addWidget(QLabel("搜索"))
        bar.addWidget(self._q, 2)
        bar.addWidget(self._only_good3)
        bar.addWidget(self._btn_refresh)
        layout.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal)

        # —— 左：选谱 ——
        left = Panel("筛选选谱", "")
        self._table = QTableWidget(0, len(_LIST_HEADERS))
        self._table.setHorizontalHeaderLabels(list(_LIST_HEADERS))
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.itemSelectionChanged.connect(self._on_select)
        left.body.addWidget(self._table)
        split.addWidget(left)

        # —— 右：摘要 + DS ——
        right = QWidget()
        right_l = QVBoxLayout(right)
        right_l.setContentsMargins(0, 0, 0, 0)
        right_l.setSpacing(10)

        summary = Panel("当前谱摘要", "")
        self._summary = QTextEdit()
        self._summary.setReadOnly(True)
        self._summary.setMaximumHeight(180)
        self._summary.setPlaceholderText("在左侧选一条谱图…")
        summary.body.addWidget(self._summary)
        right_l.addWidget(summary)

        chat = Panel("DeepSeek 分析", "基于峰表与元数据的问答")
        self._chat = QTextEdit()
        self._chat.setReadOnly(True)
        self._chat.setPlaceholderText("分析结果会显示在这里…")
        self._ask = QPlainTextEdit()
        self._ask.setPlaceholderText(
            "可选：补充问题，例如「主峰是否合理？可能的分子式有哪些？」留空则做默认解读。"
        )
        self._ask.setMaximumHeight(72)
        ask_row = QHBoxLayout()
        self._btn_ask = primary_button("分析这张谱")
        self._btn_ask.clicked.connect(self._run_deepseek)
        self._btn_clear = QPushButton("清空对话")
        self._btn_clear.clicked.connect(self._clear_chat)
        self._status = QLabel("", objectName="mutedText")
        ask_row.addWidget(self._btn_ask)
        ask_row.addWidget(self._btn_clear)
        ask_row.addWidget(self._status, 1)
        chat.body.addWidget(self._chat, 1)
        chat.body.addWidget(self._ask)
        chat.body.addLayout(ask_row)
        right_l.addWidget(chat, 1)

        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        layout.addWidget(split, 1)

        key = _load_deepseek_key()
        if key:
            self._status.setText("DeepSeek 已就绪")
        else:
            self._status.setText("未找到 API Key（可设环境变量 DEEPSEEK_API_KEY）")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._table.rowCount() == 0:
            self.refresh_list()

    def closeEvent(self, event) -> None:  # noqa: N802
        for thread in list(_RUNNING):
            if thread.parent() is self:
                thread.wait(2000)
                _RUNNING.discard(thread)
        super().closeEvent(event)

    def _start(
        self,
        url: str,
        on_ok,
        on_err,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> None:
        thread = _JsonThread(
            url,
            method=method,
            body=body,
            headers=headers,
            timeout=timeout,
            parent=self,
        )
        _RUNNING.add(thread)

        def _ok(payload: object) -> None:
            _RUNNING.discard(thread)
            on_ok(payload)

        def _err(msg: str) -> None:
            _RUNNING.discard(thread)
            on_err(msg)

        thread.ok.connect(_ok)
        thread.failed.connect(_err)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def refresh_list(self) -> None:
        base = _data_base_url()
        el = self._q.text().strip()
        if self._only_good3.isChecked():
            params: dict[str, str] = {}
            if el:
                params["element"] = el
            qs = urllib.parse.urlencode(params)
            url = f"{base}/api/v1/showcase/spectra/good3" + (f"?{qs}" if qs else "")
        else:
            params = {"limit": "80"}
            if el:
                params["element"] = el
            url = f"{base}/api/v1/showcase/spectra/rank?{urllib.parse.urlencode(params)}"
        self._btn_refresh.setEnabled(False)

        def _ok(payload: object) -> None:
            self._btn_refresh.setEnabled(True)
            if not isinstance(payload, dict):
                return
            items = list(payload.get("items") or [])
            self._table.setRowCount(0)
            for row in items:
                if not isinstance(row, dict):
                    continue
                r = self._table.rowCount()
                self._table.insertRow(r)
                vals = [
                    str(row.get("spectrum_id") or ""),
                    str(row.get("name") or ""),
                    str(row.get("metal") or row.get("chip") or ""),
                    str(row.get("element_set") or ""),
                    str(row.get("status") or ""),
                    str(row.get("peak_count") or 0),
                ]
                for c, text in enumerate(vals):
                    item = QTableWidgetItem(text)
                    if c == 0:
                        item.setData(
                            Qt.ItemDataRole.UserRole, int(row.get("spectrum_id") or 0)
                        )
                    self._table.setItem(r, c, item)

        def _err(msg: str) -> None:
            self._btn_refresh.setEnabled(True)
            self._status.setText(f"列表失败：{msg}")

        self._start(url, _ok, _err)

    def _on_select(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return
        item = self._table.item(rows[0].row(), 0)
        if item is None:
            return
        sid = int(item.data(Qt.ItemDataRole.UserRole) or 0)
        if sid <= 0:
            return
        self._selected_id = sid
        self._load_detail(sid)

    def _load_detail(self, sid: int) -> None:
        url = f"{_data_base_url()}/api/v1/showcase/spectra/{sid}"
        self._status.setText(f"加载 #{sid} …")

        def _ok(payload: object) -> None:
            if not isinstance(payload, dict):
                self._status.setText("详情格式异常")
                return
            self._detail = dict(payload)
            peaks = list(payload.get("peaks") or [])
            lines = [
                f"#{payload.get('spectrum_id')}  {payload.get('name')}",
                f"金属={payload.get('metal') or '—'}  元素集={payload.get('element_set') or '—'}",
                f"同位素={payload.get('isotope_class') or '—'}  状态={payload.get('status') or '—'}",
                f"轴策略={payload.get('axis_policy') or '—'}  "
                f"校正={payload.get('calibration_method') or '（无）'}",
                f"提示={payload.get('axis_hint') or '（无）'}",
                f"标注峰 {len(peaks)} 条",
                "",
                "mass\tformula\terr\tintensity",
            ]
            for p in peaks[:60]:
                if not isinstance(p, dict):
                    continue
                lines.append(
                    f"{p.get('peak_mass')}\t{p.get('formula') or ''}\t"
                    f"{p.get('err') if p.get('err') is not None else ''}\t"
                    f"{p.get('intensity') if p.get('intensity') is not None else ''}"
                )
            if len(peaks) > 60:
                lines.append(f"… 其余 {len(peaks) - 60} 条省略")
            self._summary.setPlainText("\n".join(lines))
            self._status.setText(f"已选 #{sid} · 峰 {len(peaks)}")

        def _err(msg: str) -> None:
            self._status.setText(f"详情失败：{msg}")
            self._summary.setPlainText(msg)

        self._start(url, _ok, _err)

    def _clear_chat(self) -> None:
        self._history.clear()
        self._chat.clear()

    def _append_chat(self, role: str, text: str) -> None:
        color = "#1a4a7a" if role == "user" else "#222"
        bg = "#e8f1fb" if role == "user" else "#f4f4f4"
        safe = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br/>")
        )
        self._chat.append(
            f'<div style="margin:6px 0;padding:8px 10px;border-radius:8px;'
            f'background:{bg};color:{color};">{safe}</div>'
        )

    def _build_context(self) -> str:
        d = self._detail
        peaks = list(d.get("peaks") or [])
        lines = [
            f"谱图 ID: {d.get('spectrum_id') or self._selected_id}",
            f"名称: {d.get('name')}",
            f"金属: {d.get('metal')}",
            f"元素集: {d.get('element_set')}",
            f"同位素类: {d.get('isotope_class')}",
            f"轴策略: {d.get('axis_policy')}",
            f"校正方法: {d.get('calibration_method')}",
            f"轴提示: {d.get('axis_hint')}",
            f"峰数: {len(peaks)}",
            "峰表 (mass, formula, err, intensity):",
        ]
        for p in peaks[:80]:
            if isinstance(p, dict):
                lines.append(
                    f"  {p.get('peak_mass')}, {p.get('formula') or ''}, "
                    f"{p.get('err')}, {p.get('intensity')}"
                )
        return "\n".join(lines)

    def _run_deepseek(self) -> None:
        if self._selected_id <= 0 or not self._detail:
            self._status.setText("请先在左侧选中一条谱")
            return
        key = _load_deepseek_key()
        if not key:
            self._status.setText("缺少 DeepSeek API Key")
            return
        question = self._ask.toPlainText().strip() or (
            "请根据下列质谱标注摘要，用中文给出：1) 谱图整体判断；"
            "2) 主要峰/分子式是否合理；3) 轴校正或元素集方面的风险；"
            "4) 建议的下一步实验或复核动作。简洁分点。"
        )
        user_msg = f"{question}\n\n—— 谱图数据 ——\n{self._build_context()}"
        self._append_chat("user", question)
        self._history.append({"role": "user", "content": user_msg})
        self._ask.clear()
        self._btn_ask.setEnabled(False)
        self._status.setText("DeepSeek 分析中…")

        body = {
            "model": _DS_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是团簇质谱实验助手。根据给定的谱图元数据与峰表做专业、简洁的中文分析。"
                        "不要编造未给出的峰；不确定时明确说明。"
                    ),
                },
                *self._history[-6:],
            ],
            "temperature": 0.3,
        }

        def _ok(payload: object) -> None:
            self._btn_ask.setEnabled(True)
            if not isinstance(payload, dict):
                self._status.setText("DeepSeek 返回异常")
                return
            choices = payload.get("choices") or []
            text = ""
            if choices and isinstance(choices[0], dict):
                msg = choices[0].get("message") or {}
                text = str(msg.get("content") or "").strip()
            if not text:
                text = json.dumps(payload, ensure_ascii=False)[:2000]
            self._append_chat("assistant", text)
            self._history.append({"role": "assistant", "content": text})
            self._status.setText("分析完成")

        def _err(msg: str) -> None:
            self._btn_ask.setEnabled(True)
            self._append_chat("assistant", f"调用失败：{msg}")
            self._status.setText("DeepSeek 失败")

        self._start(
            _DS_URL,
            _ok,
            _err,
            method="POST",
            body=body,
            headers={"Authorization": f"Bearer {key}"},
            timeout=120.0,
        )


PAGE_SPEC = PageSpec(
    key="analysis",
    label="谱图分析",
    icon="analysis",
    section="data",
    factory=AnalysisPage,
)
