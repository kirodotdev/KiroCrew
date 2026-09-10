"""Deterministic coverage for the member-memory V2 retrieval benchmark.

``kiro_crew.eval.bench.member_v2`` is the CI-run, model-backed gate measurement
(``scripts/ci-member-memory-benchmark.py``); this file drives the same
``evaluate()`` entry point with a deterministic FAKE embedder instead of a real
model, so the report-shape and structural-failure logic is covered without a
model download or network access. ``main()``'s CLI wiring is also exercised
with a fake stand-in for ``LlamaCppEmbedder`` (never the real class -- no
model is ever loaded or downloaded).
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import struct
from pathlib import Path

import pytest

from kiro_crew.config.loader import config_dir
from kiro_crew.eval.bench import member_v2
from kiro_crew.eval.bench.member_v2 import (
    _bounded_oversized_result,
    _insert,
    _new_store,
    evaluate,
    ranking_metrics,
    structural_failures,
)


def _fake_vector(text: str, dim: int = 8) -> list[float]:
    """A deterministic, text-derived unit vector -- no model, no randomness."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    raw = [b / 255.0 for b in digest[:dim]]
    norm = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [v / norm for v in raw]


def _make_embed(missing: set[str] | None = None):
    """A fake embedder; texts in *missing* report as unembeddable (``None``)."""
    missing = missing or set()

    def _embed(text: str) -> list[float] | None:
        if text in missing:
            return None
        return _fake_vector(text)

    return _embed


@pytest.fixture
def home() -> Path:
    """The isolated per-test data home ``config_dir()`` already resolves to.

    ``test/conftest.py`` autouse-pins ``KIROCREW_HOME`` per test, so this is
    never the operator's real home; it is returned rather than re-derived so a
    test that also opens a store under it addresses the identical path.
    """
    return config_dir()


class TestEvaluateReportShape:
    """``evaluate()`` against a deterministic fake embedder in an isolated home."""

    def test_full_report_shape(self, home: Path) -> None:
        report = evaluate(_make_embed(), home)

        assert report["schema_version"] == 1
        assert isinstance(report["policy_revision"], str) and report["policy_revision"]
        assert isinstance(report["corpus_sha256"], str) and len(report["corpus_sha256"]) == 64
        assert isinstance(report["protocol"], str)
        assert report["limitations"] and all(isinstance(x, str) for x in report["limitations"])

        modes = report["modes"]
        assert [m["mode"] for m in modes] == [
            "full_vectors",
            "short_vectors_missing",
            "no_embeddings",
        ]
        for mode_report in modes:
            assert mode_report["documents"] == 100
            assert mode_report["queries"] == 50
            assert mode_report["pairs"] == 5000
            admission = mode_report["admission"]
            for key in ("tp", "fp", "fn", "tn", "precision", "recall"):
                assert key in admission
            assert 0.0 <= admission["precision"] <= 1.0
            assert 0.0 <= admission["recall"] <= 1.0
            macro = mode_report["macro_metrics"]
            assert set(macro.keys()) == {"ranked", "context"}
            for phase in ("ranked", "context"):
                for metric in ("precision_returned", "fragment_recall", "topic_hit", "ndcg_at_8"):
                    value = macro[phase][metric]
                    assert 0.0 <= value <= 1.0, (mode_report["mode"], phase, metric, value)
            assert mode_report["context_bounds_passed"] is True
            assert mode_report["context_max_chars"] >= 0
            assert len(mode_report["per_query"]) == 50

        edges = report["edge_cases"]
        for language in ("zh", "ja", "ko"):
            case = edges[language]
            assert case["episode_hit"] is True
            assert case["fact_hit"] is True
            assert case["within_budget"] is True
        for case in ("bounded_oversized_isolated", "bounded_oversized", "forgotten_excluded"):
            assert edges[case]["passed"] is True

        # A sound report from a sound run must have no structural failures.
        assert structural_failures(report) == []

    def test_short_vectors_missing_mode_still_embeds_queries(self, home: Path) -> None:
        # `mode == "short_vectors_missing"` withholds vectors only for the
        # "short" ingested fragments (see `_mode_report`'s per-fragment branch);
        # queries are still embedded for every mode except "no_embeddings". A
        # deterministic embedder that never returns None for real corpus text
        # is enough to exercise that branch end to end.
        report = evaluate(_make_embed(), home)
        mode_report = next(m for m in report["modes"] if m["mode"] == "short_vectors_missing")
        assert mode_report["admission"]["tp"] + mode_report["admission"]["fn"] == 100

    def test_no_embeddings_mode_runs_with_no_vectors_at_all(self, home: Path) -> None:
        report = evaluate(_make_embed(), home)
        mode_report = next(m for m in report["modes"] if m["mode"] == "no_embeddings")
        # Every query is still scored (lexical/relevance-filter path only).
        assert len(mode_report["per_query"]) == 50
        assert mode_report["context_bounds_passed"] is True


class TestEvaluateRefusals:
    def test_embed_returning_none_for_corpus_text_raises(self, home: Path) -> None:
        from kiro_crew.eval.bench.admission_corpus import ADMISSION_TOPICS

        unembeddable = {ADMISSION_TOPICS[0].query}
        with pytest.raises(RuntimeError, match="Model failed to embed corpus input"):
            evaluate(_make_embed(missing=unembeddable), home)

    def test_embed_returning_non_finite_value_raises(self, home: Path) -> None:
        from kiro_crew.eval.bench.admission_corpus import ADMISSION_TOPICS

        target_text = ADMISSION_TOPICS[0].query

        def _embed(text: str) -> list[float] | None:
            if text == target_text:
                return [math.nan, 0.0]
            return _fake_vector(text)

        with pytest.raises(RuntimeError, match="Model failed to embed corpus input"):
            evaluate(_embed, home)

    def test_mismatched_embedding_dimensions_raise(self, home: Path) -> None:
        from kiro_crew.eval.bench.admission_corpus import ADMISSION_TOPICS

        target_text = ADMISSION_TOPICS[0].query

        def _embed(text: str) -> list[float] | None:
            if text == target_text:
                return _fake_vector(text, dim=4)
            return _fake_vector(text, dim=8)

        with pytest.raises(RuntimeError, match="Embedding dimensions changed"):
            evaluate(_embed, home)


class TestNewStoreAndInsertRefusals:
    """Direct unit coverage for `_new_store`'s and `_insert`'s own guard rails."""

    def test_new_store_refuses_a_non_v2_policy(self, home: Path, monkeypatch) -> None:
        # `_new_store` always writes a well-formed V2 manifest, so the only way
        # to exercise its own defensive check is to make the freshly-opened
        # store itself misreport. `algorithm_version` is a read-only property;
        # patch the underlying class attribute for the duration of this call.
        from kiro_crew.vector_memory import VectorMemoryStore

        monkeypatch.setattr(VectorMemoryStore, "algorithm_version", property(lambda self: "v1"))
        with pytest.raises(RuntimeError, match="did not open the owned V2 policy"):
            _new_store(home, "eval-broken-policy", dim=4)

    def test_insert_raises_when_write_episodic_refuses(self, home: Path) -> None:
        store = _new_store(home, "eval-insert-refusal", dim=4)
        try:
            with pytest.raises(RuntimeError, match="Corpus ingestion refused"):
                # Below the store's 10-char episodic floor -- write_episodic
                # returns False rather than raising.
                _insert(store, "tiny", "bad-fragment", [0.1, 0.2, 0.3, 0.4])
        finally:
            store.close()


class TestMainCli:
    """`main()`'s CLI wiring, using a fake stand-in for `LlamaCppEmbedder`.

    No real model is ever constructed, loaded, or downloaded: the fake class
    below has the same shape (`wait_ready`, `embed`, `close`, `model_id`,
    `dim`) and is substituted for the real class via monkeypatch before
    `main()` imports it.
    """

    class _FakeBackend:
        def __init__(self, model_path: Path) -> None:
            self.model_path = model_path
            self.model_id = "fake-test-model"
            self.dim = 8
            self.closed = False

        def wait_ready(self, timeout: float) -> bool:
            return True

        def embed(self, text: str, **_: object) -> list[float]:
            return _fake_vector(text, dim=self.dim)

        def close(self) -> None:
            self.closed = True

    def test_main_requires_an_existing_model_file(self, tmp_path: Path, monkeypatch) -> None:
        missing_model = tmp_path / "does-not-exist.gguf"
        out_json = tmp_path / "out.json"
        monkeypatch.setattr(
            "sys.argv",
            ["member_v2", "--model-path", str(missing_model), "--json", str(out_json)],
        )
        with pytest.raises(SystemExit):
            member_v2.main()
        assert not out_json.exists()

    def test_main_writes_a_report_with_a_fake_backend(
        self, tmp_path: Path, home: Path, monkeypatch, capsys
    ) -> None:
        model_path = tmp_path / "fake-model.gguf"
        model_path.write_bytes(b"not a real model, just needs to exist")
        out_json = tmp_path / "report.json"
        skip_download_before = os.environ.get("KIROCREW_SKIP_MODEL_DOWNLOAD")

        monkeypatch.setattr(
            "kiro_crew.embeddings.LlamaCppEmbedder", self._FakeBackend, raising=True
        )
        monkeypatch.setattr(
            "sys.argv",
            ["member_v2", "--model-path", str(model_path), "--json", str(out_json)],
        )

        member_v2.main()

        assert out_json.exists()
        written = json.loads(out_json.read_text(encoding="utf-8"))
        assert written["model"]["model_id"] == "fake-test-model"
        assert written["model"]["dimension"] == 8
        assert len(written["model"]["sha256"]) == 64
        assert structural_failures(written) == []

        captured = capsys.readouterr()
        assert "full_vectors" in captured.out

        # KIROCREW_HOME is restored to what it was before main() ran (this
        # test's own isolated home), not leaked to main()'s temporary override.
        assert os.environ.get("KIROCREW_HOME") == str(home)
        assert os.environ.get("KIROCREW_SKIP_MODEL_DOWNLOAD") == skip_download_before


class TestStructuralFailures:
    """Mutating a sound report must surface the specific failing check."""

    @pytest.fixture(scope="class")
    def sound_report(self, request) -> dict:
        # A fresh isolated home per class-scoped call would fight the
        # function-scoped autouse KIROCREW_HOME fixture, so this fixture stays
        # function-scoped in effect: pytest reuses the outer `home` fixture's
        # value only within one test's call graph. Building the report once per
        # test (cheap: 8-dim fake vectors, no model) keeps every mutation test
        # independent and avoids sharing mutable state across tests.
        return None

    def _sound(self, home: Path) -> dict:
        return evaluate(_make_embed(), home)

    def test_context_bounds_failure_is_named_by_mode(self, home: Path) -> None:
        report = self._sound(home)
        assert structural_failures(report) == []
        mutated = copy.deepcopy(report)
        mutated["modes"][0]["context_bounds_passed"] = False
        failures = structural_failures(mutated)
        assert f"{mutated['modes'][0]['mode']}: context bounds" in failures

    def test_missing_mode_is_treated_as_a_bounds_failure(self, home: Path) -> None:
        report = self._sound(home)
        mutated = copy.deepcopy(report)
        removed = mutated["modes"].pop(0)
        failures = structural_failures(mutated)
        assert f"{removed['mode']}: context bounds" in failures

    @pytest.mark.parametrize("check", ["episode_hit", "fact_hit", "within_budget"])
    def test_cjk_edge_case_failure_is_named(self, home: Path, check: str) -> None:
        report = self._sound(home)
        mutated = copy.deepcopy(report)
        mutated["edge_cases"]["zh"][check] = False
        failures = structural_failures(mutated)
        assert f"zh: {check}" in failures

    @pytest.mark.parametrize("case", ["bounded_oversized_isolated", "forgotten_excluded"])
    def test_binary_edge_case_failure_is_named(self, home: Path, case: str) -> None:
        report = self._sound(home)
        mutated = copy.deepcopy(report)
        mutated["edge_cases"][case]["passed"] = False
        failures = structural_failures(mutated)
        assert case in failures

    @pytest.mark.parametrize(
        "check", ["total_chars_matches", "within_context_cap", "within_transport_budget"]
    )
    def test_bounded_oversized_check_failure_is_named(self, home: Path, check: str) -> None:
        report = self._sound(home)
        mutated = copy.deepcopy(report)
        mutated["edge_cases"]["bounded_oversized"]["checks"][check] = False
        failures = structural_failures(mutated)
        assert f"bounded_oversized: {check}" in failures

    def test_missing_edge_cases_key_fails_every_edge_check(self, home: Path) -> None:
        report = self._sound(home)
        mutated = copy.deepcopy(report)
        mutated["edge_cases"] = {}
        failures = structural_failures(mutated)
        # Every language/case check is reported missing rather than raising.
        assert any(failure.startswith("zh:") for failure in failures)
        assert "forgotten_excluded" in failures
        assert "bounded_oversized: total_chars_matches" in failures


class TestRankingMetrics:
    def test_perfect_ranking_scores_maximally(self) -> None:
        ids = ["a", "b", "c", "d"]
        relevant = {"a", "b"}
        metrics = ranking_metrics(ids, relevant, k=4)
        assert metrics["precision_returned"] == pytest.approx(0.5)
        assert metrics["fragment_recall"] == pytest.approx(1.0)
        assert metrics["topic_hit"] == 1.0
        # a, b lead -> ideal DCG ordering achieved -> ndcg == 1.0
        assert metrics["ndcg_at_8"] == pytest.approx(1.0)

    def test_no_hits_scores_zero(self) -> None:
        metrics = ranking_metrics(["x", "y", "z"], {"a", "b"}, k=8)
        assert metrics == {
            "precision_returned": 0.0,
            "fragment_recall": 0.0,
            "topic_hit": 0.0,
            "ndcg_at_8": 0.0,
        }

    def test_empty_selection_avoids_division_by_zero(self) -> None:
        metrics = ranking_metrics([], {"a"}, k=8)
        assert metrics["precision_returned"] == 0.0
        assert metrics["fragment_recall"] == 0.0
        assert metrics["topic_hit"] == 0.0

    def test_worse_rank_position_scores_lower_ndcg(self) -> None:
        relevant = {"target"}
        first = ranking_metrics(["target", "x", "y"], relevant, k=8)
        later = ranking_metrics(["x", "y", "target"], relevant, k=8)
        assert first["ndcg_at_8"] == pytest.approx(1.0)
        assert later["ndcg_at_8"] < first["ndcg_at_8"]

    def test_k_limits_the_selected_window(self) -> None:
        # Only the top-1 is considered; the relevant id sits at position 2.
        metrics = ranking_metrics(["x", "target"], {"target"}, k=1)
        assert metrics["fragment_recall"] == 0.0
        assert metrics["topic_hit"] == 0.0


class TestBoundedOversizedResult:
    """Direct tests against a hand-built context dict -- no store, no home."""

    def _context(self, *, episode_ids: list[str], text_truncated: bool, total_chars: int) -> dict:
        episodic_context = "".join(f"[memory:{eid}]" for eid in episode_ids)
        episodes = [
            {
                "id": eid,
                "text": "some retained text" if eid == "oversized" else "other text",
                "text_truncated": text_truncated if eid == "oversized" else False,
                "retrieval": {"admitted": True},
            }
            for eid in episode_ids
        ]
        return {
            "retrieval": {"episodes": episodes},
            "episodic_context": episodic_context,
            "semantic_context": "",
            "lessons_context": "",
            "total_chars": total_chars,
        }

    def test_all_checks_pass_for_a_well_formed_context(self) -> None:
        context = self._context(
            episode_ids=["oversized"], text_truncated=True, total_chars=len("[memory:oversized]")
        )
        result = _bounded_oversized_result(context)
        assert result["passed"] is True
        assert result["checks"]["selected"] is True
        assert result["checks"]["nonempty_text"] is True
        assert result["checks"]["text_truncated"] is True
        assert result["checks"]["rendered_id"] is True
        assert result["checks"]["total_chars_matches"] is True
        assert result["checks"]["within_context_cap"] is True
        assert result["checks"]["within_transport_budget"] is True
        assert result["checks"]["transport_retained_id"] is True
        assert result["selected_ids"] == ["oversized"]
        assert result["selected_count"] == 1
        assert result["context_cap"] == 200

    def test_missing_oversized_row_fails_selected_and_downstream_checks(self) -> None:
        context = self._context(
            episode_ids=["other"], text_truncated=False, total_chars=len("[memory:other]")
        )
        result = _bounded_oversized_result(context)
        assert result["passed"] is False
        assert result["checks"]["selected"] is False
        assert result["checks"]["nonempty_text"] is False
        assert result["checks"]["text_truncated"] is False
        assert result["checks"]["rendered_id"] is False

    def test_mismatched_total_chars_fails_that_check_only(self) -> None:
        context = self._context(episode_ids=["oversized"], text_truncated=True, total_chars=999)
        result = _bounded_oversized_result(context)
        assert result["checks"]["total_chars_matches"] is False
        assert result["passed"] is False
        # Independent of the mismatch: selection and rendering still hold.
        assert result["checks"]["selected"] is True
        assert result["checks"]["rendered_id"] is True

    def test_over_cap_actual_chars_fails_within_context_cap(self) -> None:
        # actual_chars is derived from the *_context strings, not total_chars,
        # so padding episodic_context past 200 chars fails the cap check while
        # total_chars is kept consistent with the (now larger) actual size.
        padding = "x" * 250
        context = self._context(episode_ids=["oversized"], text_truncated=True, total_chars=0)
        context["episodic_context"] = context["episodic_context"] + padding
        context["total_chars"] = len(context["episodic_context"])
        result = _bounded_oversized_result(context)
        assert result["checks"]["within_context_cap"] is False
        assert result["checks"]["total_chars_matches"] is True
        assert result["passed"] is False


def _write_recall_json_struct_pack_smoke() -> None:
    """Confidence check that struct.pack round-trips a fake vector (used by `_insert`)."""
    vector = _fake_vector("smoke")
    blob = struct.pack(f"{len(vector)}f", *vector)
    assert len(blob) == len(vector) * 4
