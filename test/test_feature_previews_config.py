"""``dashboard.feature_previews``: the Feature previews switches, kept by the gateway.

The dashboard holds a browser copy of each switch as a cache
(``website/src/utils/previewFlags.ts``) and writes it here through the owner-only
config PATCH, so a switch holds on every browser and dashboard address.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.loader_builders import _feature_previews
from kiro_crew.config.sections import FEATURE_PREVIEW_FLAGS

REPO = Path(__file__).resolve().parents[1]
PREVIEW_FLAGS_TS = REPO / "website/src/utils/previewFlags.ts"


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
    cfg_path.write_text(json.dumps({"dashboard": {"folder_sort": "name"}}), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
        yield cfg_path


def _frontend_flag_names() -> set[str]:
    """The config names of the flags in ``previewFlags.ts``'s ``PREVIEW_FLAGS`` array."""
    text = PREVIEW_FLAGS_TS.read_text(encoding="utf-8")
    consts = dict(
        re.findall(r"export const (PREVIEW_[A-Z_]+) = `\$\{PREVIEW_FLAG_PREFIX\}([a-z-]+)`", text)
    )
    body = re.search(r"export const PREVIEW_FLAGS: readonly string\[\] = \[(.*?)\]", text, re.S)
    assert body, "PREVIEW_FLAGS array not found in previewFlags.ts"
    members = re.findall(r"PREVIEW_[A-Z_]+", body.group(1))
    return {consts[m].replace("-", "_") for m in members}


def test_the_gateway_list_equals_the_dashboard_flags():
    names = _frontend_flag_names()
    assert names, "no preview flag constants found in previewFlags.ts"
    assert names == set(FEATURE_PREVIEW_FLAGS)


def test_nothing_is_on_by_default():
    assert KiroCrewConfig().dashboard.feature_previews == {}


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, {}),
        ([], {}),
        ("crew", {}),
        ({"crew": True, "webhooks": False}, {"crew": True, "webhooks": False}),
        ({"crew": "yes", "webhooks": 1, "layout_harness": None}, {}),
        ({"layout-harness": True}, {}),
        ({"crew": True, "not_a_preview": True}, {"crew": True}),
    ],
)
def test_the_loader_keeps_only_known_names_with_boolean_values(raw, expected):
    assert _feature_previews(raw, {}) == expected


def test_a_stored_switch_loads(tmp_config):
    tmp_config.write_text(
        json.dumps({"dashboard": {"feature_previews": {"crew": True}}}), encoding="utf-8"
    )
    assert KiroCrewConfig.load().dashboard.feature_previews == {"crew": True}
    assert KiroCrewConfig.load().to_dict()["dashboard"]["feature_previews"] == {"crew": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", FEATURE_PREVIEW_FLAGS)
async def test_each_known_switch_is_writable(tmp_config, name):
    async with TestClient(TestServer(_make_app())) as c:
        resp = await c.patch(
            "/api/config/kirocrew",
            json={"path": f"dashboard.feature_previews.{name}", "value": True},
        )
        assert resp.status == 200, await resp.text()
    stored = json.loads(tmp_config.read_text(encoding="utf-8"))
    assert stored["dashboard"]["feature_previews"] == {name: True}
    assert stored["dashboard"]["folder_sort"] == "name"
    assert KiroCrewConfig.load().dashboard.feature_previews == {name: True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,value",
    [
        ("dashboard.feature_previews.not_a_preview", True),
        ("dashboard.feature_previews.layout-harness", True),
        ("dashboard.feature_previews", {"crew": True}),
        ("dashboard.feature_previews.crew", "true"),
        ("dashboard.feature_previews.crew", 1),
    ],
)
async def test_an_unknown_name_or_non_boolean_writes_nothing(tmp_config, path, value):
    before = tmp_config.read_bytes()
    async with TestClient(TestServer(_make_app())) as c:
        resp = await c.patch("/api/config/kirocrew", json={"path": path, "value": value})
        assert resp.status == 400, await resp.text()
    assert tmp_config.read_bytes() == before


@pytest.mark.asyncio
async def test_the_one_time_carry_up_writes_a_switch_the_gateway_does_not_hold(tmp_config):
    async with TestClient(TestServer(_make_app())) as c:
        resp = await c.patch(
            "/api/config/kirocrew",
            json={"path": "dashboard.feature_previews.crew", "value": True, "if_unset": True},
        )
        assert resp.status == 200, await resp.text()
    stored = json.loads(tmp_config.read_text(encoding="utf-8"))
    assert stored["dashboard"]["feature_previews"] == {"crew": True}
    assert KiroCrewConfig.load().dashboard.feature_previews == {"crew": True}


@pytest.mark.asyncio
async def test_the_one_time_carry_up_never_replaces_a_stored_switch(tmp_config):
    tmp_config.write_text(
        json.dumps({"dashboard": {"feature_previews": {"crew": False}}}), encoding="utf-8"
    )
    before = tmp_config.read_bytes()
    async with TestClient(TestServer(_make_app())) as c:
        resp = await c.patch(
            "/api/config/kirocrew",
            json={"path": "dashboard.feature_previews.crew", "value": True, "if_unset": True},
        )
        assert resp.status == 409, await resp.text()
        assert (await resp.json())["code"] == "already_set"
    assert tmp_config.read_bytes() == before


@pytest.mark.asyncio
async def test_a_non_owner_cannot_write_a_switch(tmp_config):
    before = tmp_config.read_bytes()
    with patch(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        return_value=False,
    ):
        async with TestClient(TestServer(_make_app())) as c:
            resp = await c.patch(
                "/api/config/kirocrew",
                json={"path": "dashboard.feature_previews.crew", "value": True},
            )
            assert resp.status in (401, 403), await resp.text()
    assert tmp_config.read_bytes() == before
