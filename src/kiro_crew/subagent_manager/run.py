"""Run behavior for the SubagentManager facade."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..subagent_persistence import (
    publish_live_cleanup_identity,
    remember_live_cleanup_identity,
)
from ._component import ManagerComponent

if TYPE_CHECKING:
    from ..subagent import (
        _AGENT_NAME_RE,
        _CANCEL_RESUME_PREFIX,
        _MAX_ERROR_DETAIL_LEN,
        _ON_DONE_TIMEOUT,
        _RESET_TIMEOUT,
        _STATE_DRAIN_TIMEOUT,
        _SYSTEM_PREFIX,
        _TRANSIENT_CONTINUE_MSG,
        _TURN_LIMIT,
        EVENT_COMPLETE,
        EVENT_PERMISSION_REQUEST,
        EVENT_TEXT_CHUNK,
        EVENT_TOOL_CALL,
        EVENT_TOOL_RESULT,
        FALLBACK_CANDIDATE_ATTEMPTS,
        FALLBACK_STORY_ATTR,
        HOOK_EVENT_POST_TOOL_USE,
        PROVIDER_LABEL_DEFAULT,
        TOOL_AUTO_APPROVE,
        TOOL_DENY,
        TRANSIENT_RETRIES,
        AcpRuntime,
        AcpSessionProvider,
        Any,
        FallbackState,
        KiroCrewConfig,
        LLMEvent,
        LLMProvider,
        Path,
        Stats,
        SubagentInfo,
        _context_groups_of,
        _describe_exception,
        _redact,
        _resolved_model_of,
        _subagent_default_effort,
        _subagent_default_model,
        _timeout_context,
        acp_error_is_transient,
        advance_fallback_candidate,
        agent_dir_for_display,
        annotate_model_fallback,
        append_fallback_story,
        apply_completion_keep,
        cap_result_file,
        configured_fallback_chain,
        evict_completed_agents,
        extract_options,
        fire_tool_hooks,
        logger,
        name_grant,
        provider_fallback_active,
        run_in_embed_pool,
        sel,
        time,
        transient_retry_delay,
        update_state,
        window_for_provider_client,
        write_result_chunk,
    )


class RunEventCoordinator(ManagerComponent):
    """Own run transitions while state remains facade-owned."""

    _publish_identity = staticmethod(publish_live_cleanup_identity)
    _remember_identity = staticmethod(remember_live_cleanup_identity)
    __slots__ = ()

    def _effective_turn_limit_impl(self, info: SubagentInfo) -> int:
        """Resolved turn cap for a run: per-spawn ``max_turns`` → config
        default (``agent.subagent_max_turns``) → hardcoded ``_TURN_LIMIT``.

        ``0`` at any level means "not set" and falls through to the next.
        """
        return info.max_turns or self._manager._default_turn_limit or _TURN_LIMIT

    async def _write_state_off_loop_impl(
        self, info: SubagentInfo, what: str, **fields: object
    ) -> bool:
        """Merge *fields* into this run's ``state.json``, off the event loop.

        Every ``state.json`` writer inside a run goes through here, for two
        reasons that are both load-bearing.

        OFF-LOOP, because ``update_state`` ends in a synchronous fsync and a
        slow FS must not freeze the gateway/heartbeat (#6288). Running in a pool
        thread also means the write TAKES ``update_state``'s per-agent lock,
        which off-loop callers hold and on-loop callers deliberately skip
        (#7280).

        DRAINED ON CANCELLATION, because cancelling a ``to_thread`` await
        detaches the worker without stopping it, and ``update_state`` rewrites
        the WHOLE file from the snapshot it already read — so a detached worker
        rolls back every field written after that read, not merely the fields it
        names. That is how a zombie erases the ``pid`` / ``session_id`` a
        cancel-respawn recovery run writes on the loop, or the retention
        ``keep`` that promote / release write on the loop (#6306, #6298, #6308).
        Cancellation is therefore held open until the worker finishes — but
        BOUNDED: ``cancel_all()`` gathers run tasks with no timeout, so an
        unbounded drain on a wedged FS would hold gateway shutdown forever, and
        this module's convention is that bounded shutdown plus recoverable state
        beats unbounded shutdown (same posture as ``_REPORT_DRAIN_TIMEOUT``).
        ``asyncio.wait`` never cancels its members, so repeated cancels of the
        awaiting task keep the worker future intact while the drain loop waits
        out the same deadline. For the whole window in which the worker may still
        write -- from the cancellation until the worker settles, however the drain
        exits -- the run HOLDS its conversation
        (``SubagentManager._abandoned_state_writers``), so the two on-loop
        ``keep`` writes are deferred past the worker instead of being rolled back
        by it. The window starts at the cancellation rather than at the drain
        deadline because on Python 3.10 a second outer cancel can finalize the run
        mid-drain.

        Returns ``update_state``'s own report: True when the merge was written,
        False when it was SKIPPED because the state was unreadable — a caller
        with a durability contract (the pre-spawn provenance write, #5394)
        retries on False. Re-raises ``asyncio.CancelledError`` after draining,
        so such a retry loop ends on cancellation instead of adding a second
        writer for the same fields.
        """
        writer = asyncio.ensure_future(asyncio.to_thread(update_state, info.id, **fields))
        try:
            return bool(await asyncio.shield(writer))
        except asyncio.CancelledError:
            # Hold this run's conversation for the WHOLE window in which the
            # worker may still write, which starts here and not at the drain
            # deadline: on Python 3.10 a second outer cancel can deliver _run's
            # finalization mid-drain, so the run can go `done` while the writer
            # is live, and a continuation reaching a released gate would then
            # write `keep` for that writer's stale whole-file rewrite to erase
            # (#6298). `keep` is written on the loop and takes no per-agent lock,
            # so ordering is the only thing protecting it. The worker's own
            # done-callback releases the hold, so it lasts exactly as long as the
            # worker does -- milliseconds on a healthy FS. Recorded on the
            # MANAGER, not on `info`: `evict_completed_agents` prunes completed
            # runs out of `_agents`, and an eviction must not release the hold.
            self._manager._abandoned_state_writers.add(info.id)

            def _settled(
                fut: "asyncio.Future[Any]",
                _mgr: Any = self._manager,
                _aid: str = info.id,
                _what: str = what,
            ) -> None:
                # The worker has landed (drained or abandoned): the conversation
                # is safe to promote or release again.
                _mgr._abandoned_state_writers.discard(_aid)
                # It may also have raised; retrieve it so it never surfaces as an
                # asynchronous "exception was never retrieved" warning.
                if not fut.cancelled() and fut.exception() is not None:
                    logger.debug(
                        "Best-effort %s write failed for %s during cancel drain",
                        _what,
                        _aid,
                        exc_info=fut.exception(),
                    )

            writer.add_done_callback(_settled)
            # Latch for _run's recovery gate: on Python 3.10, wait_for's
            # _cancel_and_wait awaits a bare future that a SECOND outer cancel
            # can interrupt, delivering _run's CancelledError handler while this
            # drain is still in flight — before expiry suppression lands. The
            # latch lets the gate see the live drain and skip scheduling a
            # recovery writer the worker could race. 3.11+ delivers the outer
            # cancel only after this child task completes, so there the latch is
            # always observed False.
            info._state_drain_active = True
            try:
                deadline = time.monotonic() + _STATE_DRAIN_TIMEOUT
                while not writer.done():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        logger.warning(
                            "%s write for %s did not drain in %.0fs on cancellation — "
                            "abandoning worker (its stale whole-file rewrite may roll "
                            "back a recovery run's state)",
                            what,
                            info.id,
                            _STATE_DRAIN_TIMEOUT,
                        )
                        # The abandoned worker is a live stale writer whose
                        # rewrite is whole-file, so it can roll back the pid /
                        # session_id a cancel-respawn recovery run writes ON the
                        # loop (no per-agent lock there) — and a lost pid means
                        # an orphan the reaper can no longer reach. Consume the
                        # one-shot recovery so this cancellation finalizes
                        # instead of respawning: losing one best-effort
                        # auto-continue on an FS already wedged past the
                        # deadline is strictly cheaper than resurrecting stale
                        # state. Uniform across sites — the blast radius is set
                        # by the whole-file rewrite, not by which fields this
                        # particular caller named. The conversation hold above
                        # outlives this branch and is released by _settled.
                        info._cancel_retry_used = True
                        break
                    try:
                        await asyncio.wait({writer}, timeout=remaining)
                    except asyncio.CancelledError:
                        pass  # repeated cancel: keep draining to the deadline
            finally:
                info._state_drain_active = False
            raise

    async def _remember_identity_off_loop(
        self,
        info: SubagentInfo,
        *,
        session_id: str,
        provider: str,
        cwd: str = "",
        keep: bool | None = None,
        conversation_key: str = "",
    ) -> None:
        """Persist protected cleanup identity off-loop and drain cancellation.

        The live generation is already published synchronously, but restart
        durability depends on this protected write. ``to_thread`` workers survive
        cancellation, so shield the worker and delay re-raising until it settles;
        otherwise terminal teardown can finish and restart before the authority
        record exists.
        """
        writer = asyncio.ensure_future(
            asyncio.to_thread(
                self._remember_identity,
                info.id,
                session_id=session_id,
                provider=provider,
                cwd=cwd,
                keep=keep,
                conversation_key=conversation_key,
            )
        )
        try:
            await asyncio.shield(writer)
        except asyncio.CancelledError:
            info._state_drain_active = True
            try:
                while not writer.done():
                    try:
                        await asyncio.wait({writer})
                    except asyncio.CancelledError:
                        pass
                if not writer.cancelled():
                    try:
                        writer.result()
                    except Exception:
                        pass
            finally:
                info._state_drain_active = False
            raise

    def update_completion_keep_impl(self, mode: str, max_chars: int) -> None:
        """Update the live completion-keep mode and char budget.

        Called from ``api_kirocrew_config_patch`` after the user changes
        ``agent.completion_keep`` or ``agent.completion_keep_chars`` from
        the Settings UI. The values are read once per subagent at
        completion time (``apply_completion_keep`` call site), so swapping
        them here takes effect for the next subagent to finish — including
        ones already running. No torn-read possible under asyncio: both
        reads happen in the same synchronous block.

        ``mode`` is validated by ``_validated_completion_keep`` at config
        load; this setter is intentionally permissive about ``max_chars``
        so the loader / handler stays the validation choke-point.
        """
        self._manager._completion_keep = mode
        self._manager._completion_keep_chars = max_chars

    def running_agents_for_impl(self, parent_key: str) -> list[dict]:
        """Return summary dicts for agents belonging to *parent_key*."""
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        def _r(s: str) -> str:
            s, _ = redact_exfiltration_urls(s)
            s, _ = redact_credentials(s)
            return s

        return [
            {
                "id": a.id,
                "task": _r(a.task[:80]),
                "agent": _r(a.agent),
                "turns": a.turns,
                "last_tool": _r(a.last_tool),
                "tool_count": a.tool_count,
                "stalled": a.stalled,
                "startedAt": a.started,
            }
            for a in self._manager._agents.values()
            if not a.done and a.parent_session_key == parent_key
        ]

    def get_impl(self, agent_id: str) -> SubagentInfo | None:
        """Get agent info by ID."""
        return self._manager._agents.get(agent_id)

    async def _teardown_run_session_impl(self, info: SubagentInfo, session_key: str) -> None:
        """Release and reset the run's own session (skipped when reaped).

        Split out of ``_run``'s ``finally`` so the caller can wrap it in a nested
        ``try/finally``: every statement here AWAITS, and a cancellation arriving
        at one of those awaits propagates straight out of the enclosing
        ``finally`` suite (the ``except Exception`` arms do not catch
        ``CancelledError``). That skipped the slot release, the task-registry pop
        and the teardown gate — leaking a concurrency slot, which is the very
        class of bug this module's guard split exists to prevent.
        """
        try:
            if info._session_sharing:
                # Session-sharing subagents: destroy the session handle
                # (unregister from shared runtime). Don't kill the runtime.
                # Skip when the reaper already tore it down (info.reaped).
                # Retain-by-default: keep the transcript files — they are
                # spawn_continue's resume material. The tombstone pruner
                # deletes them with the run folder (~1h after delivery)
                # unless the conversation is promoted (continued / keep).
                if info._shared_provider and not info.reaped:
                    try:
                        info._shared_provider.set_keep_transcript(True)
                    except Exception:
                        logger.debug("set_keep_transcript failed", exc_info=True)
                    await info._shared_provider.shutdown()
            else:
                # Retain-by-default: never delete session files at teardown.
                # The reset() below still expires the process, so an idle
                # conversation costs a JSON file, not RSS. Deletion is owned
                # by the tombstone pruner (default runs, ~1h) or the
                # conversation TTL sweep / spawn_release (promoted runs).
                self._manager._sessions.release(session_key, cleanup=False)
        except Exception:
            logger.warning("Subagent %s: release failed", info.id, exc_info=True)
        if not info._session_sharing:
            try:
                await asyncio.wait_for(
                    self._manager._sessions.reset(session_key), timeout=_RESET_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning("Subagent %s: reset timed out, force-killing", info.id)
                await self._manager._sigkill_session(session_key)
                try:
                    sel().log_tool_invocation(
                        session_key=session_key,
                        source="subagent",
                        tool_name="run_finally_force_kill",
                        outcome="sigkill",
                        metadata={"subagent_id": info.id},
                    )
                except Exception:
                    logger.exception("Subagent %s: SEL audit failed", info.id)
            except Exception:
                logger.exception("Subagent %s: reset failed", info.id)

    async def _run_impl(self, info: SubagentInfo) -> None:
        """Execute a subagent task in its own session."""
        session_key = info.conversation_key or f"subagent:{info.id}"
        try:
            await asyncio.wait_for(
                self._manager._run_inner(info, session_key), timeout=self._manager._default_timeout
            )
        except asyncio.TimeoutError:
            if not info.reaped:
                info.error = f"Timed out after {self._manager._default_timeout // 60} minutes [{_timeout_context(info, turn_limit=self._manager._effective_turn_limit(info))}]"
                info.done = True
                Stats().inc_subagent_failed()
                self._manager._write_tombstone(info, "timeout")
            logger.warning("Subagent %s timed out", info.id)
        except asyncio.CancelledError:
            if not info.reaped:
                if (
                    not info.user_stopped
                    and not self._manager._shutting_down
                    and not info._cancel_retry_used
                    # A live state-write drain means a worker is still
                    # (or may still be) writing state.json: respawning a
                    # recovery writer now re-opens the stale-overwrite race
                    # (#6306, #6308; reachable on 3.10 via a second outer
                    # cancel interrupting wait_for's _cancel_and_wait).
                    and not info._state_drain_active
                    and info.tool_count == 0
                ):
                    # UNEXPECTED cancellation (not user Stop, not shutdown):
                    # one-shot auto-continue, mirroring the main path's
                    # unexpected-cancel recovery. Skip terminal
                    # finalization (via _recovering) and respawn on a fresh
                    # task — this task is being cancelled and cannot continue.
                    #
                    # SIDE-EFFECT GATE (tool_count == 0): the respawn runs on a
                    # FRESH session with no ledger of prior tool calls, so the
                    # model cannot verify which side effects (files written,
                    # messages sent, commands run) already happened — a
                    # preamble alone cannot make re-running safe. Once any tool
                    # has executed, we finalize with the partial preserved
                    # instead of respawning. Text-only activity is safe to
                    # resume (the partial is preserved and re-presented).
                    info._cancel_retry_used = True
                    info._recovering = True
                    logger.warning(
                        "Subagent %s unexpectedly cancelled — scheduling one-shot auto-continue",
                        info.id,
                    )
                    try:
                        sel().log_tool_invocation(
                            session_key=session_key,
                            source="subagent",
                            tool_name="cancel_auto_continue",
                            outcome="scheduled",
                            metadata={"subagent_id": info.id},
                        )
                    except Exception:
                        logger.debug("SEL audit for cancel recovery failed", exc_info=True)
                    self._manager._schedule_cancel_recovery(info)
                else:
                    info.done = True
                    if (
                        info.tool_count > 0
                        and not info.user_stopped
                        and not self._manager._shutting_down
                    ):
                        # Auto-continue deliberately suppressed (side-effect
                        # gate above): be explicit so the parent/user knows the
                        # run was interrupted and NOT resumed, and why.
                        info.error = (
                            "cancelled (auto-continue suppressed: tools already "
                            "executed — resuming on a fresh session could repeat "
                            "side effects)"
                        )
                    else:
                        info.error = "cancelled"
                    # Preserve whatever streamed before the cancel as a partial
                    # result (delivered with the failure).
                    if not info.result and info.streaming_text:
                        info.result = info.streaming_text
                    Stats().inc_subagent_failed()
                    self._manager._write_tombstone(info, "cancelled")
            logger.info("Subagent %s cancelled", info.id)
        except Exception as exc:
            if not info.reaped:
                # Story appended INSIDE the cap: info.error reaches a WS frame
                # and the Subagents panel, so the rendered total stays bounded
                # by _MAX_ERROR_DETAIL_LEN exactly as before — and the budget
                # trims the ERROR text, never the story, so a verbose chain
                # cannot push the walk out of the terminal error.
                info.error = append_fallback_story(
                    _describe_exception(exc), exc, budget=_MAX_ERROR_DETAIL_LEN
                )
                info.done = True
                Stats().inc_subagent_failed()
                self._manager._write_tombstone(info, "error")
            logger.exception("Subagent %s failed", info.id)
        finally:
            # Guard 3 of 3 — the terminal REPORT, owned by the finalize claim.
            # Taken (and the report task SPAWNED) before the teardown awaits
            # below, so a cancellation landing anywhere in teardown cannot
            # strand the outcome: the shielded task is already live. The claim
            # returns False while _recovering without consuming itself, so a
            # pending cancel-recovery respawn is not reported done and its
            # respawned run can claim later.
            report_task = None
            # Set once this finally's session teardown has finished, so the
            # already-spawned report holds its "delivered" tombstone until the
            # child is provably gone (see `_report_terminal`).
            teardown_done = asyncio.Event()
            # Published where it survives this record being evicted: a settlement
            # that happens OUTSIDE this report (the parent's queue drain, issue
            # #4839) can come due after a dashboard clear/cancel has removed the run
            # from _agents AND _tasks, and it still must not tombstone a child that
            # is being killed.
            self._manager._teardown_gates[info.id] = teardown_done
            if self._manager._claim_finalize(info):
                info.elapsed = time.time() - info.started
                self._manager._record_cost(info)
                report_task = self._manager._spawn_terminal_report(
                    info,
                    source="Subagent",
                    injection_timeout_reason=(
                        f"delivery timed out after {int(_ON_DONE_TIMEOUT)}s" " (queue + injection)"
                    ),
                    mark_delivered_on_success=True,
                    settle_digest=True,
                    teardown_done=teardown_done,
                )
            # Nested try/finally: the teardown awaits must never be able to skip
            # the bookkeeping below (see `_teardown_run_session`).
            try:
                if not info.reaped:
                    await self._manager._teardown_run_session(info, session_key)
            finally:
                # Guard 2 of 3 — SLOT accounting on its own one-shot token, so
                # the count is released exactly once whichever terminal path
                # arrives first (and is NOT skipped just because the reaper set
                # `reaped`, which is how an earlier revision leaked slots).
                if self._manager._release_slot(info):
                    self._manager._running_count -= 1
                    self._manager._drain_queue()
                self._manager._tasks.pop(info.id, None)
                # Teardown is done (or was skipped because the reaper did it) —
                # release the report's delivered-tombstone gate. Unconditional,
                # so the report can never wedge on a cancelled teardown.
                teardown_done.set()
                # Set BEFORE the entry is dropped: a waiter that already holds the
                # event is released by the line above, and one arriving after finds
                # no entry, which now means exactly "nothing left to wait for".
                self._manager._teardown_gates.pop(info.id, None)

        # The report itself already ran (or is running) on the shielded task
        # spawned in the finally above; block until it completes so sequencing is
        # unchanged for callers.
        #
        # NOT during shutdown. `_run`'s CancelledError arm deliberately does not
        # re-raise, so by the time we reach this await the cancellation has been
        # consumed and `shield` would simply wait out the full _ON_DONE_TIMEOUT
        # injection cap — holding `cancel_all()`'s gather for up to 20 minutes.
        # The report is registered in `self._report_tasks`, so `cancel_all()`'s
        # bounded drain owns it from here.
        if report_task is not None and not self._manager._shutting_down:
            await self._manager._await_report(report_task)

    async def _touch_activity_impl(self, info: SubagentInfo) -> None:
        """Record stream activity for idle-stall detection.

        Updates ``last_activity`` and, if the subagent was previously flagged
        stalled by the reaper, clears the flag and notifies the UI so the
        running-card drops the "stalled" warning the moment work resumes.
        """
        info.last_activity = time.time()
        info._stall_suspect_at = 0.0  # activity resets the 2-sweep confirmation
        # Retire the oracle so the next suspicion samples a fresh baseline rather
        # than differencing against counters from before this activity, and bump
        # the generation so a consult submitted before this moment cannot land a
        # stalled verdict on an agent that has just proven it is working.
        if info._stall_oracle is not None:
            info._stall_oracle = info._stall_oracle.fresh()
        info._stall_gen += 1
        if info.stalled:
            info.stalled = False
            await self._manager._fire_event("subagent_stalled", info, {"stalled": False})

    async def _fire_event_impl(
        self, etype: str, info: SubagentInfo, extra: dict | None = None
    ) -> None:
        if self._manager._on_event:
            try:
                await self._manager._on_event(etype, info, extra or {})
            except Exception:
                logger.warning("on_event failed for %s/%s", etype, info.id, exc_info=True)

    def _queued_depth_impl(self, parent_session_key: str) -> int:
        """Number of spawns currently queued for *parent_session_key* (waiting
        behind the concurrency cap / stagger gate, not yet started)."""
        return sum(
            1 for q in self._manager._queue if q.get("parent_session_key", "") == parent_session_key
        )

    def queued_count_for_impl(self, parent_session_key: str) -> int:
        """Public queued-spawn count for *parent_session_key*.

        Spawns accepted behind the concurrency cap / stagger gate sit in
        ``_queue`` with no ``SubagentInfo`` yet, so ``running``-based checks
        read "no pending work" during exactly the window a wave is ramping.
        Reset-deferral guards must consult this alongside ``running``.
        """
        return self._manager._queued_depth(parent_session_key)

    def has_pending_work_for_impl(self, parent_session_key: str) -> bool:
        """True while *parent_session_key* has sub-agents RUNNING or QUEUED.

        The reset-deferral guards must consult this, not ``running`` alone —
        see :meth:`queued_count_for` for why. A parent session reset while a
        spawn is still queued strands that agent's completion on a
        cold-started, context-free replacement session.
        """
        if self._manager._queued_depth(parent_session_key) > 0:
            return True
        return any(a.parent_session_key == parent_session_key for a in self._manager.running)

    def _emit_queue_depth_impl(self, parent_session_key: str, batch_id: str = "") -> None:
        """Emit the current queued depth for *parent_session_key* as a
        ``subagent_queued`` lifecycle event.

        The chip is otherwise driven only by agents that have actually started
        (``subagent_spawn``), so agents sitting behind the concurrency cap /
        stagger gate are invisible and the chip can appear late or flicker.
        This advisory count lets the UI show "N waiting to start" the moment a
        wave is accepted, and stay mounted across the staggered ramp.

        Fire-and-forget: scheduled on the running loop; a no-op in sync/test
        contexts without a loop (the count is advisory UI signal, not state).
        """
        depth = self._manager._queued_depth(parent_session_key)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop (sync/test context) — advisory event skipped
        info = SubagentInfo(
            id="_queue",
            task="",
            parent_session_key=parent_session_key,
            batch_id=batch_id,
        )
        loop.create_task(self._manager._fire_event("subagent_queued", info, {"queued": depth}))

    async def _run_inner_impl(
        self,
        info: SubagentInfo,
        session_key: str,
    ) -> None:
        """Inner execution — called within timeout wrapper."""
        setattr(info, "_session_id", "")
        setattr(info, "_session_provider", "")
        setattr(info, "_session_cwd", "")
        # Mark the real start of execution BEFORE any await so the startup
        # watchdog measures from here, not from registration (which may include
        # an arbitrary spawn-approval wait). Must be the first statement.
        info._exec_started = time.time()
        # Reset the activity clock to execution start too: last_activity is set
        # at registration (like ``started``), which can include a long spawn-
        # approval / queue wait. Without this, _maybe_flag_stall would treat
        # that pre-execution delay as idle time and prematurely surface a
        # healthy, just-started subagent as "stalled".
        info.last_activity = info._exec_started
        # Inherit approval policy from parent session; yolo/trust overrides
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
            # confirmed garbage-collected (no longer in session store).
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
        # Inherit agent from parent session when not explicitly specified
        agent = info.agent or self._manager._sessions.get_agent(info.parent_session_key)
        if not info.agent and agent:
            sel().log_api_access(
                caller=f"subagent:{info.id}",
                operation="subagent.agent_inheritance",
                outcome="ok",
                source="subagent",
                resources=f"subagent_id={info.id},inherited_agent={agent}",
            )
        extra_kwargs: dict[str, Any] = {}
        # An explicit per-spawn model wins; otherwise fall back to the
        # configured sub-agent role model (agent.role_models['subagent']). When
        # that role is unpinned the helper returns "" so we omit the kwarg and
        # keep deferring to the provider's configured default, exactly as before.
        eff_model = info.model or _subagent_default_model()
        # Record the EFFECTIVE pin (per-spawn OR the role_models['subagent']
        # config pin, via ``_subagent_default_model()``) as the requested side of
        # the downgrade comparison — keying off the bare per-spawn ``model`` would
        # miss a config-pinned run served a different model.
        # For completely unpinned spawns (no per-spawn pin, no role pin) ``eff_model``
        # is ``""``; fall back to the literal ``"auto"`` sentinel so the frontend
        # can show a neutral chip instead of nothing at all (#5869).
        info.requested_model = eff_model or "auto"
        if eff_model:
            extra_kwargs["model"] = eff_model
        # Sub-agent reasoning effort (per-call override -> role_efforts['subagent']
        # -> chat default). Passed as an override so it wins over the factory's
        # agent-derived default; "" leaves it to that default.
        eff_effort = info.reasoning_effort or _subagent_default_effort()
        if eff_effort:
            extra_kwargs["reasoning_effort_override"] = eff_effort
        if info.bare:
            extra_kwargs["bare"] = True
        if info.allowed_tools:
            extra_kwargs["allowed_tools"] = info.allowed_tools
        if info.cwd:
            extra_kwargs["cwd"] = info.cwd

        # ── Session sharing: reuse parent's shared AcpRuntime ──
        # When enabled and eligible, subagents get a session on the parent's
        # companion AcpRuntime (~200ms startup, ~0 memory) instead of spawning
        # a fresh kiro-cli process (~3-5s, ~400MB).
        #
        # Retain-by-default: EVERY run keeps its session files (teardown skips
        # deletion on both arms), so any completed run is continuable while
        # its files survive. keep=True / continuation runs additionally take
        # the dedicated arm: their resume path is the proven dashboard
        # expire-and-session/load lifecycle, which owns its process. Whether a
        # SHARED-runtime sid is loadable is the open Phase 0 question — until
        # proven, a continue on a shared-arm run relies on the fail-closed
        # resume guard below rather than a spawn-time guarantee.
        if info.keep:
            self._manager._sessions.mark_continuable(session_key)
            self._manager._conversations[session_key] = time.time()
        use_session_sharing = (not info.keep) and self._manager._should_use_session_sharing(info)
        # A per-spawn or per-role model / reasoning-effort override cannot be
        # applied to the parent's already-started shared runtime (it was spawned
        # with the parent's model and cannot switch model per session). Force the
        # dedicated process path so the override in extra_kwargs actually reaches
        # get_or_create -> the provider factory; otherwise a configured sub-agent
        # model/effort would silently no-op on the default (session-sharing) path.
        if eff_model or eff_effort:
            use_session_sharing = False
        if use_session_sharing:
            try:
                client = await self._manager._create_shared_session(info, session_key, agent)
            except Exception as exc:
                # Fallback: shared runtime unavailable (dead, spawn failed, etc.)
                # Revert to legacy per-process path transparently.
                logger.warning(
                    "Subagent %s: session sharing failed (%s), falling back to dedicated process",
                    info.id,
                    exc,
                )
                info._session_sharing = False
                info._shared_provider = None
                use_session_sharing = False
                client, is_new, _resumed = await self._manager._sessions.get_or_create(
                    session_key,
                    agent=agent or None,
                    approval_policy=parent_policy,
                    **extra_kwargs,
                )
                is_cc = self._manager._is_cc_provider(client)
            else:
                is_new = True
                _resumed = False
                is_cc = False
        else:
            client, is_new, _resumed = await self._manager._sessions.get_or_create(
                session_key,
                agent=agent or None,
                approval_policy=parent_policy,
                **extra_kwargs,
            )
            is_cc = self._manager._is_cc_provider(client)

        # Capture cleanup identity immediately after successful session
        # acquisition. Every later step can fail and tombstone the run, so
        # delaying this until the state write leaves provider files unidentified.
        try:
            cleanup_session_id = (
                str(client.session_id or "") if hasattr(client, "session_id") else ""
            )
            cleanup_provider = self._manager._provider_label_of(client)
            cleanup_cwd = ""
            if is_cc:
                cleanup_cwd = info.cwd
                if not cleanup_cwd:
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
        named_agent = bool(info.agent and _AGENT_NAME_RE.fullmatch(info.agent))
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
            # streaming_text persist across the respawn; _run_inner never
            # resets them.)
            message = _CANCEL_RESUME_PREFIX + message
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
        full_message, _ = await run_in_embed_pool(
            self._manager._ctx_builder.build_message,
            message,
            is_new,
            session_key,
            project=info.cwd or None,
            provider_type=self._manager._provider_label_of(client),
            model_window=_sub_window,
            context_groups=_groups,
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
        # the panel shows what ran rather than only what was requested (issue
        # #3582). Best-effort at spawn: the ACP session/new response already
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
        # drained on cancellation via _write_state_off_loop (#6288, #6308; see
        # that helper for why a detached worker is the hazard). Best-effort with
        # ONE bounded retry: this write is the SINGLE owner of these two fields
        # on the spawn path (#5394) — the later session_id write no longer
        # doubles as a fallback, so a transient failure gets its second chance
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
        # via _write_state_off_loop (#6288, #7302): update_state ends in a
        # synchronous fsync, and the reaper, every chat turn and the heartbeat
        # share this loop. Off-loop also means the write TAKES update_state's
        # per-agent lock, which on-loop callers skip (#7280), so it can no longer
        # interleave with another pool writer's read-merge-rewrite.
        try:
            pid = self._manager._sessions.get_pid(session_key)
            if pid:
                info._pid = pid  # make available for _write_tombstone
                await self._manager._write_state_off_loop(
                    info, "PID record", pid=pid, pid_recorded_at=time.time()
                )
        except Exception:
            logger.debug("Failed to record PID for %s", info.id, exc_info=True)

        # Persist the cleanup identity captured immediately after session
        # acquisition, together with mutable retention intent.
        try:
            state_update: dict[str, object] = {
                "session_id": str(getattr(info, "_session_id", "")),
                "provider": str(getattr(info, "_session_provider", "")),
                # Model provenance (requested_model/resolved_model) is NOT
                # re-written here: the crash-safe write BEFORE the
                # subagent_spawn event above is the single owner of those two
                # fields on the spawn path, and a transient failure there is
                # handled by that write's own bounded retry (#5394).
                "keep": info.keep,
                "conversation_key": session_key if info.keep else "",
            }
            cleanup_cwd = str(getattr(info, "_session_cwd", ""))
            if cleanup_cwd:
                state_update["cwd"] = cleanup_cwd
            # Same off-loop, drained write as the PID record above (#6288,
            # #7302). This one also carries `keep`, the field the two remaining
            # on-loop writers (promote / release) contend for -- taking the
            # per-agent lock here is what orders it against any other pool
            # writer.
            await self._manager._write_state_off_loop(info, "session record", **state_update)
        except Exception:
            logger.debug("Failed to record session_id for %s", info.id, exc_info=True)

        _rp = agent_dir_for_display(info.id) / "result.txt"
        info.result_path = str(_rp)
        # Cache tool names by tool_call_id so PostToolUse can recover the tool name
        # when EVENT_TOOL_RESULT arrives (which only carries tool_call_id and output).
        # Mirrors kiro_crew.dashboard.chat_runner._pending_tools.
        _pending_tools: dict[str, str] = {}

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
            attempts = 0
            # One-shot post-activity allowance, mirroring the main path's
            # ``_posttoken_retry_used`` rule (dashboard/chat_runner.py ~L4324):
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
                try:
                    async for _ev in client.stream(msg):
                        yield _ev
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not acp_error_is_transient(exc):
                        raise
                    # Post-activity: continue instead of re-running. "Activity"
                    # is ANY text chunk, approved tool turn, or auto-allowed
                    # tool call — a mutating tool may have executed before the
                    # first text chunk, and replaying the full prompt would
                    # re-run it (duplicate writes/messages). Only a turn with
                    # zero observed activity resends the original prompt.
                    _had_activity = bool(result_text) or turns > 0 or info.tool_count > 0
                    if _had_activity:
                        if post_activity_attempts >= 1:
                            raise
                        post_activity_attempts += 1
                    elif attempts >= TRANSIENT_RETRIES:
                        # ── Throttle-exhaustion fallback chain ──
                        # Zero-activity budget spent: walk agent.fallback_model
                        # before surfacing (empty chain ⇒ raise exactly as
                        # before this feature). Two attempts per candidate
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
                        # and drained on cancellation via _write_state_off_loop
                        # (#6288, #6308).
                        try:
                            await self._manager._write_state_off_loop(
                                info, "refined model", resolved_model=_live_model
                            )
                        except Exception:
                            logger.debug(
                                "Failed to persist refined model for %s", info.id, exc_info=True
                            )
                result_text += event.text
                write_result_chunk(info.id, event.text)
                redacted = _redact(event.text)
                info.streaming_text += redacted
                if len(info.streaming_text) > 50_000:
                    info.streaming_text = "…(truncated)\n" + info.streaming_text[-40_000:]
                await self._manager._fire_event("subagent_chunk", info, {"text": redacted})
            elif event.kind == EVENT_PERMISSION_REQUEST:
                # Both kiro-cli and claude-agent-acp surface tool calls via
                # session/request_permission. Run them through the same hook
                # → parent_policy → interactive callback pipeline so the
                # approve / reads / trust / yolo protocol applies uniformly.
                #
                # Child-origin escalations (runtime-routed backend subagents)
                # do NOT consume the parent's turn budget: a child asking for
                # enough permissions would otherwise trip the parent's
                # turn_limit and kill the whole subagent run on activity that
                # is not the parent's own turns. They get their OWN bound
                # instead — without one, a chatty or adversarial backend
                # child could generate unbounded approval prompts until the
                # wall-clock reaper fires (the turn budget used to bound
                # exactly this traffic). Generous multiple of the parent's
                # limit: legitimate crews fan many small child tool calls.
                if not event.sub_session_id:
                    turns += 1
                    info.turns = turns
                else:
                    # Child escalation: counted toward its own volume bound
                    # here; side-effect activity (tool_count) is counted at
                    # APPROVAL in _approve_and_log — a purely rejected
                    # escalation executed nothing and must not consume the
                    # run's replay budget (tool_count gates prompt replay
                    # and cancel-respawn).
                    child_escalations += 1
                    if child_escalations > child_escalation_limit:
                        # Answer the triggering request BEFORE bailing: this
                        # event is already dequeued, so returning without a
                        # response would strand the child's oneshot — under
                        # session sharing the runtime outlives this subagent
                        # and nothing else tears the connection down. The
                        # "requests are answered on every queue path"
                        # contract this PR establishes applies to limit
                        # bails too.
                        try:
                            await self._manager._reject_and_log(
                                client,
                                event.request_id,
                                session_key,
                                event,
                                error="child_escalation_limit",
                            )
                        except Exception:
                            logger.exception("failed to reject escalation-limit trigger request")
                        info.result = result_text or "_Partial output._"
                        info.error = f"child_escalation_limit:{child_escalation_limit}"
                        info.done = True
                        Stats().inc_subagent_failed()
                        logger.warning(
                            "Subagent %s hit child escalation limit (%d)",
                            info.id,
                            child_escalation_limit,
                        )
                        self._manager._write_tombstone(info, "child_escalation_limit")
                        return
                # Diagnostic pointer is written for BOTH origins — orphan
                # recovery must see child activity too; only the turn
                # increment is parent-scoped.
                info.last_tool = event.title or ""
                self._manager._note_tool_dispatch(info, event)
                # Persist turn state for orphan recovery diagnostics. Off-loop
                # and drained on cancellation via _write_state_off_loop (#6288,
                # #6306) — the highest-frequency of the three off-loop state
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
                    },
                )
                if turns > turn_limit:
                    # Same contract as the child_escalation_limit bail: the
                    # triggering request is already dequeued and must be
                    # answered before this loop exits, or its oneshot strands.
                    try:
                        await self._manager._reject_and_log(
                            client, event.request_id, session_key, event, error="turn_limit"
                        )
                    except Exception:
                        logger.exception("failed to reject turn-limit trigger request")
                    info.result = result_text or "_Partial output._"
                    info.error = f"turn_limit:{turn_limit}"
                    info.done = True
                    Stats().inc_subagent_failed()
                    logger.warning("Subagent %s hit turn limit (%d)", info.id, turn_limit)
                    self._manager._write_tombstone(info, "turn_limit")
                    return
                tool_result = self._manager._ctx_builder.hooks.on_tool_call(
                    event.title,
                    session_key=session_key,
                    agent=info.agent or "",
                    app=info.app or "",
                    tool_kind=event.tool_kind,
                    raw_params=event.raw_tool_params,
                    command=event.shell_command,
                    is_shell=event.is_shell,
                    mcp_server_name=event.mcp_server_name,
                    mcp_tool_name=event.tool_name,
                )
                if tool_result.action == TOOL_DENY:
                    await self._manager._reject_and_log(
                        client, event.request_id, session_key, event, error="hook_deny"
                    )
                    continue
                if event.child_low_fidelity:
                    # UNCONDITIONAL parent grant: parent_policy=auto approves
                    # regardless of event content, so it may honor a request
                    # that is grant-eligible (see
                    # AcpEvent.child_unconditional_grant_eligible — inside
                    # this low-fidelity branch that means the canonical MCP
                    # identity is verified and only the ARGUMENTS are
                    # unverified, which this grant never reads). Honor the
                    # grant instead of stalling a trusted fan-out on an
                    # interactive card per call. The hook auto-approve below
                    # stays fail-closed for these: its auto_approve_tools
                    # patterns match the agent-authored title, which a child
                    # could forge.
                    if parent_policy == "auto" and event.child_unconditional_grant_eligible:
                        await self._manager._approve_and_log(
                            client,
                            event.request_id,
                            session_key,
                            event,
                            metadata={
                                "subagent_id": info.id,
                                "reason": "parent_policy_auto",
                                "child_mcp_identity": (
                                    f"{event.mcp_server_name}/{event.tool_name}"
                                ),
                                "child_args_unverified": True,
                            },
                            info=info,
                        )
                        continue
                    # Backend-internal child origin whose SECURITY context is
                    # absent (structured params missing, unresolved shell
                    # classification, or shell without a recoverable command —
                    # AcpEvent.child_low_fidelity): any AUTO-approve would
                    # rest on the LLM-authored title alone, so skip the hook
                    # auto-approve and parent_policy=auto branches. When an
                    # interactive approver IS configured — the per-subagent
                    # factory, or the gateway-level _on_tool_approval
                    # fallback the non-child path below also uses — hand the
                    # decision to it: that is a human/host judgment, the same
                    # downgrade the dashboard's card provides. Only a truly
                    # headless consumer fails closed.
                    _child_fallback = self._manager._on_tool_approval
                    if self._manager._on_tool_approval_factory or _child_fallback is not None:
                        # The human must know the title is ALL there is: the
                        # structured params the policy gates would verify are
                        # absent, so the displayed text is agent-authored and
                        # unverifiable. Annotate the prompt so the approval
                        # is an informed judgment, not a title-only rubber
                        # stamp.
                        event.title = (
                            "⚠️ UNVERIFIED child request (security context "
                            f"missing — title is agent-authored): {event.title or '<unknown tool>'}"
                        )
                        approved = False
                        # Same human-wait lifecycle as the ordinary callback
                        # branches below: without _awaiting_approval the
                        # reaper reads a healthy approval wait as a stalled
                        # subagent after the idle threshold.
                        info._awaiting_approval = True
                        try:
                            if self._manager._on_tool_approval_factory:
                                approve_cb = self._manager._on_tool_approval_factory(info)
                                approved = bool(await approve_cb(event))
                            elif _child_fallback is not None:
                                approved = bool(
                                    await _child_fallback(event, info.parent_session_key)
                                )
                        except Exception:
                            logger.exception("child approval callback failed")
                        finally:
                            info._awaiting_approval = False
                            info.last_activity = time.time()
                        if approved:
                            await self._manager._approve_and_log(
                                client,
                                event.request_id,
                                session_key,
                                event,
                                metadata={
                                    "subagent_id": info.id,
                                    "reason": "child_interactive_approved",
                                },
                                info=info,
                            )
                        else:
                            await self._manager._reject_and_log(
                                client,
                                event.request_id,
                                session_key,
                                event,
                                error="child_interactive_rejected",
                            )
                        continue
                    await self._manager._reject_and_log(
                        client,
                        event.request_id,
                        session_key,
                        event,
                        error="child_origin_no_command_context",
                    )
                    continue
                if tool_result.action == TOOL_AUTO_APPROVE:
                    # The hook granted this by NAME (its `auto_approve_tools`
                    # globs, or the read-only allowlist). Honour it only while
                    # each program name in the command still resolves to the
                    # program it appears to name; a shadowed, agent-tree or
                    # unidentified resolution DOWNGRADES to the remaining rungs
                    # below (parent policy, the interactive factory, the
                    # gateway fallback, or the headless fail-closed reject) —
                    # never a hard block. This surface runs unattended, which
                    # makes an unverified name the cheaper attack path here,
                    # not the rarer one.
                    _ng_refusal = await name_grant.refusal_for_event(event)
                    if _ng_refusal is None:
                        await self._manager._approve_and_log(
                            client,
                            event.request_id,
                            session_key,
                            event,
                            metadata={"subagent_id": info.id, "reason": "hook_auto_approve"},
                            info=info,
                        )
                        continue
                    logger.warning(
                        "declining a hook auto-approve: %s; the request falls "
                        "through to the subagent's normal approval path",
                        _ng_refusal.log_text,
                    )
                    name_grant.log_decline(
                        source="subagent",
                        session_key=session_key,
                        event=event,
                        refusal=_ng_refusal,
                        tier="hook_auto_approve",
                        metadata={"subagent_id": info.id},
                        sel_factory=sel,
                    )
                if parent_policy == "auto":
                    await self._manager._approve_and_log(
                        client,
                        event.request_id,
                        session_key,
                        event,
                        metadata={"subagent_id": info.id, "reason": "parent_policy_auto"},
                        info=info,
                    )
                    continue
                if self._manager._on_tool_approval_factory:
                    approve_cb = self._manager._on_tool_approval_factory(info)
                    info._awaiting_approval = True
                    try:
                        approved = await approve_cb(event)
                    finally:
                        info._awaiting_approval = False
                        info.last_activity = time.time()
                    if not approved:
                        await self._manager._reject_and_log(
                            client,
                            event.request_id,
                            session_key,
                            event,
                            metadata={"subagent_id": info.id, "reason": "factory_rejected"},
                        )
                        continue
                    await self._manager._approve_and_log(
                        client,
                        event.request_id,
                        session_key,
                        event,
                        metadata={"subagent_id": info.id},
                        info=info,
                    )
                elif self._manager._on_tool_approval:
                    info._awaiting_approval = True
                    try:
                        approved = await self._manager._on_tool_approval(
                            event, info.parent_session_key
                        )
                    finally:
                        info._awaiting_approval = False
                        info.last_activity = time.time()
                    if not approved:
                        await self._manager._reject_and_log(
                            client, event.request_id, session_key, event
                        )
                        continue
                    await self._manager._approve_and_log(
                        client,
                        event.request_id,
                        session_key,
                        event,
                        metadata={"subagent_id": info.id},
                        info=info,
                    )
                else:
                    # No callback, no auto policy — deny by default
                    await self._manager._reject_and_log(
                        client,
                        event.request_id,
                        session_key,
                        event,
                        metadata={"subagent_id": info.id, "reason": "no_policy_deny_default"},
                    )
                    continue
            elif event.kind == EVENT_TOOL_CALL:
                # Auto-allowed (kiro-internal) tools surface here as informational
                # tool_call updates and NEVER as EVENT_PERMISSION_REQUEST, so this
                # is the only progress signal a simple/read-only subagent task emits.
                # Count it, record it, and broadcast the same subagent_tool event
                # the permission path uses so the running-card shows live activity.
                info.tool_count += 1
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
                    },
                )
                # Fire PreToolUse hooks for auto-approved tools (informational only)
                sel().log_tool_invocation(
                    session_key=session_key,
                    source="subagent",
                    tool_name=event.title,
                    tool_kind=event.tool_kind,
                    outcome="auto_approved",
                    metadata={"subagent_id": info.id},
                )
                # Cache tool name so PostToolUse can recover it on EVENT_TOOL_RESULT.
                # Strip "Running: " prefix to match the name passed to PreToolUse hooks.
                _raw = event.title or ""
                if _raw.startswith("Running: "):
                    _raw = _raw[9:]
                if event.tool_call_id:
                    _pending_tools[event.tool_call_id] = _raw
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
                if self._manager.hook_store is not None:
                    try:
                        _tool_name = _pending_tools.pop(event.tool_call_id, "")
                        _out = _redact((event.tool_output or "")[:2000])
                        await self._manager.hook_store.fire(
                            HOOK_EVENT_POST_TOOL_USE,
                            tool_name=_tool_name,
                            tool_response={"output": _out},
                            subagent_id=info.id,
                            parent_session_key=info.parent_session_key or None,
                            agent_role=info.agent or None,
                        )
                    except Exception:
                        logger.debug(
                            "PostToolUse hook error in subagent",
                            exc_info=True,
                        )
            elif event.kind == EVENT_COMPLETE:
                _complete_event = event
                break

        # Strip [OPTIONS: ...] tags and redact sensitive content
        cleaned, _ = extract_options(result_text) if result_text else (result_text, [])
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
        info.result = cleaned or "_No response._"
        # Cap disk file and trim memory — gateway decides how much to show based on mode.
        if info.result_path:
            cap_result_file(Path(info.result_path))
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
        evict_completed_agents(self._manager._agents)

        # ── Per-turn usage row: attribute subagent spend. ──
        # Deliberately BEFORE `info.done`: the caller's cleanup (which awaits
        # provider.shutdown() -> handle.destroy()) runs after this function
        # returns, so an await placed after `done` sits inside the
        # done-to-teardown window. Waiters that poll for `done` would then
        # observe completion while this file write is still in flight — which
        # widens that window on slow filesystems and lets teardown-observing
        # callers race it. Writing first also means `done` never becomes
        # visible with the usage row still missing.
        try:
            # circular import: reached while kiro_crew.slack.handler is still
            # initialising (dashboard/handlers/files.py imports is_tracked_channel
            # from it), so a module-scope import raises ImportError under the
            # suite's import order.
            from kiro_crew.dashboard.handlers.usage import (
                persist_token_record_async,
                read_context_tokens,
                read_effective_agent,
            )

            _used, _window = read_context_tokens(client)
            await persist_token_record_async(
                session_key,
                # Blank while a fallback serves this run: the explicit pin
                # would bill the fallback's spend to a model that never
                # executed; model_source reports what actually ran.
                ("" if provider_fallback_active(client) else (info.model or "")),
                _complete_event,
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
                elapsed_ms=int((time.monotonic() - _turn_t0) * 1000),
                model_source=client,
            )
        except Exception:
            logger.debug("usage row (subagent) persist failed", exc_info=True)

        info.done = True
        self._manager._sessions.record_success(session_key)

        Stats().inc_subagent_completed()
        logger.info("Subagent %s completed", info.id)

    def _should_use_session_sharing_impl(self, info: SubagentInfo) -> bool:
        """Decide whether a subagent should use the shared-runtime path.

        All must hold: session_sharing config True; parent session exists and
        is ACP/kiro-backed (not CC); not a CC-specific spawn (model/allowed_tools/bare).
        """
        try:
            cfg = KiroCrewConfig.load()
            if not cfg.agent.session_sharing:
                return False
        except Exception:
            return False
        if info.model or info.allowed_tools or info.bare:
            return False
        if not info.parent_session_key:
            return False
        return self._manager._sessions.is_session_sharing_eligible(info.parent_session_key)

    async def _create_shared_session_impl(
        self,
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

        runtime = self._manager._get_parent_runtime(info.parent_session_key)
        if runtime is None:
            runtime = await self._manager._sessions.get_subagent_runtime(info.parent_session_key)

        cwd = info.cwd or str(getattr(self._manager._sessions, "_pool_cwd", ""))
        handle = await runtime.create_session(
            cwd=cwd or None,
            agent=agent or None,
        )
        provider = AcpSessionProvider(handle, runtime)
        # The handle exists now. Publish ownership before any cancellable await so
        # force-reap always takes the shared-session branch and destroys this handle
        # instead of resetting a nonexistent dedicated session.
        provider.child_fidelity_aware = True
        info._session_sharing = True
        info._shared_provider = provider
        # Capture cleanup identity before persistence or later setup can fail,
        # otherwise the live handle becomes an untracked ghost.
        cleanup_session_id = str(handle.session_id or "")
        cleanup_provider = PROVIDER_LABEL_DEFAULT
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
            try:
                # Keep the shared handle alive on a storage error, but route the
                # write through the run-owned off-loop drain so cancellation
                # cannot detach a stale whole-file writer (#6288, #7302).
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

    def _get_parent_runtime_impl(self, parent_session_key: str) -> "AcpRuntime | None":
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
