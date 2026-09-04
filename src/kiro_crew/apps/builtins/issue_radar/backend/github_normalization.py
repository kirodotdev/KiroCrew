"""Pure GitHub response normalization for Issue Radar."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

MEMBER_ASSOC_RANK = {"OWNER": 3, "MEMBER": 2, "COLLABORATOR": 1}
CHECK_FAILURE_CONCLUSIONS = {
    "failure",
    "timed_out",
    "action_required",
    "startup_failure",
    "stale",
    "error",
}
CHECK_RUNNING_STATES = {
    "queued",
    "in_progress",
    "pending",
    "waiting",
    "requested",
    "expected",
}
CHECK_OTHER_CONCLUSIONS = {"neutral", "skipped", "cancelled", "canceled"}
CHECK_BUCKETS = ("failure", "running", "success", "other")

CREW_CLAIM_MARKER_RE = re.compile(r"<!--\s*kirocrew-crew\s+([^>]*?)\s*-->")
CREW_CLAIM_FIELD_RE = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)=(\S+)")
CREW_CLAIM_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def parse_gh_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def derive_members(issues: list[dict]) -> list[dict]:
    best: dict[str, str] = {}
    for issue in issues:
        login = issue.get("author")
        association = issue.get("author_association")
        if not login or association not in MEMBER_ASSOC_RANK:
            continue
        current = best.get(login)
        if current is None or MEMBER_ASSOC_RANK[association] > MEMBER_ASSOC_RANK[current]:
            best[login] = association
    return [
        {"login": login, "association": association} for login, association in sorted(best.items())
    ]


def norm_reactions(reactions: dict | None) -> dict | None:
    if not reactions:
        return None
    total = reactions.get("total_count") or 0
    if total <= 0:
        return None
    return {
        "total": total,
        "plus1": reactions.get("+1", 0),
        "minus1": reactions.get("-1", 0),
        "laugh": reactions.get("laugh", 0),
        "hooray": reactions.get("hooray", 0),
        "confused": reactions.get("confused", 0),
        "heart": reactions.get("heart", 0),
        "rocket": reactions.get("rocket", 0),
        "eyes": reactions.get("eyes", 0),
    }


def actor_login(event: dict) -> str | None:
    return (event.get("actor") or {}).get("login")


def normalize_timeline_event(
    event: dict,
    *,
    reaction_normalizer: Callable[[dict | None], dict | None] = norm_reactions,
    actor_reader: Callable[[dict], str | None] = actor_login,
) -> dict | None:
    event_type = event.get("event")
    created = event.get("created_at")
    if event_type == "commented":
        return {
            "kind": "comment",
            "id": event.get("id"),
            "actor": (event.get("user") or {}).get("login"),
            "created_at": created,
            "updated_at": event.get("updated_at"),
            "body": event.get("body") or "",
            "author_association": event.get("author_association"),
            "reactions": reaction_normalizer(event.get("reactions")),
        }
    if event_type in ("labeled", "unlabeled"):
        label = event.get("label") or {}
        return {
            "kind": event_type,
            "actor": actor_reader(event),
            "created_at": created,
            "label": {"name": label.get("name"), "color": label.get("color")},
        }
    if event_type in ("assigned", "unassigned"):
        return {
            "kind": event_type,
            "actor": actor_reader(event),
            "created_at": created,
            "assignee": (event.get("assignee") or {}).get("login"),
        }
    if event_type == "closed":
        return {
            "kind": "closed",
            "actor": actor_reader(event),
            "created_at": created,
            "state_reason": event.get("state_reason"),
            "commit_id": event.get("commit_id"),
        }
    if event_type == "reopened":
        return {"kind": "reopened", "actor": actor_reader(event), "created_at": created}
    if event_type == "renamed":
        rename = event.get("rename") or {}
        return {
            "kind": "renamed",
            "actor": actor_reader(event),
            "created_at": created,
            "rename": {"from": rename.get("from"), "to": rename.get("to")},
        }
    if event_type in ("milestoned", "demilestoned"):
        return {
            "kind": event_type,
            "actor": actor_reader(event),
            "created_at": created,
            "milestone": (event.get("milestone") or {}).get("title"),
        }
    if event_type == "cross-referenced":
        source = (event.get("source") or {}).get("issue") or {}
        return {
            "kind": "cross-referenced",
            "actor": actor_reader(event),
            "created_at": created,
            "source": {
                "number": source.get("number"),
                "title": source.get("title"),
                "url": source.get("html_url"),
                "state": source.get("state"),
                "is_pr": bool(source.get("pull_request")),
            },
        }
    if event_type == "referenced":
        return {
            "kind": "referenced",
            "actor": actor_reader(event),
            "created_at": created,
            "commit_id": event.get("commit_id"),
        }
    if event_type == "reviewed":
        return {
            "kind": "reviewed",
            "actor": (event.get("user") or {}).get("login"),
            "created_at": event.get("submitted_at") or created,
            "review_state": event.get("state"),
            "body": event.get("body") or "",
        }
    if event_type == "committed":
        author = event.get("author") or {}
        return {
            "kind": "committed",
            "actor": author.get("name") or (event.get("committer") or {}).get("name"),
            "created_at": author.get("date")
            or (event.get("committer") or {}).get("date")
            or created,
            "commit_id": event.get("sha"),
            "message": (event.get("message") or "").splitlines()[0] if event.get("message") else "",
        }
    return None


def dep_node_kind(is_pr: bool) -> str:
    return "pull" if is_pr else "issue"


def dep_node_state(state: Any, merged_at: Any = None) -> str:
    if merged_at:
        return "merged"
    normalized = str(state or "").lower()
    if normalized == "merged":
        return "merged"
    return "closed" if normalized == "closed" else "open"


def shape_labels(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    return [
        {
            "name": label.get("name"),
            "color": label.get("color") or "888888",
            "description": label.get("description") or "",
        }
        for label in raw
        if isinstance(label, dict) and label.get("name")
    ]


def check_bucket(status: str | None, conclusion: str | None) -> str:
    normalized_status = (status or "").lower()
    normalized_conclusion = (conclusion or "").lower()
    if normalized_status in CHECK_RUNNING_STATES or normalized_conclusion in CHECK_RUNNING_STATES:
        return "running"
    if normalized_conclusion in CHECK_FAILURE_CONCLUSIONS:
        return "failure"
    if normalized_conclusion == "success":
        return "success"
    return "other"


def check_identity(row: dict) -> tuple[str, str]:
    return (str(row.get("source") or ""), str(row.get("name") or ""))


def dedupe_checks(
    rows: list[dict],
    *,
    identity: Callable[[dict], tuple[str, str]] = check_identity,
) -> list[dict]:
    def timestamp_key(row: dict) -> tuple[str, str]:
        return (
            str(row.get("started_at") or ""),
            str(row.get("completed_at") or ""),
        )

    best: dict[tuple[str, str], dict] = {}
    for row in rows:
        if not row.get("name"):
            continue
        row_identity = identity(row)
        previous = best.get(row_identity)
        if previous is None or timestamp_key(row) >= timestamp_key(previous):
            best[row_identity] = row
    return list(best.values())


def count_context_buckets(
    contexts: object,
    *,
    dedupe: Callable[[list[dict]], list[dict]] = dedupe_checks,
    bucket: Callable[[str | None, str | None], str] = check_bucket,
) -> dict[str, int]:
    rows = [row for row in contexts if isinstance(row, dict)] if isinstance(contexts, list) else []
    counts = {name: 0 for name in CHECK_BUCKETS}
    for row in dedupe(rows):
        counts[bucket(row.get("status"), row.get("conclusion"))] += 1
    return counts


def lower_or_none(value: object) -> str | None:
    return value.strip().lower() or None if isinstance(value, str) else None


def graphql_mergeable(value: object) -> bool | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    if normalized == "MERGEABLE":
        return True
    if normalized == "CONFLICTING":
        return False
    return None


def rest_pr_state(value: object) -> str | None:
    normalized = lower_or_none(value)
    if normalized is None:
        return None
    return "closed" if normalized in ("closed", "merged") else normalized


def parse_summary_rows(
    stdout: str,
    *,
    mergeable_normalizer: Callable[[object], bool | None] = graphql_mergeable,
    state_normalizer: Callable[[object], str | None] = rest_pr_state,
    bucket: Callable[[str | None, str | None], str] = check_bucket,
    context_counter: Callable[[object], dict[str, int]] = count_context_buckets,
) -> dict[int, dict]:
    rows: dict[int, dict] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        number = row.get("number")
        if not isinstance(number, int):
            continue
        rollup = row.get("rollup")
        rows[number] = {
            "additions": row.get("additions") or 0,
            "deletions": row.get("deletions") or 0,
            "changed_files": row.get("changed_files") or 0,
            "head_sha": row.get("head_sha") or None,
            "mergeable_state": None,
            "mergeable": mergeable_normalizer(row.get("mergeable_raw")),
            "pr_state": state_normalizer(row.get("pr_state")),
            "pr_merged_at": row.get("pr_merged_at") or None,
            "checks_state": bucket(None, rollup) if rollup else None,
            "checks_counts": context_counter(row.get("contexts")),
            "checks_truncated": bool(row.get("contexts_truncated")),
        }
    return rows


def summarize_checks(checks: list[dict]) -> dict:
    counts = {bucket: 0 for bucket in CHECK_BUCKETS}
    for check in checks:
        if not isinstance(check, dict):
            continue
        bucket = check.get("bucket")
        counts[bucket if isinstance(bucket, str) and bucket in counts else "other"] += 1
    state = next((bucket for bucket in CHECK_BUCKETS if counts[bucket]), None)
    return {"checks_counts": counts, "checks_state": state, "checks_truncated": False}


def apply_summaries(
    pulls: list[dict],
    summaries: dict[int, dict],
    readiness: dict[int, str | None] | None = None,
) -> list[dict]:
    ready = readiness or {}
    for pull in pulls:
        number = pull.get("number")
        pull["mergeable_state"] = ready.get(number) if isinstance(number, int) else None
        extra = summaries.get(number) if isinstance(number, int) else None
        if not extra:
            pull["additions"] = None
            pull["deletions"] = None
            pull["changed_files"] = None
            pull["checks_state"] = None
            pull["checks_counts"] = None
            pull["checks_truncated"] = False
            pull["mergeable"] = None
            pull.setdefault("head_sha", None)
            continue
        pull["additions"] = extra.get("additions", 0)
        pull["deletions"] = extra.get("deletions", 0)
        pull["changed_files"] = extra.get("changed_files", 0)
        if not pull.get("head_sha"):
            pull["head_sha"] = extra.get("head_sha")
        pull["mergeable"] = extra.get("mergeable")
        live_state = extra.get("pr_state")
        if live_state:
            pull["state"] = live_state
            if extra.get("pr_merged_at") and not pull.get("merged_at"):
                pull["merged_at"] = extra.get("pr_merged_at")
        pull["checks_state"] = extra.get("checks_state")
        pull["checks_counts"] = extra.get("checks_counts") or {
            bucket: 0 for bucket in CHECK_BUCKETS
        }
        pull["checks_truncated"] = bool(extra.get("checks_truncated"))
    return pulls


def enrichment_complete(pulls: list[dict]) -> bool:
    return all(pull.get("checks_counts") is not None for pull in pulls)


def parse_crew_marker(body: str) -> dict | None:
    match = CREW_CLAIM_MARKER_RE.search(body or "")
    if match is None:
        return None
    fields = dict(CREW_CLAIM_FIELD_RE.findall(match.group(1)))
    pull_number = fields.get("pr") or ""
    updated = fields.get("updated") or ""
    return {
        "crew_id": fields.get("id") or "",
        "phase": fields.get("phase") or "",
        "pr": int(pull_number) if pull_number.isdigit() else None,
        "updated": updated if CREW_CLAIM_ISO_Z_RE.match(updated) else None,
    }


def find_crew_claim(
    timeline_rows: list[dict],
    crew_id: str = "",
    *,
    marker_parser: Callable[[str], dict | None] = parse_crew_marker,
) -> list[dict]:
    claims: list[dict] = []
    for row in timeline_rows or []:
        if not isinstance(row, dict) or row.get("kind") != "comment":
            continue
        parsed = marker_parser(row.get("body") or "")
        if parsed is None:
            continue
        raw_id = row.get("id")
        claims.append(
            {
                "comment_id": (
                    raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None
                ),
                **parsed,
                "actor": row.get("actor"),
                "created_at": row.get("created_at"),
            }
        )
    if crew_id:
        claims = [claim for claim in claims if claim["crew_id"] == crew_id]
    claims.sort(key=lambda claim: (claim["comment_id"] is None, claim["comment_id"] or 0))
    return claims
