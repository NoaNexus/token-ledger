"""Run a small real-page QtWebEngine smoke check without touching user data.

The check serves the repository's actual ``web/index.html`` and
``web/app.js`` through an ephemeral loopback server backed by a temporary
SQLite database containing one synthetic event with an unknown model price.
It verifies that the live page renders ``未提供`` for that model and that the
theme button updates the document state.  No real home directory, account
database, or network endpoint is used.

Run from the repository root with the isolated environment, for example::

    .build-venv\\Scripts\\python.exe tests\\qt_web_smoke.py

The screenshot is written below ``artifacts/`` (ignored by git).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any


# This process is a disposable visual smoke check. Disable GPU shader/cache
# writes so repeated runs do not contend with the user's default Qt profile.
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--no-sandbox --disable-gpu --disable-gpu-compositing --disable-gpu-shader-disk-cache",
)

try:
    from PyQt5.QtCore import QEventLoop, QTimer, QUrl
    from PyQt5.QtWebEngineWidgets import (
        QWebEnginePage,
        QWebEngineProfile,
        QWebEngineView,
    )
    from PyQt5.QtWidgets import QApplication
except ImportError as error:  # pragma: no cover - depends on the local smoke environment
    print(f"QtWebEngine smoke skipped: {error}", file=sys.stderr)
    raise SystemExit(2)

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tokenledger.api import bind_local_server, local_server_url
from tokenledger.config import AppConfig
from tokenledger.db import TokenDatabase
from tokenledger.models import ParsedFile, UsageEvent
from tokenledger.scanner import ScanCoordinator


UNKNOWN_MODEL = "qt-smoke-unknown-model"


class SmokePage(QWebEnginePage):
    """Collect console diagnostics, retaining Qt's numeric error level."""

    def __init__(self, profile: QWebEngineProfile, parent: Any = None) -> None:
        super().__init__(profile, parent)
        self.console_messages: list[dict[str, Any]] = []

    def javaScriptConsoleMessage(
        self, level: Any, message: str, line_number: int, source_id: str
    ) -> None:
        try:
            level_value = int(level)
        except (TypeError, ValueError):
            level_value = int(getattr(level, "value", -1))
        self.console_messages.append(
            {
                "level": level_value,
                "message": str(message),
                "line": int(line_number),
                "source": str(source_id),
            }
        )


def wait_ms(milliseconds: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(max(0, milliseconds), loop.quit)
    loop.exec_()


def load_url(view: QWebEngineView, url: str, timeout_ms: int) -> None:
    loop = QEventLoop()
    loaded: list[bool] = []
    progress_complete: list[bool] = []

    def on_loaded(ok: bool) -> None:
        loaded.append(bool(ok))
        loop.quit()

    def on_progress(value: int) -> None:
        # Qt 5.15 on this Windows setup can reach 100% without emitting
        # loadFinished. Treat complete load progress as a safe fallback;
        # wait_for_page_data still proves that the actual app rendered.
        if int(value) >= 100:
            progress_complete.append(True)
            loop.quit()

    view.loadFinished.connect(on_loaded)
    view.loadProgress.connect(on_progress)
    view.load(QUrl(url))
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec_()
    try:
        view.loadFinished.disconnect(on_loaded)
    except TypeError:
        pass
    try:
        view.loadProgress.disconnect(on_progress)
    except TypeError:
        pass
    if not loaded and not progress_complete:
        raise TimeoutError(f"real page did not finish loading within {timeout_ms} ms")
    if loaded and not loaded[-1]:
        raise RuntimeError(f"QtWebEngine reported page load failure for {url}")


def evaluate(page: QWebEnginePage, expression: str, timeout_ms: int) -> Any:
    loop = QEventLoop()
    result: list[Any] = []

    def on_result(value: Any) -> None:
        result.append(value)
        loop.quit()

    page.runJavaScript(expression, on_result)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec_()
    if not result:
        raise TimeoutError(f"JavaScript evaluation timed out: {expression}")
    return result[0]


def wait_for_page_data(page: QWebEnginePage, timeout_ms: int) -> dict[str, Any]:
    """Wait until app.js has rendered the synthetic API response."""

    deadline = monotonic() + timeout_ms / 1000
    probe = (
        "(() => {"
        "const shell = document.getElementById('appShell');"
        "const names = Array.from(document.querySelectorAll('.model-name'))"
        ".map((node) => node.textContent.trim());"
        "return Boolean(shell && shell.dataset.viewState === 'ready' "
        "&& names.includes('qt-smoke-unknown-model'));"
        "})()"
    )
    while monotonic() < deadline:
        if evaluate(page, probe, min(500, timeout_ms)):
            details = evaluate(
                page,
                "JSON.stringify({"
                "viewState: document.getElementById('appShell').dataset.viewState,"
                "preview: document.getElementById('previewNotice').hidden === false,"
                "modelNames: Array.from(document.querySelectorAll('.model-name')).map((node) => node.textContent.trim()),"
                "modelCosts: Array.from(document.querySelectorAll('.model-cost-tag')).map((node) => node.textContent.trim()),"
                "})",
                timeout_ms,
            )
            return json.loads(details)
        wait_ms(50)
    raise TimeoutError(f"real app did not render synthetic dashboard within {timeout_ms} ms")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "artifacts",
        help="Ignored directory for the local smoke screenshot",
    )
    parser.add_argument("--timeout-ms", type=int, default=5000)
    return parser.parse_args()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> int:
    args = parse_args()
    timeout_ms = max(100, args.timeout_ms)
    app = QApplication.instance() or QApplication([sys.argv[0]])
    server = None
    server_thread: threading.Thread | None = None
    view: QWebEngineView | None = None
    profile: QWebEngineProfile | None = None

    with tempfile.TemporaryDirectory(prefix="token-ledger-qt-smoke-") as temp_name:
        temp_root = Path(temp_name)
        db_path = temp_root / "data" / "synthetic-token-ledger.db"
        web_dir = REPO_ROOT / "web"
        database = TokenDatabase(db_path)
        event = UsageEvent(
            event_id="qt-smoke-unknown-event",
            agent="codex",
            route="Qt smoke synthetic route",
            platform="Qt smoke synthetic platform",
            model=UNKNOWN_MODEL,
            session_id="qt-smoke-synthetic-session",
            occurred_at=_utc_now(),
            input_tokens=120,
            cached_input_tokens=20,
            output_tokens=30,
            total_tokens=150,
        )
        database.replace_file(
            "qt-smoke-synthetic-file",
            "codex",
            "synthetic smoke source",
            1,
            1,
            ParsedFile(events=[event], session_count=1),
            _utc_now(),
        )
        scanner = ScanCoordinator(database, [])
        config = AppConfig(
            user_home=temp_root / "synthetic-home",
            data_dir=temp_root / "data",
            web_dir=web_dir,
            host="127.0.0.1",
            port=0,
            open_browser=False,
        )
        server = bind_local_server(config, database, scanner)
        server_thread = threading.Thread(
            target=server.serve_forever,
            name="token-ledger-qt-smoke-server",
            daemon=True,
        )
        server_thread.start()
        page_url = local_server_url(server)

        try:
            # An unnamed profile is off-the-record. Keep the smoke check from
            # creating or locking any persistent Chromium profile on Windows.
            profile = QWebEngineProfile()
            profile.setHttpCacheType(QWebEngineProfile.MemoryHttpCache)
            profile.setPersistentCookiesPolicy(QWebEngineProfile.NoPersistentCookies)

            page = SmokePage(profile)
            view = QWebEngineView()
            view.setPage(page)
            view.resize(960, 640)
            view.show()

            load_url(view, page_url, timeout_ms)
            details = wait_for_page_data(page, timeout_ms)
            if details["preview"]:
                raise AssertionError("real page fell back to preview data")
            if UNKNOWN_MODEL not in details["modelNames"]:
                raise AssertionError(f"synthetic model missing from live page: {details!r}")
            if "未提供" not in details["modelCosts"]:
                raise AssertionError(
                    f"unknown price was not rendered as '未提供': {details['modelCosts']!r}"
                )

            quota_check = json.loads(evaluate(page, """JSON.stringify((() => {
                const q = {status:'fresh', label:'Synthetic', remaining_percent:null,
                           balance_text:'<b>2.00 CNY</b>', is_current:true};
                const focus = {id:'claude', quota:q, quota_windows:[q],
                    metadata:{ccswitch_providers:[{...q, name:'Synthetic'}]}};
                const card = document.createElement('div');
                card.innerHTML = renderCardQuota(focus);
                const detail = document.createElement('div');
                detail.innerHTML = renderDetailQuotaWindows(focus, null);
                return {nullPercent:absolutePercent(null), nullRatio:ratioToPercent(null),
                    cardHidden:card.querySelector('.agent-card-quota-bar').style.display,
                    detailHidden:detail.querySelector('.progress-bar-bg').style.display,
                    injectedMarkup:!!card.querySelector('b') || !!detail.querySelector('b')};
            })())""", timeout_ms))
            if quota_check != dict(nullPercent=None, nullRatio=None, cardHidden='none',
                                   detailHidden='none', injectedMarkup=False):
                raise AssertionError(f"Unknown quota or escaping regression: {quota_check!r}")

            theme_json = evaluate(
                page,
                "JSON.stringify((() => {"
                "const before = document.documentElement.getAttribute('data-theme');"
                "document.getElementById('themeToggleBtn').click();"
                "return {before, after: document.documentElement.getAttribute('data-theme'), "
                "className: document.documentElement.className};"
                "})())",
                timeout_ms,
            )
            theme_state = json.loads(theme_json)
            if theme_state["before"] != "dark" or theme_state["after"] != "light":
                raise AssertionError(f"theme toggle state mismatch: {theme_state!r}")

            wait_ms(1500)
            errors = [
                item for item in page.console_messages if item["level"] == 2
            ]
            if errors:
                raise AssertionError(f"JavaScript console errors: {errors!r}")

            args.artifact_dir.mkdir(parents=True, exist_ok=True)
            screenshot = args.artifact_dir / "qt-web-smoke.png"
            if not view.grab().save(str(screenshot), "PNG"):
                raise OSError(f"failed to save screenshot: {screenshot}")
            print(
                json.dumps(
                    {
                        "status": "PASS",
                        "page_url": page_url,
                        "database": str(db_path),
                        "web_dir": str(web_dir),
                        "unknown_model": UNKNOWN_MODEL,
                        "unknown_price": "未提供",
                        "theme": theme_state,
                        "javascript_errors": errors,
                        "console_messages": page.console_messages,
                        "screenshot": str(screenshot),
                        "real_home_scanned": False,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        except Exception as error:
            print(f"QtWebEngine smoke FAIL: {error}", file=sys.stderr)
            if view is not None and isinstance(view.page(), SmokePage):
                messages = view.page().console_messages
                if messages:
                    print(json.dumps(messages, ensure_ascii=False), file=sys.stderr)
            return 1
        finally:
            if view is not None:
                view.close()
            if server is not None:
                server.shutdown()
                server.server_close()
            if server_thread is not None:
                server_thread.join(timeout=2)
            if profile is not None:
                profile.deleteLater()
            app.processEvents()
            app.quit()


if __name__ == "__main__":
    raise SystemExit(main())
