# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：桌面客户端（无控制台窗口，onedir）。

用法：由 deploy/packaging/build.ps1 调用；也可手动：
    .\\.venv\\Scripts\\python.exe -m PyInstaller ^
        --noconfirm --clean --distpath dist --workpath build ^
        deploy/packaging/spectrum_client.spec
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# deploy/packaging/ 的上一级两级即仓库根目录
ROOT = Path(SPECPATH).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# UI 通过 pages/registry 组织页面；收集全部子模块，避免个别模块只被间接引用时漏掉。
hiddenimports = collect_submodules("apps.desktop_client") + collect_submodules("packages")
# scienceplots 的样式是 .mplstyle 数据文件：只收模块字节码不收集样式文件，
# 运行时 style.use("science") 会抛 OSError 直接崩掉整个客户端。
scienceplots_datas = collect_data_files("scienceplots")

a = Analysis(
    [str(ROOT / "apps" / "desktop_client" / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=scienceplots_datas,
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
    name="spectrum-client",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI 程序：不弹控制台窗口
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="spectrum-client",
)
