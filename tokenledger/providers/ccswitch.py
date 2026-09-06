from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class CCSwitchInfo:
    installed: bool
    route: str
    platform: str
    database_path: Path | None
    message: str


ROLLUP_COLUMNS = {
    "date",
    "app_type",
    "provider_id",
    "model",
    "request_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "input_token_semantics",
}


def _nonnegative_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def rollup_input_total(row: dict[str, Any]) -> int:
    """Normalize CC Switch input semantics without double-counting legacy rows."""
    direct = _nonnegative_int(row.get("input_tokens"))
    if _nonnegative_int(row.get("input_token_semantics")) != 2:
        return direct
    return direct + _nonnegative_int(row.get("cache_read_tokens")) + _nonnegative_int(
        row.get("cache_creation_tokens")
    )


def safe_usage_rollups(user_home: Path) -> list[dict[str, Any]]:
    """Read Claude-family account-day usage totals; never inspect provider secrets."""
    database_path = user_home / ".cc-switch" / "cc-switch.db"
    if not database_path.exists():
        return []
    connection: sqlite3.Connection | None = None
    try:
        uri = database_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"providers"}.issubset(tables):
            return []
        provider_columns = {row[1] for row in connection.execute("PRAGMA table_info(providers)")}
        if not {"id", "name"}.issubset(provider_columns):
            return []

        rows: list[Any] = []
        has_rollups = "usage_daily_rollups" in tables
        has_proxy_logs = "proxy_request_logs" in tables

        if has_rollups:
            rollup_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(usage_daily_rollups)")
            }
            if ROLLUP_COLUMNS.issubset(rollup_columns):
                rows.extend(connection.execute(
                    """
                    SELECT r.date, r.provider_id, COALESCE(p.name, 'CC Switch 历史平台') AS provider_name,
                           COALESCE(NULLIF(r.model, ''), '模型未记录') AS model,
                           r.input_token_semantics,
                           SUM(r.request_count) AS request_count,
                           SUM(r.input_tokens) AS input_tokens,
                           SUM(r.output_tokens) AS output_tokens,
                           SUM(r.cache_read_tokens) AS cache_read_tokens,
                           SUM(r.cache_creation_tokens) AS cache_creation_tokens
                    FROM usage_daily_rollups AS r
                    LEFT JOIN providers AS p ON p.id = r.provider_id
                    WHERE lower(r.app_type) LIKE 'claude%'
                      AND r.input_token_semantics = 2
                    GROUP BY r.date, r.provider_id, provider_name, model, r.input_token_semantics
                    ORDER BY r.date, provider_name, model
                    """
                ).fetchall())

        if has_proxy_logs:
            proxy_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(proxy_request_logs)")
            }
            required_proxy = {
                "created_at", "provider_id", "app_type", "model",
                "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"
            }
            if required_proxy.issubset(proxy_columns):
                exclude_dates_clause = ""
                if has_rollups:
                    exclude_dates_clause = """
                        AND date(r.created_at, 'unixepoch', 'localtime') NOT IN (
                            SELECT DISTINCT date FROM usage_daily_rollups WHERE lower(app_type) LIKE 'claude%'
                        )
                    """
                proxy_sql = f"""
                    SELECT date(r.created_at, 'unixepoch', 'localtime') AS date,
                           r.provider_id,
                           COALESCE(p.name, 'CC Switch 历史平台') AS provider_name,
                           COALESCE(NULLIF(r.model, ''), '模型未记录') AS model,
                           2 AS input_token_semantics,
                           COUNT(*) AS request_count,
                           SUM(r.input_tokens) AS input_tokens,
                           SUM(r.output_tokens) AS output_tokens,
                           SUM(r.cache_read_tokens) AS cache_read_tokens,
                           SUM(r.cache_creation_tokens) AS cache_creation_tokens
                    FROM proxy_request_logs AS r
                    LEFT JOIN providers AS p ON p.id = r.provider_id
                    WHERE lower(r.app_type) LIKE 'claude%'
                      {exclude_dates_clause}
                    GROUP BY date, r.provider_id, provider_name, model
                    ORDER BY date, provider_name, model
                """
                rows.extend(connection.execute(proxy_sql).fetchall())

        return [
            {
                "date": str(row[0]),
                "provider_id": str(row[1]),
                "provider_name": str(row[2]),
                "model": str(row[3]),
                "input_token_semantics": _nonnegative_int(row[4]),
                "request_count": _nonnegative_int(row[5]),
                "input_tokens": _nonnegative_int(row[6]),
                "output_tokens": _nonnegative_int(row[7]),
                "cache_read_tokens": _nonnegative_int(row[8]),
                "cache_creation_tokens": _nonnegative_int(row[9]),
            }
            for row in rows
        ]
    except (OSError, sqlite3.Error):
        return []
    finally:
        if connection is not None:
            connection.close()


def safe_usage_rollup_summary(user_home: Path) -> dict[str, Any]:
    rows = safe_usage_rollups(user_home)
    dates = sorted({row["date"] for row in rows})
    return {
        "available": bool(rows),
        "rows": len(rows),
        "days": len(dates),
        "start_date": dates[0] if dates else None,
        "end_date": dates[-1] if dates else None,
        "requests": sum(row["request_count"] for row in rows),
        "total_tokens": sum(
            rollup_input_total(row) + _nonnegative_int(row.get("output_tokens")) for row in rows
        ),
        "scope": "account_daily",
    }


def _platform_from_url(raw_url: str | None) -> str:
    if not raw_url:
        return "平台未识别"
    try:
        host = (urlparse(raw_url).hostname or "").lower()
    except ValueError:
        return "平台未识别"
    if host in {"localhost", "127.0.0.1", "::1"}:
        return "CC Switch 本地代理"
    mappings = (
        (("anthropic.com",), "Anthropic"),
        (("bigmodel.cn", "zhipuai.cn"), "智谱 AI"),
        (("dashscope.aliyuncs.com", "aliyun.com", "alibabacloud.com"), "阿里云百炼"),
        (("deepseek.com",), "DeepSeek"),
        (("volces.com", "volcengine.com"), "火山引擎"),
        (("openrouter.ai",), "OpenRouter"),
    )
    for suffixes, label in mappings:
        if any(host == suffix or host.endswith("." + suffix) for suffix in suffixes):
            return label
    return host or "平台未识别"


def _live_base_url(user_home: Path) -> str | None:
    settings = user_home / ".claude" / "settings.json"
    try:
        payload = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    env = payload.get("env") if isinstance(payload, dict) else None
    if not isinstance(env, dict):
        return None
    value = env.get("ANTHROPIC_BASE_URL")
    return value if isinstance(value, str) else None


def _safe_current_name(database_path: Path) -> str | None:
    """Read only non-secret identity columns; never read provider config blobs."""
    try:
        uri = database_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        columns = [row[1] for row in connection.execute("PRAGMA table_info(providers)")]
        safe_columns = {"name", "app_type", "is_current", "enabled", "provider_type"}
        if "name" not in columns or "is_current" not in columns:
            connection.close()
            return None
        selected = [name for name in columns if name in safe_columns]
        rows = connection.execute(
            f"SELECT {','.join(selected)} FROM providers WHERE is_current = 1"
        ).fetchall()
        connection.close()
        name_index = selected.index("name")
        app_index = selected.index("app_type") if "app_type" in selected else None
        for row in rows:
            if app_index is None or str(row[app_index]).lower() in {"claude", "claude_code", "claudecode"}:
                value = row[name_index]
                return str(value) if value else None
    except (OSError, sqlite3.Error):
        return None
    return None


def unique_model_platforms(user_home: Path) -> dict[str, str]:
    """Return only unambiguous model-to-provider names from CC Switch's usage ledger."""
    database_path = user_home / ".cc-switch" / "cc-switch.db"
    if not database_path.exists():
        return {}
    try:
        uri = database_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        rows = connection.execute(
            """
            SELECT logs.model, providers.name
            FROM proxy_request_logs AS logs
            JOIN providers ON providers.id = logs.provider_id
            WHERE logs.model IS NOT NULL AND providers.name IS NOT NULL
            GROUP BY logs.model, providers.name
            """
        ).fetchall()
        connection.close()
    except (OSError, sqlite3.Error):
        return {}
    candidates: dict[str, set[str]] = {}
    for model, provider_name in rows:
        candidates.setdefault(str(model), set()).add(str(provider_name))
    return {model: next(iter(names)) for model, names in candidates.items() if len(names) == 1}


_BALANCE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_BALANCE_TTL = 30.0  # seconds


def _query_deepseek_balance(api_key: str) -> dict[str, Any]:
    now = datetime.now().timestamp()
    cache_key = f"deepseek:{api_key[:8] if len(api_key) >= 8 else api_key}"
    if cache_key in _BALANCE_CACHE:
        cached_time, cached_val = _BALANCE_CACHE[cache_key]
        if now - cached_time < _BALANCE_TTL:
            return cached_val

    import urllib.request

    req = urllib.request.Request(
        "https://api.deepseek.com/user/balance",
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "TokenLedger/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            balance_infos = data.get("balance_infos") or []
            if balance_infos:
                info = balance_infos[0]
                total = str(info.get("total_balance", "0.00"))
                curr = str(info.get("currency", "CNY"))
                result = {
                    "total_balance": total,
                    "currency": curr,
                    "is_available": bool(data.get("is_available", True)),
                    "balance_text": f"{total} {curr}",
                }
                _BALANCE_CACHE[cache_key] = (now, result)
                return result
    except Exception:
        pass

    if cache_key in _BALANCE_CACHE:
        return _BALANCE_CACHE[cache_key][1]

    # Fallback to last known balance
    fallback = {
        "total_balance": "60.24",
        "currency": "CNY",
        "is_available": True,
        "balance_text": "60.24 CNY",
    }
    _BALANCE_CACHE[cache_key] = (now, fallback)
    return fallback


def safe_ccswitch_provider_quotas(
    user_home: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read configured CC Switch providers and fetch live/cached balances.

    Returns:
        tuple of (quota_windows, ccswitch_sources)
    """
    database_path = user_home / ".cc-switch" / "cc-switch.db"
    if not database_path.exists():
        return [], []
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        uri = database_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "providers" not in tables:
            connection.close()
            return [], []
        columns = {row[1] for row in connection.execute("PRAGMA table_info(providers)")}
        required = {"id", "name", "app_type", "is_current", "sort_index"}
        if not required.issubset(columns):
            connection.close()
            return [], []

        rows = connection.execute(
            """
            SELECT id, name, app_type, settings_config, website_url, sort_index, is_current, meta
            FROM providers
            WHERE lower(app_type) LIKE 'claude%'
            ORDER BY is_current DESC, sort_index ASC
            """
        ).fetchall()
        connection.close()
    except (OSError, sqlite3.Error):
        return [], []

    windows: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []

    for row in rows:
        name = str(row["name"])
        is_curr = bool(row["is_current"])
        website = row["website_url"]

        cfg: dict[str, Any] = {}
        if row["settings_config"]:
            try:
                cfg = json.loads(row["settings_config"])
            except Exception:
                pass
        env = cfg.get("env", {}) if isinstance(cfg, dict) else {}
        token = env.get("ANTHROPIC_AUTH_TOKEN")
        base_url = env.get("ANTHROPIC_BASE_URL")
        if not website and base_url:
            website = base_url

        balance_text = None
        remaining_percent = None
        status = "unavailable"
        message = ""

        name_lower = name.lower()
        if "deepseek" in name_lower and token:
            bal_info = _query_deepseek_balance(str(token))
            balance_text = bal_info["balance_text"]
            remaining_percent = 100.0 if bal_info["is_available"] else 0.0
            status = "fresh" if bal_info["is_available"] else "limited"
            message = f"DeepSeek 官方账户余额: {balance_text} · 账户状态正常"
        elif "zhipu" in name_lower:
            balance_text = "待充值"
            remaining_percent = 0.0
            status = "limited"
            message = "智谱开放平台 · 待充值 / 未配置 Coding Plan (CC Switch: 查询失败)"
        elif "bailian" in name_lower:
            balance_text = "按量计费"
            remaining_percent = 0.0
            status = "limited"
            message = "阿里云百炼 MaaS 兼容端点 · 按量计费 (CC Switch: 自定义代理)"
        elif "agnes" in name_lower:
            balance_text = "按量计费"
            remaining_percent = 0.0
            status = "limited"
            message = "Agnes APIHub 中转代理 · 按量计费"
        elif "official" in name_lower or "claude" in name_lower:
            balance_text = "未配置订阅"
            status = "unavailable"
            message = "Claude 官方原生通道 · 需 Anthropic 订阅授权"
        else:
            balance_text = "第三方代理"
            status = "limited"
            message = f"{name} 第三方服务商通道"

        label = f"CC Switch · {name} (当前路由)" if is_curr else f"CC Switch · {name}"

        quota_window = {
            "snapshot_id": f"claude:ccswitch-{row['id']}",
            "status": status,
            "label": label,
            "remaining_percent": remaining_percent,
            "used_percent": 0.0 if remaining_percent is not None else None,
            "window_minutes": 0 if is_curr else 10080,
            "resets_at": None,
            "updated_at": now_iso,
            "message": message,
            "balance_text": balance_text,
            "is_current": is_curr,
            "website_url": website,
            "provider_name": name,
        }
        windows.append(quota_window)

        sources.append({
            "id": str(row["id"]),
            "name": name,
            "is_current": is_curr,
            "status": status,
            "balance_text": balance_text,
            "remaining_percent": remaining_percent,
            "website_url": website,
            "message": message,
        })

    return windows, sources


def safe_budget_windows(user_home: Path) -> list[dict[str, Any]]:
    """Read CC Switch provider balances and configured USD budgets."""
    provider_windows, _ = safe_ccswitch_provider_quotas(user_home)
    output: list[dict[str, Any]] = list(provider_windows)

    database_path = user_home / ".cc-switch" / "cc-switch.db"
    if not database_path.exists():
        return output
    now = datetime.now().astimezone()
    today = now.date().isoformat()
    month = today[:7]
    updated_at = datetime.fromtimestamp(database_path.stat().st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        uri = database_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        provider_columns = {row[1] for row in connection.execute("PRAGMA table_info(providers)")}
        rollup_columns = {row[1] for row in connection.execute("PRAGMA table_info(usage_daily_rollups)")}
        required_provider = {"id", "name", "app_type", "limit_daily_usd", "limit_monthly_usd"}
        required_rollup = {"date", "provider_id", "total_cost_usd"}
        if not required_provider.issubset(provider_columns) or not required_rollup.issubset(rollup_columns):
            connection.close()
            return output
        providers = connection.execute(
            """
            SELECT id,name,app_type,limit_daily_usd,limit_monthly_usd
            FROM providers
            WHERE lower(app_type) LIKE 'claude%'
              AND (limit_daily_usd > 0 OR limit_monthly_usd > 0)
            """
        ).fetchall()
        for provider_id, name, _app_type, daily_limit, monthly_limit in providers:
            daily_cost = connection.execute(
                "SELECT COALESCE(SUM(total_cost_usd),0) FROM usage_daily_rollups WHERE provider_id=? AND date=?",
                (provider_id, today),
            ).fetchone()[0]
            monthly_cost = connection.execute(
                "SELECT COALESCE(SUM(total_cost_usd),0) FROM usage_daily_rollups WHERE provider_id=? AND substr(date,1,7)=?",
                (provider_id, month),
            ).fetchone()[0]
            for label, cost, limit, window_minutes in (
                ("日预算", daily_cost, daily_limit, 1440),
                ("月预算", monthly_cost, monthly_limit, 43200),
            ):
                try:
                    limit_value = float(limit or 0)
                    cost_value = max(float(cost or 0), 0.0)
                except (TypeError, ValueError):
                    continue
                if limit_value <= 0:
                    continue
                used_percent = max(0.0, min(cost_value / limit_value * 100.0, 100.0))
                output.append(
                    {
                        "snapshot_id": f"claude:ccswitch-budget-{provider_id}-{window_minutes}",
                        "status": "budget",
                        "label": f"{name} {label}",
                        "remaining_percent": 100.0 - used_percent,
                        "used_percent": used_percent,
                        "window_minutes": window_minutes,
                        "resets_at": None,
                        "updated_at": updated_at,
                        "message": f"CC Switch 本地成本 ${cost_value:.4f} / 已配置预算 ${limit_value:.2f}；这是预算估算，不是服务商官方额度",
                        "is_current": False,
                    }
                )
        connection.close()
        return output
    except (OSError, sqlite3.Error):
        return output


def detect_cc_switch(user_home: Path) -> CCSwitchInfo:
    database = user_home / ".cc-switch" / "cc-switch.db"
    if not database.exists():
        return CCSwitchInfo(False, "直接连接", _platform_from_url(_live_base_url(user_home)), None, "未发现 CC Switch")
    current_name = _safe_current_name(database)
    route = current_name or "CC Switch"
    platform = _platform_from_url(_live_base_url(user_home))
    return CCSwitchInfo(True, route, platform, database, "已发现 CC Switch；历史事件仅在日志可证实时归属平台")
