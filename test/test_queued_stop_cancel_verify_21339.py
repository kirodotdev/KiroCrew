"""A queued stop whose store cancel is refused under contention still settles the row.

A queued stop whose store cancel is refused under contention still settles the row.

When a queued (unstarted) subagent is stopped and its window entry is popped,
the stop is published to the caller and the row's store ``cancel`` is expected
to land. If nothing is cancelled -- the store is unreachable, or the row left
the unstarted states between the read and the write --
``_repost_unlanded_cancel`` retries the cancel, verified: it reads the answer of
each attempt and keeps trying under a small capped backoff while the store
refuses, so a store still contended on a retry does not leave the row ``queued``
and dispatchable. Without that, the next incarnation's dispatcher runs work the
user was told had stopped.

This drives the real single-run stop path (``cancel``) on a WINDOWED,
store-backed queued row -- the row held in the stagger window behind a full
concurrency slot, the ``_unqueue`` -> ``_repost_unlanded_cancel`` site -- with a
store whose terminal writes are refused for the first attempts. It asserts the
row ends ``cancelled`` and is not dispatchable: the verified retry must drive it
to a terminal state despite the refusals.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from overload_fakes import mock_ctx, mock_sessions, settle_store_writes, wait_taskq_open

import kiro_crew.subagent as subagent_mod
from kiro_crew import taskq
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.subagent import SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator

pytestmark = pytest.mark.usefixtures("close_subagent_managers", "healthy_host_memory")

PARENT = "dashboard:alice"


async def _windowing_manager(monkeypatch) -> tuple[SubagentManager, list[str]]:
    """A manager with one slot and a real store: the second spawn is WINDOWED.

    The first spawn fills the single concurrency slot and blocks; the second is
    accepted into the store and held in the stagger window (``_queue``), so it
    has both a window entry AND a backing store row -- the state
    ``_repost_unlanded_cancel`` is reached from. ``started`` is every run that
    started, in order.
    """
    cfg = KiroCrewConfig()
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(subagent_mod, "Stats", MagicMock())
    monkeypatch.setattr(subagent_mod, "sel", MagicMock())
    monkeypatch.setattr(subagent_mod, "list_agents", lambda: [SimpleNamespace(name="kirocrew")])
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    # Plenty of memory: nothing is memory-deferred, so a held spawn is windowed
    # rather than parked store-only.
    monkeypatch.setattr(subagent_mod, "check_memory_available", lambda **_kw: (True, 64.0))
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=1)
    await wait_taskq_open(mgr)
    # A non-zero stagger holds the second row in ``_queue`` while the single
    # slot stays full, so it stays windowed and never drains into a start.
    mgr._spawn_stagger_secs = 3600.0
    started: list[str] = []

    async def worker(info) -> None:
        started.append(info.id)
        await asyncio.sleep(3600)

    monkeypatch.setattr(mgr, "_run", AsyncMock(side_effect=worker))
    return mgr, started


async def _close(mgr: SubagentManager) -> None:
    mgr._shutting_down = True
    tasks = [task for task in mgr._tasks.values() if not task.done()]
    for task in tasks:
        task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
    mgr._taskq.close()


def _refuse_writes_n_times(monkeypatch, store, n: int) -> dict:
    """Refuse the row's terminal writes ``TaskStoreUnavailable`` *n* times.

    Patches BOTH ``store.cancel`` and ``store.finish`` -- the two ways a queued
    stop writes a row terminal -- so a shared counter models one store outage
    under contention: neither the cancel nor the queued-stop settle's ``finish``
    lands while it holds. The first *n* write attempts raise; every write after
    that commits. ``store.get``/``store.state_of`` are left alone, so the row
    still reads as an unstarted row that ought to cancel. ``calls`` counts the
    write attempts, so a test can assert the cancel is retried past the first
    refusals rather than attempted once and dropped. This models the store
    contention under which the same condition that causes the first miss still
    holds on a retry, so an unverified single retry would leave the row
    ``queued``.
    """
    from kiro_crew import taskq as _taskq

    counter = {"calls": 0}
    original_cancel = store.cancel
    original_finish = store.finish

    def flaky_cancel(task_id, **kwargs):
        counter["calls"] += 1
        if counter["calls"] <= n:
            raise _taskq.TaskStoreUnavailable("test: writer-thread contention")
        return original_cancel(task_id, **kwargs)

    def flaky_finish(task_id, *args, **kwargs):
        counter["calls"] += 1
        if counter["calls"] <= n:
            raise _taskq.TaskStoreUnavailable("test: writer-thread contention")
        return original_finish(task_id, *args, **kwargs)

    monkeypatch.setattr(store, "cancel", flaky_cancel)
    monkeypatch.setattr(store, "finish", flaky_finish)
    return counter


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_queued_stop_whose_cancel_is_refused_still_settles_the_row(
    monkeypatch,
) -> None:
    """The windowed row must end ``cancelled``, never left ``queued`` and dispatchable.

    The first cancel attempts are refused (the initial cancel and the first
    retries hit the same contention). The verified retry reads each answer and
    keeps trying under the capped backoff, driving the row to ``cancelled`` once
    the store heals; an unverified single retry would leave it ``queued``.
    """
    mgr, started = await _windowing_manager(monkeypatch)
    try:
        first = await mgr.spawn_async("the running one", parent_session_key=PARENT)
        assert first is not None
        # Let the first spawn claim the single slot and start.
        for _ in range(250):
            if first.id in started:
                break
            mgr._drain_queue()
            await asyncio.sleep(0.02)
        assert first.id in started, "fixture: the first run never started"

        second = await mgr.spawn_async("the one we stop", parent_session_key=PARENT)
        assert second is not None and second.queued is True
        # Fixture invariant: the second row is WINDOWED (in _queue) AND has a
        # backing store row -- the exact state _repost_unlanded_cancel serves.
        assert any(
            str(p.get("_preassigned_id") or "") == second.id for p in mgr._queue
        ), "fixture: the second run is not in the stagger window"
        row = await mgr._taskq.run(mgr._taskq.get, second.id)
        assert row is not None and not row.terminal, "fixture: no live store row for the second run"

        # Refuse the first three terminal writes: the initial cancel, the
        # queued-stop settle's finish, AND one retry -- so a single unverified
        # retry would already be spent before the store heals, the contention
        # condition this test exercises.
        counter = _refuse_writes_n_times(monkeypatch, mgr._taskq, n=3)

        stopped = await mgr.cancel(second.id)
        assert stopped is True, "the queued run was not reported stopped"

        # The row must reach a terminal CANCELLED state despite the refused
        # attempts -- it is NOT left ``queued`` for the next dispatcher. The
        # verified retry runs as a tracked task, so settle for it.
        row = None
        for _ in range(250):
            row = await mgr._taskq.run(mgr._taskq.get, second.id)
            if row is not None and row.terminal:
                break
            await settle_store_writes(mgr._taskq)
            await asyncio.sleep(0.02)
        assert row is not None, "the row vanished"
        assert row.state == taskq.CANCELLED, (
            f"row left {row.state!r}: the refused cancel was not retried to a "
            "terminal state, so the next dispatcher would run a stopped run"
        )
        assert counter["calls"] >= 3, (
            "the cancel was not retried past the refusals -- an unverified "
            "single retry leaves the row dispatchable"
        )
        # A row in terminal CANCELLED state is, by the store's own transition
        # rules, neither claimable nor dispatchable: `_drain_queue` and the
        # claim only ever take CLAIMABLE/QUEUED rows, and `taskq_cancel_queued`
        # left the row terminal under the one transaction that tested it was
        # unstarted. The CANCELLED assertion above is that proof; a drain loop
        # here would only re-assert it against a clock, so it is omitted.
    finally:
        await _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_wedged_store_fails_the_stop_safe_rather_than_reporting_it(
    monkeypatch,
) -> None:
    """A store wedged through every retry must NOT report a stop that did not land.

    Publishing a queued stop on an unverified cancel is the defect: the caller
    is told the run stopped while the row stays ``queued`` and would run after a
    restart. When the verified retry exhausts without the cancel landing,
    ``cancel`` reports no stop, answers ``False``, and leaves the row intact for
    a later attempt -- fail safe, not falsely settled.
    """
    mgr, started = await _windowing_manager(monkeypatch)
    try:
        first = await mgr.spawn_async("the running one", parent_session_key=PARENT)
        assert first is not None
        for _ in range(250):
            if first.id in started:
                break
            mgr._drain_queue()
            await asyncio.sleep(0.02)
        assert first.id in started, "fixture: the first run never started"

        second = await mgr.spawn_async("the one we stop", parent_session_key=PARENT)
        assert second is not None and second.queued is True
        row = await mgr._taskq.run(mgr._taskq.get, second.id)
        assert row is not None and not row.terminal, "fixture: no live store row for the second run"

        # Refuse every terminal write for the whole stop: the store never heals,
        # so no retry can land the cancel.
        counter = _refuse_writes_n_times(monkeypatch, mgr._taskq, n=10_000)

        stopped = await mgr.cancel(second.id)
        assert stopped is False, "a stop was reported on a cancel that never landed"

        # No queued-stop terminal record was published for the row.
        record = mgr._agents.get(second.id)
        assert (
            record is None or not record.queued
        ), "a queued-stop terminal was published although the cancel never landed"
        # The row is still queued (not falsely settled), so a later attempt --
        # or the next incarnation once the store heals -- can still stop it.
        row = await mgr._taskq.run(mgr._taskq.get, second.id)
        assert row is not None and not row.terminal and row.state == taskq.QUEUED, (
            f"row is {row.state if row else 'gone'!r}: a wedged-store stop must leave "
            "it queued, not falsely settled"
        )
        assert counter["calls"] >= 2, "the cancel was attempted fewer times than expected"
    finally:
        await _close(mgr)


async def _deferring_manager(monkeypatch) -> tuple[SubagentManager, dict]:
    """A manager whose memory floor defers every spawn to a STORE-ONLY row.

    The deferred row lives only in the task store -- no ``_queue`` window entry
    -- which is the case a verified retry must still report through when its
    cancel lands, or the parent is left waiting for its last child.
    """
    cfg = KiroCrewConfig()
    cfg.agent.subagent_cost_gb = 0.5
    cfg.agent.spawn_min_memory_gb = 4.0
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(subagent_mod, "Stats", MagicMock())
    monkeypatch.setattr(subagent_mod, "sel", MagicMock())
    monkeypatch.setattr(subagent_mod, "list_agents", lambda: [SimpleNamespace(name="kirocrew")])
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    free = {"gb": 3.0}
    monkeypatch.setattr(
        subagent_mod,
        "check_memory_available",
        lambda *, min_gb, **_k: (free["gb"] >= min_gb, free["gb"]),
    )
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    await wait_taskq_open(mgr)
    mgr._spawn_stagger_secs = 0.0
    mgr._taskq_admit_wait_secs = 30.0
    monkeypatch.setattr(mgr, "_run", AsyncMock(side_effect=lambda info: asyncio.sleep(3600)))
    return mgr, free


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_store_only_row_whose_retried_cancel_lands_is_still_reported(
    monkeypatch,
) -> None:
    """A store-only queued row (no window entry) still reports its stop after a retry.

    The row lives only in the store, so there is no window entry to publish
    from. When the first cancel is refused and the verified retry then lands,
    the stop must still be reported from the row's params -- otherwise the
    parent waiting on its last child is left parked though the child is
    cancelled.
    """
    mgr, _free = await _deferring_manager(monkeypatch)
    try:
        info = await mgr.spawn_async("summarize the log", parent_session_key=PARENT)
        assert info is not None and info.queued is True
        # Fixture invariant: store-only -- a live store row, NO window entry.
        assert not any(
            str(p.get("_preassigned_id") or "") == info.id for p in mgr._queue
        ), "fixture: the deferred row should be store-only, not windowed"
        row = await mgr._taskq.run(mgr._taskq.get, info.id)
        assert row is not None and not row.terminal, "fixture: no live store row"

        # Refuse the first two terminal writes so the first cancel misses and
        # the verified retry has to land it.
        counter = _refuse_writes_n_times(monkeypatch, mgr._taskq, n=2)

        stopped = await mgr.cancel(info.id)
        assert stopped is True, "a store-only row whose retried cancel landed was not reported"
        assert counter["calls"] >= 3, "the cancel was not retried past the refusal"

        # The stop was published (synthetic queued-stop record exists) and the
        # row is terminal CANCELLED.
        record = mgr._agents.get(info.id)
        assert record is not None and record.queued, "no queued-stop terminal was published"
        row = await mgr._taskq.run(mgr._taskq.get, info.id)
        assert (
            row is not None and row.state == taskq.CANCELLED
        ), f"row is {row.state if row else 'gone'!r}: a landed retry must settle it cancelled"
    finally:
        await _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_windowed_undurable_run_is_stopped_without_a_store_row(
    monkeypatch,
) -> None:
    """A queued run with NO store row (incognito/temporary) is still stopped.

    Incognito and temporary memory modes write no store row even while the
    store is open, yet the run is windowed. Stopping it must pop the window
    entry and report the stop -- it must NOT be routed into the verified cancel
    retry (which has no row to re-read) and answered "not stopped", or the pump
    would start the run the user stopped.
    """
    mgr, started = await _windowing_manager(monkeypatch)
    try:
        first = await mgr.spawn_async("the running one", parent_session_key=PARENT)
        assert first is not None
        for _ in range(250):
            if first.id in started:
                break
            mgr._drain_queue()
            await asyncio.sleep(0.02)
        assert first.id in started, "fixture: the first run never started"

        second = await mgr.spawn_async(
            "the undurable one we stop", parent_session_key=PARENT, _memory_mode="temporary"
        )
        assert second is not None and second.queued is True
        # Fixture invariant: windowed but NO durable store row (undurable mode).
        assert any(
            str(p.get("_preassigned_id") or "") == second.id for p in mgr._queue
        ), "fixture: the undurable run should be windowed"
        row = await mgr._taskq.run(mgr._taskq.get, second.id)
        assert row is None, "fixture: an undurable run must not have a store row"

        stopped = await mgr.cancel(second.id)
        assert stopped is True, "an undurable queued run was not stopped"
        # It is out of the window (will not be dispatched) and its stop was
        # published as a synthetic queued-stop record.
        assert not any(
            str(p.get("_preassigned_id") or "") == second.id for p in mgr._queue
        ), "the undurable run was left in the window and could still be dispatched"
        record = mgr._agents.get(second.id)
        assert record is not None and record.queued, "no queued-stop terminal was published"
    finally:
        await _close(mgr)
