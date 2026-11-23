"""An ``aws-https://`` decision model through the real gate, end to end.

``gate.decide`` with an ``aws-https://`` config, the real consent keystone, the real
governance evaluator and the real ``SignedHttpsProvider``; only the HTTP transport,
the AWS credentials, SEL and the decision log are stood in for. Pinned here:

* consent, the provider's allow-list id and the ceiling's ``endpoints`` list are all
  asked on the way to ONE send: a consented, permitted address is POSTed to the
  ``https://`` URL it names, signed for its service and region, with no bearer key;
* each of them alone stops the request before any byte leaves: consent recorded for
  another address, a ``providers`` list that leaves ``aws_https`` out, an
  ``endpoints`` list that leaves its host out, and an address that does not parse;
* the ``endpoints`` list is asked about the ``https://`` URL, never the
  ``aws-https://`` spelling.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from kiro_crew.config.sections import DecisionProviderConfig, DecisionsConfig
from kiro_crew.decisions import capability, impl_signed_https
from kiro_crew.decisions import gate as gate_mod
from kiro_crew.decisions.types import Choice

POINT = "skills.select"
QUESTIONS = [Choice(id="verdict", prompt="Which skill?", options=["NONE", "DUP"])]
URL = "https://decider.example.com/v1/decisions"
ADDRESS = "aws-https://decider.example.com/v1/decisions?service=execute-api&region=us-west-2"
REPLY = {
    "model": "decider-model-v2",
    "answers": {
        "verdict": {
            "type": "choice",
            "choice": "DUP",
            "probabilities": {"NONE": 0.1, "DUP": 0.9},
            "confidence": 0.9,
        }
    },
}


class _Creds:
    access_key = "AKIDEXAMPLE"
    secret_key = "secret-example"
    token = None


@pytest.fixture(autouse=True)
def quiet(_floor_monkeypatch):
    import kiro_crew.sel as sel_mod

    _floor_monkeypatch.setattr(gate_mod, "_capability_denied_warned", False)
    sel = MagicMock()
    _floor_monkeypatch.setattr(sel_mod, "sel", lambda: sel)
    return sel


@pytest.fixture(autouse=True)
def oss(monkeypatch):
    monkeypatch.setattr("kiro_crew.edition.EDITION", "oss")


@pytest.fixture
def consent(tmp_path, monkeypatch):
    """A real keystone consenting to *endpoint*, every scope included."""
    path = tmp_path / "decisions_consent.json"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)
    monkeypatch.setattr(gate_mod, "_point_scope_granted", lambda *_a: True)

    def _set(endpoint: str = ADDRESS) -> None:
        state = {"enabled": True, "endpoint": endpoint, "nudge_evidence": True}
        path.write_text(json.dumps(state), encoding="utf-8")

    return _set


@pytest.fixture
def wire(monkeypatch, tmp_path):
    """Every request the shared HTTP client would send, recorded and answered."""
    from kiro_crew.decisions import impl_systemone
    from kiro_crew.decisions import log as log_mod

    monkeypatch.setattr(log_mod, "log_dir", lambda: tmp_path / "decisions")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=REPLY, headers={"x-amzn-RequestId": "req-1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    monkeypatch.setattr(impl_systemone, "_session_for_running_loop", lambda: client)
    monkeypatch.setattr(impl_signed_https, "_frozen_credentials", lambda _profile: _Creds())
    return seen


def _policy(monkeypatch, decisions: dict | None) -> None:
    from kiro_crew.platform import context as pc
    from kiro_crew.platform.governance import parse_policy

    ceiling = None
    if decisions is not None:
        ceiling = parse_policy(
            {"version": 1, "boot": {"fail_closed": True}, "capabilities": {"decisions": decisions}}
        )
    monkeypatch.setattr(pc, "current_context", lambda: SimpleNamespace(governance=ceiling))


def _config(endpoint: str = ADDRESS, model: str = "decider-model-v2"):
    provider = DecisionProviderConfig(endpoint=endpoint, model=model, timeout_ms=5000)
    return SimpleNamespace(decisions=DecisionsConfig(bucket=100, provider=provider))


def _decide(cfg=None):
    return asyncio.run(gate_mod.decide(POINT, "hi", QUESTIONS, config=cfg or _config()))


def test_a_consented_permitted_address_is_sent_once_signed(monkeypatch, consent, wire):
    _policy(
        monkeypatch,
        {
            "enabled": True,
            "scopes": {
                "providers": {"mode": "allow", "allow": ["aws_https"]},
                "endpoints": {"mode": "allow", "allow": ["decider.example.com"]},
            },
        },
    )
    consent()
    assert gate_mod.is_enabled(POINT, config=_config()) is True
    answers = _decide()
    assert answers is not None and answers["verdict"].value == "DUP"
    (request,) = wire
    assert str(request.url) == URL
    assert json.loads(request.content)["model"] == "decider-model-v2"
    auth = request.headers["authorization"]
    assert "/us-west-2/execute-api/aws4_request" in auth
    assert "Bearer" not in auth


def test_consent_for_another_address_sends_nothing(monkeypatch, consent, wire):
    _policy(monkeypatch, None)
    consent("aws-https://other.example.com/v1/decisions?service=execute-api&region=us-west-2")
    assert gate_mod.is_enabled(POINT, config=_config()) is False
    assert _decide() is None
    assert wire == []


def test_the_providers_list_leaving_it_out_sends_nothing(monkeypatch, consent, wire):
    _policy(
        monkeypatch,
        {"enabled": True, "scopes": {"providers": {"mode": "allow", "allow": ["sagemaker"]}}},
    )
    consent()
    assert gate_mod.is_enabled(POINT, config=_config()) is False
    assert _decide() is None
    assert wire == []


def test_the_endpoints_list_leaving_its_host_out_sends_nothing(monkeypatch, consent, wire):
    _policy(
        monkeypatch,
        {
            "enabled": True,
            "scopes": {"endpoints": {"mode": "allow", "allow": ["other.example.com"]}},
        },
    )
    consent()
    assert _decide() is None
    assert wire == []


def test_the_endpoints_list_is_asked_about_the_https_url(monkeypatch, consent, wire):
    _policy(monkeypatch, None)
    asked: list = []
    real = capability.targets_denied

    def _record(*a, endpoint=None, **k):
        asked.append(endpoint)
        return real(*a, endpoint=endpoint, **k)

    monkeypatch.setattr(capability, "targets_denied", _record)
    consent()
    assert _decide() is not None
    assert URL in asked
    assert not any(isinstance(e, str) and e.startswith("aws-https://") for e in asked)


def test_an_address_that_does_not_parse_sends_nothing(monkeypatch, consent, wire):
    bad = "aws-https://decider.example.com/v1/decisions?region=us-west-2"
    _policy(monkeypatch, None)
    consent(bad)
    assert _decide(_config(bad)) is None
    assert wire == []


def test_a_blank_model_sends_nothing_and_is_logged_as_scrubbed(monkeypatch, consent, wire):
    """No hosted default id reaches the wire: the scrub refuses the empty model first."""
    _policy(monkeypatch, None)
    consent()
    assert gate_mod.lane_model(_config(model=""), lane=gate_mod.LANE_ENDPOINT) == ""
    assert gate_mod.scrub_reason("hi", QUESTIONS, model="") == gate_mod.ERROR_SCRUBBED_MODEL
    assert _decide(_config(model="")) is None
    assert wire == []


def test_a_blank_model_read_from_config_json_stays_blank(monkeypatch, consent, wire):
    """The loader does not fill a blank model with the hosted default for this scheme."""
    from kiro_crew.config.sections import DECISION_PROVIDER_MODEL_DEFAULT

    parsed = DecisionsConfig.from_raw({"provider": {"endpoint": ADDRESS, "model": ""}})
    assert parsed.provider.model == ""
    hosted = DecisionsConfig.from_raw({"provider": {"endpoint": URL, "model": ""}})
    assert hosted.provider.model == DECISION_PROVIDER_MODEL_DEFAULT
    _policy(monkeypatch, None)
    consent()
    assert _decide(SimpleNamespace(decisions=parsed)) is None
    assert wire == []
