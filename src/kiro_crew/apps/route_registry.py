"""Route Registry — middleware-based soft routing for app-provided HTTP handlers.

Uses an internal routing table instead of aiohttp's UrlDispatcher to support
dynamic enable/disable without gateway restart. A single catch-all route
dispatches to the internal table.

Supports both exact paths and path parameters (e.g. /tasks/{task_id}/comments).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web

from kiro_crew.apps.context import AppContext
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
        self._catch_all_registered = False

    def ensure_catch_all(self) -> None:
        """Register the catch-all route on the aiohttp app (idempotent)."""
        if self._catch_all_registered:
            return
        self._app.router.add_route(
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
    ) -> list[str]:
        """Load route module and add routes to internal table.

        Returns list of registered route descriptions (for logging).
        Sets health_status to degraded on failure.
        """
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

        logger.info(
            "Registered %d route(s) for app %s: %s",
            len(registered), app_name, descriptions,
        )
        return descriptions

    def deregister_app_routes(self, app_name: str) -> None:
        """Remove all routes for an app from internal table + unload modules."""
        removed = self._routes.pop(app_name, None)
        self._contexts.pop(app_name, None)
        unload_app_modules(app_name)
        if removed:
            logger.info("Deregistered %d route(s) for app %s", len(removed), app_name)

    def get_registered_apps(self) -> list[str]:
        """Return list of app names with registered routes."""
        return list(self._routes.keys())

    async def dispatch(self, request: web.Request) -> web.Response:
        """Catch-all handler that dispatches to registered app routes.

        Supports both exact paths and path parameters.
        Matching priority: exact match first, then pattern match.
        """
        app_name = request.match_info.get("app_name", "")
        path = "/" + request.match_info.get("path", "")
        method = request.method

        app_routes = self._routes.get(app_name)
        if not app_routes:
            sel().log_api_access(
                caller=f"app:{app_name}",
                operation="app_route_dispatch",
                outcome="not_found",
                resources=f"{method} {path}",
            )
            return web.json_response({"error": "not found"}, status=404)

        ctx = self._contexts.get(app_name)
        if not ctx:
            return web.json_response({"error": "app context not found"}, status=500)

        # Try exact match first (faster for most routes)
        for route in app_routes:
            if route.method == method and route.path == path and not route.has_params:
                sel().log_api_access(
                    caller=f"app:{app_name}",
                    operation="app_route_dispatch",
                    outcome="ok",
                    resources=f"{method} {path}",
                )
                return await route.handler(request, ctx)

        # Try pattern match (path params like {task_id})
        for route in app_routes:
            if route.method != method or not route.has_params:
                continue
            if route.compiled is None:
                continue
            m = route.compiled.match(path)
            if m and route.param_names:
                # Inject matched path params into match_info
                for name, value in zip(route.param_names, m.groups()):
                    request.match_info[name] = value
                sel().log_api_access(
                    caller=f"app:{app_name}",
                    operation="app_route_dispatch",
                    outcome="ok",
                    resources=f"{method} {path}",
                )
                return await route.handler(request, ctx)

        sel().log_api_access(
            caller=f"app:{app_name}",
            operation="app_route_dispatch",
            outcome="not_found",
            resources=f"{method} {path}",
        )
        return web.json_response({"error": "not found"}, status=404)
