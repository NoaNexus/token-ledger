from __future__ import annotations

import time
from pathlib import Path

from tokenledger.analytics import build_dashboard, _CACHE
from tokenledger.db import TokenDatabase
from tokenledger.models import ParsedFile, ProviderProbe, UsageEvent


def test_snapshot_cache_and_net_usage_precision(tmp_path: Path) -> None:
    _CACHE.clear()
    database = TokenDatabase(tmp_path / "test_perf.db")
    parsed = ParsedFile(
        events=[
            UsageEvent(
                event_id="e1",
                agent="codex",
                route="原生订阅",
                platform="OpenAI",
                model="gpt-5",
                session_id="s1",
                occurred_at="2026-09-01T10:00:00Z",
                input_tokens=1000,
                cached_input_tokens=900,
                output_tokens=150,
                reasoning_tokens=50,
                total_tokens=1150,
            ),
            UsageEvent(
                event_id="e2",
                agent="claude",
                route="CC Switch",
                platform="阿里云百炼",
                model="claude-3-7-sonnet",
                session_id="s2",
                occurred_at="2026-09-02T10:00:00Z",
                input_tokens=500,
                cached_input_tokens=400,
                output_tokens=100,
                reasoning_tokens=0,
                total_tokens=600,
            ),
        ],
        session_count=2,
    )
    database.replace_file("file-1", "codex", "codex/session1", 1, 100, parsed, "2026-09-02T10:00:00Z")
    database.update_provider("codex", ProviderProbe("ready", "Codex", "c1", "ok"), "2026-09-02T10:00:00Z")
    database.update_provider("claude", ProviderProbe("ready", "Claude", "c2", "ok"), "2026-09-02T10:00:00Z")

    scan_status = {"status": "ready", "progress": 100, "message": "done", "last_completed_at": None}

    # 1. First build (populates cache)
    dash1 = build_dashboard(database, scan_status, "Asia/Shanghai", 30, None)
    summary = dash1["summary"]

    # Mathematical precision of Net Usage: (Input - Cache) + Output
    assert summary["input"] == 1500
    assert summary["cached_input"] == 1300
    assert summary["non_cached_input"] == 200
    assert summary["output"] == 250
    assert summary["net_usage"] == 450  # 200 + 250
    assert summary["total"] == 1750
    assert abs(summary["cache_hit_rate"] - (1300 / 1500)) < 1e-6

    # 2. Second build (should hit memory payload cache)
    t0 = time.time()
    dash2 = build_dashboard(database, scan_status, "Asia/Shanghai", 30, None)
    t1 = time.time()
    assert (t1 - t0) < 0.05  # sub-50ms cache response
    assert dash2["summary"]["net_usage"] == 450

    # 3. Switching agent filter:
    dash_claude = build_dashboard(database, scan_status, "Asia/Shanghai", 30, "claude")
    assert dash_claude["summary"]["input"] == 500
    assert dash_claude["summary"]["cached_input"] == 400
    assert dash_claude["summary"]["net_usage"] == 200  # (500 - 400) + 100
    assert dash_claude["meta"]["selected_agent"] == "claude"

    # 4. Cache invalidation on new file replace
    parsed_new = ParsedFile(
        events=[
            UsageEvent(
                event_id="e3",
                agent="antigravity",
                route="本地可见文本估算",
                platform="Google",
                model="gemini-2.5",
                session_id="s3",
                occurred_at="2026-09-03T10:00:00Z",
                input_tokens=200,
                cached_input_tokens=0,
                output_tokens=50,
                total_tokens=250,
            ),
        ],
        session_count=1,
    )
    database.replace_file("file-2", "antigravity", "ag/session1", 2, 50, parsed_new, "2026-09-03T10:00:00Z")

    dash3 = build_dashboard(database, scan_status, "Asia/Shanghai", 30, None)
    assert dash3["summary"]["total"] == 2000  # 1750 + 250
    assert dash3["summary"]["net_usage"] == 700  # 450 + 250
