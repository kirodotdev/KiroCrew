"""How a run gets its session: shared or dedicated, priced, gated and, when late, adopted.

``_should_use_session_sharing_impl`` and ``_sharing_plan_impl`` decide whether a run shares
its parent's runtime (the AWS profile split helpers feed that decision),
``_ensure_dedicated_start_priced_impl`` reserves a dedicated process for a start admitted at
the shared price, ``_gate_wait_mark_impl`` and ``_gate_exit_reset_impl`` pause and resume
the start clock around a start-queue permit, ``_create_shared_session_impl`` creates the
session on the parent's runtime, ``_await_late_start_impl`` waits for a timed-out
``session/new`` to be adopted, ``_bind_shared_handle_impl`` binds a created handle as the
run's provider and ``_get_parent_runtime_impl`` finds the parent's runtime. New work on how
a run starts its session goes here.

A function here whose first parameter is ``self`` is a ``RunEventCoordinator`` member: the
class binds it under the same name in ``subagent_manager/run.py``. The ``*_impl`` ones are
rebound onto ``kiro_crew.subagent``'s globals by ``bind_component_globals``, so the names
they read unqualified are that module's, imported here for typing only; the others run on
this module's own globals.

The manager fields this module uses. Writes: none. Reads: ``_admission``, ``_agents``,
``_bind_shared_handle``, ``_claim_prices``, ``_dedicated_topup_lock``, ``_fire_event``,
``_gate_exit_reset``, ``_gate_wait_mark``, ``_get_parent_runtime``, ``_learned_settled_gb``,
``_memory_pressure_hold``, ``_note_startup_progress``, ``_provider_label_of``,
``_running_count``, ``_sessions``, ``_should_use_session_sharing``, ``_shutting_down``,
``_write_state_off_loop``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..aws_project_profiles import ResolvedAwsProfile

if TYPE_CHECKING:
    import time
    from collections.abc import Callable

    from ..subagent import (
        _DEDICATED_TOPUP_POLL_SECS,
        _DEDICATED_TOPUP_WAIT_SECS,
        MEMORY_CAUSE_READ_UNANSWERED,
        AcpRuntime,
        AcpSessionProvider,
        Any,
        KiroCrewConfig,
        LLMProvider,
        SubagentInfo,
        _cost_bucket,
        _dedicated_start_price_gb,
        _host_memory_reading_off_loop,
        _selection_kind,
        _SharingPlan,
        _spawn_memory_floor_and_cost,
        _startup_memory_reserve_gb,
        _subagent_default_effort,
        _subagent_default_model,
        logger,
        platform_compat,
        sel,
    )
    from .run import RunEventCoordinator


def _should_use_session_sharing_impl(self: RunEventCoordinator, info: SubagentInfo) -> bool:
    """Decide whether a subagent should use the shared-runtime path.

    All must hold: session_sharing config True; parent session exists and
    is ACP/kiro-backed (not CC); not a CC-specific spawn (model/allowed_tools/bare);
    no per-spawn backend.
    """
    # Member capability and native prompt documents are prepared at launch.
    if info._force_dedicated:
        return False
    if info.execution_context is not None and info.execution_context.member_id is not None:
        return False
    try:
        cfg = KiroCrewConfig.load()
        if not cfg.agent.session_sharing:
            return False
    except Exception:
        return False
    if info.model or info.routed_model or info.allowed_tools or info.bare:
        return False
    # The shared runtime is the parent's own process, on the parent's backend:
    # a run asked onto a backend of its own needs a process of its own.
    if info.backend is not None:
        return False
    if not info.parent_session_key:
        return False
    return self._manager._sessions.is_session_sharing_eligible(info.parent_session_key)


def _sharing_plan_impl(
    self: RunEventCoordinator,
    info: SubagentInfo,
    *,
    cfg: Any = None,
    child_aws_profile: ResolvedAwsProfile | None = None,
) -> _SharingPlan:
    """Decide how *info* will start: the ONE decision behind both callers.

    ``_run_inner`` takes the shared-runtime arm exactly when ``shared`` is
    True, and the admission gate prices the start from the same answer, so
    the price can never follow a copy of the rule that drifted. Shared needs
    template execution, no ``keep`` (a kept or continued run owns its
    process), :meth:`_should_use_session_sharing_impl`, and no per-spawn or
    per-role model / reasoning-effort pin: a pin cannot be applied to the
    parent's already-started runtime, so it forces the dedicated process
    where the override reaches the provider factory. Eligibility is the
    backend's opt-in membership (``is_session_sharing_eligible``); nothing
    here names a harness. *cfg* is a config the caller already loaded.

    A run whose ``cwd`` binds another AWS profile (``aws.project_profiles``)
    than the binding set on the parent also takes the dedicated arm, where
    the spawn sets the run's own profile: the shared arm would run it with
    the parent's. Resolving that binding reads the filesystem, so it is never
    done here: the run resolves it off the event loop and passes it as
    *child_aws_profile*. Without it -- the admission probe -- a split that is
    possible at all (a binding in the map, or one on the parent) is read as
    dedicated, the price the run may need. Only a real ``True`` from the
    session manager's answers rules sharing out, so a double that cannot
    answer them keeps the decision above.
    """
    kind = _selection_kind(info)
    eff_model = info.model or _subagent_default_model(cfg) or info.routed_model
    eff_effort = info.reasoning_effort or _subagent_default_effort(cfg)
    # Not coerced: the eligibility answer is passed through as the run has
    # always read it, and admission prices a start shared only on a real
    # ``True``, so an answer it cannot trust is priced dedicated.
    shared = (
        kind == "template"
        and not info.keep
        and not (eff_model or eff_effort)
        and self._manager._should_use_session_sharing(info)
    )
    if shared and self._aws_profile_split_possible(info):
        if child_aws_profile is None:
            shared = False
        else:
            differs = getattr(self._manager._sessions, "aws_profile_differs_from_parent", None)
            if (
                differs is not None
                and differs(info.parent_session_key, child_aws_profile.name) is True
            ):
                shared = False
    return _SharingPlan(eff_model, eff_effort, shared)


def _aws_profile_split_possible(self: RunEventCoordinator, info: SubagentInfo) -> bool:
    """Whether *info* could bind another AWS profile than its parent; reads no path."""
    possible = getattr(self._manager._sessions, "aws_profile_split_possible", None)
    return possible is not None and possible(info.parent_session_key) is True


async def _resolve_child_aws_profile(
    self: RunEventCoordinator, info: SubagentInfo
) -> ResolvedAwsProfile | None:
    """*info*'s own ``aws.project_profiles`` binding, resolved off the event loop.

    ``realpath`` over every mapped directory: one on a mount that stops
    answering stalls this run's thread, not the gateway's loop. ``None`` when
    no split is possible, so a run with no binding anywhere takes no thread hop.
    """
    if not self._aws_profile_split_possible(info):
        return None
    resolve = getattr(self._manager._sessions, "project_aws_profile", None)
    if resolve is None:
        return None
    name = await asyncio.to_thread(resolve, info.cwd)
    return ResolvedAwsProfile(name if isinstance(name, str) else None)


async def _ensure_dedicated_start_priced_impl(
    self: RunEventCoordinator, info: SubagentInfo
) -> None:
    """Reserve a dedicated process for a start admitted at the shared price.

    Admission reserved the shared price (:func:`_shared_start_price_gb`) for
    a start predicted to share its parent's runtime. When the start takes the
    dedicated arm instead -- the shared runtime was unavailable, or the
    decision changed while the row waited -- the process it is about to
    launch costs the dedicated projection, which nobody reserved. Raise the
    row's price first, so every later admission charges it, then re-check
    the floor with this row at that price. Below the floor the start waits
    here with its start clock frozen (it holds its slot, so it is still
    counted), one such start at a time so waiters do not each count the
    others and all hold, for at most ``_DEDICATED_TOPUP_WAIT_SECS``; past
    that it starts anyway and says so. A root start also waits here while the
    macOS kernel memory-pressure hold applies (subagent.md, *macOS: the
    kernel memory-pressure hold*), under the same bound: it is an admitted
    run already starting, not a held start, so it is not ended. A capacity verdict
    never fails a run that was admitted. A row not admitted at the shared
    price returns at once;
    the flag that marks it clears only once the check passed or the wait ran
    out, so a cancel during the wait leaves the respawn to re-check.
    """
    if not info._start_priced_shared:
        return
    floor, cost = _spawn_memory_floor_and_cost()
    settled = self._manager._learned_settled_gb
    info._start_price_gb = _dedicated_start_price_gb(
        cost,
        settled,
        _cost_bucket(info.agent, info.execution_context, remote=bool(info._remote_workspace)),
    )
    if floor <= 0:
        info._start_priced_shared = False
        return
    started = time.monotonic()
    deadline = started + _DEDICATED_TOPUP_WAIT_SECS
    self._manager._gate_wait_mark(info)()
    lock = self._manager._dedicated_topup_lock
    held = False
    try:
        info._topup_waiting = True
        try:
            await asyncio.wait_for(lock.acquire(), timeout=_DEDICATED_TOPUP_WAIT_SECS)
            held = True
        except asyncio.TimeoutError:
            pass
        info._topup_waiting = False

        def _need() -> float:
            # Rows still waiting for their turn have launched nothing; each
            # is counted once it holds the turn, against what went first.
            rows = list(self._manager._agents.values())
            queued_behind = sum(a._topup_waiting and not a._slot_released for a in rows)
            return floor + _startup_memory_reserve_gb(
                [a for a in rows if not a._topup_waiting],
                running_count=self._manager._running_count - queued_behind,
                cost_gb=cost,
                next_start_gb=0.0,
                settled_gb=settled,
                claim_prices=[price for price, _ in self._manager._claim_prices.values()],
            )

        # A nested child is never held by the kernel pressure hold; this row
        # itself does not count as a runtime of ours while it waits, since it
        # is still flagged shared-priced.
        root = not self._manager._admission.entry_is_child(
            {"parent_session_key": info.parent_session_key}
        )
        while True:
            asked = _need()
            avail, cause = await _host_memory_reading_off_loop(asked)
            # A read that never answered measured nothing: unknown, so it
            # keeps waiting, as the gate does. It is never the reader's
            # "unmeasurable" -1 (that fails open) and never re-read on the loop.
            unanswered = cause == MEMORY_CAUSE_READ_UNANSWERED
            # Decided here, on the loop, against what the reserve owes NOW:
            # other rows may have started or settled while the read ran.
            need = max(asked, _need())
            pressure = self._manager._memory_pressure_hold(floor_gb=floor) if root else None
            # -1 is the reader's "unmeasurable": fail open, as the gate does.
            fits = not unanswered and (avail < 0 or avail >= need)
            if fits and pressure is None:
                info._start_priced_shared = False
                break
            if time.monotonic() >= deadline and fits:
                waited = time.monotonic() - started
                logger.warning(
                    "Subagent %s: starting a dedicated process under macOS memory "
                    "pressure (%s); waited %.0fs",
                    info.id,
                    platform_compat.memory_pressure_name(pressure),
                    waited,
                )
                sel().log_tool_invocation(
                    session_key=info.parent_session_key or "",
                    source="subagent",
                    tool_name="spawn_run",
                    outcome="dedicated_start_under_memory_pressure",
                    metadata={
                        "memory_pressure_level": pressure,
                        "waited_secs": round(waited, 1),
                        "subagent_id": info.id,
                    },
                )
                info._start_priced_shared = False
                break
            if time.monotonic() >= deadline:
                waited = time.monotonic() - started
                logger.warning(
                    "Subagent %s: starting a dedicated process with %s, "
                    "below the %.2f GB its start needs; waited %.0fs",
                    info.id,
                    (
                        "memory headroom unknown (the host reading did not answer)"
                        if unanswered
                        else f"{avail:.2f} GB available"
                    ),
                    need,
                    waited,
                )
                sel().log_tool_invocation(
                    session_key=info.parent_session_key or "",
                    source="subagent",
                    tool_name="spawn_run",
                    outcome="dedicated_start_below_floor",
                    metadata={
                        # No figure when nothing was read: -1 is not an amount.
                        **({"cause": cause} if unanswered else {"available_gb": avail}),
                        "min_gb": need,
                        "start_price_gb": info._start_price_gb,
                        "waited_secs": round(waited, 1),
                        "subagent_id": info.id,
                    },
                )
                info._start_priced_shared = False
                break
            await asyncio.sleep(
                min(_DEDICATED_TOPUP_POLL_SECS, max(0.0, deadline - time.monotonic()))
            )
    finally:
        info._topup_waiting = False
        if held:
            lock.release()
        # Resume the start clock: the wait was admission's cost, not this
        # start's -- including a cancel that lands mid-wait, so a respawn
        # never inherits a paused clock.
        self._manager._gate_exit_reset(info)(0.0)


def _gate_exit_reset_impl(self: RunEventCoordinator, info: SubagentInfo) -> "Callable[..., None]":
    """The ``on_gate_acquired`` callback for *info*'s start queues.

    A start's clock PAUSES while it waits for a permit and resumes when it gets
    one: the startup watchdog (:meth:`_is_startup_stalled`) measures from
    ``_exec_started`` minus ``_start_queue_wait_ms``, the time spent queued
    in total, so only real start work counts against the deadline and a start
    overtaken in a queue is not reaped for the wait. ONE definition for both
    start paths: the dedicated path (``_run_inner`` -> ``get_or_create`` ->
    ``AcpProvider``) fires it at all three start queues -- the cold-start
    semaphore, the spawn admission and the ``session/new`` gate -- and the
    shared path (``_create_shared_session``) at the ``session/new`` gate and,
    when it needs the parent's companion runtime, at that runtime's per-parent
    lock and its spawn's admission (never across the spawn's own work).
    :meth:`_gate_wait_mark_impl` marks queue ENTRY; between the two the
    watchdog reads the clock as paused. The paused total is itself bounded
    (``monitoring._START_QUEUE_MAX_SECS``), so a start parked behind holders
    no watchdog bounds still ends.
    """
    # Imported here: a rebound ``_impl`` resolves globals in ``kiro_crew.subagent``.
    from kiro_crew.start_priority import START_QUEUE_LOG_MIN_MS, START_QUEUE_SESSION_NEW

    def _on_gate_acquired(queue_wait_ms: float, queue: str = START_QUEUE_SESSION_NEW) -> None:
        now = time.time()
        mono = time.monotonic()
        # The pause is measured on the watchdog's own clock (``time.time()``,
        # from the mark), not on the queue's monotonic wait: the two disagree
        # by however long the host was suspended during the wait, and a laptop
        # that slept in a gate queue would have that sleep charged as start
        # time. The queue's own measurement is the log's, and the fallback for
        # a grant whose entry was never marked. The monotonic twin pairs with
        # ``_exec_started_mono`` the same way.
        marked = info._gate_wait_started
        marked_mono = info._gate_wait_started_mono
        info._gate_wait_started = None
        info._gate_wait_started_mono = None
        info.last_activity = now
        info._last_activity_mono = mono
        info._start_queue_wait_ms += (
            max(0.0, now - marked) * 1000.0 if marked is not None else float(queue_wait_ms)
        )
        info._start_queue_wait_mono_ms += (
            max(0.0, mono - marked_mono) * 1000.0
            if marked_mono is not None
            else float(queue_wait_ms)
        )
        if queue_wait_ms >= START_QUEUE_LOG_MIN_MS:
            logger.info(
                "Subagent %s: waited %.0fms at the %s queue (start clock paused; "
                "%.0fms queued in total)",
                info.id,
                queue_wait_ms,
                queue,
                info._start_queue_wait_ms,
            )

    return _on_gate_acquired


def _gate_wait_mark_impl(self: RunEventCoordinator, info: SubagentInfo) -> "Callable[..., None]":
    """The ``on_gate_queued`` callback for *info*'s start queues.

    Fires immediately before a wait for a start-queue permit begins. It stamps
    ``_gate_wait_started``, and while that is set the startup watchdog reads the
    start clock as paused at that moment: a run queued for a permit is not
    starting, and the queue's length is set by the starts ahead of it. Same ONE
    definition for both start paths as :meth:`_gate_exit_reset_impl`, which
    clears the mark and adds the wait to the paused total.
    """
    from kiro_crew.start_priority import START_QUEUE_SESSION_NEW

    def _on_gate_queued(queue: str = START_QUEUE_SESSION_NEW) -> None:
        info._gate_wait_started = time.time()
        info._gate_wait_started_mono = time.monotonic()

    return _on_gate_queued


async def _create_shared_session_impl(
    self: RunEventCoordinator,
    info: SubagentInfo,
    session_key: str,
    agent: str,
) -> "LLMProvider":
    """Create a subagent session on the parent's AcpRuntime.

    The parent session (provider=kiro) runs on an AcpRuntime via
    AcpSessionProvider. Subagents create additional sessions on that SAME
    runtime — one process hosts everything. Falls back to
    get_subagent_runtime() (companion runtime) if the parent doesn't use
    AcpSessionProvider. Marks info._session_sharing=True so cleanup calls
    provider.shutdown() instead of SessionManager.release/reset.
    """

    # The clock pauses at queue ENTRY (``_gate_wait_mark``) and resumes at
    # queue EXIT (``_gate_exit_reset``); the dedicated-process path in
    # ``_run_inner`` installs the same pair at every start queue.
    _on_gate_acquired = self._manager._gate_exit_reset(info)
    _on_gate_queued = self._manager._gate_wait_mark(info)
    runtime = self._manager._get_parent_runtime(info.parent_session_key)
    if runtime is None:
        # Paused only while this start WAITS for the parent's companion
        # runtime: on the per-parent lock behind a sibling spawning it, and
        # at its own spawn's admission. Its own spawn work (kill of a dead
        # runtime, process spawn, ``initialize``) stays on the running clock,
        # which the deadline budgets, so a spawn that hangs is reaped.
        runtime = await self._manager._sessions.get_subagent_runtime(
            info.parent_session_key,
            on_gate_queued=_on_gate_queued,
            on_gate_acquired=_on_gate_acquired,
        )
    if runtime is None:
        raise RuntimeError("no shared runtime available for session sharing")
    shared_runtime: AcpRuntime = runtime

    cwd = info.cwd or str(getattr(self._manager._sessions, "_pool_cwd", ""))

    async def _late_adopter(handle: Any) -> bool:
        # A late session/new answer arrived. Keep the session only when this
        # run is still waiting for exactly that: a run that was stopped,
        # reaped or shut down in the meantime lets the collector tear it down.
        if (
            info.user_stopped
            or info.reaped
            or info._reap_started
            or info.done
            or self._manager._shutting_down
        ):
            return False
        provider = await self._manager._bind_shared_handle(
            info, session_key, shared_runtime, handle
        )
        info._late_start_provider = provider
        return True

    handle = await runtime.create_session(
        cwd=cwd or None,
        agent=agent or None,
        # THE reason this item exists: the subagent runs on the parent's
        # kiro-cli process, so every process-tree identity source answers
        # with the PARENT slot. Naming the owner here binds this session's
        # stub token to the subagent before its stubs register, so the
        # subagent cannot act as — or be re-pointed at — its parent.
        session_key=session_key,
        memory_mode=info.memory_mode,
        on_gate_acquired=_on_gate_acquired,
        on_gate_queued=_on_gate_queued,
        late_adopter=_late_adopter,
    )
    return await self._manager._bind_shared_handle(info, session_key, runtime, handle)


async def _await_late_start_impl(
    self: RunEventCoordinator, info: SubagentInfo, session_key: str, exc: Exception
) -> "LLMProvider":
    """Wait for the StartCollector owning a timed-out ``session/new``.

    The row is ``recovering`` while the collector holds the request; a
    late answer adopted into this run continues it on that session, and
    any other verdict (torn down, abandoned, runtime dead) ends this start
    attempt with a typed error. Nothing here re-issues ``session/new`` or
    starts a dedicated process: the outstanding request is the one start
    this attempt owns.
    """
    collector = getattr(exc, "collector", None)
    if collector is None:
        # The request never reached the wire (nothing to own) -- a plain
        # start failure for the ladder.
        raise RuntimeError(f"start_timeout: {exc}")
    self._manager._admission.taskq_mark(info, "recovering")
    try:
        await self._manager._fire_event(
            "subagent_recovering",
            info,
            {
                "attempt": 1,
                "max": 1,
                "stop_reason": "session_start_timeout",
                "stop_class": "start_collecting",
            },
        )
    except Exception:
        logger.debug("subagent_recovering emit failed for %s", info.id, exc_info=True)
    logger.warning(
        "Subagent %s: session/new timed out; start collector owns req_id=%s for up to %gs",
        info.id,
        getattr(collector, "req_id", "?"),
        getattr(collector, "timeout", 0.0),
    )
    try:
        await asyncio.wait_for(
            collector.settled.wait(), timeout=float(getattr(collector, "timeout", 300.0)) + 5.0
        )
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"start_abandoned: session/new collector for {info.id} did not settle"
        ) from exc
    provider = getattr(info, "_late_start_provider", None)
    if getattr(collector, "outcome", None) == "adopted" and provider is not None:
        info._late_start_provider = None
        now = time.time()
        # The adopted session is this run's real start: its clock starts here,
        # so the queue waits the abandoned attempt accumulated are not its.
        info._exec_started = now
        info._exec_started_mono = time.monotonic()
        info._start_queue_wait_ms = 0.0
        info._start_queue_wait_mono_ms = 0.0
        info._gate_wait_started = None
        info._gate_wait_started_mono = None
        info.last_activity = now
        info._last_activity_mono = info._exec_started_mono
        # ``recovering`` is the lost-owner (claimable) state; the adopted
        # session is live under our lease, so the row leaves it now rather
        # than at the first stream event.
        self._manager._admission.taskq_mark(info, "running")
        info._taskq_running_marked = True
        logger.info("Subagent %s: adopted late session %s", info.id, collector.session_id)
        return provider
    raise RuntimeError(
        f"start_abandoned: session/new for {info.id} ended {collector.outcome or 'unsettled'} "
        f"({exc})"
    )


async def _bind_shared_handle_impl(
    self: RunEventCoordinator,
    info: SubagentInfo,
    session_key: str,
    runtime: "AcpRuntime",
    handle: Any,
) -> "LLMProvider":
    """Wrap a created session handle as this run's provider and publish its identity.

    Shared by the direct create path and a late adoption: both end with a
    live handle whose ownership must be recorded before any cancellable
    await, so force-reap always finds and destroys it.
    """
    # A subagent's provider never rekeys either, and its whole point is that
    # it is NOT its parent: without the key its re-claim carries none and
    # gatewayd drops it, leaving this session unable to re-bind its token.
    provider = AcpSessionProvider(handle, runtime, session_key=session_key)
    provider.memory_mode = info.memory_mode
    # The handle exists now. Publish ownership before any cancellable await so
    # force-reap always takes the shared-session branch and destroys this handle
    # instead of resetting a nonexistent dedicated session.
    provider.child_fidelity_aware = True
    info._session_sharing = True
    info._shared_provider = provider
    # Capture cleanup identity before persistence or later setup can fail,
    # otherwise the live handle becomes an untracked ghost.
    cleanup_session_id = str(handle.session_id or "")
    # The backend that actually served this session, read from the provider
    # rather than fixed at kiro's label -- this path creates a session on
    # whatever backend the parent runs, so a constant here can only be right
    # for one of them.
    #
    # This is NOT what the continuation reads. ``_run_inner_impl`` re-captures
    # the label from the same provider immediately after session acquisition,
    # and that value is what reaches ``state.json`` and the corrected identity
    # record -- so a run that gets that far was always labelled correctly, on
    # every backend. What this write owns is the window BEFORE that re-capture:
    # a run cancelled in it leaves the identity record claiming kiro for a
    # session some other host holds, and the tombstone prune then takes
    # ``_cleanup_session_files_sync``'s kiro branch, unlinks a path that was
    # never going to exist, and reports cleanup SUCCEEDED -- where the label it
    # should have carried reports "no cleanup route for this provider" and keeps
    # the retry metadata. Fail-closed is the behaviour that constant was quietly
    # spending.
    #
    # ``_provider_label_of`` resolves it through ``PROVIDER_LABEL_BY_BACKEND``,
    # so a harness added later is one table row rather than one more branch here
    # (harness-parity H13).
    cleanup_provider = self._manager._provider_label_of(provider)
    setattr(info, "_session_id", cleanup_session_id)
    setattr(info, "_session_provider", cleanup_provider)
    self._publish_identity(
        info.id,
        session_id=cleanup_session_id,
        provider=cleanup_provider,
        keep=info.keep,
        conversation_key=session_key if info.keep else "",
    )
    try:
        await self._remember_identity_off_loop(
            info,
            session_id=cleanup_session_id,
            provider=cleanup_provider,
            keep=info.keep,
            conversation_key=session_key if info.keep else "",
        )
    except (OSError, ValueError, RecursionError):
        logger.debug(
            "Shared-session identity persistence failed for %s",
            info.id,
            exc_info=True,
        )
    if runtime.pid:
        info._pid = runtime.pid
        # Out of startup (see the dedicated-process PID record in
        # ``_run_inner``): wake a spawn the in-startup bound is holding.
        self._manager._note_startup_progress(info)
        try:
            # Keep the shared handle alive on a storage error, but route the
            # write through the run-owned off-loop drain so cancellation
            # cannot detach a stale whole-file writer.
            await self._manager._write_state_off_loop(
                info, "PID record", pid=runtime.pid, pid_recorded_at=time.time()
            )
        except Exception:
            logger.debug(
                "Shared-session PID persistence failed for %s",
                info.id,
                exc_info=True,
            )
    logger.info(
        "Subagent %s using session sharing on runtime PID %s (session %s, key %s)",
        info.id,
        runtime.pid,
        handle.session_id,
        session_key,
    )
    return provider


def _get_parent_runtime_impl(
    self: RunEventCoordinator, parent_session_key: str
) -> "AcpRuntime | None":
    """Extract the AcpRuntime from the parent session's provider.

    Returns the runtime if the parent uses AcpSessionProvider (kiro unified
    path), or None if the parent uses AcpClient (CC or legacy).
    """
    provider = self._manager._sessions.get_provider(parent_session_key)
    if provider is None:
        return None
    inner = getattr(provider, "client", None) or getattr(provider, "_client", None)
    if isinstance(inner, AcpSessionProvider):
        return inner._runtime
    return None
