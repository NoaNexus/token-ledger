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
            "total_tokens": 50000,
            "call_count": 5,
        },
        {
            "agent": "claude",
            "local_date": date(2026, 9, 2),
            "usage_scope": "account_daily",
            "total_tokens": 4000,
            "call_count": 1,
        },
        # Day 2
        {
            "agent": "claude",
            "local_date": date(2026, 9, 3),
            "usage_scope": "session",
            "total_tokens": 1000,
            "call_count": 1,
        },
        {
            "agent": "claude",
            "local_date": date(2026, 9, 3),
            "usage_scope": "account_daily",
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
