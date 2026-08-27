"""Share ledger — the local record of every live presigned share.

A presigned URL is self-contained: once minted, S3 honours it until it
expires and there is nothing to revoke server-side. What the Access section
can honestly show is therefore a LEDGER: what was shared, when it stops
working, and (approximately) with whom — written at mint time, pruned as
entries expire. "Forget" removes the record; it does not (cannot) kill the
link early, and the UI copy says so.

The ledger stores metadata only — never the URL itself. The URL embeds a
signature that IS the access grant; persisting it would turn the app data
dir into a credential store. It is returned once, to the human who asked.

Storage: ``<app data dir>/shares.json``, atomic-write + sidecar lock, same
pattern as the deploy pending store.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write
from kiro_crew.platform_compat import file_lock

logger = logging.getLogger(__name__)

APP_NAME = "aws-control"
_MAX_SHARES = 200
_NOTE_MAX = 120


def _store_path() -> Path:
    return app_data_dir(APP_NAME) / "shares.json"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _load() -> list[dict[str, Any]]:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []


def _save(entries: list[dict[str, Any]]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(entries, indent=1))


def _prune(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop entries whose link is already dead."""
    now = _now()
    alive: list[dict[str, Any]] = []
    for entry in entries:
        try:
            expires = dt.datetime.fromisoformat(entry["expiresAt"])
        except (KeyError, ValueError, TypeError):
            continue
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=dt.timezone.utc)
        if expires > now:
            alive.append(entry)
    return alive


def record_share(
    *, account: str, section: str, key: str, expires_secs: int, note: str = ""
) -> dict[str, Any]:
    """Append one share record (called at mint time). Returns the record."""
    entry = {
        "id": str(uuid.uuid4()),
        "account": account,
        "section": section,
        "key": key,
        "createdAt": _now().isoformat(timespec="seconds"),
        "expiresAt": (_now() + dt.timedelta(seconds=expires_secs)).isoformat(timespec="seconds"),
        "note": note[:_NOTE_MAX],
    }
    lock_path = _store_path().with_suffix(".lock")
    _store_path().parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fd:
        with file_lock(fd.fileno(), exclusive=True, required=True):
            entries = _prune(_load())
            entries.append(entry)
            _save(entries[-_MAX_SHARES:])
    return entry


def list_shares(account: str = "") -> list[dict[str, Any]]:
    """Live shares, newest first; optionally scoped to one account."""
    entries = _prune(_load())
    if account:
        entries = [e for e in entries if e.get("account") == account]
    return sorted(entries, key=lambda e: e.get("createdAt", ""), reverse=True)


def forget_share(share_id: str) -> Optional[dict[str, Any]]:
    """Remove one record from the ledger (the link itself lives to expiry)."""
    lock_path = _store_path().with_suffix(".lock")
    _store_path().parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as fd:
        with file_lock(fd.fileno(), exclusive=True, required=True):
            entries = _prune(_load())
            kept = [e for e in entries if e.get("id") != share_id]
            removed = next((e for e in entries if e.get("id") == share_id), None)
            _save(kept)
    return removed
