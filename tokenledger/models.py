from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    id: str
    name: str
    color: str
    short: str
    data_capability: str


@dataclass(frozen=True, slots=True)
class UsageEvent:
    event_id: str
    agent: str
    route: str
    platform: str
    model: str
    session_id: str
    occurred_at: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    usage_mode: str = "reported"
    usage_scope: str = "session"
    call_count: int = 1


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    snapshot_id: str
    agent: str
    label: str
    status: str
    remaining_percent: float | None = None
    used_percent: float | None = None
    window_minutes: int | None = None
    resets_at: str | None = None
    updated_at: str = ""
    message: str = ""


@dataclass(slots=True)
class ParsedFile:
    events: list[UsageEvent] = field(default_factory=list)
    quotas: list[QuotaSnapshot] = field(default_factory=list)
    session_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    path: Path
    path_hint: str


@dataclass(slots=True)
class ProviderProbe:
    status: str
    label: str
    path_hint: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)
