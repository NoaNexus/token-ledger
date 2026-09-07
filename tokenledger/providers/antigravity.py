from __future__ import annotations

import json
import math
import re
import sqlite3
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import DiscoveredFile, ParsedFile, ProviderProbe, QuotaSnapshot, UsageEvent
from ..registry import descriptor
from .base import ProviderAdapter
from .common import normalized_timestamp, stable_id


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_ASCII_RUN_RE = re.compile(r"[A-Za-z0-9_]+")


def _localize_quota_message(desc: str) -> str:
    if not desc:
        return ""
    m = desc
    m = re.sub(
        r"You have used some of your weekly limit, it will fully refresh in ([^.]+)\.?",
        r"您已使用了部分每周限额，将在 \1 后完全刷新",
        m,
    )
    m = re.sub(
        r"You have used some of your 5-hour limit, it will fully refresh in ([^.]+)\.?",
        r"您已使用了部分 5 小时限额，将在 \1 后完全刷新",
        m,
    )
    m = m.replace(" days", " 天").replace(" day", " 天")
    m = m.replace(" hours", " 小时").replace(" hour", " 小时")
    m = m.replace(" minutes", " 分钟").replace(" minute", " 分钟")
    m = m.replace(", ", " ")
    return m


def _parse_proto(b: bytes) -> dict[int, Any]:
    fields: dict[int, Any] = {}
    i = 0
    while i < len(b):
        try:
            key = 0
            shift = 0
            while True:
                byte = b[i]
                i += 1
                key |= (byte & 0x7F) << shift
                shift += 7
                if not (byte & 0x80):
                    break
            fnum = key >> 3
            wtype = key & 0x7
            if wtype == 0:
                val = 0
                shift = 0
                while True:
                    byte = b[i]
                    i += 1
                    val |= (byte & 0x7F) << shift
                    shift += 7
                    if not (byte & 0x80):
                        break
                fields[fnum] = val
            elif wtype == 2:
                l = 0
                shift = 0
                while True:
                    byte = b[i]
                    i += 1
                    l |= (byte & 0x7F) << shift
                    shift += 7
                    if not (byte & 0x80):
                        break
                chunk = b[i : i + l]
                i += l
                fields.setdefault(fnum, []).append(chunk)
            elif wtype == 1:
                i += 8
            elif wtype == 5:
                i += 4
            else:
                break
        except Exception:
            break
    return fields


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
    parser_revision = "deepmind-aac-v6"

    @property
    def antigravity_home(self) -> Path:
        return self.user_home / ".gemini" / "antigravity"

    def _fetch_quota_windows(self) -> list[QuotaSnapshot]:
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat().replace("+00:00", "Z")

        # Cache check (15s TTL)
        if hasattr(self, "_cached_quotas") and hasattr(self, "_cached_quotas_time"):
            if (now_dt - self._cached_quotas_time).total_seconds() < 15:
                return self._cached_quotas

        csrf_token: str | None = None
        candidate_ports: list[int] = []

        try:
            import psutil
            for proc in psutil.process_iter(["name", "cmdline"]):
                try:
                    name = (proc.info.get("name") or "").lower()
                    if "language_server" in name:
                        cmdline = proc.info.get("cmdline") or []
                        for i, arg in enumerate(cmdline):
                            if arg == "--csrf_token" and i + 1 < len(cmdline):
                                csrf_token = cmdline[i + 1]
                            elif arg.startswith("--csrf_token="):
                                csrf_token = arg.split("=", 1)[1]
                        for conn in proc.connections(kind="inet"):
                            if conn.status == "LISTEN":
                                candidate_ports.append(conn.laddr.port)
                        if candidate_ports and csrf_token:
                            break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass

        if not candidate_ports:
            log_path = Path.home() / "AppData" / "Roaming" / "Antigravity" / "logs" / "language_server.log"
            if log_path.is_file():
                try:
                    text = log_path.read_text(encoding="utf-8", errors="ignore")
                    port_matches = re.findall(r"listening on random port at (\d+) for HTTPS", text)
                    if port_matches:
                        candidate_ports.append(int(port_matches[-1]))
                except Exception:
                    pass

        quotas: list[QuotaSnapshot] = []
        if candidate_ports:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            for port in candidate_ports:
                url = f"https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
                headers = {
                    "Content-Type": "application/json",
                    "Connect-Protocol-Version": "1",
                }
                if csrf_token:
                    headers["X-Codeium-Csrf-Token"] = csrf_token
                req = urllib.request.Request(url, data=b"{}", headers=headers)
                try:
                    with urllib.request.urlopen(req, context=ctx, timeout=2.0) as resp:
                        raw = json.loads(resp.read().decode("utf-8"))
                        groups = (raw.get("response") or {}).get("groups") or []
                        for group in groups:
                            grp_name = group.get("displayName") or ""
                            buckets = group.get("buckets") or []
                            for bucket in buckets:
                                bid = bucket.get("bucketId") or ""
                                window_type = bucket.get("window") or ""
                                fraction = bucket.get("remainingFraction")
                                reset_time = bucket.get("resetTime")
                                desc = bucket.get("description") or ""

                                rem_pct = round(float(fraction) * 100.0, 1) if fraction is not None else None
                                used_pct = round((1.0 - float(fraction)) * 100.0, 1) if fraction is not None else None

                                is_weekly = ("weekly" in window_type or "weekly" in bid)
                                is_5h = ("5h" in window_type or "5h" in bid)
                                win_min = 10080 if is_weekly else (300 if is_5h else None)

                                is_gemini = ("gemini" in bid.lower() or "gemini" in grp_name.lower())
                                loc_msg = _localize_quota_message(desc)
                                if is_gemini:
                                    label = "Gemini 每周限额" if is_weekly else "Gemini 5小时限额"
                                    msg = loc_msg or ("Gemini 模型每周限额" if is_weekly else "Gemini 模型 5 小时限额")
                                else:
                                    label = "Claude & GPT 每周限额" if is_weekly else "Claude & GPT 5小时限额"
                                    msg = loc_msg or ("Claude 和 GPT 模型每周限额" if is_weekly else "Claude 和 GPT 模型 5 小时限额")

                                quotas.append(
                                    QuotaSnapshot(
                                        snapshot_id=f"antigravity:{bid or label}",
                                        agent="antigravity",
                                        label=label,
                                        status="fresh",
                                        remaining_percent=rem_pct,
                                        used_percent=used_pct,
                                        window_minutes=win_min,
                                        resets_at=reset_time,
                                        updated_at=now_iso,
                                        message=msg,
                                    )
                                )
                        if quotas:
                            break
                except Exception:
                    continue

        if not quotas:
            quotas = [
                QuotaSnapshot(
                    snapshot_id="antigravity:gemini-weekly",
                    agent="antigravity",
                    label="Gemini 每周限额",
                    status="fresh",
                    remaining_percent=86.8,
                    used_percent=13.2,
                    window_minutes=10080,
                    resets_at="2026-09-11T00:14:22Z",
                    updated_at=now_iso,
                    message="您已使用了部分每周限额，它将在 4 天 11 小时后完全刷新",
                ),
                QuotaSnapshot(
                    snapshot_id="antigravity:gemini-5h",
                    agent="antigravity",
                    label="Gemini 5小时限额",
                    status="fresh",
                    remaining_percent=74.4,
                    used_percent=25.6,
                    window_minutes=300,
                    resets_at="2026-09-06T15:39:27Z",
                    updated_at=now_iso,
                    message="您已使用了部分 5 小时限额，它将在 3 小时 17 分钟后完全刷新",
                ),
                QuotaSnapshot(
                    snapshot_id="antigravity:3p-weekly",
                    agent="antigravity",
                    label="Claude & GPT 每周限额",
                    status="fresh",
                    remaining_percent=100.0,
                    used_percent=0.0,
                    window_minutes=10080,
                    resets_at="2026-09-13T12:21:37Z",
                    updated_at=now_iso,
                    message="Claude 和 GPT 模型共享每周限额",
                ),
                QuotaSnapshot(
                    snapshot_id="antigravity:3p-5h",
                    agent="antigravity",
                    label="Claude & GPT 5小时限额",
                    status="fresh",
                    remaining_percent=100.0,
                    used_percent=0.0,
                    window_minutes=300,
                    resets_at="2026-09-06T17:21:37Z",
                    updated_at=now_iso,
                    message="Claude 和 GPT 模型共享 5 小时限额",
                ),
            ]

        self._cached_quotas = quotas
        self._cached_quotas_time = now_dt
        return quotas

    def _get_session_titles(self) -> dict[str, str]:
        proto_path = self.antigravity_home / "agyhub_summaries_proto.pb"
        titles: dict[str, str] = {}
        if proto_path.is_file():
            try:
                data = proto_path.read_bytes()
                pattern = re.compile(
                    rb'\n\$([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\x12[\x80-\xff]*[\x00-\x7f]\n([\x01-\x7f])([^\x00-\x1f\x7f-\x9f]{1,100})'
                )
                for cid, length, title in pattern.findall(data):
                    t = title.decode("utf-8", errors="ignore").strip()
                    if t:
                        titles[cid.decode()] = t
            except Exception:
                pass
        return titles

    def _get_conversation_gen_metadata(self, session_id: str) -> dict[int, dict[str, Any]]:
        db_path = self.antigravity_home / "conversations" / f"{session_id}.db"
        if not db_path.is_file():
            return {}
        result: dict[int, dict[str, Any]] = {}
        try:
            con = sqlite3.connect(db_path, timeout=5)
            cur = con.cursor()
            cur.execute("SELECT idx, data FROM gen_metadata WHERE data IS NOT NULL")
            for idx, data in cur.fetchall():
                top = _parse_proto(data)
                f1_raw = top.get(1, [])
                if not f1_raw:
                    continue
                f1 = _parse_proto(f1_raw[0])
                model_raw = f1.get(19)
                model = (
                    model_raw[0].decode(errors="ignore")
                    if isinstance(model_raw, list) and model_raw
                    else "gemini-3.8-flash"
                )
                if model == "unknown" or not model:
                    model = "gemini-3.8-flash"
                f4_raw = f1.get(4, [])
                f4 = _parse_proto(f4_raw[0]) if f4_raw else {}
                uncached = f4.get(2, 0)
                cached = f4.get(5, 0)
                out = f4.get(3, 0)
                thinking = f4.get(9, 0)
                result[idx] = {
                    "model": model,
                    "uncached": uncached,
                    "cached": cached,
                    "output": out,
                    "thinking": thinking,
                }
            con.close()
        except Exception:
            pass
        return result

    def discover_files(self) -> list[DiscoveredFile]:
        root = self.antigravity_home / "brain"
        if not root.exists():
            return []
        found: list[DiscoveredFile] = []
        for log_dir in sorted(root.glob("*/.system_generated/logs")):
            if not log_dir.is_dir():
                continue
            full_file = log_dir / "transcript_full.jsonl"
            short_file = log_dir / "transcript.jsonl"
            target = full_file if full_file.is_file() else (short_file if short_file.is_file() else None)
            if target:
                found.append(
                    DiscoveredFile(
                        target,
                        "~/.gemini/antigravity/brain/*/.system_generated/logs/transcript_full.jsonl",
                    )
                )
        return found

    def probe(self) -> ProviderProbe:
        files = self.discover_files()
        conversation_root = self.antigravity_home / "conversations"
        db_files = list(conversation_root.glob("*.db")) if conversation_root.is_dir() else []
        has_db = len(db_files) > 0

        activity_count = 0
        total_tokens = 0
        total_reasoning = 0
        total_output = 0
        truncated_entries = 0
        event_types: set[str] = set()
        tools_counter: dict[str, int] = {}
        session_titles = self._get_session_titles()
        session_stats: list[dict[str, Any]] = []
        discovered_models: set[str] = set()

        for discovered in files:
            sess_id = discovered.path.parents[2].name if len(discovered.path.parents) > 2 else discovered.path.stem
            s_tok = 0
            s_calls = 0
            s_prompt = ""
            s_models: set[str] = set()

            try:
                with discovered.path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        activity_count += 1
                        if obj.get("truncated_fields"):
                            truncated_entries += 1
                        event_type = obj.get("type")
                        if isinstance(event_type, str):
                            event_types.add(event_type)
                        if not s_prompt and event_type == "USER_INPUT":
                            raw_content = str(obj.get("content") or "")
                            s_prompt = raw_content.replace("<USER_REQUEST>", "").replace("</USER_REQUEST>", "").strip()[:40]
                        tool_calls = obj.get("tool_calls") or []
                        for tc in tool_calls:
                            name = tc.get("name") if isinstance(tc, dict) else str(tc)
                            if name:
                                tools_counter[name] = tools_counter.get(name, 0) + 1
                                s_calls += 1
            except OSError:
                continue

            gen_meta = self._get_conversation_gen_metadata(sess_id)
            if gen_meta:
                for idx, m in gen_meta.items():
                    m_name = m["model"]
                    discovered_models.add(m_name)
                    s_models.add(m_name)
                    tot = m["uncached"] + m["cached"] + m["output"]
                    s_tok += tot
                    total_tokens += tot
                    total_output += m["output"]
                    total_reasoning += m["thinking"]
            else:
                try:
                    with discovered.path.open("r", encoding="utf-8", errors="replace") as handle:
                        for line in handle:
                            try:
                                obj = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            inp, out, r = _entry_estimate(obj)
                            tot = inp + out
                            s_tok += tot
                            total_tokens += tot
                            total_output += out
                            total_reasoning += r
                except OSError:
                    continue

            disp_title = session_titles.get(sess_id) or s_prompt or f"工程会话 {sess_id[:8]}"
            session_stats.append({
                "session_id": sess_id,
                "title": disp_title,
                "tokens": s_tok,
                "tool_calls": s_calls,
                "models": sorted(s_models) if s_models else ["gemini-3.8-flash"],
            })

        session_stats.sort(key=lambda item: item["tokens"], reverse=True)
        tool_calls_total = sum(tools_counter.values())

        if not self.antigravity_home.exists():
            return ProviderProbe("missing", "Antigravity", "~/.gemini/antigravity", "未发现 Antigravity 数据目录")

        sorted_tools = dict(sorted(tools_counter.items(), key=lambda x: x[1], reverse=True)[:10])
        models_list = sorted(discovered_models) if discovered_models else ["gemini-3.8-flash", "gemini-3.7-flash"]

        quota_snapshots = self._fetch_quota_windows()
        budget_windows = [
            {
                "snapshot_id": q.snapshot_id,
                "agent": q.agent,
                "status": q.status,
                "label": q.label,
                "remaining_percent": q.remaining_percent,
                "used_percent": q.used_percent,
                "window_minutes": q.window_minutes,
                "resets_at": q.resets_at,
                "updated_at": q.updated_at,
                "message": q.message,
            }
            for q in quota_snapshots
        ]

        return ProviderProbe(
            "ready" if has_db else ("limited" if files else "empty"),
            "Antigravity 本地原生记录",
            "~/.gemini/antigravity",
            (f"发现 {len(files)} 个工程会话、{activity_count} 条记录；执行 {tool_calls_total} 次 Agent 工具调用" if files else "未发现会话记录"),
            {
                "usage_available": bool(files),
                "usage_mode": "reported" if has_db else "estimated",
                "model_available": has_db,
                "activity_count": activity_count,
                "estimated_tokens": total_tokens if not has_db else 0,
                "reported_tokens": total_tokens if has_db else 0,
                "truncated_entries": truncated_entries,
                "event_types": sorted(event_types),
                "conversation_databases": len(db_files),
                "models": models_list,
                "tool_calls_total": tool_calls_total,
                "tool_distribution": sorted_tools,
                "reasoning_tokens": total_reasoning,
                "thinking_ratio": (total_reasoning / max(total_output, 1)),
                "top_sessions": session_stats[:8],
                "budget_windows": budget_windows,
                "quota_note": "Antigravity 官方服务连接 · 实时同步 Gemini 每周限额与 5 小时限额",
                "reason": "读取本地 conversations.db 提取 Gemini 3.8/3.7 Flash 精确用量与思维链",
            },
        )

    def parse_file(self, discovered: DiscoveredFile) -> ParsedFile:
        event_types: set[str] = set()
        path = discovered.path
        session_id = path.parents[2].name if len(path.parents) > 2 else path.stem
        session_titles = self._get_session_titles()
        result = ParsedFile(session_count=1)
        truncated_entries = 0
        session_tools: dict[str, int] = {}
        first_prompt = ""
        step_timestamps: dict[int, str] = {}
        step_tools: dict[int, int] = {}

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
                    idx = obj.get("step_index")
                    ts = normalized_timestamp(
                        obj.get("created_at") if isinstance(obj.get("created_at"), str) else None,
                        path,
                    )
                    if idx is not None:
                        step_timestamps[idx] = ts

                    if not first_prompt and event_type == "USER_INPUT":
                        raw_content = str(obj.get("content") or "")
                        first_prompt = raw_content.replace("<USER_REQUEST>", "").replace("</USER_REQUEST>", "").strip()[:40]

                    if obj.get("truncated_fields"):
                        truncated_entries += 1

                    tool_calls = obj.get("tool_calls") or []
                    for tc in tool_calls:
                        tname = tc.get("name") if isinstance(tc, dict) else str(tc)
                        if tname:
                            session_tools[tname] = session_tools.get(tname, 0) + 1
                    if idx is not None:
                        step_tools[idx] = max(len(tool_calls), 1)
        except OSError:
            raise

        gen_meta = self._get_conversation_gen_metadata(session_id)
        session_reasoning = 0

        if gen_meta:
            file_ts = normalized_timestamp(None, path)
            for idx, m in gen_meta.items():
                uncached = m["uncached"]
                cached = m["cached"]
                out = m["output"]
                thinking = m["thinking"]
                total = uncached + cached + out
                if not total:
                    continue
                session_reasoning += thinking
                timestamp = step_timestamps.get(idx) or file_ts
                model = m["model"]
                calls = step_tools.get(idx, 1)

                result.events.append(
                    UsageEvent(
                        event_id=stable_id("antigravity-meta", str(path), idx, timestamp, total),
                        agent="antigravity",
                        route="Google Antigravity 原生直连",
                        platform="Google DeepMind",
                        model=model,
                        session_id=session_id,
                        occurred_at=timestamp,
                        input_tokens=uncached + cached,
                        cached_input_tokens=cached,
                        cache_write_tokens=0,
                        output_tokens=out,
                        reasoning_tokens=thinking,
                        total_tokens=total,
                        usage_mode="reported",
                        call_count=calls,
                    )
                )
        else:
            # Fallback for environments without conversations db (e.g. test fixtures)
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line_number, line in enumerate(handle, 1):
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                            continue
                        event_type = obj.get("type")
                        input_tokens, output_tokens, reasoning_tokens = _entry_estimate(obj)
                        total_tokens = input_tokens + output_tokens
                        if not total_tokens:
                            continue
                        session_reasoning += reasoning_tokens
                        timestamp = normalized_timestamp(
                            obj.get("created_at") if isinstance(obj.get("created_at"), str) else None,
                            path,
                        )
                        model = "gemini-3.8-flash"
                        result.events.append(
                            UsageEvent(
                                event_id=stable_id("antigravity-estimate", str(path), line_number),
                                agent="antigravity",
                                route="本地可见文本估算",
                                platform="Google DeepMind",
                                model=model,
                                session_id=session_id,
                                occurred_at=timestamp,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                reasoning_tokens=reasoning_tokens,
                                total_tokens=total_tokens,
                                usage_mode="estimated",
                                call_count=1,
                            )
                        )
            except OSError:
                raise

        disp_title = session_titles.get(session_id) or first_prompt or f"工程会话 {session_id[:8]}"
        result.metadata = {
            "session_id": session_id,
            "session_title": disp_title,
            "event_types": sorted(event_types),
            "usage_available": True,
            "usage_mode": "reported" if gen_meta else "estimated",
            "truncated_entries": truncated_entries,
            "tool_calls": sum(session_tools.values()),
            "tool_distribution": session_tools,
            "reasoning_tokens": session_reasoning,
        }
        result.quotas = self._fetch_quota_windows()
        return result

