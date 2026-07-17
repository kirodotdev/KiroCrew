"""Tests for api_aim_mcp_registry JSON parsing."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers.agents import api_aim_mcp_registry


def _req() -> make_mocked_request:
    return make_mocked_request("GET", "/api/aim/mcp/registry")


@pytest.mark.asyncio
async def test_aim_not_found():
    """Returns 503 when aim CLI is not installed."""
    with patch("shutil.which", return_value=None):
        resp = await api_aim_mcp_registry(_req())
    assert resp.status == 503
    body = json.loads(resp.body)
    assert "aim CLI not found" in body["error"]


@pytest.mark.asyncio
async def test_aim_nonzero_exit():
    """Returns 500 when aim exits with non-zero code."""
    with patch("shutil.which", return_value="/usr/bin/aim"), \
         patch("kiro_crew.dashboard.handlers.agents._run_aim", new_callable=AsyncMock, return_value=(1, "some error")):
        resp = await api_aim_mcp_registry(_req())
    assert resp.status == 500
    body = json.loads(resp.body)
    assert "some error" in body["error"]


@pytest.mark.asyncio
async def test_no_json_array():
    """Returns 500 when output has no JSON array."""
    with patch("shutil.which", return_value="/usr/bin/aim"), \
         patch("kiro_crew.dashboard.handlers.agents._run_aim", new_callable=AsyncMock, return_value=(0, "no json here")):
        resp = await api_aim_mcp_registry(_req())
    assert resp.status == 500
    body = json.loads(resp.body)
    assert "unexpected" in body["error"]


@pytest.mark.asyncio
async def test_invalid_json():
    """Returns 500 when JSON is malformed."""
    with patch("shutil.which", return_value="/usr/bin/aim"), \
         patch("kiro_crew.dashboard.handlers.agents._run_aim", new_callable=AsyncMock, return_value=(0, "[{broken")):
        resp = await api_aim_mcp_registry(_req())
    assert resp.status == 500
    body = json.loads(resp.body)
    assert "unexpected" in body["error"]


@pytest.mark.asyncio
async def test_parses_servers():
    """Parses a valid JSON array with multiple servers."""
    aim_output = json.dumps([
        {
            "bundleId": "builder-mcp",
            "name": "Amazon Software Builder MCP [Recommended]",
            "description": "# Builder MCP\n\nThe builder MCP server.",
            "isInstalled": True,
            "isLocalOverride": False,
        },
        {
            "bundleId": "slack-mcp",
            "name": "Slack MCP Server [Supported]",
            "description": "Slack integration.",
            "isInstalled": False,
            "isLocalOverride": False,
        },
    ])
    with patch("shutil.which", return_value="/usr/bin/aim"), \
         patch("kiro_crew.dashboard.handlers.agents._run_aim", new_callable=AsyncMock, return_value=(0, aim_output)):
        resp = await api_aim_mcp_registry(_req())
    assert resp.status == 200
    body = json.loads(resp.body)
    servers = body["servers"]
    assert len(servers) == 2

    assert servers[0]["id"] == "builder-mcp"
    assert servers[0]["title"] == "Amazon Software Builder MCP"
    assert servers[0]["tier"] == "Recommended"
    assert servers[0]["installed"] == "yes"
    assert "Builder MCP" in servers[0]["description"]

    assert servers[1]["id"] == "slack-mcp"
    assert servers[1]["title"] == "Slack MCP Server"
    assert servers[1]["tier"] == "Supported"
    assert servers[1]["installed"] == ""


@pytest.mark.asyncio
async def test_no_tier():
    """Servers without a tier badge get empty tier."""
    aim_output = json.dumps([
        {
            "bundleId": "custom-mcp",
            "name": "Custom MCP",
            "description": "A custom server.",
            "isInstalled": False,
            "isLocalOverride": False,
        },
    ])
    with patch("shutil.which", return_value="/usr/bin/aim"), \
         patch("kiro_crew.dashboard.handlers.agents._run_aim", new_callable=AsyncMock, return_value=(0, aim_output)):
        resp = await api_aim_mcp_registry(_req())
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["servers"][0]["tier"] == ""
    assert body["servers"][0]["title"] == "Custom MCP"


@pytest.mark.asyncio
async def test_json_with_leading_noise():
    """Handles output with leading text before the JSON array."""
    aim_output = "Registry MCP Servers:\n" + json.dumps([
        {"bundleId": "test-mcp", "name": "Test [Recommended]", "description": "desc", "isInstalled": False},
    ])
    with patch("shutil.which", return_value="/usr/bin/aim"), \
         patch("kiro_crew.dashboard.handlers.agents._run_aim", new_callable=AsyncMock, return_value=(0, aim_output)):
        resp = await api_aim_mcp_registry(_req())
    assert resp.status == 200
    body = json.loads(resp.body)
    assert len(body["servers"]) == 1
    assert body["servers"][0]["id"] == "test-mcp"


@pytest.mark.asyncio
async def test_json_with_trailing_noise():
    """Handles output with trailing content after the JSON array."""
    aim_output = json.dumps([
        {"bundleId": "test-mcp", "name": "Test", "description": "desc", "isInstalled": False},
    ]) + "\n} extra stuff"
    with patch("shutil.which", return_value="/usr/bin/aim"), \
         patch("kiro_crew.dashboard.handlers.agents._run_aim", new_callable=AsyncMock, return_value=(0, aim_output)):
        resp = await api_aim_mcp_registry(_req())
    assert resp.status == 200
    body = json.loads(resp.body)
    assert len(body["servers"]) == 1


@pytest.mark.asyncio
async def test_description_with_brackets():
    """Descriptions containing brackets don't break parsing."""
    aim_output = json.dumps([
        {
            "bundleId": "bracket-mcp",
            "name": "Bracket Test [Supported]",
            "description": "See [section A] and [section B] for details.",
            "isInstalled": True,
        },
    ])
    with patch("shutil.which", return_value="/usr/bin/aim"), \
         patch("kiro_crew.dashboard.handlers.agents._run_aim", new_callable=AsyncMock, return_value=(0, aim_output)):
        resp = await api_aim_mcp_registry(_req())
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body["servers"][0]["id"] == "bracket-mcp"
    assert "[section A]" in body["servers"][0]["description"]


# ── 503 "aim CLI not found" guards on sibling handlers ─────────────────
# Covers the new `_aim_path()` early-returns added in CR-272306350 r4
# at agents.py:510 (mcp list), 563 (skills list), 743 (agents list).

@pytest.mark.asyncio
async def test_mcp_list_aim_not_found():
    """`api_aim_mcp_list` returns 503 when `aim` is missing from PATH."""
    from kiro_crew.dashboard.handlers.agents import api_aim_mcp_list

    req = make_mocked_request("GET", "/api/aim/mcp")
    with patch("shutil.which", return_value=None):
        resp = await api_aim_mcp_list(req)
    assert resp.status == 503
    body = json.loads(resp.body)
    assert body["error"] == "aim CLI not found"


@pytest.mark.asyncio
async def test_skills_list_aim_not_found():
    """`api_aim_skills_list` returns 503 when `aim` is missing from PATH."""
    from kiro_crew.dashboard.handlers.agents import api_aim_skills_list

    req = make_mocked_request("GET", "/api/aim/skills")
    with patch("shutil.which", return_value=None):
        resp = await api_aim_skills_list(req)
    assert resp.status == 503
    body = json.loads(resp.body)
    assert body["error"] == "aim CLI not found"


@pytest.mark.asyncio
async def test_agents_list_aim_not_found():
    """`api_aim_agents_list` returns 503 when `aim` is missing from PATH."""
    from kiro_crew.dashboard.handlers.agents import api_aim_agents_list

    req = make_mocked_request("GET", "/api/aim/agents")
    with patch("shutil.which", return_value=None):
        resp = await api_aim_agents_list(req)
    assert resp.status == 503
    body = json.loads(resp.body)
    assert body["error"] == "aim CLI not found"
