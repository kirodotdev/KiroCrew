"""Gateway-owned provenance for monitor access to owner provider credentials.

The AutoNudge store is intentionally agent-writable, so its persisted
``creation_surface`` field can describe a grant but cannot authorize one. This
record lives under the sandbox-hidden ``.vault/`` subtree and binds an active
grant to the monitor id, owning slot, provider kind, and canonical target.

Creation uses a two-step pending/active protocol: the authorizer writes the
pending identity before the agent-writable monitor row exists, then activates
it only after the row commits. A pending entry never authorizes a probe.
Revocation first writes a separate protected tombstone, then removes the grant;
the tombstone remains authoritative across cleanup failures and restarts. Reads
fail closed on every malformed or unavailable state.

Blocking file I/O throughout; async callers offload with ``asyncio.to_thread``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from pathlib import Path
from typing import Any, Iterator

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

MONITOR_OWNER_CREDENTIALS_RECORD_NAME = "autonudge-monitor-owner-credentials.json"
MONITOR_OWNER_CREDENTIALS_REVOCATIONS_NAME = "autonudge-monitor-owner-credential-revocations.json"
_LOCK_NAME = MONITOR_OWNER_CREDENTIALS_RECORD_NAME + ".lock"
_PENDING_REVOCATIONS: set[str] = set()
_PENDING_REVOCATIONS_LOCK = threading.Lock()


@contextlib.contextmanager
def _record_lock() -> Iterator[None]:
    path = monitor_owner_credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / _LOCK_NAME, "a+", encoding="utf-8") as handle:
        with platform_compat.file_lock(handle.fileno(), exclusive=True):
            yield


def monitor_owner_credentials_path() -> Path:
    """Absolute path of the protected monitor-provenance record."""
    return data_home() / ".vault" / MONITOR_OWNER_CREDENTIALS_RECORD_NAME


def monitor_owner_credentials_revocations_path() -> Path:
    """Absolute path of the protected durable revocation record."""
    return data_home() / ".vault" / MONITOR_OWNER_CREDENTIALS_REVOCATIONS_NAME


def _read_record(*, strict: bool = False) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(monitor_owner_credentials_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError:
        if strict:
            raise
        logger.warning("monitor credential provenance unreadable; treating as empty", exc_info=True)
        return {}
    except ValueError as exc:
        if strict:
            raise OSError("monitor credential provenance record is invalid") from exc
        logger.warning("monitor credential provenance unreadable; treating as empty", exc_info=True)
        return {}
    entries = raw.get("monitors") if isinstance(raw, dict) else None
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(entries, dict):
        if strict:
            raise OSError("monitor credential provenance record is invalid")
        return {}
    valid_entries = {
        str(monitor_id): entry
        for monitor_id, entry in entries.items()
        if isinstance(entry, dict)
        and all(isinstance(entry.get(field), str) for field in ("slot_key", "kind", "target"))
        and isinstance(entry.get("active"), bool)
    }
    if strict and len(valid_entries) != len(entries):
        raise OSError("monitor credential provenance record is invalid")
    return valid_entries


def _read_revocations(*, strict: bool = False) -> set[str] | None:
    try:
        raw = json.loads(monitor_owner_credentials_revocations_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, ValueError) as exc:
        if strict:
            raise OSError("monitor credential revocation record is invalid") from exc
        logger.warning("monitor credential revocations unreadable; denying grants", exc_info=True)
        return None
    monitor_ids = raw.get("monitor_ids") if isinstance(raw, dict) else None
    if (
        not isinstance(raw, dict)
        or raw.get("version") != 1
        or not isinstance(monitor_ids, list)
        or any(not isinstance(monitor_id, str) for monitor_id in monitor_ids)
    ):
        if strict:
            raise OSError("monitor credential revocation record is invalid")
        logger.warning("monitor credential revocations invalid; denying grants")
        return None
    return set(monitor_ids)


def _read_revocations_strict() -> set[str]:
    monitor_ids = _read_revocations(strict=True)
    if monitor_ids is None:
        raise OSError("monitor credential revocation record is invalid")
    return monitor_ids


def _write_record(entries: dict[str, dict[str, Any]]) -> None:
    path = monitor_owner_credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        platform_compat.restrict_dir_to_owner(path.parent)
    except OSError:
        logger.debug("could not tighten mode on %s", path.parent, exc_info=True)
    atomic_write(
        path,
        json.dumps({"version": 1, "monitors": entries}, ensure_ascii=False, sort_keys=True),
        fsync=True,
    )


def _write_revocations(monitor_ids: set[str]) -> None:
    path = monitor_owner_credentials_revocations_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        platform_compat.restrict_dir_to_owner(path.parent)
    except OSError:
        logger.debug("could not tighten mode on %s", path.parent, exc_info=True)
    atomic_write(
        path,
        json.dumps(
            {"version": 1, "monitor_ids": sorted(monitor_ids)},
            ensure_ascii=False,
            sort_keys=True,
        ),
        fsync=True,
    )


def _entry(slot_key: str, kind: str, target: str, *, active: bool) -> dict[str, Any]:
    return {
        "slot_key": str(slot_key),
        "kind": str(kind),
        "target": str(target),
        "active": active,
    }


def prepare_monitor_owner_credentials(
    monitor_id: str,
    slot_key: str,
    kind: str,
    target: str,
) -> None:
    """Write a non-authorizing identity before the monitor row commits."""
    monitor_id = str(monitor_id)
    with _record_lock():
        revocations = _read_revocations_strict()
        entries = _read_record(strict=True)
        entries[monitor_id] = _entry(slot_key, kind, target, active=False)
        _write_record(entries)
        if monitor_id in revocations:
            revocations.remove(monitor_id)
            _write_revocations(revocations)
    _clear_pending_revocation(monitor_id)


def activate_monitor_owner_credentials(monitor_id: str) -> None:
    """Activate an existing prepared identity, or fail closed."""
    monitor_id = str(monitor_id)
    with _record_lock():
        revocations = _read_revocations_strict()
        if monitor_id in revocations:
            raise OSError("prepared monitor credential provenance is revoked")
        entries = _read_record(strict=True)
        entry = entries.get(monitor_id)
        if entry is None:
            raise OSError("prepared monitor credential provenance is unavailable")
        entry["active"] = True
        _write_record(entries)


def record_monitor_owner_credentials(
    monitor_id: str,
    slot_key: str,
    kind: str,
    target: str,
) -> None:
    """Replace one active grant after an authenticated monitor update commits."""
    monitor_id = str(monitor_id)
    with _record_lock():
        revocations = _read_revocations_strict()
        entries = _read_record(strict=True)
        entries[monitor_id] = _entry(slot_key, kind, target, active=True)
        _write_record(entries)
        if monitor_id in revocations:
            revocations.remove(monitor_id)
            _write_revocations(revocations)
    _clear_pending_revocation(monitor_id)


def forget_monitor_owner_credentials(monitor_id: str) -> None:
    """Deny one monitor immediately and durably before cleaning up its grant."""
    monitor_id = str(monitor_id)
    with _PENDING_REVOCATIONS_LOCK:
        _PENDING_REVOCATIONS.add(monitor_id)
    try:
        _persist_pending_revocation(monitor_id)
    except OSError:
        revocations = _read_revocations()
        if revocations is not None and monitor_id in revocations:
            logger.warning(
                "monitor credential grant cleanup is pending behind a durable revocation",
                exc_info=True,
            )
            return
        logger.warning("could not durably revoke monitor credential provenance", exc_info=True)
        raise


def _clear_pending_revocation(monitor_id: str) -> None:
    with _PENDING_REVOCATIONS_LOCK:
        _PENDING_REVOCATIONS.discard(str(monitor_id))


def _revocation_is_pending(monitor_id: str) -> bool:
    with _PENDING_REVOCATIONS_LOCK:
        return str(monitor_id) in _PENDING_REVOCATIONS


def _persist_pending_revocation(monitor_id: str) -> None:
    with _record_lock():
        entries = _read_record(strict=True)
        revocations = _read_revocations_strict()
        if monitor_id not in entries:
            if monitor_id in revocations:
                revocations.remove(monitor_id)
                _write_revocations(revocations)
            _clear_pending_revocation(monitor_id)
            return
        if monitor_id not in revocations:
            revocations.add(monitor_id)
            _write_revocations(revocations)
        del entries[monitor_id]
        _write_record(entries)
        revocations.remove(monitor_id)
        _write_revocations(revocations)
    _clear_pending_revocation(monitor_id)


def is_monitor_owner_credentials_recorded(
    monitor_id: str,
    slot_key: str,
    kind: str,
    target: str,
) -> bool:
    """Whether the protected record authorizes this exact monitor identity."""
    monitor_id = str(monitor_id)
    if _revocation_is_pending(monitor_id):
        try:
            _persist_pending_revocation(monitor_id)
        except OSError:
            logger.warning("monitor credential revocation is still pending", exc_info=True)
        return False
    revocations = _read_revocations()
    if revocations is None or monitor_id in revocations:
        return False
    entry = _read_record().get(monitor_id)
    return bool(
        entry is not None
        and entry.get("active") is True
        and entry.get("slot_key") == str(slot_key)
        and entry.get("kind") == str(kind)
        and entry.get("target") == str(target)
    )
