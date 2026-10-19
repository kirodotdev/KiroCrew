"""``taskq.adapters.runner``: TaskRunner steps and workflow agent calls as rows.

Fake clock, fake sleep, a real SQLite store in ``tmp_path``. No kiro-cli, no
real waits: every ``await`` here resolves on the loop's own tick.

This file pins the lanes (routing, FIFO order, the effective cap, ``settled_ok``, cancelled
waiters, the ancestor fence), ``accept``, ``admit`` and its cancel arms, the claim/start
boundary, the stop-reason recovery and its ladder, and the dependency and input waits.
``_requeue_unstarted``'s own refusals and what ``claim_only``'s ``None`` costs, the adopt
sweep, the factory, the store as the only admission path, the terminal writes that survive
an outage or a crash, and the accepted answers read from their persisted wake events are
pinned in
``test_taskq_runner_adapter_durability.py``; the shared fixtures and store helpers live in
``taskq_runner_adapter_helpers.py``.
"""

from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
from types import SimpleNamespace

import pytest
from overload_fakes import Clock, settle_store_writes
from taskq_runner_adapter_helpers import (  # noqa: F401 -- fixtures are requested by name
    _RUNNER_LOGGER,
    _admission,
    _locked_store,
    _Sleeps,
    clock_fixture,
    store_fixture,
)

from kiro_crew.recovery.ladder import RecoveryLadder
from kiro_crew.taskq import model as m
from kiro_crew.taskq.adapters import runner as r
from kiro_crew.taskq.dependency import (
    KIND_AUTH_FAILED,
    KIND_RATE_LIMITED,
    DependencyCoordinator,
    DependencySignal,
)
from kiro_crew.taskq.reconcile import reconcile_on_boot
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable
from kiro_crew.taskq.waits import WaitLedger, WaitRecord
from kiro_crew.testing.wait import async_wait_until, await_until


def _events(store: TaskStore, task_id: str) -> list[tuple[str, dict]]:
    return [(e.kind, e.data) for e in store.events(task_id)]


# ── lanes ────────────────────────────────────────────────────────────────────


def test_lane_for_system_and_session() -> None:
    assert r.lane_for("sess-1", "cron") == r.LANE_SYSTEM
    assert r.lane_for("sess-1", "hook") == r.LANE_SYSTEM
    assert r.lane_for("", "chat") == r.LANE_SYSTEM
    assert r.lane_for("sess-1", "chat") == "sess-1"
    assert r.lane_for("sess-1") == "sess-1"


@pytest.mark.asyncio
async def test_runner_lane_is_fifo_and_bounded_by_live_cap() -> None:
    lane = r.RunnerLane(1)
    order: list[str] = []

    async def worker(name: str) -> None:
        await lane.acquire(name)
        order.append(f"{name}:in")
        await asyncio.sleep(0)
        order.append(f"{name}:out")
        lane.release()

    await asyncio.gather(worker("a"), worker("b"), worker("c"))
    assert order == ["a:in", "a:out", "b:in", "b:out", "c:in", "c:out"]
    assert lane.running == 0 and lane.waiting == 0


@pytest.mark.asyncio
async def test_runner_lane_effective_cap_actuator_pauses_and_resumes() -> None:
    lane = r.RunnerLane(4)
    assert lane.effective == 4
    assert lane.set_effective_cap(0) == 0  # pause: nothing new is granted

    grants: list[str] = []

    async def waiter(name: str) -> None:
        await lane.acquire(name)
        grants.append(name)

    tasks = [asyncio.create_task(waiter("x")), asyncio.create_task(waiter("y"))]
    await asyncio.sleep(0)
    assert grants == [] and lane.waiting == 2
    assert lane.set_effective_cap(1) == 1
    await asyncio.sleep(0)
    assert grants == ["x"]  # exactly one slot, FIFO
    lane.set_effective_cap(None)
    await asyncio.sleep(0)
    assert grants == ["x", "y"]
    await asyncio.gather(*tasks)
    assert lane.effective == 4 and lane.stats()["granted"] == 2


def test_runner_lane_fixed_mode_pins_the_bound() -> None:
    ceiling = {"v": 6}
    lane = r.RunnerLane(lambda: ceiling["v"], mode=lambda: r.MODE_FIXED, pinned=3)
    assert lane.effective == 3
    lane.set_effective_cap(1)  # the controller's actuator is ignored under fixed
    assert lane.effective == 3
    ceiling["v"] = 2
    assert lane.effective == 3
    aimd = r.RunnerLane(lambda: ceiling["v"], mode=r.MODE_AIMD)
    assert aimd.effective == 2
    aimd.set_effective_cap(1)
    assert aimd.effective == 1


@pytest.mark.asyncio
async def test_lane_settled_ok_counts_a_committed_done_not_a_grant(
    store: TaskStore, clock: Clock
) -> None:
    """``settled_ok`` is the adaptive controller's completion signal: a grant
    is not one, and only a committed ``done`` -- not a fail or a cancel --
    advances it."""
    adm, _ = _admission(store, clock, cap=4)
    assert adm.lane.settled_ok == 0

    done_row = adm.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:r:agent1")
    assert done_row is not None
    done = await adm.admit(done_row.id, lane="sess")
    assert adm.lane.stats()["granted"] == 1
    assert adm.lane.settled_ok == 0  # a grant is not a completion
    done.done()
    assert adm.lane.settled_ok == 1

    failed_row = adm.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:r:agent2")
    assert failed_row is not None
    failed = await adm.admit(failed_row.id, lane="sess")
    failed.fail("boom")
    assert adm.lane.settled_ok == 1  # a failure does not count

    cancel_row = adm.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:r:agent3")
    assert cancel_row is not None
    cancelled = await adm.admit(cancel_row.id, lane="sess")
    cancelled.cancel("stop")
    assert adm.lane.settled_ok == 1  # a cancel does not count


@pytest.mark.asyncio
async def test_lane_settled_ok_ignores_a_claim_only_container_row(
    store: TaskStore, clock: Clock
) -> None:
    """A TaskRunner run row is claimed WITHOUT a lane slot (``claim_only``): it
    groups its steps but executes nothing itself, and the steps are what the
    lane meters. Its committed ``done`` must NOT advance ``settled_ok`` -- else
    every TaskRunner run would credit a completion no lane slot produced,
    double-counting against the steps that already did (Opus finding)."""
    adm, _ = _admission(store, clock, cap=4)

    # A slot-backed step completes -> counts.
    step_row = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:step1")
    assert step_row is not None
    step = await adm.admit(step_row.id, lane="sess")
    assert step.slot_held is True
    step.done()
    assert adm.lane.settled_ok == 1

    # The container run row holds no lane slot -> its done credits nothing.
    container_row = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:container")
    assert container_row is not None
    container = adm.claim_only(container_row.id)
    assert container is not None
    assert container.slot_held is False  # no slot was ever metered
    assert container.done() is True  # the row still reaches a committed done
    assert adm.lane.settled_ok == 1  # unchanged: no slot, no completion


@pytest.mark.asyncio
async def test_lane_settled_ok_counts_a_committed_done_from_done_async(
    store: TaskStore, clock: Clock
) -> None:
    """``done_async`` -- the event-loop settle path -- counts a committed
    ``done`` the same as the sync ``done``."""
    adm, _ = _admission(store, clock, cap=4)
    row = adm.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:r:async1")
    assert row is not None
    handle = await adm.admit(row.id, lane="sess")
    assert adm.lane.settled_ok == 0  # a grant is not a completion
    assert await handle.done_async() is True
    assert adm.lane.settled_ok == 1


@pytest.mark.asyncio
async def test_lane_settled_ok_does_not_count_a_deferred_done(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``done`` whose terminal write the store could not take is deferred to
    the admission's durable retry and returns ``ok=False``: the slot is already
    freed and the window long past, so crediting it late would mis-time it.
    ``settled_ok`` stays put; a later replay through ``flush_pending_finish``
    writes the row without re-entering the settle choke point, so it is never
    counted."""
    adm, _ = _admission(store, clock, cap=4)
    row = adm.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:r:deferred1")
    assert row is not None
    handle = await adm.admit(row.id, lane="sess")

    def _unavailable(*_a: object, **_k: object) -> bool:
        raise TaskStoreUnavailable("disk full")

    monkeypatch.setattr(store, "finish", _unavailable)
    assert handle.done() is False  # the write was deferred, not committed
    assert adm.lane.settled_ok == 0  # a deferred done is not counted
    assert row.id in adm._pending_finish  # it is queued for durable retry


@pytest.mark.asyncio
async def test_runner_lane_cancelled_waiter_leaves_the_queue() -> None:
    lane = r.RunnerLane(1)
    await lane.acquire("holder")
    t = asyncio.create_task(lane.acquire("w"))
    await asyncio.sleep(0)
    assert lane.waiting == 1
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert lane.waiting == 0
    # A parked waiter never held a slot: the holder's slot is still counted.
    assert lane.running == 1
    lane.release()
    assert lane.running == 0


@pytest.mark.asyncio
async def test_runner_lane_cancelled_parked_waiter_keeps_the_bound() -> None:
    """Cancelling a parked waiter leaves ``running`` alone; the next pump
    grants exactly the bound, so the lane cannot over-admit afterwards."""
    lane = r.RunnerLane(2)
    await lane.acquire("a")
    await lane.acquire("b")
    parked = [asyncio.create_task(lane.acquire(f"w{i}")) for i in range(3)]
    await asyncio.sleep(0)
    assert lane.waiting == 3 and lane.running == 2
    parked[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await parked[0]
    assert lane.running == 2 and lane.waiting == 2
    lane.release()  # one slot back: exactly one of the two remaining waiters starts
    await asyncio.sleep(0)
    assert lane.running == 2 and lane.waiting == 1
    lane.release()
    await asyncio.sleep(0)
    assert lane.running == 2 and lane.waiting == 0
    await asyncio.gather(*parked[1:])
    lane.release()
    lane.release()
    assert lane.running == 0


@pytest.mark.asyncio
async def test_runner_lane_waiter_granted_then_cancelled_hands_the_slot_on() -> None:
    lane = r.RunnerLane(1)
    await lane.acquire("holder")
    first = asyncio.create_task(lane.acquire("first"))
    second = asyncio.create_task(lane.acquire("second"))
    await asyncio.sleep(0)
    assert lane.waiting == 2
    lane.release()  # grants ``first`` without letting it run yet
    assert lane.running == 1 and lane.waiting == 1
    first.cancel()  # granted and cancelled in the same tick
    with pytest.raises(asyncio.CancelledError):
        await first
    await second  # the slot went to the next waiter
    assert lane.running == 1 and lane.waiting == 0
    lane.release()
    assert lane.running == 0


# ── lane: a descendant is never parked behind its own ancestor ───────────────
#
# ONE RunnerLane serves both runner consumers (the TaskRunner's steps and the
# workflow service's ``ctx.agent()`` calls: one shared RunnerAdmission, one
# counter), and ``acquire`` parks on a bare future with no timeout. A row whose
# own ancestor holds the slot it is waiting for therefore waits for a release
# only that ancestor can make. Each of these bounds the wait IN THE TEST, so a
# regression is a named assertion failure and never a lost xdist worker.

#: How long a pin waits before calling a park a hang. Only paid on failure: the
#: refusal and the grant both resolve on the loop's next tick, so this is
#: headroom against a loaded runner, never a duration any green run spends.
_PARK_BOUND_SECS = 5.0


async def _settled(coro) -> asyncio.Task:
    """Run *coro* as a task and wait at most :data:`_PARK_BOUND_SECS` for it."""
    task = asyncio.ensure_future(coro)
    await asyncio.wait({task}, timeout=_PARK_BOUND_SECS)
    return task


@pytest.mark.asyncio
async def test_runner_lane_refuses_a_wait_only_its_own_ancestor_could_end() -> None:
    lane = r.RunnerLane(1, name="nest")
    await lane.acquire("outer")
    assert lane.holders == frozenset({"outer"}) and lane.saturated
    nested = await _settled(lane.acquire("inner", ancestors=["outer"]))
    assert nested.done(), "a descendant parked behind its own ancestor's slot"
    with pytest.raises(r.RunnerLaneSelfBlocked, match="ancestor outer"):
        nested.result()
    # Refused, so it holds nothing and the ceiling is untouched.
    assert lane.running == 1 and lane.waiting == 0
    lane.release("outer")
    assert lane.running == 0 and lane.holders == frozenset()


@pytest.mark.asyncio
async def test_runner_lane_parks_an_ordinary_waiter_however_long_it_waits() -> None:
    """The refusal is for a self-blocking wait ONLY: a root has no ancestor, so
    a legitimately long queue wait is still served rather than turned down."""
    lane = r.RunnerLane(1, name="ord")
    await lane.acquire("long-runner")
    root = asyncio.create_task(lane.acquire("queued-root"))
    # A DESCENDANT whose ancestor holds nothing here parks the same way.
    other = asyncio.create_task(lane.acquire("child-of-elsewhere", ancestors=["absent-parent"]))
    await asyncio.sleep(0)
    assert lane.waiting == 2 and not root.done() and not other.done()
    lane.release("long-runner")
    granted = await _settled(root)
    assert granted.done() and granted.exception() is None
    lane.release("queued-root")
    served = await _settled(other)
    assert served.done() and served.exception() is None
    lane.release("child-of-elsewhere")
    assert lane.running == 0 and lane.stats()["granted"] == 3


@pytest.mark.asyncio
async def test_runner_lane_parks_a_descendant_an_unrelated_holder_is_blocking() -> None:
    """With a non-ancestor also holding, the wait CAN end without the ancestor:
    the descendant parks and takes the slot that holder frees."""
    lane = r.RunnerLane(2, name="mixed")
    await lane.acquire("outer")
    await lane.acquire("unrelated")
    nested = asyncio.create_task(lane.acquire("inner", ancestors=["outer"]))
    await asyncio.sleep(0)
    assert lane.waiting == 1 and not nested.done()
    lane.release("unrelated")
    got = await _settled(nested)
    assert got.done() and got.exception() is None
    assert lane.running == 2  # the ceiling still holds: outer + inner, not three


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [1, 2, 3, 4])
async def test_runner_lane_peak_never_exceeds_effective_with_nesting(cap: int) -> None:
    """Peak concurrent holders == the effective cap, whatever the nesting does:
    a refusal takes no slot, and a park takes one only on a grant."""
    lane = r.RunnerLane(cap, name=f"peak{cap}")
    peak = 0
    refused = 0
    hold = asyncio.Event()

    async def body(idx: int, ancestors: tuple[str, ...]) -> None:
        nonlocal peak, refused
        name = f"row{idx}"
        try:
            await lane.acquire(name, ancestors=ancestors)
        except r.RunnerLaneSelfBlocked:
            refused += 1
            return
        peak = max(peak, lane.running)
        await hold.wait()
        lane.release(name)

    # row0 is the ancestor and holds until ``hold`` is set; rows 1-3 descend
    # from it, rows 4-5 are unrelated roots.
    tasks = [asyncio.ensure_future(body(0, ()))]
    await asyncio.sleep(0)
    assert lane.holders == frozenset({"row0"})
    tasks += [asyncio.ensure_future(body(i, ("row0",))) for i in range(1, 4)]
    tasks += [asyncio.ensure_future(body(i, ())) for i in range(4, 6)]
    await asyncio.sleep(0)
    assert peak == cap  # the cap filled before anything was allowed to finish
    hold.set()
    await asyncio.wait(set(tasks), timeout=_PARK_BOUND_SECS)
    assert all(t.done() for t in tasks), "a waiter never settled"
    assert peak == cap and lane.stats()["granted"] == 6 - refused
    assert lane.running == 0 and lane.waiting == 0 and lane.holders == frozenset()
    # Only cap 1 leaves the ancestry holding EVERY slot, so only there is a
    # descendant refused. From cap 2 a descendant sibling holds one too, and
    # that is a release the FIFO queue hands on -- so those waits are parked.
    assert refused == (3 if cap == 1 else 0)


@pytest.mark.asyncio
async def test_runner_lane_refuses_a_waiter_a_cap_squeeze_left_self_blocked() -> None:
    """A descendant that parked legitimately and became self-blocking afterwards.

    The bound falls from 2 to 1 while an ancestor and an unrelated row hold, so
    the unrelated row's release frees NOTHING: the head is then waiting on its
    own ancestry with no further edge to wake it.
    """
    lane = r.RunnerLane(2, name="squeeze")
    await lane.acquire("outer")
    await lane.acquire("unrelated")
    nested = asyncio.create_task(lane.acquire("inner", ancestors=["outer"]))
    await asyncio.sleep(0)
    assert lane.waiting == 1 and not nested.done()
    assert lane.set_effective_cap(1) == 1
    lane.release("unrelated")  # running 2 -> 1, still at the bound: no grant
    settled = await _settled(nested)
    assert settled.done(), "a squeeze left a descendant parked behind its ancestor"
    with pytest.raises(r.RunnerLaneSelfBlocked, match="ancestor outer"):
        settled.result()
    assert lane.running == 1 and lane.waiting == 0


@pytest.mark.asyncio
async def test_a_squeeze_refuses_a_self_blocked_waiter_that_is_not_the_head() -> None:
    """The same squeeze with an ordinary root queued IN FRONT of the descendant.

    Judging only the head cannot break this: the root ahead is served only by the
    ancestor releasing, and the ancestor is waiting on the descendant, so a
    head-only scan leaves BOTH parked for ever behind a head that is not itself
    refusable. The refusal has to reach a waiter wherever it sits in the queue,
    and the root it was queued behind is then served by the freed slot.
    """
    lane = r.RunnerLane(2, name="squeeze-nonhead")
    await lane.acquire("outer")
    await lane.acquire("unrelated")
    root = asyncio.create_task(lane.acquire("root"))
    await asyncio.sleep(0)
    nested = asyncio.create_task(lane.acquire("inner", ancestors=["outer"]))
    await asyncio.sleep(0)
    assert lane.waiting == 2 and not root.done() and not nested.done()

    assert lane.set_effective_cap(1) == 1
    lane.release("unrelated")  # running 2 -> 1, at the bound: no grant

    settled = await _settled(nested)
    assert settled.done(), "a non-head descendant stayed parked behind its ancestor"
    with pytest.raises(r.RunnerLaneSelfBlocked, match="ancestor outer"):
        settled.result()
    # The root it sat behind is untouched, and takes the slot the ancestor frees.
    assert not root.done() and lane.waiting == 1
    lane.release("outer")
    await _settled(root)
    assert root.done() and root.exception() is None
    assert lane.running == 1 and lane.waiting == 0


@pytest.mark.asyncio
async def test_a_squeeze_that_leaves_an_ordinary_waiter_at_the_bound_still_parks() -> None:
    """The mirror of the test above: no ancestry, so the squeeze parks, never refuses."""
    lane = r.RunnerLane(2, name="squeeze-root")
    await lane.acquire("a")
    await lane.acquire("b")
    root = asyncio.create_task(lane.acquire("root"))
    await asyncio.sleep(0)
    lane.set_effective_cap(1)
    lane.release("b")
    await asyncio.sleep(0)
    assert lane.waiting == 1 and not root.done()
    lane.release("a")  # the bound is 1 and nothing holds it now
    served = await _settled(root)
    assert served.done() and served.exception() is None


@pytest.mark.asyncio
async def test_admit_refuses_and_ends_a_row_its_ancestry_is_blocking(
    store: TaskStore, clock: Clock
) -> None:
    """The lane fence through the whole admission, on the parentage the STORE holds.

    The refusal is typed and the row is ENDED, so a self-blocked descendant
    leaves no ``queued`` row behind for a kind that has no dispatcher.
    """
    adm, _ = _admission(store, clock, cap=1)
    parent = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    assert parent is not None
    outer = await adm.admit(parent.id, lane="sess")
    child = adm.accept(
        kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:wf:agent1", parent_id=parent.id
    )
    assert child is not None and child.parent_id == parent.id
    nested = await _settled(adm.admit(child.id, kind=m.KIND_WORKFLOW_AGENT, lane="sess"))
    assert nested.done(), "admit parked a descendant behind its own ancestor's slot"
    with pytest.raises(r.RunnerLaneSelfBlocked, match="ancestor taskrunner:r:task1"):
        nested.result()
    assert adm.lane.holders == frozenset({parent.id})
    await settle_store_writes(store)
    assert store.state_of(child.id) == m.CANCELLED
    assert any(
        k == "transition" and "self-blocked" in str(d.get("reason", ""))
        for k, d in _events(store, child.id)
    )
    # The ancestor kept its slot and its row: only the descendant was refused.
    assert outer.slot_held and store.state_of(parent.id) == m.STARTING
    outer.done()
    assert adm.lane.running == 0


@pytest.mark.asyncio
async def test_admit_parks_a_descendant_of_a_row_that_holds_no_slot(
    store: TaskStore, clock: Clock
) -> None:
    """The live nesting: a TaskRunner step is a child of its RUN row, and that
    container row is claimed WITHOUT a lane slot -- so a step is a descendant
    the fence must not touch. It parks behind the unrelated holder and runs."""
    adm, _ = _admission(store, clock, cap=1)
    run_row = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r")
    assert run_row is not None
    container = adm.claim_only(run_row.id, lane="sess")
    assert container is not None and not container.slot_held
    step = adm.accept(
        kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1", parent_id=run_row.id
    )
    assert step is not None
    await adm.lane.acquire("unrelated")  # the one slot, held by nobody's ancestor
    admitting = asyncio.ensure_future(adm.admit(step.id, lane="sess"))
    await settle_store_writes(store)
    await settle_store_writes(store)
    assert adm.lane.waiting == 1 and not admitting.done()
    adm.lane.release("unrelated")
    granted = await _settled(admitting)
    assert granted.done(), "a step never got the slot its container row does not hold"
    handle = granted.result()
    assert handle.state == m.STARTING and handle.slot_held
    handle.done()


# ── accept (write-before-ack) ────────────────────────────────────────────────


def test_accept_writes_the_row_before_returning(store: TaskStore, clock: Clock) -> None:
    adm, _ = _admission(store, clock)
    rec = adm.accept(
        kind=m.KIND_TASKRUNNER_STEP,
        task_id="taskrunner:run1:task1",
        session_key="sess",
        source="chat",
        params={"index": 1},
    )
    assert rec is not None
    row = store.get(rec.id)
    assert row is not None and row.state == m.QUEUED
    assert row.params[r.PARAM_LANE] == "sess"
    assert [k for k, _ in _events(store, rec.id)] == ["accepted"]


def test_accept_failure_is_a_refusal_not_an_id(store: TaskStore, clock: Clock, monkeypatch) -> None:
    adm, _ = _admission(store, clock)

    def _boom(_rec):
        raise TaskStoreUnavailable("disk full")

    monkeypatch.setattr(store, "accept_one", _boom)
    with pytest.raises(r.RunnerAdmissionRefused):
        adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    assert store.get("taskrunner:r:task1") is None


def test_accept_reuses_a_claimable_row_and_suffixes_a_terminal_one(
    store: TaskStore, clock: Clock
) -> None:
    adm, _ = _admission(store, clock)
    first = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    assert first is not None
    # Still queued: the same id is handed back, not duplicated.
    again = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    assert again is not None and again.id == first.id
    assert store.count(kind=m.KIND_TASKRUNNER_STEP) == 1
    store.claim(first.id)
    store.finish(first.id, m.FAILED, error="x")
    rerun = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    assert rerun is not None and rerun.id == "taskrunner:r:task1~2"
    assert store.get("taskrunner:r:task1").state == m.FAILED


def test_accept_without_a_store_returns_none(clock: Clock) -> None:
    adm, _ = _admission(None, clock)
    assert adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="x") is None


# ── admit ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_steps_under_cap_one_run_in_order_and_are_claimed_once(
    store: TaskStore, clock: Clock
) -> None:
    adm, _ = _admission(store, clock, cap=1)
    ids = []
    for i in (1, 2):
        rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id=f"taskrunner:r:task{i}")
        ids.append(rec.id)
    order: list[str] = []

    async def step(task_id: str) -> None:
        h = await adm.admit(task_id, lane="sess")
        order.append(f"{task_id}:start")
        assert h.running()
        await asyncio.sleep(0)
        order.append(f"{task_id}:end")
        assert h.done()

    await asyncio.gather(*(step(t) for t in ids))
    assert order == [f"{ids[0]}:start", f"{ids[0]}:end", f"{ids[1]}:start", f"{ids[1]}:end"]
    for task_id in ids:
        kinds = [k for k, _ in _events(store, task_id)]
        assert kinds.count("claimed") == 1, kinds
        rec = store.get(task_id)
        assert rec.state == m.DONE and rec.generation == 1 and rec.attempts == 1
    assert adm.lane.running == 0


@pytest.mark.asyncio
async def test_admit_defers_on_memory_pressure_instead_of_refusing(
    store: TaskStore, clock: Clock
) -> None:
    verdicts = iter(
        [
            SimpleNamespace(admitted=False, reason="memory critical"),
            SimpleNamespace(admitted=True, reason=""),
        ]
    )
    adm, sleeps = _admission(store, clock, pressure=lambda: next(verdicts), admit_wait_secs=5.0)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    h = await adm.admit(rec.id)
    assert h.generation == 1 and h.state == m.STARTING
    kinds = [k for k, _ in _events(store, rec.id)]
    assert kinds[:3] == ["accepted", "deferred", "claimed"]
    assert sleeps.calls == [5.0]
    assert adm.deferred_count == 1


@pytest.mark.asyncio
async def test_a_step_granted_after_a_lane_wait_rechecks_memory_pressure(
    store: TaskStore, clock: Clock
) -> None:
    """The lane does not pause on memory, so a step parked behind a full lane
    must not start on a grant that lands while the host is critical: the
    check it passed before parking is stale by then."""
    verdicts = iter(
        [
            SimpleNamespace(admitted=True, reason=""),  # a, lane free
            SimpleNamespace(admitted=True, reason=""),  # b, before it parks
            SimpleNamespace(admitted=False, reason="memory critical"),  # b, at its grant
            SimpleNamespace(admitted=True, reason=""),  # b, after the deferral
        ]
    )
    adm, sleeps = _admission(
        store, clock, cap=1, pressure=lambda: next(verdicts), admit_wait_secs=5.0
    )
    a = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    b = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task2")
    ha = await adm.admit(a.id)
    waiter = asyncio.create_task(adm.admit(b.id))
    await async_wait_until(lambda: adm.lane.waiting == 1)
    ha.done()  # the grant reaches b while memory is critical
    hb = await await_until(waiter)
    assert hb.state == m.STARTING
    kinds = [k for k, _ in _events(store, b.id)]
    assert kinds[:3] == ["accepted", "deferred", "claimed"]
    assert sleeps.calls == [5.0]
    assert adm.deferred_count == 1
    assert adm.lane.running == 1
    hb.done()
    assert adm.lane.running == 0


@pytest.mark.asyncio
async def test_a_step_refused_at_its_grant_gives_the_slot_back_before_deferring(
    store: TaskStore, clock: Clock
) -> None:
    """The grant-time deferral sleeps up to the admit wait; a slot held through
    that sleep blocks every other step on the lane for nothing. The slot must
    already be back when the deferral's sleep starts."""
    verdicts = iter(
        [
            SimpleNamespace(admitted=True, reason=""),  # a, lane free
            SimpleNamespace(admitted=True, reason=""),  # b, before it parks
            SimpleNamespace(admitted=False, reason="memory critical"),  # b, at its grant
            SimpleNamespace(admitted=True, reason=""),  # b, after the deferral
        ]
    )
    lane = r.RunnerLane(1, mode=r.MODE_AIMD)
    inside_sleep: list[tuple[int, bool]] = []
    sleeps = _Sleeps(clock)

    async def _sleep(secs: float) -> None:
        inside_sleep.append((lane.running, "taskrunner:r:task2" in lane.holders))
        await sleeps(secs)

    adm = r.RunnerAdmission(
        store,
        lane=lane,
        clock=clock,
        sleep=_sleep,
        pressure=lambda: next(verdicts),
        admit_wait_secs=5.0,
    )
    a = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    b = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task2")
    ha = await adm.admit(a.id)
    waiter = asyncio.create_task(adm.admit(b.id))
    await async_wait_until(lambda: lane.waiting == 1)
    ha.done()  # the grant reaches b while memory is critical
    hb = await await_until(waiter)
    assert sleeps.calls == [5.0]
    assert inside_sleep == [(0, False)]
    assert adm.deferred_count == 1
    assert [k for k, _ in _events(store, b.id)][:3] == ["accepted", "deferred", "claimed"]
    hb.done()
    assert lane.running == 0


@pytest.mark.asyncio
async def test_admit_raises_when_the_row_was_cancelled_while_waiting(
    store: TaskStore, clock: Clock
) -> None:
    adm, _ = _admission(store, clock, cap=1)
    a = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    b = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task2")
    ha = await adm.admit(a.id)
    waiter = asyncio.create_task(adm.admit(b.id))
    await asyncio.sleep(0)
    assert store.cancel(b.id, reason="user") == m.QUEUED
    ha.done()  # frees the slot; the waiter now claims -- and is refused
    with pytest.raises(r.RunnerTaskCancelled):
        await waiter
    assert adm.lane.running == 0


@pytest.mark.asyncio
async def test_admit_cancelled_while_waiting_ends_the_accepted_row(
    store: TaskStore, clock: Clock
) -> None:
    """The row was accepted, so it is already in the store; the coroutine that
    would have driven it is gone and ``taskrunner_step`` has no dispatcher, so
    ``admit`` ends the row itself instead of leaving it queued forever."""
    adm, _ = _admission(store, clock, cap=1)
    a = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    b = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task2")
    ha = await adm.admit(a.id)
    parked = asyncio.create_task(adm.admit(b.id))
    await settle_store_writes(store)
    await settle_store_writes(store)
    assert store.state_of(b.id) == m.QUEUED and adm.lane.waiting == 1
    parked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parked
    assert store.state_of(b.id) == m.CANCELLED
    assert any(
        k == "transition" and "admission cancelled" in str(d.get("reason", ""))
        for k, d in _events(store, b.id)
    )
    # The lane bookkeeping ``RunnerLane.acquire`` already gets right is untouched.
    assert adm.lane.running == 1 and adm.lane.waiting == 0
    assert ha.done() and adm.lane.running == 0


@pytest.mark.asyncio
async def test_admit_cancelled_after_the_grant_ends_the_row_without_a_double_release(
    store: TaskStore, clock: Clock
) -> None:
    """The slot was granted in the same tick as the cancel: ``acquire`` hands it
    on, so the cancel arm must end the ROW and release nothing."""
    adm, _ = _admission(store, clock, cap=1)
    a = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    b = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task2")
    ha = await adm.admit(a.id)
    parked = asyncio.create_task(adm.admit(b.id))
    await settle_store_writes(store)
    await settle_store_writes(store)
    assert adm.lane.waiting == 1
    assert ha.done()  # releases the slot: the parked future is granted, not run
    assert adm.lane.running == 1
    parked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parked
    assert store.state_of(b.id) == m.CANCELLED
    assert adm.lane.running == 0 and adm.lane.waiting == 0


@pytest.mark.asyncio
async def test_admit_cancelled_after_the_claim_ends_the_row_and_frees_the_slot(
    store: TaskStore, clock: Clock, monkeypatch
) -> None:
    """A cancel can also land on the ``starting`` write, after the claim
    committed: the caller never receives the handle, so ``admit`` owns both the
    claimed row (fenced by the generation it took) and the lane slot."""
    adm, _ = _admission(store, clock, cap=1)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")

    async def _cancelled_write(self, state, **kw):
        raise asyncio.CancelledError()

    monkeypatch.setattr(r.Admitted, "write_async", _cancelled_write)
    with pytest.raises(asyncio.CancelledError):
        await adm.admit(rec.id)
    row = store.get(rec.id)
    assert row.state == m.CANCELLED and row.lease_owner is None
    assert adm.lane.running == 0 and adm.lane.waiting == 0


@pytest.mark.asyncio
async def test_admit_cancelled_leaves_a_row_another_owner_claimed_alone(
    store: TaskStore, clock: Clock
) -> None:
    adm, _ = _admission(store, clock, cap=1)
    a = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    b = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task2")
    ha = await adm.admit(a.id)
    parked = asyncio.create_task(adm.admit(b.id))
    await settle_store_writes(store)
    await settle_store_writes(store)
    assert adm.lane.waiting == 1
    assert store.claim(b.id, owner="another-incarnation") is not None
    parked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parked
    assert store.state_of(b.id) == m.ADMITTED  # that owner's row, not ours to end
    assert ha.done()


@pytest.mark.asyncio
async def test_admit_cancelled_with_no_store_only_reraises(clock: Clock) -> None:
    adm, _ = _admission(None, clock, cap=1)
    h = await adm.admit("holder")
    parked = asyncio.create_task(adm.admit("parked"))
    await asyncio.sleep(0)
    assert adm.lane.waiting == 1
    parked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parked
    assert adm.lane.waiting == 0 and adm.lane.running == 1
    assert h.done() and adm.lane.running == 0


@pytest.mark.asyncio
async def test_admit_without_a_store_is_the_lane_alone(clock: Clock) -> None:
    adm, _ = _admission(None, clock, cap=1)
    h = await adm.admit("anything")
    assert h.generation == 0 and h.running() and adm.lane.running == 1
    assert h.done() and adm.lane.running == 0


@pytest.mark.asyncio
async def test_a_refused_starting_write_stops_the_step_and_requeues_the_row(
    tmp_path: Path, clock: Clock
) -> None:
    """``admitted`` is the store's word that no executor ever held the row, so a
    start write that did not commit refuses the admission instead of running the
    step under a row a restart cannot tell apart from one that never started."""
    store = _locked_store(tmp_path, clock)
    ran: list[str] = []
    try:
        adm, _ = _admission(store, clock, cap=1)
        rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
        assert rec is not None
        with pytest.raises(r.RunnerAdmissionRefused):
            handle = await adm.admit(rec.id)
            ran.append("step body")
            await handle.done_async()
        assert ran == [], "the step ran while the store still called the row admitted"
        row = store.get(rec.id)
        assert (
            row is not None and row.state == m.QUEUED
        ), "the row was not put back for a later dispatch"
        assert row.lease_owner is None
        assert adm.lane.running == 0 and adm.lane.waiting == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_row_left_admitted_by_an_outage_is_one_no_step_ran_under(
    tmp_path: Path, clock: Clock
) -> None:
    """The requeue needs the same store, so an outage that outlives the refusal
    leaves the row ``admitted`` -- which the next boot's reconciler requeues
    blind. That verdict is only sound because the step never started."""
    store = _locked_store(tmp_path, clock, clears_after_one_refusal=False)
    ran: list[str] = []
    try:
        adm, _ = _admission(store, clock, cap=1)
        rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
        assert rec is not None
        with pytest.raises(r.RunnerAdmissionRefused):
            handle = await adm.admit(rec.id)
            ran.append("step body")
            await handle.done_async()
        assert ran == [], "the step ran while the store still called the row admitted"
        assert store.state_of(rec.id) == m.ADMITTED
        assert adm.lane.running == 0 and adm.lane.waiting == 0
    finally:
        store.close()
    clock.advance(200.0)
    reopened = TaskStore(tmp_path / "tasks.db", clock=clock, network_fs=False).open()
    try:
        # The default adapter set, as the gateway boots: the ``admitted``
        # verdict is reached before the kind is even consulted.
        report = reconcile_on_boot(reopened)
        assert report.requeued == 1
        assert reopened.state_of("taskrunner:r:task1") == m.QUEUED
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_a_cancel_inside_the_claim_window_is_not_started(
    tmp_path: Path, clock: Clock
) -> None:
    """The other way the start write does not commit: the row was cancelled
    between the claim and the write, so the generation fences it."""

    class _CancelAfterClaim(TaskStore):
        def claim(self, task_id, *, owner=None, lease_secs=None):
            claimed = super().claim(task_id, owner=owner, lease_secs=lease_secs)
            if claimed is not None:
                super().cancel(task_id, reason="user_stop")
            return claimed

    store = _CancelAfterClaim(tmp_path / "tasks.db", clock=clock, network_fs=False).open()
    ran: list[str] = []
    try:
        adm, _ = _admission(store, clock, cap=1)
        rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
        assert rec is not None
        with pytest.raises(r.RunnerTaskCancelled):
            await adm.admit(rec.id)
            ran.append("step body")
        assert ran == []
        assert store.state_of(rec.id) == m.CANCELLED
        assert adm.lane.running == 0 and adm.lane.waiting == 0
    finally:
        store.close()


def test_claim_only_hands_back_no_handle_when_the_start_write_is_refused(
    tmp_path: Path, clock: Clock
) -> None:
    """The container row takes no lane slot, but the fence is the same: without a
    committed ``starting`` write there is no handle to drive the run with."""
    store = _locked_store(tmp_path, clock)
    try:
        adm, _ = _admission(store, clock)
        rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r")
        assert rec is not None
        assert adm.claim_only(rec.id) is None
        assert store.state_of(rec.id) == m.QUEUED
    finally:
        store.close()


def test_a_refused_container_start_reports_the_state_the_requeue_read_back(
    tmp_path: Path, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """``claim_only`` answers None for two different rows, so the requeue's
    read-back is CONSUMED rather than dropped.

    Both refusals hand back the same None: one leaves the requeued ``queued`` row
    the next boot's sweep cancels, the other a row whose state could not even be
    read. An operator looking at a run that started nothing has only this line to
    tell those apart.
    """
    store = _locked_store(tmp_path, clock)
    try:
        adm, _ = _admission(store, clock)
        rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r")
        assert rec is not None
        with caplog.at_level(logging.WARNING, logger=_RUNNER_LOGGER):
            assert adm.claim_only(rec.id) is None
        assert any(
            "taskrunner:r took no container handle; the row is queued" in message
            for message in caplog.messages
        ), caplog.messages
    finally:
        store.close()


# ── recovery (stop reason) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovering_then_reclaim_is_a_new_generation_and_fences_the_old(
    store: TaskStore, clock: Clock
) -> None:
    ladder = RecoveryLadder(clock=clock, rng=random.Random(1))
    adm, sleeps = _admission(store, clock, ladder=ladder)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    h = await adm.admit(rec.id)
    h.running()
    old_gen = h.generation
    decision = adm.decide_recovery(h, unit="sess:task1", reason="stalled")
    assert decision is not None and decision.retry and decision.delay_secs > 0
    assert h.recovering(reason="stalled", delay_secs=decision.delay_secs)
    row = store.get(rec.id)
    assert row.state == m.RECOVERING and row.next_run_at == pytest.approx(
        clock.t + decision.delay_secs
    )
    assert adm.lane.running == 0  # the slot was released for the wait
    # Not eligible before next_run_at: admit waits it out on the fake clock.
    await h.reclaim()
    assert h.generation == old_gen + 1 and adm.lane.running == 1
    assert sleeps.calls and sum(sleeps.calls) == pytest.approx(decision.delay_secs)
    # The interrupted turn's late write is fenced out.
    assert store.transition(rec.id, m.DONE, generation=old_gen) is False
    assert (
        "stale_result",
        {"from_generation": old_gen, "current": h.generation, "wanted": m.DONE},
    ) in _events(store, rec.id)
    assert h.running() and h.done()


def test_l3_ladder_bounds_the_stall_recoveries(clock: Clock) -> None:
    ladder = RecoveryLadder(clock=clock, rng=random.Random(2))
    adm, _ = _admission(None, clock, ladder=ladder)
    h = r.Admitted(adm, "t", m.KIND_TASKRUNNER_STEP, "sess", 0, _slot_held=False)
    verdicts = [adm.decide_recovery(h, unit="u", reason="stall").retry for _ in range(3)]
    assert verdicts == [True, False, False]  # L3: 2 attempts, then escalate
    adm.recovered("u")
    assert adm.decide_recovery(h, unit="u", reason="stall").retry is True


# ── waits: dependency ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_parks_in_waiting_dependency_and_tick_wakes_it(
    store: TaskStore, clock: Clock
) -> None:
    adm, _ = _admission(store, clock, cap=1)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    h = await adm.admit(rec.id)
    h.running()
    gen = h.generation
    signal = DependencySignal(
        kind=KIND_RATE_LIMITED, dependency_scope="github:api", source="test", retry_at=clock.t + 30
    )
    waiting = asyncio.create_task(adm.yield_dependency(h, signal))
    await settle_store_writes(store)
    row = store.get(rec.id)
    assert row.state == m.WAITING_DEPENDENCY
    assert row.wait["dependency_scope"] == "github:api"
    assert row.wait["resume_condition"]["at"] == pytest.approx(clock.t + 30)
    assert adm.lane.running == 0  # slot released; the runtime stays resident
    assert adm.tick() == []  # not due yet
    clock.advance(31)
    assert adm.tick() == [rec.id]
    assert await waiting is True
    # +1 at the wake (claimable ``retry_wait``), +1 at the re-admission's
    # claim: ``running`` was written only once the lane was granted again.
    assert h.generation == gen + 2 and h.state == m.RUNNING
    assert store.get(rec.id).state == m.RUNNING
    assert adm.lane.running == 1  # re-admitted through capacity
    assert h.done()


@pytest.mark.asyncio
async def test_dependency_wait_with_coordinator_shares_one_schedule(
    store: TaskStore, clock: Clock
) -> None:
    lane = r.RunnerLane(2)
    coord = DependencyCoordinator(
        store, clock=clock, rng=random.Random(3), capacity=lambda: lane.effective
    )
    adm, _ = _admission(store, clock, lane=lane, coordinator=coord)
    coord._on_wake = adm.on_wake
    handles = []
    for i in (1, 2):
        rec = adm.accept(kind=m.KIND_WORKFLOW_AGENT, task_id=f"workflow:w:agent{i}")
        h = await adm.admit(rec.id, kind=m.KIND_WORKFLOW_AGENT)
        h.running()
        handles.append(h)
    sig = DependencySignal(kind=KIND_RATE_LIMITED, dependency_scope="gh", source="t", retry_at=None)
    waits = [asyncio.create_task(adm.yield_dependency(h, sig)) for h in handles]
    await settle_store_writes(store)
    await settle_store_writes(store)
    assert coord.scopes() == ["gh"] and sorted(coord.waiters("gh")) == [h.task_id for h in handles]
    assert lane.running == 0
    clock.advance(coord.schedule("gh").retry_at - clock.t + 0.01)
    woken = adm.tick()  # probe: exactly one waiter
    assert len(woken) == 1
    clock.advance(2.0)
    woken += adm.tick()  # ramp: the rest
    assert sorted(woken) == [h.task_id for h in handles]
    assert await asyncio.gather(*waits) == [True, True]
    assert lane.running == 2
    for h in handles:
        assert h.done()


@pytest.mark.asyncio
async def test_terminal_dependency_signal_fails_the_row(store: TaskStore, clock: Clock) -> None:
    coord = DependencyCoordinator(store, clock=clock, rng=random.Random(4))
    adm, _ = _admission(store, clock, coordinator=coord)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    h = await adm.admit(rec.id)
    h.running()
    sig = DependencySignal(kind=KIND_AUTH_FAILED, dependency_scope="gh", source="t")
    assert await adm.yield_dependency(h, sig) is False
    # auth_failed becomes a sign-in wait (waiting_input), not a silent retry.
    assert store.get(rec.id).state == m.WAITING_INPUT
    assert adm.lane.running == 1  # the caller still owns its slot until it settles
    h.fail("auth")


@pytest.mark.asyncio
async def test_dependency_wait_cancelled_by_its_owner_ends_the_row(
    store: TaskStore, clock: Clock
) -> None:
    """The awaiting coroutine is this incarnation's only wake path for the wait,
    so a cancel delivered to it must settle the row rather than leave a
    ``waiting_dependency`` row nothing can resume."""
    adm, _ = _admission(store, clock, cap=1)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    h = await adm.admit(rec.id)
    h.running()
    sig = DependencySignal(
        kind=KIND_RATE_LIMITED, dependency_scope="gh", source="t", retry_at=clock.t + 30
    )
    waiting = asyncio.create_task(adm.yield_dependency(h, sig))
    await settle_store_writes(store)
    assert store.state_of(rec.id) == m.WAITING_DEPENDENCY
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    row = store.get(rec.id)
    assert row.state == m.CANCELLED and row.wait is None
    assert adm.lane.running == 0 and adm.lane.waiting == 0


@pytest.mark.asyncio
async def test_dependency_wait_cancelled_returns_false(store: TaskStore, clock: Clock) -> None:
    adm, _ = _admission(store, clock)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    h = await adm.admit(rec.id)
    h.running()
    sig = DependencySignal(kind=KIND_RATE_LIMITED, dependency_scope="gh", source="t", retry_at=None)
    waiting = asyncio.create_task(adm.yield_dependency(h, sig))
    await settle_store_writes(store)
    assert store.get(rec.id).state == m.WAITING_DEPENDENCY
    assert adm.cancel_wait(rec.id, reason="user stop")
    assert await waiting is False
    assert store.get(rec.id).state == m.CANCELLED


# ── waits: input ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_waiting_input_resumes_with_the_answer(store: TaskStore, clock: Clock) -> None:
    adm, _ = _admission(store, clock, cap=1)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    h = await adm.admit(rec.id)
    h.running()
    gen = h.generation
    waiting = asyncio.create_task(adm.waiting_input(h, tool_call_id="q-1", reason="passphrase?"))
    await settle_store_writes(store)
    row = store.get(rec.id)
    assert row.state == m.WAITING_INPUT and row.wait["tool_call_id"] == "q-1"
    assert adm.lane.running == 0
    assert adm.answer_input("nope", "x") is False  # unknown row: nothing woken
    assert adm.answer_input(rec.id, "yes") is True
    assert await waiting == "yes"
    assert h.generation == gen + 2 and adm.lane.running == 1  # wake +1, re-claim +1
    assert h.done()


@pytest.mark.asyncio
async def test_an_answer_never_ends_a_wait_it_does_not_answer(
    store: TaskStore, clock: Clock
) -> None:
    """An answer answers ONE question, so a row in another wait refuses it.

    The route's read says ``waiting_input``; by the time the write lands the step
    may have finished that question and parked on a dependency instead. Fencing
    on "any wait" would end THAT wait with an answer to a question already over,
    answer 200, and leave the row claimable with its dependency unmet -- so the
    fence is ``waiting_input`` alone, and the wait the row is really in is
    untouched.
    """
    adm, _ = _admission(store, clock, cap=1)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    h = await adm.admit(rec.id)
    h.running()
    parked = asyncio.create_task(
        adm.yield_dependency(
            h,
            DependencySignal(
                kind=KIND_RATE_LIMITED,
                dependency_scope="github:api",
                source="test",
                retry_at=clock.t + 60,
            ),
        )
    )
    await settle_store_writes(store)
    assert store.get(rec.id).state == m.WAITING_DEPENDENCY
    assert adm.answer_input(rec.id, "an answer to a question that is over") is False
    row = store.get(rec.id)
    assert row.state == m.WAITING_DEPENDENCY, "an input answer ended a dependency wait"
    assert row.wait is not None and row.wait["dependency_scope"] == "github:api"
    assert store.events(rec.id)[-1].kind == "rejected_transition"
    parked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parked


@pytest.mark.asyncio
async def test_an_answer_under_a_stale_generation_is_refused(
    store: TaskStore, clock: Clock
) -> None:
    """The route reads the row, then writes: the generation it read is the fence.

    A wait that ended between the two (a deadline, another gateway's answer) took
    the generation with it, so the answer the operator aimed at THAT question is
    refused rather than delivered to the turn that followed it.
    """
    adm, _ = _admission(store, clock, cap=1)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    h = await adm.admit(rec.id)
    h.running()
    waiting = asyncio.create_task(adm.waiting_input(h, tool_call_id="q-1"))
    await settle_store_writes(store)
    stale = store.get(rec.id).generation
    assert adm.answer_input(rec.id, "first", generation=stale) is True
    assert await waiting == "first"
    # The step asks again: same row, new generation, and the earlier read is stale.
    h.running()
    again = asyncio.create_task(adm.waiting_input(h, tool_call_id="q-2"))
    await settle_store_writes(store)
    assert adm.answer_input(rec.id, "late", generation=stale) is False
    assert store.get(rec.id).state == m.WAITING_INPUT
    assert store.events(rec.id)[-1].kind == "stale_result"
    assert adm.answer_input(rec.id, "second", generation=store.get(rec.id).generation) is True
    assert await again == "second"
    assert h.done()


@pytest.mark.asyncio
async def test_an_answer_past_the_durable_bound_is_refused_on_replay(
    store: TaskStore, clock: Clock
) -> None:
    """The durable copy is either the answer or a refusal, never a fragment.

    The wake event is the ONLY copy a restart has, and it is bounded so one
    pasted document cannot become the store's size. A bound that cut silently
    would feed the rebuilt step something the operator never wrote, with nothing
    on the record saying so -- so the event carries the marker and the replay
    reader refuses, which makes the step ask again.
    """
    adm, _ = _admission(store, clock, cap=1)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    h = await adm.admit(rec.id)
    h.running()
    waiting = asyncio.create_task(adm.waiting_input(h, tool_call_id="q-1"))
    await settle_store_writes(store)
    answer = "".join(f"clause {i}; " for i in range(300))
    assert adm.answer_input(rec.id, answer) is True
    # This process still has the whole answer, so the live step is unaffected.
    assert await waiting == answer
    wake = [e for e in store.events(rec.id) if e.kind == "wake"][-1]
    assert len(wake.data["answer"]) < len(answer), "the bound is what this pin is about"
    # A rebuilt step reads the durable copy, and a cut one is not an answer.
    assert adm.recorded_answer(rec.id) is None
    assert wake.data[r.ANSWER_TRUNCATED] is True
    assert wake.data["answer_chars"] == len(answer)
    assert len(wake.data["answer"]) == r._MAX_DURABLE_ANSWER_CHARS
    assert h.done()


@pytest.mark.asyncio
async def test_an_operator_action_from_a_worker_thread_releases_the_parked_run(
    store: TaskStore, clock: Clock
) -> None:
    """The wake reaches the parked coroutine from the thread the ROUTE uses.

    ``POST /api/tasks/{id}`` runs both actions in ``asyncio.to_thread``, so the
    release lands on a worker with no running loop. ``Event.set`` is a loop
    operation -- it resolves the waiter's future, which schedules that future's
    callbacks -- so making it there is the cross-thread call asyncio itself
    refuses under ``loop.set_debug(True)``, and the run parked on the bare
    ``await`` is left waiting for a wake that was already spent.
    """
    loop = asyncio.get_running_loop()
    was_debug = loop.get_debug()
    loop.set_debug(True)  # asyncio's own detector for a cross-thread loop call
    try:
        adm, _ = _admission(store, clock, cap=1)
        first = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
        h1 = await adm.admit(first.id)
        h1.running()
        parked = asyncio.create_task(adm.waiting_input(h1, tool_call_id="q-1"))
        await settle_store_writes(store)
        assert store.get(first.id).state == m.WAITING_INPUT
        assert (
            await asyncio.to_thread(
                adm.cancel_wait, first.id, reason="stopped", generation=h1.generation
            )
            is True
        )
        assert await await_until(parked) is None
        assert store.state_of(first.id) == m.CANCELLED
        # The answer arm of the same route, from the same kind of worker.
        second = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task2")
        h2 = await adm.admit(second.id)
        h2.running()
        waiting = asyncio.create_task(adm.waiting_input(h2, tool_call_id="q-2"))
        await settle_store_writes(store)
        assert (
            await asyncio.to_thread(adm.answer_input, second.id, "yes", generation=h2.generation)
            is True
        )
        assert await await_until(waiting) == "yes"
        assert h2.done() is True
    finally:
        loop.set_debug(was_debug)


@pytest.mark.asyncio
async def test_waiting_input_cancelled_returns_none(store: TaskStore, clock: Clock) -> None:
    adm, _ = _admission(store, clock)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    h = await adm.admit(rec.id)
    h.running()
    waiting = asyncio.create_task(adm.waiting_input(h, tool_call_id="q-2"))
    await settle_store_writes(store)
    adm.cancel_wait(rec.id)
    assert await waiting is None
    assert store.get(rec.id).state == m.CANCELLED


@pytest.mark.asyncio
async def test_a_refused_input_wait_write_ends_the_step_instead_of_parking_on_it(
    store: TaskStore, clock: Clock
) -> None:
    """The wait WRITE is the precondition for parking, as in ``yield_dependency``.

    ``answer_input`` wakes through ``WaitLedger.wake``, which only moves a row the
    store has in a WAITING state. A handle that published ``waiting_input`` over a
    refused write therefore awaited an event nothing could send, while the
    operator's answer was refused against a row that was not waiting.
    """
    adm, _ = _admission(store, clock, cap=1)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    h = await adm.admit(rec.id)
    h.running()
    refused: list[str] = []

    def _refuse(task_id, wait, *, generation=None):
        refused.append(task_id)
        return False

    store.enter_wait = _refuse  # type: ignore[method-assign]
    try:
        assert await adm.waiting_input(h, tool_call_id="q-1", reason="passphrase?") is None
    finally:
        del store.enter_wait
    assert refused == [rec.id]
    # The row never claimed to be waiting and the handle never claimed it either.
    assert store.state_of(rec.id) == m.RUNNING
    assert h.state == m.RUNNING
    # The step holds its lane slot again, so the caller's ``None`` is a step that
    # did not complete -- not a slot leaked to a coroutine nobody can wake.
    assert h.slot_held and adm.lane.running == 1
    assert adm.answer_input(rec.id, "yes") is False


@pytest.mark.asyncio
async def test_the_resume_after_a_wait_returns_the_stores_word_and_not_the_handles(
    store: TaskStore, clock: Clock
) -> None:
    """``running()`` short-circuits on ``_state == RUNNING`` WITHOUT a store call,
    so a resume that returned it would answer True from memory while the row sat
    in ``starting``. The resume writes unconditionally, so its boolean is the
    row's."""
    adm, _ = _admission(store, clock, cap=1)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    h = await adm.admit(rec.id)
    h.running()
    ledger = WaitLedger(store, clock=clock)
    assert store.enter_wait(
        rec.id,
        WaitRecord.input("q-1", since=clock(), reason="passphrase?").to_dict(),
        generation=h.generation,
    )
    assert ledger.wake(rec.id, reason="input answered") is not None
    # The row is parked claimable and the HANDLE still believes ``running``: the
    # exact shape in which a short-circuit would have skipped the write.
    h._state = m.RUNNING
    assert store.state_of(rec.id) == m.RETRY_WAIT

    assert await adm._resume_after_wait(h) is True

    assert store.state_of(rec.id) == m.RUNNING, "the resume answered True with no write"
    assert h.state == m.RUNNING


@pytest.mark.asyncio
async def test_the_two_wait_paths_publish_a_waiting_state_before_they_await(
    store: TaskStore, clock: Clock
) -> None:
    """The coupling the resume's write depends on, pinned rather than incidental.

    Both wait paths set ``_state`` to a WAITING state before ``await ev.wait()``,
    which is what makes the resume's ``running`` write a real write. A path that
    parked with ``_state`` left at ``running`` would resume through a
    short-circuit and leave the row wherever the wake put it.
    """
    adm, _ = _admission(store, clock, cap=2, coordinator=None)
    seen: list[str] = []
    for task_id, park in (
        ("taskrunner:r:task1", "input"),
        ("taskrunner:r:task2", "dependency"),
    ):
        rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id=task_id)
        h = await adm.admit(rec.id)
        h.running()
        if park == "input":
            task = asyncio.create_task(adm.waiting_input(h, tool_call_id="q"))
        else:
            task = asyncio.create_task(
                adm.yield_dependency(
                    h,
                    DependencySignal(
                        kind=KIND_RATE_LIMITED,
                        dependency_scope="github:api",
                        source="github",
                        retry_at=clock() + 30.0,
                    ),
                )
            )
        await settle_store_writes(store)
        await asyncio.sleep(0)
        seen.append(h.state)
        assert store.state_of(rec.id) == h.state, "the handle published a state the row lacks"
        adm.cancel_wait(rec.id)
        await task
    assert seen == [m.WAITING_INPUT, m.WAITING_DEPENDENCY]
    assert set(seen) <= m.WAITING


@pytest.mark.asyncio
async def test_a_resume_onto_a_row_someone_else_ended_answers_false(
    store: TaskStore, clock: Clock
) -> None:
    """The False half of the same fence: the step must not carry on over a row
    that is terminal or in another wait."""
    adm, _ = _admission(store, clock, cap=1)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    h = await adm.admit(rec.id)
    h.running()
    assert store.enter_wait(
        rec.id,
        WaitRecord.input("q-1", since=clock(), reason="passphrase?").to_dict(),
        generation=h.generation,
    )
    # Woken with the row still in its wait: not running, so not resumable.
    assert await adm._resume_after_wait(h) is False
    assert store.cancel(rec.id, reason="user_stop") is not None
    assert await adm._resume_after_wait(h) is False
