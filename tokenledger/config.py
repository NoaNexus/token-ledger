from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def resource_root() -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    return Path(bundled) if bundled else Path(__file__).resolve().parent.parent


def default_user_home() -> Path:
    override = os.environ.get("TOKEN_LEDGER_USER_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home().resolve()


def default_data_dir() -> Path:
    override = os.environ.get("TOKEN_LEDGER_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "TokenLedger"
    return default_user_home() / ".token-ledger"


@dataclass(frozen=True, slots=True)
class AppConfig:
    user_home: Path
    data_dir: Path
    web_dir: Path
    timezone: str = "Asia/Shanghai"
    host: str = "127.0.0.1"
    port: int = 0
    open_browser: bool = True

    @property
    def database_path(self) -> Path:
        return self.data_dir / "token-ledger.db"

