"""Eager folding: an EAGER fold is advanced when its entry lands, not when it is read.

WHY THIS EXISTS. A slot-keyed fold is answered by walking every line of every unit the
slot ran under, because the fold interprets one entry type and the file is mostly
message bodies (:func:`~kiro_crew.crew_log.projection.fold_slot_warm` says this at
length). The warm memo removed the repeat of that walk, but never the first one, and a
dashboard reading the work board on a timer pays it on every cold cell. The value it
wants is a function of entries this process has just written, so it can be folded then
-- and the read becomes a memo lookup.

WHAT IS ON THE APPEND PATH. One :meth:`queue.Queue.put_nowait`. Not the slot lookup,
not the fold, not the frame: those are the worker's, because each of them reads a file
or builds a value and the thread that just committed an entry is either the event loop
or the one writer thread every session's appends are serialized through. A full queue
DROPS and counts (:func:`eager_dropped`) rather than waiting, for the reason the
emitter gives about its own buffer: a slow consumer must cost currency, never turn
latency. Dropping is safe because the fold is not the record -- the log is -- so a
dropped wake leaves the memo behind the file and the next READ carries it forward,
which is exactly the lazy behaviour that was there before.

WHAT THE WORKER RUNS. :func:`~kiro_crew.crew_log.projection.read_slot_projection`, the
same call the dashboard route makes. Not a second folding path: the rules a slot fold
has to enforce about continuing a cell -- a changed unit list, a recreated unit, an
earlier unit that grew, a rewritten prefix -- are stated once over there, and a rule
missing from a copy here would be a wrong record rather than a slow one. It also
resolves the unit list through the fold's OWNER, which is what makes the value the
eager path stores equal to the one a reader would have folded.

WHAT IT DOES NOT DO: push THE FOLD. An advanced fold is not broadcast, and that is a
decision rather than an omission. A slot fold's ``last_seq`` is the NEWEST unit's own seq
by contract, and conductor units are folded before worker units -- so a conductor-side
change on a board with any worker bound leaves that number unmoved, and any client rule
that orders frames by it would discard the changed value. Pushing correctly needs a
monotonic per-(slot, fold) revision that reads and frames share, which is a new contract
in the read path; and there is no consumer yet to need it. So the value is folded here and
READ from here, and the revision question belongs to the change that adds the reader.

WHAT IT DOES PUSH is a DEADLINE, and only one: a conductor's armed work-ledger loop is
pulled forward when a worker bound to it commits a ``work/recorded`` entry
(:func:`_push_conductor_wakes`). That carries no value and no frame, so none of the
revision contract above applies to it -- the conductor's own gate then reads the ledger
itself, under its own identity, exactly as on a scheduled tick. It rides this drain rather
than the append because the lookup is a file read and the fire crosses onto the event
loop, neither of which may sit on a worker's own ``work_report``.

WHAT IT IS NOT. Durable. The memo lives in this process, so a restart folds cold; a
savepoint for a SLOT fold would be a store of its own, keyed by slot rather than by
unit, with its own admission rules about the unit vector it was folded over. That is
not this module's, and :func:`~kiro_crew.session_ledger._fold_checkpoint` already says
so about the same cell.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Final, NamedTuple

from kiro_crew.crew_log.errors import CrewLogError

logger = logging.getLogger(__name__)

#: Wakes held between the append path and the worker. Sized for a burst, not for a
#: backlog: the worker's whole job per wake is one warm continuation, so a queue this
#: deep means the consumer has stopped rather than that it is behind, and the answer to
#: a stopped consumer is to drop and say so rather than to grow.
QUEUE_LIMIT: Final[int] = 1024


#: Entry type that ends a unit, and with it the reason to hold its slot warm.
_CLOSED_TYPE: Final[str] = "session/closed"

#: Every entry type worth a wake: the types the eager folds declare, plus the closer.
#:
#: LITERALS, because :func:`note_commit` sees every append and the bulk of a log is types
#: no eager fold names -- so the filter that rejects them has to be one frozenset lookup
#: and must not reach the registry, which means importing ``projection`` on the append
#: path. The price is that the types are written in two places, and being left out of this
#: one would look exactly like a fold working lazily. That is what
#: ``test_the_append_paths_wake_filter_covers_every_eager_folds_types`` exists for: it
#: derives the set from ``EAGER_FOLD_NAMES`` and fails on any divergence, so the registry
#: stays the authority and this stays cheap.
_WAKE_TYPES: Final[frozenset[str]] = frozenset({_CLOSED_TYPE, "work/recorded", "panel/published"})

#: How long the worker waits for a wake before looping, so a stop is noticed.
_POLL_SECONDS: Final[float] = 0.5

#: Set between a test caller's teardown and the next one's setup, where a wake can only be
#: a leak. See :func:`retire_for_tests`.
_retired = threading.Event()


class _Wake(NamedTuple):
    """One committed entry, as the append path describes it in one tuple.

    ``board`` is the slot whose fold this entry belongs to, and it has to be carried
    rather than derived. A unit's HEADER names the slot that unit ran under, which for a
    conductor's own entry is the board -- but a worker's ``work/recorded`` names the
    CONDUCTOR's board in its own ``slot`` field and reaches that fold only by joining it
    (``_work_units``). Resolving the header instead advances the worker's own board and
    leaves the conductor's -- the one a dashboard reads -- exactly as stale as before. The
    emitter holds the entry, so it is the one place the board is free to read; empty means
    "the unit's header names it", which is true of every type that carries no board of its
    own.

    ``seq`` is carried so a wake is self-describing in a log line and so the worker can
    tell a wake it has already folded past from one it has not, without opening the file.
    """

    unit_id: str
    entry_type: str
    seq: int
    board: str = ""


#: Put on the queue by :func:`stop_for_tests` to wake a worker parked in ``get``. A
#: ``threading.Event`` cannot interrupt a blocking ``Queue.get``, so setting the stop flag
#: alone leaves the worker waiting out its poll interval and the join waiting with it. This
#: is not a wake and carries no entry: :func:`_await_wake` answers ``None`` for it, which
#: sends the loop back to its stop check.
#:
#: A ``_Wake`` rather than a bare ``object()`` so the queue stays typed, and recognised by
#: IDENTITY rather than by value -- and even read by value it names no unit, which the
#: folder already skips.
_STOP_SENTINEL: Final[_Wake] = _Wake("", "", 0, "")

_lock = threading.Lock()
_queue: "queue.Queue[_Wake] | None" = None
_worker: "threading.Thread | None" = None
_stopping = threading.Event()
_dropped = 0
#: Wakes ENQUEUED, and wakes whose batch has finished folding. :func:`drain` waits for
#: the two to meet.
#:
#: Counted on the PUT side rather than when the worker takes one, and that is the whole
#: correctness of the wait. A take and the counter that records it cannot be made atomic
#: with respect to a reader without holding a lock across a blocking ``get``, so a
#: waiter comparing "queue empty" against a taken-count can land in the window where the
#: wake has left the queue and nothing has recorded it -- and report a fold as finished
#: before it started. A wake counted at ``put_nowait`` is already counted before any
#: caller can call :func:`drain`, so that window does not exist.
_queued = 0
_settled = 0
_progress = threading.Condition()


def _log_exc(level: int, message: str, *args: Any) -> None:
    """Log *message* with the exception RENDERED TO TEXT, never as a traceback object.

    The package's rule, and it binds here harder than it looks. A record carrying the
    traceback carries its frames, those frames reach their callers through ``f_back``,
    and every call this module makes into the fold surface runs through frames that bind
    an open ``CrewLog`` -- whose write lease is released by a finalizer when the handle
    is dropped. A record-keeping handler holding one such record would hold that handle,
    and its lease, for as long as it kept the record.
    ``store.log_exception_text`` renders the traceback and retains no frame.

    The import is function-local for the same reason :func:`_projection` is: this module
    must not pull the storage package onto the gateway's boot path.
    """
    try:
        # boot-path import gate, not style: see this module's docstring and ``_projection``.
        from kiro_crew.crew_log.store import log_exception_text

        log_exception_text(logger, level, message, *args)
    except Exception:  # pragma: no cover - logging must never be the failure
        logger.log(level, message, *args)


def _projection() -> Any:
    """The projection module, imported on first use.

    Function-local for the reason ``emit`` gives about the whole storage package: this
    module is reachable from the gateway's boot path and the fold surface is not, so the
    import is paid by the first process that actually commits an eager entry.
    """
    # boot-path import gate. ``import kiro_crew.crew_log.eager`` loads NEITHER
    # ``projection`` NOR ``store``, which is the property this buys and the one a test
    # can check; a module-level import here would put the whole fold surface on the
    # gateway's boot path, which AUTOSDE's no-new-work-on-gateway-boot-path rule forbids
    # and which ``emit._crew_log`` exists to prevent for the same package.
    from kiro_crew.crew_log import projection

    return projection


# --------------------------------------------------------------------------- #
# The append path's whole share
# --------------------------------------------------------------------------- #


def note_commit(unit_id: str, entry_type: str, seq: int, board: str = "") -> None:
    """Tell the worker *unit_id* committed an entry of *entry_type* at *seq*.

    *board* is the slot whose fold the entry belongs to, when the entry names one of its
    own (see :class:`_Wake`). Left empty, the unit's header decides -- which is right for
    every type that has no board field and wrong for a worker's report, so the emitter
    passes it wherever the entry carries it.

    The ONE call the append path makes, and it is one ``put_nowait`` plus a membership
    test against a frozen set. It never blocks, never opens a file and never raises: a
    caller that has just committed an entry has already done the thing that mattered,
    and a failure to tell a cache about it must not reach that caller.

    An entry no eager fold names is dropped HERE rather than handed over, because the
    types that matter are a handful and the entries that do not are the bulk of a log.
    """
    if not unit_id or not entry_type:
        return
    try:
        if entry_type not in _WAKE_TYPES or _retired.is_set():
            # Retired means a caller has torn its crew log down and the home it folded
            # against is going away; a wake arriving now is a leak, not a request
            # (:func:`retire_for_tests`).
            return
        _enqueue(_Wake(unit_id, entry_type, int(seq), board))
    except queue.Full:
        _count_drop(unit_id, entry_type)
    except Exception:  # pragma: no cover - the append path must not see this
        _log_exc(logging.DEBUG, "crew log eager wake for %s/%s was lost", unit_id, entry_type)


def _enqueue(wake: _Wake) -> None:
    """Put *wake* on the queue and count it, in that order, under the progress lock.

    The count has to be taken while the put is still uncontested: a waiter that saw the
    queue grow without the count moving would read the wake as already folded.

    The fence is re-checked HERE, inside ``_progress``, not only at ``note_commit``'s
    entry. A wake that passed that entry check in the few bytecodes before
    ``retire_for_tests`` set the latch is still on its way in, and the reset that zeroes
    ``_queued`` also takes ``_progress`` -- so without this re-check the stale
    ``_queued += 1`` could land AFTER the reset and leave a count no worker will ever
    settle (``_ensure_worker`` refuses to restart under the fence), which a later
    ``drain`` reads as never reaching parity. Taking the decision under the same lock the
    reset holds makes "count this wake" and "zero the counters" mutually exclusive: the
    late wake is either counted before the reset (and then zeroed with everything else)
    or dropped after it. No new state, and no lock held across the worker join.

    The fence flag is not enough on its own. ``_retired.is_set()`` asks whether teardown
    has happened; it does not ask whether the ``pending`` queue this call is holding is
    still the live one. A wake that captured ``pending`` from ``_ensure_worker`` and then
    paused can resume after a WHOLE test cycle has turned over -- teardown reset
    ``_queue`` and the next setup CLEARED ``_retired`` again -- so the flag reads False
    at the moment the resumed wake re-checks it, and it lands its ``put`` + ``_queued +=
    1`` on the orphaned old queue while incrementing the NEW test's counter. That count
    settles against a queue no worker drains, so a later ``drain`` never reaches parity.
    The identity check closes exactly that window: it compares the queue we hold against
    the live one rather than re-asking a flag that has since been cleared, which the flag
    can never do however the set/clear is ordered.
    """
    global _queued
    pending = _ensure_worker()
    with _progress:
        if _retired.is_set() or pending is not _queue:
            return
        pending.put_nowait(wake)
        _queued += 1


def _count_drop(unit_id: str, entry_type: str) -> None:
    """Record one dropped wake. Logged at DEBUG: the COUNT is the signal, not the line."""
    global _dropped
    with _lock:
        _dropped += 1
        total = _dropped
    if total == 1 or total % 256 == 0:
        logger.warning(
            "crew log eager fold queue is full; %d wake(s) dropped so far (latest %s/%s). "
            "The folds stay correct -- the next read carries them forward -- but a "
            "dashboard sees its value later than the entry landed",
            total,
            unit_id,
            entry_type,
        )


# --------------------------------------------------------------------------- #
# The worker
# --------------------------------------------------------------------------- #


def _ensure_worker() -> "queue.Queue[_Wake]":
    """The queue, with the one daemon thread draining it running.

    Started on first use rather than at import, so a process that never commits an eager
    entry never holds a thread. Restarted when the thread is gone, which covers the
    interpreter's own teardown of a daemon and a crash inside the loop -- and does not
    cover the queue, which survives both, so a wake queued while the thread was dead is
    folded by its replacement.
    """
    global _queue, _worker
    with _lock:
        if _queue is None:
            _queue = queue.Queue(maxsize=QUEUE_LIMIT)
        # Do not (re)start under the retirement fence. ``note_commit`` already drops a
        # wake once ``_retired`` is set, but a wake that passed that check in the few
        # bytecodes before the latch went up is now here -- and starting a worker for it
        # would clear ``_stopping`` and hand ``retire_for_tests``'s reset a daemon to
        # orphan, the exact restart race the fence exists to close. Return the queue so
        # the caller's ``put_nowait`` still lands (it is reaped with the queue on the
        # next stop), but leave the worker stopped. Production never sets the fence.
        if _worker is None or not _worker.is_alive():
            if not _retired.is_set():
                _stopping.clear()
                _worker = threading.Thread(target=_run, name="crew-log-eager-fold", daemon=True)
                _worker.start()
        return _queue


def _settle(count: int) -> None:
    """Record that *count* more wakes have been folded."""
    global _settled
    with _progress:
        _settled += count
        _progress.notify_all()


def _run() -> None:
    """Drain wakes until stopped. One fold per (slot, fold), per batch.

    Everything available is taken before anything is folded, and that is the point
    rather than a nicety: a turn writes several entries, and folding once per wake would
    pay the warm continuation once per entry to reach the same value the last one
    reaches. So a batch is COALESCED -- the newest seq per unit wins -- and each affected
    (slot, fold) is folded once.
    """
    while not _stopping.is_set():
        first = _await_wake()
        if first is None:
            continue
        batch, closers, taken = _coalesce(first)
        try:
            try:
                _fold_batch(batch, closers)
            except Exception:  # pragma: no cover - the loop outlives one bad batch
                _log_exc(logging.WARNING, "crew log eager fold batch failed")
            # The SECOND consumer, and the one the module header said was missing: an
            # advanced fold is not pushed, but a conductor's armed gate wants to know
            # its worker wrote. Its own ``try`` rather than a shared one, because the
            # two consumers are independent -- a fold that raised must not cost the
            # conductor its wake, and a wake that raised must not look like a fold
            # failure. Both sit inside the ``finally`` that settles, so neither can
            # strand :func:`drain`.
            try:
                _push_conductor_wakes(batch)
            except Exception:  # pragma: no cover - the loop outlives one bad batch
                _log_exc(logging.DEBUG, "crew log conductor wake batch failed")
        finally:
            _settle(taken)


def _await_wake() -> "_Wake | None":
    """The next wake, or ``None`` when the wait timed out so the loop can re-check stop."""
    pending = _queue
    if pending is None:  # pragma: no cover - the queue is created before the thread
        _stopping.wait(_POLL_SECONDS)
        return None
    try:
        taken = pending.get(timeout=_POLL_SECONDS)
    except queue.Empty:
        return None
    if taken is _STOP_SENTINEL:
        # Not a wake: the stop woke this ``get`` so the loop re-reads its flag now rather
        # than when the poll would have lapsed.
        return None
    return taken


def _coalesce(
    first: _Wake,
) -> "tuple[dict[tuple[str, str], _Wake], dict[str, int], int]":
    """*first* plus everything queued: the newest wake per (unit, board), the closers, the count.

    The CLOSERS ride separately because a closer and a later entry mean opposite things
    about one memo, and an append after a closer is accepted, so the batch has to carry
    both and let :func:`_fold_batch` apply them in commit order. They are keyed by UNIT
    alone: closing is a fact about a unit, and the closer's own wake carries no board
    because its entry type has no board field -- so a closer keyed like the other wakes
    would never be found for the entries it outranks.

    The COUNT is returned beside the batch because coalescing collapses several wakes
    into one entry: settling by the batch's length would leave :func:`drain` waiting
    forever for the ones it merged away.
    """
    # Keyed by (unit, board) rather than by unit: one unit can append to more than one
    # board -- a worker bound to two conductors reports to both -- and collapsing those
    # onto the unit would fold one board and silently drop the other.
    batch: dict[tuple[str, str], _Wake] = {}
    closers: dict[str, int] = {}
    taken = 1
    pending = _queue
    wake: "_Wake | None" = first
    while wake is not None:
        slot_key = (wake.unit_id, wake.board)
        held = batch.get(slot_key)
        if held is None or wake.seq >= held.seq:
            batch[slot_key] = wake
        # A closer is kept BESIDE the newest wake rather than instead of it. The two mean
        # opposite things about one memo -- drop it, advance it -- and an append after a
        # closer is accepted, so which applies is decided by commit order in
        # :func:`_fold_batch` and not by which wake won the key here.
        if wake.entry_type == _CLOSED_TYPE:
            closers[wake.unit_id] = max(closers.get(wake.unit_id, 0), wake.seq)
        if pending is None:
            break
        try:
            wake = pending.get_nowait()
        except queue.Empty:
            wake = None
        else:
            taken += 1
    return batch, closers, taken


def _fold_batch(
    batch: "dict[tuple[str, str], _Wake]", closers: "dict[str, int] | None" = None
) -> None:
    """Apply one batch: drop the slots a closer named, then advance the folds above it.

    DROP FIRST, ADVANCE AFTER, which is commit order and not an arbitrary choice of one
    over the other. A closer says the unit is finished and its memo holds nothing; an entry
    committed after it says that memo has a newer value. Both are true of the same slot,
    and the file's order settles which wins: the drop applies to what the closer saw, the
    advance to what came after. Advancing first would leave the drop erasing a fold nothing
    then refolds, which costs the next dashboard read the whole history.
    """
    projection = _projection()
    closed = closers or {}
    # A closer names its slot through the unit HEADER, the only place its entry type carries
    # one -- and that slot is the only one closing this unit says anything about.
    own_slots = {unit_id: _slot_of(unit_id) for unit_id in closed}
    closed_slots = {slot for slot in own_slots.values() if slot}
    work: dict[tuple[str, str], int] = {}
    for wake in batch.values():
        if wake.entry_type == _CLOSED_TYPE:
            continue
        # The entry's own board wins over the unit's header. A worker's report names the
        # conductor's board and belongs to that fold; the header would name the worker's.
        slot = wake.board or _slot_of(wake.unit_id)
        if not slot:
            # A unit whose header does not name a slot cannot be joined into a
            # slot-keyed fold at all, so there is no cell to advance. The read path
            # answers the same way for it.
            continue
        if _outranked_by_a_closer(wake, slot, closed, own_slots):
            continue
        for name in projection.EAGER_FOLD_NAMES:
            if projection._FOLDS[name].touched_by_type(wake.entry_type):
                key = (slot, name)
                work[key] = max(work.get(key, 0), wake.seq)
    for slot in closed_slots:
        # The savepoint story for a slot fold is the memo, so dropping it costs the next
        # read one cold fold of a unit that has stopped growing -- and holds nothing for
        # a session that will never append again.
        #
        # NAMED, one fold at a time. A bare ``slot=`` matches every ``(home, slot, *)``
        # memo, and a ``session/closed`` is not always the end of a board: a slot reset,
        # or one worker unit of a LIVE crew, would take that crew's lazy ``ledger`` and
        # ``radar`` cells with it -- cells no wake ever warmed, whose next read then pays
        # a cold fold nothing asked for. The closer drops what eager folding warmed.
        for eager_name in projection.EAGER_FOLD_NAMES:
            projection.forget_slot_folds(slot=slot, name=eager_name)
    for slot, name in work:
        _advance(slot, name)


def _outranked_by_a_closer(
    wake: _Wake, slot: str, closed: "dict[str, int]", own_slots: "dict[str, str]"
) -> bool:
    """Whether the drop is the whole answer for *wake*, so advancing *slot* would undo it.

    TWO conditions, and each is load-bearing -- which is why this is a named predicate and
    not an inline comparison.

    The closer has to be this wake's OWN unit's, because a seq numbers one unit's file:
    another unit's closer seq is not a number this wake's seq can be compared to, even when
    both units write to the same board.

    And *slot* has to be that unit's own board. A worker's ``work/recorded`` names the
    CONDUCTOR's board, so a worker's last report and its own closer land in one batch about
    two different cells: the closer is about the worker's slot, the report about the
    conductor's. Closing a unit says nothing about a board it only writes to, and
    suppressing the report there loses a value the conductor's dashboard was handed.
    """
    return wake.seq <= closed.get(wake.unit_id, 0) and slot == own_slots.get(wake.unit_id)


def _push_conductor_wakes(batch: "dict[tuple[str, str], _Wake]") -> None:
    """Pull the conductor forward for every bound worker that reported in *batch*.

    ON THE WORKER THREAD, never on the writer's. That is the whole reason this lives here
    rather than beside the append in ``emit``: resolving a binding is a file read and the
    push crosses onto the event loop, so doing it where the entry is committed would put
    both on the latency of a worker's own ``work_report``.

    The slot is the UNIT's, from its header -- not ``wake.board``. A ``work/recorded``
    entry names the CONDUCTOR's board in its own field, which is the fold it belongs to
    and the wrong end of this lookup: what decides whether to push is whether the WRITER
    is a bound worker. Asking the binding is also what makes a conductor's own
    ``work/recorded`` entry push nothing -- a conductor slot has no binding as a worker --
    without this module having to know what a conductor is.

    DEDUPED by worker slot, because a batch can hold several of one worker's entries and
    the push is a deadline move: firing twice would arm the same timer twice at delay
    zero for one tick's worth of news.

    Refusals and absences are the callee's to log. Nothing here retries: the conductor's
    scheduled tick reads the same ledger a cadence later.
    """
    # Function-local for the reason ``_projection`` is, and the reason the literal set
    # above exists: neither the entry-type registry nor the wake path belongs on the
    # gateway's boot path. ONE authority for the type, though -- a second literal here
    # would be a thing to keep in step with the first for no gain, since this runs on the
    # worker thread where an import is free.
    from kiro_crew.conductor_wake import fire_for_worker_slot_from_thread
    from kiro_crew.crew_log.entry_types import WORK_ENTRY_TYPE

    seen: set[str] = set()
    for wake in batch.values():
        if wake.entry_type != WORK_ENTRY_TYPE:
            continue
        slot = _slot_of(wake.unit_id)
        if not slot or slot in seen:
            continue
        seen.add(slot)
        fire_for_worker_slot_from_thread(slot)


def _advance(slot: str, name: str) -> None:
    """Fold *slot*'s *name* through the read path, so the memo a reader takes is current.

    The folded value is DISCARDED here, and that is the whole shape of this module: folding
    STORES the value, storing it is the errand, and handing it anywhere would be a push --
    which the module docstring says this does not do. A reader gets the value by reading.
    """
    projection = _projection()
    try:
        projection.read_slot_projection(slot, name)
    except CrewLogError:
        # A refusal is about the record, not about this path: the read route raises the
        # same thing for the same slot, and inventing a value here would serve one the
        # reader is deliberately not given.
        _log_exc(logging.DEBUG, "crew log eager fold refused for %s/%s", slot, name)
        return
    except Exception:
        _log_exc(logging.WARNING, "crew log eager fold failed for %s/%s", slot, name)


def _slot_of(unit_id: str) -> str:
    """*unit_id*'s slot, or ``""`` when its header does not prove one."""
    try:
        return str(_projection().slot_of_session(unit_id) or "")
    except Exception:  # pragma: no cover - an unreadable header
        _log_exc(logging.DEBUG, "crew log eager wake could not resolve %s's slot", unit_id)
        return ""


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def drain(timeout: float = 5.0) -> bool:
    """Wait until every wake queued so far has been FOLDED. ``True`` when it has.

    A TEST SEAM, named as one: for a case that has just committed an entry and needs the
    fold to have happened before it reads. Production never waits for this -- an eager
    fold is currency, and a reader that arrives first is served by the lazy path. The wait
    is on the counters rather than on the queue being empty: an empty queue means the
    worker has TAKEN the last wake, which is not the same as having folded it, and a
    waiter that stopped there would read a value the fold had not reached yet.
    """
    if _queue is None:
        return True
    deadline = time.monotonic() + max(timeout, 0.0)
    with _progress:
        while True:
            if _settled >= _queued:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _settled >= _queued
            _progress.wait(min(remaining, _POLL_SECONDS))


#: How long :func:`stop_for_tests` waits for the worker to leave its batch. Generous
#: enough that a fold over a test's log is never mistaken for a wedge, so an expiry means
#: the thread is stuck rather than slow.
_STOP_JOIN_SECONDS: Final[float] = 30.0


def retire_for_tests() -> "BaseException | None":
    """Drain what the writer owes, stop this worker, and FENCE the gap after it.

    Three steps, and the third is the one that makes the other two enough.

    Draining first means the appends already owed are folded while the caller's data home
    is still pinned. Best-effort by design: a test whose SUBJECT is a writer that cannot
    write leaves entries owed forever, so a required drain would fail exactly those tests
    for doing their job.

    Stopping second leaves no daemon running.

    And the fence closes what draining cannot. A late append lands on the writer thread
    after this returns, calls :func:`note_commit`, and :func:`_ensure_worker` would answer
    it by clearing the stop flag and starting a replacement daemon -- which resolves the
    data home when it folds, by then whatever the environment names. So this sets a latch
    and :func:`note_commit` drops wakes while it holds: between one caller's teardown and
    the next one's setup, a wake is a leak rather than a request.
    :func:`resume_for_tests` lifts it, which is what a test framework calls before handing
    control to a test, so every test still runs against a live worker.

    RETURNED rather than raised: the caller is a teardown floor that must finish its own
    work before reporting. :func:`stop_for_tests` raises for a test that calls it directly.

    ``emit`` is imported inside the call, not at module scope, because the append path
    imports THIS module and this one is the boot-path gate's own example.
    """
    # boot-path import gate, and a cycle: ``emit`` imports THIS module to reach
    # ``note_commit``, so a module-scope import here would both close that loop and put
    # the append path's dependencies on the gateway's boot path.
    from kiro_crew.crew_log import emit

    emit.flush()
    # Fence BEFORE the stop, not after it. The drain above enqueues the wakes already
    # owed while the home is still pinned; once it returns, every FURTHER wake is a leak.
    # Setting the latch here -- rather than in a ``finally`` after ``stop_for_tests``
    # returns -- closes the window the stop itself opens: ``flush``'s 5s wait is bounded
    # and its result discarded, so the emit writer can still be live, and a wake it lands
    # during the join-then-reset would otherwise pass ``note_commit`` and reach
    # ``_ensure_worker``, which would clear ``_stopping`` and start a replacement daemon
    # that the reset then orphans -- and that orphan resolves the data home once the pin
    # lifts. With the latch already set, ``note_commit`` drops it and ``_ensure_worker``
    # refuses to (re)start, so nothing survives the stop.
    _retired.set()
    try:
        stop_for_tests()
    except TimeoutError as exc:
        return exc
    return None


def resume_for_tests() -> None:
    """Lift the fence :func:`retire_for_tests` left, so wakes are requests again.

    Called before a test runs. The fence exists for the GAP between tests, not for a test:
    a suite that appends through the real path is meant to reach the fold worker, and that
    is what this hands back.
    """
    _retired.clear()


def stop_for_tests() -> None:
    """Stop the worker and forget every queued wake and counter.

    A test seam, named as one. The worker is a process-wide daemon holding a queue, so a
    test that leaves it running lets one case's wake land inside the next case's data
    home -- and a counter carried between cases makes a drop count meaningless.

    A join that expires RAISES, because the worker resolves the data home when it folds
    and a case's home is an env pin its teardown lifts: a batch outliving this call writes
    into whatever home the environment names once that pin is gone, and this call is the
    last moment at which such a write is attributable to the case that queued it.
    ``drain_breadcrumb_writes`` in ``test/conftest.py`` raises for the same reason.

    THE ORDER IS THE CONTRACT, and it is three steps that each own one thing: ask the
    worker to stop and wait for it; give up loudly while CHANGING NOTHING if it will not;
    reset only once it is confirmed dead. A live worker is still reading ``_queue`` and
    still counting, and it is still the only thing a later stop could join -- so the
    expiry path keeps the handle, the queue and the counters exactly as the worker left
    them, and the caller either retries or reports. Resetting under a live worker, or
    dropping its handle, is how a stop becomes the thing that strands a thread.
    """
    global _queue, _worker, _dropped, _queued, _settled
    _stopping.set()
    with _lock:
        worker = _worker
        pending = _queue
    if pending is not None:
        try:
            pending.put_nowait(_STOP_SENTINEL)
        except queue.Full:
            # A full queue is a worker with plenty still to take, so its next ``get``
            # returns immediately and the stop flag is read then.
            pass
    if worker is not None and worker.is_alive():
        worker.join(timeout=_STOP_JOIN_SECONDS)
    if worker is not None and worker.is_alive():
        raise TimeoutError(
            f"{worker.name} did not stop within {_STOP_JOIN_SECONDS}s; a fold batch is "
            "still running and will resolve the data home after this test's pins lift"
        )
    with _lock:
        _worker = None
        _queue = None
        _dropped = 0
    with _progress:
        _queued = 0
        _settled = 0
        _progress.notify_all()
