from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from review_fix_helpers import _repo, unsandboxed_git  # noqa: F401  (autouse fixture)

from kiro_crew import review_fix_git
from kiro_crew.apps.builtins.code_review_sage.backend import fix_tasks
from kiro_crew.review_fix import (
    _MAX_AFFECTED_FILES,
    _MAX_EDGE_FIELDS,
    _MAX_GROUP_EDGES,
    _MAX_GROUP_REASONS,
    _MAX_PATH_CHARS,
    ReviewFixModelResolutionError,
    ReviewFixPlanError,
    _bound_edge,
    build_review_fix_groups,
    build_review_fix_tasks,
    create_review_fix_task,
    resolve_pinned_model,
    validate_group,
)
from kiro_crew.review_fix_git import ReviewFixGitError, ReviewFixPatch, discard_candidate
from kiro_crew.task_models import (
    ReviewFixDependencyGroup,
    ReviewFixFindingSnapshot,
    ReviewFixGitRecord,
    ReviewFixGroupState,
    ReviewFixMetadata,
    ReviewFixState,
    ReviewFixTargetSnapshot,
)
from kiro_crew.taskrunner import TaskRunner


class _Sessions:
    _sessions: dict = {}


def test_resolve_pinned_model_requires_concrete_advertised_id():
    resolved = resolve_pinned_model(
        "served-model", ["served-model"], provider="acp", resolved_at=2.0
    )
    assert resolved.resolved_model_id == "served-model"
    assert resolved.advertised_model_ids == ["served-model"]
    assert resolved.resolved_at == 2.0

    for requested, advertised in (
        ("auto", ["served-model"]),
        ("", ["served-model"]),
        ("other", ["served-model"]),
    ):
        with pytest.raises(ReviewFixModelResolutionError):
            resolve_pinned_model(requested, advertised)


def test_resolve_pinned_model_allows_valid_id_with_unknown_catalogue():
    # Empty advertised means unknown catalogue; the concrete id is still checked.
    resolved = resolve_pinned_model("served-model", [], provider="acp")
    assert resolved.resolved_model_id == "served-model"
    assert resolved.advertised_model_ids == []


def test_grouping_requires_exact_finding_coverage():
    findings = [
        ReviewFixFindingSnapshot(key="red", file_path="a.py"),
        ReviewFixFindingSnapshot(key="yellow", file_path="b.py"),
    ]
    groups = build_review_fix_groups(
        findings,
        [{"group_id": "hard-1", "finding_keys": ["red", "yellow"], "hard": True}],
    )
    assert groups[0].hard is True
    assert groups[0].affected_files == ["a.py", "b.py"]

    with pytest.raises(ReviewFixPlanError):
        build_review_fix_groups(findings, [{"finding_keys": ["red"]}])


def test_normal_group_reasons_and_edges_are_unchanged():
    findings = [ReviewFixFindingSnapshot(key="red", file_path="a.py")]
    raw_group = {
        "group_id": "hard-1",
        "finding_keys": ["red"],
        "reasons": ["shared helper", "same module"],
        "hard_edges": [{"from": "hard-1", "to": "hard-2", "reason": "shared symbol"}],
        "soft_edges": [{"from": "hard-1", "to": "hard-3", "reason": "same file"}],
    }

    group = build_review_fix_groups(findings, [raw_group])[0]

    assert group.reasons == ["shared helper", "same module"]
    assert group.hard_edges == [{"from": "hard-1", "to": "hard-2", "reason": "shared symbol"}]
    assert group.soft_edges == [{"from": "hard-1", "to": "hard-3", "reason": "same file"}]


def test_edge_retention_has_a_field_count_bound():
    with pytest.raises(ReviewFixPlanError, match="too many fields"):
        _bound_edge({str(index): "x" for index in range(_MAX_EDGE_FIELDS + 1)})


def test_over_long_reason_is_truncated_not_rejected():
    findings = [ReviewFixFindingSnapshot(key="red", file_path="a.py")]
    from kiro_crew.review_fix import _MAX_REASON_CHARS

    group = build_review_fix_groups(
        findings,
        [{"group_id": "g-1", "finding_keys": ["red"], "reasons": ["x" * (_MAX_REASON_CHARS + 50)]}],
    )[0]

    assert len(group.reasons[0]) == _MAX_REASON_CHARS


@pytest.mark.parametrize(
    "field,make_value,expected_code",
    [
        ("reasons", lambda: ["r"] * (_MAX_GROUP_REASONS + 1), "too_many_reasons"),
        ("hard_edges", lambda: [{"reason": "x"}] * (_MAX_GROUP_EDGES + 1), "too_many_edges"),
        (
            "affected_files",
            lambda: [f"f{i}.py" for i in range(_MAX_AFFECTED_FILES + 1)],
            "too_many_affected_files",
        ),
        (
            "affected_files",
            # A truncated path would silently rename owned work.
            lambda: ["x" * (_MAX_PATH_CHARS + 1)],
            "path_too_long",
        ),
    ],
    ids=["too-many-reasons", "too-many-edges", "too-many-affected-files", "path-too-long"],
)
def test_group_bounds_are_rejected_not_silently_truncated(field, make_value, expected_code):
    findings = [ReviewFixFindingSnapshot(key="red", file_path="a.py")]

    with pytest.raises(ReviewFixPlanError) as excinfo:
        build_review_fix_groups(
            findings,
            [{"group_id": "g-1", "finding_keys": ["red"], field: make_value()}],
        )
    assert excinfo.value.code == expected_code


@pytest.mark.parametrize("group_id", ["../../evil", "/etc/passwd", "group id", ".hidden", "a" * 65])
def test_group_id_becomes_a_filename_so_traversal_is_rejected(group_id):
    findings = [ReviewFixFindingSnapshot(key="red", file_path="a.py")]

    with pytest.raises(ReviewFixPlanError, match="group id is invalid"):
        build_review_fix_groups(findings, [{"group_id": group_id, "finding_keys": ["red"]}])


def test_generated_group_ids_are_always_safe():
    findings = [
        ReviewFixFindingSnapshot(key="red", file_path="a.py"),
        ReviewFixFindingSnapshot(key="yellow", file_path="b.py"),
    ]

    groups = build_review_fix_groups(findings, None)

    assert [group.group_id for group in groups] == ["group-1", "group-2"]


def test_default_groups_own_a_file_once_so_patches_cannot_overlap():
    # One file per default group prevents duplicate whole-file diffs.
    findings = [
        ReviewFixFindingSnapshot(key="a1", file_path="a.py"),
        ReviewFixFindingSnapshot(key="b1", file_path="b.py"),
        ReviewFixFindingSnapshot(key="a2", file_path="a.py"),
    ]

    groups = build_review_fix_groups(findings, None)

    assert [(group.group_id, group.finding_keys, group.affected_files) for group in groups] == [
        ("group-1", ["a1", "a2"], ["a.py"]),
        ("group-2", ["b1"], ["b.py"]),
    ]


@pytest.mark.parametrize("raw_hard, expected", [("false", False), ("true", False), (True, True)])
def test_hard_flag_locks_only_on_a_json_boolean(raw_hard, expected):
    # A JSON "false" string must not lock a group.
    findings = [
        ReviewFixFindingSnapshot(key="red", file_path="a.py"),
        ReviewFixFindingSnapshot(key="yellow", file_path="b.py"),
    ]

    group = build_review_fix_groups(
        findings,
        [{"group_id": "hard-1", "finding_keys": ["red", "yellow"], "hard": raw_hard}],
    )[0]

    assert group.hard is expected


def test_fileless_finding_cannot_form_an_auto_group():
    # A fileless finding would create an unscoped whole-worktree patch.
    findings = [
        ReviewFixFindingSnapshot(key="red", file_path="a.py"),
        ReviewFixFindingSnapshot(key="ghost", file_path=""),
    ]

    with pytest.raises(ReviewFixPlanError, match="ghost.*no file_path") as excinfo:
        build_review_fix_groups(findings, None)
    assert excinfo.value.code == "fileless_finding"


def test_raw_group_with_no_owned_files_is_rejected():
    findings = [ReviewFixFindingSnapshot(key="ghost", file_path="")]

    with pytest.raises(ReviewFixPlanError, match="owns no files") as excinfo:
        build_review_fix_groups(findings, [{"finding_keys": ["ghost"]}])
    assert excinfo.value.code == "fileless_group"


def test_groups_claiming_the_same_file_are_rejected():
    # Overlapping owners would apply another group's unapproved edits.
    findings = [
        ReviewFixFindingSnapshot(key="red", file_path="a.py"),
        ReviewFixFindingSnapshot(key="yellow", file_path="b.py"),
        ReviewFixFindingSnapshot(key="green", file_path="c.py"),
    ]

    with pytest.raises(ReviewFixPlanError, match="both own.*b\\.py"):
        build_review_fix_groups(
            findings,
            [
                {"group_id": "g-1", "finding_keys": ["red"], "affected_files": ["a.py", "b.py"]},
                {"group_id": "g-2", "finding_keys": ["yellow"], "affected_files": ["b.py"]},
                {"group_id": "g-3", "finding_keys": ["green"], "affected_files": ["c.py"]},
            ],
        )


def test_soft_grouping_edit_replacement_is_rejected_when_files_overlap():
    # Regrouping uses this builder, so it cannot reintroduce file overlap.
    findings = [
        ReviewFixFindingSnapshot(key="red", file_path="a.py"),
        ReviewFixFindingSnapshot(key="yellow", file_path="b.py"),
    ]

    with pytest.raises(ReviewFixPlanError, match="both own.*b\\.py"):
        build_review_fix_groups(
            findings,
            [
                {"group_id": "s-1", "finding_keys": ["red"], "affected_files": ["a.py", "b.py"]},
                {"group_id": "s-2", "finding_keys": ["yellow"], "affected_files": ["b.py"]},
            ],
        )


def test_tasks_serialize_sharing_resources():
    findings = [
        ReviewFixFindingSnapshot(key="a1", file_path="a.py"),
        ReviewFixFindingSnapshot(key="b1", file_path="b.py"),
        ReviewFixFindingSnapshot(key="a2", file_path="a.py"),
    ]
    groups = build_review_fix_groups(findings, None)

    tasks = build_review_fix_tasks(findings, groups)
    by_index = {task.index: task for task in tasks}

    assert by_index[1].depends_on == []
    assert by_index[2].depends_on == []
    assert by_index[3].depends_on == [1]
    assert all(dep < task.index for task in tasks for dep in task.depends_on)


def test_tasks_in_one_group_serialize_even_across_files():
    findings = [
        ReviewFixFindingSnapshot(key="a1", file_path="a.py"),
        ReviewFixFindingSnapshot(key="b1", file_path="b.py"),
    ]
    groups = build_review_fix_groups(
        findings,
        [{"group_id": "hard-1", "finding_keys": ["a1", "b1"], "hard": True}],
    )

    tasks = build_review_fix_tasks(findings, groups)
    by_index = {task.index: task for task in tasks}

    assert by_index[1].depends_on == []
    assert by_index[2].depends_on == [1]


def test_task_deps_are_transitive_safe_for_multi_group_files():
    findings = [
        ReviewFixFindingSnapshot(key="a1", file_path="a.py"),
        ReviewFixFindingSnapshot(key="b1", file_path="b.py"),
        ReviewFixFindingSnapshot(key="a2", file_path="a.py"),
        ReviewFixFindingSnapshot(key="a3", file_path="a.py"),
    ]
    groups = build_review_fix_groups(findings, None)

    tasks = build_review_fix_tasks(findings, groups)

    by_index = {task.index: task for task in tasks}
    assert by_index[3].depends_on == [1]
    assert by_index[4].depends_on == [3]


@pytest.mark.asyncio
async def test_create_review_fix_task_persists_candidate_and_waits_for_group_confirmation(tmp_path):
    repo = _repo(tmp_path)
    runner = TaskRunner(_Sessions(), work_dir=tmp_path / "runner")
    run = await create_review_fix_task(
        runner,
        target_path=repo,
        findings=[{"key": "red", "title": "Fix target", "path": "target.txt", "body": "change it"}],
        review_run_id="sage-1",
        pr_url="https://github.com/example/repo/pull/1",
        requested_model="served-model",
        advertised_model_ids=["served-model"],
    )

    assert run.execution_mode == "review_fix"
    assert run.review_fix is not None
    assert run.review_fix.state is ReviewFixState.AWAITING_GROUP_CONFIRMATION
    assert run.review_fix.git.candidate_worktree_path
    assert (tmp_path / "repo" / "target.txt").read_text(encoding="utf-8") == "before\n"
    await discard_candidate(run.review_fix.git, run.review_fix.target.repo_root)


@pytest.mark.asyncio
async def test_create_review_fix_task_blocks_dirty_overlap(tmp_path):
    repo = _repo(tmp_path)
    (repo / "target.txt").write_text("local\n", encoding="utf-8")
    runner = TaskRunner(_Sessions(), work_dir=tmp_path / "runner")
    run = await create_review_fix_task(
        runner,
        target_path=repo,
        findings=[{"key": "red", "path": "target.txt", "body": "change it"}],
        requested_model="served-model",
        advertised_model_ids=["served-model"],
    )
    assert run.review_fix is not None
    assert run.review_fix.state is ReviewFixState.BLOCKED_DIRTY_OVERLAP
    await discard_candidate(run.review_fix.git, run.review_fix.target.repo_root)


@pytest.mark.asyncio
async def test_create_review_fix_task_blocks_dirty_overlap_before_model_resolution(tmp_path):
    """A dirty owned file stays blocked even when the requested model is unusable."""
    repo = _repo(tmp_path)
    (repo / "target.txt").write_text("local\n", encoding="utf-8")
    runner = TaskRunner(_Sessions(), work_dir=tmp_path / "runner")
    run = await create_review_fix_task(
        runner,
        target_path=repo,
        findings=[{"key": "red", "path": "target.txt", "body": "change it"}],
        requested_model="unserved-model",
        advertised_model_ids=["served-model"],
    )
    assert run.review_fix is not None
    assert run.review_fix.state is ReviewFixState.BLOCKED_DIRTY_OVERLAP
    await discard_candidate(run.review_fix.git, run.review_fix.target.repo_root)


@pytest.mark.asyncio
async def test_resolve_model_cannot_unlock_a_dirty_owned_target(tmp_path, monkeypatch):
    """Model resolution cannot clear a dirty-overlap block."""
    runner, candidate = await _validation_runner(tmp_path, "review-fix-model-dirty", captured=False)
    run = runner.get_review_fix("review-fix-model-dirty")
    metadata = run.review_fix
    assert metadata is not None
    metadata.state = ReviewFixState.BLOCKED_MODEL_RESOLUTION
    metadata.blocked_reason = "unserved model"
    metadata.target = ReviewFixTargetSnapshot(
        repo_root=str(tmp_path),
        target_path=str(tmp_path),
        dirty_fingerprint="fingerprint",
    )

    async def same_target(*_args, **_kwargs):
        return metadata.target

    monkeypatch.setattr(review_fix_git, "inspect_target", same_target)
    monkeypatch.setattr(review_fix_git, "dirty_overlap", lambda _target, _paths: ["target.txt"])

    updated = await fix_tasks._action(
        SimpleNamespace(headers={}, query={}, match_info={}),
        runner,
        run,
        "resolve_model",
        {"model": "served-model", "advertised_model_ids": ["served-model"]},
        0,
        "fingerprint",
    )

    assert updated.review_fix is not None
    assert updated.review_fix.state is ReviewFixState.BLOCKED_DIRTY_OVERLAP


@pytest.mark.asyncio
async def test_resolve_model_git_error_fails_closed_without_a_500(tmp_path, monkeypatch):
    """A failed target re-inspection returns 409 and leaves the block intact."""
    runner, _candidate = await _validation_runner(tmp_path, "review-fix-model-git", captured=False)
    run = runner.get_review_fix("review-fix-model-git")
    metadata = run.review_fix
    assert metadata is not None
    metadata.state = ReviewFixState.BLOCKED_MODEL_RESOLUTION
    metadata.target = ReviewFixTargetSnapshot(
        repo_root=str(tmp_path),
        target_path=str(tmp_path),
        dirty_fingerprint="fingerprint",
    )

    async def broken_inspect(*_args, **_kwargs):
        raise ReviewFixGitError("git inspection failed")

    monkeypatch.setattr(fix_tasks, "is_app_enabled", lambda _name: True)
    monkeypatch.setattr(review_fix_git, "inspect_target", broken_inspect)
    app = web.Application()
    app["state"] = SimpleNamespace(task_runner=runner, owner_id="owner")
    request = make_mocked_request(
        "POST",
        "/api/taskrunner/review-fix-model-git/review-fix/actions",
        app=app,
        match_info={"task_id": "review-fix-model-git"},
    )
    request["app"] = ""
    request["user"] = "owner"
    request.json = AsyncMock(
        return_value={
            "action": "resolve_model",
            "expected_revision": 0,
            "target_fingerprint": "fingerprint",
            "confirmation_id": "confirm-model",
            "model": "served-model",
            "advertised_model_ids": ["served-model"],
        }
    )

    response = await fix_tasks.handle_fix_action(request)

    assert response.status == 409
    payload = json.loads(bytes(response.body))
    assert payload["code"] == "review_fix_action_failed"
    assert "git inspection failed" in payload["error"]
    assert metadata.state is ReviewFixState.BLOCKED_MODEL_RESOLUTION
    assert run.revision == 0
    assert metadata.revision == 0


def _validation_task_metadata(*, candidate: Path, captured: bool = True) -> ReviewFixMetadata:
    return ReviewFixMetadata(
        state=ReviewFixState.AWAITING_VALIDATION,
        target=ReviewFixTargetSnapshot(dirty_fingerprint="fingerprint"),
        git=ReviewFixGitRecord(candidate_worktree_path=str(candidate)),
        groups=[
            ReviewFixDependencyGroup(
                group_id="group-1",
                finding_keys=["finding-1"],
                candidate_patch_id="captured-patch" if captured else "",
            )
        ],
    )


async def _validation_runner(
    tmp_path: Path, task_id: str, *, captured: bool = True
) -> tuple[TaskRunner, Path]:
    """Shared single-group AWAITING_VALIDATION harness for the validate_group tests below."""
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    runner = TaskRunner(_Sessions(), work_dir=tmp_path / "runner")
    await runner.create_review_fix(
        _validation_task_metadata(candidate=candidate, captured=captured), task_id=task_id
    )
    run = runner.get_review_fix(task_id)
    assert run.review_fix is not None
    run.review_fix.state = ReviewFixState.AWAITING_VALIDATION
    run.review_fix.revision = 0
    run.revision = 0
    return runner, candidate


@pytest.mark.asyncio
@pytest.mark.parametrize("passed", [True, False])
async def test_validate_group_persists_artifacts_and_terminal_group_state(
    tmp_path, monkeypatch, passed
):
    runner, candidate = await _validation_runner(tmp_path, "review-fix-validation")

    async def fake_run_tests(_command, _cwd):
        return passed, "validation output"

    monkeypatch.setattr("kiro_crew.review_fix.run_tests", fake_run_tests)
    # Artifacts must remain inside the candidate worktree.
    updated, result = await validate_group(
        runner,
        "review-fix-validation",
        "group-1",
        expected_revision=0,
        expected_group_revision=0,
        test_command=["pytest", "-q"],
        build_command=["npm", "run", "build"],
        artifact_dir=candidate / "artifacts",
    )

    assert result is passed
    assert updated.review_fix is not None
    expected_state = ReviewFixState.READY_TO_APPLY if passed else ReviewFixState.BLOCKED_VALIDATION
    assert updated.review_fix.state is expected_state
    group = updated.review_fix.groups[0]
    expected_group_state = "ready_to_apply" if passed else "proposed"
    assert group.state.value == expected_group_state
    assert group.revision == 2
    assert len(group.validation_runs) == 2
    assert all(Path(item.artifact_path).is_file() for item in group.validation_runs)
    assert all(Path(item.artifact_path).is_relative_to(candidate) for item in group.validation_runs)
    assert len(updated.review_fix.artifact_paths) == 2


@pytest.mark.asyncio
async def test_validate_group_does_not_follow_a_planted_log_symlink(tmp_path, monkeypatch):
    """A candidate-owned artifacts entry cannot redirect log writes off worktree."""
    runner, candidate = await _validation_runner(tmp_path, "review-fix-symlink")
    outside = tmp_path / "outside.log"
    outside.write_bytes(b"OUTSIDE")
    (candidate / ".kirocrew-review-fix-artifacts").mkdir()
    (candidate / ".kirocrew-review-fix-artifacts" / "group-1-test-123.log").symlink_to(outside)
    worker_time = time.time
    monkeypatch.setattr("kiro_crew.review_fix.clock.time", lambda: 123.0)

    async def fake_run_tests(_command, _cwd):
        return True, "validation output"

    assert time.time is worker_time
    monkeypatch.setattr("kiro_crew.review_fix.run_tests", fake_run_tests)
    with pytest.raises(OSError, match="artifact symlink"):
        await validate_group(
            runner,
            "review-fix-symlink",
            "group-1",
            expected_revision=0,
            expected_group_revision=0,
            test_command=["pytest", "-q"],
            artifact_dir=candidate,
        )

    assert outside.read_bytes() == b"OUTSIDE"


@pytest.mark.asyncio
async def test_patch_write_does_not_follow_a_planted_symlink(tmp_path):
    outside = tmp_path / "outside.patch"
    outside.write_bytes(b"OUTSIDE")
    planted = tmp_path / "group.patch"
    planted.symlink_to(outside)
    patch = review_fix_git.ReviewFixPatch("id", "patch\n", ("a.py",), "")

    with pytest.raises(OSError, match="artifact symlink"):
        await review_fix_git.write_patch(patch, planted)

    assert outside.read_bytes() == b"OUTSIDE"


@pytest.mark.asyncio
async def test_validate_group_refuses_an_artifact_dir_outside_the_candidate(tmp_path):
    runner, _candidate = await _validation_runner(tmp_path, "review-fix-artifacts")

    with pytest.raises(ReviewFixPlanError, match="escapes the candidate"):
        await validate_group(
            runner,
            "review-fix-artifacts",
            "group-1",
            expected_revision=0,
            expected_group_revision=0,
            test_command=["pytest", "-q"],
            build_command=["npm", "run", "build"],
            artifact_dir=tmp_path / "elsewhere",
        )


@pytest.mark.asyncio
async def test_validate_group_refuses_without_a_captured_patch(tmp_path):
    """Validating before capture could reach READY_TO_APPLY with no patch id,
    stranding the task: Apply requires candidate_patch_id."""
    runner, candidate = await _validation_runner(tmp_path, "review-fix-uncaptured", captured=False)

    with pytest.raises(ReviewFixPlanError, match="capture") as exc_info:
        await validate_group(
            runner,
            "review-fix-uncaptured",
            "group-1",
            expected_revision=0,
            expected_group_revision=0,
            test_command=["pytest", "-q"],
            build_command=["npm", "run", "build"],
            artifact_dir=candidate / "artifacts",
        )
    assert exc_info.value.code == "capture_required"


@pytest.mark.asyncio
async def test_validate_group_treats_missing_test_command_as_failed_validation(
    tmp_path, monkeypatch
):
    # A skipped command is not validation evidence and cannot unlock Apply.
    runner, candidate = await _validation_runner(tmp_path, "review-fix-skip")

    async def fake_run_tests(_command, _cwd):
        from kiro_crew.task_executor import TESTS_SKIPPED_OUTPUT

        return True, TESTS_SKIPPED_OUTPUT

    monkeypatch.setattr("kiro_crew.review_fix.run_tests", fake_run_tests)
    updated, passed = await validate_group(
        runner,
        "review-fix-skip",
        "group-1",
        expected_revision=0,
        expected_group_revision=0,
        test_command=["definitely-missing-test-runner"],
        build_command=["definitely-missing-build-tool"],
        artifact_dir=candidate / "artifacts",
    )

    assert passed is False
    assert updated.review_fix is not None
    assert updated.review_fix.state is ReviewFixState.BLOCKED_VALIDATION
    assert all(not item.passed for item in updated.review_fix.groups[0].validation_runs)
    assert all(item.exit_code == 1 for item in updated.review_fix.groups[0].validation_runs)


@pytest.mark.asyncio
@pytest.mark.parametrize("build_command", [None, []])
async def test_validate_group_treats_a_passing_test_only_run_as_sufficient(
    tmp_path, monkeypatch, build_command
):
    """The frontend's Build field is optional; only Test is required. A
    passing test-only validation (build omitted or empty) must still unlock
    Apply -- the build step is SKIPPED, not treated as a failure."""
    runner, candidate = await _validation_runner(tmp_path, "review-fix-test-only")

    calls: list[list[str]] = []

    async def fake_run_tests(command, _cwd):
        calls.append(list(command))
        return True, "validation output"

    monkeypatch.setattr("kiro_crew.review_fix.run_tests", fake_run_tests)
    updated, passed = await validate_group(
        runner,
        "review-fix-test-only",
        "group-1",
        expected_revision=0,
        expected_group_revision=0,
        test_command=["pytest", "-q"],
        build_command=build_command,
        artifact_dir=candidate / "artifacts",
    )

    assert passed is True
    assert calls == [["pytest", "-q"]]  # the build command never ran
    assert updated.review_fix is not None
    assert updated.review_fix.state is ReviewFixState.READY_TO_APPLY
    group = updated.review_fix.groups[0]
    assert len(group.validation_runs) == 1
    assert group.validation_runs[0].kind == "test"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_id,test_command,expected_match",
    [
        ("review-fix-no-test-command", [], "test command is required"),
        (
            "review-fix-oversized",
            None,  # filled in below once _MAX_VALIDATION_COMMAND_ARGS is imported
            "too many arguments",
        ),
    ],
    ids=["missing-test-command", "oversized-command-argv"],
)
async def test_validate_group_rejects_a_bad_test_command(
    tmp_path, task_id, test_command, expected_match
):
    from kiro_crew.review_fix import _MAX_VALIDATION_COMMAND_ARGS

    if test_command is None:
        test_command = ["arg"] * (_MAX_VALIDATION_COMMAND_ARGS + 1)

    runner, _candidate = await _validation_runner(tmp_path, task_id)

    with pytest.raises(ReviewFixPlanError, match=expected_match):
        await validate_group(
            runner,
            task_id,
            "group-1",
            expected_revision=0,
            expected_group_revision=0,
            test_command=test_command,
            build_command=None,
        )


@pytest.mark.asyncio
async def test_finish_group_validation_bounds_retained_validation_runs():
    """validation_runs accumulates one record per revalidation attempt across
    a task's whole lifetime; it must be bounded like artifact_paths is, not
    grow without limit."""
    from kiro_crew.review_fix import _MAX_VALIDATION_RUNS, _finish_group_validation
    from kiro_crew.task_models import ReviewFixValidationRun

    metadata = ReviewFixMetadata(
        groups=[ReviewFixDependencyGroup(group_id="group-1", finding_keys=["finding-1"])],
    )
    group = metadata.groups[0]
    group.validation_runs = [
        ReviewFixValidationRun(
            validation_id=f"old-{i}",
            group_id="group-1",
            group_revision=0,
            kind="test",
            command=["pytest"],
            exit_code=0,
            passed=True,
            artifact_path="",
            started_at=0.0,
            finished_at=0.0,
            duration_secs=0.0,
        )
        for i in range(_MAX_VALIDATION_RUNS)
    ]

    new_run = ReviewFixValidationRun(
        validation_id="new-1",
        group_id="group-1",
        group_revision=0,
        kind="test",
        command=["pytest"],
        exit_code=0,
        passed=True,
        artifact_path="",
        started_at=0.0,
        finished_at=0.0,
        duration_secs=0.0,
    )
    _finish_group_validation(metadata, "group-1", [new_run], True)

    assert len(group.validation_runs) == _MAX_VALIDATION_RUNS
    assert group.validation_runs[-1] is new_run
    assert group.validation_runs[0].validation_id == "old-1"  # oldest entry dropped


async def _multi_group_runner(tmp_path: Path, runner: TaskRunner) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir(exist_ok=True)
    runner._runs.clear()  # each scenario gets a fresh task id-space
    await runner.create_review_fix(
        ReviewFixMetadata(
            state=ReviewFixState.AWAITING_VALIDATION,
            target=ReviewFixTargetSnapshot(dirty_fingerprint="fingerprint"),
            git=ReviewFixGitRecord(candidate_worktree_path=str(candidate)),
            groups=[
                ReviewFixDependencyGroup(
                    group_id="group-a", finding_keys=["a"], candidate_patch_id="captured-a"
                ),
                ReviewFixDependencyGroup(
                    group_id="group-b", finding_keys=["b"], candidate_patch_id="captured-b"
                ),
            ],
        ),
        task_id="review-fix-multi",
    )


async def _validate_one_group(tmp_path: Path, monkeypatch, group_id: str, passed: bool):
    runner = TaskRunner(_Sessions(), work_dir=tmp_path / "runner")
    await _multi_group_runner(tmp_path, runner)
    run = runner.get_review_fix("review-fix-multi")
    assert run.review_fix is not None
    run.review_fix.state = ReviewFixState.AWAITING_VALIDATION
    run.review_fix.revision = 0
    run.revision = 0

    async def fake_run_tests(_command, _cwd):
        return passed, "validation output"

    monkeypatch.setattr("kiro_crew.review_fix.run_tests", fake_run_tests)
    updated, result = await validate_group(
        runner,
        "review-fix-multi",
        group_id,
        expected_revision=0,
        expected_group_revision=0,
        test_command=["pytest", "-q"],
        build_command=["npm", "run", "build"],
        artifact_dir=tmp_path / "candidate" / "artifacts",
    )
    return updated, result


@pytest.mark.asyncio
async def test_first_group_validation_keeps_task_awaiting_for_sibling(tmp_path, monkeypatch):
    # Hold validation until every group has an outcome.
    updated, passed = await _validate_one_group(tmp_path, monkeypatch, "group-a", True)
    assert passed is True
    assert updated.review_fix is not None
    assert updated.review_fix.state is ReviewFixState.AWAITING_VALIDATION
    assert updated.review_fix.groups[0].state.value == "ready_to_apply"
    assert updated.review_fix.groups[1].state.value == "proposed"


async def _routed_multi_group_validation(tmp_path, monkeypatch, outcomes: dict[str, bool]):
    """Shared two-group AWAITING_VALIDATION harness routing run_tests' result by
    which group is currently being validated (run_tests' args cannot identify
    the group: same commands, same candidate cwd)."""
    runner = TaskRunner(_Sessions(), work_dir=tmp_path / "runner")
    await _multi_group_runner(tmp_path, runner)
    run = runner.get_review_fix("review-fix-multi")
    assert run.review_fix is not None
    run.review_fix.state = ReviewFixState.AWAITING_VALIDATION
    run.review_fix.revision = 0
    run.revision = 0

    validating = {"group_id": ""}

    async def routed_run_tests(_command, _cwd):
        return outcomes[validating["group_id"]], "validation output"

    monkeypatch.setattr("kiro_crew.review_fix.run_tests", routed_run_tests)

    async def validate_inner(group_id: str):
        validating["group_id"] = group_id
        current = runner.get_review_fix("review-fix-multi")
        group = next(item for item in current.review_fix.groups if item.group_id == group_id)
        return await validate_group(
            runner,
            "review-fix-multi",
            group_id,
            expected_revision=current.revision,
            expected_group_revision=group.revision,
            test_command=["pytest", "-q"],
            build_command=["npm", "run", "build"],
            artifact_dir=tmp_path / "candidate" / "artifacts",
        )

    return validate_inner


@pytest.mark.asyncio
async def test_last_group_out_of_order_fail_blocks_after_sibling_passed(tmp_path, monkeypatch):
    # B's success cannot promote the task past A's later failure.
    validate_inner = await _routed_multi_group_validation(
        tmp_path, monkeypatch, {"group-a": False, "group-b": True}
    )

    updated_b, passed_b = await validate_inner("group-b")
    assert passed_b is True
    assert updated_b.review_fix is not None
    assert updated_b.review_fix.state is ReviewFixState.AWAITING_VALIDATION

    updated_a, passed_a = await validate_inner("group-a")
    assert passed_a is False
    assert updated_a.review_fix is not None
    assert updated_a.review_fix.state is ReviewFixState.BLOCKED_VALIDATION


@pytest.mark.asyncio
async def test_all_groups_passed_then_last_advances_ready_to_apply(tmp_path, monkeypatch):
    validate_inner = await _routed_multi_group_validation(
        tmp_path, monkeypatch, {"group-a": True, "group-b": True}
    )

    updated_a, passed_a = await validate_inner("group-a")
    assert passed_a is True
    assert updated_a.review_fix is not None
    assert updated_a.review_fix.state is ReviewFixState.AWAITING_VALIDATION

    updated_b, passed_b = await validate_inner("group-b")
    assert passed_b is True
    assert updated_b.review_fix is not None
    assert updated_b.review_fix.state is ReviewFixState.READY_TO_APPLY
    assert [g.state.value for g in updated_b.review_fix.groups] == [
        "ready_to_apply",
        "ready_to_apply",
    ]


@pytest.mark.asyncio
async def test_failed_group_then_sibling_pass_blocks_validation(tmp_path, monkeypatch):
    # B's later success cannot promote a failed group.
    validate_inner = await _routed_multi_group_validation(
        tmp_path, monkeypatch, {"group-a": False, "group-b": True}
    )

    updated_a, passed_a = await validate_inner("group-a")
    assert passed_a is False
    assert updated_a.review_fix is not None
    assert updated_a.review_fix.state is ReviewFixState.AWAITING_VALIDATION

    updated_b, passed_b = await validate_inner("group-b")
    assert passed_b is True
    assert updated_b.review_fix is not None
    assert updated_b.review_fix.state is ReviewFixState.BLOCKED_VALIDATION


def _fake_web_request() -> SimpleNamespace:
    return SimpleNamespace(headers={}, query={}, match_info={})


async def _write_patch_ok(patch, patch_path):
    return ReviewFixPatch(patch.patch_id, patch.patch_text, patch.paths, str(patch_path))


def _apply_commit_fixture(tmp_path: Path, monkeypatch, *, groups: list[str]):
    """Build a ready two-group apply/commit fixture."""
    runner = TaskRunner(_Sessions(), work_dir=tmp_path / "runner")
    runner._runs.clear()  # each scenario gets a fresh task id-space
    metadata = ReviewFixMetadata(
        state=ReviewFixState.READY_TO_APPLY,
        target=ReviewFixTargetSnapshot(
            dirty_fingerprint="fingerprint",
            repo_root=str(tmp_path),
            target_path=str(tmp_path),
            branch_name="feature/fix",
            head_sha="0" * 40,
        ),
        git=ReviewFixGitRecord(candidate_worktree_path=str(tmp_path / "candidate")),
        groups=[
            ReviewFixDependencyGroup(
                group_id=group_id,
                finding_keys=[group_id],
                state=ReviewFixGroupState.READY_TO_APPLY,
                # Apply verifies the pinned capture id.
                candidate_patch_id="disk-id",
                affected_files=["target.txt"],
            )
            for group_id in groups
        ],
    )

    async def same_target(*_args, **_kwargs):
        return metadata.target

    async def fake_candidate_patch(*_args, **_kwargs):
        return ReviewFixPatch(
            patch_id="disk-id", patch_text="diff --git a/target.txt\n", paths=("target.txt",)
        )

    async def fake_apply(*_args, **_kwargs):
        return None

    async def fake_commit(*_args, **_kwargs):
        return "abc1234"

    monkeypatch.setattr(fix_tasks.review_fix_git, "inspect_target", same_target)
    monkeypatch.setattr(fix_tasks.review_fix_git, "candidate_patch", fake_candidate_patch)
    monkeypatch.setattr(fix_tasks.review_fix_git, "write_patch", _write_patch_ok)
    monkeypatch.setattr(fix_tasks.review_fix_git, "apply_patch", fake_apply)
    monkeypatch.setattr(fix_tasks.review_fix_git, "commit_group", fake_commit)

    async def _ready() -> TaskRunner:
        await runner.create_review_fix(metadata, task_id="review-fix-multi")
        run = runner.get_review_fix("review-fix-multi")
        assert run.review_fix is not None
        run.review_fix.state = ReviewFixState.READY_TO_APPLY
        run.review_fix.revision = 0
        run.revision = 0
        return runner

    return _ready


async def _apply_commit_ready(tmp_path: Path, monkeypatch, *, groups: list[str]) -> TaskRunner:
    ready = _apply_commit_fixture(tmp_path, monkeypatch, groups=groups)
    return await ready()


async def _act(runner: TaskRunner, action: str, group_id: str) -> Any:
    """Drive one mutating action through fix_tasks._action with fresh CAS context.

    expected_revision / target_fingerprint are read from the CURRENT run so the
    CAS checks pass after the previous action bumped the revision.
    """
    run = runner.get_review_fix("review-fix-multi")
    assert run.review_fix is not None
    return await fix_tasks._action(
        _fake_web_request(),
        runner,
        run,
        action,
        {"group_id": group_id, "commit_message": "fix: align target"},
        run.revision,
        run.review_fix.target.dirty_fingerprint,
    )


@pytest.mark.asyncio
async def test_first_applied_group_keeps_task_ready_for_sibling(tmp_path, monkeypatch):
    # Only the last apply may advance the task phase.
    runner = await _apply_commit_ready(tmp_path, monkeypatch, groups=["group-a", "group-b"])

    updated = await _act(runner, "apply_group", "group-a")
    assert updated.review_fix is not None
    assert updated.review_fix.state is ReviewFixState.READY_TO_APPLY
    assert [g.state.value for g in updated.review_fix.groups] == ["applied", "ready_to_apply"]

    updated = await _act(runner, "apply_group", "group-b")
    assert updated.review_fix is not None
    assert updated.review_fix.state is ReviewFixState.AWAITING_COMMIT
    assert [g.state.value for g in updated.review_fix.groups] == ["applied", "applied"]


@pytest.mark.asyncio
async def test_first_committed_group_keeps_task_awaiting_commit_for_sibling(tmp_path, monkeypatch):
    # Only the last commit may advance the task phase.
    runner = await _apply_commit_ready(tmp_path, monkeypatch, groups=["group-a", "group-b"])
    await _act(runner, "apply_group", "group-a")
    await _act(runner, "apply_group", "group-b")

    updated = await _act(runner, "commit_group", "group-a")
    assert updated.review_fix is not None
    assert updated.review_fix.state is ReviewFixState.AWAITING_COMMIT
    assert [g.state.value for g in updated.review_fix.groups] == ["committed", "applied"]

    updated = await _act(runner, "commit_group", "group-b")
    assert updated.review_fix is not None
    assert updated.review_fix.state is ReviewFixState.COMMITTED
    assert [g.state.value for g in updated.review_fix.groups] == ["committed", "committed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_group", ["group-a", "group-b"])
async def test_push_preview_requires_every_group_committed_when_state_is_committed(
    tmp_path, monkeypatch, requested_group
):
    # Push is refused while any group is not committed.
    runner = await _apply_commit_ready(tmp_path, monkeypatch, groups=["group-a", "group-b"])
    await _act(runner, "apply_group", "group-a")
    await _act(runner, "apply_group", "group-b")
    await _act(runner, "commit_group", "group-a")
    run = runner.get_review_fix("review-fix-multi")
    assert run.review_fix is not None
    run.review_fix.state = ReviewFixState.COMMITTED

    with pytest.raises(ValueError, match="not all groups are committed"):
        await _act(runner, "push_preview", requested_group)


@pytest.mark.asyncio
async def test_two_group_apply_commit_preview_push_happy_path(tmp_path, monkeypatch):
    # The lifecycle advances last-group-out and pushes only all-committed state.
    runner = await _apply_commit_ready(tmp_path, monkeypatch, groups=["group-a", "group-b"])

    updated = await _act(runner, "apply_group", "group-a")
    assert updated.review_fix is not None
    assert updated.review_fix.state is ReviewFixState.READY_TO_APPLY

    updated = await _act(runner, "apply_group", "group-b")
    assert updated.review_fix is not None
    assert updated.review_fix.state is ReviewFixState.AWAITING_COMMIT

    updated = await _act(runner, "commit_group", "group-a")
    assert updated.review_fix is not None
    assert updated.review_fix.state is ReviewFixState.AWAITING_COMMIT

    updated = await _act(runner, "commit_group", "group-b")
    assert updated.review_fix is not None
    assert updated.review_fix.state is ReviewFixState.COMMITTED
    assert all(g.state is ReviewFixGroupState.COMMITTED for g in updated.review_fix.groups)

    async def fake_preview(*_args, **_kwargs):
        return {
            "remote": "origin",
            "branch": "feature/fix",
            "upstream": "origin/feature/fix",
            "commits": ["abc1234 fix: align target"],
            "files": ["target.txt"],
            "diverged": False,
        }

    pushed: list[tuple] = []

    async def fake_push(*_args, **_kwargs):
        pushed.append(_args)
        return {"remote": _args[1], "branch": _args[2], "pushed": True}

    monkeypatch.setattr(fix_tasks.review_fix_git, "push_preview", fake_preview)
    monkeypatch.setattr(fix_tasks.review_fix_git, "push", fake_push)

    updated = await _act(runner, "push_preview", "group-a")
    assert updated.review_fix is not None
    assert updated.review_fix.state is ReviewFixState.AWAITING_PUSH

    updated = await _act(runner, "push", "group-a")
    assert updated.review_fix is not None
    assert updated.review_fix.state is ReviewFixState.PUSHED
    assert len(pushed) == 1
