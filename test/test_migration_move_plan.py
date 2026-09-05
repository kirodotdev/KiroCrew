"""Generic move-plan core (issue #7577) — shared by every unit kind's CLI verb.

``plan_cron_move`` only ever touched the generic adapter seam (bundle_kind,
bundle_version, serialize, requirements), so the same function serves session
and task-run moves. This pins that generality: one plan builder, three CLI
verbs, no per-kind copy.

Side-effect discipline: in-memory adapters, no store, no network.
"""

from __future__ import annotations

import pytest

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.migration import protocol as P
from kiro_crew.migration.cron_adapter import CronMigrationAdapter
from kiro_crew.migration.move_plan import plan_unit_move
from kiro_crew.migration.taskrun_adapter import TaskRunMigrationAdapter
from kiro_crew.task_models import Project, Task, TaskStatus


def _cron_adapter():
    job = CronJob(
        id="j1",
        name="nightly",
        message="m",
        schedule=CronSchedule(kind="cron", cron_expr="0 3 * * *"),
        agent_id="kirocrew",
    )
    return CronMigrationAdapter(job_lookup={"j1": job})


def _taskrun_adapter():
    proj = Project(
        spec_path="/repo/spec.md",
        spec_content="# plan",
        tasks=[Task(index=0, title="a", description="", status=TaskStatus.PASSED)],
        current_task=0,
        task_id="TASK_abc",
        repo_root="/repo",
    )
    return TaskRunMigrationAdapter(run_lookup={"TASK_abc": proj})


@pytest.mark.asyncio
async def test_plan_unit_move_works_for_cron():
    bundle = await plan_unit_move(_cron_adapter(), "j1", target=P.CrewRef(crew_id="dst"))
    assert bundle.bundle_kind == "cron"
    assert bundle.payload["name"] == "nightly"
    assert any(r.kind == "agent" for r in bundle.requirements)


@pytest.mark.asyncio
async def test_plan_unit_move_works_for_taskrun():
    bundle = await plan_unit_move(_taskrun_adapter(), "TASK_abc", target=P.CrewRef(crew_id="dst"))
    assert bundle.bundle_kind == "taskrun"
    assert bundle.payload["task_id"] == "TASK_abc"
    # the run's repo is a named requirement, not a shipped path
    assert any(r.kind == "git_repo" for r in bundle.requirements)
    assert "repo_root" not in bundle.payload


@pytest.mark.asyncio
async def test_plan_unit_move_carries_bundle_version_from_the_adapter():
    cron = await plan_unit_move(_cron_adapter(), "j1", target=P.CrewRef(crew_id="dst"))
    taskrun = await plan_unit_move(_taskrun_adapter(), "TASK_abc", target=P.CrewRef(crew_id="dst"))
    assert cron.bundle_version == 1 and taskrun.bundle_version == 1


@pytest.mark.asyncio
async def test_plan_unit_move_honours_supplied_source_and_handoff_id():
    bundle = await plan_unit_move(
        _cron_adapter(),
        "j1",
        target=P.CrewRef(crew_id="dst"),
        source=P.CrewRef(crew_id="src"),
        handoff_id="fixed",
    )
    assert bundle.source_crew.crew_id == "src"
    assert bundle.handoff_id == "fixed"


@pytest.mark.asyncio
async def test_plan_unit_move_unknown_unit_raises():
    with pytest.raises(KeyError):
        await plan_unit_move(_cron_adapter(), "missing", target=P.CrewRef(crew_id="dst"))


@pytest.mark.asyncio
async def test_plan_cron_move_still_delegates_to_the_generic_core():
    # the original tested API must keep working unchanged
    from kiro_crew.migration.cron_move import plan_cron_move

    bundle = await plan_cron_move(_cron_adapter(), "j1", target=P.CrewRef(crew_id="dst"))
    assert bundle.bundle_kind == "cron" and bundle.handoff_id
