"""HTTP adapters for review-fix tasks.

The adapter keeps Sage finding identity at the edge and delegates durable state,
CAS, candidate execution, and Git side effects to the core review-fix modules.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from aiohttp import web

from kiro_crew import review_fix_git
from kiro_crew.agent_sdk import advertised_model_ids
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.taskrunner import _gate_auto_approve, _sel
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.review_fix import (
    ReviewFixModelResolutionError,
    ReviewFixPlanError,
    artifact_root,
    build_review_fix_groups,
    capture_group_patch,
    create_review_fix_task,
    resolve_pinned_model,
    validate_group,
)
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.task_models import (
    ReviewFixGitRecord,
    ReviewFixGroupState,
    ReviewFixState,
)
from kiro_crew.task_reporter import build_status
from kiro_crew.taskrunner import ReviewFixConflict

logger = logging.getLogger(__name__)

# Serializes every target-mutating group action (apply/commit). The CAS
# (mutate_review_fix's expected_revision) runs AFTER the Git mutation by
# design — the state records what the tree already shows — so two concurrent
# actions at one revision both pass assert_target_unchanged and both apply to
# the real checkout; the loser's CAS is then rejected while its git changes
# remain in the tree, unrecorded. Holding this lock across inspect -> mutate
# -> persist makes that sequence atomic.
_GIT_MUTATION_LOCK = LoopBoundLock()

_CONFIRMATION_ACTIONS = {
    "confirm_grouping",
    "edit_soft_grouping",
    "resolve_model",
    "pause",
    "resume",
    "retry",
    "capture_group_patch",
    "validate_group",
    "apply_group",
    "commit_group",
    "push_preview",
    "push",
    "discard_candidate",
}

# Fields whose drift between the approved preview and the push moment means the
# user is no longer pushing what they were shown.
_PUSH_PREVIEW_FIELDS = ("remote", "branch", "upstream", "commits", "files", "diverged")


def _error(code: str, message: str, status: int = 400) -> web.Response:
    if status == 400:
        return web.json_response({"code": code, "error": message}, status=400)
    if status == 403:
        return web.json_response({"code": code, "error": message}, status=403)
    if status == 404:
        return web.json_response({"code": code, "error": message}, status=404)
    if status == 409:
        return web.json_response({"code": code, "error": message}, status=409)
    if status == 503:
        return web.json_response({"code": code, "error": message}, status=503)
    raise ValueError(f"unsupported review-fix error status: {status}")


def _safe_error(exc: Exception) -> str:
    text = redact_exfiltration_urls(str(exc))[0]
    return redact_credentials(text)[0][:2000]


def _payload(run) -> dict[str, Any]:
    status = build_status({run.task_id: run}, {})["runs"][0]
    metadata = run.review_fix
    return {
        "task_id": run.task_id,
        "revision": run.revision,
        "state": metadata.state.value if metadata else "",
        "run": status,
        "review_fix": metadata.to_dict() if metadata else None,
    }


def _active_advertised_ids(request: web.Request) -> list[str]:
    try:
        providers = request.app["state"].sessions.active_providers()
    except (KeyError, AttributeError):
        return []
    for provider in reversed(providers):
        getter = getattr(provider, "available_models", None)
        if not callable(getter):
            continue
        try:
            ids = advertised_model_ids(getter())
        except Exception:
            continue
        if ids:
            return ids
    return []


def _requested_model(body: dict[str, Any]) -> str:
    value = body.get("model")
    if not isinstance(value, str) or value.strip().lower() in {"", "default", "agent"}:
        return str(KiroCrewConfig.load().agent.model or "")
    return value.strip()


def _advertised_ids(request: web.Request, body: dict[str, Any]) -> list[str]:
    raw = body.get("advertised_model_ids", body.get("advertised_models"))
    if isinstance(raw, list):
        if all(isinstance(item, str) for item in raw):
            return [item.strip() for item in raw if item.strip()]
        return advertised_model_ids(raw)
    return _active_advertised_ids(request)


def _findings(body: dict[str, Any]) -> list[Any]:
    raw = body.get("findings", body.get("finding_snapshots", []))
    return raw if isinstance(raw, list) else []


def _read_sage_report(run_id: str) -> dict[str, Any] | None:
    """Read one Sage report through the app's guarded report reader.

    This adapter is imported both by the Sage app and by Task Runner route tests,
    so the hyphenated app directory cannot be assumed to be on ``sys.path``.
    Invalid run ids return no report rather than allowing path repair to address a
    different persisted run.
    """
    import sys

    app_root = Path(__file__).resolve().parent.parent
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))
    from sage_lib import report as sage_report
    from sage_lib import store as sage_store

    if sage_store.safe_run_id(run_id) != run_id:
        return None
    payload = sage_report.read_report(None, run_id)
    return payload if isinstance(payload, dict) else None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else str(value or "").strip()


def _number(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


async def _validate_sage_findings(
    review_run_id: str,
    pr_url: str,
    findings: list[Any],
) -> None:
    """Fail closed unless every selected snapshot belongs to this Sage PR report.

    The browser is not an authority: a caller can submit a green row, a design-only
    row, or a finding copied from another run. Matching the immutable UI key and
    every canonical snapshot field against the persisted report prevents all three
    from becoming a fix task.
    """
    if not review_run_id:
        raise ReviewFixPlanError("review_run_id is required", code="review_run_required")
    if not pr_url:
        raise ReviewFixPlanError("pr_url is required", code="review_pr_required")
    report = await asyncio.to_thread(_read_sage_report, review_run_id)
    if report is None:
        raise ReviewFixPlanError(
            "the Sage review report is unavailable", code="review_report_unavailable"
        )
    rows = report.get("rows")
    if not isinstance(rows, list):
        raise ReviewFixPlanError(
            "the Sage review report is malformed", code="review_report_unavailable"
        )
    matching_rows = [
        row for row in rows if isinstance(row, dict) and _text(row.get("url")) == pr_url
    ]
    if len(matching_rows) != 1:
        raise ReviewFixPlanError(
            "the selected pull request is not owned by this review", code="finding_not_owned"
        )
    row = matching_rows[0]
    change_id = _text(row.get("change_id"))
    band = _text(row.get("band")).lower()
    row_findings = row.get("findings")
    if not change_id or not isinstance(row_findings, list):
        raise ReviewFixPlanError(
            "the Sage finding identity is unavailable", code="finding_not_owned"
        )
    if band not in {"red", "yellow"}:
        raise ReviewFixPlanError(
            "only red and yellow findings can be fixed", code="finding_not_eligible"
        )

    seen: set[str] = set()
    for raw in findings:
        if not isinstance(raw, dict):
            raise ReviewFixPlanError(
                "selected finding snapshot is invalid", code="finding_snapshot_mismatch"
            )
        key = raw.get("key")
        if not isinstance(key, str) or key in seen:
            raise ReviewFixPlanError(
                "selected finding identity is invalid", code="finding_not_owned"
            )
        seen.add(key)
        prefix = f"{change_id}:finding:"
        if not key.startswith(prefix) or not key[len(prefix) :].isdigit():
            raise ReviewFixPlanError(
                "selected finding is not owned by this pull request", code="finding_not_owned"
            )
        index = int(key[len(prefix) :])
        if index < 0 or index >= len(row_findings) or not isinstance(row_findings[index], dict):
            raise ReviewFixPlanError(
                "selected finding is not present in this report", code="finding_not_owned"
            )
        finding = row_findings[index]
        severity = _text(finding.get("severity") or finding.get("priority")).lower()
        if severity not in {"red", "yellow"}:
            raise ReviewFixPlanError(
                "only red and yellow findings can be fixed", code="finding_not_eligible"
            )
        kind_values = {
            _text(finding.get(name)).lower().replace("_", "-")
            for name in ("kind", "type", "category", "dimension", "finding_type")
        }
        explicit_only = {
            _text(finding.get(name)).lower() for name in ("design_only", "policy_only")
        }
        if kind_values.intersection(
            {"design", "design-only", "policy", "policy-only"}
        ) or explicit_only.intersection({"1", "true", "yes"}):
            raise ReviewFixPlanError(
                "design-only and policy-only findings cannot be fixed", code="finding_not_eligible"
            )

        body = "\n\n".join(
            _text(finding.get(name))
            for name in ("observation", "consequence")
            if isinstance(finding.get(name), str) and finding.get(name).strip()
        )
        expected = {
            "title": _text(finding.get("headline") or finding.get("dimension") or row.get("title")),
            "severity": severity,
            "body": body,
            "file_path": _text(finding.get("file")),
            "line": _number(finding.get("line")),
            "end_line": _number(finding.get("end_line")),
            "fingerprint": _text(finding.get("fingerprint")),
            "suggested_fix": _text(finding.get("suggestion")),
        }
        actual = {name: raw.get(name) for name in expected}
        if actual["title"] is not None:
            actual["title"] = _text(actual["title"])
        if actual["severity"] is not None:
            actual["severity"] = _text(actual["severity"]).lower()
        if actual["body"] is not None:
            actual["body"] = _text(actual["body"])
        if actual["file_path"] is not None:
            actual["file_path"] = _text(actual["file_path"])
        if actual["fingerprint"] is not None:
            actual["fingerprint"] = _text(actual["fingerprint"])
        if actual["suggested_fix"] is not None:
            actual["suggested_fix"] = _text(actual["suggested_fix"])
        actual["line"] = _number(actual["line"])
        actual["end_line"] = _number(actual["end_line"])
        if actual != expected:
            raise ReviewFixPlanError(
                "selected finding snapshot does not match the report",
                code="finding_snapshot_mismatch",
            )


async def _read_json(request: web.Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _runner(request: web.Request):
    state = request.app.get("state")
    return getattr(state, "task_runner", None)


async def handle_create_fix_task(request: web.Request) -> web.Response:
    runner = _runner(request)
    if runner is None:
        return _error("task_runner_unavailable", "task runner is not available", 503)
    body = await _read_json(request)
    if body is None:
        return _error("invalid_json", "request body must be an object")
    target_raw = body.get("target_path") or body.get("repository") or body.get("repo_path")
    if not isinstance(target_raw, str) or not target_raw.strip():
        return _error("target_required", "target_path is required")
    target_path = Path(target_raw).expanduser().resolve()
    if is_sensitive_path(str(target_path)):
        return _error("target_denied", "target_path is not allowed", 403)
    findings = _findings(body)
    if not findings:
        return _error("findings_required", "at least one finding is required")
    review_run_id = body.get("review_run_id")
    pr_url = body.get("pr_url")
    if not isinstance(review_run_id, str) or not review_run_id.strip():
        return _error("review_run_required", "review_run_id is required")
    if not isinstance(pr_url, str) or not pr_url.strip():
        return _error("review_pr_required", "pr_url is required")
    try:
        await _validate_sage_findings(review_run_id.strip(), pr_url.strip(), findings)
    except ReviewFixPlanError as exc:
        return _error(getattr(exc, "code", "invalid_review_fix"), _safe_error(exc))
    try:
        run = await create_review_fix_task(
            runner,
            target_path=target_path,
            findings=findings,
            review_run_id=str(body.get("review_run_id") or ""),
            pr_url=str(body.get("pr_url") or ""),
            source_head_sha=str(body.get("source_head_sha") or ""),
            target_mode=str(body.get("target_mode") or "current_branch"),
            requested_model=_requested_model(body),
            advertised_model_ids=_advertised_ids(request, body),
            provider=str(body.get("provider") or "acp"),
            raw_groups=body.get("groups") if isinstance(body.get("groups"), list) else None,
            task_id=str(body.get("task_id") or ""),
            name=str(body.get("name") or ""),
            candidate_root=(
                body.get("candidate_root") if isinstance(body.get("candidate_root"), str) else None
            ),
        )
    except (ReviewFixPlanError, ReviewFixModelResolutionError) as exc:
        return _error(getattr(exc, "code", "invalid_review_fix"), _safe_error(exc))
    except Exception as exc:
        logger.exception("review-fix task creation failed")
        return _error("review_fix_creation_failed", _safe_error(exc), 400)
    if run.review_fix and run.review_fix.state in {
        ReviewFixState.BLOCKED_MODEL_RESOLUTION,
        ReviewFixState.BLOCKED_DIRTY_OVERLAP,
    }:
        return web.json_response(_payload(run), status=202)
    return web.json_response(_payload(run), status=201)


async def handle_get_fix_task(request: web.Request) -> web.Response:
    runner = _runner(request)
    if runner is None:
        return _error("task_runner_unavailable", "task runner is not available", 503)
    try:
        return web.json_response(_payload(runner.get_review_fix(request.match_info["task_id"])))
    except ValueError:
        return _error("not_found", "review-fix task not found", 404)


async def _require_action_context(request: web.Request, body: dict[str, Any]):
    runner = _runner(request)
    if runner is None:
        raise web.HTTPServiceUnavailable(text="task runner is not available")
    try:
        run = runner.get_review_fix(request.match_info["task_id"])
    except ValueError as exc:
        raise web.HTTPNotFound(text="review-fix task not found") from exc
    expected = body.get("expected_revision")
    if not isinstance(expected, int) or isinstance(expected, bool):
        raise ValueError("expected_revision is required")
    target_fingerprint = body.get("target_fingerprint", body.get("expected_target_fingerprint"))
    if not isinstance(target_fingerprint, str) or not target_fingerprint:
        raise ValueError("target_fingerprint is required")
    action = body.get("action")
    if not isinstance(action, str) or action not in _CONFIRMATION_ACTIONS:
        raise ValueError("unsupported review-fix action")
    confirmation = body.get("confirmation_id") or body.get("confirmation_intent")
    if action in _CONFIRMATION_ACTIONS and not (confirmation or body.get("confirmed") is True):
        raise ValueError("confirmation intent is required")
    metadata = run.review_fix
    assert metadata is not None
    if run.revision != expected or metadata.revision != expected:
        raise ReviewFixConflict(run, "task revision is stale")
    if metadata.target.dirty_fingerprint != target_fingerprint:
        raise ReviewFixConflict(run, "target fingerprint is stale")
    return runner, run, action, expected, target_fingerprint


def _audit_action(task_id: str, action: str, outcome: str, error: str = "") -> None:
    try:
        _sel().log_tool_invocation(
            session_key="dashboard",
            source="review_fix",
            tool_name=action,
            outcome=outcome,
            metadata={"task_id": task_id, **({"error": error[:500]} if error else {})},
        )
    except Exception:
        logger.debug("review-fix action audit failed", exc_info=True)


def _push_preview_signature(preview: Mapping[str, Any]) -> tuple[Any, ...]:
    """Reduce a push preview to a comparable tuple (its lists become tuples).

    The approved preview is read back from persisted state, so its commit and
    file lists can arrive as any sequence shape; normalizing both sides keeps
    shape drift from reading as content drift.
    """
    signature: list[Any] = []
    for field in _PUSH_PREVIEW_FIELDS:
        value = preview.get(field)
        if isinstance(value, (list, tuple)):
            signature.append(tuple(str(item) for item in value))
        else:
            signature.append(value if value is not None else "")
    return tuple(signature)


async def _apply_group(runner, run, body, expected: int, fingerprint: str):
    metadata = run.review_fix
    assert metadata is not None
    group_id = str(body.get("group_id") or "")
    group = runner.review_fix_group(run, group_id)
    if group.state is not ReviewFixGroupState.READY_TO_APPLY:
        raise ValueError("group is not ready to apply")
    if metadata.state is not ReviewFixState.READY_TO_APPLY:
        raise ValueError("task is not ready to apply")
    current_target = await review_fix_git.inspect_target(
        metadata.target.target_path, mode=metadata.target.mode
    )
    review_fix_git.assert_target_unchanged(metadata.target, current_target)
    patch = await review_fix_git.candidate_patch(
        metadata.git.candidate_worktree_path,
        metadata.target.head_sha,
        group.affected_files,
    )
    if not patch.patch_text:
        raise ValueError("group has no candidate patch")
    # A group must be captured before it can be applied: the truthy-check this
    # replaces let an uncaptured group (id "") pass the id comparison and left
    # apply-vs-commit in a split state. Require the id, THEN compare it.
    if not group.candidate_patch_id:
        raise ValueError("group was never captured; capture before apply")
    if patch.patch_id != group.candidate_patch_id:
        raise ValueError("candidate changed after validation; re-capture and re-validate the group")
    patch_path = artifact_root(metadata) / f"{group_id}.patch"
    patch = await review_fix_git.write_patch(patch, patch_path)
    await review_fix_git.apply_patch(current_target, patch)
    applied_target = await review_fix_git.inspect_target(
        metadata.target.target_path, mode=metadata.target.mode
    )

    def mutate(current):
        item = next(item for item in current.groups if item.group_id == group_id)
        item.state = ReviewFixGroupState.APPLIED
        item.revision += 1
        item.apply_confirmed = True
        item.applied_at = asyncio.get_running_loop().time()
        # candidate_patch_id is already pinned by capture and verified equal to
        # the disk patch above, so apply only records where the artifact lives.
        item.patch_path = patch.patch_path
        item.diff_path = patch.patch_path
        current.target = applied_target

    # Mirror validate_group's "last group out" rule: moving the whole task to
    # AWAITING_COMMIT on the FIRST apply would strand the siblings outside the
    # apply phase — their Apply is gated on the task being READY_TO_APPLY, and
    # the transition table has no edge back. A not-yet-applied sibling keeps
    # the task in READY_TO_APPLY (to_state=None self-transition); the last one
    # flips it.
    siblings_pending = any(
        item.group_id != group_id and item.state is ReviewFixGroupState.READY_TO_APPLY
        for item in metadata.groups
    )
    return await runner.mutate_review_fix(
        run.task_id,
        expected_revision=expected,
        expected_target_fingerprint=fingerprint,
        expected_state=ReviewFixState.READY_TO_APPLY,
        expected_group_revision=group.revision,
        group_id=group_id,
        action="apply_group",
        to_state=None if siblings_pending else ReviewFixState.AWAITING_COMMIT,
        mutate=mutate,
    )


async def _commit_group(runner, run, body, expected: int, fingerprint: str):
    metadata = run.review_fix
    assert metadata is not None
    group_id = str(body.get("group_id") or "")
    group = runner.review_fix_group(run, group_id)
    if group.state is not ReviewFixGroupState.APPLIED:
        raise ValueError("group is not applied")
    if metadata.state is not ReviewFixState.AWAITING_COMMIT:
        raise ValueError("task is not awaiting commit")
    message = body.get("commit_message")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("commit_message is required")
    current_target = await review_fix_git.inspect_target(
        metadata.target.target_path, mode=metadata.target.mode
    )
    review_fix_git.assert_target_unchanged(metadata.target, current_target)
    commit_sha = await review_fix_git.commit_group(
        current_target.repo_root,
        group.affected_files,
        message,
    )
    committed_target = await review_fix_git.inspect_target(
        metadata.target.target_path, mode=metadata.target.mode
    )

    def mutate(current):
        item = next(item for item in current.groups if item.group_id == group_id)
        item.state = ReviewFixGroupState.COMMITTED
        item.revision += 1
        item.commit_hash = commit_sha
        item.commit_message = message.strip()[:500]
        current.target = committed_target
        current.git.destination_branch = committed_target.branch_name

    # Same "last group out" rule on the commit edge: flipping the task to
    # COMMITTED while a sibling is still APPLIED-but-uncommitted would strand
    # that sibling's commit (COMMITTED only reaches AWAITING_PUSH/DONE). The
    # task holds in AWAITING_COMMIT (to_state=None is a legal self-transition)
    # until the last group commits.
    siblings_uncommitted = any(
        item.group_id != group_id and item.state is ReviewFixGroupState.APPLIED
        for item in metadata.groups
    )
    return await runner.mutate_review_fix(
        run.task_id,
        expected_revision=expected,
        expected_target_fingerprint=fingerprint,
        expected_state=ReviewFixState.AWAITING_COMMIT,
        expected_group_revision=group.revision,
        group_id=group_id,
        action="commit_group",
        to_state=None if siblings_uncommitted else ReviewFixState.COMMITTED,
        mutate=mutate,
    )


async def _action(
    request: web.Request,
    runner,
    run,
    action: str,
    body: dict[str, Any],
    expected: int,
    fingerprint: str,
):
    metadata = run.review_fix
    assert metadata is not None
    if action == "confirm_grouping":
        if metadata.state is not ReviewFixState.AWAITING_GROUP_CONFIRMATION:
            raise ValueError("task is not awaiting grouping confirmation")

        def mutate(current):
            for group in current.groups:
                group.state = ReviewFixGroupState.CONFIRMED
                group.revision += 1

        return await runner.mutate_review_fix(
            run.task_id,
            expected_revision=expected,
            expected_target_fingerprint=fingerprint,
            expected_state=ReviewFixState.AWAITING_GROUP_CONFIRMATION,
            action=action,
            mutate=mutate,
        )
    if action == "edit_soft_grouping":
        if metadata.state is not ReviewFixState.AWAITING_GROUP_CONFIRMATION:
            raise ValueError("task is not awaiting grouping confirmation")
        replacement = body.get("groups")
        if not isinstance(replacement, list):
            raise ValueError("groups is required")
        groups = build_review_fix_groups(metadata.finding_snapshots, replacement)
        old_hard = [set(group.finding_keys) for group in metadata.groups if group.hard]
        new_by_key = {key: group for group in groups for key in group.finding_keys}
        if any(
            {new_by_key[key].group_id for key in keys} != {new_by_key[next(iter(keys))].group_id}
            for keys in old_hard
        ):
            raise ValueError("hard dependency groups cannot be split")
        return await runner.mutate_review_fix(
            run.task_id,
            expected_revision=expected,
            expected_target_fingerprint=fingerprint,
            expected_state=ReviewFixState.AWAITING_GROUP_CONFIRMATION,
            action=action,
            mutate=lambda current: setattr(current, "groups", groups),
        )
    if action == "resolve_model":
        if metadata.state is not ReviewFixState.BLOCKED_MODEL_RESOLUTION:
            raise ValueError("task is not blocked on model resolution")
        requested = body.get("model")
        if not isinstance(requested, str) or not requested.strip():
            raise ValueError("model is required")
        resolution = resolve_pinned_model(
            requested,
            _advertised_ids(request, body),
            provider=str(body.get("provider") or metadata.model.provider or "acp"),
        )

        def apply_model_resolution(current):
            current.model = resolution
            current.blocked_reason = ""

        return await runner.mutate_review_fix(
            run.task_id,
            expected_revision=expected,
            expected_target_fingerprint=fingerprint,
            expected_state=ReviewFixState.BLOCKED_MODEL_RESOLUTION,
            action=action,
            to_state=ReviewFixState.AWAITING_GROUP_CONFIRMATION,
            mutate=apply_model_resolution,
        )
    if action in {"resume", "retry"}:
        if action == "retry" and metadata.state not in {
            ReviewFixState.BLOCKED_VALIDATION,
            ReviewFixState.FAILED,
            ReviewFixState.PAUSED,
        }:
            raise ValueError("task is not retryable")
        if action == "resume" and metadata.state not in {
            ReviewFixState.AWAITING_GROUP_CONFIRMATION,
            ReviewFixState.PAUSED,
            ReviewFixState.BLOCKED_VALIDATION,
            ReviewFixState.FAILED,
        }:
            raise ValueError("task is not resumable")
        # Same provenance gate the core launch endpoints use: an app-embedded
        # caller cannot mint per-run trust on a review-fix resume either. The run
        # already exists, so there is no source claim to check (None, as in
        # /execute) and a denied request still proceeds — it just runs untrusted.
        auto_approve = await _gate_auto_approve(
            request, body.get("auto_approve") is True, None, endpoint="review_fix_resume"
        )
        return await runner.execute_review_fix(
            run.task_id,
            agent=str(body.get("agent") or ""),
            fresh=bool(body.get("fresh", False)),
            auto_approve=auto_approve,
        )
    if action == "pause":
        if metadata.state is not ReviewFixState.RUNNING:
            raise ValueError("task is not running")
        await runner.mutate_review_fix(
            run.task_id,
            expected_revision=expected,
            expected_target_fingerprint=fingerprint,
            expected_state=ReviewFixState.RUNNING,
            action=action,
            to_state=ReviewFixState.PAUSED,
            mutate=lambda current: None,
        )
        runner.pause(run.task_id)
        return runner.get_review_fix(run.task_id)
    if action == "capture_group_patch":
        if metadata.state not in {
            ReviewFixState.AWAITING_VALIDATION,
            ReviewFixState.BLOCKED_VALIDATION,
        }:
            raise ValueError("task is not ready to capture a candidate patch")
        group_id = str(body.get("group_id") or "")
        runner.review_fix_group(run, group_id)
        group_revision = body.get("expected_group_revision")
        if not isinstance(group_revision, int) or isinstance(group_revision, bool):
            raise ValueError("expected_group_revision is required")
        return await capture_group_patch(
            runner,
            run.task_id,
            group_id,
            expected_revision=expected,
            expected_group_revision=group_revision,
        )
    if action == "validate_group":
        if metadata.state not in {
            ReviewFixState.AWAITING_VALIDATION,
            ReviewFixState.BLOCKED_VALIDATION,
        }:
            raise ValueError("task is not ready for validation")
        group_id = str(body.get("group_id") or "")
        runner.review_fix_group(run, group_id)
        group_revision = body.get("expected_group_revision")
        if not isinstance(group_revision, int) or isinstance(group_revision, bool):
            raise ValueError("expected_group_revision is required")
        test_command = body.get("test_command")
        build_command = body.get("build_command")
        if (
            not isinstance(test_command, list)
            or not test_command
            or not all(isinstance(value, str) for value in test_command)
        ):
            raise ValueError("test_command and build_command are required")
        if (
            not isinstance(build_command, list)
            or not build_command
            or not all(isinstance(value, str) for value in build_command)
        ):
            raise ValueError("test_command and build_command are required")
        result, _passed = await validate_group(
            runner,
            run.task_id,
            group_id,
            expected_revision=expected,
            expected_group_revision=group_revision,
            test_command=test_command,
            build_command=build_command,
        )
        return result
    if action == "apply_group":
        async with _GIT_MUTATION_LOCK:
            return await _apply_group(runner, run, body, expected, fingerprint)
    if action == "commit_group":
        async with _GIT_MUTATION_LOCK:
            return await _commit_group(runner, run, body, expected, fingerprint)
    if action == "push_preview":
        # Pushing publishes EVERY group's work, so a preview is only meaningful
        # once the whole task has committed: a mid-lifecycle COMMITTED state
        # (kept for sibling applies/commits by the last-group-out rule above)
        # must not open the push gate while un-applied or un-committed sibling
        # groups are still pending.
        if metadata.state is not ReviewFixState.AWAITING_PUSH and not (
            metadata.state is ReviewFixState.COMMITTED
            and all(g.state is ReviewFixGroupState.COMMITTED for g in metadata.groups)
        ):
            raise ValueError("not all groups are committed")
        preview = await review_fix_git.push_preview(
            metadata.target.repo_root,
            metadata.git.remote or metadata.target.remote,
            metadata.target.branch_name,
        )
        return await runner.mutate_review_fix(
            run.task_id,
            expected_revision=expected,
            expected_target_fingerprint=fingerprint,
            expected_state=metadata.state,
            action=action,
            to_state=ReviewFixState.AWAITING_PUSH,
            mutate=lambda current: setattr(current.git, "push_preview", preview),
        )
    if action == "push":
        if metadata.state is not ReviewFixState.AWAITING_PUSH:
            raise ValueError("push requires an approved push preview")
        fresh_preview = await review_fix_git.push_preview(
            metadata.target.repo_root,
            metadata.git.remote or metadata.target.remote,
            metadata.target.branch_name,
        )
        approved = metadata.git.push_preview
        # AWAITING_PUSH is only ever entered by push_preview, so an approved
        # preview exists here. Comparing what the user approved against what
        # would be pushed now is what makes "Push" mean the button's label: an
        # upstream advance or an edited branch since the preview must force a
        # new one rather than silently publishing different commits.
        if not isinstance(approved, Mapping) or _push_preview_signature(
            approved
        ) != _push_preview_signature(fresh_preview):
            raise ValueError("push preview is stale; request a new push preview")
        result = await review_fix_git.push(
            metadata.target.repo_root,
            metadata.git.remote or metadata.target.remote,
            metadata.target.branch_name,
        )
        return await runner.mutate_review_fix(
            run.task_id,
            expected_revision=expected,
            expected_target_fingerprint=fingerprint,
            expected_state=ReviewFixState.AWAITING_PUSH,
            action=action,
            to_state=ReviewFixState.PUSHED,
            mutate=lambda current: setattr(current.git, "push_result", result),
        )
    if action == "discard_candidate":
        if metadata.state is ReviewFixState.RUNNING:
            raise ValueError("cannot discard while execution is running")
        # Transition first, destroy second: the transition table rejects DONE
        # from most states, so removing the worktree first would leave a task
        # stranded mid-lifecycle with no candidate left to operate on. An
        # orphaned directory is recoverable; a bricked task is not.
        candidate_path = metadata.git.candidate_worktree_path
        repo_root = metadata.target.repo_root
        run = await runner.mutate_review_fix(
            run.task_id,
            expected_revision=expected,
            expected_target_fingerprint=fingerprint,
            expected_state=metadata.state,
            action=action,
            to_state=ReviewFixState.DONE,
            mutate=lambda current: setattr(current.git, "candidate_worktree_path", ""),
        )
        try:
            await review_fix_git.discard_candidate(
                ReviewFixGitRecord(candidate_worktree_path=candidate_path), repo_root
            )
        except Exception as exc:
            # The request still succeeds: the run is DONE. Record the orphan so
            # an operator can reclaim the directory instead of discovering it.
            # Detail is read eagerly: Python unbinds `exc` when this block ends.
            detail = _safe_error(exc)
            logger.warning("review-fix candidate removal failed: %s", detail)
            await runner.mutate_review_fix(
                run.task_id,
                expected_revision=run.revision,
                action="discard_cleanup_failed",
                mutate=lambda current, reason=detail: current.logs.append(
                    f"candidate worktree could not be removed: {reason}"
                ),
            )
        return run
    raise ValueError("unsupported review-fix action")


async def handle_fix_action(request: web.Request) -> web.Response:
    body = await _read_json(request)
    if body is None:
        return _error("invalid_json", "request body must be an object")
    task_id = request.match_info["task_id"]
    action = body.get("action")
    try:
        runner, run, action, expected, fingerprint = await _require_action_context(request, body)
        result = await _action(request, runner, run, action, body, expected, fingerprint)
        if isinstance(result, str):
            run = runner.get_review_fix(task_id)
            return web.json_response(
                {"ok": True, **_payload(run), "execution_task_id": result}, status=202
            )
        _audit_action(task_id, action, "success")
        return web.json_response({"ok": True, **_payload(runner.get_review_fix(task_id))})
    except web.HTTPException:
        raise
    except ReviewFixConflict as exc:
        _audit_action(task_id, str(action), "stale", _safe_error(exc))
        return web.json_response(
            {
                "code": exc.code,
                "error": _safe_error(exc),
                "task_id": exc.task_id,
                "revision": exc.current_revision,
                "state": exc.current_state,
                "group_revisions": exc.current_group_revisions,
            },
            status=409,
        )
    except ValueError as exc:
        _audit_action(task_id, str(action), "denied", _safe_error(exc))
        return _error("review_fix_action_rejected", _safe_error(exc), 409)
    except Exception as exc:
        logger.exception("review-fix action failed: %s", action)
        _audit_action(task_id, str(action), "error", _safe_error(exc))
        return _error("review_fix_action_failed", _safe_error(exc), 409)


async def handle_review_again(
    request: web.Request,
    review_again_handler: Callable[[web.Request, dict[str, Any]], Awaitable[web.Response]] | None,
) -> web.Response:
    """Start a re-review only after the user explicitly invokes this endpoint."""
    runner = _runner(request)
    if runner is None:
        return _error("task_runner_unavailable", "task runner is not available", 503)
    body = await _read_json(request)
    if body is None:
        return _error("invalid_json", "request body must be an object")
    try:
        run = runner.get_review_fix(request.match_info["task_id"])
        expected = body.get("expected_revision")
        fingerprint = body.get("target_fingerprint")
        if not isinstance(expected, int) or not isinstance(fingerprint, str):
            return _error(
                "review_again_context_required",
                "expected_revision and target_fingerprint are required",
            )
        if run.review_fix is None or run.review_fix.state is not ReviewFixState.PUSHED:
            return _error("review_again_not_ready", "task is not ready for re-review", 409)
        try:
            run = await runner.mutate_review_fix(
                run.task_id,
                expected_revision=expected,
                expected_target_fingerprint=fingerprint,
                expected_state=ReviewFixState.PUSHED,
                action="review_again",
                to_state=ReviewFixState.REREVIEWING,
                mutate=lambda current: None,
            )
            if review_again_handler is None:
                return web.json_response(
                    {"ok": True, **_payload(run), "review_run_id": ""}, status=202
                )
            forwarded = dict(body)
            forwarded["changes"] = [run.review_fix.pr_url] if run.review_fix else []
            return await review_again_handler(request, forwarded)
        except Exception:
            # The downstream handler can raise after the transition; leaving the
            # run in REREVIEWING with no review running would lock it out of both
            # re-review and discard. Restore only from REREVIEWING so a
            # legitimate concurrent move is never clobbered.
            current = runner.get_review_fix(run.task_id)
            state = current.review_fix.state if current.review_fix else None
            if state is ReviewFixState.REREVIEWING:
                try:
                    await runner.mutate_review_fix(
                        run.task_id,
                        expected_revision=current.revision,
                        expected_state=ReviewFixState.REREVIEWING,
                        action="review_again_rolled_back",
                        to_state=ReviewFixState.PUSHED,
                        mutate=lambda metadata: None,
                    )
                except Exception:
                    logger.warning(
                        "review-again rollback failed for %s", run.task_id, exc_info=True
                    )
            raise
    except ReviewFixConflict as exc:
        return web.json_response(
            {
                "code": exc.code,
                "error": _safe_error(exc),
                "revision": exc.current_revision,
                "state": exc.current_state,
            },
            status=409,
        )
    except ValueError as exc:
        return _error("review_again_rejected", _safe_error(exc), 409)


def register_fix_task_routes(
    app: web.Application,
    review_again_handler: (
        Callable[[web.Request, dict[str, Any]], Awaitable[web.Response]] | None
    ) = None,
) -> None:
    """Register the Sage fix-task endpoints.

    Task Runner's own review-fix routes are registered by the core dashboard
    router (``dashboard/routes/taskrunner.py``), which owns that surface.
    """
    app.router.add_post("/api/apps/code-review-sage/fix-tasks", handle_create_fix_task)
    app.router.add_get("/api/apps/code-review-sage/fix-tasks/{task_id}", handle_get_fix_task)
    app.router.add_post(
        "/api/apps/code-review-sage/fix-tasks/{task_id}/review-again",
        lambda request: handle_review_again(request, review_again_handler),
    )
