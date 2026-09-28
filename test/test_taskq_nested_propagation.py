"""Nested waits S -> A -> B through the real SubagentManager admission (RFC §14.3).

The manager, its admission pump and the durable store are real; the RUN is a
fake worker the test finishes on demand, and a parent's "blocked in
spawn_sub_agents" is the execution-layer fact the manager reads: its in-flight
tool snapshot names ``spawn_sub_agents`` (the trusted ``_meta.kiro`` tool name).
No kiro-cli, no sockets; the store clock is injected where time matters.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from overload_fakes import ManagerHarness, mock_ctx, mock_sessions

from kiro_crew.subagent import SubagentManager
from kiro_crew.taskq import model, waits
from kiro_crew.taskq.store import TaskStore
from kiro_crew.taskq.waits import WaitRecord

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.fixture
def quiet():
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        yield


@pytest.fixture
def h(quiet):
    harness: ManagerHarness | None = None

    async def make(max_concurrent: int = 2) -> ManagerHarness:
        nonlocal harness
        harness = ManagerHarness(max_concurrent)
        await harness.mgr.wait_taskq_ready()
        return harness

    yield make
    if harness is not None:
        harness.close()


# ── scenarios ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_three_level_tree_leaf_waits_and_unrelated_work_completes(h) -> None:
    hz = await h(max_concurrent=2)
    s = hz.spawn("S")
    await hz.settle()
    assert hz.state(s) == model.RUNNING and hz.mgr._running_count == 1

    hz.block_in_spawn_sub_agents(s, "call-s")
    a = hz.child_of(s, "A")
    await hz.settle()
    # S yielded its lane slot; its runtime stays resident (record says so).
    assert hz.state(s) == model.WAITING_CHILDREN
    rec_s = hz.ledger.record_of(s.id)
    assert rec_s.resume_condition.ids == [a.id]
    assert rec_s.tool_call_id == "call-s"
    assert rec_s.slot_released and rec_s.residency_charged
    assert hz.live(s)._slot_released is True
    assert hz.state(a) == model.RUNNING
    assert hz.mgr._running_count == 1
    assert hz.store.get(a.id).parent_id == s.id
    assert hz.store.get(a.id).root_id == s.id

    hz.block_in_spawn_sub_agents(a, "call-a")
    b = hz.child_of(a, "B")
    await hz.settle()
    assert hz.state(a) == model.WAITING_CHILDREN
    assert hz.state(b) == model.RUNNING
    assert hz.store.get(b.id).root_id == s.id
    assert hz.mgr._running_count == 1, "the chain holds one slot: the leaf's"

    # B hits a rate limit: dependency wait, slot released, residency kept.
    assert hz.mgr._admission.yield_slot(
        hz.live(b), WaitRecord.dependency("github:core", since=hz.store.now(), retry_at=1e12)
    )
    assert hz.state(b) == model.WAITING_DEPENDENCY
    assert hz.mgr._running_count == 0, "the whole chain holds zero execution slots"

    # Unrelated work is admitted and completes while all three levels wait.
    u1 = hz.spawn("U1", parent="dash:other")
    u2 = hz.spawn("U2", parent="dash:other")
    await hz.settle()
    assert hz.state(u1) == model.RUNNING and hz.state(u2) == model.RUNNING
    assert hz.mgr._running_count == 2
    await hz.end(u1)
    await hz.end(u2)
    assert hz.state(u1) == model.DONE and hz.state(u2) == model.DONE
    assert {hz.state(s), hz.state(a), hz.state(b)} == {
        model.WAITING_CHILDREN,
        model.WAITING_DEPENDENCY,
    }


@pytest.mark.asyncio
async def test_all_slots_held_by_waiting_parents_still_progresses(h) -> None:
    """One slot. S->A->B: every parent yields, the leaf runs, and completion
    walks back up one re-admission at a time."""
    hz = await h(max_concurrent=1)
    s = hz.spawn("S")
    await hz.settle()
    hz.block_in_spawn_sub_agents(s)
    a = hz.child_of(s, "A")
    await hz.settle()
    assert hz.state(s) == model.WAITING_CHILDREN and hz.state(a) == model.RUNNING
    hz.block_in_spawn_sub_agents(a)
    b = hz.child_of(a, "B")
    await hz.settle()
    assert hz.state(a) == model.WAITING_CHILDREN and hz.state(b) == model.RUNNING
    assert hz.mgr._running_count == 1

    gen_a = hz.store.get(a.id).generation
    await hz.end(b)
    assert hz.state(b) == model.DONE
    # A woke (last child) and was RE-ADMITTED through the pump: new generation.
    assert hz.state(a) == model.RUNNING
    assert hz.store.get(a.id).generation == gen_a + 1
    assert hz.live(a)._taskq_generation == gen_a + 1
    assert hz.live(a)._slot_released is False and not hz.live(a)._resume_pending
    assert hz.mgr._running_count == 1
    assert [e.kind for e in hz.store.events(a.id)][-2:] == ["wake", "transition"] or any(
        e.kind == "wake" for e in hz.store.events(a.id)
    )
    assert hz.state(s) == model.WAITING_CHILDREN, "S waits for A, not for B"

    await hz.end(a)
    assert hz.state(a) == model.DONE
    assert hz.state(s) == model.RUNNING
    assert hz.mgr._running_count == 1
    await hz.end(s)
    assert hz.state(s) == model.DONE
    assert hz.mgr._running_count == 0
    assert hz.mgr._queue == []


@pytest.mark.asyncio
async def test_parent_wakes_on_last_child_and_wakes_are_metered(h) -> None:
    hz = await h(max_concurrent=1)
    s = hz.spawn("S")
    await hz.settle()
    hz.block_in_spawn_sub_agents(s)
    a1 = hz.child_of(s, "A1")
    a2 = hz.child_of(s, "A2")
    await hz.settle()
    rec = hz.ledger.record_of(s.id)
    assert set(rec.resume_condition.ids) == {a1.id, a2.id}
    assert hz.state(a1) == model.RUNNING and hz.state(a2) == model.QUEUED

    await hz.end(a1)
    assert hz.state(a1) == model.DONE
    assert hz.state(s) == model.WAITING_CHILDREN, "one child left"
    settled = [e for e in hz.store.events(s.id) if e.kind == "child_settled"]
    assert settled and settled[-1].data["remaining"] == [a2.id]
    assert hz.state(a2) == model.RUNNING, "A2 took the slot A1 freed"

    # A second, unrelated parent-child pair whose leaf ends at the same time:
    # both parents become wake-eligible, but only one slot exists, so exactly
    # one is re-admitted and the other holds a pending resume entry.
    await hz.end(a2)
    assert hz.state(s) == model.RUNNING
    assert hz.mgr._running_count == 1
    await hz.end(s)
    assert hz.state(s) == model.DONE


@pytest.mark.asyncio
async def test_two_waking_parents_are_readmitted_one_at_a_time(h) -> None:
    hz = await h(max_concurrent=2)
    p1 = hz.spawn("P1", parent="dash:1")
    p2 = hz.spawn("P2", parent="dash:2")
    await hz.settle()
    hz.block_in_spawn_sub_agents(p1)
    hz.block_in_spawn_sub_agents(p2)
    c1 = hz.child_of(p1, "C1")
    c2 = hz.child_of(p2, "C2")
    await hz.settle()
    assert hz.state(c1) == model.RUNNING and hz.state(c2) == model.RUNNING
    assert hz.mgr._running_count == 2
    # Occupy every slot the children free with an unrelated queued run, so the
    # parents' wakes compete for capacity.
    hz.mgr._max_concurrent = 1
    await hz.end(c1)
    await hz.end(c2)
    live = [hz.live(p1), hz.live(p2)]
    granted = [i for i in live if not i._slot_released]
    pending = [i for i in live if i._resume_pending]
    assert len(granted) == 1 and len(pending) == 1, "no tree-wide burst"
    assert hz.mgr._running_count == 1
    assert hz.state(granted[0]) == model.RUNNING
    # The row says ``running`` only once the slot is
    # granted; the parent still waiting for capacity stays ``waiting_children``.
    assert hz.state(pending[0]) == model.WAITING_CHILDREN, "no running without a granted slot"
    await hz.end(granted[0])
    assert not hz.live(pending[0])._resume_pending
    assert hz.mgr._running_count == 1
    assert hz.state(pending[0]) == model.RUNNING  # granted now
    await hz.end(pending[0])
    assert hz.mgr._running_count == 0


@pytest.mark.asyncio
async def test_child_failure_continue_policy_wakes_parent_with_failed_child(h) -> None:
    hz = await h(max_concurrent=1)
    s = hz.spawn("S")
    await hz.settle()
    hz.block_in_spawn_sub_agents(s)
    a = hz.child_of(s, "A")
    await hz.settle()
    await hz.end(a, "fail")
    assert hz.state(a) == model.FAILED
    assert hz.state(s) == model.RUNNING, "default policy: the parent continues with the failed set"
    assert hz.live(s).error == ""


@pytest.mark.asyncio
async def test_child_failure_fail_parent_policy_fails_parent_and_cancels_siblings(h) -> None:
    hz = await h(max_concurrent=1)
    cancelled: list[str] = []

    async def _cancel(agent_id: str) -> bool:
        cancelled.append(agent_id)
        return True

    hz.mgr.cancel = _cancel  # type: ignore[method-assign]
    s = hz.spawn("S")
    await hz.settle()
    # The parent's policy lives in its params.
    with hz.store._lock:
        hz.store._c().execute(
            "UPDATE tasks SET params_json=json_set(params_json, '$.on_child_failure', 'fail_parent') "
            "WHERE id=?",
            (s.id,),
        )
    hz.block_in_spawn_sub_agents(s)
    a1 = hz.child_of(s, "A1")
    a2 = hz.child_of(s, "A2")
    await hz.settle()
    assert hz.state(a2) == model.QUEUED
    await hz.end(a1, "fail")
    assert hz.state(a1) == model.FAILED
    row = hz.store.get(s.id)
    assert row.state == model.FAILED
    last = [e for e in hz.store.events(s.id) if e.kind == "transition"][-1]
    assert last.data["reason"] == waits.WAIT_REASON_CHILD_FAILED and last.data["child"] == a1.id
    # The queued sibling is cancelled in the store; the live parent's cancel is scheduled.
    assert hz.state(a2) == model.CANCELLED
    await hz.settle()
    assert s.id in cancelled
    assert "fail_parent" in hz.live(s).error


@pytest.mark.asyncio
async def test_parent_cancel_cascades_to_children(h) -> None:
    hz = await h(max_concurrent=1)
    scheduled: list[str] = []

    async def _cancel(agent_id: str) -> bool:
        scheduled.append(agent_id)
        return True

    hz.mgr.cancel = _cancel  # type: ignore[method-assign]
    s = hz.spawn("S")
    await hz.settle()
    hz.block_in_spawn_sub_agents(s)
    a_live = hz.child_of(s, "A-live")
    a_queued = hz.child_of(s, "A-queued")
    await hz.settle()
    assert hz.state(a_live) == model.RUNNING and hz.state(a_queued) == model.QUEUED
    await hz.end(s, "cancel")
    assert hz.state(s) == model.CANCELLED
    assert hz.state(a_queued) == model.CANCELLED, "store-only child cancelled with the tree"
    await hz.settle()
    assert a_live.id in scheduled, "live child cancel scheduled through the manager"


@pytest.mark.asyncio
async def test_wait_deadline_fails_parent_and_stops_its_run(h) -> None:
    hz = await h(max_concurrent=1)
    scheduled: list[str] = []

    async def _cancel(agent_id: str) -> bool:
        scheduled.append(agent_id)
        return True

    hz.mgr.cancel = _cancel  # type: ignore[method-assign]
    now = [1000.0]
    hz.store._clock = lambda: now[0]
    s = hz.spawn("S")
    await hz.settle()
    with hz.store._lock:
        hz.store._c().execute("UPDATE tasks SET deadline_at=? WHERE id=?", (1060.0, s.id))
    hz.block_in_spawn_sub_agents(s)
    a = hz.child_of(s, "A")
    await hz.settle()
    assert hz.ledger.record_of(s.id).deadline_at == 1060.0
    hz.mgr._taskq_last_wait_expiry = 0.0
    assert hz.mgr._admission.taskq_expire_waits() == []
    now[0] = 1061.0
    hz.mgr._taskq_last_wait_expiry = 0.0
    assert hz.mgr._admission.taskq_expire_waits() == [s.id]
    assert hz.state(s) == model.FAILED
    await hz.settle()
    assert s.id in scheduled
    assert hz.state(a) == model.RUNNING, "the child is not failed by its parent's deadline here"


@pytest.mark.asyncio
async def test_restart_rebuilds_tree_from_the_store(h, monkeypatch) -> None:
    """Gateway restart: parent<->child links and wait records survive in the
    rows; the reconciler settles the dead owner's rows without reviving any,
    and a queued child whose parent is now terminal is cancelled as an orphan."""
    hz = await h(max_concurrent=1)
    s = hz.spawn("S")
    await hz.settle()
    hz.block_in_spawn_sub_agents(s)
    a = hz.child_of(s, "A")
    await hz.settle()
    hz.block_in_spawn_sub_agents(a)
    b = hz.child_of(a, "B")
    c = hz.child_of(a, "C")  # queued behind B
    await hz.settle()
    assert hz.state(s) == model.WAITING_CHILDREN
    assert hz.state(a) == model.WAITING_CHILDREN
    assert hz.state(b) == model.RUNNING and hz.state(c) == model.QUEUED
    old_incarnation = hz.store.incarnation
    hz.store.close()
    hz.close()

    # New incarnation over the same home.
    with patch.object(SubagentManager, "_run", new=AsyncMock()):
        mgr2 = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=1)
        await mgr2.wait_taskq_ready()
    store2: TaskStore = mgr2._taskq
    assert store2.previous_incarnation == old_incarnation
    # Links rebuilt from rows.
    assert [r.id for r in store2.children_of(s.id)] == [a.id]
    assert {r.id for r in store2.children_of(a.id)} == {b.id, c.id}
    # No revival: the dead owner's live rows are terminal-pending (side-effect
    # class unknown), the wait records are gone with the states.
    for info in (s, a, b):
        row = store2.get(info.id)
        assert row.state == model.UNKNOWN_SIDE_EFFECT, (info.task, row.state)
        assert row.wait is None
    # The queued grandchild's parent is terminal: cancelled at boot, never run.
    assert store2.state_of(c.id) == model.CANCELLED
    ev = store2.events(c.id)[-1]
    assert ev.data.get("reason") == waits.WAIT_REASON_PARENT_TERMINAL
    assert mgr2._running_count == 0


@pytest.mark.asyncio
async def test_rebuild_wakes_parent_whose_children_finished_while_it_was_waiting(h) -> None:
    """The in-process wake was lost (e.g. the pump never ran); rebuild wakes it
    from the rows alone. Idempotent on a second call."""
    hz = await h(max_concurrent=2)
    s = hz.spawn("S")
    await hz.settle()
    hz.block_in_spawn_sub_agents(s)
    a = hz.child_of(s, "A")
    await hz.settle()
    # Finish the child directly in the store, bypassing the manager's propagation.
    assert hz.store.transition(a.id, model.DONE)
    assert hz.state(s) == model.WAITING_CHILDREN
    report = hz.ledger.rebuild()
    assert report.woken == [s.id]
    # A rebuild runs at boot, when no run is live: the woken parent is a
    # claimable ``retry_wait`` row the dispatcher re-dispatches -- never a
    # ``running`` row nobody owns.
    assert hz.state(s) == model.RETRY_WAIT
    assert hz.ledger.rebuild().woken == []


@pytest.mark.asyncio
async def test_non_blocking_parent_keeps_its_slot(h) -> None:
    """A parent that used spawn_run (non-blocking) has work of its own: it
    never enters waiting_children and keeps its lane slot."""
    hz = await h(max_concurrent=2)
    s = hz.spawn("S")
    await hz.settle()
    hz.live(s)._inflight_tool = SimpleNamespace(tool_name="@kirocrew-core/spawn_run", title="t")
    a = hz.child_of(s, "A")
    await hz.settle()
    assert hz.state(s) == model.RUNNING
    assert hz.live(s)._slot_released is False
    assert hz.state(a) == model.RUNNING
    assert hz.mgr._running_count == 2
    assert hz.store.get(a.id).parent_id == s.id, "the link is recorded regardless"


@pytest.mark.asyncio
async def test_yield_and_resume_are_idempotent_and_generation_fenced(h) -> None:
    hz = await h(max_concurrent=1)
    s = hz.spawn("S")
    await hz.settle()
    live = hz.live(s)
    rec = WaitRecord.input("call-1", since=hz.store.now())
    assert hz.mgr._admission.yield_slot(live, rec) is True
    assert hz.mgr._admission.yield_slot(live, rec) is False, "already yielded"
    assert hz.mgr._running_count == 0
    assert hz.state(s) == model.WAITING_INPUT
    gen = hz.store.get(s.id).generation
    assert hz.mgr._admission.request_resume(live, reason="user typed") is True
    assert hz.mgr._admission.request_resume(live) is False, "already pending"
    await hz.settle()
    assert hz.mgr._running_count == 1
    assert hz.state(s) == model.RUNNING
    assert live._taskq_generation == gen + 1
    assert hz.mgr._admission.resume_granted(s.id) is True
    assert not hz.store.transition(s.id, model.DONE, generation=gen), "old generation fenced"
    await hz.end(s)
    assert hz.state(s) == model.DONE
    assert Path(os.environ["KIROCREW_HOME"]).exists()
