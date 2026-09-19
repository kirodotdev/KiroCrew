"""Per-session work ledger — a PROJECTION of the session's crew log.

Long-horizon sessions (monitor loops, goal loops) accumulate their working state
as prior transcript turns, which the harness-owned compaction then summarizes
lossily. This module gives every session a durable **state record** instead —
goal, phase, next intent, tried approaches, artifact pointers, and a bounded
event tail — so the context window becomes a cache and the record is the
authority.

The record is not stored. It is a FOLD of the session's append-only crew log:
:func:`record` appends exactly one ``ledger/recorded`` entry per call, and every
reader folds those entries back into the record (``crew_log.projection``'s
``ledger`` fold). So there is one authority for the ledger and no second document
beside it. Two paths hold ledger bytes, and only the first is live:

    <data home>/crew-log/sessions/<store name>/log.jsonl   # the entries
    <data home>/ledger/<store name>/state.json             # pre-projection, carried once

The legacy path has exactly one reader, the upgrade carry-forward: the first record
on a slot whose folded record is empty appends the old document's goal, phase, next
and artifacts as one further entry, then marks it consumed so no later session can
carry it again. That carry is the ONE exception to one entry per call, it happens
at most once per slot, and it is a separate append rather than a merged one -- so
the entry a call is making stays exactly what the caller asked for. Nothing reads
the legacy document after it is consumed, and nothing writes it at all.

Design notes:

- **One entry, one update.** The fields a call set and the event that explains
  them ride on the SAME appended line, so the phase-requires-a-reason rule is a
  property of one entry rather than of two writes a crash can separate. There is
  no ordering in which a reader sees a phase that moved without its reason.
- **Keyed by SLOT, folded across units.** A slot owns one ACP session id at a
  time, not for its whole life — a reset, an agent or model switch and a provider
  swap all cold-start a new id — so a slot's updates are spread over the crew log
  of each id it ran under. The read joins them, oldest unit first
  (``store.session_units_for_slot``); a write only ever needs the unit it is in.
- **Exact-key identity.** :func:`ledger_key` only strips the dashboard prefixes
  (the same strip the permanent-delete funnel applies); it never folds the key's
  charset. One dashboard session is legitimately spelled both
  ``dashboard_chat-X`` and ``chat-X`` and both must reach one ledger, while a
  charset fold would map distinct channel session keys onto one.
- **Purged with the session.** The entries live in the session's crew log, so the
  permanent-delete funnel that removes that unit removes the ledger with it. The
  legacy ``ledger/`` store is left alone by every request path and is collectable
  with ``kirocrew ledger-sweep``.
- **Bounds are re-applied on read.** Every field is clamped when the entry is
  built AND when the fold reads it: a writer's clamp binds the writer, and a
  damaged or planted line is exactly the input that ignores it.

The legacy store's delete machinery stays here (:func:`purge_matching` and the
helpers below), because it is still the one spelling of removing one of those
directories and the sweep is still its caller. ``crew_log.store`` also imports
:func:`_store_name`, :func:`resolved_within`, :func:`is_link` and
:func:`unlink_lock_in_hold` from here.

Callers pass keys through :func:`ledger_key`; this module never imports dashboard
state, and it reaches the crew log lazily so it stays usable from the gateway boot
path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from kiro_crew.config.paths import data_home
from kiro_crew.platform_compat import (
    release_lock,
    strip_extended_length_prefix,
    try_acquire_lock,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: The crew log entry every :func:`record` call appends, and the only type the
#: ``ledger`` fold interprets. Declared in ``crew_log.entry_types``; spelled here
#: because this module is the WRITER and the writer owns the type it produces.
LEDGER_ENTRY_TYPE = "ledger/recorded"

#: The fold registered in ``crew_log.projection`` that turns those entries back
#: into the state record. Named here for the same reason as the entry type.
_FOLD_NAME = "ledger"

#: What a ledger entry is attributed to. The gateway writes it -- a route running
#: on the gateway's own thread, on behalf of the session that called the tool -- so
#: it is the gateway's own emitter name, the same one every other non-ACP session
#: entry carries.
_ENTRY_SRC = "gateway"

#: How long an append waits for the crew log's writer before this call answers.
#: The acknowledgement is meant to mean the entry is on disk, so the wait is the
#: point; it runs on a worker thread, so it costs no event-loop time. On expiry
#: the entry is still owed and counted by the writer rather than lost.
_APPEND_FLUSH_SECONDS = 5.0

#: Slots whose ledger fold is kept between reads. Bounded by COUNT: each
#: checkpoint is a bounded record, so what needs a ceiling is how many are
#: retained. Insertion-ordered, so the oldest is the one evicted.
_FOLD_CACHE_SLOTS = 64
_fold_cache: "dict[tuple[str, str], tuple[tuple[str, ...], tuple[int, ...], Any]]" = {}

#: Whether each slot's LAST append reached disk before its call answered. Read by
#: the record route so a caller is told, rather than being handed a 200 that
#: implies a durability the wait did not prove. One entry per slot, replaced.

#: The record fields that make a ledger worth reading. A record holding none of
#: them has nothing to steer a resumed cycle with, so it reads as absent.
_CONTENT_FIELDS: tuple[str, ...] = ("goal", "phase", "next", "tried", "artifacts")


class LedgerUnavailable(RuntimeError):
    """This session's crew log cannot take a ledger update, so nothing was written.

    Raised rather than swallowed. The ledger's authority is the crew log, so with
    no log to append to there is nowhere for the update to go -- and a write that
    silently went nowhere is the one outcome a durable record must never produce.
    The caller turns this into a refusal a person can act on.
    """


#: Phases that end a workstream. ``finished_at`` is stamped when the record
#: enters one of these; anything else is an in-flight phase.
TERMINAL_PHASES = frozenset({"done", "abandoned"})

#: Vocabulary for event lines. A phase change REQUIRES one of these; an event
#: without a phase change coerces an unrecognized kind to ``note`` (the text
#: is the payload there, the kind only a filter).
EVENT_KINDS = frozenset({"progress", "decision", "tried", "blocked", "unblocked", "phase", "note"})

# Bounds. The ledger is injected into nudge turns and read back every cycle,
# so every field is capped at write time; a runaway writer degrades to a
# clamped record instead of an unbounded file. The fold re-applies them on read,
# because a file's bytes are not the writer's to promise.
_MAX_TEXT = 2000
_MAX_PHASE = 128
_MAX_ARTIFACT_KEY = 128
_MAX_TRIED = 50
_MAX_ARTIFACTS = 32
_MAX_EVENTS = 100
_MAX_EVENT_TAIL = 20
#: Refuse to parse a state file past this size: with every field clamped the
#: legitimate maximum is well under it, so anything bigger is damage or
#: tampering, and parsing it would cost what the clamps exist to prevent.
_MAX_STATE_BYTES = 1_000_000

#: Lock acquire budget. Every in-tree critical section is a sub-millisecond
#: read + atomic rename, so this is a ceiling against a live cross-process
#: holder, not a normal wait. On expiry the write FAILS CLOSED with OSError.
_LOCK_TIMEOUT_SECS = 5.0
_LOCK_POLL_SECS = 0.05

_STATE_FILE = "state.json"
#: Root for the files that govern a slot's fold, one directory per slot. It sits
#: BESIDE the per-slot stores rather than inside one, because those stores are
#: collectable residue: :func:`purge_matching` removes a whole store by breadcrumb
#: and ``ledger_sweep`` proposes a finished one for purge by age. A control file
#: inside a store is therefore removable by documented maintenance, and losing the
#: exclusion list is what lets a recycled slot key fold a deleted conversation's
#: units. It stays under the ledger root so it inherits the root's fence -- these
#: files decide what a fold reads, so a writable copy outside the fence would let
#: an agent tool hide a live slot's units. ``_scan_work_ledgers`` skips its
#: bindings directory by name for the same reason; both scans skip this one.
_CONTROL_DIR_NAME = "control"
#: Claim on a slot's legacy document, holding one of the two words below. Created
#: EXCLUSIVELY, which is what makes it a claim rather than a flag: two first
#: records on one slot -- a resumed loop and its own dashboard tab -- would
#: otherwise both carry, appending the legacy goal and phase twice.
_CARRIED_FILE = "carried"
#: A claim taken but not yet proved. The carry has been appended by this holder or
#: died trying, and the marker says which only after the append reports back.
_CARRY_PENDING = "pending"
#: A claim whose carry reached disk. Final: no later call carries this slot again,
#: which is what stops a permanent delete's preserved document coming back on a
#: recycled slot key.
_CARRY_COMMITTED = "committed"
#: :func:`_claim_carry` answers one of these. ``BUSY`` is not "nothing to carry": it
#: means ANOTHER caller holds a live claim, so this one must not record past a carry
#: that has not landed -- its own update would be appended first and the legacy goal
#: and phase would then apply over it.
_CLAIM_TAKEN = "taken"
_CLAIM_BUSY = "busy"
_CLAIM_DONE = "done"
#: How long a claim may sit ``pending`` before another call may take it over. The
#: window a holder needs is bounded by its own append wait, so a marker older than
#: this is abandoned rather than in flight, and a crashed carry stops being
#: permanent. Taking over cannot duplicate a carry: a carry runs only when the
#: folded record is empty, and a carry that landed makes it non-empty.
_CARRY_STALE_SECS = 60.0
#: Unit ids a permanent delete removed from this slot, one per line. The funnel
#: deletes the conversation's own unit, but a slot accumulates one unit per reset
#: and the earlier ones survive -- so a fresh session on a recycled slot key would
#: fold them and resurrect deleted state. Unit IDS are what is recorded, never a
#: timestamp: the ids present at delete time are exactly the ones to exclude, and
#: a successor's unit has an id that is not among them, so no clock is involved.
_DELETED_UNITS_FILE = "deleted-units"
#: Hard ceiling on how many units one slot may exclude. Overflow FAILS CLOSED: the
#: exclusion is refused and so is the delete, because dropping an id to stay under a
#: bound resurrects exactly the state the file exists to hide. A slot gains one unit
#: per reset, so reaching this means a slot with thousands of resets and deletes, and
#: the honest answer there is to refuse loudly rather than to silently expose.
_MAX_EXCLUDED_UNITS = 4096
#: Longest unit id this reader accounts for, for sizing the read below. An ACP session
#: id is far shorter; the slack is deliberate.
_MAX_UNIT_ID_BYTES = 128
#: Read bound for the exclusion file, DERIVED from its own count bound rather than
#: borrowed from the order file's. A smaller borrowed cap truncates a valid exclusion
#: set, and a truncated read rewritten as the whole set permanently drops the most
#: recently deleted units -- resurrecting exactly the state the file exists to hide. A
#: file past this is REJECTED rather than truncated, because the COUNT bound is the one
#: that is meant to fail closed.
_MAX_EXCLUDED_BYTES = _MAX_EXCLUDED_UNITS * (_MAX_UNIT_ID_BYTES + 1)
#: The slot's units in the order they first recorded, one per line. APPEND order is
#: what makes this causal: a unit is written here by the call that records into it,
#: so the sequence reflects what actually happened rather than what a clock said.
#: Unit headers carry a wall clock, and a clock that steps backward -- an NTP
#: correction, a manual set -- makes a newer unit sort before an older one, which
#: applies a retired session's goal and phase over a later one's.
_UNIT_ORDER_FILE = "unit-order"
#: How many of a slot's units the order log keeps, newest kept. A slot gains one
#: per reset, so this is generous. Past it the oldest recorded ids drop out and
#: those units fold with the never-recorded ones, which the fold applies BEFORE
#: the kept tail -- they are older than everything in it by construction.
_MAX_ORDERED_UNITS = 64
#: Size past which the order file is COMPACTED to the window above. Generous enough
#: that the ordinary path stays a bare append -- an ACP session id is well under 64
#: bytes, so this is several times the window -- and small enough that the file can
#: never become a cost. A bound that bounds only what a read RETURNS still leaves the
#: file itself unbounded, which is a bound in name only.
_MAX_ORDER_BYTES = 16 * 1024
#: Hard ceiling on what a single read of that file consumes, whatever its size. A file
#: written by a build that had no compaction is still bounded for this reader.
_MAX_ORDER_READ_BYTES = 64 * 1024
_KEY_FILE = "slot_key"
_LOCK_FILE = ".lock"

#: Fold for the READABLE half of a store directory name (it originated as the
#: Crew Mode store's fold and outlived that mode; ``work_ledger`` imports this
#: copy). Kept in a leaf module usable from the gateway boot path. Identity is
#: the digest over the exact key, never this fold.
_STORE_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
_STORE_NAME_READABLE_MAX = 80


def ledger_key(session_key: str) -> str:
    """Fold a session/slot key to the ledger's identity spelling — LOSSLESSLY.

    Only the dashboard prefixes are stripped, because one dashboard session is
    legitimately spelled both ``dashboard_chat-X`` (history/API) and
    ``chat-X`` (live slot, nudge loop) and both must reach one ledger. Nothing
    else is rewritten: a charset fold here would be lossy, and two distinct
    channel session keys that fold to the same string would share a ledger —
    one session reading and overwriting another's state.
    """
    key = session_key or ""
    if key.startswith("dashboard:"):
        key = key[len("dashboard:") :]
    while key.startswith("dashboard_"):
        key = key[len("dashboard_") :]
    return key


def _store_name(slot_key: str) -> str:
    """Directory name for *slot_key*'s ledger — unique per EXACT key.

    Readable fold capped for filesystem name limits; uniqueness comes from the
    digest over the FULL key, so ``Foo``/``foo`` and long shared prefixes all
    get distinct directories on every filesystem. The name is not decodable
    back to the key — the key is persisted inside the store instead.
    """
    readable = _STORE_NAME_UNSAFE.sub("_", slot_key)[:_STORE_NAME_READABLE_MAX]
    digest = hashlib.sha256(slot_key.encode("utf-8")).hexdigest()[:8]
    return f"{readable}-{digest}"


def _ledger_root() -> Path:
    """Ledger root, resolved against the live data home per call.

    Never captured at import: an import-time binding freezes the data home and
    defeats pod isolation and test isolation (same rule as
    ``subagent_persistence._subagents_dir``).
    """
    return data_home() / "ledger"


def resolved_within(base: Path, name: str) -> Path | None:
    """``base / name`` resolved, or ``None`` when it does not stay inside *base*.

    The symlink-safe containment check every ledger path goes through. Two
    properties keep it honest under concurrency:

    * The base is resolved ONCE and the child is built from the resolved base, so
      both sides are spelled from the same ancestors.
    * Both sides are stripped of Windows' extended-length prefix before the
      comparison. ``Path.resolve()`` on a FILE that another thread is replacing at
      that moment comes back as ``\\\\?\\C:\\...``: ``ntpath.realpath`` drops the
      prefix only after re-checking the stripped spelling, and that re-check fails
      when the file has just been swapped out. The directory, resolved separately,
      comes back as ``C:\\...``, and ``is_relative_to`` then reads the prefix alone
      as an escape. Four threads binding one worker at once reproduce it in about
      four runs of ten on a short-name temp root; the CI Windows shard is one.

    The root itself is not a member: a name that folds to nothing must not be
    granted the whole store.
    """
    parent = strip_extended_length_prefix(base.resolve())
    resolved = strip_extended_length_prefix((parent / name).resolve())
    if resolved == parent or not resolved.is_relative_to(parent):
        return None
    return resolved


def ledger_dir(slot_key: str) -> Path:
    """Validated per-session ledger directory for *slot_key*.

    Raises ``ValueError`` on an empty or path-hostile key. The fold in
    :func:`_store_name` already removes separators, but the raw key is checked
    too so a hostile key is refused loudly instead of silently folded, and the
    resolved path is required to stay inside the ledger root (symlink-safe).
    """
    if not slot_key or "\0" in slot_key or "/" in slot_key or "\\" in slot_key:
        raise ValueError(f"Invalid slot key for ledger: {slot_key!r}")
    resolved = resolved_within(_ledger_root(), _store_name(slot_key))
    if resolved is None:
        raise ValueError(f"Path traversal blocked for slot key: {slot_key!r}")
    return resolved


def control_dir(slot_key: str) -> Path:
    """Validated directory for the files that govern *slot_key*'s fold.

    Same key validation and symlink-safe containment as :func:`ledger_dir`, one
    level deeper: the store directories and this one are siblings under the ledger
    root, so no maintenance that removes a store can remove a slot's control files
    and no control file can be mistaken for a store. The two share a root so they
    share its fence.
    """
    if not slot_key or "\0" in slot_key or "/" in slot_key or "\\" in slot_key:
        raise ValueError(f"Invalid slot key for ledger: {slot_key!r}")
    root = resolved_within(_ledger_root(), _CONTROL_DIR_NAME)
    if root is None:
        raise ValueError("Path traversal blocked for the ledger control root")
    resolved = resolved_within(root, _store_name(slot_key))
    if resolved is None:
        raise ValueError(f"Path traversal blocked for slot key: {slot_key!r}")
    return resolved


def _control_file(slot_key: str, name: str, *, create: bool = False) -> Path:
    """Path to one of *slot_key*'s control files, creating its directory on demand.

    Readers pass ``create=False``: a read must not bring a directory into being,
    both because a read answering "nothing recorded" needs no directory and because
    a reader that creates one leaves residue for every slot anything ever asked
    about.
    """
    directory = control_dir(slot_key)
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def canonical_slot(slot_key: str, session_id: str) -> str:
    """The slot identity *session_id*'s OWN crew log header records, or *slot_key*.

    The write and the read have to agree on one spelling. A unit's header records the
    live slot key (`slot.key`), while a caller reaches the ledger under its own
    session key, and those are the same string only for a dashboard session, whose
    prefix :func:`ledger_key` strips. A cron-injected, hook, task-runner or ACP
    subagent session is keyed `cron:<id>`, `hook:<id>`, `taskrunner:...`, `acp:...`
    -- names :func:`ledger_key` leaves alone, because folding them would be lossy and
    two of them could collide. Without this the append lands in a unit headed by the
    slot key and every later read looks up the caller's key, finds no unit, and
    answers with an empty record.

    Falls back to *slot_key* whenever the header cannot be proved, so a slot with no
    unit yet, a crew log that is off, and an unreadable header all behave as before.
    """
    if not session_id:
        return slot_key
    try:
        from kiro_crew.crew_log import KIND_SESSION
        from kiro_crew.crew_log.store import unit_header_slot

        header = unit_header_slot(KIND_SESSION, session_id)
    except Exception:
        return slot_key
    return header or slot_key


def _clamp(value: Any, limit: int = _MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit]


def require_lock_inode(fd: int, lock_path: Path) -> None:
    """Refuse to enter a critical section on a lock inode the store does not have.

    A path-based advisory lock is taken on an INODE, and a purge that removes the
    store removes that inode. A writer that was queued on it still acquires it --
    the kernel grants the lock on the detached file -- and would then write into
    a directory that was deleted from under it (the ``mkdir`` a moment ago
    recreates it), publishing a torn store into a purged key: state without a
    breadcrumb, or a header-less item. This check, run immediately after the
    acquire, is what makes the purge's inode deletion safe: the queued writer
    compares the identity of the file it holds against the file now at the path
    and REFUSES when they differ or the path is gone. The caller sees the same
    ``OSError`` a held lock produces and retries; its next attempt opens whatever
    is really at the path -- nothing, or a fresh store.

    On Windows this check is a no-op by construction, and correctly so: a file
    cannot be unlinked while any handle is open on it, and a queued writer HOLDS
    a handle while it waits, so a purge running beside it either cannot remove
    the lock file at all (the writer then acquires the same inode and rebuilds a
    fresh store -- the ledger springs back, consistently) or removes it only when
    no writer was queued. The OS preserves lock identity there; this check exists
    for POSIX, where the unlink succeeds under an open handle. ``st_ino`` is
    compared only when both sides report one, since a filesystem that reports
    zero cannot be compared.
    """
    try:
        on_disk = os.stat(lock_path)
    except FileNotFoundError:
        raise OSError("ledger was removed while waiting for its lock; try again") from None
    held = os.fstat(fd)
    if held.st_ino and on_disk.st_ino:
        if (held.st_dev, held.st_ino) != (on_disk.st_dev, on_disk.st_ino):
            raise OSError("ledger lock was replaced while waiting; try again")


@contextmanager
def _locked(dir_path: Path, *, create: bool = True) -> Iterator[None]:
    """Bounded-against-a-holder exclusive lock over one ledger directory.

    The lock file is a dedicated inode that writes never replace (replacing
    the locked inode would let a second writer lock the NEW inode and
    interleave). The acquire is a bounded poll over
    :func:`platform_compat.try_acquire_lock` — the repo's one non-blocking
    acquire primitive, covering POSIX and Windows alike — and FAILS CLOSED
    with ``OSError`` rather than entering the critical section unserialized.

    Scope of the bound (be precise — the docstring must not out-promise the
    code). ``_LOCK_TIMEOUT_SECS`` bounds ONLY the ``try_acquire_lock`` poll
    loop below: against a *live cross-process holder* of the flock, this
    refuses with ``OSError`` instead of waiting forever. The deadline is
    checked between sleeps rather than during one, so the refusal lands within
    the budget plus at most one ``_LOCK_POLL_SECS`` interval — a bound, not an
    exact wall-clock cap.

    The pre-lock ``mkdir``/``os.open`` are ordinary path/inode syscalls; on a
    wedged filesystem (hard NFS mount, dying disk) they can stall unboundedly,
    and NO in-process deadline can interrupt them — SIGALRM is main-thread +
    POSIX-only (this runs on an ``asyncio.to_thread`` worker), a bounded
    dedicated-thread offload leaks an unkillable thread and a held fd on a
    hard hang, and ``O_NONBLOCK`` does not cover path-resolution/inode stalls
    (only FIFO/device opens). A wedged mount is therefore explicitly OUT OF
    SCOPE of this lock's deadline, not a bound this contextmanager promises.

    The deadline is established immediately before the poll it governs and is
    NOT hoisted above ``mkdir``/``os.open`` — hoisting would spend the budget
    on the pre-lock syscalls and leave a near-zero retry window for genuine
    contention, which is the inversion that must not recur.
    """
    # ``create=False`` is the PURGE's form: a writer may bring a store into
    # being by locking it, a deleter must not. With ``mkdir`` + ``O_CREAT`` a
    # second sweep racing the first would recreate the store the first just
    # removed -- a directory holding nothing but a lock file, with no breadcrumb,
    # which no later purge can name -- and only then find nothing to guard. Without
    # them the open raises ``FileNotFoundError`` for a store that is gone, and the
    # caller skips it.
    lock_path = dir_path / _LOCK_FILE
    if create:
        dir_path.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    else:
        fd = os.open(str(lock_path), os.O_RDWR)
    try:
        # Bound only the acquire poll: set the deadline adjacent to the loop
        # it governs, after the pre-lock syscalls (which it cannot bound).
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECS
        while not try_acquire_lock(fd, exclusive=True):
            if time.monotonic() >= deadline:
                raise OSError("ledger lock is held by another process; try again")
            time.sleep(_LOCK_POLL_SECS)
        try:
            # The store may have been purged while this writer waited: see
            # :func:`require_lock_inode`. Checked INSIDE the hold, so the answer
            # cannot change between the check and the write.
            require_lock_inode(fd, lock_path)
            yield
        finally:
            release_lock(fd)
    finally:
        os.close(fd)


def _empty_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "goal": "",
        "phase": "",
        "next": "",
        "tried": [],
        "artifacts": {},
        "events": [],
        "created_at": "",
        "last_progress_at": "",
        "finished_at": "",
    }


def _projection() -> Any:
    """The fold package, imported the first time a call actually needs it.

    Never at module scope, for two reasons that both matter. This module is on the
    gateway's boot path -- the nudge composer imports it -- and the crew log's
    storage package is optional behind its own flag, so importing it here would put
    that cost on every launch. And the crew log package imports THIS module (for the
    store-name fold and for the ledger's own vocabulary), so a module-scope import
    back would be a cycle.
    """
    from kiro_crew.crew_log import projection

    return projection


def crew_log_units(slot_key: str, live_session_id: str = "", alias: str = "") -> tuple[str, ...]:
    """Every crew log holding *slot_key*'s ledger entries, oldest unit first.

    *slot_key* is the CANONICAL spelling — the one a unit header records — and *alias*
    is the caller's own spelling when it differs (see :func:`canonical_slot`), joined
    so a record written under it keeps reading.

    ``()`` when the slot has none: a session that never recorded, or a gateway whose
    crew log is switched off. Every failure to LIST them answers the same way,
    because this runs on the read path of a loop cycle and a listing that cannot be
    made must not raise into one.
    """
    if not slot_key:
        return ()
    try:
        from kiro_crew.crew_log.store import session_units_for_slot

        units = session_units_for_slot(slot_key)
        if alias and alias != slot_key:
            # The caller's own spelling is joined BESIDE the canonical one, so a record
            # written under it keeps reading. Its units come FIRST: the canonical ones
            # are what is being written now, and a later update wins. Every control
            # file is keyed by the canonical spelling alone, so one slot has one
            # exclusion list and one order log however a caller spells its key.
            seen = set(units)
            units = (
                tuple(unit for unit in session_units_for_slot(alias) if unit not in seen) + units
            )
        excluded = _excluded_units(slot_key)
        if excluded:
            units = tuple(unit for unit in units if unit not in excluded)
        recorded = _recorded_unit_order(slot_key)
        if recorded:
            # The order log keeps the NEWEST ids, so anything absent from it is older
            # than everything in it: a unit that predates the log, one whose append
            # failed, or one evicted when the log filled. All of them therefore apply
            # BEFORE the kept tail. Putting them after it is what would let a slot's
            # 65th-oldest unit land last and write its stale goal and phase over the
            # current ones. A unit nothing recorded into contributes no entries, so its
            # position among them is immaterial; header order is kept for them.
            known = [unit for unit in recorded if unit in units]
            rest = [unit for unit in units if unit not in recorded]
            units = tuple(rest + known)
        if live_session_id and live_session_id in units:
            # The LIVE unit applies LAST, whatever the clock says. Units are ordered by
            # the wall clock their headers carry, and a clock that moves backward before
            # a replacement unit is created sorts that replacement BEFORE its
            # predecessor -- so a retired session's goal and phase would apply over the
            # current ones, which is the one inversion that changes what a resume reads.
            # Pinning the unit being written to the end removes it, and the fold cannot
            # be wrong about which unit that is, because its caller is inside it.
            units = tuple(u for u in units if u != live_session_id) + (live_session_id,)
        return units
    except Exception:
        # FAIL CLOSED to no units, which reads as the empty record. An exclusion list
        # that cannot be read is the case this matters for: answering with the units
        # anyway would serve a deleted conversation's state to whoever holds the slot
        # key now, and nothing later takes that back, while an empty record is
        # recovered by the next read that can see the list.
        logger.warning("ledger: could not list the crew logs for this slot", exc_info=True)
        return ()


def _fold_checkpoint(slot_key: str, units: "tuple[str, ...]") -> Any:
    """This slot's ledger fold, continued from where the last read left it.

    Folding is O(the log), not O(the record): the fold interprets only
    ``ledger/recorded`` entries but the reader still walks every line of every unit
    to find them, and a session's log carries its message bodies. A loop that reads
    the record on every wake would re-walk its whole history each time, which is the
    one cost the stored document did not have.

    So the checkpoint is kept in memory per slot and ADVANCED over the entries that
    arrived since, using the same seq-anchored machinery a cold fold uses -- the
    resumed answer and the from-scratch answer come out of one implementation, which
    is the property ``projection`` pins.

    Three things force a cold rebuild, and each would otherwise be a wrong answer
    rather than a slow one: a different unit list (the slot started another ACP
    session), a newest unit whose seq went BACKWARDS (its log was removed and
    recreated, so the seqs describe different bytes), and nothing cached at all.
    Only the newest unit can grow -- an earlier unit's session is over -- so
    advancing reads just its tail.

    In memory rather than on disk on purpose: the reader that pays this cost is the
    gateway's own loop, one process, and a durable checkpoint is a store of its own
    with its own invalidation rules. A second process simply folds cold.
    """
    projection = _projection()
    # The DATA HOME is part of the identity, not just the slot. One process serves
    # more than one home -- a pod, a test, a gateway restarted in place -- and a slot
    # key plus an ACP session id are not unique across them, so keying on the slot
    # alone lets one home's checkpoint answer another home's read. The store's own
    # scan fingerprint takes the same precaution for the same reason.
    cache_key = (str(data_home()), slot_key)
    cached = _fold_cache.get(cache_key)
    # EVERY unit's seq, not only the newest one's. An older unit is not closed to
    # writes: a forced reset tears a session down while a turn is still running, and
    # that turn goes on appending through the handle it already holds, so an earlier
    # unit can still grow. Keying growth on the newest unit alone would leave those
    # entries permanently outside the record -- the unit list is unchanged and the
    # newest seq is unchanged, so nothing would ever invalidate the checkpoint.
    seqs = tuple(_unit_last_seq(unit) for unit in units)
    if cached is not None and cached[0] == units and cached[1] != seqs:
        # Only the newest unit grew, and only forward: that is the one shape the
        # checkpoint can be continued over, because its state was folded through
        # every earlier unit already. Anything else -- an earlier unit that grew, or
        # any unit whose seq went BACKWARDS because its log was removed and
        # recreated -- describes different bytes and folds cold.
        continuable = (
            len(seqs) == len(cached[1])
            and seqs[:-1] == cached[1][:-1]
            and bool(seqs)
            and seqs[-1] > cached[1][-1]
        )
        if continuable:
            handle = projection.open_session_log(units[-1])
            if handle is not None:
                grown = projection.advance(
                    cached[2],
                    handle.iter_from(cached[1][-1] + 1, known=projection.KNOWN_TYPES),
                )
                _remember_fold(cache_key, units, seqs, grown)
                return grown
    elif cached is not None and cached[0] == units:
        return cached[2]
    checkpoint = projection.fold_slot_checkpoint(_FOLD_NAME, units)
    # Cache only a snapshot the fold AGREES with. ``seqs`` was sampled before the
    # fold, and an append landing while it ran is folded into the checkpoint but not
    # into that sample -- so the pair would say "state through N+1, seqs through N",
    # and the next read would advance from a seq already folded. ``advance`` refuses
    # that entry as at-or-below the checkpoint, which surfaces as an empty record
    # rather than as an error. Re-sampling and comparing is the whole guard: unequal
    # means this answer is correct but not cacheable, so it is returned uncached and
    # the next read folds cold.
    if tuple(_unit_last_seq(unit) for unit in units) == seqs:
        _remember_fold(cache_key, units, seqs, checkpoint)
    return checkpoint


def _unit_last_seq(unit_id: str) -> int:
    """The newest seq in *unit_id*'s log as the file itself reports it, or 0."""
    try:
        handle = _projection().open_session_log(unit_id)
    except Exception:
        return 0
    if handle is None:
        return 0
    # ``last_seq`` on a freshly opened handle is read off the file's tail, which is
    # what makes it usable as a growth signal for a reader that never appends.
    return int(getattr(handle, "last_seq", 0) or 0)


def _remember_fold(
    cache_key: "tuple[str, str]",
    units: "tuple[str, ...]",
    seqs: "tuple[int, ...]",
    checkpoint: Any,
) -> None:
    """Cache *checkpoint* under *cache_key* (data home + slot), keeping it bounded.

    Replaced whole per slot, and capped by count: a gateway sees many slots over its
    life and each checkpoint is a bounded record, so the ceiling is on how many are
    retained. The oldest entry goes first; an evicted slot folds cold on its next
    read, which costs time and never correctness.
    """
    _fold_cache[cache_key] = (units, seqs, checkpoint)
    while len(_fold_cache) > _FOLD_CACHE_SLOTS:
        _fold_cache.pop(next(iter(_fold_cache)))


def read_state(slot_key: str, live_session_id: str = "") -> dict[str, Any]:
    """The state record for *slot_key* -- a FOLD of its ``ledger/recorded`` entries.

    *live_session_id* is the ACP session the caller is serving on, when it has one. It
    does two things a read cannot do without it: it resolves the caller's key to the
    slot identity the unit headers record (:func:`canonical_slot`), and it pins the
    unit being written LAST whatever the header clocks say.

    The record is stored nowhere: it is what the entries fold to. That is what makes
    the phase-requires-a-reason rule unbreakable rather than merely enforced -- there
    is no second document that can say a phase moved while the log says why it did
    not.

    The shape is the one every reader already expected of the stored document, so
    the MCP tool, the route and the injected snapshot did not have to learn a new
    one (see ``crew_log.projection._ledger_render``).

    Best-effort by contract, as this function has always been: a slot with no
    entries reads as the empty record, and so does a fold that cannot be made. A
    nudge cycle asks this on its way into a turn, and raising there would stop the
    loop instead of telling anyone anything. The two cases are distinguished in the
    LOG rather than in the answer -- an absent ledger is silent, a refused fold is a
    warning, because that one means a reader older than the writer.

    Lock-free, and safe because the crew log is append-only: a reader sees a prefix
    of the truth, never a torn record.
    """
    key = slot_key or ""
    canonical = canonical_slot(key, live_session_id)
    units = crew_log_units(canonical, live_session_id, alias=key)
    if not units:
        return _empty_state()
    try:
        return _projection().projection_of(_fold_checkpoint(canonical, units)).value
    except Exception:
        logger.warning("ledger: folding this slot's crew logs failed", exc_info=True)
        # The cached checkpoint is not trusted after a failed advance: the failure
        # may have been a log this build cannot read, and a half-advanced state must
        # not become the answer to the next read.
        _fold_cache.pop((str(data_home()), canonical), None)
        return _empty_state()


def has_ledger(slot_key: str) -> bool:
    """Whether *slot_key* has ever recorded anything.

    A full fold, not a cheap file probe: the record has no file of its own to stat,
    and the honest answer to "has this slot recorded" is whether its entries fold
    to anything. Callers that go on to READ the record should call
    :func:`read_state` once and test it themselves rather than pay for two folds.
    """
    return _has_content(read_state(slot_key))


def _has_content(state: dict[str, Any]) -> bool:
    """Whether *state* holds anything a reader would act on."""
    return any(state.get(field) for field in _CONTENT_FIELDS)


def record_update(
    slot_key: str,
    *,
    session_id: str,
    goal: str | None = None,
    phase: str | None = None,
    next_step: str | None = None,
    tried_approach: str | None = None,
    tried_rejected_because: str | None = None,
    artifacts: dict[str, str] | None = None,
    event: str | None = None,
    event_kind: str | None = None,
) -> "tuple[dict[str, Any], bool]":
    """Append ONE ``ledger/recorded`` entry for *slot_key*; return its record and
    whether the append reached disk.

    *session_id* is the crew log the entry lands in -- the ACP session the slot is
    serving on right now. A slot owns one such id at a time rather than for its
    whole life, so a slot's updates are spread over the units it ran under and the
    read joins them; the WRITE only ever needs the one it is in.

    Enforces the ledger discipline exactly as before: passing *phase* without
    *event* and a recognized *event_kind* is refused with ``ValueError``. The
    enforcement is now stronger than a rule, because a phase and its reason ride on
    the SAME entry -- there is no ordering in which a reader can see one without the
    other, and nothing for a crash to separate.

    Returns the record the appended entry produces, folded by the same fold every
    reader uses: the state on disk, advanced over this one entry. So the answer does
    not depend on the writer having already drained, and it is not a second
    implementation of the update rules -- it is the fold, applied to an entry that is
    on its way to the file.

    Raises :class:`LedgerUnavailable` when the session has no crew log to append to.
    """
    if phase is not None:
        if not (event and event.strip()):
            raise ValueError("phase change requires an event: pass event + event_kind")
        if (event_kind or "").strip() not in EVENT_KINDS:
            kinds = ", ".join(sorted(EVENT_KINDS))
            raise ValueError(f"phase change requires event_kind (one of: {kinds})")
    if not slot_key or "\0" in slot_key:
        raise ValueError(f"Invalid slot key for ledger: {slot_key!r}")
    if not session_id:
        raise LedgerUnavailable(
            "this session has no live crew log, so its ledger cannot be recorded"
        )
    # The caller's own spelling is kept as an ALIAS and the unit header's slot becomes
    # the key everything else uses, so the append and every later read agree on one
    # identity. Without this a caller keyed `cron:<id>` or `hook:<id>` appends into a
    # unit headed by the real slot key and can never read its own record back.
    alias = slot_key
    slot_key = canonical_slot(slot_key, session_id)
    data = _entry_data(
        slot_key,
        goal=goal,
        phase=phase,
        next_step=next_step,
        tried_approach=tried_approach,
        tried_rejected_because=tried_rejected_because,
        artifacts=artifacts,
        event=event,
        event_kind=event_kind,
    )
    projection = _require_crew_log(session_id)
    units = crew_log_units(slot_key, session_id, alias=alias)
    # Read BEFORE the append, and fold the entry in below rather than re-reading
    # after it: the writer is asynchronous, so a read-back would race the drain and
    # answer with the record as it was a moment ago -- reporting a phase the caller
    # just set as unset.
    base = _fold_checkpoint(slot_key, units)
    # An upgrade CARRY-FORWARD, at most once per slot. A slot with state written
    # before the record became a fold has a document no entry describes, and a loop
    # resuming into this build would otherwise find its goal, phase and next step
    # gone. Carried as an ENTRY rather than read as a fallback, so the log stays the
    # single authority and the document is consumed instead of consulted.
    #
    # The trigger is an EMPTY FOLDED RECORD, not an absent unit: a session's crew log
    # is created on its first turn, so a slot that has recorded nothing still has a
    # unit, and keying on the unit would never carry anything at all.
    if not _has_content(projection.projection_of(base).value) and _carry_legacy_forward(
        slot_key, session_id
    ):
        units = crew_log_units(slot_key, session_id)
        base = _fold_checkpoint(slot_key, units)
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log.schema import Entry

    # Sampled BEFORE the append, so both checks bracket it. A second gateway owning
    # this unit makes the append inline rather than queued, and an inline refusal
    # increments the counter during the call below -- sampling afterwards folds that
    # increment into the baseline and reports the refused write as durable, which is
    # the one false positive this check exists to catch.
    refused_before = crew_log_emit.dropped_writes()
    seq_before = _unit_last_seq(session_id)
    crew_log_emit.on_ledger_recorded(session_id, data)
    # WAIT for the append, and say precisely what the wait proves. ``record`` answers
    # a caller that is about to act on the record, so a queued entry is not good
    # enough: the writer is asynchronous and an unclean death inside the
    # queue-to-flush window would lose an update this call already reported as taken.
    # The route runs this on a worker thread, so the wait costs no event-loop time.
    #
    # A drained queue is NOT the same claim as "this entry landed". ``flush`` answers
    # False only when the writer is still busy past the budget, in which case the
    # entry is queued rather than lost; a PERMANENTLY REFUSED append drains the job
    # and is counted instead, so it takes the True branch. Both are reported, and the
    # refusal count is sampled around this append to see it at all.
    #
    # Neither is raised. The entry is either already queued (a retry would append the
    # same update twice) or the counter is process-wide and a concurrent session's
    # refusal would be attributed here, so refusing on it would reject a good write.
    # The loss is logged, surfaced by ``dropped_writes()``, and superseded by the next
    # update, which re-reads the base from disk.
    _note_unit_order(slot_key, session_id)
    drained = crew_log_emit.flush(timeout=_APPEND_FLUSH_SECONDS)
    # THIS UNIT's own log has to have grown, which is the part the process-wide
    # refusal counter cannot say. A counter that did not move proves only that no
    # append anywhere was refused; a file whose newest seq did not move proves that
    # nothing was written HERE, whatever the counter says. Both are required, so the
    # remaining false positive needs a concurrent append into the SAME unit -- the
    # same conversation writing twice at once -- rather than any session anywhere.
    landed = _unit_last_seq(session_id) > seq_before
    durable = drained and landed and crew_log_emit.dropped_writes() == refused_before
    if not drained:
        logger.warning(
            "ledger: the crew log writer did not drain within %.1fs; this update is "
            "queued and counted, not yet durable",
            _APPEND_FLUSH_SECONDS,
        )
    elif not durable:
        logger.warning(
            "ledger: the crew log refused an append while this update was in flight; "
            "the update may not have landed and the next one supersedes it"
        )
    pending = Entry(
        type=LEDGER_ENTRY_TYPE,
        # One past what the fold consumed, which is all ``advance`` asks of it. The
        # real seq is assigned by the store under its lock and is not knowable here;
        # this entry is never written from this object, only folded.
        seq=base.last_seq + 1,
        time=int(time.time() * 1000),
        src=_ENTRY_SRC,
        data=data,
    )
    state = projection.projection_of(projection.advance(base, (pending,))).value
    # RETURNED, never stashed. A module-level flag keyed by slot is a second piece of
    # state to keep in step with this call: the caller reads it under ITS spelling
    # while this writes the canonical one, a missing key has to mean something, and a
    # concurrent update on the same slot lands between the write and the read. Handing
    # it back with the record it describes removes all three questions. It rides BESIDE
    # the record rather than inside it, because every reader of the record expects its
    # ten fields and durability is a fact about one call, not about the workstream.
    return state, durable


def record(slot_key: str, **kwargs: Any) -> dict[str, Any]:
    """:func:`record_update`'s record alone, for a caller that cannot act on durability.

    A caller that ignores durability is no worse off than one that never asked: the
    update is folded either way and the next one supersedes it.
    """
    return record_update(slot_key, **kwargs)[0]


def _carry_legacy_forward(slot_key: str, session_id: str) -> bool:
    """Append the pre-projection document as this slot's first entry. Once.

    Returns whether anything was carried. The caller takes this only when the slot's
    folded record is EMPTY, which is exactly the upgrade path: state written while
    the record was a file of its own, for a workstream still in flight. Once the
    carried entry lands the record has content, so it is never taken again.

    What it carries is the record a resume needs -- goal, phase, next, artifacts,
    and the newest rejected approach -- plus an event that says where it came from
    and names what it could not bring: the entry shape holds ONE ``tried``, so an
    older document's earlier approaches and its event tail are counted in that event
    rather than dropped in silence. Carrying them all would mean an append per row,
    which is unbounded work on a path that runs inside a turn.

    The phase rides with that event, so the carried entry keeps the invariant the
    whole record rests on: a phase never appears without a logged reason.
    """
    claim = _claim_carry(slot_key)
    if claim == _CLAIM_BUSY:
        # Another caller is carrying right now. Recording past it would append this
        # update FIRST and let the carry's legacy goal and phase apply over it, so the
        # caller is sent back instead; the retry finds the carry committed.
        raise LedgerUnavailable(
            "this slot's earlier ledger state is being carried into its crew log; "
            "try the update again"
        )
    if claim != _CLAIM_TAKEN:
        return False
    try:
        legacy = _legacy_document(slot_key)
    except LedgerUnavailable:
        # RELEASED before propagating: the claim is this call's, and leaving it standing
        # would make the retry see a fresh claim, answer BUSY, and refuse again until the
        # marker went stale.
        _finish_carry(slot_key, landed=False)
        raise
    if not _has_content(legacy):
        # RELEASED, not committed. An empty answer here is either a document with
        # nothing in it or one that could not be read, and those are indistinguishable
        # from here -- committing would permanently skip real state on a transient read
        # failure, which is the loss this two-phase claim exists to prevent.
        _finish_carry(slot_key, landed=False)
        return False
    data: dict[str, Any] = {"slot": _clamp(slot_key)}
    for field, key in (("goal", "goal"), ("next", "next")):
        value = legacy.get(key)
        if isinstance(value, str) and value:
            data[field] = _clamp(value)
    phase = legacy.get("phase")
    if isinstance(phase, str) and phase:
        data["phase"] = _clamp(phase, _MAX_PHASE)
    artifacts = legacy.get("artifacts")
    if isinstance(artifacts, dict) and artifacts:
        data["artifacts"] = {
            _clamp(k, _MAX_ARTIFACT_KEY): _clamp(v)
            for k, v in artifacts.items()
            if isinstance(k, str) and isinstance(v, str)
        }
    tried = legacy.get("tried")
    dropped_tried = 0
    if isinstance(tried, list) and tried:
        newest = tried[-1]
        if isinstance(newest, dict) and isinstance(newest.get("approach"), str):
            data["tried"] = {
                "approach": _clamp(newest["approach"]),
                "rejected_because": _clamp(newest.get("rejected_because", "")),
            }
            dropped_tried = len(tried) - 1
        else:
            dropped_tried = len(tried)
    events = legacy.get("events")
    dropped_events = len(events) if isinstance(events, list) else 0
    left = []
    if dropped_tried > 0:
        left.append(f"{dropped_tried} earlier rejected approach(es)")
    if dropped_events > 0:
        left.append(f"{dropped_events} event(s)")
    note = "carried forward from this slot's pre-projection ledger document"
    if left:
        note += "; not carried: " + " and ".join(left)
    data["event"] = _clamp(note)
    data["event_kind"] = "note"

    from kiro_crew.crew_log import emit as crew_log_emit

    refused_before = crew_log_emit.dropped_writes()
    seq_before = _unit_last_seq(session_id)
    crew_log_emit.on_ledger_recorded(session_id, data)
    drained = crew_log_emit.flush(timeout=_APPEND_FLUSH_SECONDS)
    # THIS unit's own log has to have grown, for the same reason the ordinary update
    # checks it: a process-wide refusal counter cannot say whether this append landed,
    # and committing the claim on a weaker signal marks the document consumed when it
    # was not carried.
    landed = _unit_last_seq(session_id) > seq_before
    if not drained or not landed or crew_log_emit.dropped_writes() != refused_before:
        # The carry is owed rather than lost, but this call must not go on to fold a
        # base that is missing it and then write an update over the gap: that would
        # order the update ahead of the state it is meant to extend. Refusing sends
        # the caller back, and the retry carries again -- which it can only do because
        # the claim is RELEASED here rather than left standing over state that never
        # landed.
        _finish_carry(slot_key, landed=False)
        raise LedgerUnavailable(
            "this slot's earlier ledger state is still being carried into its crew log; "
            "try the update again"
        )
    _finish_carry(slot_key, landed=True)
    logger.info("ledger: carried a pre-projection document into the crew log")
    return True


class LedgerExclusionError(RuntimeError):
    """A slot's deleted units could not be recorded, so nothing may be deleted."""


def exclude_units(slot_key: str, unit_ids: "tuple[str, ...]") -> "tuple[str, ...]":
    """Record that *unit_ids* must never again be folded into *slot_key*'s record.

    Returns the ids this call ADDED, which is what :func:`unexclude_units` takes back:
    an id another delete already recorded is not this call's to remove.

    Called by the permanent-delete funnel with every unit that delete proved belongs
    to the slot. The funnel removes the conversation's own unit; this covers the
    earlier units of the same slot, which survive it.

    Recording ids rather than deleting those units is deliberate. A unit is inside the
    fenced crew log tree and may still be open to a writer, and a slot key is recycled,
    so removing everything under the slot would take a live successor's log with it.
    An exclusion is additive, provable from the delete's own evidence, and reversible
    by hand if it is ever wrong.

    SERIALIZED on the slot's own lock and written as one deduplicated set, so two
    concurrent deletes of the same slot cannot lose each other's ids, and the file
    cannot accumulate repeats. The set is BOUNDED, and overflow FAILS CLOSED -- the
    exclusion is refused and so is the delete -- because dropping an id to stay under
    a bound is precisely the resurrection this file exists to prevent.

    RAISES :class:`LedgerExclusionError` when the record cannot be made, which aborts
    the delete before the transcript is unlinked. An undeleted conversation is visible
    and can be deleted again, while state resurrected into a stranger's session is
    neither.
    """
    if not slot_key or not unit_ids:
        return ()
    try:
        path = _control_file(slot_key, _DELETED_UNITS_FILE, create=True)
        with _locked(control_dir(slot_key)):
            current = _read_lines(path, limit=_MAX_EXCLUDED_BYTES, reject_oversized=True)
            added = tuple(unit for unit in dict.fromkeys(unit_ids) if unit not in current)
            if not added:
                return ()
            merged = (*current, *added)
            if len(merged) > _MAX_EXCLUDED_UNITS:
                raise OSError(
                    f"slot would retain {len(merged)} excluded units, over the "
                    f"{_MAX_EXCLUDED_UNITS} bound"
                )
            _rewrite_lines(path, merged)
            # Read back INSIDE the hold. A write that is buffered, short, or on a full
            # or read-only filesystem would otherwise pass silently.
            missing = set(added) - set(
                _read_lines(path, limit=_MAX_EXCLUDED_BYTES, reject_oversized=True)
            )
            if missing:
                raise OSError(f"exclusion did not persist for {sorted(missing)}")
        return added
    except (ValueError, OSError) as exc:
        logger.error(
            "ledger: could NOT record slot %r's deleted units %s, so the delete is "
            "refused rather than leaving them foldable; exclude them by hand in %r",
            slot_key,
            list(unit_ids),
            _DELETED_UNITS_FILE,
            exc_info=True,
        )
        raise LedgerExclusionError(str(exc)) from exc


def unexclude_units(slot_key: str, unit_ids: "tuple[str, ...]") -> None:
    """Undo :func:`exclude_units` for *unit_ids*, for a delete that did not happen.

    Takes ONLY the ids that call reported adding. A rollback that dropped every id it
    was asked about would take back a concurrent delete's exclusions too, and that
    delete's units would then fold into the next session on the recycled slot key.

    The exclusion is written BEFORE the transcript is unlinked, because that is the
    only point where a failure can still refuse something. When the delete then does
    not proceed, those units belong to a session that still exists and folding them is
    correct, so the exclusion has to come back out. Serialized on the same lock as the
    write, and the rollback runs on the rare path only.

    A rollback that itself fails is reported at ERROR naming the file, and leaves a
    live slot's record reading empty until an operator edits it -- recoverable by hand,
    which the alternative ordering is not.
    """
    if not slot_key or not unit_ids:
        return
    drop = set(unit_ids)
    try:
        path = _control_file(slot_key, _DELETED_UNITS_FILE)
        if not path.exists():
            return
        with _locked(control_dir(slot_key), create=False):
            kept = tuple(
                unit
                for unit in _read_lines(path, limit=_MAX_EXCLUDED_BYTES, reject_oversized=True)
                if unit not in drop
            )
            _rewrite_lines(path, kept)
    except (ValueError, OSError) as exc:
        logger.error(
            "ledger: could NOT roll back slot %r's exclusion of %s after a delete that "
            "did not proceed; that session's record reads empty until %r is edited",
            slot_key,
            list(unit_ids),
            _DELETED_UNITS_FILE,
            exc_info=True,
        )
        # PROPAGATED: the caller answers retryable rather than 200, because a request
        # that reports success has told the person their session is intact while its
        # record reads empty.
        raise LedgerExclusionError(str(exc)) from exc


def _read_lines(
    path: Path, *, limit: int = _MAX_ORDER_READ_BYTES, reject_oversized: bool = False
) -> "tuple[str, ...]":
    """The file's non-empty lines, DEDUPLICATED, oldest first, bounded by one read.

    *limit* must be sized for the FILE being read, not shared between files with
    different bounds. With *reject_oversized* a file past the limit raises instead of
    coming back short: a caller that rewrites what it reads would otherwise persist the
    truncation, which is a silent loss rather than a bounded read.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            text = fh.read(limit + 1)
    except FileNotFoundError:
        return ()
    if len(text) > limit:
        if reject_oversized:
            raise OSError(f"{path.name} is larger than its {limit}-byte bound")
        text = text[:limit]
    return tuple(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()))


def _excluded_units(slot_key: str) -> "frozenset[str]":
    """Unit ids a permanent delete excluded from this slot.

    RAISES when the list cannot be read. An empty answer would mean "nothing is
    excluded", which is the one wrong thing to say here: the caller would fold the
    units a delete excluded and serve a deleted conversation's goal and phase to
    whoever holds the slot key now, and nothing later undoes that. The caller turns a
    failure into an EMPTY RECORD instead, which is recoverable by the next read and
    tells the person nothing rather than telling them someone else's state.
    """
    try:
        return frozenset(
            _read_lines(
                _control_file(slot_key, _DELETED_UNITS_FILE),
                limit=_MAX_EXCLUDED_BYTES,
                reject_oversized=True,
            )
        )
    except (ValueError, OSError) as exc:
        raise LedgerExclusionError(f"slot {slot_key!r}'s exclusion list is unreadable") from exc


def _create_exclusive(path: Path, text: str) -> bool:
    """Create *path* holding *text*, or answer False because it already exists.

    ``O_EXCL`` is the whole point: the create is the claim, and the kernel picks one
    winner among however many callers race for it. A ``write_text`` would truncate an
    existing file instead of refusing, which is not a claim at all.
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    return True


def _claim_carry(slot_key: str) -> str:
    """Claim the right to carry this slot's pre-projection document.

    Answers ``_CLAIM_TAKEN`` (this caller carries), ``_CLAIM_BUSY`` (another caller
    holds a live claim, so this one must not record yet) or ``_CLAIM_DONE`` (carried
    already, or there is nothing to carry).

    The marker is CREATED EXCLUSIVELY and holds ``pending`` until
    :func:`_finish_carry` proves the append reached disk. Creating it is the claim,
    not a flag set afterwards: checking a marker and then carrying is a plain race,
    and two first records on the same slot -- a resumed loop and its own dashboard
    tab -- would both read no marker and both carry, appending the legacy goal and
    phase twice. An exclusive create has exactly one winner however many callers
    arrive together.

    A ``pending`` marker older than :data:`_CARRY_STALE_SECS` is ABANDONED and may be
    taken over. Its holder's window is bounded by its own append wait, so past that
    the holder either committed -- in which case the marker says so and no take-over
    happens -- or died. Taking over cannot duplicate a carry: the caller reaches here
    only with an empty folded record, and a carry that landed makes it non-empty.
    """
    try:
        if not ledger_dir(slot_key).exists():
            # Nothing to carry, and nothing to claim: a slot with no legacy directory
            # never ran before this change.
            return _CLAIM_DONE
        path = _control_file(slot_key, _CARRIED_FILE, create=True)
        # READ, DECIDE and REPLACE under the slot's own lock. An exclusive create alone
        # picks one winner among callers arriving together, but the take-over path below
        # unlinks first -- and two callers that both saw one stale marker would each
        # unlink and each create, so both would carry and the later carry would apply the
        # legacy goal and phase over a newer update.
        with _locked(control_dir(slot_key)):
            if _create_exclusive(path, _CARRY_PENDING):
                return _CLAIM_TAKEN
            held = path.read_text(encoding="utf-8").strip()
            if held == _CARRY_COMMITTED:
                return _CLAIM_DONE
            if time.time() - path.stat().st_mtime < _CARRY_STALE_SECS:
                # Someone else's LIVE attempt, and this caller must not simply carry
                # on: its own update would be appended while that carry is still
                # queued, and the carry would then apply the legacy goal and phase
                # over it.
                return _CLAIM_BUSY
            logger.warning(
                "ledger: taking over slot %r's abandoned legacy-document claim; its "
                "holder neither committed the carry nor released the claim",
                slot_key,
            )
            path.unlink(missing_ok=True)
            return _CLAIM_TAKEN if _create_exclusive(path, _CARRY_PENDING) else _CLAIM_BUSY
    except (ValueError, OSError) as exc:
        # PROPAGATED, not answered DONE. Answering DONE lets this update be appended,
        # and an appended update makes the folded record non-empty -- after which the
        # carry is never attempted again and the legacy state is lost for good on a
        # transient filesystem error. Refusing sends the caller back with the carry
        # still owed.
        logger.warning("ledger: could not claim this slot's legacy document", exc_info=True)
        raise LedgerUnavailable(
            "this slot's earlier ledger state could not be claimed for carry; "
            "try the update again"
        ) from exc


def _finish_carry(slot_key: str, *, landed: bool) -> None:
    """Mark this slot's carry claim ``committed``, or RELEASE it when it did not land.

    Committing only after the append is proved is what keeps a crashed carry
    retryable: a marker published first would make the loss permanent, because the
    claim it left behind would refuse every later attempt at state that was never
    carried. Releasing an unlanded claim is the same property, taken immediately
    rather than waiting out the staleness window.
    """
    try:
        path = _control_file(slot_key, _CARRIED_FILE)
        if landed:
            path.write_text(_CARRY_COMMITTED, encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    except (ValueError, OSError):
        # The claim stays ``pending`` and goes stale, which a later call takes over.
        logger.warning(
            "ledger: could not finish slot %r's legacy-document claim", slot_key, exc_info=True
        )


def _note_unit_order(slot_key: str, session_id: str) -> None:
    """Record that *session_id* recorded into *slot_key*, if it is not already known.

    Append order is the causal order the fold reads units in, which is why this is
    written by the call that records rather than derived from a header's clock.
    Best-effort: a slot whose order cannot be written falls back to header order, which
    is what every slot did before this existed.
    """
    if not slot_key or not session_id:
        return
    try:
        known = _recorded_unit_order(slot_key)
        if session_id in known:
            return
        path = _control_file(slot_key, _UNIT_ORDER_FILE, create=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{session_id}\n")
            # FSYNCED before the update is acknowledged. The fallback for a missing line
            # is header order, and a backward clock step is exactly what that fallback
            # gets wrong -- so a crash that keeps the ledger entry and loses this line
            # restores a retired session's goal and phase over a later one's.
            fh.flush()
            os.fsync(fh.fileno())
        # COMPACTED when the file outgrows the window, so the bound bounds the disk
        # and the read rather than only the answer. The dedup check sees the window
        # alone, so a slot past the cap re-appends ids that fell out of it and the file
        # would otherwise grow without limit. Rewriting on a threshold rather than on
        # every record keeps the ordinary path a bare append: the rewrite is one file
        # in every `_MAX_ORDERED_UNITS` writes at worst.
        if path.stat().st_size > _MAX_ORDER_BYTES:
            kept = (*known[-(_MAX_ORDERED_UNITS - 1) :], session_id)
            _rewrite_lines(path, kept)
    except (ValueError, OSError):
        logger.warning("ledger: could not record this slot's unit order", exc_info=True)


def _rewrite_lines(path: Path, lines: "tuple[str, ...]") -> None:
    """Replace a control file with *lines*, one per line, atomically.

    A torn rewrite would lose the causal order the fold depends on, so the new content
    is written beside the file and renamed over it: a reader sees the whole old file
    or the whole new one.
    """
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(f"{line}\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _recorded_unit_order(slot_key: str) -> "tuple[str, ...]":
    """The units this slot recorded into, oldest first. Empty when there is no log.

    DEDUPLICATED, and truncated to the newest :data:`_MAX_ORDERED_UNITS` distinct ids
    after that. The check above this write is not atomic, so two concurrent first
    records on one unit can both append its id; a repeated id would put the same unit
    in the fold's order twice, and folding one unit twice replays entries the fold has
    already consumed.

    Reads at most :data:`_MAX_ORDER_BYTES` plus one compaction's worth, so a file that
    grew before this build's compaction existed cannot make a loop cycle read
    unboundedly.
    """
    try:
        path = _control_file(slot_key, _UNIT_ORDER_FILE)
        if not path.exists():
            return ()
        with path.open("r", encoding="utf-8") as fh:
            text = fh.read(_MAX_ORDER_READ_BYTES)
        seen: list[str] = []
        known: set[str] = set()
        for line in text.splitlines():
            unit = line.strip()
            if unit and unit not in known:
                known.add(unit)
                seen.append(unit)
        return tuple(seen[-_MAX_ORDERED_UNITS:])
    except (ValueError, OSError):
        return ()


def _legacy_document(slot_key: str) -> dict[str, Any]:
    """The pre-projection ``state.json`` document for *slot_key*, or the empty record.

    The ONLY reader of that file left, and it exists for the carry-forward above:
    nothing consults it to answer a read, so it is not a second authority. Size
    ceiling before parse, like every other reader of it, so a damaged or hostile
    file cannot make an upgrade allocate its size.
    """
    path = ledger_dir(slot_key) / _STATE_FILE
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        # ABSENT is the empty record: there is nothing to carry and never was.
        return _empty_state()
    except OSError as exc:
        # UNREADABLE is not empty. Answering empty here reads as "nothing to carry", the
        # update then lands, the folded record stops being empty -- and the carry, which
        # only ever runs on an empty record, is never attempted again. One transient
        # error would lose the state permanently.
        raise LedgerUnavailable(f"slot {slot_key!r}'s legacy document is unreadable") from exc
    if size > _MAX_STATE_BYTES:
        logger.warning("ledger: legacy document over the size ceiling; not carried")
        return _empty_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LedgerUnavailable(f"slot {slot_key!r}'s legacy document is unreadable") from exc
    except (ValueError, UnicodeDecodeError):
        # DAMAGED content is not transient: no retry parses it, so it is the empty record
        # and the claim is consumed rather than left to be retried forever.
        logger.warning("ledger: legacy document is not readable JSON; not carried")
        return _empty_state()
    if not isinstance(raw, dict):
        return _empty_state()
    state = _empty_state()
    state.update({k: v for k, v in raw.items() if k in state})
    return state


def _require_crew_log(session_id: str) -> Any:
    """The fold package, once this session is known to HAVE a crew log.

    The ledger writes through the emitter, which treats a session with no crew log
    as a policy no-op -- correct for a turn entry nobody asked for, and wrong for an
    update a person's agent explicitly recorded. So the existence of the log is
    established here, where the caller can be told, instead of being discovered as
    silence.
    """
    from kiro_crew.crew_log import emit as crew_log_emit

    if not crew_log_emit.enabled():
        raise LedgerUnavailable(
            "the session ledger is recorded in this session's crew log, which is "
            f"switched off; set {crew_log_emit.CREW_LOG_ENV}=1 to record one"
        )
    projection = _projection()
    from kiro_crew.crew_log.schema import KIND_SESSION
    from kiro_crew.crew_log.store import CrewLog

    try:
        present = CrewLog.exists(KIND_SESSION, session_id)
    except Exception as exc:
        raise LedgerUnavailable(f"this session's crew log could not be read: {exc}") from exc
    if not present:
        raise LedgerUnavailable(
            "this session has no crew log yet, so there is nothing to record into; "
            "it is created on the session's first turn"
        )
    return projection


def _entry_data(
    slot_key: str,
    *,
    goal: str | None,
    phase: str | None,
    next_step: str | None,
    tried_approach: str | None,
    tried_rejected_because: str | None,
    artifacts: dict[str, str] | None,
    event: str | None,
    event_kind: str | None,
) -> dict[str, Any]:
    """One entry's ``data``: only the fields this call set, each clamped.

    An omitted field is left OUT rather than written empty, because the fold reads
    presence as "changed" -- writing every field on every entry would make a call
    that set only ``next`` also assert an empty goal, and the record would lose the
    goal it never touched.
    """
    data: dict[str, Any] = {"slot": _clamp(slot_key)}
    if goal is not None:
        data["goal"] = _clamp(goal)
    if phase is not None:
        data["phase"] = _clamp(phase, _MAX_PHASE)
    if next_step is not None:
        data["next"] = _clamp(next_step)
    if tried_approach:
        data["tried"] = {
            "approach": _clamp(tried_approach),
            "rejected_because": _clamp(tried_rejected_because or ""),
        }
    if artifacts:
        # Bounded HERE as well as in the fold. The fold trims the merged mapping to
        # the same limit, which bounds the RECORD -- but not the line this appends,
        # and one call handing over ten thousand pointers would write a line every
        # future fold of this slot has to parse forever. An append-only log cannot
        # take that back, so the count is cut before the entry is built.
        pointers = {
            _clamp(key, _MAX_ARTIFACT_KEY): _clamp(value)
            for key, value in list(artifacts.items())[:_MAX_ARTIFACTS]
            if isinstance(key, str) and isinstance(value, str)
        }
        if pointers:
            data["artifacts"] = pointers
    if event and event.strip():
        kind = (event_kind or "").strip()
        data["event"] = _clamp(event.strip())
        # Coerced here, at the writer, so the declared vocabulary can be a CLOSED
        # enum the append path enforces: no call site can widen it, and an
        # unrecognized kind costs the event its filter rather than its record.
        data["event_kind"] = kind if kind in EVENT_KINDS else "note"
    return data


def purge_matching(exact_keys: set[str], *, guard: Any) -> int:
    """Purge the ledgers whose breadcrumb holds one of *exact_keys*, guarded.

    This is an explicit best-effort maintenance API with one production caller
    (``ledger_sweep.purge``). It matches EXACT keys only, deliberately: a
    caller-supplied fold is exactly the one way a caller could remove a ledger
    it never listed, so there is no fold parameter, not even a defaulted one.
    Callers must establish that every key is safe to remove before invoking it.

    Every removal is locked, ordered and identity-last -- there is one spelling
    of deletion in this module. *guard* is REQUIRED: it is called as
    ``guard(dir_path)`` INSIDE that ledger's own :func:`_locked` hold, and the
    store is removed only if it answers true. A caller that truly wants the match
    alone to decide passes ``lambda _dir: True`` and says so at the call site;
    the store does not offer a default that skips the re-decision.
    That is what lets a caller re-read the record and stand down on one that
    came back to life: a selection made outside the lock is a snapshot, and
    between the snapshot and the delete a session can be resumed and write a
    live phase into the very record the caller decided was finished. Selecting
    under the lock is not enough on its own -- the removal has to happen in the
    same hold, which is why the guard is a callback rather than a filter the
    caller applies first.

    The removal is ORDERED and the lock inode goes inside the hold where the OS
    allows it: other entries first, then ``state.json`` and ``slot_key`` only
    once every other removal succeeded (a failure never leaves a store without
    its record or its name), then the lock file itself while the lock is still
    held (:func:`unlink_lock_in_hold`) -- so a writer queued on that inode
    finds the path gone when it acquires and refuses (:func:`require_lock_inode`)
    rather than publishing into the removed store, and no later writer can be
    handed a second inode while a first is still held. Windows refuses the
    in-hold unlink and gets it after release instead, which is safe there
    because it fails whenever a writer still holds a handle. A store that could
    not be fully removed is left identifiable and is not counted as removed.
    """
    removed = 0
    try:
        root = _ledger_root()
        if not root.is_dir():
            return 0
        children = list(root.iterdir())
    except OSError:
        return 0
    for child in children:
        try:
            if child.name == _CONTROL_DIR_NAME:
                # Not a store: it holds the files that govern folds, and a purge that
                # removed a slot's exclusions would let a recycled slot key fold the
                # units that purge was called to make unreadable. It carries no
                # breadcrumb either, so this only makes the skip explicit.
                continue
            if not child.is_dir() or is_link(child):
                # A linked store directory names somewhere else; the delete
                # would land there. Never followed, whatever its breadcrumb says.
                continue
            key = (child / _KEY_FILE).read_text(encoding="utf-8").strip()
            if not key:
                continue
            if key not in exact_keys:
                continue
            try:
                lock_cm = _locked(child, create=False)
                lock_cm.__enter__()
            except FileNotFoundError:
                # Removed between the listing and the lock -- by a concurrent
                # sweep, or by the writer that owned it. Nothing to do, and
                # nothing must be created in its place.
                continue
            try:
                if not guard(child):
                    continue
                if not _remove_store_contents(child):
                    # Something survived. When it was ordinary content, both
                    # identity files were kept; when it was one of the identity
                    # files themselves, whichever still exists is what names the
                    # store to the next sweep. Say exactly which, so the log is
                    # true in both cases.
                    surviving = [n for n in (_STATE_FILE, _KEY_FILE) if (child / n).exists()]
                    logger.warning(
                        "ledger purge: %s not fully removed; kept: %s",
                        child.name,
                        ", ".join(surviving) or "(no identity file survived)",
                    )
                    continue
                lock_gone = unlink_lock_in_hold(child / _LOCK_FILE)
            finally:
                lock_cm.__exit__(None, None, None)
            _remove_store_shell(child, lock_gone=lock_gone)
            removed += 1
        except Exception:
            continue
    return removed


#: The two files that make a store a ledger and let a purge NAME it. They go
#: last, and only when everything else is gone.
_IDENTITY_FILES = frozenset({_STATE_FILE, _KEY_FILE})


def _remove_store_contents(dir_path: Path) -> bool:
    """Delete *dir_path*'s contents except the lock file. Returns whether all went.

    Ordered, with the record and the breadcrumb LAST. Everything else is removed
    first and every failure is counted -- ``rmtree(ignore_errors=True)`` would
    report success over a subtree it silently left standing, and on Windows a
    sharing violation on one held entry is exactly that case. ``state.json`` and
    ``slot_key`` are unlinked only once the count is zero, so a failed removal
    never leaves a store that has lost its record or its name: it stays a
    ledger, stays addressable by key, and reads as damaged to the next sweep
    rather than as a residue nothing can aim at. Call under the hold.
    """
    failures = 0

    def _count(_fn: object, _path: object, _exc: object) -> None:
        nonlocal failures
        failures += 1

    try:
        children = list(dir_path.iterdir())
    except OSError:
        return False
    for child in children:
        if child.name == _LOCK_FILE or child.name in _IDENTITY_FILES:
            continue
        # A linked entry is unlinked as a NAME, never followed: ``is_dir`` is true
        # through a link to a directory, and walking it would delete the target.
        if child.is_dir() and not is_link(child):
            shutil.rmtree(child, onerror=_count)
        else:
            try:
                child.unlink()
            except OSError:
                failures += 1
    if failures:
        return False
    for name in (_STATE_FILE, _KEY_FILE):
        try:
            (dir_path / name).unlink(missing_ok=True)
        except OSError:
            return False
    return True


def is_link(path: Path) -> bool:
    """Whether *path* is a symbolic link or a Windows junction -- a name that
    points somewhere else.

    A delete primitive must never FOLLOW one: a store directory, or an ``items/``
    inside one, that is a link would send the removal at whatever the link names,
    and nothing about the store's own records could tell. Both stores' purges
    refuse a linked store and a linked ``items/``, and both content walkers unlink
    a linked entry itself rather than descending into it.
    """
    return path.is_symlink() or path.is_junction()


def unlink_lock_in_hold(lock_path: Path) -> bool:
    """Unlink *lock_path* while its lock is still HELD. Returns whether it went.

    The one order that keeps lock identity stable through a purge. Unlinked
    inside the hold, the inode a queued writer is waiting on is already detached
    from the path by the time that writer acquires it, so its
    :func:`require_lock_inode` check sees the path gone and refuses. Unlinked
    AFTER release there is a window in which a queued writer acquires the old
    inode, validates it against a path that still exists, and proceeds -- and the
    late unlink then detaches the very inode it holds, so the next writer creates
    a new one and the two are not serialised against each other.

    POSIX permits the unlink under an open descriptor and this returns ``True``.
    Windows refuses it and this returns ``False``; there the caller unlinks after
    release instead, which is safe on Windows precisely because it fails whenever
    any writer still holds a handle -- the OS keeps the identity stable, and a
    successful late unlink proves nobody was queued.
    """
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _remove_store_shell(dir_path: Path, *, lock_gone: bool) -> None:
    """Remove the now-empty directory, and the lock file ONLY if the hold could not.

    Call AFTER releasing. *lock_gone* is :func:`unlink_lock_in_hold`'s answer.
    When it is true the lock path was unlinked inside the hold and MUST NOT be
    touched again here: by now a writer that refused on the detached inode may
    have retried, recreated the directory and taken a FRESH lock at the same path
    -- a second unlink would detach that fresh inode under its holder, and the
    next writer would take a third, un-serialised against the second. Only the
    empty-directory ``rmdir`` is attempted, and it simply fails if a writer has
    rebuilt the store. When *lock_gone* is false the OS refused the in-hold unlink
    (Windows), and the late unlink is safe there because it fails whenever any
    writer holds a handle.
    """
    if not lock_gone:
        try:
            (dir_path / _LOCK_FILE).unlink(missing_ok=True)
        except OSError:
            logger.debug("ledger purge: lock file still held; leaving it")
    try:
        dir_path.rmdir()
    except OSError:
        logger.debug("ledger purge: ledger directory not fully removed")


#: Ceiling for the injected snapshot block. A nudge turn carries this every
#: cycle, so it must stay small even against a clamped-but-full record.
_SNAPSHOT_MAX_CHARS = 1600
_SNAPSHOT_FIELD_MAX = 300
_SNAPSHOT_TRIED_TAIL = 3


def render_snapshot(slot_key: str) -> str:
    """Compact ``[work ledger]`` block for per-cycle injection, or ``""``.

    Empty when the session has recorded nothing, or when the workstream is
    finished — a terminal ledger has nothing to steer. ONE fold, not a probe
    followed by a read: the record has no file to stat, so asking whether it
    exists and asking what it says are the same question. Callers on an event loop
    must dispatch this to a worker thread — it reads files.
    """
    state = read_state(slot_key)
    if not _has_content(state):
        return ""
    if state["phase"] in TERMINAL_PHASES:
        return ""

    def _field(v: str) -> str:
        v = " ".join(v.split())
        return v[:_SNAPSHOT_FIELD_MAX]

    lines = [
        "[work ledger — durable state for this session; authoritative over memory of prior cycles]"
    ]
    if state["goal"]:
        lines.append(f"goal: {_field(state['goal'])}")
    if state["phase"]:
        lines.append(f"phase: {_field(state['phase'])}")
    if state["next"]:
        lines.append(f"next: {_field(state['next'])}")
    for item in state["tried"][-_SNAPSHOT_TRIED_TAIL:]:
        why = f" (rejected: {_field(item['rejected_because'])})" if item["rejected_because"] else ""
        lines.append(f"tried: {_field(item['approach'])}{why}")
    for k, v in state["artifacts"].items():
        lines.append(f"artifact {k}: {_field(v)}")
    block = "\n".join(lines)
    if len(block) > _SNAPSHOT_MAX_CHARS:
        block = block[: _SNAPSHOT_MAX_CHARS - 1] + "…"
    return block
