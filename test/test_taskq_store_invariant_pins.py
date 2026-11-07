"""Mutation pins for the load-bearing lines in ``taskq.store`` that carried no test.

These lines in ``kiro_crew.taskq.store``, its ``store_waits`` and ``store_reads``
owners, and the legacy import that fills it carry invariants that no other test
observes: delete any one of them and the whole suite stays green. This file pins
the reachable ones, whose silent removal loses, misfiles or double-runs work.
Lines whose invariant is unreachable are deliberately left unpinned, because a
pin on an unreachable line freezes dead code instead of protecting behaviour.

``AUTOSDE.yaml``'s ``an-invariant-line-needs-a-mutation-pin`` requires each pin to
red under three mutations, not one:

1. the line DELETED,
2. the line LOOSENED to the next-weakest thing a reader might write (a predicate
   widened to the full claimable set, a generation replaced by ``None``),
3. the HEALTHY side asserted -- under a satisfied fence the write COMMITS and the
   row moves.

Every class below carries all three. Each is verified by applying the mutations its
docstring names to its line and observing this file red; the counts are in the PR
description.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from overload_fakes import Clock, open_task_store

from kiro_crew.taskq import lanes, migrate, model
from kiro_crew.taskq import store as store_mod
from kiro_crew.taskq.model import InvalidTransition
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable
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

    ``taskq_bridge_window`` passes ``per_lane_limit=1``, which is what stops one busy
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


def _damage_an_index(path: Path) -> None:
    """Give *path* damage that only ``PRAGMA integrity_check`` sees.

    An index re-pointed at another column keeps a valid header and schema, so the
    file opens and ``quick_check`` (which skips index-vs-table content) says ok.
    """
    with contextlib.closing(sqlite3.connect(str(path))) as conn:
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, False)
        conn.execute("CREATE TABLE scratch (a INTEGER, b INTEGER)")
        conn.execute("CREATE INDEX scratch_a ON scratch(a)")
        conn.executemany("INSERT INTO scratch VALUES (?, ?)", [(i, -i) for i in range(10)])
        conn.commit()
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute(
            "UPDATE sqlite_schema SET sql='CREATE INDEX scratch_a ON scratch(b)' "
            "WHERE name='scratch_a'"
        )
        conn.commit()
    with contextlib.closing(sqlite3.connect(str(path))) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert (
            conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
        ), "the fixture no longer models a file that opens but fails integrity_check"


class TestOpenQuarantinesAFileThatFailsTheIntegrityCheck:
    """``_open_connection``'s ``integrity_check`` verdict is the only trigger for a
    file that OPENS: every other one needs a statement touching the bad page, and
    ``open()`` reads only the schema. Without it the damaged file is served.

    Mutation this pin must red: the ``raise _CorruptStore`` arm -> ``if False:``;
    loosened to ``PRAGMA quick_check`` or to a logged warning.
    """

    def test_a_file_that_opens_but_fails_the_check_is_quarantined(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = tmp_path / "tasks" / "tasks.db"
        first = TaskStore(path, clock=clock, network_fs=False).open()
        first.accept_one(model.TaskRecord(id="before", kind=model.KIND_SUBAGENT))
        first.close()
        _damage_an_index(path)

        store = TaskStore(path, clock=clock, network_fs=False).open()
        try:
            assert store.quarantined_to is not None, "a file failing integrity_check is served"
            assert any(
                "integrity_check" in w for w in store.warnings
            ), "the verdict, not another trigger, must be what quarantined it"
            assert store.get("before") is None, "the recreated store starts empty"
        finally:
            store.close()

    def test_the_healthy_side_keeps_a_sound_file_and_its_rows(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = tmp_path / "tasks" / "tasks.db"
        first = TaskStore(path, clock=clock, network_fs=False).open()
        first.accept_one(model.TaskRecord(id="kept", kind=model.KIND_SUBAGENT))
        first.close()

        store = TaskStore(path, clock=clock, network_fs=False).open()
        try:
            assert store.quarantined_to is None
            assert store.state_of("kept") == model.QUEUED
        finally:
            store.close()


class TestAnExplicitLaneParamFilesTheRowInThatLane:
    """``_lane_in_tx``'s ``params["lane"]`` override is how a runner row lands in
    the lane ``lane_for(session_key, source)`` names: ``system`` for a cron- or
    hook-launched run, whatever its session key. Without it that run competes
    with the person's own chat for their lane's cap.

    Mutation this pin must red: ``if explicit: return explicit`` -> ``if False:``;
    loosened to letting a parent's lane win over it.
    """

    def test_the_param_decides_the_lane_and_its_absence_the_session(self, store: TaskStore) -> None:
        kind = model.KIND_TASKRUNNER_STEP
        store.accept_one(
            model.TaskRecord(
                id="cron-run", kind=kind, session_key="dash:a", params={"lane": lanes.SYSTEM_LANE}
            )
        )
        store.accept_one(model.TaskRecord(id="chat-run", kind=kind, session_key="dash:a"))
        assert store.get("cron-run").lane == lanes.SYSTEM_LANE
        assert store.get("chat-run").lane == "dash:a", "no param: the session's lane"

    def test_the_param_wins_over_the_parents_lane(self, store: TaskStore) -> None:
        _row(store, "parent", "dash:a")
        store.accept_one(
            model.TaskRecord(
                id="child",
                kind=model.KIND_TASKRUNNER_STEP,
                session_key="dash:a",
                parent_id="parent",
                params={"lane": lanes.SYSTEM_LANE},
            )
        )
        assert store.get("parent").lane == "dash:a"
        assert store.get("child").lane == lanes.SYSTEM_LANE


class TestATerminalTransitionEmitsOneCompletion:
    """``transition``'s ``emit_counter(TASKQ_COMPLETIONS, ...)`` is the series the
    spec promises for every terminal write. The counter test only checks that
    ``store.py`` names it, which ``cancel``'s own emission keeps true.

    Mutation this pin must red: the emission -> ``if False:``; loosened to every
    transition, or to before the write is known to have committed.
    """

    @pytest.fixture
    def emitted(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
        seen: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(
            store_mod, "emit_counter", lambda name, attrs: seen.append((name, dict(attrs)))
        )
        return seen

    def test_each_committed_terminal_write_emits_its_outcome_once(
        self, store: TaskStore, emitted: list[tuple[str, dict[str, Any]]]
    ) -> None:
        _running(store, "ok")
        _running(store, "bad")
        assert store.finish("ok", model.DONE)
        assert store.finish("bad", model.FAILED, error="boom")
        assert emitted == [
            (store_mod.TASKQ_COMPLETIONS, {"outcome": model.DONE}),
            (store_mod.TASKQ_COMPLETIONS, {"outcome": model.FAILED}),
        ]

    def test_a_non_terminal_or_refused_write_emits_nothing(
        self, store: TaskStore, emitted: list[tuple[str, dict[str, Any]]]
    ) -> None:
        _running(store, "t")
        stale = store.get("t").generation - 1
        assert store.finish("t", model.DONE, generation=stale) is False
        assert emitted == [], "starting, running and a refused finish are not completions"


class TestOneRefusedLegacyRowDoesNotAbortTheImport:
    """``import_legacy``'s per-row ``except`` keeps one row the store refuses (a
    locked moment) from stopping the boot import of every row after it; it is
    also the only writer of ``ImportReport.errors``.

    Mutation this pin must red: the ``except`` deleted or narrowed to an
    exception the store never raises; loosened to ``break`` at the first failure.
    """

    REFUSED = f"{migrate.TASKRUNNER_ID_PREFIX}r1"
    LATER = f"{migrate.TASKRUNNER_ID_PREFIX}r2"

    @pytest.fixture
    def runs(self, tmp_path: Path) -> Path:
        path = tmp_path / "runs.json"
        paused = [{"task_id": "r1", "status": "paused"}, {"task_id": "r2", "status": "paused"}]
        path.write_text(json.dumps(paused), encoding="utf-8")
        return path

    def _import_refusing_r1(self, store: TaskStore, runs: Path) -> migrate.ImportReport:
        def insert(rec: model.TaskRecord) -> bool:
            if rec.id == self.REFUSED:
                raise TaskStoreUnavailable("database is locked")
            return store.insert_if_absent(rec)

        return migrate.import_legacy(insert, subagents_dir=None, taskrunner_runs_path=runs)

    def test_the_rows_after_a_refused_one_are_still_imported(
        self, store: TaskStore, runs: Path
    ) -> None:
        report = self._import_refusing_r1(store, runs)
        assert store.get(self.LATER) is not None, "the refusal of r1 must not cost r2"
        assert report.taskrunner_imported == 1
        assert len(report.errors) == 1 and report.errors[0].startswith(self.REFUSED)

    def test_the_healthy_side_imports_the_refused_row_on_the_next_pass(
        self, store: TaskStore, runs: Path
    ) -> None:
        self._import_refusing_r1(store, runs)
        again = migrate.import_legacy(
            store.insert_if_absent, subagents_dir=None, taskrunner_runs_path=runs
        )
        assert again.errors == [] and again.taskrunner_imported == 1
        assert again.skipped_existing == 1
        assert store.state_of(self.REFUSED) == model.RECOVERING
