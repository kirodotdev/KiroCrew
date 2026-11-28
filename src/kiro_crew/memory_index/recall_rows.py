"""The rows V2 recall scores, read through the memory index when it can answer.

V2 recall admits a row on a cosine floor or on enough shared query terms, then
ranks what it admitted. Without the index it reads every live row to find them.
With ``memory.index.mode: on`` and a ready index it reads only the candidates
the index names (best vectors, rows holding enough query terms, and every
preference, which V2 always admits), then runs the same admission and ranking
on those rows. Every row is read from the store with the caller's own
``is_deleted = 0`` guard, so the index can never surface a deleted memory.

With ``shadow`` the scan still answers; :func:`shadow_compare` counts admitted
rows in the answer's top that the index did not name.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any, Final

from kiro_crew import memory_v2
from kiro_crew.memory_index.store_index import index_for

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)

#: How many of the scan's best rows a shadow comparison checks.
SHADOW_TOP: Final = 64


class Candidates:
    """What the index answered for one query: ``ids`` is ``None`` when the caller
    must scan; ``answers`` says whether the index's rows are the result (``on``)."""

    __slots__ = ("ids", "answers", "store")

    def __init__(self, store: Any, ids: set[str] | None, answers: bool) -> None:
        self.store = store
        self.ids = ids
        self.answers = answers


def admission_min_match(query_terms: frozenset[str]) -> int:
    """The fewest query terms V2's keyword rule admits a row on (``lexical_evidence``)."""
    wanted = len(query_terms)
    if not wanted:
        return 1
    return max(min(2, wanted), math.ceil(wanted / 2))


def candidates(
    store: VectorMemoryStore,
    kind: str,
    query_text: str,
    query_vector: list[float] | None,
    *,
    floor: float,
    skip: bool = False,
) -> Candidates:
    """Every *kind* (``semantic`` or ``episodic``) row V2 admission could admit.

    V2 admits a row on its cosine (at least *floor*, the lowest floor recall uses
    for this query, ``memory_v2.lowest_cosine_floor``) or on enough shared terms
    (``admission_min_match``).
    The index names those rows (exactly below ``memory.index.exact_rows``
    vectors, through an approximate graph above it; see
    ``StoreIndex.candidates``), so admission, ranking and MMR see the rows the
    scan would have admitted. *skip* keeps the scan for a query the index cannot
    narrow.
    """
    index = None if skip else index_for(store)
    if index is None:
        return Candidates(store, None, False)
    ids = index.candidates(
        kind,
        query_text,
        query_vector,
        min_match=admission_min_match(memory_v2.terms(query_text)),
        floor=floor,
    )
    return Candidates(store, ids, ids is not None and index.settings.mode == "on")


def fact_candidates(
    store: VectorMemoryStore,
    query_text: str,
    query_vector: list[float] | None,
    *,
    instructed: bool = False,
) -> Candidates:
    """Candidates for V2 fact recall, which keeps only admitted rows (and every
    preference, which :func:`fact_rows` reads on its own). With no query text it
    keeps every fact, so only the scan can answer. *instructed* says the query
    was embedded with the model's query instruction, which lowers its floors."""
    return candidates(
        store,
        "semantic",
        query_text,
        query_vector,
        floor=memory_v2.lowest_cosine_floor(instructed=instructed),
        skip=not query_text.strip(),
    )


def episode_candidates(
    store: VectorMemoryStore,
    query_text: str,
    query_vector: list[float] | None,
    *,
    relevance_filter: bool,
    skip: bool,
    instructed: bool = False,
) -> Candidates:
    """Candidates for V2 episode recall. Without its relevance filter a search
    keeps every row with a vector, so only the scan can answer it."""
    return candidates(
        store,
        "episodic",
        query_text,
        query_vector,
        floor=memory_v2.lowest_cosine_floor(instructed=instructed),
        skip=skip or not relevance_filter,
    )


def fact_rows(store: VectorMemoryStore, found: Candidates) -> list[Any]:
    """The live non-lesson fact rows V2 fact recall scores: every one without an
    answering index, else the ones the index named plus every preference."""
    if not found.answers:
        return store._fetch_all_locked(
            f"SELECT * FROM {store._sem_rel} WHERE key NOT LIKE 'lesson.%' AND is_deleted = 0{store._sem_guard}",
            scan="semantic",
        )
    keys = sorted(found.ids or ())
    guard = f" AND is_deleted = 0{store._sem_guard}"
    rows = store._fetch_all_locked(
        f"SELECT * FROM {store._sem_rel} WHERE key >= 'pref.' AND key < 'pref/'{guard}"
    )
    seen = {row["key"] for row in rows}
    for start in range(0, len(keys), 500):
        chunk = [
            key
            for key in keys[start : start + 500]
            if key not in seen and not key.startswith("lesson.")
        ]
        if chunk:
            marks = ", ".join("?" for _ in chunk)
            rows.extend(
                store._fetch_all_locked(
                    f"SELECT * FROM {store._sem_rel} WHERE key IN ({marks}){guard}", chunk
                )
            )
    return rows


def episode_rows(store: VectorMemoryStore, found: Candidates) -> list[Any]:
    """The live episodes V2 episode recall scores: every one without an answering
    index, else the ones the index named."""
    if not found.answers:
        return store._fetch_all_locked(
            f"SELECT * FROM {store._epi_rel} WHERE is_deleted = 0{store._epi_guard}",
            scan="episodic",
        )
    ids = sorted(found.ids or ())
    rows: list[Any] = []
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ", ".join("?" for _ in chunk)
        rows.extend(
            store._fetch_all_locked(
                f"SELECT * FROM {store._epi_rel} WHERE id IN ({marks}) AND is_deleted = 0{store._epi_guard}",
                chunk,
            )
        )
    return rows


def shadow_compare(found: Candidates, kind: str, answer: list[dict], field: str) -> None:
    """In ``shadow``, count rows of the scan's top :data:`SHADOW_TOP` the index missed."""
    if found.ids is None or found.answers:
        return
    index = index_for(found.store)
    if index is None:
        return
    top = [
        row[field] for row in answer[:SHADOW_TOP] if not str(row.get("key", "")).startswith("pref.")
    ]
    missed = [ident for ident in top if ident not in found.ids]
    index.record_shadow(kind, len(top), len(missed))
    if missed:
        logger.debug("memory index shadow: %s missed %d of the top %d", kind, len(missed), len(top))
