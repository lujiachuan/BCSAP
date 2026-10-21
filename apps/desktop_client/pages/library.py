"""谱图库：经 data_service Showcase 只读检索并展示 cluster_ms。"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from PySide6.QtCore import QSettings, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from apps.desktop_client.pages.common import page_layout, primary_button
from apps.desktop_client.pages.registry import PageSpec
from apps.desktop_client.pages.settings import DEFAULT_SERVICE_URLS
from apps.desktop_client.widgets import PageHeading, Panel

_RUNNING: set[QThread] = set()

_LIST_HEADERS = ("ID", "名称", "样品", "金属", "元素集", "同位素", "状态", "标注", "峰数")
_PEAK_HEADERS = ("质量", "分子式", "误差", "强度", "秩")


def _data_base_url() -> str:
    settings = QSettings("SpectrumPlatform", "DesktopClient")
    return str(
        settings.value("service/dataUrl", DEFAULT_SERVICE_URLS["data"])
    ).rstrip("/")


class _JsonGetThread(QThread):
    """后台 GET JSON，避免卡住 UI。"""

    ok = Signal(object)
    failed = Signal(str)

    def __init__(self, url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._url = url

    def run(self) -> None:
        try:
            req = urllib.request.Request(
                self._url,
                headers={"Accept": "application/json"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
            self.ok.emit(json.loads(raw))
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            self.failed.emit(f"HTTP {exc.code}: {body or exc.reason}")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class LibraryPage(QWidget):
    """Showcase 谱图库：检索列表 + 元数据 + working 标注表。"""

    spectrumSelected = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._selected_id = 0
        layout = page_layout(self)
        layout.addWidget(
            PageHeading(
                "谱图库",
                "Showcase 只读：经数据服务拉取 cluster_ms 目录与标注，不直连数据库。"
                " 左侧检索列表，右侧查看校正摘要与 working 峰表。",
            )
        )

        self._banner = QLabel("", objectName="mutedText")
        self._banner.setWordWrap(True)
        self._banner.hide()
        layout.addWidget(self._banner)

        toolbar = QHBoxLayout()
        self._q = QLineEdit()
        self._q.setPlaceholderText("关键字（名称 / 样品 / 元素集），如 Ag、MoAg")
        self._q.returnPressed.connect(self.refresh)
        self._element = QLineEdit()
        self._element.setPlaceholderText("元素集包含，如 Cu")
        self._element.returnPressed.connect(self.refresh)
        self._limit = QLineEdit("100")
        self._limit.setFixedWidth(56)
        self._limit.setToolTip("返回条数上限（1–500）")
        self._btn = primary_button("检索")
        self._btn.clicked.connect(self.refresh)
        self._status = QLabel("尚未检索", objectName="mutedText")
        toolbar.addWidget(QLabel("关键字"))
        toolbar.addWidget(self._q, 2)
        toolbar.addWidget(QLabel("元素集"))
        toolbar.addWidget(self._element, 1)
        toolbar.addWidget(QLabel("上限"))
        toolbar.addWidget(self._limit)
        toolbar.addWidget(self._btn)
        toolbar.addWidget(self._status, 1)
        layout.addLayout(toolbar)

        split = QSplitter(Qt.Orientation.Horizontal)

        list_panel = Panel("谱图目录", "来自 GET /api/v1/showcase/spectra")
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
        self._table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch
        )
        self._table.itemSelectionChanged.connect(self._on_row_selected)
        list_panel.body.addWidget(self._table)
        split.addWidget(list_panel)

        right = QSplitter(Qt.Orientation.Vertical)
        meta_panel = Panel("校正与元数据", "GET /api/v1/showcase/spectra/{id}")
        self._meta = QLabel("选择左侧一条谱图后显示详情。", objectName="mutedText")
        self._meta.setWordWrap(True)
        self._meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        meta_panel.body.addWidget(self._meta)
        right.addWidget(meta_panel)

        peak_panel = Panel("Working 标注", "峰质量 · 分子式 · 误差")
        self._peaks = QTableWidget(0, len(_PEAK_HEADERS))
        self._peaks.setHorizontalHeaderLabels(list(_PEAK_HEADERS))
        self._peaks.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._peaks.setAlternatingRowColors(True)
        self._peaks.verticalHeader().setVisible(False)
        self._peaks.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        peak_panel.body.addWidget(self._peaks)
        right.addWidget(peak_panel)
        right.setStretchFactor(0, 1)
        right.setStretchFactor(1, 3)
        split.addWidget(right)

        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        layout.addWidget(split, 1)

        self._probe_status()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._drain_threads()
        super().closeEvent(event)

    def _drain_threads(self) -> None:
        for thread in list(_RUNNING):
            if thread.parent() is self:
                thread.wait(2000)
                _RUNNING.discard(thread)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._table.rowCount() == 0:
            self.refresh()

    def _start(self, url: str, on_ok, on_err) -> None:
        thread = _JsonGetThread(url, self)
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

    def _set_banner(self, text: str, *, error: bool = False) -> None:
        if not text:
            self._banner.hide()
            self._banner.clear()
            return
        self._banner.setText(text)
        self._banner.setStyleSheet("color: #b00020;" if error else "")
        self._banner.show()

    def _probe_status(self) -> None:
        base = _data_base_url()
        url = f"{base}/api/v1/showcase/status"

        def _ok(payload: object) -> None:
            if not isinstance(payload, dict):
                return
            n = payload.get("n_spectra")
            detail = payload.get("detail") or ""
            self._set_banner("")
            self._status.setText(
                f"{payload.get('status')} · {n if n is not None else '?'} 条 · {detail}"
            )

        def _err(msg: str) -> None:
            self._set_banner(
                f"数据服务不可达（{_data_base_url()}）。"
                "请在本机启动 python -m apps.data_service.main，"
                "或在「系统设置」把数据服务地址改成实际可达 URL。\n"
                f"详情：{msg}",
                error=True,
            )
            self._status.setText("Showcase 不可用")

        self._start(url, _ok, _err)

    def refresh(self) -> None:
        try:
            limit = max(1, min(500, int((self._limit.text() or "100").strip())))
        except ValueError:
            limit = 100
            self._limit.setText("100")
        params: dict[str, str] = {"limit": str(limit)}
        q = self._q.text().strip()
        el = self._element.text().strip()
        if q:
            params["q"] = q
        if el:
            params["element_set"] = el
        query = urllib.parse.urlencode(params)
        url = f"{_data_base_url()}/api/v1/showcase/spectra?{query}"
        self._btn.setEnabled(False)
        self._status.setText("检索中…")

        def _ok(payload: object) -> None:
            self._btn.setEnabled(True)
            if not isinstance(payload, dict):
                self._status.setText("返回格式异常")
                return
            self._set_banner("")
            items = list(payload.get("items") or [])
            self._fill_table(items)
            detail = payload.get("detail") or ""
            self._status.setText(f"显示 {len(items)} / 共 {payload.get('total', len(items))} · {detail}")
            if items and self._table.rowCount() > 0:
                self._table.selectRow(0)

        def _err(msg: str) -> None:
            self._btn.setEnabled(True)
            self._status.setText(f"检索失败：{msg}")
            self._set_banner(
                f"无法拉取谱图目录。请确认数据服务已启动且地址正确：{_data_base_url()}\n{msg}",
                error=True,
            )

        self._start(url, _ok, _err)

    def _fill_table(self, items: list[dict[str, Any]]) -> None:
        self._table.setRowCount(0)
        self._meta.setText("选择左侧一条谱图后显示详情。")
        self._peaks.setRowCount(0)
        self._selected_id = 0
        for row in items:
            r = self._table.rowCount()
            self._table.insertRow(r)
            values = [
                str(row.get("spectrum_id") or ""),
                str(row.get("name") or ""),
                str(row.get("sample_name") or ""),
                str(row.get("metal") or ""),
                str(row.get("element_set") or ""),
                str(row.get("isotope_class") or ""),
                str(row.get("status") or ""),
                str(row.get("n_annotations") or 0),
                str(row.get("peak_count") or 0),
            ]
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                if c == 0:
                    item.setData(
                        Qt.ItemDataRole.UserRole, int(row.get("spectrum_id") or 0)
                    )
                self._table.setItem(r, c, item)

    def _on_row_selected(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return
        item = self._table.item(rows[0].row(), 0)
        if item is None:
            return
        sid = int(item.data(Qt.ItemDataRole.UserRole) or 0)
        if sid <= 0 or sid == self._selected_id:
            return
        self._selected_id = sid
        self.spectrumSelected.emit(sid)
        self._load_detail(sid)

    def _load_detail(self, spectrum_id: int) -> None:
        url = f"{_data_base_url()}/api/v1/showcase/spectra/{spectrum_id}"
        self._meta.setText(f"加载 #{spectrum_id} …")
        self._peaks.setRowCount(0)

        def _ok(payload: object) -> None:
            if not isinstance(payload, dict):
                self._meta.setText("详情格式异常")
                return
            peaks = list(payload.get("peaks") or [])
            self._meta.setText(
                f"#{payload.get('spectrum_id')}  {payload.get('name')}\n"
                f"样品={payload.get('sample_name') or '（无）'}  "
                f"金属={payload.get('metal') or '—'}  "
                f"同位素类={payload.get('isotope_class')}  "
                f"状态={payload.get('status') or '—'}\n"
                f"元素集={payload.get('element_set') or '—'}\n"
                f"轴策略={payload.get('axis_policy')}  "
                f"校正={payload.get('calibration_method') or '（无）'}\n"
                f"提示={payload.get('axis_hint') or '（无）'}\n"
                f"标注峰 {len(peaks)} 条"
            )
            show = peaks[:200]
            self._peaks.setRowCount(0)
            for p in show:
                r = self._peaks.rowCount()
                self._peaks.insertRow(r)
                cells = [
                    "" if p.get("peak_mass") is None else f"{p.get('peak_mass')}",
                    str(p.get("formula") or ""),
                    "" if p.get("err") is None else f"{p.get('err')}",
                    "" if p.get("intensity") is None else f"{p.get('intensity')}",
                    str(p.get("rank_in_peak") or ""),
                ]
                for c, text in enumerate(cells):
                    self._peaks.setItem(r, c, QTableWidgetItem(text))
            if len(peaks) > 200:
                self._meta.setText(
                    self._meta.text() + f"（表中显示前 200 / {len(peaks)}）"
                )

        def _err(msg: str) -> None:
            self._meta.setText(f"加载失败：{msg}")

        self._start(url, _ok, _err)


PAGE_SPEC = PageSpec(
    key="library",
    label="谱图库",
    icon="database",
    section="data",
    factory=LibraryPage,
)
