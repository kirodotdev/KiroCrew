"""Microsoft Graph request/response payload shaping (pure logic).

WHAT THIS OWNS
==============
Two directions, both deterministic and network-free:

* REQUEST shaping -- turn a locator path plus typed query intent into the
  ``(path, query_params)`` a Graph call is made from. This module assembles
  OData query options (``$select``, ``$filter``, ``$top``,
  ``$expand``, ``$orderby``, ``$search``, ``$count``) into the exact query
  strings Graph expects, and applies the ``calendarView`` rule where
  ``startDateTime``/``endDateTime`` are MANDATORY (see below).
* RESPONSE shaping -- read a Graph JSON response envelope into a typed
  :class:`GraphPage`: its ``value`` collection, and the paging-control
  annotations (``@odata.nextLink`` / ``@odata.deltaLink``) surfaced so the
  paging layer can drive them. The cursor SEMANTICS live in ``paging.py``; this
  module only extracts the raw annotations from the envelope.

WHAT THIS DELIBERATELY DOES NOT OWN
===================================
No vendor-error taxonomy, no typed error envelope. A non-2xx Graph response and
its ``error`` object are mapped to W01's typed shared boundary by a SEPARATE
leaf; this module shapes only the SUCCESS envelope and raises a plain
:class:`GraphPayloadError` for a structurally malformed success body (e.g. a
list response whose ``value`` is not an array). Do not grow an error hierarchy
here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence

# Graph's OData annotation keys. Named once so a typo cannot silently read a
# key that never matches (which would look like "no next page").
ODATA_NEXT_LINK = "@odata.nextLink"
ODATA_DELTA_LINK = "@odata.deltaLink"
ODATA_COUNT = "@odata.count"


class GraphPayloadError(ValueError):
    """A Graph success envelope was structurally malformed, or a request was.

    A shaping fault only. Vendor errors (the ``error`` object of a non-2xx
    response) are W01's typed boundary, mapped by a separate leaf -- never
    classified here.
    """


@dataclass(frozen=True)
class QuerySpec:
    """Typed OData query intent for a request.

    Every field is optional; only the ones set are serialized. ``top`` maps to
    ``$top`` (server-driven page size hint). ``filter_`` maps to ``$filter``.
    ``select``/``expand``/``orderby`` are comma-joined per OData. ``search``
    maps to ``$search`` and ``count`` to ``$count=true``.
    """

    select: Sequence[str] = ()
    filter_: Optional[str] = None
    top: Optional[int] = None
    expand: Sequence[str] = ()
    orderby: Sequence[str] = ()
    search: Optional[str] = None
    count: bool = False

    def to_query_params(self) -> Dict[str, str]:
        """Serialize to Graph's ``$``-prefixed query parameter dict.

        Deterministic ordering (OData option name) so a request shape hashes
        stably for evidence receipts. Does NOT url-encode -- the client layer
        owns encoding; this returns the logical param map.
        """

        params: Dict[str, str] = {}
        if self.select:
            params["$select"] = ",".join(self.select)
        if self.filter_ is not None:
            params["$filter"] = self.filter_
        if self.top is not None:
            if self.top <= 0:
                raise GraphPayloadError(f"$top must be a positive integer, got {self.top}")
            params["$top"] = str(self.top)
        if self.expand:
            params["$expand"] = ",".join(self.expand)
        if self.orderby:
            params["$orderby"] = ",".join(self.orderby)
        if self.search is not None:
            params["$search"] = self.search
        if self.count:
            params["$count"] = "true"
        return dict(sorted(params.items()))


@dataclass(frozen=True)
class CalendarViewWindow:
    """The MANDATORY start/end window for a ``calendarView`` request.

    Graph's ``calendarView`` expands recurring events across a range, so
    ``startDateTime`` and ``endDateTime`` are REQUIRED query parameters, not
    optional -- omitting them is a 400 from Graph, so it is refused here at
    shaping time. Both are ISO 8601 strings passed through verbatim (the caller
    owns timezone formatting); this type only guarantees both are present and
    non-empty and that start precedes end lexically is NOT checked (Graph owns
    range validation; an equal or reversed window is a vendor 400, not a shaping
    fault this pure layer can adjudicate without a date parser it deliberately
    omits).
    """

    start: str
    end: str

    def __post_init__(self) -> None:
        if not self.start.strip():
            raise GraphPayloadError("calendarView requires a non-empty startDateTime")
        if not self.end.strip():
            raise GraphPayloadError("calendarView requires a non-empty endDateTime")

    def to_query_params(self) -> Dict[str, str]:
        return {"startDateTime": self.start, "endDateTime": self.end}


@dataclass(frozen=True)
class GraphRequest:
    """A fully-shaped Graph request, ready for the client layer to dispatch.

    ``path`` is the root-relative path from ``locator.build_path``. ``params``
    is the merged query map, exposed as a read-only mapping. The mapping is
    copied and frozen so a shaped request can be recorded (its
    ``request_shape_hash`` for an evidence receipt) without a later mutation
    invalidating the record.
    """

    method: str
    path: str
    params: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))


def shape_request(
    *,
    method: str,
    path: str,
    query: Optional[QuerySpec] = None,
    calendar_view: Optional[CalendarViewWindow] = None,
) -> GraphRequest:
    """Assemble a :class:`GraphRequest` from typed parts.

    Merges ``query`` (OData options) and ``calendar_view`` (the mandatory
    window) into one param map. A path whose final segment is ``calendarView``
    requires that window; other paths may omit it.
    """

    last_segment = path.rstrip("/").rsplit("/", 1)[-1]
    if last_segment.lower() == "calendarview" and calendar_view is None:
        raise GraphPayloadError("calendarView request requires a start/end window")

    params: Dict[str, str] = {}
    if query is not None:
        params.update(query.to_query_params())
    if calendar_view is not None:
        params.update(calendar_view.to_query_params())
    return GraphRequest(method=method.upper(), path=path, params=params)


@dataclass(frozen=True)
class GraphPage:
    """One page of a Graph collection response.

    ``value`` is the page's items. ``next_link`` / ``delta_link`` are the raw
    ``@odata.nextLink`` / ``@odata.deltaLink`` annotations (``None`` when
    absent). ``count`` is ``@odata.count`` when the request asked for it. The
    paging layer reads these; this type just carries them.

    A page carries AT MOST ONE of ``next_link`` / ``delta_link``: Graph emits a
    ``nextLink`` while more pages remain and a ``deltaLink`` on the FINAL page
    of a delta-tracked collection. Both present at once is a malformed envelope.
    """

    value: List[Any]
    next_link: Optional[str] = None
    delta_link: Optional[str] = None
    count: Optional[int] = None


def _optional_link(envelope: Mapping[str, Any], key: str) -> Optional[str]:
    """Return an opaque paging link, rejecting a malformed present annotation."""

    if key not in envelope:
        return None
    value = envelope[key]
    if not isinstance(value, str) or not value.strip():
        raise GraphPayloadError(f"{key} must be a non-empty string when present")
    return value


def parse_collection(envelope: Mapping[str, Any]) -> GraphPage:
    """Read a Graph collection success envelope into a :class:`GraphPage`.

    Raises :class:`GraphPayloadError` for a structurally malformed success body
    -- a missing/non-array ``value``, a malformed present paging-link annotation,
    or a page carrying BOTH a ``nextLink`` and a ``deltaLink`` (contradictory
    paging state). It does NOT interpret a vendor
    ``error`` object: a non-2xx response never reaches here (the client layer
    routes it to W01's typed boundary first).
    """

    if "value" not in envelope:
        raise GraphPayloadError("collection envelope is missing 'value'")
    value = envelope["value"]
    if not isinstance(value, list):
        raise GraphPayloadError(f"'value' must be an array, got {type(value).__name__}")

    next_link = _optional_link(envelope, ODATA_NEXT_LINK)
    delta_link = _optional_link(envelope, ODATA_DELTA_LINK)
    if next_link is not None and delta_link is not None:
        raise GraphPayloadError(
            "envelope carries both @odata.nextLink and @odata.deltaLink "
            "(contradictory paging state)"
        )
    count_raw = envelope.get(ODATA_COUNT)
    count = int(count_raw) if isinstance(count_raw, int) else None

    return GraphPage(
        value=list(value),
        next_link=next_link,
        delta_link=delta_link,
        count=count,
    )
