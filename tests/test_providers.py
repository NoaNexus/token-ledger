from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tokenledger.models import DiscoveredFile
from tokenledger.providers.antigravity import AntigravityAdapter
from tokenledger.providers.claude import ClaudeAdapter
from tokenledger.providers.codex import CodexAdapter
from tokenledger.providers.ccswitch import safe_budget_windows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_codex_parses_incremental_usage_and_quota(tmp_path: Path) -> None:
    path = tmp_path / ".codex" / "sessions" / "2026" / "08" / "28" / "rollout-123.jsonl"
    write_jsonl(
        path,
        [
            {"timestamp": "2026-08-28T00:00:00Z", "payload": {"type": "turn_context", "model": "gpt-test"}},
            {
                "timestamp": "2026-08-28T00:01:00Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 60,
                            "cache_write_input_tokens": 5,
                            "output_tokens": 20,
                            "reasoning_output_tokens": 7,
                            "total_tokens": 120,
                        }
                    },
                    "rate_limits": {
                        "primary": {"used_percent": 25, "window_minutes": 300, "resets_at": 1787880000}
                    },
                },
            },
        ],
    )
    parsed = CodexAdapter(tmp_path).parse_file(DiscoveredFile(path, "fixture"))
    assert len(parsed.events) == 1
    event = parsed.events[0]
    assert event.model == "gpt-test"
    assert (event.input_tokens, event.cached_input_tokens, event.output_tokens, event.total_tokens) == (100, 60, 20, 120)
    assert parsed.quotas[0].remaining_percent == 75
    assert parsed.quotas[0].label == "5 小时窗口"


def test_claude_normalizes_cache_into_input(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "projects" / "demo" / "session.jsonl"
    write_jsonl(
        path,
        [
            {
                "timestamp": "2026-08-28T00:02:00Z",
                "sessionId": "session-1",
                "uuid": "row-1",
                "message": {
                    "id": "message-1",
                    "model": "provider-model",
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 70,
                        "cache_creation_input_tokens": 20,
                        "output_tokens": 30,
                    },
                },
            }
        ],
    )
    parsed = ClaudeAdapter(tmp_path).parse_file(DiscoveredFile(path, "fixture"))
    event = parsed.events[0]
    assert event.input_tokens == 100
    assert event.cached_input_tokens == 70
    assert event.cache_write_tokens == 20
    assert event.output_tokens == 30
    assert event.total_tokens == 130


def test_claude_reads_cc_switch_account_day_rollups(tmp_path: Path) -> None:
    database_path = tmp_path / ".cc-switch" / "cc-switch.db"
    database_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE providers(id TEXT, name TEXT, app_type TEXT);
        CREATE TABLE usage_daily_rollups(
          date TEXT, app_type TEXT, provider_id TEXT, model TEXT,
          request_count INTEGER, input_tokens INTEGER, output_tokens INTEGER,
          cache_read_tokens INTEGER, cache_creation_tokens INTEGER,
          input_token_semantics INTEGER
        );
        """
    )
    connection.execute("INSERT INTO providers VALUES(?,?,?)", ("p1", "DeepSeek", "claude"))
    connection.execute(
        "INSERT INTO usage_daily_rollups VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("2026-07-01", "claude-desktop", "p1", "deepseek-test", 7, 10, 30, 70, 20, 2),
    )
    connection.commit()
    connection.close()

    adapter = ClaudeAdapter(tmp_path)
    discovered = adapter.discover_files()
    assert [item.path for item in discovered] == [database_path]
    parsed = adapter.parse_file(discovered[0])
    assert parsed.session_count == 0
    assert len(parsed.events) == 1
    event = parsed.events[0]
    assert event.input_tokens == 100
    assert event.cached_input_tokens == 70
    assert event.cache_write_tokens == 20
    assert event.output_tokens == 30
    assert event.total_tokens == 130
    assert event.usage_scope == "account_daily"
    assert event.call_count == 7
    assert event.platform == "DeepSeek"
    assert adapter.probe().metadata["account_rollup"]["days"] == 1


def test_claude_keeps_cc_switch_database_discovered_during_temporary_read_failure(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / ".cc-switch" / "cc-switch.db"
    database_path.parent.mkdir(parents=True)
    database_path.write_bytes(b"temporarily unreadable sqlite content")
    discovered = ClaudeAdapter(tmp_path).discover_files()
    assert [item.path for item in discovered] == [database_path]


def test_antigravity_estimates_visible_text_and_marks_it_estimated(tmp_path: Path) -> None:
    path = (
        tmp_path
        / ".gemini"
        / "antigravity"
        / "brain"
        / "conversation"
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )
    write_jsonl(
        path,
        [
            {"type": "USER_INPUT", "created_at": "2026-08-28T00:00:00Z", "content": "你好 hello"},
            {
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-08-28T00:01:00Z",
                "content": "完成 done",
                "thinking": "思考",
                "tool_calls": [{"name": "read_file"}],
            },
            {"type": "CHECKPOINT", "content": "不重复计算"},
        ],
    )
    adapter = AntigravityAdapter(tmp_path)
    parsed = adapter.parse_file(DiscoveredFile(path, "fixture"))
    assert len(parsed.events) == 2
    assert all(event.usage_mode == "estimated" for event in parsed.events)
    assert parsed.events[0].input_tokens > 0
    assert parsed.events[1].output_tokens > parsed.events[1].reasoning_tokens > 0
    assert parsed.session_count == 1
    assert parsed.metadata["usage_available"] is True
    probe = adapter.probe()
    assert probe.status == "limited"
    assert probe.metadata["usage_available"] is True
    assert probe.metadata["usage_mode"] == "estimated"
    assert probe.metadata["model_available"] is False
    assert probe.metadata["activity_count"] == 3
    assert probe.metadata["event_types"] == ["CHECKPOINT", "PLANNER_RESPONSE", "USER_INPUT"]


def test_cc_switch_budget_uses_configured_limit_and_local_rollup(tmp_path: Path) -> None:
    database_path = tmp_path / ".cc-switch" / "cc-switch.db"
    database_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE providers(
          id TEXT, name TEXT, app_type TEXT, limit_daily_usd REAL, limit_monthly_usd REAL
        );
        CREATE TABLE usage_daily_rollups(date TEXT, provider_id TEXT, total_cost_usd REAL);
        """
    )
    today = __import__("datetime").datetime.now().astimezone().date().isoformat()
    connection.execute("INSERT INTO providers VALUES(?,?,?,?,?)", ("p1", "测试平台", "claude", 10.0, None))
    connection.execute("INSERT INTO usage_daily_rollups VALUES(?,?,?)", (today, "p1", 2.5))
    connection.commit()
    connection.close()
    windows = safe_budget_windows(tmp_path)
    assert len(windows) == 1
    assert windows[0]["label"] == "测试平台 日预算"
    assert windows[0]["used_percent"] == 25.0
    assert windows[0]["remaining_percent"] == 75.0
