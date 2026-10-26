"""谱图分析：两步界面——①筛选（左表右图）②分析（左图右详情+DeepSeek）。"""

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
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from apps.desktop_client.pages.common import page_layout, primary_button
from apps.desktop_client.pages.library import (
    LibraryPage,
    _BytesThread,
    _LONG_DECODE_MAX_W,
    _LongImageView,
)
from apps.desktop_client.pages.registry import PageSpec
from apps.desktop_client.pages.settings import DEFAULT_SERVICE_URLS
from apps.desktop_client.widgets import PageHeading, Panel

_RUNNING: set[QThread] = set()
_LIST_HEADERS = ("ID", "名称", "金属", "元素集", "状态", "峰数")
_DS_URL = "https://api.deepseek.com/chat/completions"
_DS_MODEL = "deepseek-chat"
_STEPS = (
    ("筛选", "左：列表 · 右：长质谱图"),
    ("分析", "左：长图 · 右：详情 + DeepSeek"),
)


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
    """两步：筛选（表+图）→ 分析（图+详情+AI）。"""

    def __init__(self) -> None:
        super().__init__()
        self._selected_id = 0
        self._selected_name = ""
        self._detail: dict[str, Any] = {}
        self._history: list[dict[str, str]] = []
        self._long_pix = QPixmap()
        self._step = 0

        layout = page_layout(self)
        layout.addWidget(
            PageHeading(
                "谱图分析",
                "① 筛选：左边列表、右边长图 · ② 分析：左边长图、右边详情与 DeepSeek。",
            )
        )

        # 步骤条
        step_row = QHBoxLayout()
        self._step_buttons: list[QPushButton] = []
        self._step_group = QButtonGroup(self)
        self._step_group.setExclusive(True)
        for i, (title, tip) in enumerate(_STEPS):
            btn = QPushButton(f"{i + 1}. {title}")
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.setObjectName("stepChip")
            self._step_group.addButton(btn, i)
            self._step_buttons.append(btn)
            step_row.addWidget(btn)
        self._step_group.idClicked.connect(self._goto_step)
        self._step_buttons[0].setChecked(True)
        step_row.addStretch(1)
        layout.addLayout(step_row)

        # 筛选条（仅第 1 步显示）
        self._filter_bar = QWidget()
        bar = QHBoxLayout(self._filter_bar)
        bar.setContentsMargins(0, 0, 0, 0)
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
        layout.addWidget(self._filter_bar)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_pick_page())
        self._stack.addWidget(self._build_anal_page())
        layout.addWidget(self._stack, 1)

        # 底栏
        foot = QHBoxLayout()
        self._btn_back = QPushButton("上一步")
        self._btn_back.clicked.connect(lambda: self._goto_step(0))
        self._current = QLabel("未选谱", objectName="mutedText")
        self._status = QLabel("", objectName="mutedText")
        self._btn_cta = primary_button("进入分析")
        self._btn_cta.clicked.connect(self._run_footer_cta)
        foot.addWidget(self._btn_back)
        foot.addWidget(self._current, 1)
        foot.addWidget(self._status, 1)
        foot.addWidget(self._btn_cta)
        layout.addLayout(foot)

        key = _load_deepseek_key()
        self._status.setText("DeepSeek 已就绪" if key else "未找到 DeepSeek API Key")
        self._update_chrome()

    def _build_pick_page(self) -> QWidget:
        """① 左列表 · 右长图。"""
        w = QWidget()
        root = QHBoxLayout(w)
        root.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Orientation.Horizontal)
        self._pick_split = split

        left = Panel("筛选选谱", "")
        self._table = QTableWidget(0, len(_LIST_HEADERS))
        self._table.setHorizontalHeaderLabels(list(_LIST_HEADERS))
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self._table.itemSelectionChanged.connect(self._on_select)
        left.body.addWidget(self._table)
        split.addWidget(left)

        right = Panel("标注长图", "选中后自动加载")
        pick_bar = QHBoxLayout()
        self._btn_load_long = QPushButton("加载长图")
        self._btn_load_long.clicked.connect(self._load_long_image)
        self._img_hint = QLabel("", objectName="mutedText")
        pick_bar.addWidget(self._btn_load_long)
        pick_bar.addWidget(self._img_hint, 1)
        right.body.addLayout(pick_bar)
        self._long_host_pick = right.body
        self._long_view = _LongImageView("在左侧选一条谱图")
        right.body.addWidget(self._long_view, 1)
        split.addWidget(right)

        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        root.addWidget(split)
        return w

    def _build_anal_page(self) -> QWidget:
        """② 左长图 · 右详情+AI。"""
        w = QWidget()
        root = QHBoxLayout(w)
        root.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Orientation.Horizontal)
        self._anal_split = split

        left = Panel("标注长图", "")
        self._long_host_anal = left.body
        # 长图控件在步骤切换时在两侧之间挪动，此处先放占位提示
        self._anal_img_placeholder = QLabel(
            "从「筛选」选谱后进入本步", objectName="mutedText"
        )
        self._anal_img_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        left.body.addWidget(self._anal_img_placeholder, 1)
        split.addWidget(left)

        right = QSplitter(Qt.Orientation.Vertical)
        summary = Panel("当前谱详情", "")
        self._summary = QTextEdit()
        self._summary.setReadOnly(True)
        self._summary.setPlaceholderText("进入分析前请先选谱…")
        summary.body.addWidget(self._summary)
        right.addWidget(summary)

        chat = Panel("DeepSeek 分析", "基于峰表与元数据的问答")
        self._chat = QTextEdit()
        self._chat.setReadOnly(True)
        self._chat.setPlaceholderText("分析结果会显示在这里…")
        self._ask = QPlainTextEdit()
        self._ask.setPlaceholderText(
            "可选补充问题；留空则做默认解读。"
        )
        self._ask.setMaximumHeight(72)
        ask_row = QHBoxLayout()
        self._btn_ask = primary_button("分析这张谱")
        self._btn_ask.clicked.connect(self._run_deepseek)
        self._btn_clear = QPushButton("清空对话")
        self._btn_clear.clicked.connect(self._clear_chat)
        ask_row.addWidget(self._btn_ask)
        ask_row.addWidget(self._btn_clear)
        ask_row.addStretch(1)
        chat.body.addWidget(self._chat, 1)
        chat.body.addWidget(self._ask)
        chat.body.addLayout(ask_row)
        right.addWidget(chat)
        right.setStretchFactor(0, 1)
        right.setStretchFactor(1, 2)
        right.setSizes([180, 360])

        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split)
        return w

    def _place_long_view(self, step: int) -> None:
        """把同一张长图控件放到当前步骤的对应侧。"""
        self._long_view.setParent(None)
        if step == 0:
            self._anal_img_placeholder.show()
            self._long_host_pick.addWidget(self._long_view, 1)
        else:
            self._anal_img_placeholder.hide()
            # 占位去掉后插入长图
            self._long_host_anal.addWidget(self._long_view, 1)
        self._long_view.show()
        if not self._long_pix.isNull():
            self._long_view.set_pixmap(self._long_pix)

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

    def _goto_step(self, index: int) -> None:
        index = max(0, min(len(_STEPS) - 1, int(index)))
        if index == 1 and self._selected_id <= 0:
            self._status.setText("请先在筛选页选一条谱")
            self._step_buttons[0].setChecked(True)
            return
        self._step = index
        self._stack.setCurrentIndex(index)
        btn = self._step_buttons[index]
        if not btn.isChecked():
            btn.setChecked(True)
        self._place_long_view(index)
        self._update_chrome()

    def _update_chrome(self) -> None:
        self._filter_bar.setVisible(self._step == 0)
        self._btn_back.setEnabled(self._step > 0)
        if self._selected_id:
            self._current.setText(
                f"当前 #{self._selected_id}  {self._selected_name}"
            )
        else:
            self._current.setText("未选谱")
        self._btn_cta.setText("进入分析" if self._step == 0 else "分析这张谱")

    def _run_footer_cta(self) -> None:
        if self._step == 0:
            self._goto_step(1)
        else:
            self._run_deepseek()

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
        name_item = self._table.item(rows[0].row(), 1)
        if item is None:
            return
        sid = int(item.data(Qt.ItemDataRole.UserRole) or 0)
        if sid <= 0:
            return
        self._selected_id = sid
        self._selected_name = name_item.text() if name_item else ""
        self._update_chrome()
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
            self._selected_name = str(payload.get("name") or self._selected_name)
            lines = [
                f"#{payload.get('spectrum_id')}  {payload.get('name')}",
                f"金属={payload.get('metal') or '—'}  "
                f"元素集={payload.get('element_set') or '—'}",
                f"同位素={payload.get('isotope_class') or '—'}  "
                f"状态={payload.get('status') or '—'}",
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
            self._update_chrome()
            self._load_long_image()

        def _err(msg: str) -> None:
            self._status.setText(f"详情失败：{msg}")
            self._summary.setPlainText(msg)

        self._start(url, _ok, _err)

    def _load_long_image(self) -> None:
        if self._selected_id <= 0:
            self._img_hint.setText("请先选谱")
            return
        sid = self._selected_id
        self._img_hint.setText("查找长图…")
        self._long_view.show_loading(True, "正在加载标注长图…")
        list_url = f"{_data_base_url()}/api/v1/showcase/spectra/{sid}/images"

        def _on_list(payload: object) -> None:
            items: list[dict[str, Any]] = []
            if isinstance(payload, dict):
                items = [
                    it for it in (payload.get("items") or []) if isinstance(it, dict)
                ]
            item = LibraryPage._pick_image_item(items, "annotate")
            if not item:
                item = LibraryPage._pick_image_item(items, "peaks")
            if not item:
                self._long_view.show_loading(False)
                self._long_view.set_placeholder("暂无长图（可先在谱图库出图）")
                self._img_hint.setText("无长图")
                return
            name = str(item.get("name") or "")
            img_url = (
                f"{_data_base_url()}/api/v1/showcase/spectra/"
                f"{sid}/images/{urllib.parse.quote(name)}"
            )
            self._fetch_long_pixmap(img_url, name)

        def _on_list_err(msg: str) -> None:
            name = f"peaks_{sid}_long.png"
            img_url = (
                f"{_data_base_url()}/api/v1/showcase/spectra/"
                f"{sid}/images/{urllib.parse.quote(name)}"
            )
            self._fetch_long_pixmap(img_url, name)
            del msg

        self._start(list_url, _on_list, _on_list_err)

    def _fetch_long_pixmap(self, url: str, name: str) -> None:
        cache_path = LibraryPage._long_cache_path(url)
        if cache_path.is_file() and cache_path.stat().st_size > 1000:
            pix = QPixmap(str(cache_path))
            if not pix.isNull():
                self._long_pix = pix
                self._long_view.show_loading(False)
                self._long_view.set_pixmap(pix)
                self._img_hint.setText(f"已缓存 · {name}")
                return

        self._long_view.show_loading(True, "正在下载长图…")
        thread = _BytesThread(url, self)
        _RUNNING.add(thread)

        def _ok(payload: object) -> None:
            _RUNNING.discard(thread)
            if not isinstance(payload, (bytes, bytearray)):
                self._long_view.show_loading(False)
                self._long_view.set_placeholder("图片数据异常")
                return
            raw = bytes(payload)
            if len(raw) < 1000 or not raw.startswith(b"\x89PNG"):
                self._long_view.show_loading(False)
                self._long_view.set_placeholder("暂无该长图")
                self._img_hint.setText("无图")
                return
            self._long_view.show_loading(True, "正在解码长图…")
            holder = LibraryPage.__new__(LibraryPage)
            pix = LibraryPage._decode_pixmap(
                holder, raw, _LONG_DECODE_MAX_W, cache_path=cache_path
            )
            self._long_view.show_loading(False)
            if pix is None or pix.isNull():
                err = getattr(holder, "_last_decode_error", "") or "解码失败"
                self._long_view.set_placeholder(str(err))
                self._img_hint.setText("解码失败")
                return
            self._long_pix = pix
            self._long_view.set_pixmap(pix)
            self._img_hint.setText(name)

        def _err(msg: str) -> None:
            _RUNNING.discard(thread)
            self._long_view.show_loading(False)
            self._long_view.set_placeholder("下载失败，请稍后重试")
            self._img_hint.setText(msg[:80])

        thread.ok.connect(_ok)
        thread.failed.connect(_err)
        thread.finished.connect(thread.deleteLater)
        thread.start()

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
            self._status.setText("请先在筛选页选中一条谱")
            return
        if self._step != 1:
            self._goto_step(1)
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
