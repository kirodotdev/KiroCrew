"""The per-loop timer tasks, the turn-lifecycle hooks that arm them, and the reconciler.

One cancellation policy (:func:`_cancel_timer`) retires a loop's timer task, and one
arming rule (:func:`_arm_from_deadline`) re-arms it toward the loop's persisted
deadline, so a user turn defers a pending fire without pushing the schedule back. The
dashboard's turn hooks (``notify_*``) are the reactive half: they cancel on user input,
resume on turn completion and record approval and start-failure evidence for the next
tick to act on, deferring around a loop's fire window. The reconciler is the backstop
that re-arms an active loop left with no live timer across two passes.

Its functions that take the service as ``self`` are
:class:`~kiro_crew.autonudge.AutoNudgeService` methods: each is bound on the class by
name and runs against the service's state through ``self``, and a call to any other
service method goes through ``self`` too, so a patch on the instance reaches it. The
plain helpers beside them are imported directly by the owners that use them.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AbstractContextManager, contextmanager
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Concatenate, Iterator, ParamSpec, TypeVar

from kiro_crew import probes, shutdown_event
from kiro_crew.autonudge_service.maintenance import _DurabilityDrain
from kiro_crew.autonudge_service.model import (
    _CONSECUTIVE_FAILURE_STANDDOWN_AFTER,
    _START_FAILURE_BACKOFF_AFTER,
    _START_FAILURE_STANDDOWN_AFTER,
    APPROVAL_HOLD,
    PERSON_WAIT_HOLD,
    SUBJECT_READ_SECS,
    NudgeAdmissionRefused,
    NudgeLoop,
    _MutationAdmission,
    _positive_number,
    _TransactionSubmissions,
    credit_held_runtime,
    hold_span,
    is_structured_monitor_loop,
    note_hold_start,
    runtime_elapsed,
)
from kiro_crew.autonudge_service.subject import loop_subject
from kiro_crew.monitoring.models import MONITOR_STATE_VERSION, MonitorDispatchResult

if TYPE_CHECKING:
    from kiro_crew.autonudge import AutoNudgeService

# The service's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.autonudge")


# Re-arm delay after a skipped/failed fire so a busy slot or a transient fire
# error can't silently orphan the loop. The delay escalates exponentially per
# consecutive failure (base << streak) up to _REARM_MAX_BACKOFF_SECS, and is
# always capped by the loop's idle_secs, so a permanently-wedged callback backs
# off to a slow poll instead of hammering every base interval.
_REARM_BACKOFF_SECS = 15
_REARM_MAX_BACKOFF_SECS = 300  # 5m ceiling for the escalated re-arm delay
_REARM_BACKOFF_MAX_SHIFT = 16  # clamp the 2**shift exponent
_MONITOR_RETRY_BACKOFF_SECS = 15
_MONITOR_RETRY_MAX_BACKOFF_SECS = 300


# Re-arm delay when a loop's deadline has already passed while a user turn was
# in flight. Small but non-zero: firing the instant the user's turn ends would
# race their follow-up message; a short beat leaves room for notify_user_input
# to cancel the pending fire again if they are still actively conversing.
_OVERDUE_REARM_SECS = 10

# How long after its runtime budget runs out a loop's timer wakes. asyncio may run
# a timer up to its clock's resolution early (about 16 ms on Windows), and a wake
# just short of the budget would fire a turn instead of stopping the loop.
_BUDGET_WAKE_SLACK_SECS = 1.0


# Gap between the loops that are overdue when the service starts. After a
# restart every overdue loop would otherwise wake in the same few seconds, each
# cold-starting its session and that session's MCP servers together, which is
# how a host short of memory crash-loops. The n-th such loop is held n gaps
# (``_boot_holds``) before its normal countdown, so their sessions start one at
# a time. A work-ledger loop's boot replay is not held (``start()``).
_BOOT_WAKE_STAGGER_SECS = 30


# How often the reconciler walks the store looking for an active loop with no
# live timer task. A stranded loop (fire delivered but the slot's stop hook
# never arrived, a dropped deferred re-arm) is rescued after two consecutive
# eligible passes -- so within two to three intervals of going quiet; the walk
# itself is an in-memory scan of a small dict, so the interval is chosen for
# rescue latency, not cost. The two-pass requirement, not this number, is what
# keeps the reconciler from mistaking short-lived live states (a running user
# turn, a mutation window) for strandings; see _reconcile_once.
_RECONCILE_INTERVAL_SECS = 60


def _resolve_beat(beat: "asyncio.Future[None]") -> None:
    """Resolve one reconciler heartbeat future (see ``_reconcile_forever``)."""
    if not beat.done():
        beat.set_result(None)


def _current_task_or_none() -> "asyncio.Task[Any] | None":
    """:func:`asyncio.current_task`, or ``None`` when no loop is running.

    ``current_task`` raises ``RuntimeError: no running event loop`` outside a loop, and
    ``stop()`` is reached from SYNCHRONOUS callers — the gateway's shutdown path and test
    teardown — where nothing is running. There, no task can be "the current" one, which is
    the answer this returns rather than an exception the caller would have to know about.
    """
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


#: The timer task an update runs on behalf of. ``_timer`` binds it only around its own
#: ``update`` calls (``firing._tick_update``), so the binding reaches the shielded task
#: in which ``update`` mutates and re-arms, and nothing else the tick spawns. From that
#: task the running tick is not "the current task", and without this binding
#: ``_cancel_timer`` would cancel the tick mid-await, skipping whatever the tick does
#: after its own update returns (the ``expired`` event of every terminal bound, a
#: ledger extension's WARNING).
_ACTING_TIMER: contextvars.ContextVar["asyncio.Task[Any] | None"] = contextvars.ContextVar(
    "autonudge_acting_timer", default=None
)


_P = ParamSpec("_P")
_R = TypeVar("_R")


class _RegisteredSectionCompletion(asyncio.Future[None]):
    """Completion signal mapping one caller-owned boundary into the drain."""

    def __init__(self, owner_task: asyncio.Task[Any] | None) -> None:
        super().__init__()
        self.owner_task = owner_task


@contextmanager
def _drain_registered_section(
    svc: AutoNudgeService,
    *,
    cancel_callback_on_exit: bool = False,
) -> Iterator[None]:
    """Register only this section, not its caller's remaining task lifetime."""
    completion = _RegisteredSectionCompletion(_current_task_or_none())
    svc._inflight_adds.add(completion)

    def resolve() -> None:
        if not completion.done():
            completion.set_result(None)
        svc._inflight_adds.discard(completion)

    try:
        yield
    finally:
        owner = completion.owner_task
        if (
            cancel_callback_on_exit
            and not svc._accepting_mutations
            and owner is not None
            and owner in svc._running_callbacks
        ):
            owner.cancel()
            # Let the callback receive that cancellation and register any exit
            # bookkeeping before shutdown wakes on this section's completion.
            completion.get_loop().call_soon(resolve)
        else:
            resolve()


def drain_section(self: AutoNudgeService) -> AbstractContextManager[None]:
    """A section shutdown's drain waits for instead of cancelling.

    The entry code outside AutoNudge uses (a channel's post-reply bookkeeping), so the
    drain's registration rule stays here: only the ``with`` body is registered, not the
    rest of the caller's task.
    """
    return _drain_registered_section(self)


async def settle_admitted_writes(self: AutoNudgeService, deadline: float) -> int:
    """Wait until *deadline* for admitted work a caller's cut of ``shutdown()`` left.

    A gateway bounds its wait on ``shutdown()`` so its own teardown can start. That cut
    frees the teardown but makes nothing durable, and the hard exit after the teardown
    would abandon an admitted mutation or executor write still running: the restart
    loads the previous row. This waits for that work until *deadline* (event-loop
    time), re-snapshotting each pass because an admitted section can start its executor
    write after the last snapshot settled. Waiting never cancels the work.

    Returns how many items were still pending when it stopped: 0 once everything
    settled. Otherwise, at *deadline* or when its caller cancels it (the cancellation is
    re-raised), it logs them at WARNING; each store write is atomic, so the file holds
    the new state or the previous complete one.
    """
    loop = asyncio.get_running_loop()
    current = asyncio.current_task()

    def unsettled() -> set[asyncio.Future[Any]]:
        return {
            future
            for future in (*self._inflight_adds, *self._inflight_persistence)
            if future is not current and not future.done()
        }

    def warn(pending: set[asyncio.Future[Any]]) -> None:
        logger.warning(
            "AutoNudge still had %d admitted mutation(s)/write(s) in flight at the "
            "graceful shutdown deadline; exiting without them",
            len(pending),
        )

    try:
        while pending := unsettled():
            remaining = deadline - loop.time()
            if remaining <= 0:
                warn(pending)
                return len(pending)
            # ``asyncio.wait`` neither cancels what it waits on nor raises on its
            # timeout, so a write it stops waiting for keeps running on its thread.
            await asyncio.wait(pending, timeout=remaining)
    except asyncio.CancelledError:
        pending = unsettled()
        if pending:
            warn(pending)
        raise
    return 0


async def _run_admitted_transaction(
    svc: AutoNudgeService,
    transaction: Coroutine[Any, Any, _R],
    *,
    cancel_callback_on_exit: bool = False,
    on_failure_log: str,
    expected: tuple[type[BaseException], ...] = (),
) -> _R:
    """Run one admitted transaction in a supervised, shielded child task.

    A caller cancelled before this transaction submitted a write stops only its wait;
    the child stays in ``_inflight_adds`` and completes the mutation. One cancelled
    after a submission is held until the transaction settles (bounded by the
    post-cancellation window if the write is wedged), then gets the ``CancelledError``.
    Submissions are counted on a token in the child's context only, so a detached write
    under the same callback lease never counts. A companion completion keeps the timer
    callback visible to shutdown; after admission closes, that callback is cancelled
    only once the child settles and only while it still awaits this transaction.
    """
    # Criterion: an admitted write whose awaiting task can be cancelled by
    # anything other than shutdown's own drain before persistence lands uses
    # this detached, registered-child path.
    owner = _current_task_or_none()
    completion = _RegisteredSectionCompletion(owner)
    awaiting_transaction = False
    submissions = _TransactionSubmissions()
    owner_token = svc._admitted_transaction_owner.set(owner)
    submissions_token = svc._admitted_transaction_submissions.set(submissions)
    try:
        inner: asyncio.Task[_R] = asyncio.ensure_future(transaction)
    finally:
        svc._admitted_transaction_submissions.reset(submissions_token)
        svc._admitted_transaction_owner.reset(owner_token)
    svc._inflight_adds.add(completion)
    svc._inflight_adds.add(inner)

    def resolve() -> None:
        if not completion.done():
            completion.set_result(None)
        svc._inflight_adds.discard(completion)

    def _finish(t: asyncio.Task[_R]) -> None:
        svc._inflight_adds.discard(t)
        exc = None if t.cancelled() else t.exception()
        if exc is not None and not isinstance(exc, expected):
            logger.warning(on_failure_log, exc_info=exc)
        if (
            awaiting_transaction
            and cancel_callback_on_exit
            and not svc._accepting_mutations
            and owner is not None
            and owner in svc._running_callbacks
        ):
            owner.cancel()
            # Let the callback receive cancellation before shutdown wakes on the
            # transaction's original-owner completion record.
            completion.get_loop().call_soon(resolve)
        else:
            resolve()

    inner.add_done_callback(_finish)
    awaiting_transaction = True
    try:
        try:
            return await asyncio.shield(inner)
        except asyncio.CancelledError:
            if submissions.count:
                # This transaction has a write in flight: hold its caller until
                # the transaction settles, as the inline transaction did.
                # ``awaiting_transaction`` stays set through the hold, so a
                # post-closure ``owner.cancel()`` from ``_finish`` lands here and
                # the caller still sees one ``CancelledError``.
                drain = _DurabilityDrain()
                drain.note_cancelled()
                await drain.settle(inner)
            raise
    finally:
        awaiting_transaction = False


def _drain_registered_owners(svc: AutoNudgeService) -> set[asyncio.Task[Any]]:
    """Running callbacks that own a drain-protected section or transaction."""
    owners: set[asyncio.Task[Any]] = set()
    for completion in svc._inflight_adds:
        if (
            isinstance(completion, _RegisteredSectionCompletion)
            and completion.owner_task is not None
        ):
            owners.add(completion.owner_task)
    return owners


def _drained_bookkeeping(
    fn: Callable[Concatenate[AutoNudgeService, _P], Awaitable[_R]],
) -> Callable[Concatenate[AutoNudgeService, _P], Coroutine[Any, Any, _R]]:
    """Register *fn*'s section in the shutdown drain while it runs.

    ``shutdown()`` cancels a running timer callback exactly like a dormant timer, so a
    gateway stop never waits on a delivery, a probe or a judge call. Bookkeeping a
    delivery has already earned -- the fire cycle's settlement, a structured monitor's
    dispatch record -- must not be cut that way, so for exactly as long as *fn* runs
    its completion future sits in ``_inflight_adds``, where shutdown drains the
    section instead of the caller's remaining task lifetime. None of these sections
    leads into a new delivery, and none contains a provider probe: a terminal
    settlement's re-probe runs between two of them, where shutdown cancels it.
    """

    @functools.wraps(fn)
    async def wrapper(self: AutoNudgeService, /, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        with _drain_registered_section(self):
            return await fn(self, *args, **kwargs)

    return wrapper


def _held_tick_rearms(loop: NudgeLoop) -> bool:
    """Whether *loop*'s held tick re-arms one read interval out to read its subject again.

    Only an approval-held GATED loop that names one pull request does, which
    ``subject_reads.hold_tick`` reads so a merge or a close settles the loop. An
    ungated loop never does: ``gate=False`` promises a terminal reading can never
    end it, so held it reads nothing, exactly as a held loop naming no pull
    request, and waits for a person.
    """
    if not loop.approval_stalled or not loop.gate or loop.monitor is None:
        return False
    target = loop_subject(loop)
    return target is not None and target.kind == probes.GH_PR


def notify_approval_stalled(self: AutoNudgeService, slot_key: str) -> None:
    """Record that a tool approval in *slot_key* went unanswered: hold the loop.

    Called when an approval prompt times out with no decision: the only evidence
    that an unattended loop cannot act (auto-approved tools never reach the wait).

    Schedules the durable record (``_record_approval_hold``) and returns; ``_timer``
    applies the HOLD, since acting here would cancel a timer that may be mid-fire and
    kill the in-flight turn. Deferring costs only the cycle already in flight.

    A hold, not a stop: the loop stays active, fires nothing, spends neither cycle
    cap nor runtime budget, and resumes through :func:`release_approval_hold` once a
    person is back, so a long patrol does not end overnight for an unanswered prompt.
    The evidence is slot-level, so an attended tab counts too: the safe direction, as
    the fire window cannot tell a nudge cycle from a dashboard turn.

    Admitted here, before anything is scheduled. A hold admitted before shutdown
    closes admission is drained, so it is on disk before ``shutdown()`` returns (the
    restart is the next wake that reads it); one after closure is dropped at DEBUG.
    Nothing raises into the approval path.
    """
    loop = self._find_by_slot(slot_key)
    if not loop or not loop.active or loop.approval_stalled:
        return
    try:
        admission = self._admit_mutation()
    except NudgeAdmissionRefused:
        logger.debug(
            "AutoNudge: shutting down -- not recording the approval hold for %s",
            slot_key,
        )
        return
    try:
        task = asyncio.ensure_future(_record_approval_hold(self, slot_key, admission=admission))
    except RuntimeError:
        # No running loop to write on. The hold is not recorded, which costs at
        # most one more declined cycle, and the next unanswered prompt records it.
        return
    self._inflight_adds.add(task)

    def _finish(t: "asyncio.Task[None]") -> None:
        self._inflight_adds.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning(
                "AutoNudge: recording the approval hold for %s failed; the loop "
                "keeps running until the next unanswered prompt",
                slot_key,
                exc_info=t.exception(),
            )

    task.add_done_callback(_finish)


async def _record_approval_hold(
    svc: AutoNudgeService,
    slot_key: str,
    *,
    admission: _MutationAdmission | None = None,
) -> None:
    """Set *slot_key*'s approval hold durably, then announce it.

    Persist before publishing, like ``release_approval_hold``: the flag and start
    time are staged on a copy, written under ``_lock``, and applied to the live loop
    only after the awaited write returns, so a hold the store refused is never shown
    or acted on. A ``CancelledError`` from the writer comes after the write settled,
    so the hold is kept and only the announcement is skipped.

    Admitted before the first await: ``notify_approval_stalled`` passes its lease and
    registers the task in ``_inflight_adds``, which ``shutdown()`` drains. A hold
    refused admission writes nothing and is logged at DEBUG.
    """
    if admission is None:
        try:
            admission = svc._admit_mutation()
        except NudgeAdmissionRefused:
            logger.debug(
                "AutoNudge: shutting down -- not recording the approval hold for %s",
                slot_key,
            )
            return
    async with svc._lock:
        loop = svc._find_by_slot(slot_key)
        if not loop or not loop.active or loop.approval_stalled:
            return
        # A structured monitor does not hold: its tick path never reads the flag
        # and the release refuses it. Its action-completion path does read it,
        # to settle the monitor as an ``approval_stall`` stop, so the flag is
        # still recorded for it -- with no hold time and no hold notice.
        structured = is_structured_monitor_loop(loop)
        stalled_at = 0.0 if structured else time.time()
        staged = deepcopy(loop)
        staged.approval_stalled = True
        staged.approval_stalled_at = stalled_at
        try:
            await svc._write_monitor_snapshot_locked(
                svc._monitor_snapshot_with_replacement(loop, staged),
                admission=admission,
            )
        except asyncio.CancelledError:
            # The writer re-raises only after the write settled: the hold is on
            # disk, so memory takes it too, and only the announcement is skipped.
            loop.approval_stalled, loop.approval_stalled_at = True, stalled_at
            note_hold_start(loop, APPROVAL_HOLD, stalled_at)
            raise
        except NudgeAdmissionRefused:
            # The lease belongs to a generation a restart has since replaced.
            # Nothing reached the store and the live row was never touched.
            logger.debug(
                "AutoNudge: the approval hold for %s was refused admission -- not recorded",
                slot_key,
            )
            return
        # Only the two hold fields are applied: a lock-free writer (the
        # turn-completion path) may have moved another field during the await.
        loop.approval_stalled, loop.approval_stalled_at = True, stalled_at
        note_hold_start(loop, APPROVAL_HOLD, stalled_at)
    if structured:
        return
    logger.warning(
        "AutoNudge: a tool approval went unanswered in loop %s's session -- "
        "it is paused for approval and fires no cycle until someone answers "
        "an approval, sends a message or fires it in that session",
        loop.id,
    )
    svc._emit("updated", loop)
    # A one-time notice, through the observer that announces the terminal
    # bounds: the loop is not stopped, but nobody is looking at it, and this is
    # the only signal that reaches a person who is away from the session.
    svc._emit("held", loop)


async def release_approval_hold(
    self: AutoNudgeService,
    slot_key: str,
    *,
    why: str,
    arm: bool = True,
    admission: _MutationAdmission | None = None,
) -> bool:
    """End *slot_key*'s approval hold, because the slot moved past it. True if one ended.

    Reached where a person is proven present -- an approval answered in the slot
    (via ``autonudge.release_approval_hold_for``), a typed dashboard message
    (``notify_user_input(human=True)``), the popover's manual fire -- and where a
    new dashboard turn starts in the slot for any sender (``_turn_open``): that
    turn proves no prompt is still open, and one that stalls again re-holds.

    The held time is added to ``created_ts`` so the hold spends no runtime budget.
    ``arm`` re-arms toward the (usually passed) deadline, so the next cycle runs within
    ``_OVERDUE_REARM_SECS``. A caller about to start a turn passes ``arm=False``: that
    turn's completion arms the loop ("user wins", as in ``notify_user_input``).

    Persist before publishing: the release is staged on a copy, written under
    ``_lock``, and applied only after the awaited write returns; a failed write leaves
    the loop held as the store says. A ``CancelledError`` from the writer comes after
    the write settled, so the release stands and the reconciler does the skipped arm.
    A structured monitor never holds, so it is left alone.

    Admitted before the first await (``_schedule_release`` passes its lease). The body
    runs in a detached registered child: a cancelled handler waiting for the lock stops
    at once, one cancelled mid-write is held until it settles. Shutdown drains it and
    arms nothing. After closure a held loop's direct caller gets
    ``NudgeAdmissionRefused`` (manual fire answers 503); with no hold, False.
    """
    if admission is None:
        try:
            admission = self._admit_mutation()
        except NudgeAdmissionRefused:
            current = self._find_by_slot(slot_key)
            if (
                not current
                or not current.active
                or not current.approval_stalled
                or is_structured_monitor_loop(current)
            ):
                return False
            logger.debug(
                "AutoNudge: shutting down -- not releasing the approval hold for %s (%s)",
                slot_key,
                why,
            )
            raise
    assert admission is not None
    return await _run_admitted_transaction(
        self,
        _release_approval_hold_admitted(
            self,
            slot_key,
            why=why,
            arm=arm,
            admission=admission,
        ),
        cancel_callback_on_exit=True,
        on_failure_log="AutoNudge: detached approval-hold release failed",
        expected=(NudgeAdmissionRefused,),
    )


async def _release_approval_hold_admitted(
    svc: AutoNudgeService,
    slot_key: str,
    *,
    why: str,
    arm: bool,
    admission: _MutationAdmission,
) -> bool:
    """The approval-hold release transaction once its caller is admitted."""
    async with svc._lock:
        loop = svc._find_by_slot(slot_key)
        if (
            not loop
            or not loop.active
            or not loop.approval_stalled
            or is_structured_monitor_loop(loop)
        ):
            return False
        now = time.time()
        since = loop.approval_stalled_at
        held = 0.0
        if isinstance(since, (int, float)) and not isinstance(since, bool) and 0 < since <= now:
            held = now - since
        created = loop.created_ts
        if (
            held
            and isinstance(created, (int, float))
            and not isinstance(created, bool)
            and created > 0
        ):
            created = created + held
        previous = loop.created_ts
        # The budget's anchor is handed back the hold's span on the monotonic clock;
        # ``created_ts`` keeps the wall span, which is what a restart reads.
        approval_start, span = hold_span(loop, APPROVAL_HOLD, since, now)
        staged = deepcopy(loop)
        staged.approval_stalled = False
        staged.approval_stalled_at = 0.0
        staged.created_ts = created
        # A person-wait hold that overlaps this one credits its own span on release.
        # Push its start forward by the overlap just credited here, so those seconds
        # are handed back once, not twice; its time before the overlap still counts.
        # Its monotonic start moves by the overlap on the monotonic clock, for the
        # same reason.
        pw_since = loop.waiting_on_person_at
        person_start: float | None = None
        if loop.waiting_on_person:
            start, _ = hold_span(loop, PERSON_WAIT_HOLD, pw_since, now)
            person_start = start + max(0.0, approval_start + span - max(start, approval_start))
        if (
            held
            and loop.waiting_on_person
            and isinstance(pw_since, (int, float))
            and not isinstance(pw_since, bool)
            and 0 < pw_since < now
        ):
            pw_since += now - max(pw_since, since)
        staged.waiting_on_person_at = pw_since

        def _apply(target: NudgeLoop) -> None:
            target.approval_stalled, target.approval_stalled_at = False, 0.0
            target.created_ts = created
            target.waiting_on_person_at = pw_since
            credit_held_runtime(target, previous, span)
            if person_start is not None:
                note_hold_start(target, PERSON_WAIT_HOLD, pw_since, mono=person_start)

        try:
            await svc._write_monitor_snapshot_locked(
                svc._monitor_snapshot_with_replacement(loop, staged),
                admission=admission,
            )
        except asyncio.CancelledError:
            # Settled before the re-raise: the release is durable, so keep it.
            _apply(loop)
            raise
        _apply(loop)
    logger.warning(
        "AutoNudge: loop %s resumed after %.0fs paused for approval (%s)",
        loop.id,
        held,
        why,
    )
    svc._emit("updated", loop)
    if arm and loop.active and loop.id in svc._loops:
        if loop.id in svc._firing:
            svc._rearm_pending.add(loop.id)
        else:
            svc._arm_from_deadline(loop)
    return True


def _schedule_release(svc: AutoNudgeService, slot_key: str, *, why: str, arm: bool = True) -> None:
    """Run :func:`release_approval_hold` detached, for a synchronous caller.

    Supervised through ``_inflight_adds`` like the judge label: strongly referenced,
    and a failed write is logged rather than lost. A plain helper taking *svc* rather
    than a service member, because it is reached from call sites in this module alone
    and binding it on the class would put a name on the service's surface that nothing
    outside here can use. Never raises: the approval and message paths that call it
    must not fail because a release could not be scheduled.

    Admitted here, where the person's action arrives, and the lease rides into
    the release. A release scheduled after gateway shutdown closes admission is
    dropped at DEBUG, as is one whose lease a restart has since replaced: the hold
    stays exactly as the store has it, and the next sign of a person releases it.
    """
    if not slot_key:
        return
    loop = svc._find_by_slot(slot_key)
    if loop is None or not loop.approval_stalled:
        return
    try:
        admission = svc._admit_mutation()
    except NudgeAdmissionRefused:
        logger.debug(
            "AutoNudge: shutting down -- not releasing the approval hold for %s (%s)",
            slot_key,
            why,
        )
        return
    try:
        task = asyncio.ensure_future(
            svc.release_approval_hold(slot_key, why=why, arm=arm, admission=admission)
        )
    except RuntimeError:
        # No running loop: nothing to schedule onto, and the hold stays until the
        # next sign of a person -- the harmless direction.
        return
    svc._inflight_adds.add(task)

    def _finish(t: "asyncio.Task[bool]") -> None:
        svc._inflight_adds.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is None:
            return
        if isinstance(exc, NudgeAdmissionRefused):
            logger.debug(
                "AutoNudge: approval-hold release for %s was refused admission; "
                "the loop stays paused",
                slot_key,
            )
            return
        logger.warning(
            "AutoNudge: approval-hold release for %s failed; the loop stays paused",
            slot_key,
            exc_info=exc,
        )

    task.add_done_callback(_finish)


def notify_cycle_start_failed(self: AutoNudgeService, slot_key: str) -> None:
    """Record that a turn in *slot_key* never obtained a model session.

    Called from the chat runner's terminal-error path when the failure is
    tagged ``session_start_failed`` and the turn was this loop's own cycle.
    Evidence, not inference: the turn was dispatched, spent its budget and
    produced nothing, which is the one thing that distinguishes a starved
    cycle from a quiet one.

    Records and returns. Like ``notify_approval_stalled``, the decision is
    left to ``_timer``, which owns every terminal and scheduling decision and
    evaluates them serialized before a fire -- deciding here would mean
    touching a timer that may be mid-fire.
    """
    loop = self._find_by_slot(slot_key)
    if not loop or not loop.active:
        return
    loop.consecutive_start_failures += 1
    # The conductor never read the ledger on this cycle, so the person-wait hold
    # must not treat it as seen: the next tick fires and tries again.
    loop.ledger_seen_fp = ""
    logger.warning(
        "AutoNudge: loop %s's cycle never got a model session "
        "(%d consecutive); it will back off at %d and stand down at %d",
        loop.id,
        loop.consecutive_start_failures,
        _START_FAILURE_BACKOFF_AFTER,
        _START_FAILURE_STANDDOWN_AFTER,
    )
    self._persist_soon()


async def notify_cycle_failed(
    self: AutoNudgeService,
    slot_key: str,
    *,
    loop_id: str,
    expected_generation: int,
) -> None:
    """Record that this loop's own delivered cycle in *slot_key* ended in a fault.

    Called from the chat runner's terminal-error paths for a turn that reached a
    model session and then died (``error`` or ``timeout``), excluding structural
    rejections and session-start failures. A spent, failed turn is what tells a loop
    making no progress from a quiet one. Only the loop's OWN cycle counts (the runner
    applies the self-wake guard, as for ``notify_cycle_start_failed``), so an erroring
    human turn cannot spend the stand-down budget.

    Fenced by id AND ``config_generation`` captured at fire time (``_directive_loop_id``,
    ``_directive_loop_gen``), matched under ``_lock``. The id catches a replaced loop
    with the same slot and generation (both start at 0); the generation catches a
    revision. Either mismatch is a stale fault and is dropped.

    The increment and its awaited write happen under ``_lock``; a failed write rolls
    the count back, and a ``CancelledError`` (raised only after the write settled)
    keeps it. The stand-down DECISION stays with ``_timer``; this only records it.
    """
    async with self._lock:
        loop = self._loops.get(loop_id)
        if loop is None or not loop.active or loop.slot_key != slot_key:
            return
        if loop.config_generation != expected_generation:
            # Stale completion of a now-revised loop: the generation advanced
            # between fire and fault, so this charge belongs to the old
            # instruction, not the loop live on the slot now.
            return
        if is_structured_monitor_loop(loop):
            # A structured monitor loop's ``_timer`` returns at its own
            # ``is_structured_monitor_loop`` guard (firing.py) BEFORE the only
            # reader of ``consecutive_failed_cycles`` (the consecutive-failure
            # stand-down), so charging it here would grow the counter
            # monotonically in the store and on the loop's API row while no bound
            # could ever act on it -- a write-only field. Monitor faults are
            # bounded by the monitor's own ``consecutive_provider_errors``
            # budget, not this stand-down, so skip the charge and keep the
            # counter meaning exactly what the bound that reads it expects.
            return
        # Persist BEFORE publishing: increment and write under the SAME lock hold,
        # rolling back ONLY when the store refused it. Two failure modes, two handlers:
        #   * A write error (``Exception``): the fsync did not land, so roll the live
        #     field back to what the store holds and propagate.
        #   * ``CancelledError`` from ``_write_monitor_snapshot_locked`` comes only
        #     AFTER its shielded write finished, so the increment is durable and
        #     stands; rolling it back would leave the live count one short.
        prior = loop.consecutive_failed_cycles
        loop.consecutive_failed_cycles = prior + 1
        # A failed turn read nothing, so the person-wait hold must not count its
        # ledger as seen; in memory only -- losing it on a restart costs one turn.
        loop.ledger_seen_fp = ""
        try:
            # ``_write_monitor_snapshot_locked`` offloads the fsyncing
            # ``_write_state`` to a worker thread and ABSORBS cancellation until
            # the executor result is observed, so the lock scope cannot release
            # (or this method return) around a half-applied snapshot -- the one
            # cancellation-safe writer the service already uses under the lock.
            await self._write_monitor_snapshot_locked()
        except asyncio.CancelledError:
            # The write already settled before the writer re-raised: the
            # increment is durable, so keep it and only propagate the cancel.
            raise
        except Exception:
            # The durable write actually failed: the increment is unaccepted
            # evidence, so unpublish it before propagating.
            loop.consecutive_failed_cycles = prior
            raise
        logger.warning(
            "AutoNudge: loop %s's cycle failed (%d consecutive); it will stand "
            "down at %d unless a turn lands first",
            loop.id,
            loop.consecutive_failed_cycles,
            _CONSECUTIVE_FAILURE_STANDDOWN_AFTER,
        )


def notify_cycle_landed(self: AutoNudgeService, slot_key: str) -> None:
    """Clear *slot_key*'s failure streaks: a turn on it completed.

    Any landed turn counts, a human's too: it proves the session can start
    (``consecutive_start_failures``) and make progress (``consecutive_failed_cycles``),
    and clearing can only keep a loop running, never stop one.

    SYNCHRONOUS and lock-free, because the turn-completion path calls it while a
    baseline commit may hold ``_lock`` mid-write (see
    ``test_a_streak_cleared_during_the_commit_write_stays_cleared``). The write is
    detached via ``_persist_soon``: a lost clear only leaves a stale streak, which can
    slow a loop but never fire one early. The dangerous direction is the increment,
    which ``notify_cycle_failed`` awaits under the lock with rollback.
    """
    loop = self._find_by_slot(slot_key)
    if loop is not None:
        # A landed turn read the board, so the work-ledger wake it answers owes no retry.
        self._ledger_wake_streak.pop(loop.id, None)
    if not loop or not (loop.consecutive_start_failures or loop.consecutive_failed_cycles):
        return
    loop.consecutive_start_failures = 0
    loop.consecutive_failed_cycles = 0
    # Drop the paid-deferral marker with the streak it belonged to: a streak
    # that climbs back to the same value must pay its own deferral again.
    self._start_failure_deferred.pop(loop.id, None)
    self._persist_soon()


def notify_turn_complete(
    self: AutoNudgeService,
    slot_key: str,
    *,
    tool_calls: int | None = None,
    reply_text: str | None = None,
    reply_flushed: bool = False,
    nudge_turn: bool | None = None,
    tool_identities: object = None,
) -> None:
    """Called by gateway after HOOK_EVENT_STOP — resume the countdown for this slot.

    Re-arms toward the persistent deadline (``_arm_from_deadline``), not a fresh
    interval: after a user turn the timer resumes the remaining time, while the nudge
    turn's own end finds the deadline cleared and starts the next full cycle. DEFERS
    while the loop's timer is mid-fire: cancelling it during ``_persist_locked()``
    would lose the ``cycle_count`` bump; the re-arm is applied when the window closes.

    *tool_calls*, *tool_identities* and *reply_text* are what the turn DID; only this
    hook sees them. *tool_identities* names each dispatch, so a turn that only read a
    file does not count as acting (absent, the count is used). *nudge_turn* binds the
    facts to this loop's delivered turn; unknown reads as not ours, since a wrong
    label is worse than a missing one. *reply_flushed* says the text is only the
    reply's final segment. None is stored: the rule reduces them to one boolean, two
    numbers and a flag.
    """
    loop = self._find_by_slot(slot_key)
    if not loop or not loop.active:
        return
    # Before the re-arm and before the mid-fire deferral, because this labels the
    # turn that just ENDED. A deferred re-arm postpones the next tick; the verdict
    # that woke this one is already decided and is waiting for exactly this answer.
    if loop.judge_recent_verdicts and nudge_turn is True:
        try:
            # The local import keeps the decisions graph off the gateway boot path.
            from kiro_crew import autonudge_judge as judge

            asyncio.get_running_loop()
            acted, tool_call_count, reply_chars, names_known = judge.owner_action_reading(
                tool_calls,
                reply_text,
                reply_flushed=reply_flushed,
                tool_identities=tool_identities,
            )
            task = asyncio.ensure_future(
                self._label_judge_delivery_locked(
                    loop,
                    acted,
                    tool_calls=tool_call_count,
                    reply_chars=reply_chars,
                    tool_names_known=names_known,
                )
            )
        except RuntimeError:
            logger.debug(
                "AutoNudge: skipped judge delivery label for loop %s without "
                "a running event loop",
                loop.id,
            )
        except Exception:
            logger.debug(
                "AutoNudge: could not schedule the judge delivery label for loop %s",
                loop.id,
                exc_info=True,
            )
        else:
            self._inflight_adds.add(task)

            def _finish(t: "asyncio.Task[None]") -> None:
                self._inflight_adds.discard(t)
                if not t.cancelled() and t.exception() is not None:
                    logger.warning(
                        "AutoNudge: detached judge delivery label failed for loop %s",
                        loop.id,
                        exc_info=t.exception(),
                    )

            task.add_done_callback(_finish)
    if loop.id in self._firing:
        self._rearm_pending.add(loop.id)
        return
    if loop.id in self._pending_monitor_wake:
        # A wake this loop OWES (``_fire_dashboard_nudge`` declined the busy slot),
        # and that turn has just ended: retry now, or the wake waits out a whole
        # interval. Delay zero, not ``_arm_from_deadline``'s overdue beat, which
        # guards a turn that has just finished. The retry runs the ordinary
        # ``_timer`` body, so every bound applies and a busy slot re-owes.
        logger.debug(
            "AutoNudge: loop %s has a wake owed and its turn just ended -- "
            "retrying the delivery now rather than at its deadline",
            loop.id,
        )
        self._arm_timer(loop, delay=0.0)
        return
    self._arm_and_announce_deadline(loop)


def _arm_and_announce_deadline(self: AutoNudgeService, loop: NudgeLoop) -> None:
    """Re-arm toward the deadline, and announce a deadline this re-arm set.

    A delivered fire clears ``next_due_ts`` and its ``fired`` frame carries that 0,
    so the dashboard caches a loop with no deadline. The first re-arm after the
    nudge turn sets the next deadline here, and without a frame the popover reads
    "not yet scheduled" until a reload re-seeds it from REST. Only a deadline this
    re-arm SET is announced: resuming an unchanged countdown publishes nothing new.
    """
    cleared = loop.next_due_ts <= 0
    self._arm_from_deadline(loop)
    if cleared and loop.next_due_ts > 0 and self._loops.get(loop.id) is loop:
        self._emit("updated", loop)


def notify_user_input(self: AutoNudgeService, slot_key: str, *, human: bool = False) -> None:
    """Called when user sends a message — cancel the pending nudge task.

    *human* says a PERSON typed it (not an app token). That is proof someone is
    back in the slot, so it also ends an approval hold (``release_approval_hold``,
    unarmed: this turn's own completion re-arms the loop).

    Cancelling the TASK defers delivery until the user's turn ends (a nudge never
    races a human turn); ``next_due_ts`` is untouched, so ``notify_turn_complete``
    resumes the same countdown.

    Mid-fire it must NOT cancel: the task may be in ``_persist_locked()``, and an
    abandoned write can later overwrite a newer update/delete. The deferred re-arm is
    dropped instead, so the user still wins.
    """
    if human:
        _schedule_release(self, slot_key, why="a person sent a message", arm=False)
    loop = self._find_by_slot(slot_key)
    if not loop:
        return
    # A user turn starting is proof the slot is alive: restart the
    # reconciler's two-pass clock so the stranded-loop backstop never
    # re-arms a timer this hook is about to cancel on purpose. If this
    # turn then dies without its stop hook, candidacy simply rebuilds
    # over the next two passes and the rescue still happens.
    self._reconcile_candidates.discard(loop.id)
    if loop.id in self._firing:
        self._rearm_pending.discard(loop.id)
        logger.info(
            "AutoNudge: user input during loop %s's fire window — dropped the "
            "deferred re-arm instead of cancelling mid-persist",
            loop.id,
        )
        return
    self._cancel_timer(loop.id)


def _cancel_timer(self: AutoNudgeService, loop_id: str, *, drop_claims: bool = True) -> None:
    """Retire one loop's timer task. The single cancellation policy.

    Three cases where a cancel is wrong, not just redundant:

    * **The running timer task itself** (a self-re-arm inside ``_timer``, or a cancel
      from ``update``'s shielded mutation issued by the tick, via
      :data:`_ACTING_TIMER`): cancelling injects a spurious ``CancelledError``.
    * **A timer callback whose admitted transaction runs in a detached child.** The
      child inherits ``_admitted_transaction_owner`` so bookkeeping still sees the
      original callback as self-cancellation; ``_emit`` clears it for observers.
      After shutdown closes admission, the transaction helper cancels the callback
      once the child settles, only while it still awaits the transaction.
    * **A task whose event loop has closed.** ``Task.cancel`` would raise
      ``RuntimeError: Event loop is closed`` out of ``remove``/``remove_sync`` (a 500),
      and this singleton's ``_timers`` outlive replaced loops (gateway shutdown, tests).
      Checked via ``get_loop().is_closed()`` rather than catching, so a real
      scheduling fault is not swallowed.

    The closed-loop check runs FIRST because ``stop()`` calls this from synchronous
    callers where ``asyncio.current_task()`` would raise, hence
    :func:`_current_task_or_none`.
    """
    # A boot hold belongs to the timer it was set for; a re-arm starts without it.
    self._boot_holds.pop(loop_id, None)
    t = self._timers.pop(loop_id, None)
    if t is None or t.done():
        return
    # Closed-loop check FIRST: it needs no running loop of its own, so a dead timer is
    # retired even from a synchronous caller.
    if t.get_loop().is_closed():
        logger.debug(
            "AutoNudge: dropped loop %s's timer without cancelling — its event loop "
            "has closed, so the task can no longer run",
            loop_id,
        )
        return
    current = _current_task_or_none()
    if t is current or t is _ACTING_TIMER.get() or t is self._admitted_transaction_owner.get():
        return
    t.cancel()
    if not drop_claims:
        # Replacing a timer is not cancelling a cycle. ``_arm_timer`` cancels before
        # it creates, so every ordinary re-arm came through here -- including the
        # backoff re-arm on the refused-fire path, which erased the claim that same
        # path had re-owed one statement earlier. The accounting fix was defeated by
        # the cleanup meant to protect it.
        return
    # A cancelled CYCLE drops its claim. Without this the id stays in the claim set
    # and the loop's next delivered fire -- a fallback, a floor tick -- inherits it
    # and is charged as a wake as well, counting one delivered turn under two
    # counters. That trade is deliberate: an undelivered observation is lost rather
    # than attributed to a turn that did not carry it.
    # The claim carries the wake's text, so dropping the claim drops the text with
    # it -- which is the point of keeping them in one value. A cancelled cycle's
    # briefs must not ride the loop's next fire: they would name items a turn
    # nobody delivered had observed.
    self._pending_monitor_wake.pop(loop_id, None)
    self._pending_floor_tick.discard(loop_id)


def _arm_timer(self: AutoNudgeService, loop: NudgeLoop, delay: float | None = None) -> None:
    # A drained owner must not repopulate the timer table shutdown just emptied.
    if not self._accepting_mutations:
        return
    self._cancel_timer(loop.id, drop_claims=False)
    # Any arm replaces the armed timer, so a push mark naming the old one is stale. A
    # push re-sets it right after this call.
    self._pushed_ticks.discard(loop.id)
    self._timers[loop.id] = asyncio.create_task(self._timer(loop, delay))


def _arm_from_deadline(self: AutoNudgeService, loop: NudgeLoop) -> None:
    """(Re)arm the timer toward the loop's persistent deadline.

    Anchored on ``next_due_ts`` so user turns defer a pending fire without pushing the
    schedule back. An unset deadline (0: a just-delivered fire, a legacy entry) starts
    a full countdown, persisted in a supervised background write so a restart resumes
    it. A future deadline resumes with the remaining time; a past one fires after
    ``_OVERDUE_REARM_SECS``, so a user mid-conversation keeps deferring it. The delay
    is capped at ``idle_secs`` so a clock jump cannot park the timer past one interval.

    ``_timer`` stops a prompt loop on a spent runtime budget only when it wakes, so a
    budget that runs out before the deadline wakes it then; which arms keep the
    deadline, and why, is in the monitor-architecture spec ("Runtime bounds").

    A monitor arms on the same deadline (one user-set clock); the probe gate in
    :meth:`_timer` decides whether its tick spends a turn. A monitor whose
    ``version`` this gateway does not implement (a newer gateway's record) gets no
    timer: arming would run a policy nobody here understands. The record is left
    as is, so its ``active`` intent survives a downgrade and an upgrade resumes it.

    After ``shutdown()`` closes admission nothing is armed, so no timer or deadline
    persist outlives the teardown.
    """
    from kiro_crew import autonudge as seams  # read at call time: the facade imports us

    if not self._accepting_mutations:
        return
    monitor = loop.monitor
    if monitor is not None and monitor.version != MONITOR_STATE_VERSION:
        logger.info(
            "AutoNudge: not arming loop %s -- its monitor record is version %s and "
            "this gateway implements %s",
            loop.id,
            monitor.version,
            MONITOR_STATE_VERSION,
        )
        return
    now = time.time()
    if loop.next_due_ts <= 0:
        loop.next_due_ts = now + loop.idle_secs
        if loop.monitor is not None:
            loop.monitor.next_probe_at = loop.next_due_ts
        self._persist_soon()
    if _held_tick_rearms(loop) and not is_structured_monitor_loop(loop):
        # A held gated loop naming one pull request: its timer is its subject read
        # (``subject_reads.hold_tick``), one read interval out whatever its deadline,
        # from every arm: a turn ending, a deferred re-arm, a reconciler rescue. Any
        # other held loop is armed toward its deadline as any loop is, so its tick
        # still reaches the cycle cap and the stop sentinel. The release arms toward
        # the deadline.
        self._arm_timer(loop, delay=SUBJECT_READ_SECS)
        return
    remaining = loop.next_due_ts - now
    if remaining <= 0:
        delay = float(seams._OVERDUE_REARM_SECS)
    else:
        delay = min(remaining, float(loop.idle_secs))
    budget = _positive_number(loop.max_runtime_secs)
    anchor = _positive_number(loop.created_ts)
    prompt_loop = not is_structured_monitor_loop(loop)
    if budget and anchor and prompt_loop and not loop.waiting_on_person:
        left = budget - runtime_elapsed(loop, anchor)
        if left > 0:
            delay = min(delay, left + _BUDGET_WAKE_SLACK_SECS)
    self._arm_timer(loop, delay=delay)


async def _reconcile_forever(self: AutoNudgeService) -> None:
    """Periodically rescue any active loop left with no live timer.

    After a delivered fire, ``notify_turn_complete`` is a dashboard loop's only
    re-arm path. If the stop hook never arrives (the turn dies on a path that
    skips it, or ``notify_user_input`` dropped the deferred re-arm), the loop
    stays ``active=true`` with no timer until a restart. Rescues re-arm toward
    the loop's persisted deadline, so they never fire before the user's schedule.

    The wait uses ``loop.call_later``, not ``asyncio.sleep``: tests patch
    ``asyncio.sleep`` to a no-op, which would turn this task into a busy loop.
    """
    from kiro_crew import autonudge as seams  # read at call time: the facade imports us

    ev_loop = asyncio.get_running_loop()
    while True:
        beat: asyncio.Future[None] = ev_loop.create_future()
        handle = ev_loop.call_later(seams._RECONCILE_INTERVAL_SECS, _resolve_beat, beat)
        try:
            await beat
        finally:
            handle.cancel()
        if shutdown_event.is_set():
            return
        try:
            self._reconcile_once()
        except Exception:  # noqa: BLE001 - one bad pass must not kill the backstop
            logger.exception("AutoNudge: reconciler pass failed")


def _reconcile_once(self: AutoNudgeService) -> None:
    """One reconciler pass: rescue active loops stranded with no live timer.

    "No live timer" means the ``_timers`` entry is absent OR its task is done:
    nothing pops a finished task, so a membership test alone misses strandings.

    A loop is re-armed only after TWO CONSECUTIVE passes see it unarmed, because
    one look cannot tell "stranded" from two live states:

    * A user turn still running (``notify_user_input`` cancelled the timer and
      clears candidacy). A turn outliving both passes is re-armed anyway; the
      fire path's busy-slot refusal keeps it out of the running turn.
    * Another coroutine's mutation window: ``update()`` may roll fields back
      after an untimed write. The lock check closes this; every mutation holds
      ``self._lock`` and this pass is synchronous.

    Never touched:

    * A loop mid-fire (``_firing``): ``_arm_timer`` cancels before it creates.
    * A loop quiesced by administrative cleanup: cleanup owns it.
    * A monitor version this gateway does not implement: ``_arm_from_deadline``
      would log its refusal every pass.
    * A monitor wake claim in flight with no completion-evidence deadline,
      except a ``BUSY`` retry. That claim died mid-handoff (``_load`` retires
      it); arming it would wake a controller answering ``NO_CHANGE`` forever.
      A ``BUSY`` claim is live and resumes at its retry deadline, as in
      ``_load``. A claim with a deadline is safe: the tick finds evidence or
      retires it via ``record_monitor_completion_evidence_unavailable``.
    """
    if self._lock.locked():
        # A mutation or persist is mid-flight: ``update()`` may roll fields back
        # inside ``self._lock`` after an untimed write, so a wedged disk can hold
        # that transient shape for any number of passes. This pass is synchronous,
        # so deferring while the lock is held rules out overlap. Candidacies stay
        # untouched: a deferred pass neither confirms nor refutes them.
        return
    eligible: set[str] = set()
    for loop in list(self._loops.values()):
        # Mirror _timer's own re-arm guard, not a stricter one: an
        # INACTIVE loop still waiting for terminal-completion evidence
        # owns a finite accepted-turn correlation whose expiry needs a
        # timer (_waits_for_terminal_completion), and losing that timer
        # to a user-input cancel with no turn-complete re-arm (the hook
        # ignores inactive loops) would otherwise strand the claim and
        # refuse every replacement watch on the slot forever.
        if not loop.active and not self._waits_for_terminal_completion(loop):
            continue
        if loop.id in self._firing or loop.id in self._maintenance_quiescing:
            continue
        if (
            loop.approval_stalled
            and not is_structured_monitor_loop(loop)
            and not _held_tick_rearms(loop)
        ):
            # Paused for approval with no pull request to read (or ungated): no live
            # timer is the intended state, and ``release_approval_hold`` re-arms it. A
            # gated one that names a pull request re-arms itself to keep reading it
            # (``subject_reads.hold_tick``), so it is rescued like any other.
            continue
        monitor = loop.monitor
        if monitor is not None and monitor.version != MONITOR_STATE_VERSION:
            continue
        if (
            monitor is not None
            and monitor.wake_in_flight
            and monitor.completion_evidence_deadline <= 0
            and monitor.wake_delivery is not MonitorDispatchResult.BUSY
        ):
            # A BUSY retry is EXEMPT from this skip: it proves no action
            # turn started, its evidence deadline is intentionally empty,
            # and _load resumes exactly this shape at its persisted retry
            # deadline on restart -- so retiring it here would kill a
            # retry the store's own recovery logic considers live.
            continue
        timer = self._timers.get(loop.id)
        if timer is not None and not timer.done():
            continue
        if loop.id not in self._reconcile_candidates:
            eligible.add(loop.id)
            continue
        logger.info(
            "AutoNudge: reconciler re-arming stranded loop %s on slot %s "
            "(active with no live timer across two passes)",
            loop.id,
            loop.slot_key,
        )
        self._arm_from_deadline(loop)
    self._reconcile_candidates = eligible
