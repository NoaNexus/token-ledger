from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .analytics import build_dashboard
from .config import AppConfig
from .db import TokenDatabase
from .registry import REGISTRY
from .scanner import ScanCoordinator


STATIC_CONTENT_TYPES = {
    ".css": "text/css",
    ".html": "text/html",
    ".js": "application/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
}
UTF8_CONTENT_TYPES = {"application/javascript", "application/json", "image/svg+xml"}


def _static_content_type(path: Path) -> str:
    return STATIC_CONTENT_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


class TokenLedgerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: AppConfig, database: TokenDatabase, scanner: ScanCoordinator):
        self.config = config
        self.database = database
        self.scanner = scanner
        super().__init__((config.host, config.port), TokenLedgerHandler)


class TokenLedgerHandler(BaseHTTPRequestHandler):
    server: TokenLedgerServer
    server_version = "TokenLedger/0.1"

    def log_message(self, format_string: str, *args: Any) -> None:
        return

    def _valid_host(self) -> bool:
        host = self.headers.get("Host", "")
        return host == "localhost" or host.startswith("localhost:") or host == "127.0.0.1" or host.startswith("127.0.0.1:")

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._valid_host():
            self._send_json({"error": "invalid host"}, HTTPStatus.FORBIDDEN)
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._send_json({"ok": True, "scan": self.server.scanner.status()})
            return
        if parsed.path == "/api/dashboard":
            query = parse_qs(parsed.query)
            raw_days = query.get("days", ["30"])[0]
            try:
                days = None if raw_days == "all" else max(min(int(raw_days or 30), 3650), 1)
            except ValueError:
                days = 30
            raw_agent = query.get("agent", ["all"])[0]
            known_agents = {item.id for item in REGISTRY}
            selected_agent = raw_agent if raw_agent in known_agents else None
            payload = build_dashboard(
                self.server.database,
                self.server.scanner.status(),
                self.server.config.timezone,
                days,
                selected_agent,
            )
            self._send_json(payload)
            return
        if parsed.path == "/api/diagnostics":
            self._send_json(
                {
                    "registry": [
                        {
                            "id": item.id,
                            "name": item.name,
                            "color": item.color,
                            "capability": item.data_capability,
                        }
                        for item in REGISTRY
                    ],
                    "sources": self.server.database.provider_states(),
                    "privacy": [
                        "仅读取结构化 token 与额度字段",
                        "不保存提示词、回复、工具参数或原始日志正文",
                        "不读取、复制或记录 API key 与 access token",
                        "服务仅绑定 127.0.0.1，不向第三方发送统计数据",
                    ],
                }
            )
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        if not self._valid_host():
            self._send_json({"error": "invalid host"}, HTTPStatus.FORBIDDEN)
            return
        origin = self.headers.get("Origin")
        if origin and not (origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")):
            self._send_json({"error": "invalid origin"}, HTTPStatus.FORBIDDEN)
            return
        if self.headers.get("X-Token-Ledger-Request") != "same-origin":
            self._send_json({"error": "missing local request header"}, HTTPStatus.FORBIDDEN)
            return
        parsed_path = urlparse(self.path).path
        if parsed_path == "/api/scan":
            query = parse_qs(urlparse(self.path).query)
            accepted = self.server.scanner.start_background(force=query.get("force", ["0"])[0] == "1")
            self._send_json(
                {"ok": accepted, "status": "scanning" if accepted else "already_scanning"},
                HTTPStatus.ACCEPTED if accepted else HTTPStatus.CONFLICT,
            )
            return
        if parsed_path == "/api/theme":
            query = parse_qs(urlparse(self.path).query)
            theme = query.get("theme", ["dark"])[0]
            is_dark = theme != "light"
            if sys.platform == "win32":
                try:
                    import ctypes
                    from .native import apply_window_theme
                    user32 = ctypes.windll.user32
                    hwnd = user32.FindWindowW(None, "Token 账本 - 本机用量工作台")
                    if hwnd:
                        apply_window_theme(hwnd, is_dark)
                except Exception:
                    pass
            self._send_json({"ok": True, "theme": "dark" if is_dark else "light"})
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        candidate = (self.server.config.web_dir / relative).resolve()
        root = self.server.config.web_dir.resolve()
        if root not in candidate.parents and candidate != root:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            candidate = root / "index.html"
        try:
            body = candidate.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = _static_content_type(candidate)
        charset = "; charset=utf-8" if content_type.startswith("text/") or content_type in UTF8_CONTENT_TYPES else ""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type + charset)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(body)
