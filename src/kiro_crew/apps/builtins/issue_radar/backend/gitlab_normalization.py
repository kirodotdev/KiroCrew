"""Pure GitLab payload normalization for Issue Radar's shared data shapes."""

from __future__ import annotations

import re


def _rows(data: object) -> list[dict]:
    """Coerce an API response into a list of dictionaries."""
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _obj(data: object) -> dict:
    return data if isinstance(data, dict) else {}


def _norm_state(state: object) -> str:
    """Translate GitLab's open states to the vocabulary shared by the UI."""
    text = str(state or "").lower()
    if text in {"opened", "locked"}:
        return "open"
    return text or "open"


def _hex_color(value: object) -> str:
    text = str(value or "").strip().lstrip("#")
    return text or "888888"


def _label_names(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(name) for name in raw if isinstance(name, str) and name]


def _username(user: object) -> str | None:
    if not isinstance(user, dict):
        return None
    name = user.get("username")
    return str(name) if name else None


def _usernames(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [username for username in (_username(item) for item in raw) if username]


_ACCESS_LEVEL_ROLES = (
    (50, "admin"),
    (40, "maintain"),
    (30, "write"),
    (20, "triage"),
    (10, "read"),
)


def _role_for_access_level(level: object) -> str:
    if not isinstance(level, (int, str)):
        return "read"
    try:
        value = int(level)
    except (TypeError, ValueError):
        return "read"
    for threshold, role in _ACCESS_LEVEL_ROLES:
        if value >= threshold:
            return role
    return "read"


def _permissions_for_access_level(level: int) -> dict:
    """Map GitLab access without overstating a Reporter's push rights."""
    return {
        "admin": level >= 50,
        "maintain": level >= 40,
        "push": level >= 30,
        "triage": level >= 20,
        "pull": level >= 10,
    }


def _access_level(details: dict) -> int:
    """Return the highest direct or inherited project access level."""
    permissions = _obj(details.get("permissions"))
    best = 0
    for key in ("project_access", "group_access"):
        entry = _obj(permissions.get(key))
        try:
            level = int(entry.get("access_level") or 0)
        except (TypeError, ValueError):
            level = 0
        best = max(best, level)
    return best


def _norm_issue(raw: dict) -> dict:
    """Normalize one GitLab issue to the shared list-row shape."""
    return {
        "number": raw.get("iid"),
        "title": raw.get("title") or "",
        "url": raw.get("web_url") or "",
        "labels": _label_names(raw.get("labels")),
        "comments": raw.get("user_notes_count") or 0,
        "reactions": (raw.get("upvotes") or 0) + (raw.get("downvotes") or 0),
        "thumbs_up": raw.get("upvotes") or 0,
        "author_association": None,
        "updated_at": raw.get("updated_at"),
        "created_at": raw.get("created_at"),
        "state": _norm_state(raw.get("state")),
        "author": _username(raw.get("author")),
        "assignees": _usernames(raw.get("assignees")),
        "body": raw.get("description") or "",
    }


def _norm_issue_detail(raw: dict, labels_by_name: dict[str, dict]) -> dict:
    """Normalize one issue, enriching GitLab's label names with label metadata."""
    upvotes = raw.get("upvotes") or 0
    downvotes = raw.get("downvotes") or 0
    total = upvotes + downvotes
    milestone = _obj(raw.get("milestone"))
    return {
        "number": raw.get("iid"),
        "title": raw.get("title") or "",
        "body": raw.get("description") or "",
        "state": _norm_state(raw.get("state")),
        "state_reason": None,
        "url": raw.get("web_url") or "",
        "author": _username(raw.get("author")),
        "author_association": None,
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "closed_at": raw.get("closed_at"),
        "closed_by": _username(raw.get("closed_by")),
        "comments": raw.get("user_notes_count") or 0,
        "locked": bool(raw.get("discussion_locked")),
        "labels": [
            {
                "name": name,
                "color": _hex_color(labels_by_name.get(name, {}).get("color")),
                "description": labels_by_name.get(name, {}).get("description") or "",
            }
            for name in _label_names(raw.get("labels"))
        ],
        "assignees": _usernames(raw.get("assignees")),
        "milestone": (
            {
                "title": milestone.get("title"),
                "state": milestone.get("state"),
                "due_on": milestone.get("due_date"),
            }
            if milestone
            else None
        ),
        "reactions": (
            {
                "total": total,
                "plus1": upvotes,
                "minus1": downvotes,
                "laugh": 0,
                "hooray": 0,
                "confused": 0,
                "heart": 0,
                "rocket": 0,
                "eyes": 0,
            }
            if total > 0
            else None
        ),
    }


def _shape_labels(raw: object) -> list[dict]:
    out: list[dict] = []
    for label in _rows(raw):
        if label.get("name"):
            out.append(
                {
                    "name": label.get("name"),
                    "color": _hex_color(label.get("color")),
                    "description": label.get("description") or "",
                }
            )
    return out


def derive_members(issues: list[dict]) -> list[dict]:
    """Return no inferred GitLab members when the authoritative endpoint fails."""
    # Issue authors may be external users, so treating them as members would grant
    # a false membership badge.
    del issues
    return []


_TITLE_CHANGE_RE = re.compile(r"changed title from \*\*(.*?)\*\* to \*\*(.*?)\*\*", re.DOTALL)
_MENTION_REF_RE = re.compile(r"mentioned in (issue|merge request) ([\w./-]*[!#])(\d+)")
_ASSIGNEE_RE = re.compile(r"@([A-Za-z0-9._-]+)")
_COMMIT_REF_RE = re.compile(r"mentioned in commit ([0-9a-f]{7,40})")


def norm_note(note: dict, *, patterns: tuple[tuple[str, str], ...]) -> dict | None:
    """Normalize a note using the facade's current system-note pattern table."""
    body = str(note.get("body") or "")
    created = note.get("created_at")
    actor = _username(note.get("author"))
    if not note.get("system"):
        return {
            "kind": "comment",
            "actor": actor,
            "created_at": created,
            "body": body,
            "author_association": None,
            "reactions": None,
        }

    low = body.lower()
    kind = next(
        (kind for prefix, kind in patterns if low.startswith(prefix)),
        None,
    )
    if kind is None:
        return None
    if kind in ("assigned", "unassigned"):
        match = _ASSIGNEE_RE.search(body)
        return {
            "kind": kind,
            "actor": actor,
            "created_at": created,
            "assignee": match.group(1) if match else None,
        }
    if kind == "renamed":
        match = _TITLE_CHANGE_RE.search(body)
        return {
            "kind": "renamed",
            "actor": actor,
            "created_at": created,
            "rename": {
                "from": match.group(1) if match else None,
                "to": match.group(2) if match else None,
            },
        }
    if kind == "cross-referenced":
        match = _MENTION_REF_RE.search(body)
        return {
            "kind": "cross-referenced",
            "actor": actor,
            "created_at": created,
            "source": {
                "number": int(match.group(3)) if match else None,
                "title": None,
                "url": None,
                "state": None,
                "is_pr": bool(match and match.group(2).endswith("!")),
            },
        }
    if kind == "referenced":
        match = _COMMIT_REF_RE.search(body)
        return {
            "kind": "referenced",
            "actor": actor,
            "created_at": created,
            "commit_id": match.group(1) if match else None,
        }
    if kind in ("milestoned", "demilestoned"):
        return {"kind": kind, "actor": actor, "created_at": created, "milestone": None}
    return None


def _norm_label_event(event: dict, labels_by_name: dict[str, dict]) -> dict | None:
    action = str(event.get("action") or "").lower()
    if action not in ("add", "remove"):
        return None
    label = _obj(event.get("label"))
    name = label.get("name")
    if not name:
        return None
    return {
        "kind": "labeled" if action == "add" else "unlabeled",
        "actor": _username(event.get("user")),
        "created_at": event.get("created_at"),
        "label": {
            "name": name,
            "color": _hex_color(
                label.get("color") or labels_by_name.get(str(name), {}).get("color")
            ),
        },
    }


def _norm_state_event(event: dict) -> dict | None:
    state = str(event.get("state") or "").lower()
    if state == "closed":
        return {
            "kind": "closed",
            "actor": _username(event.get("user")),
            "created_at": event.get("created_at"),
            "state_reason": None,
            "commit_id": None,
        }
    if state in ("reopened", "opened"):
        return {
            "kind": "reopened",
            "actor": _username(event.get("user")),
            "created_at": event.get("created_at"),
        }
    return None


_MERGEABLE_STATUSES = frozenset({"mergeable", "can_be_merged"})
_MERGE_STATUS_PENDING = frozenset({"checking", "unchecked", "preparing", ""})


def _mergeable(raw: dict) -> bool | None:
    status = str(raw.get("detailed_merge_status") or raw.get("merge_status") or "").lower()
    if status in _MERGE_STATUS_PENDING:
        return None
    return status in _MERGEABLE_STATUSES


_JOB_FAILURE_STATUSES = frozenset({"failed"})
_JOB_RUNNING_STATUSES = frozenset(
    {
        "running",
        "pending",
        "created",
        "waiting_for_resource",
        "preparing",
        "scheduled",
    }
)
_JOB_OTHER_STATUSES = frozenset({"canceled", "cancelled", "skipped", "manual"})


def _job_bucket(status: str, allow_failure: bool) -> str:
    """Map job status without treating allowed failures as blocking."""
    text = (status or "").lower()
    if text in _JOB_FAILURE_STATUSES:
        return "other" if allow_failure else "failure"
    if text in _JOB_RUNNING_STATUSES:
        return "running"
    if text == "success":
        return "success"
    return "other"


def _norm_job(job: dict) -> dict:
    status = str(job.get("status") or "")
    bucket = _job_bucket(status, bool(job.get("allow_failure")))
    stage = job.get("stage")
    return {
        "name": job.get("name") or "job",
        "status": "in_progress" if bucket == "running" else "completed",
        "conclusion": {
            "failure": "failure",
            "success": "success",
            "running": None,
        }.get(bucket, "neutral"),
        "bucket": bucket,
        "url": job.get("web_url"),
        "started_at": job.get("started_at") or job.get("created_at"),
        "completed_at": job.get("finished_at"),
        "summary": f"stage: {stage}" if stage else "",
        "app": "GitLab CI",
        "source": "gitlab-ci",
    }


_CHECK_BUCKETS = ("failure", "running", "success", "other")


def summarize_checks(checks: list[dict]) -> dict:
    """Summarize checks using the shared failure-first priority."""
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
    return {
        "checks_counts": counts,
        "checks_state": state,
        "checks_truncated": False,
    }


def _pipeline_summary(raw: dict) -> dict:
    """Derive cache-safe card enrichment from an MR's aggregate pipeline."""
    pipeline = _obj(raw.get("head_pipeline")) or _obj(raw.get("pipeline"))
    status = str(pipeline.get("status") or "").lower()
    counts = dict.fromkeys(_CHECK_BUCKETS, 0)
    state: str | None = None
    if status:
        state = _job_bucket(status, False)
        counts[state] = 1
    # GitLab's list payload has no diff counts; None avoids persisting a false 0.
    return {
        "additions": None,
        "deletions": None,
        "changed_files": None,
        "checks_state": state,
        "checks_counts": counts,
        "checks_truncated": False,
    }


def _norm_pull(raw: dict) -> dict:
    """Normalize one merge request to the shared pull-request row shape."""
    state = str(raw.get("state") or "").lower()
    row = {
        "number": raw.get("iid"),
        "title": raw.get("title") or "",
        "url": raw.get("web_url") or "",
        "state": "open" if state in ("opened", "locked") else "closed",
        "draft": bool(
            raw.get("draft") if raw.get("draft") is not None else raw.get("work_in_progress")
        ),
        "labels": _label_names(raw.get("labels")),
        "author": _username(raw.get("author")),
        "author_association": None,
        "updated_at": raw.get("updated_at"),
        "created_at": raw.get("created_at"),
        "closed_at": raw.get("closed_at"),
        "merged_at": raw.get("merged_at"),
        "assignees": _usernames(raw.get("assignees")),
        "requested_reviewers": _usernames(raw.get("reviewers")),
        "base": raw.get("target_branch"),
        "head": raw.get("source_branch"),
        # List rows must carry the reviewed commit so bulk approval can be pinned.
        "head_sha": _obj(raw.get("diff_refs")).get("head_sha") or raw.get("sha"),
        "body": raw.get("description") or "",
    }
    row.update(_pipeline_summary(raw))
    return row


def enrich_pulls(
    owner: str, repo: str, pulls: list[dict], state: str, *, host: str = ""
) -> list[dict]:
    """Return rows already enriched from GitLab's inline head pipeline."""
    del owner, repo, state, host
    return pulls


def enrich_pulls_by_number(
    owner: str, repo: str, pulls: list[dict], *, host: str = ""
) -> list[dict]:
    del owner, repo, host
    return pulls


def enrichment_complete(pulls: list[dict]) -> bool:
    """Keep incomplete rows out of the persistent list cache."""
    return all(pull.get("checks_counts") is not None for pull in pulls)


_PIPELINE_CANCELLABLE_STATES = frozenset(
    {
        "created",
        "waiting_for_resource",
        "preparing",
        "pending",
        "running",
        "scheduled",
    }
)
_PIPELINE_RETRYABLE_STATES = frozenset({"failed", "canceled", "cancelled"})
_PIPELINE_FINISHED_STATES = frozenset({"failed", "success", "canceled", "cancelled", "skipped"})
_PIPELINE_CONCLUSION = {"canceled": "cancelled"}


def _norm_pipeline_run(row: dict) -> dict | None:
    """Normalize a pipeline and expose only actions GitLab can honor."""
    if not row.get("id"):
        return None
    status = str(row.get("status") or "")
    finished = status in _PIPELINE_FINISHED_STATES
    return {
        "id": row.get("id"),
        "name": row.get("name") or f"pipeline #{row.get('iid') or row.get('id')}",
        "status": "completed" if finished else status,
        "conclusion": _PIPELINE_CONCLUSION.get(status, status) if finished else None,
        "url": row.get("web_url"),
        "event": row.get("source"),
        "created_at": row.get("created_at"),
        "cancellable": status in _PIPELINE_CANCELLABLE_STATES,
        "rerunnable": status in _PIPELINE_RETRYABLE_STATES,
    }
