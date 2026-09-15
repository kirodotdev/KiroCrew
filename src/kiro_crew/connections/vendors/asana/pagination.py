"""Asana pagination contract: offset/limit, next_page cursor, TTL-unknown token.

WHAT THIS OWNS
==============
The pure-logic pagination model for Asana's offset/limit paginated endpoints
and the two hard ceilings the API imposes. Network-free: it validates a
request's paging parameters, reads a response's ``next_page`` envelope into a
typed cursor, and decides whether more pages remain -- it makes no call.

THE CONTRACT
============
* ``limit`` is the page size, valid ONLY in the inclusive range 1..100. Asana
  rejects a limit outside it; :func:`normalize_page_request` refuses it at
  shaping time rather than as a discovered 400. ``limit`` is optional (Asana
  applies a default page size when omitted), but when present it must be in
  range.
* ``offset`` is a CURSOR TOKEN, not a numeric row index. It is an opaque
  string handed back by the previous page's ``next_page.offset`` -- a caller
  never fabricates one, and it is never added to or compared as a number. The
  first page carries no offset.
* A response's ``next_page`` is ``{offset, path, uri}`` while more pages
  remain and is ``null`` once the collection is exhausted. :func:`parse_next_page`
  reads it into a typed :class:`NextPage` (or ``None``), and :func:`has_more`
  answers the continuation question from that single source.

THE OFFSET TOKEN IS TREATED AS EXPIRABLE (EVIDENCE UNKNOWN)
===========================================================
Asana documents that an offset token "will expire after some time" but does
NOT publish a TTL. This connector therefore models the token as EXPIRABLE with
an UNKNOWN lifetime: it never assumes a token stays valid indefinitely, and a
token the server rejects as stale is surfaced as a specific, actionable error
(:class:`OffsetTokenExpired`) rather than retried blindly or treated as a
permanent cursor. The code asserts no concrete TTL value, because none is
documented -- the unknown is preserved as an unknown.

Detecting expiry itself is the server's job (this layer holds no clock and
makes no call); :func:`classify_offset_rejection` turns a rejection the caller
observed into the typed signal, without asserting which HTTP status Asana uses
for it (also unobserved -- see :mod:`kiro_crew.connections.vendors.asana.errors`).

THE ~1000-OBJECT NON-PAGINATED CEILING
======================================
Asana's non-paginated ("legacy", per Asana's own docs) endpoints truncate at
approximately 1000 objects and may time out. That ceiling is modeled explicitly
as :data:`LEGACY_UNPAGINATED_TRUNCATION_LIMIT` and
:func:`legacy_unpaginated_truncated`, so a caller can tell a genuinely-complete
small result from a silently-capped one -- and is steered toward a paginated
endpoint instead. The value is labelled approximate because Asana's own wording
is ("around 1000").

WHAT THIS DELIBERATELY DOES NOT OWN
===================================
No MCP pagination parameter names -- several MCP tools state "supports
pagination" without publishing the continuation-token parameter, whose
authoritative source is the live ``tools/list``. This module models the REST
offset/limit contract; it does not hard-code an MCP cursor parameter it cannot
cite. A malformed paging shape raises :class:`AsanaPaginationError`, a plain
``ValueError`` subclass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

#: Inclusive bounds Asana enforces on a page-size ``limit``.
LIMIT_MIN = 1
LIMIT_MAX = 100

#: Approximate object ceiling on a NON-paginated ("legacy") endpoint.
#: Asana documents "around 1000"; the value is treated as approximate, and a
#: result AT the ceiling is treated as POSSIBLY truncated (see
#: :func:`legacy_unpaginated_truncated`) rather than certainly complete.
LEGACY_UNPAGINATED_TRUNCATION_LIMIT = 1000


class AsanaPaginationError(ValueError):
    """A paging request or response shape was invalid.

    A shaping fault (a limit out of range, a non-string offset token, a
    malformed ``next_page`` envelope). Distinct from :class:`OffsetTokenExpired`,
    which is a modeled runtime condition, not a shaping mistake.
    """


class OffsetTokenExpired(RuntimeError):
    """An offset cursor token was rejected by the server as stale.

    Modeled because Asana's offset tokens expire on an UNDOCUMENTED schedule:
    the connector must not assume a token is permanently valid, and a stale
    token is a distinct, recoverable condition (restart the listing from the
    first page) -- not a generic failure and not something to retry with the
    same dead token. This class carries no HTTP status: which code Asana returns
    for an expired offset is unobserved and deliberately not asserted here.
    """


@dataclass(frozen=True)
class PageRequest:
    """A validated paging request.

    ``limit`` is ``None`` (server default) or an int in 1..100. ``offset`` is
    ``None`` (first page) or an opaque continuation token from a prior
    ``next_page.offset``. The two are independent: a first-page request may set
    a limit with no offset.
    """

    limit: Optional[int]
    offset: Optional[str]


def normalize_page_request(*, limit: object = None, offset: object = None) -> PageRequest:
    """Validate and normalize paging parameters.

    ``limit``: refused unless ``None`` or an ``int`` within 1..100. A ``bool`` is
    rejected explicitly (``True`` is an ``int`` in Python and would slip through
    as ``1``). ``offset``: refused unless ``None`` or a non-empty string -- it is
    an opaque cursor, never a number, so an ``int`` offset is a shaping error
    (the caller is treating a token as a row index).
    """

    validated_limit: Optional[int] = None
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise AsanaPaginationError(
                f"limit must be an int in {LIMIT_MIN}..{LIMIT_MAX}, got {type(limit).__name__}"
            )
        if not (LIMIT_MIN <= limit <= LIMIT_MAX):
            raise AsanaPaginationError(f"limit must be in {LIMIT_MIN}..{LIMIT_MAX}, got {limit}")
        validated_limit = limit

    validated_offset: Optional[str] = None
    if offset is not None:
        if not isinstance(offset, str):
            raise AsanaPaginationError(
                "offset must be an opaque cursor string from a prior "
                f"next_page.offset, not a {type(offset).__name__} -- it is not a row index"
            )
        if not offset.strip():
            raise AsanaPaginationError("offset must be a non-empty cursor token when supplied")
        validated_offset = offset

    return PageRequest(limit=validated_limit, offset=validated_offset)


@dataclass(frozen=True)
class NextPage:
    """A response's ``next_page`` continuation cursor.

    ``offset`` is the opaque token to pass on the next request. ``path`` and
    ``uri`` are Asana's own continuation references, carried for completeness.
    Present only while more pages remain; a ``null`` ``next_page`` parses to
    ``None`` (see :func:`parse_next_page`), which is how exhaustion is
    represented.
    """

    offset: str
    path: Optional[str] = None
    uri: Optional[str] = None


def parse_next_page(envelope: Mapping[str, Any]) -> Optional[NextPage]:
    """Read a response envelope's ``next_page`` into a :class:`NextPage`.

    Returns ``None`` when ``next_page`` is absent or explicitly ``null`` -- the
    documented exhaustion signal. Raises :class:`AsanaPaginationError` for a
    malformed cursor: a ``next_page`` present but not an object, or one missing a
    usable ``offset`` token (a continuation the caller could not act on).
    """

    if not isinstance(envelope, Mapping):
        raise AsanaPaginationError(
            f"response envelope must be a mapping, got {type(envelope).__name__}"
        )
    if "next_page" not in envelope:
        return None
    next_page = envelope["next_page"]
    if next_page is None:
        return None
    if not isinstance(next_page, Mapping):
        raise AsanaPaginationError("next_page must be an object or null")
    offset = next_page.get("offset")
    if not isinstance(offset, str) or not offset.strip():
        raise AsanaPaginationError(
            "next_page is present but carries no usable offset token; "
            "a non-null next_page must provide a continuation cursor"
        )
    path = next_page.get("path")
    uri = next_page.get("uri")
    return NextPage(
        offset=offset,
        path=path if isinstance(path, str) else None,
        uri=uri if isinstance(uri, str) else None,
    )


def has_more(envelope: Mapping[str, Any]) -> bool:
    """Whether the collection has a further page, per its ``next_page``.

    Single source of truth: ``True`` iff :func:`parse_next_page` yields a
    cursor. A caller never infers "more pages" from a full page count -- a page
    exactly ``limit`` long with a ``null`` ``next_page`` is the last page.
    """

    return parse_next_page(envelope) is not None


def continue_request(base: PageRequest, envelope: Mapping[str, Any]) -> Optional[PageRequest]:
    """Build the next :class:`PageRequest` from a response, or None if exhausted.

    Carries the original ``limit`` forward and adopts the response's own
    ``next_page.offset`` as the cursor. Returns ``None`` when the collection is
    exhausted, so a paging loop terminates on this rather than on a page-count
    heuristic. The produced offset is the server's token, never a fabricated one.
    """

    cursor = parse_next_page(envelope)
    if cursor is None:
        return None
    return PageRequest(limit=base.limit, offset=cursor.offset)


def classify_offset_rejection(*, stale: bool, detail: str = "") -> OffsetTokenExpired:
    """Build the typed stale-offset signal from a rejection the caller observed.

    The caller (which made the actual call) decides ``stale`` from the server's
    response; this pure layer only mints the typed condition, deliberately
    WITHOUT asserting the HTTP status Asana uses for an expired offset (an
    unobserved value). ``stale`` must be ``True`` -- calling this for a
    non-stale rejection is a programming error, since the only condition it
    models is expiry. Recovery is to restart the listing from the first page
    (no offset), never to resend the dead token.
    """

    if not stale:
        raise AsanaPaginationError(
            "classify_offset_rejection models offset EXPIRY only; do not call it "
            "for a rejection the caller has not determined to be a stale offset"
        )
    message = "offset cursor token expired; restart the listing from the first page"
    if detail:
        message = f"{message} ({detail})"
    return OffsetTokenExpired(message)


def legacy_unpaginated_truncated(object_count: int) -> bool:
    """Whether a NON-paginated endpoint result is possibly truncated.

    Asana's legacy (non-paginated) endpoints cap at ~1000 objects and may time
    out, giving no ``next_page`` to signal there was more. A result AT or above
    the ceiling is treated as POSSIBLY truncated (return ``True``) so a caller
    does not mistake a capped result for a complete one, and is steered to a
    paginated endpoint. Below the ceiling the result is complete. A negative
    count is a shaping error.
    """

    if object_count < 0:
        raise AsanaPaginationError(f"object_count must be non-negative, got {object_count}")
    return object_count >= LEGACY_UNPAGINATED_TRUNCATION_LIMIT
