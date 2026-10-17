"""Run a message cron job's agent turn: an agent sequence, or one agent.

The single-agent turn keeps its dispatch, its ACP-death and transient retries, the
runtime-death attribution call and its cleanup in one method, so the flags its
failure handler and its ``finally`` read stay plain locals of that method.

Orchestrator fields read: ``_cfg``, ``_cron_injecting``, ``cron_svc``, ``ctx_builder``,
``sessions``, ``subagent_mgr``.
Orchestrator fields written: none.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import time

    from kiro_crew.slack.gateway import (
        _CRON_TRANSIENT_RETRIES,
        SESSION_NOT_FOUND_GIVE_UP_TEXT,
        SESSION_NOT_FOUND_NOT_REPLAYED_TEXT,
        UNATTENDED_MCP_WAIT_SECS,
        AcpError,
        AcpProcessDied,
        Any,
        Awaitable,
        Callable,
        CronJob,
        GatewayOrchestrator,
        SessionClosingError,
        ToolApprovalPolicy,
        _annotate_model_downgrade,
        _annotate_model_fallback,
        _annotate_partial_block,
        _apply_gate_verdict,
        _cron_failure_attribution,
        _cron_stream_with_posttoken_resume,
        _CronCapture,
        _dedup_text,
        _defer_cron_before_dispatch,
        _GateTally,
        _result_hash,
        _subagent_work_pending,
        acp_error_after_tool_activity,
        acp_error_is_session_not_found,
        acp_error_is_transient,
        configured_fallback_chain,
        consume_reinjection,
        context_meter_reading,
        dataclass,
        declared_mcp_not_ready_note,
        logger,
        persist_token_record_async,
        provider_fallback_active,
        provider_last_turn_usage,
        provider_model_pin_partial,
        publish_turn_identity,
        read_context_tokens,
        read_effective_agent,
        read_turn_model,
        rearm_reinjection,
        rollback_skill_bodies,
        run_in_embed_pool,
        stop_reason_landed,
        transient_retry_delay,
        turn_stats_meta,
        wait_for_declared_mcp,
    )


async def _cron_run_sequence(
    self: GatewayOrchestrator, job: CronJob, cap: _CronCapture
) -> str | None:
    """Run an agent-sequence message job, one step after another.

    Each step runs on its own stable session key and hands back, releases and
    resets that key in its own ``finally``; a step's failure propagates.
    """
    msg, cron_execution = cap.msg, cap.cron_execution
    cron_memory_store = cap.cron_memory_store
    agents = cap.cron_agents
    assert self.sessions is not None
    assert self.ctx_builder is not None

    @dataclass(frozen=True)
    class _SequenceStep:
        alias: str
        kiro_agent: str | None
        cwd: str | None
        crew: str | None
        dispatch_agent: str
        session_key: str

    # Resolve once per fire so the pre-loop comparison and dispatch
    # use one runtime identity. Keep a list parallel to ``agents``:
    # duplicate aliases are separate steps sharing one stable key.
    steps: list[_SequenceStep] = []
    for agent in agents:
        _seq_kagent, _seq_cwd, _seq_crew = self._resolve_cron_agent(
            cron_execution, agent, sequence_step=True
        )
        steps.append(
            _SequenceStep(
                alias=agent,
                kiro_agent=_seq_kagent,
                cwd=_seq_cwd,
                crew=_seq_crew,
                dispatch_agent=_seq_kagent or agent,
                session_key=f"cron:{job.id}:{agent}",
            )
        )

    # Sweep every stable per-step key before dispatching anything.
    # Deferring mid-loop would replay completed steps on the next
    # fire, so one mismatch defers the whole sequence instead.
    for step in steps:
        if not step.alias:
            logger.error(
                "Cron '%s': empty agent_sequence step cannot resolve a crew; "
                "refusing to dispatch",
                job.name,
            )
            _defer_cron_before_dispatch(
                job,
                f"cron '{job.name}': empty agent_sequence step cannot resolve a crew; "
                "refusing to dispatch",
            )
            # The job's own configuration causes this refusal, so a
            # retry meets it again: a plain one-shot is parked
            # (``run_parks_one_shot``) instead of retried every floor.
            job.refusal_cannot_clear = True
            return None
        _live_agent = self._retained_session_agent_mismatch(
            job, step.session_key, step.dispatch_agent
        )
        if _live_agent is not None:
            logger.info(
                "Cron '%s': sequence step %r session retained under agent %r "
                "but this fire resolves it to %r; deferring the whole sequence "
                "rather than reusing its runtime",
                job.name,
                step.alias,
                _live_agent,
                step.dispatch_agent,
            )
            _defer_cron_before_dispatch(
                job,
                f"sequence step {step.alias!r} session retained under agent "
                f"{_live_agent!r} with work pending; this fire resolves the "
                f"step to {step.dispatch_agent!r} and will not run a partial "
                "sequence",
            )
            return None

    result_text = "_No response._"
    # Each step's missing-server note, kept apart from result_text because the
    # next step's reply replaces it; the stored result carries all of them.
    _seq_mcp_notes: list[str] = []
    _seq_downgraded = False
    # Run-scoped: a sequence where one agent got a tool through has
    # done work, even if a later agent was blocked outright.
    _gate = _GateTally()
    for step in steps:
        # One re-load per step when a live backend lost this step's
        # session: the step is re-run on a fresh runtime that
        # session/loads the same id. A second loss ends the fire.
        _seq_snf_retry_used = False
        while True:
            _seq_snf_retry = False
            _seq_snf_err = ""
            agent = step.alias
            agent_session_key = step.session_key
            if self.cron_svc is not None:
                self.cron_svc.register_active_session_key(job.id, agent_session_key)
            _acq = False
            # Post-compaction re-injection bookkeeping for the finally:
            # consumed the one-shot flag / turn landed. The stream
            # returns text only, so the completion's stop reason is
            # captured through on_complete (last completion wins: a
            # post-token resume's continuation is the turn that ended).
            _seq_reinjection = False
            _seq_landed = False
            # None until a completion is observed: a stream that ends
            # without one is not landed.
            _seq_stop: dict[str, str | None] = {"reason": None}
            # Whether the backend reported a completed compaction during
            # the step's stream, which drops the session-start context.
            _seq_compaction: dict[str, bool] = {"completed": False}

            def _seq_note_complete(ev: Any, _box: dict[str, str | None] = _seq_stop) -> None:
                _box["reason"] = str(getattr(ev, "stop_reason", "") or "")

            def _seq_note_compaction(ev: Any, _box: dict[str, bool] = _seq_compaction) -> None:
                if getattr(ev, "text", "") == "completed":
                    _box["completed"] = True

            try:
                _seq_execution = cron_execution.with_template(
                    step.dispatch_agent, step.crew or step.alias
                )
                client, is_new, _resumed, _downgraded = await self._acquire_with_model_fallback(
                    job,
                    cron_execution,
                    step.session_key,
                    step.dispatch_agent,
                    step.cwd,
                    step.crew,
                    execution=_seq_execution,
                    replace_execution=True,
                )
                _seq_downgraded = _seq_downgraded or _downgraded
                _acq = True
                # Publish this turn's session identity so managed MCP
                # tools resolve their parent session. The cron path was
                # the ONE turn-running surface that skipped this (every
                # other surface publishes — see messaging.identity), and
                # under session sharing the runtime env carries no
                # KIROCREW_SESSION_KEY and macOS sets no
                # KIROCREW_HOST_PID, so the ancestor PID-walk over the
                # per-turn pidfile mapping is the only identity source
                # left. Without the publish, spawn_run resolved an
                # empty parent ("notification only (parent=)") unless an
                # unrelated surface happened to be mid-turn.
                await publish_turn_identity(self.sessions, agent_session_key)
                if is_new:
                    # Unattended: hold the first prompt (bounded) for the
                    # agent spec's MCP servers, which nobody can reconnect.
                    await wait_for_declared_mcp(client, UNATTENDED_MCP_WAIT_SECS)
                # A compaction drops session-start context. Read-and-clear
                # the one-shot flag so this turn re-injects it exactly
                # once; the finally re-arms it if the turn never lands.
                _seq_reinjection = consume_reinjection(self.sessions, agent_session_key)
                # Off-loop: build_message embeds the episodic query.
                full_message, _ = await run_in_embed_pool(
                    self.ctx_builder.build_message,
                    msg,
                    is_new,
                    agent_session_key,
                    interactive=False,
                    agent=agent,
                    memory_store=cron_memory_store or None,
                    execution_context=cron_execution,
                    context_provider=client,
                    resumed=_resumed,
                    needs_reinjection=_seq_reinjection,
                    minimal_context=job.minimal_context,
                )
                # Wall clock for the cron agent turn: acp never assigns
                # TurnUsage.duration_ms, so the row falls back to this.
                # Brackets only the model turn — session acquisition and
                # the episodic-query embed above are setup, not the turn.
                _turn_t0 = time.monotonic()
                result_text, _carried_credits = await _cron_stream_with_posttoken_resume(
                    client,
                    full_message,
                    job_name=job.name,
                    approval_policy=(
                        ToolApprovalPolicy.AUTO_APPROVE
                        if job.approval_mode == "auto"
                        else ToolApprovalPolicy.HOOK_BASED
                    ),
                    hooks=self.ctx_builder.hooks,
                    on_tool_approval=(
                        None if job.approval_mode == "auto" else self._interactive_approval("cron")
                    ),
                    on_tool_gate=_gate.note,
                    on_complete=_seq_note_complete,
                    on_compaction=_seq_note_compaction,
                    fallback_models=configured_fallback_chain(),
                )
                # The prompt reached the model and the turn completed, so
                # the finally must NOT restore the re-injection flag --
                # only for a succeeded stop reason.
                _seq_landed = stop_reason_landed(_seq_stop["reason"])
                if not result_text:
                    result_text = _gate.empty_reply_placeholder()
                result_text = _annotate_model_fallback(result_text, client)
                _seq_missing = declared_mcp_not_ready_note(client)
                if _seq_missing:
                    _seq_mcp_notes.append(f"⚠ {agent}: {_seq_missing}.")
                logger.info("Cron '%s': agent '%s' completed", job.name, agent)

                # ── Per-turn usage row: background spend. ──
                try:

                    _used, _window = read_context_tokens(client)
                    _turn_usage = provider_last_turn_usage(client)
                    if _carried_credits:
                        # A resumed turn's post-turn read sees only the
                        # continuation prompt; bill the interrupted
                        # prompt's snapshotted credits too.
                        _turn_usage.credits += _carried_credits
                    await persist_token_record_async(
                        agent_session_key,
                        # Blank on a downgrade: the configured model was
                        # unavailable and the default ran instead, so the
                        # requested id would attribute spend to a model
                        # that never executed. Blank defers to
                        # model_source, which reports what actually ran.
                        # A half-applied pair pin bills the bare
                        # model that ran, not the suffixed pin.
                        (
                            ""
                            if (_seq_downgraded or provider_fallback_active(client))
                            else (
                                (job.model and provider_model_pin_partial(client))
                                or job.model
                                or ""
                            )
                        ),
                        _turn_usage,
                        provider=(self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"),
                        surface="cron",
                        agent=read_effective_agent(client) or agent or "",
                        context_used=_used,
                        context_window=_window,
                        elapsed_ms=int((time.monotonic() - _turn_t0) * 1000),
                        model_source=client,
                    )
                except Exception:
                    logger.debug("usage row (cron seq) persist failed", exc_info=True)
            except Exception as _seq_exc:
                if not acp_error_is_session_not_found(_seq_exc):
                    raise
                # Re-running the step re-sends its prompt, which
                # could repeat a tool the lost turn already ran.
                if acp_error_after_tool_activity(_seq_exc):
                    raise AcpError(
                        f"{SESSION_NOT_FOUND_NOT_REPLAYED_TEXT} {_seq_exc}"
                    ) from _seq_exc
                if _seq_snf_retry_used:
                    raise AcpError(f"{SESSION_NOT_FOUND_GIVE_UP_TEXT} {_seq_exc}") from _seq_exc
                _seq_snf_retry_used = True
                _seq_snf_retry = True
                _seq_snf_err = str(_seq_exc)
                logger.warning(
                    "Cron '%s': backend lost the session of step %r, "
                    "re-loading it on a fresh runtime and retrying the step",
                    job.name,
                    agent,
                )
            finally:
                # Before the reset below: a turn that consumed the
                # post-compaction flag but never landed, or whose backend
                # compacted the session, sets the flag so a session that
                # survives (deferred reset) re-injects.
                rearm_reinjection(
                    self.sessions,
                    agent_session_key,
                    consumed=_seq_reinjection,
                    landed=_seq_landed,
                    compacted=_seq_compaction["completed"],
                )
                rollback_skill_bodies(self.ctx_builder, agent_session_key, landed=_seq_landed)
                if _acq:
                    self.sessions.release(agent_session_key)
                    # Mirror the single-agent finally below: defer the
                    # reset when this agent's sub-agents are still
                    # running, QUEUED behind the concurrency/stagger
                    # gate, or mid-injection — _subagent_done resets
                    # after the last one. Now that this path publishes
                    # turn identity, a non-final agent's spawn_run
                    # resolves a REAL parent key, so an unconditional
                    # reset here would tear down the session a pending
                    # completion is about to inject into (cold-starting
                    # a context-free replacement) and the completion's
                    # own cleanup would clear the reaper registration
                    # for the NEXT agent's still-in-flight turn.
                    _has_pending = bool(
                        self.subagent_mgr
                        and await _subagent_work_pending(self.subagent_mgr, agent_session_key)
                    )
                    _has_injecting = self._cron_injecting.get(agent_session_key, 0) > 0
                    if _has_pending or _has_injecting:
                        logger.info(
                            "Cron '%s': deferring reset of %s, subagents pending",
                            job.name,
                            agent_session_key,
                        )
                    else:
                        await self.sessions.reset(agent_session_key)
                        if self.cron_svc is not None:
                            self.cron_svc.clear_active_session_key(job.id, agent_session_key)
            if not _seq_snf_retry:
                break
            # The finally above defers its reset while sub-agents
            # are pending so a completion about to inject is not
            # torn down; honour that same deferral here. Spawning a
            # sub-agent is tool activity, so a loss with THIS turn's
            # sub-agents in flight already took the no-replay arm and
            # never reaches here; this guards a sibling step's
            # completion still injecting. When we must defer, let
            # _subagent_done do the reset and give up the retry (a
            # re-run against the un-reset stale binding would only
            # hit Session not found again), rather than reset under
            # the pending completion.
            _retry_has_pending = bool(
                self.subagent_mgr
                and await _subagent_work_pending(self.subagent_mgr, agent_session_key)
            )
            _retry_has_injecting = self._cron_injecting.get(agent_session_key, 0) > 0
            if _retry_has_pending or _retry_has_injecting:
                logger.info(
                    "Cron '%s': lost session of step %r but sub-agents "
                    "pending; deferring reset and not retrying this step",
                    job.name,
                    agent,
                )
                raise AcpError(f"{SESSION_NOT_FOUND_GIVE_UP_TEXT} {_seq_snf_err}")
            await self.sessions.reset(agent_session_key)
    if _seq_mcp_notes:
        result_text = "\n".join(_seq_mcp_notes) + "\n\n" + result_text
    if _seq_downgraded:
        result_text = _annotate_model_downgrade(job, result_text)
    result_text = _annotate_partial_block(result_text, _gate)
    job.set_run_result(result_text)
    # This path owns the same verdict as the single-agent one, so a
    # multi-agent job's failure counter moves in both directions —
    # which is what keeps auto-pause both reachable and clearable.
    _apply_gate_verdict(job, _gate)
    return result_text


async def _cron_run_single(
    self: GatewayOrchestrator,
    job: CronJob,
    cap: _CronCapture,
    *,
    retry: Callable[[CronJob], Awaitable[str | None]],
) -> str | None:
    """Run a single-agent message job: dispatch, retries, attribution and cleanup.

    The turn's flags stay locals of this one method, so its ``except`` and its
    ``finally`` read exactly what the dispatch set. *retry* is the fire's
    ``_cron_callback``, which the ACP-death and transient arms re-enter whole.
    """
    session_key, msg = cap.session_key, cap.msg
    cron_execution, cron_memory_store = cap.cron_execution, cap.cron_memory_store
    cron_agent, _default_substituted = cap.cron_agent, cap.default_substituted
    # True once ANY prompt has been handed to the provider this
    # invocation. The whole-callback transient retry below is only
    # safe BEFORE dispatch: after it, tools may have run, so a
    # resubmit risks duplicate side effects (in-stream transient
    # errors are stream_and_collect's own retry's job).
    _prompt_dispatched = False
    # Tell the reaper which key to target if this run hangs.
    if self.cron_svc is not None:
        self.cron_svc.register_active_session_key(job.id, session_key)

    _acquired = False
    _model_downgraded = False
    # Set when the gate verdict below already counted this run, so the
    # exception handler does not count it a second time.
    _gate_counted = False
    # The provider THIS run acquired, held for the failure handler's
    # attribution question. Captured here rather than looked up when the
    # failure is handled, because by then the runtime is gone: the
    # ACP-death arm resets the session before it retries, and the retry
    # frame's own finally resets again on unwind -- so a lookup by key
    # answers None, or worse the REPLACEMENT provider, and either one
    # reads as "this job's own fault".
    _run_provider: object | None = None
    # Post-compaction re-injection bookkeeping for the finally: consumed
    # the one-shot flag / turn landed. Stop reason captured through
    # on_complete, as on the sequential path above.
    _needs_reinjection = False
    _turn_landed = False
    _turn_stop: dict[str, str | None] = {"reason": None}
    # Whether the backend reported a completed compaction during the
    # turn's stream, which drops the session-start context.
    _turn_compaction: dict[str, bool] = {"completed": False}

    def _note_complete(ev: Any, _box: dict[str, str | None] = _turn_stop) -> None:
        _box["reason"] = str(getattr(ev, "stop_reason", "") or "")

    def _note_compaction(ev: Any, _box: dict[str, bool] = _turn_compaction) -> None:
        if getattr(ev, "text", "") == "completed":
            _box["completed"] = True

    try:
        assert self.sessions is not None
        assert self.ctx_builder is not None
        # Collapse an alias (channel-bound agent) to its real kiro mode
        # + workspace before dispatch; falls back to the raw value when
        # it is already a real mode or unset.
        _single_kagent, _single_cwd, _single_crew = self._resolve_cron_agent(
            cron_execution, cron_agent or None, template_namespace=_default_substituted
        )
        _dispatch_agent = _single_kagent or cron_agent or None
        # ── Retained-session agent guard (every single-agent job) ──
        # The cron key is the stable f"cron:{job.id}", and the run's
        # `finally` DEFERS the reset while sub-agents are pending or an
        # injection is in flight, so a session can outlive its fire. The
        # claim path then returns that live `session.provider` by key
        # WITHOUT comparing the `agent` kwarg (session_allocation.py's
        # `existing is not None and not recycling` arm), so a default
        # narrowed between fires would build the NEW agent's context and
        # run it on the OLD agent's process -- keeping MCP servers, a
        # workspace and a pinned model the operator has just taken away.
        #
        # Two things let consecutive fires of one job dispatch different
        # names. An agent-less job re-reads `agent.default_agent` on
        # every fire, and the guard is NOT keyed on
        # `_default_substituted`, which fails open in exactly the case
        # this guard exists for: fire N
        # substitutes and defers its reset, the operator then CLEARS
        # `agent.default_agent`, and fire N+1 resolves an empty default,
        # so that flag is False, the guard is skipped, and the fire runs
        # the FLOOR's context on the process still holding the previous
        # default's runtime. Every other reason for declining the
        # substitution below reaches the same hole. A job pinning its
        # own agent_id reaches it too: `cron update` re-points the
        # captured template (`recapture_cron_template`), so two fires of
        # one pinned job can dispatch different names. The guard
        # therefore runs for every single-agent dispatch; a job whose
        # agent did not change compares equal and fires as before. A
        # dispatching sequence is handled separately by the
        # pre-loop sweep above, because each stable per-agent key must be
        # compared with that step's freshly resolved dispatch agent.
        #
        # DEFER, never reset: a retained session implies pending
        # sub-agent work -- the no-pending case already reset in the
        # prior fire's `finally` -- so resetting to get a correct agent
        # would destroy work in flight, which is strictly worse than
        # skipping one fire. The retention is temporary by construction
        # (`_subagent_done` resets after the last one lands) and the
        # reaper still targets this key if that reset hangs, so the
        # deferral cannot become permanent. `_defer_cron_before_dispatch`
        # is the established spelling: the run counts as neither success
        # nor failure, so a skipped fire cannot walk a job toward
        # auto-pause, and `run_never_started` keeps a delete_after_run
        # job from being consumed by a fire that ran no line.
        #
        # The active-session-key registration above is deliberately
        # LEFT in place: a session really is still live under this key,
        # which is the same reason the deferred-reset arm leaves it.
        #
        # POSITIVE EVIDENCE ONLY. A non-empty `str` from the reader that
        # differs from the name about to be dispatched is a mismatch; a
        # manager without the reader, an unreadable answer, a non-`str`,
        # or "" (no session under the key) all leave this dispatch
        # exactly as it was. Fail-closed here means refusing the unsafe
        # REUSE, not treating an unanswerable question as a mismatch --
        # deferring on a silent reader would stall every agent-less cron
        # on such a host forever, which is a much larger failure than
        # the widening being closed. `session.agent` is stored as the
        # `agent` kwarg verbatim (`agent=agent or ""`), so the two sides
        # of this comparison are the same spelling by construction.
        _live_agent = self._retained_session_agent_mismatch(job, session_key, _dispatch_agent)
        if _live_agent is not None:
            logger.info(
                "Cron '%s': session retained under agent %r but this fire "
                "dispatches %r; deferring rather than reusing its runtime",
                job.name,
                _live_agent,
                _dispatch_agent or "",
            )
            _defer_cron_before_dispatch(
                job,
                f"session retained under agent {_live_agent!r} with work "
                f"pending; this fire dispatches "
                f"{_dispatch_agent or ''!r} and will not reuse it",
            )
            return None
        # After an agent change the session record a previous run left
        # under the stable key still names the template captured before,
        # and the plain bind in `_acquire_with_model_fallback` refuses
        # it. Bring the job's own record to the current capture first,
        # template only; any other difference is left for that bind to
        # refuse. Done here, at the fire, so a record published late (a
        # run still in flight during the update) or one written while the
        # job was stateless is caught on the next fire, and a failed
        # write is retried by it.
        if job.execution_context is not None:
            from kiro_crew.cron_service.identity import rebind_cron_session_template

            await asyncio.to_thread(
                rebind_cron_session_template, session_key, cron_execution, job.name
            )
        client, is_new, _resumed, _model_downgraded = await self._acquire_with_model_fallback(
            job, cron_execution, session_key, _dispatch_agent, _single_cwd, _single_crew
        )
        _acquired = True
        _run_provider = client
        # Same identity publish as the sequential site above — the
        # single-agent cron turn must publish its pidfile mapping or
        # spawn_run's parent resolution has no source to walk to.
        await publish_turn_identity(self.sessions, session_key)
        if is_new:
            # Unattended: hold the first prompt (bounded) for the agent spec's
            # MCP servers, which nobody can reconnect by hand in this run.
            await wait_for_declared_mcp(client, UNATTENDED_MCP_WAIT_SECS)
        if job.acked_items:
            msg += (
                "\n\n[User has seen and acknowledged ALL of the following — "
                "do NOT repeat the same content]\n" + "\n".join(f"- {a}" for a in job.acked_items)
            )
        _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
        # A compaction drops session-start context. Read-and-clear the
        # one-shot flag so this turn re-injects it exactly once; the
        # finally re-arms it if the turn never lands.
        _needs_reinjection = consume_reinjection(self.sessions, session_key)
        # Off-loop: build_message embeds the episodic query.
        full_message, _ = await run_in_embed_pool(
            self.ctx_builder.build_message,
            msg,
            is_new,
            session_key,
            interactive=False,
            agent=cron_agent or None,
            memory_store=cron_memory_store or None,
            execution_context=cron_execution,
            context_provider=client,
            resumed=_resumed,
            needs_reinjection=_needs_reinjection,
            provider_type=_provider,
            minimal_context=job.minimal_context,
        )

        # Wall clock for the cron agent turn — see the sequential site
        # above. acp reports no duration, so this is the row's fallback.
        _turn_t0 = time.monotonic()
        _gate = _GateTally()
        _prompt_dispatched = True
        result_text, _carried_credits = await _cron_stream_with_posttoken_resume(
            client,
            full_message,
            job_name=job.name,
            approval_policy=(
                ToolApprovalPolicy.AUTO_APPROVE
                if job.approval_mode == "auto"
                else ToolApprovalPolicy.HOOK_BASED
            ),
            hooks=self.ctx_builder.hooks,
            on_tool_approval=(
                None if job.approval_mode == "auto" else self._interactive_approval("cron")
            ),
            on_tool_gate=_gate.note,
            on_complete=_note_complete,
            on_compaction=_note_compaction,
            fallback_models=configured_fallback_chain(),
        )

        # The prompt reached the model and the turn completed, so the
        # finally must NOT restore the re-injection flag -- only for a
        # succeeded stop reason.
        _turn_landed = stop_reason_landed(_turn_stop["reason"])

        # Recorded, not inferred from the text: the dedup hash below
        # swaps the placeholder for its count-free twin only when the
        # placeholder was substituted here, never over model prose.
        _empty_reply = not result_text
        if _empty_reply:
            result_text = _gate.empty_reply_placeholder()

        if _model_downgraded:
            result_text = _annotate_model_downgrade(job, result_text)
        result_text = _annotate_model_fallback(result_text, client)
        _missing = declared_mcp_not_ready_note(client)
        if _missing:
            result_text = f"⚠ {_missing}.\n\n{result_text}"
        # Before set_run_result and the dedup hash below, so the stored
        # result, the slot, the bell and every transport carry the
        # refusal, and a run that lost a call never hashes equal to
        # one that did not.
        result_text = _annotate_partial_block(result_text, _gate)

        job.set_run_result(result_text)

        # Context-meter reading for the dashboard slot, captured NOW:
        # the finally block below resets this session, so the open
        # path can never read the provider live. Routed through
        # broadcast_context_usage by inject_cron_result_to_dashboard.
        _ctx_reading = context_meter_reading(client)

        # ── Per-turn usage row: attribute background spend. ──
        # Best-effort; must never fail the cron turn.
        # The same usage feeds the result row's footer (meta.turn_stats).
        _turn_stats: dict[str, Any] | None = None
        try:

            _used, _window = read_context_tokens(client)
            _turn_usage = provider_last_turn_usage(client)
            if _carried_credits:
                # See the sequential site above: bill the interrupted
                # prompt's snapshotted credits alongside the
                # continuation's on a resumed turn.
                _turn_usage.credits += _carried_credits
            _turn_stats = turn_stats_meta(
                int(_turn_usage.duration_ms or (time.monotonic() - _turn_t0) * 1000),
                float(_turn_usage.credits or 0.0),
                float(_turn_usage.cost_usd or 0.0),
                read_turn_model(client),
            )
            await persist_token_record_async(
                session_key,
                # Blank on a downgrade or an active fallback — see the
                # sequential site above / provider_fallback_active. A
                # half-applied pair pin bills the bare model that ran.
                (
                    ""
                    if (_model_downgraded or provider_fallback_active(client))
                    else ((job.model and provider_model_pin_partial(client)) or job.model or "")
                ),
                _turn_usage,
                provider=_provider,
                surface="cron",
                agent=read_effective_agent(client) or cron_agent or "",
                context_used=_used,
                context_window=_window,
                elapsed_ms=int((time.monotonic() - _turn_t0) * 1000),
                model_source=client,
            )
        except Exception:
            logger.debug("usage row (cron) persist failed", exc_info=True)

        # ── Error deduplication ──
        # Suppress repeated identical results to avoid spam. This is
        # delivery-agnostic in both directions: the anchor it reads is
        # advanced by _record_cron_delivery on EVERY confirmed surface,
        # so the early return below suppresses the channel post too,
        # not only Slack's. Deliberate for a channel-delivered cron:
        # identical output is equally noisy in a Telegram chat, and the
        # 24h reminder plus the "same result N times in a row" caption
        # are what keep a persistently-identical job from going
        # unnoticed. An empty reply is hashed over its count-free
        # placeholder: the approved-call count the user sees is per-run,
        # and a key that moves with it never suppresses.
        rh = _result_hash(_dedup_text(result_text, _gate, empty_reply=_empty_reply))

        _gate_counted = _apply_gate_verdict(job, _gate)

        # Session cleanup happens in finally block
        return await self._deliver_cron_agent_result(
            job, session_key, client, result_text, rh, _ctx_reading, _turn_stats
        )
    except Exception as exc:
        if isinstance(exc, SessionClosingError) and not _prompt_dispatched:
            logger.info(
                "Cron '%s': gateway admission closed before dispatch, deferring",
                job.name,
            )
            _defer_cron_before_dispatch(job, "gateway admission is closed")
            return None
        # Attempt one retry for ACP process death before any dedup / alert.
        exc_msg = str(exc).lower()
        # Set once the ACP retry below enters its attempt. If that
        # attempt fails it may have dispatched, so the transient ladder
        # must not run again. A failed reset leaves it unset.
        _acp_retry_attempted = False
        if (
            # Match the death by type: the pipe-broken raise sites in
            # acp/client.py word it "pipe broken", which no substring
            # below covers. The typed arm is held to before dispatch,
            # where no tool can have run yet. Once the retry attempt
            # has started, this frame skips its transient ladder, so
            # no prompt is sent again after that attempt.
            # The substrings keep their old reach.
            (
                (isinstance(exc, AcpProcessDied) and not _prompt_dispatched)
                or (
                    isinstance(exc, AcpError)
                    and ("not running" in exc_msg or "process exited" in exc_msg)
                )
                # A live backend that lost the session under the mapped
                # id: the reset keeps the session-map entry, so the
                # retry's claim re-loads the SAME id on a fresh runtime.
                # Not after a tool ran: the retry re-sends the prompt.
                or (acp_error_is_session_not_found(exc) and not acp_error_after_tool_activity(exc))
            )
            and not getattr(job, "_acp_retried", False)
            and self.sessions is not None
        ):
            logger.warning(
                "Cron '%s': %s, resetting session and retrying",
                job.name,
                (
                    "backend lost the session"
                    if acp_error_is_session_not_found(exc)
                    else "ACP process died"
                ),
            )
            job._acp_retried = True  # type: ignore[attr-defined]
            try:
                if _acquired:
                    self.sessions.release(session_key)
                    _acquired = False
                await self.sessions.reset(session_key)
                # The reset destroyed the live session that carried the
                # consumed re-injection flag, and the retry starts the
                # replacement cold (session-start context comes with a
                # fresh session). Hand the bookkeeping to the retry, as
                # the transient arm does, or the finally below would
                # re-arm the REPLACEMENT after the retry lands and keeps
                # it (deferred reset for pending sub-agents) -- one
                # duplicated re-injection on its next turn. A compaction
                # the destroyed session reported goes with it.
                _needs_reinjection = False
                _turn_compaction["completed"] = False
                _acp_retry_attempted = True
                return await retry(job)
            except Exception:
                pass  # retry failed — fall through to dedup + alert
            finally:
                job._acp_retried = False  # type: ignore[attr-defined]
        # ── Transient backend errors: retry the whole callback with ──
        # backoff instead of counting a failure. stream_and_collect's
        # in-stream retry only covers errors raised INSIDE the prompt
        # stream; a throttle/5xx during session acquire, client
        # creation, or context assembly propagates here, and without
        # this branch would go straight to record_failure(), marching
        # consecutive_failures toward auto-pause (threshold 5) on pure
        # infrastructure weather -- five throttled wakes silently
        # auto-pausing a healthy perpetual agent. The subagent path
        # retries these 3x with backoff; the cron path carries the same
        # semantics.
        #
        # Guarded by the same recursion marker pattern as the ACP
        # retry: the attempt counter lives on the job for the duration
        # of the outermost invocation only, and the recursive call
        # re-enters the full callback so a retry that succeeds runs
        # the complete delivery path.
        if acp_error_is_transient(exc) and not _prompt_dispatched and not _acp_retry_attempted:
            _t_attempt = getattr(job, "_transient_attempts", 0)
            if _t_attempt < _CRON_TRANSIENT_RETRIES:
                job._transient_attempts = _t_attempt + 1  # type: ignore[attr-defined]
                _delay = transient_retry_delay(_t_attempt + 1)
                logger.warning(
                    "Cron '%s': transient backend error (attempt %d/%d), " "retrying in %.1fs: %s",
                    job.name,
                    _t_attempt + 1,
                    _CRON_TRANSIENT_RETRIES,
                    _delay,
                    exc,
                )
                # The callback only INCREMENTS `_transient_attempts`
                # (above). Reading it into the persisted telemetry and
                # clearing it for the next run both belong to
                # `CronService._execute`, which owns every other
                # per-run stamp (`last_run_ts` first among them): a
                # write from inside this frame would land BEFORE that
                # method stamps this run's `last_run_ts`, binding the
                # count to the previous run. Its read-and-clear is in a
                # `finally`, so a wake-budget cancellation landing in
                # this sleep cannot strand the just-consumed attempt on
                # the in-memory job either.
                try:
                    if _acquired and self.sessions is not None:
                        self.sessions.release(session_key)
                        _acquired = False
                except Exception:
                    logger.debug("release before transient retry failed", exc_info=True)
                # The retry re-enters this callback on the SAME live
                # session (no reset on this arm), so give it back the
                # re-injection this attempt consumed and never delivered
                # -- and hand the bookkeeping to the retry, or the
                # finally below would re-arm a second time after it lands.
                rearm_reinjection(
                    self.sessions,
                    session_key,
                    consumed=_needs_reinjection,
                    landed=_turn_landed,
                )
                # Same reason, for the build-time skill-body dedup: this
                # attempt recorded its injected bodies as sent, but a
                # transient error discarded the prompt before it landed
                # (_turn_landed is False on this arm). Without the
                # rollback the retry's build sees them already-sent and
                # demotes to a pointer, so the body never reaches the
                # window for a run that otherwise succeeds -- the skill
                # is lost for that run. Roll back so the retry re-injects
                # full bodies, and hand the settle to the retry (the
                # finally below would otherwise settle a second time).
                rollback_skill_bodies(self.ctx_builder, session_key, landed=_turn_landed)
                _needs_reinjection = False
                await asyncio.sleep(_delay)
                return await retry(job)
            # Retries exhausted — fall through to dedup + alert +
            # record_failure: a persistent outage should still count.
        logger.exception("Cron job '%s' failed", job.name)
        # During an in-flight ACP retry (inner recursive _cron_callback
        # call), suppress all notify/slack/dedup work — the outer
        # invocation is authoritative and will handle notification
        # for the retry's final failure. Without this guard, the
        # inner call emits its own dashboard notify + Slack alert
        # and advances dedup state, duplicating the outer handler.
        if getattr(job, "_acp_retried", False):
            raise
        _job_owns_failure, _hand_over_streak = _cron_failure_attribution(job, _run_provider)
        await self._cron_single_failure(
            job, exc, session_key, _gate_counted, _job_owns_failure, _hand_over_streak
        )
        raise
    finally:
        assert self.sessions is not None
        # Before the reset below: a turn that consumed the post-compaction
        # flag but never landed, or whose backend compacted the session,
        # sets the flag so a session that survives (deferred reset)
        # re-injects on its next turn.
        rearm_reinjection(
            self.sessions,
            session_key,
            consumed=_needs_reinjection,
            landed=_turn_landed,
            compacted=_turn_compaction["completed"],
        )
        rollback_skill_bodies(self.ctx_builder, session_key, landed=_turn_landed)
        if _acquired:
            self.sessions.release(session_key)
            # Defer session reset if subagents are still running,
            # queued behind the concurrency/stagger gate, or
            # mid-injection — _subagent_done will reset after the last one.
            has_pending = bool(
                self.subagent_mgr and await _subagent_work_pending(self.subagent_mgr, session_key)
            )
            has_injecting = self._cron_injecting.get(session_key, 0) > 0
            if has_pending or has_injecting:
                logger.info("Cron '%s': deferring reset, subagents pending", job.name)
                # leave the active-session registration in place so
                # the reaper can still target the ephemeral key if the deferred
                # reset hangs. _subagent_done will clear it after the real reset.
            else:
                await self.sessions.reset(session_key)
                # reset done → the reaper does not need this key.
                if self.cron_svc is not None:
                    self.cron_svc.clear_active_session_key(job.id, session_key)
