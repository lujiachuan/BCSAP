# UI 改造 M1 交付报告：AppStatusModel 状态统一 · 初始化降级进入 · 主壳/图标/性能

> 依据 `docs/UI体验审查与界面改造方案.md`（v2）执行 M1。
> M1 出口：状态单一来源；初始化与降级进入可跑通；键盘路径可用；
> 真机走查（键盘顺序 / 失败与重试实感 / DPI 图标）后签字进入 M2。

---

## 1. 本阶段改动范围

| 类型 | 文件 | 说明 |
|---|---|---|
| 新增 | `apps/desktop_client/status_model.py` | `AppStatusModel`：服务/步骤/PV/同步/操作资格单一状态源；`updated` 信号发布；状态写入即记录时间 |
| 新增 | `tests/test_status_model.py` | 模型纯逻辑单测（状态、资格推导、阻塞原因、重试键集、reset） |
| 改造 | `apps/desktop_client/initialization.py` | Worker 目标化（可单项重试，不重复已成功步骤）+ `pvStatus` 明细信号 + 失败文案；初始化页：步骤行内联“重试”、底部动作行（进入/离线进入/打开设置） |
| 改造 | `apps/desktop_client/main.py` | 状态接线全量改为模型驱动；导航资格由 `model.can_control` 推导；初始化动作（重试/进入/设置）接线；服务明细弹窗（工作台状态卡双击）；主题切换去全树 unpolish（改 update 级）+ 矢量符号按钮 |
| 改造 | `apps/desktop_client/nav_icons.py` | 按 devicePixelRatio 绘制（高 DPI 不发虚）；新增 ☾/☀/☰ 矢量符号（不再依赖字体码位） |
| 改造 | `apps/desktop_client/widgets.py` / `pages.py` | 新增 `ClickableLabel`；服务状态卡支持双击查看明细 |
| 改造 | `apps/desktop_client/theme.py` | 新增 `inlineRetry`（步骤行重试链接样式） |
| 测试 | `tools/contrast_audit.py` 回归 | 40 条对比度仍全通过（本轮未改色值） |

## 2. AppStatusModel 状态定义（M1 交付物 §10）

| 分组 | 字段 | 说明 |
|---|---|---|
| 初始化 | `phase`（starting/entered/finished）、`essential_ready` | 必要条件完成后置位 |
| 步骤 | `_steps[config/instrument/pv/data/sync] = {state, detail, at}` | 状态 + 说明 + 时间 |
| 服务 | `_services[data/instrument/epics/cache] = {state, text, detail, at}` | good/warn/error/idle/running |
| PV | `pv_total / pv_connected / pv_details` | 失败明细列表（名称：原因） |
| 同步 | `sync_current / sync_total / sync_determinate` | 未知总量时置不确定（预留） |
| 资格 | `can_control`、`blocking_reasons` | 由 instrument+epics 推导，页面不再自行拼条件 |

视图接线（订阅关系）：

```text
                AppStatusModel
   ┌───────────────┼─────────────────┐
   ▼               ▼                 ▼
侧栏底部(3 服务)  工作台(4 状态卡+Hero)  初始化页(步骤+动作行)
        └──────────┴──────────┘
       导航资格 can_control → 扫谱/调束入口启用与禁用原因
```

## 3. 初始化流程变化（对照方案 §3.2）

- **并行与目标化**：数据服务、仪器执行两项连接检查独立；仪器可达后检查全部 PV；
- **先进入、后同步**：必要条件检查完成即解锁导航并显示“进入工作台”，大批量同步继续后台执行（cache 状态持续刷新）；
- **降级进入**：仪器不可达仍可进入；扫谱/调束禁用并展示阻塞原因（Hero 文案 + 导航 tooltip）；
- **单项重试**：失败步骤行出现“重试”，只重跑对应目标（pv/仪器→instrument，data/sync→data），不重复成功步骤；任务运行期间重试禁用；
- **失败可定位**：PV 状态含“已连接/总数 + 最近检查时间 + 逐项明细（PV 名称：原因）”，工作台 EPICS 状态卡双击弹出明细；
- **进度真实性**：只有知道总量才显示百分比（现有下载流程均确定总量）；
- 服务全不可达时动作行为“进入（离线）”；部分失败为“离线进入”。

## 4. 主壳 / 图标 / 性能

- 图标：☾☀☰ 改为 QPainter 矢量（月/日/菜单），不依赖字体码位；导航与符号图标按 `devicePixelRatio` 建画布并 `setDevicePixelRatio`；
- 主题切换：移除全控件 unpolish/polish（改为样式表自动重刷 + 自绘控件 update 级刷新 + 模型驱动重配色），显著降低切换开销；
- 服务状态卡可交互：双击“EPICS/数据/仪器/本地”卡片查看当前状态、时间与失败明细。

## 5. 验证结果

| 项 | 结果 |
|---|---|
| 编译 `compileall` | 通过 |
| 全量单测 `unittest discover`（含新增模型测试） | 25 项 OK |
| 对比度 `tools/contrast_audit.py` | 40/40 通过 |
| 冒烟 A：Worker 坏端口路径（错误状态 / essentialReady / completed False / PV 失败说明） | 通过 |
| 冒烟 B：界面失败→“进入（离线）”+ 失败行重试可见；就绪→“进入工作台”+ 扫谱/调束入口启用 | 通过 |
| 截图 `--tag m1` | 双主题 18 张 `docs/ui-review-snapshots/m1/` |

## 6. 真机键盘走查清单（M1 出口需确认，逐项勾选）

- [ ] Tab 顺序：初始化页（重试按钮 → 进入/设置）→ 侧栏导航 → 工作台按钮/状态卡 → 服务设置；
- [ ] 仅键盘可完成：进入工作台、切换页面、扫谱参数填写（M2 覆盖）、设置保存/测试、打开状态明细（双击为鼠标操作，键盘等价入口：EPICS 卡按 Enter/F2 打开明细——未实现，记入遗留）；
- [ ] 双击状态卡弹窗可读（含失败 PV 明细与时间）；
- [ ] 失败时“重试”行内按钮命中区 ≥ 22px，视觉可辨识；
- [ ] 图标（☾/☀/☰）在 100/150/200% DPI 无模糊，无“豆腐块”；
- [ ] 主题切换在实机无明显闪烁（M1 性能重构效果）。
- [ ] M1 出口签字（此页确认后进入 M2）。

## 7. 遗留与取舍（M1 阶段内未做，已记录）

- 键盘打开服务明细的等价入口（Enter/F2 键）待 M2 随业务页键位统一处理；
- 未知总量同步的不确定进度条尚未启用（现有流程总量已知）；
- PV 明细目前来自工作台卡片双击弹窗；系统设置 PV 页的联动展示归入 M2 设置页改造；
- 卡片入口动画/状态点脉冲归 M3（动效规范 §6）。

## 8. 下一步（M2 预告）

扫谱页 pyqtgraph `SpectrumPlot`（刻度/缩放/十字读数/峰值标注/大数据策略）与页面重排；
自动调束三阶段步骤条与结果对比；系统设置响应式布局与 PV 页联动；占位页卡片化；核心回归。

## 9. 已知边界（有意未做）

- 真实设备联调等待不属本期；服务协议字段以现有 instrument/data 接口为准；
- Worker 网络请求仍在 QThread 内同步执行（连接/下载不阻塞 UI）；并行化两路检查（方案 §3.2 并行）在真实服务接入阶段再评估（当前单线程 QThread 串行执行亦不阻塞 UI）。
