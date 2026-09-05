"""Coverage tests for the Knowledge Library dashboard handlers.

Targets the read-side item/entity/graph endpoints, export/import, the embedding
endpoints and their background rebuild job, the chat-context search endpoint,
the agent-document route, agent ingest/sync, and the route-registration entry
point -- all of which were unexercised by the existing knowledge test modules.

Harness matches ``test_knowledge_add_source.py`` / ``test_folder_watch_handlers.py``:
a real :class:`KnowledgeStore` on a ``tmp_path`` sqlite file plus a minimal
``web.Application`` carrying only the app keys each handler reads.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.knowledge.store import KnowledgeStore

MODULE = "kiro_crew.dashboard.handlers.knowledge"


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "kb.db"))
    yield s
    s.close()


class _FakeEmbedder:
    """Minimal stand-in for InProcessEmbedder (real model never loaded)."""

    def __init__(self, *, available=True, vec=(0.1, 0.2, 0.3, 0.4), model="fake-embed:1"):
        self.model = model
        self.content_budget = 2000
        self._available = available
        self._vec = list(vec)
        self.embed_calls: list[str] = []

    async def is_available_async(self) -> bool:
        return self._available

    def embed_for_item(self, title, summary, content):
        self.embed_calls.append(title or "")
        return list(self._vec) if self._vec else None

    def embed(self, text):
        return list(self._vec) if self._vec else None


def _make_app(store, *, pipeline=None, embedder=None, watcher=None, pool=None):
    """Minimal app + every route these tests exercise."""
    app = web.Application()
    state = MagicMock()
    state.knowledge_store = store
    app["state"] = state
    if pipeline is not None:
        app["knowledge_pipeline"] = pipeline
    if embedder is not None:
        app["knowledge_embedder"] = embedder
    if watcher is not None:
        app["knowledge_watcher"] = watcher
    app["knowledge_fetch_pool"] = pool if pool is not None else MagicMock(shutdown=AsyncMock())
    app["knowledge_sync"] = MagicMock(get_connector=MagicMock(return_value=None))

    r = app.router
    r.add_get("/api/knowledge/namespaces", kh.list_namespaces)
    r.add_get("/api/knowledge/stats", kh.get_stats)
    r.add_get("/api/knowledge/entities", kh.list_entities)
    r.add_get("/api/knowledge/graph", kh.get_full_graph)
    r.add_get("/api/knowledge/export", kh.export_all)
    r.add_post("/api/knowledge/import", kh.import_bundle)
    r.add_post("/api/knowledge/agent-document", kh.add_agent_document_route)
    r.add_get("/api/knowledge/embedding/status", kh.get_embedding_status)
    r.add_post("/api/knowledge/embedding/generate", kh.batch_embed_items)
    r.add_get("/api/knowledge/search-for-context", kh.search_for_context)
    r.add_get("/api/knowledge/items/{id}", kh.get_item)
    r.add_patch("/api/knowledge/items/{id}", kh.update_item)
    r.add_delete("/api/knowledge/items/{id}", kh.delete_item)
    r.add_get("/api/knowledge/items/{id}/content", kh.get_item_content)
    r.add_get("/api/knowledge/items/{id}/related", kh.get_related_items)
    r.add_get("/api/knowledge/items/{id}/export", kh.export_item)
    r.add_get("/api/knowledge/entities/by-name/{name}/items", kh.get_entity_items)
    r.add_get("/api/knowledge/entities/{id}/graph", kh.get_entity_graph)
    r.add_get("/api/knowledge/jobs/{id}", kh.get_job)
    r.add_post("/api/knowledge/sources/{id}/ingest-text", kh.ingest_text)
    r.add_post("/api/knowledge/sources/{id}/sync", kh.sync_source)
    r.add_delete("/api/knowledge/sources/{id}", kh.delete_source)
    return app


def _client(app):
    return TestClient(TestServer(app))


def _add_job(store, job_id="job-1", *, status="processing", source_id=None):
    store.db.execute(
        "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
        "VALUES (?, ?, ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
        (job_id, source_id, status),
    )
    store.db.commit()
    return job_id


# ---------------------------------------------------------------- namespaces


class TestListNamespaces:
    @pytest.mark.asyncio
    async def test_empty_store_returns_empty_list(self, store):
        async with _client(_make_app(store)) as client:
            resp = await client.get("/api/knowledge/namespaces")
            assert resp.status == 200
            assert await resp.json() == []

    @pytest.mark.asyncio
    async def test_counts_per_namespace_descending(self, store):
        store.add_item("a", "body a", "note", namespace="work")
        store.add_item("b", "body b", "note", namespace="work")
        store.add_item("c", "body c", "note", namespace="home")
        async with _client(_make_app(store)) as client:
            data = await (await client.get("/api/knowledge/namespaces")).json()
        assert [d["name"] for d in data] == ["work", "home"]
        assert [d["count"] for d in data] == [2, 1]

    @pytest.mark.asyncio
    async def test_blank_namespace_reported_as_default(self, store):
        item_id = store.add_item("a", "body", "note")
        store.db.execute("UPDATE items SET namespace = '' WHERE id = ?", (item_id,))
        store.db.commit()
        async with _client(_make_app(store)) as client:
            data = await (await client.get("/api/knowledge/namespaces")).json()
        assert data == [{"name": "default", "count": 1}]


# --------------------------------------------------------------------- items


class TestGetItem:
    @pytest.mark.asyncio
    async def test_missing_item_is_404(self, store):
        async with _client(_make_app(store)) as client:
            resp = await client.get("/api/knowledge/items/nope")
            assert resp.status == 404
            assert (await resp.json())["error"] == "not found"

    @pytest.mark.asyncio
    async def test_returns_entities_relations_and_locations(self, store):
        sid = store.add_source("src", "local_file", "/tmp/x.md")
        item_id = store.add_item("Design", "body", "note", source_id=sid)
        e1 = store.add_entity("Alice", "person")
        e2 = store.add_entity("Bravo", "project")
        store.add_mention(item_id, e1, context="ctx")
        store.add_mention(item_id, e2)
        store.add_entity_relation(e1, e2, "works_on")
        store.add_source_location(item_id, sid, section_title="Intro")

        async with _client(_make_app(store)) as client:
            data = await (await client.get(f"/api/knowledge/items/{item_id}")).json()

        assert data["title"] == "Design"
        assert {e["name"] for e in data["entities"]} == {"Alice", "Bravo"}
        # Both endpoints of the relation resolve to display names, and the
        # relation is de-duplicated even though both of its entities are
        # mentioned by this item.
        assert len(data["relations"]) == 1
        assert data["relations"][0]["source_name"] == "Alice"
        assert data["relations"][0]["target_name"] == "Bravo"
        assert data["source_locations"][0]["section_title"] == "Intro"

    @pytest.mark.asyncio
    async def test_dangling_relation_falls_back_to_ids(self, store):
        item_id = store.add_item("Design", "body", "note")
        e1 = store.add_entity("Alice", "person")
        store.add_mention(item_id, e1)
        # A relation pointing at an entity row that does not exist (only
        # reachable with FK enforcement off, e.g. a row imported by an older
        # build): the handler must fall back to the raw id rather than raise.
        store.db.execute("PRAGMA foreign_keys = OFF")
        try:
            store.db.execute(
                "INSERT INTO entity_relations "
                "(id, source_id, target_id, relation_type, weight, created_at) "
                "VALUES ('rel-x', ?, 'ghost', 'mentions', 1, '2026-01-01T00:00:00')",
                (e1,),
            )
            store.db.commit()
        finally:
            store.db.execute("PRAGMA foreign_keys = ON")
        async with _client(_make_app(store)) as client:
            data = await (await client.get(f"/api/knowledge/items/{item_id}")).json()
        assert data["relations"][0]["target_name"] == "ghost"
        assert data["relations"][0]["source_name"] == "Alice"

    @pytest.mark.asyncio
    async def test_item_without_mentions_has_empty_graph_fields(self, store):
        item_id = store.add_item("Solo", "body", "note")
        async with _client(_make_app(store)) as client:
            data = await (await client.get(f"/api/knowledge/items/{item_id}")).json()
        assert data["entities"] == []
        assert data["relations"] == []
        assert data["source_locations"] == []


class TestUpdateItem:
    @pytest.mark.asyncio
    async def test_missing_item_is_404(self, store):
        async with _client(_make_app(store)) as client:
            resp = await client.patch("/api/knowledge/items/nope", json={"title": "x"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, store):
        item_id = store.add_item("a", "body", "note")
        async with _client(_make_app(store)) as client:
            resp = await client.patch(
                f"/api/knowledge/items/{item_id}",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid JSON"

    @pytest.mark.asyncio
    async def test_no_allowed_field_is_400(self, store):
        item_id = store.add_item("a", "body", "note")
        async with _client(_make_app(store)) as client:
            resp = await client.patch(
                f"/api/knowledge/items/{item_id}", json={"content": "hijack", "id": "other"}
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "no valid fields"
        # The disallowed keys were not written through.
        assert store.get_item(item_id)["content"] == "body"

    @pytest.mark.asyncio
    async def test_updates_allowed_fields(self, store):
        item_id = store.add_item("a", "body", "note")
        async with _client(_make_app(store)) as client:
            resp = await client.patch(
                f"/api/knowledge/items/{item_id}",
                json={"title": "renamed", "tags": ["x"], "namespace": "work"},
            )
            assert resp.status == 200
            assert (await resp.json())["ok"] is True
        row = store.get_item(item_id)
        assert row["title"] == "renamed"
        assert row["namespace"] == "work"


class TestDeleteItem:
    @pytest.mark.asyncio
    async def test_missing_item_is_404(self, store):
        async with _client(_make_app(store)) as client:
            assert (await client.delete("/api/knowledge/items/nope")).status == 404

    @pytest.mark.asyncio
    async def test_deletes_item(self, store):
        item_id = store.add_item("a", "body", "note")
        async with _client(_make_app(store)) as client:
            resp = await client.delete(f"/api/knowledge/items/{item_id}")
            assert resp.status == 200
            assert (await resp.json())["ok"] is True
        assert store.get_item(item_id) is None


class TestGetItemContent:
    @pytest.mark.asyncio
    async def test_missing_item_is_404_plain_text(self, store):
        async with _client(_make_app(store)) as client:
            resp = await client.get("/api/knowledge/items/nope/content")
            assert resp.status == 404
            assert await resp.text() == "not found"

    @pytest.mark.asyncio
    async def test_returns_plain_text_content(self, store):
        item_id = store.add_item("a", "hello body", "note")
        async with _client(_make_app(store)) as client:
            resp = await client.get(f"/api/knowledge/items/{item_id}/content")
            assert resp.status == 200
            assert resp.content_type == "text/plain"
            assert await resp.text() == "hello body"


# ------------------------------------------------------------------ entities


class TestListEntities:
    @pytest.mark.asyncio
    async def test_lists_all_ordered_by_name(self, store):
        store.add_entity("Zeta", "person")
        store.add_entity("Alpha", "project")
        async with _client(_make_app(store)) as client:
            data = await (await client.get("/api/knowledge/entities")).json()
        assert [e["name"] for e in data] == ["Alpha", "Zeta"]

    @pytest.mark.asyncio
    async def test_filters_by_type(self, store):
        store.add_entity("Zeta", "person")
        store.add_entity("Alpha", "project")
        async with _client(_make_app(store)) as client:
            data = await (
                await client.get("/api/knowledge/entities", params={"type": "project"})
            ).json()
        assert [e["name"] for e in data] == ["Alpha"]

    @pytest.mark.asyncio
    async def test_filters_by_name_substring(self, store):
        store.add_entity("Zeta", "person")
        store.add_entity("Alpha", "project")
        async with _client(_make_app(store)) as client:
            data = await (await client.get("/api/knowledge/entities", params={"q": "lph"})).json()
        assert [e["name"] for e in data] == ["Alpha"]

    @pytest.mark.asyncio
    async def test_combined_type_and_q_filters(self, store):
        store.add_entity("Alpha", "project")
        store.add_entity("Alphabet", "person")
        async with _client(_make_app(store)) as client:
            data = await (
                await client.get("/api/knowledge/entities", params={"q": "Alpha", "type": "person"})
            ).json()
        assert [e["name"] for e in data] == ["Alphabet"]

    @pytest.mark.asyncio
    async def test_non_numeric_limit_is_400(self, store):
        async with _client(_make_app(store)) as client:
            resp = await client.get("/api/knowledge/entities", params={"limit": "abc"})
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid limit"

    @pytest.mark.asyncio
    async def test_limit_is_clamped_to_at_least_one(self, store):
        store.add_entity("Alpha", "project")
        store.add_entity("Zeta", "person")
        async with _client(_make_app(store)) as client:
            data = await (await client.get("/api/knowledge/entities", params={"limit": "0"})).json()
        # 0 (and any smaller value) clamps up to 1 rather than returning nothing.
        assert len(data) == 1


class TestGetEntityGraph:
    @pytest.mark.asyncio
    async def test_non_numeric_depth_is_400(self, store):
        async with _client(_make_app(store)) as client:
            resp = await client.get("/api/knowledge/entities/x/graph", params={"depth": "deep"})
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid depth"

    @pytest.mark.asyncio
    async def test_unknown_entity_is_404(self, store):
        async with _client(_make_app(store)) as client:
            resp = await client.get("/api/knowledge/entities/ghost/graph")
            assert resp.status == 404
            assert (await resp.json())["error"] == "entity not found"

    @pytest.mark.asyncio
    async def test_returns_subgraph_for_known_entity(self, store):
        e1 = store.add_entity("Alice", "person")
        e2 = store.add_entity("Bravo", "project")
        store.add_entity_relation(e1, e2, "works_on")
        async with _client(_make_app(store)) as client:
            data = await (
                await client.get(f"/api/knowledge/entities/{e1}/graph", params={"depth": "1"})
            ).json()
        assert {n["id"] for n in data["nodes"]} == {e1, e2}


class TestGetEntityItems:
    @pytest.mark.asyncio
    async def test_matches_items_mentioning_the_entity_name(self, store):
        store.add_item("Roadmap", "Alice owns the plan", "note")
        store.add_item("Unrelated", "nothing here", "note")
        async with _client(_make_app(store)) as client:
            data = await (await client.get("/api/knowledge/entities/by-name/Alice/items")).json()
        assert [i["title"] for i in data] == ["Roadmap"]

    @pytest.mark.asyncio
    async def test_double_quote_in_name_is_escaped_not_a_syntax_error(self, store):
        store.add_item("Quoted", 'the "widget" ships', "note")
        async with _client(_make_app(store)) as client:
            resp = await client.get('/api/knowledge/entities/by-name/wid"get/items')
            # The FTS5 MATCH string doubles the quote, so the query parses and
            # simply finds nothing instead of raising OperationalError.
            assert resp.status == 200
            assert await resp.json() == []


class TestGetRelatedItems:
    @pytest.mark.asyncio
    async def test_item_with_no_mentions_returns_empty(self, store):
        item_id = store.add_item("Solo", "body", "note")
        async with _client(_make_app(store)) as client:
            resp = await client.get(f"/api/knowledge/items/{item_id}/related")
            assert resp.status == 200
            assert await resp.json() == []

    @pytest.mark.asyncio
    async def test_non_numeric_limit_is_400(self, store):
        async with _client(_make_app(store)) as client:
            resp = await client.get("/api/knowledge/items/x/related", params={"limit": "many"})
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid limit"

    @pytest.mark.asyncio
    async def test_ranks_by_shared_entity_count_and_excludes_self(self, store):
        e1 = store.add_entity("Alice", "person")
        e2 = store.add_entity("Bravo", "project")
        base = store.add_item("Base", "b", "note")
        two = store.add_item("Two shared", "b", "note")
        one = store.add_item("One shared", "b", "note")
        for eid in (e1, e2):
            store.add_mention(base, eid)
            store.add_mention(two, eid)
        store.add_mention(one, e1)

        async with _client(_make_app(store)) as client:
            data = await (await client.get(f"/api/knowledge/items/{base}/related")).json()

        assert [i["id"] for i in data] == [two, one]
        assert data[0]["shared_entities"] == 2
        assert data[1]["shared_entities"] == 1


class TestGetFullGraph:
    @pytest.mark.asyncio
    async def test_empty_graph_returns_empty_nodes_and_edges(self, store):
        async with _client(_make_app(store)) as client:
            data = await (await client.get("/api/knowledge/graph")).json()
        assert data == {"nodes": [], "edges": []}

    @pytest.mark.asyncio
    async def test_non_numeric_limit_is_400(self, store):
        async with _client(_make_app(store)) as client:
            resp = await client.get("/api/knowledge/graph", params={"limit": "all"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_returns_nodes_and_edges(self, store):
        e1 = store.add_entity("Alice", "person")
        e2 = store.add_entity("Bravo", "project")
        store.add_entity_relation(e1, e2, "works_on", weight=3)
        async with _client(_make_app(store)) as client:
            data = await (await client.get("/api/knowledge/graph")).json()
        assert {n["name"] for n in data["nodes"]} == {"Alice", "Bravo"}
        assert data["edges"][0]["type"] == "works_on"

    @pytest.mark.asyncio
    async def test_limit_drops_edges_whose_endpoint_is_outside_the_window(self, store):
        # Two connected entities plus one isolated: a limit of 1 keeps only the
        # highest-degree node, so the edge must be dropped rather than dangle.
        e1 = store.add_entity("Alice", "person")
        e2 = store.add_entity("Bravo", "project")
        store.add_entity_relation(e1, e2, "works_on")
        async with _client(_make_app(store)) as client:
            data = await (await client.get("/api/knowledge/graph", params={"limit": "1"})).json()
        assert len(data["nodes"]) == 1
        assert data["edges"] == []

    @pytest.mark.asyncio
    async def test_source_id_filter_restricts_to_mentioned_entities(self, store):
        # Create two sources with items and entities linked via mentions
        src_a = store.add_source("Source A", "local_folder", "/tmp/a")
        src_b = store.add_source("Source B", "local_folder", "/tmp/b")
        item_a = store.add_item("doc a", "content a", "note", source_id=src_a)
        item_b = store.add_item("doc b", "content b", "note", source_id=src_b)
        e1 = store.add_entity("Alice", "person")
        e2 = store.add_entity("Bravo", "service")
        store.add_entity_relation(e1, e2, "uses")
        store.add_mention(item_a, e1)
        store.add_mention(item_b, e2)
        async with _client(_make_app(store)) as client:
            # Unfiltered: both entities
            data = await (await client.get("/api/knowledge/graph")).json()
            assert {n["name"] for n in data["nodes"]} == {"Alice", "Bravo"}
            # Filter to source A: only Alice
            data_a = await (
                await client.get("/api/knowledge/graph", params={"source_id": src_a})
            ).json()
            assert {n["name"] for n in data_a["nodes"]} == {"Alice"}
            # Filter to source B: only Bravo
            data_b = await (
                await client.get("/api/knowledge/graph", params={"source_id": src_b})
            ).json()
            assert {n["name"] for n in data_b["nodes"]} == {"Bravo"}

    @pytest.mark.asyncio
    async def test_source_id_filter_with_no_mentions_returns_empty(self, store):
        src = store.add_source("Empty", "local_folder", "/tmp/empty")
        store.add_entity("Orphan", "concept")
        async with _client(_make_app(store)) as client:
            data = await (
                await client.get("/api/knowledge/graph", params={"source_id": src})
            ).json()
        assert data == {"nodes": [], "edges": []}

    @pytest.mark.asyncio
    async def test_source_id_filter_includes_source_locations(self, store):
        # An item owned by source A but also located in source B (dedup scenario)
        # should appear when filtering by source B.
        src_a = store.add_source("Owner", "local_folder", "/tmp/owner")
        src_b = store.add_source("Location", "local_folder", "/tmp/loc")
        item = store.add_item("shared doc", "content", "note", source_id=src_a)
        store.add_source_location(item, src_b)
        e1 = store.add_entity("Shared", "concept")
        store.add_mention(item, e1)
        async with _client(_make_app(store)) as client:
            # Filter by owner source: finds entity via items.source_id
            data_a = await (
                await client.get("/api/knowledge/graph", params={"source_id": src_a})
            ).json()
            assert {n["name"] for n in data_a["nodes"]} == {"Shared"}
            # Filter by location source: finds entity via source_locations
            data_b = await (
                await client.get("/api/knowledge/graph", params={"source_id": src_b})
            ).json()
            assert {n["name"] for n in data_b["nodes"]} == {"Shared"}

    @pytest.mark.asyncio
    async def test_source_id_filter_with_only_commas_returns_empty(self, store):
        # Edge case: source_id=","  should not crash (empty IN clause)
        store.add_entity("Node", "concept")
        async with _client(_make_app(store)) as client:
            data = await (
                await client.get("/api/knowledge/graph", params={"source_id": ","})
            ).json()
        assert data == {"nodes": [], "edges": []}


# --------------------------------------------------------------------- stats


class TestGetStats:
    @pytest.mark.asyncio
    async def test_reports_embeddings_disabled_without_embedder(self, store):
        store.add_item("a", "body", "note")
        async with _client(_make_app(store)) as client:
            data = await (await client.get("/api/knowledge/stats")).json()
        assert data["embeddings"] == {"enabled": False}

    @pytest.mark.asyncio
    async def test_reports_embedded_count_with_embedder(self, store):
        store.add_item("a", "body", "note", embedding=b"\x00\x01")
        store.add_item("b", "body", "note")
        emb = _FakeEmbedder()
        async with _client(_make_app(store, embedder=emb)) as client:
            data = await (await client.get("/api/knowledge/stats")).json()
        assert data["embeddings"]["enabled"] is True
        assert data["embeddings"]["available"] is True
        assert data["embeddings"]["model"] == "fake-embed:1"
        assert data["embeddings"]["embedded_items"] == 1

    @pytest.mark.asyncio
    async def test_unavailable_embedder_still_reports_enabled(self, store):
        emb = _FakeEmbedder(available=False)
        async with _client(_make_app(store, embedder=emb)) as client:
            data = await (await client.get("/api/knowledge/stats")).json()
        assert data["embeddings"]["enabled"] is True
        assert data["embeddings"]["available"] is False


# ---------------------------------------------------------------------- jobs


class TestGetJob:
    @pytest.mark.asyncio
    async def test_missing_job_is_404(self, store):
        async with _client(_make_app(store)) as client:
            assert (await client.get("/api/knowledge/jobs/nope")).status == 404

    @pytest.mark.asyncio
    async def test_returns_job_row(self, store):
        _add_job(store, "job-7", status="completed")
        async with _client(_make_app(store)) as client:
            data = await (await client.get("/api/knowledge/jobs/job-7")).json()
        assert data["id"] == "job-7"
        assert data["status"] == "completed"


# ------------------------------------------------------------- export/import


class TestExport:
    @pytest.mark.asyncio
    async def test_export_missing_item_is_404(self, store):
        async with _client(_make_app(store)) as client:
            assert (await client.get("/api/knowledge/items/x/export")).status == 404

    @pytest.mark.asyncio
    async def test_export_item_attaches_download_header(self, store):
        item_id = store.add_item("a", "body", "note")
        async with _client(_make_app(store)) as client:
            resp = await client.get(f"/api/knowledge/items/{item_id}/export")
            assert resp.status == 200
            assert "item.knowledge" in resp.headers["Content-Disposition"]
            assert (await resp.json())["items"][0]["id"] == item_id

    @pytest.mark.asyncio
    async def test_export_all_default_filename(self, store):
        store.add_item("a", "body", "note")
        async with _client(_make_app(store)) as client:
            resp = await client.get("/api/knowledge/export")
            assert resp.status == 200
            assert "filename=knowledge.knowledge" in resp.headers["Content-Disposition"]

    @pytest.mark.asyncio
    async def test_export_all_sanitizes_namespace_into_filename(self, store):
        store.add_item("a", "body", "note", namespace="work")
        async with _client(_make_app(store)) as client:
            resp = await client.get("/api/knowledge/export", params={"namespace": "work/../etc"})
            disp = resp.headers["Content-Disposition"]
        # Path separators and dots-with-slashes cannot survive into the
        # suggested filename, so the download cannot escape its directory.
        assert "/" not in disp.split("filename=")[1]
        assert "work" in disp


class TestImportBundle:
    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, store):
        async with _client(_make_app(store)) as client:
            resp = await client.post(
                "/api/knowledge/import", data="{oops", headers={"Content-Type": "application/json"}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_imports_items_entities_and_relations(self, store):
        bundle = {
            "items": [
                {
                    "id": "i1",
                    "title": "Imported",
                    "content": "hello",
                    "summary": "s",
                    "item_type": "note",
                }
            ],
            "entities": [
                {"id": "e1", "name": "Alice", "entity_type": "person", "description": "d"}
            ],
            "relations": [
                {
                    "id": "r1",
                    "source_id": "e1",
                    "target_id": "e1",
                    "relation_type": "self",
                    "description": "d",
                }
            ],
        }
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 200
            result = await resp.json()
        assert result["items_imported"] == 1
        assert result["entities_created"] == 1

    @pytest.mark.asyncio
    async def test_corrupt_json_column_is_a_clean_400(self, store, monkeypatch):
        # The store's writer-side invariant (issue #5559) rejects this value
        # AT THE STORE: a lone-surrogate escape passes json.loads (so it gets
        # through the handler's pre-redaction shape validator) but cannot be
        # UTF-8-encoded at SQLite bind time. The handler surfaces the store's
        # typed error as a 400, and the rejected row is not committed.
        sel_calls = []
        monkeypatch.setattr(kh, "_sel_log", lambda tool, **kw: sel_calls.append((tool, kw)))
        bundle = {
            "sources": [
                {
                    "id": "s1",
                    "name": "f",
                    "source_type": "local_file",
                    "uri": "/tmp/x.md",
                    "properties": '{"x": "\ud800"}',
                }
            ]
        }
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 400
            body = await resp.json()
        assert body["code"] == "malformed_knowledge_bundle"
        assert "sources.properties" in body["error"]
        assert store.db.execute("SELECT COUNT(*) AS c FROM sources").fetchone()["c"] == 0
        # The refusal of a cross-instance bundle is audited.
        assert (
            "import",
            {"outcome": "rejected", "reason": "'sources.properties' must be valid UTF-8 text"},
        ) in sel_calls

    @pytest.mark.asyncio
    async def test_missing_text_fields_coerce_to_empty_string(self, store):
        # title/content/name/relation_type are NOT NULL-ish downstream: the
        # handler must substitute "" rather than pass None through.
        bundle = {
            "items": [{"id": "i1", "item_type": "note"}],
            "entities": [{"id": "e1", "entity_type": "person"}],
            "relations": [{"id": "r1", "source_id": "e1", "target_id": "e1"}],
        }
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 200
        row = store.db.execute("SELECT title, content FROM items").fetchone()
        assert row["title"] == ""
        assert row["content"] == ""

    @pytest.mark.asyncio
    async def test_redacts_credentials_in_imported_content(self, store):
        secret = "AKIAIOSFODNN7EXAMPLE"
        bundle = {
            "items": [
                {"id": "i1", "title": "t", "item_type": "note", "content": f"key {secret} here"}
            ]
        }
        async with _client(_make_app(store)) as client:
            assert (await client.post("/api/knowledge/import", json=bundle)).status == 200
        content = store.db.execute("SELECT content FROM items").fetchone()["content"]
        assert secret not in content

    @pytest.mark.asyncio
    async def test_export_item_bundle_reimports_into_a_fresh_instance(self, store, tmp_path):
        source_store = KnowledgeStore(str(tmp_path / "source.db"))
        try:
            sid = source_store.add_source("f", "local_file", "/tmp/exp.md")
            item_id = source_store.add_item("a", "body", "note", source_id=sid)
            eid = source_store.add_entity("Svc", "service")
            source_store.add_mention(item_id, eid)
            source_store.add_source_location(item_id, sid, section_title="Main")
            async with _client(_make_app(source_store)) as export_client:
                bundle = await (
                    await export_client.get(f"/api/knowledge/items/{item_id}/export")
                ).json()
        finally:
            source_store.close()

        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 200
            result = await resp.json()
        assert result["items_imported"] == 1
        assert store.get_item(item_id) is not None

    @pytest.mark.asyncio
    async def test_bundle_violating_foreign_keys_is_400_not_500(self, store):
        bundle = {
            "source_locations": [
                {"id": "sl1", "item_id": "missing-item", "source_id": "missing-source"}
            ]
        }
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 400
            body = await resp.json()
        # A machine-readable code, not just prose -- the dashboard renders
        # `error` verbatim into a localized UI (test_error_code_contract.py).
        assert body["code"] == "malformed_knowledge_bundle"
        assert store.db.execute("SELECT COUNT(*) c FROM source_locations").fetchone()["c"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [[1, 2, 3], "just a string", 42])
    async def test_non_object_json_is_400_not_500(self, store, payload):
        # request.json() happily parses a bare array/string/number/null. The
        # shared read_bounded_json guard answers the non-object case for all
        # nine JSON-body sites in this module, so it fires before the
        # bundle-shape validator and owns this code.
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=payload)
            assert resp.status == 400
            body = await resp.json()
        assert body["code"] == "body_not_object"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bundle",
        [
            {"items": [1]},
            {"items": "not-a-list"},
            {"entities": [None]},
            {"relations": [["nested"]]},
        ],
    )
    async def test_non_object_collection_items_is_400_not_500(self, store, bundle):
        # A well-formed top-level dict whose collection isn't a list-of-objects
        # still reaches the redaction loop's item.get(...) on a non-dict entry.
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 400
            body = await resp.json()
        assert body["code"] == "malformed_knowledge_bundle"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bundle",
        [
            {"sources": [1]},
            {"source_locations": ["x"]},
            {"mentions": [42]},
        ],
    )
    async def test_non_object_source_collections_is_400_not_500(self, store, bundle):
        # sources/source_locations/mentions are never touched by the handler's
        # redaction loops (only items/entities/relations are), so a non-dict
        # entry there reaches store.import_bundle() directly -- e.g. src["id"]
        # on an int raises TypeError, which is neither a KeyError nor a
        # sqlite3.Error.
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 400
            body = await resp.json()
        assert body["code"] == "malformed_knowledge_bundle"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bundle",
        [
            {"items": [{"id": "i1", "item_type": "note", "title": [1, 2]}]},
            {"items": [{"id": "i1", "item_type": "note", "content": {"a": 1}}]},
            {"entities": [{"id": "e1", "entity_type": "person", "name": 5}]},
            {
                "relations": [
                    {"id": "r1", "source_id": "e1", "target_id": "e1", "relation_type": ["x"]}
                ]
            },
        ],
    )
    async def test_non_string_redacted_field_is_400_not_500(self, store, bundle):
        # Every field the redaction loops pass to _redact() has to be a
        # string or null -- _redact() forwards non-empty values straight into
        # a regex .finditer() call, which raises TypeError on anything else.
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 400
            body = await resp.json()
        assert body["code"] == "malformed_knowledge_bundle"

    @pytest.mark.asyncio
    async def test_oversized_integer_field_is_400_not_500(self, store):
        # Python ints have no size ceiling; SQLite's INTEGER column is 64-bit.
        # Binding an oversized value raises OverflowError at bind time inside
        # store.import_bundle(), which is neither a KeyError nor sqlite3.Error.
        bundle = {
            "items": [
                {
                    "id": "i1",
                    "title": "t",
                    "content": "c",
                    "item_type": "note",
                    "chunk_index": 10**101,
                }
            ]
        }
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 400
            body = await resp.json()
        assert body["code"] == "malformed_knowledge_bundle"
        assert store.db.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 0

    @pytest.mark.asyncio
    async def test_source_with_non_json_properties_is_400_not_500(self, store):
        # sources.properties is a TEXT column store.import_bundle() writes
        # through unparsed, but every consumer reads it back with
        # json.loads() -- a non-JSON string commits cleanly (200) and only
        # breaks a later, unrelated request (e.g. Sync).
        bundle = {
            "sources": [
                {
                    "id": "s1",
                    "name": "n",
                    "source_type": "local_file",
                    "uri": "/tmp/x",
                    "properties": "not-json",
                }
            ]
        }
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 400
            body = await resp.json()
        assert body["code"] == "malformed_knowledge_bundle"
        assert store.db.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0

    @pytest.mark.asyncio
    async def test_entity_with_non_json_aliases_is_400_not_500(self, store):
        # entities.aliases has the same shape of bug, with a worse blast
        # radius: find_entity() parses every entity's aliases on every
        # lookup, so one malformed row poisons every subsequent call.
        bundle = {
            "entities": [{"id": "e1", "name": "n", "entity_type": "person", "aliases": "not-json"}]
        }
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 400
            body = await resp.json()
        assert body["code"] == "malformed_knowledge_bundle"
        assert store.db.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "props",
        [
            pytest.param(
                1, id="int"
            ),  # SQLite TEXT-coerces to "1"; readers json.loads -> not a dict
            pytest.param([], id="list"),  # wrong container entirely
            pytest.param({}, id="dict"),  # right shape but store expects the JSON *string*
            pytest.param("", id="empty"),  # detail readers json.loads("") -> ValueError
            pytest.param(
                "[]", id="json-array"
            ),  # valid JSON, wrong parsed shape (array, not object)
            # Depth > the 3.10/3.11 recursion limit (1000) -> RecursionError there;
            # platforms whose C-scanner limit is higher parse to end-of-input and
            # raise ValueError instead -- either way the contract is a clean 400.
            # Kept moderate: a huge depth (100k) actually stack-overflowed the
            # Windows CI worker inside the C json scanner before the recursion
            # guard could fire (2MB stack vs Linux's 8MB) -- the guard is what
            # makes deep input raise instead of crash, and it isn't reachable
            # arbitrarily far down the stack.
            pytest.param("[" * 1500, id="deeply-nested"),
        ],
    )
    async def test_source_with_non_object_properties_is_400_not_500(self, store, props):
        # The old guard (`isinstance(props, str) and props`) SKIPPED validation
        # for every non-string and for "", committing a row that only crashes a
        # later read (source detail handlers json.loads the raw column with no
        # empty guard).  Anything present must be a JSON *object string*.
        bundle = {
            "sources": [
                {
                    "id": "s1",
                    "name": "n",
                    "source_type": "local_file",
                    "uri": "/tmp/x",
                    "properties": props,
                }
            ]
        }
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 400
            body = await resp.json()
        assert body["code"] == "malformed_knowledge_bundle"
        assert store.db.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "aliases",
        [
            pytest.param(
                5, id="int"
            ),  # non-string: TEXT-coerces to "5"; find_entity json.loads -> not a list
            pytest.param(
                "[1]", id="non-string-elems"
            ),  # find_entity calls a.lower() on each element -> crash
            pytest.param("", id="empty"),  # empty string is not valid JSON
            pytest.param(
                "{}", id="json-object"
            ),  # valid JSON, wrong parsed shape (object, not array)
            pytest.param("[" * 1500, id="deeply-nested"),  # see properties note above
        ],
    )
    async def test_entity_with_non_string_array_aliases_is_400_not_500(self, store, aliases):
        # Same class as properties above; aliases must additionally be an
        # array OF STRINGS because find_entity() lower()s each element.
        bundle = {
            "entities": [{"id": "e1", "name": "n", "entity_type": "person", "aliases": aliases}]
        }
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 400
            body = await resp.json()
        assert body["code"] == "malformed_knowledge_bundle"
        assert store.db.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "field_bundle",
        [
            pytest.param(
                {
                    "sources": [
                        {
                            "id": "s1",
                            "name": "n",
                            "source_type": "local_file",
                            "uri": "/tmp/x",
                            "properties": "@@boom@@",
                        }
                    ]
                },
                id="properties",
            ),
            pytest.param(
                {
                    "entities": [
                        {"id": "e1", "name": "n", "entity_type": "person", "aliases": "@@boom@@"}
                    ]
                },
                id="aliases",
            ),
        ],
    )
    async def test_recursion_error_during_validation_is_400_not_500(
        self, store, monkeypatch, field_bundle
    ):
        # Deterministic RecursionError discriminator: the depth at which the
        # platform's json C-scanner raises (vs parses) varies, so force the
        # error instead of gambling on real nesting.  The old guard caught only
        # ValueError, leaking RecursionError as an unhandled 500.
        from kiro_crew.dashboard.handlers import knowledge as knowledge_mod

        real_loads = knowledge_mod.json.loads

        def exploding_loads(s, *args, **kwargs):
            if s == "@@boom@@":
                raise RecursionError("maximum recursion depth exceeded")
            return real_loads(s, *args, **kwargs)

        monkeypatch.setattr(knowledge_mod.json, "loads", exploding_loads)
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=field_bundle)
            assert resp.status == 400
            body = await resp.json()
        assert body["code"] == "malformed_knowledge_bundle"
        assert store.db.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0
        assert store.db.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"] == 0

    @pytest.mark.asyncio
    async def test_null_properties_and_aliases_still_import(self, store):
        # Absent/null falls through to the store's '{}'/'[]' defaults -- the
        # tightened validator must not reject the shapes export never writes
        # but hand-built bundles legitimately omit.
        bundle = {
            "sources": [
                {
                    "id": "s1",
                    "name": "n",
                    "source_type": "local_file",
                    "uri": "/tmp/x",
                    "properties": None,
                }
            ],
            "entities": [{"id": "e1", "name": "n", "entity_type": "person", "aliases": None}],
        }
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 200
        assert store.db.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 1
        assert store.db.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"] == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(sqlite3.OperationalError("database is locked"), id="locked-db"),
            pytest.param(sqlite3.OperationalError("disk I/O error"), id="full-disk"),
            pytest.param(sqlite3.InterfaceError("bad binding"), id="interface"),
            pytest.param(sqlite3.InternalError("corrupt page"), id="internal"),
        ],
    )
    async def test_operational_store_failure_is_500_not_400(self, store, monkeypatch, exc):
        # A locked DB past busy_timeout or a full disk is not the client's
        # fault: 400 "malformed bundle" for a valid file sends the user off
        # debugging their export.  Every non-bad-bundle sqlite3.Error must
        # surface as a 5xx with a generic body.
        def exploding_import(bundle):
            raise exc

        monkeypatch.setattr(store, "import_bundle", exploding_import)
        bundle = {"items": [{"id": "i1", "title": "t", "content": "c", "item_type": "note"}]}
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 500
            body = await resp.json()
        assert body["code"] == "knowledge_import_failed"
        assert body["error"] == "internal server error"
        assert str(exc) not in body["error"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(sqlite3.IntegrityError("FOREIGN KEY constraint failed"), id="integrity"),
            pytest.param(
                sqlite3.ProgrammingError("Incorrect number of bindings"), id="programming"
            ),
            pytest.param(sqlite3.DataError("string or blob too big"), id="data"),
        ],
    )
    async def test_bad_bundle_store_failure_is_still_400(self, store, monkeypatch, exc):
        # The narrowed arm must keep classifying genuine bad-bundle failures
        # as 400 -- IntegrityError (constraints), ProgrammingError and
        # DataError (bad values reaching the SQL layer).
        def exploding_import(bundle):
            raise exc

        monkeypatch.setattr(store, "import_bundle", exploding_import)
        bundle = {"items": [{"id": "i1", "title": "t", "content": "c", "item_type": "note"}]}
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 400
            body = await resp.json()
        assert body["code"] == "malformed_knowledge_bundle"

    @pytest.mark.asyncio
    async def test_client_error_carries_no_raw_exception_text(self, store):
        # The dashboard renders ``error`` verbatim into a localized UI, so
        # raw driver/exception text must never reach the client -- on either
        # the 400 arm (e2e via a real FK violation) or the 500 arm (covered
        # above).  Server-side logs keep the detail instead.
        bundle = {
            "source_locations": [
                {"id": "sl1", "item_id": "missing-item", "source_id": "missing-source"}
            ]
        }
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 400
            body = await resp.json()
        assert body["error"] == "malformed bundle"
        assert "FOREIGN KEY" not in body["error"]
        assert "constraint" not in body["error"].lower()


# ----------------------------------------------------------------- embeddings


class TestGetEmbeddingStatus:
    @pytest.mark.asyncio
    async def test_disabled_without_embedder(self, store):
        store.add_item("a", "body", "note")
        async with _client(_make_app(store)) as client:
            data = await (await client.get("/api/knowledge/embedding/status")).json()
        assert data == {
            "enabled": False,
            "available": False,
            "model": None,
            "total_items": 1,
            "embedded_items": 0,
        }

    @pytest.mark.asyncio
    async def test_reports_progress_with_embedder(self, store):
        store.add_item("a", "body", "note", embedding=b"\x00")
        store.add_item("b", "body", "note")
        async with _client(_make_app(store, embedder=_FakeEmbedder())) as client:
            data = await (await client.get("/api/knowledge/embedding/status")).json()
        assert data["enabled"] is True
        assert data["available"] is True
        assert (data["total_items"], data["embedded_items"]) == (2, 1)


class TestRebuildEmbeddingsJob:
    @pytest.mark.asyncio
    async def test_completed_job_records_processed_count(self, store, monkeypatch):
        job_id = _add_job(store, "reb-1")

        async def _fake_rebuild(_store, _emb, *, job_id, force, pace):
            return 5

        monkeypatch.setattr(f"{MODULE}.rebuild_embeddings", _fake_rebuild)
        await kh._rebuild_embeddings_job(web.Application(), store, _FakeEmbedder(), job_id)
        row = store.db.execute(
            "SELECT status, items_processed FROM ingestion_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        assert row["status"] == "completed"
        assert row["items_processed"] == 5

    @pytest.mark.asyncio
    async def test_failure_records_error_on_the_job_row(self, store, monkeypatch):
        job_id = _add_job(store, "reb-2")

        async def _boom(*_a, **_kw):
            raise RuntimeError("embed exploded")

        monkeypatch.setattr(f"{MODULE}.rebuild_embeddings", _boom)
        await kh._rebuild_embeddings_job(
            web.Application(), store, _FakeEmbedder(), job_id, force=True
        )
        row = store.db.execute(
            "SELECT status, error FROM ingestion_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        assert row["status"] == "failed"
        assert "embed exploded" in row["error"]

    @pytest.mark.asyncio
    async def test_cancellation_finalizes_row_and_reraises(self, store, monkeypatch):
        # A shutdown cancellation must not leave the row 'processing', or the
        # single-flight guard would refuse every future rebuild.
        job_id = _add_job(store, "reb-3")

        async def _cancelled(*_a, **_kw):
            raise asyncio.CancelledError()

        monkeypatch.setattr(f"{MODULE}.rebuild_embeddings", _cancelled)
        with pytest.raises(asyncio.CancelledError):
            await kh._rebuild_embeddings_job(web.Application(), store, _FakeEmbedder(), job_id)
        row = store.db.execute(
            "SELECT status FROM ingestion_jobs WHERE id = ?", (job_id,)
        ).fetchone()
        assert row["status"] == "cancelled"


class TestBatchEmbedItems:
    @pytest.mark.asyncio
    async def test_without_embedder_is_400(self, store):
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/embedding/generate")
            assert resp.status == 400
            assert (await resp.json())["error"] == "Embedding not enabled"

    @pytest.mark.asyncio
    async def test_unavailable_model_is_503(self, store):
        app = _make_app(store, embedder=_FakeEmbedder(available=False))
        async with _client(app) as client:
            resp = await client.post("/api/knowledge/embedding/generate")
            assert resp.status == 503
            assert (await resp.json())["error"] == "Embedding model not available"

    @pytest.mark.asyncio
    async def test_fills_null_embeddings_synchronously(self, store):
        i1 = store.add_item("a", "body a", "note")
        store.add_item("b", "body b", "note", embedding=b"\x01")
        emb = _FakeEmbedder()
        async with _client(_make_app(store, embedder=emb)) as client:
            resp = await client.post("/api/knowledge/embedding/generate", json={})
            data = await resp.json()
        assert (data["embedded"], data["total"], data["remaining"]) == (1, 1, 0)
        row = store.db.execute(
            "SELECT embedding, embedding_sig FROM items WHERE id = ?", (i1,)
        ).fetchone()
        assert row["embedding"] is not None
        assert row["embedding_sig"]

    @pytest.mark.asyncio
    async def test_items_the_embedder_declines_stay_unembedded(self, store):
        store.add_item("a", "body a", "note")
        emb = _FakeEmbedder(vec=())
        async with _client(_make_app(store, embedder=emb)) as client:
            data = await (await client.post("/api/knowledge/embedding/generate", json={})).json()
        assert data["embedded"] == 0
        assert data["remaining"] == 1

    @pytest.mark.asyncio
    async def test_commits_periodically_across_a_large_batch(self, store):
        # The loop commits every 50 rows so a long fill is not one giant
        # transaction; drive past that boundary.
        for i in range(51):
            store.add_item(f"item {i}", "body", "note")
        async with _client(_make_app(store, embedder=_FakeEmbedder())) as client:
            data = await (await client.post("/api/knowledge/embedding/generate", json={})).json()
        assert data["embedded"] == 51
        assert data["remaining"] == 0

    @pytest.mark.asyncio
    async def test_rebuild_starts_background_job(self, store, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.start_rebuild_job", lambda _s: "new-job")
        ran = asyncio.Event()

        async def _fake_job(app, st, emb, job_id, force=False):
            ran.set()

        monkeypatch.setattr(f"{MODULE}._rebuild_embeddings_job", _fake_job)
        app = _make_app(store, embedder=_FakeEmbedder())
        async with _client(app) as client:
            resp = await client.post(
                "/api/knowledge/embedding/generate", json={"rebuild": True, "force": True}
            )
            data = await resp.json()
            await asyncio.wait_for(ran.wait(), timeout=5)
        assert data == {"job_id": "new-job", "status": "processing"}

    @pytest.mark.asyncio
    async def test_rebuild_already_running_returns_the_active_job(self, store, monkeypatch):
        _add_job(store, "in-flight", status="processing")
        monkeypatch.setattr(f"{MODULE}.start_rebuild_job", lambda _s: None)
        async with _client(_make_app(store, embedder=_FakeEmbedder())) as client:
            data = await (
                await client.post("/api/knowledge/embedding/generate", json={"rebuild": True})
            ).json()
        assert data == {"job_id": "in-flight", "status": "processing"}

    @pytest.mark.asyncio
    async def test_rebuild_claim_lost_without_visible_row_reports_null_job(
        self, store, monkeypatch
    ):
        monkeypatch.setattr(f"{MODULE}.start_rebuild_job", lambda _s: None)
        async with _client(_make_app(store, embedder=_FakeEmbedder())) as client:
            data = await (
                await client.post("/api/knowledge/embedding/generate", json={"rebuild": True})
            ).json()
        assert data == {"job_id": None, "status": "processing"}


# ------------------------------------------------------- chat-context search


def _patch_retriever(monkeypatch, results):
    """Route the handler's hybrid search at a fixed result list, off-pool."""

    async def _direct(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(f"{MODULE}.run_in_embed_pool", _direct)
    monkeypatch.setattr(
        f"{MODULE}.HybridRetriever",
        lambda *a, **kw: MagicMock(search=MagicMock(return_value=results)),
    )


class TestSearchForContext:
    @pytest.mark.asyncio
    async def test_missing_query_is_400(self, store):
        async with _client(_make_app(store)) as client:
            resp = await client.get("/api/knowledge/search-for-context", params={"q": "   "})
            assert resp.status == 400
            assert (await resp.json())["error"] == "q parameter required"

    @pytest.mark.asyncio
    async def test_builds_citation_cards(self, store, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        _patch_retriever(
            monkeypatch,
            [
                {
                    "id": "i1",
                    "title": "Design",
                    "content": "abcd" * 10,
                    "source": "src",
                    "source_type": "local_folder",
                    "source_name": "Notes",
                    "source_uri": "/notes",
                    "file_path": "/notes/a.md",
                    "section_title": "Intro",
                    "chunk_range": "1-2",
                    "match_type": "vector",
                    "summary": "sum",
                }
            ],
        )
        async with _client(_make_app(store)) as client:
            data = await (
                await client.get("/api/knowledge/search-for-context", params={"q": "design"})
            ).json()
        card = data["results"][0]
        assert card["title"] == "Design"
        assert card["match_type"] == "vector"
        assert card["source_name"] == "Notes"
        assert card["section_title"] == "Intro"
        assert card["tokens"] == 10
        assert data["total_tokens"] == 10

    @pytest.mark.asyncio
    async def test_untitled_and_default_match_type_fallbacks(self, store, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        _patch_retriever(monkeypatch, [{"id": "i1", "title": "", "content": "x"}])
        async with _client(_make_app(store)) as client:
            data = await (
                await client.get("/api/knowledge/search-for-context", params={"q": "x"})
            ).json()
        card = data["results"][0]
        assert card["title"] == "(untitled)"
        assert card["match_type"] == "keyword"
        assert card["source_type"] is None
        # No summary in the result: the card falls back to the content head.
        assert card["summary"] == "x"

    @pytest.mark.asyncio
    async def test_config_budget_truncates_and_then_stops(self, store, monkeypatch, tmp_path):
        (tmp_path / "config.json").write_text(
            json.dumps({"knowledge": {"fetch_top_n": 5, "fetch_max_tokens": 4}})
        )
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        _patch_retriever(
            monkeypatch,
            [
                {"id": "i1", "title": "big", "content": "z" * 400},
                {"id": "i2", "title": "dropped", "content": "y" * 400},
            ],
        )
        async with _client(_make_app(store)) as client:
            data = await (
                await client.get("/api/knowledge/search-for-context", params={"q": "z"})
            ).json()
        assert data["max_tokens"] == 4
        assert data["total_tokens"] == 4
        # First card is clipped to the budget; the second never gets a slot.
        assert [c["id"] for c in data["results"]] == ["i1"]
        assert len(data["results"][0]["content"]) == 16

    @pytest.mark.asyncio
    async def test_unreadable_config_falls_back_to_defaults(self, store, monkeypatch, tmp_path):
        (tmp_path / "config.json").write_text("{ not json")
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        _patch_retriever(monkeypatch, [])
        async with _client(_make_app(store)) as client:
            data = await (
                await client.get("/api/knowledge/search-for-context", params={"q": "z"})
            ).json()
        assert data["max_tokens"] == kh.KNOWLEDGE_FETCH_MAX_TOKENS
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_non_numeric_limit_falls_back_to_top_n(self, store, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        captured = {}

        async def _direct(fn, *args, **kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr(f"{MODULE}.run_in_embed_pool", _direct)
        monkeypatch.setattr(
            f"{MODULE}.HybridRetriever",
            lambda *a, **kw: MagicMock(search=MagicMock(return_value=[])),
        )
        async with _client(_make_app(store)) as client:
            resp = await client.get(
                "/api/knowledge/search-for-context", params={"q": "z", "limit": "lots"}
            )
            assert resp.status == 200
        assert captured["limit"] == kh.KNOWLEDGE_FETCH_TOP_N

    @pytest.mark.asyncio
    async def test_available_embedder_is_wired_into_the_retriever(
        self, store, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        seen = {}

        async def _direct(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        def _retriever(_store, embedder=None):
            seen["embedder"] = embedder
            return MagicMock(search=MagicMock(return_value=[]))

        monkeypatch.setattr(f"{MODULE}.run_in_embed_pool", _direct)
        monkeypatch.setattr(f"{MODULE}.HybridRetriever", _retriever)
        emb = _FakeEmbedder()
        async with _client(_make_app(store, embedder=emb)) as client:
            assert (
                await client.get("/api/knowledge/search-for-context", params={"q": "z"})
            ).status == 200
        assert seen["embedder"] == emb.embed

    @pytest.mark.asyncio
    async def test_unavailable_embedder_is_not_wired(self, store, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        seen = {}

        async def _direct(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        def _retriever(_store, embedder=None):
            seen["embedder"] = embedder
            return MagicMock(search=MagicMock(return_value=[]))

        monkeypatch.setattr(f"{MODULE}.run_in_embed_pool", _direct)
        monkeypatch.setattr(f"{MODULE}.HybridRetriever", _retriever)
        app = _make_app(store, embedder=_FakeEmbedder(available=False))
        async with _client(app) as client:
            assert (
                await client.get("/api/knowledge/search-for-context", params={"q": "z"})
            ).status == 200
        assert seen["embedder"] is None


# ------------------------------------------------------------ agent document


def _cfg(auto_add=True, auto_ingest=False, kinds=()):
    cfg = MagicMock()
    cfg.knowledge.auto_add_documents = auto_add
    cfg.knowledge.auto_ingest_artifacts = auto_ingest
    cfg.knowledge.auto_ingest_artifact_kinds = list(kinds)
    return cfg


class TestAddAgentDocumentRoute:
    @pytest.mark.asyncio
    async def test_disabled_toggle_is_403(self, store, monkeypatch):
        monkeypatch.setattr(
            f"{MODULE}.KiroCrewConfig.load", staticmethod(lambda: _cfg(auto_add=False))
        )
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post("/api/knowledge/agent-document", json={})
            assert resp.status == 403
            assert (await resp.json())["code"] == "auto_add_documents_disabled"

    @pytest.mark.asyncio
    async def test_missing_pipeline_is_503(self, store, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.KiroCrewConfig.load", staticmethod(_cfg))
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/agent-document", json={})
            assert resp.status == 503
            assert (await resp.json())["code"] == "pipeline_unavailable"

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, store, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.KiroCrewConfig.load", staticmethod(_cfg))
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(
                "/api/knowledge/agent-document",
                data="{",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_rejected_document_is_400(self, store, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.KiroCrewConfig.load", staticmethod(_cfg))
        monkeypatch.setattr(
            f"{MODULE}.add_agent_document",
            AsyncMock(return_value={"status": "error", "error": "too short"}),
        )
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(
                "/api/knowledge/agent-document", json={"title": "t", "content": "c"}
            )
            assert resp.status == 400
            body = await resp.json()
        assert (body["error"], body["code"]) == ("too short", "document_rejected")

    @pytest.mark.asyncio
    async def test_accepted_document_returns_result_and_coerces_fields(self, store, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.KiroCrewConfig.load", staticmethod(_cfg))
        fake_add = AsyncMock(return_value={"status": "added", "title": "T", "item_ids": ["i1"]})
        monkeypatch.setattr(f"{MODULE}.add_agent_document", fake_add)
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(
                "/api/knowledge/agent-document",
                json={"title": "T", "content": "body", "reason": None},
            )
            assert resp.status == 200
            assert (await resp.json())["status"] == "added"
        # None-valued optional fields arrive as empty strings, never as None.
        assert fake_add.await_args.kwargs["reason"] == ""
        assert fake_add.await_args.kwargs["source_uri"] == ""


# ----------------------------------------------------------------- ingest_text


class TestIngestText:
    @pytest.mark.asyncio
    async def test_unknown_source_is_404(self, store):
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post("/api/knowledge/sources/ghost/ingest-text", json={"text": "x"})
            assert resp.status == 404
            assert (await resp.json())["error"] == "source not found"

    @pytest.mark.asyncio
    async def test_missing_pipeline_is_503(self, store):
        sid = store.add_source("s", "web", "https://example.com")
        async with _client(_make_app(store)) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{sid}/ingest-text", json={"text": "x"}
            )
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, store):
        sid = store.add_source("s", "web", "https://example.com")
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{sid}/ingest-text",
                data="{",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_empty_text_is_400(self, store):
        sid = store.add_source("s", "web", "https://example.com")
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/ingest-text", json={"text": ""})
            assert resp.status == 400
            assert (await resp.json())["error"] == "no text provided"

    @pytest.mark.asyncio
    async def test_ingests_and_marks_source_synced(self, store):
        sid = store.add_source("s", "web", "https://example.com")
        pipeline = MagicMock(ingest_file=AsyncMock(return_value="job-9"))
        async with _client(_make_app(store, pipeline=pipeline)) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{sid}/ingest-text",
                json={"text": "hello", "name": "doc", "namespace": "work"},
            )
            assert resp.status == 200
            assert (await resp.json())["job_id"] == "job-9"
        row = store.db.execute("SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["sync_status"] == "synced"
        kwargs = pipeline.ingest_file.await_args.kwargs
        assert kwargs["original_name"] == "doc"
        assert kwargs["namespace"] == "work"
        # The temp file the handler wrote is removed on the way out.
        assert not Path(pipeline.ingest_file.await_args.args[0]).exists()

    @pytest.mark.asyncio
    async def test_ingest_failure_is_500_and_cleans_up(self, store):
        sid = store.add_source("s", "web", "https://example.com")
        pipeline = MagicMock(ingest_file=AsyncMock(side_effect=RuntimeError("boom")))
        async with _client(_make_app(store, pipeline=pipeline)) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{sid}/ingest-text", json={"text": "hello"}
            )
            assert resp.status == 500
            assert (await resp.json())["error"] == "internal server error"
        assert not Path(pipeline.ingest_file.await_args.args[0]).exists()


# --------------------------------------------------- source delete / agent sync


class TestDeleteSourceBranches:
    @pytest.mark.asyncio
    async def test_unknown_source_is_404(self, store):
        async with _client(_make_app(store)) as client:
            assert (await client.delete("/api/knowledge/sources/ghost")).status == 404

    @pytest.mark.asyncio
    async def test_delete_never_tombstones_any_source(self, store, monkeypatch):
        """No source is recorded as dismissed, auto-added or not.

        The tombstone existed so a recurring discovery sweep could not re-create the
        auto source a user had just deleted. Both discovery loops are gone, so a
        deletion is final on its own and recording it would be a write nothing reads.
        """
        auto = store.add_source(
            "auto", "local_folder", "/tmp/auto", properties={"auto_added": True}
        )
        manual = store.add_source("manual", "local_folder", "/tmp/manual")
        seen: list[tuple] = []
        monkeypatch.setattr(
            store, "delete_source_cascade", lambda source_id: seen.append((source_id,))
        )
        async with _client(_make_app(store)) as client:
            for sid in (auto, manual):
                resp = await client.delete(f"/api/knowledge/sources/{sid}")
                assert resp.status == 200
                assert (await resp.json())["status"] == "deleted"
        # One positional argument each: the cascade has no dismissal parameter left
        # for a caller to pass, so neither row can be tombstoned by mistake.
        assert seen == [(auto,), (manual,)]
        assert not store.db.execute("SELECT 1 FROM dismissed_auto_sources").fetchall()

    @pytest.mark.asyncio
    async def test_unreadable_properties_do_not_block_the_delete(self, store, monkeypatch):
        sid = store.add_source("odd", "local_folder", "/tmp/odd")
        store.db.execute("UPDATE sources SET properties = ? WHERE id = ?", ("{not json", sid))
        store.db.commit()
        monkeypatch.setattr(store, "delete_source_cascade", lambda source_id: None)
        async with _client(_make_app(store)) as client:
            assert (await client.delete(f"/api/knowledge/sources/{sid}")).status == 200

    @pytest.mark.asyncio
    async def test_cascade_failure_is_500(self, store, monkeypatch):
        sid = store.add_source("s", "local_folder", "/tmp/s")

        def _boom(source_id):
            raise RuntimeError("locked")

        monkeypatch.setattr(store, "delete_source_cascade", _boom)
        async with _client(_make_app(store)) as client:
            resp = await client.delete(f"/api/knowledge/sources/{sid}")
            assert resp.status == 500
            assert (await resp.json())["error"] == "internal server error"


class TestSyncSourceAgentBranch:
    """The URL-fetch fallback taken when no connector handles the source type."""

    @pytest.mark.asyncio
    async def test_source_without_url_is_400(self, store):
        sid = store.add_source("s", "web", "")
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 400
            assert (await resp.json())["error"] == "no URL to fetch"

    @pytest.mark.asyncio
    async def test_url_from_properties_when_uri_is_blank(self, store, monkeypatch):
        sid = store.add_source("s", "web", "", properties={"url": "https://e.test/a"})
        seen = {}

        async def _fake_sync(source_id, url, name, st, pipeline, pool):
            seen["url"] = url

        monkeypatch.setattr(f"{MODULE}._background_agent_sync", _fake_sync)
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 200
            assert (await resp.json())["status"] == "syncing"
            await asyncio.sleep(0)
        assert seen["url"] == "https://e.test/a"

    @pytest.mark.asyncio
    async def test_sync_already_in_progress_is_409(self, store):
        sid = store.add_source("s", "web", "https://e.test/a")
        store.db.execute("UPDATE sources SET sync_status = 'syncing' WHERE id = ?", (sid,))
        store.db.commit()
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 409
            assert (await resp.json())["error"] == "sync already in progress"

    @pytest.mark.asyncio
    async def test_missing_pipeline_is_503(self, store):
        sid = store.add_source("s", "web", "https://e.test/a")
        async with _client(_make_app(store)) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 503
            body = await resp.json()
            assert body["error"] == "pipeline not configured"
            assert body["code"] == "pipeline_unavailable"
            # The 503 must not leave the row wedged in 'syncing': a retry hits
            # this same 503, not a 409 "already in progress".
            status = store.db.execute(
                "SELECT sync_status FROM sources WHERE id = ?", (sid,)
            ).fetchone()["sync_status"]
            assert status != "syncing"

    @pytest.mark.asyncio
    async def test_missing_fetch_pool_is_503_without_committing_syncing(self, store):
        sid = store.add_source("s", "web", "https://e.test/a")
        app = _make_app(store, pipeline=MagicMock())
        del app["knowledge_fetch_pool"]
        pool_error = "knowledge_fetch_pool is not configured on this application"
        async with _client(app) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert resp.status == 503
            body = await resp.json()
            assert body["error"] == pool_error
            assert body["code"] == "knowledge_fetch_pool_unavailable"
            # The check runs before the 'syncing' commit, so the row is not
            # wedged: a retry gets the same 503 rather than a permanent 409.
            status = store.db.execute(
                "SELECT sync_status FROM sources WHERE id = ?", (sid,)
            ).fetchone()["sync_status"]
            assert status != "syncing"
            retry = await client.post(f"/api/knowledge/sources/{sid}/sync")
            assert retry.status == 503

    @pytest.mark.asyncio
    async def test_spawns_background_sync_and_tracks_the_task(self, store, monkeypatch):
        sid = store.add_source("s", "web", "https://e.test/a")
        ran = asyncio.Event()

        async def _fake_sync(source_id, url, name, st, pipeline, pool):
            ran.set()

        monkeypatch.setattr(f"{MODULE}._background_agent_sync", _fake_sync)
        app = _make_app(store, pipeline=MagicMock())
        async with _client(app) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
            data = await resp.json()
            await asyncio.wait_for(ran.wait(), timeout=5)
        assert data == {"synced": False, "status": "syncing", "source_id": sid}
        # The task is parked in _bg_tasks so it cannot be garbage-collected
        # mid-flight, and is discarded once it finishes.
        assert app["_bg_tasks"] == set()


class TestBackgroundAgentSync:
    @pytest.mark.asyncio
    async def test_success_marks_source_synced_and_removes_temp_file(self, store, monkeypatch):
        sid = store.add_source("s", "web", "https://example.com")
        monkeypatch.setattr(f"{MODULE}.fetch_url_content", AsyncMock(return_value="fetched body"))
        pipeline = MagicMock(ingest_file=AsyncMock(return_value="j1"))
        await kh._background_agent_sync(
            sid, "https://example.com", "S", store, pipeline, MagicMock()
        )
        row = store.db.execute("SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["sync_status"] == "synced"
        assert not Path(pipeline.ingest_file.await_args.args[0]).exists()

    @pytest.mark.asyncio
    async def test_fetch_failure_marks_source_error(self, store, monkeypatch):
        sid = store.add_source("s", "web", "https://example.com")
        monkeypatch.setattr(
            f"{MODULE}.fetch_url_content", AsyncMock(side_effect=RuntimeError("offline"))
        )
        await kh._background_agent_sync(
            sid, "https://example.com", "S", store, MagicMock(), MagicMock()
        )
        row = store.db.execute("SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["sync_status"] == "error"


# --------------------------------------------------------- startup / wiring


class TestSelLog:
    def test_emits_a_namespaced_tool_event(self, monkeypatch):
        recorded = {}
        monkeypatch.setattr(
            f"{MODULE}.sel", lambda: MagicMock(log_tool_invocation=lambda **kw: recorded.update(kw))
        )
        kh._sel_log("item.update", item_id="i1")
        assert recorded["tool_name"] == "knowledge.item.update"
        assert recorded["outcome"] == "completed"
        assert "i1" in recorded["resources"]

    def test_outcome_can_be_overridden_and_leaves_resources_empty(self, monkeypatch):
        recorded = {}
        monkeypatch.setattr(
            f"{MODULE}.sel", lambda: MagicMock(log_tool_invocation=lambda **kw: recorded.update(kw))
        )
        kh._sel_log("batch_embed", outcome="cancelled")
        assert recorded["outcome"] == "cancelled"
        assert recorded["resources"] == ""


class TestCreateEmbedder:
    def test_reads_config_json_when_present(self, monkeypatch, tmp_path):
        (tmp_path / "config.json").write_text(
            json.dumps({"knowledge": {"embed_content_budget": 77}})
        )
        monkeypatch.setattr(f"{MODULE}.config_dir", lambda: tmp_path)
        emb = kh._create_embedder(web.Application())
        assert emb.content_budget == 77

    def test_missing_config_falls_back_to_defaults(self, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{MODULE}.config_dir", lambda: tmp_path)
        assert kh._create_embedder(web.Application()) is not None

    def test_unreadable_config_falls_back_to_defaults(self, monkeypatch, tmp_path):
        (tmp_path / "config.json").write_text("{ broken")
        monkeypatch.setattr(f"{MODULE}.config_dir", lambda: tmp_path)
        assert kh._create_embedder(web.Application()) is not None


class TestStartWatcherAsync:
    @pytest.mark.asyncio
    async def test_stops_previous_watcher_and_registers_the_new_one(self, store, monkeypatch):
        old = MagicMock(stop=AsyncMock())
        started = asyncio.Event()

        class _FakeWatcher:
            def __init__(self, *, store, pipeline):
                self.store = store

            async def start(self):
                started.set()

            async def stop(self):
                return None

        monkeypatch.setattr(f"{MODULE}.KnowledgeWatcher", _FakeWatcher)

        app = _make_app(store, pipeline=MagicMock(), watcher=old)
        await kh._start_watcher_async(app)
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            old.stop.assert_awaited_once()
            assert isinstance(app["knowledge_watcher"], _FakeWatcher)
            # The watcher is constructed with the store and pipeline only: it no
            # longer receives a project-dirs callback, because nothing registers a
            # project directory on its own.
            assert app["knowledge_watcher"].store is store
        finally:
            task = app["_knowledge_watcher_task"]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_works_with_no_previous_watcher(self, store, monkeypatch):
        class _FakeWatcher:
            def __init__(self, **_kw):
                pass

            async def start(self):
                return None

        monkeypatch.setattr(f"{MODULE}.KnowledgeWatcher", _FakeWatcher)
        app = _make_app(store, pipeline=MagicMock())
        await kh._start_watcher_async(app)
        task = app["_knowledge_watcher_task"]
        await asyncio.gather(task, return_exceptions=True)
        assert app["knowledge_watcher"] is not None


class TestStartArtifactIngestAsync:
    @pytest.mark.asyncio
    async def test_disabled_toggle_is_a_no_op(self, store, monkeypatch):
        monkeypatch.setattr(
            f"{MODULE}.KiroCrewConfig.load", staticmethod(lambda: _cfg(auto_ingest=False))
        )
        app = _make_app(store, pipeline=MagicMock())
        await kh._start_artifact_ingest_async(app)
        assert "artifact_knowledge_sync" not in app

    @pytest.mark.asyncio
    async def test_enabled_registers_change_listener_and_starts(self, store, monkeypatch):
        monkeypatch.setattr(
            f"{MODULE}.KiroCrewConfig.load",
            staticmethod(lambda: _cfg(auto_ingest=True, kinds=("webapp",))),
        )
        art_store = MagicMock()
        monkeypatch.setattr(f"{MODULE}.get_default_store", lambda: art_store)
        made = {}

        class _FakeSync:
            def __init__(self, *, art_store, pipeline, kinds, loop):
                made["kinds"] = kinds
                self.on_change = object()

            async def start(self):
                made["started"] = True

        monkeypatch.setattr(f"{MODULE}.ArtifactKnowledgeSync", _FakeSync)
        app = _make_app(store, pipeline=MagicMock())
        await kh._start_artifact_ingest_async(app)
        assert made == {"kinds": {"webapp"}, "started": True}
        art_store.set_change_listener.assert_called_once()
        # The binding is held on the app so the listener is not collected.
        assert isinstance(app["artifact_knowledge_sync"], _FakeSync)


class TestSetupKnowledgeRoutes:
    @pytest.mark.asyncio
    async def test_builds_pipeline_connectors_and_routes(self, store, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{MODULE}.config_dir", lambda: tmp_path)
        app = web.Application()
        state = MagicMock()
        state.knowledge_store = store
        app["state"] = state

        kh.setup_knowledge_routes(app)
        try:
            assert app["knowledge_pipeline"] is not None
            assert app["knowledge_embedder"] is not None
            sync = app["knowledge_sync"]
            # Built-in connectors are always present; the edition seam adds to
            # them and the Default contributes nothing.
            assert sync.get_connector("local_folder") is not None
            assert sync.get_connector("obsidian_vault") is not None
            assert sync.get_connector("nope") is None
            # Both startup hooks are registered (watcher + artifact ingest).
            # aiohttp seeds on_startup with its own cleanup-ctx hook, so compare
            # membership rather than a raw length.
            assert kh._start_watcher_async in app.on_startup
            assert kh._start_artifact_ingest_async in app.on_startup

            paths = {r.resource.canonical for r in app.router.routes() if r.resource is not None}
            for expected in (
                "/api/knowledge/items",
                "/api/knowledge/stats",
                "/api/knowledge/search-for-context",
                "/api/knowledge/embedding/generate",
                "/api/knowledge/agent-document",
            ):
                assert expected in paths
        finally:
            for callback in list(app.on_cleanup):
                await callback(app)

    @pytest.mark.asyncio
    async def test_second_call_keeps_the_existing_pipeline(self, store, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{MODULE}.config_dir", lambda: tmp_path)
        app = _make_app(store, pipeline=MagicMock())
        sentinel = app["knowledge_pipeline"]
        kh.setup_knowledge_routes(app)
        assert app["knowledge_pipeline"] is sentinel
        # No LLM pool / embedder were constructed on the re-entry path.
        assert "knowledge_embedder" not in app
        assert kh._start_watcher_async not in app.on_startup

    @pytest.mark.asyncio
    async def test_cleanup_stops_watcher_and_cancels_its_task(self, store):
        app = _make_app(store, pipeline=MagicMock())
        kh.setup_knowledge_routes(app)
        watcher = MagicMock(stop=AsyncMock())
        app["knowledge_watcher"] = watcher
        forever = asyncio.ensure_future(asyncio.sleep(30))
        app["_knowledge_watcher_task"] = forever
        for callback in list(app.on_cleanup):
            await callback(app)
        watcher.stop.assert_awaited_once()
        await asyncio.gather(forever, return_exceptions=True)
        assert forever.cancelled()


# ------------------------------------------------- JSON object body guard


def _guard_app(store, **kwargs):
    """``_make_app`` plus the mutation routes the body-shape guard tests hit."""
    app = _make_app(store, **kwargs)
    r = app.router
    r.add_post("/api/knowledge/sources", kh.add_source)
    r.add_patch("/api/knowledge/sources/{id}", kh.rename_source)
    r.add_post("/api/knowledge/sources/{id}/files/retry", kh.retry_file)
    r.add_post("/api/knowledge/sources/{id}/files/skip", kh.skip_file)
    return app


# Valid JSON that is not an object: calling ``.get`` on either raised
# AttributeError and surfaced as a 500 before the shared guard.
_NON_OBJECT_BODIES = ([1, 2], 7)


class TestJsonObjectBodyGuard:
    """Every ``request.json()`` site answers 400, not 500, on a body that is
    valid JSON but not an object -- and the two previously-unguarded sites
    (files/retry, files/skip) now answer 400 on invalid JSON too."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", _NON_OBJECT_BODIES)
    async def test_update_item_non_object_body_is_400(self, store, payload):
        item_id = store.add_item("a", "body", "note")
        async with _client(_guard_app(store)) as client:
            resp = await client.patch(f"/api/knowledge/items/{item_id}", json=payload)
            assert resp.status == 400
            assert (await resp.json())["code"] == "body_not_object"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", _NON_OBJECT_BODIES)
    async def test_add_source_non_object_body_is_400(self, store, payload):
        async with _client(_guard_app(store)) as client:
            resp = await client.post("/api/knowledge/sources", json=payload)
            assert resp.status == 400
            assert (await resp.json())["code"] == "body_not_object"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", _NON_OBJECT_BODIES)
    async def test_rename_source_non_object_body_is_400(self, store, payload):
        sid = store.add_source("s", "web", "https://example.com")
        async with _client(_guard_app(store)) as client:
            resp = await client.patch(f"/api/knowledge/sources/{sid}", json=payload)
            assert resp.status == 400
            assert (await resp.json())["code"] == "body_not_object"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", ["retry", "skip"])
    @pytest.mark.parametrize("payload", _NON_OBJECT_BODIES)
    async def test_file_state_non_object_body_is_400(self, store, endpoint, payload):
        sid = store.add_source("s", "local_folder", "/tmp/x")
        async with _client(_guard_app(store)) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/files/{endpoint}", json=payload)
            assert resp.status == 400
            assert (await resp.json())["code"] == "body_not_object"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("endpoint", ["retry", "skip"])
    async def test_file_state_invalid_json_is_400_not_500(self, store, endpoint):
        # These two sites previously had no try/except at all: a malformed
        # body escaped as a raw JSONDecodeError and surfaced as a 500.
        sid = store.add_source("s", "local_folder", "/tmp/x")
        async with _client(_guard_app(store)) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{sid}/files/{endpoint}",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_json"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", _NON_OBJECT_BODIES)
    async def test_ingest_text_non_object_body_is_400(self, store, payload):
        sid = store.add_source("s", "web", "https://example.com")
        async with _client(_guard_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/ingest-text", json=payload)
            assert resp.status == 400
            assert (await resp.json())["code"] == "body_not_object"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", _NON_OBJECT_BODIES)
    async def test_import_bundle_non_object_body_is_400(self, store, payload):
        async with _client(_guard_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=payload)
            assert resp.status == 400
            assert (await resp.json())["code"] == "body_not_object"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", _NON_OBJECT_BODIES)
    async def test_agent_document_non_object_body_is_400(self, store, monkeypatch, payload):
        monkeypatch.setattr(f"{MODULE}.KiroCrewConfig.load", staticmethod(_cfg))
        async with _client(_guard_app(store, pipeline=MagicMock())) as client:
            resp = await client.post("/api/knowledge/agent-document", json=payload)
            assert resp.status == 400
            assert (await resp.json())["code"] == "body_not_object"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", _NON_OBJECT_BODIES)
    async def test_batch_embed_non_object_body_is_400(self, store, payload):
        async with _client(_guard_app(store, embedder=_FakeEmbedder())) as client:
            resp = await client.post("/api/knowledge/embedding/generate", json=payload)
            assert resp.status == 400
            assert (await resp.json())["code"] == "body_not_object"

    @pytest.mark.asyncio
    async def test_batch_embed_invalid_json_is_400(self, store):
        async with _client(_guard_app(store, embedder=_FakeEmbedder())) as client:
            resp = await client.post(
                "/api/knowledge/embedding/generate",
                data="{oops",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_batch_embed_absent_body_still_means_defaults(self, store):
        # No body at all keeps the fill-NULL default path (not a 400): the
        # guard's allow_absent branch preserves the pre-guard contract.
        async with _client(_guard_app(store, embedder=_FakeEmbedder())) as client:
            resp = await client.post("/api/knowledge/embedding/generate")
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_invalid_json_yields_code_at_every_site(self, store, monkeypatch):
        # At the previously-guarded sites the parse-failure path's entire
        # change is the machine-readable ``code`` field -- pin it everywhere.
        monkeypatch.setattr(f"{MODULE}.KiroCrewConfig.load", staticmethod(_cfg))
        item_id = store.add_item("a", "body", "note")
        sid = store.add_source("s", "web", "https://example.com")
        app = _guard_app(store, pipeline=MagicMock(), embedder=_FakeEmbedder())
        endpoints = [
            ("patch", f"/api/knowledge/items/{item_id}"),
            ("post", "/api/knowledge/sources"),
            ("patch", f"/api/knowledge/sources/{sid}"),
            ("post", f"/api/knowledge/sources/{sid}/files/retry"),
            ("post", f"/api/knowledge/sources/{sid}/files/skip"),
            ("post", f"/api/knowledge/sources/{sid}/ingest-text"),
            ("post", "/api/knowledge/import"),
            ("post", "/api/knowledge/embedding/generate"),
            ("post", "/api/knowledge/agent-document"),
        ]
        async with _client(app) as client:
            for method, path in endpoints:
                resp = await getattr(client, method)(
                    path, data="{oops", headers={"Content-Type": "application/json"}
                )
                assert resp.status == 400, (path, resp.status)
                assert (await resp.json())["code"] == "invalid_json", path

    @pytest.mark.asyncio
    async def test_absent_body_is_400_at_non_allow_absent_site(self, store):
        # Only the embedding-generate site opts into allow_absent; everywhere
        # else an empty body is a client mistake, not silent defaults.
        async with _client(_guard_app(store)) as client:
            resp = await client.post("/api/knowledge/sources")
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_unknown_charset_is_400_not_500(self, store):
        # charset= names a codec Python does not have: request.json() raises
        # LookupError (not a ValueError), which must also read as a client
        # mistake, not a 500.
        async with _client(_guard_app(store)) as client:
            resp = await client.post(
                "/api/knowledge/sources",
                data=b'{"name": "x"}',
                headers={"Content-Type": "application/json; charset=not-a-codec"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_json"

    # The RecursionError and transport-error boundaries moved with the guard:
    # they are properties of ``_shared.read_bounded_json``, not of this module,
    # and are pinned in ``test_read_bounded_json.py`` (TestDecodeContract).

    @pytest.mark.asyncio
    async def test_file_state_object_body_still_works(self, store):
        # The guard must not break the success path it fronts.
        sid = store.add_source("s", "local_folder", "/tmp/x")
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, last_seen, status) "
            "VALUES (?, ?, '2026-01-01', 'failed')",
            (sid, "/tmp/x/a.md"),
        )
        store.db.commit()
        async with _client(_guard_app(store)) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{sid}/files/retry", json={"file_path": "/tmp/x/a.md"}
            )
            assert resp.status == 200
            row = store.db.execute(
                "SELECT status FROM folder_file_state WHERE source_id = ?", (sid,)
            ).fetchone()
            assert row["status"] == "pending"
