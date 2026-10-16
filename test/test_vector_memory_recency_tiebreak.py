"""Recency reads that cut with ``LIMIT`` order over a total order.

Each read sorts newest first and breaks a timestamp tie on the row's primary
key, so rows written within one clock tick come back in the same order, and
the same subset, on every read. Rows are inserted in a shuffled key order so
that neither the table scan nor the timestamp-index scan happens to produce
key order on its own: only the tie-break does.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.vector_memory import VectorMemoryStore

_STAMP = "2026-01-01T00:00:00+00:00"
# Shuffled: neither this order nor its reverse is sorted.
_INSERT_ORDER = [7, 2, 11, 0, 5, 9, 3, 10, 1, 6, 8, 4]


def _store(tmp_path: Path, opened) -> VectorMemoryStore:
    store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
    store.init()
    return store


def _insert_tied_episodes(store: VectorMemoryStore, text: str = "tied episode") -> list[str]:
    ids = [f"ep-{n:02d}" for n in _INSERT_ORDER]
    with store._db_lock:
        for mem_id in ids:
            store.db.execute(
                "INSERT INTO episodic_memories "
                "(id, conversation_id, text, tags, importance, created_at, is_deleted) "
                "VALUES (?, 'c', ?, '[]', 0.5, ?, 0)",
                (mem_id, f"{text} {mem_id}", _STAMP),
            )
        store.db.commit()
    return sorted(ids)


class TestEpisodicListPagination:
    def test_pages_over_tied_rows_follow_id_order_and_cover_each_row_once(
        self, tmp_path: Path, opened
    ) -> None:
        """Fails without the ``id`` tie-break: tied rows then come back in scan order."""
        store = _store(tmp_path, opened)
        expected = _insert_tied_episodes(store)
        page1 = [r["id"] for r in store.get_episodic_list(limit=5, offset=0)]
        page2 = [r["id"] for r in store.get_episodic_list(limit=5, offset=5)]
        page3 = [r["id"] for r in store.get_episodic_list(limit=5, offset=10)]
        walked = page1 + page2 + page3
        assert walked == expected
        assert len(set(walked)) == len(expected)
        assert page1 == [r["id"] for r in store.get_episodic_list(limit=5, offset=0)]


class TestRetiredEpisodicPagination:
    def test_pages_over_tied_retirements_follow_id_order_and_cover_each_row_once(
        self, tmp_path: Path, opened
    ) -> None:
        """Fails without the ``e.id`` tie-break.

        It also fails with the episode's ``created_at``, ``conversation_id`` or
        ``importance`` in its place.
        """
        store = _store(tmp_path, opened)
        expected = _insert_tied_episodes(store)
        with store._db_lock:
            # The GROUP BY e.id step can hand the sort its rows in id order, so
            # a constant second sort term could pass by accident. These three
            # columns therefore run in the shuffled insert order, which is
            # neither id order nor its reverse.
            for rank, mem_id in enumerate(f"ep-{n:02d}" for n in _INSERT_ORDER):
                store.db.execute(
                    "UPDATE episodic_memories SET is_deleted = 1, conversation_id = ?, "
                    "importance = ?, created_at = ? WHERE id = ?",
                    (f"c-{rank:02d}", rank / 100, f"2025-12-{rank + 1:02d}T00:00:00+00:00", mem_id),
                )
            # Retirement events written in the reverse of the insert order, all
            # on one stamp, so neither row order nor event order is id order.
            for mem_id in reversed([f"ep-{n:02d}" for n in _INSERT_ORDER]):
                store.db.execute(
                    "INSERT INTO memory_events "
                    "(event_type, memory_type, memory_key, new_value, source, created_at) "
                    "VALUES ('conflict_retire', 'episodic', ?, 'newer', 'semantic_update', ?)",
                    (mem_id, _STAMP),
                )
            store.db.commit()
        page1 = [r["id"] for r in store.get_retired_episodic(limit=5, offset=0)]
        page2 = [r["id"] for r in store.get_retired_episodic(limit=5, offset=5)]
        page3 = [r["id"] for r in store.get_retired_episodic(limit=5, offset=10)]
        walked = page1 + page2 + page3
        assert walked == expected
        assert len(set(walked)) == len(expected)


class TestEpisodicKeywordFallback:
    def test_tied_matches_return_the_first_ids_in_order(self, tmp_path: Path, opened) -> None:
        """Fails without the ``id`` tie-break: the LIMIT keeps a scan-order subset."""
        store = _store(tmp_path, opened)
        expected = _insert_tied_episodes(store, text="kubernetes rollout")
        rows = store._fts5_episodic_search("kubernetes rollout", limit=4)
        assert [r["id"] for r in rows] == expected[:4]


class TestSemanticContextRecency:
    def test_tied_facts_keep_the_first_keys_in_order(self, tmp_path: Path, opened) -> None:
        """Fails without the ``key`` tie-break: the LIMIT keeps a scan-order subset."""
        store = _store(tmp_path, opened)
        # cap=300 holds the no-query read at its 20-row floor, and 24 tied rows
        # exceed it, so the cut decides which facts appear. Each 14-character
        # line fits all 20 rows inside the cap.
        order = _INSERT_ORDER + [n + 12 for n in reversed(_INSERT_ORDER)]
        keys = [f"project.f{n:02d}" for n in order]
        with store._db_lock:
            for key in keys:
                store.db.execute(
                    "INSERT INTO semantic_memory "
                    "(key, value_json, confidence, source, created_at, updated_at, is_deleted) "
                    "VALUES (?, '\"v\"', 1.0, 'user_explicit', ?, ?, 0)",
                    (key, _STAMP, _STAMP),
                )
            store.db.commit()
        ctx = store.get_semantic_context(cap=300)
        shown = [line.split(":", 1)[0] for line in ctx.splitlines() if line.startswith("project.")]
        assert shown == sorted(keys)[:20]
