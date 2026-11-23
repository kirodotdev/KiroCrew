"""LTS edition: hosted Jev is locked off, every other decision model follows policy.

RFC D13. In an LTS build the baseline turns
``capabilities.decisions`` on with a ``providers`` allow list that leaves hosted Jev
out (``governance_parts/baseline.py``). Hosted Jev, any endpoint host under Jev's
domain and every hand-written endpoint are refused in code
(``decisions.capability.edition_refuses``), so no policy, profile or local config can
widen to them. The wake judge always takes the LLM lane, which still honours an org
deny of the row. The ``oss`` edition is unchanged; its tests live beside the code.

Real policy, real evaluator; only SEL, the keystone path and the provider client are
stood in for.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.config.sections import (
    DECISION_PROVIDER_ENDPOINT_DEFAULT,
    DecisionProviderConfig,
    DecisionsConfig,
    NudgeWakeConfig,
)
from kiro_crew.decisions import capability
from kiro_crew.decisions import gate as gate_mod
from kiro_crew.decisions import local_models
from kiro_crew.decisions.types import Answer, Choice

POINT = "skills.select"
QUESTIONS = [Choice(id="verdict", prompt="Which skill?", options=["NONE", "DUP"])]
CLEF = (
    "https://api.cloudflare.com/client/v4/accounts/"
    + "0123abcd" * 4
    + "/ai/run/@cf/cloudflare/clef"
)
#: The models the LTS baseline allows with no policy (RFC D13).
NON_JEV = (*(m.id for m in local_models.LOCAL_MODELS), "sagemaker", "clef")
#: An AWS-signed HTTPS endpoint names a host of the owner's choosing: not in the
#: baseline, so only a policy that names it allows it.
SIGNED_HTTPS = "aws_https"
SIGNED_HTTPS_ADDRESS = (
    "aws-https://decider.example.com/v1/decisions?service=execute-api&region=us-west-2"
)


class _Recording:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def ask(self, state, questions):
        self.calls.append(state)
        return {q.id: Answer(id=q.id, value="DUP", p=0.9, confidence=0.9) for q in questions}


def _config(endpoint: str = "", model: str | None = None, judge: str | None = None):
    kwargs: dict = {"timeout_ms": 1000}
    if endpoint:
        kwargs["endpoint"] = endpoint
    if model is not None:
        kwargs["model"] = model
    decisions: dict = {"bucket": 100, "provider": DecisionProviderConfig(**kwargs)}
    if judge is not None:
        decisions["nudge_wake"] = NudgeWakeConfig(provider=judge)
    return SimpleNamespace(decisions=DecisionsConfig(**decisions))


@pytest.fixture(params=["lts"])
def edition(request, monkeypatch):
    monkeypatch.setattr("kiro_crew.edition.EDITION", request.param)
    return request.param


@pytest.fixture(autouse=True)
def quiet(_floor_monkeypatch):
    import kiro_crew.sel as sel_mod

    _floor_monkeypatch.setattr(gate_mod, "_capability_denied_warned", False)
    sel = MagicMock()
    _floor_monkeypatch.setattr(sel_mod, "sel", lambda: sel)
    return sel


@pytest.fixture
def consent(tmp_path, monkeypatch):
    """A real keystone consenting to *endpoint*, every scope included."""
    path = tmp_path / "decisions_consent.json"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)
    monkeypatch.setattr(gate_mod, "_point_scope_granted", lambda *_a: True)

    def _set(endpoint: str = DECISION_PROVIDER_ENDPOINT_DEFAULT) -> None:
        state = {"enabled": True, "endpoint": endpoint, "nudge_evidence": True}
        path.write_text(json.dumps(state), encoding="utf-8")

    return _set


@pytest.fixture
def oracle(monkeypatch, tmp_path):
    import kiro_crew.decisions.impl_systemone as impl_mod
    from kiro_crew.decisions import log as log_mod

    monkeypatch.setattr(log_mod, "log_dir", lambda: tmp_path / "decisions")
    recording = _Recording()
    monkeypatch.setattr(impl_mod, "SystemOneProvider", lambda provider: recording)
    return recording


@pytest.fixture
def signed_https_oracle(monkeypatch, tmp_path):
    import kiro_crew.decisions.impl_signed_https as impl_mod
    from kiro_crew.decisions import log as log_mod

    monkeypatch.setattr(log_mod, "log_dir", lambda: tmp_path / "decisions")
    recording = _Recording()
    monkeypatch.setattr(impl_mod, "SignedHttpsProvider", lambda provider: recording)
    return recording


def _policy(monkeypatch, decisions: dict | None, *, empty: bool = False) -> None:
    from kiro_crew.platform import context as pc
    from kiro_crew.platform.governance import parse_policy

    ceiling = None
    if decisions is not None or empty:
        doc: dict = {"version": 1, "boot": {"fail_closed": True}}
        if decisions is not None:
            doc["capabilities"] = {"decisions": decisions}
        ceiling = parse_policy(doc)
    monkeypatch.setattr(pc, "current_context", lambda: SimpleNamespace(governance=ceiling))


#: A policy that tries to widen to Jev every way the ceiling can name it.
_WIDEN_TO_JEV = {
    "enabled": True,
    "scopes": {
        "providers": {"mode": "allow", "allow": ["jev", *NON_JEV, SIGNED_HTTPS]},
        "endpoints": {"mode": "allow", "allow": ["api.typesafe.ai", "*.typesafe.ai"]},
    },
}


@pytest.mark.parametrize("policy", [None, _WIDEN_TO_JEV], ids=["no-policy", "widening-policy"])
def test_non_jev_models_usable_and_jev_never(edition, monkeypatch, policy) -> None:
    _policy(monkeypatch, policy)
    for pid in NON_JEV:
        assert capability.is_decisions_denied(provider=pid) is False, pid
    assert capability.is_decisions_denied(provider="jev") is True
    assert capability.is_decisions_denied(provider="custom") is True
    assert capability.permitted_providers()["jev"] is False
    for host in ("https://api.typesafe.ai/v1/systemone", "https://x.TypeSafe.ai./v1"):
        assert capability.targets_denied(point=POINT, model="m", endpoint=host) is True
    if policy is None:
        assert capability.targets_denied(point=POINT, model="clef", endpoint=CLEF) is False


def test_provider_ids_in_lts(edition) -> None:
    assert capability.provider_id(DECISION_PROVIDER_ENDPOINT_DEFAULT, "jev-latest") == "jev"
    assert capability.provider_id(CLEF, "clef") == "clef"
    assert capability.provider_id(CLEF + "-flash", "clef-flash") == "clef"
    # Clef's id needs the model its path names, and the exact host and path.
    assert capability.provider_id(CLEF, "clef-flash") == "custom"
    assert (
        capability.provider_id(CLEF.replace("api.cloudflare.com", "api.typesafe.ai"), "clef")
        == "custom"
    )
    assert capability.provider_id(CLEF + "/x", "clef") == "custom"
    assert capability.provider_id("https://api.typesafe.ai/v1/systemone/", "m") == "custom"
    assert capability.provider_id("https://proxy.example/v1", "m") == "custom"


def test_baseline_providers_list_is_every_id_but_jev() -> None:
    from kiro_crew.platform.governance_parts.baseline import BASELINE_INNER_LISTS

    allow = BASELINE_INNER_LISTS["capabilities.decisions"]["providers"]["allow"]
    assert set(allow) == set(NON_JEV)
    assert set(capability.provider_ids()) - {"jev", SIGNED_HTTPS} | {"clef"} == set(allow)


def test_signed_https_is_off_under_the_baseline_and_sends_nothing(
    edition, monkeypatch, consent, signed_https_oracle
) -> None:
    """Not in the baseline: under a policy that does not name it, consent alone sends
    nothing to an owner-chosen host, while a baseline model stays usable."""
    _policy(monkeypatch, None, empty=True)
    assert capability.is_decisions_denied(provider=SIGNED_HTTPS) is True
    assert capability.is_decisions_denied(provider="sagemaker") is False
    consent(SIGNED_HTTPS_ADDRESS)
    cfg = _config(endpoint=SIGNED_HTTPS_ADDRESS, model="decider-model-v2")
    assert gate_mod.is_enabled(POINT, config=cfg) is False
    assert asyncio.run(gate_mod.decide(POINT, "hi", QUESTIONS, config=cfg)) is None
    assert signed_https_oracle.calls == []


def test_signed_https_sends_once_a_policy_names_it(
    edition, monkeypatch, consent, signed_https_oracle
) -> None:
    _policy(
        monkeypatch,
        {"enabled": True, "scopes": {"providers": {"mode": "allow", "allow": [SIGNED_HTTPS]}}},
    )
    consent(SIGNED_HTTPS_ADDRESS)
    cfg = _config(endpoint=SIGNED_HTTPS_ADDRESS, model="decider-model-v2")
    assert asyncio.run(gate_mod.decide(POINT, "hi", QUESTIONS, config=cfg)) is not None
    assert len(signed_https_oracle.calls) == 1


def test_the_jev_host_behind_the_signed_scheme_never_sends(
    edition, monkeypatch, consent, signed_https_oracle
) -> None:
    """A policy naming the option cannot reach Jev's host through it."""
    _policy(monkeypatch, _WIDEN_TO_JEV)
    address = "aws-https://api.typesafe.ai/v1/systemone?service=execute-api&region=us-west-2"
    consent(address)
    cfg = _config(endpoint=address, model="decider-model-v2")
    assert asyncio.run(gate_mod.decide(POINT, "hi", QUESTIONS, config=cfg)) is None
    assert signed_https_oracle.calls == []


@pytest.mark.parametrize(
    "endpoint",
    ["", "https://api.typesafe.ai/v1/systemone/", "https://proxy.example/v1"],
    ids=["hosted-default", "jev-host-respelled", "hand-written"],
)
@pytest.mark.parametrize("policy", [None, _WIDEN_TO_JEV], ids=["no-policy", "widening-policy"])
def test_jev_and_hand_written_never_send_even_consented(
    edition, monkeypatch, consent, oracle, endpoint, policy
) -> None:
    _policy(monkeypatch, policy)
    consent(endpoint or DECISION_PROVIDER_ENDPOINT_DEFAULT)
    cfg = _config(endpoint=endpoint)
    assert gate_mod.is_enabled(POINT, config=cfg) is False
    assert asyncio.run(gate_mod.decide(POINT, "hi", QUESTIONS, config=cfg)) is None
    assert oracle.calls == []


def test_clef_sends_with_no_policy(edition, monkeypatch, consent, oracle) -> None:
    _policy(monkeypatch, None)
    consent(CLEF)
    cfg = _config(endpoint=CLEF, model="clef")
    assert asyncio.run(gate_mod.decide(POINT, "hi", QUESTIONS, config=cfg)) is not None
    assert len(oracle.calls) == 1


@pytest.mark.parametrize(
    ("judge", "lane", "authorized"),
    [
        ("auto", gate_mod.LANE_LLM, True),
        ("llm", gate_mod.LANE_LLM, True),
        ("junk", gate_mod.LANE_LLM, True),
        # An explicit Jev pin sends nothing: its endpoint can never be armed in LTS.
        ("jev", gate_mod.LANE_JEV, False),
    ],
)
def test_judge_never_reaches_jev(edition, monkeypatch, consent, judge, lane, authorized) -> None:
    _policy(monkeypatch, None)
    consent()
    cfg = _config(judge=judge)
    armed = gate_mod._consented_for(cfg, None, gate_mod.JUDGE_POINT)
    assert armed is False
    assert gate_mod._judge_authority(cfg, None, endpoint_consented=armed) == (lane, authorized)


def test_judge_keeps_an_attested_local_preset(edition, monkeypatch, consent) -> None:
    """A permitted local preset stays on the endpoint lane: the lock is Jev's only."""
    preset = local_models.LOCAL_MODELS[0]
    endpoint = local_models.endpoint_for(preset.default_port)
    _policy(monkeypatch, None)
    consent(endpoint)
    monkeypatch.setattr(capability, "is_local_preset", lambda *_a, **_k: True)
    cfg = _config(endpoint=endpoint, model=preset.model, judge="auto")
    armed = gate_mod._consented_for(cfg, None, gate_mod.JUDGE_POINT)
    assert armed is True
    assert gate_mod._judge_authority(cfg, None, endpoint_consented=armed) == (
        gate_mod.LANE_JEV,
        True,
    )


def test_org_deny_turns_everything_off(edition, monkeypatch, consent, oracle) -> None:
    _policy(monkeypatch, {"enabled": False})
    assert capability.is_decisions_denied() is True
    for pid in NON_JEV:
        assert capability.is_decisions_denied(provider=pid) is True, pid
    lane, authorized = gate_mod._judge_authority(_config(), None, endpoint_consented=False)
    assert (lane, authorized) == (gate_mod.LANE_LLM, False)
    consent(CLEF)
    cfg = _config(endpoint=CLEF, model="clef")
    assert asyncio.run(gate_mod.decide(POINT, "hi", QUESTIONS, config=cfg)) is None
    assert oracle.calls == []


def test_profile_cannot_re_arm_jev(edition, monkeypatch) -> None:
    """A profile only narrows; even one naming Jev leaves it refused."""
    from kiro_crew.platform.governance import parse_profile, resolve

    profile = parse_profile(
        {
            "version": 1,
            "name": "local",
            "capabilities": {
                "decisions": {
                    "enabled": True,
                    "scopes": {"providers": {"mode": "allow", "allow": ["jev"]}},
                }
            },
        }
    )
    decision = resolve(None, profile, "capabilities.decisions", "providers:jev")
    assert not decision.permitted
    assert capability.edition_refuses("jev") is True


def test_oss_is_unchanged(monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", "oss")
    _policy(monkeypatch, None)
    assert capability.edition_refuses("jev") is False
    assert capability.provider_id("https://proxy.example/v1", "m") is None
    assert capability.provider_id(CLEF, "clef") is None
    assert capability.is_decisions_denied(provider="jev") is False
    assert (
        capability.targets_denied(
            point=POINT, model="m", endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT
        )
        is False
    )
    assert gate_mod.judge_lane(_config(judge="jev"), endpoint_consented=False) == "jev"


@pytest.mark.parametrize(
    ("keystone", "policy", "granted"),
    [
        ({"enabled": True, "nudge_evidence": True}, None, True),
        ({"enabled": True, "nudge_evidence": False}, None, False),
        ({"enabled": False, "nudge_evidence": True}, None, False),
        ({"enabled": True, "nudge_evidence": True}, {"enabled": False}, False),
    ],
    ids=["granted", "no-scope", "no-consent", "org-deny"],
)
def test_default_judge_screening_ignores_the_locked_endpoint(
    edition, monkeypatch, tmp_path, keystone, policy, granted
) -> None:
    """The LLM-lane default brief follows the recorded grant and the row, even with a
    keystone recorded for hosted Jev, whose endpoint the LTS build refuses."""
    path = tmp_path / "decisions_consent.json"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)
    keystone = {**keystone, "endpoint": DECISION_PROVIDER_ENDPOINT_DEFAULT}
    path.write_text(json.dumps(keystone), encoding="utf-8")
    _policy(monkeypatch, policy)
    assert gate_mod.judge_evidence_scope_granted(config=_config()) is granted
