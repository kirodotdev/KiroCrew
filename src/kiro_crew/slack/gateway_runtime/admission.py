"""Admission and overload control around the subagent manager and task runner.

Opening durable subagent dispatch and the dashboard workers after the memory
fence, child liveness for the crew log, the adaptive concurrency controller and the
overload-health sources it publishes, the subagent dependency coordinator, and the
two-pass runner task admission over the task store.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import (
        AdaptiveController,
        Any,
        GatewayOrchestrator,
        KiroCrewConfig,
        logger,
    )


async def _start_subagent_dispatch_after_memory_ready(self: GatewayOrchestrator) -> None:
    """Open the durable subagent queue only after the memory fence completes.

    A recovered run prepares its memory store first, so admitting it during
    the barrier either fails it on ``MemoryStartupUnavailable`` or starts it
    without its learned memory. Same shape as the cron and heartbeat guards:
    an unprepared fence is a programming error here, not a wait.
    """
    if self.subagent_mgr is None:
        return
    startup = getattr(self, "_memory_startup", None)
    if startup is not None and (startup.stopped or not startup.ready):
        raise RuntimeError(
            "Subagent queue dispatch cannot start before memory preparation completes"
        )
    self.subagent_mgr.release_queue_dispatch()


def _register_child_liveness(self: GatewayOrchestrator) -> None:
    """Give the crew log's repair a way to ask whether a child still runs.

    The repair may close an unmatched ``subagent/spawned`` only for a child
    with no outcome still coming, and this manager is the only thing that
    knows which children are still running. It is registered from here rather
    than inside the emitter, which cannot reach the manager -- the dependency
    already runs in this direction, since the subagent side is what calls the
    emitters.

    Called AFTER the ``KIROCREW_READY`` print and never from an ``_init_*`` on
    the boot path. Importing the emitter pulls the crew log store in with it, and
    the ``no-new-work-on-gateway-boot-path`` rule counts an optional, flag-off
    subsystem's import as boot work whatever the handler checks later; gating
    the import behind the flag would satisfy the rule only while the flag is
    off. Nothing needs the probe before this point: the repair runs when a
    session opens its crew log, which is after readiness.

    Registered UNCONDITIONALLY once here, because the flag is read at emit time
    and a probe installed while the crew log is off costs nothing, while making
    the registration itself conditional would leave a later flag flip with no
    probe and a repair free to close a live child.
    """
    from kiro_crew.crew_log import emit as crew_log_emit

    def _child_still_running(agent_id: str) -> bool:
        mgr = self.subagent_mgr
        if mgr is None:
            # No registry to consult, so this cannot report a child finished.
            # "Nothing is running" would be the same answer as a genuinely
            # empty registry and would let the repair close a live child.
            return True
        return any(info.id == agent_id for info in mgr.running)

    crew_log_emit.set_child_liveness(_child_still_running)


def _start_adaptive_controller(
    self: GatewayOrchestrator, cfg: KiroCrewConfig | None = None
) -> None:
    """Run the adaptive concurrency controller beside the subagent manager.

    It bounds the manager's live cap beneath the user's ceiling and moves
    the MCP daemon's spawn gate through ``GatewayManager.set_spawn_capacity``
    (read through ``self._mcp_gateway_manager`` at call time, so a broker
    that starts or restarts later is picked up without rewiring).
    """
    if self.subagent_mgr is None or self._adaptive_controller is not None:
        return
    from kiro_crew.config import live

    cfg = cfg if cfg is not None else self._cfg
    if not getattr(cfg.agent, "adaptive_concurrency", True):
        if getattr(self, "_adaptive_start_sub", None) is None:
            self._adaptive_start_sub = live.watch_object(
                self,
                "agent.adaptive_concurrency",
                method="_start_adaptive_controller",
                name="GatewayAdaptiveStart",
            )
        return
    # Inside the ENABLED branch, not at module scope: a module-level import
    # here drags ``adaptive.{controller,policy,signals}`` into every
    # importer of ``slack.gateway`` -- the boot path -- for an operator who
    # turned the controller off, which the AUTOSDE boot-path rule forbids
    # for a subsystem a disabled switch never uses. The watch above is what
    # brings the import back when the switch flips, so gating the import
    # costs a disabled host nothing and an enabled one one lazy load.
    from kiro_crew.adaptive import controller as adaptive_controller
    from kiro_crew.adaptive.controller import AdaptiveController

    async def _set_gate(capacity: int) -> int | None:
        mgr = self._mcp_gateway_manager
        return None if mgr is None else await mgr.set_spawn_capacity(capacity)

    async def _gate_stats() -> dict:
        mgr = self._mcp_gateway_manager
        return {} if mgr is None else await mgr.stats()

    cfg_gw = cfg.mcp_gateway  # type: ignore[union-attr]  # narrowed above
    try:
        controller = AdaptiveController(
            self.subagent_mgr,
            cfg=cfg,
            set_gate_capacity=_set_gate,
            read_gate_stats=_gate_stats,
            gate_initial=int(getattr(cfg_gw, "spawn_concurrency_initial", 4)),
            gate_floor=int(getattr(cfg_gw, "spawn_concurrency_min", 1)),
            gate_ceiling=int(getattr(cfg_gw, "spawn_concurrency_max", 8)),
        )
        controller.start()
    except Exception:
        logger.warning("adaptive concurrency controller failed to start", exc_info=True)
        return
    self._adaptive_controller = controller
    subscription = getattr(self, "_adaptive_start_sub", None)
    if subscription is not None:
        subscription.cancel()
        self._adaptive_start_sub = None
    adaptive_controller.register(controller)
    self._wire_overload_health(controller)


def _wire_overload_health(self: GatewayOrchestrator, controller: AdaptiveController) -> None:
    """Publish the controller's caps and degrade reason to session health,
    and the subagent manager's dependency coordinator to the process.

    ``session_health`` renders ``effective_caps`` per lane and one
    ``degrade_reason``; both are pulled through the sources registered
    here at compute time, so the health payload never holds a stale copy.
    The degrade reason is a closed-set token (``adaptive_<action>``) because
    it is also a metric attribute. The dependency coordinator is whatever
    the manager built beside its task store; ``register_coordinator`` is
    how a caller with no task row (main chat, monitors) reads the shared
    ``retry_at`` for a scope.
    """
    from kiro_crew.dashboard import session_health
    from kiro_crew.taskq import dependency as taskq_dependency

    def _spawn_gate_cap() -> dict | None:
        state = controller.state()
        if not state.get("enabled"):
            return None
        return {
            "effective": state.get("spawn_gate_capacity"),
            "applied": state.get("applied_gate_cap"),
            "pending": state.get("gate_pending"),
            "floor": state.get("gate_floor"),
            "ceiling": state.get("gate_ceiling"),
        }

    def _exec_cap() -> dict | None:
        state = controller.state()
        if not state.get("enabled"):
            return None
        return {
            "adaptive": state.get("effective_exec_cap"),
            "ceiling": state.get("exec_ceiling"),
            "paused": bool(state.get("paused")),
            "probing": bool(state.get("probing")),
        }

    def _degrade_reason() -> str | None:
        state = controller.state()
        if state.get("paused"):
            return "adaptive_pause"
        if state.get("probing"):
            return "adaptive_probe"
        last = state.get("last") or {}
        action = last.get("action") if isinstance(last, dict) else None
        return "adaptive_decrease" if action == "decrease" else None

    monitor = session_health.default_monitor()
    monitor.register_cap_source("spawn_gate", _spawn_gate_cap)
    monitor.register_cap_source("subagents", _exec_cap)
    monitor.register_pressure_source(_degrade_reason)

    coordinator = self._subagent_dependency_coordinator()
    if coordinator is not None:
        taskq_dependency.register_coordinator(coordinator)


def _subagent_dependency_coordinator(self: GatewayOrchestrator) -> Any:
    """The subagent manager's ONE dependency coordinator, or None.

    None while the manager's store is still opening off the loop: the
    coordinator's whole point is a schedule over durable rows.

    Read on the loop by the wiring passes, so the FIRST build -- which reads
    every waiting row -- is paid for off-loop by
    :meth:`_ensure_subagent_coordinator` before each of them.
    """
    coordinator = getattr(self.subagent_mgr, "dependency_coordinator", None)
    if callable(coordinator):
        coordinator = coordinator()
    return coordinator


async def _ensure_subagent_coordinator(self: GatewayOrchestrator) -> None:
    """Build the manager's dependency coordinator on the store's writer
    thread, before the loop-side wiring asks for it.

    A no-op with no manager, with the store still opening (there is nothing
    to schedule yet -- the second wiring pass binds it then), and once one
    exists.
    """
    mgr = self.subagent_mgr
    if mgr is None:
        return
    entry = getattr(mgr, "dependency_coordinator_async", None)
    if entry is None:
        return
    await entry()


def _unwire_overload_health(self: GatewayOrchestrator) -> None:
    """Drop the health sources and the coordinator handle at shutdown so a
    late health read reports no caps instead of a stopped controller's."""
    from kiro_crew.dashboard import session_health
    from kiro_crew.taskq import dependency as taskq_dependency

    session_health.default_monitor().clear_sources()
    taskq_dependency.register_coordinator(None)
    self._unwire_runner_admission()


def _wire_runner_admission(self: GatewayOrchestrator) -> None:
    """Put TaskRunner steps and workflow ``ctx.agent()`` calls on the task queue.

    First of two passes, run while the dashboard socket is being bound so
    both consumers have their admission -- and therefore the typed
    ``task_store_unavailable`` refusal -- from the moment they can serve a
    request. One :class:`RunnerAdmission` over the subagent manager's store
    and effective cap, attached to the TaskRunner and the WorkflowService,
    with the lane's raise edge registered on the manager (see
    :meth:`SubagentManager.set_cap_raise_listener`).

    The manager's store may still be opening off the loop here, in which
    case there is no dependency coordinator yet and no rows to adopt;
    :meth:`_runner_admission_store_ready` is the second pass that binds
    both once there is. Until then the admission's own ``tick`` is what
    wakes its time-based waits, from the reaper sweep -- which is also the
    steady state when the durable queue is OFF for good.
    """
    mgr = self.subagent_mgr
    if mgr is None:
        return
    try:
        from kiro_crew.recovery.ladder import default_ladder
        from kiro_crew.taskq.adapters.runner import runner_admission_for

        coordinator = self._subagent_dependency_coordinator()
        admission = runner_admission_for(
            mgr, cfg=self._cfg, ladder=default_ladder(), coordinator=coordinator
        )
        # Before anything else in here, so a manager that cannot take the
        # raise edge leaves nothing half-wired: the lane's ceiling is this
        # manager's cap whether or not a coordinator exists yet.
        mgr.set_cap_raise_listener(admission.lane.pump)
        # Unconditional too, and NOT part of the fallback ``tick``: a
        # terminal write the store refused holds its row ``running`` under
        # this incarnation's lease, so it must be replayed while the process
        # lives, and a bound coordinator is exactly what stops ``tick``
        # (the adapter's own replay) from ever running again.
        setattr(mgr, "_runner_terminal_write_retry", admission.retry_terminal_writes)
        if coordinator is not None:
            self._subscribe_runner_admission(coordinator, admission)
        else:
            setattr(mgr, "_runner_admission_tick", admission.tick)
    except Exception:
        logger.warning("runner task admission not wired", exc_info=True)
        return
    self._runner_admission = admission
    self._attach_runner_admission_consumers(admission)


def _subscribe_runner_admission(coordinator: Any, admission: Any) -> None:
    """Put the runner's waiters on the coordinator's per-scope schedule, so
    a 429 seen by a TaskRunner step and one seen by a sub-agent share ONE
    retry instant. A give-up is delivered as a wake: the waiter reads the
    row, finds it terminal and stops retrying."""
    coordinator.subscribe(
        on_wake=admission.on_wake,
        on_fail=lambda task_id, _reason: admission.on_wake(task_id),
    )


def _attach_runner_admission_consumers(self: GatewayOrchestrator, admission: Any) -> None:
    """Hand *admission* (or ``None``, to detach) to both runner consumers.

    Attaching is also what runs each consumer's orphan-adoption sweep, and
    a sweep has rows only once the store exists -- so record whether THIS
    attach carried one. Adopting nothing is not the same as having adopted.
    """
    if admission is not None:
        self._runner_admission_adopted = admission.store is not None
    if self.task_runner is not None:
        self.task_runner.attach_task_admission(admission)
    workflow_service = getattr(self.dashboard_state, "workflow_service", None)
    if workflow_service is not None:
        workflow_service.attach_task_admission(admission)


async def _runner_admission_store_ready(self: GatewayOrchestrator) -> None:
    """Second pass: bind what the first pass had no store for. Idempotent.

    Called once the manager's store is attached. A boot that never lost the
    race passes straight through it -- the coordinator is already bound and
    the sweep already ran. A boot that did lose the race gets, HERE, the
    three things that were unavailable at socket-bind time: the shared
    coordinator (the runner's only wake path once a store exists, because
    the reaper pump stops calling the fallback ``tick`` from the moment
    there are rows), the wake / give-up subscription, and the consumers'
    adoption sweep over the rows a dead incarnation left behind.

    Runs while both consumers are still idle: the sweep settles any
    unleased ACTIVE row, so it must not race live work.
    """
    mgr = self.subagent_mgr
    admission = getattr(self, "_runner_admission", None)
    if mgr is None or admission is None:
        return
    try:
        coordinator = await mgr.dependency_coordinator_async()
        if coordinator is not None and admission.coordinator is None:
            admission.attach_coordinator(coordinator)
            self._subscribe_runner_admission(coordinator, admission)
            if hasattr(mgr, "_runner_admission_tick"):
                delattr(mgr, "_runner_admission_tick")
            # A wait parked through the ledger between the coordinator's
            # own build and this handover is in neither schedule: the
            # build-time rebuild ran before it, and an attached
            # coordinator stops ``tick`` scanning the ledger. Re-reading
            # the rows AFTER the attach leaves no such window, because a
            # park from here on reports to the coordinator itself.
            store = admission.store
            if store is not None:
                restored = await store.run(coordinator.rebuild)
                if restored:
                    logger.info(
                        "runner admission: %d dependency waiter(s) rejoined on store ready",
                        restored,
                    )
        if admission.store is not None and not self._runner_admission_adopted:
            self._attach_runner_admission_consumers(admission)
    except Exception:
        logger.warning("runner task admission not bound to its store", exc_info=True)


def _unwire_runner_admission(self: GatewayOrchestrator) -> None:
    admission = getattr(self, "_runner_admission", None)
    if admission is None:
        return
    self._runner_admission = None
    self._runner_admission_adopted = False
    self._attach_runner_admission_consumers(None)
    mgr = self.subagent_mgr
    if mgr is None:
        return
    mgr.set_cap_raise_listener(None)
    if hasattr(mgr, "_runner_admission_tick"):
        delattr(mgr, "_runner_admission_tick")
    if hasattr(mgr, "_runner_terminal_write_retry"):
        delattr(mgr, "_runner_terminal_write_retry")


def _start_dashboard_workers_after_memory_ready(self: GatewayOrchestrator) -> None:
    """Start dashboard workers whose restored jobs may enter memory."""
    if self.dashboard_state is None:
        return
    resume_channel_agents = self.dashboard_state.resume_channel_agents
    self.dashboard_state.resume_channel_agents = None
    if resume_channel_agents is not None:
        resume_channel_agents()
