"""The session tree's side of the emitter: a slot's lineage and the folds after a commit.

Which crew log came before this one in a slot (:func:`slot_previous_store`, read from the store,
and :func:`_durable_previous` and :func:`_candidate_is_same_slot`, which the supersede repair in
:mod:`kiro_crew.crew_log.emit_runtime` uses too), the parent a takeover entry cites
(:func:`_parent_citation`), and what follows a lifecycle entry once it is on disk: the
``_record_session_tree_*`` folds that advance the in-memory session tree, and the settled signal
a caller waits on (:class:`_TreeSettle`, :func:`awaiting_commit`).
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from kiro_crew.crew_log.emit_state import _KIND, enabled, logger

if TYPE_CHECKING:  # pragma: no cover -- typing only; the runtime imports stay gated
    # Type-only, so the boot-path import gate is untouched: this name exists for the
    # checker and for test_crew_log_exc_info_sites.py, which reads annotations to decide
    # whether a frame can hold a handle. A handle passed in as ``Any`` is invisible to it.
    from kiro_crew.crew_log.store import CrewLog


class _TreeSettle:
    """The outcome signal a tree emitter hands to :func:`_submit`.

    Reports what the job DID, not what did not happen to it. ``wrote`` is set inside the
    job immediately after ``log.append`` returns, and ``after`` -- which ``_submit`` runs
    for every terminal outcome -- passes that flag on. Inferring success from the absence
    of a drop hook would be wrong in the arm that matters most: an entry rejected at the
    buffer's memory ceiling is finished WITHOUT ``on_permanent_drop``, which is exactly
    the wedged-writer condition the ceiling exists for, and the caller would be told its
    takeover landed while nothing was appended and the projection never moved.

    ``fail`` remains for the two cases that never reach the job's append at all: a
    permanent drop, and a job that finds no log to write to.
    """

    def __init__(self, on_settled: "Callable[[bool], None] | None") -> None:
        self._on_settled = on_settled
        self._wrote = False
        self._told = False

    def wrote(self) -> None:
        self._wrote = True

    def fail(self) -> None:
        self._wrote = False

    def after(self) -> None:
        if self._on_settled is None or self._told:
            return
        self._told = True
        self._on_settled(self._wrote)


def _tree_settle_hooks(on_settled: "Callable[[bool], None] | None") -> _TreeSettle:
    """One :class:`_TreeSettle` per emitted entry. Trivial, and named so the two tree
    emitters share the wiring rather than repeating it."""
    return _TreeSettle(on_settled)


#: How long :func:`awaiting_commit` waits before it answers False. Bounded because a
#: retryable write stays queued against a filesystem that may never answer, and the
#: callers are user-facing requests. Matched to :func:`flush`'s own default, the other
#: place that waits on this writer.
COMMIT_WAIT_SECONDS = 5.0


async def awaiting_commit(
    emit_one: "Callable[[Callable[[bool], None]], None]",
    *,
    what: str,
    timeout: float = COMMIT_WAIT_SECONDS,
) -> bool:
    """Queue one append through *emit_one* and wait, bounded, for it to COMMIT.

    For a caller that will PUBLISH on the strength of the append -- tell a user the
    card is gone, drop a run from live state. This queue returns as soon as the
    entry is handed over, so publishing on the handover publishes a record the
    writer may still drop, and when the log is the only record of the fact there is
    then nothing left to explain the reversal and nothing for a retry to act on.

    *emit_one* is handed the ``on_settled`` callback and must pass it to exactly one
    emitter call, or invoke it itself when it decides there is nothing to queue --
    otherwise this waits out the whole bound for an answer that is not coming.

    False on a drop AND on a timeout, which are the same thing to the caller: the
    record is not there to publish from. A timeout is not a failure of the append,
    which may still land later, so the caller's own answer should be retryable
    rather than final.

    Must be called from a running loop: the callback arrives on the writer's thread
    and is marshalled back onto this one.
    """
    loop = asyncio.get_running_loop()
    settled: "asyncio.Future[bool]" = loop.create_future()

    def _settle(wrote: bool) -> None:
        def _resolve() -> None:
            # Guarded because the timeout can win the race, and setting a result on
            # a future that already has one raises.
            if not settled.done():
                settled.set_result(wrote)

        loop.call_soon_threadsafe(_resolve)

    emit_one(_settle)
    try:
        return await asyncio.wait_for(settled, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(
            "crew log: %s was not committed within %.1fs; reporting it as not recorded",
            what,
            timeout,
        )
        return False


def _record_session_tree_spawn(
    creator_sid: str, creator_slot: str, child_slot: str, entry: Any
) -> None:
    """Fold a just-committed ``session/spawned`` into the in-memory session tree.

    Same contract as :func:`_record_session_tree_decision`: called after the append
    succeeded, takes ``seq`` and ``time`` from the written line, never raises.
    """
    try:
        from kiro_crew.crew_log.session_tree_projection import record_spawned

        raw = getattr(entry, "time", 0)
        at = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
        raw_seq = getattr(entry, "seq", 0)
        seq = raw_seq if isinstance(raw_seq, int) and not isinstance(raw_seq, bool) else 0
        record_spawned(creator_sid, creator_slot, child_slot, at, seq)
    except Exception:  # pragma: no cover -- defensive; the door guards itself
        # Rendered text, never ``exc_info``: the caller is the writer job holding the
        # live handle (see ``_record_session_tree_decision``).
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "session tree projection not advanced for a spawn by %s:\n%s",
                creator_sid,
                traceback.format_exc().rstrip(),
            )


def _parent_citation(slot: str, sid: str) -> "dict[str, str]":
    """One ``{slot, sid?}`` citation, or ``{}`` when there is no slot to cite.

    ``sid`` is omitted rather than written empty, the same distinction
    :func:`on_session_opened` keeps on its own ``parent``: an empty string would read
    as a session whose id is blank, and "the gateway had no live handle for it" is a
    different fact from that.
    """
    if not slot:
        return {}
    citation: dict[str, str] = {"slot": slot}
    if sid:
        citation["sid"] = sid
    return citation


def _candidate_is_same_slot(candidate_sid: str, slot: str) -> bool:
    """Whether *candidate_sid*'s crew log records *slot* as its own.

    ``previous`` means the crew log the SAME slot was writing, and the reference
    says so, but the id reaches this emitter from the slot-to-session mapping --
    a persisted file whose entry can be stale or recycled by the time a successor
    cold-starts. So the invariant is CHECKED rather than assumed, against the
    candidate's own header, which is written once at create and never rewritten.
    Comparing the mapping against itself would prove nothing; the header is the
    crew log's own statement about which slot it belongs to.

    Answering False on any failure is deliberate, because an edge is worth writing
    only when the two crew logs are KNOWN to be one slot's. A candidate whose
    header cannot be read -- retention removed the crew log, or its header line is
    unreadable -- is not known to be this slot's, and sending a reader down an
    unverified edge lands it somewhere this slot never wrote, which is worse than
    ending the walk one link early. A session with no slot has no slot identity to
    match, so it gets no edge either.

    ``unit_header_slot`` is the accessor rather than ``CrewLog.open`` because this
    path must not write, and ``open`` does: its torn-tail truncation is
    unconditional, deliberately so, since trailing bytes that are not a whole line
    are not a record. Harmless in itself, and still wrong here -- a verification
    read would take the crew log's lock and rewrite a candidate's file while asking
    for nothing but one field. The accessor states the opposite contract, no lease
    and nothing written, and its ``None`` means "cannot prove" rather than "no such
    field", which is the refusal this function already wanted. It is also stricter
    than ``open``: it refuses a linked directory, and a header whose own ``id`` does
    not fold back to the directory holding it, both of which would let one store
    answer for another unit. Its own docstring names this caller's position exactly
    -- a unit id reached through a channel the caller does not fully trust.

    Nothing is caught here because the accessor answers ``None`` for every
    unreadable-crew-log case itself, down to the directory walk and the header
    parse. Anything it still raises is a bug in this module, and it belongs in the
    log rather than swallowed into a permanently silent "not the same slot", which
    would read exactly like a correct refusal while disabling the check for every
    slot at once.
    """
    if not slot or not candidate_sid:
        return False
    from kiro_crew.crew_log.store import unit_header_slot

    return unit_header_slot(_KIND, candidate_sid) == slot


def slot_previous_store(slot: str) -> "tuple[str, bool, bool]":
    """The crew log *slot* is writing NOW, as ``(sid, decided, complete)``.

    Read from the store, so it survives the process that wrote it. Every gateway
    process asking this question of the same slot gets the same answer: the units
    under *slot* and the succession edges they recorded are the whole input, and a
    restart reads them exactly as the process before it would have. The
    slot-to-session mapping cannot answer it -- an allocation whose history replay
    is pending holds the prior resumable id there on purpose, so for that window the
    mapping names a generation older than the store the slot is writing.

    FOUR answers, because a caller must tell three kinds of empty apart. A ``sid``
    names the store. ``decided`` false is "the units could not be read, or do not
    say" -- a unit that would not open, more than one uncited unit -- and falling
    back THERE would hand the edge to the very source this read was preferred over,
    which inside the replay window is a generation behind. The honest outcome is no
    edge: one citation lost transiently, rather than a wrong citation frozen into an
    append-only entry.

    The two DECIDED empties differ by ``complete``, and a caller that flattens them
    writes a false statement. Complete means the store holds no unit of this slot at
    all, so the absence of a predecessor is the whole truth and the caller may state
    it. Incomplete means the store holds units it cannot rank -- units written before
    these keys existed, or several each stating they start the chain -- so the caller
    may consult its next source and may state NOTHING, because an empty answer from
    that source means only that it had nothing to give, not that this slot has no
    earlier store.

    Blocking, and gated: a launch with the crew log off answers ``("", True, True)``,
    which is also what keeps the storage subsystem unimported there -- no unit exists,
    so there is nothing indeterminate about it and the absence is complete. The caller
    hops a thread for this
    (:func:`~kiro_crew.crew_log.session_tree.slot_chain_head` lists the store and
    reads a line pair per unit of the slot).
    """
    if not slot or not enabled():
        return ("", True, True)
    from kiro_crew.crew_log.session_tree import slot_chain_head

    head = slot_chain_head(slot)
    return (head.sid, head.decided, head.complete)


def _durable_previous(session_id: str) -> str:
    """The predecessor id *session_id*'s own opening entry records, or ``""``.

    The recovery read for a crew log this process is re-attaching to. Imported
    locally for the same reason :func:`_candidate_is_same_slot` does it: this module
    is on the gateway boot path and the storage package stays unloaded until a call
    actually reaches it.

    ``""`` rather than ``None`` because the caller latches the answer and an empty
    string is the latched "nothing to recover" every other decision in that job
    uses. The accessor's own ``None`` is "cannot prove" -- a crew log that is gone,
    a header that does not fold back, an opening entry not yet appended -- and all of
    those mean the same thing here: no recovery is available from this file.
    """
    from kiro_crew.crew_log.store import unit_opened_previous

    return unit_opened_previous(_KIND, session_id) or ""


def _record_session_tree_decision(
    session_id: str,
    slot: str,
    entry: Any,
    parent_slot: "str | None",
) -> None:
    """Fold a just-committed ``session/adopted`` or ``session/released`` into the
    in-memory session tree. ``parent_slot`` of ``None`` is the release.

    Called immediately AFTER the append succeeded, for the reason
    :func:`_record_session_tree_edge` is: the tree is a projection that applies deltas
    and never rescans, so this line is what makes a takeover visible without waiting
    for a cold start.

    *entry* is what ``append`` returned, so its ``seq`` and ``time`` are the values ON
    DISK. ``seq`` is what orders the decision, and taking it from the written line is
    what makes the live fold and a cold replay of that same line agree. Reading a clock
    here instead would order the fold by a moment the log does not record.

    Never raises, and never logs at a level an operator has to act on: the append has
    already succeeded, so the record is safe on disk whatever happens here, and a missed
    fold is recovered by the projection's tail replay on the next cold start.
    """
    try:
        from kiro_crew.crew_log.session_tree_projection import record_adopted, record_released

        raw = getattr(entry, "time", 0)
        at = raw if isinstance(raw, int) and not isinstance(raw, bool) else 0
        raw_seq = getattr(entry, "seq", 0)
        seq = raw_seq if isinstance(raw_seq, int) and not isinstance(raw_seq, bool) else 0
        if parent_slot:
            record_adopted(session_id, slot, at, parent_slot, seq)
        else:
            record_released(session_id, slot, at, seq)
    except Exception:  # pragma: no cover -- defensive; both doors guard themselves
        # Rendered text, never ``exc_info``, for the reason
        # :func:`_record_session_tree_edge` spells out: this frame names no handle, but
        # its CALLER is the writer job, which binds ``log`` -- and a retained traceback
        # reaches that frame through ``tb_frame.f_back``, so a handler that keeps records
        # would keep the handle and its write lease. Same ``traceback`` idiom, for the
        # same import-gate reason. Pinned by test_crew_log_exc_info_sites.py.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "session tree projection not advanced for %s:\n%s",
                session_id,
                traceback.format_exc().rstrip(),
            )


def _record_session_tree_edge(
    session_id: str,
    slot: str,
    log: CrewLog,
    parent_slot: str | None,
    superseded: str | None,
) -> None:
    """Fold a just-committed ``session/opened`` into the in-memory session tree.

    Called immediately AFTER the append succeeded, which is the whole point: the
    session tree is a projection (:mod:`kiro_crew.crew_log.session_tree_projection`)
    that applies deltas and never rescans, so without this line a reader would be
    back to re-deriving the tree from the whole store on every poll.

    The record is built from what was just WRITTEN, not from a re-read of it: the
    values are the emitter's own, and reading the entry back would be the disk access
    this design exists to remove.

    ``created_at`` comes from the log's immutable header, through the same
    ``getattr(..., "created_at", 0)`` idiom :mod:`kiro_crew.crew_log.read` uses on the
    same object. It only orders a slot's several records inside the fold, so a header
    that cannot answer costs ordering, never an edge.

    Never raises, and never logs at a level an operator has to act on: the append has
    already succeeded, so this session's history is safe on disk whatever happens here,
    and a missed record is recovered by the projection's tail replay on the next cold
    start. Raising would turn a bookkeeping miss into a failed session open.
    """
    # The body is the docstring and this ONE try: nothing sits outside the guard, so
    # nothing can raise into the writer job (pinned by the projection tests).
    try:
        from kiro_crew.crew_log.session_tree_projection import record_opened

        created_at = 0
        try:
            # ``header`` is a PROPERTY, not a method. Calling it raised TypeError, the
            # outer handler swallowed that, and every edge folded with created_at 0 --
            # which orders the tree wrong. Pinned by the projection test below.
            header = log.header
            raw = getattr(header, "created_at", 0)
            if isinstance(raw, int) and not isinstance(raw, bool):
                created_at = raw
        except Exception:
            # An unreadable header orders nothing and blocks nothing.
            created_at = 0
        record_opened(session_id, slot, created_at, parent_slot, superseded)
    except Exception:  # pragma: no cover -- defensive; record_opened guards itself
        # Rendered text, never ``exc_info``: ``log`` is a live ``CrewLog`` in this frame,
        # and a record carrying the traceback carries this frame, so a handler that keeps
        # records (``caplog``, a ``MemoryHandler``) keeps the handle and its write lease
        # alive past the drop that should have released it. A string keeps no frames.
        # The store's ``log_exception_text`` does exactly this, but this module is the
        # boot-path import gate (see ``_crew_log``) and may not import the store at module
        # level, so the render uses the ``traceback`` module already imported above --
        # the same idiom ``_report`` uses. Pinned by test_crew_log_exc_info_sites.py.
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "session tree projection not advanced for %s:\n%s",
                session_id,
                traceback.format_exc().rstrip(),
            )
