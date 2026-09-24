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

The legacy path is read only to be RETIRED, by the upgrade carry-forward: the first
record on a slot whose folded record is empty appends the old document's goal, phase,
next and artifacts as one further entry, then marks it consumed so no later session
can carry it again. That carry is the ONE exception to one entry per call, it happens
at most once per slot, and it is a separate append rather than a merged one -- so
the entry a call is making stays exactly what the caller asked for.

Until that append happens a READ folds the same pending entry in without writing it,
because the first resumed turn after an upgrade reads before it records and would
otherwise run with no goal. Both come from one construction, so the record does not
change shape when the carry lands. Nothing reads the document once it is consumed,
nothing publishes from it, and nothing writes it at all.

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
from typing import Any, Iterator, NamedTuple

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


class LedgerEntryTooLarge(ValueError):
    """This update cannot fit one crew log entry, so nothing was written.

    Distinct from the writer's own refusal, which COUNTS an oversized entry and drops
    it. That is right for callers that cannot act on it, and wrong here: an entry over
    the ceiling by construction can never land, however often it is retried, so
    reporting the update as taken would promise a record that never exists and then
    disappears from the slot at the next read.

    A ``ValueError`` because it is a fact about the update's SIZE, which is the
    caller's input, not about the state of the record's home -- the same family as the
    phase-requires-event discipline rather than as :class:`LedgerUnavailable`.
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
#: Longest unit id this module RETAINS. Enforced at every site that writes an id into
#: a control file, not merely assumed: every bound below is derived from it, so an id
#: past it makes a file outgrow the read that is sized for it. An ACP session id is
#: far shorter; the slack is deliberate.
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
#: Work records need a separate causal order. Sharing the ledger's order would let
#: a later ledger-only append move a unit whose work update is stale past the unit
#: holding the newest work update.
_WORK_UNIT_ORDER_FILE = "work-unit-order"
#: How many of a slot's units the order log keeps, newest kept. A slot gains one
#: per reset, so this is generous. Past it the oldest recorded ids drop out and
#: those units fold with the never-recorded ones, which the fold applies BEFORE
#: the kept tail -- they are older than everything in it by construction.
_MAX_ORDERED_UNITS = 64
#: Size past which the order file is COMPACTED to the window above, DERIVED from the
#: per-id bound rather than guessed. A hardcoded number is a bound on the file that
#: says nothing about the ids in it: it holds only while every id is far under the
#: per-id bound, and the two drift apart silently. Derived, one compaction's worth of
#: maximum-size ids fits by construction, so the ordinary path stays a bare append.
_MAX_ORDER_BYTES = _MAX_ORDERED_UNITS * (_MAX_UNIT_ID_BYTES + 1)
#: Hard ceiling on what a single read of that file consumes, whatever its size. Two
#: compactions' worth, so a file written between the threshold above and the rewrite
#: is still read WHOLE. That is the property that matters rather than the number: this
#: read does NOT reject an oversized file, it comes back short, and the caller rewrites
#: what it read -- so a truncated read would persist as the permanent loss of the
#: NEWEST ids, which is the order the fold depends on. Enforcing the per-id bound is
#: what keeps the file inside this.
_MAX_ORDER_READ_BYTES = 2 * _MAX_ORDER_BYTES
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

    The incremental fold belongs to the crew log and is shared with every other
    slot-keyed reader (:func:`kiro_crew.crew_log.projection.fold_slot_warm`): it keeps
    this fold's cell in memory per slot, continues it over the entries that arrived
    since, and refolds cold for every shape that cannot be carried -- a different unit
    list, a unit whose log was removed and recreated, an earlier unit that grew, a seq
    that went backwards. ONE implementation rather than one per consumer, because each
    of these readers has to enforce the same rules and a rule missing from one of them
    is a wrong record rather than a slow one.

    In memory rather than on disk, and per process: the reader that pays this cost is
    the gateway's own loop, and a durable checkpoint is a store of its own with its own
    invalidation rules. A second process simply folds cold.
    """
    return _projection().fold_slot_warm(_FOLD_NAME, units, slot=slot_key)


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

    One exception to "no entries reads as empty", and it is the upgrade path: a slot
    whose state was written before the record became a fold has a document that no
    entry describes yet, and this read folds that document's PENDING carry in without
    appending it (:func:`_legacy_preview`). Without that, the first resumed turn after
    an upgrade -- which asks this before it records anything -- would run with no goal,
    and the carry that a later write performs cannot undo a turn already taken.

    Lock-free, and safe because the crew log is append-only: a reader sees a prefix
    of the truth, never a torn record.
    """
    key = slot_key or ""
    canonical = canonical_slot(key, live_session_id)
    units = crew_log_units(canonical, live_session_id, alias=key)
    try:
        # Folded even with NO units, which costs a zero-unit fold and answers the
        # question the branch below has to ask: a slot that ran before the record
        # became a fold has a document and no crew log at all, so returning early on
        # an absent unit would skip the carry preview in exactly the case it is for.
        base = _fold_checkpoint(canonical, units)
        value = _projection().projection_of(base).value
    except Exception:
        logger.warning("ledger: folding this slot's crew logs failed", exc_info=True)
        # The warm cell is not trusted after a failed fold: the failure may have been a
        # log this build cannot read, and a half-folded state must not become the answer
        # to the next read. Dropping it costs the next read a cold fold.
        _projection().forget_slot_folds(canonical, _FOLD_NAME)
        # NOT previewed here. A refused fold means a reader older than the writer, so
        # the log may hold entries newer than the document -- answering with the
        # document would assert something about a log this build could not read.
        return _empty_state()
    if _has_content(value):
        return value
    # An EMPTY folded record is the upgrade trigger, the same one the write path uses,
    # so the two agree on WHEN a carry is owed as well as on what it carries.
    return _legacy_preview(canonical, base) or value


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
    # REFUSED here rather than reported as recorded. The writer's own ceiling refusal
    # counts the entry and drops it, which arrives as ``durable=False`` -- the same
    # answer a writer that is merely still busy gives, and that one IS recorded and
    # does land. Telling them apart matters because they need opposite answers: a busy
    # writer's update is taken and the caller may proceed, while an entry over the
    # ceiling can never land however often it is retried, so a success would promise a
    # record that never exists and vanishes from the slot at the next read. Only the
    # size is knowable before the append, so only the size is decided here.
    if not crew_log_emit.ledger_entry_fits(data):
        raise LedgerEntryTooLarge(
            "this update does not fit one crew log entry; record fewer or shorter "
            "fields (artifacts are the usual cause)"
        )
    # The stamp the record below is DERIVED from, sampled here rather than after the
    # wait. The store stamps the entry it writes from its own clock, so these are two
    # samples either way -- this one cannot be the log's value. What the position buys
    # is the direction and the size of the gap: taken before the append it is never
    # LATER than the entry the log holds, and the gap is the queue-to-write latency
    # instead of the whole drain, which on a slow writer is the flush budget.
    recorded_ms = int(time.time() * 1000)
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
    drained = crew_log_emit.flush(timeout=_APPEND_FLUSH_SECONDS)
    # THIS UNIT's own log has to have grown, which is the part the process-wide
    # refusal counter cannot say. A counter that did not move proves only that no
    # append anywhere was refused; a file whose newest seq did not move proves that
    # nothing was written HERE, whatever the counter says. Both are required, so the
    # remaining false positive needs a concurrent append into the SAME unit -- the
    # same conversation writing twice at once -- rather than any session anywhere.
    landed = _unit_last_seq(session_id) > seq_before
    durable = drained and landed and crew_log_emit.dropped_writes() == refused_before
    # PUBLISH PRECEDENCE ONLY ONCE THIS UNIT'S LOG HAS ACTUALLY GROWN. This call must
    # stay AFTER ``landed`` is computed, and the order is load-bearing rather than
    # stylistic: the order file's whole claim is "among units that have recorded, the
    # one recording right now holds the newest entry", and a refused or still-queued
    # append means this unit did NOT record. Moving it last on that path pins a
    # RETIRED unit ahead of its successor, and the rewrite is fsynced immediately, so
    # no crash is needed for the wrong order to persist -- a successor resuming with
    # no ledger content of its own then folds the retired unit's stale goal and phase
    # as current and acts on it. That is the inversion this file exists to prevent.
    #
    # Gated on ``landed`` rather than ``durable`` deliberately. ``durable`` also
    # requires ``drained`` and an unmoved refusal counter, and that counter is
    # PROCESS-WIDE, so a concurrent session's refusal would suppress a precedence
    # note this unit had genuinely earned. ``landed`` is exactly the fact the file
    # asserts: this unit's own newest seq moved.
    if landed:
        _note_unit_order(slot_key, session_id)
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
        time=recorded_ms,
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


def _legacy_carry_payload(slot_key: str, legacy: dict[str, Any]) -> dict[str, Any]:
    """The entry a carry appends for *slot_key*, built from its *legacy* document.

    Shared by the carry and by the read below, so the record a reader sees before the
    carry has run and the record it sees afterwards come out of ONE construction. Two
    spellings of this would drift, and the drift would be invisible: the two answers
    are never compared, they are seen minutes apart by the same loop.

    What it carries is the record a resume needs -- goal, phase, next, artifacts, and
    the newest rejected approach -- plus an event that says where it came from and
    names what it could not bring: the entry shape holds ONE ``tried``, so an older
    document's earlier approaches and its event tail are counted in that event rather
    than dropped in silence. Carrying them all would mean an append per row, which is
    unbounded work on a path that runs inside a turn.

    The phase rides with that event, so the carried entry keeps the invariant the
    whole record rests on: a phase never appears without a logged reason.
    """
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
    return data


def _legacy_preview(slot_key: str, base: Any) -> dict[str, Any] | None:
    """The record *slot_key*'s pending carry WOULD fold to, or ``None`` for no carry.

    The read half of the upgrade carry. A carry appends, so it can only run from a
    write: it needs a crew log to append into, it takes an exclusive claim, and it
    refuses its caller when another holder is mid-carry. A READ has none of those --
    :func:`read_state` may be asked with no live session at all, and it must not
    raise -- so the first read after an upgrade would answer the empty record while
    the document sat there waiting for a write to consume it. That read is the
    auto-nudge snapshot, so the first resumed turn would act with no goal, no phase
    and no next step, and the carry that runs later cannot undo a turn already taken.

    So the read FOLDS the pending entry in without appending it, over the same base,
    through the same projection, from the same payload the carry builds. The answer is
    the post-carry record by construction rather than by agreement, and the carry
    stays the only thing that makes it PERMANENT -- which is why this is not a second
    authority: it publishes nothing, decides nothing, and stops answering the moment
    the log has the entry.

    Three things stop it resurrecting state the log has settled. A COMMITTED marker
    ends it, which is what a finished carry leaves and also what a permanent delete
    leaves when it tombstones the slot, so a recycled slot key cannot see a deleted
    conversation's preserved document. A caller only reaches here with an EMPTY folded
    record, so this can never override anything the log recorded. And it folds ONTO
    the base rather than replacing it, so entries that carry events but no content --
    a slot that recorded a reason and no state -- keep them.

    Never raises and never grows past the document: :func:`_legacy_document` refuses a
    file over the size ceiling, and every failure here answers ``None``, which is the
    empty record the caller already had.
    """
    try:
        if _carry_committed(_control_file(slot_key, _CARRIED_FILE)):
            return None
        legacy = _legacy_document(slot_key)
    except LedgerUnavailable:
        # UNREADABLE, which is not empty -- but a read cannot do anything about it and
        # must not raise into a nudge cycle. Warned rather than silent, because the
        # carry is still owed and the next write will report it properly.
        logger.warning("ledger: could not read this slot's legacy document", exc_info=True)
        return None
    except Exception:
        logger.warning("ledger: could not check this slot's legacy document", exc_info=True)
        return None
    if not _has_content(legacy):
        return None
    try:
        from kiro_crew.crew_log.schema import Entry

        projection = _projection()
        pending = Entry(
            type=LEDGER_ENTRY_TYPE,
            # One past what the fold consumed, which is all ``advance`` asks. This entry
            # is never written from this object, only folded -- the carry's own append
            # takes its seq from the store under the store's lock.
            seq=base.last_seq + 1,
            time=int(time.time() * 1000),
            src=_ENTRY_SRC,
            data=_legacy_carry_payload(slot_key, legacy),
        )
        return dict(projection.projection_of(projection.advance(base, (pending,))).value)
    except Exception:
        logger.warning("ledger: could not fold this slot's pending carry", exc_info=True)
        return None


def _carry_legacy_forward(slot_key: str, session_id: str) -> bool:
    """Append the pre-projection document as this slot's first entry. Once.

    Returns whether anything was carried. The caller takes this only when the slot's
    folded record is EMPTY, which is exactly the upgrade path: state written while
    the record was a file of its own, for a workstream still in flight. Once the
    carried entry lands the record has content, so it is never taken again.

    What it appends is :func:`_legacy_carry_payload`'s entry, which a READ also folds
    in unappended so the record does not go dark between the upgrade and this call.
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
    data = _legacy_carry_payload(slot_key, legacy)

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
        # the caller back.
        if drained and not landed:
            # PROVED it can never land: the queue emptied and this unit's own seq did
            # not move, so nothing of this carry is still in flight. RELEASING is what
            # lets the retry carry at once.
            _finish_carry(slot_key, landed=False)
        # Otherwise the append may still be QUEUED -- a flush that ran out of budget
        # leaves work behind, and the process-wide refusal counter cannot say whose
        # write it dropped. Releasing there would let the retry, which still reads an
        # empty fold, carry a SECOND time, and two carried entries are PERMANENT: the
        # fold applies both and nothing dedups them. So the claim stays ``pending``.
        # The retry is answered BUSY until either the queued append lands -- after
        # which the fold is not empty and no carry is attempted at all -- or the claim
        # goes stale after _CARRY_STALE_SECS and a take-over carries, which is safe for
        # that same reason. A refusal bounded by the staleness window is the cheaper
        # side of the trade against history that cannot be repaired.
        raise LedgerUnavailable(
            "this slot's earlier ledger state is still being carried into its crew log; "
            "try the update again"
        )
    try:
        _finish_carry(slot_key, landed=True)
    except (ValueError, OSError):
        # TOLERATED here, unlike in a delete. The append is already proved landed, so
        # the folded record is non-empty and the take-over rule refuses a second carry
        # on its own evidence; the claim left ``pending`` goes stale and is reclaimed.
        # Refusing the update instead would fail a carry that actually succeeded.
        logger.warning(
            "ledger: carried the document but could not commit the marker; the claim "
            "will go stale and the non-empty record refuses a second carry",
            exc_info=True,
        )
    logger.info("ledger: carried a pre-projection document into the crew log")
    return True


class LedgerExclusionError(RuntimeError):
    """A slot's deleted units could not be recorded, so nothing may be deleted."""


class SlotExclusion(NamedTuple):
    """What one delete's exclusion transaction recorded, and so what it may take back.

    Two facts rather than one, because a rollback has two things to undo and each is
    only ever this transaction's: the unit ids it ADDED (an id a concurrent delete of
    the same slot already recorded is not its to remove) and whether it TOMBSTONED the
    slot's legacy document (a marker already committed when it ran is another call's
    proof that a carry landed, and is never its to withdraw).
    """

    added: "tuple[str, ...]"
    carry_tombstoned: bool


def exclude_units(slot_key: str, unit_ids: "tuple[str, ...]") -> SlotExclusion:
    """Record that *unit_ids* must never again be folded into *slot_key*'s record.

    Returns what this call recorded -- see :class:`SlotExclusion`. A delete with NO
    units is NOT nothing to do: the slot's legacy pre-projection document survives the
    delete (the age-based sweep is the only thing that removes it, and it may never
    run), so leaving its carry marker unsettled lets the next session on the recycled
    slot key claim it and carry the DELETED conversation's goal and phase into its own
    record. Only an empty *slot_key* returns early.

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
    neither. The carry tombstone is part of "the record": a slot whose marker is absent
    or still ``pending`` is a CLAIMABLE one, so a delete that proceeded on a failed
    tombstone would leave exactly the resurrection this function exists to prevent.

    A refusal LEAVES THE SLOT AS IT FOUND IT, and does it INSIDE the hold. The exclusion
    write, the tombstone and the take-back are ONE transaction on the slot's own lock,
    which is what makes the take-back safe rather than merely present: settling the
    tombstone after the release would publish the exclusion first, and a concurrent
    delete of the same slot would then reuse those ids, add none of its own, and UNLINK
    its transcript on the strength of an exclusion this call was about to withdraw --
    resurrecting the state that delete removed. From inside the hold no other delete can
    observe the intermediate state, so there is no reliance to break.
    """
    if not slot_key:
        return SlotExclusion((), False)
    added: "tuple[str, ...]" = ()
    try:
        path = _control_file(slot_key, _DELETED_UNITS_FILE, create=True)
        carried = _control_file(slot_key, _CARRIED_FILE)
        with _locked(control_dir(slot_key)):
            current = _read_lines(path, limit=_MAX_EXCLUDED_BYTES, reject_oversized=True)
            added = tuple(
                _retainable_unit(unit) for unit in dict.fromkeys(unit_ids) if unit not in current
            )
            # An EMPTY ``added`` is not nothing to do. It is the retry of a delete that
            # already recorded these units and then failed further on, so the work below
            # is done -- but the carry claim still has to be consumed, which is why that
            # happens after this block for both paths rather than behind an early return.
            if added:
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
            # The carry claim is CONSUMED by the delete, whatever state it was in. The
            # take-over rule rests on "a carry that landed makes the folded record
            # non-empty", and a delete removes the unit that held the carried entry -- so
            # after it the record is empty again and a marker left `pending` by a crash
            # between the append and its commit would let a later session take the claim
            # over and carry the preserved document into the recycled slot. That is the
            # resurrection the marker exists to prevent, so the delete settles the marker
            # rather than leaving evidence its own removal invalidated. A delete with no
            # units reaches here too, and MUST: the legacy document it tombstones is the
            # slot's, not any one unit's.
            #
            # INSIDE the hold, which is what makes this one transaction. Settling it
            # after the release would publish the exclusion first, and a concurrent
            # delete of the same slot would then reuse those ids, add none of its own,
            # settle the marker, and UNLINK its transcript on the strength of an
            # exclusion this call is about to take back -- resurrecting the state it
            # deleted. Nobody can observe the intermediate state from in here.
            try:
                tombstoned = _settle_carry_locked(carried, landed=True)
            except (ValueError, OSError):
                # ROLLED BACK to the set this transaction FOUND, not by subtracting the
                # ids it added: inside the hold those are the same thing, and writing
                # back what was read cannot express anything else. The delete is refused
                # either way, so a rollback that does not persist is reported rather
                # than raised over the original failure.
                if added:
                    _rewrite_lines(path, current)
                    if set(added) & set(
                        _read_lines(path, limit=_MAX_EXCLUDED_BYTES, reject_oversized=True)
                    ):
                        logger.error(
                            "ledger: could not take back slot %r's exclusions %s after "
                            "the delete was refused; a live session's record will read "
                            "empty until they are removed from %r by hand",
                            slot_key,
                            list(added),
                            _DELETED_UNITS_FILE,
                        )
                raise
        return SlotExclusion(added, tombstoned)
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


def unexclude_units(
    slot_key: str, unit_ids: "tuple[str, ...]", *, restore_carry: bool = False
) -> None:
    """Undo :func:`exclude_units` for *unit_ids*, for a delete that did not happen.

    Takes ONLY the ids that call reported adding. A rollback that dropped every id it
    was asked about would take back a concurrent delete's exclusions too, and that
    delete's units would then fold into the next session on the recycled slot key.

    With *restore_carry* -- passed only when that transaction reported TOMBSTONING this
    slot's legacy document -- the tombstone comes back out as well, because the session
    it was meant to silence still exists and its pre-projection state is still owed to
    it. A committed marker is otherwise permanent, so leaving one behind for a delete
    that did not happen would silence that live session's earlier state for good.

    The tombstone is lifted ONLY when the rewrite above leaves the slot with no
    exclusions at all. An id still recorded there is another delete's evidence that it
    proceeded, and the legacy document is the SLOT's rather than any one conversation's,
    so it must stay tombstoned. RESIDUE, named rather than implied: two concurrent
    deletes of one recycled slot key that both record no units, one of them rolling
    back, cannot be told apart this way, and the rollback lifts the other's tombstone.

    The exclusion is written BEFORE the transcript is unlinked, because that is the
    only point where a failure can still refuse something. When the delete then does
    not proceed, those units belong to a session that still exists and folding them is
    correct, so the exclusion has to come back out. The rewrite and the tombstone
    withdrawal share ONE hold on the slot's lock, and the rollback runs on the rare
    path only. Two holds would leave a gap in which a concurrent delete commits its own
    tombstone, and the withdrawal cannot tell that marker from the one it is undoing.

    A rollback that itself fails is reported at ERROR naming the file, and leaves a
    live slot's record reading empty until an operator edits it -- recoverable by hand,
    which the alternative ordering is not. A failed WITHDRAWAL is raised for the same
    reason rather than logged and dropped: the marker standing means the spared
    session's earlier state is never carried, and only this path ever removes one.
    """
    if not slot_key or not (unit_ids or restore_carry):
        return
    drop = set(unit_ids)
    try:
        if not control_dir(slot_key).exists():
            # Nothing was ever written for this slot, so there is nothing to take back
            # and no marker to withdraw. Returning here also keeps the lock acquire
            # below from bringing the directory into being on a pure rollback.
            return
        path = _control_file(slot_key, _DELETED_UNITS_FILE)
        carried = _control_file(slot_key, _CARRIED_FILE)
        # ONE hold over both writes, because two would leave a gap in which a concurrent
        # delete commits its OWN tombstone -- which the withdrawal cannot tell from the
        # one it is undoing, since a marker says `committed` and never says whose.
        # Removing that marker carries the other delete's legacy state into a recycled
        # slot, silently, with nothing left to put it back.
        with _locked(control_dir(slot_key), create=False):
            kept: "tuple[str, ...]" = ()
            if path.exists():
                kept = tuple(
                    unit
                    for unit in _read_lines(path, limit=_MAX_EXCLUDED_BYTES, reject_oversized=True)
                    if unit not in drop
                )
                _rewrite_lines(path, kept)
            if restore_carry and not kept:
                # NOT ``_finish_carry(landed=False)``: that releases a claim still
                # reading ``pending`` and deliberately leaves a committed marker alone,
                # so it would be inert against the very tombstone this is undoing.
                _withdraw_tombstone_locked(carried)
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


def _retainable_unit(unit_id: str) -> str:
    """*unit_id* if it is within :data:`_MAX_UNIT_ID_BYTES`, else RAISE.

    The bound is enforced HERE, at the boundary, rather than trusted of the input.
    Every control-file bound is derived from it, so an id past it makes a file outgrow
    the read sized for it -- and the order file's read does not reject an oversized
    file, it comes back short, while its caller rewrites what it read. So an unchecked
    id does not cost a bounded read: it costs the NEWEST ids permanently, and the
    caller then folds a retired session's record over a later one's.

    Refusing is the safe direction at both call sites. An exclusion turns it into a
    refused delete, which is what that file's count bound already does. The order log
    turns it into a missing line, which falls back to header order -- the same state a
    crash between the entry and the line leaves.
    """
    if len(unit_id.encode("utf-8")) > _MAX_UNIT_ID_BYTES:
        raise ValueError(
            f"unit id is {len(unit_id.encode('utf-8'))} bytes, over the "
            f"{_MAX_UNIT_ID_BYTES}-byte bound every control-file bound is derived from"
        )
    return unit_id


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
            if _carry_committed(path):
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


def _carry_committed(path: Path) -> bool:
    """True when *path* holds a COMMITTED carry marker.

    One reader for both the claim and the release, so the two cannot come to
    different conclusions about the same bytes. A marker that is absent, empty, or
    half-written is NOT committed: the claim treats it as takeable-when-stale, and
    the release is then free to remove it.
    """
    try:
        return path.read_text(encoding="utf-8").strip() == _CARRY_COMMITTED
    except FileNotFoundError:
        return False


def _settle_carry_locked(path: Path, *, landed: bool) -> bool:
    """:func:`_finish_carry`'s decision, for a caller ALREADY holding the slot's lock.

    Split out so a delete can settle the marker inside the SAME hold as its exclusion
    write. The lock is not re-entrant -- a second acquire from this process blocks on
    its own holder -- so a caller inside the hold cannot go through
    :func:`_finish_carry`, and being inside it is what makes the two writes one
    transaction: a concurrent delete cannot observe a half-finished one, so a rollback
    can never take back an id another delete has already proceeded on.

    Raises on a commit that does not reach disk, for the reason :func:`_finish_carry`
    documents: ``False`` already means "already committed by another call".
    """
    if landed:
        # READ BEFORE WRITING, inside the hold: the answer is what makes a
        # rollback's take-back exact, and a blind write cannot tell "this call
        # tombstoned the slot" from "it was already committed".
        if _carry_committed(path):
            return False
        # FSYNCED and renamed into place, like every other control write here.
        # A marker that is only in the page cache is not a tombstone: the crash
        # it guards against is exactly the one that would lose it.
        _rewrite_lines(path, (_CARRY_COMMITTED,))
        # Read back INSIDE the hold, like the exclusion write. A short write on
        # a full filesystem would otherwise leave the slot claimable while this
        # call reported it tombstoned.
        if not _carry_committed(path):
            raise OSError("the carry marker did not persist as committed")
        return True
    if not _carry_committed(path):
        path.unlink(missing_ok=True)
    return False


def _finish_carry(slot_key: str, *, landed: bool) -> bool:
    """Mark this slot's carry claim ``committed``, or RELEASE it when it did not land.

    Answers whether THIS call turned a not-committed marker into a committed one. Only
    that caller may ever take it back: a marker already committed when this ran is
    another call's proof, and one written later is another call's tombstone.

    Committing only after the append is proved is what keeps a crashed carry
    retryable: a marker published first would make the loss permanent, because the
    claim it left behind would refuse every later attempt at state that was never
    carried. Releasing an unlanded claim is the same property, taken immediately
    rather than waiting out the staleness window.

    A release only ever removes a marker that still reads ``pending``, and both
    branches run under the slot's own lock so that check cannot be raced. A COMMITTED
    marker is another call's proof that the carry landed, and it is never this call's
    to withdraw: an absent marker is a claimable one, so removing it would let the
    next session on a recycled slot key carry a deleted conversation's document in.
    Unserializing either branch would defeat the guard -- a release that read
    ``pending`` while an unlocked commit was in flight would unlink the marker that
    commit had just written.

    RAISES when a COMMIT does not reach disk, because ``False`` cannot carry that: it
    already means "already committed by another call", which is a slot that IS
    tombstoned, while a failed write leaves one that is still claimable. A caller that
    cannot tell those apart proceeds on the wrong one, so the two answers are split
    between a return value and an exception. A failed RELEASE is swallowed as before:
    it only leaves the claim ``pending``, which goes stale and is taken over.
    """
    try:
        path = _control_file(slot_key, _CARRIED_FILE)
        with _locked(control_dir(slot_key)):
            return _settle_carry_locked(path, landed=landed)
    except (ValueError, OSError):
        if landed:
            # NOT swallowed: an absent or pending marker is a CLAIMABLE one, so a
            # delete that went ahead on it would let the next session on this recycled
            # slot key carry the deleted conversation's document in. The delete's
            # caller turns this into a refused delete, mirroring the exclusion write;
            # the carry path keeps its own tolerance at its own call site, where the
            # append has already landed and the stale claim is safe to leave.
            logger.error(
                "ledger: could NOT commit slot %r's legacy-document marker", slot_key, exc_info=True
            )
            raise
        # The claim stays ``pending`` and goes stale, which a later call takes over.
        logger.warning(
            "ledger: could not finish slot %r's legacy-document claim", slot_key, exc_info=True
        )
    return False


def _withdraw_tombstone_locked(path: Path) -> None:
    """Remove a COMMITTED carry marker, for a caller ALREADY holding the slot's lock.

    The exact inverse of the tombstone :func:`exclude_units` writes, and the only thing
    in this module that removes a committed marker: :func:`_finish_carry` refuses to,
    because a committed marker is normally another call's proof that a carry landed. The
    caller earns this by having reported writing it and by finding no exclusion left on
    the slot.

    Split out for the same reason :func:`_settle_carry_locked` is: the lock is not
    re-entrant, and the withdrawal has to happen in the SAME hold as the exclusion
    rewrite that earns it. Two separate holds leave a window in which a concurrent
    delete commits its own tombstone, and this cannot tell that marker from the one it
    is undoing -- ``_carry_committed`` answers what the marker SAYS, never whose it is.

    RAISES rather than swallowing, because the marker standing means the spared
    session's earlier goal and phase are never carried, and only this function ever
    removes a committed marker. Swallowing makes that loss permanent and silent; the
    caller turns a raise into a retryable answer, which can actually succeed.
    """
    if _carry_committed(path):
        path.unlink(missing_ok=True)
        if _carry_committed(path):
            raise OSError("the carry tombstone did not withdraw")


def _note_unit_order(slot_key: str, session_id: str, *, order_file: str = _UNIT_ORDER_FILE) -> None:
    """Record that *session_id* recorded into *slot_key*, and that it recorded LAST.

    The fold reads units in this order and applies a later update over an earlier one,
    so what it needs from this file is "which unit holds the newest entry". That is why
    the order is written by the call that records rather than derived from a header's
    clock -- but it is also why merely being PRESENT is not enough.

    Appending on first record alone orders units by their FIRST record, which is not
    causal for a unit whose first record arrives late: a request from a session the slot
    has since replaced can reach here after the successor has already recorded, and it
    would then be appended AFTER the successor and stay there, because the presence
    check never revisits a known id. The retired unit's phase and next step would win
    every later fold, permanently.

    So the recording unit is moved to the END when it is not already there. The file
    then orders units by their NEWEST record, which is exactly the question the fold
    asks, and the answer is an observation rather than a claim: among units that have
    recorded, the one recording right now holds the newest entry, whatever any clock
    says. The ordinary case -- one live unit recording repeatedly -- is already last,
    so it stays a bare read.

    SERIALIZED on the slot's lock, because a move is a read-modify-write and an
    unlocked one would drop a concurrent first record's append.

    Best-effort: a slot whose order cannot be written falls back to header order, which
    is what every slot did before this existed.
    """
    if not slot_key or not session_id:
        return
    try:
        path = _control_file(slot_key, order_file, create=True)
        with _locked(control_dir(slot_key)):
            known = _recorded_unit_order(slot_key, order_file=order_file)
            if known and known[-1] == session_id:
                return
            if session_id in known:
                # MOVED, not left where its first record put it. Rewritten rather than
                # appended, because appending a known id would put the same unit in the
                # fold's order twice and fold its entries twice.
                _rewrite_lines(
                    path,
                    tuple(unit for unit in known if unit != session_id)
                    + (_retainable_unit(session_id),),
                )
                return
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"{_retainable_unit(session_id)}\n")
                # FSYNCED before the update is acknowledged. The fallback for a missing
                # line is header order, and a backward clock step is exactly what that
                # fallback gets wrong -- so a crash that keeps the ledger entry and loses
                # this line restores a retired session's goal and phase over a later
                # one's.
                fh.flush()
                os.fsync(fh.fileno())
            # COMPACTED when the file outgrows the window, so the bound bounds the disk
            # and the read rather than only the answer. The dedup check sees the window
            # alone, so a slot past the cap re-appends ids that fell out of it and the
            # file would otherwise grow without limit. Rewriting on a threshold rather
            # than on every record keeps the ordinary path a bare append: the rewrite is
            # one file in every `_MAX_ORDERED_UNITS` writes at worst.
            if path.stat().st_size > _MAX_ORDER_BYTES:
                kept = (*known[-(_MAX_ORDERED_UNITS - 1) :], session_id)
                _rewrite_lines(path, kept)
    except (ValueError, OSError):
        logger.warning("ledger: could not record this slot's unit order", exc_info=True)


def note_work_unit_recorded(slot_key: str, session_id: str) -> None:
    """Publish that *session_id* just appended a work record under its slot."""
    _note_unit_order(
        canonical_slot(slot_key, session_id),
        session_id,
        order_file=_WORK_UNIT_ORDER_FILE,
    )


def work_crew_log_units(slot_key: str) -> tuple[str, ...]:
    """Work-record units for *slot_key* in causal append order, oldest first.

    Units absent from the bounded order tail predate every retained unit and fold
    first. Listing failures fail closed because this runs on a loop-cycle read path.
    """
    if not slot_key:
        return ()
    try:
        from kiro_crew.crew_log.store import session_units_for_slot

        units = session_units_for_slot(slot_key)
        recorded = _recorded_unit_order(slot_key, order_file=_WORK_UNIT_ORDER_FILE)
        if recorded:
            known = [unit for unit in recorded if unit in units]
            rest = [unit for unit in units if unit not in recorded]
            units = tuple(rest + known)
        return units
    except Exception:
        logger.warning("work ledger: could not list this slot's crew logs", exc_info=True)
        return ()


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


def _recorded_unit_order(slot_key: str, *, order_file: str = _UNIT_ORDER_FILE) -> "tuple[str, ...]":
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
        path = _control_file(slot_key, order_file)
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

    Read by the carry above and by its read-side preview, and by nothing else. Both
    exist to RETIRE the file rather than to consult it: the preview answers only while
    the carry is still owed and only when the folded record is empty, so this file can
    never contradict something the log recorded, and it stops being read the moment
    the carry's marker commits. Size ceiling before parse, like every other reader of
    it, so a damaged or hostile file cannot make an upgrade allocate its size.
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
