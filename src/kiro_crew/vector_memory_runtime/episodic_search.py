"""Episodic retrieval: every rung of the search ladder and the context block.

``search_episodic`` dispatches: member V2 scans and fuses the whole private
population; V1 tries the FAISS accelerator, then a cosine scan over the stored
vectors (numpy-vectorized from the resident scoring set where it fits), then the
LIKE keyword fallback. The relevance gate reads raw cosine before ranking, MMR
and the ``limit`` cut. Reads go through the store's locked helpers; the one
write is the debounced ``last_accessed_at`` touch, taken under ``_db_lock``.
"""

from __future__ import annotations

import json
import logging
import math
import struct
from dataclasses import dataclass
from datetime import timezone
from typing import TYPE_CHECKING

from kiro_crew import memory_record_metadata as record_meta
from kiro_crew import memory_v2
from kiro_crew.embeddings import PRIORITY_INTERACTIVE
from kiro_crew.vector_memory_runtime import text_scoring as _text_scoring
from kiro_crew.vector_memory_runtime.embedding import _RecallQuery

if TYPE_CHECKING:
    from datetime import datetime

    import numpy as np

    from kiro_crew._sqlite_compat import sqlite3
    from kiro_crew.vector_memory import VectorMemoryStore

# The store's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.vector_memory")

# Minimum raw cosine for admission into injected context. NOT a tuned pair of
# values: measured over the real embedder, the relevant and irrelevant cosine
# distributions OVERLAP, so no threshold separates them, and both branches sit
# looser than the best achievable cut (which admits nothing irrelevant at the cost
# of ~8% of relevant fragments). The long-text relaxation is roughly twice the
# dilution it compensates for. Evidence, and the harness that produced it, in
# docs/system-specs/modules/memory-skills-hooks.md § "The admission gate is a loose
# cut, not a tuned one" — read it before treating either number as calibrated.
# Changing either changes what is admitted on every existing install.
_EPISODIC_RELEVANCE_THRESHOLD = 0.55
_EPISODIC_LONG_TEXT_CHARS = 300  # texts longer than this get a relaxed threshold
_EPISODIC_LONG_TEXT_THRESHOLD = 0.42  # relaxed threshold for long entries


@dataclass(frozen=True)
class _EpisodicScoringSet:
    """The episodic columns a vector search needs to SCORE, held in memory.

    Scoring reads only the embedding (cosine), ``tags`` (the tag filter and the
    per-tag decay rate), ``importance`` and ``created_at`` (the decay), and the
    text LENGTH (the length-aware relevance threshold). None of that changes
    between two searches with no write in between, so it is resolved once and
    reused; the row BODIES (``text``, ``conversation_id``, ``last_accessed_at``)
    are fetched per search for the ranked winners only.

    The arrays are index-aligned with ``ids``. ``numpy`` is optional at import
    time, so the annotations are deferred strings (``from __future__ import
    annotations``); only the tier that builds this runs, and it runs only when
    numpy is present.

    ``generation`` and ``data_version`` are the validity token: the first is
    bumped by every in-process writer that changes the scored population, the
    second is sqlite's own counter, which moves when ANOTHER connection commits.
    Both are needed -- ``data_version`` deliberately does not move for the
    reading connection's own commits.
    """

    dim: int
    ids: list[str]
    matrix: np.ndarray  # (n, dim) float32, C-contiguous, pre-normalized as stored
    tag_sets: list[frozenset[str]]
    decay_rates: np.ndarray  # (n,) float64
    importance: np.ndarray  # (n,) float64
    created_ts: np.ndarray  # (n,) float64, epoch seconds
    text_lens: np.ndarray  # (n,) int64
    generation: int
    data_version: int


#: Characters of one episode's text a rendered line carries. Named because three
#: readers need the same number: ``get_episodic_context``'s block, the ``fit`` walk in
#: ``_recall_once`` that bounds a recall's evidence, and
#: ``decisions/points/memory_recall.py``, which measures a decision's saving against the
#: same clip. A literal in one place and a different literal in another would make the
#: saving a number about text nobody rendered.
EPISODIC_BLOCK_TEXT_CHARS = 1500


def episodic_relevance_threshold(store: VectorMemoryStore, text: str) -> float:
    """Minimum RAW cosine for a memory to be admitted as relevant context.

    Long texts dilute cosine similarity, so the gate relaxes above the
    long-text cutoff.
    """
    if store.algorithm_version == "v2":
        return memory_v2.cosine_floor(text)
    return (
        _EPISODIC_LONG_TEXT_THRESHOLD
        if len(text) > _EPISODIC_LONG_TEXT_CHARS
        else _EPISODIC_RELEVANCE_THRESHOLD
    )


def filter_by_relevance(store: VectorMemoryStore, candidates: list[dict]) -> list[dict]:
    """Drop candidates below the length-aware raw-cosine relevance gate.

    Admission reads the raw ``cosine_sim``, never the decay-adjusted
    ``score``, and runs BEFORE ranking/MMR/truncation so a highly relevant
    but old memory is admitted rather than ordered past ``limit`` by a
    cluster of recent-but-irrelevant rows (which the gate then removes,
    leaving nothing). Rows without a ``cosine_sim`` (keyword fallback) were
    never scored on cosine, so the gate does not apply to them.
    """
    return [
        c
        for c in candidates
        if "cosine_sim" not in c
        or c["cosine_sim"] >= store._episodic_relevance_threshold(c.get("text", ""))
    ]


def search_episodic(
    store: VectorMemoryStore,
    query_embedding: list[float] | None = None,
    query_text: str = "",
    limit: int = 8,
    mmr: bool = True,
    tag_filter: list[str] | None = None,
    relevance_filter: bool = False,
    *,
    recall_query: _RecallQuery | None = None,
) -> list[dict]:
    """Search episodic memories by vector similarity with decay scoring.

    The recency decay rate defaults to ``_DEFAULT_DECAY_RATE`` per day and
    is configurable per tag via ``memory.decay_rates`` (see
    :meth:`_decay_rate_for`).
    When ``mmr=True`` (default), applies Maximal Marginal Relevance
    reranking to balance relevance with diversity.
    When ``tag_filter`` is provided, only entries matching ANY of the
    given tags are returned.
    When ``relevance_filter=True``, candidates below the raw-cosine
    relevance gate are dropped BEFORE ranking, so recency cannot order a
    relevant match out of the result. Defaults to False so dashboard/API/CLI
    callers still receive the full ranked set.
    Falls back to FTS5 text search if no embedding provided.
    """
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    if recall_query is not None:
        # Inference has already finished. Keep the identity check and the
        # local vector/index reads together, never the model wait.
        with store._db_lock:
            store._check_recall_query(recall_query)
            return store.search_episodic(
                recall_query.vector, query_text, limit, mmr, tag_filter, relevance_filter
            )
    if store.algorithm_version == "v2":
        return store._search_episodic_v2(
            query_embedding, query_text, limit, mmr, tag_filter, relevance_filter
        )
    if (
        store._faiss_index is not None
        and store._faiss_data_version is not None
        and store._faiss_data_version != store._sqlite_data_version()
    ):
        store._faiss_index = None
        store._faiss_id_map = []
    if (
        query_embedding is not None
        and vm._HAS_NUMPY
        and vm._HAS_FAISS
        and store._faiss_index is not None
        and store._faiss_index.ntotal > 0  # type: ignore[attr-defined]
    ):
        logger.debug(
            "Episodic FAISS search: vectors=%d limit=%d",
            store._faiss_index.ntotal,  # type: ignore[attr-defined]
            limit,
        )
        vec = vm.np.array(query_embedding, dtype=vm.np.float32)
        norm = vm.np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        # FAISS search + id_map lookups must be serialized against concurrent
        # writers (write_episodic on worker threads): a mid-flight add could
        # otherwise corrupt the C++ index or leave _faiss_id_map shorter than
        # index.ntotal, IndexError-ing the lookup below.
        now = vm.datetime.now(tz=timezone.utc)
        candidates: list[dict] = []
        with store._db_lock:
            # MMR reranks from the FULL candidate pool (see the _mmr_rerank pool
            # comment: truncating toward `limit` silently drops the
            # relevant-but-diverse tail pick that is the whole point of MMR). The
            # sqlite tiers already hand the rerank their entire embedded
            # population, bounded only by _MMR_MAX_POOL inside _mmr_rerank -- so
            # the FAISS tier must match that recall contract when no tag filter
            # narrows recall. Without MMR the result is candidates[:limit].
            # A tag_filter is the exception: _matches_tags screens candidates
            # AFTER this window, so a wide MMR pool would satisfy the
            # expected = min(limit, k) starvation probe below with tagged hits
            # inside the top _MMR_MAX_POOL and never fall through to
            # _sqlite_vector_search, the only tier that masks tags over the
            # complete active population. Keep the narrow max(limit * 2, 16)
            # window for a tag-filtered query so the probe still routes it to
            # the full-population tier; that window is also correct and cheaper
            # whenever native work must stay bounded independently of lifetime
            # tombstones.
            k = (
                min(_text_scoring._MMR_MAX_POOL, store._faiss_index.ntotal)  # type: ignore[attr-defined]
                if mmr and not tag_filter
                else min(max(limit * 2, 16), store._faiss_index.ntotal)  # type: ignore[attr-defined]
            )
            distances, indices = store._faiss_index.search(vec.reshape(1, -1), k)  # type: ignore[attr-defined]
            # FAISS returns ids and distances only. Every hit is resolved in
            # a single IN (...) query over an explicit column list: one
            # "SELECT *" per hit is an N+1 that also drags each row's
            # embedding BLOB back out of the store even though the vectors
            # are already resident in the index.
            hits: list[tuple[str, float]] = []
            for dist, idx in zip(distances[0], indices[0]):
                if idx == -1:
                    break
                hits.append((store._faiss_id_map[int(idx)], float(dist)))
            hit_ids = [mem_id for mem_id, _ in hits]
            blocked = store._ineligible_ids(
                [record_meta.record_id_for("episode", mem_id) for mem_id in hit_ids]
            )
            rows_by_id = store._get_episodic_batch(hit_ids)
            for mem_id, cosine_sim in hits:
                if record_meta.record_id_for("episode", mem_id) in blocked:
                    continue
                # Absent from the mapping == row missing or tombstoned; the
                # per-hit lookup treated both the same way.
                mem = rows_by_id.get(mem_id)
                if mem is None:
                    continue
                if tag_filter and not store._matches_tags(mem, tag_filter):
                    continue
                created = vm.datetime.fromisoformat(mem["created_at"])
                days_old = max(0, (now - created).days)
                decay_rate = store._decay_rate_for(mem.get("tags"))
                score = (
                    cosine_sim * (0.7 + 0.3 * mem["importance"]) * math.exp(-decay_rate * days_old)
                )
                candidates.append(
                    {**mem, "score": round(score, 4), "cosine_sim": round(cosine_sim, 4)}
                )

        if relevance_filter:
            candidates = store._filter_by_relevance(candidates)
        expected = min(limit, k)
        if len(candidates) < expected:
            return store._sqlite_vector_search(
                query_embedding,
                query_text,
                limit,
                mmr=mmr,
                tag_filter=tag_filter,
                relevance_filter=relevance_filter,
            )

        candidates.sort(key=lambda x: x["score"], reverse=True)
        result = _text_scoring._mmr_rerank(candidates, limit=limit) if mmr else candidates[:limit]

        # Update last_accessed_at under the same lock as the rest of the write
        # path. Left unlocked this UPDATE races concurrent writers/readers of the
        # store: transactions interleave (a write can be lost or clobbered) and,
        # with nothing serializing access, sqlite can raise "database is locked".
        # busy_timeout (set at connection init) waits out contention while the
        # lock keeps this metadata write consistent with the FAISS index. RLock
        # is reentrant, so re-acquiring here is safe regardless of caller.
        # _touch_last_accessed does the locking and debouncing.
        store._touch_last_accessed([c["id"] for c in result])
        return result

    # Fallback 1: stdlib cosine search over SQLite embeddings (no FAISS/numpy needed)
    if query_embedding is not None:
        return store._sqlite_vector_search(
            query_embedding,
            query_text,
            limit,
            mmr=mmr,
            tag_filter=tag_filter,
            relevance_filter=relevance_filter,
        )

    # Fallback 2: FTS5 keyword search (no embeddings — MMR not useful here)
    logger.debug("Episodic keyword fallback")
    return (
        store._eligible_rows(
            store._fts5_episodic_search(
                query_text, max(limit, store._episodic_max), tag_filter=tag_filter
            ),
            "episode",
        )[:limit]
        if query_text
        else []
    )


def search_episodic_v2(
    store: VectorMemoryStore,
    query_embedding: list[float] | None,
    query_text: str,
    limit: int,
    mmr: bool,
    tag_filter: list[str] | None,
    relevance_filter: bool,
) -> list[dict]:
    """Fuse both evidence sources before admission and the result budget.

    Scanning the active private population also includes rows awaiting an
    embedding and avoids the FAISS top-k/tag-filter starvation of V1. No
    model calls happen inside this scan or while holding the store lock.
    """
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    if limit <= 0 or (not query_text.strip() and query_embedding is None):
        return []
    rows = store._fetch_all_locked(
        f"SELECT * FROM {store._epi_rel} WHERE is_deleted = 0{store._epi_guard}",
        scan="episodic",
    )
    query_terms = memory_v2.terms(query_text)
    similarity = store._stored_similarity_scorer(query_embedding)
    now = vm.datetime.now(tz=timezone.utc)
    candidates = []
    for raw in store._eligible_rows(rows, "episode"):
        row = dict(raw)
        if tag_filter and not store._matches_tags(row, tag_filter):
            continue
        blob = row.get("embedding")
        has_vector = query_embedding is not None and blob and len(blob) == len(query_embedding) * 4
        cosine = round(similarity(row), 4) if has_vector else None
        evidence = memory_v2.relevance_evidence(query_terms, row["text"], cosine)
        if relevance_filter and not evidence["admitted"]:
            continue
        # Even an unfiltered search needs evidence: an absent query vector
        # must not turn an unrelated lexical search into a table listing.
        if cosine is None and not evidence["matched_terms"]:
            continue
        created = vm.datetime.fromisoformat(row["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        days_old = max(0, (now - created).days)
        score = memory_v2.rank_score(
            evidence,
            importance=row["importance"],
        )
        row.pop("embedding", None)
        row.update(score=score, retrieval={**evidence, "age_days": days_old})
        if cosine is not None:
            row["cosine_sim"] = cosine
        candidates.append(row)
    candidates.sort(key=lambda row: (-row["score"], row["id"]))
    result = _text_scoring._mmr_rerank(candidates, limit=limit) if mmr else candidates[:limit]
    store._touch_last_accessed([row["id"] for row in result])
    return result


def sqlite_vector_search(
    store: VectorMemoryStore,
    query_embedding: list[float],
    query_text: str,
    limit: int,
    mmr: bool = True,
    tag_filter: list[str] | None = None,
    relevance_filter: bool = False,
) -> list[dict]:
    """Cosine similarity search using embeddings stored in SQLite.

    Scoring is vectorized with numpy when available (one mat-vec over all
    surviving rows); falls back to the stdlib-only per-row loop otherwise.
    numpy is an optional accelerator here rather than a declared dependency,
    so both rungs have to stay — the same shape as
    ``_stored_similarity_scorer``.

    With numpy, the scoring columns are held resident between calls
    (:class:`_EpisodicScoringSet`) and only the ranked pool's row bodies are
    read per search. Nothing about the per-row scoring work changes between
    two searches with no write in between, and redoing it dominated the call:
    the population read and the per-row candidate build were together ~94% of
    it, against ~6% for the mat-vec. The per-call read below stays as the
    path for a store too large to hold and for a library with no
    ``data_version`` pragma.

    Every numpy rung dots in float32, matching the stored dtype and the FAISS
    path, so the resident tier and the per-call read cannot hand the same
    query two different cosines — and that number is not only a ranking key,
    it is what ``_filter_by_relevance`` compares against a fixed admission
    threshold.
    """
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    # Normalize query
    norm = math.sqrt(sum(x * x for x in query_embedding))
    q = [x / norm for x in query_embedding] if norm > 0 else query_embedding
    q_len = len(q)

    blocked = store._ineligible_ids()
    if vm._HAS_NUMPY:
        scoring = store._episodic_scoring_set(q_len)
        if scoring is not None:
            logger.debug(
                "Episodic SQLite vector search: rows_with_emb=%d (resident)",
                len(scoring.ids),
            )
            return store._rank_from_scoring_set(
                scoring,
                q,
                limit,
                mmr,
                tag_filter,
                relevance_filter,
                vm.datetime.now(tz=timezone.utc),
                blocked,
            )

    # Serialized via the locked helper — two threads running a statement at
    # the same time corrupt each other's row iteration (surfacing as
    # DatabaseError("another row available") and, on Windows CI, a NULL
    # value for a column the WHERE clause excludes). Only the fetch is
    # locked: the scoring loop below works on materialized rows.
    rows = store._fetch_all_locked(
        "SELECT id, conversation_id, text, tags, importance, created_at, "
        "last_accessed_at, embedding FROM episodic_memories "
        "WHERE is_deleted = 0 AND embedding IS NOT NULL",
        scan="episodic",
    )

    logger.debug(
        "Episodic SQLite vector search: rows_with_emb=%d",
        len(rows),
    )

    rows = store._eligible_rows(rows, "episode")
    now = vm.datetime.now(tz=timezone.utc)
    candidates: list[dict] = []
    if vm._HAS_NUMPY:
        # First pass: apply the skip rules and collect surviving rows and
        # their embedding blobs, preserving order.
        survivors: list = []
        blobs: list[bytes] = []
        for r in rows:
            blob = r["embedding"]
            n_floats = len(blob) // 4
            if n_floats != q_len:
                continue
            if tag_filter and not store._matches_tags(dict(r), tag_filter):
                continue
            survivors.append(r)
            blobs.append(blob)
        if survivors:
            # One mat-vec over every surviving row (both sides are
            # pre-normalized → the dot product IS the cosine similarity).
            # float32 matches the stored dtype and the FAISS path.
            mat = vm.np.frombuffer(b"".join(blobs), dtype=vm.np.float32).reshape(len(blobs), q_len)
            sims: list[float] = [float(s) for s in mat @ vm.np.asarray(q, dtype=vm.np.float32)]
        else:
            sims = []
        for r, cosine_sim in zip(survivors, sims):
            candidates.append(store._episodic_candidate(r, cosine_sim, now))
    else:
        for r in rows:
            blob = r["embedding"]
            n_floats = len(blob) // 4
            if n_floats != q_len:
                continue
            if tag_filter and not store._matches_tags(dict(r), tag_filter):
                continue
            vec = struct.unpack(f"{n_floats}f", blob)
            # dot product (both pre-normalized → cosine similarity)
            cosine_sim = sum(a * b for a, b in zip(q, vec))
            candidates.append(store._episodic_candidate(r, cosine_sim, now))

    if relevance_filter:
        candidates = store._filter_by_relevance(candidates)
    candidates.sort(key=lambda x: x["score"], reverse=True)
    result = _text_scoring._mmr_rerank(candidates, limit=limit) if mmr else candidates[:limit]
    # Same lock discipline as the FAISS path in search_episodic. This UPDATE
    # runs on every context assembly, so several threads reach it at once
    # (parallel subagent spawns), and sqlite's implicit BEGIN is per
    # connection: two unsynchronized writers can both observe autocommit=1
    # and both issue BEGIN, and the loser raises "cannot start a transaction
    # within a transaction". RLock is reentrant, so re-acquiring here is safe
    # regardless of caller. _touch_last_accessed does the locking and debouncing.
    store._touch_last_accessed([c["id"] for c in result])
    return result


def episodic_scoring_set(store: VectorMemoryStore, dim: int) -> _EpisodicScoringSet | None:
    """Return the resident scoring set for *dim*, building it if stale.

    None means "score from a per-call read instead": either the
    cross-process token is unavailable or the population is too large to
    hold. A ``dim`` that does not match the resident set forces a rebuild
    rather than returning nothing, because a width change means the
    embedding space was swapped and the old matrix is meaningless anyway.
    """
    if not store._episodic_scoring_supported:
        return None
    with store._db_lock:
        version = store._sqlite_data_version()
        if version is None:
            store._episodic_scoring_supported = False
            store._episodic_scoring = None
            logger.info(
                "sqlite has no data_version pragma; episodic scoring set disabled "
                "(a second process writing this store could not be detected)"
            )
            return None
        resident = store._episodic_scoring
        if (
            resident is not None
            and resident.dim == dim
            and resident.generation == store._episodic_scoring_generation
            and resident.data_version == version
        ):
            return resident
        if store._episodic_scoring_refused == (dim, store._episodic_scoring_generation, version):
            # This exact state already refused to build (over budget); the
            # per-call read is the settled answer until a write or another
            # process moves one of the tokens.
            return None
        built = store._build_episodic_scoring_set(dim, version)
        if built is None:
            store._episodic_scoring_refused = (dim, store._episodic_scoring_generation, version)
        else:
            store._episodic_scoring_refused = None
        store._episodic_scoring = built
        return built


def build_episodic_scoring_set(
    store: VectorMemoryStore, dim: int, version: int
) -> _EpisodicScoringSet | None:
    """Read the scoring columns for every active embedded row of width *dim*.

    The embedding BLOB is the only wide column read; the row bodies are
    deliberately left for the per-search winner lookup. Returns None when the
    matrix would exceed ``_EPISODIC_SCORING_MAX_BYTES``. The lock re-acquire
    is reentrant, matching ``_fetch_all_locked``'s discipline, so the caller
    already holding it is fine.
    """
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    with store._db_lock:
        rows = store.db.execute(
            "SELECT id, tags, importance, created_at, "
            "COALESCE(LENGTH(text), 0) AS text_len, embedding "
            "FROM episodic_memories WHERE is_deleted = 0 AND embedding IS NOT NULL"
        ).fetchall()
        # This is the population read the resident set exists to pay ONCE per
        # invalidation instead of once per search, so it is credited like the
        # per-call scan it replaces — a store on this tier shows
        # episodic_full_scans rising with writes, not with searches.
        store._reads.record(len(rows), "episodic")

    ids: list[str] = []
    blobs: list[bytes] = []
    tag_sets: list[frozenset[str]] = []
    decay_rates: list[float] = []
    importance: list[float] = []
    created_ts: list[float] = []
    text_lens: list[int] = []
    budget = vm._EPISODIC_SCORING_MAX_BYTES
    for r in rows:
        blob = r["embedding"]
        if len(blob) // 4 != dim:
            continue
        budget -= len(blob)
        if budget < 0:
            logger.info(
                "Episodic scoring set over %d bytes; falling back to a per-call scan",
                vm._EPISODIC_SCORING_MAX_BYTES,
            )
            return None
        raw_tags = r["tags"]
        decoded = json.loads(raw_tags) if isinstance(raw_tags, str) else (raw_tags or [])
        ids.append(r["id"])
        blobs.append(blob)
        tag_sets.append(frozenset(t.lower() for t in decoded if isinstance(t, str)))
        # The decay rate is a pure function of the row's tags and the store's
        # config mapping, which is fixed at construction, so it is resolved
        # once here instead of per row per search.
        decay_rates.append(store._decay_rate_for(raw_tags))
        importance.append(float(r["importance"]))
        # created_at is always an aware ISO string (the search path already
        # subtracts it from an aware `now`, so a naive one raises), which
        # makes .timestamp() exact rather than locale-dependent.
        created_ts.append(vm.datetime.fromisoformat(r["created_at"]).timestamp())
        text_lens.append(int(r["text_len"]))

    matrix = vm.np.frombuffer(b"".join(blobs), dtype=vm.np.float32).reshape(len(blobs), dim)
    return _EpisodicScoringSet(
        dim=dim,
        ids=ids,
        matrix=matrix,
        tag_sets=tag_sets,
        decay_rates=vm.np.asarray(decay_rates, dtype=vm.np.float64),
        importance=vm.np.asarray(importance, dtype=vm.np.float64),
        created_ts=vm.np.asarray(created_ts, dtype=vm.np.float64),
        text_lens=vm.np.asarray(text_lens, dtype=vm.np.int64),
        generation=store._episodic_scoring_generation,
        data_version=version,
    )


def rank_from_scoring_set(
    store: VectorMemoryStore,
    scoring: _EpisodicScoringSet,
    q: list[float],
    limit: int,
    mmr: bool,
    tag_filter: list[str] | None,
    relevance_filter: bool,
    now: datetime,
    blocked: set[str] | None = None,
) -> list[dict]:
    """Score, filter and rank from the resident set; resolve winner bodies.

    The filters run across the FULL population before ``limit``, exactly as
    the per-call path does, which is why ``tags``, ``importance``,
    ``created_at`` and the text length are in the set: a tag matching few
    rows, or a relevance gate admitting few, must still return those rows
    rather than whatever happened to fall inside a top-k window.

    Bodies are then resolved for the ranked pool only. The pool is the
    candidate set the reranker would see, not ``limit``, because MMR reads
    each candidate's TEXT to compute diversity and truncates the pool to
    ``_MMR_MAX_POOL`` itself -- so shrinking it here would change recall.
    """
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    sims = vm.np.asarray(
        scoring.matrix @ vm.np.asarray(q, dtype=vm.np.float32), dtype=vm.np.float64
    )
    # The relevance gate and the emitted candidate both read the ROUNDED
    # cosine, so round once and use that value for both.
    sims_rounded = vm.np.round(sims, 4)

    keep = vm.np.ones(len(scoring.ids), dtype=bool)
    if blocked:
        keep &= vm.np.fromiter(
            (record_meta.record_id_for("episode", mem_id) not in blocked for mem_id in scoring.ids),
            dtype=bool,
            count=len(scoring.ids),
        )
    if tag_filter:
        wanted = {t.lower() for t in tag_filter}
        keep &= vm.np.fromiter(
            (bool(ts & wanted) for ts in scoring.tag_sets),
            dtype=bool,
            count=len(scoring.ids),
        )
    if relevance_filter:
        thresholds = vm.np.where(
            scoring.text_lens > _EPISODIC_LONG_TEXT_CHARS,
            _EPISODIC_LONG_TEXT_THRESHOLD,
            _EPISODIC_RELEVANCE_THRESHOLD,
        )
        keep &= sims_rounded >= thresholds

    surviving = vm.np.flatnonzero(keep)
    if surviving.size == 0:
        return []

    # max(0, timedelta.days): a whole-day floor, and never negative for a row
    # stamped in the future.
    days_old = vm.np.maximum(0.0, vm.np.floor((now.timestamp() - scoring.created_ts) / 86400.0))
    scores = vm.np.round(
        sims * (0.7 + 0.3 * scoring.importance) * vm.np.exp(-scoring.decay_rates * days_old),
        4,
    )

    # Stable descending sort matches list.sort(key=score, reverse=True), which
    # leaves rows of equal score in population order.
    ranked = surviving[vm.np.argsort(-scores[surviving], kind="stable")]
    pool = ranked[: min(ranked.size, _text_scoring._MMR_MAX_POOL if mmr else limit)]

    bodies = store._get_episodic_batch([scoring.ids[int(i)] for i in pool])
    candidates: list[dict] = []
    for i in pool:
        # Absent from the mapping == the row was tombstoned or removed since
        # the set was built; same treatment as the FAISS path's resolve.
        body = bodies.get(scoring.ids[int(i)])
        if body is None:
            continue
        candidates.append({**body, "score": float(scores[i]), "cosine_sim": float(sims_rounded[i])})

    result = _text_scoring._mmr_rerank(candidates, limit=limit) if mmr else candidates[:limit]
    store._touch_last_accessed([c["id"] for c in result])
    return result


def episodic_candidate(
    store: VectorMemoryStore, r: sqlite3.Row, cosine_sim: float, now: datetime
) -> dict:
    """Build one episodic search candidate from a row and its cosine score.

    Shared by both scoring branches of :meth:`_sqlite_vector_search` so the
    candidate shape cannot silently diverge between numpy-installed and
    stdlib-only installs.
    """
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    created = vm.datetime.fromisoformat(r["created_at"])
    days_old = max(0, (now - created).days)
    decay_rate = store._decay_rate_for(r["tags"])
    score = cosine_sim * (0.7 + 0.3 * r["importance"]) * math.exp(-decay_rate * days_old)
    return {
        "id": r["id"],
        "conversation_id": r["conversation_id"],
        "text": r["text"],
        "tags": r["tags"],
        "importance": r["importance"],
        "created_at": r["created_at"],
        "last_accessed_at": r["last_accessed_at"],
        "score": round(score, 4),
        "cosine_sim": round(cosine_sim, 4),
    }


def get_episodic_context(
    store: VectorMemoryStore,
    query_embedding: list[float] | None = None,
    query_text: str = "",
    cap: int = 3000,
) -> str:
    """Format episodic search results for prompt injection.

    Results below the length-aware cosine relevance gate are dropped by
    ``search_episodic(relevance_filter=True)`` BEFORE decay ranking, so a
    relevant-but-old memory is admitted rather than ordered out by recency.
    """
    if query_embedding is None and query_text and store.embed_fn is not None:
        query_embedding = store._try_embed(query_text, PRIORITY_INTERACTIVE)
    results = store.search_episodic(
        query_embedding=query_embedding,
        query_text=query_text,
        limit=store._episodic_limit,
        relevance_filter=True,
    )
    if not results:
        return ""
    lines: list[str] = []
    total = 0
    for i, r in enumerate(results, 1):
        text = r["text"][:EPISODIC_BLOCK_TEXT_CHARS]
        line = f"{i}. {text}"
        if store.algorithm_version == "v2":
            line = f"{i}. [memory:{r['id']}] {text}"
        if total + len(line) > cap:
            if store.algorithm_version == "v2":
                continue
            break
        lines.append(line)
        total += len(line) + 1
    if not lines:
        return ""
    return (
        "[Episodic Memory — relevant past conversation fragments.]\n"
        + "\n".join(lines)
        + "\n[End of episodic memory]\n"
    )


def matches_tags(mem: dict, tag_filter: list[str]) -> bool:
    """Check if an episodic entry matches ANY of the given tags."""
    raw = mem.get("tags", "[]")
    entry_tags = json.loads(raw) if isinstance(raw, str) else (raw or [])
    return bool(set(t.lower() for t in entry_tags) & set(t.lower() for t in tag_filter))


def decay_rate_for(store: VectorMemoryStore, raw_tags: str | list[str] | None) -> float:
    """Resolve the per-day recency decay rate for an episodic row.

    Rates come from the ``memory.decay_rates`` config mapping, keyed by tag
    (case-insensitive, same as :meth:`_matches_tags`); the reserved
    ``default`` key replaces the built-in ``_DEFAULT_DECAY_RATE`` for rows
    matching no configured tag. A row carrying several configured tags uses
    the SLOWEST decay — the smallest rate, i.e. maximum retention — so a
    memory tagged both a long-retention tag (rate 0.0) and a general tag
    (rate 0.03) never ages out because of the broader tag.
    """
    if not store._decay_by_tag:
        return store._decay_default
    entry_tags = json.loads(raw_tags) if isinstance(raw_tags, str) else (raw_tags or [])
    matched = [
        store._decay_by_tag[t.lower()]
        for t in entry_tags
        if isinstance(t, str) and t.lower() in store._decay_by_tag
    ]
    return min(matched) if matched else store._decay_default


def get_episodic_batch(store: VectorMemoryStore, mem_ids: list[str]) -> dict[str, dict]:
    """Fetch several active episodic rows in one query, keyed by id.

    Replaces a per-hit ``SELECT *`` on the FAISS search path. Missing or
    tombstoned ids are simply absent from the returned mapping. Chunked at
    ``_MAX_SQL_PARAMS`` because the sqlite tier resolves a whole MMR pool
    here (up to ``_MMR_MAX_POOL``), which is well past the bound-parameter
    ceiling of a pre-3.32 sqlite; the bounded FAISS window is normally one chunk.
    """
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    if not mem_ids:
        return {}
    out: dict[str, dict] = {}
    for start in range(0, len(mem_ids), vm._MAX_SQL_PARAMS):
        chunk = mem_ids[start : start + vm._MAX_SQL_PARAMS]
        placeholders = ",".join("?" * len(chunk))
        # The FAISS search path calls this while already holding _db_lock;
        # the helper's re-acquire is safe (RLock) and keeps the site covered
        # when reached from any future unlocked caller.
        rows = store._fetch_all_locked(
            f"SELECT {store._EPISODIC_SEARCH_COLUMNS} FROM episodic_memories "
            f"WHERE id IN ({placeholders}) AND is_deleted = 0",
            tuple(chunk),
        )
        out.update({row["id"]: dict(row) for row in rows})
    return out


def touch_last_accessed(store: VectorMemoryStore, mem_ids: list[str]) -> None:
    """Record an access timestamp for episodic rows, debounced per row.

    Every context assembly searches episodic memory, so an unconditional
    UPDATE per hit turns each read into a write transaction (fsync included).
    last_accessed_at only feeds recency reporting, so a row written within
    ``_LAST_ACCESSED_DEBOUNCE_SECS`` is skipped and the rest go out in one
    ``executemany``. Holds ``_db_lock`` for the whole body so the debounce
    bookkeeping cannot interleave with a concurrent searcher's.
    """
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    if store.algorithm_version == "v2" or not mem_ids:
        return
    with store._db_lock:
        now = vm.time.monotonic()
        cutoff = now - store._LAST_ACCESSED_DEBOUNCE_SECS
        due = [
            m for m in dict.fromkeys(mem_ids) if store._last_accessed_touch.get(m, -1e18) < cutoff
        ]
        if not due:
            return
        stamp = vm._now_iso()
        store.db.executemany(
            f"UPDATE {store._epi_rel} SET last_accessed_at = ? WHERE id = ?{store._epi_guard}",
            [(stamp, m) for m in due],
        )
        store.db.commit()
        for m in due:
            store._last_accessed_touch[m] = now
        if len(store._last_accessed_touch) > store._LAST_ACCESSED_CACHE_MAX:
            store._last_accessed_touch = {
                k: v for k, v in store._last_accessed_touch.items() if v >= cutoff
            }


def fts5_episodic_search(
    store: VectorMemoryStore, query: str, limit: int, tag_filter: list[str] | None = None
) -> list[dict]:
    """Simple LIKE-based text + tags search fallback for episodic memories."""
    words = [w for w in query.strip().split()[:5] if _text_scoring._is_selective_keyword(w)]
    if not words:
        return []
    conditions = " OR ".join(["text LIKE ?" for _ in words] + ["tags LIKE ?" for _ in words])
    params: list[str] = [f"%{w}%" for w in words] * 2
    if tag_filter:
        tag_conds = " OR ".join(["tags LIKE ?" for _ in tag_filter])
        conditions = f"({conditions}) AND ({tag_conds})"
        params.extend(f'%"{t.lower()}"%' for t in tag_filter)
    # Serialized for the same reason as the vector fallback above: this runs
    # on the context-assembly path, concurrently with memory writes.
    rows = store._fetch_all_locked(
        f"SELECT id, conversation_id, text, tags, importance, created_at, last_accessed_at "
        f"FROM episodic_memories WHERE is_deleted = 0 AND ({conditions}) "
        f"ORDER BY created_at DESC LIMIT ?",
        (*params, limit),
    )
    return [dict(r) for r in rows]
