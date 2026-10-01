"""The structured-monitor transitions the typed controller drives.

Each transition stages a copy of the loop, persists the complete replacement snapshot
while holding the service lock, and only then publishes it onto the live object, so a
restart can never restore state a reader was not shown or the reverse. The accepted-
turn correlation bounds a terminal record's wake claim until completion evidence
arrives or its window expires, and a replacement whose trust grant is still being
authorized keeps the prior row restorable until it commits.

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
from copy import deepcopy
from dataclasses import fields
from typing import TYPE_CHECKING, Callable

from kiro_crew.autonudge_service.maintenance import _maintenance_lock
from kiro_crew.autonudge_service.model import (
    _MAX_IDLE_SECS,
    _MIN_IDLE_SECS,
    MonitorUpdateConflict,
    NudgeAdmissionRefused,
    NudgeLoop,
    _stopped_row_is_replaceable,
    new_goal_token,
    terminal_notification_delivery_matches,
)
from kiro_crew.autonudge_service.store import _STORE_VERSION
from kiro_crew.autonudge_service.timers import (
    _MONITOR_RETRY_BACKOFF_SECS,
    _MONITOR_RETRY_MAX_BACKOFF_SECS,
    _REARM_BACKOFF_MAX_SHIFT,
)
from kiro_crew.monitoring.decision import (
    decide_monitor,
    monitor_budget_reason,
    monitor_stall_reason,
    stamp_monitor_alerted,
)
from kiro_crew.monitoring.github_provider_errors import is_unattempted_probe
from kiro_crew.monitoring.limits import validate_runtime_secs
from kiro_crew.monitoring.models import (
    MONITOR_BUSY_RETRY_SECS,
    MONITOR_COMPLETION_EVIDENCE_TIMEOUT_SECS,
    MONITOR_STATE_VERSION,
    MONITOR_STOP_APPROVAL_STALL,
    MONITOR_STOP_COMPLETION_UNAVAILABLE,
    MONITOR_STOP_SESSION_CLOSE,
    MONITOR_STOP_SESSION_UNAVAILABLE,
    MONITOR_STOP_USER,
    MonitorActionCompletion,
    MonitorActionDisposition,
    MonitorBudgets,
    MonitorCreationSurface,
    MonitorDecision,
    MonitorDispatchResult,
    MonitorObservationStatus,
    MonitorOutcome,
    MonitorProbeResult,
    MonitorState,
    MonitorVerdict,
)
from kiro_crew.monitoring.registry import kind_supports_objective

if TYPE_CHECKING:
    from kiro_crew.autonudge import AutoNudgeService

# The service's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.autonudge")


async def add_monitor(
    self: AutoNudgeService,
    *,
    slot_key: str,
    kind: str,
    target: str,
    objective: str,
    cadence_secs: int,
    budgets: MonitorBudgets,
    wake_instructions: str = "",
    now: float | None = None,
    replace_existing: bool = True,
    replace_stopped: bool = False,
    expected_existing_monitor_id: str | None = None,
    expected_existing_config_generation: int | None = None,
    admission_check: Callable[[], bool] | None = None,
    self_armed: bool = False,
    loop_id: str | None = None,
    defer_replaced_trust_revocation: bool = False,
    creation_surface: MonitorCreationSurface = MonitorCreationSurface.DASHBOARD,
) -> NudgeLoop:
    """Create one durable structured record without legacy prompt routing."""
    inner: "asyncio.Task[NudgeLoop]" = asyncio.ensure_future(
        self._add_monitor_locked(
            slot_key=slot_key,
            kind=kind,
            target=target,
            objective=objective,
            cadence_secs=cadence_secs,
            budgets=budgets,
            wake_instructions=wake_instructions,
            now=now,
            replace_existing=replace_existing,
            replace_stopped=replace_stopped,
            expected_existing_monitor_id=expected_existing_monitor_id,
            expected_existing_config_generation=expected_existing_config_generation,
            admission_check=admission_check,
            self_armed=self_armed,
            loop_id=loop_id,
            defer_replaced_trust_revocation=defer_replaced_trust_revocation,
            creation_surface=creation_surface,
        )
    )
    self._inflight_adds.add(inner)

    def _finish(t: "asyncio.Task[NudgeLoop]") -> None:
        self._inflight_adds.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning("detached structured monitor add failed", exc_info=t.exception())

    inner.add_done_callback(_finish)
    return await asyncio.shield(inner)


async def _add_monitor_locked(
    self: AutoNudgeService,
    *,
    slot_key: str,
    kind: str,
    target: str,
    objective: str,
    cadence_secs: int,
    budgets: MonitorBudgets,
    wake_instructions: str,
    now: float | None,
    replace_existing: bool,
    replace_stopped: bool = False,
    expected_existing_monitor_id: str | None,
    expected_existing_config_generation: int | None,
    admission_check: Callable[[], bool] | None,
    self_armed: bool = False,
    loop_id: str | None = None,
    defer_replaced_trust_revocation: bool = False,
    creation_surface: MonitorCreationSurface,
) -> NudgeLoop:
    created = time.time() if now is None else now
    cadence = max(_MIN_IDLE_SECS, min(_MAX_IDLE_SECS, int(cadence_secs)))
    async with _maintenance_lock(self._base_dir):
        async with self._lock:
            if admission_check is not None and not admission_check():
                raise NudgeAdmissionRefused("session changed before monitor arm committed")
            existing = self._find_by_slot(slot_key)
            if expected_existing_monitor_id is not None:
                existing_monitor = existing.monitor if existing is not None else None
                if (
                    existing is None
                    or existing.id != expected_existing_monitor_id
                    or existing_monitor is None
                    or existing_monitor.config_generation != expected_existing_config_generation
                ):
                    raise MonitorUpdateConflict("monitor changed before restart")
            if existing:
                # Same split as the legacy add: create-only refuses ANY
                # record unless the caller opted into ``replace_stopped``,
                # which under the owner's ruling displaces only
                # SYSTEM-imposed stops (a spent bound, a finished subject).
                # A consumer-recorded stop — including a USER_STOP record
                # retained by monitor_stop — is preserved; the dashboard
                # restart route (conditional replace) is the sanctioned way
                # to succeed one. Dashboard creates never opt in, so their
                # any-record 409 keeps retained evidence intact. The
                # wake-in-flight guard below still covers a terminal record
                # that owns an accepted, uncompleted wake.
                if not replace_existing and (existing.active or not replace_stopped):
                    raise MonitorUpdateConflict("session already has an automation")
                existing_monitor = existing.monitor
                if (
                    not replace_existing
                    and existing_monitor is not None
                    and existing_monitor.version != MONITOR_STATE_VERSION
                ):
                    # Same rule as the legacy add: a future-version record
                    # is a newer gateway's state, retained inactive across a
                    # downgrade on purpose — never deletable by this one.
                    raise MonitorUpdateConflict(
                        "the session's stopped automation was written by a newer "
                        "gateway and cannot be replaced by this one"
                    )
                if (
                    not replace_existing
                    and replace_stopped
                    and not _stopped_row_is_replaceable(existing)
                ):
                    # Same owner ruling as the legacy add: consumer-recorded
                    # stops (USER_STOP, SESSION_CLOSE, tombstones) are
                    # evidence, never displaced by a re-arm.
                    raise MonitorUpdateConflict(
                        "the session's stopped automation is retained as evidence "
                        "and is not replaceable by a re-arm; its owner must clear "
                        "it first from the dashboard's goal popover"
                    )
                if existing_monitor is not None and existing_monitor.wake_in_flight:
                    raise MonitorUpdateConflict(
                        "existing monitor cannot be replaced while a wake is in flight"
                    )
            due = created + cadence
            # Refused HERE rather than discovered later. This is the one place a
            # caller-supplied kind reaches persistence, and a monitor stored under
            # a kind nothing registered would be probed by whatever provider the
            # controller happens to hold.
            if not kind_supports_objective(kind, objective):
                raise ValueError(f"no monitored kind {kind!r} supports objective {objective!r}")
            validate_runtime_secs(budgets.max_runtime_secs)
            monitor = MonitorState(
                kind=kind,
                target=target,
                objective=objective,
                created_ts=created,
                creation_surface=creation_surface,
                budgets=budgets,
                cadence_secs=cadence,
                wake_instructions=wake_instructions,
                next_probe_at=due,
            )
            loop = NudgeLoop(
                id=self._mint_loop_id(loop_id),
                slot_key=slot_key,
                message="",
                idle_secs=cadence,
                created_ts=created,
                next_due_ts=due,
                goal_token=new_goal_token(),
                monitor=monitor,
                self_armed=self_armed,
            )
            deferred_prior = (
                deepcopy(existing)
                if existing is not None and defer_replaced_trust_revocation
                else None
            )
            restore_prior_provider_credentials = False
            if existing is not None and defer_replaced_trust_revocation:
                restore_prior_provider_credentials = await self._provider_credentials_authorized(
                    existing
                )
                await self._revoke_provider_credentials_before_removal(existing.id)
            replacement_payload = {
                "version": _STORE_VERSION,
                "loops": self._serialized_loops(
                    skip={existing.id} if existing is not None else None,
                    extra=[loop],
                ),
            }
            try:
                await self._write_monitor_snapshot_locked(replacement_payload)
            except BaseException:
                if restore_prior_provider_credentials:
                    assert existing is not None
                    await self._restore_provider_credentials(existing)
                raise
            if existing is not None:
                self.remove_sync(existing.id, persist=False)
                if defer_replaced_trust_revocation:
                    self._deferred_monitor_replacements[loop.id] = (
                        deferred_prior,
                        deepcopy(loop),
                        restore_prior_provider_credentials,
                    )
                else:
                    # The snapshot above already committed the replacement.
                    self._revoke_self_arm_for(existing)
            elif defer_replaced_trust_revocation:
                self._deferred_monitor_replacements[loop.id] = (
                    None,
                    deepcopy(loop),
                    False,
                )
            self._loops[loop.id] = loop
            if self._on_monitor_tick is not None:
                self._arm_from_deadline(loop)
    self._emit("added", loop)
    return loop


def commit_monitor_replacement(self: AutoNudgeService, loop_id: str) -> None:
    """Release prior trust after a replacement's authorization commits."""
    if loop_id not in self._deferred_monitor_replacements:
        raise MonitorUpdateConflict("monitor replacement is no longer pending")
    prior, _replacement, _restore_prior_provider_credentials = (
        self._deferred_monitor_replacements.pop(loop_id)
    )
    if prior is not None:
        self._revoke_self_arm_for(prior)


async def rollback_monitor_replacement(self: AutoNudgeService, loop_id: str) -> bool:
    """Restore the pre-replacement row after authorization cannot commit.

    The failed row is removed and the prior row is written in one snapshot.
    ``False`` means another mutation already consumed the pending replacement,
    so the rollback deliberately leaves that newer state untouched.
    """
    removed: NudgeLoop | None = None
    prior: NudgeLoop | None = None
    async with _maintenance_lock(self._base_dir):
        async with self._lock:
            if loop_id not in self._deferred_monitor_replacements:
                return False
            prior, replacement, restore_prior_provider_credentials = (
                self._deferred_monitor_replacements.pop(loop_id)
            )
            current = self._loops.get(loop_id)
            if current is None or self._serialize_loop(current) != self._serialize_loop(
                replacement
            ):
                if prior is not None:
                    self._revoke_self_arm_for(prior)
                return False
            payload = {
                "version": _STORE_VERSION,
                "loops": self._serialized_loops(
                    skip={loop_id},
                    extra=[prior] if prior is not None else None,
                ),
            }
            try:
                await self._write_monitor_snapshot_locked(payload)
            except BaseException:
                self._deferred_monitor_replacements[loop_id] = (
                    prior,
                    replacement,
                    restore_prior_provider_credentials,
                )
                raise
            removed = self.remove_sync(loop_id, persist=False, emit=False)
            if prior is not None:
                self._loops[prior.id] = prior
                if restore_prior_provider_credentials:
                    await self._restore_provider_credentials(prior)
                if prior.active and self._on_monitor_tick is not None:
                    self._arm_from_deadline(prior)
    if removed is not None:
        self._emit("removed", removed)
    if prior is not None:
        self._emit("added", prior)
    return True


def _monitor_snapshot_with_replacement(
    self: AutoNudgeService,
    loop: NudgeLoop,
    replacement: NudgeLoop,
) -> dict:
    """Serialize one staged monitor replacement without changing live state."""
    return {
        "version": _STORE_VERSION,
        "loops": self._serialized_loops(replace={loop.id: replacement}),
    }


def _apply_staged_monitor(self: AutoNudgeService, loop: NudgeLoop, staged: NudgeLoop) -> None:
    """Publish a durable staged transition while preserving live object identity."""
    state = loop.monitor
    staged_state = staged.monitor
    if state is None or staged_state is None:
        raise ValueError("structured monitor replacement requires monitor state")
    for loop_field in fields(NudgeLoop):
        if loop_field.name != "monitor":
            setattr(loop, loop_field.name, deepcopy(getattr(staged, loop_field.name)))
    for state_field in fields(MonitorState):
        setattr(
            state,
            state_field.name,
            deepcopy(getattr(staged_state, state_field.name)),
        )


async def _persist_staged_monitor_locked(
    self: AutoNudgeService,
    loop: NudgeLoop,
    staged: NudgeLoop,
) -> None:
    """Persist a complete replacement before publishing it to live readers."""
    payload = self._monitor_snapshot_with_replacement(loop, staged)
    try:
        await self._write_monitor_snapshot_locked(payload)
    except asyncio.CancelledError:
        # The snapshot writer propagates cancellation only after draining
        # the executor write. Publish the state that is already durable
        # before preserving the caller's cancellation.
        self._apply_staged_monitor(loop, staged)
        raise
    self._apply_staged_monitor(loop, staged)


async def apply_monitor_probe(
    self: AutoNudgeService,
    monitor_id: str,
    result: MonitorProbeResult,
    *,
    now: float,
    config_generation: int,
) -> MonitorVerdict:
    """Persist one probe decision and any wake claim as one transition.

    A refusal that never reaches the decision engine carries no entries: no
    observation was judged, so the verdict has nothing to name.
    """
    async with self._lock:
        loop = self._loops.get(monitor_id)
        state = loop.monitor if loop is not None else None
        if loop is None or state is None or not loop.active or state.outcome is not None:
            return MonitorVerdict(decision=MonitorDecision.STOP_BLOCKED)
        staged = deepcopy(loop)
        staged_state = staged.monitor
        assert staged_state is not None
        if state.config_generation != config_generation:
            self._set_monitor_deadline(staged, now + staged_state.cadence_secs)
            decision = MonitorDecision.NO_CHANGE
            verdict = MonitorVerdict(decision=decision)
        elif state.wake_in_flight:
            return MonitorVerdict(decision=MonitorDecision.NO_CHANGE)
        else:
            verdict = decide_monitor(staged_state, result.observation, now=now)
            decision = verdict.decision
            staged_state.probe_count += 1
            staged_state.last_probe_at = now
            staged_state.last_decision = decision
            observation = result.observation
            staged_state.last_observation_status = observation.status
            staged_state.last_observation_reason_code = observation.reason_code
            provider_error = observation.provider_error or observation.supplemental_provider_error
            if is_unattempted_probe(observation):
                # THE THIRD OUTCOME, and it moves neither counter, for the same
                # reason ``shadow.apply_monitor_probe`` gives: the provider-error
                # budget is finite and never refunded, so it has to measure
                # refusals the HOST gave this watch. Charged for a request the
                # probe declined to send, a shared cooldown that unrelated work
                # opened retires a healthy watch on its own cadence; clearing the
                # streak instead is the opposite error, because an outage
                # interleaved with skips would never retire the watch it blinds.
                # This is the PRODUCTION counting site, so the rule has to hold
                # in both or it holds nowhere.
                pass
            elif provider_error is not None:
                staged_state.provider_error_count += 1
                staged_state.consecutive_provider_errors += 1
                staged_state.last_provider_error = provider_error
            else:
                staged_state.consecutive_provider_errors = 0
                staged_state.last_provider_error = None
            if observation.status is not MonitorObservationStatus.PROVIDER_ERROR:
                staged_state.last_observation = deepcopy(result.canonical)
                staged_state.last_fingerprint = observation.fingerprint
                staged_state.last_observed_at = now

            if decision in {MonitorDecision.NO_CHANGE, MonitorDecision.RECORD_ONLY}:
                self._set_monitor_deadline(staged, now + staged_state.cadence_secs)
            elif decision is MonitorDecision.RETRY_PROVIDER:
                shift = max(0, staged_state.consecutive_provider_errors - 1)
                retry = min(
                    _MONITOR_RETRY_MAX_BACKOFF_SECS,
                    _MONITOR_RETRY_BACKOFF_SECS * (2 ** min(shift, _REARM_BACKOFF_MAX_SHIFT)),
                    staged_state.cadence_secs,
                )
                self._set_monitor_deadline(staged, now + retry)
            elif decision is MonitorDecision.WAKE_ACTIONABLE:
                staged_state.last_wake_fingerprint = observation.fingerprint
                staged_state.last_wake_reason_code = observation.reason_code
                # Record that a wake was DECIDED for the conditions it
                # delivers, next to the persist so the stamp cannot outlive
                # its write: the re-alert interval is measured from here.
                # decide_monitor only READS this map. The engine owns which
                # conditions the wake covered, so it owns the keying too.
                stamp_monitor_alerted(staged_state, now=now)
                staged_state.wake_in_flight = True
                staged_state.wake_delivery = None
                self._set_monitor_deadline(staged, 0.0)
            elif decision is MonitorDecision.STOP_BUDGET:
                reason = monitor_budget_reason(staged_state, now=now)
                self._apply_monitor_budget_stop(staged, reason, stopped_at=now)
            else:
                staged.active = False
                self._set_monitor_deadline(staged, 0.0)
                staged_state.outcome = (
                    MonitorOutcome.SUCCESS
                    if decision is MonitorDecision.STOP_SUCCESS
                    else MonitorOutcome.BLOCKED
                )
                staged_state.stopped_reason = (
                    monitor_stall_reason(staged_state, now=now)
                    or observation.reason_code
                    or "monitor_blocked"
                )
                staged_state.stopped_at = now
        # Every probe advances durable inspection state and the next
        # deadline. Persist before publishing so a restart cannot restore
        # an overdue schedule and repeat an unchanged probe early.
        await self._persist_staged_monitor_locked(loop, staged)
        if not loop.active:
            self._sync_terminal_completion_timer(loop)
    self._emit("updated", loop)
    return verdict


async def stop_monitor_if_budget_exhausted(
    self: AutoNudgeService,
    monitor_id: str,
    *,
    now: float,
) -> bool:
    """Stop a spent structured monitor before starting another provider probe."""
    stopped_loop: NudgeLoop | None = None
    async with self._lock:
        loop = self._loops.get(monitor_id)
        state = loop.monitor if loop is not None else None
        if loop is not None and state is not None and loop.active and state.outcome is None:
            reason = monitor_budget_reason(state, now=now)
            if reason:
                stopped = deepcopy(loop)
                if stopped.monitor is not None:
                    stopped.monitor.last_decision = MonitorDecision.STOP_BUDGET
                self._apply_monitor_budget_stop(stopped, reason, stopped_at=now)
                await self._persist_staged_monitor_locked(loop, stopped)
                self._sync_terminal_completion_timer(loop)
                stopped_loop = loop
    if stopped_loop is not None:
        self._emit("updated", stopped_loop)
    return stopped_loop is not None


def _set_monitor_deadline(self: AutoNudgeService, loop: NudgeLoop, deadline: float) -> None:
    """Write the scheduler authority and inspection mirror together."""
    loop.next_due_ts = deadline
    if loop.monitor is not None:
        loop.monitor.next_probe_at = deadline


async def stop_monitor(
    self: AutoNudgeService,
    monitor_id: str,
    *,
    now: float | None = None,
    user_reason: str = "",
) -> NudgeLoop | None:
    """Retain a structured record with a durable user-stop outcome."""
    stopped_at = time.time() if now is None else now
    async with self._lock:
        loop = self._loops.get(monitor_id)
        state = loop.monitor if loop is not None else None
        if loop is None or state is None:
            return None
        if state.outcome is not None:
            return loop
        stopped = deepcopy(loop)
        self._apply_monitor_user_stop(stopped, stopped_at=stopped_at)
        assert stopped.monitor is not None
        stopped.monitor.user_stop_reason = user_reason
        # Keep the live state and timer untouched until the terminal
        # snapshot is durable. A failed write must leave memory matching
        # the still-active record on disk so restart cannot resurrect work
        # the current process already considers stopped.
        await self._persist_staged_monitor_locked(loop, stopped)
        self._sync_terminal_completion_timer(loop)
    self._emit("updated", loop)
    return loop


async def mark_terminal_notification_delivered(
    self: AutoNudgeService,
    monitor_id: str,
    outcome: MonitorOutcome,
    stopped_at: float,
) -> bool:
    """Persist delivery only when the same terminal generation still exists."""
    async with self._lock:
        loop = self._loops.get(monitor_id)
        state = loop.monitor if loop is not None else None
        if (
            loop is None
            or state is None
            or loop.active
            or state.outcome is not outcome
            or state.stopped_at != stopped_at
            or terminal_notification_delivery_matches(loop, outcome, stopped_at)
        ):
            return False
        staged = deepcopy(loop)
        staged_state = staged.monitor
        assert staged_state is not None
        staged_state.terminal_notification_delivered = True
        staged.terminal_notification_outcome = outcome.value
        staged.terminal_notification_stopped_at = stopped_at
        await self._persist_staged_monitor_locked(loop, staged)
    return True


async def retire_monitor_for_session_close(
    self: AutoNudgeService, monitor_id: str, *, now: float | None = None
) -> NudgeLoop | None:
    """Retain a terminal session-close record while disarming its timer."""
    stopped_at = time.time() if now is None else now
    async with self._lock:
        loop = self._loops.get(monitor_id)
        state = loop.monitor if loop is not None else None
        if loop is None or state is None:
            return None
        if state.outcome is not None:
            return loop
        staged = deepcopy(loop)
        staged_state = staged.monitor
        assert staged_state is not None
        staged.active = False
        self._retain_accepted_terminal_completion(staged, stopped_at=stopped_at)
        staged_state.outcome = MonitorOutcome.SESSION_CLOSE
        staged_state.stopped_reason = MONITOR_STOP_SESSION_CLOSE
        staged_state.stopped_at = stopped_at
        await self._persist_staged_monitor_locked(loop, staged)
        self._sync_terminal_completion_timer(loop)
    self._emit("updated", loop)
    return loop


async def restore_monitor_after_failed_session_close(
    self: AutoNudgeService,
    monitor_id: str,
    *,
    now: float | None = None,
    admission_check: Callable[[], bool] | None = None,
) -> NudgeLoop | None:
    """Rollback only the close-owned terminal transition after close failure."""
    restored_at = time.time() if now is None else now
    async with self._lock:
        loop = self._loops.get(monitor_id)
        state = loop.monitor if loop is not None else None
        if loop is None or state is None or state.outcome is not MonitorOutcome.SESSION_CLOSE:
            return None
        if admission_check is not None and not admission_check():
            return None
        staged = deepcopy(loop)
        staged_state = staged.monitor
        assert staged_state is not None
        staged.active = True
        staged_state.outcome = None
        staged_state.stopped_reason = ""
        staged_state.stopped_at = 0.0
        if (
            staged_state.wake_in_flight
            and staged_state.wake_delivery is MonitorDispatchResult.DISPATCHED
            and staged_state.completion_evidence_deadline > 0
        ):
            deadline = staged_state.completion_evidence_deadline
        elif staged_state.wake_in_flight:
            staged_state.wake_delivery = MonitorDispatchResult.BUSY
            staged_state.completion_evidence_deadline = 0.0
            deadline = restored_at + min(
                MONITOR_BUSY_RETRY_SECS,
                staged_state.cadence_secs,
            )
        else:
            deadline = restored_at + staged_state.cadence_secs
        self._set_monitor_deadline(staged, deadline)
        await self._persist_staged_monitor_locked(loop, staged)
        if self._on_monitor_tick is not None:
            self._arm_from_deadline(loop)
    self._emit("updated", loop)
    return loop


async def update_monitor(
    self: AutoNudgeService,
    monitor_id: str,
    *,
    target: str | None = None,
    objective: str | None = None,
    cadence_secs: int | None = None,
    budgets: MonitorBudgets | None = None,
    budget_patch: dict[str, int] | None = None,
    wake_instructions: str | None = None,
    creation_surface: MonitorCreationSurface | None = None,
    _prior_snapshot_out: list[NudgeLoop] | None = None,
) -> NudgeLoop | None:
    """Patch an active structured record without implicit revival."""
    if budgets is not None and budget_patch is not None:
        raise ValueError("budgets and budget_patch are mutually exclusive")
    async with self._lock:
        loop = self._loops.get(monitor_id)
        state = loop.monitor if loop is not None else None
        if loop is None or state is None or state.outcome is not None:
            return None
        reset_baseline = (target is not None and target != state.target) or (
            objective is not None and objective != state.objective
        )
        if reset_baseline and state.wake_in_flight:
            raise MonitorUpdateConflict(
                "target or objective cannot change while a wake is in flight"
            )
        if _prior_snapshot_out is not None:
            _prior_snapshot_out.append(deepcopy(loop))
        staged = deepcopy(loop)
        staged_state = staged.monitor
        assert staged_state is not None
        if target is not None:
            staged_state.target = target
        if creation_surface is not None:
            staged_state.creation_surface = creation_surface
        if objective is not None:
            # The objective allowlist upstream is a union across every publicly
            # armable kind, so it is a first filter and never the whole check.
            # This is the boundary that knows BOTH halves -- the monitor's kind is
            # already fixed -- so the pairing is refused here rather than
            # discovered at probe time.
            if not kind_supports_objective(staged_state.kind, objective):
                raise ValueError(
                    f"monitored kind {staged_state.kind!r} does not support "
                    f"objective {objective!r}"
                )
            staged_state.objective = objective
        if cadence_secs is not None:
            cadence = max(_MIN_IDLE_SECS, min(_MAX_IDLE_SECS, int(cadence_secs)))
            staged_state.cadence_secs = cadence
            staged.idle_secs = cadence
            if staged.active and not staged_state.wake_in_flight and staged.next_due_ts > 0:
                self._set_monitor_deadline(staged, time.time() + cadence)
        if budget_patch is not None:
            budget_fields = {
                "max_runtime_secs",
                "max_agent_turns",
                "max_tokens",
                "max_provider_errors",
            }
            unknown = set(budget_patch) - budget_fields
            if unknown:
                raise ValueError(
                    "unknown structured monitor budget fields: " + ", ".join(sorted(unknown))
                )
            values = {field: getattr(staged_state.budgets, field) for field in budget_fields}
            values.update(budget_patch)
            staged_state.budgets = MonitorBudgets(**values)
        elif budgets is not None:
            staged_state.budgets = budgets
        if budgets is not None or (budget_patch is not None and "max_runtime_secs" in budget_patch):
            validate_runtime_secs(staged_state.budgets.max_runtime_secs)
        if wake_instructions is not None:
            staged_state.wake_instructions = wake_instructions
        if reset_baseline:
            staged_state.config_generation += 1
            staged_state.last_observation = {}
            staged_state.last_observation_status = None
            staged_state.last_observation_reason_code = ""
            staged_state.last_fingerprint = ""
            staged_state.last_observed_at = 0.0
            staged_state.last_decision = None
            staged_state.last_wake_fingerprint = ""
            staged_state.last_wake_reason_code = ""
            staged_state.wake_in_flight = False
            staged_state.wake_delivery = None
            staged_state.completion_evidence_deadline = 0.0
            staged_state.last_completion_fingerprint = ""
            staged_state.consecutive_provider_errors = 0
            staged_state.last_provider_error = None
            staged_state.coalesce_windows = {}
            staged_state.coalesce_alerted = {}
            staged_state.stall_digest = ""
            staged_state.stall_streak = 0
            staged_state.stall_started_at = 0.0
        await self._persist_staged_monitor_locked(loop, staged)
        if loop.active and not state.wake_in_flight and loop.id not in self._firing:
            self._arm_from_deadline(loop)
    self._emit("updated", loop)
    return loop


async def rollback_monitor_update(
    self: AutoNudgeService,
    monitor_id: str,
    prior: NudgeLoop,
    failed_update: NudgeLoop,
) -> bool:
    """Restore an update only while the failed state is still current."""
    async with self._lock:
        loop = self._loops.get(monitor_id)
        if (
            loop is None
            or loop.monitor is None
            or prior.id != monitor_id
            or failed_update.id != monitor_id
            or self._serialize_loop(loop) != self._serialize_loop(failed_update)
        ):
            return False
        await self._persist_staged_monitor_locked(loop, deepcopy(prior))
        state = loop.monitor
        assert state is not None
        if loop.active and not state.wake_in_flight and loop.id not in self._firing:
            self._arm_from_deadline(loop)
    self._emit("updated", loop)
    return True


async def mark_monitor_action_in_flight(
    self: AutoNudgeService,
    monitor_id: str,
    fingerprint: str,
    *,
    now: float | None = None,
) -> bool:
    """Persist the dispatch claim for one actionable fingerprint."""
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("fingerprint must be a non-empty string")
    checked_at = time.time() if now is None else now
    if (
        isinstance(checked_at, bool)
        or not isinstance(checked_at, (int, float))
        or not math.isfinite(checked_at)
        or checked_at < 0
    ):
        raise ValueError("now must be a finite non-negative number")
    dispatched = False
    async with self._lock:
        loop = self._loops.get(monitor_id)
        state = loop.monitor if loop is not None else None
        if (
            loop is None
            or state is None
            or not loop.active
            or state.outcome is not None
            or state.wake_in_flight
            or state.last_wake_fingerprint == fingerprint
        ):
            return False
        staged = deepcopy(loop)
        staged_state = staged.monitor
        assert staged_state is not None
        reason = monitor_budget_reason(staged_state, now=checked_at)
        if reason:
            self._apply_monitor_budget_stop(staged, reason, stopped_at=checked_at)
        else:
            staged_state.last_wake_fingerprint = fingerprint
            staged_state.wake_in_flight = True
            staged_state.wake_delivery = None
            dispatched = True
        await self._persist_staged_monitor_locked(loop, staged)
        if not loop.active:
            self._sync_terminal_completion_timer(loop)
    self._emit("updated", loop)
    return dispatched


async def record_monitor_turn_completion(
    self: AutoNudgeService,
    completion: MonitorActionCompletion,
) -> None:
    """Charge one correlated, completed action turn exactly once."""
    async with self._lock:
        if self._accepted_monitor_turns.get(completion.monitor_id) == completion.fingerprint:
            self._accepted_monitor_turns.pop(completion.monitor_id, None)
        loop = self._loops.get(completion.monitor_id)
        state = loop.monitor if loop is not None else None
        if (
            loop is None
            or state is None
            or not state.wake_in_flight
            or state.last_wake_fingerprint != completion.fingerprint
        ):
            return
        staged = deepcopy(loop)
        staged_state = staged.monitor
        assert staged_state is not None
        disposition = (
            MonitorActionDisposition.APPROVAL_STALL
            if staged.approval_stalled
            else completion.disposition
        )
        if staged_state.wake_delivery is not MonitorDispatchResult.DISPATCHED:
            staged_state.wake_count += 1
        staged_state.wake_in_flight = False
        staged_state.wake_delivery = None
        staged_state.completion_evidence_deadline = 0.0
        staged_state.last_completion_fingerprint = completion.fingerprint
        staged_state.last_completion_disposition = disposition
        staged_state.last_completed_at = completion.completed_ts
        staged_state.agent_turns += 1
        if completion.input_tokens is None or completion.output_tokens is None:
            staged_state.token_usage_known = False
        if completion.input_tokens is not None:
            staged_state.input_tokens += completion.input_tokens
        if completion.output_tokens is not None:
            staged_state.output_tokens += completion.output_tokens
        reason = monitor_budget_reason(staged_state, now=completion.completed_ts)
        if staged_state.outcome is not None:
            self._set_monitor_deadline(staged, 0.0)
        elif reason:
            self._apply_monitor_budget_stop(
                staged,
                reason,
                stopped_at=completion.completed_ts,
            )
        elif (
            disposition is MonitorActionDisposition.APPROVAL_STALL and staged_state.outcome is None
        ):
            staged.active = False
            self._set_monitor_deadline(staged, 0.0)
            staged_state.outcome = MonitorOutcome.BLOCKED
            staged_state.stopped_reason = MONITOR_STOP_APPROVAL_STALL
            staged_state.stopped_at = completion.completed_ts
        elif staged.active and staged_state.outcome is None:
            self._set_monitor_deadline(
                staged,
                completion.completed_ts + staged_state.cadence_secs,
            )
        await self._persist_staged_monitor_locked(loop, staged)
        if not loop.active:
            self._sync_terminal_completion_timer(loop)
        if loop.active and state.outcome is None:
            if loop.id in self._firing:
                self._rearm_pending.add(loop.id)
            else:
                self._arm_from_deadline(loop)
    self._emit("updated", loop)


def _apply_monitor_budget_stop(
    self: AutoNudgeService,
    loop: NudgeLoop,
    reason: str,
    *,
    stopped_at: float,
) -> None:
    """Apply budget-stop fields without changing the live timer registry."""
    state = loop.monitor
    if state is None:
        return
    loop.active = False
    self._retain_accepted_terminal_completion(loop, stopped_at=stopped_at)
    state.outcome = MonitorOutcome.BUDGET
    state.stopped_reason = reason
    state.stopped_at = stopped_at


def _apply_monitor_user_stop(self: AutoNudgeService, loop: NudgeLoop, *, stopped_at: float) -> None:
    """Apply a user stop after its replacement snapshot is durable."""
    state = loop.monitor
    if state is None:
        return
    loop.active = False
    self._retain_accepted_terminal_completion(loop, stopped_at=stopped_at)
    state.outcome = MonitorOutcome.USER_STOP
    state.stopped_reason = MONITOR_STOP_USER
    state.stopped_at = stopped_at


def _retain_accepted_terminal_completion(
    self: AutoNudgeService,
    loop: NudgeLoop,
    *,
    stopped_at: float,
) -> None:
    """Bound an accepted terminal claim until completion or evidence expiry."""
    state = loop.monitor
    if state is None:
        return
    accepted = (
        self._accepted_monitor_turns.get(loop.id) == state.last_wake_fingerprint
        and state.wake_delivery is not MonitorDispatchResult.BUSY
    )
    if not accepted:
        self._accepted_monitor_turns.pop(loop.id, None)
        state.wake_in_flight = False
        state.wake_delivery = None
        state.completion_evidence_deadline = 0.0
        self._set_monitor_deadline(loop, 0.0)
        return
    deadline = state.completion_evidence_deadline
    if deadline <= stopped_at:
        deadline = stopped_at + MONITOR_COMPLETION_EVIDENCE_TIMEOUT_SECS
        state.completion_evidence_deadline = deadline
    self._set_monitor_deadline(loop, deadline)


def _waits_for_terminal_completion(self: AutoNudgeService, loop: NudgeLoop) -> bool:
    """Whether a terminal row still owns a finite accepted-turn correlation."""
    state = loop.monitor
    return bool(
        state is not None
        and state.outcome
        in {
            MonitorOutcome.BUDGET,
            MonitorOutcome.SESSION_CLOSE,
            MonitorOutcome.USER_STOP,
        }
        and state.wake_in_flight
        and state.completion_evidence_deadline > 0
    )


def _sync_terminal_completion_timer(self: AutoNudgeService, loop: NudgeLoop) -> None:
    """Keep only the timer needed to expire an accepted terminal claim."""
    if self._waits_for_terminal_completion(loop):
        if loop.id in self._firing:
            self._rearm_pending.add(loop.id)
        else:
            self._arm_from_deadline(loop)
        return
    self._cancel_timer(loop.id)


async def record_monitor_dispatch_failure(
    self: AutoNudgeService,
    monitor_id: str,
    fingerprint: str,
    *,
    now: float | None = None,
) -> None:
    """Retire an acknowledged wake when its session cannot accept it."""
    async with self._lock:
        if self._accepted_monitor_turns.get(monitor_id) == fingerprint:
            self._accepted_monitor_turns.pop(monitor_id, None)
        loop = self._loops.get(monitor_id)
        state = loop.monitor if loop is not None else None
        if (
            loop is None
            or state is None
            or state.outcome is not None
            or not state.wake_in_flight
            or state.last_wake_fingerprint != fingerprint
        ):
            return
        staged = deepcopy(loop)
        staged_state = staged.monitor
        assert staged_state is not None
        staged_state.wake_in_flight = False
        staged_state.completion_evidence_deadline = 0.0
        staged_state.wake_delivery = MonitorDispatchResult.UNAVAILABLE
        staged.active = False
        staged_state.outcome = MonitorOutcome.TARGET_UNAVAILABLE
        staged_state.stopped_reason = MONITOR_STOP_SESSION_UNAVAILABLE
        staged_state.stopped_at = time.time() if now is None else now
        self._set_monitor_deadline(staged, 0.0)
        await self._persist_staged_monitor_locked(loop, staged)
        self._cancel_timer(loop.id)
    self._emit("updated", loop)


async def monitor_dispatch_is_authorized(
    self: AutoNudgeService,
    monitor_id: str,
    fingerprint: str,
) -> bool:
    """Revalidate a persisted claim immediately before transport handoff."""
    async with self._lock:
        loop = self._loops.get(monitor_id)
        state = loop.monitor if loop is not None else None
        return bool(
            loop is not None
            and state is not None
            and loop.active
            and state.outcome is None
            and state.wake_in_flight
            and state.last_wake_fingerprint == fingerprint
            and state.wake_delivery is not MonitorDispatchResult.DISPATCHED
        )


def mark_monitor_turn_accepted(self: AutoNudgeService, monitor_id: str, fingerprint: str) -> None:
    """Remember a claimed wake that crossed a channel's provider boundary."""
    loop = self._loops.get(monitor_id)
    state = loop.monitor if loop is not None else None
    if (
        loop is not None
        and state is not None
        and loop.active
        and state.outcome is None
        and state.wake_in_flight
        and state.last_wake_fingerprint == fingerprint
    ):
        self._accepted_monitor_turns[monitor_id] = fingerprint


async def record_monitor_dispatch_busy(
    self: AutoNudgeService,
    monitor_id: str,
    fingerprint: str,
    *,
    now: float,
) -> None:
    """Retry one claimed wake after ordinary session concurrency clears."""
    async with self._lock:
        if self._accepted_monitor_turns.get(monitor_id) == fingerprint:
            self._accepted_monitor_turns.pop(monitor_id, None)
        loop = self._loops.get(monitor_id)
        state = loop.monitor if loop is not None else None
        if (
            loop is None
            or state is None
            or not loop.active
            or not state.wake_in_flight
            or state.last_wake_fingerprint != fingerprint
        ):
            return
        staged = deepcopy(loop)
        staged_state = staged.monitor
        assert staged_state is not None
        reason = monitor_budget_reason(staged_state, now=now)
        if reason:
            self._apply_monitor_budget_stop(staged, reason, stopped_at=now)
        else:
            staged_state.wake_delivery = MonitorDispatchResult.BUSY
            staged_state.completion_evidence_deadline = 0.0
            self._set_monitor_deadline(
                staged,
                now + min(MONITOR_BUSY_RETRY_SECS, staged_state.cadence_secs),
            )
        await self._persist_staged_monitor_locked(loop, staged)
        if not loop.active:
            self._sync_terminal_completion_timer(loop)
        if loop.active:
            if loop.id in self._firing:
                self._rearm_pending.add(loop.id)
            else:
                self._arm_from_deadline(loop)
    self._emit("updated", loop)


async def record_monitor_dispatched(
    self: AutoNudgeService,
    monitor_id: str,
    fingerprint: str,
    *,
    now: float,
) -> None:
    """Persist the finite window for authoritative completion evidence."""
    async with self._lock:
        loop = self._loops.get(monitor_id)
        state = loop.monitor if loop is not None else None
        if (
            loop is None
            or state is None
            or not loop.active
            or not state.wake_in_flight
            or state.last_wake_fingerprint != fingerprint
        ):
            return
        staged = deepcopy(loop)
        staged_state = staged.monitor
        assert staged_state is not None
        deadline = now + MONITOR_COMPLETION_EVIDENCE_TIMEOUT_SECS
        if staged_state.wake_delivery is not MonitorDispatchResult.DISPATCHED:
            staged_state.wake_count += 1
        staged_state.wake_delivery = MonitorDispatchResult.DISPATCHED
        staged_state.completion_evidence_deadline = deadline
        self._set_monitor_deadline(staged, deadline)
        await self._persist_staged_monitor_locked(loop, staged)
        if loop.id in self._firing:
            self._rearm_pending.add(loop.id)
        else:
            self._arm_from_deadline(loop)
    self._emit("updated", loop)


async def record_monitor_completion_evidence_unavailable(
    self: AutoNudgeService,
    monitor_id: str,
    fingerprint: str,
    *,
    now: float,
) -> None:
    """Fail closed when an accepted wake never reports raw completion."""
    async with self._lock:
        loop = self._loops.get(monitor_id)
        state = loop.monitor if loop is not None else None
        if (
            loop is None
            or state is None
            or not state.wake_in_flight
            or state.last_wake_fingerprint != fingerprint
            or state.completion_evidence_deadline <= 0
            or now < state.completion_evidence_deadline
        ):
            return
        terminal = state.outcome is not None
        if terminal and state.outcome not in {
            MonitorOutcome.BUDGET,
            MonitorOutcome.SESSION_CLOSE,
            MonitorOutcome.USER_STOP,
        }:
            return
        if self._accepted_monitor_turns.get(monitor_id) == fingerprint:
            self._accepted_monitor_turns.pop(monitor_id, None)
        staged = deepcopy(loop)
        staged_state = staged.monitor
        assert staged_state is not None
        staged_state.wake_in_flight = False
        staged_state.wake_delivery = None
        staged_state.completion_evidence_deadline = 0.0
        if not terminal:
            staged.active = False
            staged_state.outcome = MonitorOutcome.BLOCKED
            staged_state.stopped_reason = MONITOR_STOP_COMPLETION_UNAVAILABLE
            staged_state.stopped_at = now
        self._set_monitor_deadline(staged, 0.0)
        await self._persist_staged_monitor_locked(loop, staged)
        self._cancel_timer(loop.id)
    self._emit("updated", loop)


async def _deactivate_unwired_monitor(self: AutoNudgeService, loop_id: str) -> None:
    """Retain but disarm a structured record when no controller is wired."""
    async with self._lock:
        loop = self._loops.get(loop_id)
        if loop is None or loop.monitor is None:
            return
        staged = deepcopy(loop)
        staged.active = False
        assert staged.monitor is not None
        staged.monitor.wake_in_flight = False
        staged.monitor.wake_delivery = None
        staged.monitor.completion_evidence_deadline = 0.0
        staged.monitor.outcome = MonitorOutcome.BLOCKED
        staged.monitor.stopped_reason = MONITOR_STOP_SESSION_UNAVAILABLE
        staged.monitor.stopped_at = time.time()
        self._set_monitor_deadline(staged, 0.0)
        await self._persist_staged_monitor_locked(loop, staged)
        self._cancel_timer(loop.id)
    self._emit("updated", loop)
