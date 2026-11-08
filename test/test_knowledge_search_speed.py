"""Knowledge search speed fixes keep their answers.

Three changes cut a knowledge search on a 22k-item, 59k-entity library from
~0.9 s to ~0.2 s: the alias fallback in ``find_entity`` skips rows that have no
aliases, two indexes serve the graph leg, and the vector leg reuses one cached
matrix until a write changes the rows that feed it. These tests pin that each
change returns what the code returned before it.
"""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.knowledge import retrieval
from kiro_crew.knowledge.embedder import floats_to_bytes
from kiro_crew.knowledge.retrieval import ANY_EMBEDDING_SPACE, HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore

pytest.importorskip("numpy")

_DIM = 8


@pytest.fixture()
def store(tmp_path: Path):
    retrieval.clear_vector_matrix_cache()
    s = KnowledgeStore(str(tmp_path / "kb.db"))
    yield s
    s.close()
    retrieval.clear_vector_matrix_cache()


def _vec(rng: random.Random) -> list[float]:
    return [rng.gauss(0.0, 1.0) for _ in range(_DIM)]


def _retriever(store: KnowledgeStore, query_vec: list[float]) -> HybridRetriever:
    return HybridRetriever(store, embedder=lambda _q: query_vec, embed_sig=ANY_EMBEDDING_SPACE)


def _reference(store: KnowledgeStore, query_vec: list[float], monkeypatch) -> list:
    """The vector leg's answer with the cache out of the picture."""
    with monkeypatch.context() as m:
        m.setattr(retrieval, "_cached_matrix", lambda *a, **k: None)
        return _retriever(store, query_vec)._vector_search("q", limit=20)


def _count_builds(monkeypatch) -> list[int]:
    builds: list[int] = []
    real_build = retrieval._build_matrix

    def counting_build(*a, **k):
        builds.append(1)
        return real_build(*a, **k)

    monkeypatch.setattr(retrieval, "_build_matrix", counting_build)
    return builds


class TestFindEntity:
    def test_alias_still_resolves_beside_alias_less_rows(self, store):
        for i in range(5):
            store.add_entity(f"plain {i}", "concept")
        eid = store.add_entity("Kubernetes", "tool", aliases=["k8s"])
        assert store.find_entity("K8S")["id"] == eid

    def test_name_match_is_case_insensitive(self, store):
        eid = store.add_entity("FastAPI", "tool")
        assert store.find_entity("fastapi")["id"] == eid

    def test_miss_returns_none(self, store):
        store.add_entity("FastAPI", "tool")
        assert store.find_entity("django") is None

    def test_graph_indexes_exist(self, store):
        names = {
            r[0] for r in store.db.execute("SELECT name FROM sqlite_schema WHERE type = 'index'")
        }
        assert {"idx_entities_name_lower", "idx_mentions_entity_item"} <= names


def _index_names(store) -> set[str]:
    return {r[0] for r in store.db.execute("SELECT name FROM sqlite_schema WHERE type = 'index'")}


class TestSearchIndexesOffConstruction:
    """A populated library must not build the O(data) indexes while opening.

    The gateway opens the store before its listener binds; on a cold 59k-entity
    library these two indexes took ~11 s, which landed on time-to-ready.
    """

    @staticmethod
    def _populated_without_indexes(tmp_path: Path) -> str:
        path = str(tmp_path / "kb.db")
        s = KnowledgeStore(path)
        eid = s.add_entity("widget", "concept")
        s.add_mention(s.add_item("doc", "c", "doc"), eid)
        for name in ("idx_entities_name_lower", "idx_mentions_entity_item"):
            s.db.execute(f"DROP INDEX {name}")
        s.db.commit()
        return path

    def test_reopen_of_populated_store_defers_them(self, tmp_path):
        reopened = KnowledgeStore(self._populated_without_indexes(tmp_path))
        assert not {"idx_entities_name_lower", "idx_mentions_entity_item"} & _index_names(reopened)

    def test_entities_without_mentions_still_defer_them(self, tmp_path):
        path = self._populated_without_indexes(tmp_path)
        s = KnowledgeStore(path)
        s.db.execute("DELETE FROM mentions")
        s.db.commit()
        s.close()
        reopened = KnowledgeStore(path)
        assert not {"idx_entities_name_lower", "idx_mentions_entity_item"} & _index_names(reopened)

    def test_items_without_entities_defer_the_embed_state_index(self, tmp_path):
        # idx_items_embed_state covers the whole items table (vector BLOBs and
        # all); a library holding items but no entities yet must not build it
        # before bind.
        path = str(tmp_path / "kb.db")
        s = KnowledgeStore(path)
        s.add_item("doc", "c", "doc")
        s.db.execute("DROP INDEX idx_items_embed_state")
        s.db.commit()
        s.close()
        reopened = KnowledgeStore(path)
        assert "idx_items_embed_state" not in _index_names(reopened)
        reopened.ensure_search_indexes()
        assert "idx_items_embed_state" in _index_names(reopened)

    def test_ensure_search_indexes_builds_them_and_is_idempotent(self, tmp_path):
        reopened = KnowledgeStore(self._populated_without_indexes(tmp_path))
        reopened.ensure_search_indexes()
        reopened.ensure_search_indexes()
        assert {"idx_entities_name_lower", "idx_mentions_entity_item"} <= _index_names(reopened)

    def test_search_results_do_not_depend_on_them(self, tmp_path):
        reopened = KnowledgeStore(self._populated_without_indexes(tmp_path))
        before = (reopened.find_entity("WIDGET"), HybridRetriever(reopened)._graph_search("widget"))
        reopened.ensure_search_indexes()
        after = (reopened.find_entity("WIDGET"), HybridRetriever(reopened)._graph_search("widget"))
        assert before == after


class TestGraphTieOrder:
    def test_equal_counts_rank_by_item_id(self, store):
        eid = store.add_entity("widget", "concept")
        items = [store.add_item(f"doc {i}", "c", "doc") for i in range(4)]
        for item in items:
            store.add_mention(item, eid)
        ranked = HybridRetriever(store)._graph_search("widget")
        assert [i for i, _ in ranked] == sorted(items)


class TestVectorMatrixCache:
    def test_cached_ranking_matches_reference_scorer(self, store):
        rng = random.Random(7)
        for i in range(40):
            store.add_item(f"doc {i}", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        q = _vec(rng)
        r = _retriever(store, q)
        first = r._vector_search("q", limit=40)
        assert len(retrieval._matrix_cache) == 1
        second = r._vector_search("q", limit=40)
        rows = store.db.execute(
            "SELECT id, embedding FROM items WHERE embedding IS NOT NULL AND status = 'active'"
        ).fetchall()
        scored, _ = HybridRetriever._score_rows_python(rows, q)
        scored.sort(key=lambda x: x[1], reverse=True)
        reference = [(i, n + 1) for n, (i, _) in enumerate(scored[:40])]
        assert first == second == reference

    def test_a_packed_blob_that_starts_like_json_stays_on_the_matrix(self, store, monkeypatch):
        rng = random.Random(17)
        lead = 0.0
        while not floats_to_bytes([lead]).startswith(b"["):
            lead = rng.gauss(0.0, 1.0)
        for i in range(10):
            store.add_item(
                f"doc {i}", "c", "doc", embedding=floats_to_bytes([lead, *_vec(rng)[1:]])
            )
            store.add_item(f"plain {i}", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        q = _vec(rng)
        cached = _retriever(store, q)._vector_search("q", limit=20)
        (entry,) = retrieval._matrix_cache.values()
        assert entry[1] is not None and len(entry[1]) == 20
        assert cached == _reference(store, q, monkeypatch)

    def test_mixed_encodings_rank_like_the_reference(self, store, monkeypatch):
        # float32-exact JSON vectors are admitted beside packed ones and land at
        # their own row position in the joined matrix.
        rng = random.Random(16)
        for i in range(30):
            v = [float(int(x * 8)) / 8 for x in _vec(rng)]
            blob = json.dumps(v).encode() if i % 3 == 0 else floats_to_bytes(v)
            store.add_item(f"doc {i}", "c", "doc", embedding=blob)
        q = _vec(rng)
        builds = _count_builds(monkeypatch)
        cached = _retriever(store, q)._vector_search("q", limit=20)
        assert len(builds) == 1 and retrieval._matrix_cache
        assert next(iter(retrieval._matrix_cache.values()))[1] is not None
        assert cached == _reference(store, q, monkeypatch)

    def test_write_invalidates_the_cache(self, store):
        rng = random.Random(3)
        for i in range(10):
            store.add_item(f"doc {i}", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        q = _vec(rng)
        r = _retriever(store, q)
        r._vector_search("q")
        new_id = store.add_item("exact", "c", "doc", embedding=floats_to_bytes(q))
        assert r._vector_search("q")[0][0] == new_id

    def test_a_write_to_another_table_keeps_the_cache(self, store, monkeypatch):
        rng = random.Random(4)
        for i in range(5):
            store.add_item(f"doc {i}", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        builds = _count_builds(monkeypatch)
        q = _vec(rng)
        r = _retriever(store, q)
        first = r._vector_search("q")
        store.db.execute(
            "INSERT INTO ingestion_jobs (id, status, created_at, updated_at) "
            "VALUES ('job-1', 'pending', 'now', 'now')"
        )
        store.db.execute("PRAGMA wal_checkpoint(RESTART)").fetchall()
        assert r._vector_search("q") == first
        assert len(builds) == 1

    def test_an_edit_outside_the_vector_columns_keeps_the_cache(self, store, monkeypatch):
        rng = random.Random(11)
        item_id = store.add_item("doc", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        builds = _count_builds(monkeypatch)
        r = _retriever(store, _vec(rng))
        first = r._vector_search("q")
        store.update_item(item_id, title="renamed", content="new body", summary="s")
        assert r._vector_search("q") == first
        assert len(builds) == 1

    def test_a_status_change_invalidates_the_cache(self, store, monkeypatch):
        rng = random.Random(12)
        q = _vec(rng)
        top = store.add_item("top", "c", "doc", embedding=floats_to_bytes(q))
        store.add_item("other", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        r = _retriever(store, q)
        assert r._vector_search("q")[0][0] == top
        store.update_item(top, status="archived")
        assert r._vector_search("q") == _reference(store, q, monkeypatch)
        assert top not in [i for i, _ in r._vector_search("q")]

    def test_a_write_from_another_connection_invalidates_the_cache(self, store):
        rng = random.Random(13)
        for i in range(5):
            store.add_item(f"doc {i}", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        q = _vec(rng)
        r = _retriever(store, q)
        r._vector_search("q")
        other = sqlite3.connect(store._db_path)
        try:
            other.execute(
                "INSERT INTO items (id, title, content, item_type, embedding, created_at, updated_at) "
                "VALUES ('outside', 't', 'c', 'doc', ?, 'now', 'now')",
                (floats_to_bytes(q),),
            )
            other.commit()
        finally:
            other.close()
        assert r._vector_search("q")[0][0] == "outside"

    def test_a_source_location_write_invalidates_a_source_scoped_cache(self, store, monkeypatch):
        rng = random.Random(14)
        a = store.add_source("a", "file", "file:///a")
        b = store.add_source("b", "file", "file:///b")
        q = _vec(rng)
        store.add_item("in a", "c", "doc", source_id=a, embedding=floats_to_bytes(_vec(rng)))
        shared = store.add_item("in b", "c", "doc", source_id=b, embedding=floats_to_bytes(q))
        r = _retriever(store, q)
        assert shared not in [i for i, _ in r._vector_search("q", source_id=a)]
        store.add_source_location(shared, a)
        assert r._vector_search("q", source_id=a)[0][0] == shared

    def test_a_store_without_a_generation_is_not_cached(self, store):
        rng = random.Random(15)
        q = _vec(rng)
        store.add_item("a", "c", "doc", embedding=floats_to_bytes(q))
        store.db.execute("DELETE FROM vector_generation")
        assert _retriever(store, q)._vector_search("q")
        assert retrieval._matrix_cache == {}

    def test_ids_count_against_the_cap(self, store, monkeypatch):
        rng = random.Random(8)
        for i in range(10):
            store.add_item(f"doc {i}", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        matrix_and_norms = 10 * (_DIM * 4 + 8)
        # Room for the matrix and norms, not for the ids beside them.
        monkeypatch.setattr(retrieval, "_MATRIX_CACHE_MAX_BYTES", matrix_and_norms + 100)
        q = _vec(rng)
        expected = _reference(store, q, monkeypatch)
        rows = _retriever(store, q)._vector_search("q", limit=10)
        assert rows == expected  # answered by the reference path
        (entry,) = retrieval._matrix_cache.values()
        assert entry[1] is None  # remembered as too big, nothing held

    def test_an_over_cap_result_is_not_rebuilt_until_the_db_changes(self, store, monkeypatch):
        rng = random.Random(9)
        for i in range(5):
            store.add_item(f"doc {i}", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        monkeypatch.setattr(retrieval, "_MATRIX_CACHE_MAX_BYTES", 1)
        builds: list[int] = []
        real_build = retrieval._build_matrix

        def counting_build(*a, **k):
            builds.append(1)
            return real_build(*a, **k)

        monkeypatch.setattr(retrieval, "_build_matrix", counting_build)
        q = _vec(rng)
        r = _retriever(store, q)
        expected = _reference(store, q, monkeypatch)
        for _ in range(3):
            assert r._vector_search("q") == expected
        assert len(builds) == 1
        store.add_item("new", "c", "doc", embedding=floats_to_bytes(q))
        assert r._vector_search("q") == _reference(store, q, monkeypatch)
        assert len(builds) == 2

    def test_entries_share_one_byte_budget(self, store, monkeypatch):
        rng = random.Random(10)
        for i in range(10):
            store.add_item(f"doc {i}", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        sql = "SELECT id, embedding FROM items WHERE embedding IS NOT NULL"
        assert retrieval._cached_matrix(store, sql, [], _DIM) is not None
        (first,) = retrieval._matrix_cache.values()
        # Room for one entry of this size, not two.
        monkeypatch.setattr(retrieval, "_MATRIX_CACHE_MAX_BYTES", first[4] * 3 // 2)
        assert retrieval._cached_matrix(store, sql + " AND 1 = 1", [], _DIM) is not None
        assert len(retrieval._matrix_cache) == 1
        assert sum(e[4] for e in retrieval._matrix_cache.values()) <= first[4] * 3 // 2

    def test_mismatched_width_uses_reference_path(self, store, monkeypatch):
        rng = random.Random(5)
        q = _vec(rng)
        store.add_item("ok", "c", "doc", embedding=floats_to_bytes(q))
        store.add_item("short", "c", "doc", embedding=floats_to_bytes([1.0, 2.0, 3.0, 4.0]))
        builds: list[int] = []
        real_build = retrieval._build_matrix

        def counting_build(*a, **k):
            builds.append(1)
            return real_build(*a, **k)

        monkeypatch.setattr(retrieval, "_build_matrix", counting_build)
        r = _retriever(store, q)
        for _ in range(3):
            assert len(r._vector_search("q")) == 1
        # Read once by the build, then remembered as uncacheable: no matrix held.
        assert len(builds) == 1
        assert [e[1] for e in retrieval._matrix_cache.values()] == [None]

    def test_in_memory_store_is_not_cached(self):
        retrieval.clear_vector_matrix_cache()
        s = KnowledgeStore(":memory:")
        try:
            rng = random.Random(1)
            s.add_item("a", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
            assert _retriever(s, _vec(rng))._vector_search("q")
            assert retrieval._matrix_cache == {}
        finally:
            s.close()


class TestAliasFallbackIndex:
    """The alias fallback reads through ``idx_entities_with_aliases``, in table order."""

    @staticmethod
    def _two_sharing_an_alias(store) -> tuple[str, str]:
        # The index orders by the aliases text, so the later row sorts first
        # there: only a read that restores table order returns ``first``.
        first = store.add_entity("Kubernetes", "tool", aliases=["zeta", "k8s"])
        second = store.add_entity("K8s Operator", "tool", aliases=["alpha", "k8s"])
        return first, second

    def test_the_fallback_reads_the_partial_index(self, store):
        plan = " ".join(
            r[-1]
            for r in store.db.execute(
                "EXPLAIN QUERY PLAN SELECT rowid, aliases FROM entities "
                "WHERE aliases IS NOT NULL AND aliases NOT IN ('', '[]')"
            )
        )
        assert "idx_entities_with_aliases" in plan

    def test_the_first_row_in_table_order_wins(self, store):
        first, _ = self._two_sharing_an_alias(store)
        assert store.find_entity("K8S")["id"] == first

    def test_a_rowid_reused_between_the_reads_never_answers(self, store, monkeypatch):
        """The matched row is deleted and its rowid reused before the second read.

        ``entities`` has no AUTOINCREMENT, so SQLite hands the freed top rowid to
        the next insert; an alias must not resolve to that unrelated entity.
        """
        store.add_entity("Postgres", "tool", aliases=["pg"])
        doomed = store.add_entity("Kubernetes", "tool", aliases=["k8s"])
        real = store.db
        raced = {"done": False}

        class _RaceAfterCandidates:
            def __getattr__(self, name):
                return getattr(real, name)

            def execute(self, sql, *args):
                cur = real.execute(sql, *args)
                if "aliases NOT IN" in sql and not raced["done"]:
                    raced["done"] = True
                    rows = cur.fetchall()
                    rowid = real.execute(
                        "SELECT rowid FROM entities WHERE id = ?", (doomed,)
                    ).fetchone()[0]
                    real.execute("DELETE FROM entities WHERE id = ?", (doomed,))
                    real.execute(
                        "INSERT INTO entities (rowid, id, name, entity_type, aliases, "
                        "created_at, updated_at) VALUES (?, 'redis', 'Redis', 'tool', "
                        "'[\"cache\"]', '', '')",
                        (rowid,),
                    )
                    return SimpleNamespace(fetchall=lambda: rows)
                return cur

        monkeypatch.setattr(type(store), "db", property(lambda _self: _RaceAfterCandidates()))
        assert store.find_entity("k8s") is None
        assert raced["done"]

    def test_the_answer_is_the_same_without_the_index(self, store):
        first, _ = self._two_sharing_an_alias(store)
        with_index = store.find_entity("k8s")
        store.db.execute("DROP INDEX idx_entities_with_aliases")
        store.db.commit()
        assert store.find_entity("k8s") == with_index
        assert with_index["id"] == first

    def test_it_is_built_post_bind_for_a_populated_library(self, tmp_path):
        path = str(tmp_path / "kb.db")
        s = KnowledgeStore(path)
        s.add_entity("widget", "concept", aliases=["gadget"])
        s.db.execute("DROP INDEX idx_entities_with_aliases")
        s.db.commit()
        s.close()
        reopened = KnowledgeStore(path)
        try:
            assert "idx_entities_with_aliases" not in _index_names(reopened)
            assert reopened.has_pending_one_time_work()
            reopened.ensure_search_indexes()
            assert "idx_entities_with_aliases" in _index_names(reopened)
            assert reopened.find_entity("GADGET")["name"] == "widget"
        finally:
            reopened.close()


_CANDIDATES = "SELECT id, embedding FROM items WHERE embedding IS NOT NULL AND status = 'active'"


def _row_path_build(store, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(retrieval, "_read_packed", lambda *a, **k: None)
        return retrieval._build_matrix(store, _CANDIDATES, [], _DIM)


class TestOneStatementRead:
    """The matrix build reads every packed vector in one statement, not row by row."""

    def test_it_builds_what_the_row_path_builds(self, store, monkeypatch):
        rng = random.Random(31)
        for i in range(25):
            store.add_item(f"doc {i}", "c" * 500, "doc", embedding=floats_to_bytes(_vec(rng)))
        store.add_item("no vector", "c", "doc")
        with monkeypatch.context() as m:
            m.setattr(retrieval, "_read_snapshot", _refuse_row_read)
            gen, (ids, mat, norms, retained) = retrieval._build_matrix(store, _CANDIDATES, [], _DIM)
        ref_gen, (ref_ids, ref_mat, ref_norms, ref_retained) = _row_path_build(store, monkeypatch)
        assert (gen, ids, retained) == (ref_gen, ref_ids, ref_retained)
        assert mat.tobytes() == ref_mat.tobytes() and norms.tobytes() == ref_norms.tobytes()

    def test_an_empty_library_builds_nothing(self, store, monkeypatch):
        with monkeypatch.context() as m:
            m.setattr(retrieval, "_read_snapshot", _refuse_row_read)
            assert retrieval._build_matrix(store, _CANDIDATES, [], _DIM)[1] is None

    @pytest.mark.parametrize(
        "odd_blob",
        [
            pytest.param(json.dumps([0.5] * _DIM).encode(), id="legacy-json"),
            pytest.param(floats_to_bytes([0.5] * (_DIM + 1)), id="another-width"),
            pytest.param(b"[1]".ljust(_DIM * 4), id="packed-width-json-list"),
            pytest.param("x" * (_DIM * 4), id="text-of-packed-width"),
        ],
    )
    def test_anything_but_packed_vectors_goes_to_the_row_path(self, store, monkeypatch, odd_blob):
        rng = random.Random(32)
        for i in range(5):
            store.add_item(f"doc {i}", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        store.add_item("odd", "c", "doc", embedding=odd_blob)
        assert retrieval._read_packed(store, _CANDIDATES, [], _DIM * 4) is None
        q = _vec(rng)
        assert _retriever(store, q)._vector_search("q", limit=20) == _reference(
            store, q, monkeypatch
        )

    def test_a_utf16_library_goes_to_the_row_path(self, tmp_path, monkeypatch):
        path = str(tmp_path / "utf16.db")
        raw = sqlite3.connect(path)
        raw.execute("PRAGMA encoding = 'UTF-16le'")
        raw.execute("CREATE TABLE seed (x)")
        raw.commit()
        raw.close()
        s = KnowledgeStore(path)
        try:
            rng = random.Random(33)
            for i in range(5):
                s.add_item(f"doc {i}", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
            assert s.db.execute("PRAGMA encoding").fetchone()[0] == "UTF-16le"
            assert retrieval._read_packed(s, _CANDIDATES, [], _DIM * 4) is None
            q = _vec(rng)
            assert _retriever(s, q)._vector_search("q", limit=20) == _reference(s, q, monkeypatch)
        finally:
            s.close()

    def test_a_result_past_the_length_limit_goes_to_the_row_path(self, store, monkeypatch):
        """SQLITE_TOOBIG reaches Python as DataError; the search must still answer.

        The bundled pysqlite3 has no ``setlimit``, so the aggregate statement is
        made to raise what SQLite raises past its length limit.
        """
        rng = random.Random(35)
        for i in range(25):
            store.add_item(f"doc {i}", "c", "doc", embedding=floats_to_bytes(_vec(rng)))
        q = _vec(rng)
        expected = _reference(store, q, monkeypatch)
        retrieval.clear_vector_matrix_cache()
        real = store.db

        class _TooBigCursor:
            def __init__(self, cur):
                object.__setattr__(self, "_cur", cur)

            def __getattr__(self, name):
                return getattr(self._cur, name)

            def __setattr__(self, name, value):
                setattr(self._cur, name, value)

            def execute(self, sql, *args):
                if "group_concat" in sql:
                    raise retrieval.sqlite3.DataError("string or blob too big")
                return self._cur.execute(sql, *args)

        class _TooBig:
            def __getattr__(self, name):
                return getattr(real, name)

            def cursor(self, *args):
                return _TooBigCursor(real.cursor(*args))

        monkeypatch.setattr(type(store), "db", property(lambda _self: _TooBig()))
        assert retrieval._read_packed(store, _CANDIDATES, [], _DIM * 4) is None
        assert _retriever(store, q)._vector_search("q", limit=20) == expected

    def test_scoped_candidates_read_in_one_statement_too(self, store, monkeypatch):
        rng = random.Random(34)
        a = store.add_source("a", "file", "file:///a")
        for i in range(6):
            store.add_item(
                f"doc {i}",
                "c",
                "doc",
                source_id=a if i % 2 else None,
                embedding=floats_to_bytes(_vec(rng)),
            )
        q = _vec(rng)
        r = _retriever(store, q)
        with monkeypatch.context() as m:
            m.setattr(retrieval, "_read_snapshot", _refuse_row_read)
            scoped = r._vector_search("q", limit=20, source_id=a)
        retrieval.clear_vector_matrix_cache()
        with monkeypatch.context() as m:
            m.setattr(retrieval, "_cached_matrix", lambda *a, **k: None)
            assert scoped == r._vector_search("q", limit=20, source_id=a)


def _refuse_row_read(*_a, **_k):
    raise AssertionError("the build read the candidates row by row")
