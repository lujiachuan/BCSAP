# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：仪器执行服务（控制台程序，onedir）。

只安装在授权操作电脑；默认监听 127.0.0.1:8765。可注册为开机自启任务
或 Windows 服务（见 deploy/README.md）。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent.parent
sys.path.insert(0, str(ROOT))

hiddenimports = collect_submodules("apps.instrument_service") + collect_submodules("packages")

a = Analysis(
    [str(ROOT / "apps" / "instrument_service" / "main.py")],
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
