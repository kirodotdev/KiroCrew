"""Tests for /api/agents frequency ordering (api_kirocrew_agents).

The endpoint reorders the agent roster by per-agent chat-session frequency
(most-used first), degrading to config-insertion order when history is
unreadable. These tests pin both the ordering and the fallback contract.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewAgentConfig

DEFAULT_AGENT = "alpha"
CONFIG_ORDER = ["alpha", "beta", "gamma"]


def _fake_config(names):
    """A stand-in KiroCrewConfig: ordered agents dict + default_agent."""
    return SimpleNamespace(
        agents={name: KiroCrewAgentConfig(kiro_agent=name) for name in names},
        default_agent=DEFAULT_AGENT,
    )


def _make_agents_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.agents import api_kirocrew_agents

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/agents", api_kirocrew_agents)
    return app


async def _get_agents(state, names):
    with patch(
        "kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load",
        return_value=_fake_config(names),
    ):
        async with TestClient(TestServer(_make_agents_app(state))) as client:
            resp = await client.get("/api/agents")
            assert resp.status == 200
            data = await resp.json()
    return data


class TestAgentOrdering:
    @pytest.mark.asyncio
    async def test_more_sessions_ranks_higher(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("s1", "user", "hi", agent="beta")
        log.append("s2", "user", "hi", agent="beta")
        log.append("s3", "user", "hi", agent="alpha")

        data = await _get_agents(state, CONFIG_ORDER)

        order = [a["name"] for a in data["agents"]]
        assert order.index("beta") < order.index("alpha")

    @pytest.mark.asyncio
    async def test_never_used_stable_bottom_in_config_order(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.conversation_log.append("s1", "user", "hi", agent="gamma")

        data = await _get_agents(state, CONFIG_ORDER)
        order = [a["name"] for a in data["agents"]]

        assert order[0] == "gamma"
        # Never-used alpha, beta follow in config-insertion order.
        assert order[1:] == ["alpha", "beta"]

        # Determinism across reloads.
        data2 = await _get_agents(state, CONFIG_ORDER)
        assert [a["name"] for a in data2["agents"]] == order

    @pytest.mark.asyncio
    async def test_tie_break_recency_wins(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("s_beta", "user", "hi", agent="beta")
        log.append("s_alpha", "user", "hi", agent="alpha")
        # Equal count (1 each); make beta more recent than alpha.
        os.utime(tmp_path / "s_alpha.jsonl", (1000, 1000))
        os.utime(tmp_path / "s_beta.jsonl", (5000, 5000))

        data = await _get_agents(state, CONFIG_ORDER)
        order = [a["name"] for a in data["agents"]]

        assert order.index("beta") < order.index("alpha")

    @pytest.mark.asyncio
    async def test_tie_break_equal_recency_falls_to_config_index(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("s_beta", "user", "hi", agent="beta")
        log.append("s_alpha", "user", "hi", agent="alpha")
        # Equal count AND equal recency → config insertion_index breaks the tie.
        os.utime(tmp_path / "s_alpha.jsonl", (3000, 3000))
        os.utime(tmp_path / "s_beta.jsonl", (3000, 3000))

        data = await _get_agents(state, CONFIG_ORDER)
        order = [a["name"] for a in data["agents"]]

        # alpha precedes beta in CONFIG_ORDER, so alpha wins the equal-key tie.
        assert order.index("alpha") < order.index("beta")

    @pytest.mark.asyncio
    async def test_agent_set_and_default_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.conversation_log.append("s1", "user", "hi", agent="gamma")

        data = await _get_agents(state, CONFIG_ORDER)

        assert sorted(a["name"] for a in data["agents"]) == sorted(CONFIG_ORDER)
        assert data["default_agent"] == DEFAULT_AGENT


class TestAgentOrderingFallback:
    @pytest.mark.asyncio
    async def test_history_unreadable_returns_config_order(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        with patch.object(
            state.conversation_log, "agent_usage", side_effect=OSError("boom")
        ):
            data = await _get_agents(state, CONFIG_ORDER)

        order = [a["name"] for a in data["agents"]]
        assert order == CONFIG_ORDER
        assert data["default_agent"] == DEFAULT_AGENT

    @pytest.mark.asyncio
    async def test_no_conversation_log_returns_config_order(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.conversation_log = None

        data = await _get_agents(state, CONFIG_ORDER)

        order = [a["name"] for a in data["agents"]]
        assert order == CONFIG_ORDER
        assert data["default_agent"] == DEFAULT_AGENT


class TestProjectScopeRoster:
    """/api/agents surfaces the session project's agents (#1684's headline).

    Rows carry ``scope``: config aliases are ``"global"``, project discoveries
    ``"project"``. A name in both scopes lists once, as the alias — dispatch
    resolves aliases first, so the alias is what would answer.
    """

    @pytest.mark.asyncio
    async def test_project_agent_appears_with_project_scope(self, tmp_path, monkeypatch):
        import json as _json

        from kiro_crew.agent_discovery import clear_project_agent_cache

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        proj = tmp_path / "repo"
        (proj / ".kiro" / "agents").mkdir(parents=True)
        (proj / ".kiro" / "agents" / "repo-bot.json").write_text(_json.dumps({"name": "repo-bot"}))
        clear_project_agent_cache()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.active_project_dir",
            lambda state, key: str(proj),
        )
        state = _make_state(tmp_path)

        data = await _get_agents(state, CONFIG_ORDER)

        rows = {a["name"]: a for a in data["agents"]}
        assert "repo-bot" in rows, f"project agent missing from roster: {list(rows)}"
        assert rows["repo-bot"]["scope"] == "project"
        assert rows["alpha"]["scope"] == "global"

    @pytest.mark.asyncio
    async def test_alias_shadows_project_agent_of_same_name(self, tmp_path, monkeypatch):
        import json as _json

        from kiro_crew.agent_discovery import clear_project_agent_cache

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        proj = tmp_path / "repo"
        (proj / ".kiro" / "agents").mkdir(parents=True)
        (proj / ".kiro" / "agents" / "alpha.json").write_text(_json.dumps({"name": "alpha"}))
        clear_project_agent_cache()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.active_project_dir",
            lambda state, key: str(proj),
        )
        state = _make_state(tmp_path)

        data = await _get_agents(state, CONFIG_ORDER)

        alphas = [a for a in data["agents"] if a["name"] == "alpha"]
        assert len(alphas) == 1, "alias + project twin must list once"
        assert alphas[0]["scope"] == "global"

    @pytest.mark.asyncio
    async def test_no_project_dir_keeps_roster_global_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.agents.active_project_dir",
            lambda state, key: "",
        )
        state = _make_state(tmp_path)

        data = await _get_agents(state, CONFIG_ORDER)

        assert [a["name"] for a in data["agents"]] == CONFIG_ORDER
        assert all(a["scope"] == "global" for a in data["agents"])
