"""``taskq.adapters.runner``: TaskRunner steps and workflow agent calls as rows.

Fake clock, fake sleep, a real SQLite store in ``tmp_path``. No kiro-cli, no
real waits: every ``await`` here resolves on the loop's own tick.
"""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from overload_fakes import Clock, open_task_store, settle_store_writes

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


class _Sleeps:
    """Records requested sleeps; advances the fake clock instead of waiting."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, secs: float) -> None:
        self.calls.append(secs)
        self.clock.advance(secs)
        await asyncio.sleep(0)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> TaskStore:
    yield from open_task_store(tmp_path, clock)


def _admission(store: TaskStore | None, clock: Clock, **kw) -> tuple[r.RunnerAdmission, _Sleeps]:
    sleeps = _Sleeps(clock)
    lane = kw.pop("lane", None) or r.RunnerLane(kw.pop("cap", 1), mode=kw.pop("mode", r.MODE_AIMD))
    adm = r.RunnerAdmission(store, lane=lane, clock=clock, sleep=sleeps, **kw)
    return adm, sleeps


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


# ── the claim/start boundary: nothing executes under an ``admitted`` row ──────


class _LockedAfterClaim(TaskStore):
    """A REAL store whose FILE another connection locks the instant a claim
    commits, so the very next write gets SQLite's own ``database is locked``.

    That is the shape of a lock the store's ``busy_timeout`` gives up on: a
    competing writer arrives between the claim and the start write.
    ``clears_after_one_refusal`` is the transient case (the writer is gone again
    by the next statement); False keeps the file locked for the whole test, which
    is the outage case.
    """

    def __init__(self, *args, clears_after_one_refusal: bool = True, **kw) -> None:
        super().__init__(*args, **kw)
        self._blocker = None
        self._clears = bool(clears_after_one_refusal)

    def claim(self, task_id, *, owner=None, lease_secs=None):
        claimed = super().claim(task_id, owner=owner, lease_secs=lease_secs)
        if claimed is not None and self._blocker is None:
            # ``claim`` runs on the store's writer thread and ``close`` on the
            # test's, so the blocker is not pinned to either.
            self._blocker = sqlite3.connect(str(self.path), timeout=0, check_same_thread=False)
            self._blocker.execute("BEGIN EXCLUSIVE")
        return claimed

    def transition(self, *args, **kw):
        try:
            return super().transition(*args, **kw)
        finally:
            if self._clears:
                self.unlock()

    def unlock(self) -> None:
        blocker, self._blocker = self._blocker, None
        if blocker is not None:
            blocker.execute("ROLLBACK")
            blocker.close()

    def close(self) -> None:
        self.unlock()
        super().close()


def _locked_store(tmp_path: Path, clock: Clock, **kw) -> "_LockedAfterClaim":
    return _LockedAfterClaim(
        tmp_path / "tasks.db",
        window=8,
        clock=clock,
        network_fs=False,
        busy_timeout_secs=0.05,
        **kw,
    ).open()


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
        with caplog.at_level(logging.WARNING, logger="kiro_crew.taskq.adapters.runner"):
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
        assert await asyncio.wait_for(parked, 10) is None
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
        assert await asyncio.wait_for(waiting, 10) == "yes"
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


def _delete_mode_store(tmp_path: Path, clock: Clock) -> TaskStore:
    """A store in the journal mode a data home on a network filesystem gets.

    ``journal_mode=delete`` is what the store auto-detects over NFS/SMB and what
    ``agent.task_store_journal_mode=delete`` selects, and it is the mode in which
    a competing writer blocks READERS -- so a bare ``SELECT`` there is exactly as
    failure-prone as the transaction before it. WAL, the local default, does not
    block readers, which is why no other test in this file reaches this.
    """
    return TaskStore(
        tmp_path / "tasks.db", clock=clock, network_fs=True, busy_timeout_secs=0.05
    ).open()


def _claimed_unstarted(store: TaskStore, task_id: str = "taskrunner:r:task1") -> int:
    store.accept_one(m.TaskRecord(id=task_id, kind=m.KIND_TASKRUNNER_STEP))
    claimed = store.claim(task_id)
    assert claimed is not None and store.state_of(task_id) == m.ADMITTED
    return claimed.generation


def _one_warning(caplog: pytest.LogCaptureFixture) -> str:
    said = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert len(said) == 1, said
    return said[0]


def test_a_locked_read_after_the_requeue_is_the_stores_typed_error(
    tmp_path: Path, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """The refusal where ONLY ``_requeue_unstarted``'s read-back is refused.

    The rival takes its lock after ``transition`` has RETURNED, so the requeue
    commits and the row is ``queued`` while the read raises. An
    ``except TaskStoreUnavailable`` arm cannot fire for a raw
    ``sqlite3.OperationalError``, so the store's own read boundary is the only
    thing between this and a database error raised into ``admit`` -- and because
    the committed row is ``queued`` here and ``admitted`` in the sibling case
    below, the one ``None`` may not have either state written under it as fact.
    """
    store = _delete_mode_store(tmp_path, clock)
    task_id = "taskrunner:r:task1"
    try:
        assert store.journal_mode == "delete"
        generation = _claimed_unstarted(store, task_id)
        rival = sqlite3.connect(str(store.path), timeout=0, check_same_thread=False)
        committing = store.transition

        def _commit_then_let_the_rival_in(*args: object, **kw: object) -> bool:
            ok = bool(committing(*args, **kw))
            rival.execute("BEGIN EXCLUSIVE")
            rival.execute("UPDATE tasks SET updated_at=1 WHERE id=?", (task_id,))
            return ok

        store.transition = _commit_then_let_the_rival_in  # type: ignore[method-assign]
        try:
            with caplog.at_level(logging.WARNING, logger="kiro_crew.taskq.adapters.runner"):
                assert r.RunnerAdmission._requeue_unstarted(store, task_id, generation) is None
        finally:
            store.transition = committing  # type: ignore[method-assign]
            rival.execute("ROLLBACK")
            rival.close()
        assert store.state_of(task_id) == m.QUEUED, "the requeue this None reports on committed"
        said = _one_warning(caplog)
        assert m.QUEUED in said and m.ADMITTED in said, "one refusal's row was stated as fact"
    finally:
        store.close()


def test_a_locked_requeue_answers_the_same_none_over_a_row_that_stayed_admitted(
    tmp_path: Path, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """The other refusal behind that one ``None``: the rival already holds the
    lock, so the requeue itself never commits and the row keeps ``admitted``
    under a lapsing lease. Both refusals are unstarted and claimable, which is
    all the warning may claim -- the row's state is the half it cannot know."""
    store = _delete_mode_store(tmp_path, clock)
    task_id = "taskrunner:r:task1"
    try:
        generation = _claimed_unstarted(store, task_id)
        rival = sqlite3.connect(str(store.path), timeout=0, check_same_thread=False)
        rival.execute("BEGIN EXCLUSIVE")
        rival.execute("UPDATE tasks SET updated_at=1 WHERE id=?", (task_id,))
        try:
            with caplog.at_level(logging.WARNING, logger="kiro_crew.taskq.adapters.runner"):
                assert r.RunnerAdmission._requeue_unstarted(store, task_id, generation) is None
        finally:
            rival.execute("ROLLBACK")
            rival.close()
        assert store.state_of(task_id) == m.ADMITTED
        said = _one_warning(caplog)
        assert m.QUEUED in said and m.ADMITTED in said, "one refusal's row was stated as fact"
    finally:
        store.close()


def test_claim_only_leaves_a_queued_row_the_next_boots_sweep_owns(
    tmp_path: Path, clock: Clock
) -> None:
    """``claim_only``'s ``None`` is a fence, and this is what it costs.

    The requeued ``queued`` container row has no dispatcher (``KIND_SUBAGENT`` is
    the only kind ever fetched) and the boot reconciler is ACTIVE-only, so it
    lingers for the rest of THIS gateway's life -- and THIS incarnation's own
    adopt sweep skips it too, because ``params.accepted_by`` names the
    incarnation that is still running. Bounded, not forever: the next boot's
    ``adopt_orphaned_rows`` cancels it as never started.
    """
    store = _locked_store(tmp_path, clock)
    try:
        adm, _ = _admission(store, clock)
        rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r")
        assert rec is not None and adm.claim_only(rec.id) is None
        assert store.state_of(rec.id) == m.QUEUED
        assert reconcile_on_boot(store).examined == 0, "queued is not an ACTIVE row"
        assert store.fetch_dispatchable(m.KIND_SUBAGENT, limit=8) == []
        mine = r.adopt_orphaned_rows(store, kinds=(m.KIND_TASKRUNNER_STEP,))
        assert mine.cancelled == [], "this incarnation cancelled a row it may still be admitting"
        assert store.state_of(rec.id) == m.QUEUED
    finally:
        store.close()
    reopened = TaskStore(tmp_path / "tasks.db", clock=clock, network_fs=False).open()
    try:
        assert r.adopt_orphaned_rows(reopened, kinds=(m.KIND_TASKRUNNER_STEP,)).cancelled == [
            "taskrunner:r"
        ]
        assert reopened.state_of("taskrunner:r") == m.CANCELLED
    finally:
        reopened.close()


# ── adopt (the recovery adapter) ─────────────────────────────────────────────


def _crashed_row(store: TaskStore, task_id: str, *, safe: bool, parent: str | None = None) -> None:
    rec = m.TaskRecord(
        id=task_id,
        kind=m.KIND_TASKRUNNER_STEP,
        parent_id=parent,
        params={"task_id": "run1", r.PARAM_SAFE_RETRY: safe},
    )
    store.accept_one(rec)
    store.claim(task_id, owner="dead-incarnation")
    store.transition(task_id, m.STARTING)
    store.transition(task_id, m.RUNNING)


def test_adopt_resumes_safe_rows_and_parks_unsafe_ones(store: TaskStore, clock: Clock) -> None:
    _crashed_row(store, "taskrunner:run1", safe=True)
    _crashed_row(store, "taskrunner:run1:task2", safe=True, parent="taskrunner:run1")
    _crashed_row(store, "taskrunner:run2", safe=False)
    _crashed_row(store, "taskrunner:run2:task1", safe=False, parent="taskrunner:run2")
    resumed: list[str] = []

    report = r.adopt_orphaned_rows(store, resume=lambda rec: resumed.append(rec.id) or True)

    assert report.examined == 4
    assert resumed == ["taskrunner:run1"] and report.resumed == ["taskrunner:run1"]
    assert store.get("taskrunner:run1").state == m.RECOVERING  # claimable for the resume
    assert store.get("taskrunner:run1:task2").state == m.FAILED  # re-run from the checkpoint
    assert store.get("taskrunner:run2").state == m.UNKNOWN_SIDE_EFFECT
    assert store.get("taskrunner:run2:task1").state == m.UNKNOWN_SIDE_EFFECT
    assert sorted(report.unknown_side_effect) == ["taskrunner:run2", "taskrunner:run2:task1"]
    # Idempotent: nothing left to adopt.
    again = r.adopt_orphaned_rows(store, resume=lambda rec: True)
    assert again.examined == 1 and again.resumed == ["taskrunner:run1"]


def test_adopt_skips_rows_this_incarnation_still_owns(store: TaskStore, clock: Clock) -> None:
    rec = m.TaskRecord(
        id="taskrunner:live", kind=m.KIND_TASKRUNNER_STEP, params={r.PARAM_SAFE_RETRY: True}
    )
    store.accept_one(rec)
    store.claim(rec.id)  # this incarnation, fresh lease
    store.transition(rec.id, m.STARTING)
    store.transition(rec.id, m.RUNNING)
    report = r.adopt_orphaned_rows(store, resume=lambda _r: True)
    assert report.skipped == ["taskrunner:live"] and store.get(rec.id).state == m.RUNNING


def test_adopt_pages_past_its_scan_size_so_no_queued_row_is_stranded(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backlog larger than one page is settled in full, not to the page size.

    ``queued`` is the one claimable state no other sweep reaches -- it is not
    ``ACTIVE``, so the boot reconciler does not see it, and a runner kind has no
    fair-dispatch fetch -- so a row this sweep does not reach stays accepted with
    nothing that will ever run it. A cap on the SCAN is therefore a cap on what a
    restart can recover, which is the loss the store exists to prevent. The page
    size is shrunk here so the paging is what is measured rather than the volume.
    """
    monkeypatch.setattr(r, "_ADOPT_SCAN_PAGE", 3)
    ids = [f"taskrunner:orphan{i}" for i in range(7)]
    first = TaskStore(tmp_path / "tasks.db", window=8, clock=clock, network_fs=False).open()
    try:
        adm, _ = _admission(first, clock)
        for task_id in ids:
            assert adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id=task_id) is not None
            assert first.state_of(task_id) == m.QUEUED
    finally:
        first.close()

    reopened = TaskStore(tmp_path / "tasks.db", window=8, clock=clock, network_fs=False).open()
    try:
        report = r.adopt_orphaned_rows(reopened, kinds=(m.KIND_TASKRUNNER_STEP,))
        assert sorted(report.cancelled) == sorted(ids), "the sweep stopped at a page boundary"
        assert not any(reopened.state_of(task_id) == m.QUEUED for task_id in ids)
    finally:
        reopened.close()


def test_a_claim_between_two_pages_does_not_hide_a_queued_orphan(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep runs BESIDE a live gateway, so OFFSET alone proves nothing.

    A claim removes a row from ``queued`` and every row behind it shifts one
    place, so the row that lands where the cursor already is is never examined --
    one row lost per claim, left ``queued`` with no dispatcher and no reconciler.
    The pass therefore repeats while it discovers ids it had not read: the
    displaced row is EARLIER than the cursor, so the next pass reads it.
    """
    monkeypatch.setattr(r, "_ADOPT_SCAN_PAGE", 3)
    ids = [f"taskrunner:orphan{i}" for i in range(7)]
    first = TaskStore(tmp_path / "tasks.db", window=8, clock=clock, network_fs=False).open()
    try:
        adm, _ = _admission(first, clock)
        for task_id in ids:
            assert adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id=task_id) is not None
    finally:
        first.close()

    reopened = TaskStore(tmp_path / "tasks.db", window=8, clock=clock, network_fs=False).open()
    try:
        real_list_rows = reopened.list_rows
        pages: list[int] = []
        claimed: list[str] = []

        def _claim_between_pages(**kw: object) -> list:
            page = real_list_rows(**kw)  # type: ignore[arg-type]
            pages.append(len(page))
            if len(pages) == 1:
                assert reopened.claim(ids[0]) is not None
                claimed.append(ids[0])
            return page

        monkeypatch.setattr(reopened, "list_rows", _claim_between_pages)
        report = r.adopt_orphaned_rows(reopened, kinds=(m.KIND_TASKRUNNER_STEP,))
        monkeypatch.setattr(reopened, "list_rows", real_list_rows)
        assert claimed == [ids[0]], "the interleaving never ran, so this pin measured nothing"
        stranded = [t for t in ids if reopened.state_of(t) == m.QUEUED]
        assert stranded == [], "a queued orphan the shift displaced was never examined"
        assert sorted(report.cancelled) == sorted(ids[1:])
        # The claimed row is the live gateway's; the sweep leaves it alone.
        assert reopened.state_of(ids[0]) == m.ADMITTED
    finally:
        reopened.close()


def test_adopt_declined_resume_fails_the_row(store: TaskStore, clock: Clock) -> None:
    _crashed_row(store, "taskrunner:gone", safe=True)
    report = r.adopt_orphaned_rows(store, resume=lambda _r: False)
    assert report.failed == ["taskrunner:gone"]
    assert store.get("taskrunner:gone").state == m.FAILED


def _reopen(path: Path, clock: Clock) -> TaskStore:
    """A NEW incarnation on the same file -- what a gateway restart is."""
    return TaskStore(path, window=8, clock=clock, network_fs=False).open()


@pytest.mark.asyncio
async def test_adopt_ends_the_rows_a_dead_incarnation_accepted_or_admitted(
    tmp_path: Path, clock: Clock
) -> None:
    """Crash after accept (``queued``), after the claim (``admitted``) and after
    the ``starting`` write.

    The boot reconciler examines only ACTIVE rows and requeues an ``admitted``
    one; ``workflow_agent`` has no dispatcher, so both claimable rows are the
    sweep's alone. Repeated sweeps settle nothing twice.
    """
    from kiro_crew.taskq.reconcile import reconcile_on_boot

    path = tmp_path / "tasks.db"
    store_a = _reopen(path, clock)
    adm_a, _ = _admission(store_a, clock, cap=2)
    started = adm_a.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:old:agent1")
    queued = adm_a.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:old:agent2")
    admitted = adm_a.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:old:agent3")
    await adm_a.admit(started.id, kind=m.KIND_WORKFLOW_AGENT)
    assert store_a.claim(admitted.id) is not None  # claimed, never started
    assert store_a.state_of(queued.id) == m.QUEUED
    store_a.close()  # -- crash: no row has an owner any more

    store_b = _reopen(path, clock)
    try:
        reconcile_on_boot(store_b, now=clock())
        assert store_b.state_of(queued.id) == m.QUEUED  # ACTIVE-only, by design
        assert store_b.state_of(admitted.id) == m.QUEUED  # requeued: no runtime, no effect
        report = r.adopt_orphaned_rows(
            store_b, kinds=(m.KIND_WORKFLOW_AGENT,), resume=lambda _rec: False
        )
        assert sorted(report.cancelled) == [queued.id, admitted.id]
        assert store_b.state_of(queued.id) == m.CANCELLED
        assert store_b.state_of(admitted.id) == m.CANCELLED
        assert store_b.state_of(started.id) == m.UNKNOWN_SIDE_EFFECT
        # The two numbers the leak corrupted: nothing is left waiting.
        assert store_b.oldest_wait_secs() == 0.0
        assert m.QUEUED not in store_b.count_by_state()
        again = r.adopt_orphaned_rows(
            store_b, kinds=(m.KIND_WORKFLOW_AGENT,), resume=lambda _rec: False
        )
        assert again.examined == 0 and again.cancelled == []
    finally:
        store_b.close()


@pytest.mark.asyncio
async def test_adopt_leaves_a_row_this_incarnation_just_accepted_alone(
    store: TaskStore, clock: Clock
) -> None:
    """The regression pin for the live-``admit`` race: a queued row whose owner
    coroutine is parked in ``admit`` is NOT an orphan. ``params.accepted_by``
    is what says so -- a queued row has no lease to read."""
    adm, _ = _admission(store, clock, cap=1)
    holder = adm.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:live:agent1")
    waiting = adm.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:live:agent2")
    assert waiting.params[r.PARAM_ACCEPTED_BY] == store.incarnation
    h = await adm.admit(holder.id, kind=m.KIND_WORKFLOW_AGENT)
    parked = asyncio.create_task(adm.admit(waiting.id, kind=m.KIND_WORKFLOW_AGENT))
    await settle_store_writes(store)
    await settle_store_writes(store)
    assert store.state_of(waiting.id) == m.QUEUED

    report = r.adopt_orphaned_rows(store, kinds=(m.KIND_WORKFLOW_AGENT,), resume=lambda _rec: False)

    assert report.cancelled == [] and store.state_of(waiting.id) == m.QUEUED
    assert h.done()
    live = await parked  # the slot freed and the live admission claimed it
    assert live.generation == 1 and live.done()


def test_a_re_accepted_queued_row_names_the_new_incarnation_as_its_owner(
    tmp_path: Path, clock: Clock
) -> None:
    """A row RE-accepted after a restart belongs to THIS incarnation.

    ``accept`` hands an existing claimable row back instead of duplicating it --
    that is how a resume re-attaches -- and the queued-orphan sweep's owner test
    is ``params.accepted_by``. A returned row that still names the dead
    incarnation is cancelled by the sweep while the live ``admit`` is parked on
    it, which is a queue place lost for as long as the store lives.
    """
    path = tmp_path / "tasks.db"
    store_a = _reopen(path, clock)
    adm_a, _ = _admission(store_a, clock, cap=1)
    first = adm_a.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:resumed:agent1")
    assert first is not None and first.params[r.PARAM_ACCEPTED_BY] == store_a.incarnation
    store_a.close()  # -- crash while the row was still queued

    store_b = _reopen(path, clock)
    try:
        assert store_b.incarnation != first.params[r.PARAM_ACCEPTED_BY]
        adm_b, _ = _admission(store_b, clock, cap=1)
        again = adm_b.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:resumed:agent1")
        assert again is not None and again.state == m.QUEUED  # re-attached, not duplicated
        # The handed-back record AND the row on disk name the new owner.
        assert again.params[r.PARAM_ACCEPTED_BY] == store_b.incarnation
        assert store_b.get(again.id).params[r.PARAM_ACCEPTED_BY] == store_b.incarnation
        # Nothing else on the row moved: same state, same generation.
        assert store_b.get(again.id).generation == 0

        report = r.adopt_orphaned_rows(
            store_b, kinds=(m.KIND_WORKFLOW_AGENT,), resume=lambda _rec: False
        )
        assert report.cancelled == [] and store_b.state_of(again.id) == m.QUEUED
    finally:
        store_b.close()


def test_adopt_never_touches_a_claimable_subagent_row(tmp_path: Path, clock: Clock) -> None:
    """A queued ``subagent`` row is the window refill's, and the sweep is
    kind-scoped so it can never be reached."""
    path = tmp_path / "tasks.db"
    store_a = _reopen(path, clock)
    store_a.accept_one(m.TaskRecord(id="sub-1", kind=m.KIND_SUBAGENT, params={"prompt": "p"}))
    store_a.close()
    store_b = _reopen(path, clock)
    try:
        report = r.adopt_orphaned_rows(store_b, resume=lambda _rec: False)
        assert report.cancelled == [] and report.examined == 0
        assert store_b.state_of("sub-1") == m.QUEUED
    finally:
        store_b.close()


@pytest.mark.asyncio
async def test_adopt_leaves_a_retry_wait_row_carrying_an_answer_alone(
    tmp_path: Path, clock: Clock
) -> None:
    """``retry_wait`` is claimable but is NOT swept: the operator's answer is on
    THIS row's wake event, and a re-run under a ``~N`` id could not read it.
    ``test_answer_is_restored_from_the_wake_event_after_a_rebuild`` is the other
    half -- the row is meant to survive the restart and be re-admitted."""
    path = tmp_path / "tasks.db"
    store_a = _reopen(path, clock)
    adm_a, _ = _admission(store_a, clock, cap=1)
    rec = adm_a.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:ask")
    h = await adm_a.admit(rec.id)
    h.running()
    wait = asyncio.create_task(adm_a.waiting_input(h, tool_call_id="q1", reason="password?"))
    await settle_store_writes(store_a)
    assert adm_a.answer_input(rec.id, "hunter2") is True
    wait.cancel()  # -- crash before the re-admission
    with pytest.raises(asyncio.CancelledError):
        await wait
    assert store_a.state_of(rec.id) == m.RETRY_WAIT
    store_a.close()

    store_b = _reopen(path, clock)
    try:
        report = r.adopt_orphaned_rows(
            store_b, kinds=(m.KIND_TASKRUNNER_STEP,), resume=lambda _rec: False
        )
        assert report.cancelled == [] and store_b.state_of(rec.id) == m.RETRY_WAIT
        adm_b, _ = _admission(store_b, clock, cap=1)
        assert adm_b.recorded_answer(rec.id) == "hunter2"
    finally:
        store_b.close()


def test_legacy_import_row_is_adopted(store: TaskStore, clock: Clock) -> None:
    from kiro_crew.taskq.migrate import legacy_taskrunner_records

    runs = Path(store.path).parent / "runs.json"
    runs.write_text(
        '[{"task_id": "old_1", "status": "paused", "name": "n", "spec_path": "s.md"}]',
        encoding="utf-8",
    )
    for rec in legacy_taskrunner_records(runs, now=clock.t):
        store.insert_if_absent(rec)
    row = store.get("taskrunner:old_1")
    assert row.state == m.RECOVERING and row.side_effect_class == m.SIDE_EFFECT_UNKNOWN
    # An import carries no safe_retry: it is NOT re-run blind.
    report = r.adopt_orphaned_rows(store, resume=lambda _r: True)
    assert report.unknown_side_effect == ["taskrunner:old_1"]


# ── factory ──────────────────────────────────────────────────────────────────


def test_runner_admission_for_follows_the_manager_cap(store: TaskStore) -> None:
    manager = SimpleNamespace(_taskq=store, max_concurrent=5)
    cfg = SimpleNamespace(
        agent=SimpleNamespace(adaptive_concurrency_mode="aimd", admit_wait_secs=7)
    )
    adm = r.runner_admission_for(manager, cfg=cfg, pressure=lambda: SimpleNamespace(admitted=True))
    assert adm.store is store and adm.lane.effective == 5
    manager.max_concurrent = 2  # the controller's set_effective_cap landed
    assert adm.lane.effective == 2
    # Tracking the cap is not a WAKE: with a waiter parked, a raise has to be
    # pushed in as pump(). Pinned by test_subagent_config_hot_reload.py
    # ::TestCapRaiseReachesTheRunnerLane.
    cfg.agent.adaptive_concurrency_mode = "fixed"
    manager.max_concurrent = 1
    assert adm.lane.mode == r.MODE_FIXED and adm.lane.effective == 1
    assert adm._admit_wait == 7.0


# ── the durable state machine is the only path ───────────────────────────────


@pytest.mark.asyncio
async def test_required_store_missing_refuses_instead_of_lane_only(clock: Clock) -> None:
    """With ``agent.task_queue_enabled`` on, no store means REFUSE (typed):
    lane-only admission would hand out generation-0 handles a restart forgets."""
    adm, _ = _admission(
        None, clock, cap=1, require_store=True, store_error=lambda: "tasks.db: disk I/O error"
    )
    with pytest.raises(r.RunnerAdmissionRefused, match="disk I/O error"):
        adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    with pytest.raises(r.RunnerAdmissionRefused):
        await adm.admit("taskrunner:r:task1")
    assert adm.lane.running == 0
    # The legacy queue-off shape is unchanged: lane-only, generation 0.
    legacy, _ = _admission(None, clock, cap=1)
    h = await legacy.admit("legacy")
    assert h.generation == 0 and h.done()


@pytest.mark.asyncio
async def test_failed_claim_never_starts_generation_zero(
    store: TaskStore, clock: Clock, monkeypatch
) -> None:
    adm, _ = _admission(store, clock, cap=1)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")

    def _boom(_task_id):
        raise TaskStoreUnavailable("locked")

    monkeypatch.setattr(store, "claim", _boom)
    with pytest.raises(r.RunnerAdmissionRefused):
        await adm.admit(rec.id)
    assert adm.lane.running == 0
    assert store.state_of(rec.id) == m.QUEUED  # still dispatchable, never started


@pytest.mark.asyncio
async def test_settle_marks_settled_only_after_the_terminal_write_or_a_durable_retry(
    store: TaskStore, clock: Clock, monkeypatch
) -> None:
    adm, _ = _admission(store, clock, cap=1)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    h = await adm.admit(rec.id)
    h.running()
    real_finish = store.finish
    outage = {"on": True}

    def _flaky(*a, **kw):
        if outage["on"]:
            raise TaskStoreUnavailable("store down")
        return real_finish(*a, **kw)

    monkeypatch.setattr(store, "finish", _flaky)
    assert h.done() is False
    # The row is still live in the store, the write is owned by the retry,
    # the slot is released so the lane is not leaked -- and nothing forgot
    # the task while its terminal write is outstanding.
    assert store.state_of(rec.id) == m.RUNNING
    assert adm.stats()["pending_terminal_writes"] == 1
    assert adm.lane.running == 0
    assert h.done() is False  # idempotent: no second write attempt is queued
    outage["on"] = False
    assert adm.retry_terminal_writes() == 1
    assert store.state_of(rec.id) == m.DONE
    assert adm.stats()["pending_terminal_writes"] == 0
    # tick() replays too, and a fenced row (another owner ended it) is dropped.
    rec2 = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task2")
    adm.defer_terminal_write(rec2.id, 99, m.DONE, result_ref=None, error=None)
    adm.tick()
    assert adm.stats()["pending_terminal_writes"] == 0


@pytest.mark.asyncio
async def test_the_next_admit_replays_a_terminal_write_the_store_kept(
    store: TaskStore, clock: Clock, monkeypatch
) -> None:
    """The run's own next start is what commits a kept terminal write.

    ``settle`` keeps the write rather than losing it, but only a REPLAYER makes
    that a promise, and a run that carries on reaches ``admit`` and nothing else:
    the pump's off-loop sweep never calls ``tick``, and the gateway drops the
    ``tick`` hook entirely once a dependency coordinator attaches. Without a
    replayer on this path the finished step's row stays ``running`` and leased for
    the life of the process, and the next boot reconciles it as an interrupted
    run -- a step that succeeded, recorded as one that may not have.
    """
    adm, _ = _admission(store, clock, cap=2)
    first = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
    second = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task2")
    h1 = await adm.admit(first.id)
    h1.running()
    real_finish = store.finish
    outage = {"on": True}

    def _flaky(*a, **kw):
        if outage["on"]:
            raise TaskStoreUnavailable("store down")
        return real_finish(*a, **kw)

    monkeypatch.setattr(store, "finish", _flaky)
    assert await h1.done_async() is False
    assert store.state_of(first.id) == m.RUNNING
    assert store.get(first.id).lease_owner == store.incarnation
    assert adm.stats()["pending_terminal_writes"] == 1
    outage["on"] = False
    # The run carries on. No tick, no direct retry call: the next step's own
    # admission is the replayer.
    h2 = await adm.admit(second.id)
    assert store.state_of(first.id) == m.DONE, "the finished step's row was never settled"
    assert store.get(first.id).lease_owner is None
    assert adm.stats()["pending_terminal_writes"] == 0
    assert await h2.done_async() is True


# ── a deferred terminal write is durable by reconcile ───────────────────────


@pytest.mark.asyncio
async def test_crash_during_a_deferred_terminal_write_reconciles_the_row(
    tmp_path: Path, clock: Clock, monkeypatch
) -> None:
    """The store refuses the terminal write; the process crashes before the
    retry lands. Nothing is lost: the row is still ``running`` under the dead
    incarnation's lease (the retry never releases it), so the next boot's
    reconcile settles it -- ``unknown_side_effect`` for the default class, a
    claimable ``recovering`` row for side-effect-free (retry-safe) work. The pending write is
    therefore durable BY RECONCILE, not by a second persisted record."""
    from kiro_crew.taskq.reconcile import reconcile_on_boot

    path = tmp_path / "tasks.db"
    store_a = TaskStore(path, window=8, clock=clock, network_fs=False).open()
    adm, _ = _admission(store_a, clock, cap=2)
    unknown = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:unknown")
    safe = adm.accept(
        kind=m.KIND_TASKRUNNER_STEP,
        task_id="taskrunner:r:safe",
        side_effect_class=m.SIDE_EFFECT_NONE,
    )
    h1 = await adm.admit(unknown.id)
    h2 = await adm.admit(safe.id)
    h1.running()
    h2.running()

    def _down(*a, **kw):
        raise TaskStoreUnavailable("store down")

    monkeypatch.setattr(store_a, "finish", _down)
    assert h1.done() is False and h2.done() is False
    assert adm.stats()["pending_terminal_writes"] == 2
    # The lease is still the dead owner's: a running row is not claimable, so
    # nothing in the live process could start a second copy meanwhile.
    for rec in (store_a.get(unknown.id), store_a.get(safe.id)):
        assert rec is not None and rec.state == m.RUNNING
        assert rec.lease_owner == store_a.incarnation
    # -- crash: the retry never runs; a new incarnation boots on the same file.
    store_a.close()
    store_b = TaskStore(path, window=8, clock=clock, network_fs=False).open()
    report = reconcile_on_boot(store_b, now=clock())
    assert report.examined == 2 and report.awaiting_adapter == 2
    # The boot reconciler drops the dead owner's lease and hands runner kinds
    # to the runner adapter, which settles them by class.
    resumed: list[str] = []
    adopted = r.adopt_orphaned_rows(store_b, resume=lambda rec: resumed.append(rec.id) or True)
    assert unknown.id in adopted.unknown_side_effect
    assert store_b.state_of(unknown.id) == m.UNKNOWN_SIDE_EFFECT
    assert resumed == [safe.id]
    assert store_b.state_of(safe.id) == m.RECOVERING  # claimable for the runner's own admit
    adm_b, _ = _admission(store_b, clock, cap=2)
    clock.advance(3600)
    again = await adm_b.admit(safe.id)
    assert again.generation > h2.generation
    store_b.close()


# ── an accepted answer survives a crash before the grant ─────────────────────


@pytest.mark.asyncio
async def test_answer_is_restored_from_the_wake_event_after_a_rebuild(
    tmp_path: Path, clock: Clock
) -> None:
    """answer -> crash before the lane grant -> rebuild: the resumed step reads
    the answer from the persisted wake event, not from RAM."""
    path = tmp_path / "tasks.db"
    store_a = TaskStore(path, window=8, clock=clock, network_fs=False).open()
    adm, _ = _admission(store_a, clock, cap=1)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:ask")
    h = await adm.admit(rec.id)
    h.running()
    wait = asyncio.create_task(adm.waiting_input(h, tool_call_id="q1", reason="password?"))
    await settle_store_writes(store_a)
    assert store_a.state_of(rec.id) == m.WAITING_INPUT
    assert adm.answer_input(rec.id, "hunter2") is True
    # -- crash before the coroutine is re-admitted: RAM is gone.
    wait.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait
    assert store_a.state_of(rec.id) == m.RETRY_WAIT  # claimable, no lease
    store_a.close()
    store_b = TaskStore(path, window=8, clock=clock, network_fs=False).open()
    from kiro_crew.taskq.reconcile import reconcile_on_boot

    reconcile_on_boot(store_b, now=clock())
    assert store_b.state_of(rec.id) == m.RETRY_WAIT  # boot leaves a claimable row alone
    adm_b, _ = _admission(store_b, clock, cap=1)
    assert adm_b.recorded_answer(rec.id) == "hunter2"
    h_b = await adm_b.admit(rec.id)
    h_b.running()
    # The resumed step takes the answer once; a later rebuild does not replay it.
    assert adm_b.recorded_answer(rec.id) == "hunter2"
    assert adm_b.consume_answer(rec.id, "q1") is True
    assert adm_b.recorded_answer(rec.id) is None
    store_b.close()


@pytest.mark.asyncio
async def test_waiting_input_falls_back_to_the_persisted_answer(
    store: TaskStore, clock: Clock
) -> None:
    """A wake that arrives without this process's RAM copy of the answer (the
    ledger woke the row on another instance's behalf) still returns it."""
    adm, _ = _admission(store, clock, cap=1)
    rec = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:ask2")
    h = await adm.admit(rec.id)
    h.running()
    wait = asyncio.create_task(adm.waiting_input(h, tool_call_id="q2"))
    await settle_store_writes(store)
    from kiro_crew.taskq.waits import WaitLedger

    # Another instance records the answer on the wake event and wakes the row.
    assert WaitLedger(store, clock=clock).wake(
        rec.id, reason="input answered", detail={"answer": "yes"}
    )
    adm.on_wake(rec.id)
    assert await asyncio.wait_for(wait, 2) == "yes"


@pytest.mark.asyncio
async def test_terminal_decision_is_persisted_before_capacity_is_released(
    tmp_path: Path, clock: Clock, monkeypatch
) -> None:
    """``Admitted.settle`` writes the terminal state FIRST and releases the lane
    slot only afterwards; a crash between the committed write and the local
    ``_settled`` bookkeeping leaves a TERMINAL row, which neither the boot
    reconciler nor the runner adapter touches -- a cancelled retry-safe row is
    never re-run. Only an unreachable store (``TaskStoreUnavailable``) leaves
    the decision to reconcile."""
    from kiro_crew.taskq.reconcile import reconcile_on_boot

    path = tmp_path / "tasks.db"
    store_a = TaskStore(path, window=8, clock=clock, network_fs=False).open()
    adm, _ = _admission(store_a, clock, cap=1)
    rec = adm.accept(
        kind=m.KIND_TASKRUNNER_STEP,
        task_id="taskrunner:r:safe-cancel",
        side_effect_class=m.SIDE_EFFECT_NONE,
    )
    h = await adm.admit(rec.id)
    h.running()
    order: list[str] = []
    real_finish = store_a.finish

    def _finish(*a, **kw):
        order.append("finish")
        return real_finish(*a, **kw)

    def _release():
        order.append("release_slot")
        # -- crash here: the write is committed, the local bookkeeping is not.
        raise SystemExit("simulated crash after the terminal write")

    monkeypatch.setattr(store_a, "finish", _finish)
    monkeypatch.setattr(h, "release_slot", _release)
    with pytest.raises(SystemExit):
        h.cancel("operator stop")
    assert order == ["finish", "release_slot"], "the terminal write precedes the slot release"
    assert store_a.state_of(rec.id) == m.CANCELLED
    store_a.close()
    store_b = TaskStore(path, window=8, clock=clock, network_fs=False).open()
    report = reconcile_on_boot(store_b, now=clock())
    assert report.examined == 0  # a terminal row is not an active row
    resumed: list[str] = []
    adopted = r.adopt_orphaned_rows(store_b, resume=lambda rec: resumed.append(rec.id) or True)
    assert adopted.examined == 0 and resumed == []
    assert store_b.state_of(rec.id) == m.CANCELLED
    store_b.close()
