"""Durable task queue glue: store open/accept/defer/settle, the bounded window and its refill, off-loop store writes."""

from __future__ import annotations

import asyncio as _asyncio
import logging as _logging
import time as _time
from typing import TYPE_CHECKING, Any, Mapping

from .._component import ManagerComponent
from .types import DeferPoint, FairnessSettings, tombstone_terminal_state

_glue_logger = _logging.getLogger("kiro_crew.subagent_manager.admission")

if TYPE_CHECKING:
    from kiro_crew import taskq as _taskq
    from kiro_crew.taskq import lanes as _lanes

    from ...subagent import SubagentInfo, asyncio


class _TaskqBridgeMixin(ManagerComponent):
    __slots__ = ()

    if TYPE_CHECKING:
        # Sibling-mixin methods this module reaches through ``self``; typing only.
        @staticmethod
        def entry_is_child(params: Mapping[str, Any]) -> bool: ...

        def fairness_settings(self) -> FairnessSettings: ...

        def lane_of_entry(
            self, params: Mapping[str, Any], resolved: Mapping[str, str] | None = None
        ) -> str: ...

        def _resolve_lanes(self, session_keys: Any) -> dict[str, str]: ...

        def _window_lane_keys(self) -> list[str]: ...

        pump_off_loop: bool

        def taskq_cancel_children_of(self, agent_id: str, *, reason: str) -> list[str]: ...

        async def taskq_cancel_children_of_async(
            self, agent_id: str, *, reason: str
        ) -> list[str]: ...

        def taskq_child_terminal(self, child: SubagentInfo, state: str) -> None: ...

        async def taskq_child_terminal_async(self, child: SubagentInfo, state: str) -> None: ...

        def taskq_expire_waits(self) -> list[str]: ...

        @staticmethod
        def taskq_parent_id_for(parent_session_key: str | None) -> str | None: ...

    # ── durable task queue glue ──────────────────────────────────────────────
    #
    # Plain methods, deliberately not ``*_impl``: ``bind_component_globals``
    # rebinds every ``*_impl`` onto ``subagent``'s namespace, where ``_taskq``
    # is not a name. These keep this module's globals and are reached from the
    # rebound ``spawn_impl`` / ``_drain_queue_impl`` as attributes of ``self``.

    def taskq_store(self) -> "_taskq.TaskStore | None":
        """The manager's task store, or None when the durable queue is off."""
        return getattr(self._manager, "_taskq", None)

    def taskq_required_but_unavailable(self) -> str | None:
        """The typed reason spawns are refused: ``agent.task_queue_enabled`` is
        on and the store did not open. None when the store is open or the
        queue is deliberately off (legacy in-memory queue)."""
        if self.taskq_store() is not None:
            return None
        return getattr(self._manager, "_taskq_unavailable", None)

    def taskq_window(self) -> int:
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        return store.window if store is not None else _taskq.DEFAULT_DISPATCH_WINDOW

    def taskq_admit_wait_secs(self) -> float:
        return float(getattr(self._manager, "_taskq_admit_wait_secs", 30.0) or 30.0)

    def taskq_window_ids(self) -> list[str]:
        return [
            str(p.get("_preassigned_id") or "")
            for p in self._manager._queue
            if p.get("_preassigned_id")
        ]

    def taskq_excluded_ids(self) -> list[str]:
        """Rows the refill must never claim: those already in the window AND
        those with a LIVE run in this process. A live run's row can be
        claimable for a moment (a wake lands in ``retry_wait`` until the pump
        grants the slot back); claiming it here would start a second copy of a
        run that is still resident."""
        live = [aid for aid, info in self._manager._agents.items() if not info.done]
        # Rows whose accept path is still in flight (``spawn_async``: written,
        # not yet claimed or windowed) belong to that caller, not to the pump.
        admitting = list(getattr(self._manager, "_admitting_ids", ()) or ())
        return self.taskq_window_ids() + live + admitting

    def taskq_open(self, cfg: Any, *, home: Any) -> "_taskq.TaskStore | None":
        """Open the store for this manager per ``cfg.agent``.

        Import and reconcile run inside ``open_default_store``. Disabled queues
        return None; enabled queues record a typed refusal when opening fails,
        and arm its retry (``taskq_arm_reopen``). The manager's startup worker
        attaches a successful result on the loop.
        """
        agent = getattr(cfg, "agent", None)
        if agent is None or not bool(getattr(agent, "task_queue_enabled", True)):
            return None
        from kiro_crew import taskq as _taskq

        window = int(getattr(agent, "task_dispatch_window", _taskq.DEFAULT_DISPATCH_WINDOW) or 0)
        journal_mode = str(getattr(agent, "task_store_journal_mode", "auto") or "auto")
        try:
            store = _taskq.open_default_store(
                home,
                window=max(1, window),
                artifact_probe=self.taskq_artifact_probe,
                journal_mode=journal_mode,
            )
        except _taskq.TaskStoreUnavailable as exc:
            # The queue is ENABLED and its store is gone: refuse work rather
            # than accept it into an in-memory queue a restart forgets. The
            # error is kept so every spawn answers a typed refusal
            # (``task_store_unavailable``) naming the cause.
            reason = f"durable task queue unavailable: {exc}"
            setattr(self._manager, "_taskq_unavailable", reason)
            # Armed HERE, where the config is in hand and this call is off the
            # loop: the sweep that fires it cannot afford a config read.
            delay = self.taskq_arm_reopen(cfg)
            _glue_logger.error(
                "%s; spawns are refused until it opens, re-attempted in %.1fs", reason, delay
            )
            return None
        for line in store.warnings:
            _glue_logger.warning("taskq: %s", line)
        return store

    def taskq_arm_reopen(self, cfg: Any = None) -> float:
        """Schedule the next store-open attempt and return its delay in seconds.

        Called by whoever RECORDS a failed open, so the delay is measured from
        the failure rather than from the sweep that notices it. The schedule is
        the shared recovery one (``agent.recovery_backoff_*``: capped exponential
        backoff, equal jitter) -- the same clock a dependency scope retries on,
        never a schedule of this call's own. ``cfg`` is None when the config load
        is itself what failed, which takes the ladder's defaults.
        """
        from kiro_crew.recovery.policy import RecoveryPolicy

        mgr = self._manager
        attempts = int(getattr(mgr, "_taskq_reopen_attempts", 0)) + 1
        policy = RecoveryPolicy()
        if cfg is not None:
            try:
                policy = RecoveryPolicy.from_config(cfg)
            except Exception:  # noqa: BLE001 - recovery never depends on config parsing
                _glue_logger.debug("taskq: reopen backoff falls back to defaults", exc_info=True)
        delay = float(policy.backoff_secs(attempts))
        mgr._taskq_reopen_attempts = attempts
        mgr._taskq_reopen_at = _time.monotonic() + delay
        return delay

    def taskq_reopen_if_due(self) -> bool:
        """Re-attempt a failed store open once its backoff deadline has passed.

        The conditions ``taskq_open`` keeps as a refusal -- a lock, a busy or
        full disk, a read-only mount -- are all transient, so a one-shot open
        would turn a two-second lock into a gateway that refuses EVERY spawn
        until someone restarts it. While the refusal stands the manager holds no
        accepted work, so the attempt is the boot open repeated: schema, legacy
        import and reconcile re-run inside ``_initialize_taskq`` off the loop and
        the store attaches on it. A corrupt file is not retried into -- ``open``
        quarantines it and recreates the schema.

        Driven by the reaper sweep. True when an attempt was armed; False while
        one is in flight, before the deadline, once a store is attached, and
        when the queue is deliberately off.
        """
        mgr = self._manager
        if not self.taskq_required_but_unavailable():
            return False
        if getattr(mgr, "_shutting_down", False):
            return False
        pending = getattr(mgr, "_taskq_init_task", None)
        if pending is not None and not pending.done():
            return False
        if _time.monotonic() < float(getattr(mgr, "_taskq_reopen_at", 0.0) or 0.0):
            return False
        try:
            loop = _asyncio.get_running_loop()
        except RuntimeError:
            return False
        attempts = max(1, int(getattr(mgr, "_taskq_reopen_attempts", 0)))
        # One line per outage at warning, the rest at debug: a full disk lasts
        # hours and the first attempt already named the cause.
        _glue_logger.log(
            _logging.WARNING if attempts <= 1 else _logging.DEBUG,
            "taskq: re-attempting the store open after %d failed open(s)",
            attempts,
        )
        mgr._taskq_init_task = loop.create_task(mgr._initialize_taskq())
        self.track_store_task(mgr._taskq_init_task)
        return True

    @staticmethod
    def taskq_artifact_probe(rec: "_taskq.TaskRecord") -> str | None:
        """What a subagent run's tombstone proves about how it ended, if anything."""
        from kiro_crew import taskq as _taskq

        if rec.kind != _taskq.KIND_SUBAGENT:
            return None
        try:
            from kiro_crew.subagent_persistence import read_tombstone

            tombstone = read_tombstone(rec.id)
        except (ValueError, OSError):
            return None
        if not tombstone:
            return None
        return tombstone_terminal_state(str(tombstone.get("cause") or ""))

    def taskq_boot_dispatch(self) -> None:
        """Kick the pump once the loop runs, so rows that survived a restart start.

        Called from ``start_reaper``. Deferred rows and rows in backoff are not
        eligible yet; the drain schedules its own wake-up for them.
        """
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None:
            return
        try:
            pending = store.count_pending(_taskq.KIND_SUBAGENT)
        except _taskq.TaskStoreUnavailable:
            return
        if pending <= 0:
            return
        _glue_logger.info("taskq: %d persisted subagent task(s) pending at boot", pending)
        try:
            _asyncio.get_event_loop().call_later(0.0, self._manager._drain_queue)
        except RuntimeError:
            pass

    def taskq_accept(
        self,
        agent_id: str,
        params: dict[str, Any],
        *,
        parent_session_key: str,
        memory_store: str,
        app: str,
        model: str | None,
        allowed_tools: list[str] | None,
        approval_mode: str | None,
    ) -> str | None:
        """Persist the spawn as a ``queued`` row. Returns an error string on failure.

        The error means NOTHING was accepted: the caller turns it into a
        typed refusal and never hands the id out as accepted work. This is the
        SYNC path (``spawn()`` from a non-loop caller); an event-loop caller
        uses ``taskq_build_record`` + ``taskq_accept_record`` through
        ``TaskStore.run`` (``SubagentManager.spawn_async``).
        """
        unavailable = self.taskq_required_but_unavailable()
        if unavailable:
            return unavailable
        if self.taskq_store() is None:
            return None
        record = self.taskq_build_record(
            agent_id,
            params,
            parent_session_key=parent_session_key,
            memory_store=memory_store,
            app=app,
            model=model,
            allowed_tools=allowed_tools,
            approval_mode=approval_mode,
        )
        return self.taskq_accept_record(record)

    def taskq_build_record(
        self,
        agent_id: str,
        params: dict[str, Any],
        *,
        parent_session_key: str,
        memory_store: str,
        app: str,
        model: str | None,
        allowed_tools: list[str] | None,
        approval_mode: str | None,
    ) -> "_taskq.TaskRecord":
        """The row a spawn persists -- built WITHOUT touching the store, so an
        event-loop caller can hand it to ``taskq_accept_record`` off-loop."""
        from kiro_crew import taskq as _taskq

        # Nested tree: a spawn from a subagent's own session names that subagent
        # as its parent task, so the store can rebuild parent<->child links and
        # wake a waiting parent on its LAST child (taskq.waits). The parent's
        # root is resolved at accept time (a store read).
        parent_id = self.taskq_parent_id_for(parent_session_key)
        # Prevalidation is a fact about THIS request's moment, never about the
        # row: an app spawn whose agent was checked off-loop just now may be
        # started later -- from the window refill, or after a restart -- when
        # the app could be disabled and a same-named foreign agent could sit
        # under its filename. The durable row therefore never carries the flag,
        # so every start from the store runs the ownership and agent gates.
        durable_params = {k: v for k, v in params.items() if k != "_agent_prevalidated"}
        return _taskq.TaskRecord(
            id=agent_id,
            kind=_taskq.KIND_SUBAGENT,
            session_key=parent_session_key or "",
            parent_id=parent_id,
            root_id="",
            provider=model or None,
            params=durable_params,
            workspace=str(params.get("cwd") or "") or None,
            scope_ref={
                "memory_store": memory_store or "",
                "allowed_tools": list(allowed_tools) if allowed_tools else [],
                "approval_mode": approval_mode or "",
                "app": app or "",
            },
            side_effect_class=str(params.get("side_effect_class") or _taskq.SIDE_EFFECT_UNKNOWN),
        )

    def taskq_accept_record(self, record: "_taskq.TaskRecord") -> str | None:
        """Write *record* (resolving its parent's root first). Error string on
        failure, meaning nothing was accepted. Store calls only -- safe to run
        on the store's writer thread."""
        unavailable = self.taskq_required_but_unavailable()
        if unavailable:
            return unavailable
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None:
            return None
        parent_id = record.parent_id
        root_id = ""
        if parent_id:
            try:
                parent_rec = store.get(parent_id)
            except _taskq.TaskStoreUnavailable:
                parent_rec = None
            if parent_rec is None:
                parent_id = None
            else:
                root_id = parent_rec.root_id or parent_rec.id
        record.parent_id = parent_id
        record.root_id = root_id
        try:
            store.accept_one(record)
        except _taskq.TaskStoreUnavailable as exc:
            _glue_logger.error("spawn refused: task store write failed: %s", exc)
            return str(exc)
        return None

    def taskq_defer(self, agent_id: str, *, reason: str) -> bool:
        """Keep a queued row parked until the admit-wait passes; wake the pump then."""
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None:
            return False
        wait = self.taskq_admit_wait_secs()
        try:
            ok = store.defer(agent_id, store.now() + wait, reason=reason)
        except _taskq.TaskStoreUnavailable:
            _glue_logger.warning("taskq: defer of %s failed", agent_id, exc_info=True)
            return False
        if ok:
            try:
                _asyncio.get_event_loop().call_later(wait, self._manager._drain_queue)
            except RuntimeError:
                pass
        return ok

    def taskq_defer_posted(self, agent_id: str, *, reason: str) -> None:
        """:meth:`taskq_defer` for a row known to exist, with the write posted
        to the writer thread (inline without a loop). The pump wake is
        scheduled either way. The posted task is recorded in
        ``_pending_defers[agent_id]`` so the accept path can await it before
        it lets the pump see the row (:meth:`await_pending_defer`)."""
        store = self.taskq_store()
        if store is None:
            return
        wait = self.taskq_admit_wait_secs()
        task = self._post_store_write(
            store, f"defer {agent_id}", store.defer, agent_id, store.now() + wait, reason=reason
        )
        if task is not None:
            pending = getattr(self._manager, "_pending_defers", None)
            if pending is None:
                pending = {}
                setattr(self._manager, "_pending_defers", pending)
            pending[agent_id] = task
        try:
            _asyncio.get_event_loop().call_later(wait, self._manager._drain_queue)
        except RuntimeError:
            pass

    async def await_pending_defer(self, agent_id: str) -> None:
        """Wait for the defer :meth:`taskq_defer_posted` posted for *agent_id*,
        so ``next_run_at`` is on the row before the pump may refill it."""
        pending = getattr(self._manager, "_pending_defers", None)
        task = pending.pop(agent_id, None) if pending else None
        if task is not None:
            try:
                await task
            except Exception:  # noqa: BLE001 - the write logged its own failure
                pass

    def park_defer(
        self,
        agent_id: str,
        *,
        reason: str,
        parent_session_key: str,
        batch_id: str,
        queued: "SubagentInfo",
        refused: "SubagentInfo",
    ) -> None:
        """Hand a drained row's defer to the coroutine dispatcher to write.

        The point is assembled HERE rather than in ``gate``: ``spawn_impl`` runs
        rebound on ``subagent``'s globals, where a name imported at the top of
        its own module is inert. Parked on the MANAGER, keyed by id: the
        coordinator carries no state of its own (``__slots__``), and a key is
        what lets two dispatches be in flight without either reading the
        other's answer.
        """
        parked = getattr(self._manager, "_parked_defers", None)
        if parked is None:
            parked = {}
            setattr(self._manager, "_parked_defers", parked)
        parked[agent_id] = DeferPoint(
            agent_id=agent_id,
            reason=reason,
            parent_session_key=parent_session_key,
            batch_id=batch_id,
            queued=queued,
            refused=refused,
        )

    async def finish_parked_defer(self, info: "SubagentInfo") -> "SubagentInfo":
        """Write the defer parked for *info*, off-loop, and answer with its
        boolean; *info* itself when nothing was parked under that id.

        A refused write has TWO causes and they owe the requester different
        answers, so the row is re-read on the SAME writer-thread submission that
        wrote -- never on a second one, which would be another window.

        No row at all is a ``_queue`` entry that never reached the store
        (``_from_queue`` does not prove a row), and refusing is the only honest
        answer: a queued handle for a row no dispatch will ever pick is a spawn
        the requester never hears about again.

        A row that EXISTS and is past ``CLAIMABLE`` was cancelled while this
        awaited, and its stop has already been announced by the canceller. So the
        answer is the shape ``_after_dispatch_impl`` recognises and swallows
        (``queued and done and user_stopped``), never a rejection: announcing one
        here would give a single id two contradictory terminal events, one saying
        the user stopped it and one saying the host refused it.
        """
        from dataclasses import replace as _replace

        from kiro_crew import taskq as _taskq

        parked = getattr(self._manager, "_parked_defers", None)
        point = parked.pop(info.id, None) if parked else None
        if point is None:
            return info
        store = self.taskq_store()
        wait = self.taskq_admit_wait_secs()
        ok = False
        existing: Any = None
        if store is not None:

            def _defer_then_read(live: "_taskq.TaskStore") -> "tuple[bool, Any]":
                if live.defer(point.agent_id, live.now() + wait, reason=point.reason):
                    return True, None
                return False, live.get(point.agent_id)

            try:
                ok, existing = await store.run(_defer_then_read, store)
            except _taskq.TaskStoreUnavailable:
                _glue_logger.warning("taskq: defer of %s failed", point.agent_id, exc_info=True)
        if not ok:
            if existing is not None:
                return _replace(point.queued, done=True, user_stopped=True)
            return self._manager._announce_rejection(point.refused)
        try:
            _asyncio.get_event_loop().call_later(wait, self._manager._drain_queue)
        except RuntimeError:
            pass
        self._manager._emit_queue_depth(point.parent_session_key, point.batch_id)
        return point.queued

    async def taskq_should_window_async(self, agent_id: str) -> bool:
        """:meth:`taskq_should_window` with its store count on the writer thread."""
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None:
            return True
        if len(self._manager._queue) >= store.window:
            return False
        exclude = [*self.taskq_window_ids(), agent_id]
        try:
            waiting_outside = await store.run(
                store.count_pending, _taskq.KIND_SUBAGENT, exclude_ids=exclude
            )
        except _taskq.TaskStoreUnavailable:
            return True
        return int(waiting_outside) == 0

    def _post_store_write(
        self, store: "_taskq.TaskStore", what: str, fn: Any, *args: Any, **kw: Any
    ) -> "_asyncio.Task[Any] | None":
        """Run a best-effort store write whose result nothing waits for.

        On a running loop (with the off-loop pump on) the write is POSTED to
        the store's single writer thread (``TaskStore.run``): the loop never
        holds the SQLite lock, and because that executor has one worker the
        writes land in submission order -- ``admitted -> starting`` posted
        here lands before the run's own ``running`` write posted later.
        Without a loop the write runs inline.
        """
        from kiro_crew import taskq as _taskq

        try:
            loop = _asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or not type(self).pump_off_loop:
            try:
                fn(*args, **kw)
            except _taskq.TaskStoreUnavailable:
                _glue_logger.debug("taskq: %s write failed", what, exc_info=True)
            return None

        async def _write() -> None:
            try:
                await store.run(fn, *args, **kw)
            except _taskq.TaskStoreUnavailable:
                _glue_logger.debug("taskq: %s write failed", what, exc_info=True)
            except Exception:
                _glue_logger.warning("taskq: %s write raised", what, exc_info=True)

        return self.track_store_task(loop.create_task(_write()))

    def track_store_task(self, task: "_asyncio.Task[Any]") -> "_asyncio.Task[Any]":
        """Keep a posted store write alive and drainable.

        Posted writes join ``_report_tasks``, the set ``cancel_all`` drains with
        a bounded wait before the loop closes: a terminal ``finish`` that is
        still on its way to the writer thread when the gateway stops must land,
        or the row stays ``running`` and the next boot's reconcile re-dispatches
        work the user already stopped. The strong reference in the set also
        keeps the task from being garbage-collected mid-flight.
        """
        tasks = getattr(self._manager, "_report_tasks", None)
        if isinstance(tasks, set):
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        return task

    def taskq_mark(self, info: SubagentInfo, state: str) -> None:
        """Best-effort generation-fenced state write for a registered run.

        POSTED, so it is ordered against the other posted writes on the store's
        single writer thread -- in particular the wait write ``yield_slot`` posts
        behind it. A caller whose OWN next store call must see this mark landed
        cannot use this: it has to run both on one thread (see
        ``RunEventCoordinator._dependency_report_db``).
        """
        store = self.taskq_store()
        if store is None:
            return
        self._post_store_write(
            store,
            f"{info.id} -> {state}",
            self.taskq_advance,
            info.id,
            state,
            info._taskq_generation or None,
        )

    def taskq_advance(self, agent_id: str, state: str, generation: int | None) -> bool:
        """Store phase of :meth:`taskq_mark`: ``TaskStore.advance``.

        A mark is posted, so its refusal reaches nobody -- and the spawn gate
        cannot await one, because the run task already exists when the
        ``admitted -> starting`` mark goes out. One locked database between the
        claim and that mark would therefore keep a LIVE run's row in
        ``admitted``, the one state a boot reconciler requeues without asking the
        side-effect class, for the whole run: every LATER mark asks for an edge
        the table forbids from there and is refused too. ``advance`` replays the
        missed step from the row's own state instead, under this run's
        generation. The DEPENDENCY path's mark shares that one entry point --
        ``RunEventCoordinator._dependency_report_db`` -- so neither path can gain
        the replay without the other.
        """
        store = self.taskq_store()
        if store is None:
            return False
        return store.advance(agent_id, state, generation=generation)

    def taskq_fail(self, agent_id: str, reason: str) -> None:
        """Terminal ``failed`` for a persisted row refused before it registered."""
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None:
            return
        self._post_store_write(
            store, f"fail {agent_id}", store.finish, agent_id, _taskq.FAILED, error=reason
        )

    def taskq_settle(self, info: SubagentInfo) -> None:
        """Write the run's terminal state from its record; fenced by generation.

        Called from ``_claim_finalize``, the one-shot report token, so exactly
        the reporter of the outcome writes it. ``user_stopped`` is a neutral
        cancel, an ``error`` is a failure, anything else is done.
        """
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None:
            return
        if info.user_stopped:
            state = _taskq.CANCELLED
        elif info.error:
            state = _taskq.FAILED
        else:
            state = _taskq.DONE
        result_ref: str | None = None
        try:
            from kiro_crew.subagent_persistence import agent_dir_for_display

            result_ref = str(agent_dir_for_display(info.id) / "result.txt")
        except (ValueError, OSError):
            result_ref = None

        def _propagate() -> None:
            # Upward propagation (taskq.waits): a waiting parent wakes on its
            # LAST child; a ``fail_parent`` policy fails it now. After the
            # child's own terminal write so the parent's view of its children
            # is consistent.
            self.taskq_child_terminal(info, state)
            # Downward: a cancelled parent takes its children with it
            # (``cancel_tree``); a failed or finished one leaves them running
            # -- their results are delivered to the parent session by id.
            if state == _taskq.CANCELLED:
                self.taskq_cancel_children_of(info.id, reason="parent_cancelled")

        async def _propagate_async() -> None:
            # The propagation makes store writes OF ITS OWN, so the async
            # entries are what the posted settle uses: offloading only the
            # outermost ``finish`` and then reaching the ledger from this
            # callback would leave the ledger's writes on the loop.
            await self.taskq_child_terminal_async(info, state)
            if state == _taskq.CANCELLED:
                await self.taskq_cancel_children_of_async(info.id, reason="parent_cancelled")

        finish_kw: dict[str, Any] = dict(
            generation=info._taskq_generation or None,
            result_ref=result_ref,
            error=info.error or None,
        )
        try:
            loop = _asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or not type(self).pump_off_loop:
            try:
                ok = bool(store.finish(info.id, state, **finish_kw))
            except _taskq.TaskStoreUnavailable:
                _glue_logger.debug("taskq: settle of %s failed", info.id, exc_info=True)
                return
            self.taskq_report_refused_settle(store, info.id, state, ok)
            _propagate()
            return

        async def _settle() -> None:
            # The terminal write on the writer thread (in order with every
            # other posted write), the propagation after it -- its own store
            # phase offloaded too, its live-run bookkeeping back on the loop.
            try:
                ok = bool(await store.run(store.finish, info.id, state, **finish_kw))
            except _taskq.TaskStoreUnavailable:
                _glue_logger.debug("taskq: settle of %s failed", info.id, exc_info=True)
                return
            except Exception:
                _glue_logger.warning("taskq: settle of %s raised", info.id, exc_info=True)
                return
            if not ok:
                await store.run(self.taskq_report_refused_settle, store, info.id, state, ok)
            await _propagate_async()

        self.track_store_task(loop.create_task(_settle()))

    @staticmethod
    def taskq_report_refused_settle(
        store: "_taskq.TaskStore", agent_id: str, state: str, committed: bool
    ) -> None:
        """Say out loud that a run's terminal write did NOT commit.

        The transition table refuses a terminal from a PARKED row on purpose --
        ``retry_wait -> done`` is closed so a wait that could not be recorded is
        a loud failure rather than a row that quietly re-runs finished work
        (``test_a_wait_is_never_reachable_from_starting``). Loud holds only while
        someone READS the boolean, because a discarded refusal is the quietest
        outcome there is: a claimable row after the work already ran. The row's
        ACTUAL state is named, because that is what says whether the refusal was
        a park (re-dispatchable) or a newer owner's outcome (settled). Never a
        state written from here: from where the row sits the table does not
        accept what this run finished as, and any terminal it WOULD accept
        (``failed``, ``cancelled``) would report a run that succeeded as one
        that did not.
        """
        from kiro_crew import taskq as _taskq

        if committed:
            return
        try:
            current = store.state_of(agent_id)
        except _taskq.TaskStoreUnavailable:
            current = None
        _glue_logger.warning(
            "taskq: terminal write %s -> %s did not commit; the row is %s",
            agent_id,
            state,
            current or "gone",
        )

    def taskq_overflow(self, parent_session_key: str | None = None) -> int:
        """Queued rows that live only in the store (outside the in-memory window)."""
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None:
            return 0
        try:
            return store.count_pending(
                _taskq.KIND_SUBAGENT,
                exclude_ids=self.taskq_excluded_ids(),
                session_key=parent_session_key,
            )
        except _taskq.TaskStoreUnavailable:
            return 0

    async def taskq_overflow_async(self, parent_session_key: str | None = None) -> int:
        """:meth:`taskq_overflow` with its store count on the writer thread.

        The exclusion set is in-memory manager state, so it is snapshotted HERE
        on the loop and handed over, never read from the writer thread -- the
        same split :meth:`taskq_child_registered_async` makes.
        """
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None:
            return 0
        exclude = self.taskq_excluded_ids()
        try:
            return int(
                await store.run(
                    store.count_pending,
                    _taskq.KIND_SUBAGENT,
                    exclude_ids=exclude,
                    session_key=parent_session_key,
                )
            )
        except _taskq.TaskStoreUnavailable:
            return 0

    def taskq_pending_ids_for(self, parent_session_key: str) -> list[str]:
        """Ids of this parent's waiting rows that are NOT in the in-memory window."""
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None or not parent_session_key:
            return []
        try:
            rows = store.list_pending(
                _taskq.KIND_SUBAGENT,
                session_key=parent_session_key,
                exclude_ids=self.taskq_excluded_ids(),
            )
        except _taskq.TaskStoreUnavailable:
            return []
        return [r.id for r in rows]

    async def taskq_pending_ids_for_async(self, parent_session_key: str) -> list[str]:
        """:meth:`taskq_pending_ids_for` with its store read on the writer thread."""
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None or not parent_session_key:
            return []
        exclude = self.taskq_excluded_ids()
        try:
            rows = await store.run(
                store.list_pending,
                _taskq.KIND_SUBAGENT,
                session_key=parent_session_key,
                exclude_ids=exclude,
            )
        except _taskq.TaskStoreUnavailable:
            return []
        return [r.id for r in rows]

    def taskq_batch_pending(self, batch_id: str) -> bool:
        """True when a store-only queued row belongs to *batch_id*."""
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None or not batch_id:
            return False
        try:
            rows = store.fetch_pending_by_batch(
                _taskq.KIND_SUBAGENT, batch_id, exclude_ids=self.taskq_excluded_ids()
            )
        except _taskq.TaskStoreUnavailable:
            return False
        return bool(rows)

    async def taskq_batch_pending_async(self, batch_id: str) -> bool:
        """:meth:`taskq_batch_pending` with its store read on the writer thread."""
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None or not batch_id:
            return False
        exclude = self.taskq_excluded_ids()
        try:
            rows = await store.run(
                store.fetch_pending_by_batch,
                _taskq.KIND_SUBAGENT,
                batch_id,
                exclude_ids=exclude,
            )
        except _taskq.TaskStoreUnavailable:
            return False
        return bool(rows)

    def taskq_refill_window(self, *, children_only: bool = False) -> int:
        """Pull eligible store rows into ``_queue`` up to the window; returns how many.

        Lane-fair across the whole store, not just the window: each lane's
        rows enter oldest-first, and the lanes are interleaved by the same
        weighted round-robin the drain picks with, so one lane's backlog never
        fills the window while another lane's single row waits on disk.
        ``children_only`` admits nested rows only (the child reserve). Rows
        deferred past now are skipped, and when the window is otherwise empty
        the pump schedules its own wake-up at the earliest ``next_run_at``.

        Inline variant (sync callers): the store steps run on the calling
        thread. :meth:`taskq_refill_window_async` runs the same steps with
        every store read on the writer thread.
        """
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None:
            return 0
        # Wait deadlines are checked on the pump path: cheap, and the pump is
        # what runs whenever capacity moves.
        self.taskq_expire_waits()
        rows: list[_taskq.TaskRecord] = []
        try:
            absent, lanes = self._refill_absent(store, children_only)
            room, want = self._refill_make_room(store, absent, lanes)
            rows = self._refill_fetch(store, absent, room, want, children_only)
            self._refill_apply(rows)
            wake_at = (
                self._refill_idle_wake_at(store) if not rows and not self._manager._queue else None
            )
        except _taskq.TaskStoreUnavailable:
            return len(rows)
        self._refill_schedule_wake(store, wake_at)
        return len(rows)

    async def taskq_refill_window_async(self, *, children_only: bool = False) -> int:
        """:meth:`taskq_refill_window` for the event-loop pump: the store
        reads run on the store's writer thread (``TaskStore.run``); only the
        window bookkeeping touches loop state."""
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None:
            return 0
        rows: list[_taskq.TaskRecord] = []
        try:
            absent, lanes = await store.run(self._refill_absent, store, children_only)
            room, want = self._refill_make_room(store, absent, lanes)
            rows = await store.run(self._refill_fetch, store, absent, room, want, children_only)
            self._refill_apply(rows)
            wake_at = (
                await store.run(self._refill_idle_wake_at, store)
                if not rows and not self._manager._queue
                else None
            )
        except _taskq.TaskStoreUnavailable:
            return len(rows)
        self._refill_schedule_wake(store, wake_at)
        return len(rows)

    def _refill_absent(
        self, store: "_taskq.TaskStore", children_only: bool
    ) -> tuple[list[str], dict[str, str]]:
        """Store read: lanes with a waiting row whose head is not in the window.

        Returns the absent lanes AND the lane resolution the window entries
        needed, because the eviction that follows runs on the LOOP and asks the
        same question of the same entries: resolving it here (on the writer
        thread, in the async path) is what keeps that step off the connection.
        (The window's lane set is read on whatever thread calls.)
        """
        from kiro_crew import taskq as _taskq

        lanes = self._resolve_lanes(self._window_lane_keys())
        present = {
            self.lane_of_entry(p, lanes)
            for p in list(self._manager._queue)
            if not p.get("_resume_id") and (not children_only or self.entry_is_child(p))
        }
        pending = store.pending_lanes(
            _taskq.KIND_SUBAGENT,
            exclude_ids=self.taskq_excluded_ids(),
            children_only=children_only,
        )
        return [lane for lane in pending if lane not in present], lanes

    def _refill_make_room(
        self, store: "_taskq.TaskStore", absent: list[str], lanes: "Mapping[str, str] | None" = None
    ) -> tuple[int, int]:
        """Loop step: every lane with a store row waiting must have its head in
        the window, or the pick cannot take turns with it. A window full of
        one lane makes room by dropping that lane's YOUNGEST entries back to
        store-only (they are queued rows; the refill brings them back in FIFO
        order), never below one entry per lane."""
        queue = self._manager._queue
        want = min(len(absent), store.window) if absent else 0
        if absent:
            self._evict_for_lanes(want - (store.window - len(queue)), lanes)
        return store.window - len(queue), want

    def _refill_fetch(
        self,
        store: "_taskq.TaskStore",
        absent: list[str],
        room: int,
        want: int,
        children_only: bool,
    ) -> list["_taskq.TaskRecord"]:
        """Store read: the absent lanes' heads first, then whatever room is left."""
        from kiro_crew import taskq as _taskq

        exclude = self.taskq_excluded_ids()
        rows: list[_taskq.TaskRecord] = []
        if absent and room > 0:
            rows.extend(
                store.fetch_dispatchable_fair(
                    _taskq.KIND_SUBAGENT,
                    limit=min(room, want),
                    scheduler=self.lane_refill_scheduler(),
                    exclude_ids=exclude,
                    children_only=children_only,
                    lanes=absent,
                    per_lane_limit=1,
                )
            )
            exclude = exclude + [rec.id for rec in rows]
            room -= len(rows)
        if room > 0:
            rows.extend(
                store.fetch_dispatchable_fair(
                    _taskq.KIND_SUBAGENT,
                    limit=room,
                    scheduler=self.lane_refill_scheduler(),
                    exclude_ids=exclude,
                    children_only=children_only,
                )
            )
        return rows

    def _refill_apply(self, rows: list["_taskq.TaskRecord"]) -> None:
        """Loop step: the fetched rows join the window (skipping any that a
        concurrent step already placed).

        NOTHING joins the window while gateway admission is closed, the same
        gate every spawn passes: the updater reads the in-flight census once and
        then replaces the process, and a row hydrated behind that read would be
        picked by the pump only to be REFUSED by the spawn gate -- an accepted,
        durable row announced as a rejection. Left on disk it is simply the next
        boot's work. Nothing is lost by dropping them here: the fetch is a pure
        read (``fetch_dispatchable_fair`` claims nothing) and the eviction that
        made room put its entries back to store-only, so a pass that runs into
        the gate can only SHRINK the window -- which is what the census wants.
        """
        sessions = getattr(self._manager, "_sessions", None)
        if getattr(sessions, "admission_closed", False) is True:
            return
        present = {p.get("_preassigned_id") for p in self._manager._queue}
        for rec in rows:
            if rec.id not in present:
                self._manager._queue.append(self._window_entry(rec))

    @staticmethod
    def _refill_idle_wake_at(store: "_taskq.TaskStore") -> float | None:
        """Store read: the earliest ``next_run_at`` of a deferred row."""
        from kiro_crew import taskq as _taskq

        return store.next_eligible_at(_taskq.KIND_SUBAGENT)

    def _refill_schedule_wake(self, store: "_taskq.TaskStore", wake_at: float | None) -> None:
        if wake_at is None:
            return
        delay = max(0.0, min(wake_at - store.now(), self.taskq_admit_wait_secs()))
        try:
            _asyncio.get_event_loop().call_later(delay, self._manager._drain_queue)
        except RuntimeError:
            pass

    @staticmethod
    def _window_entry(rec: "_taskq.TaskRecord") -> dict[str, Any]:
        params = dict(rec.params)
        params["_preassigned_id"] = rec.id
        params["_lane"] = rec.lane
        params.pop("_legacy_import", None)
        # A row written by a build that still stored the flag re-validates too.
        params.pop("_agent_prevalidated", None)
        return params

    def _evict_for_lanes(self, count: int, lanes: "Mapping[str, str] | None" = None) -> int:
        """Drop up to *count* window entries back to store-only to make room.

        Takes the youngest entry of the most-represented lane each time, never
        a resume entry and never a lane's last entry, so every lane already in
        the window keeps its head and its FIFO order. *lanes* answers the lane
        of an entry that names none from :meth:`_refill_absent`'s off-loop
        resolution, so this loop step takes no store read of its own.
        """
        queue = self._manager._queue
        evicted = 0
        while evicted < count:
            by_lane: dict[str, list[int]] = {}
            for idx, params in enumerate(queue):
                if params.get("_resume_id") or not params.get("_preassigned_id"):
                    continue
                by_lane.setdefault(self.lane_of_entry(params, lanes), []).append(idx)
            candidates = [(len(ids), ids[-1]) for ids in by_lane.values() if len(ids) > 1]
            if not candidates:
                break
            _, victim = max(candidates)
            queue.pop(victim)
            evicted += 1
        return evicted

    def lane_refill_scheduler(self) -> "_lanes.LaneScheduler":
        """A second balance for the store->window refill, so filling the window
        does not spend the credit the drain picks with."""
        from kiro_crew.taskq import lanes as _lanes

        scheduler = getattr(self._manager, "_lane_refill_scheduler", None)
        settings = self.fairness_settings()
        if not isinstance(scheduler, _lanes.LaneScheduler):
            scheduler = _lanes.LaneScheduler(weights=settings.lane_weights)
            setattr(self._manager, "_lane_refill_scheduler", scheduler)
        else:
            scheduler.weights = settings.lane_weights
        return scheduler

    def taskq_should_window(self, agent_id: str) -> bool:
        """Whether the just-accepted spawn *agent_id* may go straight into ``_queue``.

        Only when the window has room AND no OTHER row is waiting outside it;
        otherwise the new row is store-only and the refill brings it in later,
        which is what keeps dispatch FIFO across the window boundary.
        """
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None:
            return True
        if len(self._manager._queue) >= store.window:
            return False
        try:
            waiting_outside = store.count_pending(
                _taskq.KIND_SUBAGENT, exclude_ids=[*self.taskq_window_ids(), agent_id]
            )
        except _taskq.TaskStoreUnavailable:
            return True
        return waiting_outside == 0

    def taskq_cancel_queued(self, agent_id: str) -> dict[str, Any] | None:
        """Cancel a persisted row that has not started; returns its params for the report.

        Race-safe against the drain because the STATE TEST AND THE CANCEL SHARE
        ONE TRANSACTION: ``unstarted`` is both the predicate this code judges the
        row it read by and the ``only_from`` the store re-tests under the
        generation that read returned, so a row a drain claimed and started in
        between answers None here and the caller falls through to the live reap
        -- never a ``cancelled`` row with a running spawn under it, whose own
        later writes the generation bump would fence out. A row claimed but not
        started (``admitted``) is still this path's: the spawn re-reads the state
        before it registers and stops there.
        """
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None:
            return None
        try:
            rec = store.get(agent_id)
            if rec is None or rec.terminal:
                return None
            unstarted = _taskq.CLAIMABLE | frozenset({_taskq.ADMITTED})
            if rec.state not in unstarted:
                return None
            previous = store.cancel(
                agent_id, reason="user_stop", only_from=unstarted, generation=rec.generation
            )
        except _taskq.TaskStoreUnavailable:
            _glue_logger.warning("taskq: cancel of %s failed", agent_id, exc_info=True)
            return None
        if previous is None:
            return None
        params = dict(rec.params)
        params["_preassigned_id"] = agent_id
        return params
