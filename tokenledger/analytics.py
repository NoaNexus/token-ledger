from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
except ImportError:  # Python 3.8 or an incomplete standard-library install.
    _ZoneInfo = None

from .db import TokenDatabase
from .registry import REGISTRY


TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _resolve_timezone(timezone_name: str) -> Any:
    if _ZoneInfo is not None:
        try:
            return _ZoneInfo(timezone_name)
        except Exception:
            pass
    if timezone_name == "Asia/Shanghai":
        return timezone(timedelta(hours=8), timezone_name)
    return datetime.now().astimezone().tzinfo or timezone.utc


def _metrics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    materialized = list(rows)
    sums = {field: sum(int(row.get(field) or 0) for row in materialized) for field in TOKEN_FIELDS}
    input_tokens = sums["input_tokens"]
    non_cached = max(input_tokens - sums["cached_input_tokens"], 0)
    reported_input = sum(
        int(row.get("input_tokens") or 0)
        for row in materialized
        if row.get("usage_mode") != "estimated"
    )
    estimated_total = sum(
        int(row.get("total_tokens") or 0)
        for row in materialized
        if row.get("usage_mode") == "estimated"
    )
    session_rows = [row for row in materialized if row.get("usage_scope") != "account_daily"]
    account_rows = [row for row in materialized if row.get("usage_scope") == "account_daily"]
    account_days = {
        row.get("local_date") or str(row.get("occurred_at") or "")[:10] for row in account_rows
    }
    return {
        "total": sums["total_tokens"],
        "reported_total": sums["total_tokens"] - estimated_total,
        "estimated_total": estimated_total,
        "contains_estimates": estimated_total > 0,
        "input": input_tokens,
        "cached_input": sums["cached_input_tokens"],
        "cache_write": sums["cache_write_tokens"],
        "output": sums["output_tokens"],
        "reasoning": sums["reasoning_tokens"],
        "non_cached_input": non_cached,
        "net_usage": non_cached + sums["output_tokens"],
        "cache_hit_rate": (sums["cached_input_tokens"] / reported_input) if reported_input else None,
        "sessions": len({(row["agent"], row["session_id"]) for row in session_rows}),
        "session_count_complete": not account_rows,
        "account_days": len({value for value in account_days if value}),
        "calls": sum(max(int(row.get("call_count") or 0), 0) for row in materialized),
    }


def _rows_with_local_date(
    rows: Iterable[dict[str, Any]], tz: Any, start_date: date | None = None, end_date: date | None = None
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        try:
            local_date = _parse_timestamp(row["occurred_at"]).astimezone(tz).date()
        except (TypeError, ValueError):
            continue
        if start_date is not None and local_date < start_date:
            continue
        if end_date is not None and local_date > end_date:
            continue
        copied = dict(row)
        copied["local_date"] = local_date
        output.append(copied)
    return output


def _reconcile_account_rollups(
    rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Prefer account-day totals over overlapping session events for the same agent and date."""
    materialized = list(rows)
    coverage = {
        (row["agent"], row["local_date"])
        for row in materialized
        if row.get("usage_scope") == "account_daily"
    }
    kept: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for row in materialized:
        if row.get("usage_scope") != "account_daily" and (
            row["agent"], row["local_date"]
        ) in coverage:
            suppressed.append(row)
        else:
            kept.append(row)

    diagnostics: dict[str, dict[str, Any]] = {}
    agents = {row["agent"] for row in materialized}
    for agent in agents:
        account_rows = [
            row
            for row in kept
            if row["agent"] == agent and row.get("usage_scope") == "account_daily"
        ]
        removed = [row for row in suppressed if row["agent"] == agent]
        diagnostics[agent] = {
            "has_account_rollup": bool(account_rows),
            "account_days": len({row["local_date"] for row in account_rows}),
            "account_rows": len(account_rows),
            "account_calls": sum(max(int(row.get("call_count") or 0), 0) for row in account_rows),
            "suppressed_session_events": len(removed),
            "suppressed_session_tokens": sum(int(row.get("total_tokens") or 0) for row in removed),
            "policy": "同日存在 CC Switch 账户汇总时，账户汇总优先；会话日志仅补足无汇总日期",
        }
    return kept, diagnostics


def _quota_for_agent(raw: list[dict[str, Any]], now: datetime) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not raw:
        return None, []
    windows: list[dict[str, Any]] = []
    for item in raw:
        output = {
            "status": item["status"],
            "label": item["label"],
            "remaining_percent": item["remaining_percent"],
            "used_percent": item["used_percent"],
            "window_minutes": item["window_minutes"],
            "resets_at": item["resets_at"],
            "updated_at": item["updated_at"],
            "message": item["message"],
        }
        try:
            age = now - _parse_timestamp(item["updated_at"])
            if age > timedelta(hours=24):
                output["status"] = "stale"
                output["message"] = "超过 24 小时未获得新的服务端额度窗口"
        except (TypeError, ValueError):
            output["status"] = "stale"
        windows.append(output)
    windows.sort(key=lambda item: (0 if item["window_minutes"] == 300 else 1, item["window_minutes"] or 999999))
    return windows[0], windows


def build_dashboard(
    database: TokenDatabase,
    scan_status: dict[str, Any],
    timezone_name: str,
    days: int | None,
    selected_agent: str | None,
) -> dict[str, Any]:
    tz = _resolve_timezone(timezone_name)
    now = datetime.now(timezone.utc)
    local_today = now.astimezone(tz).date()
    raw_lifetime_rows = database.usage_rows(agent=selected_agent)
    dated_lifetime_rows = _rows_with_local_date(raw_lifetime_rows, tz, end_date=local_today)
    lifetime_rows, lifetime_reconciliation = _reconcile_account_rollups(dated_lifetime_rows)
    if days is None:
        if lifetime_rows:
            start_date = min(row["local_date"] for row in lifetime_rows)
        else:
            start_date = local_today - timedelta(days=29)
        rows_with_date = lifetime_rows
        range_reconciliation = lifetime_reconciliation
        range_days = max((local_today - start_date).days + 1, 1)
    else:
        range_days = max(days, 1)
        start_date = local_today - timedelta(days=range_days - 1)
        start_local = datetime.combine(start_date, time.min, tzinfo=tz)
        start_utc = start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        raw_rows = database.usage_rows(start_at=start_utc, agent=selected_agent)
        dated_rows = _rows_with_local_date(raw_rows, tz, start_date, local_today)
        rows_with_date, range_reconciliation = _reconcile_account_rollups(dated_rows)

    daily_buckets: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in rows_with_date:
        daily_buckets[row["local_date"]].append(row)
    daily: list[dict[str, Any]] = []
    cursor = start_date
    while cursor <= local_today:
        metric = _metrics(daily_buckets.get(cursor, []))
        daily.append(
            {
                "date": cursor.isoformat(),
                "total": metric["total"],
                "input": metric["input"],
                "cached_input": metric["cached_input"],
                "output": metric["output"],
                "estimated_total": metric["estimated_total"],
            }
        )
        cursor += timedelta(days=1)

    quotas = database.latest_quotas()
    states = {row["agent"]: row for row in database.provider_states()}
    agents: list[dict[str, Any]] = []
    for provider in REGISTRY:
        bucket = [row for row in rows_with_date if row["agent"] == provider.id]
        metric = _metrics(bucket)
        state = states.get(provider.id, {})
        provider_metadata = state.get("metadata", {})
        if provider.id == "antigravity" and not bucket:
            metric["sessions"] = int(state.get("session_count") or 0)
        quota, quota_windows = _quota_for_agent(quotas.get(provider.id, []), now)
        if not quota and provider_metadata.get("budget_windows"):
            quota_windows = list(provider_metadata["budget_windows"])
            quota = quota_windows[0]
        if not quota and provider.id != "codex":
            quota = {
                "status": "unavailable",
                "label": "官方额度未提供",
                "remaining_percent": None,
                "resets_at": None,
                "updated_at": state.get("last_scan"),
                "message": provider_metadata.get("quota_note") or "本地日志可统计 Token，但没有可靠的服务端额度来源",
            }
        agents.append(
            {
                "id": provider.id,
                "name": provider.name,
                "short": provider.short,
                "color": provider.color,
                "status": state.get("status", "pending"),
                **metric,
                "models": len({row["model"] for row in bucket}),
                "usage_available": provider_metadata.get("usage_available", True),
                "model_available": provider_metadata.get("model_available", True),
                "activity_count": int(provider_metadata.get("activity_count") or 0),
                "quota": quota,
                "quota_windows": quota_windows,
                "message": state.get("message", "等待首次扫描"),
                "metadata": provider_metadata,
                "reconciliation": range_reconciliation.get(provider.id, {}),
            }
        )

    model_buckets: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows_with_date:
        model_buckets[(row["agent"], row["route"], row["platform"], row["model"])].append(row)
    total_all = sum(row["total_tokens"] for row in rows_with_date)
    models = []
    for (agent, route, platform, model), bucket in model_buckets.items():
        total = sum(row["total_tokens"] for row in bucket)
        models.append(
            {
                "agent": agent,
                "route": route,
                "platform": platform,
                "model": model,
                "total": total,
                "share": (total / total_all) if total_all else 0,
                "usage_mode": "estimated" if all(row.get("usage_mode") == "estimated" for row in bucket) else "reported",
                "usage_scope": "account_daily"
                if all(row.get("usage_scope") == "account_daily" for row in bucket)
                else "session",
            }
        )
    models.sort(key=lambda item: item["total"], reverse=True)

    sources = [
        {
            "agent": provider.id,
            "status": states.get(provider.id, {}).get("status", "pending"),
            "label": states.get(provider.id, {}).get("label", provider.name),
            "path_hint": states.get(provider.id, {}).get("path_hint", "等待发现"),
            "files": states.get(provider.id, {}).get("file_count", 0),
            "events": states.get(provider.id, {}).get("event_count", 0),
            "sessions": states.get(provider.id, {}).get("session_count", 0),
            "last_scan": states.get(provider.id, {}).get("last_scan"),
            "message": states.get(provider.id, {}).get("message", "等待首次扫描"),
            "metadata": states.get(provider.id, {}).get("metadata", {}),
        }
        for provider in REGISTRY
    ]

    return {
        "meta": {
            "generated_at": now.isoformat().replace("+00:00", "Z"),
            "range": {"start": start_date.isoformat(), "end": local_today.isoformat(), "days": range_days},
            "scan": scan_status,
            "timezone": timezone_name,
            "selected_agent": selected_agent or "all",
            "reconciliation": range_reconciliation,
        },
        "summary": _metrics(rows_with_date),
        "lifetime": {
            "summary": _metrics(lifetime_rows),
            "reconciliation": lifetime_reconciliation,
            "agents": {
                provider.id: _metrics(row for row in lifetime_rows if row["agent"] == provider.id)
                for provider in REGISTRY
            },
        },
        "daily": daily,
        "agents": agents,
        "models": models[:30],
        "sources": sources,
    }
