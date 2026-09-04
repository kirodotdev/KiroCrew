"""Tests for the review-fix planning, validation, and phase-machine modules."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from review_fix_helpers import _repo, unsandboxed_git  # noqa: F401  (autouse fixture)

from kiro_crew.apps.builtins.code_review_sage.backend import fix_tasks
from kiro_crew.review_fix import (
    ReviewFixModelResolutionError,
    ReviewFixPlanError,
    build_review_fix_groups,
    build_review_fix_tasks,
    create_review_fix_task,
    resolve_pinned_model,
    validate_group,
)
from kiro_crew.review_fix_git import ReviewFixPatch, discard_candidate
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
    # An empty advertised set means the provider's catalogue is unknown, which
    # the repo-wide model gate treats as "allow" — the concrete id itself is
    # still required and still shape-checked.
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
    # A group's patch is scoped to the WHOLE file it owns, so two default
    # groups for one file would each carry the entire file diff: applying the
    # first would land the sibling's unapproved edits too. Co-locate them.
    findings = [
        ReviewFixFindingSnapshot(key="a1", file_path="a.py"),
        ReviewFixFindingSnapshot(key="b1", file_path="b.py"),
        ReviewFixFindingSnapshot(key="a2", file_path="a.py"),
    ]

    groups = build_review_fix_groups(findings, None)

    assert [(group.group_id, group.finding_keys, group.affected_files)
            for group in groups] == [
        # group ids stay sequential in order of first appearance of each file.
        ("group-1", ["a1", "a2"], ["a.py"]),
        ("group-2", ["b1"], ["b.py"]),
    ]


@pytest.mark.parametrize("raw_hard, expected", [("false", False), ("true", False), (True, True)])
def test_hard_flag_locks_only_on_a_json_boolean(raw_hard, expected):
    # bool("false") is True, so the old coercion locked a group the caller
    # sent as the JSON string "false". Only a literal true may lock.
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
    # A fileless finding used to produce affected_files=[] and an unscoped patch
    # (a pathless `git diff` covers the whole candidate worktree); plan time is
    # where that must die.
    findings = [
        ReviewFixFindingSnapshot(key="red", file_path="a.py"),
        ReviewFixFindingSnapshot(key="ghost", file_path=""),
    ]

    with pytest.raises(ReviewFixPlanError, match="ghost.*no file_path") as excinfo:
        build_review_fix_groups(findings, None)
    assert excinfo.value.code == "fileless_finding"


def test_raw_group_with_no_owned_files_is_rejected():
    # A raw group whose findings are all fileless (and which supplies no
    # affected_files of its own) has no owned paths -> refuse the plan.
    findings = [ReviewFixFindingSnapshot(key="ghost", file_path="")]

    with pytest.raises(ReviewFixPlanError, match="owns no files") as excinfo:
        build_review_fix_groups(findings, [{"finding_keys": ["ghost"]}])
    assert excinfo.value.code == "fileless_group"


def test_groups_claiming_the_same_file_are_rejected():
    # Each group's patch captures its WHOLE affected_files set, so two groups
    # both claiming b.py would each carry the other's edits: applying the
    # first lands the sibling's unapproved changes. The plan must refuse.
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
    # The edit_soft_grouping handler (fix_tasks.py) submits its replacement
    # groups through this SAME builder, so a user-approved regrouping cannot
    # re-split a file across two groups — the same whole-file-patch overlap
    # the initial plan rejects would otherwise re-enter the lifecycle.
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

    # Different files run in parallel: no depends_on either way.
    assert by_index[1].depends_on == []
    assert by_index[2].depends_on == []
    # Same file: the later task depends on the earlier one.
    assert by_index[3].depends_on == [1]
    # Forward-only by construction, so no cycle is possible.
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

    # One group = one serialized resource, even though the files differ.
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

    # previous-task-only edges keep the file chain intact (1 -> 3 -> 4).
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
@pytest.mark.parametrize("passed", [True, False])
async def test_validate_group_persists_artifacts_and_terminal_group_state(
    tmp_path, monkeypatch, passed
):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    runner = TaskRunner(_Sessions(), work_dir=tmp_path / "runner")
    await runner.create_review_fix(
        ReviewFixMetadata(
            state=ReviewFixState.AWAITING_VALIDATION,
            target=ReviewFixTargetSnapshot(dirty_fingerprint="fingerprint"),
            git=ReviewFixGitRecord(candidate_worktree_path=str(candidate)),
            groups=[ReviewFixDependencyGroup(group_id="group-1", finding_keys=["finding-1"])],
        ),
        task_id="review-fix-validation",
    )
    run = runner.get_review_fix("review-fix-validation")
    assert run.review_fix is not None
    run.review_fix.state = ReviewFixState.AWAITING_VALIDATION
    run.review_fix.revision = 0
    run.revision = 0

    async def fake_run_tests(_command, _cwd):
        return passed, "validation output"

    monkeypatch.setattr("kiro_crew.review_fix.run_tests", fake_run_tests)
    # The artifact directory must live inside the candidate worktree; an
    # out-of-candidate dir is exactly what artifact_root() exists to refuse.
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
async def test_validate_group_refuses_an_artifact_dir_outside_the_candidate(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    runner = TaskRunner(_Sessions(), work_dir=tmp_path / "runner")
    await runner.create_review_fix(
        ReviewFixMetadata(
            state=ReviewFixState.AWAITING_VALIDATION,
            target=ReviewFixTargetSnapshot(dirty_fingerprint="fingerprint"),
            git=ReviewFixGitRecord(candidate_worktree_path=str(candidate)),
            groups=[ReviewFixDependencyGroup(group_id="group-1", finding_keys=["finding-1"])],
        ),
        task_id="review-fix-artifacts",
    )
    run = runner.get_review_fix("review-fix-artifacts")
    assert run.review_fix is not None
    run.review_fix.state = ReviewFixState.AWAITING_VALIDATION
    run.review_fix.revision = 0
    run.revision = 0

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
async def test_validate_group_treats_missing_test_command_as_failed_validation(
    tmp_path, monkeypatch
):
    # TaskRunner.run_tests reports a missing executable as ("success", sentinel)
    # so the generic runner can treat "nothing to run" as a skip. Review-fix
    # validation must NOT credit that skip as evidence: a typo'd command would
    # otherwise mint passing validation runs and unlock Apply.
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    runner = TaskRunner(_Sessions(), work_dir=tmp_path / "runner")
    await runner.create_review_fix(
        ReviewFixMetadata(
            state=ReviewFixState.AWAITING_VALIDATION,
            target=ReviewFixTargetSnapshot(dirty_fingerprint="fingerprint"),
            git=ReviewFixGitRecord(candidate_worktree_path=str(candidate)),
            groups=[ReviewFixDependencyGroup(group_id="group-1", finding_keys=["finding-1"])],
        ),
        task_id="review-fix-skip",
    )
    run = runner.get_review_fix("review-fix-skip")
    assert run.review_fix is not None
    run.review_fix.state = ReviewFixState.AWAITING_VALIDATION
    run.review_fix.revision = 0
    run.revision = 0

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
                ReviewFixDependencyGroup(group_id="group-a", finding_keys=["a"]),
                ReviewFixDependencyGroup(group_id="group-b", finding_keys=["b"]),
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
    # With two groups, the FIRST result must not flip the whole task: group B
    # would be stranded outside the validation phase (its Validate button is
    # gated on the task being in a validation state, and there is no path back).
    updated, passed = await _validate_one_group(tmp_path, monkeypatch, "group-a", True)
    assert passed is True
    assert updated.review_fix is not None
    assert updated.review_fix.state is ReviewFixState.AWAITING_VALIDATION
    assert updated.review_fix.groups[0].state.value == "ready_to_apply"
    assert updated.review_fix.groups[1].state.value == "proposed"


@pytest.mark.asyncio
async def test_last_group_out_of_order_fail_blocks_after_sibling_passed(tmp_path, monkeypatch):
    # Sequential two-group flow, reverse order: B passes first (task holds in
    # awaiting_validation so A can still validate), then A fails -> the task
    # must land BLOCKED_VALIDATION. B's earlier success must not promote the
    # task past a sibling's failure.
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    runner = TaskRunner(_Sessions(), work_dir=tmp_path / "runner")
    await _multi_group_runner(tmp_path, runner)
    run = runner.get_review_fix("review-fix-multi")
    assert run.review_fix is not None
    run.review_fix.state = ReviewFixState.AWAITING_VALIDATION
    run.review_fix.revision = 0
    run.revision = 0

    outcomes = {"group-a": False, "group-b": True}
    validating = {"group_id": ""}

    async def routed_run_tests(_command, _cwd):
        # run_tests args cannot identify the group (same commands, same
        # candidate cwd), so key the outcome on the group being validated.
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

    updated_b, passed_b = await validate_inner("group-b")
    assert passed_b is True
    assert updated_b.review_fix is not None
    # B passed but A has not validated yet: the task holds.
    assert updated_b.review_fix.state is ReviewFixState.AWAITING_VALIDATION

    updated_a, passed_a = await validate_inner("group-a")
    assert passed_a is False
    assert updated_a.review_fix is not None
    # A failed last: the task blocks even though B already passed.
    assert updated_a.review_fix.state is ReviewFixState.BLOCKED_VALIDATION


@pytest.mark.asyncio
async def test_all_groups_passed_then_last_advances_ready_to_apply(tmp_path, monkeypatch):
    # Sequential two-group flow on ONE task: A passes (task holds in
    # awaiting_validation so B can still validate), B passes -> task advances.
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    runner = TaskRunner(_Sessions(), work_dir=tmp_path / "runner")
    await _multi_group_runner(tmp_path, runner)
    run = runner.get_review_fix("review-fix-multi")
    assert run.review_fix is not None
    run.review_fix.state = ReviewFixState.AWAITING_VALIDATION
    run.review_fix.revision = 0
    run.revision = 0

    async def fake_run_tests(_command, _cwd):
        return True, "validation output"

    monkeypatch.setattr("kiro_crew.review_fix.run_tests", fake_run_tests)

    async def validate_inner(group_id: str):
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

    updated_a, passed_a = await validate_inner("group-a")
    assert passed_a is True
    assert updated_a.review_fix is not None
    assert updated_a.review_fix.state is ReviewFixState.AWAITING_VALIDATION

    updated_b, passed_b = await validate_inner("group-b")
    assert passed_b is True
    assert updated_b.review_fix is not None
    # Every group finished and passed: the task advances only now.
    assert updated_b.review_fix.state is ReviewFixState.READY_TO_APPLY
    assert [g.state.value for g in updated_b.review_fix.groups] == [
        "ready_to_apply",
        "ready_to_apply",
    ]


@pytest.mark.asyncio
async def test_failed_group_then_sibling_pass_blocks_validation(tmp_path, monkeypatch):
    # A fails, B passes afterwards: the task must land BLOCKED_VALIDATION (the
    # failed A must not be silently promoted by B's success), and B's
    # validation finishing after A's failure must still be possible.
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    runner = TaskRunner(_Sessions(), work_dir=tmp_path / "runner")
    await _multi_group_runner(tmp_path, runner)
    run = runner.get_review_fix("review-fix-multi")
    assert run.review_fix is not None
    run.review_fix.state = ReviewFixState.AWAITING_VALIDATION
    run.review_fix.revision = 0
    run.revision = 0

    outcomes = {"group-a": False, "group-b": True}
    validating = {"group_id": ""}

    async def routed_run_tests(_command, _cwd):
        # run_tests args cannot identify the group (same commands, same
        # candidate cwd), so key the outcome on the group being validated.
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

    updated_a, passed_a = await validate_inner("group-a")
    assert passed_a is False
    assert updated_a.review_fix is not None
    assert updated_a.review_fix.state is ReviewFixState.AWAITING_VALIDATION

    updated_b, passed_b = await validate_inner("group-b")
    assert passed_b is True
    assert updated_b.review_fix is not None
    # B passed but A failed earlier: the task blocks, it does not advance.
    assert updated_b.review_fix.state is ReviewFixState.BLOCKED_VALIDATION


# ── apply/commit "last group out" phase machine ────────────────────────────


def _fake_web_request() -> SimpleNamespace:
    # fix_tasks._action only reads the request on the resolve_model path
    # (advertised-ids lookup); apply/commit/push_preview never touch it.
    return SimpleNamespace(headers={}, query={}, match_info={})


async def _write_patch_ok(patch, patch_path):
    return ReviewFixPatch(patch.patch_id, patch.patch_text, patch.paths, str(patch_path))


def _apply_commit_fixture(tmp_path: Path, monkeypatch, *, groups: list[str]):
    """Shared two-group apply/commit harness.

    Builds a READY_TO_APPLY task with the named groups, pins each group's
    capture id, and stubs the git primitives so apply/commit exercise the real
    phase-machine code (fix_tasks._apply_group / _commit_group) without needing
    a live candidate worktree. Returns an async factory that creates the task
    and returns the ready runner.
    """
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
                # Must equal the id fake_candidate_patch returns below: apply
                # verifies the disk patch against the pinned capture id.
                candidate_patch_id="disk-id",
                affected_files=["target.txt"],
            )
            for group_id in groups
        ],
    )

    async def same_target(*_args, **_kwargs):
        # The stored dirty fingerprint never changes on disk in these
        # scenarios, so hand back the same snapshot: the CAS must pass and let
        # the phase logic under test run.
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
    # Two groups: applying A must NOT flip the whole task to AWAITING_COMMIT —
    # B's Apply is gated on the task being READY_TO_APPLY and the transition
    # table has no edge back, so an early flip would strand B forever. Only the
    # LAST apply advances the task.
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
    # Same rule on the commit edge: committing A while B is only APPLIED must
    # hold the task in AWAITING_COMMIT (COMMITTED admits no path back to it),
    # and the last commit flips the task to COMMITTED.
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
async def test_push_preview_refused_while_a_group_is_not_committed(tmp_path, monkeypatch):
    # Pushing publishes every group's work, so the preview gate must refuse a
    # mid-lifecycle COMMITTED state: here B is still only applied when A's
    # commit has flipped the task to COMMITTED (single-group semantics no
    # longer apply), and the preview must not open.
    runner = await _apply_commit_ready(tmp_path, monkeypatch, groups=["group-a", "group-b"])
    await _act(runner, "apply_group", "group-a")
    await _act(runner, "apply_group", "group-b")
    await _act(runner, "commit_group", "group-a")
    run = runner.get_review_fix("review-fix-multi")
    assert run.review_fix is not None
    # Scenario precondition: the task itself sits in COMMITTED while a sibling
    # has not committed. (With the last-group-out rule the task only reaches
    # COMMITTED after the LAST commit, so drive the gate directly at the state
    # the OLD code would have accepted.)
    run.review_fix.state = ReviewFixState.COMMITTED

    with pytest.raises(ValueError, match="not all groups are committed"):
        await _act(runner, "push_preview", "group-a")


@pytest.mark.asyncio
async def test_push_preview_requires_every_group_committed_when_state_is_committed(
    tmp_path, monkeypatch
):
    # The strict form of the same gate: state COMMITTED + a non-committed
    # sibling group => refused, regardless of which group id the request names.
    runner = await _apply_commit_ready(tmp_path, monkeypatch, groups=["group-a", "group-b"])
    await _act(runner, "apply_group", "group-a")
    await _act(runner, "apply_group", "group-b")
    await _act(runner, "commit_group", "group-a")
    run = runner.get_review_fix("review-fix-multi")
    assert run.review_fix is not None
    run.review_fix.state = ReviewFixState.COMMITTED

    with pytest.raises(ValueError, match="not all groups are committed"):
        await _act(runner, "push_preview", "group-b")


@pytest.mark.asyncio
async def test_two_group_apply_commit_preview_push_happy_path(tmp_path, monkeypatch):
    # Full lifecycle E2E over two groups: apply/apply/commit/commit must walk
    # READY_TO_APPLY -> AWAITING_COMMIT -> COMMITTED, the tightened preview
    # gate must accept ONLY the all-committed state, and push must consume the
    # approved preview.
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
