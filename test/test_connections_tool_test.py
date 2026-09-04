"""Authenticated Connections Test verdicts through the kiro-cli command surface.

The fake client is the process boundary: these tests never spawn kiro-cli.  The
production path asks kiro-cli for both ``/mcp`` (server liveness + tools/list
count) and ``/tools`` (the tools the active agent actually exposes).
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.agent_sdk.drivers import acp as acp_driver
from kiro_crew.connections import get_provider, tool_test
from kiro_crew.dashboard.handlers import connections


def _provider() -> dict[str, Any]:
    provider = deepcopy(get_provider("linear"))
    assert provider is not None
    return provider


def _mcp_result(*, status: str = "running", tool_count: int = 2) -> dict[str, Any]:
    return {
        "data": {
            "servers": [
                {
                    "name": "linear",
                    "status": status,
                    "toolCount": tool_count,
                    "authenticating": False,
                }
            ],
            "mode": "status",
        }
    }


def _tools_result(*names: str) -> dict[str, Any]:
    return {
        "data": {
            "tools": [
                {
                    "name": name,
                    "source": "mcp:linear",
                    "description": "",
                    "status": "requires-approval",
                }
                for name in names
            ]
        }
    }


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    class FakeClient:
        scripted: list[object] = []
        instances: list["FakeClient"] = []

        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.commands: list[str] = []
            self.shutdown_called = False
            self.__class__.instances.append(self)

        async def ensure_ready(self) -> None:
            return None

        async def command_result(self, command: str) -> dict[str, Any]:
            self.commands.append(command)
            outcome = self.__class__.scripted.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            assert isinstance(outcome, dict)
            return outcome

        async def shutdown(self) -> None:
            self.shutdown_called = True

    monkeypatch.setattr(acp_driver, "_native_command_client_factory", lambda: FakeClient)
    monkeypatch.setattr("kiro_crew.sandbox.configured_sandbox_mode", lambda: "strict")
    monkeypatch.setattr(tool_test, "data_home", lambda: tmp_path)
    return FakeClient


@pytest.mark.asyncio
async def test_usable_requires_running_server_and_agent_exposed_tools(fake_client):
    fake_client.scripted = [_mcp_result(tool_count=3), _tools_result("list_issues", "get_issue")]

    result = await tool_test.test_connection_tools(_provider())

    assert result == {
        "schema_version": 1,
        "slug": "linear",
        "verdict": "usable",
        "code": "tools_available",
        "toolCount": 2,
    }
    client = fake_client.instances[-1]
    assert client.commands == ["/mcp", "/tools"]
    assert client.shutdown_called is True
    assert client.kwargs["agent"] == "kirocrew"
    assert client.kwargs["sandbox_mode"] == "strict"


@pytest.mark.asyncio
async def test_running_server_with_no_agent_exposed_tools_is_honest_zero(fake_client):
    fake_client.scripted = [
        _mcp_result(tool_count=3),
        {"data": {"tools": [{"name": "fs_read", "source": "built-in", "status": "allowed"}]}},
    ]

    result = await tool_test.test_connection_tools(_provider())

    assert result["verdict"] == "no_tools"
    assert result["code"] == "no_tools_exposed"
    assert result["toolCount"] == 0


@pytest.mark.asyncio
async def test_server_failure_is_not_laundered_into_zero_tools(fake_client):
    fake_client.scripted = [_mcp_result(status="failed", tool_count=0), _tools_result()]

    result = await tool_test.test_connection_tools(_provider())

    assert result == {
        "schema_version": 1,
        "slug": "linear",
        "verdict": "failed",
        "code": "mcp_server_failed",
        "toolCount": 0,
    }
    assert fake_client.instances[-1].commands == ["/mcp", "/tools"]
    assert fake_client.instances[-1].shutdown_called is True


@pytest.mark.asyncio
async def test_timeout_returns_failed_and_still_shuts_down(fake_client):
    fake_client.scripted = [asyncio.TimeoutError()]

    result = await tool_test.test_connection_tools(_provider())

    assert result["verdict"] == "failed"
    assert result["code"] == "connection_test_timeout"
    assert fake_client.instances[-1].shutdown_called is True


@pytest.mark.asyncio
async def test_cancellation_propagates_after_client_shutdown(fake_client, monkeypatch):
    command_started = asyncio.Event()

    async def blocked_command(client, command: str) -> dict[str, Any]:
        client.commands.append(command)
        command_started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    monkeypatch.setattr(fake_client, "command_result", blocked_command)
    task = asyncio.create_task(tool_test.test_connection_tools(_provider()))
    await asyncio.wait_for(command_started.wait(), timeout=1.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert fake_client.instances[-1].shutdown_called is True


@pytest.mark.asyncio
async def test_malformed_command_inventory_fails_closed(fake_client):
    fake_client.scripted = [{"data": {"servers": "not-a-list"}}, _tools_result()]

    result = await tool_test.test_connection_tools(_provider())

    assert result["verdict"] == "failed"
    assert result["code"] == "invalid_tool_inventory"


async def _client() -> TestClient:
    app = web.Application()
    app.router.add_post("/api/connections/test", connections.api_connections_test)
    as_owner(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_endpoint_returns_the_tri_level_payload(monkeypatch: pytest.MonkeyPatch):
    expected = {
        "schema_version": 1,
        "slug": "linear",
        "verdict": "no_tools",
        "code": "no_tools_exposed",
        "toolCount": 0,
    }

    async def fake_test(provider):
        assert provider["slug"] == "linear"
        return expected

    monkeypatch.setattr(tool_test, "test_connection_tools", fake_test)
    client = await _client()
    try:
        response = await client.post("/api/connections/test", json={"slug": "linear"})
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 200
    assert body == expected


@pytest.mark.asyncio
async def test_endpoint_unknown_provider_keeps_machine_code():
    client = await _client()
    try:
        response = await client.post("/api/connections/test", json={"slug": "missing"})
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 400
    assert body["code"] == "unknown_provider"
