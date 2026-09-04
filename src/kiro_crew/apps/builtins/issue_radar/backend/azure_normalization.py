"""Pure Azure DevOps-to-Issue-Radar payload normalization."""

from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlparse

from .azure_transport import (
    _LEGACY_HOST_SUFFIX,
    AZURE_HOST,
    _bad_segment,
    _url_path_segments,
)
from .errors import ProviderCliError

_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_SYNTHETIC_LABEL_COLOR = "888888"


def _obj(data: object) -> dict:
    return data if isinstance(data, dict) else {}


def _values(data: object) -> list[dict]:
    """Read either Azure's wrapped collection shape or a bare array."""
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    inner = _obj(data).get("value")
    if isinstance(inner, list):
        return [row for row in inner if isinstance(row, dict)]
    return []


def _identity_login(raw: object) -> str | None:
    """Prefer Azure's stable human-typeable identity handle."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("uniqueName") or raw.get("displayName") or raw.get("principalName")
    text = str(name).strip() if name else ""
    return text or None


def _identity_logins(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [login for login in (_identity_login(item) for item in raw) if login]


def _identity_id(raw: object) -> str | None:
    """Return a validated Azure identity GUID."""
    if not isinstance(raw, dict):
        return None
    value = str(raw.get("id") or "").strip()
    return value if _GUID_RE.match(value) else None


def _tag_names(raw: object) -> list[str]:
    """Parse Azure's semicolon-delimited, case-sensitive tag field."""
    text = str(raw or "").strip()
    if not text:
        return []
    return [part.strip() for part in text.split(";") if part.strip()]


def _tags_field(names: list[str]) -> str:
    return "; ".join(names)


def _check_label(label: str) -> str:
    """Reject delimiters Azure cannot escape in its stored tag field."""
    name = str(label or "").strip()
    if not name:
        raise ProviderCliError("a tag name cannot be empty")
    if "," in name or ";" in name:
        raise ProviderCliError(
            f"Azure DevOps tags cannot contain ',' or ';' (got {label!r}) -- those are "
            "the delimiters the tag field is stored with"
        )
    if len(name) > 400:
        raise ProviderCliError(f"tag name is too long: {label!r}")
    return name


def _shape_labels(names: list[str]) -> list[dict]:
    """Shape colourless Azure tags with the shared neutral colour."""
    return [{"name": name, "color": _SYNTHETIC_LABEL_COLOR, "description": ""} for name in names]


def _work_item_url(org: str, project: str, number: int) -> str:
    """Compose the web URL omitted by field-selected batch hydration."""
    return (
        f"https://{AZURE_HOST}/{quote(org, safe='')}/{quote(project, safe='')}"
        f"/_workitems/edit/{int(number)}"
    )


def _field(fields: dict, name: str, default: object = None) -> object:
    value = fields.get(name)
    return default if value is None else value


def _norm_issue(raw: dict, *, org: str, project: str, closed_states: frozenset[str]) -> dict:
    """Normalize one work item for the shared list view."""
    fields = _obj(raw.get("fields"))
    number = raw.get("id")
    state = str(_field(fields, "System.State", "") or "")
    return {
        "number": number,
        "title": str(_field(fields, "System.Title", "") or ""),
        "url": _work_item_url(org, project, int(number)) if isinstance(number, int) else "",
        "labels": _tag_names(fields.get("System.Tags")),
        "comments": _field(fields, "System.CommentCount", 0),
        "reactions": 0,
        "thumbs_up": 0,
        "author_association": None,
        "updated_at": _field(fields, "System.ChangedDate"),
        "created_at": _field(fields, "System.CreatedDate"),
        "state": "closed" if state in closed_states else "open",
        "author": _identity_login(fields.get("System.CreatedBy")),
        "assignees": [
            login for login in (_identity_login(fields.get("System.AssignedTo")),) if login
        ],
        "body": str(_field(fields, "System.Description", "") or ""),
    }


def _norm_issue_detail(raw: dict, *, org: str, project: str, closed_states: frozenset[str]) -> dict:
    """Normalize one work item for the shared detail view."""
    fields = _obj(raw.get("fields"))
    number = raw.get("id")
    state = str(_field(fields, "System.State", "") or "")
    is_closed = state in closed_states
    iteration = str(_field(fields, "System.IterationPath", "") or "")
    return {
        "number": number,
        "title": str(_field(fields, "System.Title", "") or ""),
        "body": str(_field(fields, "System.Description", "") or ""),
        "state": "closed" if is_closed else "open",
        # Azure process reasons cannot be mapped honestly to GitHub's two values.
        "state_reason": None,
        "url": _work_item_url(org, project, int(number)) if isinstance(number, int) else "",
        "author": _identity_login(fields.get("System.CreatedBy")),
        "author_association": None,
        "created_at": _field(fields, "System.CreatedDate"),
        "updated_at": _field(fields, "System.ChangedDate"),
        # Do not substitute ChangedDate: it may describe a later unrelated edit.
        "closed_at": (_field(fields, "Microsoft.VSTS.Common.ClosedDate") if is_closed else None),
        "closed_by": (
            _identity_login(fields.get("Microsoft.VSTS.Common.ClosedBy")) if is_closed else None
        ),
        "comments": _field(fields, "System.CommentCount", 0),
        "locked": False,
        "labels": _shape_labels(_tag_names(fields.get("System.Tags"))),
        "assignees": [
            login for login in (_identity_login(fields.get("System.AssignedTo")),) if login
        ],
        "milestone": (
            {"title": iteration.rsplit("\\", 1)[-1], "state": None, "due_on": None}
            if iteration
            else None
        ),
        "reactions": None,
    }


_TRACKED_UPDATE_FIELDS = (
    "System.Tags",
    "System.State",
    "System.AssignedTo",
    "System.Title",
    "System.IterationPath",
)


def _update_actor(update: dict) -> str | None:
    return _identity_login(update.get("revisedBy"))


def _update_when(update: dict) -> object:
    fields = _obj(update.get("fields"))
    changed = _obj(fields.get("System.ChangedDate")).get("newValue")
    return changed or update.get("revisedDate")


def _tag_events(update: dict, actor: str | None, created: object) -> list[dict]:
    """Reconstruct label events by differencing Azure's whole tag field."""
    change = _obj(_obj(update.get("fields")).get("System.Tags"))
    before = set(_tag_names(change.get("oldValue")))
    after = set(_tag_names(change.get("newValue")))
    events: list[dict] = []
    for name in sorted(after - before):
        events.append(
            {
                "kind": "labeled",
                "actor": actor,
                "created_at": created,
                "label": {"name": name, "color": _SYNTHETIC_LABEL_COLOR},
            }
        )
    for name in sorted(before - after):
        events.append(
            {
                "kind": "unlabeled",
                "actor": actor,
                "created_at": created,
                "label": {"name": name, "color": _SYNTHETIC_LABEL_COLOR},
            }
        )
    return events


def _state_events(
    update: dict, actor: str | None, created: object, closed_states: frozenset[str]
) -> list[dict]:
    """Reconstruct only close/reopen transitions, dropping open-state churn."""
    change = _obj(_obj(update.get("fields")).get("System.State"))
    before = str(change.get("oldValue") or "")
    after = str(change.get("newValue") or "")
    if not after or before == after:
        return []
    was_closed, is_closed = before in closed_states, after in closed_states
    if is_closed and not was_closed:
        return [
            {
                "kind": "closed",
                "actor": actor,
                "created_at": created,
                "state_reason": None,
                "commit_id": None,
            }
        ]
    if was_closed and not is_closed:
        return [{"kind": "reopened", "actor": actor, "created_at": created}]
    return []


def _assignee_events(update: dict, actor: str | None, created: object) -> list[dict]:
    change = _obj(_obj(update.get("fields")).get("System.AssignedTo"))
    before = _identity_login(change.get("oldValue"))
    after = _identity_login(change.get("newValue"))
    events: list[dict] = []
    if before and before != after:
        events.append(
            {
                "kind": "unassigned",
                "actor": actor,
                "created_at": created,
                "assignee": before,
            }
        )
    if after and after != before:
        events.append(
            {
                "kind": "assigned",
                "actor": actor,
                "created_at": created,
                "assignee": after,
            }
        )
    return events


def _norm_update(update: dict, closed_states: frozenset[str]) -> list[dict]:
    """Normalize one work-item revision into zero or more timeline events."""
    fields = _obj(update.get("fields"))
    if not any(name in fields for name in _TRACKED_UPDATE_FIELDS):
        return []
    actor = _update_actor(update)
    created = _update_when(update)
    events: list[dict] = []
    events.extend(_tag_events(update, actor, created))
    events.extend(_state_events(update, actor, created, closed_states))
    events.extend(_assignee_events(update, actor, created))
    title = _obj(fields.get("System.Title"))
    if title.get("newValue") and title.get("oldValue") != title.get("newValue"):
        events.append(
            {
                "kind": "renamed",
                "actor": actor,
                "created_at": created,
                "rename": {"from": title.get("oldValue"), "to": title.get("newValue")},
            }
        )
    iteration = _obj(fields.get("System.IterationPath"))
    if iteration.get("newValue") and iteration.get("oldValue") != iteration.get("newValue"):
        events.append(
            {
                "kind": "milestoned",
                "actor": actor,
                "created_at": created,
                "milestone": str(iteration["newValue"]).rsplit("\\", 1)[-1],
            }
        )
    return events


def _norm_work_item_comment(comment: dict) -> dict:
    """Normalize a comment while preserving claim-ledger id and modified time."""
    modified = comment.get("modifiedDate") or comment.get("createdDate")
    return {
        "kind": "comment",
        "id": comment.get("id"),
        "actor": _identity_login(comment.get("createdBy")),
        "created_at": comment.get("createdDate"),
        "updated_at": modified,
        "body": str(comment.get("text") or ""),
        "author_association": None,
        "reactions": None,
    }


_VOTE_PROPERTY_KEYS = (
    "CodeReviewVoteResult",
    "Microsoft.TeamFoundation.Discussion.VoteResult",
)
_VOTE_STATES = {
    10: "APPROVED",
    5: "APPROVED",
    0: "COMMENTED",
    -5: "CHANGES_REQUESTED",
    -10: "CHANGES_REQUESTED",
}


def _thread_property(thread: dict, keys: tuple[str, ...]) -> object:
    """Unwrap the first matching Azure thread property."""
    properties = _obj(thread.get("properties"))
    for key in keys:
        if key in properties:
            entry = properties[key]
            if isinstance(entry, dict):
                return entry.get("$value")
            return entry
    return None


def _norm_thread_comment(thread: dict, comment: dict) -> dict | None:
    """Normalize a human PR comment; drop empty and system-generated noise."""
    if str(comment.get("commentType") or "") == "system":
        return None
    body = str(comment.get("content") or "")
    if not body.strip():
        return None
    context = _obj(thread.get("threadContext"))
    path = context.get("filePath")
    if not path:
        return {
            "kind": "comment",
            "id": comment.get("id"),
            "actor": _identity_login(comment.get("author")),
            "created_at": comment.get("publishedDate"),
            "updated_at": comment.get("lastUpdatedDate") or comment.get("publishedDate"),
            "body": body,
            "author_association": None,
            "reactions": None,
        }
    line = _obj(context.get("rightFileStart")).get("line") or _obj(
        context.get("leftFileStart")
    ).get("line")
    return {
        "kind": "review_comment",
        "actor": _identity_login(comment.get("author")),
        "created_at": comment.get("publishedDate"),
        "body": body,
        "author_association": None,
        "path": path,
        "line": line,
        "url": None,
    }


def _branch_name(ref: object) -> str | None:
    text = str(ref or "")
    if not text:
        return None
    return text[len("refs/heads/") :] if text.startswith("refs/heads/") else text


def _pr_labels(raw: object) -> list[str]:
    return [
        str(row.get("name")) for row in _values(raw) if row.get("name") and row.get("active", True)
    ]


def _norm_pull(raw: dict) -> dict:
    """Normalize one Azure pull request for the shared list view."""
    status = str(raw.get("status") or "").lower()
    merged = status == "completed"
    closed_at = raw.get("closedDate")
    return {
        "number": raw.get("pullRequestId"),
        "title": str(raw.get("title") or ""),
        "url": _pr_web_url(raw),
        "state": "open" if status == "active" else "closed",
        "draft": bool(raw.get("isDraft")),
        "labels": _pr_labels(raw.get("labels")),
        "author": _identity_login(raw.get("createdBy")),
        "author_association": None,
        # Azure exposes no PR modification timestamp.
        "updated_at": closed_at or raw.get("creationDate"),
        "created_at": raw.get("creationDate"),
        "closed_at": closed_at if status in ("completed", "abandoned") else None,
        "merged_at": closed_at if merged else None,
        "assignees": [],
        "requested_reviewers": _identity_logins(raw.get("reviewers")),
        "base": _branch_name(raw.get("targetRefName")),
        "head": _branch_name(raw.get("sourceRefName")),
        "head_sha": _obj(raw.get("lastMergeSourceCommit")).get("commitId"),
        "body": str(raw.get("description") or ""),
        # Per-PR policy enrichment decides whether this row is safe to persist.
        "additions": None,
        "deletions": None,
        "changed_files": None,
        "checks_state": None,
        "checks_counts": None,
        "checks_truncated": False,
    }


def _pr_web_url(raw: dict) -> str:
    web = _obj(_obj(raw.get("_links")).get("web")).get("href")
    if web:
        return str(web)
    repo = _obj(raw.get("repository"))
    project = _obj(repo.get("project")).get("name")
    org = _org_from_api_url(repo.get("url"))
    number = raw.get("pullRequestId")
    if not (project and org and repo.get("name") and number):
        return ""
    return (
        f"https://{AZURE_HOST}/{quote(str(org), safe='')}/{quote(str(project), safe='')}"
        f"/_git/{quote(str(repo['name']), safe='')}/pullrequest/{int(number)}"
    )


def _org_from_api_url(url: object) -> str | None:
    try:
        parsed = urlparse(str(url or ""))
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if host.endswith(_LEGACY_HOST_SUFFIX):
        candidate = host[: -len(_LEGACY_HOST_SUFFIX)]
        return candidate if not _bad_segment(candidate) else None
    if host == AZURE_HOST:
        segments = _url_path_segments(parsed.path or "")
        if segments:
            candidate = unquote(segments[0])
            return candidate if not _bad_segment(candidate) else None
    return None


_MERGE_SUCCEEDED = frozenset({"succeeded"})
_MERGE_PENDING = frozenset({"queued", "notset", "", "none"})


def _mergeable(raw: dict) -> bool | None:
    status = str(raw.get("mergeStatus") or "").lower()
    if status in _MERGE_PENDING:
        return None
    return status in _MERGE_SUCCEEDED


_EVALUATION_BUCKETS = {
    "approved": "success",
    "rejected": "failure",
    "broken": "failure",
    "queued": "running",
    "running": "running",
    "notapplicable": "other",
}
_BUILD_RESULT_BUCKETS = {
    "succeeded": "success",
    "failed": "failure",
    "partiallysucceeded": "other",
    "canceled": "other",
    "cancelled": "other",
    "none": "other",
}
_BUILD_RUNNING_STATUSES = frozenset({"notstarted", "inprogress", "postponed", "none", ""})
_BUILD_FINISHED_STATUSES = frozenset({"completed"})


def _evaluation_bucket(status: str) -> str:
    return _EVALUATION_BUCKETS.get((status or "").lower(), "other")


def _norm_evaluation(evaluation: dict) -> dict:
    status = str(evaluation.get("status") or "")
    bucket = _evaluation_bucket(status)
    config = _obj(evaluation.get("configuration"))
    policy_type = _obj(config.get("type"))
    display = str(policy_type.get("displayName") or "policy")
    settings = _obj(config.get("settings"))
    detail = str(settings.get("displayName") or "")
    blocking = bool(config.get("isBlocking", True))
    return {
        "name": detail or display,
        "status": "in_progress" if bucket == "running" else "completed",
        "conclusion": {"failure": "failure", "success": "success", "running": None}.get(
            bucket, "neutral"
        ),
        "bucket": bucket if blocking or bucket != "failure" else "other",
        "url": None,
        "started_at": evaluation.get("startedDate"),
        "completed_at": evaluation.get("completedDate"),
        "summary": display if detail else "",
        "app": "Azure DevOps policy",
        "source": str(policy_type.get("id") or "policy"),
    }


def _norm_build(build: dict) -> dict:
    status = str(build.get("status") or "").lower()
    result = str(build.get("result") or "").lower()
    if status in _BUILD_FINISHED_STATUSES:
        bucket = _BUILD_RESULT_BUCKETS.get(result, "other")
    elif status in _BUILD_RUNNING_STATUSES:
        bucket = "running"
    else:
        bucket = "other"
    definition = _obj(build.get("definition"))
    return {
        "name": str(definition.get("name") or build.get("buildNumber") or "build"),
        "status": "completed" if status in _BUILD_FINISHED_STATUSES else "in_progress",
        "conclusion": {"failure": "failure", "success": "success", "running": None}.get(
            bucket, "neutral"
        ),
        "bucket": bucket,
        "url": _obj(_obj(build.get("_links")).get("web")).get("href"),
        "started_at": build.get("startTime") or build.get("queueTime"),
        "completed_at": build.get("finishTime"),
        "summary": str(build.get("buildNumber") or ""),
        "app": "Azure Pipelines",
        "source": "azure-pipelines",
    }


_CHECK_BUCKETS = ("failure", "running", "success", "other")


def summarize_checks(checks: list[dict]) -> dict:
    """Summarize check buckets using the shared failure-first priority."""
    counts = dict.fromkeys(_CHECK_BUCKETS, 0)
    for row in checks:
        if not isinstance(row, dict):
            continue
        bucket = row.get("bucket")
        counts[bucket if isinstance(bucket, str) and bucket in counts else "other"] += 1
    for bucket in _CHECK_BUCKETS:
        if counts[bucket]:
            state: str | None = bucket
            break
    else:
        state = None
    return {"checks_counts": counts, "checks_state": state, "checks_truncated": False}


def enrichment_complete(pulls: list[dict]) -> bool:
    """Require every PR row's policy enrichment before persistence."""
    return all(pr.get("checks_counts") is not None for pr in pulls)
