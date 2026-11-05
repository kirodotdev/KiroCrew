"""Uninstall must stop an app's in-process code, at least as much as disable does.

An app's ``backend.hooks`` run inside the gateway: its ``routes`` are dispatched by
:class:`~kiro_crew.apps.route_registry.RouteRegistry`, and its ``on_shutdown`` is
invoked by the lifecycle dispatcher. Disable reaches both through
``teardown_app_runtime`` -> ``on_app_disable``. The uninstall handler runs its own
backend stop and deregistration, and these tests pin that it also runs the
in-process half, so an uninstalled app's routes stop answering when the uninstall
returns rather than at the hook reconciler's next tick.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from dashboard_owner_helpers import owner_claims

import kiro_crew.apps.hooks_integration as hooks_integration
import kiro_crew.apps.teardown as teardown
from kiro_crew.apps.route_registry import RouteRegistry

APP = "probe-routes"
ROUTES_HOOK = "app.hooks:register_routes"
SHUTDOWN_HOOK = "app.hooks:on_shutdown"


@dataclass
class _Route:
    """An installed app's own route type: structural, no ``kiro_crew`` import."""

    method: str
    path: str
    handler: Any


async def _serve_data(request: web.Request, ctx: Any) -> web.Response:
    return web.json_response({"data": "app-private"})


class _FakeDispatcher:
    """The lifecycle dispatcher surface ``on_app_disable`` and the handler read."""

    def __init__(self) -> None:
        self._cron_service = None
        self.invoked: list[tuple[str, str]] = []
        self.stop_detached_startup_hooks = AsyncMock(return_value=True)

    async def _prepare_context(self, app_info: dict[str, Any], *, phase: str) -> object:
        return object()

    async def _invoke(self, app_name: str, hook: str, ctx: object, *, phase: str) -> bool:
        self.invoked.append((app_name, hook))
        return True


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> RouteRegistry:
    """A real route registry with APP's route mounted, wired as the gateway's."""
    reg = RouteRegistry(web.Application())
    monkeypatch.setattr(hooks_integration, "_route_registry", reg)
    return reg


@pytest.fixture
def dispatcher(monkeypatch: pytest.MonkeyPatch) -> _FakeDispatcher:
    fake = _FakeDispatcher()
    monkeypatch.setattr(hooks_integration, "_lifecycle_dispatcher", fake)
    return fake


@pytest.fixture(autouse=True)
def _clean_loaded_record():
    hooks_integration.clear_loaded_hook_signature(APP)
    teardown.forget_app_hooks(APP)
    yield
    hooks_integration.clear_loaded_hook_signature(APP)
    teardown.forget_app_hooks(APP)


async def _mount(reg: RouteRegistry) -> None:
    with patch(
        "kiro_crew.apps.route_registry.load_app_module",
        return_value=lambda ctx: [_Route("GET", "/data", _serve_data)],
    ):
        await reg.register_app_routes(APP, MagicMock(), ROUTES_HOOK, MagicMock())
    assert APP in reg.get_registered_apps(), "fixture must actually mount the route"


async def _get_data(reg: RouteRegistry) -> int:
    request = make_mocked_request(
        "GET", f"/api/apps/{APP}/data", match_info={"app_name": APP, "path": "data"}
    )
    with patch("kiro_crew.apps.route_registry.sel", return_value=MagicMock()):
        response = await reg.dispatch(request)
    return response.status


def _retain_loaded_manifest(manifest: dict[str, Any]) -> None:
    """What a healthy enable records: a signature plus the manifest it loaded."""
    hooks_integration._loaded_hook_signatures[APP] = ("loaded",)
    hooks_integration._loaded_hook_manifests[APP] = manifest


async def _uninstall(manifest: dict[str, Any]) -> web.Response:
    """Drive handle_uninstall_app for APP with the file and process I/O stubbed."""
    fake_app = {
        "name": APP,
        "manifest": manifest,
        "resources": "gateway",
        "lifecycle": "gateway",
        "enabled": True,
    }
    request = MagicMock()
    request.match_info = {"name": APP}
    state = MagicMock()
    state.crons = None
    state.owner_id = ""
    request.app = {"state": state}
    owner_claims(request)
    request.json = AsyncMock(return_value={})

    with (
        patch("kiro_crew.apps.routes.get_app", return_value=fake_app),
        patch(
            "kiro_crew.apps.routes.uninstall_app",
            return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True}),
        ),
        patch("kiro_crew.apps.routes.stop_app_backend", return_value=None),
        patch("kiro_crew.apps.routes.deregister_app", return_value=None),
        patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
        patch(
            "kiro_crew.apps.routes.classify_and_clean_for_uninstall",
            return_value={"removable": [], "shared": [], "userInstalled": []},
        ),
        patch(
            "kiro_crew.apps.routes.clean_dependencies",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        from kiro_crew.apps.routes import handle_uninstall_app

        response = await handle_uninstall_app(request)
    assert response.status < 400, f"uninstall failed with {response.status}"
    return response


@pytest.mark.asyncio
class TestUninstallUnmountsInProcessRoutes:
    async def test_the_route_stops_answering_when_uninstall_returns(
        self, registry: RouteRegistry, dispatcher: _FakeDispatcher
    ):
        """A 200 from uninstall means the app's routes answer 404."""
        manifest = {"backend": {"hooks": {"routes": ROUTES_HOOK}}}
        await _mount(registry)
        _retain_loaded_manifest(manifest)
        assert await _get_data(registry) == 200

        await _uninstall(manifest)

        assert APP not in registry.get_registered_apps()
        assert await _get_data(registry) == 404
        # Cleared here, so the reconciler sees nothing left to tear down.
        assert hooks_integration.loaded_hook_signature(APP) is None

    async def test_on_shutdown_runs_from_the_loaded_manifest(
        self, registry: RouteRegistry, dispatcher: _FakeDispatcher
    ):
        """The app gets the same clean shutdown a disable gives it."""
        loaded = {"backend": {"hooks": {"routes": ROUTES_HOOK, "on_shutdown": SHUTDOWN_HOOK}}}
        await _mount(registry)
        _retain_loaded_manifest(loaded)

        # The on-disk manifest names no shutdown hook: the LOADED one decides.
        await _uninstall({"backend": {"hooks": {"routes": ROUTES_HOOK}}})

        assert dispatcher.invoked == [(APP, SHUTDOWN_HOOK)]

    async def test_an_app_never_loaded_gets_no_shutdown_code_run(
        self, registry: RouteRegistry, dispatcher: _FakeDispatcher
    ):
        """Removal must not be what launches an unstarted app's code."""
        manifest = {"backend": {"hooks": {"routes": ROUTES_HOOK, "on_shutdown": SHUTDOWN_HOOK}}}

        await _uninstall(manifest)

        assert dispatcher.invoked == []

    async def test_the_app_disable_hook_is_told_before_its_files_go(
        self, registry: RouteRegistry, dispatcher: _FakeDispatcher
    ):
        """Workers holding time-bounded authority stand down inside the request."""
        told: list[str] = []

        async def _disable(app: str) -> None:
            told.append(app)

        teardown.register_app_disable_hook(APP, _disable)

        await _uninstall({})

        assert told == [APP]


@pytest.mark.asyncio
class TestUninstallReportsResidualExecution:
    async def test_incomplete_job_cleanup_reaches_both_outputs(self):
        """A worker still running after uninstall must not read as a clean removal."""
        with patch(
            "kiro_crew.apps.teardown.on_app_disable",
            new_callable=AsyncMock,
            return_value={"job_cleanup": "partial: removed 1; 1 worker(s) still running"},
        ):
            response = await _uninstall({})

        body = json.loads(response.body)
        note = "job cleanup incomplete: partial: removed 1; 1 worker(s) still running"
        assert note in body["warnings"]
        assert note in body["uninstall_log"]

    async def test_a_raising_teardown_is_reported_and_the_uninstall_completes(self):
        with patch(
            "kiro_crew.apps.teardown.on_app_disable",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            response = await _uninstall({})

        body = json.loads(response.body)
        assert body["ok"] is True
        assert "hooks disable failed: boom" in body["warnings"]

    async def test_a_clean_teardown_adds_no_warning(self):
        with patch(
            "kiro_crew.apps.teardown.on_app_disable",
            new_callable=AsyncMock,
            return_value={"job_cleanup": "removed 2 run record(s)"},
        ):
            response = await _uninstall({})

        assert "warnings" not in json.loads(response.body)
