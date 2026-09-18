"""GitHub response decoder: a GitHub 2xx payload -> the shared
:class:`~kiro_crew.connections.control_plane.result.OperationResult`, carrying
the fetched records in the neutral ``payload`` channel.

WHAT THIS OWNS
==============
W01's production transport (``control_plane/production.py``) injects the
2xx -> ``OperationResult`` mapping as a
:data:`~kiro_crew.connections.control_plane.production.ResultDecode`, because
turning a vendor's body into the neutral shapes is the vendor's job. This module
is GitHub's decode. It does two things, both vendor-neutral once produced:

* it puts the fetched records into the ONE neutral data channel --
  a :class:`~kiro_crew.connections.control_plane.result.CollectionPayload` for a
  list reply (a JSON array, or a wrapped collection like ``{"check_runs": [...]}``),
  an :class:`~kiro_crew.connections.control_plane.result.ObjectPayload` for a
  single-object reply -- via :func:`result_with_payload`, so a consumer reads the
  rows off ``ExecutionOutcome.payload`` and NOWHERE else;
* it sets the SINGLE authoritative ``next_cursor`` on the envelope so
  :class:`~kiro_crew.connections.control_plane.executor.PageWalk` advances on it
  alone.

THE CURSOR IS SINGLE, AND LIVES ONLY ON THE ENVELOPE
====================================================
``CollectionPayload`` deliberately has no cursor of its own (W01 removed it at
RESULT_SCHEMA_VERSION 3): a second copy is a second thing that can disagree. The
continuation goes on ``OperationResult.next_cursor`` and the payload carries only
the rows. :func:`result_with_payload` enforces that a cursor is passed ONLY with
a collection.

WHERE GITHUB'S REST CURSOR COMES FROM (the established interface)
================================================================
GitHub REST list endpoints carry their next-page position ONLY in the ``Link``
response header (``rel="next"``); there is no in-body cursor. So this decoder
reads ``Link`` -- out of the very ``HttpReply.headers`` it is already decoding --
to POPULATE the single ``next_cursor``, emitted via
:func:`result_with_payload(..., next_cursor=...)`. The cursor is then single and
authoritative on the envelope, and nothing downstream re-parses ``Link``
(``ExecutionOutcome.metadata`` is a rate-limit-only allowlist that never carries
it, and never needs to). This is the sanctioned interface, not a second cursor:
the header is read where the reply is decoded, once, into the one place a cursor
lives.

WHAT THIS DELIBERATELY DOES NOT OWN
===================================
No error classification -- a non-2xx never reaches a ``ResultDecode`` (the
executor's :func:`classify_error` and GitHub's ``classify_github_failure`` own
that). No retry, no backoff, no auth, no payload copy, no cache. A structurally
impossible success body (a cursor tool whose ``pageInfo`` is not an object) is a
:class:`GithubDecodeError`, mirroring ``graph/payload.GraphPayloadError``.
"""

from __future__ import annotations

import json
from typing import Any, List, Mapping, Optional

from kiro_crew.connections.control_plane.production import HttpReply, decode_json_body
from kiro_crew.connections.control_plane.result import (
    CollectionPayload,
    ObjectPayload,
    OperationResult,
    result_with_payload,
)
from kiro_crew.connections.vendors.github.pagination import next_page_url

# Keys under which GitHub wraps a collection when the top-level body is an object
# rather than a bare array (check-runs, search results, actions listings). The
# first present list-valued key is the collection.
_WRAPPED_COLLECTION_KEYS = ("check_runs", "workflow_runs", "artifacts", "items")


class GithubDecodeError(ValueError):
    """A GitHub 2xx body was structurally malformed for its declared shape.

    A shaping fault only, mirroring ``graph/payload.GraphPayloadError``: a cursor
    tool whose ``pageInfo`` is present but not an object, for instance. A vendor
    HTTP failure is W01's typed boundary and never reaches a decode, so this is
    NOT a vendor-error class.
    """


def _parse_body(reply: HttpReply) -> Any:
    """Parse the reply body as JSON (array OR object), or ``None`` when empty.

    ``decode_json_body`` returns ``{}`` for a non-dict body, which would erase a
    JSON ARRAY (the shape every GitHub REST list returns), so this parses the raw
    bytes itself and keeps whatever JSON type came back. A non-JSON or empty body
    is ``None`` (an empty page, not an error).
    """

    if not reply.body:
        return None
    try:
        return json.loads(reply.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _rows_of(parsed: Any) -> Optional[List[Mapping[str, Any]]]:
    """The list of row dicts in ``parsed``, or ``None`` if it is not a collection.

    A bare JSON array is the rows directly; an object wrapping a list under one
    of :data:`_WRAPPED_COLLECTION_KEYS` yields that list. Anything else is not a
    collection (a single object, or an empty body).
    """

    if isinstance(parsed, list):
        return [it for it in parsed if isinstance(it, Mapping)]
    if isinstance(parsed, Mapping):
        for key in _WRAPPED_COLLECTION_KEYS:
            inner = parsed.get(key)
            if isinstance(inner, list):
                return [it for it in inner if isinstance(it, Mapping)]
    return None


def decode_rest_page(reply: HttpReply) -> OperationResult:
    """Decode a REST ``page``/``perPage`` GitHub list reply into a collection.

    The rows go into a :class:`CollectionPayload`; the continuation is GitHub's
    ``Link`` ``rel="next"`` URL, set as the SINGLE ``next_cursor`` on the envelope
    (the collection carries no cursor of its own). No ``rel="next"`` is the
    terminal page: ``status="ok"``, ``next_cursor=None``. A body that is not a
    collection at all is reported as an empty collection rather than guessed at.
    """

    rows = _rows_of(_parse_body(reply))
    payload = CollectionPayload(items=tuple(rows or ()))
    next_url = next_page_url(_header(reply.headers, "Link"))
    if next_url is None:
        return result_with_payload(payload, status="ok", next_cursor=None)
    return result_with_payload(payload, status="partial", next_cursor=next_url)


def decode_cursor_page(reply: HttpReply) -> OperationResult:
    """Decode a GraphQL-backed cursor (``after``) GitHub reply into a collection.

    Rows go into a :class:`CollectionPayload`. The GraphQL connection carries
    paging in the body's ``pageInfo`` (``hasNextPage`` / ``endCursor``), read from
    wherever the tool nests it; when ``hasNextPage`` is true with a non-empty
    ``endCursor`` that opaque cursor is the SINGLE ``next_cursor``, else the walk
    is done. A ``pageInfo`` present but not an object is a
    :class:`GithubDecodeError`. The rows are read from the first list-valued
    ``nodes`` / ``edges`` this module finds beside that ``pageInfo`` connection.
    """

    body = decode_json_body(reply)
    page_info = _find_page_info(body)
    rows = _connection_rows(body)
    payload = CollectionPayload(items=tuple(rows))
    if page_info is None:
        return result_with_payload(payload, status="ok", next_cursor=None)
    if not isinstance(page_info, Mapping):
        raise GithubDecodeError("GraphQL 'pageInfo' is present but is not an object")
    has_next = bool(page_info.get("hasNextPage"))
    end_cursor = page_info.get("endCursor")
    if has_next and isinstance(end_cursor, str) and end_cursor:
        return result_with_payload(payload, status="partial", next_cursor=end_cursor)
    return result_with_payload(payload, status="ok", next_cursor=None)


def _find_page_info(body: Any) -> Optional[Any]:
    """Return the first ``pageInfo`` value found anywhere in ``body``, or ``None``.

    A breadth-first walk over the parsed JSON: github-mcp-server's GraphQL tools
    nest the connection under different field paths, so keying on one fixed path
    would break the moment a tool nested it elsewhere.
    """

    queue: list[Any] = [body]
    while queue:
        node = queue.pop(0)
        if isinstance(node, Mapping):
            if "pageInfo" in node:
                return node["pageInfo"]
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return None


def _connection_rows(body: Any) -> List[Mapping[str, Any]]:
    """The row dicts of the first GraphQL connection found in ``body``.

    A connection exposes its rows under ``nodes`` (a list of records) or
    ``edges`` (a list of ``{node: {...}}``). This finds the first such list and
    returns its record dicts; an empty page yields ``[]``.
    """

    queue: list[Any] = [body]
    while queue:
        node = queue.pop(0)
        if isinstance(node, Mapping):
            nodes = node.get("nodes")
            if isinstance(nodes, list):
                return [it for it in nodes if isinstance(it, Mapping)]
            edges = node.get("edges")
            if isinstance(edges, list):
                out = [e.get("node") for e in edges if isinstance(e, Mapping)]
                return [n for n in out if isinstance(n, Mapping)]
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return []


def decode_single(reply: HttpReply) -> OperationResult:
    """Decode a single-object / unpaginated GitHub reply into an object payload.

    A ``Pagination.NONE`` operation returns one object (an issue, a commit, a
    file). It goes into an :class:`ObjectPayload`; there is no continuation, so
    ``next_cursor`` is ``None`` and the status is ``ok``. A body that parses to a
    non-object (an array, or empty) yields a ``None`` payload rather than a forced
    object -- the operation returned no single object.
    """

    parsed = _parse_body(reply)
    if isinstance(parsed, Mapping):
        return result_with_payload(ObjectPayload(object=parsed), status="ok", next_cursor=None)
    return result_with_payload(None, status="ok", next_cursor=None)


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    """Case-insensitive header lookup (HTTP header names are case-insensitive)."""

    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


__all__ = [
    "GithubDecodeError",
    "decode_cursor_page",
    "decode_rest_page",
    "decode_single",
]
