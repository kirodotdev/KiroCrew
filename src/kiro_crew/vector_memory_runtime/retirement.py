"""Supersession retirement: which episodes a changed semantic value retires.

Called after a semantic write commits a CHANGED value. Global V1 keeps its
original heuristic (a cosine match over ``"<key suffix>: <old value>"``, then an
exact-phrase fallback); member V2 retires only an episode that literally asserts
the old value for the full key. Both spend one ``_MAX_EPISODIC_RETIRED_PER_WRITE``
budget per write, and every retirement is a tombstone with an audit event, which
``get_retired_episodic`` lists and ``VectorMemoryStore.restore_episodic`` undoes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew import memory_v2
from kiro_crew.vector_memory_constants import _MAX_EPISODIC_RETIRED_PER_WRITE

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

# The store's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.vector_memory")


def retire_one_episodic(
    store: VectorMemoryStore, mem_id: str, text: str, superseded_by: str
) -> None:
    """Tombstone one episode as superseded, recording enough to undo it.

    Takes ``_db_lock`` itself rather than relying on the caller's hold. The lock is
    an ``RLock`` precisely so a locked section can call a helper that re-acquires,
    and taking it here makes the helper correct at any call site instead of at the
    two that happen to exist.

    ``conflict_retire`` and ``semantic_update`` are the event/source pair
    :meth:`get_retired_episodic` reads to tell a supersession apart from a user's
    own delete, so both spellings are part of the contract rather than log text.

    *superseded_by* — the semantic KEY whose new value triggered this — goes in the
    event's ``new_value``, which these events otherwise leave empty.
    ``memory_key`` has to be the EPISODE's id for the recovery listing to join on
    it, so without this the record says a row was superseded and never says by
    what: the one question a reader deciding whether to restore it actually asks.
    """
    with store._db_lock:
        before = store.db.execute(
            "SELECT * FROM episodic_memories WHERE id=?", (mem_id,)
        ).fetchone()
        store.db.execute(
            f"UPDATE {store._epi_rel} SET is_deleted = 1 WHERE id = ?{store._epi_guard}",
            (mem_id,),
        )
        store._record_mutation(
            "episode",
            mem_id,
            dict(before) if before else None,
            "semantic_update",
            metadata={"status": "superseded"},
            operation="supersede",
        )
    store._log_event(
        "conflict_retire", "episodic", mem_id, text[:200], superseded_by, "semantic_update"
    )


def retire_stale_episodic(
    store: VectorMemoryStore,
    key: str,
    old_value: str,
    *,
    defer_embedding: bool = False,
    query_embedding: list[float] | None = None,
    embedding_resolved: bool = False,
) -> None:
    """V1 keeps its original heuristic; member V2 requires literal evidence.

    Both share ``_MAX_EPISODIC_RETIRED_PER_WRITE``: the heuristic decides WHICH
    episodes a write may retire, the cap decides HOW MANY. ``defer_embedding``
    reaches only V1, the arm that embeds; V2 proves supersession from text.
    """
    if store.algorithm_version != "v2":
        store._retire_stale_episodic_v1(
            key,
            old_value,
            defer_embedding=defer_embedding,
            query_embedding=query_embedding,
            embedding_resolved=embedding_resolved,
        )
        return
    # No embedding/similarity can prove a contradiction. Require the old
    # value in an assertion about this key, then keep an undoable audit row.
    with store._db_lock:
        rows = store.db.execute(
            "SELECT id, text FROM episodic_memories WHERE is_deleted = 0 "
            "ORDER BY created_at DESC, id"
        ).fetchall()
        retired = 0
        for row in rows:
            if not memory_v2.superseded_value_is_asserted(row["text"], key, old_value):
                continue
            store._retire_one_episodic(row["id"], row["text"], key)
            retired += 1
            if retired >= _MAX_EPISODIC_RETIRED_PER_WRITE:
                break
        if retired:
            store.db.commit()
            store._invalidate_episodic_scoring()


def retire_stale_episodic_v1(
    store: VectorMemoryStore,
    key: str,
    old_value: str,
    *,
    defer_embedding: bool = False,
    query_embedding: list[float] | None = None,
    embedding_resolved: bool = False,
) -> None:
    """Soft-delete episodic entries that reference a superseded semantic value.

    Uses vector similarity search when embeddings are available (catches
    rephrased references like "User prefers red" for key "color", old "red").
    Falls back to exact phrase text matching otherwise.

    ``_MAX_EPISODIC_RETIRED_PER_WRITE`` is ONE budget for the whole call, spent
    by the vector arm first and then by the text fallback -- not a budget per
    arm, which would let a write retire twice the cap. A candidate beyond the
    cap stays alive; the vector arm's pool (``limit=50``) is a search width,
    not a retirement width, and the fallback's ``LIMIT`` fetches only what the
    remaining budget can retire.
    """
    seen: set[str] = set()

    # Vector similarity: embed "key_suffix: old_value" and find similar episodic
    key_suffix = key.rsplit(".", 1)[-1].replace("_", " ")
    query = f"{key_suffix}: {old_value}"
    # The embed is the one blocking call here, so it stays OUTSIDE the lock.
    # Everything after it touches the shared sqlite connection and MUST be
    # serialized on _db_lock: an unsynchronized DML statement races the
    # implicit BEGIN of any concurrent writer (search_episodic's
    # last_accessed_at write, another consolidation) and the loser raises
    # "cannot start a transaction within a transaction".
    # ``defer_embedding`` takes the same arm an unavailable embedder takes:
    # the text fallback below. Retiring fewer rephrased episodes is what this
    # path already does whenever the embed answers None.
    if embedding_resolved:
        emb = query_embedding
    elif defer_embedding:
        emb = None
    else:
        emb = store._try_embed(query)
    with store._db_lock:
        if emb is not None:
            # mmr=False: internal write-path caller that applies its own cosine
            # threshold below, so the MMR diversity rerank buys nothing here and
            # cost ~71ms per superseding write at 1,000 pooled candidates
            # per superseding write. mmr also SIZES the candidate pool
            # (limit vs _MMR_MAX_POOL), so keep the limit wide: the 0.7
            # threshold, not the pool cut, decides WHICH rows are candidates;
            # the per-write cap decides how many of them are retired.
            results = store.search_episodic(query_embedding=emb, query_text="", limit=50, mmr=False)
            for r in results:
                if len(seen) >= _MAX_EPISODIC_RETIRED_PER_WRITE:
                    break
                if r.get("cosine_sim", 0) > 0.7 and r["id"] not in seen:
                    seen.add(r["id"])
                    store.db.execute(
                        f"UPDATE {store._epi_rel} SET is_deleted = 1 WHERE id = ?{store._epi_guard}",
                        (r["id"],),
                    )
                    store._log_event(
                        "conflict_retire",
                        "episodic",
                        r["id"],
                        r["text"][:200],
                        None,
                        "semantic_update",
                    )

        # Text fallback: exact phrase matching. The rows the vector arm just
        # tombstoned are already is_deleted=1 on this connection, so the
        # ``seen`` check only guards the two patterns against each other.
        patterns = [f"%{key_suffix}: {old_value}%", f"%{key_suffix} {old_value}%"]
        for pat in patterns:
            remaining = _MAX_EPISODIC_RETIRED_PER_WRITE - len(seen)
            if remaining <= 0:
                break
            for r in store.db.execute(
                "SELECT id, text FROM episodic_memories WHERE is_deleted = 0 AND text LIKE ? "
                "ORDER BY created_at DESC, id LIMIT ?",
                (pat, remaining),
            ).fetchall():
                if r["id"] not in seen:
                    seen.add(r["id"])
                    store.db.execute(
                        f"UPDATE {store._epi_rel} SET is_deleted = 1 WHERE id = ?{store._epi_guard}",
                        (r["id"],),
                    )
                    store._log_event(
                        "conflict_retire",
                        "episodic",
                        r["id"],
                        r["text"][:200],
                        None,
                        "semantic_update",
                    )

        if seen:
            store.db.commit()
            store._invalidate_episodic_scoring()
    if seen:
        logger.info("Retired %d stale episodic entries for key %r", len(seen), key)


def get_retired_episodic(store: VectorMemoryStore, limit: int = 50, offset: int = 0) -> list[dict]:
    """Episodes a semantic write SUPERSEDED, newest first, with what superseded them.

    The recovery half of :meth:`_retire_stale_episodic`. A tombstone there is a
    similarity judgement about what is now false, and nothing in the store ever
    hard-deletes an episode — so the row and its full text survive, and the only
    thing missing was a way to look. Without this the rule is indistinguishable
    from data loss: every reader filters ``is_deleted = 0``.

    Joined to ``memory_events`` on the ``conflict_retire`` / ``semantic_update``
    pair, so a user's own delete is NOT listed: those two paths mean different
    things and only one of them was a guess. ``retired_at`` is the event's stamp,
    which is when the row went rather than when it was written.

    GROUPED BY episode, because the event log is append-only and a row that was
    retired, restored and retired again has one event per retirement — so an
    ungrouped join lists the same episode several times and makes ``limit`` page a
    number of EVENTS while the caller asked for a number of episodes. ``retired_at``
    is therefore the MOST RECENT retirement, and ``retired_times`` carries the count:
    a row that keeps coming back is the signal that the rule and the operator
    disagree about it, which is worth seeing rather than flattening away.
    """
    rows = store._fetch_all_locked(
        "SELECT e.id, e.conversation_id, e.text, e.tags, e.importance, e.created_at, "
        "       MAX(v.created_at) AS retired_at, "
        "       v.new_value AS superseded_by, COUNT(*) AS retired_times "
        "FROM episodic_memories e "
        "JOIN memory_events v ON v.memory_key = e.id "
        "WHERE e.is_deleted = 1 AND v.event_type = 'conflict_retire' "
        "  AND v.memory_type = 'episodic' AND v.source = 'semantic_update' "
        "GROUP BY e.id "
        "ORDER BY retired_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return [dict(r) for r in rows]
