"""POST /api/mcp/probe/{name} through the real route table.

Registers the production routes (``routes.agent_config.register``), so the test
catches a route that is not wired, a handler that is not the one the facade
exposes, and the owner gate. The probe is real: it runs against a command that
does not exist, so nothing is spawned.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import mcp_probe
from kiro_crew.dashboard.routes import agent_config
from kiro_crew.mcp_discovery_server_info import McpServerInfo

MISSING = "/nonexistent/kc-probe-one-route-test-binary"


@web.middleware
async def _auth(request: web.Request, handler):
    # The owner gate reads these claims; "app" is an app caller, not the owner.
    who = request.headers.get("X-Test-Auth", "")
    if who == "owner":
        request["user"] = "local-app"
        request["app"] = ""
    elif who == "app":
        request["user"] = "local-app"
        request["app"] = "some-app"
    return await handler(request)


async def _with_client(coro_fn, monkeypatch, servers):
    import kiro_crew.mcp_discovery as disc

    monkeypatch.setattr(disc, "list_servers", lambda: servers)
    monkeypatch.setattr(mcp_probe, "_read_global_mcps", lambda: {})
    monkeypatch.setattr(mcp_probe, "_kirocrew_mcp_servers", lambda: {})
    monkeypatch.setattr(mcp_probe, "_record_probe_verdicts", lambda rows: None)

    app = web.Application(middlewares=[_auth])
    app["state"] = type("S", (), {"_background_tasks": set(), "sessions": None})()
    agent_config.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        return await coro_fn(client)
    finally:
        await client.close()


def _server(name: str) -> McpServerInfo:
    return McpServerInfo(name=name, command=MISSING)


@pytest.mark.asyncio
async def test_owner_probe_of_one_server_answers_its_row(monkeypatch):
    async def run(client):
        resp = await client.post("/api/mcp/probe/alpha", headers={"X-Test-Auth": "owner"})
        assert resp.status == 200
        body = json.loads(await resp.text())
        assert (body["name"], body["status"]) == ("alpha", "error")

    await _with_client(run, monkeypatch, [_server("alpha"), _server("beta")])


@pytest.mark.asyncio
async def test_unknown_name_through_the_router_is_404(monkeypatch):
    async def run(client):
        resp = await client.post("/api/mcp/probe/missing", headers={"X-Test-Auth": "owner"})
        assert resp.status == 404

    await _with_client(run, monkeypatch, [_server("alpha")])


@pytest.mark.asyncio
async def test_a_non_owner_app_is_refused(monkeypatch):
    async def run(client):
        resp = await client.post("/api/mcp/probe/alpha", headers={"X-Test-Auth": "app"})
        assert resp.status == 403

    await _with_client(run, monkeypatch, [_server("alpha")])
