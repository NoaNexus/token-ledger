import sys
from pathlib import Path

extra_binaries = []
search_dirs = [
    Path(sys.prefix) / "Library" / "bin",
    Path(sys.base_prefix) / "Library" / "bin",
    Path(sys.prefix) / "DLLs",
    Path(sys.base_prefix) / "DLLs",
]
needed_dlls = [
    "sqlite3.dll",
    "ffi.dll",
    "LIBBZ2.dll",
    "liblzma.dll",
    "libcrypto-1_1-x64.dll",
    "libssl-1_1-x64.dll",
]
for dll_name in needed_dlls:
    for s_dir in search_dirs:
        candidate = s_dir / dll_name
        if candidate.is_file():
            extra_binaries.append((str(candidate), "."))
            break

for s_dir in search_dirs:
    if s_dir.is_dir():
        for candidate in s_dir.glob("*Qt5*.dll"):
            extra_binaries.append((str(candidate), "."))
        for candidate in s_dir.glob("QtWebEngineProcess.exe"):
            extra_binaries.append((str(candidate), "."))

a = Analysis(
    ["native_app.py"],
    pathex=[],
    binaries=extra_binaries,
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
