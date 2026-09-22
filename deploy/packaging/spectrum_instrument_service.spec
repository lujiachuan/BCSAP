# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：仪器执行服务（控制台程序，onedir）。

只安装在授权操作电脑；默认监听 127.0.0.1:8765。可注册为开机自启任务
或 Windows 服务（见 deploy/README.md）。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

ROOT = Path(SPECPATH).resolve().parent.parent
sys.path.insert(0, str(ROOT))

hiddenimports = (
    collect_submodules("apps.instrument_service")
    + collect_submodules("packages")
    + collect_submodules("alembic")
    + collect_submodules("optuna_dashboard")
)
# Optuna RDB storage 运行时要用 alembic 迁移脚本目录（非 .py 数据文件），
# PyInstaller 默认只收字节码，必须显式把数据目录打进去，否则 create_study 报
# “Path doesn't exist: .../optuna/storages/_rdb/alembic”（启动调束 HTTP 500）。
# include_py_files：alembic 迁移脚本（env.py / versions/v*.a.py 等）文件名不是
# 合法模块名，不会被当模块收集，必须整目录、连同 .py 原样打进去。
# optuna-dashboard 自身是独立包，前端 HTML/JS 模板与静态资源必须打进去，
# 否则打包后浏览器访问 dashboard 所有路由都 404（开发态因 site-packages 在而正常）。
datas = (
    collect_data_files("optuna", include_py_files=True)
    + collect_data_files("alembic")
    + collect_data_files("optuna_dashboard")
)

a = Analysis(
    [str(ROOT / "apps" / "instrument_service" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="spectrum-instrument-service",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="spectrum-instrument-service",
)
