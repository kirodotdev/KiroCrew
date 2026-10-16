"""What is in a store: the episode list, a carve's pages and counts, and the row counts.

``get_episodic_list`` pages the active episodes for the dashboard with an optional literal
text and tag search (a member store lists them from ``memory_items`` with their carve
columns). ``list_by_facets`` and ``count_by_facet`` page and count a crew store's carve and
refuse a V1 store (``_require_facets``), and ``memory_stats`` reports the row counts the
dashboard shows. Every read goes through the store's locked fetch helpers.

``get_episodic_list`` names ``memory_items``, a relation only the crew lineage has, which
the composition contract keeps out of ``vector_memory_runtime``, so the module lives beside
the facade. The functions are bound on ``VectorMemoryStore`` as the class attributes of the
same name and take the store as ``self``. Store fields read: ``_db_lock``, ``_lineage`` and
``_faiss_id_map``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kiro_crew import memory_schema
from kiro_crew.vector_memory_runtime import text_scoring as _text_scoring

if TYPE_CHECKING:
    from collections.abc import Mapping

    from kiro_crew.vector_memory import VectorMemoryStore


def get_episodic_list(
    self: VectorMemoryStore,
    limit: int = 50,
    offset: int = 0,
    tag_filter: list[str] | None = None,
    *,
    q: str = "",
) -> list[dict]:
    """Active episodes, with optional literal text/tag search before pagination."""
    query = _text_scoring._normalize_memory_search_query(q)
    if tag_filter:
        # Use JSON-quoted exact match to avoid substring false positives
        # e.g. "cr" should not match "cron" or "datacraft"
        tag_conds = " AND (" + " OR ".join(["tags LIKE ?" for _ in tag_filter]) + ")"
        tag_params: tuple[object, ...] = tuple(f'%"{t.lower()}"%' for t in tag_filter)
    else:
        tag_conds = ""
        tag_params = ()
    columns = "id, conversation_id, text, tags, importance, created_at, last_accessed_at"
    relation = "episodic_memories"
    if self.algorithm_version == "v2":
        columns += ", source, scope, surface, crew, session_key, derived_from"
        relation = "memory_items"
        tag_conds = " AND kind = 'episode'" + tag_conds
    if query:
        tag_conds += " AND (memory_text_contains(text, ?, 0) OR memory_text_contains(tags, ?, 1))"
        tag_params += (query, query)
    sql = (
        f"SELECT {columns} FROM {relation} WHERE is_deleted = 0{tag_conds} "
        "ORDER BY created_at DESC, id LIMIT ? OFFSET ?"
    )
    params = (*tag_params, limit, offset)
    if query:
        with self._db_lock:
            self.db.create_function(
                "memory_text_contains",
                3,
                _text_scoring._contains_memory_search_text,
                deterministic=True,
            )
            rows = self._fetch_all_locked(sql, params)
    else:
        rows = self._fetch_all_locked(sql, params)
    return [dict(r) for r in rows]


def _require_facets(self: VectorMemoryStore) -> None:
    """Refuse a facet read unless this file is on the crew lineage.

    The discrimination is ``self._lineage``, resolved once in :meth:`init` from
    the file's own schema — never a ``hasattr`` probe and never a
    ``try``/``except`` around ``no such column``, both of which would answer
    for whatever the last statement happened to touch.

    REFUSAL rather than an empty result, and the same answer at every surface.
    See :class:`memory_schema.FacetsUnsupported`: on v1 the columns do not
    exist, so an empty page would report "this crew has no memories" for a
    store holding thousands of unfaceted rows — a wrong answer to the one
    question these methods exist to answer.
    """
    if self._lineage != memory_schema.LINEAGE_CREW:
        raise memory_schema.FacetsUnsupported(
            "this memory store is on the v1 schema lineage and carries no carve "
            "facets; a facet query is answerable only on a crew memory store"
        )


def list_by_facets(
    self: VectorMemoryStore,
    filters: Mapping[str, str] | None = None,
    *,
    kind: str = "",
    limit: int = memory_schema.DEFAULT_FACET_PAGE,
    offset: int = 0,
) -> list[dict]:
    """One page of live rows matching every named facet, newest first.

    *filters* maps facet names (:data:`memory_schema.FACET_NAMES`) to exact
    values and ANDs them together; *kind* narrows to one row type. An axis the
    mapping omits is unconstrained, while an axis mapped to ``""`` selects the
    rows no writer attributed — the two are different questions, which is why
    this takes a mapping rather than a :class:`memory_schema.MemoryFacets`.

    Raises :class:`memory_schema.FacetsUnsupported` on the v1 lineage and
    :class:`memory_schema.UnknownFacet` for a name outside the closed set.
    Live rows only, like every other reader here; paging is stable because the
    order breaks ``created_at`` ties on ``id``. No ``embedding`` column is read
    or returned: a facet partitions and never scores.
    """
    self._require_facets()
    sql, params = memory_schema.facet_page_query(filters or {}, kind, limit, offset)
    return [dict(row) for row in self._fetch_all_locked(sql, params)]


def count_by_facet(
    self: VectorMemoryStore,
    group_by: str,
    filters: Mapping[str, str] | None = None,
    *,
    kind: str = "",
) -> dict[str, int]:
    """Live-row counts per distinct value of *group_by*, most populous first.

    The "what is actually in this store's memory" question: which crews,
    surfaces, scopes or kinds filled it, and how much each contributed.
    *filters* and *kind* narrow the population first, so a count can be asked
    within a carve (``count_by_facet("surface", {"crew": "finance"})``).

    A ``dict`` keyed by the stored value, mirroring
    :meth:`get_rejection_stats`; ``""`` is a legitimate key and means the rows
    on which that axis was never stamped. Truncated to
    :data:`memory_schema.MAX_FACET_GROUPS` values because ``session_key``
    cardinality is unbounded, and the order makes that the least populous
    tail. Same two refusals as :meth:`list_by_facets`.
    """
    self._require_facets()
    sql, params = memory_schema.facet_count_query(group_by, filters or {}, kind)
    return {str(row["value"]): int(row["total"]) for row in self._fetch_all_locked(sql, params)}


def memory_stats(self: VectorMemoryStore) -> dict:
    """Return counts and sizes for dashboard display."""

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    _HAS_FAISS = vm._HAS_FAISS
    _HAS_NUMPY = vm._HAS_NUMPY

    row = self._fetch_one_locked(
        "SELECT "
        "(SELECT COUNT(*) FROM semantic_memory WHERE is_deleted=0) AS sem_active, "
        "(SELECT COUNT(*) FROM semantic_memory WHERE is_deleted=1) AS sem_deleted, "
        "(SELECT COUNT(*) FROM episodic_memories WHERE is_deleted=0) AS ep_active, "
        "(SELECT COUNT(*) FROM episodic_memories WHERE is_deleted=1) AS ep_deleted, "
        "(SELECT COUNT(*) FROM memory_events) AS events_count, "
        "(SELECT COUNT(*) FROM episodic_memories WHERE is_deleted=0 AND embedding IS NOT NULL) AS ep_with_vec"
    )
    assert row is not None  # a scalar-subquery SELECT always returns one row
    faiss_size = len(self._faiss_id_map) if self._faiss_id_map else 0
    return {
        "semantic_active": row[0],
        "semantic_deleted": row[1],
        "episodic_active": row[2],
        "episodic_deleted": row[3],
        "events_count": row[4],
        "faiss_index_size": faiss_size,
        "embedded_count": row[5],
        # The FAISS index is an optional in-RAM accelerator (needs both
        # faiss and numpy); without it, retrieval falls back to an exact
        # stdlib cosine scan over the same stored embeddings.
        "faiss_available": _HAS_FAISS and _HAS_NUMPY,
    }
