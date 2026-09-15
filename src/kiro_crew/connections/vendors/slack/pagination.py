"""Per-method Slack Web API pagination — declared method by method.

There is NO single "Slack cursor pagination" to assume. Slack's own guide
(https://api.slack.com/apis/pagination) says the individual method reference is
the source of truth, and the methods split into three incompatible schemes plus
methods that do not paginate at all:

1. **Cursor** (``cursor`` + ``limit`` in; ``response_metadata.next_cursor`` out;
   an empty / null / absent ``next_cursor`` means done). Used by
   ``conversations.history``, ``conversations.replies``, ``conversations.list``,
   ``conversations.members``, ``files.info``, ``reactions.list``, ``users.list``.
   The only pagination-specific error is ``invalid_cursor``.
2. **Legacy page/count** (``page`` + ``count`` in; a ``paging`` object out with
   ``{count, total, page, pages}``; done when ``page >= pages``). Used by
   ``files.list``, ``search.messages``, ``search.files``, ``search.all``.
   There is NO cursor on these.
3. **No pagination** at all — e.g. ``usergroups.list`` returns the whole set in
   one call and accepts no page/cursor parameter.

Two facts this module encodes because getting them wrong is a real bug:

- **``conversations.history`` and ``conversations.replies`` are two SEPARATE
  paginators.** They both use cursor pagination, but a cursor from one is
  meaningless to the other: history walks a channel's top-level messages,
  replies walks ONE thread. Sharing cursor state between them would resume a
  thread walk from a channel-level cursor (or vice versa) and silently skip or
  repeat messages. :func:`paginator_for` returns an INDEPENDENT
  :class:`CursorPaginator` instance per call, and the two methods are distinct
  keys, so nothing structurally lets their state be shared.

- **``files.list`` is legacy page/count, not cursor.** The ``files.list``
  reference offers only ``count`` (default 100) and ``page`` (default 1) and
  returns a ``paging`` object with no ``next_cursor``; the cursor-paginated file
  method is ``files.info`` (it paginates a single file's comments), not
  ``files.list``. So this module treats ``files.list`` as legacy page/count and
  takes ``paging.pages`` as the terminal signal — the method reference, which
  the guide names as the source of truth, exposes no cursor to take.

This module is pure logic: it computes the next request's parameters from a
response body and says whether more remains. It performs no IO and holds no
credentials; a caller drives the actual HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class PaginationScheme(str, Enum):
    """Which of Slack's pagination schemes a method uses (or none)."""

    CURSOR = "cursor"
    PAGE_COUNT = "page_count"
    NONE = "none"


# --- Method -> scheme, copied from the official references -------------------
# Only the methods this slice needs to reason about. Each key is a literal Slack
# method name; the reference page is the source of truth for its scheme.
_METHOD_SCHEME: dict[str, PaginationScheme] = {
    # Cursor family.
    "conversations.history": PaginationScheme.CURSOR,
    "conversations.replies": PaginationScheme.CURSOR,
    "conversations.list": PaginationScheme.CURSOR,
    "conversations.members": PaginationScheme.CURSOR,
    "files.info": PaginationScheme.CURSOR,
    "reactions.list": PaginationScheme.CURSOR,
    "users.list": PaginationScheme.CURSOR,
    # Legacy page/count family.
    "files.list": PaginationScheme.PAGE_COUNT,
    "search.messages": PaginationScheme.PAGE_COUNT,
    "search.files": PaginationScheme.PAGE_COUNT,
    "search.all": PaginationScheme.PAGE_COUNT,
    # No pagination — returns the whole set in one call.
    "usergroups.list": PaginationScheme.NONE,
}

#: Slack's documented per-call maximum for a cursor ``limit`` (varies per method
#: and is subject to change; the guide states 1000 as the ceiling and recommends
#: 100-200). We clamp to this so a caller cannot request an out-of-range page.
CURSOR_LIMIT_MAX = 1000
#: The guide's recommended default page size.
CURSOR_LIMIT_DEFAULT = 200
#: The only pagination-specific Slack error (returned for a gibberish / stale /
#: wrongly-encoded cursor).
INVALID_CURSOR_ERROR = "invalid_cursor"


def scheme_for(method: str) -> PaginationScheme:
    """The pagination scheme for a Slack method name.

    An unknown method raises ``KeyError`` rather than defaulting to cursor: the
    whole point of this module is that a scheme must be *declared* from the
    reference, never assumed.
    """
    return _METHOD_SCHEME[method]


@dataclass
class CursorPaginator:
    """Drives one cursor-paginated method's walk. Stateful and single-walk.

    A fresh instance is one traversal of one collection. It is NOT shared across
    methods: ``conversations.history`` and ``conversations.replies`` each get
    their own, because a cursor from one is meaningless to the other.
    """

    method: str
    limit: int = CURSOR_LIMIT_DEFAULT
    #: The cursor to send on the NEXT call. ``None`` before the first call and
    #: after the walk is complete.
    next_cursor: Optional[str] = None
    #: True once a terminal response (empty/absent next_cursor) has been seen.
    _done: bool = field(default=False, init=False)
    #: True once at least one page has been consumed.
    _started: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= CURSOR_LIMIT_MAX:
            raise ValueError(f"limit must be in 1..{CURSOR_LIMIT_MAX}, got {self.limit}")

    @property
    def done(self) -> bool:
        """Whether the walk is complete (a terminal page was consumed)."""
        return self._done

    def next_params(self) -> dict[str, Any]:
        """Parameters for the next request (``limit`` always, ``cursor`` when set).

        Raises ``StopIteration`` if called after the walk is done — a completed
        paginator has no next request, and returning empty params would loop the
        first page forever.
        """
        if self._done:
            raise StopIteration(f"{self.method} pagination is complete")
        params: dict[str, Any] = {"limit": self.limit}
        if self.next_cursor:
            params["cursor"] = self.next_cursor
        return params

    def consume(self, response: dict[str, Any]) -> bool:
        """Record a response body; return whether MORE results remain.

        Reads ``response_metadata.next_cursor``. An empty string, ``None``, or an
        absent ``response_metadata`` all mean the walk is complete — the guide is
        explicit that fewer-than-``limit`` results is NOT the end signal, only the
        cursor is. Returns ``True`` when another :meth:`next_params` /
        :meth:`consume` round should follow, ``False`` when done.
        """
        self._started = True
        meta = response.get("response_metadata")
        cursor = ""
        if isinstance(meta, dict):
            cursor = meta.get("next_cursor") or ""
        if cursor:
            self.next_cursor = cursor
            return True
        self.next_cursor = None
        self._done = True
        return False


# Legacy methods place ``paging`` at different documented response levels.
_PAGE_COUNT_PAGING_CONTAINER: dict[str, Optional[str]] = {
    "files.list": None,
    "search.messages": "messages",
    "search.files": "files",
}
_SEARCH_ALL_CONTAINERS = ("messages", "files")


@dataclass
class PageCountPaginator:
    """Drives one legacy ``page``/``count`` method's walk (files.list, search.*).

    Terminal when the consumed page's ``paging.page >= paging.pages``. For
    ``search.all``, both independently paginated result containers must reach
    their terminal pages.
    """

    method: str
    count: int = 100
    #: The page to request NEXT (1-based, as Slack numbers pages).
    page: int = 1
    _pages: Optional[int] = field(default=None, init=False)
    _done: bool = field(default=False, init=False)
    _container_done: dict[str, bool] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError(f"count must be >= 1, got {self.count}")
        if self.page < 1:
            raise ValueError(f"page must be >= 1, got {self.page}")
        if self.method == "search.all":
            self._container_done = {key: False for key in _SEARCH_ALL_CONTAINERS}

    @property
    def done(self) -> bool:
        return self._done

    @property
    def total_pages(self) -> Optional[int]:
        """Largest page count observed after a response is consumed."""
        return self._pages

    def next_params(self) -> dict[str, Any]:
        """Parameters for the next request: ``page`` and ``count``."""
        if self._done:
            raise StopIteration(f"{self.method} pagination is complete")
        return {"page": self.page, "count": self.count}

    def consume(self, response: dict[str, Any]) -> bool:
        """Record a response body; return whether MORE pages remain.

        Reads the method's documented ``paging`` object
        (``{count, total, page, pages}``). Done when the current page number
        reaches the reported page count. A missing or malformed ``paging``
        object at that level fails closed to done, so a caller cannot loop
        forever on a response that does not describe its own paging.
        """
        if self.method == "search.all":
            return self._consume_search_all(response)

        container_key = _PAGE_COUNT_PAGING_CONTAINER[self.method]
        container = response if container_key is None else response.get(container_key)
        paging = container.get("paging") if isinstance(container, dict) else None
        if not isinstance(paging, dict):
            self._done = True
            return False
        pages = paging.get("pages")
        cur = paging.get("page", self.page)
        if not isinstance(pages, int) or not isinstance(cur, int):
            self._done = True
            return False
        self._pages = pages
        if cur >= pages:
            self._done = True
            return False
        self.page = cur + 1
        return True

    def _consume_search_all(self, response: dict[str, Any]) -> bool:
        """Consume the independently paginated messages and files containers."""
        next_pages: list[int] = []
        for container_key in _SEARCH_ALL_CONTAINERS:
            if self._container_done[container_key]:
                continue
            container = response.get(container_key)
            paging = container.get("paging") if isinstance(container, dict) else None
            if not isinstance(paging, dict):
                self._container_done[container_key] = True
                continue
            pages = paging.get("pages")
            cur = paging.get("page", self.page)
            if not isinstance(pages, int) or not isinstance(cur, int):
                self._container_done[container_key] = True
                continue
            self._pages = max(self._pages or 0, pages)
            if cur >= pages:
                self._container_done[container_key] = True
            else:
                next_pages.append(cur + 1)

        if all(self._container_done.values()):
            self._done = True
            return False
        self.page = max(next_pages)
        return True


def paginator_for(
    method: str, *, limit: Optional[int] = None, count: Optional[int] = None
) -> CursorPaginator | PageCountPaginator:
    """Return the correct, INDEPENDENT paginator for a Slack method.

    - A cursor method gets a fresh :class:`CursorPaginator` (so history and
      replies never share state).
    - A legacy method gets a :class:`PageCountPaginator`.
    - A no-pagination method (``usergroups.list``) raises ``ValueError``: asking
      for a paginator for a method that returns everything at once is a caller
      bug, not something to paper over with a one-page paginator.
    """
    scheme = scheme_for(method)
    if scheme is PaginationScheme.CURSOR:
        return CursorPaginator(
            method=method, limit=limit if limit is not None else CURSOR_LIMIT_DEFAULT
        )
    if scheme is PaginationScheme.PAGE_COUNT:
        return PageCountPaginator(method=method, count=count if count is not None else 100)
    raise ValueError(
        f"{method} does not paginate (scheme={scheme.value}); it returns the "
        f"whole result set in one call — do not request a paginator for it"
    )
