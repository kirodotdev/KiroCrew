"""Tests for the session search recall harness (``bench session-recall``).

Covers the golden-set model's refusals, exact scores on a planted store searched
through the real ``search_sessions``, that a session's age is measured from the
run rather than the calendar, the refusal when the scan window cannot see the
whole set, that the Chinese segmenter is put back, and the CLI dispatch.
"""

from __future__ import annotations

import json
import time
import types
from pathlib import Path

import pytest

from kiro_crew import history, history_search
from kiro_crew.cli_bench import bench_cmd
from kiro_crew.eval.bench import session_recall
from kiro_crew.eval.bench.session_recall import (
    SessionGoldenSet,
    SessionGoldenSetError,
    default_session_golden_set_path,
    format_session_report,
    run_session_recall,
)


class _Args:
    """Stand-in for the argparse namespace the dispatch receives."""

    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


def _session(session_id: str, age_days: int, *texts: str) -> dict:
    return {
        "id": session_id,
        "age_days": age_days,
        "messages": [{"role": "user", "content": text} for text in texts],
    }


def _query(query_id: str, query_class: str, query: str, gold: list[str]) -> dict:
    return {"id": query_id, "class": query_class, "query": query, "gold_session_ids": gold}


def _planted() -> dict:
    return {
        "name": "planted",
        "sessions": [
            _session("a", 1, "the uploader retries forever when the bucket returns 503"),
            _session("b", 2, "we pinned the uploader timeout to thirty seconds"),
            _session("c", 3, "lunch plans for friday"),
        ],
        "queries": [
            _query("q-first", "literal", "returns 503", ["a"]),
            _query("q-second", "short", "uploader", ["b"]),
            _query("q-miss", "paraphrase", "storage service keeps failing", ["a"]),
            _query("q-none", "no_answer", "kubernetes readiness probe", []),
        ],
    }


def _write(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "golden.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def _by_id(report: session_recall.SessionRecallReport) -> dict:
    return {r.query_id: r for r in report.results}


class TestGoldenSet:
    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda g: g["sessions"].append(_session("A", 9, "x")), "duplicate session ids"),
            (lambda g: g["sessions"].append(_session("d", 1, "x")), "same age_days"),
            (lambda g: g["sessions"].append(_session("../e", 9, "x")), "session id must be"),
            (lambda g: g["sessions"].append(_session("com1", 9, "x")), "Windows device name"),
            (lambda g: g["sessions"].append(_session("e", 36501, "x")), "age_days from 0"),
            (
                lambda g: g["sessions"][0]["messages"].append({"role": "tool", "content": "x"}),
                "user or assistant",
            ),
            (lambda g: g["queries"].append(_query("q-x", "literal", "x", ["z"])), "no entry"),
            (lambda g: g["queries"].append(_query("q-x", "fuzzy", "x", ["a"])), "unknown class"),
            (lambda g: g["queries"].append(_query("q-x", "no_answer", "x", ["a"])), "names gold"),
            (lambda g: g["queries"].append(_query("q-x", "literal", "x", [])), "no gold"),
            (lambda g: g["queries"].append(_query("q-first", "short", "x", ["a"])), "duplicate"),
            (lambda g: g.update(label_revision="2"), "label_revision"),
            (
                lambda g: g.update(queries=[_query("q-n", "no_answer", "x", [])]),
                "query with an answer",
            ),
        ],
    )
    def test_a_malformed_set_is_refused(self, tmp_path: Path, mutate, message: str) -> None:
        raw = _planted()
        mutate(raw)
        with pytest.raises(SessionGoldenSetError, match=message):
            SessionGoldenSet.from_json(_write(tmp_path, raw))

    def test_the_packaged_set_loads(self) -> None:
        golden = SessionGoldenSet.from_json(default_session_golden_set_path())
        assert golden.name == "session_golden_v1"
        assert {q.query_class for q in golden.queries} == set(session_recall.SESSION_QUERY_CLASSES)


class TestRun:
    def test_scores_come_from_search_sessions(self, tmp_path: Path) -> None:
        report = run_session_recall(SessionGoldenSet.from_json(_write(tmp_path, _planted())))
        results = _by_id(report)

        assert results["q-first"].ranked_session_ids == ("a",)
        assert results["q-first"].recall_any[1] == 1.0
        # Both uploader sessions match; the newer one ranks first, so the gold is second.
        assert results["q-second"].ranked_session_ids == ("a", "b")
        assert results["q-second"].recall_any[1] == 0.0
        assert results["q-second"].mrr[10] == 0.5
        assert results["q-miss"].ranked_session_ids == ()
        assert report.by_class(10)["no_answer"] == {"n": 1.0, "queries_with_rows": 0.0}

    def test_a_no_answer_query_counts_once_however_many_rows(self, tmp_path: Path) -> None:
        raw = _planted()
        raw["queries"].append(_query("q-two", "no_answer", "uploader", []))
        report = run_session_recall(SessionGoldenSet.from_json(_write(tmp_path, raw)))

        assert _by_id(report)["q-two"].ranked_session_ids == ("a", "b")
        assert report.by_class(10)["no_answer"] == {"n": 2.0, "queries_with_rows": 1.0}

    def test_age_is_measured_from_the_run(self, tmp_path: Path, monkeypatch) -> None:
        """Run a year apart, the ranking is the same: ages are relative to the run."""
        raw = _planted()
        # Content alone ranks the shorter session b first; only recency puts a ahead.
        raw["sessions"][0]["age_days"], raw["sessions"][1]["age_days"] = 1, 40
        golden = SessionGoldenSet.from_json(_write(tmp_path, raw))

        def run_at(instant: float) -> session_recall.SessionRecallReport:
            clock = types.SimpleNamespace(
                time=lambda: instant,
                time_ns=lambda: int(instant * 1e9),
                monotonic=time.monotonic,
            )
            monkeypatch.setattr(session_recall, "time", clock)
            monkeypatch.setattr(history_search, "_time", clock)
            return run_session_recall(golden)

        first = run_at(1.9e9)
        later = run_at(1.9e9 + 365 * 86400)

        assert _by_id(first)["q-second"].ranked_session_ids == ("a", "b")
        assert [r.ranked_session_ids for r in first.results] == [
            r.ranked_session_ids for r in later.results
        ]

    def test_a_window_smaller_than_the_set_is_refused(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(history, "_SEARCH_SCAN_WINDOW", 2)
        golden = SessionGoldenSet.from_json(_write(tmp_path, _planted()))

        with pytest.raises(SessionGoldenSetError, match="read 2 of 3 sessions"):
            run_session_recall(golden)

    def test_the_segmenter_is_put_back(self, tmp_path: Path) -> None:
        def sentinel(runs):
            return None

        history_search.register_cjk_segmenter(sentinel)
        try:
            run_session_recall(SessionGoldenSet.from_json(_write(tmp_path, _planted())))
            assert history_search._cjk_slot.segment is sentinel
        finally:
            history_search.reset_cjk_segmenter_for_tests()

    def test_the_packaged_set_ranks_the_same_twice(self) -> None:
        golden = SessionGoldenSet.from_json(default_session_golden_set_path())

        first, second = run_session_recall(golden), run_session_recall(golden)

        assert [r.ranked_session_ids for r in first.results] == [
            r.ranked_session_ids for r in second.results
        ]

    def test_the_report_names_the_set_and_the_segmenter(self, tmp_path: Path) -> None:
        raw = {**_planted(), "label_revision": 3}
        text = format_session_report(
            run_session_recall(SessionGoldenSet.from_json(_write(tmp_path, raw)))
        )

        assert "Session recall eval: planted (label revision 3)" in text
        assert "chinese segmenter: " in text
        assert "no_answer" in text and "queries with rows: 0" in text


class TestCli:
    def test_the_packaged_set_returns_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert bench_cmd(_Args(bench_action="session-recall", golden=None)) == 0
        assert "Session recall eval: session_golden_v1" in capsys.readouterr().out

    def test_a_missing_set_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = str(tmp_path / "absent.json")

        assert bench_cmd(_Args(bench_action="session-recall", golden=missing)) == 1
        assert "refusing to read the golden set" in capsys.readouterr().err
