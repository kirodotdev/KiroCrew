"""Store and embedder builders shared by the ``test_vector_memory_coverage*.py`` suites.

A fresh ``VectorMemoryStore`` on a SQLite file under ``tmp_path`` at a fixed embedding
width, a deterministic embedder, a ``progress(done, total)`` recorder, and readers for
the raw rows the assertions check.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from kiro_crew.vector_memory import VectorMemoryStore, _lesson_display_text


def _lesson_texts(store: VectorMemoryStore) -> list[str]:
    """Stored lessons rendered as text — write_lesson stores the mapping shape."""
    return [_lesson_display_text(json.loads(e["value_json"])) for e in store.get_lessons()]


_DIM = 8


def _store(tmp_path: Path, *, episodic_max: int = 10_000) -> VectorMemoryStore:
    store = VectorMemoryStore(
        db_path=tmp_path / "mem.db", embedding_dim=_DIM, episodic_max=episodic_max
    )
    store.init()
    return store


def _fixed_embed(value: float = 0.5, dim: int = _DIM) -> Callable[[str], list[float] | None]:
    """A deterministic embedder: every text maps to the same unit-able vector."""
    return lambda _text: [value] * dim


def _recorder(sink: list[tuple[int, int]]) -> Callable[[int, int], None]:
    """A ``progress(done, total)`` callback that records every report."""

    def _record(done: int, total: int) -> None:
        sink.append((done, total))

    return _record


def _raw_semantic_embedding(store: VectorMemoryStore, key: str) -> bytes | None:
    row = store.db.execute("SELECT embedding FROM semantic_memory WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["embedding"]


def _episodic_texts(store: VectorMemoryStore, *, deleted: bool = False) -> list[str]:
    rows = store.db.execute(
        "SELECT text FROM episodic_memories WHERE is_deleted = ? ORDER BY created_at",
        (1 if deleted else 0,),
    ).fetchall()
    return [r["text"] for r in rows]
