"""Cancellation behavior for the SubagentManager facade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._component import ManagerComponent

if TYPE_CHECKING:
    from ..subagent import (
        _ON_DONE_TIMEOUT,
        _RECOVERY_SLOT_WAIT_SECS,
        _REPORT_DRAIN_TIMEOUT,
        _RESET_TIMEOUT,
        Stats,
        SubagentInfo,
        asyncio,
        clear_tombstone,
        logger,
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
                # can no longer leak the counter.
                self._manager._running_count += 1
                # The interrupted run's finally already consumed this info's
                # slot token to free its slot. The respawn occupies a FRESH slot,
                # so re-arm the token or the respawned run's finally would no-op
                # and leave `_running_count` permanently inflated.
                info._slot_released = False
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
                # recovery is no longer outstanding.
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
                # `reaped` made this arm win the race and persist a neutral user
                # Stop as a FAILURE, with a failure stat and a "cancelled"
                # tombstone the reaper could no longer correct.
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

    def _unqueue_impl(self, agent_id: str) -> dict | None:
        """Remove and return a not-yet-started spawn from the stagger queue.

        The queue is the only record of a waiting run — ``spawn`` returns its
        queued ``SubagentInfo`` without registering it in ``_agents``. Returning
        the entry lets cancellation publish the same neutral stopped terminal
        outcome as a run that had already started, including batch accounting.
        """
        for index, params in enumerate(self._manager._queue):
            if str(params.get("_preassigned_id") or "") != agent_id:
                continue
            dropped = self._manager._queue.pop(index)
            try:
                self._manager._emit_queue_depth(
                    str(dropped.get("parent_session_key", "")),
                    str(dropped.get("batch_id", "")),
                )
            except Exception:
                logger.debug("queue-depth re-emit failed after unqueue", exc_info=True)
            return dropped
        return None

    def _report_queued_stop_impl(self, params: dict) -> None:
        """Publish a neutral terminal record for work stopped before startup."""
        info = SubagentInfo(
            id=str(params.get("_preassigned_id") or ""),
            task=str(params.get("task") or "(stopped before start)"),
            parent_session_key=str(params.get("parent_session_key") or ""),
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

    async def cancel_for_parent_impl(self, parent_session_key: str) -> tuple[int, int]:
        """Stop one parent's running and queued agents.

        Queue entries are removed before the first suspending await, so a stagger
        timer cannot start work after the user clicked Stop all. Agents parked on
        a spawn-approval prompt remain pending because that prompt has its own
        explicit reject action.
        """
        if not parent_session_key:
            return (0, 0)
        queued_ids = [
            str(params.get("_preassigned_id") or "")
            for params in self._manager._queue
            if params.get("parent_session_key", "") == parent_session_key
        ]
        queued_stopped = 0
        for agent_id in queued_ids:
            if not agent_id:
                continue
            queued = self._manager._unqueue(agent_id)
            if queued is None:
                continue
            self._manager._report_queued_stop(queued)
            queued_stopped += 1

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

    async def cancel_impl(self, agent_id: str) -> bool:
        """Cancel a single running subagent. Returns True if found and cancelled.

        User-initiated stop is a neutral terminal state, not an error: partial
        output is preserved on the info record (and remains in result.txt), the
        tombstone is written as ``user_stop``, and the ``subagent_done`` event
        carries ``stopped: true`` so the UI renders a neutral "stopped" card.
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
            agent_id, info, time.time() - info.started, reason="user_stop"
        )
        return True

    async def cancel_all_impl(self) -> None:
        """Cancel all running subagents and wait for cleanup."""
        # Shutdown-driven cancellations must never trigger the one-shot
        # unexpected-cancel auto-continue (the loop is going away).
        self._manager._shutting_down = True
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
        followup_watchers = [t for t in self._manager._followup_watchers.values() if not t.done()]
        for followup_watcher in followup_watchers:
            followup_watcher.cancel()
        if followup_watchers:
            await asyncio.gather(*followup_watchers, return_exceptions=True)
        self._manager._followup_watchers.clear()
        for agent_id in watcher_ids:
            watcher_info = self._manager._agents.get(agent_id)
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
        pending_reports = [t for t in self._manager._report_tasks if not t.done()]
        if pending_reports:
            try:
                await asyncio.wait(pending_reports, timeout=_REPORT_DRAIN_TIMEOUT)
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
                    _REPORT_DRAIN_TIMEOUT,
                )
                abandoned = [self._manager._report_owners.get(t) for t in stragglers]
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
