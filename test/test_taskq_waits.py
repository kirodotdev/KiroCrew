"""Waits with a reason (addendum §1-3, RFC §14.1-14.2): record, store, ledger.

For every wait kind: the record's fields, that the lane slot is the quota
released while the residency charge stays, what wakes it, what cancelling
it means, and that a deadline ends it in a terminal state. Fake clock, real
store on a temp file, no manager and no sockets.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from overload_fakes import Clock, ManagerHarness, open_task_store

from kiro_crew.subagent import SubagentInfo
from kiro_crew.taskq import model, waits
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable
from kiro_crew.taskq.waits import (
    CANCEL_CALL,
    CANCEL_TASK,
    CANCEL_TREE,
    EVIDENCE_DEPENDENCY_ADAPTER,
    EVIDENCE_EXECUTION_LAYER,
    EVIDENCE_MODEL_TEXT,
    RESUME_AT_TIME,
    RESUME_CHILDREN,
    RESUME_INPUT,
    RESUME_PERMISSION,
    RESUME_SIGNAL,
    ResumeCondition,
    WaitLedger,
    WaitRecord,
)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock):
    yield from open_task_store(tmp_path, clock)


@pytest.fixture
def ledger(store: TaskStore) -> WaitLedger:
    return WaitLedger(store)


def _running(store: TaskStore, task_id: str, *, parent: str | None = None, **params) -> int:
    """Accept, claim and mark ``running``; returns the generation."""
    rec = model.TaskRecord(
        id=task_id, kind=model.KIND_SUBAGENT, parent_id=parent, params={"task": task_id, **params}
    )
    store.accept_one(rec)
    claimed = store.claim(task_id)
    assert claimed is not None
    assert store.transition(task_id, model.STARTING, generation=claimed.generation)
    assert store.transition(task_id, model.RUNNING, generation=claimed.generation)
    return claimed.generation


# ── model ─────────────────────────────────────────────────────────────────────


def test_state_machine_extension() -> None:
    assert model.WAITING == {
        model.WAITING_CHILDREN,
        model.WAITING_PERMISSION,
        model.WAITING_DEPENDENCY,
        model.WAITING_INPUT,
    }
    assert model.WAITING <= model.ACTIVE
    assert not (model.WAITING & model.CLAIMABLE), "a live wait is owned, never re-claimed"
    for state in model.WAITING:
        assert state in model.TRANSITIONS[model.RUNNING]
        assert model.RUNNING in model.TRANSITIONS[state]
        assert model.RETRY_WAIT in model.TRANSITIONS[state], "park after runtime reclaim"
        assert model.CANCELLED in model.TRANSITIONS[state]
        assert model.FAILED in model.TRANSITIONS[state]
        # One reason per record: waits do not hop kinds directly.
        assert not (model.TRANSITIONS[state] & model.WAITING)
    assert model.SUCCEEDED == model.DONE


# ── record ────────────────────────────────────────────────────────────────────


def test_children_record_fields_and_round_trip() -> None:
    rec = WaitRecord.children(["b1", "b2"], since=5.0, tool_call_id="call-7", deadline_at=99.0)
    assert rec.state == model.WAITING_CHILDREN
    assert rec.resume_condition.kind == RESUME_CHILDREN
    assert rec.resume_condition.ids == ["b1", "b2"]
    assert rec.cancel_semantics == CANCEL_TREE
    assert rec.evidence_source == EVIDENCE_EXECUTION_LAYER
    assert rec.slot_released is True and rec.residency_charged is True
    again = WaitRecord.from_dict(rec.to_dict())
    assert again == rec
    assert again.remaining_children({"b1"}) == ["b2"]
    assert again.deadline_passed(99.0) and not again.deadline_passed(98.9)


def test_dependency_record_at_time_and_signal() -> None:
    timed = WaitRecord.dependency("github:api:core", since=1.0, retry_at=61.0)
    assert timed.state == model.WAITING_DEPENDENCY
    assert timed.resume_condition.kind == RESUME_AT_TIME
    assert timed.resume_condition.at == 61.0
    assert timed.dependency_scope == "github:api:core"
    assert timed.cancel_semantics == CANCEL_TASK
    assert timed.evidence_source == EVIDENCE_DEPENDENCY_ADAPTER
    assert timed.due(61.0) and not timed.due(60.0)
    sig = WaitRecord.dependency("db:primary", since=1.0)
    assert sig.resume_condition.kind == RESUME_SIGNAL
    assert sig.resume_condition.key == "db:primary"
    assert not sig.due(10**9)


def test_input_and_permission_records() -> None:
    inp = WaitRecord.input("call-1", since=1.0)
    assert inp.state == model.WAITING_INPUT
    assert inp.resume_condition.kind == RESUME_INPUT
    assert inp.resume_condition.key == "call-1"
    assert inp.cancel_semantics == CANCEL_CALL
    perm = WaitRecord.permission("approval-9", since=1.0, tool_call_id="call-2")
    assert perm.state == model.WAITING_PERMISSION
    assert perm.resume_condition.kind == RESUME_PERMISSION
    assert perm.resume_condition.key == "approval-9"
    assert perm.tool_call_id == "call-2"


def test_record_refuses_model_text_alone_and_kind_mismatch() -> None:
    with pytest.raises(ValueError, match="model text"):
        WaitRecord(
            state=model.WAITING_INPUT,
            reason="model said so",
            since=1.0,
            resume_condition=ResumeCondition(RESUME_INPUT, key="x"),
            evidence_source=EVIDENCE_MODEL_TEXT,
        )
    with pytest.raises(ValueError, match="does not fit"):
        WaitRecord(
            state=model.WAITING_CHILDREN,
            reason="",
            since=1.0,
            resume_condition=ResumeCondition(RESUME_AT_TIME, at=1.0),
        )
    with pytest.raises(ValueError, match="at least one"):
        WaitRecord.children([], since=1.0)
    with pytest.raises(ValueError, match="residency"):
        WaitRecord(
            state=model.WAITING_INPUT,
            reason="",
            since=1.0,
            resume_condition=ResumeCondition(RESUME_INPUT, key="x"),
            residency_charged=False,
        )
    assert WaitRecord.from_dict(None) is None
    assert WaitRecord.from_dict({"state": "bogus"}) is None


def test_on_child_failure_policy_defaults_to_continue() -> None:
    assert waits.on_child_failure_policy(None) == waits.ON_CHILD_FAILURE_CONTINUE
    assert waits.on_child_failure_policy({}) == waits.ON_CHILD_FAILURE_CONTINUE
    assert waits.on_child_failure_policy({"on_child_failure": "nope"}) == "continue"
    assert (
        waits.on_child_failure_policy({"on_child_failure": "fail_parent"})
        == waits.ON_CHILD_FAILURE_FAIL_PARENT
    )


# ── store + ledger: each wait kind ────────────────────────────────────────────


@pytest.mark.parametrize(
    "make",
    [
        lambda now: WaitRecord.children(["c1"], since=now),
        lambda now: WaitRecord.dependency("github", since=now, retry_at=now + 30),
        lambda now: WaitRecord.input("call-1", since=now),
        lambda now: WaitRecord.permission("appr-1", since=now),
    ],
    ids=["children", "dependency", "input", "permission"],
)
def test_enter_records_row_and_event_without_bumping_generation(store, ledger, clock, make) -> None:
    gen = _running(store, "t1")
    if make(clock.t).state == model.WAITING_CHILDREN:
        store.accept_one(model.TaskRecord(id="c1", kind=model.KIND_SUBAGENT, parent_id="t1"))
    rec = make(clock.t)
    assert ledger.enter("t1", rec, generation=gen)
    row = store.get("t1")
    assert row.state == rec.state
    assert row.generation == gen, "entering a wait keeps the run's generation"
    assert row.lease_owner is not None, "the row stays owned; nothing re-claims a live wait"
    stored = WaitRecord.from_dict(row.wait)
    assert stored == rec
    assert stored.slot_released is True and stored.residency_charged is True
    events = [e for e in store.events("t1") if e.kind == "transition" and e.data.get("wait")]
    assert len(events) == 1
    assert events[0].data["resume"] == rec.resume_condition.kind
    assert events[0].data["residency_charged"] is True
    # Not claimable while waiting: the dispatcher never re-dispatches a live wait.
    assert store.claim("t1") is None
    assert "t1" not in {r.id for r in store.fetch_dispatchable(model.KIND_SUBAGENT, limit=10)}


def test_enter_is_fenced_by_generation_and_refused_from_non_running(store, ledger, clock) -> None:
    gen = _running(store, "t1")
    rec = WaitRecord.input("c", since=clock.t)
    assert not ledger.enter("t1", rec, generation=gen + 1)
    assert store.get("t1").state == model.RUNNING
    assert ledger.enter("t1", rec, generation=gen)
    # A second wait from a wait is refused: one reason per record.
    assert not ledger.enter("t1", WaitRecord.permission("p", since=clock.t), generation=gen)
    assert store.get("t1").state == model.WAITING_INPUT


def test_wake_returns_to_running_under_new_generation(store, ledger, clock) -> None:
    gen = _running(store, "t1")
    assert ledger.enter("t1", WaitRecord.input("c", since=clock.t), generation=gen)
    clock.t += 5
    # ``resident``: the caller holds the slot and the runtime is resident
    # (admission's resume_grant) -- the one wake that goes straight to running.
    new_gen = ledger.wake("t1", reason="user answered", generation=gen, resident=True)
    assert new_gen == gen + 1
    row = store.get("t1")
    assert row.state == model.RUNNING
    assert row.wait is None, "a woken row carries no stale reason"
    assert row.lease_expires_at == clock.t + store.lease_secs
    wake_events = [e for e in store.events("t1") if e.kind == "wake"]
    assert len(wake_events) == 1 and wake_events[0].data["generation"] == new_gen
    # Old-generation callbacks from the wait period are fenced out.
    assert not store.transition("t1", model.DONE, generation=gen)
    assert store.transition("t1", model.DONE, generation=new_gen)


def test_wake_is_refused_for_stale_generation_and_non_waiting(store, ledger, clock) -> None:
    gen = _running(store, "t1")
    assert ledger.wake("t1", reason="x") is None  # running, not waiting
    assert ledger.enter("t1", WaitRecord.input("c", since=clock.t), generation=gen)
    assert ledger.wake("t1", reason="x", generation=gen + 5) is None
    assert store.get("t1").state == model.WAITING_INPUT
    assert ledger.wake("missing", reason="x") is None


def test_park_ends_residency_into_retry_wait(store, ledger, clock) -> None:
    gen = _running(store, "t1")
    assert ledger.enter(
        "t1", WaitRecord.dependency("github", since=clock.t, retry_at=clock.t + 900), generation=gen
    )
    assert ledger.park(
        "t1", next_run_at=clock.t + 900, reason="idle runtime reclaimed", generation=gen
    )
    row = store.get("t1")
    assert row.state == model.RETRY_WAIT
    assert row.wait is None
    assert row.lease_owner is None
    assert row.next_run_at == clock.t + 900
    assert store.claim("t1") is None
    clock.t += 900
    assert store.claim("t1") is not None, "re-dispatched through claim, new generation"


def test_park_is_refused_under_a_stale_generation_and_commits_under_the_live_one(
    store, ledger, clock
) -> None:
    """``park`` is the write that ends the residency charge, so it is fenced.

    A caller the row has already moved past reclaimed an OLDER run's runtime:
    letting its park land would clear a live wait and hand the row to the
    dispatcher while the runtime the wait describes is still resident. The
    COMMIT under the satisfied fence is asserted in the same test, because a
    fence narrowed to something the live caller can never satisfy refuses
    every park and the refusal half alone reads that as a pass.
    """
    gen = _running(store, "t1")
    rec = WaitRecord.dependency("github", since=clock.t, retry_at=clock.t + 900)
    assert ledger.enter("t1", rec, generation=gen)
    assert not ledger.park(
        "t1", next_run_at=clock.t + 900, reason="idle runtime reclaimed", generation=gen + 1
    )
    row = store.get("t1")
    assert row.state == model.WAITING_DEPENDENCY, "a stale park never ends the wait"
    assert WaitRecord.from_dict(row.wait) == rec, "the reason and its residency charge stay"
    assert row.generation == gen and row.lease_owner is not None
    stale = [e for e in store.events("t1") if e.kind == "stale_result"]
    assert len(stale) == 1
    assert stale[0].data["from_generation"] == gen + 1 and stale[0].data["current"] == gen
    assert ledger.park(
        "t1", next_run_at=clock.t + 900, reason="idle runtime reclaimed", generation=gen
    )
    row = store.get("t1")
    assert row.state == model.RETRY_WAIT and row.wait is None
    assert row.next_run_at == clock.t + 900 and row.lease_owner is None
    released = [
        e
        for e in store.events("t1")
        if e.kind == "transition" and e.data.get("residency_released") is True
    ]
    assert len(released) == 1 and released[0].data["to"] == model.RETRY_WAIT


def test_fail_is_refused_under_a_stale_generation_and_commits_under_the_live_one(
    store, ledger, clock
) -> None:
    """A wait's terminal write carries the same generation fence as the others.

    The deadline sweep and the ``fail_parent`` policy both end a row through
    ``fail``; a caller from a generation the row has left would fail a run that
    was already woken and re-admitted under a newer one. Both halves again: the
    refusal, and the commit once the fence is satisfied -- which is what tells a
    narrowed or wrongly-keyed fence apart from a working one.
    """
    gen = _running(store, "t1")
    rec = WaitRecord.input("call-1", since=clock.t, deadline_at=clock.t + 60)
    assert ledger.enter("t1", rec, generation=gen)
    assert not ledger.fail(
        "t1", reason=waits.WAIT_REASON_DEADLINE, error="deadline passed", generation=gen + 1
    )
    row = store.get("t1")
    assert row.state == model.WAITING_INPUT and not row.terminal
    assert WaitRecord.from_dict(row.wait) == rec
    stale = [e for e in store.events("t1") if e.kind == "stale_result"]
    assert len(stale) == 1 and stale[0].data["wanted"] == model.FAILED
    assert ledger.fail(
        "t1", reason=waits.WAIT_REASON_DEADLINE, error="deadline passed", generation=gen
    )
    row = store.get("t1")
    assert row.state == model.FAILED and row.terminal and row.wait is None
    ended = [
        e for e in store.events("t1") if e.kind == "transition" and e.data.get("to") == model.FAILED
    ]
    assert len(ended) == 1 and ended[0].data["reason"] == waits.WAIT_REASON_DEADLINE


def test_cancel_semantics_per_kind(store, ledger, clock) -> None:
    # cancel_tree: children first, then the parent; completed siblings stay done.
    _running(store, "p")
    store.accept_one(model.TaskRecord(id="c1", kind=model.KIND_SUBAGENT, parent_id="p"))
    store.accept_one(model.TaskRecord(id="c2", kind=model.KIND_SUBAGENT, parent_id="p"))
    store.claim("c2")
    assert store.transition("c2", model.STARTING)
    assert store.transition("c2", model.DONE)
    cancelled = ledger.cancel_tree("p", reason="user_stop")
    assert cancelled == ["c1", "p"]
    assert store.get("c1").state == model.CANCELLED
    assert store.get("c2").state == model.DONE
    assert store.get("p").state == model.CANCELLED
    # cancel_task on a dependency wait: only that row.
    gen = _running(store, "d")
    assert ledger.enter("d", WaitRecord.dependency("gh", since=clock.t), generation=gen)
    assert ledger.record_of("d").cancel_semantics == CANCEL_TASK
    assert store.cancel("d", reason="user") == model.WAITING_DEPENDENCY
    assert store.get("d").wait is None
    # cancel_call on an input wait: the record names the call; the task survives.
    gen = _running(store, "i")
    rec = WaitRecord.input("call-9", since=clock.t)
    assert ledger.enter("i", rec, generation=gen)
    assert ledger.record_of("i").cancel_semantics == CANCEL_CALL
    assert ledger.record_of("i").tool_call_id == "call-9"
    assert ledger.wake("i", reason="call cancelled; task continues", generation=gen) == gen + 1


def test_deadline_ends_the_wait_failed_with_reason(store, ledger, clock) -> None:
    gen = _running(store, "t1")
    _running(store, "t2")
    assert ledger.enter(
        "t1", WaitRecord.input("c", since=clock.t, deadline_at=clock.t + 60), generation=gen
    )
    assert ledger.enter(
        "t2", WaitRecord.input("c", since=clock.t), generation=store.get("t2").generation
    )
    assert ledger.expire() == []
    clock.t += 61
    assert ledger.expire() == ["t1"]
    row = store.get("t1")
    assert row.state == model.FAILED and row.terminal
    last = store.events("t1")[-1]
    assert last.data["reason"] == waits.WAIT_REASON_DEADLINE
    assert "deadline passed after 61s" in last.data["error"]
    assert store.get("t2").state == model.WAITING_INPUT, "no deadline, no expiry"
    assert ledger.expire() == [], "idempotent"


def test_dependency_due_and_signal_wake_by_scope(store, ledger, clock) -> None:
    for tid, scope in (("a", "github"), ("b", "github"), ("c", "db")):
        gen = _running(store, tid)
        assert ledger.enter(
            tid, WaitRecord.dependency(scope, since=clock.t, retry_at=clock.t + 30), generation=gen
        )
    assert ledger.due_dependency_waits() == []
    clock.t += 30
    assert [r.id for r in ledger.due_dependency_waits()] == ["a", "b", "c"]
    woken = ledger.signal("github", reason="429 window passed")
    assert woken == ["a", "b"]
    assert store.get("a").state == model.RETRY_WAIT  # claimable: no slot was granted by a signal
    assert store.get("c").state == model.WAITING_DEPENDENCY, "scope isolation"


def test_waiting_rows_and_terminal_from_wait_by_artifacts(store, ledger, clock) -> None:
    gen = _running(store, "t1")
    assert ledger.enter("t1", WaitRecord.input("c", since=clock.t), generation=gen)
    assert [r.id for r in store.waiting_rows()] == ["t1"]
    assert [r.id for r in store.waiting_rows(state=model.WAITING_INPUT)] == ["t1"]
    assert store.waiting_rows(state=model.WAITING_CHILDREN) == []
    assert store.waiting_rows(state="running") == []
    # A wait whose runtime finished before the wake write landed is settled
    # by what the artifacts prove: done is reachable from a wait.
    assert store.transition("t1", model.DONE, generation=gen)
    assert store.get("t1").wait is None


def test_wake_without_a_granted_slot_is_claimable_not_running(store, ledger, clock) -> None:
    """A wake that does not hold the lane slot must not
    publish ``running`` -- a crash before re-admission would reconcile that row
    (dead owner, class unknown) to ``unknown_side_effect`` and lose the wake.
    The default wake lands in ``retry_wait``: claimable NOW, no lease, new
    generation; ``running`` is written by the claim that actually gets a slot."""
    gen = _running(store, "t1")
    assert ledger.enter("t1", WaitRecord.input("c", since=clock.t), generation=gen)
    clock.t += 5
    new_gen = ledger.wake("t1", reason="user answered", generation=gen, detail={"answer": "y"})
    assert new_gen == gen + 1
    row = store.get("t1")
    assert row.state == model.RETRY_WAIT and row.state in model.CLAIMABLE
    assert row.state not in model.ACTIVE  # a boot reconcile leaves it alone
    assert row.wait is None and row.lease_owner is None
    assert row.next_run_at == clock.t
    wake_events = [e for e in store.events("t1") if e.kind == "wake"]
    assert wake_events[-1].data["to"] == model.RETRY_WAIT
    assert wake_events[-1].data["answer"] == "y"  # the answer survives a crash
    # The dispatcher claims it and only THEN is it running, one more generation on.
    claim = store.claim("t1", owner="w")
    assert claim is not None and claim.generation == new_gen + 1
    assert store.transition("t1", model.STARTING, generation=claim.generation)
    assert store.transition("t1", model.RUNNING, generation=claim.generation)
    # Old-generation callbacks from the wait period are fenced out.
    assert not store.transition("t1", model.DONE, generation=gen)
    with pytest.raises(ValueError):
        store.wake_wait("t1", reason="x", to=model.DONE)


def test_a_crash_after_a_slotless_wake_reconciles_to_redispatch(store, ledger, clock) -> None:
    from kiro_crew.taskq.reconcile import reconcile_on_boot

    gen = _running(store, "t1")
    assert ledger.enter("t1", WaitRecord.input("c", since=clock.t), generation=gen)
    assert ledger.wake("t1", reason="answered", generation=gen) == gen + 1
    # A fresh incarnation reconciles: the claimable row is neither settled nor
    # declared unknown_side_effect -- it is still the dispatcher's to claim.
    report = reconcile_on_boot(store)
    assert report.unknown_side_effect == 0
    assert store.state_of("t1") == model.RETRY_WAIT
    assert store.claim("t1", owner="next") is not None


def test_live_parent_wake_is_deferred_until_admission_grants_the_slot(store, ledger, clock) -> None:
    """Subagent path: with a LIVE resident parent the ledger
    leaves the row ``waiting_children``; admission's ``resume_grant`` writes
    ``running`` (``wake(resident=True)``) once the pump handed the slot back."""
    pgen = _running(store, "p")
    store.accept_one(model.TaskRecord(id="c1", kind=model.KIND_SUBAGENT, parent_id="p", params={}))
    assert ledger.enter("p", WaitRecord.children(["c1"], since=clock.t), generation=pgen)
    claim = store.claim("c1", owner="w")
    store.transition("c1", model.STARTING, generation=claim.generation)
    store.transition("c1", model.RUNNING, generation=claim.generation)
    store.transition("c1", model.DONE, generation=claim.generation)
    outcome = ledger.on_child_terminal("c1", model.DONE, defer_wake=True)
    assert outcome.wake_parent is True
    assert store.state_of("p") == model.WAITING_CHILDREN  # not running yet
    assert [e.kind for e in store.events("p")][-1] == "children_settled"
    # The grant: slot held, runtime resident -> running under a new generation.
    assert ledger.wake("p", reason="resumed through admission", generation=pgen, resident=True) == (
        pgen + 1
    )
    assert store.state_of("p") == model.RUNNING


# ── the grant: publish only what the store already agreed to (RFC §14.2) ──────


def _park_live_run(harness, task_id: str) -> tuple[SubagentInfo, int, dict]:
    """A live, resident run whose row is parked in ``waiting_input``.

    Returns ``(info, generation, wait_record)``. The info is registered with the
    manager exactly as a run that yielded its lane slot would be: slot released,
    a wait record in memory, a one-shot resume event armed.
    """
    store = harness.store
    gen = _running(store, task_id)
    record = WaitRecord.input("call-1", since=store.now())
    assert store.enter_wait(task_id, record.to_dict(), generation=gen)
    info = SubagentInfo(id=task_id, task="t")
    info._taskq_generation = gen
    info._slot_released = True
    info._resume_pending = True
    info._wait_record = record.to_dict()
    info._resume_event = asyncio.Event()
    harness.mgr._agents[task_id] = info
    return info, gen, record.to_dict()


@pytest.mark.asyncio
async def test_resume_grant_keeps_the_run_parked_when_the_durable_wake_fails(monkeypatch) -> None:
    """A grant is reported only when the durable wake LANDED.

    Publishing the slot, the generation and the resume signal ahead of the
    ``wake_wait`` result leaves the row ``waiting_*`` with its deadline while
    the run believes it holds a slot; the ledger's expiry sweep then fails that
    stale wait and cancels a live, healthy, resumed run.
    """
    harness = ManagerHarness(max_concurrent=2)
    try:
        await harness.mgr.wait_taskq_ready()
        store = harness.store
        admission = harness.mgr._admission

        # (a) the store cannot be reached: retryable, so the run stays parked.
        info, gen, record = _park_live_run(harness, "unreachable")
        before = harness.mgr._running_count
        monkeypatch.setattr(
            store, "wake_wait", lambda *a, **k: (_ for _ in ()).throw(TaskStoreUnavailable("down"))
        )
        assert admission.resume_grant({"_resume_id": info.id}) is False
        assert info._slot_released is True and info._wait_record == record
        assert info._taskq_generation == gen and info._resume_pending is False
        assert not info._resume_event.is_set()
        assert harness.mgr._running_count == before
        assert store.state_of(info.id) == model.WAITING_INPUT
        monkeypatch.undo()

        # (b) the wake is refused and the row is NOT running: the row moved out
        # from under this run, so the grant is refused too.
        info, gen, record = _park_live_run(harness, "parked")
        before = harness.mgr._running_count
        assert WaitLedger(store).park(
            info.id, reason="stale", generation=gen, next_run_at=store.now() + 99
        )
        assert admission.resume_grant({"_resume_id": info.id}) is False
        assert info._slot_released is True and info._wait_record == record
        assert info._resume_pending is False and not info._resume_event.is_set()
        assert harness.mgr._running_count == before

        # (c) the wake is refused because the row is ALREADY running (its wait
        # write was refused, or is still on the writer thread): granting is
        # what a resume wants, so it goes through.
        info, gen, record = _park_live_run(harness, "already-running")
        before = harness.mgr._running_count
        assert (
            store.wake_wait(info.id, reason="woken elsewhere", generation=gen, to=model.RUNNING)
            is not None
        )
        info._taskq_generation = store.get(info.id).generation
        assert store.state_of(info.id) == model.RUNNING
        monkeypatch.setattr(store, "wake_wait", lambda *a, **k: None)
        assert admission.resume_grant({"_resume_id": info.id}) is True
        assert info._slot_released is False and info._wait_record is None
        assert info._resume_event is None
        assert harness.mgr._running_count == before + 1
        monkeypatch.undo()

        # (d) a STALE generation: another dispatcher woke and re-claimed the row
        # while this resume sat in the window, so the wake is fenced and the
        # grant is refused rather than handing back a slot under a generation
        # every later write would be fenced by.
        info, gen, record = _park_live_run(harness, "stale-generation")
        before = harness.mgr._running_count
        assert WaitLedger(store).wake(info.id, reason="woken elsewhere", generation=gen) == gen + 1
        claim = store.claim(info.id, owner="another-incarnation")
        assert claim is not None and claim.generation > gen
        assert store.transition(info.id, model.STARTING, generation=claim.generation)
        assert admission.resume_grant({"_resume_id": info.id}) is False
        assert info._slot_released is True and info._wait_record == record
        assert info._taskq_generation == gen and not info._resume_event.is_set()
        assert harness.mgr._running_count == before
        assert store.state_of(info.id) == model.STARTING

        # (e) cancelled while its resume entry sat in the window: nothing is
        # reserved, so no slot is left spent on a run that will not resume.
        info, gen, record = _park_live_run(harness, "cancelled")
        before = harness.mgr._running_count
        info.user_stopped = True
        assert admission.resume_grant({"_resume_id": info.id}) is False
        assert harness.mgr._running_count == before
        assert info._resume_pending is False and not info._resume_event.is_set()
    finally:
        harness.mgr._agents.clear()
        await harness.mgr.cancel_all()
        harness.close()


@pytest.mark.asyncio
async def test_resume_grant_publishes_the_bumped_generation_before_the_signal() -> None:
    """The happy path's ordering: the generation the store minted is on the info
    BEFORE the waiter's event is set, so a run that wakes on that event cannot
    write under the generation its wait was fenced by."""
    harness = ManagerHarness(max_concurrent=2)
    try:
        await harness.mgr.wait_taskq_ready()
        info, gen, _record = _park_live_run(harness, "granted")
        seen: dict[str, object] = {}
        event = info._resume_event
        original_set = event.set

        def _set() -> None:
            seen["generation_at_set"] = info._taskq_generation
            seen["slot_released_at_set"] = info._slot_released
            seen["state_at_set"] = harness.store.state_of(info.id)
            original_set()

        event.set = _set  # type: ignore[method-assign]
        assert harness.mgr._admission.resume_grant({"_resume_id": info.id}) is True
        assert seen["generation_at_set"] == gen + 1
        assert seen["slot_released_at_set"] is False
        assert seen["state_at_set"] == model.RUNNING
        assert info._taskq_generation == gen + 1
    finally:
        harness.mgr._agents.clear()
        await harness.mgr.cancel_all()
        harness.close()
