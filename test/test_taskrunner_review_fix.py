from __future__ import annotations

import json

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
