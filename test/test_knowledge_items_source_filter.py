"""Tests for the `source_id` filter on GET /api/knowledge/items.

The Knowledge Library list view pages *within* a source group, so `list_items`
must be able to scope a page to one source and report that source's total (not
the global one). The `__none__` sentinel scopes to items with no source.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers.knowledge import (
    _load_items_by_id,
    _matches_source,
    list_items,
    source_counts,
)
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "items.db"))
    yield s
    s.close()


@pytest.fixture()
def seeded(store):
    """Two sources plus a sourceless item.

    alpha has 3 items, beta has 2, and one item has no source at all.
    """
    alpha = store.add_source("alpha", "local_folder", "/tmp/alpha")
    beta = store.add_source("beta", "local_folder", "/tmp/beta")
    for i in range(3):
        store.add_item(f"alpha-{i}", f"content alpha {i}", "note", source_id=alpha)
    for i in range(2):
        store.add_item(f"beta-{i}", f"content beta {i}", "note", source_id=beta)
    store.add_item("orphan", "content orphan", "note")
    return store, alpha, beta


def _request(store, query: dict) -> MagicMock:
    req = MagicMock()
    req.query = query
    req.app = {}
    return req


async def _call(store, query: dict) -> dict:
    req = _request(store, query)
    with patch(
        "kiro_crew.dashboard.handlers.knowledge._store", return_value=store
    ):
        resp = await list_items(req)
    return json.loads(resp.body.decode())


# ---------------------------------------------------------------------------
# _matches_source
# ---------------------------------------------------------------------------


def test_matches_source_exact():
    assert _matches_source({"source_id": "s1"}, "s1") is True
    assert _matches_source({"source_id": "s1"}, "s2") is False


def test_matches_source_none_sentinel_matches_missing_and_empty():
    assert _matches_source({}, "__none__") is True
    assert _matches_source({"source_id": None}, "__none__") is True
    assert _matches_source({"source_id": ""}, "__none__") is True
    assert _matches_source({"source_id": "s1"}, "__none__") is False


# ---------------------------------------------------------------------------
# list_items?source_id=
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_items_without_source_id_returns_everything(seeded):
    store, _alpha, _beta = seeded
    data = await _call(store, {})
    assert data["total"] == 6
    assert len(data["items"]) == 6


@pytest.mark.asyncio
async def test_list_items_scopes_to_one_source(seeded):
    store, alpha, _beta = seeded
    data = await _call(store, {"source_id": alpha})
    assert data["total"] == 3
    assert {i["source_id"] for i in data["items"]} == {alpha}


@pytest.mark.asyncio
async def test_list_items_total_is_scoped_not_global(seeded):
    """The pager math depends on `total` reflecting the scoped source only."""
    store, _alpha, beta = seeded
    data = await _call(store, {"source_id": beta})
    assert data["total"] == 2


@pytest.mark.asyncio
async def test_list_items_none_sentinel_returns_sourceless_items(seeded):
    store, _alpha, _beta = seeded
    data = await _call(store, {"source_id": "__none__"})
    assert data["total"] == 1
    assert data["items"][0]["title"] == "orphan"
    assert not data["items"][0].get("source_id")


@pytest.mark.asyncio
async def test_list_items_unknown_source_id_returns_empty(seeded):
    store, _alpha, _beta = seeded
    data = await _call(store, {"source_id": "does-not-exist"})
    assert data["total"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_list_items_source_id_paginates_within_source(seeded):
    """Paging is scoped: page 2 of a 3-item source at limit 2 yields 1 item."""
    store, alpha, _beta = seeded
    page1 = await _call(store, {"source_id": alpha, "page": "1", "limit": "2"})
    page2 = await _call(store, {"source_id": alpha, "page": "2", "limit": "2"})
    assert page1["total"] == page2["total"] == 3
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 1
    ids = {i["id"] for i in page1["items"]} | {i["id"] for i in page2["items"]}
    assert len(ids) == 3


@pytest.mark.asyncio
async def test_list_items_source_id_composes_with_other_filters(seeded):
    """source_id narrows alongside type/status/namespace rather than replacing."""
    store, alpha, _beta = seeded
    store.add_item("alpha-doc", "a doc", "document", source_id=alpha)
    data = await _call(store, {"source_id": alpha, "type": "document"})
    assert data["total"] == 1
    assert data["items"][0]["title"] == "alpha-doc"


@pytest.mark.asyncio
async def test_list_items_source_id_filters_search_results(seeded):
    """The hybrid-search branch honours source_id too."""
    store, alpha, beta = seeded

    results = [{"id": i["id"], "score": 1.0, "match_type": "fts"}
               for i in store.db.execute("SELECT id FROM items").fetchall()]

    retriever = MagicMock()
    retriever.search.return_value = results
    req = _request(store, {"q": "content", "source_id": beta})
    with (
        patch("kiro_crew.dashboard.handlers.knowledge._store", return_value=store),
        patch(
            "kiro_crew.dashboard.handlers.knowledge.HybridRetriever",
            return_value=retriever,
        ),
    ):
        resp = await list_items(req)
    data = json.loads(resp.body.decode())
    assert data["total"] == 2
    assert {i["source_id"] for i in data["items"]} == {beta}


# ---------------------------------------------------------------------------
# source_counts
# ---------------------------------------------------------------------------


async def _counts(store, query: dict) -> dict:
    req = _request(store, query)
    with patch(
        "kiro_crew.dashboard.handlers.knowledge._store", return_value=store
    ):
        resp = await source_counts(req)
    return json.loads(resp.body.decode())


@pytest.mark.asyncio
async def test_source_counts_reports_every_source(seeded):
    """Every source appears, so none can hide behind pagination."""
    store, alpha, beta = seeded
    data = await _counts(store, {})
    assert data["counts"] == {alpha: 3, beta: 2, "__none__": 1}
    assert data["total"] == 6


@pytest.mark.asyncio
async def test_source_counts_respects_type_filter(seeded):
    store, alpha, _beta = seeded
    store.add_item("alpha-doc", "a doc", "document", source_id=alpha)
    data = await _counts(store, {"type": "document"})
    assert data["counts"] == {alpha: 1}
    assert data["total"] == 1


@pytest.mark.asyncio
async def test_source_counts_respects_status_filter(seeded):
    store, alpha, beta = seeded
    data = await _counts(store, {"status": "active"})
    assert data["counts"] == {alpha: 3, beta: 2, "__none__": 1}
    archived = await _counts(store, {"status": "archived"})
    assert archived["counts"] == {}
    assert archived["total"] == 0


@pytest.mark.asyncio
async def test_source_counts_respects_namespace_filter(seeded):
    store, alpha, _beta = seeded
    store.add_item("ns-item", "scoped", "note", source_id=alpha, namespace="team")
    data = await _counts(store, {"namespace": "team"})
    assert data["counts"] == {alpha: 1}


@pytest.mark.asyncio
async def test_source_counts_matches_scoped_list_items_total(seeded):
    """The badge count and the group's own pager total must agree."""
    store, alpha, _beta = seeded
    counts = await _counts(store, {})
    scoped = await _call(store, {"source_id": alpha})
    assert counts["counts"][alpha] == scoped["total"]


@pytest.mark.asyncio
async def test_scoped_search_escalates_until_retrieval_is_exhausted(seeded):
    """A source scope is applied after ranking, so the candidate pool grows
    until the retriever short-reads. Otherwise matches hidden behind
    higher-ranked hits from other sources would be reported as absent."""
    store, _alpha, beta = seeded
    retriever = MagicMock()
    # Full windows until the pool exceeds 400, then a short read.

    def _search(_q, limit):
        return [{"id": "x", "score": 1.0, "match_type": "fts"}] * (
            limit if limit <= 400 else limit - 1
        )
    retriever.search.side_effect = _search
    req = _request(store, {"q": "content", "source_id": beta, "limit": "20"})
    with (
        patch("kiro_crew.dashboard.handlers.knowledge._store", return_value=store),
        patch(
            "kiro_crew.dashboard.handlers.knowledge.HybridRetriever",
            return_value=retriever,
        ),
    ):
        await list_items(req)
    limits = [c.kwargs["limit"] for c in retriever.search.call_args_list]
    assert limits == [200, 400, 800]


@pytest.mark.asyncio
async def test_scoped_search_stops_at_the_escalation_cap(seeded):
    """A corpus that never short-reads must not escalate without bound."""
    store, _alpha, beta = seeded
    retriever = MagicMock()
    retriever.search.side_effect = lambda _q, limit: [
        {"id": "x", "score": 1.0, "match_type": "fts"}
    ] * limit
    req = _request(store, {"q": "content", "source_id": beta, "limit": "20"})
    with (
        patch("kiro_crew.dashboard.handlers.knowledge._store", return_value=store),
        patch(
            "kiro_crew.dashboard.handlers.knowledge.HybridRetriever",
            return_value=retriever,
        ),
    ):
        await list_items(req)
    limits = [c.kwargs["limit"] for c in retriever.search.call_args_list]
    assert limits[-1] == 20000
    assert all(x <= 20000 for x in limits)


@pytest.mark.asyncio
async def test_unscoped_search_keeps_the_narrow_pool(seeded):
    """Without a source scope the existing limit * 3 window is unchanged."""
    store, _alpha, _beta = seeded
    retriever = MagicMock()
    retriever.search.return_value = []
    req = _request(store, {"q": "content", "limit": "20"})
    with (
        patch("kiro_crew.dashboard.handlers.knowledge._store", return_value=store),
        patch(
            "kiro_crew.dashboard.handlers.knowledge.HybridRetriever",
            return_value=retriever,
        ),
    ):
        await list_items(req)
    assert retriever.search.call_args.kwargs["limit"] == 60
    assert retriever.search.call_count == 1


@pytest.mark.asyncio
async def test_search_candidate_loading_runs_off_the_event_loop(seeded):
    """A scoped search escalates its candidate pool, so the batch SELECT and
    row serialization must not run inline on the event loop."""
    store, _alpha, beta = seeded
    retriever = MagicMock()
    retriever.search.return_value = [
        {"id": r["id"], "score": 1.0, "match_type": "fts"}
        for r in store.db.execute("SELECT id FROM items").fetchall()
    ]
    req = _request(store, {"q": "content", "source_id": beta})
    with (
        patch("kiro_crew.dashboard.handlers.knowledge._store", return_value=store),
        patch(
            "kiro_crew.dashboard.handlers.knowledge.HybridRetriever",
            return_value=retriever,
        ),
        patch(
            "kiro_crew.dashboard.handlers.knowledge.asyncio.to_thread",
            wraps=asyncio.to_thread,
        ) as to_thread,
    ):
        resp = await list_items(req)
    data = json.loads(resp.body.decode())
    # Still correct, and the load went through to_thread.
    assert data["total"] == 2
    assert any(
        c.args and getattr(c.args[0], "__name__", "") == "_load_items_by_id"
        for c in to_thread.call_args_list
    )


def test_load_items_by_id_chunks_below_the_sqlite_variable_limit(seeded):
    """A scoped search can escalate to thousands of candidates. Binding them all
    into one `IN (...)` exceeds SQLITE_MAX_VARIABLE_NUMBER (999 on older
    builds), so the load is chunked."""
    store, alpha, _beta = seeded
    real_ids = [r["id"] for r in store.db.execute("SELECT id FROM items").fetchall()]
    # Pad with ids that match nothing so the list crosses several chunks.
    item_ids = real_ids + [f"missing-{i}" for i in range(2500)]

    seen_sizes = []
    real_execute = store.db.execute

    def _spy(sql, params=()):
        if "FROM items WHERE id IN" in sql:
            seen_sizes.append(len(params))
        return real_execute(sql, params)

    with patch.object(type(store), "db", property(lambda _self: MagicMock(execute=_spy))):
        loaded = _load_items_by_id(store, item_ids)

    assert seen_sizes, "expected at least one chunked SELECT"
    assert max(seen_sizes) <= 999
    assert len(seen_sizes) > 1
    # Every real item is still returned exactly once despite the chunking.
    assert set(loaded) == set(real_ids)
    assert loaded[real_ids[0]]["id"] == real_ids[0]
    assert any(i["source_id"] == alpha for i in loaded.values())


def test_load_items_by_id_handles_empty_input(seeded):
    store, _alpha, _beta = seeded
    assert _load_items_by_id(store, []) == {}
