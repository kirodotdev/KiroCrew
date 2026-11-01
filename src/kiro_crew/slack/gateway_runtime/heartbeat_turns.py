"""Build and start the heartbeat service and run each heartbeat turn.

Includes the per-turn usage row a background dispatch surface records.

Orchestrator fields read: ``_cfg``, ``consolidator``, ``ctx_builder``, ``sessions``.
Orchestrator fields written: ``heartbeat_svc``.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import time

    from kiro_crew.slack.gateway import (
        HEARTBEAT_KEY,
        HEARTBEAT_TASK_TIMEOUT_SECS,
        AgentTurnUsage,
        Any,
        Callable,
        GatewayOrchestrator,
        HeartbeatService,
        MemoryStore,
        ToolApprovalPolicy,
        _annotate_model_fallback,
        _build_heartbeat_hooks,
        configured_fallback_chain,
        consume_reinjection,
        is_keep_response,
        logger,
        persist_token_record_async,
        provider_last_turn_usage,
        read_context_tokens,
        read_effective_agent,
        rearm_reinjection,
        redact_and_truncate,
        redact_credentials,
        redact_exfiltration_urls,
        redact_log_via_context,
        rollback_skill_bodies,
        run_in_embed_pool,
        session_store_for_turn,
        stop_reason_landed,
        stream_and_collect,
        strip_keep_sentinel,
    )


async def _persist_turn_row(
    client: Any,
    session_key: str,
    *,
    provider: str,
    surface: str,
    agent_fallback: Callable[[], str],
    t0: float,
    trigger: str = "other",
    usage: AgentTurnUsage | None = None,
) -> None:
    """Persist one per-turn usage row for a background dispatch surface.

    ``trigger`` is who started the turn (``metrics.usage_attrs``):
    ``heartbeat`` for a heartbeat task, ``monitor`` for a monitor wake. A
    caller that passes nothing records ``other``, never ``user``.

    The heartbeat and monitor surfaces — each with a success and a timeout twin —
    share this one implementation rather than each carrying its own copy of the
    block. Best-effort: a persistence failure is logged at debug and never
    propagates into the background loop, since a dropped analytics row must not
    abort a live turn.

    ``agent_fallback`` is a zero-arg callable, invoked INSIDE the try/except and
    only when ``read_effective_agent`` yields nothing — the short-circuit
    (``read_effective_agent(client) or _get_agent_for_session(key)``) keeps a
    cold-cache ``KiroCrewConfig.load()`` from running on every turn and from
    escaping the best-effort guard.

    NOTE: ``test_turn_duration_recorded.py`` counts ``persist_token_record_async``
    call sites per file and requires every one to pass ``elapsed_ms``. This
    helper is the single heartbeat/monitor call site; the two cron sites persist
    directly (they carry a ``model`` argument). Adding a new surface that
    bypasses this helper changes the count and fails that guard by design.
    """
    try:
        _used, _window = read_context_tokens(client)
        await persist_token_record_async(
            session_key,
            "",
            provider_last_turn_usage(client) if usage is None else usage,
            provider=provider,
            surface=surface,
            agent=read_effective_agent(client) or agent_fallback(),
            context_used=_used,
            context_window=_window,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            model_source=client,
            trigger=trigger,
        )
    except Exception:
        logger.debug("usage row (%s) persist failed", surface, exc_info=True)


async def _init_heartbeat(self: GatewayOrchestrator) -> None:
    """Initialize and start the heartbeat service."""
    startup = getattr(self, "_memory_startup", None)
    if startup is not None and (startup.stopped or not startup.ready):
        raise RuntimeError("Heartbeat service cannot start before memory preparation completes")
    memory = self.ctx_builder.memory if self.ctx_builder else MemoryStore()

    # Heartbeat-scoped hooks: drops the user's ``auto_approve_tools`` so
    # the heartbeat allowlist (``_heartbeat_approval``) is the sole
    # approval authority for any tool call in a heartbeat session.
    # REBUILT per run (below) from the
    # live primary manager: denied-command opt-out state is mutable at
    # runtime (Settings > Security hot-reloads ``ctx_builder.hooks``), so a
    # once-at-init snapshot would let a heartbeat session keep enforcing a
    # just-disabled rule — or skip a just-added one — until restart.
    assert self.ctx_builder is not None

    async def _heartbeat_task(task_text: str, deliver: str) -> str | None:
        assert self.sessions is not None
        assert self.ctx_builder is not None
        session_key = HEARTBEAT_KEY
        # Re-derive the heartbeat-scoped hooks from the CURRENT primary
        # manager each cycle so live denied-command changes take effect
        # without a gateway restart (cross-surface consistency).
        heartbeat_hooks = _build_heartbeat_hooks(self.ctx_builder.hooks)
        _acquired = False
        # Whether this task's prompt landed, read from its completion's stop
        # reason, so the finally can settle the skill-body record its build
        # wrote: a prompt that never landed must not leave the next task of
        # this cycle a pointer to a body the session never received.
        _turn_landed = False
        _turn_stop: dict[str, str | None] = {"reason": None}
        # Whether the backend compacted the session during this task's turn,
        # which drops the skill bodies and session-start context earlier
        # tasks of the cycle sent.
        _turn_compaction: dict[str, bool] = {"completed": False}
        # The one-shot post-compaction flag this task consumed, so the
        # finally can put it back if this task's prompt never landed.
        _needs_reinjection = False

        def _note_complete(ev: Any, _box: dict[str, str | None] = _turn_stop) -> None:
            _box["reason"] = str(getattr(ev, "stop_reason", "") or "")

        def _note_compaction(ev: Any, _box: dict[str, bool] = _turn_compaction) -> None:
            if getattr(ev, "text", "") == "completed":
                _box["completed"] = True

        try:
            # Heartbeat tasks are the operator's own queue, so nothing binds
            # the heartbeat key to a crew and the shared session resolver
            # answers the default memory store.
            _memory_store = await session_store_for_turn(self.ctx_builder, session_key)
            # Use the dedicated ``kirocrew-heartbeat`` agent — minimal
            # MCP surface (kirocrew-core only on public installs) so cycle
            # cold-starts stay cheap.  Tool calls are still gated at
            # runtime by ``_heartbeat_approval``'s read-only allowlist.
            client, is_new, _resumed = await self.sessions.get_or_create(
                session_key,
                agent="kirocrew-heartbeat",
            )
            _acquired = True

            # Prepend an unmissable HEARTBEAT_KEEP reminder to every task
            # text before message build.  This survives context
            # compaction and webhook-restored sessions where skill /
            # system-prompt copies of the same instruction can drift out
            # of effective context.
            injected = _HEARTBEAT_KEEP_INJECTION + task_text
            # A compaction during an earlier task of this cycle armed the
            # flag: consuming it makes this prompt send the dropped context
            # again and restart the skill-body record.
            _needs_reinjection = consume_reinjection(self.sessions, session_key)
            # Off-loop: build_message embeds the episodic query. Every task
            # of a cycle runs on this one session, so the record of skill
            # bodies it already holds is kept under the heartbeat key: a
            # skill two tasks match reaches the session once. The prompt is
            # still built without a session key.
            full_message, _ = await run_in_embed_pool(
                self.ctx_builder.build_message,
                injected,
                is_new,
                memory_store=_memory_store,
                needs_reinjection=_needs_reinjection,
                skill_bodies_session=session_key,
            )

            # A heartbeat turn runs unattended. Bound it with a hard deadline
            # (mirrors cron's _execute_with_timeout) as defense in depth so
            # any unexpected hang in stream_and_collect cannot freeze the
            # whole heartbeat subsystem. ``_heartbeat_approval`` already
            # rejects non-allowlisted tools immediately (no human-approval
            # wait), so the timeout is the second line of defense.
            #
            # ``hooks=heartbeat_hooks`` (NOT the interactive user hooks):
            # the user's ``auto_approve_tools`` MUST NOT widen the heartbeat
            # allowlist — ``llm_helpers._resolve_permission`` consults
            # ``hooks.on_tool_call()`` BEFORE ``on_tool_approval``.
            #
            # Clock started outside wait_for so BOTH the success path and the
            # TimeoutError branch below can report the real elapsed time.
            _turn_t0 = time.monotonic()
            result_text = await asyncio.wait_for(
                stream_and_collect(
                    client,
                    full_message,
                    approval_policy=ToolApprovalPolicy.HOOK_BASED,
                    hooks=heartbeat_hooks,
                    on_tool_approval=self._heartbeat_approval,
                    on_complete=_note_complete,
                    on_compaction=_note_compaction,
                    fallback_models=configured_fallback_chain(),
                ),
                timeout=HEARTBEAT_TASK_TIMEOUT_SECS,
            )
            _turn_landed = stop_reason_landed(_turn_stop["reason"])

            if not result_text:
                result_text = "_No response._"
            result_text = _annotate_model_fallback(result_text, client)

            # ── Per-turn usage row: attribute heartbeat spend. ──
            await _persist_turn_row(
                client,
                session_key,
                provider=(self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"),
                surface="heartbeat",
                agent_fallback=lambda: "kirocrew-heartbeat",
                t0=_turn_t0,
                trigger="heartbeat",
            )
        except asyncio.TimeoutError:
            # Tear down the in-flight turn so the underlying claude-agent-acp
            # process/turn doesn't linger holding the heartbeat session.
            # Per-task reset is safe here because asyncio.wait_for has
            # already cancelled the in-flight stream_and_collect, so any
            # concurrent heartbeat task using the same key was already
            # blocked on the per-key semaphore (held until our finally
            # releases) — they pick up the freshly-recreated session.
            logger.warning(
                "Heartbeat task timed out after %ds, resetting session: %s",
                HEARTBEAT_TASK_TIMEOUT_SECS,
                task_text[:80],
            )
            # ── Timeout spend is REAL spend. ──
            # A cancelled turn has already cost whatever it cost, so a
            # timed-out heartbeat writes a row rather than dropping that
            # silently. Record it here, BEFORE the session reset below tears
            # the client down and takes its last-turn usage with it.
            #
            # No new schema field: the record has never carried a
            # success/failure outcome for ANY surface, so a timeout row is
            # no less honest than any other row. The duration recorded is
            # the real elapsed time, which for a timeout is ~the ceiling.
            await _persist_turn_row(
                client,
                session_key,
                provider=(self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"),
                surface="heartbeat",
                agent_fallback=lambda: "kirocrew-heartbeat",
                t0=_turn_t0,
                trigger="heartbeat",
            )
            try:
                await self.sessions.reset(session_key)
            except Exception:
                logger.warning("Heartbeat: session reset after timeout failed", exc_info=True)
            # Produce a graceful incomplete result rather than crashing the loop.
            result_text = (
                f"_Heartbeat task timed out after {HEARTBEAT_TASK_TIMEOUT_SECS}s "
                "and was cancelled._"
            )
        except Exception:
            logger.exception("Heartbeat task failed: %s", task_text[:80])
            raise
        finally:
            if _acquired:
                # Arm the flag and settle this task's skill-body record
                # BEFORE the release: the next task of the cycle builds on
                # the same session, and a settle after it would act on that
                # task's build instead.
                rearm_reinjection(
                    self.sessions,
                    session_key,
                    consumed=_needs_reinjection,
                    landed=_turn_landed,
                    compacted=_turn_compaction["completed"],
                )
                rollback_skill_bodies(self.ctx_builder, session_key, landed=_turn_landed)
                # Release the per-session semaphore so the next task in
                # this cycle (asyncio.gather'd) can acquire the SAME
                # warm session.  Cycle-end teardown is handled by
                # ``_recycle_heartbeat`` (called once after gather
                # completes) — see ``HeartbeatService._process_heartbeat_file``.
                self.sessions.release(session_key)

        result_safe, _ = redact_exfiltration_urls(result_text)
        result_safe, _ = redact_credentials(result_safe)
        display_text = strip_keep_sentinel(result_safe)
        # Only notify when task is complete — suppress delivery for
        # incomplete tasks (HEARTBEAT_KEEP) to avoid spamming every cycle.
        if is_keep_response(result_safe):
            # Gate-side LOG line, so it takes the context spelling: a host with
            # a companion loaded must not have this text scanned with the
            # weaker OSS pass. Truncation comes AFTER redaction — the
            # invariant ``redact_log_via_context`` hands to its callers by
            # contract: slicing first would leave a credential as an
            # unmatchable fragment. The sibling below stays on
            # ``redact_and_truncate`` because it feeds a DELIVERY, not a log,
            # and the two want different failure modes on a non-composable
            # host.
            logger.info(
                "Heartbeat task incomplete, suppressing delivery: %s",
                redact_log_via_context(task_text)[:80],
            )
        else:
            task_safe = redact_and_truncate(task_text, 100)
            await self._deliver_result(
                "💓 Heartbeat",
                task_safe,
                display_text,
                deliver,
            )
        return result_safe

    async def _on_cycle_end() -> None:
        """Recycle the heartbeat session ONCE per cycle, not per task.

        Multi-task heartbeat cycles run concurrently via
        ``asyncio.gather`` and share ``HEARTBEAT_KEY``.  A per-task
        ``reset()`` would tear down the session under sibling tasks
        still in flight (per code review).
        ``recycle_heartbeat`` is unconditional: heartbeat promises
        "fresh context each cycle", and each entry is re-read from
        HEARTBEAT.md every cycle, so carrying a transcript forward only
        costs input tokens. Nobody waits on a heartbeat tick, so the
        per-cycle MCP cold-start is unobserved.
        """
        assert self.sessions is not None
        try:
            await self.sessions.recycle_heartbeat()
        except Exception:
            logger.warning("Heartbeat: cycle-end recycle failed", exc_info=True)

    async def _admitted_heartbeat_task(task_text: str, deliver: str) -> str | None:
        # Shared Crew fair admission: a heartbeat is Crew automation. A refused
        # task is skipped this beat (None, like a task that produced nothing).
        from kiro_crew.shared_crew.admission import OperationKind
        from kiro_crew.shared_crew.turn_gate import run_admitted

        return await run_admitted(
            _heartbeat_task(task_text, deliver),
            session_key=HEARTBEAT_KEY,
            text=task_text,
            automation=True,
            operation=OperationKind.BACKGROUND,
            refused=lambda reason: logger.info(
                "Heartbeat task skipped by fair admission (%s)", getattr(reason, "value", reason)
            ),
        )

    self.heartbeat_svc = HeartbeatService(
        memory=memory,
        on_task=_admitted_heartbeat_task,
        consolidator=self.consolidator,
        on_cycle_end=_on_cycle_end,
    )
    await self.heartbeat_svc.start()


# Prepended to every heartbeat task_text before ``ctx_builder.build_message``.
# Inline injection survives context compaction and webhook-restored sessions
# where skill / system-prompt copies of the same instruction can drift out of
# effective context.
_HEARTBEAT_KEEP_INJECTION = (
    "[HEARTBEAT TASK — you MUST include the keyword HEARTBEAT_KEEP "
    "in your response if this task is NOT complete. Omit the "
    "keyword only when the task is fully complete.]\n\n"
)
