"""Slice 5 (circle 2) — two-crew loopback integration (issue #7577, Task 5.3).

End-to-end with the REAL parts, no fakes for the machinery under test: a
source CronMigrationAdapter + plan_cron_move build a bundle; the real
MigrationCoordinator drives the five steps; a real durable LocalMigrationReceiver
persists and acks; a target CronMigrationAdapter.materialize creates the job on
the "other crew". The tunnel is represented by a same-process loopback (direct
awaits), which is the honest local stand-in Task 5.3 calls for.

Covers: cron happy path (source released, target owns, job re-created with
re-bound scope), and an unreachable target (source untouched).

Side-effect discipline: receiver store under tmp_path; jobs live in in-memory
dicts; no crons.json, no network.
"""

from __future__ import annotations

import pytest

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.migration import protocol as P
from kiro_crew.migration.cron_adapter import CronMigrationAdapter
from kiro_crew.migration.receiver import LocalMigrationReceiver, RequirementProbe


def _job():
    return CronJob(
        id="j1",
        name="nightly",
        message="run backup",
        schedule=CronSchedule(kind="cron", cron_expr="0 3 * * *"),
        agent_id="kirocrew",
        timezone="America/New_York",
        user_paused=True,
        session_key="src-session",
    )


def _target_crew_setup(tmp_path):
    """Build the target side using the REAL CronMigrationAdapter.materialize.

    An earlier version of this helper mirrored the adapter's re-bind logic in a
    local function. That made the test assert against its own copy of the
    behaviour: a mutation sweep removing the adapter's session_key re-bind left
    this test green. Passing the adapter's own bound method fixes that -- the
    receiver awaits it, so no sync wrapper is needed either.
    """
    created: dict = {}

    def create_job(fields):
        jid = "remote-j1"
        created[jid] = fields
        return jid

    target_adapter = CronMigrationAdapter(create_job=create_job, target_session_key="dst-session")
    # A real probe, because preflight no longer fails open: an unverifiable
    # requirement is now a finding at the requirement's own severity. This models
    # a target that GENUINELY has the job's agent — narrowly, so a job wanting
    # any other agent would be refused here rather than waved through. Before,
    # this end-to-end path only reached `migrated` because nothing was checked.
    probe = RequirementProbe(
        agent_exists=lambda name: name == "kirocrew",
        script_path_ok=lambda p: False,
        command_allowed=lambda c: False,
    )
    receiver = LocalMigrationReceiver(
        store_dir=tmp_path, materialize=target_adapter.materialize, requirement_probe=probe
    )
    return receiver, created


@pytest.mark.asyncio
async def test_cron_migrates_end_to_end_source_released_target_owns(tmp_path):
    src_job = _job()
    source_adapter = CronMigrationAdapter(job_lookup={"j1": src_job})
    receiver, created = _target_crew_setup(tmp_path)

    coord = P.MigrationCoordinator(
        adapter=source_adapter,
        receiver=receiver,
        source_crew=P.CrewRef(crew_id="local"),
        target_crew=P.CrewRef(crew_id="remote-ec2"),
    )

    result = await coord.migrate("j1")

    assert result.outcome == "migrated", f"reason={result.reason!r}"
    assert result.remote_unit_id == "remote-j1"
    # source released: job retained but non-executing, tombstone recorded
    assert src_job.enabled is False
    assert source_adapter.tombstone_of("j1").target_crew.crew_id == "remote-ec2"
    # target owns: job re-created, scope re-bound, user_paused + tz preserved
    assert created["remote-j1"]["session_key"] == "dst-session"
    assert created["remote-j1"]["user_paused"] is True
    assert created["remote-j1"]["timezone"] == "America/New_York"
    # durable ack survives on the receiver's store
    assert any(receiver._dir.iterdir())


@pytest.mark.asyncio
async def test_unreachable_target_leaves_source_untouched(tmp_path):
    src_job = _job()
    source_adapter = CronMigrationAdapter(job_lookup={"j1": src_job})

    class Unreachable(P.MigrationReceiver):
        async def preflight(self, bundle):
            raise ConnectionError("target crew unreachable")

        async def accept(self, bundle):
            raise ConnectionError("target crew unreachable")

    coord = P.MigrationCoordinator(
        adapter=source_adapter,
        receiver=Unreachable(),
        source_crew=P.CrewRef(crew_id="local"),
        target_crew=P.CrewRef(crew_id="remote-ec2"),
    )

    result = await coord.migrate("j1")

    assert result.outcome == "refused"  # refused at preflight
    assert src_job.enabled is True  # never quiesced — source intact


# ------------------------------ session + task-run happy paths (Task 5.3 tail)


@pytest.mark.asyncio
async def test_session_migrates_end_to_end_with_real_adapter(tmp_path):
    """Real SessionMigrationAdapter -> coordinator -> durable receiver."""
    from kiro_crew.migration.session_adapter import build_session_adapter

    class Slot:
        def __init__(self):
            self.accepting = True
            self.in_flight = False
            self.transcript_readable = True
            self.new_home = None

        def block_new_turns(self):
            self.accepting = False

        def allow_new_turns(self):
            self.accepting = True

        def drain_in_flight(self, timeout):
            return True

    slot = Slot()
    imported: dict = {}

    def importer(payload):
        imported.update(payload)
        return "remote-sess-9"

    source = build_session_adapter(
        session_id="sess-1",
        controller=slot,
        bundle_builder=lambda sid: {
            "transcript": ["hi"],
            "layer_b": {"sid": sid},
            "project": "/Users/alice/wt",  # non-portable
            "ledger": {
                "goal": "ship it",
                "phase": "implementing",
                "next": "wire tunnel",
                "tried": [],
                "artifacts": {},
            },
        },
        importer=importer,
    )

    receiver = LocalMigrationReceiver(store_dir=tmp_path, materialize=importer)
    coord = P.MigrationCoordinator(
        adapter=source,
        receiver=receiver,
        source_crew=P.CrewRef(crew_id="local"),
        target_crew=P.CrewRef(crew_id="remote-ec2"),
    )

    result = await coord.migrate("sess-1")

    assert result.outcome == "migrated"
    assert result.remote_unit_id == "remote-sess-9"
    # source released: refuses new turns, transcript still readable, names target
    assert slot.accepting is False
    assert slot.transcript_readable is True
    assert slot.new_home.target_crew.crew_id == "remote-ec2"
    # target got the ledger working state; the Mac path did not travel
    assert imported["ledger"]["goal"] == "ship it"
    assert "project" not in imported
    # and the drop was REPORTED, not swallowed
    assert any(f.detail_key == "project" for f in source.last_findings)


@pytest.mark.asyncio
async def test_taskrun_resume_path_migrates_without_reexecuting_done_tasks(tmp_path):
    """Real TaskRunMigrationAdapter on the resume path, end to end."""
    from kiro_crew.migration.taskrun_adapter import (
        GitReproProbe,
        TaskRunMigrationAdapter,
        classify_resume_or_restart,
        remaining_tasks_after_resume,
    )
    from kiro_crew.task_models import Project, Task, TaskStatus

    proj = Project(
        spec_path="/repo/.kiro/specs/x/tasks.md",
        spec_content="# plan",
        tasks=[
            Task(index=0, title="done", description="", status=TaskStatus.PASSED),
            Task(index=1, title="todo", description="", status=TaskStatus.PENDING),
        ],
        current_task=1,
        task_id="TASK_abc",
        repo_root="/repo",
        branch_name="feat/x",
        worktree_path="/wt/x",
    )

    # preflight classifies resume: all three git references reproducible
    report = classify_resume_or_restart(
        {"repo_root": "/repo", "branch_name": "feat/x", "worktree_path": "/wt/x"},
        GitReproProbe(
            repo_root_resolves=lambda r: True,
            branch_reachable=lambda b: True,
            worktree_recreatable=lambda w: True,
        ),
    )
    assert report.resume_class == "resume"

    created: dict = {}

    def create_run(payload):
        created.update(payload)
        return "remote-TASK_abc"

    source = TaskRunMigrationAdapter(run_lookup={"TASK_abc": proj})
    # git_repo is a BLOCKING requirement the probe previously had no check for,
    # so it used to be admitted unchecked. Satisfied narrowly for this run's own
    # repo — matching the adapter's `repo_root_resolves` above, which already
    # models "this checkout exists on the target".
    receiver = LocalMigrationReceiver(
        store_dir=tmp_path,
        materialize=create_run,
        requirement_probe=RequirementProbe(
            agent_exists=lambda name: False,
            script_path_ok=lambda p: False,
            command_allowed=lambda c: False,
            git_repo_ok=lambda r: r == "/repo",
        ),
    )
    coord = P.MigrationCoordinator(
        adapter=source,
        receiver=receiver,
        source_crew=P.CrewRef(crew_id="local"),
        target_crew=P.CrewRef(crew_id="remote-ec2"),
    )

    result = await coord.migrate("TASK_abc")

    assert result.outcome == "migrated"
    assert source.is_resumable_in_place("TASK_abc") is False  # released
    # the completed task travelled as complete, so the target will not re-run it
    statuses = {t["index"]: t["status"] for t in created["tasks"]}
    assert statuses[0] == "passed"
    assert [t.index for t in remaining_tasks_after_resume(proj)] == [1]
    # host-local git paths did NOT travel
    assert "worktree_path" not in created and "repo_root" not in created


@pytest.mark.asyncio
async def test_restart_classified_run_is_refused_without_confirmation(tmp_path):
    """A restart discards work, so it must never proceed silently (Req 6.4)."""
    from kiro_crew.migration.taskrun_adapter import (
        GitReproProbe,
        RestartNotConfirmed,
        classify_resume_or_restart,
        require_restart_confirmation,
    )
    from kiro_crew.task_models import Project, Task, TaskStatus

    proj = Project(
        spec_path="/repo/spec.md",
        spec_content="# plan",
        tasks=[
            Task(index=0, title="alpha", description="", status=TaskStatus.PASSED),
            Task(index=1, title="beta", description="", status=TaskStatus.PENDING),
        ],
        current_task=1,
        task_id="TASK_r",
        repo_root="/gone",
        branch_name="feat/x",
        worktree_path="/wt/x",
    )

    # the target cannot resolve repo_root -> restart, with the reason named
    report = classify_resume_or_restart(
        {"repo_root": "/gone", "branch_name": "feat/x", "worktree_path": "/wt/x"},
        GitReproProbe(
            repo_root_resolves=lambda r: False,
            branch_reachable=lambda b: True,
            worktree_recreatable=lambda w: True,
        ),
    )
    assert report.resume_class == "restart"
    assert any(f.detail_key == "repo_root" for f in report.findings)

    # and the gate refuses until the user confirms, naming what is discarded
    with pytest.raises(RestartNotConfirmed) as exc:
        require_restart_confirmation(proj, confirmed=False)
    assert "alpha" in str(exc.value)

    desc = require_restart_confirmation(proj, confirmed=True)
    assert desc["completed_count"] == 1
