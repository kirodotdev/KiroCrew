"""``agent.subagent_light_lean_context``: a light sub-agent starts without lessons and memory.

With ``agent.subagent_light_tier`` and this setting both on, a spawn marked ``light``
starts without the lessons and memory blocks unless the spawn names ``include_lessons``
or ``include_memory`` as true. Every other spawn, and every spawn while the setting is
off (the default), starts with exactly the context groups it starts with without it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import AgentConfig, KiroCrewConfig
from kiro_crew.subagent import _context_groups_of

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.fixture(autouse=True)
def _close_subagent_managers(close_subagent_managers):
    """Every manager built here is closed at teardown; the body is in ``conftest``."""


def _cfg(*, tier: bool = True, lean: bool | None = True) -> KiroCrewConfig:
    """A config with the light tier and the lean setting as asked; ``None`` leaves the default."""
    agent = AgentConfig(
        role_models={"subagent_light": "claude-haiku-4.5"}, role_efforts={"subagent_light": "low"}
    )
    # Set after construction so a tree without these settings still builds the config.
    agent.subagent_light_tier = tier
    if lean is not None:
        agent.subagent_light_lean_context = lean
    return KiroCrewConfig(agent=agent)


async def _spawn(body: dict[str, Any], cfg: KiroCrewConfig) -> tuple[dict[str, Any], Any]:
    """POST /api/spawn on a real manager whose runs never start: (response, run record)."""
    from kiro_crew.dashboard.handlers.messaging import api_spawn
    from kiro_crew.subagent import SubagentManager

    ctx = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.has_session = MagicMock(return_value=True)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
    mgr._run = AsyncMock()  # type: ignore[method-assign]
    request = MagicMock()
    request.app = {
        "state": SimpleNamespace(subagents=mgr, sessions=sessions, conversation_log=MagicMock())
    }
    request.headers = {}
    request.json = AsyncMock(return_value=body)
    with (
        patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(lambda c: cfg)),
        patch("kiro_crew.subagent._validate_agent", lambda agent, cwd="": (agent, "", "")),
    ):
        resp = await api_spawn(request)
    assert resp.status == 200, resp.text
    data = json.loads(resp.text)
    return data, mgr.get(data["id"])


def _kept(info: Any) -> tuple[bool, bool, bool]:
    """(memory, lessons, project) the run starts with."""
    return (info.include_memory, info.include_lessons, info.include_project)


def _tool(args: dict[str, Any]) -> list[dict]:
    """Run spawn_run with a canned gateway reply and return the POSTed bodies."""
    from kiro_crew import mcp_core

    bodies: list[dict] = []

    def _fake_post(path: str, body: dict) -> dict:
        if path == "/api/spawn":
            bodies.append(body)
        return {"id": "a1"}

    with (
        patch.object(mcp_core, "_post", side_effect=_fake_post),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        mcp_core._call_tool("spawn_run", args)
    return bodies


class TestPremiseOnMain:
    """What a light spawn starts with today: the settings do not change it."""

    @pytest.mark.asyncio
    async def test_a_light_spawn_with_the_tier_on_still_starts_with_every_group(self):
        _, info = await _spawn({"task": "x", "light": True}, _cfg(lean=None))
        assert _kept(info) == (True, True, True)
        assert _context_groups_of(info) >= {"memory", "lessons"}

    def test_the_setting_is_off_by_default(self):
        assert AgentConfig().subagent_light_lean_context is False


class TestGatewayLeanOn:
    @pytest.mark.asyncio
    async def test_a_light_spawn_starts_without_lessons_and_memory(self):
        data, info = await _spawn({"task": "x", "light": True}, _cfg())
        assert _kept(info) == (False, False, True)
        assert _context_groups_of(info).isdisjoint({"memory", "lessons"})
        assert "lean context" in data["light"]

    @pytest.mark.asyncio
    async def test_a_named_true_keeps_that_group_only(self):
        _, info = await _spawn({"task": "x", "light": True, "include_lessons": True}, _cfg())
        assert _kept(info) == (False, True, True)
        _, info = await _spawn({"task": "x", "light": True, "include_memory": True}, _cfg())
        assert _kept(info) == (True, False, True)

    @pytest.mark.asyncio
    async def test_a_named_false_and_the_project_group_stay_as_named(self):
        _, info = await _spawn(
            {"task": "x", "light": True, "include_memory": False, "include_project": False}, _cfg()
        )
        assert _kept(info) == (False, False, False)

    @pytest.mark.asyncio
    async def test_a_lean_only_setup_needs_no_light_pins(self):
        cfg = _cfg()
        cfg.agent.role_models = {}
        cfg.agent.role_efforts = {}
        data, info = await _spawn({"task": "x", "light": True}, cfg)
        assert _kept(info) == (False, False, True)
        assert "warning" not in data

    @pytest.mark.asyncio
    async def test_a_run_that_names_its_own_agent_or_backend_is_not_narrowed(self):
        _, info = await _spawn({"task": "x", "light": True, "agent": "reviewer"}, _cfg())
        assert _kept(info) == (True, True, True)
        # The named backend reads as installed, so the result does not depend on this host.
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO

        resolved = AsyncMock(return_value=(ACP_BACKEND_KIRO, None, None))
        with patch("kiro_crew.dashboard.handlers.messaging._requested_backend", resolved):
            _, info = await _spawn({"task": "x", "light": True, "backend": "kiro"}, _cfg())
        assert _kept(info) == (True, True, True)


class TestGatewayKeepsMain:
    @pytest.mark.asyncio
    async def test_the_setting_off_starts_a_light_spawn_with_every_group(self):
        data, info = await _spawn({"task": "x", "light": True}, _cfg(lean=False))
        assert _kept(info) == (True, True, True)
        assert "lean context" not in data.get("light", "")

    @pytest.mark.asyncio
    async def test_the_tier_off_starts_a_light_spawn_with_every_group(self):
        data, info = await _spawn({"task": "x", "light": True}, _cfg(tier=False))
        assert _kept(info) == (True, True, True)
        assert "light" not in data

    @pytest.mark.asyncio
    async def test_a_spawn_without_the_flag_starts_with_every_group(self):
        _, info = await _spawn({"task": "x"}, _cfg())
        assert _kept(info) == (True, True, True)
        _, info = await _spawn({"task": "x", "light": False}, _cfg())
        assert _kept(info) == (True, True, True)

    @pytest.mark.asyncio
    async def test_an_unreadable_configuration_keeps_every_group(self):
        def _boom(_cls):
            raise OSError("unreadable")

        with patch("kiro_crew.config.loader.KiroCrewConfig.load", classmethod(_boom)):
            from kiro_crew.dashboard.messaging_api.spawn import _light_tier_pins

            result = await _light_tier_pins(
                {"light": True},
                "",
                "",
                named_elsewhere=False,
                execution=None,
                backend=None,
            )
        assert result[4] is False


class TestToolForwarding:
    def test_a_call_that_names_nothing_posts_no_include_key(self):
        for args in ({"task": "x"}, {"task": "x", "light": True}):
            (body,) = _tool(args)
            assert not {"include_memory", "include_lessons", "include_project"} & body.keys()

    def test_a_light_task_that_names_true_forwards_it(self):
        (body,) = _tool({"task": "x", "light": True, "include_lessons": True})
        assert body["include_lessons"] is True and "include_memory" not in body

    def test_a_task_that_is_not_light_forwards_no_true(self):
        (body,) = _tool({"task": "x", "include_lessons": True, "include_memory": True})
        assert not {"include_memory", "include_lessons"} & body.keys()

    def test_a_named_false_is_forwarded_as_before(self):
        (body,) = _tool({"task": "x", "light": True, "include_memory": False})
        assert body["include_memory"] is False and "include_lessons" not in body
