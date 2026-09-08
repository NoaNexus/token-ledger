from datetime import date
from pathlib import Path
import sqlite3

from tokenledger.analytics import _reconcile_account_rollups
from tokenledger.models import DiscoveredFile
from tokenledger.providers.antigravity import AntigravityAdapter
from tokenledger.providers.ccswitch import safe_usage_rollups
from tokenledger.providers.claude import ClaudeAdapter


def test_reconcile_preserves_larger_session_events():
    # Day 1: Session total 50,000 > Account total 4,000 -> Keep session
    # Day 2: Account total 60,000 > Session total 1,000 -> Keep account
    rows = [
        # Day 1
        {
            "agent": "claude",
            "local_date": date(2026, 9, 2),
            "usage_scope": "session",
            "route": "CC Switch",
            "platform": "DeepSeek",
            "model": "synthetic-model",
            "total_tokens": 50000,
            "call_count": 5,
        },
        {
            "agent": "claude",
            "local_date": date(2026, 9, 2),
            "usage_scope": "account_daily",
            "route": "CC Switch 账户日汇总",
            "platform": "DeepSeek",
            "model": "synthetic-model",
            "total_tokens": 4000,
            "call_count": 1,
        },
        # Day 2
        {
            "agent": "claude",
            "local_date": date(2026, 9, 3),
            "usage_scope": "session",
            "route": "CC Switch",
            "platform": "DeepSeek",
            "model": "synthetic-model",
            "total_tokens": 1000,
            "call_count": 1,
        },
        {
            "agent": "claude",
            "local_date": date(2026, 9, 3),
            "usage_scope": "account_daily",
            "route": "CC Switch 账户日汇总",
            "platform": "DeepSeek",
            "model": "synthetic-model",
            "total_tokens": 60000,
            "call_count": 10,
        },
    ]

    kept, diagnostics = _reconcile_account_rollups(rows)
    assert len(kept) == 2
    # Day 1 kept session
    day1_kept = [r for r in kept if r["local_date"] == date(2026, 9, 2)][0]
    assert day1_kept["usage_scope"] == "session"
    assert day1_kept["total_tokens"] == 50000

    # Day 2 kept account
    day2_kept = [r for r in kept if r["local_date"] == date(2026, 9, 3)][0]
    assert day2_kept["usage_scope"] == "account_daily"
    assert day2_kept["total_tokens"] == 60000


def test_claude_discover_files(tmp_path: Path):
    claude_home = tmp_path / ".claude" / "projects" / "sub"
    claude_home.mkdir(parents=True)
    (claude_home / "session1.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")

    claude_3p = tmp_path / "AppData" / "Local" / "Claude-3p" / "local-agent-mode-sessions" / "folder" / ".claude" / "projects" / "out"
    claude_3p.mkdir(parents=True)
    (claude_3p / "session2.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
    (claude_3p.parent / "audit.jsonl").write_text('{"type":"audit"}\n', encoding="utf-8")

    adapter = ClaudeAdapter(tmp_path)
    discovered = adapter.discover_files()
    paths = [d.path.name for d in discovered]

    assert "session1.jsonl" in paths
    assert "session2.jsonl" in paths
    assert "audit.jsonl" not in paths


def test_antigravity_prefers_full_transcript(tmp_path: Path):
    logs_dir = tmp_path / ".gemini" / "antigravity" / "brain" / "conv1" / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "transcript.jsonl").write_text('{"type":"short"}\n', encoding="utf-8")
    (logs_dir / "transcript_full.jsonl").write_text('{"type":"full"}\n', encoding="utf-8")

    adapter = AntigravityAdapter(tmp_path)
    discovered = adapter.discover_files()
    assert len(discovered) == 1
    assert discovered[0].path.name == "transcript_full.jsonl"


def test_ccswitch_provider_quotas_and_claude_probe(tmp_path: Path):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    cc_dir = tmp_path / ".cc-switch"
    cc_dir.mkdir(parents=True)
    db_file = cc_dir / "cc-switch.db"

    with sqlite3.connect(db_file) as conn:
        conn.execute("""
            CREATE TABLE providers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                app_type TEXT NOT NULL,
                settings_config TEXT,
                website_url TEXT,
                sort_index INTEGER NOT NULL DEFAULT 0,
                is_current INTEGER NOT NULL DEFAULT 0,
                meta TEXT
            )
        """)
        conn.execute("""
            INSERT INTO providers (id, name, app_type, settings_config, website_url, sort_index, is_current)
            VALUES
                ('p1', 'DeepSeek', 'claude-desktop', '{"env":{"ANTHROPIC_AUTH_TOKEN":"test-token"}}', 'https://platform.deepseek.com', 0, 1),
                ('p2', 'Zhipu GLM', 'claude-desktop', '{}', 'https://open.bigmodel.cn', 1, 0)
        """)

    adapter = ClaudeAdapter(tmp_path)
    probe = adapter.probe()
    assert probe.metadata["cc_switch"] is True
    assert len(probe.metadata["budget_windows"]) >= 2
    # Current active provider should be first
    first_window = probe.metadata["budget_windows"][0]
    assert "DeepSeek" in first_window["label"]
    assert "(当前路由)" in first_window["label"]
    assert first_window["status"] == "unavailable"
    assert first_window["remaining_percent"] is None
    assert first_window["balance_text"] is None

    sources = probe.metadata["ccswitch_providers"]
    assert len(sources) == 2
    assert sources[0]["name"] == "DeepSeek"
    assert sources[0]["is_current"] is True
    assert sources[1]["name"] == "Zhipu GLM"
    assert sources[1]["is_current"] is False
