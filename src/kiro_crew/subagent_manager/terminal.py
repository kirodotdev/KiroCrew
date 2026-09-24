"""Terminal behavior for the SubagentManager facade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._component import ManagerComponent

if TYPE_CHECKING:
    from ..subagent import (
        _ON_DONE_TIMEOUT,
        _RESET_TIMEOUT,
        MAX_ERROR_DETAIL_LEN,
        SUBAGENT_COMPLETION_PREFIX,
        Mapping,
        ProcessHandle,
        Stats,
        SubagentInfo,
        _done_result,
        _injection_notice_outcome,
        _redact,
        _timeout_context,
        _ws_result_path,
        asyncio,
        child_process_helpers,
        ending_fence,
        failure_name,
        join_failures,
        kill_each,
        kill_set,
        kill_verified_process,
        logger,
        mark_delivered,
        os,
        process_handle_of,
        process_survived_async,
        sel,
        spawn_in_flight,
        teardown_capture,
        with_kill_failure,
    )


class TerminalCoordinator(ManagerComponent):
    """Own terminal transitions while state remains facade-owned."""

    __slots__ = ()

    def _record_crew_log_terminal(self, info: SubagentInfo) -> None:
        """Close *info*'s entry in the PARENT session's crew log, once.

        Called from the exclusive one-shot terminal report, so a child cannot be
        closed twice however the race between the reaper and ``_run``'s ``finally``
        resolves.

        The closer is chosen from the runtime's own three-way ``outcome`` and never
        re-derived from error-nullability: ``completed`` closes as a completion, and
        ``stopped`` and ``failed`` both close through ``subagent/failed`` carrying
        which one it was. A stop is not a success and must not read as one, and it
        is not an error either.

        The parent session and the turn that asked are read back from the origin
        pinned at the dispatch, and released here -- the parent is very likely on a
        different turn by now, and asking which one would file this outcome under a
        turn that did not cause it. An unknown origin means the dispatch was never
        recorded (the flag was off then, or the parent could not be resolved), and
        the emitter's empty-session-id no-op drops the closer rather than inventing
        an opener for it.

        Every name is imported inside the body: this method does not end in
        ``_impl``, so it keeps this module's globals, where the facade's imports
        exist only under ``TYPE_CHECKING``.
        """
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.subagent import logger as _logger

        try:
            if not crew_log_emit.enabled():
                return
            sid, _asking_turn = crew_log_emit.child_origin(info.id)
            if not sid:
                # Pinned but never opened -- a spawn the approval gate declined.
                # It closes nothing, and the pin is dropped here rather than left
                # for the FIFO to evict.
                crew_log_emit.forget_child_origin(info.id)
                return
            # The pin is READ here and released in the finally, after the entry is
            # handed to the writer. It is the only thing covering the gap the
            # normal path opens: `done` flips in the run loop, which drops the
            # child from the manager's running set, and this method runs later
            # from the report task -- so between them the child is neither running
            # nor owed, and a repair reading there would close it as `unknown`
            # ahead of the outcome below. Releasing after the handover means the
            # writer's debt takes over from the pin with no instant in between.
            # Reporting stays one-shot without the pop: every route here is gated
            # on `_claim_finalize`, which hands out one token.
            elapsed_ms = int(max(0.0, float(info.elapsed or 0.0)) * 1000)
            outcome = info.outcome
            if outcome == "completed":
                crew_log_emit.on_subagent_completed(sid, agent_id=info.id, duration_ms=elapsed_ms)
            else:
                crew_log_emit.on_subagent_failed(
                    sid,
                    agent_id=info.id,
                    reason=info.error or "",
                    outcome=outcome,
                    duration_ms=elapsed_ms,
                )
        except Exception:
            _logger.debug("crew log: closing a subagent entry failed", exc_info=True)
        finally:
            # Unconditional: a pin this method fails to release is a child the
            # repair would treat as live forever.
            try:
                crew_log_emit.forget_child_origin(info.id)
            except Exception:
                _logger.debug("crew log: releasing a child origin failed", exc_info=True)

    def _claim_finalize_impl(self, info: SubagentInfo, *, supersede_recovery: bool = False) -> bool:
        """Claim the exclusive right to report ``info``'s terminal outcome.

        Returns True for exactly one caller. Both the reap path and ``_run``'s
        ``finally`` call this and report only if it returns True, so the parent
        is notified exactly once no matter which wins the race or whether the
        loser is cancelled part-way through its teardown. One exception: a run
        whose stream died under a reap's own reset (the reap-echo arm) does not
        claim at all -- the reap does, after its fallback kill has decided, so
        the report it publishes carries a kill that failed (see ``_run``).

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
        if info._recovering and not supersede_recovery:
            return False
        if info._finalized:
            return False
        if info._recovering:
            # A terminal reap/stop SUPERSEDES a pending cancel-recovery respawn:
            # the agent is being killed, so there is nothing left to respawn.
            # Clearing the flag here is what keeps `False` from meaning two
            # different things to this caller ("someone else already reported"
            # vs "withheld for a respawn that will report later") — the exact
            # conflation this token exists to remove.
            info._recovering = False
        info._finalized = True
        return True

    async def _report_terminal_impl(
        self,
        info: SubagentInfo,
        *,
        source: str,
        injection_timeout_reason: str,
        mark_delivered_on_success: bool,
        settle_digest: bool = False,
        teardown_done: "asyncio.Event | None" = None,
        gate: "asyncio.Future[bool] | None" = None,
    ) -> bool:
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

        ``gate`` is for a caller that spawns this report BEFORE the record is
        final -- ``_force_reap``, ahead of the reset and kill awaits it may be
        cancelled at -- so the task exists, strongly held and drained by
        ``cancel_all()``, before any point the caller can be cancelled at. The
        report waits on it: ``True`` once the caller holds the finalize claim
        and the record is written, and the payload is built from ``info`` only
        then; ``False`` when the claim went to another path, which reports, and
        this task returns without a word (its caller disowns it from
        ``_report_owners`` before releasing it, so its exit latches nothing).

        The two call sites (reap vs. ``_run``'s ``finally``) differ only in the
        injection-timeout reason string, the log ``source`` prefix, and whether
        a successful delivery marks the result delivered — those are passed as
        arguments rather than unified away. The WS payload is identical (both
        set ``info.elapsed`` before calling), so it is built from ``info`` here.
        """
        if gate is not None and not await gate:
            return True
        # A queued synthetic terminal is registered before all sibling reports
        # are scheduled, with ``done=False`` as a batch-completion hold. The
        # exclusive report task owns the terminal transition; flipping here
        # means only the last sibling can observe the batch as fully settled.
        #
        # The crew log entry is handed over before this flip, but that order is
        # NOT what protects the log, and reading it that way was wrong: on the
        # normal completion path the run loop has already set `done` well before
        # this method runs, so by here the flip is a no-op re-flip and the child
        # left the manager's running set long ago. What covers that gap is the
        # child's origin pin, which `_record_crew_log_terminal` holds until the
        # closer is handed to the writer. This flip stays ahead of the event for
        # the paths that reach a terminal without the run loop.
        self._record_crew_log_terminal(info)
        info.done = True
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
                # The model actually served. By the terminal
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
                # WHY the run ended and whether ``result`` is a partial, so the
                # parent does not infer success from ``error`` being unset.
                "stop_reason": info.stop_reason,
                "stop_class": info.stop_class,
                "partial": info.partial,
            },
        )
        if not self._manager._on_done:
            return True
        if info.id in getattr(self._manager, "_teardown_cancelled_ids", ()):
            # The parent this would report to has been retired. ``_on_done``
            # resolves the parent key through the session registry and injects,
            # which CREATES a session when none is live — so delivering here
            # rebuilds the conversation the teardown just took down and seeds it
            # with a retired run's terminal text. The ``subagent_done`` event above
            # has already gone out, so a dashboard watching the card still sees it
            # end; what is skipped is the injection into a conversation that is
            # over. The run's own result file and tombstone are unaffected.
            # Releasing the hold is part of the same statement. A wave member parks
            # its siblings' announces on its own digest (``_digest_held_at``,
            # ``_digest_settle_ids``), and the reaper's hold-expiry sweep arms a
            # ``force_digest_flush`` for a batch whose hold has aged out. That flush
            # builds a SYNTHETIC record with a fresh id, so the gate above can never
            # match it: it would reach ``_on_done`` on its own and rebuild the retired
            # parent's conversation minutes after this skip. Dropping this run out of
            # the hold, and marking the siblings it was holding, leaves no injector
            # armed for the wave. The siblings are NOT marked delivered -- their results
            # never reached a parent, so orphan reconciliation must still be able to
            # find them.
            info._digest_held_at = 0.0
            held, info._digest_settle_ids = info._digest_settle_ids, []
            if held:
                self._manager._teardown_cancelled_ids.update(held)
            logger.info("Reaper: skipping parent delivery for %s — its parent ended", info.id)
            # The gate has now done its job for this run: the delivery it existed to stop
            # has been stopped, and ``_on_done`` was never called, so none of the gateway's
            # injection paths can fire for it either. Discarding here is what keeps the gate
            # from depending on its age backstop in the ordinary case -- an id is retained
            # until the run's delivery is actually suppressed rather than for a fixed span.
            self._manager._teardown_cancelled_ids.discard(info.id)
            return True
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
                self._manager._settle_digest_holds(info)
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
                    mark_delivered(info.id)
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
            return True
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
            return False
        except Exception:
            logger.exception("%s: announce failed for %s", source, info.id)
            return False

    async def _run_terminal_report_impl(
        self,
        info: SubagentInfo,
        *,
        source: str,
        injection_timeout_reason: str,
        mark_delivered_on_success: bool,
        settle_digest: bool = False,
        teardown_done: "asyncio.Event | None" = None,
    ) -> bool:
        """Spawn the shielded terminal report and block until it completes.

        Convenience for callers that have no cancellable ``await`` between
        taking the claim and reporting (the cancel-recovery failure arm, the
        boundary redeliveries): there is no window in which a cancellation
        could strand the outcome before the report task exists, so spawning and
        awaiting can be adjacent. Callers that DO have awaits between the claim
        and the report must instead :meth:`_spawn_terminal_report` BEFORE those
        awaits and :meth:`_await_report` after, so the report task is already
        live (and shielded) no matter where the cancellation lands: ``_run``'s
        ``finally`` spawns ahead of its session teardown, and ``_force_reap``
        spawns ahead of its reset and kill, gated until its record is final.
        """
        return await self._manager._await_report(
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
        gate: "asyncio.Future[bool] | None" = None,
    ) -> "asyncio.Task[bool]":
        """Launch :meth:`_report_terminal` on a strongly-referenced task.

        Returns immediately (no ``await``) so the caller can start the report
        BEFORE its own teardown awaits, guaranteeing the report exists and is
        held alive independently of the caller's fate. The task is retained in
        ``self._report_tasks`` (so it cannot be garbage-collected while its
        awaiter is cancelled, and so ``cancel_all()`` can drain it) and
        self-removes on completion. ``gate`` (see :meth:`_report_terminal`)
        lets a caller spawn before its record is final and release the report,
        or dismiss it, once it knows.
        """
        task = asyncio.create_task(
            self._manager._report_terminal(
                info,
                source=source,
                injection_timeout_reason=injection_timeout_reason,
                mark_delivered_on_success=mark_delivered_on_success,
                settle_digest=settle_digest,
                teardown_done=teardown_done,
                gate=gate,
            )
        )
        self._manager._report_tasks.add(task)
        # Owner map so `cancel_all()` can identify WHOSE outcome it is about to
        # abandon (and re-admit it to orphan recovery). Kept alongside the set
        # rather than replacing it: `_report_tasks` is the strong reference that
        # keeps the task alive, and both are cleared by the one done callback.
        self._manager._report_owners[task] = info

        def _forget(t: "asyncio.Task") -> None:  # type: ignore[type-arg]
            self._manager._report_tasks.discard(t)
            owner = self._manager._report_owners.pop(t, None)
            self._manager._run_events._forget_finished_live_state(info)
            if owner is None:
                return
            failed = t.cancelled()
            if not failed:
                try:
                    failed = t.result() is False
                except asyncio.CancelledError:
                    failed = True
                except Exception:
                    failed = True
            if failed:
                self._manager._latch_report_failure(owner)
            else:
                self._manager._clear_report_failure(owner)

        task.add_done_callback(_forget)
        return task

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

        Note the recovery respawn's own ``_running_count += 1`` re-admit is
        unaffected: it runs after the interrupted run's ``finally`` has already
        released, and this token is per-``SubagentInfo``.
        """
        if info._slot_released:
            return False
        info._slot_released = True
        return True

    async def _force_reap_impl(
        self, agent_id: str, info: SubagentInfo, elapsed: float, *, reason: str = ""
    ) -> None:
        """Kill a subagent's session process and mark it done -- once, however many stops ask.

        A dashboard Stop racing a deadline reap (or a parent-end cancel racing
        either) ran two reaps over one run: both retained handles, both reset,
        both killed, and when the kill failed both appended the ``; kill
        failed: …`` suffix to the persisted record while only the first
        published a report -- the record on disk and the completion the parent
        received disagreed. The first caller runs the reap (``_reap_once``);
        a caller arriving while it is in flight joins it and returns once the
        record is final -- written, audited, the report released -- not once
        that report is delivered (``_reap_once`` settles the join there; the
        delivery, capped at ``_ON_DONE_TIMEOUT``, is the first caller's alone
        to wait on), so the kill is decided once, recorded once and
        reported once. The join is shielded: a joiner cancelled mid-wait (a
        Stop request whose client went away) leaves the reap it joined
        untouched. A caller arriving after the reap has finished finds the run
        reaped and does nothing.
        """
        in_flight = self._manager._reaps_in_flight.get(agent_id)
        if in_flight is not None:
            logger.info(
                "Reaper: %s is already being reaped; joining that reap (%s)",
                agent_id,
                reason or "deadline",
            )
            await asyncio.shield(in_flight)
            return
        if info._reap_started:
            return
        settled: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._manager._reaps_in_flight[agent_id] = settled
        try:
            await self._manager._reap_once(agent_id, info, elapsed, reason=reason)
        finally:
            # Settled on every exit -- return, raise, or the cancellation the
            # arm below re-raises -- so a joiner never waits on a reap that
            # is gone.
            self._manager._reaps_in_flight.pop(agent_id, None)
            if not settled.done():
                settled.set_result(None)

    async def _reap_once_impl(
        self, agent_id: str, info: SubagentInfo, elapsed: float, *, reason: str = ""
    ) -> None:
        """The one reap of a run: teardown, record, audit, report. Entered via ``_force_reap``."""
        # The key the run's session is REGISTERED under -- the same derivation
        # as ``_run`` and ``_teardown_run_session``. A continuation
        # (``spawn_continue``) is a new run id on the ORIGINAL run's
        # conversation key, so ``subagent:<agent_id>`` would name a session
        # that is not there: a reset of it stops nothing, the retain finds no
        # handle, the fallback has nothing to signal, and the release leaves
        # the conversation's lease held -- all audited ``reaped``.
        session_key = info.conversation_key or f"subagent:{agent_id}"

        # Reap-in-flight marker + recovery cancel BEFORE ANY await in this
        # method. The session teardown below yields (bounded by _RESET_TIMEOUT,
        # longer still on the SIGKILL path). If they sat after it, a
        # cancel-recovery task whose bounded handshake expired inside that window
        # would respawn the very run being killed — tools executing after a user
        # Stop, strictly worse than a duplicate report. Note this sets
        # `_reap_started`, NOT `reaped`: setting `reaped` this early makes a run
        # woken by our own session reset skip its error synthesis and report a
        # false SUCCESS before we own the record. See `_reap_started`.
        info._reap_started = True
        # Written next to the marker, for the run loop: the session teardown
        # below poisons the run's stream, which raises ``AcpProcessDied`` inside
        # ``_run`` before this method's own record is written. ``_run`` reads
        # these to record the reap that caused the death rather than the death
        # itself. ``_stop_origin`` is left alone when a cancel already named
        # one ("stopped by user", a parent-end verb).
        if not info._reap_reason:
            info._reap_reason = reason or "reaped"
        if not info._stop_origin:
            info._stop_origin = f"reaped after {int(elapsed)}s ({reason or 'deadline'})"
        # A pending cancel-recovery respawn is moot — this agent is being killed.
        # Cancel it rather than letting it sit in its bounded handshake wait
        # (_RESET_TIMEOUT + 60s) only to discover `reaped` and bare-return.
        # The reap owns the terminal report from here (see the claim below).
        recovery_task = self._manager._tasks.pop(f"{agent_id}:recovery", None)
        if recovery_task and not recovery_task.done():
            recovery_task.cancel()

        # Guard 3 of 3 -- the terminal REPORT (subagent_done + _on_done) -- is
        # LAUNCHED HERE, before any await this method can be cancelled at, and
        # PUBLISHED at the end, once the record is final. The two halves are
        # what make the outcome safe on both sides of a cancellation:
        #
        # * A gateway shutdown runs ``cancel_all()``, which cancels the reaper
        #   task while this method awaits the reset or the fallback kill after
        #   it (a user Stop's own task can be cancelled the same way). By then
        #   the reset has usually already killed the run's runtime, so the run's
        #   reap-echo arm has written the record and the tombstone and LEFT THE
        #   REPORT TO THIS REAP (see ``_run``). A report launched only after the
        #   window was a report no cancellation point inside the window could
        #   reach: nobody reported, the parent never received the completion,
        #   and the tombstone already on disk excluded the folder from the next
        #   start's orphan recovery, so the outcome was lost for good. Launched
        #   here, the task exists -- strongly held, in ``_report_tasks``, drained
        #   by ``cancel_all()`` and re-admitted to orphan recovery if that drain
        #   has to abandon it -- before the first point the reap can be cut at.
        # * It waits on ``report_gate`` and builds its payload only when the
        #   gate is released, so it cannot tell the parent the run was reaped
        #   before the kill has decided; the tail below releases it after the
        #   record (kill failure appended, tombstone written) and the claim.
        #   ``False`` dismisses it: another path reported (the run finished on
        #   its own inside the window), exactly as the late claim decided before.
        report_gate: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        report_task = self._manager._spawn_terminal_report(
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
            gate=report_gate,
        )

        # What the fallback could not do, named for the record and the audit.
        # ``_sigkill_session`` raises nothing -- the reap must still finish
        # the teardown it owns -- but it REPORTS a refused or failed signal as
        # its result, so a process it left alive is never audited ``reaped``.
        kill_failed: str | None = None
        # The cancellation that cut the teardown short, if one did. The reap
        # still owes the record, the audit and the release of the report above
        # (a cancellation let straight out would leave the launched report
        # waiting on a gate nobody releases, until the shutdown drain abandons
        # it); the cancellation is re-raised once those are done. The kill it
        # did not finish is undecided, and undecided is recorded as a failure
        # the record names (``…; kill failed: CancelledError: …``), never as
        # ``reaped``: the process may well be alive.
        interrupted: asyncio.CancelledError | None = None
        # The key's ENDING FENCE (``SessionManager.ending_key``, the cron
        # reaper's shape), raised HERE -- synchronously, before the first await
        # of the teardown -- and held through the passes, the terminal record,
        # the audit and the release below; it lifts when the block ends, however
        # it ends. While it is up, a claim or a new allocation under the key --
        # a queued ``spawn_continue`` cold-starting the conversation key, a
        # parent's turn re-claiming it -- is HELD at the door of
        # ``get_or_create`` and lands only once the run is recorded, and an
        # allocation already in flight when it went up -- a cold start caught
        # inside ``provider.start()``, which has published nothing any snapshot
        # below could see -- is invalidated: refused at registration when its
        # start returns, its provider hard-killed by the allocation path, its
        # call allocating again after the lift. Without the fence that start
        # registered after the passes and ran on, holding its turn permit,
        # behind a record that said ``reaped`` -- and nothing reclaimed it:
        # the run's own teardown is skipped once ``reaped`` is set, and the
        # idle sweep skips a session whose semaphore is held. The startup-stall
        # reap fires exactly while such a start is in flight. A session manager
        # without the fence (a test double) is not fenced: the passes and their
        # post-pass read are the whole answer.
        with ending_fence(self._manager._sessions, session_key):
            try:
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
                            "Reaper: shared session shutdown failed for %s",
                            agent_id,
                            exc_info=True,
                        )
                else:
                    # Kill the process FIRST so the pipe unblocks, then cancel the task.
                    # This order is load-bearing for ``_run``'s reap-echo arm: the run's
                    # stream observes this teardown as ``AcpProcessDied`` before the
                    # cancel lands, and the arm reads ``_reap_started`` to record the
                    # stop instead of that death. Reordering these would not make the
                    # arm wrong, only unreachable -- the cancel's own path already
                    # records a stop -- so the arm and this order stand or fall together.
                    #
                    # Taken BEFORE the reset: the reset pops the session from the map
                    # before it can hang, so a kill that looks the key up afterwards
                    # finds nothing and leaves the process it names running. Every
                    # process the key names now is a candidate: the handle the run's
                    # own ``finally`` retained before ITS reset (the common shape: the
                    # run's teardown is the reset that hangs, and this reap is what has
                    # to act on it), a session another path is tearing down, and a
                    # session still live under the key -- the run's own, or a successor
                    # this reset pops too. Each is verified and killed on its own handle.
                    handles = self._manager._retain_process_handles(agent_id, session_key)
                    # The reset runs under a scope whose hook takes the handle of the
                    # exact session it pops: a cold start can register a successor
                    # under the key between the snapshot above and the pop, and it is
                    # that session the reset then pops and hangs on. Its handle joins
                    # the kill set; a popped session with no pid is a kill failure.
                    reset_kwargs, popped = teardown_capture(self._manager._sessions)
                    try:
                        await asyncio.wait_for(
                            self._manager._sessions.reset(session_key, **reset_kwargs),
                            timeout=_RESET_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        logger.warning("Reaper: reset hung for %s, attempting SIGKILL", agent_id)
                        targets, missing = kill_set(handles, popped)
                        kill_failed = join_failures(
                            await self._manager._sigkill_sessions(session_key, targets), missing
                        )
                    except Exception:
                        logger.exception(
                            "Reaper: reset failed for %s, attempting SIGKILL", agent_id
                        )
                        targets, missing = kill_set(handles, popped)
                        kill_failed = join_failures(
                            await self._manager._sigkill_sessions(session_key, targets), missing
                        )
                    else:
                        # A completed reset -- True, or False for a key the run's own
                        # teardown had already popped -- is not proof the process is
                        # gone: the reset's own shutdown can fail without raising out
                        # of it, and a False one stopped nothing at all. Each handle is
                        # asked instead (pid + recorded start id, and the tree the
                        # leader led); a process still standing gets the fallback, and
                        # what the fallback reports is what the record says. Nothing to
                        # verify without a handle: no session was live under the key
                        # before either reset.
                        targets, missing = kill_set(handles, popped)
                        survivors = [
                            handle for handle in targets if await process_survived_async(handle)
                        ]
                        if survivors:
                            logger.warning(
                                "Reaper: process survived the reset for %s, attempting SIGKILL",
                                agent_id,
                            )
                            kill_failed = await self._manager._sigkill_sessions(
                                session_key, survivors
                            )
                        kill_failed = join_failures(kill_failed, missing)
                    # Read after the passes, fence still up: a cold start under the
                    # key that was past its spawn door -- inside ``provider.start()``
                    # with nothing published for the snapshot to see, or already
                    # refused at registration during the passes and hard-killed there
                    # by the allocation path -- is a process the passes did not
                    # answer. Named, not waited for: the fence has settled what
                    # happens to it, and the record must not say ``reaped`` over it.
                    kill_failed = join_failures(
                        kill_failed, spawn_in_flight(self._manager._sessions, session_key)
                    )
                    # Decided: the handles were consumed by the kill, or the survivor
                    # check found the processes gone. Whatever the run's own teardown
                    # does after the cancel below re-reads nothing here.
                    self._manager._process_handles.pop(agent_id, None)
            except asyncio.CancelledError as exc:
                interrupted = exc
                kill_failed = f"{failure_name(exc)}: the stop was cancelled before its kill decided"
                # Cut short, and nothing re-reads the entry either way: a run whose
                # own teardown still runs after this skips it (``reaped`` is set
                # below), and the run's own ``finally`` pops what it retained itself.
                self._manager._process_handles.pop(agent_id, None)
                logger.warning(
                    "Reaper: the stop of %s was cancelled before its kill decided; "
                    "recording and reporting it before the cancellation goes through",
                    agent_id,
                )

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
            # Same capture for the state right after: approved, and waiting for the
            # pump to meter the start into startup (``_admit_released_start``).
            release_parked = info._start_release is not None and info._exec_started is None

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
                info.reaped = True
                self._manager._cancel_task_intentionally(task, info, reason=reason or "reaped")

            # No live task to cancel above (already exited) — the reap still owns
            # teardown bookkeeping from here, so mark it now.
            info.reaped = True
            # Guard 1 of 3 — the terminal RECORD (done/error/stat/tombstone/cost) is
            # first-arrival-wins on `info.done`, so it is never written twice. The
            # report task already exists (launched above, before the teardown), so a
            # tombstone written here is never a tombstone with no report to deliver
            # the outcome it excludes from orphan recovery.
            if not info.done:
                info.done = True
                # Neutrality follows the FIRST stopper (``stop_is_neutral`` reads
                # ``_reap_reason``): a Stop that arrived while this deadline reap was
                # already tearing the run down does not turn its failure neutral.
                if not info.stop_is_neutral:
                    info.user_stopped = False
                if not info.error and not info.user_stopped:
                    # A user stop is neutral — never synthesize a reap error for it.
                    if approval_parked:
                        # Approval-parked reap: the run never began execution — it sat
                        # registered behind an unanswered spawn approval and the
                        # reaper's wall clock fired before the (longer) approval window
                        # closed. It reached no execution deadline, so DO NOT frame it
                        # as one. Predicate captured above the cancel; see there.
                        info.error = f"Reaped after {int(elapsed)}s while still awaiting an unanswered spawn approval (never started) [{_timeout_context(info, include_elapsed=False, turn_limit=self._manager._effective_turn_limit(info))}]"
                    elif release_parked:
                        info.error = f"Reaped after {int(elapsed)}s while still waiting to be admitted into startup after spawn approval (never started) [{_timeout_context(info, include_elapsed=False, turn_limit=self._manager._effective_turn_limit(info))}]"
                    elif reason == "startup_timeout":
                        info.error = f"Failed to start within {self._manager._startup_deadline}s (no runtime launched, no turn produced) [{_timeout_context(info, include_elapsed=False, turn_limit=self._manager._effective_turn_limit(info))}]"
                    else:
                        info.error = f"Reaped after {int(elapsed)}s (exceeded {self._manager._default_timeout}s deadline) [{_timeout_context(info, include_elapsed=False, turn_limit=self._manager._effective_turn_limit(info))}]"
                if kill_failed is not None:
                    # The caller's error text names what the fallback could not
                    # do, next to the reap that asked for it; ``outcome`` still
                    # follows the stop (a user stop stays ``stopped``), so this
                    # adds the failure to the record rather than substituting it.
                    info.error = with_kill_failure(info.error, kill_failed)
                if not info.user_stopped:
                    # A user-initiated stop is a neutral outcome, not a failure.
                    Stats().inc_subagent_failed()
                self._manager._write_tombstone(info, reason or "reaped")
                self._manager._record_cost(info)
            elif kill_failed is not None and not info._finalized:
                # The run's own arm wrote the record first (its stream died under
                # this teardown), ahead of the kill's decision, and LEFT THE
                # REPORT TO THIS REAP (see ``_run``), so the tombstone it wrote
                # does not know the failure. Append it and re-write the
                # tombstone under the same cause, so the record on disk carries
                # the failure BEFORE the report is released below.
                info.error = with_kill_failure(info.error, kill_failed)
                self._manager._write_tombstone(info, info._reap_reason or "reaped")
            elif kill_failed is not None:
                # ``done`` AND the finalize token are both taken: the run finished
                # on its own inside the reap window -- a result, or an exception
                # that was not this teardown's -- and ``_run`` claimed and
                # published ITS OWN report, so the parent already holds that
                # outcome. Re-writing the record here left the tombstone on disk
                # contradicting the completion the parent received, with nothing
                # that ever re-reconciled the two. The delivered record stands;
                # the failure is kept where it is still true -- on the audit row
                # below (``outcome="failed"``, the reason named) and in the log.
                # A delivery rule for a run that completes under its own reap
                # (the report carrying both) is #13959, not this branch.
                logger.warning(
                    "Reaper: %s completed and reported before its kill decided; "
                    "the kill failure is kept on the audit, not on the delivered record: %s",
                    agent_id,
                    kill_failed,
                )
            # Guard 2 of 3 — SLOT accounting, on its own one-shot token and therefore
            # independent of both `done` (above) and `reaped`. A reap/cancel frees a
            # slot but — unlike normal completion — does NOT otherwise pump the queue,
            # so queued spawns would sit stranded until an unrelated agent finished.
            # Drain here so the freed slot is used immediately.
            if self._manager._release_slot(info):
                self._manager._running_count = max(0, self._manager._running_count - 1)
                self._manager._drain_queue()

            try:
                sel().log_tool_invocation(
                    session_key=session_key,
                    source="subagent",
                    tool_name="reaper_force_kill",
                    # Never ``reaped`` for a process the kill left alive -- or one
                    # the kill never got to decide on: the reap ended the run's
                    # record, not its process.
                    outcome="reaped" if kill_failed is None else "failed",
                    metadata={
                        "subagent_id": agent_id,
                        "session_key": session_key,
                        "elapsed": int(elapsed),
                        # What the kill could not do, named on the row that says
                        # ``failed`` -- the audit keeps it even when the record
                        # above could not (a report already delivered). Held to
                        # the record's own bound: a failed Windows tree drain
                        # carries one line per process the run spawned.
                        **(
                            {"kill_failed": kill_failed[:MAX_ERROR_DETAIL_LEN]}
                            if kill_failed is not None
                            else {}
                        ),
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
        # The fence is down: the run is recorded and audited, so a caller held
        # at the door lands under a key whose record it follows.

        # Guard 3 of 3 — the terminal REPORT (subagent_done + _on_done), owned by
        # the finalize claim. The claim deliberately does NOT consult `info.done`:
        # `_run_inner` may set `done` while the teardown above is suspended, and
        # gating on it made the reaper decline while `_run`'s finally also declined
        # (it sees `reaped`) — so nobody reported. The report runs SHIELDED, so a
        # cancellation landing mid-report (cancel_all during shutdown) still
        # delivers rather than stranding the outcome with the claim consumed.
        # The task was launched ahead of the teardown (see there); the record is
        # final now, so this is where it is RELEASED to publish -- or dismissed,
        # when the claim went to another path that reports instead. A dismissed
        # task is disowned first, so its silent exit latches no delivery failure.
        info.elapsed = elapsed
        if report_gate.done():
            # The report was abandoned before the record was final: the shutdown
            # drain cancelled it (and re-admitted the run to orphan recovery),
            # which cancels the gate it waited on. Nothing is left to release,
            # and a ``set_result`` here would raise out of a tail that still owes
            # its caller the cancellation below.
            self._manager._report_owners.pop(report_task, None)
        elif self._manager._claim_finalize(info, supersede_recovery=True):
            report_gate.set_result(True)
        else:
            self._manager._report_owners.pop(report_task, None)
            report_gate.set_result(False)
        # The record is final and the report is released (or dismissed): this
        # is what a caller that JOINED this reap (``_force_reap``) is waiting
        # for, so it is settled here, not when the delivery below returns.
        # ``_await_report`` is an unbounded shield over a parent injection
        # capped at ``_ON_DONE_TIMEOUT``, and the joiner may be the reaper's own
        # sweep or a Stop request: neither should stand behind one run's
        # delivery once the kill is decided and written. The entry itself is
        # popped by ``_force_reap`` when this reap is gone.
        joined = self._manager._reaps_in_flight.get(agent_id)
        if joined is not None and not joined.done():
            joined.set_result(None)
        if interrupted is not None:
            # The record is written, the audit says what the kill did not get
            # to decide, and the report is released on its own strongly-held
            # task (``_report_tasks``): ``cancel_all()``'s bounded drain owns it
            # from here, as it owns the run path's report. Not awaited: the
            # caller is being cancelled -- at shutdown, inside ``cancel_all()``'s
            # own gather -- and a shielded await here would hold that gather
            # for the injection cap, the very wait the drain bounds.
            raise interrupted
        await self._manager._await_report(report_task)

        # Truncate retained text AFTER _on_done to preserve full output for result injection
        if len(info.streaming_text) > 10_000:
            info.streaming_text = info.streaming_text[:10_000] + "\n…(truncated)"

    def _retain_process_handles_impl(self, agent_id: str, session_key: str) -> list[ProcessHandle]:
        """Every process the key names at this point, the run's own first, taken BEFORE a reset.

        Called by both teardown paths (``_teardown_run_session`` and
        ``_force_reap``) immediately ahead of their ``reset``, because the reset
        that follows pops the session from the map before the awaits that can
        hang. Three sources, each read once, here, and never after a reset:

        * the entry RETAINED in ``_process_handles`` under ``agent_id`` by the
          other path before ITS reset -- the run's own process, when that reset
          is the one hanging (the common shape: the run's own ``finally`` popped
          the session, the reaper then arrives and has to act on the process the
          run could not stop);
        * every session the session manager is tearing down under the key
          (``SessionManager.tearing_down``, the list in pop order), for a reset
          started outside the two paths -- a dashboard reset, ``cancel_all`` --
          that retained nothing here; two teardowns can hang under one key (a
          successor a cold start registered while the first hung, whose own
          reset popped it and hung too), and each is a process of its own;
        * the session still live under the key: the run's own when this path is
          first, else a successor a cold start registered under the key during
          the other path's awaits. The reap ends the KEY -- its reset pops that
          successor too -- so it is a candidate in its own right, verified and
          signalled on its own handle
          (:func:`kiro_crew.process_identity.process_handle_of`); preferring
          either alone would leave the other's process unrecorded.

        Distinct processes only (the same pid AND start id under two sources is
        one handle; the same pid under two start ids is two processes, and both
        are kept); a
        session with no recorded pid names no process. What this path holds is
        recorded back under ``agent_id`` so the other path finds it on its miss,
        and the caller clears the entry once it has decided. An empty list means
        no session was live, torn down or retained before either reset: nothing
        to stop.

        The live table is the session map's own ``_sessions`` dict, read the way
        the kill always read it; a map that exposes no such mapping, or no
        ``tearing_down``, is a miss, not an error -- this runs ahead of EVERY
        reset, and a reap that raised here would stop nothing and record nothing.
        """
        sessions = self._manager._sessions
        live = getattr(sessions, "_sessions", None)
        tearing_down = getattr(sessions, "tearing_down", None)
        candidates = (
            *self._manager._process_handles.get(agent_id, ()),
            *(
                process_handle_of(session)
                for session in (
                    *(tearing_down(session_key) if callable(tearing_down) else ()),
                    live.get(session_key) if isinstance(live, Mapping) else None,
                )
                if session is not None
            ),
        )
        handles: list[ProcessHandle] = []
        for candidate in candidates:
            if candidate.pid is None:
                continue
            if any(h.pid == candidate.pid and h.start_id == candidate.start_id for h in handles):
                # The same process (one pid, one start id) named by two sources
                # is one handle, one kill.
                continue
            # One pid under two start ids is TWO processes, and each is reached
            # only through its own handle: the later reading names the live
            # successor the pid was handed to, which is the only identity the
            # kill can verify and signal; the earlier names a leader that
            # exited, and what it left behind is reached only through what that
            # handle retained -- on Windows the exact-tree cleanup pin the spawn
            # reserved under (pid, old start id), which is keyed by identity and
            # so still pends beside the successor holding the pid, on POSIX the
            # group id the leader led. Keeping only the stale handle dropped the
            # successor (it ran on, audited ``reaped``); replacing it with the
            # later reading dropped the pin and the group (the old tree ran on,
            # audited ``reaped``). Both are kept and each is killed on its own.
            handles.append(candidate)
        if handles:
            self._manager._process_handles[agent_id] = list(handles)
        return handles

    async def _sigkill_sessions_impl(
        self, session_key: str, handles: list[ProcessHandle]
    ) -> str | None:
        """Kill every handle's process (:func:`kiro_crew.process_identity.kill_each`); the failures joined, or None."""
        return await kill_each(
            handles, lambda handle: self._manager._sigkill_session(session_key, handle)
        )

    async def _sigkill_session_impl(
        self, session_key: str, handle: ProcessHandle | None
    ) -> str | None:
        """Best-effort SIGKILL when graceful reset hangs.

        ``handle`` is one of the process handles the caller retained before the
        reset (:meth:`_retain_process_handles`), and it is the ONLY thing that names
        the process. The reset pops the session from the map before it can
        hang, and a session found under the key afterwards is a successor a
        cold start registered during the reset's awaits (a queued turn, a
        continuation) -- a different process, whose kill would leave the run's
        own alive while its record said reaped. So the map is never consulted
        here; ``session_key`` names the run in the log only. ``None`` means no
        session was live before the reset: nothing to kill, not a failure.

        The kill itself -- the root verified by its recorded start id before
        anything reads through the pid and again immediately before the
        signal, the group signal with its pid-scoped fallback, the tree a gone
        leader left behind decided by the group id retained while it was
        alive, the escaped-children sweep, the failure naming -- is
        :func:`kiro_crew.process_identity.kill_verified_process`, the one home
        it shares with the cron reaper, so the two audit trails read alike and
        a rule change cannot regress one caller to a silent "reaped". It never
        raises (the caller owns a teardown it must still finish) and never
        swallows: it returns what stopped the kill, and the caller records it.
        Returns None once the run's process tree has been signalled or shown
        gone, otherwise the named failure for the caller's record.

        The client's child-tree probe, record capture and escaped-children
        sweep are resolved at call time INSIDE ``child_process_helpers`` (the
        session module looks the client functions up when called), so a test's
        patch of the client module is what the sweep runs; the helper itself is
        a plain top-level import (``subagent`` imports the session module, which
        imports nothing from the subagent modules -- there is no cycle).
        """
        if handle is None:
            logger.warning("Reaper: no session found for %s", session_key)
            return None
        return await kill_verified_process(
            handle, who="Reaper", key=session_key, child_helpers=child_process_helpers()
        )

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
        if info.id in getattr(self._manager, "_teardown_cancelled_ids", ()):
            # Same statement as the terminal-report gate: this run's parent has
            # been retired, so there is no conversation for a failure notice to
            # belong to. The notice is queued into the parent's slot and drained
            # into the LLM's context on the parent key's next turn, so leaving it
            # queued would surface a retired run's completion text inside whatever
            # conversation that key serves next. This is the single choke point
            # for every failure-announce caller (the ``_on_done`` timeout here and
            # the gateway's injection paths), so gating it once covers them all.
            logger.info("Reaper: skipping failure announce for %s — its parent ended", info.id)
            return
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
