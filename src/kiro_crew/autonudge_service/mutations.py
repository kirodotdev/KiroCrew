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
from typing import TYPE_CHECKING, Any, Callable, Mapping

from kiro_crew import autonudge_stop_log
from kiro_crew.autonudge_service.maintenance import (
    _assert_mutation_lock_owned,
    _await_future_deferring_cancellation,
    _claim_mutation_lock,
    _maintenance_lock,
    _release_mutation_lock,
    _unclaim_mutation_lock,
)
from kiro_crew.autonudge_service.model import (
    _MAX_IDLE_SECS,
    _MIN_IDLE_SECS,
    _TERMINAL_BOUND_REASONS,
    AUTONUDGE_STOP_REASON,
    INVALID_BOUNDS_REASON,
    MANUAL_STOP_REASON,
    AutoNudgeStaleBaseline,
    MonitorUpdateConflict,
    NudgeAdmissionRefused,
    NudgeLoop,
    _stopped_row_is_replaceable,
    is_structured_monitor_loop,
    new_goal_token,
    normalize_stopped_detail,
)
from kiro_crew.autonudge_service.subject import infer_monitor, infer_subject
from kiro_crew.monitoring.limits import validate_runtime_secs
from kiro_crew.monitoring.models import MONITOR_STATE_VERSION, MonitorCreationSurface

if TYPE_CHECKING:
    from kiro_crew.autonudge import AutoNudgeService

# The service's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.autonudge")


async def _rollback_trust_after_failed_removal(
    loop: NudgeLoop,
    owner_revocation: Any,
    restore_provider_credentials: bool,
) -> bool:
    """Restore every pre-commit trust mutation before returning an error."""
    from kiro_crew import autonudge_provider_trust, autonudge_selfarm

    cancelled = False
    failure: BaseException | None = None
    if owner_revocation is not None:
        try:
            restored, step_cancelled = await autonudge_selfarm.await_thread_deferring_cancellation(
                autonudge_selfarm.rollback_owner_arm_revocation,
                owner_revocation,
            )
            cancelled = cancelled or step_cancelled
            if not restored:
                failure = MonitorUpdateConflict(
                    "owner admission changed before removal compensation"
                )
        except BaseException as exc:  # finish provider compensation too
            failure = exc
    if restore_provider_credentials:
        state = loop.monitor
        if state is None:
            failure = failure or ValueError(
                "provider credential restoration requires a structured monitor"
            )
        else:
            try:
                _result, step_cancelled = (
                    await autonudge_selfarm.await_thread_deferring_cancellation(
                        autonudge_provider_trust.record_monitor_owner_credentials,
                        loop.id,
                        loop.slot_key,
                        state.kind,
                        state.target,
                    )
                )
                cancelled = cancelled or step_cancelled
            except BaseException as exc:  # owner compensation already ran
                failure = failure or exc
    if failure is not None:
        raise failure
    return cancelled


async def _prepare_trust_before_removal(
    self: AutoNudgeService,
    loop: NudgeLoop,
    *,
    durable_loop_row: Mapping[str, Any] | None = None,
) -> tuple[Any, bool]:
    """Fence owner admission and revoke provider trust before store deletion."""
    from kiro_crew import autonudge_selfarm
    from kiro_crew.members import is_member_session_key

    owner_revocation, cancelled = None, False
    if is_member_session_key(loop.slot_key):
        exact_row = durable_loop_row or self._durable_loop_row(loop)
        owner_revocation, cancelled = await autonudge_selfarm.await_thread_deferring_cancellation(
            autonudge_selfarm.begin_owner_arm_revocation,
            loop.id,
            loop.slot_key,
            exact_row,
        )
    restore_provider_credentials = False
    try:
        restore_provider_credentials = await self._provider_credentials_authorized(loop)
        await self._revoke_provider_credentials_before_removal(loop.id)
    except BaseException:
        rollback_cancelled = await self._rollback_trust_after_failed_removal(
            loop,
            owner_revocation,
            bool(restore_provider_credentials),
        )
        if cancelled or rollback_cancelled:
            raise asyncio.CancelledError
        raise
    if cancelled:
        await self._rollback_trust_after_failed_removal(
            loop,
            owner_revocation,
            bool(restore_provider_credentials),
        )
        raise asyncio.CancelledError
    return owner_revocation, bool(restore_provider_credentials)


async def _commit_owner_revocation(owner_revocation: Any) -> bool:
    """CAS-delete one owner fence after the loop-store write committed."""
    if owner_revocation is None:
        return False
    from kiro_crew import autonudge_selfarm

    committed, cancelled = await autonudge_selfarm.await_thread_deferring_cancellation(
        autonudge_selfarm.commit_owner_arm_revocation,
        owner_revocation,
    )
    if not committed:
        raise MonitorUpdateConflict("owner admission changed before removal committed")
    return cancelled


def _assert_monitor_replacement_mutable(self: AutoNudgeService, loop_id: str) -> None:
    """Refuse any change while owner-credential activation is unfinished."""
    if loop_id in self._deferred_monitor_replacements:
        raise MonitorUpdateConflict("monitor replacement authorization is still being finalized")


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
    replace_existing: bool = True,
    replace_stopped: bool = False,
    self_armed: bool = False,
    loop_id: str | None = None,
    creation_surface: MonitorCreationSurface = MonitorCreationSurface.DASHBOARD,
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
            admission_check=admission_check,
            gate=gate,
            judge=judge,
            replace_existing=replace_existing,
            replace_stopped=replace_stopped,
            self_armed=self_armed,
            loop_id=loop_id,
            creation_surface=creation_surface,
        )
    )
    self._inflight_adds.add(inner)

    def _finish(t: "asyncio.Task[NudgeLoop]") -> None:
        self._inflight_adds.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning("AutoNudge: detached add() failed", exc_info=t.exception())

    inner.add_done_callback(_finish)
    try:
        result, cancelled = await _await_future_deferring_cancellation(inner)
    except BaseException as add_error:
        root_error = (
            add_error.__cause__
            if isinstance(add_error, asyncio.CancelledError) and add_error.__cause__ is not None
            else add_error
        )
        if isinstance(root_error, OSError):
            pending = self._find_by_slot(slot_key)
            if pending is not None and pending.id in self._deferred_monitor_replacements:
                try:
                    await self.rollback_monitor_replacement(pending.id)
                except BaseException as rollback_error:
                    logger.error(
                        "AutoNudge: committed replacement %s could not be rolled back",
                        pending.id,
                        exc_info=rollback_error,
                    )
                    if isinstance(add_error, asyncio.CancelledError):
                        raise asyncio.CancelledError from rollback_error
                    raise rollback_error from root_error
        raise
    if cancelled:
        raise asyncio.CancelledError
    return result


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
    replace_existing: bool = True,
    replace_stopped: bool = False,
    self_armed: bool = False,
    loop_id: str | None = None,
    creation_surface: MonitorCreationSurface = MonitorCreationSurface.DASHBOARD,
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
            replace_existing=replace_existing,
            replace_stopped=replace_stopped,
            self_armed=self_armed,
            loop_id=loop_id,
            creation_surface=creation_surface,
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
    replace_existing: bool = True,
    replace_stopped: bool = False,
    self_armed: bool = False,
    loop_id: str | None = None,
    creation_surface: MonitorCreationSurface = MonitorCreationSurface.DASHBOARD,
) -> NudgeLoop:
    from kiro_crew import autonudge as seams  # read at call time: the facade imports us

    validate_runtime_secs(max_runtime_secs, allow_unbounded=True)
    idle_secs = max(_MIN_IDLE_SECS, min(_MAX_IDLE_SECS, int(idle_secs)))
    transaction_cancelled = False
    finalize_error: BaseException | None = None
    async with self._lock:
        if admission_check is not None and not admission_check():
            raise NudgeAdmissionRefused("session changed before nudge arm committed")
        # One loop per slot — replace any existing loop on this slot.
        # persist=False: the offloaded write below persists the combined
        # removal+add atomically, avoiding a duplicate blocking save here.
        existing = self._find_by_slot(slot_key)
        restore_existing_provider_credentials = False
        existing_owner_revocation: Any = None
        if existing:
            self._assert_monitor_replacement_mutable(existing.id)
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
            if not replace_existing and (existing.active or not replace_stopped):
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
            if existing_monitor is not None and existing_monitor.wake_in_flight:
                raise MonitorUpdateConflict(
                    "existing monitor cannot be replaced while a wake is in flight"
                )
            (
                existing_owner_revocation,
                restore_existing_provider_credentials,
            ) = await self._prepare_trust_before_removal(existing)
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
                infer_monitor(message, now, creation_surface=creation_surface, judge=stored_judge)
                if gate
                else None
            ),
            gate=gate,
            banner=banner,
            self_armed=self_armed,
        )
        self._loops[loop.id] = loop
        # Persist WITHOUT blocking the event loop (no-blocking-call rule:
        # _write_state fsyncs, and a wedged disk must not freeze the
        # gateway). Snapshot under the lock (mutation safety), write on a
        # worker thread, and await it so a persistence failure still
        # propagates to the caller before the loop is reported armed.
        payload = self._serialize_state()
        future = asyncio.get_running_loop().run_in_executor(None, self._write_state, payload)
        try:
            _result, transaction_cancelled = await _await_future_deferring_cancellation(future)
        except BaseException:
            self._loops.pop(loop.id, None)
            if existing is not None:
                self._loops[existing.id] = existing
                await self._rollback_trust_after_failed_removal(
                    existing,
                    existing_owner_revocation,
                    restore_existing_provider_credentials,
                )
                if existing.active:
                    self._arm_from_deadline(existing)
            raise
        if existing is not None:
            try:
                transaction_cancelled = (
                    await self._commit_owner_revocation(existing_owner_revocation)
                    or transaction_cancelled
                )
            except (MonitorUpdateConflict, OSError, asyncio.CancelledError) as error:
                # The durable replacement must stay byte-for-value while trust
                # is unresolved. Deferred-replacement guards keep it unarmed
                # and immutable until this method leaves the service lock.
                self._deferred_monitor_replacements[loop.id] = (
                    deepcopy(existing),
                    deepcopy(loop),
                    restore_existing_provider_credentials,
                    existing_owner_revocation,
                    existing,
                )
                logger.error(
                    "AutoNudge: replacement %s committed but owner admission "
                    "finalization for displaced loop %s failed; replacement "
                    "held unarmed for rollback or startup recovery",
                    loop.id,
                    existing.id,
                    exc_info=True,
                )
                finalize_error = error
            else:
                # Committed: the displaced row is gone from the store, so its
                # self-arm entry is revoked now, not before the write.
                self._revoke_self_arm_for(existing)
                self._emit("removed", existing)
        if finalize_error is None:
            self._arm_from_deadline(loop)
    if finalize_error is not None:
        if transaction_cancelled or isinstance(finalize_error, asyncio.CancelledError):
            raise asyncio.CancelledError from finalize_error
        raise finalize_error
    self._emit("added", loop)
    logger.info("AutoNudge: added loop %s on slot %s (idle=%ds)", loop.id, slot_key, idle_secs)
    if transaction_cancelled:
        raise asyncio.CancelledError
    return loop


async def update(
    self: AutoNudgeService,
    loop_id: str,
    *,
    message: str | None = None,
    idle_secs: int | None = None,
    max_cycles: int | None = None,
    active: bool | None = None,
    max_runtime_secs: int | None = None,
    stopped_reason: str | None = None,
    banner: str | None = None,
    judge: dict | None = None,
    expected_generation: int | None = None,
    expect_fingerprint: str | None = None,
    precondition: Callable[[NudgeLoop], bool] | None = None,
    on_absent: Callable[[], None] | None = None,
    stopped_detail: str | None = None,
) -> NudgeLoop | None:
    """Patch a loop. ``precondition`` is re-taken on the live row under the lock.

    A refused precondition changes nothing and returns ``None``, the same
    "not applied" answer as a missing row; only the stale-wake stop passes one.
    ``on_absent`` is called inside the same hold when the row is missing, so
    that caller can tell a deleted row from a replaced one.

    ``active=True`` on a row stopped with ``invalid_bounds`` is applied only
    when the same patch sets both ``max_cycles`` and ``max_runtime_secs`` to
    ``0``; otherwise the row is returned still inactive. A bounded re-arm of
    such a row is ``add(replace_stopped=True)``.
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
            max_runtime_secs=max_runtime_secs,
            stopped_reason=stopped_reason,
            banner=banner,
            judge=judge,
            expected_generation=expected_generation,
            expect_fingerprint=expect_fingerprint,
            precondition=precondition,
            on_absent=on_absent,
            stopped_detail=stopped_detail,
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
    result, cancelled = await _await_future_deferring_cancellation(inner)
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _update_locked(
    self: AutoNudgeService,
    loop_id: str,
    *,
    message: str | None = None,
    idle_secs: int | None = None,
    max_cycles: int | None = None,
    active: bool | None = None,
    max_runtime_secs: int | None = None,
    stopped_reason: str | None = None,
    banner: str | None = None,
    judge: dict | None = None,
    expected_generation: int | None = None,
    expect_fingerprint: str | None = None,
    precondition: Callable[[NudgeLoop], bool] | None = None,
    on_absent: Callable[[], None] | None = None,
    stopped_detail: str | None = None,
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
            max_runtime_secs=max_runtime_secs,
            stopped_reason=stopped_reason,
            banner=banner,
            judge=judge,
            expected_generation=expected_generation,
            expect_fingerprint=expect_fingerprint,
            precondition=precondition,
            on_absent=on_absent,
            stopped_detail=stopped_detail,
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
    max_runtime_secs: int | None = None,
    stopped_reason: str | None = None,
    banner: str | None = None,
    judge: dict | None = None,
    expected_generation: int | None = None,
    expect_fingerprint: str | None = None,
    precondition: Callable[[NudgeLoop], bool] | None = None,
    on_absent: Callable[[], None] | None = None,
    stopped_detail: str | None = None,
) -> NudgeLoop | None:
    from kiro_crew import autonudge as seams  # read at call time: the facade imports us

    if max_runtime_secs is not None:
        validate_runtime_secs(max_runtime_secs, allow_unbounded=True)
    async with self._lock:
        self._assert_monitor_replacement_mutable(loop_id)
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
        # ONE place resolves the subject, from the two strings this loop now holds.
        # Reached by a changed instruction and by a replaced brief alike, because
        # either can name a different pull request and only the pair says which.
        if rebind_monitor:
            inferred = (
                infer_monitor(loop.message, time.time(), judge=loop.judge) if loop.gate else None
            )
            current = loop.monitor
            # The stored spelling is a canonical shorthand and cannot
            # express a HOST, so kind and target alone would call an edit
            # from an enterprise shorthand to the same public slug
            # "unchanged" and keep polling the wrong server. This is the
            # third of the three places that comparison had to reach; the
            # other two are the post-poll binding and the dedupe identity.
            old_probe = infer_subject(str(previous.get("message") or ""), previous.get("judge"))
            new_probe = infer_subject(loop.message, loop.judge)
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
                and loop.stopped_reason == INVALID_BOUNDS_REASON
                and not (max_cycles == 0 and max_runtime_secs == 0)
            ):
                # The row's bounds cannot be trusted: ``_load`` stamped this
                # reason because a cap, or the anchor a cap is measured against
                # (``created_ts`` for the runtime budget, ``cycle_count`` for the
                # cycle cap), could not be read back and now holds a repaired
                # zero. The row does not record WHICH field that was, so a
                # revival that leaves any bound to the stored values, or measures
                # a supplied finite cap against a stored anchor, may run
                # unlimited while its cap reads as intact (a zero anchor never
                # trips the budget). Only a patch that lifts BOTH caps explicitly
                # reads no stored bound at all -- that is the owner's deliberate
                # unlimited choice, and it is the one revival admitted here. A
                # finite re-arm goes through ``add(replace_stopped=True)``, which
                # builds a fresh row with fresh anchors; the reason is replaceable
                # for exactly that. Refused the same way as a settled monitor:
                # inactive, no deadline, reason kept so the readers still say why.
                logger.info(
                    "AutoNudge: loop %s keeps its invalid_bounds stop — a revival "
                    "that does not lift both caps would run it on repaired bounds",
                    loop.id,
                )
                loop.next_due_ts = 0.0
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
            # already inactive. The reverse order is already safe — a
            # manual pause overwriting a bound tag only ever NARROWS
            # revivability ("manual" never auto-revives).
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
            elif stopped_reason in _TERMINAL_BOUND_REASONS and not active and not loop.active:
                logger.info(
                    "AutoNudge: loop %s already deactivated (%s) — %s bound " "not overwriting it",
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
                    loop.stopped_detail = ""
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
                        # Same rule, same reason: the streak is evidence
                        # about a PAST run, and a revival starts a fresh one.
                        loop.consecutive_start_failures = 0
                else:
                    loop.stopped_reason = stopped_reason or MANUAL_STOP_REASON
                    # A stop with no words of its own leaves the field
                    # empty rather than carrying an earlier stop's text.
                    loop.stopped_detail = normalize_stopped_detail(stopped_detail)
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
    """Remove a loop, fencing owner admission across the store commit."""
    self._assert_monitor_replacement_mutable(loop_id)
    loop = self._loops.get(loop_id)
    owner_revocation: Any = None
    restore_provider_credentials = False
    if persist and loop is not None:
        from kiro_crew import autonudge_provider_trust, autonudge_selfarm
        from kiro_crew.members import is_member_session_key

        if is_member_session_key(loop.slot_key):
            owner_revocation = autonudge_selfarm.begin_owner_arm_revocation(
                loop.id,
                loop.slot_key,
                self._durable_loop_row(loop),
            )
        state = loop.monitor
        try:
            if state is not None:
                restore_provider_credentials = (
                    autonudge_provider_trust.is_monitor_owner_credentials_recorded(
                        loop.id,
                        loop.slot_key,
                        state.kind,
                        state.target,
                    )
                )
            autonudge_provider_trust.forget_monitor_owner_credentials(loop.id)
        except BaseException:
            if owner_revocation is not None:
                restored = autonudge_selfarm.rollback_owner_arm_revocation(owner_revocation)
                if not restored:
                    raise MonitorUpdateConflict(
                        "owner admission changed before removal compensation"
                    )
            raise
    loop = self._loops.pop(loop_id, None)
    if loop is None:
        return None
    self._cancel_timer(loop_id)
    self._rearm_fail_count.pop(loop_id, None)
    self._start_failure_deferred.pop(loop_id, None)
    self._rearm_pending.discard(loop_id)
    self._accepted_monitor_turns.pop(loop_id, None)
    if persist:
        try:
            self._save()
        except BaseException:
            self._loops[loop.id] = loop
            failure: BaseException | None = None
            if owner_revocation is not None:
                try:
                    if not autonudge_selfarm.rollback_owner_arm_revocation(owner_revocation):
                        failure = MonitorUpdateConflict(
                            "owner admission changed before removal compensation"
                        )
                except BaseException as exc:
                    failure = exc
            if restore_provider_credentials:
                assert loop.monitor is not None
                try:
                    autonudge_provider_trust.record_monitor_owner_credentials(
                        loop.id,
                        loop.slot_key,
                        loop.monitor.kind,
                        loop.monitor.target,
                    )
                except BaseException as exc:
                    failure = failure or exc
            if loop.active:
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    pass
                else:
                    self._arm_from_deadline(loop)
            if failure is not None:
                raise failure
            raise
        if owner_revocation is not None and not autonudge_selfarm.commit_owner_arm_revocation(
            owner_revocation
        ):
            raise MonitorUpdateConflict("owner admission changed before removal committed")
        # Self-arm entries retain the historical after-commit cleanup.
        self._revoke_self_arm_for(loop)
    if emit:
        self._emit("removed", loop)
    deferred_replacement = self._deferred_monitor_replacements.pop(loop_id, None)
    if deferred_replacement is not None and deferred_replacement[0] is not None:
        deferred_prior = deferred_replacement[0]
        assert deferred_prior is not None
        prior_owner_revocation = deferred_replacement[3]
        if prior_owner_revocation is not None:
            from kiro_crew import autonudge_selfarm

            if not autonudge_selfarm.commit_owner_arm_revocation(prior_owner_revocation):
                raise MonitorUpdateConflict(
                    "owner admission changed before deferred removal committed"
                )
        self._revoke_self_arm_for(deferred_prior)
    return loop


def _revoke_self_arm_for(self: AutoNudgeService, loop: NudgeLoop) -> None:
    """Finish post-commit cleanup without trusting ``self_armed``.

    OWNER admission is revoked strictly before the durable store deletion.
    This legacy cleanup still runs for every removed id because the
    agent-writable ``self_armed`` bit cannot classify the trust entry: a
    forged ``false`` must not let a self-arm entry survive for an id-reusing
    forgery. For an owner entry already revoked, the operation is an
    idempotent no-op; provider-trust cleanup is idempotent too.
    """
    self._revoke_self_arm(loop.id)


def _revoke_self_arm(loop_id: str) -> None:
    """Finish best-effort self-arm and provider cleanup after store commit.

    Owner admission is deliberately absent from this boundary: it was
    revoked strictly and joined before the store mutation. This callback
    remains best-effort for self-arm entries, whose historical contract is
    cleanup after their durable loop row is gone. File IO is offloaded when
    an event loop is running; the sync fallback covers shutdown and tests.
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
        self._assert_monitor_replacement_mutable(loop_id)
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
        owner_revocation: Any = None
        if existed:
            assert current is not None
            durable_loop_row = self._durable_loop_row(current)
            was_active = current.active
            current.active = False
            self._cancel_timer(loop_id)
            try:
                (
                    owner_revocation,
                    restore_provider_credentials,
                ) = await self._prepare_trust_before_removal(
                    current,
                    durable_loop_row=durable_loop_row,
                )
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
        fut = asyncio.get_running_loop().run_in_executor(None, self._write_state, payload)

        async def _restore_failed_removal() -> bool:
            self._store.pending_removals.discard(loop_id)
            self._store.stop_notes.pop(loop_id, None)
            if removed_loop is None:
                return False
            self._loops[loop_id] = removed_loop
            cancelled = await self._rollback_trust_after_failed_removal(
                removed_loop,
                owner_revocation,
                restore_provider_credentials,
            )
            if removed_loop.active:
                self._arm_from_deadline(removed_loop)
            return cancelled

        try:
            _result, write_cancelled = await _await_future_deferring_cancellation(fut)
        except BaseException:
            # Persistence is the commit point. Restore the live row, trust,
            # and timer so an immediate retry sees the durable prior state.
            await _restore_failed_removal()
            raise
        try:
            self._store.pending_removals.discard(loop_id)
            commit_cancelled = await self._commit_owner_revocation(owner_revocation)
            if removed_loop is not None:
                # The store committed and the owner fence is now gone. Self-arm
                # trust retains its historical post-commit cleanup.
                self._revoke_self_arm_for(removed_loop)
                self._emit("removed", removed_loop)
            if write_cancelled or commit_cancelled:
                raise asyncio.CancelledError
            return True
        finally:
            # The write settled or rolled back; its detail must not leak.
            self._store.stop_notes.pop(loop_id, None)


async def _revoke_provider_credentials_before_removal(loop_id: str) -> None:
    """Require a durable provider denial before an agent-writable row disappears."""
    from kiro_crew import autonudge_provider_trust, autonudge_selfarm

    await autonudge_selfarm.await_thread_to_completion(
        autonudge_provider_trust.forget_monitor_owner_credentials,
        loop_id,
    )


async def _provider_credentials_authorized(loop: NudgeLoop) -> bool:
    """Whether this exact structured row currently owns provider credentials."""
    from kiro_crew import autonudge_provider_trust, autonudge_selfarm

    state = loop.monitor
    if state is None:
        return False
    return bool(
        await autonudge_selfarm.await_thread_to_completion(
            autonudge_provider_trust.is_monitor_owner_credentials_recorded,
            loop.id,
            loop.slot_key,
            state.kind,
            state.target,
        )
    )


async def _restore_provider_credentials(loop: NudgeLoop) -> None:
    """Restore the exact grant for a row whose replacement did not commit."""
    from kiro_crew import autonudge_provider_trust, autonudge_selfarm

    state = loop.monitor
    if state is None:
        raise ValueError("provider credential restoration requires a structured monitor")
    await autonudge_selfarm.await_thread_to_completion(
        autonudge_provider_trust.record_monitor_owner_credentials,
        loop.id,
        loop.slot_key,
        state.kind,
        state.target,
    )
