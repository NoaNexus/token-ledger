from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tokenledger.models import DiscoveredFile, QuotaSnapshot
from tokenledger.providers import antigravity, ccswitch, claude
from tokenledger.providers.antigravity import AntigravityAdapter
from tokenledger.providers.claude import ClaudeAdapter


class _JsonResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _disable_antigravity_server(monkeypatch, tmp_path: Path) -> None:
    class FakePsutil:
        class NoSuchProcess(Exception):
            pass

        class AccessDenied(Exception):
            pass

        @staticmethod
        def process_iter(*args, **kwargs):
            return []

    monkeypatch.setitem(sys.modules, "psutil", FakePsutil)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "no-real-home"))


def test_deepseek_requires_official_base_url_without_network(monkeypatch) -> None:
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("network must not be used for an untrusted base URL")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    ccswitch._BALANCE_CACHE.clear()
    result = ccswitch._query_deepseek_balance("synthetic-token", "https://proxy.example.test")
    assert result["status"] == "unavailable"
    assert result["balance_text"] is None
    assert calls == []


def test_deepseek_rejects_bad_base_urls_and_cross_origin_redirect() -> None:
    assert not ccswitch._is_official_deepseek_base_url("https://api.deepseek.com:not-a-port")
    assert not ccswitch._is_official_deepseek_base_url("https://user:secret@api.deepseek.com")

    request = urllib.request.Request(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": "Bearer synthetic-token"},
    )
    same_origin = ccswitch._SameOriginRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://api.deepseek.com/user/balance?v=2",
    )
    assert same_origin is not None
    assert same_origin.full_url == "https://api.deepseek.com/user/balance?v=2"
    with pytest.raises(urllib.error.HTTPError, match="cross-origin redirect rejected"):
        ccswitch._SameOriginRedirectHandler().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://evil.example.test/collect",
        )


def test_deepseek_cache_isolated_and_failed_refresh_is_stale(monkeypatch) -> None:
    ccswitch._BALANCE_CACHE.clear()
    responses = iter(
        [
            _JsonResponse({"is_available": True, "balance_infos": [{"total_balance": "1.00", "currency": "CNY"}]}),
            _JsonResponse({"is_available": True, "balance_infos": [{"total_balance": "2.00", "currency": "CNY"}]}),
        ]
    )
    calls = []

    class FakeOpener:
        def open(self, request, **kwargs):
            calls.append(request)
            return next(responses)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: FakeOpener())
    first = ccswitch._query_deepseek_balance("synthetic-token-A", "https://api.deepseek.com")
    second = ccswitch._query_deepseek_balance("synthetic-token-B", "https://api.deepseek.com")
    assert first["balance_text"] == "1.00 CNY"
    assert second["balance_text"] == "2.00 CNY"
    assert len(calls) == 2

    first_key = "deepseek:" + hashlib.sha256("synthetic-token-A".encode()).hexdigest()
    cached_time, cached_value = ccswitch._BALANCE_CACHE[first_key]
    ccswitch._BALANCE_CACHE[first_key] = (cached_time - 60, cached_value)

    class FailingOpener:
        def open(self, *args, **kwargs):
            raise TimeoutError("synthetic offline")

    def fail(*args, **kwargs):
        return FailingOpener()

    monkeypatch.setattr(urllib.request, "build_opener", fail)
    stale = ccswitch._query_deepseek_balance("synthetic-token-A", "https://api.deepseek.com")
    assert stale["status"] == "stale"
    assert stale["balance_text"] == first["balance_text"]
    assert stale["updated_at"] == first["updated_at"]


def test_ccswitch_does_not_guess_non_official_provider_quota(tmp_path: Path) -> None:
    database_path = tmp_path / ".cc-switch" / "cc-switch.db"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE providers(
              id TEXT, name TEXT, app_type TEXT, settings_config TEXT,
              website_url TEXT, sort_index INTEGER, is_current INTEGER, meta TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO providers VALUES(?,?,?,?,?,?,?,?)",
            [
                ("p1", "DeepSeek", "claude", '{"env":{"ANTHROPIC_AUTH_TOKEN":"synthetic"}}', "https://proxy.example.test", 0, 1, None),
                ("p2", "Zhipu GLM", "claude", "{}", "https://open.bigmodel.cn", 1, 0, None),
            ],
        )
    windows, sources = ccswitch.safe_ccswitch_provider_quotas(tmp_path)
    assert len(windows) == len(sources) == 2
    assert all(item["status"] == "unavailable" for item in windows)
    assert all(item["remaining_percent"] is None for item in windows)
    assert all(item["balance_text"] is None for item in windows)


def test_ccswitch_metadata_sanitizes_urls_and_non_dict_env(tmp_path: Path) -> None:
    database_path = tmp_path / ".cc-switch" / "cc-switch.db"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE providers(
              id TEXT, name TEXT, app_type TEXT, settings_config TEXT,
              website_url TEXT, sort_index INTEGER, is_current INTEGER, meta TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO providers VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    "p1",
                    "Gateway",
                    "claude",
                    '{"env": ["not", "a", "mapping"]}',
                    "https://user:secret@proxy.example.test/public?api_key=hidden#fragment",
                    0,
                    1,
                    "unused metadata",
                ),
                (
                    "p2",
                    "Fallback",
                    "claude",
                    '{"env":{"ANTHROPIC_BASE_URL":"https://gateway.example.test/v1?token=hidden#fragment"}}',
                    None,
                    1,
                    0,
                    None,
                ),
            ],
        )

    windows, sources = ccswitch.safe_ccswitch_provider_quotas(tmp_path)
    by_id = {item["snapshot_id"].split("-")[-1]: item for item in windows}
    source_by_id = {item["id"]: item for item in sources}
    assert by_id["p1"]["website_url"] == "https://proxy.example.test/public"
    assert source_by_id["p1"]["website_url"] == "https://proxy.example.test/public"
    assert by_id["p2"]["website_url"] == "https://gateway.example.test/v1"
    assert source_by_id["p2"]["website_url"] == "https://gateway.example.test/v1"
    serialized = json.dumps([windows, sources], ensure_ascii=False)
    assert "secret" not in serialized
    assert "api_key" not in serialized
    assert "token=hidden" not in serialized


def test_ccswitch_merges_rollup_and_proxy_per_provider_model(tmp_path: Path) -> None:
    database_path = tmp_path / ".cc-switch" / "cc-switch.db"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE providers(id TEXT, name TEXT);
            CREATE TABLE usage_daily_rollups(
              date TEXT, app_type TEXT, provider_id TEXT, model TEXT,
              request_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,
              cache_read_tokens INTEGER, cache_creation_tokens INTEGER,
              input_token_semantics INTEGER
            );
            CREATE TABLE proxy_request_logs(
              created_at INTEGER, provider_id TEXT, app_type TEXT, model TEXT,
              input_tokens INTEGER, output_tokens INTEGER,
              cache_read_tokens INTEGER, cache_creation_tokens INTEGER
            );
            """
        )
        connection.executemany(
            "INSERT INTO providers VALUES(?,?)",
            [
                ("p1", "Same Provider"),
                ("p2", "Proxy Only"),
                ("p3", "Rollup Only"),
                ("p4", "Multiple Semantics"),
            ],
        )
        connection.execute(
            "INSERT INTO usage_daily_rollups VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("2026-09-01", "claude", "p1", "synthetic-model", 1, 100, 10, 0, 0, 2),
        )
        connection.execute(
            "INSERT INTO usage_daily_rollups VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("2026-09-01", "claude", "p3", "synthetic-model", 1, 30, 5, 0, 0, 2),
        )
        connection.executemany(
            "INSERT INTO usage_daily_rollups VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                ("2026-09-01", "claude", "p4", "synthetic-model", 1, 30, 5, 4, 6, 2),
                ("2026-09-01", "claude", "p4", "synthetic-model", 2, 12, 3, 0, 0, 1),
            ],
        )
        created_at = int(datetime(2026, 9, 1, 12, tzinfo=timezone.utc).timestamp())
        connection.executemany(
            "INSERT INTO proxy_request_logs VALUES(?,?,?,?,?,?,?,?)",
            [
                (created_at, "p1", "claude", "synthetic-model", 300, 30, 0, 0),
                (created_at, "p2", "claude", "synthetic-model", 200, 20, 0, 0),
            ],
        )

    rows = ccswitch.safe_usage_rollups(tmp_path)
    by_provider = {row["provider_id"]: row for row in rows}
    assert by_provider["p1"]["input_tokens"] == 300
    assert by_provider["p2"]["input_tokens"] == 200
    assert by_provider["p3"]["input_tokens"] == 30
    assert by_provider["p4"]["input_tokens"] == 52
    assert by_provider["p4"]["output_tokens"] == 8
    assert by_provider["p4"]["request_count"] == 3


def test_antigravity_no_server_returns_no_quota(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / ".gemini" / "antigravity").mkdir(parents=True)
    _disable_antigravity_server(monkeypatch, tmp_path)
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("no server means no quota request")

    monkeypatch.setattr(antigravity.urllib.request, "urlopen", fail)
    quotas = AntigravityAdapter(tmp_path)._fetch_quota_windows()
    assert quotas == []
    assert calls == []


def test_antigravity_failed_refresh_keeps_real_stale_timestamp(monkeypatch, tmp_path: Path) -> None:
    _disable_antigravity_server(monkeypatch, tmp_path)
    monkeypatch.setattr(antigravity.urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()))
    adapter = AntigravityAdapter(tmp_path)
    previous = QuotaSnapshot(
        snapshot_id="antigravity:synthetic",
        agent="antigravity",
        label="Synthetic window",
        status="fresh",
        remaining_percent=42.0,
        used_percent=58.0,
        window_minutes=300,
        resets_at="2026-09-10T00:00:00Z",
        updated_at="2026-09-08T00:00:00Z",
        message="official synthetic result",
    )
    adapter._cached_quotas = [previous]
    adapter._cached_quotas_time = datetime.now(timezone.utc) - timedelta(seconds=16)
    quotas = adapter._fetch_quota_windows()
    assert len(quotas) == 1
    assert quotas[0].status == "stale"
    assert quotas[0].remaining_percent == 42.0
    assert quotas[0].updated_at == previous.updated_at
    assert "旧快照" in quotas[0].message


def test_antigravity_reparses_when_only_conversation_db_changes(monkeypatch, tmp_path: Path) -> None:
    logs_dir = tmp_path / ".gemini" / "antigravity" / "brain" / "conv1" / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True)
    transcript = logs_dir / "transcript.jsonl"
    transcript.write_text('{"type":"USER_INPUT","content":"secret prompt"}\n', encoding="utf-8")
    database_path = tmp_path / ".gemini" / "antigravity" / "conversations" / "conv1.db"
    database_path.parent.mkdir(parents=True)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE gen_metadata(idx INTEGER, data BLOB)")
    adapter = AntigravityAdapter(tmp_path)
    discovered = DiscoveredFile(transcript, "fixture")
    assert adapter.should_reparse(discovered) is True
    adapter.mark_parsed(discovered)
    assert adapter.should_reparse(discovered) is False
    transcript_signature = transcript.stat().st_mtime_ns, transcript.stat().st_size
    old_db_ns = database_path.stat().st_mtime_ns
    with sqlite3.connect(database_path) as connection:
        connection.execute("INSERT INTO gen_metadata VALUES(?,?)", (1, b"synthetic"))
    os.utime(database_path, ns=(old_db_ns + 2_000_000_000, old_db_ns + 2_000_000_000))
    assert (transcript.stat().st_mtime_ns, transcript.stat().st_size) == transcript_signature
    assert adapter.should_reparse(discovered) is True


def test_antigravity_metadata_uses_anonymous_session_id(monkeypatch, tmp_path: Path) -> None:
    logs_dir = tmp_path / ".gemini" / "antigravity" / "brain" / "conv1" / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True)
    transcript = logs_dir / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"type": "USER_INPUT", "created_at": "2026-09-08T00:00:00Z", "content": "SECRET PROJECT TITLE"}) + "\n",
        encoding="utf-8",
    )
    _disable_antigravity_server(monkeypatch, tmp_path)
    adapter = AntigravityAdapter(tmp_path)
    parsed = adapter.parse_file(DiscoveredFile(transcript, "fixture"))
    metadata_text = json.dumps(parsed.metadata, ensure_ascii=False)
    assert "SECRET PROJECT TITLE" not in metadata_text
    assert parsed.metadata["session_title"] == "工程会话 conv1"


def test_claude_route_cache_refreshes_and_unknown_platform_stays_unknown(monkeypatch, tmp_path: Path) -> None:
    states = iter(
        [
            SimpleNamespace(installed=True, route="first", platform="First", database_path=None),
            SimpleNamespace(installed=True, route="second", platform="Second", database_path=None),
        ]
    )
    monkeypatch.setattr(claude, "detect_cc_switch", lambda user_home: next(states))
    monkeypatch.setattr(claude, "unique_model_platforms", lambda user_home: {})
    adapter = ClaudeAdapter(tmp_path)
    assert adapter._get_switch_meta()[2] == "平台未识别（历史）"
    database_path = tmp_path / ".cc-switch" / "cc-switch.db"
    database_path.parent.mkdir(parents=True)
    database_path.write_bytes(b"changed dependency")
    assert adapter._get_switch_meta()[2] == "平台未识别（历史）"

    path = tmp_path / ".claude" / "projects" / "demo" / "session.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-09-08T00:00:00Z",
                "sessionId": "synthetic-session",
                "message": {
                    "id": "message-1",
                    "model": "unmapped-model",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    event = adapter.parse_file(DiscoveredFile(path, "fixture")).events[0]
    assert event.platform == "平台未识别（历史）"
