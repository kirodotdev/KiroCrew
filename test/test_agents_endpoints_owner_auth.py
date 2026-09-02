"""Every mutating agents-module route refuses a non-owner caller with 403.

Enumerate-the-invariant coverage for the owner boundary on
``handlers/agents.py``: rather than one test per handler, the real route
registrars are walked and EVERY mutating verb that resolves to a handler
defined in that module must refuse a request lacking the owner dashboard
identity — before any body parsing, filesystem write, or config load (the
requests below deliberately carry no JSON body, so a handler that parsed the
body first would answer 400, not 403). A new mutating route added to the
module without the gate fails this test by construction.

``~/.kiro/agents`` and ``cfg.agents`` are machine-global — a write installs
tool grants and MCP server commands that later sessions execute — so the
boundary is ``is_owner_dashboard_request``, the same predicate that already
gates ``mcp_apps.api_mcp_apps_call`` and the source-provider mutations.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.routes import agent_config as agent_config_routes
from kiro_crew.dashboard.routes import agents as agents_routes

pytestmark = pytest.mark.asyncio

_AGENTS_HANDLER_MODULE = "kiro_crew.dashboard.handlers.agents"
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Coherence floor so the router walk cannot go vacuous (a refactor that moved
#: handlers to another module would otherwise silently empty the enumeration
#: and the loop below would assert nothing). The walk is authoritative — routes
#: it finds beyond this set are still checked.
_EXPECTED_MUTATING_ROUTES = {
    ("PUT", "/api/agent/config"),
    ("PUT", "/api/config/default-agent"),
    ("POST", "/api/capability/mcp/install"),
    ("POST", "/api/capability/mcp/uninstall"),
    ("POST", "/api/capability/skills/install"),
    ("POST", "/api/capability/skills/uninstall"),
    ("POST", "/api/capability/agents/install"),
    ("POST", "/api/capability/agents/uninstall"),
    ("POST", "/api/capability/plugins/sync"),
    ("PATCH", "/api/agents/detail/{name}"),
    ("DELETE", "/api/agents/detail/{name}"),
    ("POST", "/api/agents"),
    ("POST", "/api/agents/sync"),
    ("PUT", "/api/agents/{name}"),
    ("DELETE", "/api/agents/{name}"),
}


class _FakeState:
    """No owner configured: only the signed local bootstrap subjects pass."""

    owner_id = ""


def _build_app() -> web.Application:
    @web.middleware
    async def _identity(request, handler):
        # Stand-in for the token-auth middleware: the AUTHENTICATED claims the
        # owner predicate reads. Tests select the caller via headers.
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = _FakeState()
    agents_routes.register(app)
    agent_config_routes.register(app)
    return app


def _mutating_agents_routes(app: web.Application) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for route in app.router.routes():
        if route.method not in _MUTATING_METHODS:
            continue
        if getattr(route.handler, "__module__", "") != _AGENTS_HANDLER_MODULE:
            continue
        resource = route.resource
        assert resource is not None
        found.add((route.method, resource.canonical))
    return found


async def test_route_walk_covers_the_known_mutating_set() -> None:
    """The enumeration itself is asserted, so it can never pass vacuously."""
    app = _build_app()
    found = _mutating_agents_routes(app)
    missing = _EXPECTED_MUTATING_ROUTES - found
    assert not missing, f"router walk lost known mutating routes: {sorted(missing)}"


async def test_every_mutating_agents_route_refuses_non_owner() -> None:
    """A non-owner dashboard subject gets 403 on every mutating route."""
    app = _build_app()
    routes = _mutating_agents_routes(app)
    assert routes  # belt and braces; the walk test above pins the floor
    async with TestClient(TestServer(app)) as client:
        for method, canonical in sorted(routes):
            path = canonical.replace("{name}", "some-agent")
            resp = await client.request(method, path, headers={"X-Test-User": "someone-else"})
            assert resp.status == 403, (
                f"{method} {canonical} answered {resp.status} for a non-owner "
                "subject — every mutating agents route must be owner-gated"
            )
            body = await resp.json()
            assert "owner authorization" in body["error"], (method, canonical)
            assert body["code"] == "owner_only", (method, canonical)


async def test_every_mutating_agents_route_refuses_app_tokens() -> None:
    """An app-scoped token is refused even when the subject string matches."""
    app = _build_app()
    routes = _mutating_agents_routes(app)
    async with TestClient(TestServer(app)) as client:
        for method, canonical in sorted(routes):
            path = canonical.replace("{name}", "some-agent")
            resp = await client.request(method, path, headers={"X-Test-App": "some-app"})
            assert resp.status == 403, (
                f"{method} {canonical} answered {resp.status} for an "
                "app-scoped token — apps must not drive agent-spec mutations"
            )


async def test_read_only_agents_routes_stay_open() -> None:
    """The gate covers mutations only: GETs in the module are not owner-gated.

    Pins the boundary's shape (mutating verbs, not the whole module) so the
    gate cannot silently widen into read paths the roster UI depends on.
    """
    app = _build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/api/config/default-agent", headers={"X-Test-User": "someone-else"}
        )
        assert resp.status != 403
