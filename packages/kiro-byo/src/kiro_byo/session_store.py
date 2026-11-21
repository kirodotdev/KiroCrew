"""Durable ACP session snapshots and migration support.

Only durable snapshot I/O belongs here. The ACP server retains responsibility
for protocol replay, session admission and resource lifetime. The two I/O hooks
keep existing test injection and host location behavior stable.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .acp_session_state import Session
from .errors import SessionBusyError
from .session_events import (
    SESSION_EVENT_SCHEMA_VERSION, append_event, bootstrap_events, derive_route_pins,
    derive_session_projection, ensure_event_projection,
    ensure_route_pin_projection, validate_events,
)
from .state_io import _file_transaction_lock


class SessionStoreMixin:
    """Revision-fenced storage methods for the server's live Session objects."""

    @staticmethod
    def _session_storage_home() -> Path:
        raise NotImplementedError

    @staticmethod
    def _session_storage_write(path: Path, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    @classmethod
    def _session_store_dir(cls) -> Path:
        return cls._session_storage_home() / "data" / "sessions" / "kiro-byo"

    @staticmethod
    def _safe_session_id(session_id: str) -> str:
        value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(session_id or "")).strip("._")
        return value or "invalid"

    @classmethod
    def _legacy_session_path(cls, session_id: str) -> Path:
        """0.1.0 pre-audit path kept only for backward-compatible reads/deletes."""
        return cls._session_store_dir() / f"{cls._safe_session_id(session_id)}.json"

    @classmethod
    def _session_path(cls, session_id: str) -> Path:
        # Sanitizing alone is not injective (``a/b`` and ``a?b`` both become
        # ``a_b``). Keep a readable prefix but bind the filename to the exact
        # ACP session id so independent sessions can never overwrite each other.
        raw = str(session_id or "")
        prefix = cls._safe_session_id(raw)[:80]
        digest = hashlib.sha256(raw.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
        return cls._session_store_dir() / f"{prefix}-{digest}.json"

    @classmethod
    def _session_revision_on_disk(cls, path: Path, session_id: str) -> int | None:
        """Read the currently committed generation; invalid records fail closed."""
        try:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                # Schema-v1/v2/pre-hash records live under a sanitized name.
                # Treat their implicit revision as 0 when migrating to the
                # hashed filename; an existing hashed copy always wins.
                legacy = cls._legacy_session_path(session_id)
                try:
                    raw = json.loads(legacy.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    return None
        except (OSError, ValueError) as exc:
            raise SessionBusyError("session record is unavailable or corrupt") from exc
        if not isinstance(raw, dict) or raw.get("session_id") != session_id:
            raise SessionBusyError("session record identity mismatch")
        revision = raw.get("storage_revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise SessionBusyError("session record revision is invalid")
        return revision

    @classmethod
    def _assert_session_current(cls, session: Session) -> None:
        """Admission check before an ACP turn, not a remote exactly-once claim."""
        path = cls._session_path(session.session_id)
        with _file_transaction_lock(path):
            actual = cls._session_revision_on_disk(path, session.session_id)
            if session.storage_loaded:
                if actual != session.storage_revision:
                    raise SessionBusyError("session snapshot is stale; reload in a fresh ACP session")
            elif actual is not None:
                raise SessionBusyError("session already exists; refusing to replace its history")

    def _persist_session(self, session: Session) -> bool:
        """Persist only if no other ACP writer has advanced this session.

        File locking serializes read-check-write across processes. The revision
        rejects stale whole-snapshot overwrites, including lost tool events.
        Live-turn execution exclusivity is a separate requirement.
        """
        try:
            path = self._session_path(session.session_id)
            with session.state_lock:
                with _file_transaction_lock(path):
                    actual = self._session_revision_on_disk(path, session.session_id)
                    if session.storage_loaded:
                        if actual != session.storage_revision:
                            raise SessionBusyError(
                                "session snapshot is stale; reload in a fresh ACP session"
                            )
                    elif actual is not None:
                        raise SessionBusyError("session already exists; refusing to overwrite its history")
                    updated_at = time.time()
                    ensure_event_projection(session, reason="persist_sync")
                    ensure_route_pin_projection(session, reason="persist_sync")
                    revision = (actual or 0) + 1
                    payload = {
                        "schema_version": 3,
                        "storage_revision": revision,
                        "session_event_schema_version": SESSION_EVENT_SCHEMA_VERSION,
                        "session_id": session.session_id,
                        "cwd": session.cwd,
                        "last_additional_directories": list(session.additional_directories),
                        "model": session.model,
                        "selection_kind": str(session.selection_kind or "unknown"),
                        "selection_source": str(session.selection_source or ""),
                        "selection_requested": str(session.selection_requested or ""),
                        "effort": session.effort,
                        "thinking_mode": session.thinking_mode,
                        "effort_user_set": bool(session.effort_user_set),
                        "thinking_user_set": bool(session.thinking_user_set),
                        "agent": {
                            "id": session.agent.agent_id,
                            "prompt": session.agent.prompt,
                            "tools": list(session.agent.tools),
                            "tools_declared": session.agent.tools_declared,
                            "excludedTools": list(session.agent.excluded_tools),
                        },
                        "events": session.events,
                        "loaded_tool_names": list(session.loaded_tool_names),
                        "created_at": session.created_at,
                        "updated_at": updated_at,
                    }
                    self._session_storage_write(path, payload)
                    session.updated_at = updated_at
                    session.storage_revision = revision
                    session.storage_loaded = True
                    return True
        except (OSError, TimeoutError) as exc:
            # Callers that are about to dispatch models/tools MUST check False.
            # A stale/conflicting snapshot is raised instead of being ignored.
            self.trace(f"session persistence failed session={session.session_id}: {exc}")
            return False

    def _load_persisted_record(self, session_id: str) -> dict[str, Any] | None:
        payload = None
        for path in (self._session_path(session_id), self._legacy_session_path(session_id)):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                break
            except (OSError, json.JSONDecodeError):
                continue
        if payload is None:
            return None
        if not isinstance(payload, dict):
            return None
        schema = int(payload.get("schema_version") or 0)
        if schema not in {1, 2, 3}:
            return None
        if str(payload.get("session_id") or "") != session_id:
            return None
        if schema in {2, 3}:
            events = payload.get("events")
            if not validate_events(events):
                return None
        else:
            messages = payload.get("messages")
            if not isinstance(messages, list):
                return None
            events = bootstrap_events(row for row in messages if isinstance(row, dict))
            payload["events"] = events

        # 0.1.2 schema-v2 stored sticky pins beside the event log. Fold that
        # legacy side state into semantic events in memory; the next save writes
        # schema-v3 with events as the only durable routing truth.
        legacy_pins = payload.get("route_pins")
        if schema < 3 and isinstance(legacy_pins, dict):
            projected = derive_route_pins(events)
            for key, value in legacy_pins.items():
                if (
                    isinstance(key, str)
                    and isinstance(value, (list, tuple))
                    and len(value) == 2
                    and all(isinstance(item, str) and item for item in value)
                ):
                    pin = (str(value[0]), str(value[1]))
                    if projected.get(key) != pin:
                        append_event(
                            events,
                            "routing/pin",
                            {
                                "key": key,
                                "provider_id": pin[0],
                                "model_id": pin[1],
                                "reason": "schema_v2_migration",
                            },
                        )
                        projected[key] = pin

        projection = derive_session_projection(events)
        payload["messages"] = projection.messages
        payload["route_pins"] = projection.route_pins
        return payload

    @classmethod
    def _persisted_session_records(cls) -> list[dict[str, Any]]:
        store = cls._session_store_dir()
        if not store.is_dir():
            return []
        records: dict[str, dict[str, Any]] = {}
        for path in store.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) not in {1, 2, 3}:
                continue
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                continue
            # The hashed record wins over its old sanitized-name predecessor
            # independent of glob enumeration order.
            current_path = cls._session_path(session_id)
            if session_id not in records or path == current_path:
                records[session_id] = payload
        return list(records.values())

    @staticmethod
    def _session_info(record: dict[str, Any]) -> dict[str, Any] | None:
        session_id = str(record.get("session_id") or "").strip()
        cwd = str(record.get("cwd") or "").strip()
        if not session_id or not cwd or not Path(cwd).is_absolute():
            # Pre-0.9.10 records may not persist cwd. Do not fabricate an invalid ACP
            # SessionInfo for them; they remain directly loadable by id.
            return None
        updated = float(record.get("updated_at") or 0.0)
        result: dict[str, Any] = {"sessionId": session_id, "cwd": cwd}
        if updated > 0:
            result["updatedAt"] = datetime.fromtimestamp(updated, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        return result
