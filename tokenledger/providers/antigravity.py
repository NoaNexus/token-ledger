from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from ..models import DiscoveredFile, ParsedFile, ProviderProbe, UsageEvent
from ..registry import descriptor
from .base import ProviderAdapter
from .common import normalized_timestamp, stable_id


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_ASCII_RUN_RE = re.compile(r"[A-Za-z0-9_]+")


def estimate_visible_tokens(value: Any) -> int:
    """Estimate visible text tokens without pretending to use Gemini's tokenizer."""
    if value is None or value == "":
        return 0
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            value = str(value)
    count = 0
    cursor = 0
    for match in _ASCII_RUN_RE.finditer(value):
        prefix = value[cursor : match.start()]
        count += sum(1 for char in prefix if not char.isspace())
        count += math.ceil(len(match.group(0)) / 4)
        cursor = match.end()
    count += sum(1 for char in value[cursor:] if not char.isspace())
    return count


def _entry_estimate(obj: dict[str, Any]) -> tuple[int, int, int]:
    event_type = str(obj.get("type") or "")
    content_tokens = estimate_visible_tokens(obj.get("content"))
    thinking_tokens = estimate_visible_tokens(obj.get("thinking"))
    tool_tokens = estimate_visible_tokens(obj.get("tool_calls"))
    if event_type == "PLANNER_RESPONSE":
        reasoning = thinking_tokens
        output = content_tokens + tool_tokens + reasoning
        return 0, output, reasoning
    if event_type in {"USER_INPUT", "GENERIC", "SYSTEM_MESSAGE"}:
        return content_tokens + thinking_tokens + tool_tokens, 0, 0
    return 0, 0, 0


class AntigravityAdapter(ProviderAdapter):
    descriptor = descriptor("antigravity")
    parser_revision = "visible-estimate-v2"

    @property
    def antigravity_home(self) -> Path:
        return self.user_home / ".gemini" / "antigravity"

    def discover_files(self) -> list[DiscoveredFile]:
        root = self.antigravity_home / "brain"
        if not root.exists():
            return []
        return [
            DiscoveredFile(path, "~/.gemini/antigravity/brain/*/.system_generated/logs/transcript.jsonl")
            for path in sorted(root.rglob("transcript.jsonl"), key=str)
            if path.is_file()
        ]

    def probe(self) -> ProviderProbe:
        files = self.discover_files()
        activity_count = 0
        estimated_tokens = 0
        truncated_entries = 0
        event_types: set[str] = set()
        for discovered in files:
            try:
                with discovered.path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        activity_count += 1
                        input_tokens, output_tokens, _ = _entry_estimate(obj)
                        estimated_tokens += input_tokens + output_tokens
                        if obj.get("truncated_fields"):
                            truncated_entries += 1
                        event_type = obj.get("type")
                        if isinstance(event_type, str):
                            event_types.add(event_type)
            except OSError:
                continue
        conversation_root = self.antigravity_home / "conversations"
        conversation_databases = len(list(conversation_root.glob("*.db"))) if conversation_root.exists() else 0
        if not self.antigravity_home.exists():
            return ProviderProbe("missing", "Antigravity", "~/.gemini/antigravity", "未发现 Antigravity 数据目录")
        return ProviderProbe(
            "limited" if files else "empty",
            "Antigravity 本地可见文本",
            "~/.gemini/antigravity/brain",
            (f"发现 {len(files)} 个会话、{activity_count} 条记录；估算 {estimated_tokens:,} Token" if files else "未发现会话记录"),
            {
                "usage_available": bool(files),
                "usage_mode": "estimated",
                "model_available": False,
                "activity_count": activity_count,
                "estimated_tokens": estimated_tokens,
                "truncated_entries": truncated_entries,
                "event_types": sorted(event_types),
                "conversation_databases": conversation_databases,
                "reason": "按本地 transcript 可见文本估算；不含隐藏上下文、重复上下文和官方计费修正",
            },
        )

    def parse_file(self, discovered: DiscoveredFile) -> ParsedFile:
        event_types: set[str] = set()
        path = discovered.path
        session_id = path.parents[2].name if len(path.parents) > 2 else path.stem
        result = ParsedFile(session_count=1)
        truncated_entries = 0
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, 1):
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event_type = obj.get("type")
                    if isinstance(event_type, str):
                        event_types.add(event_type)
                    input_tokens, output_tokens, reasoning_tokens = _entry_estimate(obj)
                    total_tokens = input_tokens + output_tokens
                    if not total_tokens:
                        continue
                    if obj.get("truncated_fields"):
                        truncated_entries += 1
                    timestamp = normalized_timestamp(
                        obj.get("created_at") if isinstance(obj.get("created_at"), str) else None,
                        path,
                    )
                    result.events.append(
                        UsageEvent(
                            event_id=stable_id("antigravity-estimate", str(path), line_number),
                            agent="antigravity",
                            route="本地可见文本估算",
                            platform="Google Antigravity",
                            model="模型未记录（估算）",
                            session_id=session_id,
                            occurred_at=timestamp,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            reasoning_tokens=reasoning_tokens,
                            total_tokens=total_tokens,
                            usage_mode="estimated",
                        )
                    )
        except OSError:
            raise
        result.metadata = {
            "event_types": sorted(event_types),
            "usage_available": True,
            "usage_mode": "estimated",
            "truncated_entries": truncated_entries,
        }
        return result
