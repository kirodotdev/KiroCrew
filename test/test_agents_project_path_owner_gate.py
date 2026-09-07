"""Owner gate on the ``GET /api/agents?project_path=`` fallback.

The raw ``project_path`` query-param fallback (added for the Schedule job
form, which has no live chat slot to key off of) has no owner check.
``is_sensitive_path`` guards only credential homes, not the multi-human
authorization boundary, so an allow-listed messaging user's non-owner
``!dashboard`` token (``app == ""``, which sails through every app-token
check) could name an arbitrary absolute path and read back that directory's
project agent names via ``_agent_roster_row`` -- a read no other caller's
project scope could ever cross into. These tests lock in that the fallback is
gated on the same ``is_owner_dashboard_request`` predicate this module's
mutating routes already use.
"""

from __future__ import annotations

import json as _json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.agent_discovery import clear_project_agent_cache
from kiro_crew.config.loader import KiroCrewAgentConfig


def _fake_config():
    return SimpleNamespace(
        agents={"alpha": KiroCrewAgentConfig(kiro_agent="alpha")},
        default_agent="alpha",
    )


def _make_agents_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.agents import api_kirocrew_agents

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/agents", api_kirocrew_agents)
    return app


async def _get_agents_with_project_path(state, project_path: str, *, owner: bool):
    with (
        patch(
            "kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load",
            return_value=_fake_config(),
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents.requesting_slot_project",
            lambda state, key: None,
        ),
        patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            lambda request: owner,
        ),
    ):
        async with TestClient(TestServer(_make_agents_app(state))) as client:
            resp = await client.get("/api/agents", params={"project_path": project_path})
            assert resp.status == 200
            data = await resp.json()
    return data


class TestProjectPathFallbackOwnerGate:
    @pytest.mark.asyncio
    async def test_non_owner_project_path_is_ignored(self, tmp_path):
        proj = tmp_path / "repo"
        (proj / ".kiro" / "agents").mkdir(parents=True)
        (proj / ".kiro" / "agents" / "repo-bot.json").write_text(_json.dumps({"name": "repo-bot"}))
        clear_project_agent_cache()
        state = _make_state(tmp_path)

        data = await _get_agents_with_project_path(state, str(proj), owner=False)

        names = {a["name"] for a in data["agents"]}
        assert "repo-bot" not in names, (
            "a non-owner request must never resolve project_path -- the "
            "fallback must be silently ignored, not surfaced as an error "
            "that would confirm the path's existence either way"
        )

    @pytest.mark.asyncio
    async def test_owner_project_path_still_resolves(self, tmp_path):
        proj = tmp_path / "repo"
        (proj / ".kiro" / "agents").mkdir(parents=True)
        (proj / ".kiro" / "agents" / "repo-bot.json").write_text(_json.dumps({"name": "repo-bot"}))
        clear_project_agent_cache()
        state = _make_state(tmp_path)

        data = await _get_agents_with_project_path(state, str(proj), owner=True)

        names = {a["name"] for a in data["agents"]}
        assert "repo-bot" in names, "the owner's own request must still resolve project_path"
