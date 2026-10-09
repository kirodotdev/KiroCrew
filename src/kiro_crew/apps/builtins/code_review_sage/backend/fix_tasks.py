"""HTTP adapters for review-fix tasks.

The adapter keeps Sage finding identity at the edge and delegates durable state,
CAS, candidate execution, and Git side effects to the core review-fix modules.
"""

from __future__ import annotations

import asyncio
import logging
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from aiohttp import web

from kiro_crew import review_fix_git
from kiro_crew.agent_sdk import advertised_model_ids
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
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

# Group actions mutate Git before CAS persist, so this lock makes inspect ->
# mutate -> persist atomic and prevents an unrecorded concurrent apply.
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

# Drift in these fields means push would publish an unapproved preview.
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


def _redact_untrusted(value: Any) -> Any:
    """Redact credential and exfiltration material in model-written payload text."""
    if isinstance(value, str):
        return redact_credentials(redact_exfiltration_urls(value)[0])[0]
    if isinstance(value, list):
        return [_redact_untrusted(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_untrusted(item) for key, item in value.items()}
    return value


def _payload(run) -> dict[str, Any]:
    status = _redact_untrusted(build_status({run.task_id: run}, {})["runs"][0])
    metadata = run.review_fix
    return {
        "task_id": run.task_id,
        "revision": run.revision,
        "state": metadata.state.value if metadata else "",
        "run": status,
        "review_fix": _redact_untrusted(metadata.to_dict()) if metadata else None,
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
    """Load and validate a Sage report."""
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
    """Ensure requested findings exist in the Sage report."""
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
        # Matching alias order and normalization close the snapshot-smuggling gap.
        actual = {
            "title": _text(raw.get("title")) or _text(raw.get("headline")),
            "severity": (_text(raw.get("severity")) or _text(raw.get("priority"))).lower(),
            "body": (
                _text(raw.get("body")) or _text(raw.get("description")) or _text(raw.get("message"))
            ),
            "file_path": (
                _text(raw.get("file_path")) or _text(raw.get("path")) or _text(raw.get("file"))
            ),
            "line": _number(raw.get("line", raw.get("start_line"))),
            "end_line": _number(raw.get("end_line")),
            "fingerprint": _text(raw.get("fingerprint")),
            "suggested_fix": _text(raw.get("suggested_fix")) or _text(raw.get("fix")),
        }
        if actual != expected:
            raise ReviewFixPlanError(
                "selected finding snapshot does not match the report",
                code="finding_snapshot_mismatch",
            )


def _read_local_session(session_id: str) -> dict[str, Any] | None:
    """Load a local review session through its guarded loader."""
    import sys

    app_root = Path(__file__).resolve().parent.parent
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))
    from sage_lib import local_review as sage_local_review

    session = sage_local_review.load_session(session_id)
    return session if isinstance(session, dict) else None


async def _validate_local_findings(
    local_session_id: str,
    target_path: Path,
    findings: list[Any],
) -> list[dict[str, Any]]:
    """Select server-owned findings for a local review."""
    if not local_session_id:
        raise ReviewFixPlanError("local_session_id is required", code="local_session_required")
    session = await asyncio.to_thread(_read_local_session, local_session_id)
    if session is None:
        raise ReviewFixPlanError(
            "the local review session is unavailable", code="local_session_not_found"
        )
    session_repo_raw = session.get("repository")
    if not isinstance(session_repo_raw, str) or not session_repo_raw:
        raise ReviewFixPlanError(
            "the local review session is unavailable", code="local_session_not_found"
        )
    session_repo = Path(session_repo_raw).expanduser().resolve()
    if session_repo != target_path:
        raise ReviewFixPlanError(
            "target_path does not match the local review session's repository",
            code="target_repository_mismatch",
        )
    by_id: dict[str, dict] = {
        str(item["id"]): item
        for item in session.get("findings", [])
        if isinstance(item, dict) and item.get("id")
    }
    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    for raw in findings:
        if not isinstance(raw, dict):
            raise ReviewFixPlanError(
                "selected finding snapshot is invalid", code="finding_not_owned"
            )
        finding_id = _text(raw.get("id") or raw.get("key"))
        if not finding_id or finding_id in seen:
            raise ReviewFixPlanError(
                "selected finding identity is invalid", code="finding_not_owned"
            )
        seen.add(finding_id)
        server = by_id.get(finding_id)
        if server is None:
            raise ReviewFixPlanError(
                "selected finding is not present in this local review session",
                code="finding_not_owned",
            )
        # A mismatched fingerprint is stale cache, not a different finding.
        fingerprint = raw.get("fingerprint")
        if fingerprint is not None and _text(fingerprint) != _text(server.get("fingerprint")):
            raise ReviewFixPlanError(
                "selected finding does not match the session's record",
                code="finding_snapshot_mismatch",
            )
        selected.append(
            {
                "key": finding_id,
                "title": _text(server.get("title")),
                "severity": _text(server.get("severity")),
                "body": _text(server.get("message")),
                "file_path": _text(server.get("file")),
                "line": _number(server.get("line")),
                "end_line": _number(server.get("end_line")),
                "fingerprint": _text(server.get("fingerprint")),
                "suggested_fix": _text(server.get("suggestion")),
            }
        )
    if not selected:
        raise ReviewFixPlanError("at least one finding is required", code="findings_required")
    return selected


async def _read_json(request: web.Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _runner(request: web.Request):
    state = request.app.get("state")
    return getattr(state, "task_runner", None)


_FixTaskHandler = Callable[..., Awaitable[web.Response]]


def _require_enabled(handler: _FixTaskHandler) -> _FixTaskHandler:
    """Deny when code-review-sage is disabled."""

    @wraps(handler)
    async def _wrapped(request: web.Request, *args, **kwargs) -> web.Response:
        if not await asyncio.to_thread(is_app_enabled, "code-review-sage"):
            return web.json_response(
                {"code": "app_disabled", "error": "code-review-sage is disabled"}, status=403
            )
        return await handler(request, *args, **kwargs)

    return _wrapped


@_require_enabled
async def handle_create_fix_task(request: web.Request) -> web.Response:
    owner_denied = await require_owner_dashboard_request(request, "code_review_sage.fix_create")
    if owner_denied is not None:
        return owner_denied
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
    local_session_id = body.get("local_session_id")
    # Validate each mutually exclusive finding source against its durable record.
    is_local = isinstance(local_session_id, str) and bool(local_session_id.strip())
    has_review_run_id = isinstance(review_run_id, str) and review_run_id.strip()
    has_pr_url = isinstance(pr_url, str) and pr_url.strip()
    if is_local and (has_review_run_id or has_pr_url):
        return _error(
            "local_and_review_run_conflict",
            "local_session_id cannot be combined with review_run_id or pr_url",
        )
    if is_local:
        assert isinstance(local_session_id, str)
        try:
            findings = await _validate_local_findings(
                local_session_id.strip(), target_path, findings
            )
        except ReviewFixPlanError as exc:
            return _error(getattr(exc, "code", "invalid_review_fix"), _safe_error(exc))
    else:
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
            review_run_id="" if is_local else str(body.get("review_run_id") or ""),
            pr_url="" if is_local else str(body.get("pr_url") or ""),
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


@_require_enabled
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
    if action not in _CONFIRMATION_ACTIONS:
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
    """Normalize a push preview for comparison."""
    signature: list[Any] = []
    for field in _PUSH_PREVIEW_FIELDS:
        value = preview.get(field)
        if isinstance(value, (list, tuple)):
            signature.append(tuple(str(item) for item in value))
        else:
            signature.append(value if value is not None else "")
    return tuple(signature)


async def _apply_group(runner, run, body, expected: int, fingerprint: str):
    run, metadata = _revalidate_under_lock(runner, run.task_id, expected, fingerprint)
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
    # A captured id proves validation before apply.
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

    # Wait for the last apply so siblings are never stranded outside the phase.
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


def _revalidate_under_lock(runner, task_id: str, expected: int, fingerprint: str):
    """Re-read review-fix state under the Git lock."""
    run = runner.get_review_fix(task_id)
    metadata = run.review_fix
    assert metadata is not None
    if run.revision != expected or metadata.revision != expected:
        raise ReviewFixConflict(run, "task revision is stale")
    if metadata.target.dirty_fingerprint != fingerprint:
        raise ReviewFixConflict(run, "target fingerprint is stale")
    return run, metadata


async def _commit_group(runner, run, body, expected: int, fingerprint: str):
    run, metadata = _revalidate_under_lock(runner, run.task_id, expected, fingerprint)
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

    # Wait for the last commit so an uncommitted sibling is never stranded.
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
        current_target = await review_fix_git.inspect_target(
            metadata.target.target_path, mode=metadata.target.mode
        )
        overlap = review_fix_git.dirty_overlap(
            current_target,
            [path for group in metadata.groups for path in group.affected_files],
        )

        def apply_model_resolution(current):
            current.model = resolution
            current.blocked_reason = ""

        if overlap:
            return await runner.mutate_review_fix(
                run.task_id,
                expected_revision=expected,
                expected_target_fingerprint=fingerprint,
                expected_state=ReviewFixState.BLOCKED_MODEL_RESOLUTION,
                action=action,
                to_state=ReviewFixState.BLOCKED_DIRTY_OVERLAP,
                mutate=lambda current: setattr(
                    current, "blocked_reason", ", ".join(overlap)[:2000]
                ),
            )

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
        # App callers cannot mint per-run trust on resume; untrusted runs continue.
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
        if (
            not isinstance(test_command, list)
            or not test_command
            or not all(isinstance(value, str) for value in test_command)
        ):
            raise ValueError("test_command is required")
        # Absent means skip the build; present-but-malformed is a request error.
        build_command_raw = body.get("build_command")
        build_command: list[str] | None
        if not build_command_raw:
            build_command = None
        elif not isinstance(build_command_raw, list) or not all(
            isinstance(value, str) for value in build_command_raw
        ):
            raise ValueError("build_command must be a list of strings when provided")
        else:
            build_command = build_command_raw
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
        # Keep preview and its CAS persist atomic with a locked push.
        async with _GIT_MUTATION_LOCK:
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
        async with _GIT_MUTATION_LOCK:
            return await _push(runner, run.task_id, expected, fingerprint)
    if action == "discard_candidate":
        async with _GIT_MUTATION_LOCK:
            return await _discard_candidate(runner, run.task_id, expected, fingerprint)
    raise ValueError("unsupported review-fix action")


async def _push(runner, task_id: str, expected: int, fingerprint: str):
    # Revalidate the irreversible remote mutation under the shared lock.
    run, metadata = _revalidate_under_lock(runner, task_id, expected, fingerprint)
    if metadata.state is not ReviewFixState.AWAITING_PUSH:
        raise ValueError("push requires an approved push preview")
    fresh_preview = await review_fix_git.push_preview(
        metadata.target.repo_root,
        metadata.git.remote or metadata.target.remote,
        metadata.target.branch_name,
    )
    approved = metadata.git.push_preview
    # Push must publish exactly the approved preview, not a newer branch state.
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
        action="push",
        to_state=ReviewFixState.PUSHED,
        mutate=lambda current: setattr(current.git, "push_result", result),
    )


async def _discard_candidate(runner, task_id: str, expected: int, fingerprint: str):
    # Wait for an in-flight push before discarding its candidate.
    run, metadata = _revalidate_under_lock(runner, task_id, expected, fingerprint)
    if metadata.state is ReviewFixState.RUNNING:
        raise ValueError("cannot discard while execution is running")
    # Transition first; an orphan is recoverable, a bricked task is not.
    candidate_path = metadata.git.candidate_worktree_path
    repo_root = metadata.target.repo_root
    run = await runner.mutate_review_fix(
        run.task_id,
        expected_revision=expected,
        expected_target_fingerprint=fingerprint,
        expected_state=metadata.state,
        action="discard_candidate",
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


@_require_enabled
async def handle_fix_action(request: web.Request) -> web.Response:
    owner_denied = await require_owner_dashboard_request(request, "code_review_sage.fix_action")
    if owner_denied is not None:
        return owner_denied
    body = await _read_json(request)
    if body is None:
        return _error("invalid_json", "request body must be an object")
    task_id = request.match_info["task_id"]
    action = body.get("action")
    try:
        runner, run, action, expected, fingerprint = await _require_action_context(request, body)
        result = await _action(request, runner, run, action, body, expected, fingerprint)
        _audit_action(task_id, action, "success")
        if isinstance(result, str):
            run = runner.get_review_fix(task_id)
            return web.json_response(
                {"ok": True, **_payload(run), "execution_task_id": result}, status=202
            )
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


@_require_enabled
async def handle_review_again(
    request: web.Request,
    review_again_handler: Callable | None = None,
) -> web.Response:
    """Start a re-review only after the user explicitly invokes this endpoint."""
    owner_denied = await require_owner_dashboard_request(request, "code_review_sage.review_again")
    if owner_denied is not None:
        return owner_denied
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
            # Restore only from REREVIEWING and never clobber a concurrent move.
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
