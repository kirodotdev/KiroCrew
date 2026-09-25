"""Cancellation behavior for the SubagentManager facade."""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping, Sequence

from ._component import ManagerComponent

if TYPE_CHECKING:
    from ..subagent import (
        _MAX_FOLLOWUP_MESSAGE_CHARS,
        _MAX_PENDING_FOLLOWUPS,
        _ON_DONE_TIMEOUT,
        _RECOVERY_SLOT_WAIT_SECS,
        _REPORT_DRAIN_TIMEOUT,
        _RESET_TIMEOUT,
        Stats,
        SubagentInfo,
        _audit_ids,
        asyncio,
        clear_tombstone,
        delivery_is_parked,
        logger,
        orphan_recovery_can_see,
        stage_boundary_owner_for_run,
        time,
    )


class CancellationCoordinator(ManagerComponent):
    """Own cancellation transitions while state remains facade-owned."""

    __slots__ = ()

    def _schedule_cancel_recovery_impl(self, info: SubagentInfo) -> None:
        """Respawn *info*'s run on a fresh task after an unexpected cancellation.

        Called from ``_run``'s CancelledError handler — the current task is
        being cancelled and cannot continue itself, so the continuation runs on
        a new task. One-shot: gated by ``info._cancel_retry_used`` at the call
        site. The original run's finally block still performs session cleanup
        (release/reset) but skips terminal finalization while ``_recovering``.

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
                    info.error = "cancelled (recovery failed)"
                    info.elapsed = time.time() - info.started
                    Stats().inc_subagent_failed()
                    self._manager._write_tombstone(info, "cancelled")
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
        for index, params in enumerate(self._manager._queue):
            if params.get("_resume_id") or str(params.get("_preassigned_id") or "") != agent_id:
                continue
            dropped = self._manager._queue.pop(index)
            store = admission.taskq_store()
            if stored is None and store is not None:
                # A window entry always HAS a row while a store is attached -- a
                # spawn whose accept the store refused is never queued -- so
                # nothing cancelled here means the cancel did not LAND: the store
                # was unreachable, or the row left the unstarted states between
                # the read and the write. The caller publishes a stop either way,
                # and a row left `queued` is dispatchable by the next
                # incarnation, which would run work the user was told had
                # stopped. So the refusal is audible (the shape `taskq_settle`
                # uses for a refused `finish`) and re-posted to the writer
                # thread, where a store that answers again cancels the row;
                # `taskq_cancel_queued` re-reads the state under its own
                # transaction, so a row that legitimately started is left alone.
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
            try:
                self._manager._emit_queue_depth(
                    str(dropped.get("parent_session_key", "")),
                    str(dropped.get("batch_id", "")),
                )
            except Exception:
                logger.debug("queue-depth re-emit failed after unqueue", exc_info=True)
            return dropped
        return stored

    def _report_queued_stop_impl(self, params: dict) -> None:
        """Publish a neutral terminal record for work stopped before startup."""
        info = SubagentInfo(
            id=str(params.get("_preassigned_id") or ""),
            task=str(params.get("task") or "(stopped before start)"),
            parent_session_key=str(params.get("parent_session_key") or ""),
            _stage_boundary_owner=str(params.get("_stage_boundary_owner") or ""),
            agent=str(params.get("agent") or ""),
            user_stopped=True,
            queued=True,
            batch_id=str(params.get("batch_id") or ""),
            batch_total=max(0, int(params.get("batch_total") or 0)),
        )
        if not info.id:
            return
        # Queued runs have no `_agents` record yet. Register every synthetic
        # terminal before report tasks can run, leaving `done=False` until each
        # task starts. That keeps earlier reports from treating themselves as
        # the final batch member and flushing a partial digest while sibling
        # queued-stop reports are still pending.
        self._manager._agents[info.id] = info
        if not self._manager._claim_finalize(info):
            self._manager._agents.pop(info.id, None)
            return
        self._manager._spawn_terminal_report(
            info,
            source="Queued stop",
            injection_timeout_reason="delivery timed out after queued subagent stop",
            mark_delivered_on_success=False,
            settle_digest=True,
        )

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
        def _parked_on_an_unanswered_approval(info: "SubagentInfo") -> bool:
            return bool(getattr(info, "_awaiting_approval", False)) and (
                getattr(info, "_exec_started", None) is None
            )

        live = [
            info.id
            for info in mine
            if not info.done and not _parked_on_an_unanswered_approval(info)
        ]
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
            info.id for info in mine if not info.done and _parked_on_an_unanswered_approval(info)
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
            for params in self._manager._queue
            if params.get("parent_session_key", "") == parent_session_key
            and not params.get("_resume_id")
        ]
        selected = tuple(agent_id for agent_id in [*live, *queued] if agent_id)
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
    ) -> int:
        """Stop exactly the runs in *agent_ids*, reporting none of them home.

        The cancellation half. It takes IDS rather than a parent key so that what
        is stopped was decided by :meth:`snapshot_teardown_children_impl` at a
        point where the answer could not be contaminated — a key would be
        re-resolved here, which is the whole defect.

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
        #     starts into whatever the key serves next. Same for a durable row that has
        #     spilled out of the in-memory window.
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
        # an await, and an await here cannot tell work belonging to the retired
        # conversation from work a successor under the same key has just started -- which
        # needs a conversation-incarnation counter the session layer does not have.
        # Tracked as a follow-up.
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

        stopped = 0
        for agent_id in agent_ids:
            if not agent_id:
                continue
            info = self._manager._agents.get(agent_id)
            if info is not None and not info.done:
                # A LIVE run goes through the ordinary reap, which does no store
                # work of its own. The stop's cause and origin are written on the
                # record first: the run's own record and log then name the parent
                # end that stopped it, not the runtime death the reap's teardown
                # caused, and ``cancel`` carries the cause into the tombstone.
                # First stopper wins: a user Stop or a stage cancel already in
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
            # cancel-did-not-land retry and the queue-depth re-emit.
            try:
                params = await self._manager._admission.taskq_cancel_queued_async(
                    # A teardown may not cancel a CLAIMED-but-unstarted row. Its claimer
                    # sits between the claim and the registration, so cancelling here
                    # leaves that claimer to register and run work this teardown believed
                    # it had stopped -- and the claimer's own state re-read before
                    # registering has nothing to catch, because the row is gone rather
                    # than claimable. A row in that window may also be carrying a person's
                    # decision (a spawn approval is the visible case), which a teardown has
                    # no standing to revoke for them. Stop-all keeps the wider behaviour:
                    # there the user asked for exactly that.
                    agent_id,
                    allow_admitted=False,
                )
                entry = self._manager._unqueue(agent_id, stored=params, store_cancelled=True)
                if entry is not None:
                    self._manager._report_queued_stop(entry)
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
        queued_stopped = self._stop_queued(
            [
                str(params.get("_preassigned_id") or "")
                for params in self._manager._queue
                if params.get("parent_session_key", "") == parent_session_key
                and not params.get("_resume_id")
            ]
        )
        # This parent's rows waiting outside the in-memory window. The read is
        # a store read, so it comes AFTER the in-memory queue is drained: its
        # await is the first suspension point this method has, and one taken
        # before the drain would let a stagger timer start a queued agent. A row
        # started from disk during it is caught by the running sweep below.
        queued_stopped += self._stop_queued(
            await self._manager._admission.taskq_pending_ids_for_async(parent_session_key)
        )

        running_ids = [
            info.id
            for info in self._manager._agents.values()
            if info.parent_session_key == parent_session_key
            and not info.done
            and not info.queued
            and not (info._awaiting_approval and info._exec_started is None)
        ]
        results = await asyncio.gather(
            *(self._manager.cancel(agent_id) for agent_id in running_ids),
            return_exceptions=True,
        )
        running_stopped = sum(result is True for result in results)
        return (running_stopped, queued_stopped)

    def _boundary_scope_matches_impl(
        self,
        params: Mapping[str, object],
        parent_session_key: str,
        boundary_owner: str,
    ) -> bool:
        return (
            str(params.get("parent_session_key") or "") == parent_session_key
            and str(params.get("_stage_boundary_owner") or "") == boundary_owner
        )

    def _boundary_cancellation_pending_impl(self, params: Mapping[str, object]) -> bool:
        """Whether exact cancellation authority forbids dispatch of *params*."""
        parent = str(params.get("parent_session_key") or "")
        owner = str(params.get("_stage_boundary_owner") or "")
        return bool(
            parent
            and owner
            and (
                (parent, owner) in self._manager._pending_boundary_cancellations
                or self._manager.boundary_cancellation_refused(parent, owner)
            )
        )

    def boundary_cancellation_pending_reason_impl(
        self,
        parent_session_key: str,
        boundary_owner: str,
    ) -> str:
        """Latest settlement failure or cap refusal for one exact scope."""
        retained = self._manager._pending_boundary_cancellations.get(
            (parent_session_key, boundary_owner),
            "",
        )
        return retained or self._manager._boundary_cancellation_refusal(
            parent_session_key,
            boundary_owner,
        )

    def _schedule_boundary_cancel_retry_impl(self) -> None:
        """Arm one later pump pass for unresolved durable cancellations."""
        if not self._manager._pending_boundary_cancellations:
            return
        pending = self._manager._boundary_cancel_retry_handle
        if pending is not None and not pending.cancelled():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        def _retry() -> None:
            self._manager._boundary_cancel_retry_handle = None
            self._manager._drain_queue()

        delay = max(0.05, self._manager._admission.taskq_admit_wait_secs())
        self._manager._boundary_cancel_retry_handle = loop.call_later(delay, _retry)

    def _apply_boundary_cancelled_rows_impl(
        self,
        parent_session_key: str,
        boundary_owner: str,
        cancelled: list[dict],
        *,
        settled: bool,
    ) -> int:
        """Apply confirmed store cancels to loop-owned queue state and reports."""
        by_id = {
            str(params.get("_preassigned_id") or ""): params
            for params in cancelled
            if params.get("_preassigned_id")
        }
        stopped = 0
        dropped_rows: list[dict] = []
        for index in range(len(self._manager._queue) - 1, -1, -1):
            params = self._manager._queue[index]
            if params.get("_resume_id") or not self._manager._boundary_scope_matches(
                params,
                parent_session_key,
                boundary_owner,
            ):
                continue
            agent_id = str(params.get("_preassigned_id") or "")
            if not settled and agent_id not in by_id:
                continue
            dropped_rows.append(self._manager._queue.pop(index))
            by_id.pop(agent_id, None)
        for dropped in reversed(dropped_rows):
            self._manager._report_queued_stop(dropped)
            self._manager._emit_queue_depth(
                str(dropped.get("parent_session_key") or ""),
                str(dropped.get("batch_id") or ""),
            )
            stopped += 1
        for agent_id, params in by_id.items():
            if agent_id in self._manager._agents:
                continue
            self._manager._report_queued_stop(params)
            self._manager._emit_queue_depth(
                str(params.get("parent_session_key") or ""),
                str(params.get("batch_id") or ""),
            )
            stopped += 1
        if settled:
            self._manager._pending_boundary_cancellations.pop(
                (parent_session_key, boundary_owner),
                None,
            )
        return stopped

    async def _settle_boundary_queue_impl(
        self,
        parent_session_key: str,
        boundary_owner: str,
    ) -> int:
        """Cancel one scope's durable queued rows without blocking the loop."""
        cancelled: list[dict] = []
        failure = ""
        try:
            cancelled, failure = await self._manager._admission.taskq_cancel_boundary_async(
                parent_session_key,
                boundary_owner,
            )
        except Exception as exc:
            failure = str(exc).strip() or type(exc).__name__
            logger.warning(
                "Stage-boundary queued cancellation failed for parent=%s owner=%s",
                parent_session_key,
                boundary_owner,
                exc_info=True,
            )
        if failure:
            failure = self._manager._bounded_boundary_cancellation_failure(failure)
        settled = not failure
        stopped = self._manager._apply_boundary_cancelled_rows(
            parent_session_key,
            boundary_owner,
            cancelled,
            settled=settled,
        )
        if failure:
            self._manager._pending_boundary_cancellations[(parent_session_key, boundary_owner)] = (
                failure
            )
            logger.warning(
                "Stage-boundary queued cancellation remains pending for " "parent=%s owner=%s: %s",
                parent_session_key,
                boundary_owner,
                failure,
            )
            self._manager._schedule_boundary_cancel_retry()
        elif not self._manager._pending_boundary_cancellations:
            pending = self._manager._boundary_cancel_retry_handle
            if pending is not None and not pending.cancelled():
                self._manager._cancel_task_intentionally(
                    pending,
                    reason="boundary cancellation settled",
                )
            self._manager._boundary_cancel_retry_handle = None
        return stopped

    async def retry_pending_boundary_cancellations_impl(self) -> None:
        """Retry exact durable cancels before the pump may dispatch a row."""
        for parent_session_key, boundary_owner in tuple(
            self._manager._pending_boundary_cancellations
        ):
            await self._manager._settle_boundary_queue(
                parent_session_key,
                boundary_owner,
            )

    def _revoke_boundary_owners_impl(
        self,
        parent_session_key: str,
        boundary_owner: str,
    ) -> tuple[SubagentInfo, ...]:
        """Revoke every in-process record owned by one exact boundary."""
        candidates = (
            *self._manager._agents.values(),
            *self._manager._report_owners.values(),
            *getattr(self._manager, "_followup_watcher_infos", {}).values(),
        )
        matching: list[SubagentInfo] = []
        seen: set[int] = set()
        for info in candidates:
            identity = id(info)
            if identity in seen:
                continue
            seen.add(identity)
            if (
                info.parent_session_key != parent_session_key
                or stage_boundary_owner_for_run(info) != boundary_owner
            ):
                continue
            matching.append(info)
            info.user_stopped = True
            info._stage_boundary_cancelled = True
            # The reap this revocation leads to reads these: the tombstone and
            # the run's own stop line then say a stage was cancelled, not that
            # the user pressed Stop. First stopper wins -- a cancel already in
            # flight keeps its own attribution.
            if not info._reap_reason:
                info._reap_reason = "stage_cancel"
            if not info._stop_origin:
                info._stop_origin = f"stage cancelled ({boundary_owner})"
            if info.pending_followups:
                info.pending_followups = []
                self._manager._audit_followup(info, "followup_suppressed")
            watcher = self._manager._followup_watchers.get(info.id)
            if watcher is not None and not watcher.done():
                self._manager._cancel_task_intentionally(
                    watcher,
                    info,
                    reason="stage boundary cancelled",
                )
        self._manager.discard_report_failures(parent_session_key, boundary_owner)
        return tuple(info for info in matching if not info.done and not info.queued)

    async def cancel_for_boundary_impl(
        self,
        parent_session_key: str,
        boundary_owner: str,
        *,
        retain_scope: bool = True,
    ) -> tuple[int, int]:
        """Stop work owned by one exact stage boundary, including approval waits."""
        if not parent_session_key or not boundary_owner:
            return (0, 0)
        refusal = (
            self._manager._hold_boundary_cancellation(
                parent_session_key,
                boundary_owner,
            )
            if retain_scope
            else self._manager._boundary_cancellation_refusal(
                parent_session_key,
                boundary_owner,
            )
        )
        # The scope decision is synchronous. Revoke live work, completed reports,
        # and watcher-owned follow-ups before any durable writer can suspend.
        live_infos = self._manager._revoke_boundary_owners(
            parent_session_key,
            boundary_owner,
        )
        if refusal:
            results = await asyncio.gather(
                *(self._manager.cancel(info.id) for info in live_infos),
                return_exceptions=True,
            )
            return (sum(result is True for result in results), 0)
        queued_stopped = await self._manager._settle_boundary_queue(
            parent_session_key,
            boundary_owner,
        )
        results = await asyncio.gather(
            *(self._manager.cancel(info.id) for info in live_infos),
            return_exceptions=True,
        )
        return (sum(result is True for result in results), queued_stopped)

    def _stop_queued(self, agent_ids: Sequence[str]) -> int:
        """Unqueue each id that is still waiting and report it stopped; count them.

        A SEQUENCE, not an iterable: ``_unqueue`` mutates ``_queue``, so a lazy
        generator over it would stop short of the ids it was asked to remove.
        """
        stopped = 0
        for agent_id in agent_ids:
            if not agent_id:
                continue
            queued = self._manager._unqueue(agent_id)
            if queued is None:
                continue
            self._manager._report_queued_stop(queued)
            stopped += 1
        return stopped

    async def cancel_impl(self, agent_id: str) -> bool:
        """Cancel a single running subagent. Returns True if found and cancelled.

        User-initiated stop is a neutral terminal state, not an error: partial
        output is preserved on the info record (and remains in result.txt), the
        tombstone is written as ``user_stop``, and the ``subagent_done`` event
        carries ``stopped: true`` so the UI renders a neutral "stopped" card.

        A caller that is NOT the user pressing Stop names itself on the record
        first: the parent-end teardown and the stage-boundary cancel write
        ``info._reap_reason`` (the tombstone cause -- ``parent_end``,
        ``stage_cancel``) and ``info._stop_origin`` (the one-line who/why)
        before calling here, and both ride into the reap unchanged. Nothing is
        inferred from the origin text. The run loop reads the same fields when
        its stream dies under the reap, so it reports that stop rather than the
        death the stop caused (see ``_run``'s reap-echo arm).
        """
        info = self._manager._agents.get(agent_id)
        if not info or info.done:
            # A run still WAITING behind the stagger has no `_agents` record at
            # all: `spawn` builds its queued SubagentInfo and returns it without
            # registering. Unqueueing prevents startup; the synthetic terminal
            # report keeps its parent and batch accounting from waiting forever.
            queued = self._manager._unqueue(agent_id)
            if queued is not None:
                logger.info("Cancelled queued subagent %s before it started", agent_id)
                self._manager._report_queued_stop(queued)
                return True
            return False
        info.user_stopped = True
        # Recorded BEFORE the reap so the run loop can read them when its stream
        # dies under the session teardown ``_force_reap`` is about to do. A
        # caller that already named the cause keeps it; a bare cancel is the
        # user pressing Stop.
        if not info._reap_reason:
            info._reap_reason = "user_stop"
        if not info._stop_origin:
            info._stop_origin = "stopped by user"
        # Neutral semantics live in the RECORD, not just the live event: a user
        # stop leaves ``error`` unset so every consumer (reconnect snapshots,
        # tombstones, /api/spawn listing, orphan reconciliation) derives the
        # same neutral "stopped" status without having to cross-check
        # ``user_stopped``. _force_reap is also user_stopped-aware and will not
        # synthesize a reap error for this path.
        # Preserve whatever streamed before the stop as a partial result.
        if not info.result and info.streaming_text:
            info.result = info.streaming_text
        # _force_reap emits the (single) stopped-aware ``subagent_done`` event
        # and drives _on_done delivery — no second event here.
        await self._manager._force_reap(
            agent_id,
            info,
            time.time() - info.started,
            reason=info._reap_reason,
        )
        return True

    async def cancel_all_impl(self, cancellation_budget: float | None = None) -> None:
        """Cancel all running subagents and wait for cleanup.

        ``cancellation_budget`` bounds EVERY phase of this coroutine that can block, for a
        caller that is itself on a deadline -- the gateway passes what is left of its
        graceful-shutdown budget. ``None`` keeps the previous defaults: an unbounded wait
        for the runs to come down and the module default for the report drain, which is
        right when nothing is waiting on the caller.

        Bounding only ONE phase is not enough and was the earlier mistake: whichever phase
        is left unbounded becomes the one that spends the whole budget, and the caller's
        later steps starve exactly as before. So each blocking phase below sizes itself
        from what REMAINS, and no single wedged task can consume the lot.

        The straggler compensation at the end is deliberately NOT bounded by it. That
        compensation, and the ``clear_tombstone`` re-admission it ends in, are the only
        things that keep an undelivered completion visible to the next start's orphan
        recovery, so a budget that could cut them would trade a bounded shutdown for a
        silent, permanent loss.

        For the same reason this is a budget rather than something a caller wraps around
        the coroutine: an outer ``wait_for`` cancels at whichever await is live, including
        the window between the report drain and that re-admission.
        """
        # The loop clock is the right one for asyncio deadlines, and using it keeps this
        # module's imports unchanged.
        deadline: float | None = (
            None
            if cancellation_budget is None
            else asyncio.get_running_loop().time() + max(cancellation_budget, 0.0)
        )

        def _remaining(default: float | None) -> float | None:
            """Seconds the next blocking phase may spend: the rest of the budget.

            ``default`` is what that phase used before any budget existed, and is returned
            unchanged when the caller gave none. Never negative: a spent budget means "do
            not wait", which is still a legal timeout, and never a skipped phase.
            """
            if deadline is None:
                return default
            return max(deadline - asyncio.get_running_loop().time(), 0.0)

        def _readmit_for_next_start(agent_id: str, because: str) -> None:
            """Remove *agent_id*'s tombstone so the next start's orphan recovery sees it.

            ``list_orphans`` skips any folder that HAS a tombstone, so the tombstone
            alone decides whether a persisted obligation is reachable -- removing it is
            the discharge, and the write before it is not.

            Reached when an outer cancellation took away the ANSWER this decision would
            otherwise be made on, so it deliberately does not ask whether the obligation
            is certainly outstanding: re-admitting a run that did deliver costs one
            duplicate notice on the next start, while keeping the tombstone costs the
            message itself. Never raises, because a raise here replaces a recoverable
            outcome with the unrecoverable one it exists to prevent.
            """
            try:
                clear_tombstone(agent_id)
                if orphan_recovery_can_see(agent_id):
                    logger.warning(
                        "cancel_all: %s %s -- re-admitted to orphan recovery for the " "next start",
                        agent_id,
                        because,
                    )
                else:
                    logger.warning(
                        "cancel_all: %s %s AND its tombstone could not be removed, so "
                        "the next start skips the run",
                        agent_id,
                        because,
                    )
            except Exception:
                logger.debug(
                    "cancel_all: failed to re-admit %s to orphan recovery",
                    agent_id,
                    exc_info=True,
                )

        # Shutdown-driven cancellations must never trigger the one-shot
        # unexpected-cancel auto-continue (the loop is going away).
        self._manager._shutting_down = True
        boundary_retry = self._manager._boundary_cancel_retry_handle
        if boundary_retry is not None and not boundary_retry.cancelled():
            self._manager._cancel_task_intentionally(
                boundary_retry,
                reason="shutdown boundary cancellation retry",
            )
        self._manager._boundary_cancel_retry_handle = None
        self._manager._pending_boundary_cancellations.clear()
        retained_retry = self._manager._retained_claim_retry_handle
        if retained_retry is not None and not retained_retry.cancelled():
            self._manager._cancel_task_intentionally(
                retained_retry,
                reason="shutdown retained claim retry",
            )
        self._manager._retained_claim_retry_handle = None
        # Do not clear or release retained claims here. Their durable rows are
        # still ADMITTED, so process teardown ends the in-memory reservation and
        # the next boot reconciles them to QUEUED as one atomic ownership change.
        if self._manager._reaper_task and not self._manager._reaper_task.done():
            self._manager._reaper_task.cancel()
            self._manager._reaper_task = None
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
            # Bounded like every other blocking phase, and bounded with ``asyncio.wait``
            # rather than a ``wait_for`` around a gather: these tasks are ALREADY cancelled
            # above, and wait_for would cancel again and then AWAIT the result, so one
            # watcher slow to honour its cancel would still hold this phase for as long as
            # it liked. What has to be bounded is how long this site waits, not when it asks.
            #
            # Letting go is safe because dispatch is gated mechanically on
            # ``_shutting_down`` (set at the top of this method, checked in continuation.py
            # and run.py before any continuation is dispatched), so a watcher that wakes
            # after this wait cannot reach the shutting-down gateway. Waiting without a
            # bound is what let a single watcher spend the whole budget before the phases
            # below it ever started -- including the tombstone re-admission that keeps an
            # undelivered completion visible.
            _finished, still_cancelling = await asyncio.wait(
                followup_watchers, timeout=_remaining(None)
            )
            if still_cancelling:
                logger.warning(
                    "cancel_all: %d follow-up watcher(s) did not finish cancelling "
                    "within the cancellation budget; continuing to the announcements",
                    len(still_cancelling),
                )
        self._manager._followup_watchers.clear()
        self._manager._followup_watcher_parents.clear()
        self._manager._followup_watcher_infos.clear()
        for agent_id in watcher_ids:
            watcher_info = watcher_infos.get(agent_id) or self._manager._agents.get(agent_id)
            if watcher_info is not None and watcher_info.pending_followups:
                dropped = list(watcher_info.pending_followups)
                self._manager._audit_followup(watcher_info, "followup_expired")
                announce_failure = ""
                try:
                    # Bounded: this awaits ``_on_done`` injection, which the comment above
                    # records as slow, and it runs once PER watcher -- so unbounded it is
                    # the phase most able to spend a whole shutdown on one slow parent and
                    # leave the drain and the tombstone re-admission below it unreached.
                    announced = await asyncio.wait_for(
                        self._manager._announce_followup_failure(
                            watcher_info,
                            "follow_up dropped: the gateway is shutting down before "
                            "the run completed; the queued message(s) were not "
                            "dispatched",
                            messages=dropped,
                        ),
                        timeout=_remaining(None),
                    )
                    if not announced:
                        # The announcement contains its own failures -- no ``_on_done`` to
                        # reach, or one that raised -- so the only failure an exception
                        # could report here is this site's own timeout. Reading its answer
                        # is what keeps a swallowed failure from looking like delivery and
                        # clearing the queue below.
                        announce_failure = "was not delivered"
                except asyncio.TimeoutError:
                    announce_failure = "exceeded the cancellation budget"
                except Exception:  # noqa: BLE001 - shutdown must not wedge here
                    # A RAISE loses the queue exactly as a timeout does, so it takes the same
                    # durability step rather than only a debug line. Two triggers, one
                    # remedy: the reason differs, the obligation does not.
                    announce_failure = "failed"
                    logger.debug(
                        "shutdown follow_up announce failed for %s", agent_id, exc_info=True
                    )
                if announce_failure:
                    # In-memory retention buys nothing HERE: the process is exiting, so
                    # leaving ``pending_followups`` set preserves the queue only until it
                    # dies with the gateway. The obligation has to reach DISK.
                    #
                    # It must reach disk the way orphan recovery actually READS: a tombstone
                    # is the wrong instrument, because ``list_orphans`` skips any folder that
                    # HAS one -- writing a tombstone would hide this run from the next start
                    # rather than hand it over. So persist the queue into the run's state and
                    # re-admit the folder, which is the same pair the straggler compensation
                    # below uses for an undelivered completion.
                    #
                    # ``update_state`` returns False when it SKIPS (state missing or corrupt,
                    # e.g. the reaper already deleted the record), and a durability contract
                    # has to see that rather than read a silent no-op as success. A
                    # non-persistent run is updated in memory only and reports True, which is
                    # correct: it must leave no disk residue and has no next-boot state to be
                    # recovered into.
                    # Off the loop, via the one writer every in-run ``state.json`` write goes
                    # through: ``update_state`` ends in a synchronous fsync, and this is a
                    # coroutine on the gateway's only event loop, so calling it directly
                    # would freeze the loop on a slow filesystem. `test_subagent_provenance_
                    # write.py` enforces exactly that, and it is right to.
                    #
                    # NOT wrapped in a timeout of my own, deliberately. Cancelling a
                    # ``to_thread`` await detaches the worker WITHOUT stopping it, and
                    # ``update_state`` rewrites the whole file from the snapshot it already
                    # read -- so a detached worker rolls back fields it never named. This
                    # writer owns that hazard and drains its worker on cancellation under its
                    # own bound; a second bound wrapped around it would re-create the exact
                    # rollback it exists to prevent.
                    # Written under the SAME two bounds admission refuses past, because a
                    # queue can also reach here from a run admitted before a lower cap was
                    # in force. A bare slice would be the defect the bound exists to close
                    # one step later: the messages that do not fit are gone AND nothing
                    # records that anything was dropped. So the overflow travels as a
                    # count beside the messages that fit, and the next start's notice can
                    # say how many it cannot name.
                    persist_messages = [
                        str(m)[:_MAX_FOLLOWUP_MESSAGE_CHARS]
                        for m in dropped[:_MAX_PENDING_FOLLOWUPS]
                    ]
                    overflow = len(dropped) - len(persist_messages)
                    if overflow:
                        logger.warning(
                            "shutdown follow_up handover for %s holds %d message(s), above "
                            "the %d cap; persisting the first %d and recording %d as "
                            "unnamed overflow",
                            agent_id,
                            len(dropped),
                            _MAX_PENDING_FOLLOWUPS,
                            len(persist_messages),
                            overflow,
                        )
                    try:
                        persisted = await self._manager._write_state_off_loop(
                            watcher_info,
                            "followup_undelivered",
                            pending_followups=persist_messages,
                            pending_followups_overflow=overflow,
                        )
                    except asyncio.CancelledError:
                        # The writer itself survives this: ``_write_state_off_loop``
                        # shields it and drains it before re-raising, so the queue may
                        # well be on disk. What cancellation takes is the ANSWER, and
                        # with it the re-admission below -- which is the step that makes
                        # a persisted queue reachable at all. Doing it here is what
                        # stops an outer ``wait_for`` expiring mid-write from turning a
                        # recoverable handover into a silent, permanent loss, and it is
                        # the reason a reserve alone is not the whole fix: a reserve
                        # makes the cancellation unlikely, this makes it survivable.
                        _readmit_for_next_start(
                            agent_id,
                            f"shutdown follow_up announce {announce_failure} and the "
                            "handover write was cancelled",
                        )
                        raise
                    if persisted:
                        clear_tombstone(agent_id)
                        # The write is not the discharge. ``list_orphans`` skips any folder
                        # holding a tombstone, so the persisted queue is reachable exactly
                        # when none remains -- and the removal's own answer cannot establish
                        # that, being False both for a folder that never had one and for one
                        # whose tombstone outlived the unlink (a held-open file). Asking about
                        # PRESENCE is asking what the next start asks.
                        if orphan_recovery_can_see(agent_id):
                            logger.warning(
                                "shutdown follow_up announce for %s %s; the undelivered "
                                "message(s) are persisted and the run re-admitted to orphan "
                                "recovery for the next start",
                                agent_id,
                                announce_failure,
                            )
                        else:
                            logger.warning(
                                "shutdown follow_up announce for %s %s AND its tombstone "
                                "could not be removed, so the next start skips the run and "
                                "the queued message(s) are not recoverable",
                                agent_id,
                                announce_failure,
                            )
                    else:
                        logger.warning(
                            "shutdown follow_up announce for %s %s AND its state could not "
                            "be written, so the queued message(s) are not recoverable",
                            agent_id,
                            announce_failure,
                        )
                else:
                    # Clear ONLY once the parent has actually been told. Clearing first and
                    # announcing after means a bounded announce that gives up has destroyed
                    # the queue AND not reported it: the parent was promised a completion
                    # event by its spawn_steer reply, and orphan recovery covers terminal
                    # reports rather than queued follow-ups, so nothing downstream could
                    # reconstruct them. Leaving the queue in place on failure keeps the only
                    # surviving record of what was never dispatched.
                    watcher_info.pending_followups = []
        tasks_to_await: list[asyncio.Task] = []  # type: ignore[type-arg]
        # Owner per cancelled task, so the phase below can say WHOSE outcome it gave up on.
        # Read from ``_agents`` here rather than after the wait: ``_tasks`` is cleared and a
        # reaped run can leave the registry while the wait is in flight.
        teardown_owners: dict[asyncio.Task, SubagentInfo] = {}  # type: ignore[type-arg]
        for agent_id, task in list(self._manager._tasks.items()):
            if not task.done():
                # _shutting_down (set above) is the terminal marker for this
                # site; the chokepoint enforces the contract mechanically.
                self._manager._cancel_task_intentionally(
                    task, self._manager._agents.get(agent_id), reason="shutdown"
                )
                tasks_to_await.append(task)
                owner = self._manager._agents.get(agent_id)
                if owner is not None:
                    teardown_owners[task] = owner
        if tasks_to_await:
            # Bounded the way the watcher phase above is bounded, and for its reason: these
            # tasks are ALREADY cancelled, so a ``wait_for`` around a gather would cancel
            # them a second time and then AWAIT the result, letting one run slow to honour
            # its cancel hold this phase for as long as it likes. ``asyncio.wait`` bounds how
            # long this SITE waits, which is the quantity a shutdown budget is about.
            #
            # Letting go forfeits only the rest of their teardown -- never a terminal report,
            # which is shielded and drained separately below. Waiting without a bound is what
            # let a single run that would not come down spend the whole budget and starve the
            # phases after it.
            finished_teardowns, still_tearing_down = await asyncio.wait(
                tasks_to_await, timeout=_remaining(None)
            )
            if still_tearing_down:
                logger.warning(
                    "cancel_all: %d run(s) did not finish teardown within the "
                    "cancellation budget; continuing to the report drain",
                    len(still_tearing_down),
                )
                # Letting go of the WAIT is safe; letting the run discharge its own outcome
                # afterwards is not. A run stopped here has not reached the ``finally`` that
                # SPAWNS its terminal report, so the drain below has nothing of its to find
                # and the straggler re-admission -- which iterates registered report tasks
                # -- cannot cover it. If it is scheduled once more before the loop closes it
                # runs its cancel arm and writes a terminal tombstone, and that tombstone is
                # exactly what ``list_orphans`` uses to exclude the folder: the outcome would
                # be hidden from the one path left to deliver it, with nothing having
                # delivered it. Marking the record is what stops that write; re-admitting
                # here instead would not hold, because the run's own write lands after it.
                for abandoned_task in still_tearing_down:
                    info = teardown_owners.get(abandoned_task)
                    if info is None or info._reported_to_parent:
                        continue
                    info._shutdown_outcome_abandoned = True
                    logger.warning(
                        "cancel_all: %s did not finish teardown, so its terminal report was "
                        "never registered; its folder is left visible to the next start's "
                        "orphan recovery rather than tombstoned",
                        info.id,
                    )
            for finished in finished_teardowns:
                # Read each outcome, which the gathered form did for free: an unread
                # exception on a closing loop surfaces as a bare "never retrieved" warning
                # with no site attached. A cancelled task is the normal case and carries
                # nothing to read.
                if finished.cancelled():
                    continue
                teardown_error = finished.exception()
                if teardown_error is not None:
                    logger.debug("cancel_all: run teardown raised %r", teardown_error)
        self._manager._tasks.clear()
        # Shielded terminal reports keep running after their awaiter is
        # cancelled (that is the point). Drain them with a BOUNDED wait so a
        # report is not orphaned by a closing event loop, without letting a
        # wedged injection block shutdown indefinitely.
        pending_reports = [t for t in self._manager._report_tasks if not t.done()]
        if pending_reports:
            # The runs this phase can still owe a delivery to, read BEFORE the waits
            # below. An outer cancellation lands at whichever await is live, and the
            # re-admission that follows those waits is the only thing keeping an
            # undelivered completion visible to the next start -- so the set it needs
            # cannot be one that is only computed after the await the cancellation hits.
            owed = [
                owner
                for owner in (self._manager._report_owners.get(t) for t in pending_reports)
                if owner is not None
            ]

            def _readmit_owed(because: str) -> None:
                """Re-admit every run this phase still owes a delivery to.

                Filtered on the same ``_reported_to_parent`` marker the ordered path
                uses, so a report that already reached the parent is not re-admitted
                into a duplicate delivery.
                """
                for owner in owed:
                    if not owner._reported_to_parent:
                        _readmit_for_next_start(owner.id, because)

            # Sized from what is LEFT after the teardown wait above, not from the caller's
            # original figure: the budget is shared across phases, so spending it earlier
            # has to shorten this one.
            drain_timeout = _remaining(_REPORT_DRAIN_TIMEOUT)
            try:
                await asyncio.wait(pending_reports, timeout=drain_timeout)
            except asyncio.CancelledError:
                _readmit_owed("its terminal report drain was cancelled")
                raise
            except Exception:
                logger.debug("cancel_all: report drain wait failed", exc_info=True)
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
                logger.warning(
                    "cancel_all: %d terminal report(s) did not drain in %.0fs — "
                    "cancelling; their completions may not have been delivered",
                    len(stragglers),
                    drain_timeout,
                )
                abandoned = [self._manager._report_owners.get(t) for t in stragglers]
                for report_task in stragglers:
                    report_task.cancel()
                try:
                    await asyncio.gather(*stragglers, return_exceptions=True)
                except asyncio.CancelledError:
                    _readmit_owed("its terminal report was cancelled during shutdown")
                    raise
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
