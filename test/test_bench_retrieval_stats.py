"""Retrieval metric maths, the measurability guard, and the A/B statistics.

Split into three parts because they need three different amounts of machinery.
The metric functions are pure and are tested on hand-built ranked lists. The
measurability guard is pure too but reasons over a whole corpus. The end-to-end
section is the only part that needs a real store, and it uses the toy embedder so
it runs in milliseconds with no model and no network.

The two assertions worth the most are in the end-to-end section. First, that an
unscorable query is *skipped* rather than scored zero — the difference between
measuring the memory layer and measuring the dataset's bookkeeping. Second, that
``run_retrieval`` is bit-for-bit reproducible: the entire design rests on
retrieval being deterministic (local embedder, deterministic ranker, no sampling),
because that is what licenses an exact delta from a single pass with no
repetitions and no confidence interval. If that ever stops being true, every
"exact delta" claim in the reports becomes false and nothing else would notice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.eval.bench.corpus import (
    CAT_MULTI_HOP,
    CAT_SINGLE_HOP,
    BenchInstance,
    BenchQuery,
    BenchSession,
    BenchTurn,
    Corpus,
)
from kiro_crew.eval.bench.ingest import IngestConfig, ingest_instance
from kiro_crew.eval.bench.retrieval import (
    RetrievalConfig,
    RetrievalNotMeasurable,
    aggregate,
    corpus_has_distractors,
    ndcg_at_k,
    recall_all_at_k,
    recall_any_at_k,
    recall_micro_at_k,
    retrieve_for_instance,
    retrieve_for_query,
)
from kiro_crew.eval.bench.run import run_retrieval
from kiro_crew.eval.bench.stats import (
    MIN_REPS,
    ArmResult,
    Comparison,
    compare_interleaved,
    measure_arm,
    noise_band_from,
    sensitivity_check,
)
from kiro_crew.eval.bench.toy_embedder import TOY_EMBEDDER_ID, toy_embed_fn

EMBED = toy_embed_fn()


# ════════════════════════════════════════════════════════════════════════════
# nDCG
# ════════════════════════════════════════════════════════════════════════════


def test_ndcg_is_one_for_a_perfect_ranking() -> None:
    assert ndcg_at_k(["a", "b", "c", "d"], ["a", "b"], 4) == 1.0


def test_ndcg_is_strictly_lower_for_a_reversed_ranking() -> None:
    """Position matters: nDCG is the only reported metric that can see rank order.

    recall@k is blind inside the window, so a change that reorders the window
    without changing its membership is invisible to it and visible here.
    """
    perfect = ndcg_at_k(["a", "b", "c", "d"], ["a", "b"], 4)
    reversed_ = ndcg_at_k(["c", "d", "b", "a"], ["a", "b"], 4)
    assert reversed_ < perfect
    assert 0.0 < reversed_ < 1.0


def test_ndcg_does_not_penalise_gold_larger_than_k() -> None:
    """Three golds and a window of two: filling both slots is a perfect score.

    The ideal DCG is built from ``min(k, |gold|)`` relevant items, so a multi-hop
    question needing more evidence than the cut-off admits is not scored against an
    impossibility. Without this the reported ceiling for such queries would be
    ``k / |gold|`` and every multi-hop number would read as a failure.
    """
    assert ndcg_at_k(["g1", "g2", "x"], ["g1", "g2", "g3"], 2) == 1.0


def test_ndcg_ignores_hits_beyond_the_cut_off() -> None:
    assert ndcg_at_k(["x", "y", "g1"], ["g1"], 2) == 0.0


def test_ndcg_deduplicates_gold_when_building_the_ideal() -> None:
    """The ideal is built from the gold *set*, so a repeated ref cannot inflate it."""
    assert ndcg_at_k(["g1", "x"], ["g1", "g1"], 2) == 1.0


# ════════════════════════════════════════════════════════════════════════════
# The three recalls — the whole point is that they disagree
# ════════════════════════════════════════════════════════════════════════════


def test_the_three_recalls_split_a_partially_retrieved_two_gold_case() -> None:
    """0.0 / 1.0 / 0.5 on the same ranked list, and each is the honest answer.

    ``recall_all`` says the model cannot answer this multi-hop question, which is
    true. ``recall_any`` says the memory layer surfaced relevant evidence, which is
    also true. ``recall_micro`` says half the evidence arrived, which is the number
    that moves smoothly as ranking improves. Reporting only one of the three is how
    a harness either overstates usefulness or hides progress.
    """
    ranked, gold, k = ["g1", "x"], ["g1", "g2"], 2
    assert recall_all_at_k(ranked, gold, k) == 0.0
    assert recall_any_at_k(ranked, gold, k) == 1.0
    assert recall_micro_at_k(ranked, gold, k) == 0.5


def test_all_three_agree_when_every_gold_is_inside_the_window() -> None:
    ranked, gold, k = ["g1", "g2", "x"], ["g1", "g2"], 3
    assert recall_all_at_k(ranked, gold, k) == 1.0
    assert recall_any_at_k(ranked, gold, k) == 1.0
    assert recall_micro_at_k(ranked, gold, k) == 1.0


def test_all_three_agree_when_nothing_relevant_is_retrieved() -> None:
    ranked, gold, k = ["x", "y"], ["g1", "g2"], 2
    assert recall_all_at_k(ranked, gold, k) == 0.0
    assert recall_any_at_k(ranked, gold, k) == 0.0
    assert recall_micro_at_k(ranked, gold, k) == 0.0


def test_recall_respects_the_cut_off_and_not_the_full_ranking() -> None:
    ranked, gold = ["x", "g1"], ["g1"]
    assert recall_any_at_k(ranked, gold, 1) == 0.0
    assert recall_any_at_k(ranked, gold, 2) == 1.0


@pytest.mark.parametrize("fn", [ndcg_at_k, recall_all_at_k, recall_any_at_k, recall_micro_at_k])
def test_empty_gold_returns_zero_from_every_metric_without_dividing_by_zero(fn: object) -> None:
    """Empty gold reaches these functions only through a bug upstream; do not crash.

    ``recall_all``'s ``issubset`` and ``ndcg``'s ``idcg`` would both otherwise
    report 1.0 or raise for a query with no ground truth, and either would be worse
    than the explicit 0.0 that the aggregator filters out anyway.
    """
    assert fn(["a", "b"], [], 5) == 0.0  # type: ignore[operator]
    assert fn([], [], 5) == 0.0  # type: ignore[operator]


@pytest.mark.parametrize("fn", [ndcg_at_k, recall_all_at_k, recall_any_at_k, recall_micro_at_k])
def test_empty_ranking_with_real_gold_scores_zero(fn: object) -> None:
    assert fn([], ["g1"], 5) == 0.0  # type: ignore[operator]


# ════════════════════════════════════════════════════════════════════════════
# corpus_has_distractors — the refusal that stops a meaningless number
# ════════════════════════════════════════════════════════════════════════════


def _turn(turn_id: str, session_id: str, text: str, speaker: str = "Alice") -> BenchTurn:
    return BenchTurn(turn_id=turn_id, session_id=session_id, speaker=speaker, text=text)


def _session(session_id: str, text: str) -> BenchSession:
    return BenchSession(session_id, (_turn(f"{session_id}#t0", session_id, text),))


def _query(query_id: str = "q1", **kw: object) -> BenchQuery:
    base: dict[str, object] = {
        "query_id": query_id,
        "question": "what happened?",
        "category": CAT_SINGLE_HOP,
    }
    base.update(kw)
    return BenchQuery(**base)  # type: ignore[arg-type]


def test_evidence_only_corpus_is_refused_and_names_the_variant_that_fixes_it() -> None:
    """The refusal has to be actionable, because it is the whole point of the guard.

    ``longmemeval_oracle`` satisfies ``gold_sessions == all_sessions`` for 500/500
    instances, so any ranking scores 1.0. Saying "cannot measure retrieval" without
    naming ``longmemeval_s`` leaves the reader with a dead end and a tempting
    ``force_no_distractors`` flag.
    """
    corpus = Corpus(
        "longmemeval",
        "oracle",
        (
            BenchInstance(
                "i1",
                (_session("s1", "the scuba lessons in Malta"),),
                (_query(gold_session_ids=("s1",)),),
            ),
        ),
    )
    ok, why = corpus_has_distractors(corpus)
    assert ok is False
    assert "evidence-only" in why
    assert "trivially 1.0" in why
    assert "longmemeval_s instead of longmemeval_oracle" in why


def test_one_distractor_session_is_enough_to_make_a_corpus_measurable() -> None:
    corpus = Corpus(
        "longmemeval",
        "s_cleaned",
        (
            BenchInstance(
                "i1",
                (
                    _session("s1", "the scuba lessons in Malta"),
                    _session("s2", "an unrelated session about bread"),
                ),
                (_query(gold_session_ids=("s1",)),),
            ),
        ),
    )
    ok, why = corpus_has_distractors(corpus)
    assert ok is True
    assert "1/1 scorable queries face a haystack with distractor sessions" in why


def test_a_corpus_with_no_resolvable_gold_is_refused_for_a_different_reason() -> None:
    """Distinct message: nothing to score against is not the same as nothing to rank.

    Conflating the two would send someone looking for a bigger haystack when the
    real problem is that ``resolve_gold`` emptied every evidence set.
    """
    corpus = Corpus(
        "locomo",
        "locomo10",
        (
            BenchInstance(
                "i1",
                (_session("s1", "the scuba lessons in Malta"),),
                (_query(gold_turn_ids=()),),
            ),
        ),
    )
    ok, why = corpus_has_distractors(corpus)
    assert ok is False
    assert "no query with resolvable gold sessions" in why
    assert "evidence-only" not in why


def test_instances_without_gold_are_not_counted_in_the_denominator() -> None:
    """One measurable instance beside one ungoldened one is still measurable."""
    corpus = Corpus(
        "mixed",
        "v0",
        (
            BenchInstance("i0", (_session("a1", "no gold anywhere in here"),), (_query("q0"),)),
            BenchInstance(
                "i1",
                (
                    _session("s1", "the scuba lessons in Malta"),
                    _session("s2", "an unrelated session about bread"),
                ),
                (_query("q1", gold_session_ids=("s1",)),),
            ),
        ),
    )
    ok, why = corpus_has_distractors(corpus)
    assert ok is True
    assert "1/1 scorable queries" in why


def test_run_retrieval_refuses_an_evidence_only_corpus(tmp_path: Path) -> None:
    """The guard is wired into the runner, not merely available to it."""
    corpus = Corpus(
        "longmemeval",
        "oracle",
        (
            BenchInstance(
                "i1",
                (_session("s1", "the scuba diving lessons in Malta"),),
                (_query(gold_session_ids=("s1",)),),
            ),
        ),
    )
    with pytest.raises(RetrievalNotMeasurable, match="evidence-only"):
        run_retrieval(corpus, embed_fn=EMBED, embedder_id=TOY_EMBEDDER_ID,
                      store_root=tmp_path / "refused")


# ════════════════════════════════════════════════════════════════════════════
# End to end: a real store, the toy embedder
# ════════════════════════════════════════════════════════════════════════════

_RELEVANT = "we finally booked the scuba diving lessons in Malta for the June holiday"
_DISTRACTORS = (
    "the espresso machine needs a replacement pressure gasket before Tuesday",
    "planting heirloom tomatoes along the south balcony railing this weekend",
    "the rescue greyhound named Pip has settled into the flat surprisingly well",
    "rewiring the garage lighting circuit took most of Saturday afternoon",
    "the quarterly budget spreadsheet still refuses to reconcile by nine pounds",
)
_QUESTION = "where did they book the scuba diving lessons?"


def _needle_instance(*, with_unscorable: bool = False) -> BenchInstance:
    """One clearly-relevant session among several clearly-irrelevant ones.

    Every session's text differs well inside its first 80 characters: the store
    dedups unconditionally on ``LOWER(SUBSTR(text, 1, 80))``, so fixtures that
    share an opening would collapse into one row and the retrieval assertion would
    be measuring the fixture.
    """
    sessions = (_session("gold", _RELEVANT),) + tuple(
        _session(f"noise{i}", text) for i, text in enumerate(_DISTRACTORS)
    )
    queries: tuple[BenchQuery, ...] = (
        _query(
            "q_scorable",
            question=_QUESTION,
            gold_session_ids=("gold",),
            gold_turn_ids=("gold#t0",),
        ),
    )
    if with_unscorable:
        queries += (
            _query("q_unscorable", question="what colour was the car?", category=CAT_MULTI_HOP),
        )
    return BenchInstance("needle", sessions, queries)


def _needle_corpus() -> Corpus:
    return Corpus("toy", "needle", (_needle_instance(with_unscorable=True),))


def test_the_relevant_session_is_retrieved_at_k_five(tmp_path: Path) -> None:
    """The end-to-end confidence check: the ruler can find a needle it was given.

    Toy-embedder lexical overlap is enough here on purpose — the assertion is that
    ingest, the store's ranking, and the attribution back to corpus ids all line
    up, not that the embedder is good.
    """
    loaded = ingest_instance(
        _needle_instance(), db_path=tmp_path / "needle.db", embed_fn=EMBED,
        config=IngestConfig(timeline="now"),
    )
    try:
        result = retrieve_for_query(
            loaded,
            loaded.instance.queries[0],
            embed_fn=EMBED,
            config=RetrievalConfig(),
        )
        assert "gold" in result.retrieved_session_ids[:5]
        assert recall_all_at_k(result.retrieved_session_ids, result.gold_session_ids, 5) == 1.0
        assert result.retrieved_session_ids[0] == "gold"
        # Turn-level attribution resolved through text_to_turn, not through tags.
        assert "gold#t0" in result.retrieved_turn_ids
        assert result.unattributed_hits == 0
    finally:
        loaded.close()


def test_retrieve_for_instance_skips_an_unscorable_query_rather_than_scoring_it(
    tmp_path: Path,
) -> None:
    """A query with no resolvable gold is excluded, never counted as a miss.

    Scoring it zero would drag the reported mean down by an amount that depends on
    how much dangling bookkeeping the dataset shipped — 4 empty evidence lists and
    7 dangling refs in LoCoMo — which is a property of the file, not the system.
    """
    inst = _needle_instance(with_unscorable=True)
    loaded = ingest_instance(
        inst, db_path=tmp_path / "skip.db", embed_fn=EMBED,
        config=IngestConfig(timeline="now"),
    )
    try:
        results = retrieve_for_instance(loaded, embed_fn=EMBED, config=RetrievalConfig())
        assert [r.query_id for r in results] == ["q_scorable"]

        agg = aggregate(results, instances=(inst,), k_values=(5,))
        assert agg.scored_queries == 1
        assert agg.skipped_unscorable == 1  # visible in the denominator, not hidden
        assert agg.session["recall_all@5"] == 1.0
    finally:
        loaded.close()


def test_run_retrieval_is_deterministic_over_the_whole_metrics_block(
    tmp_path: Path,
) -> None:
    """The load-bearing claim of the entire design, asserted directly.

    Retrieval is measured once and its delta reported as exact — no reps, no
    confidence interval — and that is only legitimate if two runs over the same
    corpus with the same embedder agree completely. So the assertion is dict
    equality over the whole session block, not a tolerance on one headline number.

    Distinct ``store_root`` per run on purpose: reusing one would ingest into an
    existing db, where the text-prefix dedup drops every fragment as a duplicate
    and the second run would measure an empty store.
    """
    corpus = _needle_corpus()
    first = run_retrieval(
        corpus,
        embed_fn=EMBED,
        embedder_id=TOY_EMBEDDER_ID,
        store_root=tmp_path / "run_a",
        ingest_config=IngestConfig(timeline="now"),
    )
    second = run_retrieval(
        corpus,
        embed_fn=EMBED,
        embedder_id=TOY_EMBEDDER_ID,
        store_root=tmp_path / "run_b",
        ingest_config=IngestConfig(timeline="now"),
    )

    assert first.metrics.session == second.metrics.session
    assert first.metrics.turn == second.metrics.turn
    assert first.metrics.by_category == second.metrics.by_category
    assert first.corpus_fingerprint == second.corpus_fingerprint
    # And the run actually measured something, so equality is not equality of {}.
    assert first.metrics.scored_queries == 1
    assert first.headline(5) == 1.0


def test_unattributed_hits_are_counted_rather_than_depressing_turn_recall(
    tmp_path: Path,
) -> None:
    """A row the harness did not write must be visible, not silently a turn miss.

    ``text_to_turn`` is keyed on fragment text, so any row the harness did not
    write cannot be attributed to a turn. Dropping such hits silently would make
    turn-level recall drift downward for a reason invisible in the output — exactly
    the class of error a benchmark cannot afford, because the number still looks
    plausible.
    """
    loaded = ingest_instance(
        _needle_instance(), db_path=tmp_path / "extra.db", embed_fn=EMBED,
        config=IngestConfig(timeline="now"),
    )
    try:
        foreign = "an interloping row about scuba diving that the harness never wrote"
        assert loaded.store.write_episodic(
            foreign,
            embedding=EMBED(foreign),
            conversation_id="gold",
            tags=["bench"],
            importance=0.5,
            source="not-the-benchmark",
        )
        assert foreign not in loaded.text_to_turn

        result = retrieve_for_query(
            loaded,
            loaded.instance.queries[0],
            embed_fn=EMBED,
            config=RetrievalConfig(),
        )
        assert result.unattributed_hits == 1
        # The real gold turn still resolves, so recall is not damaged by the ghost.
        assert "gold#t0" in result.retrieved_turn_ids
        agg = aggregate([result], k_values=(5,))
        assert agg.unattributed_hits == 1
        assert agg.turn["recall_any@5"] == 1.0
    finally:
        loaded.close()


def test_retrieved_sessions_are_distinct_and_ordered_by_their_best_hit(
    tmp_path: Path,
) -> None:
    """"Top 5 sessions" is not "sessions among the top 5 fragments"."""
    inst = BenchInstance(
        "multi",
        (
            BenchSession(
                "gold",
                (
                    _turn("g1", "gold", _RELEVANT),
                    _turn("g2", "gold", "the Malta scuba trip was booked for June", "Bob"),
                ),
            ),
            _session("noise0", _DISTRACTORS[0]),
        ),
        (_query("q1", question=_QUESTION, gold_session_ids=("gold",)),),
    )
    loaded = ingest_instance(
        inst, db_path=tmp_path / "multi.db", embed_fn=EMBED,
        config=IngestConfig(timeline="now"),
    )
    try:
        result = retrieve_for_query(
            loaded, inst.queries[0], embed_fn=EMBED, config=RetrievalConfig()
        )
        assert result.retrieved_session_ids.count("gold") == 1
        assert result.retrieved_session_ids[0] == "gold"
        assert len(result.retrieved_turn_ids) >= 2
    finally:
        loaded.close()


def test_retrieval_config_limit_is_the_largest_cut_off() -> None:
    """Asking for more and slicing would change what MMR reranked, so it must not."""
    cfg = RetrievalConfig(k_values=(1, 3, 5))
    assert cfg.limit == 5
    described = cfg.describe()
    assert described == {"k_values": [1, 3, 5], "mmr": True, "relevance_filter": False, "limit": 5}


def test_retrieval_defaults_mirror_the_production_read_path() -> None:
    cfg = RetrievalConfig()
    assert cfg.mmr is True
    assert cfg.relevance_filter is False
    assert 8 in cfg.k_values  # the store's own default episodic_limit


# ════════════════════════════════════════════════════════════════════════════
# stats.py
# ════════════════════════════════════════════════════════════════════════════


def test_arm_result_refuses_a_deterministic_arm_with_differing_values() -> None:
    """Averaging the discrepancy away would hide a broken isolation between arms.

    Two different values from a metric declared exact means either the metric is
    not deterministic or the arms leaked into each other. Both invalidate the
    comparison, so this raises instead of quietly reporting a median.
    """
    with pytest.raises(ValueError) as exc:
        ArmResult(name="baseline", metric="recall_all@5", values=(0.4, 0.5), deterministic=True)
    msg = str(exc.value)
    assert "declared deterministic" in msg
    assert "2 distinct values" in msg
    assert "refuses rather" in msg


def test_arm_result_accepts_repeated_identical_values_when_deterministic() -> None:
    arm = ArmResult(name="b", metric="m", values=(0.5, 0.5, 0.5), deterministic=True)
    assert arm.center == 0.5
    assert arm.spread == 0.0


def test_arm_result_requires_at_least_one_measurement() -> None:
    with pytest.raises(ValueError, match="has no measurements"):
        ArmResult(name="b", metric="m", values=(), deterministic=True)


def test_arm_center_is_the_median_not_the_mean() -> None:
    """Constructed so the two differ: median 0.20, mean 0.40.

    One unlucky rep — a provider hiccup, a host under load — must not move the
    result, which is the reason measurer.py insists on the median and the reason
    copying that choice here was the point of reusing its protocol.
    """
    arm = ArmResult(name="b", metric="m", values=(0.1, 0.2, 0.9), deterministic=False)
    assert arm.center == 0.2
    assert arm.center != sum(arm.values) / len(arm.values)


def test_arm_spread_is_zero_for_a_deterministic_arm_and_half_the_range_otherwise() -> None:
    noisy = ArmResult(name="b", metric="m", values=(0.4, 0.6), deterministic=False)
    assert noisy.spread == pytest.approx(0.1)
    # A deterministic arm cannot hold differing values at all, so its spread is 0.
    assert ArmResult(name="b", metric="m", values=(0.4, 0.4), deterministic=True).spread == 0.0
    single = ArmResult(name="b", metric="m", values=(0.4,), deterministic=False)
    assert single.spread == 0.0


def test_noise_band_is_two_sigma_and_refuses_to_invent_one_from_a_single_sample() -> None:
    assert noise_band_from([0.5]) == 0.0
    assert noise_band_from([]) == 0.0
    assert noise_band_from([0.4, 0.6]) == pytest.approx(2.0 * 0.1414213562, rel=1e-6)


# ── compare_interleaved: the call pattern IS the property ─────────────────────


class _Counter:
    """Records both how many times each arm ran and in what order."""

    def __init__(self, log: list[str], label: str, values: list[float] | None = None) -> None:
        self.log = log
        self.label = label
        self.values = values
        self.calls = 0

    def __call__(self) -> float:
        self.log.append(self.label)
        value = self.values[self.calls % len(self.values)] if self.values else 0.5
        self.calls += 1
        return value


def test_deterministic_comparison_calls_each_arm_exactly_once() -> None:
    """Repeating an exact computation buys nothing and reads as false precision."""
    log: list[str] = []
    base = _Counter(log, "baseline", [0.40])
    cand = _Counter(log, "candidate", [0.55])

    cmp_ = compare_interleaved(
        "recall_all@5", base, cand, deterministic=True, reps=5, warmups=3
    )

    assert (base.calls, cand.calls) == (1, 1)
    assert log == ["baseline", "candidate"]
    assert cmp_.noise_band == 0.0
    assert cmp_.deterministic is True
    assert cmp_.delta == pytest.approx(0.15)
    assert cmp_.verdict == "improved"
    assert any("no band" in n for n in cmp_.notes)
    assert "exact (deterministic metric)" in cmp_.summary()


def test_stochastic_comparison_alternates_baseline_and_candidate() -> None:
    """Assert the alternation itself, not just the counts.

    Interleaving is the mechanism that makes drift cancel in the paired delta. An
    implementation that ran all of arm A then all of arm B would produce identical
    call *counts* and let a host slowing mid-run land entirely on one arm — which
    is indistinguishable from the effect being measured. So the order is what gets
    pinned.
    """
    log: list[str] = []
    base = _Counter(log, "baseline", [0.40])
    cand = _Counter(log, "candidate", [0.50])

    compare_interleaved(
        "recall_all@5", base, cand, deterministic=False, reps=3, warmups=1
    )

    # 1 warmup pair + 3 measured pairs, strictly alternating throughout.
    assert log == ["baseline", "candidate"] * 4
    assert (base.calls, cand.calls) == (4, 4)


def test_warmups_are_discarded_and_not_part_of_the_values() -> None:
    """A cold first call carries process-start and model-load cost, not the effect."""
    log: list[str] = []
    base = _Counter(log, "baseline", [9.0, 0.4, 0.4])
    cand = _Counter(log, "candidate", [9.0, 0.5, 0.5])
    cmp_ = compare_interleaved("m", base, cand, deterministic=False, reps=2, warmups=1)
    assert cmp_.baseline.values == (0.4, 0.4)
    assert cmp_.candidate.values == (0.5, 0.5)
    assert 9.0 not in cmp_.baseline.values


def test_reps_are_floored_at_the_minimum() -> None:
    """A single-rep stochastic arm has no spread and cannot be trusted."""
    log: list[str] = []
    base, cand = _Counter(log, "b"), _Counter(log, "c")
    cmp_ = compare_interleaved("m", base, cand, deterministic=False, reps=1, warmups=0)
    assert len(cmp_.baseline.values) == MIN_REPS >= 2
    assert base.calls == MIN_REPS


def test_zero_spread_baseline_is_flagged_as_suspicious_not_as_precision() -> None:
    """band == 0 makes every non-zero delta read conclusive; say so in the notes."""
    log: list[str] = []
    cmp_ = compare_interleaved(
        "m", _Counter(log, "b", [0.4]), _Counter(log, "c", [0.5]),
        deterministic=False, reps=2, warmups=0,
    )
    assert cmp_.noise_band == 0.0
    assert any("treat with suspicion" in n for n in cmp_.notes)


def test_measure_arm_skips_warmups_entirely_for_a_deterministic_metric() -> None:
    log: list[str] = []
    run = _Counter(log, "run", [0.42])
    arm = measure_arm("a", "m", run, deterministic=True, reps=9, warmups=4)
    assert run.calls == 1
    assert arm.values == (0.42,)
    assert arm.deterministic is True


# ── verdict ──────────────────────────────────────────────────────────────────


def _cmp(
    base_vals: tuple[float, ...],
    cand_vals: tuple[float, ...],
    *,
    deterministic: bool,
    band: float,
    higher_is_better: bool = True,
) -> Comparison:
    return Comparison(
        metric="recall_all@5",
        baseline=ArmResult("baseline", "recall_all@5", base_vals, deterministic),
        candidate=ArmResult("candidate", "recall_all@5", cand_vals, deterministic),
        noise_band=band,
        higher_is_better=higher_is_better,
    )


def test_verdict_is_unchanged_for_a_zero_delta() -> None:
    assert _cmp((0.5,), (0.5,), deterministic=True, band=0.0).verdict == "unchanged"


def test_verdict_is_inconclusive_when_a_noisy_delta_sits_inside_the_band() -> None:
    cmp_ = _cmp((0.40, 0.60), (0.42, 0.62), deterministic=False, band=0.10)
    assert cmp_.delta == pytest.approx(0.02)
    assert abs(cmp_.delta) <= cmp_.noise_band
    assert cmp_.verdict == "inconclusive"


def test_unchanged_and_inconclusive_are_different_words_for_a_noisy_arm() -> None:
    """The distinction licenses different conclusions, so it must not collapse.

    "Unchanged" invites "the fix did nothing, drop it". "Inconclusive" says the fix
    may have helped by less than this instrument can resolve — which is a reason to
    get a better instrument, not to discard the fix. Both arms here are noisy and
    both deltas are inside the band; only one of them is exactly zero.
    """
    zero = _cmp((0.40, 0.60), (0.40, 0.60), deterministic=False, band=0.10)
    tiny = _cmp((0.40, 0.60), (0.41, 0.61), deterministic=False, band=0.10)
    assert zero.verdict == "unchanged"
    assert tiny.verdict == "inconclusive"
    assert zero.verdict != tiny.verdict


def test_a_deterministic_delta_is_never_inconclusive_however_small() -> None:
    """An exact metric has no band to hide inside; a 0.0001 delta is a real one."""
    cmp_ = _cmp((0.5000,), (0.5001,), deterministic=True, band=0.10)
    assert cmp_.deterministic is True
    assert cmp_.verdict == "improved"


def test_verdict_outside_the_band_reports_improved_or_regressed() -> None:
    up = _cmp((0.40, 0.60), (0.70, 0.90), deterministic=False, band=0.10)
    down = _cmp((0.70, 0.90), (0.40, 0.60), deterministic=False, band=0.10)
    assert up.verdict == "improved"
    assert down.verdict == "regressed"


def test_higher_is_better_false_flips_improved_and_regressed() -> None:
    """The same protocol has to serve a latency metric, where down is the win."""
    faster = _cmp((0.90,), (0.40,), deterministic=True, band=0.0, higher_is_better=False)
    slower = _cmp((0.40,), (0.90,), deterministic=True, band=0.0, higher_is_better=False)
    assert faster.verdict == "improved"
    assert slower.verdict == "regressed"
    # And the sign of the delta is unaffected by the direction of "better".
    assert faster.delta < 0 < slower.delta


def test_relative_is_none_rather_than_a_division_by_zero_baseline() -> None:
    assert _cmp((0.0,), (0.5,), deterministic=True, band=0.0).relative is None
    assert _cmp((0.4,), (0.5,), deterministic=True, band=0.0).relative == pytest.approx(0.25)


def test_summary_states_the_verdict_and_the_band_it_was_judged_against() -> None:
    noisy = _cmp((0.40, 0.60), (0.42, 0.62), deterministic=False, band=0.10)
    text = noisy.summary()
    assert "inconclusive" in text
    assert "noise band ±0.1000" in text


# ── sensitivity_check ────────────────────────────────────────────────────────


def test_sensitivity_check_fails_when_a_known_degradation_is_invisible() -> None:
    """A ruler that reports "no change" because it is blind looks like one that isn't.

    This is the canary from measurer.py, and it is the habit worth copying: without
    it, a null result cannot be distinguished from an instrument that cannot resolve
    the change. The message has to say that outright, because the tempting reading
    of a failed canary is "so there was no regression".
    """
    ok, why = sensitivity_check(
        "recall_all@5", lambda: 0.50, lambda: 0.50, noise_band=0.05, reps=2
    )
    assert ok is False
    assert "canary failed" in why
    assert "does not clear the noise band of 0.0500" in why
    assert "not evidence of no change" in why


def test_sensitivity_check_passes_when_the_degradation_clears_the_band() -> None:
    ok, why = sensitivity_check(
        "recall_all@5", lambda: 0.80, lambda: 0.20, noise_band=0.05, reps=2
    )
    assert ok is True
    assert "canary cleared" in why
    assert "0.6000" in why


def test_sensitivity_check_fails_a_degradation_exactly_on_the_band() -> None:
    """``drop <= noise_band`` — on the boundary the instrument has not proven itself.

    Values chosen to be exactly representable in binary floating point so the
    boundary really is the boundary: ``0.5 - 0.25 == 0.25`` holds exactly, whereas
    ``0.55 - 0.50`` is 0.050000000000000044 and would land just outside a 0.05 band.
    """
    ok, why = sensitivity_check(
        "m", lambda: 0.5, lambda: 0.25, noise_band=0.25, reps=2
    )
    assert ok is False
    assert "canary failed" in why


def test_sensitivity_check_runs_both_arms_at_least_twice_with_no_warmup() -> None:
    log: list[str] = []
    good, bad = _Counter(log, "good", [0.8]), _Counter(log, "bad", [0.2])
    sensitivity_check("m", good, bad, noise_band=0.05, reps=3)
    assert (good.calls, bad.calls) == (3, 3)
    # Arms are measured one after the other here, not interleaved: the canary is
    # not a paired A/B, it is a one-off proof that the ruler has resolution.
    assert log == ["good"] * 3 + ["bad"] * 3


def test_distractors_are_judged_per_query_not_per_instance_union() -> None:
    """Regression: a multi-query instance must not be judged by its gold UNION.

    This is the bug the full LoCoMo corpus exposed and a small slice hid. An
    earlier version computed ``gold`` as the union over an instance's queries, so a
    conversation whose ~199 questions collectively cite every one of its 19-32
    sessions looked evidence-only and the whole dataset was refused. But "every
    session is evidence for SOME question" says nothing about the task an
    individual question faces -- each one has 1-3 gold sessions and 16-29
    distractors.

    The fixture below is the minimal shape of that: two queries whose gold sets
    union to the entire haystack, while each query individually faces a distractor.
    Under the old union logic this returns False; it must return True.
    """
    corpus = Corpus(
        "locomo",
        "locomo10",
        (
            BenchInstance(
                "conv-1",
                (_session("s1", "the scuba lessons in Malta"), _session("s2", "the charity race")),
                (
                    _query("q1", gold_session_ids=("s1",)),
                    _query("q2", gold_session_ids=("s2",)),
                ),
            ),
        ),
    )
    ok, why = corpus_has_distractors(corpus)
    assert ok is True, why
    # Both queries are counted, not the one instance -- the denominator is the unit
    # of measurement, and reporting instances here is what made the bug invisible.
    assert "2/2 scorable queries" in why


def test_single_query_instance_whose_gold_is_the_whole_haystack_is_still_refused() -> None:
    """The per-query rule must not weaken the guard it exists for.

    LongMemEval-oracle is one query per instance with gold == every session, so
    per-query and per-instance agree there. Pinning it means a future refactor
    cannot fix the LoCoMo false-refusal by simply deleting the check.
    """
    corpus = Corpus(
        "longmemeval",
        "oracle",
        (
            BenchInstance(
                "i1",
                (_session("s1", "alpha"), _session("s2", "beta")),
                (_query("q1", gold_session_ids=("s1", "s2")),),
            ),
        ),
    )
    ok, why = corpus_has_distractors(corpus)
    assert ok is False
    assert "longmemeval_s" in why
