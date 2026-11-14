"""R22: a Type is a capability-scored pool, not a hardcoded model alias.

All data is synthetic and offline; this checks the routing contract, not model
quality or a real user's provider credentials.
"""
from __future__ import annotations

import pytest

from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.mode_router import route_model
from kiro_byo.policy_packs import get_policy_pack


def _cfg(mode: str) -> KiroByoConfig:
    def model(name: str, tier: str, vision: bool, score: float) -> ModelSpec:
        return ModelSpec(
            "p", "P", name, name, routing_tiers=(tier,),
            routing_quality=score, routing_speed=score, routing_economy=score,
            tool_use=True, input_modalities=("text", "image") if vision else ("text",),
            context_window=128_000,
        )
    # The better text-only model is ineligible when an image arrives. Each Type
    # must then select the eligible image peer within its own routing policy.
    provider = ProviderSpec("p", "P", "openai-compatible", "https://example.invalid/v1", "", (
        model("fast-text", "fast", False, .99),
        model("fast-vision", "fast", True, .65),
        model("strong-text", "strong", False, .99),
        model("strong-vision", "strong", True, .68),
    ))
    return KiroByoConfig((provider,), mode_pack=mode)


@pytest.mark.parametrize("pack,types", [
    ("tiga", ("sky", "multi", "power", "glitter")),
    ("dyna", ("miracle", "flash", "strong")),
])
def test_forms_do_not_pin_a_single_model_id(pack: str, types: tuple[str, ...]) -> None:
    cfg = _cfg(pack)
    for policy in types:
        def route(image: bool):
            decision = route_model(
                cfg, pack=pack, policy=policy, prompt="请分析这份材料并提供建议",
                history=[], has_image=image, tools_required=False, tools_available=True,
            )
            assert decision is not None
            return decision
        text = route(False)
        image = route(True)
        assert text.model.model_id.endswith("text"), (pack, policy, text.model.model_id)
        assert image.model.model_id.endswith("vision"), (pack, policy, image.model.model_id)
        assert len(text.candidates) >= 4
        assert len(image.candidates) >= 2
        assert text.model.model_id != image.model.model_id


def test_only_glitter_keeps_leader_within_one_session() -> None:
    tiga = get_policy_pack("tiga")
    dyna = get_policy_pack("dyna")
    assert {row.id for row in tiga.policies if row.execution.sticky} == {"glitter"}
    assert not any(row.execution.sticky for row in dyna.policies)
