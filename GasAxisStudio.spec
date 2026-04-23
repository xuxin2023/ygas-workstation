# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project_root = Path.cwd()
assets_dir = project_root / "assets"
icon_path = assets_dir / "app.ico"

if not icon_path.exists():
    raise FileNotFoundError(f"Missing required application icon: {icon_path}")

datas = []
if assets_dir.exists():
    datas.append((str(assets_dir), "assets"))

hiddenimports = ["PySide6.QtSvg", "pyqtgraph"]

a = Analysis(
    ["main.py"],
    pathex=[str(project_root)],
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
    name="GasAxisStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="GasAxisStudio",
)
