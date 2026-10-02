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
import math
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, fields
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew import autonudge_stop_log
from kiro_crew.autonudge_service.maintenance import (
    _assert_mutation_lock_owned,
    _claim_mutation_lock,
    _maintenance_lock,
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
    STOP_SENTINEL_REASON,
    AutoNudgeStaleBaseline,
    MonitorUpdateConflict,
    NudgeAdmissionRefused,
    NudgeLoop,
    ScheduledMessageInFlight,
    SlotNudgeRetirement,
    _stopped_row_is_replaceable,
    budget_elapsed,
    cap_reached,
    is_channel_key,
    is_scheduled_message,
    is_structured_monitor_loop,
    new_goal_token,
    reason_in,
    scheduled_message_trust_id,
)
from kiro_crew.autonudge_service.subject import (
    infer_monitor,
    infer_subject,
    needs_session_texts,
    read_session_texts,
)
from kiro_crew.goal_command import goal_command_mutates_automation
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


class _ScheduledTransition(Enum):
    UPDATE = "update"
    CANCEL = "cancel"
    COMPLETE = "complete"
    CLEANUP = "cleanup"
    DISCARD = "discard"


_SCHEDULED_RESTORE_RETRY_BASE_SECS = 0.25
_SCHEDULED_RESTORE_RETRY_MAX_SECS = 30.0


@dataclass
class _ScheduledRestoreRetry:
    """Exact close-retirement authority retained until restore or rejection."""

    retirement: SlotNudgeRetirement
    admission_check: Callable[[], bool] | None
    task: asyncio.Task[NudgeLoop | None] | None = None
    attempts: int = 0


async def _sleep_before_scheduled_restore_retry(delay: float) -> None:
    """Test seam for the bounded delay between metadata-write attempts."""
    await asyncio.sleep(delay)


async def add(
    self: AutoNudgeService,
    slot_key: str,
    message: str,
    idle_secs: int = 60,
    max_cycles: int = 0,
    stop_sentinel_path: str = "",
    max_runtime_secs: int = 0,
    banner: str = "",
    scheduled_at: float = 0.0,
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
) -> NudgeLoop:
    # CANCELLATION SAFETY: the mutate+persist runs as a SHIELDED task. If
    # the awaiting caller is cancelled mid-write, a bare await would release
    # ``_lock`` while the executor write is still running — a subsequent
    # add/update could persist newer state first and then be clobbered by
    # this operation's stale snapshot (lost update after restart). Shielding
    # keeps the inner task (and the lock) alive until the write completes,
    # so writes remain strictly serialized; the cancelled caller still sees
    # CancelledError, with the arm possibly landed (same "mutation may have
    # already landed" semantics as other cancellation-uncertain mutations).
    # The inner task is retained in ``_inflight_adds`` (discarded when done)
    # so it stays SUPERVISED — strongly referenced and completion-logged —
    # even if every awaiting caller has been cancelled.
    inner: "asyncio.Task[NudgeLoop]" = asyncio.ensure_future(
        self._add_locked(
            slot_key,
            message,
            idle_secs=idle_secs,
            max_cycles=max_cycles,
            stop_sentinel_path=stop_sentinel_path,
            max_runtime_secs=max_runtime_secs,
            banner=banner,
            scheduled_at=scheduled_at,
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
        )
    )
    self._inflight_adds.add(inner)

    def _finish(t: "asyncio.Task[NudgeLoop]") -> None:
        self._inflight_adds.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning("AutoNudge: detached add() failed", exc_info=t.exception())

    inner.add_done_callback(_finish)
    return await asyncio.shield(inner)


def _mint_loop_id(
    self: AutoNudgeService,
    requested: str | None,
    *,
    scheduled: bool = False,
) -> str:
    """The new loop's id: the caller's pre-minted one, else a fresh one.

    Scheduled loops carry their id into durable transcript retry and drop-notice
    identities, so they use the full UUID-sized value. Ordinary AutoNudge and
    monitor ids keep their historical eight-hex format.

    A caller pre-mints an id when something must be recorded ABOUT the loop
    before it exists -- the authorizer writes the keystone-gated self-arm
    entry first, so a failed trust write denies before this store is
    touched and a loop this arm would displace is never removed for
    nothing. Called under ``_lock``, so the in-use check is not racy; an id
    already in use is a caller bug and is refused as a conflict -- the
    caller's record would otherwise name the wrong loop.
    """
    if requested is None:
        minted = uuid.uuid4().hex
        return minted if scheduled else minted[:8]
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
    scheduled_at: float = 0.0,
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
            scheduled_at=scheduled_at,
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
    scheduled_at: float = 0.0,
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
) -> NudgeLoop:
    from kiro_crew import autonudge as seams  # read at call time: the facade imports us

    validate_runtime_secs(max_runtime_secs, allow_unbounded=True)
    idle_secs = max(_MIN_IDLE_SECS, min(_MAX_IDLE_SECS, int(idle_secs)))
    if isinstance(scheduled_at, bool):
        raise ValueError("scheduled_at must be a finite epoch-seconds number")
    scheduled_at = float(scheduled_at)
    if not math.isfinite(scheduled_at) or scheduled_at < 0:
        raise ValueError("scheduled_at must be a finite epoch-seconds number")
    if scheduled_at > 0 and is_channel_key(slot_key):
        raise ValueError("scheduled messages currently require a dashboard session")
    if scheduled_at > 0 and goal_command_mutates_automation(message):
        raise ValueError("scheduled messages cannot change session goals")
    # A bare ``PR <number>`` resolves against the session's own log. Reading it is a
    # whole-transcript read, so it runs in a worker thread BEFORE the lock is taken,
    # never on the event loop and never while other mutations wait.
    arm_session_texts = (
        await asyncio.to_thread(read_session_texts, message, slot_key, watch)
        if (gate or watch) and needs_session_texts(message, slot_key, watch)
        else None
    )
    async with self._lock:
        if admission_check is not None and not admission_check():
            raise NudgeAdmissionRefused("session changed before nudge arm committed")
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
            # Create-only (``replace_existing=False``) refuses ANY existing
            # record by default — the dashboard REST creates depend on that:
            # their documented contract is a 409 that never discards a
            # retained inspection record. ``replace_stopped`` is the
            # directive re-arm path's explicit opt-in to narrow the refusal
            # to ACTIVE records, because a retained INACTIVE row
            # (approval-stalled, capped, budget-spent, or a terminal record
            # kept for inspection) otherwise deadlocks the session's only
            # re-arm: monitor_update's approval-stall refusal names
            # monitor_start as the remedy. The wake-in-flight guard below
            # still runs for the replaced-inactive case, so a terminal
            # record whose accepted wake is awaiting completion evidence
            # keeps its own refusal rather than having its correlation
            # orphaned by a replacement.
            if (
                not replace_existing
                and not displaces_default
                and (existing.active or not replace_stopped)
            ):
                raise MonitorUpdateConflict("session already has an automation")
            if is_scheduled_message(existing):
                raise MonitorUpdateConflict(
                    "scheduled message must be unscheduled by an authenticated "
                    "dashboard user before another automation can replace it"
                )
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
            id=self._mint_loop_id(loop_id, scheduled=scheduled_at > 0),
            slot_key=slot_key,
            message="" if scheduled_at > 0 else message,
            idle_secs=idle_secs,
            max_cycles=1 if scheduled_at > 0 else max(0, int(max_cycles)),
            created_ts=now,
            goal_token=new_goal_token(),
            stop_sentinel_path="" if scheduled_at > 0 else stop_sentinel_path,
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
            next_due_ts=scheduled_at if scheduled_at > 0 else now + idle_secs,
            scheduled_message=scheduled_at > 0,
            scheduled_at=scheduled_at,
            # The SUBJECT is decided HERE, from the instruction the caller
            # already wrote -- no target, kind or enable flag is ever passed.
            # WHETHER to look for one is the ``gate`` argument above, which the
            # arming surfaces set and this service defaults to False; saying
            # "rather than from a parameter" was true before that default moved
            # and is not any more. What has never been a parameter, and is the
            # point, is the subject: every earlier attempt at this saving
            # shipped as an opt-in and measured zero adoption -- the switch
            # existed, the agent arming the loop was mid-task, and nothing made
            # it worth its five steps. There is no SUBJECT parameter to forget
            # here: whatever the caller already wrote is where the target comes
            # from, on every surface. Gating itself is not inherited by
            # construction, though: each arming surface chooses. monitor_start's
            # directive gates by default, the generic REST route does not.
            #
            # ``gate=False`` is the one escape, and it is an opt-OUT of a
            # default that lives at the ARMING SURFACE: monitor_start's own
            # directive gates unless told otherwise, while this service and the
            # generic REST route default to ungated -- they also arm loops whose
            # work is not a pull request. So it cannot repeat the zero-adoption
            # failure on the babysit path, which is the path the evidence is
            # about. The escape exists because a loop whose duty is to act WHILE
            # its subject is quiet is invisible to an observation of that
            # subject; keying that only on the wording of the instruction made a
            # cadence contract depend on prose.
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
                # An explicit ``watch`` gates on its own, without ``gate``. Scheduled
                # messages are one-shot user text, never observed subjects.
                if (gate or watch) and scheduled_at <= 0
                else None
            ),
            gate=bool(gate or watch) if scheduled_at <= 0 else False,
            banner=banner,
            self_armed=self_armed,
            default_patrol=bool(default_patrol),
        )
        self._loops[loop.id] = loop
        # Persist WITHOUT blocking the event loop (no-blocking-call rule:
        # _write_state fsyncs, and a wedged disk must not freeze the
        # gateway). Snapshot under the lock (mutation safety), write on a
        # worker thread, and await it so a persistence failure still
        # propagates to the caller before the loop is reported armed.
        payload = self._serialize_state()
        try:
            await asyncio.get_running_loop().run_in_executor(None, self._write_state, payload)
        except BaseException:
            self._loops.pop(loop.id, None)
            if existing is not None:
                self._loops[existing.id] = existing
                if restore_existing_provider_credentials:
                    await self._restore_provider_credentials(existing)
                if detached_timer is not None and not detached_timer.done():
                    # Still delivering: give it back rather than arm a second timer.
                    self._timers[existing.id] = detached_timer
                elif existing.active:
                    self._arm_from_deadline(existing)
            raise
        self._arm_from_deadline(loop)
        if existing is not None:
            # Committed: the displaced row is gone from the store, so its
            # self-arm entry is revoked now, not before the write.
            self._revoke_self_arm_for(existing)
            self._emit("removed", existing)
    self._emit("added", loop)
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
    scheduled_at: float | None = None,
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
            scheduled_at=scheduled_at,
            precondition=precondition,
            on_absent=on_absent,
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


async def update_pending_scheduled_message(
    self: AutoNudgeService,
    loop_id: str,
    *,
    message: str | None = None,
    scheduled_at: float | None = None,
) -> NudgeLoop | None:
    """Update one pending scheduled message under the service mutation lock."""
    inner: "asyncio.Task[NudgeLoop | None]" = asyncio.ensure_future(
        self._update_locked(
            loop_id,
            message="",
            scheduled_at=scheduled_at,
            _scheduled_message_update=True,
            _scheduled_exact_message=message,
        )
    )
    self._inflight_adds.add(inner)

    def _finish(t: "asyncio.Task[NudgeLoop | None]") -> None:
        self._inflight_adds.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning("AutoNudge: detached scheduled update failed", exc_info=t.exception())

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
    scheduled_at: float | None = None,
    _scheduled_message_update: bool = False,
    _scheduled_exact_message: str | None = None,
    precondition: Callable[[NudgeLoop], bool] | None = None,
    on_absent: Callable[[], None] | None = None,
) -> NudgeLoop | None:
    lock = await self._acquire_mutation_lock(loop_id)
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
            scheduled_at=scheduled_at,
            _scheduled_message_update=_scheduled_message_update,
            _scheduled_exact_message=_scheduled_exact_message,
            precondition=precondition,
            on_absent=on_absent,
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
    scheduled_at: float | None = None,
    _scheduled_message_update: bool = False,
    _scheduled_exact_message: str | None = None,
    precondition: Callable[[NudgeLoop], bool] | None = None,
    on_absent: Callable[[], None] | None = None,
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
        scheduled_provenance = None
        if is_scheduled_message(loop):
            if not _scheduled_message_update:
                raise MonitorUpdateConflict(
                    "protected scheduled messages require an authenticated dashboard user"
                )
            scheduled_provenance = await _scheduled_provenance_for_transition(
                loop, _ScheduledTransition.UPDATE
            )
            if scheduled_provenance is None:
                raise MonitorUpdateConflict("scheduled message provenance is not authorized")
            candidate_message = (
                _scheduled_exact_message
                if _scheduled_exact_message is not None
                else scheduled_provenance.message
            )
            if goal_command_mutates_automation(candidate_message):
                raise ValueError("scheduled messages cannot change session goals")
            if loop.cycle_count > 0 or loop.id in self._firing or not loop.active:
                raise ValueError("scheduled message is already firing or completed")
            message = ""
        if scheduled_at is not None:
            if isinstance(scheduled_at, bool):
                raise ValueError("scheduled_at must be a future epoch-seconds number")
            scheduled_at = float(scheduled_at)
            if not math.isfinite(scheduled_at) or scheduled_at <= time.time():
                raise ValueError("scheduled_at must be a future epoch-seconds number")
            if not is_scheduled_message(loop):
                raise ValueError("scheduled_at can only update a scheduled message")
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
                # The labelled history goes with the generation, for the reason the
                # criteria path states below: every label in it was earned answering
                # a question this instruction has just replaced. The instruction IS
                # the target, so those rows may also be about a different subject
                # entirely -- but the reset does not turn on that, because a reworded
                # instruction about the SAME subject is still a changed question, and
                # that is the case this path is most often used for. Carrying the rows
                # forward would show the judge a hit rate for a different question
                # from the one it now faces, and they supersede the single last
                # verdict, so that reading is what the next tick sees. The rollback restores
                # every field from the snapshot above, so a retarget that never lands
                # returns the history untouched.
                loop.judge_quiet_streak = 0
                loop.judge_cursors = {}
                loop.judge_pr_seen = {}
                loop.judge_last_verdict = {}
                loop.judge_recent_verdicts = []
                # The instruction IS the target, so a changed instruction can
                # change the subject. Re-infer, or the loop keeps polling the
                # pull request it was armed on: the new subject is never
                # watched, and the old one merging would retire the loop while
                # the work it was retargeted to sits unobserved.
                #
                # An unchanged subject keeps its existing monitor rather than a
                # fresh one -- refining the wording of an instruction about the
                # same PR is the common use of this path, and rebuilding would
                # discard the metering counters and the follow-up allowance for
                # no reason. A message that stops naming one subject clears
                # the monitor, which returns the loop to a plain timer.
                #
                # A loop armed with gate=False is never re-inferred here. Its
                # caller said the cadence matters, and re-gating it because the
                # wording changed would revoke that through the documented way
                # to revise a loop -- silently, since an ungated loop and a
                # re-gated one look identical until the turns stop arriving.
                #
                # The rebinding itself happens AFTER the judge block below, not
                # here. The brief's target list also names the subject, an update
                # may carry a new message and a new brief in one call, and the
                # subject has to be resolved from the pair this loop ends up with
                # -- deciding it here would read the brief being replaced.
                rebind_monitor = True
        if banner is not None:
            # Display-only, so no deadline or timer consequence — unlike
            # ``idle_secs`` below, quieting a running loop must not restart
            # its countdown. "" clears it back to the verbose default.
            loop.banner = banner
        if judge is not None:
            # ``{}`` clears the owner's own CRITERIA, after which a gated loop runs
            # under the default brief; ``judge: false`` is what takes the judge off
            # a live loop, stored as the reserved opt-out marker. Any other object
            # replaces the criteria. Absent leaves it alone, so an update that only
            # changes the interval does not disarm the judge.
            #
            # Both the streak and the read cursors are reset with it, because they
            # are facts about the OLD brief: a streak earned under one set of
            # criteria must not count toward the floor under another, and a cursor
            # belongs to a target list that may have just changed. The pull-request
            # baseline goes for the same reason and matters most: it records the
            # reading a VERDICT was reached on, and that verdict answered the
            # question being replaced, so keeping it would let an unchanged board
            # screen the new criteria quiet without ever putting them to the judge.
            # Resetting costs at most one re-read; keeping them could hold a loop
            # quiet on a brief nobody armed.
            loop.judge = seams.scrubbed_judge_spec(judge)
            loop.judge_quiet_streak = 0
            loop.judge_cursors = {}
            loop.judge_pr_seen = {}
            loop.judge_last_verdict = {}
            # The labelled history goes too: every label in it was earned against
            # the criteria being replaced, so carrying it forward would show the
            # judge a hit rate for a question nobody is asking any more.
            loop.judge_recent_verdicts = []
            # A REPLACED brief can name a different pull request, and the collector
            # will ask about the new list from the next tick on. A monitor left on
            # the old subject would publish a reading the collector drops, so the
            # watch would go on spending a fetch and the judge would see nothing.
            # Only for a gated loop: a structured monitor is refused far above, and
            # an ungated loop has no judge to read.
            rebind_monitor = rebind_monitor or loop.gate
        requested_watch = str(watch or "").strip()
        if requested_watch:
            # ARM a subject on a live loop. This is the one subject a message edit cannot
            # reach: a work-ledger watch observes the session's OWN key, which no wording
            # contains, so without this field a conductor that armed a plain timer has to
            # tear its loop down -- losing the cycle count the revision path exists to
            # keep -- in order to start gating.
            #
            # ``gate`` is set with it, the same fold the arm path applies for the same
            # reason: the tick path reads the STORED flag and refuses to poll a loop whose
            # gate is False even when it carries a monitor, so leaving the two to disagree
            # produces a loop that looks armed and observes nothing.
            loop.gate = True
            rebind_monitor = True
        # ONE place resolves the subject, from the two strings this loop now holds.
        # Reached by a changed instruction and by a replaced brief alike, because
        # either can name a different pull request and only the pair says which.
        if rebind_monitor:
            # The loop's OWN kind, fed back into every derivation below. A
            # work-ledger watch's subject is this session, which no message
            # names, so re-inferring from text alone would answer "this
            # instruction names nothing observable" and CLEAR the monitor --
            # turning the documented way to reword an instruction into a
            # silent way to disarm the watch. For gh-pr it changes nothing.
            # A watch REQUESTED by this update wins over the loop's current kind, and has
            # to: on a loop with no monitor the stored kind is "", which re-infers from
            # text alone, answers "this instruction names nothing observable", and leaves
            # the monitor None -- so the field would validate, reach here, and do nothing.
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
                    # A wake claimed for the OLD subject must not be spent on
                    # the new one. The claim is keyed by loop id, so without
                    # this the in-flight turn's delivery charges a wake to a
                    # monitor that has observed nothing, and grants it a
                    # follow-up allowance it never earned. Remembered so the
                    # persistence rollback below can hand it back if this
                    # retarget never lands.
                    claim_discarded_for_retarget = loop.id in self._pending_monitor_wake
                    self._pending_monitor_wake.discard(loop.id)
                    # And the floor claim, for the identical reason. I added
                    # that second claim one round ago and wrote on the pull
                    # request that two hand-written claim sets with two release
                    # points would go wrong at the third site; this IS that
                    # site, missed by the same change that predicted it. Third
                    # time a claim has been released in one set and forgotten
                    # in another.
                    floor_discarded_for_retarget = loop.id in self._pending_floor_tick
                    self._pending_floor_tick.discard(loop.id)
                    loop.monitor = inferred
                    if not generation_advanced:
                        # A BRIEF-only retarget changes the watched subject while
                        # leaving the instruction alone, so nothing above advances
                        # the generation -- and a structural-terminal verdict
                        # recorded for the OLD subject would then still read as
                        # current and deactivate this new watch. The stale flag is
                        # not confined to one in-flight turn: it sits on the slot
                        # until the next genuine turn, so the window is a whole
                        # interval. Same rule as a changed instruction: a different
                        # subject is a different configuration.
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
        if scheduled_at is not None:
            loop.scheduled_at = scheduled_at
            loop.next_due_ts = scheduled_at
        #: Set when ``active=True`` reached a FINISHED row and was declined.
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
                # A monitor with an outcome is finished -- its subject merged,
                # or its budget is spent. Reviving it would fire ungated
                # prompts at a settled subject, because the tick gate
                # declines to observe a monitor that already has an outcome.
                #
                # A current, unsettled monitor falls through to the ordinary
                # activation below: its delivery is gated per tick, so
                # resuming it cannot inject an ungated prompt. Spelling both
                # refusals out separately matters -- collapsing them into one
                # branch on ``loop.monitor is not None`` swallowed the
                # revival entirely, leaving the loop neither refused nor
                # activated.
                loop.active = False
                loop.next_due_ts = 0.0
            elif (
                active
                and not loop.active
                and reason_in(loop.stopped_reason, FINISHED_LOOP_REASONS)
                and not _stop_file_lifted(loop)
            ):
                # A FINISHED loop: the agent created its stop file, or the watched
                # subject merged or closed (that one is usually refused above by
                # its outcome; this also covers a row whose monitor was cleared by
                # a retarget after it finished). There is nothing to resume --
                # reviving would re-fire a goal the agent declared met, or poll a
                # settled subject -- so the revival is declined whoever asks: the
                # popover's Play, ``monitor_update``, an app reconciler. The
                # record stays inactive under its reason; clearing it, or arming
                # a NEW loop that displaces it, are the ways on.
                #
                # The one exception is a stop file that is GONE: Issue Radar and
                # Research Lab arm the same file as an operator's kill switch, and
                # an operator who deletes it means "run again" -- their reconciler
                # re-arms the row on its next pass. The goal popover never deletes
                # the file (the next arm on the slot does, after Clear), so its Done
                # stays as it is.
                logger.info(
                    "AutoNudge: loop %s is finished (%s) — not reviving it",
                    loop.id,
                    loop.stopped_reason,
                )
                refused_revival = True
            # TERMINAL-TRANSITION ATOMICITY: a bound-tagged deactivation
            # (stopped_reason supplied — the _timer's cycle_cap /
            # runtime_budget paths) must never OVERWRITE a deactivation
            # that landed first. The race: user pauses right after the
            # timer detects expiry — the pause persists "manual" and
            # cancels the timer, but the timer's already-inflight shielded
            # update would stamp "runtime_budget" over it, making the loop
            # budget-revivable against an explicit pause. Both transitions
            # serialize on _lock, so re-checking here closes the race: the
            # bound's deactivation degrades to a no-op when the loop is
            # already inactive. The reverse order -- a reasonless pause
            # arriving after the bound -- is closed two branches down.
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
                    # was_active``. A still-active loop also receives
                    # ``active=True`` from an ordinary settings save (the
                    # goal popover sends it on every edit), and treating
                    # that as an answer would erase evidence recorded
                    # moments earlier and let one more doomed cycle fire.
                    # Keeping it costs at most a resumable stop the operator
                    # can undo; dropping it costs a wasted cycle and the
                    # silence this stop exists to end.
                    if not was_active:
                        loop.approval_stalled = False
                        # Same rule, same reason: the streaks are evidence
                        # about a PAST run, and a revival starts a fresh one.
                        loop.consecutive_start_failures = 0
                        loop.consecutive_failed_cycles = 0
                        # The user's resume resets the counter BEHIND A SPENT
                        # BOUND, and only that one: a spent cycle cap zeroes
                        # ``cycle_count``, a spent time budget re-anchors
                        # ``created_ts`` (it IS the budget clock -- every reader
                        # of the runtime left measures from it). Without the
                        # reset the timer re-stops the loop on its first tick
                        # unless the bound was raised first (Hermes ``/goal
                        # resume`` resets the turn counter). Spent means EITHER
                        # the reason the loop stopped with names that bound,
                        # read from the snapshot because this revival has
                        # already cleared the field, OR the bound as it stands
                        # after this update would refuse the first tick anyway:
                        # the wall clock keeps running through a pause, so a
                        # loop paused by hand with an hour of budget left and
                        # resumed the next day is as dead a press as one the
                        # timer stopped, and a cap raised in this same save is
                        # read here as raised. The OTHER counter is kept: the
                        # cap is a lifetime limit the user typed, and a pause
                        # must not quietly turn it into a fresh one, so that
                        # same overnight pause at 7 of 24 comes back at 7 of 24
                        # on a fresh clock. A pause with both allowances left
                        # -- by hand, by the agent, on a stalled approval or a
                        # session that would not start -- resets nothing. A
                        # non-string reason simply matches neither bound, and
                        # the bound reads are type-safe, so no malformed row
                        # raises here after ``active`` has already flipped and
                        # leaves the loop half-revived in memory. Opt-in rather
                        # than a property of every revival: the Research Lab and
                        # Issue Radar reconcilers re-arm ANY inactive loop of a
                        # live campaign or crew every few seconds, and a
                        # ``monitor_update`` bound raise asks for its increment
                        # -- on those paths a reset would turn the user's cap
                        # into a per-run allowance.
                        stopped_with = previous["stopped_reason"]
                        if fresh_run and (stopped_with == CYCLE_CAP_REASON or cap_reached(loop)):
                            loop.cycle_count = 0
                        if fresh_run and (
                            stopped_with == RUNTIME_BUDGET_REASON or budget_elapsed(loop)
                        ):
                            loop.created_ts = time.time()
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
        # Deadline bookkeeping (BEFORE the snapshot below so it persists):
        # an interval change restarts an EXISTING countdown at the new
        # interval — the old deadline encodes the old cadence and honouring
        # it would make the new setting take a full stale cycle to apply.
        # Any other patch (message edit, cap raise) keeps the deadline, so
        # a monitor_update refining the instruction never delays the next
        # check. Deactivation clears it — a paused loop holds no schedule.
        # A deadline that is ALREADY cleared (a delivered fire whose turn
        # is still running — nudge turns commonly call monitor_update)
        # stays cleared: the turn's END anchors the next full countdown
        # via notify_turn_complete, and assigning here would start the
        # interval mid-turn, so a turn longer than the interval would be
        # followed by a spurious overdue fire instead of a full cycle.
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
            await asyncio.get_running_loop().run_in_executor(None, self._write_state, payload)
        except BaseException:
            for field_name, value in previous.items():
                setattr(loop, field_name, value)
            if claim_was_held:
                # The retarget above dropped this loop's pending wake claim,
                # because a claim earned by the OLD subject must not be spent on
                # the new one. If the write then fails the retarget did not
                # happen -- so the claim belongs to the loop again, and leaving
                # it discarded costs the delivered wake its accounting and its
                # follow-up turn. Rolling back the fields but not this is the
                # same incomplete-restore defect as the terminal transition's.
                self._pending_monitor_wake.add(loop.id)
            if floor_discarded_for_retarget:
                # Same restore, same reason. This is the fourth hand-written
                # restore of a per-loop claim in this file, and the review has now
                # found a claim missing from one of them three separate times --
                # which is the argument for one transition object with one restore
                # rather than a fifth.
                self._pending_floor_tick.add(loop.id)
            raise
        if scheduled_provenance is not None:
            # circular import: autonudge imports this owner; self-arm state stays lazy.
            from kiro_crew import autonudge_selfarm

            trusted_at = (
                scheduled_at if scheduled_at is not None else scheduled_provenance.scheduled_at
            )
            exact_message = (
                _scheduled_exact_message
                if _scheduled_exact_message is not None
                else scheduled_provenance.message
            )
            try:
                replaced = await asyncio.to_thread(
                    autonudge_selfarm.replace_scheduled_message,
                    scheduled_message_trust_id(loop.id),
                    scheduled_provenance,
                    exact_message,
                    trusted_at,
                )
                if not replaced:
                    raise MonitorUpdateConflict(
                        "scheduled message provenance changed during update"
                    )
            except BaseException:
                for field_name, value in previous.items():
                    setattr(loop, field_name, value)
                rollback_payload = self._serialize_state()
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, self._write_state, rollback_payload
                    )
                except BaseException:
                    logger.error(
                        "AutoNudge: failed to persist scheduled rollback for loop %s",
                        loop.id,
                        exc_info=True,
                    )
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
    self: AutoNudgeService,
    loop_id: str,
    *,
    persist: bool = True,
    emit: bool = True,
    _scheduled_transition: _ScheduledTransition | None = None,
) -> NudgeLoop | None:
    """Remove a loop, requiring an explicit transition for scheduled records."""
    existing = self._loops.get(loop_id)
    if existing is not None and is_scheduled_message(existing) and _scheduled_transition is None:
        raise MonitorUpdateConflict(
            "protected scheduled messages require an authenticated dashboard user"
        )
    if persist and loop_id in self._loops:
        from kiro_crew import autonudge_provider_trust

        autonudge_provider_trust.forget_monitor_owner_credentials(loop_id)
    loop = self._loops.pop(loop_id, None)
    if loop is None:
        return None
    self._cancel_timer(loop_id)
    self._rearm_fail_count.pop(loop_id, None)
    self._start_failure_deferred.pop(loop_id, None)
    self._rearm_pending.discard(loop_id)
    # Beside the line above, and for its reason: a claim released in one set and
    # forgotten in another outlives its loop, and an id reused by a later loop would
    # inherit a pull-forward nobody asked for.
    self._pulled_forward.discard(loop_id)
    self._pushed_ticks.discard(loop_id)
    self._pushed_running.discard(loop_id)
    self._pull_forward_counts.pop(loop_id, None)
    self._pull_forward_capped = {pair for pair in self._pull_forward_capped if pair[0] != loop_id}
    self._accepted_monitor_turns.pop(loop_id, None)
    self._scheduled_delivery_pending.pop(loop_id, None)
    self._scheduled_turn_outcomes.pop(loop_id, None)
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


def _revoke_self_arm_for(
    self: AutoNudgeService,
    loop: NudgeLoop,
    *,
    scheduled_memory_already_revoked: bool = False,
) -> None:
    """Revoke for EVERY loop leaving the store, not only ``self_armed`` ones.

    The ``self_armed`` bit lives in the agent-writable store, so keying the
    revocation on it would let a forged ``false`` (plus a restart) skip the
    revoke and leave the keystone entry orphaned for a later loop that
    reuses the id on the same slot -- the exact adversary the trust record
    exists to defeat. Revocation therefore keys on the one fact the store
    cannot forge: the loop is being removed. A loop with no entry costs one
    offloaded read (``forget_self_arm`` writes only when it deletes).
    """
    self._revoke_self_arm(
        loop.id,
        scheduled_memory_already_revoked=scheduled_memory_already_revoked,
    )


def _revoke_self_arm(
    loop_id: str,
    *,
    scheduled_memory_already_revoked: bool = False,
) -> None:
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
    # circular import: autonudge imports this owner; trust state stays lazy.
    from kiro_crew import autonudge_provider_trust, autonudge_selfarm

    def _forget_all_trust() -> None:
        autonudge_selfarm.forget_self_arm(loop_id)
        if not scheduled_memory_already_revoked:
            autonudge_selfarm.delete_scheduled_message_record(scheduled_message_trust_id(loop_id))
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
    """Remove a loop if its live row satisfies ``precondition`` under the lock."""
    lock = await self._acquire_mutation_lock(loop_id)
    if lock is None:
        return False
    try:
        return await self._remove_unserialized(
            loop_id,
            precondition=precondition,
            on_absent=on_absent,
            stop_reason=stop_reason,
            stop_detail=stop_detail,
            mutation_lock=lock,
        )
    finally:
        _release_mutation_lock(lock)


async def remove_by_slot(self: AutoNudgeService, slot_key: str) -> SlotNudgeRetirement | None:
    """Retire the current slot generation inside one maintenance transaction."""
    lock = _maintenance_lock(self._base_dir)
    async with lock:
        _claim_mutation_lock(lock)
        try:
            loop = self._find_by_slot(slot_key)
            if loop is None:
                return None
            if is_scheduled_message(loop) and (
                loop.cycle_count > 0
                or loop.id in self._scheduled_delivery_pending
                or loop.id in self._scheduled_turn_outcomes
            ):
                raise ScheduledMessageInFlight("scheduled message turn is still settling")
            scheduled: list[Any | None] = []
            if is_structured_monitor_loop(loop):
                await self.retire_monitor_for_session_close(loop.id)
            else:
                removed = await self._remove_unserialized(
                    loop.id,
                    stop_reason="session_closed",
                    mutation_lock=lock,
                    _scheduled_transition=(
                        _ScheduledTransition.DISCARD if is_scheduled_message(loop) else None
                    ),
                    _scheduled_provenance_out=scheduled,
                )
                if not removed:
                    return None
            return SlotNudgeRetirement(
                loop=loop,
                scheduled_provenance=scheduled[0] if scheduled else None,
            )
        finally:
            _unclaim_mutation_lock(lock)


async def _scheduled_provenance_for_transition(
    loop: NudgeLoop,
    transition: _ScheduledTransition,
) -> Any | None:
    """Authorize a scheduled transition from process-memory provenance."""
    # circular import: autonudge imports this owner; self-arm state stays lazy.
    from kiro_crew import autonudge_selfarm

    if transition is _ScheduledTransition.DISCARD:
        return None
    provenance = await asyncio.to_thread(
        autonudge_selfarm.read_scheduled_message,
        scheduled_message_trust_id(loop.id),
        loop.slot_key,
    )
    if transition in {_ScheduledTransition.UPDATE, _ScheduledTransition.CANCEL}:
        if provenance is None or provenance.completed:
            raise MonitorUpdateConflict("scheduled message provenance is not authorized")
        return provenance
    if transition is _ScheduledTransition.COMPLETE:
        if provenance is None:
            raise MonitorUpdateConflict("scheduled message provenance is not authorized")
        if not provenance.completed:
            marked = await asyncio.to_thread(
                autonudge_selfarm.mark_scheduled_message_completed,
                scheduled_message_trust_id(loop.id),
                provenance,
            )
            if not marked:
                raise MonitorUpdateConflict(
                    "scheduled message provenance changed before completion"
                )
            provenance = autonudge_selfarm.ScheduledMessageProvenance(
                provenance.slot_key,
                provenance.message,
                provenance.scheduled_at,
                completed=True,
                containment_meta=provenance.containment_meta,
            )
        return provenance
    if provenance is None or not provenance.completed:
        raise MonitorUpdateConflict("scheduled message cleanup lacks trusted completion evidence")
    return provenance


async def _revoke_scheduled_provenance_before_removal(loop: NudgeLoop) -> Any | None:
    """Capture and delete provenance before mutable removal can commit."""
    # circular import: autonudge imports this owner; self-arm state stays lazy.
    from kiro_crew import autonudge_selfarm

    trust_id = scheduled_message_trust_id(loop.id)

    def _capture_revoke_and_verify() -> Any | None:
        provenance = autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key)
        autonudge_selfarm.delete_scheduled_message_record(trust_id)
        if autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key) is not None:
            raise OSError("scheduled-message provenance survived revocation")
        return provenance

    return await asyncio.to_thread(_capture_revoke_and_verify)


async def _restore_scheduled_provenance(loop: NudgeLoop, provenance: Any) -> None:
    """Restore exact process-memory state after a mutable rollback."""
    # circular import: autonudge imports this owner; self-arm state stays lazy.
    from kiro_crew import autonudge_selfarm

    trust_id = scheduled_message_trust_id(loop.id)

    def _restore_and_verify() -> None:
        autonudge_selfarm.record_scheduled_message(
            trust_id,
            provenance.slot_key,
            provenance.message,
            provenance.scheduled_at,
            containment_meta=provenance.containment_meta,
        )
        if provenance.completed:
            pending = autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key)
            if pending is None or not autonudge_selfarm.mark_scheduled_message_completed(
                trust_id, pending
            ):
                raise OSError("scheduled-message completion state could not be restored")
        if autonudge_selfarm.read_scheduled_message(trust_id, loop.slot_key) != provenance:
            raise OSError("scheduled-message provenance could not be restored")

    await asyncio.to_thread(_restore_and_verify)


async def remove_pending_scheduled_message(self: AutoNudgeService, loop_id: str) -> Any | None:
    """Capture exact text and remove a still-pending authenticated schedule."""
    if loop_id in self._firing:
        return None
    lock = await self._acquire_mutation_lock(loop_id)
    if lock is None:
        return None

    def _still_pending(current: NudgeLoop) -> bool:
        return (
            is_scheduled_message(current)
            and current.active
            and current.cycle_count == 0
            and current.id not in self._firing
        )

    captured: list[Any | None] = []
    try:
        removed = await self._remove_unserialized(
            loop_id,
            precondition=_still_pending,
            mutation_lock=lock,
            _scheduled_transition=_ScheduledTransition.CANCEL,
            _scheduled_provenance_out=captured,
        )
    finally:
        _release_mutation_lock(lock)
    if not removed:
        return None
    provenance = captured[0] if captured else None
    if provenance is None:
        raise ValueError("scheduled message provenance is unavailable")
    return provenance


async def discard_scheduled_message(self: AutoNudgeService, loop_id: str) -> bool:
    """Discard one unusable scheduled record at a gateway lifecycle boundary."""
    lock = await self._acquire_mutation_lock(loop_id)
    if lock is None:
        return False
    try:
        return await self._remove_unserialized(
            loop_id,
            precondition=is_scheduled_message,
            mutation_lock=lock,
            _scheduled_transition=_ScheduledTransition.DISCARD,
        )
    finally:
        _release_mutation_lock(lock)


def _track_scheduled_restore_task(
    service: AutoNudgeService,
    retry: _ScheduledRestoreRetry,
    task: "asyncio.Task[NudgeLoop | None]",
) -> None:
    """Keep one restore task supervised until it settles or service stop cancels it."""
    retry.task = task
    service._inflight_adds.add(task)

    def _finish(done: "asyncio.Task[NudgeLoop | None]") -> None:
        service._inflight_adds.discard(done)
        if not done.cancelled() and done.exception() is not None:
            logger.warning(
                "AutoNudge: detached scheduled close rollback failed",
                exc_info=done.exception(),
            )

    task.add_done_callback(_finish)


def _scheduled_restore_is_admissible(retry: _ScheduledRestoreRetry) -> bool:
    """Fail closed when the original dashboard generation cannot be proved."""
    if retry.admission_check is None:
        return True
    try:
        return retry.admission_check()
    except Exception:
        logger.warning("AutoNudge: scheduled close rollback admission check failed", exc_info=True)
        return False


async def _retry_scheduled_message_after_failed_session_close(
    service: AutoNudgeService,
    retry: _ScheduledRestoreRetry,
) -> NudgeLoop | None:
    """Retry one retained metadata restore without spinning on a wedged disk."""
    loop_id = retry.retirement.loop.id
    while service._scheduled_restore_retries.get(loop_id) is retry:
        if not _scheduled_restore_is_admissible(retry):
            service._scheduled_restore_retries.pop(loop_id, None)
            return None
        delay = min(
            _SCHEDULED_RESTORE_RETRY_BASE_SECS * (2 ** min(retry.attempts, 16)),
            _SCHEDULED_RESTORE_RETRY_MAX_SECS,
        )
        retry.attempts += 1
        await _sleep_before_scheduled_restore_retry(delay)
        if service._scheduled_restore_retries.get(loop_id) is not retry:
            return None
        if not _scheduled_restore_is_admissible(retry):
            service._scheduled_restore_retries.pop(loop_id, None)
            return None
        try:
            restored = await service._restore_scheduled_message_after_failed_session_close(
                retry.retirement,
                admission_check=retry.admission_check,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "AutoNudge: scheduled close rollback retry failed",
                exc_info=True,
            )
            continue
        service._scheduled_restore_retries.pop(loop_id, None)
        return restored
    return None


async def _attempt_scheduled_message_after_failed_session_close(
    service: AutoNudgeService,
    retry: _ScheduledRestoreRetry,
) -> NudgeLoop | None:
    """Run the caller-visible attempt and retain authority before propagating failure."""
    loop_id = retry.retirement.loop.id
    try:
        restored = await service._restore_scheduled_message_after_failed_session_close(
            retry.retirement,
            admission_check=retry.admission_check,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        if service._scheduled_restore_retries.get(loop_id) is retry:
            retry_task = asyncio.create_task(
                _retry_scheduled_message_after_failed_session_close(service, retry)
            )
            _track_scheduled_restore_task(service, retry, retry_task)
        raise
    service._scheduled_restore_retries.pop(loop_id, None)
    return restored


async def restore_scheduled_message_after_failed_session_close(
    self: AutoNudgeService,
    retirement: SlotNudgeRetirement,
    *,
    admission_check: Callable[[], bool] | None = None,
) -> NudgeLoop | None:
    """Restore one close-retired schedule only for its original slot generation."""
    loop_id = retirement.loop.id
    existing = self._scheduled_restore_retries.get(loop_id)
    if existing is not None:
        task = existing.task
        if task is None:
            return None
        return await asyncio.shield(task)

    retry = _ScheduledRestoreRetry(
        retirement=retirement,
        admission_check=admission_check,
    )
    self._scheduled_restore_retries[loop_id] = retry
    inner: "asyncio.Task[NudgeLoop | None]" = asyncio.create_task(
        _attempt_scheduled_message_after_failed_session_close(self, retry)
    )
    _track_scheduled_restore_task(self, retry, inner)
    return await asyncio.shield(inner)


def cancel_scheduled_restore_retries(service: AutoNudgeService) -> None:
    """Release retained rollback authority and cancel every retry on service stop."""
    retries = tuple(service._scheduled_restore_retries.values())
    service._scheduled_restore_retries.clear()
    for retry in retries:
        task = retry.task
        if task is not None and not task.done() and not task.get_loop().is_closed():
            task.cancel()


async def _restore_scheduled_message_after_failed_session_close(
    self: AutoNudgeService,
    retirement: SlotNudgeRetirement,
    *,
    admission_check: Callable[[], bool] | None,
) -> NudgeLoop | None:
    loop = retirement.loop
    provenance = retirement.scheduled_provenance
    if provenance is None or not is_scheduled_message(loop):
        return None
    lock = _maintenance_lock(self._base_dir)
    async with lock:
        _claim_mutation_lock(lock)
        try:
            async with self._lock:
                if admission_check is not None and not admission_check():
                    return None
                if self._find_by_slot(loop.slot_key) is not None or loop.id in self._loops:
                    return None
                await self._restore_scheduled_provenance(loop, provenance)
                if admission_check is not None and not admission_check():
                    await self._revoke_scheduled_provenance_before_removal(loop)
                    return None
                restored = deepcopy(loop)
                restored.message = ""
                restored.scheduled_message = True
                restored.scheduled_at = provenance.scheduled_at
                restored.scheduled_completed = provenance.completed
                restored.next_due_ts = provenance.scheduled_at if restored.active else 0.0
                payload = self._serialize_state()
                payload["loops"] = self._serialized_loops(extra=[restored])

                def _publish_restored() -> None:
                    self._loops[restored.id] = restored
                    if restored.active:
                        self._arm_from_deadline(restored)
                    self._emit("added", restored)

                try:
                    await self._write_monitor_snapshot_locked(payload)
                except asyncio.CancelledError:
                    # The snapshot writer reports cancellation only after its
                    # executor write settles. Publish the state that is already
                    # durable before preserving caller cancellation, matching
                    # the staged-monitor transition contract.
                    _publish_restored()
                    raise
                except BaseException:
                    await self._revoke_scheduled_provenance_before_removal(restored)
                    raise
                if admission_check is not None and not admission_check():
                    await self._revoke_scheduled_provenance_before_removal(restored)
                    await self._write_monitor_snapshot_locked()
                    return None
                _publish_restored()
                return restored
        finally:
            _unclaim_mutation_lock(lock)


async def mark_scheduled_message_completed(self: AutoNudgeService, loop: NudgeLoop) -> Any:
    """Mark the exact pending scheduled provenance inert before cleanup."""
    return await _scheduled_provenance_for_transition(loop, _ScheduledTransition.COMPLETE)


async def cleanup_completed_scheduled_message(self: AutoNudgeService, loop_id: str) -> bool:
    """Remove a one-shot only when process-memory completion authorizes it."""
    lock = await self._acquire_mutation_lock(loop_id)
    if lock is None:
        return False
    try:
        loop = self._loops.get(loop_id)
        if loop is None or not is_scheduled_message(loop):
            return False
        await _scheduled_provenance_for_transition(loop, _ScheduledTransition.CLEANUP)
        return await self._remove_unserialized(
            loop_id,
            precondition=lambda current: (
                is_scheduled_message(current) and current.scheduled_completed
            ),
            mutation_lock=lock,
            _scheduled_transition=_ScheduledTransition.CLEANUP,
        )
    finally:
        _release_mutation_lock(lock)


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

    lock = await self._acquire_mutation_lock(monitor_id)
    if lock is None:
        return False
    try:
        return await self._remove_unserialized(
            monitor_id,
            precondition=_still_terminal,
            mutation_lock=lock,
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
    _scheduled_transition: _ScheduledTransition | None = None,
    _scheduled_provenance_out: list[Any | None] | None = None,
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
        scheduled_provenance: Any | None = None
        delivery_pending_present = loop_id in self._scheduled_delivery_pending
        delivery_pending = self._scheduled_delivery_pending.get(loop_id)
        turn_outcome_present = loop_id in self._scheduled_turn_outcomes
        turn_outcome = self._scheduled_turn_outcomes.get(loop_id)
        if existed:
            assert current is not None
            if is_scheduled_message(current) and _scheduled_transition is None:
                raise MonitorUpdateConflict(
                    "protected scheduled messages require an authenticated dashboard user"
                )
            was_active = current.active
            current.active = False
            self._cancel_timer(loop_id)
            try:
                if is_scheduled_message(current):
                    revoke_task = asyncio.create_task(
                        self._revoke_scheduled_provenance_before_removal(current)
                    )
                    revoke_cancelled = False
                    while not revoke_task.done():
                        try:
                            await asyncio.shield(revoke_task)
                        except asyncio.CancelledError:
                            revoke_cancelled = True
                    scheduled_provenance = revoke_task.result()
                    if revoke_cancelled:
                        if scheduled_provenance is not None:
                            await self._restore_scheduled_provenance(current, scheduled_provenance)
                        current.active = was_active
                        if current.active and scheduled_provenance is not None:
                            self._arm_from_deadline(current)
                        raise asyncio.CancelledError()
                restore_provider_credentials = await self._provider_credentials_authorized(current)
                await self._revoke_provider_credentials_before_removal(loop_id)
            except BaseException:
                if scheduled_provenance is not None:
                    await self._restore_scheduled_provenance(current, scheduled_provenance)
                current.active = was_active
                if current.active and (
                    not is_scheduled_message(current) or scheduled_provenance is not None
                ):
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
        close_rollback_requested = _scheduled_provenance_out is not None
        if existed:
            removed_loop = self.remove_sync(
                loop_id,
                persist=False,
                emit=False,
                _scheduled_transition=_scheduled_transition,
            )
            self._store.pending_removals.add(loop_id)
            if stop_reason:
                self._store.stop_notes[loop_id] = (
                    autonudge_stop_log.safe_text(stop_reason),
                    autonudge_stop_log.safe_text(stop_detail, autonudge_stop_log.DETAIL_MAX_CHARS),
                )
        payload = self._serialize_state()
        fut = asyncio.get_running_loop().run_in_executor(None, self._write_state, payload)
        scheduled_memory_already_revoked = removed_loop is not None and is_scheduled_message(
            removed_loop
        )

        async def _restore_failed_removal(*, rearm: bool = True) -> None:
            self._store.pending_removals.discard(loop_id)
            if removed_loop is None:
                return
            self._loops[loop_id] = removed_loop
            if close_rollback_requested:
                if delivery_pending_present:
                    self._scheduled_delivery_pending[loop_id] = delivery_pending
                if turn_outcome_present and turn_outcome is not None:
                    self._scheduled_turn_outcomes[loop_id] = turn_outcome
            if scheduled_provenance is not None:
                await self._restore_scheduled_provenance(removed_loop, scheduled_provenance)
            if restore_provider_credentials:
                await self._restore_provider_credentials(removed_loop)
            if (
                rearm
                and removed_loop.active
                and (not is_scheduled_message(removed_loop) or scheduled_provenance is not None)
            ):
                self._arm_from_deadline(removed_loop)

        try:
            await asyncio.shield(fut)
        except asyncio.CancelledError:
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
            if close_rollback_requested and removed_loop is not None:

                async def _rollback_cancelled_close() -> None:
                    # The removal snapshot committed, but cancellation prevents
                    # the caller from receiving its rollback token. Restore the
                    # exact row and provenance before allowing the close to end.
                    await _restore_failed_removal(rearm=False)
                    await self._write_monitor_snapshot_locked()
                    if removed_loop.active:
                        self._arm_from_deadline(removed_loop)

                rollback = asyncio.create_task(_rollback_cancelled_close())
                while not rollback.done():
                    try:
                        await asyncio.shield(rollback)
                    except asyncio.CancelledError:
                        continue
                rollback.result()
                raise asyncio.CancelledError()
            self._store.pending_removals.discard(loop_id)
            if removed_loop is not None:
                self._revoke_self_arm_for(
                    removed_loop,
                    scheduled_memory_already_revoked=scheduled_memory_already_revoked,
                )
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
                self._revoke_self_arm_for(
                    removed_loop,
                    scheduled_memory_already_revoked=scheduled_memory_already_revoked,
                )
                self._emit("removed", removed_loop)
            if _scheduled_provenance_out is not None:
                _scheduled_provenance_out.append(scheduled_provenance)
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
