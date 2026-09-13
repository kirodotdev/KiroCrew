"""Reconcile-first boot and the legacy import: nothing lost, nothing revived."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from overload_fakes import Clock
from overload_fakes import task_record as _rec

from kiro_crew import taskq
from kiro_crew.taskq import migrate, model
from kiro_crew.taskq.reconcile import reconcile_on_boot
from kiro_crew.taskq.store import TaskStore


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
