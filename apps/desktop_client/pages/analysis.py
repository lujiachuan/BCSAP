"""谱图分析：读取 Showcase 标注摘要（分析写回仍旁路 web_manager）。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from PySide6.QtCore import QSettings, QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTextEdit,
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
    ok = Signal(object)
    failed = Signal(str)

    def __init__(self, url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._url = url

    def run(self) -> None:
        try:
            req = urllib.request.Request(
                self._url, headers={"Accept": "application/json"}, method="GET"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.ok.emit(json.loads(resp.read().decode("utf-8")))
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            self.failed.emit(f"HTTP {exc.code}: {body or exc.reason}")
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class AnalysisPage(QWidget):
    """按谱图 ID 拉取 working 标注与校正摘要。"""

    def __init__(self) -> None:
        super().__init__()
        layout = page_layout(self)
        layout.addWidget(
            PageHeading(
                "谱图分析",
                "按 ID 查看 working 标注摘要。完整六步工作流（筛选/长图/校正/交付）请用「谱图库」；"
                "写操作经 data_service 代理 SHOWCASE_UPSTREAM。",
            )
        )
        row = QHBoxLayout()
        row.addWidget(QLabel("谱图 ID"))
        self._sid = QLineEdit("534")
        self._sid.setFixedWidth(100)
        self._sid.returnPressed.connect(self.load)
        self._btn = primary_button("加载标注")
        self._btn.clicked.connect(self.load)
        self._hint = QLabel("", objectName="mutedText")
        row.addWidget(self._sid)
        row.addWidget(self._btn)
        row.addWidget(self._hint, 1)
        layout.addLayout(row)

        panel = Panel("标注与校正", "working lane")
        self._out = QTextEdit()
        self._out.setReadOnly(True)
        panel.body.addWidget(self._out)
        layout.addWidget(panel, 1)

    def closeEvent(self, event) -> None:  # noqa: N802
        for thread in list(_RUNNING):
            if thread.parent() is self:
                thread.wait(2000)
                _RUNNING.discard(thread)
        super().closeEvent(event)

    def load(self) -> None:
        raw = (self._sid.text() or "").strip()
        try:
            sid = int(raw)
        except ValueError:
            self._hint.setText("请输入整数谱图 ID")
            return
        url = f"{_data_base_url()}/api/v1/showcase/spectra/{sid}"
        self._btn.setEnabled(False)
        self._hint.setText("加载中…")
        thread = _JsonGetThread(url, self)
        _RUNNING.add(thread)

        def _ok(payload: object) -> None:
            _RUNNING.discard(thread)
            self._btn.setEnabled(True)
            if not isinstance(payload, dict):
                self._hint.setText("格式异常")
                return
            peaks = list(payload.get("peaks") or [])
            self._hint.setText(
                f"#{payload.get('spectrum_id')} · {payload.get('metal')} · "
                f"{payload.get('axis_policy')} · 标注 {len(peaks)}"
            )
            lines = [
                f"名称: {payload.get('name')}",
                f"元素集: {payload.get('element_set')}",
                f"同位素类: {payload.get('isotope_class')}",
                f"轴策略: {payload.get('axis_policy')}",
                f"校正: {payload.get('calibration_method') or '（无）'}",
                f"提示: {payload.get('axis_hint') or '（无）'}",
                "",
                "mass\tformula\terr",
            ]
            for p in peaks[:80]:
                lines.append(
                    f"{p.get('peak_mass')}\t{p.get('formula') or ''}\t"
                    f"{p.get('err') if p.get('err') is not None else ''}"
                )
            if len(peaks) > 80:
                lines.append(f"… 其余 {len(peaks) - 80} 条省略")
            self._out.setPlainText("\n".join(lines))

        def _err(msg: str) -> None:
            _RUNNING.discard(thread)
            self._btn.setEnabled(True)
            self._hint.setText(f"失败：{msg}")
            self._out.setPlainText(msg)

        thread.ok.connect(_ok)
        thread.failed.connect(_err)
        thread.finished.connect(thread.deleteLater)
        thread.start()


PAGE_SPEC = PageSpec(
    key="analysis",
    label="谱图分析",
    icon="analysis",
    section="data",
    factory=AnalysisPage,
)
