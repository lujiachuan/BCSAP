"""模拟 IOC 自检：起本地 softIoc，走执行服务的真实链路做端到端校验。

用法（在仓库根目录）::

    .\\.venv\\Scripts\\python.exe sim\\sim_check.py

本脚本交叉校验三件必须一致的东西：

1. 客户端/执行服务里的**默认 PV 映射**（``device_profiles.CLUSTER_SOURCE_ENTRIES``）；
2. ``device_profiles.SIM_ONLY_ENTRIES`` —— demo 台账补充的实机 PV
   （BD Reset/Error、JM:01/:07 设定/开关、MSScan 与 MS:Scanz），只进模拟 IOC，
   不进应用默认映射；
3. ``sim/ioc.db`` 里实际定义的**模拟 PV** 与真实的 **Channel Access 读写通路**
   （ctypes + ca.dll）。

任何一处对不上都会报错，因此改了映射/台账却没改 ioc.db（或反之）能立刻发现。

安全：脚本强制 ``EPICS_CA_ADDR_LIST=127.0.0.1`` + ``EPICS_CA_AUTO_ADDR_LIST=NO``，
只与本地模拟 IOC 通信，**不会碰到任何真实束线设备**。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.instrument_service import device_profiles  # noqa: E402
from apps.instrument_service.pv_mapping import default_config  # noqa: E402
from apps.instrument_service.runtime import InstrumentRuntime  # noqa: E402

SIM_DIR = Path(__file__).resolve().parent
IOC_DB = SIM_DIR / "ioc.db"

_RECORD = re.compile(r'record\(\s*(\w+)\s*,\s*"([^"]+)"\s*\)\s*\{(.*?)\}', re.S)
_FIELD = re.compile(r'field\(\s*(\w+)\s*,\s*"?([^"\n]*?)"?\s*\)')

_READY_TIMEOUT_S = 25.0


class Report:
    """收集检查结果并统计。"""

    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def check(self, ok: bool, name: str, detail: str = "") -> bool:
        self.rows.append((bool(ok), name, detail))
        return bool(ok)

    def section(self, title: str) -> None:
        print(f"\n--- {title} ---")

    def failures(self) -> list[tuple[bool, str, str]]:
        return [row for row in self.rows if not row[0]]

    def print_summary(self) -> None:
        passed = sum(1 for ok, _n, _d in self.rows if ok)
        for ok, name, detail in self.rows:
            mark = "PASS" if ok else "FAIL"
            suffix = f"  {detail}" if detail else ""
            print(f"  [{mark}] {name}{suffix}")
        print(f"\n合计 {passed} / {len(self.rows)} 项通过")


def parse_ioc_db(path: Path) -> dict[str, dict]:
    """解析 ioc.db 得到 ``{pv: {"type", "val", "egu"}}``。"""
    text = path.read_text(encoding="utf-8")
    records: dict[str, dict] = {}
    for record_type, pv, body in _RECORD.findall(text):
        fields = {name: value for name, value in _FIELD.findall(body)}
        value = None
        raw = fields.get("VAL", "").strip()
        if raw:
            try:
                value = float(raw)
            except ValueError:
                value = None
        records[pv] = {"type": record_type, "val": value, "egu": fields.get("EGU", "").strip()}
    return records


def find_soft_ioc() -> Path | None:
    override = os.environ.get("SPECTRUM_TEST_SOFTIOC")
    if override and Path(override).is_file():
        return Path(override)
    base = os.environ.get("EPICS_BASE")
    if not base:
        return None
    arch = os.environ.get("EPICS_HOST_ARCH")
    candidates = []
    if arch:
        candidates.append(Path(base) / "bin" / arch / "softIoc.exe")
    candidates.append(Path(base) / "bin" / "softIoc.exe")
    return next((c for c in candidates if c.is_file()), None)


def start_ioc(soft_ioc: Path) -> subprocess.Popen:
    """启动 IOC。必须持有 stdin 管道：softIoc 一旦读到 EOF 就会退出。"""
    return subprocess.Popen(
        [str(soft_ioc), "-d", str(IOC_DB)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_until_ready(config, report: Report):
    """反复重建网关直到全部 PV 连上；IOC 刚起来时 CA 搜索需要一点时间。"""
    deadline = time.monotonic() + _READY_TIMEOUT_S
    last_health = None
    while time.monotonic() < deadline:
        runtime = InstrumentRuntime(config)
        health = runtime.check_health()
        if health.summary.connected == health.summary.total:
            return runtime, health
        runtime.close()
        last_health = health
        time.sleep(0.5)
    return None, last_health


def main() -> int:
    report = Report()
    print("=" * 68)
    print(" 模拟 IOC 自检（本地 softIoc + 真实 Channel Access 链路）")
    print("=" * 68)

    # ---------- 0. 前置条件 ----------
    report.section("前置条件")
    soft_ioc = find_soft_ioc()
    if soft_ioc is None:
        print("  [FAIL] 找不到 softIoc.exe：请设置 EPICS_BASE（可含 EPICS_HOST_ARCH）")
        print("         或用 SPECTRUM_TEST_SOFTIOC 直接指定路径。")
        return 1
    report.check(True, "定位 softIoc.exe", str(soft_ioc))
    report.check(IOC_DB.is_file(), "定位 ioc.db", str(IOC_DB))
    if not IOC_DB.is_file():
        report.print_summary()
        return 1

    # ---------- 1. 默认映射 + 台账补充 <-> ioc.db 一致性 ----------
    report.section("默认 PV 映射 + 台账补充 与 ioc.db 一致性")
    config = default_config()
    db_records = parse_ioc_db(IOC_DB)
    report.check(bool(db_records), f"ioc.db 解析出 {len(db_records)} 个 PV")

    sim_only_entries = device_profiles.SIM_ONLY_ENTRIES
    mapped_pvs = {entry.pv for entry in config.entries}
    sim_only_pvs = {entry.pv for entry in sim_only_entries}
    db_pvs = set(db_records)
    missing = sorted(mapped_pvs - db_pvs)
    extra = sorted(db_pvs - mapped_pvs - sim_only_pvs)
    missing_sim = sorted(sim_only_pvs - db_pvs)
    report.check(
        not missing, "映射里的 PV 在 ioc.db 中都有定义", f"缺失 {missing}" if missing else ""
    )
    report.check(
        not missing_sim,
        "台账补充 PV 在 ioc.db 中都有定义",
        f"缺失 {missing_sim}" if missing_sim else "",
    )
    report.check(
        not extra, "ioc.db 中没有映射/台账之外的多余 PV", f"多余 {extra}" if extra else ""
    )

    # 生成器对「设定→回读」配对里的回读侧输出 calc 记录（镜像设定值），
    # 其余只读输出 ai。这里复刻同一逻辑，避免把 calc 误判成类型不一致。
    mirror_of = {e.readback_signal: e.signal for e in config.entries if e.readback_signal}

    def expect_type(entry, *, mirrored: bool) -> str:
        if entry.writable:
            return "ao"
        return "calc" if mirrored else "ai"

    for entry in config.entries:
        record = db_records.get(entry.pv)
        if record is None:
            continue
        report.check(
            record["type"] == expect_type(entry, mirrored=entry.signal in mirror_of),
            f"{entry.pv} 记录类型与可写/镜像标记一致",
            f"实际 {record['type']}，期望 {expect_type(entry, mirrored=entry.signal in mirror_of)}",
        )
        report.check(
            record["egu"] == entry.unit,
            f"{entry.pv} 工程单位与映射一致",
            f"ioc.db={record['egu']} 映射={entry.unit}",
        )

    for entry in sim_only_entries:
        record = db_records.get(entry.pv)
        if record is None:
            continue
        report.check(
            record["type"] == expect_type(entry, mirrored=False),
            f"{entry.pv} 记录类型与可写标记一致（台账补充）",
            f"实际 {record['type']}，期望 {expect_type(entry, mirrored=False)}",
        )
        report.check(
            record["egu"] == entry.unit,
            f"{entry.pv} 工程单位与台账一致",
            f"ioc.db={record['egu']} 台账={entry.unit}",
        )

    # ---------- 2. 启动 IOC 并连通 ----------
    report.section("启动模拟 IOC 并建立 CA 连接")
    os.environ["EPICS_CA_ADDR_LIST"] = "127.0.0.1"
    os.environ["EPICS_CA_AUTO_ADDR_LIST"] = "NO"

    ioc = start_ioc(soft_ioc)
    try:
        time.sleep(3.0)
        if ioc.poll() is not None:
            print(f"  [FAIL] softIoc 启动后立即退出（exit={ioc.returncode}）")
            report.print_summary()
            return 1
        report.check(True, "softIoc 进程存活", f"pid={ioc.pid}")

        runtime, health = wait_until_ready(config, report)
        if runtime is None:
            print("  [FAIL] 未能在超时内连上全部 PV。逐项状态：")
            if health is not None:
                for item in health.items:
                    print(f"         {item.pv:<18} connected={item.connected} detail={item.detail}")
            print("         常见原因：ca.dll 不可加载，或本机 CA 找不到 127.0.0.1 上的 IOC。")
            report.print_summary()
            return 1

        try:
            report.check(
                health.status == "ready",
                "健康检查整体状态为 ready",
                f"status={health.status}",
            )
            report.check(
                health.summary.required_failed == 0,
                "必需 PV 无掉线",
                f"required_failed={health.summary.required_failed}",
            )

            # ---------- 3. 读值校验 ----------
            # 普通 ao/ai 记录对照 ioc.db 的 VAL 初值；calc 记录没有 VAL，
            # 它镜像自己的设定信号，因此对照设定信号的当前值。
            report.section("读值校验（ao/ai 对照 ioc.db 初值，calc 对照镜像设定值）")
            for entry in config.entries:
                record = db_records[entry.pv]
                reading = runtime.gateway().read(entry.signal)
                if record["type"] == "calc":
                    source_signal = mirror_of[entry.signal]
                    source_reading = runtime.gateway().read(source_signal)
                    ok = (
                        reading.connected
                        and source_reading.connected
                        and abs(reading.value - source_reading.value) < 1e-6
                    )
                    report.check(
                        ok,
                        f"读 {entry.pv}（镜像）",
                        f"实际 {reading.value} 期望 {source_reading.value}"
                        f"（源 {source_signal}）单位 {reading.unit}",
                    )
                else:
                    expected = record["val"]
                    ok = (
                        reading.connected
                        and expected is not None
                        and abs(reading.value - expected) < 1e-6
                    )
                    report.check(
                        ok,
                        f"读 {entry.pv}",
                        f"实际 {reading.value} 期望 {expected} 单位 {reading.unit}",
                    )
                report.check(
                    reading.unit == entry.unit,
                    f"{entry.pv} 单位来自受控映射",
                    f"{reading.unit!r}",
                )

            # ---------- 4. 写回往返（含镜像回读联动） ----------
            report.section("写入往返（仅可写 PV）")
            for entry in config.entries:
                if not entry.writable:
                    continue
                base = db_records[entry.pv]["val"] or 0.0
                target = round(base + 0.5, 3)
                runtime.gateway().write(entry.signal, target, uuid.uuid4())
                back = runtime.gateway().read(entry.signal)
                report.check(
                    back.connected and abs(back.value - target) < 1e-6,
                    f"写 {entry.pv} = {target}",
                    f"读回 {back.value}",
                )
                if entry.readback_signal:
                    rb_reading = runtime.gateway().read(entry.readback_signal)
                    report.check(
                        rb_reading.connected and abs(rb_reading.value - target) < 1e-6,
                        f"{entry.pv} 写后回读 {entry.readback_signal} 联动",
                        f"回读值 {rb_reading.value}",
                    )

            # ---------- 5. 只读 PV ----------
            report.section("只读 PV 行为")
            read_only = [item for item in health.items if not item.writable]
            report.check(bool(read_only), "存在被标记为只读的 PV", f"{len(read_only)} 个")
            for item in read_only:
                entry = next(e for e in config.entries if e.pv == item.pv)
                report.check(
                    not entry.writable and item.connected,
                    f"{item.pv} 可读但标记为不可写",
                    f"connected={item.connected} writable={item.writable}",
                )

            # ---------- 6. 改映射应立即改变读的 PV ----------
            report.section("映射热更新（改 PV 名后立即生效）")
            first = config.entries[0]
            donor = next(e for e in config.entries if e.pv != first.pv)
            remapped = config.model_copy(
                update={
                    "entries": [
                        first.model_copy(update={"pv": donor.pv}) if e.signal == first.signal else e
                        for e in config.entries
                    ]
                }
            )
            runtime.apply(remapped)
            after = runtime.check_health()
            item = next(i for i in after.items if i.signal == first.signal)
            reading = runtime.gateway().read(first.signal)
            # 与 donor 的实时值比较，而不是 ioc.db 初值：前面的写入往返已经改过它。
            donor_reading = runtime.gateway().read(donor.signal)
            report.check(
                item.pv == donor.pv,
                f"改映射后 {first.signal} 指向新 PV",
                f"{first.pv} -> {item.pv}",
            )
            report.check(
                reading.connected and abs(reading.value - donor_reading.value) < 1e-6,
                "改映射后读到的是新 PV 的值",
                f"实际 {reading.value} 期望 {donor_reading.value}（{donor.pv} 实时值）",
            )
        finally:
            runtime.close()
    finally:
        ioc.terminate()
        try:
            ioc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            ioc.kill()

    report.section("结果")
    report.print_summary()
    failures = report.failures()
    if failures:
        print(f"\n有 {len(failures)} 项未通过。")
        return 1
    print("\n模拟 IOC 与执行服务链路自检全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
