from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from unittest import mock

import pytest

from kiro_crew.task_models import (
    Project,
    ReviewFixDependencyGroup,
    ReviewFixFindingSnapshot,
    ReviewFixGroupState,
    ReviewFixMetadata,
    ReviewFixModelResolution,
    ReviewFixState,
    ReviewFixTargetMode,
    ReviewFixTargetSnapshot,
)
from kiro_crew.taskrunner import ReviewFixConflict, TaskRunner
from kiro_crew.workflow_memory import TaskSnapshotError


class _Sessions:
    _sessions: dict = {}


@pytest.mark.asyncio
async def test_review_fix_metadata_round_trips_and_survives_restart(tmp_path):
    metadata = ReviewFixMetadata(
        review_run_id="sage-run-1",
        pr_url="https://github.com/example/repo/pull/1",
        source_head_sha="source-sha",
        selected_finding_keys=["finding-red"],
        finding_snapshots=[
            ReviewFixFindingSnapshot(
                key="finding-red",
                title="Use the shared helper",
                severity="red",
                file_path="src/example.py",
                line=7,
            )
        ],
        target=ReviewFixTargetSnapshot(
            mode=ReviewFixTargetMode.CURRENT_BRANCH,
            repo_root=str(tmp_path),
            target_ref="feature/fix",
            head_sha="target-sha",
            dirty_fingerprint="clean-fingerprint",
        ),
        model=ReviewFixModelResolution(
            requested_model="auto",
            provider="acp",
            resolved_model_id="served-model",
            advertised_model_ids=["served-model"],
            resolved_at=1.0,
        ),
        groups=[ReviewFixDependencyGroup(group_id="group-1", finding_keys=["finding-red"])],
    )
    runner = TaskRunner(_Sessions(), work_dir=tmp_path)
    run = await runner.create_review_fix(metadata, task_id="review-fix-1", work_dir=str(tmp_path))

    assert run.execution_mode == "review_fix"
    assert run.commit_policy == "manual_group"
    assert run.review_fix is not None
    assert run.review_fix.state is ReviewFixState.DRAFT

    persisted = json.loads((tmp_path / "runs.json").read_text(encoding="utf-8"))
    assert persisted[0]["review_fix"]["target"]["head_sha"] == "target-sha"
    assert persisted[0]["review_fix"]["groups"][0]["state"] == "proposed"

    restored_before_publish = TaskRunner(_Sessions(), work_dir=tmp_path)
    restored_before_publish._runs.clear()
    restored_before_publish._review_fix_creations_inflight["review-fix-1"] = run
    snapshot = json.loads(restored_before_publish._serialize_runs())
    assert any(row["task_id"] == "review-fix-1" for row in snapshot)

    restored = TaskRunner(_Sessions(), work_dir=tmp_path)
    restored_run = restored.get_review_fix("review-fix-1")
    assert restored_run.revision == 0
    assert restored_run.review_fix is not None
    assert restored_run.review_fix.model.resolved_model_id == "served-model"


@pytest.mark.asyncio
async def test_review_fix_mutation_increments_revision_and_rejects_stale_commands(tmp_path):
    runner = TaskRunner(_Sessions(), work_dir=tmp_path)
    await runner.create_review_fix(
        ReviewFixMetadata(
            target=ReviewFixTargetSnapshot(dirty_fingerprint="fingerprint"),
            groups=[ReviewFixDependencyGroup(group_id="group-1")],
        ),
        task_id="review-fix-2",
    )

    updated = await runner.mutate_review_fix(
        "review-fix-2",
        expected_revision=0,
        action="confirm_grouping",
        expected_state=ReviewFixState.DRAFT,
        to_state=ReviewFixState.PLANNING,
        mutate=lambda metadata: metadata.groups[0].__setattr__("revision", 1),
        expected_target_fingerprint="fingerprint",
    )
    assert updated.revision == 1
    assert updated.review_fix is not None
    assert updated.review_fix.audit_log[-1].action == "confirm_grouping"

    with pytest.raises(ReviewFixConflict) as exc_info:
        await runner.mutate_review_fix(
            "review-fix-2",
            expected_revision=0,
            action="stale",
            mutate=lambda metadata: metadata.logs.append("must-not-apply"),
        )

    assert exc_info.value.code == "stale_task_state"
    review_fix = runner.get_review_fix("review-fix-2").review_fix
    assert review_fix is not None
    assert "must-not-apply" not in review_fix.logs


@pytest.mark.asyncio
async def test_review_fix_group_revision_is_part_of_cas(tmp_path):
    runner = TaskRunner(_Sessions(), work_dir=tmp_path)
    await runner.create_review_fix(
        ReviewFixMetadata(groups=[ReviewFixDependencyGroup(group_id="group-1", revision=3)]),
        task_id="review-fix-3",
    )

    with pytest.raises(ReviewFixConflict):
        await runner.mutate_review_fix(
            "review-fix-3",
            expected_revision=0,
            action="apply_group",
            group_id="group-1",
            expected_group_revision=2,
            mutate=lambda metadata: None,
        )


@pytest.mark.asyncio
async def test_review_fix_mutation_persists_before_publishing_and_leaves_memory_on_failure(
    tmp_path,
):
    """Verify durable publish precedes memory publication and failure rollback."""
    runner = TaskRunner(_Sessions(), work_dir=tmp_path)
    run = await runner.create_review_fix(
        ReviewFixMetadata(
            target=ReviewFixTargetSnapshot(dirty_fingerprint="fingerprint"),
            groups=[ReviewFixDependencyGroup(group_id="group-1")],
        ),
        task_id="review-fix-persist-fail",
    )
    previous_review_fix = run.review_fix
    previous_revision = run.revision

    write_started = asyncio.Event()
    release_write = asyncio.Event()
    seen_inflight: dict = {}

    async def fake_apersist_runs():
        seen_inflight["value"] = dict(runner._review_fix_inflight)
        write_started.set()
        await asyncio.wait_for(release_write.wait(), timeout=5)
        raise OSError("disk full")

    with mock.patch.object(runner, "_apersist_runs", side_effect=fake_apersist_runs):
        task = asyncio.create_task(
            runner.mutate_review_fix(
                "review-fix-persist-fail",
                expected_revision=0,
                action="confirm_grouping",
                mutate=lambda metadata: metadata.groups[0].__setattr__("revision", 1),
            )
        )
        try:
            await asyncio.wait_for(write_started.wait(), timeout=5)

            # While the write is still in flight, a concurrent reader must see
            # the OLD state -- the candidate is not published until the durable
            # write returns successfully.
            mid_write = runner.get_review_fix("review-fix-persist-fail")
            assert mid_write.review_fix is previous_review_fix
            assert mid_write.revision == previous_revision
        finally:
            release_write.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError):
                await asyncio.wait_for(task, timeout=5)

    # The persist call saw the candidate registered as in-flight, not as a
    # mutation of the live run.
    inflight = seen_inflight["value"]
    candidate, revision = inflight["review-fix-persist-fail"]
    assert revision == previous_revision + 1
    assert candidate.groups[0].revision == 1

    run_after = runner.get_review_fix("review-fix-persist-fail")
    assert run_after.review_fix is previous_review_fix
    assert run_after.revision == previous_revision
    # A failed write must still clear the in-flight entry, or every later
    # snapshot would keep resurrecting a candidate that never landed.
    assert "review-fix-persist-fail" not in runner._review_fix_inflight


@pytest.mark.asyncio
async def test_review_fix_creation_failure_leaves_run_unpublished(tmp_path):
    """A failed first snapshot leaves neither the registry nor reservation published."""
    runner = TaskRunner(_Sessions(), work_dir=tmp_path)
    metadata = ReviewFixMetadata(
        target=ReviewFixTargetSnapshot(dirty_fingerprint="fingerprint"),
        groups=[ReviewFixDependencyGroup(group_id="group-1")],
    )

    def fail_first_snapshot(_seq: int, payload: str):
        snapshot = json.loads(payload)
        assert any(row["task_id"] == "review-fix-unpublished" for row in snapshot)
        raise TaskSnapshotError("disk full")

    with mock.patch.object(runner, "_commit_snapshot", side_effect=fail_first_snapshot):
        with pytest.raises(TaskSnapshotError):
            await runner.create_review_fix(metadata, task_id="review-fix-unpublished")

    assert "review-fix-unpublished" not in runner._runs
    assert "review-fix-unpublished" not in runner._review_fix_creations_inflight


@pytest.mark.asyncio
async def test_concurrent_review_fix_creation_reserves_the_id(tmp_path):
    runner = TaskRunner(_Sessions(), work_dir=tmp_path)

    def metadata(name: str) -> ReviewFixMetadata:
        return ReviewFixMetadata(review_run_id=name)

    results = await asyncio.gather(
        runner.create_review_fix(metadata("first"), task_id="review-fix-duplicate"),
        runner.create_review_fix(metadata("second"), task_id="review-fix-duplicate"),
        return_exceptions=True,
    )
    errors = [item for item in results if isinstance(item, BaseException)]
    created = [item for item in results if not isinstance(item, BaseException)]
    run = runner._runs.get("review-fix-duplicate")

    assert len(created) == 1
    assert len(errors) == 1 and "already exists" in str(errors[0])
    assert run is not None and run.review_fix is not None
    assert run.review_fix.review_run_id == "first"
    assert "review-fix-duplicate" not in runner._review_fix_creations_inflight


async def _blocked_review_fix(runner: TaskRunner, task_id: str, work_dir: str = "") -> Project:
    await runner.create_review_fix(
        ReviewFixMetadata(state=ReviewFixState.BLOCKED_DIRTY_OVERLAP),
        task_id=task_id,
        work_dir=work_dir,
    )
    run = runner._runs[task_id]
    run.review_fix.state = ReviewFixState.BLOCKED_DIRTY_OVERLAP
    return run


@pytest.mark.asyncio
async def test_generic_execute_cannot_launch_a_blocked_review_fix(tmp_path):
    runner = TaskRunner(_Sessions(), work_dir=tmp_path)
    run = await _blocked_review_fix(runner, "review-fix-generic", "/real/checkout")
    runner._execute_tasks = mock.AsyncMock()

    with pytest.raises(ValueError, match="execute_review_fix"):
        await runner.execute_plan("review-fix-generic", workspace_dir=str(tmp_path))

    runner._execute_tasks.assert_not_called()
    assert (run.work_dir, run.status) == ("/real/checkout", "planned")
    assert run.review_fix is not None
    assert run.review_fix.state is ReviewFixState.BLOCKED_DIRTY_OVERLAP


@pytest.mark.asyncio
async def test_generic_retry_cannot_launch_a_blocked_review_fix(tmp_path):
    runner = TaskRunner(_Sessions(), work_dir=tmp_path)
    run = await _blocked_review_fix(runner, "review-fix-retry")
    runner._execute_tasks = mock.AsyncMock()

    with pytest.raises(ValueError, match="execute_review_fix"):
        await runner.retry_from_task("review-fix-retry", 1)

    runner._execute_tasks.assert_not_called()
    assert run.status == "planned"
    assert run.review_fix is not None
    assert run.review_fix.state is ReviewFixState.BLOCKED_DIRTY_OVERLAP


@pytest.mark.asyncio
async def test_generic_run_cannot_replace_a_review_fix_id(tmp_path):
    runner = TaskRunner(_Sessions(), work_dir=tmp_path)
    run = await _blocked_review_fix(runner, "review-fix-run")

    with pytest.raises(ValueError, match="execute_review_fix"):
        await runner.run("spec.md", task_id="review-fix-run", input_content="do work\n")

    assert run.execution_mode == "review_fix"
    assert run.review_fix is not None
    assert run.review_fix.state is ReviewFixState.BLOCKED_DIRTY_OVERLAP


@pytest.mark.asyncio
async def test_review_fix_mutation_publishes_only_after_persist_succeeds(tmp_path):
    runner = TaskRunner(_Sessions(), work_dir=tmp_path)
    await runner.create_review_fix(
        ReviewFixMetadata(
            target=ReviewFixTargetSnapshot(dirty_fingerprint="fingerprint"),
            groups=[ReviewFixDependencyGroup(group_id="group-1")],
        ),
        task_id="review-fix-persist-success",
    )

    updated = await runner.mutate_review_fix(
        "review-fix-persist-success",
        expected_revision=0,
        action="confirm_grouping",
        mutate=lambda metadata: metadata.logs.append("applied"),
    )

    assert updated.revision == 1
    run_after = runner.get_review_fix("review-fix-persist-success")
    assert run_after.review_fix is updated.review_fix
    assert run_after.revision == 1

    persisted = json.loads((tmp_path / "runs.json").read_text(encoding="utf-8"))
    persisted_run = next(r for r in persisted if r["task_id"] == "review-fix-persist-success")
    assert persisted_run["revision"] == 1
    assert persisted_run["review_fix"]["logs"] == ["applied"]


@pytest.mark.asyncio
async def test_generic_project_persistence_has_no_review_fix_payload(tmp_path):
    runner = TaskRunner(_Sessions(), work_dir=tmp_path)
    runner._runs["generic-1"] = Project(
        task_id="generic-1", spec_path="spec.md", spec_content="", status="planned"
    )
    runner._persist_runs()

    persisted = json.loads((tmp_path / "runs.json").read_text(encoding="utf-8"))
    assert "review_fix" not in persisted[0]
    restored = TaskRunner(_Sessions(), work_dir=tmp_path)
    assert restored._runs["generic-1"].review_fix is None


@pytest.mark.asyncio
async def test_ready_to_apply_transitions_to_awaiting_commit(tmp_path):
    runner = TaskRunner(_Sessions(), work_dir=tmp_path)
    await runner.create_review_fix(
        ReviewFixMetadata(
            target=ReviewFixTargetSnapshot(dirty_fingerprint="fingerprint"),
            groups=[
                ReviewFixDependencyGroup(
                    group_id="group-1",
                    finding_keys=["finding-1"],
                    state=ReviewFixGroupState.READY_TO_APPLY,
                )
            ],
        ),
        task_id="review-fix-apply-transition",
    )
    run = runner.get_review_fix("review-fix-apply-transition")
    assert run.review_fix is not None
    run.review_fix.state = ReviewFixState.READY_TO_APPLY
    run.review_fix.revision = 0
    run.revision = 0

    updated = await runner.mutate_review_fix(
        "review-fix-apply-transition",
        expected_revision=0,
        expected_group_revision=0,
        expected_state=ReviewFixState.READY_TO_APPLY,
        expected_target_fingerprint="fingerprint",
        group_id="group-1",
        action="apply_group",
        to_state=ReviewFixState.AWAITING_COMMIT,
        mutate=lambda metadata: setattr(metadata.groups[0], "state", ReviewFixGroupState.APPLIED),
    )

    assert updated.review_fix is not None
    assert updated.review_fix.state is ReviewFixState.AWAITING_COMMIT
    assert updated.review_fix.groups[0].state is ReviewFixGroupState.APPLIED


@pytest.mark.asyncio
async def test_failed_start_restores_the_prior_state(tmp_path, monkeypatch):
    """execute_review_fix transitions to RUNNING before it can launch; if the
    launch itself fails there is no background task left to move the run on, so
    the start must be rolled back rather than stranding a fake RUNNING state."""
    runner = TaskRunner(_Sessions(), work_dir=tmp_path)
    await runner.create_review_fix(
        ReviewFixMetadata(
            target=ReviewFixTargetSnapshot(dirty_fingerprint="fingerprint"),
            model=ReviewFixModelResolution(resolved_model_id="served-model"),
            groups=[
                ReviewFixDependencyGroup(
                    group_id="group-1",
                    state=ReviewFixGroupState.CONFIRMED,
                )
            ],
        ),
        task_id="review-fix-failed-start",
    )
    run = runner.get_review_fix("review-fix-failed-start")
    assert run.review_fix is not None
    run.review_fix.state = ReviewFixState.AWAITING_GROUP_CONFIRMATION
    run.review_fix.revision = 0
    run.revision = 0

    async def exploding_plan(*_args, **_kwargs):
        raise ValueError("planner rejected the spec")

    monkeypatch.setattr(runner, "execute_plan", exploding_plan)

    with pytest.raises(ValueError, match="planner rejected the spec"):
        await runner.execute_review_fix("review-fix-failed-start")

    restored = runner.get_review_fix("review-fix-failed-start")
    assert restored.review_fix is not None
    assert restored.review_fix.state is ReviewFixState.AWAITING_GROUP_CONFIRMATION
    assert restored.review_fix.audit_log[-1].action == "start_rolled_back"

    # The rolled-back run is not bricked: it can be started again.
    async def working_plan(*_args, **_kwargs):
        return "execution-task-1"

    monkeypatch.setattr(runner, "execute_plan", working_plan)
    execution_task_id = await runner.execute_review_fix("review-fix-failed-start")
    assert execution_task_id == "execution-task-1"
    started = runner.get_review_fix("review-fix-failed-start").review_fix
    assert started is not None
    assert started.state is ReviewFixState.RUNNING


@pytest.mark.asyncio
async def test_review_fix_concurrent_mutations_same_revision_one_wins(tmp_path):
    """A per-task_id lock must make the CAS check atomic with persist+publish.

    Without it, two concurrent mutations can both read revision 0, both pass
    their CAS check, and both publish -- a lost update, with neither call
    ever observing a conflict.
    """
    runner = TaskRunner(_Sessions(), work_dir=tmp_path)
    await runner.create_review_fix(
        ReviewFixMetadata(
            target=ReviewFixTargetSnapshot(dirty_fingerprint="fingerprint"),
            groups=[ReviewFixDependencyGroup(group_id="group-1")],
        ),
        task_id="review-fix-concurrent",
    )

    async def attempt(tag: str):
        return await runner.mutate_review_fix(
            "review-fix-concurrent",
            expected_revision=0,
            action=f"confirm_{tag}",
            mutate=lambda metadata, tag=tag: metadata.logs.append(tag),
        )

    results = await asyncio.gather(attempt("a"), attempt("b"), return_exceptions=True)
    successes = [r for r in results if isinstance(r, Project)]
    conflicts = [r for r in results if isinstance(r, ReviewFixConflict)]
    assert len(successes) == 1
    assert len(conflicts) == 1

    final = runner.get_review_fix("review-fix-concurrent")
    assert final.revision == 1
    assert final.review_fix is not None
    assert len(final.review_fix.logs) == 1


@pytest.mark.asyncio
async def test_review_fix_mutation_candidate_survives_unrelated_concurrent_persist(tmp_path):
    runner = TaskRunner(_Sessions(), work_dir=tmp_path)
    await runner.create_review_fix(
        ReviewFixMetadata(
            target=ReviewFixTargetSnapshot(dirty_fingerprint="fingerprint"),
            groups=[ReviewFixDependencyGroup(group_id="group-1")],
        ),
        task_id="review-fix-race",
    )
    runner._runs["generic-race"] = Project(
        task_id="generic-race", spec_path="spec.md", spec_content="", status="planned"
    )

    original_commit_snapshot = runner._commit_snapshot
    gate = threading.Event()
    started = threading.Event()
    held_seq: dict = {}

    def gated_commit_snapshot(seq, payload):
        if "seq" not in held_seq:
            held_seq["seq"] = seq
            started.set()
            gate.wait(timeout=5)
        return original_commit_snapshot(seq, payload)

    with mock.patch.object(runner, "_commit_snapshot", side_effect=gated_commit_snapshot):
        mutate_task = asyncio.create_task(
            runner.mutate_review_fix(
                "review-fix-race",
                expected_revision=0,
                action="confirm_grouping",
                mutate=lambda metadata: metadata.groups[0].__setattr__("revision", 1),
            )
        )
        await asyncio.to_thread(started.wait, 5)
        await runner._apersist_runs()  # unrelated write; wins with a higher seq
        gate.set()
        await mutate_task

    persisted = json.loads((tmp_path / "runs.json").read_text(encoding="utf-8"))
    persisted_run = next(r for r in persisted if r["task_id"] == "review-fix-race")
    assert persisted_run["review_fix"]["groups"][0]["revision"] == 1
