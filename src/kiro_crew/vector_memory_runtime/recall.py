"""On-demand recall: one bounded evidence payload over the selected store.

``recall`` embeds the question at most once, then reads facts, episodes and
lessons with that single result under the store lock; a vector space that
changed in between discards the partial answer and retries lexically without a
second inference. ``recall_once`` fits the evidence into the character cap,
lets the ``memory.recall`` decision hook narrow the episodes the budget chose,
and renders the payload from the evidence it selected.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from kiro_crew import memory_v2
from kiro_crew.embeddings import PRIORITY_INTERACTIVE
from kiro_crew.vector_memory_runtime import episodic_search as _episodic_search
from kiro_crew.vector_memory_runtime.embedding import _RecallQuery, _RecallSpaceChanged

if TYPE_CHECKING:
    from pathlib import Path

    from kiro_crew.vector_memory import VectorMemoryStore

# The store's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.vector_memory")


def _kept_episodes(
    results: list[dict],
    keep: Callable[[list[dict]], list[dict] | None] | None,
) -> list[dict]:
    """*results* narrowed by *keep*, or *results* unchanged.

    Every unusable answer keeps the full result: ``None`` (no decision), a raise, a
    non-sequence, and a row the search did not produce. The last one matters most -- a
    hook may REMOVE entries and nothing else, so an answer carrying an unknown row is
    treated as unusable rather than returned, and a recall can never hand back a memory
    its own search did not rank.

    Identity, not equality, is what membership is judged on: two distinct episodes
    can hold equal dicts, and a membership test by value would let one answer
    admit the other.

    Ranked order is preserved: the walk is over *results*, so a hook's own ordering is
    discarded. A keep/drop answer says nothing about rank.
    """
    if keep is None:
        return results
    try:
        narrowed = keep(list(results))
    except Exception:
        logger.debug("Episodic keep hook failed; injecting the similarity result")
        return results
    if narrowed is None:
        return results
    if not isinstance(narrowed, list):
        logger.debug(
            "Episodic keep hook returned %s; injecting the similarity result", type(narrowed)
        )
        return results
    offered = {id(row) for row in results}
    if any(id(row) not in offered for row in narrowed):
        logger.debug("Episodic keep hook named a row this search did not rank; injecting it whole")
        return results
    # Ranked order is the search's, so the hook's own ordering is discarded: it
    # answered a keep/drop question, which says nothing about rank.
    chosen = {id(row) for row in narrowed}
    return [row for row in results if id(row) in chosen]


def get_context_preview(store: VectorMemoryStore, query_text: str = "") -> dict:
    """Preview what would be injected into context (for debugging).

    Reports the UNSCOPED view. A ``project_dir`` parameter was offered here
    briefly and removed: the only caller never passed one, so it could not
    change any observed output, and a knob nobody turns still has to be read
    and trusted by whoever comes next.
    """
    if store.algorithm_version == "v2":
        return store.recall(query_text)
    semantic = store.get_semantic_context(query_text=query_text)
    episodic = store.get_episodic_context(query_text=query_text)
    lessons = store.get_lessons_context(query_text=query_text)
    return {
        "semantic_chars": len(semantic),
        "episodic_chars": len(episodic),
        "lessons_chars": len(lessons),
        "total_chars": len(semantic) + len(episodic) + len(lessons),
        "semantic_preview": semantic[:500],
        "episodic_preview": episodic[:500],
        "lessons_count": len(store.get_lessons()),
    }


def check_recall_query(store: VectorMemoryStore, query: _RecallQuery | None) -> None:
    """Called under the store lock before a read and before publication."""
    if query is not None and query.generation is not None:
        from kiro_crew import embeddings

        if (
            store.embed_fn is embeddings.make_sync_embed_fn()
            and embeddings.store_embedding_space_is_stale(store)
        ):
            raise _RecallSpaceChanged
        if (
            query.generation != store._space_generation
            or query.signature != store.recorded_embedding_space()
            or not store._embedding_current(query.vector)
        ):
            raise _RecallSpaceChanged


def recall(
    store: VectorMemoryStore,
    query_text: str,
    *,
    cap: int = 3000,
    project_dir: str | Path | None = None,
    keep: Callable[[list[dict]], list[dict] | None] | None = None,
) -> dict:
    """Compute once; discard mixed-space results and retry keyword-only once.

    *keep*, when given, may narrow the recalled EPISODES before they are returned;
    it returns ``None`` to keep every one. It is the seam the ``memory.recall``
    decision point attaches to (``decisions/points/memory_recall.py``), reached
    through the ``memory_recall`` tool, and it is a callable rather than a filtered
    list so this method still owns the search: a hook that raises, returns a
    non-list, or names rows this search did not produce leaves the recall result
    exactly as it is.
    """
    with store._db_lock:
        generation = store._space_generation
        signature = store.recorded_embedding_space()
    vector = (
        store._try_embed(query_text, PRIORITY_INTERACTIVE)
        if query_text.strip() and cap > 0 and store.embed_fn
        else None
    )
    query = _RecallQuery(vector, generation, signature)
    try:
        return store._recall_once(
            query_text, cap=cap, project_dir=project_dir, query=query, keep=keep
        )
    except _RecallSpaceChanged:
        # No inference on the retry, even when the first inference failed.
        # Keyword ranking cannot mix vector spaces during another switch.
        return store._recall_once(
            query_text,
            cap=cap,
            project_dir=project_dir,
            query=_RecallQuery(None, None, None),
            keep=keep,
        )


def recall_once(
    store: VectorMemoryStore,
    query_text: str,
    *,
    cap: int,
    project_dir: str | Path | None,
    query: _RecallQuery,
    keep: Callable[[list[dict]], list[dict] | None] | None = None,
) -> dict:
    """Bounded on-demand member context with the evidence actually selected.

    Reads only this store, using its existing V1 or V2 ranking policy.
    """
    from kiro_crew import memory_recall
    from kiro_crew.memory_recall import (
        bound_recall_payload,
        recall_evidence,
        v2_operating_point,
    )

    def retrieval(facts: list[dict], episodes: list[dict]) -> dict:
        evidence: dict = {"facts": facts, "episodes": episodes}
        if store.algorithm_version == "v2":
            evidence["operating_point"] = v2_operating_point(
                store.recorded_embedding_space(), embed_fn=store.embed_fn
            )
        return evidence

    cap = min(max(0, int(cap)), 12000)
    if not query_text.strip() or cap == 0:
        return {
            "algorithm_version": store.algorithm_version,
            "policy_revision": store.policy_revision,
            "semantic_context": "",
            "episodic_context": "",
            "lessons_context": "",
            "retrieval": retrieval([], []),
            "semantic_chars": 0,
            "episodic_chars": 0,
            "lessons_chars": 0,
            "total_chars": 0,
            "semantic_preview": "",
            "episodic_preview": "",
            "lessons_count": 0,
        }
    query_embedding = query.vector
    # Embed the original question once; lexical scoring uses its topic
    # terms so CJK question endings cannot suppress known facts.
    query_text = " ".join(sorted(memory_recall.recall_terms(query_text)))
    facts = (
        store._semantic_candidates_v2(query_text, recall_query=query)
        if store.algorithm_version == "v2"
        else store._semantic_candidates_v1(query_text, recall_query=query)
    )
    for fact in facts:
        fact.setdefault("id", f"key:{fact['key']}")
        fact.pop("embedding", None)
        fact.setdefault("retrieval", {"reason": "v1_hybrid_match"})
    episodes = store.search_episodic(
        query_embedding=query_embedding,
        query_text=query_text,
        limit=store._episodic_limit,
        relevance_filter=True,
        recall_query=query,
    )

    for episode in episodes:
        episode.pop("embedding", None)
        episode.setdefault(
            "retrieval",
            {
                "reason": (
                    "v1_vector_match" if query_embedding is not None else "v1_keyword_match"
                ),
                "cosine": episode.get("cosine_sim"),
            },
        )

    def fit(rows: list[dict], budget: int, *, episodic: bool) -> tuple[int, list[dict]]:
        """Select evidence that fits *budget* chars of ``[memory:id] body`` lines.

        Returns the characters those lines consume and the selected evidence.
        ``bound_recall_payload`` renders the model-facing context from the
        evidence, so there is one formatter and the two cannot drift.
        """
        chosen = []
        remaining = budget
        for row in rows:
            truncated = False
            display_id = row["id"]
            if episodic:
                body = row["text"][: _episodic_search.EPISODIC_BLOCK_TEXT_CHARS]
            else:
                body = f"{store._fact_label(row)}: {memory_v2.visible_json(row['value_json'])}"
            line = f"[memory:{display_id}] {body}\n"
            if len(line) > remaining:
                # V2 admitted this evidence already. Preserve one bounded,
                # locatable snippet when its full text alone exceeds this
                # section's share rather than silently dropping the row.
                framing = len(f"[memory:{display_id}] \n")
                available = remaining - framing
                marker = "… [truncated]"
                if available < 32 or available <= len(marker):
                    continue
                if not episodic:
                    label = store._fact_label(row)[: min(96, max(8, available // 3))]
                    value = memory_v2.visible_json(row["value_json"])
                    body = f"{label}: {value}"
                body = body[: available - len(marker)] + marker
                line = f"[memory:{display_id}] {body}\n"
                truncated = True
            evidence = recall_evidence(row, body, episodic=episodic)
            if truncated:
                evidence["text_truncated" if episodic else "snippet_truncated"] = True
            chosen.append(evidence)
            remaining -= len(line)
        return budget - remaining, chosen

    # Reserve the rules budget first; context never exceeds the requested
    # cap, including wrappers. Small caps may safely return no memory.
    # Recall is the only way a private store's or a non-default workspace's
    # lessons reach the model, so they stay in the payload.
    lessons = store.get_lessons_context(
        query_text, cap=cap // 3, project_dir=project_dir, recall_query=query
    )
    if len(lessons) > cap // 3:
        lessons = ""
    remainder = cap - len(lessons)
    wrapper_size = memory_recall.CONTEXT_WRAPPER_CHARS
    semantic_chars, facts = fit(facts, max(0, remainder // 2 - wrapper_size), episodic=False)
    if semantic_chars:
        semantic_chars += wrapper_size
    _, episodes = fit(episodes, max(0, remainder - semantic_chars - wrapper_size), episodic=True)
    # The decision seam, AFTER `fit` and before the payload is rendered. The
    # ordering is the rule: `fit` is the char budget, so it decides which ranked
    # episodes this recall would return. A hook shown the pre-budget list could drop
    # a high-ranked episode and free room a lower-ranked one then fits into, which is
    # the hook WIDENING the result rather than narrowing it. Screening what `fit`
    # selected can only shrink the payload, and `bound_recall_payload` below renders
    # the contexts and char counts from the evidence, so the numbers follow.
    episodes = _kept_episodes(episodes, keep)
    # Contexts, char counts and previews are rendered from the evidence here.
    result = bound_recall_payload(
        {
            "algorithm_version": store.algorithm_version,
            "policy_revision": store.policy_revision,
            "lessons_context": lessons,
            "retrieval": retrieval(facts, episodes),
            "lessons_count": store.count_lessons(),
        },
        context_cap=cap,
    )
    with store._db_lock:
        store._check_recall_query(query)
    return result
