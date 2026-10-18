"""The per-item Azure DevOps reads behind the detail pane.

One work item's detail and a reference summary, and the work-item and
pull-request timelines. Callers reach every name here through
:mod:`kiro_crew.apps.builtins.issue_radar.backend.azure_client`.
"""

from __future__ import annotations

from . import azure_normalization as _normalization
from .azure_client_cli import (
    _API_GIT,
    _API_WIT,
    _API_WIT_COMMENTS,
    _COMMENT_PAGE_SIZE,
    _MAX_PAGES,
    _SYNTHETIC_LABEL_COLOR,
    AZ_PAGINATE_TIMEOUT_SEC,
    AZ_TIMEOUT_SEC,
    AZURE_HOST,
    _az_invoke,
    _az_invoke_paged,
    _check_repo,
    _field,
    _identity_login,
    _norm_issue_detail,
    _obj,
    _split_owner,
    _tag_names,
    _values,
    _work_item_url,
)
from .azure_client_work_items import (
    _closed_state_names,
    _hydrate_work_items,
    _project_closed_states,
)
from .errors import ProviderCliError


def _work_item(org: str, project: str, number: int, *, host: str, timeout: float) -> dict:
    """One work item with the fields this module reads."""
    rows = _hydrate_work_items(org, project, [int(number)], host=host, timeout=timeout)
    if not rows:
        raise ProviderCliError(f"could not read {org}/{project}#{int(number)} on {AZURE_HOST}")
    return rows[0]


def get_issue_detail(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> dict:
    """Full detail for one work item.

    ``number`` is the work item id (collection-unique on Azure, unlike GitLab's
    project-scoped iid), coerced to ``int`` before it can reach an argv. ``repo``
    is ignored: a work item is addressed by project and id.
    """
    org, project = _split_owner(owner)
    del repo
    raw = _work_item(org, project, number, host=host, timeout=timeout)
    fields = _obj(raw.get("fields"))
    work_item_type = str(_field(fields, "System.WorkItemType", "") or "")
    closed_states = (
        _closed_state_names(org, project, work_item_type, host=host, timeout=timeout)
        if work_item_type
        else _project_closed_states(org, project, host=host, timeout=timeout)
    )
    return _norm_issue_detail(raw, org=org, project=project, closed_states=closed_states)


def get_ref_summary(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = AZ_TIMEOUT_SEC
) -> dict:
    """Compact summary of one referenced WORK ITEM (hover card, ``#123``).

    Deliberately work-item-only, and that is a provider difference rather than a
    gap -- the same one gitlab_client documents. GitHub shares ONE number sequence
    between issues and pull requests, so its ``/issues/{n}`` endpoint answers for
    both and the caller needs ``is_pr`` to learn which it got. Azure allocates work
    item ids and pull request ids from entirely different services, so ``#5`` and
    ``!5`` are unrelated items; falling back to the pull request endpoint when work
    item 5 is absent would describe a different item under the number the user
    asked about. A missing work item raises instead, and ``is_pr`` is always
    ``False``.
    """
    org, project = _split_owner(owner)
    del repo
    raw = _work_item(org, project, number, host=host, timeout=timeout)
    fields = _obj(raw.get("fields"))
    work_item_type = str(_field(fields, "System.WorkItemType", "") or "")
    closed_states = (
        _closed_state_names(org, project, work_item_type, host=host, timeout=timeout)
        if work_item_type
        else _project_closed_states(org, project, host=host, timeout=timeout)
    )
    state = str(_field(fields, "System.State", "") or "")
    is_closed = state in closed_states
    return {
        "number": raw.get("id"),
        "title": str(_field(fields, "System.Title", "") or ""),
        "state": "closed" if is_closed else "open",
        "state_reason": None,
        "url": _work_item_url(org, project, int(number)),
        "author": _identity_login(fields.get("System.CreatedBy")),
        "author_association": None,
        "created_at": _field(fields, "System.CreatedDate"),
        "updated_at": _field(fields, "System.ChangedDate"),
        "closed_at": _field(fields, "Microsoft.VSTS.Common.ClosedDate") if is_closed else None,
        "comments": _field(fields, "System.CommentCount", 0),
        "is_pr": False,
        "draft": False,
        "merged_at": None,
        "labels": [
            {"name": name, "color": _SYNTHETIC_LABEL_COLOR}
            for name in _tag_names(fields.get("System.Tags"))
        ],
    }


# ── timeline ────────────────────────────────────────────────────────────────
#
# GitHub serves one unified `issues/{n}/timeline`. Azure splits the same
# information across two endpoints, so the work item timeline is assembled here:
#
#   comments   -- human discussion (a preview-only resource)
#   updates    -- the revision log: every field change, with the actor
#
# An update carries oldValue/newValue per field, so the typed events GitHub emits
# (labeled, assigned, closed, renamed) are RECONSTRUCTED from field diffs rather
# than read as events. Anything whose field is not one of those is dropped rather
# than shown as a raw field name, matching the other clients' "drop the noise"
# behaviour.

# Field -> the event kind its change becomes. Only fields the triage pane renders
# are listed; everything else in an update is noise here.
_TRACKED_UPDATE_FIELDS = _normalization._TRACKED_UPDATE_FIELDS
_update_actor = _normalization._update_actor
_update_when = _normalization._update_when
_tag_events = _normalization._tag_events
_state_events = _normalization._state_events
_assignee_events = _normalization._assignee_events
_norm_update = _normalization._norm_update
_norm_work_item_comment = _normalization._norm_work_item_comment


def list_issue_timeline(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = AZ_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """Normalized, chronological timeline for one work item.

    A failure on the SECONDARY stream (the revision log) degrades to omitting
    those entries rather than failing the whole pane: the comments are the
    substance, and the updates endpoint can be far larger than the comment list on
    a long-lived work item.
    """
    org, project = _split_owner(owner)
    del repo
    work_item_id = int(number)
    # Azure pages work-item comments with a continuation token, and a timeline that
    # stops at the first page silently drops later discussion -- the omission looks
    # like a quiet conversation rather than a truncated read. Bounded by _MAX_PAGES
    # for the same reason every other paged read here is: a pathological item must
    # not hold the request open indefinitely.
    comments: list[dict] = []
    token = ""
    for _ in range(_MAX_PAGES):
        query: dict[str, object] = {"$top": _COMMENT_PAGE_SIZE, "order": "asc"}
        if token:
            query["continuationToken"] = token
        page = _obj(
            _az_invoke(
                org=org,
                area="wit",
                resource="comments",
                host=host,
                timeout=timeout,
                route={"project": project, "workItemId": work_item_id},
                query=query,
                api_version=_API_WIT_COMMENTS,
            )
        )
        comments.extend(_values(page.get("comments")))
        token = str(page.get("continuationToken") or "")
        if not token:
            break
    events: list[dict] = [_norm_work_item_comment(row) for row in comments]

    closed_states = _project_closed_states(org, project, host=host, timeout=timeout)
    try:
        updates = _az_invoke_paged(
            org=org,
            area="wit",
            resource="updates",
            host=host,
            timeout=timeout,
            route={"project": project, "id": work_item_id},
            api_version=_API_WIT,
        )
    except ProviderCliError:
        updates = []
    for update in updates:
        events.extend(_norm_update(update, closed_states))

    events.sort(key=lambda e: str(e.get("created_at") or ""))
    return events


# Azure records a reviewer's vote as a SYSTEM thread carrying this property, so a
# vote is recoverable even though system comments are otherwise dropped.
_VOTE_PROPERTY_KEYS = _normalization._VOTE_PROPERTY_KEYS
_VOTE_STATES = _normalization._VOTE_STATES
_thread_property = _normalization._thread_property
_norm_thread_comment = _normalization._norm_thread_comment


def list_pr_timeline(
    owner: str, repo: str, number: int, *, host: str = "", timeout: float = AZ_PAGINATE_TIMEOUT_SEC
) -> list[dict]:
    """Normalized timeline for one pull request, including inline comments.

    Azure keeps PR discussion in THREADS rather than in a flat comment list, and
    an inline (diff) comment is just a thread carrying a ``threadContext``, so both
    kinds come from one read. Reviewer votes are recovered from the system threads
    that record them, which is why those are inspected before being dropped.

    Azure's thread resolution has SEVEN states (active, fixed, wontFix, closed,
    byDesign, pending, unknown) against the app's two-state model, so mapping it
    would be lossy in a way that matters (``wontFix`` and ``fixed`` are opposite
    outcomes that both read as resolved). The timeline therefore reports the
    comments themselves and leaves resolution to the provider's own UI rather than
    flattening a verdict.
    """
    org, project = _split_owner(owner)
    name = _check_repo(repo)
    pr_id = int(number)
    threads = _values(
        _az_invoke(
            org=org,
            area="git",
            resource="pullRequestThreads",
            host=host,
            timeout=timeout,
            route={"project": project, "repositoryId": name, "pullRequestId": pr_id},
            api_version=_API_GIT,
        )
    )
    events: list[dict] = []
    for thread in threads:
        vote = _thread_property(thread, _VOTE_PROPERTY_KEYS)
        if vote is not None:
            try:
                # Via str because a property value arrives untyped: Azure has
                # written this as both a number and a numeric string.
                value = int(str(vote).strip())
            except (TypeError, ValueError):
                value = 0
            author = None
            comments = _values(thread.get("comments"))
            if comments:
                author = _identity_login(comments[0].get("author"))
            events.append(
                {
                    "kind": "reviewed",
                    "actor": author,
                    "created_at": thread.get("publishedDate"),
                    "review_state": _VOTE_STATES.get(value, "COMMENTED"),
                    "body": "",
                }
            )
            continue
        for comment in _values(thread.get("comments")):
            entry = _norm_thread_comment(thread, comment)
            if entry is not None:
                events.append(entry)
    events.sort(key=lambda e: str(e.get("created_at") or ""))
    return events
