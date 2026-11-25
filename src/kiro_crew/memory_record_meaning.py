"""Search by meaning for the memory page's record list.

The search words are embedded once with the store's own embedding function, and
every live fact, lesson and experience is scored by cosine against its stored
vector with the store's own scorer (``_stored_similarity_scorer``, the one the
lesson and dedup paths use). Both store lineages expose the same
``semantic_memory`` and ``episodic_memories`` relations with an ``embedding``
column, so one read serves both.

Nothing here imports ``vector_memory``: the store object brings its methods, so
the dashboard's import path stays free of it.
"""

from __future__ import annotations

from typing import Any

#: Rows scoring below this are counted, not listed, until the page asks for them
#: (``weak=1``). A cosine between the query and the row's stored vector.
#: shortcut: one fixed line for every embedding model; per-model calibration
#: (the operating point recall already records) is the upgrade if a model's
#: scores sit much higher or lower.
MEANING_CUTOFF = 0.45

_VECTORS_SQL = (
    "SELECT 'semantic' AS grp, key AS id, embedding FROM semantic_memory "
    "WHERE is_deleted = 0 AND embedding IS NOT NULL UNION ALL "
    "SELECT 'episode', id, embedding FROM episodic_memories "
    "WHERE is_deleted = 0 AND embedding IS NOT NULL"
)


class MeaningUnavailable(Exception):
    """The store cannot embed the search words (no model, or it failed)."""


def embed_query(store: Any, text: str) -> list[float]:
    """The search words' vector. Blocking model inference: call off the event loop
    and outside the store lock."""
    from kiro_crew.embeddings_settings import PRIORITY_INTERACTIVE

    vector = store._try_embed(text, PRIORITY_INTERACTIVE) if text.strip() else None
    if not vector:
        raise MeaningUnavailable
    return vector


def score_rows(store: Any, vector: list[float]) -> dict[tuple[str, str], float]:
    """Cosine per row, keyed ``("semantic"|"episode", id)``, rounded to 2 places.

    A row with no vector, or one from another model's width, is absent; the
    caller scores it 0. Reads under the caller's lock and snapshot.
    """
    scorer = store._stored_similarity_scorer(vector)
    scores: dict[tuple[str, str], float] = {}
    cursor = store.db.execute(_VECTORS_SQL)
    try:
        for row in cursor:
            # Opposite direction ranks like no match: a page score is 0 to 1.
            scores[(row["grp"], row["id"] or "")] = round(max(0.0, scorer({"embedding": row["embedding"]})), 2)
    finally:
        cursor.close()
    return scores


def summary_score(scores: dict[tuple[str, str], float], summary: dict) -> float:
    group = "episode" if summary["kind"] == "episode" else "semantic"
    return scores.get((group, summary["id"] or ""), 0.0)
