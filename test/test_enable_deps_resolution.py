"""Tests for dependency resolution during app enable.

Verifies that handle_enable_app() resolves dependencies.capabilities when a user
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
    """Verify dependencies.capabilities is resolved when an app is enabled."""

    @pytest.mark.asyncio
    async def test_dependencies_resolved_on_enable(self) -> None:
        """When the manifest declares capability deps, resolve_dependencies is called."""
        from kiro_crew.apps.dependencies import DependencyResult

        fake_app_info = {
            "name": "test-app",
            "manifest": {
                "dependencies": {
                    "capabilities": {"agents": ["TestCapabilityPkg"]},
                },
            },
            "resources": "gateway",
            "enabled": True,
            "origin": "builtin",
        }

        mock_dep_result = DependencyResult(installed=["capability/agents/TestCapabilityPkg"])

        with (
            patch("kiro_crew.apps.routes.get_app", return_value=fake_app_info),
            patch(
                "kiro_crew.apps.routes.enable_app",
                return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True}),
            ),
            patch("kiro_crew.apps.routes.register_app", return_value=MagicMock(to_dict=lambda: {})),
            patch("kiro_crew.apps.routes.start_app_backend", return_value=None),
            patch("kiro_crew.apps.routes.on_app_enable", new_callable=AsyncMock, return_value=None),
            patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
            patch(
                "kiro_crew.apps.routes._resolve_deps",
                new_callable=AsyncMock,
                return_value=mock_dep_result,
            ) as mock_resolve,
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
            assert body["dependencies"]["installed"] == ["capability/agents/TestCapabilityPkg"]

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
            patch(
                "kiro_crew.apps.routes.enable_app",
                return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True}),
            ),
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
                    "capabilities": {"agents": ["GoodPkg", "BadPkg"]},
                },
            },
            "resources": "gateway",
            "enabled": True,
        }

        mock_dep_result = DependencyResult(
            installed=["capability/agents/GoodPkg"],
            failed=["capability/agents/BadPkg"],
        )

        with (
            patch("kiro_crew.apps.routes.get_app", return_value=fake_app_info),
            patch(
                "kiro_crew.apps.routes.enable_app",
                return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True}),
            ),
            patch("kiro_crew.apps.routes.register_app", return_value=MagicMock(to_dict=lambda: {})),
            patch("kiro_crew.apps.routes.start_app_backend", return_value=None),
            patch("kiro_crew.apps.routes.on_app_enable", new_callable=AsyncMock, return_value=None),
            patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
            patch(
                "kiro_crew.apps.routes._resolve_deps",
                new_callable=AsyncMock,
                return_value=mock_dep_result,
            ),
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
            assert "capability/agents/BadPkg" in body["dependencies"]["failed"]
            assert "capability/agents/GoodPkg" in body["dependencies"]["installed"]

    @pytest.mark.asyncio
    async def test_deps_resolved_before_on_enable_script(self) -> None:
        """Dependencies are resolved BEFORE setup.onEnable runs."""
        call_order: list[str] = []

        from kiro_crew.apps.dependencies import DependencyResult

        fake_app_info = {
            "name": "ordered-app",
            "manifest": {
                "dependencies": {"capabilities": {"agents": ["SomePkg"]}},
                "setup": {"onEnable": "echo post-install"},
            },
            "resources": "gateway",
            "enabled": True,
        }

        async def mock_resolve(*args, **kwargs):
            call_order.append("resolve_deps")
            return DependencyResult(installed=["capability/agents/SomePkg"])

        async def mock_script(*args, **kwargs):
            call_order.append("on_enable_script")
            return {"output": "", "failed": False}

        with (
            patch("kiro_crew.apps.routes.get_app", return_value=fake_app_info),
            patch(
                "kiro_crew.apps.routes.enable_app",
                return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True}),
            ),
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


class TestClientInstallOnEnableIsAdvisory:
    """A ``platform.installMode: "client"`` app's onEnable must not gate the enable.

    Such an app's real payload is a desktop application distributed separately, so
    its script targets something that legitimately may not be on this host. The
    rollback is right for a server app (a failed script means a broken app) and
    wrong here: it made the dashboard half impossible to enable.
    """

    @staticmethod
    def _client_app(name: str = "crew-companion", os_list: list[str] | None = None) -> dict:
        return {
            "name": name,
            "manifest": {
                "setup": {"onEnable": 'open "$HOME/Applications/Crew Companion.app"'},
                "platform": {
                    "os": os_list if os_list is not None else ["macos"],
                    "installMode": "client",
                },
            },
            "resources": "app",
            "enabled": True,
            "origin": "builtin",
        }

    @pytest.mark.asyncio
    async def test_client_app_stays_enabled_when_desktop_app_missing(self) -> None:
        """The reported bug: `open` exits non-zero, and the app must STILL enable."""
        failed_script = {"output": "The file ... does not exist.", "failed": True}

        with (
            patch("kiro_crew.apps.routes.get_app", return_value=self._client_app()),
            patch(
                "kiro_crew.apps.routes.enable_app",
                return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True}),
            ),
            patch("kiro_crew.apps.routes.on_app_enable", new_callable=AsyncMock, return_value=None),
            patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
            patch(
                "kiro_crew.apps.routes._run_lifecycle_script",
                new_callable=AsyncMock,
                return_value=failed_script,
            ),
            patch("kiro_crew.apps.routes.disable_app") as mock_disable,
            patch("kiro_crew.apps.routes.sys") as mock_sys,
        ):
            mock_sys.platform = "darwin"
            from kiro_crew.apps.routes import handle_enable_app

            request = MagicMock()
            request.match_info = {"name": "crew-companion"}
            request.app = {"state": MagicMock()}

            response = await handle_enable_app(request)

        import json

        assert response.status == 200
        body = json.loads(response.body)
        assert body.get("ok") is True
        # The failure is surfaced, not swallowed — but it is advisory.
        assert body["onEnable"]["failed"] is True
        mock_disable.assert_not_called()

    @pytest.mark.asyncio
    async def test_client_app_script_skipped_on_unsupported_os(self) -> None:
        """A macOS-only client app enabled on Linux must not run its macOS command."""
        with (
            patch("kiro_crew.apps.routes.get_app", return_value=self._client_app()),
            patch(
                "kiro_crew.apps.routes.enable_app",
                return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True}),
            ),
            patch("kiro_crew.apps.routes.on_app_enable", new_callable=AsyncMock, return_value=None),
            patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
            patch(
                "kiro_crew.apps.routes._run_lifecycle_script", new_callable=AsyncMock
            ) as mock_script,
            patch("kiro_crew.apps.routes.disable_app") as mock_disable,
            patch("kiro_crew.apps.routes.sys") as mock_sys,
        ):
            mock_sys.platform = "linux"
            from kiro_crew.apps.routes import handle_enable_app

            request = MagicMock()
            request.match_info = {"name": "crew-companion"}
            request.app = {"state": MagicMock()}

            response = await handle_enable_app(request)

        import json

        assert response.status == 200
        assert json.loads(response.body)["onEnable"]["skipped"] == "unsupported_platform"
        mock_script.assert_not_called()
        mock_disable.assert_not_called()

    @pytest.mark.parametrize(
        "platform_value",
        [None, "client", 123, [], {"os": None}, {"arch": None}, {"os": 123}],
        ids=["null", "string", "int", "list", "os-null", "arch-null", "os-int"],
    )
    def test_a_malformed_platform_block_cannot_break_enable(self, platform_value) -> None:
        """The enable path is the FIRST place `platform` is parsed, so it must not raise.

        `PlatformConfig.from_dict` iterates `os`/`arch` directly, so `"os": null` in
        a hand-edited app.json raises TypeError there — which would surface as a 500
        on enable. An unreadable block reads as "not a client app", keeping the
        strict rollback rather than widening the advisory path.
        """
        from kiro_crew.apps.routes import _client_install_manifest

        assert _client_install_manifest({"platform": platform_value}) is None

    def test_a_well_formed_client_block_is_still_recognised(self) -> None:
        """The guard above must not swallow the case the feature depends on."""
        from kiro_crew.apps.routes import _client_install_manifest

        cfg = _client_install_manifest({"platform": {"os": ["macos"], "installMode": "client"}})
        assert cfg is not None
        assert cfg.installMode == "client"

    @pytest.mark.asyncio
    async def test_server_app_onenable_failure_still_rolls_back(self) -> None:
        """The guard on the fix: a normal app's rollback behaviour is UNCHANGED."""
        failed_script = {"output": "boom", "failed": True}
        server_app = {
            "name": "server-app",
            "manifest": {"setup": {"onEnable": "exit 1"}},
            "resources": "gateway",
            "enabled": True,
        }

        with (
            patch("kiro_crew.apps.routes.get_app", return_value=server_app),
            patch(
                "kiro_crew.apps.routes.enable_app",
                return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True}),
            ),
            patch("kiro_crew.apps.routes.register_app", return_value=MagicMock(to_dict=lambda: {})),
            patch("kiro_crew.apps.routes.start_app_backend", return_value=None),
            patch("kiro_crew.apps.routes.stop_app_backend"),
            patch("kiro_crew.apps.routes.deregister_app") as mock_dereg,
            patch("kiro_crew.apps.routes.on_app_enable", new_callable=AsyncMock, return_value=None),
            patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
            patch(
                "kiro_crew.apps.routes._run_lifecycle_script",
                new_callable=AsyncMock,
                return_value=failed_script,
            ),
            patch("kiro_crew.apps.routes.disable_app") as mock_disable,
        ):
            from kiro_crew.apps.routes import handle_enable_app

            request = MagicMock()
            request.match_info = {"name": "server-app"}
            request.app = {"state": MagicMock()}

            response = await handle_enable_app(request)

        import json

        assert response.status == 400
        assert json.loads(response.body)["code"] == "on_enable_failed"
        mock_disable.assert_called_once()
        mock_dereg.assert_called_once()
