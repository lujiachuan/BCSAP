# UI 改造 M0 交付报告：令牌 v2 · 对比度修复 · 表面体系样板

> 依据 `docs/UI体验审查与界面改造方案.md`（v2 可执行版）执行 M0。
> M0 出口标准：对比度通过；L0/L1/L2 表面样板；标题栏着色 POC；工作台双主题样板；
> 真机确认玻璃边界、标题栏与 DPI 后由负责人签字进入 M1。

---

## 1. 本阶段改动范围

| 类型 | 文件 | 说明 |
|---|---|---|
| 新增 | `apps/desktop_client/ui_tokens.py` | 设计令牌 v2：颜色（含表面/玻璃/语义弱底/环境光）+ 非颜色尺度（字号/圆角/间距/动效）；LIGHT/DARK 键集合断言一致 |
| 新增 | `apps/desktop_client/surfaces.py` | L0 `AmbientCanvas`（环境光渐变 + 弥散光晕）；L2 `GlassCard`（自绘：外圈光晕→半透明填充→顶部高光带→1px 描边；不使用 QGraphicsEffect） |
| 新增 | `tools/contrast_audit.py` | 正式对比度核算工具：正文 ≥4.5、大字号/图形 ≥3；玻璃表面按 alpha 合成到最差环境光底核算；失败退出码 1 |
| 重构 | `apps/desktop_client/theme.py` | 令牌装配层：QSS 由 ui_tokens 生成（字号/圆角/间距走尺度令牌）；L0 透明化；焦点统一 1px 换色不跳动；表格 hover/滚动条/菜单/Tab/进度等强化；QPalette 选中色用 accentSolid |
| 改造 | `apps/desktop_client/pages.py`（WorkbenchPage） | M0 样板：Hero 玻璃卡（模式/可执行性/阻塞原因/下一步）+ 4 张服务状态玻璃卡 + 最近实验 L1 面板 + 快捷操作玻璃卡；保留全部对外 API |
| 改造 | `apps/desktop_client/widgets.py` | Panel 对齐新间距；MetricCard 迁移为玻璃卡；状态点样式 |
| 改造 | `apps/desktop_client/main.py` | 主壳改 `AmbientCanvas` 舞台；页面滚动链透明化（viewport 不自动填充）；接入 DWM 标题栏着色并在主题切换时重刷 |
| 改造 | `apps/desktop_client/windows_chrome.py` | 令牌化：由当前主题驱动标题栏/边框/文字颜色与深浅模式，异常静默回退 |
| 改造 | `tools/ui_snapshot.py` | 支持 `--tag <子目录>`，生成可对比的改版前后截图组 |

## 2. 对比度修复结果（`tools/contrast_audit.py` 全量 40 条通过）

| 配对 | 修复前 | 修复后 | 备注 |
|---|---:|---:|---|
| 浅色·侧栏分组文字 | 2.30（不通过） | **4.59** | navMuted 加深 |
| 浅色·导航选中项文字 | 4.39（待修） | **5.89** | 选中底提亮 + 选中字用 accentHover |
| 浅色·警告色 | 3.49（不通过） | **5.26** | statusWarn 加深 |
| 浅色·次要文字 @ 画布 | 4.42（待修） | **4.58** | muted 加深 |
| 深色·主按钮白字 | 3.42（不通过） | **4.83** | accentSolid（#2e74bd），悬停 5.94 |
| 深色·强调文字 | 4.42（待修） | **5.92** | accent 提亮 |
| 深色·侧栏分组文字 | 4.03（待修） | **5.46** | navMuted 提亮 |
| 新增·玻璃卡表面（两主题，最差底合成） | — | 全部 ≥4.5 | L2 表面文字对比度纳入验收 |

> 运行方式：`python tools/contrast_audit.py`（退出码 0 = 全通过）。

## 3. 表面体系落地（对照 v2 §2.2）

- **L0 页面基底**：`AmbientCanvas` 垂直渐变（ambientA→B→C）+ 两团低饱和光晕；页面/滚动容器透明，面板间空隙、卡片外围均由环境光提供氛围。
- **L1 数据工作面**：`QFrame#panel` 不透明（surfacePanel + 1px 描边 + radiusPanel），承载表格/图表/表单，保证连续阅读。
- **L2 强调/浮层**：Hero、服务状态卡、快捷操作、指标卡统一走 `GlassCard` 自绘（半透明填充 + 高光带 + 外圈光晕 + 描边），无真实模糊（Qt 边界见方案 §2.2）。
- 图表区（LinePlot）维持面板底色承接（M2 接入 pyqtgraph 后按 L1 深化）。

## 4. 标题栏着色 POC（Windows）

`windows_chrome.py` 令牌化后由 `main` 在首次显示与每次主题切换时调用：
深色主题开启沉浸式暗色标题栏并把标题栏/边框涂成侧栏同色；浅色主题关闭沉浸模式。
DWM 属性失败时静默回退系统外观，不影响启动（离屏/非 Windows 环境自动跳过）。

## 5. 截图产物

基线：`docs/ui-review-snapshots/light_*.png`、`dark_*.png`（18 张，改版前）
M0：`docs/ui-review-snapshots/m0/light_*.png`、`dark_*.png`（18 张，改版后，含模拟数据状态）

重新生成：`python tools/ui_snapshot.py --tag <标识>`

## 6. 真机走查清单（M0 出口需负责人确认，逐项勾选）

- [ ] Windows 11 实机启动（浅色/深色各一次），确认标题栏与主题同色、无明显割裂；
- [ ] 深色主题下主按钮文字清晰（accentSolid 生效）；
- [ ] 工作台 L0 渐变 + 玻璃卡质感在实机可见，且浅色玻璃面无发灰/脏感；
- [ ] 100% / 125% / 150% / 200% DPI 下无文字裁切、图标模糊、圆角异常；
- [ ] 1366×768 下工作台与服务卡无溢出，表格可读；
- [ ] 玻璃卡上的正文/次要文字在实机显示器（含亮度校准差异）仍清晰；
- [ ] 侧栏分组标题（实验控制/数据与系统）对比度肉眼可接受；
- [ ] 切主题（侧栏 ☾ 按钮 / 设置-外观）无闪烁卡顿；
- [ ] 键盘 Tab 顺序：导航→工作台按钮→重新检查 可走通；
- [ ] 视觉方向签字（此页确认后进入 M1）。

## 7. 下一步（M1 预告，待 M0 签字后执行）

`AppStatusModel` 单一状态源与初始化降级进入、侧栏状态摘要/图标 DPR 与矢量字形替换、
组件库升级收尾（按钮/输入/表格/滚动条双主题微调）、主题切换性能重构（去全树 repolish）。

## 8. 已知边界（本阶段有意未做）

- 页面/卡片入场动效、状态脉冲（M3 规范 §6）；
- 图表 pyqtgraph 接入、调束/扫谱页重排（M2）；
- 真实背景模糊与 Windows Acrylic/Mica（独立 POC，非主线）；
- `windows_chrome` 在部分远程桌面/老版本系统可能无效果（已按方案回退设计）。
