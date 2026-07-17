"""Tests for dependency resolution during app enable.

Verifies that handle_enable_app() resolves dependencies.aim when a user
enables an app (builtin or otherwise).
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_dashboard_server():
    """Pre-mock dashboard.server to avoid circular import with mimir."""
    if "kiro_crew.dashboard.server" not in sys.modules:
        sys.modules["kiro_crew.dashboard.server"] = MagicMock()
    yield


class TestEnableDepsResolution:
    """Verify dependencies.aim is resolved when an app is enabled."""

    @pytest.mark.asyncio
    async def test_dependencies_resolved_on_enable(self) -> None:
        """When manifest has dependencies.aim, resolve_dependencies is called."""
        from kiro_crew.apps.dependencies import DependencyResult

        fake_app_info = {
            "name": "test-app",
            "manifest": {
                "dependencies": {
                    "aim": ["TestAICapabilities"],
                },
            },
            "resources": "gateway",
            "enabled": True,
            "origin": "builtin",
        }

        mock_dep_result = DependencyResult(installed=["aim/agents/TestAICapabilities"])

        with (
            patch("kiro_crew.apps.routes.get_app", return_value=fake_app_info),
            patch("kiro_crew.apps.routes.enable_app", return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True})),
            patch("kiro_crew.apps.routes.register_app", return_value=MagicMock(to_dict=lambda: {})),
            patch("kiro_crew.apps.routes.start_app_backend", return_value=None),
            patch("kiro_crew.apps.routes.on_app_enable", new_callable=AsyncMock, return_value=None),
            patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
            patch("kiro_crew.apps.routes._resolve_deps", new_callable=AsyncMock, return_value=mock_dep_result) as mock_resolve,
        ):
            from kiro_crew.apps.routes import handle_enable_app

            request = MagicMock()
            request.match_info = {"name": "test-app"}
            request.app = {"state": MagicMock()}

            response = await handle_enable_app(request)

            # Verify resolve_dependencies was called
            mock_resolve.assert_called_once()
            call_args = mock_resolve.call_args
            assert call_args[0][0] == "test-app"

            # Verify response includes dependency info
            import json
            body = json.loads(response.body)
            assert "dependencies" in body
            assert body["dependencies"]["installed"] == ["aim/agents/TestAICapabilities"]

    @pytest.mark.asyncio
    async def test_no_dependencies_skips_resolution(self) -> None:
        """When manifest has no dependencies, resolve_dependencies is NOT called."""
        fake_app_info = {
            "name": "simple-app",
            "manifest": {},
            "resources": "gateway",
            "enabled": True,
        }

        with (
            patch("kiro_crew.apps.routes.get_app", return_value=fake_app_info),
            patch("kiro_crew.apps.routes.enable_app", return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True})),
            patch("kiro_crew.apps.routes.register_app", return_value=MagicMock(to_dict=lambda: {})),
            patch("kiro_crew.apps.routes.start_app_backend", return_value=None),
            patch("kiro_crew.apps.routes.on_app_enable", new_callable=AsyncMock, return_value=None),
            patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
            patch("kiro_crew.apps.routes._resolve_deps", new_callable=AsyncMock) as mock_resolve,
        ):
            from kiro_crew.apps.routes import handle_enable_app

            request = MagicMock()
            request.match_info = {"name": "simple-app"}
            request.app = {"state": MagicMock()}

            await handle_enable_app(request)

            mock_resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_deps_reported_but_enable_continues(self) -> None:
        """Failed dependency resolution is reported but doesn't block enable."""
        from kiro_crew.apps.dependencies import DependencyResult

        fake_app_info = {
            "name": "partial-app",
            "manifest": {
                "dependencies": {
                    "aim": ["GoodPkg", "BadPkg"],
                },
            },
            "resources": "gateway",
            "enabled": True,
        }

        mock_dep_result = DependencyResult(
            installed=["aim/agents/GoodPkg"],
            failed=["aim/agents/BadPkg"],
        )

        with (
            patch("kiro_crew.apps.routes.get_app", return_value=fake_app_info),
            patch("kiro_crew.apps.routes.enable_app", return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True})),
            patch("kiro_crew.apps.routes.register_app", return_value=MagicMock(to_dict=lambda: {})),
            patch("kiro_crew.apps.routes.start_app_backend", return_value=None),
            patch("kiro_crew.apps.routes.on_app_enable", new_callable=AsyncMock, return_value=None),
            patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
            patch("kiro_crew.apps.routes._resolve_deps", new_callable=AsyncMock, return_value=mock_dep_result),
        ):
            from kiro_crew.apps.routes import handle_enable_app

            request = MagicMock()
            request.match_info = {"name": "partial-app"}
            request.app = {"state": MagicMock()}

            response = await handle_enable_app(request)

            import json
            body = json.loads(response.body)
            # Enable still succeeds
            assert body["ok"] is True
            # But reports the failure
            assert "dependencies" in body
            assert "aim/agents/BadPkg" in body["dependencies"]["failed"]
            assert "aim/agents/GoodPkg" in body["dependencies"]["installed"]

    @pytest.mark.asyncio
    async def test_deps_resolved_before_on_enable_script(self) -> None:
        """Dependencies are resolved BEFORE setup.onEnable runs."""
        call_order: list[str] = []

        from kiro_crew.apps.dependencies import DependencyResult

        fake_app_info = {
            "name": "ordered-app",
            "manifest": {
                "dependencies": {"aim": ["SomePkg"]},
                "setup": {"onEnable": "echo post-install"},
            },
            "resources": "gateway",
            "enabled": True,
        }

        async def mock_resolve(*args, **kwargs):
            call_order.append("resolve_deps")
            return DependencyResult(installed=["aim/agents/SomePkg"])

        async def mock_script(*args, **kwargs):
            call_order.append("on_enable_script")
            return {"output": "", "failed": False}

        with (
            patch("kiro_crew.apps.routes.get_app", return_value=fake_app_info),
            patch("kiro_crew.apps.routes.enable_app", return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True})),
            patch("kiro_crew.apps.routes.register_app", return_value=MagicMock(to_dict=lambda: {})),
            patch("kiro_crew.apps.routes.start_app_backend", return_value=None),
            patch("kiro_crew.apps.routes._run_lifecycle_script", side_effect=mock_script),
            patch("kiro_crew.apps.routes.on_app_enable", new_callable=AsyncMock, return_value=None),
            patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
            patch("kiro_crew.apps.routes._resolve_deps", side_effect=mock_resolve),
        ):
            from kiro_crew.apps.routes import handle_enable_app

            request = MagicMock()
            request.match_info = {"name": "ordered-app"}
            request.app = {"state": MagicMock()}

            await handle_enable_app(request)

        assert call_order == ["resolve_deps", "on_enable_script"]
