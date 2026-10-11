"""Route Registry — middleware-based soft routing for app-provided HTTP handlers.

Uses an internal routing table instead of aiohttp's UrlDispatcher to support
dynamic enable/disable without gateway restart. A single catch-all route
dispatches to the internal table.

Supports both exact paths and path parameters (e.g. /tasks/{task_id}/comments).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web

from kiro_crew.apps.context import AppContext
from kiro_crew.apps.manifest import (
    MAX_AGENT_ROUTES_PER_APP,
    agent_route_matches,
    parse_agent_route,
)
from kiro_crew.apps.module_loader import load_app_module, unload_app_modules
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)


@dataclass
class AppRoute:
    """A route declared by an app's route registration function."""

    method: str  # HTTP method (uppercase): GET, POST, PUT, DELETE, PATCH
    path: str  # relative path starting with / (e.g. "/status", "/tasks/{task_id}")
    handler: Callable[[web.Request, AppContext], Awaitable[web.Response]]


# ---------------------------------------------------------------------------
# Path pattern matching
# ---------------------------------------------------------------------------

# Matches {param_name} segments in route paths
_PARAM_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _compile_pattern(path: str) -> tuple[re.Pattern[str], list[str]]:
    """Compile a route path with {params} into a regex pattern.

    Returns (compiled_regex, list_of_param_names).
    """
    params: list[str] = []
    regex_parts: list[str] = []
    last_end = 0

    for match in _PARAM_RE.finditer(path):
        # Literal segment before this param
        literal = path[last_end:match.start()]
        regex_parts.append(re.escape(literal))
        # Param segment — match anything except /
        params.append(match.group(1))
        regex_parts.append(r"([^/]+)")
        last_end = match.end()

    # Trailing literal
    regex_parts.append(re.escape(path[last_end:]))
    pattern = re.compile("^" + "".join(regex_parts) + "$")
    return pattern, params


def _has_params(path: str) -> bool:
    """Check if a path contains {param} segments."""
    return bool(_PARAM_RE.search(path))


def _match_path_pattern(
    route_pattern: str, actual_path: str
) -> dict[str, str] | None:
    """Try to match actual_path against a route pattern with {params}.

    Returns dict of matched params or None if no match.
    """
    compiled, param_names = _compile_pattern(route_pattern)
    m = compiled.match(actual_path)
    if not m:
        return None
    return dict(zip(param_names, m.groups()))


# ---------------------------------------------------------------------------
# Route Registry
# ---------------------------------------------------------------------------


@dataclass
class _RegisteredRoute:
    """Internal representation of a registered route."""

    method: str
    path: str  # original path pattern (e.g. "/tasks/{task_id}")
    handler: Callable[[web.Request, AppContext], Awaitable[web.Response]]
    has_params: bool
    compiled: re.Pattern[str] | None = None
    param_names: list[str] | None = None


class RouteRegistry:
    """Middleware-based soft routing for app-provided HTTP handlers.

    Instead of registering routes directly with aiohttp's UrlDispatcher
    (which doesn't support removal), this maintains an internal routing
    table and dispatches via a catch-all handler.
    """

    def __init__(self, app: web.Application) -> None:
        self._app = app
        self._routes: dict[str, list[_RegisteredRoute]] = {}  # app_name -> routes
        self._contexts: dict[str, AppContext] = {}  # app_name -> context
        # app_name -> parsed (method, path) pairs from the manifest's agentRoutes.
        # Installed in the same generation as the app's routes: assigned only once
        # the hook module loaded and its routes are in ``_routes``, popped on every
        # failure path and on deregistration, so a declaration never outlives the
        # route table it was declared against.
        self._agent_routes: dict[str, list[tuple[str, str]]] = {}
        self._catch_all_registered = False
        self._catch_all_route: web.AbstractRoute | None = None

    @property
    def http_app(self) -> web.Application:
        """The aiohttp Application this registry dispatches on.

        Read by the hooks wiring to fill ``AppContext.http_app``. Exposed as a
        property so that wiring does not reach into ``_app``: the registry is the
        one component the gateway already hands the Application to, so it is the
        honest place to ask, and a read-only property keeps it from being
        reassigned by a caller that only meant to look.
        """
        return self._app

    def ensure_catch_all(self) -> None:
        """Register the catch-all route on the aiohttp app (idempotent)."""
        if self._catch_all_registered:
            return
        self._catch_all_route = self._app.router.add_route(
            "*", "/api/apps/{app_name}/{path:.*}", self.dispatch
        )
        self._catch_all_registered = True
        logger.info("Route Registry catch-all registered")

    async def register_app_routes(
        self,
        app_name: str,
        app_dir: Path,
        hook_path: str,
        ctx: AppContext,
        agent_routes: Iterable[object] = (),
    ) -> list[str]:
        """Load route module and add routes to internal table.

        *agent_routes* are the manifest's ``agentRoutes`` entries. They are retained
        all or none: if any entry fails to parse, or there are more than
        ``MAX_AGENT_ROUTES_PER_APP``, the app gets NO agent routes and the refusal
        is logged once. Install-time validation already refuses both shapes, so this
        only bites a manifest that skipped it, and a partial list would hide which
        routes went missing. The declarations are installed only after the app's
        routes are, so they and the route table are one generation.

        Returns list of registered route descriptions (for logging).
        Sets health_status to degraded on failure.
        """
        self._agent_routes.pop(app_name, None)
        retained_agent_routes = self._parse_agent_routes(app_name, agent_routes)

        try:
            register_fn = load_app_module(app_name, app_dir, hook_path)
        except (ImportError, ValueError) as exc:
            logger.error(
                "Failed to load route module for app %s: %s", app_name, exc
            )
            ctx.health.mark_degraded(f"Route module load failed: {exc}")
            return []

        try:
            routes = register_fn(ctx)
        except Exception as exc:
            logger.error(
                "Route registration function failed for app %s: %s",
                app_name, exc, exc_info=True,
            )
            ctx.health.mark_degraded(f"Route registration failed: {exc}")
            return []

        if not isinstance(routes, list):
            logger.error(
                "App %s route function returned %s, expected list[AppRoute]",
                app_name, type(routes).__name__,
            )
            ctx.health.mark_degraded("Route function returned non-list")
            return []

        registered: list[_RegisteredRoute] = []
        descriptions: list[str] = []

        for route in routes:
            if not isinstance(route, AppRoute):
                logger.warning("App %s: skipping non-AppRoute item: %s", app_name, type(route))
                continue

            method = route.method.upper()
            path = route.path if route.path.startswith("/") else f"/{route.path}"

            rr = _RegisteredRoute(
                method=method,
                path=path,
                handler=route.handler,
                has_params=_has_params(path),
            )
            if rr.has_params:
                rr.compiled, rr.param_names = _compile_pattern(path)

            registered.append(rr)
            descriptions.append(f"{method} /api/apps/{app_name}{path}")

        self._routes[app_name] = registered
        self._contexts[app_name] = ctx
        self.ensure_catch_all()
        if retained_agent_routes:
            self._agent_routes[app_name] = retained_agent_routes

        logger.info(
            "Registered %d route(s) for app %s: %s",
            len(registered), app_name, descriptions,
        )
        return descriptions

    def _parse_agent_routes(
        self, app_name: str, agent_routes: Iterable[object]
    ) -> list[tuple[str, str]]:
        """Parse every declaration, or refuse the whole list and say so once."""
        declared = list(agent_routes)
        if len(declared) > MAX_AGENT_ROUTES_PER_APP:
            logger.warning(
                "App %s declares %d agent routes, more than the %d allowed; "
                "refusing all of its agent routes",
                app_name,
                len(declared),
                MAX_AGENT_ROUTES_PER_APP,
            )
            return []
        parsed_routes: list[tuple[str, str]] = []
        for entry in declared:
            parsed, reason = parse_agent_route(entry)
            if parsed is None:
                logger.warning(
                    "App %s agent route %r is malformed (%s); refusing all of its agent routes",
                    app_name,
                    entry,
                    reason,
                )
                return []
            parsed_routes.append(parsed)
        return parsed_routes

    def deregister_app_routes(self, app_name: str) -> None:
        """Remove all routes for an app from internal table + unload modules."""
        removed = self._routes.pop(app_name, None)
        self._contexts.pop(app_name, None)
        self._agent_routes.pop(app_name, None)
        unload_app_modules(app_name)
        if removed:
            logger.info("Deregistered %d route(s) for app %s", len(removed), app_name)

    def get_registered_apps(self) -> list[str]:
        """Return list of app names with registered routes."""
        return list(self._routes.keys())

    def _resolve_route(
        self, app_name: str, method: str, path: str
    ) -> tuple[_RegisteredRoute, dict[str, str]] | None:
        """Resolve with the exact-first precedence used for app route dispatch."""
        app_routes = self._routes.get(app_name, ())
        for route in app_routes:
            if route.method == method and route.path == path and not route.has_params:
                return route, {}
        for route in app_routes:
            if route.method != method or not route.has_params or route.compiled is None:
                continue
            match = route.compiled.match(path)
            if match is not None:
                return route, dict(zip(route.param_names or (), match.groups()))
        return None

    def agent_route_arm(self, resolved_route: object) -> bool:
        """Whether aiohttp selected this registry's catch-all for a request.

        The only question ``token_auth`` asks before arming the agent-route path.
        A host-owned route at the same path, a core app-lifecycle handler, or a
        request outside ``/api/apps/`` is not the catch-all, so it never arms;
        whether the resolved app route is DECLARED is answered once, in
        ``dispatch``, from the one resolution it performs.
        """
        return self._catch_all_route is not None and resolved_route is self._catch_all_route

    def _declares(self, app_name: str, route: _RegisteredRoute, path: str) -> bool:
        """Whether *app_name* declared the registered *route* for agents at *path*."""
        return (route.method, route.path) in self._agent_routes.get(
            app_name, ()
        ) and agent_route_matches(route.path, path)

    async def dispatch(self, request: web.Request) -> web.Response:
        """Catch-all handler that dispatches to registered app routes.

        Supports both exact paths and path parameters.
        Matching priority: exact match first, then pattern match.

        A request ``token_auth`` armed as an agent-route call (``internal_auth`` and
        ``app_agent_route`` both set) is admitted here and nowhere else: it must
        name its session in ``X-Session-Key`` and the route resolved for it must be
        one the app declared in ``agentRoutes``, or it is refused with 403 before any
        handler runs. The handler then reads the session as
        ``request["kirocrew_agent_session"]``. That key is set ONLY on that arm, so a
        cookie (browser) request never carries it, whatever headers it sends.

        The published session is attested only on the unix-socket transport, and
        only when ``_verify_unix_peer`` can resolve the peer's tenancy and pin the
        header to it. When that tenancy is unknown, and on TCP loopback, it is the
        caller's own claim, so an app must treat it as the session the call is FOR,
        not as proof of who made the call.
        """
        app_name = request.match_info.get("app_name", "")
        path = "/" + request.match_info.get("path", "")
        method = request.method
        # Who the dispatch rows name. An agent-route call is attributed to the
        # calling session, with the app it reached in the resources, so the trail
        # says which session drove which app route; every other call keeps the
        # app as the caller.
        audit_caller = f"app:{app_name}"
        audit_resources = f"{method} {path}"
        agent_arm = request.get("internal_auth") is True and request.get("app_agent_route") is True
        agent_session = ""

        if agent_arm:
            agent_session = request.headers.get("X-Session-Key", "").strip()
            if not agent_session:
                sel().log_api_access(
                    caller=f"app:{app_name}",
                    operation="app_route_dispatch",
                    outcome="denied",
                    resources=f"{method} {path}",
                    error="agent route call without X-Session-Key",
                )
                return web.json_response(
                    {
                        "error": "an agent route call must identify its session (X-Session-Key)",
                        "code": "agent_session_required",
                    },
                    status=403,
                )
            audit_caller = agent_session
            audit_resources = f"app:{app_name} {method} {path}"

        app_routes = self._routes.get(app_name)
        if not app_routes:
            sel().log_api_access(
                caller=audit_caller,
                operation="app_route_dispatch",
                outcome="not_found",
                resources=audit_resources,
            )
            return web.json_response({"error": "not found"}, status=404)

        ctx = self._contexts.get(app_name)
        if not ctx:
            return web.json_response({"error": "app context not found"}, status=500)

        resolved = self._resolve_route(app_name, method, path)
        if resolved is not None:
            route, path_params = resolved
            if agent_arm:
                if not self._declares(app_name, route, path):
                    sel().log_api_access(
                        caller=audit_caller,
                        operation="app_route_dispatch",
                        outcome="denied",
                        resources=audit_resources,
                        error="route is not a declared agent route",
                    )
                    return web.json_response(
                        {
                            "error": f"{method} {path} is not a declared agent route of {app_name}",
                            "code": "agent_route_not_declared",
                        },
                        status=403,
                    )
                request["kirocrew_agent_session"] = agent_session
            request.match_info.update(path_params)
            sel().log_api_access(
                caller=audit_caller,
                operation="app_route_dispatch",
                outcome="ok",
                resources=audit_resources,
            )
            return await route.handler(request, ctx)

        sel().log_api_access(
            caller=audit_caller,
            operation="app_route_dispatch",
            outcome="not_found",
            resources=audit_resources,
        )
        return web.json_response({"error": "not found"}, status=404)
