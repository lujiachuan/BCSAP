"""端到端联调自检：真实 Channel Access ↔ 本地模拟 IOC ↔ 执行层读写。

用法（先在另一个窗口起模拟 IOC）::

    D:\\EPICS\\base\\bin\\windows-x64-mingw\\softIoc.exe -d sim\\ioc.db
    .\\.venv\\Scripts\\python.exe tools\\check_signal_chain.py

与 ``sim_check.py`` 的区别：``sim_check`` 验的是「服务能起来、健康检查变绿」，
本脚本验的是**写入链路**——读快照、越界拒绝、斜坡分步，以及每一步之后
IOC 上的真实值到底有没有被改动。校验逻辑在内存网关下的覆盖见
``tests/test_signal_io.py``；这里补的是「真实 CA 也走同一条路」。

脚本**只在本机回环**上跑（``EPICS_CA_ADDR_LIST=127.0.0.1`` + 禁自动发现），
不会碰到现场束线 IOC。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 必须在导入 epics 之前钉死回环，杜绝误连现场 IOC
os.environ["EPICS_CA_ADDR_LIST"] = "127.0.0.1"
os.environ["EPICS_CA_AUTO_ADDR_LIST"] = "NO"

from apps.instrument_service import pv_mapping  # noqa: E402
from apps.instrument_service.runtime import InstrumentRuntime  # noqa: E402
from packages.contracts import SignalWriteRequest  # noqa: E402

SIGNAL = "gas.ar.flow_setpoint"

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        _failures.append(label)


def main() -> int:
    config = pv_mapping.default_config()
    runtime = InstrumentRuntime(config)  # 默认工厂 = 真实 ChannelAccessGateway
    try:
        signals = runtime.signals

        # ---- 1. 连通性：真实 CA 能不能读到模拟 IOC ----
        health = runtime.check_health()
        print(f"健康检查: {health.status}  "
              f"connected={health.summary.connected}/{health.summary.total}")
        check("真实 CA 可以连接模拟 IOC", health.summary.connected > 0,
              f"{health.summary.connected} 项已连接")
        if health.summary.connected == 0:
            first = health.items[0].detail if health.items else "无明细"
            print(f"  首项失败原因: {first}")
            return 1

        # ---- 2. 批量快照 ----
        snap = signals.read_snapshot([SIGNAL, "vacuum.chamber_pressure",
                                      "detector.fc1.beam_current"])
        for reading in snap.readings:
            print(f"  read {reading.signal:32s} = {reading.value} {reading.unit}")
        check("批量快照返回全部请求信号", len(snap.readings) == 3)
        check("快照读数均为已连接",
              all(r.connected and r.value is not None for r in snap.readings))

        # ---- 3. 正常写入并确认 IOC 真的变了 ----
        before = signals.read_value(SIGNAL)
        result = signals.write(SignalWriteRequest(signal=SIGNAL, value=120.0))
        after = signals.read_value(SIGNAL)
        check("区间内写入被受理", result.accepted, f"applied={result.applied}")
        check("IOC 上的值确实被改动", after == 120.0, f"{before} -> {after}")

        # ---- 4. 越界写入必须被拒且设备不动 ----
        result = signals.write(SignalWriteRequest(signal=SIGNAL, value=600.0))
        untouched = signals.read_value(SIGNAL)
        check("超过上限被拒绝", not result.accepted, result.reason or "")
        check("被拒后 IOC 值未变", untouched == 120.0, f"仍为 {untouched}")

        # ---- 5. 大变化按斜坡分步（max_step=50）----
        result = signals.write(SignalWriteRequest(signal=SIGNAL, value=320.0))
        check("大变化被拆成斜坡", result.accepted and len(result.ramp_steps) == 4,
              f"steps={result.ramp_steps}")
        check("斜坡末值即目标值", result.applied == 320.0, f"applied={result.applied}")
        check("IOC 最终值 = 目标值", signals.read_value(SIGNAL) == 320.0)

        # ---- 6. 只读信号不可写 ----
        result = signals.write(
            SignalWriteRequest(signal="detector.fc1.beam_current", value=1.0)
        )
        check("只读信号被拒绝", not result.accepted, result.reason or "")

        # ---- 7. 复位，避免给后续联调留脏值 ----
        signals.write(SignalWriteRequest(signal=SIGNAL, value=0.0))
        print(f"  已复位 {SIGNAL} = {signals.read_value(SIGNAL)}")
    finally:
        runtime.close()

    print()
    if _failures:
        print(f"失败 {len(_failures)} 项: {_failures}")
        return 1
    print("链路自检全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
