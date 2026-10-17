"""Durable task queue glue: store open/accept/defer/settle, the bounded window and its refill, off-loop store writes.

This module is the import path of ``_TaskqBridgeMixin``, the one class. Its body keeps the
store handle and its settings, the exclusion sets the reads and the refill share, the store's
open, re-open and boot dispatch, and a run's own writes: the best-effort write path nothing
waits for (``_post_store_write`` / ``track_store_task``), the run's marks, the failure of a
row refused before its run registered, and the terminal settle. Every other member is a
module-level function of a ``taskq_bridge_*`` owner that the class binds under the same
name, grouped at the end of the class by owner. Which owner holds what, and where new work
goes: docs/system-specs/modules/taskq.md.
"""

from __future__ import annotations

import asyncio as _asyncio
import logging as _logging
import time as _time
from typing import TYPE_CHECKING, Any, Collection, Mapping

from kiro_crew.subagent_wait_reasons import (  # noqa: F401 - the module's public surface
    QUEUED_WAIT_EXPIRED_TEXT,
    RESUMING_AFTER_RESTART,
    RESUMING_RETRY,
)

from .._component import ManagerComponent
from . import taskq_bridge_accept as _accept
from . import taskq_bridge_cancel as _cancel
from . import taskq_bridge_expiry as _expiry
from . import taskq_bridge_reads as _reads
from . import taskq_bridge_window as _window
from .taskq_bridge_cancel import (  # noqa: F401 - re-exported
    _STATE_READ_ATTEMPTS,
    _STATE_READ_BACKOFF_SECS,
)
from .taskq_bridge_expiry import _OWED_REPLAY_PAGE  # noqa: F401 - re-exported
from .taskq_bridge_reads import (  # noqa: F401 - re-exported
    _DEFER_CLOSERS,
    QUEUED_LISTING_CAP,
    UNKNOWN_PENDING,
    _defer_details,
    _in_memory_queued_entries,
    _read_queued_row,
    _read_queued_rows,
)
from .taskq_bridge_window import (  # noqa: F401 - re-exported
    _OVERDUE_WAKE_GRACE_SECS,
    _OVERDUE_WAKE_WARN_EVERY_SECS,
)
from .types import (  # noqa: F401 - the module's public surface
    MEMORY_WAIT_UNTIL_KEY,
    MIN_RECHECK_DELAY_SECS,
    WINDOW_ENTRY_RECOVERING,
    DeferPoint,
    FairnessSettings,
    QueuedReadUnavailable,
    QueuedRun,
    QueuedRunListing,
    outcome_task_state,
    tombstone_terminal_state,
)

_glue_logger = _logging.getLogger("kiro_crew.subagent_manager.admission")


if TYPE_CHECKING:
    # Lazy, as everywhere in this module: the bridge loads taskq on first use.
    from kiro_crew import taskq as _taskq

    from ...subagent import SubagentInfo, asyncio


class _TaskqBridgeMixin(ManagerComponent):
    __slots__ = ()

    if TYPE_CHECKING:
        # Sibling-mixin methods the bridge reaches through ``self``, in this body and in its
        # ``taskq_bridge_*`` owners; typing only.
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
    # is not a name. Each keeps the globals of the module that defines it (this one,
    # or the owner the class binds it from) and is reached from the rebound
    # ``spawn_impl`` / ``_drain_queue_impl`` as an attribute of ``self``.

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

    def taskq_excluded_ids(
        self, *, counted: Collection[str] = (), window: bool = True
    ) -> list[str]:
        """Rows the refill must never claim: those already in the window AND
        those with a LIVE run in this process. A live run's row can be
        claimable for a moment (a wake lands in ``retry_wait`` until the pump
        grants the slot back); claiming it here would start a second copy of a
        run that is still resident.

        *counted* names admitting rows a COUNT should still see (the chip's,
        :meth:`taskq_chip_excluded_ids`); every other part stays excluded.
        *window* False keeps the window's rows in (the parent-end sweep,
        :meth:`taskq_pending_ids_for_async`)."""
        live = self._live_run_ids()
        # Rows whose accept path is still in flight (``spawn_async``: written,
        # not yet claimed or windowed) belong to that caller, not to the pump.
        # Copy before filtering: the refill runs this on the store's writer
        # thread while ``spawn_async`` adds and discards ids on the loop, and
        # iterating the live set raises if it changes size mid-pass.
        admitting_now = list(getattr(self._manager, "_admitting_ids", ()) or ())
        admitting = [aid for aid in admitting_now if aid not in counted]
        return (self.taskq_window_ids() if window else []) + live + admitting

    def taskq_dispatch_excluded_ids(self, *, counted: Collection[str] = ()) -> list[str]:
        """:meth:`taskq_excluded_ids` plus the rows the pump has popped from the
        window and not yet claimed (``_dispatching_ids``).

        For the pump's own reads: the refill that tops the window up, its wake,
        and (through :meth:`taskq_chip_excluded_ids`) the depth the chip shows.
        A popped row's durable state is still QUEUED until its claim lands, so
        without this the refill re-hydrates a row that is being started and the
        depth counts it as waiting. Cancellation and pending-work reads keep
        :meth:`taskq_excluded_ids`: a parent's Stop must reach a row in exactly
        this popped-unclaimed state, or it starts after the stop has reported
        done.
        """
        dispatching = list(getattr(self._manager, "_dispatching_ids", ()) or ())
        return self.taskq_excluded_ids(counted=counted) + dispatching

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
        """What a subagent run's artifacts prove about how it ended, if anything.

        A readable tombstone decides by its recorded ending, then its cause; an
        unreadable one says nothing. With no tombstone, or one whose cause
        proves nothing (``gateway_restart``), the run is done when its result is
        whole by ``result_is_whole``, the rule the orphan reconcile announces it
        with, so the row and the parent's notice agree. A run folder whose
        result is not whole is ``STARTED``: the spawn gate writes the folder
        before it posts the ``starting`` mark, so the folder is the record that
        a run began even when the row still says ``admitted``, and it is the
        same record the orphan reconcile reports the run from. No folder says
        nothing.
        """
        from kiro_crew import taskq as _taskq
        from kiro_crew.taskq.reconcile import STARTED

        if rec.kind != _taskq.KIND_SUBAGENT:
            return None
        try:
            from kiro_crew.subagent_persistence import (
                _agent_dir,
                _read_state_at,
                _read_tombstone_at,
                result_is_whole,
            )

            folder = _agent_dir(rec.id)
            tombstone, present = _read_tombstone_at(folder / "tombstone.json")
            if tombstone is not None:
                cause = str(tombstone.get("cause") or "")
                if cause != "gateway_restart":
                    return tombstone_terminal_state(cause, str(tombstone.get("outcome") or ""))
            elif present:
                return None
            state = _read_state_at(folder)
            if state is None:
                return None
            return _taskq.DONE if result_is_whole(state) else STARTED
        except (ValueError, OSError):
            return None

    def taskq_boot_dispatch(self) -> None:
        """Kick the pump once the loop runs, so rows that survived a restart start.

        Called from ``start_reaper``. Deferred rows and rows in backoff are not
        eligible yet; the drain schedules its own wake-up for them.
        """
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None:
            return
        # An expiry an earlier process wrote and never reported (the store
        # attached before the reaper started; ``_initialize_taskq`` covers the
        # other order).
        self.taskq_schedule_owed_replay()
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

    def _post_store_write(
        self, store: "_taskq.TaskStore", what: str, fn: Any, *args: Any, **kw: Any
    ) -> "_asyncio.Task[Any] | None":
        """Run a best-effort store write whose result nothing waits for.

        On a running loop (with the off-loop pump on) the write is POSTED to
        the store's single writer thread (``TaskStore.post``) by this call,
        not by a task that runs later: the loop never holds the SQLite lock,
        and because that executor has one worker the writes land in the order
        their callers ran -- ``admitted -> starting`` posted here lands before
        the run's own ``running`` write posted later, and before any read
        queued after this call (the queue-depth chip's re-read relies on it).
        The returned task only waits for the result. Without a loop the write
        runs inline.
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
        try:
            posted = store.post(fn, *args, **kw)
        except Exception:
            _glue_logger.warning("taskq: %s write could not be posted", what, exc_info=True)
            return None
        # Retrieved here too, so a waiter cancelled before its first step (the
        # shutdown drain) does not leave the outcome reported as never read.
        posted.add_done_callback(lambda done: done.cancelled() or done.exception())

        async def _write() -> None:
            try:
                await posted
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

    @staticmethod
    def _release_taken_start(store: "_taskq.TaskStore", agent_id: str, generation: int) -> bool:
        """Give back a start the claimer took but did not carry to registration.

        One generation-fenced ``starting -> retry_wait`` write with no due time,
        so the row is claimable again at once. A row that has moved on (cancelled,
        requeued, or settled by another owner) is refused by the fence or the
        transition table and left alone. Safe on the writer thread.
        """
        from kiro_crew import taskq as _taskq

        return store.transition(
            agent_id,
            _taskq.RETRY_WAIT,
            generation=generation,
            next_run_at=None,
            detail={"released": "start not carried to registration"},
        )

    @staticmethod
    def _release_untaken_claim(store: "_taskq.TaskStore", agent_id: str, generation: int) -> bool:
        """Give back a claim whose claimer was cancelled before it used it.

        One generation-fenced ``admitted -> queued`` write with no due time, so
        the row is unleased and claimable again at once. A row that has moved on
        (cancelled, requeued, started or settled by another owner) is refused by
        the fence or the transition table and left alone. Safe on the writer
        thread.
        """
        from kiro_crew import taskq as _taskq

        return store.transition(
            agent_id,
            _taskq.QUEUED,
            generation=generation,
            next_run_at=None,
            detail={"released": "claim not carried to its start"},
        )

    def taskq_settle(self, info: SubagentInfo, *, row_settled: bool = False) -> None:
        """Write the run's terminal state from its record; fenced by generation.

        Called from ``_claim_finalize``, the one-shot report token, so exactly
        the reporter of the outcome writes it. ``user_stopped`` is a neutral
        cancel, an ``error`` is a failure, anything else is done.

        *row_settled* says the reporter's own store call already wrote that
        state -- a queued stop whose own cancel of the waiting row landed --
        so the ``finish`` is skipped: it could only be refused, and it would cost a
        writer job per row. The propagation still runs, because the cancel
        does not propagate and the reporter owes it either way.
        """
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None:
            return
        state = outcome_task_state(info.outcome) or _taskq.DONE
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
        if row_settled:
            if loop is None or not type(self).pump_off_loop:
                _propagate()
            else:
                self.track_store_task(loop.create_task(_propagate_async()))
            return
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
        # A row that already holds the state this write asked for lost nothing:
        # another write settled it the same way first. A queued stop whose own
        # cancel did not land re-posts that cancel ahead of its settle, so when
        # the store answers again the re-posted cancel writes ``cancelled`` and
        # this ``finish`` is refused over the very state it asked for
        # (``_repost_unlanded_cancel`` has already warned). Not a warning.
        log = _glue_logger.debug if current == state else _glue_logger.warning
        log(
            "taskq: terminal write %s -> %s did not commit; the row is %s",
            agent_id,
            state,
            current or "gone",
        )

    # ── bound from the ``taskq_bridge_*`` owners ─────────────────────────────
    # Each name below IS that owner's module-level function, so ``self.<name>``,
    # ``SpawnAdmissionCoordinator.<name>`` and a class-level patch reach it exactly as
    # they reach a method written in this body. Two kinds of member must stay a ``def``
    # above for mypy: one a sibling mixin also declares (its multiple-inheritance check
    # binds ``self`` only on a ``def``), and one called through ``self`` by a function
    # whose nested function relies on an outer ``is None`` check (reading a bound name
    # defers that function's check, which drops the narrowing inside it).
    # From ``taskq_bridge_accept``: a waiting spawn's accept and its defers.
    taskq_accept = _accept.taskq_accept
    taskq_build_record = _accept.taskq_build_record
    taskq_accept_record = _accept.taskq_accept_record
    taskq_defer = _accept.taskq_defer
    taskq_defer_posted = _accept.taskq_defer_posted
    await_pending_defer = _accept.await_pending_defer
    park_defer = _accept.park_defer
    finish_parked_defer = _accept.finish_parked_defer
    # From ``taskq_bridge_expiry``: the max wait of a memory deferral and its owed reports.
    taskq_memory_wait_bound_secs = _expiry.taskq_memory_wait_bound_secs
    _memory_wait_exclusions = _expiry._memory_wait_exclusions
    _expire_memory_waits_db = staticmethod(_expiry._expire_memory_waits_db)
    _sweep_inputs = _expiry._sweep_inputs
    taskq_expire_memory_waits = _expiry.taskq_expire_memory_waits
    taskq_expire_memory_waits_async = _expiry.taskq_expire_memory_waits_async
    _report_memory_waits_expired = _expiry._report_memory_waits_expired
    _clear_owed_report_after = _expiry._clear_owed_report_after
    taskq_clear_owed_report_if_delivered = _expiry.taskq_clear_owed_report_if_delivered
    taskq_clear_owed_reports = _expiry.taskq_clear_owed_reports
    taskq_replay_owed_expiries_async = _expiry.taskq_replay_owed_expiries_async
    _owed_replay_wanted = _expiry._owed_replay_wanted
    taskq_schedule_owed_replay = _expiry.taskq_schedule_owed_replay
    # From ``taskq_bridge_reads``: reads of accepted work that has no run yet.
    _overflow_query = _reads._overflow_query
    taskq_overflow = _reads.taskq_overflow
    taskq_overflow_async = _reads.taskq_overflow_async
    taskq_overflow_or_none_async = _reads.taskq_overflow_or_none_async
    taskq_chip_excluded_ids = _reads.taskq_chip_excluded_ids
    taskq_chip_overflow_async = _reads.taskq_chip_overflow_async
    taskq_pending_ids_for = _reads.taskq_pending_ids_for
    taskq_pending_ids_for_async = _reads.taskq_pending_ids_for_async
    _retained_claim_ids_for = _reads._retained_claim_ids_for
    _retained_starting_ids = staticmethod(_reads._retained_starting_ids)
    _live_run_ids = _reads._live_run_ids
    taskq_queued_run_async = _reads.taskq_queued_run_async
    taskq_queued_runs_async = _reads.taskq_queued_runs_async
    _queued_run_from_row = _reads._queued_run_from_row
    _queued_run = _reads._queued_run
    taskq_batch_pending = _reads.taskq_batch_pending
    taskq_batch_pending_async = _reads.taskq_batch_pending_async
    # From ``taskq_bridge_window``: the bounded dispatch window and its refill.
    taskq_should_window_async = _window.taskq_should_window_async
    taskq_should_window = _window.taskq_should_window
    taskq_refill_window = _window.taskq_refill_window
    taskq_refill_window_async = _window.taskq_refill_window_async
    _refill_absent = _window._refill_absent
    _refill_make_room = _window._refill_make_room
    _refill_fetch = _window._refill_fetch
    _refill_fetch_retiring = _window._refill_fetch_retiring
    _retire_legacy_stage_rows = staticmethod(_window._retire_legacy_stage_rows)
    _report_retired_stage_rows = _window._report_retired_stage_rows
    _refill_apply = _window._refill_apply
    _refill_idle_wake_read = _window._refill_idle_wake_read
    _refill_schedule_wake = _window._refill_schedule_wake
    _window_entry = staticmethod(_window._window_entry)
    _evict_for_lanes = _window._evict_for_lanes
    lane_refill_scheduler = _window.lane_refill_scheduler
    # From ``taskq_bridge_cancel``: cancelling persisted rows that have not started.
    taskq_cancel_queued_async = _cancel.taskq_cancel_queued_async
    taskq_row_is_claimed_unstarted_async = _cancel.taskq_row_is_claimed_unstarted_async
    taskq_cancel_queued = _cancel.taskq_cancel_queued
    taskq_cancel_queued_each = _cancel.taskq_cancel_queued_each
    taskq_post_cancel_queued = _cancel.taskq_post_cancel_queued
