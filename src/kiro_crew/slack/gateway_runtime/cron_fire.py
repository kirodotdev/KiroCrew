"""Build the cron service and the one callback it fires for every job.

``_init_cron`` creates the service with ``_cron_callback`` as ``on_job``. That callback
runs a crewmate's goal reminder, captures what the fire needs, checks the overlap
guard, and dispatches: the command and script arms, the message job's fire-time gate
and first-run tab, then the agent sequence or the single-agent turn. This module
also owns what a fire needs before its turn runs: the capture, resolving a crew alias
to the agent it dispatches, and acquiring the session that agent runs in; and
arming the scheduler once memory is ready, and saying when startup moved an
unreadable cron store aside.

Orchestrator fields read: ``_cfg``, ``_no_crons``, ``_running_script_ids``,
``ctx_builder``, ``dashboard_state``, ``sessions``.
Orchestrator fields written: ``_cron_armed``, ``_cron_quarantine``,
``_cron_reconciled``, ``_cron_runs_holding`` (a message run's task, from before its
first acquire to its end), ``cron_svc``.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    import asyncio

    from kiro_crew.execution_context import ExecutionContext
    from kiro_crew.slack.gateway import (
        CronJob,
        CronService,
        GatewayOrchestrator,
        LLMProvider,
        Path,
        _await_cron_fire_time_gate,
        _pre_create_cron_slot,
        _retire_sessions_on_identity_change,
        agent_sequence_dispatches,
        build_cron_session_context,
        cron_job_env_without_reserved,
        data_home,
        live,
        logger,
        provider_model_pin_refused,
        redact,
        redact_credentials,
        redact_exfiltration_urls,
        sel,
    )


class _CronCapture(NamedTuple):
    """What a cron fire captured before it dispatches; see ``_cron_capture``."""

    session_key: str
    msg: str
    cron_agents: list[str]
    cron_execution: ExecutionContext
    cron_memory_store: str
    cron_agent: str
    default_substituted: bool
    # The previous run's result framed as untrusted data, for build_message's
    # request_prefix_context (``cron_prior_result_context``); "" when none.
    prior_result: str = ""
    # The agent name the governance gate is handed: the alias a chat turn on
    # this agent hands it, read before a crewmate's worker swap. "" when unset.
    gate_agent: str = ""


def _cron_extra_env(job: CronJob) -> dict[str, str] | None:
    """job.env plus KIROCREW_APPROVAL_MODE when the job runs auto.

    SubagentManager.spawn's own auto-approve fallback for a cron's
    spawn_run subagents (parent_trusted, i.e.
    sessions.get_approval_policy(parent_session_key)=="auto")
    depends on parent_session resolving back to this cron's session
    key -- an identity-plumbing path that can fail silently and
    leave the spawn stuck on the interactive approval path a cron
    has no responder for. Injecting the mode directly as an env var
    the spawned kiro-cli process inherits (mirroring
    KIROCREW_SESSION_KEY/KIROCREW_CHANNEL_ID) lets the spawn_run MCP
    tool (mcp_core.py) read and forward its own approval_mode
    explicitly, independent of whether parent_session resolution
    succeeds.

    KIROCREW_APPROVAL_MODE is a RESERVED control var: it is stripped
    from the app/user-controlled ``job.env`` on every path and only
    re-injected here when the job's VALIDATED ``approval_mode`` is
    "auto". Otherwise an app manifest could set it directly in
    ``job.env`` and have an interactive cron's spawn_run subagents
    silently auto-approved -- an authorization bypass.

    The two governance policy-path vars are reserved for the same
    reason, and the strip set is ``_CRON_RESERVED_ENV_KEYS``.
    """
    env = cron_job_env_without_reserved(job.env)
    if job.approval_mode == "auto":
        env["KIROCREW_APPROVAL_MODE"] = "auto"
    return env or None


def _annotate_model_downgrade(job: CronJob, text: str) -> str:
    # job.model is LLM-controllable via MCP; redact before it
    # reaches Slack/dashboard through last_result.
    safe_model = redact_credentials(redact_exfiltration_urls(job.model)[0])[0]
    return f"⚠️ Model '{safe_model}' unavailable; ran with default.\n\n" + text


async def _init_cron(self: GatewayOrchestrator, *, arm: bool = True) -> None:
    """Initialize the cron service, optionally arming it immediately."""

    async def _cron_callback(job: CronJob) -> str | None:
        # A crewmate's Daily Goal Reminder is not an ordinary cron turn: it
        # runs as one hidden turn in the crewmate's own thread, built from
        # its current goal record (dashboard.goal_reminder).
        from kiro_crew.dashboard import goal_reminder

        if goal_reminder.is_reminder_job(job):
            # It still starts an agent turn, so it passes the same fire-time
            # governance gate as a message job first: a `capabilities.cron`
            # ceiling tightened after the switch was turned on must stop it.
            gate_reason, gate_starved = await _await_cron_fire_time_gate(
                job, tool_name="cron_goal_checkin", tool_kind="cron_message"
            )
            if gate_starved:
                return None
            if gate_reason:
                # No record_failure(), as for the message-job deny below.
                job.last_status = "error"
                job.last_error = redact(gate_reason)
                job.fire_time_denied = True
                try:
                    sel().log_tool_invocation(
                        session_key=f"cron:{job.id}",
                        tool_name="cron_goal_checkin",
                        tool_kind="cron_message",
                        outcome="denied",
                    )
                except Exception:
                    logger.debug(
                        "SEL logging failed in goal check-in fire-time deny path",
                        exc_info=True,
                    )
                await self._alert_cron_failure(job, gate_reason, denied=True)
                return None
            outcome = await goal_reminder.fire(self.dashboard_state, job)
            # The run's summary in the job's history: a check-in delivers
            # nothing itself, so without it a skip ("the mate is busy") would
            # read as a success that said nothing, silent job or not.
            job.set_run_result(redact(outcome))
            return outcome

        cap = await self._cron_capture(job)

        # ── Concurrent execution guard ──
        if (job.script or job.command) and job.id in self._running_script_ids:
            logger.info("Cron '%s': previous execution still running, skipping", job.name)
            return None

        # ── Command mode: direct shell execution (sandboxed) ──
        if job.command:
            return await self._cron_run_command(job)

        # ── Code-based script execution (deterministic, no LLM) ──
        if job.script:
            result = await self._cron_run_script(job)
            if job.last_status == "error" and not getattr(job, "run_never_started", False):
                # A failed run writes no record, so the crewmate's page would keep the
                # last good one as current: the gateway records the failure there. A
                # run that never started (overlap guard, admission closed, pool
                # starved) is not a failure of the job, as the scheduler counts it.
                await self._record_bound_script_failure(job)
            return result

        # ── Fire-time governance gate: message (LLM) jobs ──
        # Command and script jobs are gated inside their blocks above; a job
        # reaching this point dispatches an LLM turn, so it is gated here too:
        # without this check, disabling the cron capability after scheduling
        # would leave message jobs firing. Same deny semantics as the other
        # kinds: mark the run failed, keep the job.
        # Off-loop for the same reason as the command/script sites above, and
        # on the GOVERNANCE pool for the same reason: a message job gets no
        # pool allowance on either deadline, so gating it on the cron pool
        # would charge a saturated pool's queue directly to its execution
        # budget.
        gate_reason, gate_starved = await _await_cron_fire_time_gate(
            job, tool_name="cron_message_dispatch", tool_kind="cron_message"
        )
        if gate_starved:
            return None
        if gate_reason:
            # No record_failure() — see the command-path deny above: a
            # policy denial must not feed the auto-pause counter.
            job.last_status = "error"
            job.last_error = redact(gate_reason)
            job.fire_time_denied = True
            try:
                sel().log_tool_invocation(
                    session_key=f"cron:{job.id}",
                    tool_name="cron_message_dispatch",
                    tool_kind="cron_message",
                    outcome="denied",
                )
            except Exception:
                logger.debug(
                    "SEL logging failed in cron message fire-time deny path", exc_info=True
                )
            await self._alert_cron_failure(job, gate_reason, denied=True)
            return None

        # ── First-run tab pre-create ──
        # The result injection at the end is the only other creator of the
        # job's dashboard tab, so a new job's first run would have none:
        # caller_slot_key refuses every session-control verb with
        # caller_unidentified, and sub-agent/widget/question/approval routing
        # finds no surface row. The tab must exist DURING the run. Eligibility
        # lives in the helper. After the fire-time gate, since a denied run
        # needs no tab; a run that then fails leaves an empty tab, accepted.
        # The guarded wrapper keeps a failed mint from killing or consuming a
        # one-shot run (see _pre_create_cron_slot).
        if self.dashboard_state:
            await _pre_create_cron_slot(self.dashboard_state, job)

        # ── Sequential agent execution ──
        # When agent_sequence has multiple agents, run them sequentially
        # with per-agent session keys and per-job env vars.
        if agent_sequence_dispatches(cap.cron_agents):
            dispatch = self._cron_run_sequence(job, cap)
        else:
            # ── Single-agent path (existing behavior) ──
            # Per-job env vars (single-agent path) travel via extra_env passthrough
            dispatch = self._cron_run_single(job, cap, retry=_cron_callback)
        from kiro_crew.shared_crew.admission_entry import admitted_cron_dispatch

        return await admitted_cron_dispatch(job, _held(dispatch))

    async def _held(dispatch: Any) -> Any:
        # ── The run's own holding ──
        # The task awaiting this is the cron run from before its first acquire
        # to its end, so a permit it holds is the run's own from the moment the
        # claim sets the session's turn_owner. The idle cleanup after a last
        # sub-agent (_reset_cron_parent_when_idle) reads that holder and leaves
        # a session this task holds to the run's finalizer. The outermost frame
        # owns the entry: a retry re-enters _cron_callback in the same task and
        # must not drop it when its own frame returns.
        run_task = asyncio.current_task()
        owned = run_task if run_task not in self._cron_runs_holding else None
        if owned is not None:
            self._cron_runs_holding.add(owned)
        try:
            return await dispatch
        finally:
            if owned is not None:
                self._cron_runs_holding.discard(owned)

    self._cron_reconciled = False
    self._cron_armed = False
    # An unparseable store refuses every write, and the app cron cleanup
    # below and in the dashboard's startup hooks writes to it, so the
    # gateway could not start. create() moves it aside instead.
    self.cron_svc = await CronService.create(
        base_dir=data_home(), on_job=_cron_callback, quarantine_unreadable=True
    )
    moved = getattr(self.cron_svc, "quarantined_store", None)
    self._cron_quarantine = moved if isinstance(moved, Path) else None
    if self.dashboard_state:
        self.cron_svc.set_refresh_callback(self.dashboard_state.push_refresh)
    # A run its deadline ends never returns to _cron_callback, whose failure
    # paths are the only ones that alert; the service hands it back here.
    self.cron_svc.set_timeout_alert(self._alert_cron_timeout)
    if self._no_crons:
        logger.info("Cron scheduler disabled (--no-crons)")
    else:
        # CronService.create has loaded durable jobs but has not armed a
        # timer yet. Remove jobs owned by disabled or execution-denied apps
        # at this boundary; if cleanup cannot complete, leave the entire
        # scheduler stopped rather than risk firing a denied command.
        from kiro_crew.apps.bridges import reconcile_app_crons_for_execution

        try:
            await reconcile_app_crons_for_execution(self.cron_svc)
        except Exception:
            logger.exception(
                "App cron execution reconciliation failed; refusing to arm " "the cron scheduler"
            )
            return
        self._cron_reconciled = True
        if arm:
            await self._start_cron_after_memory_ready()


async def _record_bound_script_failure(self: GatewayOrchestrator, job: CronJob) -> None:
    """Record *job*'s failed run in the crew log of the crewmate it is bound to.

    Only for a job bound to a crewmate (``member_id``). The identity is the
    scheduler's own record of the job, never anything the script said. BEST-EFFORT and
    off the loop: the recorder never raises, and a failure that cannot be recorded
    (no crew log yet, the crew panel off) changes nothing about the run.
    """
    import asyncio as _asyncio

    from kiro_crew.dashboard.handlers.agent_panel_record import record_cron_failure

    member_id = str(getattr(job, "member_id", "") or "")
    if not member_id:
        return
    try:
        await _asyncio.to_thread(
            record_cron_failure, str(job.id), member_id, str(job.last_error or "")
        )
    except Exception:
        logger.debug("Cron '%s': failure record skipped", job.name, exc_info=True)


async def _alert_cron_timeout(self: GatewayOrchestrator, job: CronJob, detail: str) -> None:
    """The service's timeout alert: record a bound script's failure, then alert.

    A run its deadline ends is cancelled inside ``_cron_run_script``, so the failure
    record after it in ``_cron_callback`` never runs; this is where that run is
    recorded instead. A run that never started is skipped, as there.
    """
    if getattr(job, "script", None) and not getattr(job, "run_never_started", False):
        await self._record_bound_script_failure(job)
    await self._alert_cron_failure(job, detail)


async def _cron_capture(self: GatewayOrchestrator, job: CronJob) -> _CronCapture:
    """What one cron fire captures before it dispatches anything.

    The session key and prompt ``build_cron_session_context`` picks, a snapshot of
    the agent sequence, the execution the run is bound to with its memory store, and
    the agent it dispatches: the configured default stands in for the bare floor
    only where that is safe. Every kind of job captures, before the overlap guard.
    """
    # helper picks stable vs ephemeral session key; a persistent job's previous
    # result is carried separately, framed as data, ahead of the request.
    session_key, msg = build_cron_session_context(job)
    from kiro_crew.cron_service.identity import cron_prior_result_context

    prior_result = cron_prior_result_context(job)

    from kiro_crew.cron import resolve_cron_memory
    from kiro_crew.execution_context import (
        execution_for_store,
        execution_from_record,
    )

    # Snapshot the job before yielding; reloading a cron cannot rebind it.
    cron_agents = list(job.agent_sequence)
    # Only an agent run writes a transcript under this key; tagging a script or
    # command job would create an empty transcript on every fire.
    if (
        not job.persistent_session
        and not (job.script or job.command)
        and not agent_sequence_dispatches(cron_agents)
    ):

        def tag_run_transcript() -> None:
            from kiro_crew.history import ConversationLog

            ConversationLog().update_metadata(session_key, {"cron_run": "ephemeral"})

        await asyncio.to_thread(tag_run_transcript)
    # ── Default-agent fallback (LLM jobs) ──
    # A job with no agent_id runs config.agent.default_agent, like the chat
    # transports. Otherwise dispatch falls to AcpClient's "kirocrew" floor,
    # which carries none of the default agent's MCP servers.
    #
    # Resolved above the branch and applied below it, to the dispatched agent
    # only, and only where that agent is still the bare "kirocrew" floor:
    #   * Above the branch, because every post-migration record carries
    #     `execution_context` and takes the other arm.
    #   * Not into the capture: `bind_session_execution` publishes it under the
    #     stable key `cron:{job.id}` and refuses a candidate that differs from
    #     the recorded one, so a changed `template_id` makes every later fire
    #     raise "session already belongs to another execution" until the job
    #     auto-pauses.
    #   * Floor-gated, because a captured `template_id` naming a real template
    #     is a deliberate selection (see the gate below).
    # job.agent_id is never mutated. A dispatching agent_sequence owns its own
    # dispatch (agent_sequence_dispatches); a lone entry is dormant and falls
    # through to agent_id. A non-str configured default degrades to the floor.
    _default_agent = ""
    # True once the dispatched agent came from ``agent.default_agent``.
    # That field is the TEMPLATE namespace (config/sections_agent.py, free-form
    # str, no alias validation) -- unlike the alias-namespaced default,
    # which has its own `default_agent_not_alias` guard. The collapse in
    # _resolve_cron_agent treats ANY name that keys ``cfg.agents`` as a
    # crew alias, so without this flag a default that merely shares a
    # name with a crew member would hand an agent-less cron that crew's
    # workspace and capability MCP servers -- a silent capability and
    # identity widening, announced at DEBUG only.
    _default_substituted = False
    _agentless_job = not (job.agent_id or "").strip() and not agent_sequence_dispatches(
        job.agent_sequence
    )
    if _agentless_job:
        try:
            # Prefer the last applied reload over the boot snapshot: the config
            # watcher adopts agent.default_agent live, while self._cfg is
            # rebuilt only by unrelated MCP handlers.
            _cfg_now = live.snapshot() or self._cfg
            _configured = _cfg_now.agent.default_agent
            if isinstance(_configured, str):
                _default_agent = _configured.strip()
        except Exception:
            _default_agent = ""
    if job.execution_context is not None:
        cron_execution = execution_from_record({"execution_context": job.execution_context})
    else:
        legacy_selection = CronJob(
            id=job.id,
            name=job.name,
            message=job.message,
            member_id=job.member_id,
            memory_store=job.memory_store,
            agent_id=job.agent_id,
        )

        def resolve_legacy_execution():
            # Both calls can load configuration. The worker sees only
            # captured selectors, never the scheduler's mutable job.
            resolve_cron_memory(legacy_selection, validate_memory_files=False)
            return execution_for_store(
                legacy_selection.memory_store,
                template_id=legacy_selection.agent_id or "kirocrew",
            )

        cron_execution = await asyncio.to_thread(resolve_legacy_execution)
    # The governance gate's agent is the name a chat turn on this agent hands
    # it. A capture that selected a member runs as that member whatever the job
    # names in agent_id (the dashboard saves a template there beside member_id;
    # cron_update may set another member's name), so the gate gets the member's
    # live alias, the one _resolve_cron_agent dispatches through
    # member_config_for_id, else its captured selection_name. Only a capture
    # that selected no member uses the job's own alias. Read here, before the
    # worker swap below rewrites a crewmate's selection to the worker template,
    # and kept apart from the dispatched template. A capture that selected
    # nothing falls back to the dispatched agent at the return, so a
    # substituted default is named as a chat turn on the default is.
    if cron_execution.member_id or cron_execution.selection_kind == "member":
        _gate_alias = ""
        if cron_execution.member_id:
            try:
                from kiro_crew.execution_context import member_config_for_id

                _gate_cfg = live.snapshot() or getattr(self, "_cfg", None)
                if _gate_cfg is not None:
                    _gate_alias, _ = member_config_for_id(_gate_cfg, cron_execution.member_id)
            except Exception:
                _gate_alias = ""
        if not _gate_alias and cron_execution.selection_kind == "member":
            _gate_alias = cron_execution.selection_name.strip()
    else:
        _gate_alias = (job.agent_id or "").strip()
    if not agent_sequence_dispatches(cron_agents):
        # A crewmate's scheduled turn is work, so it runs on that crewmate's
        # worker (its kit, servers and permissions), never on the conductor its
        # chat runs on. The key, store, member and mode are the capture's; only
        # the selection becomes the worker's. Read at every fire, so a job
        # captured before the crewmate owned its file needs no migration.
        from kiro_crew.mate_worker import cron_worker_execution

        # The live config the resolver below reads, never a reload: the capture
        # runs on the loop's behalf and a fire must not reread config.json.
        _cfg_live = live.snapshot() or getattr(self, "_cfg", None)
        worker_execution = (
            await asyncio.to_thread(cron_worker_execution, cron_execution, _cfg_live)
            if _cfg_live is not None
            else None
        )
        if worker_execution is not None:
            cron_execution = worker_execution
    cron_memory_store, cron_agent = (
        cron_execution.store.legacy_name,
        cron_execution.template_id,
    )
    # The configured default lands only here: cron_agent is read only by
    # _resolve_cron_agent and the `agent=` kwargs below, which no durable
    # record compares. Every gate is needed:
    #   * cron_agent is still the "kirocrew" floor (after .strip()). A schedule
    #     made from a template chat with no `agent` names its template only in
    #     the captured template_id (bind_cron_memory in cron_service/identity.py
    #     overwrites it only `if job.agent_id`). Overriding that would split
    #     dispatch from the captured store and from dispatched_agents_from_disk,
    #     the delete guard's view of dispatch.
    #   * No cron_execution.member_id. A member whose agent names no kiro_agent
    #     captures the floor spelling; substituting would inject the default
    #     template's prompt into that member's envelope and bill usage to it.
    #   * selection_kind != "member". A pre-migration member persists no
    #     member_id; selection_kind is the authoritative namespace.
    #   * No job.member_id. On the `execution_context is None` arm,
    #     execution_for_store rebuilds the capture as a "template" with a null
    #     member_id, so the job is the only evidence of a member selection.
    #     A truthy non-str declines, which keeps the floor.
    if (
        _default_agent
        and (cron_agent or "").strip() in ("", "kirocrew")
        and not job.member_id
        and not cron_execution.member_id
        and cron_execution.selection_kind != "member"
    ):
        # ── Spawn-allowlist divergence gate ──
        # The substitution reaches the dispatched `agent=` only, while
        # bind_session_execution publishes the UNSUBSTITUTED capture
        # under `cron:{job.id}` -- which it must, for the auto-pause
        # reason above. Both spawn entry points derive the parent's
        # declaration from that record alone: read_session_execution(
        # parent) -> parent_spawn_allowlists(execution.template_id), in
        # dashboard/handlers/messaging.py and the off-loop twin in
        # subagent.py. The floor spelling resolves to `()`, which
        # _vet_parent_available_agents admits as "no declaration to
        # honour" -- so a toolsSettings.subagent.availableAgents list on
        # the substituted template is never consulted and a child it
        # forbids starts.
        #
        # Honouring it would need a NON-DURABLE parent execution the
        # spawn gate reads. Until that exists, a default that declares an
        # allowlist is not substituted, and the job keeps the floor.
        #
        # DENY BY DEFAULT. A non-empty tuple declines, and so does the
        # reader's `None`. Only `()` -- "no readable spec declares this
        # name, and every spec file parsed" -- permits the substitution.
        #
        # `None` must not substitute on the theory that the spawn gate
        # refuses anyway because it asks about the CAPTURE's floor
        # spelling. That premise is false: `agent.py` generates and
        # installs `kirocrew.json` into the agents directory and
        # `config/defaults.json` declares `"name": "kirocrew"`, so a
        # readable spec DOES declare the floor name -- and
        # `parent_spawn_allowlists` answers `None` only when NO readable
        # spec declares the name while some file could not be read. For
        # the floor it therefore returns `()`, "no declaration to
        # honour", and the gate ADMITS. `None` is exactly the state where
        # a restrictive declaration may be sitting in the spec file the
        # hardened reader refused, in a shared user-writable directory,
        # so letting a falsy value skip the check is the deny-by-default
        # violation this clause exists to avoid.
        from kiro_crew.subagent import parent_spawn_allowlists as _spawn_allowlists

        _declared = await asyncio.to_thread(_spawn_allowlists, _default_agent)
        if _declared or _declared is None:
            logger.warning(
                "Cron '%s': default agent %r %s, and the captured "
                "execution's floor template cannot carry that declaration to "
                "the spawn gate; keeping the floor for this fire rather than "
                "dispatching a restricted agent whose allowlist would not be "
                "honoured",
                job.name,
                _default_agent,
                (
                    "declares toolsSettings.subagent.availableAgents"
                    if _declared
                    else "has an unreadable agent spec, so its "
                    "toolsSettings.subagent.availableAgents is unknown"
                ),
            )
        else:
            cron_agent = _default_agent
            _default_substituted = True
    return _CronCapture(
        session_key,
        msg,
        cron_agents,
        cron_execution,
        cron_memory_store,
        cron_agent,
        _default_substituted,
        prior_result,
        _gate_alias or cron_agent,
    )


def _resolve_cron_agent(
    self: GatewayOrchestrator,
    cron_execution: ExecutionContext,
    alias: str | None,
    *,
    template_namespace: bool = False,
    sequence_step: bool = False,
) -> "tuple[str | None, str | None, str | None]":
    """Resolve a cron agent alias to (kiro_agent, cwd, crew_alias).

    A channel-bound cron carries the channel's agent ALIAS (e.g. ``in-3d``),
    but kiro-cli accepts only a materialized agent mode, so the raw alias fails
    with "Agent mode 'in-3d' is not available". Like ``_turn_admit``'s
    ``_allocation_kwargs``, dispatch the alias's ``kiro_agent`` in its workspace
    ``cwd`` and return ``crew_alias`` for ``crew_agent=``. Without it
    ``resolve_crew_identity`` returns ``""`` and the crew's member gates,
    pinned model / reasoning effort and watchdog windows are skipped. Returns
    (None, None, None) on any miss, so the caller keeps the raw value.

    ``sequence_step`` marks an ``agent_sequence`` step, where ``alias`` is the
    step's own crew. The captured-member pin is then skipped so each step runs
    as its own crew; the captured memory store stays shared by every step.
    """
    if not alias:
        return None, None, None
    # A template-namespace name is NOT a crew alias, even when it
    # happens to key ``cfg.agents``. ``agent.default_agent`` is a
    # free-form str in the template namespace with no alias
    # validation (config/sections_agent.py), so an operator naming a crew
    # member there is ordinary input -- and the collapse below keys
    # only on membership in ``cfg.agents``, then returns the alias as
    # ``crew_agent``, which ``resolve_crew_identity`` honours
    # verbatim. That would give an agent-less cron the named crew's
    # workspace, pinned model and capability MCP servers, silently
    # apart from a DEBUG line. Return the EXPLICIT no-crew spelling
    # rather than the miss tuple the other early returns use: `""`
    # is what `resolve_crew_identity` documents as opting out of the
    # fallback ("an explicit crew_agent wins verbatim -- including
    # \"\""), while `None` is the fallback-ENABLED spelling, so a
    # `None` here would still let `if agent and agent in
    # config.agents` reselect the colliding crew (loader.py) and the
    # bypass would be a no-op. `kiro_agent`/`cwd` stay None so the
    # name dispatches as the TEMPLATE it was configured as, in the
    # default workspace.
    # Consequence to expect, and the intended one: a default that is
    # really only a crew alias now fails closed and loudly with
    # "Agent mode '<name>' is not available", instead of quietly
    # inheriting that crew's identity.
    if template_namespace:
        return None, None, ""
    try:
        from kiro_crew.config.loader import (
            resolve_agent_bindings,
            workspace_dir_from_entry,
        )

        cfg = getattr(self, "_cfg", None)
        # Prefer the last applied reload: an agent created at runtime is in
        # live.snapshot() but not in the boot self._cfg.
        cfg = live.snapshot() or cfg
        if cfg is None:
            return None, None, None
        agents = getattr(cfg, "agents", None) or {}
        # Resolve a member execution by IDENTITY, not name. The store was
        # captured at authoring; if a same-name alias was deleted and
        # recreated, a by-name lookup would bind the new member's runtime to
        # the retired member's store. member_config_for_id pins to the captured
        # member_id and raises on a mismatch, so we fall back to the raw value.
        # Skipped for an agent_sequence step, whose ``alias`` is its own crew.
        resolved_alias = alias
        captured_member = None if sequence_step else getattr(cron_execution, "member_id", None)
        from kiro_crew.mate_worker import MATE_WORKER_AGENT, is_own_worker_execution

        own_worker = not sequence_step and is_own_worker_execution(cron_execution)
        if captured_member:
            try:
                from kiro_crew.execution_context import member_config_for_id

                resolved_alias, _ = member_config_for_id(cfg, captured_member)
            except Exception:
                logger.debug(
                    "cron member identity %r not resolvable in live cfg; "
                    "leaving agent %r unchanged",
                    captured_member,
                    alias,
                    exc_info=True,
                )
                return None, None, None
        # A non-member execution collapses only a real Kiro Crew alias:
        # resolve_agent_bindings falls back to the default agent for an unknown
        # name, which would rewrite a real kiro mode (e.g. 'kirocrew-lite').
        elif alias not in agents:
            return None, None, None
        # validate_memory_files=False: this runs on the event loop, and the
        # default does a synchronous stat + SQLite read. The session validates
        # the store later.
        bindings = resolve_agent_bindings(cfg, resolved_alias, validate_memory_files=False)
        kiro_agent = bindings.kiro_agent or None
        if own_worker:
            # The crewmate's alias resolves to its chat agent, the conductor; a
            # scheduled turn runs on its worker instead, in the crewmate's
            # workspace. No crew alias: a worker is a template session whose
            # identity is its record (member, store), as a hand-off opens it.
            kiro_agent = MATE_WORKER_AGENT
        # Anchor the workspace dir by the one placement rule. The
        # resolved bindings.workspace_dir is the RAW configured value
        # (e.g. the shipped relative default "workspace"); handing
        # that to the provider as a cwd would resolve it against the
        # gateway PROCESS directory, not the data home. Resolve the
        # alias's workspace entry through workspace_dir_from_entry so
        # a relative dir anchors under config_dir() and an absolute
        # dir is honored as-is.
        agent_cfg = agents.get(resolved_alias)
        ws_name = getattr(agent_cfg, "workspace", None) if agent_cfg else None
        ws_entry = None
        if ws_name:
            ws_entry = getattr(cfg, "workspaces", {}).get(ws_name)
        # workspace_dir_from_entry(None) is the BASE workspace
        # directory under config_dir() — the documented answer for an
        # unmapped/empty workspace name (loader.py's workspace_dir_for
        # rule). Deliberately NOT cfg.default_workspace's dir: that
        # rule forbids an unmapped name hopping to whatever absolute
        # dir the default declares, so a missing mapping anchors to
        # the data home rather than escaping it (or defaulting to the
        # gateway process cwd via a None).
        ws_dir = workspace_dir_from_entry(ws_entry)
        cwd = str(ws_dir) if ws_dir else None
        # Carry the identity-resolved alias back as crew_alias:
        # prepare_runtime needs it to resolve the member identity (the
        # kiro_agent name alone is not a config.agents key), and it is
        # the alias pinned to the captured member_id above.
        return kiro_agent, cwd, ("" if own_worker else resolved_alias)
    except Exception:
        logger.debug("cron agent resolve failed for %r", alias, exc_info=True)
        return None, None, None


async def _acquire_with_model_fallback(
    self: GatewayOrchestrator,
    job: CronJob,
    cron_execution: ExecutionContext,
    key: str,
    agent_id: str | None,
    cwd: str | None = None,
    crew_agent: str | None = None,
    *,
    execution: ExecutionContext | None = None,
    replace_execution: bool = False,
) -> "tuple[LLMProvider, bool, bool, bool]":
    """get_or_create honoring job.model; if that model is
    unavailable, retry once with the registry default.
    Returns (client, is_new, resumed, downgraded).

    ``agent_id`` is the RESOLVED kiro agent mode (an alias must be
    collapsed via _resolve_cron_agent before this call), ``cwd``
    is that agent's workspace so the session runs in the right tree,
    and ``crew_agent`` is the original alias so prepare_runtime
    resolves the member identity (its capability gates, model /
    reasoning-effort pins, and watchdog windows). ``execution``
    overrides only the record published under the session key; its
    store and member identity must remain the captured run's.
    ``replace_execution`` admits a sequence step's template switch
    only while the durable record still matches the fresh read.
    """

    assert self.sessions is not None
    from kiro_crew.execution_context import (
        bind_session_execution,
        read_session_execution,
    )

    bound_execution = execution if execution is not None else cron_execution
    if replace_execution:
        expected_execution = await asyncio.to_thread(read_session_execution, key)
        await asyncio.to_thread(
            bind_session_execution,
            key,
            bound_execution,
            replace_existing=True,
            expected=expected_execution,
        )
    else:
        await asyncio.to_thread(bind_session_execution, key, bound_execution)
    # A crewmate's scheduled turn runs on its worker (``cron_worker_execution``),
    # so its key takes the worker's backend route (``select_provider_backend``:
    # the crewmate's member backend), as a hand-off's worker does.
    from kiro_crew.mate_worker import note_worker_session

    note_worker_session(key, bound_execution)
    modes = getattr(self.ctx_builder, "_session_memory_modes", None)
    if isinstance(modes, dict):
        # A separately scheduled run is durable work, not a child
        # conversation. Only this trusted dispatch admits its key.
        from kiro_crew.messaging.privacy_mode import strictest
        from kiro_crew.subagent_persistence import bind_session_memory_mode
        from kiro_crew.workflows.registry import _await_owned

        publication = asyncio.create_task(
            asyncio.to_thread(bind_session_memory_mode, key, bound_execution.memory_mode)
        )
        admitted_mode = await _await_owned(publication)
        modes[key] = strictest((admitted_mode, modes.get(key, "persistent"))) or "persistent"

    async def _rebind_prepared_execution(client: LLMProvider) -> None:
        if not replace_execution:
            return
        prepared_template = getattr(client, "loaded_capability_template", None)
        if (
            not isinstance(prepared_template, str)
            or not prepared_template
            or prepared_template == bound_execution.template_id
        ):
            return
        rebound = bound_execution.with_template(prepared_template, crew_agent or "")
        await asyncio.to_thread(
            bind_session_execution,
            key,
            rebound,
            replace_existing=True,
            expected=bound_execution,
        )

    # The dashboard's per-turn account gate, run before acquiring as
    # _run_chat runs it. A `cron:` key never claims a warm-pool
    # process, but a fire whose sub-agents are still running skips the
    # reset in its `finally`, and the next fire of a
    # `persistent_session` job, or of the same `agent_sequence` step,
    # reuses that live session. After an account switch or
    # `kiro-cli logout` outside the dashboard it still holds the
    # previous account, and a host where only crons run has no
    # dashboard turn to retire it. Passed as unattended: when the gate
    # cannot identify the signed-in account (a social login, an
    # unreadable store) the fire touches no session, since it could
    # not tell a stale one from a healthy one on any fire.
    await _retire_sessions_on_identity_change(self.dashboard_state, unattended=True)
    try:
        client, is_new, resumed = await self.sessions.get_or_create(
            key,
            agent=agent_id,
            crew_agent=crew_agent,
            channel_id=job.channel,
            approval_policy=job.approval_mode,
            model=job.model or None,
            extra_env=_cron_extra_env(job),
            cwd=cwd,
        )
        await _rebind_prepared_execution(client)
        # A config-option backend refuses a pin without raising and
        # stays on its default: the same downgrade as the except
        # below, so report it the same way.
        return (
            client,
            is_new,
            resumed,
            bool(job.model) and provider_model_pin_refused(client),
        )
    except Exception as model_exc:
        if not job.model:
            raise
        # Only fall back when the failure plausibly implicates the
        # pinned model; unrelated session-creation errors (provider
        # spawn, missing factory, transient I/O) must propagate so
        # they are not misreported as a model downgrade.
        _err = str(model_exc).lower()
        if "model" not in _err and job.model.lower() not in _err:
            raise
        logger.warning(
            "Cron '%s': model %r unavailable (%s); retrying with default",
            job.name,
            job.model,
            model_exc,
        )
        client, is_new, resumed = await self.sessions.get_or_create(
            key,
            agent=agent_id,
            crew_agent=crew_agent,
            channel_id=job.channel,
            approval_policy=job.approval_mode,
            extra_env=_cron_extra_env(job),
            cwd=cwd,
        )
        await _rebind_prepared_execution(client)
        return client, is_new, resumed, True


def _retained_session_agent_mismatch(
    self: GatewayOrchestrator, job: CronJob, key: str, agent: str | None
) -> str | None:
    """Return the retained agent only on positive mismatch evidence."""
    live_agent = None
    try:
        reader = getattr(self.sessions, "_get_session_agent", None)
        if callable(reader):
            live_agent = reader(key)
    except Exception:
        logger.debug("cron '%s': live session agent unreadable", job.name, exc_info=True)
        live_agent = None
    if isinstance(live_agent, str) and live_agent and live_agent != (agent or ""):
        return live_agent
    return None


def _announce_cron_quarantine(self: GatewayOrchestrator) -> None:
    """Tell the dashboard that startup moved an unreadable cron store aside."""
    moved = getattr(self, "_cron_quarantine", None)
    if moved is None or self.dashboard_state is None:
        return
    self.dashboard_state.notify(
        "cron",
        "Scheduled tasks could not be read",
        "The schedule file was not a readable schedule (invalid JSON, or no jobs "
        f"list), so Kiro Crew moved it to {moved} and started with no scheduled "
        "tasks. Nothing was deleted. To restore: fix that copy, stop Kiro Crew, "
        f"move it back to {data_home() / 'crons.json'}, and start again. "
        "`kirocrew doctor` shows the same steps.",
    )


async def _start_cron_after_memory_ready(self: GatewayOrchestrator) -> None:
    """Arm overdue jobs only after the memory preparation fence completes."""
    if self._no_crons or self.cron_svc is None or not self._cron_reconciled:
        return
    if self._cron_armed:
        return
    startup = getattr(self, "_memory_startup", None)
    if startup is not None and (startup.stopped or not startup.ready):
        raise RuntimeError("Cron scheduler cannot start before memory preparation completes")
    await self.cron_svc.start()
    self._cron_armed = True
    if self.sessions:
        self.cron_svc.start_reaper(self.sessions)
    else:
        logger.warning("Cron reaper not started: sessions not available")
