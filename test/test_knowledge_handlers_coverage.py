"""Coverage tests for the Knowledge Library dashboard handlers.

Targets the read-side item/entity/graph endpoints, export/import, the embedding
endpoints and their background rebuild job, the chat-context search endpoint,
the agent-document route, agent ingest/sync, and the route-registration entry
point -- all of which were unexercised by the existing knowledge test modules.

Harness matches ``test_knowledge_add_source.py`` / ``test_folder_watch_handlers.py``:
a real :class:`KnowledgeStore` on a ``tmp_path`` sqlite file plus a minimal
``web.Application`` carrying only the app keys each handler reads.

This file keeps the namespace, item, entity, graph, stats and job endpoints,
export/import, and the embedding endpoints with their rebuild job. The rest is in
sibling files: ``test_knowledge_handlers_coverage_context_search.py`` (the
chat-context search endpoint), ``test_knowledge_handlers_coverage_source_writes.py``
(the agent-document route, ingest-text, source delete, and the URL-fetch sync branch
with its background task) and ``test_knowledge_handlers_coverage_module_wide.py``
(SEL logging, embedder construction, the startup hooks, route registration and the
JSON object body guard). The store fixture, app builder and the other pieces these
files share live in ``knowledge_handlers_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from knowledge_handlers_coverage_helpers import (  # noqa: F401 -- the fixture is requested by name
    MODULE,
    _client,
    _FakeEmbedder,
    _make_app,
    store_fixture,
)

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.testing.wait import await_until


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


def _malformed_cases(old_test_name: str, bundles: list) -> list:
    return [
        pytest.param(bundle, id=f"{old_test_name}-bundle{i}") for i, bundle in enumerate(bundles)
    ]


# Each group reaches the import handler by a different path; all must answer
# 400 malformed_knowledge_bundle rather than a 500.
_MALFORMED_BUNDLE_CASES = [
    # A well-formed top-level dict whose collection isn't a list-of-objects
    # still reaches the redaction loop's item.get(...) on a non-dict entry.
    *_malformed_cases(
        "test_non_object_collection_items_is_400_not_500",
        [
            {"items": [1]},
            {"items": "not-a-list"},
            {"entities": [None]},
            {"relations": [["nested"]]},
        ],
    ),
    # sources/source_locations/mentions are never touched by the handler's
    # redaction loops (only items/entities/relations are), so a non-dict
    # entry there reaches store.import_bundle() directly -- e.g. src["id"]
    # on an int raises TypeError, which is neither a KeyError nor a
    # sqlite3.Error.
    *_malformed_cases(
        "test_non_object_source_collections_is_400_not_500",
        [
            {"sources": [1]},
            {"source_locations": ["x"]},
            {"mentions": [42]},
        ],
    ),
    # Every field the redaction loops pass to _redact() has to be a
    # string or null -- _redact() forwards non-empty values straight into
    # a regex .finditer() call, which raises TypeError on anything else.
    *_malformed_cases(
        "test_non_string_redacted_field_is_400_not_500",
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
    ),
]


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
        # The store's writer-side invariant rejects this value
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
            source_store._close_all_for_tests()

        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 200
            result = await resp.json()
        assert result["items_imported"] == 1
        assert store.get_item(item_id) is not None

    @pytest.mark.asyncio
    async def test_bundle_violating_foreign_keys_is_400_not_500(self, store):
        # The source IS declared, so the store resolves the reference and the row
        # reaches the insert; its ITEM is what no bundle carries, which is the
        # foreign key this arm exists for. An undeclared source is refused earlier,
        # by the store's own typed rejection, and is covered separately.
        bundle = {
            "sources": [
                {
                    "id": "s1",
                    "name": "A",
                    "source_type": "local_file",
                    "uri": "/a.md",
                    "created_at": "2024-01-01T00:00:00",
                }
            ],
            "source_locations": [{"id": "sl1", "item_id": "missing-item", "source_id": "s1"}],
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
    @pytest.mark.parametrize("bundle", _MALFORMED_BUNDLE_CASES)
    async def test_malformed_bundle_is_400_not_500(self, store, bundle):
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
        # The source is declared so the reference resolves and the row reaches the
        # insert; the missing ITEM is what trips the foreign key, which is the arm
        # whose driver text must not escape.
        bundle = {
            "sources": [
                {
                    "id": "s1",
                    "name": "A",
                    "source_type": "local_file",
                    "uri": "/a.md",
                    "created_at": "2024-01-01T00:00:00",
                }
            ],
            "source_locations": [{"id": "sl1", "item_id": "missing-item", "source_id": "s1"}],
        }
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 400
            body = await resp.json()
        assert body["error"] == "malformed bundle"
        assert "FOREIGN KEY" not in body["error"]
        assert "constraint" not in body["error"].lower()

    @pytest.mark.asyncio
    async def test_the_withheld_account_is_redacted_in_the_response(self, store):
        """A document key is BUNDLE-authored text on an HTTP path. `slug` is exempt
        from the inbound redaction loop on purpose -- it lands in a PRIMARY KEY and has
        to be the real text -- and it carries no length bound, so the account naming it
        is where bundle text would otherwise reach a reader unfiltered."""
        bundle = {
            "sources": [
                {
                    "id": "s1",
                    "name": "Auto-added",
                    "source_type": "agent",
                    "uri": "agent://",
                    "created_at": "2024-01-01T00:00:00",
                }
            ],
            "agent_item_state": [
                {
                    "source_id": "s1",
                    "slug": "doc-AKIAIOSFODNN7EXAMPLE",
                    "content_hash": "h",
                    "item_ids": json.dumps(["absent-AKIAIOSFODNN7EXAMPLE"]),
                    "updated_at": "2024-01-01T00:00:00",
                    "name": "N",
                }
            ],
        }
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 200
            result = await resp.json()

        named = [
            w for w in result["withheld"] if w.get("reason") == "ownership_row_names_absent_items"
        ]
        assert named, f"fixture must produce a withheld entry: {result['withheld']}"
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(
            result
        ), "bundle-authored credential text reached the import response"
        assert "REDACTED" in named[0]["key"]
        assert "REDACTED" in named[0]["items"]

    @pytest.mark.asyncio
    async def test_an_undeclared_source_entry_is_redacted_in_the_response(self, store):
        """The account entry for a state row whose source the bundle never declares
        carries that source id and the document key in the bundle's own words."""
        bundle = {
            "sources": [
                {
                    "id": "s1",
                    "name": "Auto-added",
                    "source_type": "agent",
                    "uri": "agent://",
                    "created_at": "2024-01-01T00:00:00",
                }
            ],
            "items": [
                {
                    "id": "i1",
                    "title": "T",
                    "content": "body",
                    "item_type": "document",
                    "source_id": "s1",
                }
            ],
            "agent_item_state": [
                {
                    "source_id": "src-AKIAIOSFODNN7EXAMPLE",
                    "slug": "doc-AKIAIOSFODNN7EXAMPLE",
                    "content_hash": "h",
                    "item_ids": json.dumps(["i1"]),
                    "updated_at": "2024-01-01T00:00:00",
                    "name": "N",
                }
            ],
        }
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 200
            result = await resp.json()

        named = [
            w for w in result["withheld"] if w.get("reason") == "ownership_row_source_undeclared"
        ]
        assert named, f"fixture must produce a withheld entry: {result['withheld']}"
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(
            result
        ), "bundle-authored credential text reached the import response"
        assert "REDACTED" in named[0]["source_id"]
        assert "REDACTED" in named[0]["key"]

    @pytest.mark.asyncio
    async def test_the_stored_document_key_stays_byte_exact(self, store):
        """The control on the other side: redaction belongs to the RESPONSE. The store
        matches this key against a PRIMARY KEY, so a redacted one would not find its own
        document."""
        slug = "doc-AKIAIOSFODNN7EXAMPLE"
        bundle = {
            "sources": [
                {
                    "id": "s1",
                    "name": "Auto-added",
                    "source_type": "agent",
                    "uri": "agent://",
                    "created_at": "2024-01-01T00:00:00",
                }
            ],
            "items": [
                {
                    "id": "i1",
                    "title": "T",
                    "content": "body",
                    "item_type": "document",
                    "source_id": "s1",
                }
            ],
            "agent_item_state": [
                {
                    "source_id": "s1",
                    "slug": slug,
                    "content_hash": "h",
                    "item_ids": json.dumps(["i1"]),
                    "updated_at": "2024-01-01T00:00:00",
                    "name": "N",
                }
            ],
        }
        async with _client(_make_app(store)) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 200
            result = await resp.json()

        assert result["ownership_rows_imported"] == 1
        assert result["withheld"] == []
        row = store.db.execute("SELECT slug FROM agent_item_state").fetchone()
        assert row["slug"] == slug, "the stored key was redacted; it must stay byte-exact"


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

    @pytest.mark.asyncio
    async def test_status_takes_no_db_connection_on_the_loop(self, store, monkeypatch):
        """The dashboard polls this endpoint while open; the COUNTs must run
        in a worker thread. On the loop, a contended knowledge DB busy-waits
        every task (watchdog heartbeat included) for the connection's whole
        busy timeout. Strict mode turns an on-loop take into a raise,
        which aiohttp surfaces as a 500."""
        await asyncio.to_thread(store.add_item, "a", "body", "note")
        monkeypatch.setenv("KIROCREW_STRICT_ON_LOOP_STORE", "1")
        async with _client(_make_app(store)) as client:
            resp = await client.get("/api/knowledge/embedding/status")
            assert resp.status == 200
            data = await resp.json()
        assert (data["total_items"], data["embedded_items"]) == (1, 0)


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
            await await_until(ran, what="the rebuild job running")
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
