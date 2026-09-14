# demo 功能对齐与改造报告

> 对象：`demo/argon_tuning`（旧 Tkinter 原型）→ `apps/` + `packages/`（spectrum-platform 新平台）
> 范围：手动控制测试、自动调束、扫谱三大功能的对齐与实现
> 依据：`docs/软件总体架构与实施设计.md`、`docs/平台架构与技术选型方案.md`

---

## 1. demo 是什么

### 1.1 一句话

`demo/argon_tuning/argon_tuning/cluster_control_gui.py` 是一套**已上真机的团簇离子源束流调试软件**——不是纯界面原型。它把装置几十路 EPICS PV 收进一个 tkinter 面板，做手动调参、贝叶斯自动调束和质谱扫描采集。

### 1.2 启动方式

| 方式 | 文件 | 说明 |
|---|---|---|
| 主入口 | `cluster_control_gui.py`（4076 行 / 206 KB） | 唯一主程序，"统一控制面板 v2" |
| 双击 | `启动控制系统.bat`、`启动氩气控制面板.bat` | 都指向上面那个脚本 |
| 命令行 | — | `python cluster_control_gui.py`（`--smoke` 自检 / `--read-only` 只读 / `--backend ca\|pva`） |

> ⚠️ `.bat` 里**写死了原开发机解释器路径** `C:\Users\sciart\AppData\Local\Programs\Python\Python314\python.exe`，本机不存在，双击会失败。

配套启动器（数据链路，不是主程序）：`启动数据监听.bat` → `labview_listen.py`（监听 8905）；`启动LabVIEW曲线.bat` → `labview_xy_viewer.py`；`启动数据转发_可换目标.cmd` → `tcp_relay_config.ps1`（跑在 1.7 中继机上，8904 → 目标机）。

### 1.3 装置与网络拓扑（`PV清单.md` 实测）

| 地址 | 角色 |
|---|---|
| 192.168.1.133 | 全系统主 IOC（DW 13 路 / 气体 / 溅射 / JM / BD 5 路 / 磁铁 / FC） |
| 192.168.1.7 | LabVIEW 中继机（同一套 PV 可达） |
| 192.168.1.101 | cRIO-9054 softIoc（`MSScan.db` 部署在此） |
| 172.16.14.123 | 预研装置 IOC（最早验证的那台） |

**优化目标**：`BD:FC:01:BeamCurrent` / `FC2`（法拉第杯束流电流，最大化）。

### 1.4 四个页签

| 页签 | 内容 |
|---|---|
| **① 手动控制** | 顶栏 FC1/FC2 大字 + 目标电流趋势图；三列排布：左=气体流量(Ar/He 0–500 sccm)/溅射电源 JSPow/聚焦漂移 JM_POWER，中=**DW 13 路高压阵列**，右=**BD 新高压电源 5 路** + **磁铁电源 4 路**。行控件范式：`回读 \| Spinbox \| 写入 \| ±10 \| ±1`，改值即写、不弹确认 |
| **② 贝叶斯调参** | 目标 FC1/FC2 单选；变量表（勾选 + `[min,max]` + 当前设定/实际回读）；**探测器联动**（按 FC1–FC5 自动勾选上游参数）；两种策略（① 单参数逐一→联合微调 ② 全联合）；10 项调参参数；参数响应 XY 图；监控建议区；调参日志 |
| **③ 质谱扫描数据采集** | LabVIEW 实时（磁铁电流 vs 皮安表，TCP 8904→8905）；MSScan 起停与参数（改值自动写）；停止后回落；LabVIEW 程序 Run/Abort；画布交互（拖框放大/右键平移/滚轮/复位）；**X 轴切换 电流 I(A) ↔ 质量 Mass(u)**（`Mass = a0 + a1·I + a2·I² + a3·I³`，系数可编辑）；导出 JSON(全参数)/TXT(仅电流)，命名 `元素-序号-Ar-He-气压Pa-PowerW-cm` |
| **④ 参数上限** | 所有可调参数的写入上限，可编辑保存到 `limits_config.json` + 追加 `limits_history.jsonl`，保存后立即生效；支持恢复内置默认 |

### 1.5 算法与安全

- 调参引擎：`skopt.gp_minimize`（GP + EI）；无 skopt 时回退**自包含 sklearn GP+EI**（`_gp_minimize_self`）
- `run_sequential` 两阶段：阶段 1 逐变量优化 + 保持 `hold_wait` 稳定 + **束流归零自动回退上一路并抬高下限重调**；阶段 2 各最优点 ± 原范围 × `joint_frac`/2 联合微调
- 安全：写值 clamp 到范围、按 `max_step` 斜坡写入、`stop_event` 随时停止、结束/异常回安全值、只读模式、全部关断（带确认）

### 1.6 辅助脚本

`pv_access.py`（统一 CA/PVA 后端，**无 monitor/subscribe**，`read_pv.py --monitor` 是 0.5 s 轮询假装的）、`labview_listen.py`、`labview_live.py`、`labview_xy_viewer.py`、`lv_control.py` / `lv_click.py`（SSH + PsExec 在会话 2 做**像素级点击**控 LabVIEW Run/Abort）、`mscan_relay.py`（PV 上升沿 → 自动点击）、`tcp_relay_config.ps1`、`argon_ioc.py`（PVA 服务器）、`bayes_tune_argon.py`、`argon_flow_gui.py`、`read_pv.py`、`probe_pvs.py`。

### 1.7 demo 自身的问题（迁移时不要照抄）

1. **界面直接 `pyepics.put()` 写设备**——没有执行层校验、没有设备锁、没有审计。
2. **`tuning_logs/*.jsonl` 只有 `{ts, trial, phase, msg}`**——是阶段与消息日志，**不是逐 trial 数值记录**。"建议值→写入→回读→目标电流"那些数字只在界面上滚过，**没有落盘**，调参复现能力接近零。
3. **`labview_listen.py` 与 `labview_live.py` 争写同一个 `latest_labview.json` 且 schema 不兼容**（`profile[]` vs `history[]`），同跑会让曲线查看器间歇读到没有 `profile` 的快照。
4. **`lv_control.py` / `mscan_relay.py` 依赖 1920×1080 会话 2 的硬编码像素坐标**，分辨率或窗口布局一变就失效。
5. **`db/argon_flow.db` 的 `DRVH=100/DRVL=0` 与实际 0–500 sccm 量程不一致**（残留的小量程演示值）。
6. **代码内置默认上限与实际生效值不一致**——现场把 JM3/JM4 与 DW 其余通道收到 5100 V、主高压收到 50 kV、磁铁速率 10 A/s，都记在 `limits_config.json` 里，比代码里的 `LIMITS_DEFAULT` 更紧。

---

## 2. 新平台改造前的状态

| 层 | 改造前 |
|---|---|
| `packages/optimizer/` | **空包骨架**，只有一句"现有 demo 中的优化器将迁移到这里" |
| `apps/instrument_service/app.py` | **只有 4 个端点**：`status`、`health/live`、`pvs/health`、`pv-mapping` GET/PUT。**没有任何读写 PV 值的端点** |
| `packages/epics_adapter/` | `EpicsGateway` 协议**已有** `connect/read/write/snapshot`，真实 CA 与模拟实现都已实现 `write` |
| `pv_mapping` 默认配置 | 7 条 `BL:*` 信号（Q1/Q2/Einzel/steerer/source/detector） |
| 扫谱页 `scan.py` | 页面内 `QTimer` + `_signal()` 公式造数据，**不调服务、不落盘** |
| 调束页 `tuning.py` | 页面内 `_next_iteration()` 拟合曲线 + 随机噪声，硬编码最佳值 `19.08`，全程标注"（模拟）" |
| `packages/domain/scan.py` | 已有 `ScanState` + 合法转换表 |

**结构性结论**：底层能力齐全，**中间那截 API 是断的**。而架构文档第 68 行明确规定"仪器执行服务是硬件操作的唯一应用入口"——所以 demo 那种 GUI 直连 pyepics 的做法在新平台是被架构禁止的，这是搬功能时最大的改造点。

---

## 3. 功能对齐表

### 3.1 页签 ↔ 页面

| demo | 新平台 | 对齐方式 |
|---|---|---|
| **① 手动控制** | **手动控制**页（新增，`pages/manual.py`） | 新增模块。新平台原本没有手动操作入口 |
| **② 贝叶斯调参** | **自动调束**页 + `packages/optimizer/` + `tuning_service.py` | 沿用页面骨架（三 Tab），引擎与服务端全新实现 |
| **③ 质谱扫描数据采集** | **扫谱**页 + `scan_service.py` + `scan_store.py` | 沿用页面骨架与 PyQtGraph 绘图 |
| **④ 参数上限** | 并入**设备配置**（`PvMappingEntry`） | 概念合并：上限成为映射条目字段，由执行层强制 |

### 3.2 控件/能力迁移对照

| demo 能力 | 新平台落地 |
|---|---|
| 回读｜Spinbox｜写入｜±步进 行范式 | `manual.py` 保留 demo 的**单页三列版式**，一行一个设备：`设备 \| 设定 \| 下发 \| 回读 \| 输出`（列宽写死、行高统一 28px） |
| ±1/±10 快捷设定值按钮 | **不再提供**（用户反馈按钮过于分散）。改为设定框**滚轮调值**，步进档 `×1 / ×10 / ×100` 放在**每张卡片表头**（每组独立）；基准步长取量程 1/500 的 1-2-5 整数，三档即量程的 **0.2% / 2% / 20%**（0–500 sccm → 1/10/100；0–50 kV → 0.1/1/10；0–5100 V → 10/100/1000），小数位比步长多留一位。分母取 500 是为了让最粗一档不超过量程 25%——否则一格必然被端点夹住，三档看不出差别（见 `docs/手动控制页目标电流趋势与界面优化调研.md` §7.6） |
| 一路信号一行 | **按设备折行**：`hv_array.dw04` 的电压设定/输出使能/电压回读/电流回读合成一行，128 路信号折成 30 行（+2 路 FC 在顶栏） |
| 按设备组分页签 | **不做**：三列按 PV 命名空间分列（`hv_array` 独占中列，`hv_bd`/`magnet` 右列，其余左列），与 demo 一致 |
| 改值即自动写入 | **改为必须点「下发」**——高压装置上"碰一下就下发"是事故来源；滚轮误滚也只改了一个待确认的数字 |
| 开关只有开/关两键 | **开/关一对按钮**（选中态跟随实际回读，实际开着而按钮显示关，点一次就误发关断）；量程 >1 的整数模式（如气流模式 0–2）退化为整数框 |
| 全部关断 | `_all_off_plan()`：**先断输出、再退设定值**；脉冲信号不参与（写 0 无语义）；结束时报告被拒项数 |
| 顶栏 FC1/FC2 大字 | 保留：`detector` 命名空间的只读量进顶栏大字读数（demo 顶栏就是 FC1/FC2 电流） |
| 探测器联动勾选上游参数 | 暂未迁移（见 §7） |
| 两种调参策略 | 暂未迁移（见 §7） |
| 参数响应 XY 图 | 调束页「目标量收敛」曲线（x = 轮次，y = 目标测量） |
| 调参日志（仅界面滚动） | **`tuning_iterations` 表**：候选/下发/回读/目标/质量全部落盘 |
| Mass = 多项式(I) 可编辑系数 | 扫谱页「X 轴显示质量」，**仅影响显示**；落盘始终是电流实际值 |
| 导出 JSON(全参数)/TXT | 服务端 NPZ + SHA-256（`spectrum_contracts` 存储路径） |
| 导出命名 `元素-序号-Ar-He-气压-PowerW-cm` | 暂未迁移（见 §7） |
| limits_config.json 现场值 | 写进 `device_profiles.py` 的 `max_value` |

### 3.3 关键架构差异

```
demo:        Tkinter GUI ──(pyepics 直接读写)──► IOC
             无校验、无锁、无审计

新平台:      PySide6 GUI ──HTTP──► instrument_service ──► 执行层校验 ──► CA ──► IOC
             ▲ 界面不直连 EPICS        ▲ 硬件写入的唯一入口
                                       ├ 边界 / 最大单步 / 变化速率
                                       ├ 设备组锁（按共享资源分组）
                                       ├ 状态机（扫谱 / 调束）
                                       └ 本地暂存（SQLite + NPZ + SHA-256）
```

---

## 4. 改造内容（逐文件）

### Phase 0 · 读写链路地基

| 文件 | 内容 |
|---|---|
| `packages/contracts/signals.py`（新增） | `SignalReading` / `SignalSnapshot` / `SignalWriteRequest` / `SignalWriteResult` |
| `packages/contracts/pv_mapping.py` | `PvMappingEntry` 扩出 `group`、`role`、`readback_signal`、`min_value`、`max_value`、`max_step`、`max_rate`、`settle_tol`、`settle_timeout`（全部可选，旧配置照常读入） |
| `apps/instrument_service/signal_io.py`（新增） | **写执行层**：`_execute` 按「边界 → 最大单步 → 变化速率」校验；斜坡分步（`MAX_RAMP_STEPS=128`）；写后读回；`command_id` 幂等；`device_state_unknown` 标记；`wait_settled` |
| `apps/instrument_service/device_profiles.py`（新增） | 团簇源 **128 个信号**，按 8 组（气体流量 6 / 溅射电源 6 / 腔体气压 1 / 聚焦·漂移管 12 / DW 13 路 52 / BD 5 路 21 / 磁铁电源 28 / 束流探测 2），其中可写 69 |
| `apps/instrument_service/app.py` | `POST /control/v1/signals/read`、`POST /control/v1/signals/write` |
| `apps/instrument_service/runtime.py` | 读写服务随网关/配置热更新重建；`gateway_factory` 测试注入位 |
| `sim/ioc.db` | 重新生成 128 条记录 |
| `tools/generate_sim_ioc.py`（新增） | ioc.db 改为**生成**，两份配置无法再漂移 |

### Phase 1 · 手动控制页

| 文件 | 内容 |
|---|---|
| `apps/desktop_client/instrument_api.py`（新增） | 客户端读写入入口；**写操作全局串行**（并发写会让两条斜坡交错成谁也不是的曲线） |
| `apps/desktop_client/pages/manual.py`（新增） | **单页三列**（对齐 demo，不按设备组分页签）：顶栏 FC1/FC2 大字 + 步进档 + 下发结果消息区；左列气体/溅射/真空/聚焦、中列 DW 13 路、右列 BD + 磁铁；一行一个设备（`设备 \| 设定 \| 下发 \| 回读 \| 输出`，统一 28px 行高）；设定框支持滚轮；开/关按钮跟随实际状态；1 s 轮询快照；全部关断队列；服务不可达时整体禁用 |
| `apps/desktop_client/main.py` | 「手动控制」纳入**控制资格**判定与**退出保护**（全部关断进行中退出会拦截确认） |
| `apps/desktop_client/nav_icons.py` | 新增 `control` 仪表盘图标 |

### Phase 2 · 扫谱

| 文件 | 内容 |
|---|---|
| `packages/contracts/scan.py`（新增） | `ScanAxis`（支持单路/成组）/`ScanRunRequest`/`ScanPoint`/`ScanRunStatus`/`ScanPointsResponse` |
| `apps/instrument_service/device_locks.py`（新增） | 设备组锁：**一次性全取或全不取**（文档 6.3） |
| `apps/instrument_service/scan_service.py`（新增） | 状态机 + 逐点流程：算目标 → 边界检查 → 写 → **等读回稳定** → 积分采样（中位数）→ 存**实际坐标**/信号/时间/质量 → 发进度 |
| `apps/instrument_service/scan_store.py`（新增） | 本地暂存：SQLite 元数据 + NPZ **临时文件→fsync→原子改名** + SHA-256（文档 7.3） |
| `apps/instrument_service/app.py` | 4 个扫谱端点 + `acknowledge` |
| `apps/desktop_client/pages/scan.py` | 重写：提交任务给服务、轮询增量点、扫描轴单路/成组、Mass 换算（仅显示）、**暂停按文档 9.3 关闭** |

### Phase 3 · 自动调束

| 文件 | 内容 |
|---|---|
| `packages/optimizer/bayes.py`（新增） | `GpEiOptimizer`：GP 回归 + EI 采集函数，**只依赖 numpy + math** |
| `packages/optimizer/__init__.py` | 从空骨架改为导出实现 |
| `packages/domain/tuning.py`（新增） | `TuningState` + 合法转换表（比扫谱多一个 `AWAITING_CONFIRMATION`） |
| `packages/contracts/tuning.py`（新增） | `TuningVariable`/`TuningRunRequest`/`TuningProposal`/`TuningIteration`/`TuningRunStatus` |
| `apps/instrument_service/tuning_store.py`（新增） | `tuning_runs` + `tuning_iterations` 两张表 |
| `apps/instrument_service/tuning_service.py`（新增） | confirm 模式状态机：建议 → 人工确认 → 执行层校验 → 写 → 读回 → 记录 |
| `apps/instrument_service/app.py` | 6 个调束端点 |
| `apps/desktop_client/pages/tuning.py` | 重写：变量表由设备映射生成、待确认候选 + 「确认并执行」、真实轮次曲线与对比表；`changes_table` 由局部变量升为实例属性 |

---

## 5. 架构约束遵循情况（实测）

| 约束（架构文档） | 落实 | 证据 |
|---|---|---|
| 执行服务是硬件写入的唯一入口 | `signal_io.py` 是唯一写路径；界面只发 HTTP | 客户端源码中**零** `import epics/p4p` |
| `packages/optimizer` 不含 PySide6/FastAPI/SQLAlchemy/EPICS | 实测 import 仅 `numpy/math/dataclasses/typing/__future__` | AST 扫描脚本输出 |
| 写入经执行层边界/单步/速率校验 | `SignalWriteService._execute` + `_plan_ramp` | 越界 600 sccm 被拒且设备不动；400 sccm 拆 8 步 |
| 任何模式不能绕过执行层校验 | `TuningService._apply` 一律走 `signals.write(...)` | 调束的候选也受同一套校验 |
| 设备组锁按共享资源分组 | `DeviceLockManager` + `group` 字段 | 四路磁铁、束流探测分组；扫谱持锁期间调束启动失败 |
| 调束第一版保留「建议→人工确认」 | `SUPPORTED_MODES = ("confirm",)`；其余**显式拒绝**并指向文档 6.6 | 在线自检「候选生成后设备未被改动」 |
| 每轮保存算法版本与随机种子 | `tuning_runs.algorithm/algorithm_version/seed` | `gp-ei / 1.0 / seed=0` |
| 建议值 ≠ 已执行值 | `TuningIteration` 分开存 `proposed`/`applied`/`readback` | 表内三列独立 |
| 结果未知时不自动重试 | `device_state_unknown` → `RECOVERY_REQUIRED`，**保持设备锁** | 需 `acknowledge` 才释放 |
| 硬件联锁由硬件/IOC 承担 | **应用层未实现联锁**，代码中明确注明不假装实现 | `tuning_service.py` 模块 docstring |
| 谱图 NPZ 格式 | 复用既有 `packages/spectrum/codec.py`（`format_version/x/y` + SHA-256） | NPZ 解出的 x 与接口上报 SHA-256 一致 |

---

## 6. 验证方式与证据

### 6.1 自动化测试

```
.\.venv\Scripts\python.exe -m unittest discover -s tests
Ran 211 tests ... OK
```

新增测试文件：
- `tests/test_signal_io.py`（35）—— 越界/单步/速率/斜坡/幂等/干跑/稳定判据/惰性建连
- `tests/test_manual_page.py`（23）—— 行类型按 role、步长推导、全部关断序列、忙时提示
- `tests/test_scan_service.py`（32）—— 逐点流程、失败语义、恢复锁、停止、参数校验
- `tests/test_scan_page.py`（18）—— 质量换算仅显示、终态补齐、排除点不绘图
- `tests/test_optimizer.py`（14）—— 边界、确定性、**收敛性**、耗时守卫
- `tests/test_tuning_service.py`（22）—— confirm 模式、范围越界、模式守卫、恢复锁

### 6.2 在线自检（offscreen 跑真实页面代码 → 服务 → CA → IOC）

| 脚本 | 覆盖 |
|---|---|
| `tools/check_signal_chain.py` | 真实 CA 连通、快照、区间内写入并确认 IOC 变化、越界拒绝且设备不动、斜坡分步、只读拒绝 |
| `tools/check_scan_page_live.py` | 16 项：暂停关闭、点数与服务端一致、**x 是实际回读**、落盘 NPZ 可解、质量换算不改落盘 |
| `tools/check_tuning_page_live.py` | 20 项：模式固定 confirm、**候选生成后设备未被改动**、建议/下发/回读分开、算法版本与种子、完成后释放锁 |

三个脚本全部通过。

### 6.3 本地运行栈

```
1) 模拟 IOC（带回读耦合）
   D:\EPICS\base\bin\windows-x64-mingw\softIoc.exe -d sim\ioc.db

2) 执行服务
   $env:EPICS_CA_ADDR_LIST="127.0.0.1"; $env:EPICS_CA_AUTO_ADDR_LIST="NO"
   $env:SPECTRUM_SCAN_STORE="D:\BCSAP\build\scan_spool"   # 可选，覆盖暂存目录
   .\.venv\Scripts\python.exe -m apps.instrument_service.main

3) 客户端
   .\.venv\Scripts\python.exe -m apps.desktop_client.main
```

> `sim/ioc.db` 里配对的回读信号用 `calc` 记录镜像设定值（`field(INPA, "... CP")`）。
> 用 `PP` **不生效**——`PP` 是反方向，回读会一直停在初始值，扫描的每个点都被判"未稳定"。

---

## 7. 尚未迁移 / 待确认

### 7.1 未迁移的 demo 能力

| 能力 | 状态 |
|---|---|
| 探测器联动（按 FC1–FC5 自动勾选上游参数） | 未迁移 |
| 调参策略（① 单参数逐一→联合微调 / ② 全联合） | 未迁移；当前只有联合优化 |
| 束流归零自动回退重调（`max_retry`） | 未迁移 |
| 导出文件名规则（元素-序号-Ar-He-气压-PowerW-cm） | 未迁移；样品/元数据仍属占位页 |
| LabVIEW 链路（TCP 8905 采集、8904 转发、SSH 像素点击） | 全未迁移（demo 的 `latest_labview.json` 争写与硬编码坐标两个问题也不应照抄） |
| MSScan PV 起停与"停止后回落" | 未迁移 |
| 连续自动调束 | **按文档 6.6 刻意不开放**，需另行安全评审 |
| 扫谱暂停 | **按文档 9.3 刻意关闭**（需确认现场允许"让磁场停在某电流上"） |

### 7.2 待确认

1. **联锁**：文档明确由硬件/IOC 承担，应用层未实现。需确认现场 IOC 侧联锁配置。
2. **稳定判据取值**：`settle_tol`/`settle_timeout` 目前按 demo 的 `--settle-tol/--settle-wait` 填（气体 2 sccm/30 s，磁铁 0.5 A/60 s），需按设备实际响应复核。
3. **暂存目录**：默认 `%LOCALAPPDATA%\SpectrumPlatform\spool`；受限环境下用 `SPECTRUM_SCAN_STORE` 覆盖，不可写时返回 503 + 明确指引。
4. **`role` 推导规则**：`device_profiles.role_for()` 按命名后缀归类，新增信号名若无法归类会直接抛错（有测试保护），需要时补规则。
5. **demo 的现场限值**：已写进 `device_profiles.py`，但那是 demo 时点的值，真机接入前需复核。

### 7.3 建议的下一步

1. 真实装置接入前，先用 `sim/` 跑一遍完整流程（手动 → 扫谱 → 调束），确认操作手感。
2. 按文档 6.6 的分阶段计划，评估是否开放"自动写入但每轮确认"。
3. 补齐 §7.1 中与实验流程相关的项（探测器联动、单参→联合策略、导出命名）。
4. 样品/谱图库/同步等占位页接入 `data_service` 后，把落盘谱图接上中央库。

---

## 附：本文档的事实边界

- demo 与装置的当前连通性：**192.168.1.x 网段全部不通**（主 IOC / LabVIEW 中继 / cRIO），本机环境下 `172.16.14.123:5064` 可连但只命中 `Part1:Sputtering` 一个 PV。因此本轮所有验证都在 `sim/` 模拟 IOC 上完成，**未做真机联调**。
- 本机 `.venv` 只装了 `numpy` 与 `pydantic`（+ FastAPI/PySide6）；**scipy / sklearn / skopt 均不可用，且 PyPI 不可达**。调束算法因此实现为 numpy 自包含版本。
- 未创建任何 git commit，改动均在工作区。
