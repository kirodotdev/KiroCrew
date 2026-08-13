"""Integration test for api_agent_config PUT.

Regression test for bug where local variable 'config_path' shadowed the
imported config_path() function, causing "'PosixPath' object is not callable".
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import api_agent_config


@pytest.mark.asyncio
async def test_api_agent_config_put_succeeds(tmp_path):
    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {"config": {"name": "test", "tools": ["a"], "allowedTools": ["b"]}}

    request.json = mock_json

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch(
            "kiro_crew.agent.build_agent_config",
            return_value={"toolsSettings": {"execute_bash": {"deniedCommands": ["rm -rf"]}}},
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            return_value={"tools": ["a", "c"], "allowedTools": ["b"]},
        ),
    ):

        response = await api_agent_config(request)

    assert response.status == 200
    # Verify the handler actually wrote the config files
    assert installed.exists()
    assert json.loads(installed.read_text(encoding="utf-8"))["name"] == "test"
    assert mc_cfg.exists()
    assert json.loads(mc_cfg.read_text(encoding="utf-8"))["removedTools"]["tools"] == ["c"]


@pytest.mark.asyncio
async def test_api_agent_config_put_strips_governed_grants(tmp_path, monkeypatch):
    """A dashboard PUT persists the config verbatim, so it MUST run the whole map
    through the governance filter — else a governed @denied allowedTools entry or
    a governed server's autoApprove written here restores the bypass the per-ref
    writers close. Executable (not source-inspection) coverage of that writer."""
    import kiro_crew.platform.governance as gov

    # Govern @denied only; everything else may auto-approve.
    monkeypatch.setattr(gov, "may_skip_gate_now", lambda ref: ref != "@denied")

    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {
            "config": {
                "name": "test",
                # mount list must NOT be filtered (mounting != auto-approving)
                "tools": ["@denied", "@ok"],
                "allowedTools": ["@ok", "@denied"],
                "mcpServers": {
                    "denied": {"url": "u", "autoApprove": ["dangerous"]},
                    "ok": {"url": "u", "autoApprove": ["fine"]},
                },
            }
        }

    request.json = mock_json

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch("kiro_crew.dashboard.handlers.agents.get_shipped_tools", return_value={"tools": [], "allowedTools": []}),
    ):
        response = await api_agent_config(request)

    assert response.status == 200
    written = json.loads(installed.read_text(encoding="utf-8"))
    # Governed @denied dropped from auto-approve; ungoverned @ok kept.
    assert written["allowedTools"] == ["@ok"]
    # Mount list is untouched — @denied stays mounted, just not auto-approved.
    assert written["tools"] == ["@denied", "@ok"]
    # Governed server loses autoApprove; ungoverned server keeps it.
    assert "autoApprove" not in written["mcpServers"]["denied"]
    assert written["mcpServers"]["ok"]["autoApprove"] == ["fine"]


@pytest.mark.asyncio
async def test_api_agent_config_put_strips_bookkeeping_keys(tmp_path):
    """A dashboard PUT must not re-pollute the kiro spec with Kiro Crew keys.

    Regression for #2570: the agent-detail PATCH strips ``model_managed`` /
    ``cc_model``, but the whole-config PUT used to persist them verbatim.
    kiro-cli ``deny_unknown_fields`` then rejects the entire agent until the
    next ``migrate_agent_specs`` heal on gateway rebuild.
    """
    from kiro_crew import agent_state

    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {
            "config": {
                "name": "kirocrew",
                "tools": ["a"],
                "allowedTools": ["b"],
                "model_managed": True,
                "cc_model": "claude-sonnet-4.6",
            }
        }

    request.json = mock_json

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch("kiro_crew.agent.get_shipped_tools", return_value={"tools": [], "allowedTools": []}),
    ):
        response = await api_agent_config(request)

    assert response.status == 200
    written = json.loads(installed.read_text(encoding="utf-8"))
    assert "model_managed" not in written
    assert "cc_model" not in written
    assert written["name"] == "kirocrew"
    # Lifted into the sidecar when previously unset (same rule as migrate).
    assert agent_state.get_model_managed("kirocrew") is True
    assert agent_state.get_cc_model("kirocrew") == "claude-sonnet-4.6"


@pytest.mark.asyncio
async def test_api_agent_config_put_does_not_clobber_sidecar(tmp_path):
    """A stale bookkeeping key in the PUT body must not overwrite the sidecar."""
    from kiro_crew import agent_state

    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    agent_state.set_model_managed("kirocrew", False)
    agent_state.set_cc_model("kirocrew", "test-model-stub")

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {
            "config": {
                "name": "kirocrew",
                "tools": ["a"],
                "allowedTools": ["b"],
                "model_managed": True,
                "cc_model": "claude-sonnet-4.6",
            }
        }

    request.json = mock_json

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch("kiro_crew.agent.get_shipped_tools", return_value={"tools": [], "allowedTools": []}),
    ):
        response = await api_agent_config(request)

    assert response.status == 200
    written = json.loads(installed.read_text(encoding="utf-8"))
    assert "model_managed" not in written
    assert "cc_model" not in written
    assert agent_state.get_model_managed("kirocrew") is False
    assert agent_state.get_cc_model("kirocrew") == "test-model-stub"
