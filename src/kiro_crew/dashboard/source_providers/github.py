"""GitHub reads: pull requests, their checks and review threads, and issues.

Each read goes through the shared provider runner and projects GitHub's shapes
into the payload contract. Identity always comes from the validated ref, never
from a provider echo.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kiro_crew.dashboard.source_providers import projection, runner
from kiro_crew.dashboard.source_providers.contract import SourceProviderError, SourceRef

# The supersession rule and the row flattening are the structured monitor's own,
# imported rather than copied: the dashboard glyph and the monitor read the same
# rollup, so a second implementation would be a second place to be wrong.
from kiro_crew.monitoring.github_pull_request import _flat_check_row, _mark_superseded_rows


def _github_check(item: dict[str, Any]) -> dict[str, Any]:
    conclusion = str(item.get("conclusion") or item.get("state") or "").upper()
    status = str(item.get("status") or "").upper()
    if status and status != "COMPLETED":
        bucket = "pending"
    elif conclusion in {"SUCCESS", "NEUTRAL"}:
        bucket = "passed"
    elif conclusion in {"SKIPPED", "STALE"}:
        bucket = "skipped"
    elif conclusion in {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "ERROR"}:
        bucket = "failed"
    else:
        bucket = "pending"
    return {
        "name": item.get("name") or item.get("context") or "Check",
        "workflow": item.get("workflowName") or "",
        "status": status,
        "conclusion": conclusion,
        "bucket": bucket,
        "url": item.get("detailsUrl") or item.get("targetUrl") or "",
        "startedAt": item.get("startedAt") or item.get("createdAt") or "",
        "completedAt": item.get("completedAt") or "",
    }


def _github_checks(rollup: list[Any]) -> list[dict[str, Any]]:
    """Project GitHub's status-check rollup, dropping the rows a newer run displaced.

    ``statusCheckRollup`` returns EVERY check-run recorded against the head sha,
    not one per check. A concurrency group that cancels a run in favour of a
    newer dispatch of the same workflow leaves the loser's rows in the rollup as
    ``CANCELLED``, so rendering the raw rollup inflated the panel's totals and
    let a superseded cancellation paint the CI glyph red on a pull request whose
    replacement run passed.

    The rows arrive flat from ``_github_rollup_read`` (each check run's
    workflow-run fields lifted beside its own), and the collapse is the rule the
    structured monitor's GitHub provider enforces, called rather than copied so
    the glyph and the monitor cannot disagree: identity is the workflow
    DEFINITION id, the run's triggering event and the check name; newest is the
    greatest run id; and a row is dropped only when its older run concluded
    ``CANCELLED`` and the row itself is ``COMPLETED``+``CANCELLED``. Two rows of
    one run, a row without a run identity, and every status context are kept.
    Anything the rollup cannot prove was displaced stays, because over-counting
    is cosmetic while dropping a live failure turns the glyph green over red.

    Input order is preserved.
    """
    rows: list[object] = [item for item in rollup if isinstance(item, dict)]
    return [
        _github_check(row)
        for row, superseded in _mark_superseded_rows(rows)
        if not superseded and isinstance(row, dict)
    ]


def _github_comment(item: dict[str, Any], kind: str) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or item.get("databaseId") or ""),
        "kind": kind,
        "author": projection._author(item.get("author") or item.get("user")),
        "body": item.get("body") or "",
        "state": item.get("state") or "",
        "createdAt": item.get("createdAt")
        or item.get("submittedAt")
        or item.get("created_at")
        or "",
        "url": item.get("url") or item.get("html_url") or "",
        "path": item.get("path") or "",
        "line": item.get("line") or item.get("original_line"),
        "threadId": "",
        "resolvable": False,
        "resolved": False,
    }


_GITHUB_REVIEW_THREADS_QUERY = (
    "query($owner:String!,$repo:String!,$number:Int!)"
    "{repository(owner:$owner,name:$repo)"
    "{pullRequest(number:$number)"
    "{reviewThreads(first:100){nodes{id isResolved "
    "comments(first:10){nodes{databaseId}}}}}}}"
)

# The head commit's rollup, with each check run's workflow-run identity. Owner and
# repository travel through gh's raw-string `-f` (the typed `-F` would turn an
# all-digit name into an integer the `String!` variable rejects); only the number
# is `-F`. The two ids are nullable on the wire (an app-created check run has a
# check suite but no workflow run), and the suite's conclusion is the run's own.
_GITHUB_ROLLUP_QUERY = (
    "query($o:String!,$r:String!,$n:Int!,$c:String){repository(owner:$o,name:$r)"
    "{pullRequest(number:$n){headRefOid commits(last:1){nodes{commit{oid "
    "statusCheckRollup{contexts(first:100,after:$c){pageInfo{hasNextPage endCursor} "
    "nodes{__typename ... on CheckRun{name status conclusion startedAt completedAt "
    "detailsUrl checkSuite{conclusion workflowRun{databaseId event "
    "workflow{databaseId name}}}} "
    "... on StatusContext{context state targetUrl createdAt}}}}}}}}}}"
)
_GITHUB_ROLLUP_MAX_PAGES = 10


def _github_thread_ids(payload: Any) -> set[str]:
    """Return review-thread IDs scoped to the queried pull request."""
    if not isinstance(payload, dict):
        return set()
    try:
        nodes = payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (KeyError, TypeError):
        return set()
    return {str(node["id"]) for node in projection._as_list(nodes) if node.get("id")}


def _github_thread_map(payload: Any) -> dict[str, dict[str, Any]]:
    """Map an inline comment databaseId to its review thread id and state."""
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(payload, dict):
        return result
    try:
        nodes = payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except (KeyError, TypeError):
        return result
    for node in projection._as_list(nodes):
        thread_id = node.get("id")
        if not thread_id:
            continue
        is_resolved = bool(node.get("isResolved"))
        comments = node.get("comments")
        comment_nodes = comments.get("nodes") if isinstance(comments, dict) else []
        for comment in projection._as_list(comment_nodes):
            database_id = comment.get("databaseId")
            if database_id is None:
                continue
            result[str(database_id)] = {
                "threadId": str(thread_id),
                "resolved": is_resolved,
            }
    return result


def _github_merge_state(details: dict[str, Any]) -> tuple[str, str]:
    """Normalize GitHub merge fields to (mergeable, mergeStateStatus).

    ``mergeable`` is one of ``mergeable`` / ``conflicting`` / ``unknown`` and
    ``mergeStateStatus`` is GitHub's merge-state vocabulary lowercased
    (``clean``, ``dirty``, ``behind``, ``blocked``, ``unstable``, ...).
    """
    raw_mergeable = str(details.get("mergeable") or "").upper()
    if raw_mergeable == "MERGEABLE":
        mergeable = "mergeable"
    elif raw_mergeable == "CONFLICTING":
        mergeable = "conflicting"
    else:
        mergeable = "unknown" if raw_mergeable else ""
    return mergeable, str(details.get("mergeStateStatus") or "").lower()


async def _github_settled_merge_state(ref: SourceRef, details: dict[str, Any]) -> tuple[str, str]:
    """Merge state for a GitHub PR, re-reading while it is still being computed.

    Re-reads only ``mergeable``/``mergeStateStatus``, at most
    ``_MERGE_STATE_REREADS`` times. A failed or still-unsettled re-read keeps the
    original value rather than raising: an unknown merge state degrades one
    banner, and must never fail the whole panel.
    """
    mergeable, merge_state = _github_merge_state(details)
    if projection._merge_state_settled(mergeable, merge_state):
        return mergeable, merge_state
    for _ in range(projection._MERGE_STATE_REREADS):
        await asyncio.sleep(projection._MERGE_STATE_REREAD_DELAY_SECS)
        try:
            data = await runner._run_json(
                "gh", "pr", "view", ref.url, "--json", "mergeable,mergeStateStatus"
            )
        except SourceProviderError:
            break
        if not isinstance(data, dict):
            break
        reread, reread_state = _github_merge_state(data)
        if projection._merge_state_settled(reread, reread_state):
            return reread, reread_state
    return mergeable, merge_state


async def _fetch_github(ref: SourceRef) -> dict[str, Any]:
    # `statusCheckRollup` is deliberately ABSENT from this field set: `gh`
    # resolves a `--json` field set atomically, so bundling the rollup (which
    # needs Checks read access that fine-grained tokens commonly lack) would
    # fail the whole panel read over the one section the token cannot see. The
    # rollup rides a separate degradable read below.
    fields = ",".join(
        [
            "additions",
            "author",
            "autoMergeRequest",
            "baseRefName",
            "body",
            "changedFiles",
            "comments",
            "commits",
            "deletions",
            "headRefName",
            "headRefOid",
            "isDraft",
            "mergeStateStatus",
            "mergeable",
            "mergedAt",
            "number",
            "reviews",
            "state",
            "title",
            "updatedAt",
            "url",
        ]
    )
    repo_api = f"repos/{ref.owner}/{ref.repo}/pulls/{ref.number}"
    details = await runner._run_json("gh", "pr", "view", ref.url, "--json", fields)
    if not isinstance(details, dict):
        raise SourceProviderError("GitHub returned an invalid pull-request payload")

    # Secondary endpoints degrade to empty sections instead of failing the
    # whole panel: the primary payload above already carries the core data.
    files_raw: Any
    review_comments_raw: Any
    review_threads_raw: Any
    merge_state_raw: Any
    rollup_raw: Any
    (
        files_raw,
        review_comments_raw,
        review_threads_raw,
        merge_state_raw,
        rollup_raw,
    ) = await asyncio.gather(
        runner._run_json(
            "gh",
            "api",
            f"{repo_api}/files?per_page={projection._SECONDARY_PAGE_SIZE}",
            max_output_bytes=runner._DIFF_OUTPUT_BYTES,
        ),
        runner._run_json(
            "gh",
            "api",
            f"{repo_api}/comments?per_page={projection._SECONDARY_PAGE_SIZE}",
            max_output_bytes=runner._DISCUSSION_OUTPUT_BYTES,
        ),
        runner._run_json(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={_GITHUB_REVIEW_THREADS_QUERY}",
            "-f",
            f"owner={ref.owner}",
            "-f",
            f"repo={ref.repo}",
            "-F",
            f"number={ref.number}",
            max_output_bytes=runner._DISCUSSION_OUTPUT_BYTES,
        ),
        # Runs alongside the secondary calls so its re-read wait overlaps with
        # fetches this request was making anyway.
        _github_settled_merge_state(ref, details),
        _github_rollup_read(ref),
        return_exceptions=True,
    )
    partial_sections: list[str] = []
    if isinstance(files_raw, BaseException):
        projection._mark_partial(partial_sections, "files")
    if isinstance(review_comments_raw, BaseException) or isinstance(
        review_threads_raw, BaseException
    ):
        projection._mark_partial(partial_sections, "inline review comments")
    checks: list[dict[str, Any]] = []
    if isinstance(rollup_raw, BaseException):
        # The rollup is read separately from the core fields precisely so a
        # token without Checks read access (or a transient rollup failure)
        # costs the checks SECTION, never the panel. Name it in
        # `partialSections` so the empty list cannot read as "no checks": the
        # frontend banner surfaces the degraded section, and
        # `record_full_payload_status` keeps a known CI glyph alive while
        # `checks` is partial instead of erasing it.
        projection._mark_partial(partial_sections, "checks")
    else:
        rollup_checks, rollup_head = rollup_raw
        head_oid = str(details.get("headRefOid") or "")
        # A missing sha on either side DELIBERATELY fails open (accepts the
        # rollup): treating it as unverifiable would degrade every read where
        # the provider omits the field, which is worse than the narrow race
        # this guard exists for.
        if head_oid and rollup_head and rollup_head != head_oid:
            # The core read and the rollup read straddled a push: these checks
            # describe a different commit than the rest of the payload. Mark
            # the section unavailable rather than pin another head's CI to
            # this one; the next refresh re-pairs them.
            projection._mark_partial(partial_sections, "checks")
        else:
            checks = rollup_checks
    files = projection._or_empty(files_raw)
    review_comments = projection._or_empty(review_comments_raw)
    thread_map = _github_thread_map(projection._or_empty(review_threads_raw))
    file_rows = projection._as_list(files)
    review_comment_rows = projection._as_list(review_comments)
    changed_files = details.get("changedFiles")
    if (isinstance(changed_files, int) and changed_files > len(file_rows)) or (
        not isinstance(changed_files, int) and len(file_rows) >= projection._SECONDARY_PAGE_SIZE
    ):
        projection._mark_partial(partial_sections, "files")
    # GitHub's review-comment endpoint does not expose its total in the
    # primary payload. A full page means another page may exist.
    if len(review_comment_rows) >= projection._SECONDARY_PAGE_SIZE:
        projection._mark_partial(partial_sections, "inline review comments")

    inline_comments = [_github_comment(item, "inline") for item in review_comment_rows]
    for comment in inline_comments:
        info = thread_map.get(comment["id"])
        if info:
            comment["threadId"] = info["threadId"]
            comment["resolved"] = info["resolved"]
            comment["resolvable"] = True

    comments = [
        *(
            _github_comment(item, "comment")
            for item in projection._as_list(details.get("comments"))
        ),
        *(_github_comment(item, "review") for item in projection._as_list(details.get("reviews"))),
        *inline_comments,
    ]
    commits = []
    for item in projection._as_list(details.get("commits")):
        authors = projection._as_list(item.get("authors"))
        commits.append(
            {
                "sha": item.get("oid") or "",
                "title": item.get("messageHeadline") or "",
                "body": item.get("messageBody") or "",
                "author": projection._author(authors[0]) if authors else "",
                "date": item.get("committedDate") or item.get("authoredDate") or "",
                "url": (
                    f"https://github.com/{ref.owner}/{ref.repo}/commit/{item.get('oid')}"
                    if item.get("oid")
                    else ""
                ),
            }
        )

    normalized_files = []
    for item in projection._as_list(files):
        normalized_files.append(
            {
                "path": item.get("filename") or "",
                "status": item.get("status") or "modified",
                "additions": item.get("additions") or 0,
                "deletions": item.get("deletions") or 0,
                "patch": item.get("patch") or "",
            }
        )

    github_mergeable, github_merge_state = (
        merge_state_raw if isinstance(merge_state_raw, tuple) else _github_merge_state(details)
    )
    return {
        "provider": "github",
        # Identity comes from the VALIDATED ref, never the provider echo: the
        # browser submits this url back for refresh/resolve, so a compromised or
        # hostile instance echoing a different web_url could otherwise steer an
        # owner-authenticated mutation at an unrelated pull/merge request.
        "url": ref.url,
        "number": ref.number,
        "title": details.get("title") or "",
        "description": details.get("body") or "",
        "state": details.get("state") or "",
        "draft": bool(details.get("isDraft")),
        "mergedAt": details.get("mergedAt") or "",
        "mergeable": github_mergeable,
        "mergeStateStatus": github_merge_state,
        "autoMerge": bool(details.get("autoMergeRequest")),
        "updatedAt": details.get("updatedAt") or "",
        "headBranch": details.get("headRefName") or "",
        "baseBranch": details.get("baseRefName") or "",
        "headSha": details.get("headRefOid") or "",
        "author": projection._author(details.get("author")),
        "additions": details.get("additions") or 0,
        "deletions": details.get("deletions") or 0,
        "changedFiles": details.get("changedFiles") or len(normalized_files),
        "commits": commits,
        "checks": checks,
        "comments": comments,
        "files": normalized_files,
        "partialSections": partial_sections,
    }


async def _github_rollup_read(ref: SourceRef) -> tuple[list[dict[str, Any]], str]:
    """Read the check rollup ALONE, paired with the head sha it was read at.

    ``gh pr view`` resolves a ``--json`` field set atomically: one unreadable
    field fails the whole read. ``statusCheckRollup`` needs Checks read access
    that fine-grained tokens commonly lack, so it must never share a field set
    with data the token IS authorized for — every rollup consumer
    routes through this one isolated query instead of growing its own copy.
    ``headRefOid`` rides along (core pull-request data, readable whenever the
    PR itself is) so callers that pair this read with a separate core read can
    detect the two straddling a push and refuse to render another commit's
    checks.

    It is a GraphQL read rather than ``gh pr view --json statusCheckRollup``
    because the collapse needs each check run's workflow-run identity (definition
    id, event, run id, run conclusion), which the ``--json`` payload does not
    carry. Every page must name the same head, both on the pull request and on
    the commit the rollup hangs off; a disagreement, a malformed page, or a board
    past the page cap raises, so the caller degrades the checks section instead
    of rendering a partial or mixed board.
    """
    rows: list[dict[str, Any]] = []
    head = ""
    cursor = ""
    for _ in range(_GITHUB_ROLLUP_MAX_PAGES):
        argv = ["gh", "api", "graphql", "-f", f"query={_GITHUB_ROLLUP_QUERY}"]
        if cursor:
            argv += ["-f", f"c={cursor}"]
        argv += ["-f", f"o={ref.owner}", "-f", f"r={ref.repo}", "-F", f"n={ref.number}"]
        data = await runner._run_json(*argv, max_output_bytes=runner._CHECKS_OUTPUT_BYTES)
        try:
            if not isinstance(data, dict) or data.get("errors"):
                raise TypeError
            pull = data["data"]["repository"]["pullRequest"]
            page_head = str(pull.get("headRefOid") or "")
            commits = pull["commits"]["nodes"]
            commit = commits[0]["commit"] if commits else None
        except (KeyError, TypeError, IndexError, AttributeError):
            raise SourceProviderError("GitHub returned an invalid checks payload") from None
        if head and page_head != head:
            raise SourceProviderError("GitHub head moved while reading checks")
        head = page_head
        if commit is None:
            return [], head  # a pull request with no commit has no checks
        if not isinstance(commit, dict) or (head and commit.get("oid") != head):
            raise SourceProviderError("GitHub returned checks for a different commit")
        rollup = commit.get("statusCheckRollup")
        if rollup is None:
            return rows, head  # the host reports no checks for this head
        try:
            contexts = rollup["contexts"]
            nodes = contexts["nodes"]
            page = contexts["pageInfo"] or {}
            rows.extend(_flat_check_row(node) for node in nodes)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceProviderError("GitHub returned an invalid checks payload") from None
        if not page.get("hasNextPage"):
            # The panel polls the checks endpoint while checks are pending and
            # writes the result straight over the full payload's `checks`, so
            # every consumer MUST collapse identically — an uncollapsed reply
            # would re-inflate the counts and resurrect a superseded CANCELLED
            # failure on the first poll after the panel opens.
            return _github_checks(rows), head
        cursor = str(page.get("endCursor") or "")
        if not cursor:
            raise SourceProviderError("GitHub returned an invalid checks payload")
    # A board past the cap reads unavailable rather than in part: a partial read
    # could keep a displaced row whose successor sits on a page never fetched.
    raise SourceProviderError("GitHub check rollup exceeds the page limit")


async def _fetch_github_checks(ref: SourceRef) -> list[dict[str, Any]]:
    checks, _head = await _github_rollup_read(ref)
    return checks


def _github_issue_reactions(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    reactions = {"total": projection._int_or_zero(value.get("total_count"))}
    for contract_key, github_key in projection._GITHUB_REACTION_KEYS:
        reactions[contract_key] = projection._int_or_zero(value.get(github_key))
    return reactions


def _github_issue_comment(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or item.get("node_id") or ""),
        "author": projection._author(item.get("user") or item.get("author")),
        "body": str(item.get("body") or ""),
        "createdAt": str(item.get("created_at") or item.get("createdAt") or ""),
        "url": projection._safe_https_url(item.get("html_url") or item.get("url")),
    }


def _github_linked_changes(timeline: Any) -> list[dict[str, Any]]:
    """Cross-referenced PULL REQUESTS from an issue's timeline.

    GitHub records "this was mentioned from X" as a ``cross-referenced`` event
    whose ``source.issue`` is the mentioning item. Issues and pull requests are
    the same REST object type, distinguished only by the presence of a
    ``pull_request`` sub-object, so filtering on that key is what keeps a plain
    issue-to-issue mention out of the linked-changes list. Duplicates are folded
    because one pull request can cross-reference an issue repeatedly.
    """
    changes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in projection._as_list(timeline):
        if str(event.get("event") or "") != "cross-referenced":
            continue
        origin = event.get("source")
        item = origin.get("issue") if isinstance(origin, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get("pull_request"), dict):
            continue
        url = projection._safe_https_url(item.get("html_url"))
        if not url or url in seen:
            continue
        seen.add(url)
        changes.append(
            {
                "provider": "github",
                "url": url,
                "number": projection._int_or_zero(item.get("number")),
                "title": str(item.get("title") or ""),
                "state": str(item.get("state") or "").lower(),
            }
        )
    return changes


async def _fetch_github_issue(ref: SourceRef) -> dict[str, Any]:
    issue_api = f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}"
    details = await runner._run_json("gh", "api", issue_api)
    if not isinstance(details, dict):
        raise SourceProviderError("GitHub returned an invalid issue payload")

    # Secondary endpoints degrade to empty sections instead of failing the
    # whole panel: the primary payload above already carries the core data.
    comments_raw: Any
    timeline_raw: Any
    comments_raw, timeline_raw = await asyncio.gather(
        runner._run_json(
            "gh",
            "api",
            f"{issue_api}/comments?per_page={projection._SECONDARY_PAGE_SIZE}",
            max_output_bytes=runner._DISCUSSION_OUTPUT_BYTES,
        ),
        runner._run_json(
            "gh",
            "api",
            f"{issue_api}/timeline?per_page={projection._SECONDARY_PAGE_SIZE}",
            max_output_bytes=runner._DISCUSSION_OUTPUT_BYTES,
        ),
        return_exceptions=True,
    )
    partial_sections: list[str] = []
    if isinstance(comments_raw, BaseException):
        projection._mark_partial(partial_sections, "comments")
    if isinstance(timeline_raw, BaseException):
        projection._mark_partial(partial_sections, "linked changes")
    comment_rows = projection._as_list(projection._or_empty(comments_raw))
    comment_count = projection._int_or_zero(details.get("comments"))
    if len(comment_rows) >= projection._SECONDARY_PAGE_SIZE or comment_count > len(comment_rows):
        projection._mark_partial(partial_sections, "comments")
    timeline = projection._or_empty(timeline_raw)
    if len(projection._as_list(timeline)) >= projection._SECONDARY_PAGE_SIZE:
        # A full page may be truncated, so a cross-reference on a later page
        # would be missing from the linked-changes list.
        projection._mark_partial(partial_sections, "linked changes")

    return {
        "provider": "github",
        # Identity comes from the VALIDATED ref, never the provider echo: the
        # browser submits this url back for refresh, so a hostile or compromised
        # instance echoing a different html_url could otherwise steer a
        # credential-backed read at an unrelated repository or object.
        "url": ref.url,
        "number": ref.number,
        "title": str(details.get("title") or ""),
        "description": str(details.get("body") or ""),
        "state": str(details.get("state") or "").lower(),
        "stateReason": str(details.get("state_reason") or ""),
        "author": projection._author(details.get("user")),
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
        "commentCount": comment_count,
        "locked": bool(details.get("locked")),
        "reactions": _github_issue_reactions(details.get("reactions")),
        "comments": [_github_issue_comment(item) for item in comment_rows],
        "linkedChanges": _github_linked_changes(timeline),
        "partialSections": partial_sections,
    }
