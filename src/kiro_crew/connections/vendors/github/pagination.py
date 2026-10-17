"""GitHub-specific pagination wire parsing.

GitHub exposes two DIFFERENT pagination contracts across its API surface, and
the github-mcp-server tool catalog carries both:

* **REST page/perPage** -- numeric ``page`` + ``perPage`` query params, with a
  ``Link`` response header whose ``rel="next"`` URL carries the next page.
  ``perPage`` is capped at 100; a caller asking for more is silently clamped
  server-side, so a client that wants deterministic behaviour clamps first.
* **A cursor ``after``** -- the GraphQL-backed tools (``list_issues``,
  ``pull_request_read`` method ``get_review_comments``, ``list_dependabot_alerts``)
  advance with an opaque ``after`` cursor, NOT a page number.

The campaign evidence records an explicit contradiction: a generic connector
must not assume one pagination contract across all operations, and the ``after``
cursor MUST be expressed as a parameter distinct from the native REST
``page``/``perPage`` -- collapsing them into one "page token" field loses the
information that they advance different ways and are read by different tools.

This module OWNS only the GitHub reading of these two contracts: parsing a
``Link`` header, clamping ``perPage``, and keeping the cursor param name
separate from the REST param names. It builds no request, performs no I/O, and
decides no retry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional

# GitHub's documented hard ceiling for ``perPage`` on paginated REST list
# endpoints. A request above this is clamped by the server; clamp locally so
# the client's own record of what it asked for matches what it will get.
MAX_PER_PAGE = 100

# The native REST pagination param names. The cursor param below is
# deliberately NOT one of these -- keeping the sets disjoint is the whole point
# of the contradiction the evidence recorded.
REST_PAGE_PARAM = "page"
REST_PER_PAGE_PARAM = "perPage"

# The cursor param name for GraphQL-backed list tools. A separate name space
# from the REST params: a tool takes one contract or the other, never a mix.
CURSOR_PARAM = "after"

# ``Link: <url>; rel="next", <url>; rel="last"`` -- one comma-separated entry
# per relation. Capture the URL inside the angle brackets and its rel token.
_LINK_ENTRY_RE = re.compile(r"<([^>]*)>\s*;\s*(.*)")
_REL_RE = re.compile(r'rel\s*=\s*"([^"]*)"')


def clamp_per_page(requested: int) -> int:
    """Clamp a requested ``perPage`` into GitHub's accepted range [1, 100].

    A value above 100 is capped at 100 (GitHub's server-side ceiling); a value
    below 1 is raised to 1 (a page of zero rows is not a meaningful request).
    Pure and total: every int maps to a value in [1, 100].
    """
    if requested > MAX_PER_PAGE:
        return MAX_PER_PAGE
    if requested < 1:
        return 1
    return requested


def parse_link_header(link_header: Optional[str]) -> Dict[str, str]:
    """Parse an RFC-5988 ``Link`` header into a ``{rel: url}`` mapping.

    Tolerant of the shapes GitHub actually emits and of malformed input:

    * ``None`` or an empty/whitespace header -> ``{}`` (the last page has no
      ``Link`` header at all, which is not an error).
    * An entry missing its angle-bracketed URL, or missing a ``rel=``, is
      skipped rather than raising -- a single malformed entry must not lose the
      well-formed entries beside it, and a caller reading ``.get("next")`` on
      the result already handles "no next page".

    Returns the LAST value seen for a repeated rel (GitHub does not repeat a
    rel, so this only matters for malformed input, where last-wins is a stable,
    documented choice rather than an accident).
    """
    result: Dict[str, str] = {}
    if not link_header or not link_header.strip():
        return result

    # Split on commas that separate entries. A comma inside a URL is not valid
    # in a GitHub pagination URL (they are percent-encoded), so a plain split
    # is correct for this provider's headers.
    for raw_entry in link_header.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        match = _LINK_ENTRY_RE.match(entry)
        if not match:
            continue
        url, params = match.group(1).strip(), match.group(2)
        rel_match = _REL_RE.search(params)
        if not rel_match:
            continue
        result[rel_match.group(1).strip()] = url

    return result


def next_page_url(link_header: Optional[str]) -> Optional[str]:
    """Return the ``rel="next"`` URL from a ``Link`` header, or ``None``.

    ``None`` means there is no next page -- the terminal, expected state of a
    page walk, not an error.
    """
    return parse_link_header(link_header).get("next")


@dataclass(frozen=True)
class RestPageRequest:
    """The native REST page/perPage request params for one page.

    ``per_page`` is stored already clamped, so this object cannot represent an
    out-of-range request that the server would silently rewrite.
    """

    page: int
    per_page: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "per_page", clamp_per_page(self.per_page))
        if self.page < 1:
            object.__setattr__(self, "page", 1)

    def as_query_params(self) -> Dict[str, int]:
        """Render to query params under GitHub's REST param names."""
        return {REST_PAGE_PARAM: self.page, REST_PER_PAGE_PARAM: self.per_page}


@dataclass(frozen=True)
class CursorPageRequest:
    """A cursor-paginated request for one page of a GraphQL-backed tool.

    The cursor lives under :data:`CURSOR_PARAM` (``after``), never under the
    REST param names -- a validator (and a human reader) can tell the two
    contracts apart by which param carries the position. ``per_page`` is still
    clamped: the cursor tools honour a page size too.
    """

    after: Optional[str]
    per_page: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "per_page", clamp_per_page(self.per_page))

    def as_query_params(self) -> Dict[str, object]:
        """Render to query params. Omits the cursor on the first page.

        The result never contains :data:`REST_PAGE_PARAM`: a cursor request is
        not a page-number request, and emitting ``page`` here would be the
        exact contract-collapse the evidence warned against.
        """
        params: Dict[str, object] = {REST_PER_PAGE_PARAM: self.per_page}
        if self.after is not None:
            params[CURSOR_PARAM] = self.after
        return params
