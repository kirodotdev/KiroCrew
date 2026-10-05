"""Memory waits: which starts wait for memory, what wakes them, and each lane's share.

subagent.md (*Memory waits: event wake and the per-lane share*) is the one
statement of these rules; the code points there.

A start the memory floor defers is recorded here from its first deferral until
it is admitted, started or ended (``_memory_waits``). The record is what makes
the wait event-driven: a child reaching its terminal, a wait ending and a
sampler tick each run one FIT PASS
(:meth:`_MemoryWakeMixin.wake_memory_waits`), which takes one host reading and
brings forward only the waits that reading now admits, so a wake that finds the
host still short writes nothing at all. The admit wait stays as the
restart-safe backstop. The sampler runs only while a wait is recorded. The
record is also what the per-lane memory share reads: while another lane waits
for memory, a lane already at or above its share of running dedicated children
is not given the next memory admission
(:meth:`_MemoryWakeMixin.lane_share_holds`).
"""

from __future__ import annotations

import asyncio as _asyncio
import logging as _logging
import time as _time
from typing import TYPE_CHECKING, Any

from .._component import ManagerComponent
from .types import MEMORY_SAMPLER_SECS, MEMORY_WAIT_UNTIL_KEY, MemoryWait

_glue_logger = _logging.getLogger("kiro_crew.subagent_manager.admission")

if TYPE_CHECKING:
    from kiro_crew import taskq as _taskq
    from kiro_crew.taskq import lanes as _lanes


class _MemoryWakeMixin(ManagerComponent):
    __slots__ = ()

    if TYPE_CHECKING:
        # Sibling-mixin methods this module reaches through ``self``; typing only.
        def lane_for_session(self, session_key: str | None) -> str: ...

        def lane_scheduler(self) -> "_lanes.LaneScheduler": ...

        def taskq_store(self) -> "_taskq.TaskStore | None": ...

        def _post_store_write(
            self, store: "_taskq.TaskStore", what: str, fn: Any, *args: Any, **kw: Any
        ) -> "_asyncio.Task[Any] | None": ...

    # ── the record: a wait begins, is re-checked, and ends ──

    def _memory_state(self) -> "dict[str, MemoryWait]":
        """The manager's wait record, its wake and sampler state created on first
        use for a facade built without ``__init__``."""
        mgr = self._manager
        waits = getattr(mgr, "_memory_waits", None)
        if waits is None:
            waits = {}
            mgr._memory_waits = waits
            mgr._memory_fit_task = None
            mgr._memory_wake_again = False
            mgr._memory_sampler_handle = None
            mgr._memory_sample_task = None
        return waits

    def memory_wait_began(
        self,
        agent_id: str,
        *,
        parent_session_key: str,
        start_gb: float | None,
        price_gb: float | None,
        nested: bool,
        durable: bool,
    ) -> None:
        """Record that *agent_id* waits for memory, or refresh its record on a re-check.

        *start_gb* is what the floor charges this start for itself (0 for a
        nested start that shares its parent's runtime), *price_gb* what its row
        owes once admitted; the fit pass rebuilds the bar from them. The first
        begin arms the sampler.
        """
        waits = self._memory_state()
        current = waits.get(agent_id)
        if current is not None:
            current.start_gb = start_gb
            current.price_gb = price_gb
            current.durable = durable
            return
        waits[agent_id] = MemoryWait(
            lane=self.lane_for_session(parent_session_key),
            parent_session_key=parent_session_key,
            since=_time.monotonic(),
            start_gb=start_gb,
            price_gb=price_gb,
            nested=nested,
            durable=durable,
        )
        self._arm_memory_sampler()

    def memory_wait_ended(self, agent_id: str) -> MemoryWait | None:
        """Forget *agent_id*'s memory wait (admitted, started or ended); the record, or None.

        With no wait left the sampler stops at once. A root start waiting in
        another lane gets a fit pass: this wait leaving may be what ends the
        starvation the per-lane share held it for. Nothing else does: a wait
        ending frees no memory (a start that leaves it either takes memory or
        never held any), so only the share can have moved.
        """
        waits = getattr(self._manager, "_memory_waits", None)
        ended = waits.pop(agent_id, None) if waits else None
        if ended is None:
            return None
        if not waits:
            self._disarm_memory_sampler()
        elif any(w.lane != ended.lane and not w.nested for w in waits.values()):
            self.wake_memory_waits("wait_ended")
        return ended

    # ── the per-lane memory share ──

    def lane_share_holds(self, parent_session_key: str, agent_id: str) -> bool:
        """Whether the per-lane memory share holds back this start.

        True when another lane waits for memory below its share while this
        start's lane already runs at or above its own. A lane's share is its
        weight's part (``agent.lane_weights``, the round-robin's weights) of the
        dedicated children running across the lanes that contend for memory --
        those with a dedicated child running or a start waiting. The starved
        lane is by construction never held (its count is below its share), so
        the hold cannot deadlock: it only decides which lane the memory goes to
        next.
        """
        from kiro_crew.subagent import _holds_dedicated_runtime

        mgr = self._manager
        lane = self.lane_for_session(parent_session_key)
        waits = getattr(mgr, "_memory_waits", None) or {}
        waiting = {wait.lane for aid, wait in waits.items() if aid != agent_id} - {lane}
        if not waiting:
            return False
        running: dict[str, int] = {}
        for info in list(mgr._agents.values()):
            if _holds_dedicated_runtime(info):
                key = self.lane_for_session(info.parent_session_key)
                running[key] = running.get(key, 0) + 1
        mine = running.get(lane, 0)
        if mine == 0:
            return False
        scheduler = self.lane_scheduler()
        contending = set(running) | waiting | {lane}
        weights = {key: max(1, int(scheduler.weight_of(key))) for key in contending}
        total_weight = sum(weights.values())
        total_running = sum(running.values())

        def share(key: str) -> float:
            return total_running * weights[key] / total_weight

        if mine < share(lane):
            return False
        return any(running.get(key, 0) < share(key) for key in waiting)

    # ── the wake: child terminal, a wait ending, sampler tick ──

    def wake_memory_waits(self, reason: str) -> None:
        """Run a fit pass over the recorded memory waits (:meth:`_fit_pass`).

        Called on a child's terminal, a wait ending and each sampler tick. One
        pass runs at a time; a wake that arrives while one is in flight runs
        one more pass after it, so a burst of events costs at most two
        readings. A no-op with nothing waiting, and with no
        running loop (the admit wait is then the re-check).
        """
        mgr = self._manager
        # Read with defaults: a facade built without ``__init__`` (a test's
        # minimal manager) reaches this from every run's terminal.
        if not getattr(mgr, "_memory_waits", None) or getattr(mgr, "_shutting_down", False):
            return
        try:
            loop = _asyncio.get_running_loop()
        except RuntimeError:
            return
        task = mgr._memory_fit_task
        if task is not None and not task.done():
            mgr._memory_wake_again = True
            return
        mgr._memory_wake_again = False
        mgr._memory_fit_task = loop.create_task(self._fit_passes(reason))

    async def _fit_passes(self, reason: str) -> None:
        mgr = self._manager
        try:
            while True:
                await self._fit_pass(reason)
                if not mgr._memory_wake_again or getattr(mgr, "_shutting_down", False):
                    break
                mgr._memory_wake_again = False
        except Exception:
            _glue_logger.debug("memory fit pass (%s) failed", reason, exc_info=True)
        finally:
            if mgr._memory_fit_task is _asyncio.current_task():
                mgr._memory_fit_task = None

    def _memory_bar_base_gb(self, floor_gb: float, cost_gb: float) -> float:
        """The floor plus every start still owed but not yet in the reading: the
        gate's bar (``spawn_min_memory_gb`` plus ``_startup_memory_reserve_gb``)
        for a start that charges itself nothing."""
        from kiro_crew.subagent import _startup_memory_reserve_gb

        mgr = self._manager
        return floor_gb + _startup_memory_reserve_gb(
            list(mgr._agents.values()),
            running_count=mgr._running_count,
            cost_gb=cost_gb,
            next_start_gb=0.0,
            settled_gb=mgr._learned_settled_gb,
            claim_prices=[price for price, _ in mgr._claim_prices.values()],
        )

    async def _fit_pass(self, reason: str) -> None:
        """Bring forward the waits one host reading now admits; touch nothing else.

        The bar each wait must clear is rebuilt from the current state, as the
        gate builds it (the floor, every start still warming, and the start's
        own charge), so a row settling or a run ending lowers it without the
        host reading moving. The reading is the gate's own
        (``_host_memory_reading_off_loop``, single-flight and bounded). Oldest
        wait first, a wait is brought forward when the reading clears its bar
        plus what the waits brought forward before it in this pass will
        charge, and the per-lane share would not hold it. A wait that does not
        fit is left parked as it is: no store write, no event, no depth frame.
        A reading with a cause (unanswered, an unreadable cgroup) brings
        nothing forward, since the gate would defer on it too; one that is
        unmeasurable (-1) brings every wait forward, since the gate fails open
        on it. A floor turned off brings every wait forward.
        """
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.subagent import _host_memory_reading_off_loop, _spawn_memory_floor_and_cost

        mgr = self._manager
        if not mgr._memory_waits or getattr(mgr, "_shutting_down", False):
            return
        agent_cfg = None
        try:
            agent_cfg = KiroCrewConfig.load().agent
        except Exception:
            _glue_logger.debug("memory fit pass: config unreadable; defaults", exc_info=True)
        floor_gb, cost_gb = _spawn_memory_floor_and_cost(agent_cfg)
        if floor_gb <= 0:
            self._release_memory_waits(list(mgr._memory_waits), reason)
            return

        def own(wait: MemoryWait) -> float:
            return cost_gb if wait.start_gb is None else max(0.0, wait.start_gb)

        lowest = self._memory_bar_base_gb(floor_gb, cost_gb) + min(
            own(wait) for wait in mgr._memory_waits.values()
        )
        avail, cause = await _host_memory_reading_off_loop(lowest)
        if cause or not mgr._memory_waits or getattr(mgr, "_shutting_down", False):
            return
        # Rebuilt after the read: a start admitted while it ran is charged.
        base = self._memory_bar_base_gb(floor_gb, cost_gb)
        release: list[str] = []
        charged = 0.0
        for aid, wait in sorted(mgr._memory_waits.items(), key=lambda item: item[1].since):
            if not wait.nested and self.lane_share_holds(wait.parent_session_key, aid):
                continue
            if avail >= 0 and avail < base + own(wait) + charged:
                continue
            release.append(aid)
            charged += cost_gb if wait.price_gb is None else max(0.0, wait.price_gb)
        if release:
            self._release_memory_waits(release, reason)

    def _release_memory_waits(self, ids: list[str], reason: str) -> None:
        """Make the waits in *ids* eligible now and pump.

        An in-memory window entry has its not-before stamp cleared (it stays a
        floor wait, so the pick still leaves it to the gate) and its parked
        time cut at now (``_floor_waits``). A durable row is brought forward in
        the store (:meth:`TaskStore.expedite`, on the writer thread), and the
        pump runs once that write has landed, so the refill can take it.
        """
        mgr = self._manager
        waits = mgr._memory_waits
        wanted = {aid for aid in ids if aid in waits}
        if not wanted or getattr(mgr, "_shutting_down", False):
            return
        now = _time.monotonic()
        for params in mgr._queue:
            aid = str(params.get("_preassigned_id") or "")
            if aid in wanted and float(params.get(MEMORY_WAIT_UNTIL_KEY) or 0.0) > now:
                params[MEMORY_WAIT_UNTIL_KEY] = 0.0
        # The floor's parked clock is integer ``monotonic_ns`` (the gate adds
        # whole admit waits to it), not the window's float seconds.
        now_ns = _time.monotonic_ns()
        for aid in wanted:
            park = mgr._floor_waits.get(aid)
            if park is not None:
                mgr._floor_waits[aid] = (park[0], park[1], min(park[2], now_ns))
        durable = sorted(aid for aid in wanted if waits[aid].durable)
        _glue_logger.debug(
            "memory wake (%s): %d of %d waiting start(s) now fit",
            reason,
            len(wanted),
            len(waits),
        )
        store = self.taskq_store() if durable else None
        task = (
            self._post_store_write(store, "memory wake", store.expedite, durable)
            if store is not None
            else None
        )
        if task is None:
            mgr._drain_queue()
        else:
            task.add_done_callback(lambda _done: mgr._drain_queue())

    async def _prune_memory_waits(self) -> None:
        """Forget waits whose start has stopped waiting, by what holds it now.

        Every exit forgets its wait (``_forget_pending_start``), so this is the
        backstop for an exit that raced the deferral: a wait left behind would
        keep the sampler running and count a lane as waiting that is not. A
        start with no durable row waits while it is in the window or being
        dispatched; a durable one while its row is still waiting
        (``taskq.CLAIMABLE``), read on the writer thread. A registered run is a
        run, not a start.
        """
        from kiro_crew import taskq as _taskq

        mgr = self._manager
        waits = mgr._memory_waits
        windowed = {str(p.get("_preassigned_id") or "") for p in mgr._queue}
        windowed |= set(mgr._dispatch_window_ids) | set(mgr._undurable_in_dispatch)
        gone = {aid for aid in waits if aid in mgr._agents}
        gone |= {aid for aid, w in waits.items() if not w.durable and aid not in windowed}
        durable = [aid for aid, w in waits.items() if w.durable and aid not in gone]
        store = self.taskq_store()
        if durable and store is not None:

            def _states(live: "_taskq.TaskStore") -> dict[str, str | None]:
                return {aid: live.state_of(aid) for aid in durable}

            try:
                states = await store.run(_states, store)
            except _taskq.TaskStoreUnavailable:
                states = {}
            gone |= {aid for aid, state in states.items() if state not in _taskq.CLAIMABLE}
        for aid in sorted(gone):
            if aid in waits:
                self.memory_wait_ended(aid)

    def _arm_memory_sampler(self) -> None:
        mgr = self._manager
        if getattr(mgr, "_shutting_down", False) or not mgr._memory_waits:
            return
        try:
            loop = _asyncio.get_running_loop()
        except RuntimeError:
            return
        handle = mgr._memory_sampler_handle
        # Re-armed when none is pending, including one whose loop went away
        # before it fired (past due by more than a second).
        if handle is not None and not handle.cancelled() and handle.when() >= loop.time() - 1.0:
            return
        if mgr._memory_sample_task is not None and not mgr._memory_sample_task.done():
            return  # the tick in flight re-arms when it ends
        mgr._memory_sampler_handle = loop.call_later(MEMORY_SAMPLER_SECS, self._sampler_fired)

    def _disarm_memory_sampler(self) -> None:
        mgr = self._manager
        handle = mgr._memory_sampler_handle
        if handle is not None and not handle.cancelled():
            # Cancelled while still the manager's field: that identity is the
            # intentional-cancel marker for a timer.
            mgr._cancel_task_intentionally(handle, reason="memory sampler: nothing waits")
        mgr._memory_sampler_handle = None

    def _sampler_fired(self) -> None:
        mgr = self._manager
        mgr._memory_sampler_handle = None
        if getattr(mgr, "_shutting_down", False) or not mgr._memory_waits:
            return
        mgr._memory_sample_task = _asyncio.get_running_loop().create_task(self._sample_once())

    async def _sample_once(self) -> None:
        """One sampler tick: prune the record, then run a fit pass.

        The fit pass is what reads the host and rebuilds the bar
        (:meth:`_fit_pass`), so memory another program frees, and a warming
        row that has settled since (it owes no start price any more), are seen
        within a tick, and a host that stays short costs one reading per tick
        and no write.
        """
        mgr = self._manager
        try:
            await self._prune_memory_waits()
            self.wake_memory_waits("sampler")
        except Exception:
            _glue_logger.debug("memory sampler tick failed", exc_info=True)
        finally:
            mgr._memory_sample_task = None
            self._arm_memory_sampler()
