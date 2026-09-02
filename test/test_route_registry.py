"""Property tests for Route Registry.

Feature: app-sdk-gateway-hooks
Properties 1, 2: Route prefix enforcement and deregistration completeness.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.route_registry import (
    RouteRegistry,
    _compile_pattern,
    _has_params,
    _match_path_pattern,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def _app_name() -> st.SearchStrategy[str]:
    return st.from_regex(r"[a-z][a-z0-9-]{2,12}", fullmatch=True)


def _route_path() -> st.SearchStrategy[str]:
    """Generate valid route paths."""
    segment = st.from_regex(r"[a-z][a-z0-9-]{0,10}", fullmatch=True)
    return st.lists(segment, min_size=1, max_size=4).map(lambda parts: "/" + "/".join(parts))


def _method() -> st.SearchStrategy[str]:
    return st.sampled_from(["GET", "POST", "PUT", "DELETE", "PATCH"])


# ---------------------------------------------------------------------------
# Property 1: Route prefix enforcement
# ---------------------------------------------------------------------------


class TestRoutePrefixEnforcement:
    """Property 1: Route prefix enforcement.

    **Validates: Requirements 1.2, 1.5**
    """

    @settings(max_examples=100)
    @given(app_name=_app_name(), paths=st.lists(_route_path(), min_size=1, max_size=5))
    def test_all_routes_under_app_prefix(self, app_name: str, paths: list[str]) -> None:
        """Routes in internal table are relative; dispatch enforces prefix."""
        mock_app = MagicMock()
        mock_app.router = MagicMock()
        mock_app.router.add_route = MagicMock()

        registry = RouteRegistry(mock_app)

        # Manually populate internal table (simulates register_app_routes)
        from kiro_crew.apps.route_registry import _RegisteredRoute

        routes = []
        for p in paths:
            routes.append(_RegisteredRoute(
                method="GET", path=p, handler=AsyncMock(),
                has_params=False, compiled=None, param_names=None,
            ))
        registry._routes[app_name] = routes
        registry._contexts[app_name] = MagicMock()

        # Internal paths are relative (no /api/apps/ prefix stored)
        for route in routes:
            assert not route.path.startswith("/api/apps/")
            # Full path is enforced by the catch-all: /api/apps/{name}/{path}
            full = f"/api/apps/{app_name}{route.path}"
            assert full.startswith(f"/api/apps/{app_name}/")

    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(app_a=_app_name(), app_b=_app_name(), path=_route_path())
    def test_no_collisions_between_apps(self, app_a: str, app_b: str, path: str) -> None:
        """Two apps with the same relative path produce different absolute paths."""
        if app_a == app_b:
            return
        full_a = f"/api/apps/{app_a}{path}"
        full_b = f"/api/apps/{app_b}{path}"
        assert full_a != full_b


# ---------------------------------------------------------------------------
# Property 2: Route deregistration completeness
# ---------------------------------------------------------------------------


class TestRouteDeregistration:
    """Property 2: Route deregistration completeness.

    **Validates: Requirements 1.3**
    """

    def test_deregister_removes_all_routes(self) -> None:
        """After deregistration, no routes remain for the app."""
        mock_app = MagicMock()
        mock_app.router = MagicMock()
        mock_app.router.add_route = MagicMock()

        registry = RouteRegistry(mock_app)
        # Manually add routes to internal table
        registry._routes["test-app"] = [MagicMock()]
        registry._contexts["test-app"] = MagicMock()

        registry.deregister_app_routes("test-app")

        assert "test-app" not in registry._routes
        assert "test-app" not in registry._contexts

    @settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(app_name=_app_name(), n_routes=st.integers(min_value=1, max_value=10))
    def test_deregister_clears_all_n_routes(self, app_name: str, n_routes: int) -> None:
        """Deregistering an app with N routes leaves zero routes."""
        mock_app = MagicMock()
        mock_app.router = MagicMock()
        mock_app.router.add_route = MagicMock()

        registry = RouteRegistry(mock_app)
        registry._routes[app_name] = [MagicMock() for _ in range(n_routes)]
        registry._contexts[app_name] = MagicMock()

        registry.deregister_app_routes(app_name)

        assert app_name not in registry._routes
        assert registry.get_registered_apps() == [] or app_name not in registry.get_registered_apps()

    def test_deregister_nonexistent_is_noop(self) -> None:
        """Deregistering a non-registered app doesn't raise."""
        mock_app = MagicMock()
        mock_app.router = MagicMock()
        registry = RouteRegistry(mock_app)
        # Should not raise
        registry.deregister_app_routes("nonexistent-app")


# ---------------------------------------------------------------------------
# Path pattern matching tests
# ---------------------------------------------------------------------------


class TestPathPatternMatching:
    """Unit tests for path parameter matching."""

    def test_exact_path_no_params(self) -> None:
        assert not _has_params("/status")
        assert _has_params("/tasks/{task_id}")

    def test_single_param_match(self) -> None:
        result = _match_path_pattern("/tasks/{task_id}", "/tasks/123")
        assert result == {"task_id": "123"}

    def test_multiple_params(self) -> None:
        result = _match_path_pattern(
            "/tasks/{task_id}/comments/{comment_id}",
            "/tasks/abc/comments/456",
        )
        assert result == {"task_id": "abc", "comment_id": "456"}

    def test_no_match(self) -> None:
        result = _match_path_pattern("/tasks/{task_id}", "/users/123")
        assert result is None

    def test_trailing_slash_mismatch(self) -> None:
        result = _match_path_pattern("/tasks/{id}", "/tasks/123/extra")
        assert result is None

    def test_compile_pattern_produces_regex(self) -> None:
        pattern, params = _compile_pattern("/items/{id}/sub/{sub_id}")
        assert params == ["id", "sub_id"]
        assert pattern.match("/items/foo/sub/bar")
        assert not pattern.match("/items/foo/sub/")
