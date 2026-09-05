"""Review-fix orchestration shared by Sage and Task Runner dashboard routes."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from kiro_crew import platform_compat, review_fix_git
from kiro_crew.agent_sdk import model_is_unusable
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.task_executor import TESTS_SKIPPED_OUTPUT, run_tests
from kiro_crew.task_models import (
    ReviewFixDependencyGroup,
    ReviewFixFindingSnapshot,
    ReviewFixGroupState,
    ReviewFixMetadata,
    ReviewFixModelResolution,
    ReviewFixState,
    ReviewFixTargetMode,
    ReviewFixValidationRun,
    Task,
)
from kiro_crew.validation import MODEL_ID_RE

if TYPE_CHECKING:
    from kiro_crew.taskrunner import TaskRunner


_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
# A supplied group id becomes an artifact FILENAME ({group_id}.patch, log), so it
# is validated at plan time instead of being sanitized after the fact: anything a
# traversal ("../") or a hidden/absolute component would need is simply rejected.
_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_ARTIFACT_BYTES = 256 * 1024


class ReviewFixModelResolutionError(ValueError):
    """Raised when a review-fix task cannot obtain a concrete served model."""

    code = "blocked_model_resolution"


class ReviewFixPlanError(ValueError):
    """Raised when immutable finding snapshots cannot form a safe plan."""

    def __init__(self, message: str, *, code: str = "invalid_review_fix") -> None:
        super().__init__(message)
        self.code = code


def resolve_pinned_model(
    requested_model: str,
    advertised_model_ids: Sequence[str],
    *,
    provider: str = "acp",
    resolved_at: float | None = None,
) -> ReviewFixModelResolution:
    """Resolve only a concrete, advertised model for a review-fix task.

    ``auto`` and an empty requested id are intentionally rejected: a fix task
    must keep using the same concrete model across retries and resumes. An
    empty advertised set means the provider's catalogue is unknown, so it
    allows rather than blocks — the repo-wide model-gate invariant.
    """
    requested = str(requested_model or "").strip()
    advertised = [str(value).strip() for value in advertised_model_ids if str(value).strip()]
    if not requested or requested.lower() == "auto":
        raise ReviewFixModelResolutionError("a concrete review-fix model is required")
    if not MODEL_ID_RE.fullmatch(requested):
        raise ReviewFixModelResolutionError("review-fix model id is invalid")
    if advertised and model_is_unusable(requested, advertised):
        raise ReviewFixModelResolutionError("review-fix model is not advertised for this provider")
    return ReviewFixModelResolution(
        requested_model=requested,
        provider=str(provider or "acp"),
        resolved_model_id=requested,
        advertised_model_ids=advertised,
        resolved_at=resolved_at or time.time(),
    )


def _finding_snapshot(raw: Any, index: int) -> ReviewFixFindingSnapshot:
    if not isinstance(raw, dict):
        raise ReviewFixPlanError("finding must be an object")
    key = str(
        raw.get("key") or raw.get("id") or raw.get("fingerprint") or f"finding-{index}"
    ).strip()
    if not key:
        raise ReviewFixPlanError("finding key is required")
    line = raw.get("line", raw.get("start_line"))
    end_line = raw.get("end_line")
    return ReviewFixFindingSnapshot(
        key=key,
        title=str(raw.get("title") or raw.get("headline") or "").strip(),
        severity=str(raw.get("severity") or raw.get("priority") or "").strip(),
        body=str(raw.get("body") or raw.get("description") or raw.get("message") or "").strip(),
        file_path=str(raw.get("file_path") or raw.get("path") or raw.get("file") or "").strip(),
        line=int(line) if isinstance(line, (int, float)) else None,
        end_line=int(end_line) if isinstance(end_line, (int, float)) else None,
        fingerprint=str(raw.get("fingerprint") or "").strip(),
        suggested_fix=str(raw.get("suggested_fix") or raw.get("fix") or "").strip(),
    )


def build_review_fix_groups(
    findings: Sequence[ReviewFixFindingSnapshot],
    raw_groups: Sequence[Any] | None = None,
) -> list[ReviewFixDependencyGroup]:
    """Normalize a user/planner grouping while preserving hard edges."""
    by_key = {finding.key: finding for finding in findings}
    if len(by_key) != len(findings):
        raise ReviewFixPlanError("finding keys must be unique")
    if not raw_groups:
        # A fileless finding would leave its group with no owned paths, and an
        # unscoped patch diff covers the WHOLE candidate worktree (see
        # review_fix_git.candidate_patch) -- refuse to plan such a group.
        for finding in findings:
            if not finding.file_path:
                raise ReviewFixPlanError(
                    f"finding {finding.key!r} has no file_path; cannot plan a fix group",
                    code="fileless_finding",
                )
        # One group PER FILE, not per finding: a group's patch is scoped to the
        # whole file it owns (see review_fix_git.candidate_patch), so two
        # groups owning the same file would each carry the entire file diff --
        # applying the first would land the sibling's unapproved edits too.
        by_file: dict[str, list[ReviewFixFindingSnapshot]] = {}
        for finding in findings:
            by_file.setdefault(finding.file_path, []).append(finding)
        return [
            ReviewFixDependencyGroup(
                group_id=f"group-{index}",
                finding_keys=[finding.key for finding in group],
                affected_files=[path],
            )
            for index, (path, group) in enumerate(by_file.items(), start=1)
        ]

    groups: list[ReviewFixDependencyGroup] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_groups, start=1):
        if not isinstance(raw, dict):
            raise ReviewFixPlanError("dependency group must be an object")
        keys = [str(value) for value in raw.get("finding_keys", raw.get("findings", []))]
        if not keys or any(key not in by_key for key in keys) or seen.intersection(keys):
            raise ReviewFixPlanError("dependency groups must cover each finding once")
        seen.update(keys)
        raw_group_id = str(raw.get("group_id") or "")
        if raw_group_id and not _GROUP_ID_RE.fullmatch(raw_group_id):
            raise ReviewFixPlanError("dependency group id is invalid")
        files = [by_key[key].file_path for key in keys if by_key[key].file_path]
        affected = sorted(set(files) | {str(value) for value in raw.get("affected_files", [])})
        if not affected:
            raise ReviewFixPlanError(
                f"dependency group {raw_group_id or index} owns no files; "
                "a fix group must be scoped to owned paths",
                code="fileless_group",
            )
        groups.append(
            ReviewFixDependencyGroup(
                group_id=raw_group_id or f"group-{index}",
                finding_keys=keys,
                # Strictly boolean: a JSON "false" string is truthy and must
                # not lock the group.
                hard=raw.get("hard") is True,
                hard_edges=[
                    dict(edge) for edge in raw.get("hard_edges", []) if isinstance(edge, dict)
                ],
                soft_edges=[
                    dict(edge) for edge in raw.get("soft_edges", []) if isinstance(edge, dict)
                ],
                reasons=[str(value) for value in raw.get("reasons", [])],
                affected_files=affected,
            )
        )
    if seen != set(by_key):
        raise ReviewFixPlanError("dependency groups must cover every selected finding")
    # A group's patch captures its whole affected_files set, so two groups
    # whose derived sets overlap would each carry the other's edits: applying
    # the first lands the sibling's unapproved changes. Reject the plan
    # instead of silently corrupting the approval lifecycle.
    owners: dict[str, str] = {}
    for group in groups:
        for path in group.affected_files:
            previous = owners.setdefault(path, group.group_id)
            if previous != group.group_id:
                raise ReviewFixPlanError(
                    f"dependency groups {previous!r} and {group.group_id!r} both "
                    f"own {path!r}; a file may belong to at most one group"
                )
    return groups


def build_review_fix_tasks(
    findings: Sequence[ReviewFixFindingSnapshot],
    groups: Sequence[ReviewFixDependencyGroup],
) -> list[Task]:
    """Create one Task Runner task per immutable finding snapshot.

    Tasks that share a resource serialize through ``depends_on`` (previous task
    only, so the chain stays acyclic and forward-only by construction): two
    findings touching the same file, or two findings in the same dependency
    group, would otherwise run concurrently inside ONE candidate worktree and
    overwrite each other's edits.
    """
    group_of: dict[str, str] = {
        key: group.group_id for group in groups for key in group.finding_keys
    }
    last_task_for_file: dict[str, int] = {}
    last_task_for_group: dict[str, int] = {}
    tasks: list[Task] = []
    for index, finding in enumerate(findings, start=1):
        title = finding.title or finding.key
        description = finding.body or finding.suggested_fix or title
        dependencies = {last_task_for_group.get(group_of.get(finding.key, ""))}
        dependencies.update(last_task_for_file.get(path) for path in {finding.file_path})
        dependencies.discard(None)
        dependencies.discard(index)
        depends_on = sorted(dep for dep in dependencies if isinstance(dep, int) and dep < index)
        tasks.append(
            Task(
                index=index,
                title=title[:500],
                description=description[:5000],
                depends_on=depends_on,
                task_type="fix",
            )
        )
        last_task_for_file[finding.file_path] = index
        last_task_for_group[group_of.get(finding.key, "")] = index
    return tasks


async def create_review_fix_task(
    runner: "TaskRunner",
    *,
    target_path: str | Path,
    findings: Sequence[Any],
    review_run_id: str = "",
    pr_url: str = "",
    source_head_sha: str = "",
    target_mode: ReviewFixTargetMode | str = ReviewFixTargetMode.CURRENT_BRANCH,
    requested_model: str = "",
    advertised_model_ids: Sequence[str] = (),
    provider: str = "acp",
    raw_groups: Sequence[Any] | None = None,
    task_id: str = "",
    name: str = "",
    candidate_root: str | Path | None = None,
) -> Any:
    """Create a retained candidate and a durable review-fix Task Runner run."""
    try:
        mode = (
            target_mode
            if isinstance(target_mode, ReviewFixTargetMode)
            else ReviewFixTargetMode(str(target_mode))
        )
    except ValueError as exc:
        raise ReviewFixPlanError("invalid review-fix target mode") from exc
    snapshots = [_finding_snapshot(raw, index) for index, raw in enumerate(findings, start=1)]
    if not snapshots:
        raise ReviewFixPlanError("at least one finding is required")
    groups = build_review_fix_groups(snapshots, raw_groups)
    tasks = build_review_fix_tasks(snapshots, groups)
    target = await review_fix_git.inspect_target(target_path, mode=mode)
    metadata = ReviewFixMetadata(
        review_run_id=review_run_id,
        pr_url=pr_url,
        source_head_sha=source_head_sha or target.head_sha,
        selected_finding_keys=[finding.key for finding in snapshots],
        finding_snapshots=snapshots,
        target=target,
        model=ReviewFixModelResolution(
            requested_model=str(requested_model or ""),
            provider=str(provider or "acp"),
            advertised_model_ids=[str(value) for value in advertised_model_ids],
        ),
        groups=groups,
    )
    run = await runner.create_review_fix(
        metadata,
        task_id=task_id,
        name=name or review_run_id or "review-fix",
        spec_content="\n".join(task.description for task in tasks),
        tasks=tasks,
    )
    task_id = run.task_id
    safe_id = _SAFE_ID_RE.sub("-", task_id).strip("-._") or "task"
    default_root = Path(target.repo_root).parent / ".kirocrew-work" / "review-fix" / safe_id
    root = Path(candidate_root).expanduser().resolve() if candidate_root else default_root
    candidate_path = root / "candidate"
    candidate_branch = f"kirocrew/review-fix/{safe_id}"
    try:
        git_record = await review_fix_git.create_candidate(target, candidate_path, candidate_branch)
    except Exception:
        await runner.delete_run(task_id)
        raise
    run = await runner.mutate_review_fix(
        task_id,
        expected_revision=0,
        action="candidate_created",
        mutate=lambda current: setattr(current, "git", git_record),
    )
    run.work_dir = git_record.candidate_worktree_path
    run.branch_name = git_record.candidate_branch
    run.repo_root = target.repo_root
    run.git_enabled = True
    await runner._apersist_runs()

    try:
        resolution = resolve_pinned_model(requested_model, advertised_model_ids, provider=provider)
    except ReviewFixModelResolutionError as exc:
        blocked_reason = str(exc)
        await runner.mutate_review_fix(
            task_id,
            expected_revision=run.revision,
            action="model_resolution_failed",
            to_state=ReviewFixState.BLOCKED_MODEL_RESOLUTION,
            mutate=lambda current: setattr(current, "blocked_reason", blocked_reason),
        )
        return runner.get_review_fix(task_id)

    run = await runner.mutate_review_fix(
        task_id,
        expected_revision=run.revision,
        action="model_resolved",
        mutate=lambda current: setattr(current, "model", resolution),
    )
    overlap = review_fix_git.dirty_overlap(
        target, [path for group in groups for path in group.affected_files]
    )
    if overlap:
        run = await runner.mutate_review_fix(
            task_id,
            expected_revision=run.revision,
            action="dirty_overlap_detected",
            to_state=ReviewFixState.BLOCKED_DIRTY_OVERLAP,
            mutate=lambda current: setattr(current, "blocked_reason", ", ".join(overlap)[:2000]),
        )
        return run
    run = await runner.mutate_review_fix(
        task_id,
        expected_revision=run.revision,
        action="grouping_proposed",
        to_state=ReviewFixState.PLANNING,
        mutate=lambda current: setattr(current, "blocked_reason", ""),
    )
    return await runner.mutate_review_fix(
        task_id,
        expected_revision=run.revision,
        action="awaiting_group_confirmation",
        to_state=ReviewFixState.AWAITING_GROUP_CONFIRMATION,
        mutate=lambda current: None,
    )


async def capture_group_patch(
    runner: "TaskRunner",
    task_id: str,
    group_id: str,
    *,
    expected_revision: int,
    expected_group_revision: int,
) -> Any:
    """Capture a group patch and bump both group and task revisions."""
    run = runner.get_review_fix(task_id)
    metadata = run.review_fix
    assert metadata is not None
    group = runner.review_fix_group(run, group_id)
    if not group.affected_files:
        # An unscoped capture would hash a whole-worktree diff as this group's
        # patch; capture only proves something for owned paths.
        raise ReviewFixPlanError(
            f"group {group_id} owns no files; cannot capture an unscoped patch",
            code="fileless_group",
        )
    patch = await review_fix_git.candidate_patch(
        metadata.git.candidate_worktree_path,
        metadata.target.head_sha,
        group.affected_files,
    )
    return await runner.mutate_review_fix(
        task_id,
        expected_revision=expected_revision,
        expected_group_revision=expected_group_revision,
        group_id=group_id,
        action="group_patch_captured",
        mutate=lambda current: _update_group_patch(current, group_id, patch),
    )


def _update_group_patch(
    metadata: ReviewFixMetadata, group_id: str, patch: review_fix_git.ReviewFixPatch
) -> None:
    group = next(group for group in metadata.groups if group.group_id == group_id)
    group.candidate_patch_id = patch.patch_id
    group.candidate_base_sha = metadata.target.head_sha
    group.candidate_head_sha = metadata.target.head_sha
    group.revision += 1
    metadata.diff_paths = sorted(set(metadata.diff_paths) | set(patch.paths))


def artifact_root(metadata: ReviewFixMetadata, artifact_dir: str | Path | None = None) -> Path:
    """Resolve the per-task artifact directory and refuse an unsafe one.

    Group ids become filenames inside this directory, so the directory itself is
    the trust boundary: a link anywhere on its parent chain (or a junction, on
    Windows) would redirect writes out of the task-owned candidate, and a root
    outside the candidate worktree would let one task's artifacts overwrite
    another's. Both are rejected rather than sanitized.
    """
    candidate_raw = metadata.git.candidate_worktree_path
    if not candidate_raw:
        raise ReviewFixPlanError("review-fix candidate worktree is unavailable")
    candidate = Path(candidate_raw).expanduser().resolve()
    root = (
        Path(artifact_dir).expanduser() if artifact_dir else candidate
    ) / ".kirocrew-review-fix-artifacts"
    resolved = root.resolve()
    linked = platform_compat.first_linked_ancestor(resolved)
    if linked or platform_compat.is_link_or_junction(resolved):
        raise ReviewFixPlanError("review-fix artifact directory is not a real directory")
    try:
        resolved.relative_to(candidate)
    except ValueError as exc:
        raise ReviewFixPlanError("review-fix artifact directory escapes the candidate") from exc
    return resolved


async def validate_group(
    runner: "TaskRunner",
    task_id: str,
    group_id: str,
    *,
    expected_revision: int,
    expected_group_revision: int,
    test_command: Sequence[str],
    build_command: Sequence[str],
    artifact_dir: str | Path | None = None,
) -> tuple[Any, bool]:
    """Run the required full test and build commands and persist bounded artifacts."""
    run = runner.get_review_fix(task_id)
    metadata = run.review_fix
    assert metadata is not None
    runner.review_fix_group(run, group_id)
    validation_state = metadata.state
    if validation_state not in {
        ReviewFixState.AWAITING_VALIDATION,
        ReviewFixState.BLOCKED_VALIDATION,
    }:
        raise ReviewFixPlanError("task is not ready for validation")
    if not test_command or not build_command:
        raise ReviewFixPlanError("full test and build commands are required")
    await runner.mutate_review_fix(
        task_id,
        expected_revision=expected_revision,
        expected_group_revision=expected_group_revision,
        group_id=group_id,
        action="validation_started",
        expected_state=validation_state,
        to_state=(
            ReviewFixState.AWAITING_VALIDATION
            if validation_state is ReviewFixState.BLOCKED_VALIDATION
            else None
        ),
        mutate=lambda current: _set_group_state(current, group_id, "validating"),
    )
    current = runner.get_review_fix(task_id)
    current_group = runner.review_fix_group(current, group_id)
    root = artifact_root(metadata, artifact_dir)
    root.mkdir(parents=True, exist_ok=True)
    validations: list[ReviewFixValidationRun] = []
    for kind, command in (("test", test_command), ("build", build_command)):
        started = time.time()
        passed, output = await run_tests(list(command), Path(metadata.git.candidate_worktree_path))
        # A missing executable comes back as ("success", sentinel) from the
        # shared Task Runner helper. That skip-as-pass semantic serves the
        # generic runner, but here the bool IS the validation evidence — a
        # test/build command that never ran must not unlock Apply, or a typo'd
        # command would mint a passing validation record (exit_code=0) for a
        # fix that was never checked.
        if passed and output == TESTS_SKIPPED_OUTPUT:
            passed = False
        finished = time.time()
        safe_output = redact_credentials(redact_exfiltration_urls(output or "")[0])[0]
        safe_output = safe_output[:_MAX_ARTIFACT_BYTES]
        artifact_path = root / f"{group_id}-{kind}-{int(started)}.log"
        # Off-loop: an artifact can carry up to 256KB of captured output, and a
        # synchronous write here would stall every other dashboard request.
        await asyncio.to_thread(artifact_path.write_text, safe_output, encoding="utf-8")
        validations.append(
            ReviewFixValidationRun(
                validation_id=f"{group_id}-{kind}-{int(started * 1000)}",
                group_id=group_id,
                group_revision=current_group.revision,
                kind=kind,
                command=[str(value) for value in command],
                exit_code=0 if passed else 1,
                passed=passed,
                artifact_path=str(artifact_path),
                started_at=started,
                finished_at=finished,
                duration_secs=max(0.0, finished - started),
            )
        )
    passed = all(item.passed for item in validations)
    current = runner.get_review_fix(task_id)
    metadata_now = current.review_fix
    assert metadata_now is not None
    # Advance the TASK phase only when every group has finished THIS group's
    # outcome. Groups validate one at a time (the shared candidate worktree is
    # a serialized resource), so moving the whole task to READY_TO_APPLY /
    # BLOCKED_VALIDATION on the FIRST result would strand the others: their
    # Validate button is gated on the task being in a validation state, and
    # AWAITING_VALIDATION admits no path back to RUNNING that reopens it. A
    # not-yet-finished sibling keeps the task in AWAITING_VALIDATION
    # (to_state=None, no transition); the last group out flips it.
    siblings_finished = all(
        group.group_id == group_id or _group_validation_phase_done(group)
        for group in metadata_now.groups
    )
    if siblings_finished:
        # Sibling OUTCOME matters, not just completion: one failed group must
        # block the task even when the last group to finish passed.
        siblings_passed = all(
            _group_validation_passed(group)
            for group in metadata_now.groups
            if group.group_id != group_id
        )
        to_state = (
            ReviewFixState.READY_TO_APPLY
            if passed and siblings_passed
            else ReviewFixState.BLOCKED_VALIDATION
        )
    else:
        to_state = None
    return (
        await runner.mutate_review_fix(
            task_id,
            expected_revision=current.revision,
            expected_group_revision=current_group.revision,
            group_id=group_id,
            action="validation_finished",
            to_state=to_state,
            expected_state=ReviewFixState.AWAITING_VALIDATION,
            mutate=lambda current_metadata: _finish_group_validation(
                current_metadata, group_id, validations, passed
            ),
        ),
        passed,
    )


def _set_group_state(metadata: ReviewFixMetadata, group_id: str, state: str) -> None:
    group = next(group for group in metadata.groups if group.group_id == group_id)
    group.state = type(group.state)(state)
    group.revision += 1


def _group_validation_phase_done(group: ReviewFixDependencyGroup) -> bool:
    """Has this group finished its validation phase, pass or fail?

    ``READY_TO_APPLY`` is written by ``_finish_group_validation`` on a pass.
    A FAIL lands the group back at ``PROPOSED`` — which is also the
    fresh-from-planning state — so the state alone cannot distinguish "never
    validated" from "validated and failed"; a PROPOSED group only counts as
    finished when it carries recorded validation runs. ``validating`` (set by
    ``validation_started``) and every other lifecycle state mean the group is
    still inside its validation phase.
    """
    return group.state is ReviewFixGroupState.READY_TO_APPLY or (
        group.state is ReviewFixGroupState.PROPOSED and bool(group.validation_runs)
    )


def _group_validation_passed(group: ReviewFixDependencyGroup) -> bool:
    """Did this group's finished validation phase pass?

    The group state is the outcome of the LATEST attempt (``_finish_group_
    validation`` overwrites it and revalidation rewrites it), so read the
    state rather than the run history — history accumulates across
    revalidation attempts and an old failed run would poison the verdict.
    """
    return group.state is ReviewFixGroupState.READY_TO_APPLY


def _finish_group_validation(
    metadata: ReviewFixMetadata,
    group_id: str,
    validations: list[ReviewFixValidationRun],
    passed: bool,
) -> None:
    group = next(group for group in metadata.groups if group.group_id == group_id)
    group.validation_runs.extend(validations)
    group.state = type(group.state).READY_TO_APPLY if passed else type(group.state).PROPOSED
    group.revision += 1
    metadata.artifact_paths.extend(item.artifact_path for item in validations if item.artifact_path)
    metadata.artifact_paths = metadata.artifact_paths[-100:]
