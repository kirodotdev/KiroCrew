"""Waves behavior for the SubagentManager facade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._component import ManagerComponent

if TYPE_CHECKING:
    from ..subagent import (
        _RESET_TIMEOUT,
        _WAVE_STUCK_SECS,
        DIGEST_HOLD_SECS,
        SubagentInfo,
        asyncio,
        logger,
        mark_delivered,
        sel,
        time,
        uuid,
    )


class WaveDigestCoordinator(ManagerComponent):
    """Own waves transitions while state remains facade-owned."""

    __slots__ = ()

    def batch_members_pending_impl(self, batch_id: str) -> bool:
        """True while ANY member of *batch_id* is still outstanding — running
        (registered, not done), queued behind the stagger gate (not yet
        registered), OR not yet submitted (sibling POSTs still in flight —
        a fast-failing first member must not finalize the
        wave and emit a partial digest before the rest of the batch even
        arrives). The wave digest must also not be held hostage by unrelated
        agents under the same parent."""
        if not batch_id:
            return False
        _bs = self._manager._batch_submitted.get(batch_id)
        if _bs is not None and _bs[1] > 0 and _bs[0] < _bs[1]:
            return True  # submissions still in flight
        if any(a.batch_id == batch_id and not a.done for a in self._manager._agents.values()):
            return True
        return any(p.get("batch_id") == batch_id for p in self._manager._queue)

    def finalize_batch_impl(self, batch_id: str) -> None:
        """Prune per-wave bookkeeping once the wave digest has fired.

        Bounds `_seen_batches` / `_batch_submitted` growth: without
        this, long-lived gateways accrete an entry per wave forever.
        """
        if not batch_id:
            return
        self._manager._seen_batches.discard(batch_id)
        self._manager._batch_submitted.pop(batch_id, None)
        self._manager._batch_progress_ts.pop(batch_id, None)

    def record_lost_submission_impl(
        self,
        batch_id: str,
        batch_total: int,
        reason: str,
        parent_session_key: str = "",
    ) -> None:
        """Reconcile a wave member whose spawn submission was LOST before it
        reached :meth:`spawn` (transport error / timeout / pre-spawn HTTP
        rejection in ``api_spawn``).

        Every sibling POST carried ``batch_total`` counting the lost member,
        but ``spawn()`` never ran for it — so ``submitted < expected``
        forever, ``batch_members_pending()`` stays True, the digest chunk
        never fires, and held sibling results strand until a gateway restart.
        This helper counts the lost
        member as submitted AND announces a synthetic terminal member through
        the single completion consumer, so the wave's accounting sees a
        failure line and can close.

        Idempotent-ish by construction: each call reconciles exactly one lost
        member; callers invoke it once per lost submission.
        """
        if not batch_id:
            return
        _bs = self._manager._batch_submitted.setdefault(batch_id, [0, max(0, int(batch_total))])
        _bs[0] += 1
        self._manager._batch_progress_ts[batch_id] = time.time()
        try:
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="submission_lost",
                metadata={"batch_id": batch_id, "reason": reason[:200]},
            )
        except Exception:
            logger.debug("SEL audit failed for lost submission", exc_info=True)
        info = SubagentInfo(
            id=uuid.uuid4().hex[:8],
            task="(submission lost before spawn)",
            agent="",
            parent_session_key=parent_session_key,
            done=True,
            error=f"spawn submission lost: {reason[:300]}",
            batch_id=batch_id,
            batch_total=max(0, int(batch_total)),
        )
        if self._manager._on_done:
            try:
                self._manager._tasks[f"lost-{info.id}"] = asyncio.ensure_future(
                    self._manager._safe_announce(info)
                )
            except RuntimeError:
                pass  # no running loop (sync/test context)

    def _sweep_stuck_waves_impl(self, now: float) -> None:
        """Reaper backstop: force-reconcile waves wedged by lost submissions.

        A wave is STUCK when ``submitted < expected``, every registered
        member is terminal, nothing of the wave sits in the spawn queue, and
        there has been no submission progress for ``_WAVE_STUCK_SECS``. The
        count-driven ``batch_members_pending()`` can never close such a wave
        on its own — no future completion event will arrive. Reconciling via
        :meth:`record_lost_submission` (once per sweep per wave — waves with
        multiple losses converge across sweeps) re-enters the completion
        consumer so held sibling results deliver instead of stranding until
        restart. Also bounds the ``_batch_submitted``/``_batch_progress_ts``
        leak in the stuck case.
        """
        for batch_id, _bs in list(self._manager._batch_submitted.items()):
            if _bs[1] <= 0 or _bs[0] >= _bs[1]:
                continue  # complete or unbounded — not wedged by lost POSTs
            last = self._manager._batch_progress_ts.get(batch_id, 0.0)
            if now - last < _WAVE_STUCK_SECS:
                continue  # still within the grace window
            members = [a for a in self._manager._agents.values() if a.batch_id == batch_id]
            if any(not a.done for a in members):
                continue  # live members will re-evaluate the wave on completion
            if any(p.get("batch_id") == batch_id for p in self._manager._queue):
                continue  # queued members still pending — not stuck
            parent = members[0].parent_session_key if members else ""
            logger.warning(
                "Reaper: wave %s stuck (%d/%d submitted, no progress for %.0fs)"
                " — reconciling one lost submission",
                batch_id,
                _bs[0],
                _bs[1],
                now - last,
            )
            self._manager.record_lost_submission(
                batch_id,
                _bs[1],
                f"submission never arrived (wave stuck > {_WAVE_STUCK_SECS}s"
                " — reconciled by reaper liveness backstop)",
                parent_session_key=parent,
            )

    def _sweep_digest_holds_impl(self, now: float) -> None:
        """Reaper backstop: release wave results whose HOLD DEADLINE expired.

        The gateway holds a completed member's per-agent injection until the
        wave's digest chunk fires. Both of the chunk's triggers are event-driven
        — a COUNT trigger (``SUBAGENT_DIGEST_CHUNK_SIZE`` completions pending)
        and wave close — so neither can fire while a straggler is simply *not
        finishing*. With the default count (10) above any realistic wave size,
        the only flush that ever fires is the wave-close one, and a member that
        HANGS rather than fails withholds every sibling's finished result for
        the full ``_TIMEOUT_SECS`` reap window (issue #2215).

        This sweep is the timer the event-driven triggers lack: when the OLDEST
        outstanding hold in a wave has aged past :data:`DIGEST_HOLD_SECS` and
        the wave is still live, it announces a synthetic *flush-only* record
        through the single completion consumer (the same re-entry mechanism
        :meth:`record_lost_submission` uses), which forces the partial digest
        out. Ordinary fast waves never reach the deadline, so the deliberate
        "small wave = one consolidated digest" behavior is untouched.
        """
        if DIGEST_HOLD_SECS <= 0 or self._manager._on_done is None:
            return  # deadline disabled — count-trigger-only
        oldest: dict[str, float] = {}
        parents: dict[str, str] = {}
        totals: dict[str, int] = {}
        for info in list(self._manager._agents.values()):
            _bid = info.batch_id
            if not _bid or info._digest_held_at <= 0.0:
                continue
            _prev = oldest.get(_bid)
            if _prev is None or info._digest_held_at < _prev:
                oldest[_bid] = info._digest_held_at
            parents.setdefault(_bid, info.parent_session_key)
            totals.setdefault(_bid, info.batch_total)
        for batch_id, held_at in oldest.items():
            age = now - held_at
            if age < DIGEST_HOLD_SECS:
                continue  # still inside the grace window
            if not self._manager.batch_members_pending(batch_id):
                # The wave is closing on its own — the real wave-close flush is
                # already in flight (or the held flags are stale bookkeeping).
                # Forcing a partial digest here would race it and could emit a
                # duplicate chunk for the same members.
                continue
            logger.warning(
                "Reaper: wave %s held results for %.0fs (deadline %.0fs) —"
                " forcing partial digest flush",
                batch_id,
                age,
                DIGEST_HOLD_SECS,
            )
            self._manager.force_digest_flush(
                batch_id,
                parents.get(batch_id, ""),
                totals.get(batch_id, 0),
                age,
            )

    def force_digest_flush_impl(
        self,
        batch_id: str,
        parent_session_key: str,
        batch_total: int,
        held_secs: float,
    ) -> None:
        """Announce a synthetic *flush-only* record to release a wave's held
        results without waiting for another member to complete.

        The record is deliberately NOT a wave member: ``_digest_flush_only``
        tells the gateway to skip every per-member side effect (terminal WS
        event, orchestration accounting, done/ok/err counters, digest lines) and
        only force the pending chunk out. Announcing it through ``_on_done`` —
        rather than reaching into the gateway's digest buffers — reuses the one
        completion consumer that owns digest composition, routing, and the
        held-tombstone settle contract.
        """
        if not batch_id or self._manager._on_done is None:
            return
        info = SubagentInfo(
            id=uuid.uuid4().hex[:8],
            task=f"(wave digest flush — results held {int(held_secs)}s)",
            parent_session_key=parent_session_key,
            done=True,
            batch_id=batch_id,
            batch_total=max(0, int(batch_total)),
        )
        info._digest_flush_only = True
        try:
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="digest_hold_expired",
                metadata={"batch_id": batch_id, "held_secs": int(held_secs)},
            )
        except Exception:
            logger.debug("SEL audit failed for digest hold expiry", exc_info=True)
        try:
            self._manager._tasks[f"flush-{info.id}"] = asyncio.ensure_future(
                self._manager._announce_digest_flush(info)
            )
        except RuntimeError:
            pass  # no running loop (sync/test context)

    async def _announce_digest_flush_impl(self, info: SubagentInfo) -> None:
        """Run the flush-only announce with the SAME settle contract as ``_run``.

        ``_settle_digest_holds`` must run only after ``_on_done`` returns
        cleanly: a routing failure has to leave the held members undelivered so
        orphan reconciliation can still recover them after a restart. This path
        has no run loop to enforce that ordering, so it enforces it here.
        """
        assert self._manager._on_done is not None
        try:
            await self._manager._on_done(info)
        except Exception:
            logger.warning(
                "Digest hold flush announce failed for wave %s", info.batch_id, exc_info=True
            )
            return
        self._manager._settle_digest_holds(info)

    async def settle_queued_delivery_impl(self, agent_ids: list[str]) -> None:
        """Write the ``delivered`` tombstones for completions consumed from a queue.

        The queued-injection path (issue #4839) deliberately leaves a completion
        un-tombstoned until the parent's turn has consumed the announce, so the
        write lands here — in the parent's drain — rather than in
        :meth:`_report_terminal`. That is also why it must repeat the gate that
        report holds: a ``delivered`` tombstone EXCLUDES the folder from restart
        orphan reconciliation, and the drain can come due while the run's teardown
        is still killing its child, so writing early would let a crash in that
        window strand a live child that nothing would ever reap.

        The wait is bounded exactly as the report's is (teardown is itself bounded
        by ``_RESET_TIMEOUT`` then SIGKILL, and runs in a ``finally``), and a
        timeout writes anyway rather than abandoning the retention bound — the same
        trade the report makes. The gate is read from ``_teardown_gates``, which
        outlives the run's ``_agents``/``_tasks`` records: a dashboard "clear
        completed" or "cancel" pops both of those for a run that is done but still
        tearing down, so inferring "record gone means child gone" would tombstone a
        live child. No gate entry means teardown has finished (or never started).

        The tombstone write itself is offloaded: it fsyncs, and this runs on the
        gateway event loop.
        """
        for agent_id in agent_ids:
            gate = self._manager._teardown_gates.get(agent_id)
            if gate is not None and not gate.is_set():
                try:
                    await asyncio.wait_for(gate.wait(), timeout=_RESET_TIMEOUT + 30)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Subagent %s: teardown did not complete before the queued "
                        "delivered tombstone; writing it anyway",
                        agent_id,
                    )
            try:
                await asyncio.to_thread(mark_delivered, agent_id)
            except Exception:
                logger.debug(
                    "Failed to mark drained subagent %s delivered", agent_id, exc_info=True
                )

    def _settle_digest_holds_impl(self, info: SubagentInfo) -> None:
        """Settle delivery tombstones for wave members whose injection was
        held for this member's digest. Called ONLY after ``_on_done`` returned
        without raising — and it is a real settle only for the routes where
        that return IS the confirmation. Both dashboard routes hand off
        asynchronously, so they detach the ids before ``_on_done`` returns and
        owe them to the parent's consumption instead (the queue branch via
        ``_defer_queued_delivery``, the direct-injection branch via the same
        slot ledger), leaving this a no-op there. Marking the held members
        delivered no longer risks the restart-loss window here (settling at
        digest composition, before routing, would).

        The ids are taken off ``info`` BEFORE settling, so a re-entry cannot
        write a second tombstone and a route that detached them first leaves
        this a no-op.

        A failing tombstone write is logged and skipped, never raised: one
        unwritable run folder must not strand the rest of the chunk.
        """
        ids, info._digest_settle_ids = info._digest_settle_ids, []
        for _hid in ids:
            try:
                mark_delivered(_hid)
            except Exception:
                logger.debug("Failed to settle held subagent %s", _hid, exc_info=True)
