"""Versioned API reference prices, never subscription bills or historical invoices."""
from __future__ import annotations

from typing import Any, Iterable

USD_TO_CNY = 7.20  # Display conversion assumption, not a live exchange rate.
PRICE_CHECKED_AT = "2026-09-08"
OPENAI_MODELS = "https://developers.openai.com/api/docs/models/"
CLAUDE_PRICING = "https://platform.claude.com/docs/en/about-claude/pricing"
DEEPSEEK_PRICING = "https://api-docs.deepseek.com/quick_start/pricing/"

def _rule(inp: float, cache: float, out: float, url: str,
          note: str = "", write: float | None = None) -> dict[str, Any]:
    return {"input": inp, "cache": cache, "output": out,
            "write": inp if write is None else write, "currency": "USD",
            "source": url, "note": note}

_BASE = "标准基础档参考；不重放历史调价、长上下文加价、服务档位或订阅账单"
PRICING_CATALOG = {
    "gpt-5.6-sol": _rule(4, .4, 20, OPENAI_MODELS + "gpt-5.6-sol", _BASE, 5),
    "gpt-5.6-terra": _rule(2, .2, 12, OPENAI_MODELS + "gpt-5.6-terra", _BASE, 2.5),
    "gpt-5.6-luna": _rule(.2, .02, 1.2, OPENAI_MODELS + "gpt-5.6-luna", _BASE, .25),
    "gpt-6-astra": _rule(10, 1, 50, OPENAI_MODELS + "gpt-6-astra", _BASE, 12.5),
    "claude-haiku-4-5": _rule(1, .1, 5, CLAUDE_PRICING, _BASE + "；缓存写入按5分钟参考", 1.25),
    "claude-sonnet-4-5": _rule(3, .3, 15, CLAUDE_PRICING, _BASE + "；缓存写入按5分钟参考", 3.75),
    "claude-sonnet-4-6": _rule(3, .3, 15, CLAUDE_PRICING, _BASE + "；缓存写入按5分钟参考", 3.75),
    "claude-sonnet-5": _rule(2, .2, 10, CLAUDE_PRICING, _BASE + "；缓存写入按5分钟参考", 2.5),
    "claude-opus-5": _rule(5, .5, 25, CLAUDE_PRICING, _BASE + "；缓存写入按5分钟参考", 6.25),
    "deepseek-v4-flash": _rule(.44, .014, 1.32, DEEPSEEK_PRICING, "当前高峰档参考；低峰为一半，不重放历史时段与调价"),
    "deepseek-v4-pro": _rule(1.32, .044, 3.96, DEEPSEEK_PRICING, "当前高峰档参考；低峰为一半，不重放历史时段与调价"),
    "deepseek-v4-flash-vision-exp": _rule(.44, .014, 1.32, DEEPSEEK_PRICING, "当前高峰档参考；低峰为一半，不重放历史时段与调价"),
}

def estimate_token_cost(model_name: str, input_tokens: int,
                        cached_input_tokens: int, output_tokens: int,
                        agent: str = "", cache_write_tokens: int = 0) -> dict[str, Any]:
    # Only exact identifiers are supported. A name prefix is not a price contract.
    key = (model_name or "").strip().lower()
    rule = PRICING_CATALOG.get(key)
    if rule is None:
        return {"cost_known": False, "cost_cny": 0.0, "cost_usd": 0.0,
                "cost_cny_text": "未提供", "cost_usd_text": "未提供",
                "pricing_source": "没有已核验的参考单价", "pricing_model_matched": None,
                "unit_rate_text": "未知价格未计入估算", "pricing_checked_at": PRICE_CHECKED_AT}
    inp = max(int(input_tokens or 0), 0)
    cached = min(max(int(cached_input_tokens or 0), 0), inp)
    write = min(max(int(cache_write_tokens or 0), 0), inp - cached)
    out = max(int(output_tokens or 0), 0)
    usd = ((inp - cached - write) * rule["input"] + cached * rule["cache"]
           + write * rule["write"] + out * rule["output"]) / 1_000_000
    cny = usd * USD_TO_CNY
    return {
        "cost_known": True, "cost_cny": cny, "cost_usd": usd,
        "cost_cny_text": f"¥{cny:,.2f}", "cost_usd_text": f"${usd:,.2f}",
        "pricing_source": rule["source"], "pricing_model_matched": key,
        "pricing_checked_at": PRICE_CHECKED_AT,
        "unit_rate_text": (f"输入 ${rule['input']}/M · 缓存读取 ${rule['cache']}/M · "
                           f"缓存写入 ${rule['write']}/M · 输出 ${rule['output']}/M · "
                           f"{rule['note']}；美元换算人民币固定按 {USD_TO_CNY}"),
    }

def cost_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    cny = usd = 0.0
    unknown = known = 0
    for row in rows:
        tokens = max(int(row.get("total_tokens") or 0), 0)
        price = estimate_token_cost(row.get("model", ""), row.get("input_tokens", 0),
                                    row.get("cached_input_tokens", 0), row.get("output_tokens", 0),
                                    row.get("agent", ""), row.get("cache_write_tokens", 0))
        if price["cost_known"]:
            cny += price["cost_cny"]
            usd += price["cost_usd"]
            known += tokens
        else:
            unknown += tokens
    prefix = "部分估算 " if unknown and known else ""
    return {"estimated_cost_cny": round(cny, 4), "estimated_cost_usd": round(usd, 4),
            "has_unpriced_usage": unknown > 0, "unpriced_tokens": unknown,
            "cost_cny_text": "未提供" if unknown and not known else f"{prefix}¥{cny:,.2f}",
            "cost_usd_text": "未提供" if unknown and not known else f"{prefix}${usd:,.2f}"}
