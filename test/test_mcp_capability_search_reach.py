"""MCP discovery search over the CapabilityManager seam.

The regression these pin: ``CapabilityProvider`` consumes at most
``_LIST_LIMIT_GUARD`` registry rows, so on a registry bigger than that guard the
client-side filter only ever sees the head of the list and every row past it is
unfindable. Measured on an internal registry of 5612 servers before the fix: a
search for a bundle sorted past the cap returned only substring noise.

The fix passes the query DOWN to the manager as a HINT. These tests cover both
sides of that contract -- a manager that filters server-side becomes fully
searchable, and a manager that ignores the hint (including one still on the
older zero-arg signature) keeps working with its old reach.
"""

from __future__ import annotations

import pytest

from kiro_crew.mcp_providers.capability import _LIST_LIMIT_GUARD, CapabilityProvider
from kiro_crew.mcp_utils import registry_accepts_query
from kiro_crew.platform.capability_bound import BoundedCapabilityManager


def _rows(count: int, *, prefix: str = "srv") -> list[dict[str, object]]:
    """A registry big enough to overflow the row guard, ids sorted a-z."""
    return [
        {
            "id": f"{prefix}-{i:05d}-mcp",
            "title": f"Server {i}",
            "description": "d",
            "installed": False,
        }
        for i in range(count)
    ]


class QueryAwareManager:
    """A manager that filters server-side, like a large real registry must."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.queries: list[str | None] = []

    def available(self) -> bool:
        return True

    async def registry(self, query: str | None = None) -> list[dict[str, object]]:
        self.queries.append(query)
        if not query:
            return self._rows
        needle = query.lower()
        return [r for r in self._rows if needle in str(r["id"]).lower()]


class LegacyManager:
    """A manager still on the original zero-arg signature."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.calls = 0

    def available(self) -> bool:
        return True

    async def registry(self) -> list[dict[str, object]]:
        self.calls += 1
        return self._rows


@pytest.mark.asyncio
async def test_search_finds_a_row_past_the_row_guard():
    """The whole point: a server sorted past the guard is still findable."""
    rows = _rows(_LIST_LIMIT_GUARD * 4)
    target = str(rows[-1]["id"])
    mgr = QueryAwareManager(rows)

    results = await CapabilityProvider(lambda: mgr).search(target)

    assert [r.id for r in results] == [target]
    assert mgr.queries == [target.lower()]


@pytest.mark.asyncio
async def test_unfiltered_browse_sends_no_query():
    """fetch_detail hints with the id; a plain listing must not invent a query."""
    rows = _rows(3)
    mgr = QueryAwareManager(rows)
    provider = CapabilityProvider(lambda: mgr)

    detail = await provider.fetch_detail(str(rows[2]["id"]))

    assert detail is not None and detail.id == rows[2]["id"]
    assert mgr.queries == [str(rows[2]["id"])]


@pytest.mark.asyncio
async def test_detail_reaches_a_row_past_the_row_guard():
    rows = _rows(_LIST_LIMIT_GUARD * 3)
    target = str(rows[-1]["id"])

    detail = await CapabilityProvider(lambda: QueryAwareManager(rows)).fetch_detail(target)

    assert detail is not None
    assert detail.id == target


@pytest.mark.asyncio
async def test_legacy_zero_arg_manager_still_searches():
    """No TypeError, and the head of the list stays searchable as before."""
    rows = _rows(_LIST_LIMIT_GUARD * 2)
    mgr = LegacyManager(rows)

    results = await CapabilityProvider(lambda: mgr).search(str(rows[0]["id"]))

    assert [r.id for r in results] == [rows[0]["id"]]
    assert mgr.calls == 1


@pytest.mark.asyncio
async def test_client_side_filter_survives_a_manager_that_ignores_the_hint():
    """The hint is advisory: a manager returning everything must not leak rows."""

    class IgnoringManager(QueryAwareManager):
        async def registry(self, query: str | None = None):
            self.queries.append(query)
            return self._rows

    mgr = IgnoringManager(_rows(10))
    results = await CapabilityProvider(lambda: mgr).search("srv-00003")

    assert [r.id for r in results] == ["srv-00003-mcp"]


@pytest.mark.asyncio
async def test_bounded_wrapper_forwards_the_hint():
    """Every caller reaches the manager through the bind, so it must pass it on."""
    rows = _rows(_LIST_LIMIT_GUARD * 2)
    target = str(rows[-1]["id"])
    inner = QueryAwareManager(rows)
    bound = BoundedCapabilityManager(inner)

    assert registry_accepts_query(bound.registry)
    results = await CapabilityProvider(lambda: bound).search(target)

    assert [r.id for r in results] == [target]
    assert inner.queries == [target.lower()]


@pytest.mark.asyncio
async def test_bounded_wrapper_does_not_break_a_legacy_manager():
    inner = LegacyManager(_rows(5))
    bound = BoundedCapabilityManager(inner)

    assert await bound.registry(query="srv") == inner._rows
    assert await bound.registry() == inner._rows
    assert inner.calls == 2


def test_registry_accepts_query_shapes():
    async def zero_arg():
        return []

    async def keyword(query=None):
        return []

    async def kwargs_only(**kw):
        return []

    async def positional_only(query=None, /):
        return []

    assert not registry_accepts_query(zero_arg)
    assert registry_accepts_query(keyword)
    assert registry_accepts_query(kwargs_only)
    # Positional-only cannot take ``query=`` as a keyword, so it is not accepting.
    assert not registry_accepts_query(positional_only)
    # Nothing to introspect degrades to "does not accept" rather than raising.
    assert not registry_accepts_query(None)
