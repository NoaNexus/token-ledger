from __future__ import annotations

from .models import ProviderDescriptor


REGISTRY: tuple[ProviderDescriptor, ...] = (
    ProviderDescriptor("codex", "Codex", "#146EF5", "CX", "tokens+quota"),
    ProviderDescriptor("claude", "Claude Code", "#F07040", "CL", "tokens"),
    ProviderDescriptor("antigravity", "Antigravity", "#7857FF", "AG", "activity"),
)


def descriptor(agent_id: str) -> ProviderDescriptor:
    return next(item for item in REGISTRY if item.id == agent_id)

