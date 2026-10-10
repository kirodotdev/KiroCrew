"""Durable task queue glue: store open/accept/defer/settle, the bounded window and its refill, off-loop store writes."""

from __future__ import annotations

import asyncio as _asyncio
import functools as _functools
import logging as _logging
import time as _time
from typing import TYPE_CHECKING, Any, Collection, Mapping, Sequence

from kiro_crew.subagent_wait_reasons import (
    QUEUED_WAIT_EXPIRED_TEXT,
    RESUMING_AFTER_RESTART,
    RESUMING_RETRY,
)

from .._component import ManagerComponent
from .types import (
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
#: replacement session. The queue-depth chip does not read this answer: its
#: reader, :meth:`_TaskqBridgeMixin.taskq_chip_overflow_async`, publishes nothing at
#: all while the store cannot be read.
UNKNOWN_PENDING = 1

#: How far past its wake a deferred row may be before an empty refill pass is
#: reported as stuck (a row that came due during the pass is ordinary), and how
#: often that report may repeat.
_OVERDUE_WAKE_GRACE_SECS = 1.0
_OVERDUE_WAKE_WARN_EVERY_SECS = 60.0

#: Most store rows one queued listing returns. A listing is a page for a reader,
#: not an inventory a caller acts on, so the oldest this many are enough; the
#: Stop-all cascade has its own unbounded read (``taskq_pending_ids_for``). A
#: listing that stopped here says so (``QueuedRunListing.partial``), since a
#: cut-off tail otherwise reads as spawns that were never accepted.
QUEUED_LISTING_CAP = 100

#: Owed expiry reports one replay read returns. The replay pages until a read
#: comes back short, so this bounds one writer-thread call, not the replay.
_OWED_REPLAY_PAGE = 256

#: The events that close a deferral: a claim or a state change after the
#: ``deferred`` event means the sentence does not describe the row's current wait.
_DEFER_CLOSERS = ("claimed", "transition")


def _defer_details(store: "_taskq.TaskStore", recs: "list[_taskq.TaskRecord]") -> dict[str, str]:
    """Each still-parked row's deferral sentence, from ONE batched event read.

    A row's sentence is reported only while the deferral is in force
    (``next_run_at`` in the future) AND its ``deferred`` event is newer than
    the row's last claim or transition: a ``recovering`` backoff or a
    dependency park also sets ``next_run_at``, and an older memory-gate
    sentence does not describe either wait.
    """
    now = store.now()
    parked = [r.id for r in recs if r.next_run_at is not None and r.next_run_at > now]
    latest = store.latest_events(parked, ("deferred", *_DEFER_CLOSERS))
    out: dict[str, str] = {}
    for rid in parked:
        deferred = latest.get((rid, "deferred"))
        if deferred is None:
            continue
        closed_at = max(
            (latest[(rid, k)].seq for k in _DEFER_CLOSERS if (rid, k) in latest), default=-1
        )
        if deferred.seq > closed_at:
            out[rid] = str(deferred.data.get("reason") or "")
    return out


def _read_queued_row(
    store: "_taskq.TaskStore", agent_id: str
) -> "tuple[_taskq.TaskRecord, str] | None":
    """Store read for the by-id lookup: the row when it is accepted, not started."""
    # Lazy, as everywhere in this module: the bridge loads taskq on first use.
    from kiro_crew import taskq as _taskq

    try:
        rec = store.get(agent_id)
    except ValueError as exc:
        # A row this build cannot model (a state, kind or class written by a
        # newer one): unreadable, the outage answer, never an escaped 500.
        _glue_logger.warning("taskq: row %s is unreadable by this build: %s", agent_id, exc)
        raise _taskq.TaskStoreUnavailable(f"row {agent_id} unreadable") from exc
    if rec is None or rec.kind != _taskq.KIND_SUBAGENT:
        return None
    if rec.state not in _taskq.CLAIMABLE | {_taskq.ADMITTED}:
        return None
    return rec, _defer_details(store, [rec]).get(rec.id, "")


def _read_queued_rows(
    store: "_taskq.TaskStore",
    *,
    session_key: str | None,
    app: str | None,
    exclude_ids: list[str],
    registered_ids: tuple[str, ...] = (),
) -> "tuple[list[tuple[_taskq.TaskRecord, str]], bool]":
    """Store read for :meth:`_TaskqBridgeMixin.taskq_queued_runs_async`.

    Runs on the writer thread. Returns the rows and whether the listing stopped
    at :data:`QUEUED_LISTING_CAP` with more rows left: one row past the cap is
    read to tell the two apart. *app* narrows the read in SQL, before the cap,
    so an app's page is its own rows and the flag says nothing about others'.
    """
    # Lazy, as everywhere in this module: the bridge loads taskq on first use.
    from kiro_crew import taskq as _taskq

    try:
        recs = store.list_pending(
            _taskq.KIND_SUBAGENT,
            session_key=session_key,
            exclude_ids=exclude_ids,
            limit=QUEUED_LISTING_CAP + 1,
            include_admitted=True,
            exclude_admitted_ids=registered_ids,
            app=app,
        )
    except ValueError as exc:
        _glue_logger.warning("taskq: a queued row is unreadable by this build: %s", exc)
        raise _taskq.TaskStoreUnavailable("queued rows unreadable") from exc
    truncated = len(recs) > QUEUED_LISTING_CAP
    recs = recs[:QUEUED_LISTING_CAP]
    details = _defer_details(store, recs)
    return [(rec, details.get(rec.id, "")) for rec in recs], truncated


if TYPE_CHECKING:
    # Lazy, as everywhere in this module: the bridge loads taskq on first use.
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

        def lane_priorities(self) -> dict[str, int]: ...

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
        with, so the row and the parent's notice agree.
        """
        from kiro_crew import taskq as _taskq

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
            return _taskq.DONE if result_is_whole(_read_state_at(folder) or {}) else None
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
        #
        # ``_parent_spawn_policy`` is a read of the parent agent spec at THIS
        # request's moment. No queue keeps it: every drain re-reads it -- the
        # durable pump off-loop before every re-check, the in-memory synchronous
        # drain through the gate's memo-pinned inline read -- so the row never
        # carries a snapshot that an edited spec would leave stale across a
        # restart.
        #
        # ``WINDOW_ENTRY_RECOVERING`` is window bookkeeping read off the row's
        # state at hydration (``_window_entry``), never a fact a row carries.
        _PROCESS_LOCAL_PARAMS = (
            "_agent_prevalidated",
            "approval_mode",
            "_parent_spawn_policy",
            WINDOW_ENTRY_RECOVERING,
        )
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
        # Before the write, so no parent-end sweep can read the row unrecorded.
        self._manager._cancellation.note_teardown_store_accept(record.session_key, record.id)
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
            ok = store.defer(agent_id, wait=wait, reason=reason)
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
            store, f"defer {agent_id}", store.defer, agent_id, wait=wait, reason=reason
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
        wait: "Mapping[str, Any] | None" = None,
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
            wait=dict(wait or {}),
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

        A write the store could not take at all (``TaskStoreUnavailable``) gave
        neither answer, so the row is answered as still queued and re-checked by
        the next pass: it is almost certainly a stored row still QUEUED and due,
        and a refusal now would be followed by the pump starting it. A
        storeless entry cannot reach here while a store is attached, because
        spawns are refused when the queue is on and its store is not.

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
                if live.defer(point.agent_id, wait=wait, reason=point.reason):
                    return True, None
                return False, live.get(point.agent_id)

            try:
                ok, existing = await store.run(_defer_then_read, store)
            except _taskq.TaskStoreUnavailable:
                # Neither answer is known: the row may well still be QUEUED and
                # due, and a refusal announced now would be followed by the
                # pump starting it, one id with two terminal events. So it is
                # answered as still queued, unlabelled, and re-checked by the
                # next pass, which the timer below arms.
                _glue_logger.warning(
                    "taskq: defer of %s failed; left queued for the next pass",
                    point.agent_id,
                    exc_info=True,
                )
                try:
                    _asyncio.get_event_loop().call_later(wait, self._manager._drain_queue)
                except RuntimeError:
                    pass
                return point.queued
        if not ok:
            if existing is not None:
                return _replace(point.queued, done=True, user_stopped=True)
            return self._manager._announce_rejection(point.refused)
        try:
            _asyncio.get_event_loop().call_later(wait, self._manager._drain_queue)
        except RuntimeError:
            pass
        # The defer is written: publish the gate's label with the depth. A
        # refused row (``not ok`` above) publishes nothing, so no label outlives
        # a row that never waited.
        self._manager._emit_queue_depth(
            point.parent_session_key, point.batch_id, wait=dict(point.wait) or None
        )
        return point.queued

    # ── the max wait of a memory deferral (``agent.subagent_queue_max_wait_secs``) ──
    #
    # Every re-park above writes ``now + admit_wait``, so on its own a deferral
    # renews itself for as long as the host stays short. The bound ends it: a row
    # parked by the memory floor that has spent the bound
    # PARKED (``TaskStore.deferred_longer_than``) is failed in the store and
    # reported to its parent as ``QUEUED_WAIT_EXPIRED_TEXT``. The pump runs the
    # sweep at the END of a pass, after the pass has re-parked what it
    # re-checked, so a row whose re-check still found the host short is caught in
    # the same pass. A row whose deferral merely lapsed waits for a slot, not for
    # memory: it is not parked, so it is left alone, and the time it spends that
    # way never counts toward the bound once it is parked again.

    def taskq_memory_wait_bound_secs(self) -> float:
        """The live bound, in seconds; 0 means no bound.

        One reader for both memory waits: this sweep's store deferrals and the
        macOS kernel memory-pressure hold (``_memory_pressure_holds``), which
        holds non-durable starts too, so it is the manager's value, not the store's.
        """
        return float(getattr(self._manager, "_subagent_queue_max_wait_secs", 0) or 0)

    def _memory_wait_exclusions(self) -> list[str]:
        """Rows the sweep must not end: a live run, an accept in flight, a pop in flight.

        Read on the loop, which owns these sets. Window rows are NOT excluded: a
        row there is still only queued, and ending it drops its entry too.
        """
        admitting = list(getattr(self._manager, "_admitting_ids", ()) or ())
        dispatching = list(getattr(self._manager, "_dispatching_ids", ()) or ())
        return [*self._live_run_ids(), *admitting, *dispatching]

    @staticmethod
    def _expire_memory_waits_db(
        store: "_taskq.TaskStore",
        bound: float,
        exclude_ids: list[str],
    ) -> "list[_taskq.TaskRecord]":
        """Store half of the sweep (writer thread): fail each row past the bound.

        The read and every write share this one writer-thread call, and each
        write is fenced by the generation the read returned, so a row a claim
        took in between is refused rather than failed under its new owner. Each
        failure is written as owing its report (``report_owed``), cleared only
        once the report has reached the parent (:meth:`_clear_owed_report_after`),
        so a process lost in between, or a report that never got through, leaves
        the next one to make it (:meth:`taskq_replay_owed_expiries_async`).
        Returns the rows whose failure landed.

        Each ``finish`` commits on its own, so a refusal after the first one
        never discards what already landed: this process owes those rows their
        report, and no later read names them (the sweep's read wants a waiting
        row, the replay another incarnation's). A refused ``finish`` ends the
        loop and leaves that row and the rest queued for the next sweep. Only
        the read may raise, before anything is written.
        """
        from kiro_crew import taskq as _taskq

        ended: list[_taskq.TaskRecord] = []
        for rec in store.deferred_longer_than(_taskq.KIND_SUBAGENT, bound, exclude_ids=exclude_ids):
            try:
                landed = store.finish(
                    rec.id,
                    _taskq.FAILED,
                    generation=rec.generation,
                    error=QUEUED_WAIT_EXPIRED_TEXT,
                    report_owed=True,
                )
            except _taskq.TaskStoreUnavailable:
                _glue_logger.debug(
                    "taskq: memory-wait expiry of %s refused; left to the next sweep",
                    rec.id,
                    exc_info=True,
                )
                break
            if landed:
                ended.append(rec)
        return ended

    def _sweep_inputs(self) -> "tuple[float, list[str]] | None":
        """The bound and the exclusions; None for no sweep (no bound)."""
        bound = self.taskq_memory_wait_bound_secs()
        if bound <= 0:
            return None
        return bound, self._memory_wait_exclusions()

    def taskq_expire_memory_waits(self) -> int:
        """End every memory-deferred row past the bound; how many ended (inline twin).

        For the inline pump (no off-loop pump). Without a running loop nothing is
        ended, since the terminal report it owes needs one.
        """
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        inputs = self._sweep_inputs()
        if store is None or inputs is None:
            return 0
        try:
            _asyncio.get_running_loop()
        except RuntimeError:
            return 0
        try:
            ended = self._expire_memory_waits_db(store, *inputs)
        except _taskq.TaskStoreUnavailable:
            _glue_logger.debug("taskq: memory-wait expiry failed", exc_info=True)
            return 0
        self._report_memory_waits_expired(ended, inputs[0])
        return len(ended)

    async def taskq_expire_memory_waits_async(self) -> int:
        """:meth:`taskq_expire_memory_waits` with its store half on the writer thread."""
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        inputs = self._sweep_inputs()
        if store is None or inputs is None:
            return 0
        try:
            ended = await store.run(self._expire_memory_waits_db, store, *inputs)
        except _taskq.TaskStoreUnavailable:
            _glue_logger.debug("taskq: memory-wait expiry failed", exc_info=True)
            return 0
        self._report_memory_waits_expired(ended, inputs[0])
        return len(ended)

    def _report_memory_waits_expired(
        self, ended: "list[_taskq.TaskRecord]", bound: float, *, replay: bool = False
    ) -> None:
        """Loop half: drop each ended row's window entry and report it.

        The report is the queued-stop report with the expiry as its ``error``, so
        batch accounting, the digest, a waiting parent's wake and the re-published
        queued depth take the path a stopped queued row already takes. A row
        ended while a teardown of its parent is open, and accepted before that
        teardown's snapshot (``accepted_before_open_teardown``), is reported to
        the dashboard but never injected: the injector creates a session when
        none is live, which would rebuild the conversation that ended. A
        successor's own rows under the same key are reported as usual. Once
        the teardown's store sweep is done, it has stopped every such row.

        Once a report has reached the parent, the row's owed report is cleared
        in the store (:meth:`_clear_owed_report_after`); a report cancelled with
        its process (the shutdown drain's stragglers) stays owed, for the next
        start. *replay* is that start.
        """
        manager = self._manager
        for rec in ended:
            params = dict(rec.params)
            parent = rec.session_key or str(params.get("parent_session_key") or "")
            params["_preassigned_id"] = rec.id
            params["parent_session_key"] = parent
            if replay:
                _glue_logger.warning(
                    "taskq: reporting subagent %s's memory-wait expiry, which an earlier "
                    "process wrote and did not report (%s)",
                    rec.id,
                    QUEUED_WAIT_EXPIRED_TEXT,
                )
            else:
                _glue_logger.warning(
                    "taskq: subagent %s waited for memory longer than %.0fs; ending it (%s)",
                    rec.id,
                    bound,
                    QUEUED_WAIT_EXPIRED_TEXT,
                )
            if manager._cancellation.accepted_before_open_teardown(parent, rec.id):
                manager._teardown_cancelled_ids.add(rec.id)
            entry = manager._unqueue(rec.id, stored=params, store_cancelled=True)
            # The sweep already wrote ``failed``: that is the row's terminal
            # write, so the report's settle writes no second one.
            report = manager._report_queued_stop(
                entry or params,
                error=QUEUED_WAIT_EXPIRED_TEXT,
                report_owed=True,
                row_settled=True,
            )
            self._clear_owed_report_after(rec.id, report)

    def _clear_owed_report_after(self, agent_id: str, report: "_asyncio.Task[Any] | None") -> None:
        """Clear *agent_id*'s owed report once *report* has reached the parent.

        At once with no report: no task means no report will run here (an empty
        id, or a finalize claim another path already holds), so nothing is left
        for this process to owe. A report whose record the gateway PARKED is not
        delivered when its task returns: a wave member held for its digest
        (``_digest_held``) or an announce queued behind a busy slot
        (``_delivery_queued``) is only in this process's memory until that
        digest or that turn reaches the parent. The gateway hands the debt on
        with it (``SubagentDelivery.report_owed``), and whichever settle
        delivers it clears the mark (:meth:`taskq_clear_owed_reports`), so a
        restart in between leaves it owed for the replay. A report that did not
        reach the parent stays owed too: one that returned False (the injection
        timed out, or ``_on_done`` raised), raised, or was cancelled with its
        process, and one whose task returned True although the gateway gave up
        on the injection (``_report_undelivered``: a channel or cron parent's
        attempts all failed, and the gateway swallowed that). The next start
        replays it.
        """
        info = self._manager._report_owners.get(report) if report is not None else None

        def _clear(task: "_asyncio.Task[Any] | None" = None) -> None:
            if task is not None and (
                task.cancelled() or task.exception() is not None or task.result() is not True
            ):
                return
            if info is None:
                self.taskq_clear_owed_reports([agent_id])
            else:
                self.taskq_clear_owed_report_if_delivered(info)

        if report is None:
            _clear()
        else:
            report.add_done_callback(_clear)

    def taskq_clear_owed_report_if_delivered(self, info: Any) -> None:
        """Clear *info*'s owed report if the report task that just returned True delivered it.

        Not when the gateway parked it (``_digest_held``, ``_delivery_queued``),
        whose settle clears it on delivery, nor when it gave up on the injection
        (``_report_undelivered``). A record that owes nothing is left alone.
        """
        if not info._report_owed:
            return
        if info._digest_held or info._delivery_queued or info._report_undelivered:
            return
        self.taskq_clear_owed_reports([info.id])

    def taskq_clear_owed_reports(self, agent_ids: "Collection[str]") -> None:
        """Clear the store's owed report of each of *agent_ids*: it reached the parent."""
        store = self.taskq_store()
        if store is None:
            return
        for agent_id in agent_ids:
            self._post_store_write(
                store, f"report cleared {agent_id}", store.mark_reported, agent_id
            )

    async def taskq_replay_owed_expiries_async(self) -> int:
        """Report the memory-wait expiries an earlier process wrote and never reported.

        After the store is attached, until one replay has read every owed row:
        the rows ``TaskStore.owed_reports`` names (another incarnation's, so none
        of this process's in-flight reports), a page at a time. Each is reported
        exactly as a live expiry is, and cleared only once that report has
        reached the parent (:meth:`_clear_owed_report_after`). The
        replay is marked done only once a page comes back short; a read the
        store refuses leaves it to the next call (the reaper sweep), which
        resumes after the last page reported, so no row is reported twice by
        this process. A replayed row of a parent that ended before the restart
        is reported to that key like any restored row. Nothing is replayed while the gateway
        holds dispatch for its memory barrier (``_queue_dispatch_held``): a
        report injected then fails on ``MemoryStartupUnavailable``, which the
        injector swallows, so the report would look run and its owed mark would
        be cleared undelivered. ``release_queue_dispatch`` schedules it, and
        each reaper sweep retries it. Returns the rows reported by this call.
        """
        from kiro_crew import taskq as _taskq

        manager = self._manager
        store = self.taskq_store()
        if store is None or not self._owed_replay_wanted():
            return 0
        setattr(manager, "_taskq_owed_replaying", True)
        reported = 0
        try:
            while True:
                after = getattr(manager, "_taskq_owed_replay_after", None)
                try:
                    page = await store.run(
                        store.owed_reports,
                        _taskq.KIND_SUBAGENT,
                        limit=_OWED_REPLAY_PAGE,
                        after=after,
                    )
                except _taskq.TaskStoreUnavailable:
                    failures = int(getattr(manager, "_taskq_owed_replay_failures", 0)) + 1
                    setattr(manager, "_taskq_owed_replay_failures", failures)
                    # One line per outage at warning; the sweep's retries at debug.
                    _glue_logger.log(
                        _logging.WARNING if failures <= 1 else _logging.DEBUG,
                        "taskq: owed expiry reports could not be read; retrying next sweep",
                        exc_info=True,
                    )
                    return reported
                expired = [rec for rec in page if rec.state == _taskq.FAILED]
                self._report_memory_waits_expired(
                    expired, self.taskq_memory_wait_bound_secs(), replay=True
                )
                reported += len(expired)
                if len(page) < _OWED_REPLAY_PAGE:
                    setattr(manager, "_taskq_owed_replayed", True)
                    return reported
                setattr(manager, "_taskq_owed_replay_after", (page[-1].updated_at, page[-1].id))
        finally:
            setattr(manager, "_taskq_owed_replaying", False)

    def _owed_replay_wanted(self) -> bool:
        """True while an owed-report replay is still owed and may run now.

        Not once one replay has read every owed row, not while one is in
        flight, and not while dispatch is held for the memory barrier.
        """
        manager = self._manager
        return not (
            getattr(manager, "_taskq_owed_replayed", False)
            or getattr(manager, "_taskq_owed_replaying", False)
            or getattr(manager, "_queue_dispatch_held", False)
        )

    def taskq_schedule_owed_replay(self) -> None:
        """Post :meth:`taskq_replay_owed_expiries_async` on the running loop, if any.

        Called at every store attach (``taskq_boot_dispatch``, ``_initialize_taskq``),
        at ``release_queue_dispatch``, and from each reaper sweep, which retries a
        replay the store refused or the dispatch hold put off.
        """
        if self.taskq_store() is None or not self._owed_replay_wanted():
            return
        try:
            loop = _asyncio.get_running_loop()
        except RuntimeError:
            return
        self.track_store_task(loop.create_task(self.taskq_replay_owed_expiries_async()))

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

    def _overflow_query(
        self, parent_session_key: str | None, exclude_ids: list[str] | None = None
    ) -> dict[str, Any]:
        """The ``count_pending`` arguments every overflow entry takes (the
        "accepted, no run yet" definition), snapshotted from manager state on the
        caller's thread (the loop, for the async ones). *exclude_ids* defaults to
        :meth:`taskq_excluded_ids`; the chip passes its own set, and also leaves
        out ``recovering`` rows (:meth:`taskq_chip_overflow_async`)."""
        return {
            "exclude_ids": self.taskq_excluded_ids() if exclude_ids is None else exclude_ids,
            "session_key": parent_session_key,
            "include_admitted": True,
            "exclude_admitted_ids": list(self._manager._agents),
        }

    def taskq_overflow(self, parent_session_key: str | None = None) -> int:
        """Accepted rows with no run that live only in the store (outside the window).

        Claimable rows and ``admitted`` ones nothing registered: a claim the
        pump awaits, or one retained across a store outage
        (``_retained_claims``), is this parent's accepted work exactly like a
        queued row, and no other probe would count it.

        A store that cannot be read answers :data:`UNKNOWN_PENDING`, not 0: no
        store at all is a queue with no rows in it, while a locked or full one is
        a queue whose rows nobody can see, and only the first of those is
        evidence that this parent has nothing waiting.

        A row the pump has popped and not yet claimed is counted: it is still
        this parent's accepted work, which is what the guard that holds a
        parent's reset while its children wait asks about. The chip's reading
        leaves it out (:meth:`taskq_chip_overflow_async`).
        """
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None:
            return 0
        try:
            return store.count_pending(
                _taskq.KIND_SUBAGENT, **self._overflow_query(parent_session_key)
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
        count = await self.taskq_overflow_or_none_async(parent_session_key)
        return UNKNOWN_PENDING if count is None else count

    async def taskq_overflow_or_none_async(
        self, parent_session_key: str | None = None
    ) -> int | None:
        """:meth:`taskq_overflow_async`, answering None for a store nobody could read.

        For the one reader that must tell "children are waiting" from "nobody
        could look" -- the synthesis fire gate, which re-checks on an outage
        rather than waiting for a completion that may never come.
        """
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None:
            return 0
        try:
            return int(
                await store.run(
                    store.count_pending,
                    _taskq.KIND_SUBAGENT,
                    **self._overflow_query(parent_session_key),
                )
            )
        except _taskq.TaskStoreUnavailable:
            _glue_logger.warning(
                "taskq: queued count for %s unreadable; answering some",
                parent_session_key or "<any parent>",
                exc_info=True,
            )
            return None

    def taskq_chip_excluded_ids(self) -> list[str]:
        """The rows the queue-depth chip leaves out of its store count.

        The refill's never-claim set (:meth:`taskq_dispatch_excluded_ids`) with
        one difference: a row a ``spawn_async`` caller is still admitting is
        left out only until the gate has queued it (``_admitting_waiting``).
        The refill must never claim such a row, but once it is deferred or
        behind the cap it IS waiting, and the gate's own labelled request for
        it must count it -- leaving it out would publish 0 for a wave whose
        rows all wait, and drop the label with it.
        """
        return self.taskq_dispatch_excluded_ids(counted=self._manager._admitting_waiting)

    async def taskq_chip_overflow_async(self, parent_session_key: str) -> int | None:
        """The store half of the queue-depth chip's count, or ``None`` when the
        store cannot say.

        The chip's one reader, excluding :meth:`taskq_chip_excluded_ids`. Like
        :meth:`taskq_overflow` it counts ``admitted`` rows no run is registered
        for (a claim retained across an outage is still waiting work). Unlike
        it, a ``recovering`` row is not counted: it is a run that had started
        before its owner was lost (a gateway restart), waiting to be rebuilt,
        not a spawn waiting to start -- the queued listing names it "waiting to
        resume", and counting it would put "N waiting to start" on the card
        after every restart. Work still owed (the pending-work guards) keeps it. The
        exclusion sets are snapshotted here, on the loop, before the read; on
        the inline pump (``pump_off_loop`` off) the read itself runs here too.

        An unreadable store answers ``None``, not :data:`UNKNOWN_PENDING`: the
        chip then publishes nothing, because any number it sent would be a
        guess. ``TaskStoreUnavailable`` is the outage and is logged at DEBUG
        (the caller retries); anything else is a defect and is logged as one.
        """
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None:
            return 0
        try:
            count = _functools.partial(
                store.count_pending,
                _taskq.KIND_SUBAGENT,
                **self._overflow_query(parent_session_key, self.taskq_chip_excluded_ids()),
                include_recovering=False,
            )
            return int(await store.run(count) if type(self).pump_off_loop else count())
        except _taskq.TaskStoreUnavailable:
            _glue_logger.debug(
                "taskq: queue depth for %s unreadable", parent_session_key, exc_info=True
            )
        except Exception:
            _glue_logger.warning(
                "taskq: queue depth for %s failed", parent_session_key, exc_info=True
            )
        return None

    def taskq_pending_ids_for(self, parent_session_key: str) -> list[str]:
        """Ids of this parent's waiting rows that are NOT in the in-memory window.

        Waiting means accepted with no run yet, the definition the overflow
        count uses: claimable rows, and ``admitted`` rows nothing has
        registered -- a claim the pump is still awaiting, or a retained one.
        Stop all cancels exactly these ids, and the claimer's post-claim
        re-read refuses a row cancelled here, so a claimed row never starts
        after the stop.
        """
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None or not parent_session_key:
            return []
        try:
            rows = store.list_pending(
                _taskq.KIND_SUBAGENT,
                session_key=parent_session_key,
                exclude_ids=self.taskq_excluded_ids(),
                include_admitted=True,
                exclude_admitted_ids=list(self._manager._agents),
            )
        except _taskq.TaskStoreUnavailable:
            return []
        return [r.id for r in rows]

    async def taskq_pending_ids_for_async(
        self, parent_session_key: str, *, include_window: bool = False
    ) -> list[str]:
        """:meth:`taskq_pending_ids_for` with its store read on the writer thread.

        *include_window* is the parent-end teardown's reading
        (``cancel_for_teardown_impl``): the window's rows too. The teardown
        unqueues a window entry together with its row, and an entry the refill
        hydrated after the teardown's snapshot was named by nobody; the
        snapshot's fence, not the window, is what tells a retired
        conversation's row from one its successor under the same key queued.
        That reading leaves ``admitted`` rows out: the teardown refuses a
        claimed-not-started row (``allow_admitted=False``), so naming one would
        only mark it in the delivery gate and the sweep's audit as stopped.

        A refused read is the reading's to decide. The teardown's reading lets
        :class:`TaskStoreUnavailable` propagate: "nothing to stop" would release
        the fence over rows nobody stopped, so the teardown keeps the fence and
        owes the sweep instead. The default reading (``cancel_for_parent``)
        answers ``[]``.
        """
        from kiro_crew import taskq as _taskq

        store = self.taskq_store()
        if store is None or not parent_session_key:
            return []
        exclude = self.taskq_excluded_ids(window=not include_window)
        registered = list(self._manager._agents)
        try:
            rows = await store.run(
                store.list_pending,
                _taskq.KIND_SUBAGENT,
                session_key=parent_session_key,
                exclude_ids=exclude,
                include_admitted=not include_window,
                exclude_admitted_ids=registered,
            )
        except _taskq.TaskStoreUnavailable:
            if include_window:
                raise
            return []
        return [r.id for r in rows]

    def _live_run_ids(self) -> list[str]:
        """Ids with a live run in this process: the registry answers for them.

        The ONE spelling of "a run exists", shared by the refill's exclusions
        and the queued reads, so "accepted, no run yet" means one thing.
        """
        # Copy before filtering: the refill runs this on the store's writer
        # thread while the loop registers and retires runs.
        return [aid for aid, info in list(self._manager._agents.items()) if not info.done]

    async def taskq_queued_run_async(self, agent_id: str) -> QueuedRun | None:
        """The accepted spawn *agent_id* when no run exists for it yet, else None.

        The store row may be claimable or ``admitted``: a claim the pump is
        awaiting (or a retained one) has left ``queued`` but is not registered,
        and answering "not found" for it is the gap this read closes. A
        registered id is never answered here; the registry answers it. An
        unreadable store answers from the window, and when the window has no
        such entry raises :class:`QueuedReadUnavailable`: "not
        queued" is not knowable then, and a caller must not read it as gone.
        """
        from kiro_crew import taskq as _taskq

        manager = self._manager
        if agent_id in manager._agents:
            return None
        store = self.taskq_store()
        outage: Exception | None = None
        if store is not None:
            try:
                found = await store.run(_read_queued_row, store, agent_id)
            except _taskq.TaskStoreUnavailable as exc:
                _glue_logger.warning(
                    "taskq: queued row %s unreadable; answering from the window",
                    agent_id,
                    exc_info=True,
                )
                found, outage = None, exc
            if found is not None and agent_id not in manager._agents:
                rec, detail = found
                return self._queued_run_from_row(rec, detail)
        for params in list(manager._queue):
            if str(params.get("_preassigned_id") or "") == agent_id and not params.get(
                "_resume_id"
            ):
                return self._queued_run(
                    agent_id, params, str(params.get("parent_session_key") or ""), 0.0, ""
                )
        if outage is not None and agent_id not in manager._agents:
            raise QueuedReadUnavailable(str(outage)) from outage
        return None

    async def taskq_queued_runs_async(
        self, parent_session_key: str | None = None, *, app: str | None = None
    ) -> QueuedRunListing:
        """Accepted spawns no run exists for yet, oldest first.

        Both halves of the queue are read: the dispatch window (``_queue``) and
        the store's rows, which include every gate-deferred row, every row
        waiting outside the window and every claimed, unregistered one.
        *parent_session_key* ``None`` means every parent; *app* narrows to one
        owning app inside the store read.

        The listing is ``partial`` when it stopped at :data:`QUEUED_LISTING_CAP`
        or the store could not be read: an outage leaves the window half, and a
        listing cannot answer it with invented rows, but it must not read as
        complete either. The transition into a partial listing is logged once.
        """
        from kiro_crew import taskq as _taskq

        manager = self._manager
        rows: list[tuple[_taskq.TaskRecord, str]] = []
        truncated = unreadable = False
        store = self.taskq_store()
        if store is not None:
            try:
                rows, truncated = await store.run(
                    _read_queued_rows,
                    store,
                    session_key=parent_session_key,
                    app=app,
                    exclude_ids=self._live_run_ids(),
                    registered_ids=tuple(manager._agents),
                )
            except _taskq.TaskStoreUnavailable:
                unreadable = True
        partial = truncated or unreadable
        if partial != manager._queued_listing_partial:
            manager._queued_listing_partial = partial
            if partial:
                _glue_logger.warning(
                    "taskq: queued listing is partial (more than %d store rows, or the "
                    "store is unreadable)",
                    QUEUED_LISTING_CAP,
                )
        out: list[QueuedRun] = []
        seen: set[str] = set()
        live = set(self._live_run_ids())  # re-read: a run may have registered meanwhile
        for rec, detail in rows:
            if rec.id in live or (rec.state == _taskq.ADMITTED and rec.id in manager._agents):
                continue
            out.append(self._queued_run_from_row(rec, detail))
            seen.add(rec.id)
        # Window entries the store did not return: legacy in-memory and
        # restricted work, which has no row, and every entry while the store is
        # unreadable. A ``_resume_id`` entry belongs to a resident run, not to an
        # unstarted one. A page cut at the cap may simply not have reached an
        # entry's row, so a truncated store half adds none.
        for params in [] if truncated else list(manager._queue):
            aid = str(params.get("_preassigned_id") or "")
            if not aid or params.get("_resume_id") or aid in seen or aid in manager._agents:
                continue
            parent = str(params.get("parent_session_key") or "")
            if parent_session_key is not None and parent != parent_session_key:
                continue
            if app is not None and str(params.get("app") or "") != app:
                continue
            out.append(self._queued_run(aid, params, parent, 0.0, ""))
        return QueuedRunListing(tuple(out), partial)

    def _queued_run_from_row(self, rec: "_taskq.TaskRecord", detail: str) -> QueuedRun:
        from kiro_crew import taskq as _taskq

        params = dict(rec.params)
        parent = rec.session_key or str(params.get("parent_session_key") or "")
        resuming = ""
        if rec.state == _taskq.RECOVERING:
            resuming = RESUMING_AFTER_RESTART
        elif rec.state == _taskq.RETRY_WAIT and rec.attempts > 0:
            resuming = RESUMING_RETRY
        return self._queued_run(
            rec.id, params, parent, rec.created_at, "" if resuming else detail, resuming
        )

    def _queued_run(
        self,
        agent_id: str,
        params: Mapping[str, Any],
        parent: str,
        accepted_at: float,
        detail: str,
        resuming: str = "",
    ) -> QueuedRun:
        manager = self._manager
        label = manager._queue_wait.get(parent) or {}
        reason = "" if resuming else str(label.get("reason") or "")
        return QueuedRun(
            id=agent_id,
            task=str(params.get("task") or ""),
            parent_session_key=parent,
            agent=str(params.get("agent") or params.get("crew") or ""),
            app=str(params.get("app") or ""),
            accepted_at=float(accepted_at or 0.0),
            reason=reason,
            reason_detail=detail,
            resuming=resuming,
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
        deferred or leased past now are skipped, and when the window is
        otherwise empty the pump schedules its own wake-up at the earliest
        time a row this pass may claim becomes claimable: the later of its
        ``next_run_at`` and ``lease_expires_at`` (:meth:`TaskStore.next_eligible_at`).

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
            rows, retired, stranded = self._refill_fetch_retiring(
                store, absent, room, want, children_only
            )
            self._report_retired_stage_rows(store, retired, stranded)
            self._refill_apply(rows)
            wake_at = (
                self._refill_idle_wake_read(store, children_only)()
                if not rows and not self._manager._queue
                else None
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
            rows, retired, stranded = await store.run(
                self._refill_fetch_retiring, store, absent, room, want, children_only
            )
            self._report_retired_stage_rows(store, retired, stranded)
            self._refill_apply(rows)
            wake_at = (
                await store.run(self._refill_idle_wake_read(store, children_only))
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
            exclude_ids=self.taskq_dispatch_excluded_ids(),
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

        exclude = self.taskq_dispatch_excluded_ids()
        # The child reserve's own top-up ignores the priority tiers: see
        # :meth:`lane_refill_scheduler`.
        sched = self.lane_refill_scheduler(tiered=not children_only)
        rows: list[_taskq.TaskRecord] = []
        if absent and room > 0:
            rows.extend(
                store.fetch_dispatchable_fair(
                    _taskq.KIND_SUBAGENT,
                    limit=min(room, want),
                    scheduler=sched,
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
                    scheduler=sched,
                    exclude_ids=exclude,
                    children_only=children_only,
                )
            )
        return rows

    def _refill_fetch_retiring(
        self,
        store: "_taskq.TaskStore",
        *fetch_args: Any,
    ) -> tuple[list["_taskq.TaskRecord"], list[dict[str, Any]], bool]:
        """Store phase: :meth:`_refill_fetch`, then retire legacy stage rows.

        One store step, so the off-loop refill keeps its single writer-thread hop.
        """
        return self._retire_legacy_stage_rows(store, self._refill_fetch(store, *fetch_args))

    @staticmethod
    def _retire_legacy_stage_rows(
        store: "_taskq.TaskStore",
        rows: list["_taskq.TaskRecord"],
    ) -> tuple[list["_taskq.TaskRecord"], list[dict[str, Any]], bool]:
        """Store phase: cancel rows a chat Autopilot stage queued, keep the rest.

        Such a row carries ``_stage_boundary_owner``. The stage that owned it is
        gone with the retired Autopilot, so the row is stopped rather than run.
        Returns the rows still to dispatch, the params of each one stopped, and
        whether a store outage left any stage row uncancelled. An outage stops
        the cancelling but still returns every row already cancelled, so each
        committed cancel is reported; the rest stay queued, are never
        dispatched from this pass, and the caller arms a retry pass for them.
        """
        from kiro_crew import taskq as _taskq

        kept: list[_taskq.TaskRecord] = []
        retired: list[dict[str, Any]] = []
        store_down = False
        for rec in rows:
            if not rec.params.get("_stage_boundary_owner"):
                kept.append(rec)
                continue
            if store_down:
                continue
            try:
                previous = store.cancel(
                    rec.id,
                    reason="user_stop",
                    only_from=_taskq.CLAIMABLE,
                    generation=rec.generation,
                )
            except _taskq.TaskStoreUnavailable:
                _glue_logger.warning(
                    "taskq: cancel of retired-stage row %s failed", rec.id, exc_info=True
                )
                store_down = True
                continue
            if previous is not None:
                params = dict(rec.params)
                params["_preassigned_id"] = rec.id
                params["_stop_origin"] = "queued by chat Autopilot, which was removed"
                retired.append(params)
        return kept, retired, store_down

    def _report_retired_stage_rows(
        self,
        store: "_taskq.TaskStore",
        retired: list[dict[str, Any]],
        stranded: bool,
    ) -> None:
        """Loop step: publish each retired stage row as stopped before start.

        A stage row a store outage left queued gets a retry pass of its own:
        the refill that follows may finish without any further store step, and
        nothing else is certain to drain the queue again in this process.
        """
        if stranded:
            self._refill_schedule_wake(store, store.now() + self.taskq_admit_wait_secs())
        for params in retired:
            _glue_logger.info(
                "taskq: %s was queued by chat Autopilot, which was removed; stopped",
                params.get("_preassigned_id"),
            )
            self._manager._report_queued_stop(params)

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
        held_modes = getattr(self._manager, "_held_approval_modes", {})
        floor_deferred = getattr(self._manager, "_floor_deferred_ids", ())
        # A parent with a Stop all in flight gets nothing back in the window.
        # This fetch may have run on the writer thread before the stop's
        # cancels did, so its rows can be ones the stop just cancelled, or this
        # parent's store-only rows, which the stop's pending read leaves out
        # once they are windowed. Left on disk, they are the stop's to cancel.
        stopping = getattr(self._manager, "_stopping_parents", None) or {}
        for rec in rows:
            entry = self._window_entry(rec)
            if rec.id in held_modes:
                # This process accepted the row with that mode and is the one
                # starting it: the same request's consent, not a replay
                # (``spawn_impl`` records it; ``_window_entry`` says why the store
                # never carries it).
                entry["approval_mode"] = held_modes[rec.id]
            if rec.id in floor_deferred:
                # Its last defer was the memory floor's: a floor wait whose admit
                # wait the store already metered (``next_run_at``), so the pick
                # leaves it to the gate, which re-checks the floor before the
                # pressure hold, and no pressure clock starts below the floor.
                entry[MEMORY_WAIT_UNTIL_KEY] = 0.0
            if str(entry.get("parent_session_key") or "") in stopping:
                continue
            if rec.id not in present:
                self._manager._queue.append(entry)

    def _refill_idle_wake_read(
        self, store: "_taskq.TaskStore", children_only: bool
    ) -> "_functools.partial[float | None]":
        """The store read for when the earliest row this pass may claim, and
        could not, becomes claimable -- over the same rows the pass read. Its
        exclusions are taken here, by the caller (on the loop)."""
        from kiro_crew import taskq as _taskq

        return _functools.partial(
            store.next_eligible_at,
            _taskq.KIND_SUBAGENT,
            exclude_ids=self.taskq_dispatch_excluded_ids(),
            children_only=children_only,
        )

    def _refill_schedule_wake(self, store: "_taskq.TaskStore", wake_at: float | None) -> None:
        if wake_at is None:
            return
        due_in = wake_at - store.now()
        if due_in < -_OVERDUE_WAKE_GRACE_SECS:
            # The wake is read over the rows this pass may claim, so a row well
            # past it that the pass still did not take is held by something the
            # read cannot see. A wake re-armed at 0 would re-run this same
            # empty pass on every loop turn for as long as that lasts; the
            # floor below bounds it, and this says so.
            now = _time.monotonic()
            if now - self._manager._overdue_wake_warned_at >= _OVERDUE_WAKE_WARN_EVERY_SECS:
                self._manager._overdue_wake_warned_at = now
                _glue_logger.warning(
                    "taskq: a waiting subagent row is %.0fs past its wake and none is "
                    "claimable; re-checking every %.2fs",
                    -due_in,
                    MIN_RECHECK_DELAY_SECS,
                )
        delay = max(MIN_RECHECK_DELAY_SECS, min(due_in, self.taskq_admit_wait_secs()))
        try:
            _asyncio.get_event_loop().call_later(delay, self._manager._drain_queue)
        except RuntimeError:
            pass

    @staticmethod
    def _window_entry(rec: "_taskq.TaskRecord") -> dict[str, Any]:
        from kiro_crew import taskq as _taskq

        params = dict(rec.params)
        params["_preassigned_id"] = rec.id
        params["_lane"] = rec.lane
        # The row is a run being rebuilt after its owner was lost, so the
        # chip's window half leaves it out as its store half does; ``spawn``
        # takes the mark as its keyword and puts it back on a re-queued entry.
        # Set from the row's state alone, never from its params.
        params.pop(WINDOW_ENTRY_RECOVERING, None)
        if rec.state == _taskq.RECOVERING:
            params[WINDOW_ENTRY_RECOVERING] = True
        params.pop("_legacy_import", None)
        # Every row written before the stage-owner token was removed carries
        # ``_stage_boundary_owner`` (empty for an unowned spawn). ``spawn`` no
        # longer takes that keyword, so it is dropped here; a NON-empty owner
        # never reaches this point, ``_retire_legacy_stage_rows`` stops it first.
        params.pop("_stage_boundary_owner", None)
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

    def lane_refill_scheduler(self, *, tiered: bool = True) -> "_lanes.LaneScheduler":
        """A second balance for the store->window refill, so filling the window
        does not spend the credit the drain picks with.

        ``tiered=False`` clears the tiers on it for this caller, which is how
        the ``children_only`` refill (the child reserve's own top-up) hydrates
        rows: the store reader drives :meth:`LaneScheduler.interleave` itself,
        so the exemption is expressed on the scheduler handed to it rather than
        as an argument. A tier there would hydrate one chat's queued children
        pass after pass and leave another chat's in-flight tree with no head in
        the window for the reserve to start.
        """
        from kiro_crew.taskq import lanes as _lanes

        scheduler = getattr(self._manager, "_lane_refill_scheduler", None)
        settings = self.fairness_settings()
        if not isinstance(scheduler, _lanes.LaneScheduler):
            scheduler = _lanes.LaneScheduler(weights=settings.lane_weights)
            setattr(self._manager, "_lane_refill_scheduler", scheduler)
        else:
            scheduler.weights = settings.lane_weights
        scheduler.priorities = self.lane_priorities() if tiered else {}
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
        work they asked to end; the claimer's post-claim re-read (``claim_and_start``)
        refuses a row cancelled here. A parent-end teardown passes False: a row in that
        window may be carrying a decision a person made (a spawn approval is the visible
        case), which a teardown has no standing to revoke on their behalf. Refusing
        leaves the row to the incarnation that owns it.

        Race-safe against the drain because the STATE TEST AND THE CANCEL SHARE
        ONE TRANSACTION: ``unstarted`` is both the predicate this code judges the
        row it read by and the ``only_from`` the store re-tests inside the cancel,
        so a row a drain claimed and started in between answers None here and the
        caller falls through to the live reap -- never a ``cancelled`` row with a
        running spawn under it. A row claimed but not started (``admitted``) is
        still this path's: the spawn re-reads the state before it registers and
        stops there.

        The cancel is deliberately NOT fenced on the read's generation. A claim, a
        requeue or a wake can bump the generation while the row stays unstarted
        (``queued`` -> ``admitted``, a wait woken into ``retry_wait``); a generation
        fence refused that cancel as ``stale_result`` and dropped the stop, leaving a
        row nothing executes and nothing cancels -- a ``retry_wait`` row is outside
        the boot reconciler's ``ACTIVE`` set, and the parent's queued count keeps it
        attached. ``only_from`` alone is the guarantee the fence was there
        for: whatever generation the row is at, it is cancelled only while unstarted.
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
            previous = store.cancel(agent_id, reason="user_stop", only_from=unstarted)
        except _taskq.TaskStoreUnavailable:
            _glue_logger.warning("taskq: cancel of %s failed", agent_id, exc_info=True)
            return None
        if previous is None:
            return None
        params = dict(rec.params)
        params["_preassigned_id"] = agent_id
        return params

    def taskq_cancel_queued_each(
        self, agent_ids: Sequence[str]
    ) -> dict[str, dict[str, Any] | None | Exception]:
        """:meth:`taskq_cancel_queued` for every id, as ONE call: a bulk stop's store phase.

        Each id keeps its own answer: the params of a row that was cancelled,
        None for one that was not, or the exception its cancel raised. Caught per
        id, so one row's error neither cancels nor skips the rest -- the caller
        reports the rows that were cancelled and then raises the error.
        """
        outcomes: dict[str, dict[str, Any] | None | Exception] = {}
        for agent_id in agent_ids:
            try:
                outcomes[agent_id] = self.taskq_cancel_queued(agent_id)
            except Exception as exc:  # noqa: BLE001 - handed back per row, raised by the caller
                outcomes[agent_id] = exc
        return outcomes

    def taskq_post_cancel_queued(
        self, agent_ids: Sequence[str]
    ) -> "dict[str, dict[str, Any] | None | Exception] | _asyncio.Future[Any]":
        """Queue :meth:`taskq_cancel_queued_each` on the writer thread NOW.

        Returns the future of that one job, or the answers themselves when the
        store is called inline -- no running loop, or the pump on the loop --
        which is :meth:`_post_store_write`'s rule, and for its reason: queued
        here, the cancels land before any refill read or claim queued after this
        call returns, so the caller may drop the rows' window entries before it
        awaits the answer. Inline, the pump's own store calls are inline too and
        are not ordered behind a writer-thread job, so the cancels have to have
        landed before this returns.
        """
        store = self.taskq_store()
        if store is None or not agent_ids:
            return {}
        try:
            _asyncio.get_running_loop()
        except RuntimeError:
            return self.taskq_cancel_queued_each(agent_ids)
        if not type(self).pump_off_loop:
            return self.taskq_cancel_queued_each(agent_ids)
        return store.post(self.taskq_cancel_queued_each, list(agent_ids))
