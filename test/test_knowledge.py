"""Unit tests for the Knowledge Library (store, chunker, readers, extractor, retrieval)."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from kiro_crew.knowledge import readers
from kiro_crew.knowledge.chunker import HeadingAwareChunker
from kiro_crew.knowledge.extractor import EntityExtractor
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.retrieval import HybridRetriever, _bytes_to_floats
from kiro_crew.knowledge.store import KnowledgeStore, SimpleDiGraph

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture()
def store_factory(tmp_path):
    """Return a callable that creates a new store at a given path."""
    stores = []

    def _make(name="test.db"):
        s = KnowledgeStore(str(tmp_path / name))
        stores.append(s)
        return s

    yield _make
    for s in stores:
        s.close()


# ---------------------------------------------------------------------------
# 1. KnowledgeStore
# ---------------------------------------------------------------------------

class TestKnowledgeStore:
    def test_create_and_get_item(self, store):
        item_id = store.add_item("Auth Design", "JWT tokens with 1h expiry", "design_doc",
                                 summary="Auth overview", tags=["auth", "jwt"])
        item = store.get_item(item_id)
        assert item is not None
        assert item["title"] == "Auth Design"
        assert item["content"] == "JWT tokens with 1h expiry"
        assert item["item_type"] == "design_doc"
        assert item["summary"] == "Auth overview"
        assert json.loads(item["tags"]) == ["auth", "jwt"]

    def test_fts_search(self, store):
        store.add_item("Auth Design", "JWT tokens with refresh flow", "design_doc")
        store.add_item("Database Schema", "DynamoDB table layout", "design_doc")
        results = store.search_items_fts("JWT")
        assert len(results) >= 1
        assert results[0]["title"] == "Auth Design"

    def test_add_entity_and_relation(self, store):
        e1 = store.add_entity("AuthService", "service", description="Handles auth")
        e2 = store.add_entity("DynamoDB", "technology", description="NoSQL DB")
        rid = store.add_entity_relation(e1, e2, "uses", description="Stores tokens")
        assert rid is not None
        assert store.graph.has_edge(e1, e2)
        edge = store.graph.edges[e1, e2]
        assert edge["relation_type"] == "uses"

    def test_entity_subgraph(self, store):
        e1 = store.add_entity("ServiceA", "service")
        e2 = store.add_entity("ServiceB", "service")
        e3 = store.add_entity("Database", "technology")
        store.add_entity_relation(e1, e2, "calls")
        store.add_entity_relation(e2, e3, "uses")
        sg = store.get_entity_subgraph(e1, depth=2)
        node_ids = {n["id"] for n in sg["nodes"]}
        assert e1 in node_ids
        assert e2 in node_ids
        assert e3 in node_ids
        assert len(sg["edges"]) == 2
        # Verify D3.js format: nodes have id/name/type, edges have source/target/type
        for n in sg["nodes"]:
            assert "id" in n and "name" in n and "type" in n
        for e in sg["edges"]:
            assert "source" in e and "target" in e and "type" in e

    def test_export_import_roundtrip(self, store_factory):
        s1 = store_factory("export.db")
        s1.add_item("Doc A", "Content A", "design_doc")
        s1.add_item("Doc B", "Content B", "runbook")
        s1.add_entity("SvcX", "service")
        bundle = s1.export_all()
        assert len(bundle["items"]) == 2
        assert len(bundle["entities"]) == 1

        s2 = store_factory("import.db")
        result = s2.import_bundle(bundle)
        assert result["items_imported"] == 2
        assert result["entities_created"] == 1
        stats = s2.get_stats()
        assert stats["items"] == 2
        assert stats["entities"] == 1

    def test_delete_item(self, store):
        item_id = store.add_item("Temp Doc", "Will be deleted", "personal_notes")
        assert store.get_item(item_id) is not None
        store.delete_item(item_id)
        assert store.get_item(item_id) is None
        # FTS should also be clean
        assert store.search_items_fts("deleted") == []

    def test_find_entity_case_insensitive(self, store):
        store.add_entity("DynamoDB", "technology")
        found = store.find_entity("dynamodb")
        assert found is not None
        assert found["name"] == "DynamoDB"

    def test_merge_entities(self, store):
        e_keep = store.add_entity("AuthService", "service")
        e_merge = store.add_entity("Auth Service", "service")
        e_other = store.add_entity("Database", "technology")
        store.add_entity_relation(e_merge, e_other, "uses")
        item_id = store.add_item("Doc", "content", "design_doc")
        store.add_mention(item_id, e_merge)

        store.merge_entities(e_keep, e_merge)

        # Merged entity should be gone
        assert store.find_entity("Auth Service") is None
        # Relation should point to kept entity
        rels = store.db.execute(
            "SELECT * FROM entity_relations WHERE source_id = ?", (e_keep,)
        ).fetchall()
        assert len(rels) == 1
        assert rels[0]["target_id"] == e_other
        # Mention should reference kept entity
        mentions = store.db.execute(
            "SELECT * FROM mentions WHERE entity_id = ?", (e_keep,)
        ).fetchall()
        assert len(mentions) == 1


# ---------------------------------------------------------------------------
# 2. HeadingAwareChunker
# ---------------------------------------------------------------------------

class TestHeadingAwareChunker:
    def test_chunk_markdown(self):
        text = "# Introduction\nThis is the intro paragraph.\n\n# Details\nHere are the details."
        chunker = HeadingAwareChunker(target_size=10)  # Very small to force split
        chunks = chunker.chunk(text)
        assert len(chunks) >= 2
        for c in chunks:
            assert "line_start" in c and "line_end" in c
            assert "content" in c
            assert c["chunk_index"] >= 0

    def test_chunk_code(self):
        code = "import os\n\ndef foo():\n    return 1\n\ndef bar():\n    return 2\n"
        chunker = HeadingAwareChunker(target_size=500)
        chunks = chunker.chunk_code(code, language="python")
        assert len(chunks) >= 1
        # All code should be present across chunks
        combined = "\n".join(c["content"] for c in chunks)
        assert "def foo():" in combined
        assert "def bar():" in combined
        for c in chunks:
            assert "line_start" in c and "line_end" in c

    def test_small_text_single_chunk(self):
        text = "Just a short note."
        chunker = HeadingAwareChunker(target_size=500)
        chunks = chunker.chunk(text)
        assert len(chunks) == 1
        assert chunks[0]["content"] == text


# ---------------------------------------------------------------------------
# 3. FileReader
# ---------------------------------------------------------------------------

class TestFileReader:
    def test_read_markdown(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text("# Hello\nWorld", encoding="utf-8")
        reader = FileReader()
        text, meta = reader.read(str(md))
        assert "# Hello" in text
        assert "World" in text
        assert meta["format"] == "md"
        assert meta["title"] == "test"
        assert meta["line_count"] == 2

    def test_read_unsupported(self, tmp_path):
        f = tmp_path / "data.xyz"
        f.write_text("binary-ish", encoding="utf-8")
        reader = FileReader()
        # Unsupported extension still falls through to _read_text
        text, meta = reader.read(str(f))
        assert "binary-ish" in text

    def test_supported_formats(self):
        reader = FileReader()
        for ext in ('.md', '.txt', '.py', '.html', '.json', '.yaml', '.csv'):
            assert ext in reader.SUPPORTED, f"{ext} missing from SUPPORTED"


def _make_pdf(text: str = "Hello PDF regression") -> bytes:
    """Build a structurally valid single-page PDF with a text object.

    Kept minimal but complete (xref table + trailer) so pdfminer/pdfplumber
    parse it deterministically -- no external PDF writer needed.
    """
    import io

    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
    ]
    stream = ("BT /F1 24 Tf 72 700 Td (%s) Tj ET" % text).encode()
    objs.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (i, body))
    xref_pos = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objs) + 1))
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF"
        % (len(objs) + 1, xref_pos)
    )
    return out.getvalue()


class TestFileReaderPdf:
    """PDF ingestion regression coverage.

    Guards PDF folder ingestion was shipped (readers.py routes
    ``.pdf`` -> ``_read_pdf`` -> ``pdfplumber``) but ``pdfplumber`` was never
    declared as a runtime dependency, so the built env couldn't import it and
    every PDF degraded to the missing-dep sentinel. These tests fail loudly if
    the runtime dependency goes missing again.
    """

    def test_pdf_extension_supported_and_dispatched(self):
        reader = FileReader()
        assert '.pdf' in reader.SUPPORTED
        assert reader._DISPATCH.get('.pdf') == '_read_pdf'

    def test_pdfplumber_runtime_dep_present(self):
        # The optional import in readers.py must succeed in the built env.
        # If this fails, 'pdfplumber' is missing from setup.cfg install_requires.
        assert readers.pdfplumber is not None, (
            "pdfplumber import failed -- declare 'pdfplumber' in setup.cfg "
            "install_requires"
        )

    def test_read_pdf_extracts_text(self, tmp_path):
        p = tmp_path / "doc.pdf"
        p.write_bytes(_make_pdf("Hello PDF regression"))
        reader = FileReader()
        text, meta = reader.read(str(p))
        assert "Hello PDF regression" in text
        assert meta["format"] == "pdf"
        assert meta["page_count"] == 1

    def test_read_pdf_does_not_hit_missing_dep_guard(self, tmp_path):
        # A malformed PDF must surface a real parse error, never the
        # 'PDF support requires pdfplumber' sentinel (which only fires when
        # the runtime dependency is absent).
        p = tmp_path / "bad.pdf"
        p.write_bytes(b"%PDF-1.0\nnot a real pdf\n%%EOF")
        reader = FileReader()
        text, meta = reader.read(str(p))
        assert "PDF support requires pdfplumber" not in text
        assert meta.get("error") != "PDF support requires pdfplumber"


# ---------------------------------------------------------------------------
# 4. EntityExtractor
# ---------------------------------------------------------------------------

class TestEntityExtractor:
    def test_extract_no_agent(self):
        import asyncio
        ext = EntityExtractor(pool=None)
        result = asyncio.get_event_loop().run_until_complete(ext.extract("some text"))
        assert result == {"title": "", "entities": [], "relations": [], "category": "document", "summary": ""}

    def test_parse_json_response(self):
        ext = EntityExtractor()
        raw = json.dumps({
            "entities": [{"name": "Svc", "type": "service", "description": "A service"}],
            "relations": [],
            "category": "design_doc",
            "summary": "A service doc."
        })
        result = ext._parse_response(raw)
        assert len(result["entities"]) == 1
        assert result["category"] == "design_doc"

    def test_parse_code_block_response(self):
        ext = EntityExtractor()
        raw = '```json\n{"entities": [], "relations": [], "category": "runbook", "summary": "ops"}\n```'
        result = ext._parse_response(raw)
        assert result["category"] == "runbook"
        assert result["summary"] == "ops"


# ---------------------------------------------------------------------------
# 5. HybridRetriever
# ---------------------------------------------------------------------------

class TestHybridRetriever:
    def test_keyword_search(self, store):
        store.add_item("Auth Design", "JWT tokens with refresh flow", "design_doc")
        store.add_item("DB Schema", "DynamoDB table layout", "design_doc")
        retriever = HybridRetriever(store)
        results = retriever.search("JWT")
        assert len(results) >= 1
        assert results[0]["title"] == "Auth Design"
        assert "keyword" in results[0]["match_type"]

    def test_rrf_fuse(self):
        list_a = [("item1", 1), ("item2", 2), ("item3", 3)]
        list_b = [("item2", 1), ("item3", 2), ("item4", 3)]
        fused = HybridRetriever._rrf_fuse(list_a, list_b, None, k=60)
        ids = [item_id for item_id, _ in fused]
        # item2 appears in both lists at good ranks, should be top
        assert ids[0] == "item2"
        # All 4 items should be present
        assert set(ids) == {"item1", "item2", "item3", "item4"}

    # --- Recall for natural-language queries ---

    def test_sanitize_strips_stopwords_and_or_joins(self):
        out = HybridRetriever._sanitize_fts5_query("VoC related to Budget Planning")
        assert " OR " in out
        # connective stopwords dropped...
        assert '"related"' not in out and '"to"' not in out
        # ...content tokens retained and individually quoted
        assert '"VoC"' in out and '"Budget"' in out and '"Planning"' in out

    def test_sanitize_all_stopwords_falls_back(self):
        # A query of only stopwords must not collapse to an empty match.
        out = HybridRetriever._sanitize_fts5_query("the and of")
        assert out != ""

    def test_sanitize_is_injection_safe(self):
        # Each token stays double-quoted with internal quotes doubled, so user
        # input cannot inject FTS5 operators (BSC1 Input Validation invariant).
        out = HybridRetriever._sanitize_fts5_query('foo" bar')
        assert '"foo"""' in out
        assert '"bar"' in out

    def test_keyword_search_or_recall(self, store):
        # Natural-language query whose connective tokens ("related","to") the
        # target doc lacks. Old implicit-AND required every literal token -> 0
        # hits; the OR-match recovers the relevant item.
        store.add_item("Budget Planning VoC", "voice of customer budget planning notes", "doc")
        store.add_item("Unrelated", "something entirely about widgets", "doc")
        retriever = HybridRetriever(store)
        results = retriever.search("VoC related to Budget Planning")
        assert "Budget Planning VoC" in [r["title"] for r in results]

    def test_rrf_fuse_vector_weight(self):
        # A vector-only hit and a keyword-only hit at the same rank: the
        # weighted vector leg must score higher.
        kw = [("kw_item", 1)]
        vec = [("vec_item", 1)]
        fused = HybridRetriever._rrf_fuse(kw, [], vec, weights=(1.0, 1.0, 2.0))
        ranked = dict(fused)
        assert ranked["vec_item"] > ranked["kw_item"]

    # --- Citation metadata surfacing ---

    def test_search_attaches_source_location(self, store):
        # A result for an item that has a source_locations row carries the
        # section + line range so callers can cite it.
        sid = store.add_source("auth.md", "local_file", "/docs/auth.md")
        item_id = store.add_item(
            "Auth Design", "JWT tokens with refresh flow", "design_doc", source_id=sid
        )
        store.add_source_location(
            item_id, sid, chunk_range="10-25", section_title="Token Lifecycle"
        )
        retriever = HybridRetriever(store)
        results = retriever.search("JWT")
        top = next(r for r in results if r["id"] == item_id)
        assert top["section_title"] == "Token Lifecycle"
        assert top["chunk_range"] == "10-25"

    def test_search_omits_location_when_absent(self, store):
        # An item with no source_locations row degrades cleanly -- the citation
        # keys are simply absent, not None placeholders.
        store.add_item("DB Schema", "DynamoDB table layout", "design_doc")
        retriever = HybridRetriever(store)
        results = retriever.search("DynamoDB")
        assert results
        assert "section_title" not in results[0]
        assert "chunk_range" not in results[0]

    def test_search_attaches_folder_file_path(self, store):
        # A folder/vault result carries source_type/source_name and the specific
        # file path (from folder_file_state), not just the folder-root uri.
        sid = store.add_source("Opportunity Planner", "local_folder", "/home/nrb/op/src/")
        item_id = store.add_item(
            "Auth Design", "JWT tokens with refresh flow", "design_doc", source_id=sid
        )
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, item_ids, last_seen, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, "/home/nrb/op/src/auth.md", json.dumps([item_id]), "now", "done"),
        )
        retriever = HybridRetriever(store)
        results = retriever.search("JWT")
        top = next(r for r in results if r["id"] == item_id)
        assert top["source_type"] == "local_folder"
        assert top["source_name"] == "Opportunity Planner"
        assert top["file_path"] == "/home/nrb/op/src/auth.md"

    def test_search_attaches_artifact_slug(self, store):
        # An artifact result carries the artifact slug + name (from
        # artifact_item_state) for a /artifacts/<slug> citation.
        sid = store.add_source("Artifacts", "artifact", "artifact://aggregate")
        item_id = store.add_item(
            "OP Vision", "vision content goes here", "document", source_id=sid
        )
        store.db.execute(
            "INSERT INTO artifact_item_state (source_id, slug, item_ids, updated_at, name) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, "op-vision", json.dumps([item_id]), "now", "OP Vision Plan"),
        )
        retriever = HybridRetriever(store)
        results = retriever.search("vision")
        top = next(r for r in results if r["id"] == item_id)
        assert top["source_type"] == "artifact"
        assert top["artifact_slug"] == "op-vision"
        assert top["artifact_name"] == "OP Vision Plan"


# ---------------------------------------------------------------------------
# 6. SimpleDiGraph
# ---------------------------------------------------------------------------


class TestSimpleDiGraph:
    def test_add_node_and_has_node(self):
        g = SimpleDiGraph()
        g.add_node("a", name="A")
        assert g.has_node("a")
        assert not g.has_node("b")

    def test_add_edge_and_has_edge(self):
        g = SimpleDiGraph()
        g.add_node("a")
        g.add_node("b")
        g.add_edge("a", "b", weight=1.0)
        assert g.has_edge("a", "b")
        assert not g.has_edge("b", "a")

    def test_successors_predecessors(self):
        g = SimpleDiGraph()
        g.add_node("a")
        g.add_node("b")
        g.add_node("c")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        assert set(g.successors("a")) == {"b", "c"}
        assert set(g.predecessors("b")) == {"a"}
        assert list(g.successors("c")) == []

    def test_degree(self):
        g = SimpleDiGraph()
        g.add_node("a")
        g.add_node("b")
        g.add_node("c")
        g.add_edge("a", "b")
        g.add_edge("c", "a")
        assert g.degree("a") == 2  # 1 outgoing + 1 incoming
        assert g.degree("b") == 1

    def test_nodes_iteration_and_subscript(self):
        g = SimpleDiGraph()
        g.add_node("x", name="X", entity_type="svc")
        g.add_node("y", name="Y", entity_type="db")
        assert set(g.nodes) == {"x", "y"}
        assert g.nodes["x"]["name"] == "X"
        assert "x" in g.nodes
        assert len(g.nodes) == 2

    def test_edges_iteration_and_subscript(self):
        g = SimpleDiGraph()
        g.add_edge("a", "b", relation_type="calls")
        edges = list(g.edges(data=True))
        assert len(edges) == 1
        assert edges[0] == ("a", "b", {"relation_type": "calls"})
        assert g.edges["a", "b"]["relation_type"] == "calls"

    def test_clear(self):
        g = SimpleDiGraph()
        g.add_node("a")
        g.add_edge("a", "b")
        g.clear()
        assert not g.has_node("a")
        assert not g.has_edge("a", "b")
        assert list(g.nodes) == []


# ---------------------------------------------------------------------------
# 7. KnowledgeStore -- additional coverage
# ---------------------------------------------------------------------------


class TestKnowledgeStoreExtended:
    def test_update_item_fts_sync(self, store):
        item_id = store.add_item("Original", "old content about cats", "doc")
        assert len(store.search_items_fts("cats")) == 1
        store.update_item(item_id, title="Updated", content="new content about dogs")
        # After update, new content should be searchable
        assert len(store.search_items_fts("dogs")) == 1
        item = store.get_item(item_id)
        assert item["title"] == "Updated"
        assert item["content"] == "new content about dogs"

    def test_update_item_no_fields(self, store):
        item_id = store.add_item("Doc", "content", "doc")
        store.update_item(item_id)  # no-op, should not crash

    def test_update_item_non_fts_field(self, store):
        item_id = store.add_item("Doc", "content", "doc")
        store.update_item(item_id, status="archived")
        assert store.get_item(item_id)["status"] == "archived"

    def test_get_item_missing(self, store):
        assert store.get_item("nonexistent") is None

    def test_add_source_and_get_by_uri(self, store):
        sid = store.add_source("myfile", "local_file", "/tmp/test.md",
                               properties={"content_hash": "abc123"})
        found = store.get_source_by_uri("/tmp/test.md")
        assert found is not None
        assert found["id"] == sid
        assert store.get_source_by_uri("/tmp/nope") is None

    def test_update_source(self, store):
        sid = store.add_source("f", "local_file", "/tmp/f.md")
        store.update_source(sid, last_synced="2026-01-01T00:00:00")
        row = store.db.execute("SELECT last_synced FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["last_synced"] == "2026-01-01T00:00:00"

    def test_update_source_no_fields(self, store):
        sid = store.add_source("f", "local_file", "/tmp/f2.md")
        store.update_source(sid)  # no-op

    def test_add_source_location(self, store):
        sid = store.add_source("f", "local_file", "/tmp/loc.md")
        item_id = store.add_item("Doc", "content", "doc", source_id=sid)
        store.add_source_location(item_id, sid, chunk_range="0-10", section_title="Intro")
        rows = store.db.execute(
            "SELECT * FROM source_locations WHERE item_id = ?", (item_id,)).fetchall()
        assert len(rows) == 1
        assert rows[0]["section_title"] == "Intro"

    def test_get_neighbors_depth(self, store):
        e1 = store.add_entity("A", "svc")
        e2 = store.add_entity("B", "svc")
        e3 = store.add_entity("C", "svc")
        store.add_entity_relation(e1, e2, "calls")
        store.add_entity_relation(e2, e3, "calls")
        # depth=1 should get B only
        n1 = store.get_neighbors(e1, depth=1)
        assert {n["id"] for n in n1} == {e2}
        # depth=2 should get B and C
        n2 = store.get_neighbors(e1, depth=2)
        assert {n["id"] for n in n2} == {e2, e3}

    def test_get_neighbors_bidirectional(self, store):
        e1 = store.add_entity("A", "svc")
        e2 = store.add_entity("B", "svc")
        store.add_entity_relation(e2, e1, "calls")
        # e1 has no outgoing but has incoming from e2
        neighbors = store.get_neighbors(e1, depth=1)
        assert {n["id"] for n in neighbors} == {e2}

    def test_find_entity_by_alias(self, store):
        store.add_entity("DynamoDB", "technology", aliases=["ddb", "dynamo"])
        found = store.find_entity("ddb")
        assert found is not None
        assert found["name"] == "DynamoDB"

    def test_find_entity_not_found(self, store):
        assert store.find_entity("nonexistent") is None

    def test_export_item_with_entities(self, store):
        sid = store.add_source("f", "local_file", "/tmp/exp.md")
        item_id = store.add_item("Doc", "content", "doc", source_id=sid)
        e1 = store.add_entity("Svc", "service")
        e2 = store.add_entity("DB", "technology")
        store.add_mention(item_id, e1)
        store.add_mention(item_id, e2)
        store.add_entity_relation(e1, e2, "uses", source_item_id=item_id)
        store.add_source_location(item_id, sid, section_title="Main")
        bundle = store.export_item(item_id)
        assert bundle["item"]["id"] == item_id
        assert len(bundle["entities"]) == 2
        assert len(bundle["relations"]) == 1
        assert len(bundle["source_locations"]) == 1

    def test_export_item_missing(self, store):
        assert store.export_item("nope") == {}

    def test_delete_item_cleans_mentions(self, store):
        item_id = store.add_item("Doc", "content", "doc")
        eid = store.add_entity("Svc", "service")
        store.add_mention(item_id, eid, context="test")
        sid = store.add_source("f", "local_file", "/tmp/del.md")
        store.add_source_location(item_id, sid)
        store.delete_item(item_id)
        assert store.db.execute("SELECT * FROM mentions WHERE item_id = ?", (item_id,)).fetchone() is None
        assert store.db.execute("SELECT * FROM source_locations WHERE item_id = ?", (item_id,)).fetchone() is None

    def test_get_stats(self, store):
        store.add_item("A", "a", "doc")
        store.add_entity("E", "svc")
        stats = store.get_stats()
        assert stats["items"] == 1
        assert stats["entities"] == 1
        assert stats["relations"] == 0
        assert stats["sources"] == 0

    def test_graph_has_node(self, store):
        eid = store.add_entity("Svc", "service")
        assert store.graph.has_node(eid)
        assert not store.graph.has_node("fake")

    def test_graph_degree(self, store):
        e1 = store.add_entity("A", "svc")
        e2 = store.add_entity("B", "svc")
        store.add_entity_relation(e1, e2, "calls")
        assert store.graph.degree(e1) == 1
        assert store.graph.degree(e2) == 1

    def test_load_graph_on_reopen(self, tmp_path):
        db_path = str(tmp_path / "reload.db")
        s1 = KnowledgeStore(db_path)
        e1 = s1.add_entity("A", "svc")
        e2 = s1.add_entity("B", "svc")
        s1.add_entity_relation(e1, e2, "calls")
        s1.close()
        s2 = KnowledgeStore(db_path)
        assert s2.graph.has_node(e1)
        assert s2.graph.has_edge(e1, e2)
        s2.close()


# ---------------------------------------------------------------------------
# 8. HybridRetriever -- additional coverage
# ---------------------------------------------------------------------------


class TestHybridRetrieverExtended:
    def test_graph_search(self, store):
        e1 = store.add_entity("JWT", "concept")
        item_id = store.add_item("Auth", "JWT token design", "doc")
        store.add_mention(item_id, e1)
        retriever = HybridRetriever(store)
        results = retriever._graph_search("JWT")
        assert len(results) >= 1
        assert results[0][0] == item_id

    def test_graph_search_no_match(self, store):
        retriever = HybridRetriever(store)
        assert retriever._graph_search("nonexistent") == []

    def test_graph_search_with_neighbors(self, store):
        e1 = store.add_entity("Auth", "service")
        e2 = store.add_entity("JWT", "concept")
        store.add_entity_relation(e1, e2, "uses")
        item_id = store.add_item("Token doc", "about tokens", "doc")
        store.add_mention(item_id, e2)
        retriever = HybridRetriever(store)
        results = retriever._graph_search("Auth")
        assert len(results) >= 1

    def test_vector_search_no_embedder(self, store):
        retriever = HybridRetriever(store, embedder=None)
        assert retriever._vector_search("query") is None

    def test_vector_search_with_embedder(self, store):
        emb = json.dumps([1.0, 0.0, 0.0])
        store.add_item("Vec Doc", "vector content", "doc", embedding=emb)
        retriever = HybridRetriever(store, embedder=lambda q: [1.0, 0.0, 0.0])
        results = retriever._vector_search("query")
        assert results is not None
        assert len(results) == 1

    def test_cosine_similarity_identical(self):
        assert HybridRetriever._cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)

    def test_cosine_similarity_orthogonal(self):
        assert HybridRetriever._cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_cosine_similarity_zero_vector(self):
        assert HybridRetriever._cosine_similarity([0, 0], [1, 1]) == 0.0

    def test_search_combined_match_types(self, store):
        e1 = store.add_entity("JWT", "concept")
        emb = json.dumps([1.0, 0.0])
        item_id = store.add_item("JWT Auth", "JWT token design", "doc", embedding=emb)
        store.add_mention(item_id, e1)
        retriever = HybridRetriever(store, embedder=lambda q: [1.0, 0.0])
        results = retriever.search("JWT")
        assert len(results) >= 1
        # Should have multiple match types
        mt = results[0]["match_type"]
        assert "keyword" in mt

    def test_search_graph_pair_terms(self, store):
        """Graph search tries consecutive word pairs."""
        e1 = store.add_entity("Auth Service", "service")
        item_id = store.add_item("Doc", "about auth service", "doc")
        store.add_mention(item_id, e1)
        retriever = HybridRetriever(store)
        results = retriever._graph_search("Auth Service details")
        assert len(results) >= 1

    def test_bytes_to_floats_valid(self):
        assert _bytes_to_floats(json.dumps([1.0, 2.0]).encode()) == [1.0, 2.0]

    def test_bytes_to_floats_empty(self):
        assert _bytes_to_floats(b"") == []
        assert _bytes_to_floats(None) == []

    def test_bytes_to_floats_invalid(self):
        assert _bytes_to_floats(b"not json") == []


# ---------------------------------------------------------------------------
# 9. EntityExtractor -- additional coverage
# ---------------------------------------------------------------------------


class TestEntityExtractorExtended:
    def test_extract_empty_text(self):
        import asyncio
        ext = EntityExtractor(pool=None)
        result = asyncio.get_event_loop().run_until_complete(ext.extract(""))
        assert result == {"title": "", "entities": [], "relations": [], "category": "document", "summary": ""}

    def test_extract_with_agent(self):
        import asyncio

        class MockPool:
            async def send(self, prompt, timeout=60.0):
                return json.dumps({
                    "entities": [{"name": "Svc", "type": "service", "description": "A"}],
                    "relations": [], "category": "design_doc", "summary": "test"
                })

            async def send_batch(self, prompts, timeout=60.0):
                return [await self.send(p, timeout) for p in prompts]

        ext = EntityExtractor(pool=MockPool())
        result = asyncio.get_event_loop().run_until_complete(ext.extract("some text"))
        assert result["category"] == "design_doc"
        assert len(result["entities"]) == 1

    def test_extract_agent_exception(self):
        import asyncio

        class BadPool:
            async def send(self, prompt, timeout=60.0):
                raise RuntimeError("fail")

            async def send_batch(self, prompts, timeout=60.0):
                raise RuntimeError("fail")

        ext = EntityExtractor(pool=BadPool())
        result = asyncio.get_event_loop().run_until_complete(ext.extract("text"))
        assert result == {"title": "", "entities": [], "relations": [], "category": "document", "summary": ""}

    def test_parse_response_regex_fallback(self):
        ext = EntityExtractor()
        raw = 'Some preamble text {"entities": [], "relations": [], "category": "runbook", "summary": "ok"} trailing'
        result = ext._parse_response(raw)
        assert result["category"] == "runbook"

    def test_parse_response_garbage(self):
        ext = EntityExtractor()
        result = ext._parse_response("totally invalid garbage")
        assert result == {"title": "", "entities": [], "relations": [], "category": "document", "summary": ""}

    def test_extract_code_block(self):
        ext = EntityExtractor()
        assert ext._extract_code_block("no block here") is None
        result = ext._extract_code_block('```\n{"a": 1}\n```')
        assert result == '{"a": 1}'

    def test_validate_partial_data(self):
        ext = EntityExtractor()
        result = ext._validate({"category": "runbook"})
        assert result["entities"] == []
        assert result["relations"] == []
        assert result["summary"] == ""
        assert result["category"] == "runbook"


# ---------------------------------------------------------------------------
# 10. Chunker -- additional coverage
# ---------------------------------------------------------------------------


class TestChunkerExtended:
    def test_chunk_with_overlap(self):
        text = "# A\n" + " ".join(["word"] * 600) + "\n\n# B\n" + " ".join(["other"] * 100)
        chunker = HeadingAwareChunker(target_size=500, overlap=10)
        chunks = chunker.chunk(text)
        assert len(chunks) >= 2
        # Second chunk should contain overlap from first
        if len(chunks) > 1:
            assert chunks[1]["chunk_index"] == 1

    def test_chunk_slides(self):
        text = "## Slide 1: Intro\nHello world\n\n## Slide 2: Details\nMore info"
        chunker = HeadingAwareChunker()
        slides = chunker.chunk_slides(text)
        assert len(slides) == 2
        assert slides[0]["section_title"] == "Slide 1: Intro"
        assert "Hello world" in slides[0]["content"]

    def test_chunk_code_oversized(self):
        # Generate a single huge function
        lines = ["def big():"] + [f"    x = {i}" for i in range(1000)]
        code = "\n".join(lines)
        chunker = HeadingAwareChunker(target_size=50)
        chunks = chunker.chunk_code(code, language="python")
        assert len(chunks) > 1
        combined = "\n".join(c["content"] for c in chunks)
        assert "def big():" in combined

    def test_chunk_no_headings(self):
        text = "Just plain text without any headings at all."
        chunker = HeadingAwareChunker(target_size=500)
        chunks = chunker.chunk(text)
        assert len(chunks) == 1
        assert chunks[0]["section_title"] is None

    def test_small_overlap_does_not_duplicate_whole_prev_chunk(self):
        """Regression: overlap=1 passed the ``overlap > 0`` guard but
        ``int(1 / 1.3) == 0``, and ``prev_words[-0:]`` is ``prev_words[0:]`` — the
        ENTIRE previous chunk. So a small (but valid, user-configurable via a source's
        ``chunk_overlap`` property) overlap silently prepended the whole previous chunk
        to every subsequent chunk, duplicating content across the knowledge base.
        """
        words = " ".join(f"w{i}" for i in range(400))
        chunker = HeadingAwareChunker(target_size=50, overlap=1)
        chunks = chunker.chunk(words)
        assert len(chunks) >= 2  # must actually split to exercise the overlap path

        prev_word_count = len(chunks[0]["content"].split())
        # The overlap prefix is the first line of chunk[1] (joined with "\n" + content).
        overlap_prefix = chunks[1]["content"].split("\n", 1)[0]
        injected = len(overlap_prefix.split())
        # A tiny overlap must inject a tiny prefix — never (almost) the whole prev chunk.
        assert injected < prev_word_count, (
            f"overlap=1 injected {injected} words but previous chunk has "
            f"{prev_word_count} — the entire previous chunk was duplicated"
        )
        assert injected <= 2, f"overlap=1 should inject ~0-1 words, got {injected}"

    def test_zero_overlap_injects_nothing(self):
        words = " ".join(f"w{i}" for i in range(400))
        chunker = HeadingAwareChunker(target_size=50, overlap=0)
        chunks = chunker.chunk(words)
        assert len(chunks) >= 2
        # With overlap=0 chunk[1] has no injected prefix line from chunk[0].
        assert not chunks[1]["content"].startswith(chunks[0]["content"].split()[0] + " w")

    def test_large_overlap_still_works(self):
        # The fix must not change behavior for the normal/default overlap.
        words = " ".join(f"w{i}" for i in range(400))
        chunker = HeadingAwareChunker(target_size=50, overlap=200)
        chunks = chunker.chunk(words)
        assert len(chunks) >= 2
        overlap_prefix = chunks[1]["content"].split("\n", 1)[0]
        # int(200/1.3) = 153, capped by prev chunk length — a real, multi-word overlap.
        assert len(overlap_prefix.split()) >= 2


# ---------------------------------------------------------------------------
# 11. FileReader -- additional coverage
# ---------------------------------------------------------------------------


class TestFileReaderExtended:
    def test_read_html_without_html2text(self, tmp_path):
        """Test HTML reading (exercises html2text or regex fallback)."""
        html_file = tmp_path / "test.html"
        html_file.write_text("<html><body><p>Hello</p></body></html>")
        reader = FileReader()
        text, meta = reader.read(str(html_file))
        assert "Hello" in text

    def test_read_latin1_fallback(self, tmp_path):
        f = tmp_path / "latin.txt"
        f.write_bytes(b"caf\xe9")
        reader = FileReader()
        text, meta = reader.read(str(f))
        assert "caf" in text

    def test_read_json_file(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text('{"key": "value"}')
        reader = FileReader()
        text, meta = reader.read(str(f))
        assert '"key"' in text
        assert meta["format"] == "json"


class TestPysqlite3Fallback:
    """Verify modules fall back to stdlib sqlite3 when pysqlite3 is unavailable."""

    _MODULES = (
        "kiro_crew.knowledge.store",
        "kiro_crew.knowledge.retrieval",
        "kiro_crew.snapshot",
    )

    def _reload_without_pysqlite3(self, module_name: str):
        """Force-reimport a module with pysqlite3 blocked."""
        import sqlite3 as stdlib_sqlite3

        saved = sys.modules.pop("pysqlite3", None)
        for mod in list(sys.modules):
            if mod == module_name or mod.startswith(module_name + "."):
                sys.modules.pop(mod)

        sys.modules["pysqlite3"] = None  # type: ignore[assignment]
        try:
            mod = importlib.import_module(module_name)
            assert mod.sqlite3 is stdlib_sqlite3
        finally:
            del sys.modules["pysqlite3"]
            if saved is not None:
                sys.modules["pysqlite3"] = saved

    def test_store_falls_back_to_stdlib_sqlite3(self):
        self._reload_without_pysqlite3("kiro_crew.knowledge.store")

    def test_retrieval_falls_back_to_stdlib_sqlite3(self):
        self._reload_without_pysqlite3("kiro_crew.knowledge.retrieval")

    def test_snapshot_falls_back_to_stdlib_sqlite3(self):
        self._reload_without_pysqlite3("kiro_crew.snapshot")


# ---------------------------------------------------------------------------
# 12. chunk_markdown() -- heading-aware markdown chunking
# ---------------------------------------------------------------------------


class TestChunkMarkdown:
    def test_splits_on_headings(self):
        text = "# Intro\nParagraph one.\n\n## Details\n" + " ".join(["detail"] * 400) + "\n\n## Conclusion\n" + " ".join(["final"] * 400)
        chunker = HeadingAwareChunker(target_size=500)
        chunks = chunker.chunk_markdown(text)
        assert len(chunks) >= 2
        # All chunks have required fields
        for c in chunks:
            assert "content" in c
            assert "section_title" in c
            assert "chunk_index" in c
            assert "line_start" in c

    def test_preserves_markdown_formatting(self):
        text = "# Title\n\n**Bold text** and `code`.\n\n- List item 1\n- List item 2"
        chunker = HeadingAwareChunker(target_size=500)
        chunks = chunker.chunk_markdown(text)
        combined = "\n".join(c["content"] for c in chunks)
        assert "**Bold text**" in combined
        assert "`code`" in combined
        assert "- List item" in combined

    def test_no_headings_falls_back(self):
        text = "Just plain text without headings."
        chunker = HeadingAwareChunker(target_size=500)
        chunks = chunker.chunk_markdown(text)
        assert len(chunks) == 1
        assert chunks[0]["content"] == text

    def test_section_titles_extracted(self):
        text = "## Architecture\n" + " ".join(["arch"] * 300) + "\n\n## Security\n" + " ".join(["sec"] * 300)
        chunker = HeadingAwareChunker(target_size=500)
        chunks = chunker.chunk_markdown(text)
        titles = [c["section_title"] for c in chunks]
        assert "Architecture" in titles
        assert "Security" in titles

    def test_oversized_section_splits(self):
        text = "# Big Section\n" + " ".join(["word"] * 1000)
        chunker = HeadingAwareChunker(target_size=50)
        chunks = chunker.chunk_markdown(text)
        assert len(chunks) > 1


# ---------------------------------------------------------------------------
# 13. FileReader -- .docx content_type metadata
# ---------------------------------------------------------------------------


class TestDocxContentType:
    def test_docx_returns_content_type_markdown(self, tmp_path):
        """Verify _read_docx sets content_type: markdown in metadata."""
        try:
            from docx import Document
        except ImportError:
            pytest.skip("python-docx not installed")
        # Create a minimal .docx
        doc = Document()
        doc.add_heading("Test Heading", level=1)
        doc.add_paragraph("Some content here.")
        path = tmp_path / "test.docx"
        doc.save(str(path))

        reader = FileReader()
        text, meta = reader.read(str(path))
        assert meta.get("content_type") == "markdown"
        assert "# Test Heading" in text
        assert "Some content here." in text

    def test_docx_content_type_in_dispatch(self):
        """Verify .docx is in the dispatch table."""
        reader = FileReader()
        assert '.docx' in reader._DISPATCH


class TestCosineSimilarityDimensionMismatch:
    """Regression: HybridRetriever._cosine_similarity must treat vectors of
    different dimensionality as incomparable (return 0.0), not silently truncate.

    The query vector is freshly embedded while the item vector is read from the DB,
    so a change in embedding dimensionality between ingestion and query yields
    mismatched lengths. With a plain ``zip(a, b)`` the dot product silently
    truncates to the shorter length while the norms still use the full vectors,
    producing a meaningless (often falsely high) similarity. The sibling code in
    ``vector_memory.py`` already guards this exact case (``if n_floats != q_len:
    continue``); this helper must not score across mismatched dimensions either.
    """

    def test_mismatched_dims_return_zero_not_false_match(self):
        # 8-dim query vs 4-dim stored item that happens to equal the query's prefix.
        # Truncating zip() makes these look identical (1.0); they are incomparable.
        query_vec = [1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0]
        item_vec = [1.0, 1.0, 1.0, 1.0]
        sim = HybridRetriever._cosine_similarity(query_vec, item_vec)
        assert sim == 0.0, (
            f"mismatched-dimension vectors must be incomparable (0.0), got {sim} "
            "— dot product silently truncated while norms used full vectors"
        )

    def test_mismatched_dims_other_order_also_zero(self):
        # Order must not matter: shorter query vs longer item is equally incomparable.
        sim = HybridRetriever._cosine_similarity([1.0, 1.0, 1.0, 1.0],
                                                 [1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0])
        assert sim == 0.0

    def test_equal_dims_unaffected(self):
        # The fix must not change behavior for the normal equal-length case.
        a = [1.0, 0.0, 0.0]
        b = [1.0, 0.0, 0.0]
        assert HybridRetriever._cosine_similarity(a, b) == pytest.approx(1.0)
        orthogonal = HybridRetriever._cosine_similarity([1.0, 0.0], [0.0, 1.0])
        assert orthogonal == pytest.approx(0.0)

    def test_empty_vector_edge_cases(self):
        # Document guard precedence (review nit): the length check runs first.
        # [] vs [1.0] are mismatched dims -> 0.0 (length guard wins).
        assert HybridRetriever._cosine_similarity([], [1.0]) == 0.0
        assert HybridRetriever._cosine_similarity([1.0], []) == 0.0
        # [] vs [] are equal-length but zero-norm -> 0.0 (zero-norm guard).
        assert HybridRetriever._cosine_similarity([], []) == 0.0
# ---------------------------------------------------------------------------
# Embedding rebuild background job
# ---------------------------------------------------------------------------


class _FakeEmbedder:
    """Embedder stub: returns a fixed vector and records which items it embedded."""

    model = "fake-embed"
    base_url = ""
    content_budget = 10_000  # mirrors the real _EMBED_CONTENT_BUDGET default

    def __init__(self):
        self.embedded_titles: list[str] = []

    def is_available(self) -> bool:
        return True

    async def is_available_async(self) -> bool:
        return self.is_available()

    def embed_for_item(self, title, summary, content=None):
        self.embedded_titles.append(title)
        return [0.1, 0.2, 0.3, 0.4]


@pytest.mark.asyncio
class TestRebuildEmbeddingsJob:
    async def _run(self, store, embedder, n_items):
        from kiro_crew.dashboard.handlers.knowledge import _rebuild_embeddings_job

        # Seed active items, each with a stale (single-element) embedding so we can
        # prove the rebuild overwrites in place rather than only filling NULLs.
        from kiro_crew.knowledge.embedder import floats_to_bytes
        for i in range(n_items):
            store.add_item(f"Item {i:03d}", f"body {i}", "document",
                           embedding=floats_to_bytes([9.9]))
        job_id = "rebuildjob01"
        now = "2026-06-16T00:00:00"
        store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES (?, NULL, 'processing', ?, ?)", (job_id, now, now))
        store.db.commit()
        await _rebuild_embeddings_job(None, store, embedder, job_id)
        return job_id

    async def test_rebuild_reembeds_all_items_across_batches(self, store):
        # More than one _REBUILD_BATCH_SIZE page to exercise the id-cursor loop.
        from kiro_crew.knowledge.embedder import embed_signature, floats_to_bytes
        from kiro_crew.knowledge.ingestion import _REBUILD_BATCH_SIZE
        n = _REBUILD_BATCH_SIZE + 5
        embedder = _FakeEmbedder()
        job_id = await self._run(store, embedder, n)

        # Every active item was embedded exactly once (cursor: no skips, no repeats).
        assert len(embedder.embedded_titles) == n
        assert len(set(embedder.embedded_titles)) == n

        job = store.db.execute(
            "SELECT * FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
        assert job["status"] == "completed"
        assert job["items_total"] == n
        assert job["items_processed"] == n

        # Stale [9.9] vectors were overwritten in place with the new embedding, and
        # every item is stamped with the current signature + an embedded_at timestamp.
        row = store.db.execute(
            "SELECT embedding, embedding_sig, embedded_at FROM items "
            "WHERE status = 'active' LIMIT 1").fetchone()
        assert row["embedding"] == floats_to_bytes([0.1, 0.2, 0.3, 0.4])
        assert row["embedding_sig"] == embed_signature(embedder.model)
        assert row["embedded_at"]

    async def test_rebuild_is_idempotent_skips_current_sig(self, store):
        # First rebuild stamps every item with the current sig.
        embedder = _FakeEmbedder()
        await self._run(store, embedder, 3)
        assert len(embedder.embedded_titles) == 3

        # A second rebuild on an unchanged setup finds nothing stale -> no-op.
        from kiro_crew.knowledge.ingestion import rebuild_embeddings
        second = _FakeEmbedder()
        processed = await rebuild_embeddings(store, second)
        assert processed == 0
        assert second.embedded_titles == []

    async def test_rebuild_partial_retry_resumes_only_stale(self, store):
        # One item already carries the current sig; the rest are stale (NULL sig).
        from kiro_crew.knowledge.embedder import embed_signature, floats_to_bytes
        from kiro_crew.knowledge.ingestion import rebuild_embeddings
        embedder = _FakeEmbedder()
        sig = embed_signature(embedder.model)
        done = store.add_item("done", "body", "document",
                              embedding=floats_to_bytes([0.1, 0.2, 0.3, 0.4]))
        store.db.execute("UPDATE items SET embedding_sig = ? WHERE id = ?", (sig, done))
        for i in range(2):
            store.add_item(f"stale {i}", "body", "document")
        store.db.commit()

        processed = await rebuild_embeddings(store, embedder)
        # Only the two stale items re-embed; the already-current one is skipped.
        assert processed == 2
        assert sorted(embedder.embedded_titles) == ["stale 0", "stale 1"]

    async def test_rebuild_force_reembeds_current_sig(self, store):
        # All items already current; force=True ignores the sig and re-embeds all.
        embedder = _FakeEmbedder()
        await self._run(store, embedder, 3)

        from kiro_crew.knowledge.ingestion import rebuild_embeddings
        forced = _FakeEmbedder()
        processed = await rebuild_embeddings(store, forced, force=True)
        assert processed == 3
        assert len(forced.embedded_titles) == 3

    async def test_rebuild_marks_job_failed_on_error(self, store):
        class _BoomEmbedder(_FakeEmbedder):
            def embed_for_item(self, title, summary, content=None):
                raise RuntimeError("ollama down mid-rebuild")

        job_id = await self._run(store, _BoomEmbedder(), 3)
        job = store.db.execute(
            "SELECT * FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert "ollama down" in (job["error"] or "")

    async def test_rebuild_heartbeats_updated_at_per_item_not_per_batch(self, store):
        """A slow item must not let the single-flight claimer judge the live job
        abandoned mid-batch: updated_at is committed AFTER EACH item, so it
        advances within a batch rather than only at end-of-batch. Regression for
        the concurrent-rebuild duplication the per-batch-only heartbeat allowed."""
        from kiro_crew.knowledge.ingestion import rebuild_embeddings

        # Capture the job row's COMMITTED updated_at as each item is embedded (the
        # embedder runs between the prior item's heartbeat commit and this one).
        seen_updated_at: list[str] = []

        class _RecordingEmbedder(_FakeEmbedder):
            def embed_for_item(self, title, summary, content=None):
                row = store.db.execute(
                    "SELECT updated_at FROM ingestion_jobs WHERE id = 'hbjob0000001'"
                ).fetchone()
                seen_updated_at.append(row["updated_at"] if row else "")
                return super().embed_for_item(title, summary, content)

        for i in range(3):
            store.add_item(f"hb {i}", "body", "document")
        job_id = "hbjob0000001"
        base = "2026-06-16T00:00:00"
        store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES (?, NULL, 'processing', ?, ?)", (job_id, base, base))
        store.db.commit()

        await rebuild_embeddings(store, _RecordingEmbedder(), job_id=job_id)

        # By the 2nd/3rd item, the committed updated_at has advanced past the
        # batch-start value — proving a per-item heartbeat, not a per-batch one.
        assert seen_updated_at, "embedder never ran"
        assert any(ts > base for ts in seen_updated_at[1:]), (
            f"updated_at never advanced mid-batch: {seen_updated_at}"
        )


@pytest.mark.asyncio
class TestWatcherSelfHeal:
    def _watcher(self, store, embedder):
        from kiro_crew.knowledge.watcher import KnowledgeWatcher

        class _Pipe:
            pass
        pipe = _Pipe()
        pipe.embedder = embedder
        return KnowledgeWatcher(store, pipe)

    async def test_stale_items_trigger_rebuild_job(self, store):
        # Items with NULL sig are stale -> watcher fires a tracked rebuild job.
        from kiro_crew.knowledge.embedder import embed_signature
        embedder = _FakeEmbedder()
        for i in range(3):
            store.add_item(f"Item {i}", "body", "document")
        watcher = self._watcher(store, embedder)

        await watcher._maybe_reembed_stale()
        assert watcher._reembed_task is not None
        await watcher._reembed_task

        job = store.db.execute(
            "SELECT * FROM ingestion_jobs WHERE source_id IS NULL "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
        assert job["status"] == "completed"
        assert job["items_processed"] == 3
        assert len(embedder.embedded_titles) == 3
        sig = embed_signature(embedder.model)
        stale = store.db.execute(
            "SELECT COUNT(*) AS c FROM items WHERE embedding_sig IS NULL OR embedding_sig != ?",
            (sig,)).fetchone()["c"]
        assert stale == 0

    async def test_no_stale_items_is_noop(self, store):
        # Everything already current -> no job created.
        from kiro_crew.knowledge.embedder import embed_signature, floats_to_bytes
        embedder = _FakeEmbedder()
        sig = embed_signature(embedder.model)
        item_id = store.add_item("current", "body", "document",
                                 embedding=floats_to_bytes([0.1, 0.2, 0.3, 0.4]))
        store.db.execute("UPDATE items SET embedding_sig = ? WHERE id = ?", (sig, item_id))
        store.db.commit()
        watcher = self._watcher(store, embedder)

        await watcher._maybe_reembed_stale()
        assert watcher._reembed_task is None
        assert embedder.embedded_titles == []

    async def test_single_flight_skips_when_job_processing(self, store):
        # A dashboard rebuild already in flight (fresh updated_at) -> watcher does
        # not stack a second.
        embedder = _FakeEmbedder()
        store.add_item("stale", "body", "document")
        now = datetime.now().isoformat()
        store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES ('inflight0001', NULL, 'processing', ?, ?)", (now, now))
        store.db.commit()
        watcher = self._watcher(store, embedder)

        await watcher._maybe_reembed_stale()
        assert watcher._reembed_task is None
        assert embedder.embedded_titles == []

    async def test_stale_processing_row_does_not_block(self, store):
        # A 'processing' row whose updated_at is older than the staleness window is
        # from a crash that bypassed cleanup -> the guard ignores it and the watcher
        # starts a fresh rebuild rather than being permanently blocked.
        from kiro_crew.knowledge.ingestion import _REBUILD_STALE_AFTER
        embedder = _FakeEmbedder()
        store.add_item("stale", "body", "document")
        old = (datetime.now() - _REBUILD_STALE_AFTER - timedelta(minutes=1)).isoformat()
        store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES ('dead00000001', NULL, 'processing', ?, ?)", (old, old))
        store.db.commit()
        watcher = self._watcher(store, embedder)

        await watcher._maybe_reembed_stale()
        assert watcher._reembed_task is not None
        await watcher._reembed_task
        assert embedder.embedded_titles == ["stale"]

    async def test_cancelled_job_row_is_finalized_not_left_processing(self, store):
        # If the rebuild task is cancelled (e.g. app shutdown), the job row must be
        # finalized to 'cancelled' and the CancelledError re-raised -- otherwise the
        # row stays 'processing' and permanently blocks the single-flight guard.
        embedder = _FakeEmbedder()
        store.add_item("item", "body", "document")
        watcher = self._watcher(store, embedder)
        now = datetime.now().isoformat()
        store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES ('cancel000001', NULL, 'processing', ?, ?)", (now, now))
        store.db.commit()

        async def _boom(*a, **k):
            raise asyncio.CancelledError()

        with patch("kiro_crew.knowledge.watcher.rebuild_embeddings", _boom):
            with pytest.raises(asyncio.CancelledError):
                await watcher._run_reembed_job(embedder, "cancel000001")

        row = store.db.execute(
            "SELECT status FROM ingestion_jobs WHERE id = 'cancel000001'").fetchone()
        assert row["status"] == "cancelled"


class TestEmbedSignature:
    def test_base_url_ignored_by_signature(self):
        # Embeddings run in-process (no external inference endpoint), so the
        # sig hashes f"{model}|inprocess|{budget}" — no base_url input. Same
        # model = stable signature; changing the model changes the signature,
        # triggering the sig-gated rebuild.
        from kiro_crew.knowledge.embedder import embed_signature

        a = embed_signature("m")
        b = embed_signature("m")
        assert a == b

    def test_model_changes_signature(self):
        from kiro_crew.knowledge.embedder import embed_signature

        assert embed_signature("m1") != embed_signature("m2")

    def test_content_budget_changes_signature(self):
        # Changing the budget must change the embed signature, else items
        # truncated under the old budget would never be re-embedded.
        from kiro_crew.knowledge.embedder import embed_signature

        assert embed_signature("m") != embed_signature("m", content_budget=42)

    def test_embedder_signature_matches_model_signature(self):
        from kiro_crew.knowledge.embedder import (
            _EMBED_CONTENT_BUDGET,
            embed_signature,
            embedder_signature,
        )

        class _E:
            model = "m"
            content_budget = _EMBED_CONTENT_BUDGET

        assert embedder_signature(_E()) == embed_signature("m")


class _FlakyEmbedder(_FakeEmbedder):
    """Returns None for items whose title is in ``fail_titles`` (transient failure)."""

    def __init__(self, fail_titles):
        super().__init__()
        self.fail_titles = set(fail_titles)

    def embed_for_item(self, title, summary, content=None):
        self.embedded_titles.append(title)
        if title in self.fail_titles:
            return None
        return [0.1, 0.2, 0.3, 0.4]


@pytest.mark.asyncio
class TestRebuildFailureAccounting:
    async def test_none_vec_not_counted_as_processed(self, store):
        # When embed returns None, the item is NOT counted as processed, its sig
        # stays stale (so it retries), but items_failed reflects the miss.
        from kiro_crew.knowledge.ingestion import rebuild_embeddings

        ok = store.add_item("ok", "body", "document")
        bad = store.add_item("bad", "body", "document")
        store.db.commit()
        embedder = _FlakyEmbedder(fail_titles=["bad"])

        processed = await rebuild_embeddings(store, embedder)
        assert processed == 1  # only the successful one
        # Failed item kept a NULL sig (retryable) but got an embedded_at attempt stamp.
        row = store.db.execute(
            "SELECT embedding_sig, embedded_at FROM items WHERE id = ?", (bad,)
        ).fetchone()
        assert row["embedding_sig"] is None
        assert row["embedded_at"]
        ok_row = store.db.execute("SELECT embedding_sig FROM items WHERE id = ?", (ok,)).fetchone()
        assert ok_row["embedding_sig"] is not None

    async def test_job_row_tracks_items_failed(self, store):
        from kiro_crew.knowledge.ingestion import rebuild_embeddings

        for t in ("a", "b", "c"):
            store.add_item(t, "body", "document")
        store.db.commit()
        now = datetime.now().isoformat()
        store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES ('jobfail00001', NULL, 'processing', ?, ?)",
            (now, now),
        )
        store.db.commit()
        embedder = _FlakyEmbedder(fail_titles=["b"])

        await rebuild_embeddings(store, embedder, job_id="jobfail00001")
        job = store.db.execute(
            "SELECT items_processed, items_failed FROM ingestion_jobs WHERE id = 'jobfail00001'"
        ).fetchone()
        assert job["items_processed"] == 2
        assert job["items_failed"] == 1

    async def test_watcher_backs_off_recently_failed_item(self, store):
        # A perpetually-failing item keeps a stale sig; once it has a recent
        # embedded_at attempt stamp, the watcher's stale count excludes it so it
        # doesn't re-trigger a rebuild every scan (post-merge retrigger-loop fix).
        from kiro_crew.knowledge.embedder import embedder_signature
        from kiro_crew.knowledge.ingestion import count_stale_items

        embedder = _FakeEmbedder()
        sig = embedder_signature(embedder)
        item = store.add_item("perma-fail", "body", "document")
        # Stale sig but attempted just now -> in backoff window -> not counted.
        store.db.execute(
            "UPDATE items SET embedded_at = ? WHERE id = ?", (datetime.now().isoformat(), item)
        )
        store.db.commit()
        assert count_stale_items(store, sig) == 0
        # An item never attempted (NULL embedded_at) IS counted.
        store.add_item("never-tried", "body", "document")
        store.db.commit()
        assert count_stale_items(store, sig) == 1


@pytest.mark.asyncio
class TestRebuildLostUpdateRace:
    async def test_concurrent_write_skips_stale_vector_update(self, store):
        # If a concurrent writer (file re-ingest) bumps the row's updated_at past the
        # rebuild's read snapshot while the embedder is working, the rebuild's UPDATE
        # must not land -- otherwise a new-content row gets an old-content vector
        # stamped "current". The guarded UPDATE drops on the contended row.
        import sqlite3

        from kiro_crew.knowledge.ingestion import rebuild_embeddings

        item = store.add_item("racey", "old body", "document")
        store.db.commit()
        # embed_for_item runs in a worker thread (run_in_executor), so it cannot
        # touch the main-thread sqlite connection. The concurrent writer opens its
        # OWN connection to the same db file -- which is exactly the real race
        # (ingestion writing while a rebuild embeds).
        db_path = store.db.execute("PRAGMA database_list").fetchall()[0]["file"]

        class _RacingEmbedder(_FakeEmbedder):
            def __init__(self, path, iid):
                super().__init__()
                self._path = path
                self._iid = iid

            def embed_for_item(self, title, summary, content=None):
                # Simulate a concurrent ingestion write landing mid-embed: bump
                # updated_at into the future relative to the rebuild's snapshot.
                conn = sqlite3.connect(self._path, timeout=30, isolation_level=None)
                try:
                    conn.execute(
                        "UPDATE items SET updated_at = ? WHERE id = ?",
                        ((datetime.now() + timedelta(minutes=1)).isoformat(), self._iid),
                    )
                finally:
                    conn.close()
                return super().embed_for_item(title, summary, content)

        embedder = _RacingEmbedder(db_path, item)
        processed = await rebuild_embeddings(store, embedder)
        assert processed == 0  # lost the race -> not stamped
        row = store.db.execute("SELECT embedding_sig FROM items WHERE id = ?", (item,)).fetchone()
        assert row["embedding_sig"] is None  # left stale for its own re-embed


@pytest.mark.asyncio
class TestStartRebuildJobSingleFlight:
    async def test_concurrent_claims_create_one_job(self, store):
        # The atomic claim must let only ONE of two racing callers (watcher tick vs
        # dashboard click) create a job -- the other gets None.
        from kiro_crew.knowledge.ingestion import start_rebuild_job

        first = start_rebuild_job(store)
        second = start_rebuild_job(store)
        assert first is not None
        assert second is None
        n = store.db.execute(
            "SELECT COUNT(*) AS c FROM ingestion_jobs "
            "WHERE source_id IS NULL AND status = 'processing'"
        ).fetchone()["c"]
        assert n == 1

    async def test_stale_processing_rows_swept_to_abandoned(self, store):
        # Crashed leftovers (stale 'processing' rows) are finalized to 'abandoned'
        # when a new rebuild claims the slot, so they don't accumulate forever.
        from kiro_crew.knowledge.ingestion import _REBUILD_STALE_AFTER, start_rebuild_job

        old = (datetime.now() - _REBUILD_STALE_AFTER - timedelta(minutes=1)).isoformat()
        for i in range(3):
            store.db.execute(
                "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
                "VALUES (?, NULL, 'processing', ?, ?)",
                (f"ghost{i:08d}", old, old),
            )
        store.db.commit()

        job_id = start_rebuild_job(store)
        assert job_id is not None
        abandoned = store.db.execute(
            "SELECT COUNT(*) AS c FROM ingestion_jobs WHERE status = 'abandoned'"
        ).fetchone()["c"]
        assert abandoned == 3
        # No stale 'processing' rows survived; only the fresh claim is processing.
        processing = store.db.execute(
            "SELECT id FROM ingestion_jobs WHERE status = 'processing'"
        ).fetchall()
        assert [r["id"] for r in processing] == [job_id]


@pytest.mark.asyncio
class TestDashboardRebuildCancel:
    async def test_dashboard_job_cancel_finalizes_row(self, store):
        # Same cancel-finalization contract as the watcher, in the dashboard wrapper.
        from kiro_crew.dashboard.handlers.knowledge import _rebuild_embeddings_job

        embedder = _FakeEmbedder()
        store.add_item("item", "body", "document")
        now = datetime.now().isoformat()
        store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES ('dashcancel01', NULL, 'processing', ?, ?)",
            (now, now),
        )
        store.db.commit()

        async def _boom(*a, **k):
            raise asyncio.CancelledError()

        with patch("kiro_crew.dashboard.handlers.knowledge.rebuild_embeddings", _boom):
            with pytest.raises(asyncio.CancelledError):
                await _rebuild_embeddings_job(None, store, embedder, "dashcancel01")

        row = store.db.execute(
            "SELECT status FROM ingestion_jobs WHERE id = 'dashcancel01'"
        ).fetchone()
        assert row["status"] == "cancelled"


@pytest.mark.asyncio
class TestWatcherLargeRebuildWarning:
    """A stale count at/over _LARGE_REBUILD_WARN_THRESHOLD logs a prominent WARNING."""

    def _watcher(self, store):
        from kiro_crew.knowledge.watcher import KnowledgeWatcher

        class _Pipe:
            pass
        pipe = _Pipe()
        pipe.embedder = _FakeEmbedder()
        return KnowledgeWatcher(store, pipe)

    async def test_large_stale_count_logs_warning(self, store, monkeypatch, caplog):
        import kiro_crew.knowledge.watcher as watcher_mod
        monkeypatch.setattr(watcher_mod, "_LARGE_REBUILD_WARN_THRESHOLD", 3)
        for i in range(3):
            store.add_item(f"Item {i}", "body", "document")
        watcher = self._watcher(store)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.knowledge.watcher"):
            await watcher._maybe_reembed_stale()
        assert watcher._reembed_task is not None
        await watcher._reembed_task

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                    and "full background re-embed" in r.getMessage()]
        assert len(warnings) == 1
        assert "3 items" in warnings[0].getMessage()

    async def test_small_stale_count_no_warning(self, store, monkeypatch, caplog):
        import kiro_crew.knowledge.watcher as watcher_mod
        monkeypatch.setattr(watcher_mod, "_LARGE_REBUILD_WARN_THRESHOLD", 100)
        store.add_item("Only item", "body", "document")
        watcher = self._watcher(store)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.knowledge.watcher"):
            await watcher._maybe_reembed_stale()
        assert watcher._reembed_task is not None
        await watcher._reembed_task

        assert not [r for r in caplog.records if r.levelno == logging.WARNING
                    and "full background re-embed" in r.getMessage()]
