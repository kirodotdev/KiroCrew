"""Linear issue reads and search over Linear's GraphQL API.

Linear is a hosted service with no CLI, so the gateway calls ``api.linear.app``
itself with a personal API key the operator stores in Crew's vault as
``LINEAR_API_KEY``. The key is read from the vault on each request, sent only in
the ``Authorization`` header, and never logged or returned: every error this
module raises is its own fixed text, never Linear's.

Every query is a constant document and the request's values travel only as
GraphQL variables. A search's filter is ``{"and": [<team clause>, ...]}``, so no
structured filter or raw ``IssueFilter`` can widen it past the requested team,
and a row from any other team is dropped anyway.

Importing this module installs Linear into issue search and into the single-issue
read (``issue_search.install_provider``, ``cache.install_issue_reader``): the
search query types live in ``issue_search``, which is listed after ``cache``, so
neither of them can import this module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import string
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientError, ClientSession, ClientTimeout

from kiro_crew.config import loader as config_loader
from kiro_crew.dashboard.handlers import _shared
from kiro_crew.dashboard.source_providers import (
    LOGGER_NAME,
    cache,
    issue_search,
    links,
    projection,
    sanitize,
)
from kiro_crew.dashboard.source_providers.contract import SourceProviderError, SourceRef
from kiro_crew.dashboard.source_providers.issue_search import IssueQuery, IssueQueryError
from kiro_crew.secrets import SecretVault

logger = logging.getLogger(LOGGER_NAME)

PROVIDER = "linear"
#: The vault name of the personal API key. Vault-only: it is never read from
#: ``.env``, so it never reaches the process environment a child could inherit.
API_KEY_NAME = "LINEAR_API_KEY"
_API_URL = "https://api.linear.app/graphql"
_FETCH_TIMEOUT = 15  # seconds per HTTP call
_MAX_COMMENTS = 50
_MAX_LABELS = 50
# Per row of a search page; Linear bills a query by the objects it can return.
_SEARCH_MAX_LABELS = 20
# Linear's largest page; ``fetch_search`` walks past earlier pages with it.
_PAGE_MAX = 250
# The paging window, like GitHub search's: past it a search answers no rows.
_SEARCH_MAX_RESULTS = 1000
_MAX_CURSOR_CHARS = 1024
# Workflow state types that mean the issue is done.
_CLOSED_STATE_TYPES = ("completed", "canceled")
_MAX_PERSON_CHARS = 255
_RAW_MAX_DEPTH = 8
_RAW_MAX_KEY_CHARS = 64
_RAW_KEY_CHARS = frozenset(string.ascii_letters)

_SETUP_MESSAGE = (
    "Add a Linear API key to read Linear issues: create a personal API key in "
    f"Linear (Settings > Security & access) and store it in Settings > Secrets as {API_KEY_NAME}."
)
_AUTH_MESSAGE = (
    f"Linear rejected the stored API key. Replace {API_KEY_NAME} in Settings > Secrets "
    "with a current personal API key."
)
_FORBIDDEN_MESSAGE = "The stored Linear API key is not allowed to read this."
_RATE_LIMIT_MESSAGE = "Linear's rate limit was reached. Try again in a minute."

_ISSUE_FIELDS = """
    number title description priority priorityLabel createdAt updatedAt
    state { name type }
    team { key }
"""

_ISSUE_QUERY = (
    """
query KiroCrewLinearIssue($id: String!, $labels: Int!, $comments: Int!) {
  issue(id: $id) {
    url completedAt canceledAt"""
    + _ISSUE_FIELDS
    + """
    creator { name displayName }
    assignee { name displayName }
    labels(first: $labels) { nodes { name color description } }
    comments(first: $comments) { nodes { id body createdAt url user { name displayName } } }
  }
}
"""
)

_ORGANIZATION_QUERY = "query KiroCrewLinearOrganization { organization { urlKey } }"

_SEARCH_QUERY = (
    """
query KiroCrewLinearIssues(
  $filter: IssueFilter!, $sort: [IssueSortInput!], $first: Int!, $after: String, $labels: Int!
) {
  organization { urlKey }
  issues(filter: $filter, sort: $sort, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {"""
    + _ISSUE_FIELDS
    + """
      creator { name displayName avatarUrl }
      assignee { name displayName avatarUrl }
      labels(first: $labels) { nodes { name color description } }
    }
  }
}
"""
)

# Reads only the page boundaries, to reach a later page's cursor cheaply.
_PAGE_QUERY = """
query KiroCrewLinearIssuePage(
  $filter: IssueFilter!, $sort: [IssueSortInput!], $first: Int!, $after: String
) {
  issues(filter: $filter, sort: $sort, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
  }
}
"""


# ── Credential and transport ────────────────────────────────────────────────


def _vault_secret(name: str) -> str:
    """Return the vault secret *name*, or ``""`` when absent or unreadable."""
    try:
        secret = SecretVault(config_loader.config_dir()).get(name)
    except Exception:
        logger.debug("Linear API key unavailable", exc_info=True)
        return ""
    return secret.reveal() if secret is not None else ""


async def _api_key() -> str:
    """The stored API key. BLOCKING vault read, so it runs in a worker thread."""
    key = (await asyncio.to_thread(_vault_secret, API_KEY_NAME)).strip()
    if not key:
        raise SourceProviderError(_SETUP_MESSAGE)
    # A header value holds no whitespace or control character; refuse the key
    # instead of letting the HTTP client raise with it in hand.
    if not key.isascii() or not key.isprintable() or " " in key:
        raise SourceProviderError(f"The stored {API_KEY_NAME} is not a Linear API key.")
    return key


async def _post(body: bytes, key: str) -> tuple[int, bytes]:
    """POST one GraphQL request; ``(status, body)`` with the body capped."""
    headers = {
        "Authorization": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    timeout = ClientTimeout(total=_FETCH_TIMEOUT)
    async with ClientSession(timeout=timeout) as session:
        async with session.post(
            _API_URL, data=body, headers=headers, allow_redirects=False
        ) as resp:
            return resp.status, await _shared.read_capped_response(
                resp, sanitize._MAX_PAYLOAD_BYTES
            )


def _error_codes(errors: Any) -> set[str]:
    return {
        str(projection._as_dict(error.get("extensions")).get("code") or "").upper()
        for error in projection._as_list(errors)
    }


async def _graphql(document: str, variables: dict[str, Any], *, refused: str) -> dict[str, Any]:
    """Run *document* with *variables* and return its ``data``.

    *refused* is the message for a request Linear answered with errors other than
    auth or rate limit (an unknown issue, a filter it does not accept). Linear's
    own error text is never shown: it can repeat the submitted values.
    """
    key = await _api_key()
    body = json.dumps({"query": document, "variables": variables}).encode()
    try:
        status, raw = await _post(body, key)
    except (ClientError, asyncio.TimeoutError) as exc:
        raise SourceProviderError(f"Could not reach Linear: {type(exc).__name__}") from None
    if len(raw) > sanitize._MAX_PAYLOAD_BYTES:
        raise SourceProviderError("Linear's answer exceeds the size limit.")
    if status == 401:
        raise SourceProviderError(_AUTH_MESSAGE)
    if status == 403:
        raise SourceProviderError(_FORBIDDEN_MESSAGE)
    if status == 429:
        raise SourceProviderError(_RATE_LIMIT_MESSAGE)
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        raise SourceProviderError(
            f"Linear returned HTTP {status} with no readable answer."
        ) from None
    if not isinstance(payload, dict):
        raise SourceProviderError("Linear returned an invalid answer.")
    if payload.get("errors"):
        codes = _error_codes(payload["errors"])
        if "AUTHENTICATION_ERROR" in codes:
            raise SourceProviderError(_AUTH_MESSAGE)
        if "RATELIMITED" in codes:
            raise SourceProviderError(_RATE_LIMIT_MESSAGE)
        if "FORBIDDEN" in codes:
            raise SourceProviderError(_FORBIDDEN_MESSAGE)
        raise SourceProviderError(refused)
    data = payload.get("data")
    if status != 200 or not isinstance(data, dict):
        raise SourceProviderError(f"Linear returned HTTP {status} for this request.")
    return data


def _workspace(data: dict[str, Any]) -> str:
    """The organization's url key from *data*, checked before it builds a URL."""
    url_key = str(projection._as_dict(data.get("organization")).get("urlKey") or "").lower()
    if not links.is_linear_workspace(url_key):
        raise SourceProviderError("Linear returned an invalid workspace.")
    return url_key


def _person_name(value: Any) -> str:
    person = projection._as_dict(value)
    return str(person.get("displayName") or person.get("name") or "")


def _closed(state: dict[str, Any]) -> bool:
    return str(state.get("type") or "") in _CLOSED_STATE_TYPES


def _priority(row: dict[str, Any]) -> str | None:
    """The priority's label, or None for Linear's "No priority" (0)."""
    value = row.get("priority")
    label = str(row.get("priorityLabel") or "")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return label or None


# ── One issue ───────────────────────────────────────────────────────────────


async def issue_url_for_key(key: Any) -> str:
    """The canonical URL of issue *key* (``ENG-123``) in the API key's workspace."""
    parsed = links.linear_issue_key(key.strip()) if isinstance(key, str) else None
    if parsed is None:
        raise ValueError("key must be a Linear issue key like ENG-123.")
    data = await _graphql(
        _ORGANIZATION_QUERY, {}, refused="Linear did not report the API key's workspace."
    )
    return links.linear_issue_url(_workspace(data), *parsed)


def _same_workspace(issue_url: Any, workspace: str) -> bool:
    """Whether Linear's own URL for the issue is in *workspace*.

    An API key reads one workspace, and an issue key is unique only inside one,
    so a link into another workspace would otherwise show this workspace's issue
    that happens to share the key.
    """
    if not isinstance(issue_url, str):
        return False
    parsed = urlparse(issue_url)
    segments = parsed.path.split("/")
    return (
        parsed.hostname == links.LINEAR_HOST
        and len(segments) > 1
        and segments[1].lower() == workspace
    )


async def _fetch_linear_issue(ref: SourceRef) -> dict[str, Any]:
    """Read one issue in the single-issue payload shape. Unredacted: ``cache`` redacts."""
    key = f"{ref.repo}-{ref.number}"
    data = await _graphql(
        _ISSUE_QUERY,
        {"id": key, "labels": _MAX_LABELS, "comments": _MAX_COMMENTS + 1},
        refused=f"Linear issue {key} was not found, or the stored API key cannot read it.",
    )
    issue = data.get("issue")
    if not isinstance(issue, dict):
        raise SourceProviderError(f"Linear issue {key} was not found.")
    if not _same_workspace(issue.get("url"), ref.owner):
        raise SourceProviderError(
            f"Linear issue {key} is not in the {ref.owner} workspace the stored API key reads."
        )
    state = projection._as_dict(issue.get("state"))
    closed = _closed(state)
    canceled = str(state.get("type") or "") == "canceled"
    labels = projection._issue_labels(projection._as_dict(issue.get("labels")).get("nodes"))
    priority = _priority(issue)
    if priority:
        # The issue payload has no priority field, so it rides as a label, as Jira's does.
        labels.insert(0, {"name": f"Priority: {priority}", "color": "", "description": ""})
    rows = projection._as_list(projection._as_dict(issue.get("comments")).get("nodes"))
    partial: list[str] = []
    if len(rows) > _MAX_COMMENTS:
        projection._mark_partial(partial, "comments")
    comments = sorted(
        (
            {
                "id": str(row.get("id") or ""),
                "author": _person_name(row.get("user")),
                "body": str(row.get("body") or ""),
                "createdAt": str(row.get("createdAt") or ""),
                "url": projection._safe_https_url(row.get("url")),
            }
            for row in rows[:_MAX_COMMENTS]
        ),
        key=lambda comment: comment["createdAt"],
    )
    assignee = _person_name(issue.get("assignee"))
    return {
        "provider": PROVIDER,
        "url": ref.url,
        "number": ref.number,
        "title": str(issue.get("title") or ""),
        "description": str(issue.get("description") or ""),
        "state": "closed" if closed else "open",
        "stateReason": ("not_planned" if canceled else "completed") if closed else "",
        "author": _person_name(issue.get("creator")),
        "createdAt": str(issue.get("createdAt") or ""),
        "updatedAt": str(issue.get("updatedAt") or ""),
        "closedAt": str(issue.get("completedAt") or issue.get("canceledAt") or ""),
        "closedBy": "",
        "labels": labels,
        "assignees": [assignee] if assignee else [],
        "milestone": None,
        "commentCount": len(comments),
        "locked": False,
        "reactions": None,
        "comments": comments,
        "linkedChanges": [],
        "partialSections": partial,
        "failedSections": [],
    }


# ── Search ──────────────────────────────────────────────────────────────────


def _is_person_filter(value: str) -> bool:
    """``me``, an email address or a display name, which may hold spaces."""
    return len(value) <= _MAX_PERSON_CHARS and value.isprintable()


def _person_filter(value: str) -> dict[str, Any]:
    if value == "me":
        return {"isMe": {"eq": True}}
    field = "email" if "@" in value else "displayName"
    return {field: {"eqIgnoreCase": value}}


def _refuse_constant(name: str) -> Any:
    raise ValueError(name)


def raw_filter(raw: str) -> dict[str, Any]:
    """Parse a raw ``IssueFilter``: a JSON object of field names and comparators.

    Linear checks the fields against its schema. Here the shape is bounded: keys
    are ASCII letters, values are JSON scalars, lists and objects nested at most
    ``_RAW_MAX_DEPTH`` deep, and NaN or Infinity (which are not JSON) are refused.
    """
    message = "raw must be a Linear IssueFilter JSON object."
    try:
        value = json.loads(raw, parse_constant=_refuse_constant)
    except (ValueError, RecursionError):
        raise IssueQueryError(message) from None
    if not isinstance(value, dict) or not value:
        raise IssueQueryError(message)
    pending: list[tuple[Any, int]] = [(value, 1)]
    while pending:
        node, depth = pending.pop()
        if isinstance(node, (dict, list)) and depth > _RAW_MAX_DEPTH:
            raise IssueQueryError(f"raw nests deeper than {_RAW_MAX_DEPTH} levels.")
        if isinstance(node, dict):
            for key, child in node.items():
                if not 0 < len(key) <= _RAW_MAX_KEY_CHARS or not set(key) <= _RAW_KEY_CHARS:
                    raise IssueQueryError("raw field names are letters only.")
                pending.append((child, depth + 1))
        elif isinstance(node, list):
            pending.extend((child, depth + 1) for child in node)
        elif isinstance(node, float) and not math.isfinite(node):
            raise IssueQueryError(message)
        elif isinstance(node, str) and not node.isprintable():
            raise IssueQueryError("raw contains control characters.")
    return value


def validate_search(query: IssueQuery) -> IssueQuery:
    scope = query.scope.upper()
    if not links.is_linear_team_key(scope):
        raise IssueQueryError("scope must be a Linear team key like ENG.")
    if query.host not in ("", links.LINEAR_HOST):
        raise IssueQueryError("host must be omitted for Linear.")
    for field, value in (("assignee", query.assignee), ("author", query.author)):
        if value and not _is_person_filter(value):
            raise IssueQueryError(f"{field} must be me, an email address or a Linear display name.")
    if query.raw:
        raw_filter(query.raw)
    return replace(query, scope=scope, host=links.LINEAR_HOST)


def search_filter(query: IssueQuery) -> dict[str, Any]:
    """The ``IssueFilter`` variable: the team clause ANDed with every other one."""
    clauses: list[dict[str, Any]] = [{"team": {"key": {"eq": query.scope}}}]
    if query.raw:
        clauses.append(raw_filter(query.raw))
        return {"and": clauses}
    if query.state != "all":
        operator = "in" if query.state == "closed" else "nin"
        clauses.append({"state": {"type": {operator: list(_CLOSED_STATE_TYPES)}}})
    if query.assignee:
        clauses.append({"assignee": _person_filter(query.assignee)})
    if query.author:
        clauses.append({"creator": _person_filter(query.author)})
    clauses.extend(
        {"labels": {"some": {"name": {"eqIgnoreCase": label}}}} for label in query.labels
    )
    if query.text:
        clauses.append({"searchableContent": {"contains": query.text}})
    return {"and": clauses}


def _search_sort(query: IssueQuery) -> list[dict[str, Any]]:
    field = "createdAt" if query.sort == "created" else "updatedAt"
    order = "Ascending" if query.order == "asc" else "Descending"
    return [{field: {"order": order}}]


def _page_info(data: dict[str, Any]) -> tuple[bool, str | None]:
    """``(hasNextPage, endCursor)`` of the issues connection in *data*."""
    info = projection._as_dict(projection._as_dict(data.get("issues")).get("pageInfo"))
    cursor = info.get("endCursor")
    if not isinstance(cursor, str) or not 0 < len(cursor) <= _MAX_CURSOR_CHARS:
        cursor = None
    return info.get("hasNextPage") is True and cursor is not None, cursor


async def fetch_search(query: IssueQuery) -> dict[str, Any]:
    """Read one page of the team's issues, unredacted (``cache`` redacts).

    The endpoint's cursor is a page number while Linear pages by opaque cursor,
    so a later page first walks the earlier ones reading only their boundaries,
    ``_PAGE_MAX`` issues per call. The window keeps that to at most four calls.
    """
    offset = (query.page - 1) * query.limit
    if offset >= _SEARCH_MAX_RESULTS:
        return {"urlKey": "", "nodes": [], "hasNextPage": False}
    refused = "Linear refused this search. Check the raw IssueFilter field names."
    variables: dict[str, Any] = {"filter": search_filter(query), "sort": _search_sort(query)}
    after: str | None = None
    skip = offset
    while skip > 0:
        step = min(skip, _PAGE_MAX)
        data = await _graphql(
            _PAGE_QUERY, {**variables, "first": step, "after": after}, refused=refused
        )
        more, after = _page_info(data)
        if not more:
            return {"urlKey": "", "nodes": [], "hasNextPage": False}
        skip -= step
    # The last page inside the window reads only the rows the window still holds.
    first = min(query.limit, _SEARCH_MAX_RESULTS - offset)
    data = await _graphql(
        _SEARCH_QUERY,
        {**variables, "first": first, "after": after, "labels": _SEARCH_MAX_LABELS},
        refused=refused,
    )
    nodes = projection._as_dict(data.get("issues")).get("nodes")
    if not isinstance(nodes, list):
        raise SourceProviderError("Linear returned an invalid issue list")
    more, _cursor = _page_info(data)
    more = more and offset + query.limit < _SEARCH_MAX_RESULTS
    return {"urlKey": _workspace(data), "nodes": nodes, "hasNextPage": more}


def _person(value: Any) -> dict[str, str] | None:
    person = projection._as_dict(value)
    login = str(person.get("displayName") or person.get("name") or "")
    if not login:
        return None
    return {
        "login": login,
        "name": str(person.get("name") or login),
        "avatarUrl": projection._safe_https_url(person.get("avatarUrl")),
    }


def _issue_number(value: Any) -> int | None:
    """Linear's ``number`` is a GraphQL Float; keep a positive whole one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    number = int(value)
    return number if 0 < number < 10**9 else None


def _summary(query: IssueQuery, workspace: str, row: dict[str, Any]) -> dict[str, Any] | None:
    # Only the requested team's issues, whatever the filter matched.
    if projection._as_dict(row.get("team")).get("key") != query.scope:
        return None
    number = _issue_number(row.get("number"))
    if number is None:
        return None
    state = projection._as_dict(row.get("state"))
    closed = _closed(state)
    assignee = _person(row.get("assignee"))
    return {
        "provider": PROVIDER,
        # Built from the validated team and workspace, never from Linear's url.
        "url": links.linear_issue_url(workspace, query.scope, number),
        "key": f"{query.scope}-{number}",
        "number": number,
        "title": str(row.get("title") or ""),
        "state": "closed" if closed else "open",
        "statusLabel": str(state.get("name") or ("Closed" if closed else "Open")),
        "labels": projection._issue_labels(projection._as_dict(row.get("labels")).get("nodes")),
        "assignees": [assignee] if assignee else [],
        "author": _person(row.get("creator")),
        "priority": _priority(row),
        # shortcut: Linear's list API has no comment count, so this is 0; read the
        # comments per issue if a list needs the count.
        "commentCount": 0,
        "createdAt": str(row.get("createdAt") or ""),
        "updatedAt": str(row.get("updatedAt") or ""),
        # Stripped only; finish_issue_list cuts it after cache.search_issues redacts.
        "excerpt": issue_search.strip_markdown(row.get("description")),
        "project": query.scope,
        "linkedChanges": [],
    }


def project_search(query: IssueQuery, data: dict[str, Any]) -> dict[str, Any]:
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        raise SourceProviderError("Linear returned an invalid issue list")
    if not nodes:
        return issue_search._result(query, [], None, None)
    workspace = str(data.get("urlKey") or "")
    if not links.is_linear_workspace(workspace):
        raise SourceProviderError("Linear returned an invalid workspace.")
    issues = [
        summary
        for summary in (_summary(query, workspace, row) for row in projection._as_list(nodes))
        if summary
    ]
    next_page = query.page + 1 if data.get("hasNextPage") is True else None
    # Linear reports no total.
    return issue_search._result(query, issues, next_page, None)


issue_search.install_provider(
    PROVIDER,
    issue_search.IssueProvider(
        validate=validate_search, fetch=fetch_search, project=project_search
    ),
)
cache.install_issue_reader(PROVIDER, _fetch_linear_issue)
