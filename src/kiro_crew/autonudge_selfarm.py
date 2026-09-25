"""AutoNudge self-arm records and process-memory scheduled-message provenance.

Self-arm records remain in the sandbox-visible ``trust/`` subtree because existing
SEL consumers require that location. Their reader is deliberately total: any
missing, malformed, or unreadable record refuses a self-arm.

Scheduled composer provenance is held only in gateway process memory. Exact text,
slot identity, the authoritative deadline, scheduling-time containment state, and
completion state never enter the agent-writable AutoNudge store or any file under
the data home. A process-local
reentrant lock makes record, read, compare-and-swap update, completion, removal,
and rollback atomic across gateway worker threads. The empty map in a new gateway
process makes persisted metadata from an earlier process definitively stale, so
startup reconciliation removes it instead of replaying it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

SELF_ARM_RECORD_NAME = "autonudge-self-armed.json"
_LOCK_SUFFIX = ".lock"
# Backward-compatible test seam for the self-arm sibling lock filename.
_LOCK_NAME = f"{SELF_ARM_RECORD_NAME}{_LOCK_SUFFIX}"
_SCHEDULED_MESSAGES_LOCK = threading.RLock()


@contextlib.contextmanager
def _record_lock(path: Path) -> Iterator[None]:
    """Exclusive lock spanning one read-modify-write transaction."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / f"{path.name}{_LOCK_SUFFIX}"
    with open(lock_path, "a+", encoding="utf-8") as fh:
        with platform_compat.file_lock(fh.fileno(), exclusive=True):
            yield


def self_arm_record_path() -> Path:
    """Absolute path of self-arm records, retained for sandboxed SEL consumers."""
    return data_home() / "trust" / SELF_ARM_RECORD_NAME


def _read_self_arm_record(path: Path) -> dict[str, dict[str, Any]]:
    """Read self-arm entries; every failure is an authorization refusal."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        logger.warning("autonudge self-arm record is unavailable; refusing", exc_info=True)
        return {}
    entries = raw.get("loops") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {
        str(loop_id): entry
        for loop_id, entry in entries.items()
        if isinstance(entry, dict) and isinstance(entry.get("slot_key"), str)
    }


def _write_self_arm_record(path: Path, entries: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        platform_compat.restrict_dir_to_owner(path.parent)
    except OSError:
        logger.debug("could not tighten mode on %s", path.parent, exc_info=True)
    atomic_write(
        path,
        json.dumps({"version": 1, "loops": entries}, ensure_ascii=False, sort_keys=True),
        fsync=True,
    )


@dataclass(frozen=True)
class ScheduledMessageProvenance:
    """Authoritative composer content held outside the agent-writable loop store."""

    slot_key: str
    message: str
    scheduled_at: float
    completed: bool = False
    containment_meta: dict[str, Any] | None = None


_SCHEDULED_MESSAGES: dict[str, ScheduledMessageProvenance] = {}


def _clone_scheduled_provenance(
    provenance: ScheduledMessageProvenance,
) -> ScheduledMessageProvenance:
    """Copy mutable containment state before it crosses the registry lock."""
    return ScheduledMessageProvenance(
        slot_key=provenance.slot_key,
        message=provenance.message,
        scheduled_at=provenance.scheduled_at,
        completed=provenance.completed,
        containment_meta=deepcopy(provenance.containment_meta),
    )


def _reset_scheduled_messages_for_tests() -> None:
    """Clear process-memory scheduled provenance. Test-only restart seam."""
    with _SCHEDULED_MESSAGES_LOCK:
        _SCHEDULED_MESSAGES.clear()


def record_self_arm(loop_id: str, slot_key: str) -> None:
    """Record that *loop_id* on *slot_key* was armed by that session's own turn."""
    path = self_arm_record_path()
    with _record_lock(path):
        entries = _read_self_arm_record(path)
        entries[str(loop_id)] = {"slot_key": str(slot_key), "armed_ts": time.time()}
        _write_self_arm_record(path, entries)


def record_scheduled_message(
    record_id: str,
    slot_key: str,
    message: str,
    scheduled_at: float,
    *,
    containment_meta: dict[str, Any] | None = None,
) -> None:
    """Record authoritative user-authored content in gateway process memory."""
    if (
        not isinstance(message, str)
        or isinstance(scheduled_at, bool)
        or (containment_meta is not None and not isinstance(containment_meta, dict))
    ):
        raise ValueError("invalid scheduled-message provenance")
    provenance = ScheduledMessageProvenance(
        slot_key=str(slot_key),
        message=message,
        scheduled_at=float(scheduled_at),
        containment_meta=deepcopy(containment_meta),
    )
    if not math.isfinite(provenance.scheduled_at) or provenance.scheduled_at <= 0:
        raise ValueError("invalid scheduled-message provenance")
    with _SCHEDULED_MESSAGES_LOCK:
        _SCHEDULED_MESSAGES[str(record_id)] = provenance


def read_scheduled_message(
    record_id: str,
    slot_key: str,
) -> ScheduledMessageProvenance | None:
    """Read exact scheduled content only when its process-memory slot matches."""
    with _SCHEDULED_MESSAGES_LOCK:
        provenance = _SCHEDULED_MESSAGES.get(str(record_id))
        if provenance is None or provenance.slot_key != str(slot_key):
            return None
        return _clone_scheduled_provenance(provenance)


def replace_scheduled_message(
    record_id: str,
    expected: ScheduledMessageProvenance,
    message: str,
    scheduled_at: float,
) -> bool:
    """Atomically replace pending process-memory content by exact-value CAS."""
    if not isinstance(message, str) or isinstance(scheduled_at, bool):
        raise ValueError("invalid scheduled-message provenance")
    replacement = ScheduledMessageProvenance(
        slot_key=expected.slot_key,
        message=message,
        scheduled_at=float(scheduled_at),
        containment_meta=deepcopy(expected.containment_meta),
    )
    if (
        expected.completed
        or not math.isfinite(replacement.scheduled_at)
        or replacement.scheduled_at <= 0
    ):
        raise ValueError("invalid scheduled-message provenance")
    with _SCHEDULED_MESSAGES_LOCK:
        record_key = str(record_id)
        if _SCHEDULED_MESSAGES.get(record_key) != expected:
            return False
        _SCHEDULED_MESSAGES[record_key] = replacement
        return True


def mark_scheduled_message_completed(
    record_id: str,
    expected: ScheduledMessageProvenance,
) -> bool:
    """Atomically mark one exact process-memory message completed."""
    completed = ScheduledMessageProvenance(
        expected.slot_key,
        expected.message,
        expected.scheduled_at,
        completed=True,
        containment_meta=deepcopy(expected.containment_meta),
    )
    with _SCHEDULED_MESSAGES_LOCK:
        record_key = str(record_id)
        current = _SCHEDULED_MESSAGES.get(record_key)
        if current is None:
            return False
        if current.completed:
            return current == completed
        if current != expected:
            return False
        _SCHEDULED_MESSAGES[record_key] = completed
        return True


def delete_scheduled_message_record(record_id: str) -> None:
    """Delete one process-memory provenance record."""
    with _SCHEDULED_MESSAGES_LOCK:
        _SCHEDULED_MESSAGES.pop(str(record_id), None)


def forget_self_arm(loop_id: str) -> None:
    """Drop a self-arm or scheduled-provenance record. Best-effort; never raises."""
    try:
        if str(loop_id).startswith("scheduled-message:"):
            delete_scheduled_message_record(loop_id)
            return
        path = self_arm_record_path()
        with _record_lock(path):
            entries = _read_self_arm_record(path)
            if str(loop_id) in entries:
                del entries[str(loop_id)]
                _write_self_arm_record(path, entries)
    except OSError:
        logger.warning("could not revoke autonudge record for %s", loop_id, exc_info=True)


def is_recorded_self_arm(loop_id: str, slot_key: str) -> bool:
    """Whether the trust record vouches that *loop_id* self-armed on *slot_key*."""
    entry = _read_self_arm_record(self_arm_record_path()).get(str(loop_id))
    return (
        entry is not None and entry.get("kind") is None and entry.get("slot_key") == str(slot_key)
    )
