"""The legacy loop transactions: arm, update, deactivate and remove.

Every transaction snapshots under the service lock, writes on a worker thread and
treats the rename as its commit point: a failed write restores the prior live loop and
timer, a removal restores its row for an immediate retry. A caller cancelled mid-write
leaves the shielded inner task holding the lock until the executor write settles.
Provider-credential denial is made durable before an agent-writable row can disappear,
and the self-arm trust entry is revoked only after the store committed the removal.

Its functions are :class:`~kiro_crew.autonudge.AutoNudgeService` methods: each is bound
on the class by name and runs against the service's state through ``self``, and a call
to any other service method goes through ``self`` too, so a patch on the instance
reaches it.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from copy import deepcopy
from dataclasses import fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew import autonudge_stop_log
from kiro_crew.autonudge_service.gate import _clear_short_reading_hold
from kiro_crew.autonudge_service.maintenance import (
    _assert_mutation_lock_owned,
    _claim_mutation_lock,
    _maintenance_lock,
    _monitor_mutation_guard,
    _release_mutation_lock,
    _unclaim_mutation_lock,
)
from kiro_crew.autonudge_service.model import (
    _KEPT_STOP_REASONS,
    _MAX_IDLE_SECS,
    _MIN_IDLE_SECS,
    AUTONUDGE_STOP_REASON,
    CYCLE_CAP_REASON,
    FINISHED_LOOP_REASONS,
    MANUAL_STOP_REASON,
    RUNTIME_BUDGET_REASON,
    SERVICE_SHUTTING_DOWN_MESSAGE,
    STOP_SENTINEL_REASON,
    AutoNudgeStaleBaseline,
    MonitorUpdateConflict,
    NudgeAdmissionReason,
    NudgeAdmissionRefused,
    NudgeLoop,
    _MutationAdmission,
    _stopped_row_is_replaceable,
    budget_elapsed,
    cap_reached,
    is_structured_monitor_loop,
    new_goal_token,
    reason_in,
    restart_runtime_clock,
    restore_runtime_anchor,
    snapshot_runtime_anchor,
    unreadable_bound_field,
)
from kiro_crew.autonudge_service.subject import (
    infer_monitor,
    infer_subject,
    needs_session_texts,
    read_session_texts,
)
from kiro_crew.autonudge_service.timers import _run_admitted_transaction
from kiro_crew.monitoring.limits import validate_runtime_secs
from kiro_crew.monitoring.models import MONITOR_STATE_VERSION, MonitorCreationSurface

if TYPE_CHECKING:
    from kiro_crew.autonudge import AutoNudgeService

# The service's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.autonudge")


def _stop_file_lifted(loop: NudgeLoop) -> bool:
    """Whether a loop its stop file finished may run again: the file is gone.

    Only a ``stop_sentinel`` row with a known path answers True, and only while
    nothing exists at that path. A finished watch has no file to lift, and a row
    whose path was dropped at load (``repair_sentinel_path``) has nothing to
    consult, so both stay finished. The same ``exists`` the timer runs.
    """
    if loop.stopped_reason != STOP_SENTINEL_REASON or not loop.stop_sentinel_path:
        return False
    return not Path(loop.stop_sentinel_path).exists()


async def add(
    self: AutoNudgeService,
    slot_key: str,
    message: str,
    idle_secs: int = 60,
    max_cycles: int = 0,
    stop_sentinel_path: str = "",
    max_runtime_secs: int = 0,
    banner: str = "",
    admission_check: Callable[[], bool] | None = None,
    # UNGATED by default, and the default lives at the ARMING SURFACES instead.
    # The evidence for gating is about monitor_start -- a babysit loop whose work
    # IS the pull request. This service also arms loops whose work is not: a goal
    # loop, an app's own timer. Defaulting to gated here inferred a monitor from
    # any message that merely MENTIONED one PR, which throttles such a loop and,
    # if that PR is already merged, deactivates it before its first turn.
    gate: bool = False,
    judge: dict | None = None,
    watch: str = "",
    replace_existing: bool = True,
    replace_stopped: bool = False,
    self_armed: bool = False,
    loop_id: str | None = None,
    creation_surface: MonitorCreationSurface = MonitorCreationSurface.DASHBOARD,
    default_patrol: bool = False,
    #: The stale stop file a finished goal left at this slot's default path, to be
    #: removed inside this transaction (``_retire_stale_stop_sentinel``).
    retire_stale_stop_sentinel: Path | None = None,
) -> NudgeLoop:
    # A stale stop file that cannot be removed saves the new loop finished, which is
    # a second write in this transaction, so only that add takes a multi-write lease.
    admission = self._admit_mutation(
        allow_multiple_persistence=retire_stale_stop_sentinel is not None
    )
    # CANCELLATION SAFETY: the mutate+persist runs as a SHIELDED task, so a caller
    # cancelled mid-write cannot release ``_lock`` while the executor write runs; a
    # later write would land first and be clobbered by this stale snapshot. The
    # cancelled caller sees CancelledError with the arm possibly landed.
    # ``_inflight_adds`` keeps the inner task referenced and completion-logged.
    inner: "asyncio.Task[NudgeLoop]" = asyncio.ensure_future(
        self._add_locked(
            slot_key,
            message,
            idle_secs=idle_secs,
            max_cycles=max_cycles,
            stop_sentinel_path=stop_sentinel_path,
            max_runtime_secs=max_runtime_secs,
            banner=banner,
            admission_check=admission_check,
            gate=gate,
            judge=judge,
            watch=watch,
            replace_existing=replace_existing,
            replace_stopped=replace_stopped,
            self_armed=self_armed,
            loop_id=loop_id,
            creation_surface=creation_surface,
            default_patrol=default_patrol,
            retire_stale_stop_sentinel=retire_stale_stop_sentinel,
            admission=admission,
        )
    )
    self._inflight_adds.add(inner)

    def _finish(t: "asyncio.Task[NudgeLoop]") -> None:
        self._inflight_adds.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning("AutoNudge: detached add() failed", exc_info=t.exception())

    inner.add_done_callback(_finish)
    return await asyncio.shield(inner)


def _mint_loop_id(self: AutoNudgeService, requested: str | None) -> str:
    """The new loop's id: the caller's pre-minted one, else a fresh one.

    A caller pre-mints an id when something must be recorded ABOUT the loop
    before it exists -- the authorizer writes the keystone-gated self-arm
    entry first, so a failed trust write denies before this store is
    touched and a loop this arm would displace is never removed for
    nothing. Called under ``_lock``, so the in-use check is not racy; an id
    already in use is a caller bug (or a collision on 8 hex chars, which is
    not worth silently re-minting over -- the caller's record would name
    the wrong loop) and is refused as a conflict.
    """
    if requested is None:
        return uuid.uuid4().hex[:8]
    if requested in self._loops:
        raise MonitorUpdateConflict(f"loop id {requested!r} is already in use")
    return requested


def detach_firing_default_timer(svc: AutoNudgeService, existing: NudgeLoop) -> Any:
    """Unregister a FIRING default patrol's timer without cancelling it.

    The conductor's own arm usually runs inside that patrol's wake. On a channel
    session the wake's timer task awaits the whole turn, so the cancel that
    ``remove_sync`` would apply aborts the very turn issuing the arm. Taking the
    task out of ``_timers`` first lets ``remove_sync`` find nothing to cancel;
    the delivery finishes, and the fire cycle's own ``loop.id not in
    self._loops`` check drops its bookkeeping for the removed row. Returns the
    detached task (or ``None``) so a failed replacement can put it back.
    """
    if existing.id not in svc._firing:
        return None
    return svc._timers.pop(existing.id, None)


async def _retire_stale_stop_sentinel(
    svc: AutoNudgeService,
    loop: NudgeLoop,
    path: Path,
    admission: _MutationAdmission | None,
) -> bool:
    """Remove the stop file a finished goal left at *path*; True when *loop* may run.

    Called by ``_add_unserialized`` once *loop* is durable and before its timer is
    armed. When the file cannot be removed and the timer's own ``exists`` still sees
    it, the first tick would finish *loop* under ``stop_sentinel``, so the loop is
    saved that way now, timerless, in a second write under the add's lease. If that
    write fails too, the active row stands and its first tick finishes it.
    """
    try:
        await asyncio.to_thread(path.unlink, missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - the row is committed; the add must not fail
        failure = exc
    else:
        return True
    try:
        still_there = await asyncio.to_thread(path.exists)
    except Exception:  # noqa: BLE001 - an entry that cannot be read reads as still there
        still_there = True
    if not still_there:
        return True
    logger.warning(
        "AutoNudge: could not remove the old stop file %r (%r); loop %r is saved "
        "finished, as its first tick would have left it",
        str(path),
        failure,
        loop.id,
    )
    # Persist before publishing: the finished state is written from a staged copy, and
    # the live row changes only once that write is durable, so a concurrent reader
    # never sees a stop the store does not hold, and a failed write changes nothing.
    staged = deepcopy(loop)
    staged.active, staged.next_due_ts, staged.stopped_reason = False, 0.0, STOP_SENTINEL_REASON
    try:
        await asyncio.shield(
            svc._start_persistence(
                svc._monitor_snapshot_with_replacement(loop, staged), admission=admission
            )
        )
    except Exception:  # noqa: BLE001 - the committed active row stands
        logger.warning("AutoNudge: loop %r could not be saved finished", loop.id, exc_info=True)
        return True
    loop.active, loop.next_due_ts, loop.stopped_reason = False, 0.0, STOP_SENTINEL_REASON
    return False


async def _add_locked(
    self: AutoNudgeService,
    slot_key: str,
    message: str,
    *,
    idle_secs: int,
    max_cycles: int,
    stop_sentinel_path: str,
    max_runtime_secs: int = 0,
    banner: str = "",
    admission_check: Callable[[], bool] | None = None,
    gate: bool = False,
    judge: dict | None = None,
    watch: str = "",
    replace_existing: bool = True,
    replace_stopped: bool = False,
    self_armed: bool = False,
    loop_id: str | None = None,
    creation_surface: MonitorCreationSurface = MonitorCreationSurface.DASHBOARD,
    default_patrol: bool = False,
    retire_stale_stop_sentinel: Path | None = None,
    admission: _MutationAdmission | None = None,
) -> NudgeLoop:
    async with _maintenance_lock(self._base_dir):
        return await self._add_unserialized(
            slot_key,
            message,
            idle_secs=idle_secs,
            max_cycles=max_cycles,
            stop_sentinel_path=stop_sentinel_path,
            max_runtime_secs=max_runtime_secs,
            banner=banner,
            admission_check=admission_check,
            gate=gate,
            judge=judge,
            watch=watch,
            replace_existing=replace_existing,
            replace_stopped=replace_stopped,
            self_armed=self_armed,
            loop_id=loop_id,
            creation_surface=creation_surface,
            default_patrol=default_patrol,
            retire_stale_stop_sentinel=retire_stale_stop_sentinel,
            admission=admission,
        )


async def _add_unserialized(
    self: AutoNudgeService,
    slot_key: str,
    message: str,
    *,
    idle_secs: int,
    max_cycles: int,
    stop_sentinel_path: str,
    max_runtime_secs: int = 0,
    banner: str = "",
    admission_check: Callable[[], bool] | None = None,
    gate: bool = False,
    judge: dict | None = None,
    watch: str = "",
    replace_existing: bool = True,
    replace_stopped: bool = False,
    self_armed: bool = False,
    loop_id: str | None = None,
    creation_surface: MonitorCreationSurface = MonitorCreationSurface.DASHBOARD,
    default_patrol: bool = False,
    retire_stale_stop_sentinel: Path | None = None,
    admission: _MutationAdmission | None = None,
) -> NudgeLoop:
    from kiro_crew import autonudge as seams  # read at call time: the facade imports us

    validate_runtime_secs(max_runtime_secs, allow_unbounded=True)
    idle_secs = max(_MIN_IDLE_SECS, min(_MAX_IDLE_SECS, int(idle_secs)))
    # A bare ``PR <number>`` resolves against the session's own log. Reading it is a
    # whole-transcript read, so it runs in a worker thread BEFORE the lock is taken,
    # never on the event loop and never while other mutations wait.
    arm_session_texts = (
        await asyncio.to_thread(read_session_texts, message, slot_key, watch)
        if (gate or watch) and needs_session_texts(message, slot_key, watch)
        else None
    )
    async with self._lock:
        if not self._mutation_allowed(admission):
            raise NudgeAdmissionRefused(
                SERVICE_SHUTTING_DOWN_MESSAGE,
                reason=NudgeAdmissionReason.SERVICE_SHUTTING_DOWN,
            )
        if admission_check is not None and not admission_check():
            raise NudgeAdmissionRefused(
                "session changed before nudge arm committed",
                reason=NudgeAdmissionReason.SESSION_CHANGED,
            )
        # One loop per slot — replace any existing loop on this slot.
        # persist=False: the offloaded write below persists the combined
        # removal+add atomically, avoiding a duplicate blocking save here.
        existing = self._find_by_slot(slot_key)
        restore_existing_provider_credentials = False
        detached_timer: Any = None
        # An agent's own create-only arm replaces the gateway's ACTIVE default
        # patrol rather than meeting a 409 (``NudgeLoop.default_patrol``). A
        # stopped default patrol keeps the ordinary retained-row rules below, so a
        # person's stop of it is still evidence, and one default never displaces
        # another.
        displaces_default = bool(
            existing is not None
            and existing.default_patrol is True
            and existing.active
            and not default_patrol
        )
        if existing:
            # Create-only (``replace_existing=False``) refuses ANY existing record:
            # the dashboard REST creates promise a 409 that never discards a retained
            # inspection record. ``replace_stopped`` narrows the refusal to ACTIVE
            # records for the directive re-arm, because a retained INACTIVE row would
            # otherwise deadlock it (monitor_update's approval-stall refusal names
            # monitor_start as the remedy). The wake-in-flight guard below still runs,
            # so a record whose accepted wake awaits completion keeps its refusal.
            if (
                not replace_existing
                and not displaces_default
                and (existing.active or not replace_stopped)
            ):
                raise MonitorUpdateConflict("session already has an automation")
            existing_monitor = existing.monitor
            if (
                not replace_existing
                and existing_monitor is not None
                and existing_monitor.version != MONITOR_STATE_VERSION
            ):
                # A future-version record belongs to the newer gateway that
                # wrote it: _load() retains it inactive so an upgrade can
                # resume the watch, and the retarget path refuses to touch
                # it for the same reason. A stopped-replacement here would
                # destroy state this gateway cannot even read. Checked
                # BEFORE the evidence allowlist so the version message —
                # the actionable one — wins for such records.
                raise MonitorUpdateConflict(
                    "the session's stopped automation was written by a newer "
                    "gateway and cannot be replaced by this one"
                )
            if (
                not replace_existing
                and replace_stopped
                and not displaces_default
                and not _stopped_row_is_replaceable(existing)
            ):
                # Owner ruling (option A): only system-imposed stops are
                # re-armable. A stop recorded FOR a consumer — a research
                # tombstone, a manual pause, a user stop, session-close
                # retention — is evidence, and an unknown reason is
                # treated as evidence too.
                raise MonitorUpdateConflict(
                    "the session's stopped automation is retained as evidence "
                    f"(stop reason: {existing.stopped_reason or 'none recorded'!s}) "
                    "and is not replaceable by a re-arm; its owner must clear it "
                    "first from the dashboard's goal popover"
                )
            # A default patrol's in-flight wake is usually the very turn issuing
            # this arm; nothing waits on its completion once the agent's own loop
            # replaces it, so it does not hold the replacement off.
            if (
                existing_monitor is not None
                and existing_monitor.wake_in_flight
                and not displaces_default
            ):
                raise MonitorUpdateConflict(
                    "existing monitor cannot be replaced while a wake is in flight"
                )
            restore_existing_provider_credentials = await self._provider_credentials_authorized(
                existing
            )
            await self._revoke_provider_credentials_before_removal(existing.id)
            if displaces_default:
                detached_timer = detach_firing_default_timer(self, existing)
            self.remove_sync(existing.id, persist=False, emit=False)
        now = time.time()
        # Scrubbed ONCE, then used for both the stored field and the subject the
        # monitor is built from. Two calls would be two values: the scrub may
        # rewrite a target, and a watch bound to a string the loop does not carry
        # is a watch whose own reader drops its reading.
        stored_judge = seams.scrubbed_judge_spec(judge) if isinstance(judge, dict) else {}
        loop = NudgeLoop(
            id=self._mint_loop_id(loop_id),
            slot_key=slot_key,
            message=message,
            idle_secs=idle_secs,
            max_cycles=max(0, int(max_cycles)),
            created_ts=now,
            goal_token=new_goal_token(),
            stop_sentinel_path=stop_sentinel_path,
            max_runtime_secs=max(0, int(max_runtime_secs)),
            # The judge brief the caller supplied, or nothing. Scrubbed and bounded
            # HERE as well as at the decode boundary, because the two entrances are
            # independent: a store row comes off disk, and this one comes from a
            # tool call and is serialized to the dashboard without ever passing the
            # decode path. Stored whatever the consent scope says, so a loop armed
            # today is judged once the scope is granted -- the tick, not the arm, is
            # where that is decided.
            judge=stored_judge,
            # Anchor the first deadline at arm time (set BEFORE the
            # snapshot below so it persists): the countdown starts the
            # moment the loop is armed, and user turns from here on only
            # defer delivery, never restart it.
            next_due_ts=now + idle_secs,
            # The SUBJECT is inferred HERE from the instruction the caller wrote;
            # no target or kind is ever passed, because an opt-in subject parameter
            # goes unused. WHETHER to look is ``gate``: each arming surface chooses
            # (monitor_start's directive gates by default; this service and the
            # generic REST route do not, since they also arm non-PR loops).
            # ``gate=False`` lets a loop that must act WHILE its subject is quiet opt
            # out, instead of keying that cadence on the instruction's wording.
            monitor=(
                infer_monitor(
                    message,
                    now,
                    creation_surface=creation_surface,
                    judge=stored_judge,
                    watch=watch,
                    slot_key=slot_key,
                    session_texts=arm_session_texts,
                )
                # An explicit ``watch`` gates on its own, without ``gate``. This
                # path defaults to UNGATED for the reason above, so requiring
                # both would make the field work from monitor_start and do
                # nothing here -- and no caller means "observe this subject, and
                # ignore it". ``slot_key`` is what makes a work-ledger watch
                # possible at all: its subject is the session being armed, which
                # no amount of reading the message can recover.
                if (gate or watch)
                else None
            ),
            # ``watch`` is folded in here as well as above, because the tick path
            # reads the STORED flag and refuses to poll a loop whose ``gate`` is
            # False even when it carries a monitor. Leaving the two to disagree is
            # what makes a watch that looks armed and never observes anything.
            gate=bool(gate or watch),
            banner=banner,
            self_armed=self_armed,
            default_patrol=bool(default_patrol),
        )
        # The budget counts from here on the monotonic clock, not from the first
        # read a whole interval later, when the wall clock may have stepped.
        restart_runtime_clock(loop)
        self._loops[loop.id] = loop
        # Persist WITHOUT blocking the event loop (no-blocking-call rule:
        # _write_state fsyncs, and a wedged disk must not freeze the
        # gateway). Snapshot under the lock (mutation safety), write on a
        # worker thread, and await it so a persistence failure still
        # propagates to the caller before the loop is reported armed.
        payload = self._serialize_state()
        try:
            await asyncio.shield(self._start_persistence(payload, admission=admission))
        except BaseException:
            self._loops.pop(loop.id, None)
            if existing is not None:
                self._loops[existing.id] = existing
                if restore_existing_provider_credentials:
                    await self._restore_provider_credentials(existing)
                if detached_timer is not None and not detached_timer.done():
                    # Still delivering: give it back rather than arm a second timer --
                    # but only while admission is open. Once ``shutdown()`` has closed
                    # it, the timer table is emptied and this tick is either a running
                    # callback shutdown cancelled or one it drains through its own
                    # bookkeeping section, so putting it back would register it behind
                    # the teardown: the repopulation ``_arm_timer`` refuses after closure.
                    if self._accepting_mutations:
                        self._timers[existing.id] = detached_timer
                elif existing.active:
                    self._arm_from_deadline(existing)
            raise
        # The stale stop file goes here: after the row is durable, before its timer
        # exists, and inside this shielded transaction, so a cancelled caller cannot
        # skip it and the first tick cannot read it. A refused or failed add never
        # gets this far, so the loop it would have displaced keeps its stop file.
        finished = retire_stale_stop_sentinel is not None and not (
            await _retire_stale_stop_sentinel(self, loop, retire_stale_stop_sentinel, admission)
        )
        if not finished:
            self._arm_from_deadline(loop)
        if existing is not None:
            # Committed: the displaced row is gone from the store, so its
            # self-arm entry is revoked now, not before the write.
            self._revoke_self_arm_for(existing)
            self._emit("removed", existing)
    self._emit("added", loop)
    if finished:
        # The same pair of events a first-tick stop-file finish produces.
        self._emit("updated", loop)
    logger.info("AutoNudge: added loop %s on slot %s (idle=%ds)", loop.id, slot_key, idle_secs)
    return loop


async def update(
    self: AutoNudgeService,
    loop_id: str,
    *,
    message: str | None = None,
    idle_secs: int | None = None,
    max_cycles: int | None = None,
    active: bool | None = None,
    fresh_run: bool = False,
    max_runtime_secs: int | None = None,
    stopped_reason: str | None = None,
    banner: str | None = None,
    judge: dict | None = None,
    watch: str | None = None,
    expected_generation: int | None = None,
    expect_fingerprint: str | None = None,
    precondition: Callable[[NudgeLoop], bool] | None = None,
    on_absent: Callable[[], None] | None = None,
) -> NudgeLoop | None:
    """Patch a loop. ``precondition`` is re-taken on the live row under the lock.

    A refused precondition changes nothing and returns ``None``, the same
    "not applied" answer as a missing row; only the stale-wake stop passes one.
    ``on_absent`` is called inside the same hold when the row is missing, so
    that caller can tell a deleted row from a replaced one. ``fresh_run`` marks
    a revival as the user's own resume, which resets ONLY the counter behind a
    spent bound: a spent cycle cap (the loop stopped on ``cycle_cap``, or its
    count has reached a non-zero cap) zeroes ``cycle_count``; a spent time
    budget (it stopped on ``runtime_budget``, or the clock has run a non-zero
    budget out by the press) re-anchors ``created_ts``. The other counter is
    kept, so a loop paused by hand at 7 of 24 and resumed the next day comes
    back at 7 of 24 on a fresh clock. A revival without the flag keeps both in
    every case (the reconciler re-arms and a ``monitor_update`` bound raise,
    where a fresh allowance is not what was asked).
    """
    admission = self._admit_mutation()
    # CANCELLATION SAFETY: same contract as add(). The mutate+persist runs
    # as a SHIELDED, supervised task so a caller cancelled mid-write cannot
    # release ``_lock`` while the executor write is still in flight — which
    # would let a later write land first and then be clobbered by this
    # operation's stale snapshot (lost update after restart).
    inner: "asyncio.Task[NudgeLoop | None]" = asyncio.ensure_future(
        self._update_locked(
            loop_id,
            message=message,
            idle_secs=idle_secs,
            max_cycles=max_cycles,
            active=active,
            fresh_run=fresh_run,
            max_runtime_secs=max_runtime_secs,
            stopped_reason=stopped_reason,
            banner=banner,
            judge=judge,
            watch=watch,
            expected_generation=expected_generation,
            expect_fingerprint=expect_fingerprint,
            precondition=precondition,
            on_absent=on_absent,
            admission=admission,
        )
    )
    self._inflight_adds.add(inner)

    def _finish(t: "asyncio.Task[NudgeLoop | None]") -> None:
        self._inflight_adds.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        # A stale baseline is the 409 this update's caller already surfaces, so it is
        # an answer rather than a fault; every other exception keeps its warning.
        if exc is None or isinstance(exc, AutoNudgeStaleBaseline):
            return
        logger.warning("AutoNudge: detached update() failed", exc_info=exc)

    inner.add_done_callback(_finish)
    return await asyncio.shield(inner)


async def _update_locked(
    self: AutoNudgeService,
    loop_id: str,
    *,
    message: str | None = None,
    idle_secs: int | None = None,
    max_cycles: int | None = None,
    active: bool | None = None,
    fresh_run: bool = False,
    max_runtime_secs: int | None = None,
    stopped_reason: str | None = None,
    banner: str | None = None,
    judge: dict | None = None,
    watch: str | None = None,
    expected_generation: int | None = None,
    expect_fingerprint: str | None = None,
    precondition: Callable[[NudgeLoop], bool] | None = None,
    on_absent: Callable[[], None] | None = None,
    admission: _MutationAdmission | None = None,
) -> NudgeLoop | None:
    lock = await self._acquire_mutation_lock(loop_id, admission=admission)
    if lock is None:
        return None
    try:
        return await self._update_unserialized(
            loop_id,
            message=message,
            idle_secs=idle_secs,
            max_cycles=max_cycles,
            active=active,
            fresh_run=fresh_run,
            max_runtime_secs=max_runtime_secs,
            stopped_reason=stopped_reason,
            banner=banner,
            judge=judge,
            watch=watch,
            expected_generation=expected_generation,
            expect_fingerprint=expect_fingerprint,
            precondition=precondition,
            on_absent=on_absent,
            admission=admission,
        )
    finally:
        _release_mutation_lock(lock)


async def _update_unserialized(
    self: AutoNudgeService,
    loop_id: str,
    *,
    message: str | None = None,
    idle_secs: int | None = None,
    max_cycles: int | None = None,
    active: bool | None = None,
    fresh_run: bool = False,
    max_runtime_secs: int | None = None,
    stopped_reason: str | None = None,
    banner: str | None = None,
    judge: dict | None = None,
    watch: str | None = None,
    expected_generation: int | None = None,
    expect_fingerprint: str | None = None,
    precondition: Callable[[NudgeLoop], bool] | None = None,
    on_absent: Callable[[], None] | None = None,
    admission: _MutationAdmission | None = None,
) -> NudgeLoop | None:
    from kiro_crew import autonudge as seams  # read at call time: the facade imports us

    if max_runtime_secs is not None:
        validate_runtime_secs(max_runtime_secs, allow_unbounded=True)
    # The session log a retarget's bare ``PR <number>`` resolves against, read in a
    # worker thread BEFORE the lock, keyed by the message and session it was read for.
    # Inside the hold it is used only when the loop still carries exactly that pair;
    # otherwise the bare number resolves nothing, the same as an unreadable log.
    peeked = self._loops.get(loop_id)
    retarget_key: tuple[str, str] | None = None
    retarget_texts: list[str] | None = None
    if peeked is not None:
        peeked_message = message if message is not None else peeked.message
        peeked_watch = str(watch or "").strip() or (
            peeked.monitor.kind if peeked.monitor is not None else ""
        )
        if needs_session_texts(peeked_message, peeked.slot_key, peeked_watch):
            retarget_key = (peeked_message, peeked.slot_key)
            retarget_texts = await asyncio.to_thread(
                read_session_texts, peeked_message, peeked.slot_key, peeked_watch
            )
    async with self._lock:
        loop = self._loops.get(loop_id)
        if not loop:
            # Inside the hold, so the caller can inspect the slot before any
            # concurrent arm or delete can change it.
            if on_absent is not None:
                on_absent()
            return None
        # Same contract as ``_remove_unserialized``: the caller's decision is
        # re-taken on the live row inside the ``_lock`` hold that mutates it,
        # so a pause that landed while this call waited cannot be overwritten.
        if precondition is not None and not precondition(loop):
            return None
        # ATOMIC generation fence (inside _lock, before any mutation): a
        # caller applying a structural-terminal stop passes the generation it
        # captured at fire time. If the loop's config generation has moved
        # since (a changed instruction, or a revival), the completion is
        # STALE -- it belongs to an older configuration -- so refuse the stop
        # without touching the loop. Checked here, not by an external
        # read-then-update, so there is no TOCTOU window between the compare
        # and the write.
        if expected_generation is not None and loop.config_generation != expected_generation:
            logger.info(
                "AutoNudge: loop %s structural stop refused — captured gen %s "
                "!= current gen %s (config changed under the fired turn)",
                loop.id,
                expected_generation,
                loop.config_generation,
            )
            return loop
        if is_structured_monitor_loop(loop):
            # Generic update owns only legacy prompt loops. Reject before
            # touching even one shared scheduling field so a non-HTTP
            # caller cannot bypass structured policy.
            return loop
        # Under the lock, so no write can land between this and the mutation. The
        # fingerprint is authoritative: a projection baseline cannot distinguish goals.
        if expect_fingerprint is not None and (
            not expect_fingerprint or loop.goal_token != expect_fingerprint
        ):
            raise AutoNudgeStaleBaseline(loop_id)
        # Keep typed nested values intact. ``asdict`` recursively converts
        # MonitorState to a plain dict, which is not a valid rollback value.
        previous = {item.name: getattr(loop, item.name) for item in fields(loop)}
        # The short-reading hold lives ON the monitor, which ``previous`` holds by
        # reference, so a re-aim's clear of it is kept by value for the rollback.
        previous_hold = (
            (loop.monitor, loop.monitor.short_reading_digest, loop.monitor.short_reading_held)
            if loop.monitor is not None
            else None
        )
        # The budget's anchor is keyed to ``created_ts`` and kept outside the fields
        # ``previous`` holds, so the Resume that re-anchors it below needs its own
        # entry in the rollback.
        previous_anchor = snapshot_runtime_anchor(loop)
        # Set only if a retarget takes this loop's pending wake claim, so the
        # rollback below restores exactly what it removed and nothing else.
        claim_discarded_for_retarget = False
        floor_discarded_for_retarget = False
        #: Whether this update changed something that can change the WATCHED
        #: SUBJECT -- the instruction, or the brief whose target list names it.
        #: Resolved once below, after both have landed.
        rebind_monitor = False
        #: Whether this update has already spent a config generation. One update
        #: changes one configuration, so the counter advances at most once even
        #: when both the instruction and the watched subject move together.
        generation_advanced = False
        was_active = loop.active
        restored_claim: int | None = None
        claim_undelivered = False
        if message is not None:
            retarget = message != loop.message
            loop.message = message
            # A new goal is a new identity, so a baseline served for the old text
            # cannot authorise a write.
            loop.goal_token = new_goal_token()
            if retarget:
                # A changed instruction is a new config generation, so a
                # structural-terminal verdict recorded for the OLD
                # instruction does not apply to it. Advanced here (not on a
                # no-op same-message save) so an unrelated settings save does
                # not spend a generation.
                loop.config_generation += 1
                generation_advanced = True
                # The labelled history goes with the generation: every label was earned
                # answering the question this instruction replaced, even when the
                # subject is the same. Kept, it would show the judge a hit rate for
                # another question, and it supersedes the last verdict on the next
                # tick. The rollback restores it from the snapshot above.
                loop.judge_quiet_streak = 0
                loop.judge_cursors = {}
                loop.judge_pr_seen = {}
                loop.judge_last_verdict = {}
                loop.judge_recent_verdicts = []
                _clear_short_reading_hold(loop)
                # The instruction IS the target, so re-infer the subject, or the loop
                # keeps polling the old pull request and retires when it merges.
                # An unchanged subject keeps its monitor (and its metering counters);
                # a message naming no subject clears it back to a plain timer. A
                # gate=False loop is never re-inferred: its caller chose the cadence.
                # The rebinding runs AFTER the judge block below, because the brief's
                # targets also name the subject and may change in the same call.
                rebind_monitor = True
        if banner is not None:
            # Display-only, so no deadline or timer consequence — unlike
            # ``idle_secs`` below, quieting a running loop must not restart
            # its countdown. "" clears it back to the verbose default.
            loop.banner = banner
        if judge is not None:
            # ``{}`` clears the owner's CRITERIA (a gated loop then runs under the
            # default brief); ``judge: false`` stores the opt-out marker; any other
            # object replaces the criteria. The streak, read cursors and pull-request
            # baseline are facts about the OLD brief, so they reset: a kept baseline
            # would screen the new criteria quiet without ever asking the judge.
            loop.judge = seams.scrubbed_judge_spec(judge)
            loop.judge_quiet_streak = 0
            loop.judge_cursors = {}
            loop.judge_pr_seen = {}
            loop.judge_last_verdict = {}
            # The labelled history goes too: every label in it was earned against
            # the criteria being replaced, so carrying it forward would show the
            # judge a hit rate for a question nobody is asking any more.
            loop.judge_recent_verdicts = []
            _clear_short_reading_hold(loop)
            # A REPLACED brief can name a different pull request, and the collector
            # will ask about the new list from the next tick on. A monitor left on
            # the old subject would publish a reading the collector drops, so the
            # watch would go on spending a fetch and the judge would see nothing.
            # Only for a gated loop: a structured monitor is refused far above, and
            # an ungated loop has no judge to read.
            rebind_monitor = rebind_monitor or loop.gate
        requested_watch = str(watch or "").strip()
        if requested_watch:
            # ARM a subject on a live loop: a work-ledger watch observes the session's
            # OWN key, which no message names, so only this field reaches it without
            # tearing the loop down and losing its cycle count. ``gate`` is set with it
            # because the tick path will not poll a gate=False loop, monitor or not.
            loop.gate = True
            rebind_monitor = True
        # ONE place resolves the subject, from the two strings this loop now holds.
        # Reached by a changed instruction and by a replaced brief alike, because
        # either can name a different pull request and only the pair says which.
        if rebind_monitor:
            # The loop's OWN kind feeds every derivation below: a work-ledger subject
            # is this session, which no message names, so re-inferring from text alone
            # would CLEAR the monitor on a reword. A watch REQUESTED by this update wins,
            # since a loop with no monitor stores kind "" and the field would do nothing.
            watch_kind = requested_watch or (loop.monitor.kind if loop.monitor is not None else "")
            session_texts = (
                retarget_texts if retarget_key == (loop.message, loop.slot_key) else None
            )
            inferred = (
                infer_monitor(
                    loop.message,
                    time.time(),
                    judge=loop.judge,
                    watch=watch_kind,
                    slot_key=loop.slot_key,
                    session_texts=session_texts,
                )
                if loop.gate
                else None
            )
            current = loop.monitor
            # The stored spelling is a canonical shorthand and cannot
            # express a HOST, so kind and target alone would call an edit
            # from an enterprise shorthand to the same public slug
            # "unchanged" and keep polling the wrong server. This is the
            # third of the three places that comparison had to reach; the
            # other two are the post-poll binding and the dedupe identity.
            # The OLD subject is the one the current monitor was bound to, so a bare
            # number in the previous message resolves against that, never the log.
            from kiro_crew.autonudge_judge import watched_pr_subject

            old_probe = infer_subject(
                str(previous.get("message") or ""),
                previous.get("judge"),
                watch=watch_kind,
                slot_key=loop.slot_key,
                monitor_target=watched_pr_subject(loop),
            )
            new_probe = infer_subject(
                loop.message,
                loop.judge,
                watch=watch_kind,
                slot_key=loop.slot_key,
                session_texts=session_texts,
            )
            same_host = (old_probe.host_key if old_probe else None) == (
                new_probe.host_key if new_probe else None
            )
            same_subject = (
                inferred is not None
                and current is not None
                and current.kind == inferred.kind
                and current.target == inferred.target
                and same_host
            )
            if not same_subject:
                if current is not None and current.version != MONITOR_STATE_VERSION:
                    # A FUTURE version cannot be interpreted here, so it must
                    # not be REPLACED here either. The revival guard below
                    # already refuses to touch such a record, on the grounds
                    # that the stored intent belongs to the newer gateway that
                    # wrote it -- but that guard runs after this assignment,
                    # so a downgraded gateway destroyed the payload before the
                    # rule protecting it ever applied. Same rule, second
                    # surface: leave the record alone and let the message
                    # change without rebinding the watch.
                    logger.info(
                        "AutoNudge: loop %s carries a monitor from version %d, so "
                        "its retarget is refused rather than overwriting state "
                        "this gateway cannot read",
                        loop.id,
                        current.version,
                    )
                else:
                    # A wake claimed for the OLD subject must not be spent on the new
                    # one: the claim is keyed by loop id, so the in-flight delivery
                    # would charge a monitor that observed nothing. The claim (with
                    # its briefs) is TAKEN so the rollback below can restore it exactly.
                    claim_discarded_for_retarget = loop.id in self._pending_monitor_wake
                    discarded_briefs_for_retarget = self._pending_monitor_wake.pop(loop.id, "")
                    # The floor claim goes too, for the same reason: every release
                    # site must clear both claim sets.
                    floor_discarded_for_retarget = loop.id in self._pending_floor_tick
                    self._pending_floor_tick.discard(loop.id)
                    loop.monitor = inferred
                    if not generation_advanced:
                        # A BRIEF-only retarget changes the subject but not the
                        # instruction, so advance the generation here: otherwise a
                        # structural-terminal verdict for the OLD subject, which sits
                        # on the slot until the next real turn, deactivates this watch.
                        loop.config_generation += 1
                        generation_advanced = True
        interval_changed = False
        if idle_secs is not None:
            new_idle = max(_MIN_IDLE_SECS, min(_MAX_IDLE_SECS, int(idle_secs)))
            interval_changed = new_idle != loop.idle_secs
            loop.idle_secs = new_idle
        if max_cycles is not None:
            loop.max_cycles = max(0, int(max_cycles))
        if max_runtime_secs is not None:
            loop.max_runtime_secs = max(0, int(max_runtime_secs))
        #: Set when ``active=True`` reached a FINISHED row, or a row with an
        #: unreadable stored bound, and was declined.
        refused_revival = False
        if active is not None:
            if (
                active
                and loop.monitor is not None
                and loop.monitor.version != MONITOR_STATE_VERSION
            ):
                # A FUTURE version cannot be interpreted here. IGNORE the
                # flag -- deliberately without touching ``loop.active`` --
                # because the stored intent belongs to the newer gateway that
                # wrote it and will resume it. Forcing it off would let an
                # older process silently retire a watch it cannot even read.
                pass
            elif active and loop.monitor is not None and loop.monitor.outcome is not None:
                # A monitor with an outcome is finished (merged, or budget spent):
                # reviving it would fire ungated prompts, because the tick gate does
                # not observe a settled monitor. An unsettled monitor falls through to
                # the ordinary activation below, which stays gated per tick. Keep the
                # two branches apart: one ``loop.monitor is not None`` branch would
                # neither refuse nor activate.
                loop.active = False
                loop.next_due_ts = 0.0
            elif (
                active
                and not loop.active
                and reason_in(loop.stopped_reason, FINISHED_LOOP_REASONS)
                and not _stop_file_lifted(loop)
            ):
                # A FINISHED loop (its stop file exists, or its subject merged or
                # closed, including a row whose monitor a later retarget cleared) has
                # nothing to resume, so the revival is declined whoever asks. Clear
                # it, or arm a NEW loop that displaces it. Exception: a stop file that
                # is GONE. Issue Radar and Research Lab use it as an operator kill
                # switch, and deleting it means "run again"; the goal popover never
                # deletes it (the next arm after Clear does).
                logger.info(
                    "AutoNudge: loop %s is finished (%s) — not reviving it",
                    loop.id,
                    loop.stopped_reason,
                )
                refused_revival = True
            elif active and not loop.active and unreadable_bound_field(loop) is not None:
                # A stored bound with no lossless integer reading (see ``_load``)
                # is a limit the user typed that nothing can read, so running the
                # loop would run it without that limit. Declined whoever asks,
                # like a finished row, until a fresh bound replaces the value; a
                # bound passed with this same call already has, above.
                logger.info(
                    "AutoNudge: loop %s has an unreadable %s — not reviving it",
                    loop.id,
                    unreadable_bound_field(loop),
                )
                refused_revival = True
            # TERMINAL-TRANSITION ATOMICITY: a bound-tagged deactivation (the timer's
            # cycle_cap / runtime_budget, with stopped_reason) must never OVERWRITE one
            # that landed first, or a user pause just before it becomes budget-revivable.
            # Both serialize on _lock, so the bound is a no-op on an inactive loop. The
            # reverse order (a reasonless pause after the bound) is closed two branches down.
            elif (
                not active
                and stopped_reason is None
                and loop.stopped_reason == AUTONUDGE_STOP_REASON
            ):
                # A reasonless repeat of an already-inactive state is not a
                # new stop transition. Preserve source-owned completion
                # evidence until its Research Lab watchdog consumes it;
                # dashboard retries and unrelated patches must not turn a
                # deliberate stop into a revivable manual pause.
                logger.info(
                    "AutoNudge: loop %s retains its source stop reason on "
                    "reasonless inactive update",
                    loop.id,
                )
            elif (
                not active
                and stopped_reason is None
                and not loop.active
                and reason_in(loop.stopped_reason, _KEPT_STOP_REASONS)
            ):
                # The MIRROR of the race above: the bound landed first and a
                # reasonless pause (the goal popover's, pressed off a stale
                # running reading) arrives second. A repeat of an inactive state
                # is not a new stop, and "manual" over the bound would lose why
                # the loop ended. A FINISHED stop is kept for one more reason:
                # its reason is what refuses the revival, so "manual" over it
                # would hand Play back on a loop with nothing to resume.
                logger.info(
                    "AutoNudge: loop %s keeps its %s stop on reasonless inactive update",
                    loop.id,
                    loop.stopped_reason,
                )
            elif reason_in(stopped_reason, _KEPT_STOP_REASONS) and not active and not loop.active:
                # A bound or the stop file landing on a loop already inactive --
                # a pause that beat the timer's own stop to the lock -- is not a
                # new transition either, and must not make that pause look like
                # a finished or capped-out loop.
                logger.info(
                    "AutoNudge: loop %s already deactivated (%s) — %s stop not overwriting it",
                    loop.id,
                    loop.stopped_reason or MANUAL_STOP_REASON,
                    stopped_reason,
                )
            else:
                loop.active = bool(active)
                # Record WHY on every deactivation and clear it on every
                # revival, so the store always reflects the LAST transition.
                # ``stopped_reason`` is an internal caller parameter (_timer's
                # terminal bounds pass "cycle_cap"/"runtime_budget"); external
                # deactivations (REST pause, deactivate-mid-fire) default to
                # "manual", which the revive logic never auto-resumes.
                if loop.active:
                    loop.stopped_reason = ""
                    # Spent only by an actual REVIVAL, hence ``not
                    # was_active``. A still-active loop may still receive
                    # an idempotent ``active=True`` from API or app callers,
                    # and treating that as an answer would erase evidence
                    # recorded moments earlier and let one more doomed cycle
                    # fire. Keeping it costs at most a resumable stop the
                    # operator can undo; dropping it costs a wasted cycle and
                    # the silence this stop exists to end.
                    if not was_active:
                        restored_claim = self._store.unreconciled_claim.pop(loop.id, None)
                        claim_undelivered = loop.id in self._store.undelivered_claim
                        if restored_claim is not None:
                            # A turn that may have gone out is charged; one known unsent is not.
                            self._store.undelivered_claim.discard(loop.id)
                            if not claim_undelivered:
                                loop.cycle_count = max(loop.cycle_count, restored_claim)
                        loop.approval_stalled = False
                        loop.waiting_on_person = False
                        loop.waiting_on_person_at = 0.0
                        loop.ledger_seen_fp = ""
                        # Same rule, same reason: the streaks are evidence
                        # about a PAST run, and a revival starts a fresh one.
                        loop.consecutive_start_failures = 0
                        loop.consecutive_failed_cycles = 0
                        # The user's resume resets only the counter behind a SPENT
                        # bound: a spent cap zeroes ``cycle_count``, a spent time
                        # budget re-anchors ``created_ts`` (the budget clock), or
                        # the timer would re-stop the loop on its first tick. Spent
                        # means the snapshot's stop reason names that bound, OR the
                        # bound after this update would refuse the first tick (the
                        # clock runs through a pause). The other counter is kept:
                        # the cap is a lifetime limit. Bound reads are type-safe so
                        # a malformed row cannot raise after ``active`` flipped.
                        # Opt-in (``fresh_run``) because app reconcilers re-arm
                        # inactive loops every few seconds and would turn the cap
                        # into a per-run allowance.
                        stopped_with = previous["stopped_reason"]
                        if fresh_run and (stopped_with == CYCLE_CAP_REASON or cap_reached(loop)):
                            loop.cycle_count = 0
                        if fresh_run and (
                            stopped_with == RUNTIME_BUDGET_REASON or budget_elapsed(loop)
                        ):
                            loop.created_ts = time.time()
                            restart_runtime_clock(loop)
                else:
                    loop.stopped_reason = stopped_reason or MANUAL_STOP_REASON
        if (
            refused_revival
            and not any(
                value is not None
                for value in (message, idle_secs, max_cycles, max_runtime_secs, banner, judge)
            )
            and not requested_watch
        ):
            # Only the revival was asked, and it was declined, so nothing changed:
            # no store write, no ``updated`` frame. The Issue Radar and Research Lab
            # reconcilers re-arm every inactive loop of a live crew or campaign on
            # each pass (every 60 s and 5 s), so a finished row they cannot revive
            # would otherwise cost a full store rewrite and a broadcast per pass
            # for the life of the campaign.
            return loop
        revived = loop.active and not was_active
        if revived:
            # A revival re-arms the loop for a fresh run: a structural verdict
            # recorded before it was stopped must not carry over (the user or
            # a directive chose to run it again). Advancing the generation
            # invalidates any in-flight stale completion keyed to the old one.
            loop.config_generation += 1
        # Deadline bookkeeping (BEFORE the snapshot below so it persists): an
        # interval change restarts an EXISTING countdown at the new interval; any
        # other patch keeps the deadline; deactivation clears it. A deadline that
        # is ALREADY cleared (its fire's turn is still running) stays cleared:
        # notify_turn_complete anchors the next countdown at the turn's END.
        if not loop.active:
            loop.next_due_ts = 0.0
        elif interval_changed and loop.next_due_ts > 0:
            loop.next_due_ts = time.time() + loop.idle_secs
        # Persist WITHOUT blocking the event loop — _write_state fsyncs, and
        # a wedged disk must not freeze chat/heartbeat/liveness. Snapshot
        # under THIS lock hold (mutation safety + serialization vs the
        # post-fire write) and await the offloaded write so a persistence
        # failure still reaches the caller. Same contract as _add_locked.
        payload = self._serialize_state()
        claim_was_held = claim_discarded_for_retarget
        try:
            await asyncio.shield(self._start_persistence(payload, admission=admission))
        except BaseException:
            for field_name, value in previous.items():
                setattr(loop, field_name, value)
            restore_runtime_anchor(loop, previous_anchor)
            if restored_claim is not None:
                self._store.unreconciled_claim[loop.id] = restored_claim
                if claim_undelivered:
                    self._store.undelivered_claim.add(loop.id)
            if previous_hold is not None:
                held_on, held_digest, held_count = previous_hold
                held_on.short_reading_digest = held_digest
                held_on.short_reading_held = held_count
            if claim_was_held:
                # The write failed, so the retarget did not happen: hand back the
                # wake claim it dropped, or the delivered wake loses its accounting
                # and follow-up turn. The claim and its briefs are one value, so
                # restore both, or the retried delivery would name no item.
                self._pending_monitor_wake[loop.id] = discarded_briefs_for_retarget
            if floor_discarded_for_retarget:
                # Same restore for the floor claim.
                self._pending_floor_tick.add(loop.id)
            raise
        # Re-arm the timer with the new settings — but NEVER while its
        # callback is mid-fire. Cancelling a firing timer cancels the
        # in-flight turn itself (channel loops run the turn inline in
        # _on_fire), destroying the response and the cycle accounting. A
        # firing timer re-arms itself on every exit path anyway (backoff
        # re-arm when undelivered, self-re-arm for channel keys,
        # notify_turn_complete for dashboard slots), and each of those reads
        # the freshly-updated idle_secs/active, so the new settings still
        # take effect on the next cycle.
        if loop.id in self._firing:
            logger.info(
                "AutoNudge: loop %s updated mid-fire — deferring re-arm to the "
                "running timer so the in-flight turn is not cancelled",
                loop.id,
            )
        else:
            self._cancel_timer(loop_id)
            # Arm only when a schedule exists (deadline set) or this update
            # REVIVED the loop (fresh full countdown for a paused loop —
            # nothing else will arm it). An active loop with a cleared
            # deadline is a delivered fire whose turn is still running;
            # notify_turn_complete owns its next arm (see the deadline
            # bookkeeping above), so arming here would anchor the interval
            # mid-turn.
            if loop.active and (loop.next_due_ts > 0 or revived):
                self._arm_from_deadline(loop)
    self._emit("updated", loop)
    return loop


def remove_sync(
    self: AutoNudgeService, loop_id: str, *, persist: bool = True, emit: bool = True
) -> NudgeLoop | None:
    """Remove a loop. ``persist=False`` skips the blocking save — used by
    async callers that snapshot+offload the write themselves right after."""
    if persist and loop_id in self._loops:
        from kiro_crew import autonudge_provider_trust

        autonudge_provider_trust.forget_monitor_owner_credentials(loop_id)
    loop = self._loops.pop(loop_id, None)
    if loop is None:
        return None
    if not self._monitor_mutation_lock_users.get(loop_id, 0):
        self._monitor_mutation_locks.pop(loop_id, None)
    self._cancel_timer(loop_id)
    self._rearm_fail_count.pop(loop_id, None)
    self._terminal_reprobe_unknowns.pop(loop_id, None)
    self._start_failure_deferred.pop(loop_id, None)
    self._rearm_pending.discard(loop_id)
    # Beside the line above, and for its reason: a claim released in one set and
    # forgotten in another outlives its loop, and an id reused by a later loop would
    # inherit a pull-forward nobody asked for.
    self._pulled_forward.discard(loop_id)
    self._pushed_ticks.discard(loop_id)
    self._pushed_running.discard(loop_id)
    self._ledger_wake_streak.pop(loop_id, None)
    self._ledger_tick_fp.pop(loop_id, None)
    self._pull_forward_counts.pop(loop_id, None)
    self._pull_forward_capped = {pair for pair in self._pull_forward_capped if pair[0] != loop_id}
    self._accepted_monitor_turns.pop(loop_id, None)
    if persist:
        self._save()
        # Revoke the keystone-gated self-arm entry only AFTER the store
        # committed the removal: a save that raises leaves the loop's
        # durable row in place, and a loop that is still stored must keep
        # the entry it needs to fire. Provider credential denial is the
        # inverse: it must be durable before the agent-writable row can
        # disappear. ``persist=False`` callers own both commit boundaries.
        self._revoke_self_arm_for(loop)
    if emit:
        self._emit("removed", loop)
    deferred_replacement = self._deferred_monitor_replacements.pop(loop_id, None)
    if deferred_replacement is not None and deferred_replacement[0] is not None:
        deferred_prior = deferred_replacement[0]
        assert deferred_prior is not None
        self._revoke_self_arm_for(deferred_prior)
    return loop


def _revoke_self_arm_for(self: AutoNudgeService, loop: NudgeLoop) -> None:
    """Revoke for EVERY loop leaving the store, not only ``self_armed`` ones.

    The ``self_armed`` bit lives in the agent-writable store, so a forged
    ``false`` (plus a restart) would skip the revoke and leave the keystone
    entry for a later loop reusing the id on the same slot. Revocation keys on
    the fact the store cannot forge: the loop is being removed. A loop with no
    entry costs one offloaded read (``forget_self_arm`` writes only on delete).

    The judge's per-loop remark stash goes here too: every caller reaches this
    line only AFTER the store committed the removal, while a rollback never
    does, so forgetting at ``_loops.pop`` would strip a restored loop of a stash
    it still owes. Only loop-id-keyed judge state goes; the forgotten state and
    take counter serve a notice whose owner is NOT known and stay.
    """
    from kiro_crew import autonudge_judge as judge

    self._revoke_self_arm(loop.id)
    judge.forget_pr_bodies(loop.id)


def _revoke_self_arm(loop_id: str) -> None:
    """Finish best-effort trust cleanup after a loop leaves the store.

    Every removal path (explicit remove, session close, replacement by a
    new arm) funnels through ``remove_sync``, so this is the one place the
    self-arm trust record is told a loop has left the store -- and the ONLY
    path that drops an entry, since ``record_self_arm`` is a pure upsert.
    Provider credential denial has already been made durable before removal;
    repeating it here finishes cleanup after a partial tombstone transaction.
    File IO is offloaded when an event loop is running (``remove_sync`` is
    reached from async paths that must not block on fsync); the sync
    fallback covers shutdown/test callers with no loop. Best-effort: a
    revocation failure is logged inside ``forget_self_arm`` and never
    breaks the removal.
    """
    from kiro_crew import autonudge_provider_trust, autonudge_selfarm

    def _forget_all_trust() -> None:
        autonudge_selfarm.forget_self_arm(loop_id)
        autonudge_provider_trust.forget_monitor_owner_credentials(loop_id)

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        _forget_all_trust()
        return
    fut = running.run_in_executor(None, _forget_all_trust)

    def _log(f: "asyncio.Future[None]") -> None:
        if not f.cancelled() and f.exception() is not None:
            logger.warning("self-arm revocation failed for %s", loop_id, exc_info=f.exception())

    fut.add_done_callback(_log)


async def remove(
    self: AutoNudgeService,
    loop_id: str,
    *,
    precondition: Callable[[NudgeLoop], bool] | None = None,
    on_absent: Callable[[], None] | None = None,
    stop_reason: str = "",
    stop_detail: str = "",
) -> bool:
    """Remove a loop if its live row satisfies ``precondition`` under the lock.

    The admitted transaction runs in a supervised, shielded child task, so
    cancellation of the handler awaiting it cannot abort a lock wait or a write.
    A handler cancelled while the transaction still waits for a lock stops waiting
    at once; one cancelled once the write is in flight is held until the write
    settles and then receives the cancellation. Shutdown drains that child to its
    durable boundary without waiting on the caller's remaining lifetime. A timer
    callback that owns the call is cancelled only after the child settles when
    admission has closed.
    """
    admission = self._admit_mutation()
    return await _run_admitted_transaction(
        self,
        _remove_admitted(
            self,
            loop_id,
            precondition=precondition,
            on_absent=on_absent,
            stop_reason=stop_reason,
            stop_detail=stop_detail,
            admission=admission,
        ),
        cancel_callback_on_exit=True,
        on_failure_log="AutoNudge: detached remove() failed",
    )


async def _remove_admitted(
    svc: AutoNudgeService,
    loop_id: str,
    *,
    precondition: Callable[[NudgeLoop], bool] | None,
    on_absent: Callable[[], None] | None,
    stop_reason: str,
    stop_detail: str,
    admission: _MutationAdmission,
) -> bool:
    """The legacy removal transaction ``remove`` owns once admitted."""
    # Give an active maintenance quiesce its existing first refusal point, but
    # never wait on this monitor's continuation while holding the data-home lock.
    preflight_lock = await svc._acquire_mutation_lock(loop_id, admission=admission)
    if preflight_lock is None:
        return False
    _release_mutation_lock(preflight_lock)
    async with _monitor_mutation_guard(svc, loop_id):
        lock = await svc._acquire_mutation_lock(loop_id, admission=admission)
        if lock is None:
            return False
        try:
            return await svc._remove_unserialized(
                loop_id,
                precondition=precondition,
                on_absent=on_absent,
                stop_reason=stop_reason,
                stop_detail=stop_detail,
                mutation_lock=lock,
                admission=admission,
            )
        finally:
            _release_mutation_lock(lock)


async def remove_by_slot(self: AutoNudgeService, slot_key: str) -> NudgeLoop | None:
    """Retire the current slot generation inside one maintenance transaction."""
    lock = _maintenance_lock(self._base_dir)
    async with lock:
        _claim_mutation_lock(lock)
        try:
            loop = self._find_by_slot(slot_key)
            if loop is None:
                return None
            if is_structured_monitor_loop(loop):
                await self.retire_monitor_for_session_close(loop.id)
            else:
                await self._remove_unserialized(
                    loop.id,
                    stop_reason="session_closed",
                    mutation_lock=lock,
                )
            return loop
        finally:
            _unclaim_mutation_lock(lock)


async def clear_terminal_monitor(self: AutoNudgeService, monitor_id: str) -> bool:
    """Remove a structured monitor row ONLY while it is still terminal.

    The owner-facing clear (``authorize_and_clear_monitor``) checks the
    record's state, then audits — and the audit hands off to a thread, which
    yields the event loop. In that window a concurrent
    ``restore_monitor_after_failed_session_close`` can put the SAME row back
    into service, so an unconditional removal afterwards would delete a live
    watch with no record it existed: exactly the harm the live-monitor
    refusal exists to prevent, reached from the other side.

    So the decision is re-taken here, under the one lock hold that also
    performs the removal. Returns ``False`` when the row moved on (restored,
    already gone, or a wake accepted since), and removes nothing.
    """

    def _still_terminal(loop: NudgeLoop) -> bool:
        state = loop.monitor
        if state is None or state.outcome is None:
            return False
        if state.version != MONITOR_STATE_VERSION:
            return False
        return not state.wake_in_flight

    admission = self._admit_mutation()
    return await _run_admitted_transaction(
        self,
        _clear_terminal_monitor_admitted(
            self,
            monitor_id,
            precondition=_still_terminal,
            admission=admission,
        ),
        cancel_callback_on_exit=True,
        on_failure_log="AutoNudge: detached terminal monitor clear failed",
        expected=(NudgeAdmissionRefused,),
    )


async def _clear_terminal_monitor_admitted(
    svc: AutoNudgeService,
    monitor_id: str,
    *,
    precondition: Callable[[NudgeLoop], bool],
    admission: _MutationAdmission,
) -> bool:
    """The terminal-row removal ``clear_terminal_monitor`` owns once admitted."""
    preflight_lock = await svc._acquire_mutation_lock(monitor_id, admission=admission)
    if preflight_lock is None:
        return False
    _release_mutation_lock(preflight_lock)
    async with _monitor_mutation_guard(svc, monitor_id):
        lock = await svc._acquire_mutation_lock(monitor_id, admission=admission)
        if lock is None:
            return False
        try:
            return await svc._remove_unserialized(
                monitor_id,
                precondition=precondition,
                mutation_lock=lock,
                admission=admission,
            )
        finally:
            _release_mutation_lock(lock)


async def _remove_unserialized(
    self: AutoNudgeService,
    loop_id: str,
    *,
    precondition: Callable[[NudgeLoop], bool] | None = None,
    on_absent: Callable[[], None] | None = None,
    stop_reason: str = "",
    stop_detail: str = "",
    mutation_lock: asyncio.Lock | None = None,
    admission: _MutationAdmission | None = None,
) -> bool:
    """Remove one loop. Returns whether the removal happened.

    ``stop_reason``/``stop_detail`` travel with THIS removal's write only: they
    are staged for the stop record just before it and dropped once it settles,
    so a failed removal cannot leave a reason for a later, unrelated stop.

    ``precondition`` is evaluated on the LIVE row inside the same ``_lock``
    hold that removes it, so a caller whose decision was taken before an
    await can re-take it atomically here instead of racing whatever landed
    in between. A refused precondition changes nothing. ``on_absent`` is
    called inside the same hold when the row is missing.
    """
    if mutation_lock is None:
        raise RuntimeError("mutation lock must be held by the caller")
    _assert_mutation_lock_owned(mutation_lock)
    if mutation_lock is not _maintenance_lock(self._base_dir):
        raise RuntimeError("mutation lock must be the service maintenance lock")
    async with self._lock:
        # Before the live row is touched: a caller without a live or inherited
        # admission must not begin a removal after closure.
        if not self._mutation_allowed(admission):
            raise NudgeAdmissionRefused(
                SERVICE_SHUTTING_DOWN_MESSAGE,
                reason=NudgeAdmissionReason.SERVICE_SHUTTING_DOWN,
            )
        existed = loop_id in self._loops
        if not existed and loop_id not in self._store.pending_removals:
            if on_absent is not None:
                on_absent()
            return False
        current = self._loops.get(loop_id)
        if precondition is not None:
            if current is None:
                if on_absent is not None:
                    on_absent()
                return False
            if not precondition(current):
                return False
        restore_provider_credentials = False
        if existed:
            assert current is not None
            was_active = current.active
            current.active = False
            self._cancel_timer(loop_id)
            try:
                restore_provider_credentials = await self._provider_credentials_authorized(current)
                await self._revoke_provider_credentials_before_removal(loop_id)
            except BaseException:
                current.active = was_active
                if current.active:
                    self._arm_from_deadline(current)
                raise
            current.active = was_active
        # Remove in-memory but SKIP the blocking save: _save() -> _write_state
        # fsyncs, and a wedged disk must not freeze the event loop. Snapshot
        # under THIS lock hold (serialization vs the post-fire write). Keep
        # the removal INLINE (not a separate task) so _cancel_timer's
        # "never cancel the current task" self-guard still applies when
        # _timer removes its own loop.
        removed_loop: NudgeLoop | None = None
        if existed:
            removed_loop = self.remove_sync(loop_id, persist=False, emit=False)
            self._store.pending_removals.add(loop_id)
            if stop_reason:
                self._store.stop_notes[loop_id] = (
                    autonudge_stop_log.safe_text(stop_reason),
                    autonudge_stop_log.safe_text(stop_detail, autonudge_stop_log.DETAIL_MAX_CHARS),
                )
        payload = self._serialize_state()
        fut: asyncio.Future[None] | None = None

        async def _restore_failed_removal() -> None:
            self._store.pending_removals.discard(loop_id)
            if removed_loop is None:
                return
            self._loops[loop_id] = removed_loop
            if restore_provider_credentials:
                await self._restore_provider_credentials(removed_loop)
            if removed_loop.active:
                self._arm_from_deadline(removed_loop)

        try:
            # Submission is part of the transaction: admission may close or the
            # executor may reject work here, and either failure must restore the
            # live row just like a write failure does.
            fut = self._start_persistence(payload, admission=admission)
            await asyncio.shield(fut)
        except asyncio.CancelledError:
            assert fut is not None
            # Caller cancelled mid-write: the executor thread can't be
            # cancelled and is still fsyncing. shield re-raised on us
            # immediately, so DRAIN the write to completion before this
            # `async with` exits and releases _lock — otherwise a waiter
            # (add()/update()/_persist_locked) could acquire the lock and
            # race a second os.replace(), clobbering newer state with this
            # stale removal snapshot ("lost update after restart"). Then
            # propagate the cancellation.
            while not fut.done():
                try:
                    await asyncio.shield(fut)
                except asyncio.CancelledError:
                    continue
            try:
                fut.result()
            except Exception:
                await _restore_failed_removal()
                raise
            self._store.pending_removals.discard(loop_id)
            if removed_loop is not None:
                self._revoke_self_arm_for(removed_loop)
                self._emit("removed", removed_loop)
            raise
        except Exception:
            # Persistence is the commit point. Restore the live row (and
            # its timer when it was active) so an immediate retry can still
            # see the same loop the durable store retained.
            await _restore_failed_removal()
            raise
        else:
            self._store.pending_removals.discard(loop_id)
            if removed_loop is not None:
                # The store committed: the row is gone, so its self-arm
                # entry may go too (see remove_sync for why not earlier).
                self._revoke_self_arm_for(removed_loop)
                self._emit("removed", removed_loop)
            return True
        finally:
            # The write settled (committed or rolled back): its note has done its
            # job or must not outlive it, either way.
            self._store.stop_notes.pop(loop_id, None)


async def _revoke_provider_credentials_before_removal(loop_id: str) -> None:
    """Require a durable provider denial before an agent-writable row disappears."""
    from kiro_crew import autonudge_provider_trust

    await asyncio.to_thread(
        autonudge_provider_trust.forget_monitor_owner_credentials,
        loop_id,
    )


async def _provider_credentials_authorized(loop: NudgeLoop) -> bool:
    """Whether this exact structured row currently owns provider credentials."""
    from kiro_crew import autonudge_provider_trust

    state = loop.monitor
    if state is None:
        return False
    return await asyncio.to_thread(
        autonudge_provider_trust.is_monitor_owner_credentials_recorded,
        loop.id,
        loop.slot_key,
        state.kind,
        state.target,
    )


async def _restore_provider_credentials(loop: NudgeLoop) -> None:
    """Restore the exact grant for a row whose replacement did not commit."""
    from kiro_crew import autonudge_provider_trust

    state = loop.monitor
    if state is None:
        raise ValueError("provider credential restoration requires a structured monitor")
    await asyncio.to_thread(
        autonudge_provider_trust.record_monitor_owner_credentials,
        loop.id,
        loop.slot_key,
        state.kind,
        state.target,
    )
