"""``GET /api/agents?project_path=`` serves the PROJECT row on a name collision.

A name declared both in ``cfg.agents`` and by the bound project resolves to the
project's definition at fire time: kiro-cli searches ``<project>/.kiro/agents``
before the user-level directory and the fire runs with the bound folder as cwd.
The roster therefore has to serve the project row and suppress the global one --
serving the global row would advertise an agent that cannot run in that
directory, so the picker would offer one definition while another answers.

The endpoint must not compute ``project_names - set(cfg.agents.keys())``: that
keeps the losing side, so a colliding project agent produces NO row at all and
the global row is served in its place. It also makes the picker's ``overrides
global`` marker unreachable, because the frontend can only mark a collision it
can see.
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
    """A global roster whose ``reviewer`` is the name the project also declares.

    ``description``/``memory_store`` are set so the served row can be attributed
    to one side or the other rather than merely counted.
    """
    return SimpleNamespace(
        agents={
            "alpha": KiroCrewAgentConfig(kiro_agent="alpha"),
            "reviewer": KiroCrewAgentConfig(
                kiro_agent="reviewer-template",
                description="GLOBAL reviewer",
                memory_store="reviewer-private",
            ),
        },
        default_agent="alpha",
    )


def _make_agents_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.agents import api_kirocrew_agents

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/agents", api_kirocrew_agents)
    return app


async def _roster(state, project_path: str):
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
            resp = await client.get("/api/agents", params={"project_path": project_path})
            assert resp.status == 200
            return await resp.json()


def _seed(tmp_path, *names: str):
    proj = tmp_path / "repo"
    (proj / ".kiro" / "agents").mkdir(parents=True)
    for name in names:
        (proj / ".kiro" / "agents" / f"{name}.json").write_text(_json.dumps({"name": name}))
    clear_project_agent_cache()
    return proj


class TestProjectRowWinsACollision:
    @pytest.mark.asyncio
    async def test_colliding_name_is_served_as_the_project_row(self, tmp_path):
        proj = _seed(tmp_path, "reviewer")
        state = _make_state(tmp_path)

        data = await _roster(state, str(proj))

        rows = [a for a in data["agents"] if a["name"] == "reviewer"]
        assert len(rows) == 1, (
            "exactly one row per name -- two rows would offer a choice the job's "
            f"bare-string agent field cannot record, got {rows}"
        )
        assert rows[0]["scope"] == "project", (
            "the project definition is what a fire in this directory resolves, so "
            "it must be the row served; serving the global row advertises an agent "
            "that cannot run here"
        )

    @pytest.mark.asyncio
    async def test_the_shadowed_global_row_is_not_also_served(self, tmp_path):
        """The losing row must be gone, not merely outranked.

        Leaving it in would keep the frontend's dedup responsible for choosing a
        winner -- the split responsibility that produced the original defect.
        """
        proj = _seed(tmp_path, "reviewer")
        state = _make_state(tmp_path)

        data = await _roster(state, str(proj))

        assert [a["scope"] for a in data["agents"] if a["name"] == "reviewer"] == ["project"]
        assert not [
            a for a in data["agents"] if a["name"] == "reviewer" and a["scope"] == "global"
        ], "the shadowed global row must be suppressed"

    @pytest.mark.asyncio
    async def test_the_project_row_does_not_inherit_the_shadowed_private_store(self, tmp_path):
        """The winner lands on the default record, NOT the shadowed alias's.

        A ``<project>/.kiro/agents/*.json`` is writable by anyone who can land a
        branch. Inheriting the shadowed crew's named memory store would turn
        landing a branch into a read of that crew's memory.
        """
        proj = _seed(tmp_path, "reviewer")
        state = _make_state(tmp_path)

        data = await _roster(state, str(proj))

        row = next(a for a in data["agents"] if a["name"] == "reviewer")
        assert (
            row["memory_store"] != "reviewer-private"
        ), "the project row must not carry the shadowed alias's private store"
        assert row["description"] != "GLOBAL reviewer", (
            "nor its description -- the row would then describe the definition "
            "that is NOT running"
        )

    @pytest.mark.asyncio
    async def test_non_colliding_rows_on_both_sides_are_untouched(self, tmp_path):
        """The change is scoped to the collision case.

        A project-only agent still appears, and an unrelated global alias is not
        collaterally dropped by the suppression.
        """
        proj = _seed(tmp_path, "reviewer", "repo-only")
        state = _make_state(tmp_path)

        data = await _roster(state, str(proj))

        by_name = {a["name"]: a for a in data["agents"]}
        assert by_name["repo-only"]["scope"] == "project"
        assert (
            by_name["alpha"]["scope"] == "global"
        ), "an unrelated global alias must survive the collision suppression"

    @pytest.mark.asyncio
    async def test_no_project_path_leaves_the_global_roster_whole(self, tmp_path):
        """Unbound: no project scope exists, so nothing is suppressed."""
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
                resp = await client.get("/api/agents")
                assert resp.status == 200
                data = await resp.json()

        row = next(a for a in data["agents"] if a["name"] == "reviewer")
        assert row["scope"] == "global"
        assert row["description"] == "GLOBAL reviewer", (
            "with no bound folder the configured alias is the one that runs, so "
            "its own record must be served"
        )

    def test_the_handler_docstring_states_the_rule_this_class_pins(self):
        """The endpoint's docstring is the contract a reader meets first, and it
        must agree with the behaviour above. Saying "listed once, as the alias:
        dispatch resolves aliases first, so the alias is what would answer"
        states the inverse of both the row the code serves AND of dispatch,
        where a project definition shadows a same-named alias
        (``_resolve_agent_selection``'s project-override step). A contract that
        states the inverse of the tested behaviour is worse than none."""
        from kiro_crew.dashboard.handlers.agents import api_kirocrew_agents

        doc = api_kirocrew_agents.__doc__ or ""
        assert "as the alias" not in doc, (
            "the docstring still says a collision is served as the ALIAS row; "
            "the code serves the project row and drops the alias row"
        )
        assert "dispatch resolves aliases first" not in doc, (
            "the docstring still claims dispatch resolves aliases first; a project "
            "definition shadows a same-named alias in dispatch"
        )
        assert "PROJECT row" in doc and "shadows" in doc, (
            "the docstring must state that a colliding name is served as the "
            "project row because the project definition shadows the alias"
        )
