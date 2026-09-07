from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

# Enable high refresh rate & GPU rasterization flags for Chromium inside Qt WebEngine
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
    "--enable-gpu-rasterization "
    "--enable-features=CanvasOopRasterization"
)

from .config import AppConfig, default_data_dir, default_user_home, resource_root
from .db import TokenDatabase
from .native import (
    APP_NAME,
    acquire_single_instance,
    activate_existing_window,
    apply_dark_titlebar,
    migrate_legacy_database,
    release_single_instance,
)
from .providers import AntigravityAdapter, ClaudeAdapter, CodexAdapter
from .scanner import ScanCoordinator
from .api import TokenLedgerServer


def _free_port_if_stale(port: int = 8765) -> None:
    """If port is occupied by an unresponsive/zombie process, terminate it to unblock binding."""
    try:
        import psutil

        current_pid = os.getpid()
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                pid = conn.pid
                if pid and pid != current_pid:
                    try:
                        p = psutil.Process(pid)
                        name = p.name().lower()
                        if "python" in name:
                            p.terminate()
                            p.wait(timeout=1.5)
                    except Exception:
                        try:
                            psutil.Process(pid).kill()
                        except Exception:
                            pass
    except Exception:
        pass


def run_desktop() -> int:
    # 1. Single Instance Check: if already running, activate the existing window and exit cleanly
    if not acquire_single_instance():
        activate_existing_window()
        return 0

    # 2. Register Windows AppUserModelID so Taskbar groups with custom icon, not python/browser
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NoaNexus.TokenLedger.Desktop.App")
        except Exception:
            pass

    # 3. Setup SQLite database and configuration
    migrate_legacy_database(default_data_dir() / "token-ledger.db")
    config = AppConfig(
        user_home=default_user_home(),
        data_dir=default_data_dir(),
        web_dir=resource_root() / "web",
        timezone="Asia/Shanghai",
        port=8765,
        open_browser=False,
    )
    database = TokenDatabase(config.database_path)
    adapters = [
        CodexAdapter(config.user_home),
        ClaudeAdapter(config.user_home),
        AntigravityAdapter(config.user_home),
    ]
    scanner = ScanCoordinator(database, adapters)

    if "--scan-only" in sys.argv:
        result = scanner.scan_sync(force="--force" in sys.argv)
        if sys.stdout:
            print(result["message"])
        return 0

    # 4. Check if server is already running on 8765, else start in daemon thread
    def check_health(timeout: float = 0.5) -> bool:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=timeout):
                return True
        except Exception:
            return False

    server = None
    server_alive = check_health(0.5)
    if not server_alive:
        _free_port_if_stale(8765)
        try:
            server = TokenLedgerServer(config, database, scanner)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="tokenledger-http")
            server_thread.start()
            for _ in range(50):
                if check_health(0.05):
                    break
                time.sleep(0.02)
        except OSError:
            time.sleep(0.2)
            if not check_health(0.3):
                _free_port_if_stale(8765)
                try:
                    server = TokenLedgerServer(config, database, scanner)
                    server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="tokenledger-http")
                    server_thread.start()
                except Exception:
                    pass

    # 5. Delay background scan by 3.5s so initial UI loads & renders instantly (<150ms)
    threading.Timer(3.5, lambda: scanner.start_background(force="--force" in sys.argv)).start()

    # 6. Launch Native Desktop Window via PyQt5 WebEngine
    try:
        from PyQt5.QtCore import QCoreApplication, QEvent, Qt, QTimer, QUrl
        from PyQt5.QtGui import QGuiApplication, QIcon
        from PyQt5.QtWebEngineWidgets import QWebEngineView
        from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget

        # Configure High DPI and OpenGL context sharing
        QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

        if hasattr(Qt, "HighDpiScaleFactorRoundingPolicy"):
            QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
                Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
            )

        app = QApplication(sys.argv)
        app.setApplicationName("Token 账本")
        app.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #090C10;
                color: #EDEDED;
            }
        """)
        icon_path = str(resource_root() / "assets" / "token-ledger.ico")
        icon = QIcon(icon_path)
        app.setWindowIcon(icon)

        class LedgerMainWindow(QMainWindow):
            def __init__(self):
                super().__init__()
                self.setWindowTitle("Token 账本 - 本机用量工作台")
                self.setWindowIcon(icon)
                self.resize(1440, 920)

                # Wrap view in dedicated container with layout to decouple QMainWindow geometry from Chromium HWND
                self.container = QWidget(self)
                self.layout = QVBoxLayout(self.container)
                self.layout.setContentsMargins(0, 0, 0, 0)
                self.layout.setSpacing(0)

                self.view = QWebEngineView(self.container)
                from PyQt5.QtGui import QColor
                self.view.page().setBackgroundColor(QColor("#090C10"))
                self.view.setUrl(QUrl("http://127.0.0.1:8765/"))
                self.layout.addWidget(self.view)
                self.setCentralWidget(self.container)

                # Force native HWND creation and apply dark titlebar
                try:
                    apply_dark_titlebar(int(self.winId()))
                except Exception:
                    pass

            def showEvent(self, event):
                super().showEvent(event)
                try:
                    hwnd = int(self.winId())
                    apply_dark_titlebar(hwnd)
                    QTimer.singleShot(60, lambda: apply_dark_titlebar(hwnd))
                    QTimer.singleShot(250, lambda: apply_dark_titlebar(hwnd))
                except Exception:
                    pass

            def changeEvent(self, event):
                super().changeEvent(event)
                if event.type() == QEvent.WindowStateChange:
                    # Refresh view render pump on maximize/restore to prevent DirectComposition swapchain lockup
                    QTimer.singleShot(40, lambda: self.view.update())
                    try:
                        apply_dark_titlebar(int(self.winId()))
                    except Exception:
                        pass

        window = LedgerMainWindow()
        window.show()
        try:
            hwnd = int(window.winId())
            apply_dark_titlebar(hwnd)
            QTimer.singleShot(80, lambda: apply_dark_titlebar(hwnd))
            QTimer.singleShot(300, lambda: apply_dark_titlebar(hwnd))
        except Exception:
            pass

        # Clean shutdown handler
        def cleanup():
            if server:
                try:
                    threading.Thread(target=server.shutdown, daemon=True).start()
                    server.server_close()
                except Exception:
                    pass
            release_single_instance()
            # Terminate immediately so no daemon threads or helper processes hang
            os._exit(0)

        app.aboutToQuit.connect(cleanup)

        ret = app.exec_()
        cleanup()
        return ret
    except Exception as e:
        # Fallback to Edge App Window if PyQt5 is unavailable
        try:
            error_log = config.data_dir / "desktop_error.log"
            error_log.parent.mkdir(parents=True, exist_ok=True)
            with error_log.open("a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] PyQt5 launch fallback: {traceback.format_exc()}\n")
        except Exception:
            pass

        from .__main__ import launch_desktop_window
        launch_desktop_window("http://127.0.0.1:8765/")
        if server:
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                server.server_close()
        return 0


if __name__ == "__main__":
    sys.exit(run_desktop())
