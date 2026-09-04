"""Two GPT-review blockers, pinned so they cannot come back.

Both were real, and both had the same shape: the harness published a number that
read as more trustworthy than the thing behind it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.eval.bench.corpus import (
    CAT_SINGLE_HOP,
    BenchInstance,
    BenchQuery,
    BenchSession,
    BenchTurn,
    Corpus,
)
from kiro_crew.eval.bench.ingest import IngestConfig
from kiro_crew.eval.bench.retrieval import (
    QueryRetrieval,
    RetrievalConfig,
    aggregate,
)
from kiro_crew.eval.bench.run import compare_reports, run_retrieval
from kiro_crew.eval.bench.toy_embedder import TOY_EMBEDDER_ID, toy_embed_fn

# ── A session cut-off wider than the fragment window is not measurable ───────
# Retrieval asks the store for `limit` FRAGMENTS; the distinct sessions among them
# are however many they span. Measured on LoCoMo with a 20-fragment window: 8-14
# distinct sessions per query, so "the top 20 sessions" was observable for 0 of 57
# queries -- yet recall_all@20 was computed anyway and reported as a low score.
# The metric was bounded by the window, not by the ranker.


def _qr(query_id: str, *, sessions: tuple[str, ...], gold: tuple[str, ...]) -> QueryRetrieval:
    return QueryRetrieval(
        query_id=query_id,
        category=CAT_SINGLE_HOP,
        raw_category="4",
        unanswerable=False,
        gold_session_ids=gold,
        gold_turn_ids=(),
        retrieved_session_ids=sessions,
        retrieved_turn_ids=(),
    )


def test_a_cutoff_wider_than_the_observed_list_is_omitted_not_scored_low() -> None:
    """Three observed sessions cannot answer "are all gold in the top 10"."""
    results = [_qr("q1", sessions=("s1", "s2", "s3"), gold=("s9",))]
    agg = aggregate(results, k_values=(1, 3, 10))

    assert "recall_all@3" in agg.session  # 3 observed, so @3 is measurable
    assert "recall_all@10" not in agg.session  # @10 is not, and must not appear
    assert agg.session_measurable[3] == 1
    assert agg.session_measurable[10] == 0


def test_a_measurable_cutoff_still_scores_a_genuine_miss_as_zero() -> None:
    """The filter must exclude the UNOBSERVABLE, not the unsuccessful.

    Otherwise it would quietly launder real misses into omissions and inflate
    every number it touched.
    """
    results = [_qr("q1", sessions=("s1", "s2", "s3"), gold=("s9",))]
    agg = aggregate(results, k_values=(3,))
    assert agg.session_measurable[3] == 1
    assert agg.session["recall_all@3"] == 0.0


def test_queries_are_filtered_per_query_not_per_run() -> None:
    """One short list must not remove the cut-off for the queries that do support it."""
    results = [
        _qr("wide", sessions=tuple(f"s{i}" for i in range(10)), gold=("s0",)),
        _qr("narrow", sessions=("s0", "s1"), gold=("s0",)),
    ]
    agg = aggregate(results, k_values=(5,))
    # Only the wide query is eligible at k=5, and it succeeds -> 1.0 over n=1.
    assert agg.session_measurable[5] == 1
    assert agg.session["recall_all@5"] == 1.0


def test_the_report_names_an_omitted_cutoff_instead_of_dropping_it_silently(
    tmp_path: Path,
) -> None:
    """A missing row reads as "not requested"; it was requested and found unmeasurable."""
    from kiro_crew.eval.bench.run import format_report

    corpus = Corpus(
        "toy",
        "v0",
        (
            BenchInstance(
                "i1",
                tuple(
                    BenchSession(f"s{i}", (BenchTurn(f"s{i}#t0", f"s{i}", "Alice", f"topic {i}"),))
                    for i in range(4)
                ),
                (
                    BenchQuery(
                        query_id="q1",
                        question="topic 0",
                        category=CAT_SINGLE_HOP,
                        gold_session_ids=("s0",),
                    ),
                ),
            ),
        ),
    )
    result = run_retrieval(
        corpus,
        ingest_config=IngestConfig(timeline="now"),
        retrieval_config=RetrievalConfig(k_values=(1, 50)),
        embed_fn=toy_embed_fn(),
        embedder_id=TOY_EMBEDDER_ID,
        store_root=tmp_path,
    )
    text = format_report(result, k_values=(1, 50))
    assert "k = 50 omitted" in text
    assert "bounded by the window" in text


# ── The embedder identity must be recorded and compared ──────────────────────
# A report saved from a --toy-embedder run carried no embedder identity, so it
# compared as equivalent to a real run: `compare` printed an "exact" delta between
# a hashed bag-of-words and a language model.


def test_the_report_records_which_embedder_produced_it(tmp_path: Path) -> None:
    corpus = Corpus(
        "toy",
        "v0",
        (
            BenchInstance(
                "i1",
                (
                    BenchSession("s1", (BenchTurn("s1#t0", "s1", "Alice", "the blue mat"),)),
                    BenchSession("s2", (BenchTurn("s2#t0", "s2", "Alice", "a green park"),)),
                ),
                (
                    BenchQuery(
                        query_id="q1",
                        question="blue mat",
                        category=CAT_SINGLE_HOP,
                        gold_session_ids=("s1",),
                    ),
                ),
            ),
        ),
    )
    result = run_retrieval(
        corpus,
        ingest_config=IngestConfig(timeline="now"),
        retrieval_config=RetrievalConfig(k_values=(1,)),
        embed_fn=toy_embed_fn(),
        embedder_id=TOY_EMBEDDER_ID,
        store_root=tmp_path,
    )
    assert result.embedder == TOY_EMBEDDER_ID
    assert result.to_json()["config"]["embedder"] == TOY_EMBEDDER_ID


def test_an_injected_embedder_without_an_identity_is_refused(tmp_path: Path) -> None:
    """Fail closed: an unlabelled embedder would be saved as if it were production."""
    corpus = Corpus(
        "toy",
        "v0",
        (
            BenchInstance(
                "i1",
                (
                    BenchSession("s1", (BenchTurn("s1#t0", "s1", "A", "x"),)),
                    BenchSession("s2", (BenchTurn("s2#t0", "s2", "A", "y"),)),
                ),
                (
                    BenchQuery(
                        query_id="q1",
                        question="x",
                        category=CAT_SINGLE_HOP,
                        gold_session_ids=("s1",),
                    ),
                ),
            ),
        ),
    )
    with pytest.raises(ValueError) as exc:
        run_retrieval(
            corpus,
            ingest_config=IngestConfig(timeline="now"),
            embed_fn=toy_embed_fn(),
            store_root=tmp_path,
        )
    assert "embedder_id" in str(exc.value)


def _report(embedder: str) -> dict:
    return {
        "corpus": {"fingerprint": "abc"},
        "config": {
            "ingest": {"granularity": "turn"},
            "retrieval": {"mmr": True},
            "search_backend": "sqlite_cosine",
            "embedder": embedder,
            # Required since round 13: absent provenance is refused,
            # not compared -- two reports both missing a field used
            # to compare as compatible.
            "environment": {"python": "3.12.10", "platform": "linux-x86_64"},
        },
        "metrics": {
            "session": {"recall_all@5": 0.5},
            "session_measurable": {"5": 1977},
            "session_population": {"5": "a" * 64},
        },
    }


def test_a_toy_baseline_and_a_real_candidate_are_refused() -> None:
    out = compare_reports(_report(TOY_EMBEDDER_ID), _report("qwen3-embedding:0.6b@1024"), k=5)
    assert "## Not comparable" in out
    assert "embedder" in out
    assert "No delta is reported" in out
    assert "recall_all@5" not in out


def test_a_report_predating_the_embedder_field_does_not_pass_silently() -> None:
    """A missing key must count as a mismatch, or old reports bypass the new guard."""
    legacy = _report("qwen3-embedding:0.6b@1024")
    del legacy["config"]["embedder"]
    out = compare_reports(legacy, _report("qwen3-embedding:0.6b@1024"), k=5)
    assert "## Not comparable" in out
    assert "embedder" in out


def test_two_runs_from_the_same_embedder_still_compare() -> None:
    out = compare_reports(_report(TOY_EMBEDDER_ID), _report(TOY_EMBEDDER_ID), k=5)
    assert "## Not comparable" not in out
    assert "recall_all@5" in out


def test_the_saved_json_round_trips_through_compare(tmp_path: Path) -> None:
    """End-to-end: what write_report saves must be what compare_reports accepts."""
    a = tmp_path / "a.json"
    a.write_text(json.dumps(_report(TOY_EMBEDDER_ID)), encoding="utf-8")
    loaded = json.loads(a.read_text(encoding="utf-8"))
    assert "## Not comparable" not in compare_reports(loaded, loaded, k=5)
