"""Stop and cancel against a claimed row's start: the ``admitted -> starting`` compare-and-set.

Split from ``test_taskq_admission_integration.py`` (the 2,000-line cap). The real admission
coordinator and a real ``TaskStore`` run with a fake worker; each test lands a stop, a cancel
or a store outage at one point between a row's claim and its run's registration.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from overload_fakes import settle_depth_emits, settle_store_writes
from test_taskq_admission_integration import _STOP_PARENT, _manager

from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
from kiro_crew.taskq import model
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable
from kiro_crew.testing.wait import async_wait_until, await_until
from kiro_crew.testing.wait import finished as _finished

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.fixture
def quiet():
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        yield


async def _park_run(_self: SubagentManager, _info: SubagentInfo) -> None:
    """A run that holds its slot until it is stopped: a start is visible as a live task."""
    await asyncio.Event().wait()


async def _popped_row(monkeypatch: pytest.MonkeyPatch) -> tuple[SubagentManager, dict]:
    """A real manager on the production (writer-thread) pump, holding one row
    for :data:`_STOP_PARENT` that the pump has just popped from the window: the
    state the pump is in when it hands the row to ``_dispatch_async``."""
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    mgr = await _manager(max_concurrent=1)
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        # Another parent's run takes the one slot, so this parent's row queues.
        mgr.spawn("occupy", parent_session_key="dash:elsewhere")
        waiting = mgr.spawn("waiting", parent_session_key=_STOP_PARENT)
    assert waiting.queued and not waiting.done
    params = mgr._queue.pop(0)
    assert params["_preassigned_id"] == waiting.id
    mgr._running_count = 0
    return mgr, params


def _record_depths(mgr: SubagentManager) -> list[int]:
    """Every ``subagent_queued`` depth published for :data:`_STOP_PARENT`."""
    depths: list[int] = []

    async def on_event(etype: str, info: Any, extra: dict) -> None:
        if etype == "subagent_queued" and info.parent_session_key == _STOP_PARENT:
            depths.append(int(extra["queued"]))

    mgr._on_event = on_event
    return depths


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("stop_lands", ["read_before_claim", "after_claim"])
async def test_stop_all_between_claim_and_start_keeps_the_row_stopped(
    quiet, monkeypatch: pytest.MonkeyPatch, stop_lands: str
) -> None:
    """A row Stop all stops after the pump claimed it, and before the pump's
    start write took it, ends stopped: it never starts, and the parent's depth
    is 0.

    The pump's claim is a writer-thread hop, so Stop all can run while the
    pump is suspended on it. Explicit barriers, no sleeps, put Stop all in
    each place it can land ahead of the start write:

    * ``read_before_claim``: its store read lists the row while it is still
      queued, and its cancel lands after the claim made it ``admitted``.
    * ``after_claim``: the whole Stop all runs after the claim landed, so its
      read finds a CLAIMED row no run is registered for yet.

    In both, the cancel bumps the generation, so the pump's ``admitted ->
    starting`` compare-and-set is refused and nothing registers. A Stop all
    that lands AFTER that write finds a ``starting`` row its cancel does not
    accept and reaps the registered run as a running one instead
    (``test_a_row_registered_during_stop_alls_read_is_reaped_not_replaced``).
    """
    from kiro_crew.taskq import KIND_SUBAGENT

    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    depths = _record_depths(mgr)
    real_run = store.run
    taskq_claim = mgr._admission.taskq_claim
    at_claim, release_claim = asyncio.Event(), asyncio.Event()
    claimed, resume_after_claim = asyncio.Event(), asyncio.Event()
    listed, release_stop = asyncio.Event(), asyncio.Event()

    async def _gated(fn, /, *args, **kwargs):
        if fn == taskq_claim:
            at_claim.set()
            await release_claim.wait()
            result = await real_run(fn, *args, **kwargs)
            claimed.set()
            await resume_after_claim.wait()
            return result
        if fn == store.list_pending and kwargs.get("session_key") == _STOP_PARENT:
            result = await real_run(fn, *args, **kwargs)
            listed.set()
            await release_stop.wait()
            return result
        return await real_run(fn, *args, **kwargs)

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            dispatch = asyncio.create_task(mgr._admission._dispatch_async_impl(params))
            await await_until(at_claim)
            if stop_lands == "read_before_claim":
                stop = asyncio.create_task(mgr.cancel_for_parent(_STOP_PARENT))
                await await_until(listed)
                assert store.state_of(agent_id) == model.QUEUED
                release_claim.set()
                await await_until(claimed)
                assert store.state_of(agent_id) == model.ADMITTED
                release_stop.set()
                stopped = await await_until(stop)
                resume_after_claim.set()
            else:
                release_claim.set()
                await await_until(claimed)
                assert store.state_of(agent_id) == model.ADMITTED
                release_stop.set()
                stopped = await _finished(mgr.cancel_for_parent(_STOP_PARENT), "the stop")
                resume_after_claim.set()
            result = await await_until(dispatch)
        await settle_store_writes(store, rounds=4)
        await settle_depth_emits(mgr)

        # Stopped, and counted as the queued row it was.
        assert stopped == (0, 1)
        assert result is not None and result.done and result.user_stopped
        # Never started: no run task, and the record is the queued-stop one.
        assert agent_id not in mgr._tasks
        terminal = mgr._agents[agent_id]
        assert terminal.queued and terminal.user_stopped
        assert store.state_of(agent_id) == model.CANCELLED
        # The slot the claim reserved went back.
        assert mgr._running_count == 0 and mgr._startup_reservations == 0
        # Depth 0: in the store, in the manager's count, and on the card.
        assert store.count_pending(KIND_SUBAGENT, session_key=_STOP_PARENT) == 0
        assert await mgr.queued_count_for_async(_STOP_PARENT) == 0
        assert depths and depths[-1] == 0
    finally:
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_row_registered_during_stop_alls_read_is_reaped_not_replaced(
    quiet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop all read the row while it was queued; the pump then claimed AND
    registered it before Stop all resumed. The row is a live run now, so Stop
    all reaps it as one: its ``_agents`` record is the run's own, never a
    queued-stop record laid over it, and its store row is not cancelled out
    from under the run. The claim's own ``admitted -> starting`` write is what
    made it a run: a registered run's row is never still ``admitted``. The
    pass's writer-thread cancel job never names the run's row: the running
    sweep's reap is its only stop.
    """
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    real_run = store.run
    listed, release_stop = asyncio.Event(), asyncio.Event()
    batched: list[str] = []
    real_batch = SpawnAdmissionCoordinator.taskq_post_cancel_queued

    def _record_batch(self: Any, ids: Any) -> Any:
        batched.extend(ids)
        return real_batch(self, ids)

    monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_post_cancel_queued", _record_batch)

    async def _gated(fn, /, *args, **kwargs):
        if fn == store.list_pending and kwargs.get("session_key") == _STOP_PARENT:
            result = await real_run(fn, *args, **kwargs)
            listed.set()
            await release_stop.wait()
            return result
        return await real_run(fn, *args, **kwargs)

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            stop = asyncio.create_task(mgr.cancel_for_parent(_STOP_PARENT))
            await await_until(listed)
            started = await _finished(mgr._admission._dispatch_async_impl(params), "the dispatch")
            assert started is not None and mgr._agents[agent_id] is started
            assert agent_id in mgr._tasks
            assert store.state_of(agent_id) == model.STARTING
            release_stop.set()
            stopped = await await_until(stop)
        await settle_store_writes(store, rounds=4)

        assert stopped == (1, 0), "a registered run is stopped as the running run it is"
        assert agent_id not in batched, "the queued pass's cancel job left the run's row out"
        assert mgr._agents[agent_id] is started
        assert started.reaped and started.user_stopped and not started.queued
        assert agent_id not in mgr._tasks
        assert store.state_of(agent_id) == model.CANCELLED
    finally:
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_claim_resumed_between_stop_alls_post_and_answer_is_reaped_once(
    quiet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop all's store read lists the row while it is still unstarted; the
    pump's ``admitted -> starting`` write lands before Stop all posts its
    batched cancel, so the store takes the start; the claimer resumes after
    the job is posted and before its answer comes back, when no queued-stop
    record exists yet, and registers the run. Explicit barriers, no sleeps:
    the read's answer is held until the start write landed, the start write's
    answer until the job is posted, and the job's answer until the claimer
    has registered.

    The batch's cancel then meets a ``starting`` row it does not accept, so
    it neither cancels nor reports that row; the running sweep reaps and
    counts it, once: ``(1, 0)``. Before the start was a compare-and-set, the
    cancel landed on the still-``admitted`` row under the registered run and
    Stop all counted the one agent twice: ``(1, 1)``.
    """
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    real_run = store.run
    taskq_start = mgr._admission.taskq_claim_start
    listed, started, posted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_answer = asyncio.Event()
    batched: list[str] = []
    real_batch = SpawnAdmissionCoordinator.taskq_post_cancel_queued

    def _hold_answer(self: Any, ids: Any) -> Any:
        job = real_batch(self, ids)
        batched.extend(ids)
        posted.set()

        async def _answer_later() -> Any:
            answer = await job
            await release_answer.wait()
            return answer

        return asyncio.ensure_future(_answer_later())

    monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_post_cancel_queued", _hold_answer)

    async def _gated(fn, /, *args, **kwargs):
        result = await real_run(fn, *args, **kwargs)
        if fn == store.list_pending and kwargs.get("session_key") == _STOP_PARENT:
            listed.set()
            await started.wait()
        elif fn == taskq_start:
            started.set()
            await posted.wait()
        return result

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            stop = asyncio.create_task(mgr.cancel_for_parent(_STOP_PARENT))
            await await_until(listed)
            assert store.state_of(agent_id) == model.QUEUED
            dispatch = asyncio.create_task(mgr._admission._dispatch_async_impl(params))
            await await_until(started)
            assert store.state_of(agent_id) == model.STARTING
            await await_until(posted)
            assert agent_id in batched
            result = await await_until(dispatch)
            assert result is not None and mgr._agents[agent_id] is result
            release_answer.set()
            stopped = await await_until(stop)
        await settle_store_writes(store, rounds=4)

        assert stopped == (1, 0), "a registered run is stopped as the running run it is"
        assert result.reaped and result.user_stopped and not result.queued
        assert store.state_of(agent_id) == model.CANCELLED
        assert agent_id not in mgr._tasks
        assert mgr._running_count == 0 and mgr._startup_reservations == 0
        # The row's one cancel is the reap's own settle, from ``starting``: the
        # batch's cancel found no unstarted row to take.
        transitions = [e.data for e in store.events(agent_id) if e.kind == "transition"]
        assert transitions[-1].get("from") == model.STARTING
        assert transitions[-1].get("to") == model.CANCELLED
        assert all(
            t.get("from") != model.ADMITTED or t.get("to") == model.STARTING for t in transitions
        )
    finally:
        release_answer.set()
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_store_only_cancel_between_claim_and_start_refuses_the_start(
    quiet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel that reaches the row through the store alone (an orphan
    cancel or reconcile installs no ``_agents`` record) while the pump is
    suspended on its claim still refuses the start: the cancel bumped the
    generation, so the ``admitted -> starting`` compare-and-set is refused as
    ``stale_result``, and the reservation goes back. The loop check cannot
    catch this one, since nothing in the loop records the stop."""
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    real_run = store.run
    taskq_claim = mgr._admission.taskq_claim
    claimed, resume_after_claim = asyncio.Event(), asyncio.Event()

    async def _gated(fn, /, *args, **kwargs):
        if fn == taskq_claim:
            result = await real_run(fn, *args, **kwargs)
            claimed.set()
            await resume_after_claim.wait()
            return result
        return await real_run(fn, *args, **kwargs)

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            dispatch = asyncio.create_task(mgr._admission._dispatch_async_impl(params))
            await await_until(claimed)
            assert store.state_of(agent_id) == model.ADMITTED
            assert store.cancel(agent_id, reason="store_only_cancel") == model.ADMITTED
            assert agent_id not in mgr._agents
            resume_after_claim.set()
            await await_until(dispatch)
        await settle_store_writes(store, rounds=4)

        assert agent_id not in mgr._tasks, "a row cancelled in the store never starts"
        assert mgr._running_count == 0 and mgr._startup_reservations == 0
        assert store.state_of(agent_id) == model.CANCELLED
        refusals = [e.data for e in store.events(agent_id) if e.kind == "stale_result"]
        assert refusals and refusals[-1].get("wanted") == model.STARTING
    finally:
        resume_after_claim.set()
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_store_only_cancel_after_the_start_was_taken_cannot_end_an_unstarted_row(
    quiet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store decides the start atomically. Once the ``admitted ->
    starting`` write landed, and before the pump has resumed to register, a
    store-only cancel of an unstarted row (the predicate every cancel of a
    waiting row carries, ``taskq_cancel_queued``'s ``only_from``) is refused:
    the row is ``starting`` at the claim's generation, the run registers with
    that generation, and its own later writes are not fenced out. Before the
    compare-and-set, the same cancel committed on the still-``admitted`` row
    and the run registered anyway, every write of it ``stale_result``."""
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    real_run = store.run
    taskq_start = mgr._admission.taskq_claim_start
    started, resume_after_start = asyncio.Event(), asyncio.Event()

    async def _gated(fn, /, *args, **kwargs):
        if fn == taskq_start:
            result = await real_run(fn, *args, **kwargs)
            started.set()
            await resume_after_start.wait()
            return result
        return await real_run(fn, *args, **kwargs)

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            dispatch = asyncio.create_task(mgr._admission._dispatch_async_impl(params))
            await await_until(started)
            before = store.get(agent_id)
            assert before is not None and before.state == model.STARTING
            assert agent_id not in mgr._agents
            unstarted = model.CLAIMABLE | frozenset({model.ADMITTED})
            assert store.cancel(agent_id, reason="store_only_cancel", only_from=unstarted) is None
            assert store.state_of(agent_id) == model.STARTING
            resume_after_start.set()
            result = await await_until(dispatch)
        await settle_store_writes(store, rounds=4)

        assert result is not None and mgr._agents[agent_id] is result and agent_id in mgr._tasks
        after = store.get(agent_id)
        assert after is not None and after.state == model.STARTING
        assert after.generation == before.generation == result._taskq_generation
        # The run's own next write is accepted under that generation.
        assert await store.run(
            store.transition, agent_id, model.RUNNING, generation=after.generation
        )
        assert not any(e.kind == "stale_result" for e in store.events(agent_id))
    finally:
        resume_after_start.set()
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_single_stop_between_the_taken_start_and_registration_reaps_the_run(
    quiet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single ``cancel`` that runs after the ``admitted -> starting`` write
    landed and before the pump resumed finds a ``starting`` row with no record
    and nothing in the window. It joins the start's answer
    (``_starting_claims``) and then stops the run the pump registered, as the
    running run it is: answered True, reaped, the row cancelled, the slot
    back. Without the join it answered False -- nothing running -- and the
    run then started under a Stop the user had already been told found nothing.
    """
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    real_run = store.run
    taskq_start = mgr._admission.taskq_claim_start
    started, resume_after_start = asyncio.Event(), asyncio.Event()

    async def _gated(fn, /, *args, **kwargs):
        if fn == taskq_start:
            result = await real_run(fn, *args, **kwargs)
            started.set()
            await resume_after_start.wait()
            return result
        return await real_run(fn, *args, **kwargs)

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            dispatch = asyncio.create_task(mgr._admission._dispatch_async_impl(params))
            await await_until(started)
            assert store.state_of(agent_id) == model.STARTING
            assert agent_id not in mgr._agents
            assert mgr._admission.starting_claim_of(agent_id) is not None
            stop = asyncio.create_task(mgr.cancel(agent_id))
            await asyncio.sleep(0)
            assert not stop.done(), "the stop waits for the start's answer"
            resume_after_start.set()
            result = await await_until(dispatch)
            assert result is not None and mgr._agents[agent_id] is result
            stopped = await await_until(stop)
        await settle_store_writes(store, rounds=4)

        assert stopped is True
        assert result.reaped and result.user_stopped and not result.queued
        assert agent_id not in mgr._tasks
        assert store.state_of(agent_id) == model.CANCELLED
        assert mgr._admission.starting_claim_of(agent_id) is None
        assert mgr._running_count == 0 and mgr._startup_reservations == 0
    finally:
        resume_after_start.set()
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_stop_all_reaching_a_start_in_flight_after_its_pending_read_stops_the_run(
    quiet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop all whose pending read runs after the pump's ``admitted ->
    starting`` write landed, and whose running sweep runs before the pump
    registered the run, finds the row in neither pass: the read leaves a
    ``starting`` row out and ``_agents`` has no record yet. The running sweep
    takes the parent's starts in flight (``starting_claim_ids_for``) and stops
    each through ``cancel``, which joins the start and reaps the run it
    registered: counted once as running, the row cancelled, the slot back.
    """
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    real_run = store.run
    taskq_start = mgr._admission.taskq_claim_start
    started, resume_after_start = asyncio.Event(), asyncio.Event()
    listed = asyncio.Event()

    async def _gated(fn, /, *args, **kwargs):
        if fn == taskq_start:
            result = await real_run(fn, *args, **kwargs)
            started.set()
            await resume_after_start.wait()
            return result
        if fn == store.list_pending and kwargs.get("session_key") == _STOP_PARENT:
            result = await real_run(fn, *args, **kwargs)
            listed.set()
            return result
        return await real_run(fn, *args, **kwargs)

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            dispatch = asyncio.create_task(mgr._admission._dispatch_async_impl(params))
            await await_until(started)
            assert store.state_of(agent_id) == model.STARTING
            assert agent_id not in mgr._agents
            assert mgr._admission.starting_claim_ids_for(_STOP_PARENT) == [agent_id]
            stop = asyncio.create_task(_finished(mgr.cancel_for_parent(_STOP_PARENT), "the stop"))
            await await_until(listed)
            # The pending read has answered and the sweep has taken its ids;
            # the claimer is still held, so the run is not registered yet.
            assert agent_id not in mgr._agents
            resume_after_start.set()
            result = await await_until(dispatch)
            assert result is not None and mgr._agents[agent_id] is result
            stopped = await await_until(stop)
        await settle_store_writes(store, rounds=4)

        assert stopped == (1, 0)
        assert result.reaped and result.user_stopped and not result.queued
        assert agent_id not in mgr._tasks
        assert store.state_of(agent_id) == model.CANCELLED
        assert mgr._admission.starting_claim_of(agent_id) is None
        assert mgr._admission.starting_claim_ids_for(_STOP_PARENT) == []
        assert mgr._running_count == 0 and mgr._startup_reservations == 0
    finally:
        resume_after_start.set()
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("cascade", ["parent_cancelled", "sibling_failed"])
async def test_a_parent_cancel_cascade_reaps_a_child_whose_start_is_in_flight(
    quiet, monkeypatch: pytest.MonkeyPatch, cascade: str
) -> None:
    """The two cascades that end a row this process may be starting -- a
    cancelled parent's (``taskq_cancel_children_of_async``) and a failed
    sibling's under ``fail_parent`` (``_cancel_live_or_row``) -- hand the rows
    this process runs to ``cancel`` and cancel every other row unconditionally
    (``cancel_tree``). A row whose ``admitted -> starting`` write landed and
    whose run is not yet registered is this process's too: it is counted live,
    its row is left to the start, and the scheduled ``cancel`` joins the
    start's answer and reaps the run. Cancelled as a row instead, the claimer
    would register a run over a cancelled row that nothing reaps."""
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    parent_id = "parent-of-the-claimed-row"

    def _link_to_parent() -> None:
        # The row's parent link, as a nested spawn's accept would have written
        # it; the cascade reads only this column.
        with store._lock:
            conn = store._c()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE tasks SET parent_id=? WHERE id=?", (parent_id, agent_id))
            conn.execute("COMMIT")

    await store.run(_link_to_parent)
    real_run = store.run
    taskq_start = mgr._admission.taskq_claim_start
    started, resume_after_start = asyncio.Event(), asyncio.Event()

    async def _gated(fn, /, *args, **kwargs):
        if fn == taskq_start:
            result = await real_run(fn, *args, **kwargs)
            started.set()
            await resume_after_start.wait()
            return result
        return await real_run(fn, *args, **kwargs)

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            dispatch = asyncio.create_task(mgr._admission._dispatch_async_impl(params))
            await await_until(started)
            assert store.state_of(agent_id) == model.STARTING
            assert agent_id not in mgr._agents
            if cascade == "parent_cancelled":
                cascaded = await mgr._admission.taskq_cancel_children_of_async(
                    parent_id, reason="parent_cancelled"
                )
                assert cascaded == [agent_id]
            else:
                mgr._admission._cancel_live_or_row(agent_id, reason="child_failed")
            await settle_store_writes(store, rounds=2)
            assert store.state_of(agent_id) == model.STARTING, "the row is left to its start"
            resume_after_start.set()
            result = await await_until(dispatch)
            assert result is not None and mgr._agents[agent_id] is result
            # The scheduled cancel joined the start and reaps the registered run.
            await async_wait_until(lambda: result.reaped, describe=lambda: result.outcome)
        await settle_store_writes(store, rounds=4)

        assert result.reaped and result.user_stopped and not result.queued
        assert agent_id not in mgr._tasks
        assert store.state_of(agent_id) == model.CANCELLED
        transitions = [e.data for e in store.events(agent_id) if e.kind == "transition"]
        assert transitions[-1].get("from") == model.STARTING, "the reap's own settle ended the row"
        assert mgr._running_count == 0 and mgr._startup_reservations == 0
    finally:
        resume_after_start.set()
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_claimer_cancelled_under_its_start_write_releases_the_row(
    quiet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claimer's task is cancelled (a shutdown, a caller whose client went
    away) while its ``admitted -> starting`` write is on the writer thread. The
    claimer awaits that shielded write to its answer: the start was taken, so
    no run will ever register it, and the claimer releases the row before the
    cancel propagates: ``starting -> retry_wait`` under the claim's generation,
    eligible at once and unleased, so the next pump pass dispatches it again.
    The reservation goes back and no start is left pending. Left ``starting``,
    the row would read as a live run and a boot reconcile would settle it by
    side-effect class though it never ran."""
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    real_run = store.run
    taskq_start = mgr._admission.taskq_claim_start
    started, release_start = asyncio.Event(), asyncio.Event()

    async def _gated(fn, /, *args, **kwargs):
        result = await real_run(fn, *args, **kwargs)
        if fn == taskq_start and not started.is_set():
            started.set()
            await release_start.wait()  # held until the test cancels the claimer
        return result

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            dispatch = asyncio.create_task(mgr._admission._dispatch_async_impl(params))
            await await_until(started)
            assert store.state_of(agent_id) == model.STARTING
            dispatch.cancel()
            release_start.set()
            with pytest.raises(asyncio.CancelledError):
                await await_until(dispatch, what="the cancelled claimer")
            await settle_store_writes(store, rounds=4)

            released = store.get(agent_id)
            assert released is not None and released.state == model.RETRY_WAIT
            assert released.next_run_at is None and released.lease_owner is None
            assert agent_id not in mgr._agents and agent_id not in mgr._tasks
            assert mgr._admission.starting_claim_of(agent_id) is None
            assert mgr._running_count == 0 and mgr._startup_reservations == 0
            transitions = [e.data for e in store.events(agent_id) if e.kind == "transition"]
            assert (transitions[-1].get("from"), transitions[-1].get("to")) == (
                model.STARTING,
                model.RETRY_WAIT,
            )

            # Dispatchable again: the next pump pass claims, starts and
            # registers it under a new generation.
            await mgr._drain_queue_pass()
            await settle_store_writes(store, rounds=4)
            again = store.get(agent_id)
            assert again is not None and again.state == model.STARTING
            assert again.generation == released.generation + 1
            assert agent_id in mgr._agents and agent_id in mgr._tasks
    finally:
        release_start.set()
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_claimer_cancelled_under_its_claim_await_requeues_the_row(
    quiet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claimer's task is cancelled while its claim is on the writer thread
    and after the claim job took the row. The generation the claim minted never
    reached the claimer, so nothing would carry the ``admitted`` row on until a
    boot reconcile. The claimer awaits the shielded claim to its answer and
    gives the row back before the cancel propagates: ``admitted -> queued``
    under the claim's generation, eligible at once and unleased, with the
    reservation returned and no start write ever posted. The next pump pass
    claims, starts and registers it under a new generation."""
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    real_run = store.run
    taskq_claim = mgr._admission.taskq_claim
    taskq_start = mgr._admission.taskq_claim_start
    claimed, release_claim = asyncio.Event(), asyncio.Event()
    start_writes: list[str] = []

    async def _gated(fn, /, *args, **kwargs):
        if fn == taskq_start:
            start_writes.append(args[0])
        result = await real_run(fn, *args, **kwargs)
        if fn == taskq_claim and not claimed.is_set():
            claimed.set()
            await release_claim.wait()  # held until the test cancels the claimer
        return result

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
            # The release asks the pump for a pass. Record that request instead
            # of running it, so no pass reclaims the row while the released
            # state is asserted; the test runs that pass itself below.
            patch.object(mgr, "_drain_queue") as pump_request,
        ):
            dispatch = asyncio.create_task(mgr._admission._dispatch_async_impl(params))
            await await_until(claimed)
            taken = store.get(agent_id)
            assert taken is not None and taken.state == model.ADMITTED
            dispatch.cancel()
            release_claim.set()
            with pytest.raises(asyncio.CancelledError):
                await await_until(dispatch, what="the cancelled claimer")
            await settle_store_writes(store, rounds=4)

            released = store.get(agent_id)
            assert released is not None and released.state == model.QUEUED
            assert released.generation == taken.generation
            assert released.next_run_at is None and released.lease_owner is None
            assert start_writes == [], "a cancelled claimer posts no start write"
            assert agent_id not in mgr._agents and agent_id not in mgr._tasks
            assert mgr._admission.starting_claim_of(agent_id) is None
            assert mgr._running_count == 0 and mgr._startup_reservations == 0
            transitions = [e.data for e in store.events(agent_id) if e.kind == "transition"]
            assert (transitions[-1].get("from"), transitions[-1].get("to")) == (
                model.ADMITTED,
                model.QUEUED,
            )
            assert pump_request.call_count == 1, "the release requests one pump pass"

            await mgr._drain_queue_pass()
            await settle_store_writes(store, rounds=4)
            again = store.get(agent_id)
            assert again is not None and again.state == model.STARTING
            assert again.generation == released.generation + 1
            assert agent_id in mgr._agents and agent_id in mgr._tasks
    finally:
        release_claim.set()
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_claimer_cancelled_under_a_failing_claim_still_propagates_its_cancel(
    quiet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claimer is cancelled while its claim is on the writer thread, and the
    claim then fails with the store unavailable. Nothing was taken, so nothing
    is given back, and the claimer raises its own ``CancelledError``, not the
    store's outage: a cancel is never swallowed into a logged pump failure."""
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    real_run = store.run
    taskq_claim = mgr._admission.taskq_claim
    at_claim, release_claim = asyncio.Event(), asyncio.Event()
    before = store.get(agent_id)
    assert before is not None

    async def _gated(fn, /, *args, **kwargs):
        if fn == taskq_claim:
            at_claim.set()
            await release_claim.wait()  # held until the test cancels the claimer
            raise TaskStoreUnavailable("store down under the claim")
        return await real_run(fn, *args, **kwargs)

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            dispatch = asyncio.create_task(mgr._admission._dispatch_async_impl(params))
            await await_until(at_claim)
            dispatch.cancel()
            release_claim.set()
            with pytest.raises(asyncio.CancelledError):
                await await_until(dispatch, what="the cancelled claimer")
            await settle_store_writes(store, rounds=4)

            row = store.get(agent_id)
            assert row is not None and row.state == before.state
            assert row.generation == before.generation
            assert agent_id not in mgr._agents and agent_id not in mgr._tasks
            assert mgr._admission.starting_claim_of(agent_id) is None
    finally:
        release_claim.set()
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("shutting_down", [False, True])
async def test_a_cancelled_claimer_retains_a_start_outage_unless_shutting_down(
    quiet, monkeypatch: pytest.MonkeyPatch, shutting_down: bool
) -> None:
    """Cancellation propagates; only a live manager retains the unavailable start."""
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    real_run = store.run
    taskq_start = mgr._admission.taskq_claim_start
    started, release_write = asyncio.Event(), asyncio.Event()

    async def _gated(fn, /, *args, **kwargs):
        if fn == taskq_start and not started.is_set():
            started.set()
            await await_until(release_write, what="the unavailable start answer")
            raise TaskStoreUnavailable("store down under the start write")
        return await real_run(fn, *args, **kwargs)

    dispatch = None
    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            dispatch = asyncio.create_task(mgr._admission._dispatch_async_impl(params))
            await await_until(started, what="the pending start write")
            claimed = await real_run(store.get, agent_id)
            assert claimed is not None and claimed.state == model.ADMITTED
            mgr._shutting_down = shutting_down
            dispatch.cancel()
            release_write.set()
            with pytest.raises(asyncio.CancelledError):
                await await_until(dispatch, what="the cancelled claimer")
            row = await real_run(store.get, agent_id)
            assert row is not None and row.state == model.ADMITTED
            assert row.generation == claimed.generation
            assert row.lease_owner == store.incarnation
            assert agent_id not in mgr._agents and agent_id not in mgr._tasks
            assert mgr._admission.starting_claim_of(agent_id) is None
            assert (agent_id in mgr._retained_claims) is not shutting_down
            assert mgr._running_count == int(not shutting_down)
            assert mgr._startup_reservations == int(not shutting_down)
            if shutting_down:
                assert mgr._retained_claim_retry_handle is None
            else:
                assert mgr._retained_claims[agent_id][1] == claimed.generation
                await _finished(mgr._drain_queue_pass(), "the cancelled claim retry")
                row = await real_run(store.get, agent_id)
                assert row is not None and row.state == model.STARTING
                assert row.generation == claimed.generation
                assert agent_id in mgr._agents and agent_id in mgr._tasks
                assert agent_id not in mgr._retained_claims
                assert mgr._retained_claim_retry_handle is None
                assert mgr._running_count == 1 and mgr._startup_reservations == 0
                assert mgr._admission.starting_claim_of(agent_id) is None
    finally:
        release_write.set()
        if dispatch is not None and not dispatch.done():
            dispatch.cancel()
            await _finished(
                asyncio.gather(dispatch, return_exceptions=True), "the dispatch cleanup"
            )
        mgr._shutting_down = False
        await _finished(mgr.cancel_all(), "the manager cleanup")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_reentry_refused_by_closed_admission_releases_the_start_to_retry_wait(
    quiet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gateway admission closes after the store took the claim's start and
    before the re-entry registers it: the re-entry returns its refusal without
    registering or writing the row. The accepted work is not ended: the claimer
    releases the taken start by one generation-fenced ``starting ->
    retry_wait`` write, unleased and eligible at once, with no ``failed``
    transition, and the slot goes back. Once admission reopens, the next pump
    pass claims and starts it under the next generation."""
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    real_run = store.run
    taskq_start = mgr._admission.taskq_claim_start
    taken: dict[str, int] = {}

    async def _gated(fn, /, *args, **kwargs):
        result = await real_run(fn, *args, **kwargs)
        if fn == taskq_start and not taken:
            # Admission closes for the re-entry that follows this write.
            taken["generation"] = int(args[1])
            mgr._sessions.admission_closed = True
        return result

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            result = await _finished(
                mgr._admission._dispatch_async_impl(params), "the refused dispatch"
            )
            await settle_store_writes(store, rounds=4)

            assert taken, "the start write ran"
            assert result is None or (result.done and "admission is closed" in result.error)
            assert agent_id not in mgr._agents and agent_id not in mgr._tasks
            released = store.get(agent_id)
            assert released is not None and released.state == model.RETRY_WAIT
            assert released.generation == taken["generation"]
            assert released.lease_owner is None and released.next_run_at is None
            transitions = [e.data for e in store.events(agent_id) if e.kind == "transition"]
            assert not any(t.get("to") == model.FAILED for t in transitions)
            assert (transitions[-1].get("from"), transitions[-1].get("to")) == (
                model.STARTING,
                model.RETRY_WAIT,
            )
            assert mgr._running_count == 0 and mgr._startup_reservations == 0
            assert mgr._admission.starting_claim_of(agent_id) is None

            mgr._sessions.admission_closed = False
            await mgr._drain_queue_pass()
            await settle_store_writes(store, rounds=4)
            again = store.get(agent_id)
            assert again is not None and again.state == model.STARTING
            assert again.generation == taken["generation"] + 1
            assert agent_id in mgr._agents and agent_id in mgr._tasks
    finally:
        mgr._sessions.admission_closed = False
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("release_outage", [False, True])
@pytest.mark.parametrize("shutting_down", [False, True])
async def test_a_release_queued_behind_another_writer_job_survives_the_claimers_cancel(
    quiet, monkeypatch: pytest.MonkeyPatch, caplog, release_outage: bool, shutting_down: bool
) -> None:
    """A queued release survives cancellation and settles its reservation once."""
    from threading import Event as ThreadEvent

    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    real_run = store.run
    taskq_start = mgr._admission.taskq_claim_start
    release_start = mgr._admission._release_taken_start
    loop = asyncio.get_running_loop()
    writer_started, release_posted = asyncio.Event(), asyncio.Event()
    release_writer = ThreadEvent()
    taken: dict[str, int] = {}
    writer_job = None
    dispatch = None

    def _hold_writer() -> None:
        loop.call_soon_threadsafe(writer_started.set)
        assert release_writer.wait(60), "the test releases the held writer"

    def _unavailable_release() -> None:
        raise TaskStoreUnavailable("store down under the queued release write")

    async def _gated(fn, /, *args, **kwargs):
        nonlocal writer_job
        if fn == release_start:
            # The callback runs once real_run posts behind the held writer.
            loop.call_soon(release_posted.set)
            if release_outage:
                return await real_run(_unavailable_release)
        result = await real_run(fn, *args, **kwargs)
        if fn == taskq_start and not taken:
            taken["generation"] = int(args[1])
            mgr._sessions.admission_closed = True
            writer_job = store.post(_hold_writer)
            await await_until(writer_started, what="the occupied writer")
        return result

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            dispatch = asyncio.create_task(mgr._admission._dispatch_async_impl(params))
            await await_until(release_posted, what="the release queued behind the writer")
            mgr._shutting_down = shutting_down
            dispatch.cancel()
            release_writer.set()
            with pytest.raises(asyncio.CancelledError):
                await await_until(dispatch, what="the cancelled claimer's queued release")
            row = await real_run(store.get, agent_id)
            assert row is not None and row.generation == taken["generation"]
            if release_outage:
                assert row.state == model.STARTING
                assert "release of the untaken start" in caplog.text
            else:
                assert row.state == model.RETRY_WAIT
                assert row.lease_owner is None and row.next_run_at is None
            retained = release_outage and not shutting_down
            assert (agent_id in mgr._retained_claims) is retained
            if retained:
                assert mgr._retained_claims[agent_id][1] == taken["generation"]
            assert mgr._running_count == int(retained)
            assert mgr._startup_reservations == int(retained)
            assert agent_id not in mgr._agents and agent_id not in mgr._tasks
            assert mgr._admission.starting_claim_of(agent_id) is None
    finally:
        release_writer.set()
        if writer_job is not None:
            await await_until(writer_job, what="the writer cleanup")
        if dispatch is not None and not dispatch.done():
            dispatch.cancel()
            await _finished(
                asyncio.gather(dispatch, return_exceptions=True), "the dispatch cleanup"
            )
        mgr._sessions.admission_closed = False
        mgr._shutting_down = False
        await _finished(mgr.cancel_all(), "the manager cleanup")


@pytest.mark.asyncio
@pytest.mark.parametrize("release_outages", [1, 2])
async def test_a_release_outage_retains_the_claim_and_the_retry_registers_it(
    quiet, monkeypatch: pytest.MonkeyPatch, release_outages: int
) -> None:
    """An unavailable release keeps one reservation until the same claim registers."""
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    mgr._dispatching_ids.add(agent_id)
    mgr._dispatch_window_ids.add(agent_id)
    real_run = store.run
    taskq_start = mgr._admission.taskq_claim_start
    release_start = mgr._admission._release_taken_start
    taken: dict[str, int] = {}
    release_attempts = 0

    async def _gated(fn, /, *args, **kwargs):
        nonlocal release_attempts
        if fn == release_start:
            release_attempts += 1
            raise TaskStoreUnavailable("store down under the release write")
        result = await real_run(fn, *args, **kwargs)
        if fn == taskq_start and not taken:
            taken["generation"] = int(args[1])
            mgr._sessions.admission_closed = True
        return result

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            await _finished(mgr._admission._dispatch_async_impl(params), "the refused dispatch")
            for attempt in range(1, release_outages + 1):
                row = await real_run(store.get, agent_id)
                assert row is not None and row.state == model.STARTING
                assert row.generation == taken["generation"]
                assert row.lease_owner == store.incarnation
                assert mgr._retained_claims[agent_id][1] == taken["generation"]
                assert mgr._running_count == 1 and mgr._startup_reservations == 1
                assert agent_id not in mgr._agents and agent_id not in mgr._tasks
                assert mgr._admission.starting_claim_of(agent_id) is None
                assert agent_id not in mgr._dispatching_ids
                assert agent_id in mgr._dispatch_window_ids
                assert mgr.is_queued(agent_id)
                assert release_attempts == attempt
                if attempt < release_outages:
                    await _finished(mgr._drain_queue_pass(), "the retained release retry")

            assert await real_run(taskq_start, agent_id, taken["generation"])
            assert not await real_run(taskq_start, agent_id, taken["generation"] + 1)
            with patch.object(store, "_incarnation", "another-dispatcher"):
                assert not await real_run(taskq_start, agent_id, taken["generation"])

            mgr._sessions.admission_closed = False
            await _finished(mgr._drain_queue_pass(), "the registering retry")
            await settle_store_writes(store, rounds=4)
            row = await real_run(store.get, agent_id)
            assert row is not None and row.state == model.STARTING
            assert row.generation == taken["generation"]
            assert agent_id in mgr._agents and agent_id in mgr._tasks
            assert agent_id not in mgr._retained_claims
            assert agent_id not in mgr._dispatch_window_ids
            assert mgr._retained_claim_retry_handle is None
            assert mgr._running_count == 1 and mgr._startup_reservations == 0
            assert mgr._admission.starting_claim_of(agent_id) is None
            assert release_attempts == release_outages
    finally:
        mgr._sessions.admission_closed = False
        await _finished(mgr.cancel_all(), "the manager cleanup")


@pytest.mark.asyncio
@pytest.mark.parametrize("shutting_down", [False, True])
async def test_a_cancelled_claimer_retains_a_release_outage_unless_shutting_down(
    quiet, monkeypatch: pytest.MonkeyPatch, shutting_down: bool
) -> None:
    """Cancellation propagates; only a live manager retains the unavailable release."""
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    mgr._dispatching_ids.add(agent_id)
    mgr._dispatch_window_ids.add(agent_id)
    real_run = store.run
    taskq_start = mgr._admission.taskq_claim_start
    release_start = mgr._admission._release_taken_start
    started, release_write = asyncio.Event(), asyncio.Event()
    release_attempts = 0

    async def _gated(fn, /, *args, **kwargs):
        nonlocal release_attempts
        if fn == release_start:
            release_attempts += 1
            raise TaskStoreUnavailable("store down under the release write")
        result = await real_run(fn, *args, **kwargs)
        if fn == taskq_start and not started.is_set():
            started.set()
            await await_until(release_write, what="the shielded start answer")
        return result

    dispatch = None
    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            dispatch = asyncio.create_task(mgr._admission._dispatch_async_impl(params))
            await await_until(started, what="the committed start")
            taken = await real_run(store.get, agent_id)
            assert taken is not None and taken.state == model.STARTING
            mgr._shutting_down = shutting_down
            dispatch.cancel()
            release_write.set()
            with pytest.raises(asyncio.CancelledError):
                await await_until(dispatch, what="the cancelled claimer")
            assert release_attempts == 1
            row = await real_run(store.get, agent_id)
            assert row is not None and row.state == model.STARTING
            assert row.generation == taken.generation
            assert agent_id not in mgr._agents and agent_id not in mgr._tasks
            assert mgr._admission.starting_claim_of(agent_id) is None
            assert (agent_id in mgr._retained_claims) is not shutting_down
            assert mgr._running_count == int(not shutting_down)
            assert mgr._startup_reservations == int(not shutting_down)
            if shutting_down:
                assert mgr._retained_claim_retry_handle is None
            else:
                assert agent_id not in mgr._dispatching_ids
                assert agent_id in mgr._dispatch_window_ids
                assert mgr.is_queued(agent_id)
                await _finished(mgr._drain_queue_pass(), "the cancelled claim retry")
                row = await real_run(store.get, agent_id)
                assert row is not None and row.state == model.STARTING
                assert row.generation == taken.generation
                assert agent_id in mgr._agents and agent_id in mgr._tasks
                assert agent_id not in mgr._retained_claims
                assert mgr._running_count == 1 and mgr._startup_reservations == 0
                assert mgr._admission.starting_claim_of(agent_id) is None
    finally:
        release_write.set()
        if dispatch is not None and not dispatch.done():
            dispatch.cancel()
            await _finished(
                asyncio.gather(dispatch, return_exceptions=True), "the dispatch cleanup"
            )
        mgr._shutting_down = False
        await _finished(mgr.cancel_all(), "the manager cleanup")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_retained_claim_stop_all_cancelled_is_refused_on_retry(
    quiet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim retained across a store outage (``_retained_claims``) holds an
    ``admitted`` row no run is registered for. Stop all cancels that row and
    reports it stopped. The retry (``retry_retained_claims`` ->
    ``claim_and_start(retained_generation=...)``) then finds the stop's record
    and refuses the start without asking the store: no run, the row stays
    cancelled, the retained slot goes back, and the parent's depth is 0."""
    from kiro_crew.taskq import KIND_SUBAGENT

    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    depths = _record_depths(mgr)
    real_run = store.run
    taskq_start = mgr._admission.taskq_claim_start
    start_attempts: list[int] = []

    async def _outage_on_first_start(fn, /, *args, **kwargs):
        if fn == taskq_start:
            start_attempts.append(args[1])
            if len(start_attempts) == 1:
                raise TaskStoreUnavailable("store down under the start write")
        return await real_run(fn, *args, **kwargs)

    try:
        with (
            patch.object(store, "run", side_effect=_outage_on_first_start),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            first = await _finished(mgr._admission._dispatch_async_impl(params), "the dispatch")
            assert first is not None and first.queued and not first.done
            retained = await store.run(store.get, agent_id)
            assert retained is not None and retained.state == model.ADMITTED
            assert mgr._retained_claims[agent_id][1] == retained.generation
            assert agent_id not in mgr._agents and mgr._running_count == 1

            stopped = await _finished(mgr.cancel_for_parent(_STOP_PARENT), "the stop")
            assert stopped == (0, 1), "the retained row is stopped as the queued row it is"
            assert await store.run(store.state_of, agent_id) == model.CANCELLED
            terminal = mgr._agents[agent_id]
            assert terminal.queued and terminal.user_stopped

            await mgr._drain_queue_pass()
        await settle_store_writes(store, rounds=4)
        await settle_depth_emits(mgr)

        assert start_attempts == [retained.generation], "the retry never asked the store to start"
        assert agent_id not in mgr._retained_claims
        assert mgr._retained_claim_retry_handle is None
        assert agent_id not in mgr._tasks
        assert mgr._agents[agent_id] is terminal
        assert store.state_of(agent_id) == model.CANCELLED
        assert mgr._running_count == 0 and mgr._startup_reservations == 0
        assert store.count_pending(KIND_SUBAGENT, session_key=_STOP_PARENT) == 0
        assert depths and depths[-1] == 0
    finally:
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["single_cancel", "stop_all"])
async def test_a_retained_starting_claim_is_stopped_and_refused_on_retry(
    quiet, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    """A claim retained after its release could not reach the store holds a
    ``starting`` row with no run and no claimer in flight. A single Stop or
    Stop all cancels that row and reports it stopped, and the retry then meets
    the stop's record and is refused: no run, the row stays cancelled, and the
    retained slot goes back."""
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    mgr._dispatching_ids.add(agent_id)
    mgr._dispatch_window_ids.add(agent_id)
    real_run = store.run
    taskq_start = mgr._admission.taskq_claim_start
    release_start = mgr._admission._release_taken_start
    taken: dict[str, int] = {}
    start_attempts: list[int] = []

    async def _gated(fn, /, *args, **kwargs):
        if fn == release_start:
            raise TaskStoreUnavailable("store down under the release write")
        if fn == taskq_start:
            start_attempts.append(int(args[1]))
        result = await real_run(fn, *args, **kwargs)
        if fn == taskq_start and not taken:
            taken["generation"] = int(args[1])
            mgr._sessions.admission_closed = True
        return result

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            await _finished(mgr._admission._dispatch_async_impl(params), "the refused dispatch")
            row = await real_run(store.get, agent_id)
            assert row is not None and row.state == model.STARTING
            assert mgr._retained_claims[agent_id][1] == taken["generation"]
            assert agent_id not in mgr._agents
            assert mgr._admission.starting_claim_of(agent_id) is None

            if stop == "single_cancel":
                stopped = await _finished(mgr.cancel(agent_id), "the single stop")
                assert stopped is True
            else:
                stopped = await _finished(mgr.cancel_for_parent(_STOP_PARENT), "the stop all")
                assert stopped == (0, 1), "the retained row is stopped as the queued row it is"
            assert await real_run(store.state_of, agent_id) == model.CANCELLED
            terminal = mgr._agents[agent_id]
            assert terminal.queued and terminal.user_stopped

            mgr._sessions.admission_closed = False
            await _finished(mgr._drain_queue_pass(), "the refused retry")
            await settle_store_writes(store, rounds=4)

        assert start_attempts == [taken["generation"]], "the retry never asked the store to start"
        assert agent_id not in mgr._retained_claims
        assert agent_id not in mgr._tasks
        assert mgr._agents[agent_id] is terminal
        assert store.state_of(agent_id) == model.CANCELLED
        assert mgr._running_count == 0 and mgr._startup_reservations == 0
    finally:
        mgr._sessions.admission_closed = False
        await _finished(mgr.cancel_all(), "the manager cleanup")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_claim_of_a_row_the_store_never_saw_still_starts(
    quiet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``taskq_claim`` lets a row the store never saw (a legacy in-memory
    entry) proceed at generation 0. That claim has no row to move, so the
    ``admitted -> starting`` start write is skipped and the run starts, as the
    spec states; asking the store would find nothing and end the spawn as
    stopped."""
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    legacy_id = "legacy-entry-no-row"
    params["_preassigned_id"] = legacy_id
    assert store.get(legacy_id) is None
    real_run = store.run
    taskq_start = mgr._admission.taskq_claim_start
    rereads: list[tuple] = []

    async def _spy(fn, /, *args, **kwargs):
        if fn == taskq_start:
            rereads.append(args)
        return await real_run(fn, *args, **kwargs)

    try:
        with (
            patch.object(store, "run", side_effect=_spy),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            started = await _finished(mgr._admission._dispatch_async_impl(params), "the dispatch")

        assert started is not None and not started.done and not started.queued
        assert mgr._agents[legacy_id] is started and legacy_id in mgr._tasks
        assert rereads == []
    finally:
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_queued_stop_record_installed_during_the_release_does_not_keep_the_reservation(
    quiet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admission closes after the store took the start, so the re-entry
    returns without registering and the claimer awaits the release write. A
    stop reporting the row meanwhile installs a synthetic queued-stop record
    under its id. That record is no registration: the claimer still gives the
    reservation back and answers its pending start ``False``. Red when
    ``registered`` was read from ``_agents`` after the release awaited."""
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    real_run = store.run
    taskq_start = mgr._admission.taskq_claim_start
    release_start = mgr._admission._release_taken_start
    answers: list[bool] = []
    record = SubagentInfo(id=agent_id, task="waiting", parent_session_key=_STOP_PARENT)
    record.queued = True

    async def _gated(fn, /, *args, **kwargs):
        result = await real_run(fn, *args, **kwargs)
        if fn == taskq_start:
            mgr._sessions.admission_closed = True
            pending = mgr._admission.starting_claim_of(agent_id)
            assert pending is not None
            pending.add_done_callback(lambda f: answers.append(f.result()))
        elif fn == release_start:
            # The queued-stop report's synthetic record, installed while the
            # claimer awaits this release.
            mgr._agents[agent_id] = record
        return result

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
        ):
            await _finished(mgr._admission._dispatch_async_impl(params), "the refused dispatch")
            await settle_store_writes(store, rounds=4)

        assert mgr._agents.get(agent_id) is record
        assert agent_id not in mgr._tasks
        assert store.state_of(agent_id) == model.RETRY_WAIT
        assert answers == [False]
        assert mgr._running_count == 0 and mgr._startup_reservations == 0
    finally:
        mgr._agents.pop(agent_id, None)
        mgr._sessions.admission_closed = False
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_stop_of_a_start_in_flight_reads_the_store_off_the_loop(
    quiet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single ``cancel`` (Stop all's running sweep and the ``waits.py``
    cascades take the same call) that finds a row whose start is in flight
    runs its store phase on the writer thread, never on the loop, and still
    reaps the run the start registers. Red when the sync
    ``taskq_cancel_queued`` ran on the loop for that row."""
    import threading

    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    real_run = store.run
    taskq_start = mgr._admission.taskq_claim_start
    started, resume_after_start = asyncio.Event(), asyncio.Event()
    loop_thread = threading.get_ident()
    on_loop: list[str] = []
    real_cancel_queued = SpawnAdmissionCoordinator.taskq_cancel_queued

    def _recording(self, cancel_id: str, **kwargs: Any):
        if threading.get_ident() == loop_thread:
            on_loop.append(cancel_id)
        return real_cancel_queued(self, cancel_id, **kwargs)

    async def _gated(fn, /, *args, **kwargs):
        if fn == taskq_start:
            result = await real_run(fn, *args, **kwargs)
            started.set()
            await resume_after_start.wait()
            return result
        return await real_run(fn, *args, **kwargs)

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
            patch.object(SpawnAdmissionCoordinator, "taskq_cancel_queued", new=_recording),
        ):
            dispatch = asyncio.create_task(mgr._admission._dispatch_async_impl(params))
            await await_until(started)
            assert mgr._admission.starting_claim_of(agent_id) is not None
            stop = asyncio.create_task(mgr.cancel(agent_id))
            await asyncio.sleep(0)
            resume_after_start.set()
            result = await await_until(dispatch)
            stopped = await await_until(stop)
            await settle_store_writes(store, rounds=4)

        assert on_loop == []
        assert stopped is True
        assert result is not None and result.reaped and result.user_stopped
        assert store.state_of(agent_id) == model.CANCELLED
        assert mgr._running_count == 0 and mgr._startup_reservations == 0
    finally:
        resume_after_start.set()
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_reentry_that_raises_after_registering_keeps_the_live_run(
    quiet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-entry registers the run and creates its task, then raises (a
    post-registration audit or log write, for one). The run is live and holds
    its slot: the claimer re-raises, leaves the row ``starting``, keeps the
    run registered and answers its pending start ``True``. Red when the
    except arm released every taken start, which put a running row back in a
    claimable state and freed its slot."""
    mgr, params = await _popped_row(monkeypatch)
    store: TaskStore = mgr._taskq
    agent_id = params["_preassigned_id"]
    real_run = store.run
    taskq_start = mgr._admission.taskq_claim_start
    release_start = mgr._admission._release_taken_start
    released: list[str] = []
    answers: list[bool] = []

    async def _gated(fn, /, *args, **kwargs):
        if fn == release_start:
            released.append(str(args[1]))
        result = await real_run(fn, *args, **kwargs)
        if fn == taskq_start:
            pending = mgr._admission.starting_claim_of(agent_id)
            assert pending is not None
            pending.add_done_callback(lambda f: answers.append(f.result()))
        return result

    def _log_raises(_self: SubagentManager, info: SubagentInfo) -> None:
        raise RuntimeError("post-registration write failed")

    try:
        with (
            patch.object(store, "run", side_effect=_gated),
            patch.object(SubagentManager, "_run", new=_park_run),
            patch.object(SubagentManager, "_log_spawned", new=_log_raises),
        ):
            with pytest.raises(RuntimeError, match="post-registration write failed"):
                await _finished(mgr._admission._dispatch_async_impl(params), "the raising dispatch")
            await settle_store_writes(store, rounds=4)

        assert released == []
        assert answers == [True]
        assert agent_id in mgr._agents and agent_id in mgr._tasks
        assert store.state_of(agent_id) == model.STARTING
        assert mgr._running_count == 1 and mgr._startup_reservations == 0
    finally:
        await mgr.cancel_all()
