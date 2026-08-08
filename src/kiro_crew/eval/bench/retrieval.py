"""The retrieval ruler: did the memory layer surface the evidence the question needed?

This is the primary metric of the whole harness, and the reason is determinism.
Kiro Crew cannot control sampling — ``temperature``, ``top_p`` and ``seed`` are not
threaded through the provider stack at all (the only sampling-adjacent knob is
``reasoning_effort``), so any end-to-end answer score is a random variable whose
noise must be beaten down with repetitions. Retrieval has no such problem: the
embedder is local and deterministic, and so is the ranker. A retrieval delta
between two commits is therefore an *exact* number, measurable in one pass, with
no reps and no confidence interval to argue about.

That makes this the right instrument for the question "did my fix help or hurt".
The end-to-end scorers answer a different, noisier question and cost far more.

Two granularities are reported because they fail differently. Session-level recall
asks whether the right *conversation* was reachable; turn-level asks whether the
right *utterance* ranked. A change that improves session recall while degrading
turn rank is a real and common outcome (diversity reranking does exactly this),
and a single blended number would hide it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

from .corpus import BenchInstance, BenchQuery, Corpus
from .ingest import EmbedFn, IngestedInstance

#: Reported cut-offs. 5 and 10 are the cut-offs LongMemEval's own retrieval
#: metrics use; 8 is included because it is ``VectorMemoryStore``'s default
#: ``episodic_limit``, i.e. the number of fragments production actually asks for.
DEFAULT_K_VALUES = (1, 3, 5, 8, 10, 20)


class RetrievalNotMeasurable(RuntimeError):
    """Raised rather than reporting a recall number that is trivially 1.0."""


@dataclass(frozen=True)
class RetrievalConfig:
    """``mmr`` and ``relevance_filter`` mirror ``search_episodic``'s own knobs.

    Both default to production behavior: MMR reranking is ON by default in the
    store (``_MMR_LAMBDA = 0.6``, mixing cosine relevance with pairwise Jaccard
    diversity), and ``relevance_filter`` is OFF by default so recency cannot be
    prevented from ordering a relevant match out of the set. Measuring anything
    other than the production defaults is legitimate but must be an explicit
    choice, so both appear in the report.
    """

    k_values: tuple[int, ...] = DEFAULT_K_VALUES
    mmr: bool = True
    relevance_filter: bool = False

    @property
    def limit(self) -> int:
        """Ask the store for as many fragments as the largest cut-off needs.

        Requesting exactly ``max(k)`` rather than more matters: MMR reranking is
        applied to the returned window, so the size of the request changes the
        composition of the result. Asking for 100 and slicing to 10 would not be
        the same measurement as asking for 10.
        """
        return max(self.k_values)

    def describe(self) -> dict[str, object]:
        return {
            "k_values": list(self.k_values),
            "mmr": self.mmr,
            "relevance_filter": self.relevance_filter,
            "limit": self.limit,
        }


@dataclass(frozen=True)
class QueryRetrieval:
    """One query's ranked results, already attributed back to corpus ids.

    ``retrieved_session_ids`` is distinct sessions in order of their best-ranked
    fragment. That is what a session-level cut-off means — "the top 5 sessions",
    not "the sessions appearing among the top 5 fragments" — and the two differ
    whenever one session supplies several of the leading hits.
    """

    query_id: str
    category: str
    raw_category: str
    unanswerable: bool
    gold_session_ids: tuple[str, ...]
    gold_turn_ids: tuple[str, ...]
    retrieved_session_ids: tuple[str, ...]
    retrieved_turn_ids: tuple[str, ...]
    unattributed_hits: int = 0


def _dcg(relevances: Sequence[int]) -> float:
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(relevances))


def ndcg_at_k(ranked: Sequence[str], gold: Sequence[str], k: int) -> float:
    """Binary-relevance nDCG@k.

    The ideal ranking places ``min(k, |gold|)`` relevant items first, so a query
    with more gold items than ``k`` is not penalised for the impossibility of
    fitting them all in the window.
    """
    if not gold:
        return 0.0
    goldset = set(gold)
    rels = [1 if item in goldset else 0 for item in ranked[:k]]
    ideal = [1] * min(k, len(goldset))
    idcg = _dcg(ideal)
    return (_dcg(rels) / idcg) if idcg else 0.0


def recall_all_at_k(ranked: Sequence[str], gold: Sequence[str], k: int) -> float:
    """1.0 only when EVERY gold item is inside the window.

    This is LongMemEval's ``recall_all@k`` and it is the strict, honest measure
    for multi-hop questions: surfacing three of four required sessions does not
    let a model answer the question, so scoring it 0.75 overstates the memory
    layer's usefulness. Reported alongside the micro variant, not instead of it.
    """
    if not gold:
        return 0.0
    return 1.0 if set(gold).issubset(set(ranked[:k])) else 0.0


def recall_any_at_k(ranked: Sequence[str], gold: Sequence[str], k: int) -> float:
    if not gold:
        return 0.0
    return 1.0 if set(gold) & set(ranked[:k]) else 0.0


def recall_micro_at_k(ranked: Sequence[str], gold: Sequence[str], k: int) -> float:
    if not gold:
        return 0.0
    goldset = set(gold)
    return len(goldset & set(ranked[:k])) / len(goldset)


def corpus_has_distractors(corpus: Corpus) -> tuple[bool, str]:
    """Whether queries face a haystack containing sessions that are not their gold.

    A corpus that fails this cannot measure retrieval: if a question's haystack
    contains nothing but its own evidence, then any ranking whatsoever achieves
    perfect recall, and the resulting number describes the dataset rather than the
    system. This is not hypothetical — LongMemEval's ``oracle`` variant satisfies
    ``set(gold_sessions) == set(all_sessions)`` for 500 of 500 instances, and it is
    the smallest and therefore most tempting variant to reach for.

    **The test is per QUERY, not per instance.** An earlier version unioned gold
    across an instance's queries, which is wrong for any multi-query dataset and
    produced a false refusal on the whole of LoCoMo: each conversation carries ~199
    questions against 19–32 sessions, so the union of their gold sets covers every
    session. That means "every session is evidence for *some* question", which says
    nothing about whether an *individual* question faces distractors — and it does,
    16–29 of them. The bug also hid itself on small slices, where too few queries
    were present to cover the haystack, so a subset run passed while the full
    corpus was refused.

    Returns the verdict plus a message suitable for a refusal.
    """
    total = 0
    with_distractors = 0
    for inst in corpus.instances:
        all_sessions = {s.session_id for s in inst.sessions}
        for query in inst.queries:
            gold = set(query.gold_session_ids)
            if not gold:
                continue
            total += 1
            if all_sessions - gold:
                with_distractors += 1
    if total == 0:
        return False, (
            f"corpus {corpus.name}/{corpus.variant} has no query with resolvable "
            "gold sessions, so retrieval cannot be scored against it"
        )
    if with_distractors == 0:
        return False, (
            f"corpus {corpus.name}/{corpus.variant} is evidence-only: every "
            f"haystack session is gold for its own query in all {total} scorable "
            "queries, so recall is trivially 1.0 for any ranking. This measures "
            "reading comprehension, not retrieval — use a variant with distractors "
            "(e.g. longmemeval_s instead of longmemeval_oracle)."
        )
    return True, (
        f"{with_distractors}/{total} scorable queries face a haystack with "
        "distractor sessions"
    )


def retrieve_for_query(
    loaded: IngestedInstance,
    query: BenchQuery,
    *,
    embed_fn: EmbedFn,
    config: RetrievalConfig,
) -> QueryRetrieval:
    """Run one question through the real ``search_episodic`` and attribute the hits.

    The query text is embedded and passed as ``query_embedding`` *and* as
    ``query_text``, which is what the production read path does: the text is used
    by the FTS5 fallback and by MMR's token-level diversity term, so omitting it
    would measure a configuration production never runs.
    """
    hits = loaded.store.search_episodic(
        query_embedding=embed_fn(query.question),
        query_text=query.question,
        limit=config.limit,
        mmr=config.mmr,
        relevance_filter=config.relevance_filter,
    )

    session_order: list[str] = []
    turn_order: list[str] = []
    unattributed = 0
    for hit in hits:
        sid = str(hit.get("conversation_id") or "")
        if sid and sid not in session_order:
            session_order.append(sid)
        tid = loaded.text_to_turn.get(str(hit.get("text") or ""))
        if tid is None:
            # A hit whose text is not in the map is a row this harness did not
            # write, or one whose text the store rewrote. Counted rather than
            # dropped silently: it would otherwise depress turn-level recall for a
            # reason invisible in the output.
            unattributed += 1
        elif tid not in turn_order:
            turn_order.append(tid)

    return QueryRetrieval(
        query_id=query.query_id,
        category=query.category,
        raw_category=query.raw_category,
        unanswerable=query.unanswerable,
        gold_session_ids=query.gold_session_ids,
        gold_turn_ids=query.gold_turn_ids,
        retrieved_session_ids=tuple(session_order),
        retrieved_turn_ids=tuple(turn_order),
        unattributed_hits=unattributed,
    )


def retrieve_for_instance(
    loaded: IngestedInstance,
    *,
    embed_fn: EmbedFn,
    config: RetrievalConfig,
) -> list[QueryRetrieval]:
    """Every scorable query in one instance. Unscorable ones are skipped, not zeroed.

    A query whose gold set is empty after :meth:`BenchInstance.resolve_gold` has no
    ground truth in this haystack — 4 LoCoMo items ship an empty evidence list and
    7 of its evidence refs dangle. Counting those as misses would measure the
    dataset's bookkeeping, so they are excluded here and their count is surfaced by
    :func:`aggregate`.
    """
    return [
        retrieve_for_query(loaded, q, embed_fn=embed_fn, config=config)
        for q in loaded.instance.queries
        if q.scorable_retrieval
    ]


@dataclass
class RetrievalAggregate:
    """Means over queries, with the denominators kept visible.

    ``skipped_unscorable`` and ``unattributed_hits`` exist so a number can never be
    read as more complete than it is: an aggregate over 1 500 of 1 986 questions is
    a different claim from an aggregate over all of them, and a harness that prints
    only the mean invites the wrong one.
    """

    scored_queries: int = 0
    skipped_unscorable: int = 0
    unattributed_hits: int = 0
    session: dict[str, float] = field(default_factory=dict)
    turn: dict[str, float] = field(default_factory=dict)
    #: cut-off -> how many queries that cut-off was measurable on. A cut-off whose
    #: count is 0 was never observable from the fragment window and is absent from
    #: the metric dicts entirely, rather than present with a falsely-lowered value.
    session_measurable: dict[int, int] = field(default_factory=dict)
    turn_measurable: dict[int, int] = field(default_factory=dict)
    by_category: dict[str, dict[str, float]] = field(default_factory=dict)
    unanswerable_queries: int = 0

    def headline(self, k: int = 5) -> float:
        """The single number to watch: strict session recall at k.

        Strict (``recall_all``) rather than ``any`` because a partially-retrieved
        multi-hop evidence set does not let the model answer, and session rather
        than turn because it is the level production actually injects at.
        """
        return self.session.get(f"recall_all@{k}", 0.0)


def _metric_block(
    results: Sequence[QueryRetrieval],
    k_values: Sequence[int],
    level: str,
) -> tuple[dict[str, float], dict[int, int]]:
    """Metrics at each cut-off, plus how many queries each cut-off was measurable on.

    The measurability filter is the load-bearing part, and it exists because a
    session-level cut-off is NOT free to choose. Retrieval asks the store for
    ``limit`` *fragments*; the distinct sessions among them are however many they
    happen to span. Measured on LoCoMo with a 20-fragment window: 8-14 distinct
    sessions per query (median 12). So "the top 20 sessions" is not observable
    from that window for ANY query, and computing it anyway silently reports a
    falsely-lowered recall -- the metric would be bounded by the window rather
    than by the ranker.

    Enlarging the window is not the fix: MMR reranking is applied to the returned
    set, so asking for more fragments changes the composition of the result and
    therefore measures a different configuration than the one production runs.

    A cut-off is therefore counted only for queries whose observed ranked list has
    at least ``k`` entries, and the surviving count is returned so the report can
    print the denominator instead of implying the full corpus.
    """
    out: dict[str, float] = {}
    measurable: dict[int, int] = {}
    if not results:
        return out, measurable

    for k in k_values:
        eligible = []
        for r in results:
            ranked = r.retrieved_session_ids if level == "session" else r.retrieved_turn_ids
            gold = r.gold_session_ids if level == "session" else r.gold_turn_ids
            if not gold:
                continue
            if len(ranked) < k:
                # The window never exposed k distinct items, so "top k" is not
                # observable here. Excluded rather than scored as a partial miss.
                continue
            eligible.append((ranked, gold))
        measurable[k] = len(eligible)
        if not eligible:
            continue
        for name, fn in (
            ("recall_all", recall_all_at_k),
            ("recall_any", recall_any_at_k),
            ("recall_micro", recall_micro_at_k),
            ("ndcg", ndcg_at_k),
        ):
            vals = [fn(ranked, gold, k) for ranked, gold in eligible]
            out[f"{name}@{k}"] = sum(vals) / len(vals)
    return out, measurable


def aggregate(
    results: Sequence[QueryRetrieval],
    *,
    instances: Sequence[BenchInstance] = (),
    k_values: Sequence[int] = DEFAULT_K_VALUES,
) -> RetrievalAggregate:
    agg = RetrievalAggregate(
        scored_queries=len(results),
        unattributed_hits=sum(r.unattributed_hits for r in results),
        unanswerable_queries=sum(1 for r in results if r.unanswerable),
    )
    agg.skipped_unscorable = sum(
        1 for inst in instances for q in inst.queries if not q.scorable_retrieval
    )
    agg.session, agg.session_measurable = _metric_block(results, k_values, "session")
    agg.turn, agg.turn_measurable = _metric_block(results, k_values, "turn")

    buckets: dict[str, list[QueryRetrieval]] = {}
    for r in results:
        buckets.setdefault(r.category, []).append(r)
    # Per-category session metrics only: the turn-level block doubles the output
    # size for a signal that is dominated by the session-level one at this
    # granularity, and the full turn block is still available in aggregate.turn.
    agg.by_category = {
        cat: _metric_block(rs, k_values, "session")[0] for cat, rs in sorted(buckets.items())
    }
    return agg
