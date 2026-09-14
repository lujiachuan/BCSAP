"""手动控制测试页：**单页三列排布**，一行一个设备。

对齐 demo（``demo/argon_tuning``）「① 手动控制」页的版式——那个页面就是**一页三列**，
不是按设备组分页签::

    ┌──────────────────────────────────────────────────────────────────┐
    │ 手动控制                                         [×1][×10] 步进   │  ← 顶栏（跨三列）
    │ FC1 电流: 12.5 nA      FC2 电流: 8.3 nA       最近下发: …        │
    ├──────────────────┬────────────────────┬──────────────────────────┤
    │ 气体流量          │ 高压阵列（DW 13 路）│ 新高压电源（BD 5 路）     │
    │ 溅射电源          │                    │ 磁铁电源（4 路）          │
    │ 腔体真空          │ DW1 skim …         │ 束流探测                 │
    │ 聚焦 / 漂移管      │ DW13 圆柱 …        │                          │
    └──────────────────┴────────────────────┴──────────────────────────┘

左 / 中 / 右三列按**信号的 PV 命名空间**分列（不认分组显示名——分组名来自映射、
可以被改；PV 前缀来自 IOC，稳定）：``hv_array`` 独占中列，``hv_bd`` / ``magnet`` /
``detector`` 去右列，其余走左列。分组名对不上任何命名空间时，行数最多的一组
自动落到中列，保证「内容最多的那组独占一列」。

一行一个设备
------------
设备按信号前缀归并（``hv_array.dw04.voltage_setpoint`` 属于 ``hv_array.dw04``），
设备名取该设备各信号标签的最长公共前缀，因此「DW4 通道4 电压设定 / 输出使能 /
电压回读 / 电流回读」压成一行::

    设备         电压设定          回读               输出
    DW4 通道4    [2000.0] [下发]   2000 V · 2.5 mA    [开][关]

* **设定**：可写的 ``setpoint`` 信号。设定框支持**滚轮调值**（``_WheelSpinBox``），
  点「下发」才动作。**改值不自动下发**——高压装置上「转盘碰一下就下发」是真实
  事故来源，滚轮误滚只改了一个待确认的数字。设备有 2 个设定量时（磁铁电流+速率、
  主高压电压+电流）在同一行的设定格里上下叠两槽。
* **回读**：该设备全部只读量平铺（``2000 V · 2.5 mA``），超出
  ``READBACK_SUMMARY_LIMIT`` 个收敛成 ``· +n``，明细进 tooltip。
* **输出**：0/1 开关渲染成一对 ``[开][关]`` 按钮，选中态跟随实际回读（实际开着
  而按钮显示关，点一次就误发关断）；量程 >1 的整数「开关」（如气流模式 0-2）
  退化成整数框；``pulse`` 信号渲染成一次性动作按钮。

写入结果
--------
行里不再放「状态」列（三列版式放不下）：下发结果统一进**顶栏的消息区**
（带设备+量名与原始拒绝原因），同时被拒的那一行回读数字变红、原因进 tooltip。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from PySide6.QtCore import QPoint, QRect, QSettings, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from apps.desktop_client import instrument_api
from apps.desktop_client.pages.common import page_layout
from apps.desktop_client.pages.registry import PageSpec
from apps.desktop_client.pv_mapping_api import PvMappingRequestThread
from apps.desktop_client.spectrum_plot import SpectrumPlot
from apps.desktop_client.theme import current_palette
from apps.desktop_client.trend_buffer import DEFAULT_KEEP_S, TrendBuffer
from apps.desktop_client.widgets import PageHeading

# 回读轮询间隔：128 路一次快照，本地回环开销很低
POLL_INTERVAL_MS = 1000
# 数值读数用等宽字体，位数对齐才看得出变化
VALUE_FONT = "Consolas"
READBACK_PT = 9
BIG_VALUE_PT = 11

# ---- 三列宽度预算：由**实测需求**反推，不是拍的 ----
# 实测每种设备形态的格子需求（build/row_budget.py）：
#   设备名 77px（DW10 后偏转板）｜设定槽 spin 59px（"10000.0"）+ 下发 28px
#   ｜回读 103px（"5000 W · 999.9 mA" + 稳定标记）｜输出 97px（模式框 60+34）
# 曾经 OUTPUT_WIDTH=80 而输出格实测要 104–114px，磁铁/溅射/气路三张卡片的
# 按钮是被裁掉的；这里按需求给足，再靠收紧按钮内边距把总宽压回去。
# 目标：三个 3 列在**现场最小屏 1600x1000**（可用约 1346px）下不用横向滚动。
NAME_WIDTH = 80
SPIN_WIDTH = 64
SEND_WIDTH = 36
MODE_SPIN_WIDTH = 60
READBACK_WIDTH = 114
OUTPUT_WIDTH = 104
TOGGLE_WIDTH = 32
PULSE_WIDTH = 28
SLOT_SPACING = 4
SLOT_WIDTH = SPIN_WIDTH + SEND_WIDTH + SLOT_SPACING
ROW_SPACING = 4
COLUMN_SPACING = 6
COLUMN_COUNT = 3
CANVAS_MARGIN = 6
CARD_GAP = 10

# 行高：一个设定槽的高度 + 每多叠一个槽增加的高度
ROW_HEIGHT = 28
SLOT_STACK_STEP = 24
EMPTY = "—"

# 一列的最小宽度：行宽 + 面板左右内边距 + 面板左右边框
PANEL_MARGIN = 3
PANEL_BORDER = 2
# 卡片位置/大小的存储前缀。v2 用的是 saveGeometry（对子控件无效，等于没存），
# 这里换 v3 存 [x, y, w, h]，旧存档自然失效、不会再干扰布局。
LAYOUT_SETTINGS_PREFIX = "manual/layout-v3/"
COLUMN_MIN_WIDTH = (
    NAME_WIDTH
    + SLOT_WIDTH
    + READBACK_WIDTH
    + OUTPUT_WIDTH
    + 3 * ROW_SPACING
    + 2 * PANEL_MARGIN
    + PANEL_BORDER
)
MIN_HOLDER_WIDTH = COLUMN_COUNT * COLUMN_MIN_WIDTH + (COLUMN_COUNT - 1) * COLUMN_SPACING

# 顶栏大字读数最多显示几路；超出就退回组面板，避免有数看不见
TOPBAR_SLOTS = 3
# 顶栏读数：定宽数字 + 变化量文字的预留宽度（防止每秒抖动 / 布局跳动）
READOUT_MIN_WIDTH = 92
TREND_TEXT_WIDTH = 58
# 变化量比较的时间基准（秒）
DELTA_WINDOW_S = 30.0
# 顶栏 sparkline 的显示窗口（秒）
SPARKLINE_WINDOW_S = 120.0

# 滚轮 / 方向键步进档：基准步长按量程推出，再乘这三档
STEP_LEVELS = (1, 10, 100)
# 基准步长 = 量程 / SPAN_PER_BASE_STEP，于是三档正好是量程的 0.2% / 2% / 20%。
# 这个分母不是随便取的：档位之间必须正好差 10 倍，而且**最粗一档不能一滚就顶到
# 量程端点**——顶到端点后增量被夹住，×100 与 ×10 的差别就消失了。实测旧的分母
# 50（×100 = 200% 量程）在 DW 通道 5000 V 处三档增量全是 +100 V，与 ×1 完全一样。
SPAN_PER_BASE_STEP = 500.0
# 单行最多几个设定槽（现场映射最多 2 个：磁铁电流+速率、主高压电压+电流）
MAX_SETPOINT_SLOTS = 3
# 回读列一行最多平铺几个读数，超出用 «· +n» 提示，明细进 tooltip
READBACK_SUMMARY_LIMIT = 2

# 三列分列：按信号 PV 命名空间认列，不认可改的分组显示名
_COLUMN_OF_NAMESPACE = {
    "hv_array": 1,
    "hv_bd": 2,
    "magnet": 2,
    "detector": 2,
}
_DEFAULT_COLUMN = 0
# 顶栏大字读数取自这些命名空间（demo 顶栏就是 FC1/FC2 电流）
_TOPBAR_NAMESPACES = ("detector",)

# 可写角色；其余（含空角色）一律按只读呈现，不给语义不明的可写控件。
_WRITABLE_ROLES = ("setpoint", "toggle", "pulse")
_ROLE_BUCKETS = {"setpoint": "setpoints", "toggle": "toggles", "pulse": "pulses"}

# 「全部关断」按此顺序下发：先断输出，再退设定值。顺序反了会出现
# 「输出仍开着而设定值已退」的中间状态。
_ALL_OFF_ROLE_ORDER = ("toggle", "setpoint")


def _floor_nice(value: float) -> float:
    """取不超过 value 的最大 1-2-5 整数倍，用于推导步进幅度。"""
    if value <= 0 or not math.isfinite(value):
        return 1.0
    exponent = math.floor(math.log10(value))
    base = 10.0**exponent
    for multiple in (5, 2, 1):
        if multiple * base <= value:
            return multiple * base
    return base  # pragma: no cover - 循环必然命中


def _step_sizes(entry: dict) -> tuple[float, float]:
    """由量程推出设定框的常用步长（基准档、粗档）。

    基准档取量程的 1/500，再取 1-2-5 整数倍，所以三档步进是量程的
    0.2% / 2% / 20% 量级：粗档最多滚 5 格就走完整个量程，不会一格顶到端点。
    """
    low = entry.get("min_value")
    high = entry.get("max_value")
    if low is None or high is None or high <= low:
        return 1.0, 10.0
    fine = _floor_nice((high - low) / SPAN_PER_BASE_STEP)
    return fine, fine * 10.0


def _decimals_for(step: float) -> int:
    if step >= 1:
        return 0
    return min(4, max(0, -math.floor(math.log10(step))))


def _input_format(entry: dict) -> tuple[float, int]:
    """设定框的（基准步长, 小数位）。

    小数位比步长多留一位：0-50 kV 这种量程若只给 0 位小数就再也输不进 12.5。
    """
    fine, _coarse = _step_sizes(entry)
    return fine, min(3, _decimals_for(fine) + 1)


def _device_of(signal: str) -> str:
    """信号所属设备：去掉最后一段（``hv_array.dw04.voltage_setpoint`` → ``hv_array.dw04``）。"""
    head, _, _tail = signal.rpartition(".")
    return head or signal


def _namespace_of(device: str) -> str:
    """设备的 PV 命名空间：``hv_bd.cylinder1`` → ``hv_bd``；无冒号段时取自身。"""
    head, _, _tail = device.partition(".")
    return head or device


def _common_label(labels: Sequence[str]) -> str:
    """设备名 = 各信号标签的最长公共前缀，忽略与设备名毫无共同部分的离群标签。

    ``DW4 通道4 电压设定 / DW4 通道4 输出使能`` → ``DW4 通道4``；
    ``溅射功率设定 / 溅射电源使能 / 灭弧`` → ``溅射``（「灭弧」是离群标签）。

    公共前缀若停在半个词中间（``A 电压``，因为该设备只有电压类信号），就退到
    最后一个词边界：否则量名会被设备名吃掉，同组里「电压设定 / 电流设定」
    会双双变成「设定」。
    """
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0].strip()

    prefix = max(labels, key=len)
    for label in labels:
        limit = min(len(prefix), len(label))
        index = 0
        while index < limit and prefix[index] == label[index]:
            index += 1
        if index == 0:
            continue
        prefix = prefix[:index]

    ended_on_boundary = prefix.endswith(" ")
    prefix = prefix.strip(" ·-(")
    if prefix and not ended_on_boundary and " " in prefix:
        prefix = prefix.rsplit(" ", 1)[0].strip(" ·-(")
    return prefix or labels[0].strip()


def _short_label(label: str, device_label: str) -> str:
    """去掉设备名后的量名：``DW4 通道4 电压设定`` + ``DW4 通道4`` → ``电压设定``。"""
    if device_label and label.startswith(device_label):
        rest = label[len(device_label) :].strip(" ·-")
        if rest:
            return rest
    return label


def _fmt(value: object, unit: str = "", decimals: int = 3) -> str:
    if value is None:
        return EMPTY
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return EMPTY
    text = f"{number:.{decimals}f}".rstrip("0").rstrip(".")
    return f"{text}{(' ' + unit) if unit else ''}"


def _fmt_fixed(value: object, unit: str = "", decimals: int = 2) -> str:
    """固定小数位的读数，**不做去尾零**。

    顶栏大字读数用这个：`_fmt` 会把 `12.500` 压成 `12.5`、`8.000` 压成 `8`，
    位数一变整行就会左右抖动（既有 UI 方案 §5.1 要求"读数固定宽度"）。
    表格里的回读列是定宽右对齐的，抖动传不出去，继续用 `_fmt` 省地方。
    """
    if value is None:
        return EMPTY
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return EMPTY
    return f"{number:.{decimals}f}{(' ' + unit) if unit else ''}"


def _parse_time(text: object) -> float | None:
    """解析服务端给的 ISO 时间戳，返回 Unix 秒；解不出来返回 None。

    服务端可能给带时区的（`...+00:00`）或不带的（按 UTC 当作本地会出偏差），
    所以统一按"无时区即 UTC"处理。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip().replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.timestamp()


def _lag_seconds(reading: dict, now: float | None = None) -> float | None:
    """这条读数距现在多久（秒）。优先 `received_time`，退回 `source_time`。"""
    stamp = _parse_time(reading.get("received_time"))
    if stamp is None:
        stamp = _parse_time(reading.get("source_time"))
    if stamp is None:
        return None
    return max(0.0, (now if now is not None else time.time()) - stamp)


def _set_state(label: QLabel, text: str, state: str = "") -> None:
    """写文本、挂 tooltip 并切换语义配色（good / warn / error）。"""
    label.setText(text)
    label.setToolTip(text)
    label.setProperty("state", state)
    label.style().unpolish(label)
    label.style().polish(label)


def _pulse_caption(label: str) -> str:
    """从标签里取动作名作按钮文字（「磁铁1 启动」→「启动」）。"""
    tail = label.split()[-1] if label.split() else label
    return tail or "触发"


class DeviceSpec:
    """一个设备的全部信号，按控件角色分好组。"""

    def __init__(self, device: str, entries: list[dict]) -> None:
        self.device = device
        self.namespace = _namespace_of(device)
        self.entries = entries
        self.label = _common_label(
            [str(e.get("label") or e.get("signal") or "") for e in entries]
        )
        self.setpoints: list[dict] = []
        self.toggles: list[dict] = []
        self.pulses: list[dict] = []
        self.readbacks: list[dict] = []
        for entry in entries:
            role = str(entry.get("role") or "")
            bucket = _ROLE_BUCKETS.get(role) if entry.get("writable") else None
            if bucket is None:
                # 只读量、以及角色不明的可写条目（宁可不给控件）都走回读列
                self.readbacks.append(entry)
            else:
                getattr(self, bucket).append(entry)

    @property
    def controls(self) -> list[dict]:
        return self.setpoints + self.toggles + self.pulses

    def signals(self) -> list[str]:
        return [str(entry.get("signal") or "") for entry in self.entries]

    def unit_of(self, signal: str) -> str:
        for entry in self.entries:
            if str(entry.get("signal") or "") == signal:
                return str(entry.get("unit") or "")
        return ""

    def label_of(self, signal: str) -> str:
        for entry in self.entries:
            if str(entry.get("signal") or "") == signal:
                return str(entry.get("label") or signal)
        return signal

    def slot_count(self) -> int:
        return min(MAX_SETPOINT_SLOTS, len(self.setpoints))

    def row_height(self) -> int:
        return ROW_HEIGHT + max(0, self.slot_count() - 1) * SLOT_STACK_STEP


def build_device_specs(entries: list[dict]) -> list[DeviceSpec]:
    """把映射条目按设备归并，保持设备首次出现的顺序。"""
    buckets: dict[str, list[dict]] = {}
    for entry in entries:
        buckets.setdefault(_device_of(str(entry.get("signal") or "")), []).append(entry)
    return [DeviceSpec(device, items) for device, items in buckets.items()]


def column_of(specs: Sequence[DeviceSpec]) -> int:
    """一组设备该放第几列：按 PV 命名空间，认不出来就走默认列。"""
    for spec in specs:
        column = _COLUMN_OF_NAMESPACE.get(spec.namespace)
        if column is not None:
            return column
    return _DEFAULT_COLUMN


def assign_columns(
    groups: Sequence[tuple[str, Sequence[DeviceSpec]]],
) -> dict[str, int]:
    """把设备组分配到三列。

    命名空间认得出来的按 ``_COLUMN_OF_NAMESPACE`` 走；一组都没落到中列时，
    行数最多的一组顶上——中列是给内容最多的那组独占的。
    """
    assignment: dict[str, int] = {}
    for name, specs in groups:
        assignment[name] = column_of(specs)

    if 1 not in assignment.values() and groups:
        biggest = max(groups, key=lambda item: sum(s.row_height() for s in item[1]))
        assignment[biggest[0]] = 1
    return assignment


def _all_off_plan(entries: list[dict]) -> list[tuple[str, float]]:
    """「全部关断」的下发序列：先断全部输出，再退全部设定值。

    顺序反了会出现「输出仍开着、而设定值已经退到 0」的中间状态——对高压电源
    来说这是最不该出现的组合。脉冲类信号（启动/停止/复位/灭弧）不参与：
    它们是一次性触发，写 0 没有语义，反而可能触发另一次动作。
    """
    by_role: dict[str, list[dict]] = {}
    for entry in entries:
        if entry.get("writable"):
            by_role.setdefault(str(entry.get("role") or ""), []).append(entry)

    plan: list[tuple[str, float]] = []
    for role in _ALL_OFF_ROLE_ORDER:
        for entry in by_role.get(role, []):
            if role == "toggle":
                safe = 0.0
            else:
                low = entry.get("min_value")
                safe = float(low) if low is not None else 0.0
            plan.append((str(entry["signal"]), safe))
    return plan


class _WheelSpinBox(QDoubleSpinBox):
    """滚轮直接调值。

    Qt 的 ``QAbstractSpinBox`` 只在获得焦点时才响应滚轮（避免表单滚动误改），
    但表格式页面里控件密集、点位小，必须能直接滚。放开是安全的：本页改值
    **不自动下发**，误滚只是改了一个待确认的数字。
    """

    def wheelEvent(self, event) -> None:  # noqa: N802
        if not self.isEnabled():
            event.ignore()
            return
        ticks = event.angleDelta().y() / 120.0
        if not ticks:
            event.ignore()
            return
        self.setValue(self.value() + ticks * self.singleStep())
        event.accept()


class _TopBar(QFrame):
    """跨三列的顶栏：FC1/FC2 大字读数 + 步进档 + 最近下发结果 + 页面按钮。"""

    def __init__(self, page: ManualControlPage) -> None:
        super().__init__(objectName="manualTopbar")
        self.page = page
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 7, 12, 7)
        layout.setSpacing(12)

        self.readouts: list[tuple[dict, QLabel]] = []
        self.readout_trends: list[QLabel] = []
        self.readout_sparks: list[_Sparkline] = []
        self.readout_box = QWidget()
        self.readout_layout = QHBoxLayout(self.readout_box)
        self.readout_layout.setContentsMargins(0, 0, 0, 0)
        self.readout_layout.setSpacing(16)
        layout.addWidget(self.readout_box)

        self.message_label = QLabel("正在读取设备映射…")
        self.message_label.setObjectName("mutedText")
        # 拒绝原因可能很长，不能让它把整页撑得比窗口还宽——给多少地方用多少，
        # 剩下的进 tooltip（_set_state 会挂）。
        self.message_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.message_label.setMinimumWidth(120)
        layout.addWidget(self.message_label, 1)

        self.lock_button = QPushButton("🔒 锁定布局", objectName="stepButton")
        self.lock_button.setCheckable(True)
        self.lock_button.setToolTip("锁定后卡片不可拖拽/调整大小")
        self.lock_button.toggled.connect(page.toggle_layout_lock)
        layout.addWidget(self.lock_button)

        self.reload_button = QPushButton("重新载入映射")
        self.reload_button.clicked.connect(page.reload_mapping)
        layout.addWidget(self.reload_button)

        self.reset_layout_button = QPushButton("恢复布局")
        self.reset_layout_button.setToolTip("清掉卡片的位置记忆，回到默认三列网格")
        self.reset_layout_button.clicked.connect(page.reset_layout)
        layout.addWidget(self.reset_layout_button)

        self.all_off_button = QPushButton("全部关断", objectName="dangerButton")
        self.all_off_button.clicked.connect(page.confirm_all_off)
        self.all_off_button.setEnabled(False)
        layout.addWidget(self.all_off_button)

    def set_readouts(self, entries: Sequence[dict]) -> None:
        """顶栏大字读数（demo 顶栏就是 FC1/FC2 电流）。

        每格是「标签 + 定宽数字 + 变化量 / 滞后」三段：
        * 数字用 `_fmt_fixed` 固定两位小数——`_fmt` 会去尾零，位数一变整条
          顶栏就跟着抖（既有 UI 方案 §5.1「读数固定宽度」）；
        * 变化量相对 ``DELTA_WINDOW_S`` 前的值，带箭头与正负号，颜色只作辅助；
        * 滞后（服务时间戳算出来的）单独显示，和"未连接"区分开。
        """
        while self.readout_layout.count():
            item = self.readout_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.readouts.clear()
        self.readout_trends.clear()
        self.readout_sparks.clear()
        for entry in entries:
            cell = QWidget()
            cell_layout = QHBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(4)
            cell_layout.addWidget(QLabel(str(entry.get("label") or ""), objectName="mutedText"))
            value = QLabel(EMPTY, objectName="heroValue")
            value.setFont(QFont(VALUE_FONT, BIG_VALUE_PT))
            value.setMinimumWidth(READOUT_MIN_WIDTH)
            value.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            cell_layout.addWidget(value)
            trend = QLabel("", objectName="mutedText")
            trend.setFont(QFont(VALUE_FONT, 8))
            trend.setMinimumWidth(TREND_TEXT_WIDTH)
            cell_layout.addWidget(trend)
            spark = _Sparkline(self.page)
            spark.set_signal(str(entry.get("signal") or ""))
            cell_layout.addWidget(spark)
            self.readout_layout.addWidget(cell)
            self.readouts.append((entry, value))
            self.readout_trends.append(trend)
            self.readout_sparks.append(spark)

    def apply_readings(self, readings: dict[str, dict]) -> None:
        now = time.time()
        for index, (entry, label) in enumerate(self.readouts):
            signal = str(entry.get("signal") or "")
            reading = readings.get(signal) or {}
            trend = self.readout_trends[index] if index < len(self.readout_trends) else None
            if not reading.get("connected"):
                _set_state(label, "未连接", "error")
                label.setToolTip(signal)
                if trend is not None:
                    _set_state(trend, "", "")
                continue
            unit = str(entry.get("unit") or "")
            value = reading.get("value")
            _set_state(label, _fmt_fixed(value, unit), "good")
            lag = _lag_seconds(reading, now)
            if lag is not None and lag > 2 * POLL_INTERVAL_MS / 1000.0:
                # 读数还在来，但已经旧了——和"未连接"不是一回事
                _set_state(label, _fmt_fixed(value, unit), "warn")
                label.setToolTip(f"{signal}\n数据滞后 {lag:.0f}s（服务端时间戳）")
                if trend is not None:
                    _set_state(trend, f"滞后{lag:.0f}s", "warn")
                continue
            label.setToolTip(signal)
            if trend is not None:
                _set_state(trend, self.trend_text(signal), "")

    def trend_text(self, signal: str) -> str:
        """变化量文字：相对 ``DELTA_WINDOW_S`` 前的采样。"""
        delta = self.page.trend_delta(signal)
        if delta is None:
            return ""
        arrow = "▲" if delta >= 0 else "▼"
        return f"{arrow}{delta:+.2f}"

    def set_message(self, text: str, state: str = "") -> None:
        _set_state(self.message_label, text, state)

    @property
    def status_label(self) -> QLabel:
        """页面状态就写在顶栏消息区（保持与旧接口同名，便于自检）。"""
        return self.message_label


class _SetpointEditor(QWidget):
    """一个设定槽：滚轮设定框 + 下发（步进档是页面级的，在顶栏）。"""

    def __init__(self, entry: dict, on_submit: Callable[[float], None]) -> None:
        super().__init__()
        self.entry = entry
        self.signal = str(entry.get("signal") or "")
        self.unit = str(entry.get("unit") or "")
        self._on_submit = on_submit
        self.base_step, decimals = _input_format(entry)

        low = entry.get("min_value")
        high = entry.get("max_value")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SLOT_SPACING)

        self.spin = _WheelSpinBox(objectName="rowInput")
        self.spin.setDecimals(decimals)
        self.spin.setSingleStep(self.base_step)
        self.spin.setRange(
            float(low) if low is not None else -1e9,
            float(high) if high is not None else 1e9,
        )
        self.spin.setFixedWidth(SPIN_WIDTH)
        self.spin.setKeyboardTracking(False)
        self.spin.setToolTip(
            f"{entry.get('label', self.signal)}\n滚轮 / 方向键调值，点「下发」才写设备"
        )
        layout.addWidget(self.spin)

        self.send_button = QPushButton("下发", objectName="setButton")
        self.send_button.setFixedWidth(SEND_WIDTH)
        self.send_button.setToolTip(f"下发 {entry.get('label', self.signal)}")
        self.send_button.clicked.connect(lambda: self._on_submit(self.spin.value()))
        layout.addWidget(self.send_button)

    def set_level(self, level: int) -> None:
        """页面级步进档变化时重设单步步长。"""
        self.spin.setSingleStep(self.base_step * level)

    def level(self) -> int:
        """当前生效的档位倍数。"""
        step = self.spin.singleStep()
        return int(round(step / self.base_step)) if self.base_step else 1

    def controls(self) -> list[QWidget]:
        return [self.spin, self.send_button]

    def set_enabled(self, enabled: bool) -> None:
        for widget in self.controls():
            widget.setEnabled(enabled)

    def apply_reading(self, value: object) -> None:
        """按实际值回填，便于「在当前值上微调」（正在输入时不打断）。"""
        if value is None or self.spin.hasFocus():
            return
        self.spin.setValue(float(value))  # type: ignore[arg-type]


class _GroupPanel(QFrame):
    """三列版式里的一个设备组面板（比共享 ``Panel`` 更紧凑）。

    高压组（DW / 聚焦 / 漂移 / 圆筒等）自动加琥珀色左边框警示；
    header 可点击折叠 body，长列表不用一屏全铺开。

    ``with_steps=False`` 用于趋势卡片那种不需要「×1/×10/×100 步进档」的面板。
    """

    # 命中这些关键词的组视为高压/危险组，加警示边框
    HAZARD_KEYWORDS = ("DW", "dw", "hv", "聚焦", "漂移", "圆筒", "kV", "高压")

    def __init__(
        self,
        title: str,
        subtitle: str = "",
        hazard: bool = False,
        *,
        with_steps: bool = True,
    ) -> None:
        super().__init__(objectName="manualGroup")
        self.setProperty("hazard", hazard)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 5, 6, 7)
        outer.setSpacing(3)

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(1, 0, 1, 3)
        header_layout.setSpacing(5)
        # 折叠开关：点击切换 body 可见性
        self._fold_button = QPushButton("▾", objectName="stepButton")
        self._fold_button.setFixedWidth(20)
        self._fold_button.setCheckable(True)
        self._fold_button.setChecked(False)
        self._fold_button.setToolTip("折叠/展开本组")
        header_layout.addWidget(self._fold_button)
        header_layout.addWidget(QLabel(title, objectName="panelTitle"))
        header_layout.addStretch()

        # 倍率只改变本组设定框的滚轮/方向键步长，不改数值、更不会自动下发。
        self._step_level_index = 0
        self.step_buttons: list[QPushButton] = []
        step_group = QButtonGroup(self)
        step_group.setExclusive(True)
        for index, level in enumerate(STEP_LEVELS if with_steps else ()):
            button = QPushButton(f"×{level}", objectName="stepButton")
            button.setCheckable(True)
            button.setFixedWidth(34)
            button.setToolTip(f"本组输入步长 ×{level}；仅调整输入，不会下发")
            button.clicked.connect(
                lambda _checked=False, i=index: self.set_step_level(i)
            )
            step_group.addButton(button)
            header_layout.addWidget(button)
            self.step_buttons.append(button)
        if self.step_buttons:
            self.step_buttons[0].setChecked(True)

        if subtitle:
            header_layout.addWidget(QLabel(subtitle, objectName="mutedText"))
        outer.addWidget(header)

        # body 内容放进 holder，折叠时隐藏 holder。注意 self.body 只能挂到一个父
        # 布局上——以前这里同时 addLayout 到 outer 和 holder，Qt 会报
        # "layout already has a parent" 并靠 re-parent 兜住。
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(1)

        self._body_holder = QWidget()
        holder_layout = QVBoxLayout(self._body_holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setSpacing(1)
        holder_layout.addLayout(self.body)
        outer.addWidget(self._body_holder)

        self._fold_button.toggled.connect(self._on_fold_toggled)

        # 危险组只设置语义属性，具体颜色由浅/深主题共同管理。

    def _on_fold_toggled(self, checked: bool) -> None:
        self._body_holder.setVisible(not checked)
        self._fold_button.setText("▸" if checked else "▾")

    @property
    def step_level(self) -> int:
        return STEP_LEVELS[self._step_level_index]

    def set_step_level(self, index: int) -> None:
        self._step_level_index = max(0, min(len(STEP_LEVELS) - 1, index))
        self.step_buttons[self._step_level_index].setChecked(True)
        for editor in self.findChildren(_SetpointEditor):
            editor.set_level(self.step_level)


class _DraggableFrame(QFrame):
    """包一个设备组卡片：header 可拖拽移动，右下角可 resize。

    位置/大小持久化到 QSettings（按组名），下次启动恢复。

    **为什么不用 `saveGeometry` / `restoreGeometry`**：那两个 API 只对顶层窗口
    有效，对画布里的子控件是空操作（实测 `restoreGeometry` 返回 False 且几何
    一点不变）——上一版就是这样，拖了半天其实从没存下来过。这里改成直接存
    ``[x, y, w, h]``，并在恢复时用当前画布尺寸校验：放不进画布的存档一律丢弃，
    否则卡片会被摆到看不见的地方。
    """

    HIT_HEADER = 32
    HIT_RESIZE = 14
    MIN_W = 360
    MIN_H = 120

    def __init__(self, child: _GroupPanel, key: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("manualFrame")
        self.setAutoFillBackground(False)
        self._key = key
        self._locked = False
        self._drag_offset: QPoint | None = None
        self._resize_origin: QPoint | None = None
        self._resize_geom: QRect | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(child)
        self.child = child
        self.setMinimumSize(self.MIN_W, self.MIN_H)
        self.setMouseTracking(True)

    def set_locked(self, locked: bool) -> None:
        self._locked = locked
        self._drag_offset = None
        self._resize_origin = None
        self._resize_geom = None
        if locked:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    # ---- 持久化 ----
    def save_geometry(self) -> None:
        settings = layout_settings()
        settings.setValue(
            f"{LAYOUT_SETTINGS_PREFIX}{self._key}/rect",
            [self.x(), self.y(), self.width(), self.height()],
        )

    def restore_geometry(self, canvas: QWidget | None = None) -> bool:
        """按存档恢复位置，返回是否真的恢复了（False 表示沿用默认网格位置）。

        存档只在**完整落在当前画布内**时才用：画布大小会随窗口变，跨尺寸沿用
        旧坐标会把卡片摆到画布外——看起来就是"卡片不见了"。
        """
        settings = layout_settings()
        value = settings.value(f"{LAYOUT_SETTINGS_PREFIX}{self._key}/rect")
        rect = _parse_saved_rect(value)
        if rect is None:
            return False
        x, y, width, height = rect
        if canvas is None:
            canvas = self.parentWidget()
        if canvas is None:
            return False
        if x < 0 or y < 0 or width < self.MIN_W or height < self.MIN_H:
            return False
        if x + width > canvas.width() or y + height > canvas.height():
            return False
        self.setGeometry(x, y, width, height)
        return True

    # ---- 鼠标交互 ----
    def _hit(self, pos: QPoint) -> str:
        if (
            pos.x() >= self.width() - self.HIT_RESIZE
            and pos.y() >= self.height() - self.HIT_RESIZE
        ):
            return "resize"
        if pos.y() <= self.HIT_HEADER:
            return "move"
        return ""

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._locked:
            return
        hit = self._hit(event.position().toPoint())
        if hit == "move" and event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.raise_()
        elif hit == "resize" and event.button() == Qt.MouseButton.LeftButton:
            self._resize_origin = event.globalPosition().toPoint()
            self._resize_geom = self.geometry()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        elif self._resize_origin is not None and self._resize_geom is not None:
            delta = event.globalPosition().toPoint() - self._resize_origin
            new_w = max(self.MIN_W, self._resize_geom.width() + delta.x())
            new_h = max(self.MIN_H, self._resize_geom.height() + delta.y())
            self.resize(new_w, new_h)
        else:
            hit = self._hit(pos)
            self.setCursor(
                Qt.CursorShape.SizeFDiagCursor
                if hit == "resize"
                else Qt.CursorShape.SizeAllCursor
                if hit == "move"
                else Qt.CursorShape.ArrowCursor
            )
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_offset = None
        self._resize_origin = None
        self._resize_geom = None
        self.save_geometry()
        super().mouseReleaseEvent(event)


class _DeviceRow(QWidget):
    """一行一个设备：名称 ｜ 设定槽 ｜ 回读 ｜ 输出。"""

    def __init__(self, spec: DeviceSpec, page: ManualControlPage) -> None:
        super().__init__()
        self.spec = spec
        self.page = page
        self.device = spec.device
        self.label_text = spec.label
        self._rejected = False
        self.switches: list[tuple[str, QPushButton, QPushButton]] = []
        self.modes: list[tuple[dict, _WheelSpinBox, QPushButton]] = []
        self.triggers: list[QPushButton] = []
        self.output_layout: QHBoxLayout | None = None

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 1, 0, 1)
        row.setSpacing(ROW_SPACING)
        self.setMinimumHeight(spec.row_height())

        self.name_label = QLabel(spec.label)
        self.name_label.setFixedWidth(NAME_WIDTH)
        self.name_label.setToolTip(
            "\n".join(
                f"{entry.get('label', '')}    {entry.get('pv', '')}" for entry in spec.entries
            )
        )
        row.addWidget(self.name_label)

        # 设定槽：设备有 2 个设定量时在同一个格子里上下叠两槽
        self.editors: list[_SetpointEditor] = []
        slot_cell = QWidget()
        slot_cell.setFixedWidth(SLOT_WIDTH)
        slot_layout = QVBoxLayout(slot_cell)
        slot_layout.setContentsMargins(0, 0, 0, 0)
        slot_layout.setSpacing(2)
        for entry in spec.setpoints[:MAX_SETPOINT_SLOTS]:
            signal = str(entry.get("signal") or "")
            editor = _SetpointEditor(
                entry, lambda value, s=signal: self.page.write_signal(self, s, value)
            )
            slot_layout.addWidget(editor)
            self.editors.append(editor)
        if not self.editors:
            slot_layout.addWidget(QLabel(EMPTY))
        row.addWidget(slot_cell)

        self.readback_label = QLabel(EMPTY)
        self.readback_label.setObjectName("readbackValue")
        self.readback_label.setFont(QFont(VALUE_FONT, READBACK_PT))
        self.readback_label.setFixedWidth(READBACK_WIDTH)
        self.readback_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        row.addWidget(self.readback_label)

        cell = QWidget()
        cell.setFixedWidth(OUTPUT_WIDTH)
        self.output_layout = QHBoxLayout(cell)
        self.output_layout.setContentsMargins(0, 0, 0, 0)
        self.output_layout.setSpacing(SLOT_SPACING)
        row.addWidget(cell)

        self._build_output()

    # ------------------------------------------------------------------
    # 输出格
    # ------------------------------------------------------------------
    def _build_output(self) -> None:
        assert self.output_layout is not None
        for entry in self.spec.toggles:
            signal = str(entry.get("signal") or "")
            high = entry.get("max_value")
            if high is not None and float(high) > 1.0:
                self._build_mode(entry, signal)
                continue
            # 开/关分成两个明确动作，避免单个 toggle 在回读延迟时语义反转。
            on = QPushButton("开", objectName="switchButton")
            off = QPushButton("关", objectName="switchButton")
            on.setProperty("action", "on")
            off.setProperty("action", "off")
            group = QButtonGroup(self)
            group.setExclusive(True)
            for button in (on, off):
                button.setCheckable(True)
                button.setFixedWidth(TOGGLE_WIDTH)
                button.setFixedHeight(24)
                group.addButton(button)
                self.output_layout.addWidget(button)
            on.setToolTip(f"开启 {entry.get('label', signal)}")
            off.setToolTip(f"关闭 {entry.get('label', signal)}")
            on.clicked.connect(
                lambda _checked=False, s=signal: self.page.write_signal(self, s, 1.0)
            )
            off.clicked.connect(
                lambda _checked=False, s=signal: self.page.write_signal(self, s, 0.0)
            )
            self.switches.append((signal, on, off))

        for entry in self.spec.pulses:
            signal = str(entry.get("signal") or "")
            button = QPushButton(
                _pulse_caption(str(entry.get("label") or "")), objectName="rowButton"
            )
            button.setFixedWidth(PULSE_WIDTH)
            button.setToolTip(str(entry.get("label") or signal))
            button.clicked.connect(
                lambda _checked=False, s=signal: self.page.write_signal(self, s, 1.0)
            )
            self.output_layout.addWidget(button)
            self.triggers.append(button)

    def _build_mode(self, entry: dict, signal: str) -> None:
        """量程 >1 的「开关」其实是多档模式，用整数框而不是开/关。"""
        assert self.output_layout is not None
        low = entry.get("min_value")
        high = entry.get("max_value")
        spin = _WheelSpinBox(objectName="rowInput")
        spin.setDecimals(0)
        spin.setSingleStep(1.0)
        spin.setRange(
            float(low) if low is not None else 0.0,
            float(high) if high is not None else 1.0,
        )
        spin.setFixedWidth(MODE_SPIN_WIDTH)
        spin.setToolTip(str(entry.get("label") or signal))
        send = QPushButton("下发", objectName="setButton")
        send.setFixedWidth(SEND_WIDTH)
        send.clicked.connect(
            lambda _checked=False, s=signal, w=spin: self.page.write_signal(self, s, w.value())
        )
        self.output_layout.addWidget(spin)
        self.output_layout.addWidget(send)
        self.modes.append((entry, spin, send))

    # ------------------------------------------------------------------
    # 刷新
    # ------------------------------------------------------------------
    def writable_signals(self) -> list[str]:
        return [str(entry.get("signal") or "") for entry in self.spec.controls]

    def readback_signals(self) -> list[str]:
        return [str(entry.get("signal") or "") for entry in self.spec.readbacks]

    def controls(self) -> list[QWidget]:
        """随「未连接」一起启停的控件。"""
        widgets: list[QWidget] = []
        for editor in self.editors:
            widgets.extend(editor.controls())
        for _signal, on, off in self.switches:
            widgets.extend((on, off))
        for _entry, spin, send in self.modes:
            widgets.extend([spin, send])
        widgets.extend(self.triggers)
        return widgets

    def set_controls_enabled(self, enabled: bool) -> None:
        for widget in self.controls():
            widget.setEnabled(enabled)

    @staticmethod
    def _is_connected(readings: dict[str, dict], signal: str) -> bool:
        return bool((readings.get(signal) or {}).get("connected"))

    def apply_readings(self, readings: dict[str, dict]) -> None:
        writable = self.writable_signals()
        readbacks = self.readback_signals()
        if writable:
            connected = all(self._is_connected(readings, s) for s in writable)
        else:
            # 只读设备：读数在就算在线
            connected = any(self._is_connected(readings, s) for s in readbacks)
        if not connected and self._rejected:
            self._rejected = False

        for editor in self.editors:
            editor.apply_reading((readings.get(editor.signal) or {}).get("value"))
        self._sync_switches(readings)
        self._sync_modes(readings)
        self._refresh_readback(readings, connected)
        self.set_controls_enabled(connected)

    def _sync_switches(self, readings: dict[str, dict]) -> None:
        """开关 toggle 的选中态跟随实际回读——否则会误发一次反向动作。"""
        for signal, on, off in self.switches:
            reading = readings.get(signal) or {}
            if not reading.get("connected"):
                continue
            state = bool(round(float(reading.get("value") or 0.0)))
            on.blockSignals(True)
            off.blockSignals(True)
            on.setChecked(state)
            off.setChecked(not state)
            on.blockSignals(False)
            off.blockSignals(False)

    def _sync_modes(self, readings: dict[str, dict]) -> None:
        for entry, spin, _send in self.modes:
            signal = str(entry.get("signal") or "")
            reading = readings.get(signal) or {}
            if reading.get("connected") and not spin.hasFocus():
                spin.setValue(float(reading.get("value") or 0.0))

    def _refresh_readback(self, readings: dict[str, dict], connected: bool) -> None:
        if not self.spec.readbacks:
            _set_state(self.readback_label, EMPTY, "")
            return
        if not connected:
            # 整行掉线时说「未连接」，比一排「—」清楚
            _set_state(self.readback_label, "未连接", "error")
            self.readback_label.setToolTip("\n".join(self.readback_signals()))
            return
        parts: list[str] = []
        detail: list[str] = []
        for entry in self.spec.readbacks:
            signal = str(entry.get("signal") or "")
            reading = readings.get(signal) or {}
            text = (
                _fmt(reading.get("value"), str(entry.get("unit") or ""))
                if reading.get("connected")
                else EMPTY
            )
            parts.append(text)
            detail.append(f"{text}    {signal}")
        summary = " · ".join(parts[:READBACK_SUMMARY_LIMIT])
        hidden = len(parts) - READBACK_SUMMARY_LIMIT
        if hidden > 0:
            summary += f" · +{hidden}"
        # 稳定/未稳定：**文字标记 + 颜色**双重表达（既有 UI 方案 §2.3/§4.4：
        # 不能只靠颜色区分状态）。≈ = 回读已进入容差，≠ = 还没到位。
        mark = self._settle_mark(readings)
        if mark:
            summary = f"{mark} {summary}" if summary else mark
        stale = self._stale_seconds(readings)
        if stale is not None:
            summary = f"{summary} · 滞后{stale:.0f}s"
        tone = "error" if self._rejected else self._settled_tone(readings)
        if stale is not None and tone != "error":
            tone = "warn"
        _set_state(self.readback_label, summary or EMPTY, tone)
        self.readback_label.setToolTip("\n".join(detail))

    def _stale_seconds(self, readings: dict[str, dict]) -> float | None:
        """本体读数滞后超过 2 个轮询周期就返回滞后秒数，否则 None。

        "连着的但数据不新"和"断连"是两回事，以前都看不出来（服务返回的
        `received_time` 客户端一处没用）。
        """
        worst: float | None = None
        for entry in self.spec.readbacks or self.spec.controls:
            signal = str(entry.get("signal") or "")
            reading = readings.get(signal) or {}
            if not reading.get("connected"):
                continue
            lag = _lag_seconds(reading)
            if lag is not None and lag > 2 * POLL_INTERVAL_MS / 1000.0:
                worst = lag if worst is None else max(worst, lag)
        return worst

    def _settle_mark(self, readings: dict[str, dict]) -> str:
        """稳定标记：≈ / ≠，没有可判定的设定槽就返回空串。"""
        tone = self._settled_tone(readings)
        if tone == "good":
            return "≈"
        if tone == "warn":
            return "≠"
        return ""

    def _settled_tone(self, readings: dict[str, dict]) -> str:
        """比较**命令值**与**配对回读**：偏差大=warn，已稳定=good。

        比的是设定 PV 的实际值与它 ``readback_signal`` 指向的回读 PV——

        * 不能用 ``editor.spin.value()``：设定框会被设定 PV 回填，拿它和设定 PV
          比就是自己跟自己比，永远"稳定"（这正是这一版之前的 bug）；
        * 也不看用户正在输入的草稿值：没点「下发」就不该影响"设备到没到位"。

        容差优先用映射里的 ``settle_tol``——执行层 ``wait_settled`` 与扫谱/调束
        用的是同一套阈值，界面和它们保持一致；映射没给才退回经验值。
        """
        has_setpoint = False
        for editor in self.editors:
            readback_signal = str(editor.entry.get("readback_signal") or "")
            if not readback_signal:
                continue
            commanded = readings.get(editor.signal) or {}
            actual_reading = readings.get(readback_signal) or {}
            if not commanded.get("connected") or not actual_reading.get("connected"):
                continue
            try:
                setpoint = float(commanded.get("value"))  # type: ignore[arg-type]
                actual = float(actual_reading.get("value"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            has_setpoint = True
            tol = editor.entry.get("settle_tol")
            if tol is not None:
                if abs(actual - setpoint) > float(tol):
                    return "warn"
                continue
            delta = abs(actual - setpoint)
            if delta > 0.5 and setpoint != 0 and delta / abs(setpoint) > 0.02:
                return "warn"
        return "good" if has_setpoint else ""

    def mark_rejected(self, reason: str) -> None:
        """被拒的行把回读数字标红，原因进 tooltip（消息区另有完整原因）。"""
        self._rejected = True
        _set_state(self.readback_label, self.readback_label.text(), "error")
        self.readback_label.setToolTip(reason)

    def clear_rejection(self) -> None:
        if self._rejected:
            self._rejected = False
            _set_state(self.readback_label, self.readback_label.text(), "")

    def quantity_label(self, signal: str) -> str:
        return self.spec.label_of(signal)


def series_with_gaps(
    points: Sequence[tuple[float, float]], max_gap_s: float
) -> tuple[list[float], list[float]]:
    """把「(时间戳, 值)」序列转成绘图用的 x/y，**在缺口处插入 NaN 断线**。

    轮询失败或信号掉线时缓冲里根本没有点；如果直接连线，图上会把"读取中断"
    画成一条平稳的直线——那是最坏的一种误读。插入 NaN 让 pyqtgraph 断笔。
    """
    xs: list[float] = []
    ys: list[float] = []
    previous: float | None = None
    for stamp, value in points:
        if previous is not None and stamp - previous > max_gap_s:
            xs.append(previous + (stamp - previous) / 2.0)
            ys.append(float("nan"))
        xs.append(stamp)
        ys.append(value)
        previous = stamp
    return xs, ys


def best_so_far(values: Sequence[float]) -> list[float]:
    """历史最优（运行最大值）序列，用于趋势图上的"最优"虚线。"""
    out: list[float] = []
    best = float("-inf")
    for value in values:
        if value == value and value > best:  # 跳过 NaN
            best = value
        out.append(best)
    return out


class _Sparkline(QWidget):
    """顶栏内联 sparkline：只看形状，不交互。

    顶栏大字只管"现在多少"，这条 18px 的小曲线管"在往哪走"——趋势卡片被折叠
    或滚出视野时，方向感仍然在。
    """

    WIDTH = 56
    HEIGHT = 18

    def __init__(self, page: ManualControlPage) -> None:
        super().__init__()
        self.page = page
        self._signal = ""
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self.setToolTip(f"最近 {SPARKLINE_WINDOW_S / 60:.0f} 分钟走势")

    def set_signal(self, signal: str) -> None:
        self._signal = signal

    def refresh(self) -> None:
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        tokens = current_palette()
        box = self.rect().adjusted(0, 1, -1, -1)
        points = (
            self.page.trend.window(self._signal, SPARKLINE_WINDOW_S) if self._signal else []
        )
        if len(points) < 2:
            painter.setPen(QPen(QColor(tokens["plotAxis"]), 1))
            painter.drawLine(box.bottomLeft(), box.bottomRight())
            return
        values = [v for _t, v in points]
        low, high = min(values), max(values)
        if high - low < 1e-9:
            low, high = low - 1.0, high + 1.0
        first, last = points[0][0], points[-1][0]
        span = (last - first) or 1.0
        path = QPainterPath()
        for index, (stamp, value) in enumerate(points):
            x = box.left() + (stamp - first) / span * box.width()
            y = box.bottom() - (value - low) / (high - low) * box.height()
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        painter.setPen(QPen(QColor(tokens["plotLine"]), 1.2))
        painter.drawPath(path)


class _TrendPanel(_GroupPanel):
    """束流电流趋势卡片：窗口档位 + 多条曲线 + 统计行。

    与设备卡片同一套外壳（可折叠、可拖拽、可缩放、位置持久化），只是 body 里
    放的是曲线而不是设备行。数据来自 ``page.trend``（客户端滚动缓冲），
    由每次快照推进——不额外发请求。
    """

    WINDOWS = ((30.0, "30s"), (120.0, "2min"), (600.0, "10min"),
               (DEFAULT_KEEP_S, "30min"))
    DEFAULT_WINDOW_S = 120.0
    HEIGHT = 244

    def __init__(self, page: ManualControlPage) -> None:
        super().__init__("束流电流趋势", "来自顶栏读数", with_steps=False)
        self.page = page
        self.window_s = self.DEFAULT_WINDOW_S
        self.signals: list[dict] = []
        self.show_best = False

        bar = QWidget()
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.setSpacing(3)
        self.window_buttons: list[QPushButton] = []
        group = QButtonGroup(self)
        group.setExclusive(True)
        for index, (seconds, caption) in enumerate(self.WINDOWS):
            button = QPushButton(caption, objectName="stepButton")
            button.setCheckable(True)
            button.setFixedWidth(40)
            button.setToolTip(f"显示最近 {caption}")
            button.clicked.connect(lambda _c=False, i=index: self.set_window_index(i))
            group.addButton(button)
            bar_layout.addWidget(button)
            self.window_buttons.append(button)
        self.window_buttons[
            [w[0] for w in self.WINDOWS].index(self.DEFAULT_WINDOW_S)
        ].setChecked(True)

        self.best_button = QPushButton("最优", objectName="stepButton")
        self.best_button.setCheckable(True)
        self.best_button.setFixedWidth(36)
        self.best_button.setToolTip("叠加显示窗口内的历史最优（运行最大值）")
        self.best_button.toggled.connect(self._on_best_toggled)
        bar_layout.addWidget(self.best_button)

        self.clear_button = QPushButton("清空", objectName="stepButton")
        self.clear_button.setFixedWidth(36)
        self.clear_button.setToolTip("清掉已缓冲的采样，重新开始记")
        self.clear_button.clicked.connect(self._on_clear)
        bar_layout.addWidget(self.clear_button)
        bar_layout.addStretch()
        self.body.addWidget(bar)

        self.plot = SpectrumPlot(
            "相对时间 / s", "束流 / nA", compact=True, value_formatter=self._readout_text
        )
        self.body.addWidget(self.plot, 1)

        self.stats_label = QLabel("等待数据…", objectName="mutedText")
        self.stats_label.setFont(QFont(VALUE_FONT, 8))
        self.body.addWidget(self.stats_label)

    # ------------------------------------------------------------------
    def set_readouts(self, entries: Sequence[dict]) -> None:
        """绑定要画的信号（顶栏那几路）。"""
        self.signals = list(entries)
        self.refresh()

    def set_window_index(self, index: int) -> None:
        index = max(0, min(len(self.WINDOWS) - 1, index))
        self.window_s = self.WINDOWS[index][0]
        self.window_buttons[index].setChecked(True)
        self.refresh()

    def _on_best_toggled(self, checked: bool) -> None:
        self.show_best = checked
        self.refresh()

    def _on_clear(self) -> None:
        self.page.trend.clear()
        self.refresh()

    def _readout_text(self, x: float, y: float) -> str:
        unit = str(self.signals[0].get("unit") or "") if self.signals else ""
        return f"{x:.0f} s   {y:.2f} {unit}".rstrip()

    def refresh(self) -> None:
        if not self.signals:
            self.stats_label.setText("等待数据…")
            return
        now = time.time()
        max_gap = 3 * POLL_INTERVAL_MS / 1000.0
        series: list[tuple[list[float], list[float]]] = []
        for entry in self.signals:
            signal = str(entry.get("signal") or "")
            points = self.page.trend.window(signal, self.window_s, now)
            xs, ys = series_with_gaps(points, max_gap)
            # x 轴用"距今多少秒"（0 = 现在），负数往左
            xs = [stamp - now for stamp in xs]
            series.append((xs, ys))
        best: list[float] | None = None
        if self.show_best and series:
            best = best_so_far(series[0][1])
        self.plot.set_series(series, best=best)
        if series and series[0][0]:
            self.plot.set_ranges(-self.window_s, 0.0)
        self._refresh_stats(now)

    def _refresh_stats(self, now: float) -> None:
        parts: list[str] = []
        for entry in self.signals:
            signal = str(entry.get("signal") or "")
            stats = self.page.trend.stats(signal, self.window_s, now)
            name = str(entry.get("label") or signal)
            if stats is None:
                parts.append(f"{name}: 无数据")
                continue
            parts.append(
                f"{name} 当前 {stats['latest']:.2f} · 均 {stats['mean']:.2f} · "
                f"σ {stats['std']:.3f} · 峰峰 {stats['range']:.2f} · "
                f"{stats['count']} 点/{stats['span_s']:.0f}s"
            )
        self.stats_label.setText("　|　".join(parts))


def layout_settings() -> QSettings:
    """卡片布局的存储入口。

    抽成函数是为了测试能把它换成临时 INI 文件：注册表写入在受限环境里会返回
    `Status.AccessError` 而被静默丢弃，测试没法验证"存-取-清"这条链路。
    """
    return QSettings("SpectrumPlatform", "DesktopClient")


def _parse_saved_rect(value: object) -> tuple[int, int, int, int] | None:
    """解析存档里的 ``[x, y, w, h]``；解析不出来返回 None。"""
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p for p in value.strip().strip("[]").replace(",", " ").split() if p]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        return None
    if len(parts) != 4:
        return None
    try:
        numbers = [int(float(p)) for p in parts]
    except (TypeError, ValueError):
        return None
    return numbers[0], numbers[1], numbers[2], numbers[3]


def _header_row(slot_labels: Sequence[str]) -> QWidget:
    """与数据行同宽的表头，保证列对齐。"""
    header = QWidget()
    row = QHBoxLayout(header)
    row.setContentsMargins(0, 0, 0, 2)
    row.setSpacing(ROW_SPACING)

    name = QLabel("设备", objectName="columnHeader")
    name.setFixedWidth(NAME_WIDTH)
    row.addWidget(name)

    slot_cell = QWidget()
    slot_cell.setFixedWidth(SLOT_WIDTH)
    slot_layout = QVBoxLayout(slot_cell)
    slot_layout.setContentsMargins(0, 0, 0, 0)
    slot_layout.setSpacing(2)
    for text in (slot_labels or ["设定"]):
        slot_layout.addWidget(QLabel(text, objectName="columnHeader"))
    row.addWidget(slot_cell)

    readback = QLabel("回读", objectName="columnHeader")
    readback.setFixedWidth(READBACK_WIDTH)
    readback.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    row.addWidget(readback)

    output = QLabel("输出", objectName="columnHeader")
    output.setFixedWidth(OUTPUT_WIDTH)
    row.addWidget(output)
    return header


def _slot_labels(specs: Sequence[DeviceSpec]) -> list[str]:
    """设定槽的表头：用量名（「电压设定」），同组内不一致时退回「设定N」。"""
    labels: list[str] = []
    slots = max((spec.slot_count() for spec in specs), default=0)
    for index in range(slots):
        names = {
            _short_label(str(spec.setpoints[index].get("label") or ""), spec.label)
            for spec in specs
            if index < len(spec.setpoints)
        }
        labels.append(next(iter(names)) if len(names) == 1 else f"设定{index + 1}")
    return labels


class _FrameItemView:
    """为逻辑列诊断提供最小的 QLayoutItem 兼容视图。"""

    def __init__(self, widget: QWidget) -> None:
        self._widget = widget

    def widget(self) -> QWidget:
        return self._widget


class _ColumnView:
    """自由画布中某一逻辑列的只读视图，不接管卡片几何。"""

    def __init__(self, page: ManualControlPage, column: int) -> None:
        self._page = page
        self._column = column

    def _widgets(self) -> list[QWidget]:
        return [
            frame for frame in self._page._frames
            if frame.property("layoutColumn") == self._column
        ]

    def count(self) -> int:
        return len(self._widgets())

    def itemAt(self, index: int) -> _FrameItemView | None:  # noqa: N802
        widgets = self._widgets()
        return _FrameItemView(widgets[index]) if 0 <= index < len(widgets) else None


class ManualControlPage(QWidget):
    """手动控制测试页（单页三列，一行一个设备）。

    进入页面即从执行服务拉取当前 PV 映射（分组、标签、角色、边界都来自映射，
    不在界面里再抄一份），再按 1 Hz 轮询快照刷新回读值。
    """

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[_DeviceRow] = []
        self._rows_by_signal: dict[str, _DeviceRow] = {}
        self._editors: list[_SetpointEditor] = []
        self._entries: list[dict] = []
        self._read_in_flight = False
        self._pending: tuple[_DeviceRow, str, float] | None = None
        self._all_off_queue: list[tuple[str, float]] = []
        self._all_off_active = False
        self._all_off_rejected = 0
        self._mapping_loaded = False
        # 客户端滚动缓冲：服务端没有时序库，趋势只能自己留（见 trend_buffer 说明）
        self.trend = TrendBuffer()
        self.trend_panel: _TrendPanel | None = None
        # 顶栏变化量要跨快照比较，记一下最近一次读数
        self._topbar_signals: list[dict] = []

        layout = page_layout(self)
        layout.addWidget(
            PageHeading(
                "手动控制",
                "一页三列：滚轮改设定，点「下发」才动作，开关按钮跟随实际状态。",
            )
        )
        self.topbar = _TopBar(self)
        layout.addWidget(self.topbar)

        # 整页一个滚动条（demo 手动页也是这么兜底的），纵向滚动、横向按需
        self.scroll = QScrollArea(objectName="groupScroll")
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        # viewport 必须透明，否则深浅切换时露出系统窗口色（右下角那块浅白）
        self.scroll.viewport().setAutoFillBackground(False)
        layout.addWidget(self.scroll, 1)

        # 自由画布：卡片可拖拽移动、右下角 resize，位置持久化
        self._canvas = QWidget(objectName="manualCanvas")
        self._canvas.setAutoFillBackground(False)
        self._canvas.setMinimumSize(MIN_HOLDER_WIDTH + 2 * CANVAS_MARGIN, 640)
        self.scroll.setWidget(self._canvas)
        self._frames: list[_DraggableFrame] = []
        self._grid_x = 0
        self._grid_y = 0
        self._build_columns()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll)

        self._load_mapping()

    # ------------------------------------------------------------------
    # 界面骨架
    # ------------------------------------------------------------------
    def _build_columns(self) -> None:
        # 每列独立累计 y，短卡片之后立即接下一张，避免固定 220 px 步长留下大片空洞。
        self._grid_x = 0
        self._grid_y = 0
        self._column_y = [CANVAS_MARGIN] * COLUMN_COUNT

    def _clear_body(self) -> None:
        for frame in self._frames:
            frame.deleteLater()
        self._frames.clear()
        self._rows.clear()
        self._rows_by_signal.clear()
        self._editors.clear()
        self._grid_x = 0
        self._grid_y = 0
        self._column_y = [CANVAS_MARGIN] * COLUMN_COUNT

    @property
    def status_label(self) -> QLabel:
        return self.topbar.status_label

    @property
    def _columns(self) -> list[_ColumnView]:
        """返回自由画布的三个逻辑列，供布局检查和诊断使用。"""
        return [_ColumnView(self, column) for column in range(COLUMN_COUNT)]

    @property
    def all_off_button(self) -> QPushButton:
        return self.topbar.all_off_button

    def set_step_level(self, index: int) -> None:
        level = STEP_LEVELS[max(0, min(len(STEP_LEVELS) - 1, index))]
        self._step_level_index = STEP_LEVELS.index(level)
        for editor in self._editors:
            editor.set_level(level)

    def step_level(self) -> int:
        return STEP_LEVELS[getattr(self, "_step_level_index", 0)]

    def toggle_layout_lock(self, locked: bool) -> None:
        """锁定/解锁所有卡片拖拽。"""
        for frame in self._frames:
            frame.set_locked(locked)
        self.topbar.lock_button.setText("🔓 解锁布局" if locked else "🔒 锁定布局")

    # ------------------------------------------------------------------
    # 映射载入
    # ------------------------------------------------------------------
    def reload_mapping(self) -> None:
        self._mapping_loaded = False
        self._load_mapping()

    def _load_mapping(self) -> None:
        self._request = PvMappingRequestThread(
            instrument_api.instrument_base_url(), None
        )
        self._request.completed.connect(self._on_mapping)
        self._request.start()

    def _on_mapping(self, payload: dict) -> None:
        if not payload.get("ok"):
            self._mapping_loaded = False
            self.topbar.set_message(
                f"读取设备映射失败：{payload.get('message', '')}。请确认执行服务已启动"
                "（系统设置里检查仪器执行服务地址）。",
                "error",
            )
            self.all_off_button.setEnabled(False)
            return

        self._entries = list((payload.get("config") or {}).get("entries", []))
        self._build_rows()
        self._mapping_loaded = True
        writable = sum(1 for e in self._entries if e.get("writable"))
        self.topbar.set_message(
            f"{len(self._entries)} 路受控信号折成 {len(self._rows)} 个设备"
            f"（可写 {writable} 路）。",
            "good",
        )
        self.all_off_button.setEnabled(writable > 0)
        self._poll()

    def _build_rows(self) -> None:
        self._clear_body()
        groups: dict[str, list[dict]] = {}
        for entry in self._entries:
            groups.setdefault(str(entry.get("group") or "未分组"), []).append(entry)

        built = [(name, build_device_specs(entries)) for name, entries in groups.items()]
        topbar_entries = self._pick_topbar_entries(built)
        consumed = {str(e.get("signal") or "") for e in topbar_entries}
        self._topbar_signals = list(topbar_entries)
        self.topbar.set_readouts(topbar_entries)
        self._add_trend_card()

        assignment = assign_columns(built)
        for name, specs in built:
            if all(signal in consumed for signal in (s for spec in specs for s in spec.signals())):
                continue  # 整组都在顶栏大字里了，不再占画布
            self._add_panel(name, specs, assignment.get(name, _DEFAULT_COLUMN))

    def _add_trend_card(self, column: int = 1) -> None:
        """束流电流趋势卡片：放在**中列**最上面，可折叠/拖拽/缩放。

        放中列是因为高度：左列本来就有 4 张面板（约 564px），再加 244px 的趋势卡
        会超出窗口高度，一屏看不全；中列只有 DW 一张（约 460px），加完 714px 仍
        放得下。只有顶栏真的接到读数（1–3 路）时才建——没有束流信号时画空图没意义。
        """
        if not self._topbar_signals:
            self.trend_panel = None
            return
        panel = _TrendPanel(self)
        panel.set_readouts(self._topbar_signals)
        self.trend_panel = panel
        self._place_panel(
            panel, key="__trend__", column=column, height=_TrendPanel.HEIGHT
        )

    def reset_layout(self) -> None:
        """清掉所有卡片的位置/大小记忆，回到默认三列网格。

        拖乱了的兜底入口：存档只在放得进画布时才生效，但用户拖动后完全可能把卡片
        叠在一起或推到别处，得有一条"回到出厂布局"的路。
        """
        settings = layout_settings()
        for key in list(settings.allKeys()):
            if key.startswith(LAYOUT_SETTINGS_PREFIX):
                settings.remove(key)
        if self._entries:
            self._build_rows()
        self.topbar.set_message("已恢复默认卡片布局。", "good")

    def _pick_topbar_entries(self, built: Sequence[tuple[str, Sequence[DeviceSpec]]]) -> list[dict]:
        """顶栏大字读数：``_TOPBAR_NAMESPACES`` 里的只读量，太多就退回组面板。"""
        picked: list[dict] = []
        for _name, specs in built:
            for spec in specs:
                if spec.namespace not in _TOPBAR_NAMESPACES:
                    continue
                picked.extend(spec.readbacks)
        return picked if 0 < len(picked) <= TOPBAR_SLOTS else []

    def _add_panel(self, name: str, specs: Sequence[DeviceSpec], column: int) -> None:
        writable = sum(
            1 for spec in specs for entry in spec.controls if entry.get("writable")
        )
        hazard = any(
            kw in name or any(kw in spec.label for spec in specs)
            for kw in _GroupPanel.HAZARD_KEYWORDS
        )
        panel = _GroupPanel(name, f"{len(specs)} 个设备 · 可写 {writable} 路", hazard=hazard)
        panel.body.addWidget(_header_row(_slot_labels(specs)))
        for spec in specs:
            widget = _DeviceRow(spec, self)
            panel.body.addWidget(widget)
            self._rows.append(widget)
            self._editors.extend(widget.editors)
            for signal in spec.signals():
                self._rows_by_signal[signal] = widget
        # 包进可拖拽/resize 容器。默认几何按三列当前可用宽度计算，
        # 每列分别累计高度；用户移动后仍会保存到 layout-v2。
        self._place_panel(
            panel,
            key=name,
            column=column,
            height=max(104, 52 + sum(spec.row_height() for spec in specs)),
        )

    def _place_panel(
        self, panel: _GroupPanel, *, key: str, column: int, height: int
    ) -> _DraggableFrame:
        """把一张卡片放进画布的某一列，并推进该列的累计高度。

        注意末尾的 ``show()`` 不能省：卡片是画布的子控件，不显式显示就是不可见的
        （这一处漏掉过一次，结果是除趋势卡片外所有设备卡片都"消失"了）。
        """
        frame = _DraggableFrame(panel, key=key, parent=self._canvas)
        column = max(0, min(COLUMN_COUNT - 1, column))
        frame.setProperty("layoutColumn", column)
        available = max(
            MIN_HOLDER_WIDTH + 2 * CANVAS_MARGIN,
            self.scroll.viewport().width(),
        )
        column_width = max(
            COLUMN_MIN_WIDTH,
            (available - 2 * CANVAS_MARGIN - (COLUMN_COUNT - 1) * COLUMN_SPACING)
            // COLUMN_COUNT,
        )
        grid_x = CANVAS_MARGIN + column * (column_width + COLUMN_SPACING)
        frame.setGeometry(grid_x, self._column_y[column], column_width, height)
        # 存档只在放得进当前画布时才生效；否则沿用上面的默认网格位置
        frame.restore_geometry(self._canvas)
        frame.show()
        frame.raise_()
        self._frames.append(frame)
        self._column_y[column] = max(
            self._column_y[column], frame.y() + frame.height() + CARD_GAP
        )
        self._canvas.setMinimumHeight(max(640, max(self._column_y) + CANVAS_MARGIN))
        return frame

    # ------------------------------------------------------------------
    # 轮询与写入
    # ------------------------------------------------------------------
    def _poll(self) -> None:
        if self._read_in_flight or not self._mapping_loaded:
            return
        self._read_in_flight = True
        self._reader = instrument_api.request_read()
        self._reader.completed.connect(self._on_snapshot)

    def _on_snapshot(self, payload: dict) -> None:
        self._read_in_flight = False
        if not payload.get("ok"):
            self.topbar.set_message(f"读取失败：{payload.get('message', '')}", "error")
            return
        readings = instrument_api.readings_by_signal(payload.get("payload"))
        self._record_trend(readings)
        self.topbar.apply_readings(readings)
        for row in self._rows:
            row.apply_readings(readings)

    def _record_trend(self, readings: dict[str, dict]) -> None:
        """把顶栏那几路读数存进滚动缓冲。

        时间戳用服务端给的 ``received_time``——一次 128 路快照是 128 次串行 CA 读，
        用本地时钟会把读取耗时算进时间轴。读不到值就不写（缺口留白，不补 0）。
        """
        for entry in self._topbar_signals:
            signal = str(entry.get("signal") or "")
            reading = readings.get(signal) or {}
            if not reading.get("connected"):
                continue
            self.trend.push(signal, reading.get("value"), _parse_time(reading.get("received_time")))
        if self.trend_panel is not None:
            self.trend_panel.refresh()

    def trend_delta(self, signal: str) -> float | None:
        """相对 ``DELTA_WINDOW_S`` 秒前的变化量，顶栏显示用。"""
        return self.trend.delta(signal, DELTA_WINDOW_S)

    def trend_window(self, signal: str, seconds: float) -> list[tuple[float, float]]:
        return self.trend.window(signal, seconds)

    def write_signal(self, row: _DeviceRow, signal: str, value: float) -> None:
        """页面统一的写入口：串行下发，忙时明确告知而不是静默丢弃。"""
        thread = instrument_api.request_write(signal, value)
        if thread is None:
            self.topbar.set_message(instrument_api.write_busy_message(), "warn")
            return
        for other in self._rows:
            other.clear_rejection()
        self._pending = (row, signal, value)
        self.topbar.set_message(
            f"{row.quantity_label(signal)} · 下发中… {_fmt(value, row.spec.unit_of(signal))}",
            "warn",
        )
        thread.completed.connect(self._on_write_result)
        # 队列推进挂 finished 而不是 completed：completed 触发时写线程仍在运行，
        # 此刻发起下一次写入会被串行保护挡掉。finished 在 completed 之后发出。
        thread.finished.connect(self._pump_all_off)

    def _on_write_result(self, payload: dict) -> None:
        pending = self._pending
        self._pending = None
        if pending is None:
            return
        row, signal, _requested = pending
        unit = row.spec.unit_of(signal)
        quantity = row.quantity_label(signal)

        if not payload.get("ok"):
            reason = f"请求失败：{payload.get('message', '')}"
            row.mark_rejected(reason)
            self._count_all_off_rejection()
            self.topbar.set_message(f"{quantity} · {reason}", "error")
            return
        result = payload.get("payload") or {}
        if result.get("accepted"):
            detail = f"已下发 {_fmt(result.get('applied'), unit)}"
            if result.get("readback") is not None:
                detail += f" · 回读 {_fmt(result.get('readback'), unit)}"
            steps = len(result.get("ramp_steps") or [])
            if steps > 1:
                detail += f" · 斜坡 {steps} 步"
            # 设备状态未知是安全字段：写是发出去了，但执行层无法确认设备到底到没到，
            # 必须显性提示现场核对，不能混在"已下发"里当成功。
            if result.get("device_state_unknown"):
                message = f"{quantity} · {detail} · 但设备状态未知，请现场核对"
                row.mark_rejected(f"{detail}；设备状态未知，请现场核对")
                self.topbar.set_message(message, "error")
                return
            self.topbar.set_message(f"{quantity} · {detail}", "good")
        else:
            reason = f"被拒绝：{result.get('reason', '未知原因')}"
            row.mark_rejected(reason)
            self._count_all_off_rejection()
            self.topbar.set_message(f"{quantity} · {reason}", "error")

    def _count_all_off_rejection(self) -> None:
        """批量关断途中的被拒项要计数——「已下发完毕」不等于「都到位了」。"""
        if self._all_off_active:
            self._all_off_rejected += 1

    # ------------------------------------------------------------------
    # 全部关断
    # ------------------------------------------------------------------
    def confirm_all_off(self) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("确认全部关断")
        box.setText(
            "将按「先断输出、再退设定值」的顺序，把所有可写信号写回各自的安全值"
            "（下限或 0）。\n\n这会真实改动设备，请确认现场条件允许。"
        )
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.addButton("执行全部关断", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is cancel:
            return
        self.start_all_off()

    def start_all_off(self) -> None:
        queue = _all_off_plan(self._entries)
        if not queue:
            return
        self._all_off_queue = queue
        self._all_off_active = True
        self._all_off_rejected = 0
        self.all_off_button.setEnabled(False)
        self.topbar.set_message(f"全部关断：共 {len(queue)} 项，逐条下发中…", "warn")
        self._pump_all_off()

    def _pump_all_off(self) -> None:
        """逐条下发全部关断队列（写入串行，必须一条完成再发下一条）。

        这个函数也挂在**每一次**普通写入的 ``finished`` 上（用来推进队列），
        所以没有批量在跑时必须立刻返回——否则会把刚显示出来的下发结果覆盖成
        「全部关断已下发完毕」。
        """
        while self._all_off_queue:
            signal, value = self._all_off_queue.pop(0)
            row = self._rows_by_signal.get(signal)
            if row is None:
                continue
            thread = instrument_api.request_write(signal, value)
            if thread is None:
                # 上一次写入尚未收尾，放回队首等下一次 finished 再试
                self._all_off_queue.insert(0, (signal, value))
                return
            self._pending = (row, signal, value)
            thread.completed.connect(self._on_write_result)
            thread.finished.connect(self._pump_all_off)
            return

        if not self._all_off_active:
            return
        self._all_off_active = False
        self.all_off_button.setEnabled(True)
        if self._all_off_rejected:
            self.topbar.set_message(
                f"全部关断下发完毕，但 {self._all_off_rejected} 项被拒绝——"
                "设备未全部回到安全值，请逐条核对。",
                "error",
            )
        else:
            self.topbar.set_message("全部关断已下发完毕。", "good")

    # ------------------------------------------------------------------
    # 主窗口协议
    # ------------------------------------------------------------------
    def is_operation_active(self) -> bool:
        """全部关断序列进行中即视为有任务，退出时需确认。"""
        return bool(self._all_off_queue) or instrument_api.is_write_busy()

    def safe_stop(self) -> None:
        """退出前停止继续下发（已下发的那一条无法撤回，如实保留）。"""
        self._all_off_queue.clear()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._poll_timer.start()
        self._poll()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._poll_timer.stop()
        super().hideEvent(event)


PAGE_SPEC = PageSpec(
    key="manual",
    label="手动控制",
    icon="control",
    section="control",
    factory=ManualControlPage,
)
