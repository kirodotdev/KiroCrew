"""Writing, forgetting and restoring episodes.

``write_episodic_outcome`` screens the text (length bounds, then the injection screen, whose
audit event carries the snippet :func:`kiro_crew.vector_memory._injection_audit_snippet`
redacts), dedups it by text and by vector, applies the V1 capacity rule and adds the vector
to the FAISS accelerator; ``write_episodic`` is its ``bool`` form. The rest forget, restore
and evict episodes and read one row.

The functions are bound on ``VectorMemoryStore`` as the class attributes of the same name
and take the store as ``self``. Store fields read: ``embed_fn``, ``_db_lock``,
``_dedup_threshold``, ``_episodic_max``, ``_faiss_index``, ``_faiss_id_map``,
``_space_generation``, ``_lineage``, ``_epi_rel`` and ``_epi_guard``. Written:
``_faiss_writes_since_save``.
"""

from __future__ import annotations

import json
import logging
import math
import struct
from typing import TYPE_CHECKING

from kiro_crew import memory_record_metadata as record_meta
from kiro_crew import memory_schema
from kiro_crew.vector_memory_runtime.write_outcomes import EpisodicWriteOutcome, SemanticRejectCode

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

# The store's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.vector_memory")


def embed_episodic(self: VectorMemoryStore, text: str) -> list[float] | None:
    """Embed episodic *text* before entering a caller-owned write lock."""
    return self._try_embed(text) if self.embed_fn else None


def write_episodic(
    self: VectorMemoryStore,
    text: str,
    embedding: list[float] | None = None,
    conversation_id: str = "",
    tags: list[str] | None = None,
    importance: float = 0.5,
    source: str = "consolidation",
    *,
    preserve_existing: bool = False,
    defer_embedding: bool = False,
    embedding_resolved: bool = False,
    embedding_generation: int | None = None,
    facets: "memory_schema.MemoryFacets | None" = None,
    metadata: dict | None = None,
) -> bool:
    """Write an episodic memory; ``True`` only if a row was written.

    See :meth:`write_episodic_outcome`, which this wraps, for the arguments
    and for WHY a write was refused.
    """
    return (
        self.write_episodic_outcome(
            text,
            embedding,
            conversation_id,
            tags,
            importance,
            source,
            preserve_existing=preserve_existing,
            defer_embedding=defer_embedding,
            embedding_resolved=embedding_resolved,
            embedding_generation=embedding_generation,
            facets=facets,
            metadata=metadata,
        )
        is EpisodicWriteOutcome.WRITTEN
    )


def write_episodic_outcome(
    self: VectorMemoryStore,
    text: str,
    embedding: list[float] | None = None,
    conversation_id: str = "",
    tags: list[str] | None = None,
    importance: float = 0.5,
    source: str = "consolidation",
    *,
    preserve_existing: bool = False,
    defer_embedding: bool = False,
    embedding_resolved: bool = False,
    embedding_generation: int | None = None,
    facets: "memory_schema.MemoryFacets | None" = None,
    metadata: dict | None = None,
) -> "EpisodicWriteOutcome":
    """Write an episodic memory with optional embedding and dedup.

    Returns what the write DID. The capacity refusal is decided inside the
    same ``BEGIN IMMEDIATE`` transaction that would have inserted the row, so
    :attr:`EpisodicWriteOutcome.AT_CAPACITY` is the store's own verdict at
    refusal time -- a caller must not re-derive it from a later count, which a
    concurrent cap raise or eviction can change in between.

    *facets* stamps the crew lineage's carve axes and is ignored on v1. An
    episode is the kind that most needs them: it is delivered ONLY by
    per-prompt similarity search, so the carve is the only thing that can
    bound which episodes a query is even allowed to surface.

    ``preserve_existing`` rejects similarity and capacity conflicts instead
    of tombstoning an active entry. Import paths use it to remain merge-only,
    and so does any writer that passes ``defer_embedding``: with no vector the
    similarity dedup below cannot run, and a row admitted without it must not
    evict one it was never compared against.

    ``defer_embedding`` stores the row with a NULL embedding instead of
    embedding inline, leaving it for :meth:`backfill_missing_embeddings`.
    ``embedding_resolved`` says the caller already attempted inference and
    supplied its result, including ``None``; this method then performs no
    inference of its own. Such a caller also supplies ``embedding_generation``
    from :attr:`space_generation` before inference, so a model swap in the gap
    leaves the vector NULL. Inference cost grows steeply with text length (~0.4s per 2000-char chunk
    on CPU), so a bulk writer such as the onboarding importer would hold its
    caller for minutes. The row is FTS5 keyword-searchable immediately, and
    becomes semantically searchable once the sweep fills it in. Only for
    callers that schedule that sweep — a row left NULL forever is silently
    absent from vector search. Deferral also skips the similarity dedup
    (which needs a vector), so the caller keeps its own duplicate check.
    """

    from kiro_crew import vector_memory as vm  # circular import: the facade imports this module

    _EPISODIC_TEXT_MAX = vm._EPISODIC_TEXT_MAX
    _EPISODIC_TEXT_MIN = vm._EPISODIC_TEXT_MIN
    _FAISS_SAVE_INTERVAL = vm._FAISS_SAVE_INTERVAL
    _HAS_NUMPY = vm._HAS_NUMPY
    _contains_injection = vm._contains_injection
    _now_iso = vm._now_iso
    np = vm.np
    uuid4 = vm.uuid4

    text = text.strip()
    metadata = record_meta.normalize_metadata(metadata) if metadata is not None else None
    if facets and facets.derived_from:
        metadata = {**(metadata or {}), "source_ref": facets.derived_from}
    if len(text) < _EPISODIC_TEXT_MIN or len(text) > _EPISODIC_TEXT_MAX:
        logger.debug(
            "Episodic rejected: len=%d (min=%d max=%d)",
            len(text),
            _EPISODIC_TEXT_MIN,
            _EPISODIC_TEXT_MAX,
        )
        return EpisodicWriteOutcome.REFUSED

    # Prompt-injection screening (XPIA defense-in-depth).
    # Episodic text is derived from conversation transcripts, so a poisoned
    # turn could persist steering instructions that get re-injected into
    # future contexts. Mirror the semantic-KV screen (validate_semantic) and
    # drop the entry on match, emitting an auditable reject event.
    if _contains_injection(text):
        logger.warning("Episodic write rejected: blocked content patterns (src=%s)", source)
        # Scrub untrusted rejected content before persisting its audit snippet.
        # The dashboard also redacts all memory events before returning them.
        safe_snippet = vm._injection_audit_snippet(text)
        self._log_event(
            SemanticRejectCode.INJECTION.value,
            "episodic",
            "",
            None,
            safe_snippet,
            source,
        )
        return EpisodicWriteOutcome.REFUSED

    clean_tags = [t.strip().lower()[:50] for t in (tags or [])[:10] if t.strip()]
    importance = max(0.0, min(1.0, importance))

    # Text-hash dedup: reject near-identical text before expensive embedding.
    # The store shares one SQLite connection across worker threads, so even
    # this read must use the same lock as the write-side double-check.
    text_prefix = text if self.algorithm_version == "v2" else text[:80].lower()
    dedup_predicate = (
        "text = ?" if self.algorithm_version == "v2" else "LOWER(SUBSTR(text, 1, 80)) = ?"
    )
    with self._db_lock:
        existing = self.db.execute(
            "SELECT id FROM episodic_memories WHERE is_deleted = 0 " f"AND {dedup_predicate}",
            (text_prefix,),
        ).fetchone()
    if existing:
        logger.debug("Episodic text-hash dedup: prefix matches id=%s", existing["id"])
        return EpisodicWriteOutcome.REFUSED

    # Auto-embed if no embedding provided and embed_fn available.
    #
    # `embed_generation` records which vector space the embedding below belongs
    # to. _try_embed already discards a vector produced ACROSS a space change,
    # but it returns before this function takes _db_lock, and a model swap can
    # land in that gap — most plausibly while the INSERT queues behind
    # reconcile's own lock hold. Committing then would leave a stale-space
    # vector that reconcile has already swept past and that backfill never
    # revisits, because backfill only refills NULLs. So carry the generation to
    # the write and re-check it while holding the lock.
    embed_generation = (
        embedding_generation
        if embedding_resolved and embedding_generation is not None
        else self._space_generation
    )
    if (
        embedding is None
        and not defer_embedding
        and not embedding_resolved
        and self.embed_fn is not None
    ):
        embedding = self._try_embed(text)

    embedding_blob: bytes | None = None
    if embedding is not None:
        if _HAS_NUMPY:
            vec = np.array(embedding, dtype=np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            embedding_blob = vec.tobytes()
        else:
            # Normalize without numpy
            norm_f: float = math.sqrt(sum(x * x for x in embedding))
            normed = [x / norm_f for x in embedding] if norm_f > 0 else embedding
            embedding_blob = struct.pack(f"{len(normed)}f", *normed)

    # db + FAISS critical section — serialized against concurrent readers on
    # the event loop thread (search_episodic) and other writer threads. The
    # blocking embed above already ran outside the lock, so this only guards
    # local work. FAISS add + _faiss_id_map.append MUST stay atomic together:
    # a reader that sees index.ntotal == N+1 while len(id_map) == N would
    # IndexError (or the concurrent add/search would corrupt the C++ index).
    with self._embedding_config_guard(embedding), self._db_lock:
        if embedding_blob is not None and self._space_generation != embed_generation:
            # A model swap landed between the embed and this lock. Persist NULL
            # rather than a vector from the previous space — the backfill at the
            # end of the swap re-embeds this row in the new one. The text is
            # still written, so nothing is lost.
            logger.debug("Dropping an episodic embedding produced in a previous space")
            embedding_blob = None
            embedding = None
        # Re-check under the write lock. The fast check above avoids an
        # unnecessary embed in the common case, but cannot prevent a native
        # writer from inserting the same text between that check and this
        # critical section.
        existing = self.db.execute(
            "SELECT id FROM episodic_memories WHERE is_deleted = 0 " f"AND {dedup_predicate}",
            (text_prefix,),
        ).fetchone()
        if existing is not None:
            logger.debug(
                "Episodic text-hash dedup under lock: prefix matches id=%s",
                existing["id"],
            )
            return EpisodicWriteOutcome.REFUSED
        # Dedup via FAISS — only when THIS write has an embedding. The index
        # being non-empty says nothing about the current write: with embeddings
        # disabled (embedding_provider="none") or a transient embed failure,
        # `embedding_blob` is None and the query vector below would be unbound
        # (UnboundLocalError), losing the memory entirely. Degrade to a
        # non-deduped write instead (the text-prefix dedup above still applies).
        if (
            self.algorithm_version != "v2"
            and embedding_blob is not None
            and self._faiss_index is not None
            and self._faiss_index.ntotal > 0  # type: ignore[attr-defined]
        ):
            query_vec = np.frombuffer(embedding_blob, dtype=np.float32).reshape(1, -1)
            distances, indices = self._faiss_index.search(query_vec, 5)  # type: ignore[attr-defined]
            for dist, idx in zip(distances[0], indices[0]):
                if idx == -1:
                    break
                cosine_sim = float(dist)  # inner product on normalized = cosine
                if cosine_sim > self._dedup_threshold:
                    existing_id = self._faiss_id_map[int(idx)]
                    existing = self._get_episodic(existing_id)
                    if existing is None:
                        # The matched vector points to a tombstoned/deleted row
                        # (a "ghost": tombstone paths set is_deleted=1 but never
                        # remove the vector from _faiss_index/_faiss_id_map, so it
                        # keeps matching). _get_episodic filters is_deleted=0, so it
                        # is None here. Treating that as a conflict would REJECT the
                        # new write against a deleted memory (data loss). Skip the
                        # ghost and keep scanning, mirroring search_episodic's
                        # `if not mem or mem["is_deleted"]: continue`.
                        continue
                    if preserve_existing:
                        self._log_event(
                            "conflict_skip",
                            "episodic",
                            existing_id,
                            "",
                            text[:200],
                            source,
                        )
                        return EpisodicWriteOutcome.REFUSED
                    if len(text) > len(existing["text"]) * 1.2:
                        self._delete_episodic_row(existing_id)
                        self._log_event(
                            "merge",
                            "episodic",
                            existing_id,
                            existing["text"][:200],
                            text[:200],
                            source,
                        )
                        break
                    else:
                        self._log_event(
                            "conflict_skip",
                            "episodic",
                            existing_id,
                            "",
                            text[:200],
                            source,
                        )
                        return EpisodicWriteOutcome.REFUSED

        if preserve_existing:
            mem_id = str(uuid4())
            now = _now_iso()
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if not self._embedding_current(embedding):
                    embedding_blob = None
                active_count = self.db.execute(
                    "SELECT COUNT(*) FROM episodic_memories WHERE is_deleted = 0"
                ).fetchone()[0]
                if self.algorithm_version != "v2" and active_count >= self._episodic_max:
                    self.db.commit()
                    return EpisodicWriteOutcome.AT_CAPACITY
                self.db.execute(
                    memory_schema.episodic_insert(self._lineage),
                    memory_schema.episodic_insert_params(
                        self._lineage,
                        mem_id,
                        conversation_id,
                        text,
                        embedding_blob,
                        json.dumps(clean_tags),
                        importance,
                        now,
                        source,
                    ),
                )
                self._record_mutation(
                    "episode", mem_id, None, source, metadata=metadata, operation="create"
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        else:
            self._enforce_episodic_cap()
            mem_id = str(uuid4())
            now = _now_iso()
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                if not self._embedding_current(embedding):
                    embedding_blob = None
                self.db.execute(
                    memory_schema.episodic_insert(self._lineage),
                    memory_schema.episodic_insert_params(
                        self._lineage,
                        mem_id,
                        conversation_id,
                        text,
                        embedding_blob,
                        json.dumps(clean_tags),
                        importance,
                        now,
                        source,
                    ),
                )
                self._record_mutation(
                    "episode", mem_id, None, source, metadata=metadata, operation="create"
                )
                self.db.commit()

        # Add to FAISS. The C++ index and the Python _faiss_id_map MUST commit
        # together — if index.ntotal ends up ahead of len(_faiss_id_map) a later
        # lookup IndexErrors and similarity results desync. Append the id first
        # (a cheap, reliable list op), then add the vector, and roll the id back
        # if the add raises so the two structures stay atomically in sync.
        self._invalidate_episodic_scoring()
        if embedding_blob is not None and self._faiss_index is not None:
            vec = np.frombuffer(embedding_blob, dtype=np.float32).reshape(1, -1)
            self._faiss_id_map.append(mem_id)
            try:
                self._faiss_index.add(vec)  # type: ignore[attr-defined]
            except Exception:
                self._faiss_id_map.pop()  # roll back partial add — keep in sync
                raise
            self._faiss_writes_since_save += 1
            if self._faiss_writes_since_save >= _FAISS_SAVE_INTERVAL:
                self.save_faiss_index()

    self._stamp_facets(mem_id, facets)
    self._log_event("create", "episodic", mem_id, None, text[:200], source)
    has_vec = embedding_blob is not None
    logger.debug(
        "Episodic written: id=%s src=%s imp=%.2f vec=%s text=%s…",
        mem_id[:8],
        source,
        importance,
        has_vec,
        text[:80],
    )
    return EpisodicWriteOutcome.WRITTEN


def has_episodic_text(self: VectorMemoryStore, text: str) -> bool:
    """Return whether an active episodic memory exactly matches *text*."""
    return (
        self._fetch_one_locked(
            "SELECT 1 FROM episodic_memories WHERE is_deleted = 0 AND text = ? LIMIT 1",
            (text,),
        )
        is not None
    )


def restore_episodic(self: VectorMemoryStore, mem_id: str, source: str = "user_explicit") -> bool:
    """Un-tombstone one episode. False when it is absent or already active.

    Restores rather than re-inserting, so the row keeps its id, its text, its
    vector and its ``created_at`` — a re-insert would look like a new memory and
    would re-enter the similarity dedup that may have been what removed it.
    """
    with self._db_lock, self.db:
        row = self.db.execute(
            "SELECT * FROM episodic_memories WHERE id = ? AND is_deleted = 1",
            (mem_id,),
        ).fetchone()
        if row is None:
            return False
        self.db.execute(
            f"UPDATE {self._epi_rel} SET is_deleted = 0 WHERE id = ?{self._epi_guard}",
            (mem_id,),
        )
        self._record_mutation(
            "episode",
            mem_id,
            dict(row),
            source,
            metadata={"status": "active"},
            operation="restore",
        )
        self.db.commit()
        # A warm NumPy set or FAISS index may have been built while this
        # episode was retired. A commit on this connection does not bump
        # PRAGMA data_version, so invalidate both derived populations now.
        self.invalidate_episode_content()
    # Logged so a restore is as auditable as the retire was, and so a row that keeps
    # being retired and restored is visible as a loop rather than as churn.
    self._log_event("restore", "episodic", mem_id, None, row["text"][:200], source)
    logger.info("Restored retired episodic entry %s", mem_id[:8])
    return True


def delete_episodic(self: VectorMemoryStore, mem_id: str, source: str = "user_explicit") -> bool:
    """Tombstone an episodic memory."""
    existing = self._get_episodic(mem_id)
    if not existing:
        return False
    with self._db_lock, self.db:
        self.db.execute(
            f"UPDATE {self._epi_rel} SET is_deleted = 1 WHERE id = ?{self._epi_guard}",
            (mem_id,),
        )
        self._record_mutation("episode", mem_id, existing, source, operation="forget")
        self.db.commit()
        self._invalidate_episodic_scoring()
    self._log_event("delete", "episodic", mem_id, existing["text"][:200], None, source)
    return True


def _get_episodic(self: VectorMemoryStore, mem_id: str) -> dict | None:
    row = self._fetch_one_locked(
        "SELECT * FROM episodic_memories WHERE id = ? AND is_deleted = 0", (mem_id,)
    )
    return dict(row) if row else None


def _delete_episodic_row(self: VectorMemoryStore, mem_id: str) -> None:
    with self._db_lock, self.db:
        before = self.db.execute("SELECT * FROM episodic_memories WHERE id=?", (mem_id,)).fetchone()
        self.db.execute(
            f"UPDATE {self._epi_rel} SET is_deleted = 1 WHERE id = ?{self._epi_guard}",
            (mem_id,),
        )
        self._record_mutation(
            "episode", mem_id, dict(before) if before else None, "dedup", operation="forget"
        )
        self.db.commit()
        self._invalidate_episodic_scoring()


def _enforce_episodic_cap(self: VectorMemoryStore) -> None:
    """Enforce the legacy V1 cap; private V2 memory is retained until corrected or forgotten."""
    if self.algorithm_version == "v2":
        return
    with self._db_lock:
        count = self.db.execute(
            "SELECT COUNT(*) FROM episodic_memories WHERE is_deleted = 0"
        ).fetchone()[0]
        if count < self._episodic_max:
            return
        excess = count - self._episodic_max + 1
        rows = self.db.execute(
            "SELECT * FROM episodic_memories WHERE is_deleted = 0 "
            "ORDER BY importance ASC, created_at ASC LIMIT ?",
            (excess,),
        ).fetchall()
        for row in rows:
            self.db.execute(
                f"UPDATE {self._epi_rel} SET is_deleted = 1 WHERE id = ?{self._epi_guard}",
                (row["id"],),
            )
            self._record_mutation("episode", row["id"], dict(row), "capacity", operation="forget")
        self.db.commit()
        self._invalidate_episodic_scoring()
