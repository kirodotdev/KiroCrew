"""Enumerate-the-invariant coverage for the remaining MCP/config mutations.

The real ``agent_config`` registrar is walked so a newly registered targeted
mutation cannot silently omit the owner gate. Requests deliberately omit JSON
bodies: a handler that parses before authorizing returns 400 instead of the
required 403.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.routes import agent_config as agent_config_routes

pytestmark = pytest.mark.asyncio

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_TARGET_HANDLER_MODULES = frozenset(
    {
        "kiro_crew.dashboard.handlers.core",
        "kiro_crew.dashboard.handlers.mcp",
        "kiro_crew.dashboard.handlers.mcp_custom",
        "kiro_crew.dashboard.handlers.mcp_discover",
    }
)
_EXPECTED_MUTATING_ROUTES = {
    ("PUT", "/api/config/kirocrew"),
    ("PATCH", "/api/config/kirocrew"),
    ("POST", "/api/mcp/discover/install"),
    ("POST", "/api/mcp/custom"),
    ("PUT", "/api/mcp/custom/{name}"),
    ("POST", "/api/mcp/sync"),
    ("POST", "/api/mcp/apply"),
    ("POST", "/api/mcp/toggle"),
    ("POST", "/api/mcp/toggle-tool"),
    ("POST", "/api/mcp/toggle-all"),
    ("POST", "/api/mcp/remove"),
    ("PUT", "/api/mcp/servers/{name}"),
    ("DELETE", "/api/mcp/servers/{name}"),
    ("POST", "/api/mcp/probe"),
    ("POST", "/api/mcp/quarantine/clear"),
    ("POST", "/api/mcp/measure"),
    ("POST", "/api/mcp-gateway/enable"),
    ("POST", "/api/mcp-gateway/servers/stub"),
    ("POST", "/api/mcp-gateway/resolve-refresh"),
    ("PUT", "/api/config/theme"),
}


class _FakeState:
    owner_id = ""


def _build_app() -> web.Application:
    @web.middleware
    async def _identity(request, handler):
        # Stand-in for token_auth_middleware. The test-only internal flag models
        # the positive marker written after a validated X-Internal-Secret.
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = request.headers.get("X-Test-App", "")
        if request.headers.get("X-Test-Internal") == "1":
            request["internal_auth"] = True
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = _FakeState()
    agent_config_routes.register(app)
    return app


def _target_mutating_routes(app: web.Application) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for route in app.router.routes():
        if route.method not in _MUTATING_METHODS:
            continue
        if getattr(route.handler, "__module__", "") not in _TARGET_HANDLER_MODULES:
            continue
        resource = route.resource
        assert resource is not None
        found.add((route.method, resource.canonical))
    return found


def _route_path(canonical: str) -> str:
    return canonical.replace("{name}", "example")


async def test_route_walk_covers_every_targeted_mutation() -> None:
    app = _build_app()
    found = _target_mutating_routes(app)
    assert found == _EXPECTED_MUTATING_ROUTES, (
        "targeted MCP/config mutation route set drifted: "
        f"missing={sorted(_EXPECTED_MUTATING_ROUTES - found)} "
        f"unexpected={sorted(found - _EXPECTED_MUTATING_ROUTES)}"
    )


async def test_every_targeted_mutation_rejects_non_owner_before_body_parse() -> None:
    app = _build_app()
    async with TestClient(TestServer(app)) as client:
        for method, canonical in sorted(_target_mutating_routes(app)):
            response = await client.request(
                method,
                _route_path(canonical),
                headers={"X-Test-User": "someone-else"},
            )
            assert response.status == 403, (
                f"{method} {canonical} answered {response.status}; "
                "owner authorization must run before body parsing"
            )
            body = await response.json()
            assert body["code"] == "owner_only", (method, canonical)


async def test_every_targeted_mutation_rejects_app_tokens() -> None:
    app = _build_app()
    async with TestClient(TestServer(app)) as client:
        for method, canonical in sorted(_target_mutating_routes(app)):
            response = await client.request(
                method,
                _route_path(canonical),
                headers={"X-Test-App": "some-app"},
            )
            assert (
                response.status == 403
            ), f"{method} {canonical} answered {response.status} for an app token"
            body = await response.json()
            assert body["code"] == "owner_only", (method, canonical)


async def test_internal_auth_exception_is_limited_to_server_registration() -> None:
    app = _build_app()
    async with TestClient(TestServer(app)) as client:
        # The internal caller is admitted by the server-registration handler and
        # reaches body parsing; no mutation occurs because the body is absent.
        response = await client.put(
            "/api/mcp/servers/example",
            headers={"X-Test-Internal": "1"},
        )
        assert response.status == 400

        # The same internal marker must not bypass any other targeted mutation.
        response = await client.post(
            "/api/mcp/sync",
            headers={"X-Test-Internal": "1", "X-Test-User": "someone-else"},
        )
        assert response.status == 403
        assert (await response.json())["code"] == "owner_only"
