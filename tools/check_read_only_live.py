"""只读部署模式自检：确认这台执行服务**真的**拒了所有写入，而读取照常。

用法（先在另一个窗口用只读参数起服务，端口与在线实例分开，免得干扰现场）::

    $env:SPECTRUM_READ_ONLY = "1"
    $env:EPICS_CA_ADDR_LIST = "127.0.0.1"     # 本地联调：只连模拟 IOC
    $env:EPICS_CA_AUTO_ADDR_LIST = "NO"
    .\\.venv\\Scripts\\python.exe -X utf8 -c `
        "import uvicorn; uvicorn.run('apps.instrument_service.app:app', ``
         host='127.0.0.1', port=8767, log_level='warning')"

    .\\.venv\\Scripts\\python.exe tools\\check_read_only_live.py            # 默认 8767
    .\\.venv\\Scripts\\python.exe tools\\check_read_only_live.py http://127.0.0.1:8765

为什么要有这个自检：界面把按钮变灰只是"界面不撒谎"，真正的强制点在执行层。现场
想知道"这台机器到底能不能写"，只能在真实服务上问一遍——本脚本就是这么做的：状态接口
报只读、单点/成组/回落三种写入全部被拒且**设备值一点没动**、扫谱与调束启动即 400、
PV 映射保存也 400（映射决定"谁能写、写到多少"），同时读取链路（状态/映射/信号）保持可用。

单元与界面覆盖见 ``tests/test_read_only_mode.py``（31 例）。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8767"
READ_ONLY_MARK = "全局只读模式"
MAGNET_GROUP = "磁铁电源"

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def request(base: str, path: str, payload: dict | None = None, method: str | None = None):
    """返回 ``(status_code, body)``；4xx 不当异常抛，交给调用方判语义。"""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    verb = method or ("POST" if data is not None else "GET")
    req = urllib.request.Request(
        base + path, data=data, method=verb, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15.0) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"detail": raw}


def detail_of(body: dict) -> str:
    detail = body.get("detail")
    if isinstance(detail, dict):
        return json.dumps(detail, ensure_ascii=False)
    return str(detail or body.get("message") or "")


def pick_signals(entries: list[dict]) -> tuple[list[str], str, str]:
    """从真实映射里挑一组自检信号。

    优先**磁铁**：成组下发与回落本来就是给它们的，用别的设备自检等于没验到那条路。
    映射里没有磁铁时才退回"任一带回读的可写信号"，让脚本在别的现场也能跑起来。
    """
    magnets = [
        e
        for e in entries
        if e.get("writable") and e.get("readback_signal") and e.get("group") == MAGNET_GROUP
    ]
    candidates = magnets or [
        e for e in entries if e.get("writable") and e.get("readback_signal")
    ]
    if not candidates:
        raise SystemExit("映射里没有带回读的可写信号，无法自检")
    target = next(
        (e for e in entries if e.get("beam_target")),
        next((e for e in entries if not e.get("writable")), candidates[0]),
    )
    setpoints = [str(e["signal"]) for e in candidates[:4]]
    return setpoints, str(candidates[0]["readback_signal"]), str(target["signal"])


def read_value(base: str, signal: str) -> float | None:
    status, body = request(base, "/control/v1/signals/read", {"signals": [signal]})
    if status != 200:
        return None
    readings = body.get("readings") or []
    return None if not readings else readings[0].get("value")


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE).rstrip("/")
    print(f"只读自检目标：{base}\n")

    # ---- 1. 状态接口先自报家门 ----
    status, body = request(base, "/control/v1/status")
    check("状态接口可达", status == 200, f"HTTP {status}")
    if status != 200:
        print("  服务不可达：请先用 SPECTRUM_READ_ONLY=1 起执行服务。")
        return 1
    check("状态接口报告全局只读", bool(body.get("read_only")), body.get("detail", ""))
    if not body.get("read_only"):
        print("\n  这个实例**不是**只读实例：不要用它做只读结论，请换端口重起。")
        return 1

    # ---- 2. 读取链路必须照常可用 ----
    status, mapping = request(base, "/control/v1/pv-mapping")
    check("只读不影响读映射", status == 200 and bool(mapping.get("entries")),
          f"HTTP {status}，{len(mapping.get('entries') or [])} 条")
    if status != 200:
        return 1
    setpoints, readback, beam = pick_signals(mapping["entries"])
    before = read_value(base, readback)
    check("只读不影响读信号", before is not None, f"{readback} = {before}")
    print(f"  自检信号：{'、'.join(setpoints)} / 回读 {readback} / 目标 {beam}\n")

    # ---- 3. 单点写入：拒绝 + 设备不动 ----
    status, body = request(
        base, "/control/v1/signals/write", {"signal": setpoints[0], "value": 100.0}
    )
    check("单点写入被拒", status == 200 and not body.get("accepted"),
          f"HTTP {status} {body.get('reason', '')}")
    check("拒绝理由说明是只读", READ_ONLY_MARK in str(body.get("reason", "")))
    check("单点被拒后设备值未变", read_value(base, readback) == before,
          f"仍为 {read_value(base, readback)}")

    # ---- 4. 成组写入：整批一次都不写 ----
    status, body = request(
        base,
        "/control/v1/signals/write-batch",
        {"writes": [{"signal": s, "value": 120.0} for s in setpoints], "atomic": True},
    )
    check(f"成组写入整批未下发（{len(setpoints)} 路）",
          status == 200 and body.get("nothing_written") is True,
          f"accepted={body.get('accepted')} rejected={body.get('rejected')}")
    check("成组被拒后设备值未变", read_value(base, readback) == before)

    # ---- 5. 回落：安全动作也一样拒，但要说清一路都没退 ----
    status, body = request(
        base,
        "/control/v1/magnets/retract",
        {"setpoint_signals": setpoints, "current_a": 0.0, "rate_a_s": 2.0,
         "timeout_s": 5.0},
    )
    check("回落未报成功", status == 200 and body.get("ok") is False, f"HTTP {status}")
    check("回落理由说明是只读", READ_ONLY_MARK in str(body.get("message", "")),
          str(body.get("message", ""))[:90])
    check("回落被拒后设备值未变", read_value(base, readback) == before)

    # ---- 6. 扫谱 / 调束 / 映射保存：启动阶段就 400 ----
    scan = {
        "axis": {"label": "只读自检", "setpoint_signals": setpoints,
                 "readback_signal": readback},
        "detector_signal": beam, "start": 0.0, "stop": 20.0, "step": 5.0,
    }
    status, body = request(base, "/control/v1/scan/runs", scan)
    check("扫谱启动即 400", status == 400 and READ_ONLY_MARK in detail_of(body),
          f"HTTP {status} {detail_of(body)[:90]}")

    tuning = {
        "target_signal": beam,
        "variables": [{"signal": s, "low": 0.0, "high": 100.0} for s in setpoints],
        "mode": "confirm",
    }
    status, body = request(base, "/control/v1/tuning/runs", tuning)
    check("调束启动即 400", status == 400 and READ_ONLY_MARK in detail_of(body),
          f"HTTP {status} {detail_of(body)[:90]}")

    status, body = request(base, "/control/v1/pv-mapping", mapping, method="PUT")
    check("保存 PV 映射即 400", status == 400 and READ_ONLY_MARK in detail_of(body),
          f"HTTP {status} {detail_of(body)[:90]}")

    print()
    if _failures:
        print(f"失败 {len(_failures)} 项: {_failures}")
        return 1
    print("只读自检全部通过：所有写入被拒，设备值未变，读取链路正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
