"""Where a running turn yields its lane slot and waits, and how a stop is noted.

``_yield_for_stop_recovery_impl``, ``_yield_for_dependency_impl`` and
``_yield_for_infra_retry_impl`` release the run's lane slot, record the wait on its durable
row and re-admit it through ``_await_lane_resume_impl`` (``_withdraw_resume`` drops the
resume entry of a waiter that stopped waiting); ``lane_wait_for_status`` records a typed
input wait, ``ensure_running_marked`` writes the row ``running`` the waits need,
``_dependency_verdict`` and ``_dependency_report_db`` run a dependency park's database
phase, and ``_stop_recovery_wanted_impl``, ``_taskq_note_stop_recovery_impl``,
``_stop_recovery_note_db`` and ``_stop_error_text_impl`` decide, note and word a stop. New
work on a wait inside a run goes here.

A function here whose first parameter is ``self`` is a ``RunEventCoordinator`` member: the
class binds it under the same name in ``subagent_manager/run.py``. The ``*_impl`` ones are
rebound onto ``kiro_crew.subagent``'s globals by ``bind_component_globals``, so the names
they read unqualified are that module's, imported here for typing only; the others run on
this module's own globals, and ``_dependency_report_db`` and ``_stop_recovery_note_db`` are
bound through ``staticmethod``.

The manager fields this module uses. Writes: ``_queue``. Reads: ``_admission``,
``_await_lane_resume``, ``_fire_event``, ``_leave_startup``, ``_shutting_down``,
``_taskq_note_stop_recovery``, ``_taskq_pump``, ``_yield_for_dependency``,
``dependency_coordinator_async``.
"""

from __future__ import annotations

import logging as _logging
import time as _time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import time

    from ..subagent import (
        _RECOVERY_SLOT_WAIT_SECS,
        MAX_ERROR_DETAIL_LEN,
        STOP_CLASS_CANCELLED,
        STOP_RECOVERY_MAX_RETRIES,
        Any,
        SubagentInfo,
        _redact,
        classify_stop_reason,
        logger,
        sel,
    )
    from .run import RunEventCoordinator


def _stop_recovery_wanted_impl(self: RunEventCoordinator, info: SubagentInfo, stop: Any) -> bool:
    """True when a completion's class should be recovered IN PLACE.

    Only ``recoverable`` classes (stalled / recovering) qualify, and only
    while the shared budget (``STOP_RECOVERY_MAX_RETRIES``) has room and no
    terminal marker has been set on the run: a user stop, a reap in flight
    or manager shutdown all win over recovery.
    """
    return bool(
        stop.recoverable
        and info._stop_recovery_used < STOP_RECOVERY_MAX_RETRIES
        and not info.user_stopped
        and not info._reap_started
        and not info.reaped
        and not self._manager._shutting_down
    )


async def _yield_for_stop_recovery_impl(
    self: RunEventCoordinator, info: SubagentInfo, event: Any
) -> str | None:
    """Yield the lane slot for a recoverable completion, re-admit, and
    return the continue-nudge to send on the same session.

    Yielding = the LANE slot is released through ``admission.yield_slot``
    so queued work can start; the session (process, FDs, memory) stays
    alive and keeps its residency charge, exactly as the addendum requires.
    The durable row goes ``running -> waiting_dependency`` (scope
    ``session:<stop class>``, evidence from the liveness oracle) and back
    to ``running`` under a NEW generation when the pump grants the resume
    entry -- FIFO with every other wake, never a poll of the running count.
    Bounded by ``_RECOVERY_SLOT_WAIT_SECS``. Returns ``None`` when
    re-admission was refused (deadline, shutdown, stop, reap); the caller
    then surfaces the withheld completion and the run ends ``failed`` with
    its partial preserved.
    """
    import re

    from kiro_crew.agent_sdk.drivers.acp_vocab import WAIT_REASON_INPUT
    from kiro_crew.dashboard.state import (
        TOOL_STALL_RECOVERY_PREFIX,
        build_tool_stall_recovery_prompt,
    )
    from kiro_crew.taskq.waits import EVIDENCE_LIVENESS_ORACLE, WaitRecord

    stop = classify_stop_reason(getattr(event, "stop_reason", ""))
    info._stop_recovery_used += 1
    attempt = info._stop_recovery_used
    status = getattr(event, "status", None)
    typed_input_wait = bool(
        status is not None and getattr(status, "wait_reason", "") == WAIT_REASON_INPUT
    )
    # A typed ``waiting_input`` status earlier in the stream has already
    # yielded the slot (``lane_wait_for_status``); the completion that
    # follows only needs the resume. Otherwise the wait is recorded here.
    released = False
    if not info._slot_released:
        self.ensure_running_marked(info)
        released = self._manager._admission.yield_slot(
            info,
            WaitRecord.dependency(
                f"session:{stop.name}",
                since=time.time(),
                reason=(
                    f"{stop.name} completion ({stop.stop_reason}); in-place recovery "
                    f"{attempt}/{STOP_RECOVERY_MAX_RETRIES}"
                ),
                source=EVIDENCE_LIVENESS_ORACLE,
            ),
        )
    self._manager._taskq_note_stop_recovery(
        info,
        {
            "phase": "yielded",
            "stop_reason": stop.stop_reason,
            "stop_class": stop.name,
            "attempt": attempt,
            "max": STOP_RECOVERY_MAX_RETRIES,
            "slot_released": released or info._slot_released,
        },
    )
    logger.warning(
        "Subagent %s: %s completion (stop_reason=%r) — yielding slot, recovery %d/%d",
        info.id,
        stop.name,
        stop.stop_reason,
        attempt,
        STOP_RECOVERY_MAX_RETRIES,
    )
    try:
        await self._manager._fire_event(
            "subagent_recovering",
            info,
            {
                "attempt": attempt,
                "max": STOP_RECOVERY_MAX_RETRIES,
                "stop_reason": stop.stop_reason,
                "stop_class": stop.name,
            },
        )
    except Exception:
        logger.debug("subagent_recovering emit failed for %s", info.id, exc_info=True)
    try:
        sel().log_api_access(
            caller=info.parent_session_key or f"subagent:{info.id}",
            operation="subagent.stop_recovery",
            outcome="recovering",
            source="subagent",
            resources=(
                f"subagent_id={info.id},stop_class={stop.name},"
                f"attempt={attempt},slot_released={released or info._slot_released}"
            ),
        )
    except Exception:
        logger.debug("SEL audit for stop recovery failed", exc_info=True)
    # Re-admission through capacity, never a blind increment: the resume
    # entry waits its turn in the pump like every other wake.
    readmitted = await self._manager._await_lane_resume(
        info,
        reason=f"stop recovery {attempt}/{STOP_RECOVERY_MAX_RETRIES} ({stop.name})",
        timeout=_RECOVERY_SLOT_WAIT_SECS,
    )
    if not readmitted:
        logger.warning(
            "Subagent %s: no lane slot for in-place recovery — surfacing %s without recovery",
            info.id,
            stop.name,
        )
        # Spend the budget so the surfaced completion is terminal.
        info._stop_recovery_used = STOP_RECOVERY_MAX_RETRIES
        return None
    self._manager._taskq_note_stop_recovery(
        info, {"phase": "readmitted", "stop_class": stop.name, "attempt": attempt}
    )
    evidence = str(getattr(event, "text", "") or "")
    idle_m = re.search(r"idle_secs=(\d+)", evidence)
    body = build_tool_stall_recovery_prompt(
        str(getattr(event, "title", "") or ""),
        int(idle_m.group(1)) if idle_m else 0,
        command=str(getattr(event, "tool_input", "") or ""),
        # Typed verdict first; the evidence-text marker is the fallback for
        # a provider that forwards no status object.
        stuck_input=typed_input_wait or "stuck_input" in evidence,
    )
    return f"{TOOL_STALL_RECOVERY_PREFIX}\n{body}"


async def _await_lane_resume_impl(
    self: RunEventCoordinator,
    info: SubagentInfo,
    *,
    reason: str,
    timeout: float,
    request: bool = True,
) -> bool:
    """Wait for the pump to hand a yielded lane slot back to *info*.

    The ONE re-admission primitive for every wait the run loop enters
    (stop recovery, dependency wait, infra retry): a resume entry is queued
    at the front of the window and the coroutine parks on
    ``info._resume_event`` until ``admission.resume_grant`` sets it (slot
    held again, row ``running`` under a new generation) or the coordinator
    fails the wait. ``request=False`` only parks: the resume entry is then
    queued by whoever owns the wake (the dependency coordinator's
    ``wake_through`` seam), so a scope that is not due yet is never
    re-entered early. False on timeout / failure, with the resume entry
    withdrawn so a late grant cannot hand a slot to a run that gave up. A
    run that never yielded answers True at once.
    """
    if not info._slot_released:
        return True
    event = getattr(info, "_resume_event", None)
    if event is None:
        event = asyncio.Event()
        info._resume_event = event
    if request and not info._resume_pending:
        if not self._manager._admission.request_resume(info, reason=reason):
            return not info._slot_released
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        self._withdraw_resume(info)
        return False
    finally:
        info._resume_event = None
    if info._wait_failed:
        self._withdraw_resume(info)
        return False
    return not info._slot_released


def _withdraw_resume(self: RunEventCoordinator, info: "SubagentInfo") -> None:
    """Drop *info*'s pending resume entry: the waiter stopped waiting."""
    queue = self._manager._queue
    for index, params in enumerate(list(queue)):
        if str(params.get("_resume_id") or "") == info.id:
            queue.pop(index)
            break
    info._resume_pending = False


async def _yield_for_dependency_impl(
    self: RunEventCoordinator, info: SubagentInfo, signal: Any
) -> bool | None:
    """Park *info* on its dependency scope's ONE schedule and wait for the wake.

    ``coordinator.report`` writes the wait (``running -> waiting_dependency``
    with the scope's ``retry_at``); the lane slot is then released without
    a second row write. The coordinator wakes the scope by capacity
    (probe, then ``wake_per_tick``); a live run's wake comes back through
    admission (``taskq_wake_through`` -> ``request_resume``), so the slot
    is re-granted FIFO like every other start. A typed provider throttle is
    also reported to the adaptive controller. True once the slot is held
    again; False when the coordinator failed the scope (deadline or
    attempts cap -- the row is already ``failed``), when the wait did not
    persist (``unpersisted``: the row is another owner's or has ended, or the
    store could not say, so the run stops rather than retry work it may not
    own), or when the wait timed out. None when
    NO row exists for the run (``no_row``: a memory mode that keeps none).
    Nothing was decided then and nothing else owns the work, so the caller
    handles the error as it would with no coordinator at all.
    """
    from kiro_crew.taskq.dependency import (
        KIND_CONCURRENCY_EXCEEDED,
        KIND_RATE_LIMITED,
        OUTCOME_NO_ROW,
        PHASE_WAITING,
    )
    from kiro_crew.taskq.waits import EVIDENCE_DEPENDENCY_ADAPTER, WaitRecord

    coordinator = await self._manager.dependency_coordinator_async()
    if coordinator is None or info.done or info._reap_started or info.user_stopped:
        return False
    if signal.kind in (KIND_RATE_LIMITED, KIND_CONCURRENCY_EXCEEDED):
        try:
            from kiro_crew.adaptive.controller import current as _current_controller

            controller = _current_controller()
            if controller is not None:
                controller.record_provider_throttle(signal.dependency_scope)
        except Exception:
            logger.debug("provider throttle report failed for %s", info.id, exc_info=True)
    info._wait_failed = ""
    info._resume_event = asyncio.Event()
    now = time.time()
    # The ``running`` mark and the coordinator's wait write are ONE unit on
    # ONE thread: a provider error can land before the turn's first frame
    # (the prompt itself was refused), and the session IS live -- the turn
    # was issued -- so the row must be ``running`` before the wait is
    # written or the wait is refused and the row parked instead.
    #
    # The same fact takes the run out of startup: the provider answered
    # this session's prompt, so what follows is a wait for the dependency,
    # bounded by the scope's own deadline and ending in its own error --
    # never a start that is not starting for the startup watchdog to reap,
    # and never a place in the in-startup bound while parked.
    self._manager._leave_startup(info)
    verdict = await self._dependency_verdict(coordinator, info, signal)
    if verdict.outcome != "wait":
        info._resume_event = None
        logger.warning(
            "Subagent %s: dependency %s %s (%s)",
            info.id,
            signal.dependency_scope,
            verdict.outcome,
            verdict.reason,
        )
        return None if verdict.outcome == OUTCOME_NO_ROW else False
    if not info._slot_released:
        self._manager._admission.yield_slot(
            info,
            WaitRecord.dependency(
                signal.dependency_scope,
                since=now,
                retry_at=verdict.retry_at,
                reason=f"{signal.kind} from {signal.source}: {signal.detail}",
                source=EVIDENCE_DEPENDENCY_ADAPTER,
            ),
            persist=False,
        )
    logger.warning(
        "Subagent %s: %s on %s — parked on the scope schedule (retry_at in %.1fs, "
        "attempt %d, %s); lane slot yielded",
        info.id,
        signal.kind,
        signal.dependency_scope,
        max(0.0, float(verdict.retry_at or now) - now),
        verdict.attempts,
        PHASE_WAITING,
    )
    try:
        sel().log_api_access(
            caller=info.parent_session_key or f"subagent:{info.id}",
            operation="subagent.dependency_wait",
            outcome="waiting",
            source="subagent",
            resources=(
                f"subagent_id={info.id},scope={signal.dependency_scope},"
                f"kind={signal.kind},attempt={verdict.attempts}"
            ),
        )
    except Exception:
        logger.debug("SEL audit for dependency wait failed", exc_info=True)
    # Arm the pump for this scope's deadline (the reaper sweep is the backstop).
    self._manager._taskq_pump()
    deadline = coordinator.wait_deadline_secs or 0.0
    timeout = (deadline if deadline > 0 else 86400.0) + coordinator.backoff_max_secs + 60.0
    # The wake itself queues the resume (``taskq_wake_through``); this
    # only parks on the grant.
    woke = await self._manager._await_lane_resume(
        info,
        reason=f"dependency {signal.dependency_scope} recovered",
        timeout=timeout,
        request=False,
    )
    if not woke:
        coordinator.forget(info.id)
        if info._wait_failed:
            logger.warning(
                "Subagent %s: dependency wait on %s failed: %s",
                info.id,
                signal.dependency_scope,
                info._wait_failed,
            )
    return woke


async def _yield_for_infra_retry_impl(
    self: RunEventCoordinator, info: SubagentInfo, infra: Any
) -> str | None:
    """L1 of the recovery ladder for a sub-agent: a tool call the MCP gateway
    refused (capacity ``-32001`` / recoverable infra) ended the turn normally.

    The ladder decides retry-or-escalate for THIS run (its per-run L1
    budget); the wait itself is a dependency signal on the shared
    ``mcp_gateway:<class>`` scope, so every sub-agent refused by the same
    gateway waits on ONE schedule and wakes by capacity. Only a
    server-stated ``retry_after`` becomes the signal's ``retry_at``: the
    ladder's per-run delay is NOT passed as one, because the scope's
    schedule is shared and each woken probe is a different run at its own
    attempt 1 -- honouring that ~2 s delay exactly made the whole scope
    retry every 2 s and spend its probe budget a minute into an outage.
    Without a server value the shared recovery schedule (equal-jitter
    exponential backoff, ``agent.recovery_backoff_*``) governs the scope.
    Returns the continuation that re-issues exactly the refused call, or
    None once the ladder escalates (the normal completion is then surfaced
    as it was).
    """
    from kiro_crew.dashboard.state import build_infra_retry_prompt
    from kiro_crew.recovery.ladder import CLASS_CAPACITY, L1_TOOL_CALL, default_ladder
    from kiro_crew.taskq.dependency import (
        KIND_CONCURRENCY_EXCEEDED,
        KIND_DEPENDENCY_UNAVAILABLE,
        DependencySignal,
    )

    decision = default_ladder().observe_failure(
        L1_TOOL_CALL,
        f"subagent:{info.id}",
        retry_after_secs=infra.retry_after_secs,
        reason=infra.error_class,
        task_id=info.id if self._manager._admission.taskq_store() is not None else None,
    )
    if not decision.retry:
        logger.warning(
            "Subagent %s: L1 budget spent for %s (%s); surfacing the completion",
            info.id,
            infra.error_class,
            decision.action,
        )
        return None
    signal = DependencySignal(
        kind=(
            KIND_CONCURRENCY_EXCEEDED
            if infra.error_class == CLASS_CAPACITY
            else KIND_DEPENDENCY_UNAVAILABLE
        ),
        dependency_scope=f"mcp_gateway:{infra.error_class}",
        source="mcp_gateway",
        retry_at=(
            time.time() + float(infra.retry_after_secs)
            if infra.retry_after_secs is not None and float(infra.retry_after_secs) > 0
            else None
        ),
        detail=str(infra.detail or infra.error_class)[:200],
    )
    try:
        await self._manager._fire_event(
            "subagent_recovering",
            info,
            {
                "attempt": decision.attempt,
                "max": default_ladder().layer_policy(L1_TOOL_CALL).max_attempts,
                "stop_reason": "end_turn",
                "stop_class": f"infra:{infra.error_class}",
            },
        )
    except Exception:
        logger.debug("subagent_recovering emit failed for %s", info.id, exc_info=True)
    if not await self._manager._yield_for_dependency(info, signal):
        return None
    return build_infra_retry_prompt(infra.error_class, infra.retry_after_secs)


def ensure_running_marked(self: RunEventCoordinator, info: "SubagentInfo") -> None:
    """Write the durable row ``running`` once the run's own turn exists.

    Normally done by the first stream event addressed to this session (a
    ``runtime_global`` frame is a co-tenant's); the stop-recovery path calls it
    too, because a wait can only be recorded on a ``running`` row and the
    turn that failed was a real turn on a live session. The write is POSTED,
    which is what orders it ahead of the wait write ``yield_slot`` posts
    behind it. The dependency path does NOT come through here: its wait write
    is inline, so its mark travels into the same database phase
    (:meth:`_dependency_report_db`). Both marks are ``TaskStore.advance``,
    never a bare ``transition``, so the missed-``starting`` replay is a
    property of the STORE and not of whichever caller reached it.

    ``info._taskq_running_marked`` is set BEFORE the posted write, so it says
    only that a mark was issued for this run -- never that the row IS
    ``running``. It is a duplicate-write guard and nothing else: no durable
    decision may read it, because the write it leads is best-effort and its
    refusal reaches nobody.
    """
    if info._taskq_running_marked:
        return
    info._taskq_running_marked = True
    self._manager._admission.taskq_mark(info, "running")


def _dependency_report_db(
    coordinator: Any,
    store: Any,
    task_id: str,
    signal: Any,
    generation: int | None,
) -> Any:
    """Database phase of a dependency park: the ``running`` mark and then the
    coordinator's verdict, on ONE thread.

    ``report`` persists the wait itself and the transition table accepts one
    only FROM ``running``. A wait refused on a ``starting`` row is PARKED
    (``retry_wait``) instead, and ``retry_wait`` has an edge to neither
    ``running`` nor ``done`` -- so the run's own terminal write is refused
    too and the row stays claimable after the work already completed. The
    mark can therefore not be posted behind this call.

    The mark is UNCONDITIONAL, and the decision it looks like is the STORE's:
    ``TaskStore.advance`` reads the row and writes nothing when it is already
    ``running``, replays ``admitted -> starting -> running`` when an earlier
    mark was lost to a locked database, and refuses the row another owner
    ended. Nothing here may gate it on ``info._taskq_running_marked``: that
    flag is published AHEAD of a posted write whose refusal reaches nobody,
    so a flag-gated park skips the mark on a row still ``starting`` and
    writes ``retry_wait`` on a LIVE resident run -- claimable, outside the
    set a boot reconcile examines (``model.ACTIVE``), and with ``running``,
    ``done`` and every wake refused from there, so the run's result can never
    be recorded. The same entry point ``taskq_advance`` uses, never a bare
    ``transition``, so the replay is a property of the store and not of
    whichever caller reached it.
    """
    from kiro_crew import taskq as _taskq

    if store is not None:
        try:
            store.advance(task_id, _taskq.RUNNING, generation=generation)
        except _taskq.TaskStoreUnavailable:
            _logging.getLogger("kiro_crew.subagent_manager.run").debug(
                "taskq: running mark for %s ahead of its dependency wait failed",
                task_id,
                exc_info=True,
            )
    return coordinator.report(task_id, signal, generation=generation, from_state=_taskq.RUNNING)


async def _dependency_verdict(
    self: RunEventCoordinator, coordinator: "Any", info: "SubagentInfo", signal: "Any"
) -> "Any":
    """*signal*'s verdict, with the ``running`` mark on the same thread as
    the wait write it enables (:meth:`_dependency_report_db`).

    With the off-loop pump the pair runs on the store's writer thread;
    without it, inline, where program order already holds. The mark travels
    unconditionally: which write it makes is the row's own state to decide,
    never this process's ``_taskq_running_marked``, whose whole purpose is to
    keep a LATER stream mark from advancing the parked row back out of the
    wait the coordinator just recorded on it.
    """
    admission = self._manager._admission
    store = admission.taskq_store()
    info._taskq_running_marked = True
    args = (coordinator, store, info.id, signal, info._taskq_generation or None)
    if store is None or not type(admission).pump_off_loop:
        return self._dependency_report_db(*args)
    return await store.run(self._dependency_report_db, *args)


def lane_wait_for_status(self: RunEventCoordinator, info: "SubagentInfo", event: Any) -> bool:
    """A typed ``waiting_input`` status from the execution layer / oracle
    (W4): release the lane slot now, keep the residency, record the wait.

    The record carries the tool call the wait belongs to; ``safe_retry``
    rides along in the reason so the health panel can say whether the
    blocked command can be re-run non-interactively. Anything else on the
    status channel (running, recovering, other wait reasons) is not this
    run's lane decision and is ignored here.
    """
    from kiro_crew.agent_sdk.drivers.acp_vocab import WAIT_REASON_INPUT
    from kiro_crew.taskq.waits import (
        EVIDENCE_EXECUTION_LAYER,
        EVIDENCE_LIVENESS_ORACLE,
        WaitRecord,
    )

    status = getattr(event, "status", None)
    if status is None or getattr(status, "wait_reason", "") != WAIT_REASON_INPUT:
        return False
    if info._slot_released or info.done:
        return False
    source = (
        EVIDENCE_LIVENESS_ORACLE
        if getattr(status, "origin", "") == "liveness_oracle"
        else EVIDENCE_EXECUTION_LAYER
    )
    record = WaitRecord.input(
        str(getattr(status, "tool_call_id", "") or getattr(event, "tool_call_id", "") or ""),
        since=_time.time(),
        reason=(
            "a command is waiting for real user input"
            + (
                " (safe to re-run non-interactively)"
                if getattr(status, "safe_retry", False)
                else ""
            )
        ),
        source=source,
    )
    return self._manager._admission.yield_slot(info, record)


def _taskq_note_stop_recovery_impl(
    self: RunEventCoordinator, info: SubagentInfo, data: dict[str, Any]
) -> None:
    """Best-effort ``stop_recovery`` event + progress marker on the durable row.

    Deliberately NOT a state transition: the row keeps ``running`` and our
    lease, so the yield can never be read as a lost owner and re-claimed.
    """
    admission = self._manager._admission
    store = admission.taskq_store()
    if store is None:
        return
    admission._post_store_write(
        store,
        f"{info.id} stop_recovery note",
        self._stop_recovery_note_db,
        store,
        info.id,
        info._taskq_generation,
        dict(data),
    )


def _stop_recovery_note_db(
    store: "Any", task_id: str, generation: int, data: "dict[str, Any]"
) -> None:
    """The two writes of a stop-recovery note as one off-loop unit."""
    store.append_event(task_id, "stop_recovery", data)
    store.record_progress(task_id, generation, data)


def _stop_error_text_impl(
    self: RunEventCoordinator, info: SubagentInfo, stop: Any, event: Any
) -> str:
    """Terminal ``error`` text for a non-success completion.

    Names the class and the raw stop reason (so a parent can act on it),
    the recovery attempts spent, and whether a partial was preserved.
    """
    evidence = _redact(str(getattr(event, "text", "") or ""))[:MAX_ERROR_DETAIL_LEN]
    partial = " — partial result preserved" if info.partial else ""
    if stop.name == STOP_CLASS_CANCELLED:
        return f"cancelled (stop_reason={stop.stop_reason}): turn cancelled by the runtime{partial}"
    if stop.recoverable:
        return (
            f"{stop.name}: {stop.stop_reason}"
            f" — {info._stop_recovery_used}/{STOP_RECOVERY_MAX_RETRIES} in-place "
            f"recovery attempts exhausted{partial}" + (f" [{evidence}]" if evidence else "")
        )
    if not stop.known:
        return f"failed (unexpected stop_reason={stop.stop_reason!r}){partial}"
    return f"failed ({stop.stop_reason}){partial}" + (f" [{evidence}]" if evidence else "")
