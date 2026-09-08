from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .db import TokenDatabase
from .models import QuotaSnapshot
from .providers.base import ProviderAdapter
from .providers.common import stable_id


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ScanCoordinator:
    def __init__(self, database: TokenDatabase, adapters: Iterable[ProviderAdapter]):
        self.database = database
        self.adapters = list(adapters)
        self._guard = threading.Lock()
        self._status_guard = threading.Lock()
        self._status: dict[str, Any] = {
            "status": "idle",
            "progress": 0,
            "message": "等待首次扫描",
            "last_completed_at": self.database.get_meta("last_scan_completed"),
        }

    def status(self) -> dict[str, Any]:
        with self._status_guard:
            return dict(self._status)

    def _set_status(self, **values: Any) -> None:
        with self._status_guard:
            self._status.update(values)

    def start_background(self, force: bool = False) -> bool:
        if not self._guard.acquire(blocking=False):
            return False
        thread = threading.Thread(target=self._run_locked, args=(force,), daemon=True, name="token-ledger-scan")
        thread.start()
        return True

    def scan_sync(self, force: bool = False) -> dict[str, Any]:
        self._guard.acquire()
        try:
            self._run(force)
        finally:
            self._guard.release()
        return self.status()

    def _run_locked(self, force: bool) -> None:
        try:
            self._run(force)
        except Exception as error:
            import logging
            logging.exception("Token Ledger 增量扫描异常: %s", error)
            err_desc = str(error).strip() or type(error).__name__
            self._set_status(
                status="error",
                message=f"扫描异常：{type(error).__name__} ({err_desc[:60]})，请重试",
                finished_at=utc_now(),
            )
        finally:
            self._guard.release()

    def _run(self, force: bool) -> None:
        started_at = utc_now()
        self._set_status(status="scanning", progress=0, message="正在发现本地 Agent 数据…")
        discovered_by_adapter = [(adapter, adapter.discover_files()) for adapter in self.adapters]
        total_files = sum(len(files) for _, files in discovered_by_adapter)
        processed = 0
        total_changed = 0
        total_errors = 0

        for adapter, discovered_files in discovered_by_adapter:
            agent = adapter.descriptor.id
            parser_revision = str(getattr(adapter, "parser_revision", "1"))
            revision_key = f"parser_revision:{agent}"
            stored_revision = self.database.get_meta(revision_key)
            reparse_adapter = (
                (stored_revision is None and parser_revision != "1")
                or (stored_revision is not None and stored_revision != parser_revision)
            )
            known = self.database.file_signatures(agent)
            seen_ids: set[str] = set()
            adapter_errors = 0
            for discovered in discovered_files:
                file_id = stable_id(agent, str(discovered.path.resolve()).lower())
                seen_ids.add(file_id)
                try:
                    stat = discovered.path.stat()
                except OSError:
                    total_errors += 1
                    adapter_errors += 1
                    processed += 1
                    continue
                signature = (stat.st_mtime_ns, stat.st_size)
                if (
                    force
                    or reparse_adapter
                    or adapter.should_reparse(discovered)
                    or known.get(file_id) != signature
                ):
                    try:
                        parsed = adapter.parse_file(discovered)
                        self.database.replace_file(
                            file_id,
                            agent,
                            discovered.path_hint,
                            stat.st_mtime_ns,
                            stat.st_size,
                            parsed,
                            utc_now(),
                        )
                        # Some adapters derive their parse result from files
                        # that are not themselves the discovered file (for
                        # example an Antigravity conversation DB/WAL).  Let
                        # those adapters commit their dependency fingerprint
                        # only after the parsed result has been persisted.
                        mark_parsed = getattr(adapter, "mark_parsed", None)
                        if callable(mark_parsed):
                            try:
                                mark_parsed(discovered)
                            except OSError:
                                # A fingerprinting failure is non-fatal; the
                                # next scan should simply parse again.
                                pass
                        total_changed += 1
                    except (OSError, PermissionError, ValueError) as error:
                        total_errors += 1
                        adapter_errors += 1
                        self.database.mark_file_error(
                            file_id,
                            agent,
                            discovered.path_hint,
                            stat.st_mtime_ns,
                            stat.st_size,
                            utc_now(),
                            type(error).__name__,
                        )
                processed += 1
                progress = round((processed / max(total_files, 1)) * 100)
                self._set_status(
                    progress=progress,
                    message=f"正在索引 {adapter.descriptor.name} · {processed}/{total_files}",
                )
            self.database.remove_missing_files(agent, seen_ids)
            probe = adapter.probe()
            self.database.update_provider(agent, probe, utc_now())
            if isinstance(probe.metadata, dict) and isinstance(probe.metadata.get("budget_windows"), list):
                def _safe_float(val: Any) -> float | None:
                    try:
                        return float(val) if val is not None else None
                    except (TypeError, ValueError):
                        return None

                def _safe_int(val: Any) -> int | None:
                    try:
                        return int(val) if val is not None else None
                    except (TypeError, ValueError):
                        return None

                snapshots = [
                    QuotaSnapshot(
                        snapshot_id=str(w.get("snapshot_id") or f"{agent}:{w.get('label')}"),
                        agent=agent,
                        label=str(w.get("label", "")),
                        status=str(w.get("status", "unavailable")),
                        remaining_percent=_safe_float(w.get("remaining_percent")),
                        used_percent=_safe_float(w.get("used_percent")),
                        window_minutes=_safe_int(w.get("window_minutes")),
                        resets_at=w.get("resets_at"),
                        updated_at=str(w.get("updated_at") or utc_now()),
                        message=str(w.get("message", "")),
                    )
                    for w in probe.metadata["budget_windows"]
                    if isinstance(w, dict)
                ]
                self.database.save_quotas(snapshots, replace_agent=agent)
            if not adapter_errors:
                self.database.set_meta(revision_key, parser_revision)

        completed_at = utc_now()
        self.database.set_meta("last_scan_completed", completed_at)
        message = f"索引完成：更新 {total_changed} 个文件"
        if total_errors:
            message += f"，{total_errors} 个文件暂时不可读"
        self._set_status(
            status="ready" if not total_errors else "partial",
            progress=100,
            message=message,
            started_at=started_at,
            last_completed_at=completed_at,
        )
