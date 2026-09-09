# 部署目录

按 [部署矩阵与环境初始化](../docs/部署矩阵与环境初始化.md) 的拓扑组织。本目录保存打包与安装资源。

| 子目录 / 文件 | 内容 |
| --- | --- |
| `packaging/` | PyInstaller spec ×3、`build.ps1` 构建脚本 |
| `README.md` | 本文件：打包与 Windows 服务化说明 |

当前阶段：0.1.0 骨架已打通“打包 → 可运行 exe”链路；尚未做代码签名、图标、Inno 安装包与自动升级。

## 1. 交付形态总览

| 目标 | 形态 | 工具 |
| --- | --- | --- |
| 桌面客户端（操作电脑、检索电脑） | **安装程序**（“软件”形态）：PyInstaller 文件夹版 + Inno Setup 打包 | PyInstaller → Inno Setup 6 |
| data_service（数据服务器） | **Windows 服务**长期运行 | NSSM 包装（推荐），或 PyInstaller exe + NSSM |
| instrument_service（授权操作电脑） | 随客户端单文件夹交付，**客户端启动时自动拉起**（也可改为 NSSM/任务计划独立常驻） | 客户端内监督（instrument_supervisor） |

## 2. 构建可执行程序（PyInstaller）

前置：已按 README §7 建好 `.venv` 并安装对应依赖；构建脚本会自动补装 PyInstaller（首次需联网）。

```powershell
# 在仓库根目录执行
.\deploy\packaging\build.ps1                     # 客户端（GUI，无控制台）
.\deploy\packaging\build.ps1 -Target data-service
.\deploy\packaging\build.ps1 -Target instrument-service
.\deploy\packaging\build.ps1 -Target operator-package
#   ↑ 客户端 + 仪器服务一起构建，并组装成“操作电脑单文件夹交付包”：
#     dist\SpectrumPlatform-Operator\
#     ├─ spectrum-client\spectrum-client.exe
#     └─ spectrum-instrument-service\spectrum-instrument-service.exe
```

产物（onedir 文件夹版，需整体分发，不能只拷 exe）：

```text
dist/spectrum-client/spectrum-client.exe            # 桌面客户端
dist/spectrum-data-service/spectrum-data-service.exe
dist/spectrum-instrument-service/spectrum-instrument-service.exe
dist/SpectrumPlatform-Operator/                     # 操作电脑整包（见下）
```

**操作电脑单文件夹交付（客户端自动拉起仪器服务）**：把
`dist\SpectrumPlatform-Operator\` 整个文件夹拷到操作电脑，双击其中的
`spectrum-client\spectrum-client.exe`。客户端启动时探测
`http://127.0.0.1:8765/control/v1/health/live`，不可达就自动以隐藏窗口
拉起同目录的 `spectrum-instrument-service.exe`，就绪后才进入主界面；
纯检索电脑只拷 `dist\spectrum-client\`（没有服务文件夹，客户端不会尝试
拉起任何东西）。退出策略由 QSettings 键 `instrument/stopServiceOnExit`
控制（默认 `true`＝关界面带走服务，适合骨架阶段；接真实扫谱后建议改为
`false` 并改用 NSSM/任务计划常驻）。开发/测试可用环境变量
`SPECTRUM_INSTRUMENT_EXE` 覆盖服务 exe 路径。

注意：spec 从仓库源码直接分析打包，**构建用的 venv 应是干净的常规（非 -e 可编辑）安装**；
可编辑安装下打包偶发漏模块，若遇到请在干净 venv 里 `pip install .` 后再构建。
打包产物是构建时刻源码的快照，客户端 UI 大改后需要重新构建。

## 3. 服务器端：注册为 Windows 服务（NSSM）

NSSM（[nssm.cc](https://nssm.cc/)）用一条命令把任意 exe 变成开机自启、崩溃自动重启的服务。
推荐服务器 A 直接包装 `.venv` 里的 python/uvicorn（升级方便），不需要先打包 exe：

```powershell
# 数据服务器 A（管理员 PowerShell）
# 安装 nssm 后，把 python 作为服务注册：
nssm install SpectrumDataService "D:\BCSAP\.venv\Scripts\python.exe" "-m apps.data_service.main"
nssm set SpectrumDataService AppDirectory "D:\BCSAP"
nssm set SpectrumDataService AppStdout "D:\BCSAP\logs\data-service.log"
nssm set SpectrumDataService AppStderr "D:\BCSAP\logs\data-service.err.log"
nssm set SpectrumDataService Start SERVICE_AUTO_START
nssm start SpectrumDataService
```

若服务无法访问交互桌面无关紧要（数据服务无 GUI）；注意：
生产环境 data_service 要对外服务时，监听地址需从 `127.0.0.1` 放开（见
[部署矩阵与环境初始化](../docs/部署矩阵与环境初始化.md) 3.1），并配合防火墙白名单与 HTTPS。

## 4. 操作电脑端：instrument_service 开机自启

两种方式任选：

- **NSSM 服务**（登录前即可运行，无需用户登录）：

  ```powershell
  nssm install SpectrumInstrumentService "D:\BCSAP\.venv\Scripts\python.exe" "-m apps.instrument_service.main"
  nssm set SpectrumInstrumentService AppDirectory "D:\BCSAP"
  nssm set SpectrumInstrumentService Start SERVICE_AUTO_START
  ```

- **任务计划程序**（用户登录后运行，桌面会话内更接近调试习惯）：

  ```powershell
  schtasks /Create /TN "SpectrumInstrumentService" /TR "D:\BCSAP\.venv\Scripts\python.exe -m apps.instrument_service.main" /SC ONLOGON /RL LIMITED
  ```

按设计执行服务只监听 `127.0.0.1:8765`，不要为其开放远程端口。

## 5. 客户端：从文件夹版到“软件”安装包（Inno Setup 6）

文件夹版验证通过后，用 Inno Setup 生成安装程序（首次需在本机安装 Inno Setup 6）：

```iss
; deploy/packaging/spectrum-client.iss —— 预留，按需补全
[Setup]
AppName=谱图平台
AppVersion=0.1.0
DefaultDirName={autopf}\SpectrumPlatform
OutputDir=..\..\dist\installer
OutputBaseFilename=Setup-SpectrumPlatform-Client-0.1.0
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes

[Files]
Source: "..\..\dist\spectrum-client\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\谱图平台"; Filename: "{app}\spectrum-client.exe"
Name: "{autodesktop}\谱图平台"; Filename: "{app}\spectrum-client.exe"

[Run]
Filename: "{app}\spectrum-client.exe"; Description: "启动谱图平台"; Flags: nowait postinstall skipifsilent
```

正式分发前还需：应用图标（`.ico`）、版本信息（spec 的 `version=`）、代码签名证书、数字签名后的安装包校验。

## 6. 与文档的关系

- [部署矩阵与环境初始化](../docs/部署矩阵与环境初始化.md)：每台机器装什么、地址怎么配、数据库就绪接入。
- [软件总体架构与实施设计](../docs/软件总体架构与实施设计.md)：进程边界与安全边界设计依据。
