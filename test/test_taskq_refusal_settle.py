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
from kiro_crew.subagent_manager.admission.types import REFUSAL_TOMBSTONE_CAUSE
from kiro_crew.taskq.reconcile import REFUSED
from kiro_crew.testing.wait import async_wait_until, await_until

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
    return await async_wait_until(
        lambda: [r for r in caplog.records if needle in r.getMessage()],
        timeout=deadline,
        describe=lambda: f"no log line containing {needle!r}; "
        f"logged: {[r.getMessage() for r in caplog.records]}",
    )


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
                    await async_wait_until(
                        lambda: f"reject-{waiting.id}" in manager._tasks and lost
                    )
                    announce = manager._tasks[f"reject-{waiting.id}"]
                    await _await_joined(announce)
                    assert announced == [], "the wave heard the refusal before its row settled"
                    # The drained branch refuses before the claim: the row is still QUEUED.
                    assert store.state_of(waiting.id) == _taskq.QUEUED
                    release.set()
                    first = await await_until(dispatch)
                    await await_until(announce)
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
                    await async_wait_until(
                        lambda: lost
                        and waiting.id in (getattr(manager, "_pending_defers", None) or {})
                    )
                    await _await_joined(dispatch)
                    # ...and the poll reads NO record yet: the refusal is unpublished.
                    assert (
                        waiting.id not in manager._agents
                    ), "registered a refusal the store has not taken"
                    assert store.state_of(waiting.id) == _taskq.QUEUED
                    release.set()
                    first = await await_until(dispatch)
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
        """The settle is not given up on: it retries past the read bound until the row reads terminal.

        Store contention is routine here, so one retry is not a guarantee. When
        the FAILED write keeps losing past the state-read bound, the settle
        keeps retrying with a capped backoff and logs once that it is doing so.
        The refused spawn is a persistent run, so its tombstone is down and
        read back by then and the caller hears the refusal on it -- but the
        settle goes on behind the caller, and the row reads FAILED once the
        store takes the write, after every one of the lost tries.
        """
        from kiro_crew import taskq as _taskq
        from kiro_crew.subagent_manager import admission as admission_mod
        from kiro_crew.subagent_manager.admission import taskq_bridge as bridge_mod
        from kiro_crew.subagent_persistence import read_tombstone

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
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
                # The refusal came back on the tombstone, past the read bound...
                assert (read_tombstone(waiting.id) or {}).get("cause") == REFUSAL_TOMBSTONE_CAUSE
                assert len(lost) >= bridge_mod._STATE_READ_ATTEMPTS, lost
                # ...and the settle kept going: the row reads FAILED once the
                # store takes the write, after every lost try.
                await async_wait_until(lambda: store.state_of(waiting.id) == _taskq.FAILED)
                assert len(lost) == lose, lost
            held = [
                r
                for r in caplog.records
                if "and retrying until the row reads terminal" in r.getMessage()
            ]
            assert len(held) == 1, [r.getMessage() for r in caplog.records]
            await async_wait_until(lambda: not (getattr(manager, "_pending_defers", None) or {}))
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
                await async_wait_until(lambda: store.state_of(waiting.id) == _taskq.QUEUED)
                assert store.claim(waiting.id) is not None
                assert store.state_of(waiting.id) == _taskq.ADMITTED
                # The gate's refusal: a POSTED failed write, recorded under the id.
                manager._admission.taskq_fail(waiting.id, REFUSAL)
                first = asyncio.create_task(manager._admission.await_pending_defer(waiting.id))
                await await_until(holding)
                assert not first.done()
                first.cancel()
                with suppress(asyncio.CancelledError):
                    await first
                # The settle outlived its awaiter and is still the row's pending entry.
                pending = getattr(manager, "_pending_defers", None) or {}
                settle = pending[waiting.id]
                assert not settle.done()
                assert store.state_of(waiting.id) == _taskq.ADMITTED
                # With its awaiter gone the row is out of ``_admitting_ids``; the
                # pending settle alone keeps it out of the REFILL until the
                # ``failed`` lands, so a pump pass in this window cannot start
                # the run the refusal rejected -- while the cancellation and
                # pending-work reads still see it, so a parent's Stop in the
                # same window reaches it rather than reporting done over it.
                assert waiting.id not in getattr(manager, "_admitting_ids", set())
                assert waiting.id in manager._admission.taskq_dispatch_excluded_ids()
                assert waiting.id not in manager._admission.taskq_excluded_ids(window=False)
                # A later awaiter joins it and returns only once the row reads FAILED.
                second = asyncio.create_task(manager._admission.await_pending_defer(waiting.id))
                await _await_joined(second)
                assert not second.done()
                release.set()
                await await_until(second)
                assert settle.done()
                # Released a tick later, by the settle's own done-callback.
                await async_wait_until(
                    lambda: waiting.id not in (getattr(manager, "_pending_defers", None) or {})
                )
            assert store.state_of(waiting.id) == _taskq.FAILED, store.state_of(waiting.id)
            assert waiting.id not in (getattr(manager, "_pending_defers", None) or {})
            assert len(lost) >= bridge_mod._STATE_READ_ATTEMPTS, lost
        finally:
            await manager.cancel_all()
            manager._taskq.close()

    @pytest.mark.asyncio
    async def test_a_refusal_with_its_tombstone_down_is_released_past_the_read_bound(
        self, monkeypatch, caplog
    ) -> None:
        """Persist before you publish, with the tombstone as the persisted verdict.

        The store refuses every ``failed`` write. The persistent run's refusal
        tombstone is on disk and read back: the next boot's reconcile settles
        the row ``failed`` from it whether it reads ADMITTED (active pass) or
        QUEUED (queued pass, ``REFUSED``), so once ``_STATE_READ_ATTEMPTS``
        store tries have failed the awaiter is released on that verdict. The
        settle itself is NOT released: it warns once that the tombstone stands
        in for the row, keeps retrying, and lands ``failed`` the moment the
        store answers again.
        """
        from kiro_crew import taskq as _taskq
        from kiro_crew.subagent_manager import admission as admission_mod
        from kiro_crew.subagent_manager.admission import taskq_bridge as bridge_mod
        from kiro_crew.subagent_persistence import read_tombstone

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
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
                await async_wait_until(lambda: store.state_of(waiting.id) == _taskq.QUEUED)
                assert store.claim(waiting.id) is not None
                assert store.state_of(waiting.id) == _taskq.ADMITTED
                manager._admission.taskq_fail(waiting.id, REFUSAL, memory_mode="persistent")
                awaiter = asyncio.create_task(manager._admission.await_pending_defer(waiting.id))
                # The settle is past its tombstone step, the store has refused
                # the write past the read bound, and it says the tombstone stands in...
                held = await _await_log(caplog, "its tombstone is the verdict")
                assert len(held) == 1, [r.getMessage() for r in caplog.records]
                assert (read_tombstone(waiting.id) or {}).get("cause") == REFUSAL_TOMBSTONE_CAUSE
                assert len(lost) >= bridge_mod._STATE_READ_ATTEMPTS, lost
                # ...so the awaiter is released on it, the row still unsettled in the store.
                await await_until(awaiter)
                assert store.state_of(waiting.id) == _taskq.ADMITTED
                rec = store.get(waiting.id)
                assert rec is not None
                assert manager._admission.taskq_artifact_probe(rec) == REFUSED
                # The release dropped the row from the in-process exclusion, so
                # the store holds it instead: live dispatch cannot claim it
                # before its ending is on disk.
                assert store.held_until_settled() == [waiting.id]
                assert waiting.id not in (getattr(manager, "_pending_defers", None) or {})
                assert store.claim(waiting.id) is None
                assert [r.id for r in store.fetch_dispatchable(_taskq.KIND_SUBAGENT, limit=8)] == []
                # The settle did not stop with the awaiter: the store recovers
                # and the write lands.
                release.set()
                await async_wait_until(lambda: store.state_of(waiting.id) == _taskq.FAILED)
                assert store.claim(waiting.id) is None
        finally:
            await manager.cancel_all()
            manager._taskq.close()

    @pytest.mark.asyncio
    async def test_a_refusal_with_no_tombstone_is_held_while_the_store_refuses(
        self, monkeypatch, caplog
    ) -> None:
        """Persist before you publish: with no tombstone the hold has no bound.

        A memory-only run keeps no tombstone, so the ``failed`` write is the
        only durable verdict. The store refuses it: the awaiter holds past the
        read bound, the settle warns once that it is holding and keeps
        retrying, and the refusal returns only once the row reads FAILED.
        """
        from kiro_crew import taskq as _taskq
        from kiro_crew.subagent_manager import admission as admission_mod
        from kiro_crew.subagent_manager.admission import taskq_bridge as bridge_mod
        from kiro_crew.subagent_persistence import read_tombstone

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
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
                await async_wait_until(lambda: store.state_of(waiting.id) == _taskq.QUEUED)
                assert store.claim(waiting.id) is not None
                assert store.state_of(waiting.id) == _taskq.ADMITTED
                manager._admission.taskq_fail(waiting.id, REFUSAL)
                awaiter = asyncio.create_task(manager._admission.await_pending_defer(waiting.id))
                # The settle is into its retry loop past the read bound...
                held = await _await_log(caplog, "holding its refusal")
                assert len(held) == 1, [r.getMessage() for r in caplog.records]
                assert read_tombstone(waiting.id) is None
                # ...and the awaiter is parked on it, the refusal unpublished.
                await _await_joined(awaiter)
                assert not awaiter.done(), "published a refusal nothing durable records"
                assert store.state_of(waiting.id) == _taskq.ADMITTED
                assert len(lost) >= bridge_mod._STATE_READ_ATTEMPTS, lost
                # The store recovers: the write lands and only then is the awaiter released.
                release.set()
                await await_until(awaiter)
                assert store.state_of(waiting.id) == _taskq.FAILED
                assert waiting.id not in (getattr(manager, "_pending_defers", None) or {})
        finally:
            await manager.cancel_all()
            manager._taskq.close()

    @pytest.mark.asyncio
    async def test_a_write_the_store_refuses_for_one_row_does_not_hold_the_dispatcher(
        self, monkeypatch, caplog
    ) -> None:
        """The dispatcher's wait on a refused row's settle ends at the read bound.

        The store converts every ``sqlite3.Error`` into ``TaskStoreUnavailable``,
        so a write that fails for ONE row (a damaged page, a constraint) while
        every other row's claim succeeds is indistinguishable from an outage to
        the settle. The dispatcher runs the picked rows one at a time, so a hold
        with no bound on its await would stop every other row for as long as
        that one write kept failing. Past ``_STATE_READ_ATTEMPTS`` tries the
        dispatcher moves on: the next row is claimed and dispatched while the
        refused row stays out of the refill and its terminal record stays
        unregistered until the write lands. Nothing is published early -- the
        caller's own hold (``spawn_async``) is unchanged and tested above, and
        the drained non-batch refusal's announce to the parent (``_on_done``,
        scheduled by ``_after_dispatch_impl``) keeps the full hold too: it is
        the refusal published, so it fires only once the row reads ``failed``.
        """
        from kiro_crew import taskq as _taskq
        from kiro_crew.subagent_manager import admission as admission_mod
        from kiro_crew.subagent_manager.admission import taskq_bridge as bridge_mod
        from kiro_crew.subagent_persistence import read_tombstone

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        monkeypatch.setattr(bridge_mod, "_SETTLE_RETRY_BACKOFF_CAP_SECS", 0.02)
        announced: list[tuple[str, str | None]] = []

        async def _on_done(info: SubagentInfo) -> None:
            announced.append((info.id, store.state_of(info.id)))

        manager = _store_manager()
        manager._on_done = _on_done
        await manager.wait_taskq_ready()
        manager._spawn_stagger_secs = 0.0
        store = manager._admission.taskq_store()
        assert store is not None, "this pin needs the real durable store"
        release = threading.Event()
        real_finish = type(store).finish
        refused_id: list[str] = []

        def _finish_refusing_one_row(self_store, task_id, state, **kw):  # type: ignore[no-untyped-def]
            if state == _taskq.FAILED and task_id in refused_id and not release.is_set():
                raise _taskq.TaskStoreUnavailable("database disk image is malformed")
            return real_finish(self_store, task_id, state, **kw)

        # The disk refuses this row's tombstone too (the writer answers False
        # for its own failures): no durable verdict, so the caller's hold has
        # no bound -- the case the dispatcher must not share.
        real_tombstone = type(manager._admission)._tombstone_refused_row

        def _tombstone_lost_for_one_row(agent_id: str, reason: str) -> bool:
            if agent_id in refused_id:
                return False
            return real_tombstone(agent_id, reason)

        try:
            with (
                patch("kiro_crew.subagent.Stats"),
                patch("kiro_crew.subagent.sel"),
                patch.object(manager, "_run", _park),
                patch.object(type(store), "finish", _finish_refusing_one_row),
                patch.object(
                    type(manager._admission),
                    "_tombstone_refused_row",
                    staticmethod(_tombstone_lost_for_one_row),
                ),
                caplog.at_level(logging.WARNING, logger="kiro_crew.subagent_manager.admission"),
            ):
                manager.spawn("occupy", parent_session_key=ROOT)
                bad = manager.spawn("bad", parent_session_key=ROOT)
                good = manager.spawn("good", parent_session_key=ROOT)
                assert bad is not None and bad.queued and good is not None and good.queued
                refused_id.append(bad.id)
                await async_wait_until(lambda: store.state_of(good.id) == _taskq.QUEUED)
                bad_params = next(p for p in manager._queue if p["_preassigned_id"] == bad.id)
                manager._queue.remove(bad_params)
                bad_params["cwd"] = "/tmp/elsewhere"  # disallowed by the time the drain re-checks
                manager._running_count = 0
                with patch(
                    "kiro_crew.subagent.validate_cwd",
                    return_value=("", "cwd is outside every allowed root"),
                ):
                    dispatch = asyncio.create_task(
                        manager._admission._dispatch_async_impl(bad_params)
                    )
                    # The settle is past the read bound and still retrying...
                    held = await _await_log(caplog, "holding its refusal")
                    assert len(held) == 1, [r.getMessage() for r in caplog.records]
                    assert read_tombstone(bad.id) is None
                    # ...and the dispatcher has moved on: its answer is the refusal.
                    first = await await_until(dispatch)
                assert first is not None and first.done and first.error, first
                # The row is NOT published: still QUEUED in the store (the
                # drained branch refuses before the claim), out of the refill,
                # with no terminal record registered yet, and the parent has
                # not heard the refusal -- the pump's announce task, scheduled
                # by the drain's after-dispatch step, waits for the write.
                assert store.state_of(bad.id) == _taskq.QUEUED
                assert bad.id in manager._admission.taskq_dispatch_excluded_ids()
                assert bad.id not in manager._agents
                manager._admission._after_dispatch_impl(bad_params, first, refill=lambda **_kw: 0)
                announce = manager._tasks[f"reject-{bad.id}"]
                await _await_joined(announce)
                assert announced == [], "the parent heard the refusal before its row settled"
                # The store is healthy for every other row: the next one claims
                # and starts while the bad row's write is still being retried.
                good_params = next(p for p in manager._queue if p["_preassigned_id"] == good.id)
                manager._queue.remove(good_params)
                started = await manager._admission._dispatch_async_impl(good_params)
                assert started is not None and not started.done and started.id == good.id
                assert good.id in manager._tasks
                assert store.state_of(good.id) in (_taskq.ADMITTED, _taskq.STARTING, _taskq.RUNNING)
                # The bad row's write lands once the store takes it: settled and
                # registered as the terminal record, nothing left pending.
                release.set()
                await async_wait_until(lambda: store.state_of(bad.id) == _taskq.FAILED)
                await async_wait_until(lambda: bad.id in manager._agents)
                assert manager._agents[bad.id].error == first.error
                await async_wait_until(
                    lambda: bad.id not in (getattr(manager, "_pending_defers", None) or {})
                )
                assert bad.id not in manager._admission.taskq_dispatch_excluded_ids()
                await await_until(announce)
                assert announced == [(bad.id, _taskq.FAILED)], announced
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
            # Held until the test releases it. A bound this long is a stuck
            # test, not a slow runner, and an expiry FAILS the hog (and so
            # ``await hog``) rather than freeing the writer under the
            # ADMITTED assertion below.
            if not writer_release.wait(120):
                raise TimeoutError("the writer blockade was never released")

        try:
            with (
                patch("kiro_crew.subagent.Stats"),
                patch("kiro_crew.subagent.sel"),
                patch.object(manager, "_run", _park),
            ):
                manager.spawn("occupy", parent_session_key=ROOT)
                waiting = manager.spawn("waiting", parent_session_key=ROOT)
                assert waiting is not None and waiting.queued
                await async_wait_until(lambda: store.state_of(waiting.id) == _taskq.QUEUED)
                assert store.claim(waiting.id) is not None
                assert store.state_of(waiting.id) == _taskq.ADMITTED
                hog = asyncio.ensure_future(store.run(_hog))
                await async_wait_until(writer_busy.is_set)
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
                await await_until(settle)
            assert store.state_of(waiting.id) == _taskq.FAILED, store.state_of(waiting.id)
            assert waiting.id not in (getattr(manager, "_pending_defers", None) or {})
        finally:
            writer_release.set()
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
                await async_wait_until(lambda: store.state_of(waiting.id) == _taskq.QUEUED)
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
                await async_wait_until(lambda: read_tombstone(waiting.id))
                assert read_tombstone(waiting.id), "the settle did not write its tombstone"
                assert not settle.done()
                settle.cancel()
                with suppress(asyncio.CancelledError):
                    await settle
                # The store never heard the failure...
                assert store.state_of(waiting.id) == _taskq.ADMITTED
                # ...but the run's ending is on disk, and the probe reads it as the refusal.
                tombstone = read_tombstone(waiting.id) or {}
                assert tombstone.get("cause") == REFUSAL_TOMBSTONE_CAUSE, tombstone
                assert REFUSAL in str(tombstone.get("detail", "")), tombstone
                rec = store.get(waiting.id)
                assert rec is not None
                assert manager._admission.taskq_artifact_probe(rec) == REFUSED
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
                await async_wait_until(
                    lambda: store.state_of(refused.id)
                    == store.state_of(untouched.id)
                    == _taskq.QUEUED
                )
                # Refused without a claim, as the memory-pressure expiry refuses.
                manager._admission.taskq_fail(refused.id, REFUSAL, memory_mode="persistent")
                settle = (getattr(manager, "_pending_defers", None) or {})[refused.id]
                await async_wait_until(lambda: read_tombstone(refused.id))
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
                    first = await await_until(
                        asyncio.ensure_future(manager._admission._dispatch_async_impl(params))
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
                    first = await await_until(
                        asyncio.ensure_future(manager._admission._dispatch_async_impl(params))
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


class TestPendingWriteExclusion:
    """A row whose settle or defer write is pending is the REFILL's to skip, not a Stop's.

    ``taskq_dispatch_excluded_ids`` feeds the pump's reads and must keep such a
    row out until its write lands. ``taskq_excluded_ids`` feeds the cancellation
    and pending-work reads (``taskq_pending_ids_for_async``) and must still name
    it: a parent's Stop in that window has to reach the row, or the stop reports
    done over a row that starts later.
    """

    @pytest.mark.asyncio
    async def test_a_pending_write_hides_its_row_from_the_refill_only(self) -> None:
        manager = _store_manager()
        try:
            manager._pending_defers = {"r1": asyncio.get_running_loop().create_future()}
            admission = manager._admission
            assert "r1" in admission.taskq_dispatch_excluded_ids()
            assert "r1" not in admission.taskq_excluded_ids()
            assert "r1" not in admission.taskq_excluded_ids(window=False)
            # The chip's count still sees a row it is told to count.
            assert "r1" not in admission.taskq_dispatch_excluded_ids(counted={"r1"})
        finally:
            await manager.cancel_all()


class TestRefusalTombstoneRetention:
    """A refusal tombstone outlives the prune window while its row is unsettled.

    Once a refusal is published on its tombstone (the store still refusing the
    ``failed`` write), that file is the only durable record of the refusal and
    the verdict the next boot settles the row on. The reaper's prune keeps it
    while the row is not terminal -- or while the store cannot say -- and prunes
    it on its window once the row reads terminal, so the folder does not live
    forever either.
    """

    @pytest.mark.asyncio
    async def test_a_stale_refusal_tombstone_is_kept_until_its_row_is_terminal(
        self, tmp_path, monkeypatch
    ) -> None:
        import json
        import time

        from kiro_crew import taskq as _taskq
        from kiro_crew.subagent import write_refusal_tombstone
        from kiro_crew.subagent_persistence import create_agent_folder, prune_stale_tombstones

        monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path)
        manager = _store_manager()
        await manager.wait_taskq_ready()
        store = manager._admission.taskq_store()
        assert store is not None, "this pin needs the real durable store"
        try:
            # A refused persistent run: its row QUEUED (the write never landed),
            # its refusal tombstone on disk and already past the 7-day window --
            # the tombstone's own ``died`` stamp is aged, the clock is real.
            (rid,) = store.accept([_taskq.TaskRecord(id="r1", kind=_taskq.KIND_SUBAGENT)])
            create_agent_folder(rid, task="t", parent_session=ROOT)
            write_refusal_tombstone(rid, REFUSAL)
            ts_path = tmp_path / rid / "tombstone.json"
            ts = json.loads(ts_path.read_text(encoding="utf-8"))
            ts["died"] = time.time() - 8 * 86400
            ts_path.write_text(json.dumps(ts), encoding="utf-8")
            keep = manager._monitor._refusal_tombstone_unsettled
            assert prune_stale_tombstones(7, 3600, keep) == 0
            assert ts_path.exists()
            # The store cannot answer: the tombstone is still kept.
            with patch.object(
                type(store), "state_of", side_effect=_taskq.TaskStoreUnavailable("locked")
            ):
                assert prune_stale_tombstones(7, 3600, keep) == 0
            # The store is required but not open (this boot is still opening
            # it, or the open failed and the reaper will retry): kept, since the
            # reopen's reconcile is what reads the marker.
            with (
                patch.object(manager, "_taskq", None),
                patch.object(manager, "_taskq_unavailable", "durable task queue is initializing"),
            ):
                assert prune_stale_tombstones(7, 3600, keep) == 0
            # The queue deliberately off: no store will ever read it, so it goes
            # on its window like any other tombstone.
            with (
                patch.object(manager, "_taskq", None),
                patch.object(manager, "_taskq_unavailable", None),
            ):
                assert (
                    manager._monitor._refusal_tombstone_unsettled(rid, {"cause": "refused"})
                    is False
                )
            # The row settles: the tombstone goes on its window like any other.
            assert store.transition(rid, _taskq.FAILED)
            assert prune_stale_tombstones(7, 3600, keep) == 1
            assert not (tmp_path / rid).exists()
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
        assert kwargs["cause"] == REFUSAL_TOMBSTONE_CAUSE
        assert "AKIAIOSFODNN7EXAMPLE" not in kwargs["detail"]
        assert len(kwargs["detail"]) <= MAX_ERROR_DETAIL_LEN


class TestBootSettleHeldUntilWritten:
    """A ``queued`` row whose boot settle the store refused is not dispatchable.

    The queued pass reads a refusal's marker and writes ``failed``; if THAT
    write raises ``TaskStoreUnavailable`` the report records it and
    ``open_default_store`` returns the store regardless -- which is correct for
    boot, and would be a run its caller was told was refused if the pump then
    claimed the row. So the store holds the row out of every claim and lands
    the write at the first dispatch read that can, before any row is handed
    to the dispatcher (persist before publish).
    """

    def test_a_held_row_is_excluded_until_its_terminal_write_lands(self, tmp_path) -> None:
        from overload_fakes import Clock
        from overload_fakes import task_record as _rec

        from kiro_crew import taskq as _taskq
        from kiro_crew.taskq.reconcile import reconcile_on_boot
        from kiro_crew.taskq.store import TaskStore

        clock = Clock(9000.0)
        path = tmp_path / "t.db"
        TaskStore(path, clock=clock, network_fs=False).open().accept(
            [_rec("refused"), _rec("plain")]
        )
        store = TaskStore(path, clock=clock, network_fs=False).open()
        verdicts = {"refused": REFUSED, "plain": None}
        down = {"on": True}
        real_transition = type(store).transition

        def _transition(self_store, task_id, state, **kw):  # type: ignore[no-untyped-def]
            if down["on"] and task_id == "refused":
                raise _taskq.TaskStoreUnavailable("database is locked")
            return real_transition(self_store, task_id, state, **kw)

        try:
            with patch.object(type(store), "transition", _transition):
                report = reconcile_on_boot(store, artifact_probe=lambda r: verdicts[r.id])
                assert report.held == 1 and len(report.errors) == 1, report
                # The write never landed: the row IS still queued on disk...
                assert store.state_of("refused") == _taskq.QUEUED
                assert store.held_until_settled() == ["refused"]
                # ...and still the dispatcher cannot see or take it, while the
                # untouched row beside it is handed out as usual.
                assert [r.id for r in store.fetch_dispatchable(_taskq.KIND_SUBAGENT, limit=8)] == [
                    "plain"
                ]
                assert store.claim("refused") is None
                assert store.claim_next(_taskq.KIND_SUBAGENT).record.id == "plain"
                assert store.held_until_settled() == ["refused"]
                # The store answers again: the next dispatch read lands the
                # ending first, and the row is published to nobody.
                down["on"] = False
                assert store.fetch_dispatchable(_taskq.KIND_SUBAGENT, limit=8) == []
            assert store.state_of("refused") == _taskq.FAILED
            assert store.held_until_settled() == []
            assert store.claim("refused") is None
        finally:
            store.close()

    @pytest.mark.parametrize("verdict", ["done", "failed", "cancelled", "started"])
    def test_an_earlier_attempts_ending_does_not_settle_a_queued_row(
        self, tmp_path, verdict: str
    ) -> None:
        """Only the refusal marker settles a ``queued`` row.

        A row comes back to ``queued`` after an attempt ran: a dependency wake
        makes a parked ``retry_wait`` row claimable again, a lost-owner requeue
        puts an ``admitted`` one back. That attempt's tombstone or whole result
        on disk is an ending of the ATTEMPT, not of the row the dispatcher is
        about to run again, and the queued pass leaves such a row exactly as
        it was: not settled, not held, dispatchable.
        """
        from overload_fakes import Clock
        from overload_fakes import task_record as _rec

        from kiro_crew import taskq as _taskq
        from kiro_crew.taskq.reconcile import reconcile_on_boot
        from kiro_crew.taskq.store import TaskStore

        clock = Clock(9000.0)
        path = tmp_path / "t.db"
        TaskStore(path, clock=clock, network_fs=False).open().accept([_rec("again")])
        store = TaskStore(path, clock=clock, network_fs=False).open()
        try:
            report = reconcile_on_boot(store, artifact_probe=lambda r: verdict)
            assert report.queued_examined == 1, report
            assert report.changed == 0 and report.held == 0 and not report.errors, report
            assert store.state_of("again") == _taskq.QUEUED
            assert store.held_until_settled() == []
            assert [r.id for r in store.fetch_dispatchable(_taskq.KIND_SUBAGENT, limit=8)] == [
                "again"
            ]
        finally:
            store.close()


class TestBootQueuedPassIsBounded:
    """The queued pass walks the backlog one dispatch window at a time.

    A backlog left behind under admission pressure can be large; reading it in
    one ``fetchall`` at boot would hold every queued payload at once, past the
    bound the dispatch window puts on everything else. The pass pages by
    ``(created_at, id)`` with the window as the page size, examines every
    queued row exactly once, and runs before the active pass so a row that pass
    requeues is not read a second time.
    """

    def test_the_backlog_is_read_one_window_at_a_time_and_each_row_once(self, tmp_path) -> None:
        from overload_fakes import Clock
        from overload_fakes import task_record as _rec

        from kiro_crew import taskq as _taskq
        from kiro_crew.taskq.reconcile import reconcile_on_boot
        from kiro_crew.taskq.store import TaskStore

        clock = Clock(9000.0)
        path = tmp_path / "t.db"
        first = TaskStore(path, clock=clock, window=2, network_fs=False).open()
        # Five queued rows across three distinct ``created_at`` values, so the
        # keyset crosses both an equal-timestamp tie and a timestamp step...
        first.accept([_rec("q1"), _rec("q2")])
        clock.t += 1
        first.accept([_rec("q3"), _rec("q4")])
        clock.t += 1
        first.accept([_rec("q5")])
        # ...plus an ADMITTED row the active pass will requeue, with no runtime.
        first.accept([_rec("lost", side_effect_class=_taskq.SIDE_EFFECT_NONE)])
        assert first.claim("lost") is not None
        first.close()

        store = TaskStore(path, clock=clock, window=2, network_fs=False).open()
        pages: list[int] = []
        real = type(store).queued_rows

        def _counting(self_store, **kw):  # type: ignore[no-untyped-def]
            page = real(self_store, **kw)
            pages.append(len(page))
            return page

        seen: list[str] = []

        def _probe(rec):  # type: ignore[no-untyped-def]
            seen.append(rec.id)
            return None

        try:
            with patch.object(type(store), "queued_rows", _counting):
                report = reconcile_on_boot(store, artifact_probe=_probe)
            assert pages == [2, 2, 1], pages
            assert sorted(seen) == ["lost", "q1", "q2", "q3", "q4", "q5"], seen
            assert report.queued_examined == 5 and report.requeued == 1, report
            assert store.state_of("lost") == _taskq.QUEUED
            # The active pass probed ``lost`` once before requeueing it; the
            # queued pages (2 + 2 + 1) held the five queued rows and not it.
        finally:
            store.close()

    def test_a_refused_retry_wait_row_is_settled_on_boot_too(self, tmp_path) -> None:
        """The refusal-only boot pass reads ``retry_wait`` rows, not ``queued`` alone.

        A start the claimer took but did not carry to registration is given back
        as ``starting -> retry_wait`` (``_release_taken_start``), and the refill
        claims a ``retry_wait`` row exactly as it claims a ``queued`` one. A row
        refused in that state whose ``failed`` write never lands is held in
        memory only; a restart drops the hold, and a boot pass that read
        ``queued`` rows alone would leave the refused row for the dispatcher to
        run. The pass settles it on its refusal marker and leaves a
        ``retry_wait`` row whose artifacts say nothing exactly as it was.
        """
        from overload_fakes import Clock
        from overload_fakes import task_record as _rec

        from kiro_crew import taskq as _taskq
        from kiro_crew.subagent_manager.admission import taskq_bridge as bridge_mod
        from kiro_crew.taskq.reconcile import REFUSED, reconcile_on_boot
        from kiro_crew.taskq.store import TaskStore

        clock = Clock(9000.0)
        path = tmp_path / "t.db"
        first = TaskStore(path, clock=clock, window=4, network_fs=False).open()
        first.accept([_rec("refused"), _rec("parked")])
        for task_id in ("refused", "parked"):
            claimed = first.claim(task_id)
            assert claimed is not None
            assert first.transition(task_id, _taskq.STARTING, generation=claimed.generation)
            assert bridge_mod._TaskqBridgeMixin._release_taken_start(
                first, task_id, claimed.generation
            )
            assert first.state_of(task_id) == _taskq.RETRY_WAIT
        first.close()

        store = TaskStore(path, clock=clock, window=4, network_fs=False).open()
        try:
            report = reconcile_on_boot(
                store, artifact_probe=lambda rec: REFUSED if rec.id == "refused" else None
            )
            assert report.queued_examined == 2, report
            assert report.settled_failed == 1 and report.requeued == 0, report
            assert store.state_of("refused") == _taskq.FAILED
            assert store.state_of("parked") == _taskq.RETRY_WAIT
        finally:
            store.close()


class TestBootQueuedPassFencesDispatch:
    """A queued pass whose READ the store refuses fences the subagent dispatch.

    A row the pass never examined may owe the terminal write a hold exists for,
    and a hold needs the row's id, so until the pass has run to completion no
    dispatch read publishes a ``subagent`` row and no claim on one lands. Every
    dispatch read re-runs the pass first; the first one the store lets finish
    settles what the artifacts say and lifts the fence. Other kinds are not
    fenced: the refusal marker the pass settles on is written for a refused
    spawn alone, so no other kind can owe it. The fence logs when it engages.
    """

    def test_no_row_is_published_until_the_queued_pass_completes(self, tmp_path, caplog) -> None:
        from overload_fakes import Clock
        from overload_fakes import task_record as _rec

        from kiro_crew import taskq as _taskq
        from kiro_crew.taskq import model as _model
        from kiro_crew.taskq.reconcile import reconcile_on_boot
        from kiro_crew.taskq.store import TaskStore

        clock = Clock(9000.0)
        path = tmp_path / "t.db"
        TaskStore(path, clock=clock, network_fs=False).open().accept(
            [_rec("refused"), _rec("plain"), _rec("cron-row", kind=_model.KIND_CRON)]
        )
        store = TaskStore(path, clock=clock, network_fs=False).open()
        verdicts = {"refused": REFUSED, "plain": None, "cron-row": None}
        down = {"on": True}
        real = type(store).queued_rows

        def _queued_rows(self_store, **kw):  # type: ignore[no-untyped-def]
            if down["on"]:
                raise _taskq.TaskStoreUnavailable("database is locked")
            return real(self_store, **kw)

        try:
            with (
                patch.object(type(store), "queued_rows", _queued_rows),
                caplog.at_level(logging.WARNING, logger="kiro_crew.taskq.store"),
            ):
                report = reconcile_on_boot(store, artifact_probe=lambda r: verdicts[r.id])
                assert report.queued_examined == 0
                assert report.errors and "queued pass" in report.errors[0], report
                # The operator can see the fence engage.
                assert any("dispatch fenced" in r.getMessage() for r in caplog.records), [
                    r.getMessage() for r in caplog.records
                ]
                # Nothing was examined, so no subagent row is published: not
                # even the one whose artifacts say nothing. A claim on one is
                # the store's typed failure, not a refusal: the caller leaves
                # the row queued instead of reporting the spawn stopped.
                assert store.fetch_dispatchable(_taskq.KIND_SUBAGENT, limit=8) == []
                with pytest.raises(_taskq.TaskStoreUnavailable, match="fenced"):
                    store.claim("plain")
                assert store.claim_next(_taskq.KIND_SUBAGENT) is None
                assert store.state_of("refused") == _taskq.QUEUED
                assert store.state_of("plain") == _taskq.QUEUED
                # A row of another kind owes no refusal marker and is not held
                # back by the subagent fence.
                assert [r.id for r in store.fetch_dispatchable(_model.KIND_CRON, limit=8)] == [
                    "cron-row"
                ]
                assert store.claim("cron-row") is not None
                # The store answers again: the next dispatch read runs the pass
                # to completion first -- the refusal lands -- and then publishes
                # only the row with nothing recorded against it.
                down["on"] = False
                assert [r.id for r in store.fetch_dispatchable(_taskq.KIND_SUBAGENT, limit=8)] == [
                    "plain"
                ]
            assert store.state_of("refused") == _taskq.FAILED
            assert report.queued_examined == 2 and report.settled_failed == 1, report
            assert store.claim("plain") is not None
        finally:
            store.close()

    def test_a_kind_lookup_the_store_refuses_is_the_stores_typed_failure(self, tmp_path) -> None:
        """``claim``'s kind read raises ``TaskStoreUnavailable``, never a raw sqlite error.

        The read that decides whether the fence applies runs before the claim's
        own transaction; a busy-timeout lock or an I/O error there must reach the
        dispatcher as the store's typed failure, which is the one exception the
        pump's claim path handles (and releases the reservation on).
        """
        import sqlite3

        from overload_fakes import Clock
        from overload_fakes import task_record as _rec

        from kiro_crew import taskq as _taskq
        from kiro_crew.taskq.store import TaskStore

        class _LockedKindRead:
            def __init__(self, real: sqlite3.Connection) -> None:
                self._real = real

            def execute(self, sql: str, *args):  # type: ignore[no-untyped-def]
                if sql.lstrip().upper().startswith("SELECT KIND FROM TASKS"):
                    raise sqlite3.OperationalError("database is locked")
                return self._real.execute(sql, *args)

            def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
                return getattr(self._real, name)

        store = TaskStore(tmp_path / "t.db", clock=Clock(9000.0), network_fs=False).open()
        try:
            store.accept([_rec("row")])
            real = store._conn
            assert real is not None
            store._conn = _LockedKindRead(real)  # type: ignore[assignment]
            with pytest.raises(_taskq.TaskStoreUnavailable):
                store.claim("row")
            store._conn = real
            assert store.state_of("row") == _taskq.QUEUED
            assert store.claim("row") is not None
        finally:
            store.close()
