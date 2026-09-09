"""开发用工具：离屏渲染客户端主要页面并保存截图（不弹出窗口）。

用法（在仓库根目录）::

    python tools/ui_snapshot.py [--out docs/ui-review-snapshots]

输出浅色 / 深色两套主要页面截图，供 UI/UX 审查与改版前后对比。工具会
屏蔽启动时的真实服务检查（_start_initialization 置空），并用合成数据
模拟「扫谱进行中 / 调束监控 / 调束结果」等状态，不触碰用户 QSettings。

只读截图：本工具不会调用窗口 closeEvent，因此不写注册表偏好。
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

import apps.desktop_client.main as main_module  # noqa: E402
from apps.desktop_client.pages import (  # noqa: E402
    ScanPage,
    TuningPage,
)
from apps.desktop_client.theme import apply_theme, theme_name  # noqa: E402

# 屏蔽真实初始化：截图过程不访问 127.0.0.1:8000 / 8765。
main_module.MainWindow._start_initialization = lambda self: None  # type: ignore[method-assign]

_NAV_ROW = {
    "workbench": 1,
    "scan": 3,
    "tuning": 4,
    "library": 6,
    "settings": 9,
}


def build_window(app: QApplication, theme: str) -> main_module.MainWindow:
    apply_theme(app, theme)
    window = main_module.MainWindow()
    window.show()
    app.processEvents()
    return window


def goto(app: QApplication, window: main_module.MainWindow, row: int) -> None:
    window.navigation.setCurrentRow(row)
    app.processEvents()


def snap(app: QApplication, window: QWidget, out: Path, name: str) -> None:
    app.processEvents()
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


def fill_scan_running(app: QApplication, window: main_module.MainWindow) -> None:
    scan = window.pages.findChild(ScanPage)
    x_values = [10.0 + index * 0.01 for index in range(9_000)]
    y_values = [scan._signal(value) for value in x_values]
    cursor = int(len(x_values) * 0.62)
    scan.plot.set_data(x_values[:cursor], y_values[:cursor])
    scan.progress.setValue(62)
    scan._set_scan_state("扫描进行中")
    scan.current_label.setText(f"当前 m/z：{x_values[cursor - 1]:.2f}")
    scan.intensity_label.setText(f"当前强度：{y_values[cursor - 1]:.1f}")
    peak_index = max(range(cursor), key=y_values.__getitem__)
    scan.peak_label.setText(f"最大峰：{x_values[peak_index]:.2f}")
    scan.start_button.setEnabled(False)
    scan.pause_button.setEnabled(True)
    scan.stop_button.setEnabled(True)
    goto(app, window, _NAV_ROW["scan"])


def fill_tuning(app: QApplication, window: main_module.MainWindow) -> None:
    tuning = window.pages.findChild(TuningPage)
    tuning._target_iterations = 40

    # 监控页（tab 1）
    values = []
    for iteration in range(1, 25):
        trend = 8.31 + 10.9 * (1 - math.exp(-iteration / 10))
        values.append(
            trend + 0.7 * math.sin(iteration * 1.7) + random.uniform(-0.25, 0.25)
        )
    tuning.tuning_plot.set_data(list(range(1, 25)), values)
    best = max(values)
    tuning.current_card.value_label.setText(f"{values[-1]:.2f} μA")
    tuning.best_card.value_label.setText(f"{best:.2f} μA")
    tuning.gain_card.value_label.setText(f"+{(best / 8.31 - 1) * 100:.1f}%")
    tuning.iteration_label.setText("第 24 / 40 次迭代")
    tuning.tuning_progress.setValue(60)
    tuning.tuning_state.setText("正在优化")
    tuning.tuning_stop_button.setEnabled(True)
    tuning.tabs.setTabEnabled(0, False)
    tuning.tabs.setTabEnabled(1, True)
    tuning.tabs.setTabEnabled(2, False)
    tuning.tabs.setCurrentIndex(1)
    goto(app, window, _NAV_ROW["tuning"])
    snap(app, window, SNAP_DIR, f"{theme_name()}_04_tuning-monitor")

    # 结果页（tab 2）
    tuning.result_notice.setText("优化正常完成 · 无安全告警（模拟）")
    tuning.result_detail.setText("最佳结果出现在第 35 次迭代")
    tuning.result_card.value_label.setText("19.08 μA")
    if tuning.result_card.detail_label is not None:
        tuning.result_card.detail_label.setText("提升 129.6%")
    tuning.apply_status.setText("尚未应用结果")
    tuning.reviewed_checkbox.setChecked(True)
    tuning.tabs.setTabEnabled(0, True)
    tuning.tabs.setTabEnabled(2, True)
    tuning.tabs.setCurrentIndex(2)
    app.processEvents()


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
    goto(app, window, _NAV_ROW["workbench"])
    snap(app, window, SNAP_DIR, f"{theme}_01_workbench")

    # 2) 扫谱（进行中）
    fill_scan_running(app, window)
    snap(app, window, SNAP_DIR, f"{theme}_02_scan-running")

    # 3) 自动调束：参数配置
    tuning = window.pages.findChild(TuningPage)
    tuning.tabs.setCurrentIndex(0)
    goto(app, window, _NAV_ROW["tuning"])
    snap(app, window, SNAP_DIR, f"{theme}_03_tuning-config")

    # 4) 自动调束：运行监控 / 5) 结果确认
    fill_tuning(app, window)
    snap(app, window, SNAP_DIR, f"{theme}_05_tuning-result")

    # 6) 谱图库（占位页）
    goto(app, window, _NAV_ROW["library"])
    snap(app, window, SNAP_DIR, f"{theme}_06_library")

    # 7) 系统设置：服务连接 / 8) 外观
    settings = window._settings_page
    if settings is not None:
        settings.data_url.setText("http://127.0.0.1:8000")
        settings.instrument_url.setText("http://127.0.0.1:8765")
        settings._set_feedback("good", "两个服务均响应正常。")
        settings.settings_stack.setCurrentIndex(0)
        goto(app, window, _NAV_ROW["settings"])
        snap(app, window, SNAP_DIR, f"{theme}_07_settings-service")
        settings.settings_stack.setCurrentIndex(3)
        app.processEvents()
        snap(app, window, SNAP_DIR, f"{theme}_08_settings-appearance")

    window.hide()
    window.deleteLater()
    app.processEvents()


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

    capture_theme(app, "light")
    capture_theme(app, "dark")
    print(f"done -> {SNAP_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
