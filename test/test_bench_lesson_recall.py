"""Tests for the lesson ranking recall harness (``bench lesson-recall``).

Covers the golden-set model's refusals, the refusal when ``write_lesson`` merges
a golden rule into another, the deterministic keyword and toy-vector runs over a
real ``VectorMemoryStore``, that the score comes from the production ranking
call, and the CLI dispatch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import vector_memory
from kiro_crew.cli_bench import bench_cmd
from kiro_crew.eval.bench.lesson_recall import (
    KEYWORD_ONLY_EMBEDDER_ID,
    LESSON_QUERY_CLASSES,
    LessonGoldenSet,
    LessonGoldenSetError,
    _stored_rule_text,
    _write_rules,
    default_lesson_golden_set_path,
    format_lesson_report,
    run_lesson_recall,
)
from kiro_crew.eval.bench.toy_embedder import TOY_EMBEDDER_ID
from kiro_crew.vector_memory import VectorMemoryStore


class _Args:
    """Stand-in for the argparse namespace the dispatch receives."""

    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


def _golden(tmp_path: Path, rules: list[dict], queries: list[dict]) -> Path:
    path = tmp_path / "golden.json"
    path.write_text(json.dumps({"name": "t", "rules": rules, "queries": queries}))
    return path


_RULES = [
    {"id": "r-kafka", "rule": "Pause the Kafka consumer group before resetting its offsets."},
    {"id": "r-ttl", "rule": "Lower the DNS record TTL a day before migrating a hostname."},
]
_QUERY = {
    "id": "q",
    "class": "distinctive_term",
    "request": "kafka offsets",
    "gold_rule_ids": ["r-kafka"],
}


@pytest.fixture(scope="module")
def packaged() -> LessonGoldenSet:
    return LessonGoldenSet.from_json(default_lesson_golden_set_path())


@pytest.fixture(scope="module")
def keyword_report(packaged: LessonGoldenSet):
    return run_lesson_recall(packaged, use_embeddings=False)


class TestGoldenSet:
    def test_packaged_set_covers_every_class(self, packaged: LessonGoldenSet) -> None:
        classes = [q.query_class for q in packaged.queries]
        assert {c: classes.count(c) for c in LESSON_QUERY_CLASSES} == dict.fromkeys(
            LESSON_QUERY_CLASSES, 6
        )
        assert len(packaged.rules) == 40

    @pytest.mark.parametrize(
        ("rules", "query", "message"),
        [
            (_RULES, {**_QUERY, "gold_rule_ids": ["r-gone"]}, "names rules no entry defines"),
            (_RULES, {**_QUERY, "class": "abstention"}, "unknown class"),
            (_RULES, {**_QUERY, "gold_rule_ids": []}, "non-empty gold_rule_ids"),
            ([_RULES[0], {**_RULES[1], "id": "r-kafka"}], _QUERY, "duplicate rule ids"),
            ([_RULES[0], {**_RULES[0], "id": "r-copy"}], _QUERY, "same text"),
        ],
    )
    def test_unscorable_sets_are_refused(
        self, tmp_path: Path, rules: list[dict], query: dict, message: str
    ) -> None:
        with pytest.raises(LessonGoldenSetError, match=message):
            LessonGoldenSet.from_json(_golden(tmp_path, rules, [query]))

    def test_a_file_that_is_not_utf8_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "golden.json"
        path.write_bytes(b'{"name": "t\xff", "rules": [], "queries": []}')
        with pytest.raises(LessonGoldenSetError, match="not valid UTF-8"):
            LessonGoldenSet.from_json(path)

    def test_a_lone_surrogate_is_refused_before_it_reaches_the_store(self, tmp_path: Path) -> None:
        """The JSON escape parses to a lone surrogate, which the store cannot encode."""
        golden = _golden(tmp_path, _RULES, [_QUERY])
        golden.write_text(golden.read_text().replace("Pause the", r"Pause \ud800 the"))
        with pytest.raises(
            LessonGoldenSetError, match="'r-kafka' holds text that is not valid UTF-8"
        ):
            LessonGoldenSet.from_json(golden)

    def test_non_object_json_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "golden.json"
        path.write_text("[]")
        with pytest.raises(LessonGoldenSetError, match="JSON object"):
            LessonGoldenSet.from_json(path)


class TestRun:
    def test_every_stored_rule_is_ranked_for_every_query(
        self, packaged: LessonGoldenSet, keyword_report
    ) -> None:
        rule_ids = sorted(r.id for r in packaged.rules)
        assert [sorted(r.ranked_rule_ids) for r in keyword_report.results] == [rule_ids] * len(
            packaged.queries
        )

    def test_keyword_run_is_labelled_keyword_only(self, keyword_report) -> None:
        assert keyword_report.embedder_id == KEYWORD_ONLY_EMBEDDER_ID

    def test_keyword_run_finds_a_rule_that_shares_a_distinctive_word(self, keyword_report) -> None:
        by_class = keyword_report.by_class(1)
        assert by_class["distinctive_term"]["recall_any"] == 1.0
        assert by_class["short_request"]["recall_any"] == 1.0

    def test_keyword_run_cannot_rank_a_paraphrase(self, keyword_report) -> None:
        """A paraphrase shares no word with its rule, so words alone rarely find it.

        This is what separates the keyword measure from a semantic run, and it
        fails if the paraphrase queries start sharing words with their rules.
        """
        assert keyword_report.by_class(3)["paraphrase"]["recall_any"] < 0.5

    def test_score_comes_from_the_production_ranking_call(
        self, packaged: LessonGoldenSet, keyword_report, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reversing ``_rank_lessons`` must change the score, or the harness ranks on its own."""
        original = VectorMemoryStore._rank_lessons

        def reversed_rank(self, entries, query_text, *, recall_query=None):
            return list(reversed(original(self, entries, query_text, recall_query=recall_query)))

        monkeypatch.setattr(VectorMemoryStore, "_rank_lessons", reversed_rank)
        reversed_report = run_lesson_recall(packaged, use_embeddings=False)
        assert reversed_report.headline(1)["mrr"] < keyword_report.headline(1)["mrr"]

    def test_toy_vector_run_is_deterministic(self, packaged: LessonGoldenSet) -> None:
        first = run_lesson_recall(packaged)
        second = run_lesson_recall(packaged)
        assert first.embedder_id == TOY_EMBEDDER_ID
        assert [r.ranked_rule_ids for r in first.results] == [
            r.ranked_rule_ids for r in second.results
        ]

    def test_tied_rules_rank_alike_whatever_instants_the_store_clock_reads(
        self, packaged: LessonGoldenSet, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A store clock frozen on one instant ranks like one that never repeats.

        ``get_lessons`` orders rows newest first and by key within one stamp, and
        ``rank_lessons`` keeps that order for rules tied on every score. A coarse
        clock (about 15.6 ms on Windows under Python 3.12) puts back-to-back
        writes on one stamp in groups that differ from run to run; the two runs
        here are its extremes, every write on one instant and each on its own.
        The keyword run is used because words alone tie more rules than the toy
        vector does. Each run must also hand the store's clock back as it found it.

        Fails if the rules are stamped by the store's clock: the frozen run then
        ranks tied rules by key, and the other newest first. Fails too if the
        harness leaves its own clock in place after a run.
        """

        def frozen() -> str:
            return "2026-01-01T00:00:00+00:00"

        monkeypatch.setattr(vector_memory, "_now_iso", frozen)
        one_instant = run_lesson_recall(packaged, use_embeddings=False)
        assert vector_memory._now_iso is frozen
        ticks = iter(f"2026-01-01T00:00:00.{tick:06d}+00:00" for tick in range(1, 1_000_000))

        def ticking() -> str:
            return next(ticks)

        monkeypatch.setattr(vector_memory, "_now_iso", ticking)
        distinct_instants = run_lesson_recall(packaged, use_embeddings=False)
        assert vector_memory._now_iso is ticking
        assert [r.ranked_rule_ids for r in one_instant.results] == [
            r.ranked_rule_ids for r in distinct_instants.results
        ]

    def test_the_store_reads_the_golden_rules_back_newest_first_in_file_order(
        self, packaged: LessonGoldenSet, tmp_path: Path, opened
    ) -> None:
        """Each rule is stamped after the one before it, so the store reads the file reversed.

        That is the order ``rank_lessons`` keeps for rules tied on every score, and
        the order a host whose clock never repeats already produces, so a run's
        numbers stay comparable across hosts.

        Fails if the rules share one stamp or are stamped out of file order: the
        store then reads them back by key, or in some other order.
        """
        store = opened(VectorMemoryStore(db_path=tmp_path / "memory.db"))
        store.init()
        rule_id_by_text = _write_rules(store, packaged.rules)
        assert [rule_id_by_text[_stored_rule_text(row)] for row in store.get_lessons()] == [
            rule.id for rule in reversed(packaged.rules)
        ]

    def test_a_rule_the_store_merges_is_refused_by_name(self, tmp_path: Path) -> None:
        """``write_lesson`` keeps the longer of two rules when one contains the other."""
        rules = [
            {
                "id": "r-short",
                "rule": "Pause the Kafka consumer group before resetting its offsets.",
            },
            {
                "id": "r-long",
                "rule": "Pause the Kafka consumer group before resetting its offsets, every time.",
            },
        ]
        query = {**_QUERY, "gold_rule_ids": ["r-short"]}
        golden = LessonGoldenSet.from_json(_golden(tmp_path, rules, [query]))
        with pytest.raises(LessonGoldenSetError, match="r-long"):
            run_lesson_recall(golden, use_embeddings=False)

    def test_a_rule_stored_without_a_vector_is_refused(self, tmp_path: Path) -> None:
        """A hybrid run where a rule has no vector would score partly as a keyword run."""
        golden = LessonGoldenSet.from_json(_golden(tmp_path, _RULES, [_QUERY]))
        with pytest.raises(LessonGoldenSetError, match="without a vector: r-kafka, r-ttl"):
            run_lesson_recall(golden, embed_fn=lambda _text: [], embedder_id="fake")

    def test_a_request_that_is_not_embedded_is_refused(self, tmp_path: Path) -> None:
        golden = LessonGoldenSet.from_json(_golden(tmp_path, _RULES, [_QUERY]))

        def rules_only(text: str) -> list[float]:
            # Distinct directions, so the store's cosine dedup keeps both rules.
            if text == _QUERY["request"]:
                return []
            return [1.0, 0.0] if "Kafka" in text else [0.0, 1.0]

        with pytest.raises(LessonGoldenSetError, match="request was not embedded"):
            run_lesson_recall(golden, embed_fn=rules_only, embedder_id="fake")

    def test_uncomputed_cut_off_is_refused(self, keyword_report) -> None:
        with pytest.raises(ValueError, match="not computed"):
            keyword_report.headline(2)

    def test_report_names_every_class(self, keyword_report) -> None:
        text = format_lesson_report(keyword_report, k=3)
        assert all(query_class in text for query_class in LESSON_QUERY_CLASSES)


class TestCli:
    @staticmethod
    def _args(**overrides: object) -> _Args:
        base = {
            "bench_action": "lesson-recall",
            "golden": None,
            "k": 3,
            "real_embedder": False,
            "no_embeddings": True,
        }
        return _Args(**{**base, **overrides})

    def test_keyword_path_returns_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert bench_cmd(self._args()) == 0
        out = capsys.readouterr().out
        assert "Lesson recall eval: lesson_golden_v1" in out
        assert KEYWORD_ONLY_EMBEDDER_ID in out

    def test_a_non_default_cut_off_is_computed(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert bench_cmd(self._args(k=2)) == 0
        assert "recall_any@2" in capsys.readouterr().out

    def test_missing_golden_refuses(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert bench_cmd(self._args(golden=str(tmp_path / "nope.json"))) == 1
        assert "golden set" in capsys.readouterr().out

    def test_real_embedder_refuses_when_warmup_times_out(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.knowledge.embedder as emb

        monkeypatch.setattr(emb.InProcessEmbedder, "wait_ready", lambda self, timeout=None: False)
        assert bench_cmd(self._args(real_embedder=True, no_embeddings=False)) == 1
        assert "did not become ready within 120 seconds" in capsys.readouterr().out
