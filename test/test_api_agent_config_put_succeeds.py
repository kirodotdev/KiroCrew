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
