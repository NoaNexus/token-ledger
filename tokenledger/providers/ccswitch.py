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
        rollup_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(usage_daily_rollups)")
        }
        provider_columns = {row[1] for row in connection.execute("PRAGMA table_info(providers)")}
        if not ROLLUP_COLUMNS.issubset(rollup_columns) or not {"id", "name"}.issubset(
            provider_columns
        ):
            return []
        rows = connection.execute(
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
        ).fetchall()
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


def safe_budget_windows(user_home: Path) -> list[dict[str, Any]]:
    """Read configured CC Switch USD budgets and local cost rollups only."""
    database_path = user_home / ".cc-switch" / "cc-switch.db"
    if not database_path.exists():
        return []
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
            return []
        providers = connection.execute(
            """
            SELECT id,name,app_type,limit_daily_usd,limit_monthly_usd
            FROM providers
            WHERE lower(app_type) LIKE 'claude%'
              AND (limit_daily_usd > 0 OR limit_monthly_usd > 0)
            """
        ).fetchall()
        output: list[dict[str, Any]] = []
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
                        "status": "budget",
                        "label": f"{name} {label}",
                        "remaining_percent": 100.0 - used_percent,
                        "used_percent": used_percent,
                        "window_minutes": window_minutes,
                        "resets_at": None,
                        "updated_at": updated_at,
                        "message": f"CC Switch 本地成本 ${cost_value:.4f} / 已配置预算 ${limit_value:.2f}；这是预算估算，不是服务商官方额度",
                    }
                )
        connection.close()
        return sorted(output, key=lambda item: item["window_minutes"])
    except (OSError, sqlite3.Error):
        return []


def detect_cc_switch(user_home: Path) -> CCSwitchInfo:
    database = user_home / ".cc-switch" / "cc-switch.db"
    if not database.exists():
        return CCSwitchInfo(False, "直接连接", _platform_from_url(_live_base_url(user_home)), None, "未发现 CC Switch")
    current_name = _safe_current_name(database)
    route = current_name or "CC Switch"
    platform = _platform_from_url(_live_base_url(user_home))
    return CCSwitchInfo(True, route, platform, database, "已发现 CC Switch；历史事件仅在日志可证实时归属平台")
