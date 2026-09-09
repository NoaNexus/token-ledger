from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from functools import wraps
from threading import RLock
from time import monotonic

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
except ImportError:  # Python 3.8 or an incomplete standard-library install.
    _ZoneInfo = None

from . import __version__
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
    """Choose the larger complete source only for identified matching coverage."""
    materialized = list(rows)
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in materialized:
        platform = str(row.get("platform") or "").strip().casefold()
        model = str(row.get("model") or "").strip().casefold()
        route = str(row.get("route") or "")
        known = bool(platform and model and "未" not in platform and "未" not in model
                     and "cc switch" in route.casefold())
        # Reconcile only matching, identified CC Switch coverage. Keep unrelated
        # platforms and direct connections even when a larger daily rollup exists.
        scope = "ccswitch" if "cc switch" in route.casefold() else route
        identity = (platform, model, scope) if known else (id(row),)
        grouped[(row["agent"], row["local_date"], *identity)].append(row)

    kept: list[dict[str, Any]] = []
    suppressed_session: list[dict[str, Any]] = []
    suppressed_account: list[dict[str, Any]] = []

    for group_rows in grouped.values():
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
        account_dates = {row["local_date"] for row in materialized
                         if row["agent"] == agent and row.get("usage_scope") == "account_daily"}
        uncertain = [row for row in kept if row["agent"] == agent
                     and row.get("usage_scope") != "account_daily"
                     and row["local_date"] in account_dates
                     and (not row.get("platform") or not row.get("model")
                          or "未" in str(row.get("platform")) or "未" in str(row.get("model")))]
        diagnostics[agent] = {
            "has_account_rollup": bool(account_rows),
            "account_days": len({row["local_date"] for row in account_rows}),
            "account_rows": len(account_rows),
            "account_calls": sum(max(int(row.get("call_count") or 0), 0) for row in account_rows),
            "suppressed_session_events": len(removed),
            "suppressed_session_tokens": sum(int(row.get("total_tokens") or 0) for row in removed),
            "uncertain_overlap": bool(uncertain),
            "uncertain_overlap_events": len(uncertain),
            "policy": "仅对同日同平台同模型同路由的覆盖取较大值；身份未知记录保留，可能重叠，不能视为精确账单",
        }
    return kept, diagnostics


def _quota_for_agent(raw: list[dict[str, Any]], now: datetime) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not raw:
        return None, []
    windows: list[dict[str, Any]] = []
    for item in raw:
        output = {
            "snapshot_id": item.get("snapshot_id"),
            "status": item.get("status", "unavailable"),
            "label": item.get("label", "官方额度未提供"),
            "remaining_percent": item.get("remaining_percent"),
            "used_percent": item.get("used_percent"),
            "window_minutes": item.get("window_minutes"),
            "resets_at": item.get("resets_at"),
            "updated_at": item.get("updated_at"),
            "message": item.get("message", ""),
        }
        for extra_key in ("balance_text", "is_current", "website_url", "provider_name"):
            if extra_key in item:
                output[extra_key] = item[extra_key]
        resets_at = item.get("resets_at")
        if resets_at:
            try:
                reset_time = _parse_timestamp(resets_at)
                if now >= reset_time:
                    output["status"] = "stale"
                    output["remaining_percent"] = None
                    output["used_percent"] = None
                    output["message"] = "旧额度窗口已过期，等待新的服务端快照"
            except (TypeError, ValueError):
                pass
        try:
            age = now - _parse_timestamp(item.get("updated_at") or "")
            if age > timedelta(hours=24):
                output["status"] = "stale"
                output["message"] = "超过 24 小时未获得新的服务端额度窗口"
        except (TypeError, ValueError):
            if output["status"] != "unavailable":
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


# Re-export for callers that used the original analytics pricing helpers.
from .pricing import PRICING_CATALOG, USD_TO_CNY, estimate_token_cost, cost_summary


def _calculate_total_cost(rows: Iterable[dict[str, Any]]) -> tuple[float, float, str, str]:
    value = cost_summary(rows)
    return (value["estimated_cost_cny"], value["estimated_cost_usd"],
            value["cost_cny_text"], value["cost_usd_text"])


_CACHE = _DashboardSnapshotCache()
_CACHE_LOCK = RLock()


def _serialized(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _CACHE_LOCK:
            return function(*args, **kwargs)
    return wrapped


@_serialized
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
    cache_key = (db_path, internal_version, db_data_version, timezone_name, int(monotonic() // 15))

    if _CACHE.key != cache_key or _CACHE.local_today != local_today:
        _CACHE.clear()
        raw_rows = database.usage_rows()
        dated_rows = _rows_with_local_date(raw_rows, tz, end_date=local_today)
        lifetime_rows, lifetime_reconciliation = _reconcile_account_rollups(dated_rows)

        _CACHE.key = cache_key
        _CACHE.local_today = local_today
        _CACHE.lifetime_rows = lifetime_rows
        _CACHE.lifetime_reconciliation = lifetime_reconciliation
        lt_summary = _metrics(lifetime_rows)
        lt_summary.update(cost_summary(lifetime_rows))
        _CACHE.lifetime_summary = lt_summary

        lifetime_agents = {}
        for provider in REGISTRY:
            p_rows = [row for row in lifetime_rows if row["agent"] == provider.id]
            p_metric = _metrics(p_rows)
            p_metric.update(cost_summary(p_rows))
            lifetime_agents[provider.id] = p_metric
        _CACHE.lifetime_agents = lifetime_agents

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
        lifetime_summary.update(cost_summary(scoped_lifetime))
        lifetime_reconciliation = {
            selected_agent: _CACHE.lifetime_reconciliation.get(selected_agent, {})
        }
    else:
        scoped_lifetime = all_lifetime
        lifetime_summary = dict(_CACHE.lifetime_summary)
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
        day_rows = daily_buckets.get(cursor, [])
        metric = _metrics(day_rows)
        day_cost = cost_summary(day_rows)
        daily.append(
            {
                "date": cursor.isoformat(),
                "total": metric["total"],
                "input": metric["input"],
                "cached_input": metric["cached_input"],
                "output": metric["output"],
                "estimated_total": metric["estimated_total"],
                "cost_cny": day_cost["estimated_cost_cny"],
                "cost_usd": day_cost["estimated_cost_usd"],
                **day_cost,
            }
        )
        cursor += timedelta(days=1)

    quotas = database.latest_quotas()
    states = {row["agent"]: row for row in database.provider_states()}
    agents: list[dict[str, Any]] = []
    for provider in REGISTRY:
        bucket = agent_buckets.get(provider.id, [])
        metric = _metrics(bucket)
        lt_agent = _CACHE.lifetime_agents.get(provider.id, {})
        state = states.get(provider.id, {})
        provider_metadata = state.get("metadata", {})
        if provider.id == "antigravity" and not bucket:
            metric["sessions"] = int(state.get("session_count") or 0)
        quota, quota_windows = _quota_for_agent(quotas.get(provider.id, []), now)
        if (not quota or provider.id == "claude") and provider_metadata.get("budget_windows"):
            quota, quota_windows = _quota_for_agent(list(provider_metadata["budget_windows"]), now)
        if not quota and provider.id != "codex":
            quota = {
                "status": "unavailable",
                "label": "官方额度未提供",
                "remaining_percent": None,
                "resets_at": None,
                "updated_at": state.get("last_scan"),
                "message": provider_metadata.get("quota_note") or "本地日志可统计 Token，但没有可靠的服务端额度来源",
            }
        claude_benchmark = None

        agents.append(
            {
                "id": provider.id,
                "name": provider.name,
                "short": provider.short,
                "color": provider.color,
                "status": state.get("status", "pending"),
                **metric,
                **cost_summary(bucket),
                "lifetime_cost_cny_text": lt_agent.get("cost_cny_text", "¥0.00"),
                "lifetime_cost_usd_text": lt_agent.get("cost_usd_text", "$0.00"),
                "models": len({row["model"] for row in bucket}),
                "usage_available": provider_metadata.get("usage_available", True),
                "model_available": provider_metadata.get("model_available", True),
                "activity_count": int(provider_metadata.get("activity_count") or 0),
                "quota": quota,
                "quota_windows": quota_windows,
                "message": state.get("message", "等待首次扫描"),
                "metadata": provider_metadata,
                "reconciliation": range_reconciliation.get(provider.id, {}),
                "claude_benchmark": claude_benchmark,
            }
        )

    total_all = sum(row["total_tokens"] for row in rows_with_date)
    models = []

    for (agent, route, platform, model), bucket in model_buckets.items():
        total = sum(row["total_tokens"] for row in bucket)
        inp = sum(row.get("input_tokens") or 0 for row in bucket)
        cinp = sum(row.get("cached_input_tokens") or 0 for row in bucket)
        out = sum(row.get("output_tokens") or 0 for row in bucket)
        cwrite = sum(row.get("cache_write_tokens") or 0 for row in bucket)
        cost_info = estimate_token_cost(model, inp, cinp, out, agent=agent, cache_write_tokens=cwrite)

        models.append(
            {
                "agent": agent,
                "route": route,
                "platform": platform,
                "model": model,
                "total": total,
                "input": inp,
                "cached_input": cinp,
                "cache_write": cwrite,
                "output": out,
                "share": (total / total_all) if total_all else 0,
                "usage_mode": "estimated" if all(row.get("usage_mode") == "estimated" for row in bucket) else "reported",
                "usage_scope": "account_daily"
                if all(row.get("usage_scope") == "account_daily" for row in bucket)
                else "session",
                "cost_cny": cost_info["cost_cny"],
                "cost_known": cost_info["cost_known"],
                "pricing_checked_at": cost_info["pricing_checked_at"],
                "cost_usd": cost_info["cost_usd"],
                "cost_cny_text": cost_info["cost_cny_text"],
                "cost_usd_text": cost_info["cost_usd_text"],
                "pricing_source": cost_info["pricing_source"],
                "pricing_model": cost_info["pricing_model_matched"],
                "unit_rate_text": cost_info.get("unit_rate_text", ""),
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

    summary_metric = _metrics(rows_with_date)
    summary_metric.update(cost_summary(rows_with_date))

    payload = {
        "meta": {
            "version": __version__,
            "generated_at": now.isoformat().replace("+00:00", "Z"),
            "range": {"start": start_date.isoformat(), "end": local_today.isoformat(), "days": range_days},
            "scan": scan_status,
            "timezone": timezone_name,
            "selected_agent": selected_agent or "all",
            "reconciliation": range_reconciliation,
        },
        "summary": summary_metric,
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
