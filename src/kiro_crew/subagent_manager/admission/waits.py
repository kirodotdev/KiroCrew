"""Waits: yield the lane slot, keep the residency, resume through admission; child registration and deadlines."""

from __future__ import annotations

import asyncio as _asyncio
import logging as _logging
import time as _time
from typing import TYPE_CHECKING, Any

from .._component import ManagerComponent

_glue_logger = _logging.getLogger("kiro_crew.subagent_manager.admission")

if TYPE_CHECKING:
    from kiro_crew import taskq as _taskq

    from ...subagent import SubagentInfo, asyncio, time


class _WaitsMixin(ManagerComponent):
    __slots__ = ()

    if TYPE_CHECKING:
        # Sibling-mixin methods this module reaches through ``self``; typing only.
        def _post_store_write(
            self, store: "_taskq.TaskStore", what: str, fn: Any, *args: Any, **kw: Any
        ) -> "_asyncio.Task[Any] | None": ...

        def taskq_store(self) -> "_taskq.TaskStore | None": ...

    # ── waits: yield the lane slot, keep the residency, resume through admission ──
    #
    # Addendum §1-3 / RFC §14. A live run that cannot progress (children still
    # running, dependency cooling down, a command wanting real input, an
    # approval pending) yields its LANE slot here so the pump can start queued
    # work, while its runtime stays resident and charged. Waking re-enters the
    # same pump as a ``_resume_id`` entry, so a wake never bypasses capacity.

    @staticmethod
    def taskq_parent_id_for(parent_session_key: str | None) -> str | None:
        """The task id of the subagent whose session spawned this one, if any."""
        key = str(parent_session_key or "")
        prefix = "subagent:"
        if not key.startswith(prefix):
            return None
        return key[len(prefix) :] or None

    def taskq_ledger(self) -> "_taskq.WaitLedger | None":
        store = self.taskq_store()
        if store is None:
            return None
        from kiro_crew import taskq as _taskq

        return _taskq.WaitLedger(store)

    def yield_slot(
        self, info: SubagentInfo, record: "_taskq.WaitRecord", *, persist: bool = True
    ) -> bool:
        """Release *info*'s lane slot for a wait; the residency charge stays.

        Idempotent per run: a run that already yielded answers False. The wait
        record is written on the row (``running -> record.state``) under the
        run's generation; the runtime (session handle, process, FDs) is NOT
        touched -- ``residency_charged`` is the record's statement of that.
        ``persist=False`` skips that row write for a caller whose wait the
        ``DependencyCoordinator`` has already written (one wait, one write);
        the in-memory bookkeeping and the ``subagent_waiting`` event are the
        same either way.
        """
        if info.done or info._slot_released or info._resume_pending:
            return False
        released = self._manager._release_slot(info)
        if not released:
            return False
        self._manager._running_count -= 1
        record.slot_released = True
        info._wait_record = record.to_dict()
        store = self.taskq_store() if persist else None
        if store is not None:
            # POSTED, not inline, and that is an ORDERING requirement rather
            # than an off-loop convenience: the transition table accepts a wait
            # only FROM ``running``, the run's own ``running`` mark is itself
            # posted (``taskq_mark``), and a posted write is not ordered against
            # an inline one. Written inline here, a wait carried by the run's
            # FIRST stream frame reaches the row while it is still ``starting``,
            # is refused, and the late ``running`` write then leaves the row
            # ``running`` with no durable wait reason at all. On the store's one
            # writer thread the two land in submission order instead.
            self._post_store_write(
                store,
                f"wait write {info.id}",
                store.enter_wait,
                info.id,
                record.to_dict(),
                generation=info._taskq_generation or None,
            )
        try:
            _asyncio.get_event_loop().create_task(
                self._manager._fire_event(
                    "subagent_waiting",
                    info,
                    {
                        "state": record.state,
                        "reason": record.reason,
                        "resume": record.resume_condition.kind,
                        "residency_charged": record.residency_charged,
                    },
                )
            )
        except RuntimeError:
            pass
        _glue_logger.info(
            "Subagent %s: %s (%s) -- lane slot yielded, residency kept",
            info.id,
            record.state,
            record.reason,
        )
        self._manager._drain_queue()
        return True

    def request_resume(self, info: SubagentInfo, *, reason: str = "") -> bool:
        """Queue a resume entry for a yielded run; the pump grants it by capacity.

        Resume entries go to the FRONT of the window: the run is already
        resident, so granting it first shortens the interval in which a woken
        run continues without a slot. It still waits for a free slot like any
        other start, but not for the spawn stagger -- a resume starts no
        process, so there is no burst to smooth.
        """
        if info.done or not info._slot_released or info._resume_pending:
            return False
        info._resume_pending = True
        self._manager._queue.insert(
            0,
            {
                "_resume_id": info.id,
                "_preassigned_id": info.id,
                "parent_session_key": info.parent_session_key,
                "batch_id": info.batch_id,
                "reason": reason,
            },
        )
        self._manager._drain_queue()
        return True

    def resume_reserve(self, params: dict[str, Any]) -> bool:
        """First half of a resume grant: reserve the lane slot for *params*.

        Same shape as the dispatch path's ``ClaimPoint``: the CAPACITY is taken
        on the loop, where the pump's own ``any_slot`` re-check reads it, and
        the durable wake plus the run-state publish follow in
        :meth:`resume_grant_async`. Every outcome other than a granted resume
        gives the reservation back (:meth:`_resume_release`), so the cap is
        never left spent by a run that did not resume.

        A reservation is NOT a grant: ``_slot_released``, ``_wait_record``,
        ``_taskq_generation`` and ``_resume_event`` -- everything the waiting
        coroutine reads -- stay untouched until the store's wake has landed.

        No slot is reserved while gateway admission is closed, the same gate
        every spawn passes: the updater reads the in-flight census once and then
        replaces the process, so a slot taken behind that read is work the
        restart would interrupt mid-turn. The run stays parked with its wait
        intact -- :meth:`_resume_release`'s outcome, without its give-back,
        since nothing was taken -- and the next wake requests the resume again.
        """
        info = self._manager._agents.get(str(params.get("_resume_id") or ""))
        if info is None or info.done or info.reaped or info.user_stopped:
            if info is not None:
                info._resume_pending = False
            return False
        sessions = getattr(self._manager, "_sessions", None)
        if getattr(sessions, "admission_closed", False) is True:
            info._resume_pending = False
            _glue_logger.info(
                "Subagent %s: resume held back (gateway admission is closed)", info.id
            )
            return False
        # Not a process start: the spawn stagger (``_last_spawn_ts``) is left
        # alone so a resume never delays the next real start.
        self._manager._running_count += 1
        return True

    def _resume_unreserve(self, agent_id: str, why: str) -> bool:
        """Give back the capacity :meth:`resume_reserve` took."""
        self._manager._running_count = max(0, int(self._manager._running_count) - 1)
        _glue_logger.warning("Subagent %s: resume not granted (%s)", agent_id, why)
        return False

    def _resume_release(self, info: SubagentInfo, why: str) -> bool:
        """Undo :meth:`resume_reserve` for a resume that will not be granted.

        The run stays parked with its slot released and its wait record intact,
        so ``_await_lane_resume``'s bounded ``wait_for`` is what surfaces the
        refusal -- the existing path for a wait that never woke -- and a later
        ``request_resume`` can try again.
        """
        info._resume_pending = False
        return self._resume_unreserve(info.id, why)

    def resume_grant(self, params: dict[str, Any]) -> bool:
        """The pump popped a resume entry: hand the slot back to the run.

        Reserve, wake the row, publish -- in that order, INLINE, for the pump
        running without a loop (sync callers, tests). The coroutine pump splits
        the same three steps across :meth:`resume_reserve` and
        :meth:`resume_grant_async` so the SQLite call runs on the writer thread.
        """
        if not self.resume_reserve(params):
            return False
        info = self._manager._agents[str(params.get("_resume_id") or "")]
        store = self.taskq_store()
        if store is None:
            return self._resume_publish(info, None)
        return self._resume_publish(info, self._resume_wake(store, info, params))

    async def resume_grant_async(self, params: dict[str, Any]) -> bool:
        """Second half of a resume grant, for the coroutine pump: the durable
        wake on the writer thread, then the publish back on the loop.

        The caller must already hold the reservation :meth:`resume_reserve`
        took. Answers False when the wake did not land -- the reservation is
        released and the run stays parked.
        """
        agent_id = str(params.get("_resume_id") or "")
        info = self._manager._agents.get(agent_id)
        if info is None:
            return self._resume_unreserve(agent_id, "the run left the manager")
        store = self.taskq_store()
        if store is None:
            return self._resume_publish(info, None)
        return self._resume_publish(info, await store.run(self._resume_wake, store, info, params))

    @staticmethod
    def _resume_wake(
        store: "_taskq.TaskStore", info: SubagentInfo, params: dict[str, Any]
    ) -> tuple[int | None, str | None, bool]:
        """Database phase of a resume grant.

        ``(new_generation, row_state, store_reached)``. Safe on any thread -- it
        touches the store and the request parameters and nothing the loop owns.
        A ``new_generation`` is a landed wake; otherwise *row_state* is what the
        row actually holds, or ``None`` when there is no row at all.
        """
        from kiro_crew import taskq as _taskq

        try:
            # The slot is reserved and the runtime is resident: this is the ONE
            # wake that may write ``running`` directly.
            new_gen = store.wake_wait(
                info.id,
                reason=str(params.get("reason") or "resumed through admission"),
                generation=info._taskq_generation or None,
                to=_taskq.RUNNING,
            )
            if new_gen is not None:
                return (new_gen, _taskq.RUNNING, True)
            rec = store.get(info.id)
        except _taskq.TaskStoreUnavailable:
            return (None, None, False)
        return (None, rec.state if rec is not None else None, True)

    def _resume_publish(
        self, info: SubagentInfo, wake: "tuple[int | None, str | None, bool] | None"
    ) -> bool:
        """Loop phase of a resume grant: publish the run state, or refuse.

        *wake* is :meth:`_resume_wake`'s result, or ``None`` for the legacy
        in-memory queue (no store, so no row to wake and nothing to verify).
        The publish is the whole point of the split: a grant reported without a
        landed wake leaves the row ``waiting_*`` with its deadline, which the
        ledger's expiry sweep later fails -- cancelling a live, healthy run.
        """
        from kiro_crew import taskq as _taskq

        new_gen: int | None = None
        if wake is not None:
            new_gen, row_state, reached = wake
            if not reached:
                return self._resume_release(info, "the task store could not be reached")
            # Two refusals the wake can report that are NOT failures. No row at
            # all is a legacy in-memory entry: there is no durable state to
            # protect, and refusing would strand a resident run with no way back
            # to a slot. A ``running`` row means the wait write was refused or is
            # still on the writer thread behind this wake, and ``running`` is the
            # state a granted resume wants anyway -- the generation the run
            # carries is still the current one. Anything else is the row moving
            # out from under this run.
            if new_gen is None and row_state not in (None, _taskq.RUNNING):
                return self._resume_release(info, f"the row is {row_state}, not waiting for a slot")
        info._slot_released = False
        info._resume_pending = False
        info._wait_record = None
        if new_gen is not None:
            info._taskq_generation = new_gen
        event = getattr(info, "_resume_event", None)
        if event is not None:
            event.set()
            # One event per wait: a later yield arms a fresh one, so a holder
            # that arrives after this grant cannot read a stale set().
            info._resume_event = None
        try:
            _asyncio.get_event_loop().create_task(
                self._manager._fire_event(
                    "subagent_resumed", info, {"generation": info._taskq_generation}
                )
            )
        except RuntimeError:
            pass
        _glue_logger.info("Subagent %s: resumed (slot re-admitted)", info.id)
        return True

    def resume_granted(self, agent_id: str) -> bool:
        """Whether a yielded run holds its slot again (for a caller holding a tool result)."""
        info = self._manager._agents.get(agent_id)
        return info is not None and not info._slot_released and not info._resume_pending

    def taskq_child_registered(self, child: SubagentInfo) -> None:
        """A child started or queued under a live subagent parent.

        The parent enters ``waiting_children`` only when the EXECUTION LAYER
        shows it blocked on its children: its in-flight tool is the blocking
        ``spawn_sub_agents`` (trusted ``_meta.kiro`` tool name, never model
        text). A parent that used the non-blocking ``spawn_run`` keeps
        running and keeps its slot -- it has work of its own.
        """
        parent_id = self.taskq_parent_id_for(child.parent_session_key)
        if not parent_id:
            return
        parent = self._manager._agents.get(parent_id)
        if parent is None or parent.done:
            return
        inflight = getattr(parent, "_inflight_tool", None)
        tool_name = str(getattr(inflight, "tool_name", "") or "")
        if not tool_name.endswith("spawn_sub_agents"):
            return
        outstanding = self.taskq_outstanding_children(parent_id)
        from kiro_crew import taskq as _taskq

        if child.id not in outstanding:
            outstanding.append(child.id)
        current = _taskq.WaitRecord.from_dict(parent._wait_record)
        if parent._slot_released:
            # Already waiting: a later child of the same blocking call joins
            # the awaited set, so the wake stays "on the LAST child".
            if current is None or current.state != _taskq.WAITING_CHILDREN:
                return
            merged = list(current.resume_condition.ids)
            merged.extend(i for i in outstanding if i not in merged)
            current.resume_condition.ids = merged
            parent._wait_record = current.to_dict()
            store = self.taskq_store()
            if store is not None:
                try:
                    store.update_wait(
                        parent_id, current.to_dict(), generation=parent._taskq_generation or None
                    )
                except _taskq.TaskStoreUnavailable:
                    _glue_logger.debug("taskq: wait update for %s failed", parent_id, exc_info=True)
            return
        record = _taskq.WaitRecord.children(
            outstanding,
            since=self._store_now(),
            tool_call_id=str(getattr(inflight, "title", "") or ""),
            deadline_at=self.taskq_deadline_of(parent_id),
        )
        self.yield_slot(parent, record)

    async def taskq_child_registered_async(self, child: SubagentInfo) -> None:
        """:meth:`taskq_child_registered` for event-loop callers: the store
        reads (the ledger's outstanding children, the parent's deadline) run
        on the writer thread and the wait writes (``update_wait`` /
        ``enter_wait``) are posted there; the parent's slot yield and the
        in-memory bookkeeping stay on the loop."""
        from kiro_crew import taskq as _taskq

        parent_id = self.taskq_parent_id_for(child.parent_session_key)
        if not parent_id:
            return
        parent = self._manager._agents.get(parent_id)
        if parent is None or parent.done:
            return
        inflight = getattr(parent, "_inflight_tool", None)
        tool_name = str(getattr(inflight, "tool_name", "") or "")
        if not tool_name.endswith("spawn_sub_agents"):
            return
        store = self.taskq_store()
        ledger_ids: list[str] = []
        deadline: float | None = None
        if store is not None:
            try:
                ledger_ids, deadline = await store.run(
                    self._child_registration_reads, store, parent_id
                )
            except _taskq.TaskStoreUnavailable:
                pass
        outstanding = self._merge_outstanding_children(parent_id, ledger_ids)
        if child.id not in outstanding:
            outstanding.append(child.id)
        current = _taskq.WaitRecord.from_dict(parent._wait_record)
        if parent._slot_released:
            if current is None or current.state != _taskq.WAITING_CHILDREN:
                return
            merged = list(current.resume_condition.ids)
            merged.extend(i for i in outstanding if i not in merged)
            current.resume_condition.ids = merged
            parent._wait_record = current.to_dict()
            if store is not None:
                self._post_store_write(
                    store,
                    f"wait update {parent_id}",
                    store.update_wait,
                    parent_id,
                    current.to_dict(),
                    generation=parent._taskq_generation or None,
                )
            return
        record = _taskq.WaitRecord.children(
            outstanding,
            since=self._store_now(),
            tool_call_id=str(getattr(inflight, "title", "") or ""),
            deadline_at=deadline,
        )
        # The yield itself is loop bookkeeping; its row write is posted -- by
        # ``yield_slot``, which posts every wait write now.
        self.yield_slot(parent, record)

    @staticmethod
    def _child_registration_reads(
        store: "_taskq.TaskStore", parent_id: str
    ) -> tuple[list[str], float | None]:
        """Store half of the W3 branch: outstanding children + the parent's deadline."""
        from kiro_crew import taskq as _taskq

        ids = list(_taskq.WaitLedger(store).outstanding_children(parent_id))
        rec = store.get(parent_id)
        return ids, (rec.deadline_at if rec is not None else None)

    def _merge_outstanding_children(self, parent_id: str, ids: list[str]) -> list[str]:
        out = list(ids)
        key = f"subagent:{parent_id}"
        for info in self._manager._agents.values():
            if info.parent_session_key == key and not info.done and info.id not in out:
                out.append(info.id)
        return out

    def taskq_outstanding_children(self, parent_id: str) -> list[str]:
        """Non-terminal children of *parent_id*: store rows plus live in-memory runs."""
        ids: list[str] = []
        ledger = self.taskq_ledger()
        if ledger is not None:
            from kiro_crew import taskq as _taskq

            try:
                ids.extend(ledger.outstanding_children(parent_id))
            except _taskq.TaskStoreUnavailable:
                pass
        key = f"subagent:{parent_id}"
        for info in self._manager._agents.values():
            if info.parent_session_key == key and not info.done and info.id not in ids:
                ids.append(info.id)
        return ids

    def taskq_deadline_of(self, agent_id: str) -> float | None:
        store = self.taskq_store()
        if store is None:
            return None
        from kiro_crew import taskq as _taskq

        try:
            rec = store.get(agent_id)
        except _taskq.TaskStoreUnavailable:
            return None
        return rec.deadline_at if rec is not None else None

    def _store_now(self) -> float:
        store = self.taskq_store()
        return store.now() if store is not None else _time.time()

    def _child_terminal_target(self, child: SubagentInfo) -> "tuple[Any, str, Any, bool] | None":
        """``(store, parent_id, parent, live_parent)`` for a child's propagation.

        Pure loop-state resolution, shared by the sync and async entry points so
        the two cannot disagree about which parent is live.
        """
        store = self.taskq_store()
        parent_id = self.taskq_parent_id_for(child.parent_session_key)
        if store is None or not parent_id:
            return None
        parent = self._manager._agents.get(parent_id)
        # A LIVE parent (resident runtime, lane slot yielded) is re-admitted
        # through the pump: its row stays ``waiting_children`` until
        # ``resume_grant`` writes ``running`` with the slot actually granted.
        # Without a live parent the ledger wakes the row to a claimable
        # ``retry_wait`` and the dispatcher re-dispatches it.
        live_parent = parent is not None and not parent.done and parent._slot_released
        return (store, parent_id, parent, live_parent)

    @staticmethod
    def _child_terminal_reads(
        store: "_taskq.TaskStore",
        child_id: str,
        state: str,
        *,
        parent_id: str,
        live_parent: bool,
    ) -> "tuple[Any, int | None]":
        """Database phase of the propagation: the ledger's decision, and the
        parent's new generation when the ledger woke its row itself.

        Safe on any thread. Both reads belong to ONE phase because the
        generation is only meaningful for the outcome that produced it.
        """
        from kiro_crew import taskq as _taskq

        ledger = _taskq.WaitLedger(store)
        outcome = ledger.on_child_terminal(child_id, state, defer_wake=live_parent)
        generation: int | None = None
        if outcome.wake_parent and not live_parent:
            rec = store.get(parent_id)
            generation = rec.generation if rec is not None else None
        return (outcome, generation)

    def taskq_child_terminal(self, child: SubagentInfo, state: str) -> None:
        """Propagate a child's terminal state to its parent (after the store write).

        ``continue`` (default): the parent wakes when its LAST awaited child
        ends. ``fail_parent``: the parent fails now and its other children are
        cancelled, children first. Completed siblings are untouched either way.

        The synchronous entry point, for a caller already off the loop;
        :meth:`taskq_child_terminal_async` is the one the posted terminal write
        uses, because offloading only the outermost ``finish`` and then reaching
        the ledger from its callback puts the propagation's OWN writes back on
        the loop.
        """
        target = self._child_terminal_target(child)
        if target is None:
            return
        from kiro_crew import taskq as _taskq

        store, parent_id, parent, live_parent = target
        try:
            outcome, generation = self._child_terminal_reads(
                store, child.id, state, parent_id=parent_id, live_parent=live_parent
            )
        except _taskq.TaskStoreUnavailable:
            _glue_logger.debug("taskq: child propagation for %s failed", child.id, exc_info=True)
            return
        self._child_terminal_apply(child, state, parent, parent_id, outcome, generation)

    async def taskq_child_terminal_async(self, child: SubagentInfo, state: str) -> None:
        """:meth:`taskq_child_terminal` for event-loop callers: the ledger's
        propagation writes run on the writer thread, the cancels and the resume
        request stay on the loop."""
        target = self._child_terminal_target(child)
        if target is None:
            return
        from kiro_crew import taskq as _taskq

        store, parent_id, parent, live_parent = target
        try:
            outcome, generation = await store.run(
                self._child_terminal_reads,
                store,
                child.id,
                state,
                parent_id=parent_id,
                live_parent=live_parent,
            )
        except _taskq.TaskStoreUnavailable:
            _glue_logger.debug("taskq: child propagation for %s failed", child.id, exc_info=True)
            return
        self._child_terminal_apply(child, state, parent, parent_id, outcome, generation)

    def _child_terminal_apply(
        self,
        child: SubagentInfo,
        state: str,
        parent: Any,
        parent_id: str,
        outcome: Any,
        generation: int | None,
    ) -> None:
        """Loop phase of the propagation: cancels and the parent's re-admission."""
        from kiro_crew.taskq import waits as _waits

        if outcome.fail_parent:
            for sibling in outcome.cancel_siblings:
                self._cancel_live_or_row(sibling, reason=_waits.WAIT_REASON_CHILD_FAILED)
            if parent is not None and not parent.done:
                parent.error = parent.error or (
                    f"child {child.id} {state} (on_child_failure=fail_parent)"
                )
                self._schedule_cancel(parent_id)
            return
        if outcome.wake_parent and parent is not None:
            # A live parent's row is still ``waiting_children``; the pump's
            # grant is what writes ``running`` (``resume_grant`` -> ``wake_wait``).
            # Otherwise the ledger already moved the row to a claimable
            # ``retry_wait`` under a new generation, read in the same database
            # phase: adopt it so a late write is not fenced as stale.
            if generation is not None:
                parent._taskq_generation = generation
            self.request_resume(parent, reason=f"last awaited child {child.id} {state}")

    def _cancel_live_or_row(self, agent_id: str, *, reason: str) -> None:
        info = self._manager._agents.get(agent_id)
        if info is not None and not info.done:
            self._schedule_cancel(agent_id)
            return
        store = self.taskq_store()
        if store is None:
            return
        from kiro_crew import taskq as _taskq

        # Nothing reads the result, so the cancel-tree write is POSTED to the
        # writer thread on the loop and runs inline off it.
        self._post_store_write(
            store,
            f"cancel tree {agent_id}",
            _taskq.WaitLedger(store).cancel_tree,
            agent_id,
            reason=reason,
        )

    def _schedule_cancel(self, agent_id: str) -> None:
        try:
            _asyncio.get_event_loop().create_task(self._manager.cancel(agent_id))
        except RuntimeError:
            pass

    def _live_agent_ids(self) -> frozenset[str]:
        """The ids this process is still running, read on the loop that owns them."""
        return frozenset(i.id for i in self._manager._agents.values() if not i.done)

    @staticmethod
    def _cancel_children_db(
        store: "_taskq.TaskStore", agent_id: str, reason: str, live_ids: frozenset[str]
    ) -> tuple[list[str], list[str]]:
        """Database phase of a cancel-tree: ``(live_children, cancelled_rows)``.

        Safe on any thread. A child this process is running is handed back for
        the loop to cancel through the manager; every other one is a row, and
        its whole subtree is cancelled here.
        """
        from kiro_crew import taskq as _taskq

        ledger = _taskq.WaitLedger(store)
        live: list[str] = []
        cancelled: list[str] = []
        for child in ledger.children_of(agent_id):
            if child.terminal:
                continue
            if child.id in live_ids:
                live.append(child.id)
            else:
                cancelled.extend(ledger.cancel_tree(child.id, reason=reason))
        return (live, cancelled)

    def taskq_cancel_children_of(self, agent_id: str, *, reason: str) -> list[str]:
        """Cancel-tree for a parent being cancelled: store rows first, live runs scheduled."""
        store = self.taskq_store()
        if store is None:
            return []
        from kiro_crew import taskq as _taskq

        live_ids = self._live_agent_ids()
        try:
            live, cancelled = self._cancel_children_db(store, agent_id, reason, live_ids)
        except _taskq.TaskStoreUnavailable:
            return []
        return self._cancel_children_apply(live, cancelled)

    async def taskq_cancel_children_of_async(self, agent_id: str, *, reason: str) -> list[str]:
        """:meth:`taskq_cancel_children_of` for event-loop callers: the ledger
        sweep on the writer thread, the live-run cancels on the loop."""
        store = self.taskq_store()
        if store is None:
            return []
        from kiro_crew import taskq as _taskq

        live_ids = self._live_agent_ids()
        try:
            live, cancelled = await store.run(
                self._cancel_children_db, store, agent_id, reason, live_ids
            )
        except _taskq.TaskStoreUnavailable:
            return []
        return self._cancel_children_apply(live, cancelled)

    def _cancel_children_apply(self, live: list[str], cancelled: list[str]) -> list[str]:
        for child_id in live:
            self._schedule_cancel(child_id)
        return [*live, *cancelled]

    def taskq_expire_waits(self) -> list[str]:
        """Fail waits past their deadline and cancel the live runs they belonged to.

        Rate-limited to once per second: the pump calls this on every refill,
        and a deadline is a wall-clock fact that does not need sub-second checks.
        """
        expired = self.taskq_expire_waits_store()
        self.taskq_expire_waits_apply(expired)
        return expired

    def taskq_expire_waits_store(self) -> list[str]:
        """Store half of :meth:`taskq_expire_waits` (safe on the writer thread):
        the rate limit and the ledger sweep. Returns the expired ids."""
        ledger = self.taskq_ledger()
        if ledger is None:
            return []
        from kiro_crew import taskq as _taskq

        now = _time.monotonic()
        last = float(getattr(self._manager, "_taskq_last_wait_expiry", 0.0) or 0.0)
        if now - last < 1.0:
            return []
        setattr(self._manager, "_taskq_last_wait_expiry", now)
        try:
            return ledger.expire()
        except _taskq.TaskStoreUnavailable:
            return []

    def taskq_expire_waits_apply(self, expired: list[str]) -> None:
        """Loop half of :meth:`taskq_expire_waits`: cancel the live runs the
        expired waits belonged to (schedules tasks, so it runs on the loop)."""
        for agent_id in expired:
            info = self._manager._agents.get(agent_id)
            if info is not None and not info.done:
                info.error = info.error or "wait deadline passed"
                self._schedule_cancel(agent_id)
