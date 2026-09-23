"""Durable task queue glue: store open/accept/defer/settle, the bounded window and its refill, off-loop store writes."""

from __future__ import annotations

import asyncio as _asyncio
import logging as _logging
import time as _time
from typing import TYPE_CHECKING, Any, Mapping

from .._component import ManagerComponent
from .types import DeferPoint, FairnessSettings, tombstone_terminal_state

_glue_logger = _logging.getLogger("kiro_crew.subagent_manager.admission")

# How many times a single row's state is re-read before the answer is given up on, and the
# pause between tries. Same shape and reasoning as the durable sweep's own bound: an error
# reading one row is normally writer-thread contention, not an outage.
_STATE_READ_ATTEMPTS = 3
_STATE_READ_BACKOFF_SECS = 0.2

#: What a store-only queued count answers while the store cannot be read: SOME
#: waiting work, never none. Every consumer of that count is a fail-closed
#: predicate -- the attached-children guard before a session teardown
#: (``dashboard.chat_utils.subagents_attached``), the cron reset-deferral guards
#: (``has_pending_work_for``), the Slack pending probe -- and 0 is the one answer
#: that lets them strand an accepted child's completion on a cold-started
#: replacement session. The queue-depth chip is the only reader for which this is
#: a number rather than a predicate, and one advisory unit during an outage the
#: log already names is the whole price.
UNKNOWN_PENDING = 1

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
        #
        # An ad-hoc ``approval_mode="auto"`` is the same kind of fact and a
        # GRANT besides: it skips the spawn gate and pre-approves the run's
        # tools. The pump respawns a recovered row by forwarding these params
        # verbatim, so persisting it would let a restart start work and run
        # tools on an authorisation nobody renewed -- one request's consent
        # replayed after the process that received it is gone. It is dropped
        # here, so a recovered row faces the gate its caller faced; the value is
        # still recorded in ``scope_ref``, which the schema defines as
        # references rather than grants and which no start path reads.
        _PROCESS_LOCAL_PARAMS = ("_agent_prevalidated", "approval_mode")
        durable_params = {k: v for k, v in params.items() if k not in _PROCESS_LOCAL_PARAMS}
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
            except Exception:
                # The SAME arms as the posted path below, and for a reason
                # stronger than symmetry: ``_claim_finalize`` spends the
                # one-shot report token BEFORE it calls this, so an exception
                # leaving here costs the terminal report as well as the
                # propagation -- the claimer never sees True, so it reports
                # nothing, and no second claimer can. A store call that raises
                # something other than ``TaskStoreUnavailable`` is a defect to
                # be read in the log, never a run whose outcome no one hears.
                _glue_logger.warning("taskq: settle of %s raised", info.id, exc_info=True)
            else:
                self.taskq_report_refused_settle(store, info.id, state, ok)
                # The one refusal that is another owner's outcome to report; see
                # the comment on the propagation below.
                if not ok and self.taskq_superseded_by_live_owner(
                    store, info.id, info._taskq_generation or None
                ):
                    return
            # The propagation is owed by the REPORTER of the outcome, not by the
            # row's write, because ``_claim_finalize`` hands out one token: a
            # parent this settle leaves unwoken is a tree nothing retries, and a
            # cancelled parent's children keep running. So it runs on every arm
            # EXCEPT the one where the reporter is not the owner --
            # :meth:`taskq_superseded_by_live_owner`, a LIVE newer generation
            # holding the row. There the propagation is not merely redundant: it
            # tells the ledger this child is terminal, so a parent waiting on it
            # as its last child is woken while the replacement is still running.
            # Otherwise it decides from the child id it is given, never from the
            # child's row, so a refused write does not change what it decides;
            # both halves absorb a store outage of their own.
            _propagate()
            return

        async def _settle() -> None:
            # The terminal write on the writer thread (in order with every
            # other posted write), the propagation after it -- its own store
            # phase offloaded too, its live-run bookkeeping back on the loop,
            # and reached from every arm for the reason the inline path gives.
            try:
                ok = bool(await store.run(store.finish, info.id, state, **finish_kw))
            except _taskq.TaskStoreUnavailable:
                _glue_logger.debug("taskq: settle of %s failed", info.id, exc_info=True)
            except Exception:
                _glue_logger.warning("taskq: settle of %s raised", info.id, exc_info=True)
            else:
                if not ok:
                    await store.run(self.taskq_report_refused_settle, store, info.id, state, ok)
                    superseded = False
                    try:
                        superseded = bool(
                            await store.run(
                                self.taskq_superseded_by_live_owner,
                                store,
                                info.id,
                                info._taskq_generation or None,
                            )
                        )
                    except Exception:
                        # Defaults to propagating, which is the direction that
                        # cannot strand a parent: a read this settle could not
                        # make is no evidence that anybody else owns the report.
                        _glue_logger.debug(
                            "taskq: owner read for %s failed", info.id, exc_info=True
                        )
                    if superseded:
                        return
            await _propagate_async()

        self.track_store_task(loop.create_task(_settle()))

    @staticmethod
    def taskq_superseded_by_live_owner(
        store: "_taskq.TaskStore", agent_id: str, generation: int | None
    ) -> bool:
        """Whether a LIVE newer incarnation of *agent_id* owns its outcome now.

        Read only when the terminal write did NOT commit, and True for the ONE
        refusal whose propagation belongs to somebody else: the row moved to a
        newer generation AND is not terminal, so a replacement holds a
        ``_claim_finalize`` token of its own and will report and propagate when
        it ends. The other two refusals still owe it, which is why the question
        is not "is my generation stale":

        * the transition table refused a PARKED row (``retry_wait -> done`` is
          closed). The generation on the row is still THIS run's, nobody else
          will ever report it, and the propagation is the only thing that ends
          the parent's wait.
        * the generation moved and the row is already TERMINAL -- another owner
          settled it, which is the shape of an operator cancel through
          ``/api/tasks`` (``store.cancel`` bumps the generation).
          ``WaitLedger.on_child_terminal`` rebuilds the terminal set from the
          store, so running it again decides the same thing, while skipping it
          leaves the parent parked until the next boot's ``rebuild``.

        Safe on any thread: one row read and two integer comparisons. An
        unreadable store answers False -- an outage leaves THIS generation on
        the row, so there is no evidence of another owner and the propagation
        stays owed.
        """
        from kiro_crew import taskq as _taskq

        if not generation:
            return False
        try:
            row = store.get(agent_id)
        except _taskq.TaskStoreUnavailable:
            return False
        if row is None or int(row.generation) <= int(generation) or row.terminal:
            return False
        _glue_logger.info(
            "taskq: terminal write for %s was fenced by generation %d, which is still %s; "
            "the propagation is that owner's",
            agent_id,
            int(row.generation),
            row.state,
        )
        return True

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
        """Queued rows that live only in the store (outside the in-memory window).

        A store that cannot be read answers :data:`UNKNOWN_PENDING`, not 0: no
        store at all is a queue with no rows in it, while a locked or full one is
        a queue whose rows nobody can see, and only the first of those is
        evidence that this parent has nothing waiting.
        """
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
            _glue_logger.warning(
                "taskq: queued count for %s unreadable; answering some",
                parent_session_key or "<any parent>",
                exc_info=True,
            )
            return UNKNOWN_PENDING

    async def taskq_overflow_async(self, parent_session_key: str | None = None) -> int:
        """:meth:`taskq_overflow` with its store count on the writer thread.

        The exclusion set is in-memory manager state, so it is snapshotted HERE
        on the loop and handed over, never read from the writer thread -- the
        same split :meth:`taskq_child_registered_async` makes. The outage answer
        is the sync entry's.
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
            _glue_logger.warning(
                "taskq: queued count for %s unreadable; answering some",
                parent_session_key or "<any parent>",
                exc_info=True,
            )
            return UNKNOWN_PENDING

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

    def taskq_cancel_boundary_store(
        self,
        store: "_taskq.TaskStore",
        parent_session_key: str,
        boundary_owner: str,
    ) -> tuple[list[dict[str, Any]], str]:
        """Store phase for exact queued cancellation; runs on the writer thread."""
        from kiro_crew import taskq as _taskq

        cancelled: list[dict[str, Any]] = []
        try:
            rows = store.list_pending(
                _taskq.KIND_SUBAGENT,
                session_key=parent_session_key,
            )
            rows.extend(
                row
                for row in store.active_rows()
                if row.kind == _taskq.KIND_SUBAGENT
                and row.session_key == parent_session_key
                and row.state == _taskq.ADMITTED
            )
            unstarted = _taskq.CLAIMABLE | frozenset({_taskq.ADMITTED})
            seen: set[str] = set()
            for row in rows:
                if (
                    row.id in seen
                    or str(row.params.get("_stage_boundary_owner") or "") != boundary_owner
                ):
                    continue
                seen.add(row.id)
                previous = store.cancel(
                    row.id,
                    reason="user_stop",
                    only_from=unstarted,
                    generation=row.generation,
                )
                if previous is None:
                    continue
                params = dict(row.params)
                params["_preassigned_id"] = row.id
                cancelled.append(params)
        except _taskq.TaskStoreUnavailable as exc:
            return cancelled, str(exc) or "task store unavailable"
        return cancelled, ""

    async def taskq_cancel_boundary_async(
        self,
        parent_session_key: str,
        boundary_owner: str,
    ) -> tuple[list[dict[str, Any]], str]:
        """Cancel exact queued rows on the store's single writer thread."""
        store = self.taskq_store()
        if store is None:
            return [], ""
        return await store.run(
            self.taskq_cancel_boundary_store,
            store,
            parent_session_key,
            boundary_owner,
        )

    def taskq_batch_pending(self, batch_id: str) -> bool:
        """True when a store-only queued row belongs to *batch_id*.

        True as well while the store cannot be read, for
        :data:`UNKNOWN_PENDING`'s reason: the wave's own bookkeeping is PRUNED
        when the digest closes, so a digest closed over members nobody could see
        is not one wrong message -- a SECOND digest can then fire for the same
        batch. Holding the wave open costs a later digest instead.

        Two readers take the answer with OPPOSITE polarity and True is the safer
        failure for both. ``_sweep_stuck_waves`` skips its reconcile on True, and
        the next sweep re-examines the wave. ``_sweep_digest_holds`` forces the
        partial digest out on True -- which it reaches only for a hold already
        past ``DIGEST_HOLD_SECS`` -- so the price is a chunk that may race the
        wave-close flush, against the alternative of leaving every finished
        sibling's result undelivered for as long as the outage lasts.
        """
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None or not batch_id:
            return False
        try:
            rows = store.fetch_pending_by_batch(
                _taskq.KIND_SUBAGENT, batch_id, exclude_ids=self.taskq_excluded_ids()
            )
        except _taskq.TaskStoreUnavailable:
            _glue_logger.warning(
                "taskq: batch %s membership unreadable, holding the wave", batch_id, exc_info=True
            )
            return True
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
            _glue_logger.warning(
                "taskq: batch %s membership unreadable, holding the wave", batch_id, exc_info=True
            )
            return True
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
            rows = self._reconcile_refill_boundaries_sync(store, rows)
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
            rows = await self._reconcile_refill_boundaries_async(rows)
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

    @staticmethod
    def _refill_boundary_scope(rec: "_taskq.TaskRecord") -> tuple[str, str] | None:
        owner = str(rec.params.get("_stage_boundary_owner") or "")
        parent = str(rec.session_key or rec.params.get("parent_session_key") or "")
        return (parent, owner) if parent and owner else None

    def _stale_refill_boundary_scopes(
        self,
        rows: list["_taskq.TaskRecord"],
    ) -> set[tuple[str, str]]:
        """Exact stage scopes without a live boundary owner.

        The resolver is gateway-wired and reads loop-owned dashboard state, so
        the async refill calls this only after its store fetch returns. A manager
        without that resolver keeps legacy/test behavior. Resolver failure is
        observable but is not treated as boundary absence, so no row is
        cancelled without a positive stale-owner verdict.
        """
        resolver = getattr(self._manager, "_stage_boundary_for_scope", None)
        if not callable(resolver):
            return set()
        stale: set[tuple[str, str]] = set()
        for rec in rows:
            scope = self._refill_boundary_scope(rec)
            if scope is None:
                continue
            try:
                boundary = resolver(*scope)
            except Exception:
                _glue_logger.warning(
                    "taskq: stage-boundary lookup failed during refill for parent=%s owner=%s",
                    *scope,
                    exc_info=True,
                )
                continue
            if boundary is None:
                stale.add(scope)
        return stale

    def _without_refill_boundary_scopes(
        self,
        rows: list["_taskq.TaskRecord"],
        stale: set[tuple[str, str]],
    ) -> list["_taskq.TaskRecord"]:
        if not stale:
            return rows
        return [rec for rec in rows if self._refill_boundary_scope(rec) not in stale]

    def _reconcile_refill_boundaries_sync(
        self,
        store: "_taskq.TaskStore",
        rows: list["_taskq.TaskRecord"],
    ) -> list["_taskq.TaskRecord"]:
        """Cancel stale stage rows inline for the non-loop refill variant."""
        stale = self._stale_refill_boundary_scopes(rows)
        for parent, owner in sorted(stale):
            if self._manager._hold_boundary_cancellation(parent, owner):
                continue
            cancelled, failure = self.taskq_cancel_boundary_store(store, parent, owner)
            if failure:
                failure = self._manager._bounded_boundary_cancellation_failure(failure)
            self._manager._apply_boundary_cancelled_rows(
                parent,
                owner,
                cancelled,
                settled=not failure,
            )
            if failure:
                self._manager._pending_boundary_cancellations[(parent, owner)] = failure
                self._manager._schedule_boundary_cancel_retry()
        return self._without_refill_boundary_scopes(rows, stale)

    async def _reconcile_refill_boundaries_async(
        self,
        rows: list["_taskq.TaskRecord"],
    ) -> list["_taskq.TaskRecord"]:
        """Cancel stale stage rows through the exact off-loop writer path."""
        stale = self._stale_refill_boundary_scopes(rows)
        for parent, owner in sorted(stale):
            if self._manager._hold_boundary_cancellation(parent, owner):
                continue
            await self._manager._settle_boundary_queue(parent, owner)
        return self._without_refill_boundary_scopes(rows, stale)

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
        order); a window with no lane to spare gives up one head instead of
        making no room at all (:meth:`_evict_for_lanes`)."""
        queue = self._manager._queue
        # Restricted entries have no durable copy and are outside its window.
        windowed = sum(p.get("_memory_mode", "persistent") == "persistent" for p in queue)
        want = min(len(absent), store.window) if absent else 0
        if absent:
            windowed -= self._evict_for_lanes(want - (store.window - windowed), lanes)
        return max(0, store.window - windowed), want

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
            entry = self._window_entry(rec)
            if self._manager._boundary_cancellation_pending(entry):
                continue
            if rec.id not in present:
                self._manager._queue.append(entry)

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
        # Both process-local params are stripped on the READ side as well as by
        # ``taskq_build_record``, because a row is written by one build and
        # started by another: a row that still carries either one replays one
        # request's moment into a start the request never authorised -- the
        # ownership and agent gates skipped (``_agent_prevalidated``), or the
        # spawn gate skipped and the run's tools pre-approved on a consent
        # nobody renewed (``approval_mode``). The value stays readable in
        # ``scope_ref``, which the schema defines as references rather than
        # grants and which no start path reads.
        params.pop("_agent_prevalidated", None)
        params.pop("approval_mode", None)
        return params

    def _evict_for_lanes(self, count: int, lanes: "Mapping[str, str] | None" = None) -> int:
        """Drop up to *count* window entries back to store-only to make room.

        Takes the youngest entry of the most-represented lane each time and
        never a resume entry, so every lane in the window keeps its head and its
        FIFO order for as long as any lane has a SPARE entry to give.

        A window holding exactly one entry per lane has no spare, and keeping
        every head there frees NOTHING -- which is not a milder outcome but the
        worst one: the refill hydrates no row at all, so the slot the child
        reserve holds open can never be filled from disk and a tree waits on its
        own child for as long as the process lives. The youngest head goes
        instead, at most ONE per call, so the window churns by a single entry:
        that entry is a queued row, refetched in FIFO order on a later pass.

        *lanes* answers the lane of an entry that names none from
        :meth:`_refill_absent`'s off-loop resolution, so this loop step takes no
        store read of its own.
        """
        queue = self._manager._queue
        evicted = 0
        head_dropped = False
        while evicted < count:
            by_lane: dict[str, list[int]] = {}
            for idx, params in enumerate(queue):
                if (
                    params.get("_resume_id")
                    or not params.get("_preassigned_id")
                    or params.get("_memory_mode", "persistent") != "persistent"
                ):
                    continue
                by_lane.setdefault(self.lane_of_entry(params, lanes), []).append(idx)
            spare = [(len(ids), ids[-1]) for ids in by_lane.values() if len(ids) > 1]
            if spare:
                _, victim = max(spare)
            elif by_lane and not head_dropped:
                victim = max(ids[-1] for ids in by_lane.values())
                head_dropped = True
            else:
                break
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

    async def taskq_cancel_queued_async(
        self, agent_id: str, *, allow_admitted: bool = True
    ) -> dict[str, Any] | None:
        """:meth:`taskq_cancel_queued` run whole on the writer thread.

        The sync twin is reached from ``_unqueue`` on paths already off the loop. A
        parent-end teardown is not one of them: it runs as a coroutine inside the
        session lifecycle, so ``store.get``/``store.cancel`` there stall the gateway
        loop for as long as the task store is contended — the
        ``no-sync-store-call-from-a-coroutine`` rule.

        DELEGATION, not a second copy. ``store.run`` takes any callable and runs it on
        the writer thread, so the twin's own body runs there unchanged: one hop instead
        of two, and the race-safety argument in its docstring — the state test and the
        cancel sharing one ``only_from`` under the generation the read returned — is the
        same code rather than the same intent restated. Restating it is what would let
        the two drift, and a drift here reads as "cancelled row with a running spawn
        under it" on exactly one of the two paths.
        """
        store = self.taskq_store()
        if store is None:
            return None
        return await store.run(self.taskq_cancel_queued, agent_id, allow_admitted=allow_admitted)

    async def taskq_row_is_claimed_unstarted_async(self, agent_id: str) -> bool:
        """True only when *agent_id*'s row is CLAIMED and not started (``admitted``).

        This is the one state a parent-end teardown must not reap through the live path:
        the row's claimer sits between its claim and its registration, so stopping it here
        is the act the store's own state gate just refused, reached through another door.
        Every other answer -- started, missing, unreadable -- takes the reap.

        A row that cannot be read answers ``False`` for "claimed", because an unknown is
        not a claim. Folding the two together spared a LIVE run whenever the store was
        briefly unreadable, leaving it working against a conversation that has ended --
        the failure this path exists to prevent. The narrow exemption needs positive
        evidence, so only the state that names it earns it.

        The read is retried on the same bound the durable sweep uses: an error here is
        normally writer-thread contention rather than a real outage.
        """
        # The same deferred import the other 28 store calls in this module use, rather than
        # a module-scope one: this file is reached from the gateway boot path, and the
        # convention there is that a flag-gated subsystem's IMPORT is deferred too. Spelled
        # as the package alias so this line has the shape its siblings do.
        from kiro_crew import taskq as _taskq_model

        store = self.taskq_store()
        if store is None:
            return False
        for attempt in range(_STATE_READ_ATTEMPTS):
            try:
                rec = await store.run(store.get, agent_id)
            except Exception:
                if attempt + 1 < _STATE_READ_ATTEMPTS:
                    await _asyncio.sleep(_STATE_READ_BACKOFF_SECS)
                    continue
                _glue_logger.warning(
                    "taskq: could not read the state of %s in %d attempts; treating it as "
                    "not claimed so a live run is not spared by a store outage",
                    agent_id,
                    _STATE_READ_ATTEMPTS,
                    exc_info=True,
                )
                return False
            return rec is not None and rec.state == _taskq_model.ADMITTED
        return False

    def taskq_cancel_queued(
        self, agent_id: str, *, allow_admitted: bool = True
    ) -> dict[str, Any] | None:
        """Cancel a persisted row that has not started; returns its params for the report.

        ``allow_admitted=False`` refuses a row that has been CLAIMED but not started.
        Stop-all leaves it True: the user pressed Stop, and a claimed-not-started row is
        work they asked to end. A parent-end teardown passes False, for two reasons that
        arrive at the same place. An ``admitted`` row has a claimer between its claim and
        its registration, so cancelling it there leaves that claimer to register and run
        work the teardown believed it had stopped -- and the claimer's own re-read of the
        state before registering is the guard that then has nothing to catch, because the
        row is gone rather than claimable. And a row in that window may be carrying a
        decision a person made (a spawn approval is the visible case), which a teardown
        has no standing to revoke on their behalf. Refusing leaves the row to the
        incarnation that owns it.

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
            unstarted = _taskq.CLAIMABLE
            if allow_admitted:
                unstarted = unstarted | frozenset({_taskq.ADMITTED})
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
