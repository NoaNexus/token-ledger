from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
import traceback

# Enable high refresh rate & GPU rasterization flags for Chromium inside Qt WebEngine
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--enable-gpu-rasterization --enable-features=CanvasOopRasterization",
)

from .config import AppConfig, default_data_dir, default_user_home, resource_root
from .db import TokenDatabase
from .native import acquire_single_instance, activate_existing_window, apply_window_theme, migrate_legacy_database, release_single_instance
from .providers import AntigravityAdapter, ClaudeAdapter, CodexAdapter
from .scanner import ScanCoordinator
from .api import bind_local_server, local_server_url


def _fit_window_geometry(
    available_x: int,
    available_y: int,
    available_width: int,
    available_height: int,
    *,
    margin: int = 24,
    max_width: int = 1440,
    max_height: int = 920,
) -> tuple[int, int, int, int]:
    """Center a window inside the monitor's work area (which excludes the taskbar)."""
    width = min(max_width, max(1, available_width - margin * 2))
    height = min(max_height, max(1, available_height - margin * 2))
    x = available_x + max(0, (available_width - width) // 2)
    y = available_y + max(0, (available_height - height) // 2)
    return x, y, width, height


def _start_scan_scheduler(
    scanner: ScanCoordinator,
    *,
    initial_delay: float = 3.5,
    interval: float = 60.0,
    force: bool = False,
) -> tuple[threading.Event, threading.Thread]:
    """Start stoppable desktop scans without keeping the process alive at shutdown."""
    stop_event = threading.Event()
    delay = max(0.0, float(initial_delay))
    period = max(0.01, float(interval))

    def run() -> None:
        if stop_event.wait(delay):
            return
        scanner.start_background(force=force)
        while not stop_event.wait(period):
            scanner.start_background()

    thread = threading.Thread(target=run, daemon=True, name="tokenledger-desktop-scan")
    thread.start()
    return stop_event, thread


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
        try:
            result = scanner.scan_sync(force="--force" in sys.argv)
            if sys.stdout:
                print(result["message"])
            return 0
        finally:
            release_single_instance()

    # 4. Prefer 8765, then bind an OS-assigned loopback port if it is occupied.
    # Never probe or reuse an identity-unknown service on the requested port.
    server = bind_local_server(config, database, scanner)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="tokenledger-http")
    server_thread.start()
    window_url = local_server_url(server)

    # 5. Start a stoppable initial + 60-second incremental scan schedule.
    scan_stop, scan_thread = _start_scan_scheduler(
        scanner,
        force="--force" in sys.argv,
    )

    # Clean shutdown handler is defined before the Qt import so the fallback
    # path can release the server, scheduler, and single-instance lock too.
    cleanup_lock = threading.Lock()
    cleanup_done = False

    def cleanup() -> None:
        nonlocal cleanup_done
        with cleanup_lock:
            if cleanup_done:
                return
            cleanup_done = True
        scan_stop.set()
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
        if server_thread.is_alive():
            server_thread.join(timeout=0.5)
        if scan_thread.is_alive() and scan_thread is not threading.current_thread():
            scan_thread.join(timeout=0.5)
        release_single_instance()

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
                self._is_dark = True
                self.setWindowTitle("Token 账本 - 本机用量工作台")
                self.setWindowIcon(icon)
                screen = QApplication.primaryScreen()
                if screen is not None:
                    available = screen.availableGeometry()
                    self.setGeometry(*_fit_window_geometry(available.x(), available.y(), available.width(), available.height()))
                else:
                    self.resize(1440, 920)

                # Wrap view in dedicated container with layout to decouple QMainWindow geometry from Chromium HWND
                self.container = QWidget(self)
                self.layout = QVBoxLayout(self.container)
                self.layout.setContentsMargins(0, 0, 0, 0)
                self.layout.setSpacing(0)

                self.view = QWebEngineView(self.container)
                from PyQt5.QtGui import QColor
                self.view.page().setBackgroundColor(QColor("#090C10"))
                self.view.setUrl(QUrl(window_url))
                self.layout.addWidget(self.view)
                self.setCentralWidget(self.container)

                # Listen to document.title changes from web app to dynamically sync light/dark theme
                def on_title_changed(title: str):
                    if "[theme:light]" in title:
                        self.set_theme(False)
                        self.setWindowTitle("Token 账本 - 本机用量工作台")
                    elif "[theme:dark]" in title:
                        self.set_theme(True)
                        self.setWindowTitle("Token 账本 - 本机用量工作台")

                self.view.titleChanged.connect(on_title_changed)

                # Force native HWND creation and apply initial theme
                try:
                    self.set_theme(self._is_dark)
                except Exception:
                    pass

            def set_theme(self, is_dark: bool) -> None:
                self._is_dark = is_dark
                bg_color = "#090C10" if is_dark else "#F8FAFC"
                text_color = "#EDEDED" if is_dark else "#0F172A"
                self.setStyleSheet(f"QMainWindow, QWidget {{ background-color: {bg_color}; color: {text_color}; }}")
                try:
                    from PyQt5.QtGui import QColor
                    self.view.page().setBackgroundColor(QColor(bg_color))
                except Exception:
                    pass
                try:
                    hwnd = int(self.winId())
                    apply_window_theme(hwnd, is_dark)
                except Exception:
                    pass

            def showEvent(self, event):
                super().showEvent(event)
                try:
                    hwnd = int(self.winId())
                    apply_window_theme(hwnd, self._is_dark)
                    QTimer.singleShot(60, lambda: apply_window_theme(hwnd, self._is_dark))
                    QTimer.singleShot(250, lambda: apply_window_theme(hwnd, self._is_dark))
                except Exception:
                    pass

            def changeEvent(self, event):
                super().changeEvent(event)
                if event.type() == QEvent.WindowStateChange:
                    # Refresh view render pump on maximize/restore to prevent DirectComposition swapchain lockup
                    QTimer.singleShot(40, lambda: self.view.update())
                    try:
                        apply_window_theme(int(self.winId()), self._is_dark)
                    except Exception:
                        pass

        window = LedgerMainWindow()
        window.show()
        try:
            hwnd = int(window.winId())
            apply_window_theme(hwnd, window._is_dark)
            QTimer.singleShot(80, lambda: apply_window_theme(hwnd, window._is_dark))
            QTimer.singleShot(300, lambda: apply_window_theme(hwnd, window._is_dark))
        except Exception:
            pass

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
        try:
            launch_desktop_window(window_url)
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            cleanup()
        return 0


if __name__ == "__main__":
    sys.exit(run_desktop())
