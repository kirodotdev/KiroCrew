"""Mutation pins for the load-bearing lines in ``taskq.store`` that carried no test.

Five lines in ``kiro_crew.taskq.store`` carry invariants that no other test
observes: delete any one of them and the whole suite stays green. This file pins
those five -- the reachable ones, whose silent removal loses or double-runs work.
Lines whose invariant is unreachable are deliberately left unpinned, because a
pin on an unreachable line freezes dead code instead of protecting behaviour.

``AUTOSDE.yaml``'s ``an-invariant-line-needs-a-mutation-pin`` requires each pin to
red under three mutations, not one:

1. the line DELETED,
2. the line LOOSENED to the next-weakest thing a reader might write (a predicate
   widened to the full claimable set, a generation replaced by ``None``),
3. the HEALTHY side asserted -- under a satisfied fence the write COMMITS and the
   row moves.

Every class below carries all three. Each was verified by applying the audit's own
mutation to ``store.py`` and observing this file red; the counts are in the PR
description.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from overload_fakes import Clock, open_task_store

from kiro_crew.taskq import lanes, model
from kiro_crew.taskq.model import InvalidTransition
from kiro_crew.taskq.store import TaskStore
from kiro_crew.taskq.waits import WaitRecord


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> TaskStore:
    yield from open_task_store(tmp_path, clock, name="tasks/tasks.db", window=4)


def _running(store: TaskStore, task_id: str, *, kind: str = model.KIND_SUBAGENT) -> None:
    """Accept a row and walk it to ``running`` through the real transitions."""
    store.accept([model.TaskRecord(id=task_id, kind=kind, params={"task": task_id})])
    assert store.claim(task_id) is not None
    assert store.transition(task_id, model.STARTING)
    assert store.transition(task_id, model.RUNNING)


def _row(store: TaskStore, task_id: str, session: str, *, parent: str | None = None) -> None:
    store.accept_one(
        model.TaskRecord(
            id=task_id,
            kind=model.KIND_SUBAGENT,
            params={"task": task_id},
            session_key=session,
            parent_id=parent,
        )
    )
    store._clock.t += 1  # type: ignore[attr-defined]


class TestUpdateWaitFencesOnTheStateItClaimsToBeIn:
    """``update_wait``'s ``AND state=?`` is the only DB-side re-check of the row.

    Its one caller (``subagent_manager/admission/waits.py``) reads the state in
    MEMORY and then posts the write on the writer thread, so a concurrent
    ``wake_wait`` can move the row in between. Widen this predicate and the wait
    record is rewritten onto a row that already left the wait, which resurrects a
    question nobody is waiting on.

    Mutation this pin must red: ``WHERE id=? AND state=?`` ->
    ``WHERE id=? AND (state=? OR 1)``, and the same by deletion.
    """

    def test_a_row_that_left_the_wait_refuses_the_rewrite(
        self, store: TaskStore, clock: Clock
    ) -> None:
        _running(store, "t1")
        entered = WaitRecord.input("call-1", since=clock.t, reason="a passphrase?")
        assert store.enter_wait("t1", entered.to_dict())
        before = store.get("t1").wait

        # A concurrent wake takes the row to retry_wait, which is not a wait state.
        assert store.wake_wait("t1", reason="answered elsewhere") is not None
        assert store.get("t1").state == model.RETRY_WAIT

        stale = WaitRecord.input("call-1", since=clock.t, reason="a DIFFERENT question").to_dict()
        assert (
            store.update_wait("t1", stale) is False
        ), "the row left waiting_input, so the rewrite must be refused DB-side"
        assert (
            store.get("t1").wait == before or store.get("t1").wait is None
        ), "a refused rewrite must not leave the new record on the row"

    def test_the_healthy_side_commits_and_the_record_moves(
        self, store: TaskStore, clock: Clock
    ) -> None:
        _running(store, "t2")
        assert store.enter_wait("t2", WaitRecord.input("call-2", since=clock.t).to_dict())
        again = WaitRecord.input("call-2", since=clock.t, reason="now with a reason").to_dict()
        assert store.update_wait("t2", again) is True, "a satisfied fence must COMMIT"
        assert store.get("t2").state == model.WAITING_INPUT
        assert (store.get("t2").wait or {}).get(
            "reason"
        ) == "now with a reason", "the committed rewrite must be readable on the row"


class TestUpdateWaitFencesOnTheGenerationTheCallerHandsIt:
    """``update_wait``'s optional ``AND generation=?`` is a caller-supplied fence.

    A caller that read the row at generation G and posts the rewrite later must
    not land it on generation G+1 -- that is a different run of the same row. The
    state fence above cannot catch it, because a row can return to the SAME wait
    state under a new generation.

    Mutation this pin must red: ``AND generation=?`` ->
    ``AND (generation=? OR 1)``, and the same by deletion.
    """

    def test_a_stale_generation_is_refused_in_the_same_wait_state(
        self, store: TaskStore, clock: Clock
    ) -> None:
        _running(store, "t3")
        assert store.enter_wait("t3", WaitRecord.input("call-3", since=clock.t).to_dict())
        old_gen = store.get("t3").generation

        # Same row, same wait state, a NEW generation: wake, re-dispatch, re-enter.
        # The re-dispatch bumps the generation again, so read it rather than
        # assuming ``wake_wait``'s return value is still current.
        new_gen = store.wake_wait("t3", reason="woken")
        assert new_gen is not None and new_gen > old_gen
        assert store.claim("t3") is not None
        assert store.transition("t3", model.STARTING)
        assert store.transition("t3", model.RUNNING)
        assert store.enter_wait("t3", WaitRecord.input("call-3", since=clock.t).to_dict())
        assert store.get("t3").state == model.WAITING_INPUT
        assert (
            store.get("t3").generation > old_gen
        ), "the row is back in the same wait under a later generation"

        stale = WaitRecord.input("call-3", since=clock.t, reason="from the old run").to_dict()
        assert (
            store.update_wait("t3", stale, generation=old_gen) is False
        ), "the state matches but the generation does not: this is another run"
        assert (store.get("t3").wait or {}).get("reason") != "from the old run"

    def test_the_current_generation_commits(self, store: TaskStore, clock: Clock) -> None:
        _running(store, "t4")
        assert store.enter_wait("t4", WaitRecord.input("call-4", since=clock.t).to_dict())
        gen = store.get("t4").generation
        fresh = WaitRecord.input("call-4", since=clock.t, reason="this run").to_dict()
        assert (
            store.update_wait("t4", fresh, generation=gen) is True
        ), "the caller's own generation must COMMIT"
        assert (store.get("t4").wait or {}).get("reason") == "this run"


class TestEnterWaitRefusesAStateThatIsNotAWait:
    """``enter_wait``'s ``if state not in WAITING`` keeps it from writing a terminal.

    ``enter_wait`` reaches the general ``transition``, so without this guard
    ``enter_wait(id, {"state": "done"})`` writes a REAL terminal transition and
    silently drops the wait record -- the row is finished and nothing waits.

    Mutation this pin must red: ``if state not in WAITING:`` ->
    ``if False:``, and the same by deletion.
    """

    def test_a_terminal_state_is_refused_and_the_row_does_not_move(self, store: TaskStore) -> None:
        _running(store, "t5")
        with pytest.raises(InvalidTransition):
            store.enter_wait("t5", {"state": model.DONE})
        assert (
            store.get("t5").state == model.RUNNING
        ), "a refused enter_wait must not have written a terminal transition"

    def test_a_claimable_state_is_refused_too(self, store: TaskStore) -> None:
        """The next-weakest guard a reader might write is 'not terminal'."""
        _running(store, "t6")
        with pytest.raises(InvalidTransition):
            store.enter_wait("t6", {"state": model.QUEUED})
        assert store.get("t6").state == model.RUNNING

    def test_the_healthy_side_enters_the_wait_and_lands_the_record(
        self, store: TaskStore, clock: Clock
    ) -> None:
        _running(store, "t7")
        rec = WaitRecord.input("call-7", since=clock.t, reason="a real wait").to_dict()
        assert store.enter_wait("t7", rec) is True, "a genuine WAITING state must COMMIT"
        row = store.get("t7")
        assert row.state == model.WAITING_INPUT
        assert (row.wait or {}).get("reason") == "a real wait"


class TestOrphanedChildrenOnlyReportsChildrenOfTerminalParents:
    """``orphaned_children``'s ``AND p.state IN (terminal)`` bounds a cancelling read.

    ``WaitLedger.rebuild()`` runs on every ``open_default_store`` and cancels what
    this read returns. Widen the parent predicate and rebuild cancels EVERY
    non-terminal child on the host, including the children of a parent that
    reconcile deliberately left mid-flight.

    Mutation this pin must red: ``AND p.state IN {_SQL_TERMINAL}``
    -> ``AND (p.state IN {_SQL_TERMINAL} OR 1)``, and the same by deletion.
    """

    def test_a_child_of_a_live_parent_is_not_orphaned(self, store: TaskStore) -> None:
        _running(store, "parent-live")
        _row(store, "kid-live", "subagent:parent-live", parent="parent-live")
        assert store.get("parent-live").state == model.RUNNING
        ids = [r.id for r in store.orphaned_children()]
        assert (
            "kid-live" not in ids
        ), "the parent is still running, so nothing has abandoned this child"

    def test_a_child_of_a_terminal_parent_is_orphaned(self, store: TaskStore) -> None:
        _running(store, "parent-gone")
        _row(store, "kid-gone", "subagent:parent-gone", parent="parent-gone")
        assert store.finish("parent-gone", model.DONE) or True
        assert store.get("parent-gone").state in model.TERMINAL
        ids = [r.id for r in store.orphaned_children()]
        assert "kid-gone" in ids, "a terminal parent collects nobody: the child IS orphaned"

    def test_the_two_cases_are_distinguished_in_one_read(self, store: TaskStore) -> None:
        """The healthy side: the read separates them rather than returning both."""
        _running(store, "p-live")
        _row(store, "k-live", "subagent:p-live", parent="p-live")
        _running(store, "p-dead")
        _row(store, "k-dead", "subagent:p-dead", parent="p-dead")
        store.finish("p-dead", model.DONE)
        ids = {r.id for r in store.orphaned_children()}
        assert ids & {"k-live", "k-dead"} == {
            "k-dead"
        }, "exactly the child of the terminal parent, not both"


class TestFairFetchHonoursThePerLaneCap:
    """``fetch_dispatchable_fair``'s ``WHERE lane_rank<=?`` is the per-lane cap.

    ``taskq_bridge`` passes ``per_lane_limit=1``, which is what stops one busy
    lane from filling a dispatch batch on its own. No test passed the argument at
    all before this pin, so widening the comparison changed nothing observable.

    Mutation this pin must red: ``WHERE lane_rank<=?`` ->
    ``WHERE (lane_rank<=? OR 1)``, and the same by deletion.
    """

    def test_one_row_per_lane_at_cap_one(self, store: TaskStore) -> None:
        for i in range(3):
            _row(store, f"a{i}", "dash:a")
        for i in range(3):
            _row(store, f"b{i}", "dash:b")
        got = store.fetch_dispatchable_fair(
            model.KIND_SUBAGENT,
            limit=6,
            scheduler=lanes.LaneScheduler(),
            per_lane_limit=1,
        )
        per_lane: dict[str, int] = {}
        for r in got:
            per_lane[r.lane] = per_lane.get(r.lane, 0) + 1
        assert per_lane, "the read must return something to be meaningful"
        assert (
            max(per_lane.values()) == 1
        ), f"per_lane_limit=1 must admit one row per lane, got {per_lane}"

    def test_a_cap_of_two_admits_two_and_no_more(self, store: TaskStore) -> None:
        """The next-weakest reading is 'the cap is advisory': it is not."""
        for i in range(4):
            _row(store, f"a{i}", "dash:a")
        got = store.fetch_dispatchable_fair(
            model.KIND_SUBAGENT,
            limit=4,
            scheduler=lanes.LaneScheduler(),
            per_lane_limit=2,
        )
        assert [r.id for r in got] == [
            "a0",
            "a1",
        ], "two oldest of the lane, FIFO inside it, and nothing beyond the cap"

    def test_the_healthy_side_returns_the_whole_lane_without_a_cap(self, store: TaskStore) -> None:
        for i in range(4):
            _row(store, f"a{i}", "dash:a")
        got = store.fetch_dispatchable_fair(
            model.KIND_SUBAGENT, limit=4, scheduler=lanes.LaneScheduler()
        )
        assert [r.id for r in got] == [
            "a0",
            "a1",
            "a2",
            "a3",
        ], "no cap means the lane fills the batch: the cap is what differs"
