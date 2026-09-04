"""Every mutating route registered by the agent_config registrar refuses non-owner callers.

Unlike ``test_agents_endpoints_owner_auth.py`` which filters by handler module
(``handlers.agents``), this test walks ALL mutating routes registered by both
the ``agent_config`` and ``agents`` route registrars without filtering by
handler module. This ensures that new routes added to ANY handler module in the
registrar cannot silently escape the owner gate invariant.

A mutating route (POST/PUT/PATCH/DELETE) that is registered through the
registrar must either:
1. Return 401 or 403 for a non-owner caller, OR
2. Be explicitly documented in one of the exclusion sets below with a
   justification for why the route intentionally skips the owner gate.

The coherence floor ``_MINIMUM_GATED_ROUTES`` prevents the walk from going
vacuous if a refactor empties the route table.
"""

from __future__ import annotations

import re

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.routes import agent_config as agent_config_routes
from kiro_crew.dashboard.routes import agents as agents_routes

pytestmark = pytest.mark.asyncio

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# --------------------------------------------------------------------------- #
# Pre-owner exclusions: routes that intentionally only require authentication
# (not ownership) because they execute during the initial onboarding flow
# BEFORE an owner is configured.
# --------------------------------------------------------------------------- #
_PRE_OWNER_EXCLUSIONS: frozenset[tuple[str, str]] = frozenset(
    {
        # Onboarding import routes use ``_caller()`` which checks authentication
        # but not ownership. These routes run during initial setup when the user
        # is importing configuration from another installation -- there is no
        # configured owner yet, so the owner gate cannot apply.
        ("POST", "/api/onboarding/import/apply"),
        ("PUT", "/api/onboarding/import/state"),
    }
)

# --------------------------------------------------------------------------- #
# Known ungated routes: mutating routes that predate the owner-gating effort.
# These routes are currently authenticated-only (not owner-gated). They are
# candidates for future owner-gating work but are NOT in scope for the current
# migration PR. Each entry documents WHY it is excluded.
#
# IMPORTANT: This set must NOT grow. Any NEW mutating route added to the
# registrar that is not owner-gated must either be added to _PRE_OWNER_EXCLUSIONS
# (with justification) or must implement require_owner_dashboard_request.
# The test_known_ungated_routes_not_growing test enforces this via the
# _MAX_KNOWN_UNGATED_ROUTES ceiling below.
# --------------------------------------------------------------------------- #
_KNOWN_UNGATED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        # --- MCP management routes (mcp.py) ---
        # These routes configure MCP servers and assume any authenticated
        # dashboard session may manage MCP configuration. Owner-gating them
        # is tracked as future work.
        ("POST", "/api/mcp/probe"),
        ("POST", "/api/mcp/quarantine/clear"),
        ("POST", "/api/mcp/measure"),
        ("POST", "/api/mcp/sync"),
        ("POST", "/api/mcp/apply"),
        ("POST", "/api/mcp/toggle"),
        ("POST", "/api/mcp/toggle-tool"),
        ("POST", "/api/mcp/toggle-all"),
        ("POST", "/api/mcp/remove"),
        ("PUT", "/api/mcp/servers/{name}"),
        ("DELETE", "/api/mcp/servers/{name}"),
        ("POST", "/api/mcp-gateway/enable"),
        ("POST", "/api/mcp-gateway/servers/stub"),
        ("POST", "/api/mcp-gateway/resolve-refresh"),
        # --- MCP custom/discover routes (mcp_custom.py, mcp_discover.py) ---
        # Custom MCP server registration and discovery-based installation.
        # Authenticated-only, predating the owner-gate migration.
        ("POST", "/api/mcp/custom"),
        ("PUT", "/api/mcp/custom/{name}"),
        ("POST", "/api/mcp/discover/install"),
        # --- Kiro Crew config routes (core.py) ---
        # Application-level configuration routes that predated owner-gating.
        # They configure the Kiro Crew application itself but were registered
        # before ownership semantics existed.
        ("PUT", "/api/config/kirocrew"),
        ("PATCH", "/api/config/kirocrew"),
        ("PUT", "/api/config/theme"),
    }
)

# Ceiling for the debt list above, asserted by
# ``test_known_ungated_routes_not_growing``. Raising it is the reviewable act
# that adding a new ungated route costs; lower it whenever an entry is gated and
# removed, so the ratchet stays tight.
_MAX_KNOWN_UNGATED_ROUTES = 20

# --------------------------------------------------------------------------- #
# Coherence floor: the walk must find at least this many GATED mutating routes.
# This is the count of owner-gated routes the walk enforces, measured live at
# issue #8505 (registered by handlers.agents, handlers.connections,
# handlers.files, and handlers.members). Keep it equal to the real count -- a
# slack floor cannot catch a refactor that silently drops routes out of the
# walk. Hardcoded deliberately: deriving it from the walk itself would
# reintroduce the vacuity one level up. If routes are intentionally removed,
# the assertion fails and you must lower the floor in the same commit. If
# routes are added, the ``>=`` assertion still passes, so raising the floor
# back to the real count is a manual, unenforced step -- do it whenever you
# touch this file, or the slack this floor exists to prevent regrows.
# --------------------------------------------------------------------------- #
_MINIMUM_GATED_ROUTES = 26


class _FakeState:
    """Simulates a state object with no configured owner.

    When ``owner_id`` is empty the ``is_owner_dashboard_request`` predicate
    rejects all callers that are not the signed local bootstrap subject
    (``local-app``, ``local-startup``).
    """

    owner_id = ""


def _build_app() -> web.Application:
    """Build a minimal app with both route registrars and identity middleware."""

    @web.middleware
    async def _identity(request, handler):
        # Stand-in for the token-auth middleware: populates the AUTHENTICATED
        # claims that the owner predicate reads. Tests select the caller via
        # headers so we can simulate a non-owner caller.
        request["user"] = request.headers.get("X-Test-User", "local-app")
        request["app"] = request.headers.get("X-Test-App", "")
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = _FakeState()
    # Register BOTH registrars so the walk covers the full route surface.
    agents_routes.register(app)
    agent_config_routes.register(app)
    return app


def _all_mutating_routes(app: web.Application) -> set[tuple[str, str]]:
    """Enumerate every mutating route from the app router.

    Does NOT filter by handler module -- walks all routes regardless of which
    handler module they resolve to. This is the key difference from the
    per-module test in ``test_agents_endpoints_owner_auth.py``.
    """
    found: set[tuple[str, str]] = set()
    for route in app.router.routes():
        if route.method not in _MUTATING_METHODS:
            continue
        resource = route.resource
        if resource is None:
            continue
        found.add((route.method, resource.canonical))
    return found


def _gated_routes(app: web.Application) -> set[tuple[str, str]]:
    """Return only the mutating routes that are expected to be owner-gated.

    This is the full set of mutating routes minus both exclusion sets.
    """
    all_routes = _all_mutating_routes(app)
    return all_routes - _PRE_OWNER_EXCLUSIONS - _KNOWN_UNGATED_ROUTES


def _substitute_path_params(canonical: str) -> str:
    """Replace ``{param}`` placeholders with a dummy value for requests."""
    return re.sub(r"\{[^}]+\}", "test-item", canonical)


async def test_walk_finds_minimum_gated_routes() -> None:
    """The enumeration finds at least the coherence floor of gated routes.

    Guards against the walk going vacuous if a refactor moves or removes route
    registrations.
    """
    app = _build_app()
    found = _gated_routes(app)
    assert len(found) >= _MINIMUM_GATED_ROUTES, (
        f"Route walk found only {len(found)} gated mutating routes "
        f"(expected >= {_MINIMUM_GATED_ROUTES}). "
        f"If routes were intentionally removed, lower the floor. "
        f"Found: {sorted(found)}"
    )


async def test_every_gated_route_refuses_non_owner() -> None:
    """A non-owner dashboard subject gets 401 or 403 on every gated route.

    The request carries no JSON body and uses a non-owner identity header.
    The owner gate must fire BEFORE body parsing or state access, so the
    denial response should always come first.

    We accept both 401 (stale session relabel) and 403 (direct owner denial)
    as valid denial responses.
    """
    app = _build_app()
    routes = _gated_routes(app)
    assert routes, "no gated mutating routes found -- test setup is broken"

    async with TestClient(TestServer(app)) as client:
        for method, canonical in sorted(routes):
            path = _substitute_path_params(canonical)
            resp = await client.request(method, path, headers={"X-Test-User": "someone-else"})
            assert resp.status in (401, 403), (
                f"{method} {canonical} answered {resp.status} for a non-owner "
                f"subject -- every gated mutating route must return 401 or 403 "
                f"(or be explicitly listed in _PRE_OWNER_EXCLUSIONS or "
                f"_KNOWN_UNGATED_ROUTES)"
            )


async def test_known_ungated_routes_not_growing() -> None:
    """The ungated set is a shrink-only ratchet -- it must never grow.

    This is the tightening direction of the invariant. ``_KNOWN_UNGATED_ROUTES``
    is the debt list: mutating routes that predate owner-gating and are still
    authenticated-only. Every entry is registered (asserted by
    ``test_exclusion_sets_are_actually_registered``) and every route OUTSIDE the
    two exclusion sets is proven to refuse non-owners (asserted by
    ``test_every_gated_route_refuses_non_owner``).

    What neither of those catches is a NEW ungated route being added to the
    registrar and quietly appended to this set to keep CI green. Pinning the
    ceiling makes that edit visible: the ceiling has to be raised in the same
    diff, which is a reviewable act rather than a silent one. Gating a route and
    deleting its entry is the only change that needs no ceiling edit -- lower
    ``_MAX_KNOWN_UNGATED_ROUTES`` to the new size to keep the ratchet tight.
    """
    assert len(_KNOWN_UNGATED_ROUTES) <= _MAX_KNOWN_UNGATED_ROUTES, (
        f"_KNOWN_UNGATED_ROUTES grew to {len(_KNOWN_UNGATED_ROUTES)} entries "
        f"(ceiling {_MAX_KNOWN_UNGATED_ROUTES}). A new mutating route must be "
        f"owner-gated, not added to the debt list. If the addition is genuinely "
        f"unavoidable, raise the ceiling in the same diff and say why."
    )
    # The two sets must stay disjoint. An entry in both is excluded twice, so
    # removing it from one -- the natural move when a pre-owner route grows up --
    # silently leaves it excluded by the other and the walk never tests it.
    overlap = _PRE_OWNER_EXCLUSIONS & _KNOWN_UNGATED_ROUTES
    assert not overlap, f"a route is excluded by BOTH sets: {sorted(overlap)}"


async def test_exclusion_sets_are_actually_registered() -> None:
    """Every route in both exclusion sets must actually exist in the router.

    Prevents stale exclusions from hiding the removal of a once-excluded route.
    If a route is removed from the registrar, it must also be removed from the
    exclusion sets.
    """
    app = _build_app()
    all_routes: set[tuple[str, str]] = set()
    for route in app.router.routes():
        resource = route.resource
        if resource is None:
            continue
        all_routes.add((route.method, resource.canonical))

    for method, path in _PRE_OWNER_EXCLUSIONS:
        assert (method, path) in all_routes, (
            f"Pre-owner excluded route {method} {path} is not registered. "
            f"Remove it from _PRE_OWNER_EXCLUSIONS or fix the route registration."
        )

    for method, path in _KNOWN_UNGATED_ROUTES:
        assert (method, path) in all_routes, (
            f"Known-ungated route {method} {path} is not registered. "
            f"Remove it from _KNOWN_UNGATED_ROUTES or fix the route registration."
        )
