"""Crash-durable state for the board charge relay, kept beside the work ledger.

The relay copies a bound worker's charge into its board's log off the writer thread
(:mod:`kiro_crew.crew_log.work_relay`). Until the copy lands it lives only in memory,
so a crash between the charge's own commit (durable in the worker's unit) and the copy
loses the board's count of that charge, and nothing replays it. This module makes the
copy survive a crash, WITHOUT changing the crew-log record format: it keeps two small
facts per worker in a sidecar the relay owns, and replays unrelayed charges on start.

Two facts, both in one per-worker JSON file (``relay_boundary_path``), written whole:

* BOUNDARIES. Each time a worker is bound, the bind records -- under the worker's
  binding lock, with the bind itself -- the worker unit tail it was written over
  (``boundary_seq``) and a fingerprint of the binding file's exact bytes
  (``binding_fp``). A charge committed after a bind has a seq above that bind's
  boundary_seq, so the boundaries say which binding each charge was written under
  WITHOUT a wall clock: the binding whose boundary is the greatest one below the
  charge's seq. The list is append-only and bounded; a ring never drops the newest
  record, so the live binding's boundary is always present.
* CURSOR. The greatest charge seq whose board copy has landed, per
  ``(worker unit, board)``. A charge above it is unrelayed. The cursor advances only
  after the copy is on disk, so a crash between the copy and the cursor write replays
  the copy, and the reader counts it once by its ``closer`` (``<unit>:<seq>``) the way
  it already dedupes a retried append. Exactly once holds with no new record field.

WHAT REPLAY SENDS, AND WHAT IT REFUSES. Replay re-sends a charge only when it can PROVE
the charge was written under the binding in force now, and skips every case it cannot:

* the worker must be bound now, and the live binding's bytes must fingerprint-match the
  newest boundary record (same board, same item, same bytes);
* the worker unit must have EXACTLY ONE boundary record. A second boundary means the
  worker was rebound, and a charge above the newest boundary could have been written
  under the older binding yet landed above the newer boundary (the relay reads the
  binding off the writer, after the seq is fixed, so that race is real). Replay cannot
  tell the two apart by seq alone, so it refuses the whole above-boundary window for a
  rebound worker. Routing a rebound worker's charges to the board each was written under
  is a later change, on the same boundary records.

So replay never lands a charge on a wrong board. Its worst case here is the loss there
was before this change (it re-sends nothing), which is why this slice is safe on its own.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any, Final, Iterator, NamedTuple

logger = logging.getLogger(__name__)

#: Boundary records kept per worker. A ring, so a worker rebound many times does not
#: grow the file without bound; the newest is never dropped (it is the live binding).
_MAX_BOUNDARIES: Final[int] = 64
#: Cursor entries kept per worker, one per (unit, board) the worker ever relayed to.
_MAX_CURSOR: Final[int] = 256
#: Seqs copied ABOVE a cursor's contiguous frontier, kept per (unit, board) so a live
#: copy that lands out of order cannot pull the frontier over a seq not yet copied.
_MAX_PENDING: Final[int] = 4096

#: One lock per worker, so every sidecar read-modify-write (a bind's boundary note on
#: the bind thread, a landed copy's cursor advance on the relay thread, replay on start)
#: is serialized from load through store and none clobbers another's write. In-process:
#: one gateway owns a worker's sidecar, and the bind's own file lock orders two gateways.
_locks_guard = threading.Lock()
_locks: "OrderedDict[str, threading.Lock]" = OrderedDict()
_MAX_LOCKS: Final[int] = 4096


@contextmanager
def _worker_lock(worker_slot_key: str) -> Iterator[None]:
    """Hold *worker_slot_key*'s sidecar lock across one read-modify-write."""
    with _locks_guard:
        lock = _locks.get(worker_slot_key)
        if lock is None:
            lock = threading.Lock()
            _locks[worker_slot_key] = lock
            while len(_locks) > _MAX_LOCKS:
                # An evicted lock is only ever re-created; eviction cannot drop a HELD
                # lock, since a holder keeps its own reference for the with-block.
                _locks.popitem(last=False)
        else:
            _locks.move_to_end(worker_slot_key)
    with lock:
        yield


class Boundary(NamedTuple):
    """One bind, anchored to the worker unit tail it was written over."""

    bind_gen: int
    board: str
    item_id: str
    unit: str
    boundary_seq: int
    binding_fp: str


def _binding_fingerprint(worker_slot_key: str) -> str:
    """A fingerprint of the binding file's exact bytes, or ``""`` when it is gone."""
    from kiro_crew import work_ledger

    try:
        data = work_ledger.binding_path(worker_slot_key).read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(data).hexdigest()


def _newest_unit_and_tail(worker_slot_key: str) -> "tuple[str, int]":
    """The worker's current newest work unit and its tail seq, or ``("", 0)``."""
    from kiro_crew import session_ledger

    unit = session_ledger.newest_work_unit(worker_slot_key)
    if not unit:
        return "", 0
    from kiro_crew.crew_log.read import read_page

    # One range read of the whole tail; ``read_page`` walks the file live and reports
    # the true ``last_seq`` even for a unit this process has not appended to.
    page = read_page(unit, 1, 2**62)
    return unit, int(page.get("last_seq") or 0)


def _sidecar_path(worker_slot_key: str) -> "Any":
    from kiro_crew import work_ledger

    return work_ledger.relay_boundary_path(worker_slot_key)


def _load(worker_slot_key: str) -> "dict[str, Any]":
    """The worker's sidecar as a dict, or an empty one when absent or untrusted."""
    from kiro_crew import work_ledger

    try:
        raw = work_ledger._read_json_record(_sidecar_path(worker_slot_key))
    except Exception:  # pragma: no cover - a sidecar read must not cost a bind
        raw = None
    if not isinstance(raw, dict):
        return {"worker": worker_slot_key, "boundaries": [], "cursor": {}}
    boundaries = raw.get("boundaries")
    cursor = raw.get("cursor")
    return {
        "worker": str(raw.get("worker") or worker_slot_key),
        "boundaries": boundaries if isinstance(boundaries, list) else [],
        "cursor": cursor if isinstance(cursor, dict) else {},
    }


def _store(worker_slot_key: str, state: "dict[str, Any]") -> None:
    from kiro_crew import work_ledger

    work_ledger._write_record(_sidecar_path(worker_slot_key), state)


def note_boundary(
    worker_slot_key: str, board: str, item_id: str, *, prior_existed: bool = False
) -> None:
    """Record the bind just written for *worker_slot_key*. Never raises.

    CALL UNDER THE WORKER'S BINDING LOCK, right after the binding file is written, so
    the fingerprint is of the bytes this bind wrote and ``boundary_seq`` is the unit
    tail no later charge of this binding can fall at or below.

    *prior_existed* is whether a binding file this write replaced was already there. A
    binding that existed before the FIRST boundary record (an upgrade from a build that
    did not record boundaries, or a rebind the relay never saw) could have had a charge
    in flight under it, which can land above the boundary this records; that first
    boundary is marked unproven so replay refuses the window, the same as a rebind.
    """
    try:
        unit, tail = _newest_unit_and_tail(worker_slot_key)
        fingerprint = _binding_fingerprint(worker_slot_key)
        if not unit or not fingerprint:
            # No unit to anchor to, or the binding file is unreadable: nothing provable
            # to record. A later charge simply is not replayed, which is main's loss.
            return
        # One lock from load through store, so a concurrent cursor advance cannot load
        # the sidecar before this boundary lands and then store its stale snapshot over
        # it (which would drop the boundary and make replay refuse the window).
        with _worker_lock(worker_slot_key):
            state = _load(worker_slot_key)
            state["worker"] = worker_slot_key
            boundaries = state["boundaries"]
            bind_gen = 1 + max((int(b.get("bind_gen") or 0) for b in boundaries), default=0)
            record = {
                "bind_gen": bind_gen,
                "board": board,
                "item_id": item_id,
                "unit": unit,
                "boundary_seq": tail,
                "binding_fp": fingerprint,
            }
            if prior_existed and not boundaries:
                # First boundary over a binding that already existed: the window above
                # it is not provably this binding's. Refused like a rebind.
                record["prior_unrecorded"] = True
            boundaries.append(record)
            # Keep the newest; the last element is the live binding and is never cut.
            if len(boundaries) > _MAX_BOUNDARIES:
                state["boundaries"] = boundaries[-_MAX_BOUNDARIES:]
            _store(worker_slot_key, state)
    except Exception as exc:  # pragma: no cover - a boundary note must not cost the bind
        # Type and message, never ``exc_info``: this reads a unit tail through a crew-log
        # handle, and a traceback record would keep that handle's write lease alive
        # (``test_crew_log_exc_info_sites``).
        logger.debug("work relay: could not note a bind boundary: %r", exc)


def _cursor_key(unit: str, board: str) -> str:
    return f"{unit}\x1f{board}"


def _cursor_entry(cursor: "dict[str, Any]", key: str) -> "dict[str, Any]":
    """*key*'s cursor entry as ``{through, pending}``; an old bare-int entry reads as a
    frontier with no pending seqs, so a sidecar written by an earlier build still loads."""
    raw = cursor.get(key)
    if isinstance(raw, dict):
        pending = raw.get("pending")
        return {
            "through": int(raw.get("through") or 0),
            "pending": sorted({int(s) for s in pending}) if isinstance(pending, list) else [],
        }
    return {"through": int(raw or 0), "pending": []}


def _drain_pending(entry: "dict[str, Any]") -> None:
    """Fold every copied seq contiguous with the frontier into it, so the frontier is
    the greatest seq with no gap below it among the copied seqs."""
    pending = entry["pending"]
    while pending and pending[0] <= entry["through"] + 1:
        entry["through"] = max(entry["through"], pending.pop(0))


def relayed_through(worker_slot_key: str, unit: str, board: str) -> int:
    """The greatest charge seq with NO uncopied charge below it; replay resumes above it.

    This is the contiguous frontier only: a copy that landed above a gap sits in
    ``pending`` and does not move it, so a live copy can never pull the resume point
    over a charge a crash left uncopied (which replay still owes).
    """
    state = _load(worker_slot_key)
    return _cursor_entry(state["cursor"], _cursor_key(unit, board))["through"]


def advance_cursor(
    worker_slot_key: str, unit: str, board: str, seq: int, *, contiguous: bool = False
) -> None:
    """Record that *unit*'s charge at *seq* has its copy to *board* on disk. Never raises.

    A LIVE copy (``contiguous=False``) only ever records *seq* in ``pending``: it never
    moves the frontier, because an older charge the same crash left uncopied may sit
    below it, and moving the frontier would make replay skip that charge forever (F1).
    REPLAY (``contiguous=True``) walks the unit's charges in ascending order and has
    copied every charge up to *seq*, so it may move the frontier to *seq* and fold in
    any now-contiguous ``pending``. The frontier never moves backward.
    """
    try:
        with _worker_lock(worker_slot_key):
            state = _load(worker_slot_key)
            cursor = state["cursor"]
            key = _cursor_key(unit, board)
            entry = _cursor_entry(cursor, key)
            if seq <= entry["through"]:
                return
            if contiguous:
                entry["through"] = seq
            elif seq not in entry["pending"]:
                entry["pending"].append(seq)
                entry["pending"].sort()
            _drain_pending(entry)
            if len(entry["pending"]) > _MAX_PENDING:
                # Drop the lowest copied-above-the-frontier seqs; replay re-sends them if
                # still needed and the reader dedupes by closer, so this only costs work.
                entry["pending"] = entry["pending"][-_MAX_PENDING:]
            cursor[key] = entry
            if len(cursor) > _MAX_CURSOR:
                # Drop the lowest-frontier entries; they name the oldest (unit, board)s.
                ordered = sorted(cursor, key=lambda k: _cursor_entry(cursor, k)["through"])
                for stale in ordered[: len(cursor) - _MAX_CURSOR]:
                    del cursor[stale]
            _store(worker_slot_key, state)
    except Exception as exc:  # pragma: no cover - a cursor write must not cost the copy
        # Type and message only, for the same handle-lease reason as ``note_boundary``.
        logger.debug("work relay: could not advance the relay cursor: %r", exc)


def _live_boundary(worker_slot_key: str, state: "dict[str, Any]") -> "Boundary | None":
    """The newest boundary IFF it still equals the live binding, else ``None``.

    Proven, not assumed: the live binding must name the same board and item, and the
    binding file's bytes must fingerprint-match what the bind recorded.
    """
    boundaries = state["boundaries"]
    if not boundaries:
        return None
    newest = boundaries[-1]
    from kiro_crew import work_ledger

    live = work_ledger.read_binding(worker_slot_key)
    if live is None:
        return None
    board, item_id = live
    if board != str(newest.get("board") or "") or item_id != str(newest.get("item_id") or ""):
        return None
    if _binding_fingerprint(worker_slot_key) != str(newest.get("binding_fp") or ""):
        return None
    return Boundary(
        bind_gen=int(newest.get("bind_gen") or 0),
        board=board,
        item_id=item_id,
        unit=str(newest.get("unit") or ""),
        boundary_seq=int(newest.get("boundary_seq") or 0),
        binding_fp=str(newest.get("binding_fp") or ""),
    )


class Replayable(NamedTuple):
    """A charge proven safe to replay to *board* from *unit*."""

    worker_slot_key: str
    unit: str
    board: str
    after_seq: int


def replayable_window(worker_slot_key: str) -> "Replayable | None":
    """The unrelayed-charge window that is proven safe to replay for *worker_slot_key*.

    ``None`` -- the refusal -- for every case this cannot prove: no boundary, a live
    binding that does not match the newest boundary, a worker rebound at all (more than
    one boundary record), or a first boundary recorded over a binding that already
    existed (``prior_unrecorded``). Their above-boundary charges a later change routes
    by boundary.
    """
    state = _load(worker_slot_key)
    boundaries = state["boundaries"]
    if len(boundaries) != 1:
        # Zero: nothing recorded. More than one: rebound, so a charge above the newest
        # boundary cannot be proven to belong to the live binding by seq alone. Skip.
        return None
    if boundaries[0].get("prior_unrecorded"):
        # A binding existed before this boundary was first recorded, so a charge in
        # flight under the older binding could sit above this boundary_seq. Skip.
        return None
    live = _live_boundary(worker_slot_key, state)
    if live is None or not live.unit:
        return None
    after = max(live.boundary_seq, relayed_through(worker_slot_key, live.unit, live.board))
    return Replayable(worker_slot_key, live.unit, live.board, after)


def workers_with_boundaries() -> "list[str]":
    """The worker slot keys that have a relay-boundary sidecar, for replay on start.

    The slot key is read from inside each sidecar, not folded back from its filename:
    the filename is a one-way readable-plus-digest fold.
    """
    from kiro_crew import work_ledger

    workers: "list[str]" = []
    try:
        entries = sorted(work_ledger.relay_boundaries_dir().glob("*.json"))
    except OSError:
        return workers
    for path in entries:
        try:
            raw = work_ledger._read_json_record(path)
        except Exception:  # pragma: no cover - one unreadable sidecar skips, not stops
            raw = None
        if isinstance(raw, dict):
            worker = str(raw.get("worker") or "")
            if worker:
                workers.append(worker)
    return workers


def forget_for_tests() -> None:
    """Drop the per-worker locks. The sidecar files are the durable state and a test's
    temp data home is discarded between tests; only the lock registry is process-held."""
    with _locks_guard:
        _locks.clear()
