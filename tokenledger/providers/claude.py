from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import DiscoveredFile, ParsedFile, ProviderProbe, UsageEvent
from ..registry import descriptor
from .base import ProviderAdapter
from .ccswitch import (
    detect_cc_switch,
    rollup_input_total,
    safe_budget_windows,
    safe_usage_rollup_summary,
    safe_usage_rollups,
    unique_model_platforms,
)
from .common import as_nonnegative_int, normalized_timestamp, stable_id


def _reasoning_tokens(details: Any) -> int:
    if not isinstance(details, dict):
        return 0
    for key in ("reasoning_tokens", "thinking_tokens"):
        if key in details:
            return as_nonnegative_int(details.get(key))
    return 0


class ClaudeAdapter(ProviderAdapter):
    descriptor = descriptor("claude")
    parser_revision = "cc-switch-proxy-plus-3p-v2"

    @property
    def claude_home(self) -> Path:
        return self.user_home / ".claude"

    @property
    def claude_3p_home(self) -> Path:
        return self.user_home / "AppData" / "Local" / "Claude-3p"

    def discover_files(self) -> list[DiscoveredFile]:
        files: list[DiscoveredFile] = []
        root = self.claude_home / "projects"
        if root.exists():
            files.extend(
                DiscoveredFile(path, "~/.claude/projects/**/*.jsonl")
                for path in sorted(root.rglob("*.jsonl"), key=str)
                if path.is_file()
            )
        root_3p = self.claude_3p_home / "local-agent-mode-sessions"
        if root_3p.exists():
            files.extend(
                DiscoveredFile(path, "%LOCALAPPDATA%/Claude-3p/**/.claude/projects/**/*.jsonl")
                for path in sorted(root_3p.rglob("*.jsonl"), key=str)
                if path.is_file()
                and "audit" not in path.name.lower()
                and "telemetry" not in str(path).lower()
            )
        switch = detect_cc_switch(self.user_home)
        # Keep the database in the discovery set even if a read is temporarily blocked;
        # the scanner will retain the last valid events and mark only this source as errored.
        if switch.database_path:
            files.append(
                DiscoveredFile(
                    switch.database_path,
                    "~/.cc-switch/cc-switch.db::usage_daily_rollups + proxy_request_logs（Claude 账户日汇总）",
                )
            )
        return files

    def should_reparse(self, discovered: DiscoveredFile) -> bool:
        # SQLite may keep fresh rollups in WAL while the main file signature stays unchanged.
        return discovered.path.name.lower() == "cc-switch.db"

    def probe(self) -> ProviderProbe:
        files = self.discover_files()
        switch = detect_cc_switch(self.user_home)
        session_files = [item for item in files if item.path.suffix.lower() == ".jsonl"]
        rollup = safe_usage_rollup_summary(self.user_home)
        if not self.claude_home.exists() and not rollup["available"]:
            return ProviderProbe("missing", "Claude Code", "~/.claude", "未发现 Claude Code 数据目录")
        status = "ready" if files else "empty"
        route_message = f"；当前路由：{switch.route} / {switch.platform}" if switch.installed else ""
        rollup_message = ""
        if rollup["available"]:
            rollup_message = (
                f"；CC Switch 账户日汇总 {rollup['days']} 天"
                f"（{rollup['start_date']}—{rollup['end_date']}）"
            )
        return ProviderProbe(
            status,
            "Claude Code 会话 + CC Switch 账户日汇总",
            "~/.claude/projects/**/*.jsonl + ~/.cc-switch/cc-switch.db::usage_daily_rollups",
            (f"发现 {len(session_files)} 个会话日志" if session_files else "未发现会话日志")
            + rollup_message
            + route_message,
            {
                "cc_switch": switch.installed,
                "current_route": switch.route,
                "current_platform": switch.platform,
                "account_rollup": rollup,
                "reconciliation_policy": "同日存在 CC Switch 账户汇总时，账户汇总优先；会话日志仅补足无汇总日期",
                "budget_windows": safe_budget_windows(self.user_home) if switch.installed else [],
                "quota_note": (
                    "CC Switch 的余额查询结果未写入本地数据库；配置日/月 USD 预算后可显示预算百分比"
                    if switch.installed else ""
                ),
            },
        )

    def _get_switch_meta(self) -> tuple[str, dict[str, str], str]:
        if not hasattr(self, "_cached_switch_meta"):
            switch = detect_cc_switch(self.user_home)
            route = "CC Switch" if switch.installed else "直接连接"
            platforms = unique_model_platforms(self.user_home) if switch.installed else {}
            self._cached_switch_meta = (route, platforms, switch.platform)
        return self._cached_switch_meta

    def parse_file(self, discovered: DiscoveredFile) -> ParsedFile:
        path = discovered.path
        if path.name.lower() == "cc-switch.db":
            return self._parse_cc_switch_rollups(path)
        route, model_platforms, fallback_platform = self._get_switch_meta()
        seen: set[str] = set()
        result = ParsedFile(session_count=1)
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                if '"usage"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = obj.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                message_id = str(message.get("id") or obj.get("uuid") or f"line-{line_number}")
                if message_id in seen:
                    continue
                seen.add(message_id)
                timestamp = normalized_timestamp(obj.get("timestamp"), path)
                direct_input = as_nonnegative_int(usage.get("input_tokens"))
                cached = as_nonnegative_int(usage.get("cache_read_input_tokens"))
                cache_write = as_nonnegative_int(usage.get("cache_creation_input_tokens"))
                input_tokens = direct_input + cached + cache_write
                output = as_nonnegative_int(usage.get("output_tokens"))
                reasoning = _reasoning_tokens(usage.get("output_tokens_details"))
                model = str(message.get("model") or "未识别模型")
                platform = model_platforms.get(model, fallback_platform)
                session_id = str(obj.get("sessionId") or path.stem)
                result.events.append(
                    UsageEvent(
                        event_id=stable_id("claude", str(path), message_id),
                        agent="claude",
                        route=route,
                        platform=platform,
                        model=model,
                        session_id=session_id,
                        occurred_at=timestamp,
                        input_tokens=input_tokens,
                        cached_input_tokens=cached,
                        cache_write_tokens=cache_write,
                        output_tokens=output,
                        reasoning_tokens=reasoning,
                        total_tokens=input_tokens + output,
                    )
                )
        return result

    def _parse_cc_switch_rollups(self, path: Path) -> ParsedFile:
        rows = safe_usage_rollups(self.user_home)
        if path.exists() and not rows:
            raise ValueError("CC Switch 账户日汇总表不可用或为空")
        result = ParsedFile(
            session_count=0,
            metadata={"usage_scope": "account_daily", "source": "CC Switch 账户日汇总"},
        )
        for row in rows:
            date_value = row["date"]
            input_tokens = rollup_input_total(row)
            cached = as_nonnegative_int(row.get("cache_read_tokens"))
            cache_write = as_nonnegative_int(row.get("cache_creation_tokens"))
            output = as_nonnegative_int(row.get("output_tokens"))
            provider_id = str(row.get("provider_id") or "unknown")
            model = str(row.get("model") or "模型未记录")
            semantics = as_nonnegative_int(row.get("input_token_semantics"))
            result.events.append(
                UsageEvent(
                    event_id=stable_id(
                        "claude-account-day", provider_id, date_value, model, str(semantics)
                    ),
                    agent="claude",
                    route="CC Switch 账户日汇总",
                    platform=str(row.get("provider_name") or "CC Switch 历史平台"),
                    model=model,
                    session_id=f"ccswitch-account:{provider_id}",
                    # Use local noon in Asia/Shanghai so a date-only rollup stays on its source date.
                    occurred_at=f"{date_value}T04:00:00Z",
                    input_tokens=input_tokens,
                    cached_input_tokens=cached,
                    cache_write_tokens=cache_write,
                    output_tokens=output,
                    total_tokens=input_tokens + output,
                    usage_scope="account_daily",
                    call_count=as_nonnegative_int(row.get("request_count")),
                )
            )
        return result
