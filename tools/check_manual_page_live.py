"""手动控制页的在线自检：用真实页面代码走通「界面 → 执行服务 → CA → IOC」。

与 ``check_signal_chain.py`` 的分工：那个脚本验执行服务的读写端点，
本脚本验**客户端页面本身**——三列版式、行控件构建、快照刷新、滚轮调值、
写入口、拒绝原因回显、开/关按钮状态同步。两者都要求先起好模拟 IOC 与执行服务。

用法::

    D:\\EPICS\\base\\bin\\windows-x64-mingw\\softIoc.exe -d sim\\ioc.db
    .\\.venv\\Scripts\\python.exe -m apps.instrument_service.main
    .\\.venv\\Scripts\\python.exe tools\\check_manual_page_live.py

用 offscreen 平台跑：不弹窗、不需要人工点击，但走的是与真实窗口完全相同的
页面代码路径。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json  # noqa: E402
import urllib.request  # noqa: E402

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from apps.desktop_client.pages import manual  # noqa: E402

BASE = "http://127.0.0.1:8765"
SIGNAL = "gas.ar.flow_setpoint"
SWITCH = "hv_array.dw01.switch"
# 现场最小屏；页面按它的满窗尺寸布局（侧栏 168 + 页面边距 36）
WINDOW_W, WINDOW_H = 1600, 1000
SIDEBAR_W = 168

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def read_ioc(signal: str) -> float | None:
    """直接问执行服务要设备当前值，绕开页面自身状态，确保是真实读数。"""
    body = json.dumps({"signals": [signal]}).encode()
    request = urllib.request.Request(
        BASE + "/control/v1/signals/read",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload["readings"][0]["value"]


def grouped(entries: list[dict]) -> dict[str, list[dict]]:
    """按设备组切分映射条目，与页面 ``_build_rows`` 的口径一致。"""
    result: dict[str, list[dict]] = {}
    for item in entries:
        result.setdefault(str(item.get("group") or "未分组"), []).append(item)
    return result


def pump(app: QApplication, predicate, timeout: float = 30.0) -> bool:
    """转 Qt 事件循环直到 predicate 为真或超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.05)
    app.processEvents()
    return predicate()


def wheel_to(spin, ticks: int) -> None:
    """往设定框上滚一格，验证滚轮调值真的生效。"""
    event = QWheelEvent(
        QPointF(5.0, 5.0),
        QPointF(5.0, 5.0),
        QPoint(0, 0),
        QPoint(0, 120 * ticks),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    spin.wheelEvent(event)


def main() -> int:
    app = QApplication.instance() or QApplication([])

    page = manual.ManualControlPage()
    # 按现场最小屏（1600x1000）的满窗尺寸布局：视口太小的话，几何类断言
    # （每列高度、列宽）量的是个不存在的窗口，等于没测。
    page.setFixedSize(WINDOW_W - SIDEBAR_W - 36, WINDOW_H - 150)
    page.show()  # 不 show 的话滚动区不会布局，视口尺寸量出来是假的
    app.processEvents()

    if not pump(app, lambda: page._mapping_loaded, timeout=25):
        print("页面未能载入设备映射：", page.status_label.text())
        return 1
    check("页面从执行服务载入映射", True, page.status_label.text())

    def message() -> str:
        return page.status_label.text()

    def submit(row, signal: str, value: float) -> None:
        """提交一次写入，并把消息区先清空。

        不清空的话 ``pump`` 可能被**上一次**留下的文案立刻满足（"已下发" / "被拒绝"
        都还在），于是后面的断言实际是在看旧结果。实测踩到过一次：越界写入之后紧接着
        的斜坡写入断言偶发失败——两次写入的结果与消息区是异步先后到达的，
        断言点落在哪个中间态取决于调度。
        """
        page.topbar.set_message("", "idle")
        page.write_signal(row, signal, value)

    def settled_not_error(timeout: float = 10.0) -> bool:
        """等这一行不再是"被拒标红"。

        断言写得比这更窄会偶发误报：回读一旦进容差，这一行会显示 ``good``（≈ 已稳定），
        而"还没到位"时是 ``warn``/空——三种都是**标红已清掉**的正常结局。
        实测：写 320 之后有时那一帧正好读到 320，状态就是 ``good``。
        """
        return pump(
            app,
            lambda: row.readback_label.property("state") != "error",
            timeout,
        )

    # ---- 版式：单页三列，不按设备组分页签 ----
    panels = [
        sum(1 for i in range(column.count()) if column.itemAt(i).widget() is not None)
        for column in page._columns
    ]
    check("单页三列、没有设备组页签",
          len(page._columns) == 3 and not hasattr(page, "group_tabs") and sum(panels) > 0,
          f"三列面板数 {panels}")

    # 卡片建出来不等于看得见：漏调 show() 时它们只是"存在但不可见"，
    # 光数控件数量是发现不了的（这里踩过一次）。
    hidden = [f._key for f in page._frames if f.isHidden()]
    check("所有卡片都真的显示出来了",
          not hidden,
          f"{len(page._frames)} 张" if not hidden else f"没显示：{hidden}")

    # 顶栏吃掉的分组不再占画布，其余每组都该有一张卡片；
    # 另有趋势卡片与磁铁成组卡片这两个"非分组卡片"
    group_count = len({str(e.get("group") or "未分组") for e in page._entries})
    expected_cards = (
        group_count
        - (1 if page.topbar.readouts else 0)
        + (1 if page.trend_panel is not None else 0)
        + (1 if page.magnet_group_panel is not None else 0)
    )
    check("卡片数与分组数对得上",
          len(page._frames) == expected_cards,
          f"{len(page._frames)} 张 / 期望 {expected_cards}"
          f"（共 {group_count} 组，顶栏收走 {len(page.topbar.readouts)} 路）")
    check("成组设定卡片存在（磁铁 1+2 / 3+4 / 1~4）",
          page.magnet_group_panel is not None
          and page.magnet_group_panel.group_combo.count() == 3,
          str(
            page.magnet_group_panel.group_combo.count()
            if page.magnet_group_panel is not None
            else None
          ))

    # 每列的默认高度要放得进一屏，否则"少了一张卡片"其实是滚下去了
    column_heights: dict[int, int] = {}
    for frame in page._frames:
        column = int(frame.property("layoutColumn") or 0)
        column_heights[column] = max(
            column_heights.get(column, 0), frame.y() + frame.height()
        )
    viewport_height = page.scroll.viewport().height()
    over = {c: h for c, h in column_heights.items() if h > viewport_height}
    check("每列默认高度都放得进一屏",
          not over,
          f"列高 {column_heights} / 视口 {viewport_height}px"
          + (f"，超出：{over}" if over else ""))

    check("顶栏有恢复布局入口",
          page.topbar.reset_layout_button.isEnabled(),
          page.topbar.reset_layout_button.toolTip())

    # ---- 折行：一行一个设备 ----
    target_devices = sum(
        len(manual.build_device_specs(entries)) for entries in grouped(page._entries).values()
    )
    check("按设备折行（一行一个设备）",
          0 < len(page._rows) <= target_devices < len(page._entries),
          f"{len(page._entries)} 路信号 → {len(page._rows)} 行 / "
          f"{target_devices} 个设备")

    row = page._rows_by_signal.get(SIGNAL)
    check("目标行存在", isinstance(row, manual._DeviceRow))
    if row is None:
        return 1

    check("该行把设定与它配对的回读放在一起",
          [str(e.get("signal")) for e in row.spec.readbacks] == ["gas.ar.flow_readback"],
          str([str(e.get("signal")) for e in row.spec.readbacks]))
    check("该行一个设定槽；气路模式是多档量不是开/关",
          len(row.editors) == 1 and not row.switches and len(row.modes) == 1,
          f"{len(row.editors)} 槽 / {len(row.switches)} 开关 / {len(row.modes)} 模式框")

    # ---- 快照刷新：回读值应显示出来 ----
    page._read_in_flight = False
    page._poll()
    pump(app, lambda: "sccm" in row.readback_label.text(), timeout=15)
    check("回读值与 IOC 一致", "sccm" in row.readback_label.text(), row.readback_label.text())

    editor = row.editors[0]
    check("设定框基准步长按量程推出",
          editor.base_step == 1.0,
          f"基准步长 {editor.base_step:g}")

    # ---- 滚轮调值：只改数字，不下发 ----
    before = editor.spin.value()
    wheel_to(editor.spin, 1)
    check("滚轮改设定值但不下发",
          editor.spin.value() == before + editor.spin.singleStep(),
          f"{before} → {editor.spin.value()}")

    # ---- 步进档在卡片上（×1/×10/×100），且真的改到设定框步长 ----
    card = row.parentWidget()
    while card is not None and not isinstance(card, manual._GroupPanel):
        card = card.parentWidget()
    if card is None or not card.step_buttons:
        check("该行所在卡片带步进档按钮", False)
    else:
        check("该行所在卡片带三档步进按钮",
              [b.text() for b in card.step_buttons] == ["×1", "×10", "×100"],
              f"{[b.text() for b in card.step_buttons]}")
        card.set_step_level(1)
        check("卡片 ×10 档把设定框步长放大十倍",
              editor.spin.singleStep() == editor.base_step * 10,
              f"{editor.spin.singleStep():g}")
        # 三档必须真的按 10 倍递进：旧基准步长取量程 1/50，×100 = 200% 量程，
        # 一格必然被端点夹住，看起来和 ×10 一样（现场反馈的「比例不对」）。
        editor.spin.setValue(250.0)  # 0-500 sccm 的中点
        deltas = []
        for index in range(len(card.step_buttons)):
            card.set_step_level(index)
            was = editor.spin.value()
            wheel_to(editor.spin, 1)
            deltas.append(editor.spin.value() - was)
            editor.spin.setValue(250.0)
        check("三档滚轮增量严格成 10 倍（1 / 10 / 100 sccm）",
              deltas == [editor.base_step * level for level in manual.STEP_LEVELS],
              f"实测增量 {[f'{d:g}' for d in deltas]}")
        card.set_step_level(0)
    editor.spin.setValue(0.0)

    # ---- 正常写入 ----
    submit(row, SIGNAL, 120.0)
    pump(app, lambda: "已下发" in message() or "拒绝" in message())
    check("页面提交写入后显示成功", "已下发" in message(), message())
    check("IOC 真的变成了 120", read_ioc(SIGNAL) == 120.0, str(read_ioc(SIGNAL)))

    # ---- 越界写入：回显服务端原因，且设备不动 ----
    submit(row, SIGNAL, 600.0)
    pump(app, lambda: "拒绝" in message() or "已下发" in message())
    check("越界写入回显拒绝原因（带设备 + 量名）",
          "被拒绝" in message() and "上限" in message() and "流量设定" in message(),
          message())
    check("被拒的行回读标红", row.readback_label.property("state") == "error",
          str(row.readback_label.property("state")))
    check("被拒后 IOC 未变", read_ioc(SIGNAL) == 120.0, str(read_ioc(SIGNAL)))

    # ---- 斜坡：大变化应被拆步并标注步数 ----
    submit(row, SIGNAL, 320.0)
    pump(app, lambda: "斜坡" in message() or "拒绝" in message())
    check("大变化按斜坡分步并在消息区标注步数", "斜坡" in message(), message())
    check("斜坡后 IOC = 320", read_ioc(SIGNAL) == 320.0, str(read_ioc(SIGNAL)))
    # 标红由"重新下发"清掉：清掉之后这一行可能是 good（≈ 已稳定）/ warn（≠ 还没到位）
    # / 空，**只要不是 error 就说明标红没了**
    settled_not_error()
    check("新一次下发清掉上一次的标红", row.readback_label.property("state") != "error",
          str(row.readback_label.property("state")))

    # ---- 趋势：快照推进滚动缓冲，趋势卡片跟着动 ----
    trend_panel = page.trend_panel
    if trend_panel is None:
        check("有束流读数时应建出趋势卡片", False, "trend_panel 为空")
    else:
        check("趋势卡片绑定了顶栏读数",
              [e["signal"] for e in trend_panel.signals]
              == [e["signal"] for e, _ in page.topbar.readouts],
              str([e["signal"] for e in trend_panel.signals]))
        check("默认窗口 2 分钟", trend_panel.window_s == 120.0,
              f"{trend_panel.window_s:g}s")
        trend_frame = next((f for f in page._frames if f._key == "__trend__"), None)
        check("趋势卡片放在中列",
              trend_frame is not None
              and int(trend_frame.property("layoutColumn") or 0) == 1,
              f"列 {trend_frame.property('layoutColumn') if trend_frame else '无'}")
        check("顶栏每路读数各有一条 sparkline",
              len(page.topbar.readout_sparks) == len(page.topbar.readouts),
              f"{len(page.topbar.readout_sparks)} 条")
        signal0 = str(trend_panel.signals[0].get("signal"))
        before_points = page.trend.signal_points(signal0)
        for _ in range(3):
            page._read_in_flight = False
            page._poll()
            pump(app, lambda: not page._read_in_flight, timeout=15)
        after_points = page.trend.signal_points(signal0)
        check("每次快照都给趋势缓冲添了点",
              after_points > before_points,
              f"{before_points} → {after_points} 点")
        values = [v for _t, v in page.trend.window(signal0, 120.0)]
        check("缓冲里存的是真读数（不是补的 0）",
              bool(values) and any(v != 0 for v in values),
              f"{values[-3:] if values else values}")
        check("统计行有内容", "σ" in trend_panel.stats_label.text(),
              trend_panel.stats_label.text()[:60])
        check("顶栏读数固定两位小数",
              all(len(v.text().split(" ")[0].split(".")[-1]) == 2
                  for _e, v in page.topbar.readouts
                  if "." in v.text()),
              str([v.text() for _e, v in page.topbar.readouts]))

    # ---- 开/关按钮真的能操作输出信号 ----
    switch_row = page._rows_by_signal.get(SWITCH)
    if switch_row is None:
        check("能找到一路高压输出开关", False)
    elif not switch_row.switches:
        check("高压输出开关渲染成开/关按钮", False, "该行没有开关按钮")
    else:
        _signal, on, off = switch_row.switches[0]
        on.click()
        pump(app, lambda: "已下发" in message() or "拒绝" in message())
        check("点「开」把输出开关写成 1", read_ioc(SWITCH) == 1.0, str(read_ioc(SWITCH)))
        # 按钮选中态是**按回读值**刷新的（每个轮询周期一次），点完立刻断言会撞上"回读还没回来"
        # 那一帧。等它跟随到位（或服务端明确拒绝）再断言。
        followed = pump(
            app,
            lambda: (on.isChecked() and not off.isChecked()) or "拒绝" in message(),
            10,
        )
        check("按钮选中态跟随实际回读", followed,
              f"开={on.isChecked()} 关={off.isChecked()} msg={message()[:40]!r}")
        off.click()
        pump(app, lambda: "已下发" in message() or "拒绝" in message())
        print(f"  已复位 {SWITCH} = {read_ioc(SWITCH)}")

    # ---- 全部关断序列的规划（不真正下发，避免牵连其它信号）----
    plan = manual._all_off_plan(page._entries)
    roles = {
        entry["signal"]: entry.get("role")
        for entry in page._entries
        if entry.get("writable")
    }
    first_setpoint = next(
        (i for i, (signal, _) in enumerate(plan) if roles.get(signal) == "setpoint"),
        len(plan),
    )
    toggles_after = [
        signal for signal, _ in plan[first_setpoint:] if roles.get(signal) == "toggle"
    ]
    check("全部关断：输出类信号先于设定值下发", toggles_after == [],
          f"{len(plan)} 项，首个设定值在第 {first_setpoint} 位")
    check("全部关断不含脉冲信号",
          all(roles.get(signal) != "pulse" for signal, _ in plan))

    # ---- 复位 ----
    page.write_signal(row, SIGNAL, 0.0)
    pump(app, lambda: "已下发" in message())
    print(f"  已复位 {SIGNAL} = {read_ioc(SIGNAL)}")

    page.deleteLater()
    print()
    if _failures:
        print(f"失败 {len(_failures)} 项: {_failures}")
        return 1
    print("手动控制页在线自检全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
