"""The run's turn: its session, its prompt and the streamed answer, in ``_run_inner_impl``.

``_run_inner_impl`` is the body ``_run_impl`` wraps in its deadline. It resolves the agent
and the execution the run carries, starts the session on the parent's shared runtime or a
dedicated process (``run_start`` decides which), builds the prompt, streams the turn through
the retry ladder in its nested ``_stream_with_transient_retry``, answers the child's tool
prompts through the run's permission ladder (``RunEventCoordinator._permission_policy``),
and records the completion. It is one function, kept whole so its locals and nested closures
stay as they are. New work inside a run's turn goes here.

A function here whose first parameter is ``self`` is a ``RunEventCoordinator`` member: the
class binds it under the same name in ``subagent_manager/run.py``. The ``*_impl`` ones are
rebound onto ``kiro_crew.subagent``'s globals by ``bind_component_globals``, so the names
they read unqualified are that module's, imported here for typing only; this module defines
no other.

The manager fields this module uses. Writes: ``_agents``, ``_conversations``,
``_report_tasks``. Reads: ``_await_late_start``, ``_clear_tool_dispatch``,
``_completion_keep``, ``_completion_keep_chars``, ``_create_shared_session``,
``_ctx_builder``, ``_effective_turn_limit``, ``_ensure_dedicated_start_priced``,
``_fire_event``, ``_gate_exit_reset``, ``_gate_wait_mark``, ``_global_approval_mode``,
``_is_cc_provider``, ``_is_yolo``, ``_leave_startup``, ``_note_startup_progress``,
``_note_tool_dispatch``, ``_note_tool_result``, ``_parent_steering_dirs``,
``_provider_label_of``, ``_sessions``,
``_sharing_plan``, ``_shutting_down``, ``_start_result_file``, ``_stop_error_text``,
``_stop_recovery_wanted``, ``_touch_activity``, ``_write_finished_result_off_loop``,
``_write_state_off_loop``, ``_write_tombstone``, ``_yield_for_dependency``,
``_yield_for_infra_retry``, ``_yield_for_stop_recovery``, ``arm_report_in_flight``,
``dependency_coordinator_async``, ``hook_store``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import time

    from ..subagent import (
        _CANCEL_RESUME_PREFIX,
        _SYSTEM_PREFIX,
        _TRANSIENT_CONTINUE_MSG,
        EVENT_AGENT_SWITCHED,
        EVENT_COMPLETE,
        EVENT_PERMISSION_REQUEST,
        EVENT_TEXT_CHUNK,
        EVENT_TOOL_CALL,
        EVENT_TOOL_RESULT,
        FALLBACK_CANDIDATE_ATTEMPTS,
        FALLBACK_STORY_ATTR,
        HOOK_EVENT_POST_TOOL_USE,
        STOP_CLASS_CANCELLED,
        TERMINAL_TOOL_STATUSES,
        TRANSIENT_RETRIES,
        Any,
        FallbackState,
        LLMEvent,
        Stats,
        SubagentInfo,
        _context_groups_of,
        _redact,
        _resolved_model_of,
        _route_unpinned_start,
        _RunCreditAccounting,
        _selection_kind,
        _validate_agent,
        _vet_spawn_governance,
        acp_error_is_transient,
        advance_fallback_candidate,
        agent_dir_for_display,
        annotate_model_fallback,
        apply_completion_keep,
        classify_stop_reason,
        configured_fallback_chain,
        evict_completed_agents,
        extract_options,
        fire_tool_hooks,
        hook_gate_kwargs,
        invalidate_stale_kas_session,
        is_registered_agent_name,
        logger,
        provider_fallback_active,
        refuse_stale_switch,
        replace_stale_shared_session,
        reproject_claimed_session,
        run_in_embed_pool,
        sel,
        transient_retry_delay,
        turn_spec_hooks,
        window_for_provider_client,
        write_result_chunk,
    )
    from .run import RunEventCoordinator


async def _run_inner_impl(
    self: RunEventCoordinator,
    info: SubagentInfo,
    session_key: str,
    usage: _RunCreditAccounting,
) -> None:
    """Inner execution — called within timeout wrapper."""
    setattr(info, "_session_id", "")
    setattr(info, "_session_provider", "")
    setattr(info, "_session_cwd", "")
    # Mark the real start of execution BEFORE any await so the startup
    # watchdog measures from here, not from registration (which may include
    # an arbitrary spawn-approval wait). Must be the first statement.
    info._exec_started = time.time()
    info._exec_started_mono = time.monotonic()
    info._first_stream_started = None
    info._first_stream_mono = None
    info._startup_cotenant_frames = 0
    # The paused part of the clock belongs to THIS attempt: a recovery respawn
    # re-runs the same info, and a queue wait the previous attempt accumulated
    # (or a mark it was cancelled inside) would be subtracted from a clock that
    # never paid it -- blinding the watchdog, or spending the saturation cap.
    info._start_queue_wait_ms = 0.0
    info._start_queue_wait_mono_ms = 0.0
    info._gate_wait_started = None
    info._gate_wait_started_mono = None
    # Prompt-hook time paused the startup clock too, per attempt the same way.
    info._prompt_hooks_secs = 0.0
    # The durable row stays ``starting`` until this run's OWN turn produces
    # its first stream event addressed to its session
    # (``ensure_running_marked`` in the stream loop below):
    # session creation, the session-start gate and a late adoption are all
    # start time, and a row that reads ``running`` while no turn exists yet
    # would let a stall be judged against a session that is still being
    # built.
    info._taskq_running_marked = False
    # Reset the activity clock to execution start too: last_activity is set
    # at registration (like ``started``), which can include a long spawn-
    # approval / queue wait. Without this, _maybe_flag_stall would treat
    # that pre-execution delay as idle time and prematurely surface a
    # healthy, just-started subagent as "stalled".
    info.last_activity = info._exec_started
    info._last_activity_mono = info._exec_started_mono
    if info.error.startswith("memory_unavailable:"):
        raise RuntimeError(info.error)
    if not isinstance(info.memory_store, str):
        raise ValueError("memory_unavailable: the recorded memory identity is malformed")
    # Local imports: this body runs on ``kiro_crew.subagent``'s globals
    # (bind_component_globals), which do not export these names.
    from kiro_crew.agent_sdk import capture_prompt_timeout
    from kiro_crew.agent_sdk.drivers.acp_vocab import EVENT_STRUCTURED_STATUS
    from kiro_crew.recovery.ladder import InfraError
    from kiro_crew.taskq.dependency import classify_exception

    # The per-scope dependency coordinator (None without a durable queue):
    # decides whether a classified provider failure waits on a shared
    # schedule or falls back to the in-turn ladder. Awaited: a first build
    # reads every waiting row, and this run may be the first caller.
    _dep_coordinator = await self._manager.dependency_coordinator_async()

    # Every continuation reaches this allocation boundary, including direct
    # manager callers and recovery. Restore the original conversation's
    # protected mode off-loop before any provider or model context exists.
    if info.conversation_key:
        from kiro_crew.subagent_persistence import tighten_run_memory_mode
        from kiro_crew.workflows.registry import _await_owned

        if not info.conversation_key.startswith("subagent:"):
            raise ValueError("memory_unavailable: unrecognized conversation identity")
        original_id = info.conversation_key.removeprefix("subagent:")
        requested_mode = info.memory_mode

        def restore_mode():
            mode = tighten_run_memory_mode(original_id, requested_mode)
            return tighten_run_memory_mode(info.id, mode)

        owned = asyncio.create_task(asyncio.to_thread(restore_mode))
        info._state_drain_active = True
        try:
            info.memory_mode = await _await_owned(owned)
            info._memory_mode_ready = True
        except (OSError, ValueError):
            raise ValueError(
                "memory_unavailable: conversation memory mode is unavailable"
            ) from None
        finally:
            info._state_drain_active = False

    from kiro_crew.execution_context import bind_session_execution

    if info.execution_context is None:
        raise ValueError("memory_unavailable: no captured execution context")
    info.execution_context = info.execution_context.with_mode(info.memory_mode)
    captured_execution = info.execution_context
    is_continuation = bool(info.conversation_key)

    def publish_execution():
        execution = captured_execution
        if is_continuation:
            from kiro_crew.execution_context import read_session_execution

            original_execution = read_session_execution(session_key, required=True)
            if original_execution.store != captured_execution.store:
                raise ValueError("memory_unavailable: continuation changed memory owner")
            execution = original_execution.with_mode(captured_execution.memory_mode)
        bind_session_execution(session_key, execution)
        # A crewmate's own worker (``spawn_run(crew=...)`` of a crewmate that owns
        # its file) takes its crewmate's backend route and kit, as a hand-off's
        # worker does; any other run drops the key from that route.
        from kiro_crew.mate_worker import note_worker_session

        note_worker_session(session_key, execution)

    from kiro_crew.workflows.registry import _await_owned

    publication = asyncio.create_task(asyncio.to_thread(publish_execution))
    info._state_drain_active = True
    try:
        await _await_owned(publication)
    finally:
        info._state_drain_active = False
    parent_policy = self._manager._sessions.get_approval_policy(info.parent_session_key)
    # Explicit approval_mode from spawn caller (e.g. Mochi bg agent)
    if not parent_policy and info.approval_mode == "auto":
        parent_policy = "auto"
        sel().log_api_access(
            caller=info.parent_session_key or f"subagent:{info.id}",
            operation="subagent.approval_mode_auto_policy",
            outcome="ok",
            source="subagent",
            resources=f"subagent_id={info.id}",
        )
    if not parent_policy and self._manager._is_yolo and self._manager._is_yolo():
        parent_policy = "auto"
        sel().log_api_access(
            caller=info.parent_session_key,
            operation="subagent.yolo_policy_fallback",
            outcome="ok",
            source="subagent",
            resources=f"subagent_id={info.id}",
        )
    if not parent_policy and self._manager._global_approval_mode == "auto":
        # Apply global config as fallback only when parent is absent or
        # confirmed garbage-collected (absent from the session store).
        # If parent session still exists but returned no policy, deny by
        # default — the session is alive and intentionally non-auto.
        if not info.parent_session_key:
            _parent_gone = True  # no_parent
        elif self._manager._sessions.has_session(info.parent_session_key) is False:
            _parent_gone = True  # parent_gc
        else:
            _parent_gone = False  # parent alive or store error → deny
            sel().log_api_access(
                caller=f"subagent:{info.id}",
                operation="subagent.config_policy_fallback",
                outcome="denied",
                source="subagent",
                resources=f"subagent_id={info.id},reason=parent_alive_or_store_error",
            )
        if _parent_gone:
            parent_policy = "auto"
            _reason = "parent_gc" if info.parent_session_key else "no_parent"
            sel().log_api_access(
                caller=f"subagent:{info.id}",
                operation="subagent.config_policy_fallback",
                outcome="ok",
                source="subagent",
                resources=f"subagent_id={info.id},reason={_reason}",
            )
    # auto_approve_subagent_tools auto-approves tool calls inside
    # subagents (separate from the spawn gate, deny-by-default).
    if not parent_policy and self._manager._ctx_builder and self._manager._ctx_builder.hooks:
        if self._manager._ctx_builder.hooks.auto_approve_subagent_tools is True:
            parent_policy = "auto"
            sel().log_api_access(
                caller=info.parent_session_key or f"subagent:{info.id}",
                operation="subagent.auto_approve_subagent_tools_policy",
                outcome="ok",
                source="subagent",
                resources=f"subagent_id={info.id}",
            )
    # Admission captured both memory identity and this invocation's persona
    # before any asynchronous work. A continuation's explicit override is
    # effective only for this turn; its next continuation keeps its lineage.
    from dataclasses import replace

    execution = info.execution_context
    durable_selection = (
        execution.selection_kind,
        (
            execution.selection_name
            if execution.selection_kind == "member"
            else execution.template_id
        ),
    )
    agent = info.agent or execution.template_id
    kind = _selection_kind(info)
    if info.crew or execution.member_id or (not info.agent and agent):
        policy_agent = info.crew or (execution.selection_name if kind == "member" else agent)
        denial = await asyncio.to_thread(
            _vet_spawn_governance, info.parent_session_key, policy_agent, app=info.app
        )
        if denial:
            raise RuntimeError(f"spawn refused by governance: {denial}")
        sel().log_api_access(
            caller=f"subagent:{info.id}",
            operation="subagent.agent_inheritance",
            outcome="ok",
            source="subagent",
            resources=f"subagent_id={info.id},inherited_agent={agent}",
        )
    effective_cwd = info.cwd or str(getattr(self._manager._sessions, "_pool_cwd", "") or "")
    from kiro_crew.mate_worker import is_own_worker_execution

    own_worker = not info.agent and is_own_worker_execution(execution)
    if own_worker:
        # The worker's spec lives in the crewmate folder, which the agent roster
        # does not list, so it is not looked up by name; what it needs is a
        # backend that carries its per-session kit (the hand-off's own check).
        from kiro_crew.acp_backends import resolve_selected_backend
        from kiro_crew.config.loader import KiroCrewConfig as _Config
        from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable, crew_for_member_id
        from kiro_crew.mate_worker import crewmate_worker_backend, worker_kit_backend_refusal

        _cfg = await asyncio.to_thread(_Config.load)
        try:
            worker_backend = (
                resolve_selected_backend(info.backend)
                if info.backend is not None
                else crewmate_worker_backend(
                    _cfg, crew_for_member_id(_cfg, str(execution.member_id or ""))
                )
            )
        except CrewmateHarnessUnavailable as exc:
            # The crewmate's pinned harness cannot start, so neither can its
            # worker; it never runs on another harness in its place. The
            # message leads with its code, as the kit refusal below does.
            raise RuntimeError(str(exc)) from None
        backend_reason = worker_kit_backend_refusal(worker_backend)
        if backend_reason is not None:
            raise RuntimeError(f"capability_harness_unsupported: {backend_reason}")
    elif agent and (kind == "member" or (info.conversation_key and not info.agent)):
        agent, error, code = await asyncio.to_thread(_validate_agent, agent, effective_cwd)
        if error:
            info.error_code = code
            raise RuntimeError(error)
    await self._await_identity_write(
        info,
        asyncio.ensure_future(
            asyncio.to_thread(
                self._write_run_agent,
                info.id,
                durable_selection[1],
                kind=durable_selection[0],
            )
        ),
    )
    turn_execution = replace(execution, template_id=agent)
    extra_kwargs: dict[str, Any] = {
        "crew_agent": execution.selection_name if kind == "member" else "",
    }
    # The owning app the spawn was vetted for (``_vet_spawn_governance`` above)
    # is the child session's app too: its provider's ``owning_app`` is what
    # the child's own governance reads, its git proxy's egress checks among
    # them, so an app-owned chat's subagent keeps the app's profile. Passed as
    # ``owning_app``, not ``app_name``: the child keeps the model the spawn
    # path resolves and reports (``_spawn_effective_model``), not the app's pin.
    if info.app:
        extra_kwargs["owning_app"] = info.app
    # An explicit per-spawn model wins; otherwise fall back to the
    # configured sub-agent role model (agent.role_models['subagent']). When
    # that role is unpinned the helper returns "" so we omit the kwarg and
    # keep deferring to the provider's configured default, exactly as before.
    # The one sharing decision (``_sharing_plan``), which admission also read
    # to price this start: template execution, not ``keep``, the eligibility
    # predicate, and no model / reasoning-effort pin.
    # An UNPINNED start may take a model from ``model.route``'s derived tiers
    # (``decisions.model_route_derive``, off by default). Decided before the
    # sharing plan, which reads the routed model as a pin: a routed model
    # cannot be applied to the parent's already-started runtime.
    await _route_unpinned_start(info, kind, self._manager._sessions)
    plan = self._manager._sharing_plan(
        info, child_aws_profile=await self._resolve_child_aws_profile(info)
    )
    eff_model = plan.eff_model
    # Record the EFFECTIVE pin (per-spawn OR the role_models['subagent']
    # config pin, via ``_subagent_default_model()``) as the requested side of
    # the downgrade comparison — keying off the bare per-spawn ``model`` would
    # miss a config-pinned run served a different model.
    # For completely unpinned spawns (no per-spawn pin, no role pin) ``eff_model``
    # is ``""``; fall back to the literal ``"auto"`` sentinel so the frontend
    # can show a neutral chip instead of nothing at all.
    info.requested_model = eff_model or "auto"
    if eff_model:
        extra_kwargs["model"] = eff_model
    # Sub-agent reasoning effort (per-call override -> role_efforts['subagent']
    # -> chat default). Passed as an override so it wins over the factory's
    # agent-derived default; "" leaves it to that default.
    eff_effort = plan.eff_effort
    if eff_effort:
        extra_kwargs["reasoning_effort_override"] = eff_effort
    # The per-spawn backend (spawn_run ``backend``). ``is not None``: ``""``
    # is a Kiro request and must reach the factory under a non-Kiro default.
    if info.backend is not None:
        extra_kwargs["backend_override"] = info.backend
    if info.bare:
        extra_kwargs["bare"] = True
    if info.allowed_tools:
        extra_kwargs["allowed_tools"] = info.allowed_tools
    if info.cwd:
        extra_kwargs["cwd"] = info.cwd
    # A dedicated process joins its parent's session tree: the parent's
    # ``$KIROCREW_SCRATCH`` is mounted beside the child's own scratch and is
    # what the child's ``$KIROCREW_SCRATCH`` names, so a brief the parent
    # staged there is readable (agent_scratch). Inert on the shared-runtime
    # arm, where the child already runs in the parent's process; None when
    # the parent has no live provider or spawned without scratch. Also the
    # signal that skips the warm pool, whose mounts were fixed at pre-spawn.
    if info.parent_session_key:
        resolve_scratch = getattr(self._manager._sessions, "parent_work_scratch_dir", None)
        shared_scratch = (
            resolve_scratch(info.parent_session_key) if resolve_scratch is not None else None
        )
        if shared_scratch is not None:
            extra_kwargs["shared_scratch"] = shared_scratch

    # ── Session sharing: reuse parent's shared AcpRuntime ──
    # When enabled and eligible, subagents get a session on the parent's
    # companion AcpRuntime (~200ms startup, ~0 memory) instead of spawning
    # a fresh kiro-cli process (~3-5s, ~400MB).
    #
    # Retain-by-default: EVERY run keeps its session files (teardown skips
    # deletion on both arms), so any completed run is continuable while
    # its files survive. keep=True / continuation runs additionally take
    # the dedicated arm: their resume path is the proven dashboard
    # expire-and-session/load lifecycle, which owns its process.
    #
    # A SHARED-runtime sid IS loadable, on every backend in
    # ``ACP_BACKENDS_SESSION_SHARING``. Teardown disposes the in-memory session on
    # both arms -- a resident subagent session would hold its MCP fleet on a
    # runtime nobody is using -- and what a later ``spawn_continue`` addresses is
    # the record the host kept: kiro-cli's transcript under
    # ``<kiro home>/sessions/cli``, or the thread ``codex`` persists under
    # ``CODEX_HOME``. Both are driven end to end with the shared session's runtime
    # process dead before the continuation runs. The fail-closed resume guard
    # below still applies, because a record can be pruned or released between the
    # two runs; it is one of two things standing between a shared-arm run and its
    # follow-up rather than the only one.
    # ── Remote session environment: the child works where the parent does ──
    # Bound and fenced like the parent, on the dedicated arm: the shared runtime's
    # workspace server is the parent's and the companion is local. A failed bind
    # refuses the run.
    from kiro_crew.coder.session_launch import inherit_environment_for_subagent

    inherits_remote = bool(info.parent_session_key) and (
        await inherit_environment_for_subagent(info.parent_session_key, session_key)
    )
    info._remote_workspace = inherits_remote
    if info.keep:
        self._manager._sessions.mark_continuable(session_key)
        self._manager._conversations[session_key] = time.monotonic()
    # The parent's shared runtime carries its workspace server, so a remote
    # child gets its own; a start priced shared takes the top-up below.
    use_session_sharing = plan.shared and not inherits_remote
    if use_session_sharing:
        # Local import: this module's ``*_impl`` bodies resolve globals through
        # ``kiro_crew.subagent``, which does not export this name.
        from kiro_crew.agent_sdk.drivers.acp_vocab import AcpRequestTimeout as _StartTimeout

        try:
            client = await self._manager._create_shared_session(info, session_key, agent)
        except _StartTimeout as exc:
            # CONGESTION is never a reason for a dedicated process (RFC
            # §4.4): a timed-out session/new may still create its session,
            # and a second runtime here would double the load that caused
            # the timeout. The StartCollector attached to the exception
            # owns the outstanding request; wait for its verdict and either
            # continue on the adopted session or end this attempt.
            client = await self._manager._await_late_start(info, session_key, exc)
            is_new = True
            _resumed = False
            is_cc = False
        except Exception as exc:
            # The shared runtime itself is unavailable (dead, spawn failed):
            # not congestion. The failure is counted on the ladder's L3
            # rung for the parent's runtime (two inside the cooldown
            # escalate to L4, one notice); the dedicated process stays the
            # per-run recovery because the shared runtime's rebuild belongs
            # to the session that owns it, not to a child run.
            from kiro_crew.recovery.ladder import L3_ACP_RUNTIME, default_ladder

            try:
                default_ladder().observe_failure(
                    L3_ACP_RUNTIME,
                    f"runtime:{info.parent_session_key or 'companion'}",
                    reason=f"shared runtime unavailable: {exc}"[:200],
                    task_id=info.id,
                )
            except Exception:
                logger.debug("ladder L3 observe_failure failed", exc_info=True)
            logger.warning(
                "Subagent %s: shared runtime unavailable (%s), using a dedicated process",
                info.id,
                exc,
            )
            info._session_sharing = False
            info._shared_provider = None
            use_session_sharing = False
            # Admitted at the shared price; the process this launches was
            # never reserved. Top up (and re-check the floor) first.
            await self._manager._ensure_dedicated_start_priced(info)

            def _fallback_claim():
                return self._manager._sessions.get_or_create(
                    session_key,
                    agent=agent or None,
                    approval_policy=parent_policy,
                    on_gate_acquired=self._manager._gate_exit_reset(info),
                    on_gate_queued=self._manager._gate_wait_mark(info),
                    **extra_kwargs,
                )

            # Same claim-time re-check as the dedicated arm below.
            client, is_new, _resumed = await reproject_claimed_session(
                self._manager._sessions,
                session_key,
                agent,
                await _fallback_claim(),
                _fallback_claim,
            )
            is_cc = self._manager._is_cc_provider(client)
        else:
            is_new = True
            _resumed = False
            is_cc = False
        if use_session_sharing:
            # A shared session's batch is built during its session/new; a
            # PreToolUse hook added meanwhile is not in it, and nothing else
            # re-checks a session this run owns outright. Replaced if stale.
            client = await replace_stale_shared_session(
                client,
                agent,
                lambda: self._manager._create_shared_session(info, session_key, agent),
            )
    else:
        # The dedicated process's ``session/new`` runs under the same
        # ``SessionStartGate`` as a shared session's; hand it the same
        # gate clock callbacks (``_gate_wait_mark`` at entry,
        # ``_gate_exit_reset`` at exit), threaded through the provider
        # factory to ``AcpProvider``.
        #
        # A kept KAS session whose registered batch auto-approves what a
        # PreToolUse hook now covers is reset first, so the claim re-projects.
        await invalidate_stale_kas_session(self._manager._sessions, session_key, agent)
        # Admission may have priced this start shared and the decision
        # turned dedicated since (parent gone, sharing off, a role pin set
        # while it waited): reserve the process before launching it.
        await self._manager._ensure_dedicated_start_priced(info)
        # This arm owns its process: a flag an earlier shared attempt of the
        # run left (a cancel-recovery respawn) would hand this process's
        # teardown to the shared arm. The old attempt's teardown, which read
        # it, has finished before this one runs.
        info._session_sharing = False
        info._shared_provider = None

        def _claim():
            return self._manager._sessions.get_or_create(
                session_key,
                agent=agent or None,
                approval_policy=parent_policy,
                on_gate_acquired=self._manager._gate_exit_reset(info),
                on_gate_queued=self._manager._gate_wait_mark(info),
                **extra_kwargs,
            )

        # Decided again under the lease: the pre-claim reset is declined for a
        # session another turn holds, and this claim may have waited for it.
        client, is_new, _resumed = await reproject_claimed_session(
            self._manager._sessions, session_key, agent, await _claim(), _claim
        )
        is_cc = self._manager._is_cc_provider(client)

    # Capture cleanup identity immediately after successful session
    # acquisition. Every later step can fail and tombstone the run, so
    # delaying this until the state write leaves provider files unidentified.
    try:
        cleanup_session_id = str(client.session_id or "") if hasattr(client, "session_id") else ""
        cleanup_provider = self._manager._provider_label_of(client)
        # Continuations use this record to recover their project on every
        # backend, even when continuing a follow-up after gateway restart.
        provider_cwd = getattr(client, "cwd", "")
        cleanup_cwd = provider_cwd if isinstance(provider_cwd, str) and provider_cwd else info.cwd
        if not cleanup_cwd and is_cc:
            inner = getattr(client, "client", None)
            work_dir = getattr(inner, "_work_dir", None)
            if work_dir:
                cleanup_cwd = str(work_dir)
        setattr(info, "_session_id", cleanup_session_id)
        setattr(info, "_session_provider", cleanup_provider)
        setattr(info, "_session_cwd", cleanup_cwd)
        self._publish_identity(
            info.id,
            session_id=cleanup_session_id,
            provider=cleanup_provider,
            cwd=cleanup_cwd,
            keep=info.keep,
            conversation_key=session_key if info.keep else "",
        )
    except Exception:
        logger.debug("Failed to capture live cleanup identity for %s", info.id, exc_info=True)

    # Both arms above land here with a live session, so this is the one place
    # that can say what its MCP servers reported — before the run spends its
    # turn budget hunting a tool that was never mounted. Logged for the
    # operator AND carried into the prompt below, because the run that does
    # the hunting never reads the log.
    mcp_problems = self._warn_unusable_mcp_servers(info, client)

    # Fail CLOSED on a continuation that did not actually resume. Identity is
    # already captured so the abnormal tombstone can reclaim the fresh session.
    if info.conversation_key and not _resumed:
        raise RuntimeError(
            "resume_failed: session/load did not restore conversation "
            f"{info.conversation_key} — refusing to execute the "
            "follow-up without its prior context. The conversation "
            "may be locked by a live process or its files corrupt; "
            "re-spawn with a fresh task carrying a summary."
        )
    # Intentionally check info.agent (not resolved `agent`) so only
    # explicitly requested agents skip _SYSTEM_PREFIX (defense-in-depth).
    named_agent = bool(info.agent and is_registered_agent_name(info.agent))
    raw_task = info._raw_task or info.task
    message = raw_task if named_agent else (_SYSTEM_PREFIX + raw_task)
    if info._cancel_retry_used and (info.streaming_text or info.tool_count > 0):
        # One-shot auto-continue after an unexpected cancellation: tell the
        # model the prior attempt was interrupted so it completes the task
        # instead of assuming a fresh start. Same activity predicate as
        # the transient-retry path: a mutating tool may
        # have executed BEFORE the first text chunk, so tool_count must
        # trigger the preamble too — a bare original prompt after tool
        # activity invites duplicate side effects. (info.tool_count and
        # streaming_text persist across the respawn until this point;
        # streaming_text restarts only on the new attempt's first chunk.)
        message = _CANCEL_RESUME_PREFIX + message
    # A server the session could not mount is stated before the task, so the
    # run never spends a turn discovering the absence for itself. Empty for a
    # healthy session, which leaves this prompt exactly as it was.
    message = self._spawn_mcp_notice(mcp_problems) + message
    # Scale the injected-context budget to this subagent's model window (a
    # subagent can be pinned to a smaller model). Resolved from the live
    # client; None ⇒ 1M reference.
    _sub_window = window_for_provider_client(client)
    # Context scope this run was spawned with. Passed even when every group
    # is on, so build_message applies one code path for sub-agents.
    _groups = _context_groups_of(info)
    # Off-loop: build_message embeds the episodic query (blocking urllib).
    # A run's explicitly-given cwd IS its project for skill scoping: a
    # dashboard spawn inherits the parent slot's project as its cwd, so the
    # hands-off surface keeps the repo-scoped skills that surface exists for.
    # The pool default is deliberately NOT substituted -- it is the
    # workspace directory, not a checkout, so it can only ever mean "this
    # run named no project", which is exactly the fail-closed case. Keeping
    # one meaning for that makes the rule the same on every surface.
    # The child's own memory silo. Without it every subagent reads the
    # operator's global store however the parent crew is bound, which makes
    # a crew's isolation end at the moment it delegates.
    #
    # Prepare before the offloaded build because vector initialization is
    # blocking file IO. A private store that cannot be prepared refuses the
    # turn; it cannot continue with Global memory.
    from kiro_crew.context import prepare_store_vectors
    from kiro_crew.memory_startup import MemoryStartupUnavailable

    if info.memory_mode != "temporary":
        try:
            await prepare_store_vectors(
                self._manager._ctx_builder, info.memory_store, session_key=session_key
            )
        except MemoryStartupUnavailable:
            # Not an optional-recall miss: the gateway's memory fence is
            # closed (still preparing, stopped, or this store's restore
            # failed). A chat turn is refused at admission in that state;
            # a run admitted here would execute with its memory silently
            # absent, so it fails with the fence's own reason instead.
            raise
        except (OSError, ValueError, RuntimeError):
            # Learned recall is optional for prompt construction. Explicit
            # memory tools still report the unavailable captured store.
            logger.debug("Subagent learned memory is unavailable", exc_info=True)
    from kiro_crew.session_work_dir import is_subagent_work_dir

    # A continuation of a run spawned without a cwd restores that run's
    # per-run scratch directory as info.cwd; it is still not a project.
    _is_scratch = bool(info.cwd) and await asyncio.to_thread(is_subagent_work_dir, info.cwd)
    _project: str | None = None if _is_scratch else (info.cwd or None)
    if info.crew:
        from kiro_crew.members import record_activity

        activity_member = execution.selection_name
        if own_worker:
            # A worker record names the worker template, not the crewmate; the
            # crewmate is the one its member id names (the alias it has now).
            from kiro_crew.config.loader import KiroCrewConfig as _Config
            from kiro_crew.execution_context import member_config_for_id

            _cfg = await asyncio.to_thread(_Config.load)
            try:
                activity_member, _ = member_config_for_id(_cfg, execution.member_id or "")
            except ValueError:
                activity_member = info.crew

        # A respawn of this run re-enters with the same key; the dedupe keeps one entry.
        await asyncio.to_thread(
            record_activity,
            activity_member,
            session_key,
            execution.memory_mode,
            project=_project or "",
            via="spawn",
            dedupe_session=True,
        )
    # The parent's folder steering, so a child spawned from a chat filed in a
    # folder follows the same guides its parent does. build_message gates the
    # section on the project group, so a run that withheld it skips the read.
    info.steering_dirs = ()
    steering_resolver = self._manager._parent_steering_dirs
    if steering_resolver is not None and info.include_project and info.parent_session_key:
        try:
            info.steering_dirs = tuple(await steering_resolver(info.parent_session_key))
        except Exception:  # noqa: BLE001 - steering is optional; it must never fail the run
            logger.warning("Folder steering unavailable for subagent %s", info.id, exc_info=True)
    full_message, _ = await run_in_embed_pool(
        self._manager._ctx_builder.build_message,
        message,
        is_new,
        session_key,
        project=_project,
        memory_store=info.memory_store or None,
        execution_context=turn_execution,
        provider_type=self._manager._provider_label_of(client),
        model_window=_sub_window,
        context_groups=_groups,
        # Triggers read only the task the parent wrote, not the sub-agent
        # prefix, the resume preamble or the MCP notice prepended above.
        skill_trigger_text=raw_task,
        blocks_reads=info.memory_mode == "temporary",
        context_provider=client,
        agent=agent or None,
        resumed=_resumed,
        # A parent agent reads this reply, not a person at a dashboard, so the
        # [OPTIONS:] and card guidance has nothing to render into.
        interactive=False,
        steering_dirs=info.steering_dirs,
        # The agent was validated against this directory's .kiro/agents, so
        # its spec is found there too. A cold-started run with no cwd has a
        # per-run provider cwd that holds no templates.
        template_dir=effective_cwd or None,
    )
    # The one place the resolved scope and its cost are both known — without
    # this, "the sub-agent didn't know X" is undebuggable after the fact.
    logger.info(
        "Subagent %s context: groups=%s, %d chars",
        info.id,
        ",".join(sorted(_groups)) or "conduct-only",
        len(full_message),
    )

    result_text = ""
    # Text streamed since the run's last tool call: the final segment the
    # leaked-tool-call check reads at the end (see ``leaked_tool_call``).
    _final_segment = ""
    turns = 0
    turn_limit = self._manager._effective_turn_limit(info)
    # Separate volume bound for child-origin permission escalations —
    # they are exempt from the parent's turn budget (see the
    # EVENT_PERMISSION_REQUEST branch) but must not be unbounded.
    child_escalations = 0
    child_escalation_limit = max(turn_limit * 3, 60)
    # Reports inherited agent (not just info.agent) so telemetry shows
    # the actual agent used for this subagent session.
    #
    # Read back the model the live session actually resolved to serve, so
    # the panel shows what ran rather than only what was requested.
    # Best-effort at spawn: the ACP session/new response already
    # carries the served id (readable now, even on the backend default),
    # while the raw CC path only knows it after the first turn — so this is
    # refreshed authoritatively at completion below. Only overwrite a prior
    # non-empty value with another non-empty one, so a spawn-time read that
    # succeeded is never clobbered back to "" by a transient later miss.
    _spawn_model = _resolved_model_of(client)
    if _spawn_model:
        info.resolved_model = _spawn_model
    # Persist provenance to disk BEFORE the spawn event so a gateway restart
    # in the window between the event and the later session_id state write
    # cannot lose it — orphan recovery reads these from disk. Off-loop and
    # drained on cancellation via _write_state_off_loop (see
    # that helper for why a detached worker is the hazard). Best-effort with
    # ONE bounded retry: this write is the SINGLE owner of these two fields
    # on the spawn path — the later session_id write does not
    # double as a fallback, so a transient failure gets its second chance
    # HERE rather than from a second writer downstream. update_state reports
    # a silently-skipped merge (unreadable state) as False, which counts as a
    # failure for the retry — only a REPORTED write ends the loop. A
    # persistence hiccup must still never block the spawn. A cancellation
    # ends the loop instead of retrying: `except Exception` does not catch
    # CancelledError, so the helper's post-drain re-raise propagates rather
    # than starting a second writer for the same fields.
    for _provenance_attempt in range(2):
        try:
            _wrote = await self._manager._write_state_off_loop(
                info,
                "model provenance",
                requested_model=info.requested_model,
                resolved_model=info.resolved_model,
                # What a continuation after a restart keeps: the caller's own
                # pin, not ``requested_model`` (which also holds a role pin or
                # a routed tier), and the effort.
                pinned_model=info.model,
                reasoning_effort=info.reasoning_effort,
            )
            if _wrote:
                break
            logger.debug("Provenance write skipped (unreadable state) for %s", info.id)
        except Exception:
            logger.debug("Failed to persist model provenance for %s", info.id, exc_info=True)
    # The live generation was published synchronously at acquisition so
    # terminal tombstones are cancellation-safe. Persist the sidecar only
    # after the drained provenance write: cancellation during provenance must
    # not prevent its required model fields from landing, and cancellation
    # here still leaves the live tombstone snapshot complete.
    try:
        await self._remember_identity_off_loop(
            info,
            session_id=str(getattr(info, "_session_id", "")),
            provider=str(getattr(info, "_session_provider", "")),
            cwd=str(getattr(info, "_session_cwd", "")),
            keep=info.keep,
            conversation_key=session_key if info.keep else "",
        )
    except Exception:
        logger.debug("Failed to persist cleanup identity for %s", info.id, exc_info=True)
    await self._manager._fire_event(
        "subagent_spawn",
        info,
        {
            "task": _redact(info.task),
            "agent": agent or "",
            # Caller free text like ``task``, so redacted the same way.
            "label": _redact(info.label),
            "model": info.resolved_model,
            # The requested pin is caller-supplied (spawn_run.model), so it
            # is redacted like every other free-text field on the frame -- an
            # unavailable/AKIA-shaped pin must never reach the dashboard
            # socket raw.
            "requested_model": _redact(info.requested_model),
            # The sub-agent's own session key (see build_subagent_snapshot):
            # lets a client fetch this node's own context-trace.
            "child_session": info.conversation_key or f"subagent:{info.id}",
        },
    )
    # Stream results to disk for orchestrated chat.

    # Record PID for orphan recovery. Off-loop and drained on cancellation
    # via _write_state_off_loop: update_state ends in a
    # synchronous fsync, and the reaper, every chat turn and the heartbeat
    # share this loop. Off-loop also means the write TAKES update_state's
    # per-agent lock, which on-loop callers skip, so it cannot
    # interleave with another pool writer's read-merge-rewrite.
    try:
        pid = self._manager._sessions.get_pid(session_key)
        if pid:
            info._pid = pid  # make available for _write_tombstone
            # Out of startup: a queued spawn held by the in-startup bound
            # may start now (``_note_startup_progress``).
            self._manager._note_startup_progress(info)
            await self._manager._write_state_off_loop(
                info, "PID record", pid=pid, pid_recorded_at=time.time()
            )
    except Exception:
        logger.debug("Failed to record PID for %s", info.id, exc_info=True)

    # Persist the cleanup identity captured immediately after session
    # acquisition, together with mutable retention intent.
    try:
        state_update: dict[str, object] = {
            # Diagnostics only; continuation uses the protected agent.json.
            "agent": agent,
            "session_id": str(getattr(info, "_session_id", "")),
            "provider": str(getattr(info, "_session_provider", "")),
            # Model provenance (requested_model/resolved_model) is NOT
            # re-written here: the crash-safe write BEFORE the
            # subagent_spawn event above is the single owner of those two
            # fields on the spawn path, and a transient failure there is
            # handled by that write's own bounded retry.
            "keep": info.keep,
            "conversation_key": session_key if info.keep else "",
        }
        cleanup_cwd = str(getattr(info, "_session_cwd", ""))
        if cleanup_cwd:
            state_update["cwd"] = cleanup_cwd
        # Same off-loop, drained write as the PID record above.
        # This one also carries `keep`, the field the two remaining
        # on-loop writers (promote / release) contend for -- taking the
        # per-agent lock here is what orders it against any other pool
        # writer.
        #
        # This write is the SINGLE owner of `cwd` and `session_id` on the
        # spawn path (continuation reads `cwd` back through
        # recorded_cwd), so it gets the same ONE bounded retry as the
        # model-provenance write above: an exception or a reported skip
        # (False -- update_state could not read the state) gets a second
        # chance, and only a reported write ends the loop. CancelledError
        # is not an Exception, so a cancellation still ends the loop
        # instead of starting a second writer. A write that fails both
        # times is logged at WARNING, so a run whose record lacks `cwd` is
        # visible in the log.
        _session_record_written = False
        for _session_record_attempt in range(2):
            try:
                if await self._manager._write_state_off_loop(
                    info, "session record", **state_update
                ):
                    _session_record_written = True
                    break
                logger.debug("Session record write skipped (unreadable state) for %s", info.id)
            except Exception:
                logger.debug("Failed to record session_id for %s", info.id, exc_info=True)
        if not _session_record_written:
            logger.warning(
                "Session record (session_id/cwd) was not persisted for %s after 2 attempts",
                info.id,
            )
    except Exception:
        logger.debug("Failed to record session_id for %s", info.id, exc_info=True)

    _rp = agent_dir_for_display(info.id) / "result.txt"
    info.result_path = str(_rp)
    # This attempt's first text chunk starts the live partial and
    # result.txt afresh, so they hold this attempt's text alone, never
    # glued to an earlier attempt's: until then that partial stays
    # readable. Two flags, because a failed write must not cost the live
    # partial: it starts afresh on the first chunk whatever the disk does,
    # and the file is replaced by the first write that succeeds, with
    # every chunk this attempt has streamed so far (again after a refused
    # append).
    _attempt_has_text = False
    _result_file_started = False
    # Cache tool names by tool_call_id so PostToolUse can recover the tool name
    # when EVENT_TOOL_RESULT arrives (which only carries tool_call_id and output).
    # Mirrors kiro_crew.dashboard.chat_runner._pending_tools.
    _pending_tools: dict[str, str] = {}
    # tool_call_id -> the tool id the call's PostToolUse spec hooks match on.
    from kiro_crew.agent_sdk.spec_hooks import PostToolIdentity, post_tool_identity
    from kiro_crew.hooks import pre_tool_match_names

    _pending_tool_ids: dict[str, PostToolIdentity] = {}
    # The subagent's OWN spec hooks, when its OWN backend never receives them:
    # keyed on this run's agent and provider, never the parent's, so a
    # kiro-cli subagent (whose harness runs the field) gets none from Crew.
    # On such a backend PreToolUse hooks gate each permission request below;
    # the KAS projection turns every call they cover into one.
    _spec = await turn_spec_hooks(client, agent)
    from kiro_crew.subagent_manager.prompt_hooks import prompt_hook_prefix

    # Before the first stream, as a dashboard turn fires them before its own.
    # Hook time is not the runtime's silence: the reaper's startup, first-prompt
    # and stall checks skip the run while they run, the startup clock does not
    # count them, and the activity clock restarts when they end, on every exit.
    info._prompt_hooks_running = True
    _hooks_t0 = time.monotonic()
    try:
        _hook_prefix = await prompt_hook_prefix(
            self._manager.hook_store,
            info,
            session_key=session_key,
            task=raw_task,
            fresh_session=is_new and not _resumed,
            spec_hooks=_spec.hooks,
            spec_hooks_cwd=_spec.cwd,
        )
    finally:
        info._prompt_hooks_secs += time.monotonic() - _hooks_t0
        info.last_activity = time.time()
        info._last_activity_mono = time.monotonic()
        info._prompt_hooks_running = False
    full_message = _hook_prefix + full_message
    # The run's permission ladder. Imported here because this body runs on
    # the facade's globals (bind_component_globals), not this module's.
    from kiro_crew import tool_permission

    # The gate consult stays here, at the surface: it is this run's own
    # attribution of the caller (the spawning app's profile keeps
    # constraining the child's ongoing tool calls). Bound to a name because
    # test_hooks' extraction scan recognises the assignment shape.
    def _consult_gate(event: LLMEvent) -> object:
        verdict = self._manager._ctx_builder.hooks.on_tool_call(
            event.title,
            session_key=session_key,
            agent=info.agent or "",
            app=info.app or "",
            **hook_gate_kwargs(event),
        )
        return verdict

    def _policy_for(spec: Any) -> tool_permission.Policy:
        # ``sel`` is read per row, as the facade's audit seam expects.
        return self._permission_policy(
            info,
            spec=spec,
            parent_policy=parent_policy,
            consult=_consult_gate,
            sel=lambda: sel(),
            log=logger,
        )

    _wire = tool_permission.AcpWire(client)
    _policy = _policy_for(_spec)
    from kiro_crew.deny_notice import REFUSED_TURN_RESUME_MAX, build_refused_turn_resume_prompt

    # The host's refusal in the current prompt attempt, as (title, reason,
    # cause), set by the permission branch below and read by the stream when
    # the turn then ends cancelled; and how many such turns this run resumed.
    _refused_call: list[tuple[str, str, str]] = []
    _person_rejected = [False]
    _refusal_resumes = [0]
    # Set when the stream ends on a generate failure after real output.
    _kept_after_generate_failure = False
    # One run owns one immutable manager deadline. Capture a transport
    # budget only when the concrete provider declares that capability, then
    # reuse it across every retry, fallback, and recovery continuation.
    # Legacy duck providers retain their one-argument stream call. Captured
    # here, after the cleanup identity above is published and persisted: a
    # cancel landing on this await still leaves a tombstone whose provider
    # files can be reclaimed.
    prompt_timeout = await asyncio.to_thread(
        capture_prompt_timeout, client, float(info.timeout_secs)
    )
    # ── Per-turn usage row: attribute subagent spend. ──
    # One row per prompt the child answers, so a Stop-hook continuation is
    # billed like the dashboard turn it mirrors. ``_row_t0`` is when the
    # prompt the row describes began.
    _row_t0 = [time.monotonic()]

    def _persist_usage_row(completion: LLMEvent | None) -> None:
        # Its inputs are read here, synchronously, and the append itself is
        # best-effort analytics on a task the manager holds (``_report_tasks``,
        # which ``cancel_all()`` drains within its bound): no ending waits on
        # it, so a cancel, the shutdown or a wedged FS can never hold one.
        try:
            # circular import: reached while kiro_crew.slack.handler is still
            # initialising (dashboard/file_api/channel_upload.py imports is_tracked_channel
            # from it), so a module-scope import raises ImportError under the
            # suite's import order.
            from kiro_crew.dashboard.handlers.usage import (
                persist_token_record_async,
                read_context_tokens,
                read_effective_agent,
            )

            _used, _window = read_context_tokens(client)
            _usage_row = asyncio.ensure_future(
                persist_token_record_async(
                    session_key,
                    # Blank while a fallback serves this run: the explicit pin
                    # would bill the fallback's spend to a model that never
                    # executed; model_source reports what actually ran.
                    ("" if provider_fallback_active(client) else (info.model or "")),
                    completion,
                    provider="claude_code" if is_cc else "acp",
                    surface="subagent",
                    # Ownership stamp (see _build_token_record): an app-dispatched
                    # subagent's spend must be readable by that app's audit — the
                    # illustrator lane of an app is exactly this path.
                    app=info.app or "",
                    # Explicit/inherited `agent` FIRST here — unlike every other
                    # surface. Under session sharing this subagent reuses the
                    # PARENT's runtime, so read_effective_agent() would report the
                    # parent's agent and misattribute a `spawn_run(agent="…")` turn.
                    # `agent` is already the resolved value (it inherits the parent
                    # session's agent when the spawn did not name one), and the
                    # helper stays as the fallback for when it is empty.
                    agent=agent or read_effective_agent(client) or "",
                    context_used=_used,
                    context_window=_window,
                    elapsed_ms=int((time.monotonic() - _row_t0[0]) * 1000),
                    model_source=client,
                )
            )
            self._manager._report_tasks.add(_usage_row)
            _usage_row.add_done_callback(self._manager._report_tasks.discard)
        except Exception:
            logger.debug("usage row (subagent) persist failed", exc_info=True)
        _row_t0[0] = time.monotonic()

    # Stop-hook continuations this run has sent, the depth a Stop hook reads.
    _stop_continuations = [0]

    def _stop_hook_may_continue() -> bool:
        return not (
            info.user_stopped
            or self._stop_in_progress(info)
            or info._reap_started
            or info.reaped
            or self._manager._shutting_down
        )

    async def _fire_stop_hooks(done: LLMEvent | None, *, may_continue: bool = True) -> str | None:
        """Fire Stop for the prompt that just ended; the next prompt when a
        hook continues the child (``stop_hooks.child_stop_continuation``)."""
        nonlocal _final_segment, result_text, _result_file_started
        store = self._manager.hook_store
        if store is None:
            return None
        from kiro_crew.subagent_manager.stop_hooks import child_stop_continuation

        _next = await child_stop_continuation(
            store,
            final_text=_final_segment,
            session_key=session_key,
            info=info,
            spec=_spec,
            client=client,
            depth=_stop_continuations[0],
            ended_ok=classify_stop_reason(
                str(getattr(done, "stop_reason", "") or "") if done is not None else ""
            ).is_success,
            may_continue=may_continue,
            can_continue=_stop_hook_may_continue,
            log=logger,
        )
        if _next is None:
            return None
        _stop_continuations[0] += 1
        # The next Stop judges the continuation's own answer.
        _final_segment = ""
        # The parent gets both prompts' answers, a blank line apart, in the
        # result, the live partial and result.txt alike.
        if result_text:
            result_text += "\n\n"
            if _attempt_has_text:
                info.streaming_text += "\n\n"
            if _result_file_started:
                _result_file_started = write_result_chunk(info.id, "\n\n")
        return _next

    async def _stream_with_transient_retry():
        """Yield stream events, retrying transient backend errors.

        Parity with the main path's retry ladder (chat_runner B1/B2):
        - Pre-token (no text streamed yet): re-send the SAME prompt on the
          same live session, up to TRANSIENT_RETRIES with exp backoff.
        - Post-token (partial already streamed): send a CONTINUE prompt so
          the preserved partial is finished, not duplicated.
        Non-transient errors and exhausted budgets propagate unchanged
        (handled by _run's generic exception arm → error tombstone).
        """
        nonlocal _final_segment
        attempts = 0
        # One-shot post-activity allowance, mirroring the main path's
        # ``_posttoken_retry_used`` rule (_on_acp_posttoken_transient, chat_turn/acp_recovery.py):
        # each continuation turn issued AFTER observed activity is an
        # independent opportunity for the model to repeat a side-effecting
        # tool, so post-activity recovery gets exactly ONE attempt.
        # TRANSIENT_RETRIES applies only while zero activity was observed
        # (replaying the bare prompt is side-effect-free by definition).
        # PARITY NOTE: this ladder and chat_runner's are two copies with
        # intentionally identical semantics — a fix to either's activity
        # predicate or budget rules must be mirrored in the other.
        post_activity_attempts = 0
        # Throttle-exhaustion fallback chain (agent.fallback_model):
        # engaged only once the zero-activity budget above is spent, same
        # trigger as stream_and_collect's Case 2.75 and the dashboard's
        # fallback branch. State is per-run (this closure), matching
        # "a slot/session-scoped equivalent" — the sticky marker for the
        # session lives on the provider via TURN_FALLBACK_ATTR.
        _fb_state = FallbackState(configured_fallback_chain())
        msg = full_message
        while True:
            usage.begin(client)
            # A new prompt starts with nothing in flight. A call the last
            # attempt or turn never closed would otherwise wait in the
            # in-flight set and be handed the slot when a later call returns.
            self._manager._clear_tool_dispatch(info)
            _refused_call.clear()
            _person_rejected[0] = False
            try:
                if not use_session_sharing:
                    # Publish the live dedicated PID before every prompt,
                    # including retries after a provider process replacement.
                    # Shared sessions do not own their runtime's PID mapping.
                    from kiro_crew.messaging.identity import publish_turn_identity

                    await publish_turn_identity(self._manager._sessions, session_key)
                # A completion whose stop reason classifies as RECOVERABLE
                # (tool stall, stale_recover) is withheld from the run loop
                # while budget remains: the slot is yielded, re-admitted,
                # and a continue-nudge is sent on the SAME session so the
                # preserved partial is finished in place — never a verbatim
                # re-run of the original task. Mirrors chat_runner's
                # tool-stall continuation and shares its budget
                # (STOP_RECOVERY_MAX_RETRIES). Once the budget is spent the
                # completion is surfaced and the run ends `failed` with the
                # partial flagged.
                #
                # A completion that ended NORMALLY right after a tool call
                # the MCP gateway refused for capacity (``last_infra_error``
                # on the session handle) is the ladder's L1 case: withheld
                # the same way, the slot yielded to the gateway-capacity
                # scope's shared schedule, and the refused call re-issued
                # by a continuation on the same session.
                _withheld: LLMEvent | None = None
                _infra: Any = None
                # A completion CANCELLED right after the host refused a call:
                # codex's only reject option for a shell command aborts the
                # whole turn. It is withheld, and the turn resumes on the same
                # session with the refusal as the next prompt.
                _refused_turn: LLMEvent | None = None
                # The completion that ends this prompt, if the stream sent one.
                _ended: LLMEvent | None = None
                stream = (
                    client.stream(msg)
                    if prompt_timeout is None
                    else client.stream(msg, timeout=prompt_timeout)
                )
                async for _ev in stream:
                    if not _ev.runtime_global:
                        # A frame addressed to THIS session: the run's own
                        # turn exists, so the durable row is ``running`` and
                        # the run is out of startup from here. Taken on the
                        # raw frame, ahead of the withholding below, because
                        # a completion withheld for recovery is an answer
                        # too. A ``runtime_global`` frame is a co-tenant's
                        # traffic that the shared runtime fanned out, so it
                        # proves nothing about this start (see the
                        # activity-clock note in the loop that consumes this
                        # stream).
                        self.ensure_running_marked(info)
                        self._manager._leave_startup(info)
                    elif info._first_stream_started is None:
                        # Kept for the startup reap's record, so a start
                        # that saw only co-tenant traffic reads apart from
                        # one whose stream stayed silent.
                        info._startup_cotenant_frames += 1
                    if _ev.kind == EVENT_STRUCTURED_STATUS:
                        # W4: the execution layer (or the liveness oracle)
                        # says a tool is waiting for real input. The lane
                        # slot is released now, the residency stays; the
                        # completion that follows (cancel policy) re-enters
                        # through the stop-recovery path.
                        self.lane_wait_for_status(info, _ev)
                    if _ev.kind == EVENT_COMPLETE:
                        _stop_c = classify_stop_reason(getattr(_ev, "stop_reason", ""))
                        if self._manager._stop_recovery_wanted(info, _stop_c):
                            _withheld = _ev
                            continue
                        if (
                            _stop_c.name == STOP_CLASS_CANCELLED
                            and _refused_call
                            and not _person_rejected[0]
                            and _refusal_resumes[0] < REFUSED_TURN_RESUME_MAX
                            and not info.user_stopped
                            and not self._stop_in_progress(info)
                            and not info._reap_started
                            and not info.reaped
                            and not self._manager._shutting_down
                        ):
                            _refused_turn = _ev
                            continue
                        _last_infra = getattr(client, "last_infra_error", None)
                        if (
                            _stop_c.is_success
                            and isinstance(_last_infra, InfraError)
                            and not info.user_stopped
                            and not info._reap_started
                            and not self._manager._shutting_down
                        ):
                            _withheld = _ev
                            _infra = _last_infra
                            continue
                        # The prompt's own end: held back until the Stop
                        # hooks below have judged it. The stream is drained,
                        # as for a withheld completion, so its own cleanup
                        # (the turn's active mark and lock) runs before any
                        # continuation prompt starts.
                        _ended = _ev
                        continue
                    yield _ev
                if _refused_turn is not None:
                    # Every prompt end fires Stop, a resumed one included.
                    await _fire_stop_hooks(_refused_turn, may_continue=False)
                    _title, _reason, _cause = _refused_call[-1]
                    _resume = build_refused_turn_resume_prompt(_title, _reason, cause=_cause)
                    if not _resume:
                        yield _refused_turn
                        return
                    usage.settle(_refused_turn)
                    _refusal_resumes[0] += 1
                    logger.info(
                        "Subagent %s: turn cancelled after a host refusal; resuming (%d/%d)",
                        info.id,
                        _refusal_resumes[0],
                        REFUSED_TURN_RESUME_MAX,
                    )
                    _final_segment = ""
                    msg = _resume
                    continue
                if _withheld is None:
                    # A stream that ends with no completion event ended
                    # normally too, and its Stop hooks fire the same way.
                    _next = await _fire_stop_hooks(_ended)
                    if _next is None:
                        if _ended is not None:
                            yield _ended
                        return
                    # Settled and recorded even with no completion event: the
                    # next prompt's ``usage.begin`` resets the baseline, and
                    # ``settle(None)`` reads the provider's own last-turn stats.
                    usage.settle(_ended)
                    _persist_usage_row(_ended)
                    logger.info(
                        "Subagent %s: a Stop hook asked to continue (%d)",
                        info.id,
                        _stop_continuations[0],
                    )
                    msg = _next
                    continue
                # Preserve this turn's billing before recovery can cancel
                # or begin another attempt with a fresh usage baseline.
                usage.settle(_withheld)
                await _fire_stop_hooks(_withheld, may_continue=False)
                if _infra is not None:
                    _nudge = await self._manager._yield_for_infra_retry(info, _infra)
                else:
                    _nudge = await self._manager._yield_for_stop_recovery(info, _withheld)
                if _nudge is None:
                    # Re-admission failed: surface the withheld completion
                    # so the run ends through the classifier as `failed`
                    # (or, for a spent L1 budget, as the normal completion
                    # it was -- the parent sees the refused call in the
                    # partial).
                    yield _withheld
                    return
                _final_segment = ""
                msg = _nudge
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                usage.settle()
                if not acp_error_is_transient(exc):
                    raise
                # Post-activity: continue instead of re-running. "Activity"
                # is ANY text chunk, approved tool turn, or auto-allowed
                # tool call — a mutating tool may have executed before the
                # first text chunk, and replaying the full prompt would
                # re-run it (duplicate writes/messages). Only a turn with
                # zero observed activity resends the original prompt.
                _had_activity = bool(result_text) or turns > 0 or info.tool_count > 0
                # The one continue is spent and the backend still fails to
                # generate after real output: end the stream and keep that
                # output (flagged below) instead of failing a run that answered.
                if (
                    post_activity_attempts >= 1
                    # A bare [OPTIONS: ...] tag is stripped later, leaving nothing.
                    and extract_options(result_text)[0].strip()
                    and "failed to generate a response" in str(exc).lower()
                ):
                    nonlocal _kept_after_generate_failure
                    _kept_after_generate_failure = True
                    logger.warning("Subagent %s: kept output after a transient error", info.id)
                    return
                # A failure an adapter recognises as a DEPENDENCY condition
                # (provider throttle, 5xx, connection loss: taskq.dependency)
                # is not retried here: the run reports it to the per-scope
                # coordinator -- ONE schedule for every session on that
                # scope -- and yields its lane slot until the scope wakes
                # it by capacity. The in-turn ladder below stays for
                # transients no adapter classifies, for a manager without a
                # durable queue, and for a run that has no task row at all
                # (``None``: nothing records its wait and nothing else owns
                # its work). A row another owner holds, a row that ended,
                # or a store that cannot say stops the run (``False``).
                _signal = classify_exception(exc) if _dep_coordinator is not None else None
                if _signal is not None and not _signal.terminal:
                    if _had_activity and post_activity_attempts >= 1:
                        raise
                    _parked = await self._manager._yield_for_dependency(info, _signal)
                    if _parked is False:
                        raise
                    if _parked:
                        if _had_activity:
                            post_activity_attempts += 1
                            msg = _TRANSIENT_CONTINUE_MSG
                        else:
                            msg = full_message
                        continue
                if _had_activity:
                    if post_activity_attempts >= 1:
                        raise
                    post_activity_attempts += 1
                elif attempts >= TRANSIENT_RETRIES:
                    # ── Throttle-exhaustion fallback chain ──
                    # Zero-activity budget spent: walk agent.fallback_model
                    # before surfacing (empty chain ⇒ raise directly).
                    # Two attempts per candidate
                    # (FALLBACK_CANDIDATE_ATTEMPTS), ~2s backoff each —
                    # NOT the exponential same-model curve; see
                    # llm_helpers Case 2.75 for the rationale.
                    if not _fb_state.chain:
                        raise
                    if not _fb_state.should_retry_active():
                        _cand = await advance_fallback_candidate(
                            client,
                            _fb_state,
                            surface="subagent",
                            log_suffix=f", id={info.id}",
                        )
                        if _cand is None:
                            _story = _fb_state.exhaustion_story()
                            if _story:
                                logger.warning(
                                    "model fallback: chain exhausted (%s) for "
                                    "subagent %s; surfacing original error",
                                    _story,
                                    info.id,
                                )
                                try:
                                    setattr(exc, FALLBACK_STORY_ATTR, _story)
                                except Exception:
                                    pass
                            raise
                    _fb_delay = transient_retry_delay(1)
                    await self._manager._fire_event(
                        "subagent_retrying",
                        info,
                        {
                            "attempt": _fb_state.attempts,
                            "max": FALLBACK_CANDIDATE_ATTEMPTS,
                            "fallback_model": _fb_state.active or "",
                        },
                    )
                    try:
                        sel().log_api_access(
                            caller=info.parent_session_key or f"subagent:{info.id}",
                            operation="subagent.model_fallback_retry",
                            outcome="retrying",
                            source="subagent",
                            resources=(
                                f"subagent_id={info.id},"
                                f"model={_fb_state.active or ''},"
                                f"attempt={_fb_state.attempts}"
                            ),
                        )
                    except Exception:
                        logger.debug("SEL audit for fallback retry failed", exc_info=True)
                    await asyncio.sleep(_fb_delay)
                    # Zero activity by construction on this arm — replay
                    # the original prompt, never a continuation.
                    msg = full_message
                    continue
                attempts += 1
                delay = transient_retry_delay(attempts)
                logger.warning(
                    "Subagent %s: transient backend error (attempt %d/%d), "
                    "retrying in %.1fs: %s",
                    info.id,
                    attempts,
                    TRANSIENT_RETRIES,
                    delay,
                    exc,
                )
                await self._manager._fire_event(
                    "subagent_retrying",
                    info,
                    {"attempt": attempts, "max": TRANSIENT_RETRIES},
                )
                try:
                    sel().log_api_access(
                        caller=info.parent_session_key or f"subagent:{info.id}",
                        operation="subagent.transient_retry",
                        outcome="retrying",
                        source="subagent",
                        resources=f"subagent_id={info.id},attempt={attempts}",
                    )
                except Exception:
                    logger.debug("SEL audit for transient retry failed", exc_info=True)
                await asyncio.sleep(delay)
                msg = _TRANSIENT_CONTINUE_MSG if _had_activity else full_message

    _complete_event: LLMEvent | None = None
    # Wall clock for THIS subagent's own turn. Deliberately started here,
    # at the subagent's own stream, not on the parent side: under session
    # sharing this subagent reuses the parent's runtime, so a parent-side
    # clock would charge the child for the parent's elapsed time. acp
    # leaves TurnUsage.duration_ms at 0, so the row needs this.
    # Includes transient-retry backoff, which is real wall time the caller
    # waited for this turn.
    _turn_t0 = time.monotonic()
    _row_t0[0] = _turn_t0
    async for event in _stream_with_transient_retry():
        # Refresh the activity clock for every event kind that BELONGS to
        # this session (thinking chunks, tool-call updates, etc.) before
        # dispatch, so idle-stall detection only trips on a genuine no-event
        # hang -- not on an event kind this switch does not special-case.
        #
        # ``runtime_global`` events are the one exclusion: the frame behind
        # them carried no ``sessionId`` and the runtime fanned it out to
        # several sessions sharing one kiro-cli process, so it is another
        # tenant's traffic. Under ``agent.session_sharing`` (default true)
        # co-tenant subagents are separate sessions on the parent's runtime,
        # and counting the roster broadcast as activity reset
        # ``last_activity`` for a whole batch of wedged subagents at the same
        # instant, cleared their "stalled" badge and restarted the idle count
        # on agents that had made no progress -- so the badge flapped and the
        # reported ``idle_secs`` measured time since an unrelated agent's
        # roster churn. Field data: three co-tenants flagged in one reaper
        # sweep at idle 214s/214s/215s (one shared refresh instant) while
        # their elapsed was 1445s/1447s/1538s.
        #
        # Deliberately a PROVENANCE test, not an event-kind test: the same
        # kind reached through a routed frame (the KAS sub-agent lifecycle
        # path) is this session's own progress and must still count, or a
        # working agent gets falsely badged. Approval waits stay exempt via
        # _awaiting_approval.
        #
        # Plain attribute access: every provider yields ``LLMEvent`` (an
        # alias of ``AcpEvent``), which declares the field, so there is no
        # shape here that could raise. A hop that forgets to carry the flag
        # degrades to the default False, i.e. "counts as activity" -- the
        # fail-open direction, which can only delay a badge, never invent
        # one.
        if not event.runtime_global:
            await self._manager._touch_activity(info)
        if event.kind == EVENT_TEXT_CHUNK:
            # The CC/raw provider only learns its served model once the
            # backend answers the first turn — by the first text chunk that
            # has happened, so refresh here. Runs once (guarded on a still-
            # empty value) and stays cheap: covers every downstream exit
            # path (normal, turn_limit, child-escalation, cancel) without
            # threading the live client through each. Never overwrites a good
            # spawn-time read with "".
            if not info.resolved_model:
                _live_model = _resolved_model_of(client)
                if _live_model:
                    info.resolved_model = _live_model
                    # Persist the CC-path refinement so a restart after the
                    # first turn still recovers the served model. Off-loop
                    # and drained on cancellation via _write_state_off_loop.
                    try:
                        await self._manager._write_state_off_loop(
                            info, "refined model", resolved_model=_live_model
                        )
                    except Exception:
                        logger.debug(
                            "Failed to persist refined model for %s", info.id, exc_info=True
                        )
            result_text += event.text
            _final_segment += event.text
            if not _attempt_has_text:
                # The in-memory partial a stop or a cancel delivers. It
                # keeps growing when the file cannot be written, which is
                # exactly when it is the only copy left.
                _attempt_has_text = True
                info.streaming_text = ""
            # The live partial takes the chunk before the file is
            # started off the loop, so a Stop landing in that wait still
            # delivers it.
            redacted = _redact(event.text)
            info.streaming_text += redacted
            if len(info.streaming_text) > 50_000:
                info.streaming_text = "…(truncated)\n" + info.streaming_text[-40_000:]
            if _result_file_started:
                # A refused append starts the file over on the next
                # chunk, so a lost chunk leaves no hole mid-file either.
                _result_file_started = write_result_chunk(info.id, event.text)
            else:
                _result_file_started = await self._manager._start_result_file(info, result_text)
            await self._manager._fire_event("subagent_chunk", info, {"text": redacted})
        elif event.kind == EVENT_PERMISSION_REQUEST:
            # Both kiro-cli and claude-agent-acp surface tool calls via
            # session/request_permission. Every one is settled by the run's
            # permission ladder (``_permission_policy``), the limit bails
            # below included.
            #
            # Child-origin escalations (runtime-routed backend subagents)
            # do NOT consume the parent's turn budget: a child asking for
            # enough permissions would otherwise trip the parent's
            # turn_limit and kill the whole subagent run on activity that
            # is not the parent's own turns. They get their OWN bound
            # instead — without one, a chatty or adversarial backend
            # child could generate unbounded approval prompts until the
            # wall-clock reaper fires. Generous multiple of the parent's
            # limit: legitimate crews fan many small child tool calls.
            # The id the request names the tool by is what the call's
            # PostToolUse spec hooks match on.
            if event.tool_call_id:
                _named = post_tool_identity(
                    client,
                    event,
                    is_shell=False,
                    earlier=_pending_tool_ids.get(event.tool_call_id),
                )
                if _named is not None:
                    _pending_tool_ids[event.tool_call_id] = _named
            _ask = tool_permission.Ask(event, _wire, session_key)
            # A run that is being stopped bails every request as ``stopped``
            # before any grant or person is consulted: audited, and rejected
            # bare on the wire. The stop cancels this task only after its
            # session teardown; when the teardown and its kill both fail, the
            # stream keeps going, and a request read here would otherwise be
            # settled for a run the user stopped. The request is answered
            # (it is already dequeued) and the run ends here, as at the limit
            # bails below; the stream's own ending arms record the stop.
            if self._stop_in_progress(info):
                logger.warning(
                    "Subagent %s: rejecting a tool request, a stop is in progress",
                    info.id,
                )
                try:
                    await tool_permission.bail(_ask, _policy, "stopped")
                except Exception:
                    logger.exception("failed to reject a request after a stop")
                break
            if not event.sub_session_id:
                turns += 1
                info.turns = turns
                # The run's own gated call is a tool-call boundary too (a
                # backend may surface it ONLY as this request), so the
                # final segment starts again after it.
                _final_segment = ""
            else:
                # Child escalation: counted toward its own volume bound
                # here; side-effect activity (tool_count) is counted at
                # APPROVAL by the ladder's narrator — a purely rejected
                # escalation executed nothing and must not consume the
                # run's replay budget (tool_count gates prompt replay
                # and cancel-respawn).
                child_escalations += 1
                if child_escalations > child_escalation_limit:
                    # Answer the triggering request BEFORE bailing: this
                    # event is already dequeued, so returning without a
                    # response would strand the child's oneshot — under
                    # session sharing the runtime outlives this subagent
                    # and nothing else tears the connection down.
                    try:
                        await tool_permission.bail(_ask, _policy, "child_escalation_limit")
                    except Exception:
                        logger.exception("failed to reject escalation-limit trigger request")
                    info.result = result_text or "_Partial output._"
                    info.error = f"child_escalation_limit:{child_escalation_limit}"
                    info.done = True
                    # In-run terminal flip: the report is spawned only
                    # after this return unwinds to ``_run``'s ``finally``
                    # -- arm with the flip.
                    self._manager.arm_report_in_flight(info)
                    Stats().inc_subagent_failed()
                    logger.warning(
                        "Subagent %s hit child escalation limit (%d)",
                        info.id,
                        child_escalation_limit,
                    )
                    usage.settle()
                    self._manager._write_tombstone(info, "child_escalation_limit")
                    return
            # Diagnostic pointer is written for BOTH origins — orphan
            # recovery must see child activity too; only the turn
            # increment is parent-scoped.
            info.last_tool = event.title or ""
            self._manager._note_tool_dispatch(info, event)
            # Persist turn state for orphan recovery diagnostics. Off-loop
            # and drained on cancellation via _write_state_off_loop — the
            # highest-frequency of the three off-loop state
            # writers, so the one most likely to be in flight when a
            # cancellation lands.
            try:
                await self._manager._write_state_off_loop(
                    info, "diagnostics", turns=turns, last_tool=event.title or ""
                )
            except Exception:
                pass
            await self._manager._fire_event(
                "subagent_tool",
                info,
                {
                    "tool": _redact(event.title or ""),
                    "tool_kind": event.tool_kind,
                    "turns": info.turns,
                    "tool_count": info.tool_count,
                    # Lets the card's tool-call list tell this request apart
                    # from the parent's own call that announced it (same id,
                    # same tool_count) and from a child's request (other id,
                    # tool_count unchanged).
                    "tool_call_id": _redact(event.tool_call_id or ""),
                },
            )
            if turns > turn_limit:
                # Same contract as the child_escalation_limit bail: the
                # triggering request is already dequeued and must be
                # answered before this loop exits, or its oneshot strands.
                try:
                    await tool_permission.bail(_ask, _policy, "turn_limit")
                except Exception:
                    logger.exception("failed to reject turn-limit trigger request")
                info.result = result_text or "_Partial output._"
                info.error = f"turn_limit:{turn_limit}"
                info.done = True
                # In-run terminal flip -- same arm as the escalation-limit
                # return above.
                self._manager.arm_report_in_flight(info)
                Stats().inc_subagent_failed()
                logger.warning("Subagent %s hit turn limit (%d)", info.id, turn_limit)
                usage.settle()
                self._manager._write_tombstone(info, "turn_limit")
                return
            settled = await tool_permission.settle(_ask, _policy)
            if settled.outcome == "rejected":
                # A person said no in this attempt: their answer stands, so a
                # cancelled ending is never resumed as a host refusal.
                _person_rejected[0] = True
            elif settled.outcome == "refused" and settled.cause is not None:
                _refused_call.append((event.title or "", settled.reason, settled.cause))
            if settled.rung == "stopped":
                # The ladder bailed this request for a stop that landed
                # while it was being settled: the run ends here, as at the
                # bail above. A person's no (a stop's cancel the approval
                # wait turned into a denial) reads on: a stream the stop tore
                # down raises at the next read, a live one meets the bail.
                break
        elif event.kind == EVENT_AGENT_SWITCHED:
            # A mid-run mode switch runs a different agent, so ITS spec hooks gate
            # the permission requests that follow, not the previous agent's. An
            # unnamed switch falls back to the agent the session recorded for it.
            _spec = await turn_spec_hooks(client, event.text or "")
            _policy = _policy_for(_spec)
            await refuse_stale_switch(client, event.text or "")
        elif event.kind == EVENT_TOOL_CALL:
            # Auto-allowed (kiro-internal) tools surface here as informational
            # tool_call updates and NEVER as EVENT_PERMISSION_REQUEST, so this
            # is the only progress signal a simple/read-only subagent task emits.
            # Count it, record it, and broadcast the same subagent_tool event
            # the permission path uses so the running-card shows live activity.
            info.tool_count += 1
            _final_segment = ""
            info.last_tool = event.title or info.last_tool
            self._manager._note_tool_dispatch(info, event)
            await self._manager._fire_event(
                "subagent_tool",
                info,
                {
                    "tool": _redact(event.title or ""),
                    "tool_kind": event.tool_kind,
                    "turns": info.turns,
                    "tool_count": info.tool_count,
                    "tool_call_id": _redact(event.tool_call_id or ""),
                },
            )
            # Fire PreToolUse hooks for auto-approved tools (informational only).
            # On a gated turn this frame precedes the call's permission request,
            # so nothing has approved it yet.
            sel().log_tool_invocation(
                session_key=session_key,
                source="subagent",
                tool_name=event.title,
                tool_kind=event.tool_kind,
                outcome="invoked" if _spec.gated else "auto_approved",
                metadata={"subagent_id": info.id},
            )
            # Cache tool name so PostToolUse can recover it on EVENT_TOOL_RESULT.
            # Strip "Running: " prefix to match the name passed to PreToolUse hooks.
            _raw = event.title or ""
            if _raw.startswith("Running: "):
                _raw = _raw[9:]
            if event.tool_call_id:
                _pending_tools[event.tool_call_id] = _raw
                _named = post_tool_identity(
                    client, event, is_shell=bool(getattr(event, "is_shell", False))
                )
                if _named is None:
                    _pending_tool_ids.pop(event.tool_call_id, None)
                else:
                    _pending_tool_ids[event.tool_call_id] = _named
            # A gated turn runs PreToolUse hooks on the permission request
            # instead (see hooks.permission_pre_tool_block); firing here too
            # would run each twice.
            if not _spec.gated:
                await fire_tool_hooks(
                    self._manager.hook_store,
                    event.title,
                    event.tool_input,
                    subagent_id=info.id,
                    parent_session_key=info.parent_session_key or None,
                    agent_role=info.agent or None,
                )
        elif event.kind == EVENT_TOOL_RESULT:
            # A FINAL result means the tool is done: drop the attribution
            # snapshot so a later idle stretch is not judged against a
            # command that has already returned. A non-final progress frame
            # is not the end of the tool — the gate is in _note_tool_result.
            self._manager._note_tool_result(info, event)
            # Fire PostToolUse hooks (parity with chat_runner). Until this
            # branch existed, hooks registered for subagent-spawned tools
            # received PreToolUse but never PostToolUse — losing the
            # tool_response payload.
            # Once per call, on its terminal result: a progress frame carrying
            # partial output is not the finished call.
            _terminal = bool(getattr(event, "tool_final", False)) or (
                getattr(event, "tool_status", None) in TERMINAL_TOOL_STATUSES
            )
            if self._manager.hook_store is not None and _terminal:
                try:
                    _tool_name = _pending_tools.pop(event.tool_call_id, "")
                    _named = _pending_tool_ids.pop(event.tool_call_id, None)
                    _out = _redact((event.tool_output or "")[:2000])
                    await self._manager.hook_store.fire(
                        HOOK_EVENT_POST_TOOL_USE,
                        tool_name=_tool_name,
                        tool_response={"output": _out},
                        # This child's own key, for per-session attribution;
                        # parent_session_key below names who spawned it.
                        session_key=session_key,
                        subagent_id=info.id,
                        parent_session_key=info.parent_session_key or None,
                        agent_role=info.agent or None,
                        extra_hooks=_spec.hooks,
                        extra_hooks_cwd=_spec.cwd,
                        # The spec's matchers are kiro-cli tool names, which a
                        # KAS call's title is not; None keeps title matching.
                        extra_hooks_tool_names=(
                            pre_tool_match_names(
                                _tool_name,
                                tool_identity=_named.tool_identity,
                                mcp_server=_named.mcp_server,
                                harness_tool_id=_named.tool_id,
                            )[1]
                            if _named is not None and _spec.hooks
                            else None
                        ),
                    )
                except Exception:
                    logger.debug(
                        "PostToolUse hook error in subagent",
                        exc_info=True,
                    )
        elif event.kind == EVENT_COMPLETE:
            _complete_event = event
            usage.settle(event)
            break

    # A provider may finish without an explicit completion event. Its
    # current prompt stats are still the authoritative billing record.
    usage.settle()

    # Strip [OPTIONS: ...] tags and redact sensitive content
    cleaned, _ = extract_options(result_text) if result_text else (result_text, [])
    # Read before the fallback annotation, which is not a reply.
    _no_reply = not cleaned.strip()
    if cleaned:
        from kiro_crew.security import (
            redact_credentials,
            redact_exfiltration_urls,
        )

        cleaned, _ = redact_exfiltration_urls(cleaned)
        cleaned, _ = redact_credentials(cleaned)
    # Model-fallback visibility (agent.fallback_model): a run served by a
    # fallback model must say so in the delivered result — same contract as
    # the cron/heartbeat annotation and the dashboard notice card. One
    # shared spelling (llm_helpers.annotate_model_fallback) redacts the
    # config-sourced model ids the same way as the result body.
    cleaned = annotate_model_fallback(cleaned, client)
    # EVENT_COMPLETE only says the stream ENDED. Classify its stop reason
    # (one mapping for every entry: acp.types.classify_stop_reason) so a
    # stall, a runtime cancel or a transport death is never recorded as a
    # finished result. The compaction-transient verdict is deliberately NOT
    # passed here: a sub-agent has no reset+resume ladder to recover a
    # transient compaction failure in place, so for it that reason is
    # terminal (the main chat passes the verdict and re-queues).
    _stop = classify_stop_reason(
        str(getattr(_complete_event, "stop_reason", "") or "")
        if _complete_event is not None
        else ""
    )
    info.stop_reason = _stop.stop_reason
    info.stop_class = _stop.name
    if _stop.is_success and _final_segment:
        # A normal end of turn whose final segment carries a tool call
        # written as prose: the call never ran, yet the run reads as a
        # success. Flag it for the parent, whatever the earlier tool-call
        # count, NOTICE-ONLY like the dashboard's mixed-turn notice
        # (should_notice_mixed_turn_leak): earlier calls may have taken
        # effect, so the outcome stays completed and nothing is re-issued.
        # Deferred import: a core module must not import the dashboard
        # layer at import time.
        from kiro_crew.dashboard.chat_utils_turn_text import has_leaked_tool_call

        info.leaked_tool_call = has_leaked_tool_call(_final_segment)
    if not _stop.is_success:
        # Whatever streamed before the non-success completion is a PARTIAL:
        # preserved (result + result.txt) and flagged, never a finished
        # answer. A cancel the user asked for keeps the neutral record
        # contract (error unset, ``user_stopped`` carries the outcome).
        info.partial = bool(cleaned)
        if not info.user_stopped:
            info.error = self._manager._stop_error_text(info, _stop, _complete_event)
    info.result = cleaned or "_No response._"
    # Flag whether the completion-event copy will drop content, so the gateway
    # emits a summary + result_path pointer (read on demand) instead of a lossy
    # blob. The full transcript stays in result.txt for the TTL grace window.
    info.result_truncated = (
        self._manager._completion_keep_chars > 0
        and len(info.result) > self._manager._completion_keep_chars
    )
    info.result = apply_completion_keep(
        info.result,
        self._manager._completion_keep,
        self._manager._completion_keep_chars,
    )
    if _kept_after_generate_failure:
        # Added AFTER the keep cap, so no keep mode can cut it off.
        _warn = (
            "_Warning: the backend failed to generate a final response; "
            "this is the output streamed before that._"
        )
        info.result = f"{_warn}\n\n{info.result}"
        info.partial = True
        # result.txt is what spawn_status / spawn_run read for a done run.
        write_result_chunk(info.id, f"\n\n{_warn}\n")
    # The whole answer a claimed completed ending rewrites result.txt from.
    _finished_text = result_text
    if _stop.is_success and _no_reply and not info.made_no_tool_calls:
        # A child that called a tool and then closed without a reply: its side
        # effects already landed, so a bare "_No response._" invites the
        # parent to re-spawn it and run them twice. Flag it partial and say
        # to continue, never resend (messaging.empty_turn_copy). Added after
        # the keep cap, like the warning above, so no keep mode cuts it off.
        from kiro_crew.messaging.empty_turn_copy import EMPTY_TURN_NOTICE_AFTER_WORK

        info.partial = True
        _base = info.result.strip() or "_No response._"
        info.result = f"{_base}\n\n{EMPTY_TURN_NOTICE_AFTER_WORK}"
        _finished_text = info.result
    evict_completed_agents(self._manager._agents)

    _persist_usage_row(_complete_event)

    def _count_success() -> None:
        # The one success bookkeeping, for a claimed ending and the tail's alike.
        self._manager._sessions.record_success(session_key)
        Stats().inc_subagent_completed()
        logger.info("Subagent %s completed", info.id)

    # ── The ending, claimed once. ──
    # No await since the complete event, so this is first-arrival-wins
    # against every stop. A whole answer -- a SUCCESSFUL complete event: the
    # generator can also just stop between chunks when the transport dies,
    # and that absent stop reason classifies as a normal end of turn, which
    # the explicit ``_complete_event`` check tells apart -- claims the
    # completed ending unless a stop got here first. From the claim on, every
    # stop path treats the run as ``done`` and does nothing, and a cancel,
    # the shutdown or the deadline only cuts this tail short: ``done`` is
    # recorded either way, and _run's arms leave a recorded ending alone.
    # An ``error`` already stamped is a stop too: a failed child under
    # ``on_child_failure=fail_parent`` and an expired wait stamp it and only
    # schedule their cancel.
    if (
        _complete_event is not None
        and _stop.is_success
        and not (info.done or info._reap_started or info.user_stopped or info.error)
    ):
        info._ending_claimed = True
        _count_success()
        try:
            # result.txt rewritten whole from memory, then the flag that says
            # so (``subagent_persistence.result_is_whole``).
            await self._manager._write_finished_result_off_loop(info, _finished_text)
        except Exception:
            logger.warning("Failed to record result_complete for %s", info.id, exc_info=True)
        finally:
            # Read before the flip: the hold belongs to the line that makes
            # the done transition (see the ``was_done`` arm below).
            _flipped = not info.done
            info.done = True
            # The claimed-success flip is a terminal done transition like
            # any other, and the most common one. _run's finally spawns the
            # report task then awaits the session teardown; the report's own
            # arm runs only after that teardown await, so without arming
            # here member A is done, not pending, and not in flight for the
            # whole teardown window -- a sibling completion would read the
            # wave as closeable and finalize it early, and A's late report
            # would finalize it again. Arm with the flip.
            if _flipped:
                self._manager.arm_report_in_flight(info)
        return
    # Any other ending, including a whole answer a stop got to first: _run's
    # ``finally`` caps the streamed file and records no whole answer.
    if _stop.is_success and info._reap_started and not info.done:
        # A reap in flight got here first and owns this ending: ``_run``
        # records the stop it is making and leaves the report to it, as for
        # the runtime death its teardown causes. A bare ``done`` here would
        # make the reap's record guard skip, so a deadline-stopped run would
        # be reported completed. A stream that just stopped, with no
        # complete event, had not finished its answer, and the record says so.
        info._answer_finished = _complete_event is not None
        logger.info(
            "Subagent %s %s after a reap began",
            info.id,
            "finished its answer" if info._answer_finished else "stream ended",
        )
        return
    was_done = info.done
    info.done = True
    # Success is a terminal done transition like any other: between this
    # flip and the report task's first run, the caller's teardown can
    # yield -- arm with the flip so the window is closed on EVERY terminal
    # path, not just failures. Only on the transition: when another path
    # (``was_done``, a reap) recorded the ending first, it armed at its own
    # flip and its report releases that hold, possibly already. ``_run``'s
    # finally then spawns no report (the claim is gone), so a re-arm here
    # would pin the wave-close fallback until ``finalize_batch``.
    if not was_done:
        self._manager.arm_report_in_flight(info)
    if info.user_stopped:
        # The user-stop path owns the tombstone/stat for this record.
        logger.info("Subagent %s stream ended by user stop (%s)", info.id, _stop.stop_reason)
    elif _stop.is_success and not info.error:
        if was_done:
            # Another path recorded this run's ending first; it owns the stat.
            logger.info("Subagent %s finished after its ending was recorded", info.id)
        else:
            _count_success()
    else:
        Stats().inc_subagent_failed()
        _tombstone_cause = "cancelled" if _stop.name == STOP_CLASS_CANCELLED else "error"
        if not self._leave_for_next_start_impl(info, stop_reason=_stop.stop_reason):
            self._manager._write_tombstone(info, _tombstone_cause)
        logger.warning(
            "Subagent %s ended %s (stop_reason=%r, recovery attempts=%d, partial=%s)",
            info.id,
            _stop.name,
            _stop.stop_reason,
            info._stop_recovery_used,
            info.partial,
        )
