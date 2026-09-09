from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .models import ParsedFile, ProviderProbe, QuotaSnapshot
from .providers.common import stable_id


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS file_states (
    file_id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    path_hint TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0,
    session_count INTEGER NOT NULL DEFAULT 0,
    last_scan TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_file_states_agent ON file_states(agent);

CREATE TABLE IF NOT EXISTS usage_events (
    event_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES file_states(file_id) ON DELETE CASCADE,
    agent TEXT NOT NULL,
    route TEXT NOT NULL,
    platform TEXT NOT NULL,
    model TEXT NOT NULL,
    session_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    usage_mode TEXT NOT NULL DEFAULT 'reported',
    usage_scope TEXT NOT NULL DEFAULT 'session',
    call_count INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_usage_time ON usage_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_usage_agent ON usage_events(agent);
CREATE INDEX IF NOT EXISTS idx_usage_agent_time ON usage_events(agent, occurred_at);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_events(model);

CREATE TABLE IF NOT EXISTS quota_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    label TEXT NOT NULL,
    status TEXT NOT NULL,
    remaining_percent REAL,
    used_percent REAL,
    window_minutes INTEGER,
    resets_at TEXT,
    updated_at TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS provider_states (
    agent TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    label TEXT NOT NULL,
    path_hint TEXT NOT NULL,
    file_count INTEGER NOT NULL DEFAULT 0,
    event_count INTEGER NOT NULL DEFAULT 0,
    session_count INTEGER NOT NULL DEFAULT 0,
    last_scan TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

class TokenDatabase:
    def __init__(self, path: Path):
        self.path = path
        self._internal_version: int = 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(usage_events)").fetchall()
            }
            if "usage_mode" not in columns:
                connection.execute(
                    "ALTER TABLE usage_events ADD COLUMN usage_mode TEXT NOT NULL DEFAULT 'reported'"
                )
            if "usage_scope" not in columns:
                connection.execute(
                    "ALTER TABLE usage_events ADD COLUMN usage_scope TEXT NOT NULL DEFAULT 'session'"
                )
            if "call_count" not in columns:
                connection.execute(
                    "ALTER TABLE usage_events ADD COLUMN call_count INTEGER NOT NULL DEFAULT 1"
                )
            # Discard legacy derived metadata, not usage history. Older releases
            # persisted conversation titles and fabricated quota fallbacks.
            if not connection.execute("SELECT 1 FROM app_meta WHERE key='migration:2.4.3'").fetchone():
                connection.execute("PRAGMA secure_delete=ON")
                connection.execute("DELETE FROM quota_snapshots WHERE agent IN ('claude','antigravity')")
                connection.execute("UPDATE provider_states SET metadata_json='{}' WHERE agent IN ('claude','antigravity')")
                connection.execute("INSERT INTO app_meta(key,value) VALUES('migration:2.4.3','1')")

    def data_version(self) -> tuple:
        # PRAGMA data_version cannot be compared across newly opened connections.
        signatures = []
        for path in (self.path, Path(str(self.path) + "-wal")):
            try:
                stat = path.stat()
                signatures.append((stat.st_mtime_ns, stat.st_size))
            except OSError:
                signatures.append(None)
        return tuple(signatures)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def file_signatures(self, agent: str) -> dict[str, tuple[int, int]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT file_id, mtime_ns, size_bytes FROM file_states WHERE agent = ? AND status = 'ready'",
                (agent,),
            ).fetchall()
        return {row["file_id"]: (row["mtime_ns"], row["size_bytes"]) for row in rows}

    def replace_file(
        self,
        file_id: str,
        agent: str,
        path_hint: str,
        mtime_ns: int,
        size_bytes: int,
        parsed: ParsedFile,
        scanned_at: str,
    ) -> None:
        preserve_history = agent == 'codex' and parsed.metadata.get('codex_history_v2') is True
        with self.connect() as connection:
            existing_history = []
            if preserve_history:
                existing_history = connection.execute(
                    """
                    SELECT event_id,occurred_at,input_tokens,cached_input_tokens,
                           cache_write_tokens,output_tokens,reasoning_tokens,total_tokens
                    FROM usage_events WHERE file_id=?
                    """,
                    (file_id,),
                ).fetchall()
            connection.execute(
                """
                INSERT INTO file_states(file_id,agent,path_hint,mtime_ns,size_bytes,event_count,session_count,last_scan,status,message)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(file_id) DO UPDATE SET
                  agent=excluded.agent,path_hint=excluded.path_hint,mtime_ns=excluded.mtime_ns,
                  size_bytes=excluded.size_bytes,event_count=excluded.event_count,
                  session_count=excluded.session_count,last_scan=excluded.last_scan,
                  status=excluded.status,message=excluded.message
                """,
                (
                    file_id,
                    agent,
                    path_hint,
                    mtime_ns,
                    size_bytes,
                    len(parsed.events),
                    parsed.session_count,
                    scanned_at,
                    "ready",
                    "",
                ),
            )
            if preserve_history:
                # Upgrade path: retain old observations when a source log was
                # shortened. Remove only rows matched to current events or to
                # parser-proven inherited fork signatures.
                history_rows = [
                    row for row in existing_history
                    if not (
                        str(row["event_id"]).startswith("codex-v2:c:")
                        or str(row["event_id"]).startswith("codex-v2:l:")
                    )
                ]
                legacy_candidates = [
                    row for row in history_rows
                    if not str(row["event_id"]).startswith("codex-v2:h:")
                ]
                used_history: set[str] = set()
                def event_signature(event):
                    return (
                        event.occurred_at, event.input_tokens,
                        event.cached_input_tokens, event.cache_write_tokens,
                        event.output_tokens, event.reasoning_tokens,
                        event.total_tokens,
                    )
                for event in parsed.events:
                    signature = event_signature(event)
                    for row in history_rows:
                        if row["event_id"] in used_history:
                            continue
                        if tuple(row[key] for key in (
                            "occurred_at", "input_tokens", "cached_input_tokens",
                            "cache_write_tokens", "output_tokens",
                            "reasoning_tokens", "total_tokens"
                        )) == signature:
                            used_history.add(row["event_id"])
                            break
                inherited_signatures = [
                    tuple(signature)
                    for signature in parsed.metadata.get("codex_inherited_signatures", [])
                    if isinstance(signature, (list, tuple)) and len(signature) == 6
                ]
                for signature in inherited_signatures:
                    for row in legacy_candidates:
                        if row["event_id"] in used_history:
                            continue
                        row_signature = tuple(row[key] for key in (
                            "input_tokens", "cached_input_tokens", "cache_write_tokens",
                            "output_tokens", "reasoning_tokens", "total_tokens"
                        ))
                        if row_signature == signature:
                            used_history.add(row["event_id"])
                            break
                # A source may have been shortened after the old scan, so the
                # current file may no longer contain the copied prefix. Parent
                # per-call shapes let migration remove only the old rows that
                # have direct parent evidence; all other old observations stay.
                for encoded in parsed.metadata.get("codex_parent_usage_signatures", []):
                    if not isinstance(encoded, (list, tuple)) or len(encoded) != 7:
                        continue
                    signature = tuple(encoded[:6])
                    try:
                        count = max(0, int(encoded[6]))
                    except (TypeError, ValueError):
                        continue
                    for _ in range(count):
                        for row in legacy_candidates:
                            if row["event_id"] in used_history:
                                continue
                            row_signature = tuple(row[key] for key in (
                                "input_tokens", "cached_input_tokens", "cache_write_tokens",
                                "output_tokens", "reasoning_tokens", "total_tokens"
                            ))
                            if row_signature == signature:
                                used_history.add(row["event_id"])
                                break
                connection.executemany(
                    "DELETE FROM usage_events WHERE file_id=? AND event_id=?",
                    [(file_id, event_id) for event_id in used_history],
                )
                connection.executemany('DELETE FROM usage_events WHERE file_id=? AND event_id=?',
                                       [(file_id, event_id) for event_id in parsed.metadata.get('codex_inherited_ids', [])])
                for row in history_rows:
                    if row["event_id"] in used_history:
                        continue
                    history_id = "codex-v2:h:" + stable_id(
                        file_id, row["event_id"], row["occurred_at"],
                        row["input_tokens"], row["cached_input_tokens"],
                        row["cache_write_tokens"], row["output_tokens"],
                        row["reasoning_tokens"], row["total_tokens"],
                    )
                    connection.execute(
                        "UPDATE usage_events SET event_id=? WHERE file_id=? AND event_id=?",
                        (history_id, file_id, row["event_id"]),
                    )
            else:
                connection.execute("DELETE FROM usage_events WHERE file_id = ?", (file_id,))
            connection.executemany(
                """INSERT INTO usage_events(
                  event_id,file_id,agent,route,platform,model,session_id,occurred_at,
                  input_tokens,cached_input_tokens,cache_write_tokens,output_tokens,
                  reasoning_tokens,total_tokens,usage_mode,usage_scope,call_count
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """ + (" ON CONFLICT(event_id) DO UPDATE SET file_id=excluded.file_id" if preserve_history else ""),
                [
                    (
                        event.event_id,
                        file_id,
                        event.agent,
                        event.route,
                        event.platform,
                        event.model,
                        event.session_id,
                        event.occurred_at,
                        event.input_tokens,
                        event.cached_input_tokens,
                        event.cache_write_tokens,
                        event.output_tokens,
                        event.reasoning_tokens,
                        event.total_tokens,
                        event.usage_mode,
                        event.usage_scope,
                        event.call_count,
                    )
                    for event in parsed.events
                ],
            )
            if preserve_history:
                connection.execute("UPDATE file_states SET event_count=(SELECT COUNT(*) FROM usage_events WHERE file_id=?) WHERE file_id=?", (file_id, file_id))
            for quota in parsed.quotas:
                connection.execute(
                    """
                    INSERT INTO quota_snapshots(
                      snapshot_id,agent,label,status,remaining_percent,used_percent,
                      window_minutes,resets_at,updated_at,message
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(snapshot_id) DO UPDATE SET
                      agent=excluded.agent,label=excluded.label,status=excluded.status,
                      remaining_percent=excluded.remaining_percent,used_percent=excluded.used_percent,
                      window_minutes=excluded.window_minutes,resets_at=excluded.resets_at,
                      updated_at=excluded.updated_at,message=excluded.message
                    WHERE excluded.updated_at >= quota_snapshots.updated_at
                    """,
                    (
                        quota.snapshot_id,
                        quota.agent,
                        quota.label,
                        quota.status,
                        quota.remaining_percent,
                        quota.used_percent,
                        quota.window_minutes,
                        quota.resets_at,
                        quota.updated_at,
                        quota.message,
                    ),
                )
        self._internal_version += 1

    def save_quotas(self, quotas: Sequence[QuotaSnapshot], *, replace_agent: str | None = None) -> None:
        if replace_agent is not None and any(q.agent != replace_agent for q in quotas):
            raise ValueError("Quota replacement must contain only the selected agent")
        if not quotas and replace_agent is None:
            return
        with self.connect() as connection:
            if replace_agent is not None:
                connection.execute("DELETE FROM quota_snapshots WHERE agent=?", (replace_agent,))
            for quota in quotas:
                connection.execute(
                    """
                    INSERT INTO quota_snapshots(
                      snapshot_id,agent,label,status,remaining_percent,used_percent,
                      window_minutes,resets_at,updated_at,message
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(snapshot_id) DO UPDATE SET
                      agent=excluded.agent,label=excluded.label,status=excluded.status,
                      remaining_percent=excluded.remaining_percent,used_percent=excluded.used_percent,
                      window_minutes=excluded.window_minutes,resets_at=excluded.resets_at,
                      updated_at=excluded.updated_at,message=excluded.message
                    WHERE excluded.updated_at >= quota_snapshots.updated_at
                    """,
                    (
                        quota.snapshot_id,
                        quota.agent,
                        quota.label,
                        quota.status,
                        quota.remaining_percent,
                        quota.used_percent,
                        quota.window_minutes,
                        quota.resets_at,
                        quota.updated_at,
                        quota.message,
                    ),
                )
        self._internal_version += 1

    def mark_file_error(
        self,
        file_id: str,
        agent: str,
        path_hint: str,
        mtime_ns: int,
        size_bytes: int,
        scanned_at: str,
        message: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO file_states(file_id,agent,path_hint,mtime_ns,size_bytes,last_scan,status,message)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(file_id) DO UPDATE SET last_scan=excluded.last_scan,status=excluded.status,message=excluded.message
                """,
                (file_id, agent, path_hint, mtime_ns, size_bytes, scanned_at, "error", message[:300]),
            )
        self._internal_version += 1

    def remove_missing_files(self, agent: str, seen_file_ids: set[str]) -> None:
        with self.connect() as connection:
            existing = [row[0] for row in connection.execute("SELECT file_id FROM file_states WHERE agent = ?", (agent,))]
            retained = set()
            if agent == 'codex':
                retained = {row[0] for row in connection.execute("SELECT DISTINCT file_id FROM usage_events WHERE agent='codex' AND event_id LIKE 'codex-v2:%'")}
                for file_id in retained - seen_file_ids:
                    connection.execute("UPDATE file_states SET status='retained',message='源日志暂不可见，保留已确认的历史用量' WHERE file_id=?", (file_id,))
                # A moved log owns the same event IDs, never another copy.
                connection.execute("UPDATE file_states SET event_count=(SELECT COUNT(*) FROM usage_events WHERE usage_events.file_id=file_states.file_id) WHERE agent='codex'")
            missing = [(file_id,) for file_id in existing if file_id not in seen_file_ids and file_id not in retained]
            connection.executemany("DELETE FROM file_states WHERE file_id = ?", missing)
        self._internal_version += 1

    def update_provider(self, agent: str, probe: ProviderProbe, scanned_at: str) -> None:
        with self.connect() as connection:
            counts = connection.execute(
                """
                SELECT COUNT(*) AS files, COALESCE(SUM(event_count),0) AS events,
                       COALESCE(SUM(session_count),0) AS sessions
                FROM file_states WHERE agent = ?
                """,
                (agent,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO provider_states(
                  agent,status,label,path_hint,file_count,event_count,session_count,last_scan,message,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(agent) DO UPDATE SET
                  status=excluded.status,label=excluded.label,path_hint=excluded.path_hint,
                  file_count=excluded.file_count,event_count=excluded.event_count,
                  session_count=excluded.session_count,last_scan=excluded.last_scan,
                  message=excluded.message,metadata_json=excluded.metadata_json
                """,
                (
                    agent,
                    probe.status,
                    probe.label,
                    probe.path_hint,
                    counts["files"],
                    counts["events"],
                    counts["sessions"],
                    scanned_at,
                    probe.message,
                    json.dumps(probe.metadata, ensure_ascii=False),
                ),
            )
        self._internal_version += 1

    def provider_states(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM provider_states ORDER BY agent").fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json"))
            except json.JSONDecodeError:
                item["metadata"] = {}
            output.append(item)
        return output

    def usage_rows(self, start_at: str | None = None, agent: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if start_at:
            clauses.append("occurred_at >= ?")
            params.append(start_at)
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM usage_events" + where + " ORDER BY occurred_at", params
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_quotas(self) -> dict[str, list[dict[str, Any]]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM quota_snapshots ORDER BY agent, window_minutes, updated_at DESC"
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["agent"], []).append(dict(row))
        return grouped

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO app_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        self._internal_version += 1

    def get_meta(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
