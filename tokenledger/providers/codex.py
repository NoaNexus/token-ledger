from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import DiscoveredFile, ParsedFile, ProviderProbe, QuotaSnapshot, UsageEvent
from ..registry import descriptor
from .base import ProviderAdapter
from .common import as_nonnegative_int, normalized_timestamp, stable_id


SESSION_PATTERN = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
TOKEN_FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                'output_tokens', 'reasoning_output_tokens', 'total_tokens')


def _usage_signature(raw: Any) -> tuple[int, ...]:
    return tuple(as_nonnegative_int(raw.get(key)) for key in TOKEN_FIELDS)


def _counter(raw: Any) -> tuple[int, ...] | None:
    if not isinstance(raw, dict) or 'total_tokens' not in raw:
        return None
    return tuple(as_nonnegative_int(raw.get(key)) for key in TOKEN_FIELDS)


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
    parser_revision = "2"

    def _parent_counters(self, session_id: str) -> set[tuple[int, ...]]:
        # Only follow a verified UUID within the discovered session roots.
        if not SESSION_PATTERN.fullmatch(session_id):
            return set()
        if not hasattr(self, '_session_paths'):
            self.discover_files()
        result: set[tuple[int, ...]] = set()
        for path in self._session_paths.get(session_id, []):
            try:
                stat = path.stat()
                key = (str(path), stat.st_mtime_ns, stat.st_size)
                cache = getattr(self, '_parent_counter_cache', {})
                if key not in cache:
                    counters = set()
                    with path.open(encoding='utf-8', errors='replace') as handle:
                        for line in handle:
                            if '"token_count"' not in line:
                                continue
                            try:
                                obj = json.loads(line)
                            except ValueError:
                                continue
                            payload = obj.get('payload') or {}
                            value = _counter((payload.get('info') or {}).get('total_token_usage'))
                            if payload.get('type') == 'token_count' and value is not None:
                                counters.add(value)
                    cache[key] = counters
                    self._parent_counter_cache = cache
                result.update(cache[key])
            except OSError:
                continue
        return result

    def _parent_usage_signatures(self, session_id: str) -> Counter[tuple[int, ...]]:
        """Return counted per-call usage shapes for migration-only history matching."""
        if not SESSION_PATTERN.fullmatch(session_id):
            return Counter()
        if not hasattr(self, '_session_paths'):
            self.discover_files()
        result: Counter[tuple[int, ...]] = Counter()
        for path in self._session_paths.get(session_id, []):
            try:
                stat = path.stat()
                key = (str(path), stat.st_mtime_ns, stat.st_size)
                cache = getattr(self, '_parent_usage_cache', {})
                if key not in cache:
                    signatures: Counter[tuple[int, ...]] = Counter()
                    with path.open(encoding='utf-8', errors='replace') as handle:
                        for line in handle:
                            if '"token_count"' not in line:
                                continue
                            try:
                                obj = json.loads(line)
                            except ValueError:
                                continue
                            payload = obj.get('payload') or {}
                            if payload.get('type') != 'token_count':
                                continue
                            usage = (payload.get('info') or {}).get('last_token_usage') or {}
                            if isinstance(usage, dict):
                                signatures[_usage_signature(usage)] += 1
                    cache[key] = signatures
                    self._parent_usage_cache = cache
                result.update(cache[key])
            except OSError:
                continue
        return result

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
        self._session_paths: dict[str, list[Path]] = {}
        for item in found:
            match = SESSION_PATTERN.search(item.path.name)
            if match:
                self._session_paths.setdefault(match.group(1), []).append(item.path)
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
        result = ParsedFile(session_count=1, metadata={'codex_history_v2': True})
        previous: tuple[int, ...] | None = None
        inherited: set[tuple[int, ...]] = set()
        inherited_prefix = False
        epoch = 0
        latest_quotas: dict[str, QuotaSnapshot] = {}
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                if '"model"' not in line and '"token_count"' not in line and '"session_meta"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = obj.get("payload") or {}
                if obj.get('type') == 'session_meta':
                    identity = payload.get('id') or payload.get('session_id')
                    if isinstance(identity, str) and SESSION_PATTERN.fullmatch(identity):
                        session_id = identity
                    parent = payload.get('forked_from_id') or payload.get('forked_from')
                    if isinstance(parent, str):
                        inherited = self._parent_counters(parent)
                        inherited_prefix = bool(inherited)
                        parent_usage = self._parent_usage_signatures(parent)
                        if parent_usage:
                            result.metadata['codex_parent_usage_signatures'] = [
                                [*signature, count]
                                for signature, count in parent_usage.items()
                            ]
                payload_model = payload.get("model")
                if isinstance(payload_model, str) and payload_model:
                    model = payload_model
                if payload.get("type") != "token_count":
                    continue
                info = payload.get('info') or {}
                usage = info.get('last_token_usage') or {}
                timestamp = normalized_timestamp(obj.get("timestamp"), path)
                rate_limits = payload.get('rate_limits')
                if isinstance(rate_limits, dict):
                    for key in ('primary', 'secondary', 'individual_limit'):
                        snapshot = _quota_from_window('codex', key, rate_limits.get(key), timestamp)
                        if snapshot and (key not in latest_quotas or latest_quotas[key].updated_at <= timestamp):
                            latest_quotas[key] = snapshot
                cumulative = _counter(info.get('total_token_usage'))
                if cumulative is not None:
                    if inherited_prefix and cumulative in inherited:
                        previous = cumulative
                        result.metadata['inherited_events_skipped'] = result.metadata.get('inherited_events_skipped', 0) + 1
                        result.metadata.setdefault('codex_inherited_ids', []).append('codex-v2:c:' + stable_id(session_id, ('counter', epoch, cumulative)))
                        result.metadata.setdefault('codex_inherited_signatures', []).append(_usage_signature(usage))
                        continue
                    inherited_prefix = False
                    if previous is not None and cumulative[-1] == previous[-1]:
                        # Repeated progress/quota snapshots are not new calls.
                        continue
                    if previous is not None and cumulative[-1] > previous[-1]:
                        usage = {key: max(current - old, 0) for key, current, old in zip(TOKEN_FIELDS, cumulative, previous)}
                    elif previous is not None:
                        epoch += 1
                    previous = cumulative
                if not isinstance(usage, dict) or not usage:
                    continue
                input_tokens = as_nonnegative_int(usage.get("input_tokens"))
                cached = as_nonnegative_int(usage.get("cached_input_tokens"))
                cache_write = as_nonnegative_int(usage.get("cache_write_input_tokens"))
                output = as_nonnegative_int(usage.get("output_tokens"))
                reasoning = as_nonnegative_int(usage.get("reasoning_output_tokens"))
                total = as_nonnegative_int(usage.get("total_tokens")) or input_tokens + output
                dedupe = ('counter', epoch, cumulative) if cumulative is not None else (timestamp, total, input_tokens, output, cached, cache_write, reasoning)
                if dedupe not in seen:
                    seen.add(dedupe)
                    result.events.append(
                        UsageEvent(
                            event_id=('codex-v2:c:' if cumulative is not None else 'codex-v2:l:') + stable_id(session_id, dedupe),
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
        result.quotas = list(latest_quotas.values())
        return result
