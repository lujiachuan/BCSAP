"""设备配置档：团簇离子源与磁电双聚焦的受控信号清单。

来源是 demo（``demo/argon_tuning/argon_tuning/``）实测确认的 PV 台账
（``PV清单.md``）与现场上限配置（``limits_config.json``）：

* PV 名与通道含义取自 ``PV清单.md``；
* 上限取 **现场实际保存值**，不是代码里的内置默认值——两者不一致且现场值更紧
  （JM3/JM4 与 DW 其余通道 5100 V、主高压 50 kV、磁铁速率 10 A/s）；
* 稳定判据取自 demo 的 ``--settle-tol / --settle-wait``（气体 2 sccm / 30 s）。

``group`` 与 demo 手动控制页的板块一一对应，同时也是设备组锁的粒度；
``GROUP_ORDER`` 决定界面里的呈现顺序。
"""

from __future__ import annotations

from packages.contracts import PvMappingConfig, PvMappingEntry

CONFIG_VERSION = 1

# 分组名（与 demo 手动控制页板块一致），顺序即界面顺序
GROUP_GAS = "气体流量"
GROUP_SPUTTER = "溅射电源"
GROUP_VACUUM = "腔体气压"
GROUP_ION_OPTICS = "聚焦/漂移管"
GROUP_DW = "高压阵列 DW"
GROUP_BD = "新高压电源 BD"
GROUP_MAGNET = "磁铁电源"
GROUP_DETECTOR = "束流探测"

GROUP_ORDER: tuple[str, ...] = (
    GROUP_GAS,
    GROUP_SPUTTER,
    GROUP_VACUUM,
    GROUP_ION_OPTICS,
    GROUP_DW,
    GROUP_BD,
    GROUP_MAGNET,
    GROUP_DETECTOR,
)

# --- 现场上限（来自 demo limits_config.json，单位见各条目）---
GAS_MAX = 500.0
SPUTTER_MAX = 500.0
JM_MAX = 5100.0
DW_MAX_DEFAULT = 5100.0
DW_MAX_BY_CHANNEL = {1: 200.0, 2: 2500.0, 3: 2500.0}  # skim / 引出1 / 引出2
MAG_CURRENT_MAX = 600.0
MAG_RATE_MAX = 10.0

# DW 13 路通道含义（PV清单.md）
DW_CHANNELS: tuple[tuple[int, str], ...] = (
    (1, "skim"),
    (2, "引出1"),
    (3, "引出2"),
    (4, "通道4"),
    (5, "前偏转上"),
    (6, "前偏转下"),
    (7, "前偏转左"),
    (8, "前偏转右"),
    (9, "后偏转上"),
    (10, "后偏转下"),
    (11, "后偏转左"),
    (12, "后偏转右"),
    (13, "三圆筒"),
)

# BD 新高压电源 5 路：(键名, 显示名, PV 前缀, 上限, 单位)
BD_DEVICES: tuple[tuple[str, str, str, float, str], ...] = (
    ("cylinder1", "三圆筒1", "BD:FocusThreecyLinderHV:01", 22000.0, "V"),
    ("cylinder2", "三圆筒2", "BD:FocusThreecyLinderHV:02", 22000.0, "V"),
    ("deflector1", "电偏转1", "BD:MultipolePositiveHV:01", 10000.0, "V"),
    ("deflector2", "电偏转2", "BD:MultipolePositiveHV:02", 10000.0, "V"),
    ("main", "主高压", "BD:MainHV:01", 50.0, "kV"),
)

MAGNET_COUNT = 4


def _e(**kwargs: object) -> PvMappingEntry:
    return PvMappingEntry(**kwargs)  # type: ignore[arg-type]


# 信号角色由命名规则推导，而不是在每一处 _e(...) 里手写：
# 128 条里有一半是循环生成的，逐条写 role 必然漏。规则本身受
# tests/test_signal_io.py::test_every_entry_has_a_role 保护——出现无法归类的
# 新信号名会直接让测试失败，而不是在界面上悄悄退化成只读。
_SETPOINT_SUFFIXES = (
    ".voltage_setpoint",
    ".current_setpoint",
    ".current_rate_setpoint",
    ".flow_setpoint",
    ".power_setpoint",
    ".power_5k_setpoint",
)
_TOGGLE_SUFFIXES = (".switch", ".output_enable", ".mode", ".power_enable")
_PULSE_SUFFIXES = (".arc_clear", ".start", ".stop", ".reset")


def role_for(signal: str, writable: bool) -> str:
    """按命名规则给出信号角色。"""
    for suffix in _SETPOINT_SUFFIXES:
        if signal.endswith(suffix):
            return "setpoint"
    for suffix in _TOGGLE_SUFFIXES:
        if signal.endswith(suffix):
            return "toggle"
    for suffix in _PULSE_SUFFIXES:
        if signal.endswith(suffix):
            return "pulse"
    if signal.endswith("_readback") or not writable:
        return "readback"
    raise ValueError(f"无法归类信号角色，请在 device_profiles.role_for 中补充规则：{signal}")


# 可作为自动调束**变量**的设定量：真正改变束流的那几类连续量。
# 磁铁的 ``.current_rate_setpoint`` 是**保护参数**（变化速率上限），不是被优化量：
# 把它当变量去"优化"，优化器会去调变化快慢，既没物理意义、又会让设备运动变得不可预测。
_TUNABLE_SETPOINT_SUFFIXES = (
    ".flow_setpoint",
    ".power_setpoint",
    ".power_5k_setpoint",
    ".voltage_setpoint",
    ".current_setpoint",
)
_NOT_TUNABLE_SUFFIXES = (".current_rate_setpoint",)
# 可作为调束**目标**的量：只有束流测量（法拉第杯）。气压、电压回读这类只读量
# 不该出现在"最大化目标"下拉框里。
_BEAM_TARGET_SUFFIXES = (".beam_current",)


def tunable_for(signal: str, role: str) -> bool:
    """该设定量是否允许作为自动调束的可调变量（默认不允许）。"""
    if role != "setpoint":
        return False
    if signal.endswith(_NOT_TUNABLE_SUFFIXES):
        return False
    return signal.endswith(_TUNABLE_SETPOINT_SUFFIXES)


def beam_target_for(signal: str) -> bool:
    """该只读量是否可作为调束目标（束流测量）。"""
    return signal.endswith(_BEAM_TARGET_SUFFIXES)


def _build_entries() -> list[PvMappingEntry]:
    entries: list[PvMappingEntry] = []

    # ---------------- 气体流量 ----------------
    for key, gas, pv_set, pv_rb, pv_mode in (
        ("ar", "Ar", "Part1:Flow_W:CS200A:Setpoint", "Part1:Flow_R:CS200A:InstantSCCM",
         "Part1:Valve_CMD:CS200A:Mode"),
        ("he", "He", "Part1:Flow_W:CS200A02:Setpoint", "Part1:Flow_R:CS200A02:InstantSCCM",
         "Part1:Valve_CMD:CS200A02:Mode"),
    ):
        entries += [
            _e(signal=f"gas.{key}.flow_setpoint", label=f"{gas} 流量设定",
               pv=pv_set, unit="sccm", writable=True, required=False,
               group=GROUP_GAS, readback_signal=f"gas.{key}.flow_readback",
               min_value=0.0, max_value=GAS_MAX, max_step=50.0, max_rate=500.0,
               settle_tol=2.0, settle_timeout=30.0),
            _e(signal=f"gas.{key}.flow_readback", label=f"{gas} 瞬时流量",
               pv=pv_rb, unit="sccm", writable=False, required=False,
               group=GROUP_GAS),
            _e(signal=f"gas.{key}.mode", label=f"{gas} 阀门模式",
               pv=pv_mode, unit="", writable=True, required=False,
               group=GROUP_GAS, min_value=0.0, max_value=2.0, max_step=1.0),
        ]

    # ---------------- 溅射电源 ----------------
    entries += [
        _e(signal="sputter.power_setpoint", label="溅射功率设定",
           pv="Part1:JSPow:Pwr-S", unit="W", writable=True, required=False,
           group=GROUP_SPUTTER, readback_signal="sputter.power_readback",
           min_value=0.0, max_value=SPUTTER_MAX, max_step=50.0, max_rate=500.0,
           settle_tol=1.0, settle_timeout=10.0),
        _e(signal="sputter.power_5k_setpoint", label="溅射功率设定(5K量程)",
           pv="Part1:JSPow:Pwr-S5K", unit="W", writable=True, required=False,
           group=GROUP_SPUTTER, min_value=0.0, max_value=5000.0,
           max_step=500.0, max_rate=5000.0),
        _e(signal="sputter.power_enable", label="溅射电源开关",
           pv="Part1:JSPow:Power-Sel", unit="", writable=True, required=False,
           group=GROUP_SPUTTER, min_value=0.0, max_value=1.0, max_step=1.0),
        _e(signal="sputter.arc_clear", label="灭弧清除",
           pv="Part1:JSPow:ArcClear-S", unit="", writable=True, required=False,
           group=GROUP_SPUTTER, min_value=0.0, max_value=1.0, max_step=1.0),
        _e(signal="sputter.power_readback", label="溅射功率回读",
           pv="Part1:JSPow:Pwr-R", unit="W", writable=False, required=False,
           group=GROUP_SPUTTER),
        _e(signal="sputter.arc_rate_readback", label="打弧速率回读",
           pv="Part1:JSPow:ArcRate-R", unit="", writable=False, required=False,
           group=GROUP_SPUTTER),
    ]

    # ---------------- 腔体气压 ----------------
    entries.append(
        _e(signal="vacuum.chamber_pressure", label="冷凝腔气压",
           pv="Part1:Sputtering", unit="Pa", writable=False, required=True,
           group=GROUP_VACUUM)
    )

    # ---------------- 聚焦 / 漂移管 ----------------
    for chan, key, name in (("03", "focus", "聚焦"), ("04", "drift", "漂移管")):
        entries += [
            _e(signal=f"ion_optics.{key}.voltage_setpoint", label=f"{name}电压设定",
               pv=f"Part1:JM_POWER:{chan}:SET_VOL", unit="V", writable=True,
               required=False, group=GROUP_ION_OPTICS,
               readback_signal=f"ion_optics.{key}.voltage_readback",
               min_value=0.0, max_value=JM_MAX, max_step=500.0, max_rate=5000.0,
               settle_tol=5.0, settle_timeout=15.0),
            _e(signal=f"ion_optics.{key}.output_enable", label=f"{name}输出开关",
               pv=f"Part1:JM_POWER:{chan}:OutPut", unit="", writable=True,
               required=False, group=GROUP_ION_OPTICS,
               min_value=0.0, max_value=1.0, max_step=1.0),
            _e(signal=f"ion_optics.{key}.voltage_readback", label=f"{name}电压回读",
               pv=f"Part1:JM_POWER:{chan}:MEAS_VOL", unit="V", writable=False,
               required=False, group=GROUP_ION_OPTICS),
            _e(signal=f"ion_optics.{key}.current_readback", label=f"{name}电流回读",
               pv=f"Part1:JM_POWER:{chan}:MEAS_CUR", unit="A", writable=False,
               required=False, group=GROUP_ION_OPTICS),
        ]
    for chan in ("01", "07"):
        entries += [
            _e(signal=f"ion_optics.jm{chan}.voltage_readback",
               label=f"JM{chan} 电压回读", pv=f"Part1:JM_POWER:{chan}:MEAS_VOL",
               unit="V", writable=False, required=False, group=GROUP_ION_OPTICS),
            _e(signal=f"ion_optics.jm{chan}.current_readback",
               label=f"JM{chan} 电流回读", pv=f"Part1:JM_POWER:{chan}:MEAS_CUR",
               unit="A", writable=False, required=False, group=GROUP_ION_OPTICS),
        ]

    # ---------------- DW 13 路高压阵列 ----------------
    for ch, name in DW_CHANNELS:
        limit = DW_MAX_BY_CHANNEL.get(ch, DW_MAX_DEFAULT)
        entries += [
            _e(signal=f"hv_array.dw{ch:02d}.voltage_setpoint",
               label=f"DW{ch} {name} 电压设定", pv=f"Part1:DW:Voltage_Set{ch}",
               unit="V", writable=True, required=False, group=GROUP_DW,
               readback_signal=f"hv_array.dw{ch:02d}.voltage_readback",
               min_value=0.0, max_value=limit, max_step=500.0, max_rate=5000.0,
               settle_tol=5.0, settle_timeout=15.0),
            _e(signal=f"hv_array.dw{ch:02d}.switch", label=f"DW{ch} {name} 输出开关",
               pv=f"Part1:DW:Voltage_Switch{ch}", unit="", writable=True,
               required=False, group=GROUP_DW,
               min_value=0.0, max_value=1.0, max_step=1.0),
            _e(signal=f"hv_array.dw{ch:02d}.voltage_readback",
               label=f"DW{ch} {name} 电压回读", pv=f"Part1:DW:Voltage_Read{ch}",
               unit="V", writable=False, required=False, group=GROUP_DW),
            _e(signal=f"hv_array.dw{ch:02d}.current_readback",
               label=f"DW{ch} {name} 电流回读", pv=f"Part1:DW:Current_Read{ch}",
               unit="mA", writable=False, required=False, group=GROUP_DW),
        ]

    # ---------------- BD 新高压电源 5 路 ----------------
    for key, name, prefix, limit, unit in BD_DEVICES:
        entries += [
            _e(signal=f"hv_bd.{key}.voltage_setpoint", label=f"{name} 高压设定",
               pv=f"{prefix}:HVSet", unit=unit, writable=True, required=False,
               group=GROUP_BD, readback_signal=f"hv_bd.{key}.voltage_readback",
               min_value=0.0, max_value=limit,
               max_step=limit / 10.0, max_rate=limit * 10.0,
               settle_tol=limit / 1000.0, settle_timeout=15.0),
            _e(signal=f"hv_bd.{key}.output_enable", label=f"{name} 输出使能",
               pv=f"{prefix}:OutEnable", unit="", writable=True, required=False,
               group=GROUP_BD, min_value=0.0, max_value=1.0, max_step=1.0),
            _e(signal=f"hv_bd.{key}.voltage_readback", label=f"{name} 高压回读",
               pv=f"{prefix}:HVMonitor", unit=unit, writable=False,
               required=False, group=GROUP_BD),
            _e(signal=f"hv_bd.{key}.current_readback", label=f"{name} 电流回读",
               pv=f"{prefix}:CurrentMonitor", unit="mA", writable=False,
               required=False, group=GROUP_BD),
        ]
    # 主高压额外一路电流设定（mV 量级：mA）
    entries.append(
        _e(signal="hv_bd.main.current_setpoint", label="主高压 电流设定",
           pv="BD:MainHV:01:HVCurrentSet", unit="mA", writable=True,
           required=False, group=GROUP_BD,
           min_value=0.0, max_value=1000.0, max_step=10.0, max_rate=100.0)
    )

    # ---------------- 磁铁电源 4 路 ----------------
    for n in range(1, MAGNET_COUNT + 1):
        prefix = f"BD:DipoleMagnet:{n:02d}"
        entries += [
            _e(signal=f"magnet.m{n}.current_setpoint", label=f"磁铁{n} 电流设定",
               pv=f"{prefix}:CurrentSet", unit="A", writable=True, required=False,
               group=GROUP_MAGNET,
               readback_signal=f"magnet.m{n}.current_readback",
               rate_signal=f"magnet.m{n}.current_rate_setpoint",
               min_value=0.0, max_value=MAG_CURRENT_MAX,
               max_step=100.0, max_rate=50.0, settle_tol=0.5, settle_timeout=60.0),
            _e(signal=f"magnet.m{n}.current_rate_setpoint",
               label=f"磁铁{n} 电流速率设定", pv=f"{prefix}:CurrentRateSet",
               unit="A/s", writable=True, required=False, group=GROUP_MAGNET,
               min_value=0.0, max_value=MAG_RATE_MAX, max_step=1.0, max_rate=1.0),
            _e(signal=f"magnet.m{n}.current_readback", label=f"磁铁{n} 电流回读",
               pv=f"{prefix}:CurrentMonitor", unit="A", writable=False,
               required=False, group=GROUP_MAGNET),
            _e(signal=f"magnet.m{n}.hv_readback", label=f"磁铁{n} 主高压回读",
               pv=f"{prefix}:HVMonitor", unit="V", writable=False,
               required=False, group=GROUP_MAGNET),
            _e(signal=f"magnet.m{n}.start", label=f"磁铁{n} 启动",
               pv=f"{prefix}:Start", unit="", writable=True, required=False,
               group=GROUP_MAGNET, min_value=0.0, max_value=1.0, max_step=1.0),
            _e(signal=f"magnet.m{n}.stop", label=f"磁铁{n} 停止",
               pv=f"{prefix}:Stop", unit="", writable=True, required=False,
               group=GROUP_MAGNET, min_value=0.0, max_value=1.0, max_step=1.0),
            _e(signal=f"magnet.m{n}.reset", label=f"磁铁{n} 复位",
               pv=f"{prefix}:Reset", unit="", writable=True, required=False,
               group=GROUP_MAGNET, min_value=0.0, max_value=1.0, max_step=1.0),
        ]

    # ---------------- 束流探测（法拉第杯） ----------------
    entries += [
        _e(signal="detector.fc1.beam_current", label="FC1 束流电流",
           pv="BD:FC:01:BeamCurrent", unit="nA", writable=False, required=True,
           group=GROUP_DETECTOR),
        _e(signal="detector.fc2.beam_current", label="FC2 束流电流",
           pv="BD:FC:02:BeamCurrent", unit="nA", writable=False, required=False,
           group=GROUP_DETECTOR),
    ]

    return entries


def _with_roles(entries: list[PvMappingEntry]) -> tuple[PvMappingEntry, ...]:
    """给每条映射补上 role 与**调束用途标记**。

    标记同样是**按命名规则推导**而不是逐条手写：128 条里有一半是循环生成的，
    漏标一条的后果是界面上少一个可调参数、或者把不该优化的量（磁铁速率）列进去。
    规则本身受 ``tests/test_signal_io.py`` 的用例保护。
    """
    return tuple(
        entry.model_copy(
            update={
                "role": role_for(entry.signal, entry.writable),
                "tunable": tunable_for(
                    entry.signal, role_for(entry.signal, entry.writable)
                ),
                "beam_target": beam_target_for(entry.signal),
                "scan_axis": (
                    entry.signal.startswith("magnet.")
                    and entry.signal.endswith(".current_setpoint")
                ),
                "scan_detector": beam_target_for(entry.signal),
                "safe_value": (
                    0.0
                    if entry.writable
                    and role_for(entry.signal, entry.writable) == "setpoint"
                    and not entry.signal.endswith(".current_rate_setpoint")
                    else None
                ),
            }
        )
        for entry in entries
    )


CLUSTER_SOURCE_ENTRIES: tuple[PvMappingEntry, ...] = _with_roles(_build_entries())

# ---------------------------------------------------------------------------
# 仅模拟 IOC 覆盖的实机 PV（台账补充，不进入默认映射）
# ---------------------------------------------------------------------------
# 来源（demo/argon_tuning/argon_tuning/）：
#   * PV清单.md F 节：BD 新高压电源的 Reset/Error 字段（实机 13/13 连通）；
#   * PV清单.md D 节：JM_POWER:01/:07 四个字段全 0 实测存在（含义待确认，
#     按命名规律补齐 SET_VOL / OutPut，供模拟 IOC 完整覆盖）；
#   * _tmp/MSScan_v2.db：BD:MSScan:01:* 与 BD:MS:Scanz:*（质谱扫描，IOC 已上线）。
# 这些 PV 在实机上存在，但应用默认映射（default_config）暂未纳入——界面健康检查
# 与自动调束不依赖它们。生成 sim/ioc.db 时一并输出，保证本地联调时台账里的每个
# PV 都能 caget/caput。role 逐条手写（不经过 role_for：命名不满足映射侧规则，
# 且模拟 IOC 不需要界面控件语义）。
SIM_ONLY_ENTRIES: tuple[PvMappingEntry, ...] = (
    # ---- BD 新高压电源：三圆筒1/2 的 Reset/Error，主高压 Reset（PV清单 F 节）----
    _e(signal="bd_extra.cylinder1.reset", label="三圆筒1 复位(台账补充)",
       pv="BD:FocusThreecyLinderHV:01:Reset", unit="", writable=True,
       required=False, group=GROUP_BD, role="pulse",
       min_value=0.0, max_value=1.0, max_step=1.0),
    _e(signal="bd_extra.cylinder1.error", label="三圆筒1 故障(台账补充)",
       pv="BD:FocusThreecyLinderHV:01:Error", unit="", writable=False,
       required=False, group=GROUP_BD, role="readback"),
    _e(signal="bd_extra.cylinder2.reset", label="三圆筒2 复位(台账补充)",
       pv="BD:FocusThreecyLinderHV:02:Reset", unit="", writable=True,
       required=False, group=GROUP_BD, role="pulse",
       min_value=0.0, max_value=1.0, max_step=1.0),
    _e(signal="bd_extra.cylinder2.error", label="三圆筒2 故障(台账补充)",
       pv="BD:FocusThreecyLinderHV:02:Error", unit="", writable=False,
       required=False, group=GROUP_BD, role="readback"),
    _e(signal="bd_extra.main.reset", label="主高压 复位(台账补充)",
       pv="BD:MainHV:01:Reset", unit="", writable=True,
       required=False, group=GROUP_BD, role="pulse",
       min_value=0.0, max_value=1.0, max_step=1.0),
    # ---- JM_POWER:01/:07 设定/开关（PV清单 D 节：全 0 实测存在，含义待确认）----
    _e(signal="ion_optics.jm01.voltage_setpoint", label="JM01 电压设定(台账补充)",
       pv="Part1:JM_POWER:01:SET_VOL", unit="V", writable=True,
       required=False, group=GROUP_ION_OPTICS, role="setpoint",
       min_value=0.0, max_value=JM_MAX, max_step=500.0, max_rate=5000.0),
    _e(signal="ion_optics.jm01.output_enable", label="JM01 输出开关(台账补充)",
       pv="Part1:JM_POWER:01:OutPut", unit="", writable=True,
       required=False, group=GROUP_ION_OPTICS, role="toggle",
       min_value=0.0, max_value=1.0, max_step=1.0),
    _e(signal="ion_optics.jm07.voltage_setpoint", label="JM07 电压设定(台账补充)",
       pv="Part1:JM_POWER:07:SET_VOL", unit="V", writable=True,
       required=False, group=GROUP_ION_OPTICS, role="setpoint",
       min_value=0.0, max_value=JM_MAX, max_step=500.0, max_rate=5000.0),
    _e(signal="ion_optics.jm07.output_enable", label="JM07 输出开关(台账补充)",
       pv="Part1:JM_POWER:07:OutPut", unit="", writable=True,
       required=False, group=GROUP_ION_OPTICS, role="toggle",
       min_value=0.0, max_value=1.0, max_step=1.0),
    # ---- 质谱扫描：cRIO IOC 部署的 MSScan 与 LabVIEW 实际消费的 Scanz（MSScan_v2.db）----
    _e(signal="ms_scan.start", label="质谱扫描启动(台账补充)",
       pv="BD:MSScan:01:ScanStart", unit="", writable=True,
       required=False, group=GROUP_MAGNET, role="pulse",
       min_value=0.0, max_value=1.0, max_step=1.0),
    _e(signal="ms_scan.stop", label="质谱扫描停止(台账补充)",
       pv="BD:MSScan:01:ScanStop", unit="", writable=True,
       required=False, group=GROUP_MAGNET, role="pulse",
       min_value=0.0, max_value=1.0, max_step=1.0),
    _e(signal="ms_scan.start_current", label="扫描起始电流(台账补充)",
       pv="BD:MSScan:01:StartCurrent", unit="A", writable=True,
       required=False, group=GROUP_MAGNET, role="setpoint",
       min_value=0.0, max_value=600.0, max_step=100.0, max_rate=50.0),
    _e(signal="ms_scan.stop_current", label="扫描终止电流(台账补充)",
       pv="BD:MSScan:01:StopCurrent", unit="A", writable=True,
       required=False, group=GROUP_MAGNET, role="setpoint",
       min_value=0.0, max_value=600.0, max_step=100.0, max_rate=50.0),
    _e(signal="ms_scan.wait_step", label="每步等待时间(台账补充)",
       pv="BD:MSScan:01:WaitStep", unit="", writable=True,
       required=False, group=GROUP_MAGNET, role="setpoint",
       min_value=0.0, max_value=100000.0, max_step=1000.0, max_rate=100000.0),
    _e(signal="ms_scan.wait_times", label="等待次数(台账补充)",
       pv="BD:MSScan:01:WaitTimes", unit="", writable=True,
       required=False, group=GROUP_MAGNET, role="setpoint",
       min_value=0.0, max_value=100000.0, max_step=1000.0, max_rate=100000.0),
    _e(signal="ms_scan.scanz.start_a", label="扫描起始电流A(台账补充)",
       pv="BD:MS:Scanz:StartA", unit="A", writable=True,
       required=False, group=GROUP_MAGNET, role="setpoint",
       min_value=0.0, max_value=600.0, max_step=100.0, max_rate=50.0),
    _e(signal="ms_scan.scanz.stop_a", label="扫描终止电流A(台账补充)",
       pv="BD:MS:Scanz:StopA", unit="A", writable=True,
       required=False, group=GROUP_MAGNET, role="setpoint",
       min_value=0.0, max_value=600.0, max_step=100.0, max_rate=50.0),
    _e(signal="ms_scan.scanz.boolean_start", label="扫描启动布尔(台账补充)",
       pv="BD:MS:Scanz:BooleanStart", unit="", writable=True,
       required=False, group=GROUP_MAGNET, role="pulse",
       min_value=0.0, max_value=1.0, max_step=1.0),
    _e(signal="ms_scan.scanz.boolean_stop", label="扫描停止布尔(台账补充)",
       pv="BD:MS:Scanz:BooleanStop", unit="", writable=True,
       required=False, group=GROUP_MAGNET, role="pulse",
       min_value=0.0, max_value=1.0, max_step=1.0),
    _e(signal="ms_scan.scanz.rate_set", label="扫描速率(台账补充)",
       pv="BD:MS:Scanz:RateSet", unit="A/s", writable=True,
       required=False, group=GROUP_MAGNET, role="setpoint",
       min_value=0.0, max_value=10.0, max_step=1.0, max_rate=10.0),
    _e(signal="ms_scan.scanz.wait_step", label="每步等待时间(台账补充)",
       pv="BD:MS:Scanz:WaitStep", unit="", writable=True,
       required=False, group=GROUP_MAGNET, role="setpoint",
       min_value=0.0, max_value=100000.0, max_step=1000.0, max_rate=100000.0),
    _e(signal="ms_scan.scanz.wait_time", label="等待次数(台账补充)",
       pv="BD:MS:Scanz:WaitTime", unit="", writable=True,
       required=False, group=GROUP_MAGNET, role="setpoint",
       min_value=0.0, max_value=100000.0, max_step=1000.0, max_rate=100000.0),
)

# 模拟网关初值：取 demo 实机观测到的现场值，便于对照界面读数。
SIMULATED_VALUES: dict[str, float] = {
    "gas.ar.flow_setpoint": 100.0,
    "gas.ar.flow_readback": 250.0,
    "gas.ar.mode": 1.0,
    "gas.he.flow_setpoint": 0.0,
    "gas.he.flow_readback": 0.18,
    "gas.he.mode": 1.0,
    "sputter.power_setpoint": 50.0,
    "sputter.power_5k_setpoint": 0.0,
    "sputter.power_enable": 1.0,
    "sputter.arc_clear": 0.0,
    "sputter.power_readback": 35.0,
    "sputter.arc_rate_readback": 0.0,
    "vacuum.chamber_pressure": 0.9765,
    "ion_optics.focus.voltage_setpoint": 3000.0,
    "ion_optics.focus.voltage_readback": 3300.0,
    "ion_optics.focus.current_readback": 0.0,
    "ion_optics.focus.output_enable": 0.0,
    "ion_optics.drift.voltage_setpoint": 5038.0,
    "ion_optics.drift.voltage_readback": 5031.0,
    "ion_optics.drift.current_readback": 0.0,
    "ion_optics.drift.output_enable": 0.0,
    "hv_bd.cylinder1.voltage_setpoint": 19600.0,
    "hv_bd.cylinder1.voltage_readback": 2.198,
    "hv_bd.cylinder1.current_readback": 0.00029,
    "hv_bd.cylinder1.output_enable": 1.0,
    "hv_bd.cylinder2.voltage_setpoint": 15500.0,
    "hv_bd.cylinder2.voltage_readback": 0.0,
    "hv_bd.cylinder2.current_readback": 0.00027,
    "hv_bd.cylinder2.output_enable": 1.0,
    "hv_bd.deflector1.voltage_setpoint": 8199.8,
    "hv_bd.deflector1.voltage_readback": 8203.39,
    "hv_bd.deflector1.current_readback": 0.05,
    "hv_bd.deflector1.output_enable": 1.0,
    "hv_bd.deflector2.voltage_setpoint": 8199.0,
    "hv_bd.deflector2.voltage_readback": 8192.72,
    "hv_bd.deflector2.current_readback": 0.05,
    "hv_bd.deflector2.output_enable": 1.0,
    "hv_bd.main.voltage_setpoint": 40.0,
    "hv_bd.main.current_setpoint": 0.2,
    "hv_bd.main.voltage_readback": 39.999,
    "hv_bd.main.current_readback": 0.1705,
    "hv_bd.main.output_enable": 1.0,
    "detector.fc1.beam_current": 8.31,
    "detector.fc2.beam_current": 0.0104,
}

# DW 与磁铁的模拟初值按 demo 实机值补齐（数值次要，关键是让界面有真实感）
for _ch, _ in DW_CHANNELS:
    SIMULATED_VALUES.setdefault(f"hv_array.dw{_ch:02d}.voltage_setpoint",
                                float({1: 9, 2: 300, 3: 1180, 13: 3800}.get(_ch, 0)))
    SIMULATED_VALUES.setdefault(f"hv_array.dw{_ch:02d}.switch", 1.0 if _ch != 4 else 0.0)
    SIMULATED_VALUES.setdefault(
        f"hv_array.dw{_ch:02d}.voltage_readback",
        float({1: 0, 2: 298, 3: 1183, 5: 2375, 6: 2368, 7: 2384, 8: 2371,
               9: 2376, 10: 2366, 11: 2342, 12: 2370, 13: 3818}.get(_ch, 0)),
    )
    SIMULATED_VALUES.setdefault(f"hv_array.dw{_ch:02d}.current_readback", 0.0)

for _n in range(1, MAGNET_COUNT + 1):
    SIMULATED_VALUES.setdefault(f"magnet.m{_n}.current_setpoint", 600.0)
    SIMULATED_VALUES.setdefault(f"magnet.m{_n}.current_rate_setpoint", 2.0)
    SIMULATED_VALUES.setdefault(f"magnet.m{_n}.current_readback", 110.0 + _n)
    SIMULATED_VALUES.setdefault(f"magnet.m{_n}.hv_readback", 12000.0)
    for _field in ("start", "stop", "reset"):
        SIMULATED_VALUES.setdefault(f"magnet.m{_n}.{_field}", 0.0)


def default_config() -> PvMappingConfig:
    """默认设备配置：团簇离子源与磁电双聚焦。"""
    return PvMappingConfig(
        version=CONFIG_VERSION,
        entries=list(CLUSTER_SOURCE_ENTRIES),
    )


def simulated_values() -> dict[str, tuple[float, str]]:
    """模拟网关的 ``{signal: (初值, 单位)}``。"""
    return {
        entry.signal: (
            SIMULATED_VALUES.get(entry.signal, 0.0),
            entry.unit,
        )
        for entry in CLUSTER_SOURCE_ENTRIES
    }
