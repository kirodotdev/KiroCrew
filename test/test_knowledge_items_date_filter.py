"""Tests for the date order and added-day range on GET /api/knowledge/items.

`sort=newest|oldest` orders by the day an item was added (`created_at`), and
`added_from` / `added_to` keep only items added inside that inclusive range, in
both the list branch and the hybrid-search branch.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers.knowledge import list_items
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "items.db"))
    # Added on four days: two inside the OCTOBER range below, one on each side.
    for title, added in (
        ("september", "2026-09-30T23:59:59"),
        ("early october", "2026-10-01T00:00:00"),
        ("late october", "2026-10-31T23:00:00"),
        ("november", "2026-11-01T00:00:01"),
    ):
        item_id = s.add_item(title, f"report {title}", "note")
        s.db.execute("UPDATE items SET created_at = ? WHERE id = ?", (added, item_id))
    s.db.commit()
    yield s
    s.close()


async def _call(store, query: dict, retriever=None):
    req = MagicMock()
    req.query = query
    req.app = {}
    with (
        patch("kiro_crew.dashboard.handlers.knowledge._store", return_value=store),
        patch(
            "kiro_crew.dashboard.handlers.knowledge.HybridRetriever",
            return_value=retriever,
        ),
    ):
        resp = await list_items(req)
    return resp.status, json.loads(resp.body.decode())


OCTOBER = {"added_from": "2026-10-01", "added_to": "2026-10-31"}


@pytest.mark.parametrize(
    ("sort", "titles"),
    [("oldest", ["early october", "late october"]), ("newest", ["late october", "early october"])],
)
@pytest.mark.asyncio
async def test_list_keeps_the_added_range_in_date_order(store, sort, titles):
    status, data = await _call(store, {**OCTOBER, "sort": sort, "limit": "100"})
    assert status == 200
    assert data["total"] == 2
    assert [i["title"] for i in data["items"]] == titles


@pytest.mark.asyncio
async def test_search_narrows_to_the_added_range_oldest_first(store):
    # Relevance puts the newest first; the date order must override it, and the
    # range must drop the hits added outside October.
    ranked = store.db.execute("SELECT id FROM items ORDER BY created_at DESC").fetchall()
    retriever = MagicMock()
    retriever.search.return_value = [
        {"id": r["id"], "score": 1.0, "match_type": "keyword"} for r in ranked
    ]
    status, data = await _call(
        store, {"q": "report", "status": "active", "sort": "oldest", **OCTOBER}, retriever
    )
    assert status == 200
    assert data["total"] == 2
    assert [i["title"] for i in data["items"]] == ["early october", "late october"]


@pytest.mark.parametrize(
    "query",
    [{"sort": "random"}, {"added_from": "20261001"}, {"added_to": "2026-10-32"}],
    ids=["unknown-sort", "compact-date", "impossible-date"],
)
@pytest.mark.asyncio
async def test_a_malformed_sort_or_day_is_refused(store, query):
    status, data = await _call(store, query)
    assert status == 400
    assert data["error"] == "invalid sort/added_from/added_to"
    assert data["code"] == "invalid_query_params"


@pytest.mark.asyncio
async def test_a_date_sorted_search_sorts_one_window_on_every_page(tmp_path):
    # 70 hits at limit 20: relevance paging widens the window for page 4, but a
    # date order must sort the same window on every page, or pages would repeat
    # and skip entries.
    s = KnowledgeStore(str(tmp_path / "many.db"))
    ids = [s.add_item(f"hit {n}", "report", "note") for n in range(70)]
    retriever = MagicMock()
    retriever.search.side_effect = lambda _q, limit, status: [
        {"id": i, "score": 1.0, "match_type": "keyword"} for i in ids[:limit]
    ]
    query = {"q": "report", "status": "active", "sort": "oldest", "limit": "20"}
    pages = [(await _call(s, {**query, "page": str(p)}, retriever))[1] for p in (1, 2, 3, 4)]
    s.close()
    assert {p["total"] for p in pages} == {60}
    shown = [i["id"] for p in pages for i in p["items"]]
    assert len(shown) == len(set(shown)) == 60
