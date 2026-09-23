"""Reconcile-first boot and the legacy import: nothing lost, nothing revived."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from overload_fakes import Clock
from overload_fakes import task_record as _rec

from kiro_crew import taskq
from kiro_crew.taskq import migrate, model, waits
from kiro_crew.taskq.reconcile import reconcile_on_boot
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable


def _crash_and_reopen(path: Path, clock: Clock) -> TaskStore:
    """A new incarnation over the same file, as a restarted gateway sees it."""
    return TaskStore(path, clock=clock, network_fs=False).open()


@pytest.fixture
def clock() -> Clock:
    return Clock(9000.0)


def _seed_crash_scenario(path: Path, clock: Clock) -> dict[str, int]:
    """Rows at every point a crash can hit; returns generations by id."""
    s = TaskStore(path, clock=clock, network_fs=False).open()
    gens: dict[str, int] = {}
    s.accept(
        [
            _rec("queued_only"),
            _rec("admitted_lost", side_effect_class=model.SIDE_EFFECT_NONE),
            _rec("running_unknown"),  # default class unknown
            _rec("running_idem", side_effect_class=model.SIDE_EFFECT_IDEMPOTENT_KEY),
            _rec("done_before_ack"),
            _rec("cancelled_before_crash"),
            _rec("was_cancelled_while_running"),
            _rec("tr", kind=model.KIND_TASKRUNNER_STEP, side_effect_class=model.SIDE_EFFECT_NONE),
        ]
    )
    for tid in (
        "admitted_lost",
        "running_unknown",
        "running_idem",
        "done_before_ack",
        "was_cancelled_while_running",
        "tr",
    ):
        gens[tid] = s.claim(tid).generation
    for tid in ("running_unknown", "running_idem", "done_before_ack", "tr"):
        s.transition(tid, model.STARTING, generation=gens[tid])
        s.transition(tid, model.RUNNING, generation=gens[tid])
    s.transition(
        "was_cancelled_while_running",
        model.STARTING,
        generation=gens["was_cancelled_while_running"],
    )
    s.cancel("was_cancelled_while_running", reason="user_stop")
    s.cancel("cancelled_before_crash", reason="user_stop")
    # "done_before_ack": the result artifact exists but the done write never landed
    s.close()  # crash
    return gens


def test_reconcile_settles_every_lost_owner_row_by_class(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "t.db"
    _seed_crash_scenario(path, clock)
    clock.t += 100

    def probe(rec: model.TaskRecord) -> str | None:
        return model.DONE if rec.id == "done_before_ack" else None

    s = _crash_and_reopen(path, clock)
    try:
        report = reconcile_on_boot(s, artifact_probe=probe)
        assert report.examined == 5  # 4 subagent active + taskrunner; queued/cancelled untouched
        assert report.settled_done == 1
        assert report.unknown_side_effect == 1
        assert report.requeued == 1  # admitted_lost: claimed, never started
        assert report.recovering == 1  # running_idem (idempotent_key)
        assert report.awaiting_adapter == 1
        assert report.errors == []

        assert s.state_of("queued_only") == model.QUEUED
        assert s.state_of("done_before_ack") == model.DONE
        assert s.state_of("running_unknown") == model.UNKNOWN_SIDE_EFFECT
        assert s.state_of("admitted_lost") == model.QUEUED
        assert s.state_of("running_idem") == model.RECOVERING
        assert s.state_of("tr") == model.RUNNING  # no adapter: state kept, lease dropped
        assert s.get("tr").lease_owner is None
        assert [e.kind for e in s.events("tr")][-1] == "awaiting_adapter"
        # recovering rows are re-dispatchable after their backoff
        rec = s.get("running_idem")
        assert rec.lease_owner is None
        assert rec.next_run_at == clock.t + model.recovery_backoff_secs(rec.attempts)
        clock.t += 200
        assert s.claim("running_idem") is not None
        assert s.claim("admitted_lost") is not None
    finally:
        s.close()


def test_cancelled_never_revives_after_reconcile(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "t.db"
    _seed_crash_scenario(path, clock)

    def probe(rec: model.TaskRecord) -> str | None:
        return model.DONE  # even a probe that says "done" cannot revive a cancel

    s = _crash_and_reopen(path, clock)
    try:
        reconcile_on_boot(s, artifact_probe=probe)
        assert s.state_of("cancelled_before_crash") == model.CANCELLED
        assert s.state_of("was_cancelled_while_running") == model.CANCELLED
        assert s.claim("cancelled_before_crash") is None
        assert s.claim("was_cancelled_while_running") is None
        assert s.fetch_dispatchable(model.KIND_SUBAGENT, limit=100) == [s.get("queued_only")]
    finally:
        s.close()


def test_terminal_states_never_regress_across_restart(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "t.db"
    s = TaskStore(path, clock=clock, network_fs=False).open()
    s.accept([_rec("ok"), _rec("bad")])
    for tid, terminal in (("ok", model.DONE), ("bad", model.FAILED)):
        g = s.claim(tid).generation
        s.transition(tid, model.STARTING, generation=g)
        s.transition(tid, model.RUNNING, generation=g)
        s.finish(tid, terminal, generation=g)
    s.close()
    s = _crash_and_reopen(path, clock)
    try:
        report = reconcile_on_boot(s, artifact_probe=lambda r: model.FAILED)
        assert report.examined == 0
        assert s.state_of("ok") == model.DONE and s.state_of("bad") == model.FAILED
    finally:
        s.close()


def test_reconcile_is_idempotent_and_ignores_live_rows(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "t.db"
    _seed_crash_scenario(path, clock)
    s = _crash_and_reopen(path, clock)
    try:
        first = reconcile_on_boot(s)
        assert first.changed > 0
        # this incarnation now dispatches a row: it is live and must be left alone
        clock.t += 1000
        live = s.claim("queued_only")
        assert live is not None
        second = reconcile_on_boot(s)
        assert second.changed == 0
        assert s.state_of("queued_only") == model.ADMITTED
    finally:
        s.close()


def test_probe_tombstone_maps_to_failed_and_cancelled(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "t.db"
    s = TaskStore(path, clock=clock, network_fs=False).open()
    s.accept([_rec("f"), _rec("c")])
    for tid in ("f", "c"):
        g = s.claim(tid).generation
        s.transition(tid, model.STARTING, generation=g)
    s.close()
    verdicts = {"f": model.FAILED, "c": model.CANCELLED}
    s = _crash_and_reopen(path, clock)
    try:
        report = reconcile_on_boot(s, artifact_probe=lambda r: verdicts[r.id])
        assert report.settled_failed == 1 and report.settled_cancelled == 1
        assert s.state_of("f") == model.FAILED and s.state_of("c") == model.CANCELLED
    finally:
        s.close()


# ── legacy import ─────────────────────────────────────────────────────────────


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def test_import_legacy_subagent_folders_and_taskrunner_paused_runs(
    tmp_path: Path, clock: Clock
) -> None:
    home = tmp_path / "home"
    sub = home / "subagents"
    _write_json(
        sub / "aaa11111" / "state.json",
        {
            "id": "aaa11111",
            "task": "old work",
            "agent": "kirocrew",
            "parent_session": "dash:1",
            "started": 8000.0,
            "max_turns": 7,
            "status": "running",
            "memory_store": "crew-x",
        },
    )
    _write_json(sub / "bbb22222" / "state.json", {"id": "bbb22222", "task": "done work"})
    _write_json(sub / "bbb22222" / "tombstone.json", {"cause": "delivered"})
    _write_json(sub / "ccc33333" / "state.json", {"id": "other-id", "task": "mismatch"})
    (sub / "ddd44444").mkdir()
    (sub / "ddd44444" / "state.json").write_text("{not json", encoding="utf-8")
    runs = home / "work" / "runs.json"
    _write_json(
        runs,
        [
            {
                "task_id": "run-1",
                "name": "n1",
                "status": "paused",
                "spec_path": "/spec.md",
                "auto_approve": True,
            },
            {"task_id": "run-2", "name": "n2", "status": "completed"},
            {"task_id": "run-3", "name": "n3", "status": "planned"},
        ],
    )
    s = TaskStore(tmp_path / "t.db", clock=clock, network_fs=False).open()
    try:
        report = migrate.import_legacy(
            s.insert_if_absent, subagents_dir=sub, taskrunner_runs_path=runs, now=clock.t
        )
        assert report.subagents_imported == 1
        assert report.taskrunner_imported == 1
        assert report.errors == []
        sub_row = s.get("aaa11111")
        assert sub_row is not None
        assert sub_row.state == model.RECOVERING
        assert sub_row.kind == model.KIND_SUBAGENT
        assert sub_row.session_key == "dash:1"
        assert (
            sub_row.params["task"] == "old work" and sub_row.params["_preassigned_id"] == "aaa11111"
        )
        assert sub_row.scope_ref == {"memory_store": "crew-x"}
        assert sub_row.side_effect_class == model.SIDE_EFFECT_UNKNOWN
        assert sub_row.created_at == 8000.0
        assert sub_row.result_ref == str(sub / "aaa11111")
        assert s.get("bbb22222") is None and s.get("ccc33333") is None and s.get("ddd44444") is None
        tr = s.get(f"{migrate.TASKRUNNER_ID_PREFIX}run-1")
        assert tr is not None and tr.kind == model.KIND_TASKRUNNER_STEP
        assert tr.state == model.RECOVERING
        assert tr.scope_ref == {"auto_approve": False}  # persisted bypass never restored
        assert s.get(f"{migrate.TASKRUNNER_ID_PREFIX}run-2") is None
        assert s.get(f"{migrate.TASKRUNNER_ID_PREFIX}run-3") is None
        # idempotent
        again = migrate.import_legacy(
            s.insert_if_absent, subagents_dir=sub, taskrunner_runs_path=runs, now=clock.t
        )
        assert again.imported == 0 and again.skipped_existing == 2
        assert s.count() == 2
    finally:
        s.close()


def test_import_tolerates_missing_sources(tmp_path: Path, clock: Clock) -> None:
    s = TaskStore(tmp_path / "t.db", clock=clock, network_fs=False).open()
    try:
        report = migrate.import_legacy(
            s.insert_if_absent,
            subagents_dir=tmp_path / "nope",
            taskrunner_runs_path=tmp_path / "nope.json",
        )
        assert report.imported == 0 and report.errors == []
    finally:
        s.close()


def test_open_default_store_imports_then_reconciles(tmp_path: Path) -> None:
    """The boot sequence: an orphaned run folder becomes a row and is settled
    (class unknown, no tombstone -> unknown_side_effect) before any dispatch."""
    home = tmp_path / "home"
    _write_json(
        home / "subagents" / "orph0001" / "state.json",
        {"id": "orph0001", "task": "t", "started": 1.0},
    )
    s = taskq.open_default_store(home, window=8)
    try:
        assert s.path == home / "tasks" / "tasks.db"
        assert s.window == 8
        assert s.state_of("orph0001") == model.UNKNOWN_SIDE_EFFECT
        kinds = [e.kind for e in s.events("orph0001")]
        assert kinds[:2] == ["imported", "transition"]
    finally:
        s.close()
    # second boot: nothing to import, nothing to settle, row untouched
    s2 = taskq.open_default_store(home, window=8)
    try:
        assert s2.state_of("orph0001") == model.UNKNOWN_SIDE_EFFECT
        assert s2.count() == 1
    finally:
        s2.close()


def test_open_default_store_probe_marks_delivered_run_done(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_json(home / "subagents" / "run00001" / "state.json", {"id": "run00001", "task": "t"})
    s = TaskStore(home / "tasks" / "tasks.db", network_fs=False).open()
    s.accept([_rec("run00001")])
    g = s.claim("run00001").generation
    s.transition("run00001", model.STARTING, generation=g)
    s.close()  # crash before the run's terminal write
    _write_json(home / "subagents" / "run00001" / "tombstone.json", {"cause": "delivered"})

    def probe(rec: model.TaskRecord) -> str | None:
        return model.DONE if (home / "subagents" / rec.id / "tombstone.json").exists() else None

    s = taskq.open_default_store(home, artifact_probe=probe)
    try:
        assert s.state_of("run00001") == model.DONE
    finally:
        s.close()


# ── the boot sweeps settle every row they can reach ───────────────────────────
#
# ``open_default_store`` runs ``reconcile_on_boot`` and ``WaitLedger.rebuild``
# once each and keeps only the store, and the pump's repeated sweep reads wait
# DEADLINES alone -- so a parent these two do not wake, and an orphan they do not
# cancel, waits for the next restart. That makes the per-row isolation below the
# whole guarantee, not a nicety: an all-or-nothing pass loses every row behind
# the first refused write for the life of the process.


class _RefusingStore(TaskStore):
    """A real store whose named write is refused for named ids, as a transient
    write failure (full disk, read-only mount, a lock past ``busy_timeout``)
    raises it: one row's write fails, every other row's still commits."""

    def __init__(self, *a: object, refuse: dict[str, set[str]], **kw: object) -> None:
        super().__init__(*a, **kw)  # type: ignore[arg-type]
        self._refuse = refuse

    def _check(self, write: str, task_id: str) -> None:
        if task_id in self._refuse.get(write, ()):
            raise TaskStoreUnavailable(f"task {write} failed: disk I/O error")

    def transition(self, task_id: str, *a: object, **kw: object) -> bool:
        self._check("transition", task_id)
        return super().transition(task_id, *a, **kw)  # type: ignore[arg-type]

    def wake_wait(self, task_id: str, *a: object, **kw: object) -> "int | None":
        self._check("wake", task_id)
        return super().wake_wait(task_id, *a, **kw)  # type: ignore[arg-type]

    def cancel(self, task_id: str, *a: object, **kw: object) -> "int | None":
        self._check("cancel", task_id)
        return super().cancel(task_id, *a, **kw)  # type: ignore[arg-type]


def _seed_wait_scenario(path: Path, clock: Clock) -> None:
    """One deadline-passed dependency wait, one wakeable parent, one orphan."""
    s = TaskStore(path, clock=clock, network_fs=False).open()
    s.accept(
        [
            _rec("dep_overdue"),
            _rec("parent"),
            _rec("kid", parent_id="parent"),
            _rec("dead_parent"),
            _rec("orphan", parent_id="dead_parent"),
        ]
    )

    def _run(task_id: str) -> int:
        gen = s.claim(task_id).generation
        s.transition(task_id, model.STARTING, generation=gen)
        s.transition(task_id, model.RUNNING, generation=gen)
        return gen

    s.enter_wait(
        "dep_overdue",
        waits.WaitRecord.dependency(
            "github", since=clock.t - 100.0, deadline_at=clock.t - 1.0
        ).to_dict(),
        generation=_run("dep_overdue"),
    )
    s.enter_wait(
        "parent",
        waits.WaitRecord.children(["kid"], since=clock.t).to_dict(),
        generation=_run("parent"),
    )
    s.transition("kid", model.DONE, generation=_run("kid"))
    s.transition("dead_parent", model.FAILED, generation=_run("dead_parent"))
    s.close()  # crash: the parent's wake and the orphan's cancel died with it


def _reopen_refusing(path: Path, clock: Clock, **refuse: set[str]) -> "_RefusingStore":
    return _RefusingStore(path, clock=clock, network_fs=False, refuse=refuse).open()


def test_expire_returns_the_rows_it_failed_even_when_a_later_row_is_refused(
    tmp_path: Path, clock: Clock
) -> None:
    """The ids ``expire`` returns are the ids whose live runs get cancelled.

    ``taskq_expire_waits_store`` answers ``[]`` for a raise, so raising after a
    row was already failed leaves that row terminal with its runtime still
    resident and still charged to the host budget -- a leak no counter would
    ever decrement.
    """
    path = tmp_path / "t.db"
    s = TaskStore(path, clock=clock, network_fs=False).open()
    s.accept([_rec("first"), _rec("second")])
    for tid in ("first", "second"):
        gen = s.claim(tid).generation
        s.transition(tid, model.STARTING, generation=gen)
        s.transition(tid, model.RUNNING, generation=gen)
        s.enter_wait(
            tid,
            waits.WaitRecord.dependency(
                "github", since=clock.t - 100.0, deadline_at=clock.t - 1.0
            ).to_dict(),
            generation=gen,
        )
    s.close()
    refusing = _reopen_refusing(path, clock, transition={"second"})
    try:
        assert waits.WaitLedger(refusing).expire() == ["first"]
        assert refusing.state_of("first") == model.FAILED
        assert refusing.state_of("second") == model.WAITING_DEPENDENCY
    finally:
        refusing.close()


def test_rebuild_settles_the_rows_behind_a_refused_deadline_write(
    tmp_path: Path, clock: Clock
) -> None:
    """A refused deadline write costs its own row only.

    The deadline half runs FIRST, so raising out of it is what strands the parent
    and the orphan: neither has a deadline, so no later sweep looks at them again.
    """
    path = tmp_path / "t.db"
    _seed_wait_scenario(path, clock)
    s = _reopen_refusing(path, clock, transition={"dep_overdue"})
    try:
        report = waits.WaitLedger(s).rebuild()
        assert report.expired == []  # its write was refused
        assert s.state_of("dep_overdue") == model.WAITING_DEPENDENCY
        # ...and everything behind it is still settled.
        assert report.woken == ["parent"]
        assert report.cancelled_orphans == ["orphan"]
        assert s.state_of("parent") == model.RETRY_WAIT
        assert s.state_of("orphan") == model.CANCELLED
        # The pump re-reads deadlines on every sweep, so that row is not lost.
        assert report.errors == []
        assert waits.WaitLedger(_crash_and_reopen(path, clock)).expire() == ["dep_overdue"]
    finally:
        s.close()


def test_rebuild_names_the_parent_whose_wake_was_refused_and_still_cancels_the_orphan(
    tmp_path: Path, clock: Clock
) -> None:
    """A refused wake is reported per row; the orphan behind it is still cancelled."""
    path = tmp_path / "t.db"
    _seed_wait_scenario(path, clock)
    s = _reopen_refusing(path, clock, wake={"parent"})
    try:
        report = waits.WaitLedger(s).rebuild()
        assert report.expired == ["dep_overdue"]
        assert report.woken == []
        assert [e.split(":")[0] for e in report.errors] == ["parent"]
        assert s.state_of("parent") == model.WAITING_CHILDREN
        assert report.cancelled_orphans == ["orphan"]
        assert s.state_of("orphan") == model.CANCELLED
    finally:
        s.close()


def test_rebuild_names_the_orphan_whose_cancel_was_refused(
    tmp_path: Path, clock: Clock, caplog
) -> None:
    """An uncancelled orphan is a run nothing will collect: it is named, not swallowed.

    This is the one refusal the sweep already tolerated, and the loss was that
    it left no trace: the row keeps running with a terminal parent and the boot
    reads as clean.
    """
    path = tmp_path / "t.db"
    _seed_wait_scenario(path, clock)
    s = _reopen_refusing(path, clock, cancel={"orphan"})
    try:
        with caplog.at_level("INFO", logger="kiro_crew.taskq.waits"):
            report = waits.WaitLedger(s).rebuild()
        assert report.woken == ["parent"]
        assert report.cancelled_orphans == []
        assert [e.split(":")[0] for e in report.errors] == ["orphan"]
        assert "unsettled=1" in caplog.text
        assert s.state_of("orphan") == model.QUEUED
    finally:
        s.close()


def test_a_wait_past_its_deadline_is_failed_and_never_woken(tmp_path: Path, clock: Clock) -> None:
    """The two halves agree on a deadline-passed row without depending on order.

    The parent's children are all terminal AND its deadline has passed: the
    deadline wins. With the deadline write refused the row must STAY waiting so
    the pump's next sweep still fails it -- waking it to ``retry_wait`` would
    resume a run past its deadline and take the row out of every later sweep's
    reach.
    """
    path = tmp_path / "t.db"
    s = TaskStore(path, clock=clock, network_fs=False).open()
    s.accept([_rec("parent"), _rec("kid", parent_id="parent"), _rec("other")])
    for tid in ("parent", "kid"):
        gen = s.claim(tid).generation
        s.transition(tid, model.STARTING, generation=gen)
        s.transition(tid, model.RUNNING, generation=gen)
        if tid == "parent":
            s.enter_wait(
                "parent",
                waits.WaitRecord.children(
                    ["kid"], since=clock.t - 50.0, deadline_at=clock.t - 1.0
                ).to_dict(),
                generation=gen,
            )
        else:
            s.transition("kid", model.DONE, generation=gen)
    s.close()

    refusing = _reopen_refusing(path, clock, transition={"parent"})
    try:
        report = waits.WaitLedger(refusing).rebuild()
        assert report.woken == [] and report.expired == []
        assert refusing.state_of("parent") == model.WAITING_CHILDREN
    finally:
        refusing.close()
    reopened = _crash_and_reopen(path, clock)
    try:
        assert waits.WaitLedger(reopened).expire() == ["parent"]
        assert reopened.state_of("parent") == model.FAILED
    finally:
        reopened.close()


def test_a_children_wait_whose_deadline_has_not_passed_is_still_woken(
    tmp_path: Path, clock: Clock
) -> None:
    """The other half of the deadline verdict: NOT PAST is not the same as SET.

    ``rebuild`` is the only pass that wakes a restored parent -- the pump's
    repeated sweep reads deadlines alone -- so a parent skipped here is a parent
    never woken, and when its deadline does arrive the pump FAILS it: children
    that were accepted and ran to completion, discarded. Whether the row carries
    a deadline is one keystroke away from whether it is past one, and that
    keystroke costs exactly that loss, so this side is pinned as hard as the
    passed side above.
    """
    path = tmp_path / "t.db"
    s = TaskStore(path, clock=clock, network_fs=False).open()
    s.accept([_rec("parent"), _rec("kid", parent_id="parent")])
    for tid in ("parent", "kid"):
        gen = s.claim(tid).generation
        s.transition(tid, model.STARTING, generation=gen)
        s.transition(tid, model.RUNNING, generation=gen)
        if tid == "parent":
            s.enter_wait(
                "parent",
                waits.WaitRecord.children(
                    ["kid"], since=clock.t, deadline_at=clock.t + 300.0
                ).to_dict(),
                generation=gen,
            )
        else:
            s.transition("kid", model.DONE, generation=gen)
    s.close()  # crash: the parent's in-process wake died with it

    reopened = _crash_and_reopen(path, clock)
    try:
        ledger = waits.WaitLedger(reopened)
        report = ledger.rebuild()
        assert report.expired == [] and report.errors == []
        assert report.woken == ["parent"]
        assert reopened.state_of("parent") == model.RETRY_WAIT
        assert ledger.rebuild().woken == [], "idempotent"
        # The wake took the row out of the waiting set, so the deadline arriving
        # afterwards has nothing left to fail and the completed child work is
        # collected rather than thrown away.
        clock.t += 301
        assert ledger.expire() == []
        assert reopened.state_of("parent") == model.RETRY_WAIT
        assert reopened.claim("parent") is not None, "re-dispatched, never stranded"
    finally:
        reopened.close()


def test_reconcile_settles_the_row_whose_probe_raised_and_the_rows_behind_it(
    tmp_path: Path, clock: Clock
) -> None:
    """A probe that raises says nothing about ITS row and nothing about the rest.

    ``None`` is already the probe's "the artifacts say nothing" answer, so an
    exception is that answer for one row; unwinding would abandon every row after
    it, which the boot's single pass never revisits.
    """
    path = tmp_path / "t.db"
    _seed_crash_scenario(path, clock)

    def probe(rec: model.TaskRecord) -> str | None:
        if rec.id == "admitted_lost":
            raise OSError("tombstone directory is unreadable")
        return model.DONE if rec.id == "done_before_ack" else None

    s = _crash_and_reopen(path, clock)
    try:
        report = reconcile_on_boot(s, artifact_probe=probe)
        assert report.examined == 5
        assert [e.split(":")[0] for e in report.errors] == ["admitted_lost"]
        assert "artifact probe failed" in report.errors[0]
        # Settled by class, which is what a probe answering ``None`` would give.
        assert s.state_of("admitted_lost") == model.QUEUED
        # ...and every row the sweep had not reached yet is settled too.
        assert s.state_of("running_unknown") == model.UNKNOWN_SIDE_EFFECT
        assert s.state_of("running_idem") == model.RECOVERING
        assert s.state_of("done_before_ack") == model.DONE
    finally:
        s.close()
