"""Microsoft Graph pagination / cursor protocol (pure logic).

WHY A CLOSED SET OF VARIANTS, NOT A GENERIC LOOP
================================================
Graph does not have "a pagination scheme". It has SEVERAL, and a generic
"follow ``@odata.nextLink`` until it is absent" loop is wrong for most of the
catalog: it would page collections that do not paginate, drop the delta cursor
a change-tracked collection ends on, silently succeed against a collection
whose ``$filter`` is mandatory, and misread a ``410 Gone`` resync as a terminal
error. So this module models the ACTUAL evidence variants as an explicit closed
enum (:class:`PagingMode`) and refuses to apply a paging engine to an operation
whose mode does not have one.

THE VARIANTS (each observed in campaign evidence)
=================================================
* ``NEXT_LINK`` -- plain ``@odata.nextLink``: follow the opaque link until
  absent. The link is opaque and ALREADY carries the cursor (``$skiptoken``);
  the caller re-sends it verbatim and never reconstructs query params.
* ``TOP_SERVER_DRIVEN`` -- ``$top`` is a page-SIZE hint the server may honor or
  cap; the continuation is STILL a ``@odata.nextLink``. The distinction that
  matters: ``$top`` is applied to the FIRST request only and MUST NOT be
  re-appended to the server's nextLink (the link already encodes the page size).
* ``NEXT_LINK_THEN_DELTA`` -- a change-tracked collection pages via
  ``@odata.nextLink`` and TERMINATES on ``@odata.deltaLink`` instead of running
  out of links. The deltaLink is not an end-of-data signal to discard: it is the
  cursor for the NEXT round of change tracking, surfaced to the caller. Together
  with ``DELTA_RESYNC``, this is a delta-terminated mode.
* ``CALENDAR_VIEW`` -- pages via ``@odata.nextLink`` like ``NEXT_LINK``, but the
  FIRST request's mandatory ``startDateTime``/``endDateTime`` window (see
  ``payload.CalendarViewWindow``) is a precondition of paging at all; this mode
  records that the window is required so the driver refuses a first page built
  without one.
* ``FILTER_REQUIRED`` -- some collections reject a request with no ``$filter``;
  the mode records that ``$filter`` is a precondition, so the driver refuses a
  first page whose request carries none.
* ``DELTA_RESYNC`` -- like ``NEXT_LINK_THEN_DELTA`` on successful pages, this
  mode terminates on ``@odata.deltaLink``. A delta cursor can also expire; Graph
  then answers ``HTTP 410 Gone`` carrying ``resyncChangesApplyDifferences`` or
  ``resyncChangesUploadDifferences`` and a fresh link in the ``Location`` header.
  The correct response is NOT to fail: it is to RE-ENUMERATE the whole collection
  from that fresh link. A 410 resync applies to EITHER delta-terminated mode, so
  :func:`parse_resync` is deliberately not gated by mode.
* ``NONE`` -- the operation does not paginate (a single-fetch, a mutation, a
  non-collection read). 38 of the campaign's 73 catalog operations are pagination
  ``n/a``; the engine MUST NOT be applied to them by default, so ``NONE`` is a
  first-class value the driver refuses to page, not the absence of a value.
* ``UNKNOWN`` -- exactly one collection in the evidence shows "no explicit
  nextLink in the fetched examples". Its paging is genuinely unknown: we do NOT
  assume it pages, and we do NOT fabricate a nextLink loop for it. ``UNKNOWN`` is
  preserved verbatim so a later evidence pass can resolve it to a real mode; the
  driver refuses to page it, distinctly from ``NONE`` (which is a POSITIVE claim
  that it does not paginate).

WHAT THIS DELIBERATELY DOES NOT OWN
===================================
No vendor-error taxonomy. ``410 Gone`` is handled here ONLY as the specific
delta-resync paging signal above -- recognized by its resync annotation, not
classified as an error. Every other non-2xx (401/403/404/429/5xx) is W01's
typed boundary, mapped by a separate leaf; this module never sees them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from kiro_crew.connections.vendors.microsoft.graph.payload import GraphPage


class PagingMode(str, Enum):
    """The pagination contract of a single operation. Closed set; see module doc."""

    NEXT_LINK = "next_link"
    TOP_SERVER_DRIVEN = "top_server_driven"
    NEXT_LINK_THEN_DELTA = "next_link_then_delta"
    CALENDAR_VIEW = "calendar_view"
    FILTER_REQUIRED = "filter_required"
    DELTA_RESYNC = "delta_resync"
    NONE = "none"
    UNKNOWN = "unknown"


#: Modes whose successful terminal page must carry ``@odata.deltaLink``.
DELTA_TERMINATED_MODES = frozenset({PagingMode.NEXT_LINK_THEN_DELTA, PagingMode.DELTA_RESYNC})


#: The resync annotations Graph puts in a ``410 Gone`` body when a delta cursor
#: has expired. Recognized as a paging signal, NOT as an error to classify.
RESYNC_APPLY = "resyncChangesApplyDifferences"
RESYNC_UPLOAD = "resyncChangesUploadDifferences"


class PagingError(ValueError):
    """A paging precondition was violated, or a mode has no paging engine.

    A shaping/precondition fault only. NOT a vendor error -- vendor errors are
    W01's typed boundary. Raised for: applying the driver to a ``NONE`` /
    ``UNKNOWN`` mode; a ``CALENDAR_VIEW`` / ``FILTER_REQUIRED`` first page whose
    request omits its mandatory precondition; a delta-terminated page missing
    its delta cursor; a malformed resync signal.
    """


@dataclass(frozen=True)
class PageStep:
    """The driver's verdict for one page: whether to continue, and how.

    Exactly one of ``next_cursor`` / ``delta_cursor`` is set when the collection
    continues or completes-with-delta respectively; both ``None`` means the
    collection is exhausted with no delta cursor (a plain ``NEXT_LINK`` run that
    ran out of links). ``done`` is True when no further page request is owed.
    ``delta_cursor`` being set with ``done=True`` is the change-tracking case:
    there is no next page NOW, but the caller holds a cursor to resume later.
    """

    done: bool
    next_cursor: Optional[str] = None
    delta_cursor: Optional[str] = None


_PAGEABLE = frozenset(
    {
        PagingMode.NEXT_LINK,
        PagingMode.TOP_SERVER_DRIVEN,
        PagingMode.NEXT_LINK_THEN_DELTA,
        PagingMode.CALENDAR_VIEW,
        PagingMode.FILTER_REQUIRED,
        PagingMode.DELTA_RESYNC,
    }
)


def assert_pageable(mode: PagingMode) -> None:
    """Raise :class:`PagingError` if ``mode`` must not be paged, with the reason."""

    if mode is PagingMode.NONE:
        raise PagingError(
            "operation is pagination 'n/a' (mode NONE): the paging engine must "
            "not be applied to it"
        )
    if mode is PagingMode.UNKNOWN:
        raise PagingError(
            "operation paging is UNKNOWN (no explicit nextLink in fetched "
            "examples): do not assume it paginates or fabricate a nextLink"
        )
    if mode not in _PAGEABLE:  # pragma: no cover - exhaustive guard for a new enum value
        raise PagingError(f"unhandled paging mode {mode!r}")


def check_first_page_preconditions(mode: PagingMode, query_params: Mapping[str, str]) -> None:
    """Refuse a first-page request that omits a mandatory precondition for ``mode``.

    ``CALENDAR_VIEW`` requires ``startDateTime`` AND ``endDateTime``;
    ``FILTER_REQUIRED`` requires ``$filter``. Every other pageable mode has no
    precondition. Checked against the already-shaped query param map so the
    refusal happens before any request would be dispatched.
    """

    assert_pageable(mode)
    if mode is PagingMode.CALENDAR_VIEW:
        missing = [p for p in ("startDateTime", "endDateTime") if not query_params.get(p)]
        if missing:
            raise PagingError("calendarView first page requires " + " and ".join(missing))
    elif mode is PagingMode.FILTER_REQUIRED:
        if not query_params.get("$filter"):
            raise PagingError("this collection requires a $filter on the first page")


def next_step(mode: PagingMode, page: GraphPage) -> PageStep:
    """Given a fetched ``page``, decide the next paging action for ``mode``.

    * ``NEXT_LINK`` / ``TOP_SERVER_DRIVEN`` / ``CALENDAR_VIEW`` / ``FILTER_REQUIRED``:
      continue on ``next_link`` while present; a ``delta_link`` on one of these
      is unexpected and surfaced as a delta cursor rather than silently dropped.
    * Delta-terminated modes (``NEXT_LINK_THEN_DELTA`` / ``DELTA_RESYNC``):
      continue on ``next_link``; when it is absent, require ``delta_link`` and
      complete WITH that delta cursor (done, resumable). A 410 resync is a
      SEPARATE, mode-agnostic entry point (:func:`parse_resync`) that produces a
      fresh starting cursor, not a step from a success page.

    The opaque link is returned VERBATIM as the cursor -- the caller re-sends it
    unchanged and never rebuilds query params (the link already carries the
    ``$skiptoken``/``$deltatoken``). In particular ``TOP_SERVER_DRIVEN`` does not
    re-append ``$top`` to the nextLink.
    """

    assert_pageable(mode)

    if page.next_link is not None:
        # More pages remain regardless of mode; the delta terminator only
        # applies once the nextLink is exhausted.
        return PageStep(done=False, next_cursor=page.next_link)

    if page.delta_link is not None:
        # No next page; a delta cursor means "done for now, resume later".
        # For NEXT_LINK_THEN_DELTA / DELTA_RESYNC this is the expected end; for
        # the plain modes it is unexpected but we surface it rather than drop it.
        return PageStep(done=True, delta_cursor=page.delta_link)

    if mode in DELTA_TERMINATED_MODES:
        raise PagingError(f"{mode.value} terminal page is missing the required @odata.deltaLink")

    # A non-delta collection is exhausted with no cursor.
    return PageStep(done=True)


@dataclass(frozen=True)
class ResyncSignal:
    """A parsed ``410 Gone`` delta-resync signal.

    ``kind`` is whichever of the two resync annotations Graph sent
    (``RESYNC_APPLY`` -> keep local state and apply differences;
    ``RESYNC_UPLOAD`` -> local state is untrusted, re-upload). ``fresh_link`` is
    the ``Location`` header's new link to RE-ENUMERATE the whole collection from.
    """

    kind: str
    fresh_link: str


def parse_resync(
    status_code: int,
    body: Mapping[str, Any],
    headers: Mapping[str, str],
) -> Optional[ResyncSignal]:
    """Recognize a delta-cursor-expired resync from a ``410 Gone``.

    Returns a :class:`ResyncSignal` when ``status_code`` is 410 AND the body
    carries a resync annotation AND the ``Location`` header carries a fresh link.
    Returns ``None`` for any non-410 status (that is not this function's concern
    -- other statuses are W01's typed boundary). Raises :class:`PagingError`
    only for a 410 that claims a resync annotation but is MISSING its ``Location``
    link -- a malformed resync we must not silently treat as terminal.

    Header lookup is case-insensitive (HTTP header names are).
    """

    if status_code != 410:
        return None

    kind = _resync_kind(body)
    if kind is None:
        # A 410 without a resync annotation is not a delta resync; it is a
        # plain gone/not-found for W01 to type. Not ours.
        return None

    location = _lookup_header(headers, "location")
    if not location or not location.strip():
        raise PagingError(f"410 resync ({kind}) is missing the Location header's fresh link")
    return ResyncSignal(kind=kind, fresh_link=location.strip())


def _resync_kind(body: Mapping[str, Any]) -> Optional[str]:
    """Return the resync annotation present in ``body``, or ``None``.

    Graph places the annotation on the ``error`` object; accept it at either the
    top level or under ``error`` so a caller need not pre-unwrap the envelope.
    """

    for scope in (body, body.get("error") if isinstance(body.get("error"), Mapping) else None):
        if not isinstance(scope, Mapping):
            continue
        for key in (RESYNC_APPLY, RESYNC_UPLOAD):
            if key in scope or scope.get("code") == key:
                return key
    return None


def _lookup_header(headers: Mapping[str, str], name: str) -> Optional[str]:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None
