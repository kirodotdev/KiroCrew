"""Slice 4 (circle 1) — task-run resume/restart classifier (issue #7577).

Task 4.2 / Req 6.2-6.3: whether a migrated task-runner run can RESUME or must
RESTART is a *git reproducibility* question on the target, not a run-record
question. The classifier probes: does repo_root resolve, is the branch
reachable, can the worktree be recreated. Any unreproducible reference is
NAMED (Req 6.3), and an unreproducible git state forces 'restart'.

Side-effect discipline: the git probe is INJECTED as a callable, so the test
does no real git, no disk, no subprocess.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew.migration import protocol as P
from kiro_crew.migration.taskrun_adapter import (
    GitReproProbe,
    PROJECT_DROP_FIELDS,
    PROJECT_SHIP_FIELDS,
    RestartNotConfirmed,
    TaskRunMigrationAdapter,
    classify_resume_or_restart,
    describe_discarded_progress,
    remaining_tasks_after_resume,
    require_restart_confirmation,
    run_fidelity_findings,
    serialize_project,
)


def _probe(repo_root=True, branch=True, worktree=True) -> GitReproProbe:
    return GitReproProbe(
        repo_root_resolves=lambda rr: repo_root,
        branch_reachable=lambda b: branch,
        worktree_recreatable=lambda w: worktree,
    )


_STATE = {
    "repo_root": "/repo",
    "branch_name": "feat/x",
    "worktree_path": "/wt/x",
    "commit_hashes": ["abc123"],
}


def test_all_reproducible_classifies_resume_with_no_blocking_findings():
    report = classify_resume_or_restart(_STATE, _probe())
    assert report.resume_class == "resume"
    assert report.blocked is False


def test_missing_repo_root_forces_restart_and_names_it():
    report = classify_resume_or_restart(_STATE, _probe(repo_root=False))
    assert report.resume_class == "restart"
    named = {f.detail_key for f in report.findings}
    assert "repo_root" in named


def test_unreachable_branch_forces_restart_and_names_it():
    report = classify_resume_or_restart(_STATE, _probe(branch=False))
    assert report.resume_class == "restart"
    assert any(f.detail_key == "branch_name" for f in report.findings)


def test_worktree_not_recreatable_forces_restart_and_names_it():
    report = classify_resume_or_restart(_STATE, _probe(worktree=False))
    assert report.resume_class == "restart"
    assert any(f.detail_key == "worktree_path" for f in report.findings)


def test_multiple_unreproducible_references_are_all_named():
    report = classify_resume_or_restart(
        _STATE, _probe(repo_root=False, branch=False, worktree=False)
    )
    assert report.resume_class == "restart"
    named = {f.detail_key for f in report.findings}
    assert {"repo_root", "branch_name", "worktree_path"} <= named


def test_restart_finding_is_advisory_not_blocking():
    # restart is a user-confirmable outcome, not a hard block: the confirmation
    # gate (Task 4.4) lives elsewhere. The classifier only classifies.
    report = classify_resume_or_restart(_STATE, _probe(repo_root=False))
    assert all(f.severity == "advisory" for f in report.findings)
    assert report.blocked is False


# ---------------------------------------- circle 2: serialize / quiesce / resume

from kiro_crew.task_models import Project, Task, TaskStatus, WorkingMemory


def _project():
    return Project(
        spec_path="/repo/.kiro/specs/x/tasks.md",
        spec_content="# plan\n- do things",
        tasks=[
            Task(index=0, title="a", description="", status=TaskStatus.PASSED),
            Task(index=1, title="b", description="", status=TaskStatus.SKIPPED),
            Task(
                index=2,
                title="c",
                description="",
                status=TaskStatus.PENDING,
                requires_approval=True,
            ),
            Task(index=3, title="d", description="", status=TaskStatus.PENDING),
        ],
        current_task=2,
        replan_count=1,
        memory=WorkingMemory(files_changed=["src/x.py"], decisions=["chose Y"]),
        task_id="TASK_abc",
        repo_root="/repo",
        branch_name="feat/x",
        worktree_path="/wt/x",
    )


def test_serialize_project_carries_tasks_status_current_and_memory():
    payload = serialize_project(_project())
    assert payload["current_task"] == 2
    assert payload["replan_count"] == 1
    assert payload["spec_content"].startswith("# plan")
    assert len(payload["tasks"]) == 4
    # per-task status + approval flags survive
    assert payload["tasks"][0]["status"] == "passed"
    assert payload["tasks"][2]["requires_approval"] is True
    # working memory survives
    assert payload["memory"]["files_changed"] == ["src/x.py"]
    assert payload["memory"]["decisions"] == ["chose Y"]


def test_remaining_tasks_after_resume_skips_completed():
    remaining = remaining_tasks_after_resume(_project())
    idxs = [t.index for t in remaining]
    assert 0 not in idxs and 1 not in idxs  # PASSED / SKIPPED not re-run
    assert idxs == [2, 3]  # only pending work remains


def test_resume_preserves_requires_approval_on_pending_task():
    remaining = remaining_tasks_after_resume(_project())
    approval_task = next(t for t in remaining if t.index == 2)
    assert approval_task.requires_approval is True  # migration is not an approval channel


@pytest.mark.asyncio
async def test_quiesce_refuses_when_a_task_is_mid_execution():
    proj = _project()
    proj.tasks[2].status = TaskStatus.IN_PROGRESS
    a = TaskRunMigrationAdapter(run_lookup={"TASK_abc": proj})
    with pytest.raises(P.MidRunError):
        await a.quiesce("TASK_abc")


@pytest.mark.asyncio
async def test_quiesce_pauses_at_task_boundary_when_idle():
    a = TaskRunMigrationAdapter(run_lookup={"TASK_abc": _project()})
    token = await a.quiesce("TASK_abc")
    assert isinstance(token, P.QuiesceToken)


@pytest.mark.asyncio
async def test_adapter_serialize_round_trips_through_the_seam():
    a = TaskRunMigrationAdapter(run_lookup={"TASK_abc": _project()})
    payload = await a.serialize("TASK_abc")
    assert payload["task_id"] == "TASK_abc"
    assert len(payload["tasks"]) == 4


# ------------- circle 3: restart confirmation (4.4) + resumability (4.7)


def test_describe_discarded_progress_names_what_a_restart_throws_away():
    desc = describe_discarded_progress(_project())
    # two tasks are already done (PASSED + SKIPPED) and would be re-run
    assert desc["completed_count"] == 2
    assert "a" in desc["completed_titles"] and "b" in desc["completed_titles"]
    assert desc["commit_count"] == 0  # no commit_hashes on the fixture


def test_restart_without_confirmation_is_refused_and_names_the_loss():
    with pytest.raises(RestartNotConfirmed) as exc:
        require_restart_confirmation(_project(), confirmed=False)
    msg = str(exc.value)
    assert "2" in msg  # names the discarded count
    assert "restart" in msg.lower()


def test_restart_with_confirmation_returns_the_discarded_summary():
    desc = require_restart_confirmation(_project(), confirmed=True)
    assert desc["completed_count"] == 2  # explicit, never silent


def test_resume_classification_needs_no_confirmation():
    # a resume discards nothing, so the gate is not in its path at all
    report = classify_resume_or_restart(_STATE, _probe())
    assert report.resume_class == "resume"


@pytest.mark.asyncio
async def test_migration_failure_leaves_the_run_resumable_in_place():
    proj = _project()
    a = TaskRunMigrationAdapter(run_lookup={"TASK_abc": proj})
    token = await a.quiesce("TASK_abc")
    assert a.is_resumable_in_place("TASK_abc") is False  # quiesced == paused
    # a pre-ack failure un-quiesces: the run must be runnable again, unchanged
    await a.unquiesce("TASK_abc", token)
    assert a.is_resumable_in_place("TASK_abc") is True
    # and no completed task was disturbed by the round trip (Req 6.8)
    assert [t.index for t in remaining_tasks_after_resume(proj)] == [2, 3]


@pytest.mark.asyncio
async def test_tombstone_is_never_written_without_a_remote_id():
    a = TaskRunMigrationAdapter(run_lookup={"TASK_abc": _project()})
    with pytest.raises(ValueError):
        await a.tombstone("TASK_abc", P.CrewRef(crew_id="dst"), "")


@pytest.mark.asyncio
async def test_taskrun_adapter_completes_the_seam():
    a = TaskRunMigrationAdapter(
        run_lookup={"TASK_abc": _project()}, create_run=lambda payload: "remote-TASK_abc"
    )
    remote = await a.materialize({"task_id": "TASK_abc", "tasks": []})
    assert remote == "remote-TASK_abc"
    await a.tombstone("TASK_abc", P.CrewRef(crew_id="dst"), remote)
    ts = a.tombstone_of("TASK_abc")
    assert ts.unit_kind == "taskrun" and ts.remote_unit_id == "remote-TASK_abc"
    assert a.is_resumable_in_place("TASK_abc") is False  # released to target


# --------------------------- circle 4: Project allow-list drift guard (4.9)


def test_project_ship_and_drop_partition_covers_every_field():
    all_fields = {f.name for f in dataclasses.fields(Project)}
    partitioned = set(PROJECT_SHIP_FIELDS) | set(PROJECT_DROP_FIELDS)
    missing = all_fields - partitioned
    assert not missing, f"Project fields with no ship/drop decision: {missing}"
    overlap = set(PROJECT_SHIP_FIELDS) & set(PROJECT_DROP_FIELDS)
    assert not overlap, f"fields in BOTH ship and drop: {overlap}"


def test_project_drift_guard_named_fields_still_exist():
    all_fields = {f.name for f in dataclasses.fields(Project)}
    for f in PROJECT_SHIP_FIELDS:
        assert f in all_fields, f"ship field '{f}' no longer on Project"
    for f in PROJECT_DROP_FIELDS:
        assert f in all_fields, f"drop field '{f}' no longer on Project"


def test_host_local_git_state_is_dropped_from_the_payload():
    # worktree_path is a SOURCE-host path; the target recreates its own
    assert "worktree_path" in PROJECT_DROP_FIELDS
    payload = serialize_project(_project())
    assert "worktree_path" not in payload


def test_run_identity_and_timing_are_dropped():
    for f in ("started_at", "finished_at", "last_task_time"):
        assert f in PROJECT_DROP_FIELDS
    payload = serialize_project(_project())
    for f in ("started_at", "finished_at", "last_task_time"):
        assert f not in payload


def test_serialize_still_ships_the_resume_critical_state():
    payload = serialize_project(_project())
    for f in ("tasks", "current_task", "replan_count", "memory", "spec_content", "task_id"):
        assert f in payload, f"{f} must survive for a resume to be coherent"


# ------------- circle 5: the persisted form (runs.json raw dict) works too


def _raw_run():
    """A run exactly as ``runs.json`` stores it (verified against
    taskrunner.py's _serialize_runs at ebc0936): the task list is under
    ``task_details``, statuses are strings, and there is NO ``memory`` and NO
    ``current_task`` -- those live only on the in-memory Project."""
    return {
        "task_id": "TASK_raw",
        "name": "raw run",
        "spec_path": "/repo/spec.md",
        "spec_content": "# plan",
        "status": "paused",
        "replan_count": 0,
        "repo_root": "/repo",
        "branch_name": "feat/x",
        "worktree_path": "/wt/x",
        "work_dir": "/wt/x",
        "started_at": 111.0,
        "error": "",
        "commit_hashes": [],
        "git_enabled": True,
        "source": "spec",
        "task_details": [
            {
                "index": 0,
                "title": "a",
                "status": "passed",
                "requires_approval": False,
                "attempts": 1,
            },
            {
                "index": 1,
                "title": "b",
                "status": "pending",
                "requires_approval": True,
                "attempts": 0,
            },
        ],
    }


def test_serialize_accepts_the_persisted_raw_dict():
    payload = serialize_project(_raw_run())
    assert payload["task_id"] == "TASK_raw"
    assert len(payload["tasks"]) == 2  # task_details normalized to tasks
    # the same allow-list applies: host-local paths and timings are dropped
    assert "worktree_path" not in payload and "repo_root" not in payload
    assert "started_at" not in payload and "work_dir" not in payload


def test_persisted_form_normalizes_task_details_onto_one_wire_key():
    payload = serialize_project(_raw_run())
    # one task-list key on the wire regardless of which shape it came from
    assert "task_details" not in payload
    assert payload["tasks"][0]["status"] == "passed"


def test_remaining_tasks_after_resume_works_on_the_raw_dict():
    remaining = remaining_tasks_after_resume(_raw_run())
    assert [t["index"] for t in remaining] == [1]  # 'passed' not re-run


def test_describe_discarded_progress_works_on_the_raw_dict():
    desc = describe_discarded_progress(_raw_run())
    assert desc["completed_count"] == 1
    assert desc["completed_titles"] == ["a"]


def test_persisted_form_reports_the_state_runs_json_does_not_hold():
    """runs.json carries no WorkingMemory and no current_task, so a migration
    sourced from disk loses them. Report it, never swallow it (cf. Layer B)."""
    findings = run_fidelity_findings(_raw_run())
    keys = {f.detail_key for f in findings}
    assert "memory" in keys and "current_task" in keys
    assert all(f.severity == "advisory" for f in findings)


def test_live_project_reports_no_fidelity_gap():
    assert run_fidelity_findings(_project()) == []


@pytest.mark.asyncio
async def test_adapter_serializes_and_derives_requirements_from_raw_dict():
    a = TaskRunMigrationAdapter(run_lookup={"TASK_raw": _raw_run()})
    payload = await a.serialize("TASK_raw")
    assert payload["task_id"] == "TASK_raw"
    reqs = await a.requirements("TASK_raw")
    assert any(r.kind == "git_repo" and r.identity == "/repo" for r in reqs)


@pytest.mark.asyncio
async def test_quiesce_refuses_mid_run_on_the_raw_dict():
    raw = _raw_run()
    raw["task_details"][1]["status"] = "in_progress"
    a = TaskRunMigrationAdapter(run_lookup={"TASK_raw": raw})
    with pytest.raises(P.MidRunError):
        await a.quiesce("TASK_raw")
