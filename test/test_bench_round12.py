"""Round-12: adversarial queries must not be scored for retrieval recall.

MEASURED on LoCoMo before the fix:

    queries total           1986
    scorable (before)       1977   -- included all 446 adversarial items
    scorable (after)        1531
    share removed           22.6%

Every adversarial item is `unanswerable` AND carries gold evidence, so every one of
them was counted by the metric — and for those the correct behaviour is REFUSAL, which
means surfacing the evidence was being rewarded with the sign flipped. 22.6% of the
published population.

The rule lives in `BenchQuery.scorable_retrieval` rather than at the filter sites, so
the retrieval loop and the `skipped_unscorable` count cannot disagree and a third
caller cannot forget it. This class's own docstring already stated the rule; only the
code did not apply it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.eval.bench.corpus import (
    CAT_ADVERSARIAL,
    CAT_SINGLE_HOP,
    BenchInstance,
    BenchQuery,
    BenchSession,
    BenchTurn,
)
from kiro_crew.eval.bench.ingest import IngestConfig, ingest_instance
from kiro_crew.eval.bench.retrieval import RetrievalConfig, retrieve_for_instance


def _cached_corpus() -> Path:
    """Where the harness itself would put the cached corpus.

    Derived rather than hardcoded: a literal home path fails the repo's scrub-lint and
    would be wrong on every other machine, and `datasets` already owns this answer.
    """
    from kiro_crew.eval.bench import datasets

    return Path(datasets.cache_dir()) / "locomo10.json"


CORPUS = _cached_corpus()


def _query(qid: str, *, unanswerable: bool, gold: bool = True) -> BenchQuery:
    return BenchQuery(
        query_id=qid,
        question="where did they book the lessons?",
        category=CAT_ADVERSARIAL if unanswerable else CAT_SINGLE_HOP,
        raw_category="5" if unanswerable else "1",
        gold_session_ids=("s1",) if gold else (),
        gold_turn_ids=("t1",) if gold else (),
        unanswerable=unanswerable,
    )


def test_an_unanswerable_query_with_gold_is_not_scorable() -> None:
    """The whole finding in one assertion: gold alone is not sufficient."""
    assert _query("adv", unanswerable=True).scorable_retrieval is False


def test_an_answerable_query_with_gold_is_still_scorable() -> None:
    """Otherwise the fix would empty the benchmark."""
    assert _query("ok", unanswerable=False).scorable_retrieval is True


def test_a_query_without_gold_is_still_excluded() -> None:
    """The original condition must survive the new one."""
    assert _query("nogold", unanswerable=False, gold=False).scorable_retrieval is False


def test_the_retrieval_loop_skips_unanswerable_queries(tmp_path: Path) -> None:
    """Asserted through the loop, not only the property, so the filter is covered."""
    inst = BenchInstance(
        instance_id="r12",
        sessions=(
            BenchSession(
                "s1",
                (
                    BenchTurn(turn_id="t1", session_id="s1", speaker="A", text="malta lessons"),
                    BenchTurn(turn_id="t2", session_id="s1", speaker="B", text="espresso row"),
                ),
                "2023/04/10 (Mon) 23:07",
            ),
        ),
        queries=(_query("ok", unanswerable=False), _query("adv", unanswerable=True)),
    )
    loaded = ingest_instance(
        inst,
        db_path=tmp_path / "r12.db",
        embed_fn=lambda _t: [0.1, 0.2],
        config=IngestConfig(granularity="turn", timeline="now"),
    )
    try:
        results = retrieve_for_instance(
            loaded, embed_fn=lambda _t: [0.1, 0.2], config=RetrievalConfig()
        )
        scored = {r.query_id for r in results}
        assert scored == {"ok"}, f"adversarial query was scored: {scored}"
    finally:
        loaded.close()


@pytest.mark.skipif(not CORPUS.exists(), reason="LoCoMo corpus not cached here")
def test_the_real_corpus_population_matches_the_documented_figure() -> None:
    """Pins the number the design note publishes.

    If the corpus revision moves or the rule changes, the doc and the code disagree
    loudly instead of the doc quietly describing a different experiment -- which is
    exactly what this round's finding was.
    """
    from kiro_crew.eval.bench.adapters.locomo import load_locomo_file

    corpus = load_locomo_file(CORPUS)
    scorable = sum(1 for i in corpus.instances for q in i.queries if q.scorable_retrieval)
    unanswerable = sum(1 for i in corpus.instances for q in i.queries if q.unanswerable)

    assert unanswerable == 446, f"adversarial count moved: {unanswerable}"
    assert scorable == 1531, f"scorable population moved: {scorable}"
