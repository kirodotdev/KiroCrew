"""PATCH /api/config/kirocrew for ``agent.strands_env``, the map Settings > Agent
Harness writes for the Strands harness.

The claims: the value is a keyed change -- each named variable is set to its
``secret://`` reference, or removed by ``null``, and every other stored entry is
kept; a key config.local.json sets is refused with a 409; anything else is refused
with a 400 that names the key and never the value, and leaves the file as it was.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.sections import STRANDS_ENV_NAMES, validate_strands_env_patch

_SEED = {"agent": {"approval_mode": "auto"}}


@web.middleware
async def _owner_identity(request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    state = request.app.get("state")
    if state is not None:
        state.owner_id = ""
    return await handler(request)


def _make_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    app = web.Application(middlewares=[_owner_identity])
    app["state"] = MagicMock()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    return app


@pytest.fixture
def tmp_config(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(_SEED), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
        yield cfg_path


async def _patch(value: object):
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.patch(
            "/api/config/kirocrew", json={"path": "agent.strands_env", "value": value}
        )
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_a_change_sets_its_keys_and_keeps_the_rest(tmp_config) -> None:
    status, _ = await _patch({"AWS_REGION": "secret://my region"})
    assert status == 200
    status, _ = await _patch({"ANTHROPIC_API_KEY": "secret://STRANDS_ANTHROPIC_API_KEY"})
    assert status == 200
    data = json.loads(tmp_config.read_text(encoding="utf-8"))
    assert data["agent"]["strands_env"] == {
        "AWS_REGION": "secret://my region",
        "ANTHROPIC_API_KEY": "secret://STRANDS_ANTHROPIC_API_KEY",
    }
    assert data["agent"]["approval_mode"] == "auto"


@pytest.mark.asyncio
async def test_null_removes_one_key_only(tmp_config) -> None:
    await _patch({"AWS_REGION": "secret://r", "OPENAI_API_KEY": "secret://k"})
    status, _ = await _patch({"AWS_REGION": None})
    assert status == 200
    stored = json.loads(tmp_config.read_text(encoding="utf-8"))["agent"]["strands_env"]
    assert stored == {"OPENAI_API_KEY": "secret://k"}


@pytest.mark.asyncio
async def test_an_empty_change_changes_nothing(tmp_config) -> None:
    await _patch({"OPENAI_API_KEY": "secret://k"})
    status, _ = await _patch({})
    assert status == 200
    stored = json.loads(tmp_config.read_text(encoding="utf-8"))["agent"]["strands_env"]
    assert stored == {"OPENAI_API_KEY": "secret://k"}


@pytest.mark.asyncio
async def test_a_key_the_local_overlay_sets_is_refused(tmp_config) -> None:
    # The handler asks the loader whether config.local.json sets a key; this test
    # answers "yes" for one Strands key only.
    with patch(
        "kiro_crew.config.loader.overlay_pins",
        side_effect=lambda *parts: parts == ("agent", "strands_env", "GEMINI_API_KEY"),
    ):
        status, body = await _patch({"GEMINI_API_KEY": None})
        assert status == 409
        assert body["code"] == "config_overlay_owned"
        status, _ = await _patch({"OPENAI_API_KEY": "secret://k"})
        assert status == 200
    stored = json.loads(tmp_config.read_text(encoding="utf-8"))["agent"]["strands_env"]
    assert stored == {"OPENAI_API_KEY": "secret://k"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        {"ANTHROPIC_API_KEY": "sk-ant-plaintext-value"},
        {"PATH": "secret://x"},
        {"AWS_CONFIG_FILE": "secret://x"},
        {"OPENAI_API_KEY": "secret://"},
        {"OPENAI_API_KEY": "secret:// padded "},
        {"OPENAI_API_KEY": 7},
        ["OPENAI_API_KEY"],
        "secret://x",
    ],
)
async def test_anything_else_is_refused_without_echoing_the_value(tmp_config, value) -> None:
    status, body = await _patch(value)
    assert status == 400
    assert "sk-ant-plaintext-value" not in json.dumps(body)
    assert json.loads(tmp_config.read_text(encoding="utf-8")) == _SEED


def test_the_names_are_the_strands_cli_provider_names() -> None:
    # The CLI's PROVIDER_ENVIRONMENT_KEYS, less the two file-path variables and the
    # raw AWS key pair the agent scrub removes, and AWS_PROFILE (the sandbox masks
    # ~/.aws, so a profile name points at nothing).
    assert STRANDS_ENV_NAMES == {
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_BEARER_TOKEN_BEDROCK",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "OLLAMA_HOST",
        "OLLAMA_MODEL",
        "LITELLM_BASE_URL",
        "LITELLM_API_KEY",
        "LITELLM_MODEL",
    }


def test_the_refusal_names_the_key_not_the_value() -> None:
    with pytest.raises(ValueError) as exc:
        validate_strands_env_patch({"GEMINI_API_KEY": "AIza-plaintext"})
    assert "GEMINI_API_KEY" in str(exc.value)
    assert "AIza-plaintext" not in str(exc.value)


def test_the_provider_choices_are_the_ones_the_harness_accepts() -> None:
    from kiro_crew.acp.harness.strands import STRANDS_PROVIDERS
    from kiro_crew.dashboard.handlers.core_config_schema import _EDITABLE_CONFIG

    assert set(_EDITABLE_CONFIG["agent.strands_provider"]["values"]) == set(STRANDS_PROVIDERS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model", ["us.anthropic.claude-sonnet-4-5-20250929-v1:0", "llama3.2:latest", ""]
)
async def test_a_model_id_with_a_tag_or_version_is_accepted(tmp_config, model) -> None:
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.patch(
            "/api/config/kirocrew", json={"path": "agent.strands_model", "value": model}
        )
        assert resp.status == 200
    assert json.loads(tmp_config.read_text(encoding="utf-8"))["agent"]["strands_model"] == model


@pytest.mark.asyncio
async def test_a_model_with_a_space_is_refused(tmp_config) -> None:
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.patch(
            "/api/config/kirocrew", json={"path": "agent.strands_model", "value": "a b"}
        )
        assert resp.status == 400


def test_the_harness_checks_the_same_name_set_the_dashboard_writes() -> None:
    from kiro_crew.acp.harness import strands

    assert strands.STRANDS_ENV_NAMES is STRANDS_ENV_NAMES


def test_a_change_card_cannot_carry_the_map() -> None:
    # The PATCH merges by key, so a card's whole-map "after" and its whole-map
    # undo would not do what the card shows; the catalog refuses the proposal.
    from kiro_crew import change_card_catalog as catalog

    params = catalog.validate_params(
        "setting.change",
        {"path": "agent.strands_env", "value": {"OPENAI_API_KEY": "secret://k"}},
    )
    with pytest.raises(catalog.CardCatalogError) as refused:
        catalog.build_preview(
            "setting.change", params, {"key": "agent.strands_env", "value": {}}, {}
        )
    assert refused.value.code == "setting_not_editable"
