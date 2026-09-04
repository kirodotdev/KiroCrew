"""Round-7 findings: two blocking, both real, both the same root cause as before.

Finding 1 is the FOURTH site of one fact I established in round 5 and then failed
to sweep: the live embedder's vector width is not necessarily 1024. Round 5 fixed
the recorded *identity*; the store was still constructed with the default width,
which gates every write and sizes the FAISS index.

Finding 2 sharpens round 4's own fix. That round required equal, non-zero
`session_measurable` counts. Equal counts are necessary and not sufficient:
eligibility depends on how many distinct items the retrieval window exposed, so a
ranking change can swap which queries qualify and leave the count untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.eval.bench.corpus import (
    CAT_SINGLE_HOP,
    BenchInstance,
    BenchSession,
    BenchTurn,
)
from kiro_crew.eval.bench.ingest import IngestConfig, IngestError, ingest_instance
from kiro_crew.eval.bench.retrieval import QueryRetrieval, aggregate
from kiro_crew.eval.bench.run import compare_reports

LME_TS = "2023/04/10 (Mon) 23:07"


def _instance() -> BenchInstance:
    return BenchInstance(
        instance_id="r7",
        sessions=(
            BenchSession(
                "s1",
                (
                    BenchTurn(turn_id="t1", session_id="s1", speaker="Alice", text="malta lessons"),
                    BenchTurn(turn_id="t2", session_id="s1", speaker="Bob", text="espresso row"),
                ),
                LME_TS,
            ),
        ),
        queries=(),
    )


# ── Finding 1: the store's width comes from the vector, not from a constant ──


@pytest.mark.parametrize("width", [8, 768, 1024, 1536])
def test_the_store_adopts_the_embedders_actual_width(tmp_path: Path, width: int) -> None:
    """A non-1024 embedder must ingest cleanly.

    `VectorMemoryStore` defaults to 1024 and gates every write on that width, so
    before the fix a 768-dim model had every vector rejected or crashed the first
    FAISS add. 1024 is included so the test cannot pass by being width-agnostic in
    a way that breaks the normal case.
    """
    loaded = ingest_instance(
        _instance(),
        db_path=tmp_path / f"w{width}.db",
        embed_fn=lambda _t: [0.01] * width,
        config=IngestConfig(granularity="turn", timeline="now"),
    )
    try:
        assert loaded.report.written == 2
        assert loaded.report.attempted == 2
    finally:
        loaded.close()


def test_the_store_is_constructed_with_the_embedders_width(tmp_path: Path) -> None:
    """The mechanism, asserted directly -- and honestly about what it proves.

    I could not reproduce the defect's symptom on this host, and the reason is
    worth recording rather than papering over. `faiss` is not importable here (it
    is in no dependency group), and probing `VectorMemoryStore` directly shows the
    declared width is inert on the sqlite_cosine path: writing an 8-dim vector into
    a store declaring 1024 returns True, unrejected. The bench also uses ONE
    `embed_fn` for both the stored fragments and the query, so both sides are the
    same width whatever the store was told, and retrieval works either way.

    Where `faiss` IS importable the declared width sizes `IndexFlatIP`, and a
    mismatched vector fails the add -- the reviewer's stated mechanism, on a
    configuration this host cannot run. So this test asserts the store received the
    right width, which is checkable here and fails without the fix, instead of
    asserting an outcome that passes for the wrong reason.
    """
    for width in (8, 768, 1536):
        loaded = ingest_instance(
            _instance(),
            db_path=tmp_path / f"dim{width}.db",
            embed_fn=lambda _t, w=width: [0.01] * w,
            config=IngestConfig(granularity="turn", timeline="now"),
        )
        try:
            assert loaded.store._embedding_dim == width, (
                f"store declares {loaded.store._embedding_dim}, embedder produces "
                f"{width} -- a FAISS host would fail the first add"
            )
        finally:
            loaded.close()


def test_the_width_probe_is_not_counted_as_a_fragment(tmp_path: Path) -> None:
    """The probe is overhead, not data -- it must not inflate the denominator."""
    calls: list[str] = []

    def embed(text: str) -> list[float]:
        calls.append(text)
        return [0.5, 0.5]

    loaded = ingest_instance(
        _instance(),
        db_path=tmp_path / "probe.db",
        embed_fn=embed,
        config=IngestConfig(granularity="turn", timeline="now"),
    )
    try:
        assert loaded.report.attempted == 2, "the probe must not count as a fragment"
        assert len(calls) == 3, "two fragments plus one width probe"
    finally:
        loaded.close()


def test_a_failed_width_probe_refuses(tmp_path: Path) -> None:
    with pytest.raises(IngestError) as excinfo:
        ingest_instance(
            _instance(),
            db_path=tmp_path / "noprobe.db",
            embed_fn=lambda _t: None,
            config=IngestConfig(granularity="turn", timeline="now"),
        )
    assert "width" in str(excinfo.value)


# ── Finding 2: equal counts are not the same population ──────────────────────


def _result(qid: str, ranked: tuple[str, ...]) -> QueryRetrieval:
    return QueryRetrieval(
        query_id=qid,
        category=CAT_SINGLE_HOP,
        raw_category="1",
        unanswerable=False,
        gold_session_ids=("s1",),
        gold_turn_ids=("t1",),
        retrieved_session_ids=ranked,
        retrieved_turn_ids=ranked,
        unattributed_hits=0,
    )


def test_the_population_digest_depends_on_the_set_not_the_order() -> None:
    a = aggregate([_result("q1", ("s1",)), _result("q2", ("s1",))], k_values=(1,))
    b = aggregate([_result("q2", ("s1",)), _result("q1", ("s1",))], k_values=(1,))
    assert a.session_population[1] == b.session_population[1]


def test_a_different_query_set_of_the_same_size_gets_a_different_digest() -> None:
    a = aggregate([_result("q1", ("s1",)), _result("q2", ("s1",))], k_values=(1,))
    b = aggregate([_result("q1", ("s1",)), _result("q3", ("s1",))], k_values=(1,))
    assert a.session_measurable[1] == b.session_measurable[1] == 2
    assert a.session_population[1] != b.session_population[1]


def _report(digest: str | None, *, mean: float, count: int = 1977) -> dict:
    metrics: dict = {
        "session": {"recall_all@5": mean},
        "session_measurable": {"5": count},
    }
    if digest is not None:
        metrics["session_population"] = {"5": digest}
    return {
        "corpus": {"fingerprint": "f" * 64},
        "config": {
            "ingest": {"granularity": "turn", "timeline": "now"},
            "retrieval": {"limit": 20, "mmr": True},
            "search_backend": "sqlite_cosine",
            "embedder": "qwen3-embedding:0.6b@1024",
            # Required since round 13: absent provenance is refused,
            # not compared -- two reports both missing a field used
            # to compare as compatible.
            "environment": {"python": "3.12.10", "platform": "linux-x86_64"},
        },
        "metrics": metrics,
    }


def test_equal_counts_with_different_populations_refuse() -> None:
    """The case a count check structurally cannot catch."""
    out = compare_reports(_report("a" * 64, mean=0.70), _report("b" * 64, mean=0.75))
    assert "not comparable" in out
    assert "not the same" in out
    assert "0.05" not in out, "no delta may be published for different populations"


def test_equal_counts_with_the_same_population_compare() -> None:
    """Otherwise the guard would refuse every legitimate comparison."""
    out = compare_reports(_report("a" * 64, mean=0.70), _report("a" * 64, mean=0.75))
    assert "not comparable" not in out
    assert "recall_all@5" in out


def test_a_report_predating_the_digest_refuses_rather_than_trusting_the_count() -> None:
    """Legacy artifacts must not take the comparable path on counts alone."""
    out = compare_reports(_report(None, mean=0.70), _report("a" * 64, mean=0.75))
    assert "not comparable" in out
    assert "predate" in out


def test_the_digest_reaches_the_serialized_report() -> None:
    """A field the comparison REQUIRES must actually be written to disk.

    Exercises `RunResult.to_json` rather than round-tripping a dict, because the
    comparison refusing on a missing digest would otherwise be triggered by the
    harness's own reports.
    """
    from kiro_crew.eval.bench.retrieval import RetrievalAggregate
    from kiro_crew.eval.bench.run import RunResult

    agg = RetrievalAggregate()
    agg.session = {"recall_all@5": 0.5}
    agg.session_measurable = {5: 1977}
    agg.session_population = {5: "c" * 64}
    agg.turn_population = {5: "d" * 64}

    result = RunResult(
        corpus_name="locomo",
        corpus_variant="locomo10",
        corpus_fingerprint="f" * 64,
        instances=1,
        sessions=1,
        turns=2,
        queries=1,
        ingest={"granularity": "turn", "timeline": "now"},
        retrieval={"limit": 20, "mmr": True},
        backend="sqlite_cosine",
        embedder="qwen3-embedding:0.6b@1024",
        metrics=agg,
    )
    payload = json.loads(json.dumps(result.to_json()))
    assert payload["metrics"]["session_population"] == {"5": "c" * 64}
    assert payload["metrics"]["turn_population"] == {"5": "d" * 64}
