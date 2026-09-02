"""Terminal behavior for the SubagentManager facade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._component import ManagerComponent

if TYPE_CHECKING:
    from ..subagent import (
        _ON_DONE_TIMEOUT,
        _OUTBOX_RESULT_SUMMARY_LEN,
        _RESET_TIMEOUT,
        _TERMINAL_RETRY_SECONDS,
        EXECUTION_LEASE_SECONDS,
        SUBAGENT_COMPLETION_PREFIX,
        AuthorityOutcomeUncertain,
        CoordinatorDecision,
        DeliveryState,
        OutboxEvent,
        RunCompletion,
        RunOutcome,
        Stats,
        SubagentInfo,
        _done_result,
        _injection_notice_outcome,
        _load_child_process_helpers,
        _OutboxDeliveryContext,
        _OutboxDeliveryRetry,
        _redact,
        _TerminalCommitRejected,
        _timeout_context,
        _ws_result_path,
        asyncio,
        clear_tombstone,
        dashboard_slot_key,
        json,
        logger,
        mark_delivered,
        os,
        platform_compat,
        redact_credentials,
        redact_exfiltration_urls,
        sel,
        subprocess_executor,
        time,
    )


class TerminalCoordinator(ManagerComponent):
    """Own terminal transitions while state remains facade-owned."""

    __slots__ = ()

    async def _coordinator_mark_starting_impl(self, info: SubagentInfo) -> None:
        """Acquire the first durable lifecycle transition before child startup."""

        if info._coordinator_started or info._coordinator_fence is None:
            return
        command = info._coordinator_command
        if command is None:
            raise RuntimeError("coordinator execution command is missing")
        try:
            result = await self._manager._coordinator.mark_starting(
                command,
                info._coordinator_fence,
                info._coordinator_version,
            )
        except Exception:
            result = await self._manager._coordinator.mark_starting(
                command,
                info._coordinator_fence,
                info._coordinator_version,
            )
        if result.value is None or result.decision is CoordinatorDecision.REJECTED:
            raise RuntimeError(f"coordinator start refused: {result.reason.value}")
        info._coordinator_version = result.value.version
        info._coordinator_started = True

    async def _coordinator_mark_running_impl(self, info: SubagentInfo) -> None:
        """Commit that a child session exists before prompting it."""

        if info._coordinator_running or info._coordinator_fence is None:
            return
        try:
            result = await self._manager._coordinator.mark_running(
                info.id,
                info._coordinator_fence,
                info._coordinator_version,
            )
        except Exception:
            result = await self._manager._coordinator.mark_running(
                info.id,
                info._coordinator_fence,
                info._coordinator_version,
            )
        if result.value is None or result.decision is CoordinatorDecision.REJECTED:
            raise RuntimeError(f"coordinator running transition refused: {result.reason.value}")
        info._coordinator_version = result.value.version
        info._coordinator_running = True

    def _start_coordinator_heartbeat_impl(self, info: SubagentInfo) -> None:
        fence = info._coordinator_fence
        if fence is None or info.id in self._manager._lease_tasks:
            return
        renew_fence = fence

        async def _renew() -> None:
            cadence = EXECUTION_LEASE_SECONDS / 3
            while True:
                await asyncio.sleep(cadence)
                try:
                    renewed = await self._manager._coordinator.renew(
                        info.id,
                        renew_fence,
                        time.time() + EXECUTION_LEASE_SECONDS,
                    )
                except Exception:
                    logger.warning(
                        "Subagent %s coordinator lease renewal failed",
                        info.id,
                        exc_info=True,
                    )
                    continue
                if not renewed:
                    logger.error("Subagent %s lost its coordinator execution lease", info.id)
                    return

        task = asyncio.create_task(_renew())
        self._manager._lease_tasks[info.id] = task

        def _forget(done: asyncio.Task[None]) -> None:
            if self._manager._lease_tasks.get(info.id) is done:
                self._manager._lease_tasks.pop(info.id, None)

        task.add_done_callback(_forget)

    async def _stop_coordinator_heartbeat_impl(self, run_id: str) -> None:
        lease_task = self._manager._lease_tasks.pop(run_id, None)
        if lease_task is None:
            return
        lease_task.cancel()
        await asyncio.gather(lease_task, return_exceptions=True)

    def _coordinator_outcome_impl(self, info: SubagentInfo) -> RunOutcome:
        if info.user_stopped:
            return RunOutcome.STOPPED
        if info.error:
            return RunOutcome.FAILED
        return RunOutcome.COMPLETED

    def _completion_payload_impl(self, info: SubagentInfo) -> str:
        task, _ = redact_exfiltration_urls(info.task)
        task, _ = redact_credentials(task)
        error, _ = redact_exfiltration_urls(info.error)
        error, _ = redact_credentials(error)
        summary, _ = redact_exfiltration_urls(_done_result(info.result))
        summary, _ = redact_credentials(summary)
        summary = summary[:_OUTBOX_RESULT_SUMMARY_LEN]
        return json.dumps(
            {
                "id": info.id,
                "parent_session_key": info.parent_session_key,
                "agent": info.agent,
                "task": task[:1000],
                "outcome": info.outcome,
                "error": error[:2000],
                "result_path": info.result_path,
                "result_summary": summary,
                "result_truncated": bool(
                    info.result_truncated or len(info.result) > _OUTBOX_RESULT_SUMMARY_LEN
                ),
                "user_stopped": info.user_stopped,
                "silent": info.silent,
                "batch_id": info.batch_id,
                "batch_total": info.batch_total,
                "elapsed": info.elapsed,
                "conversation_key": info.conversation_key,
                "resolved_model": info.resolved_model,
                "requested_model": _redact(info.requested_model),
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    async def _commit_terminal_event_impl(self, info: SubagentInfo) -> OutboxEvent | None:
        fence = info._coordinator_fence
        if fence is None:
            return None
        try:
            result = await self._manager._coordinator.complete(
                RunCompletion(
                    run_id=info.id,
                    outcome=self._manager._coordinator_outcome(info),
                    result_path=info.result_path,
                    error=_redact(info.error),
                    event_type="subagent_completion",
                    destination=info.parent_session_key,
                    payload_json=self._manager._completion_payload(info),
                    terminal_at=time.time(),
                ),
                fence,
                info._coordinator_version,
            )
        except Exception:
            logger.exception("Subagent %s terminal coordinator commit failed", info.id)
            return None
        if result.decision is CoordinatorDecision.REJECTED:
            logger.error(
                "Subagent %s terminal coordinator commit refused: %s",
                info.id,
                result.reason.value,
            )
            raise _TerminalCommitRejected(result.reason.value)
        if result.value is None:
            return None
        info._coordinator_version = result.value.run_version
        info._delivery_event_id = result.value.event_id
        return result.value

    def _info_from_outbox_impl(self, event: OutboxEvent) -> SubagentInfo:
        payload = json.loads(event.payload_json)
        if not isinstance(payload, dict):
            raise ValueError("subagent completion payload must be an object")
        info = SubagentInfo(
            id=str(payload.get("id") or event.run_id),
            task=str(payload.get("task") or ""),
            parent_session_key=str(payload.get("parent_session_key") or event.destination),
            agent=str(payload.get("agent") or ""),
            done=True,
            result=str(payload.get("result_summary") or ""),
            result_path=str(payload.get("result_path") or ""),
            result_truncated=bool(payload.get("result_truncated")),
            error=str(payload.get("error") or ""),
            user_stopped=bool(payload.get("user_stopped")),
            silent=bool(payload.get("silent")),
            elapsed=float(payload.get("elapsed") or 0.0),
            conversation_key=str(payload.get("conversation_key") or ""),
            resolved_model=str(payload.get("resolved_model") or ""),
            requested_model=str(payload.get("requested_model") or ""),
        )
        # Batch progress lives in the gateway's volatile digest state. After a
        # restart, replay each stable event independently instead of inventing
        # a fresh one-member wave from persisted batch labels.
        info._delivery_event_id = event.event_id
        return info

    async def _deliver_outbox_event_impl(self, event: OutboxEvent) -> bool:
        """Hand one claimed event to existing routing, returning acceptance."""

        context = self._manager._outbox_contexts.get(event.event_id)
        if context is None:
            context = self._manager._outbox_live_contexts.pop(event.run_id, None)
            if context is None:
                info = self._manager._info_from_outbox(event)
                context = _OutboxDeliveryContext(
                    info=info,
                    source="Subagent outbox",
                    injection_timeout_reason="durable completion delivery timed out",
                    mark_delivered_on_success=True,
                    settle_digest=True,
                    teardown_done=None,
                )
            self._manager._outbox_contexts[event.event_id] = context
        info = context.info
        info._delivery_event_id = event.event_id
        # A deferred destination already accepted this stable event into its
        # own queue or digest. Background retries keep its durable identity
        # pending for acknowledgement without repeating routing or accounting.
        if info._digest_held or info._delivery_queued:
            return False
        info._delivery_failed = False
        if not context.effects_fired:
            await self._manager._fire_event(
                "subagent_done",
                info,
                {
                    "elapsed": info.elapsed,
                    "error": _redact(info.error) if info.error else None,
                    "stopped": info.user_stopped,
                    "outcome": info.outcome,
                    "task": _redact(info.task),
                    "agent": _redact(info.agent),
                    "child_session": info.conversation_key or f"subagent:{info.id}",
                    "model": info.resolved_model,
                    "requested_model": _redact(info.requested_model),
                    "result": _done_result(info.result),
                    "event_id": event.event_id,
                },
            )
            context.effects_fired = True
        if self._manager._on_done:
            info._delivery_retry = context.callback_started
            context.callback_started = True
            try:
                await asyncio.wait_for(self._manager._on_done(info), timeout=_ON_DONE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.error(
                    "%s: completion injection timed out for %s after %.0fs",
                    context.source,
                    info.id,
                    _ON_DONE_TIMEOUT,
                )
                try:
                    await self._manager._sessions.reset(info.parent_session_key)
                except Exception:
                    logger.debug(
                        "Failed to reset parent session %s after injection timeout",
                        info.parent_session_key,
                        exc_info=True,
                    )
                self._manager.notify_injection_failed(
                    info,
                    reason=context.injection_timeout_reason,
                )
                raise _OutboxDeliveryRetry(context.injection_timeout_reason)
            except Exception:
                logger.exception("%s: announce failed for %s", context.source, info.id)
                raise
        if info._delivery_failed:
            raise _OutboxDeliveryRetry("completion destination reported a routing failure")
        info._delivery_retry = False
        info._delivery_batch_progress = None
        info._delivery_batch_final = False
        info._reported_to_parent = True
        if info._digest_held or info._delivery_queued:
            return False
        if context.settle_digest:
            await self._manager._settle_digest_holds(info)
        if context.mark_delivered_on_success and info.outcome == "completed":
            teardown_done = context.teardown_done
            if teardown_done is not None and not teardown_done.is_set():
                try:
                    await asyncio.wait_for(teardown_done.wait(), timeout=_RESET_TIMEOUT + 30)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Subagent %s: teardown did not complete before the "
                        "delivered tombstone; writing it anyway",
                        info.id,
                    )
            try:
                await asyncio.to_thread(mark_delivered, info.id)
            except Exception:
                logger.debug("Failed to mark subagent %s delivered", info.id, exc_info=True)
            try:
                slot_key = dashboard_slot_key(info.parent_session_key)
                if slot_key:
                    _ws_result_path(slot_key, info.id).unlink(missing_ok=True)
            except Exception:
                logger.debug("Failed to clean workspace result for %s", info.id, exc_info=True)
        self._manager._outbox_contexts.pop(event.event_id, None)
        return True

    async def _reject_waiting_before_terminal_impl(self, info: SubagentInfo, error: str) -> None:
        """Persist a pre-start rejection while its run lease protects terminal delivery."""

        while True:
            try:
                await self._manager.command_authority.reject_waiting_execution(
                    info.id,
                    error,
                    stop_heartbeat=False,
                )
                return
            except asyncio.CancelledError:
                raise
            except AuthorityOutcomeUncertain:
                logger.warning(
                    "Subagent %s waiting rejection commit failed; retrying",
                    info.id,
                    exc_info=True,
                )
                await asyncio.sleep(_TERMINAL_RETRY_SECONDS)

    def _claim_finalize_impl(self, info: SubagentInfo, *, supersede_recovery: bool = False) -> bool:
        """Claim the exclusive right to report ``info``'s terminal outcome.

        Returns True for exactly one caller. Both the reap path and ``_run``'s
        ``finally`` call this and report only if it returns True, so the parent
        is notified exactly once no matter which wins the race or whether the
        loser is cancelled part-way through its teardown.

        Contains no ``await``, so on a single-threaded event loop the
        check-and-set is atomic with respect to other tasks.

        Returns False while ``_recovering`` — a cancel-recovery respawn is
        pending and the agent must not be reported done yet — leaving the claim
        OPEN so the respawned run can take it later.

        ``supersede_recovery=True`` overrides that withholding and is used ONLY
        by definitively-terminal callers (`_force_reap`, which also serves user
        Stop). Without it a reap landing inside the recovery window stranded the
        outcome: the reap was refused the claim, performed teardown and set
        ``reaped``, reported nothing — and `_resume`'s ``reaped`` abort path
        bare-returns, so no path ever reported and the agent sat unfinished
        until the reaper's wall-clock deadline. Superseding also clears
        ``_recovering``, because a killed agent has nothing left to respawn.

        Scope note: this token governs REPORTING only, and it deliberately does
        NOT consult ``info.done``. Gating it on ``done`` is wrong:
        if ``_run_inner`` set ``done`` while the reap awaited its session reset,
        the reaper refused the claim, still marked ``reaped``, and ``_run``'s
        finally then skipped its own claim — nobody reported. The terminal RECORD
        (tombstone/stat) keeps its own ``not info.done`` guard and slot accounting
        has its own one-shot token (:meth:`_release_slot`); three concerns, three
        guards. Session teardown stays keyed on ``reaped``.
        """
        return self._manager._lifecycle.claim_report(
            info,
            supersede_recovery=supersede_recovery,
        )

    async def _report_terminal_impl(
        self,
        info: SubagentInfo,
        *,
        source: str,
        injection_timeout_reason: str,
        mark_delivered_on_success: bool,
        settle_digest: bool = False,
        teardown_done: "asyncio.Event | None" = None,
    ) -> None:
        """Deliver ``info``'s one-shot terminal report as a single unit.

        This is the exact work the finalize claim guards: fire the
        ``subagent_done`` WS event, then inject the completion into the parent
        (``_on_done``) with its ``_ON_DONE_TIMEOUT`` cap, timeout handling, and
        (for the ``_run`` path) the result.txt TTL / workspace-cleanup
        bookkeeping.

        Why this is a separate coroutine run under ``asyncio.shield`` (see
        ``_run_terminal_report``): the claim makes reporting EXCLUSIVE but not
        ATOMIC. A claimer cancelled mid-report (``_force_reap`` /
        ``cancel_all()`` cancelling the task while it awaits ``_fire_event`` or
        ``_on_done``) would exit without delivering, and the other path — seeing
        the claim already taken — stays silent, so the completed outcome never
        reaches the parent. Running the report on a shielded, strongly-held task
        makes it complete independently of caller cancellation.

        The two call sites (reap vs. ``_run``'s ``finally``) differ only in the
        injection-timeout reason string, the log ``source`` prefix, and whether
        a successful delivery marks the result delivered — those are passed as
        arguments rather than unified away. The WS payload is identical (both
        set ``info.elapsed`` before calling), so it is built from ``info`` here.
        """
        if info._coordinator_fence is not None:
            context = _OutboxDeliveryContext(
                info=info,
                source=source,
                injection_timeout_reason=injection_timeout_reason,
                mark_delivered_on_success=mark_delivered_on_success,
                settle_digest=settle_digest,
                teardown_done=teardown_done,
            )
            self._manager._outbox_live_contexts[info.id] = context
            event = None
            while event is None:
                try:
                    event = await self._manager._commit_terminal_event(info)
                except _TerminalCommitRejected:
                    if self._manager._outbox_live_contexts.get(info.id) is context:
                        self._manager._outbox_live_contexts.pop(info.id, None)
                    await self._manager._stop_coordinator_heartbeat(info.id)
                    await self._manager.command_authority.stop_execution_heartbeat(info.id)
                    try:
                        await asyncio.to_thread(clear_tombstone, info.id)
                    except Exception:
                        logger.debug(
                            "Failed to re-admit stale-fence result %s to recovery",
                            info.id,
                            exc_info=True,
                        )
                    return
                if event is None:
                    await asyncio.sleep(_TERMINAL_RETRY_SECONDS)
            if self._manager._outbox_live_contexts.get(info.id) is context:
                self._manager._outbox_live_contexts.pop(info.id, None)
            if not info._reported_to_parent:
                self._manager._outbox_contexts.setdefault(event.event_id, context)
            try:
                await self._manager._stop_coordinator_heartbeat(info.id)
            finally:
                await self._manager.command_authority.stop_execution_heartbeat(info.id)
            if info._reported_to_parent:
                return
            while True:
                try:
                    attempts = await self._manager._outbox_delivery.drain_once(
                        event_id=event.event_id
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "Subagent %s durable completion routing failed",
                        info.id,
                        exc_info=True,
                    )
                else:
                    if any(attempt.status is DeliveryState.DELIVERED for attempt in attempts):
                        return
                    if info._reported_to_parent or info._digest_held or info._delivery_queued:
                        return
                await asyncio.sleep(_TERMINAL_RETRY_SECONDS)

        await self._manager._fire_event(
            "subagent_done",
            info,
            {
                "elapsed": info.elapsed,
                "error": _redact(info.error) if info.error else None,
                "stopped": info.user_stopped,
                "outcome": info.outcome,
                "task": _redact(info.task),
                "agent": _redact(info.agent),
                # The sub-agent's own session key (see build_subagent_snapshot):
                # lets a client fetch this node's own context-trace even after
                # it has finished.
                "child_session": info.conversation_key or f"subagent:{info.id}",
                # The model actually served (issue #3582). By the terminal
                # report this is the authoritative value on every provider — the
                # CC/raw path has completed at least one turn, so its
                # ``_resolved_model_id`` is populated (refreshed in ``_run``).
                "model": info.resolved_model,
                # Carry the requested pin on the terminal report too, redacted
                # like the spawn frame: after a reconnect the completed card is
                # rebuilt from this event alone, so without it the live-downgrade
                # amber chip would silently vanish from a downgraded finished run.
                "requested_model": _redact(info.requested_model),
                "result": _done_result(info.result),
            },
        )
        if not self._manager._on_done:
            return
        try:
            await asyncio.wait_for(self._manager._on_done(info), timeout=_ON_DONE_TIMEOUT)
            # The outcome has REACHED the parent. Recorded before any further
            # await so a shutdown cancellation landing in the teardown wait or
            # the tombstone write below is not mistaken for a lost delivery by
            # `cancel_all()` (which would re-deliver it on the next start).
            info._reported_to_parent = True
            if settle_digest:
                # _on_done returned without raising, so the wave digest (if this
                # was the final member) has been handed off. Only NOW settle the
                # held members' delivery tombstones.
                await self._manager._settle_digest_holds(info)
            # Digest-held wave members are NOT marked delivered here: their
            # result has not reached the parent yet (the gateway marks them when
            # the digest fires), so a restart mid-wave leaves them visible to
            # orphan reconciliation.
            #
            # A QUEUED injection is the same statement about a different wait:
            # the announce is parked in the parent's slot queue, so the result is
            # not in its context yet and the retention clock must not start (the
            # drain settles it). Both flags are set by the gateway inside
            # _on_done, above.
            if (
                mark_delivered_on_success
                and not info.error
                and not info._digest_held
                and not info._delivery_queued
            ):
                # Wait for the caller's session teardown before writing the
                # "delivered" tombstone. This report is deliberately SPAWNED
                # ahead of teardown (so a cancellation cannot strand it), which
                # opens a window the older post-teardown ordering did not have:
                # a crash after the tombstone but before teardown finished would
                # leave a surviving child process EXCLUDED from orphan
                # reconciliation — invisible and never reaped. The wait is
                # bounded because the caller's teardown is itself bounded
                # (_RESET_TIMEOUT then SIGKILL) and runs in a `finally`, so the
                # event is set even when the caller is cancelled.
                if teardown_done is not None and not teardown_done.is_set():
                    try:
                        await asyncio.wait_for(teardown_done.wait(), timeout=_RESET_TIMEOUT + 30)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Subagent %s: teardown did not complete before the "
                            "delivered tombstone; writing it anyway",
                            info.id,
                        )
                # Retain result.txt for a TTL grace window instead of deleting
                # it now, so the parent can read the full transcript
                # (spawn_status / read / grep) after the completion event. A
                # "delivered" tombstone excludes it from orphan reconciliation;
                # the reaper prunes it after agent.subagent_result_ttl_secs.
                try:
                    await asyncio.to_thread(mark_delivered, info.id)
                except Exception:
                    logger.debug("Failed to mark subagent %s delivered", info.id, exc_info=True)
                # Clean up workspace result file (agent-{id}.md in parent dir).
                # The directory is named after the parent's SLOT, which a
                # channel-born parent has while its session key stays the
                # channel's own; without a tab there is no directory to clean.
                try:
                    # Lazy: the dashboard layer must not be imported by a core
                    # module at import time.
                    from kiro_crew.dashboard.chat_utils import dashboard_slot_key

                    slot_key = dashboard_slot_key(info.parent_session_key)
                    if slot_key:
                        _ws_result_path(slot_key, info.id).unlink(missing_ok=True)
                except Exception:
                    logger.debug("Failed to clean workspace result for %s", info.id, exc_info=True)
        except asyncio.TimeoutError:
            logger.error(
                "%s: completion injection timed out for %s after %.0fs",
                source,
                info.id,
                _ON_DONE_TIMEOUT,
            )
            # Kill the parent session's kiro-cli process so the next agent's
            # injection gets a clean provider instead of hitting "Prompt already
            # in progress" on the stuck one.
            try:
                await self._manager._sessions.reset(info.parent_session_key)
            except Exception:
                logger.debug(
                    "Failed to reset parent session %s after injection timeout",
                    info.parent_session_key,
                    exc_info=True,
                )
            self._manager.notify_injection_failed(info, reason=injection_timeout_reason)
        except Exception:
            logger.exception("%s: announce failed for %s", source, info.id)

    async def _run_terminal_report_impl(
        self,
        info: SubagentInfo,
        *,
        source: str,
        injection_timeout_reason: str,
        mark_delivered_on_success: bool,
        settle_digest: bool = False,
        teardown_done: "asyncio.Event | None" = None,
    ) -> None:
        """Spawn the shielded terminal report and block until it completes.

        Convenience for callers that have no cancellable ``await`` between
        taking the claim and reporting (``_force_reap``): there is no window in
        which a cancellation could strand the outcome before the report task
        exists, so spawning and awaiting can be adjacent. Callers that DO have a
        teardown ``await`` between the claim and the report (``_run``'s
        ``finally``) must instead :meth:`_spawn_terminal_report` BEFORE that
        await and :meth:`_await_report` after, so the report task is already
        live (and shielded) no matter where the cancellation lands.
        """
        await self._manager._await_report(
            self._manager._spawn_terminal_report(
                info,
                source=source,
                injection_timeout_reason=injection_timeout_reason,
                mark_delivered_on_success=mark_delivered_on_success,
                settle_digest=settle_digest,
                teardown_done=teardown_done,
            )
        )

    def _spawn_terminal_report_impl(
        self,
        info: SubagentInfo,
        *,
        source: str,
        injection_timeout_reason: str,
        mark_delivered_on_success: bool,
        settle_digest: bool = False,
        teardown_done: "asyncio.Event | None" = None,
    ) -> "asyncio.Task":  # type: ignore[type-arg]
        """Launch :meth:`_report_terminal` on a strongly-referenced task.

        Returns immediately (no ``await``) so the caller can start the report
        BEFORE its own teardown awaits, guaranteeing the report exists and is
        held alive independently of the caller's fate. The task is retained in
        ``self._report_tasks`` (so it cannot be garbage-collected while its
        awaiter is cancelled, and so ``cancel_all()`` can drain it) and
        self-removes on completion.
        """
        return self._manager._lifecycle.spawn_report(
            info,
            lambda: self._manager._report_terminal(
                info,
                source=source,
                injection_timeout_reason=injection_timeout_reason,
                mark_delivered_on_success=mark_delivered_on_success,
                settle_digest=settle_digest,
                teardown_done=teardown_done,
            ),
        )

    def _release_slot_impl(self, info: SubagentInfo) -> bool:
        """Claim the exclusive right to free ``info``'s concurrency slot.

        Returns True for exactly one caller; that caller decrements
        ``_running_count`` once and drains the queue. Contains no ``await``, so
        the check-and-set is atomic with respect to other tasks on the loop.

        Why this is its OWN token rather than a side effect of ``done`` or
        ``reaped``: both terminal paths (`_force_reap` and `_run`'s ``finally``)
        can run for the same agent, and previous revisions inferred slot
        ownership from whichever flag happened to be set. That produced a double
        decrement in one interleaving and — after the flag order was changed to
        fix a delivery bug — no decrement at all in another, inflating
        ``_running_count`` and permanently starving the spawn queue. An explicit
        one-shot token makes the count independent of report and record ordering.

        Recovery uses the scheduler's atomic capacity check and reoccupation
        after the interrupted run's ``finally`` has released its old slot.
        """
        return self._manager._scheduler.claim_release(info)

    async def _force_reap_impl(
        self, agent_id: str, info: SubagentInfo, elapsed: float, *, reason: str = ""
    ) -> None:
        """Kill a subagent's session process and mark it done."""
        session_key = f"subagent:{agent_id}"

        # Reap-in-flight marker + recovery cancel BEFORE ANY await in this
        # method. Both used to sit after the session teardown below, which yields
        # (bounded by _RESET_TIMEOUT, longer still on the SIGKILL path). A
        # cancel-recovery task whose bounded handshake expired inside that window
        # respawned the very run being killed — tools executing after a user
        # Stop, strictly worse than a duplicate report. Note this sets
        # `_reap_started`, NOT `reaped`: setting `reaped` this early makes a run
        # woken by our own session reset skip its error synthesis and report a
        # false SUCCESS before we own the record. See `_reap_started`.
        self._manager._lifecycle.begin_reap(info)
        # A pending cancel-recovery respawn is moot — this agent is being killed.
        # Cancel it rather than letting it sit in its bounded handshake wait
        # (_RESET_TIMEOUT + 60s) only to discover `reaped` and bare-return.
        # The reap owns the terminal report from here (see the claim below).
        recovery_task = self._manager._tasks.pop(f"{agent_id}:recovery", None)
        if recovery_task and not recovery_task.done():
            recovery_task.cancel()

        if info._session_sharing:
            # Session-sharing subagent: NEVER SIGKILL the shared runtime —
            # the parent session owns it and other co-tenants may be active.
            # Conservative approach: shut down only this subagent's provider
            # handle, leaving the shared runtime intact.
            runtime_pid = info._pid
            logger.info(
                "Reaper: conservative shutdown for session-sharing %s — "
                "runtime pid=%s kept alive (shared runtime, never SIGKILL)",
                agent_id,
                runtime_pid,
            )
            try:
                sel().log_tool_invocation(
                    session_key=session_key,
                    source="subagent",
                    tool_name="smart_hard_kill",
                    outcome="conservative-shutdown",
                    resources=f"runtime_pid={runtime_pid}",
                    metadata={
                        "subagent_id": agent_id,
                        "runtime_pid": runtime_pid,
                        "decision": "session-sharing-never-kill",
                    },
                )
            except Exception:
                logger.debug("SEL audit for conservative shutdown failed", exc_info=True)
            # Shutdown the shared provider handle only
            try:
                if info._shared_provider:
                    await info._shared_provider.shutdown()
            except Exception:
                logger.debug(
                    "Reaper: shared session shutdown failed for %s", agent_id, exc_info=True
                )
        else:
            # Kill the process FIRST so the pipe unblocks, then cancel the task.
            try:
                await asyncio.wait_for(
                    self._manager._sessions.reset(session_key), timeout=_RESET_TIMEOUT
                )
            except asyncio.TimeoutError:
                logger.warning("Reaper: reset hung for %s, attempting SIGKILL", agent_id)
                await self._manager._sigkill_session(session_key)
            except Exception:
                logger.exception("Reaper: reset failed for %s", agent_id)

        # Snapshot "parked on a never-answered spawn approval" BEFORE the
        # intentional cancel below, because the flag's owner clears it in a
        # `finally` that the cancel schedules. Reading it at the record site
        # instead would be correct only while no `await` sits between the cancel
        # and that site — an invariant nothing enforces, and breaking it would
        # silently restore the misleading deadline message. Both conjuncts are
        # load-bearing: run.py also sets `_awaiting_approval` for mid-run TOOL
        # prompts, where `_exec_started` is already set, so `_exec_started is
        # None` is what distinguishes "never started" from "was running".
        approval_parked = info._awaiting_approval and info._exec_started is None

        task = self._manager._tasks.pop(agent_id, None)
        if task and not task.done():
            # `reaped` is set HERE — late, immediately before the intentional
            # cancel — not at the top of the method. Late enough that a run woken
            # by the session reset above still synthesizes its own error (a run
            # that sees `reaped` skips error synthesis, and reporting with no
            # error set delivers a false success). Early enough to satisfy the
            # intentional-cancel contract: visible when the task's
            # CancelledError arm runs. The recovery scheduler reads the earlier
            # `_reap_started` instead, so it is not affected by this placement.
            self._manager._lifecycle.mark_reaped(info)
            self._manager._cancel_task_intentionally(task, info, reason=reason or "reaped")

        # No live task to cancel above (already exited) — the reap still owns
        # teardown bookkeeping from here, so mark it now.
        self._manager._lifecycle.mark_reaped(info)
        # Guard 1 of 3 — the terminal RECORD (done/error/stat/tombstone/cost) is
        # first-arrival-wins on `info.done`, so it is never written twice.
        if self._manager._lifecycle.claim_record(info):
            if not info.error and not info.user_stopped:
                # A user stop is neutral — never synthesize a reap error for it.
                if approval_parked:
                    # Approval-parked reap: the run never began execution — it sat
                    # registered behind an unanswered spawn approval and the
                    # reaper's wall clock fired before the (longer) approval window
                    # closed. It reached no execution deadline, so DO NOT frame it
                    # as one. Predicate captured above the cancel; see there.
                    info.error = f"Reaped after {int(elapsed)}s while still awaiting an unanswered spawn approval (never started) [{_timeout_context(info, include_elapsed=False, turn_limit=self._manager._effective_turn_limit(info))}]"
                elif reason == "startup_timeout":
                    info.error = f"Failed to start within {self._manager._startup_deadline}s (no runtime launched, no turn produced) [{_timeout_context(info, include_elapsed=False, turn_limit=self._manager._effective_turn_limit(info))}]"
                else:
                    info.error = f"Reaped after {int(elapsed)}s (exceeded {self._manager._default_timeout}s deadline) [{_timeout_context(info, include_elapsed=False, turn_limit=self._manager._effective_turn_limit(info))}]"
            if not info.user_stopped:
                # A user-initiated stop is a neutral outcome, not a failure.
                Stats().inc_subagent_failed()
            self._manager._write_tombstone(info, reason or "reaped")
            self._manager._record_cost(info)
        # Guard 2 of 3 — SLOT accounting, on its own one-shot token and therefore
        # independent of both `done` (above) and `reaped`. A reap/cancel frees a
        # slot but — unlike normal completion — does NOT otherwise pump the queue,
        # so queued spawns would sit stranded until an unrelated agent finished.
        # Drain here so the freed slot is used immediately.
        if self._manager._release_slot(info):
            self._manager._scheduler.running_count = max(
                0, self._manager._scheduler.running_count - 1
            )
            self._manager._drain_queue()

        try:
            sel().log_tool_invocation(
                session_key=session_key,
                source="subagent",
                tool_name="reaper_force_kill",
                outcome="reaped",
                metadata={
                    "subagent_id": agent_id,
                    "session_key": session_key,
                    "elapsed": int(elapsed),
                },
            )
        except Exception:
            logger.exception("Reaper: SEL audit failed for %s", agent_id)

        try:
            # Retain-by-default: the reaped run's session files stay on disk
            # (spawn_continue resume material); the tombstone pruner owns
            # their deletion. A force-reaped long run is exactly the case
            # retention exists for.
            self._manager._sessions.release(session_key, cleanup=False)
        except Exception:
            logger.warning("Reaper: release failed for %s", agent_id, exc_info=True)

        # Guard 3 of 3 — the terminal REPORT (subagent_done + _on_done), owned by
        # the finalize claim. The claim deliberately does NOT consult `info.done`:
        # `_run_inner` may set `done` while the teardown above is suspended, and
        # gating on it made the reaper decline while `_run`'s finally also declined
        # (it sees `reaped`) — so nobody reported. The report runs SHIELDED, so a
        # cancellation landing mid-report (cancel_all during shutdown) still
        # delivers rather than stranding the outcome with the claim consumed.
        info.elapsed = elapsed
        if self._manager._claim_finalize(info, supersede_recovery=True):
            await self._manager._run_terminal_report(
                info,
                source="Reaper",
                injection_timeout_reason=(
                    f"delivery timed out after {int(_ON_DONE_TIMEOUT)}s (reaper)"
                ),
                mark_delivered_on_success=False,
                # This member's own result is NOT marked delivered (it was
                # reaped, not completed) — but if it was the wave member whose
                # `_on_done` flushed the batch digest, its SIBLINGS' successful
                # results HAVE now reached the parent. Settling is about their
                # holds, not this member's outcome, so it must happen on this
                # path too or held siblings stay visible to orphan
                # reconciliation and get spuriously "recovered" after a restart.
                settle_digest=True,
            )

        # Truncate retained text AFTER _on_done to preserve full output for result injection
        if len(info.streaming_text) > 10_000:
            info.streaming_text = info.streaming_text[:10_000] + "\n…(truncated)"

    async def _sigkill_session_impl(self, session_key: str) -> None:
        """Best-effort SIGKILL when graceful reset hangs.

        Uses killpg to kill the entire process group, then sweeps
        escaped children in different PGIDs (MCP servers).

        Async so the Windows ``taskkill`` spawn offloads to
        :func:`kiro_crew.executors.subprocess_executor` via
        :func:`platform_compat.kill_process_tree_async` / ``kill_pid_async``
        instead of blocking the reaper loop's event loop for the duration of
        ``taskkill.exe``.
        """
        try:
            (
                _capture_child_records,
                _get_child_pids,
                _kill_escaped_children,
                _is_our_child,
            ) = _load_child_process_helpers()

            session = self._manager._sessions._sessions.get(session_key)
            if not session:
                return
            client = getattr(session.provider, "_client", None)
            raw_pid = getattr(client, "_pid", None) if client else None
            pid = raw_pid if isinstance(raw_pid, int) else None
            if not pid:
                return
            # Snapshot child tree before killing — children in different
            # PGIDs survive killpg. macOS pgrep/ps spawns are offloaded to
            # subprocess_executor to keep the reaper loop responsive
            loop = asyncio.get_running_loop()
            raw_children = getattr(client, "_child_pids", None)
            child_pids: dict = dict(raw_children) if isinstance(raw_children, dict) else {}
            fresh = await loop.run_in_executor(subprocess_executor(), _get_child_pids, pid)
            new_pids = [p for p in fresh if p not in child_pids]
            if new_pids:
                child_pids.update(
                    await loop.run_in_executor(
                        subprocess_executor(), _capture_child_records, new_pids
                    )
                )
            # Validate PID hasn't been recycled before killing.
            original_start = getattr(client, "_start_time", None)
            if original_start is None:
                logger.debug("Reaper: PID %d already dead for %s", pid, session_key)
                await loop.run_in_executor(
                    subprocess_executor(), _kill_escaped_children, child_pids
                )
                return
            if not await loop.run_in_executor(
                subprocess_executor(), _is_our_child, pid, original_start
            ):
                logger.warning("Reaper: PID %d recycled for %s, skipping killpg", pid, session_key)
                stored = dict(raw_children) if isinstance(raw_children, dict) else {}
                await loop.run_in_executor(subprocess_executor(), _kill_escaped_children, stored)
                return
            # Kill the entire process group first
            logger.warning(
                "Reaper: killpg for PID %d (%d children) for %s",
                pid,
                len(child_pids),
                session_key,
            )
            try:
                # Async variants offload Windows taskkill to
                # subprocess_executor so the reaper loop never blocks the
                # event loop on taskkill.exe.
                await platform_compat.kill_process_tree_async(pid, platform_compat.SIGKILL)
            except ValueError:
                # Guard refused the pid outright (non-int/reserved) — nothing
                # safe to signal. Mirrors CronService._sigkill_session so a
                # broadcast-guard refusal is a clean log line, not the noisy
                # generic `except Exception` traceback below.
                logger.error("Reaper: kill guard refused pid %r for %s", pid, session_key)
            except (ProcessLookupError, OSError):
                try:
                    await platform_compat.kill_pid_async(pid, platform_compat.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            # Sweep children that escaped to different PGIDs
            await loop.run_in_executor(subprocess_executor(), _kill_escaped_children, child_pids)
        except Exception:
            logger.exception("Reaper: SIGKILL failed for %s", session_key)

    def notify_injection_failed_impl(
        self, info: SubagentInfo, reason: str = "delivery timed out"
    ) -> None:
        """Notify UI and queue failure for LLM when injection times out.

        Appends a synthetic error to the dashboard slot (UI) and queues a
        failure message into ``slot._pending_subagent_failures`` so the LLM
        learns about the failure on the next ``_run_chat`` turn and can read
        the result from disk if needed. The notice's outcome line is derived
        from the record (:func:`_injection_notice_outcome`) rather than
        asserting completion: this path fires for every terminal state whose
        report could not be injected, including runs cancelled or rejected
        before they ever executed.
        """
        info._delivery_failed = True
        try:
            # Lazy: the dashboard layer must not be imported by a core module at
            # import time.
            from kiro_crew.dashboard.chat_utils import dashboard_slot_key

            # The failure is queued into a SLOT, so the gate is whether the
            # parent has a tab — true for a channel-born parent whose session
            # key is the channel's own. Without one there is nothing to append
            # to and nothing to drain on the next turn.
            slot_name = dashboard_slot_key(info.parent_session_key)
            if not slot_name:
                return

            # Build failure message the LLM will see on next turn
            task_preview = _redact((info.task or "")[:100])
            result_hint = ""
            if info.result_path:
                try:
                    size = os.path.getsize(info.result_path)
                    size_str = f"{size:,} bytes"
                except OSError:
                    size_str = ""
                result_hint = (
                    f"\nResult saved at: {info.result_path}"
                    + (f" ({size_str})" if size_str else "")
                    + "\nUse the read tool to retrieve it if needed."
                )
            failure_msg = (
                f"{SUBAGENT_COMPLETION_PREFIX}\n"
                f"Agent `{info.id}` ❌ {reason}\n"
                f"Task: {task_preview}\n"
                f"{_injection_notice_outcome(info)}{result_hint}"
            )

            # Queue for LLM context drain on next _run_chat
            if self._manager._on_event:
                _task = asyncio.ensure_future(
                    self._manager._fire_event(
                        "subagent_injection_failed",
                        info,
                        {
                            "error": reason,
                            "slot": slot_name,
                            "failure_msg": failure_msg,
                        },
                    )
                )
                _task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        except Exception:
            logger.debug("notify_injection_failed failed for %s", info.id, exc_info=True)
