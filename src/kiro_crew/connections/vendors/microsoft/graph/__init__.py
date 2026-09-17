"""Shared Microsoft Graph runtime base (W05): the pure-logic foundation every
Graph-backed connector stream reuses instead of re-deriving.

This first slice ships three network-free concerns:

* :mod:`~kiro_crew.connections.vendors.microsoft.graph.locator` -- resource locator
  (``/me`` vs ``/users/{id}``, the app-only ``/me`` refusal, the required Graph
  resource shapes).
* :mod:`~kiro_crew.connections.vendors.microsoft.graph.payload` -- request/response
  payload shaping (OData query options, the mandatory ``calendarView`` window,
  the collection-envelope reader).
* :mod:`~kiro_crew.connections.vendors.microsoft.graph.paging` -- the ``@odata.nextLink``
  cursor protocol, modelling each ACTUAL evidence variant as a closed set and
  refusing to page an operation whose mode has no engine.

It defines NO vendor-error taxonomy: the RUN-family closed error set and its
typed envelope are W01's single source, and the Graph-vendor-error ->
W01-typed-envelope mapping is a separate leaf that consumes W01's boundary.
"""

from kiro_crew.connections.vendors.microsoft.graph.locator import (
    GraphLocator,
    GraphLocatorError,
    Principal,
    ResourceRef,
    build_path,
    is_app_only,
)
from kiro_crew.connections.vendors.microsoft.graph.paging import (
    RESYNC_APPLY,
    RESYNC_UPLOAD,
    PageStep,
    PagingError,
    PagingMode,
    ResyncSignal,
    assert_pageable,
    check_first_page_preconditions,
    next_step,
    parse_resync,
)
from kiro_crew.connections.vendors.microsoft.graph.payload import (
    ODATA_COUNT,
    ODATA_DELTA_LINK,
    ODATA_NEXT_LINK,
    CalendarViewWindow,
    GraphPage,
    GraphPayloadError,
    GraphRequest,
    QuerySpec,
    parse_collection,
    shape_request,
)

__all__ = [
    # locator
    "GraphLocator",
    "GraphLocatorError",
    "Principal",
    "ResourceRef",
    "build_path",
    "is_app_only",
    # payload
    "CalendarViewWindow",
    "GraphPage",
    "GraphPayloadError",
    "GraphRequest",
    "ODATA_COUNT",
    "ODATA_DELTA_LINK",
    "ODATA_NEXT_LINK",
    "QuerySpec",
    "parse_collection",
    "shape_request",
    # paging
    "PageStep",
    "PagingError",
    "PagingMode",
    "RESYNC_APPLY",
    "RESYNC_UPLOAD",
    "ResyncSignal",
    "assert_pageable",
    "check_first_page_preconditions",
    "next_step",
    "parse_resync",
]
