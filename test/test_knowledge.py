"""Unit tests for the Knowledge Library (store, chunker, readers, extractor, retrieval).

The additional store coverage with the folder sources and the watcher loop, the
additional retrieval and parsing coverage, the embedding rebuild job and the CJK
keyword recall live in ``test_knowledge_store_sources.py``,
``test_knowledge_retrieval_parsing.py``, ``test_knowledge_embedding_rebuild.py`` and
``test_knowledge_cjk_recall.py``; the ``store`` fixtures in ``knowledge_helpers.py``.
"""

from __future__ import annotations

import codecs
import json
import threading
import time
from datetime import datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from knowledge_helpers import store_factory_fixture  # noqa: F401
from knowledge_helpers import store_fixture  # noqa: F401
from pdf_test_helpers import flate_bomb_pdf

from kiro_crew import pdf_extract
from kiro_crew.knowledge import readers
from kiro_crew.knowledge.chunker import HeadingAwareChunker
from kiro_crew.knowledge.extractor import EntityExtractor
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.retrieval import (
    ANY_EMBEDDING_SPACE,
    HybridRetriever,
)
from kiro_crew.knowledge.store import KnowledgeBundleError, KnowledgeStore, SimpleDiGraph

#: Slack for a float budget compared against its nominal cap. Two readings of
#: the same monotonic tick do not cancel exactly once a cap has been added to
#: one of them, so `(now + cap) - now <= cap` is not a sound assertion.
_FLOAT_SLACK = 1e-6


# ---------------------------------------------------------------------------
# 1. KnowledgeStore
# ---------------------------------------------------------------------------


class TestKnowledgeStore:
    def test_create_and_get_item(self, store):
        item_id = store.add_item(
            "Auth Design",
            "JWT tokens with 1h expiry",
            "design_doc",
            summary="Auth overview",
            tags=["auth", "jwt"],
        )
        item = store.get_item(item_id)
        assert item is not None
        assert item["title"] == "Auth Design"
        assert item["content"] == "JWT tokens with 1h expiry"
        assert item["item_type"] == "design_doc"
        assert item["summary"] == "Auth overview"
        assert json.loads(item["tags"]) == ["auth", "jwt"]

    def test_close_all_releases_every_threads_connection_and_reopens_lazily(self, store):
        """``close()`` is per-thread by contract; ``_close_all_for_tests()`` is the teardown seam.

        A worker's connection has no other close path once the worker has
        returned, and an unclosed sqlite3 connection is a reference cycle on
        CPython 3.11+, so it holds its descriptors until the cyclic collector
        runs. After ``_close_all_for_tests()`` every thread -- the closer included --
        reconnects on its next take rather than touching a closed handle. The
        worker here stays ALIVE across ``_close_all_for_tests()`` and takes ``store.db``
        again on the same thread, so what is asserted is the generation check
        on a thread whose thread-local still caches the closed handle, not a
        fresh thread with empty thread-local state.
        """
        # The driver the store itself runs on: pysqlite3 on Linux x86_64 (CI), the
        # stdlib elsewhere. Their ProgrammingError classes are unrelated types.
        from kiro_crew._sqlite_compat import sqlite3

        store.add_item("Doc", "body", "note")
        took_first = threading.Event()
        closed_all = threading.Event()
        seen: dict[str, object] = {}

        def worker() -> None:
            first = store.db
            seen["first"] = first
            took_first.set()
            assert closed_all.wait(timeout=10), "the teardown close never happened"
            try:
                first.execute("SELECT 1")
            except sqlite3.ProgrammingError as exc:
                seen["closed_error"] = exc
            second = store.db
            seen["second"] = second
            seen["count_after"] = second.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]

        t = threading.Thread(target=worker)
        t.start()
        assert took_first.wait(timeout=10)
        assert seen["first"] is not store.db, "the worker got the loop thread's handle"
        store._close_all_for_tests()
        closed_all.set()
        t.join(timeout=10)
        assert not t.is_alive()
        assert "closed_error" in seen, "the closed handle still answered on the worker thread"
        assert (
            seen["second"] is not seen["first"]
        ), "the worker reused the handle the teardown close closed"
        assert seen["count_after"] == 1
        # The closer's own thread reconnects too.
        assert store.db.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 1

    def test_production_keeps_the_thread_guard_and_the_seam_refuses_without_the_flag(
        self, tmp_path, monkeypatch
    ):
        """The cross-thread relaxation is confined to tests by a module flag.

        With the flag off (production) a connection keeps SQLite's own
        thread-affinity guard, so a handle cached from ``store.db`` and used on
        another thread is refused rather than raced; and the every-thread close
        refuses to run at all, because production has no moment at which every
        thread is provably idle short of process exit. The refusal names the
        file that flips the flag -- ``test/conftest.py``, which only the
        ``test/`` testpath loads -- and says that the app test trees under
        ``src/kiro_crew/apps/builtins`` do not activate the flip themselves,
        rather than pointing at a conftest that holds none.
        """
        from kiro_crew._sqlite_compat import sqlite3
        from kiro_crew.knowledge import store as store_mod

        monkeypatch.setattr(store_mod, "_ALLOW_CROSS_THREAD_CLOSE_FOR_TESTS", False)
        prod = KnowledgeStore(str(tmp_path / "prod.db"))
        try:
            handle = prod.db
            outcome: list = []

            def misuse() -> None:
                try:
                    handle.execute("SELECT 1")
                    outcome.append("allowed")
                except sqlite3.ProgrammingError as exc:
                    outcome.append(exc)

            t = threading.Thread(target=misuse)
            t.start()
            t.join(timeout=10)
            assert outcome and isinstance(outcome[0], sqlite3.ProgrammingError), (
                "a production connection used from another thread was not refused: " f"{outcome!r}"
            )
            with pytest.raises(RuntimeError, match="test seam") as excinfo:
                prod._close_all_for_tests()
        finally:
            prod.close()
        message = str(excinfo.value)
        assert "test/conftest.py" in message, message
        assert "rootdir" not in message, message
        assert "src/kiro_crew/apps/builtins" in message, message

    def test_the_test_mode_connection_still_refuses_another_threads_use(self, store):
        """Relaxing SQLite's guard for the teardown close does not relax it for USE.

        Under the conftest's flag a connection is opened ``check_same_thread=False``
        so another thread may close it; every other operation from a thread that
        did not open it is refused exactly as production refuses it, so the suite
        still catches a caller that caches ``store.db`` and uses it from a worker.
        """
        from kiro_crew._sqlite_compat import sqlite3

        handle = store.db
        outcome: list = []

        def misuse() -> None:
            try:
                handle.execute("SELECT 1")
                outcome.append("allowed")
            except sqlite3.ProgrammingError as exc:
                outcome.append(exc)

        t = threading.Thread(target=misuse)
        t.start()
        t.join(timeout=10)
        assert outcome and isinstance(
            outcome[0], sqlite3.ProgrammingError
        ), f"a test-mode connection used from another thread was not refused: {outcome!r}"
        # The owner still works, and so does the loop-thread's own take.
        assert handle.execute("SELECT 1").fetchone()[0] == 1

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
        paused = s1.add_source(
            "vault", "local_folder", "/tmp/rt-paused", properties={"sync_status": "paused"}
        )
        unconfirmed = s1.add_source(
            "docs",
            "local_folder",
            "/tmp/rt-unconfirmed",
            properties={"sync_status": "pending_confirmation"},
        )
        # An outcome state a completed operation wrote: a bundle is untrusted
        # input, so restoring it as-is would assert work that never ran here.
        errored = s1.add_source("dead", "local_folder", "/tmp/rt-errored")
        s1.update_source(errored, sync_status="error")
        bundle = s1.export_all()
        assert {s["sync_status"] for s in bundle["sources"]} == {
            "paused",
            "pending_confirmation",
            "error",
        }

        s2 = store_factory("import-status.db")
        s2.import_bundle(bundle)
        restored = {
            r["id"]: r["sync_status"]
            for r in s2.db.execute("SELECT id, sync_status FROM sources").fetchall()
        }
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
        s2.import_bundle(
            {
                "sources": [
                    {
                        "id": "legacy-1",
                        "name": "vault",
                        "source_type": "local_folder",
                        "uri": "/tmp/rt-legacy",
                        "properties": json.dumps({"sync_status": "paused"}),
                    }
                ]
            }
        )
        row = s2.db.execute(
            "SELECT sync_status, properties FROM sources WHERE id = ?", ("legacy-1",)
        ).fetchone()
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
            s1.import_bundle(
                {
                    "sources": [
                        {
                            "id": "refused-1",
                            "name": "vault",
                            "source_type": "local_folder",
                            "uri": "/tmp/rt-refused",
                            "properties": json.dumps({"sync_status": "error"}),
                        }
                    ]
                }
            )
            row = s1.db.execute(
                "SELECT sync_status, properties FROM sources WHERE id = ?", ("refused-1",)
            ).fetchone()
            assert row["sync_status"] == "pending_confirmation"
            # The refused value is gone from the blob too, so nothing can lift it later.
            assert "sync_status" not in json.loads(row["properties"] or "{}")
        finally:
            s1.close()

        s2 = KnowledgeStore(db)
        try:
            row = s2.db.execute(
                "SELECT sync_status FROM sources WHERE id = ?", ("refused-1",)
            ).fetchone()
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
        assert (
            store.db.execute("SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()[
                "sync_status"
            ]
            == "error"
        )

        store.update_source(sid, sync_status="synced", if_sync_status="error")
        assert (
            store.db.execute("SELECT sync_status FROM sources WHERE id = ?", (sid,)).fetchone()[
                "sync_status"
            ]
            == "synced"
        )

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
            (
                sid,
                "s",
                "local_folder",
                "/tmp/repair-race",
                json.dumps({"sync_status": "pending_confirmation"}),
                "pending",
                now,
                now,
            ),
        )
        store.db.commit()
        store.close()

        real_loads = json.loads
        fired: list[bool] = []

        def confirm_lands_mid_scan(raw):
            parsed = real_loads(raw)
            if (
                not fired
                and isinstance(parsed, dict)
                and parsed.get("sync_status") == "pending_confirmation"
            ):
                fired.append(True)
                # The user confirms the source while the pass is mid-row: a
                # COLUMN-only transition, leaving properties untouched.
                conn = sqlite3.connect(db_path, timeout=30)
                try:
                    conn.execute("UPDATE sources SET sync_status = 'active' WHERE id = ?", (sid,))
                    conn.commit()
                finally:
                    conn.close()
            return parsed

        with patch("kiro_crew.knowledge.store.json.loads", confirm_lands_mid_scan):
            reopened = KnowledgeStore(db_path)
        try:
            assert fired, "the mid-scan write never landed; the test proves nothing"
            assert (
                reopened.db.execute(
                    "SELECT sync_status FROM sources WHERE id = ?", (sid,)
                ).fetchone()["sync_status"]
                == "active"
            )
        finally:
            reopened.close()

    def test_migration_survives_a_pathologically_nested_blob(self, store, tmp_path):
        """One unparsable legacy row must not stop the gateway from starting.

        ``json.loads`` recurses per nesting level and raises RecursionError --
        a RuntimeError, so not covered by the ValueError/TypeError guard. This
        migration runs on EVERY store open, so an uncaught one would abort every
        construction rather than skipping the row.
        """
        # Built by multiplication, not by wrapping in a loop: the accumulator
        # would sit on the RIGHT of the concat, so CPython cannot append in
        # place and every iteration recopies the whole string -- ~25 GB of
        # transient memcpy, 10 s of CPU on a loaded worker, to produce the same
        # 420 KB blob this builds in 0.3 ms. Same idiom as
        # test_cron_count_from_disk.py's `"[" * depth + "]" * depth`. The depth
        # is load-bearing, not padding: python3.12 decodes ~10,000 levels
        # before RecursionError, so a small number would assert nothing.
        deep = '{"a": ' * 60000 + '{"sync_status": "active"}' + "}" * 60000
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
                (sid, "s", "local_folder", uri, props_json, "pending", now, now),
            )
        store.db.commit()
        store.close()

        reopened = KnowledgeStore(str(tmp_path / "test.db"))
        try:
            rows = {
                r["id"]: r["sync_status"]
                for r in reopened.db.execute("SELECT id, sync_status FROM sources").fetchall()
            }
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
        paused. `import_bundle` can store a bundle's properties verbatim, so such
        a row can exist.
        """
        escaped = str(uuid4())
        now = datetime.now().isoformat()
        raw = '{"sync_\\u0073tatus": "paused"}'
        assert "sync_status" not in raw, "the point of the fixture is the escape"
        assert json.loads(raw) == {"sync_status": "paused"}
        store.db.execute(
            "INSERT INTO sources (id, name, source_type, uri, properties, sync_status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (escaped, "s", "local_folder", "/tmp/escaped", raw, "pending", now, now),
        )
        store.db.commit()
        store.close()

        reopened = KnowledgeStore(str(tmp_path / "test.db"))
        try:
            row = reopened.db.execute(
                "SELECT sync_status, properties FROM sources WHERE id = ?", (escaped,)
            ).fetchone()
            assert row["sync_status"] == "paused"
            # Retired too, and re-serialized, so the escape cannot come back.
            assert json.loads(row["properties"]) == {}
        finally:
            reopened.close()

    # ---- import_bundle JSON-column well-formedness ------------------------
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

    @pytest.mark.parametrize(
        "props",
        [
            "{not json",  # unparseable
            "",  # empty string: json.loads("") raises
            "[]",  # parses, wrong shape (readers index a dict)
            "null",  # parses to None, not a dict
            {"k": "v"},  # non-string: would bind str(dict) repr as TEXT
            7,  # non-string scalar
            pytest.param(
                "[" * 200000 + "]" * 200000,  # json.loads raises RecursionError
                # Short id: the default id embeds all 400k characters, and on
                # Windows pytest's PYTEST_CURRENT_TEST env var (which carries the
                # full test id) is capped at 32767 chars -> setup ValueError.
                id="deep-nesting",
            ),
            '{"x": "\ud800"}',  # lone surrogate: json.loads accepts, SQLite bind cannot UTF-8-encode
        ],
    )
    def test_import_bundle_rejects_malformed_properties(self, store, props):
        bundle = {"sources": [self._source(properties=props)]}
        with pytest.raises(KnowledgeBundleError):
            store.import_bundle(bundle)
        # The transaction rolled back: no partial row committed.
        assert store.db.execute("SELECT COUNT(*) AS c FROM sources").fetchone()["c"] == 0

    @pytest.mark.parametrize(
        "aliases",
        [
            "{not json",  # unparseable
            "",  # empty string
            "{}",  # parses, wrong shape (find_entity iterates a list)
            '["ok", 3]',  # list with a non-string element (.lower() crashes)
            ["a"],  # non-string: a Python list, not JSON text
            '["\ud800"]',  # lone surrogate: json.loads accepts, SQLite bind cannot UTF-8-encode
        ],
    )
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

        assert store.find_entity("Auth Service") is None
        rels = store.db.execute(
            "SELECT * FROM entity_relations WHERE source_id = ?", (e_keep,)
        ).fetchall()
        assert len(rels) == 1
        assert rels[0]["target_id"] == e_other
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
        combined = "\n".join(c["content"] for c in chunks)
        assert "def foo():" in combined
        assert "def bar():" in combined
        for c in chunks:
            assert "line_start" in c and "line_end" in c

    def test_chunk_code_splits_on_kotlin_declaration_keywords(self):
        # Kotlin/C# spell their declarations `fun`/`object`/`interface`/`internal`.
        # Without those keywords in the boundary regex the whole file is one block
        # that the oversized branch then slices on word count, so a chunk starts
        # mid-declaration and function-level retrieval granularity is lost.
        source = "\n".join(
            [
                "package com.example",
                "",
                "interface Greeter {",
                "    val name: String",
                "}",
                "",
                "object Registry {",
                "    val items = 0",
                "}",
                "",
                "internal val secret = 0",
                "",
                "fun greet(): Int {",
                "    return 1",
                "}",
            ]
        )
        # target_size=6 is picked so no block merges into its neighbour and none
        # trips the oversized-split branch: every declaration therefore heads its
        # own chunk, which is exactly what the boundary regex is asserted on.
        chunks = HeadingAwareChunker(target_size=6).chunk_code(source, language="kt")
        heads = [c["content"].split("\n", 1)[0] for c in chunks]
        for decl in (
            "interface Greeter {",
            "object Registry {",
            "internal val secret = 0",
            "fun greet(): Int {",
        ):
            assert decl in heads, f"{decl!r} did not start a chunk; chunk heads: {heads}"
        # Boundaries partition the lines: nothing is dropped or duplicated.
        assert "\n".join(c["content"] for c in chunks) == source

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
        for ext in (".md", ".txt", ".py", ".html", ".json", ".jsonl", ".ndjson", ".yaml", ".csv"):
            assert ext in reader.SUPPORTED, f"{ext} missing from SUPPORTED"

    def test_asciidoc_extensions_ingested_as_plain_text(self, tmp_path):
        reader = FileReader()
        assert ".asc" not in reader.SUPPORTED
        for ext in (".adoc", ".asciidoc"):
            assert ext in reader.SUPPORTED
            assert reader._DISPATCH[ext] == "_read_asciidoc"
            f = tmp_path / f"guide{ext}"
            f.write_text("= Guide\n\nhello asciidoc", encoding="utf-8")
            text, meta = reader.read(str(f))
            assert "hello asciidoc" in text
            assert meta["format"] == ext.lstrip(".")

    def test_powershell_extensions_ingested_as_plain_text(self, tmp_path):
        # PowerShell scripts (.ps1), modules (.psm1), and module manifests
        # (.psd1) are plain UTF-8 text: they must be in SUPPORTED (so folder
        # sources ingest rather than silently skip them) and must flow through
        # the generic _read_text path, not a _DISPATCH reader.
        reader = FileReader()
        samples = {
            ".ps1": 'Write-Host "hello from a script"',
            ".psm1": 'function Get-Thing { "hello from a module" }',
            ".psd1": "@{ ModuleVersion = '1.0'; Description = 'hello manifest' }",
        }
        for ext, content in samples.items():
            assert ext in reader.SUPPORTED, f"{ext} missing from SUPPORTED"
            assert ext not in reader._DISPATCH, f"{ext} must use the generic text path"
            f = tmp_path / f"sample{ext}"
            f.write_text(content, encoding="utf-8")
            text, meta = reader.read(str(f))
            assert content in text
            assert meta["format"] == ext.lstrip(".")
            assert meta["extension"] == ext
        # Scripts and modules chunk at function boundaries like their .sh/.rb
        # peers; the .psd1 manifest is data, so it stays on the generic path.
        from kiro_crew.knowledge.ingestion import CODE_EXTS

        assert ".ps1" in CODE_EXTS
        assert ".psm1" in CODE_EXTS
        assert ".psd1" not in CODE_EXTS

    def test_php_extensions_ingested_as_plain_text(self, tmp_path):
        # PHP sources (.php) and PHP+HTML templates (.phtml) are plain UTF-8
        # text, so they take the same path as .ps1/.psm1: present in SUPPORTED
        # (the folder-scan gate; include_extensions can only narrow it, so a
        # folder source over a codebase whose extension is absent there indexes
        # only its README and config, with no warning) and read through the
        # generic _read_text path.
        reader = FileReader()
        samples = {
            ".php": '<?php\nfunction greet() { echo "hello from php"; }\n',
            ".phtml": "<ul><?php foreach ($items as $i): ?><li><?= $i ?></li><?php endforeach; ?></ul>\n",
        }
        for ext, content in samples.items():
            assert ext in reader.SUPPORTED, f"{ext} missing from SUPPORTED"
            assert ext not in reader._DISPATCH, f"{ext} must use the generic text path"
            f = tmp_path / f"sample{ext}"
            f.write_text(content, encoding="utf-8")
            text, meta = reader.read(str(f))
            assert content in text
            assert meta["format"] == ext.lstrip(".")
            assert meta["extension"] == ext
        # .php sources chunk at function/class/method lines like their .sh/.rb
        # peers (the code chunker's language-generic boundary regex already
        # matches `function `, `class `, `public `, `private `). A .phtml
        # template is markup with embedded PHP and carries no such lines, so
        # it stays on the generic chunker, as the .psd1 manifest does.
        from kiro_crew.knowledge.ingestion import CODE_EXTS

        assert ".php" in CODE_EXTS
        assert ".phtml" not in CODE_EXTS

    def test_kotlin_and_peer_code_extensions_ingested_as_plain_text(self, tmp_path):
        # Kotlin (.kt/.kts) and the C#/Swift/Scala trio are plain UTF-8 text, so
        # the generic _read_text path handles them with no reader and no new
        # dependency. They must be in SUPPORTED because that set is the
        # folder-scan gate (folder_watcher._walk), and a source's
        # include_extensions can only narrow it: an extension absent there is
        # skipped before any reader runs, so a folder source over such a repo
        # indexes only its README and config.
        reader = FileReader()
        samples = {
            ".kt": 'fun main() { println("hello from kotlin") }',
            ".kts": 'val greeting = "hello from a kotlin script"',
            ".cs": "internal class Greeter { public void Hi() {} }",
            ".swift": 'func greet() { print("hello from swift") }',
            ".scala": "object Greeter { def hi(): Unit = () }",
        }
        for ext, content in samples.items():
            assert ext in reader.SUPPORTED, f"{ext} missing from SUPPORTED"
            assert ext not in reader._DISPATCH, f"{ext} must use the generic text path"
            f = tmp_path / f"sample{ext}"
            f.write_text(content, encoding="utf-8")
            text, meta = reader.read(str(f))
            assert content in text
            assert meta["format"] == ext.lstrip(".")
            assert meta["extension"] == ext

    def test_code_extensions_are_a_subset_of_supported(self):
        # CODE_EXTS (ingestion.py) and SUPPORTED (readers.py) are two
        # hand-maintained registries over the same extensions, and only SUPPORTED
        # gates the folder scan. An extension listed in CODE_EXTS alone never
        # reaches the chunker it selects -- the file is dropped upstream with no
        # error -- so the subset relation is the guard against that silent drift.
        from kiro_crew.knowledge.ingestion import CODE_EXTS

        assert {".kt", ".kts"} <= CODE_EXTS
        missing = sorted(CODE_EXTS - FileReader.SUPPORTED)
        assert not missing, f"CODE_EXTS entries absent from FileReader.SUPPORTED: {missing}"

    def test_kotlin_script_routes_to_the_code_chunker(self):
        # .kts is a Kotlin build/script file, not prose: it must reach chunk_code
        # like .kt rather than the generic prose chunker.
        from kiro_crew.knowledge.ingestion import _run_chunker

        chunker = MagicMock()
        chunks = _run_chunker(chunker, ".kts", "val x = 1", "file:///build.gradle.kts")
        assert chunks is chunker.chunk_code.return_value
        chunker.chunk_code.assert_called_once_with("val x = 1", language="kts")
        chunker.chunk.assert_not_called()

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
            assert "\x00" not in text, f"{name}: NUL bytes leaked into indexed text"
            assert meta["format"] == "psd1"
        # A BOM that lies (truncated/invalid UTF-16 payload) degrades to
        # latin-1 like the utf-8 branch does -- ingest never hard-fails on it.
        liar = tmp_path / "truncated.psd1"
        liar.write_bytes(codecs.BOM_UTF16_LE + b"A")
        text, meta = reader.read(str(liar))
        assert meta["format"] == "psd1", "invalid UTF-16 must degrade, not error"
        # The HTML reader shares the same decode: BOM'd UTF-16 HTML from
        # Windows tooling must not fall into the latin-1 mojibake path either.
        page = tmp_path / "saved.html"
        page.write_bytes(
            codecs.BOM_UTF16_LE + "<html><body>utf16 page body</body></html>".encode("utf-16-le")
        )
        text, meta = reader.read(str(page))
        assert "utf16 page body" in text
        assert "\x00" not in text


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
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref_pos)
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
        assert ".pdf" in reader.SUPPORTED
        assert reader._DISPATCH.get(".pdf") == "_read_pdf"

    def test_pdfplumber_runtime_dep_present(self):
        # The extractor child imports pdfplumber; the parent only checks it is
        # installed. If this fails, 'pdfplumber' is missing from setup.cfg
        # install_requires.
        assert pdf_extract.pdfplumber_available(), (
            "pdfplumber import failed -- declare 'pdfplumber' in setup.cfg " "install_requires"
        )

    def test_read_pdf_extracts_text(self, tmp_path):
        p = tmp_path / "doc.pdf"
        p.write_bytes(_make_pdf("Hello PDF regression"))
        reader = FileReader()
        text, meta = reader.read(str(p))
        assert "Hello PDF regression" in text
        assert meta["format"] == "pdf"
        assert meta["page_count"] == 1
        assert "truncated" not in meta

    def test_read_pdf_runs_in_the_bounded_child(self, tmp_path, monkeypatch):
        """The reader hands the open file to ``extract_pdf_segments`` with the
        ingest caps and never parses in this process."""
        calls = []

        def fake_extract(source, *, max_chars, deadline, max_pages):
            calls.append((source.read(5), max_chars, max_pages, deadline - time.monotonic()))
            return pdf_extract.PdfExtraction(
                (("page 1", "first"), ("page 3", "third")), True, None, 3
            )

        monkeypatch.setattr(readers, "extract_pdf_segments", fake_extract)
        p = tmp_path / "doc.pdf"
        p.write_bytes(_make_pdf("ignored by the fake"))
        text, meta = FileReader()._read_pdf(str(p))
        assert text == "first\nthird"
        assert meta == {"format": "pdf", "page_count": 3, "truncated": True}
        [(head, max_chars, max_pages, remaining)] = calls
        assert head == b"%PDF-"
        assert max_chars == readers._PDF_MAX_CHARS
        assert max_pages == pdf_extract.PDF_MAX_PAGES
        # `remaining` is `(now + cap) - now'` in floats: with `now` a large
        # monotonic reading and `now'` the same tick on a coarse clock, the
        # result lands a few ulps either side of the cap, so an exact `<=`
        # trips by ~1e-13 on Windows. The bound is the cap, to float slack.
        assert 0 < remaining <= readers._PDF_WALL_SECS + _FLOAT_SLACK

    def test_read_pdf_flate_bomb_is_a_read_error(self, tmp_path):
        """A page that inflates past the child's ceiling is the ordinary error
        sentinel: ``ingest_file`` records it and the scan continues. The reason
        names the ceiling (``memory``), so this cannot pass on a timeout."""
        p = tmp_path / "bomb.pdf"
        p.write_bytes(flate_bomb_pdf())
        text, meta = FileReader().read(str(p))
        assert meta["format"] == "error"
        assert meta["error"] == "PDF extraction failed: memory"
        assert text.startswith("Error reading file:")

    def test_read_pdf_child_failure_kinds_are_all_errors(self, tmp_path, monkeypatch):
        for kind in ("cpu", "timeout", "killed", "parse", "protocol", "spawn"):
            monkeypatch.setattr(
                readers,
                "extract_pdf_segments",
                lambda *_a, _kind=kind, **_k: pdf_extract.PdfExtraction((), True, _kind, 0),
            )
            p = tmp_path / f"{kind}.pdf"
            p.write_bytes(_make_pdf("x"))
            _text, meta = FileReader()._read_pdf(str(p))
            assert meta == {"format": "error", "error": f"PDF extraction failed: {kind}"}

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
        assert result == {
            "title": "",
            "entities": [],
            "relations": [],
            "category": "document",
            "summary": "",
        }

    def test_parse_json_response(self):
        ext = EntityExtractor()
        raw = json.dumps(
            {
                "entities": [{"name": "Svc", "type": "service", "description": "A service"}],
                "relations": [],
                "category": "design_doc",
                "summary": "A service doc.",
            }
        )
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
        item_id = store.add_item("OP Vision", "vision content goes here", "document", source_id=sid)
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
        # ANY_EMBEDDING_SPACE: this asserts the SOURCE scope, and the items carry
        # no signature, so the space predicate is deliberately out of the way.
        retriever = HybridRetriever(
            store, embedder=lambda q: [1.0, 0.0, 0.0, 0.0], embed_sig=ANY_EMBEDDING_SPACE
        )
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


class TestHybridRetrieverNamespaceFilter:
    def test_namespace_narrows_keyword_seeds(self, store):
        # Both items match the query; scoping to one namespace keeps only its
        # item. namespace is an organisational label on items, not a source.
        store.add_item("Auth A", "JWT tokens for service alpha", "doc", namespace="client-a")
        store.add_item("Auth B", "JWT tokens for service beta", "doc", namespace="client-b")
        retriever = HybridRetriever(store)
        results = retriever.search("JWT", namespace="client-a")
        assert [r["title"] for r in results] == ["Auth A"]

    def test_omitted_namespace_keeps_current_behavior(self, store):
        # Regression: no namespace == the pre-filter result set.
        store.add_item("Auth A", "JWT tokens for service alpha", "doc", namespace="client-a")
        store.add_item("Auth B", "JWT tokens for service beta", "doc", namespace="client-b")
        retriever = HybridRetriever(store)
        results = retriever.search("JWT")
        assert {r["title"] for r in results} == {"Auth A", "Auth B"}

    def test_namespace_narrows_vector_seeds(self, store):
        # Identical embeddings in two namespaces; scoping keeps one. The query
        # shares no tokens with the content, isolating the vector leg.
        vec = json.dumps([1.0, 0.0, 0.0, 0.0]).encode()
        store.add_item("Vec A", "alpha content", "doc", namespace="client-a", embedding=vec)
        store.add_item("Vec B", "beta content", "doc", namespace="client-b", embedding=vec)
        retriever = HybridRetriever(
            store, embedder=lambda q: [1.0, 0.0, 0.0, 0.0], embed_sig=ANY_EMBEDDING_SPACE
        )
        results = retriever.search("unrelatedquerytoken", namespace="client-a")
        assert [r["title"] for r in results] == ["Vec A"]

    def test_unknown_namespace_returns_no_results(self, store):
        # A nonexistent namespace empties the seed legs without raising; unlike
        # source_id there is no existence probe, so it just yields nothing.
        store.add_item("Auth", "JWT tokens", "doc", namespace="client-a")
        retriever = HybridRetriever(store)
        assert retriever.search("JWT", namespace="no-such-namespace") == []

    def test_namespace_and_source_id_compose(self, store):
        # Both filters apply together: only the item matching BOTH the source
        # and the namespace survives the seed legs.
        src_a = store.add_source("Docs A", "local_folder", "/tmp/a")
        src_b = store.add_source("Docs B", "local_folder", "/tmp/b")
        store.add_item("Match", "JWT tokens here", "doc", source_id=src_a, namespace="client-a")
        # Same source, wrong namespace.
        store.add_item("Wrong NS", "JWT tokens here", "doc", source_id=src_a, namespace="client-b")
        # Right namespace, wrong source.
        store.add_item("Wrong Src", "JWT tokens here", "doc", source_id=src_b, namespace="client-a")
        retriever = HybridRetriever(store)
        results = retriever.search("JWT", source_id=src_a, namespace="client-a")
        assert [r["title"] for r in results] == ["Match"]


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
