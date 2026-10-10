"""A refused task-queue row is settled FAILED before its refusal is published.

Every admission-time refusal of a row that already exists in the durable store
-- a drained row whose agent fails validation at dispatch, a row ``spawn_async``
committed before admission closed, a claimed row refused at the gate's commit
point, a row ended under memory pressure -- carries a ``failed`` write the caller
must not get ahead of. A refusal published while that write is still in flight
leaves, if the write is lost to writer-thread contention, a row ADMITTED (or
QUEUED) with no runtime: the one state the next incarnation's ``reconcile_on_boot``
requeues without asking, running work the caller was told was refused. These
tests pin ``taskq_fail``'s settle: the write
is retried until the row reads terminal, a ``persistent`` run's tombstone goes
down first as the verdict the boot probe reads if the store never takes the
write, and every awaiter of the refusal joins the same shielded task.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.subagent import SubagentInfo, SubagentManager

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

ROOT = "dashboard:chat-1"
REFUSAL = "spawn refused: test"


def _sessions(trusted: set[str]) -> MagicMock:
    """A SessionManager stand-in whose trust is scoped to *trusted* keys only."""
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0

    async def _empty_stream(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return
        yield  # noqa: unreachable -- makes this an async generator

    provider.stream = MagicMock(side_effect=lambda *a, **kw: _empty_stream())
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_approval_policy = MagicMock(
        side_effect=lambda key: "auto" if key in trusted else ""
    )
    sessions.has_session = MagicMock(side_effect=lambda key: key in trusted)
    sessions.is_session_sharing_eligible = MagicMock(return_value=False)
    return sessions


def _ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = False
    ctx.hooks.auto_approve_subagent_tools = False
    return ctx


async def _await_joined(task: "asyncio.Task[object]", *, deadline: float = 10.0) -> None:
    """Wait until *task*'s coroutine is SUSPENDED at an await -- it has entered its join.

    An awaiter created with ``create_task`` has not run yet; a ``sleep(n)`` barrier
    assumes it will have reached its shielded join by then (D1). The positive signal
    is the coroutine's own ``cr_await``: set only while it is parked on an inner
    awaitable, so a pending task with it set is inside the join. The ceiling is a
    lost-run bound and RAISES with the task's state.
    """
    loop = asyncio.get_running_loop()
    until = loop.time() + deadline
    while True:
        if task.done():
            raise AssertionError(f"awaiter finished before it was observed joining: {task!r}")
        if task.get_coro().cr_await is not None:  # type: ignore[union-attr]
            return
        if loop.time() >= until:
            raise AssertionError(f"awaiter never reached its join within {deadline}s: {task!r}")
        await asyncio.sleep(0)


async def _await_log(caplog, needle: str, *, deadline: float = 10.0) -> list:
    """Wait until a record containing *needle* is logged; the ceiling is a lost-run bound.

    The signal is the product's own line, never a duration (testing-conventions
    D1): a barrier of ``sleep(n)`` samples a loaded runner mid-flight. Reaching
    the ceiling RAISES quoting what was logged instead, so a hang reads as one.
    """
    loop = asyncio.get_running_loop()
    until = loop.time() + deadline
    while True:
        hits = [r for r in caplog.records if needle in r.getMessage()]
        if hits:
            return hits
        if loop.time() >= until:
            raise AssertionError(
                f"no log line containing {needle!r} within {deadline}s; "
                f"logged: {[r.getMessage() for r in caplog.records]}"
            )
        await asyncio.sleep(0.01)


def _store_manager() -> SubagentManager:
    """A manager with one slot, so a second spawn is a queued, stored row."""
    manager = SubagentManager(
        sessions=_sessions({ROOT}),
        ctx_builder=_ctx_builder(),
        on_spawn_approval=AsyncMock(return_value=True),
        max_concurrent=1,
    )
    manager._global_approval_mode = ""
    return manager


async def _park(info: SubagentInfo) -> None:
    await asyncio.Event().wait()


class TestRefusedRowSettle:
    """``taskq_fail`` settles the row; its awaiters hold the refusal until it has."""

    @pytest.mark.asyncio
    async def test_a_refusal_on_the_drained_branch_settles_its_row_before_returning(
        self, monkeypatch
    ) -> None:
        """The dispatcher's non-claim branch awaits the posted settle too.

        A queued row whose agent name fails validation at dispatch is failed
        by a POSTED store write and returned as a rejection without reaching the
        claim. That branch runs under neither ``spawn_async``'s accept ``finally``
        nor ``claim_and_start``, so unless it awaits the posted write itself the
        entry sits in the pending map for the process lifetime and the row is
        left for the next incarnation's reconcile to requeue and run.
        """
        from kiro_crew import taskq as _taskq
        from kiro_crew.subagent_manager import admission as admission_mod

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        manager = SubagentManager(
            sessions=_sessions({ROOT}),
            ctx_builder=_ctx_builder(),
            on_spawn_approval=AsyncMock(return_value=True),
            max_concurrent=1,
        )
        manager._global_approval_mode = ""
        await manager.wait_taskq_ready()
        manager._spawn_stagger_secs = 0.0
        store = manager._admission.taskq_store()
        assert store is not None, "this pin needs the real durable store"

        async def _park(info: SubagentInfo) -> None:
            await asyncio.Event().wait()

        try:
            with (
                patch("kiro_crew.subagent.Stats"),
                patch("kiro_crew.subagent.sel"),
                patch.object(manager, "_run", _park),
            ):
                manager.spawn("occupy", parent_session_key=ROOT)
                waiting = manager.spawn("waiting", parent_session_key=ROOT)
                assert waiting is not None and waiting.queued
                params = manager._queue.pop(0)
                # Named while it waited: the name is re-validated at dispatch.
                params["agent"] = "ghost"
                manager._running_count = 0
                with patch(
                    "kiro_crew.subagent._validate_agent",
                    return_value=("", "agent 'ghost' not found", "agent_not_found"),
                ):
                    first = await manager._admission._dispatch_async_impl(params)
            assert first is not None and first.done and first.error, first
            # The row is terminal NOW, and the pending map holds nothing for it.
            assert store.state_of(waiting.id) == _taskq.FAILED, store.state_of(waiting.id)
            assert waiting.id not in (getattr(manager, "_pending_defers", None) or {})
        finally:
            await manager.cancel_all()
            manager._taskq.close()

    @pytest.mark.asyncio
    async def test_a_batch_members_rejection_is_announced_only_once_its_row_settles(
        self, monkeypatch
    ) -> None:
        """The wave's completion event for a refused member waits for the row.

        A refused batch member is announced to the parent through ``_on_done``
        (``_announce_rejection`` schedules ``_safe_announce``), which is where
        the wave counts it failed and, when it closes the wave, releases the
        digest. That announce is the refusal PUBLISHED, so it must not run
        while the row's ``failed`` write is still in flight: the dispatcher's
        own await of the settle does not gate a task scheduled beside it.
        """
        from kiro_crew import taskq as _taskq
        from kiro_crew.subagent_manager import admission as admission_mod
        from kiro_crew.subagent_manager.admission import taskq_bridge as bridge_mod

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        monkeypatch.setattr(bridge_mod, "_STATE_READ_BACKOFF_SECS", 0.01)
        monkeypatch.setattr(bridge_mod, "_SETTLE_RETRY_BACKOFF_CAP_SECS", 0.02)
        announced: list[tuple[str, str | None]] = []

        async def _on_done(info: SubagentInfo) -> None:
            announced.append((info.id, store.state_of(info.id)))

        manager = SubagentManager(
            sessions=_sessions({ROOT}),
            ctx_builder=_ctx_builder(),
            on_spawn_approval=AsyncMock(return_value=True),
            on_done=_on_done,
            max_concurrent=1,
        )
        manager._global_approval_mode = ""
        await manager.wait_taskq_ready()
        manager._spawn_stagger_secs = 0.0
        store = manager._admission.taskq_store()
        assert store is not None, "this pin needs the real durable store"
        release = threading.Event()
        real_finish = type(store).finish
        lost: list[str] = []

        def _finish_losing_until_released(self_store, task_id, state, **kw):  # type: ignore[no-untyped-def]
            if state == _taskq.FAILED and not release.is_set():
                lost.append(task_id)
                raise _taskq.TaskStoreUnavailable("database is locked")
            return real_finish(self_store, task_id, state, **kw)

        try:
            with (
                patch("kiro_crew.subagent.Stats"),
                patch("kiro_crew.subagent.sel"),
                patch.object(manager, "_run", _park),
                patch.object(type(store), "finish", _finish_losing_until_released),
            ):
                manager.spawn("occupy", parent_session_key=ROOT)
                waiting = manager.spawn(
                    "waiting", parent_session_key=ROOT, batch_id="wave-1", batch_total=2
                )
                assert waiting is not None and waiting.queued
                params = manager._queue.pop(0)
                params["agent"] = "ghost"
                manager._running_count = 0
                with patch(
                    "kiro_crew.subagent._validate_agent",
                    return_value=("", "agent 'ghost' not found", "agent_not_found"),
                ):
                    dispatch = asyncio.create_task(manager._admission._dispatch_async_impl(params))
                    # The refusal's announce task exists and is parked on the settle.
                    for _ in range(500):
                        if f"reject-{waiting.id}" in manager._tasks and lost:
                            break
                        await asyncio.sleep(0.01)
                    announce = manager._tasks[f"reject-{waiting.id}"]
                    await _await_joined(announce)
                    assert announced == [], "the wave heard the refusal before its row settled"
                    # The drained branch refuses before the claim: the row is still QUEUED.
                    assert store.state_of(waiting.id) == _taskq.QUEUED
                    release.set()
                    first = await asyncio.wait_for(dispatch, 10)
                    await asyncio.wait_for(announce, 10)
            assert first is not None and first.done and first.error
            # The parent heard the refusal exactly once, with the row already FAILED.
            assert announced == [(waiting.id, _taskq.FAILED)], announced
            assert store.state_of(waiting.id) == _taskq.FAILED
        finally:
            await manager.cancel_all()
            manager._taskq.close()

    @pytest.mark.asyncio
    async def test_a_drained_refusals_record_is_registered_only_once_its_row_settles(
        self, monkeypatch
    ) -> None:
        """The terminal record a status poll reads waits for the row too.

        A drained row's refusal registers a ``done`` record under its id so the
        caller's next ``GET /api/spawn/{id}`` reads the failure rather than a
        404. That record is the refusal PUBLISHED to the poll, so it must not
        exist while the row's ``failed`` write is still in flight: a restart
        in that window reverses what the poll already reported. The record is
        registered when the settle lands, before the dispatcher returns.
        """
        from kiro_crew import taskq as _taskq
        from kiro_crew.subagent_manager import admission as admission_mod
        from kiro_crew.subagent_manager.admission import taskq_bridge as bridge_mod

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        monkeypatch.setattr(bridge_mod, "_STATE_READ_BACKOFF_SECS", 0.01)
        monkeypatch.setattr(bridge_mod, "_SETTLE_RETRY_BACKOFF_CAP_SECS", 0.02)
        manager = _store_manager()
        await manager.wait_taskq_ready()
        manager._spawn_stagger_secs = 0.0
        store = manager._admission.taskq_store()
        assert store is not None, "this pin needs the real durable store"
        release = threading.Event()
        real_finish = type(store).finish
        lost: list[str] = []

        def _finish_losing_until_released(self_store, task_id, state, **kw):  # type: ignore[no-untyped-def]
            if state == _taskq.FAILED and not release.is_set():
                lost.append(task_id)
                raise _taskq.TaskStoreUnavailable("database is locked")
            return real_finish(self_store, task_id, state, **kw)

        try:
            with (
                patch("kiro_crew.subagent.Stats"),
                patch("kiro_crew.subagent.sel"),
                patch.object(manager, "_run", _park),
                patch.object(type(store), "finish", _finish_losing_until_released),
            ):
                manager.spawn("occupy", parent_session_key=ROOT)
                waiting = manager.spawn("waiting", parent_session_key=ROOT)
                assert waiting is not None and waiting.queued
                params = manager._queue.pop(0)
                # The cwd the row was queued with is disallowed by the time the
                # drain re-checks it: the policy gate refuses the drained row.
                params["cwd"] = "/tmp/elsewhere"
                manager._running_count = 0
                with patch(
                    "kiro_crew.subagent.validate_cwd",
                    return_value=("", "cwd is outside every allowed root"),
                ):
                    dispatch = asyncio.create_task(manager._admission._dispatch_async_impl(params))
                    # The settle is retrying; the dispatcher is parked on it...
                    for _ in range(500):
                        if lost and waiting.id in (getattr(manager, "_pending_defers", None) or {}):
                            break
                        await asyncio.sleep(0.01)
                    await _await_joined(dispatch)
                    # ...and the poll reads NO record yet: the refusal is unpublished.
                    assert (
                        waiting.id not in manager._agents
                    ), "registered a refusal the store has not taken"
                    assert store.state_of(waiting.id) == _taskq.QUEUED
                    release.set()
                    first = await asyncio.wait_for(dispatch, 10)
            assert first is not None and first.done and first.error
            # Returned with the row FAILED and the record in place for the next poll.
            assert store.state_of(waiting.id) == _taskq.FAILED
            registered = manager._agents.get(waiting.id)
            assert registered is not None and registered.done and registered.error
        finally:
            await manager.cancel_all()
            manager._taskq.close()

    @pytest.mark.asyncio
    async def test_a_refusal_is_held_until_its_row_settles_past_the_read_bound(
        self, monkeypatch, caplog
    ) -> None:
        """The settle is not given up on: the refusal is HELD until the row reads terminal.

        Store contention is routine here, so one retry is not a guarantee. When
        the FAILED write keeps losing past the state-read bound, the caller
        does not hear the refusal -- a refusal published over a row still
        ADMITTED is the one state the next incarnation's reconcile requeues and
        runs. The settle keeps retrying with a capped backoff, logs once that
        it is holding, and the refusal returns with the row FAILED.
        """
        from kiro_crew import taskq as _taskq
        from kiro_crew.subagent_manager import admission as admission_mod
        from kiro_crew.subagent_manager.admission import taskq_bridge as bridge_mod

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        monkeypatch.setattr(bridge_mod, "_STATE_READ_BACKOFF_SECS", 0.01)
        monkeypatch.setattr(bridge_mod, "_SETTLE_RETRY_BACKOFF_CAP_SECS", 0.02)
        manager = _store_manager()
        await manager.wait_taskq_ready()
        manager._spawn_stagger_secs = 0.0
        store = manager._admission.taskq_store()
        assert store is not None, "this pin needs the real durable store"
        lose = bridge_mod._STATE_READ_ATTEMPTS + 2
        real_finish = type(store).finish
        lost: list[str] = []

        def _finish_losing(self_store, task_id, state, **kw):  # type: ignore[no-untyped-def]
            if state == _taskq.FAILED and len(lost) < lose:
                lost.append(task_id)
                raise _taskq.TaskStoreUnavailable("database is locked")
            return real_finish(self_store, task_id, state, **kw)

        try:
            with (
                patch("kiro_crew.subagent.Stats"),
                patch("kiro_crew.subagent.sel"),
                patch.object(manager, "_run", _park),
                patch.object(type(store), "finish", _finish_losing),
                caplog.at_level(logging.WARNING, logger="kiro_crew.subagent_manager.admission"),
            ):
                manager.spawn("occupy", parent_session_key=ROOT)
                waiting = manager.spawn("waiting", parent_session_key=ROOT)
                assert waiting is not None and waiting.queued
                params = manager._queue.pop(0)
                params["agent"] = "ghost"
                manager._running_count = 0
                with patch(
                    "kiro_crew.subagent._validate_agent",
                    return_value=("", "agent 'ghost' not found", "agent_not_found"),
                ):
                    first = await manager._admission._dispatch_async_impl(params)
            assert first is not None and first.done and first.error, first
            # The refusal came back only once the row read FAILED, past ``lose`` lost writes.
            assert store.state_of(waiting.id) == _taskq.FAILED, store.state_of(waiting.id)
            assert len(lost) == lose, lost
            held = [r for r in caplog.records if "holding its refusal" in r.getMessage()]
            assert len(held) == 1, [r.getMessage() for r in caplog.records]
            assert not (getattr(manager, "_pending_defers", None) or {})
        finally:
            await manager.cancel_all()
            manager._taskq.close()

    @pytest.mark.asyncio
    async def test_a_cancelled_awaiter_does_not_stop_the_settle_and_a_later_one_joins_it(
        self, monkeypatch
    ) -> None:
        """The settle is a retained store task, not the awaiter's own coroutine.

        A caller cancelled while its refusal is held (a turn torn down, a
        dispatcher cancelled) must not take the settle down with it, or the
        row is left ADMITTED exactly as if nothing had retried. The settle
        stays in the pending map under the row's id, so a later awaiter joins
        it -- shielded -- and returns once the row reads terminal; the entry is
        gone once it has.
        """
        from kiro_crew import taskq as _taskq
        from kiro_crew.subagent_manager import admission as admission_mod
        from kiro_crew.subagent_manager.admission import taskq_bridge as bridge_mod

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        monkeypatch.setattr(bridge_mod, "_STATE_READ_BACKOFF_SECS", 0.01)
        monkeypatch.setattr(bridge_mod, "_SETTLE_RETRY_BACKOFF_CAP_SECS", 0.02)
        manager = SubagentManager(
            sessions=_sessions({ROOT}),
            ctx_builder=_ctx_builder(),
            on_spawn_approval=AsyncMock(return_value=True),
            max_concurrent=1,
        )
        manager._global_approval_mode = ""
        await manager.wait_taskq_ready()
        manager._spawn_stagger_secs = 0.0
        store = manager._admission.taskq_store()
        assert store is not None, "this pin needs the real durable store"

        async def _park(info: SubagentInfo) -> None:
            await asyncio.Event().wait()

        loop = asyncio.get_running_loop()
        release = threading.Event()
        holding = asyncio.Event()
        real_finish = type(store).finish
        lost: list[str] = []

        def _finish_losing_until_released(self_store, task_id, state, **kw):  # type: ignore[no-untyped-def]
            if state == _taskq.FAILED and not release.is_set():
                lost.append(task_id)
                if len(lost) >= bridge_mod._STATE_READ_ATTEMPTS:
                    loop.call_soon_threadsafe(holding.set)
                raise _taskq.TaskStoreUnavailable("database is locked")
            return real_finish(self_store, task_id, state, **kw)

        try:
            with (
                patch("kiro_crew.subagent.Stats"),
                patch("kiro_crew.subagent.sel"),
                patch.object(manager, "_run", _park),
                patch.object(type(store), "finish", _finish_losing_until_released),
            ):
                manager.spawn("occupy", parent_session_key=ROOT)
                waiting = manager.spawn("waiting", parent_session_key=ROOT)
                assert waiting is not None and waiting.queued
                for _ in range(200):
                    if store.state_of(waiting.id) == _taskq.QUEUED:
                        break
                    await asyncio.sleep(0.01)
                assert store.claim(waiting.id) is not None
                assert store.state_of(waiting.id) == _taskq.ADMITTED
                # The gate's refusal: a POSTED failed write, recorded under the id.
                manager._admission.taskq_fail(waiting.id, REFUSAL)
                first = asyncio.create_task(manager._admission.await_pending_defer(waiting.id))
                await asyncio.wait_for(holding.wait(), 5)
                assert not first.done()
                first.cancel()
                with suppress(asyncio.CancelledError):
                    await first
                # The settle outlived its awaiter and is still the row's pending entry.
                pending = getattr(manager, "_pending_defers", None) or {}
                settle = pending[waiting.id]
                assert not settle.done()
                assert store.state_of(waiting.id) == _taskq.ADMITTED
                # A later awaiter joins it and returns only once the row reads FAILED.
                second = asyncio.create_task(manager._admission.await_pending_defer(waiting.id))
                await _await_joined(second)
                assert not second.done()
                release.set()
                await asyncio.wait_for(second, 5)
                assert settle.done()
            assert store.state_of(waiting.id) == _taskq.FAILED, store.state_of(waiting.id)
            assert waiting.id not in (getattr(manager, "_pending_defers", None) or {})
            assert len(lost) >= bridge_mod._STATE_READ_ATTEMPTS, lost
        finally:
            await manager.cancel_all()
            manager._taskq.close()

    @pytest.mark.asyncio
    async def test_a_refusal_is_held_while_the_store_refuses_even_with_its_tombstone_down(
        self, monkeypatch, caplog
    ) -> None:
        """Persist before you publish: the awaiter has no bound of its own.

        The store refuses every ``failed`` write. The persistent run's tombstone
        is on disk, and the next boot's reconcile would read it as failed for
        an ACTIVE row -- but the refusal is still not published: the row reads
        ADMITTED, the caller's turn would end on a verdict the store has not
        taken, and a QUEUED row refused the same way is a row no boot probes at
        all. The awaiter holds, the settle warns once that it is holding and
        keeps retrying, and the refusal returns only once the row reads FAILED.
        """
        from kiro_crew import taskq as _taskq
        from kiro_crew.subagent_manager import admission as admission_mod
        from kiro_crew.subagent_manager.admission import taskq_bridge as bridge_mod
        from kiro_crew.subagent_persistence import read_tombstone

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        monkeypatch.setattr(bridge_mod, "_STATE_READ_BACKOFF_SECS", 0.01)
        monkeypatch.setattr(bridge_mod, "_SETTLE_RETRY_BACKOFF_CAP_SECS", 0.02)
        manager = _store_manager()
        await manager.wait_taskq_ready()
        manager._spawn_stagger_secs = 0.0
        store = manager._admission.taskq_store()
        assert store is not None, "this pin needs the real durable store"
        release = threading.Event()
        real_finish = type(store).finish
        lost: list[str] = []

        def _finish_refusing_until_released(self_store, task_id, state, **kw):  # type: ignore[no-untyped-def]
            if state == _taskq.FAILED and not release.is_set():
                lost.append(task_id)
                raise _taskq.TaskStoreUnavailable("disk I/O error")
            return real_finish(self_store, task_id, state, **kw)

        try:
            with (
                patch("kiro_crew.subagent.Stats"),
                patch("kiro_crew.subagent.sel"),
                patch.object(manager, "_run", _park),
                patch.object(type(store), "finish", _finish_refusing_until_released),
                caplog.at_level(logging.WARNING, logger="kiro_crew.subagent_manager.admission"),
            ):
                manager.spawn("occupy", parent_session_key=ROOT)
                waiting = manager.spawn("waiting", parent_session_key=ROOT)
                assert waiting is not None and waiting.queued
                for _ in range(200):
                    if store.state_of(waiting.id) == _taskq.QUEUED:
                        break
                    await asyncio.sleep(0.01)
                assert store.claim(waiting.id) is not None
                assert store.state_of(waiting.id) == _taskq.ADMITTED
                manager._admission.taskq_fail(waiting.id, REFUSAL, memory_mode="persistent")
                awaiter = asyncio.create_task(manager._admission.await_pending_defer(waiting.id))
                # The settle is past its tombstone step and into its retry loop...
                held = await _await_log(caplog, "holding its refusal")
                assert len(held) == 1, [r.getMessage() for r in caplog.records]
                assert (read_tombstone(waiting.id) or {}).get("cause") == "error"
                # ...and the awaiter is parked on it, the refusal unpublished.
                await _await_joined(awaiter)
                assert not awaiter.done(), "published a refusal the store has not taken"
                assert store.state_of(waiting.id) == _taskq.ADMITTED
                assert len(lost) >= bridge_mod._STATE_READ_ATTEMPTS, lost
                # The store recovers: the write lands and only then is the awaiter released.
                release.set()
                await asyncio.wait_for(awaiter, 10)
                assert store.state_of(waiting.id) == _taskq.FAILED
                assert waiting.id not in (getattr(manager, "_pending_defers", None) or {})
        finally:
            await manager.cancel_all()
            manager._taskq.close()

    @pytest.mark.asyncio
    async def test_a_caller_cancelled_while_the_failed_write_is_still_queued_does_not_lose_it(
        self, monkeypatch
    ) -> None:
        """One tracked, shielded task covers the FIRST write as well as its verification.

        The store has one writer thread; while writes queue ahead, the refusal's
        own ``failed`` write is still only a coroutine waiting its turn. If the
        caller awaiting it is cancelled THEN -- and the shutdown that cancels a
        turn is the very event whose restart requeues an ADMITTED row -- the
        write must still reach SQLite. So the write is not a separate posted
        task the awaiter can take down: it is the first step of the retained
        settle, and the awaiter only joins that settle, shielded.
        """
        from kiro_crew import taskq as _taskq
        from kiro_crew.subagent_manager import admission as admission_mod

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        manager = SubagentManager(
            sessions=_sessions({ROOT}),
            ctx_builder=_ctx_builder(),
            on_spawn_approval=AsyncMock(return_value=True),
            max_concurrent=1,
        )
        manager._global_approval_mode = ""
        await manager.wait_taskq_ready()
        manager._spawn_stagger_secs = 0.0
        store = manager._admission.taskq_store()
        assert store is not None, "this pin needs the real durable store"

        async def _park(info: SubagentInfo) -> None:
            await asyncio.Event().wait()

        # Occupy the single writer thread so the failed write cannot start.
        writer_busy = threading.Event()
        writer_release = threading.Event()

        def _hog() -> None:
            writer_busy.set()
            writer_release.wait(5)

        try:
            with (
                patch("kiro_crew.subagent.Stats"),
                patch("kiro_crew.subagent.sel"),
                patch.object(manager, "_run", _park),
            ):
                manager.spawn("occupy", parent_session_key=ROOT)
                waiting = manager.spawn("waiting", parent_session_key=ROOT)
                assert waiting is not None and waiting.queued
                for _ in range(200):
                    if store.state_of(waiting.id) == _taskq.QUEUED:
                        break
                    await asyncio.sleep(0.01)
                assert store.claim(waiting.id) is not None
                assert store.state_of(waiting.id) == _taskq.ADMITTED
                hog = asyncio.ensure_future(store.run(_hog))
                await asyncio.get_running_loop().run_in_executor(None, writer_busy.wait, 5)
                # The refusal's write is queued behind the hog; its awaiter is
                # cancelled before the write has run.
                manager._admission.taskq_fail(waiting.id, REFUSAL)
                awaiter = asyncio.create_task(manager._admission.await_pending_defer(waiting.id))
                await _await_joined(awaiter)
                assert not awaiter.done()
                awaiter.cancel()
                with suppress(asyncio.CancelledError):
                    await awaiter
                assert store.state_of(waiting.id) == _taskq.ADMITTED
                settle = (getattr(manager, "_pending_defers", None) or {})[waiting.id]
                assert not settle.done() and not settle.cancelled()
                writer_release.set()
                await hog
                await asyncio.wait_for(settle, 5)
            assert store.state_of(waiting.id) == _taskq.FAILED, store.state_of(waiting.id)
            assert waiting.id not in (getattr(manager, "_pending_defers", None) or {})
        finally:
            await manager.cancel_all()
            manager._taskq.close()

    @pytest.mark.asyncio
    async def test_a_settle_cancelled_at_the_drain_still_fails_the_row_on_the_next_boot(
        self, monkeypatch
    ) -> None:
        """The verdict does not live in the store alone.

        ``cancel_all`` drains tracked writes for a bounded time and then cancels
        the stragglers, so a store wedged for the whole drain takes the
        ``failed`` write down with the shutdown -- and the next boot's reconcile
        requeues an ADMITTED row without asking. The settle therefore writes the
        refused run's tombstone FIRST, where the store cannot reach: the
        reconcile asks the artifact probe before it requeues, and a tombstone
        whose cause is ``error`` is its ``failed``. Here the store never accepts
        the write, the settle is cancelled the way the drain cancels it, and a
        reconcile over the row -- as the next incarnation would run it --
        settles the row failed instead of requeueing it.
        """
        from kiro_crew import taskq as _taskq
        from kiro_crew.subagent_manager import admission as admission_mod
        from kiro_crew.subagent_manager.admission import taskq_bridge as bridge_mod
        from kiro_crew.subagent_persistence import read_tombstone
        from kiro_crew.taskq import reconcile as _reconcile

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        monkeypatch.setattr(bridge_mod, "_STATE_READ_BACKOFF_SECS", 0.01)
        monkeypatch.setattr(bridge_mod, "_SETTLE_RETRY_BACKOFF_CAP_SECS", 0.02)
        manager = SubagentManager(
            sessions=_sessions({ROOT}),
            ctx_builder=_ctx_builder(),
            on_spawn_approval=AsyncMock(return_value=True),
            max_concurrent=1,
        )
        manager._global_approval_mode = ""
        await manager.wait_taskq_ready()
        manager._spawn_stagger_secs = 0.0
        store = manager._admission.taskq_store()
        assert store is not None, "this pin needs the real durable store"

        async def _park(info: SubagentInfo) -> None:
            await asyncio.Event().wait()

        real_finish = type(store).finish

        def _finish_never_lands(self_store, task_id, state, **kw):  # type: ignore[no-untyped-def]
            if state == _taskq.FAILED:
                raise _taskq.TaskStoreUnavailable("database is locked")
            return real_finish(self_store, task_id, state, **kw)

        try:
            with (
                patch("kiro_crew.subagent.Stats"),
                patch("kiro_crew.subagent.sel"),
                patch.object(manager, "_run", _park),
                patch.object(type(store), "finish", _finish_never_lands),
            ):
                manager.spawn("occupy", parent_session_key=ROOT, _memory_mode="persistent")
                waiting = manager.spawn(
                    "waiting", parent_session_key=ROOT, _memory_mode="persistent"
                )
                assert waiting is not None and waiting.queued
                for _ in range(200):
                    if store.state_of(waiting.id) == _taskq.QUEUED:
                        break
                    await asyncio.sleep(0.01)
                assert store.claim(waiting.id) is not None
                assert store.state_of(waiting.id) == _taskq.ADMITTED
                manager._admission.taskq_fail(waiting.id, REFUSAL, memory_mode="persistent")
                settle = (getattr(manager, "_pending_defers", None) or {})[waiting.id]
                # Let the settle write its tombstone (the design puts that step
                # FIRST) and run into its retry loop, then cancel it the way the
                # drain cancels a straggler. Waiting on the artifact rather than
                # a fixed pause keeps the pin about the cancel, not the runner's
                # speed: on a slow Windows runner a 100 ms pause landed before
                # the off-loop tombstone write had finished.
                for _ in range(500):
                    if read_tombstone(waiting.id):
                        break
                    await asyncio.sleep(0.01)
                assert read_tombstone(waiting.id), "the settle did not write its tombstone"
                assert not settle.done()
                settle.cancel()
                with suppress(asyncio.CancelledError):
                    await settle
                # The store never heard the failure...
                assert store.state_of(waiting.id) == _taskq.ADMITTED
                # ...but the run's ending is on disk, and the probe reads it as failed.
                tombstone = read_tombstone(waiting.id) or {}
                assert tombstone.get("cause") == "error", tombstone
                assert REFUSAL in str(tombstone.get("detail", "")), tombstone
                rec = store.get(waiting.id)
                assert rec is not None
                assert manager._admission.taskq_artifact_probe(rec) == _taskq.FAILED
            # The next incarnation's reconcile, over this row: settled failed, not requeued.
            report = _reconcile.ReconcileReport()
            _reconcile._settle_one(
                store,
                rec,
                manager._admission.taskq_artifact_probe,
                frozenset(_reconcile.DEFAULT_RECOVERY_ADAPTERS),
                store.now(),
                report,
            )
            assert report.settled_failed == 1 and report.requeued == 0, report
            assert store.state_of(waiting.id) == _taskq.FAILED
        finally:
            await manager.cancel_all()
            manager._taskq.close()

    @pytest.mark.asyncio
    async def test_a_queued_rows_refusal_tombstone_settles_it_failed_on_boot_instead_of_starting_it(
        self, monkeypatch
    ) -> None:
        """A refused row the dispatcher never claimed is not run by the next boot.

        The memory-pressure expiry refuses a row still QUEUED. Its tombstone is
        written first; when the ``failed`` write never lands before shutdown
        the row survives QUEUED. Left to the pump, the refused run starts
        under an id whose tombstone already records an ending, for the NEXT
        crash's probe to misread. The reconcile reads the queued rows'
        artifacts too: a recorded ending settles the row, and a queued row
        whose artifacts say nothing is left exactly as it was.
        """
        from kiro_crew import taskq as _taskq
        from kiro_crew.subagent_manager import admission as admission_mod
        from kiro_crew.subagent_manager.admission import taskq_bridge as bridge_mod
        from kiro_crew.subagent_persistence import read_tombstone
        from kiro_crew.taskq import reconcile as _reconcile

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        monkeypatch.setattr(bridge_mod, "_STATE_READ_BACKOFF_SECS", 0.01)
        monkeypatch.setattr(bridge_mod, "_SETTLE_RETRY_BACKOFF_CAP_SECS", 0.02)
        manager = _store_manager()
        await manager.wait_taskq_ready()
        manager._spawn_stagger_secs = 0.0
        store = manager._admission.taskq_store()
        assert store is not None, "this pin needs the real durable store"
        real_finish = type(store).finish

        def _finish_never_lands(self_store, task_id, state, **kw):  # type: ignore[no-untyped-def]
            if state == _taskq.FAILED:
                raise _taskq.TaskStoreUnavailable("database is locked")
            return real_finish(self_store, task_id, state, **kw)

        try:
            with (
                patch("kiro_crew.subagent.Stats"),
                patch("kiro_crew.subagent.sel"),
                patch.object(manager, "_run", _park),
                patch.object(type(store), "finish", _finish_never_lands),
            ):
                manager.spawn("occupy", parent_session_key=ROOT, _memory_mode="persistent")
                refused = manager.spawn(
                    "refused while queued", parent_session_key=ROOT, _memory_mode="persistent"
                )
                untouched = manager.spawn("still queued", parent_session_key=ROOT)
                assert refused is not None and refused.queued
                assert untouched is not None and untouched.queued
                for _ in range(200):
                    if store.state_of(refused.id) == store.state_of(untouched.id) == _taskq.QUEUED:
                        break
                    await asyncio.sleep(0.01)
                # Refused without a claim, as the memory-pressure expiry refuses.
                manager._admission.taskq_fail(refused.id, REFUSAL, memory_mode="persistent")
                settle = (getattr(manager, "_pending_defers", None) or {})[refused.id]
                for _ in range(500):
                    if read_tombstone(refused.id):
                        break
                    await asyncio.sleep(0.01)
                assert read_tombstone(refused.id), "the settle did not write its tombstone"
                settle.cancel()
                with suppress(asyncio.CancelledError):
                    await settle
                # The store never heard the failure: the row is still a dispatchable QUEUED row.
                assert store.state_of(refused.id) == _taskq.QUEUED
            # The next incarnation's boot reconcile, over the whole store.
            report = _reconcile.reconcile_on_boot(
                store, artifact_probe=manager._admission.taskq_artifact_probe
            )
            assert report.queued_examined == 2, report
            assert report.settled_failed == 1 and report.requeued == 0, report
            assert store.state_of(refused.id) == _taskq.FAILED
            assert store.state_of(untouched.id) == _taskq.QUEUED
        finally:
            await manager.cancel_all()
            manager._taskq.close()

    @pytest.mark.asyncio
    async def test_a_failed_write_that_raises_something_other_than_the_stores_failure_ends_the_hold(
        self, monkeypatch, caplog
    ) -> None:
        """Only the store's typed failure is retried without limit.

        Store contention is ``TaskStoreUnavailable`` and is retried for as long
        as it lasts. Any other exception from the ``failed`` write is this
        row's -- a bad argument, a bug -- and no retry changes it; retrying
        anyway would park the dispatcher on that row and stop every other
        queued row from starting. The settle ends with a warning and leaves the
        row to its tombstone and the boot reconcile.
        """
        from kiro_crew import taskq as _taskq
        from kiro_crew.subagent_manager import admission as admission_mod
        from kiro_crew.subagent_manager.admission import taskq_bridge as bridge_mod

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        monkeypatch.setattr(bridge_mod, "_STATE_READ_BACKOFF_SECS", 0.01)
        monkeypatch.setattr(bridge_mod, "_SETTLE_RETRY_BACKOFF_CAP_SECS", 0.02)
        manager = _store_manager()
        await manager.wait_taskq_ready()
        manager._spawn_stagger_secs = 0.0
        store = manager._admission.taskq_store()
        assert store is not None, "this pin needs the real durable store"
        real_finish = type(store).finish

        def _finish_raises_a_bug(self_store, task_id, state, **kw):  # type: ignore[no-untyped-def]
            if state == _taskq.FAILED:
                raise ValueError("not a store condition")
            return real_finish(self_store, task_id, state, **kw)

        try:
            with (
                patch("kiro_crew.subagent.Stats"),
                patch("kiro_crew.subagent.sel"),
                patch.object(manager, "_run", _park),
                patch.object(type(store), "finish", _finish_raises_a_bug),
                caplog.at_level(logging.WARNING, logger=bridge_mod._glue_logger.name),
            ):
                manager.spawn("occupy", parent_session_key=ROOT)
                waiting = manager.spawn("waiting", parent_session_key=ROOT)
                assert waiting is not None and waiting.queued
                params = manager._queue.pop(0)
                params["agent"] = "ghost"
                manager._running_count = 0
                with patch(
                    "kiro_crew.subagent._validate_agent",
                    return_value=("", "agent 'ghost' not found", "agent_not_found"),
                ):
                    # Bounded: with the write raising the same way every time,
                    # the dispatcher must come back, not hold.
                    first = await asyncio.wait_for(
                        manager._admission._dispatch_async_impl(params), 10
                    )
            assert first is not None and first.done and first.error
            assert store.state_of(waiting.id) == _taskq.QUEUED, "no retry can land this write"
            assert waiting.id not in (getattr(manager, "_pending_defers", None) or {})
            assert any("ending its settle" in r.getMessage() for r in caplog.records)
        finally:
            await manager.cancel_all()
            manager._taskq.close()

    @pytest.mark.asyncio
    async def test_a_tombstone_offload_that_fails_still_settles_the_row_in_the_store(
        self, monkeypatch, caplog
    ) -> None:
        """The tombstone and the store write are two verdicts; losing the hop to
        the first must not skip the second.

        The tombstone is written on a worker thread. If that hop itself fails
        (no thread to run it: the pool is gone) before either durable write,
        the settle must still run the store write, or the refusal is
        published over a row left QUEUED with no tombstone -- dispatchable
        after a restart.
        """
        from kiro_crew import taskq as _taskq
        from kiro_crew.subagent_manager import admission as admission_mod
        from kiro_crew.subagent_manager.admission import taskq_bridge as bridge_mod
        from kiro_crew.subagent_persistence import read_tombstone

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        manager = _store_manager()
        await manager.wait_taskq_ready()
        manager._spawn_stagger_secs = 0.0
        store = manager._admission.taskq_store()
        assert store is not None, "this pin needs the real durable store"
        real_to_thread = bridge_mod._asyncio.to_thread

        async def _to_thread_without_a_worker(fn, /, *args, **kwargs):  # type: ignore[no-untyped-def]
            if getattr(fn, "__name__", "") == "_tombstone_refused_row":
                raise RuntimeError("cannot schedule new futures after shutdown")
            return await real_to_thread(fn, *args, **kwargs)

        try:
            with (
                patch("kiro_crew.subagent.Stats"),
                patch("kiro_crew.subagent.sel"),
                patch.object(manager, "_run", _park),
                patch.object(bridge_mod._asyncio, "to_thread", _to_thread_without_a_worker),
                caplog.at_level(logging.WARNING, logger=bridge_mod._glue_logger.name),
            ):
                manager.spawn("occupy", parent_session_key=ROOT, _memory_mode="persistent")
                waiting = manager.spawn(
                    "waiting", parent_session_key=ROOT, _memory_mode="persistent"
                )
                assert waiting is not None and waiting.queued
                params = manager._queue.pop(0)
                params["agent"] = "ghost"
                manager._running_count = 0
                with patch(
                    "kiro_crew.subagent._validate_agent",
                    return_value=("", "agent 'ghost' not found", "agent_not_found"),
                ):
                    first = await asyncio.wait_for(
                        manager._admission._dispatch_async_impl(params), 10
                    )
            assert first is not None and first.done and first.error
            # No tombstone could be written...
            assert read_tombstone(waiting.id) is None
            # ...and the store still holds the verdict the caller was given.
            assert store.state_of(waiting.id) == _taskq.FAILED
            assert any("could not be offloaded" in r.getMessage() for r in caplog.records)
        finally:
            await manager.cancel_all()
            manager._taskq.close()


class TestRefusalTombstoneHygiene:
    """The refused row's tombstone ``detail`` is redacted by the facade's writer.

    The write lives in ``subagent.py`` beside the run tombstone writer, so the
    one classified module owns the field's hygiene and the admission bridge
    carries no redactor of its own.
    """

    def test_refusal_tombstone_detail_is_redacted_and_bounded(self) -> None:
        from kiro_crew.process_identity import MAX_ERROR_DETAIL_LEN
        from kiro_crew.subagent import write_refusal_tombstone

        reason = "refused: key AKIAIOSFODNN7EXAMPLE leaked " + "x" * (MAX_ERROR_DETAIL_LEN * 2)
        with (
            patch("kiro_crew.subagent.write_tombstone") as writer,
            patch("kiro_crew.subagent.read_state", return_value={}),
            patch("kiro_crew.subagent.tombstone_recovery_action", return_value="none"),
        ):
            write_refusal_tombstone("r1", reason)
        writer.assert_called_once()
        kwargs = writer.call_args.kwargs
        assert writer.call_args.args == ("r1",)
        assert kwargs["cause"] == "error"
        assert "AKIAIOSFODNN7EXAMPLE" not in kwargs["detail"]
        assert len(kwargs["detail"]) <= MAX_ERROR_DETAIL_LEN
