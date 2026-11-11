"""The FAISS tier of ``search_episodic`` re-reads the index under ``_db_lock``.

Its eligibility checks run before the lock is taken, so an owner edit, a model
swap, a rollback or a concurrent search can drop ``_faiss_index`` while the
search waits. The lock-held read must notice and fall back to the SQLite tier
instead of raising ``AttributeError`` on ``None``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import vector_memory as vm
from kiro_crew.vector_memory import VectorMemoryStore

pytestmark = pytest.mark.xdist_group(name="tree_scan_test_vector_memory")


class _StubIndex:
    ntotal = 3

    def search(self, vec, k):  # pragma: no cover - must not be reached
        raise AssertionError("the dropped index was searched")


class _DropIndexOnFirstAcquire:
    """``_db_lock`` stand-in: the first acquire finds the index already dropped."""

    def __init__(self, store: VectorMemoryStore) -> None:
        self._store, self._inner, self._fired = store, store._db_lock, False

    def __enter__(self):
        if not self._fired:
            self._fired = True
            self._store._faiss_index, self._store._faiss_id_map = None, []
        return self._inner.__enter__()

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


def _embed(text: str) -> list[float]:
    seed = sum(ord(c) for c in text)
    return [float((seed + i) % 7) + 0.1 for i in range(8)]


def test_an_index_dropped_while_waiting_for_the_lock_falls_back(
    tmp_path: Path, opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not vm._HAS_NUMPY:
        pytest.skip("numpy not available")
    store = opened(VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=8))
    store.init()
    store.embed_fn = _embed
    text = "an episode about the findings doc and topic alpha beta"
    store.write_episodic(text, embedding=_embed(text))

    monkeypatch.setattr(vm, "_HAS_FAISS", True)
    store._faiss_index, store._faiss_id_map = _StubIndex(), ["a", "b", "c"]
    store._faiss_data_version = None
    store._db_lock = _DropIndexOnFirstAcquire(store)

    results = store.search_episodic(query_embedding=_embed("findings doc"), limit=5)

    assert [r["text"] for r in results] == [text]
