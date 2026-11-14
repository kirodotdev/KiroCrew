"""Relay model ambiguity: safe, explainable cold-start sampling, never random rotation."""
from __future__ import annotations

from dataclasses import replace

import pytest

from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.mode_router import route_model, route_diagnostics


def _unknown_relay(count: int = 25) -> KiroByoConfig:
    models = tuple(
        ModelSpec("relay", "Relay", f"model-{i:02d}", f"Model {i:02d}")
        for i in range(count)
    )
    return KiroByoConfig((ProviderSpec(
        "relay", "Relay", "openai-compatible", "https://example.invalid/v1", "", models,
    ),), mode_pack="tiga")


@pytest.mark.parametrize("pack,form", [
    ("tiga", "sky"), ("tiga", "multi"),
    ("dyna", "miracle"), ("dyna", "flash"),
])
def test_ambiguous_relay_scores_are_deterministic_per_request(pack: str, form: str) -> None:
    cfg = _unknown_relay()
    observed: set[str] = set()
    for n in range(32):
        prompt = f"你好 {n}"
        decision = route_model(cfg, pack=pack, policy=form, prompt=prompt, history=[],
                               has_image=False, tools_available=False, tools_required=False)
        assert decision is not None
        repeated = route_model(cfg, pack=pack, policy=form, prompt=prompt, history=[],
                               has_image=False, tools_available=False, tools_required=False)
        assert repeated is not None and repeated.model.model_id == decision.model.model_id
        assert "stable ID tie-break" in " ".join(decision.reason)
        assert len(decision.candidates) == 25
        observed.add(decision.model.model_id)
        assert decision.selection_source == "bounded_cold_start"
        if pack == "tiga" and form == "sky":
            # Sky may sample only its declared capability-fallback pool here;
            # none of these models is falsely certified as Fast.
            assert "outside-tier fallback" in " ".join(decision.reason)
    assert len(observed) > 1



@pytest.mark.parametrize("pack,form", [
    ("tiga", "power"), ("tiga", "glitter"), ("dyna", "strong"),
])
def test_strict_strong_forms_do_not_claim_unknown_relay_models_are_strong(pack: str, form: str) -> None:
    decision = route_model(
        _unknown_relay(), pack=pack, policy=form, prompt="你好", history=[],
        has_image=False, tools_available=False, tools_required=False,
    )
    assert decision is None


def test_completed_route_diagnostics_include_sticky_and_failover_below_top_12() -> None:
    decision = route_model(
        _unknown_relay(), pack="tiga", policy="multi", prompt="你好", history=[],
        has_image=False, tools_available=False, tools_required=False,
    )
    assert decision is not None
    sticky = decision.candidates[20]
    failover = decision.candidates[23]
    selected = replace(
        decision, provider=sticky.provider, model=sticky.model,
        selection_source="sticky",
    )
    info = route_diagnostics(
        selected, served_provider=failover.provider.id, served_model=failover.model.model_id,
    )
    assert info["selection_source"] == "provider_fallback"
    assert len(info["candidates"]) <= 12
    assert sum(bool(row["selected"]) for row in info["candidates"]) == 1
    assert sum(bool(row["served"]) for row in info["candidates"]) == 1
    assert {row["model"] for row in info["candidates"]} >= {sticky.model.model_id, failover.model.model_id}


def test_normal_route_retains_original_top_twelve() -> None:
    decision = route_model(
        _unknown_relay(), pack="tiga", policy="multi", prompt="你好", history=[],
        has_image=False, tools_available=False, tools_required=False,
    )
    assert decision is not None
    info = route_diagnostics(decision)
    assert [row["model"] for row in info["candidates"]] == [
        row.model.model_id for row in decision.candidates[:12]
    ]
