from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import DiscoveredFile, ParsedFile, ProviderProbe, QuotaSnapshot, UsageEvent
from ..registry import descriptor
from .base import ProviderAdapter
from .common import as_nonnegative_int, normalized_timestamp, stable_id


SESSION_PATTERN = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)


def _reset_time(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace("+00:00", "Z")
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OSError):
        return None


def _quota_from_window(agent: str, key: str, raw: Any, updated_at: str) -> QuotaSnapshot | None:
    if not isinstance(raw, dict):
        return None
    used = raw.get("used_percent")
    try:
        used_percent = max(0.0, min(float(used), 100.0))
    except (TypeError, ValueError):
        used_percent = None
    minutes = as_nonnegative_int(raw.get("window_minutes")) or None
    if minutes == 300:
        label = "5 小时窗口"
    elif minutes == 10080:
        label = "每周窗口"
    elif key == "primary":
        label = "主要额度窗口"
    else:
        label = "次要额度窗口"
    remaining = 100.0 - used_percent if used_percent is not None else None
    return QuotaSnapshot(
        snapshot_id=f"{agent}:{key}",
        agent=agent,
        label=label,
        status="fresh",
        remaining_percent=remaining,
        used_percent=used_percent,
        window_minutes=minutes,
        resets_at=_reset_time(raw.get("resets_at")),
        updated_at=updated_at,
        message="来自 Codex 会话返回的服务端额度窗口",
    )


class CodexAdapter(ProviderAdapter):
    descriptor = descriptor("codex")

    @property
    def codex_home(self) -> Path:
        return self.user_home / ".codex"

    def discover_files(self) -> list[DiscoveredFile]:
        found: list[DiscoveredFile] = []
        for folder in ("sessions", "archived_sessions"):
            root = self.codex_home / folder
            if not root.exists():
                continue
            hint = f"~/.codex/{folder}/**/rollout-*.jsonl"
            found.extend(DiscoveredFile(path, hint) for path in root.rglob("*.jsonl") if path.is_file())
        return sorted(found, key=lambda item: str(item.path))

    def probe(self) -> ProviderProbe:
        files = self.discover_files()
        if not self.codex_home.exists():
            return ProviderProbe("missing", "Codex", "~/.codex", "未发现 Codex 数据目录")
        status = "ready" if files else "empty"
        message = f"发现 {len(files)} 个本地会话日志" if files else "已发现 Codex，但没有会话日志"
        return ProviderProbe(status, "Codex 本地会话", "~/.codex/sessions + archived_sessions", message)

    def parse_file(self, discovered: DiscoveredFile) -> ParsedFile:
        path = discovered.path
        match = SESSION_PATTERN.search(path.name)
        session_id = match.group(1) if match else path.stem
        model = "未识别模型"
        seen: set[tuple[Any, ...]] = set()
        result = ParsedFile(session_count=1)
        latest_quotas: dict[str, QuotaSnapshot] = {}
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                if '"model"' not in line and '"token_count"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = obj.get("payload") or {}
                payload_model = payload.get("model")
                if isinstance(payload_model, str) and payload_model:
                    model = payload_model
                if payload.get("type") != "token_count":
                    continue
                usage = ((payload.get("info") or {}).get("last_token_usage") or {})
                timestamp = normalized_timestamp(obj.get("timestamp"), path)
                if not isinstance(usage, dict) or not usage:
                    continue
                input_tokens = as_nonnegative_int(usage.get("input_tokens"))
                cached = as_nonnegative_int(usage.get("cached_input_tokens"))
                cache_write = as_nonnegative_int(usage.get("cache_write_input_tokens"))
                output = as_nonnegative_int(usage.get("output_tokens"))
                reasoning = as_nonnegative_int(usage.get("reasoning_output_tokens"))
                total = as_nonnegative_int(usage.get("total_tokens")) or input_tokens + output
                dedupe = (timestamp, total, input_tokens, output, cached)
                if dedupe not in seen:
                    seen.add(dedupe)
                    result.events.append(
                        UsageEvent(
                            event_id=stable_id("codex", str(path), timestamp, total, input_tokens, output),
                            agent="codex",
                            route="原生订阅",
                            platform="OpenAI",
                            model=model,
                            session_id=session_id,
                            occurred_at=timestamp,
                            input_tokens=input_tokens,
                            cached_input_tokens=cached,
                            cache_write_tokens=cache_write,
                            output_tokens=output,
                            reasoning_tokens=reasoning,
                            total_tokens=total,
                        )
                    )
                rate_limits = payload.get("rate_limits")
                if isinstance(rate_limits, dict):
                    for key in ("primary", "secondary", "individual_limit"):
                        snapshot = _quota_from_window("codex", key, rate_limits.get(key), timestamp)
                        if snapshot:
                            previous = latest_quotas.get(key)
                            if previous is None or previous.updated_at <= snapshot.updated_at:
                                latest_quotas[key] = snapshot
        result.quotas = list(latest_quotas.values())
        return result

