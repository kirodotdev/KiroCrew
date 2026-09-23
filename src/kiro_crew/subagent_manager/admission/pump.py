"""The dispatch pump: stagger, drain, the atomic claim -> dispatch -> register -> release unit, approval, start logging."""

from __future__ import annotations

import logging as _logging
from typing import TYPE_CHECKING, Any

from .._component import ManagerComponent
from .types import ClaimPoint

_glue_logger = _logging.getLogger("kiro_crew.subagent_manager.admission")

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from kiro_crew import taskq as _taskq

    from ...subagent import (
        SpawnAdmissionCoordinator,
        SpawnApprovalUnreachable,
        Stats,
        SubagentInfo,
        _context_groups_field,
        asyncio,
        create_agent_folder,
        logger,
        sel,
        time,
    )


class _PumpMixin(ManagerComponent):
    __slots__ = ()

    if TYPE_CHECKING:
        # Sibling-mixin methods this module reaches through ``self``; typing only.
        def taskq_store(self) -> "_taskq.TaskStore | None": ...

        def taskq_admit_wait_secs(self) -> float: ...

        async def taskq_child_registered_async(self, info: "SubagentInfo") -> None: ...

        def _record_crew_log_spawn_started(self, info: "SubagentInfo") -> None: ...

    def _should_stagger_queue_impl(self, now: float) -> tuple[bool, bool]:
        """Decide whether a spawn arriving at *now* must be queued.

        Returns ``(should_queue, slot_free)``. A spawn is queued when either no
        slot is free (at capacity) OR a spawn started within the stagger window
        (``subagent_spawn_stagger_secs``) — so the initial fill never bursts and
        no two agents start within the interval (dynamic-subagent-sizing.md §5.3).
        """
        # The cap as the fairness dispatcher reads it: the effective cap, lifted
        # for the child reserve while a parent waits under an adaptive squeeze
        # (``CapacityView``). The reserve's root-only narrowing is applied by
        # the caller, which knows whether the spawn is nested.
        slot_free = self._manager._admission.capacity_view().any_slot
        too_soon = (now - self._manager._last_spawn_ts) < self._manager._spawn_stagger_secs
        return (not slot_free or too_soon, slot_free)

    def _drain_queue_impl(self) -> None:
        """Spawn the next queued task if a slot is available and the stagger
        interval has elapsed.

        This is the single staggered pump: at most one start per
        ``subagent_spawn_stagger_secs`` (dynamic-subagent-sizing.md §5.3). If a
        slot is free but a spawn started too recently, it reschedules itself at
        the interval boundary rather than bursting.

        On a running event loop with a durable store the pump is a COROUTINE
        (``_drain_queue_async``): the store reads that top the window up
        (``pending_lanes`` / ``fetch_dispatchable_fair`` / ``next_eligible_at``)
        and the wait-expiry sweep run on the store's writer thread through
        ``TaskStore.run``, and only the window mutation and the pick happen on
        the loop. One drain coroutine is in flight at a time; a request that
        lands while one runs is coalesced into one more pass. Without a
        running loop (sync callers, tests) the pump runs inline.
        """
        # Nothing waiting anywhere: return before reading any other manager
        # attribute, so a minimal facade with only a queue can pump safely.
        store = self._manager._admission.taskq_store()
        if not self._manager._queue and store is None:
            return
        # The gateway holds the pump closed between the durable store's open
        # and the memory barrier (``defer_queue_dispatch``): rows that survived
        # a restart are claimed by this pump, and a run started before memory
        # is prepared either fails on ``MemoryStartupUnavailable`` or runs
        # without its learned memory. ``release_queue_dispatch`` opens the hold
        # and drains once, so nothing that asked in between is lost. Read with
        # a default so a minimal facade without the attribute still pumps.
        if getattr(self._manager, "_queue_dispatch_held", False):
            # One line, not one per pass: a hold that is never opened would
            # otherwise look exactly like the silent "accepted, never claimed"
            # queue this hold exists to prevent. Only a real manager reaches
            # here (a facade without the flag pumped above), so the companion
            # flag is always present.
            if not self._manager._queue_dispatch_hold_logged:
                self._manager._queue_dispatch_hold_logged = True
                # ``logger``, not ``_glue_logger``: an ``*_impl`` runs on
                # ``subagent``'s globals (``bind_component_globals``), where
                # this module's own logger name does not exist.
                logger.debug(
                    "taskq pump refusing passes: dispatch held until the memory "
                    "barrier releases it (in-memory queue=%d, durable store=%s)",
                    len(self._manager._queue),
                    "attached" if store is not None else "none",
                )
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or store is None or not SpawnAdmissionCoordinator.pump_off_loop:
            self._drain_queue_sync_impl(refill=self._manager._admission.taskq_refill_window)
            return
        pending = getattr(self._manager, "_drain_task", None)
        if pending is not None and not pending.done():
            setattr(self._manager, "_drain_again", True)
            return
        setattr(self._manager, "_drain_again", False)
        setattr(
            self._manager,
            "_drain_task",
            self._manager._admission.track_store_task(
                loop.create_task(self._drain_queue_async_impl())
            ),
        )

    async def _drain_queue_async_impl(self) -> None:
        """The pump as a coroutine: every store read off-loop, then the sync
        pick/spawn on an already topped-up window (see :meth:`_drain_queue_impl`).

        A ``_drain_queue()`` request that lands while a pass runs (capacity
        released mid-drain) is coalesced into ``_drain_again``; the SAME task
        loops and runs one more pass for it, since the entry point sees this
        task as still active and would schedule nothing.
        """
        while True:
            setattr(self._manager, "_drain_again", False)
            await self._manager._drain_queue_pass()
            if not getattr(self._manager, "_drain_again", False):
                return

    def _schedule_retained_claim_retry(self) -> None:
        """Arm one later pump pass for an admitted claim awaiting the store."""
        if not self._manager._retained_claims or self._manager._shutting_down:
            return
        pending = self._manager._retained_claim_retry_handle
        if pending is not None and not pending.cancelled():
            return
        import asyncio as _asyncio

        try:
            loop = _asyncio.get_running_loop()
        except RuntimeError:
            return

        def _retry() -> None:
            self._manager._retained_claim_retry_handle = None
            self._manager._drain_queue()

        delay = max(0.05, self.taskq_admit_wait_secs())
        self._manager._retained_claim_retry_handle = loop.call_later(delay, _retry)

    def _retain_claim(
        self,
        point: ClaimPoint,
        generation: int,
        reenter: "Callable[[tuple[int, bool, str]], Any]",
        stop_params: "Mapping[str, Any] | None",
    ) -> None:
        """Keep one admitted generation and its reserved slot for retry."""
        self._manager._retained_claims[point.agent_id] = (
            point,
            generation,
            reenter,
            dict(stop_params or {}),
        )
        self._schedule_retained_claim_retry()

    async def retry_retained_claims(self) -> None:
        """Retry one held generation before the pump considers queued rows."""
        retained = self._manager._retained_claims
        try:
            agent_id, entry = next(iter(retained.items()))
        except StopIteration:
            return
        retained.pop(agent_id, None)
        point, generation, reenter, stop_params = entry
        result = await self.claim_and_start(
            point,
            reenter,
            stop_params=stop_params,
            retained_generation=generation,
        )
        if result is not None and not result.done and result.id in self._manager._agents:
            await self.taskq_child_registered_async(result)
        self._after_dispatch_impl(stop_params, result, refill=lambda **_kw: 0)
        if retained:
            self._schedule_retained_claim_retry()
        else:
            pending = self._manager._retained_claim_retry_handle
            if pending is not None and not pending.cancelled():
                self._manager._cancel_task_intentionally(
                    pending,
                    reason="retained claim settled",
                )
            self._manager._retained_claim_retry_handle = None

    async def _drain_queue_pass_impl(self) -> None:
        admission = self._manager._admission
        retain_error_detail = True
        try:
            await self._manager.retry_pending_boundary_cancellations()
            await admission.retry_retained_claims()
            store = admission.taskq_store()
            if store is not None:
                try:
                    admission.taskq_expire_waits_apply(
                        await store.run(admission.taskq_expire_waits_store)
                    )
                except Exception:
                    # ``logger``, not this module's ``_glue_logger``: an
                    # ``*_impl`` runs on ``subagent``'s globals, where that name
                    # does not exist (``bind_component_globals``), so loading it
                    # here would be a NameError on the failure path.
                    logger.debug("taskq: wait expiry failed", exc_info=True)
                await admission.ensure_coordinator_async()
                await admission.refresh_pending_children_async()
                if admission.capacity_view().any_slot:
                    await admission.taskq_refill_window_async()
                    await admission.taskq_refill_window_async(children_only=True)
            picked: list[dict[str, Any]] = []
            granting: list[dict[str, Any]] = []
            # The pick's own store read: an entry that does not name its lane
            # resolves its parent chain through ``store.get``. Resolved here, on
            # the writer thread, and nothing awaits between this and the pick
            # below, so the window these lanes describe is the one picked from.
            lanes = await admission.resolve_window_lanes_async()
            self._drain_queue_sync_impl(
                refill=lambda **_kw: 0,
                dispatch=picked.append,
                grant=granting.append,
                lanes=lanes,
            )
            for entry in granting:
                if not await admission.resume_grant_async(entry):
                    # The reservation went back; a freed slot means the window
                    # may have work for it now.
                    self._manager._drain_queue()
            for params in picked:
                retain_error_detail = params.get("_memory_mode", "persistent") == "persistent"
                drained = await self._dispatch_async_impl(params)
                self._after_dispatch_impl(params, drained, refill=lambda **_kw: 0)
        except Exception:
            logger.error("drain pump failed", exc_info=retain_error_detail)

    async def _dispatch_async_impl(self, params: dict[str, Any]) -> "SubagentInfo | None":
        """Start a picked window row with its claim (``store.claim``) on the
        writer thread: the gates run on the loop and stop at the claim
        (``ClaimPoint``), the claim is awaited through ``TaskStore.run``, and
        registration re-enters with the result (``_claimed``). A row the
        pressure gate parked instead stops one step earlier and its
        ``store.defer`` is awaited the same way (``DeferPoint``)."""
        store = self._manager._admission.taskq_store()
        admission = self._manager._admission
        first: Any = self._manager.spawn(
            **params,
            _from_queue=True,
            _stop_before_claim=store is not None,
            _child_registration=store is None,
        )
        if not isinstance(first, ClaimPoint):
            if first is not None:
                # Ahead of the registration below, never after it: the row is
                # durably parked -- or refused for want of a row -- before any
                # caller can read the answer as a queued handle.
                first = await admission.finish_parked_defer(first)
            # A re-queued (or, without a store, started) row: the W3 branch
            # runs here, awaited, instead of inline in ``spawn_impl``.
            if (
                store is not None
                and first is not None
                and (first.id in self._manager._agents or (first.queued and not first.done))
            ):
                await admission.taskq_child_registered_async(first)
            return first
        assert store is not None
        result: Any = await admission.claim_and_start(
            first,
            lambda claimed: self._manager.spawn(
                **params, _from_queue=True, _claimed=claimed, _child_registration=False
            ),
            stop_params=params,
        )
        if result is not None and not result.done and result.id in self._manager._agents:
            await admission.taskq_child_registered_async(result)
        return result

    async def claim_and_start(
        self,
        point: ClaimPoint,
        reenter: "Callable[[tuple[int, bool, str]], Any]",
        *,
        stop_params: "Mapping[str, Any] | None" = None,
        retained_generation: int | None = None,
    ) -> "SubagentInfo | None":
        """Claim a reserved row, settle its durable authority, then register it.

        A failure before the claim leaves the row queued and releases the
        reservation. A store outage after the claim retains the admitted
        generation and reservation for a later pump pass. Registration consumes
        the reservation; every durably refused outcome releases it.
        """
        store = self.taskq_store()
        assert store is not None
        report_params: dict[str, Any] | None = None
        claim_will_register = False
        claim_retained = False
        result: Any = None
        try:
            claimed = (
                (retained_generation, True, "")
                if retained_generation is not None
                else await store.run(self.taskq_claim, point.agent_id)
            )
            generation, proceed, _reason = claimed
            if proceed and point.boundary_owner:
                from kiro_crew import taskq as _taskq

                try:
                    still_current = await store.run(
                        self.taskq_claim_still_current,
                        point.agent_id,
                        generation,
                    )
                    cancellation_pending = getattr(
                        self._manager,
                        "_boundary_cancellation_pending",
                        None,
                    )
                    pending = callable(cancellation_pending) and cancellation_pending(
                        {
                            "parent_session_key": point.parent_session_key,
                            "_stage_boundary_owner": point.boundary_owner,
                        }
                    )
                    if still_current and pending:
                        stopped = await store.run(
                            store.cancel,
                            point.agent_id,
                            reason="boundary_cancel_before_registration",
                            only_from=frozenset({_taskq.ADMITTED}),
                            generation=generation,
                        )
                        claimed = (generation, False, self.CLAIM_REFUSED)
                        if stopped is not None:
                            report_params = dict(stop_params or {})
                            report_params.setdefault("_preassigned_id", point.agent_id)
                            report_params.setdefault("parent_session_key", point.parent_session_key)
                            report_params.setdefault("_stage_boundary_owner", point.boundary_owner)
                except _taskq.TaskStoreUnavailable:
                    still_current = None
                    _glue_logger.warning(
                        "taskq: post-claim settlement of %s failed; retaining generation %d",
                        point.agent_id,
                        generation,
                        exc_info=True,
                    )
                if still_current is None:
                    self._retain_claim(point, generation, reenter, stop_params)
                    claim_retained = True
                    if retained_generation is None:
                        result = reenter((generation, False, self.CLAIM_RETAINED))
                    return result
                if not still_current:
                    claimed = (generation, False, self.CLAIM_REFUSED)
            # No await between a successful final durable/boundary check and
            # registration: cancellation cannot interleave after the
            # revalidation a registered start relies on. A refused claim may
            # await its terminal store write because it never registers.
            claim_will_register = bool(claimed[1])
            result = reenter(claimed)
        finally:
            # Queued-stop reporting temporarily installs a synthetic terminal
            # record under this id. Only a still-proceeding claim that really
            # registered may consume the reservation; terminal report identity
            # is not a registered start. A retained claim keeps the reservation.
            registered = claim_will_register and point.agent_id in self._manager._agents
            if not registered and not claim_retained:
                self.release_reservation(point.agent_id)
            if report_params is not None:
                self._manager._report_queued_stop(report_params)
                self._manager._emit_queue_depth(
                    point.parent_session_key,
                    str(report_params.get("batch_id") or ""),
                )
        assert not isinstance(result, ClaimPoint)
        return result

    def release_reservation(self, agent_id: str) -> None:
        """Give back the slot a ``ClaimPoint`` reserved for a row that did not start."""
        self._manager._running_count = max(0, int(self._manager._running_count) - 1)
        _glue_logger.debug("taskq: reservation for %s released", agent_id)

    def _drain_queue_sync_impl(
        self,
        *,
        refill: "Callable[..., int]",
        dispatch: "Callable[[dict[str, Any]], None] | None" = None,
        grant: "Callable[[dict[str, Any]], None] | None" = None,
        lanes: "Mapping[str, str] | None" = None,
    ) -> None:
        """The pick-and-spawn half of the pump. *refill* tops the window up
        from the store (the inline path) or is a no-op when
        ``_drain_queue_async`` already did so off-loop. *dispatch*, when given,
        receives the picked row instead of the sync ``spawn`` (the coroutine
        pump claims it on the writer thread); *grant*, when given, receives a
        popped RESUME entry whose lane slot is already reserved, instead of the
        whole grant running inline (the coroutine pump wakes the row on the
        writer thread and publishes the run state from the result). *lanes*
        carries the lane of every entry that does not name its own, resolved
        off the loop by the caller for the same reason as the rest."""
        if not self._manager._queue and self._manager._admission.taskq_store() is None:
            return
        view = self._manager._admission.capacity_view()
        if not view.any_slot:
            return
        # The window is a bounded view over the store: top it up in lane order
        # (weighted round-robin across lanes, FIFO inside a lane), so a row
        # that waited on disk is never overtaken by a younger one of its own
        # lane, and one lane's backlog never fills the whole window.
        refill()
        if not self._manager._queue:
            return
        # RESUME entries first: a live run that yielded its lane slot for a
        # wait and whose wake condition has been met. It re-enters through
        # this pump so a wake never bypasses capacity, but a resume is not a
        # process start -- the run is already resident -- so it neither waits
        # for the spawn stagger nor consumes it; granting hands the slot back
        # to the waiting coroutine instead of spawning. Compatibility doubles
        # can expose no cancellation predicate and therefore have no matching
        # authority to apply.
        boundary_cancellation_pending = getattr(
            self._manager,
            "_boundary_cancellation_pending",
            None,
        )
        while self._manager._queue:
            index = next(
                (
                    i
                    for i, p in enumerate(self._manager._queue)
                    if p.get("_resume_id")
                    and not (
                        callable(boundary_cancellation_pending) and boundary_cancellation_pending(p)
                    )
                ),
                None,
            )
            if index is None:
                break
            params = self._manager._queue.pop(index)
            params.pop("_lane", None)
            if grant is None:
                self._manager._admission.resume_grant(params)
            elif self._manager._admission.resume_reserve(params):
                # The RESERVATION is what the capacity re-check below reads, so
                # it has to happen here; the durable wake and the run-state
                # publish follow off-loop in ``resume_grant_async``.
                grant(params)
            view = self._manager._admission.capacity_view()
            if not view.any_slot:
                return
        refill()
        if not self._manager._queue:
            return
        elapsed = time.monotonic() - self._manager._last_spawn_ts
        if elapsed < self._manager._spawn_stagger_secs:
            # Too soon since the last start — reschedule at the boundary.
            try:
                asyncio.get_event_loop().call_later(
                    self._manager._spawn_stagger_secs - elapsed, self._manager._drain_queue
                )
            except RuntimeError:
                pass  # no running loop (sync/test context)
            return
        # Lane-aware pick: the weighted round-robin over the lanes with
        # eligible entries (resumes were granted above). When only the child
        # reserve is left, roots are not eligible; the window is topped up
        # with nested rows so the reserve can be used.
        index = self._manager._admission.pick_window_index(view, lanes=lanes)
        if index is None:
            if refill(children_only=True) > 0:
                index = self._manager._admission.pick_window_index(view, lanes=lanes)
        if index is None:
            return
        params = self._manager._queue.pop(index)
        params.pop("_lane", None)
        # A run can be cancelled WHILE it waits here — a user stop, or a session
        # deleted out from under it. Starting it anyway would execute tools for
        # work already reported as stopped, so skip it and drain the next one
        # instead: `cancel()` marks the info terminal but cannot unqueue this.
        queued_id = str(params.get("_preassigned_id") or "")
        if queued_id:
            waiting = self._manager._agents.get(queued_id)
            if waiting is not None and (waiting.done or waiting.user_stopped or waiting.reaped):
                logger.info("Skipping queued spawn %s: cancelled while waiting", queued_id)
                self._manager._emit_queue_depth(
                    str(params.get("parent_session_key", "")), str(params.get("batch_id", ""))
                )
                if self._manager._queue:
                    self._manager._drain_queue()
                return
        logger.info(
            "Draining queue: spawning '%s' (%d left)",
            (
                str(params.get("task", ""))[:40]
                if params.get("_memory_mode", "persistent") == "persistent"
                else queued_id
            ),
            len(self._manager._queue),
        )
        # The popped item's parent just lost one waiting agent — re-emit its
        # queued depth (0 when this was its last) so the chip's "waiting" count
        # tracks the drain. Done before spawn() so an immediate re-queue there
        # (still too soon since last start) re-bumps it correctly afterwards.
        self._manager._emit_queue_depth(
            str(params.get("parent_session_key", "")), str(params.get("batch_id", ""))
        )
        # spawn() re-checks the gate; since elapsed >= stagger and a slot is
        # free, it starts immediately and updates _last_spawn_ts. Forward the FULL
        # kwarg set so approval_mode / silent / model / allowed_tools / bare survive
        # the queue round-trip — including `_preassigned_id`, which makes the agent
        # start under the id its caller was already told (and, if the gate re-queues
        # it, keeps that id across the second round-trip too).
        if dispatch is None:
            drained = self._manager.spawn(**params, _from_queue=True)
        else:
            # Event-loop pump: the dispatcher hands the picked row back and
            # takes the claim on the writer thread (see ``_dispatch_async_impl``).
            dispatch(params)
            return
        self._after_dispatch_impl(params, drained, refill=refill)

    def _after_dispatch_impl(
        self,
        params: dict[str, Any],
        drained: "SubagentInfo | None",
        *,
        refill: "Callable[..., int]",
    ) -> None:
        """What the pump does once a picked row has been handed to ``spawn``."""
        if (
            drained is not None
            and drained.queued is True
            and drained.done is True
            and drained.user_stopped is True
        ):
            # The store refused the claim: cancelled while it waited, under a
            # cancel that never saw an ``_agents`` record. Nothing started;
            # take the next row.
            if self._manager._queue:
                self._manager._drain_queue()
            return
        # A drained spawn has NO synchronous reader: this call site is a timer
        # callback, and the original caller was handed a queued info long ago. So a
        # terminal rejection here — the cwd was deleted while the run waited, the
        # agent stopped resolving — was dropped on the floor: no completion event,
        # and the caller's own bookkeeping showed the run as still going. Crew left
        # such a topic `running` forever.
        #
        # Only for NON-batch runs, which is exactly the set `_announce_rejection`
        # skips (it announces batch members itself, from inside `spawn`). Announcing
        # regardless double-counted a queued batch rejection: the wave's own
        # accounting closed early and emitted a duplicate or incomplete digest.
        if (
            drained is not None
            and drained.done
            and drained.error
            and not drained.batch_id
            and self._manager._on_done
        ):
            try:
                self._manager._tasks[f"reject-{drained.id}"] = asyncio.ensure_future(
                    self._manager._safe_announce(drained)
                )
            except RuntimeError:
                pass  # no running loop (sync/test context)
        # Top the window back up after the pop, so the chip's depth and the
        # next drain both see the oldest rows already in memory. On the
        # coroutine path *refill* is a no-op here and the follow-up pass
        # scheduled below (or the next capacity change) tops it up off-loop.
        refill()
        if (self._manager._queue or self._manager._admission.taskq_store() is not None) and (
            self._manager._admission.capacity_view().any_slot
        ):
            try:
                asyncio.get_event_loop().call_later(
                    self._manager._spawn_stagger_secs, self._manager._drain_queue
                )
            except RuntimeError:
                pass

    async def _spawn_with_approval_impl(self, info: SubagentInfo) -> None:
        """Request approval before starting the subagent.

        If approval is denied the subagent is marked as done with an
        error and the running count is decremented without executing.

        A callback that has nowhere to raise the prompt reports it by raising
        ``SpawnApprovalUnreachable``, and the spawn is refused right here rather
        than left registered until the reaper's deadline. Waiting is only correct
        when a prompt actually reached a surface and went unanswered; when it
        reached none, the wait can only end one way and costs the caller the full
        deadline to learn it.

        Args:
            info (SubagentInfo): The subagent metadata.
        """
        assert self._manager._on_spawn_approval is not None
        request_id: str = f"spawn:{info.id}"
        # Set only on the unreachable path, where it carries the refusal prose.
        # Also the flag that picks the audit reason below, so the two cannot
        # drift apart.
        no_surface_error: str = ""
        try:
            from kiro_crew.security import (
                redact_credentials,
                redact_exfiltration_urls,
            )

            task_safe, _ = redact_exfiltration_urls(info.task)
            task_safe, _ = redact_credentials(task_safe)
            task_preview: str = task_safe[:80]
            # Mark the pre-execution spawn gate as a human-wait so the reaper
            # does not misreport it. This is the SAME lifecycle the mid-run TOOL
            # approvals use in run.py: set before the await, cleared in a
            # finally. The run has NOT started here (_exec_started is None),
            # which is exactly what lets _force_reap distinguish a never-answered
            # spawn approval from a mid-run tool prompt and report the accurate
            # cause.
            info._awaiting_approval = True
            # Name the wait as well as marking it. The flag above is machine
            # state read by the reaper and by the wire; this is the line an
            # operator gets. Without it an operator has no lead at all:
            # ``kirocrew logs`` holds no record keyed to the affected run id,
            # while a wait with no deadline of its own holds the run at turn 0.
            # ``parent_session_key`` is in the record on purpose: an unowned
            # spawn (the CLI posts none) raises its prompt with ``slot=""``, so
            # it is surfaced only on the global approvals feed and appears in no
            # chat tab, which is the case with the least other evidence.
            logger.info(
                "Subagent %s awaiting spawn approval (request_id=%s, parent=%s)",
                info.id,
                request_id,
                info.parent_session_key or "<unowned>",
            )
            try:
                approved: bool = await self._manager._on_spawn_approval(
                    request_id, f"spawn_run({task_preview})", info.parent_session_key
                )
            finally:
                info._awaiting_approval = False
        except SpawnApprovalUnreachable as unreachable:
            # Not a refusal: nobody was there to refuse. Ordered ABOVE the
            # generic handler below, which would otherwise flatten this into the
            # same "spawn rejected" a human decline produces — and the generic
            # prose is slow to diagnose.
            #
            # The raiser names the missing SURFACE; the rungs are this gate's own
            # cascade. Keeping the split means the sentence does not go stale
            # when a channel learns to deliver the prompt itself.
            detail = (
                str(unreachable).strip() if info.memory_mode == "persistent" else ""
            ) or "no interactive surface is attached"
            # TWO AUDIENCES, and which text each gets is a security decision, not
            # a formatting one. The rung list is the OPERATOR's: it names two
            # `config.json` keys, and `security.py` records that `config.json` is
            # writable by any auto-approved agent shell. `info.error` travels to
            # the calling agent as a completion event — automation input — so
            # putting the how-to there hands the party this gate CONSTRAINS the
            # recipe for removing it, which an unattended or prompt-injected
            # agent can simply follow. The log is where an operator looks, so
            # the how-to lives here and nowhere the agent can read it.
            logger.warning(
                "Subagent %s refused: the spawn approval prompt reached no "
                "surface that could answer it (%s, parent=%s). To let spawns run "
                "without a prompt, use any one of: spawn with "
                'approval_mode="auto"; turn on Trust for the parent session in '
                "the dashboard; set hooks.auto_approve_subagent_spawn to true in "
                'config.json; or add "subagent" to hooks.auto_approve_sources.',
                info.id,
                detail,
                info.parent_session_key or "<unowned>",
            )
            approved = False
            # Terse, and names no file and no key — so it is actionable for the
            # agent (tell the human, or stop delegating) without being followable
            # into a self-granted bypass.
            no_surface_error = (
                "spawn rejected: no surface could show the approval prompt, so "
                f"nobody could answer it ({detail}). The spawn was refused now "
                "rather than held until the reaper's deadline. Ask the operator "
                "to open the dashboard and spawn again, or to enable spawn "
                "auto-approval."
            )
        except Exception:
            logger.error(
                "Spawn approval failed for %s", info.id, exc_info=info.memory_mode == "persistent"
            )
            approved = False

        if not approved:
            info.done = True
            # Prose only, deliberately no ``error_code``. The one reader of
            # that field (``POST /api/spawn``) runs BEFORE this task does, so a
            # code minted here would reach no caller — and an unread code is
            # contract surface bought for nothing (see ``error_code``'s own
            # note in ``subagent.py``). The audit ``reason`` below is what
            # separates this from a decline for a machine; the prose is what
            # separates it for the agent that receives the completion event.
            info.error = no_surface_error or "spawn rejected"
            # Slot accounting through the one-shot token, NOT a bare decrement.
            # A user Stop funnels into `_force_reap` and can land while this
            # approval is still pending (a human prompt has no deadline), and
            # `_force_reap` releases the slot and reports. A bare decrement here
            # would double-release — driving `_running_count` negative — and the
            # announce below would double-report the completion.
            if self._manager._release_slot(info):
                self._manager._running_count -= 1
                self._manager._drain_queue()
            self._manager._tasks.pop(info.id, None)
            # ``outcome`` keeps its existing vocabulary — the refusal is still a
            # rejection — and the reason rides in metadata, so an auditor can
            # tell a declined spawn from an undeliverable one without a new
            # outcome value to teach every reader.
            _reject_meta: dict[str, str] = {"subagent_id": info.id}
            if no_surface_error:
                _reject_meta["reason"] = "no_approval_surface"
            sel().log_tool_invocation(
                session_key=info.parent_session_key,
                source="subagent",
                tool_name="spawn_run",
                outcome="rejected",
                metadata=_reject_meta,
            )
            logger.info("Subagent %s spawn rejected", info.id)
            # Report ownership through the same claim every other terminal path
            # uses, so a concurrent reap/stop cannot also announce.
            if self._manager._on_done and self._manager._claim_finalize(info):
                await self._manager._safe_announce(info)
            return

        self._manager._log_spawned(info)
        await self._manager._run(info)

    def _log_spawned_impl(self, info: SubagentInfo) -> None:
        """Record spawn metrics and audit log entry.

        Args:
            info (SubagentInfo): The subagent metadata.
        """
        # Persist agent folder to disk for orphan recovery
        try:

            create_agent_folder(
                info.id,
                task=info.task,
                agent=info.agent,
                parent_session=info.parent_session_key,
                max_turns=info.max_turns,
                context_groups=_context_groups_field(info),
                delegation=info.delegation,
                memory_store=info.memory_store,
                execution_context=info.execution_context,
                memory_mode=info.memory_mode,
                app=info.app,
            )
        except Exception:
            logger.warning(
                "Failed to create agent folder for %s",
                info.id,
                exc_info=info.memory_mode == "persistent",
            )
            # The run task may already be registered. Its normal terminal path
            # settles the failure before allocating a provider, for every store.
            info.error = "memory_unavailable: could not persist this run's memory binding"
            return

        # Written HERE, past the folder write, for the reason the stat below is:
        # this is the point a start is confirmed. A run whose memory binding
        # could not be persisted settles as a failure without ever allocating a
        # provider, and its pin was never opened, so nothing closes an opener
        # that was never written.
        self._record_crew_log_spawn_started(info)
        Stats().inc_subagent_spawned()
        # Beside that stat, and for the same reason: this is the confirmed-start
        # funnel. Every path reaches it only AFTER the spawn is approved -- the
        # approval path calls it once the user allows and returns earlier on a
        # rejection -- so a rejected or unstarted spawn is never counted, which
        # the admission-time increment could not promise. ``concurrency`` is the
        # live running count, bounded by ``_max_concurrent``, so the aggregator's
        # MAX over that attribute is the concurrency high-water mark without a
        # second instrument.
        #
        # Imported HERE, not at module scope: ``bind_component_globals`` rebinds
        # every ``*_impl`` function's ``__globals__`` to ``subagent``'s namespace
        # for patch compatibility, so a module-level import in this file is not
        # visible from inside this function at all.
        try:
            from kiro_crew.metrics.events import SUBAGENTS_SPAWNED, emit_counter

            emit_counter(
                SUBAGENTS_SPAWNED,
                {
                    "concurrency": self._manager._running_count,
                    "batched": bool(getattr(info, "batch_id", "")),
                },
            )
        except Exception:
            logger.debug(
                "subagent spawned counter failed", exc_info=info.memory_mode == "persistent"
            )
        sel().log_tool_invocation(
            session_key=info.parent_session_key,
            source="subagent",
            tool_name="spawn_run",
            outcome="spawned",
            metadata=(
                {"subagent_id": info.id, "agent": info.agent or "kirocrew", "cwd": info.cwd}
                if info.memory_mode == "persistent"
                else {"subagent_id": info.id}
            ),
        )
        if info.memory_mode == "persistent":
            logger.info("Subagent %s spawned: %s", info.id, info.task[:80])
        else:
            logger.info("Subagent %s spawned", info.id)

    #: ``taskq_claim`` reason: the store exists but could not be reached for
    #: the claim. The row is NOT started -- an unclaimed start would run at
    #: generation 0 with no lease for reconcile to find -- it stays queued.
    CLAIM_UNAVAILABLE = "claim_unavailable"
    #: ``claim_and_start`` reason: the row is already ADMITTED under this
    #: process, but its post-claim durable check could not finish. The caller
    #: receives a queued handle while the retained-claim pump owns retry.
    CLAIM_RETAINED = "claim_retained"
    #: ``taskq_claim`` reason: the store knows the row and refuses it
    #: (cancelled, or claimed by another dispatcher).
    CLAIM_REFUSED = "claim_refused"

    def taskq_claim(self, agent_id: str) -> tuple[int, bool, str]:
        """``(generation, proceed, reason)`` for a spawn about to register.

        ``proceed`` is False when the store KNOWS the row and refuses it
        (cancelled, or already claimed by another dispatcher --
        :attr:`CLAIM_REFUSED`) AND when the store could not be reached
        (:attr:`CLAIM_UNAVAILABLE`): a run that starts without a claim holds
        no lease, so reconcile could never see or fence it. Only a row the
        store never saw -- a legacy in-memory entry with no durable store at
        all -- proceeds with generation 0.
        """
        store = self.taskq_store()
        if store is None:
            return (0, True, "")
        from kiro_crew import taskq as _taskq

        try:
            claimed = store.claim(agent_id)
            if claimed is not None:
                return (claimed.generation, True, "")
            state = store.state_of(agent_id)
        except _taskq.TaskStoreUnavailable:
            _glue_logger.warning("taskq: claim of %s failed", agent_id, exc_info=True)
            return (0, False, self.CLAIM_UNAVAILABLE)
        if state is None:
            return (0, True, "")
        if state == _taskq.ADMITTED and self.taskq_lease_is_ours(agent_id):
            # Already claimed by THIS dispatcher on an earlier pass (a stagger
            # re-queue): keep the generation it was claimed under.
            rec = store.get(agent_id)
            return (rec.generation if rec else 0, True, "")
        _glue_logger.info("taskq: %s not started, store state is %s", agent_id, state)
        return (0, False, self.CLAIM_REFUSED)

    def taskq_claim_still_current(self, agent_id: str, generation: int) -> bool | None:
        """Whether this dispatcher still owns the admitted claim generation.

        ``None`` means the store could not answer and makes re-entry retry rather
        than registering work whose durable lease cannot be proved. Called only
        through :meth:`TaskStore.run`, immediately before loop registration.
        """
        store = self.taskq_store()
        if store is None:
            return False
        from kiro_crew import taskq as _taskq

        try:
            rec = store.get(agent_id)
        except _taskq.TaskStoreUnavailable:
            _glue_logger.warning(
                "taskq: claim revalidation of %s failed",
                agent_id,
                exc_info=True,
            )
            return None
        return bool(
            rec is not None
            and rec.state == _taskq.ADMITTED
            and rec.generation == generation
            and rec.lease_owner == store.incarnation
        )

    def taskq_lease_is_ours(self, agent_id: str) -> bool:
        store = self.taskq_store()
        if store is None:
            return False
        rec = store.get(agent_id)
        return rec is not None and rec.lease_owner == store.incarnation
