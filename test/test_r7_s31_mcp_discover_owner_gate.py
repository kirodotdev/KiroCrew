"""Owner gate on the mutating route of ``handlers/mcp_discover.py``.

``POST /api/mcp/discover/install`` writes an MCP server entry into ``mcp.json``
and rebuilds every rendered agent config.  Its four sibling
mutating MCP routes (``POST /api/mcp/custom``, ``PUT /api/mcp/custom/{name}``,
``POST /api/mcp/apply``, ``POST /api/mcp/remove``) all call
``_shared.require_owner_dashboard_request`` as their first statement, so a
non-owner allow-listed dashboard caller is refused before any body work happens.

This module pins the same pair for the discover install route: the non-owner
caller refused with the shared ``owner_only`` denial, and nothing written on that
path.  The handler is driven for real -- the registrar's own handler object,
reached over a ``TestClient`` -- so a gate added anywhere other than the request
path does not satisfy it.

Providers are replaced with an in-process double: this test performs no network
I/O and never touches the operator's data home.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import mcp_discover
from kiro_crew.mcp_providers.base import (
    McpInstallPlan,
    McpServerDetail,
    ProviderRegistry,
)

pytestmark = pytest.mark.asyncio

_OWNER = "U0OWNER"
_NON_OWNER = "U0NONOWNER"

_REFUSAL = {"error": "owner authorization required", "code": "owner_only"}

_SERVER_ID = "io.example/evil-server"


class _State:
    """Minimum dashboard state the handler and the gate read."""

    owner_id = _OWNER

    def push_refresh(self, _what: str) -> None:  # pragma: no cover - capability path
        return None


class _FakeOfficialProvider:
    """Stands in for the official registry: no network, one installable entry."""

    name = "official"
    display_name = "Official Registry"

    def is_available(self) -> bool:
        return True

    async def search(self, query: str, *, limit: int = 20):  # pragma: no cover
        return []

    async def fetch_detail(self, server_id: str) -> McpServerDetail | None:
        return McpServerDetail(
            id=server_id,
            name="evil-server",
            description="attacker-chosen entry",
            provider="official",
            install_plan=McpInstallPlan(
                method="npx",
                spec={"command": "npx", "args": ["-y", "evil-server"]},
            ),
        )


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Seal the handler off from the operator's home; record what it would write."""
    recorded: list[tuple] = []

    registry = ProviderRegistry()
    registry.register(_FakeOfficialProvider())
    monkeypatch.setattr(mcp_discover, "_get_registry", lambda: registry)

    def _record(name: str, *, enabled: bool, spec: dict | None = None) -> str:
        recorded.append((name, enabled, spec))
        return "added"

    monkeypatch.setattr(mcp_discover, "_set_kirocrew_entry", _record)
    monkeypatch.setattr(mcp_discover, "_find_server_spec_anywhere", lambda _name: None)

    import kiro_crew.agent as agent_module

    monkeypatch.setattr(agent_module, "rebuild_agent_config", lambda *a, **k: None)

    class _Sel:
        def log_api_access(self, **_kw) -> None:
            return None

    monkeypatch.setattr(mcp_discover, "sel", lambda: _Sel())
    return recorded


def _identity(user: str):
    @web.middleware
    async def middleware(request: web.Request, handler):
        request["user"] = user
        request["app"] = ""
        return await handler(request)

    return middleware


async def _client(user: str) -> TestClient:
    app = web.Application(middlewares=[_identity(user)])
    app["state"] = _State()
    app.router.add_post("/api/mcp/discover/install", mcp_discover.api_mcp_discover_install)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_non_owner_cannot_install_a_discovered_mcp_server(writes: list[tuple]) -> None:
    """A non-owner dashboard caller is refused, and nothing is written."""
    client = await _client(_NON_OWNER)
    try:
        resp = await client.post(
            "/api/mcp/discover/install",
            json={"provider": "official", "id": _SERVER_ID},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 403, f"non-owner reached the install body: {resp.status} {body}"
    assert body == _REFUSAL, body
    assert writes == [], f"non-owner install wrote an mcp.json entry: {writes}"


async def test_owner_install_still_works(writes: list[tuple]) -> None:
    """Control: the gate must not close the owner's own install path."""
    client = await _client(_OWNER)
    try:
        resp = await client.post(
            "/api/mcp/discover/install",
            json={"provider": "official", "id": _SERVER_ID},
        )
        body = await resp.json()
    finally:
        await client.close()

    assert resp.status == 200, body
    assert body["ok"] is True, body
    assert [name for name, _enabled, _spec in writes] == ["evil-server"], writes
