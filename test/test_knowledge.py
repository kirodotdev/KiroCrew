"""Unit tests for the Knowledge Library (store, chunker, readers, extractor, retrieval)."""

from __future__ import annotations

import asyncio
import codecs
import importlib
import json
import logging
import re
import sys
import threading
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from kiro_crew.embeddings import PRIORITY_NORMAL
from kiro_crew.knowledge import readers
from kiro_crew.knowledge.chunker import HeadingAwareChunker
from kiro_crew.knowledge.extractor import EntityExtractor
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.retrieval import HybridRetriever, _bytes_to_floats
from kiro_crew.knowledge.store import KnowledgeBundleError, KnowledgeStore, SimpleDiGraph
from kiro_crew.knowledge.sync import SyncScheduler

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

    def test_export_import_restores_a_paused_source(self, store_factory):
        """A restored bundle keeps each source's state, from the column.

        ``export_all`` serializes SELECT * FROM sources, so the state travels in
        the column; the blob carries no copy. Seeding the restored column from
        the blob would land every source at the 'pending' default and silently
        resume walking a folder the user had paused.
        """
        s1 = store_factory("export-status.db")
        paused = s1.add_source("vault", "local_folder", "/tmp/rt-paused",
                               properties={"sync_status": "paused"})
        unconfirmed = s1.add_source("docs", "local_folder", "/tmp/rt-unconfirmed",
                                    properties={"sync_status": "pending_confirmation"})
        # An outcome state a completed operation wrote: a bundle is untrusted
        # input, so restoring it as-is would assert work that never ran here.
        errored = s1.add_source("dead", "local_folder", "/tmp/rt-errored")
        s1.update_source(errored, sync_status="error")
        bundle = s1.export_all()
        assert {s["sync_status"] for s in bundle["sources"]} == {
            "paused", "pending_confirmation", "error"}

        s2 = store_factory("import-status.db")
        s2.import_bundle(bundle)
        restored = {r["id"]: r["sync_status"] for r in s2.db.execute(
            "SELECT id, sync_status FROM sources").fetchall()}
        assert restored[paused] == "paused"
        assert restored[unconfirmed] == "pending_confirmation"
        # The refused 'error' does not survive. It lands 'pending_confirmation' rather
        # than the allowlist's 'pending' because this source walks a tree and 'pending'
        # is not skipped by the sweep -- so an imported folder would be walked on
        # arrival. Either way the outcome state the bundle claimed is gone.
        assert restored[errored] == "pending_confirmation"

    def test_export_import_restores_a_legacy_bundle_from_the_blob(self, store_factory):
        """A bundle written before the column travelled still restores."""
        s2 = store_factory("import-legacy.db")
        s2.import_bundle({"sources": [{
            "id": "legacy-1", "name": "vault", "source_type": "local_folder",
            "uri": "/tmp/rt-legacy",
            "properties": json.dumps({"sync_status": "paused"}),
        }]})
        row = s2.db.execute(
            "SELECT sync_status, properties FROM sources WHERE id = ?",
            ("legacy-1",)).fetchone()
        assert row["sync_status"] == "paused"
        assert "sync_status" not in json.loads(row["properties"])

    def test_import_does_not_leave_a_refused_status_for_the_migration(self, tmp_path):
        """A refused bundle status cannot come back at the next store open.

        A bundle is untrusted input, so an outcome state in it is refused. Storing the
        blob verbatim would leave that refused value inside the row for the every-open
        error-lift to read, applying it one reopen later and quiescing a source the
        allowlist had just protected.

        The row lands 'pending_confirmation' rather than the allowlist's 'pending'
        because this source WALKS A TREE: 'pending' is not skipped by the sweep, so a
        bundle naming any readable directory would have it walked on arrival. What this
        test pins either way is that the refused value does not survive -- checked here
        and again after a reopen.
        """
        db = str(tmp_path / "import-refused.db")
        s1 = KnowledgeStore(db)
        try:
            s1.import_bundle({"sources": [{
                "id": "refused-1", "name": "vault", "source_type": "local_folder",
                "uri": "/tmp/rt-refused",
                "properties": json.dumps({"sync_status": "error"}),
            }]})
            row = s1.db.execute(
                "SELECT sync_status, properties FROM sources WHERE id = ?",
                ("refused-1",)).fetchone()
            assert row["sync_status"] == "pending_confirmation"
            # The refused value is gone from the blob too, so nothing can lift it later.
            assert "sync_status" not in json.loads(row["properties"] or "{}")
        finally:
            s1.close()

        s2 = KnowledgeStore(db)
        try:
            row = s2.db.execute(
                "SELECT sync_status FROM sources WHERE id = ?", ("refused-1",)).fetchone()
            assert row["sync_status"] == "pending_confirmation"
        finally:
            s2.close()

    def test_update_source_compare_and_set_refuses_a_moved_row(self, store):
        """``if_sync_status`` makes a snapshot-derived write lose a race.

        A caller that decided what to write from a status it read earlier must
        not overwrite a transition that landed in between -- a sweep that saw
        'missing' and writes 'synced' would otherwise bury the 'error' a manual
        sync recorded while it ran.
        """
        sid = store.add_source("f", "local_file", "/tmp/cas.md")
        store.update_source(sid, sync_status="error")

        store.update_source(sid, sync_status="synced", if_sync_status="missing")
        assert store.db.execute(
            "SELECT sync_status FROM sources WHERE id = ?",
            (sid,)).fetchone()["sync_status"] == "error"

        store.update_source(sid, sync_status="synced", if_sync_status="error")
        assert store.db.execute(
            "SELECT sync_status FROM sources WHERE id = ?",
            (sid,)).fetchone()["sync_status"] == "synced"

    def test_migration_lift_loses_to_a_concurrent_column_write(self, store, tmp_path):
        """The repair binds the COLUMN it read, not just the blob.

        Every live writer transitions the column WITHOUT touching properties, so
        a blob-only precondition would still match and would stamp the blob's
        initial state over a transition that had just landed.
        """
        import sqlite3

        db_path = str(tmp_path / "test.db")
        sid = str(uuid4())
        now = datetime.now().isoformat()
        store.db.execute(
            "INSERT INTO sources (id, name, source_type, uri, properties, sync_status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, "s", "local_folder", "/tmp/repair-race",
             json.dumps({"sync_status": "pending_confirmation"}), "pending", now, now))
        store.db.commit()
        store.close()

        real_loads = json.loads
        fired: list[bool] = []

        def confirm_lands_mid_scan(raw):
            parsed = real_loads(raw)
            if (not fired and isinstance(parsed, dict)
                    and parsed.get("sync_status") == "pending_confirmation"):
                fired.append(True)
                # The user confirms the source while the pass is mid-row: a
                # COLUMN-only transition, leaving properties untouched.
                conn = sqlite3.connect(db_path, timeout=30)
                try:
                    conn.execute(
                        "UPDATE sources SET sync_status = 'active' WHERE id = ?", (sid,))
                    conn.commit()
                finally:
                    conn.close()
            return parsed

        with patch("kiro_crew.knowledge.store.json.loads", confirm_lands_mid_scan):
            reopened = KnowledgeStore(db_path)
        try:
            assert fired, "the mid-scan write never landed; the test proves nothing"
            assert reopened.db.execute(
                "SELECT sync_status FROM sources WHERE id = ?",
                (sid,)).fetchone()["sync_status"] == "active"
        finally:
            reopened.close()

    def test_migration_survives_a_pathologically_nested_blob(self, store, tmp_path):
        """One unparsable legacy row must not stop the gateway from starting.

        ``json.loads`` recurses per nesting level and raises RecursionError --
        a RuntimeError, so not covered by the ValueError/TypeError guard. This
        migration runs on EVERY store open, so an uncaught one would abort every
        construction rather than skipping the row.
        """
        deep = '{"sync_status": "active"}'
        for _ in range(60000):
            deep = '{"a": ' + deep + '}'
        with pytest.raises(RecursionError):
            json.loads(deep)

        ok = str(uuid4())
        bad = str(uuid4())
        now = datetime.now().isoformat()
        for sid, props_json, uri in (
            (bad, deep, "/tmp/deep"),
            (ok, json.dumps({"sync_status": "active"}), "/tmp/ok"),
        ):
            store.db.execute(
                "INSERT INTO sources (id, name, source_type, uri, properties, sync_status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, "s", "local_folder", uri, props_json, "pending", now, now))
        store.db.commit()
        store.close()

        reopened = KnowledgeStore(str(tmp_path / "test.db"))
        try:
            rows = {r["id"]: r["sync_status"] for r in reopened.db.execute(
                "SELECT id, sync_status FROM sources").fetchall()}
            # The row that could be read is still repaired; the other is skipped.
            assert rows[ok] == "active"
            assert rows[bad] == "pending"
        finally:
            reopened.close()

    def test_migration_converges_a_json_escaped_status_key(self, store, tmp_path):
        """A JSON-escaped key is still the key, so it still converges.

        JSON permits escapes inside a KEY, so a blob stored as
        {"sync_\\u0073tatus": "paused"} parses to `sync_status` while never
        containing that substring literally. Deciding membership by raw text
        would skip the row: the column would stay at its 'pending' default and
        the watcher, which now reads the column, would walk a folder the user had
        paused. `import_bundle` used to store a bundle's properties verbatim, so
        such a row can exist.
        """
        escaped = str(uuid4())
        now = datetime.now().isoformat()
        raw = '{"sync_\\u0073tatus": "paused"}'
        assert "sync_status" not in raw, "the point of the fixture is the escape"
        assert json.loads(raw) == {"sync_status": "paused"}
        store.db.execute(
            "INSERT INTO sources (id, name, source_type, uri, properties, sync_status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (escaped, "s", "local_folder", "/tmp/escaped", raw, "pending", now, now))
        store.db.commit()
        store.close()

        reopened = KnowledgeStore(str(tmp_path / "test.db"))
        try:
            row = reopened.db.execute(
                "SELECT sync_status, properties FROM sources WHERE id = ?",
                (escaped,)).fetchone()
            assert row["sync_status"] == "paused"
            # Retired too, and re-serialized, so the escape cannot come back.
            assert json.loads(row["properties"]) == {}
        finally:
            reopened.close()

    # ---- import_bundle JSON-column well-formedness (issue #5559) -----------
    # The invariant "sources.properties / entities.aliases is JSON text every
    # reader json.loads()s back" is enforced at the writer, so every store
    # caller is covered — not only the dashboard handler.

    def _source(self, **overrides):
        src = {"id": "s1", "name": "f", "source_type": "local_file", "uri": "/tmp/x.md"}
        src.update(overrides)
        return src

    def _entity(self, **overrides):
        ent = {"id": "e1", "name": "Svc", "entity_type": "service"}
        ent.update(overrides)
        return ent

    @pytest.mark.parametrize("props", [
        "{not json",          # unparseable
        "",                   # empty string: json.loads("") raises
        "[]",                 # parses, wrong shape (readers index a dict)
        "null",               # parses to None, not a dict
        {"k": "v"},           # non-string: would bind str(dict) repr as TEXT
        7,                    # non-string scalar
        pytest.param(
            "[" * 200000 + "]" * 200000,  # json.loads raises RecursionError
            # Short id: the default id embeds all 400k characters, and on
            # Windows pytest's PYTEST_CURRENT_TEST env var (which carries the
            # full test id) is capped at 32767 chars -> setup ValueError.
            id="deep-nesting",
        ),
        '{"x": "\ud800"}',    # lone surrogate: json.loads accepts, SQLite bind cannot UTF-8-encode
    ])
    def test_import_bundle_rejects_malformed_properties(self, store, props):
        bundle = {"sources": [self._source(properties=props)]}
        with pytest.raises(KnowledgeBundleError):
            store.import_bundle(bundle)
        # The transaction rolled back: no partial row committed.
        assert store.db.execute("SELECT COUNT(*) AS c FROM sources").fetchone()["c"] == 0

    @pytest.mark.parametrize("aliases", [
        "{not json",          # unparseable
        "",                   # empty string
        "{}",                 # parses, wrong shape (find_entity iterates a list)
        '["ok", 3]',          # list with a non-string element (.lower() crashes)
        ["a"],                # non-string: a Python list, not JSON text
        '["\ud800"]',         # lone surrogate: json.loads accepts, SQLite bind cannot UTF-8-encode
    ])
    def test_import_bundle_rejects_malformed_aliases(self, store, aliases):
        bundle = {"entities": [self._entity(aliases=aliases)]}
        with pytest.raises(KnowledgeBundleError):
            store.import_bundle(bundle)
        assert store.db.execute("SELECT COUNT(*) AS c FROM entities").fetchone()["c"] == 0

    def test_import_bundle_defaults_absent_and_null_json_columns(self, store):
        bundle = {
            "sources": [self._source(), self._source(id="s2", uri="/tmp/y.md", properties=None)],
            "entities": [self._entity(), self._entity(id="e2", name="Svc2", aliases=None)],
        }
        store.import_bundle(bundle)
        for row in store.db.execute("SELECT properties FROM sources"):
            assert json.loads(row["properties"]) == {}
        for row in store.db.execute("SELECT aliases FROM entities"):
            assert json.loads(row["aliases"]) == []

    def test_import_bundle_accepts_valid_json_columns(self, store):
        bundle = {
            "sources": [self._source(properties='{"namespace": "docs"}')],
            "entities": [self._entity(aliases='["svc", "the-svc"]')],
        }
        result = store.import_bundle(bundle)
        assert result["entities_created"] == 1
        props = store.db.execute("SELECT properties FROM sources").fetchone()["properties"]
        assert json.loads(props) == {"namespace": "docs"}
        # The committed alias row is readable by the alias-scanning reader.
        assert store.find_entity("THE-SVC")["id"] == "e1"

    def test_import_bundle_rejection_rolls_back_earlier_rows(self, store):
        # A valid source followed by a corrupt entity must commit NOTHING:
        # the whole bundle is one transaction.
        bundle = {
            "sources": [self._source()],
            "entities": [self._entity(aliases="{oops")],
        }
        with pytest.raises(KnowledgeBundleError):
            store.import_bundle(bundle)
        assert store.db.execute("SELECT COUNT(*) AS c FROM sources").fetchone()["c"] == 0

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
        for ext in ('.md', '.txt', '.py', '.html', '.json', '.jsonl', '.ndjson', '.yaml', '.csv'):
            assert ext in reader.SUPPORTED, f"{ext} missing from SUPPORTED"

    def test_powershell_extensions_ingested_as_plain_text(self, tmp_path):
        # PowerShell scripts (.ps1), modules (.psm1), and module manifests
        # (.psd1) are plain UTF-8 text: they must be in SUPPORTED (so folder
        # sources ingest rather than silently skip them) and must flow through
        # the generic _read_text path, not a _DISPATCH reader.
        reader = FileReader()
        samples = {
            '.ps1': 'Write-Host "hello from a script"',
            '.psm1': 'function Get-Thing { "hello from a module" }',
            '.psd1': "@{ ModuleVersion = '1.0'; Description = 'hello manifest' }",
        }
        for ext, content in samples.items():
            assert ext in reader.SUPPORTED, f"{ext} missing from SUPPORTED"
            assert ext not in reader._DISPATCH, f"{ext} must use the generic text path"
            f = tmp_path / f"sample{ext}"
            f.write_text(content, encoding="utf-8")
            text, meta = reader.read(str(f))
            assert content in text
            assert meta['format'] == ext.lstrip('.')
            assert meta['extension'] == ext
        # Scripts and modules chunk at function boundaries like their .sh/.rb
        # peers; the .psd1 manifest is data, so it stays on the generic path.
        from kiro_crew.knowledge.ingestion import CODE_EXTS
        assert '.ps1' in CODE_EXTS
        assert '.psm1' in CODE_EXTS
        assert '.psd1' not in CODE_EXTS

    def test_utf16_powershell_files_decode_cleanly(self, tmp_path):
        # Windows PowerShell 5.1 tooling (New-ModuleManifest, the legacy ISE)
        # writes UTF-16LE with a BOM. Without BOM sniffing those bytes miss
        # utf-8 and land in the latin-1 fallback, which preserves the BOM and
        # interleaved NULs -- the store would index mojibake, not the script.
        reader = FileReader()
        content = "@{ ModuleVersion = '1.0'; Description = 'utf16 manifest' }"
        for name, encoding in (
            ("manifest-le.psd1", "utf-16-le"),
            ("manifest-be.psd1", "utf-16-be"),
        ):
            f = tmp_path / name
            # Write the BOM explicitly so both endiannesses are exercised.
            bom = codecs.BOM_UTF16_LE if encoding == "utf-16-le" else codecs.BOM_UTF16_BE
            f.write_bytes(bom + content.encode(encoding))
            text, meta = reader.read(str(f))
            assert content in text, f"{name}: UTF-16 content not decoded"
            assert '\x00' not in text, f"{name}: NUL bytes leaked into indexed text"
            assert meta['format'] == 'psd1'
        # A BOM that lies (truncated/invalid UTF-16 payload) degrades to
        # latin-1 like the utf-8 branch does -- ingest never hard-fails on it.
        liar = tmp_path / "truncated.psd1"
        liar.write_bytes(codecs.BOM_UTF16_LE + b'A')
        text, meta = reader.read(str(liar))
        assert meta['format'] == 'psd1', "invalid UTF-16 must degrade, not error"
        # The HTML reader shares the same decode: BOM'd UTF-16 HTML from
        # Windows tooling must not fall into the latin-1 mojibake path either.
        page = tmp_path / "saved.html"
        page.write_bytes(codecs.BOM_UTF16_LE
                         + "<html><body>utf16 page body</body></html>".encode("utf-16-le"))
        text, meta = reader.read(str(page))
        assert "utf16 page body" in text
        assert '\x00' not in text


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

    def test_read_pdf_releases_each_page_cache(self, monkeypatch):
        events = []

        class FakePage:
            def __init__(self, number, text=None, error=None):
                self.number = number
                self.text = text
                self.error = error

            def extract_text(self):
                events.append(("extract", self.number))
                if self.error is not None:
                    raise self.error
                return self.text

            def close(self):
                events.append(("close", self.number))

        class FakePdf:
            def __init__(self, pages):
                self.pages = pages

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeLegacyPage:
            def extract_text(self):
                events.append(("extract", 4))
                return "legacy"

            def flush_cache(self):
                events.append(("flush", 4))

        first_pages = [FakePage(1, "first"), FakePage(2, "second")]
        failing_pages = [FakePage(3, error=ValueError("bad page"))]
        legacy_pages = [FakeLegacyPage()]
        opened = iter((FakePdf(first_pages), FakePdf(failing_pages), FakePdf(legacy_pages)))

        class FakePdfplumber:
            @staticmethod
            def open(_path):
                return next(opened)

        monkeypatch.setattr(readers, "pdfplumber", FakePdfplumber)

        text, meta = FileReader()._read_pdf("ok.pdf")
        assert text == "first\nsecond"
        assert meta == {"format": "pdf", "page_count": 2}
        assert events == [
            ("extract", 1),
            ("close", 1),
            ("extract", 2),
            ("close", 2),
        ]

        text, meta = FileReader()._read_pdf("bad.pdf")
        assert text == "Error reading file: bad page"
        assert meta == {"format": "error", "error": "bad page"}
        assert events[-2:] == [("extract", 3), ("close", 3)]

        text, meta = FileReader()._read_pdf("legacy.pdf")
        assert text == "legacy"
        assert meta == {"format": "pdf", "page_count": 1}
        assert events[-2:] == [("extract", 4), ("flush", 4)]

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
        sid = store.add_source("Opportunity Planner", "local_folder", "/home/alice/op/src/")
        item_id = store.add_item(
            "Auth Design", "JWT tokens with refresh flow", "design_doc", source_id=sid
        )
        store.db.execute(
            "INSERT INTO folder_file_state (source_id, file_path, item_ids, last_seen, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, "/home/alice/op/src/auth.md", json.dumps([item_id]), "now", "done"),
        )
        retriever = HybridRetriever(store)
        results = retriever.search("JWT")
        top = next(r for r in results if r["id"] == item_id)
        assert top["source_type"] == "local_folder"
        assert top["source_name"] == "Opportunity Planner"
        assert top["file_path"] == "/home/alice/op/src/auth.md"

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


class TestHybridRetrieverSourceFilter:
    def test_source_id_narrows_keyword_seeds(self, store):
        # Both items match the query; scoping to one source keeps only its item
        # (no entities exist, so the unfiltered graph leg contributes nothing).
        src_a = store.add_source("Docs A", "local_folder", "/tmp/a")
        src_b = store.add_source("Docs B", "local_folder", "/tmp/b")
        store.add_item("Auth A", "JWT tokens for service alpha", "doc", source_id=src_a)
        store.add_item("Auth B", "JWT tokens for service beta", "doc", source_id=src_b)
        retriever = HybridRetriever(store)
        results = retriever.search("JWT", source_id=src_a)
        assert [r["title"] for r in results] == ["Auth A"]

    def test_omitted_source_id_keeps_current_behavior(self, store):
        # Regression: no source_id == the pre-filter result set.
        src_a = store.add_source("Docs A", "local_folder", "/tmp/a")
        src_b = store.add_source("Docs B", "local_folder", "/tmp/b")
        store.add_item("Auth A", "JWT tokens for service alpha", "doc", source_id=src_a)
        store.add_item("Auth B", "JWT tokens for service beta", "doc", source_id=src_b)
        retriever = HybridRetriever(store)
        results = retriever.search("JWT")
        assert {r["title"] for r in results} == {"Auth A", "Auth B"}

    def test_graph_leg_still_reaches_other_sources(self, store):
        # The graph leg is deliberately unfiltered: an entity hit in another
        # source still surfaces, marked as a graph match, while the keyword
        # seeds stay scoped to the requested source.
        src_a = store.add_source("Docs A", "local_folder", "/tmp/a")
        src_b = store.add_source("Docs B", "local_folder", "/tmp/b")
        store.add_item("Auth A", "JWT tokens for service alpha", "doc", source_id=src_a)
        item_b = store.add_item("Gateway Doc", "routing notes", "doc", source_id=src_b)
        ent = store.add_entity("Gateway", "service")
        store.add_mention(item_b, ent)
        retriever = HybridRetriever(store)
        results = retriever.search("JWT Gateway", source_id=src_a)
        by_title = {r["title"]: r for r in results}
        assert "Auth A" in by_title
        assert "Gateway Doc" in by_title
        assert by_title["Gateway Doc"]["match_type"] == "graph"

    def test_source_id_narrows_vector_seeds(self, store):
        # Identical embeddings in two sources; scoping keeps one. The query
        # shares no tokens with the content, isolating the vector leg.
        src_a = store.add_source("Docs A", "local_folder", "/tmp/a")
        src_b = store.add_source("Docs B", "local_folder", "/tmp/b")
        vec = json.dumps([1.0, 0.0, 0.0, 0.0]).encode()
        store.add_item("Vec A", "alpha content", "doc", source_id=src_a, embedding=vec)
        store.add_item("Vec B", "beta content", "doc", source_id=src_b, embedding=vec)
        retriever = HybridRetriever(store, embedder=lambda q: [1.0, 0.0, 0.0, 0.0])
        results = retriever.search("unrelatedquerytoken", source_id=src_a)
        assert [r["title"] for r in results] == ["Vec A"]

    def test_unknown_source_id_returns_graph_only_results(self, store):
        # A nonexistent id empties the seed legs without raising; the tool
        # layer is what turns this into a guidance message.
        store.add_item("Auth", "JWT tokens", "doc")
        retriever = HybridRetriever(store)
        assert retriever.search("JWT", source_id="no-such-source") == []

    def test_scoped_search_includes_dedup_survivor_via_source_locations(self, store):
        # An item owned by source A but located in source B (the surviving copy
        # of a cross-source dedup collapse) still belongs to B's scope — the
        # same ownership-OR-location rule the /api/knowledge/graph filter uses.
        src_a = store.add_source("Owner", "local_folder", "/tmp/owner")
        src_b = store.add_source("Location", "local_folder", "/tmp/loc")
        item = store.add_item("Shared Doc", "JWT tokens shared", "doc", source_id=src_a)
        store.add_source_location(item, src_b)
        retriever = HybridRetriever(store)
        assert [r["title"] for r in retriever.search("JWT", source_id=src_b)] == ["Shared Doc"]


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

    def test_add_source_persists_sync_status_column(self, store):
        """Insert seeds the COLUMN and stores no second copy in the blob.

        The dashboard reads the COLUMN to pick the row's control (the Confirm
        button renders only for 'pending_confirmation'), so a column stuck at
        the 'pending' default while properties carries 'pending_confirmation'
        makes a folder source unstartable. Callers still STATE the initial
        status in properties; it is lifted onto the column and dropped from the
        blob so the row cannot hold two answers.
        """
        sid = store.add_source(
            "vault", "local_folder", "/tmp/vault",
            properties={"sync_status": "pending_confirmation"})
        row = store.db.execute(
            "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["sync_status"] == "pending_confirmation"
        assert "sync_status" not in json.loads(row["properties"])

    def test_add_source_does_not_mutate_the_callers_properties(self, store):
        """Lifting the status out is not allowed to edit the caller's dict."""
        props = {"sync_status": "active", "namespace": "docs"}
        store.add_source("vault", "local_folder", "/tmp/vault-nomut", properties=props)
        assert props == {"sync_status": "active", "namespace": "docs"}

    def test_add_source_sync_status_defaults_to_pending(self, store):
        """A caller that states no sync_status keeps the column's default."""
        sid = store.add_source("f", "local_file", "/tmp/nostatus.md", properties={})
        row = store.db.execute(
            "SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["sync_status"] == "pending"

    def test_add_source_rejects_non_initial_sync_status(self, store):
        """A transient or outcome state in properties never seeds the column.

        The create endpoint passes request-body properties through, so a
        caller-supplied 'syncing' would otherwise persist and make the sync
        endpoint report a conflict forever for a source whose sync never
        started. Only durable states pass; a claim about work that never ran
        falls back to 'pending', and the forged value survives in neither store.
        """
        for forged in ("syncing", "synced", "error", "missing", "garbage"):
            sid = store.add_source(
                "f", "local_file", f"/tmp/forged-{forged}.md",
                properties={"sync_status": forged})
            row = store.db.execute(
                "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)).fetchone()
            assert row["sync_status"] == "pending", forged
            assert "sync_status" not in json.loads(row["properties"]), forged

    def test_add_source_accepts_paused_as_an_initial_state(self, store):
        """A source may START paused: it is a durable decision, not a claim.

        A bundle import restores a source the user had paused, and the column is
        now the only place that state can live -- dropping it would silently
        resume scanning a folder the user stopped.
        """
        sid = store.add_source("vault", "local_folder", "/tmp/vault-paused",
                               properties={"sync_status": "paused"})
        row = store.db.execute(
            "SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["sync_status"] == "paused"

    def test_auto_added_source_persists_sync_status_column(self, store):
        """An auto-added source keeps the same single-store invariant.

        The aggregate source the agent's add-document tool creates seeds
        sync_status='active' in properties; the column must carry it or the
        dashboard renders the stale 'pending' control for a source that is
        already active.
        """
        sid = store.add_source(
            "agent-added", "agent", "agent://",
            properties={"sync_status": "active", "auto_added": True})
        assert sid is not None
        row = store.db.execute(
            "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["sync_status"] == "active"
        assert "sync_status" not in json.loads(row["properties"])
        assert json.loads(row["properties"])["auto_added"] is True

    def test_migration_repairs_divergent_sync_status_rows(self, store, tmp_path):
        """Reopening a store repairs rows whose column diverged from the JSON.

        Rows inserted while the INSERT paths wrote only the properties JSON
        have a column stuck at 'pending'; the migration copies the JSON state
        over so those sources become startable. Rows a handler already
        transitioned (non-'pending' column) are never touched.
        """
        divergent = str(uuid4())
        live = str(uuid4())
        agree = str(uuid4())
        listprops = str(uuid4())
        forged = str(uuid4())
        now = datetime.now().isoformat()
        for sid, column, props_json, uri in (
            (divergent, "pending", json.dumps({"sync_status": "pending_confirmation"}), "/tmp/div"),
            (live, "paused", json.dumps({"sync_status": "active"}), "/tmp/live"),
            (agree, "pending", json.dumps({}), "/tmp/agree"),
            # Imported/legacy rows can hold non-object JSON; the repair must
            # skip them instead of crashing store initialization.
            (listprops, "pending", "[]", "/tmp/listprops"),
            # A lifecycle state in the JSON is never a valid initial state and
            # must not be copied over (a forged 'syncing' would lock the sync
            # endpoint into reporting a conflict).
            (forged, "pending", json.dumps({"sync_status": "syncing"}), "/tmp/forged"),
        ):
            store.db.execute(
                "INSERT INTO sources (id, name, source_type, uri, properties, sync_status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, "s", "local_folder", uri, props_json, column, now, now))
        store.db.commit()
        store.close()

        reopened = KnowledgeStore(str(tmp_path / "test.db"))
        try:
            rows = {r["id"]: r["sync_status"] for r in reopened.db.execute(
                "SELECT id, sync_status FROM sources").fetchall()}
            assert rows[divergent] == "pending_confirmation"
            assert rows[live] == "paused"
            assert rows[agree] == "pending"
            assert rows[listprops] == "pending"
            assert rows[forged] == "pending"
        finally:
            reopened.close()

    def test_migration_skips_a_row_whose_properties_moved_mid_repair(self, store, tmp_path):
        """A properties-only write landing mid-pass wins over the snapshot.

        The pass reads both copies, then writes. A pre-column
        ``SyncScheduler._record_failure`` moved the properties copy WITHOUT the
        column, so comparing only the column would let the repair stamp
        'pending_confirmation' onto a row whose blob now reads 'error' -- the
        dashboard would offer Confirm for a source the scheduler has given up on.
        Binding the blob as read refuses every write for that row, including the
        retire, so nothing is lost; the next open sees the settled state and
        converges it.
        """
        import sqlite3

        db_path = str(tmp_path / "test.db")
        sid = str(uuid4())
        now = datetime.now().isoformat()
        store.db.execute(
            "INSERT INTO sources (id, name, source_type, uri, properties, sync_status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, "s", "local_folder", "/tmp/race",
             json.dumps({"sync_status": "pending_confirmation"}), "pending", now, now))
        store.db.commit()
        store.close()

        real_loads = json.loads
        fired: list[bool] = []

        def failure_lands_mid_scan(raw):
            parsed = real_loads(raw)
            # Fire once, only for the row under test: the pass parses each
            # candidate row between its SELECT and its UPDATE.
            if (not fired and isinstance(parsed, dict)
                    and parsed.get("sync_status") == "pending_confirmation"):
                fired.append(True)
                conn = sqlite3.connect(db_path, timeout=30)
                try:
                    conn.execute(
                        "UPDATE sources SET properties = ? WHERE id = ?",
                        (json.dumps({"sync_status": "error", "consecutive_failures": 3}), sid))
                    conn.commit()
                finally:
                    conn.close()
            return parsed

        with patch("kiro_crew.knowledge.store.json.loads", failure_lands_mid_scan):
            reopened = KnowledgeStore(db_path)
        try:
            assert fired, "the mid-scan write never landed; the test proves nothing"
            row = reopened.db.execute(
                "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)).fetchone()
            # Never the stale snapshot: the row the scheduler gave up on must not
            # come back offering Confirm.
            assert row["sync_status"] == "pending"
            # The refused retire left the copy intact, so nothing was dropped.
            assert json.loads(row["properties"])["sync_status"] == "error"
        finally:
            reopened.close()

        # The next open sees a row nobody is racing and retires the copy. The
        # blob's 'error' is a lifecycle value, so it is dropped rather than
        # promoted: it cannot be ordered against the column, and the scheduler's
        # own failure count re-marks the source on its next failed attempt.
        settled = KnowledgeStore(db_path)
        try:
            row = settled.db.execute(
                "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)).fetchone()
            assert row["sync_status"] == "pending"
            assert "sync_status" not in json.loads(row["properties"])
            assert json.loads(row["properties"])["consecutive_failures"] == 3
        finally:
            settled.close()

    def test_migration_lifts_a_legacy_json_only_error_onto_the_column(self, store, tmp_path):
        """The copy is retired, and a LIFECYCLE value in it is never promoted.

        Only an INITIAL state is repaired, and only onto a column still at its
        un-written default. A blob 'error' is left where it is: it cannot be
        ordered against the column, so promoting it would mark a recovered source
        errored with no copy left to correct it.
        """
        divergent = str(uuid4())
        legacy_error = str(uuid4())
        healthy = str(uuid4())
        recovered = str(uuid4())
        listprops = str(uuid4())
        now = datetime.now().isoformat()
        for sid, column, props_json, uri in (
            (divergent, "pending",
             json.dumps({"sync_status": "pending_confirmation"}), "/tmp/divergent"),
            (legacy_error, "pending", json.dumps({"sync_status": "error",
                                                  "consecutive_failures": 3}), "/tmp/legacy"),
            (healthy, "synced", json.dumps({"mtime": 1}), "/tmp/healthy"),
            # A pre-column failure recorded in the blob, then a successful
            # re-ingest that wrote the COLUMN only. The column is the newer
            # answer and must survive untouched.
            (recovered, "synced", json.dumps({"sync_status": "error"}), "/tmp/recovered"),
            (listprops, "pending", "[]", "/tmp/listprops"),
        ):
            # local_folder: the reopen also runs the orphan cleanup, which
            # deletes item-less sources of every other type.
            store.db.execute(
                "INSERT INTO sources (id, name, source_type, uri, properties, sync_status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, "s", "local_folder", uri, props_json, column, now, now))
        store.db.commit()
        store.close()

        reopened = KnowledgeStore(str(tmp_path / "test.db"))
        try:
            rows = {r["id"]: dict(r) for r in reopened.db.execute(
                "SELECT id, sync_status, properties FROM sources").fetchall()}
            # An initial state IS repaired onto an un-written column.
            assert rows[divergent]["sync_status"] == "pending_confirmation"
            # A lifecycle value is NOT promoted, whatever the column reads.
            assert rows[legacy_error]["sync_status"] == "pending"
            assert rows[recovered]["sync_status"] == "synced"
            assert rows[healthy]["sync_status"] == "synced"
            assert rows[listprops]["sync_status"] == "pending"
            # The second store is RETIRED, not left for the next open to re-read.
            for sid, r in rows.items():
                parsed = json.loads(r["properties"] or "{}")
                if isinstance(parsed, dict):
                    assert "sync_status" not in parsed, sid
            # The rest of the blob survives the strip.
            assert json.loads(rows[legacy_error]["properties"])["consecutive_failures"] == 3
        finally:
            reopened.close()

    def test_migration_does_not_re_error_a_source_that_has_since_synced(self, store, tmp_path):
        """A recovered source survives the upgrade, whenever it recovered.

        Ingestion's success writers are column-only -- they never touch
        properties -- so a legacy blob-'error' row that syncs keeps its blob copy.
        Both orderings must leave the healthy column alone: a recovery that landed
        BEFORE the first open under this change (the copy is still present when
        the migration first runs) and one that lands after it.
        """
        before = str(uuid4())
        after = str(uuid4())
        now = datetime.now().isoformat()
        for sid, column, uri in ((before, "synced", "/tmp/before"),
                                 (after, "pending", "/tmp/after")):
            store.db.execute(
                "INSERT INTO sources (id, name, source_type, uri, properties, sync_status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, "s", "local_folder", uri,
                 json.dumps({"sync_status": "error", "consecutive_failures": 3}),
                 column, now, now))
        store.db.commit()
        store.close()

        first = KnowledgeStore(str(tmp_path / "test.db"))
        try:
            rows = {r["id"]: r["sync_status"] for r in first.db.execute(
                "SELECT id, sync_status FROM sources").fetchall()}
            # Recovered before the upgrade: never overwritten.
            assert rows[before] == "synced"
            assert rows[after] == "pending"
            # A successful re-sync, written the way ingestion writes it: column
            # only, properties untouched.
            first.db.execute("UPDATE sources SET sync_status = 'synced' WHERE id = ?", (after,))
            first.db.commit()
        finally:
            first.close()

        second = KnowledgeStore(str(tmp_path / "test.db"))
        try:
            rows = {r["id"]: r["sync_status"] for r in second.db.execute(
                "SELECT id, sync_status FROM sources").fetchall()}
            assert rows[before] == "synced"
            assert rows[after] == "synced"
        finally:
            second.close()

    def test_update_source_drops_a_properties_borne_status(self, store):
        """A status written through properties is dropped, not stored.

        This is the seam that makes the column the only store: a legacy row's
        second copy disappears the first time anything writes its properties,
        and no caller can create a new one.
        """
        sid = store.add_source("f", "local_file", "/tmp/lift.md")
        store.update_source(sid, properties={"sync_status": "missing", "mtime": 7})
        row = store.db.execute(
            "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)).fetchone()
        props = json.loads(row["properties"])
        assert "sync_status" not in props
        assert props["mtime"] == 7
        # Dropped, NOT applied: see the next test for why that matters.
        assert row["sync_status"] == "pending"

    def test_update_source_does_not_let_a_stale_blob_move_the_column(self, store):
        """An unrelated properties write must not resurrect a legacy status.

        A row written by the pre-column watcher carries 'missing' in its blob.
        The watcher re-reads that blob to persist mtime/content_hash after
        re-ingesting the file, so a seam that APPLIED the blob's status would
        stamp 'missing' back onto a source that had just been re-ingested.
        """
        sid = store.add_source("f", "local_file", "/tmp/stale.md")
        store.db.execute(
            "UPDATE sources SET properties = ?, sync_status = 'synced' WHERE id = ?",
            (json.dumps({"sync_status": "missing", "mtime": 1}), sid))
        store.db.commit()

        legacy_blob = json.loads(store.db.execute(
            "SELECT properties FROM sources WHERE id = ?", (sid,)).fetchone()["properties"])
        legacy_blob["mtime"] = 2
        store.update_source(sid, properties=json.dumps(legacy_blob))

        row = store.db.execute(
            "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["sync_status"] == "synced"
        assert json.loads(row["properties"]) == {"mtime": 2}

    def test_update_source_drops_a_status_out_of_a_serialized_blob(self, store):
        """Callers that hand over pre-serialized JSON get the same treatment."""
        sid = store.add_source("f", "local_file", "/tmp/lift-str.md")
        store.update_source(sid, properties=json.dumps({"sync_status": "error", "mtime": 3}))
        row = store.db.execute(
            "SELECT properties FROM sources WHERE id = ?", (sid,)).fetchone()
        assert json.loads(row["properties"]) == {"mtime": 3}

    def test_update_source_writes_an_explicit_status(self, store):
        """The kwarg is the only channel a transition may use."""
        sid = store.add_source("f", "local_file", "/tmp/lift-both.md")
        store.update_source(
            sid, properties={"sync_status": "missing"}, sync_status="active")
        row = store.db.execute(
            "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["sync_status"] == "active"
        assert "sync_status" not in json.loads(row["properties"])

    def test_update_source_leaves_a_non_object_blob_alone(self, store):
        """A blob that is not a JSON object is stored as given, not rewritten."""
        sid = store.add_source("f", "local_file", "/tmp/lift-list.md")
        store.update_source(sid, properties="[]")
        row = store.db.execute(
            "SELECT properties FROM sources WHERE id = ?", (sid,)).fetchone()
        assert row["properties"] == "[]"

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
        assert bundle["items"][0]["id"] == item_id
        assert len(bundle["entities"]) == 2
        assert len(bundle["relations"]) == 1
        assert len(bundle["source_locations"]) == 1
        assert len(bundle["mentions"]) == 2
        assert bundle["sources"][0]["id"] == sid

    def test_export_item_missing(self, store):
        assert store.export_item("nope") == {}

    def test_export_item_without_source(self, store):
        item_id = store.add_item("Doc", "content", "doc")
        bundle = store.export_item(item_id)
        assert bundle["items"][0]["id"] == item_id
        assert bundle["sources"] == []

    def test_export_item_roundtrips_into_a_fresh_instance(self, store_factory):
        s1 = store_factory("export_item_src.db")
        sid = s1.add_source("f", "local_file", "/tmp/exp2.md")
        item_id = s1.add_item("Doc", "content", "doc", source_id=sid)
        eid = s1.add_entity("Svc", "service")
        s1.add_mention(item_id, eid)
        s1.add_source_location(item_id, sid, section_title="Main")
        bundle = s1.export_item(item_id)

        s2 = store_factory("export_item_dst.db")
        result = s2.import_bundle(bundle)
        assert result["items_imported"] == 1
        assert s2.get_item(item_id)["title"] == "Doc"
        mentions = s2.db.execute(
            "SELECT * FROM mentions WHERE item_id = ?", (item_id,)
        ).fetchall()
        assert len(mentions) == 1
        assert mentions[0]["entity_id"] == eid

    def test_export_item_excludes_relations_whose_other_endpoint_is_not_exported(self, store):
        """A relation touching an entity outside this item's mentions must not
        ride along -- the receiving store never gets that entity's row, so
        re-importing the relation would violate entity_relations' FK on
        source_id/target_id."""
        item_id = store.add_item("Doc", "content", "doc")
        mentioned = store.add_entity("Svc", "service")
        outside = store.add_entity("Unrelated", "service")
        store.add_mention(item_id, mentioned)
        store.add_entity_relation(mentioned, outside, "calls")
        bundle = store.export_item(item_id)
        assert bundle["relations"] == []
        assert {e["id"] for e in bundle["entities"]} == {mentioned}

    def test_export_item_excludes_relations_owned_by_a_different_item(self, store):
        """A relation recorded under another item's observation (source_item_id
        set to that other item) must not ride along either -- re-importing it
        here references an item that was never exported alongside it."""
        item_id = store.add_item("Doc", "content", "doc")
        other_item_id = store.add_item("Other", "content", "doc")
        e1 = store.add_entity("A", "service")
        e2 = store.add_entity("B", "service")
        store.add_mention(item_id, e1)
        store.add_mention(item_id, e2)
        store.add_entity_relation(e1, e2, "calls", source_item_id=other_item_id)
        bundle = store.export_item(item_id)
        assert bundle["relations"] == []

    def test_export_item_with_a_cross_referencing_relation_roundtrips_cleanly(self, store_factory):
        """End-to-end reproduction of the FK bug: exporting an item whose
        mentioned entity has a relation to an unexported entity must still
        re-import cleanly (the offending relation is simply dropped, not
        carried along to break the import)."""
        s1 = store_factory("cross_ref_src.db")
        item_id = s1.add_item("Doc", "content", "doc")
        mentioned = s1.add_entity("Svc", "service")
        outside = s1.add_entity("Unrelated", "service")
        s1.add_mention(item_id, mentioned)
        s1.add_entity_relation(mentioned, outside, "calls")
        bundle = s1.export_item(item_id)

        s2 = store_factory("cross_ref_dst.db")
        result = s2.import_bundle(bundle)
        assert result["items_imported"] == 1
        assert result["relations_rebuilt"] == 0
        assert s2.get_item(item_id) is not None

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
# 7b. Retirement of folders Kiro Crew registered itself
# ---------------------------------------------------------------------------


class TestRetireAutoRegisteredFolders:
    """A folder row left behind by the removed auto-registration paths.

    Those paths registered a directory nobody named, and the sweep re-validated its
    containment before every scan. Both are gone, so such a row must not be walked at
    all until the user adopts it. The refusal lives in ``FolderWatcher.scan_source``
    because that is the single funnel every scan goes through -- the watcher's sweep
    and both dashboard endpoints -- and deliberately NOT in the store constructor,
    where taking the write lock would stall the event loop at startup.
    """

    @staticmethod
    def _add(store, props, status="active", uri="/tmp/legacy", stype="local_folder"):
        sid = store.add_source("s", stype, uri, properties={**props, "sync_status": status})
        return sid

    @staticmethod
    def _row(store, sid):
        r = store.db.execute(
            "SELECT sync_status, properties FROM sources WHERE id = ?", (sid,)).fetchone()
        return r["sync_status"], json.loads(r["properties"] or "{}")

    @staticmethod
    def _watcher(store):
        from kiro_crew.knowledge.watcher import KnowledgeWatcher
        return KnowledgeWatcher(store=store, pipeline=object(), interval=1)

    # -- the funnel ---------------------------------------------------------

    @pytest.mark.asyncio
    async def test_a_scan_of_an_auto_registered_source_is_refused_and_retires_it(
            self, store, tmp_path):
        """The gate is in scan_source, so every caller is covered by construction.

        A database written before the removal holds such a row, and ``import_bundle``
        restores one verbatim into a running gateway, so the row can appear at any
        time -- not only at startup.
        """
        folder = tmp_path / "legacy"
        folder.mkdir()
        (folder / "note.md").write_text("# legacy\n\nbody\n" * 40)
        sid = self._add(store, {"auto_added": True}, uri=str(folder))
        fw = self._watcher(store)._folder_watcher
        fw._do_scan = AsyncMock()  # type: ignore[method-assign]

        stats = await fw.scan_source({"id": sid, "uri": str(folder),
                                      "source_type": "local_folder",
                                      "properties": json.dumps({"auto_added": True})})

        fw._do_scan.assert_not_awaited()
        assert stats["unconfirmed"] is True
        status, props = self._row(store, sid)
        assert status == "pending_confirmation"
        assert props["auto_registration_retired"] is True
        assert props["auto_added"] is True  # redaction + the gate still read it

    @pytest.mark.asyncio
    async def test_the_refusal_is_audited(self, store, tmp_path):
        """A refusal to walk a tree is a permission decision, so it is auditable.

        The paths this replaced logged both their auto-add and their scan-denied
        decisions, and this file's two TOCTOU refusals log theirs, so a silent refusal
        would be the one permission outcome with no record.
        """
        folder = tmp_path / "audited"
        folder.mkdir()
        sid = self._add(store, {"auto_added": True}, uri=str(folder))
        fw = self._watcher(store)._folder_watcher
        fw._do_scan = AsyncMock()  # type: ignore[method-assign]
        events: list[dict] = []

        class _Sel:
            @staticmethod
            def log_tool_invocation(**kw):
                events.append(kw)

        with patch("kiro_crew.knowledge.folder_watcher.sel", lambda: _Sel()):
            await fw.scan_source({"id": sid, "uri": str(folder),
                                  "source_type": "local_folder",
                                  "properties": json.dumps({"auto_added": True})})

        denied = [e for e in events
                  if e.get("tool_name") == "knowledge.source.scan_denied"]
        assert len(denied) == 1, events
        assert denied[0]["outcome"] == "denied"
        assert f"source_id={sid}" in denied[0]["resources"]
        assert "reason=auto_registered_unconfirmed" in denied[0]["resources"]
        # Records whether the row also moved, so a refusal that could not take the
        # write lock is distinguishable in the log from one that retired the row.
        assert "retired=True" in denied[0]["resources"]

    @pytest.mark.asyncio
    async def test_a_folder_the_user_added_scans_normally(self, store, tmp_path):
        folder = tmp_path / "mine"
        folder.mkdir()
        sid = self._add(store, {}, uri=str(folder))
        fw = self._watcher(store)._folder_watcher
        fw._do_scan = AsyncMock(return_value={"new": 0})  # type: ignore[method-assign]

        await fw.scan_source({"id": sid, "uri": str(folder),
                              "source_type": "local_folder", "properties": "{}"})

        fw._do_scan.assert_awaited_once()
        assert self._row(store, sid)[0] == "active"

    @pytest.mark.asyncio
    async def test_an_adopted_source_scans_normally(self, store, tmp_path):
        """Once the marker is on the row, the gate lets it through."""
        folder = tmp_path / "adopted"
        folder.mkdir()
        props = {"auto_added": True, "auto_registration_retired": True}
        sid = self._add(store, props, uri=str(folder))
        fw = self._watcher(store)._folder_watcher
        fw._do_scan = AsyncMock(return_value={"new": 0})  # type: ignore[method-assign]

        await fw.scan_source({"id": sid, "uri": str(folder),
                              "source_type": "local_folder",
                              "properties": json.dumps(props)})

        fw._do_scan.assert_awaited_once()
        assert self._row(store, sid)[0] == "active"

    @pytest.mark.asyncio
    async def test_the_sweep_refuses_it_too(self, store, tmp_path):
        """The sweep reaches the same gate rather than carrying its own copy."""
        folder = tmp_path / "swept"
        folder.mkdir()
        sid = self._add(store, {"auto_added": True}, uri=str(folder))
        watcher = self._watcher(store)
        watcher._folder_watcher._do_scan = AsyncMock()  # type: ignore[method-assign]

        await watcher._scan()

        watcher._folder_watcher._do_scan.assert_not_awaited()
        assert self._row(store, sid)[0] == "pending_confirmation"

    # -- the marker contract ------------------------------------------------

    @pytest.mark.parametrize("props", [
        {"auto_added": "false"},       # truthy string: must NOT look auto-added
        {"auto_added": 1},             # not a real boolean
        {"auto_added": True, "auto_registration_retired": "false"},  # fail-OPEN if truthy
        {"auto_added": True, "auto_registration_retired": 0},
    ])
    def test_markers_are_compared_strictly(self, props):
        """``properties`` is user-editable JSON and arrives through import_bundle.

        ``"false"`` is truthy, and on the retired marker that direction is fail-open:
        the row would read as adopted and skip the refusal entirely.
        """
        from kiro_crew.knowledge.store import is_auto_registered

        expected = (props.get("auto_added") is True
                    and props.get("auto_registration_retired") is not True)
        assert is_auto_registered(props) is expected

    def test_a_real_legacy_row_is_recognised(self):
        from kiro_crew.knowledge.store import is_auto_registered

        assert is_auto_registered({"auto_added": True}) is True
        assert is_auto_registered({"auto_added": True,
                                   "auto_registration_retired": True}) is False

    # -- the store primitive ------------------------------------------------

    def test_a_paused_row_keeps_its_pause_and_is_still_marked(self, store):
        """Pausing already stops the scan; overwriting it would lose user state."""
        sid = self._add(store, {"auto_added": True}, status="paused", uri="/tmp/paused")
        assert store.retire_auto_registered_folder(sid) is True
        status, props = self._row(store, sid)
        assert status == "paused"
        assert props["auto_registration_retired"] is True

    def test_the_aggregate_sources_are_out_of_scope(self, store):
        """They carry the marker and walk nothing, so they must not be gated."""
        agent = self._add(store, {"auto_added": True}, uri="agent://", stype="agent")
        artifact = self._add(store, {"auto_added": True}, uri="artifact://",
                             stype="artifact")
        for sid in (agent, artifact):
            assert store.retire_auto_registered_folder(sid) is False
            status, props = self._row(store, sid)
            assert status == "active", sid
            assert "auto_registration_retired" not in props, sid

    def test_retiring_is_idempotent(self, store):
        sid = self._add(store, {"auto_added": True}, uri="/tmp/once")
        assert store.retire_auto_registered_folder(sid) is True
        # Adopted by the user in between; a second call must not undo that.
        store.db.execute("UPDATE sources SET sync_status = 'active' WHERE id = ?", (sid,))
        store.db.commit()
        assert store.retire_auto_registered_folder(sid) is False
        assert self._row(store, sid)[0] == "active"

    def test_an_unreadable_properties_blob_is_not_retired_and_does_not_raise(self, store):
        sid = self._add(store, {}, uri="/tmp/broken")
        store.db.execute("UPDATE sources SET properties = ? WHERE id = ?",
                         ("{not json", sid))
        store.db.commit()
        assert store.retire_auto_registered_folder(sid) is False

    def test_an_ingestion_write_cannot_land_inside_the_retirement(self, store, tmp_path):
        """The retirement holds the write lock across its read and its write.

        Ingestion writes ``sync_status = 'synced'`` straight onto the row, so without
        the lock that write lands in between and the paused branch stamps the retired
        marker onto a row that has just become scannable again -- permanently, since
        the marker is what stops a later retirement.

        The racer opens through ``store.sqlite3`` -- the module the store itself bound
        -- because that is the library the real writer uses. A stdlib ``sqlite3``
        connection does not contend with a ``pysqlite3`` one on this database
        (measured: both sides took BEGIN IMMEDIATE at once), so a racer opened that way
        would land whether or not a lock is held, and the test would prove nothing.
        """
        from kiro_crew.knowledge import store as store_mod
        sqlite = store_mod.sqlite3

        db_path = str(tmp_path / "test.db")
        sid = self._add(store, {"auto_added": True}, status="paused", uri="/tmp/raced")
        real_loads = json.loads
        outcome: list[str] = []

        def ingestion_writes_mid_pass(raw):
            parsed = real_loads(raw)
            if not outcome and isinstance(parsed, dict) and parsed.get("auto_added"):
                conn = sqlite.connect(db_path, timeout=0.2, isolation_level=None)
                try:
                    conn.execute(
                        "UPDATE sources SET sync_status = 'synced' WHERE id = ?", (sid,))
                    outcome.append("landed")
                except sqlite.OperationalError as exc:
                    outcome.append(f"refused: {exc}")
                finally:
                    conn.close()
            return parsed

        with patch("kiro_crew.knowledge.store.json.loads", ingestion_writes_mid_pass):
            store.retire_auto_registered_folder(sid)

        assert outcome, "the mid-pass write never ran; the test proves nothing"
        assert outcome[0].startswith("refused"), outcome[0]
        status, props = self._row(store, sid)
        assert status == "paused"
        assert props["auto_registration_retired"] is True

    def test_a_lock_it_cannot_take_defers_instead_of_raising(self, store, tmp_path):
        """A busy database must not turn a refusal into an exception.

        The scan is refused either way -- the funnel's ``continue`` does not depend on
        this returning True -- so deferring costs nothing and raising would surface as
        a failed scan the user cannot act on.
        """
        from kiro_crew.knowledge import store as store_mod
        sqlite = store_mod.sqlite3

        db_path = str(tmp_path / "test.db")
        sid = self._add(store, {"auto_added": True}, uri="/tmp/busy")
        holder = sqlite.connect(db_path, timeout=30, isolation_level=None)
        try:
            holder.execute("BEGIN IMMEDIATE")
            store.db.execute("PRAGMA busy_timeout=50")
            assert store.retire_auto_registered_folder(sid) is False
            status, props = self._row(store, sid)
            assert status == "active"
            assert "auto_registration_retired" not in props
        finally:
            store.db.execute("PRAGMA busy_timeout=10000")
            holder.execute("ROLLBACK")
            holder.close()


class TestImportedFolderSourcesWaitForConfirmation:
    """``import_bundle`` may not hand the Library a directory that scans on arrival.

    The status allowlist admits ``active``, and a bundle is untrusted input that names
    its own ``uri``, so trusting the claimed status would let an imported row point at
    any readable directory and have the next sweep walk it and spend extraction calls
    on it. Importing a bundle is one decision; each directory inside it is not.
    """

    @staticmethod
    def _bundle(source):
        return {"version": 1, "sources": [source], "items": [], "entities": [],
                "relations": []}

    @staticmethod
    def _status(store, sid):
        return store.db.execute(
            "SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()[0]

    @pytest.mark.parametrize("claimed", ["active", "synced", "pending", "error", None])
    @pytest.mark.parametrize("stype", ["local_folder", "obsidian_vault"])
    def test_a_restored_walking_source_waits(self, store, tmp_path, claimed, stype):
        """Every status the sweep would scan is narrowed, including 'pending'."""
        sid = str(uuid4())
        src = {"id": sid, "name": "imported", "source_type": stype,
               "uri": str(tmp_path / "anywhere"), "properties": "{}"}
        if claimed is not None:
            src["sync_status"] = claimed
        store.import_bundle(self._bundle(src))
        assert self._status(store, sid) == "pending_confirmation"

    @pytest.mark.parametrize("stype", ["local_folder", "obsidian_vault"])
    def test_a_restored_paused_source_keeps_its_pause(self, store, tmp_path, stype):
        """Pausing already stops the scan, and it is the user's own decision.

        Overwriting it with a confirmation prompt would lose that state to protect
        against nothing -- the same reasoning the retirement path uses for a paused
        row.
        """
        sid = str(uuid4())
        store.import_bundle(self._bundle({
            "id": sid, "name": "paused", "source_type": stype,
            "uri": str(tmp_path / "paused"), "sync_status": "paused",
            "properties": "{}",
        }))
        assert self._status(store, sid) == "paused"

    def test_the_markers_do_not_change_that(self, store, tmp_path):
        """Not even a bundle claiming the source was already adopted.

        This is the shape the review raised: both markers true and an active status.
        The status is refused on the source TYPE, so the markers never get a say.
        """
        sid = str(uuid4())
        store.import_bundle(self._bundle({
            "id": sid, "name": "adopted?", "source_type": "local_folder",
            "uri": str(tmp_path / "claimed"), "sync_status": "active",
            "properties": json.dumps({"auto_added": True,
                                      "auto_registration_retired": True}),
        }))
        assert self._status(store, sid) == "pending_confirmation"

    @pytest.mark.parametrize("stype", ["local_file", "agent", "artifact", "wiki"])
    def test_a_source_that_walks_nothing_keeps_its_restored_status(self, store, stype):
        """The narrowing is scoped to walking sources, not to imports in general.

        An aggregate or single-file source has no tree to descend, so restoring it as
        active costs nothing and forcing confirmation on it would strand content the
        bundle legitimately carries.
        """
        sid = str(uuid4())
        store.import_bundle(self._bundle({
            "id": sid, "name": "agg", "source_type": stype, "uri": f"{stype}://x",
            "sync_status": "active", "properties": "{}",
        }))
        assert self._status(store, sid) == "active"


class TestWatcherRunLoop:
    """``KnowledgeWatcher.start`` / ``stop``: the background loop's own contract."""

    @staticmethod
    def _watcher(store, interval=0.01):
        from kiro_crew.knowledge.watcher import KnowledgeWatcher
        return KnowledgeWatcher(store=store, pipeline=object(), interval=interval)

    @pytest.mark.asyncio
    async def test_a_failing_sweep_does_not_kill_the_loop(self, store):
        """One bad sweep must cost one interval, not the whole watcher.

        The loop is the only thing that ever scans, so an exception escaping it leaves
        every source unscanned until the gateway restarts -- with nothing reporting
        that it stopped.
        """
        w = self._watcher(store)
        calls = []

        async def _scan():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("first sweep exploded")
            await w.stop()

        w._scan = _scan  # type: ignore[method-assign]
        await asyncio.wait_for(w.start(), timeout=5)
        assert len(calls) >= 2, "the loop did not survive the failing sweep"

    @pytest.mark.asyncio
    async def test_stop_ends_the_loop_without_waiting_out_the_interval(self, store):
        """``stop`` sets the event the interval wait is racing, so it returns at once."""
        w = self._watcher(store, interval=3600)
        w._scan = AsyncMock()  # type: ignore[method-assign]
        task = asyncio.create_task(w.start())
        await asyncio.sleep(0)  # let the first sweep run and enter the wait
        await w.stop()
        await asyncio.wait_for(task, timeout=5)
        w._scan.assert_awaited()


class TestScheduledDedupCadence:
    """``KnowledgeWatcher._maybe_dedup_sweep``: when it runs, and what it may delete.

    A scheduled pass differs in kind from a human-invoked one -- it deletes
    unattended -- so the first one in a process is a dry run whose findings are only
    logged. These pin the cadence gate and that preview-then-apply rule; a regression
    in either deletes documents on the first sweep after a restart.
    """

    @staticmethod
    def _watcher(store):
        from kiro_crew.knowledge.watcher import KnowledgeWatcher
        return KnowledgeWatcher(store=store, pipeline=object(), interval=1)

    @staticmethod
    def _cfg(every: int):
        cfg = MagicMock()
        cfg.knowledge.dedup_every_n_sweeps = every
        return cfg

    @pytest.mark.asyncio
    async def test_zero_disables_the_scheduled_pass(self, store):
        w = self._watcher(store)
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig") as cfg, \
                patch("kiro_crew.knowledge.watcher.dedup_sweep") as sweep:
            cfg.load.return_value = self._cfg(0)
            await w._maybe_dedup_sweep()
        sweep.assert_not_called()

    @pytest.mark.asyncio
    async def test_it_only_runs_on_the_cadence(self, store):
        w = self._watcher(store)
        w._sweep_count = 5  # 5 % 4 != 0
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig") as cfg, \
                patch("kiro_crew.knowledge.watcher.dedup_sweep") as sweep:
            cfg.load.return_value = self._cfg(4)
            await w._maybe_dedup_sweep()
        sweep.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_first_pass_previews_and_the_next_one_applies(self, store):
        """apply=False first, then apply=True -- and only exact matches either way.

        ``certain_only`` is not a tuning knob here: an unattended fuzzy collapse
        deletes the loser's copy, so a wrong match (same filename, cosine over the
        threshold, different facts) would cost a document its only text with nobody
        watching.
        """
        w = self._watcher(store)
        w._sweep_count = 4
        calls = []

        def _sweep(store_arg, apply, certain_only):
            calls.append({"apply": apply, "certain_only": certain_only})
            return [{"loser": "a", "winner": "b", "reason": "exact"}]

        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig") as cfg, \
                patch("kiro_crew.knowledge.watcher.dedup_sweep", _sweep):
            cfg.load.return_value = self._cfg(4)
            await w._maybe_dedup_sweep()
            assert w._dedup_applied_once is True
            await w._maybe_dedup_sweep()

        assert [c["apply"] for c in calls] == [False, True]
        assert all(c["certain_only"] for c in calls)

    @pytest.mark.asyncio
    async def test_a_failed_pass_does_not_arm_the_applying_one(self, store):
        """A raised sweep must not consume the preview: the next pass would delete
        without anything ever having been logged."""
        w = self._watcher(store)
        w._sweep_count = 4
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig") as cfg, \
                patch("kiro_crew.knowledge.watcher.dedup_sweep",
                      side_effect=RuntimeError("locked")):
            cfg.load.return_value = self._cfg(4)
            await w._maybe_dedup_sweep()  # contained, must not raise
        assert w._dedup_applied_once is False

    @pytest.mark.asyncio
    async def test_an_unreadable_cadence_setting_skips_the_pass(self, store):
        w = self._watcher(store)
        with patch("kiro_crew.knowledge.watcher.KiroCrewConfig") as cfg, \
                patch("kiro_crew.knowledge.watcher.dedup_sweep") as sweep:
            cfg.load.side_effect = RuntimeError("no config")
            await w._maybe_dedup_sweep()
        sweep.assert_not_called()


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

    def test_parse_response_prose_wrapped(self):
        ext = EntityExtractor()
        raw = 'Some preamble text {"entities": [], "relations": [], "category": "runbook", "summary": "ok"} trailing'
        result = ext._parse_response(raw)
        assert result["category"] == "runbook"

    def test_parse_response_garbage(self):
        ext = EntityExtractor()
        result = ext._parse_response("totally invalid garbage")
        assert result == {"title": "", "entities": [], "relations": [], "category": "document", "summary": ""}

    def test_parse_response_stray_brace_in_prose(self):
        # The old greedy first-'{'-to-last-'}' regex spanned from the
        # {placeholder} aside to the trailing "{}" echo, so the slice never
        # parsed and a valid payload was silently lost.
        ext = EntityExtractor()
        raw = (
            'Per the {name, type} shape: {"entities": [], "relations": [], '
            '"category": "runbook", "summary": "ok"} — use {} when empty.'
        )
        result = ext._parse_response(raw)
        assert result["category"] == "runbook"
        assert result["summary"] == "ok"

    def test_parse_response_non_dict_reply_is_empty(self):
        # A top-level array reply must yield the empty result, not leak an
        # AttributeError out of _validate (which nuked a whole extract_batch
        # under the old direct json.loads path).
        ext = EntityExtractor()
        raw = '[{"entities": []}]'
        result = ext._parse_response(raw)
        assert result == {"title": "", "entities": [], "relations": [], "category": "document", "summary": ""}

    def test_parse_response_two_different_payloads_refuse_the_guess(self):
        # The shared extractor's ambiguity contract: two DIFFERENT
        # payload-shaped dicts mean the caller cannot know which is real.
        ext = EntityExtractor()
        raw = '{"summary": "first"} or maybe {"summary": "second"}'
        result = ext._parse_response(raw)
        assert result == {"title": "", "entities": [], "relations": [], "category": "document", "summary": ""}

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

    def embed_for_item(self, title, summary, content=None, *, priority=PRIORITY_NORMAL):
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
            def embed_for_item(self, title, summary, content=None, *, priority=PRIORITY_NORMAL):
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
            def embed_for_item(self, title, summary, content=None, *, priority=PRIORITY_NORMAL):
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

    def embed_for_item(self, title, summary, content=None, *, priority=PRIORITY_NORMAL):
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

            def embed_for_item(self, title, summary, content=None, *, priority=PRIORITY_NORMAL):
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


# ---------------------------------------------------------------------------
# EntityExtractor -- untrusted-chunk nonce-suffixed delimiters (CWE-94)
# ---------------------------------------------------------------------------


class TestEntityExtractorNonceDelimiters:
    """The extractor wraps each untrusted chunk in per-call nonce-suffixed
    delimiters so the boundary cannot be forged by content embedding a legacy
    static delimiter."""

    def test_nonce_markers_wrap_chunk_and_survive_forged_delimiter(self):
        import asyncio

        class CapturePool:
            def __init__(self):
                self.prompt: str | None = None

            async def send(self, prompt, timeout=60.0):
                self.prompt = prompt
                return "{}"

            async def send_batch(self, prompts, timeout=60.0):
                return [await self.send(p, timeout) for p in prompts]

        pool = CapturePool()
        ext = EntityExtractor(pool=pool)
        # A benign chunk that embeds the *legacy static* end marker must not
        # break prompt formatting; the real boundary is nonce-suffixed.
        chunk = "benign notes mentioning a fake <<<END_UNTRUSTED_CHUNK>>> token inline"
        asyncio.get_event_loop().run_until_complete(ext.extract(chunk))
        assert pool.prompt is not None
        assert chunk in pool.prompt
        assert "<<<BEGIN_UNTRUSTED_CHUNK_" in pool.prompt
        assert "<<<END_UNTRUSTED_CHUNK_" in pool.prompt

    def test_batch_path_wraps_each_chunk_with_distinct_per_chunk_nonces(self):
        # Ingestion drives extract_batch (not extract), so the batch path must
        # apply the same per-chunk nonce-suffixed delimiters -- and each chunk
        # must get its OWN nonce (per-chunk uuid), not a shared one.
        import asyncio
        import re

        class CapturePool:
            def __init__(self):
                self.prompts: list[str] = []

            async def send(self, prompt, timeout=60.0):
                return "{}"

            async def send_batch(self, prompts, timeout=60.0):
                # Record ALL prompts passed to the batch send.
                self.prompts = list(prompts)
                return ["{}" for _ in prompts]

        pool = CapturePool()
        ext = EntityExtractor(pool=pool)
        chunks = [
            "first chunk with a forged <<<END_UNTRUSTED_CHUNK>>> marker inline",
            "second chunk of untrusted content",
        ]
        asyncio.get_event_loop().run_until_complete(ext.extract_batch(chunks))

        assert len(pool.prompts) == 2
        nonce_re = re.compile(r"<<<BEGIN_UNTRUSTED_CHUNK_([0-9a-f]+)>>>")
        nonces = []
        for chunk, prompt in zip(chunks, pool.prompts):
            # The chunk content is present and wrapped in matching nonce markers.
            assert chunk in prompt
            begin = nonce_re.search(prompt)
            assert begin is not None, "no begin nonce marker in batch prompt"
            nonce = begin.group(1)
            assert f"<<<BEGIN_UNTRUSTED_CHUNK_{nonce}>>>" in prompt
            assert f"<<<END_UNTRUSTED_CHUNK_{nonce}>>>" in prompt
            nonces.append(nonce)
        # Per-chunk uuid: the two chunks must NOT share a nonce.
        assert nonces[0] != nonces[1], "each chunk must get a distinct per-chunk nonce"


# ---------------------------------------------------------------------------
# SyncScheduler.sync_all -- errored sources must be quiesced (issue #3946)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSyncAllSkipsErroredSources:
    """sync_all must skip an errored source, whichever writer marked it.

    KnowledgeIngestion and SyncScheduler both mark failure in the sync_status
    COLUMN, which is the only store sync_all reads. Rows errored before the
    column existed carry the state in their properties JSON, which cannot be
    ordered against the column and so is never promoted onto it; such a row is
    polled until an attempt of its own fails, and that failure writes the column
    (issue #3946).
    """

    def _scheduler(self, store):
        scheduler = SyncScheduler(store, pipeline=None, connectors={})
        attempted: list[str] = []

        async def _spy(source_id: str) -> dict:
            attempted.append(source_id)
            return {"synced": False, "items_created": 0, "error": None}

        scheduler.sync_source = _spy  # type: ignore[method-assign]
        return scheduler, attempted

    async def test_column_only_error_is_skipped(self, store):
        # A healthy source that should still be attempted.
        ok_id = store.add_source("Healthy", "local_file", "/tmp/ok")
        # A source errored the way ingestion.py does it: COLUMN only, no
        # sync_status entry in the properties JSON.
        err_id = store.add_source("Dead", "local_file", "/tmp/dead")
        store.db.execute("UPDATE sources SET sync_status = 'error' WHERE id = ?", (err_id,))
        store.db.commit()
        # Guard: the failing writer really left the JSON untouched.
        row = store.db.execute("SELECT properties FROM sources WHERE id = ?", (err_id,)).fetchone()
        assert json.loads(row["properties"] or "{}").get("sync_status") is None

        scheduler, attempted = self._scheduler(store)
        await scheduler.sync_all()

        assert err_id not in attempted, "column-only errored source must be skipped"
        assert ok_id in attempted, "healthy source must still be synced"

    async def test_legacy_json_only_error_is_quiesced_by_its_first_failure(self, store, tmp_path):
        """A pre-column errored row is polled until an attempt of its own fails.

        Its state lives in the properties blob only, which cannot be ordered
        against the column, so the store does not promote it -- promoting would
        mark a source errored that had in fact recovered. It is therefore polled
        like any healthy source, and the first attempt that FAILS is what quiesces
        it: ``_record_failure`` reads ``consecutive_failures`` from the blob, which
        such a row already carries at or above MAX_FAILURES, so that one failure
        writes the column and the source is skipped from then on.
        """
        err_id = str(uuid4())
        ok_id = str(uuid4())
        now = datetime.now().isoformat()
        for sid, uri, props_json in (
            (err_id, "/tmp/legacy",
             json.dumps({"sync_status": "error", "consecutive_failures": 3})),
            (ok_id, "/tmp/legacy-ok", json.dumps({})),
        ):
            # local_folder: the reopen also runs the orphan cleanup, which
            # deletes item-less sources of every other type.
            store.db.execute(
                "INSERT INTO sources (id, name, source_type, uri, properties, sync_status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, "LegacyDead", "local_folder", uri, props_json, "pending", now, now))
        store.db.commit()
        store.close()

        reopened = KnowledgeStore(str(tmp_path / "test.db"))
        try:
            scheduler, attempted = self._scheduler(reopened)
            await scheduler.sync_all()
            assert attempted == [err_id, ok_id] or set(attempted) == {err_id, ok_id}, (
                "both legacy rows are polled, the blob-errored one included")

            # An attempt that FAILS is what writes the column.
            scheduler._record_failure(err_id)
            assert reopened.db.execute(
                "SELECT sync_status FROM sources WHERE id = ?",
                (err_id,)).fetchone()["sync_status"] == "error"

            attempted.clear()
            await scheduler.sync_all()
            assert err_id not in attempted, "an errored column must never be retried"
            assert ok_id in attempted, "healthy source must still be synced"
        finally:
            reopened.close()


class TestCjkKeywordRecall:
    """CJK recall on the FTS keyword leg (issue #3691).

    Vocabulary is shared with ``TestCjkSearch`` in test_history.py so the two
    search surfaces are read against the same examples. The query is the
    four-character Chinese phrase for "memory leak"; the decoys reuse its four
    characters inside other words ("internal", "to save", "relief valve",
    "water leak") without ever spelling either half of the query.

    Strings are written as escapes because the repository forbids literal
    Chinese in source; each is glossed in English beside it.
    """

    LEAK = "\u5185\u5b58\u6cc4\u6f0f"  # "memory leak" (4 chars: memory + leak)
    MODEL = "\u6a21\u578b"  # "model", an ordinary two-character word

    # "investigated the data-leak problem in memory today" -- holds both halves
    # of LEAK as adjacent pairs, but spelled apart in the sentence.
    DOC_APART = "\u4eca\u5929\u8c03\u67e5\u4e86\u5185\u5b58\u91cc\u7684\u6570\u636e\u6cc4\u6f0f\u95ee\u9898"
    # "finished locating the memory leak" -- holds the whole run verbatim.
    DOC_RUN = "\u5185\u5b58\u6cc4\u6f0f\u5b9a\u4f4d\u5b8c\u6210\u4e86"
    # "a record of the internal relief valve and the water leak" -- reuses all
    # four characters of LEAK, but spells neither "memory" nor "leak".
    DOC_DECOY = "\u5185\u90e8\u4fdd\u5b58\u4e86\u6cc4\u538b\u9600\u548c\u6f0f\u6c34\u7684\u8bb0\u5f55"
    # "the user decided to use this model for inference"
    DOC_MODEL = "\u7528\u6237\u51b3\u5b9a\u7528\u8fd9\u4e2a\u6a21\u578b\u6765\u505a\u63a8\u7406"

    @staticmethod
    def _titles(results):
        return sorted(r["title"] for r in results)

    @staticmethod
    def _make_legacy_index(store, title, content, tags="[]"):
        """Rewrite one row's index entry the pre-fix way and clear the marker.

        Reproduces a database written before this change: raw (un-segmented)
        terms, and ``user_version`` back at 0. The CREATE statement is identical
        either way, which is exactly why the marker is what distinguishes them.
        """
        store.ensure_fts_index_current()
        rowid = store.db.execute(
            "SELECT rowid FROM items WHERE title = ?", (title,)).fetchone()[0]
        store.db.execute("INSERT INTO items_fts (items_fts) VALUES ('delete-all')")
        store.db.execute(
            "INSERT INTO items_fts (rowid, title, content, tags) VALUES (?, ?, ?, ?)",
            (rowid, title, content, tags))
        store.db.execute("PRAGMA user_version = 0")

    def test_store_fts_finds_spaceless_cjk_query(self, store):
        """The reported bug: a spaceless CJK query returned nothing at all."""
        store.add_item("apart", self.DOC_APART, "note")
        store.add_item("run", self.DOC_RUN, "note")
        results = store.search_items_fts(self.LEAK)
        assert self._titles(results) == ["apart", "run"]

    def test_store_fts_excludes_scattered_characters(self, store):
        """Recall must not be bought with a character-soup match."""
        store.add_item("run", self.DOC_RUN, "note")
        store.add_item("decoy", self.DOC_DECOY, "note")
        assert self._titles(store.search_items_fts(self.LEAK)) == ["run"]

    def test_store_fts_finds_two_character_cjk_word(self, store):
        """Two characters is an ordinary word length in CJK, not an edge case."""
        store.add_item("model", self.DOC_MODEL, "note")
        assert self._titles(store.search_items_fts(self.MODEL)) == ["model"]

    def test_store_fts_matches_cjk_in_title(self, store):
        store.add_item(self.LEAK, "plain ascii body", "note")
        assert self._titles(store.search_items_fts(self.LEAK)) == [self.LEAK]

    def test_cjk_tags_are_stored_ascii_escaped(self, store):
        """Known limitation, out of this fix's reach: the tags COLUMN is escaped.

        ``add_item`` persists tags with ``json.dumps`` at its default
        ``ensure_ascii=True``, so a CJK tag is stored with its characters
        backslash-escaped and the index receives the terms ``u6a21``/``u578b``.
        No query-side change can reach a CJK tag, because the CJK never arrives
        in the column. Pinned here so the boundary of this fix is explicit and a
        later change to the column encoding has a test that must be updated
        deliberately.
        """
        store.add_item("tagged", "plain ascii body", "note", tags=[self.MODEL])
        stored = store.db.execute("SELECT tags FROM items").fetchone()[0]
        assert "\\u6a21" in stored
        assert store.search_items_fts(self.MODEL) == []

    def test_retriever_keyword_leg_finds_spaceless_cjk_query(self, store):
        """The hybrid path with no embedder, so only the keyword leg can answer."""
        store.add_item("run", self.DOC_RUN, "note")
        store.add_item("unrelated", "\u5b8c\u5168\u65e0\u5173\u7684\u8bdd\u9898", "note")
        results = HybridRetriever(store).search(self.LEAK)
        assert self._titles(results) == ["run"]
        assert "keyword" in results[0]["match_type"]

    def test_retriever_mixed_script_query_matches_both_halves(self, store):
        store.add_item("mixed", "kirocrew \u7684\u90e8\u7f72\u6d41\u7a0b\u8bb0\u5f55", "note")
        store.add_item("cjk_only", "\u90e8\u7f72\u6d41\u7a0b\u8bb0\u5f55", "note")
        results = HybridRetriever(store).search("kirocrew\u90e8\u7f72")
        assert self._titles(results) == ["mixed"]

    def test_update_item_leaves_no_stale_cjk_hit(self, store):
        """A CJK-segmented index must be un-indexed with segmented terms.

        FTS5's 'delete' command subtracts the terms it is handed, and
        'integrity-check' does not report a mismatch -- so a raw-text delete
        against a segmented index silently keeps serving the old content.
        """
        item_id = store.add_item("doc", self.DOC_RUN, "note")
        assert store.search_items_fts(self.LEAK)
        store.update_item(item_id, content="\u5b8c\u5168\u65e0\u5173\u7684\u8bdd\u9898")
        assert store.search_items_fts(self.LEAK) == []

    def test_delete_item_leaves_no_stale_cjk_hit(self, store):
        item_id = store.add_item("doc", self.DOC_RUN, "note")
        assert store.search_items_fts(self.LEAK)
        store.delete_item(item_id)
        assert store.search_items_fts(self.LEAK) == []

    def test_ascii_search_behaviour_is_unchanged(self, store):
        """Segmentation touches CJK only: no substring matching leaks into ASCII."""
        store.add_item("Auth Design", "JWT tokens with refresh flow", "design_doc")
        store.add_item("DB Schema", "DynamoDB table layout", "design_doc")
        assert self._titles(store.search_items_fts("JWT")) == ["Auth Design"]
        # "oke" is a substring of "tokens" and must NOT match, the way a trigram
        # tokenizer would have made it.
        assert store.search_items_fts("oke") == []
        results = HybridRetriever(store).search("JWT")
        assert results[0]["title"] == "Auth Design"

    def test_snippet_source_text_is_not_segmented(self, store):
        """Only the index copy is segmented; the stored item keeps its own text."""
        item_id = store.add_item("doc", self.DOC_RUN, "note")
        assert store.get_item(item_id)["content"] == self.DOC_RUN
        assert store.search_items_fts(self.LEAK)[0]["content"] == self.DOC_RUN

    def test_graph_leg_finds_entity_named_inside_a_cjk_run(self, store):
        """An entity name inside a spaceless run is unreachable by a whitespace split."""
        item_id = store.add_item("doc", self.DOC_RUN, "note")
        eid = store.add_entity("\u5185\u5b58", "component")  # "memory"
        store.add_mention(item_id, eid, "\u5185\u5b58")
        results = HybridRetriever(store).search(self.LEAK)
        assert [r["title"] for r in results] == ["doc"]

    def test_migrating_reader_and_concurrent_writer_do_not_deadlock(self, tmp_path):
        """A rebuild must not deadlock against a writer that owns SQLite's lock.

        The inversion this guards: the rebuilding reader holds a Python lock and
        then wants SQLite's writer lock, while a writer already owns SQLite's and
        wants the Python one. Neither can proceed, so both sit until
        busy_timeout (10s) and the event-loop writer returns 500. The fix is that
        the FTS write path takes no Python lock at all -- writes are serialized
        by SQLite, which is also the only thing that works across processes.
        """
        path = str(tmp_path / "deadlock.db")
        first = KnowledgeStore(path)
        try:
            for i in range(60):
                first.add_item(f"doc{i}", self.DOC_RUN, "note")
            keep = first.add_item("keep", self.DOC_RUN, "note")
            rows = first.db.execute(
                "SELECT rowid, title, content, tags FROM items").fetchall()
            first.db.execute("INSERT INTO items_fts (items_fts) VALUES ('delete-all')")
            first.db.execute("BEGIN IMMEDIATE")
            for r in rows:
                first.db.execute(
                    "INSERT INTO items_fts (rowid,title,content,tags) VALUES (?,?,?,?)",
                    (r["rowid"], r["title"], r["content"], r["tags"]))
            first.db.execute("PRAGMA user_version = 0")
            first.db.execute("COMMIT")
        finally:
            first.close()

        store = KnowledgeStore(path)
        errors: list[BaseException] = []
        done: list[str] = []

        def reader():
            try:
                store.search_items_fts(self.LEAK, limit=100)
                done.append("reader")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def writer():
            try:
                for _ in range(12):
                    store.update_item(keep, summary="x")
                done.append("writer")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=reader), threading.Thread(target=writer)]
        try:
            for t in threads:
                t.start()
            # Generous vs the ~0.1s this takes, far under sqlite's 10s busy_timeout,
            # so a hang here is the deadlock and not slowness.
            for t in threads:
                t.join(timeout=30)
            assert not [t for t in threads if t.is_alive()], "deadlocked"
            assert not errors, f"raised: {errors!r}"
            assert sorted(done) == ["reader", "writer"]
            store.db.execute("INSERT INTO items_fts (items_fts) VALUES ('integrity-check')")
        finally:
            store.close()

    def test_a_failed_migration_degrades_instead_of_faulting(self, store, monkeypatch):
        """A migration that cannot take the writer lock must not fault the read.

        The rebuild opens BEGIN IMMEDIATE, so a concurrent long import can hold
        the lock past busy_timeout and make it raise OperationalError. Every
        caller is a reader, and a reader's own query already degrades on that
        error, so letting it escape from the migration turns a transient lock
        into an HTTP 500 -- and the dashboard's entity lookup runs its query
        with no guard at all, so there it surfaces directly.

        The failure is injected on the instance rather than produced with a real
        second connection: the contract under test is which exceptions cross the
        migration boundary, and a real lock drags in cross-connection timing
        that has nothing to do with it.

        The raised class is read from the *method's own* ``__globals__`` rather
        than from any import: ``except sqlite3.OperationalError`` resolves the
        ``sqlite3`` name in the defining module's namespace, and that module
        prefers ``pysqlite3`` with a standard-library fallback, so the class the
        catch will compare against is whatever THAT namespace holds. Re-importing
        the module by name can hand back a different object, and then the
        injected error misses the catch and the test fails on import order.
        """
        catches = KnowledgeStore.ensure_fts_index_current.__globals__["sqlite3"]

        attempts = []

        def locked():
            attempts.append(1)
            raise catches.OperationalError("database is locked")

        monkeypatch.setattr(store, "_migrate_fts_index", locked)

        # Swallowed, so the reader that called it can serve the legacy index...
        store._fts_index_current = False
        store.ensure_fts_index_current()
        # ...and NOT latched, so the next reader tries again once the lock clears.
        store._fts_index_current = False
        store.ensure_fts_index_current()
        assert len(attempts) == 2, "a failed migration latched; it must be retried"

        # A corrupt database is NOT a transient lock and must still propagate:
        # OperationalError is a subclass of DatabaseError, not the reverse, so
        # this asserts the catch is narrow rather than a blanket except.
        def corrupt():
            raise catches.DatabaseError("database disk image is malformed")

        monkeypatch.setattr(store, "_migrate_fts_index", corrupt)
        store._fts_index_current = False
        with pytest.raises(catches.DatabaseError):
            store.ensure_fts_index_current()

    def test_every_fts_reader_routes_through_the_migration_check(self):
        """The swallow above is only load-bearing if every reader goes through it.

        Checked structurally because the alternative -- driving all three
        readers against a genuinely locked database -- tests SQLite's timing
        rather than this PR's control flow.
        """
        import inspect

        from kiro_crew.dashboard.handlers import knowledge as handler_mod
        from kiro_crew.knowledge import retrieval as retrieval_mod
        from kiro_crew.knowledge import store as store_mod

        readers = {
            "search_items_fts": store_mod.KnowledgeStore.search_items_fts,
            "_keyword_search": retrieval_mod.HybridRetriever._keyword_search,
            "_entity_items_rows": handler_mod._entity_items_rows,
        }
        missing = [
            name for name, fn in readers.items()
            if "ensure_fts_index_current" not in inspect.getsource(fn)
        ]
        assert not missing, (
            f"FTS reader(s) {missing} do not trigger the index migration; a legacy "
            "database would serve them stale CJK terms"
        )

    def test_every_items_fts_write_goes_through_the_wrappers(self):
        """The funnel is load-bearing, so enforce it rather than trust it.

        A writer that bypasses `_fts_index`/`_fts_unindex` reintroduces exactly
        the bug this class pins: the wrong term representation either serves
        deleted content as live hits -- which FTS5's 'integrity-check' does NOT
        report -- or raises 'database disk image is malformed'. Neither failure
        points at the call site that caused it, so the invariant has to be
        checked mechanically.
        """
        import inspect

        from kiro_crew.knowledge import store as store_mod

        module_writes = len(re.findall(r"INSERT INTO items_fts", inspect.getsource(store_mod)))
        owned = sum(
            len(re.findall(r"INSERT INTO items_fts", inspect.getsource(fn)))
            for fn in (
                store_mod.KnowledgeStore._fts_index,
                store_mod.KnowledgeStore._fts_unindex,
                # The version-gated rebuild owns the 'delete-all' index reset.
                store_mod.KnowledgeStore._migrate_fts_index,
            )
        )
        assert module_writes == owned, (
            f"{module_writes - owned} raw 'INSERT INTO items_fts' outside "
            "_fts_index/_fts_unindex/_migrate_fts_index; route it through the wrappers"
        )
        assert owned >= 3, "wrappers lost their writes; this guard would pass vacuously"

    def test_every_store_transaction_is_begin_immediate(self):
        """`_fts_terms_segmented`'s correctness rests on this, so pin it.

        The representation is read without a Python lock, safe only because the
        reader already owns SQLite's writer lock. A future writer opening a plain
        deferred ``BEGIN`` would silently re-open the race, and prose in a
        docstring cannot catch that.
        """
        import inspect

        from kiro_crew.knowledge import store as store_mod

        source = inspect.getsource(store_mod)
        bare = re.findall(r"""execute\(\s*["']BEGIN["']\s*\)""", source)
        assert bare == [], f"{len(bare)} deferred BEGIN(s) in store.py; use BEGIN IMMEDIATE"
        assert 'execute("BEGIN IMMEDIATE")' in source

    def test_entity_items_lookup_matches_a_cjk_entity_name(self, tmp_path):
        """The third FTS reader lives in the handler and builds its own query.

        Quoting the whole entity name matches nothing against a
        character-segmented index, so a CJK entity name would silently return no
        items on this endpoint.
        """
        from kiro_crew.dashboard.handlers.knowledge import _entity_items_rows

        store = KnowledgeStore(str(tmp_path / "entity.db"))
        try:
            store.add_item("run", self.DOC_RUN, "note")
            store.add_item("other", "plain ascii body", "note")
            rows = _entity_items_rows(store, "\u5185\u5b58")  # "memory"
            assert [r["title"] for r in rows] == ["run"]
            # ASCII entity names keep working.
            assert [r["title"] for r in _entity_items_rows(store, "ascii")] == ["other"]
            assert _entity_items_rows(store, "   ") == []
        finally:
            store.close()

    def test_entity_items_lookup_keeps_multiword_ascii_adjacent(self, tmp_path):
        """A multi-word ASCII entity name stays a PHRASE, as it was before.

        The old handler quoted the whole name, so "New York" required those words
        adjacent. Splitting the name into AND-ed terms would quietly loosen that
        to "both words present anywhere", which is a different (and wrong) answer
        for an entity name.
        """
        from kiro_crew.dashboard.handlers.knowledge import _entity_items_rows

        store = KnowledgeStore(str(tmp_path / "phrase.db"))
        try:
            store.add_item("adjacent", "a trip to New York next week", "note")
            store.add_item("scattered", "New arrivals shipped to York later", "note")
            rows = _entity_items_rows(store, "New York")
            assert [r["title"] for r in rows] == ["adjacent"]
        finally:
            store.close()

    def test_entity_items_lookup_mixed_script_name(self, tmp_path):
        from kiro_crew.dashboard.handlers.knowledge import _entity_items_rows

        store = KnowledgeStore(str(tmp_path / "mixed_entity.db"))
        try:
            store.add_item("mixed", "kirocrew \u90e8\u7f72\u6d41\u7a0b\u8bb0\u5f55", "note")
            store.add_item("apart", "\u90e8\u7f72 then separately kirocrew", "note")
            rows = _entity_items_rows(store, "kirocrew \u90e8\u7f72")
            assert [r["title"] for r in rows] == ["mixed"]
        finally:
            store.close()

    def test_legacy_write_before_any_search_does_not_corrupt(self, tmp_path):
        """A writer must not hand segmented terms to a not-yet-migrated index.

        FTS5's 'delete' subtracts the exact terms it is given, so a segmented
        delete against raw terms raises 'database disk image is malformed'. On a
        legacy database there are writers that run before any reader can migrate
        it: the orphan reclaim inside _migrate (so, inside __init__), and the
        startup watcher sweep updating or deleting an item before the first
        search. So writes follow the representation the database declares.
        """
        path = str(tmp_path / "legacy_write.db")
        first = KnowledgeStore(path)
        try:
            item_id = first.add_item("run", self.DOC_RUN, "note")
            self._make_legacy_index(first, "run", self.DOC_RUN)
        finally:
            first.close()

        store = KnowledgeStore(path)
        try:
            assert store._fts_terms_segmented() is False
            # The update that used to raise. Both halves of the FTS sync run here.
            store.update_item(item_id, content="\u5b8c\u5168\u65e0\u5173\u7684\u8bdd\u9898")
            store.db.execute("INSERT INTO items_fts (items_fts) VALUES ('integrity-check')")
            # A delete on the same legacy index must also survive.
            second = store.add_item("run2", self.DOC_RUN, "note")
            store.delete_item(second)
            store.db.execute("INSERT INTO items_fts (items_fts) VALUES ('integrity-check')")
            # And the migration still lands correctly afterwards.
            store.add_item("run3", self.DOC_RUN, "note")
            assert "run3" in self._titles(store.search_items_fts(self.LEAK, limit=50))
            assert store._fts_terms_segmented() is True
        finally:
            store.close()

    def test_legacy_delete_of_cjk_item_does_not_raise(self, tmp_path):
        """The precise shape GPT flagged: delete a CJK item on a v0 database."""
        path = str(tmp_path / "legacy_delete.db")
        first = KnowledgeStore(path)
        try:
            item_id = first.add_item(self.LEAK, self.DOC_RUN, "note")
            self._make_legacy_index(first, self.LEAK, self.DOC_RUN)
        finally:
            first.close()

        store = KnowledgeStore(path)
        try:
            store.delete_item(item_id)  # used to raise DatabaseError
            store.db.execute("INSERT INTO items_fts (items_fts) VALUES ('integrity-check')")
            assert store.get_item(item_id) is None
        finally:
            store.close()

    def test_migration_declares_segmented_before_reinserting(self, tmp_path):
        """The rebuild writes through _fts_index while user_version is still old.

        If the declaration were flipped only after the commit, the rebuild would
        re-insert RAW terms and the migration would be a no-op that still bumped
        the marker -- leaving CJK permanently unsearchable on every upgraded
        database, with nothing to retry it.
        """
        path = str(tmp_path / "declare.db")
        first = KnowledgeStore(path)
        try:
            first.add_item("run", self.DOC_RUN, "note")
            self._make_legacy_index(first, "run", self.DOC_RUN)
        finally:
            first.close()

        store = KnowledgeStore(path)
        try:
            store.ensure_fts_index_current()
            # Segmented terms are what a per-character phrase query needs.
            assert self._titles(store.search_items_fts(self.LEAK)) == ["run"]
        finally:
            store.close()

    def test_construction_does_not_rebuild_the_index(self, tmp_path):
        """The rebuild must not run in __init__.

        KnowledgeStore is constructed on the gateway event-loop thread (see the
        threading note in __init__, and setup_knowledge_routes reading the lazy
        state.knowledge_store property at boot), while its FTS readers run on
        worker threads. A data-scaled reindex in the constructor would stall the
        gateway at startup for the length of a full reindex of the corpus.
        """
        path = str(tmp_path / "lazy.db")
        first = KnowledgeStore(path)
        try:
            first.add_item("run", self.DOC_RUN, "note")
            self._make_legacy_index(first, "run", self.DOC_RUN)
        finally:
            first.close()

        reopened = KnowledgeStore(path)
        try:
            # Constructed, but nothing re-indexed and the marker untouched.
            assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == 0
            assert reopened._fts_index_current is False
            # The first reader migrates, on the reader's thread.
            assert self._titles(reopened.search_items_fts(self.LEAK)) == ["run"]
            assert reopened._fts_index_current is True
            from kiro_crew.knowledge.store import FTS_INDEX_VERSION
            assert reopened.db.execute(
                "PRAGMA user_version").fetchone()[0] == FTS_INDEX_VERSION
        finally:
            reopened.close()

    def test_rebuild_runs_once_across_concurrent_readers(self, tmp_path):
        """Concurrent readers must not each start their own rebuild."""
        path = str(tmp_path / "concurrent.db")
        first = KnowledgeStore(path)
        try:
            first.add_item("run", self.DOC_RUN, "note")
            self._make_legacy_index(first, "run", self.DOC_RUN)
        finally:
            first.close()

        store = KnowledgeStore(path)
        calls = []
        real = store._migrate_fts_index

        def counting():
            calls.append(1)
            real()

        store._migrate_fts_index = counting  # type: ignore[method-assign]
        try:
            results = []
            threads = [
                threading.Thread(
                    target=lambda: results.append(store.search_items_fts(self.LEAK)))
                for _ in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert len(calls) == 1, f"rebuilt {len(calls)} times, expected once"
            assert all(self._titles(r) == ["run"] for r in results)
        finally:
            store.close()

    def test_retriever_leg_migrates_a_legacy_index(self, tmp_path):
        """The retriever keyword leg is the other reader and must migrate too."""
        path = str(tmp_path / "legacy_retriever.db")
        first = KnowledgeStore(path)
        try:
            first.add_item("run", self.DOC_RUN, "note")
            self._make_legacy_index(first, "run", self.DOC_RUN)
        finally:
            first.close()

        store = KnowledgeStore(path)
        try:
            results = HybridRetriever(store).search(self.LEAK)
            assert self._titles(results) == ["run"]
        finally:
            store.close()

    def test_legacy_index_is_rebuilt_on_open(self, tmp_path):
        """A database written before this change carries un-segmented terms.

        Its CREATE statement is byte-identical to the new one, so the rebuild is
        gated on PRAGMA user_version rather than a schema probe.
        """
        from kiro_crew.knowledge.store import FTS_INDEX_VERSION

        path = str(tmp_path / "legacy.db")
        first = KnowledgeStore(path)
        try:
            first.add_item("run", self.DOC_RUN, "note")
            self._make_legacy_index(first, "run", self.DOC_RUN)
            # Pre-fix state: the query the fix exists to serve finds nothing.
            first._fts_index_current = True  # suppress the lazy migration
            assert first.search_items_fts(self.LEAK) == []
        finally:
            first.close()

        reopened = KnowledgeStore(path)
        try:
            assert self._titles(reopened.search_items_fts(self.LEAK)) == ["run"]
            assert reopened.db.execute(
                "PRAGMA user_version").fetchone()[0] == FTS_INDEX_VERSION
        finally:
            reopened.close()

    def test_rebuild_spans_more_than_one_batch(self, tmp_path, monkeypatch):
        """The rebuild is batched; the batch boundary must not drop a row."""
        path = str(tmp_path / "many.db")
        monkeypatch.setattr(KnowledgeStore, "_FTS_REBUILD_BATCH", 2)
        first = KnowledgeStore(path)
        try:
            for i in range(5):
                first.add_item(f"doc{i}", self.DOC_RUN, "note")
            first.db.execute("PRAGMA user_version = 0")
            first.db.execute("INSERT INTO items_fts (items_fts) VALUES ('delete-all')")
        finally:
            first.close()
        reopened = KnowledgeStore(path)
        try:
            assert self._titles(reopened.search_items_fts(self.LEAK, limit=50)) == [
                f"doc{i}" for i in range(5)]
        finally:
            reopened.close()


class TestCjkFts5Primitives:
    """The shared FTS5 dialect helpers (src/kiro_crew/_sqlite_compat.py)."""

    def test_non_cjk_expression_is_unchanged(self):
        from kiro_crew._sqlite_compat import fts5_cjk_match_groups, fts5_quote_tokens

        for query in ["JWT", "PROJ-123 hooks.py", 'say "hi" now', "", "   "]:
            assert fts5_cjk_match_groups(query) == fts5_quote_tokens(query), query

    def test_non_cjk_text_is_not_segmented(self):
        from kiro_crew._sqlite_compat import fts5_segment_for_index

        for text in ["JWT tokens with refresh flow", "PROJ-123", ""]:
            assert fts5_segment_for_index(text) == text

    def test_cjk_run_becomes_adjacent_pair_alternatives(self):
        from kiro_crew._sqlite_compat import fts5_cjk_match_groups

        # The 4-char "memory leak" run -> its three overlapping pairs, each a
        # phrase over the segmented characters.
        assert fts5_cjk_match_groups("\u5185\u5b58\u6cc4\u6f0f") == [
            '("\u5185 \u5b58" OR "\u5b58 \u6cc4" OR "\u6cc4 \u6f0f")']

    def test_single_cjk_character_has_no_pair(self):
        from kiro_crew._sqlite_compat import fts5_cjk_match_groups

        assert fts5_cjk_match_groups("\u5185") == ['"\u5185"']

    def test_mixed_script_token_ands_its_runs(self):
        from kiro_crew._sqlite_compat import fts5_cjk_match_groups

        assert fts5_cjk_match_groups("kirocrew\u90e8\u7f72") == [
            '("kirocrew" AND "\u90e8 \u7f72")']

    def test_quotes_in_input_cannot_escape_the_literal(self):
        from kiro_crew._sqlite_compat import fts5_cjk_match_groups

        assert fts5_cjk_match_groups('a" OR body:*') == ['"a"""', '"OR"', '"body:*"']

    def test_hangul_is_not_segmented(self):
        """Modern Korean is space-separated, so it needs no character gate."""
        from kiro_crew._sqlite_compat import fts5_cjk_match_groups, is_cjk_char

        assert not is_cjk_char("\ud68c")  # first syllable of "meeting"
        # "meeting" (2 syllables) stays one token, not a character pair.
        assert fts5_cjk_match_groups("\ud68c\uc758") == ['"\ud68c\uc758"']
