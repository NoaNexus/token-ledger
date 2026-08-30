from __future__ import annotations

from pathlib import Path

from tokenledger import analytics
from tokenledger.analytics import build_dashboard
from tokenledger.db import TokenDatabase
from tokenledger.models import ParsedFile, ProviderProbe, UsageEvent
from tokenledger.providers.claude import ClaudeAdapter
from tokenledger.providers.common import stable_id
from tokenledger.scanner import ScanCoordinator


def test_dashboard_metrics_and_truthful_unavailable_quota(tmp_path: Path) -> None:
    database = TokenDatabase(tmp_path / "ledger.db")
    parsed = ParsedFile(
        events=[
            UsageEvent(
                event_id="event-1",
                agent="claude",
                route="CC Switch",
                platform="平台未识别（历史）",
                model="model-a",
                session_id="session-a",
                occurred_at="2026-08-28T01:00:00Z",
                input_tokens=100,
                cached_input_tokens=80,
                output_tokens=20,
                total_tokens=120,
            )
        ],
        session_count=1,
    )
    database.replace_file("file-1", "claude", "fixture", 1, 10, parsed, "2026-08-28T01:01:00Z")
    database.update_provider(
        "claude",
        ProviderProbe("ready", "Claude", "fixture", "ready"),
        "2026-08-28T01:01:00Z",
    )
    dashboard = build_dashboard(
        database,
        {"status": "ready", "progress": 100, "message": "done", "last_completed_at": None},
        "Asia/Shanghai",
        30,
        None,
    )
    assert dashboard["summary"]["total"] == 120
    assert dashboard["summary"]["net_usage"] == 40
    assert dashboard["summary"]["cache_hit_rate"] == 0.8
    assert dashboard["lifetime"]["summary"]["total"] == 120
    assert dashboard["lifetime"]["agents"]["claude"]["total"] == 120
    claude = next(item for item in dashboard["agents"] if item["id"] == "claude")
    assert claude["quota"]["status"] == "unavailable"


def test_shanghai_timezone_falls_back_without_zoneinfo(monkeypatch) -> None:
    monkeypatch.setattr(analytics, "_ZoneInfo", None)
    resolved = analytics._resolve_timezone("Asia/Shanghai")
    assert resolved.utcoffset(None).total_seconds() == 8 * 60 * 60


def test_dashboard_separates_estimated_usage(tmp_path: Path) -> None:
    database = TokenDatabase(tmp_path / "ledger.db")
    parsed = ParsedFile(
        events=[
            UsageEvent(
                event_id="estimated-1",
                agent="antigravity",
                route="本地可见文本估算",
                platform="Google Antigravity",
                model="模型未记录（估算）",
                session_id="session-a",
                occurred_at="2026-08-28T01:00:00Z",
                input_tokens=80,
                output_tokens=20,
                total_tokens=100,
                usage_mode="estimated",
            )
        ],
        session_count=1,
    )
    database.replace_file("file-est", "antigravity", "fixture", 1, 10, parsed, "2026-08-28T01:01:00Z")
    dashboard = build_dashboard(
        database,
        {"status": "ready", "progress": 100, "message": "done", "last_completed_at": None},
        "Asia/Shanghai",
        30,
        None,
    )
    assert dashboard["summary"]["total"] == 100
    assert dashboard["summary"]["reported_total"] == 0
    assert dashboard["summary"]["estimated_total"] == 100
    assert dashboard["summary"]["contains_estimates"] is True


def test_account_day_rollup_replaces_overlapping_claude_session_usage(tmp_path: Path) -> None:
    database = TokenDatabase(tmp_path / "ledger.db")
    session_rows = ParsedFile(
        events=[
            UsageEvent(
                event_id="session-overlap",
                agent="claude",
                route="CC Switch",
                platform="平台未识别（历史）",
                model="session-model",
                session_id="session-overlap",
                occurred_at="2026-08-28T01:00:00Z",
                input_tokens=100,
                output_tokens=20,
                total_tokens=120,
            ),
            UsageEvent(
                event_id="session-only",
                agent="claude",
                route="CC Switch",
                platform="平台未识别（历史）",
                model="session-model",
                session_id="session-only",
                occurred_at="2026-08-29T01:00:00Z",
                input_tokens=40,
                output_tokens=10,
                total_tokens=50,
            ),
        ],
        session_count=2,
    )
    account_rows = ParsedFile(
        events=[
            UsageEvent(
                event_id="account-day",
                agent="claude",
                route="CC Switch 账户日汇总",
                platform="DeepSeek",
                model="deepseek-test",
                session_id="ccswitch-account:p1",
                occurred_at="2026-08-28T04:00:00Z",
                input_tokens=450,
                cached_input_tokens=400,
                output_tokens=50,
                total_tokens=500,
                usage_scope="account_daily",
                call_count=10,
            )
        ]
    )
    database.replace_file("sessions", "claude", "fixture", 1, 10, session_rows, "2026-08-29T02:00:00Z")
    database.replace_file("account", "claude", "fixture", 1, 10, account_rows, "2026-08-29T02:00:00Z")

    dashboard = build_dashboard(
        database,
        {"status": "ready", "progress": 100, "message": "done", "last_completed_at": None},
        "Asia/Shanghai",
        30,
        None,
    )
    assert dashboard["summary"]["total"] == 550
    assert dashboard["summary"]["calls"] == 11
    assert dashboard["summary"]["sessions"] == 1
    assert dashboard["summary"]["session_count_complete"] is False
    claude = next(item for item in dashboard["agents"] if item["id"] == "claude")
    assert claude["reconciliation"]["suppressed_session_events"] == 1
    assert claude["reconciliation"]["suppressed_session_tokens"] == 120
    assert {item["model"] for item in dashboard["models"]} == {"deepseek-test", "session-model"}


def test_temporary_cc_switch_read_error_keeps_last_valid_account_rollup(tmp_path: Path) -> None:
    source = tmp_path / ".cc-switch" / "cc-switch.db"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"temporarily unreadable sqlite content")
    database = TokenDatabase(tmp_path / "ledger.db")
    file_id = stable_id("claude", str(source.resolve()).lower())
    database.replace_file(
        file_id,
        "claude",
        "fixture",
        source.stat().st_mtime_ns,
        source.stat().st_size,
        ParsedFile(
            events=[
                UsageEvent(
                    event_id="last-good-rollup",
                    agent="claude",
                    route="CC Switch 账户日汇总",
                    platform="DeepSeek",
                    model="deepseek-test",
                    session_id="ccswitch-account:p1",
                    occurred_at="2026-08-28T04:00:00Z",
                    total_tokens=500,
                    usage_scope="account_daily",
                    call_count=10,
                )
            ]
        ),
        "2026-08-28T05:00:00Z",
    )
    status = ScanCoordinator(database, [ClaudeAdapter(tmp_path)]).scan_sync()
    assert status["status"] == "partial"
    assert [row["event_id"] for row in database.usage_rows(agent="claude")] == [
        "last-good-rollup"
    ]
