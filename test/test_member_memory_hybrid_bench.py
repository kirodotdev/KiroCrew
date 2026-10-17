"""Evaluation integrity checks; these never pretend synthetic vectors measure semantics."""

from __future__ import annotations

import math

import pytest

from kiro_crew.eval.bench import member_v2


def test_ranking_metrics_penalize_false_positives_missing_evidence_and_bad_order():
    result = member_v2.ranking_metrics(["wrong", "gold-a"], {"gold-a", "gold-b"})
    assert result["precision_returned"] == 0.5
    assert result["fragment_recall"] == 0.5
    assert result["topic_hit"] == 1
    assert result["ndcg_at_8"] == pytest.approx((1 / math.log2(3)) / (1 + 1 / math.log2(3)))
    assert member_v2.ranking_metrics([], {"gold-a"}) == {
        "precision_returned": 0,
        "fragment_recall": 0,
        "topic_hit": 0,
        "ndcg_at_8": 0,
    }


def test_evaluation_refuses_missing_model_evidence_before_opening_memory(tmp_path):
    with pytest.raises(RuntimeError, match="failed to embed corpus input 0"):
        member_v2.evaluate(lambda _: None, tmp_path)
    assert not (tmp_path / "memory_stores").exists()


def test_insert_assigns_identity_after_write_normalizes_trailing_space(tmp_path, monkeypatch):
    # Match the benchmark CLI: the positive named-store predicate is rooted at
    # KIROCREW_HOME, so an arbitrary tmp_path is deliberately a legacy V1 file.
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    store = member_v2._new_store(tmp_path, "normalized-fixture", 1)
    try:
        member_v2._insert(store, "oversized text " + "detail " * 20, "oversized", None)
        row = store.db.execute(
            "SELECT id, text FROM memory_items WHERE id = 'oversized'"
        ).fetchone()
        assert row is not None
        assert row["text"].endswith("detail")
    finally:
        store.close()


def test_edge_report_isolates_oversized_retention_and_keeps_competing_observation(
    monkeypatch, tmp_path
):
    class Store:
        def __init__(self):
            self.identities = []

        def write_episodic(self, *_args, **_kwargs):
            return True

        def set_semantic(self, *_args):
            return None

        def recall(self, query, *, cap):
            if query != "budgettoken memory":
                language = {"灯塔部署位置": "zh", "灯台デプロイ先": "ja", "등대 배포 위치": "ko"}[
                    query
                ]
                return _context(
                    episodes=[{"id": language}],
                    facts=[{"key": f"project.{language}"}],
                    episodic_context=f"[memory:{language}] context\n",
                    total_chars=20,
                )
            if "small" in self.identities:
                return _context(
                    episodes=[{"id": "small", "text": "small"}],
                    episodic_context="[memory:small] small\n",
                    total_chars=21,
                )
            return _context(
                episodes=[
                    {
                        "id": "oversized",
                        "text": "bounded excerpt",
                        "text_truncated": True,
                    }
                ],
                episodic_context="[memory:oversized] bounded excerpt\n",
                total_chars=35,
            )

        def delete_episodic(self, identity):
            assert identity == "small"

        def search_episodic(self, **_kwargs):
            return []

        def close(self):
            return None

    def _context(*, episodes, episodic_context, total_chars, facts=None):
        return {
            "semantic_context": "",
            "episodic_context": episodic_context,
            "lessons_context": "",
            "retrieval": {"facts": facts or [], "episodes": episodes},
            "semantic_chars": 0,
            "episodic_chars": len(episodic_context),
            "lessons_chars": 0,
            "total_chars": total_chars,
        }

    store = Store()

    def insert(_store, _text, identity, _vector):
        store.identities.append(identity)

    monkeypatch.setattr(member_v2, "_new_store", lambda *_args: store)
    monkeypatch.setattr(member_v2, "_insert", insert)

    report = member_v2._edge_report(tmp_path, {"seed": [1.0]})

    assert report["bounded_oversized_isolated"]["passed"] is True
    competing = report["bounded_oversized"]
    assert competing["passed"] is False
    assert competing["selected_ids"] == ["small"]
    assert competing["checks"]["selected"] is False
    assert competing["checks"]["within_context_cap"] is True
    assert competing["checks"]["within_transport_budget"] is True
    assert competing["checks"]["transport_retained_id"] is False


@pytest.mark.parametrize("broken", [None, "context", "forgotten", "oversized", "transport"])
def test_ci_structural_gate_preserves_quality_observations_and_refuses_contract_failures(broken):
    edges = {
        language: {"episode_hit": True, "fact_hit": True, "within_budget": True}
        for language, _query, _text in member_v2.CJK_CASES
    }
    edges.update(
        {
            "bounded_oversized_isolated": {"passed": True},
            "forgotten_excluded": {"passed": True},
            "bounded_oversized": {
                "passed": False,
                "checks": {
                    "selected": False,
                    "total_chars_matches": True,
                    "within_context_cap": True,
                    "within_transport_budget": True,
                },
            },
        }
    )
    report = {
        "modes": [
            {"mode": mode, "context_bounds_passed": True, "admission": {"recall": 0}}
            for mode in member_v2.MODES
        ],
        "edge_cases": edges,
    }
    if broken == "context":
        report["modes"][0]["context_bounds_passed"] = False
    elif broken == "forgotten":
        edges["forgotten_excluded"]["passed"] = False
    elif broken == "oversized":
        edges["bounded_oversized_isolated"]["passed"] = False
    elif broken == "transport":
        edges["bounded_oversized"]["checks"]["within_transport_budget"] = False
    assert bool(member_v2.structural_failures(report)) is (broken is not None)
