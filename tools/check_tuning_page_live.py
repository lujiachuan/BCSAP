"""自动调束页的在线自检：用真实页面代码走通「建议 → 确认 → 写设备 → 记录」。

需要先起好模拟 IOC 与执行服务。offscreen 跑，不弹窗、不需要人工点击，
但走的是与真实窗口完全相同的页面代码路径。

重点验证两件事：
1. 候选必须**等人确认**才会写设备（第一版不允许连续自动写入）；
2. 每轮记录里建议值、实际下发值、实际回读值是分开的。

另外验证调束配置的保存与恢复（改造报告 §8.9 P2.3）：自检把配置写到 ``build/`` 下，
免得覆盖操作员本机真正保存的那一份。
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
# 自检用的调束配置落点：与操作员的真实配置分开（默认在 %LOCALAPPDATA% 下）
CONFIG_PATH = ROOT / "build" / "check_tuning_config.json"
os.environ["SPECTRUM_TUNING_CONFIG"] = str(CONFIG_PATH)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from apps.desktop_client import instrument_api  # noqa: E402
from apps.desktop_client.pages import tuning as tuning_module  # noqa: E402
from apps.desktop_client.pages.tuning import TuningPage  # noqa: E402

BASE = "http://127.0.0.1:8765"
MAGNET = "magnet.m1.current_setpoint"
_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""), flush=True)
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
    with urllib.request.urlopen(f"{BASE}/control/v1/tuning/runs/{run_id}", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def get_iterations(run_id: str) -> list[dict]:
    with urllib.request.urlopen(
        f"{BASE}/control/v1/tuning/runs/{run_id}/iterations", timeout=10
    ) as r:
        return json.loads(r.read().decode("utf-8"))["iterations"]


TERMINAL_TUNING_STATES = {"completed", "aborted", "failed", "recovery_required"}


def release_run(page) -> None:  # noqa: ANN001
    """把自检留下的调束任务收尾，释放设备组。

    **「等待确认」是占着设备的**（设备正处在半优化状态），所以自检中途失败退出时
    必须自己收尾：否则下一次自检或扫谱会被 ``设备组不可用`` 挡住，看起来像"服务坏了"。
    先停止，若落入 ``recovery_required`` 再人工确认一次。
    """
    run_id = getattr(page, "_run_id", None)
    if not run_id:
        return
    try:
        status = get_run(run_id)
    except Exception:  # noqa: BLE001  收尾路径不该再抛异常
        return
    if status.get("state") in TERMINAL_TUNING_STATES and not status.get("locked_groups"):
        return
    try:
        if status.get("state") not in TERMINAL_TUNING_STATES:
            instrument_api.request_tuning_stop(run_id)
        for _ in range(100):
            status = get_run(run_id)
            if status.get("state") in TERMINAL_TUNING_STATES:
                break
            time.sleep(0.05)
        if status.get("state") == "recovery_required":
            instrument_api.request_tuning_acknowledge(run_id, note="在线自检收尾")
        for _ in range(100):
            if not get_run(run_id).get("locked_groups"):
                break
            time.sleep(0.05)
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] 自检收尾未完成：{exc}", flush=True)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    page = TuningPage()
    # 页面的 _complain 会弹模态框等点击；offscreen 下没人点，会永久阻塞。
    # 这里改成记录，既避免挂死，也能把"为什么被拒"打印出来。
    complaints: list[str] = []
    page._complain = lambda message: complaints.append(message)  # type: ignore[method-assign]
    return run_checks(app, page, complaints)


def run_checks(app: QApplication, page: TuningPage, complaints: list[str]) -> int:
    try:
        return _checks(app, page, complaints)
    finally:
        # 自检无论怎么退出都要把任务收尾：等待确认中的任务一直占着设备组，
        # 会把后续的扫谱/调束都挡在"设备组不可用"外面。
        release_run(page)


def _checks(app: QApplication, page: TuningPage, complaints: list[str]) -> int:
    check("页面从执行服务载入可调参数",
          pump(app, lambda: bool(page._rows), 25),
          f"{len(page._rows)} 行")
    if not page._rows:
        print("  hint:", page.variable_hint.text(), flush=True)
        return 1
    check("目标下拉已填充", page.target.count() > 0,
          f"{page.target.count()} 项，当前 {page.target.currentData()}")
    check("可调参数里包含磁铁电流",
          any(r["entry"]["signal"] == MAGNET for r in page._rows))

    # 先配置好再启动（顺序反了会提交一个空选择）
    row = next(r for r in page._rows if r["entry"]["signal"] == MAGNET)
    row["check"].setCheckState(Qt.CheckState.Checked)
    row["low"].setValue(150.0)
    row["high"].setValue(200.0)
    page.iterations_spin.setValue(2)
    page.settle_spin.setValue(10.0)

    # 调束配置的保存与恢复（改造报告 §8.9 P2.3）：存到 build/ 下的自检文件，
    # 再新开一个页面，看界面是不是真的回到这一套。
    CONFIG_PATH.unlink(missing_ok=True)
    page.save_tuning_config()
    check("保存调束配置写出文件", CONFIG_PATH.exists(), str(CONFIG_PATH))
    saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    check("配置里记下了勾选与范围",
          any(v["signal"] == MAGNET and v.get("enabled") and v.get("low") == 150.0
              and v.get("high") == 200.0 for v in saved.get("variables") or []),
          f"max_iterations={saved.get('max_iterations')} "
          f"strategy={saved.get('strategy')}")
    fresh = TuningPage()
    fresh._complain = lambda message: complaints.append(message)  # type: ignore[method-assign]
    try:
        restored_rows = pump(app, lambda: bool(fresh._rows), 25)
        fresh_row = next(
            (r for r in fresh._rows if r["entry"]["signal"] == MAGNET), None
        )
        check("新开的页面载回了上次配置",
              restored_rows and fresh_row is not None
              and fresh_row["check"].checkState() == Qt.CheckState.Checked
              and fresh_row["low"].value() == 150.0
              and fresh_row["high"].value() == 200.0
              and fresh.iterations_spin.value() == 2
              and fresh.settle_spin.value() == 10.0,
              fresh.config_note.text()[:80])
        check("载入后界面说明了来源", "已载入调束配置" in fresh.config_note.text())
    finally:
        fresh.deleteLater()

    before = json.loads(
        urllib.request.urlopen(
            urllib.request.Request(
                BASE + "/control/v1/signals/read",
                data=json.dumps({"signals": [MAGNET]}).encode(),
                headers={"Content-Type": "application/json"},
            ),
            timeout=10,
        ).read().decode()
    )["readings"][0]["value"]

    # 界面不给模式开关：抓一下真实请求体，确认发出去的确实是 confirm
    captured: dict = {}
    original_start = instrument_api.request_tuning_start

    def spy(request: dict, base_url=None):
        captured.update(request)
        return original_start(request, base_url)

    instrument_api.request_tuning_start = spy  # type: ignore[assignment]
    try:
        page.start_tuning()
        check("没有触发参数校验拒绝", not complaints, "；".join(complaints))
        check("提交的模式固定为 confirm", captured.get("mode") == "confirm",
              str(captured.get("mode")))
        check("高级参数随任务下发（噪声/种子）",
              captured.get("noise") == page.noise_spin.value()
              and captured.get("seed") == page.seed_spin.value(),
              f"noise={captured.get('noise')} seed={captured.get('seed')}")
        check("启动后进入等待确认",
              pump(app, lambda: page._state == "awaiting_confirmation", 30),
              f"state={page._state}")
    finally:
        instrument_api.request_tuning_start = original_start  # type: ignore[assignment]

    run_id = page._run_id
    if not run_id:
        print("  启动未成功；若提示「已有调束任务在进行中」，"
              "说明上一次自检中途退出留下了未收尾的任务，重启执行服务即可。", flush=True)
        return 1

    # 关键：候选已生成，但设备必须还没被动过
    after = json.loads(
        urllib.request.urlopen(
            urllib.request.Request(
                BASE + "/control/v1/signals/read",
                data=json.dumps({"signals": [MAGNET]}).encode(),
                headers={"Content-Type": "application/json"},
            ),
            timeout=10,
        ).read().decode()
    )["readings"][0]["value"]
    check("候选生成后设备未被改动（确认制生效）", after is not None and after == before,
          f"{before} -> {after}")
    if after is None:
        print("  提示：读到 None，说明设备读数不可用，本次断言无意义", flush=True)
    check("界面显示了待确认候选", page._pending is not None,
          (page.proposal_label.text()[:60] + "…") if page.proposal_label.text() else "")
    check("确认按钮可用", page.approve_button.isEnabled())

    # 第一轮
    page._approve()
    check("确认后完成第 1 轮",
          pump(app, lambda: len(page._iterations) >= 1, 120),
          f"{len(page._iterations)} 轮")

    iterations = get_iterations(run_id)
    record = iterations[0]
    check("记录里建议值/下发值/回读值分开保存",
          all(k in record for k in ("proposed", "applied", "readback")),
          f"proposed={record['proposed'][MAGNET]:.2f} "
          f"applied={record['applied'][MAGNET]:.2f} "
          f"readback={record['readback'][MAGNET]:.2f}")
    check("实际下发值等于建议值（本次未触发斜坡拒绝）",
          abs(record["applied"][MAGNET] - record["proposed"][MAGNET]) < 1e-6)
    check("本轮有目标测量", record["objective"] is not None,
          f"objective={record['objective']}")

    # 第二轮 → 完成
    page._approve()
    check("第 2 轮后任务完成",
          pump(app, lambda: page._state == "completed", 120),
          f"state={page._state}")
    check("结果页已解禁并切换过去",
          page.tabs.isTabEnabled(2) and page.tabs.currentIndex() == 2)
    # 参数变化表由 _on_iterations 回填（tuning.py:632）；失败时把页内实际数据打出来，
    # 否则只看到"0 行"无法判断是页面没收到还是数据本身为空。
    detail = f"{page.changes_table.rowCount()} 行 / 页内轮次 {len(page._iterations)}"
    if page._iterations:
        first = page._iterations[0]
        detail += (f" / 首轮 keys={sorted(first)}"
                   f" readback={first.get('readback')}"
                   f" objective={first.get('objective')}")
    check("参数变化表已回填", page.changes_table.rowCount() > 0, detail)
    # 轮次明细是异步拉的，等它到齐再断言，否则是在跟请求赛跑
    check("收敛曲线拿到全部轮次",
          pump(app, lambda: len(page._iterations) >= 2, 20),
          f"{len(page._iterations)} 轮")
    check("轮次上限后不再活跃", not page.is_operation_active())

    status = get_run(run_id)
    check("算法版本与种子已记录",
          bool(status.get("algorithm")) and status.get("seed") is not None,
          f"{status.get('algorithm')}/{status.get('algorithm_version')} seed={status.get('seed')}")
    check("完成后不再占用设备组", status.get("locked_groups") == [],
          str(status.get("locked_groups")))

    # ---- 辅助分析：响应曲线 / 建议 / 日志（改造报告 §8.9 P2.4）----
    check("每轮都记下了所属阶段",
          all(it.get("stage") for it in get_iterations(run_id)),
          str([it.get("stage") for it in get_iterations(run_id)]))

    choices = [page.plot_choice.itemData(i) for i in range(page.plot_choice.count())]
    check("曲线下拉里有收敛 + 本次用到的变量",
          choices[:1] == [""] and MAGNET in choices, str(choices[:3]))

    index = page.plot_choice.findData(MAGNET)
    page.plot_choice.setCurrentIndex(index)
    xs, ys = page.tuning_plot.raw_data()
    check("切到该变量后画的是响应曲线（x = 实际回读）",
          index > 0 and len(xs) >= 2 and list(xs) == sorted(xs),
          f"x={[round(float(v), 2) for v in xs]} y={[round(float(v), 3) for v in ys]}")
    check("响应曲线写明了采样点数与最优点",
          "已采样 2 点" in page.response_note.text()
          and "最优目标" in page.response_note.text(),
          page.response_note.text()[:90])

    log_text = page.log_view.toPlainText()
    check("日志区一轮一行且四列齐全",
          "第 1 轮" in log_text and "第 2 轮" in log_text
          and "建议" in log_text and "下发" in log_text
          and "回读" in log_text and "共 2 轮" in log_text,
          log_text.splitlines()[1][:70] if len(log_text.splitlines()) > 1 else log_text[:70])

    export_path = CONFIG_PATH.with_name("check_tuning_log.jsonl")
    export_path.unlink(missing_ok=True)
    tuning_module.QFileDialog.getSaveFileName = staticmethod(  # type: ignore[assignment]
        lambda *a, **k: (str(export_path), "")
    )
    page.export_tuning_log()
    exported = (
        [json.loads(line) for line in export_path.read_text(encoding="utf-8").splitlines()]
        if export_path.exists()
        else []
    )
    check("导出日志写出 JSONL（meta + 每轮 + end）",
          bool(exported)
          and exported[0].get("event") == "meta"
          and exported[0].get("run_id") == run_id
          and exported[-1].get("event") == "end"
          and exported[-1].get("iterations") == 2,
          f"{len(exported)} 行，末行 state={exported[-1].get('state') if exported else '—'}")

    page.deleteLater()

    # 自检自己收尾，避免留下占用设备的任务让下一次自检撞 409
    if status.get("state") not in ("completed", "aborted", "failed", "recovery_required"):
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{BASE}/control/v1/tuning/runs/{run_id}/stop", data=b"{}",
                    headers={"Content-Type": "application/json"},
                ),
                timeout=10,
            )
            print("  （已请求停止残留任务）", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  （停止残留任务失败：{exc}）", flush=True)

    print()
    if _failures:
        print(f"失败 {len(_failures)} 项: {_failures}", flush=True)
        return 1
    print("自动调束页在线自检全部通过。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
