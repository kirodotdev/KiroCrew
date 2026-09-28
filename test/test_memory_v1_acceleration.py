"""V1 validity filtering must preserve its optional vector accelerators."""

from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import vector_memory as vm
from kiro_crew.vector_memory import VectorMemoryStore


def _forget(store: VectorMemoryStore, episode_id: str) -> None:
    changed = store.db.execute(
        "UPDATE memory_record_meta SET status = 'forgotten' WHERE record_id = ?",
        (episode_id,),
    )
    assert changed.rowcount == 1
    store.db.commit()


def _write(store: VectorMemoryStore, text: str, embedding: list[float]) -> str:
    assert store.write_episodic(text, embedding=embedding)
    row = store.db.execute("SELECT id FROM episodic_memories WHERE text = ?", (text,)).fetchone()
    assert row is not None
    return str(row["id"])


@pytest.mark.skipif(not vm._HAS_NUMPY, reason="numpy acceleration is unavailable")
def test_v1_forgotten_row_keeps_resident_numpy_scoring(tmp_path: Path) -> None:
    store = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    try:
        store.init()
        store._faiss_index = None
        forgotten = _write(store, "forgotten exact vector", [1.0, 0.0])
        kept = _write(store, "eligible nearby vector", [0.8, 0.6])
        _forget(store, forgotten)

        # Exercise numpy's real mat-vec path. Validity metadata filters the
        # resident population; it must not demote the whole V1 store to the
        # per-row stdlib scan merely because one record is ineligible.
        with patch.object(vm.np, "asarray", wraps=vm.np.asarray) as asarray:
            result = store.search_episodic(
                query_embedding=[1.0, 0.0], query_text="vector", limit=1, mmr=False
            )

        assert asarray.called
        assert [row["id"] for row in result] == [kept]
        assert store._episodic_scoring is not None
    finally:
        store.close()


@pytest.mark.skipif(
    not (vm._HAS_FAISS and vm._HAS_NUMPY), reason="FAISS acceleration is unavailable"
)
def test_v1_faiss_overfetches_past_forgotten_top_hits(tmp_path: Path) -> None:
    store = VectorMemoryStore(db_path=tmp_path / "memory.db", embedding_dim=2)
    try:
        store.init()
        # Keep setup independent of write-time FAISS dedup; this regression is
        # about the rebuilt search index and its candidate window.
        store._faiss_index = None
        first = _write(store, "forgotten closest vector", [1.0, 0.0])
        second = _write(store, "forgotten second vector", [0.99, 0.1])
        kept = _write(store, "eligible third vector", [0.8, 0.6])
        _forget(store, first)
        _forget(store, second)
        assert store.build_faiss_index() == 3
        native_index = store._faiss_index
        assert native_index is not None
        search_widths: list[int] = []

        class _SearchProbe:
            @property
            def ntotal(self) -> int:
                return int(native_index.ntotal)  # type: ignore[attr-defined]

            def search(self, query, k: int):
                search_widths.append(k)
                return native_index.search(query, k)  # type: ignore[attr-defined]

        store._faiss_index = _SearchProbe()

        # A limit=1 search requests only two neighbours. Both top hits are
        # now ineligible, so finding the third proves the FAISS request widened
        # for blocked ids instead of starving the candidate window.
        result = store.search_episodic(
            query_embedding=[1.0, 0.0], query_text="vector", limit=1, mmr=False
        )

        assert [row["id"] for row in result] == [kept]
        assert search_widths and search_widths[0] >= 3
    finally:
        store.close()
