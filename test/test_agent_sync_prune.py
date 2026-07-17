"""Tests for agent sync prune logic in dashboard/handlers/agents.py."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.aim_agents import AimAgent
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig


def _make_aim_agent(name: str) -> AimAgent:
    return AimAgent(
        name=name,
        filename=f"local-OmniAgents-{name}.json",
        description=f"{name} agent",
        model="auto",
        source="aim",
        package="OmniAgents",
    )


def _make_config(agents: dict[str, KiroCrewAgentConfig]) -> KiroCrewConfig:
    """Create a MagicMock standing in for KiroCrewConfig with the given agents dict."""
    cfg = MagicMock(spec=KiroCrewConfig)
    cfg.agents = agents
    cfg.default_agent = "kirocrew"
    cfg.save = MagicMock()
    return cfg


async def _run_sync(cfg: KiroCrewConfig, aim_agents_list: list[AimAgent]) -> dict:
    """Invoke the production _do_agents_sync with mocked dependencies and return parsed body."""
    from kiro_crew.dashboard.handlers.agents import _do_agents_sync

    request = MagicMock()
    request.get.return_value = "dashboard"

    sel_mock = MagicMock()

    with (
        patch("kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load", return_value=cfg),
        patch("kiro_crew.dashboard.handlers.agents.list_agents", return_value=aim_agents_list),
        patch("kiro_crew.dashboard.handlers.agents._sel", return_value=sel_mock),
    ):
        response = await _do_agents_sync(request)

    assert response.body is not None
    return json.loads(response.body)


class TestAgentSyncPrune:
    """Tests for the prune step in _do_agents_sync (real production code path)."""

    @pytest.mark.asyncio
    async def test_prune_removes_stale_aim_agents(self):
        """Agents with source='aim' not in scan results get pruned."""
        agents = {
            "omni-reviewer": KiroCrewAgentConfig(kiro_agent="omni-reviewer", source="aim"),
            "omni-aws": KiroCrewAgentConfig(kiro_agent="omni-aws", source="aim"),
            "gpu-dev": KiroCrewAgentConfig(kiro_agent="gpu-dev", source="aim"),
        }
        cfg = _make_config(agents)
        aim_list = [_make_aim_agent("omni-aws"), _make_aim_agent("gpu-dev")]

        body = await _run_sync(cfg, aim_list)

        assert body["pruned"] == ["omni-reviewer"]
        assert "omni-reviewer" not in cfg.agents
        assert "omni-aws" in cfg.agents
        assert "gpu-dev" in cfg.agents
        cfg.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_prune_skips_kirocrew_owned_agents(self):
        """Agents with source='kirocrew' are never pruned."""
        agents = {
            "kirocrew": KiroCrewAgentConfig(kiro_agent="kirocrew", source="kirocrew"),
            "stale-aim": KiroCrewAgentConfig(kiro_agent="stale-aim", source="aim"),
        }
        cfg = _make_config(agents)
        aim_list = [_make_aim_agent("gpu-dev")]

        body = await _run_sync(cfg, aim_list)

        assert "stale-aim" in body["pruned"]
        assert "kirocrew" not in body["pruned"]
        assert "kirocrew" in cfg.agents

    @pytest.mark.asyncio
    async def test_prune_skips_user_created_agents(self):
        """Agents with source='builtin' (user-created) are never pruned."""
        agents = {
            "my-custom": KiroCrewAgentConfig(kiro_agent="my-custom", source="builtin"),
            "stale-aim": KiroCrewAgentConfig(kiro_agent="stale-aim", source="aim"),
        }
        cfg = _make_config(agents)
        aim_list = [_make_aim_agent("gpu-dev")]

        body = await _run_sync(cfg, aim_list)

        assert "stale-aim" in body["pruned"]
        assert "my-custom" not in body["pruned"]
        assert "my-custom" in cfg.agents

    @pytest.mark.asyncio
    async def test_no_prune_when_scan_returns_empty(self):
        """Empty scan result (likely transient failure) should not prune anything."""
        agents = {
            "omni-aws": KiroCrewAgentConfig(kiro_agent="omni-aws", source="aim"),
            "gpu-dev": KiroCrewAgentConfig(kiro_agent="gpu-dev", source="aim"),
        }
        cfg = _make_config(agents)
        aim_list: list[AimAgent] = []

        body = await _run_sync(cfg, aim_list)

        assert body["pruned"] == []
        assert body["synced"] == []
        assert "omni-aws" in cfg.agents
        assert "gpu-dev" in cfg.agents
        cfg.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_and_prune_in_same_sync(self):
        """A single sync both adds new agents and prunes stale ones."""
        agents = {
            "old-agent": KiroCrewAgentConfig(kiro_agent="old-agent", source="aim"),
        }
        cfg = _make_config(agents)
        aim_list = [_make_aim_agent("new-agent")]

        body = await _run_sync(cfg, aim_list)

        assert body["synced"] == ["new-agent"]
        assert body["pruned"] == ["old-agent"]
        assert "new-agent" in cfg.agents
        assert "old-agent" not in cfg.agents
        cfg.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_noop_when_nothing_changed(self):
        """No adds or prunes when config matches scan exactly."""
        agents = {
            "omni-aws": KiroCrewAgentConfig(kiro_agent="omni-aws", source="aim"),
        }
        cfg = _make_config(agents)
        aim_list = [_make_aim_agent("omni-aws")]

        body = await _run_sync(cfg, aim_list)

        assert body["synced"] == []
        assert body["pruned"] == []
        cfg.save.assert_not_called()
