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


USD_TO_CNY = 7.20

PRICING_CATALOG: dict[str, dict[str, Any]] = {
    # OpenAI / Codex Frontier Family
    "gpt-5.6-sol": {"input": 2.50, "cache": 1.25, "output": 10.00, "currency": "USD", "source": "OpenAI 官方定价 (Sol 旗舰)"},
    "gpt-5.6-luna": {"input": 1.25, "cache": 0.3125, "output": 5.00, "currency": "USD", "source": "OpenAI 官方定价 (Luna 敏捷)"},
    "codex-auto-review": {"input": 2.00, "cache": 0.50, "output": 8.00, "currency": "USD", "source": "OpenAI 官方定价 (代码审查)"},
    "gpt-5.6-terra": {"input": 0.60, "cache": 0.15, "output": 2.40, "currency": "USD", "source": "OpenAI 官方定价 (Terra 高速)"},
    "gpt-6-astra": {"input": 3.00, "cache": 1.50, "output": 12.00, "currency": "USD", "source": "OpenAI 官方定价 (Astra)"},
    "gpt-5.5": {"input": 2.50, "cache": 1.25, "output": 10.00, "currency": "USD", "source": "OpenAI 官方定价 (GPT-5)"},
    "gpt-5": {"input": 2.50, "cache": 1.25, "output": 10.00, "currency": "USD", "source": "OpenAI 官方定价 (GPT-5)"},
    "gpt-6": {"input": 3.00, "cache": 1.50, "output": 12.00, "currency": "USD", "source": "OpenAI 官方定价 (GPT-6)"},
    "gpt-4o-mini": {"input": 0.15, "cache": 0.075, "output": 0.60, "currency": "USD", "source": "OpenAI 官方定价"},
    "gpt-4o": {"input": 2.50, "cache": 1.25, "output": 10.00, "currency": "USD", "source": "OpenAI 官方定价"},
    "gpt-4-turbo": {"input": 10.00, "cache": 5.00, "output": 30.00, "currency": "USD", "source": "OpenAI 官方定价"},
    "gpt-4": {"input": 30.00, "cache": 15.00, "output": 60.00, "currency": "USD", "source": "OpenAI 官方定价"},
    "o1-mini": {"input": 3.00, "cache": 1.50, "output": 12.00, "currency": "USD", "source": "OpenAI 官方定价"},
    "o1-preview": {"input": 15.00, "cache": 7.50, "output": 60.00, "currency": "USD", "source": "OpenAI 官方定价"},
    "o1": {"input": 15.00, "cache": 7.50, "output": 60.00, "currency": "USD", "source": "OpenAI 官方定价"},
    "o3-mini": {"input": 1.10, "cache": 0.55, "output": 4.40, "currency": "USD", "source": "OpenAI 官方定价"},
    "chatgpt-4o-latest": {"input": 5.00, "cache": 2.50, "output": 15.00, "currency": "USD", "source": "OpenAI 官方定价"},

    # DeepSeek Family
    "deepseek-reasoner": {"input": 4.00, "cache": 1.00, "output": 16.00, "currency": "CNY", "source": "DeepSeek 官方定价 (R1)"},
    "deepseek-chat": {"input": 1.00, "cache": 0.10, "output": 2.00, "currency": "CNY", "source": "DeepSeek 官方定价 (V3)"},
    "deepseek-v4-pro": {"input": 2.00, "cache": 0.50, "output": 8.00, "currency": "CNY", "source": "DeepSeek 官方定价 (V4 Pro)"},
    "deepseek-v4-flash": {"input": 0.50, "cache": 0.10, "output": 1.00, "currency": "CNY", "source": "DeepSeek 官方定价 (V4 Flash)"},
    "deepseek-v4-flash-vision-exp": {"input": 0.50, "cache": 0.10, "output": 1.00, "currency": "CNY", "source": "DeepSeek 官方定价"},
    "deepseek-coder": {"input": 1.00, "cache": 0.10, "output": 2.00, "currency": "CNY", "source": "DeepSeek 官方定价"},

    # Claude Family (Anthropic)
    "claude-3-7-sonnet": {"input": 3.00, "cache": 0.30, "output": 15.00, "currency": "USD", "source": "Anthropic 官方定价"},
    "claude-3-5-sonnet": {"input": 3.00, "cache": 0.30, "output": 15.00, "currency": "USD", "source": "Anthropic 官方定价"},
    "claude-sonnet-4-6": {"input": 3.00, "cache": 0.30, "output": 15.00, "currency": "USD", "source": "Anthropic 官方定价"},
    "claude-sonnet-5": {"input": 3.00, "cache": 0.30, "output": 15.00, "currency": "USD", "source": "Anthropic 官方定价"},
    "claude-3-5-haiku": {"input": 0.80, "cache": 0.08, "output": 4.00, "currency": "USD", "source": "Anthropic 官方定价"},
    "claude-haiku-4-5": {"input": 0.80, "cache": 0.08, "output": 4.00, "currency": "USD", "source": "Anthropic 官方定价"},
    "claude-3-haiku": {"input": 0.25, "cache": 0.025, "output": 1.25, "currency": "USD", "source": "Anthropic 官方定价"},
    "claude-3-opus": {"input": 15.00, "cache": 1.50, "output": 75.00, "currency": "USD", "source": "Anthropic 官方定价"},
    "claude-opus-5": {"input": 15.00, "cache": 1.50, "output": 75.00, "currency": "USD", "source": "Anthropic 官方定价"},

    # Gemini Family (Google DeepMind)
    "gemini-3.8-flash": {"input": 0.10, "cache": 0.025, "output": 0.40, "currency": "USD", "source": "Google 官方标准价"},
    "gemini-3.7-flash": {"input": 0.10, "cache": 0.025, "output": 0.40, "currency": "USD", "source": "Google 官方标准价"},
    "gemini-3.7-flash-exp-b": {"input": 0.10, "cache": 0.025, "output": 0.40, "currency": "USD", "source": "Google 官方标准价"},
    "gemini-3.1-pro": {"input": 1.25, "cache": 0.31, "output": 5.00, "currency": "USD", "source": "Google 官方标准价"},
    "gemini-2.5-pro": {"input": 1.25, "cache": 0.31, "output": 5.00, "currency": "USD", "source": "Google 官方标准价"},
    "gemini-2.0-flash": {"input": 0.10, "cache": 0.025, "output": 0.40, "currency": "USD", "source": "Google 官方标准价"},
    "gemini-2.0-pro": {"input": 1.25, "cache": 0.31, "output": 5.00, "currency": "USD", "source": "Google 官方标准价"},
    "gemini-1.5-pro": {"input": 1.25, "cache": 0.31, "output": 5.00, "currency": "USD", "source": "Google 官方标准价"},
    "gemini-1.5-flash": {"input": 0.075, "cache": 0.018, "output": 0.30, "currency": "USD", "source": "Google 官方标准价"},

    # Domestic LLMs
    "glm-5.3-flash": {"input": 0.10, "cache": 0.05, "output": 0.10, "currency": "CNY", "source": "智谱开放平台 (GLM Flash)"},
    "glm-5.2": {"input": 1.00, "cache": 0.20, "output": 1.00, "currency": "CNY", "source": "智谱开放平台"},
    "glm-4.7": {"input": 1.00, "cache": 0.20, "output": 1.00, "currency": "CNY", "source": "智谱开放平台"},
    "glm-4.5-air": {"input": 0.50, "cache": 0.10, "output": 0.50, "currency": "CNY", "source": "智谱开放平台"},
    "glm-4-plus": {"input": 10.00, "cache": 5.00, "output": 10.00, "currency": "CNY", "source": "智谱开放平台"},
    "glm-4-flash": {"input": 0.10, "cache": 0.05, "output": 0.10, "currency": "CNY", "source": "智谱开放平台"},
    "qwen-max": {"input": 16.00, "cache": 4.00, "output": 40.00, "currency": "CNY", "source": "阿里云百炼 (Qwen Max)"},
    "qwen3.7-plus": {"input": 0.80, "cache": 0.20, "output": 2.00, "currency": "CNY", "source": "阿里云百炼 (Qwen Plus)"},
    "qwen-plus": {"input": 0.80, "cache": 0.20, "output": 2.00, "currency": "CNY", "source": "阿里云百炼 (Qwen Plus)"},
    "qwen-turbo": {"input": 0.30, "cache": 0.10, "output": 0.60, "currency": "CNY", "source": "阿里云百炼 (Qwen Turbo)"},
    "qwen3.5-flash": {"input": 0.10, "cache": 0.05, "output": 0.20, "currency": "CNY", "source": "阿里云百炼 (Qwen Flash)"},
    "moonshot-v1-8k": {"input": 12.00, "cache": 3.00, "output": 12.00, "currency": "CNY", "source": "Moonshot 开放平台"},
    "doubao-pro": {"input": 0.80, "cache": 0.16, "output": 2.00, "currency": "CNY", "source": "火山引擎 (豆包 Pro)"},
    "doubao-lite": {"input": 0.30, "cache": 0.06, "output": 0.60, "currency": "CNY", "source": "火山引擎 (豆包 Lite)"},
    "minimax-abab6.5s": {"input": 1.00, "cache": 0.20, "output": 1.00, "currency": "CNY", "source": "MiniMax 开放平台"},
}


def estimate_token_cost(
    model_name: str,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    agent: str = "",
) -> dict[str, Any]:
    """Estimate token cost and commercial equivalent value based on official pricing."""
    target_key = None
    m_lower = (model_name or "").lower().strip()

    # Exact or prefix match against catalog (sorted by key length descending to prioritize more specific keys)
    for key in sorted(PRICING_CATALOG.keys(), key=len, reverse=True):
        if key == m_lower or m_lower.startswith(key):
            target_key = key
            break

    if not target_key:
        if "o1-mini" in m_lower:
            target_key = "o1-mini"
        elif "o1" in m_lower:
            target_key = "o1"
        elif "o3" in m_lower:
            target_key = "o3-mini"
        elif "gpt-4o-mini" in m_lower:
            target_key = "gpt-4o-mini"
        elif "gpt-4-turbo" in m_lower:
            target_key = "gpt-4-turbo"
        elif "gpt-4" in m_lower:
            target_key = "gpt-4o"
        elif "sol" in m_lower or "gpt-5" in m_lower or "gpt-6" in m_lower:
            target_key = "gpt-5.6-sol"
        elif "luna" in m_lower:
            target_key = "gpt-5.6-luna"
        elif "terra" in m_lower:
            target_key = "gpt-5.6-terra"
        elif "codex" in m_lower:
            target_key = "codex-auto-review"
        elif "deepseek" in m_lower:
            if "r1" in m_lower or "reason" in m_lower:
                target_key = "deepseek-reasoner"
            elif "pro" in m_lower:
                target_key = "deepseek-v4-pro"
            elif "flash" in m_lower:
                target_key = "deepseek-v4-flash"
            else:
                target_key = "deepseek-chat"
        elif "claude" in m_lower or "anthropic" in m_lower:
            if "opus" in m_lower:
                target_key = "claude-3-opus"
            elif "haiku" in m_lower:
                target_key = "claude-3-5-haiku"
            else:
                target_key = "claude-3-7-sonnet"
        elif "sonnet" in m_lower:
            target_key = "claude-3-7-sonnet"
        elif "haiku" in m_lower:
            target_key = "claude-3-5-haiku"
        elif "opus" in m_lower:
            target_key = "claude-3-opus"
        elif "gemini" in m_lower:
            target_key = "gemini-3.1-pro" if "pro" in m_lower else "gemini-3.8-flash"
        elif "glm" in m_lower or "chatglm" in m_lower:
            if "flash" in m_lower:
                target_key = "glm-5.3-flash"
            elif "air" in m_lower:
                target_key = "glm-4.5-air"
            else:
                target_key = "glm-4-plus"
        elif "qwen" in m_lower or "tongyi" in m_lower:
            if "max" in m_lower:
                target_key = "qwen-max"
            elif "plus" in m_lower:
                target_key = "qwen3.7-plus"
            elif "flash" in m_lower:
                target_key = "qwen3.5-flash"
            else:
                target_key = "qwen-turbo"
        elif "moonshot" in m_lower or "kimi" in m_lower:
            target_key = "moonshot-v1-8k"
        elif "doubao" in m_lower:
            target_key = "doubao-lite" if "lite" in m_lower else "doubao-pro"
        elif "minimax" in m_lower or "abab" in m_lower:
            target_key = "minimax-abab6.5s"
        elif "flash" in m_lower:
            target_key = "gemini-3.7-flash"
        elif "pro" in m_lower:
            target_key = "gemini-3.1-pro"
        elif agent == "codex":
            target_key = "gpt-5.6-sol"
        elif agent == "claude":
            target_key = "deepseek-v4-pro"
        elif agent == "antigravity":
            target_key = "gemini-3.8-flash"
        else:
            target_key = "gemini-3.8-flash"

    rule = PRICING_CATALOG.get(target_key, PRICING_CATALOG["gemini-3.8-flash"])
    currency = rule["currency"]
    inp_rate = rule["input"]
    cache_rate = rule["cache"]
    out_rate = rule["output"]

    uncached_inp = max(input_tokens - cached_input_tokens, 0)
    # Token rates are per 1M (1,000,000) tokens
    raw_cost = (
        (uncached_inp / 1_000_000.0) * inp_rate
        + (cached_input_tokens / 1_000_000.0) * cache_rate
        + (output_tokens / 1_000_000.0) * out_rate
    )

    if currency == "USD":
        cost_usd = raw_cost
        cost_cny = raw_cost * USD_TO_CNY
    else:
        cost_cny = raw_cost
        cost_usd = raw_cost / USD_TO_CNY

    cost_cny_text = f"¥{cost_cny:,.2f}" if cost_cny >= 0.01 else f"¥{cost_cny:,.4f}"
    cost_usd_text = f"${cost_usd:,.2f}" if cost_usd >= 0.01 else f"${cost_usd:,.4f}"

    return {
        "cost_cny": round(cost_cny, 4),
        "cost_usd": round(cost_usd, 4),
        "cost_cny_text": cost_cny_text,
        "cost_usd_text": cost_usd_text,
        "pricing_source": rule["source"],
        "pricing_model_matched": target_key,
    }


def _calculate_total_cost(rows: Sequence[dict[str, Any]]) -> tuple[float, float, str, str]:
    """Calculate commercial equivalent cost for a collection of usage rows."""
    cny = 0.0
    usd = 0.0
    buckets: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        m = r.get("model") or ""
        a = r.get("agent") or ""
        inp = r.get("input_tokens") or 0
        cinp = r.get("cached_input_tokens") or 0
        out = r.get("output_tokens") or 0
        b = buckets[(a, m)]
        b[0] += inp
        b[1] += cinp
        b[2] += out
    for (a, m), (inp, cinp, out) in buckets.items():
        ci = estimate_token_cost(m, inp, cinp, out, agent=a)
        cny += ci["cost_cny"]
        usd += ci["cost_usd"]
    cny_text = f"¥{cny:,.2f}" if cny >= 0.01 else f"¥{cny:,.4f}"
    usd_text = f"${usd:,.2f}" if usd >= 0.01 else f"${usd:,.4f}"
    return round(cny, 2), round(usd, 2), cny_text, usd_text


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
        lt_summary = _metrics(lifetime_rows)
        lt_cny, lt_usd, lt_cny_t, lt_usd_t = _calculate_total_cost(lifetime_rows)
        lt_summary["estimated_cost_cny"] = lt_cny
        lt_summary["estimated_cost_usd"] = lt_usd
        lt_summary["cost_cny_text"] = lt_cny_t
        lt_summary["cost_usd_text"] = lt_usd_t
        _CACHE.lifetime_summary = lt_summary

        lifetime_agents = {}
        for provider in REGISTRY:
            p_rows = [row for row in lifetime_rows if row["agent"] == provider.id]
            p_metric = _metrics(p_rows)
            p_cny, p_usd, p_cny_t, p_usd_t = _calculate_total_cost(p_rows)
            p_metric["estimated_cost_cny"] = p_cny
            p_metric["estimated_cost_usd"] = p_usd
            p_metric["cost_cny_text"] = p_cny_t
            p_metric["cost_usd_text"] = p_usd_t
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
        lt_cny, lt_usd, lt_cny_t, lt_usd_t = _calculate_total_cost(scoped_lifetime)
        lifetime_summary["estimated_cost_cny"] = lt_cny
        lifetime_summary["estimated_cost_usd"] = lt_usd
        lifetime_summary["cost_cny_text"] = lt_cny_t
        lifetime_summary["cost_usd_text"] = lt_usd_t
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
        p_cny, p_usd, p_cny_t, p_usd_t = _calculate_total_cost(bucket)
        lt_agent = _CACHE.lifetime_agents.get(provider.id, {})
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
                "estimated_cost_cny": p_cny,
                "estimated_cost_usd": p_usd,
                "cost_cny_text": p_cny_t,
                "cost_usd_text": p_usd_t,
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
            }
        )

    total_all = sum(row["total_tokens"] for row in rows_with_date)
    models = []
    total_cost_cny = 0.0
    total_cost_usd = 0.0

    for (agent, route, platform, model), bucket in model_buckets.items():
        total = sum(row["total_tokens"] for row in bucket)
        inp = sum(row.get("input_tokens") or 0 for row in bucket)
        cinp = sum(row.get("cached_input_tokens") or 0 for row in bucket)
        out = sum(row.get("output_tokens") or 0 for row in bucket)
        cost_info = estimate_token_cost(model, inp, cinp, out, agent=agent)
        total_cost_cny += cost_info["cost_cny"]
        total_cost_usd += cost_info["cost_usd"]

        models.append(
            {
                "agent": agent,
                "route": route,
                "platform": platform,
                "model": model,
                "total": total,
                "input": inp,
                "cached_input": cinp,
                "output": out,
                "share": (total / total_all) if total_all else 0,
                "usage_mode": "estimated" if all(row.get("usage_mode") == "estimated" for row in bucket) else "reported",
                "usage_scope": "account_daily"
                if all(row.get("usage_scope") == "account_daily" for row in bucket)
                else "session",
                "cost_cny": cost_info["cost_cny"],
                "cost_usd": cost_info["cost_usd"],
                "cost_cny_text": cost_info["cost_cny_text"],
                "cost_usd_text": cost_info["cost_usd_text"],
                "pricing_source": cost_info["pricing_source"],
                "pricing_model": cost_info["pricing_model_matched"],
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
    summary_metric["estimated_cost_cny"] = round(total_cost_cny, 2)
    summary_metric["estimated_cost_usd"] = round(total_cost_usd, 2)
    summary_metric["cost_cny_text"] = f"¥{total_cost_cny:,.2f}"
    summary_metric["cost_usd_text"] = f"${total_cost_usd:,.2f}"

    payload = {
        "meta": {
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
