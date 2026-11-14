"""Tests for ``dashboard.unfiled_sessions_above_folders`` -- the sidebar's placement
of sessions filed in no folder.

Its own module rather than a class in ``test_config_patch.py``: that file sits a
few lines under the 2,000-line cap, so the PATCH helpers it would share are copied
here instead.
"""

from __future__ import annotations

import json
from dataclasses import fields
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import DashboardConfig

_PATH = "dashboard.unfiled_sessions_above_folders"


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


def _seed_config() -> dict:
    return {
        "agents": {
            "kirocrew": {
                "kiro_agent": "kirocrew",
                "workspace": "default",
                "memory_store": "default",
            }
        },
        "default_agent": "kirocrew",
        "session": {"pool_agent": "", "timeout_secs": 3600, "autocompact_pct": 50.0},
        "agent": {"approval_mode": "auto", "sandbox": "auto"},
        "auto_update": False,
    }


@pytest.fixture
def tmp_config(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(_seed_config()), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
        yield cfg_path


async def _patch(client, path, value):
    return await client.patch("/api/config/kirocrew", json={"path": path, "value": value})


class TestUnfiledAboveFoldersRoundTrip:
    """The setting is written by the sidebar's sort-and-filter menu and read back
    by every browser and the desktop app, so what a PATCH stores must be exactly
    what a fresh load reads, and nothing but a boolean may land."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [True, False])
    async def test_each_value_round_trips_through_the_config_file(self, tmp_config, value) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, _PATH, value)
            assert resp.status == 200, await resp.text()
        stored = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert stored["dashboard"]["unfiled_sessions_above_folders"] is value
        assert KiroCrewConfig.load().dashboard.unfiled_sessions_above_folders is value

    @pytest.mark.asyncio
    async def test_a_non_boolean_is_refused_and_the_file_untouched(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, _PATH, True)).status == 200
            stored = tmp_config.read_bytes()
            for bad in ("true", "", 1, 0, None, [True], {"on": True}):
                resp = await _patch(c, _PATH, bad)
                assert resp.status == 400, bad
                assert tmp_config.read_bytes() == stored, bad
        assert KiroCrewConfig.load().dashboard.unfiled_sessions_above_folders is True


class TestUnfiledAboveFoldersField:
    """Default off: a config that never mentions the key draws today's layout, and
    a hand-edited non-boolean cannot flip it."""

    def test_the_default_is_off(self) -> None:
        field = next(
            f for f in fields(DashboardConfig) if f.name == "unfiled_sessions_above_folders"
        )
        assert field.default is False
        assert KiroCrewConfig().dashboard.unfiled_sessions_above_folders is False

    def test_an_absent_key_loads_as_off(self, tmp_config) -> None:
        assert KiroCrewConfig.load().dashboard.unfiled_sessions_above_folders is False

    @pytest.mark.parametrize("raw", ["true", 1, None, [True]])
    def test_a_hand_edited_non_boolean_loads_as_off(self, tmp_config, raw) -> None:
        tmp_config.write_text(
            json.dumps({"dashboard": {"unfiled_sessions_above_folders": raw}}), encoding="utf-8"
        )
        assert KiroCrewConfig.load().dashboard.unfiled_sessions_above_folders is False

    def test_the_allowlist_row_is_a_bool(self) -> None:
        """The PATCH allowlist and the dataclass field are two spellings of one
        setting: pinned so the route cannot accept a shape the loader discards."""
        from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

        assert _EDITABLE_CONFIG[_PATH] == {"type": "bool"}
