"""Every model pin a Settings picker writes accepts the provider/model ids it offers.

The chat default, role-model, throttle-fallback, refusal-fallback and knowledge
extraction pickers list the same ``useAvailableModelsQuery()`` names as the
Decisions tier pickers, and OpenCode advertises those names as ``provider/model``. A pin whose PATCH grammar refused the
slash would answer "Could not save this setting" for a model its own picker offered.
Load-time config accepts the same id (``normalize_agent_model`` /
``model_registry.to_provider_id`` pass it through), so the PATCH gate is the only
place that can refuse it.

These run the real ``PATCH /api/config/kirocrew`` handler against a temp
``config.json`` and pin, for each pin:

* a provider-qualified id is stored as sent;
* the widened grammar still refuses malformed segments and command characters;
* the pin shares the tier's pattern, so the two cannot drift apart again.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

_BASE_CONFIG = {
    "agents": {"kirocrew": {"kiro_agent": "kirocrew"}},
    "default_agent": "kirocrew",
}

# Every PATCH-editable model pin whose picker lists the advertised model names
# and whose value is checked against the account's entitlement (`validate_fn`).
_MODEL_PINS = (
    "agent.role_models.background",
    "agent.role_models.subagent",
    "agent.fallback_model",
    "agent.refusal_fallback_model",
)

# The chat default model shares the grammar but has no entitlement `validate_fn`:
# an unknown-but-well-formed id is refused downstream by the backend.
_CHAT_DEFAULT_PIN = "agent.model"

# The knowledge extraction pin takes the same grammar-only spec: its picker lists the
# default harness's models, but the pool's zero-tool workers may run on another
# backend (``llm_pool._get_acp_backend``), so no one catalog can judge it.
_EXTRACTION_PIN = "knowledge.extraction_model"

_GRAMMAR_ONLY_PINS = (_CHAT_DEFAULT_PIN, _EXTRACTION_PIN)

# The wake judge's pin keeps the bare-id grammar: the decision gate scrubs any
# model ``decisions.types.is_model_id`` refuses (it has no ``/``), so a saved
# ``provider/model`` here would be a pin the judge never runs on.
_WAKE_JUDGE_PIN = "decisions.nudge_wake.llm_model"


def _app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_config, api_kirocrew_config_patch

    app = web.Application()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    app.router.add_get("/api/config/kirocrew", api_kirocrew_config)
    return as_owner(app)


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_BASE_CONFIG), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    return path


async def _patch(client, path, value):
    return await client.patch("/api/config/kirocrew", json={"path": path, "value": value})


def _stored(config_file, dotted):
    node = json.loads(config_file.read_text(encoding="utf-8"))
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


@pytest.mark.asyncio
@pytest.mark.parametrize("pin", _MODEL_PINS)
async def test_a_provider_qualified_picker_model_can_be_saved(config_file, pin):
    """The id the picker offered is the id that is stored."""
    model = "example-provider/example-model"
    async with TestClient(TestServer(_app())) as client:
        resp = await _patch(client, pin, model)
        assert resp.status == 200, await resp.text()
    assert _stored(config_file, pin) == model


@pytest.mark.asyncio
@pytest.mark.parametrize("pin", _MODEL_PINS)
@pytest.mark.parametrize("value", ["a; rm -rf /", "provider//model", "/model", "model/"])
async def test_a_pin_refuses_a_value_outside_the_model_id_grammar(config_file, pin, value):
    """Opening the provider/model shape never admits empty segments or commands."""
    async with TestClient(TestServer(_app())) as client:
        resp = await _patch(client, pin, value)
        assert resp.status == 400
    assert _stored(config_file, pin) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("pin", _GRAMMAR_ONLY_PINS)
async def test_a_grammar_only_pin_saves_a_provider_qualified_id(config_file, pin):
    """Settings → Chat and Settings → Knowledge list the same advertised names, so
    their pins save them too."""
    model = "example-provider/example-model"
    async with TestClient(TestServer(_app())) as client:
        resp = await _patch(client, pin, model)
        assert resp.status == 200, await resp.text()
    assert _stored(config_file, pin) == model


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["a; rm -rf /", "provider//model", "/model", "model/"])
@pytest.mark.parametrize("pin", _GRAMMAR_ONLY_PINS)
async def test_a_grammar_only_pin_refuses_a_malformed_id(config_file, pin, value):
    async with TestClient(TestServer(_app())) as client:
        resp = await _patch(client, pin, value)
        assert resp.status == 400
    assert _stored(config_file, pin) is None


@pytest.mark.parametrize("pin", (*_MODEL_PINS, *_GRAMMAR_ONLY_PINS))
def test_every_model_pin_shares_the_tier_grammar(pin):
    """One pattern for every validated model pin, so a picker cannot offer an id
    that one of its siblings refuses."""
    from kiro_crew.config.sections import DECISION_MODEL_ROUTE_TIERS
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    tier = _EDITABLE_CONFIG[f"decisions.model_route.{DECISION_MODEL_ROUTE_TIERS[0]}"]
    assert _EDITABLE_CONFIG[pin]["pattern"] == tier["pattern"]
    if pin in _MODEL_PINS:
        assert _EDITABLE_CONFIG[pin]["validate_fn"] is tier["validate_fn"]


@pytest.mark.asyncio
async def test_the_extraction_pin_saves_what_its_picker_offers_on_any_harness(config_file):
    """With codex as the default harness the Knowledge picker lists codex ids, while
    the pool's zero-tool workers fall back to Kiro. Neither catalog judges the pin,
    so the id the picker offered saves although the Kiro session does not
    advertise it."""
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    assert "validate_fn" not in _EDITABLE_CONFIG[_EXTRACTION_PIN]
    assert _EDITABLE_CONFIG[_EXTRACTION_PIN]["max_len"] == 64
    data = {**_BASE_CONFIG, "agent": {"acp_backend": "codex"}}
    config_file.write_text(json.dumps(data), encoding="utf-8")

    def _session(backend, *ids):
        rows = [{"modelId": m, "name": m, "description": ""} for m in ids]
        return SimpleNamespace(
            client=SimpleNamespace(backend=backend), available_models=lambda: rows
        )

    app = _app()
    kiro = _session("", "auto", "example-kiro-model")
    codex = _session("codex", "example-codex-model")
    app["state"] = SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: [kiro, codex]))
    async with TestClient(TestServer(app)) as client:
        resp = await _patch(client, _EXTRACTION_PIN, "example-codex-model")
        assert resp.status == 200, await resp.text()
    assert _stored(config_file, _EXTRACTION_PIN) == "example-codex-model"


@pytest.mark.asyncio
async def test_the_wake_judge_pin_refuses_an_id_its_gate_would_scrub(config_file):
    """Settings must not show a saved wake-judge pin the judge never runs on."""
    from kiro_crew.decisions.types import is_model_id

    model = "example-provider/example-model"
    assert not is_model_id(model)
    async with TestClient(TestServer(_app())) as client:
        resp = await _patch(client, _WAKE_JUDGE_PIN, model)
        assert resp.status == 400
    assert _stored(config_file, _WAKE_JUDGE_PIN) is None


@pytest.mark.asyncio
async def test_the_wake_judge_pin_still_saves_a_bare_id(config_file):
    """A bare id passes both the PATCH gate and the decision gate's model check."""
    from kiro_crew.decisions.types import is_model_id

    model = "example-model"
    assert is_model_id(model)
    async with TestClient(TestServer(_app())) as client:
        resp = await _patch(client, _WAKE_JUDGE_PIN, model)
        assert resp.status == 200, await resp.text()
    assert _stored(config_file, _WAKE_JUDGE_PIN) == model
