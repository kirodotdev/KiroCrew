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

    with patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed), \
         patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults), \
         patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock), \
         patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg), \
         patch("kiro_crew.agent.build_agent_config", return_value={"toolsSettings": {"execute_bash": {"deniedCommands": ["rm -rf"]}}}), \
         patch("kiro_crew.agent.get_shipped_tools", return_value={"tools": ["a", "c"], "allowedTools": ["b"]}):

        response = await api_agent_config(request)

    assert response.status == 200
    # Verify the handler actually wrote the config files
    assert installed.exists()
    assert json.loads(installed.read_text(encoding="utf-8"))["name"] == "test"
    assert mc_cfg.exists()
    assert json.loads(mc_cfg.read_text(encoding="utf-8"))["removedTools"]["tools"] == ["c"]
