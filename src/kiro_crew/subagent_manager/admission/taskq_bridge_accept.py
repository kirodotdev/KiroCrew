"""A waiting spawn's row: its accept, and each defer the gate writes for it.

A spawn is persisted BEFORE its id is returned: ``taskq_accept`` on the sync path,
``taskq_build_record`` + ``taskq_accept_record`` through ``TaskStore.run`` from ``spawn_async``.
A row the gate defers -- at accept, or when it re-checks a drained row -- waits out the admit
wait, and the pump is woken then; the defer is written inline (``taskq_defer``), posted to the
writer thread (``taskq_defer_posted``), or handed to the coroutine dispatcher to write off-loop
(``park_defer`` / ``finish_parked_defer``).

A function here whose first parameter is ``self`` is a ``_TaskqBridgeMixin`` method: the class
binds it under the same name in ``admission/taskq_bridge.py``, so it reaches the manager through
``self._manager`` and its sibling members through ``self``, and a class-level patch of a member
reaches every call made through ``self`` or the class.

The manager fields this module uses. Writes: ``_parked_defers``, ``_pending_defers``. Reads:
``_cancellation``.
"""

from __future__ import annotations

import asyncio as _asyncio
import logging as _logging
from typing import TYPE_CHECKING, Any, Mapping

from .types import WINDOW_ENTRY_RECOVERING, DeferPoint

if TYPE_CHECKING:
    # Typing only. ``taskq`` loads on first use, as everywhere in the bridge, and the
    # facade imports this module at run time.
    from kiro_crew import taskq as _taskq

    from ...subagent import SubagentInfo
    from .taskq_bridge import _TaskqBridgeMixin

_glue_logger = _logging.getLogger("kiro_crew.subagent_manager.admission")


def taskq_accept(
    self: _TaskqBridgeMixin,
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
    self: _TaskqBridgeMixin,
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


def taskq_accept_record(self: _TaskqBridgeMixin, record: "_taskq.TaskRecord") -> str | None:
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


def taskq_defer(self: _TaskqBridgeMixin, agent_id: str, *, reason: str) -> bool:
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


def taskq_defer_posted(self: _TaskqBridgeMixin, agent_id: str, *, reason: str) -> None:
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


async def await_pending_defer(self: _TaskqBridgeMixin, agent_id: str) -> None:
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
    self: _TaskqBridgeMixin,
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


async def finish_parked_defer(self: _TaskqBridgeMixin, info: "SubagentInfo") -> "SubagentInfo":
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
