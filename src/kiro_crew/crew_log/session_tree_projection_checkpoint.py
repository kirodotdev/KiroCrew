"""The session tree projection's checkpoint: what it holds, and how it is read and written.

The checkpoint is a fold shortcut, never an authority (see
:mod:`kiro_crew.crew_log.session_tree_projection`). This module holds its file name and
version, the store identity a payload is bound to (``_current_root``), the row codec
with the string bounds every door into the state applies (``_within_bounds``,
``_edge_within_bounds``), the load that discards whatever it cannot prove
(``_load_checkpoint``), the atomic save, and the debounced write that brings the file up
to date with the state. Each function here whose first parameter is ``self`` is bound as
the ``SessionTreeProjection`` attribute of the same name in
:mod:`kiro_crew.crew_log.session_tree_projection`, which keeps the projection's state,
so ``self.<name>(...)`` calls and instance and class patches reach it as they reach a
method defined in the class body. They read ``_cancel_epoch``, ``_dirty``, ``_edges``,
``_lock``, ``_records``, ``_root``, ``_scans``, ``_seed_retry_timer``, ``_spawn_scans``,
``_spawned`` and ``_write_scheduled``, and write ``_cancel_epoch``, ``_dirty``,
``_seed_retry_timer`` and ``_write_scheduled``. New checkpoint work goes here.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from kiro_crew.crew_log.checkpoint import CHECKPOINT_DIR, MAX_CHECKPOINT_BYTES
from kiro_crew.crew_log.schema import KIND_SESSION
from kiro_crew.crew_log.session_tree import EdgeRecord, OpenedRecord
from kiro_crew.crew_log.store import log_exception_text
from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN, MAX_SHORT_STRING

if TYPE_CHECKING:
    from kiro_crew.crew_log.session_tree_projection import SessionTreeProjection

logger = logging.getLogger("kiro_crew.crew_log.session_tree_projection")


#: Checkpoint file name, under the store root's ``projections`` directory. Store-level
#: rather than per-unit (``checkpoint.checkpoint_path``) because this fold is over the
#: COLLECTION of session logs, not over one of them -- the one projection in this
#: package that is, which is why it gets its own file rather than a name in a unit's
#: bundle.
CHECKPOINT_NAME: Final[str] = "session-tree.json"
#: Serialized-state version. Bump when the stored fields or the fold's semantics
#: change: a mismatch DISCARDS the file and falls back to a cold rebuild, which costs
#: one scan and is always correct. Migrating a checkpoint would mean trusting an older
#: build's reading of a rule this build may have changed.
#: Bumped when the payload's shape changes. A mismatch DISCARDS the file rather than
#: migrating it, so an older build's checkpoint -- which carries no ``root`` and so
#: cannot be proven to describe this store, or no ``edges`` and so cannot be proven to
#: hold the store's adoptions -- is rebuilt from the log instead. The ``edges`` half is
#: why this is a bump rather than a key read additively: a file written before
#: adoptions existed would load as "no session has ever been adopted", which looks
#: exactly like the truth and is wrong.
CHECKPOINT_VERSION: Final[int] = 5


#: One unit's log identity for the scan cache: the directory's mtime, its segment count,
#: and the newest segment's size and mtime. Named because it appears in the state, in the
#: checkpoint and in two signatures, and four bare ints in a tuple say nothing at a call
#: site about which four.
ScanIdentity = tuple[int, int, int, int]

#: What a checkpoint load yields, and what a tail replay yields on top of it. Aliased for
#: the same reason: a five-place tuple in a signature is not readable at the call site.
_LoadedCheckpoint = tuple[
    "dict[str, OpenedRecord]",
    "dict[tuple[str, str], EdgeRecord]",
    "dict[str, ScanIdentity]",
    "dict[tuple[str, str], EdgeRecord]",
    "dict[str, ScanIdentity]",
]
_ReplayResult = tuple[
    "dict[str, OpenedRecord]",
    "dict[tuple[str, str], EdgeRecord]",
    "dict[str, ScanIdentity]",
    bool,
    bool,
]

#: How long a dirty projection waits before its checkpoint is written, in seconds. A
#: debounce, not a delay: a burst of session creations coalesces into ONE write. Short
#: enough that an ordinary shutdown leaves almost nothing for the tail replay to pick
#: up, and a longer gap costs only that replay.
CHECKPOINT_DEBOUNCE_SECS: Final[float] = 1.0


def _checkpoint_path() -> Path:
    """Where the checkpoint lives. Does not create anything, and never raises here --
    the caller's own try/except owns every failure, because a checkpoint that cannot be
    located is exactly as recoverable as one that cannot be parsed: rebuild."""
    from kiro_crew.crew_log.store import crew_log_root

    return crew_log_root(KIND_SESSION) / CHECKPOINT_DIR / CHECKPOINT_NAME


def _current_root() -> str:
    """The store root this process would read right now, as a string, or ``""``.

    Path arithmetic over the data home, not I/O, so it is cheap enough to check on
    every read. It is the projection's store IDENTITY: the fold is the in-memory image
    of ONE store, and a home that moves under the process must re-seed rather than keep
    answering from the old one.
    """
    try:
        from kiro_crew.crew_log.store import crew_log_root

        return str(crew_log_root(KIND_SESSION))
    except Exception:
        return ""


def _record_to_json(record: OpenedRecord) -> dict[str, Any]:
    """One record as plain JSON. Keys are short because the file holds one per unit."""
    out: dict[str, Any] = {
        "sid": record.sid,
        "slot": record.slot,
        "at": record.created_at,
    }
    # Omitted rather than written as null, the same distinction the emitter keeps on the
    # entry itself: "no creator recorded" and "a creator recorded as nothing" are
    # different facts, and only the first is a thing that happens.
    if record.parent_slot:
        out["parent"] = record.parent_slot
    if record.previous_sid:
        out["prev"] = record.previous_sid
    return out


def _within_bounds(record: OpenedRecord) -> bool:
    """Whether every RETAINED string on *record* is within its bound.

    One rule for every door into the state, because the cost is the same at each: these
    strings live in memory for as long as the projection does, up to ``TREE_UNIT_CAP``
    records, and they are written into the checkpoint. The limits are the ones the cold
    scanner already applies to a unit's head -- ``MAX_ACP_SESSION_ID_LEN`` for an ACP
    session id, ``MAX_SHORT_STRING`` for a slot key -- so no path admits a value another
    path would refuse.

    Callers REFUSE the record rather than truncating it. A truncated id or slot key is a
    DIFFERENT key: it matches nothing, or it matches another session.
    """
    if len(record.sid) > MAX_ACP_SESSION_ID_LEN:
        return False
    if len(record.slot) > MAX_SHORT_STRING:
        return False
    if record.parent_slot is not None and len(record.parent_slot) > MAX_SHORT_STRING:
        return False
    if record.previous_sid is not None and len(record.previous_sid) > MAX_ACP_SESSION_ID_LEN:
        return False
    return True


def _edge_within_bounds(edge: EdgeRecord) -> bool:
    """Whether every RETAINED string on *edge* is within its bound.

    The same limits :func:`_within_bounds` applies, for the same reason and against
    the same risk: these strings are held for as long as the projection lives and are
    written into the checkpoint. The slot keys take ``MAX_SHORT_STRING`` and the
    citing log's id takes ``MAX_ACP_SESSION_ID_LEN``, which is what the scanner
    applies to the same two values on a unit's head.
    """
    if len(edge.slot) > MAX_SHORT_STRING:
        return False
    if edge.parent_slot is not None and len(edge.parent_slot) > MAX_SHORT_STRING:
        return False
    if len(edge.sid) > MAX_ACP_SESSION_ID_LEN:
        return False
    return True


def _record_from_json(raw: Any) -> OpenedRecord | None:
    """One record from the checkpoint, or ``None`` when it is not one.

    Every field is type-checked rather than coerced. The file is disposable, so a row
    this cannot read costs a re-read of that unit's head on the replay; a row coerced
    into the wrong shape would instead be folded as though it had been read.

    BOUNDED, on the same terms and with the same limits the scanner applies to a unit's
    head: these strings are RETAINED for as long as the projection lives, so the
    checkpoint cannot be the one path that admits a value the scanner would refuse.
    Bounds refuse the whole record rather than truncating it -- a truncated id or slot
    key is a DIFFERENT key, which matches nothing or, worse, matches another session.
    The checkpoint is written from folded records and so should never carry one, but it
    is a plain file under the data home: what makes it safe to load is this check, not
    its provenance.
    """
    if not isinstance(raw, dict):
        return None
    sid = raw.get("sid")
    slot = raw.get("slot")
    at = raw.get("at")
    parent = raw.get("parent")
    previous = raw.get("prev")
    if not isinstance(sid, str) or not sid:
        return None
    if not isinstance(slot, str):
        return None
    if isinstance(at, bool) or not isinstance(at, int):
        return None
    if parent is not None and not isinstance(parent, str):
        return None
    if previous is not None and not isinstance(previous, str):
        return None
    record = OpenedRecord(
        sid=sid,
        slot=slot,
        created_at=at,
        parent_slot=parent or None,
        previous_sid=previous or None,
    )
    return record if _within_bounds(record) else None


def _edge_to_json(edge: EdgeRecord) -> dict[str, Any]:
    """One decision as plain JSON. Short keys, one row per adopted slot.

    ``parent`` is OMITTED for a release rather than written as null, the same
    distinction the entry itself keeps: a decision with no parent is the release, and
    a key that is absent cannot be confused with a key whose value failed to load.
    """
    out: dict[str, Any] = {
        "slot": edge.slot,
        "at": edge.at,
        "sid": edge.sid,
        "seq": edge.seq,
    }
    if edge.parent_slot:
        out["parent"] = edge.parent_slot
    return out


def _edge_from_json(raw: Any) -> EdgeRecord | None:
    """One decision from the checkpoint, or ``None`` when it is not one.

    Type-checked rather than coerced, and BOUNDED on the same limits every other
    door into this state applies, for the reason :func:`_record_from_json` gives: the
    file is a plain file under the data home, and what makes it safe to load is this
    check rather than its provenance. A row this cannot read costs that slot's
    decision until the next cold scan, which is why it is dropped rather than
    guessed at.
    """
    if not isinstance(raw, dict):
        return None
    slot = raw.get("slot")
    at = raw.get("at")
    sid = raw.get("sid")
    parent = raw.get("parent")
    if not isinstance(slot, str) or not slot or len(slot) > MAX_SHORT_STRING:
        return None
    if isinstance(at, bool) or not isinstance(at, int):
        return None
    if not isinstance(sid, str) or not sid or len(sid) > MAX_ACP_SESSION_ID_LEN:
        return None
    if parent is not None and (not isinstance(parent, str) or len(parent) > MAX_SHORT_STRING):
        return None
    seq = raw.get("seq", 0)
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        return None
    return EdgeRecord(slot=slot, parent_slot=parent or None, at=at, sid=sid, seq=seq)


def _schedule_checkpoint(self: SessionTreeProjection) -> None:
    """Arm ONE debounced background write. Never raises, never blocks the caller.

    The caller is the emitter's thread finishing an append, or a removal path, so
    the write must not happen inline: it is a file write on a path neither of those
    is waiting for. The scheduling flag is what makes a burst of session creations
    cost one write rather than one per session.

    A pool that will not accept the job is not an error worth reporting: the
    checkpoint is a shortcut, so the cost of never writing it is a longer tail
    replay next time.
    """
    with self._lock:
        if self._write_scheduled or not self._dirty:
            return
        self._write_scheduled = True
        epoch = self._cancel_epoch
        # Resolved HERE rather than in the worker. The path comes from the
        # environment, and the worker resolves nothing until a debounce has
        # elapsed -- by which time the data home can be a different one (a pod, a
        # relocated home, a test that repointed it). Pinning it at arm time is what
        # makes the write land in the store this state was folded from, or nowhere.
        target = _checkpoint_path()
    try:
        from kiro_crew.executors import maintenance_executor

        maintenance_executor().submit(self._debounced_write, target, epoch)
    except Exception:
        # Including a pool shut down during teardown, which is ordinary at exit.
        with self._lock:
            self._write_scheduled = False
        # Rendered text, never ``exc_info``. This frame holds no handle, but its
        # CALLER chain can: ``apply`` <- ``record_opened`` <- the emitter's edge
        # recorder, whose ``log`` is a live ``CrewLog``. A retained traceback reaches
        # that frame through ``tb_frame.f_back`` and keeps the handle, and its write
        # lease, alive past the drop that should have released it. Pinned by
        # test_crew_log_session_tree_projection.py.
        log_exception_text(logger, logging.DEBUG, "session tree checkpoint could not be scheduled")


def cancel_pending_checkpoint(self: SessionTreeProjection) -> None:
    """Abandon a debounced write that has not fired yet. Never blocks, never raises.

    For a caller discarding this projection -- a test tearing down its data home, a
    shutdown -- that must not leave a worker sleeping on the maintenance pool with a
    write still owed. The worker is not interrupted (nothing here joins a pool
    thread); it is made a no-op, so it wakes, sees the epoch moved, and returns.

    ``dirty`` is deliberately left alone. Cancelling says "not this write", not
    "the state is saved": a projection that keeps being used re-arms on its next
    change, and clearing the flag here would silently forfeit that write instead.
    """
    with self._lock:
        self._cancel_epoch += 1
        timer = self._seed_retry_timer
        self._seed_retry_timer = None
    if timer is not None:
        # Cancelled outright: unlike a pool worker, a timer can be, and a
        # discarded projection should not keep a thread for its cooldown.
        timer.cancel()


def _debounced_write(
    self: SessionTreeProjection, target: "Path | None" = None, epoch: int = -1
) -> None:
    """Wait out the debounce, then write once. Runs on the maintenance pool.

    It writes the checkpoint; it does not scan. That distinction is the whole
    reason a background task is acceptable here at all.

    ``target`` is the path resolved when this was armed and ``epoch`` the
    cancellation generation then current; a mismatch against the live epoch means
    the write was abandoned while this waited, so it returns having written nothing.
    """
    try:
        time.sleep(CHECKPOINT_DEBOUNCE_SECS)
    except Exception:  # pragma: no cover -- defensive
        pass
    with self._lock:
        self._write_scheduled = False
        if epoch != self._cancel_epoch:
            return
        if not self._dirty:
            return
        payload = {
            "ver": CHECKPOINT_VERSION,
            "written_at": int(time.time() * 1000),
            # The store these records describe, written INTO the file so a reader
            # can prove it rather than assume it. The target alone cannot: it is
            # pinned when the write is armed, and the payload is built here.
            "root": self._root or _current_root(),
            "records": [_record_to_json(r) for r in self._records.values()],
            "edges": [_edge_to_json(e) for e in self._edges.values()],
            # The unit identities at which a complete read found NO decision, so the
            # next boot can skip re-deriving that per unit. A cache, never evidence:
            # see ``_scans``.
            "scans": {sid: list(ident) for sid, ident in self._scans.items()},
            # The creators' records of the sessions they created, and the unit
            # identities those were read at (see ``_spawned``, ``_spawn_scans``).
            "spawned": [_edge_to_json(s) for s in self._spawned.values()],
            "spawn_scans": {sid: list(ident) for sid, ident in self._spawn_scans.items()},
        }
        # Cleared BEFORE the write, and deliberately: an apply landing during it
        # re-dirties the state and arms another write, where clearing after would
        # let that apply be swallowed by this one's completion.
        self._dirty = False
    if not _save_checkpoint(payload, target):
        # Fail-soft: the state in memory is still right, and the cost of a lost
        # write is a longer tail replay on the next cold start. Re-dirtied so a
        # later change tries again rather than leaving the file permanently behind.
        with self._lock:
            if epoch == self._cancel_epoch:
                self._dirty = True


def flush_checkpoint(self: SessionTreeProjection) -> bool:
    """Write the checkpoint NOW, skipping the debounce. Returns whether it wrote.

    For a caller that wants the shortcut on disk at a chosen moment -- a test, or a
    deliberate shutdown -- rather than whenever the debounce elapses.
    """
    with self._lock:
        if not self._records and not self._edges and not self._spawned and not self._dirty:
            return False
        payload = {
            "ver": CHECKPOINT_VERSION,
            "written_at": int(time.time() * 1000),
            "root": self._root or _current_root(),
            "records": [_record_to_json(r) for r in self._records.values()],
            "edges": [_edge_to_json(e) for e in self._edges.values()],
            # The unit identities at which a complete read found NO decision, so the
            # next boot can skip re-deriving that per unit. A cache, never evidence:
            # see ``_scans``.
            "scans": {sid: list(ident) for sid, ident in self._scans.items()},
            # The creators' records of the sessions they created, and the unit
            # identities those were read at (see ``_spawned``, ``_spawn_scans``).
            "spawned": [_edge_to_json(s) for s in self._spawned.values()],
            "spawn_scans": {sid: list(ident) for sid, ident in self._spawn_scans.items()},
        }
        self._dirty = False
        # Pinned from the SAME locked block that built the payload. Resolving it
        # inside the write would let a home that moves in between send these
        # records to a store they do not describe.
        target = _checkpoint_path()
    wrote = _save_checkpoint(payload, target)
    if not wrote:
        with self._lock:
            self._dirty = True
    return wrote


def _load_checkpoint() -> "_LoadedCheckpoint | None":
    """The checkpoint's records, decisions and scan cache, or ``None`` when there is no
    usable one.

    ``None`` is every failure, undifferentiated on purpose: absent, unreadable,
    oversized, unparseable, wrong ``ver``, or a payload whose shape this does not
    recognise all mean the same thing to the caller -- rebuild, which is always
    correct. Never raises, and never migrates: a ``ver`` mismatch is DISCARDED, because
    forward-applying an older build's state is how a fold quietly becomes garbage.

    The two are returned together, from one read, for the reason
    :class:`~kiro_crew.crew_log.session_tree.TreeReading` carries its flag: they are
    halves of one state, and a caller that could load the records and ask for the
    decisions separately could fold a tree from two different moments.
    """
    try:
        path = _checkpoint_path()
        size = path.stat().st_size
        if size > MAX_CHECKPOINT_BYTES:
            logger.warning(
                "session tree checkpoint is %d bytes, past the %d-byte ceiling; "
                "discarding it and rebuilding",
                size,
                MAX_CHECKPOINT_BYTES,
            )
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.debug("session tree checkpoint unreadable; rebuilding", exc_info=True)
        return None
    if not isinstance(payload, dict) or payload.get("ver") != CHECKPOINT_VERSION:
        return None
    # The file says which store it describes; a mismatch is DISCARDED. Without this the
    # only thing tying a checkpoint to a store is its path, and a file that reached the
    # wrong path -- a write armed before the data home moved, a home restored from
    # elsewhere -- would be trusted and would report another store's lineage as this
    # one's, silently and with no recovery path.
    stored_root = payload.get("root")
    if not isinstance(stored_root, str) or stored_root != _current_root():
        logger.debug("session tree checkpoint describes another store; rebuilding")
        return None
    rows = payload.get("records")
    if not isinstance(rows, list):
        return None
    # A missing ``edges`` key cannot reach here: the version gate above admits only a
    # file this build wrote, and this build writes the key whether or not any session
    # has been adopted. So a payload that lacks it is not an older file to tolerate but
    # a damaged one, and the whole checkpoint is discarded rather than read as "no
    # adoptions" -- which is the one wrong answer that looks exactly like a right one.
    edge_rows = payload.get("edges")
    if not isinstance(edge_rows, list):
        return None
    # Same reasoning as ``edges`` for the key having to be PRESENT, and the opposite
    # reasoning for a malformed VALUE inside it: this one is a cache, so a row that does
    # not parse is dropped and its unit is simply re-read, where dropping a decision row
    # would change the tree.
    scan_rows = payload.get("scans")
    if not isinstance(scan_rows, dict):
        return None
    records: dict[str, OpenedRecord] = {}
    for raw in rows:
        record = _record_from_json(raw)
        if record is not None:
            records[record.sid] = record
    # Required for the reason ``edges`` is: a file missing it would load as "no
    # session recorded a child", which looks exactly like the truth.
    spawn_rows = payload.get("spawned")
    spawn_scan_rows = payload.get("spawn_scans")
    if not isinstance(spawn_rows, list) or not isinstance(spawn_scan_rows, dict):
        return None
    edges: dict[tuple[str, str], EdgeRecord] = {}
    for raw in edge_rows:
        edge = _edge_from_json(raw)
        if edge is not None:
            edges[(edge.slot, edge.sid)] = edge
    spawned: dict[tuple[str, str], EdgeRecord] = {}
    for raw in spawn_rows:
        spawn = _edge_from_json(raw)
        if spawn is not None and spawn.parent_slot:
            spawned[(spawn.slot, spawn.sid)] = spawn
    return records, edges, _scans_from_json(scan_rows), spawned, _scans_from_json(spawn_scan_rows)


def _scans_from_json(scan_rows: "dict[Any, Any]") -> "dict[str, ScanIdentity]":
    """A scan-identity cache from the checkpoint. A row that does not parse is dropped,
    which only costs its unit a re-read."""
    scans: dict[str, ScanIdentity] = {}
    for sid, raw in scan_rows.items():
        if not isinstance(sid, str) or not sid:
            continue
        if not isinstance(raw, list) or len(raw) != 4:
            continue
        if not all(isinstance(part, int) and not isinstance(part, bool) for part in raw):
            continue
        scans[sid] = (raw[0], raw[1], raw[2], raw[3])
    return scans


def _save_checkpoint(payload: dict[str, Any], path: "Path | None" = None) -> bool:
    """Write the checkpoint atomically. Returns whether it landed. Never raises.

    ``path`` is the resolved target. A background caller pins it when it ARMS the write,
    so a debounce that elapses after the data home moved still writes where the state
    came from; ``None`` resolves it here, which is right for a synchronous caller whose
    environment cannot have changed underneath it.

    No ``fsync``, the same choice ``checkpoint.save`` makes for the same reason: what a
    crash leaves unpersisted is an older shortcut or none, and both are states the
    reconciliation already handles. Paying for durability on a value that is explicitly
    disposable would be paying for the wrong thing.
    """
    try:
        from kiro_crew.atomic_write import atomic_write

        blob = json.dumps(payload, separators=(",", ":"))
        if len(blob.encode("utf-8")) > MAX_CHECKPOINT_BYTES:
            logger.warning(
                "session tree checkpoint would exceed the %d-byte ceiling; not written",
                MAX_CHECKPOINT_BYTES,
            )
            return False
        # ``atomic_write`` creates its target's parents itself, which is why there is no
        # ``mkdir`` here -- the same reason ``checkpoint.save`` has none.
        atomic_write(path or _checkpoint_path(), blob, fsync=False, newline="")
        return True
    except Exception:
        logger.debug("session tree checkpoint could not be written", exc_info=True)
        return False
