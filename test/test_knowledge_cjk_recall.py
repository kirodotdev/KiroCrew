"""CJK recall on the knowledge store's FTS keyword leg.

The keyword leg's CJK recall and the shared FTS5 dialect helpers.
The ``store`` fixture comes from ``knowledge_helpers.py``.
"""

from __future__ import annotations

import importlib
import re
import threading

import pytest
from knowledge_helpers import store_fixture  # noqa: F401

from kiro_crew.knowledge.retrieval import HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore


class TestCjkKeywordRecall:
    """CJK recall on the FTS keyword leg.

    Vocabulary is shared with ``TestCjkSearch`` in test_history_session_search.py so the two
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
    DOC_APART = (
        "\u4eca\u5929\u8c03\u67e5\u4e86\u5185\u5b58\u91cc\u7684\u6570\u636e\u6cc4\u6f0f\u95ee\u9898"
    )
    # "finished locating the memory leak" -- holds the whole run verbatim.
    DOC_RUN = "\u5185\u5b58\u6cc4\u6f0f\u5b9a\u4f4d\u5b8c\u6210\u4e86"
    # "a record of the internal relief valve and the water leak" -- reuses all
    # four characters of LEAK, but spells neither "memory" nor "leak".
    DOC_DECOY = (
        "\u5185\u90e8\u4fdd\u5b58\u4e86\u6cc4\u538b\u9600\u548c\u6f0f\u6c34\u7684\u8bb0\u5f55"
    )
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
        rowid = store.db.execute("SELECT rowid FROM items WHERE title = ?", (title,)).fetchone()[0]
        store.db.execute("INSERT INTO items_fts (items_fts) VALUES ('delete-all')")
        store.db.execute(
            "INSERT INTO items_fts (rowid, title, content, tags) VALUES (?, ?, ?, ?)",
            (rowid, title, content, tags),
        )
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
            rows = first.db.execute("SELECT rowid, title, content, tags FROM items").fetchall()
            first.db.execute("INSERT INTO items_fts (items_fts) VALUES ('delete-all')")
            first.db.execute("BEGIN IMMEDIATE")
            for r in rows:
                first.db.execute(
                    "INSERT INTO items_fts (rowid,title,content,tags) VALUES (?,?,?,?)",
                    (r["rowid"], r["title"], r["content"], r["tags"]),
                )
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
            store._close_all_for_tests()

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
            name
            for name, fn in readers.items()
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

        family = (store_mod, *map(importlib.import_module, store_mod._STORE_OWNERS))
        source = "\n".join(inspect.getsource(module) for module in family)
        module_writes = len(re.findall(r"INSERT INTO items_fts", source))
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

        family = (store_mod, *map(importlib.import_module, store_mod._STORE_OWNERS))
        source = "\n".join(inspect.getsource(module) for module in family)
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
            # An update against a legacy index. Both halves of the FTS sync run here.
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
            store.delete_item(item_id)  # must not raise DatabaseError
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

            assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == FTS_INDEX_VERSION
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
                threading.Thread(target=lambda: results.append(store.search_items_fts(self.LEAK)))
                for _ in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                # flake-ok: joins four threads that each run one bounded search; moved verbatim, tracked for a de-flake pass
                t.join()
            assert len(calls) == 1, f"rebuilt {len(calls)} times, expected once"
            assert all(self._titles(r) == ["run"] for r in results)
        finally:
            store._close_all_for_tests()

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
            assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == FTS_INDEX_VERSION
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
                f"doc{i}" for i in range(5)
            ]
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
            '("\u5185 \u5b58" OR "\u5b58 \u6cc4" OR "\u6cc4 \u6f0f")'
        ]

    def test_single_cjk_character_has_no_pair(self):
        from kiro_crew._sqlite_compat import fts5_cjk_match_groups

        assert fts5_cjk_match_groups("\u5185") == ['"\u5185"']

    def test_mixed_script_token_ands_its_runs(self):
        from kiro_crew._sqlite_compat import fts5_cjk_match_groups

        assert fts5_cjk_match_groups("kirocrew\u90e8\u7f72") == ['("kirocrew" AND "\u90e8 \u7f72")']

    def test_quotes_in_input_cannot_escape_the_literal(self):
        from kiro_crew._sqlite_compat import fts5_cjk_match_groups

        assert fts5_cjk_match_groups('a" OR body:*') == ['"a"""', '"OR"', '"body:*"']

    def test_hangul_is_not_segmented(self):
        """Modern Korean is space-separated, so it needs no character gate."""
        from kiro_crew._sqlite_compat import fts5_cjk_match_groups, is_cjk_char

        assert not is_cjk_char("\ud68c")  # first syllable of "meeting"
        # "meeting" (2 syllables) stays one token, not a character pair.
        assert fts5_cjk_match_groups("\ud68c\uc758") == ['"\ud68c\uc758"']
