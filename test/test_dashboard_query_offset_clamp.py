"""Every dashboard route that binds a query ``offset`` into SQLite clamps it.

``sqlite3`` raises ``OverflowError`` on a bound int past the signed 64-bit
range, so a route that only parsed ``int(request.query["offset"])`` and handed
it to the store answered ``?offset=9223372036854775808`` with a 500 where the
caller asked for a page that is simply empty. ``GET /api/lessons`` already
folds the offset into ``[0, LESSON_LIST_OFFSET_MAX]``; this pins the same
clamp, through the one shared helper, on each sibling route whose store binds
the offset as a parameter -- driven against a REAL ``VectorMemoryStore`` so the
bind is exercised and a mock cannot hide the overflow.

The routes that slice a Python list instead (the session list, the two cron
history routes, the memory-records list) are not here: slicing treats an
out-of-range index as the empty tail, so they never needed the clamp.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from member_memory_helpers import declare_v2_store

from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.dashboard.handlers import memory as mem_mod
from kiro_crew.dashboard.handlers import memory_admin, memory_edit
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.validation import LESSON_LIST_OFFSET_MAX, clamp_query_offset
from kiro_crew.vector_memory import VectorMemoryStore, open_member_database

# The first value the signed 64-bit range cannot hold, and one far past it:
# the first is the exact boundary ``sqlite3`` refuses, the second shows the
# clamp is a ceiling and not a wrap.
_HUGE = (str(2**63), str(2**80))


def _request(query: dict[str, str]) -> MagicMock:
    request = MagicMock()
    request.app = {"state": MagicMock()}
    request.query = dict(query)
    request.headers = {"X-Session-Key": "dashboard:ui"}
    request.match_info = {}
    return request


def _body(resp) -> dict:
    return json.loads(resp.text or "")


@pytest.fixture
def v1(tmp_path: Path):
    """A v1-lineage store with one semantic row, so a history read has a record."""
    store = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    store.init()
    store.set_semantic("user.email", "owner@example.com", 1.0, "user_explicit")
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def v2(tmp_path: Path):
    """A v2-lineage member store: the carve route refuses v1 before it pages."""
    directory = declare_v2_store(tmp_path, "member-alice")
    store = open_member_database(
        directory / "memory.db", member_id="alice", store_id="member-alice", embedding_dim=2
    )
    try:
        yield store
    finally:
        store.close()


@pytest.mark.parametrize("huge", [2**63, 2**80])
def test_the_facet_page_query_binds_the_shared_ceiling_for_route_and_cli(huge) -> None:
    """The carve route and the CLI verb both reach this one query, so the bound lives here."""
    from kiro_crew import memory_schema

    _sql, params = memory_schema.facet_page_query({}, "", memory_schema.DEFAULT_FACET_PAGE, huge)
    assert params[-1] == LESSON_LIST_OFFSET_MAX


def test_the_helper_folds_into_the_shared_window() -> None:
    assert clamp_query_offset(-4) == 0
    assert clamp_query_offset(0) == 0
    assert clamp_query_offset(7) == 7
    assert clamp_query_offset(LESSON_LIST_OFFSET_MAX) == LESSON_LIST_OFFSET_MAX
    assert clamp_query_offset(2**63) == LESSON_LIST_OFFSET_MAX
    assert clamp_query_offset(2**80) == LESSON_LIST_OFFSET_MAX


@pytest.mark.asyncio
@pytest.mark.parametrize("huge", _HUGE)
@pytest.mark.parametrize(
    ("handler", "key"),
    [
        (mem_mod.api_memory_semantic, "entries"),
        (mem_mod.api_memory_events, "events"),
        (mem_mod.api_memory_episodic_list, "entries"),
    ],
)
async def test_memory_list_routes_answer_a_huge_offset_with_an_empty_page(
    v1, handler, key, huge
) -> None:
    with patch.object(
        mem_mod, "_vector_tier_for_request", new=AsyncMock(return_value=(v1, "default", None))
    ):
        resp = await handler(_request({"offset": huge}))
    assert resp.status == 200, huge
    assert _body(resp)[key] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("huge", _HUGE)
async def test_the_carve_route_answers_a_huge_offset_with_an_empty_page(v2, huge) -> None:
    with patch.object(
        mem_mod,
        "_vector_tier_for_request",
        new=AsyncMock(return_value=(v2, "member-alice", None)),
    ):
        resp = await mem_mod.api_memory_carve(_request({"offset": huge}))
    assert resp.status == 200, huge
    assert _body(resp) == {"store": "member-alice", "entries": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("huge", _HUGE)
async def test_the_retired_route_answers_a_huge_offset_with_an_empty_page(v1, huge) -> None:
    with (
        patch.object(
            memory_admin, "require_owner_dashboard_request", new=AsyncMock(return_value=None)
        ),
        patch.object(memory_admin, "_admin_store", new=AsyncMock(return_value=("default", None))),
        patch.object(memory_admin, "vector_memory_for_store", new=AsyncMock(return_value=v1)),
    ):
        resp = await memory_admin.api_memory_retired(_request({"offset": huge}))
    assert resp.status == 200, huge
    assert _body(resp) == {"retired": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("huge", _HUGE)
async def test_the_record_history_route_answers_a_huge_offset_with_an_empty_page(v1, huge) -> None:
    with (
        patch.object(
            memory_edit, "require_owner_dashboard_request", new=AsyncMock(return_value=None)
        ),
        patch.object(memory_edit, "_admin_store", new=AsyncMock(return_value=("default", None))),
        patch.object(memory_edit, "vector_memory_for_store", new=AsyncMock(return_value=v1)),
    ):
        resp = await memory_edit.api_memory_record_history(
            _request({"offset": huge, "kind": "fact", "id": "user.email"})
        )
    assert resp.status == 200, huge
    body = _body(resp)
    assert body["entries"] == []
    assert body["has_more"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("huge", _HUGE)
async def test_the_knowledge_items_route_answers_a_huge_page_with_an_empty_page(
    tmp_path: Path, huge
) -> None:
    """``page`` is the query here, and ``(page - 1) * limit`` is what the store binds."""
    store = KnowledgeStore(str(tmp_path / "kb.db"))
    try:
        request = _request({"page": huge})
        request.app["state"].knowledge_store = store
        resp = await kh.list_items(request)
    finally:
        store._close_all_for_tests()
    assert resp.status == 200, huge
    body = _body(resp)
    assert body["items"] == []
    assert body["total"] == 0


@pytest.mark.asyncio
async def test_the_record_history_route_keeps_refusing_a_negative_offset(v1) -> None:
    """The ceiling folds; the library's own non-negative floor still answers 400."""
    with (
        patch.object(
            memory_edit, "require_owner_dashboard_request", new=AsyncMock(return_value=None)
        ),
        patch.object(memory_edit, "_admin_store", new=AsyncMock(return_value=("default", None))),
        patch.object(memory_edit, "vector_memory_for_store", new=AsyncMock(return_value=v1)),
    ):
        resp = await memory_edit.api_memory_record_history(
            _request({"offset": "-5", "kind": "fact", "id": "user.email"})
        )
    assert resp.status == 400
    assert _body(resp)["code"] == "invalid_memory_edit"


@pytest.mark.asyncio
async def test_a_non_integer_offset_is_still_refused_not_clamped(v1) -> None:
    """The clamp folds a value; it never invents one for a string that is not a number."""
    with patch.object(
        mem_mod, "_vector_tier_for_request", new=AsyncMock(return_value=(v1, "default", None))
    ):
        resp = await mem_mod.api_memory_events(_request({"offset": "1.5"}))
    assert resp.status == 400
