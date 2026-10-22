"""谱图库：对标 showcase 三步工作流（筛选 → 峰值 → 校正）。"""

from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QRectF,
    QSettings,
    QSize,
    QStandardPaths,
    QThread,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QKeyEvent, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
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

# Qt 加载超高 PNG 会失败；Pillow 先缩到此宽度再显示（可再放大查看）
_LONG_DECODE_MAX_W = 2800

from apps.desktop_client.pages.common import page_layout, primary_button
from apps.desktop_client.pages.registry import PageSpec
from apps.desktop_client.pages.settings import DEFAULT_SERVICE_URLS
from apps.desktop_client.spectrum_plot import SpectrumPlot
from apps.desktop_client.widgets import Panel

_RUNNING: set[QThread] = set()

_STEPS = (
    ("筛选", "质量排序 / Good3 / 曲线"),
    ("峰值", "峰确认与长图"),
    ("校正", "纠正助手 + 标注长图"),
)

_LIST_HEADERS = ("ID", "名称", "金属", "元素集", "状态", "峰数", "分")
_PEAK_HEADERS = ("质量", "分子式", "误差", "强度", "秩")

# 对齐 showcase DEFAULT_MASS_*
_DEFAULT_MASS_POLY = (-1.08608, 0.02743, 0.01722, -1.9947e-7)
_DEFAULT_MASS_FORMULA = (
    "-1.08608+0.02743*I+0.01722*I*I-1.9947*I*I*I*0.0000001"
)
_ALT_MASS_FORMULA = "-1.08608 + 0.02743*A + 0.01722*A^2 - 1.9947e-7*A^3"


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


class _LongImageView(QWidget):
    """可滚动、可放大的长图查看器（Ctrl+滚轮缩放）。"""

    def __init__(self, placeholder: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source = QPixmap()
        self._zoom = 1.0
        self._fit_mode = True  # True=适应宽度

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        tools = QHBoxLayout()
        self._btn_fit = QPushButton("适应宽度")
        self._btn_fit.clicked.connect(self.fit_width)
        self._btn_zoom_out = QPushButton("缩小")
        self._btn_zoom_out.clicked.connect(lambda: self.nudge_zoom(1 / 1.25))
        self._btn_zoom_in = QPushButton("放大")
        self._btn_zoom_in.clicked.connect(lambda: self.nudge_zoom(1.25))
        self._btn_100 = QPushButton("100%")
        self._btn_100.clicked.connect(self.zoom_native)
        self._zoom_label = QLabel("—")
        self._zoom_label.setObjectName("mutedText")
        tools.addWidget(self._btn_fit)
        tools.addWidget(self._btn_zoom_out)
        tools.addWidget(self._btn_zoom_in)
        tools.addWidget(self._btn_100)
        tools.addWidget(self._zoom_label, 1)
        root.addLayout(tools)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(False)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
        )
        self._scroll.viewport().setStyleSheet("background: transparent;")
        self._label = QLabel(placeholder)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setWordWrap(True)
        self._label.setMinimumHeight(120)
        # 淡灰提示字，不要灰底黑字
        self._label.setStyleSheet(
            "QLabel { color: #a8b0b8; background: transparent; font-size: 14px; "
            "padding: 24px; border: none; }"
        )
        self._scroll.setWidget(self._label)
        root.addWidget(self._scroll, 1)

        # loading overlay sits on top of scroll（仅 spinner + 淡提示）
        self._load_box = QWidget(self)
        self._load_box.setStyleSheet("background: transparent;")
        self._load_box.hide()
        lb = QVBoxLayout(self._load_box)
        lb.setContentsMargins(24, 48, 24, 48)
        lb.addStretch(2)
        self._spinner = _LoadingSpinner(self._load_box, size=140)
        lb.addWidget(self._spinner, 0, Qt.AlignmentFlag.AlignHCenter)
        self._load_hint = QLabel("正在出图…")
        self._load_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._load_hint.setStyleSheet(
            "font-size: 15px; font-weight: 500; color: #8a949c; background: transparent;"
        )
        self._load_hint.setWordWrap(True)
        lb.addSpacing(16)
        lb.addWidget(self._load_hint)
        lb.addStretch(3)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._load_box.setGeometry(self._scroll.geometry())
        if self._fit_mode and not self._source.isNull():
            self.fit_width()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.nudge_zoom(1.15)
            elif delta < 0:
                self.nudge_zoom(1 / 1.15)
            event.accept()
            return
        super().wheelEvent(event)

    def set_placeholder(self, text: str) -> None:
        self._source = QPixmap()
        self._label.setPixmap(QPixmap())
        self._label.setText(text)
        self._label.adjustSize()
        # 保证提示区至少铺满视口，但保持透明无灰底
        vw = max(200, self._scroll.viewport().width())
        vh = max(120, self._scroll.viewport().height())
        self._label.resize(max(self._label.width(), vw - 16), max(self._label.height(), vh - 16))
        self._zoom_label.setText("—")

    def set_pixmap(self, pix: QPixmap) -> None:
        self._source = QPixmap(pix)
        self._fit_mode = True
        self.fit_width()

    def show_loading(self, on: bool = True, message: str | None = None) -> None:
        if on:
            # 出图时清掉底下的提示字，避免灰底黑字叠在 loading 下
            self._label.setPixmap(QPixmap())
            self._label.setText("")
            if message:
                self._load_hint.setText(message)
            self._load_box.setGeometry(self._scroll.geometry())
            self._load_box.show()
            self._load_box.raise_()
            self._spinner.start()
        else:
            self._spinner.stop()
            self._load_box.hide()

    def fit_width(self) -> None:
        if self._source.isNull():
            return
        self._fit_mode = True
        vw = max(200, self._scroll.viewport().width() - 8)
        self._zoom = vw / float(self._source.width())
        self._apply_zoom()

    def zoom_native(self) -> None:
        if self._source.isNull():
            return
        self._fit_mode = False
        self._zoom = 1.0
        self._apply_zoom()

    def nudge_zoom(self, factor: float) -> None:
        if self._source.isNull():
            return
        self._fit_mode = False
        self._zoom = max(0.15, min(4.0, self._zoom * factor))
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        if self._source.isNull():
            return
        w = max(1, int(self._source.width() * self._zoom))
        h = max(1, int(self._source.height() * self._zoom))
        scaled = self._source.scaled(
            QSize(w, h),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._label.setPixmap(scaled)
        self._label.setText("")
        self._label.resize(scaled.size())
        self._zoom_label.setText(f"{int(self._zoom * 100)}%")


class LibraryPage(QWidget):
    """Showcase 对齐：三步谱图库（筛选 → 峰值 → 校正）。"""

    spectrumSelected = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._selected_id = 0
        self._selected_name = ""
        self._step = 0
        self._job_id: str | None = None
        self._correct_history: list[dict[str, str]] = []
        self._pending_correct: dict[str, Any] | None = None
        self._meta = QLabel("")  # 详情缓冲（不展示身份页）
        self._detail: dict[str, Any] = {}
        self._curve_raw_x: list[float] = []
        self._curve_raw_y: list[float] = []
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
        self._stack.addWidget(self._build_filter())
        self._stack.addWidget(self._build_peaks())
        self._stack.addWidget(self._build_correct())
        layout.addWidget(self._stack, 1)

        # 底栏
        foot = QHBoxLayout()
        self._btn_back = QPushButton("上一步")
        self._btn_back.clicked.connect(lambda: self._goto_step(max(0, self._step - 1)))
        self._current = QLabel("未选谱", objectName="mutedText")
        self._job_status = QLabel("", objectName="mutedText")
        self._btn_cta = primary_button("执行本步")
        self._btn_cta.clicked.connect(self._run_step_cta)
        foot.addWidget(self._btn_back)
        foot.addWidget(self._current, 1)
        foot.addWidget(self._job_status, 1)
        foot.addWidget(self._btn_cta)
        layout.addLayout(foot)

        self._update_footer()

    # ---- builders ----

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
        self._element_chip.setPlaceholderText("元素 / 关键字，如 Cu、Au、Ag …")
        self._element_chip.setMinimumWidth(280)
        self._element_chip.setMinimumHeight(36)
        self._element_chip.setClearButtonEnabled(True)
        self._element_chip.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._element_chip.returnPressed.connect(self._reload_filter_list)
        bar.addWidget(self._only_good3)
        bar.addWidget(QLabel("搜索"))
        bar.addWidget(self._element_chip, 3)
        bar.addWidget(self._btn_rank)
        bar.addWidget(self._btn_good3)
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
        self._table = self._rank_table  # 兼容旧刷新路径
        self._rank_table.itemSelectionChanged.connect(
            lambda: self._on_table_select(self._rank_table)
        )
        left.body.addWidget(self._rank_table)
        split.addWidget(left)

        right = Panel("曲线", "")
        # 对齐 showcase：X 轴电流/质量可选 + 质量公式可改
        axis_bar = QHBoxLayout()
        self._x_current = QRadioButton("X轴: 电流(A)")
        self._x_mass = QRadioButton("X轴: 质量(m/z)")
        self._x_current.setChecked(True)
        self._x_axis_group = QButtonGroup(self)
        self._x_axis_group.addButton(self._x_current)
        self._x_axis_group.addButton(self._x_mass)
        self._x_current.toggled.connect(self._redraw_curve)
        self._mass_formula = QLineEdit(_DEFAULT_MASS_FORMULA)
        self._mass_formula.setPlaceholderText("Mass = f(I)，I 为电流 A")
        self._mass_formula.setMinimumWidth(320)
        self._btn_apply_mass = QPushButton("应用公式")
        self._btn_apply_mass.clicked.connect(self._redraw_curve)
        self._mass_formula.returnPressed.connect(self._redraw_curve)
        axis_bar.addWidget(self._x_current)
        axis_bar.addWidget(self._x_mass)
        axis_bar.addWidget(QLabel("公式 Mass ="))
        axis_bar.addWidget(self._mass_formula, 1)
        axis_bar.addWidget(self._btn_apply_mass)
        right.body.addLayout(axis_bar)
        self._plot = SpectrumPlot("电流 (A)", "强度")
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
        # 主操作只放底栏「峰值长图」，避免顶/底重复
        self._peaks = self._make_table(_PEAK_HEADERS)
        self._peaks.hide()
        img_panel = Panel("峰值长图（仅认峰，不标分子式）", "")
        self._long_view = _LongImageView("点底栏「峰值长图」加载或自动出图")
        img_panel.body.addWidget(self._long_view)
        root.addWidget(img_panel, 1)
        return w

    def _build_correct(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)
        bar = QHBoxLayout()
        self._btn_tools = QPushButton("校正工具")
        self._btn_tools.clicked.connect(self._cta_correct_tools)
        # 「标注长图」只放底栏；导出用独立勾选（不用 AI 对话）
        bar.addWidget(self._btn_tools)
        bar.addStretch(1)
        root.addLayout(bar)

        export_bar = QHBoxLayout()
        export_bar.addWidget(QLabel("导出"))
        self._ex_png = QCheckBox("标注长图 PNG")
        self._ex_png.setChecked(True)
        self._ex_txt = QCheckBox("元数据 TXT")
        self._ex_txt.setChecked(True)
        self._ex_csv = QCheckBox("分子式 CSV")
        self._ex_csv.setChecked(True)
        self._btn_export = primary_button("导出选中…")
        self._btn_export.setToolTip("校正后导出：长图 / 元数据 / 含分子式的峰表")
        self._btn_export.clicked.connect(self._cta_export_selected)
        export_bar.addWidget(self._ex_png)
        export_bar.addWidget(self._ex_txt)
        export_bar.addWidget(self._ex_csv)
        export_bar.addWidget(self._btn_export)
        export_bar.addStretch(1)
        root.addLayout(export_bar)

        split = QSplitter(Qt.Orientation.Horizontal)
        # 对齐 showcase #correctChat：纠正助手问答
        left = Panel("纠正助手", "描述哪里不对 → AI 提议 → 确认执行")
        self._correct_log = QTextEdit()
        self._correct_log.setReadOnly(True)
        self._correct_log.setPlaceholderText("对话记录会显示在这里…")
        self._correct_msg = QPlainTextEdit()
        self._correct_msg.setPlaceholderText("描述这张图哪里不对…（Enter 发送，Shift+Enter 换行）")
        self._correct_msg.setMaximumHeight(88)
        self._correct_msg.installEventFilter(self)
        ask_row = QHBoxLayout()
        self._btn_propose = primary_button("让 AI 提议")
        self._btn_propose.clicked.connect(self._cta_correct_propose)
        self._btn_correct_run = QPushButton("确认执行")
        self._btn_correct_run.setEnabled(False)
        self._btn_correct_run.clicked.connect(self._cta_correct_run)
        ask_row.addWidget(self._btn_propose)
        ask_row.addWidget(self._btn_correct_run)
        left.body.addWidget(self._correct_log, 1)
        left.body.addWidget(self._correct_msg)
        left.body.addLayout(ask_row)
        split.addWidget(left)

        right = Panel("标注长图（分子式）", "")
        self._correct_view = _LongImageView("点底栏「标注长图」加载或自动出图")
        right.body.addWidget(self._correct_view)
        split.addWidget(right)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        root.addWidget(split, 1)
        return w

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is getattr(self, "_correct_msg", None) and isinstance(event, QKeyEvent):
            if (
                event.type() == event.Type.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            ):
                self._cta_correct_propose()
                return True
        return super().eventFilter(obj, event)

    def _active_long_which(self) -> str:
        return "correct" if self._step == 2 else "long"

    def _long_viewer(self, which: str | None = None) -> _LongImageView:
        which = which or self._active_long_which()
        return self._correct_view if which == "correct" else self._long_view

    def _show_image_loading(
        self,
        *,
        which: str | None = None,
        on: bool = True,
        message: str | None = None,
    ) -> None:
        self._long_viewer(which).show_loading(on=on, message=message)

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
        if self._rank_table.rowCount() == 0:
            self._reload_filter_list()
            self._load_facets()

    def _goto_step(self, index: int) -> None:
        index = max(0, min(len(_STEPS) - 1, int(index)))
        self._step = index
        self._stack.setCurrentIndex(self._step)
        btn = self._step_buttons[self._step]
        if not btn.isChecked():
            btn.setChecked(True)
        self._update_footer()
        if self._step == 0:
            self._reload_filter_list()
            if self._selected_id:
                self._load_curve(self._selected_id)
        elif self._step == 1 and self._selected_id:
            self._load_detail(self._selected_id)
            self._load_long_image(which="long", mode="peaks")
        elif self._step == 2 and self._selected_id:
            self._load_long_image(which="correct", mode="annotate")

    def _update_footer(self) -> None:
        self._btn_back.setEnabled(self._step > 0)
        if self._selected_id:
            self._current.setText(f"当前 #{self._selected_id}  {self._selected_name}")
        else:
            self._current.setText("未选谱")
        cta = {
            0: "质量排序 / Good3",
            1: "峰值长图",
            2: "标注长图",
        }.get(self._step, "执行")
        self._btn_cta.setText(cta)

    def _run_step_cta(self) -> None:
        {
            0: self._reload_filter_list,
            1: self._cta_peaks_long,
            2: self._cta_annotate_long,
        }.get(self._step, lambda: None)()

    @staticmethod
    def _pick_image_item(
        items: list[dict[str, Any]], mode: str
    ) -> dict[str, Any] | None:
        """按 showcase 规则选图：peaks=认峰长图；annotate=标分子式长图。"""
        peaks: list[dict[str, Any]] = []
        annotate: list[dict[str, Any]] = []
        for it in items:
            kind = str(it.get("kind") or "").lower()
            name = str(it.get("name") or "")
            is_peaks = (
                kind in {"peaks_long", "peaks", "peak_long"}
                or name.startswith("peaks_")
                or "_peaks_" in name
            )
            is_formula = (
                not is_peaks
                and (
                    kind in {"long", "annotate_long", "formula_long"}
                    or (name.endswith("_long.png") and name.startswith("spec_"))
                    or (name.endswith("_long.png") and "peaks_" not in name)
                )
            )
            if is_peaks:
                peaks.append(it)
            elif is_formula:
                annotate.append(it)
        if mode == "peaks":
            return peaks[0] if peaks else None
        return annotate[0] if annotate else None

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
        """错误用弹窗；底栏不再堆灰底小字。"""
        if error and text:
            self._job_status.clear()
            self._alert(text)
        elif not text:
            self._job_status.clear()

    def _alert(self, text: str, *, title: str = "提示") -> None:
        msg = str(text or "").strip()
        if not msg:
            return
        # 上游偶发前缀 "error "
        if msg.lower().startswith("error "):
            msg = msg[6:].strip()
        QMessageBox.warning(self, title, msg)

    def _load_facets(self) -> None:
        url = f"{_data_base_url()}/api/v1/showcase/facets"

        def _ok(payload: object) -> None:
            del payload  # 分面仅内部使用，界面不展示技术小字

        def _err(msg: str) -> None:
            del msg

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
        if sid != self._selected_id:
            self._reset_correct_chat()
        self._selected_id = sid
        self._selected_name = name_item.text() if name_item else ""
        self.spectrumSelected.emit(sid)
        self._update_footer()
        self._load_detail(sid)
        if self._step == 0:
            self._load_curve(sid)
        elif self._step == 1:
            self._load_long_image(which="long", mode="peaks")
        elif self._step == 2:
            self._load_long_image(which="correct", mode="annotate")

    def _load_detail(self, spectrum_id: int) -> None:
        url = f"{_data_base_url()}/api/v1/showcase/spectra/{spectrum_id}"

        def _ok(payload: object) -> None:
            if not isinstance(payload, dict):
                return
            self._detail = dict(payload)
            peaks = list(payload.get("peaks") or [])
            saved = str(payload.get("mass_formula") or "").strip()
            if hasattr(self, "_mass_formula"):
                self._mass_formula.setText(saved or _DEFAULT_MASS_FORMULA)
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
            if self._curve_raw_x:
                self._redraw_curve()

        def _err(msg: str) -> None:
            self._meta.setText(f"加载失败：{msg}")

        self._start(url, _ok, _err)

    def _load_curve(self, spectrum_id: int) -> None:
        url = f"{_data_base_url()}/api/v1/showcase/spectra/{spectrum_id}/curve"

        def _ok(payload: object) -> None:
            if not isinstance(payload, dict):
                return
            try:
                self._curve_raw_x = [float(v) for v in (payload.get("x") or [])]
                self._curve_raw_y = [float(v) for v in (payload.get("y") or [])]
            except (TypeError, ValueError) as exc:
                self._set_banner(f"曲线数据异常：{exc}", error=True)
                return
            self._redraw_curve()

        def _err(msg: str) -> None:
            self._set_banner(f"曲线加载失败：{msg}", error=True)

        self._start(url, _ok, _err, timeout=90.0)

    @staticmethod
    def _eval_mass_at(current_a: float, formula: str) -> float:
        src = (formula or _DEFAULT_MASS_FORMULA).strip() or _DEFAULT_MASS_FORMULA
        compact = src.replace(" ", "")
        default_c = _DEFAULT_MASS_FORMULA.replace(" ", "")
        alt_c = _ALT_MASS_FORMULA.replace(" ", "")
        if compact in {default_c, alt_c}:
            c0, c1, c2, c3 = _DEFAULT_MASS_POLY
            a = float(current_a)
            return c0 + c1 * a + c2 * a * a + c3 * a * a * a
        expr = src.replace("^", "**")
        return float(
            eval(  # noqa: S307 — 仅用户本地质量公式，变量锁定为 A/I
                expr,
                {"__builtins__": {}},
                {"A": float(current_a), "I": float(current_a), "math": math},
            )
        )

    def _apply_mass_formula(self, xs: list[float], formula: str) -> list[float]:
        try:
            return [self._eval_mass_at(a, formula) for a in xs]
        except Exception:
            return list(xs)

    def _redraw_curve(self) -> None:
        if not self._curve_raw_x or not self._curve_raw_y:
            return
        if len(self._curve_raw_x) != len(self._curve_raw_y):
            return
        use_mass = bool(getattr(self, "_x_mass", None) and self._x_mass.isChecked())
        formula = (
            self._mass_formula.text().strip()
            if hasattr(self, "_mass_formula")
            else _DEFAULT_MASS_FORMULA
        ) or _DEFAULT_MASS_FORMULA
        xs = (
            self._apply_mass_formula(self._curve_raw_x, formula)
            if use_mass
            else list(self._curve_raw_x)
        )
        try:
            self._plot.set_axis_labels(
                "Mass (m/z)" if use_mass else "电流 (A)", "强度"
            )
            self._plot.set_data(xs, self._curve_raw_y, auto_range=True)
        except Exception as exc:  # noqa: BLE001
            self._set_banner(f"曲线绘制失败：{exc}", error=True)

    def _load_long_image(
        self,
        which: str | None = None,
        *,
        mode: str | None = None,
        auto_generate: bool = True,
    ) -> None:
        if self._selected_id <= 0:
            return
        which = which or self._active_long_which()
        # 峰值步=peaks；校正步=annotate（标分子式）
        if mode is None:
            mode = "annotate" if which == "correct" else "peaks"
        view = self._long_viewer(which)
        loading_msg = (
            "正在加载峰值长图…" if mode == "peaks" else "正在加载标注长图…"
        )
        self._show_image_loading(which=which, on=True, message=loading_msg)
        url = f"{_data_base_url()}/api/v1/showcase/spectra/{self._selected_id}/images"
        sid = self._selected_id

        def _ok(payload: object) -> None:
            if not isinstance(payload, dict):
                self._show_image_loading(which=which, on=False)
                view.set_placeholder("暂无法读取长图目录")
                return
            items = list(payload.get("items") or [])
            long_item = self._pick_image_item(items, mode)
            # peaks_*_long 常只落盘 annotate_data，不一定进 spectrum_images 列表
            if mode == "peaks":
                name = (
                    str(long_item.get("name") or "")
                    if long_item
                    else f"peaks_{sid}_long.png"
                )
                img_url = (
                    f"{_data_base_url()}/api/v1/showcase/spectra/"
                    f"{sid}/images/{urllib.parse.quote(name)}"
                )
                self._fetch_pixmap(
                    img_url,
                    which=which,
                    mode=mode,
                    on_missing=(
                        self._cta_peaks_confirm if auto_generate else None
                    ),
                )
                return
            # annotate：有则加载；无则自动标注出图
            if not long_item:
                if auto_generate:
                    self._show_image_loading(
                        which=which, on=True, message="尚无标注长图，正在自动出图…"
                    )
                    self._cta_annotate()
                else:
                    self._show_image_loading(which=which, on=False)
                    view.set_placeholder("暂无标注长图")
                return
            name = str(long_item.get("name") or "")
            img_url = (
                f"{_data_base_url()}/api/v1/showcase/spectra/"
                f"{sid}/images/{urllib.parse.quote(name)}"
            )
            self._fetch_pixmap(
                img_url,
                which=which,
                mode=mode,
                on_missing=(self._cta_annotate if auto_generate else None),
            )

        def _err(msg: str) -> None:
            # 列目录失败时，峰值步仍尝试直链 peaks_{id}_long.png
            if mode == "peaks":
                name = f"peaks_{sid}_long.png"
                img_url = (
                    f"{_data_base_url()}/api/v1/showcase/spectra/"
                    f"{sid}/images/{urllib.parse.quote(name)}"
                )
                self._fetch_pixmap(
                    img_url,
                    which=which,
                    mode=mode,
                    on_missing=(
                        self._cta_peaks_confirm if auto_generate else None
                    ),
                )
                return
            if mode == "annotate" and auto_generate:
                self._show_image_loading(
                    which=which, on=True, message="尚无标注长图，正在自动出图…"
                )
                self._cta_annotate()
                return
            self._show_image_loading(which=which, on=False)
            view.set_placeholder("暂无法读取长图目录")

        self._start(url, _ok, _err)

    @staticmethod
    def _long_cache_dir() -> Path:
        root = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.CacheLocation
        )
        path = Path(root or ".") / "bcsap_long_images"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def _long_cache_path(cls, url: str) -> Path:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:28]
        return cls._long_cache_dir() / f"{digest}.jpg"

    def _decode_pixmap(
        self, raw: bytes, max_w: int | None = None, *, cache_path: Path | None = None
    ) -> QPixmap | None:
        """Pillow 缩放超高长图后再交给 Qt；成功则写入本地缓存，避免每次重下重解。"""
        import io

        if cache_path is not None and cache_path.is_file() and cache_path.stat().st_size > 1000:
            pix = QPixmap(str(cache_path))
            if not pix.isNull():
                return pix

        max_w = min(int(max_w or _LONG_DECODE_MAX_W), _LONG_DECODE_MAX_W)
        max_w = max(800, max_w)
        max_h = 10000
        try:
            from PIL import Image

            Image.MAX_IMAGE_PIXELS = None  # 长图超限
            with Image.open(io.BytesIO(raw)) as im:
                im = im.convert("RGB")
                target_w = min(max_w, im.width)
                nh = max(1, int(im.height * (target_w / float(im.width))))
                if nh > max_h:
                    scale = max_h / float(nh)
                    target_w = max(400, int(target_w * scale))
                    nh = max_h
                if im.size != (target_w, nh):
                    # 超大图用 BILINEAR，比 LANCZOS 稳且快
                    im = im.resize((target_w, nh), Image.Resampling.BILINEAR)
                out = cache_path or (self._long_cache_dir() / "_tmp_decode.jpg")
                im.save(str(out), "JPEG", quality=88, optimize=True)
                pix = QPixmap(str(out))
                if not pix.isNull():
                    return pix
        except Exception as exc:  # noqa: BLE001
            self._last_decode_error = f"{type(exc).__name__}: {exc}"
            return None
        self._last_decode_error = "Qt 无法加载缩放后的 JPEG"
        return None

    def _fetch_pixmap(
        self,
        url: str,
        *,
        which: str = "long",
        mode: str = "peaks",
        on_missing=None,
    ) -> None:
        view = self._long_viewer(which)
        tip = "峰值长图" if mode == "peaks" else "标注长图"
        cache_path = self._long_cache_path(url)
        # 本地缓存命中：不再从上游拉
        if cache_path.is_file() and cache_path.stat().st_size > 1000:
            pix = QPixmap(str(cache_path))
            if not pix.isNull():
                self._show_image_loading(which=which, on=False)
                view.set_pixmap(pix)
                return

        self._show_image_loading(
            which=which, on=True, message=f"正在下载高清{tip}…"
        )
        thread = _BytesThread(url, self)
        _RUNNING.add(thread)

        def _ok(payload: object) -> None:
            _RUNNING.discard(thread)
            if not isinstance(payload, (bytes, bytearray)):
                self._show_image_loading(which=which, on=False)
                view.set_placeholder("图片数据异常")
                return
            # 上游 404 有时返回 HTML/JSON 小包
            if len(payload) < 1000 and not bytes(payload).startswith(b"\x89PNG"):
                if callable(on_missing):
                    self._show_image_loading(
                        which=which, on=True, message=f"尚无{tip}，正在自动出图…"
                    )
                    on_missing()
                else:
                    self._show_image_loading(which=which, on=False)
                    view.set_placeholder(f"暂无{tip}")
                return
            self._show_image_loading(
                which=which, on=True, message=f"正在解码{tip}…"
            )
            self._last_decode_error = ""
            pix = self._decode_pixmap(
                bytes(payload), _LONG_DECODE_MAX_W, cache_path=cache_path
            )
            self._show_image_loading(which=which, on=False)
            if pix is None or pix.isNull():
                err = getattr(self, "_last_decode_error", "") or "未知原因"
                view.set_placeholder(f"解码失败：{err}")
                return
            view.set_pixmap(pix)

        def _err(msg: str) -> None:
            _RUNNING.discard(thread)
            missing = "404" in msg or "Not Found" in msg or "HTTP 404" in msg
            if missing and callable(on_missing):
                self._show_image_loading(
                    which=which, on=True, message=f"尚无{tip}，正在自动出图…"
                )
                on_missing()
                return
            self._show_image_loading(which=which, on=False)
            view.set_placeholder("下载失败，请稍后重试")

        thread.ok.connect(_ok)
        thread.failed.connect(_err)
        thread.finished.connect(thread.deleteLater)
        thread.start()

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
            "peaks": "正在寻峰出图（仅认峰，不标分子式）…",
            "annotate": "正在标注分子式并出图…",
            "correct": "正在校正…",
        }.get(kind, "处理中…")
        # peaks 任务结果落在峰值步视图；annotate 在校正步
        which = "long" if kind == "peaks" else self._active_long_which()
        if kind == "annotate":
            which = "correct"
        self._show_image_loading(which=which, on=True, message=title)
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
            job_kind = getattr(self, "_job_kind", "annotate")
            which = "long" if job_kind == "peaks" else "correct"
            if job_kind not in {"peaks", "annotate"}:
                which = self._active_long_which()
            mode = "peaks" if job_kind == "peaks" else "annotate"
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
                    if job_kind == "correct":
                        self._append_correct_bubble(
                            progress or "纠正失败", kind="err"
                        )
                    return
                if job_kind == "correct":
                    self._append_correct_bubble(
                        progress or "纠正完成", kind="ok"
                    )
                if self._selected_id:
                    self._load_detail(self._selected_id)
                    # 任务刚完成：只加载，勿再自动触发寻峰（防循环）
                    self._load_long_image(
                        which=which, mode=mode, auto_generate=False
                    )
                    if self._step == 0:
                        self._load_curve(self._selected_id)

        def _err(msg: str) -> None:
            which = self._active_long_which()
            self._show_image_loading(which=which, on=False)
            self._set_banner(f"轮询失败：{msg}", error=True)
            self._poll.stop()

        self._start(url, _ok, _err, timeout=30.0)

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

    def _cta_annotate_long(self) -> None:
        """有标注长图则加载；没有则自动标注出图。"""
        if self._selected_id <= 0:
            self._set_banner("请先选择谱图", error=True)
            return
        self._load_long_image(which="correct", mode="annotate", auto_generate=True)

    def _cta_annotate(self) -> None:
        """上游标注出图任务（仅当本地没有长图时由加载路径触发）。"""
        if self._selected_id <= 0:
            self._set_banner("请先选择谱图", error=True)
            return
        if self._job_id and getattr(self, "_job_kind", "") == "annotate":
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

    def _cta_peaks_long(self) -> None:
        """有 peaks 长图则加载；没有则自动寻峰出图。"""
        if self._selected_id <= 0:
            self._set_banner("请先选择谱图", error=True)
            return
        self._load_long_image(which="long", mode="peaks", auto_generate=True)

    def _cta_peaks_confirm(self) -> None:
        """上游寻峰出图任务（仅当本地没有长图时由加载路径触发）。"""
        if self._selected_id <= 0:
            self._set_banner("请先选择谱图", error=True)
            return
        if self._job_id and getattr(self, "_job_kind", "") == "peaks":
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

    def _reset_correct_chat(self) -> None:
        self._correct_history = []
        self._pending_correct = None
        if hasattr(self, "_correct_log"):
            self._correct_log.clear()
        if hasattr(self, "_btn_correct_run"):
            self._set_correct_confirm(None)

    def _append_correct_bubble(self, text: str, *, kind: str = "ai") -> None:
        """kind: user | ai | ok | err"""
        color = {
            "user": "#1a4a7a",
            "ai": "#222",
            "ok": "#1b6b3a",
            "err": "#a11",
        }.get(kind, "#222")
        bg = {
            "user": "#e8f1fb",
            "ai": "#f4f4f4",
            "ok": "#e8f7ee",
            "err": "#fdecea",
        }.get(kind, "#f4f4f4")
        safe = html.escape(text).replace("\n", "<br/>")
        self._correct_log.append(
            f'<div style="margin:6px 0;padding:8px 10px;border-radius:8px;'
            f'background:{bg};color:{color};">{safe}</div>'
        )
        self._correct_log.verticalScrollBar().setValue(
            self._correct_log.verticalScrollBar().maximum()
        )

    def _set_correct_confirm(self, plan: dict[str, Any] | None) -> None:
        self._pending_correct = plan
        btn = self._btn_correct_run
        if plan and plan.get("tool"):
            params = plan.get("params") or {}
            ts = params.get("tile_start")
            if plan.get("intent") == "crop_draw" and ts not in (None, ""):
                btn.setText(f"确认：从 {ts} u 起出图")
            else:
                btn.setText("确认执行")
            btn.setEnabled(True)
        else:
            btn.setText("确认执行")
            btn.setEnabled(False)

    def _figure_kind(self) -> str:
        # 校正步优先 annotate；否则 peaks
        if self._step == 2:
            return "annotate"
        if self._step == 1:
            return "peaks"
        return "annotate"

    def _cta_correct_tools(self) -> None:
        url = f"{_data_base_url()}/api/v1/showcase/correct/tools"

        def _ok(payload: object) -> None:
            if not isinstance(payload, dict):
                return
            tools = payload.get("tools") or []
            lines = [f"可用工具 {len(tools)} 个："]
            for t in tools[:40]:
                if not isinstance(t, dict):
                    continue
                lines.append(
                    f"- {t.get('name') or t.get('id')}  ({t.get('id')})  "
                    f"{t.get('summary') or ''}"
                )
            self._append_correct_bubble("\n".join(lines), kind="ai")

        self._start(
            url,
            _ok,
            lambda m: self._append_correct_bubble(f"工具清单失败：{m}", kind="err"),
        )

    def _cta_correct_propose(self) -> None:
        """对齐 showcase askCorrect：带 message + history 的问答提议。"""
        if self._selected_id <= 0:
            self._set_banner("请先选择谱图", error=True)
            return
        msg = self._correct_msg.toPlainText().strip()
        if not msg:
            self._set_banner("先写一句这张图哪里不对", error=True)
            return
        self._append_correct_bubble(msg, kind="user")
        self._correct_history.append({"role": "user", "content": msg})
        self._correct_msg.clear()
        self._btn_propose.setEnabled(False)
        url = f"{_data_base_url()}/api/v1/showcase/correct/propose"

        def _ok(payload: object) -> None:
            self._btn_propose.setEnabled(True)
            if not isinstance(payload, dict):
                self._append_correct_bubble("提议返回异常", kind="err")
                return
            reply = str(
                payload.get("reply") or payload.get("reason") or "没有可执行方案"
            )
            bits = [reply]
            if payload.get("tool"):
                bits.append(f"[{payload.get('tool')}]")
            if payload.get("intent"):
                bits.append(f"({payload.get('intent')})")
            params = payload.get("params") or {}
            param_bits = []
            if isinstance(params, dict):
                if params.get("tile_start") not in (None, ""):
                    param_bits.append(f"出图起点 {params['tile_start']} u")
                if params.get("tile_end") not in (None, ""):
                    param_bits.append(f"出图终点 {params['tile_end']} u")
            if param_bits:
                bits.append(" · ".join(param_bits) + " · 不改质量轴")
            self._append_correct_bubble(" ".join(bits), kind="ai")
            self._correct_history.append({"role": "assistant", "content": reply})
            if payload.get("awaiting_confirm") and payload.get("tool"):
                self._set_correct_confirm(
                    {
                        "tool": payload.get("tool"),
                        "params": params if isinstance(params, dict) else {},
                        "reason": reply,
                        "intent": str(payload.get("intent") or ""),
                    }
                )
                self._job_status.setText("已出方案，点「确认执行」才会改图")
            else:
                self._set_correct_confirm(None)

        def _err(m: str) -> None:
            self._btn_propose.setEnabled(True)
            self._append_correct_bubble(f"提议失败：{m}", kind="err")
            self._set_banner(f"提议失败：{m}", error=True)

        self._start(
            url,
            _ok,
            _err,
            method="POST",
            body={
                "spectrum_id": self._selected_id,
                "message": msg,
                "history": self._correct_history[-8:],
                "figure_kind": self._figure_kind(),
            },
            timeout=120.0,
        )

    def _cta_correct_run(self) -> None:
        """对齐 showcase confirmCorrect：POST /correct/run。"""
        plan = self._pending_correct
        if self._selected_id <= 0 or not plan or not plan.get("tool"):
            return
        self._set_correct_confirm(None)
        self._append_correct_bubble(f"正在执行 {plan.get('tool')}…", kind="user")
        url = f"{_data_base_url()}/api/v1/showcase/correct/run"

        def _ok(payload: object) -> None:
            if not isinstance(payload, dict):
                self._append_correct_bubble("执行返回异常", kind="err")
                return
            job = payload.get("job_id")
            if job:
                self._watch_job(str(job), "correct")
            else:
                hint = str(payload.get("hint") or payload.get("message") or "纠正完成")
                ok = payload.get("ok") is not False
                self._append_correct_bubble(hint, kind="ok" if ok else "err")
                self._load_long_image(
                    which="correct", mode="annotate", auto_generate=False
                )

        def _err(m: str) -> None:
            self._append_correct_bubble(f"执行失败：{m}", kind="err")
            self._set_correct_confirm(plan)
            self._set_banner(f"执行失败：{m}", error=True)

        self._start(
            url,
            _ok,
            _err,
            method="POST",
            body={
                "spectrum_id": self._selected_id,
                "tool": plan.get("tool"),
                "params": plan.get("params") or {},
                "async": True,
            },
            timeout=180.0,
        )

    def _cta_export_selected(self) -> None:
        """校正后导出：长图 / 元数据 TXT / 分子式 CSV（独立按钮，不走 AI）。"""
        if self._selected_id <= 0:
            self._alert("请先选择谱图")
            return
        want_png = self._ex_png.isChecked()
        want_txt = self._ex_txt.isChecked()
        want_csv = self._ex_csv.isChecked()
        if not (want_png or want_txt or want_csv):
            self._alert("请至少勾选一种导出内容")
            return
        folder = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if not folder:
            return
        out_dir = Path(folder)
        sid = self._selected_id
        stem = f"spectrum_{sid}"
        written: list[str] = []
        errors: list[str] = []

        detail = dict(self._detail) if self._detail else {}
        if (want_txt or want_csv) and not detail:
            errors.append("尚未加载谱详情，请先进入本步并等待详情完成")

        if want_txt and detail:
            meta_path = out_dir / f"{stem}_meta.txt"
            lines = [
                f"spectrum_id={detail.get('spectrum_id') or sid}",
                f"name={detail.get('name') or ''}",
                f"sample_name={detail.get('sample_name') or ''}",
                f"metal={detail.get('metal') or ''}",
                f"element_set={detail.get('element_set') or ''}",
                f"status={detail.get('status') or ''}",
                f"isotope_class={detail.get('isotope_class') or ''}",
                f"axis_policy={detail.get('axis_policy') or ''}",
                f"calibration_method={detail.get('calibration_method') or ''}",
                f"mass_formula={detail.get('mass_formula') or self._mass_formula.text()}",
                f"power={detail.get('power') or ''}",
                f"ar_flow={detail.get('ar_flow') or ''}",
                f"he_flow={detail.get('he_flow') or ''}",
                f"pressure={detail.get('pressure') or ''}",
                f"condensation_distance={detail.get('condensation_distance') or ''}",
                f"notes={detail.get('notes') or ''}",
                f"peak_count={len(detail.get('peaks') or [])}",
            ]
            meta_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            written.append(meta_path.name)

        if want_csv and detail:
            csv_path = out_dir / f"{stem}_formulas.csv"
            peaks = list(detail.get("peaks") or [])
            with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    ["peak_mass", "formula", "err", "intensity", "rank_in_peak"]
                )
                for p in peaks:
                    if not isinstance(p, dict):
                        continue
                    writer.writerow(
                        [
                            p.get("peak_mass", ""),
                            p.get("formula", ""),
                            p.get("err", ""),
                            p.get("intensity", ""),
                            p.get("rank_in_peak", ""),
                        ]
                    )
            written.append(csv_path.name)
            if not peaks:
                errors.append("分子式 CSV 已写出，但峰表为空（可先点「标注长图」）")

        if want_png:
            try:
                png_name, png_bytes = self._fetch_annotate_long_bytes(sid)
                png_path = out_dir / (png_name or f"{stem}_long.png")
                png_path.write_bytes(png_bytes)
                written.append(png_path.name)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"长图导出失败：{exc}")

        if written and not errors:
            QMessageBox.information(
                self,
                "导出完成",
                f"已写入 {out_dir}：\n" + "\n".join(f"· {n}" for n in written),
            )
        elif written:
            QMessageBox.warning(
                self,
                "导出部分完成",
                f"已写入：\n"
                + "\n".join(f"· {n}" for n in written)
                + "\n\n注意：\n"
                + "\n".join(f"· {e}" for e in errors),
            )
        else:
            self._alert("导出失败：\n" + "\n".join(errors) if errors else "无文件写出")

    def _fetch_annotate_long_bytes(self, sid: int) -> tuple[str, bytes]:
        """同步拉取标注长图（优先目录里的 formula long）。"""
        base = _data_base_url()
        list_url = f"{base}/api/v1/showcase/spectra/{sid}/images"
        req = urllib.request.Request(list_url, method="GET")
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
        items = list(payload.get("items") or []) if isinstance(payload, dict) else []
        item = self._pick_image_item(
            [it for it in items if isinstance(it, dict)], "annotate"
        )
        if not item:
            raise RuntimeError("还没有标注长图，请先点底栏「标注长图」")
        name = str(item.get("name") or f"spec_{sid}_long.png")
        img_url = (
            f"{base}/api/v1/showcase/spectra/{sid}/images/{urllib.parse.quote(name)}"
        )
        req2 = urllib.request.Request(img_url, method="GET")
        with urllib.request.urlopen(req2, timeout=180) as resp:
            raw = resp.read()
        if len(raw) < 1000 or not raw.startswith(b"\x89PNG"):
            raise RuntimeError("标注长图下载无效")
        return name, raw


PAGE_SPEC = PageSpec(
    key="library",
    label="谱图库",
    icon="database",
    section="data",
    factory=LibraryPage,
)
