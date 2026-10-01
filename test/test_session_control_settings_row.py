"""Settings > Developer > Session control: ``agent.session_control`` over PATCH.

The row writes the key through ``PATCH /api/config/kirocrew``. These tests pin
the three things the row relies on: the owner's write lands in config and
``session_control_enabled()`` (the per-call gate every session-control route
reads) follows it with no restart; only a real bool is accepted; and a caller
that is not the dashboard owner cannot write it.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def _make_app(*, app_name: str = "") -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    @web.middleware
    async def _identity(request, handler):
        request["user"] = "local-app"
        request["app"] = app_name
        request.app["state"].owner_id = ""
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = MagicMock()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    return app


@pytest.fixture
def tmp_config(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"agent": {"approval_mode": "auto"}}), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
        yield cfg_path


async def _patch(client, value):
    return await client.patch(
        "/api/config/kirocrew", json={"path": "agent.session_control", "value": value}
    )


def test_session_control_is_an_editable_bool():
    from kiro_crew.dashboard.handlers import core

    assert core._EDITABLE_CONFIG["agent.session_control"] == {"type": "bool"}


@pytest.mark.asyncio
async def test_owner_write_reaches_the_per_call_gate(tmp_config):
    from kiro_crew.dashboard.session_control import session_control_enabled

    # Absent key: the shipped default, on.
    assert session_control_enabled() is True
    async with TestClient(TestServer(_make_app())) as client:
        off = await _patch(client, False)
        assert off.status == 200
        assert (await off.json())["agent"]["session_control"] is False
        assert (
            json.loads(tmp_config.read_text(encoding="utf-8"))["agent"]["session_control"] is False
        )
        # No restart: the next session-control call reads the new value.
        assert session_control_enabled() is False

        on = await _patch(client, True)
        assert on.status == 200
        assert (
            json.loads(tmp_config.read_text(encoding="utf-8"))["agent"]["session_control"] is True
        )
        assert session_control_enabled() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
async def test_non_boolean_is_refused_and_nothing_is_written(tmp_config, value):
    before = tmp_config.read_bytes()
    async with TestClient(TestServer(_make_app())) as client:
        response = await _patch(client, value)
        assert response.status == 400
        assert (await response.json())["error"] == "must be a boolean"
    assert tmp_config.read_bytes() == before


@pytest.mark.asyncio
async def test_a_caller_that_is_not_the_owner_cannot_write_it(tmp_config):
    """An app-scoped caller (not the dashboard owner) is refused before any write,
    so the switch cannot be flipped by anything but the owner's own dashboard."""
    before = tmp_config.read_bytes()
    async with TestClient(TestServer(_make_app(app_name="some-app"))) as client:
        response = await _patch(client, True)
        assert response.status == 403
        assert (await response.json())["code"] == "owner_only"
    assert tmp_config.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("overlay_value", [True, False])
async def test_a_write_the_local_overlay_would_shadow_is_refused(tmp_config, overlay_value):
    """``config.local.json`` deep-merges over ``config.json``, so a base write of this
    key would be shadowed. A 200 there would show Off while agents keep control, so
    the route refuses with 409 and writes nothing, whichever value the overlay holds."""
    local = tmp_config.parent / "config.local.json"
    local.write_text(json.dumps({"agent": {"session_control": overlay_value}}), encoding="utf-8")
    before = tmp_config.read_bytes()
    with patch("kiro_crew.config.loader.config_local_path", return_value=local):
        async with TestClient(TestServer(_make_app())) as client:
            response = await _patch(client, not overlay_value)
            assert response.status == 409
            body = await response.json()
            assert body["code"] == "session_control_overlay_owned"
            assert "config.local.json" in body["error"]
    assert tmp_config.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_section", [None, [], "off", 0])
async def test_a_non_object_agent_overlay_is_refused(tmp_config, agent_section):
    """A present non-object ``agent`` overlay replaces the base section in the deep
    merge, and the loader then falls back to defaults (On). A written ``false`` would
    never take effect, so the overlay owns the key and the route refuses."""
    local = tmp_config.parent / "config.local.json"
    local.write_text(json.dumps({"agent": agent_section}), encoding="utf-8")
    before = tmp_config.read_bytes()
    with patch("kiro_crew.config.loader.config_local_path", return_value=local):
        async with TestClient(TestServer(_make_app())) as client:
            response = await _patch(client, False)
            assert response.status == 409
            body = await response.json()
            assert body["code"] == "session_control_overlay_owned"
    assert tmp_config.read_bytes() == before


@pytest.mark.asyncio
async def test_an_overlay_without_the_key_does_not_block_the_write(tmp_config):
    local = tmp_config.parent / "config.local.json"
    local.write_text(json.dumps({"agent": {"model": "x"}}), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_local_path", return_value=local):
        async with TestClient(TestServer(_make_app())) as client:
            response = await _patch(client, False)
            assert response.status == 200
    assert json.loads(tmp_config.read_text(encoding="utf-8"))["agent"]["session_control"] is False


@pytest.mark.parametrize(
    ("overlay", "non_object_owns", "expected"),
    [
        (None, True, []),
        ("{not json", True, []),
        ({"model": "x"}, True, []),
        ({"agent": {"session_control": True, "model": "x"}}, True, ["session_control"]),
        ({"agent": None}, True, ["session_control", "apps_trusted"]),
        ({"agent": None}, False, []),
        ({"agent": ["x"]}, False, []),
    ],
)
def test_the_shared_overlay_reader(tmp_path, overlay, non_object_owns, expected):
    # One reader serves the session-control 409 and the trust-settings 409; only
    # the {"agent": <non-object>} case differs, and the caller chooses it.
    from kiro_crew.config.loader import overlay_owned_agent_keys

    local = tmp_path / "config.local.json"
    if isinstance(overlay, str):
        local.write_text(overlay, encoding="utf-8")
    elif overlay is not None:
        local.write_text(json.dumps(overlay), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_local_path", return_value=local):
        owned = overlay_owned_agent_keys(
            ("session_control", "apps_trusted"), non_object_agent_owns=non_object_owns
        )
    assert owned == expected
