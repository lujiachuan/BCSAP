# 模拟 EPICS IOC（本地联调用）

在没有真实束线设备的情况下，用 EPICS base 自带的 `softIoc` 起一个**本地模拟 IOC**，
提供与**当前实机 PV 台账**一一对应的 150 个 PV。设备访问统一走真实 EPICS Channel
Access（没有模式开关），所以只要起好这个本地 IOC，就能在完全不碰真实设备的前提下走通
「客户端 → 执行服务 → Channel Access → IOC」的完整链路。

```
sim/
├── ioc.db          模拟 IOC 的 PV 数据库（150 个 PV）
├── start-ioc.cmd   一键启动（双击即用）
├── sim_check.py    无头自检：起 IOC → 走执行服务链路 → 逐项断言
└── README.md       本文件
```

`ioc.db` 是**生成文件**，不要手改：`tools/generate_sim_ioc.py` 负责生成，
来源是 `apps/instrument_service/device_profiles.py`（见下文「PV 清单」）。

## 1. 前置条件

| 条件 | 说明 |
| --- | --- |
| `EPICS_BASE` | 指向 EPICS base 安装目录，需要其中的 `softIoc.exe` |
| `EPICS_HOST_ARCH` | 可选。设置后优先找 `%EPICS_BASE%\bin\%EPICS_HOST_ARCH%\softIoc.exe`；未设置时脚本会回退扫描 `%EPICS_BASE%\bin\*\softIoc.exe`（如 `windows-x64-mingw`），再回退 `%EPICS_BASE%\bin\softIoc.exe` |
| 可加载的 `ca.dll` | 见下方「CA 库的坑」 |

### CA 库的坑

执行服务用 ctypes 直接加载 `ca.dll`，定位是**全自动**的，界面上没有配置项。**EPICS base 的
mingw 构建常常加载不了**——它依赖 `libgcc_s_seh-1.dll` / `libstdc++-6.dll`，这两个 DLL 在很多
机器上并不存在。遇到这种情况，执行服务会按下面的顺序继续尝试下一个候选目录，通常能自动
落到官方 MSVC 构建的 CA 分发上（它只依赖 `VCRUNTIME140` / `MSVCP140`）：

1. 环境变量 `SPECTRUM_CA_LIB_DIR` 或 `EPICS_CA_LIB_DIR`
2. `EPICS_BASE/bin/$EPICS_HOST_ARCH`
3. `EPICS_BASE/bin`
4. 与 `EPICS_BASE` 同盘的 `CA-*-windows-*` 目录（如 `D:\EPICS\CA-3.15.6-windows-x64`）

每个候选目录的失败原因都会写进 PV 健康明细，便于定位。只有自动发现全都失败时，才需要用
环境变量**显式指定**——注意显式指定时**只认那一个目录**，不会再回退（宁可报错也不静默换用
另一个版本不受控的 CA 库）：

```powershell
$env:SPECTRUM_CA_LIB_DIR = "D:\EPICS\CA-3.15.6-windows-x64"
```

## 2. 启动模拟 IOC

双击 `start-ioc.cmd`（或在该目录执行）。窗口保持打开就表示 IOC 在运行；
输入 `exit` 回车即可停止。

启动后会看到 PV 数据库加载与 `iocInit: All initialization complete`。
另开一个窗口执行下面命令可独立验证：

```cmd
caget Part1:Flow_W:CS200A:Setpoint
caput Part1:DW:Voltage_Set2 300
caput BD:MSScan:01:ScanStart 1
```

### 关于 `EPICS_CA_ADDR_LIST`（实测结论）

**本机通信不需要设置任何 `EPICS_CA_*` 环境变量。** 实测四种配置都能发现本地 IOC：

| CA 环境 | 结果 |
| --- | --- |
| 不设任何 `EPICS_CA_*` 变量 | ✅ 可发现 |
| `EPICS_CA_ADDR_LIST=127.0.0.1` + `EPICS_CA_AUTO_ADDR_LIST=NO` | ✅ 可发现 |
| 仅 `EPICS_CA_AUTO_ADDR_LIST=YES` | ✅ 可发现 |
| 仅 `EPICS_CA_ADDR_LIST=127.0.0.1` | ✅ 可发现 |
| **只设 `EPICS_CA_AUTO_ADDR_LIST=NO` 且不给地址表** | ❌ 找不到（唯一会失败的组合） |

所以正常用默认环境即可。`start-ioc.cmd` 里仍然设了
`EPICS_CA_ADDR_LIST=127.0.0.1` + `AUTO_ADDR_LIST=NO`，目的是让**在该窗口里运行的
`caget`/`caput` 被钉死在 localhost**，不会误连到真实束线；执行服务进程不需要这些设置。

## 3. 在客户端里用它

1. 先启动模拟 IOC（上一节）。
2. 启动客户端。启动初始化的 PV 健康检查应显示 **128 / 128 已连接**（健康检查只覆盖
   应用默认映射，不覆盖「仅模拟」台账补充 PV）；若显示未连接，见下方排查。
3. 打开「系统设置 → PV 映射」，表格应列出 `device_profiles.CLUSTER_SOURCE_ENTRIES`
   的 128 条映射，可直接增删改。台账补充的 22 个 PV（BD Reset/Error、JM:01/:07
   设定/开关、MSScan 与 MS:Scanz）不在映射表里，但模拟 IOC 上都能 `caget`/`caput`。
4. 改完点 **保存映射**，立即生效，无需重启执行服务。

回到「自动调束」页，PV 列应显示 `ioc.db` 里的 PV 名。

**看不到 PV 或显示未连接时**：

- 表格为空 + 提示「仪器执行服务不可达」→ 执行服务没起来。服务起来后点「重新载入」即可，
  映射不会丢。
- 表格有 128 行但全红 → 执行服务在跑但连不上 IOC。多半是 `ca.dll` 没加载成功，展开 PV 健康
  明细看每个候选目录的失败原因（见上一节的 CA 库说明）。
- 刚启动客户端时健康检查可能等上约 5 秒：设备访问走真实 CA，服务端第一次检查要等 IOC 是否
  在线。IOC 在的时候这条检查是毫秒级的。

## 4. 自检

```powershell
.\.venv\Scripts\python.exe sim\sim_check.py
```

它会：

1. 校验**默认 PV 映射**与**台账补充 PV** 与 `ioc.db` 严格一致（PV 名集合、记录类型
   `ao`/`ai`/`calc` 与「可写/镜像」标记对应、工程单位一致）；
2. 启动本地 `softIoc` 并建立真实 CA 连接；
3. 逐 PV 读值：普通记录对照 `ioc.db` 初值，`calc` 镜像记录对照设定信号当前值；
4. 对每个可写 PV 做写入读回往返，并验证「设定 → 回读」镜像联动；
5. 校验只读 PV 可读但被标记为不可写；
6. 改一条映射的 PV 名，验证热更新后立即读到新 PV 的值。

全部通过时退出码为 0，可直接接进 CI。当前 **723 项断言全部通过**。

**安全**：脚本强制 `EPICS_CA_ADDR_LIST=127.0.0.1` 且 `EPICS_CA_AUTO_ADDR_LIST=NO`，
只与本机模拟 IOC 通信，**不会碰到任何真实束线设备**。

## 5. PV 清单

`ioc.db` 由 `tools/generate_sim_ioc.py` 生成，来源是
`apps/instrument_service/device_profiles.py` 的两组条目：

### 5.1 应用默认映射（128 个，`CLUSTER_SOURCE_ENTRIES`）

与界面/健康检查/自动调束直接对应的受控信号，按设备分组（`ao` = 可写设定，
`ai` = 只读回读，`calc` = 镜像回读）：

| 分组 | 数量 | 覆盖 |
| --- | --- | --- |
| 气体流量 | 6 | Ar/He 流量设定、瞬时流量回读、阀门模式 |
| 溅射电源 | 6 | 功率设定（含 5K 量程）、开关、灭弧、功率/打弧速率回读 |
| 腔体气压 | 1 | 冷凝腔气压 `Part1:Sputtering` |
| 聚焦/漂移管 | 12 | JM 03/04 电压设定、输出开关、电压/电流回读；JM 01/07 电压/电流回读 |
| 高压阵列 DW | 52 | 13 路 ×（电压设定、输出开关、电压回读、电流回读） |
| 新高压电源 BD | 21 | 三圆筒1/2、电偏转1/2、主高压的设定/使能/回读 + 主高压电流设定 |
| 磁铁电源 | 28 | 4 路 ×（电流设定、速率设定、电流/高压回读、启动/停止/复位） |
| 束流探测 | 2 | FC1/FC2 束流电流 |

### 5.2 台账补充（22 个，`SIM_ONLY_ENTRIES`）

来自 demo 实机台账（`demo/argon_tuning/argon_tuning/`）但默认映射暂未纳入的 PV，
**只为让本地联调时台账里的每个 PV 都能 `caget`/`caput`**，不进健康检查与自动调束：

| PV | 含义 | 来源 |
| --- | --- | --- |
| `BD:FocusThreecyLinderHV:01:Reset` / `:Error` | 三圆筒1 复位 / 故障 | PV清单.md F 节（实机 13/13 连通） |
| `BD:FocusThreecyLinderHV:02:Reset` / `:Error` | 三圆筒2 复位 / 故障 | 同上 |
| `BD:MainHV:01:Reset` | 主高压 复位 | 同上 |
| `Part1:JM_POWER:01:SET_VOL` / `:OutPut` | JM01 电压设定 / 输出开关 | PV清单.md D 节（全 0 实测存在，含义待确认） |
| `Part1:JM_POWER:07:SET_VOL` / `:OutPut` | JM07 电压设定 / 输出开关 | 同上 |
| `BD:MSScan:01:ScanStart` / `ScanStop` | 质谱扫描启动 / 停止触发 | `_tmp/MSScan_v2.db`（cRIO IOC 已部署） |
| `BD:MSScan:01:StartCurrent` / `StopCurrent` | 扫描起始 / 终止电流（A） | 同上 |
| `BD:MSScan:01:WaitStep` / `WaitTimes` | 每步等待时间 / 等待次数 | 同上 |
| `BD:MS:Scanz:StartA` / `StopA` | LabVIEW VI 消费的起始 / 终止电流（A） | 同上 |
| `BD:MS:Scanz:BooleanStart` / `BooleanStop` | 扫描启动 / 停止布尔 | 同上 |
| `BD:MS:Scanz:RateSet` | 电流速率（A/s） | 同上 |
| `BD:MS:Scanz:WaitStep` / `WaitTime` | 每步等待时间 / 等待次数 | 同上 |

改了 `device_profiles.py`（任一组条目）就要重跑
`tools\generate_sim_ioc.py`，`sim_check.py` 会帮你发现不一致。

### 5.3 与「200 多个 PV」的差距

当前 demo 台账能**确认 PV 名**的完整集合就是上面这 150 个。`PV清单.md` 的「待补充」部分
（JM 其余通道、溅射显示类字段、状态类字段、CS30 束诊等）**没有给出 PV 名，无法凭空推断**，
所以没有纳入。如果实机 IOC 确实已有 200+ 个 PV，把实机 IOC 的 `st.cmd`/`.db` 导出
（或 `caget` 全量清单）补充进 `device_profiles.py`（或直接给出清单）后重跑生成即可扩展，
本流程不需要任何结构性改动。

## 6. 两处「必须保持 ASCII」的约束

这两个文件里的说明文字刻意只用英文，不是偷懒，而是各自有硬性原因：

| 文件 | 原因 |
| --- | --- |
| `ioc.db` | EPICS 的 `dbLoadRecords` 不保证能正确处理 db 文件中的非 ASCII 字节，中文注释可能导致加载异常或乱码（生成器也会在写出前检查并拒绝非 ASCII） |
| `start-ioc.cmd` | `cmd.exe` 按 **OEM 代码页**（中文 Windows 为 936）解析 `.cmd` 文件。UTF-8 的中文字节会让解析偏移错位，把后面的语法撕碎，典型报错是 `'H"=="" (' is not recognized as an internal or external command`。同样原因也不要在脚本里用 `chcp 65001` 去救——那会进一步改变解析行为 |

仓库里既有的 `deploy/packaging/build-all.cmd` 同样是全 ASCII（0 个非 ASCII 字节），遵循同一约定。
中文说明统一写在本文件里。`sim_check.py` 是 Python 源文件，UTF-8 中文没有任何问题。

## 7. 已知限制

这是**通信链路**的模拟，不是物理模型：

- **没有设备限值**：`ao` 记录未设 `DRVH`/`DRVL`，越界设定值照样接受。应用侧目前也还没有
  参数边界校验，所以「写越界值」这条路径暂时测不出拦截行为。
- **没有物理耦合**：`calc` 镜像回读只跟随设定值，不会模拟真实设备的动态响应；其余只读值
  是静态初值。用它跑自动调束时，优化目标不会有真实响应。
- **只在内存里**：写入的新值存在于 IOC 进程内存中，重启 IOC 后回到 `ioc.db` 的初值。
- **只有 CA**：用的是 Channel Access（`ca.dll`），没有 PVA/pvAccess。EPICS base 里的
  `softIocPVA.exe` 本模拟未使用。
- **单一 IOC**：所有 PV 在同一个 IOC 上，未模拟多 IOC / 网关 / 网络分区等现场情况
  （实机是主 IOC 192.168.1.133 + cRIO 192.168.1.101 等多个 IOC）。
- **台账补充 PV 不参与健康检查**：`SIM_ONLY_ENTRIES` 只保证模拟 IOC 上有这些 PV 可读写，
  应用不会把它们计入健康检查、扫描或调束。
