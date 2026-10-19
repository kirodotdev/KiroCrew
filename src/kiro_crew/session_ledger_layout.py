"""The session ledger's vocabulary, its on-disk layout and its store lock.

The entry type and fold name, every record and control-file bound and file name (the
snapshot's bounds stay beside ``render_snapshot`` in the facade), the two errors a write
raises, the exact-key identity (:func:`ledger_key`, :func:`_store_name`), the
symlink-safe store and control paths (:func:`resolved_within`, :func:`ledger_dir`,
:func:`control_dir`), :func:`canonical_slot`, the field clamp, the bounded store lock
(:func:`require_lock_inode`, :func:`_locked`) and the empty record.

An owner module of :mod:`kiro_crew.session_ledger`, the one import path: callers, tests and
documents reach every name here as ``kiro_crew.session_ledger.<name>``, and a patch through
that facade reaches this module (:mod:`kiro_crew._facade_mirror`). It imports no other
owner, and nothing here imports the facade. A new record bound or control-file name goes
here.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from kiro_crew.config.paths import data_home
from kiro_crew.platform_compat import (
    make_owner_only_dir,
    release_lock,
    strip_extended_length_prefix,
    try_acquire_lock,
)

SCHEMA_VERSION = 1

#: The crew log entry every :func:`record` call appends, and the only type the
#: ``ledger`` fold interprets. Declared in ``crew_log.entry_types``; spelled here
#: because ``session_ledger`` is the WRITER and the writer owns the type it produces.
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

#: How many times :func:`_unit_last_seq` opens a unit's log that refused to open, and
#: how long it waits between opens: about half a second in all, the window
#: ``atomic_write`` budgets for the same Windows hold -- an indexer or scanner handle on
#: a file that was just written. A heuristic shared with that one, not a measured
#: hold-time distribution.
_SEQ_READ_ATTEMPTS = 10
_SEQ_READ_BACKOFF_SECS = 0.05

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


# Tracebacks and logs name both errors by the import path their callers use.
LedgerUnavailable.__module__ = "kiro_crew.session_ledger"
LedgerEntryTooLarge.__module__ = "kiro_crew.session_ledger"


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
#: Longest unit id the ledger RETAINS. Enforced at every site that writes an id into
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
#: Panel publishes need a third, for the same reason work records do: sharing either
#: order would let an append that is not a publish move a unit whose newest PANEL is
#: older past the unit holding the newest one, and the panel fold takes the newest
#: entry whole.
_PANEL_UNIT_ORDER_FILE = "panel-unit-order"
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
        # Owner-only at the DIRECTORY, so every control file and every temp a rewrite
        # stages inside it is covered without each writer choosing a mode.
        make_owner_only_dir(directory)
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
