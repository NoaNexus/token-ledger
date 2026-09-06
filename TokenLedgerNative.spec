# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

a = Analysis(
    ["native_app.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("assets", "assets"),
        ("web", "web"),
    ],
    hiddenimports=[
        "zoneinfo",
        "psutil",
        "PyQt5.QtCore",
        "PyQt5.QtGui",
        "PyQt5.QtWidgets",
        "PyQt5.QtWebEngineWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
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

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="TokenLedger",
)
