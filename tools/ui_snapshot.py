"""开发用工具：离屏渲染客户端主要页面并保存截图（不弹出窗口）。

用法（在仓库根目录）::

    python tools/ui_snapshot.py [--out docs/ui-review-snapshots] [--tag m5]

输出浅色 / 深色两套主要页面截图，供 UI/UX 审查与改版前后对比。工具会：

* 屏蔽启动时的真实服务检查（``_start_initialization`` 置空），并把客户端的联网入口
  （读信号、拉映射、拉调束目录）换成**不会完成的假线程**，再用合成数据把页面填成
  「扫谱进行中 / 调束监控 / 调束结果 / 手动控制」等状态——同一份输入每次都渲染出同一张图，
  且不碰现场的任何服务与设备；
* 用 ``SPECTRUM_TUNING_CONFIG`` 把调束配置指到临时目录：否则调束页会自动载入
  操作员本机保存的那一份配置，截图就不再可复现了。

只读截图：本工具不会调用窗口 closeEvent，因此不写注册表偏好。
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 截图必须可复现：把调束配置的读取落点挪出用户目录（在导入客户端模块之前设好）
os.environ["SPECTRUM_TUNING_CONFIG"] = str(
    Path(tempfile.gettempdir()) / "spectrum-ui-snapshot-tuning.json"
)

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import apps.desktop_client.main as main_module  # noqa: E402
from apps.desktop_client import instrument_api  # noqa: E402
from apps.desktop_client.pages import (  # noqa: E402
    ManualControlPage,
    ScanPage,
    TuningPage,
)
from apps.desktop_client.pages import manual as manual_module  # noqa: E402
from apps.desktop_client.pages import settings as settings_module  # noqa: E402
from apps.desktop_client.pages import tuning as tuning_module  # noqa: E402
from apps.desktop_client.theme import apply_theme, theme_name  # noqa: E402

# 屏蔽真实初始化：截图过程不访问 127.0.0.1:8000 / 8765。
main_module.MainWindow._start_initialization = lambda self: None  # type: ignore[method-assign]


class _NeverFinishes(QObject):
    """假请求线程：connect 之后永远不回调，页面停在"已发出、还没回来"的状态。

    截图要的是**确定的画面**：真发请求会让画面取决于现场服务当时的状态。
    """

    completed = Signal(object)
    finished = Signal()

    def start(self) -> None:
        return None

    def isRunning(self) -> bool:
        return False

    def deleteLater(self) -> None:  # noqa: N802  覆盖 QObject 的同名方法
        # 页面在“请求释放”时会调它；这里不真删，免得页面还握着引用时对象就没了
        return None


def fake_thread(*_args, **_kwargs) -> _NeverFinishes:
    return _NeverFinishes()


def block_network() -> None:
    """把客户端所有联网入口换成假线程（在创建页面之前调用）。"""
    for name in (
        "request_read",
        "request_scan_start",
        "request_scan_status",
        "request_scan_points",
        "request_tuning_catalog",
        "request_tuning_status",
        "request_tuning_iterations",
        "request_magnet_retract",
        "request_pv_health",
    ):
        if hasattr(instrument_api, name):
            setattr(instrument_api, name, fake_thread)
    for module in (manual_module, tuning_module, settings_module):
        module.PvMappingRequestThread = fake_thread  # type: ignore[assignment]




def build_window(app: QApplication, theme: str) -> main_module.MainWindow:
    apply_theme(app, theme)
    window = main_module.MainWindow()
    window.show()
    app.processEvents()
    return window


def goto(app: QApplication, window: main_module.MainWindow, page_key: str) -> None:
    """切到某一页。

    用 ``_navigate_to``（页面注册表的 key → 侧栏行）而不是写死的行号：侧栏后来又加了
    「样品管理 / 谱图分析 / 任务与同步」，写死的行号会把截图悄悄拍到**别的页面**上
    （实测：调束的两张图拍成了同一个页面，字节完全一致才发现）。
    """
    window._navigate_to(page_key)
    app.processEvents()


def snap(
    app: QApplication,
    window: main_module.MainWindow,
    out: Path,
    name: str,
    *,
    page_key: str | None = None,
) -> None:
    """抓一张图；给了 ``page_key`` 就**先核对当前页**再抓。

    拍错页面比没有截图更糟：看图的人会以为那一页长这样。实测踩过——侧栏行号写死，
    两次截图都拍在扫谱页上，两张 PNG 字节完全一致才发现。
    """
    app.processEvents()
    if page_key is not None:
        row = window._row_of_key.get(page_key)
        expected = window._page_rows.get(row) if row is not None else None
        if expected is not None and window.pages.currentIndex() != expected:
            raise SystemExit(
                f"{name}: 期望拍「{page_key}」页，当前却是第 "
                f"{window.pages.currentIndex()} 页（期望第 {expected} 页）"
            )
    window.grab().save(str(out / f"{name}.png"))
    print(f"saved {name}.png")


def mark_ready(window: main_module.MainWindow) -> None:
    """模拟全部服务就绪的启动后状态。"""
    window._model.set_service("data", "good", "正常")
    window._model.set_service("instrument", "good", "就绪")
    window._model.set_service("epics", "good", "12 / 12")
    window._model.set_service("cache", "good", "已同步")
    window._sync_service_views()
    window._on_essential_ready()


def fill_initialization(window: main_module.MainWindow) -> None:
    page = window.initialization_page
    page.set_step("config", "good", "本机配置已加载")
    page.set_step("instrument", "good", "仪器执行服务可达")
    page.set_step("pv", "good", "12 / 12 PV 已连接")
    page.set_step("data", "good", "正常")
    page.set_step("sync", "running", "正在下载谱图 12 / 40")
    page.set_sync_progress(12, 40)


def spectrum_peaks(x_values: list[float]) -> list[float]:
    """合成一张质谱：几个高斯峰 + 一点噪声（只是为了让曲线看起来像现场数据）。

    以前这里调的是扫谱页里的一个内部合成函数；那个函数随"平台自己算谱"的写法一起
    没了，截图工具不该依赖页面内部的假数据生成器——它只要一份**形状像样**的数据。
    """
    peaks = ((10.06, 0.03, 92.0), (10.12, 0.02, 41.0), (10.21, 0.035, 63.0))
    rng = random.Random(20260914)
    values = []
    for x in x_values:
        height = sum(
            amplitude * math.exp(-((x - center) ** 2) / (2 * width**2))
            for center, width, amplitude in peaks
        )
        values.append(height + rng.uniform(0.0, 0.8))
    return values


def fill_scan_running(app: QApplication, window: main_module.MainWindow) -> None:
    scan = window.pages.findChild(ScanPage)
    x_values = [10.0 + index * 0.01 for index in range(9_000)]
    y_values = spectrum_peaks(x_values)
    cursor = int(len(x_values) * 0.62)
    scan.plot.set_data(x_values[:cursor], y_values[:cursor])
    scan.plot.annotate_peaks(3)
    scan.progress.setValue(62)
    scan.state_label.setText("扫描进行中 · 已完成 62%")
    scan.current_label.setText(f"当前 I：{x_values[cursor - 1]:.3f} A")
    scan.intensity_label.setText(f"当前强度：{y_values[cursor - 1]:.3f} nA")
    peak_index = max(range(cursor), key=y_values.__getitem__)
    scan.peak_label.setText(f"最大峰：{x_values[peak_index]:.3f} A")
    scan.start_button.setEnabled(False)
    scan.stop_button.setEnabled(True)
    goto(app, window, "scan")


def tuning_mapping() -> list[dict]:
    """截图用的调束变量（形状与设备档案一致：可调标记 + 范围 + 最大单步）。"""
    entries = [
        {
            "signal": "magnet.m1.current_setpoint",
            "label": "磁铁1 电流设定",
            "pv": "BD:DipoleMagnet:01:CurrentSet",
            "unit": "A",
            "writable": True,
            "required": False,
            "group": "磁铁电源",
            "role": "setpoint",
            "readback_signal": "magnet.m1.current_readback",
            "min_value": 0.0,
            "max_value": 600.0,
            "max_step": 20.0,
            "settle_tol": 0.5,
            "tunable": True,
        },
        {
            "signal": "ion_optics.focus.voltage_setpoint",
            "label": "聚焦电压设定",
            "pv": "Part1:JM_POWER:03:SET_VOL",
            "unit": "V",
            "writable": True,
            "required": False,
            "group": "聚焦电源",
            "role": "setpoint",
            "readback_signal": "ion_optics.focus.voltage_readback",
            "min_value": 0.0,
            "max_value": 4000.0,
            "max_step": 200.0,
            "settle_tol": 5.0,
            "tunable": True,
        },
        {
            "signal": "detector.fc1.beam_current",
            "label": "FC1 束流电流",
            "pv": "BD:FC:01:BeamCurrent",
            "unit": "nA",
            "writable": False,
            "required": True,
            "group": "束流探测",
            "role": "readback",
            "beam_target": True,
        },
    ]
    return entries


def tuning_catalog() -> dict:
    return {
        "targets": [
            {
                "signal": "detector.fc1.beam_current",
                "label": "FC1 束流电流",
                "unit": "nA",
                "group": "束流探测",
                "stage": "detector",
            }
        ],
        "variables": [
            {
                "signal": "magnet.m1.current_setpoint",
                "label": "磁铁1 电流设定",
                "unit": "A",
                "group": "磁铁电源",
                "stage": "magnet",
                "low": 0.0,
                "high": 600.0,
                "max_step": 20.0,
            },
            {
                "signal": "ion_optics.focus.voltage_setpoint",
                "label": "聚焦电压设定",
                "unit": "V",
                "group": "聚焦电源",
                "stage": "focus",
                "low": 0.0,
                "high": 4000.0,
                "max_step": 200.0,
            },
        ],
        "stages": [
            {"key": "magnet", "label": "磁铁电源", "groups": ["磁铁电源"]},
            {"key": "focus", "label": "聚焦电源", "groups": ["聚焦电源"]},
            {"key": "detector", "label": "束流探测", "groups": ["束流探测"]},
        ],
        "upstream": {
            "detector.fc1.beam_current": [
                "magnet.m1.current_setpoint",
                "ion_optics.focus.voltage_setpoint",
            ]
        },
        "linked_sets": [],
        "excluded": {},
    }


def tuning_iterations(count: int = 24) -> list[dict]:
    """合成的轮次记录：形状与 `/tuning/runs/{id}/iterations` 一致。"""
    records: list[dict] = []
    rng = random.Random(20260914)
    for index in range(count):
        trend = 8.31 + 10.9 * (1 - math.exp(-(index + 1) / 10))
        objective = trend + 0.7 * math.sin(index * 1.7) + rng.uniform(-0.25, 0.25)
        magnet = 180.0 + 12.0 * math.sin(index / 3.0)
        focus = 2200.0 + 260.0 * math.cos(index / 4.0)
        values = {
            "magnet.m1.current_setpoint": round(magnet, 2),
            "ion_optics.focus.voltage_setpoint": round(focus, 1),
        }
        records.append(
            {
                "iteration": index,
                "proposed": values,
                "applied": values,
                "readback": values,
                "target": round(objective, 3),
                "objective": round(objective, 3),
                "quality": "ok",
                "detail": None,
                "at": f"2026-09-14T00:{index:02d}:00+00:00",
                "stage": "joint",
            }
        )
    return records


def fill_tuning(app: QApplication, window: main_module.MainWindow) -> None:
    tuning = window.pages.findChild(TuningPage)
    tuning._on_mapping({"ok": True, "config": {"entries": tuning_mapping()}})
    tuning._on_catalog({"ok": True, "payload": tuning_catalog()})
    for row in tuning._rows:
        row["check"].setCheckState(tuning_module.Qt.CheckState.Checked)

    records = tuning_iterations()
    tuning._apply_status(
        {
            "state": "running",
            "run_id": "snap-run",
            "strategy": "joint",
            "stage": "joint",
            "completed_iterations": len(records),
            "max_iterations": 40,
            "snapshot": {
                "magnet.m1.current_setpoint": 175.0,
                "ion_optics.focus.voltage_setpoint": 2123.0,
            },
            "baseline_objective": 8.31,
            "algorithm": "gp-ei",
            "algorithm_version": "gp-ei/1.0",
            "seed": 0,
        }
    )
    tuning._on_iterations({"ok": True, "payload": {"iterations": records}})
    tuning.tabs.setTabEnabled(1, True)
    tuning.tabs.setTabEnabled(2, False)
    tuning.tabs.setCurrentIndex(1)
    tuning.tuning_stop_button.setEnabled(True)
    goto(app, window, "tuning")
    snap(app, window, SNAP_DIR, f"{theme_name()}_04_tuning-monitor")

    # 结果页（tab 2）：终态 + 三种设备处置可用
    tuning._apply_status(
        {
            "state": "completed",
            "run_id": "snap-run",
            "strategy": "joint",
            "stage": "joint",
            "completed_iterations": len(records),
            "max_iterations": 40,
            "snapshot": {
                "magnet.m1.current_setpoint": 175.0,
                "ion_optics.focus.voltage_setpoint": 2123.0,
            },
            "baseline_objective": 8.31,
            "algorithm": "gp-ei",
            "algorithm_version": "gp-ei/1.0",
            "seed": 0,
        }
    )
    tuning._on_iterations({"ok": True, "payload": {"iterations": records}})
    tuning.tabs.setTabEnabled(0, True)
    tuning.tabs.setTabEnabled(2, True)
    tuning.tabs.setCurrentIndex(2)
    app.processEvents()


def fill_manual(app: QApplication, window: main_module.MainWindow) -> None:
    """手动控制页：用真实映射（128 路）铺满卡片，再喂一份合成回读。

    这一页过去没有截图回归（`docs/手动控制页目标电流趋势与界面优化调研.md` 的 O7），
    而它恰好是控件最多、布局最容易退化的一页。
    """
    from apps.instrument_service import pv_mapping

    page = window.pages.findChild(ManualControlPage)
    entries = [entry.model_dump() for entry in pv_mapping.default_config().entries]
    page._on_mapping({"ok": True, "config": {"entries": entries}})
    readings = {}
    for index, entry in enumerate(entries):
        value = 12.5 + (index % 7) * 3.25
        readings[str(entry["signal"])] = {
            "signal": str(entry["signal"]),
            "value": value,
            "unit": entry.get("unit", ""),
            "connected": True,
            "readback": value,
        }
    page._on_snapshot({"ok": True, "payload": {"readings": list(readings.values())}})
    page.topbar.set_message(
        f"{len(entries)} 路受控信号折成 {len(page._rows)} 个设备（模拟数据）。", "good"
    )
    goto(app, window, "manual")


SNAP_DIR = Path("docs/ui-review-snapshots")


def capture_theme(app: QApplication, theme: str) -> None:
    window = build_window(app, theme)

    # 0) 初始化页（服务检查进行中）
    window.pages.setCurrentIndex(window._initialization_index)
    fill_initialization(window)
    snap(app, window, SNAP_DIR, f"{theme}_00_initialization")

    # 就绪后
    mark_ready(window)

    # 1) 工作台
    goto(app, window, "workbench")
    snap(app, window, SNAP_DIR, f"{theme}_01_workbench", page_key="workbench")

    # 2) 扫谱（进行中）
    fill_scan_running(app, window)
    snap(app, window, SNAP_DIR, f"{theme}_02_scan-running", page_key="scan")

    # 3) 手动控制（卡片最多的一页，过去没有截图回归）
    fill_manual(app, window)
    snap(app, window, SNAP_DIR, f"{theme}_09_manual", page_key="manual")

    # 4) 自动调束：参数配置
    tuning = window.pages.findChild(TuningPage)
    tuning.tabs.setCurrentIndex(0)
    goto(app, window, "tuning")
    snap(app, window, SNAP_DIR, f"{theme}_03_tuning-config", page_key="tuning")

    # 5) 自动调束：运行监控 / 6) 结果确认
    fill_tuning(app, window)
    snap(app, window, SNAP_DIR, f"{theme}_05_tuning-result", page_key="tuning")

    # 7) 谱图库（占位页）
    goto(app, window, "library")
    snap(app, window, SNAP_DIR, f"{theme}_06_library", page_key="library")

    # 8) 系统设置：服务连接 / 9) 外观
    settings = window._settings_page
    if settings is not None:
        settings.data_url.setText("http://127.0.0.1:8000")
        settings.instrument_url.setText("http://127.0.0.1:8765")
        settings._set_feedback("good", "两个服务均响应正常。")
        settings.settings_stack.setCurrentIndex(0)
        goto(app, window, "settings")
        snap(app, window, SNAP_DIR, f"{theme}_07_settings-service", page_key="settings")
        settings.settings_stack.setCurrentIndex(3)
        app.processEvents()
        snap(app, window, SNAP_DIR, f"{theme}_08_settings-appearance", page_key="settings")

    window.hide()
    window.deleteLater()
    app.processEvents()


def report_duplicates(out: Path) -> int:
    """两张截图完全一样通常意味着"拍错了页面"，而不是"两个页面长得一样"。"""
    import hashlib

    seen: dict[str, list[str]] = {}
    for path in sorted(out.glob("*.png")):
        digest = hashlib.md5(path.read_bytes()).hexdigest()
        seen.setdefault(digest, []).append(path.name)
    duplicates = [names for names in seen.values() if len(names) > 1]
    for names in duplicates:
        print(f"  [warn] 这些截图字节完全一致，检查是不是拍到了同一页：{names}")
    if not duplicates:
        print(f"  [ok] {len(seen)} 张截图互不相同")
    return len(duplicates)


def main() -> int:
    global SNAP_DIR  # noqa: PLW0603
    parser = argparse.ArgumentParser(description="离屏渲染客户端页面截图")
    parser.add_argument("--out", default="docs/ui-review-snapshots", help="输出目录")
    parser.add_argument("--tag", default="", help="子目录标识（如 m0），便于改版前后对比")
    args = parser.parse_args()
    SNAP_DIR = REPO_ROOT / args.out
    if args.tag:
        SNAP_DIR = SNAP_DIR / args.tag
    SNAP_DIR.mkdir(parents=True, exist_ok=True)

    app = QApplication(sys.argv)
    app.setApplicationName("谱图与束流控制平台")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    block_network()

    capture_theme(app, "light")
    capture_theme(app, "dark")
    report_duplicates(SNAP_DIR)
    print(f"done -> {SNAP_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
