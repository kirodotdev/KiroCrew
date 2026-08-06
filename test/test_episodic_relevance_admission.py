"""Episodic recall must admit a relevant memory before recency can rank it out.

Regression for the bug where ``get_episodic_context`` filtered on raw cosine only
AFTER ``search_episodic`` had ranked candidates by a time-decayed score and
truncated to ``limit``: a highly relevant but old memory was ordered past the cut
by a cluster of recent-but-irrelevant rows, which the filter then dropped, leaving
empty context while an exact match sat in the store.

These exercise the stdlib ``_sqlite_vector_search`` path (FAISS is optional and its
index is empty for directly-inserted rows), so they run identically with or without
faiss installed.
"""

from __future__ import annotations

import math
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kiro_crew.vector_memory import _EPISODIC_LONG_TEXT_CHARS, VectorMemoryStore

# Query embedding. Stored vectors are unit vectors, so their dot product with this
# is exactly their cosine similarity (the search path normalises the query).
_Q = [1.0, 0.0, 0.0, 0.0]


def _unit_at_cosine(cos: float) -> list[float]:
    """A unit vector whose dot product with ``_Q`` equals ``cos``."""
    return [cos, math.sqrt(max(0.0, 1.0 - cos * cos)), 0.0, 0.0]


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _store(tmp_path: Path) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / "mem.db")
    store.init()
    return store


def _insert(
    store: VectorMemoryStore,
    mem_id: str,
    text: str,
    cosine: float,
    days_old: int,
    importance: float = 0.5,
) -> None:
    vec = _unit_at_cosine(cosine)
    blob = struct.pack(f"{len(vec)}f", *vec)
    ts = _iso_days_ago(days_old)
    store.db.execute(
        "INSERT INTO episodic_memories "
        "(id, conversation_id, text, tags, embedding, importance, "
        "created_at, last_accessed_at, is_deleted) "
        "VALUES (?, '', ?, '[]', ?, ?, ?, ?, 0)",
        (mem_id, text, blob, importance, ts, ts),
    )
    store.db.commit()


def _seed_old_exact_plus_recent_fillers(store: VectorMemoryStore) -> None:
    # One highly relevant match, 90 days old, and ten irrelevant notes from today.
    # Decay scores: exact ~1.0*0.85*exp(-2.7)=0.057; each filler ~0.20*0.85=0.170.
    # So every filler outranks the exact match on the time-decayed score.
    _insert(store, "exact", "EXACT the deploy target is us-west-2", cosine=1.0, days_old=90)
    for i in range(10):
        _insert(store, f"filler-{i}", f"FILLER unrelated note {i}", cosine=0.20, days_old=0)


class TestEpisodicRelevanceAdmission:
    def test_old_exact_match_survives_recent_irrelevant_fillers(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_old_exact_plus_recent_fillers(store)
        ctx = store.get_episodic_context(query_embedding=_Q)
        # Before the fix the exact match was ranked past `limit` by the fillers, which
        # were then dropped by the cosine gate, so this returned "".
        assert "EXACT" in ctx
        assert "FILLER" not in ctx

    def test_search_with_relevance_filter_admits_the_old_match(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _seed_old_exact_plus_recent_fillers(store)
        ids = {r["id"] for r in store.search_episodic(query_embedding=_Q, relevance_filter=True)}
        assert "exact" in ids
        assert not any(i.startswith("filler") for i in ids)

    def test_default_search_stays_unfiltered_for_dashboard_and_api(self, tmp_path: Path) -> None:
        # The default (relevance_filter=False) must still return the full ranked set,
        # sub-threshold rows included, so dashboard/API/CLI callers are unchanged.
        store = _store(tmp_path)
        _seed_old_exact_plus_recent_fillers(store)
        ids = {r["id"] for r in store.search_episodic(query_embedding=_Q)}
        assert any(i.startswith("filler") for i in ids)

    def test_recent_exact_match_still_returned(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _insert(store, "exact", "EXACT recent relevant memory", cosine=1.0, days_old=0)
        assert "EXACT" in store.get_episodic_context(query_embedding=_Q)

    def test_only_irrelevant_memories_yields_empty_context(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        for i in range(5):
            _insert(store, f"note-{i}", f"note {i}", cosine=0.20, days_old=0)
        assert store.get_episodic_context(query_embedding=_Q) == ""

    def test_long_text_gets_the_relaxed_threshold(self, tmp_path: Path) -> None:
        # A long entry at cosine 0.5 clears the relaxed 0.42 gate; an identical-cosine
        # short entry does not clear the 0.55 gate. Locks the length-aware admission.
        store = _store(tmp_path)
        _insert(store, "long", "LONG " + "x" * (_EPISODIC_LONG_TEXT_CHARS + 10), 0.5, days_old=0)
        _insert(store, "short", "SHORT y", cosine=0.5, days_old=0)
        ctx = store.get_episodic_context(query_embedding=_Q)
        assert "LONG" in ctx
        assert "SHORT" not in ctx
