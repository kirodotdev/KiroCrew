"""Authenticated record of which nudge loops a crew/member session armed ITSELF.

``NudgeLoop.self_armed`` is the one bit that relaxes the crew/member
external-arm refusal at fire time (``GatewayOrchestrator._fire_dashboard_nudge``),
and the loop store it lives in (``autonudge.json``) is agent-writable: an
agent -- or a prompt injected into one -- can write ``"self_armed": true`` on a
loop it did not arm from that session, and a restart would restore it. The
persisted bit alone is therefore not authorization; it is a hint that must
AGREE with a record the agent cannot forge.

That record lives here, under the keystone-gated ``trust/`` subtree (on
``security._SENSITIVE_HOME_DIRS`` as a whole directory, like the SEL HMAC key,
member DM bindings and member rules): the agent's file tools can neither read
nor write it, and only gateway code opening the path directly -- the authorizer,
at the moment it admits a self-arm -- ever writes it. The fire-time guard admits
a crew/member wake only when BOTH hold: ``loop.self_armed is True`` on the
record it loaded AND ``is_recorded_self_arm(loop.id, loop.slot_key)`` here. A
forged bit with no trust entry refuses; a stale trust entry with no bit refuses.

One flat JSON object ``{loop_id: {"slot_key": ..., "armed_ts": ...}}``. An entry
is written by exactly one path (the authorizer admitting a self-arm) and REVOKED
by exactly one path: its loop leaving the store (``AutoNudgeService.remove_sync``
calls :func:`forget_self_arm` on remove and on replacement), so a removed loop's
id cannot keep its authorization and the file cannot grow past the loops that
were armed and not yet removed. A write never prunes against a caller-supplied
view of the store -- see :func:`record_self_arm` for the race that would open.
Every read-modify-write runs under an exclusive file lock so two concurrent
self-arms cannot drop each other's entries. Every reader is TOTAL: a
missing, unreadable or malformed file reads as "not recorded", which is the
refusing answer.

Blocking file IO throughout -- async callers offload via ``asyncio.to_thread``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Iterator

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

SELF_ARM_RECORD_NAME = "autonudge-self-armed.json"
_LOCK_NAME = SELF_ARM_RECORD_NAME + ".lock"


@contextlib.contextmanager
def _record_lock() -> Iterator[None]:
    """Exclusive lock spanning one read-modify-write transaction.

    Two self-arms committing at once (two members arming in the same second)
    would otherwise each read the pre-transaction file and the second write
    would drop the first's entry -- a loop the authorizer reported as armed
    that the fire-time guard then refuses. A sibling lock file rather than the
    record itself, because ``atomic_write`` replaces the record's inode.
    """
    path = self_arm_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / _LOCK_NAME
    with open(lock_path, "a+", encoding="utf-8") as fh:
        with platform_compat.file_lock(fh.fileno(), exclusive=True):
            yield


def self_arm_record_path() -> Path:
    """Absolute path of the record, inside the keystone-gated ``trust/`` root."""
    return data_home() / "trust" / SELF_ARM_RECORD_NAME


def _read_record() -> dict[str, dict[str, Any]]:
    """Return the record's entries, or ``{}`` for absent/unreadable/malformed."""
    try:
        raw = json.loads(self_arm_record_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("autonudge self-arm record unreadable; treating as empty", exc_info=True)
        return {}
    entries = raw.get("loops") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {
        str(loop_id): entry
        for loop_id, entry in entries.items()
        if isinstance(entry, dict) and isinstance(entry.get("slot_key"), str)
    }


def _write_record(entries: dict[str, dict[str, Any]]) -> None:
    path = self_arm_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # The trust subtree is owner-only everywhere else (sel.py creates it 0o700);
    # tighten best-effort so a parents=True mkdir does not leave a default-mode
    # directory. The sensitive-path floor is the real fence.
    try:
        platform_compat.restrict_dir_to_owner(path.parent)
    except OSError:
        logger.debug("could not tighten mode on %s", path.parent, exc_info=True)
    atomic_write(
        path,
        json.dumps({"version": 1, "loops": entries}, ensure_ascii=False, sort_keys=True),
        fsync=True,
    )


def record_self_arm(loop_id: str, slot_key: str) -> None:
    """Record that *loop_id* on *slot_key* was armed by that session's own turn.

    A pure UPSERT of one entry: every other entry is preserved verbatim. The
    write deliberately does NOT prune against a "live loop ids" set supplied
    by the caller -- the authorizer takes that snapshot outside this lock, and
    two crew/member sessions arming in the same second (the crew-boot case the
    exception exists for) would race it: the arm whose snapshot predates the
    other's ``svc.add`` but acquires the lock LAST would prune the sibling's
    freshly written entry, and that sibling's loop -- reported as armed --
    would be refused at every fire. Removing entries is revocation's job
    (:func:`forget_self_arm`), reached from every path a loop leaves the store
    (``AutoNudgeService.remove_sync``), so the record cannot grow past the
    loops that were ever armed and not yet removed. Raises ``OSError`` on a
    failed write: the caller (the authorizer) treats that as fail-closed -- a
    self-armed loop that cannot be recorded would never be allowed to fire, so
    it must not be reported as armed.
    """
    with _record_lock():
        entries = _read_record()
        entries[str(loop_id)] = {"slot_key": str(slot_key), "armed_ts": time.time()}
        _write_record(entries)


def forget_self_arm(loop_id: str) -> None:
    """Drop *loop_id* from the record. Best-effort; never raises."""
    try:
        with _record_lock():
            entries = _read_record()
            if str(loop_id) in entries:
                del entries[str(loop_id)]
                _write_record(entries)
    except OSError:
        logger.warning("could not revoke self-arm record for %s", loop_id, exc_info=True)


def is_recorded_self_arm(loop_id: str, slot_key: str) -> bool:
    """Whether the trust record vouches that *loop_id* self-armed on *slot_key*.

    Total: any failure to read is ``False`` (refuse). Both the id AND the slot
    must match, so a forged loop that reuses a recorded id on a different slot
    does not inherit the authorization.
    """
    entry = _read_record().get(str(loop_id))
    return entry is not None and entry.get("slot_key") == str(slot_key)
