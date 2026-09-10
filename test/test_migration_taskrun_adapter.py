"""Task-run serialization, fidelity reporting and the plan-side adapter.

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

from kiro_crew.migration.protocol import MidRunError
from kiro_crew.migration.taskrun_adapter import (
    PROJECT_DROP_FIELDS,
    PROJECT_SHIP_FIELDS,
    TaskRunMigrationAdapter,
    describe_discarded_progress,
    run_fidelity_findings,
    serialize_project,
)
from kiro_crew.task_models import Project, Task, TaskStatus, WorkingMemory

_STATE = {
    "repo_root": "/repo",
    "branch_name": "feat/x",
    "worktree_path": "/wt/x",
    "commit_hashes": ["abc123"],
}

# ---------------------------------------- circle 2: serialize / quiesce / resume


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
async def test_mid_run_refusal_works_on_the_raw_dict():
    raw = _raw_run()
    raw["task_details"][1]["status"] = "in_progress"
    a = TaskRunMigrationAdapter(run_lookup={"TASK_raw": raw})
    with pytest.raises(MidRunError):
        await a.refuse_if_mid_run("TASK_raw")
