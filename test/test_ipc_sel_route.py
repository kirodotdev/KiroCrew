"""``POST /api/ipc/sel``: the gateway writes an MCP stdio server's audit records.

The agent socket gate admits a request with a capability bound to one server and one
session, and holds ``X-Session-Key`` to that session. A middleware here stands in for
that gate, so these tests pin the route alone: the actor comes from the capability,
never from the body; only two kinds are accepted; bodies are capped; each refusal is
itself recorded; a caller the gate did not admit is refused.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.agent_ipc import GRANT_KEY, Binding
from kiro_crew.dashboard.handlers import ipc_sel

_SESSION = "dashboard:chat-1"


class _Rec:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any]]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.rows.append(("log_api_access", kw))

    def log_tool_invocation(self, **kw: Any) -> None:
        self.rows.append(("log_tool_invocation", kw))


@pytest.fixture
def rec(monkeypatch):
    out = _Rec()
    monkeypatch.setattr(ipc_sel, "sel", lambda: out)
    monkeypatch.setattr(ipc_sel, "_buckets", {})
    return out


async def _client(*, admitted: bool = True) -> TestClient:
    @web.middleware
    async def _gate(request, handler):
        if admitted:
            request[GRANT_KEY] = Binding(server="kirocrew-core", session_token="t", pid=1)
        return await handler(request)

    app = web.Application(middlewares=[_gate])
    app.router.add_post(ipc_sel.PATH, ipc_sel.handle_ipc_sel)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _post(client: TestClient, body: Any) -> int:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    resp = await client.post(ipc_sel.PATH, data=raw, headers={"X-Session-Key": _SESSION})
    return resp.status


_TOOL = {
    "method": "log_tool_invocation",
    "kwargs": {"tool_name": "skill_search", "outcome": "completed", "metadata": {"q": "x"}},
}


@pytest.mark.asyncio
async def test_a_record_lands_as_the_capabilitys_actor_tagged_with_its_role(rec):
    client = await _client()
    try:
        assert await _post(client, _TOOL) == 204
    finally:
        await client.close()
    ((kind, kw),) = rec.rows
    assert kind == "log_tool_invocation"
    assert kw["session_key"] == _SESSION and kw["source"] == "mcp"
    assert kw["metadata"] == {
        "q": "x",
        "process_role": "mcp_stdio_server",
        "server": "kirocrew-core",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["session_key", "agent", "source", "caller"])
async def test_a_body_naming_an_actor_is_refused_and_the_refusal_recorded(rec, field):
    body = {"method": "log_tool_invocation", "kwargs": {**_TOOL["kwargs"], field: "dashboard:x"}}
    client = await _client()
    try:
        assert await _post(client, body) == 400
    finally:
        await client.close()
    ((kind, kw),) = rec.rows
    assert kind == "log_api_access"
    assert kw["operation"] == "ipc_sel" and kw["outcome"] == "rejected"
    assert kw["error"] == "sel_invalid_body" and kw["caller"] == _SESSION


@pytest.mark.asyncio
async def test_a_body_cannot_overwrite_the_role_tag(rec):
    body = {
        "method": "log_tool_invocation",
        "kwargs": {**_TOOL["kwargs"], "metadata": {"process_role": "gateway", "server": "x"}},
    }
    client = await _client()
    try:
        assert await _post(client, body) == 204
    finally:
        await client.close()
    ((_kind, kw),) = rec.rows
    assert kw["metadata"]["process_role"] == "mcp_stdio_server"
    assert kw["metadata"]["server"] == "kirocrew-core"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["log_governance_decision", "log_governance_degraded", "log", "log_sel_rotation"]
)
async def test_an_unlisted_kind_is_refused(rec, method):
    client = await _client()
    try:
        assert await _post(client, {"method": method, "kwargs": {"outcome": "x"}}) == 400
    finally:
        await client.close()
    assert [r[1]["error"] for r in rec.rows] == ["sel_invalid_body"]


@pytest.mark.asyncio
async def test_an_oversized_field_and_an_oversized_body_are_refused(rec):
    long_field = {"method": "log_api_access", "kwargs": {"operation": "o" * 501, "outcome": "x"}}
    client = await _client()
    try:
        assert await _post(client, long_field) == 400
        assert await _post(client, b"x" * (16 * 1024 + 1)) == 413
    finally:
        await client.close()
    assert [r[1]["error"] for r in rec.rows] == ["sel_invalid_body", "sel_body_too_large"]


@pytest.mark.asyncio
async def test_a_caller_the_agent_socket_did_not_admit_is_refused(rec):
    client = await _client(admitted=False)
    try:
        assert await _post(client, _TOOL) == 403
    finally:
        await client.close()
    assert rec.rows == []


@pytest.mark.asyncio
async def test_the_rate_limit_answers_429_and_records_one_refusal(rec, monkeypatch):
    from kiro_crew.apps import sel_routes

    monkeypatch.setattr(sel_routes, "_BURST", 2)
    monkeypatch.setattr(sel_routes, "_RATE", 0.0)
    client = await _client()
    try:
        statuses = [await _post(client, _TOOL) for _ in range(4)]
    finally:
        await client.close()
    assert statuses == [204, 204, 429, 429]
    assert [r[1].get("error") for r in rec.rows if r[0] == "log_api_access"] == ["sel_rate_limited"]


def test_the_route_is_a_strict_internal_path():
    from kiro_crew.dashboard.server_internal_paths import _STRICT_INTERNAL_API_PATHS

    assert ipc_sel.PATH in _STRICT_INTERNAL_API_PATHS


def test_the_forwarder_and_the_route_name_the_same_path():
    from kiro_crew import mcp_sel_forward

    assert mcp_sel_forward.PATH == ipc_sel.PATH


def test_every_mcp_stdio_server_redeems_a_pickup_before_it_serves():
    """Each server under the mask needs a capability, or every audited call is refused."""
    import ast
    import importlib
    import importlib.util
    import inspect

    from kiro_crew.acp.ipc_pickup import PICKUP_SERVERS
    from kiro_crew.mcp_entry import SLIM_SERVERS

    for command, (module_name, func) in SLIM_SERVERS.items():
        assert f"kirocrew-{command.removeprefix('mcp-')}" in PICKUP_SERVERS, command
        module = importlib.import_module(module_name)
        source = inspect.getsource(module)
        tool_list = importlib.util.find_spec(f"{module_name}_tool_list")
        if tool_list is not None:
            source += inspect.getsource(importlib.import_module(f"{module_name}_tool_list"))
        called = {
            getattr(node.func, "id", getattr(node.func, "attr", ""))
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
        }
        redeems = called & {"redeem_for_this_server", "redeem_at_startup"}
        assert redeems, f"{module_name}.{func} never redeems the pickup"
