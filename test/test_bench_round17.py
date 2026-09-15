"""Round-17 review finding: the excluded population was reported under one wrong reason.

``retrieve_for_instance`` keeps only ``scorable_retrieval`` queries and that predicate
already excludes the unanswerable ones, so counting ``unanswerable`` over the RESULTS was
structurally always zero. The whole adversarial population then appeared under
``skipped_unscorable``, whose summary line reads "with no resolvable gold" -- telling the
reader that 446 LoCoMo records are broken when 9 are.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.eval.bench import datasets
from kiro_crew.eval.bench.adapters.locomo import load_locomo_file
from kiro_crew.eval.bench.corpus import (
    CAT_ADVERSARIAL,
    CAT_SINGLE_HOP,
    BenchInstance,
    BenchQuery,
)
from kiro_crew.eval.bench.retrieval import RetrievalAggregate


def _instance(queries: list[BenchQuery]) -> BenchInstance:
    return BenchInstance(instance_id="i1", sessions=(), queries=tuple(queries))


def _query(qid: str, *, unanswerable: bool, gold: bool = True) -> BenchQuery:
    return BenchQuery(
        query_id=qid,
        question="q?",
        category=CAT_ADVERSARIAL if unanswerable else CAT_SINGLE_HOP,
        raw_category="5" if unanswerable else "1",
        gold_session_ids=("s1",) if gold else (),
        gold_turn_ids=("t1",) if gold else (),
        unanswerable=unanswerable,
    )


def test_the_two_exclusion_reasons_are_counted_separately() -> None:
    """Both reasons are population-level counts, and they add up to the total."""
    from kiro_crew.eval.bench.retrieval import aggregate

    inst = _instance(
        [
            _query("adversarial", unanswerable=True),
            _query("dangling", unanswerable=False, gold=False),
            _query("scorable", unanswerable=False),
        ]
    )

    agg = aggregate([], instances=[inst], k_values=(1,), turn_attribution=False)

    assert agg.unanswerable_queries == 1, "the adversarial query counted as absent"
    assert agg.skipped_missing_gold == 1
    assert agg.skipped_unscorable == agg.unanswerable_queries + agg.skipped_missing_gold


def test_the_summary_names_the_reason_that_actually_applies() -> None:
    """The single gold-flavoured clause was wrong for the larger population."""
    from kiro_crew.eval.bench.run import _excluded_phrase

    phrase = _excluded_phrase(
        RetrievalAggregate(
            scored_queries=1531,
            skipped_unscorable=455,
            unanswerable_queries=446,
            skipped_missing_gold=9,
        )
    )

    assert "446 unanswerable by design" in phrase
    assert "9 with no resolvable gold" in phrase
    assert "455 with no resolvable gold" not in phrase


def test_no_clause_is_printed_when_nothing_was_excluded() -> None:
    """A clean corpus must not grow an empty parenthesis."""
    from kiro_crew.eval.bench.run import _excluded_phrase

    assert _excluded_phrase(RetrievalAggregate(scored_queries=10)) == ""


def test_only_the_reason_that_occurred_is_named() -> None:
    """A corpus with no dangling gold must not report a zero for it."""
    from kiro_crew.eval.bench.run import _excluded_phrase

    phrase = _excluded_phrase(
        RetrievalAggregate(
            scored_queries=5, skipped_unscorable=2, unanswerable_queries=2
        )
    )
    assert "unanswerable by design" in phrase
    assert "resolvable gold" not in phrase


def test_the_json_report_carries_both_counts() -> None:
    """A reader of the stored report must be able to split the denominator too."""
    source = Path(
        __import__("kiro_crew.eval.bench.run", fromlist=["run"]).__file__ or ""
    ).read_text(encoding="utf-8")
    effective = [ln for ln in source.splitlines() if not ln.lstrip().startswith("#")]
    assert any('"skipped_missing_gold"' in ln for ln in effective)
    assert any('"unanswerable_queries"' in ln for ln in effective)


@pytest.mark.skipif(
    not (datasets.cache_dir() / "locomo10.json").exists(),
    reason="the LoCoMo corpus is not cached on this host",
)
def test_the_real_corpus_split_matches_the_documented_figures() -> None:
    """The published denominator is 1531 of 1986, and the 455 split 446 / 9.

    The design note quotes these; a corpus revision that moved them would otherwise
    leave the document quietly wrong.
    """
    corpus = load_locomo_file(datasets.cache_dir() / "locomo10.json")
    queries = [q for inst in corpus.instances for q in inst.queries]
    excluded = [q for q in queries if not q.scorable_retrieval]

    assert len(queries) == 1986
    assert len(queries) - len(excluded) == 1531
    assert sum(1 for q in excluded if q.unanswerable) == 446
    assert sum(1 for q in excluded if not q.unanswerable) == 9


def test_the_design_note_does_not_blame_gold_for_the_whole_exclusion() -> None:
    """The document has to carry the same split the report now prints."""
    note = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "architecture"
        / "design-notes"
        / "memory-benchmarks.md"
    ).read_text(encoding="utf-8")

    assert "446" in note, "the adversarial count is not stated"
    assert json.dumps("455 with no resolvable gold")[1:-1] not in note
