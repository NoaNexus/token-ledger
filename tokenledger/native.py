from __future__ import annotations

import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any

from .config import default_data_dir


APP_NAME = "Token 账本"
APP_VERSION = "2.1.0"
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
    if not acquire_single_instance():
        _native_message(APP_NAME, "Token 账本已经在运行。")
        return 0
    try:
        from .qt_native import run

        return run(argv)
    except Exception as error:
        log_dir = default_data_dir()
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "native-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        _native_message(APP_NAME, f"应用启动失败：{type(error).__name__}\n错误详情已保存到本地数据目录。", True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
