"""GitLab reads: merge requests, their pipelines and discussions, and issues.

Every ``glab`` call carries the ref's validated host, so an allowlisted
self-managed instance is never read at the same project path on gitlab.com. The
CI glyph comes from the pipeline aggregate; the per-job list is display only.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from kiro_crew.dashboard.source_providers import projection, runner
from kiro_crew.dashboard.source_providers.contract import SourceProviderError, SourceRef


def _gitlab_status_bucket(status: str) -> str:
    """Map a single GitLab *job* status to a check bucket for the Checks list.

    This is the per-job display vocabulary and is deliberately FAITHFUL: a job
    that failed buckets as ``failed`` even when it is ``allow_failure`` (GitLab
    shows such a job as failed-but-allowed, and hiding it as ``skipped`` made the
    Checks tab claim "all checks passed" while a job was red). The single CI
    glyph is NOT rolled up from these job buckets for GitLab — it comes from the
    pipeline aggregate via ``_gitlab_aggregate_ci`` — so a faithful failed job
    here never diverges the chip from the full-payload projection.
    """
    state = status.lower()
    if state in {"success", "passed"}:
        return "passed"
    if state in {"skipped", "manual"}:
        return "skipped"
    if state in {"failed", "canceled", "cancelled"}:
        return "failed"
    return "pending"


def _gitlab_aggregate_ci(status: str) -> str | None:
    """Project a GitLab *pipeline aggregate* status to the chip CI vocabulary.

    Single source of truth for the GitLab CI glyph, called by BOTH the chip path
    (which reads the pipeline aggregate directly) and the full-payload path (via
    ``ciStatus`` stamped by ``_fetch_gitlab``). Because both sides call this one
    function on the same aggregate value, the two projections cannot drift by
    construction — regardless of how the vocabulary is mapped below.

    The aggregate is authoritative and lossless for the glyph: GitLab folds
    ``allow_failure`` failures into a ``success`` aggregate, so an allowed
    failure correctly reads "passed" here while its job still shows failed in the
    Checks list. ``manual`` is a *blocking* gate (the pipeline is waiting on a
    manual action), so it maps to ``running`` — not ``passed`` — because work is
    still outstanding.
    """
    state = status.lower()
    if state in {"success", "passed"}:
        return "passed"
    if state in {"failed", "canceled", "cancelled"}:
        return "failed"
    if state == "skipped":
        # A wholly skipped pipeline has no failures and nothing outstanding.
        return "passed"
    if not state:
        return None
    # running / pending / created / scheduled / preparing / waiting_for_resource
    # and manual (a blocking manual gate is still outstanding work).
    return "running"


# GitLab detailed_merge_status values mapped onto the GitHub-style
# merge-state vocabulary the frontend renders.
_GITLAB_MERGE_STATE_MAP = {
    "mergeable": "clean",
    "conflict": "dirty",
    # need_rebase keeps its own value: on fast-forward-only projects a merge
    # commit cannot unblock the MR, so it must not be conflated with "behind".
    "need_rebase": "need_rebase",
    "ci_must_pass": "blocked",
    "ci_still_running": "unstable",
    "discussions_not_resolved": "blocked",
    "not_approved": "blocked",
    "blocked_status": "blocked",
    "external_status_checks": "blocked",
    "jira_association_missing": "blocked",
    "requested_changes": "blocked",
    "status_checks_must_pass": "blocked",
    "policies_denied": "blocked",
    "security_policy_violations": "blocked",
    "merge_request_blocked": "blocked",
    "draft_status": "draft",
}


def _gitlab_merge_state(details: dict[str, Any]) -> tuple[str, str]:
    """Normalize GitLab merge fields to (mergeable, mergeStateStatus).

    ``detailed_merge_status`` is authoritative; the deprecated legacy
    ``merge_status`` is consulted only when the detailed field is absent
    (it can be stale or coarse and must never override the detailed value).
    """
    detailed = str(details.get("detailed_merge_status") or "").lower()
    legacy = str(details.get("merge_status") or "").lower()
    if detailed:
        if detailed == "conflict":
            mergeable = "conflicting"
        elif detailed == "mergeable":
            mergeable = "mergeable"
        else:
            mergeable = "unknown"
        return mergeable, _GITLAB_MERGE_STATE_MAP.get(detailed, "unknown")
    if legacy == "cannot_be_merged":
        mergeable = "conflicting"
    elif legacy == "can_be_merged":
        mergeable = "mergeable"
    else:
        mergeable = "unknown" if legacy else ""
    return mergeable, ""


async def _gitlab_settled_merge_state(
    ref: SourceRef, mr_api: str, details: dict[str, Any]
) -> tuple[str, str]:
    """Merge state for a GitLab MR, re-reading while it is still being computed.

    GitLab exposes ``detailed_merge_status`` only on the merge-request endpoint,
    so the re-read repeats that request and takes the merge fields from it. Same
    failure posture as the GitHub path: degrade to the original value.
    """
    mergeable, merge_state = _gitlab_merge_state(details)
    if projection._merge_state_settled(mergeable, merge_state):
        return mergeable, merge_state
    for _ in range(projection._MERGE_STATE_REREADS):
        await asyncio.sleep(projection._MERGE_STATE_REREAD_DELAY_SECS)
        try:
            data = await runner._run_json("glab", "api", mr_api)
        except SourceProviderError:
            break
        if not isinstance(data, dict):
            break
        reread, reread_state = _gitlab_merge_state(data)
        if projection._merge_state_settled(reread, reread_state):
            return reread, reread_state
    return mergeable, merge_state


def _gitlab_pipeline_as_check(pipeline: dict[str, Any]) -> dict[str, Any]:
    """Represent a whole pipeline as a single check row.

    A pipeline standing in for its jobs must keep PIPELINE-level semantics: a
    ``manual`` pipeline is blocked on a required job, so it is marked as a
    required gate (``allow_failure: False``) rather than falling through to the
    job-level reading that treats a lone manual step as skipped -- which the
    frontend would then roll up as passed.
    """
    record = {**pipeline, "name": "Pipeline"}
    if str(pipeline.get("status") or "").lower() == "manual":
        record["allow_failure"] = False
    return _gitlab_check(record)


def _gitlab_check(item: dict[str, Any]) -> dict[str, Any]:
    status = str(item.get("status") or "").lower()
    bucket = _gitlab_status_bucket(status)
    if status == "manual" and item.get("allow_failure") is False:
        # A required manual job is an unsatisfied gate, not an optional step that
        # can be treated as skipped: rolling it up as passed would show green
        # while the pipeline is still blocked on a human action.
        bucket = "pending"
    return {
        "name": item.get("name") or "Job",
        "workflow": item.get("stage") or "",
        "status": status.upper(),
        "conclusion": status.upper(),
        "bucket": bucket,
        "url": item.get("web_url") or "",
        "startedAt": item.get("started_at") or "",
        "completedAt": item.get("finished_at") or "",
    }


async def _fetch_gitlab(ref: SourceRef) -> dict[str, Any]:
    project = quote(ref.project, safe="")
    mr_api = f"projects/{project}/merge_requests/{ref.number}"
    details = await runner._run_json("glab", "api", mr_api, host=ref.host)
    if not isinstance(details, dict):
        raise SourceProviderError("GitLab returned an invalid merge-request payload")

    # Secondary endpoints degrade to empty sections instead of failing the
    # whole panel: the primary payload above already carries the core data.
    commits_raw: Any
    discussions_raw: Any
    changes_raw: Any
    pipelines_raw: Any
    merge_state_raw: Any
    commits_raw, discussions_raw, changes_raw, pipelines_raw, merge_state_raw = (
        await asyncio.gather(
            runner._run_json(
                "glab",
                "api",
                f"{mr_api}/commits?per_page={projection._SECONDARY_PAGE_SIZE}",
                host=ref.host,
            ),
            runner._run_json(
                "glab",
                "api",
                f"{mr_api}/discussions?per_page={projection._SECONDARY_PAGE_SIZE}",
                max_output_bytes=runner._DISCUSSION_OUTPUT_BYTES,
                host=ref.host,
            ),
            runner._run_json(
                "glab",
                "api",
                f"{mr_api}/changes",
                max_output_bytes=runner._DIFF_OUTPUT_BYTES,
                host=ref.host,
            ),
            runner._run_json("glab", "api", f"{mr_api}/pipelines?per_page=20", host=ref.host),
            # Runs alongside the secondary calls so its re-read wait overlaps
            # with fetches this request was making anyway.
            _gitlab_settled_merge_state(ref, mr_api, details),
            return_exceptions=True,
        )
    )
    partial_sections: list[str] = []
    for raw_value, section in (
        (commits_raw, "commits"),
        (discussions_raw, "review discussions"),
        (changes_raw, "files"),
        (pipelines_raw, "checks"),
    ):
        if isinstance(raw_value, BaseException):
            projection._mark_partial(partial_sections, section)
    commits = projection._or_empty(commits_raw)
    discussions = projection._or_empty(discussions_raw)
    changes = projection._or_empty(changes_raw)
    pipelines = projection._or_empty(pipelines_raw)
    commit_rows = projection._as_list(commits)
    discussion_rows = projection._as_list(discussions)
    if len(commit_rows) >= projection._SECONDARY_PAGE_SIZE:
        projection._mark_partial(partial_sections, "commits")
    if len(discussion_rows) >= projection._SECONDARY_PAGE_SIZE:
        projection._mark_partial(partial_sections, "review discussions")

    jobs: Any = []
    pipeline_rows = projection._as_list(pipelines)
    if pipeline_rows and pipeline_rows[0].get("id"):
        try:
            jobs = await runner._run_json(
                "glab",
                "api",
                f"projects/{project}/pipelines/{pipeline_rows[0]['id']}/jobs?per_page={projection._SECONDARY_PAGE_SIZE}",
                host=ref.host,
            )
        except SourceProviderError:
            projection._mark_partial(partial_sections, "checks")
            jobs = []
    if len(projection._as_list(jobs)) >= projection._SECONDARY_PAGE_SIZE:
        # A full page of jobs may be truncated — a failed job on a later page
        # would be invisible in this list. The CI glyph is projected from the
        # pipeline AGGREGATE (`ciStatus` below), which stays authoritative
        # regardless, but flag the Checks LIST as partial so the panel does not
        # imply it is exhaustive.
        projection._mark_partial(partial_sections, "checks")

    raw_changes = changes.get("changes") if isinstance(changes, dict) else []
    change_rows = projection._as_list(raw_changes)
    reported_change_count = str(details.get("changes_count") or "").rstrip("+")
    if (isinstance(changes, dict) and changes.get("overflow")) or (
        reported_change_count.isdigit() and int(reported_change_count) > len(change_rows)
    ):
        projection._mark_partial(partial_sections, "files")
    normalized_files = []
    for item in change_rows:
        if item.get("deleted_file"):
            status = "deleted"
        elif item.get("new_file"):
            status = "added"
        elif item.get("renamed_file"):
            status = "renamed"
        else:
            status = "modified"
        patch = item.get("diff") or ""
        normalized_files.append(
            {
                "path": item.get("new_path") or item.get("old_path") or "",
                "status": status,
                "additions": sum(
                    1
                    for line in patch.splitlines()
                    if line.startswith("+") and not line.startswith("+++")
                ),
                "deletions": sum(
                    1
                    for line in patch.splitlines()
                    if line.startswith("-") and not line.startswith("---")
                ),
                "patch": patch,
            }
        )

    gitlab_comments = []
    for discussion in projection._as_list(discussions):
        thread_id = str(discussion.get("id") or "")
        for note in projection._as_list(discussion.get("notes")):
            if note.get("system"):
                continue
            gitlab_comments.append(
                {
                    "id": str(note.get("id") or ""),
                    "kind": "comment",
                    "author": projection._author(note.get("author")),
                    "body": note.get("body") or "",
                    "state": "",
                    "createdAt": note.get("created_at") or "",
                    "url": "",
                    "path": "",
                    "line": None,
                    "threadId": thread_id,
                    "resolvable": bool(note.get("resolvable")),
                    "resolved": bool(note.get("resolved")),
                }
            )

    gitlab_mergeable, gitlab_merge_state = (
        merge_state_raw if isinstance(merge_state_raw, tuple) else _gitlab_merge_state(details)
    )
    gitlab_checks = [_gitlab_check(item) for item in projection._as_list(jobs)]
    # The single CI glyph is projected from the pipeline AGGREGATE (authoritative
    # and lossless — GitLab folds allow_failure into it and marks a blocking
    # manual gate), NOT rolled up from the per-job buckets, so a truncated /
    # empty / allow_failure job list can never diverge the glyph from the chip
    # path (which reads the same aggregate). `checks` below is a faithful
    # display list only.
    pipeline_status = str(pipeline_rows[0].get("status") or "") if pipeline_rows else ""
    gitlab_ci = _gitlab_aggregate_ci(pipeline_status)
    if not gitlab_checks and pipeline_rows and pipeline_status and "checks" not in partial_sections:
        # A pipeline EXISTS but its jobs have not materialized yet (freshly
        # created pipeline). Synthesize a single "Pipeline" row from the
        # aggregate so the Checks tab is not empty while jobs spin up — a
        # pipeline-less MR keeps `checks` empty. Display-only; the glyph comes
        # from `ciStatus`.
        gitlab_checks = [_gitlab_check({**pipeline_rows[0], "name": "Pipeline"})]
    payload: dict[str, Any] = {
        "provider": "gitlab",
        # See _fetch_github: identity is the validated ref, not the provider's
        # web_url/iid. This matters most for a self-managed instance, whose
        # responses are outside the trust boundary the allowlist establishes.
        "url": ref.url,
        "number": ref.number,
        "title": details.get("title") or "",
        "description": details.get("description") or "",
        "state": details.get("state") or "",
        "draft": bool(details.get("draft") or details.get("work_in_progress")),
        "mergedAt": details.get("merged_at") or "",
        "mergeable": gitlab_mergeable,
        "mergeStateStatus": gitlab_merge_state,
        "autoMerge": bool(details.get("merge_when_pipeline_succeeds")),
        "updatedAt": details.get("updated_at") or "",
        "headBranch": details.get("source_branch") or "",
        "baseBranch": details.get("target_branch") or "",
        "headSha": details.get("sha") or "",
        "author": projection._author(details.get("author")),
        "additions": sum(item["additions"] for item in normalized_files),
        "deletions": sum(item["deletions"] for item in normalized_files),
        "changedFiles": len(normalized_files),
        "commits": [
            {
                "sha": item.get("id") or item.get("short_id") or "",
                "title": item.get("title") or "",
                "body": item.get("message") or "",
                "author": item.get("author_name") or "",
                "date": item.get("created_at") or item.get("committed_date") or "",
                "url": item.get("web_url") or "",
            }
            for item in commit_rows
        ],
        "checks": gitlab_checks,
        "comments": gitlab_comments,
        "files": normalized_files,
        "partialSections": partial_sections,
    }
    if gitlab_ci is not None:
        # Authoritative aggregate CI for the glyph; consumed by
        # `status_from_full_payload` so the full-payload projection matches the
        # chip path (which reads the same aggregate) exactly.
        payload["ciStatus"] = gitlab_ci
    return payload


async def _fetch_gitlab_checks(ref: SourceRef) -> list[dict[str, Any]]:
    project = quote(ref.project, safe="")
    mr_api = f"projects/{project}/merge_requests/{ref.number}"
    pipelines = await runner._run_json(
        "glab",
        "api",
        f"{mr_api}/pipelines?per_page=1",
        max_output_bytes=runner._CHECKS_OUTPUT_BYTES,
        host=ref.host,
    )
    pipeline_rows = projection._as_list(pipelines)
    if not pipeline_rows:
        return []
    pipeline = pipeline_rows[0]
    pipeline_id = pipeline.get("id")
    if not pipeline_id:
        return [_gitlab_pipeline_as_check(pipeline)]
    jobs = await runner._run_json(
        "glab",
        "api",
        f"projects/{project}/pipelines/{pipeline_id}/jobs?per_page={projection._SECONDARY_PAGE_SIZE}",
        max_output_bytes=runner._CHECKS_OUTPUT_BYTES,
        host=ref.host,
    )
    job_rows = projection._as_list(jobs)
    if not job_rows:
        return [_gitlab_pipeline_as_check(pipeline)]
    return [_gitlab_check(item) for item in job_rows]


def _gitlab_issue_reactions(details: dict[str, Any]) -> dict[str, int] | None:
    """Synthesize the reaction block from GitLab's up/down vote counters.

    GitLab's issue payload exposes only ``upvotes``/``downvotes``, not the full
    award-emoji breakdown, so the remaining counters stay zero rather than being
    fetched from a separate endpoint this phase does not need. ``total`` is the
    sum of what is actually known.
    """
    if "upvotes" not in details and "downvotes" not in details:
        return None
    plus1 = projection._int_or_zero(details.get("upvotes"))
    minus1 = projection._int_or_zero(details.get("downvotes"))
    reactions = {contract_key: 0 for contract_key, _ in projection._GITHUB_REACTION_KEYS}
    reactions["plus1"] = plus1
    reactions["minus1"] = minus1
    return {"total": plus1 + minus1, **reactions}


def _gitlab_linked_changes(related: Any) -> list[dict[str, Any]]:
    """Normalize GitLab's ``related_merge_requests`` reply to linked changes."""
    changes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in projection._as_list(related):
        url = projection._safe_https_url(item.get("web_url"))
        if not url or url in seen:
            continue
        seen.add(url)
        changes.append(
            {
                "provider": "gitlab",
                "url": url,
                "number": projection._int_or_zero(item.get("iid") or item.get("id")),
                "title": str(item.get("title") or ""),
                # GitLab says "opened"; the contract's state field is free-form
                # text but both providers should read the same in the panel.
                "state": "open" if item.get("state") == "opened" else str(item.get("state") or ""),
            }
        )
    return changes


async def _fetch_gitlab_issue(ref: SourceRef) -> dict[str, Any]:
    project = quote(ref.project, safe="")
    issue_api = f"projects/{project}/issues/{ref.number}"
    # with_labels_details upgrades `labels` from bare names to objects carrying
    # the colour the panel renders; without it every label would be colourless.
    details = await runner._run_json(
        "glab", "api", f"{issue_api}?with_labels_details=true", host=ref.host
    )
    if not isinstance(details, dict):
        raise SourceProviderError("GitLab returned an invalid issue payload")

    # Secondary endpoints degrade to empty sections instead of failing the
    # whole panel: the primary payload above already carries the core data.
    notes_raw: Any
    related_raw: Any
    notes_raw, related_raw = await asyncio.gather(
        runner._run_json(
            "glab",
            "api",
            f"{issue_api}/notes?per_page={projection._SECONDARY_PAGE_SIZE}",
            max_output_bytes=runner._DISCUSSION_OUTPUT_BYTES,
            host=ref.host,
        ),
        runner._run_json(
            "glab",
            "api",
            f"{issue_api}/related_merge_requests",
            host=ref.host,
        ),
        return_exceptions=True,
    )
    partial_sections: list[str] = []
    if isinstance(notes_raw, BaseException):
        projection._mark_partial(partial_sections, "comments")
    if isinstance(related_raw, BaseException):
        projection._mark_partial(partial_sections, "linked changes")
    note_rows = projection._as_list(projection._or_empty(notes_raw))
    if len(note_rows) >= projection._SECONDARY_PAGE_SIZE:
        projection._mark_partial(partial_sections, "comments")

    comments = []
    for note in note_rows:
        if note.get("system"):
            # Label/milestone/state churn, not discussion.
            continue
        note_id = str(note.get("id") or "")
        comments.append(
            {
                "id": note_id,
                "author": projection._author(note.get("author")),
                "body": str(note.get("body") or ""),
                "createdAt": str(note.get("created_at") or ""),
                # GitLab notes carry no permalink of their own, so anchor off the
                # VALIDATED ref url rather than any provider-echoed link.
                "url": f"{ref.url}#note_{note_id}" if note_id else "",
            }
        )
    reported_comment_count = projection._int_or_zero(details.get("user_notes_count"))
    if reported_comment_count > len(comments) and "comments" not in partial_sections:
        projection._mark_partial(partial_sections, "comments")

    return {
        "provider": "gitlab",
        # See _fetch_github_issue: identity is the validated ref, not the
        # provider's web_url/iid. This matters most for a self-managed instance,
        # whose responses are outside the trust boundary the allowlist sets.
        "url": ref.url,
        "number": ref.number,
        "title": str(details.get("title") or ""),
        "description": str(details.get("description") or ""),
        # GitLab says "opened"; the contract's vocabulary is open/closed.
        "state": "open" if details.get("state") == "opened" else str(details.get("state") or ""),
        # GitLab has no equivalent of GitHub's state_reason.
        "stateReason": "",
        "author": projection._author(details.get("author")),
        "createdAt": str(details.get("created_at") or ""),
        "updatedAt": str(details.get("updated_at") or ""),
        "closedAt": str(details.get("closed_at") or ""),
        "closedBy": projection._author(details.get("closed_by")),
        "labels": projection._issue_labels(details.get("labels")),
        "assignees": [
            name
            for name in (
                projection._author(item) for item in projection._as_list(details.get("assignees"))
            )
            if name
        ],
        "milestone": projection._issue_milestone(details.get("milestone")),
        "commentCount": reported_comment_count or len(comments),
        "locked": bool(details.get("discussion_locked")),
        "reactions": _gitlab_issue_reactions(details),
        "comments": comments,
        "linkedChanges": _gitlab_linked_changes(projection._or_empty(related_raw)),
        "partialSections": partial_sections,
    }
