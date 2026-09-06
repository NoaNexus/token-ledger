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
    total = input_tokens = cached_input = cache_write = output_tokens = reasoning_tokens = 0
    reported_input = estimated_total = calls = 0
    sessions: set[tuple[str, str]] = set()
    account_days: set[Any] = set()
    has_account_rows = False

    for row in rows:
        inp = int(row.get("input_tokens") or 0)
        cinp = int(row.get("cached_input_tokens") or 0)
        cwrite = int(row.get("cache_write_tokens") or 0)
        out = int(row.get("output_tokens") or 0)
        reasoning = int(row.get("reasoning_tokens") or 0)
        tot = int(row.get("total_tokens") or 0)
        mode = row.get("usage_mode")
        scope = row.get("usage_scope")

        total += tot
        input_tokens += inp
        cached_input += cinp
        cache_write += cwrite
        output_tokens += out
        reasoning_tokens += reasoning
        calls += max(int(row.get("call_count") or 0), 0)

        if mode == "estimated":
            estimated_total += tot
        else:
            reported_input += inp

        if scope == "account_daily":
            has_account_rows = True
            account_days.add(row.get("local_date") or str(row.get("occurred_at") or "")[:10])
        else:
            sessions.add((row["agent"], row["session_id"]))

    non_cached = max(input_tokens - cached_input, 0)
    return {
        "total": total,
        "reported_total": total - estimated_total,
        "estimated_total": estimated_total,
        "contains_estimates": estimated_total > 0,
        "input": input_tokens,
        "cached_input": cached_input,
        "cache_write": cache_write,
        "output": output_tokens,
        "reasoning": reasoning_tokens,
        "non_cached_input": non_cached,
        "net_usage": non_cached + output_tokens,
        "cache_hit_rate": (cached_input / reported_input) if reported_input else None,
        "sessions": len(sessions),
        "session_count_complete": not has_account_rows,
        "account_days": len({v for v in account_days if v}),
        "calls": calls,
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
    """Reconcile session-level events and account-day rollups intelligently.

    For any (agent, date) where both session events and account rollups exist:
    - If session total >= account rollup total, keep detailed session events (richer and higher volume);
    - If account rollup total > session total, keep account rollup (capturing outside/desktop API activity).
    """
    materialized = list(rows)
    grouped: dict[tuple[str, Any], list[dict[str, Any]]] = defaultdict(list)
    for row in materialized:
        grouped[(row["agent"], row["local_date"])].append(row)

    kept: list[dict[str, Any]] = []
    suppressed_session: list[dict[str, Any]] = []
    suppressed_account: list[dict[str, Any]] = []

    for (agent, local_date), group_rows in grouped.items():
        session_items = [r for r in group_rows if r.get("usage_scope") != "account_daily"]
        account_items = [r for r in group_rows if r.get("usage_scope") == "account_daily"]

        if session_items and account_items:
            s_tokens = sum(int(r.get("total_tokens") or 0) for r in session_items)
            a_tokens = sum(int(r.get("total_tokens") or 0) for r in account_items)
            if s_tokens >= a_tokens:
                kept.extend(session_items)
                suppressed_account.extend(account_items)
            else:
                kept.extend(account_items)
                suppressed_session.extend(session_items)
        elif session_items:
            kept.extend(session_items)
        elif account_items:
            kept.extend(account_items)

    diagnostics: dict[str, dict[str, Any]] = {}
    agents = {row["agent"] for row in materialized}
    for agent in agents:
        account_rows = [
            row
            for row in kept
            if row["agent"] == agent and row.get("usage_scope") == "account_daily"
        ]
        removed = [row for row in suppressed_session if row["agent"] == agent]
        diagnostics[agent] = {
            "has_account_rollup": bool(account_rows),
            "account_days": len({row["local_date"] for row in account_rows}),
            "account_rows": len(account_rows),
            "account_calls": sum(max(int(row.get("call_count") or 0), 0) for row in account_rows),
            "suppressed_session_events": len(removed),
            "suppressed_session_tokens": sum(int(row.get("total_tokens") or 0) for row in removed),
            "policy": "同日同时存在会话明细与账户汇总时，智能优选覆盖度更完整的数据源",
        }
    return kept, diagnostics


def _quota_for_agent(raw: list[dict[str, Any]], now: datetime) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not raw:
        return None, []
    windows: list[dict[str, Any]] = []
    for item in raw:
        output = {
            "snapshot_id": item.get("snapshot_id"),
            "status": item["status"],
            "label": item["label"],
            "remaining_percent": item["remaining_percent"],
            "used_percent": item["used_percent"],
            "window_minutes": item["window_minutes"],
            "resets_at": item["resets_at"],
            "updated_at": item["updated_at"],
            "message": item["message"],
        }
        for extra_key in ("balance_text", "is_current", "website_url", "provider_name"):
            if extra_key in item:
                output[extra_key] = item[extra_key]
        resets_at = item.get("resets_at")
        if resets_at:
            try:
                reset_time = _parse_timestamp(resets_at)
                if now >= reset_time:
                    output["status"] = "fresh"
                    output["remaining_percent"] = 100.0
                    output["used_percent"] = 0.0
                    output["message"] = "额度重置周期已届满，已自动重置为 100%"
            except (TypeError, ValueError):
                pass
        try:
            age = now - _parse_timestamp(item["updated_at"])
            if age > timedelta(hours=24) and output["status"] != "fresh":
                output["status"] = "stale"
                output["message"] = "超过 24 小时未获得新的服务端额度窗口"
        except (TypeError, ValueError):
            output["status"] = "stale"
        windows.append(output)

    def _quota_sort_key(item: dict[str, Any]) -> tuple[int, int, int]:
        label = item.get("label", "")
        if "(当前" in label or item.get("is_current"):
            return (0, 0, 0)
        family = 0 if "Gemini" in label else (1 if "Claude" in label or "GPT" in label else 2)
        win = 0 if item.get("window_minutes") == 10080 else (1 if item.get("window_minutes") == 300 else 2)
        return (family, win, item.get("window_minutes") or 999999)

    windows.sort(key=_quota_sort_key)
    return windows[0], windows


class _DashboardSnapshotCache:
    def __init__(self) -> None:
        self.key: Any = None
        self.local_today: date | None = None
        self.lifetime_rows: list[dict[str, Any]] = []
        self.lifetime_reconciliation: dict[str, dict[str, Any]] = {}
        self.lifetime_summary: dict[str, Any] = {}
        self.lifetime_agents: dict[str, dict[str, Any]] = {}
        self.payload_cache: dict[tuple[int | None, str | None], dict[str, Any]] = {}

    def clear(self) -> None:
        self.key = None
        self.local_today = None
        self.lifetime_rows = []
        self.lifetime_reconciliation = {}
        self.lifetime_summary = {}
        self.lifetime_agents = {}
        self.payload_cache.clear()


_CACHE = _DashboardSnapshotCache()


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

    db_path = str(getattr(database, "path", ""))
    internal_version = getattr(database, "_internal_version", 0)
    data_ver_fn = getattr(database, "data_version", None)
    db_data_version = data_ver_fn() if callable(data_ver_fn) else 0
    cache_key = (db_path, internal_version, db_data_version, timezone_name)

    if _CACHE.key != cache_key or _CACHE.local_today != local_today:
        _CACHE.clear()
        raw_rows = database.usage_rows()
        dated_rows = _rows_with_local_date(raw_rows, tz, end_date=local_today)
        lifetime_rows, lifetime_reconciliation = _reconcile_account_rollups(dated_rows)

        _CACHE.key = cache_key
        _CACHE.local_today = local_today
        _CACHE.lifetime_rows = lifetime_rows
        _CACHE.lifetime_reconciliation = lifetime_reconciliation
        _CACHE.lifetime_summary = _metrics(lifetime_rows)
        _CACHE.lifetime_agents = {
            provider.id: _metrics(row for row in lifetime_rows if row["agent"] == provider.id)
            for provider in REGISTRY
        }

    req_key = (days, selected_agent)
    if req_key in _CACHE.payload_cache:
        cached = _CACHE.payload_cache[req_key]
        return {
            **cached,
            "meta": {
                **cached["meta"],
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "scan": scan_status,
            },
        }

    all_lifetime = _CACHE.lifetime_rows
    if selected_agent:
        scoped_lifetime = [row for row in all_lifetime if row["agent"] == selected_agent]
        lifetime_summary = _metrics(scoped_lifetime)
        lifetime_reconciliation = {
            selected_agent: _CACHE.lifetime_reconciliation.get(selected_agent, {})
        }
    else:
        scoped_lifetime = all_lifetime
        lifetime_summary = _CACHE.lifetime_summary
        lifetime_reconciliation = _CACHE.lifetime_reconciliation

    if days is None:
        if scoped_lifetime:
            start_date = min(row["local_date"] for row in scoped_lifetime)
        else:
            start_date = local_today - timedelta(days=29)
        rows_with_date = scoped_lifetime
        range_days = max((local_today - start_date).days + 1, 1)
    else:
        range_days = max(days, 1)
        start_date = local_today - timedelta(days=range_days - 1)
        rows_with_date = [row for row in scoped_lifetime if row["local_date"] >= start_date]

    range_reconciliation = {
        agent: reco
        for agent, reco in _CACHE.lifetime_reconciliation.items()
        if not selected_agent or agent == selected_agent
    }

    daily_buckets: dict[date, list[dict[str, Any]]] = defaultdict(list)
    agent_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    model_buckets: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows_with_date:
        daily_buckets[row["local_date"]].append(row)
        agent_buckets[row["agent"]].append(row)
        model_buckets[(row["agent"], row["route"], row["platform"], row["model"])].append(row)

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
        bucket = agent_buckets.get(provider.id, [])
        metric = _metrics(bucket)
        state = states.get(provider.id, {})
        provider_metadata = state.get("metadata", {})
        if provider.id == "antigravity" and not bucket:
            metric["sessions"] = int(state.get("session_count") or 0)
        quota, quota_windows = _quota_for_agent(quotas.get(provider.id, []), now)
        if (not quota or provider.id == "claude") and provider_metadata.get("budget_windows"):
            quota_windows = list(provider_metadata["budget_windows"])
            quota = quota_windows[0] if quota_windows else quota
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


    payload = {
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
            "summary": lifetime_summary,
            "reconciliation": lifetime_reconciliation,
            "agents": _CACHE.lifetime_agents,
        },
        "daily": daily,
        "agents": agents,
        "models": models[:30],
        "sources": sources,
    }
    _CACHE.payload_cache[req_key] = payload
    return payload
