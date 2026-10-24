"""Cancellation while a runner wait is being re-admitted."""

from __future__ import annotations

import asyncio
import random
import threading
from contextlib import suppress
from pathlib import Path

import pytest
from overload_fakes import Clock, open_task_store, settle_store_writes

from kiro_crew.taskq import model as m
from kiro_crew.taskq.adapters import runner as r
from kiro_crew.taskq.dependency import (
    KIND_AUTH_FAILED,
    KIND_RATE_LIMITED,
    DependencyCoordinator,
    DependencySignal,
)
from kiro_crew.taskq.store import TaskStore
from kiro_crew.taskq.waits import WaitRecord
from kiro_crew.testing.wait import async_wait_until, await_until


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> TaskStore:
    yield from open_task_store(tmp_path, clock)


@pytest.mark.asyncio
async def test_dependency_cancel_while_reacquiring_ends_the_woken_row(
    store: TaskStore, clock: Clock
) -> None:
    """A wake's generation bump must not fence out the waiter's own cancel."""
    admission = r.RunnerAdmission(store, lane=r.RunnerLane(1), clock=clock)
    target = await admission.accept_async(
        kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:target"
    )
    assert target is not None
    target_handle = await admission.admit(target.id)
    assert await target_handle.running_async()

    signal = DependencySignal(
        kind=KIND_RATE_LIMITED,
        dependency_scope="provider:test",
        source="test",
        retry_at=clock() + 30,
    )
    waiting = asyncio.create_task(admission.yield_dependency(target_handle, signal))
    await settle_store_writes(store)
    assert (await store.run(store.state_of, target.id)) == m.WAITING_DEPENDENCY

    blocker = await admission.accept_async(
        kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:blocker"
    )
    assert blocker is not None
    blocker_handle = await admission.admit(blocker.id)
    assert await blocker_handle.running_async()

    clock.advance(31)
    assert await asyncio.to_thread(admission.tick) == [target.id]
    await async_wait_until(lambda: admission.lane.waiting == 1)
    row = await store.run(store.get, target.id)
    assert row is not None and row.state == m.RETRY_WAIT
    assert row.generation == target_handle.generation + 1

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await await_until(waiting, what="cancelled dependency waiter")

    row = await store.run(store.get, target.id)
    assert row is not None and row.state == m.CANCELLED
    assert row.lease_owner is None
    assert admission.lane.waiting == 0
    assert await blocker_handle.cancel_async("test cleanup")


@pytest.mark.asyncio
async def test_dependency_cancel_cannot_end_a_later_retry_wait_epoch(
    store: TaskStore, clock: Clock
) -> None:
    """The fallback identifies one wake by both its state and generation."""
    admission = r.RunnerAdmission(store, lane=r.RunnerLane(1), clock=clock)
    target = await admission.accept_async(
        kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:target"
    )
    assert target is not None
    handle = await admission.admit(target.id)
    assert await handle.running_async()

    wait = WaitRecord.dependency("provider:test", since=clock()).to_dict()
    assert await store.run(store.enter_wait, target.id, wait, generation=handle.generation)
    first_wake = await store.run(
        store.wake_wait,
        target.id,
        reason="dependency recovered",
        generation=handle.generation,
    )
    assert first_wake == handle.generation + 1

    claimed = await store.run(store.claim, target.id)
    assert claimed is not None
    assert await store.run(store.transition, target.id, m.STARTING, generation=claimed.generation)
    assert await store.run(store.transition, target.id, m.RUNNING, generation=claimed.generation)
    assert await store.run(store.enter_wait, target.id, wait, generation=claimed.generation)
    second_wake = await store.run(
        store.wake_wait,
        target.id,
        reason="dependency recovered again",
        generation=claimed.generation,
    )
    assert second_wake is not None and second_wake > first_wake

    await asyncio.to_thread(
        admission._cancel_woken_dependency,
        store,
        target.id,
        first_wake,
        "stale dependency cancellation",
    )

    row = await store.run(store.get, target.id)
    assert row is not None and row.state == m.RETRY_WAIT
    assert row.generation == second_wake
    assert await store.run(store.cancel, target.id, reason="test cleanup", generation=second_wake)
    handle.release_slot()


@pytest.mark.asyncio
async def test_dependency_cancel_preserves_an_answered_input_handoff(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a persisted dependency wait licenses the retry-wait fallback."""
    coordinator = DependencyCoordinator(store, clock=clock, rng=random.Random(4))
    admission = r.RunnerAdmission(store, lane=r.RunnerLane(1), clock=clock, coordinator=coordinator)
    target = await admission.accept_async(
        kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:target"
    )
    assert target is not None
    handle = await admission.admit(target.id)
    assert await handle.running_async()

    reacquiring = asyncio.Event()
    release_reacquire = asyncio.Event()

    async def _hold_reacquire() -> None:
        reacquiring.set()
        await release_reacquire.wait()  # flake-ok: the test cancels this held waiter

    monkeypatch.setattr(handle, "reacquire_slot", _hold_reacquire)
    signal = DependencySignal(
        kind=KIND_AUTH_FAILED,
        dependency_scope="provider:test",
        source="test",
    )
    waiting = asyncio.create_task(admission.yield_dependency(handle, signal))
    await await_until(reacquiring, what="authentication wait reacquiring its slot")
    assert (await store.run(store.state_of, target.id)) == m.WAITING_INPUT

    assert await asyncio.to_thread(admission.answer_input, target.id, "approved")
    row = await store.run(store.get, target.id)
    assert row is not None and row.state == m.RETRY_WAIT

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await await_until(waiting, what="cancelled authentication waiter")

    row = await store.run(store.get, target.id)
    assert row is not None and row.state == m.RETRY_WAIT
    assert await admission.recorded_answer_async(target.id) == "approved"
    assert await store.run(
        store.cancel, target.id, reason="test cleanup", generation=row.generation
    )


@pytest.mark.asyncio
async def test_dependency_cancel_after_park_commit_ends_the_woken_row(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait owns its wake even before the store await returns to the loop."""
    admission = r.RunnerAdmission(store, lane=r.RunnerLane(1), clock=clock)
    target = await admission.accept_async(
        kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:target"
    )
    assert target is not None
    handle = await admission.admit(target.id)
    assert await handle.running_async()

    committed = threading.Event()
    release_enter = threading.Event()
    enter_wait = store.enter_wait

    def _enter_then_hold(*args, **kwargs):
        result = enter_wait(*args, **kwargs)
        committed.set()
        if not release_enter.wait(30):
            raise AssertionError("the test did not release the committed wait")
        return result

    monkeypatch.setattr(store, "enter_wait", _enter_then_hold)
    signal = DependencySignal(
        kind=KIND_RATE_LIMITED,
        dependency_scope="provider:test",
        source="test",
        retry_at=clock() + 30,
    )
    waiting = asyncio.create_task(admission.yield_dependency(handle, signal))
    try:
        await async_wait_until(committed.is_set)
        wake_generation = await asyncio.to_thread(
            store.wake_wait,
            target.id,
            reason="dependency recovered",
            generation=handle.generation,
        )
        assert wake_generation == handle.generation + 1

        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await await_until(waiting, what="cancelled dependency park write")
    finally:
        release_enter.set()
        if not waiting.done():
            waiting.cancel()
            with suppress(asyncio.CancelledError):
                await await_until(waiting, what="dependency park cleanup")

    await settle_store_writes(store)
    row = await store.run(store.get, target.id)
    assert row is not None and row.state == m.CANCELLED
