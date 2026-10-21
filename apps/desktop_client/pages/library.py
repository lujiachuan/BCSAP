"""谱图库：对标 showcase 六步工作流（读写经 data_service 代理上游）。"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from PySide6.QtCore import QByteArray, QRectF, QSettings, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from apps.desktop_client.pages.common import page_layout, primary_button
from apps.desktop_client.pages.registry import PageSpec
from apps.desktop_client.pages.settings import DEFAULT_SERVICE_URLS
from apps.desktop_client.spectrum_plot import SpectrumPlot
from apps.desktop_client.widgets import Panel

_RUNNING: set[QThread] = set()

_STEPS = (
    ("身份", "队列与元素确认"),
    ("筛选", "质量排序 / Good3 / 曲线"),
    ("峰值", "峰确认与长图"),
    ("校正", "轴校正与重标注"),
    ("分析", "成分与摘要"),
    ("交付", "导出交付包"),
)

_LIST_HEADERS = ("ID", "名称", "金属", "元素集", "状态", "峰数", "分")
_PEAK_HEADERS = ("质量", "分子式", "误差", "强度", "秩")
_QUEUE_STATUSES = (
    ("", "全部"),
    ("imported", "待确认/imported"),
    ("needs_elements", "缺元素"),
    ("peaked", "已找峰"),
    ("annotated", "已就绪"),
)


def _data_base_url() -> str:
    settings = QSettings("SpectrumPlatform", "DesktopClient")
    return str(
        settings.value("service/dataUrl", DEFAULT_SERVICE_URLS["data"])
    ).rstrip("/")


class _HttpThread(QThread):
    """后台 JSON GET/POST。"""

    ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        url: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        timeout: float = 60.0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._url = url
        self._method = method.upper()
        self._body = body
        self._timeout = timeout

    def run(self) -> None:
        try:
            data = None
            headers = {"Accept": "application/json"}
            if self._body is not None:
                data = json.dumps(self._body, ensure_ascii=False).encode("utf-8")
                headers["Content-Type"] = "application/json"
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


class _BytesThread(QThread):
    ok = Signal(object)  # bytes
    failed = Signal(str)

    def __init__(self, url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._url = url

    def run(self) -> None:
        try:
            req = urllib.request.Request(self._url, method="GET")
            with urllib.request.urlopen(req, timeout=120) as resp:
                self.ok.emit(resp.read())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class _LoadingSpinner(QWidget):
    """居中大号环形 loading 动效。"""

    def __init__(self, parent: QWidget | None = None, *, size: int = 128) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.hide()

    def start(self) -> None:
        self._angle = 0
        self.show()
        if not self._timer.isActive():
            self._timer.start(30)

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    def _tick(self) -> None:
        self._angle = (self._angle + 8) % 360
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        margin = 14
        rect = QRectF(
            margin, margin, self.width() - 2 * margin, self.height() - 2 * margin
        )
        dim = QPen(QColor(31, 111, 120, 45))
        dim.setWidth(8)
        painter.setPen(dim)
        painter.drawEllipse(rect)
        pen = QPen(QColor("#1f6f78"))
        pen.setWidth(8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, -self._angle * 16, 270 * 16)


class LibraryPage(QWidget):
    """Showcase 对齐：六步谱图库。"""

    spectrumSelected = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._selected_id = 0
        self._selected_name = ""
        self._step = 0
        self._max_unlocked = 0  # 未做过的后续步骤保持灰掉
        self._job_id: str | None = None
        self._poll = QTimer(self)
        self._poll.setInterval(1500)
        self._poll.timeout.connect(self._poll_job)

        layout = page_layout(self)
        layout.addWidget(QLabel("谱图库", objectName="pageTitle"))

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
        layout.addLayout(step_row)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_identity())
        self._stack.addWidget(self._build_filter())
        self._stack.addWidget(self._build_peaks())
        self._stack.addWidget(self._build_correct())
        self._stack.addWidget(self._build_analysis())
        self._stack.addWidget(self._build_export())
        layout.addWidget(self._stack, 1)

        # 底栏
        foot = QHBoxLayout()
        self._btn_back = QPushButton("上一步")
        self._btn_back.clicked.connect(lambda: self._goto_step(max(0, self._step - 1)))
        self._current = QLabel("未选谱", objectName="mutedText")
        self._job_status = QLabel("", objectName="mutedText")
        self._btn_next = primary_button("下一步")
        self._btn_next.clicked.connect(self._go_next_step)
        self._btn_cta = primary_button("执行本步")
        self._btn_cta.clicked.connect(self._run_step_cta)
        foot.addWidget(self._btn_back)
        foot.addWidget(self._current, 1)
        foot.addWidget(self._job_status, 1)
        foot.addWidget(self._btn_cta)
        foot.addWidget(self._btn_next)
        layout.addLayout(foot)

        self._refresh_step_enabled()
        self._update_footer()

    # ---- builders ----

    def _build_identity(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)
        bar = QHBoxLayout()
        self._q = QLineEdit()
        self._q.setPlaceholderText("关键字（名称 / 样品 / 元素集）")
        self._q.returnPressed.connect(self.refresh_list)
        self._status_filter = QComboBox()
        for value, label in _QUEUE_STATUSES:
            self._status_filter.addItem(label, value)
        self._status_filter.currentIndexChanged.connect(self.refresh_list)
        self._btn_refresh = primary_button("刷新目录")
        self._btn_refresh.clicked.connect(self.refresh_list)
        self._btn_infer = QPushButton("推断元素")
        self._btn_infer.clicked.connect(self._cta_infer)
        self._btn_suspect = QPushButton("扫描存疑")
        self._btn_suspect.clicked.connect(self._cta_suspect)
        bar.addWidget(self._q, 2)
        bar.addWidget(self._status_filter, 1)
        bar.addWidget(self._btn_refresh)
        bar.addWidget(self._btn_infer)
        bar.addWidget(self._btn_suspect)
        root.addLayout(bar)

        self._facet = QLabel("")
        self._facet.setWordWrap(True)
        self._facet.hide()
        root.addWidget(self._facet)

        split = QSplitter(Qt.Orientation.Horizontal)
        list_panel = Panel("谱图目录", "")
        self._table = self._make_table(_LIST_HEADERS)
        self._table.itemSelectionChanged.connect(self._on_row_selected)
        list_panel.body.addWidget(self._table)
        split.addWidget(list_panel)

        meta_panel = Panel("当前谱 · 身份", "")
        self._meta = QLabel("选择一条谱图。")
        self._meta.setWordWrap(True)
        self._meta.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        meta_panel.body.addWidget(self._meta)
        split.addWidget(meta_panel)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)
        return w

    def _build_filter(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)
        bar = QHBoxLayout()
        self._only_good3 = QCheckBox("只看 Good3")
        self._only_good3.stateChanged.connect(self._reload_filter_list)
        self._btn_good3 = QPushButton("刷新 Good3")
        self._btn_good3.clicked.connect(self._cta_good3_refresh)
        self._btn_rank = QPushButton("质量排序")
        self._btn_rank.clicked.connect(self._reload_filter_list)
        self._element_chip = QLineEdit()
        self._element_chip.setPlaceholderText("元素芯片，如 Au / Ag")
        self._element_chip.setFixedWidth(100)
        bar.addWidget(self._only_good3)
        bar.addWidget(QLabel("元素"))
        bar.addWidget(self._element_chip)
        bar.addWidget(self._btn_rank)
        bar.addWidget(self._btn_good3)
        bar.addStretch(1)
        root.addLayout(bar)

        weights = QHBoxLayout()
        self._w_m = self._weight_slider("质量", 22)
        self._w_a = self._weight_slider("丰度", 18)
        self._w_r = self._weight_slider("分辨", 18)
        self._w_n = self._weight_slider("干净", 12)
        for box in (self._w_m, self._w_a, self._w_r, self._w_n):
            weights.addLayout(box)
        root.addLayout(weights)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = Panel("排序列表", "")
        self._rank_table = self._make_table(_LIST_HEADERS)
        self._rank_table.itemSelectionChanged.connect(
            lambda: self._on_table_select(self._rank_table)
        )
        left.body.addWidget(self._rank_table)
        split.addWidget(left)

        right = Panel("曲线", "")
        self._plot = SpectrumPlot("m/z", "I")
        right.body.addWidget(self._plot)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        root.addWidget(split, 1)
        return w

    def _weight_slider(self, title: str, default: int) -> QVBoxLayout:
        box = QVBoxLayout()
        lab = QLabel(f"{title} {default}")
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 40)
        slider.setValue(default)
        slider.valueChanged.connect(lambda v, L=lab, T=title: L.setText(f"{T} {v}"))
        box.addWidget(lab)
        box.addWidget(slider)
        # stash slider on self for later
        setattr(self, f"_slider_{title}", slider)
        return box

    def _build_peaks(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)
        bar = QHBoxLayout()
        self._btn_peak_confirm = primary_button("峰确认（上游）")
        self._btn_peak_confirm.clicked.connect(self._cta_peaks_confirm)
        self._btn_load_long = QPushButton("加载长图")
        self._btn_load_long.clicked.connect(self._load_long_image)
        bar.addWidget(self._btn_peak_confirm)
        bar.addWidget(self._btn_load_long)
        bar.addStretch(1)
        root.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal)
        peak_panel = Panel("Working 峰", "")
        self._peaks = self._make_table(_PEAK_HEADERS)
        peak_panel.body.addWidget(self._peaks)
        split.addWidget(peak_panel)

        img_panel = Panel("长图", "")
        self._long_scroll = QScrollArea()
        self._long_scroll.setWidgetResizable(True)
        (
            self._long_host,
            self._long_label,
            self._long_load_box,
            self._long_spinner,
            self._long_load_hint,
        ) = self._make_image_host("选择谱图后加载长图")
        self._long_scroll.setWidget(self._long_host)
        img_panel.body.addWidget(self._long_scroll)
        split.addWidget(img_panel)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        root.addWidget(split, 1)
        return w

    def _build_correct(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)
        bar = QHBoxLayout()
        self._btn_tools = QPushButton("校正工具")
        self._btn_tools.clicked.connect(self._cta_correct_tools)
        self._btn_propose = QPushButton("AI 提议")
        self._btn_propose.clicked.connect(self._cta_correct_propose)
        self._btn_annotate = primary_button("重跑标注+长图")
        self._btn_annotate.clicked.connect(self._cta_annotate)
        bar.addWidget(self._btn_tools)
        bar.addWidget(self._btn_propose)
        bar.addWidget(self._btn_annotate)
        bar.addStretch(1)
        root.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = Panel("校正说明 / 工具", "")
        self._correct_log = QTextEdit()
        self._correct_log.setReadOnly(True)
        left.body.addWidget(self._correct_log)
        split.addWidget(left)

        right = Panel("长图预览", "")
        self._correct_scroll = QScrollArea()
        self._correct_scroll.setWidgetResizable(True)
        (
            self._correct_host,
            self._correct_img,
            self._correct_load_box,
            self._correct_spinner,
            self._correct_load_hint,
        ) = self._make_image_host("校正后长图")
        self._correct_scroll.setWidget(self._correct_host)
        right.body.addWidget(self._correct_scroll)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        root.addWidget(split, 1)
        return w

    def _build_analysis(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)
        panel = Panel("分析摘要", "")
        self._analysis = QTextEdit()
        self._analysis.setReadOnly(True)
        panel.body.addWidget(self._analysis)
        root.addWidget(panel, 1)
        return w

    def _build_export(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)
        panel = Panel("交付", "")
        form = QFormLayout()
        self._btn_export = primary_button("请求交付包")
        self._btn_export.clicked.connect(self._cta_export)
        self._export_log = QTextEdit()
        self._export_log.setReadOnly(True)
        form.addRow(self._btn_export)
        panel.body.addLayout(form)
        panel.body.addWidget(self._export_log)
        root.addWidget(panel, 1)
        return w

    def _make_image_host(
        self, placeholder: str
    ) -> tuple[QWidget, QLabel, QWidget, _LoadingSpinner, QLabel]:
        host = QWidget()
        host.setMinimumHeight(420)
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        load_box = QWidget()
        load_box.setMinimumHeight(420)
        load_box.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        lb = QVBoxLayout(load_box)
        lb.setContentsMargins(24, 48, 24, 48)
        lb.addStretch(2)
        spinner = _LoadingSpinner(load_box, size=140)
        lb.addWidget(spinner, 0, Qt.AlignmentFlag.AlignHCenter)
        hint = QLabel("正在出图…")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("font-size: 18px; font-weight: 600; color: #1f6f78;")
        hint.setWordWrap(True)
        lb.addSpacing(16)
        lb.addWidget(hint)
        lb.addStretch(3)
        load_box.hide()

        label = QLabel(placeholder)
        label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter
        )
        label.setMinimumHeight(200)

        lay.addWidget(load_box, 1)
        lay.addWidget(label, 1)
        return host, label, load_box, spinner, hint

    def _active_long_which(self) -> str:
        return "correct" if self._step == 3 else "long"

    def _show_image_loading(
        self,
        *,
        which: str | None = None,
        on: bool = True,
        message: str | None = None,
    ) -> None:
        which = which or self._active_long_which()
        if which == "correct":
            box, spinner, label, hint = (
                self._correct_load_box,
                self._correct_spinner,
                self._correct_img,
                self._correct_load_hint,
            )
        else:
            box, spinner, label, hint = (
                self._long_load_box,
                self._long_spinner,
                self._long_label,
                self._long_load_hint,
            )
        if on:
            label.hide()
            label.clear()
            label.setPixmap(QPixmap())
            if message:
                hint.setText(message)
            elif not hint.text():
                hint.setText("正在出图…")
            box.show()
            box.raise_()
            spinner.start()
        else:
            spinner.stop()
            box.hide()
            label.show()

    def _make_table(self, headers: tuple[str, ...]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(list(headers))
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        return table

    # ---- lifecycle ----

    def closeEvent(self, event) -> None:  # noqa: N802
        self._poll.stop()
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
            self.refresh_list()
            self._load_facets()

    def _refresh_step_enabled(self) -> None:
        for i, btn in enumerate(self._step_buttons):
            btn.setEnabled(i <= self._max_unlocked)

    def _go_next_step(self) -> None:
        nxt = min(len(_STEPS) - 1, self._step + 1)
        if nxt > self._max_unlocked:
            self._max_unlocked = nxt
            self._refresh_step_enabled()
        self._goto_step(nxt)

    def _goto_step(self, index: int) -> None:
        index = max(0, min(len(_STEPS) - 1, int(index)))
        if index > self._max_unlocked:
            # 未解锁步骤保持灰掉，忽略点击
            btn = self._step_buttons[self._step]
            if not btn.isChecked():
                btn.setChecked(True)
            return
        self._step = index
        self._stack.setCurrentIndex(self._step)
        btn = self._step_buttons[self._step]
        if not btn.isChecked():
            btn.setChecked(True)
        self._update_footer()
        if self._step == 1:
            self._reload_filter_list()
            if self._selected_id:
                self._load_curve(self._selected_id)
        elif self._step == 2 and self._selected_id:
            self._load_detail(self._selected_id)
            self._load_long_image()
        elif self._step == 3 and self._selected_id:
            self._load_long_image(target=self._correct_img)
        elif self._step == 4 and self._selected_id:
            self._fill_analysis()

    def _update_footer(self) -> None:
        self._btn_back.setEnabled(self._step > 0)
        self._btn_next.setEnabled(self._step < len(_STEPS) - 1)
        if self._selected_id:
            self._current.setText(f"当前 #{self._selected_id}  {self._selected_name}")
        else:
            self._current.setText("未选谱")
        cta = {
            0: "刷新目录",
            1: "质量排序 / Good3",
            2: "峰确认",
            3: "重跑标注",
            4: "刷新分析",
            5: "请求交付包",
        }.get(self._step, "执行")
        self._btn_cta.setText(cta)

    def _run_step_cta(self) -> None:
        {
            0: self.refresh_list,
            1: self._reload_filter_list,
            2: self._cta_peaks_confirm,
            3: self._cta_annotate,
            4: self._fill_analysis,
            5: self._cta_export,
        }.get(self._step, lambda: None)()

    # ---- http helpers ----

    def _start(
        self,
        url: str,
        on_ok,
        on_err,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> None:
        thread = _HttpThread(
            url, method=method, body=body, timeout=timeout, parent=self
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

    def _set_banner(self, text: str, *, error: bool = False) -> None:
        """成功详情不再顶栏刷小字；仅错误时在底栏提示。"""
        if error and text:
            self._job_status.setText(text)
        elif not text:
            self._job_status.clear()

    def _load_facets(self) -> None:
        url = f"{_data_base_url()}/api/v1/showcase/facets"

        def _ok(payload: object) -> None:
            del payload  # 分面仅内部使用，界面不展示技术小字

        def _err(msg: str) -> None:
            del msg

        self._start(url, _ok, _err)

    def refresh_list(self) -> None:
        params: dict[str, str] = {"limit": "100"}
        q = self._q.text().strip()
        st = self._status_filter.currentData()
        if q:
            params["q"] = q
        if st:
            params["status"] = str(st)
        url = f"{_data_base_url()}/api/v1/showcase/spectra?{urllib.parse.urlencode(params)}"
        self._btn_refresh.setEnabled(False)

        def _ok(payload: object) -> None:
            self._btn_refresh.setEnabled(True)
            if not isinstance(payload, dict):
                return
            self._fill_table(self._table, list(payload.get("items") or []))

        def _err(msg: str) -> None:
            self._btn_refresh.setEnabled(True)
            self._set_banner(f"检索失败：{msg}", error=True)

        self._start(url, _ok, _err)

    def _reload_filter_list(self) -> None:
        base = _data_base_url()
        el = self._element_chip.text().strip()
        if self._only_good3.isChecked():
            params = {}
            if el:
                params["element"] = el
            qs = urllib.parse.urlencode(params)
            url = f"{base}/api/v1/showcase/spectra/good3" + (f"?{qs}" if qs else "")
        else:
            params = {"limit": "80"}
            if el:
                params["element"] = el
            url = f"{base}/api/v1/showcase/spectra/rank?{urllib.parse.urlencode(params)}"

        def _ok(payload: object) -> None:
            if not isinstance(payload, dict):
                return
            items = list(payload.get("items") or [])
            self._fill_table(self._rank_table, items)
            if self._selected_id:
                self._load_curve(self._selected_id)

        def _err(msg: str) -> None:
            self._set_banner(f"排序/Good3 失败：{msg}", error=True)

        self._start(url, _ok, _err)

    def _fill_table(self, table: QTableWidget, items: list[dict[str, Any]]) -> None:
        table.setRowCount(0)
        for row in items:
            r = table.rowCount()
            table.insertRow(r)
            score = row.get("score")
            values = [
                str(row.get("spectrum_id") or ""),
                str(row.get("name") or ""),
                str(row.get("metal") or row.get("chip") or ""),
                str(row.get("element_set") or ""),
                str(row.get("status") or ""),
                str(row.get("peak_count") or 0),
                "" if score is None else f"{float(score):.3f}",
            ]
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                if c == 0:
                    item.setData(
                        Qt.ItemDataRole.UserRole, int(row.get("spectrum_id") or 0)
                    )
                table.setItem(r, c, item)

    def _on_row_selected(self) -> None:
        self._on_table_select(self._table)

    def _on_table_select(self, table: QTableWidget) -> None:
        rows = table.selectionModel().selectedRows()
        if not rows:
            return
        item = table.item(rows[0].row(), 0)
        name_item = table.item(rows[0].row(), 1)
        if item is None:
            return
        sid = int(item.data(Qt.ItemDataRole.UserRole) or 0)
        if sid <= 0:
            return
        self._selected_id = sid
        self._selected_name = name_item.text() if name_item else ""
        self.spectrumSelected.emit(sid)
        self._update_footer()
        self._load_detail(sid)
        if self._step == 1:
            self._load_curve(sid)
        if self._step in (2, 3):
            self._load_long_image(
                target=self._long_label if self._step == 2 else self._correct_img
            )

    def _load_detail(self, spectrum_id: int) -> None:
        url = f"{_data_base_url()}/api/v1/showcase/spectra/{spectrum_id}"

        def _ok(payload: object) -> None:
            if not isinstance(payload, dict):
                return
            peaks = list(payload.get("peaks") or [])
            self._meta.setText(
                f"#{payload.get('spectrum_id')}  {payload.get('name')}\n"
                f"样品={payload.get('sample_name') or '—'}  金属={payload.get('metal') or '—'}\n"
                f"元素集={payload.get('element_set') or '—'}\n"
                f"状态={payload.get('status') or '—'}  同位素={payload.get('isotope_class')}\n"
                f"功率={payload.get('power') or '—'}  Ar={payload.get('ar_flow') or '—'}  "
                f"He={payload.get('he_flow') or '—'}  P={payload.get('pressure') or '—'}  "
                f"距={payload.get('condensation_distance') or '—'}\n"
                f"轴策略={payload.get('axis_policy')}  校正={payload.get('calibration_method') or '—'}\n"
                f"提示={payload.get('axis_hint') or '—'}\n"
                f"notes={(payload.get('notes') or '—')[:240]}\n"
                f"标注峰 {len(peaks)} 条"
            )
            self._peaks.setRowCount(0)
            for p in peaks[:200]:
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
            self._selected_name = str(payload.get("name") or self._selected_name)
            self._update_footer()
            if self._step == 4:
                self._fill_analysis(payload)

        def _err(msg: str) -> None:
            self._meta.setText(f"加载失败：{msg}")

        self._start(url, _ok, _err)

    def _load_curve(self, spectrum_id: int) -> None:
        url = f"{_data_base_url()}/api/v1/showcase/spectra/{spectrum_id}/curve"

        def _ok(payload: object) -> None:
            if not isinstance(payload, dict):
                return
            x = list(payload.get("x") or [])
            y = list(payload.get("y") or [])
            try:
                self._plot.set_data(x, y, auto_range=True)
            except Exception as exc:  # noqa: BLE001
                self._set_banner(f"曲线绘制失败：{exc}", error=True)

        def _err(msg: str) -> None:
            self._set_banner(f"曲线加载失败：{msg}", error=True)

        self._start(url, _ok, _err, timeout=90.0)

    def _load_long_image(self, target: QLabel | None = None) -> None:
        if self._selected_id <= 0:
            return
        use_correct = target is self._correct_img or (
            target is None and self._step == 3
        )
        which = "correct" if use_correct else "long"
        label = self._correct_img if use_correct else self._long_label
        self._show_image_loading(which=which, on=True)
        url = f"{_data_base_url()}/api/v1/showcase/spectra/{self._selected_id}/images"

        def _ok(payload: object) -> None:
            if not isinstance(payload, dict):
                self._show_image_loading(which=which, on=False)
                label.setText("长图目录异常")
                return
            items = list(payload.get("items") or [])
            long_item = None
            for it in items:
                kind = str(it.get("kind") or "")
                name = str(it.get("name") or "")
                if kind == "long" or name.endswith("_long.png"):
                    long_item = it
                    break
            if not long_item:
                self._show_image_loading(which=which, on=False)
                label.setText("暂无长图（可点「重跑标注+长图」生成）")
                return
            name = str(long_item.get("name") or "")
            img_url = (
                f"{_data_base_url()}/api/v1/showcase/spectra/"
                f"{self._selected_id}/images/{urllib.parse.quote(name)}"
            )
            self._fetch_pixmap(img_url, label, which=which)

        def _err(msg: str) -> None:
            self._show_image_loading(which=which, on=False)
            label.setText(f"图片目录失败：{msg}")

        self._start(url, _ok, _err)

    def _decode_pixmap(self, raw: bytes, max_w: int) -> QPixmap | None:
        """解码长图。

        Showcase 长图常达数千×两万像素，Qt 的 PNG 插件会直接失败/崩溃；
        先用 Pillow 缩到显示宽度，再交给 QPixmap。
        """
        import os
        import tempfile

        max_w = max(200, int(max_w or 900))
        # 1) Pillow 路径（超高长图）
        try:
            from PIL import Image
            import io

            with Image.open(io.BytesIO(raw)) as im:
                im = im.convert("RGB")
                if im.width > max_w:
                    nh = max(1, int(im.height * (max_w / float(im.width))))
                    im = im.resize((max_w, nh), Image.Resampling.BILINEAR)
                fd, path = tempfile.mkstemp(prefix="bcsap_long_", suffix=".png")
                os.close(fd)
                try:
                    im.save(path, "PNG", optimize=False)
                    pix = QPixmap(path)
                    if not pix.isNull():
                        return pix
                finally:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        except Exception:
            pass

        # 2) 落盘 + QImageReader 缩放（无 Pillow 时的回退）
        fd, path = tempfile.mkstemp(prefix="bcsap_long_raw_", suffix=".png")
        os.close(fd)
        try:
            with open(path, "wb") as f:
                f.write(raw)
            reader = QImageReader(path)
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid() and size.width() > max_w:
                nh = max(1, int(size.height() * (max_w / float(size.width()))))
                from PySide6.QtCore import QSize

                reader.setScaledSize(QSize(max_w, nh))
            image = reader.read()
            if not image.isNull():
                return QPixmap.fromImage(image)
            pix = QPixmap(path)
            if not pix.isNull():
                if pix.width() > max_w:
                    pix = pix.scaledToWidth(
                        max_w, Qt.TransformationMode.SmoothTransformation
                    )
                return pix
        except Exception:
            pass
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

        # 3) 最后尝试内存加载（仅小图）
        pix = QPixmap()
        if pix.loadFromData(QByteArray(raw), "PNG") and not pix.isNull():
            if pix.width() > max_w:
                pix = pix.scaledToWidth(
                    max_w, Qt.TransformationMode.SmoothTransformation
                )
            return pix
        return None

    def _fetch_pixmap(
        self, url: str, label: QLabel, *, which: str = "long"
    ) -> None:
        self._show_image_loading(
            which=which, on=True, message="正在下载长图…"
        )
        thread = _BytesThread(url, self)
        _RUNNING.add(thread)

        def _ok(payload: object) -> None:
            _RUNNING.discard(thread)
            if not isinstance(payload, (bytes, bytearray)):
                self._show_image_loading(which=which, on=False)
                label.setText("图片数据异常")
                return
            max_w = 720
            host = self._correct_host if which == "correct" else self._long_host
            if host.width() > 200:
                max_w = max(400, host.width() - 24)
            pix = self._decode_pixmap(bytes(payload), max_w)
            self._show_image_loading(which=which, on=False)
            if pix is None or pix.isNull():
                label.setText("PNG 解码失败")
                return
            label.setPixmap(pix)
            label.setText("")
            label.setMinimumHeight(pix.height())

        def _err(msg: str) -> None:
            _RUNNING.discard(thread)
            self._show_image_loading(which=which, on=False)
            label.setText(f"下载失败：{msg}")

        thread.ok.connect(_ok)
        thread.failed.connect(_err)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _fill_analysis(self, payload: dict[str, Any] | None = None) -> None:
        if payload is None:
            if self._selected_id:
                self._load_detail(self._selected_id)
            return
        peaks = list(payload.get("peaks") or [])
        lines = [
            f"#{payload.get('spectrum_id')} {payload.get('name')}",
            f"metal={payload.get('metal')} class={payload.get('isotope_class')}",
            f"policy={payload.get('axis_policy')} method={payload.get('calibration_method')}",
            f"peaks={len(peaks)}",
            "",
            "前 30 峰：",
        ]
        for p in peaks[:30]:
            lines.append(
                f"  {p.get('peak_mass')}\t{p.get('formula') or ''}\t{p.get('err')}"
            )
        self._analysis.setPlainText("\n".join(lines))

    # ---- write CTAs ----

    def _job_progress_text(self, payload: dict[str, Any]) -> str:
        status = str(payload.get("status") or payload.get("state") or "")
        msg = str(
            payload.get("message")
            or payload.get("detail")
            or payload.get("phase")
            or ""
        ).strip()
        pct = payload.get("percent")
        if pct is None:
            pct = payload.get("progress")
        parts = [p for p in (status, msg) if p]
        text = "  ".join(parts) if parts else "正在出图…"
        if pct is not None and str(pct) not in text:
            text = f"{text}  ({pct}%)"
        return text

    def _watch_job(self, job_id: str, kind: str = "annotate") -> None:
        self._job_id = job_id
        self._job_kind = kind
        # 出图/峰确认：进度显示在长图区正中大 loading，底栏不再刷小字
        title = {
            "peaks": "正在峰确认并出图…",
            "annotate": "正在标注并出图…",
            "correct": "正在校正…",
        }.get(kind, "处理中…")
        self._show_image_loading(
            which=self._active_long_which(), on=True, message=title
        )
        self._job_status.clear()
        self._poll.start()

    def _poll_job(self) -> None:
        if not self._job_id:
            self._poll.stop()
            return
        kind = getattr(self, "_job_kind", "annotate")
        if kind == "peaks":
            url = f"{_data_base_url()}/api/v1/showcase/peaks/jobs/{self._job_id}"
        elif kind == "correct":
            url = f"{_data_base_url()}/api/v1/showcase/correct/jobs/{self._job_id}"
        else:
            url = f"{_data_base_url()}/api/v1/showcase/annotate/jobs/{self._job_id}"

        def _ok(payload: object) -> None:
            if not isinstance(payload, dict):
                return
            status = str(payload.get("status") or payload.get("state") or "")
            progress = self._job_progress_text(payload)
            which = self._active_long_which()
            self._show_image_loading(which=which, on=True, message=progress)
            done = status.lower() in {
                "done",
                "ok",
                "finished",
                "success",
                "completed",
                "error",
                "failed",
            }
            if payload.get("ok") is True and payload.get("done") is True:
                done = True
            # 上游有时用 running + percent=100
            if str(payload.get("percent") or "") in {"100", "100.0"} and status.lower() in {
                "done",
                "ok",
                "finished",
                "success",
                "completed",
                "",
            }:
                done = True
            failed = status.lower() in {"error", "failed"}
            if done:
                self._poll.stop()
                self._job_id = None
                if failed:
                    self._show_image_loading(which=which, on=False)
                    self._set_banner(progress or "任务失败", error=True)
                    return
                if self._selected_id:
                    self._load_detail(self._selected_id)
                    self._load_long_image(
                        target=self._correct_img if which == "correct" else self._long_label
                    )
                    if self._step == 1:
                        self._load_curve(self._selected_id)

        def _err(msg: str) -> None:
            which = self._active_long_which()
            self._show_image_loading(which=which, on=False)
            self._set_banner(f"轮询失败：{msg}", error=True)
            self._poll.stop()

        self._start(url, _ok, _err, timeout=30.0)

    def _cta_infer(self) -> None:
        url = f"{_data_base_url()}/api/v1/showcase/spectra/infer-elements"
        self._start(
            url,
            lambda _p: self._job_status.setText("元素推断已提交"),
            lambda m: self._set_banner(m, error=True),
            method="POST",
            body={},
        )

    def _cta_suspect(self) -> None:
        url = f"{_data_base_url()}/api/v1/showcase/spectra/scan-element-suspects"
        self._start(
            url,
            lambda _p: self._job_status.setText("存疑扫描已提交"),
            lambda m: self._set_banner(m, error=True),
            method="POST",
            body={},
        )

    def _cta_good3_refresh(self) -> None:
        url = f"{_data_base_url()}/api/v1/showcase/spectra/good3/refresh"
        self._start(
            url,
            lambda _p: (
                self._job_status.setText("Good3 已刷新"),
                self._reload_filter_list(),
            ),
            lambda m: self._set_banner(m, error=True),
            method="POST",
            body={},
            timeout=120.0,
        )

    def _cta_annotate(self) -> None:
        if self._selected_id <= 0:
            self._set_banner("请先选择谱图", error=True)
            return
        url = f"{_data_base_url()}/api/v1/showcase/spectra/{self._selected_id}/annotate"

        def _ok(payload: object) -> None:
            if not isinstance(payload, dict):
                return
            job = payload.get("job_id")
            if job:
                self._watch_job(str(job), "annotate")
            else:
                self._job_status.setText("标注已提交")

        self._start(
            url,
            _ok,
            lambda m: self._set_banner(m, error=True),
            method="POST",
            body={"async": True, "draw": True, "max_panels": 80},
            timeout=120.0,
        )

    def _cta_peaks_confirm(self) -> None:
        if self._selected_id <= 0:
            self._set_banner("请先选择谱图", error=True)
            return
        url = f"{_data_base_url()}/api/v1/showcase/spectra/{self._selected_id}/peaks/confirm"

        def _ok(payload: object) -> None:
            if not isinstance(payload, dict):
                return
            job = payload.get("job_id")
            if job:
                self._watch_job(str(job), "peaks")
            else:
                self._job_status.setText("峰确认已提交")

        self._start(
            url,
            _ok,
            lambda m: self._set_banner(m, error=True),
            method="POST",
            body={"async": True},
            timeout=120.0,
        )

    def _cta_correct_tools(self) -> None:
        url = f"{_data_base_url()}/api/v1/showcase/correct/tools"

        def _ok(payload: object) -> None:
            self._correct_log.setPlainText(
                json.dumps(payload, ensure_ascii=False, indent=2)[:8000]
            )

        self._start(url, _ok, lambda m: self._correct_log.setPlainText(m))

    def _cta_correct_propose(self) -> None:
        if self._selected_id <= 0:
            self._set_banner("请先选择谱图", error=True)
            return
        url = f"{_data_base_url()}/api/v1/showcase/correct/propose"

        def _ok(payload: object) -> None:
            self._correct_log.setPlainText(
                json.dumps(payload, ensure_ascii=False, indent=2)[:8000]
            )
            if isinstance(payload, dict) and payload.get("job_id"):
                self._watch_job(str(payload["job_id"]), "correct")

        self._start(
            url,
            _ok,
            lambda m: self._correct_log.setPlainText(m),
            method="POST",
            body={"spectrum_id": self._selected_id},
            timeout=120.0,
        )

    def _cta_export(self) -> None:
        # 打开打包接口（可能返回 zip）；先探测
        params = {}
        if self._selected_id:
            params["spectrum_id"] = str(self._selected_id)
        qs = urllib.parse.urlencode(params)
        url = f"{_data_base_url()}/api/v1/showcase/export/pack" + (f"?{qs}" if qs else "")
        self._export_log.setPlainText(f"请求 {url} …")

        def _ok(payload: object) -> None:
            # JSON 错误体时
            self._export_log.setPlainText(
                "交付接口返回 JSON（可能是错误或清单）：\n"
                + json.dumps(payload, ensure_ascii=False, indent=2)[:4000]
            )

        def _err(msg: str) -> None:
            # zip 会被 JSON 解析失败——提示用户用浏览器下载
            self._export_log.setPlainText(
                f"{msg}\n\n若为二进制 zip，请在浏览器打开：\n{url}"
            )

        self._start(url, _ok, _err, timeout=120.0)


PAGE_SPEC = PageSpec(
    key="library",
    label="谱图库",
    icon="database",
    section="data",
    factory=LibraryPage,
)
