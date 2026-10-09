"""Cancellation behavior for the SubagentManager facade."""

from __future__ import annotations

from typing import TYPE_CHECKING, AbstractSet, Any, Mapping, Sequence

from ._component import ManagerComponent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from ..subagent import (
        _ON_DONE_TIMEOUT,
        _RECOVERY_SLOT_WAIT_SECS,
        _REPORT_DRAIN_TIMEOUT,
        _RESET_TIMEOUT,
        Stats,
        SubagentInfo,
        _audit_ids,
        _parked_at_spawn_approval,
        asyncio,
        clear_tombstone,
        delivery_is_parked,
        logger,
        time,
    )


class CancellationCoordinator(ManagerComponent):
    """Own cancellation transitions while state remains facade-owned."""

    __slots__ = ()

    def _schedule_cancel_recovery_impl(
        self, info: SubagentInfo, *, reason: str = "unexpected_cancel"
    ) -> None:
        """Respawn *info* on a fresh task after a recoverable terminal event.

        The default serves an unexpected cancellation. ``context_overflow``
        serves a first-turn overflow and forces the replacement onto a fresh
        dedicated runtime after the original attempt's handle (shared) or
        process (dedicated) has finished teardown. ``session_not_found`` serves a
        live backend that lost the run's session: the teardown's reset keeps the
        durable pointer, so the replacement re-loads the same id.

        The current task cannot continue itself, so the replacement runs on a
        new task. Each caller owns its one-shot gate. The original run's finally
        block still performs session cleanup but skips terminal finalization
        while ``_recovering``.

        **Cancellation-source contract.** This branch exists for cancellations
        that arrive from OUTSIDE the manager's own lifecycle — in practice the
        parent task tree being torn down around a live subagent (e.g. a
        dashboard slot reset/removal cancelling background tasks, or an event
        during gateway component re-init) — mirroring the main path's
        unexpected-cancel recovery. Every INTENTIONAL cancel site in
        this module sets a terminal marker before cancelling, and the recovery
        branch defers to all of them: ``cancel()`` sets ``user_stopped``,
        ``cancel_all()`` sets ``_shutting_down``, and ``_force_reap`` sets
        ``reaped`` (checked before the recovery branch). Any NEW code path that
        cancels a subagent task on purpose MUST set one of those markers first,
        or the cancel will be treated as unexpected and recovered once.

        Coordination is explicit, not timed: ``_resume`` awaits the ORIGINAL
        task object to fully complete (its finally does session release/reset,
        slot decrement, and pops the task registry) before respawning. This
        guarantees the old finally can neither pop the new task out of
        ``self._tasks`` nor emit a duplicate completion, and the respawn never
        starts against a session whose reset is still in flight. The respawn
        then re-acquires a slot by waiting for capacity (the old finally's
        ``_drain_queue`` may have admitted a queued spawn into the freed slot),
        so the concurrency ceiling is never exceeded.

        The pending ``_resume`` task itself is registered in ``self._tasks``
        (under ``"<id>:recovery"``) so ``cancel_all()`` reaches it during
        shutdown — a recovery can never outlive or escape manager teardown.
        """
        orig_task = asyncio.current_task()
        recovery_key = f"{info.id}:recovery"

        async def _resume() -> None:
            try:
                # Explicit handshake: wait for the original task's finally
                # (session release/reset, slot decrement, task-registry pop)
                # to fully complete before respawning. The finally is bounded
                # (_RESET_TIMEOUT-capped reset), so add slack on top of it.
                if orig_task is not None:
                    await asyncio.wait({orig_task}, timeout=_RESET_TIMEOUT + 60)
                    if not orig_task.done():
                        logger.error(
                            "Subagent %s cancel-recovery: original task did not "
                            "finish teardown in time — aborting recovery",
                            info.id,
                        )
                        raise RuntimeError("original task teardown timed out")
                if info.done or info._reap_started or info.reaped or self._manager._shutting_down:
                    info._recovering = False
                    return
                if reason == "context_overflow":
                    # Reset preserves a dedicated session's durable pointer so
                    # an ordinary keep run can resume it. This session was
                    # rejected before its first turn, however, and loading it
                    # would reproduce the same deterministic overflow. Forget
                    # only the exact rejected SID after teardown and before any
                    # replacement allocation or capacity wait. SessionMap owns
                    # the comparison and removal under one process-wide lock,
                    # so a successor SID survives and makes recovery fail closed,
                    # and so does a run whose rejected SID was never captured.
                    session_key = f"subagent:{info.id}"
                    rejected_sid = str(getattr(info, "_session_id", "") or "")
                    if not rejected_sid:
                        # The identity capture after session acquisition is
                        # best-effort, while the allocation may already have
                        # persisted a resumable mapping for this key. Without
                        # the rejected SID nothing can tell a mapped SID that
                        # IS the rejected attempt from a successor, so neither
                        # deletion nor a replacement that could ``session/load``
                        # it is safe. Fail closed here, touching no mapping,
                        # before any capacity wait or allocation -- the same
                        # terminal arm a preserved successor takes.
                        raise RuntimeError(
                            "rejected session identity unknown; cannot retire it "
                            "before recovery respawn"
                        )
                    removed, current_sid = self._manager._sessions.forget_conversation_if_sid(
                        session_key, rejected_sid
                    )
                    if not removed and current_sid is not None:
                        raise RuntimeError(
                            "rejected session mapping changed before recovery respawn"
                        )
                    if removed:
                        # Deletion changes the live map immediately, while its
                        # file rewrite is debounced. Make retirement durable
                        # before clearing attempt state, waiting for capacity,
                        # allocating the replacement, or publishing recovery.
                        # An absent mapping changed nothing and needs no flush;
                        # a successor mapping failed closed above untouched.
                        await self._manager._sessions.aflush()

                    # The original task has completed its finally, including
                    # destruction of its shared handle or the reset of its own
                    # dedicated process. The replacement has not entered
                    # ``_run_inner`` yet, so the record must read as a run
                    # that has not started: the startup watchdog reaps a run
                    # with ``_exec_started`` set, no PID, no stream and no turn
                    # once its clock passes the deadline, and the clock still
                    # stamped here belongs to the FIRST attempt. With the PID
                    # cleared below, a capacity wait that outlives that stale
                    # clock would be force-reaped as a stalled start. So both
                    # clock fields go first, before the PID and before any
                    # await; ``_run_inner_impl`` re-stamps them for the
                    # replacement as its first statement.
                    info._startup_deadline_stamp = None
                    info._exec_started = None
                    # Then process identity, because samplers read the PID
                    # before sharing state; then ownership. The PID is the
                    # retired first attempt's (a shared runtime this run no
                    # longer leases, or its own process the reset ended), and
                    # the replacement records its own in ``_run_inner``. All
                    # of this runs only after teardown, since clearing any of
                    # it earlier would make the original teardown treat a
                    # shared runtime as a dedicated session and reset the
                    # wrong lifecycle boundary.
                    info._pid = None
                    info._session_sharing = False
                    info._shared_provider = None
                # Re-acquire a slot through capacity, not blind increment:
                # the old finally freed our slot and may have drained a queued
                # spawn into it. Wait (bounded) for a free slot so recovery
                # never pushes the pool past max_concurrent.
                deadline = time.time() + _RECOVERY_SLOT_WAIT_SECS
                while self._manager._running_count >= self._manager._max_concurrent:
                    if time.time() >= deadline or self._manager._shutting_down:
                        raise RuntimeError("no free slot for recovery respawn")
                    await asyncio.sleep(0.25)
                if info.done or info._reap_started or info.reaped or self._manager._shutting_down:
                    info._recovering = False
                    return
                info._recovering = False
                # Claim the slot and launch the respawn ATOMICALLY (no await
                # between capacity check, increment, and create_task). An await
                # in that window would let a finishing subagent's _drain_queue
                # admit a queued spawn into the same slot and push the pool
                # past max_concurrent. The respawned _run owns the slot from
                # here (its finally decrements). The informational
                # subagent_recovering emit happens after, where a cancellation
                # cannot leak the counter.
                self._manager._running_count += 1
                # The interrupted run's finally already consumed this info's
                # slot token to free its slot. The respawn occupies a FRESH slot,
                # so re-arm the token or the respawned run's finally would no-op
                # and leave `_running_count` permanently inflated.
                info._slot_released = False
                # The respawn is a NEW process: the dead one's RSS readings must
                # not make the spawn guard treat it as settled (a ~zero gap for
                # the sweep before it is measured). Its peak stays -- a high-water
                # mark for the run, and the conservative direction -- but the
                # sample count and the last reading start over so the fresh
                # process is priced as warming until the reaper has seen it.
                # Generation FIRST: a sweep whose off-loop read is in flight
                # re-checks it after reading, so it must already have moved
                # before the readings below are cleared.
                info._rss_generation += 1
                info._rss_samples = 0
                info.last_rss_gb = 0.0
                self._manager._tasks[info.id] = asyncio.create_task(self._manager._run(info))
                try:
                    await self._manager._fire_event("subagent_recovering", info, {"attempt": 1})
                except Exception:
                    logger.debug("subagent_recovering emit failed for %s", info.id, exc_info=True)
            except Exception:
                logger.exception("Subagent %s cancel-recovery respawn failed", info.id)
                info._recovering = False
                # The RECORD keeps its own first-arrival-wins `done` guard...
                if not info.done and not info._reap_started and not info.reaped:
                    # Full terminal finalization — the UI must never be left on
                    # a running card and the parent must still hear about the
                    # failure (with any partial result) even when the respawn
                    # itself could not happen.
                    info.done = True
                    if reason == "context_overflow":
                        info.error = (
                            "agent context exceeded the model window and the dedicated-session "
                            "recovery could not start"
                        )
                        tombstone_cause = "error"
                    elif reason == "session_not_found":
                        info.error = (
                            "the backend lost this run's session and the fresh-runtime "
                            "re-run could not start"
                        )
                        tombstone_cause = "error"
                    else:
                        info.error = "cancelled (recovery failed)"
                        tombstone_cause = "cancelled"
                    info.elapsed = time.time() - info.started
                    Stats().inc_subagent_failed()
                    self._manager._write_tombstone(info, tombstone_cause)
                    self._manager._record_cost(info)
                if not info.elapsed:
                    # Report needs an elapsed even when the record above was
                    # skipped because another path had already set `done`.
                    info.elapsed = time.time() - info.started
                # ...and the REPORT goes through the one-shot claim, exactly like
                # the reap and `_run`'s finally. Routing through the claim (not a
                # direct `subagent_done`/`_on_done` fire) keeps this from being a
                # fourth reporter outside the very claim this
                # class uses to guarantee exactly-once delivery, so a reaper
                # racing a failed respawn cannot deliver the outcome twice.
                # Reporting via `_run_terminal_report` also shields the delivery,
                # which matters here because `_force_reap` cancels this task.
                if self._manager._claim_finalize(info):
                    await self._manager._run_terminal_report(
                        info,
                        source="Recovery",
                        injection_timeout_reason=(
                            f"delivery timed out after {int(_ON_DONE_TIMEOUT)}s "
                            "(recovery failure)"
                        ),
                        mark_delivered_on_success=False,
                        # Same reasoning as the reap path: settle siblings' holds.
                        settle_digest=True,
                    )
            finally:
                # Whether respawned, aborted, or cancelled: this pending
                # recovery is not outstanding.
                _reg = self._manager._tasks.get(recovery_key)
                if _reg is asyncio.current_task():
                    self._manager._tasks.pop(recovery_key, None)

        async def _resume_guarded() -> None:
            try:
                await _resume()
            except asyncio.CancelledError:
                # The pending recovery itself was cancelled (cancel_all during
                # shutdown, or manager teardown). Terminal by default — a
                # cancelled recovery NEVER re-recovers; just make sure the
                # record isn't left in limbo.
                info._recovering = False
                _live = self._manager._tasks.get(info.id)
                if _live is not None and not _live.done():
                    # Respawn already launched — the live run owns the record
                    # (its own CancelledError arm is terminal: one-shot flag is
                    # spent). Don't finalize over it.
                    raise
                # `_reap_started`, not just `reaped`: `_force_reap` cancels this
                # task BEFORE it sets `reaped` (which must stay false until the
                # reaper owns the record — see `_reap_started`). Consulting only
                # `reaped` would let this arm win the race and persist a neutral
                # user Stop as a FAILURE, with a failure stat and a "cancelled"
                # tombstone the reaper could not correct.
                if not info.done and not info._reap_started and not info.reaped:
                    info.done = True
                    info.error = "cancelled"
                    info.elapsed = time.time() - info.started
                    if not info.user_stopped:
                        # A user-initiated stop is a neutral outcome, not a
                        # failure — matching the reap path's own record guard.
                        Stats().inc_subagent_failed()
                    self._manager._write_tombstone(info, "cancelled")
                raise

        _t = asyncio.create_task(_resume_guarded())
        self._manager._tasks[recovery_key] = _t
        _t.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    def _unqueue_impl(
        self, agent_id: str, *, stored: dict | None = None, store_cancelled: bool = False
    ) -> dict | None:
        """Remove and return a not-yet-started spawn from the stagger queue.

        The queue is the only record of a waiting run — ``spawn`` returns its
        queued ``SubagentInfo`` without registering it in ``_agents``. Returning
        the entry lets cancellation publish the same neutral stopped terminal
        outcome as a run that had already started, including batch accounting.

        A ``_resume_id`` entry is NOT such a spawn and is never matched here.
        ``request_resume`` files one for a run that is already RESIDENT (runtime
        alive, lane slot yielded) under the run's own ``_preassigned_id``, so an
        id match alone cannot tell the two apart — and treating a resume entry as
        an unstarted spawn hands a live run to ``_report_queued_stop``, whose
        synthetic ``queued=True`` record replaces the real ``_agents`` row: the
        coroutine keeps executing, the parent is told the work never started, and
        the record ``resume_grant`` needs to hand the slot back is gone. Skipping
        it leaves the run to the paths that own a live one — ``cancel``'s reap
        for a resident record, the store row for a claimable one, which
        ``taskq_cancel_queued`` above already returned.
        """
        # The persisted row is cancelled BEFORE the window entry is dropped, so a
        # drain racing this cannot claim it; a cancel that did not LAND is
        # handled below rather than assumed. A row outside the window is cancelled here as well and its
        # params come back from the store so the queued-stop report is whole.
        admission = self._manager._admission
        # ``store_cancelled`` says the caller already cancelled the row through
        # the ASYNC seam, which is how a coroutine avoids the synchronous store
        # call below. A boolean rather than a sentinel default because a default
        # is evaluated in this module's namespace while the body runs in the one
        # ``bind_component_globals`` rebinds it onto, and no single module-level
        # name is visible to both.
        if not store_cancelled:
            stored = admission.taskq_cancel_queued(agent_id)
        entry = self._take_window_entry(agent_id)
        if entry is None:
            # A non-durable row the coroutine pump has popped and is still
            # dispatching: taking it here is what makes the pump drop it. It has
            # no store row, so there is no cancel to re-post for it.
            undurable = self._manager._undurable_in_dispatch.pop(agent_id, None)
            return stored if undurable is None else undurable
        if stored is None:
            self._repost_unlanded_cancel(agent_id)
        return entry

    def _take_window_entry(self, agent_id: str) -> dict | None:
        """Pop *agent_id*'s UNSTARTED window entry, never a ``_resume_id`` one."""
        queue = self._manager._queue
        for index, params in enumerate(queue):
            if params.get("_resume_id") or str(params.get("_preassigned_id") or "") != agent_id:
                continue
            return queue.pop(index)
        return None

    def _repost_unlanded_cancel(self, agent_id: str) -> None:
        """Say that a stopped window entry's store cancel did not land, and retry it.

        A window entry always HAS a row while a store is attached -- a spawn
        whose accept the store refused is never queued -- so nothing cancelled
        for one means the cancel did not LAND: the store was unreachable, or the
        row left the unstarted states between the read and the write. The caller
        publishes a stop either way, and a row left `queued` is dispatchable by
        the next incarnation, which would run work the user was told had
        stopped. So the refusal is audible (the shape `taskq_settle` uses for a
        refused `finish`) and re-posted to the writer thread, where a store that
        answers again cancels the row; `taskq_cancel_queued` re-reads the state
        under its own transaction, so a row that legitimately started is left
        alone.
        """
        # Imported here: this helper is not an ``_impl`` and so keeps this
        # module's namespace, where the facade's ``logger`` is only a type hint.
        from ..subagent import logger

        admission = self._manager._admission
        store = admission.taskq_store()
        if store is None:
            return
        logger.warning(
            "Queued stop for %s: no store row was cancelled — re-posting the cancel",
            agent_id,
        )
        admission._post_store_write(
            store,
            f"queued cancel retry {agent_id}",
            admission.taskq_cancel_queued,
            agent_id,
        )

    def _report_queued_stop_impl(
        self,
        params: dict,
        *,
        row_settled: bool = False,
        error: str = "",
        report_owed: bool = False,
    ) -> "asyncio.Task[bool] | None":
        """Publish the terminal record of work that ended before startup.

        A neutral stop by default. With *error* it is the failure that ended the
        wait instead -- the memory wait's max-wait expiry
        (``taskq_expire_memory_waits``) -- reported through the same synthetic
        record, so batch accounting and delivery are the stop's. *report_owed*
        marks that record as one whose report the store owes until it reaches
        the parent (``SubagentInfo._report_owed``).

        Every end of a waiting row lands here, whichever path removed it, so
        this is where the parent's queued depth is asked for -- once per row,
        each under its own wave; a bulk stop's requests share one read -- and
        the terminal record itself asks for nothing (``queued=True``).

        *row_settled* says the caller's own cancel of the store row landed, so
        that cancel is the row's terminal write and the report's settle writes
        no second one (see ``taskq_settle``).

        The claim is per ROW: a row that already holds its finalized queued-stop
        record was reported by whichever stop reached it first, and this one
        reports nothing. Each call builds a fresh ``SubagentInfo``, so the
        record's own one-shot claim cannot see an earlier report of the row.

        Returns the report task, or None when no report runs here (no id, the
        row already reported, or the finalize claim is already another path's).
        """
        if self._queued_stop_reported(str(params.get("_preassigned_id") or "")):
            return None
        self._republish_queue_depth(
            str(params.get("parent_session_key") or ""), str(params.get("batch_id") or "")
        )
        info = SubagentInfo(
            id=str(params.get("_preassigned_id") or ""),
            task=str(params.get("task") or "(stopped before start)"),
            parent_session_key=str(params.get("parent_session_key") or ""),
            agent=str(params.get("agent") or ""),
            user_stopped=not error,
            error=error,
            queued=True,
            batch_id=str(params.get("batch_id") or ""),
            batch_total=max(0, int(params.get("batch_total") or 0)),
        )
        if not info.id:
            return None
        # A caller that is not the user names the stop, so the announce does not
        # credit the user with a stop they never pressed.
        stop_origin = params.get("_stop_origin")
        if isinstance(stop_origin, str) and stop_origin:
            info._stop_origin = stop_origin
        # Never over a REGISTERED run's record. A row the pump claimed and
        # registered while its stop was still on the way is a live run: a
        # synthetic ``queued=True`` terminal laid over it leaves the run
        # executing behind a "stopped before start" card, and every running
        # sweep skips a queued record, so nothing would ever stop it. The
        # record stays; the live path (the running sweep, ``cancel``) owns it.
        if self._registered_run(info.id):
            logger.info(
                "Queued stop for %s skipped: the run is registered; the live stop owns it",
                info.id,
            )
            return None
        info._report_owed = report_owed
        # Not registered, so the row will never start: drop what this process
        # kept for its start. A registered run's holds stay with its own start.
        self._manager._forget_pending_start(info.id)
        # Queued runs have no `_agents` record yet. Register every synthetic
        # terminal before report tasks can run, leaving `done=False` until each
        # task starts. That keeps earlier reports from treating themselves as
        # the final batch member and flushing a partial digest while sibling
        # queued-stop reports are still pending.
        self._manager._agents[info.id] = info
        if not self._manager._claim_finalize(info, row_settled=row_settled):
            self._manager._agents.pop(info.id, None)
            return None
        return self._manager._spawn_terminal_report(
            info,
            source="Queued expiry" if error else "Queued stop",
            injection_timeout_reason=(
                "delivery timed out after queued subagent expiry"
                if error
                else "delivery timed out after queued subagent stop"
            ),
            mark_delivered_on_success=False,
            settle_digest=True,
        )

    def _registered_run(self, agent_id: str) -> bool:
        """Whether *agent_id* is a registered run: an ``_agents`` record that is
        not a queued-stop one. Its row belongs to the live path (the running
        sweep, ``cancel``), never to a queued stop."""
        record = self._manager._agents.get(agent_id)
        return record is not None and not record.queued

    def _queued_stop_reported(self, agent_id: str) -> bool:
        """Whether a stop has already reported *agent_id* as stopped before start.

        Read from the synthetic record ``_report_queued_stop`` registers: a
        waiting row has no ``_agents`` record of its own, and a run that started
        is never ``queued``.
        """
        record = self._manager._agents.get(agent_id) if agent_id else None
        return record is not None and record.queued and record._finalized

    def snapshot_teardown_children_impl(self, parent_session_key: str) -> tuple[str, ...]:
        """The run ids belonging to *parent_session_key*, read with no await.

        The selection half of a parent-end teardown, split out so it can be taken
        while the session registry lock is still held — before the retired key is
        exposed for reuse. Selecting later, which is where the teardown's own
        awaits are, matches on a key string that a cold start may by then have
        registered a SUCCESSOR under, and the retired generation's teardown would
        cancel the successor's runs.

        Synchronous for that reason and not by preference: an ``await`` anywhere in
        here would reopen the window it exists to close. Both the live and the
        QUEUED runs are taken, because a queued run's stagger timer would otherwise
        start work for a parent that is gone.
        """
        if not parent_session_key:
            return ()
        mine = [
            info
            for info in self._manager._agents.values()
            if info.parent_session_key == parent_session_key
        ]

        # Parked on a spawn-approval prompt and never started: NOT this teardown's to
        # stop, which is the rule ``cancel_for_parent_impl`` already applies one method
        # down ("that prompt has its own explicit reject action"). The approval is a
        # decision a person has been asked for, and cancelling the run answers it for
        # them -- the request then reads as "not found or expired", which is
        # indistinguishable from having taken too long to reply.
        #
        # This is the same statement ``taskq_cancel_queued(allow_admitted=False)`` makes
        # by refusing a claimed row, reached on the in-memory side: the id lives in
        # ``_agents`` here rather than in the store, so the store's state gate never saw
        # it. ``_exec_started is None`` is
        # part of the test for the same reason it is there: a run that has begun
        # executing and is parked on a LATER approval is live work, and a parent end does
        # stop that.
        live = [info.id for info in mine if not info.done and not _parked_at_spawn_approval(info)]
        # Parked on a spawn approval and never started: not CANCELLED, but not ignored
        # either. Two things are true at once and they want different halves of the
        # teardown.
        #
        # Not cancelled, because the approval is a decision a person was asked for and
        # cancelling answers it for them: the reply then reads "not found or expired",
        # which is what having taken too long to answer also looks like. This is the rule
        # ``cancel_for_parent_impl`` already applies for Stop-all, and the
        # private-workflow E2E is the case that shows it -- a pooled worker's ``destroy``
        # cancelled such a child and the approval POST answered 404.
        #
        # But its delivery is still gated, because the conversation it would report into
        # has ended. If the person approves later the run proceeds and its result goes to
        # its own file and tombstone rather than into whatever session that key serves by
        # then. So it keeps its own decision and loses only the injection, which is the
        # same split the finished-but-undelivered children get.
        approval_parked = [
            info.id for info in mine if not info.done and _parked_at_spawn_approval(info)
        ]
        # Finished, but its outcome has not reached the parent. The question is asked
        # through ``delivery_is_parked``, which reads the classification in
        # ``DELIVERY_ROUTING_FIELDS`` -- enumerated from the four modules that WRITE
        # delivery routing state rather than assembled from whichever representation a
        # failure happened to expose. Naming fields inline here is what let a parked
        # representation through repeatedly: ``_reported_to_parent`` is set the moment
        # ``_on_done`` RETURNS, and several routes return having only parked the work.
        #
        # There is nothing left to CANCEL in any of these -- which is why the ids are not
        # returned -- but the delivery is exactly what must not land, since the injector
        # resolves the parent key through the session registry and CREATES a session when
        # none is live. Selecting only the not-done runs left this whole class of child
        # free to rebuild the conversation the teardown had just taken down.
        undelivered = [info.id for info in mine if info.done and delivery_is_parked(info)]
        queued = [
            str(params.get("_preassigned_id") or "")
            for params in [*self._manager._queue, *self._manager._undurable_in_dispatch.values()]
            if params.get("parent_session_key", "") == parent_session_key
            and not params.get("_resume_id")
        ]
        selected = tuple(agent_id for agent_id in [*live, *queued] if agent_id)
        # Taken from the window by a Stop all batch whose cancels are still on
        # the writer thread (``_stop_queued``): in neither ``_queue`` nor
        # ``_agents``, and once the batch's cancel lands the store sweep no
        # longer names them either. Nothing is left to CANCEL -- the batch owns
        # that -- but its report of each row would inject into the conversation
        # that has just ended, so they are gated like the undelivered ones.
        batching = [
            agent_id
            for agent_id, parent in self._manager.__dict__.get("_batched_stop_parents", {}).items()
            if parent == parent_session_key
        ]
        # Armed HERE, not in the cancel: this method is the last synchronous point
        # before the teardown's awaits, and a run that completes during those awaits
        # would otherwise report into the retired parent before anything marked it.
        # The marked set is WIDER than the returned one, deliberately: the gate is about
        # delivery and the return value is about cancellation, and the finished-but-
        # undelivered runs need the first without the second.
        self._manager._teardown_cancelled_ids.update(selected)
        self._manager._teardown_cancelled_ids.update(
            agent_id for agent_id in undelivered if agent_id
        )
        self._manager._teardown_cancelled_ids.update(
            agent_id for agent_id in approval_parked if agent_id
        )
        self._manager._teardown_cancelled_ids.update(batching)
        # A follow-up watcher is a SECOND announce path for the same run, and the id gate
        # cannot see it: when a queued follow-up cannot be delivered the watcher announces a
        # SYNTHETIC failure built with a fresh id, so it walks past a gate keyed on the run
        # that produced it -- the same shape as the wave digest's flush record. Disarm it at
        # the source for the same reason. The accepted continuation is cancelled rather than
        # announced: its parent has ended, so an announcement would itself recreate the
        # retired conversation.
        #
        # Include watcher-held records as well as ``_agents``. A watcher deliberately
        # outlives its completed run and can therefore be the only remaining owner record
        # after completed-state eviction.
        followup_infos = {
            id(info): info
            for info in (
                *mine,
                *getattr(self._manager, "_followup_watcher_infos", {}).values(),
            )
            if info.parent_session_key == parent_session_key
        }
        for info in followup_infos.values():
            if getattr(info, "pending_followups", None):
                info.pending_followups = []
                self._manager._audit_followup(info, "followup_suppressed")
            followup_watcher = self._manager._followup_watchers.get(info.id)
            if followup_watcher is not None and not followup_watcher.done():
                self._manager._cancel_task_intentionally(
                    followup_watcher,
                    info,
                    reason="parent teardown cancelled owned follow-up",
                )
            self._manager._followup_watchers.pop(info.id, None)
            getattr(self._manager, "_followup_watcher_parents", {}).pop(info.id, None)
            getattr(self._manager, "_followup_watcher_infos", {}).pop(info.id, None)
        return selected

    async def cancel_for_teardown_impl(
        self,
        agent_ids: "Sequence[str]",
        *,
        parent_session_key: str,
        verb: str = "",
        accepted_since: AbstractSet[str] | None = None,
        retry: bool = False,
    ) -> int:
        """Stop exactly the runs in *agent_ids*, reporting none of them home.

        *retry* marks the reaper's re-run of a store sweep whose read was
        refused (:meth:`retry_owed_teardown_sweeps_impl`); it only quiets the
        refusal's log line.

        The cancellation half. It takes IDS rather than a parent key so that what
        is stopped was decided by :meth:`snapshot_teardown_children_impl` at a
        point where the answer could not be contaminated — a key would be
        re-resolved here, which is the whole defect.

        *accepted_since* is that snapshot's fence (:meth:`note_teardown_snapshot`):
        the ids of the rows the store accepted for this parent since it, still
        recording while this call runs. With it, every OTHER waiting row of this
        parent is stopped too, after the snapshot's own ids: a row held only by
        the store (a memory-deferred spawn, or one past the window) is in no
        snapshot, and left alone it stays queued for a conversation that has
        ended and starts into whatever the key serves next. The fence is what
        tells such a row from one a successor under the same key queued
        meanwhile, so the successor's rows are never swept. It orders by accept,
        not by a clock: a wall clock stepped back during the teardown would
        stamp the successor's row before the snapshot.

        Distinct from :meth:`cancel_for_parent_impl`, which is the user pressing
        Stop all: that verb's terminal report goes back to a parent the user is
        still looking at, and is the point of it. At a parent end the parent is
        gone, so each run is marked and the delivery gate in
        ``_report_terminal_impl`` drops the injection. The kill itself is the same
        machinery — no second reap path exists, and one would drift.
        """
        selected = {a for a in agent_ids if a}
        snapshot_ids = sorted(selected)
        agent_ids = sorted(selected)

        # ONE audit line for the whole teardown, emitted where every field is known: the
        # verb that ended the conversation, the key, and what the snapshot selected. A
        # parent end cancels work a user may be waiting on, and the ids it took are
        # otherwise only inferable from the absence of a result -- which is how a
        # cancelled-too-early row reads from the outside. WARNING when the snapshot
        # names work to discard: the gateway log's default level is WARNING, and at INFO
        # this line was invisible in every field report of runs "dying at random" --
        # each of those was a parent end whose only record sat below the level anyone
        # reads. A childless parent end discards nothing and stays at INFO, so the
        # warning is a signal rather than one line per closed tab.
        #
        # RESIDUAL, in TWO halves, and this line is where both are visible -- by what it
        # does not name. The teardown arms its mark and takes its snapshot at the one
        # synchronous point available, inside the registry lock hold that retires the key,
        # and anything ALREADY IN FLIGHT at that instant does not see either:
        #
        #   * ADMITTED LATE MAY START. A spawn between its row write and its registration
        #     is in neither the queue nor ``_agents``, so no snapshot can name it, and it
        #     starts into whatever the key serves next. A durable row whose
        #     ``taskq_accept_record`` ran before the snapshot is outside this half: the
        #     fence never recorded it, so the sweep below stops it. One that ran after is
        #     spared, the retired conversation's accept still queued on the writer
        #     thread at the snapshot included.
        #   * REPORTING LATE MAY DELIVER. A report that has already passed the delivery
        #     gate and is suspended inside ``_on_done`` is not stopped by marking its id
        #     afterwards: the injector resolves the parent through ``get_or_create``,
        #     which CREATES a session when none is live, and never re-reads the mark. So
        #     the gate is not a backstop for this half -- the earlier claim that a missed
        #     run's report is dropped holds only for a run the snapshot did name.
        #
        # Both are bounded by the run's own timeout. Neither is closed by another recheck:
        # the two halves are the same defect at opposite ends of the same window, and a
        # recheck added at either end leaves the other open. Selecting or re-testing needs
        # an await, and after an await, work belonging to the retired conversation looks
        # like work a successor under the same key has just started -- telling them apart
        # needs a conversation-incarnation counter the session layer does not have.
        # Tracked as a follow-up. A durable row is the one exception, because every
        # accept is recorded against the snapshot's fence as it happens: the store sweep
        # below selects after an await, by that record, and that is all it selects by.
        audit = logger.warning if snapshot_ids else logger.info
        audit(
            "parent-end teardown: verb=%s key=%s snapshot=%d total=%d snapshot_ids=%s",
            verb or "unnamed",
            parent_session_key or "-",
            len(snapshot_ids),
            len(agent_ids),
            _audit_ids(snapshot_ids),
        )

        # Marked BEFORE anything is stopped, and by id: a queued run has no
        # ``_agents`` row, so its synthetic terminal is built fresh by
        # ``_report_queued_stop`` and would default the flag to False. The delivery
        # gate reads this set, which is why marking here covers the live runs, the
        # queued ones and any follow-up synthetic alike.
        self._manager._teardown_cancelled_ids.update(agent_ids)

        async def _targets() -> AsyncIterator[str]:
            for agent_id in agent_ids:
                yield agent_id
            if accepted_since is None:
                return
            # Then this parent's rows the store accepted before the snapshot and no
            # snapshot could name (held only by the store, or hydrated into the window
            # after it). Read only now, once the named runs are stopped, so the sweep
            # never delays the reap of a live run behind a store read.
            try:
                swept = await self._manager._admission.taskq_pending_ids_for_async(
                    parent_session_key, include_window=True
                )
            except Exception:
                # Left queued, those rows would start into whatever the key serves
                # next, or expire into the conversation that ended. So the fence
                # stays open (an expiry it does not record injects nothing) and
                # each reaper sweep retries the read until it lands.
                # One warning per teardown; the reaper's retries log at debug.
                log = logger.debug if retry else logger.warning
                log(
                    "Teardown: reading the store rows of %s failed; the reaper retries it",
                    parent_session_key,
                    exc_info=True,
                )
                self._owe_teardown_sweep(parent_session_key, accepted_since, verb)
                return
            # The fence is read under its lock, after the store read: a row a
            # successor queued before that read was recorded before it was written.
            with self._manager._teardown_fence_lock:
                spilled = sorted(set(swept) - selected - accepted_since)
            if not spilled:
                return
            logger.warning(
                "parent-end teardown: verb=%s key=%s store_rows=%d store_ids=%s",
                verb or "unnamed",
                parent_session_key or "-",
                len(spilled),
                _audit_ids(spilled),
            )
            # Marked before each is stopped, for the reason the snapshot's ids are.
            self._manager._teardown_cancelled_ids.update(spilled)
            for agent_id in spilled:
                yield agent_id

        stopped = 0
        async for agent_id in _targets():
            if not agent_id:
                continue
            info = self._manager._agents.get(agent_id)
            if info is not None and info._ending_claimed:
                # Ending completed on its own: a parent end does not undo it,
                # and its report is already gated by the mark above.
                continue
            if info is not None and not info.done:
                # A LIVE run goes through the ordinary reap, which does no store
                # work of its own. The stop's cause and origin are written on the
                # record first: the run's own record and log then name the parent
                # end that stopped it, not the runtime death the reap's teardown
                # caused, and ``cancel`` carries the cause into the tombstone.
                # First stopper wins: a user Stop or a deadline reap already in
                # flight owns the attribution, and this teardown must not rewrite
                # the record of who actually ended the run.
                if not info._reap_reason:
                    info._reap_reason = "parent_end"
                if not info._stop_origin:
                    info._stop_origin = f"parent conversation ended ({verb or 'unnamed'})"
                try:
                    if await self._manager.cancel(agent_id):
                        stopped += 1
                except Exception:
                    logger.warning(
                        "Teardown: cancelling subagent %s failed", agent_id, exc_info=True
                    )
                continue
            # A QUEUED run is unqueued here rather than through ``cancel``, whose
            # ``_unqueue`` reaches the SYNCHRONOUS ``taskq_cancel_queued``. This
            # method is a coroutine on the gateway loop, so that call would stall it
            # for as long as the task store is contended. The store phase is awaited
            # through the writer thread and the result handed to ``_unqueue``, which
            # then skips its own call and keeps the rest of its behaviour — the
            # cancel-did-not-land retry. The depth request rides on the
            # ``_report_queued_stop`` below, as it does for every stopped row.
            try:
                params = await self._manager._admission.taskq_cancel_queued_async(
                    # A teardown may not cancel a CLAIMED-but-unstarted row: a row in
                    # that window may be carrying a person's decision (a spawn approval
                    # is the visible case), which a teardown has no standing to revoke
                    # for them. Stop-all keeps the wider behaviour: there the user asked
                    # for exactly that, and the claimer's post-claim re-read refuses the
                    # row it cancelled.
                    agent_id,
                    allow_admitted=False,
                )
                entry = self._manager._unqueue(agent_id, stored=params, store_cancelled=True)
                if entry is not None:
                    self._manager._report_queued_stop(entry, row_settled=params is not None)
                    stopped += 1
                    continue
                # Nothing was unqueued, and a refusal is not a commit -- but WHY the store
                # refused decides what happens next, and the two reasons want opposite
                # things. A row a drain has STARTED is a live run, and the live reap is
                # what stops it. A row merely CLAIMED is owned by a claimer that has not
                # registered yet: reaping it here is the same act the store just refused,
                # reached through a different door. So the live path is taken only for a
                # row that is actually executing, and a claimed one is left to the
                # incarnation that owns it.
                claimed_unstarted = (
                    await self._manager._admission.taskq_row_is_claimed_unstarted_async(agent_id)
                )
                if claimed_unstarted:
                    logger.info(
                        "Teardown: leaving %s to its claimer -- the row is claimed and "
                        "not started, so stopping it here would strand a registration",
                        agent_id,
                    )
                elif (late := self._manager._agents.get(agent_id)) is not None and not late.done:
                    # ``cancel`` only for a CONFIRMED live record. It reaches ``_unqueue``,
                    # whose store call is the SYNCHRONOUS one, and this method is a
                    # coroutine on the gateway loop -- so calling it blind stalls the loop
                    # for the SQLite busy timeout whenever the store is contended
                    # (``no-sync-store-call-from-a-coroutine``).
                    #
                    # ``not done`` as well as present, matching the live branch at the top of
                    # this loop. A PRESENT record is not a running one: a child that was live
                    # when the snapshot named it can finish during this loop's own awaits, and
                    # ``_force_reap`` marks such a record done and drops its task without
                    # popping it from ``_agents`` -- so the record lingers, terminal. Reading
                    # presence alone routed exactly that record into the synchronous
                    # ``_unqueue`` this comment exists to avoid, and the row is already
                    # terminal so there was nothing for it to do there either.
                    if await self._manager.cancel(agent_id):
                        stopped += 1
                else:
                    # No record and no claim: the store is the only thing that knew about
                    # this row, it refused, and there is nothing here to reap. Retrying the
                    # store is the next sweep's job, not this one's -- and doing it on this
                    # loop is what the rule above forbids.
                    logger.info(
                        "Teardown: %s has no live record and the store declined it; "
                        "leaving it for the next sweep",
                        agent_id,
                    )
            except Exception:
                logger.warning("Teardown: unqueueing subagent %s failed", agent_id, exc_info=True)
        return stopped

    async def cancel_for_parent_impl(self, parent_session_key: str) -> tuple[int, int]:
        """Stop one parent's running and queued agents.

        Queue entries are removed before the first suspending await, so a stagger
        timer cannot start work after the user clicked Stop all. Agents parked on
        a spawn-approval prompt remain pending because that prompt has its own
        explicit reject action.
        """
        if not parent_session_key:
            return (0, 0)
        # UNSTARTED entries only, the same class every other ``_queue`` scan
        # separates out (the pump's grant loop, the refill's lane census, the
        # eviction, the reserve): a ``_resume_id`` entry is a RESIDENT run asking
        # for its lane slot back, filed under its own ``_preassigned_id`` and
        # carrying its own ``parent_session_key``, so both terms of this match
        # hit it. It is stopped by the running sweep below — where its intact
        # ``_agents`` record still is — instead of through the queued-stop path,
        # which would publish a synthetic "never started" terminal over a live
        # run. Leaving the entry in the window does not weaken the pre-await
        # drain this method promises: a resume STARTS nothing (the run is already
        # resident), so a pump pass during the store read below can only hand a
        # slot back to a coroutine the running sweep then reaps, and a pass after
        # the sweep meets a ``user_stopped`` run that ``resume_reserve`` refuses.
        queued_stopped = 0
        # Held across both passes: the refill windows none of this parent's
        # rows meanwhile (``_refill_apply``), so a fetch queued on the writer
        # thread before this stop can neither put back a row it is cancelling
        # nor window a store-only row the pending read below would then skip.
        stopping: dict[str, int] = self._manager.__dict__.setdefault("_stopping_parents", {})
        stopping[parent_session_key] = stopping.get(parent_session_key, 0) + 1
        try:
            # ``_stop_queued`` drops these window entries before its first await,
            # so a stagger timer cannot start a queued agent once the stop began.
            queued_stopped = await self._stop_queued(
                [
                    str(params.get("_preassigned_id") or "")
                    for params in [
                        *self._manager._queue,
                        *self._manager._undurable_in_dispatch.values(),
                    ]
                    if params.get("parent_session_key", "") == parent_session_key
                    and not params.get("_resume_id")
                ],
                parent_session_key,
            )
            # This parent's rows waiting outside the in-memory window, read after
            # the window pass. A row started from disk meanwhile is caught by the
            # running sweep below.
            queued_stopped += await self._stop_queued(
                await self._manager._admission.taskq_pending_ids_for_async(parent_session_key),
                parent_session_key,
            )
        finally:
            if stopping.get(parent_session_key, 0) > 1:
                stopping[parent_session_key] -= 1
            else:
                stopping.pop(parent_session_key, None)
                # A pump pass that ran meanwhile windowed none of this parent's
                # rows, so a row spawned after the passes read their ids could
                # wait on disk beside a free slot with no pass due to bring it
                # in. One more pass, on the next loop turn: after the running
                # sweep below has taken its ids, so a row it starts is not
                # reaped with them.
                if not self._manager._shutting_down:
                    asyncio.get_running_loop().call_soon(self._manager._drain_queue)
            # Each row the pass stopped asked for the depth in its queued-stop
            # report. A pass that stopped none -- nothing was left, or it failed
            # or was cancelled first -- asks here, so a card whose count went
            # stale is repaired even then. A pass that raises ends the call
            # here too, before the running sweep: reaping would free slots the
            # pump fills at once with the very rows the failed pass did not
            # reach, and the request reports the failure.
            if not queued_stopped:
                self._republish_queue_depth(parent_session_key)

        running_ids = [
            info.id
            for info in self._manager._agents.values()
            if info.parent_session_key == parent_session_key
            and not info.done
            and not info.queued
            and not _parked_at_spawn_approval(info)
        ]
        results = await asyncio.gather(
            *(self._manager.cancel(agent_id) for agent_id in running_ids),
            return_exceptions=True,
        )
        running_stopped = sum(result is True for result in results)
        return (running_stopped, queued_stopped)

    def note_teardown_snapshot(self, parent_session_key: str) -> None:
        """Open the fence for the store sweep of the parent-end cancel that
        follows (``accepted_since`` on :meth:`cancel_for_teardown_impl`):
        from here on, every row the store accepts for *parent_session_key* is
        recorded in it (:meth:`note_teardown_store_accept`).

        Synchronous like the snapshot itself, and taken beside it: every
        waiting row of this parent the fence does not record was accepted for
        a conversation that has ended by now. Kept per key at the LATEST
        snapshot, so two teardowns of one key whose cancels overlap both spare
        only rows accepted after a conversation that has since ended let go of
        the key. No store, nothing to sweep: no fence.
        """
        if self._manager._admission.taskq_store() is None or not parent_session_key:
            return
        with self._manager._teardown_fence_lock:
            self._manager._teardown_store_fences[parent_session_key] = set()

    def take_teardown_snapshot(self, parent_session_key: str) -> set[str] | None:
        """The fence :meth:`note_teardown_snapshot` opened, consumed by the cancel
        that sweeps for it, and still recording until
        :meth:`release_teardown_snapshot`. ``None`` when no snapshot opened one
        (no store, or a cancel no snapshot preceded), and the sweep is skipped."""
        with self._manager._teardown_fence_lock:
            fence = self._manager._teardown_store_fences.pop(parent_session_key, None)
            if fence is not None:
                self._manager._teardown_store_sweeps.append((parent_session_key, fence))
        return fence

    def _owe_teardown_sweep(
        self, parent_session_key: str, fence: AbstractSet[str], verb: str
    ) -> None:
        """Keep *fence* open for a retry of its store sweep (a refused read)."""
        with self._manager._teardown_fence_lock:
            owed = self._manager._teardown_sweeps_owed
            if not any(held is fence for _key, held, _verb in owed):
                owed.append((parent_session_key, fence, verb))

    async def retry_owed_teardown_sweeps_impl(self) -> int:
        """Run again each teardown store sweep whose read the store refused.

        Called from every reaper sweep. Each owed sweep is taken off the list and
        run as the teardown's own cancel with no snapshot ids, under the fence it
        kept open, so it stops the retired conversation's rows and spares a
        successor's exactly as the first read would have. A read refused again
        puts it back for the next sweep; one that lands releases the fence.
        Returns the rows stopped.
        """
        with self._manager._teardown_fence_lock:
            owed = list(self._manager._teardown_sweeps_owed)
            self._manager._teardown_sweeps_owed.clear()
        stopped = 0
        for parent_session_key, fence, verb in owed:
            try:
                stopped += await self.cancel_for_teardown_impl(
                    (),
                    parent_session_key=parent_session_key,
                    verb=verb,
                    accepted_since=fence,
                    retry=True,
                )
            finally:
                self.release_teardown_snapshot(fence)
        return stopped

    def release_teardown_snapshot(self, fence: AbstractSet[str] | None) -> None:
        """Stop recording into *fence*: its cancel has swept, or never will.

        Not while its store sweep is owed (:meth:`_owe_teardown_sweep`): the
        retry still needs the record, and an expiry of a retired row is gated
        by it until then.
        """
        if fence is None:
            return
        with self._manager._teardown_fence_lock:
            owed = getattr(self._manager, "_teardown_sweeps_owed", ())
            if any(held is fence for _key, held, _verb in owed):
                return
            self._manager._teardown_store_sweeps[:] = [
                held for held in self._manager._teardown_store_sweeps if held[1] is not fence
            ]

    def note_teardown_store_accept(self, parent_session_key: str, agent_id: str) -> None:
        """Record *agent_id* in every open fence for *parent_session_key*.

        Called by the accept BEFORE its row is written, so a row any sweep's
        store read can see was recorded first; possibly on the store's writer
        thread, hence the lock. A recorded id whose write then fails costs
        nothing: the fence only ever spares a row."""
        with self._manager._teardown_fence_lock:
            fence = self._manager._teardown_store_fences.get(parent_session_key)
            if fence is not None:
                fence.add(agent_id)
            for key, held in self._manager._teardown_store_sweeps:
                if key == parent_session_key:
                    held.add(agent_id)

    def accepted_before_open_teardown(self, parent_session_key: str, agent_id: str) -> bool:
        """True while a teardown of *parent_session_key* is open and *agent_id*
        is not in its fence: the row was accepted for the conversation that ended.

        Open from the snapshot (:meth:`note_teardown_snapshot`) until its cancel
        has swept the store (:meth:`release_teardown_snapshot`). Read by a path
        that ends a store row in that window (the memory wait's max-wait
        expiry), which must not inject into the retired conversation: the
        injector creates a session when none is live. Once the sweep is done,
        such a row has been stopped by it.
        """
        if not parent_session_key or not agent_id:
            return False
        with self._manager._teardown_fence_lock:
            fences = [
                held
                for key, held in self._manager._teardown_store_sweeps
                if key == parent_session_key
            ]
            pending = self._manager._teardown_store_fences.get(parent_session_key)
            if pending is not None:
                fences.append(pending)
            return any(agent_id not in fence for fence in fences)

    def _republish_queue_depth(self, parent_session_key: str, batch_id: str = "") -> None:
        """Re-publish *parent_session_key*'s queued depth after a stop.

        A dashboard still showing a count from a frame it never saw superseded
        gets its answer here even from a stop that found nothing: Stop all is
        the control a user reaches for exactly then,
        and this is the authoritative count that repairs the card (and, at
        depth 0, forgets the remembered wait label). Guarded: an advisory event
        must never turn a stop into a failed request.
        """
        # Imported here: this helper is not an ``_impl`` and so keeps this
        # module's namespace, where the facade's ``logger`` is only a type hint.
        from ..subagent import logger

        try:
            self._manager._emit_queue_depth(parent_session_key, batch_id)
        except Exception:
            logger.debug("queue-depth re-emit failed after stop", exc_info=True)

    async def _stop_queued(self, agent_ids: Sequence[str], parent_session_key: str) -> int:
        """Unqueue each id that is still waiting and report it stopped; count them.

        Every row's store cancel runs in ONE job on the store's writer thread
        (``taskq_post_cancel_queued``), never on the loop: a cancel on the loop
        holds it for the store's busy timeout, once per row, whenever the store
        is contended.

        An id registered as a run after the ids were read (the store read is an
        await) is left out of the job: it is a live run, which the running sweep
        after this pass reaps, and cancelling its row would end it under the run
        and fence out the run's own settlement.

        The job is queued and the window entries are dropped before this method
        first suspends, so a stagger timer finds no entry to start, and a refill
        or a claim queued after the job lands behind the cancels and finds the
        rows cancelled. A refill fetch queued BEFORE the job is the caller's to
        fence (``cancel_for_parent`` holds the parent in ``_stopping_parents``).
        The job is queued first so that a post that raises leaves every entry in
        the window: nothing was cancelled, and nothing is reported stopped.

        The answers are applied by a tracked task that the caller awaits through
        a shield. Once the job is queued its cancels land whether or not the
        caller is still waiting, and a row cancelled in the store without a
        queued-stop report would leave its wave waiting for a completion that
        never comes.

        Until that task has reported a row, the row is in neither ``_queue``
        nor ``_agents``, and nothing on the loop says a cancel of it is on the
        way. Every id of the job is filed in ``_batched_stops`` for that span,
        and the two readers that would otherwise act on the row join its answer:
        a single ``cancel`` (``cancel_impl``), which would report the row itself
        and leave the batch, finding the row already cancelled, to report a
        popped one again; and a claim whose post-claim re-read answered before
        the cancel landed (``claim_and_start``), which would register the row
        as a run the cancel then ends under it, counted once as queued and once
        as running. Each id's parent (*parent_session_key*, whose rows the ids
        are) is filed beside it in ``_batched_stop_parents``, for a third
        reader: a parent-end teardown's snapshot
        (``snapshot_teardown_children``), which reads ``_queue`` and
        ``_agents`` and would otherwise leave the batch's report of each row
        free to inject into the ended conversation.
        """
        import asyncio

        ids = [
            agent_id
            for agent_id in dict.fromkeys(agent_ids)
            if agent_id and not self._registered_run(agent_id)
        ]
        if not ids:
            return 0
        admission = self._manager._admission
        outcomes = admission.taskq_post_cancel_queued(ids)
        entries = {agent_id: self._take_window_entry(agent_id) for agent_id in ids}
        # A non-durable row the coroutine pump is dispatching has no store row:
        # taking it here, before the first await, keeps the pump from starting it.
        in_dispatch = self._manager._undurable_in_dispatch
        undurable = {
            agent_id: in_dispatch.pop(agent_id)
            for agent_id in ids
            if entries[agent_id] is None and agent_id in in_dispatch
        }
        if isinstance(outcomes, dict):
            return self._report_stopped_rows(ids, entries, outcomes, undurable=undurable)
        posted = outcomes
        batched: dict[str, asyncio.Future[Any]] = self._manager.__dict__.setdefault(
            "_batched_stops", {}
        )
        loop = asyncio.get_running_loop()
        joins = {agent_id: loop.create_future() for agent_id in ids}
        batched.update(joins)
        parents: dict[str, str] = self._manager.__dict__.setdefault("_batched_stop_parents", {})
        parents.update(dict.fromkeys(ids, parent_session_key))

        async def _apply() -> int:
            try:
                return self._report_stopped_rows(ids, entries, await posted, joins, undurable)
            finally:
                # A batch that never answered (the store closed under it) has
                # reported nothing, and a joined cancel learns exactly that.
                for agent_id, join in joins.items():
                    if batched.get(agent_id) is join:
                        del batched[agent_id]
                        parents.pop(agent_id, None)
                    if not join.done():
                        join.set_result(False)

        applying = admission.track_store_task(asyncio.ensure_future(_apply()))
        return await asyncio.shield(applying)

    def _report_stopped_rows(
        self,
        ids: Sequence[str],
        entries: Mapping[str, dict | None],
        outcomes: Mapping[str, object],
        joins: Mapping[str, asyncio.Future[Any]] | None = None,
        undurable: Mapping[str, dict] | None = None,
    ) -> int:
        """Report each row the store phase stopped, count them, then raise any row error.

        Each row is reported before the next is touched. A row whose cancel
        raised was NOT stopped: the store still holds it waiting, so its window
        entry goes back -- at the tail, where a refill would put it -- the rest
        of the stop goes on, and the first such failure is raised once it has:
        the caller must not go on as if the pass had stopped everything. A row
        whose report raised WAS stopped: it counts, and the report failure is
        logged. A row this batch took from the window that another stop's
        earlier cancel reported first counts too, so the count does not depend
        on which of the two reported it. Each row's answer also resolves its
        *joins* future, which a single ``cancel`` of that row (``cancel_impl``)
        or its claim (``claim_and_start``) may be waiting on.
        """
        # Imported here: this helper is not an ``_impl`` and so keeps this
        # module's namespace, where the facade's ``logger`` is only a type hint.
        from ..subagent import logger

        def _answer(agent_id: str, result: object) -> None:
            join = (joins or {}).get(agent_id)
            if join is not None and not join.done():
                join.set_result(result)

        stopped = 0
        failure: Exception | None = None
        for agent_id in ids:
            outcome = outcomes.get(agent_id)
            taken = (undurable or {}).get(agent_id)
            if taken is not None:
                # Already out of the pump's hands and with no store row, so
                # whatever the store answered, it is stopped and gets its report.
                stopped += 1
                try:
                    self._manager._report_queued_stop(taken)
                except Exception:
                    logger.warning(
                        "Reporting queued subagent %s stopped failed", agent_id, exc_info=True
                    )
                _answer(agent_id, True)
                continue
            if isinstance(outcome, Exception):
                logger.warning("Stopping queued subagent %s failed", agent_id, exc_info=outcome)
                failure = failure or outcome
                unstopped = entries.get(agent_id)
                if unstopped is not None and not any(
                    str(p.get("_preassigned_id") or "") == agent_id and not p.get("_resume_id")
                    for p in self._manager._queue
                ):
                    self._manager._queue.append(unstopped)
                _answer(agent_id, outcome)
                continue
            stored = outcome if isinstance(outcome, dict) else None
            entry = entries.get(agent_id)
            if entry is not None and stored is None:
                if self._queued_stop_reported(agent_id):
                    # Another stop's cancel, queued before this batch, landed
                    # first and that stop reported the row: nothing to re-post.
                    # The row still counts, so this stop's count is the same
                    # whichever of the two reported it.
                    stopped += 1
                    _answer(agent_id, True)
                    continue
                self._repost_unlanded_cancel(agent_id)
            row = entry if entry is not None else stored
            if row is None:
                continue
            if entry is None and self._registered_run(agent_id):
                # Registered after the job was posted: a live run, which the
                # running sweep reaps and counts. The claim joins this answer
                # (``claim_and_start``), so only a start that did not is here.
                _answer(agent_id, False)
                continue
            stopped += 1
            try:
                self._manager._report_queued_stop(row, row_settled=stored is not None)
            except Exception:
                logger.warning(
                    "Reporting queued subagent %s stopped failed", agent_id, exc_info=True
                )
            _answer(agent_id, True)
        if failure is not None:
            raise failure
        return stopped

    async def cancel_impl(self, agent_id: str) -> bool:
        """Cancel a single running subagent. Returns True if found and cancelled.

        User-initiated stop is a neutral terminal state, not an error: partial
        output is preserved on the info record (and in result.txt, as the latest
        attempt that wrote text left it), the tombstone is written as
        ``user_stop``, and the ``subagent_done`` event
        carries ``stopped: true`` so the UI renders a neutral "stopped" card.

        A caller that is NOT the user pressing Stop names itself on the record
        first: the parent-end teardown writes
        ``info._reap_reason`` (the tombstone cause -- ``parent_end``)
        and ``info._stop_origin`` (the one-line who/why)
        before calling here, and both ride into the reap unchanged. Nothing is
        inferred from the origin text. The run loop reads the same fields when
        its stream dies under the reap, so it reports that stop rather than the
        death the stop caused (see ``_run``'s reap-echo arm).
        """
        info = self._manager._agents.get(agent_id)
        if info is not None and info._ending_claimed:
            # A run that claimed its completed ending is ``done`` to a Stop:
            # nothing is left to stop, and nothing on the record is stamped. A
            # registered run is never in the stagger queue, so no unqueue (and
            # no store call on the loop) is attempted for it either.
            return False
        if not info or info.done or info.queued:
            # A registered ``queued`` record is a queued stop's terminal record
            # (``_report_queued_stop``), registered with ``done=False`` until its
            # report task runs. Its row never started and it holds no lane slot,
            # so a second Stop landing in that window (a double-clicked Stop, a
            # second client) is answered below, after the batched-stop join, and
            # never reaches the reap, which would free a slot the record does
            # not hold.
            #
            # A row a Stop all batch is cancelling and has not yet reported: the
            # batch owns its cancel and its one report, so this joins that
            # answer. Cancelling here too would land first and report the row,
            # and the batch, finding a popped row already cancelled, would
            # report it again.
            batched = self._manager.__dict__.get("_batched_stops", {}).get(agent_id)
            if batched is not None:
                outcome = await asyncio.shield(batched)
                if isinstance(outcome, Exception):
                    raise outcome
                return bool(outcome)
            if info is not None and info.queued:
                # The queued stop's terminal record: its row has already ended
                # and its report is pending, so nothing is left to cancel. The
                # answer is "not running", with no store call on the loop.
                return False
            # A run still WAITING behind the stagger has no `_agents` record at
            # all: `spawn` builds its queued SubagentInfo and returns it without
            # registering. Unqueueing prevents startup; the synthetic terminal
            # report keeps its parent and batch accounting from waiting forever.
            # The store phase is taken here rather than inside ``_unqueue`` so
            # the report knows whether this cancel landed (``row_settled``).
            stored = self._manager._admission.taskq_cancel_queued(agent_id)
            queued = self._manager._unqueue(agent_id, stored=stored, store_cancelled=True)
            if queued is not None:
                logger.info("Cancelled queued subagent %s before it started", agent_id)
                self._manager._report_queued_stop(queued, row_settled=stored is not None)
                return True
            return False
        if not info._reap_started:
            # The stamps below name THIS stop as the run's stopper. A reap already
            # in flight (a deadline, a parent end) owns the record, and the record
            # follows the FIRST stopper (``stop_is_neutral``) -- ``outcome`` reads
            # ``user_stopped`` directly, so writing it over a claimed deadline
            # failure would publish that failure as a neutral stop. Such a Stop
            # stamps nothing and joins the reap in flight (``_force_reap``
            # coalesces), returning once that reap's record is final.
            info.user_stopped = True
            # Recorded BEFORE the reap so the run loop can read them when its
            # stream dies under the session teardown ``_force_reap`` is about to
            # do. A caller that already named the cause keeps it; a bare cancel
            # is the user pressing Stop.
            if not info._reap_reason:
                info._reap_reason = "user_stop"
            if not info._stop_origin:
                info._stop_origin = "stopped by user"
            # Neutral semantics live in the RECORD, not just the live event: a
            # user stop leaves ``error`` unset so every consumer (reconnect
            # snapshots, tombstones, /api/spawn listing, orphan reconciliation)
            # derives the same neutral "stopped" status without having to
            # cross-check ``user_stopped``. _force_reap is also
            # user_stopped-aware and will not synthesize a reap error for this
            # path. Preserve whatever streamed before the stop as a partial
            # result.
            if not info.result and info.streaming_text:
                info.result = info.streaming_text
        # _force_reap emits the (single) stopped-aware ``subagent_done`` event
        # and drives _on_done delivery — no second event here.
        #
        # Run as a TRACKED task, awaited here: this reap lives in the caller's
        # task (a request handler, a parent's teardown), which ``cancel_all``
        # does not know. Tracked, a gateway shutdown cancels it beside the
        # reaper task, so its cancellation arm finishes the record and releases
        # the report inside the drain instead of sitting in a hanging reset
        # until the shutdown budget hard-exits the process. Awaiting the task
        # keeps the caller's own cancellation reaching the reap as before
        # (cancelling an awaiter cancels the future it waits on).
        reap = asyncio.ensure_future(
            self._manager._force_reap(
                agent_id,
                info,
                time.time() - info.started,
                reason=info._reap_reason,
            )
        )
        self._manager._reap_tasks.add(reap)
        reap.add_done_callback(self._manager._reap_tasks.discard)
        await reap
        return True

    async def cancel_all_impl(self) -> None:
        """Cancel all running subagents and wait for cleanup."""
        # Shutdown-driven cancellations must never trigger the one-shot
        # unexpected-cancel auto-continue (the loop is going away).
        self._manager._shutting_down = True
        retained_retry = self._manager._retained_claim_retry_handle
        if retained_retry is not None and not retained_retry.cancelled():
            self._manager._cancel_task_intentionally(
                retained_retry,
                reason="shutdown retained claim retry",
            )
        self._manager._retained_claim_retry_handle = None
        for depth_retry in self._manager._queue_depth_retries.values():
            self._manager._cancel_task_intentionally(
                depth_retry.handle,
                reason="shutdown queue depth retry",
            )
        self._manager._queue_depth_retries.clear()
        pressure_recheck = self._manager._pressure_recheck_handle
        if pressure_recheck is not None and not pressure_recheck.cancelled():
            self._manager._cancel_task_intentionally(
                pressure_recheck,
                reason="shutdown memory-pressure recheck",
            )
        self._manager._pressure_recheck_handle = None
        memory_sampler = getattr(self._manager, "_memory_sampler_handle", None)
        if memory_sampler is not None and not memory_sampler.cancelled():
            self._manager._cancel_task_intentionally(
                memory_sampler,
                reason="shutdown memory sampler",
            )
        self._manager._memory_sampler_handle = None
        memory_sample = getattr(self._manager, "_memory_sample_task", None)
        if memory_sample is not None and not memory_sample.done():
            self._manager._cancel_task_intentionally(
                memory_sample,
                reason="shutdown memory sampler tick",
            )
        self._manager._memory_sample_task = None
        memory_fit = getattr(self._manager, "_memory_fit_task", None)
        if memory_fit is not None and not memory_fit.done():
            self._manager._cancel_task_intentionally(
                memory_fit,
                reason="shutdown memory fit pass",
            )
        self._manager._memory_fit_task = None
        # Do not clear or release retained claims here. Their durable rows are
        # still ADMITTED, so process teardown ends the in-memory reservation and
        # the next boot reconciles them to QUEUED as one atomic ownership change.
        if self._manager._reaper_task and not self._manager._reaper_task.done():
            self._manager._reaper_task.cancel()
            self._manager._reaper_task = None
        # The reaps that live outside the reaper task (a Stop, a parent-end
        # cancel, each awaited in its caller's task) are cancelled the same way
        # and gathered here, so each one's cancellation arm has finished the
        # record and released its report before the run tasks are cancelled and
        # the reports drained. Left alone, such a reap sat in its hanging reset
        # with its report waiting on a gate nobody released, and the gateway's
        # shutdown budget hard-exited the process before the drain could abandon
        # it -- the tombstone its run's arm wrote then excluded the folder from
        # orphan recovery, so the parent never received the completion.
        inflight_reaps = [t for t in self._manager._reap_tasks if not t.done()]
        for reap in inflight_reaps:
            self._manager._cancel_task_intentionally(reap, reason="shutdown reap")
        if inflight_reaps:
            await asyncio.gather(*inflight_reaps, return_exceptions=True)
        # Follow-up watchers are cancelled and gathered before announcing.
        # The announce awaits — _on_done injection can be slow — and
        # a busy-retry watcher waking during that await could dispatch a
        # continuation into the shutting-down gateway, so every watcher task
        # must be DEAD before anything here yields. Announcing afterwards is
        # safe: the settle-after-outcome protocol leaves undelivered messages
        # in their queues, so each is still present to be reported. An
        # ACCEPTED follow-up must not die silently: the spawn_steer reply
        # promised the parent a completion event, so each non-empty queue is
        # announced as a synthetic failure — the parent learns the message was
        # dropped instead of waiting forever.
        # Snapshot ids BEFORE cancelling: each watcher's done-callback pops it
        # from the dict as the gather completes it, so a post-gather snapshot
        # is already empty.
        watcher_ids = list(self._manager._followup_watchers)
        watcher_infos = dict(self._manager._followup_watcher_infos)
        followup_watchers = [t for t in self._manager._followup_watchers.values() if not t.done()]
        for followup_watcher in followup_watchers:
            followup_watcher.cancel()
        if followup_watchers:
            await asyncio.gather(*followup_watchers, return_exceptions=True)
        self._manager._followup_watchers.clear()
        self._manager._followup_watcher_parents.clear()
        self._manager._followup_watcher_infos.clear()
        for agent_id in watcher_ids:
            watcher_info = watcher_infos.get(agent_id) or self._manager._agents.get(agent_id)
            if watcher_info is not None and watcher_info.pending_followups:
                dropped = list(watcher_info.pending_followups)
                watcher_info.pending_followups = []
                self._manager._audit_followup(watcher_info, "followup_expired")
                try:
                    await self._manager._announce_followup_failure(
                        watcher_info,
                        "follow_up dropped: the gateway is shutting down before "
                        "the run completed; the queued message(s) were not "
                        "dispatched",
                        messages=dropped,
                    )
                except Exception:  # noqa: BLE001 - shutdown must not wedge here
                    logger.debug(
                        "shutdown follow_up announce failed for %s", agent_id, exc_info=True
                    )
        tasks_to_await: list[asyncio.Task] = []  # type: ignore[type-arg]
        for agent_id, task in list(self._manager._tasks.items()):
            if not task.done():
                # _shutting_down (set above) is the terminal marker for this
                # site; the chokepoint enforces the contract mechanically.
                self._manager._cancel_task_intentionally(
                    task, self._manager._agents.get(agent_id), reason="shutdown"
                )
                tasks_to_await.append(task)
        if tasks_to_await:
            await asyncio.gather(*tasks_to_await, return_exceptions=True)
        self._manager._tasks.clear()
        # Shielded terminal reports keep running after their awaiter is
        # cancelled (that is the point). Drain them with a BOUNDED wait so a
        # report is not orphaned by a closing event loop, without letting a
        # wedged injection block shutdown indefinitely.
        #
        # A drained task can start another one: a Stop all's tracked applier
        # spawns each row's queued-stop report once the writer-thread job
        # answers, which is after the first snapshot here. So the drain
        # re-reads ``_report_tasks`` and waits for what joined it, all inside
        # the one budget, and whatever joined it is a straggler like the rest.
        pending_reports = [t for t in self._manager._report_tasks if not t.done()]
        if pending_reports:
            drain_deadline = asyncio.get_running_loop().time() + _REPORT_DRAIN_TIMEOUT
            waiting = list(pending_reports)
            # The membership test runs over every report task on each pass, so
            # it reads a set: a list made a large shutdown's drain quadratic.
            seen = set(pending_reports)
            while True:
                remaining = drain_deadline - asyncio.get_running_loop().time()
                try:
                    await asyncio.wait(waiting, timeout=max(0.0, remaining))
                except Exception:
                    logger.debug("cancel_all: report drain wait failed", exc_info=True)
                    break
                joined = [t for t in self._manager._report_tasks if not t.done() and t not in seen]
                seen.update(joined)
                pending_reports.extend(joined)
                if not joined or asyncio.get_running_loop().time() >= drain_deadline:
                    break
                waiting = [t for t in pending_reports if not t.done()]
            # `asyncio.wait` RETURNS on timeout without touching the stragglers.
            # Leaving them pending is worse than not shielding at all: shutdown
            # would proceed while they keep invoking `_on_done` against
            # tearing-down state, and they would then die when the loop closes —
            # losing the very report the shield exists to guarantee. So cancel
            # them explicitly and gather to completion, which also surfaces any
            # exception into the log instead of an "exception was never
            # retrieved" warning at interpreter exit.
            stragglers = [t for t in pending_reports if not t.done()]
            if stragglers:
                # The set also holds writes that carry no completion (a run's
                # usage row, a posted task-queue write): only a task with a
                # report owner is a completion that may go undelivered.
                abandoned = [self._manager._report_owners.get(t) for t in stragglers]
                logger.warning(
                    "cancel_all: %d pending report/write task(s) did not drain in %.0fs — "
                    "cancelling; %d of them carry a subagent completion that may not "
                    "have been delivered",
                    len(stragglers),
                    _REPORT_DRAIN_TIMEOUT,
                    sum(owner is not None for owner in abandoned),
                )
                for report_task in stragglers:
                    report_task.cancel()
                try:
                    await asyncio.gather(*stragglers, return_exceptions=True)
                except Exception:
                    logger.debug("cancel_all: straggler gather failed", exc_info=True)
                # A cancelled report is a LOST delivery, and the terminal record
                # for it was already written — including a tombstone, which is
                # exactly what `list_orphans()` uses to exclude a folder from the
                # next start's reconciliation. Left alone, the outcome is
                # unrecoverable: never injected, and invisible to the one path
                # that could still inject it.
                #
                # Extending the drain to `_ON_DONE_TIMEOUT` instead was rejected:
                # it would hold gateway shutdown for up to 20 minutes on a single
                # wedged injection, which is what the bounded drain exists to
                # prevent. Bounded shutdown plus recoverable state is strictly
                # better than unbounded shutdown.
                #
                # Only reports cancelled BEFORE `_on_done` returned are re-admitted
                # — `_reported_to_parent` marks the ones that already reached the
                # parent, so a cancellation in the later teardown/tombstone waits
                # does not cause a duplicate delivery on restart.
                for task, owner in zip(stragglers, abandoned):
                    if owner is None or not task.cancelled():
                        continue
                    if owner._reported_to_parent:
                        continue
                    try:
                        if clear_tombstone(owner.id):
                            logger.warning(
                                "cancel_all: %s's completion was not delivered — "
                                "re-admitted to orphan recovery for the next start",
                                owner.id,
                            )
                    except Exception:
                        logger.debug(
                            "cancel_all: failed to re-admit %s to orphan recovery",
                            owner.id,
                            exc_info=True,
                        )
