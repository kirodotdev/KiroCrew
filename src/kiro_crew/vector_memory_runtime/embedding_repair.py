"""Embedding-space repair: the recorded vector space and the NULL-vector sweeps.

``memory_meta`` records which space the stored vectors were produced in and which
explicit rebuild request they satisfy. Reconciling against a different space
fences in-flight inference (``begin_space_change``), clears every stored vector to
NULL and drops the FAISS index; the backfill sweeps then re-embed NULL rows -- lesson,
semantic and episodic -- one derived transaction per row, and each rechecks the
space generation under the store lock before it publishes.
"""

from __future__ import annotations

import json
import logging
import struct
from typing import TYPE_CHECKING, Callable, cast

from kiro_crew.vector_memory_runtime import lessons as _lessons

if TYPE_CHECKING:
    import faiss

    from kiro_crew._sqlite_compat import sqlite3
    from kiro_crew.vector_memory import VectorMemoryStore

# The store's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.vector_memory")

# memory_meta key holding the embedding_space_signature() the stored vectors
# were produced under. Absent means "unknown" — see reconcile_embedding_space.
_EMBED_SIG_KEY = "embedding_space_sig"


def recorded_rebuild_generation(store: VectorMemoryStore) -> str:
    """Explicit rebuild request whose old vectors were durably invalidated."""
    return store._read_meta("embedding_rebuild_generation") or ""


def embedding_repair_state(store: VectorMemoryStore, generation: str) -> tuple[bool, int]:
    """Snapshot invalidation acknowledgment and remaining live NULL vectors."""
    with store._db_lock:
        pending = bool(generation and store.recorded_rebuild_generation() != generation)
        remaining = sum(
            store.db.execute(
                f"SELECT COUNT(*) FROM {relation} WHERE is_deleted = 0 AND embedding IS NULL"
            ).fetchone()[0]
            for relation in ("semantic_memory", "episodic_memories")
        )
        return pending, remaining


def begin_space_change(store: VectorMemoryStore) -> None:
    """Mark the start of a vector-space change (a live model swap).

    Call this the moment the outgoing model stops being authoritative, BEFORE
    the new one is ready. Everything already inside :meth:`_try_embed` at that
    instant produced its vector in the old space, and the guard there drops
    those results rather than letting them commit behind the reconcile.

    Distinct from :meth:`set_embedding_dim`, which only fires when the WIDTH
    changes: two different models of the same width are different spaces and
    would otherwise slip through unnoticed.
    """
    with store._db_lock:
        store._space_generation += 1


def set_embedding_dim(store: VectorMemoryStore, dim: int) -> bool:
    """Retarget the store at a new vector width. Returns True if it changed.

    ``_embedding_dim`` is otherwise fixed at construction, yet it gates BOTH
    the FAISS index width (:meth:`build_faiss_index`) and the per-row shape
    check in :meth:`backfill_missing_embeddings`. Swapping to a model of a
    different dimensionality without updating it means every re-embedded
    vector fails validation and stays NULL forever, with the index stuck at
    the old width — so a live model change must call this.

    Callers must reconcile (which NULLs every stored vector) before or right
    after this: mixing widths in one index is exactly what the signature
    machinery exists to prevent. The in-memory index is dropped here so it
    cannot be reused at the old width.
    """
    if dim <= 0 or dim == store._embedding_dim:
        return False
    with store._db_lock:
        logger.info("Embedding width changed %d -> %d", store._embedding_dim, dim)
        store._embedding_dim = dim
        store._faiss_index = None
        store._faiss_id_map = []
    return True


def recorded_embedding_space(store: VectorMemoryStore) -> str | None:
    """Signature the stored vectors were produced under, or None if unrecorded.

    Read-only companion to :meth:`reconcile_embedding_space`, for callers that
    must detect a stale vector space WITHOUT mutating — a one-shot CLI can
    then degrade itself to keyword search instead of clearing vectors it has
    no way to re-embed. ``None`` means the store predates space tracking, so
    its vectors came from the bundled model.
    """
    return store._read_meta(_EMBED_SIG_KEY)


def has_stored_embeddings(store: VectorMemoryStore) -> bool:
    """Whether any persisted vector needs an existing space attribution."""
    queries = (
        "SELECT 1 FROM episodic_memories WHERE embedding IS NOT NULL LIMIT 1",
        "SELECT 1 FROM semantic_memory WHERE embedding IS NOT NULL LIMIT 1",
    )
    return any(store._fetch_one_locked(query) is not None for query in queries)


def reconcile_embedding_space(
    store: VectorMemoryStore,
    signature: str,
    *,
    clear_when_unknown: bool = False,
    force: bool = False,
    rebuild_generation: str = "",
) -> int:
    """Serialize the request check and invalidation across SQLite connections."""
    with store._db_lock:
        try:
            store.db.execute("BEGIN IMMEDIATE")
            result = store._reconcile_embedding_space_locked(
                signature,
                clear_when_unknown=clear_when_unknown,
                force=force,
                rebuild_generation=rebuild_generation,
            )
            store.db.commit()
            return result
        except Exception:
            store.db.rollback()
            raise


def reconcile_embedding_space_locked(
    store: VectorMemoryStore,
    signature: str,
    *,
    clear_when_unknown: bool = False,
    force: bool = False,
    rebuild_generation: str = "",
) -> int:
    """Discard embeddings produced by a DIFFERENT model. Returns rows invalidated.

    Stored vectors are only comparable to each other when they came from the
    same model at the same dimensionality. Without a record of which model
    produced them, swapping the embedding model corrupts search
    silently: with a different dim the old rows are quietly dropped from the
    index, and with the SAME dim (any other 1024-d model) stale vectors are
    cosine-scored against new-model queries and return meaningless
    similarities.

    This records the active vector space in ``memory_meta`` and, when it
    changes, clears every stored embedding to NULL and drops the FAISS index.
    That deliberately reuses the existing NULL-embedding machinery instead of
    adding a parallel one: :meth:`backfill_missing_embeddings` already
    re-embeds NULL episodic rows in batches and now repairs NULL lesson rows
    alongside them, ``build_faiss_index`` and ``_sqlite_vector_search``
    already skip NULL rows, and FTS keyword search is
    unaffected — so search stays correct (just keyword-only for the affected
    rows) while the re-embed proceeds, and an interrupted run is simply
    resumed by the next sweep.

    The first call on a pre-existing database has no recorded space to compare
    against, and what to do then depends on whether the caller can ATTRIBUTE
    those vectors:

    - ``clear_when_unknown=False`` (default) — the active space is the one
      that produced them (the bundled model), so stamp the signature and
      change nothing. A plain upgrade must not force every user to re-embed
      their whole memory.
    - ``clear_when_unknown=True`` — the caller knows the active space did NOT
      produce them, so they are foreign and get cleared. Callers decide this
      by comparing the active signature against the bundled model's
      (``embeddings.default_embedding_space_signature``), which is provable:
      un-versioned vectors predate custom-model support, so the bundled model
      is the only thing that could have written them. Deciding it that way
      rather than by "is a custom model configured?" covers a model selected
      by config, by env var, or by a programmatic
      ``register_embedding_backend`` alike. Without this the common upgrade
      order — stop, update, point ``embed_model_path`` at a model, start —
      would stamp the NEW signature onto bundled-model vectors and they would
      never be re-embedded.

    A signature that already matches is a no-op unless ``force=True``.
    Explicit model apply uses force to rebuild inherited legacy vectors
    whose old metadata cannot prove which weights produced them.
    """
    with store._db_lock:
        stored = store._read_meta(_EMBED_SIG_KEY)
        pending = bool(
            rebuild_generation and store.recorded_rebuild_generation() != rebuild_generation
        )
        force = force or pending
        if stored == signature and not force:
            return 0
        if stored is None and not clear_when_unknown and not force:
            store._write_meta_in_transaction(_EMBED_SIG_KEY, signature)
            logger.info("Recorded embedding vector space %s for existing memory", signature)
            return 0
        # Even equal signatures may represent an explicit rebuild. Fence
        # in-flight reads/writes before changing any derived state.
        if stored is not None or force or store.has_stored_embeddings():
            store.begin_space_change()
        store._faiss_index = None
        store._faiss_id_map = []
        store._invalidate_episodic_scoring()
        store._invalidate_semantic_scoring()
        try:
            episodic = store.db.execute(
                f"UPDATE {store._epi_rel} SET embedding = NULL WHERE embedding IS NOT NULL"
                f"{store._epi_guard}"
            ).rowcount
            semantic = store.db.execute(
                f"UPDATE {store._sem_rel} SET embedding = NULL WHERE embedding IS NOT NULL"
                f"{store._sem_guard}"
            ).rowcount
            stale_removal_failed = False
            for stale in (store._faiss_path, store._faiss_path.with_suffix(".ids.json")):
                try:
                    stale.unlink(missing_ok=True)
                except OSError:
                    stale_removal_failed = True
                    logger.warning("Could not remove stale FAISS file %s", stale, exc_info=True)
            if not stale_removal_failed:
                store._write_meta_in_transaction(_EMBED_SIG_KEY, signature)
                if rebuild_generation:
                    store._write_meta_in_transaction(
                        "embedding_rebuild_generation", rebuild_generation
                    )
            # The outer reconciliation owns the only commit. Failed index
            # removal leaves the request unacknowledged and vectors NULL.
        except Exception:
            store.db.rollback()
            raise
    invalidated = max(0, episodic) + max(0, semantic)
    if stale_removal_failed:
        # Deliberately do NOT stamp the signature. Stamping would mark the
        # reconciliation done while a stale index survives on disk, making
        # the corruption permanent. Leaving the old signature makes the next
        # boot retry — the embeddings are already NULL, so the retry is a
        # cheap no-op UPDATE plus another unlink attempt.
        logger.error(
            "Embedding vector space NOT reconciled: stale FAISS files could not be "
            "removed. Stored embeddings were cleared, but the signature is left "
            "unchanged so the next start retries. Semantic search may be degraded "
            "until then; delete %s and its .ids.json sidecar to resolve now.",
            store._faiss_path,
        )
        return invalidated
    if invalidated:
        logger.warning(
            "Embedding model changed (vector space %s -> %s) — invalidated %d stored "
            "embeddings (%d episodic, %d semantic). They are keyword-searchable now and "
            "are re-embedded in the background.",
            stored or "unrecorded",
            signature,
            invalidated,
            episodic,
            semantic,
        )
    else:
        logger.info("Recorded embedding vector space %s (no stored vectors)", signature)
    return invalidated


def has_pending_embeddings(store: VectorMemoryStore) -> bool:
    """True when any row is waiting for a vector. Never loads the model.

    The existence probe for :meth:`backfill_missing_embeddings`: it answers
    "would that sweep have anything to do?" without touching the embedder, so
    a caller can skip a ~700MB model load on a boot with nothing to embed.
    Three ``SELECT 1 ... LIMIT 1`` reads over the SAME predicates the sweep's
    three sub-sweeps use — episodic, ``lesson.*`` semantic, and non-lesson
    semantic — so a row this returns False for is a row that sweep would not
    have embedded either.

    Deliberately independent of ``embed_fn``: the question is whether WORK
    exists, not whether this store is currently able to do it. The sweep
    keeps its own ``embed_fn is None`` guard, and a caller that is about to
    bind ``embed_fn`` needs the answer before it does so.

    The numpy gate on the episodic loop is likewise not mirrored here. numpy
    is a declared runtime dependency, so its absence is a broken install
    rather than a state to optimise for, and erring toward True there only
    costs what every boot pays today.
    """
    probes = (
        "SELECT 1 FROM episodic_memories WHERE is_deleted = 0 AND embedding IS NULL LIMIT 1",
        "SELECT 1 FROM semantic_memory WHERE is_deleted = 0 AND embedding IS NULL "
        "AND key LIKE 'lesson.%' LIMIT 1",
        "SELECT 1 FROM semantic_memory WHERE is_deleted = 0 AND embedding IS NULL "
        "AND key NOT LIKE 'lesson.%' LIMIT 1",
    )
    return any(store._fetch_one_locked(sql) is not None for sql in probes)


def backfill_rows(
    store: VectorMemoryStore, sql: str, *, kind: str, identity: str, limit: int | None
) -> list[sqlite3.Row]:
    """Page bounded repair fairly, including past rows whose inference failed."""
    if limit is None:
        return store._fetch_all_locked(sql)
    with store._db_lock:
        cursors: dict[str, str] | None = getattr(store, "_backfill_cursors", None)
        if cursors is None:
            cursors = {}
            store._backfill_cursors = cursors
        cursor = cursors.get(kind, "")
        query = sql + f" AND {identity} > ? ORDER BY {identity} LIMIT ?"
        rows = store._fetch_all_locked(query, (cursor, max(0, limit)))
        if not rows and cursor:
            rows = store._fetch_all_locked(query, ("", max(0, limit)))
        cursors[kind] = rows[-1][identity] if rows else ""
        return rows


def backfill_missing_embeddings(
    store: VectorMemoryStore,
    progress: "Callable[[int, int], None] | None" = None,
    *,
    pace: bool = True,
    max_rows_per_kind: int | None = None,
    should_stop: "Callable[[], bool] | None" = None,
) -> int:
    """Compute missing episodic embeddings and extend the resident index.

    Entries written while the embedding model was still downloading (first
    boot, or a migration that ran before the model landed) are stored with a
    NULL ``embedding`` and are keyword-searchable only. So are rows written
    with ``write_episodic(defer_embedding=True)`` by a bulk writer such as
    the onboarding importer. Once the model is present and ``embed_fn`` is
    bound, this sweep embeds those rows and adds them to the resident vector
    index so they become semantically searchable.

    Rows cleared by :meth:`reconcile_embedding_space` after an embedding-model
    change arrive here the same way, so a model swap re-embeds through this
    one path rather than a parallel one. Lesson vectors cleared by the same
    call are repaired here too via :meth:`_backfill_lesson_embeddings`, and
    non-lesson semantic rows via :meth:`_backfill_semantic_kv_embeddings`
    (covers rows written before write-time embedding existed, rows written
    while the model was absent, and ``set_semantic_if_absent`` imports,
    which defer embedding to this sweep by design); the returned count stays
    EPISODIC-only, which is what callers report.

    Idempotent and cheap in steady state: a no-op (returns 0) when there is
    no ``embed_fn``, numpy is missing, or no NULL-embedding rows remain.
    Synchronous + blocking (runs model inference) — call from a worker thread
    / executor, never directly on the event loop.

    FAISS is NOT required. It is an optional accelerator and not a declared
    dependency, so gating on it made this sweep a silent no-op on a stock
    install — every deferred row stayed NULL forever. ``search_episodic``
    already falls back to ``_sqlite_vector_search`` (a stdlib cosine scan
    over these blobs), so the stored vectors are useful either way; the
    resident index extension below is simply skipped when faiss is absent.

    *pace* (default on) idles between rows so the sweep targets
    ``memory.embedding_bulk_duty`` of wall time — the same total CPU work
    spread thinner, which is what keeps an unattended post-migration sweep
    from pinning several cores for tens of minutes. It is a target rather
    than a ceiling: a single row whose inference is slow enough to ask for
    more than :data:`~kiro_crew.embeddings._MAX_BULK_PACE_SLEEP` of idle is
    capped there, so that row runs at a higher effective duty. Pass
    ``pace=False`` for a sweep a human explicitly asked for and is waiting
    on. Gateway maintenance can bound each kind with ``max_rows_per_kind``;
    successive visits page past failed rows and wrap for retries. A supplied
    ``should_stop`` fences commits after shutdown. Defaults retain the full
    sweep for existing callers.
    """
    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    if store.embed_fn is None:
        return 0
    # Repair lesson vectors FIRST: they need no numpy (struct-packed and
    # compared directly, never indexed), and they must be rebuilt even when
    # there is not a single NULL episodic row — which is exactly the state
    # after reconcile_embedding_space() on a memory that holds only lessons.
    store._backfill_lesson_embeddings(
        progress, pace=pace, max_rows=max_rows_per_kind, should_stop=should_stop
    )
    # Same for non-lesson semantic KV rows: struct-packed, no numpy, no
    # FAISS — get_semantic_context ranks them straight from the stored blob.
    # No progress callback: the (done,total) stream belongs to the episodic
    # loop below, and a second denominator would make the dashboard bar
    # jump backward when both row types need re-embedding.
    store._backfill_semantic_kv_embeddings(
        pace=pace, max_rows=max_rows_per_kind, should_stop=should_stop
    )
    if not vm._HAS_NUMPY:
        return 0
    rows = store._backfill_rows(
        "SELECT id, text FROM episodic_memories WHERE is_deleted = 0 AND embedding IS NULL",
        kind="episode",
        identity="id",
        limit=max_rows_per_kind,
    )
    if not rows:
        return 0
    embedded = 0
    total = len(rows)
    if progress is not None:
        # Report the denominator up front: without it an indicator can only
        # spin, and this loop can run for minutes on a large corpus.
        progress(0, total)
    for row in rows:
        # Sampled BEFORE the embed, re-checked under the lock, matching
        # _backfill_semantic_kv_embeddings: a model swap landing across the
        # embed must not commit a vector from the old space (reconcile has
        # already swept past this row, so nothing would ever clear it). The
        # window existed before pacing but was sub-second; idling between
        # rows widens it to seconds, which makes the guard load-bearing.
        if should_stop is not None and should_stop():
            break
        backfill_generation = store._space_generation
        vec = store._embed_bulk_row(row["text"], pace=pace)
        if should_stop is not None and should_stop():
            break
        if not vec:
            if progress is not None:
                progress(embedded, total)
            continue
        arr = vm.np.asarray(vec, dtype=vm.np.float32)
        # Validate dimension before storing: a wrong-dim vector is skipped by
        # build_faiss_index() but would be written non-NULL, so a later sweep
        # would never retry it. Leave it NULL instead so it stays a candidate.
        if arr.shape != (store._embedding_dim,):
            logger.warning(
                "Backfill embed dim mismatch for %s (got %s, expected %d) — leaving NULL",
                row["id"],
                arr.shape,
                store._embedding_dim,
            )
            if progress is not None:
                progress(embedded, total)
            continue
        # L2-normalize to match write_episodic(): the FAISS IndexFlatIP scores
        # inner product, which only equals cosine similarity on unit vectors.
        norm = float(vm.np.linalg.norm(arr))
        if norm > 0:
            arr = arr / norm
        blob = arr.tobytes()
        with store._vector_commit(vec) as current:
            if should_stop is not None and should_stop():
                break
            if not current or backfill_generation != store._space_generation:
                logger.debug("Dropping an episodic backfill from a previous space")
                if progress is not None:
                    progress(embedded, total)
                continue
            # Owner editing can replace an episode body in place. Match
            # that body as well as identity, liveness and the NULL vector.
            updated = store.db.execute(
                f"UPDATE {store._epi_rel} SET embedding = ? "
                f"WHERE id = ? AND text = ? AND embedding IS NULL AND is_deleted = 0{store._epi_guard}",
                (blob, row["id"], row["text"]),
            ).rowcount
            # A newly embedded row is a row neither resident scoring tier has
            # seen. A winner-body lookup can drop vanished ids but cannot
            # surface new ones, so update both derived populations here.
            if updated:
                store._invalidate_episodic_scoring()
                if vm._HAS_FAISS and store._faiss_index is not None:
                    store._faiss_id_map.append(row["id"])
                    try:
                        cast("faiss.Index", store._faiss_index).add(arr.reshape(1, -1))
                    except Exception:
                        # SQLite already owns the vector. Disable the
                        # accelerator rather than leave its id map desynced;
                        # the complete SQLite tier remains available.
                        store._faiss_index = None
                        store._faiss_id_map = []
                        store._faiss_data_version = None
                        logger.warning(
                            "FAISS rejected an episodic backfill; using SQLite search",
                            exc_info=True,
                        )
                    else:
                        # Keep the resident accelerator current without the
                        # O(total rows) rebuild and index-file rewrite required
                        # after each bounded 16-row maintenance page.
                        store._faiss_writes_since_save += 1
        embedded += int(bool(updated))
        if progress is not None:
            progress(embedded, total)
    if embedded and not (should_stop is not None and should_stop()):
        logger.info("Backfilled embeddings for %d episodic entries", embedded)
    return embedded


def backfill_lesson_embeddings(
    store: VectorMemoryStore,
    progress: "Callable[[int, int], None] | None" = None,
    *,
    pace: bool = True,
    max_rows: int | None = None,
    should_stop: "Callable[[], bool] | None" = None,
) -> int:
    """Embed lesson rows whose vector is NULL. Returns the count embedded.

    Lesson vectors drive semantic dedup and contradiction detection
    (:meth:`write_lesson`, :meth:`find_contradiction_candidates`). They are
    otherwise only refilled lazily inside ``write_lesson``, capped at
    ``_MAX_BACKFILLS_PER_CALL`` per call — fine for the handful of legacy rows
    that cap was written for, but not for a wholesale invalidation: after
    :meth:`reconcile_embedding_space` clears every lesson vector on a model
    change, lesson writes are rare enough that recovery could take
    arbitrarily long, and until then dedup silently degrades and can accept a
    duplicate or contradictory lesson.

    Scoped to ``lesson.*`` keys because lessons embed different TEXT than
    the other semantic rows (the raw rule text, matching write_lesson);
    non-lesson rows are swept by :meth:`_backfill_semantic_kv_embeddings`.
    Failures leave the row NULL so a later sweep retries it, matching the
    episodic sweep's contract. No FAISS involvement: lesson vectors are
    compared directly, never indexed.
    """
    if store.embed_fn is None:
        return 0
    rows = store._backfill_rows(
        "SELECT key, value_json FROM semantic_memory "
        "WHERE is_deleted = 0 AND embedding IS NULL AND key LIKE 'lesson.%'",
        kind="directive",
        identity="key",
        limit=max_rows,
    )
    if not rows:
        return 0
    embedded = 0
    total = len(rows)
    if progress is not None:
        progress(0, total)
    for row in rows:
        try:
            # Canonical embedding input: the mapping's rule field (matching
            # write_lesson, which embeds the bare rule), the stored text for
            # a legacy string row. Embedding a mapping row's str() would
            # vectorize its Python repr.
            text = _lessons._lesson_embed_text(json.loads(row["value_json"]))
        except (ValueError, TypeError):
            logger.debug("Skipping lesson %s with unparseable value", row["key"])
            continue
        if not text:
            logger.debug("Skipping lesson %s with no renderable text", row["key"])
            continue
        # Same guard as the episodic and semantic-KV sweeps: sampled before
        # the embed, re-checked under the lock, so a model swap landing
        # across the (now paced) embed cannot commit an old-space vector.
        if should_stop is not None and should_stop():
            break
        lesson_generation = store._space_generation
        vec = store._embed_bulk_row(text, pace=pace)
        if should_stop is not None and should_stop():
            break
        if not vec:
            continue
        # Stored un-normalized to match write_lesson(): _cosine_sim()
        # normalizes both operands itself.
        blob = struct.pack(f"{len(vec)}f", *vec)
        with store._vector_commit(vec) as current:
            if should_stop is not None and should_stop():
                break
            if not current or lesson_generation != store._space_generation:
                logger.debug("Dropping a lesson backfill from a previous space")
                continue
            # Same three-part guard as _backfill_semantic_kv_embeddings, for
            # the same reason: `embedding IS NULL` alone matches a row whose
            # value was REWRITTEN during the (paced) embed — the write path
            # clears the vector when the rule text changes — so the old
            # rule's vector would be stamped onto the new rule and rank it by
            # text it does not hold. `value_json` pins the row we embedded,
            # and `is_deleted = 0` keeps a vector off a row tombstoned in the
            # same window.
            store.db.execute(
                f"UPDATE {store._sem_rel} SET embedding = ? "
                f"WHERE key = ? AND value_json = ? AND embedding IS NULL "
                f"AND is_deleted = 0{store._sem_guard}",
                (blob, row["key"], row["value_json"]),
            )
        embedded += 1
        if progress is not None:
            progress(embedded, total)
    if embedded:
        logger.info("Backfilled embeddings for %d lessons", embedded)
    return embedded


def backfill_semantic_kv_embeddings(
    store: VectorMemoryStore,
    progress: "Callable[[int, int], None] | None" = None,
    *,
    pace: bool = True,
    max_rows: int | None = None,
    should_stop: "Callable[[], bool] | None" = None,
) -> int:
    """Embed non-lesson semantic rows whose vector is NULL. Returns the count.

    Steady-state rows are embedded at write time (``_write_semantic``); this
    sweep repairs the rest: rows written while the embedding model was
    absent, rows cleared by :meth:`reconcile_embedding_space` after a model
    swap, and bulk-imported rows from :meth:`set_semantic_if_absent`, which
    defers embedding here the way ``write_episodic(defer_embedding=True)``
    does for episodic bulk writers.

    The embedded text is ``"<key> <value_json>"`` — the same text the write
    path embeds and :meth:`get_semantic_context` ranks against, so a
    backfilled vector is indistinguishable from a write-time one. Blobs are
    struct-packed and un-normalized, matching the lesson contract
    (:meth:`_stored_similarity_scorer` divides both norms out). Failures
    leave the row NULL so a later sweep retries it. No FAISS involvement.
    """
    if store.embed_fn is None:
        return 0
    rows = store._backfill_rows(
        "SELECT key, value_json FROM semantic_memory "
        "WHERE is_deleted = 0 AND embedding IS NULL AND key NOT LIKE 'lesson.%'",
        kind="fact",
        identity="key",
        limit=max_rows,
    )
    if not rows:
        return 0
    embedded = 0
    total = len(rows)
    if progress is not None:
        progress(0, total)
    for row in rows:
        # Sampled BEFORE the embed, re-checked under the lock: a model swap
        # landing across the embed must not commit a vector from the old
        # space (reconcile has already swept past this row).
        if should_stop is not None and should_stop():
            break
        backfill_generation = store._space_generation
        vec = store._embed_bulk_row(f"{row['key']} {row['value_json']}", pace=pace)
        if should_stop is not None and should_stop():
            break
        if not vec:
            if progress is not None:
                progress(embedded, total)
            continue
        blob = struct.pack(f"{len(vec)}f", *vec)
        with store._vector_commit(vec) as current:
            if should_stop is not None and should_stop():
                break
            if not current or backfill_generation != store._space_generation:
                logger.debug("Dropping a semantic backfill from a previous space")
                continue
            # value_json guard: a concurrent re-write of this key already
            # cleared-and-refilled its own vector; stamping the OLD value's
            # vector over it would rank the row by text it does not hold.
            # `is_deleted = 0` is the third leg, for the window pacing opens:
            # a row tombstoned during the pause must not come
            # back carrying a vector.
            store.db.execute(
                f"UPDATE {store._sem_rel} SET embedding = ? "
                f"WHERE key = ? AND value_json = ? AND embedding IS NULL "
                f"AND is_deleted = 0{store._sem_guard}",
                (blob, row["key"], row["value_json"]),
            )
            # Drop the cached scoring rows under the writer lock, before the
            # vector-commit context releases it: the stamp changes the
            # embedding column the cache holds, and an own-connection commit
            # does not move data_version, so a reader taking the lock in the
            # gap between commit and a later invalidation would serve the
            # just-embedded row scored keyword-only. Matches the write path.
            store._invalidate_semantic_scoring()
        embedded += 1
        if progress is not None:
            progress(embedded, total)
    if embedded:
        logger.info("Backfilled embeddings for %d semantic entries", embedded)
    return embedded
