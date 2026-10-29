"""Server-side session list filtering by creator, before pagination.

Input is the persisted session catalog (``SessionCatalogProjection.list_sessions``
rows, each carrying a public ``attribution``). The filter, the search and the
counts are computed over the whole catalog, then one page is cut, so no session
is ever hidden by a client-side filter over a partial page.

Order and paging: newest ``modified`` first, ties broken by a digest of the key,
which is a total order. The cursor is a keyset position (the last row's
``modified`` and key digest), not an offset, so a page never repeats a row
already returned in the same walk. A session modified during the walk moves to
the top and is seen on the next walk from the start, not twice in this one.

Counts: ``total_matching`` is every session passing the search and the creator
filter. ``hidden_by_filter`` is every session passing the search that the
creator filter excluded, so ``total_matching + hidden_by_filter`` is the number
of sessions the search alone would list.

The filter is discovery, not access control: it only narrows what an already
authorized viewer sees.

``unattributed`` (``unknown`` is accepted as its older spelling) lists sessions
with no human creator and no named trigger. A session a schedule or a webhook
started names that trigger, so it is listed under All and its trigger, never
under Unattributed.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from kiro_crew.shared_crew.attribution import (
    creator_of_public,
    public_attribution_from_meta,
    trigger_of_public,
)
from kiro_crew.shared_crew.codec import ContractError
from kiro_crew.shared_crew.contracts import Principal, SessionPage, SessionQuery

#: Row keys copied from a catalog row into a page item.
_ITEM_KEYS = ("key", "title", "agent", "modified", "created", "folder_id", "memory_mode")


def _key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _sort_key(row: Mapping[str, Any]) -> tuple[float, str]:
    try:
        modified = float(row.get("modified") or 0.0)
    except (TypeError, ValueError):
        modified = 0.0
    return (-modified, _key_digest(str(row.get("key") or "")))


def encode_cursor(position: tuple[float, str]) -> str:
    raw = f"{position[0]!r}|{position[1]}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[float, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        modified, digest = raw.split("|", 1)
        position = (float(modified), digest)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ContractError("invalid_cursor", "cursor", "malformed cursor") from None
    if len(digest) != 16 or any(c not in "0123456789abcdef" for c in digest):
        raise ContractError("invalid_cursor", "cursor", "malformed cursor")
    return position


def _matches_search(row: Mapping[str, Any], needle: str) -> bool:
    if not needle:
        return True
    hay = f"{row.get('title') or ''}\n{row.get('key') or ''}".casefold()
    return needle in hay


def _item(row: Mapping[str, Any]) -> dict[str, Any]:
    item = {k: row[k] for k in _ITEM_KEYS if k in row}
    attribution = row.get("attribution")
    item["attribution"] = (
        dict(attribution)
        if isinstance(attribution, Mapping)
        else public_attribution_from_meta({}, key=str(row.get("key") or ""))
    )
    return item


def query_sessions(
    rows: Iterable[Mapping[str, Any]],
    query: SessionQuery,
    viewer: Principal,
    *,
    exclude: Callable[[str], bool] | None = None,
) -> SessionPage:
    """Filter, count and page *rows* for *viewer*.

    *exclude* drops rows that are not conversations (machine transcripts)
    before anything is counted, so the counts describe the list the user sees.
    Raises ``ContractError`` for a malformed cursor.
    """
    after = decode_cursor(query.cursor) if query.cursor else None
    needle = query.search.strip().casefold()
    matching: list[tuple[tuple[float, str], Mapping[str, Any]]] = []
    hidden = 0
    for row in rows:
        key = str(row.get("key") or "")
        if exclude is not None and exclude(key):
            continue
        if not _matches_search(row, needle):
            continue
        attribution = row.get("attribution")
        triggered = trigger_of_public(attribution) is not None
        if query.matches(creator_of_public(attribution), viewer, triggered=triggered):
            matching.append((_sort_key(row), row))
        else:
            hidden += 1
    matching.sort(key=lambda pair: pair[0])
    start = 0
    if after is not None:
        start = next((i for i, (pos, _) in enumerate(matching) if pos > after), len(matching))
    page = matching[start : start + query.limit]
    has_more = start + query.limit < len(matching)
    return SessionPage(
        items=tuple(_item(row) for _, row in page),
        total_matching=len(matching),
        hidden_by_filter=hidden,
        next_cursor=encode_cursor(page[-1][0]) if page and has_more else "",
    )
