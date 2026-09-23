"""Zoom pagination contract: per-endpoint, never assumed uniform.

Pure logic. Zoom does not paginate every endpoint the same way, so this module
classifies an endpoint into exactly one pagination discipline and, for the
cursor shapes, carries the cursor state (including any date window that MUST
stay fixed across pages). It performs no request.

The four disciplines:

* ``CURSOR`` -- ``next_page_token`` + ``page_size``; token absent on the last
  page. The list/search shape.
* ``DATE_WINDOWED_CURSOR`` -- a cursor additionally windowed by ``from``/``to``
  (``recordings.list``). The window is part of the cursor's stable state.
* ``CURSORLESS`` -- one response, no token (the instances list, single-resource
  GETs). Declared cursor-less, never assumed paged.
* ``MONTH_ITERATED`` -- iterated by ``month``/``year`` (``report/daily``), not by
  a cursor.

See ``docs/system-specs/modules/connector-zoom.md``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional


class Pagination(enum.Enum):
    """The pagination discipline an endpoint follows."""

    CURSOR = "cursor"
    DATE_WINDOWED_CURSOR = "date_windowed_cursor"
    CURSORLESS = "cursorless"
    MONTH_ITERATED = "month_iterated"

    @property
    def is_cursor_paged(self) -> bool:
        """Whether following a ``next_page_token`` is valid for this discipline."""
        return self in (Pagination.CURSOR, Pagination.DATE_WINDOWED_CURSOR)


# Endpoint identities are the campaign's neutral operation ids (see the W00 Zoom
# operation catalog). Kept as a data table so a newly-classified endpoint is a
# data change, not a control-flow edit.
_PAGINATION_BY_OPERATION: dict[str, Pagination] = {
    # cursor list/search
    "zoom.meetings.list": Pagination.CURSOR,
    "zoom.webinars.list": Pagination.CURSOR,
    "zoom.meetings.past_participants": Pagination.CURSOR,
    "zoom.webinars.participants": Pagination.CURSOR,
    "zoom.meetings.registrants": Pagination.CURSOR,
    "zoom.webinars.registrants": Pagination.CURSOR,
    "zoom.contacts.search": Pagination.CURSOR,
    "zoom.devices.list": Pagination.CURSOR,
    # date-windowed cursor
    "zoom.recordings.list": Pagination.DATE_WINDOWED_CURSOR,
    # cursor-less: one response, no token
    "zoom.meetings.get": Pagination.CURSORLESS,
    "zoom.meetings.recurring_instances": Pagination.CURSORLESS,
    "zoom.meetings.ai_summary": Pagination.CURSORLESS,
    "zoom.recordings.get_detail": Pagination.CURSORLESS,
    "zoom.users.me_profile": Pagination.CURSORLESS,
    "zoom.webinars.get_detail": Pagination.CURSORLESS,
    # month-iterated, not cursor
    "zoom.reports.daily_usage": Pagination.MONTH_ITERATED,
}


def classify_pagination(operation_id: str) -> Pagination:
    """Return the pagination discipline for a Zoom operation.

    Raises ``KeyError`` for an unknown operation rather than defaulting to a
    cursor -- guessing "it is probably cursor-paged" is exactly the failure the
    cursor-less pin (negative fault test 3) exists to prevent, so an unclassified
    endpoint must be added to the table deliberately.
    """
    try:
        return _PAGINATION_BY_OPERATION[operation_id]
    except KeyError as exc:
        raise KeyError(
            f"unknown Zoom operation {operation_id!r}: classify its pagination "
            "explicitly rather than assuming a cursor"
        ) from exc


@dataclass(frozen=True)
class CursorRequest:
    """The parameters for the next page of a cursor-paged Zoom request.

    ``page_size`` and the optional ``next_page_token`` are the cursor. ``date_from``
    / ``date_to`` are populated only for :data:`Pagination.DATE_WINDOWED_CURSOR`
    and, once set, MUST be carried unchanged across every subsequent page.
    """

    page_size: int
    next_page_token: Optional[str]
    date_from: Optional[str] = None
    date_to: Optional[str] = None


def next_cursor_request(
    operation_id: str,
    page_size: int,
    next_page_token: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> CursorRequest:
    """Build the next cursor request for a cursor-paged Zoom endpoint.

    Refuses a cursor-less or month-iterated endpoint (negative fault test 3):
    asking this function to follow a cursor on the instances list or a
    single-resource GET raises ``ValueError`` rather than producing a request
    that loops for a ``next_page_token`` that never appears.

    For :data:`Pagination.DATE_WINDOWED_CURSOR` (``recordings.list``) both
    ``date_from`` and ``date_to`` are required and are carried on the returned
    request so the window stays fixed across pages; supplying them for a plain
    cursor endpoint is refused so the window is never silently attached where
    Zoom does not expect it.
    """
    discipline = classify_pagination(operation_id)
    if not discipline.is_cursor_paged:
        raise ValueError(
            f"{operation_id!r} is {discipline.value}, not cursor-paged: do not "
            "follow a next_page_token on it"
        )
    if page_size <= 0:
        raise ValueError("page_size must be a positive integer")

    if discipline is Pagination.DATE_WINDOWED_CURSOR:
        if date_from is None or date_to is None:
            raise ValueError(
                f"{operation_id!r} is date-windowed: both date_from and date_to "
                "are required and must stay fixed across pages"
            )
        return CursorRequest(
            page_size=page_size,
            next_page_token=next_page_token,
            date_from=date_from,
            date_to=date_to,
        )

    if date_from is not None or date_to is not None:
        raise ValueError(f"{operation_id!r} is a plain cursor endpoint and takes no date " "window")
    return CursorRequest(page_size=page_size, next_page_token=next_page_token)


def to_next_cursor(next_page_token: Optional[str]) -> Optional[str]:
    """Fold a Zoom page's ``next_page_token`` into the shared opaque cursor.

    The control plane's :class:`kiro_crew.connections.control_plane.OperationResult`
    carries pagination as a single OPAQUE ``next_cursor`` (a continuation token,
    or ``None`` when the result is complete) rather than any vendor's raw
    locator shape. Zoom's cursor locator IS its ``next_page_token`` string, so
    the fold is: a present, non-empty token becomes the opaque ``next_cursor``;
    an absent or empty token (Zoom's terminal-page signal) becomes ``None``.
    The date window a ``DATE_WINDOWED_CURSOR`` endpoint must keep fixed is NOT
    part of this opaque token -- it is carried on the vendor :class:`CursorRequest`
    and re-supplied per page by the adapter, per this connector's own spec.
    """
    if next_page_token is None or next_page_token == "":
        return None
    return next_page_token
