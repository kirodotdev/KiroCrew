"""Every model pin a Settings picker writes accepts the provider/model ids it offers.

The role-model, throttle-fallback, refusal-fallback and wake-judge pickers list the
same ``useAvailableModelsQuery()`` names as the Decisions tier pickers, and OpenCode
advertises those names as ``provider/model``. A pin whose PATCH grammar refused the
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
    "decisions.nudge_wake.llm_model",
)


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


@pytest.mark.parametrize("pin", _MODEL_PINS)
def test_every_model_pin_shares_the_tier_grammar(pin):
    """One pattern for every validated model pin, so a picker cannot offer an id
    that one of its siblings refuses."""
    from kiro_crew.config.sections import DECISION_MODEL_ROUTE_TIERS
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    tier = _EDITABLE_CONFIG[f"decisions.model_route.{DECISION_MODEL_ROUTE_TIERS[0]}"]
    assert _EDITABLE_CONFIG[pin]["pattern"] == tier["pattern"]
    assert _EDITABLE_CONFIG[pin]["validate_fn"] is tier["validate_fn"]
