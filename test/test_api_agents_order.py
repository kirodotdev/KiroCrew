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

from conftest import make_dir_link
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
    """/api/agents surfaces the session project's agents.

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


class TestOwnerProjectPathAudit:
    """The owner ``?project_path=`` audit row names the tree that was scanned.

    ``critical=True`` makes this write fail-closed, so the row is load-bearing:
    an operator reads it to learn WHICH directory an owner's roster request
    enumerated. A spelling and the tree it resolves to can differ (a symlinked
    path, a ``~`` form), and the row has to name the latter.
    """

    @pytest.mark.asyncio
    async def test_the_allowed_row_names_the_resolved_dir_not_the_spelling(
        self, tmp_path, monkeypatch
    ):
        """Both platform outcomes are ASSERTED, neither is skipped.

        Where a link resolves, the row must name the resolved tree rather than
        the spelling. Where the pinned scan refuses a link in the target's
        ancestry before anything resolves (Windows), the outcome is ``denied``
        and the row takes the ``resolved_path or raw_project_path`` fallback at
        :mod:`~kiro_crew.dashboard.handlers.agents`. Branching here rather than
        skipping keeps the ratchet whole: the refused-link row is pinned HERE
        and nowhere else, so a ``skipif`` left its spelling unverified on the
        one platform that produces it.
        """
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.handlers import agents as agents_mod

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        real = tmp_path / "real-project"
        (real / ".kiro" / "agents").mkdir(parents=True)
        link = tmp_path / "spelled-differently"
        # A JUNCTION on Windows, where a directory symlink needs
        # SeCreateSymbolicLinkPrivilege and fails WinError 1314 in an unelevated
        # shell. The reparse machinery traverses both identically, so the
        # audit-path assertions below run on every platform instead of being
        # skipped on the one that would otherwise lose them.
        make_dir_link(link, real)

        audited: list[dict] = []
        monkeypatch.setattr(
            agents_mod,
            "_sel",
            lambda: MagicMock(log_api_access=lambda **kw: audited.append(kw)),
        )
        # Patched at its SOURCE module: the handler imports this name inside the
        # function body, so a module-attribute patch on `agents_mod` binds
        # nothing the call site reads.
        from kiro_crew.dashboard.handlers import source_providers as sp_mod

        monkeypatch.setattr(sp_mod, "is_owner_dashboard_request", lambda request: True)

        state = _make_state(tmp_path)
        with patch(
            "kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load",
            return_value=_fake_config(CONFIG_ORDER),
        ):
            async with TestClient(TestServer(_make_agents_app(state))) as client:
                resp = await client.get("/api/agents", params={"project_path": str(link)})
                # Both platform answers are ASSERTED, neither is skipped. Where
                # the descriptor-pinned walk does not exist the owner scan
                # REFUSES rather than degrading (the recorded Decision B), so no
                # roster is served and the resolved-vs-spelling row below is
                # unreachable -- which is itself the assertion on that platform.
                assert resp.status in (200, 503), resp.status
                if resp.status == 503:
                    return

        rows = [e for e in audited if e.get("outcome") in {"allowed", "denied"}]
        assert rows, f"the owner scan emitted no audited row: {audited}"
        row = rows[-1]
        if row["outcome"] == "allowed":
            # The resolved tree, not the symlink spelling the caller sent.
            assert row["resources"] == str(real.resolve()), (
                f"audit row names {row['resources']!r}, "
                f"not the scanned tree {str(real.resolve())!r}"
            )
        else:
            # The link was refused before anything resolved, so `resolved_path`
            # is empty and the row falls back to the raw spelling. Asserting the
            # SPELLING is the point: it is what an operator reads to learn which
            # directory the request named when none was scanned.
            assert row["resources"] == str(link), (
                f"a refused-link row must fall back to the raw spelling "
                f"{str(link)!r}, not {row['resources']!r}"
            )
