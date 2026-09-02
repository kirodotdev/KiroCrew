"""Tests for the /api/capability/* handlers over the CapabilityManager seam.

Registry/CLI output parsing now lives in the edition's CapabilityManager (the
core only passes results through), so these tests exercise the handler
contract: 503 when the manager is unavailable, pass-through of structured
results when it is, and 500 on operation errors.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers import agents as agents_mod


class FakeManager:
    """Minimal in-memory CapabilityManager for handler tests."""

    def __init__(self, *, available=True, registry=None, mcp=None, skills=None, agents=None, raise_on=None):
        self._available = available
        self._registry = registry if registry is not None else []
        self._mcp = mcp if mcp is not None else []
        self._skills = skills if skills is not None else []
        self._agents = agents if agents is not None else []
        self._raise_on = set(raise_on or ())

    def available(self) -> bool:
        return self._available

    async def _maybe(self, op, val):
        if op in self._raise_on:
            raise RuntimeError("boom")
        return val

    async def list_mcp(self):
        return await self._maybe("list_mcp", self._mcp)

    async def registry(self):
        return await self._maybe("registry", self._registry)

    async def list_skills(self):
        return await self._maybe("list_skills", self._skills)

    async def list_agents(self):
        return await self._maybe("list_agents", self._agents)


def _patch(mgr):
    return patch("kiro_crew.dashboard.handlers.agents._capability_manager", return_value=mgr)


def _req(path):
    return make_mocked_request("GET", path)


@pytest.mark.asyncio
async def test_registry_unavailable_503():
    with _patch(FakeManager(available=False)):
        resp = await agents_mod.api_capability_mcp_registry(_req("/api/capability/mcp/registry"))
    assert resp.status == 503
    assert json.loads(resp.body)["error"] == "capability manager not available"


@pytest.mark.asyncio
async def test_registry_passthrough():
    """The manager owns parsing; the handler passes entries through verbatim."""
    entries = [
        {"id": "builder-mcp", "installed": "yes", "title": "Builder", "tier": "Recommended", "description": "d"},
        {"id": "slack-mcp", "installed": "", "title": "Slack", "tier": "Supported", "description": "d2"},
    ]
    with _patch(FakeManager(registry=entries)):
        resp = await agents_mod.api_capability_mcp_registry(_req("/api/capability/mcp/registry"))
    assert resp.status == 200
    assert json.loads(resp.body)["servers"] == entries


@pytest.mark.asyncio
async def test_registry_operation_error_500():
    with _patch(FakeManager(raise_on={"registry"})):
        resp = await agents_mod.api_capability_mcp_registry(_req("/api/capability/mcp/registry"))
    assert resp.status == 500


@pytest.mark.asyncio
async def test_mcp_list_unavailable_503():
    with _patch(FakeManager(available=False)):
        resp = await agents_mod.api_capability_mcp_list(_req("/api/capability/mcp"))
    assert resp.status == 503
    assert json.loads(resp.body)["error"] == "capability manager not available"


@pytest.mark.asyncio
async def test_mcp_list_passthrough():
    servers = [{"name": "server-a", "server_id": "a"}, {"name": "server-b"}]
    with _patch(FakeManager(mcp=servers)):
        resp = await agents_mod.api_capability_mcp_list(_req("/api/capability/mcp"))
    assert resp.status == 200
    assert json.loads(resp.body) == servers


@pytest.mark.asyncio
async def test_skills_list_unavailable_503():
    with _patch(FakeManager(available=False)):
        resp = await agents_mod.api_capability_skills_list(_req("/api/capability/skills"))
    assert resp.status == 503


@pytest.mark.asyncio
async def test_skills_list_passthrough():
    skills = [{"key": "package/pkg-a", "name": "pkg-a", "source": "package"}]
    with _patch(FakeManager(skills=skills)):
        resp = await agents_mod.api_capability_skills_list(_req("/api/capability/skills"))
    assert resp.status == 200
    assert json.loads(resp.body) == skills


@pytest.mark.asyncio
async def test_agents_list_unavailable_503():
    with _patch(FakeManager(available=False)):
        resp = await agents_mod.api_capability_agents_list(_req("/api/capability/agents"))
    assert resp.status == 503


@pytest.mark.asyncio
async def test_agents_list_passthrough():
    agents = [{"name": "agent-pkg", "package": "agent-pkg"}]
    with _patch(FakeManager(agents=agents)):
        resp = await agents_mod.api_capability_agents_list(_req("/api/capability/agents"))
    assert resp.status == 200
    assert json.loads(resp.body) == agents
