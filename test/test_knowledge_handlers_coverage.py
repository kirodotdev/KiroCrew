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

    def __init__(self, *, available=True, vec=(0.1, 0.2, 0.3, 0.4),
                 model="fake-embed:1"):
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
    app["knowledge_llm_pool"] = (
        pool if pool is not None else MagicMock(shutdown=AsyncMock()))
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
        (job_id, source_id, status))
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
                (e1,))
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
            resp = await client.patch(f"/api/knowledge/items/{item_id}",
                                      data="not json",
                                      headers={"Content-Type": "application/json"})
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid JSON"

    @pytest.mark.asyncio
    async def test_no_allowed_field_is_400(self, store):
        item_id = store.add_item("a", "body", "note")
        async with _client(_make_app(store)) as client:
            resp = await client.patch(f"/api/knowledge/items/{item_id}",
                                      json={"content": "hijack", "id": "other"})
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
                json={"title": "renamed", "tags": ["x"], "namespace": "work"})
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
            data = await (await client.get(
                "/api/knowledge/entities", params={"type": "project"})).json()
        assert [e["name"] for e in data] == ["Alpha"]

    @pytest.mark.asyncio
    async def test_filters_by_name_substring(self, store):
        store.add_entity("Zeta", "person")
        store.add_entity("Alpha", "project")
        async with _client(_make_app(store)) as client:
            data = await (await client.get(
                "/api/knowledge/entities", params={"q": "lph"})).json()
        assert [e["name"] for e in data] == ["Alpha"]

    @pytest.mark.asyncio
    async def test_combined_type_and_q_filters(self, store):
        store.add_entity("Alpha", "project")
        store.add_entity("Alphabet", "person")
        async with _client(_make_app(store)) as client:
            data = await (await client.get(
                "/api/knowledge/entities",
                params={"q": "Alpha", "type": "person"})).json()
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
            data = await (await client.get(
                "/api/knowledge/entities", params={"limit": "0"})).json()
        # 0 (and any smaller value) clamps up to 1 rather than returning nothing.
        assert len(data) == 1


class TestGetEntityGraph:
    @pytest.mark.asyncio
    async def test_non_numeric_depth_is_400(self, store):
        async with _client(_make_app(store)) as client:
            resp = await client.get("/api/knowledge/entities/x/graph",
                                    params={"depth": "deep"})
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
            data = await (await client.get(
                f"/api/knowledge/entities/{e1}/graph", params={"depth": "1"})).json()
        assert {n["id"] for n in data["nodes"]} == {e1, e2}


class TestGetEntityItems:
    @pytest.mark.asyncio
    async def test_matches_items_mentioning_the_entity_name(self, store):
        store.add_item("Roadmap", "Alice owns the plan", "note")
        store.add_item("Unrelated", "nothing here", "note")
        async with _client(_make_app(store)) as client:
            data = await (await client.get(
                "/api/knowledge/entities/by-name/Alice/items")).json()
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
            resp = await client.get("/api/knowledge/items/x/related",
                                    params={"limit": "many"})
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
            data = await (await client.get(
                f"/api/knowledge/items/{base}/related")).json()

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
            data = await (await client.get(
                "/api/knowledge/graph", params={"limit": "1"})).json()
        assert len(data["nodes"]) == 1
        assert data["edges"] == []


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
            assert (await resp.json())["item"]["id"] == item_id

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
            resp = await client.get("/api/knowledge/export",
                                    params={"namespace": "work/../etc"})
            disp = resp.headers["Content-Disposition"]
        # Path separators and dots-with-slashes cannot survive into the
        # suggested filename, so the download cannot escape its directory.
        assert "/" not in disp.split("filename=")[1]
        assert "work" in disp


class TestImportBundle:
    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, store):
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", data="{oops",
                                     headers={"Content-Type": "application/json"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_imports_items_entities_and_relations(self, store):
        bundle = {
            "items": [{"id": "i1", "title": "Imported", "content": "hello",
                       "summary": "s", "item_type": "note"}],
            "entities": [{"id": "e1", "name": "Alice", "entity_type": "person",
                          "description": "d"}],
            "relations": [{"id": "r1", "source_id": "e1", "target_id": "e1",
                           "relation_type": "self", "description": "d"}],
        }
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 200
            result = await resp.json()
        assert result["items_imported"] == 1
        assert result["entities_created"] == 1

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
        bundle = {"items": [{"id": "i1", "title": "t", "item_type": "note",
                             "content": f"key {secret} here"}]}
        async with _client(_make_app(store)) as client:
            assert (await client.post("/api/knowledge/import", json=bundle)).status == 200
        content = store.db.execute("SELECT content FROM items").fetchone()["content"]
        assert secret not in content


# ----------------------------------------------------------------- embeddings


class TestGetEmbeddingStatus:
    @pytest.mark.asyncio
    async def test_disabled_without_embedder(self, store):
        store.add_item("a", "body", "note")
        async with _client(_make_app(store)) as client:
            data = await (await client.get("/api/knowledge/embedding/status")).json()
        assert data == {"enabled": False, "available": False, "model": None,
                        "total_items": 1, "embedded_items": 0}

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

        async def _fake_rebuild(_store, _emb, *, job_id, force):
            return 5

        monkeypatch.setattr(f"{MODULE}.rebuild_embeddings", _fake_rebuild)
        await kh._rebuild_embeddings_job(web.Application(), store,
                                         _FakeEmbedder(), job_id)
        row = store.db.execute(
            "SELECT status, items_processed FROM ingestion_jobs WHERE id = ?",
            (job_id,)).fetchone()
        assert row["status"] == "completed"
        assert row["items_processed"] == 5

    @pytest.mark.asyncio
    async def test_failure_records_error_on_the_job_row(self, store, monkeypatch):
        job_id = _add_job(store, "reb-2")

        async def _boom(*_a, **_kw):
            raise RuntimeError("embed exploded")

        monkeypatch.setattr(f"{MODULE}.rebuild_embeddings", _boom)
        await kh._rebuild_embeddings_job(web.Application(), store,
                                         _FakeEmbedder(), job_id, force=True)
        row = store.db.execute(
            "SELECT status, error FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
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
            await kh._rebuild_embeddings_job(web.Application(), store,
                                             _FakeEmbedder(), job_id)
        row = store.db.execute(
            "SELECT status FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
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
            "SELECT embedding, embedding_sig FROM items WHERE id = ?", (i1,)).fetchone()
        assert row["embedding"] is not None
        assert row["embedding_sig"]

    @pytest.mark.asyncio
    async def test_items_the_embedder_declines_stay_unembedded(self, store):
        store.add_item("a", "body a", "note")
        emb = _FakeEmbedder(vec=())
        async with _client(_make_app(store, embedder=emb)) as client:
            data = await (await client.post(
                "/api/knowledge/embedding/generate", json={})).json()
        assert data["embedded"] == 0
        assert data["remaining"] == 1

    @pytest.mark.asyncio
    async def test_commits_periodically_across_a_large_batch(self, store):
        # The loop commits every 50 rows so a long fill is not one giant
        # transaction; drive past that boundary.
        for i in range(51):
            store.add_item(f"item {i}", "body", "note")
        async with _client(_make_app(store, embedder=_FakeEmbedder())) as client:
            data = await (await client.post(
                "/api/knowledge/embedding/generate", json={})).json()
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
            resp = await client.post("/api/knowledge/embedding/generate",
                                     json={"rebuild": True, "force": True})
            data = await resp.json()
            await asyncio.wait_for(ran.wait(), timeout=5)
        assert data == {"job_id": "new-job", "status": "processing"}

    @pytest.mark.asyncio
    async def test_rebuild_already_running_returns_the_active_job(self, store, monkeypatch):
        _add_job(store, "in-flight", status="processing")
        monkeypatch.setattr(f"{MODULE}.start_rebuild_job", lambda _s: None)
        async with _client(_make_app(store, embedder=_FakeEmbedder())) as client:
            data = await (await client.post("/api/knowledge/embedding/generate",
                                            json={"rebuild": True})).json()
        assert data == {"job_id": "in-flight", "status": "processing"}

    @pytest.mark.asyncio
    async def test_rebuild_claim_lost_without_visible_row_reports_null_job(
            self, store, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.start_rebuild_job", lambda _s: None)
        async with _client(_make_app(store, embedder=_FakeEmbedder())) as client:
            data = await (await client.post("/api/knowledge/embedding/generate",
                                            json={"rebuild": True})).json()
        assert data == {"job_id": None, "status": "processing"}


# ------------------------------------------------------- chat-context search


def _patch_retriever(monkeypatch, results):
    """Route the handler's hybrid search at a fixed result list, off-pool."""

    async def _direct(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(f"{MODULE}.run_in_embed_pool", _direct)
    monkeypatch.setattr(
        f"{MODULE}.HybridRetriever",
        lambda *a, **kw: MagicMock(search=MagicMock(return_value=results)))


class TestSearchForContext:
    @pytest.mark.asyncio
    async def test_missing_query_is_400(self, store):
        async with _client(_make_app(store)) as client:
            resp = await client.get("/api/knowledge/search-for-context",
                                    params={"q": "   "})
            assert resp.status == 400
            assert (await resp.json())["error"] == "q parameter required"

    @pytest.mark.asyncio
    async def test_builds_citation_cards(self, store, monkeypatch, tmp_path):
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        _patch_retriever(monkeypatch, [{
            "id": "i1", "title": "Design", "content": "abcd" * 10,
            "source": "src", "source_type": "local_folder",
            "source_name": "Notes", "source_uri": "/notes",
            "file_path": "/notes/a.md", "section_title": "Intro",
            "chunk_range": "1-2", "match_type": "vector", "summary": "sum",
        }])
        async with _client(_make_app(store)) as client:
            data = await (await client.get("/api/knowledge/search-for-context",
                                           params={"q": "design"})).json()
        card = data["results"][0]
        assert card["title"] == "Design"
        assert card["match_type"] == "vector"
        assert card["source_name"] == "Notes"
        assert card["section_title"] == "Intro"
        assert card["tokens"] == 10
        assert data["total_tokens"] == 10

    @pytest.mark.asyncio
    async def test_untitled_and_default_match_type_fallbacks(self, store, monkeypatch,
                                                             tmp_path):
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        _patch_retriever(monkeypatch, [{"id": "i1", "title": "", "content": "x"}])
        async with _client(_make_app(store)) as client:
            data = await (await client.get("/api/knowledge/search-for-context",
                                           params={"q": "x"})).json()
        card = data["results"][0]
        assert card["title"] == "(untitled)"
        assert card["match_type"] == "keyword"
        assert card["source_type"] is None
        # No summary in the result: the card falls back to the content head.
        assert card["summary"] == "x"

    @pytest.mark.asyncio
    async def test_config_budget_truncates_and_then_stops(self, store, monkeypatch,
                                                          tmp_path):
        (tmp_path / "config.json").write_text(json.dumps(
            {"knowledge": {"fetch_top_n": 5, "fetch_max_tokens": 4}}))
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        _patch_retriever(monkeypatch, [
            {"id": "i1", "title": "big", "content": "z" * 400},
            {"id": "i2", "title": "dropped", "content": "y" * 400},
        ])
        async with _client(_make_app(store)) as client:
            data = await (await client.get("/api/knowledge/search-for-context",
                                           params={"q": "z"})).json()
        assert data["max_tokens"] == 4
        assert data["total_tokens"] == 4
        # First card is clipped to the budget; the second never gets a slot.
        assert [c["id"] for c in data["results"]] == ["i1"]
        assert len(data["results"][0]["content"]) == 16

    @pytest.mark.asyncio
    async def test_unreadable_config_falls_back_to_defaults(self, store, monkeypatch,
                                                            tmp_path):
        (tmp_path / "config.json").write_text("{ not json")
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        _patch_retriever(monkeypatch, [])
        async with _client(_make_app(store)) as client:
            data = await (await client.get("/api/knowledge/search-for-context",
                                           params={"q": "z"})).json()
        assert data["max_tokens"] == kh.KNOWLEDGE_FETCH_MAX_TOKENS
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_non_numeric_limit_falls_back_to_top_n(self, store, monkeypatch,
                                                         tmp_path):
        monkeypatch.setattr(f"{MODULE}.data_home", lambda: tmp_path)
        captured = {}

        async def _direct(fn, *args, **kwargs):
            captured.update(kwargs)
            return []

        monkeypatch.setattr(f"{MODULE}.run_in_embed_pool", _direct)
        monkeypatch.setattr(f"{MODULE}.HybridRetriever",
                            lambda *a, **kw: MagicMock(search=MagicMock(return_value=[])))
        async with _client(_make_app(store)) as client:
            resp = await client.get("/api/knowledge/search-for-context",
                                    params={"q": "z", "limit": "lots"})
            assert resp.status == 200
        assert captured["limit"] == kh.KNOWLEDGE_FETCH_TOP_N

    @pytest.mark.asyncio
    async def test_available_embedder_is_wired_into_the_retriever(self, store,
                                                                  monkeypatch, tmp_path):
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
            assert (await client.get("/api/knowledge/search-for-context",
                                     params={"q": "z"})).status == 200
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
            assert (await client.get("/api/knowledge/search-for-context",
                                     params={"q": "z"})).status == 200
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
        monkeypatch.setattr(f"{MODULE}.KiroCrewConfig.load",
                            staticmethod(lambda: _cfg(auto_add=False)))
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
            resp = await client.post("/api/knowledge/agent-document", data="{",
                                     headers={"Content-Type": "application/json"})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_json"

    @pytest.mark.asyncio
    async def test_rejected_document_is_400(self, store, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.KiroCrewConfig.load", staticmethod(_cfg))
        monkeypatch.setattr(f"{MODULE}.add_agent_document", AsyncMock(
            return_value={"status": "error", "error": "too short"}))
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post("/api/knowledge/agent-document",
                                     json={"title": "t", "content": "c"})
            assert resp.status == 400
            body = await resp.json()
        assert (body["error"], body["code"]) == ("too short", "document_rejected")

    @pytest.mark.asyncio
    async def test_accepted_document_returns_result_and_coerces_fields(
            self, store, monkeypatch):
        monkeypatch.setattr(f"{MODULE}.KiroCrewConfig.load", staticmethod(_cfg))
        fake_add = AsyncMock(return_value={"status": "added", "title": "T",
                                           "item_ids": ["i1"]})
        monkeypatch.setattr(f"{MODULE}.add_agent_document", fake_add)
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post("/api/knowledge/agent-document",
                                     json={"title": "T", "content": "body",
                                           "reason": None})
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
            resp = await client.post("/api/knowledge/sources/ghost/ingest-text",
                                     json={"text": "x"})
            assert resp.status == 404
            assert (await resp.json())["error"] == "source not found"

    @pytest.mark.asyncio
    async def test_missing_pipeline_is_503(self, store):
        sid = store.add_source("s", "web", "https://example.com")
        async with _client(_make_app(store)) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/ingest-text",
                                     json={"text": "x"})
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, store):
        sid = store.add_source("s", "web", "https://example.com")
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/ingest-text",
                                     data="{", headers={"Content-Type": "application/json"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_empty_text_is_400(self, store):
        sid = store.add_source("s", "web", "https://example.com")
        async with _client(_make_app(store, pipeline=MagicMock())) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/ingest-text",
                                     json={"text": ""})
            assert resp.status == 400
            assert (await resp.json())["error"] == "no text provided"

    @pytest.mark.asyncio
    async def test_ingests_and_marks_source_synced(self, store):
        sid = store.add_source("s", "web", "https://example.com")
        pipeline = MagicMock(ingest_file=AsyncMock(return_value="job-9"))
        async with _client(_make_app(store, pipeline=pipeline)) as client:
            resp = await client.post(f"/api/knowledge/sources/{sid}/ingest-text",
                                     json={"text": "hello", "name": "doc",
                                           "namespace": "work"})
            assert resp.status == 200
            assert (await resp.json())["job_id"] == "job-9"
        row = store.db.execute("SELECT sync_status FROM sources WHERE id = ?",
                               (sid,)).fetchone()
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
            resp = await client.post(f"/api/knowledge/sources/{sid}/ingest-text",
                                     json={"text": "hello"})
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
    async def test_auto_added_source_is_tombstoned(self, store, monkeypatch):
        sid = store.add_source("auto", "local_folder", "/tmp/auto",
                               properties={kh.AUTO_ADDED_PROP: True})
        seen = {}

        def _cascade(source_id, dismiss_uri=None):
            seen["source_id"] = source_id
            seen["dismiss_uri"] = dismiss_uri

        monkeypatch.setattr(store, "delete_source_cascade", _cascade)
        async with _client(_make_app(store)) as client:
            resp = await client.delete(f"/api/knowledge/sources/{sid}")
            assert resp.status == 200
            assert (await resp.json())["status"] == "deleted"
        assert seen == {"source_id": sid, "dismiss_uri": "/tmp/auto"}

    @pytest.mark.asyncio
    async def test_hand_added_source_gets_no_tombstone(self, store, monkeypatch):
        sid = store.add_source("manual", "local_folder", "/tmp/manual")
        seen = {}
        monkeypatch.setattr(
            store, "delete_source_cascade",
            lambda source_id, dismiss_uri=None: seen.update(uri=dismiss_uri))
        async with _client(_make_app(store)) as client:
            assert (await client.delete(f"/api/knowledge/sources/{sid}")).status == 200
        assert seen == {"uri": None}

    @pytest.mark.asyncio
    async def test_unreadable_properties_do_not_block_the_delete(self, store,
                                                                 monkeypatch):
        sid = store.add_source("odd", "local_folder", "/tmp/odd")
        store.db.execute("UPDATE sources SET properties = ? WHERE id = ?",
                         ("{not json", sid))
        store.db.commit()
        monkeypatch.setattr(store, "delete_source_cascade",
                            lambda source_id, dismiss_uri=None: None)
        async with _client(_make_app(store)) as client:
            assert (await client.delete(f"/api/knowledge/sources/{sid}")).status == 200

    @pytest.mark.asyncio
    async def test_cascade_failure_is_500(self, store, monkeypatch):
        sid = store.add_source("s", "local_folder", "/tmp/s")

        def _boom(source_id, dismiss_uri=None):
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
            assert (await resp.json())["error"] == "pipeline not configured"

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
    async def test_success_marks_source_synced_and_removes_temp_file(self, store,
                                                                     monkeypatch):
        sid = store.add_source("s", "web", "https://example.com")
        monkeypatch.setattr(f"{MODULE}.fetch_url_content",
                            AsyncMock(return_value="fetched body"))
        pipeline = MagicMock(ingest_file=AsyncMock(return_value="j1"))
        await kh._background_agent_sync(sid, "https://example.com", "S", store,
                                        pipeline, MagicMock())
        row = store.db.execute("SELECT sync_status FROM sources WHERE id = ?",
                               (sid,)).fetchone()
        assert row["sync_status"] == "synced"
        assert not Path(pipeline.ingest_file.await_args.args[0]).exists()

    @pytest.mark.asyncio
    async def test_fetch_failure_marks_source_error(self, store, monkeypatch):
        sid = store.add_source("s", "web", "https://example.com")
        monkeypatch.setattr(f"{MODULE}.fetch_url_content",
                            AsyncMock(side_effect=RuntimeError("offline")))
        await kh._background_agent_sync(sid, "https://example.com", "S", store,
                                        MagicMock(), MagicMock())
        row = store.db.execute("SELECT sync_status FROM sources WHERE id = ?",
                               (sid,)).fetchone()
        assert row["sync_status"] == "error"


# --------------------------------------------------------- startup / wiring


class TestSelLog:
    def test_emits_a_namespaced_tool_event(self, monkeypatch):
        recorded = {}
        monkeypatch.setattr(f"{MODULE}.sel",
                            lambda: MagicMock(log_tool_invocation=lambda **kw:
                                              recorded.update(kw)))
        kh._sel_log("item.update", item_id="i1")
        assert recorded["tool_name"] == "knowledge.item.update"
        assert recorded["outcome"] == "completed"
        assert "i1" in recorded["resources"]

    def test_outcome_can_be_overridden_and_leaves_resources_empty(self, monkeypatch):
        recorded = {}
        monkeypatch.setattr(f"{MODULE}.sel",
                            lambda: MagicMock(log_tool_invocation=lambda **kw:
                                              recorded.update(kw)))
        kh._sel_log("batch_embed", outcome="cancelled")
        assert recorded["outcome"] == "cancelled"
        assert recorded["resources"] == ""


class TestCreateEmbedder:
    def test_reads_config_json_when_present(self, monkeypatch, tmp_path):
        (tmp_path / "config.json").write_text(json.dumps(
            {"knowledge": {"embed_content_budget": 77}}))
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
    async def test_stops_previous_watcher_and_registers_the_new_one(self, store,
                                                                    monkeypatch):
        old = MagicMock(stop=AsyncMock())
        started = asyncio.Event()

        class _FakeWatcher:
            def __init__(self, *, store, pipeline, project_dirs):
                self.project_dirs = project_dirs

            async def start(self):
                started.set()

            async def stop(self):
                return None

        monkeypatch.setattr(f"{MODULE}.KnowledgeWatcher", _FakeWatcher)
        monkeypatch.setattr(f"{MODULE}._slot_project_snapshot", lambda _s: ["/proj"])

        app = _make_app(store, pipeline=MagicMock(), watcher=old)
        await kh._start_watcher_async(app)
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            old.stop.assert_awaited_once()
            assert isinstance(app["knowledge_watcher"], _FakeWatcher)
            # The dirs callback reads live chat-slot projects, not recents.
            assert app["knowledge_watcher"].project_dirs() == ["/proj"]
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
        monkeypatch.setattr(f"{MODULE}.KiroCrewConfig.load",
                            staticmethod(lambda: _cfg(auto_ingest=False)))
        app = _make_app(store, pipeline=MagicMock())
        await kh._start_artifact_ingest_async(app)
        assert "artifact_knowledge_sync" not in app

    @pytest.mark.asyncio
    async def test_enabled_registers_change_listener_and_starts(self, store,
                                                                monkeypatch):
        monkeypatch.setattr(
            f"{MODULE}.KiroCrewConfig.load",
            staticmethod(lambda: _cfg(auto_ingest=True, kinds=("webapp",))))
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
    async def test_builds_pipeline_connectors_and_routes(self, store, monkeypatch,
                                                         tmp_path):
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

            paths = {r.resource.canonical for r in app.router.routes()
                     if r.resource is not None}
            for expected in ("/api/knowledge/items", "/api/knowledge/stats",
                             "/api/knowledge/search-for-context",
                             "/api/knowledge/embedding/generate",
                             "/api/knowledge/agent-document"):
                assert expected in paths
        finally:
            for callback in list(app.on_cleanup):
                await callback(app)

    @pytest.mark.asyncio
    async def test_second_call_keeps_the_existing_pipeline(self, store, monkeypatch,
                                                           tmp_path):
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
