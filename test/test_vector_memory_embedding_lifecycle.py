"""Tests for how a memory's embedding is produced, stored, repaired and indexed.

They pin lazy ``embed_fn`` rebinding and content-free embed logs, write-time semantic
vectors that context ranking reads back and the scoring cache over them, writes that carry
no vector, deferred embedding and the backfill sweep, FAISS index builds that skip
mismatched dimensions, dedup against tombstoned vectors, id-map rollback and repair, the
recorded embedding space, and the logged, counted, warn-once refusal of an
episodic-to-semantic promotion, a pass that clusters only rows with a stored vector. Recall
ranking lives in ``test_vector_memory_recall_ranking.py`` and the ``_db_lock`` discipline in
``test_vector_memory_lock_discipline.py``.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from unittest import mock

import pytest

import kiro_crew.embeddings as embeddings_mod
from kiro_crew.vector_memory import _HAS_FAISS, _HAS_NUMPY, SemanticRejectCode, VectorMemoryStore

# Same xdist group as ``test_vector_memory.py`` and the topic files its docstring names, so
# this file's tests share that group's worker; nothing in this file reads the source tree.
pytestmark = pytest.mark.xdist_group(name="tree_scan_test_vector_memory")


class TestEmbedLogsCarryNoMemoryText:
    """The embed diagnostics log sizes, never the memory text itself.

    ``text`` is user memory content. A DEBUG line that echoes even a 50-char
    prefix puts that content into the log; the three branches (success, None,
    exception) must all stay content-free.
    """

    _SENTINEL = "SENTINEL-MEMORY-CONTENT-do-not-log"

    def _store(self, tmp_path: Path, *, opened) -> VectorMemoryStore:
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        return store

    def _assert_not_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        for record in caplog.records:
            assert self._SENTINEL not in record.getMessage()
            assert self._SENTINEL not in (record.exc_text or "")

    def test_success_branch(self, tmp_path: Path, caplog: pytest.LogCaptureFixture, opened) -> None:
        store = self._store(tmp_path, opened=opened)
        store.embed_fn = lambda _t: [0.1, 0.2]
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.vector_memory"):
            assert store._try_embed(self._SENTINEL) == [0.1, 0.2]
        self._assert_not_logged(caplog)

    def test_none_branch(self, tmp_path: Path, caplog: pytest.LogCaptureFixture, opened) -> None:
        store = self._store(tmp_path, opened=opened)
        store.embed_fn = lambda _t: None
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.vector_memory"):
            assert store._try_embed(self._SENTINEL) is None
        self._assert_not_logged(caplog)

    def test_exception_branch(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, opened
    ) -> None:
        store = self._store(tmp_path, opened=opened)

        def boom(_t: str) -> list[float]:
            raise RuntimeError("embedder down")

        store.embed_fn = boom
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.vector_memory"):
            assert store._try_embed(self._SENTINEL) is None
        self._assert_not_logged(caplog)


class TestEmbedFnLazyRebind:
    """Tests for lazy embed_fn rebinding via embed_fn_factory.

    Before this fix, if Ollama was unavailable at gateway boot,
    vector_memory.embed_fn stayed None for the entire gateway lifetime, and
    every new memory wrote with embedding=NULL. Lazy rebind recovers from this
    by retrying the factory on subsequent embed attempts (rate-limited).
    """

    def test_no_factory_returns_none(self, tmp_path: Path, opened) -> None:
        """When neither embed_fn nor factory is set, _try_embed returns None."""
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        assert store.embed_fn is None
        assert store.embed_fn_factory is None
        assert store._try_embed("hello") is None

    def test_factory_lazily_binds_when_available(self, tmp_path: Path, opened) -> None:
        """If embed_fn is None but factory returns a working callable, it binds."""
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        store._embed_fn_rebind_cooldown_secs = 0.0  # disable cooldown for test

        def good_embed(_: str) -> list[float]:
            return [0.1, 0.2, 0.3]

        store.embed_fn_factory = lambda: good_embed

        result = store._try_embed("hello")
        assert result == [0.1, 0.2, 0.3]
        assert store.embed_fn is good_embed  # rebound

    def test_factory_returning_none_does_not_bind(self, tmp_path: Path, opened) -> None:
        """If factory returns None (Ollama still down), embed_fn stays None."""
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        store._embed_fn_rebind_cooldown_secs = 0.0
        store.embed_fn_factory = lambda: None

        assert store._try_embed("hello") is None
        assert store.embed_fn is None

    def test_factory_returning_broken_callable_does_not_bind(self, tmp_path: Path, opened) -> None:
        """If factory returns a callable that always returns None, do not bind it."""
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        store._embed_fn_rebind_cooldown_secs = 0.0

        def broken_embed(_: str) -> None:
            return None

        store.embed_fn_factory = lambda: broken_embed

        assert store._try_embed("hello") is None
        assert store.embed_fn is None  # probe failed — do not bind

    def test_cooldown_prevents_repeated_factory_calls(self, tmp_path: Path, opened) -> None:
        """Cooldown rate-limits factory invocations when Ollama stays down."""
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        store._embed_fn_rebind_cooldown_secs = 60.0  # long cooldown

        call_count = [0]

        def factory() -> None:
            call_count[0] += 1
            return None

        store.embed_fn_factory = factory

        store._try_embed("first")
        store._try_embed("second")
        store._try_embed("third")
        # Only the first attempt should have called the factory; cooldown blocks the rest.
        assert call_count[0] == 1

    def test_factory_exception_is_swallowed(self, tmp_path: Path, opened) -> None:
        """Factory raising must not break _try_embed."""
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        store._embed_fn_rebind_cooldown_secs = 0.0

        def boom() -> None:
            raise RuntimeError("ollama unreachable")

        store.embed_fn_factory = boom

        assert store._try_embed("hello") is None  # no exception
        assert store.embed_fn is None

    def test_existing_embed_fn_takes_precedence(self, tmp_path: Path, opened) -> None:
        """If embed_fn is already set, factory is never consulted."""
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        store._embed_fn_rebind_cooldown_secs = 0.0

        def primary(_: str) -> list[float]:
            return [1.0, 2.0]

        called = [False]

        def factory():
            called[0] = True
            return lambda _t: [9.9, 9.9]

        store.embed_fn = primary
        store.embed_fn_factory = factory

        result = store._try_embed("hello")
        assert result == [1.0, 2.0]
        assert called[0] is False  # factory must not be touched

    def test_factory_returning_empty_list_probe_does_not_bind(self, tmp_path: Path, opened) -> None:
        """If probe returns an empty list (zero-dim or misconfigured model), do not bind.

        The original `if probe:` check
        was falsy for `[]` AND for `0` AND for `None`, conflating "probe failed" with
        "probe returned a degenerate response." The tightened check rejects empty/None
        explicitly so a misconfigured model can't slip through as a working embed_fn.
        """
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        store._embed_fn_rebind_cooldown_secs = 0.0

        def empty_embed(_: str) -> list[float]:
            return []  # zero-dim — would have been treated as "probe failed" too aggressively

        store.embed_fn_factory = lambda: empty_embed

        assert store._try_embed("hello") is None
        assert store.embed_fn is None  # empty probe must not bind

    def test_rebind_lock_serializes_concurrent_factory_calls(self, tmp_path: Path, opened) -> None:
        """Two threads racing into the rebind block share at most one factory call per cooldown.

        Without the lock, both threads
        could observe `embed_fn is None` and `cooldown elapsed` simultaneously, then both
        call the factory + probe. With the lock, the loser sees the cooldown bumped and skips.
        """
        import threading

        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        store._embed_fn_rebind_cooldown_secs = 60.0  # long cooldown so the loser is blocked

        call_count = [0]
        in_factory = threading.Event()
        release_factory = threading.Event()

        def slow_factory():
            call_count[0] += 1
            in_factory.set()  # signal "I'm in the factory"
            release_factory.wait(
                timeout=2.0
            )  # wait until the other thread has had a chance to race
            return lambda _t: [0.1, 0.2, 0.3]

        store.embed_fn_factory = slow_factory

        results: list[list[float] | None] = [None, None]

        def worker(idx: int) -> None:
            results[idx] = store._try_embed("hello")

        t1 = threading.Thread(target=worker, args=(0,))
        t2 = threading.Thread(target=worker, args=(1,))
        t1.start()
        # Wait for t1 to be inside the factory (holding the lock), then start t2.
        in_factory.wait(timeout=2.0)
        t2.start()
        # Give t2 a moment to attempt to enter the lock and block.
        # Then release t1's factory call so it completes.
        release_factory.set()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        # Exactly one factory call despite two concurrent _try_embed invocations.
        # The lock serializes; the loser sees embed_fn is not None on re-check
        # and skips the factory entirely.
        assert call_count[0] == 1, f"Lock failed: factory called {call_count[0]} times"
        assert store.embed_fn is not None  # one of the threads bound it
        # Both threads should have gotten the embedding (loser used the bound embed_fn)
        assert results[0] == [0.1, 0.2, 0.3]
        assert results[1] == [0.1, 0.2, 0.3]


@pytest.mark.skipif(not _HAS_NUMPY, reason="numpy not available (Linux-compiled binary)")
class TestFaissDimMismatch:
    """Tests for build_faiss_index dimension validation (skip mismatched entries)."""

    def test_mismatched_dim_skipped_not_crashed(self, tmp_path: Path, monkeypatch, opened) -> None:
        """Entries with wrong embedding dim are skipped, not added to the index."""
        from unittest.mock import MagicMock

        import numpy as np

        import kiro_crew.vector_memory as vm_mod

        # Mock faiss with a real-behaving IndexFlatIP stand-in
        mock_index = MagicMock()
        mock_index.ntotal = 0
        added_vectors = []

        def mock_add(vec):
            # Simulate real faiss behavior: assert dimension matches
            if vec.shape[1] != 768:
                raise AssertionError(f"assert {vec.shape[1]} == 768")
            added_vectors.append(vec)
            mock_index.ntotal += 1

        mock_index.add = mock_add

        mock_faiss = MagicMock()
        mock_faiss.IndexFlatIP = MagicMock(return_value=mock_index)

        monkeypatch.setattr(vm_mod, "_HAS_FAISS", True)
        monkeypatch.setattr(vm_mod, "_HAS_NUMPY", True)
        monkeypatch.setattr(vm_mod, "faiss", mock_faiss)

        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=768))
        store.init()

        # Insert episodic entries with correct (768) and wrong (1024) dims
        good_vec = np.random.randn(768).astype(np.float32)
        bad_vec = np.random.randn(1024).astype(np.float32)

        store.db.execute(
            "INSERT INTO episodic_memories (id, text, embedding, is_deleted, importance, created_at, last_accessed_at) "
            "VALUES (?, ?, ?, 0, 1.0, '2026-01-01', '2026-01-01')",
            ("good-1", "correct dim entry", good_vec.tobytes()),
        )
        store.db.execute(
            "INSERT INTO episodic_memories (id, text, embedding, is_deleted, importance, created_at, last_accessed_at) "
            "VALUES (?, ?, ?, 0, 1.0, '2026-01-01', '2026-01-01')",
            ("bad-1", "wrong dim entry", bad_vec.tobytes()),
        )
        store.db.commit()

        # build_faiss_index should NOT crash — should skip the bad entry
        count = store.build_faiss_index()

        # Only the good entry should be indexed
        assert count == 1
        assert len(store._faiss_id_map) == 1
        assert store._faiss_id_map[0] == "good-1"

    def test_all_mismatched_dims_yields_empty_index(
        self, tmp_path: Path, monkeypatch, opened
    ) -> None:
        """If all entries have wrong dims, index builds empty without crashing."""
        from unittest.mock import MagicMock

        import numpy as np

        import kiro_crew.vector_memory as vm_mod

        mock_index = MagicMock()
        mock_index.ntotal = 0
        mock_faiss = MagicMock()
        mock_faiss.IndexFlatIP = MagicMock(return_value=mock_index)

        monkeypatch.setattr(vm_mod, "_HAS_FAISS", True)
        monkeypatch.setattr(vm_mod, "_HAS_NUMPY", True)
        monkeypatch.setattr(vm_mod, "faiss", mock_faiss)

        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=768))
        store.init()

        bad_vec = np.random.randn(1024).astype(np.float32)
        store.db.execute(
            "INSERT INTO episodic_memories (id, text, embedding, is_deleted, importance, created_at, last_accessed_at) "
            "VALUES (?, ?, ?, 0, 1.0, '2026-01-01', '2026-01-01')",
            ("bad-only", "all wrong", bad_vec.tobytes()),
        )
        store.db.commit()

        count = store.build_faiss_index()
        assert count == 0
        assert len(store._faiss_id_map) == 0


@pytest.mark.skipif(not _HAS_NUMPY, reason="numpy not available (Linux-compiled binary)")
class TestWriteEpisodicWithoutEmbedding:
    """write_episodic must not crash when the write has no embedding but the
    FAISS index is non-empty.

    Repro: memories were written with embeddings (index populated), then
    embeddings are disabled (embedding_provider="none" -> embed_fn unbound) or
    a single embed call fails. The FAISS dedup block must not dereference the
    unbound `vec` local and raise UnboundLocalError, losing the memory.
    Regression guard for the gateway consolidation crash.
    """

    dim = 16

    def _store_with_mock_index(self, tmp_path: Path, monkeypatch, *, opened):
        """Store whose FAISS index is a populated mock (ntotal=1)."""
        from unittest.mock import MagicMock

        import kiro_crew.vector_memory as vm_mod

        mock_faiss = MagicMock()
        monkeypatch.setattr(vm_mod, "_HAS_FAISS", True)
        monkeypatch.setattr(vm_mod, "_HAS_NUMPY", True)
        monkeypatch.setattr(vm_mod, "faiss", mock_faiss)

        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=self.dim))
        store.init()

        # Seed one embedded row and wire the mock index to it (ntotal=1)
        import numpy as np

        seed_vec = np.ones(self.dim, dtype=np.float32)
        seed_vec /= np.linalg.norm(seed_vec)
        store.db.execute(
            "INSERT INTO episodic_memories (id, text, embedding, is_deleted, importance, "
            "created_at, last_accessed_at) VALUES (?, ?, ?, 0, 1.0, '2026-01-01', '2026-01-01')",
            ("existing-1", "Team agreed on PostgreSQL for the database layer", seed_vec.tobytes()),
        )
        store.db.commit()

        mock_index = MagicMock()
        mock_index.ntotal = 1
        store._faiss_index = mock_index
        store._faiss_id_map = ["existing-1"]
        return store, mock_index

    def test_no_embedding_write_does_not_crash(self, tmp_path: Path, monkeypatch, opened) -> None:
        """A write with embeddings disabled degrades gracefully (no dedup, no crash)."""
        store, mock_index = self._store_with_mock_index(tmp_path, monkeypatch, opened=opened)
        store.embed_fn = None  # embeddings disabled (embedding_provider="none")

        # Distinct text (different 80-char prefix) so text-hash dedup does not apply.
        # Pre-fix this raised UnboundLocalError on `vec` inside the dedup block.
        assert store.write_episodic("User decided to use Rust for the parser rewrite")

        # Dedup search must have been skipped and nothing added to the index
        mock_index.search.assert_not_called()
        mock_index.add.assert_not_called()

        row = store.db.execute(
            "SELECT embedding FROM episodic_memories WHERE is_deleted = 0 "
            "AND text = 'User decided to use Rust for the parser rewrite'"
        ).fetchone()
        assert row is not None
        assert row["embedding"] is None  # persisted without a vector

    def test_embedded_writes_still_dedup(self, tmp_path: Path, monkeypatch, opened) -> None:
        """FAISS dedup still runs for writes that DO carry an embedding."""
        import numpy as np

        store, mock_index = self._store_with_mock_index(tmp_path, monkeypatch, opened=opened)

        # Near-identical vector -> cosine above threshold -> conflict_skip.
        # Keep the text SHORTER than 1.2x the seeded entry so the dedup takes
        # the conflict_skip path (returns False), not the longer-text merge path.
        mock_index.search.return_value = (
            np.array([[0.99]], dtype=np.float32),
            np.array([[0]], dtype=np.int64),
        )
        duplicate_vec = [1.0] * self.dim
        assert not store.write_episodic(
            "Same vector, new text prefix here",
            embedding=duplicate_vec,
        )
        mock_index.search.assert_called_once()


class TestEpisodicGhostVectorDedup:
    """Round-2 bugfix: a new episodic write that matches a TOMBSTONED (deleted)
    vector still living in the FAISS index must NOT be rejected as a conflict.

    Tombstone paths (merge, dashboard delete, cap eviction, stale retirement) set
    is_deleted=1 but leave the vector in _faiss_index/_faiss_id_map. dedup then
    matched that ghost, _get_episodic returned None (is_deleted=0 filter), and the
    old code hit the else-branch `return False`, silently dropping the new memory.
    """

    def _seed_tombstoned_ghost(self, tmp_path: Path, *, opened):
        if not _HAS_NUMPY:
            pytest.skip("numpy not available")
        import numpy as np

        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=8))
        store.init()
        # Fixed unit embedding so the mock index can report a high self-similarity.
        vec = [1.0] + [0.0] * 7
        store.embed_fn = lambda text: vec
        # Seed one real episodic row, then tombstone it (leaving a FAISS ghost).
        assert store.write_episodic("Team chose PostgreSQL for storage") is not False
        row = store.db.execute(
            "SELECT id FROM episodic_memories WHERE is_deleted = 0 LIMIT 1"
        ).fetchone()
        ghost_id = row["id"]
        store.db.execute("UPDATE episodic_memories SET is_deleted = 1 WHERE id = ?", (ghost_id,))
        store.db.commit()

        # Mock FAISS so the next write's dedup search returns a >threshold hit that
        # maps to the tombstoned ghost id.
        class _MockIndex:
            ntotal = 1

            def search(self, q, k):
                return np.array([[0.99]], dtype=np.float32), np.array([[0]])

            def add(self, vec):
                # Accept the post-dedup add of the new vector (bumps ntotal).
                self.ntotal += 1

        store._faiss_index = _MockIndex()
        store._faiss_id_map = [ghost_id]
        store._dedup_threshold = 0.88
        return store, ghost_id

    def test_write_matching_tombstoned_ghost_is_accepted(self, tmp_path: Path, opened) -> None:
        store, ghost_id = self._seed_tombstoned_ghost(tmp_path, opened=opened)
        # New memory whose embedding matches the ghost above the dedup threshold.
        result = store.write_episodic("We standardized on Postgres for the database layer")
        # Must be stored, NOT rejected against a deleted memory.
        assert result is not False
        live = store.db.execute(
            "SELECT text FROM episodic_memories WHERE is_deleted = 0"
        ).fetchall()
        assert any("standardized on Postgres" in r["text"] for r in live)
        # And it must not have been logged as a conflict_skip against the ghost.
        conflicts = [
            e
            for e in store.get_events()
            if e["event_type"] == "conflict_skip" and e.get("memory_key") == ghost_id
        ]
        assert conflicts == []


@pytest.mark.skipif(not (_HAS_FAISS and _HAS_NUMPY), reason="faiss/numpy not available")
class TestBackfillMissingEmbeddings:
    """Re-embed sweep: embed episodic rows written without a vector."""

    def test_backfills_null_rows_and_extends_index(self, tmp_path: Path, opened) -> None:
        # Write episodic entries with NO embed_fn → embedding stored as NULL.
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        assert store.write_episodic("User standardized on Postgres for storage")
        assert store.write_episodic("User prefers pytest over unittest for tests")
        null_before = store.db.execute(
            "SELECT COUNT(*) FROM episodic_memories WHERE embedding IS NULL"
        ).fetchone()[0]
        assert null_before == 2

        # Bind an embed_fn returning a NON-unit vector and sweep.
        store.embed_fn = lambda text: [0.1] * store._embedding_dim
        embedded = store.backfill_missing_embeddings()
        assert embedded == 2
        null_after = store.db.execute(
            "SELECT COUNT(*) FROM episodic_memories WHERE embedding IS NULL"
        ).fetchone()[0]
        assert null_after == 0
        # The resident index includes the freshly embedded rows without a rebuild.
        assert store._faiss_index is not None
        assert store._faiss_index.ntotal == 2  # type: ignore[attr-defined]
        # Stored vectors must be L2-normalized (IndexFlatIP scores IP == cosine
        # only on unit vectors) — matching write_episodic().
        import numpy as _np

        blob = store.db.execute(
            "SELECT embedding FROM episodic_memories WHERE is_deleted=0 LIMIT 1"
        ).fetchone()[0]
        stored = _np.frombuffer(blob, dtype=_np.float32)
        assert abs(float(_np.linalg.norm(stored)) - 1.0) < 1e-5

    def test_dim_mismatch_left_null_for_retry(self, tmp_path: Path, opened) -> None:
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        assert store.write_episodic("A memory that will get a bad-dim embedding")
        # embed_fn returns the WRONG dimension → row must stay NULL (not stored
        # non-NULL, which would block every future backfill retry).
        store.embed_fn = lambda text: [0.1] * (store._embedding_dim + 3)
        assert store.backfill_missing_embeddings() == 0
        null_count = store.db.execute(
            "SELECT COUNT(*) FROM episodic_memories WHERE embedding IS NULL"
        ).fetchone()[0]
        assert null_count == 1

    def test_noop_without_embed_fn(self, tmp_path: Path, opened) -> None:
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        store.write_episodic("Some memory without any embedding attached")
        # No embed_fn bound → sweep is a no-op, rows stay NULL.
        assert store.backfill_missing_embeddings() == 0
        null_count = store.db.execute(
            "SELECT COUNT(*) FROM episodic_memories WHERE embedding IS NULL"
        ).fetchone()[0]
        assert null_count == 1

    def test_noop_when_nothing_pending(self, tmp_path: Path, opened) -> None:
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        store.embed_fn = lambda text: [0.1] * store._embedding_dim
        # No episodic rows at all → returns 0.
        assert store.backfill_missing_embeddings() == 0

    def test_bounded_pages_extend_resident_index_without_full_rebuild(
        self, tmp_path: Path, opened
    ) -> None:
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        for index in range(33):
            assert store.write_episodic(
                f"Deferred bounded repair row {index}", defer_embedding=True
            )
        store.embed_fn = lambda text: [0.1] * store._embedding_dim
        with (
            mock.patch.object(store, "build_faiss_index") as rebuild,
            mock.patch.object(store, "save_faiss_index") as save,
        ):
            assert store.backfill_missing_embeddings(max_rows_per_kind=16, pace=False) == 16
            assert store.backfill_missing_embeddings(max_rows_per_kind=16, pace=False) == 16
            assert store.backfill_missing_embeddings(max_rows_per_kind=16, pace=False) == 1
        rebuild.assert_not_called()
        save.assert_not_called()
        assert store._faiss_index is not None
        assert store._faiss_index.ntotal == 33  # type: ignore[attr-defined]


def test_fresh_store_records_ready_custom_space_before_first_vector(
    tmp_path, monkeypatch, opened
) -> None:
    dimension = 20

    class _CustomBackend:
        model_id = "custom:test-space"

        def __init__(self):
            self.dim = dimension

        @staticmethod
        def is_ready():
            return True

        @staticmethod
        def embed(text, *, priority=0):
            index = int(text.rsplit(" ", 1)[-1])
            vector = [0.0] * dimension
            vector[index] = 1.0
            return vector

    backend = _CustomBackend()
    monkeypatch.setattr(embeddings_mod, "_shared_embedder", backend)
    monkeypatch.setattr(embeddings_mod, "_backend_factory", lambda: backend)
    store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dimension))
    store.init()
    assert store.recorded_embedding_space() is None
    assert store.has_stored_embeddings() is False
    store.embed_fn = embeddings_mod.make_sync_embed_fn()

    for index in range(dimension):
        assert store.write_episodic(f"Fresh custom vector {index}")

    assert store.recorded_embedding_space() == embeddings_mod.embedding_space_signature(
        backend.model_id, backend.dim
    )
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM episodic_memories WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        == dimension
    )


@pytest.mark.skipif(not _HAS_NUMPY, reason="numpy not available")
class TestDeferredEmbedding:
    """``defer_embedding`` + the backfill sweep that must fill the NULLs in.

    Deliberately NOT gated on faiss: it is an optional accelerator and not a
    declared dependency, so a faiss-gated test class silently skips on a stock
    install — which is how the sweep shipped as a no-op there in the first place.
    """

    def test_defer_embedding_stores_null_and_skips_the_embed(self, tmp_path: Path, opened) -> None:
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        calls: list[str] = []

        def _embed(text: str) -> list[float]:
            calls.append(text)
            return [0.1] * store._embedding_dim

        store.embed_fn = _embed
        assert store.write_episodic(
            "Deferred row that must land without a vector", defer_embedding=True
        )
        # The expensive inference never ran on the caller's thread...
        assert calls == []
        # ...and the row is on disk, just without a vector yet.
        assert (
            store.db.execute(
                "SELECT COUNT(*) FROM episodic_memories WHERE is_deleted=0 AND embedding IS NULL"
            ).fetchone()[0]
            == 1
        )

    def test_backfill_fills_deferred_rows(self, tmp_path: Path, opened) -> None:
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        store.embed_fn = lambda text: [0.1] * store._embedding_dim
        for index in range(3):
            assert store.write_episodic(
                f"Deferred memory number {index} for the sweep", defer_embedding=True
            )

        assert store.backfill_missing_embeddings() == 3
        assert (
            store.db.execute(
                "SELECT COUNT(*) FROM episodic_memories WHERE is_deleted=0 AND embedding IS NULL"
            ).fetchone()[0]
            == 0
        )

    def test_backfill_runs_without_faiss(self, tmp_path: Path, monkeypatch, opened) -> None:
        """Gating the sweep on faiss made it a silent no-op on a stock install.

        faiss is not a declared dependency, so every deferred row would have
        stayed NULL forever — invisible to vector search with no error anywhere.
        ``search_episodic`` needs only the stored blob (``_sqlite_vector_search``),
        so the vectors are useful with or without the index.
        """
        import kiro_crew.vector_memory as vm_mod

        monkeypatch.setattr(vm_mod, "_HAS_FAISS", False)
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        store.embed_fn = lambda text: [0.1] * store._embedding_dim
        assert store.write_episodic(
            "A row that needs a vector but has no faiss", defer_embedding=True
        )

        assert store.backfill_missing_embeddings() == 1
        assert (
            store.db.execute(
                "SELECT COUNT(*) FROM episodic_memories WHERE is_deleted=0 AND embedding IS NULL"
            ).fetchone()[0]
            == 0
        )

    def test_deferred_rows_are_keyword_searchable_before_backfill(
        self, tmp_path: Path, opened
    ) -> None:
        """Nothing is lost in the interim — FTS5 finds the row with no vector."""
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        assert store.write_episodic(
            "The deployment pipeline uses a canary stage before production",
            defer_embedding=True,
        )
        results = store.search_episodic(query_text="canary deployment pipeline", limit=5)
        assert any("canary" in row["text"] for row in results)


class TestFaissIndexIdMapSync:
    """Regression guards for the FAISS index / _faiss_id_map desync bug.

    The C++ index (index.ntotal) and the parallel Python _faiss_id_map are two
    structures that must stay in lockstep. If a mid-write add fails after the id
    was appended (or the persisted pair drifts), later lookups return wrong
    results or IndexError on _faiss_id_map[idx]. The write path must apply both
    atomically (rolling back on failure) and load must detect + repair a desync.
    """

    def _fake_embed(self, dim: int):
        def _embed(text: str):
            seed = sum(ord(c) for c in text)
            return [float((seed + i) % 7) + 0.1 for i in range(dim)]

        return _embed

    def test_write_rolls_back_id_map_when_faiss_add_fails(self, tmp_path: Path, opened) -> None:
        if not (_HAS_FAISS and _HAS_NUMPY):
            pytest.skip("FAISS/numpy not available on this platform")

        dim = 16
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dim))
        store.init()
        store.embed_fn = self._fake_embed(dim)
        store.build_faiss_index()

        # Land one clean vector so the structures start in sync and non-empty.
        assert store.write_episodic("first episodic memory about topic alpha beta")
        assert store._faiss_index.ntotal == len(store._faiss_id_map)
        baseline_ntotal = store._faiss_index.ntotal
        baseline_ids = len(store._faiss_id_map)

        # Force the FAISS add to raise mid-write. The id must be rolled back so
        # index.ntotal stays equal to len(_faiss_id_map) (no dangling id).
        real_add = store._faiss_index.add

        def _boom(_vec):
            raise RuntimeError("simulated FAISS add failure")

        store._faiss_index.add = _boom  # type: ignore[assignment]
        with pytest.raises(RuntimeError):
            store.write_episodic("second episodic memory about topic gamma delta")
        store._faiss_index.add = real_add  # type: ignore[assignment]

        assert len(store._faiss_id_map) == baseline_ids
        assert store._faiss_index.ntotal == baseline_ntotal
        assert store._faiss_index.ntotal == len(store._faiss_id_map)

    def test_load_detects_and_rebuilds_on_desync(self, tmp_path: Path, opened) -> None:
        if not (_HAS_FAISS and _HAS_NUMPY):
            pytest.skip("FAISS/numpy not available on this platform")

        import json

        dim = 16
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dim))
        store.init()
        store.embed_fn = self._fake_embed(dim)
        store.build_faiss_index()
        for i in range(4):
            assert store.write_episodic(f"episodic memory number {i} about topic alpha beta")
        store.save_faiss_index()
        expected = store._faiss_index.ntotal
        assert expected == len(store._faiss_id_map) == 4

        # Corrupt the persisted id-map so it is shorter than the index (a desync
        # exactly like an interrupted mid-write would leave behind).
        id_map_path = store._faiss_path.with_suffix(".ids.json")
        truncated = json.loads(id_map_path.read_text(encoding="utf-8"))[:2]
        id_map_path.write_text(json.dumps(truncated), encoding="utf-8")

        # A fresh store loading the corrupt pair must detect the mismatch and
        # rebuild from SQLite (source of truth) rather than serve corrupt lookups.
        store2 = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dim))
        store2.init()
        store2.embed_fn = self._fake_embed(dim)
        assert store2.load_faiss_index() is False  # rebuilt, not loaded as-is
        assert store2._faiss_index.ntotal == len(store2._faiss_id_map)
        assert store2._faiss_index.ntotal == expected

    def test_load_accepts_consistent_index(self, tmp_path: Path, opened) -> None:
        if not (_HAS_FAISS and _HAS_NUMPY):
            pytest.skip("FAISS/numpy not available on this platform")

        dim = 16
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dim))
        store.init()
        store.embed_fn = self._fake_embed(dim)
        store.build_faiss_index()
        for i in range(3):
            assert store.write_episodic(f"episodic memory number {i} about topic alpha beta")
        store.save_faiss_index()

        store2 = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dim))
        store2.init()
        store2.embed_fn = self._fake_embed(dim)
        # In-sync pair loads as-is (True) and stays consistent.
        assert store2.load_faiss_index() is True
        assert store2._faiss_index.ntotal == len(store2._faiss_id_map) == 3


class TestSemanticWriteTimeEmbedding:
    """set_semantic persists a value vector; retrieval ranks from storage.

    The write path embeds ``"<key> <value_json>"`` in _write_semantic's tail
    and get_semantic_context ranks from the STORED blobs (one query embed per
    request, never one per row). The backfill sweep repairs rows written while
    the model was absent, including set_semantic_if_absent imports.
    """

    _DIM = 8

    def _directional_embed(self, counter: list[int] | None = None):
        """Deterministic embed: one axis per known topic word.

        ``tokyo`` and ``nippon`` share an axis so a query can hit a row on the
        VECTOR term with zero keyword overlap — isolating what is under test.
        Optionally counts calls via *counter* (a single-element list).
        """
        axes = {"paris": 0, "tokyo": 1, "nippon": 1, "coffee": 2}

        def _embed(text: str) -> list[float]:
            if counter is not None:
                counter[0] += 1
            vec = [0.0] * self._DIM
            for word, i in axes.items():
                if word in text.lower():
                    vec[i] += 1.0
            if not any(vec):
                vec[self._DIM - 1] = 1.0
            return vec

        return _embed

    def _stored_embedding(self, store: VectorMemoryStore, key: str) -> bytes | None:
        row = store.db.execute(
            "SELECT embedding FROM semantic_memory WHERE key = ?", (key,)
        ).fetchone()
        assert row is not None
        return row["embedding"]

    def test_set_semantic_persists_embedding(self, tmp_path: Path, opened) -> None:
        import struct

        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        embed = self._directional_embed()
        store.embed_fn = embed

        assert store.set_semantic("pref.city", "Paris", 1.0, "user_explicit") is None

        blob = self._stored_embedding(store, "pref.city")
        assert blob is not None, "write-time embedding was not persisted"
        # The stored vector is the embed of "<key> <value_json>" — the same
        # text retrieval ranks against.
        expected = embed('pref.city "Paris"')
        assert list(struct.unpack(f"{len(blob) // 4}f", blob)) == pytest.approx(expected)

    def test_update_replaces_stale_vector(self, tmp_path: Path, opened) -> None:
        import struct

        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        embed = self._directional_embed()
        store.embed_fn = embed

        assert store.set_semantic("pref.city", "Paris", 1.0, "user_explicit") is None
        assert store.set_semantic("pref.city", "Tokyo", 1.0, "user_explicit") is None

        blob = self._stored_embedding(store, "pref.city")
        assert blob is not None
        assert list(struct.unpack(f"{len(blob) // 4}f", blob)) == pytest.approx(
            embed('pref.city "Tokyo"')
        )

    def test_update_without_embed_fn_clears_stale_vector(self, tmp_path: Path, opened) -> None:
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        store.embed_fn = self._directional_embed()
        assert store.set_semantic("pref.city", "Paris", 1.0, "user_explicit") is None
        assert self._stored_embedding(store, "pref.city") is not None

        # Model gone: the re-write must NOT keep ranking by the old value's
        # vector — NULL leaves the row a backfill candidate.
        store.embed_fn = None
        assert store.set_semantic("pref.city", "Tokyo", 1.0, "user_explicit") is None
        assert self._stored_embedding(store, "pref.city") is None

    def test_space_swap_mid_embed_leaves_null(self, tmp_path: Path, opened) -> None:
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        inner = self._directional_embed()

        def _swapping_embed(text: str):
            # A model swap lands while the embed is in flight: the produced
            # vector belongs to the OLD space and must not be committed.
            store._space_generation += 1
            return inner(text)

        store.embed_fn = _swapping_embed
        assert store.set_semantic("pref.city", "Paris", 1.0, "user_explicit") is None
        assert self._stored_embedding(store, "pref.city") is None

    def test_lesson_write_keeps_raw_rule_vector(self, tmp_path: Path, opened) -> None:
        import struct

        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        counter = [0]
        embed = self._directional_embed(counter)
        store.embed_fn = embed

        rule = "always drink coffee before reviews"
        assert store.write_lesson(rule).wrote is True
        lessons = store.get_lessons()
        assert len(lessons) == 1
        blob = lessons[0]["embedding"]
        assert isinstance(blob, bytes)
        # write_lesson owns the lesson vector contract: raw rule text, NOT the
        # "<key> <value_json>" envelope _write_semantic embeds for KV rows.
        assert list(struct.unpack(f"{len(blob) // 4}f", blob)) == pytest.approx(embed(rule))
        # counter includes the assertion line's own embed(rule) call above.
        # Exactly ONE embed during write_lesson (the rule): _write_semantic must
        # skip lesson.* keys, or every lesson write pays a second, discarded
        # embed of the JSON envelope.
        assert counter[0] == 2, "lesson write embedded more than the rule text"

    def test_get_semantic_context_ranks_from_stored_vectors(self, tmp_path: Path, opened) -> None:
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        store.embed_fn = self._directional_embed()
        # "nippon" shares a vector axis with "tokyo" but has zero keyword
        # overlap with the query, so ranking it first requires the stored
        # vector to be read — the keyword term alone cannot produce it.
        assert store.set_semantic("pref.travel", "nippon", 1.0, "user_explicit") is None
        assert store.set_semantic("pref.drink", "coffee", 1.0, "user_explicit") is None

        counter = [0]
        store.embed_fn = self._directional_embed(counter)
        ctx = store.get_semantic_context(query_text="tokyo")

        assert counter[0] == 1, "retrieval must embed only the query, never per row"
        lines = [ln for ln in ctx.splitlines() if ln.startswith("pref.")]
        assert lines and lines[0].startswith("pref.travel"), ctx

    def test_write_time_embed_uses_bulk_priority(self, tmp_path: Path, opened) -> None:
        from kiro_crew.embeddings import PRIORITY_BULK

        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        seen: list[int] = []

        def _embed(text: str, priority: int = -1) -> list[float]:
            seen.append(priority)
            return [1.0] * self._DIM

        _embed.accepts_priority = True  # type: ignore[attr-defined]
        store.embed_fn = _embed

        assert store.set_semantic("pref.city", "Paris", 1.0, "user_explicit") is None
        # The write-time vector is a corpus-style deferred asset (nothing
        # blocks on it), reached from consolidation and import loops — it must
        # never queue ahead of interactive or explicit-write embeds.
        assert seen == [PRIORITY_BULK]

    def test_reaffirmation_keeps_vector_without_reembedding(self, tmp_path: Path, opened) -> None:
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        texts: list[str] = []
        inner = self._directional_embed()

        def _embed(text: str) -> list[float]:
            texts.append(text)
            return inner(text)

        store.embed_fn = _embed
        assert store.set_semantic("pref.city", "Paris", 1.0, "user_explicit") is None
        blob = self._stored_embedding(store, "pref.city")
        assert blob is not None

        # Same value re-affirmed (consolidation rewrites the same keys every
        # cycle): the upsert keeps the vector and the tail skips the embed —
        # re-embedding identical text would spend an inference per cycle.
        assert store.set_semantic("pref.city", "Paris", 1.0, "user_explicit") is None
        assert self._stored_embedding(store, "pref.city") == blob
        assert texts.count('pref.city "Paris"') == 1, texts

    def test_semantic_scoring_cache_reuses_rows_without_changing_context(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        store.embed_fn = self._directional_embed()
        assert store.set_semantic("pref.travel", "nippon", 1.0, "user_explicit") is None
        assert store.set_semantic("pref.drink", "coffee", 1.0, "user_explicit") is None

        expected = store.get_semantic_context(query_text="tokyo")
        assert store._semantic_scoring is not None
        original_fetch = store._fetch_all_locked
        scoring_scans = 0

        def count_scoring_scans(sql: str, params: tuple = ()):
            nonlocal scoring_scans
            if sql.startswith(
                "SELECT key, value_json, updated_at, embedding, source FROM semantic_memory"
            ):
                scoring_scans += 1
            return original_fetch(sql, params)

        monkeypatch.setattr(store, "_fetch_all_locked", count_scoring_scans)
        assert store.get_semantic_context(query_text="tokyo") == expected
        assert scoring_scans == 0, "unchanged semantic context must reuse its resident scoring rows"

    def test_semantic_scoring_cache_follows_a_local_write_and_delete(self, tmp_path: Path) -> None:
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        assert store.set_semantic("pref.city", "Paris", 1.0, "user_explicit") is None
        assert "pref.city: Paris" in store.get_semantic_context(query_text="paris")
        assert store._semantic_scoring is not None
        generation = store._semantic_scoring_generation

        # The write patches the resident set under a new generation rather than dropping it.
        assert store.set_semantic("pref.city", "Tokyo", 1.0, "user_explicit") is None
        assert store._semantic_scoring_generation > generation
        assert store._semantic_scoring is not None
        assert store._semantic_scoring.generation == store._semantic_scoring_generation
        context = store.get_semantic_context(query_text="tokyo")
        assert "pref.city: Tokyo" in context
        assert "Paris" not in context

        assert store.delete_semantic("pref.city", "user_explicit") is True
        assert store._semantic_scoring is not None
        assert store._semantic_scoring.rows == ()
        assert store.get_semantic_context(query_text="tokyo") == ""

    @pytest.mark.parametrize("writer", ["upsert", "insert_if_absent", "delete"])
    def test_semantic_scoring_invalidation_stays_inside_writer_lock(
        self, tmp_path: Path, monkeypatch, writer: str
    ) -> None:
        class _CheckedRLock:
            def __init__(self) -> None:
                self._lock = threading.RLock()
                self._owner: int | None = None
                self._depth = 0

            def __enter__(self) -> "_CheckedRLock":
                self._lock.acquire()
                current = threading.get_ident()
                if self._owner == current:
                    self._depth += 1
                else:
                    assert self._owner is None
                    self._owner = current
                    self._depth = 1
                return self

            def __exit__(self, exc_type, exc_value, traceback) -> None:
                assert self._owner == threading.get_ident()
                self._depth -= 1
                if self._depth == 0:
                    self._owner = None
                self._lock.release()

            def held_by_current_thread(self) -> bool:
                return self._owner == threading.get_ident()

        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        assert store.set_semantic("pref.city", "Paris", 1.0, "user_explicit") is None
        assert store.get_semantic_context(query_text="paris")

        checked_lock = _CheckedRLock()
        monkeypatch.setattr(store, "_db_lock", checked_lock)
        original_invalidate = store._invalidate_semantic_scoring

        def invalidate_under_writer_lock(*args: object) -> None:
            assert checked_lock.held_by_current_thread()
            original_invalidate(*args)

        monkeypatch.setattr(store, "_invalidate_semantic_scoring", invalidate_under_writer_lock)

        if writer == "upsert":
            assert store.set_semantic("pref.city", "Tokyo", 1.0, "user_explicit") is None
        elif writer == "insert_if_absent":
            assert (
                store.set_semantic_if_absent("pref.country", "Japan", 1.0, "user_explicit")
                == "imported"
            )
        else:
            assert store.delete_semantic("pref.city", "user_explicit") is True

    def test_semantic_scoring_cache_invalidates_after_deferred_vector_backfill(
        self, tmp_path: Path
    ) -> None:
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        assert store.set_semantic("pref.city", "Paris", 1.0, "user_explicit") is None
        assert "pref.city: Paris" in store.get_semantic_context(query_text="paris")
        assert store._semantic_scoring is not None

        store.embed_fn = self._directional_embed()
        assert store._backfill_semantic_kv_embeddings(pace=False) == 1
        assert store._semantic_scoring is None
        assert store.get_semantic_context(query_text="paris")

    def test_semantic_scoring_cache_detects_an_external_commit(self, tmp_path: Path) -> None:
        import sqlite3 as stdlib_sqlite3

        db_path = tmp_path / "mem.db"
        store = VectorMemoryStore(db_path=db_path)
        store.init()
        assert store.set_semantic("pref.city", "Paris", 1.0, "user_explicit") is None
        assert "pref.city: Paris" in store.get_semantic_context(query_text="paris")
        assert store._semantic_scoring is not None

        with stdlib_sqlite3.connect(db_path) as other:
            other.execute(
                "UPDATE semantic_memory SET value_json = ?, updated_at = ? WHERE key = ?",
                ('"Tokyo"', "2099-01-01T00:00:00+00:00", "pref.city"),
            )

        refreshed = store.get_semantic_context(query_text="tokyo")
        assert "pref.city: Tokyo" in refreshed
        assert "pref.city: Paris" not in refreshed

    def test_semantic_scoring_cache_is_dropped_on_a_connection_swap(self, tmp_path: Path) -> None:
        # The validity token is per-connection (data_version), so a retained
        # snapshot must not survive a connection swap: a fresh connection's
        # baseline could equal it and serve stale rows. Both a repeated init()
        # (swap on a live object) and close() drop it, like the episodic twin.
        store = VectorMemoryStore(db_path=tmp_path / "mem.db")
        store.init()
        assert store.set_semantic("pref.city", "Paris", 1.0, "user_explicit") is None
        assert store.get_semantic_context(query_text="paris")
        assert store._semantic_scoring is not None

        store.init()  # repeated init swaps the connection
        assert store._semantic_scoring is None
        assert store._semantic_scoring_refused is None

        assert store.get_semantic_context(query_text="paris")
        assert store._semantic_scoring is not None
        store.close()
        assert store._semantic_scoring is None
        assert store._semantic_scoring_refused is None

    def test_null_vector_row_does_not_outrank_embedded_row(self, tmp_path: Path, opened) -> None:
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        # Legacy row written while the model was absent: NULL vector, but a
        # perfect keyword overlap with the query (key + value hit every word).
        assert (
            store.set_semantic(
                "pref.tokyo_travel_plans", "tokyo travel plans", 1.0, "user_explicit"
            )
            is None
        )
        # Embedded row: zero keyword overlap, perfect vector match ("nippon"
        # shares the query's axis in the fake embedding space).
        store.embed_fn = self._directional_embed()
        assert store.set_semantic("pref.trip", "nippon", 1.0, "user_explicit") is None

        ctx = store.get_semantic_context(query_text="tokyo travel plans")
        lines = [ln for ln in ctx.splitlines() if ln.startswith("pref.")]
        # Both rows score on the same weighted scale: the vectorless row must
        # not keep the unweighted keyword score (1.0) and outrank the
        # vector-matched row (0.6) — evidence-backed rows win.
        assert lines and lines[0].startswith("pref.trip"), ctx

    def test_embedding_persist_failure_does_not_fail_the_write(
        self, tmp_path: Path, opened
    ) -> None:
        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        store.embed_fn = self._directional_embed()

        # A DB error while persisting the derived vector (disk full, I/O error)
        # must not escape: the semantic row is already committed, and callers
        # batch many keys per call — an exception here would discard every
        # remaining item in the batch. The row stays NULL for the backfill.
        import sqlite3 as _sqlite3

        real_db = store.db

        class _FailingDb:
            def execute(self, sql: str, *args: object):
                if sql.startswith("UPDATE semantic_memory SET embedding"):
                    raise _sqlite3.OperationalError("disk I/O error")
                return real_db.execute(sql, *args)

            def __getattr__(self, name: str):
                return getattr(real_db, name)

        store._db = _FailingDb()  # type: ignore[assignment]
        try:
            assert store.set_semantic("pref.city", "Paris", 1.0, "user_explicit") is None
        finally:
            store._db = real_db

        row = store.db.execute(
            "SELECT value_json, embedding FROM semantic_memory WHERE key = 'pref.city'"
        ).fetchone()
        assert row is not None and row["value_json"] == '"Paris"'
        assert row["embedding"] is None

    def test_backfill_covers_semantic_kv_rows(self, tmp_path: Path, opened) -> None:
        import struct

        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db"))
        store.init()
        # Written while the model is absent: rows land with NULL embeddings.
        assert store.set_semantic("pref.city", "Paris", 1.0, "user_explicit") is None
        assert store.set_semantic_if_absent("pref.drink", "coffee", 1.0, "import") == "imported"
        assert self._stored_embedding(store, "pref.city") is None
        assert self._stored_embedding(store, "pref.drink") is None

        embed = self._directional_embed()
        store.embed_fn = embed
        store.backfill_missing_embeddings()

        for key, value_json in (("pref.city", '"Paris"'), ("pref.drink", '"coffee"')):
            blob = self._stored_embedding(store, key)
            assert blob is not None, f"backfill left {key} NULL"
            assert list(struct.unpack(f"{len(blob) // 4}f", blob)) == pytest.approx(
                embed(f"{key} {value_json}")
            )


@pytest.mark.skipif(not _HAS_NUMPY, reason="numpy not available (Linux-compiled binary)")
class TestPromotionSkipIsObservable:
    """A rejected promotion keeps its episodic rows, so it re-clusters and re-attempts forever.

    The success branch owns the only log line in the loop, so before this the retry was silent.
    """

    def test_rejected_promotion_is_logged_and_counted(self, tmp_path: Path, caplog, opened) -> None:
        import logging
        from unittest.mock import patch

        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=8))
        store.init()
        store._faiss_index = None  # keep the promotion corpus independent of optional dedup
        store.embed_fn = lambda text: [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

        for i in range(3):
            store.write_episodic(f"user prefers {i}")

        # set_semantic refuses: promoted stays 0 and the rows survive. The mock returns the REAL
        # tuple shape, so these assertions can still fail if the log stops unpacking it.
        with (
            patch.object(store, "_infer_semantic_key", return_value="pref.os"),
            patch.object(
                store,
                "set_semantic",
                return_value=(SemanticRejectCode.VALUE_EMPTY, "Value must not be null or empty"),
            ),
            patch.object(store, "_delete_episodic_row") as mock_delete,
            # Scoped to this module's logger: the package level is raised elsewhere, so an
            # unscoped at_level passes alone and fails in a full-suite run.
            caplog.at_level(logging.INFO, logger="kiro_crew.vector_memory"),
        ):
            promoted = store.promote_episodic_patterns(min_count=1, min_sim=0.0)

        assert promoted == 0
        # Rows are retained, which is what makes the refusal repeat on every later pass.
        assert not mock_delete.called
        msgs = [r.getMessage() for r in caplog.records]
        assert any(
            "Promotion skipped pref.os (value_empty: Value must not be null or empty)" in m
            for m in msgs
        ), msgs
        assert any("promoted," in m and "skipped" in m for m in msgs), msgs

    def test_repeat_refusal_warns_once_but_counts_every_pass(
        self, tmp_path: Path, caplog, opened
    ) -> None:
        """The refusal is deterministic, so a second pass must not re-warn about the same key."""
        import logging
        from unittest.mock import patch

        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=8))
        store.init()
        store._faiss_index = None  # keep the promotion corpus independent of optional dedup
        store.embed_fn = lambda text: [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

        for i in range(3):
            store.write_episodic(f"user prefers {i}")

        with (
            patch.object(store, "_infer_semantic_key", return_value="pref.os"),
            patch.object(
                store,
                "set_semantic",
                return_value=(SemanticRejectCode.VALUE_EMPTY, "Value must not be null or empty"),
            ),
            patch.object(store, "_delete_episodic_row"),
            caplog.at_level(logging.INFO, logger="kiro_crew.vector_memory"),
        ):
            store.promote_episodic_patterns(min_count=1, min_sim=0.0)
            store.promote_episodic_patterns(min_count=1, min_sim=0.0)

        warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len([m for m in warns if "Promotion skipped pref.os" in m]) == 1, warns
        # The count is NOT suppressed: both passes still report the skip in their summary.
        summaries = [m for m in caplog.records if "promoted," in m.getMessage()]
        assert len(summaries) == 2, [m.getMessage() for m in summaries]

    def test_refusal_set_is_bounded_so_an_evicted_key_warns_again(
        self, tmp_path: Path, caplog, opened
    ) -> None:
        """The warn-once set is capped, so eviction trades a repeat warning for bounded memory."""
        import logging
        from unittest.mock import patch

        import kiro_crew.vector_memory as vm_mod

        store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=8))
        store.init()
        store._faiss_index = None  # keep the promotion corpus independent of optional dedup
        store.embed_fn = lambda text: [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

        for i in range(3):
            store.write_episodic(f"user prefers {i}")

        # Cap 0 evicts the entry as soon as it is added, which is the eviction path itself
        # rather than a stand-in for it: the second pass must therefore warn again.
        with (
            patch.object(vm_mod, "_MAX_PROMOTION_REFUSED", 0),
            patch.object(store, "_infer_semantic_key", return_value="pref.os"),
            patch.object(
                store,
                "set_semantic",
                return_value=(SemanticRejectCode.VALUE_EMPTY, "Value must not be null or empty"),
            ),
            patch.object(store, "_delete_episodic_row"),
            caplog.at_level(logging.INFO, logger="kiro_crew.vector_memory"),
        ):
            store.promote_episodic_patterns(min_count=1, min_sim=0.0)
            store.promote_episodic_patterns(min_count=1, min_sim=0.0)
            assert len(store._promotion_refused) == 0

        warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len([m for m in warns if "Promotion skipped pref.os" in m]) == 2, warns
