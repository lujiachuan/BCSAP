# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：共享数据服务（控制台程序，onedir）。

服务端推荐形态是“源码 venv + NSSM 包装”或本 spec 打出的控制台 exe 再挂 NSSM。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent.parent
sys.path.insert(0, str(ROOT))

hiddenimports = collect_submodules("apps.data_service") + collect_submodules("packages")

a = Analysis(
    [str(ROOT / "apps" / "data_service" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
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
    name="spectrum-data-service",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # 服务进程保留控制台日志输出，由 NSSM 重定向到日志文件
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="spectrum-data-service",
)
