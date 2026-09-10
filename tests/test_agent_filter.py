from pathlib import Path
from datetime import datetime, timezone
import pytest

from tokenledger.analytics import build_dashboard
from tokenledger.db import TokenDatabase
from tokenledger.models import ParsedFile, UsageEvent

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = REPO_ROOT / "web" / "index.html"
APP_JS = REPO_ROOT / "web" / "app.js"
STYLES_CSS = REPO_ROOT / "web" / "styles.css"


def test_index_html_has_agent_filter_controls() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Topbar pills
    assert 'id="agentPills"' in html
    assert 'data-agent-filter="all"' in html
    assert 'data-agent-filter="codex"' in html
    assert 'data-agent-filter="claude"' in html
    assert 'data-agent-filter="antigravity"' in html

    # Detail hero pills
    assert 'class="agent-pill-group segmented-control"' in html
    assert 'Codex 全景' in html
    assert 'Claude & CC Switch' in html
    assert 'Antigravity DeepMind' in html


def test_styles_css_has_filter_styles() -> None:
    css = STYLES_CSS.read_text(encoding="utf-8")

    assert ".comparison-table tbody tr.is-selected-agent" in css
    assert ".comparison-table tbody tr.is-other-agent" in css
    assert ".workspace.is-filtering" in css
    assert "#section-codex" in css
    assert "#section-claude" in css
    assert "#section-antigravity" in css


def test_app_js_has_agent_filtering_logic() -> None:
    js = APP_JS.read_text(encoding="utf-8")

    # Core controller functions
    assert "function selectAgent(agent)" in js
    assert "function syncAgentPills(agent = state.agent)" in js
    assert "function setupAgentPills()" in js

    # renderOverview dynamic agent handling
    assert "activeAgent" in js
    assert "trendPanel(data.daily, activeAgent)" in js
    assert "rankingPanel(data.models, data.agents, activeAgent)" in js

    # renderDetail dynamic agent handling
    assert "is-selected-agent" in js
    assert "is-other-agent" in js
    assert "section-codex" in js
    assert "section-claude" in js
    assert "section-antigravity" in js


def test_analytics_build_dashboard_filters_by_agent(tmp_path: Path) -> None:
    db = TokenDatabase(tmp_path / "test.db")
    now = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)

    occurred = now.isoformat().replace("+00:00", "Z")
    event_codex = UsageEvent(
        event_id="e-codex-1",
        agent="codex",
        session_id="s-codex-1",
        occurred_at=occurred,
        model="gpt-5",
        route="direct",
        platform="openai",
        input_tokens=1000,
        cached_input_tokens=200,
        output_tokens=300,
        reasoning_tokens=50,
        total_tokens=1300,
    )
    event_claude = UsageEvent(
        event_id="e-claude-1",
        agent="claude",
        session_id="s-claude-1",
        occurred_at=occurred,
        model="claude-3-7-sonnet",
        route="direct",
        platform="anthropic",
        input_tokens=2000,
        cached_input_tokens=500,
        output_tokens=600,
        reasoning_tokens=100,
        total_tokens=2600,
    )

    db.replace_file("f-codex", "codex", "dummy.jsonl", 1, 100, ParsedFile(events=[event_codex]), occurred)
    db.replace_file("f-claude", "claude", "dummy.jsonl", 1, 100, ParsedFile(events=[event_claude]), occurred)

    all_data = build_dashboard(db, {}, "Asia/Shanghai", 30, None)
    assert all_data["summary"]["total"] == 3900
    assert all_data["lifetime"]["summary"]["total"] == 3900

    codex_data = build_dashboard(db, {}, "Asia/Shanghai", 30, "codex")
    assert codex_data["summary"]["total"] == 1300
    assert codex_data["lifetime"]["summary"]["total"] == 1300

    claude_data = build_dashboard(db, {}, "Asia/Shanghai", 30, "claude")
    assert claude_data["summary"]["total"] == 2600
    assert claude_data["lifetime"]["summary"]["total"] == 2600
