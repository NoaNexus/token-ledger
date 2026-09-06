from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from .api import TokenLedgerServer
from .config import AppConfig, default_data_dir, default_user_home, resource_root
from .db import TokenDatabase
from .providers import AntigravityAdapter, ClaudeAdapter, CodexAdapter
from .scanner import ScanCoordinator


def launch_desktop_window(url: str) -> None:
    """Launch the dashboard in standalone window mode (no address bar/tabs) via Edge, or fallback to default browser."""
    if sys.platform == "win32":
        edge_paths = [
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ]
        for candidate in edge_paths:
            if candidate.is_file():
                try:
                    subprocess.Popen([str(candidate), f"--app={url}", "--window-size=1360,880"])
                    return
                except Exception:
                    pass
    webbrowser.open(url)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Token Ledger local AI-agent usage dashboard")
    parser.add_argument("--user-home", type=Path, default=default_user_home())
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--scan-only", action="store_true")
    parser.add_argument("--force", action="store_true", help="Reparse every discovered file")
    parser.add_argument("--scan-interval", type=int, default=60, help="Seconds between automatic incremental scans; 0 disables")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # If server is already running, activate window and exit cleanly
    if args.port != 0 and not args.scan_only:
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/api/health", timeout=0.8):
                from .native import activate_existing_window
                if not activate_existing_window():
                    if not args.no_browser:
                        launch_desktop_window(f"http://127.0.0.1:{args.port}/")
                return 0
        except Exception:
            pass

    config = AppConfig(
        user_home=args.user_home.expanduser().resolve(),
        data_dir=args.data_dir.expanduser().resolve(),
        web_dir=resource_root() / "web",
        timezone=args.timezone,
        port=args.port,
        open_browser=not args.no_browser,
    )
    database = TokenDatabase(config.database_path)
    adapters = [CodexAdapter(config.user_home), ClaudeAdapter(config.user_home), AntigravityAdapter(config.user_home)]
    scanner = ScanCoordinator(database, adapters)
    if args.scan_only:
        result = scanner.scan_sync(force=args.force)
        print(result["message"])
        return 0

    server = TokenLedgerServer(config, database, scanner)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    scanner.start_background(force=args.force)
    stop_scans = threading.Event()
    if args.scan_interval > 0:
        interval = max(args.scan_interval, 15)

        def scan_periodically() -> None:
            while not stop_scans.wait(interval):
                scanner.start_background()

        threading.Thread(target=scan_periodically, daemon=True, name="token-ledger-auto-scan").start()
    if config.open_browser:
        threading.Timer(0.5, lambda: launch_desktop_window(url)).start()
    print(f"Token Ledger 已启动：{url}")
    print(f"本地索引：{config.database_path}")
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        stop_scans.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
