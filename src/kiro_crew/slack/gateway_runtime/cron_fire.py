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
``_cron_reconciled``, ``cron_svc``.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

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
            return await goal_reminder.fire(self.dashboard_state, job)

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
            return await self._cron_run_script(job)

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
        # The result injection at the end of this callback is the other
        # creator site for the job's dashboard tab, and on its own it leaves
        # a NEW job's first run without one: session-control caller identity
        # (caller_slot_key walks slot links for cron:{id}) refuses every verb
        # with caller_unidentified, and the dashboard-surface registry has no
        # row for sub-agent/widget/question/approval routing. Bind the tab up
        # front — eligibility (persistent_session and not hide_in_chat) lives
        # inside the helper, so script/command jobs never reach it (they
        # return above) and ineligible message jobs are untouched. Placed
        # AFTER the fire-time gate: a denied run dispatches nothing a tab
        # could serve. Visible consequence, accepted deliberately: a first
        # run that starts and then fails leaves an empty tab. Pre-creating
        # anyway is the choice, because gating the tab on the run reaching
        # injection would reopen the very hole this closes — identity must
        # exist DURING the run. Guarded wrapper, not a bare await: a tab we
        # FAIL to mint must not kill — or, via the run_never_started
        # lifecycle, CONSUME — the one-shot run it was meant to serve (see
        # _pre_create_cron_slot for the retention-marker contract).
        if self.dashboard_state:
            await _pre_create_cron_slot(self.dashboard_state, job)

        # ── Sequential agent execution ──
        # When agent_sequence has multiple agents, run them sequentially
        # with per-agent session keys and per-job env vars.
        if agent_sequence_dispatches(cap.cron_agents):
            return await self._cron_run_sequence(job, cap)

        # ── Single-agent path (existing behavior) ──
        # Per-job env vars (single-agent path) travel via extra_env passthrough
        return await self._cron_run_single(job, cap, retry=_cron_callback)

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


async def _cron_capture(self: GatewayOrchestrator, job: CronJob) -> _CronCapture:
    """What one cron fire captures before it dispatches anything.

    The session key and prompt ``build_cron_session_context`` picks, a snapshot of
    the agent sequence, the execution the run is bound to with its memory store, and
    the agent it dispatches: the configured default stands in for the bare floor
    only where that is safe. Every kind of job captures, before the overlap guard.
    """
    # helper picks stable vs ephemeral session key and
    # decides whether to prepend last_result, based on job.persistent_session.
    session_key, msg = build_cron_session_context(job)

    from kiro_crew.cron import resolve_cron_memory
    from kiro_crew.execution_context import (
        execution_for_store,
        execution_from_record,
    )

    # Snapshot the job before yielding; reloading a cron cannot rebind it.
    cron_agents = list(job.agent_sequence)
    # ── Default-agent fallback (LLM jobs) ──
    # A job with no explicit agent_id must run the configured default
    # agent (config.agent.default_agent), matching the chat transports'
    # fallback (transport_dispatch: `self.agent or cfg.agent.default_agent`).
    # Without it the empty selector reaches dispatch as AcpClient's
    # "kirocrew" floor -- an agent whose config carries none of the
    # default agent's MCP servers, so agent-less cron sessions silently
    # run without the expected toolset.
    #
    # Resolved ABOVE the branch and applied BELOW it, to the dispatched
    # agent only -- never to the captured execution identity -- and only
    # where the resolved agent is still the bare "kirocrew" floor. All
    # three halves are load-bearing:
    #   * Above the branch, because every record created since the
    #     execution-context migration carries `execution_context` and so
    #     takes the other arm. Resolving inside `else:` reaches only
    #     pre-migration jobs and leaves the reported symptom in place for
    #     everything created since.
    #   * Not into the capture, because `cron_execution` is what
    #     `bind_session_execution` publishes under the session key, and a
    #     persistent_session job (the default) reuses the stable key
    #     `cron:{job.id}` on every fire. That binder is called
    #     positionally, so `replace_existing` is False, and it refuses a
    #     candidate that differs from what is already recorded. A job that
    #     has already run holds `template_id="kirocrew"`; folding the
    #     default into the captured identity changes that field, so every
    #     later fire raises "session already belongs to another execution"
    #     before get_or_create -- not transient, caught nowhere in this
    #     callback, one recorded failure per fire until the job
    #     auto-pauses. Leaving the captured identity alone is also what
    #     keeps the resolved name out of the durable store.
    #   * Floor-gated, because a captured `template_id` that names a real
    #     template is a deliberate selection and must survive; see the
    #     gate below.
    # job.agent_id is never mutated: the snapshot is deliberately immune
    # to later mutation ("captured selectors, never the scheduler's
    # mutable job"), and a declared agent_sequence owns its own dispatch
    # and is left untouched. agent_sequence_dispatches is the ONE spelling
    # of "the sequence owns dispatch": a lone entry is dormant and
    # dispatch falls through to agent_id, which must still get the default.
    # Typed deliberately: a malformed or non-str configured default must
    # not reach the dispatched agent kwarg, so it degrades to the existing
    # "kirocrew" floor instead of failing the dispatch.
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
            # Prefer the last APPLIED reload over the boot snapshot.
            # agent.default_agent carries no restart=True mark, so the
            # config watcher adopts a dashboard/CLI change live, while
            # self._cfg is rebuilt only by unrelated MCP handlers. A
            # boot-only read would keep dispatching the "kirocrew" floor
            # after an operator sets a default -- the exact symptom this
            # hunk removes -- until a gateway restart or an incidental
            # reload. Same one-liner the sibling resolver below uses.
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
    cron_memory_store, cron_agent = (
        cron_execution.store.legacy_name,
        cron_execution.template_id,
    )
    # The configured default lands HERE and nowhere else: cron_agent is
    # read only by _resolve_cron_agent and the `agent=` kwargs below, none
    # of which any durable record compares.
    #
    # Gated on the resolved agent still being the bare "kirocrew" floor,
    # which is the whole of what this change replaces. A record whose
    # captured template_id names a REAL template keeps it: a schedule
    # created from a template chat with no `agent` argument names its
    # template ONLY there, `agent_id` staying empty: bind_cron_memory
    # (cron_service/identity.py:143-152) captures the CREATOR session's
    # execution, and overwrites template_id only `if job.agent_id`. So
    # an agent_id-only gate reads that job as agent-less. Overriding it
    # would re-point the job onto the default on every fire while its
    # captured execution and memory store stayed the original template's,
    # and dispatched_agents_from_disk -- the delete guard's ONE walk of
    # the dispatch-mirroring rule -- still reported the old agent: guard
    # and dispatch would drift, which is the same silent wrong-agent class
    # this change removes, reintroduced on the other arm. Compared after
    # .strip() so a whitespace-only capture counts as the floor, which is
    # where whitespace agent_id handling lives now that the capture itself
    # is left byte-for-byte alone.
    # Also gated on the capture carrying no member_id. A cron created
    # from a crew member's chat is captured by resolve_member_execution
    # as a MEMBER execution whose template_id is
    # `agent.kiro_agent or "kirocrew"` -- so a member whose agent names
    # no kiro_agent captures the floor spelling with agent_id empty, and
    # a floor-only gate reads it as "selected nothing". Substituting
    # there makes build_message inject the substituted template's system
    # prompt and documents into that member's envelope while
    # _resolve_cron_agent still dispatches the member's own kiro_agent,
    # and attributes the usage row to the substituted name. Requiring
    # member_id to be unset narrows the override to a capture that
    # really did select nothing.
    #
    # member_id ALONE does not narrow it far enough. A member predating
    # the identity migration persists no member_id at all, and
    # `with_template` states the rule that admits it: such a record "is
    # named by ``selection_kind == \"member\"`` and ``selection_name``
    # alone" (execution_context.py). ExecutionContext.__post_init__
    # constrains selection_kind to exactly "member" or "template", so
    # the namespace is the authoritative answer to "did this capture
    # select a member?" and member_id is only its persisted form.
    # Requiring BOTH keeps a legacy member on an unmigrated store out of
    # the override, which a member_id-only gate admitted.
    #
    # And BOTH capture gates together are still not enough, because on
    # the `job.execution_context is None` arm cron_execution is
    # RECONSTRUCTED, not loaded: resolve_legacy_execution calls
    # execution_for_store, which for a V1 store returns
    # `ExecutionContext(None, MemoryStoreRef(name), "template", ...)` --
    # hardcoding a null member_id and the "template" namespace whatever
    # the schedule actually selected. A member cron predating the
    # migration therefore passes both capture gates while job.member_id
    # still names the member, and resolve_cron_memory deliberately keeps
    # that record dispatching rather than raising
    # (cron_service/identity.py: "Older V1 schedules may still carry the
    # historical member selector beside their explicit legacy store"),
    # with nothing backfilling execution_context on load. So the JOB is
    # asked as well: on that arm it is the only surviving evidence of the
    # selection. A truthy non-str also declines, which is the safe
    # direction -- declining leaves the pre-change floor behaviour.
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
        # Honouring it properly means a NON-DURABLE parent execution the
        # spawn gate reads, in two files this change does not touch and
        # whose reviewers are scoped to spawn admission, not cron. So
        # this change narrows itself instead: a default that declares an
        # allowlist is not substituted, and the job keeps the floor it
        # already had before this change. Nothing an operator has today
        # regresses; the capability simply waits for that CR.
        #
        # DENY BY DEFAULT. A non-empty tuple declines, and so does the
        # reader's `None`. Only `()` -- "no readable spec declares this
        # name, and every spec file parsed" -- permits the substitution.
        #
        # An earlier revision substituted on `None`, arguing the spawn
        # gate would refuse anyway because it asks about the CAPTURE's
        # floor spelling. That premise is false: `agent.py` generates and
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

    A cron bound to a Slack channel carries that channel's agent
    ALIAS (e.g. ``in-3d``) in ``job.agent_id`` / ``agent_sequence``.
    kiro-cli only accepts a materialized agent MODE, not a Kiro Crew
    alias, so dispatching the alias verbatim fails closed with
    "Agent mode 'in-3d' is not available … its ~/.kiro/agents/
    in-3d.json is likely missing". The dashboard chat path already
    collapses the alias the right way — see
    ``chat_runner._allocation_kwargs``, which passes
    ``agent=<kiro_agent>`` alongside ``crew_agent=<alias>`` so
    ``prepare_runtime`` still resolves the member identity from the
    alias. The cron path was the one turn-running surface that
    skipped it.

    Mirror that here: dispatch the alias's ``kiro_agent`` (usually
    ``kirocrew``) as ``agent``, run in the agent's workspace ``cwd``,
    AND return the ``crew_alias`` so the caller can pass it as
    ``crew_agent=`` — without which ``resolve_crew_identity`` sees
    only the bare kiro template name (not a ``config.agents`` key),
    returns ``""``, and every member-capability gate, the crew's
    pinned model / reasoning-effort, and its watchdog windows are
    silently skipped. Returns (None, None, None) on any miss so the
    caller falls back to the raw value unchanged (behavior-preserving
    for a job whose agent is already a real mode or is unset).

    ``sequence_step`` marks a declared ``agent_sequence`` dispatch,
    where ``alias`` is the step's OWN crew, not the schedule's
    captured member. The captured-member pin below is then skipped so
    each step resolves its own crew's kiro_agent, workspace, pinned
    model and capability gates; the captured memory store is shared
    by every step regardless (it is bound per session key by the
    caller), so the run keeps one memory identity while the steps run
    as the crews the sequence names. Without this, every step
    resolved to the captured member and the other crews named in the
    sequence never actually ran.
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
        # Prefer the last APPLIED reload over the boot snapshot: an
        # agent created at runtime (dashboard/CLI) writes cfg.agents
        # in live.snapshot() but not in the boot self._cfg, so a
        # boot-only read would miss a hot-added alias and dispatch it
        # raw (failing closed) on every fire until a gateway restart.
        # Same one-liner the watchdog/mcp reads use elsewhere here.
        cfg = live.snapshot() or cfg
        if cfg is None:
            return None, None, None
        agents = getattr(cfg, "agents", None) or {}
        # Resolve by IDENTITY, not by name, for a member execution.
        # The memory store is captured at authoring (cron_execution),
        # but the runtime/workspace/crew_agent are resolved live here;
        # if a same-name alias was deleted and recreated, a by-name
        # lookup would bind the NEW member's runtime while the store
        # stays the retired member's silo — a cross-identity memory
        # leak (the memory-store-seam execution_context.py guards).
        # member_config_for_id pins resolution to the captured
        # member_id, so the alias whose bindings we read is the same
        # member the store belongs to; a mismatch (recreated/renamed)
        # raises and we fall back to the raw value unchanged.
        #
        # Skipped for a declared agent_sequence step: there the
        # captured member is the run's shared memory identity, not
        # the agent each step runs as, and ``alias`` already names
        # the step's OWN crew. Pinning here would collapse every step
        # onto the captured member, so the sequence step falls
        # through to the by-alias path below instead.
        resolved_alias = alias
        captured_member = None if sequence_step else getattr(cron_execution, "member_id", None)
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
        # For a non-member (template/legacy) execution there is no
        # identity to pin to: ONLY collapse a real Kiro Crew alias.
        # resolve_agent_bindings falls back to the default agent for
        # an unknown name, so resolving unconditionally would rewrite
        # a legitimate kiro mode (e.g. 'kirocrew-lite') into the
        # default. A name that is not an alias is either a real mode
        # or unset — leave it untouched.
        elif alias not in agents:
            return None, None, None
        # validate_memory_files=False: we only need the alias's
        # kiro_agent + workspace mapping here, and this runs on the
        # gateway event loop. The default (True) does a synchronous
        # store dir stat + SQLite identity read, which would stall
        # the loop on every cron fire; the session's own
        # execution-context resolution validates the store later.
        bindings = resolve_agent_bindings(cfg, resolved_alias, validate_memory_files=False)
        kiro_agent = bindings.kiro_agent or None
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
        return kiro_agent, cwd, resolved_alias
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
