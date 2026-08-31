from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication, QLabel

from tokenledger.qt_native import AgentCard, QuotaRow, SourceCard, TokenFlowWidget, TrendChart, _initial_window_geometry


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def visible_text(widget: object) -> str:
    return "\n".join(label.text() for label in widget.findChildren(QLabel))


@pytest.mark.parametrize(
    ("available", "expected_maximum"),
    (
        (QRect(0, 0, 1920, 1040), (1440, 900)),
        (QRect(0, 0, 1366, 728), (1318, 656)),
        (QRect(1920, 0, 1024, 700), (976, 628)),
    ),
)
def test_initial_window_geometry_stays_inside_taskbar_work_area(
    available: QRect,
    expected_maximum: tuple[int, int],
) -> None:
    geometry = _initial_window_geometry(available)
    assert available.contains(geometry)
    assert geometry.width() <= expected_maximum[0]
    assert geometry.height() <= expected_maximum[1]
    assert geometry.center() == available.center()


def test_agent_card_keeps_cache_breakdown_and_quota(qt_app: QApplication) -> None:
    card = AgentCard()
    card.set_data(
        {
            "id": "codex",
            "name": "Codex",
            "status": "ready",
            "usage_available": True,
            "total": 220,
            "input": 180,
            "cached_input": 120,
            "cache_write": 11,
            "output": 40,
            "reasoning": 14,
            "net_usage": 100,
            "cache_hit_rate": 2 / 3,
            "sessions": 3,
            "session_count_complete": True,
            "models": 2,
            "calls": 8,
            "quota_windows": [
                {
                    "status": "ready",
                    "label": "5 小时窗口",
                    "remaining_percent": 62,
                    "used_percent": 38,
                    "resets_at": "2026-08-30T12:00:00Z",
                    "message": "服务端额度",
                }
            ],
            "reconciliation": {},
        },
        {"total": 1000},
    )
    text = visible_text(card)
    for expected in ("输入", "缓存读取", "缓存写入", "输出", "推理", "净用量", "缓存命中率"):
        assert expected in text
    assert "5 小时窗口" in text
    assert "剩余 62.0%" in text
    assert "全部历史 1,000 Token" in text


def test_unknown_quota_never_looks_like_zero_percent(qt_app: QApplication) -> None:
    row = QuotaRow()
    row.set_data(None)
    text = visible_text(row)
    assert "额度未提供" in text
    assert "—" in text
    assert "0.0%" not in text


def test_estimated_source_and_token_flow_are_explicit(qt_app: QApplication) -> None:
    source = SourceCard()
    source.set_data(
        {
            "label": "Antigravity 本地可见文本",
            "status": "limited",
            "path_hint": "~/.gemini/antigravity/brain",
            "files": 10,
            "events": 50,
            "sessions": 4,
            "message": "基于本地文本估算",
            "metadata": {"usage_mode": "estimated", "estimated_tokens": 372311},
        }
    )
    assert "本地估算：37.23 万 Token" in visible_text(source)

    flow = TokenFlowWidget()
    flow.set_data({"input": 100, "cached_input": 70, "output": 20, "reasoning": 8})
    accessible = flow.accessibleName()
    assert "输入 100" in accessible
    assert "缓存读取 70" in accessible
    assert "推理 8" in accessible


def test_expensive_custom_charts_reuse_their_paint_cache(qt_app: QApplication) -> None:
    flow = TokenFlowWidget()
    flow.resize(900, 152)
    flow.set_data({"input": 100, "cached_input": 70, "output": 20, "reasoning": 8})
    flow.show()
    flow.repaint()
    qt_app.processEvents()
    first_flow_cache = flow._paint_cache
    flow.repaint()
    qt_app.processEvents()
    assert first_flow_cache is not None
    assert flow._paint_cache is first_flow_cache
    assert flow._paint_cache_signature == (
        flow.width(),
        flow.height(),
        round(max(flow.devicePixelRatioF(), 1.0), 3),
    )
    assert flow._paint_cache.devicePixelRatio() == pytest.approx(max(flow.devicePixelRatioF(), 1.0))

    trend = TrendChart()
    trend.resize(900, 250)
    trend.set_data([{"date": "2026-08-30", "total": 100}])
    trend.show()
    trend.repaint()
    qt_app.processEvents()
    first_trend_cache = trend._paint_cache
    trend.repaint()
    qt_app.processEvents()
    assert first_trend_cache is not None
    assert trend._paint_cache is first_trend_cache
    assert trend._paint_cache_signature == (
        trend.width(),
        trend.height(),
        round(max(trend.devicePixelRatioF(), 1.0), 3),
    )
    assert trend._paint_cache.devicePixelRatio() == pytest.approx(max(trend.devicePixelRatioF(), 1.0))
