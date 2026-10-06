"""The dispatch pump: stagger, drain, the atomic claim -> dispatch -> register -> release unit, approval, start logging."""

from __future__ import annotations

import logging as _logging
from typing import TYPE_CHECKING, Any

from .._component import ManagerComponent
from .types import MIN_RECHECK_DELAY_SECS, ClaimPoint

_glue_logger = _logging.getLogger("kiro_crew.subagent_manager.admission")

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from kiro_crew import taskq as _taskq

    from ...subagent import (
        _RELEASE_REPUMP_SECS,
        DENY_CAUSE_APPROVAL_UNDELIVERABLE,
        DENY_CAUSE_HOOK_ERROR,
        MEMORY_PRESSURE_NEVER_STARTED,
        SpawnAdmissionCoordinator,
        SpawnApprovalUnreachable,
        Stats,
        SubagentInfo,
        _context_groups_field,
        asyncio,
        create_agent_folder,
        logger,
        parent_spawn_policy,
        sel,
        time,
    )


class _PumpMixin(ManagerComponent):
    __slots__ = ()

    if TYPE_CHECKING:
        # Sibling-mixin methods this module reaches through ``self``; typing only.
        MEMORY_WAIT_UNTIL_KEY: str

        def taskq_store(self) -> "_taskq.TaskStore | None": ...

        def taskq_admit_wait_secs(self) -> float: ...

        async def taskq_child_registered_async(self, info: "SubagentInfo") -> None: ...

        def _record_crew_log_spawn_started(self, info: "SubagentInfo") -> None: ...

        def _record_crew_log_spawn_approval_requested(
            self, info: "SubagentInfo", *, approval_id: str, reason: str
        ) -> "tuple[str, int]": ...

        @staticmethod
        def entry_is_resident_resume(params: "Mapping[str, Any]") -> bool: ...

        @staticmethod
        def entry_is_child(params: "Mapping[str, Any]") -> bool: ...

    def _should_stagger_queue_impl(self, now: float) -> tuple[bool, bool]:
        """Decide whether a spawn arriving at *now* must be queued.

        Returns ``(should_queue, slot_free)``. A spawn is queued when any of
        three holds: no slot is free (at capacity); a spawn started within the
        stagger window (``subagent_spawn_stagger_secs``) -- so the initial fill
        never bursts and no two agents start within the interval
        (dynamic-subagent-sizing.md §5.3); or as many agents are already in
        startup as ``_startup_cap`` allows (two session-start gate rounds)
        -- so a slow-start regime cannot pile the whole cap into startup at
        once. The three bound different things: the RUNNING population, the
        RATE of starts, and the IN-STARTUP population. ``slot_free`` reports
        only the first, so the caller can tell a hold that a running agent's
        exit will release from one that needs the pump re-armed.
        """
        # The cap as the fairness dispatcher reads it: the effective cap, lifted
        # for the child reserve while a parent waits under an adaptive squeeze
        # (``CapacityView``). The reserve's root-only narrowing is applied by
        # the caller, which knows whether the spawn is nested.
        slot_free = self._manager._admission.capacity_view().any_slot
        too_soon = (now - self._manager._last_spawn_ts) < self._manager._spawn_stagger_secs
        startup_full = self._manager._startup_population() >= self._manager._startup_cap()
        return (not slot_free or too_soon or startup_full, slot_free)

    def _drain_queue_impl(self) -> None:
        """Spawn the next queued task if a slot is available and the stagger
        interval has elapsed.

        This is the single staggered pump: at most one start per
        ``subagent_spawn_stagger_secs`` (dynamic-subagent-sizing.md §5.3). If a
        slot is free but a spawn started too recently, it reschedules itself at
        the interval boundary rather than bursting.

        On a running event loop the pump is a COROUTINE
        (``_drain_queue_async``): the store reads that top the window up
        (``pending_lanes`` / ``fetch_dispatchable_fair`` / ``next_eligible_at``)
        and the wait-expiry sweep run on the store's writer thread through
        ``TaskStore.run``, the memory floor is read on a worker
        (``MemoryReadPoint``), and only the window mutation and the pick happen
        on the loop. That holds with no store too (the task queue off): the
        in-memory memory wait re-pumps from a loop timer, and an inline pump
        would read the host on the loop on every retry. One drain coroutine is
        in flight at a time; a request that lands while one runs is coalesced
        into one more pass. Without a running loop (sync callers, tests) the
        pump runs inline.
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
        if loop is None or not SpawnAdmissionCoordinator.pump_off_loop:
            self._drain_queue_sync_impl(refill=self._manager._admission.taskq_refill_window)
            # After the pass, like the coroutine pump: the rows it re-parked are
            # the ones whose wait may now be past the bound.
            if store is not None:
                self._manager._admission.taskq_expire_memory_waits()
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

        delay = max(MIN_RECHECK_DELAY_SECS, self.taskq_admit_wait_secs())
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
        # The retry settled the row -- it registered, was refused, or stopped --
        # unless it re-retained on a still-failing store. Drop its queryability
        # window only when it did NOT re-retain; a still-retained row stays
        # pending work the done-probe must keep the serial guard for.
        if agent_id not in self._manager._retained_claims:
            self._manager._dispatch_window_ids.discard(agent_id)
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
        # Bound before the ``try``: the ``finally`` below walks ``picked``, and a
        # store error raised by the awaits ahead of the pick must reach the
        # handler as itself, not as an UnboundLocalError over an empty pick.
        picked: list[dict[str, Any]] = []
        granting: list[dict[str, Any]] = []
        in_dispatch = self._manager._undurable_in_dispatch
        try:
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
            # The pick's own store read: an entry that does not name its lane
            # resolves its parent chain through ``store.get``. Resolved here, on
            # the writer thread, and nothing awaits between this and the pick
            # below, so the window these lanes describe is the one picked from.
            lanes = await admission.resolve_window_lanes_async()

            def _pick(params: dict[str, Any]) -> None:
                # Held from the pop, not from its own dispatch: the grants below
                # await first, and a stop in that gap must find the row too.
                picked.append(params)
                if self._is_undurable(params, store):
                    in_dispatch[str(params["_preassigned_id"])] = params

            self._drain_queue_sync_impl(
                refill=lambda **_kw: 0,
                dispatch=_pick,
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
                held = self._is_undurable(params, store)
                try:
                    if held and in_dispatch.get(str(params["_preassigned_id"])) is not params:
                        # A stop took the held row while the grants awaited and
                        # has already reported it: nothing to start.
                        drained = None
                    else:
                        drained = await self._dispatch_async_impl(params)
                finally:
                    # The dispatching mark lives for ONE attempt. Whatever
                    # ``spawn`` answered -- started, re-queued, parked, refused,
                    # or raised -- the row is either registered (live-excluded)
                    # or back in the store as waiting, and either way the
                    # count and the refill must see it as the store does. A
                    # retained claim is the exception: it holds a reserved slot
                    # and is still pending with no ``_agents`` row, so its
                    # queryability window survives until the retry settles it.
                    retained = str(params.get("_preassigned_id") or "") in (
                        self._manager._retained_claims
                    )
                    self._unmark_dispatching(params, retained=retained)
                self._after_dispatch_impl(params, drained, refill=lambda **_kw: 0)
            # Last, so a row this pass re-checked and re-parked for memory is
            # already parked again when its wait is measured against the bound.
            if store is not None:
                await admission.taskq_expire_memory_waits_async()
        except Exception:
            logger.error("drain pump failed", exc_info=retain_error_detail)
        finally:
            # A row the pick marked but the loop never reached (a raise in the
            # granting loop, or a cancelled pass) would otherwise keep its mark
            # for the process lifetime and be skipped by every refill: the
            # durable row would never run again. The store is the truth for
            # every row this pass did not dispatch. A row the inner loop
            # retained is the exception: its claim is held for the retry and it
            # is still pending with no ``_agents`` row, so its queryability
            # window must survive this sweep exactly as it survived the inner
            # one -- erasing it here would let the done-probe read the id as
            # finished before the retry runs.
            for params in picked:
                agent_id = str(params.get("_preassigned_id") or "")
                retained = agent_id in self._manager._retained_claims
                self._unmark_dispatching(params, retained=retained)
                # A held row the pass never handed to the gate (a raise in the
                # grants, or in an earlier step) is still its only copy: back
                # to the window, not lost with the pass.
                if agent_id and in_dispatch.get(agent_id) is params:
                    del in_dispatch[agent_id]
                    self._requeue_undispatched(params)

    def _unmark_dispatching(self, params: "Mapping[str, Any]", *, retained: bool = False) -> None:
        """Drop the popped row's dispatching mark, if it carries an id.

        ``_dispatching_ids`` is the depth-count exclusion and is always dropped
        once the attempt ends -- a retained claim holds a reserved slot, so it
        is already out of the waiting count. ``_dispatch_window_ids`` is the
        queryability window the done-probe reads: a retained claim is still
        pending work with no ``_agents`` row, so its id is KEPT until the
        retained claim registers or is refused (``retry_retained_claims``);
        dropping them here would let the probe read the id as finished and
        release the caller's serial guard.
        """
        agent_id = str(params.get("_preassigned_id") or "")
        self._manager._dispatching_ids.discard(agent_id)
        if not retained:
            self._manager._dispatch_window_ids.discard(agent_id)

    @staticmethod
    def _is_undurable(params: "Mapping[str, Any]", store: Any) -> bool:
        """Whether a popped row has no durable record to survive the dispatch.

        Incognito or temporary memory never writes one, and neither does any
        row while the task queue is off; such a row is held in
        ``_undurable_in_dispatch`` from its pop until the gate takes it.
        """
        return bool(params.get("_preassigned_id")) and (
            store is None or params.get("_memory_mode", "persistent") != "persistent"
        )

    def _requeue_undispatched(self, params: dict[str, Any]) -> None:
        """Put a non-durable row back in the window after its dispatch raised.

        The coroutine pump popped it and its off-loop reads failed (a pool that
        cannot start a thread is one way) before the gate took it, so this is
        its only copy. It goes back at the front, not eligible again until the
        admit wait passes (``MEMORY_WAIT_UNTIL_KEY``), so a failure that lasts
        is retried at that pace instead of on every pass. A row the gate
        already registered or re-queued is left alone.
        """
        agent_id = str(params.get("_preassigned_id") or "")
        if agent_id in self._manager._agents or any(
            str(p.get("_preassigned_id") or "") == agent_id for p in self._manager._queue
        ):
            return
        import time as _time

        # Off the dispatching mark first, so the depth below counts it waiting.
        self._unmark_dispatching(params)
        until = _time.monotonic() + self.taskq_admit_wait_secs()
        params[self.MEMORY_WAIT_UNTIL_KEY] = until
        self._manager._queue.insert(0, params)
        _glue_logger.warning("Queued spawn %s: dispatch failed, re-queued for a retry", agent_id)
        self._manager._emit_queue_depth(
            str(params.get("parent_session_key", "")), str(params.get("batch_id", ""))
        )
        self._manager._admission.arm_memory_wait(until)

    async def _dispatch_async_impl(self, params: dict[str, Any]) -> "SubagentInfo | None":
        """Start a picked window row with its claim (``store.claim``) on the
        writer thread: the gates run on the loop and stop at the claim
        (``ClaimPoint``), the claim is awaited through ``TaskStore.run``, and
        registration re-enters with the result (``_claimed``). A row the
        pressure gate parked instead stops one step earlier and its
        ``store.defer`` is awaited the same way (``DeferPoint``)."""
        store = self._manager._admission.taskq_store()
        admission = self._manager._admission
        # A row with no durable record has no other copy while the awaits below
        # run: it is held where a stop can find it, and every await is followed
        # by a check that no stop took it (``_undurable_in_dispatch``).
        agent_id = str(params.get("_preassigned_id") or "")
        undurable = self._is_undurable(params, store)
        in_dispatch = self._manager._undurable_in_dispatch
        if undurable:
            in_dispatch[agent_id] = params

        def _still_wanted() -> bool:
            return in_dispatch.get(agent_id) is params

        try:
            # The parent agent spec's ``availableAgents`` declaration is re-read
            # here, off the loop, so the gate's re-check at dispatch costs the
            # loop no directory scan (same reason the record read is threaded).
            policy = await asyncio.to_thread(
                parent_spawn_policy, str(params.get("parent_session_key") or "")
            )
            # Neither the queued entry nor the durable row carries a policy
            # (``queue_params`` never stores one, ``taskq_build_record`` drops
            # the key); the filter keeps a params dict that somehow holds one
            # from shadowing the fresh read. ``params`` itself stays whole: it
            # is the row's identity for the stop path below.
            spawn_params = {k: v for k, v in params.items() if k != "_parent_spawn_policy"}
            # The drain's agent re-validation, off the loop for the same reason.
            agent_check = await self._manager._check_agent_off_loop(
                str(params.get("agent") or ""),
                str(params.get("cwd") or ""),
                app=str(params.get("app") or ""),
                execution_context=params.get("_execution_context"),
                prevalidated=bool(params.get("_agent_prevalidated")),
            )
            if undurable and not _still_wanted():
                # Stopped while the reads ran: the stop already reported it.
                self._unmark_dispatching(params)
                return None
            # The memory floor is read on a worker: the gate stops at its read
            # (``MemoryReadPoint``) and is re-entered with the reading and these
            # same flags (``_spawn_after_memory_read``).
            reentry: dict[str, Any] = dict(
                _parent_spawn_policy=policy,
                _agent_check=agent_check,
                _from_queue=True,
                _stop_before_claim=store is not None,
                _child_registration=store is None,
            )
            first: Any = await self._manager._spawn_after_memory_read(
                self._manager.spawn(**spawn_params, **reentry, _stop_before_memory_read=True),
                _still_wanted if undurable else None,
                **reentry,
            )
        except Exception:
            if undurable and in_dispatch.pop(agent_id, None) is params:
                self._requeue_undispatched(params)
            raise
        finally:
            if in_dispatch.get(agent_id) is params:
                del in_dispatch[agent_id]
        if not isinstance(first, ClaimPoint):
            # Not claimed: the gate re-queued, parked or refused the row, so
            # it is waiting (or gone) again and the depth must count it as the
            # store sees it. Cleared BEFORE the parked defer publishes its
            # depth, or that emit would read the row as still dispatching.
            self._unmark_dispatching(params)
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
                **spawn_params,
                _parent_spawn_policy=policy,
                _agent_check=agent_check,
                _from_queue=True,
                _claimed=claimed,
                _child_registration=False,
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

        Between the claim and the registration the row is re-read (still
        ``admitted``, same generation, our lease) and the loop is checked for a
        stop recorded meanwhile; either one failing refuses the start. A
        failure before the claim leaves the row queued and releases the
        reservation. A store outage after the claim retains the admitted
        generation and reservation for a later pump pass. Registration consumes
        the reservation; every durably refused outcome releases it.
        """
        store = self.taskq_store()
        assert store is not None
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
            if proceed:
                import asyncio as _asyncio

                from kiro_crew import taskq as _taskq

                # Every claim the store took re-reads its row before it
                # registers. The claim was a writer-thread hop, and a stop that
                # ran while the pump waited on it (Stop all's
                # ``taskq_cancel_queued`` accepts an ``admitted`` row) has
                # cancelled the row by now: registering anyway starts work the
                # parent was told had stopped. Generation 0 is a row the store
                # never saw (``taskq_claim``), which has nothing to re-read and
                # keeps its legacy start.
                try:
                    still_current = (
                        await store.run(
                            self.taskq_claim_still_current,
                            point.agent_id,
                            generation,
                        )
                        if generation
                        else True
                    )
                    # A Stop all batch whose cancel of this row was queued
                    # AFTER that re-read answers it stale, and installs no
                    # record until its answer comes back. Wait for that answer,
                    # then re-read behind the cancel: the start is decided only
                    # by a read the cancel cannot have overtaken.
                    while still_current and generation:
                        batched = self._batched_stop_of(point.agent_id)
                        if batched is None:
                            break
                        await _asyncio.shield(batched)
                        still_current = await store.run(
                            self.taskq_claim_still_current,
                            point.agent_id,
                            generation,
                        )
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
                elif self._stopped_while_claimed(point.agent_id):
                    # The re-read answered before a stop landed, and the stop
                    # ran before this coroutine resumed. A queued stop installs
                    # its record synchronously, and a batched one's answer was
                    # waited for above, so the loop's own state is the last
                    # word: the row is not ours to start.
                    claimed = (generation, False, self.CLAIM_REFUSED)
            # No await between a successful final durable check and
            # registration: cancellation cannot interleave after the
            # revalidation a registered start relies on. A refused claim may
            # await its terminal store write because it never registers.
            claim_will_register = bool(claimed[1])
            if not claim_will_register:
                # The claim did not take the row (store unavailable, refused,
                # superseded): it is still QUEUED and the re-entry's own depth
                # request must count it. A read takes the exclusion set when it
                # starts, after the request (a burst's first read runs in its
                # own task; one in flight reads again), so dropping the mark
                # here, before re-entry asks, is always seen by that read.
                self._manager._dispatching_ids.discard(point.agent_id)
                self._manager._dispatch_window_ids.discard(point.agent_id)
            result = reenter(claimed)
        finally:
            # Queued-stop reporting temporarily installs a synthetic terminal
            # record under this id. Only a still-proceeding claim that really
            # registered may consume the reservation; terminal report identity
            # is not a registered start. A retained claim keeps the reservation.
            registered = claim_will_register and point.agent_id in self._manager._agents
            if not registered and not claim_retained:
                self.release_reservation(point.agent_id)
            if registered:
                # Every registered start re-publishes the parent's queued depth.
                # A direct spawn owes nothing to the count, so its emit reports
                # the depth as it stands. A row the drain popped was asked for
                # at the pop, but that read may have failed (nothing published,
                # a retry armed), and this request is the one that follows the
                # row out of the claimable states; asked twice, the burst
                # coalesces it into at most one more read.
                started = self._manager._agents[point.agent_id]
                self._manager._emit_queue_depth(started.parent_session_key, started.batch_id)
        assert not isinstance(result, ClaimPoint)
        return result

    def _batched_stop_of(self, agent_id: str) -> "asyncio.Future[Any] | None":
        """The answer a Stop all batch owes *agent_id*, while its cancel is in
        flight (``cancellation._stop_queued`` files it), else ``None``."""
        return self._manager.__dict__.get("_batched_stops", {}).get(agent_id)

    def _stopped_while_claimed(self, agent_id: str) -> bool:
        """Whether a stop was recorded for *agent_id* while its claim was in flight.

        A claimed row has no ``_agents`` record until it registers, so a
        stopped or ended record there was put there by a stop: the queued-stop
        report's ``user_stopped`` terminal is the usual one. The inline pump
        applies the same test to a popped row before it spawns one.
        """
        waiting = self._manager._agents.get(agent_id)
        return waiting is not None and bool(waiting.done or waiting.user_stopped or waiting.reaped)

    def release_reservation(self, agent_id: str) -> None:
        """Give back the slot a ``ClaimPoint`` reserved for a row that did not start."""
        self._manager._running_count = max(0, int(self._manager._running_count) - 1)
        self._manager._startup_reservations = max(0, int(self._manager._startup_reservations) - 1)
        self._manager._claim_prices.pop(agent_id, None)
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
        # Approval-released starts first (they hold their slots already, so
        # this must precede the capacity check): one per pass, under the
        # stagger and the in-startup bound. Neither outcome ends the pass --
        # a RESUME waits on a lane slot, never on the startup bound or the
        # stagger, so the grants below run whether a start was released or is
        # being held; and the fresh-spawn pick further down applies the same
        # two checks itself, so a hold here is a hold there too. Guarded by the
        # scan so a minimal facade with only a queue still pumps.
        if any(p.get("_startup_release") for p in self._manager._queue):
            self._release_admitted_start_impl()
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
        # to the waiting coroutine instead of spawning.
        while self._manager._queue:
            index = next(
                (i for i, p in enumerate(self._manager._queue) if self.entry_is_resident_resume(p)),
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
        # In-startup bound (``_startup_cap``, two session-start gate rounds): as many
        # agents as ``_startup_cap`` allows are past admission but have no
        # runtime, answer or turn yet. Hold the pick -- the resumes above were
        # granted, a resume is not a start -- and arm nothing: the next edge is
        # one of them leaving startup, which ``_note_startup_progress`` (PID or
        # first answer) and the slot-release drain (terminal, including the
        # watchdog's reap of a wedged one) both pump. A timer here would only
        # poll for those same edges.
        if self._manager._startup_population() >= self._manager._startup_cap():
            return
        # Lane-aware pick: the weighted round-robin over the lanes with
        # eligible entries (resumes were granted above). When only the child
        # reserve is left, roots are not eligible; the window is topped up
        # with nested rows so the reserve can be used.
        # The kernel memory-pressure hold keeps root entries out of the pick, the
        # way the child reserve does; read once per pass, and only if a root
        # entry is considered at all.
        pressure: list[int | None] = []

        def _root_held(params: Mapping[str, Any]) -> bool:
            if not pressure:
                pressure.append(self._manager._memory_pressure_hold())
            level = pressure[0]
            # An expired row is picked: the gate's re-check ends it, never started.
            return (
                level is not None
                and self._manager._memory_pressure_holds(
                    str(params.get("_preassigned_id") or ""),
                    level,
                    parent_session_key=str(params.get("parent_session_key") or ""),
                    batch_id=str(params.get("batch_id") or ""),
                    relabel=True,
                    commit_expiry=False,
                )
                == "held"
            )

        index = self._manager._admission.pick_window_index(view, lanes=lanes, root_held=_root_held)
        if index is None:
            if refill(children_only=True) > 0:
                index = self._manager._admission.pick_window_index(
                    view, lanes=lanes, root_held=_root_held
                )
        if index is None:
            return
        params = self._manager._queue.pop(index)
        params.pop("_lane", None)
        params.pop(self.MEMORY_WAIT_UNTIL_KEY, None)
        # The floor mark is one-shot: the gate re-checks the floor now and sets
        # it again if the row is deferred on it again.
        self._manager._floor_deferred_ids.discard(str(params.get("_preassigned_id") or ""))
        # A run can be cancelled WHILE it waits here — a user stop, or a session
        # deleted out from under it. Starting it anyway would execute tools for
        # work already reported as stopped, so skip it and drain the next one
        # instead: `cancel()` marks the info terminal but cannot unqueue this.
        queued_id = str(params.get("_preassigned_id") or "")
        if queued_id:
            if self._stopped_while_claimed(queued_id):
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
        # The popped row is in flight between the window and its claim: it is
        # in none of the exclusion sets the store count reads (not windowed,
        # not registered, not admitting) while its durable state is still
        # QUEUED, so every depth read until the claim lands counts it as
        # waiting. Mark it dispatching so the depth request below and any
        # refill in the meantime leave it out (the pending-work guards still
        # count it: it is accepted work until the claim lands). The mark lives
        # for one attempt and is released where the attempt ends: the ``finally``
        # around each ``spawn`` call (inline pump below, coroutine pump in
        # ``_drain_queue_pass_impl``), the not-a-claim branch of
        # ``_dispatch_async_impl``, the non-proceeding claim in
        # ``claim_and_start``, and the gate's failed-claim emit.
        if queued_id:
            self._manager._dispatching_ids.add(queued_id)
            self._manager._dispatch_window_ids.add(queued_id)
        # The popped item's parent just lost one waiting agent — ask for its
        # queued depth (0 when this was its last) so the chip's "waiting" count
        # tracks the drain. The read runs later, as its own task, and the mark
        # above already leaves the row out; a re-queue in spawn() (still too
        # soon since last start) asks again, and the coalesced emit reads after
        # both.
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
            try:
                drained = self._manager.spawn(**params, _from_queue=True)
            finally:
                # Same one-attempt lifetime as the coroutine pump: a ``spawn``
                # that raises must not leave the row excluded from refill.
                self._unmark_dispatching(params)
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
        # The crew-log pair for this prompt, which is the only record of the wait
        # a fold can read: the SEL audit below says how the spawn ended, not that
        # anyone was ever asked. ``_log_origin`` is what the request entry was
        # filed under, so the decision lands beside its own request rather than
        # under whatever turn the parent reached while a person took their time.
        #
        # The decision fields are PRE-SEEDED with the reading that holds for an
        # exit neither handler below sees: a user Stop or a reap cancels this
        # task, and a CancelledError is not an ``Exception``. Nothing judged the
        # spawn there, so it is attributed to the host with no reason code --
        # the same shape the chat runner's own host-cancelled approval writes.
        # Seeding them and writing in a ``finally`` is what makes the pair total
        # over every exit; an unanswered request left in the fold's ``pending``
        # map forever is the same silence this fix removes, one step along.
        _log_origin: tuple[str, int] = ("", 0)
        _log_decision: str = "rejected"
        _log_by: str = "host"
        _log_cause: str = ""
        try:
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
                # Before the await, so a fold read while the prompt is still open
                # shows it as pending -- which is the whole point of recording it.
                _log_origin = self._record_crew_log_spawn_approval_requested(
                    info, approval_id=request_id, reason=f"spawn_run({task_preview})"
                )
                try:
                    approved: bool = await self._manager._on_spawn_approval(
                        request_id, f"spawn_run({task_preview})", info.parent_session_key
                    )
                finally:
                    info._awaiting_approval = False
                # A person answered, at a surface this site cannot name, so the
                # entry asserts neither who decided nor why.
                _log_decision = "approved" if approved else "rejected"
                _log_by = ""
                _log_cause = ""
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
                _log_cause = DENY_CAUSE_APPROVAL_UNDELIVERABLE
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
                    "Spawn approval failed for %s",
                    info.id,
                    exc_info=info.memory_mode == "persistent",
                )
                approved = False
                _log_cause = DENY_CAUSE_HOOK_ERROR
        finally:
            # The request's answer, on every exit including the cancelled one.
            # A no-op when no request was written, so the two are all-or-nothing.
            self._record_crew_log_approval_decided(
                _log_origin,
                approval_id=request_id,
                decision=_log_decision,
                by=_log_by,
                cause=_log_cause,
            )

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

        # The prompt resolved; the START has not been admitted. While parked
        # this agent counted against nothing (it was starting nothing), so its
        # release is where the in-startup bound has to be applied -- and a bulk
        # trust/yolo grant releases every parked prompt in one pass. It goes
        # through the pump like a fresh spawn and is metered into startup by
        # the same stagger and in-startup checks. Three ways out, each its own
        # outcome, because they are announced differently:
        outcome = await self._manager._admit_released_start(info)
        if outcome == "admission_closed":
            # Refused at release: the run holds a slot and a row but never
            # started. Same terminal bookkeeping as a declined prompt, so the
            # slot, the queue and the parent's completion event all settle.
            info.done = True
            info.error = (
                "spawn rejected: the gateway closed admission before this "
                "approved spawn could start"
            )
            if self._manager._release_slot(info):
                self._manager._running_count -= 1
                self._manager._drain_queue()
            self._manager._tasks.pop(info.id, None)
            sel().log_tool_invocation(
                session_key=info.parent_session_key,
                source="subagent",
                tool_name="spawn_run",
                outcome="rejected",
                metadata={"subagent_id": info.id, "reason": "admission_closed"},
            )
            if self._manager._on_done and self._manager._claim_finalize(info):
                await self._manager._safe_announce(info)
            return
        if outcome == "never_started":
            # The macOS pressure hold kept it past its bound: ended, never
            # started, with the same terminal bookkeeping as a refusal here.
            info.done = True
            info.error = MEMORY_PRESSURE_NEVER_STARTED
            if self._manager._release_slot(info):
                self._manager._running_count -= 1
                self._manager._drain_queue()
            self._manager._tasks.pop(info.id, None)
            if self._manager._on_done and self._manager._claim_finalize(info):
                await self._manager._safe_announce(info)
            return
        if outcome != "admitted":
            # A user stop or a reap landed while the start waited for the bound.
            # Neither is a rejection: ``_force_reap`` owns that run's terminal
            # record (a stop is neutral; a reap names the wait it interrupted)
            # and its announce, so nothing is written here.
            return
        # The confirmed-start funnel: recorded only for a start that is
        # actually about to run, never for one refused or ended while waiting.
        self._manager._log_spawned(info)
        await self._manager._run(info)

    async def _admit_released_start_impl(self, info: SubagentInfo) -> str:
        """Wait for the pump to meter *info* -- released from the approval
        prompt -- into startup. Answers one of three outcomes: ``"admitted"``
        (it may run); ``"admission_closed"`` (gateway admission is closed at
        release time -- an approved start that has not begun is new work, and
        the updater's pause admits none; the caller writes that refusal); or
        ``"ended"`` (a user stop or a reap landed while it waited -- the path
        that ended it owns the terminal record and the announce, so the caller
        writes nothing).

        The entry reuses the queue's RESIDENT shape (``_resume_id``): every
        scan that separates unstarted spawns from resident runs -- the
        queued-stop paths, the refill census, the eviction, the continuation
        lookup -- already leaves such an entry alone, and the run IS resident:
        registered, holding its slot, its row claimed. ``_startup_release``
        tells the pump this resident is waiting to START rather than to resume,
        so it is admitted by the stagger + in-startup gate
        (:meth:`_release_admitted_start_impl`) and never by the resume grant, which
        hands back a yielded lane slot this run never gave up. Accounting while
        it waits: in ``_agents``, in ``_running_count``, in the queue depth its
        parent's chip shows, and NOT in ``_startup_population`` -- it is not
        starting until the pump says so.
        """
        # A stop or a reap that landed while the approval prompt was open wins
        # over the admission gate: its own path owns the record, and a closed
        # gateway must not add a rejection to a run already being ended.
        if info.done or info.user_stopped or info.reaped or info._reap_started:
            return "ended"
        # The same admission gate every registration in this package sits
        # behind: yield-free with the append below, so the start is either
        # queued for release before the updater's pause or refused after it.
        if getattr(self._manager._sessions, "admission_closed", False) is True:
            logger.info(
                "Subagent %s: approved start refused (gateway admission is closed)", info.id
            )
            return "admission_closed"
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        info._start_release = fut
        entry = {
            "_resume_id": info.id,
            "_startup_release": True,
            # The waiter itself, so the pump can always wake it -- including a
            # run that is not registered in ``_agents`` at the time it is metered.
            "_start_info": info,
            "parent_session_key": info.parent_session_key,
            "batch_id": info.batch_id,
        }
        self._manager._queue.append(entry)
        self._manager._emit_queue_depth(info.parent_session_key, info.batch_id)
        # The wait is edge-driven (PID / first answer / terminal / stagger
        # boundary all pump), with a slow self-re-arming re-pump as the
        # backstop: a pump pass that failed on an unrelated row is logged and
        # swallowed, and without this the released start would wait for the
        # next edge to arrive by itself. A timer rather than ``wait_for`` so the
        # wake stays ONE hop from ``set_result``: the pump re-arms itself at the
        # stagger boundary right after a release, and the released run's first
        # step (which puts it in ``_startup_population``) must land before that
        # re-arm can admit the next one -- the same ordering the direct
        # dispatch paths rely on.
        repump: Any = None

        def _repump() -> None:
            nonlocal repump
            repump = None
            if fut.done():
                return
            self._manager._drain_queue()
            repump = loop.call_later(_RELEASE_REPUMP_SECS, _repump)

        try:
            self._manager._drain_queue()
            if not fut.done():
                repump = loop.call_later(_RELEASE_REPUMP_SECS, _repump)
            granted = await fut
        finally:
            if repump is not None:
                repump.cancel()
            info._start_release = None
            # A stop or a reap while waiting leaves the entry behind; drop it
            # so the pump never meters a run that already ended.
            for index, params in enumerate(list(self._manager._queue)):
                if (
                    params.get("_startup_release")
                    and str(params.get("_resume_id") or "") == info.id
                ):
                    self._manager._queue.pop(index)
                    self._manager._emit_queue_depth(info.parent_session_key, info.batch_id)
                    break
        if info.done or info.user_stopped or info.reaped or info._reap_started:
            return "ended"
        if granted == "never_started":
            # The pressure hold ended it (``_release_admitted_start_impl``).
            return "never_started"
        return "admitted" if granted else "ended"

    def _release_admitted_start_impl(self) -> str:
        """The pump's released-start phase: meter ONE approval-released start
        into startup, under the same stagger and in-startup checks a fresh
        spawn passes. Returns ``"released"`` when a start was let into startup
        this pass, ``"held"`` when one is waiting but may not start yet (the
        stagger or the in-startup bound), and ``""`` when none is waiting. The
        caller treats none of these as the end of the pass: a held start holds
        only STARTS, and the resume grants that follow it wait on lane slots,
        not on the startup bound.

        Runs BEFORE the capacity check on purpose: a released start already
        holds its slot, so at a full cap ``any_slot`` is False and a pass that
        checked capacity first would never reach it -- two approved spawns at a
        cap of two would wait on each other forever. It also runs before the
        resume grants, since a released start is older than anything admitted
        after it and the bound it waits on is the one the resumes skip.
        """
        queue = self._manager._queue
        while True:
            index = next(
                (i for i, p in enumerate(queue) if p.get("_startup_release")),
                None,
            )
            if index is None:
                return ""
            params = queue[index]
            info = params.get("_start_info")
            fut = getattr(info, "_start_release", None) if info is not None else None
            if (
                info is None
                or fut is None
                or fut.done()
                or info.done
                or info.user_stopped
                or info.reaped
                or info._reap_started
            ):
                # Ended while waiting, or already released: not a start. Wake
                # the waiter with False so it returns without running.
                queue.pop(index)
                if fut is not None and not fut.done():
                    fut.set_result(False)
                continue
            break
        elapsed = time.monotonic() - self._manager._last_spawn_ts
        if elapsed < self._manager._spawn_stagger_secs:
            try:
                asyncio.get_event_loop().call_later(
                    self._manager._spawn_stagger_secs - elapsed, self._manager._drain_queue
                )
            except RuntimeError:
                pass  # no running loop (sync/test context)
            return "held"
        if self._manager._startup_population() >= self._manager._startup_cap():
            # Held; the next edge out of startup pumps again (see the same
            # hold on the spawn side below).
            return "held"
        # The kernel memory-pressure hold re-checked at release, so a prompt
        # answered after the hold began does not launch past it. It is decided
        # per entry (roots only, each with its own clock), so a held root is
        # passed over rather than blocking the nested starts queued behind it.
        # Its recheck timer and ``_RELEASE_REPUMP_SECS`` both pump again.
        # An entry that ended while it waited is retired here as in the head
        # scan above, wherever it sits: behind a held root it would otherwise
        # stay counted, and its waiter parked, for as long as that root is held.
        pressure: list[int | None] = []
        held = False
        index = 0
        while index < len(queue):
            params = queue[index]
            if not params.get("_startup_release"):
                index += 1
                continue
            info = params.get("_start_info")
            fut = getattr(info, "_start_release", None) if info is not None else None
            if (
                info is None
                or fut is None
                or fut.done()
                or info.done
                or info.user_stopped
                or info.reaped
                or info._reap_started
            ):
                queue.pop(index)
                if fut is not None and not fut.done():
                    fut.set_result(False)
                if info is not None:
                    self._manager._forget_pending_start(info.id)
                continue
            if self.entry_is_child(params):
                break
            if not pressure:
                pressure.append(self._manager._memory_pressure_hold())
            verdict = (
                "release"
                if pressure[0] is None
                else self._manager._memory_pressure_holds(
                    info.id,
                    pressure[0],
                    parent_session_key=info.parent_session_key,
                    batch_id=info.batch_id,
                    relabel=True,
                )
            )
            if verdict == "expired":
                # Ended, never started: the waiter answers it as such.
                queue.pop(index)
                fut.set_result("never_started")
                self._manager._forget_pending_start(info.id)
                self._manager._emit_queue_depth(info.parent_session_key, info.batch_id)
                continue
            if verdict != "held":
                break
            held = True
            index += 1
        else:
            return "held" if held else ""
        queue.pop(index)
        self._manager._forget_pending_start(info.id)
        # This start begins NOW. Stamp the stagger clock as every direct
        # dispatch does at ``create_task``: ``_run_inner`` writes
        # ``_exec_started`` on its first step, one loop iteration from here,
        # and the stamp keeps the pump from admitting into that gap -- the
        # same cover the direct paths rely on. One release per pass; the
        # re-arm at the stagger boundary takes the next.
        self._manager._last_spawn_ts = time.monotonic()
        logger.info(
            "Releasing approved spawn %s into startup (%d left queued, in_startup=%d/%d)",
            info.id,
            len(queue),
            self._manager._startup_population(),
            self._manager._startup_cap(),
        )
        fut.set_result(True)
        self._manager._emit_queue_depth(info.parent_session_key, info.batch_id)
        if queue:
            try:
                asyncio.get_event_loop().call_later(
                    self._manager._spawn_stagger_secs, self._manager._drain_queue
                )
            except RuntimeError:
                pass
        return "released"

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
