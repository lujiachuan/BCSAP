"""谱图库：经 data_service Showcase 只读检索 cluster_ms。"""

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
    """谱图库：关键字/元素集检索 + 点选查看元数据与标注。"""

    spectrumSelected = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._active: _JsonGetThread | None = None
        layout = page_layout(self)
        layout.addWidget(
            PageHeading(
                "谱图库",
                "经数据服务 Showcase 只读查询本机 cluster_ms；不直连数据库。"
                " 可按关键字、元素集检索，点选一行查看校正摘要与 working 标注。",
            )
        )

        toolbar = QHBoxLayout()
        self._q = QLineEdit()
        self._q.setPlaceholderText("关键字（名称 / 样品 / 元素集），如 Ag、MoAg")
        self._q.returnPressed.connect(self.refresh)
        self._element = QLineEdit()
        self._element.setPlaceholderText("元素集包含，如 Cu")
        self._element.returnPressed.connect(self.refresh)
        self._limit = QLineEdit("50")
        self._limit.setFixedWidth(56)
        self._limit.setPlaceholderText("条数")
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
        list_panel = Panel("谱图列表", "点击一行加载右侧详情")
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["ID", "名称", "金属", "同位素", "状态", "标注数"]
        )
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self._table.itemSelectionChanged.connect(self._on_row_selected)
        list_panel.body.addWidget(self._table)
        split.addWidget(list_panel)

        detail_panel = Panel("谱图详情", "校正摘要 + working 标注（前若干条）")
        self._detail = QTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setPlaceholderText("选择左侧一条谱图后显示详情。")
        detail_panel.body.addWidget(self._detail)
        split.addWidget(detail_panel)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        layout.addWidget(split, 1)

        # 进入页面时拉一次状态，方便确认服务是否通
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

    def _probe_status(self) -> None:
        url = f"{_data_base_url()}/api/v1/showcase/status"

        def _ok(payload: object) -> None:
            if isinstance(payload, dict):
                self._status.setText(
                    f"{payload.get('status')} · {payload.get('detail') or ''}"
                )

        def _err(msg: str) -> None:
            self._status.setText(f"Showcase 不可用：{msg}")

        self._start(url, _ok, _err)

    def refresh(self) -> None:
        try:
            limit = max(1, min(500, int((self._limit.text() or "50").strip())))
        except ValueError:
            limit = 50
            self._limit.setText("50")
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
            items = list(payload.get("items") or [])
            self._fill_table(items)
            detail = payload.get("detail") or ""
            self._status.setText(f"共 {payload.get('total', len(items))} 条 · {detail}")

        def _err(msg: str) -> None:
            self._btn.setEnabled(True)
            self._status.setText(f"检索失败：{msg}")

        self._start(url, _ok, _err)

    def _fill_table(self, items: list[dict[str, Any]]) -> None:
        self._table.setRowCount(0)
        self._detail.clear()
        for row in items:
            r = self._table.rowCount()
            self._table.insertRow(r)
            values = [
                str(row.get("spectrum_id") or ""),
                str(row.get("name") or ""),
                str(row.get("metal") or ""),
                str(row.get("isotope_class") or ""),
                str(row.get("status") or ""),
                str(row.get("n_annotations") or 0),
            ]
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole, int(row.get("spectrum_id") or 0))
                self._table.setItem(r, c, item)

    def _on_row_selected(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return
        item = self._table.item(rows[0].row(), 0)
        if item is None:
            return
        sid = int(item.data(Qt.ItemDataRole.UserRole) or 0)
        if sid <= 0:
            return
        self.spectrumSelected.emit(sid)
        self._load_detail(sid)

    def _load_detail(self, spectrum_id: int) -> None:
        url = f"{_data_base_url()}/api/v1/showcase/spectra/{spectrum_id}"
        self._detail.setPlainText(f"加载 #{spectrum_id} …")

        def _ok(payload: object) -> None:
            if not isinstance(payload, dict):
                self._detail.setPlainText("详情格式异常")
                return
            peaks = list(payload.get("peaks") or [])
            lines = [
                f"#{payload.get('spectrum_id')}  {payload.get('name')}",
                f"金属={payload.get('metal')}  同位素类={payload.get('isotope_class')}  状态={payload.get('status')}",
                f"元素集={payload.get('element_set')}",
                f"轴策略={payload.get('axis_policy')}",
                f"校正方法={payload.get('calibration_method') or '（无）'}",
                f"提示={payload.get('axis_hint') or '（无）'}",
                f"标注峰 {len(peaks)} 条（显示前 40）",
                "",
                "mass\tformula\terr\tintensity",
            ]
            for p in peaks[:40]:
                lines.append(
                    f"{p.get('peak_mass')}\t{p.get('formula') or ''}\t"
                    f"{p.get('err') if p.get('err') is not None else ''}\t"
                    f"{p.get('intensity') if p.get('intensity') is not None else ''}"
                )
            if len(peaks) > 40:
                lines.append(f"… 其余 {len(peaks) - 40} 条省略")
            self._detail.setPlainText("\n".join(lines))

        def _err(msg: str) -> None:
            self._detail.setPlainText(f"加载失败：{msg}")

        self._start(url, _ok, _err)


PAGE_SPEC = PageSpec(
    key="library",
    label="谱图库",
    icon="database",
    section="data",
    factory=LibraryPage,
)
