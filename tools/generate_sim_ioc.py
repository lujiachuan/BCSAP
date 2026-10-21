"""根据当前设备配置生成 ``sim/ioc.db``。

用法::

    .\\.venv\\Scripts\\python.exe tools\\generate_sim_ioc.py

**为什么是生成的**：``sim/ioc.db`` 必须与执行服务的默认设备配置逐条对应，
手写两份迟早漂移（旧文件的注释里就留过「应用尚未强制参数上限」这类过时说明）。
改了 ``apps/instrument_service/device_profiles.py`` 就重跑本脚本。

设计取舍：**故意不设 DRVH/DRVL**。若模拟 IOC 自己就夹住越界值，联调时无法
分辨「越界值是被应用的执行层拦下的」还是「被 IOC 夹掉的」——而执行层校验
正是要验证的对象。因此这里保持宽容，由应用负责拒绝。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.instrument_service import device_profiles  # noqa: E402

HEADER = """\
# Simulated beamline IOC for local development and wiring checks.
#
# GENERATED FILE -- do not edit by hand.
# Regenerate with:  .\\.venv\\Scripts\\python.exe tools\\generate_sim_ioc.py
# Source of truth:  apps/instrument_service/device_profiles.py
#   (CLUSTER_SOURCE_ENTRIES = application default mapping,
#    SIM_ONLY_ENTRIES      = real-PV ledger extras covered by the sim IOC only)
#
# The PV set mirrors the full demo ledger (demo/argon_tuning/argon_tuning/):
# gas / sputter / vacuum / ion optics / DW array / BD high voltage / magnets /
# Faraday cups from the PV ledger, plus the MSScan & MS:Scanz records deployed
# on the mass-scan IOC. Pointing the service at this IOC turns every health-check
# item green and every ledger PV addressable via caget/caput without editing config.
#
# Record types model the device honestly:
#   ao    = writable setpoint   (mapping entry writable=True)
#   ai    = read-only reading   (mapping entry writable=False)
#   calc  = readback that mirrors its setpoint (so the simulated device
#           actually follows what you write; without this every scan point
#           would be judged "not settled" and the scan flow would be
#           impossible to exercise locally)
#
# DRVH/DRVL are deliberately NOT set: if this IOC clamped out-of-range writes,
# a wiring test could not tell whether a bad value was stopped by the
# application's execution layer or by the IOC. The application must reject it.
#
# Comments are kept ASCII on purpose: EPICS dbLoadRecords is not guaranteed to
# handle non-ASCII bytes. Chinese labels live in device_profiles.py.
"""


# 束流物理输出：signal -> (峰值, [(输入信号, 最优值, sigma), ...])。
# 生成 calc 记录：各维高斯衰减取几何平均（EXP(-0.5*sum(((x-best)/sigma)^2)/n)），
# 让软 IOC 的 FC1 随 DW 电压变化，可直接拿真实 CA 链路跑自动调束。
# 参数与 packages/epics_adapter/simulated.py 的 _BEST/_SIGMA/_PEAK_BEAM 对齐。
PHYSICAL_OUTPUTS: dict[str, tuple[float, list[tuple[str, float, float]]]] = {
    "detector.fc1.beam_current": (
        12.0,
        [
            ("hv_array.dw01.voltage_setpoint", 100.0, 75.0),
            ("hv_array.dw02.voltage_setpoint", 1500.0, 1000.0),
            ("hv_array.dw03.voltage_setpoint", 1500.0, 1000.0),
            ("hv_array.dw04.voltage_setpoint", 3000.0, 2000.0),
        ],
    ),
}
_INP_LETTERS = "ABCDEFGHIJKL"


def _num(value: float) -> str:
    """EPICS 友好的数值字面量：避免 %g 产生 1e-05 这类指数写法。"""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _validate_no_overlap(mapped, sim_only) -> None:
    """默认映射与台账补充不允许出现同名 PV；宁可在生成期炸掉也不要写出坏 IOC。"""
    mapped_pvs = {e.pv for e in mapped}
    extra_pvs = {e.pv for e in sim_only}
    overlap = sorted(mapped_pvs & extra_pvs)
    if overlap:
        raise SystemExit(f"台账补充 PV 与默认映射重复：{overlap}")
    if len(extra_pvs) != len(sim_only):
        raise SystemExit("SIM_ONLY_ENTRIES 内部存在重复 PV 名，请检查 device_profiles.py")


def main() -> int:
    mapped = device_profiles.CLUSTER_SOURCE_ENTRIES
    sim_only = device_profiles.SIM_ONLY_ENTRIES
    _validate_no_overlap(mapped, sim_only)
    entries = [*mapped, *sim_only]
    lines: list[str] = [HEADER]

    # 「设定 → 回读」配对：回读侧改用 calc 记录镜像设定值。
    # 否则模拟装置的回读永远不动，扫谱的每个点都会判定为「未稳定」——
    # 逻辑上没错，但模拟 IOC 就完全没法用来开发扫描流程了。
    mirror_of = {
        entry.readback_signal: entry.signal
        for entry in entries
        if entry.readback_signal
    }

    pv_by_signal = {e.signal: e.pv for e in entries}
    for entry in entries:
        unit = entry.unit if entry.unit.isascii() else ""
        physical = PHYSICAL_OUTPUTS.get(entry.signal)
        if physical is not None:
            peak, inputs = physical
            n = len(inputs)
            # calc 的 CALC 字段上限 40 字符，放不下完整 4 维高斯，
            # 拆成「每维归一化偏差 aux calc」+「主 calc 汇总」两步。
            aux_pvs: list[str] = []
            for i, (src_signal, best, sigma) in enumerate(inputs):
                src_pv = pv_by_signal[src_signal]
                aux_pv = f"{entry.pv}:n{i + 1}"
                aux_pvs.append(aux_pv)
                lines.append(f"# --- {entry.signal} aux {i + 1} | normalized offset^2 ---")
                lines.append(f'record(calc, "{aux_pv}") {{')
                lines.append(f'    field(DESC, "{entry.signal} aux {i + 1}")')
                lines.append(f'    field(INPA, "{src_pv} CP")')
                lines.append(
                    f'    field(CALC, "((A-{_num(best)})/{_num(sigma)})^2")'
                )
                lines.append('    field(SCAN, "1 second")')
                lines.append('    field(PREC, "6")')
                lines.append("}")
                lines.append("")
            letters = _INP_LETTERS[:n]
            sum_expr = "+".join(letters)
            lines.append(f"# --- {entry.signal} | physical beam model ---")
            lines.append(f'record(calc, "{entry.pv}") {{')
            lines.append(f'    field(DESC, "{entry.signal}")')
            for i, aux_pv in enumerate(aux_pvs):
                letter = _INP_LETTERS[i]
                lines.append(f'    field(INP{letter}, "{aux_pv} CP")')
            # 几何平均：PEAK * exp(-0.5 * sum(offset^2) / n)
            lines.append(
                f'    field(CALC, "{_num(peak)}*EXP(-.5*({sum_expr})/{n})")'
            )
            # CP 输入 + I/O Intr：任一 DW 变化经 aux 即时重算
            lines.append('    field(SCAN, "1 second")')
            if unit:
                lines.append(f'    field(EGU,  "{unit}")')
            lines.append('    field(PREC, "6")')
            lines.append("}")
            lines.append("")
            continue
        mirror = mirror_of.get(entry.signal)
        if mirror is not None and not entry.writable:
            source = next(e.pv for e in entries if e.signal == mirror)
            lines.append(f"# --- {entry.signal} | mirrors {mirror} ---")
            lines.append(f'record(calc, "{entry.pv}") {{')
            lines.append(f'    field(DESC, "{entry.signal}")')
            # CP（Channel Process）：被链接的设定记录一处理，本记录就重算。
            # 用 PP 不行——PP 是反方向（本记录处理时去处理目标），
            # 结果是回读永远停在初始值，扫描的每个点都会被判「未稳定」。
            lines.append(f'    field(INPA, "{source} CP")')
            lines.append('    field(CALC, "A")')
            if unit:
                lines.append(f'    field(EGU,  "{unit}")')
            lines.append('    field(PREC, "6")')
            lines.append("}")
            lines.append("")
            continue

        record = "ao" if entry.writable else "ai"
        value = device_profiles.SIMULATED_VALUES.get(entry.signal, 0.0)
        bounds = ""
        if entry.writable and (entry.min_value is not None or entry.max_value is not None):
            lo = "" if entry.min_value is None else _num(entry.min_value)
            hi = "" if entry.max_value is None else _num(entry.max_value)
            bounds = f" allowed {lo}..{hi}"

        lines.append(
            f"# --- {entry.signal} | "
            f"{'writable' if entry.writable else 'read-only'}{bounds} ---"
        )
        lines.append(f'record({record}, "{entry.pv}") {{')
        lines.append(f'    field(DESC, "{entry.signal}")')
        lines.append(f'    field(VAL,  "{_num(value)}")')
        if unit:
            lines.append(f'    field(EGU,  "{unit}")')
        lines.append('    field(PREC, "6")')
        lines.append("}")
        lines.append("")

    target = ROOT / "sim" / "ioc.db"
    payload = "\n".join(lines)
    # dbLoadRecords 不保证处理非 ASCII，宁可在生成期炸掉也不要写出坏 IOC 文件
    non_ascii = sorted({ch for ch in payload if not ch.isascii()})
    if non_ascii:
        raise SystemExit(f"生成内容含非 ASCII 字符，EPICS 无法安全加载：{non_ascii}")
    target.write_text(payload, encoding="ascii", newline="\n")
    writable = sum(e.writable for e in entries)
    print(f"wrote {target} : {len(entries)} records "
          f"({len(mapped)} mapped + {len(sim_only)} sim-only, "
          f"{writable} writable)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

