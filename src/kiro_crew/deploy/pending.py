"""Pending deploy confirmations store.

Persists preview payloads from MCP tool callers so they can be confirmed
via the dashboard UI (Artifact Deploy page) by a cookie-authenticated human.

Storage: ``~/.kiro/crew/deploy/pending-deploys.json`` — same atomic-write
pattern as profiles.py registry.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.platform_compat import file_lock

logger = logging.getLogger(__name__)

_MAX_PENDING = 20
_EXPIRY_SECONDS = 3600  # 1 hour


def _store_path() -> Path:
    return config_dir() / "deploy" / "pending-deploys.json"


def _load_raw() -> list[dict[str, Any]]:
    p = _store_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _save_raw(entries: list[dict[str, Any]]) -> None:
    p = _store_path()
    # mode=0o600 is NOT optional. NamedTemporaryFile creates its file owner-only
    # and this function never widened it, so the pending store is 0o600 today.
    # Omitting the mode would publish it at the umask default (0o644) instead.
    atomic_write(p, json.dumps(entries, indent=2), fsync=True, mode=0o600)


def _prune_expired(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = time.time()
    return [e for e in entries if now - e.get("created_at_epoch", 0) < _EXPIRY_SECONDS]


def add_pending(params: dict[str, Any]) -> dict[str, Any]:
    """Persist a pending confirmation entry. Returns the entry with its id."""
    entry = {
        "id": params.get("id") or str(uuid.uuid4()),
        "site_id": params.get("site_id", ""),
        "artifact_slug": params.get("artifact_slug", ""),
        "local_dir": params.get("local_dir", ""),
        "profile": params.get("profile", ""),
        "region": params.get("region", ""),
        "ttl_hours": params.get("ttl_hours", 72),
        "scan_summary": params.get("scan_summary", "clean"),
        "content_digest": params.get("content_digest", ""),
        # Preview was scan-blocked by OVERRIDABLE (non-credential)
        # findings — confirming requires an explicit human override action.
        "override_scan_required": bool(params.get("override_scan_required", False)),
        "created_at_epoch": params.get("created_at_epoch") or time.time(),
    }
    lock_path = _store_path().with_suffix(".lock")
    _store_path().parent.mkdir(parents=True, exist_ok=True)
    # required=True: a deploy store that cannot obtain cross-process exclusion
    # must fail loudly rather than risk a double-deploy / lost write. flock_compat
    # is a Windows no-op, so this uses platform_compat's real msvcrt lock.
    with open(lock_path, "w") as fd:
        with file_lock(fd.fileno(), exclusive=True, required=True):
            entries = _prune_expired(_load_raw())
            entries.append(entry)
            # Cap at _MAX_PENDING, drop oldest
            if len(entries) > _MAX_PENDING:
                entries = entries[-_MAX_PENDING:]
            _save_raw(entries)
    return entry


def list_pending() -> list[dict[str, Any]]:
    """Return non-expired pending entries."""
    entries = _prune_expired(_load_raw())
    return entries


def get_pending(entry_id: str) -> dict[str, Any] | None:
    """Get a single pending entry by id, or None if expired/missing."""
    for e in _prune_expired(_load_raw()):
        if e.get("id") == entry_id:
            return e
    return None


def remove_pending(entry_id: str) -> bool:
    """Remove an entry (confirm or dismiss). Returns True if found."""
    lock_path = _store_path().with_suffix(".lock")
    _store_path().parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fd:
        with file_lock(fd.fileno(), exclusive=True, required=True):
            entries = _prune_expired(_load_raw())
            before = len(entries)
            entries = [e for e in entries if e.get("id") != entry_id]
            _save_raw(entries)
            return len(entries) < before


def claim_pending(entry_id: str) -> dict[str, Any] | None:
    """Atomically claim (remove and return) a pending entry.

    Under the file lock, reads entries; if id is present, removes it and returns
    the entry. Otherwise returns None. This prevents double-deploy from
    concurrent confirms.
    """
    lock_path = _store_path().with_suffix(".lock")
    _store_path().parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fd:
        with file_lock(fd.fileno(), exclusive=True, required=True):
            entries = _prune_expired(_load_raw())
            claimed = None
            remaining = []
            for e in entries:
                if e.get("id") == entry_id and claimed is None:
                    claimed = e
                else:
                    remaining.append(e)
            if claimed is not None:
                _save_raw(remaining)
            return claimed
