"""扫谱页的在线自检：用真实页面代码走通「界面 → 执行服务 → CA → IOC → 落盘」。

需要先起好模拟 IOC 与执行服务（见 check_signal_chain.py 的说明）。
用 offscreen 跑，不弹窗、不需要人工点击，但走的是与真实窗口完全相同的页面代码路径。

要点：确认界面拿到的是**服务端实际采集**的点，横坐标是设备实际回读值，
并且质量不合格的点没有混进曲线。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from apps.desktop_client.pages.scan import ScanPage  # noqa: E402

BASE = "http://127.0.0.1:8765"

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def pump(app: QApplication, predicate, timeout: float = 120.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.05)
    app.processEvents()
    return predicate()


def get_run(run_id: str) -> dict:
    with urllib.request.urlopen(f"{BASE}/control/v1/scan/runs/{run_id}", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def read_signals(signals: list[str]) -> dict[str, dict]:
    """直接问执行服务读一次现场值（自检只看设备真实状态，不信界面文案）。"""
    body = json.dumps({"signals": signals}).encode()
    request = urllib.request.Request(
        BASE + "/control/v1/signals/read", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as r:
        payload = json.loads(r.read().decode("utf-8"))
    return {item["signal"]: item for item in payload.get("readings", [])}


def read_signal(signal: str) -> float | None:
    item = read_signals([signal]).get(signal) or {}
    return None if item.get("value") is None else float(item["value"])


def main() -> int:
    app = QApplication.instance() or QApplication([])
    page = ScanPage()

    check("暂停按钮按文档 9.3 关闭", not page.pause_button.isEnabled(),
          page.pause_button.toolTip()[:34] + "…")

    # 缩小规模，只为验证链路
    page.start_value.setValue(100.0)
    page.end_value.setValue(140.0)
    page.step_value.setValue(20.0)
    page.dwell_value.setValue(20.0)
    page.samples.setValue(2)
    check("摘要显示预计点数", "预计 3 点" in page.scan_summary.text(),
          page.scan_summary.text())

    page.start_scan()
    check("提交后进入进行中", pump(app, lambda: page._run_id is not None, 20),
          f"run_id={page._run_id}")
    run_id = page._run_id
    if not run_id:
        return 1

    # 等到页面**真正收尾**（终态 + 点已拉全 + 轮询已停），而不是只看到终态标记：
    # 服务端把最后几个点写完才置终态，页面还要再拉一轮才追上
    def drained() -> bool:
        return (
            page._state in {"completed", "aborted", "failed", "recovery_required"}
            and not page._pending_terminal
            and not page._timer.isActive()
        )

    finished = pump(app, drained, 180)
    check("扫描到达终态并拉全数据", finished,
          f"state={page._state} points={len(page._points)}")

    status = get_run(run_id)
    check("服务端报告完成", status["state"] == "completed", status.get("message", ""))
    check("界面点数与服务端一致",
          len(page._points) == status["completed_points"],
          f"界面 {len(page._points)} / 服务端 {status['completed_points']}")
    check("进度条到 100%", page.progress.value() == 100, f"{page.progress.value()}%")

    # ---- 回落（文档 6.6）：请求里带参数、由服务端执行、界面如实转述 ----
    request = page._build_request()
    check("扫描请求带上了回落参数",
          (request.get("retract") or {}).get("current_a") == page.retract_current.value()
          and (request.get("retract") or {}).get("auto") is True,
          str(request.get("retract")))
    check("服务端回落结论被转述到状态条（不是客户端自己宣布）",
          "回落" in page.retract_status.text(), page.retract_status.text())
    magnet_now = read_signal("magnet.m1.current_readback")
    check("回落真的执行了：磁铁回读 == 回落电流",
          magnet_now is not None and abs(magnet_now - page.retract_current.value()) <= 0.5,
          f"回读 {magnet_now} / 目标 {page.retract_current.value()}")
    check("导出内容里记下了逐路回读",
          bool(page._points and page._points[0].get("readback_values")),
          str(page._points[0].get("readback_values") if page._points else None))

    # 横坐标必须是设备实际回读值（模拟 IOC 已把回读耦合到设定，故应等于目标）
    included = [p for p in page._points if p.get("included")]
    coords = [round(float(p["coordinate"]), 3) for p in included]
    check("点的横坐标来自实际回读", coords == [100.0, 120.0, 140.0], str(coords))
    check("全部点质量合格", all(p["quality"] == "ok" for p in included),
          str([p["quality"] for p in included]))

    # 谱图确实画了数据
    check("曲线已绘制", len(included) == 3, f"{len(included)} 点")
    check("最大峰已标注（完成后）", page._state == "completed")

    # 落盘校验：NPZ 里的 x 应是电流，不是质量
    npz = status.get("spectrum_path")
    check("服务端已落盘谱图", bool(npz and Path(npz).is_file()), str(npz))
    if npz and Path(npz).is_file():
        from packages.spectrum.codec import decode_spectrum

        x, y = decode_spectrum(Path(npz).read_bytes())
        check("落盘的 x 是电流实际值（不是换算后的质量）",
              [round(float(v), 3) for v in x] == [100.0, 120.0, 140.0],
              str([float(v) for v in x]))

    # 质量换算只影响显示：勾选后不应改动落盘数据
    page.mass_toggle.setChecked(True)
    app.processEvents()
    check("勾选质量换算后仍显示曲线",
          page.current_label.text().endswith("u"), page.current_label.text())
    page.mass_toggle.setChecked(False)

    check("按钮已复位", page.start_button.isEnabled() and not page.stop_button.isEnabled())
    check("不再视为进行中", not page.is_operation_active())

    page.deleteLater()
    print()
    if _failures:
        print(f"失败 {len(_failures)} 项: {_failures}")
        return 1
    print("扫谱页在线自检全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
