"""Pending deploy confirmations store.

Persists preview payloads from MCP tool callers so they can be confirmed
via the dashboard UI (Artifact Deploy page) by a cookie-authenticated human.

Storage: ``~/.kiro/crew/deploy/pending-deploys.json`` — same atomic-write
pattern as profiles.py registry.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import read_bytes_with_retry, replace_with_retry
from kiro_crew.config.paths import config_dir
from kiro_crew.platform_compat import file_lock, open_lock_file

logger = logging.getLogger(__name__)

_MAX_PENDING = 20
_EXPIRY_SECONDS = 3600  # 1 hour


def _store_path() -> Path:
    return config_dir() / "deploy" / "pending-deploys.json"


class PendingStoreUnreadable(Exception):
    """The pending store cannot be read or set aside, so a write would replace it."""


def _load_raw() -> list[dict[str, Any]]:
    """The tolerant read for :func:`list_pending` and :func:`claim_pending`.

    A present file that cannot be read or parsed reads as no entries. That is safe
    for a reader and for a claim, which saves only on a hit. One leading UTF-8
    byte-order mark is not content and reads normally.
    """
    p = _store_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        if isinstance(data, list):
            return data
    except (OSError, ValueError, RecursionError):
        pass
    return []


def _load_for_update() -> list[dict[str, Any]]:
    """The read :func:`add_pending` and :func:`remove_pending` save from.

    Both save whatever they loaded, so the empty answer :func:`_load_raw` gives a
    present-but-unreadable file would be written over every entry still in it. Here
    a file that cannot be decoded or parsed as a list is moved aside to
    ``<name>.corrupt-<utc>`` with a warning first, and the write starts from an
    empty store with the old bytes kept. When the read itself fails, or the move
    does, the write raises :class:`PendingStoreUnreadable` and the file stays as it is.
    """
    p = _store_path()
    try:
        data = json.loads(read_bytes_with_retry(p).decode("utf-8-sig"))
    except FileNotFoundError:
        return []
    except OSError as e:
        # A read that fails says nothing about the bytes, which may be a good store:
        # moving it aside would hide every entry, so the write is refused instead.
        raise PendingStoreUnreadable(f"{p} could not be read ({e}); not saving over it") from e
    except (ValueError, RecursionError) as e:
        # RecursionError: JSON nested past the interpreter's limit, a hand-edited file.
        _set_aside(p, e)
        return []
    if not isinstance(data, list):
        _set_aside(p, f"a JSON {type(data).__name__}, not a list")
        return []
    return data


def _set_aside(p: Path, reason: object) -> None:
    """Move an unreadable store to ``<name>.corrupt-<utc>`` so a save cannot destroy it."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    aside = p.with_name(f"{p.name}.corrupt-{stamp}")
    try:
        replace_with_retry(p, aside)
    except OSError as e:
        raise PendingStoreUnreadable(
            f"{p} could not be read ({reason}) or moved aside ({e}); not saving over it"
        ) from e
    logger.warning(
        "pending deploy store %s could not be read (%s); moved it to %s before saving",
        p,
        reason,
        aside.name,
    )


def _save_raw(entries: list[dict[str, Any]]) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", dir=str(p.parent), suffix=".tmp", delete=False, encoding="utf-8"
    )
    try:
        tmp.write(json.dumps(entries, indent=2))
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        # `replace_with_retry`, not a bare `os.replace`: on Windows the rename
        # raises `PermissionError` while any other handle is open on either
        # path -- an indexer, an AV scanner, or a concurrent reader of this
        # store -- and that transient is the only thing standing between a
        # fully-durable write (fsync'd temp, above) and the entry landing.
        # Every writer here funnels through this one call, so a faulted rename
        # aborts whichever of add/claim/remove was in flight: a preview that
        # cannot record its pending confirmation, a human confirm whose atomic
        # claim cannot be persisted, or a dismiss that does not take.
        #
        # The retry only helps where it is allowed to sleep, and it is: the
        # dashboard drives every one of these through `asyncio.to_thread`
        # (`deploy/handlers.py`), so the helper's off-the-event-loop gate leaves
        # it enabled on that path.
        replace_with_retry(tmp.name, p)
    except BaseException:
        tmp.close()
        with contextlib.suppress(OSError):
            os.unlink(tmp.name)
        raise


def expires_at_epoch(created_at_epoch: float) -> float:
    """The instant a pending entry made at ``created_at_epoch`` stops being confirmable."""
    return created_at_epoch + _EXPIRY_SECONDS


def _prune_expired(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = time.time()
    return [e for e in entries if now < expires_at_epoch(e.get("created_at_epoch", 0))]


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
    # open_lock_file opens writable and never truncates; its docstring carries
    # the Windows rationale.
    with open_lock_file(lock_path) as fd:
        with file_lock(fd, exclusive=True, required=True):
            entries = _prune_expired(_load_for_update())
            entries.append(entry)
            # Cap at _MAX_PENDING, drop oldest
            if len(entries) > _MAX_PENDING:
                entries = entries[-_MAX_PENDING:]
            _save_raw(entries)
    return entry


def list_pending() -> list[dict[str, Any]]:
    """Return non-expired pending entries, each with a derived (never stored) ``expires_at_epoch``."""
    return [
        {**e, "expires_at_epoch": expires_at_epoch(e.get("created_at_epoch", 0))}
        for e in _prune_expired(_load_raw())
    ]


def remove_pending(entry_id: str) -> bool:
    """Remove an entry (confirm or dismiss). Returns True if found."""
    lock_path = _store_path().with_suffix(".lock")
    _store_path().parent.mkdir(parents=True, exist_ok=True)
    # open_lock_file: writable but non-truncating; see add_pending above.
    with open_lock_file(lock_path) as fd:
        with file_lock(fd, exclusive=True, required=True):
            entries = _prune_expired(_load_for_update())
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
    # open_lock_file: writable but non-truncating; see add_pending above.
    with open_lock_file(lock_path) as fd:
        with file_lock(fd, exclusive=True, required=True):
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
