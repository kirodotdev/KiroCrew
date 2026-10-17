"""Incremental slot-detail reads: a cursor a polling reader echoes back.

A reader that polls the newest page of a session (the app-sdk ``ChatEmbed``)
already holds almost every row the next read returns. With a cursor it asks only
for what may have changed:

* A paged read that passes ``cursor=1`` gets ``cursor: {from, since, digest}``
  back. ``from`` is the absolute row index of the page's first row, ``since`` the
  index of its last row, and ``digest`` a digest of the rows in ``[from, since)``.
* The next read passes those three values back (``from``, ``since``, ``digest``
  query parameters). The server composes the page as before and compares the
  digest of ``[from, since)`` again. Equal means the reader's copy
  of those rows is current, so the response carries only rows from ``since`` on,
  echoed as ``delta: {from, since}``. Anything else -- a row edited in place
  (an approval resolved, a card patched), a rewind, a rotation, a different
  redaction context -- fails the comparison and the response is the full page,
  with no ``delta``, which the reader takes as a replacement.

The last row is never inside the span: the folded streaming row changes on every
chunk, so it is always re-sent. The digest is built from each row's render-memo
identity (``wire_render.prepare_message_rows``): a keyed digest of the whole
stored row plus the redaction context, which is exactly what the render memo
trusts to mean "renders the same". A row with no identity (a streaming chunk, an
OAuth row, anything in an owner-view scope) issues no cursor over it. The digest
is never a hash of display text, and it is an integrity check, not a secret: a
reader that forges one only corrupts its own copy, and every row it is served is
rendered and redacted as on a full read.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# A reader's held page is never wider than the handler's page ceiling, so a span
# wider than it is not a cursor this server issued; it is answered with a full page.
from kiro_crew.dashboard.chat_utils import SLOT_DETAIL_MAX_LIMIT as DELTA_MAX_SPAN

_DIGEST_RE = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class DeltaCursor:
    """The ``from``/``since``/``digest`` triple a reader echoes back."""

    start: int
    since: int
    digest: str


@dataclass(frozen=True)
class PagePlan:
    """What one cursor-mode response serves and reports."""

    fragments: list[str]
    next_before: int
    has_more: bool
    delta: dict[str, int] | None
    cursor: dict[str, Any] | None


class DeltaQueryError(ValueError):
    """A malformed cursor query; the handler answers 400."""


def parse_delta_query(query: Mapping[str, str]) -> tuple[DeltaCursor | None, bool]:
    """``(cursor, cursor_mode)`` from a slot-detail query string.

    ``cursor_mode`` is true when the response must carry a ``cursor`` (``cursor=1``
    or an echoed cursor). The three echo parameters come together or not at all.
    """
    names = ("from", "since", "digest")
    present = [n for n in names if n in query]
    want = query.get("cursor") == "1"
    if not present:
        return None, want
    if len(present) != len(names):
        raise DeltaQueryError("from, since and digest must be passed together")
    try:
        start = int(query["from"])
        since = int(query["since"])
    except ValueError as exc:
        raise DeltaQueryError("from and since must be integers") from exc
    digest = query["digest"]
    if start < 0 or since < start or not _DIGEST_RE.fullmatch(digest):
        raise DeltaQueryError("from/since/digest is not a cursor this server issues")
    return DeltaCursor(start=start, since=since, digest=digest), True


def read_limit(window: int, cursor: DeltaCursor | None) -> int:
    """Rows to compose so the page reaches back to the cursor's ``from``.

    The page must hold the reader's span ``[from, since)`` to check it, plus up
    to *window* newer rows. A span wider than any page this server serves is not
    honoured, so it costs no wider read either.
    """
    if cursor is None or cursor.since - cursor.start > DELTA_MAX_SPAN:
        return window
    return window + (cursor.since - cursor.start)


def render_from(page_start: int, total: int, window: int, cursor: DeltaCursor | None) -> int:
    """The first composed row a cursor-mode response renders.

    The read reaches back to the cursor's ``from`` only so the span can be
    checked; rows older than both that span and the new window are neither
    hashed nor served, so they are not rendered either. Rendering stays about
    one page plus the rows that arrived since the reader's last poll.
    """
    first = total - window if cursor is None else min(cursor.start, total - window)
    return max(page_start, first)


def span_digest(identities: Sequence[bytes], start: int, since: int) -> str:
    """Digest of the row identities ``[start, since)``, bound to their position."""
    h = hashlib.blake2b(digest_size=16)
    h.update(f"{start}:{since}\n".encode("ascii"))
    for identity in identities:
        h.update(len(identity).to_bytes(4, "big"))
        h.update(identity)
    return h.hexdigest()


def plan_page(
    fragments: Sequence[str | None],
    identities: Sequence[bytes | None],
    *,
    page_start: int,
    total: int,
    window: int,
    cursor: DeltaCursor | None,
) -> PagePlan:
    """Decide what a cursor-mode response serves.

    *fragments* are the page's rendered rows, one per collapsed row starting at
    absolute index *page_start*; ``None`` is a row that renders as nothing (an
    empty streaming run). *identities* are the same rows' identities, ``None``
    where none vouches for the render. *window* is the reader's page size.

    When the fragments do not line up one-to-one with ``[page_start, total)`` no
    position can be trusted: the whole composed page is served as a replacement
    and no cursor is issued, so the reader's next read is a plain full one.
    """
    if len(fragments) != total - page_start or len(identities) != len(fragments):
        return PagePlan(
            fragments=[f for f in fragments if f is not None],
            next_before=page_start,
            has_more=page_start > 0,
            delta=None,
            cursor=None,
        )

    def _span(a: int, b: int) -> list[bytes] | None:
        seg = identities[a - page_start : b - page_start]
        if any(i is None or fragments[a - page_start + k] is None for k, i in enumerate(seg)):
            return None
        return [i for i in seg if i is not None]

    new_from = max(page_start, total - window)
    applied = False
    if (
        cursor is not None
        and cursor.since - cursor.start <= DELTA_MAX_SPAN
        and page_start <= cursor.start
        and cursor.since <= total
    ):
        held = _span(cursor.start, cursor.since)
        applied = (
            held is not None and span_digest(held, cursor.start, cursor.since) == cursor.digest
        )
    serve_from = max(cursor.since, new_from) if applied and cursor is not None else new_from
    served = [f for f in fragments[serve_from - page_start :] if f is not None]

    next_since = max(new_from, total - 1)
    next_span = _span(new_from, next_since)
    next_cursor = (
        {
            "from": new_from,
            "since": next_since,
            "digest": span_digest(next_span, new_from, next_since),
        }
        if next_span is not None
        else None
    )
    return PagePlan(
        fragments=served,
        next_before=new_from,
        has_more=new_from > 0,
        delta=(
            {"from": cursor.start, "since": cursor.since}
            if applied and cursor is not None
            else None
        ),
        cursor=next_cursor,
    )
