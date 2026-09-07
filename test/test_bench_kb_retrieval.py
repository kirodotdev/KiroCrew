"""Tests for the Knowledge Library retrieval eval harness (``bench kb-retrieval``).

Covers the pure metric (``mrr_at_k``), the golden-set model's refusals, the
end-to-end deterministic run against a real ``KnowledgeStore`` + ``HybridRetriever``
via the toy embedder, per-class and abstention scoring, cross-process determinism,
and the CLI dispatch (toy path + the --real-embedder refusal when the model is
absent).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.cli_bench import bench_cmd
from kiro_crew.eval.bench.kb_retrieval import (
    KB_QUERY_CLASSES,
    KBGoldenSet,
    KBGoldenSetError,
    default_golden_set_path,
    format_kb_report,
    mrr_at_k,
    run_kb_retrieval,
)


class _Args:
    """Stand-in for the argparse namespace the dispatch receives."""

    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


# -- mrr_at_k -----------------------------------------------------------------


class TestMrrAtK:
    def test_first_rank_is_one(self) -> None:
        assert mrr_at_k(["a", "b", "c"], ["a"], 3) == 1.0

    def test_second_rank_is_half(self) -> None:
        assert mrr_at_k(["x", "a", "c"], ["a"], 3) == 0.5

    def test_third_rank_is_third(self) -> None:
        assert mrr_at_k(["x", "y", "a"], ["a"], 3) == pytest.approx(1 / 3)

    def test_outside_window_is_zero(self) -> None:
        assert mrr_at_k(["x", "y", "z", "a"], ["a"], 3) == 0.0

    def test_no_gold_is_zero(self) -> None:
        assert mrr_at_k(["a", "b"], [], 3) == 0.0

    def test_first_of_multiple_gold_counts(self) -> None:
        # Reciprocal of the FIRST gold hit, regardless of how many gold exist.
        assert mrr_at_k(["x", "g2", "g1"], ["g1", "g2"], 5) == 0.5


# -- golden set model ---------------------------------------------------------


def _write_golden(tmp_path: Path, docs: list[dict], queries: list[dict]) -> Path:
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"name": "t", "docs": docs, "queries": queries}))
    return p


class TestGoldenSet:
    def test_shipped_v1_loads_and_validates(self) -> None:
        gs = KBGoldenSet.from_json(default_golden_set_path())
        assert gs.docs and gs.queries
        for q in gs.queries:
            assert q.query_class in KB_QUERY_CLASSES

    def test_shipped_v1_preserves_supersession_chronology(self) -> None:
        # KnowledgeStore.add_item timestamps in insertion order. Older/draft evidence
        # must therefore precede its superseding/authoritative documents, or future
        # freshness-aware ranking would reward stale evidence.
        ids = [d.id for d in KBGoldenSet.from_json(default_golden_set_path()).docs]
        assert ids.index("d-feature-flag-announcement") < ids.index("d-feature-flag-retracted")
        assert (
            ids.index("d-security-mfa-proposal")
            < ids.index("d-security-mfa")
            < ids.index("d-security-mfa-audit")
        )

    def test_missing_file_refuses(self, tmp_path: Path) -> None:
        # Match the application-owned prefix, not platform-specific errno text
        # (POSIX says "No such file"; Windows says "cannot find the file").
        with pytest.raises(KBGoldenSetError, match="refusing to read the golden set"):
            KBGoldenSet.from_json(tmp_path / "nope.json")

    def test_bad_json_refuses(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        with pytest.raises(KBGoldenSetError, match="not valid JSON"):
            KBGoldenSet.from_json(p)

    def test_control_bearing_filename_is_escaped_in_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.eval.bench.kb_retrieval as kbr

        malicious = tmp_path / "bad\x1b]0;title\x07.json"
        monkeypatch.setattr(kbr, "read_text_nofollow", lambda *_a, **_kw: "{")
        with pytest.raises(KBGoldenSetError) as excinfo:
            KBGoldenSet.from_json(malicious)
        message = str(excinfo.value)
        assert "\x1b" not in message
        assert "\\x1b" in message
        assert "\\x07" in message

    def test_dangling_gold_ref_refuses(self, tmp_path: Path) -> None:
        p = _write_golden(
            tmp_path,
            docs=[{"id": "d1", "title": "t", "content": "c"}],
            queries=[
                {"id": "q1", "class": "clean_fact", "question": "?", "gold_doc_ids": ["MISSING"]}
            ],
        )
        with pytest.raises(KBGoldenSetError, match="undefined gold docs"):
            KBGoldenSet.from_json(p)

    def test_duplicate_doc_id_refuses(self, tmp_path: Path) -> None:
        p = _write_golden(
            tmp_path,
            docs=[
                {"id": "d1", "title": "t", "content": "c"},
                {"id": "d1", "title": "t2", "content": "c2"},
            ],
            queries=[{"id": "q1", "class": "clean_fact", "question": "?", "gold_doc_ids": ["d1"]}],
        )
        with pytest.raises(KBGoldenSetError, match="duplicate doc ids"):
            KBGoldenSet.from_json(p)

    def test_unknown_class_refuses(self, tmp_path: Path) -> None:
        p = _write_golden(
            tmp_path,
            docs=[{"id": "d1", "title": "t", "content": "c"}],
            queries=[{"id": "q1", "class": "not_a_class", "question": "?", "gold_doc_ids": ["d1"]}],
        )
        with pytest.raises(KBGoldenSetError, match="unknown class"):
            KBGoldenSet.from_json(p)

    def test_no_queries_refuses(self, tmp_path: Path) -> None:
        p = _write_golden(tmp_path, docs=[{"id": "d1", "title": "t", "content": "c"}], queries=[])
        with pytest.raises(KBGoldenSetError, match="no queries"):
            KBGoldenSet.from_json(p)

    def test_abstention_query_is_flagged(self, tmp_path: Path) -> None:
        p = _write_golden(
            tmp_path,
            docs=[{"id": "d1", "title": "t", "content": "c"}],
            queries=[
                {"id": "q1", "class": "abstention", "question": "?", "gold_doc_ids": []},
                # validate() refuses an all-abstention set (answerable metrics
                # would be fabricated 0.0s), so keep one answerable query.
                {"id": "q2", "class": "clean_fact", "question": "??", "gold_doc_ids": ["d1"]},
            ],
        )
        gs = KBGoldenSet.from_json(p)
        assert gs.queries[0].is_abstention is True
        assert gs.queries[1].is_abstention is False

    def test_non_dict_json_refuses(self, tmp_path: Path) -> None:
        # Valid JSON that is not an object (list/scalar/null) must refuse cleanly,
        # not crash with AttributeError on .get().
        for payload in ("[]", "null", "42", '"a string"'):
            p = tmp_path / "scalar.json"
            p.write_text(payload)
            with pytest.raises(KBGoldenSetError, match="must be a JSON object"):
                KBGoldenSet.from_json(p)

    def test_lone_surrogate_name_refuses_before_report_output(self, tmp_path: Path) -> None:
        payload = {
            "name": "\ud800",
            "docs": [{"id": "d1", "title": "t", "content": "c"}],
            "queries": [
                {
                    "id": "q1",
                    "class": "clean_fact",
                    "question": "?",
                    "gold_doc_ids": ["d1"],
                }
            ],
        }
        p = tmp_path / "surrogate-name.json"
        # ensure_ascii=True emits valid ASCII JSON whose decoded string contains
        # the lone surrogate, reproducing the later print() UnicodeEncodeError.
        p.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(KBGoldenSetError, match="name.*valid UTF-8"):
            KBGoldenSet.from_json(p)

    def test_terminal_escape_name_refuses_before_report_output(self, tmp_path: Path) -> None:
        payload = {
            "name": "\x1b]0;changed-title\x07",
            "docs": [{"id": "d1", "title": "t", "content": "c"}],
            "queries": [
                {
                    "id": "q1",
                    "class": "clean_fact",
                    "question": "?",
                    "gold_doc_ids": ["d1"],
                }
            ],
        }
        p = tmp_path / "control-name.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(KBGoldenSetError, match="name.*printable text"):
            KBGoldenSet.from_json(p)

    def test_null_docs_refuses(self, tmp_path: Path) -> None:
        # A present-but-null collection would crash the tuple comprehension with
        # TypeError; it must refuse as the documented error instead.
        p = tmp_path / "nulldocs.json"
        p.write_text('{"docs": null, "queries": []}')
        with pytest.raises(KBGoldenSetError, match="'docs' must be a list"):
            KBGoldenSet.from_json(p)

    def test_class_gold_mismatch_refuses(self, tmp_path: Path) -> None:
        # An answerable class with no gold, or abstention WITH gold, would land in
        # the wrong aggregate bucket and report a meaningless metric.
        no_gold = _write_golden(
            tmp_path,
            docs=[{"id": "d1", "title": "t", "content": "c"}],
            queries=[{"id": "q1", "class": "clean_fact", "question": "?", "gold_doc_ids": []}],
        )
        with pytest.raises(KBGoldenSetError, match="has no gold docs"):
            KBGoldenSet.from_json(no_gold)
        abst_with_gold = _write_golden(
            tmp_path,
            docs=[{"id": "d1", "title": "t", "content": "c"}],
            queries=[{"id": "q1", "class": "abstention", "question": "?", "gold_doc_ids": ["d1"]}],
        )
        with pytest.raises(KBGoldenSetError, match="abstention.*but has gold docs"):
            KBGoldenSet.from_json(abst_with_gold)

    def test_sensitive_path_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The golden path is argv-supplied and agent-reachable, so it must go
        # through the sensitive-path gate. Inject the verdict on the classifier the
        # guard consults, rather than relocating HOME/USERPROFILE -- moving the real
        # protection boundary is a test side effect and does not exercise the gate
        # against the operator's actual home.
        import kiro_crew.security as security

        secret = tmp_path / "looks_ok.json"
        secret.write_text('{"docs": [], "queries": []}')
        real_is_sensitive = security.is_sensitive_path
        monkeypatch.setattr(
            security,
            "is_sensitive_path",
            lambda s: str(secret) in s or real_is_sensitive(s),
        )
        with pytest.raises(KBGoldenSetError, match="protected location"):
            KBGoldenSet.from_json(secret)


# -- end-to-end run against the real store + retriever (toy embedder) ---------


class TestRunKbRetrieval:
    def test_shipped_set_runs_and_scores(self) -> None:
        gs = KBGoldenSet.from_json(default_golden_set_path())
        report = run_kb_retrieval(gs)
        assert len(report.results) == len(gs.queries)
        head = report.headline(3)
        for key in ("recall_any@3", "mrr@3", "ndcg@3"):
            assert 0.0 <= head[key] <= 1.0

    def test_toy_embedder_finds_clean_fact(self) -> None:
        gs = KBGoldenSet.from_json(default_golden_set_path())
        report = run_kb_retrieval(gs)
        clean = [r for r in report.results if r.query_class == "clean_fact"]
        assert clean
        assert any(r.recall_any.get(5, 0.0) == 1.0 for r in clean)

    def test_abstention_scored_separately(self) -> None:
        gs = KBGoldenSet.from_json(default_golden_set_path())
        report = run_kb_retrieval(gs)
        abst = [r for r in report.results if r.is_abstention]
        assert abst
        for r in abst:
            assert r.abstained in (0.0, 1.0)
        by_class = report.by_class(3)
        if "abstention" in by_class:
            assert "abstention_rate" in by_class["abstention"]
            assert "recall_any" not in by_class["abstention"]

    def test_deterministic_across_runs(self) -> None:
        gs = KBGoldenSet.from_json(default_golden_set_path())
        h1 = run_kb_retrieval(gs).headline(3)
        h2 = run_kb_retrieval(gs).headline(3)
        assert h1 == h2

    def test_keyword_only_mode_runs(self) -> None:
        gs = KBGoldenSet.from_json(default_golden_set_path())
        report = run_kb_retrieval(gs, use_embeddings=False)
        assert len(report.results) == len(gs.queries)

    def test_format_report_mentions_classes(self) -> None:
        gs = KBGoldenSet.from_json(default_golden_set_path())
        text = format_kb_report(run_kb_retrieval(gs), k=3)
        assert "KB retrieval eval" in text
        assert "HEADLINE" in text
        assert "clean_fact" in text

    def test_non_default_k_is_computed_not_zero(self) -> None:
        # A cut-off outside DEFAULT_KB_K_VALUES must be explicitly computed, or the
        # headline defaults it to 0.0 -- a false-zero benchmark result. Passing
        # k_values including the requested k is what the CLI does; verify the dict
        # actually carries the key so headline(k) is real.
        gs = KBGoldenSet.from_json(default_golden_set_path())
        report = run_kb_retrieval(gs, k_values=(1, 2, 3, 5, 10))
        assert 2 in report.k_values
        for r in report.results:
            assert 2 in r.recall_any  # key present -> headline(2) is measured, not 0.0
        # headline(2) is real; it must NOT silently return 0.0.
        assert report.headline(2)  # does not raise

    def test_uncomputed_k_fails_loud(self) -> None:
        # The old .get(k, 0.0) default silently fabricated a 0.0 for an uncomputed
        # cut-off; now it must raise rather than report a fake low score.
        gs = KBGoldenSet.from_json(default_golden_set_path())
        report = run_kb_retrieval(gs)  # default k_values = (1, 3, 5, 10)
        with pytest.raises(KBGoldenSetError, match="was not computed"):
            report.headline(2)
        with pytest.raises(KBGoldenSetError, match="was not computed"):
            report.by_class(2)

    def test_empty_embedding_refuses_when_enabled(self) -> None:
        # With embeddings enabled, an embedder that returns empty must fail closed
        # -- otherwise a --real-embedder run silently reports keyword-only metrics
        # under a semantic embedder label. The guard now covers both doc ingest and
        # query search (one fail-closed wrapper). The refusal path must ALSO close
        # the store and remove its temp dir (an open SQLite handle breaks
        # TemporaryDirectory.cleanup() on Windows -- WinError 32).
        import glob
        import tempfile

        gs = KBGoldenSet.from_json(default_golden_set_path())
        before = set(glob.glob(str(Path(tempfile.gettempdir()) / "kb_eval_*")))
        with pytest.raises(KBGoldenSetError, match="empty vector"):
            run_kb_retrieval(gs, embed_fn=lambda _t: [], embedder_id="fake")
        after = set(glob.glob(str(Path(tempfile.gettempdir()) / "kb_eval_*")))
        assert after <= before  # refusal path left no temp dir behind
        # And keyword-only mode (embeddings disabled) runs fine with no embedder.
        report = run_kb_retrieval(gs, embed_fn=lambda _t: [], use_embeddings=False)
        assert len(report.results) == len(gs.queries)

    def test_keyword_only_run_has_keyword_only_identity(self) -> None:
        # A --no-embeddings run must NOT carry a semantic (or toy) embedder label:
        # the printed identity has to reflect that the vector leg was off.
        gs = KBGoldenSet.from_json(default_golden_set_path())
        report = run_kb_retrieval(gs, embedder_id="qwen3-embedding:0.6b", use_embeddings=False)
        assert "keyword-only" in report.embedder_id.lower()
        assert "qwen" not in report.embedder_id.lower()
        assert "keyword-only" in format_kb_report(report).lower()

    def test_temp_dir_cleaned_up_after_run(self) -> None:
        # A completed run must close the store and remove its temp dir (the store
        # holds an open SQLite handle; on Windows an un-closed handle would make
        # TemporaryDirectory.cleanup() raise). Assert no kb_eval_* dir lingers.
        import glob
        import tempfile

        gs = KBGoldenSet.from_json(default_golden_set_path())
        before = set(glob.glob(str(Path(tempfile.gettempdir()) / "kb_eval_*")))
        run_kb_retrieval(gs)
        after = set(glob.glob(str(Path(tempfile.gettempdir()) / "kb_eval_*")))
        assert after <= before  # no new kb_eval_ temp dir left behind


# -- CLI dispatch -------------------------------------------------------------


class TestCli:
    def test_toy_path_returns_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = bench_cmd(
            _Args(
                bench_action="kb-retrieval",
                golden=None,
                k=3,
                real_embedder=False,
                no_embeddings=False,
            )
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "KB retrieval eval" in out
        assert "toy" in out.lower()

    def test_missing_golden_refuses(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = bench_cmd(
            _Args(
                bench_action="kb-retrieval",
                golden=str(tmp_path / "nope.json"),
                k=3,
                real_embedder=False,
                no_embeddings=False,
            )
        )
        assert rc == 1
        assert "refusing to run" in capsys.readouterr().out

    def test_real_embedder_refuses_when_absent(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.knowledge.embedder as emb

        monkeypatch.setattr(emb.InProcessEmbedder, "is_available", lambda self: False)
        rc = bench_cmd(
            _Args(
                bench_action="kb-retrieval",
                golden=None,
                k=3,
                real_embedder=True,
                no_embeddings=False,
            )
        )
        assert rc == 1
        assert "not resident" in capsys.readouterr().out

    def test_real_embedder_reports_actual_model_identity(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A custom embedding model must be labeled truthfully in the report.

        Regression: the CLI hardcoded ``embedder_id = "qwen3-embedding:0.6b"``,
        so a --real-embedder run whose ``InProcessEmbedder`` resolved a
        different model still reported Qwen3. The label must come from
        ``embedder.model``.
        """
        import kiro_crew.knowledge.embedder as emb
        from kiro_crew.eval.bench.toy_embedder import toy_embed_fn

        fake_model = "custom-embedding:test-9b"
        deterministic = toy_embed_fn()

        monkeypatch.setattr(emb.InProcessEmbedder, "is_available", lambda self: True)
        monkeypatch.setattr(emb.InProcessEmbedder, "model", property(lambda self: fake_model))
        monkeypatch.setattr(emb.InProcessEmbedder, "embed", lambda self, text: deterministic(text))
        rc = bench_cmd(
            _Args(
                bench_action="kb-retrieval",
                golden=None,
                k=3,
                real_embedder=True,
                no_embeddings=False,
            )
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert fake_model in out
        assert "qwen" not in out.lower()


class TestRound7Fixes:
    """Regression tests for the head-03b9a4858 GPT review round."""

    def test_huge_k_does_not_crash(self) -> None:
        """An attacker-sized -k must not overflow the SQL LIMIT arithmetic.

        Regression: ``limit = max(k_values)`` flowed an unbounded cut-off into
        the store's SQL LIMIT, crashing with an uncaught OverflowError for
        e.g. ``-k 4611686018427387904``. The retrieval depth is now clamped to
        the corpus size (a deeper LIMIT cannot change any ranking).
        """
        gs = KBGoldenSet.from_json(default_golden_set_path())
        huge = 2**62
        report = run_kb_retrieval(gs, k_values=(3, huge))
        # The requested cut-off is still computed (scoring is over the ranked
        # list, which is at most corpus-sized) -- never silently dropped.
        assert huge in report.k_values
        head = report.headline(3)
        assert 0.0 <= head["recall_any@3"] <= 1.0

    def test_recall_micro_reported(self) -> None:
        """recall_micro must appear per query, in by_class, headline, and text."""
        gs = KBGoldenSet.from_json(default_golden_set_path())
        report = run_kb_retrieval(gs)
        for r in report.results:
            assert set(r.recall_micro.keys()) == set(report.k_values)
        head = report.headline(3)
        assert "recall_micro@3" in head
        by_class = report.by_class(3)
        answerable = [c for c in by_class if c != "abstention"]
        assert answerable and all("recall_micro" in by_class[c] for c in answerable)
        assert "recall_micro" in format_kb_report(report, k=3)

    def test_recall_micro_is_fractional_for_partial_hit(self) -> None:
        """A two-gold query with one hit scores 0.5 micro, not the 1/0 of any/all."""
        from kiro_crew.eval.bench.kb_retrieval import KBQueryResult, KBRetrievalReport
        from kiro_crew.eval.bench.retrieval import recall_micro_at_k

        ranked, gold = ("g1", "z1", "z2"), ("g1", "g2")
        micro = recall_micro_at_k(ranked, gold, 3)
        assert micro == 0.5  # the scorer itself is fractional
        r = KBQueryResult(
            query_id="q-x",
            query_class="multi_hop",
            is_abstention=False,
            recall_any={3: 1.0},
            recall_all={3: 0.0},
            recall_micro={3: micro},
            ndcg={3: 0.5},
            mrr={3: 1.0},
        )
        report = KBRetrievalReport(golden_set="t", embedder_id="toy", k_values=(3,))
        report.results.append(r)
        head = report.headline(3)
        assert head["recall_micro@3"] == 0.5
        assert head["recall_any@3"] == 1.0
        assert head["recall_all@3"] == 0.0

    def test_store_error_is_a_refusal_not_a_traceback(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sqlite failure (e.g. full temp volume) exits 1 with a refusal message.

        Regression: ``run_kb_retrieval``'s store build could raise
        ``sqlite3.OperationalError`` past the CLI dispatch, printing an uncaught
        traceback instead of the deliberate-refusal message every other failure
        path in ``_kb_retrieval`` produces.
        """
        import kiro_crew.eval.bench.kb_retrieval as kbr
        from kiro_crew._sqlite_compat import sqlite3

        def _boom(*_a: object, **_k: object) -> None:
            raise sqlite3.OperationalError("database or disk is full")

        monkeypatch.setattr(kbr, "run_kb_retrieval", _boom)
        # Route the CLI through the patched module attribute.
        import kiro_crew.cli_bench as cb

        rc = cb.bench_cmd(
            _Args(
                bench_action="kb-retrieval",
                golden=None,
                k=3,
                real_embedder=False,
                no_embeddings=False,
            )
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "refusing to run" in out
        assert "disk is full" in out

    def test_invalid_utf8_golden_is_a_refusal_not_a_traceback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A golden file with broken UTF-8 exits 1 with a refusal message.

        Regression: ``KBGoldenSet.from_json`` decodes the file as UTF-8; invalid
        bytes raise ``UnicodeDecodeError``, which escaped the CLI's refusal
        handler and printed an uncaught traceback instead of the deliberate
        ``refusing to run`` message every other malformed input produces.
        """
        bad = tmp_path / "bad-golden.json"
        bad.write_bytes(b'{"name": "x", "docs": [], "queries": [\xff\xfe]}')
        rc = bench_cmd(
            _Args(
                bench_action="kb-retrieval",
                golden=str(bad),
                k=3,
                real_embedder=False,
                no_embeddings=False,
            )
        )
        assert rc == 1
        assert "refusing to run" in capsys.readouterr().out

    def test_all_abstention_golden_refuses(self, tmp_path: Path) -> None:
        """A golden set with only abstention queries must refuse, not report 0.0s.

        Regression: ``headline()`` averages the answerable population; with zero
        answerable queries ``_mean([])`` returns 0.0 for all five answerable
        metrics, fabricating scores for a population that was never measured
        (the same invariant ``_require_k`` enforces for uncomputed cut-offs:
        an unmeasurable metric must be absent or refused, never 0.0).
        """
        p = _write_golden(
            tmp_path,
            docs=[{"id": "d1", "title": "t", "content": "c"}],
            queries=[
                {"id": "q1", "class": "abstention", "question": "?", "gold_doc_ids": []},
                {"id": "q2", "class": "abstention", "question": "??", "gold_doc_ids": []},
            ],
        )
        with pytest.raises(KBGoldenSetError, match="no answerable queries"):
            KBGoldenSet.from_json(p)

    def test_deeply_nested_golden_is_a_refusal_not_a_traceback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A golden file of deeply nested JSON exits 1 with a refusal message.

        Regression: ``json.loads`` raises ``RecursionError`` on ~10k nested
        arrays; it escaped ``from_json``'s JSONDecodeError conversion and the
        CLI's ``(KBGoldenSetError, UnicodeError)`` refusal handlers, printing
        an uncaught traceback. ``from_json`` now converts it at the source so
        every caller refuses cleanly, whatever parse-time class the payload
        provokes.
        """
        deep = tmp_path / "deep-golden.json"
        deep.write_text("[" * 10000 + "]" * 10000)
        with pytest.raises(KBGoldenSetError, match="not valid JSON|too deeply nested"):
            KBGoldenSet.from_json(deep)
        rc = bench_cmd(
            _Args(
                bench_action="kb-retrieval",
                golden=str(deep),
                k=3,
                real_embedder=False,
                no_embeddings=False,
            )
        )
        assert rc == 1
        assert "refusing to run" in capsys.readouterr().out

    def test_nonregular_golden_refuses_without_reading(self) -> None:
        """A non-regular golden path (device/FIFO) refuses without consuming it.

        Regression: ``--golden /dev/zero`` reached an unbounded ``read()`` and
        exhausted memory. The shared reader opens nonblocking, classifies the SAME
        descriptor with fstat, and refuses before reading any bytes.
        """
        if not Path("/dev/zero").exists():
            pytest.skip("no /dev/zero on this platform")
        with pytest.raises(KBGoldenSetError, match="not a regular file"):
            KBGoldenSet.from_json("/dev/zero")

    def test_oversized_golden_refuses(self, tmp_path: Path) -> None:
        """A golden file over the size cap refuses instead of being slurped.

        Uses a sparse file so the test costs no real disk; the refusal must
        come from the stat-based size check, before the read.
        """
        big = tmp_path / "big-golden.json"
        with open(big, "wb") as fh:
            fh.seek(64 * 1024 * 1024)  # 64 MiB, over the 16 MiB cap
            fh.write(b"\0")
        with pytest.raises(KBGoldenSetError, match="too large"):
            KBGoldenSet.from_json(big)
