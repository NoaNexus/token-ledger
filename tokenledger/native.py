from __future__ import annotations

import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any

from .config import default_data_dir


APP_NAME = "Token 账本"
APP_VERSION = "2.2.1"
_INSTANCE_HANDLE: int | None = None


def acquire_single_instance() -> bool:
    global _INSTANCE_HANDLE
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\TokenLedgerNativeDesktop")
        if not handle:
            return True
        if ctypes.windll.kernel32.GetLastError() == 183:
            ctypes.windll.kernel32.CloseHandle(handle)
            return False
        _INSTANCE_HANDLE = int(handle)
    except Exception:
        return True
    return True


def release_single_instance() -> None:
    global _INSTANCE_HANDLE
    if sys.platform == "win32" and _INSTANCE_HANDLE:
        try:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(_INSTANCE_HANDLE)
            _INSTANCE_HANDLE = None
        except Exception:
            pass


def activate_existing_window(window_title: str = "Token 账本 - 本机用量工作台") -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, window_title)
        if not hwnd:
            # Enumerate visible top-level windows matching Token 账本
            EnumWindows = user32.EnumWindows
            EnumWindowsProc = ctypes.WINFUNCTYPE(
                ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)
            )
            GetWindowTextLengthW = user32.GetWindowTextLengthW
            GetWindowTextW = user32.GetWindowTextW
            IsWindowVisible = user32.IsWindowVisible

            found_hwnd = None

            def foreach(h, l):
                nonlocal found_hwnd
                if IsWindowVisible(h):
                    length = GetWindowTextLengthW(h)
                    if length > 0:
                        buff = ctypes.create_unicode_buffer(length + 1)
                        GetWindowTextW(h, buff, length + 1)
                        if "Token 账本" in buff.value:
                            found_hwnd = h
                            return False
                return True

            EnumWindows(EnumWindowsProc(foreach), 0)
            hwnd = found_hwnd

        if hwnd:
            SW_RESTORE = 9
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


def compact_number(value: Any) -> str:
    number = float(value or 0)
    absolute = abs(number)
    if absolute >= 100_000_000:
        return f"{number / 100_000_000:.3g} 亿"
    if absolute >= 10_000:
        return f"{number / 10_000:.4g} 万"
    return f"{int(number):,}"


def percent(value: Any, digits: int = 1) -> str:
    if value is None:
        return "未提供"
    return f"{float(value):.{digits}f}%"


def ratio_percent(value: Any) -> float | None:
    if value is None:
        return None
    return max(0.0, min(float(value) * 100.0, 100.0))


def metric_rows(metrics: dict[str, Any]) -> list[tuple[str, str]]:
    """Return the complete, stable token breakdown used by every desktop card."""
    return [
        ("输入", compact_number(metrics.get("input"))),
        ("缓存读取", compact_number(metrics.get("cached_input"))),
        ("缓存写入", compact_number(metrics.get("cache_write"))),
        ("输出", compact_number(metrics.get("output"))),
        ("推理", compact_number(metrics.get("reasoning"))),
        ("净用量", compact_number(metrics.get("net_usage"))),
    ]


def _source_database_candidates() -> list[Path]:
    candidates = [Path.cwd() / ".data" / "token-ledger.db"]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent.parent / ".data" / "token-ledger.db")
    else:
        candidates.append(Path(__file__).resolve().parent.parent / ".data" / "token-ledger.db")
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def migrate_legacy_database(target: Path) -> Path | None:
    if target.exists():
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    for source in _source_database_candidates():
        if not source.is_file() or source.resolve() == target.resolve():
            continue
        source_connection: sqlite3.Connection | None = None
        target_connection: sqlite3.Connection | None = None
        try:
            source_connection = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
            target_connection = sqlite3.connect(target)
            source_connection.backup(target_connection)
            target_connection.commit()
            return source
        except sqlite3.Error:
            if target.exists():
                try:
                    target.unlink()
                except OSError:
                    pass
        finally:
            if target_connection is not None:
                target_connection.close()
            if source_connection is not None:
                source_connection.close()
    return None


def _native_message(title: str, message: str, error: bool = False) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10 if error else 0x40)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    from .desktop import run_desktop
    return run_desktop()


if __name__ == "__main__":
    raise SystemExit(main())
