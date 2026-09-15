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

    @pytest.mark.asyncio
    async def test_malformed_project_path_does_not_crash_the_request(self, tmp_path):
        """GPT 5.6 Review F3: an embedded null byte makes
        ``os.path.realpath`` raise ``ValueError`` with no guard in
        ``resolve_project_path``. Before the fix, that exception escaped the
        `run_in_executor` await in ``api_kirocrew_agents`` uncaught, turning
        an ordinary owner request into a bare 500 instead of the same
        "not a usable directory" outcome any other invalid path already
        gets. The legitimate caller (JobForm's ProjectPicker) can never emit
        this value; only a raw query string can.
        """
        clear_project_agent_cache()
        state = _make_state(tmp_path)

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
                lambda request: True,
            ),
        ):
            async with TestClient(TestServer(_make_agents_app(state))) as client:
                resp = await client.get("/api/agents", params={"project_path": "/tmp/\x00bad"})
                # The request must survive -- no bare 500 -- and simply carry
                # no project-scoped agents, the same outcome a nonexistent
                # directory already produces.
                assert resp.status == 200
                data = await resp.json()
        assert data["agents"] == [
            a for a in data["agents"] if a.get("source") != "project"
        ], "a malformed project_path must resolve to no project rows, not crash"

    @pytest.mark.asyncio
    async def test_nonexistent_project_path_is_audited_as_denied(self, tmp_path):
        """GPT 5.6 Review F1: a nonexistent/non-directory ``project_path`` is
        neither ``denied`` (not sensitive) nor ``resolved`` (not a valid
        existing directory), so before the fix NEITHER audit branch fired --
        an owner-only permission decision with no record at all. A nonempty,
        unresolved path must now log a ``denied`` SEL event naming the raw
        input (``resolved`` is always empty on this branch)."""
        clear_project_agent_cache()
        state = _make_state(tmp_path)
        missing = str(tmp_path / "does-not-exist")

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
                lambda request: True,
            ),
            patch("kiro_crew.dashboard.handlers.agents.sel") as mock_sel,
        ):
            async with TestClient(TestServer(_make_agents_app(state))) as client:
                resp = await client.get("/api/agents", params={"project_path": missing})
                assert resp.status == 200

        calls = [
            c
            for c in mock_sel.return_value.log_api_access.call_args_list
            if c.kwargs.get("operation") == "api_kirocrew_agents.project_path"
        ]
        assert len(calls) == 1, "exactly one audit event for this owner-gated decision"
        assert calls[0].kwargs["outcome"] == "denied"
        assert calls[0].kwargs["resources"] == missing
        assert calls[0].kwargs["error"] == "not a usable directory"
