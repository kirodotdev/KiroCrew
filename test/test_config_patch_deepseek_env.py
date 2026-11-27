"""PATCH /api/config/kirocrew for ``agent.deepseek_env``, the map Settings > Agent
Harness > DeepSeek Harness > Provider key writes.

The claims: the value is a keyed change naming ``DEEPSEEK_API_KEY`` only -- set to
its ``secret://`` reference, or removed by ``null`` -- and every other stored entry
(another provider route's key, set in the file) is kept; a key config.local.json
sets is refused with a 409; anything else is refused with a 400 that names the key
and never the value, and leaves the file as it was.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config import sections

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


def _stored(cfg_path) -> object:
    return json.loads(cfg_path.read_text(encoding="utf-8"))["agent"].get("deepseek_env")


async def _patch(value: object):
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.patch(
            "/api/config/kirocrew", json={"path": "agent.deepseek_env", "value": value}
        )
        return resp.status, await resp.json()


def test_the_dashboard_maps_only_the_default_routes_key() -> None:
    assert sections.DEEPSEEK_ENV_NAMES == {"DEEPSEEK_API_KEY"}


def test_validate_passes_a_reference_and_a_removal() -> None:
    assert sections.validate_deepseek_env_patch({"DEEPSEEK_API_KEY": "secret://K"}) == {
        "DEEPSEEK_API_KEY": "secret://K"
    }
    assert sections.validate_deepseek_env_patch({"DEEPSEEK_API_KEY": None}) == {
        "DEEPSEEK_API_KEY": None
    }


def test_merge_sets_removes_and_keeps_the_rest() -> None:
    stored = {"OPENROUTER_API_KEY": "secret://other"}
    added = sections.merge_env_patch(stored, {"DEEPSEEK_API_KEY": "secret://K"})
    assert added == {"OPENROUTER_API_KEY": "secret://other", "DEEPSEEK_API_KEY": "secret://K"}
    assert sections.merge_env_patch(added, {"DEEPSEEK_API_KEY": None}) == stored
    assert stored == {"OPENROUTER_API_KEY": "secret://other"}, "the stored map is not mutated"


@pytest.mark.asyncio
async def test_a_save_sets_the_reference(tmp_config) -> None:
    status, _ = await _patch({"DEEPSEEK_API_KEY": "secret://DEEPSEEK_HARNESS_API_KEY"})
    assert status == 200
    data = json.loads(tmp_config.read_text(encoding="utf-8"))
    assert data["agent"]["deepseek_env"] == {
        "DEEPSEEK_API_KEY": "secret://DEEPSEEK_HARNESS_API_KEY"
    }
    assert data["agent"]["approval_mode"] == "auto"


@pytest.mark.asyncio
async def test_a_change_keeps_another_routes_entry(tmp_config) -> None:
    # Another provider route's key is set in the file, outside the dashboard's one name.
    seed = {"agent": {"deepseek_env": {"OPENROUTER_API_KEY": "secret://or"}}}
    tmp_config.write_text(json.dumps(seed), encoding="utf-8")
    status, _ = await _patch({"DEEPSEEK_API_KEY": "secret://k"})
    assert status == 200
    assert _stored(tmp_config) == {
        "OPENROUTER_API_KEY": "secret://or",
        "DEEPSEEK_API_KEY": "secret://k",
    }
    status, _ = await _patch({"DEEPSEEK_API_KEY": None})
    assert status == 200
    assert _stored(tmp_config) == {"OPENROUTER_API_KEY": "secret://or"}


@pytest.mark.asyncio
async def test_a_key_the_local_overlay_sets_is_refused(tmp_config) -> None:
    with patch(
        "kiro_crew.config.loader.overlay_pins",
        side_effect=lambda *parts: parts == ("agent", "deepseek_env", "DEEPSEEK_API_KEY"),
    ):
        status, body = await _patch({"DEEPSEEK_API_KEY": "secret://k"})
    assert status == 409
    assert body["code"] == "config_overlay_owned"
    assert "DEEPSEEK_API_KEY" in body["error"]
    assert json.loads(tmp_config.read_text(encoding="utf-8")) == _SEED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        {"DEEPSEEK_API_KEY": "sk-plaintext-value"},
        {"OPENROUTER_API_KEY": "secret://x"},
        {"PATH": "secret://x"},
        {"DEEPSEEK_API_KEY": "secret://"},
        {"DEEPSEEK_API_KEY": "secret:// padded "},
        {"DEEPSEEK_API_KEY": 7},
        ["DEEPSEEK_API_KEY"],
        "secret://x",
    ],
)
async def test_anything_else_is_refused_without_echoing_the_value(tmp_config, value) -> None:
    status, body = await _patch(value)
    assert status == 400
    assert "sk-plaintext-value" not in json.dumps(body)
    assert json.loads(tmp_config.read_text(encoding="utf-8")) == _SEED


def test_the_refusal_names_the_key_not_the_value() -> None:
    with pytest.raises(ValueError) as exc:
        sections.validate_deepseek_env_patch({"DEEPSEEK_API_KEY": "sk-plaintext"})
    assert "DEEPSEEK_API_KEY" in str(exc.value)
    assert "sk-plaintext" not in str(exc.value)


def test_a_change_card_cannot_carry_the_map() -> None:
    # The PATCH merges by key, so a card's whole-map "after" and its whole-map
    # undo would not do what the card shows; the catalog refuses the proposal.
    from kiro_crew import change_card_catalog as catalog

    params = catalog.validate_params(
        "setting.change",
        {"path": "agent.deepseek_env", "value": {"DEEPSEEK_API_KEY": "secret://k"}},
    )
    with pytest.raises(catalog.CardCatalogError) as refused:
        catalog.build_preview(
            "setting.change", params, {"key": "agent.deepseek_env", "value": {}}, {}
        )
    assert refused.value.code == "setting_not_editable"
    assert "DeepSeek Harness" in str(refused.value)
