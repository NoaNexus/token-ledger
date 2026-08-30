# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path


runtime_bin = Path(sys.base_prefix) / "Library" / "bin"
runtime_dlls = [
    (str(runtime_bin / name), ".")
    for name in (
        "sqlite3.dll",
        "ffi.dll",
        "libcrypto-1_1-x64.dll",
    )
    if (runtime_bin / name).is_file()
]

a = Analysis(
    ["native_app.py"],
    pathex=[],
    binaries=runtime_dlls,
    datas=[("assets/token-ledger.ico", "assets")],
    hiddenimports=[
        "zoneinfo",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tokenledger.api", "tkinter"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="TokenLedger",
    icon="assets/token-ledger.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
