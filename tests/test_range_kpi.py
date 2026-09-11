from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "web" / "app.js"


def _overview_source() -> str:
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function renderOverview(data) {")
    end = source.index("function renderDetail(data)", start)
    return source[start:end]


def test_overview_first_kpi_reflects_range_and_preserves_lifetime() -> None:
    overview = _overview_source()
    first_card, _ = overview.split("<!-- Card 2:", 1)

    assert "const lifetime = data.lifetime?.summary || summary;" in overview
    assert "summary.total" in first_card
    assert "全周期累计" in first_card or "全周期历史累计" in first_card

    for field in ("total", "sessions"):
        assert f"lifetime.{field}" in first_card
    assert "costQualifier(summary)" in first_card
    assert "costInlineText(summary)" in first_card


def test_overview_agent_accumulated_badges_still_use_lifetime_data() -> None:
    overview = _overview_source()

    assert "data.lifetime?.agents?.[agent.id]" in overview
    assert "data.lifetime?.reconciliation?.[agent.id]" in overview


def test_all_range_keeps_full_history_label() -> None:
    source = APP_JS.read_text(encoding="utf-8")

    assert 'all: "全部历史"' in source
