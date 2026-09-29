"""Embedding inference for the store, and the provenance an inference carries.

``try_embed`` is the one place the store asks its embedder for a vector: it
lazily rebinds a missing embedder, refuses managed inference whose shared
backend does not match the database's recorded space, and tags every result
with the space token observed before inference, so a publisher can refuse a
vector that crossed a model swap. The scorers compare a query against stored
vectors without re-embedding rows. Nothing here writes to the database.
"""

from __future__ import annotations

import logging
import math
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from kiro_crew.embeddings import PRIORITY_BULK, PRIORITY_NORMAL

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

# The store's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.vector_memory")


class _EmbeddingVector(list[float]):
    """An inference result bound to the database space observed before inference."""

    def __init__(
        self, values: list[float], token: tuple[str | None, str], *, managed: bool = False
    ):
        super().__init__(values)
        self.space_token = token
        self.managed = managed


@dataclass(frozen=True)
class _RecallQuery:
    """One inference result, including failure, scoped to a single recall."""

    vector: list[float] | None
    generation: int | None
    signature: str | None


class _RecallSpaceChanged(Exception):
    """Discard a partial recall rather than mix embedding spaces."""


def try_embed(
    store: VectorMemoryStore, text: str, priority: int = PRIORITY_NORMAL
) -> list[float] | None:
    """Embed text using embed_fn if available.

    If embed_fn is None but embed_fn_factory is set, attempt to lazily
    rebind embed_fn (rate-limited via cooldown). This recovers from the
    case where the embedding model was unavailable at gateway boot — without it, the
    gateway would silently write all subsequent memories without embeddings
    until the next restart.

    Concurrency: this is a SYNCHRONOUS method. The factory call and probe
    perform blocking model inference (or a model load on first call),
    so this method MUST be invoked from a sync context (worker thread, sync
    handler, etc.). Callers reaching this from an async event loop should
    wrap the call in `asyncio.to_thread()` to avoid stalling the loop. Async
    callers (history consolidation, dashboard memory handlers) MUST offload
    via `asyncio.to_thread()`; the sync paths (add_memory, inject, recall)
    call directly. The rebind block is serialized by `_embed_fn_rebind_lock`
    so concurrent writers share at most one factory call + probe per cooldown
    window.
    """
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    if store.embed_fn is None and store.embed_fn_factory is not None:
        # Hold the rebind lock for the cooldown check + factory call + probe so
        # the "once per cooldown window" invariant holds under multi-threaded
        # write load (TOCTOU on _embed_fn_last_rebind_attempt without this).
        with store._embed_fn_rebind_lock:
            # Re-check under the lock: another thread may have just bound embed_fn.
            if store.embed_fn is None:
                now = vm.time.monotonic()
                if (
                    now - store._embed_fn_last_rebind_attempt
                    >= store._embed_fn_rebind_cooldown_secs
                ):
                    store._embed_fn_last_rebind_attempt = now
                    try:
                        candidate = store.embed_fn_factory()
                    except Exception:
                        logger.debug("embed_fn_factory raised", exc_info=True)
                        candidate = None
                    if candidate is not None:
                        # Verify the candidate actually works before binding — a non-None
                        # callable that always returns None is no better than no factory.
                        # Use explicit `is not None and len() > 0` rather than `if probe:` so
                        # that a hypothetical zero-dim or empty-list probe response is treated
                        # as a misconfiguration (don't bind), not as success.
                        try:
                            probe = candidate("_kirocrew_embed_probe")
                        except Exception:
                            probe = None
                        if probe is not None and len(probe) > 0:
                            store.embed_fn = candidate
                            logger.info(
                                "Lazily rebound embed_fn (probe dim=%d); embeddings re-enabled",
                                len(probe),
                            )
    if store.embed_fn is not None:
        from kiro_crew import embeddings

        # Check after lazy rebinding too. A callable does not establish that
        # this store has handled the current model's durable rebuild request.
        if store.embed_fn is embeddings.make_sync_embed_fn():
            if (
                store.algorithm_version != "v2"
                and store.recorded_embedding_space() is None
                and not store.has_stored_embeddings()
            ):
                embeddings.reconcile_store_embedding_space(store)
            if embeddings.store_embedding_space_is_stale(store):
                return None
        try:
            generation_before = store._space_generation
            managed = store.embed_fn is embeddings.make_sync_embed_fn()
            producer = embeddings.get_shared_embedder() if managed else None
            persistent_token = store._embedding_token() if store._db is not None else (None, "")
            if producer is not None and (
                persistent_token[0]
                != embeddings.embedding_space_signature(producer.model_id, producer.dim)
                or (
                    embeddings.embedding_rebuild_generation()
                    and persistent_token[1] != embeddings.embedding_rebuild_generation()
                )
            ):
                return None
            # A managed call pins its producer through cache/coalescing too;
            # a later singleton replacement cannot relabel its result.
            if producer is not None:
                result = embeddings._shared_sync_embed(text, priority=priority, backend=producer)
            elif getattr(store.embed_fn, "accepts_priority", False):
                result = store.embed_fn(text, priority=priority)  # type: ignore[call-arg]
            else:
                result = store.embed_fn(text)
            if store._space_generation != generation_before:
                # A model swap landed while this text was in flight. The
                # vector belongs to the previous space; committing it would
                # leave a stale-space row that reconcile already passed over
                # and backfill will never revisit. Drop it -- the caller
                # stores NULL and the backfill re-embeds it in the new space.
                logger.debug("Discarding an embedding produced across a space change")
                return None
            # Log only the size of the text: memory content is user data and
            # must not reach the log, even truncated.
            if result:
                logger.debug("Embedded for migration: dim=%d text_len=%d", len(result), len(text))
            else:
                logger.debug("Embed returned None for text_len=%d", len(text))
            return (
                _EmbeddingVector(result, persistent_token, managed=managed)
                if result is not None
                else None
            )
        except Exception:
            logger.debug("Embed failed for text_len=%d", len(text), exc_info=True)
            return None
    return None


def embed_bulk_row(store: VectorMemoryStore, text: str, *, pace: bool) -> "list[float] | None":
    """Embed one row of a corpus sweep, then optionally pace the loop.

    The backfill sweeps (``embedding_repair``) are the longest-running CPU work the gateway does
    unattended — a migrated memory of a few thousand rows is tens of minutes
    of continuous inference — and to a user that is indistinguishable from a
    runaway process. ``memory.embedding_bulk_duty`` spreads the same total
    work over more wall time by idling between rows (see
    :func:`kiro_crew.embeddings.bulk_pace_delay`).

    The sleep is HERE, on the sweep's own thread, and holds neither the DB
    lock nor the model: an interactive embed arriving mid-pause is served
    immediately. It also deliberately covers a row that failed to embed —
    the delay is derived from measured elapsed time, so a no-op returns 0.0
    and only real work is paced.

    The pause falls between this row's inference and its write, which is what
    makes it safe to interrupt: a sweep killed mid-pause leaves the row's
    ``embedding`` NULL and the next sweep re-embeds it, exactly as it already
    does for every row it never reached.

    *pace* is False for a sweep a human explicitly asked for and is watching
    a progress bar on; slowing that down would be paying the cost with none
    of the benefit, since the load is expected in that case.

    *pace* therefore also selects the scheduling class, because attendance —
    not corpus size — is what both dials are really keyed on. An unattended
    sweep embeds at ``PRIORITY_BULK``, which is what gives it the reduced
    ``memory.embedding_bulk_threads`` pool; an attended one embeds at
    ``PRIORITY_NORMAL`` and so keeps the full interactive pool. Without this,
    ``pace=False`` would switch off the idling but leave the sweep on one
    thread, making the very path this PR declares "full speed" ~3x slower
    than before pacing existed.
    """
    from kiro_crew import vector_memory  # circular import: read the pacing seams at call time

    priority = PRIORITY_BULK if pace else PRIORITY_NORMAL
    if not pace:
        return store._try_embed(text, priority)
    started = vector_memory.time.monotonic()
    vec = store._try_embed(text, priority)
    delay = vector_memory.bulk_pace_delay(vector_memory.time.monotonic() - started)
    if delay > 0:
        vector_memory.time.sleep(delay)
    return vec


def stored_similarity_scorer(
    query_emb: list[float] | None,
) -> Callable[[dict], float]:
    """Build a cosine scorer for one query, with query-side work done once.

    The query vector and its norm are the same for every row, so deriving
    them per row repeats a full pass over the query once per lesson. Hoisting
    them out of the loop is where nearly all of the saving is — vectorizing
    the dot product while still converting the query inside the loop keeps
    most of the original cost. ``_sqlite_vector_search`` already normalizes
    its query once for the same reason; this is the lesson-path equivalent.

    Stored lesson vectors are un-normalized by contract (see
    ``backfill_lesson_embeddings``), so the row norm stays inside the loop
    and both norms are divided out. A bare inner product would be correct
    only while the embedding model happens to emit unit vectors, which
    nothing enforces.

    A row whose vector has a different dimensionality is incomparable and
    scores 0.0 rather than being truncated against the query, matching
    ``_sqlite_vector_search`` and ``HybridRetriever._cosine_similarity``.

    The raw (possibly negative) cosine value is returned uncapped — a
    ranking caller that never distinguishes "no vector" (0.0) from
    "opposite direction" (negative) should clamp at its own call site
    (``max(0.0, ...)``); a threshold caller comparing the value against a
    band that may include non-positive bounds needs the true value. The
    numpy path promotes both operands to float64 before the norm and the
    dot product: the stored blob is float32 on disk, and accumulating a
    many-dimensional norm/dot in float32 lands ~1e-7 away from the plain
    ``_cosine_sim`` this scorer replaces — irrelevant when only sorting,
    not irrelevant when the value is compared against a fixed threshold
    like the semantic-dedup line.
    """
    from kiro_crew import vector_memory  # circular import: the optional numpy seam lives there

    if not query_emb:
        return lambda row: 0.0
    q_len = len(query_emb)
    q_bytes = q_len * 4

    if vector_memory._HAS_NUMPY:
        np = vector_memory.np
        q_vec = np.asarray(query_emb, dtype=np.float64)
        q_norm = float(np.linalg.norm(q_vec))
        if not q_norm:
            return lambda row: 0.0

        def numpy_scorer(row: dict) -> float:
            blob = row.get("embedding")
            if not isinstance(blob, bytes) or len(blob) != q_bytes:
                return 0.0
            vec = np.frombuffer(blob, dtype=np.float32).astype(np.float64)
            denom = float(np.linalg.norm(vec)) * q_norm
            return float(vec @ q_vec) / denom if denom else 0.0

        return numpy_scorer

    q_norm_py = math.sqrt(sum(x * x for x in query_emb))
    if not q_norm_py:
        return lambda row: 0.0

    def stdlib_scorer(row: dict) -> float:
        blob = row.get("embedding")
        if not isinstance(blob, bytes) or len(blob) != q_bytes:
            return 0.0
        vec = struct.unpack(f"{q_len}f", blob)
        denom = math.sqrt(sum(y * y for y in vec)) * q_norm_py
        if not denom:
            return 0.0
        return sum(x * y for x, y in zip(query_emb, vec)) / denom

    return stdlib_scorer
