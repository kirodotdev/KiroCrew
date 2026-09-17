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

import asyncio
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

#: ``armed_by`` values an entry may carry. Absent reads as ``ARMED_BY_SELF``.
ARMED_BY_SELF = "self"
ARMED_BY_OWNER = "owner"


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


def _read_record_strict_raw() -> dict[str, Any]:
    """The record's ``loops`` map VERBATIM, for the writers.

    Unlike :func:`_read_record`, which is the readers' total view, this one
    keeps every entry as stored -- a malformed sibling included -- and RAISES
    ``OSError`` when the file exists but cannot be read or is not the expected
    shape. A writer that read a corrupt file as ``{}`` would then write its one
    entry over every sibling's, turning one bad byte into every other member's
    loop being refused at its next wake; refusing the write keeps the file as
    evidence and the caller fails closed. A missing file is an empty map: nothing
    to lose.
    """
    path = self_arm_record_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise OSError(f"autonudge trust record unreadable: {path}") from exc
    entries = raw.get("loops") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        raise OSError(f"autonudge trust record malformed: {path}")
    return {str(loop_id): entry for loop_id, entry in entries.items()}


def _write_record(entries: dict[str, Any]) -> None:
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
    _record_arm(loop_id, slot_key, ARMED_BY_SELF)


def record_owner_arm(loop_id: str, slot_key: str, *, txn: str = "") -> None:
    """Record that *loop_id* on *slot_key* was armed by the dashboard OWNER.

    The owner's Perpetual mode switch on the Crew Members page arms a member's
    own thread from OUTSIDE that thread's turn, which the self-arm exception
    does not cover. It is admitted on the owner-gated member route only, and
    recorded here under its own ``armed_by`` so the fire-time guard can vouch
    for it without the loop ever claiming to be self-armed: a self-arm entry
    never satisfies :func:`is_recorded_owner_arm` and an owner entry never
    satisfies :func:`is_recorded_self_arm`. Same lock, same upsert-only write,
    same revocation path (``forget_self_arm`` on removal) and the same
    fail-closed ``OSError`` contract as :func:`record_self_arm`.

    PRECEDENCE: one entry per loop, one party per entry. An owner TAKEOVER of
    a loop the member armed itself (the owner turning Perpetual mode on over
    a stopped self-armed loop) rewrites that loop's entry to ``owner``; the
    loop is then governed by the owner's rules (caps refused to the member,
    its stop retained) and the store's ``self_armed`` bit, which nothing
    rewrites, is inert because :func:`is_recorded_self_arm` does not vouch
    for an owner entry. The caller that takes over reads the entry first
    (:func:`read_arm_entry_strict`) so a failed takeover can put it back,
    and stamps its own ``txn`` token on the entry so that restore touches
    only the entry IT wrote: "still says owner" is not an identity once a
    second takeover can write owner too. An arm-time owner entry (no
    takeover) carries no token.
    """
    _record_arm(loop_id, slot_key, ARMED_BY_OWNER, txn=txn)


def _record_arm(loop_id: str, slot_key: str, armed_by: str, *, txn: str = "") -> None:
    with _record_lock():
        # Strict: a corrupt file refuses the write (OSError, fail closed for
        # the arm) rather than being replaced by a map holding only this entry.
        entries = _read_record_strict_raw()
        entry: dict[str, Any] = {
            "slot_key": str(slot_key),
            "armed_ts": time.time(),
            "armed_by": armed_by,
        }
        if txn:
            entry["txn"] = str(txn)
        entries[str(loop_id)] = entry
        _write_record(entries)


def restore_arm_party_if_token(
    loop_id: str, slot_key: str, expected_txn: str, prior_party: str
) -> bool:
    """Undo one takeover's owner entry, atomically, and only if it is still that takeover's.

    One locked read-modify-write: under :func:`_record_lock` the record is read
    strictly and verbatim, the entry for *loop_id* must name *slot_key*, say
    ``owner`` and carry exactly *expected_txn*; then it is rewritten to a self
    entry (``prior_party == "self"``) or removed (``prior_party == ""``), and
    every sibling is written back byte-for-byte. Returns ``True`` when it
    restored, ``False`` when the entry is not this takeover's any more (a later
    takeover's token, another party, no entry) -- in which case nothing is
    written. Because the compare and the write share the lock, an entry cannot
    change between them; the caller never needs a second read. A
    ``prior_party`` of ``owner`` or a blank token means nothing was changed and
    nothing is restored (``False``). Raises ``OSError`` when the file or the
    entry is malformed (fail closed, file left intact) or the write fails.
    """
    if not expected_txn or prior_party == ARMED_BY_OWNER:
        return False
    if prior_party not in (ARMED_BY_SELF, ""):
        raise ValueError(f"unknown prior party {prior_party!r}")
    with _record_lock():
        entries = _read_record_strict_raw()
        entry = entries.get(str(loop_id))
        if entry is None:
            return False
        if not isinstance(entry, dict) or not isinstance(entry.get("slot_key"), str):
            raise OSError(f"autonudge trust record entry for {loop_id} is malformed")
        if entry["slot_key"] != str(slot_key) or entry.get("armed_by") != ARMED_BY_OWNER:
            return False
        if entry.get("txn") != expected_txn:
            return False
        if prior_party == ARMED_BY_SELF:
            entries[str(loop_id)] = {
                "slot_key": str(slot_key),
                "armed_ts": time.time(),
                "armed_by": ARMED_BY_SELF,
            }
        else:
            del entries[str(loop_id)]
        _write_record(entries)
        return True


def forget_self_arm(loop_id: str) -> None:
    """Drop *loop_id* from the record. Best-effort; never raises."""
    try:
        with _record_lock():
            entries = _read_record_strict_raw()
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
    return _armed_by_of(loop_id, slot_key) == ARMED_BY_SELF


def is_recorded_owner_arm(loop_id: str, slot_key: str) -> bool:
    """Whether the trust record vouches that the OWNER armed *loop_id* on *slot_key*.

    Total like :func:`is_recorded_self_arm`, and disjoint from it: an entry
    vouches for exactly one arming party.
    """
    return _armed_by_of(loop_id, slot_key) == ARMED_BY_OWNER


def recorded_arm_party(loop_id: str, slot_key: str) -> str:
    """The recorded arming party for *loop_id* on *slot_key*: ``"self"``,
    ``"owner"`` or ``""`` for none. Total like the two predicates; a caller
    that must tell "none" from "could not read" uses
    :func:`read_arm_party_strict` instead.
    """
    return _armed_by_of(loop_id, slot_key)


def read_arm_entry_strict(loop_id: str, slot_key: str) -> tuple[str, str]:
    """``(party, txn)`` for *loop_id* on *slot_key*, with the strict reader's contract.

    ``party`` is what :func:`read_arm_party_strict` answers and raises on;
    ``txn`` is the takeover token the entry carries, ``""`` when none (an
    arm-time entry, an entry for another slot, or no entry). A non-string
    token is malformed and raises like any other malformed field.
    """
    entries = _read_record_strict_raw()
    if str(loop_id) not in entries:
        return "", ""
    entry = entries[str(loop_id)]
    if not isinstance(entry, dict) or not isinstance(entry.get("slot_key"), str):
        raise OSError(f"autonudge trust record entry for {loop_id} is malformed")
    if entry["slot_key"] != str(slot_key):
        return "", ""
    armed_by = entry.get("armed_by", ARMED_BY_SELF)
    if armed_by not in (ARMED_BY_SELF, ARMED_BY_OWNER):
        raise OSError(f"autonudge trust record entry for {loop_id} names an unknown party")
    txn = entry.get("txn", "")
    if not isinstance(txn, str):
        raise OSError(f"autonudge trust record entry for {loop_id} carries a malformed token")
    return str(armed_by), txn


def read_arm_party_strict(loop_id: str, slot_key: str) -> str:
    """Like :func:`recorded_arm_party`, but RAISES ``OSError`` when the record
    exists and cannot be read, instead of answering ``""``.

    For the callers whose refusing answer is not the safe one: an applier
    deciding whether a member may cap or remove its own loop must fail CLOSED
    on an unreadable record (refuse the cap, keep the record), and a total
    read would hand it the permissive ``""`` there. So every INDETERMINATE
    state raises: an unreadable or mis-shaped file, and an entry for this loop
    that is present but malformed (not a dict, no string ``slot_key``, an
    ``armed_by`` that names no known party). Only two states answer ``""``,
    and both are certain: no file at all, or no entry for this loop -- nothing
    was ever recorded. An entry naming ANOTHER slot is a certain answer too:
    whoever armed that loop did not arm it on this slot.
    """
    return read_arm_entry_strict(loop_id, slot_key)[0]


def _armed_by_of(loop_id: str, slot_key: str) -> str:
    """The recorded arming party for *loop_id* on *slot_key*, or ``""``.

    An entry with no ``armed_by`` predates the field and was written by the
    only writer that existed then -- the self-arm path -- so it reads as
    ``"self"``. Any other spelling reads as nobody, which refuses.
    """
    entry = _read_record().get(str(loop_id))
    return _party_of_entry(entry, slot_key)


def _party_of_entry(entry: dict[str, Any] | None, slot_key: str) -> str:
    if entry is None or entry.get("slot_key") != str(slot_key):
        return ""
    armed_by = entry.get("armed_by", ARMED_BY_SELF)
    if armed_by in (ARMED_BY_SELF, ARMED_BY_OWNER):
        return str(armed_by)
    return ""


async def await_thread_to_completion(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """``asyncio.to_thread`` that a cancellation cannot leave running unjoined.

    For the trust-record writes (``record_owner_arm``, the takeover restore):
    the thread is started as its own future and awaited through a shield; if
    the awaiting task is cancelled, the thread is awaited AGAIN -- shielded,
    in a loop, so a SECOND cancellation does not abandon it either -- until
    the thread future is done, and only then does the cancellation propagate.
    By the time the caller unwinds (releasing a lock, letting a shutdown
    drain's join report done) the write has finished. The wait is bounded by
    the write itself (one locked file rewrite) and, at shutdown, by the
    drain's own join timeout, which gives up on the TASK and logs so; the
    thread still runs to its end on its own. The thread's own exception, when
    the caller was cancelled, is dropped here: the caller is unwinding on the
    cancel, and the write's outcome is re-read by whoever acts next.
    """
    fut = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        while not fut.done():
            try:
                await asyncio.shield(fut)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - the write's own error is not this cancel's
                break
        raise
