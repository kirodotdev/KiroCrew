"""Coverage tests for kiro_crew.apps.routes — validation, denial and error paths.

Complements ``test_app_routes.py`` (happy-path lifecycle) by exercising the
branches a normal install/enable/uninstall never reaches: input validation,
permission/governance refusals, structured error codes, the registry-install
and SSE-stream endpoints, the git-blob proxy's SSRF gate, and the app-backend
reverse proxy's authorization gates.

Everything runs in-process against ``aiohttp``'s ``TestServer`` with
``KIROCREW_HOME`` pointed at ``tmp_path``. No git, no network egress, no
subprocesses: the few handlers that genuinely shell out are reached only on
branches that return before the spawn, and the paths that cannot avoid it
(``_run_lifecycle_script``, ``_fetch_git_blob``, the real ``openCommand``
launch) are deliberately left to the integration suites.
"""

from __future__ import annotations

import asyncio
import json
import platform as platform_mod
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    AppResult,
    enable_app,
    install_app,
    register_external_app,
)
from kiro_crew.apps.routes import (
    _client_install_manifest,
    _get_app_secret,
    _is_safe_repo_identifier,
    _notify_builtin_service,
    _resolve_app_backend_url,
    _sync_builtin_config,
    _unregister_notification_channels,
    invalidate_app_secret_cache,
    register_app_routes,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

APP = "cov-test-app"


def _make_app_source(
    tmp_path: Path, name: str = APP, **manifest_extra: Any
) -> Path:
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    manifest: dict[str, Any] = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Coverage Test App",
        "description": "App for routes coverage testing",
        "author": "tester",
    }
    manifest.update(manifest_extra)
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    return src


def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate KIROCREW_HOME and neutralize out-of-process side effects."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    # These synthetic apps are third-party; admit them explicitly so the
    # execution guard is not the thing under test in every case.
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    import kiro_crew.apps.bridges as bridges_mod

    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    import kiro_crew.apps.backend as bmod

    bmod._processes.clear()
    bmod._allocated_ports.clear()
    monkeypatch.setattr(routes_mod, "sel", lambda: MagicMock())
    invalidate_app_secret_cache(APP)
    return home


def _make_app(*, app_identity: str | None = None) -> web.Application:
    """An aiohttp app with the routes registered.

    ``app_identity`` stands in for ``token_auth_middleware`` having
    authenticated an APP token, which is what the proxy's cross-app guard
    reads off ``request["app"]``.
    """
    middlewares = []
    if app_identity is not None:

        @web.middleware
        async def _identity(
            request: web.Request, handler: Any
        ) -> web.StreamResponse:
            request["app"] = app_identity
            return await handler(request)

        middlewares.append(_identity)
    app = web.Application(middlewares=middlewares)
    register_app_routes(app)
    return app


def _install(tmp_path: Path, **manifest_extra: Any) -> None:
    install_app(_make_app_source(tmp_path, **manifest_extra))


async def _no_op_executor(*args: Any, **kwargs: Any) -> None:
    return None


def _sse_events(text: str) -> list[tuple[str, str]]:
    """Parse an SSE body into ``(event, data)`` pairs."""
    events: list[tuple[str, str]] = []
    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        name = ""
        data: list[str] = []
        for line in frame.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data.append(line[len("data: ") :])
        events.append((name, "\n".join(data)))
    return events


# ---------------------------------------------------------------------------
# Builtin-service helpers (_sync_builtin_config / _notify_builtin_service)
# ---------------------------------------------------------------------------


class TestBuiltinServiceHelpers:
    def test_sync_is_noop_for_non_service_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_env(tmp_path, monkeypatch)
        before = (home / "config.json").read_text(encoding="utf-8")
        _sync_builtin_config("not-a-service-app", enabled=True)
        assert (home / "config.json").read_text(encoding="utf-8") == before

    def test_sync_writes_enabled_flag_for_service_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_env(tmp_path, monkeypatch)
        monkeypatch.setitem(
            routes_mod._BUILTIN_SERVICE_APPS, APP, ("covsvc", "restart_covsvc")
        )
        _sync_builtin_config(APP, enabled=True)
        data = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert data["covsvc"]["enabled"] is True
        # The pre-existing section is preserved, not clobbered.
        assert data["agent"]["apps_allow_third_party"] is True

        _sync_builtin_config(APP, enabled=False)
        data = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert data["covsvc"]["enabled"] is False

    def test_sync_raises_oserror_on_malformed_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_env(tmp_path, monkeypatch)
        (home / "config.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setitem(
            routes_mod._BUILTIN_SERVICE_APPS, APP, ("covsvc", "restart_covsvc")
        )
        with pytest.raises(OSError, match="Could not read config.json"):
            _sync_builtin_config(APP, enabled=True)

    @pytest.mark.asyncio
    async def test_notify_returns_none_for_non_service_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        request = make_mocked_request("POST", "/x", app=web.Application())
        assert await _notify_builtin_service(request, "not-a-service-app") is None

    @pytest.mark.asyncio
    async def test_notify_warns_when_no_gateway_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setitem(
            routes_mod._BUILTIN_SERVICE_APPS, APP, ("covsvc", "restart_covsvc")
        )
        request = make_mocked_request("POST", "/x", app=web.Application())
        warn = await _notify_builtin_service(request, APP)
        assert warn is not None and "no gateway state" in warn

    @pytest.mark.asyncio
    async def test_notify_warns_when_no_restart_callback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setitem(
            routes_mod._BUILTIN_SERVICE_APPS, APP, ("covsvc", "restart_covsvc")
        )
        app = web.Application()
        app["state"] = SimpleNamespace()
        request = make_mocked_request("POST", "/x", app=app)
        warn = await _notify_builtin_service(request, APP)
        assert warn is not None and "no restart callback" in warn

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "result,expected",
        [
            ("ok", None),
            ("init returned without service", None),
            ("degraded", "restart returned: degraded"),
        ],
    )
    async def test_notify_maps_restart_result(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        result: str,
        expected: str | None,
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setitem(
            routes_mod._BUILTIN_SERVICE_APPS, APP, ("covsvc", "restart_covsvc")
        )

        async def _restart() -> str:
            return result

        app = web.Application()
        app["state"] = SimpleNamespace(restart_covsvc=_restart)
        request = make_mocked_request("POST", "/x", app=app)
        assert await _notify_builtin_service(request, APP) == expected

    @pytest.mark.asyncio
    async def test_notify_reports_restart_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setitem(
            routes_mod._BUILTIN_SERVICE_APPS, APP, ("covsvc", "restart_covsvc")
        )

        async def _restart() -> str:
            raise RuntimeError("service exploded")

        app = web.Application()
        app["state"] = SimpleNamespace(restart_covsvc=_restart)
        request = make_mocked_request("POST", "/x", app=app)
        warn = await _notify_builtin_service(request, APP)
        assert warn is not None and "restart failed" in warn


class TestUnregisterNotificationChannels:
    def test_noop_without_state_or_bus(self) -> None:
        # No state at all, and state without a bus, must both be silent.
        _unregister_notification_channels(
            make_mocked_request("POST", "/x", app=web.Application()), APP
        )
        app = web.Application()
        app["state"] = SimpleNamespace()
        _unregister_notification_channels(
            make_mocked_request("POST", "/x", app=app), APP
        )

    def test_unregisters_via_bus(self) -> None:
        bus = MagicMock()
        bus.unregister_app_channels.return_value = 2
        app = web.Application()
        app["state"] = SimpleNamespace(notification_bus=bus)
        _unregister_notification_channels(
            make_mocked_request("POST", "/x", app=app), APP
        )
        bus.unregister_app_channels.assert_called_once_with(APP)


# ---------------------------------------------------------------------------
# GET /api/apps — backend status enrichment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_apps_enriches_running_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    monkeypatch.setattr(
        routes_mod,
        "list_app_processes",
        lambda: [
            {"app_name": APP, "port": 7999, "healthy": True, "pid": 4242},
            {"app_name": "other-app", "port": 7998, "healthy": False, "pid": 1},
        ],
    )
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps")
        assert resp.status == 200
        rows = await resp.json()
    entry = next(a for a in rows if a["name"] == APP)
    assert entry["backend_status"] == {
        "running": True,
        "port": 7999,
        "healthy": True,
        "pid": 4242,
    }


# ---------------------------------------------------------------------------
# Migrated deploy-web compatibility redirects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,location",
    [
        ("/api/apps/deploy-web", "/api/deploy/list"),
        ("/api/apps/deploy-web/manifest", "/api/deploy/config"),
        ("/api/apps/deploy-web/config", "/api/deploy/config"),
    ],
)
async def test_deploy_web_redirects_to_canonical_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, location: str
) -> None:
    _setup_env(tmp_path, monkeypatch)
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(path, allow_redirects=False)
        assert resp.status == 307
        assert resp.headers["Location"] == location


# ---------------------------------------------------------------------------
# POST /api/apps/install — validation
# ---------------------------------------------------------------------------


class TestInstallValidation:
    @pytest.mark.asyncio
    async def test_invalid_json_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/install",
                data="not-json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid JSON"

    @pytest.mark.asyncio
    async def test_min_version_gate_rejects_before_copying(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_env(tmp_path, monkeypatch)
        src = _make_app_source(tmp_path, minKiroCrewVersion="999.0.0")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/apps/install", json={"source": str(src)})
            assert resp.status == 400
            assert "999.0.0" in (await resp.json())["error"]
        assert not (home / "apps" / APP).exists()

    @pytest.mark.asyncio
    async def test_unreadable_manifest_falls_back_to_path_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A corrupt app.json must not 500 in the pre-flight version check —
        # it falls through to install_app, which rejects it as a bad manifest.
        _setup_env(tmp_path, monkeypatch)
        src = tmp_path / "source" / APP
        src.mkdir(parents=True)
        (src / APP_MANIFEST_FILENAME).write_text("{ corrupt", encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/apps/install", json={"source": str(src)})
            assert resp.status == 400
            assert (await resp.json())["ok"] is False

    @pytest.mark.asyncio
    async def test_install_failure_is_reported_as_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/install", json={"source": str(tmp_path / "nope")}
            )
            assert resp.status == 400


# ---------------------------------------------------------------------------
# POST /api/apps/register — self-managed app registration
# ---------------------------------------------------------------------------


class TestRegisterExternal:
    @pytest.mark.asyncio
    async def test_invalid_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/register",
                data="{",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {"version": "1.0.0", "displayName": "X"},
            {"name": "ext-app", "displayName": "X"},
            {"name": "ext-app", "version": "1.0.0"},
        ],
    )
    async def test_required_fields(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        body: dict[str, str],
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/apps/register", json=body)
            assert resp.status == 400
            assert "required" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_success_returns_secret(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/register",
                json={
                    "name": "ext-app",
                    "version": "1.0.0",
                    "displayName": "External App",
                    "lifecycle": "app",
                    "resources": "app",
                },
            )
            assert resp.status == 201
            data = await resp.json()
        assert data["ok"] is True
        assert data["secret"]

    @pytest.mark.asyncio
    async def test_rejected_name_is_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/register",
                json={
                    "name": "Not Kebab Case",
                    "version": "1.0.0",
                    "displayName": "X",
                },
            )
            assert resp.status == 400
            assert (await resp.json())["ok"] is False


# ---------------------------------------------------------------------------
# POST /api/apps/{name}/update
# ---------------------------------------------------------------------------


class TestUpdateApp:
    @pytest.mark.asyncio
    async def test_not_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/apps/ghost/update")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_self_managed_lifecycle_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        register_external_app(
            "ext-app", "1.0.0", "External App", lifecycle="app", resources="app"
        )
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/apps/ext-app/update")
            assert resp.status == 400
            assert "lifecycle='app'" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_missing_source_is_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        register_external_app(
            "ext-app", "1.0.0", "External App", lifecycle="gateway", resources="app"
        )
        async with TestClient(TestServer(_make_app())) as client:
            # Body is not JSON at all, so the source also cannot come from there.
            resp = await client.post(
                "/api/apps/ext-app/update",
                data="{",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert "source path required" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_registry_update_failure_keeps_resources(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        deregistered: list[str] = []

        async def _failed_install(name: str, **kwargs: Any) -> dict[str, Any]:
            return {"ok": False, "name": name, "error": "clone failed"}

        monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
        monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
        monkeypatch.setattr(routes_mod, "install_from_registry", _failed_install)
        monkeypatch.setattr(
            routes_mod, "deregister_app", lambda n: deregistered.append(n)
        )

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 400
            assert (await resp.json())["error"] == "clone failed"
        # Nothing was torn down, so the app is still usable.
        assert deregistered == []

    @pytest.mark.asyncio
    async def test_registry_update_success_reregisters(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        calls: list[str] = []

        async def _ok_install(name: str, **kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "name": name}

        monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
        monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
        monkeypatch.setattr(routes_mod, "install_from_registry", _ok_install)
        monkeypatch.setattr(
            routes_mod, "deregister_app", lambda n: calls.append("deregister")
        )
        monkeypatch.setattr(
            routes_mod, "stop_app_backend", lambda n: calls.append("stop")
        )
        monkeypatch.setattr(
            routes_mod, "start_app_backend", lambda n: calls.append("start")
        )

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 200
            data = await resp.json()
        assert data["ok"] is True
        assert "registration" in data
        # Old resources are only swapped out AFTER the re-install succeeded.
        assert calls == ["deregister", "stop", "start"]

    @pytest.mark.asyncio
    async def test_local_update_failure_restores_registration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        registered: list[str] = []

        monkeypatch.setattr(
            routes_mod,
            "update_app",
            lambda source, expected_name=None: AppResult(
                ok=False, name=APP, error="source manifest mismatch"
            ),
        )
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: None)
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)

        # A `lambda n: registered.append(n) or ...` reads fine but does not type
        # check: list.append returns None, so mypy rejects using it as a value.
        def _register(name: str) -> SimpleNamespace:
            registered.append(name)
            return SimpleNamespace(to_dict=dict)

        monkeypatch.setattr(routes_mod, "register_app", _register)

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 400
            assert (await resp.json())["error"] == "source manifest mismatch"
        # The rollback re-registered what the failed update had torn down.
        assert registered == [APP]

    @pytest.mark.asyncio
    async def test_local_update_success_returns_registration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        monkeypatch.setattr(
            routes_mod,
            "update_app",
            lambda source, expected_name=None: AppResult(
                ok=True, name=APP, message="updated"
            ),
        )
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: None)
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 200
            data = await resp.json()
        assert data["ok"] is True
        assert "registration" in data


# ---------------------------------------------------------------------------
# Uninstall preview + uninstall refusals
# ---------------------------------------------------------------------------


class TestUninstallPreview:
    """``handle_uninstall_preview`` is not on the router (no
    ``add_get('/api/apps/{name}/uninstall/preview')`` in
    ``register_app_routes``), so it is exercised as a handler with a mocked
    request rather than over HTTP.
    """

    @staticmethod
    async def _preview(name: str) -> tuple[int, dict[str, Any]]:
        request = make_mocked_request(
            "GET",
            f"/api/apps/{name}/uninstall/preview",
            match_info={"name": name},
            app=web.Application(),
        )
        resp = await routes_mod.handle_uninstall_preview(request)
        # Response.body is `bytes | Payload | None`; only the bytes case is
        # JSON-decodable, so narrow explicitly rather than feeding mypy a union.
        raw = resp.body if isinstance(resp.body, bytes) else b"{}"
        return resp.status, json.loads(raw or b"{}")

    @pytest.mark.asyncio
    async def test_not_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        status, _ = await self._preview("ghost")
        assert status == 404

    @pytest.mark.asyncio
    async def test_locked_lifecycle_cannot_be_previewed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        register_external_app(
            "locked-app", "1.0.0", "Locked App", lifecycle="locked", resources="app"
        )
        status, body = await self._preview("locked-app")
        assert status == 400
        assert "lifecycle=locked" in body["error"]

    @pytest.mark.asyncio
    async def test_preview_lists_resources_and_dependencies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        register_external_app(
            "prev-app",
            "1.0.0",
            "Preview App",
            lifecycle="gateway",
            resources="app",
            manifest_data={
                "name": "prev-app",
                "version": "1.0.0",
                "displayName": "Preview App",
                "agents": ["a1"],
                "skills": ["s1"],
                "crons": [{"name": "c1"}],
            },
        )
        status, data = await self._preview("prev-app")
        assert status == 200
        assert data["app"] == "prev-app"
        assert data["resources"] == {
            "agents": ["a1"],
            "skills": ["s1"],
            "crons": ["c1"],
        }
        assert "dependencies" in data


class TestUninstallRefusals:
    @pytest.mark.asyncio
    async def test_not_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/apps/ghost/uninstall")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_locked_lifecycle_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        register_external_app(
            "locked-app", "1.0.0", "Locked App", lifecycle="locked", resources="app"
        )
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/apps/locked-app/uninstall")
            assert resp.status == 400
            assert "lifecycle=locked" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_removable_dependencies_are_cleaned_and_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, dependencies={"python": ["somepkg"]})

        async def _clean(name: str, removable: list[dict[str, Any]]) -> list[str]:
            return [d["id"] for d in removable]

        monkeypatch.setattr(
            routes_mod,
            "classify_and_clean_for_uninstall",
            lambda name, declared, keep_specific=(): {
                "removable": [{"id": "python:somepkg"}, {"id": "python:keepme"}]
            },
        )
        monkeypatch.setattr(routes_mod, "clean_dependencies", _clean)
        monkeypatch.setattr(routes_mod, "canonical_dep_key", lambda k: k)

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                f"/api/apps/{APP}/uninstall",
                json={"keep_specific": ["python:keepme", "", 7]},
            )
            assert resp.status == 200
            data = await resp.json()
        # The sanitized keep list survives the parse boundary and is honored.
        assert data["cleaned_dependencies"] == ["python:somepkg"]
        assert "1 dependency" in data["uninstall_log"]

    @pytest.mark.asyncio
    async def test_keep_dependencies_skips_cleanup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, dependencies={"python": ["somepkg"]})
        called = {"n": 0}

        def _classify(*args: Any, **kwargs: Any) -> dict[str, Any]:
            called["n"] += 1
            return {"removable": []}

        monkeypatch.setattr(
            routes_mod, "classify_and_clean_for_uninstall", _classify
        )
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                f"/api/apps/{APP}/uninstall", json={"keep_dependencies": True}
            )
            assert resp.status == 200
        assert called["n"] == 0

    @pytest.mark.asyncio
    async def test_on_uninstall_output_is_redacted_and_failure_noted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, setup={"onUninstall": "teardown.sh"})
        seen_env: dict[str, Any] = {}

        async def _script(
            app_name: str,
            script: str,
            *,
            timeout: int = 30,
            extra_env: dict[str, str] | None = None,
            action: str = "lifecycle_script",
        ) -> dict[str, Any]:
            seen_env.update(extra_env or {})
            return {
                "output": "wiped with token=ghp_0123456789abcdefghijABCDEFGHIJ0123456789",
                "failed": True,
            }

        monkeypatch.setattr(routes_mod, "_run_lifecycle_script", _script)
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/uninstall", json={})
            assert resp.status == 200
            log = (await resp.json())["uninstall_log"]
        assert "ghp_0123456789abcdefghijABCDEFGHIJ0123456789" not in log
        assert "onUninstall script failed" in log
        # The script is told which data disposition was chosen.
        assert seen_env == {"KEEP_DATA": "1", "PURGE_DATA": "0"}

    @pytest.mark.asyncio
    async def test_file_removal_failure_is_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        monkeypatch.setattr(
            routes_mod,
            "uninstall_app",
            lambda name, keep_data=True: AppResult(
                ok=False, name=name, error="permission denied"
            ),
        )
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/uninstall", json={})
            assert resp.status == 400
            assert (await resp.json())["error"] == "permission denied"


# ---------------------------------------------------------------------------
# Enable / disable warning + rollback branches
# ---------------------------------------------------------------------------


class TestEnableBranches:
    #: An OS that is never the host, so the app under test is always
    #: "unsupported here". Hardcoding "windows" made this pass on Linux and fail
    #: on Windows (there the platform IS supported, so onEnable runs and trips
    #: the guard below with a 500). Derive it instead.
    _FOREIGN_OS = "linux" if platform_mod.system() == "Windows" else "windows"

    @pytest.mark.asyncio
    async def test_client_app_script_skipped_on_unsupported_platform(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A client-install app declares a desktop payload for another OS; its
        # onEnable can only fail here, so it must be skipped, not executed.
        _setup_env(tmp_path, monkeypatch)
        _install(
            tmp_path,
            setup={"onEnable": "open /Applications/Nope.app"},
            platform={"os": [self._FOREIGN_OS], "installMode": "client"},
        )

        async def _must_not_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("onEnable must not run on an unsupported platform")

        monkeypatch.setattr(routes_mod, "_run_lifecycle_script", _must_not_run)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/enable")
            assert resp.status == 200
            body = await resp.json()
        assert body["onEnable"]["skipped"] == "unsupported_platform"

    @pytest.mark.asyncio
    async def test_failed_on_enable_rolls_back_to_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, setup={"onEnable": "setup.sh"})

        async def _failed(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"output": "boom", "failed": True}

        monkeypatch.setattr(routes_mod, "_run_lifecycle_script", _failed)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/enable")
            assert resp.status == 400
            body = await resp.json()
            assert body["code"] == "on_enable_failed"
            # The rollback really left the app disabled.
            resp = await client.get(f"/api/apps/{APP}")
            assert (await resp.json())["enabled"] is False

    @pytest.mark.asyncio
    async def test_hook_failure_becomes_a_warning_not_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)

        async def _boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("hook exploded")

        monkeypatch.setattr(routes_mod, "on_app_enable", _boom)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/enable")
            assert resp.status == 200
            body = await resp.json()
        assert any("hooks failed" in w for w in body["warnings"])

    @pytest.mark.asyncio
    async def test_health_status_issues_are_redacted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)

        async def _hooks(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "health_status": {
                    "issues": [
                        "cannot reach token=ghp_0123456789abcdefghijABCDEFGHIJ0123456789"
                    ]
                }
            }

        monkeypatch.setattr(routes_mod, "on_app_enable", _hooks)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/enable")
            assert resp.status == 200
            issues = (await resp.json())["hooks"]["health_status"]["issues"]
        assert "ghp_0123456789abcdefghijABCDEFGHIJ0123456789" not in issues[0]

    @pytest.mark.asyncio
    async def test_enable_of_unknown_app_is_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/apps/ghost/enable")
            assert resp.status == 404


class TestDisableBranches:
    @pytest.mark.asyncio
    async def test_not_installed_is_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/apps/ghost/disable")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_failed_on_disable_warns_but_still_disables(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, setup={"onDisable": "teardown.sh"})
        enable_app(APP)

        async def _failed(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "output": "failed with token=ghp_0123456789abcdefghijABCDEFGHIJ0123456789",
                "failed": True,
            }

        # Patched on the SHARED teardown, not on `routes`: this PR routes the
        # disable path's hook/backend/onDisable work through
        # `apps/teardown.py::teardown_app_runtime`, the one implementation the
        # trust-revocation path also calls, so `routes` no longer holds these
        # symbols. The behaviour these tests pin is unchanged — the warnings still
        # surface on the disable response — only the module that owns the step moved.
        from kiro_crew.apps import teardown as teardown_mod

        # Patched on `teardown`, NOT on `lifecycle_scripts`: teardown does
        # `from ... import run_lifecycle_script`, so it holds its own binding and a
        # patch on the defining module never reaches it. Getting this wrong passed
        # locally for the wrong reason — the real runner executed, the missing
        # script failed, and the expected warning appeared anyway — while on a CI
        # host with no unprivileged userns the sandbox raised instead, producing
        # "could not be run" rather than "script failed".
        monkeypatch.setattr(teardown_mod, "run_lifecycle_script", _failed)
        monkeypatch.setattr(teardown_mod, "stop_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/disable")
            assert resp.status == 200
            body = await resp.json()
            warnings = body["warnings"]
            assert any("onDisable script failed" in w for w in warnings)
            assert not any(
                "ghp_0123456789abcdefghijABCDEFGHIJ0123456789" in w for w in warnings
            )
            resp = await client.get(f"/api/apps/{APP}")
            assert (await resp.json())["enabled"] is False

    @pytest.mark.asyncio
    async def test_hook_failure_and_cron_cleanup_surface_as_warnings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)

        async def _boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("disable hook exploded")

        # Patched on the SHARED teardown, not on `routes`: this PR routes the
        # disable path's hook/backend/onDisable work through
        # `apps/teardown.py::teardown_app_runtime`, the one implementation the
        # trust-revocation path also calls, so `routes` no longer holds these
        # symbols. The behaviour these tests pin is unchanged — the warnings still
        # surface on the disable response — only the module that owns the step moved.
        from kiro_crew.apps import teardown as teardown_mod

        monkeypatch.setattr(teardown_mod, "on_app_disable", _boom)
        monkeypatch.setattr(teardown_mod, "stop_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/disable")
            assert resp.status == 200
            assert any(
                "hooks disable failed" in w for w in (await resp.json())["warnings"]
            )

    @pytest.mark.asyncio
    async def test_cron_cleanup_message_is_forwarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)

        async def _hooks(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"cron_cleanup": "2 job(s) left enabled", "other": 1}

        # Patched on the SHARED teardown, not on `routes`: this PR routes the
        # disable path's hook/backend/onDisable work through
        # `apps/teardown.py::teardown_app_runtime`, the one implementation the
        # trust-revocation path also calls, so `routes` no longer holds these
        # symbols. The behaviour these tests pin is unchanged — the warnings still
        # surface on the disable response — only the module that owns the step moved.
        from kiro_crew.apps import teardown as teardown_mod

        monkeypatch.setattr(teardown_mod, "on_app_disable", _hooks)
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/disable")
            assert (await resp.json())["warnings"] == ["2 job(s) left enabled"]

    @pytest.mark.asyncio
    async def test_disable_failure_is_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        monkeypatch.setattr(
            routes_mod,
            "disable_app",
            lambda name: AppResult(ok=False, name=name, error="metadata locked"),
        )
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/disable")
            assert resp.status == 400
            assert (await resp.json())["error"] == "metadata locked"


# ---------------------------------------------------------------------------
# _client_install_manifest
# ---------------------------------------------------------------------------


class TestClientInstallManifest:
    def test_missing_or_non_dict_platform(self) -> None:
        assert _client_install_manifest({}) is None
        assert _client_install_manifest({"platform": "macos"}) is None

    def test_malformed_platform_block_is_ignored(self) -> None:
        # ``"os": null`` makes PlatformConfig.from_dict raise TypeError; an
        # unguarded call would turn a hand-edited manifest into a 500 on enable.
        assert _client_install_manifest({"platform": {"os": None}}) is None

    def test_server_install_mode_is_not_a_client_app(self) -> None:
        assert _client_install_manifest({"platform": {"os": ["linux"]}}) is None

    def test_client_install_mode_returns_config(self) -> None:
        cfg = _client_install_manifest(
            {"platform": {"os": ["macos"], "installMode": "client"}}
        )
        assert cfg is not None
        assert cfg.installMode == "client"
        assert cfg.supports_platform("darwin")
        assert not cfg.supports_platform("linux")


# ---------------------------------------------------------------------------
# POST /api/apps/{name}/open
# ---------------------------------------------------------------------------


class TestOpenApp:
    @pytest.mark.asyncio
    async def test_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/apps/ghost/open")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_disabled_app_is_409(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, openCommand="true")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/open")
            assert resp.status == 409
            assert (await resp.json())["code"] == "app_disabled"

    @pytest.mark.asyncio
    async def test_missing_open_command_is_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/open")
            assert resp.status == 400
            assert "openCommand" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_execution_denial_is_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, openCommand="true")
        enable_app(APP)
        monkeypatch.setattr(
            routes_mod,
            "app_execution_denied",
            lambda name, **kwargs: "third-party app execution is not admitted",
        )
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/open")
            assert resp.status == 403
            body = await resp.json()
        assert body["code"] == "app_execution_denied"
        assert "not admitted" in body["error"]

    @pytest.mark.asyncio
    async def test_headless_host_returns_command_instead_of_launching(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No DISPLAY and not macOS: the gateway must hand the command back
        # rather than spawn something no one can see.
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, openCommand="open-my-app")
        enable_app(APP)
        monkeypatch.setattr(routes_mod, "app_execution_denied", lambda n, **k: None)
        monkeypatch.setattr(platform_mod, "system", lambda: "Linux")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/open")
            assert resp.status == 200
            body = await resp.json()
        assert body["remote"] is True
        assert body["ok"] is False
        assert body["command"] == "open-my-app"

    @pytest.mark.asyncio
    async def test_launch_returns_pid_on_a_desktop_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, openCommand="open-my-app")
        enable_app(APP)
        monkeypatch.setattr(routes_mod, "app_execution_denied", lambda n, **k: None)
        monkeypatch.setattr(platform_mod, "system", lambda: "Darwin")
        wrapped: dict[str, Any] = {}

        def _wrap(argv: list[str], mode: str = "standard") -> tuple[list[str], Any]:
            wrapped["argv"] = argv
            wrapped["mode"] = mode
            return argv, None

        async def _spawn(*argv: str, **kwargs: Any) -> Any:
            return SimpleNamespace(pid=4321)

        monkeypatch.setattr(routes_mod, "wrap_argv", _wrap)
        monkeypatch.setattr(routes_mod, "cgroup_scope_argv", lambda argv: argv)
        monkeypatch.setattr(routes_mod, "create_subprocess_limited", _spawn)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/open")
            assert resp.status == 200
            body = await resp.json()
        assert body == {"ok": True, "name": APP, "pid": 4321}
        # The command is sandboxed and cgroup-capped, never bare.
        assert wrapped["argv"] == ["/bin/sh", "-c", "open-my-app"]
        assert wrapped["mode"] == "standard"

    @pytest.mark.asyncio
    async def test_launch_failure_is_500(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, openCommand="open-my-app")
        enable_app(APP)
        monkeypatch.setattr(routes_mod, "app_execution_denied", lambda n, **k: None)
        monkeypatch.setattr(platform_mod, "system", lambda: "Darwin")
        monkeypatch.setattr(
            routes_mod, "wrap_argv", lambda argv, mode="standard": (argv, None)
        )
        monkeypatch.setattr(routes_mod, "cgroup_scope_argv", lambda argv: argv)

        async def _boom(*argv: str, **kwargs: Any) -> Any:
            raise OSError("no such executable")

        monkeypatch.setattr(routes_mod, "create_subprocess_limited", _boom)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/open")
            assert resp.status == 500
            assert "failed to launch" in (await resp.json())["error"]


# ---------------------------------------------------------------------------
# POST /api/apps/registry/install
# ---------------------------------------------------------------------------


class TestRegistryInstall:
    @pytest.mark.asyncio
    async def test_invalid_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/registry/install",
                data="{",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_name_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/apps/registry/install", json={})
            assert resp.status == 400
            assert "name required" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_needs_client_install_is_200_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)

        async def _needs_client(name: str, **kwargs: Any) -> dict[str, Any]:
            return {"ok": False, "name": name, "needsClientInstall": True}

        monkeypatch.setattr(routes_mod, "install_from_registry", _needs_client)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/registry/install", json={"name": "some-app"}
            )
            assert resp.status == 200
            assert (await resp.json())["needsClientInstall"] is True

    @pytest.mark.asyncio
    async def test_failure_redacts_log_and_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)

        async def _failed(name: str, **kwargs: Any) -> dict[str, Any]:
            return {
                "ok": False,
                "name": name,
                "log": "cloning with token=ghp_0123456789abcdefghijABCDEFGHIJ0123456789",
                "error": "auth failed for token=ghp_0123456789abcdefghijABCDEFGHIJ0123456789",
            }

        monkeypatch.setattr(routes_mod, "install_from_registry", _failed)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/registry/install", json={"name": "some-app"}
            )
            assert resp.status == 400
            body = await resp.json()
        assert "ghp_0123456789abcdefghijABCDEFGHIJ0123456789" not in body["log"]
        assert "ghp_0123456789abcdefghijABCDEFGHIJ0123456789" not in body["error"]

    @pytest.mark.asyncio
    async def test_success_registers_and_returns_201(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)

        async def _ok(name: str, **kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "name": APP, "log": "done"}

        started: list[str] = []
        monkeypatch.setattr(routes_mod, "install_from_registry", _ok)
        monkeypatch.setattr(
            routes_mod, "start_app_backend", lambda n: started.append(n)
        )
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/registry/install", json={"name": APP}
            )
            assert resp.status == 201
            body = await resp.json()
        assert "registration" in body
        assert started == [APP]


# ---------------------------------------------------------------------------
# POST /api/apps/registry/install-stream (SSE)
# ---------------------------------------------------------------------------


class TestRegistryInstallStream:
    @pytest.mark.asyncio
    async def test_invalid_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/registry/install-stream",
                data="{",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_name_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/apps/registry/install-stream", json={})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_streams_log_lines_then_done(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)

        async def _streaming(name: str, log_lines: Any = None, **kw: Any) -> dict:
            assert log_lines is not None
            log_lines.append("step one")
            # Multi-line output must be reframed as multiple data: lines so a
            # newline in build output cannot break SSE framing.
            log_lines.append("step two\nstep two continued")
            return {"ok": True, "name": APP}

        monkeypatch.setattr(routes_mod, "install_from_registry", _streaming)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/registry/install-stream", json={"name": APP}
            )
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/event-stream")
            events = _sse_events(await resp.text())
        assert ("log", "step one") in events
        assert ("log", "step two\nstep two continued") in events
        name, payload = events[-1]
        assert name == "done"
        done = json.loads(payload)
        assert done["ok"] is True
        assert "registration" in done

    @pytest.mark.asyncio
    async def test_failed_install_reports_done_with_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)

        async def _failed(name: str, log_lines: Any = None, **kw: Any) -> dict:
            return {"ok": False, "name": name, "error": "build failed"}

        monkeypatch.setattr(routes_mod, "install_from_registry", _failed)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/registry/install-stream", json={"name": "some-app"}
            )
            assert resp.status == 200
            events = _sse_events(await resp.text())
        name, payload = events[-1]
        assert name == "done"
        assert json.loads(payload)["error"] == "build failed"

    @pytest.mark.asyncio
    async def test_needs_client_install_short_circuits_done(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)

        async def _needs_client(name: str, log_lines: Any = None, **kw: Any) -> dict:
            return {"ok": True, "name": name, "needsClientInstall": True}

        monkeypatch.setattr(routes_mod, "install_from_registry", _needs_client)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/registry/install-stream", json={"name": "some-app"}
            )
            events = _sse_events(await resp.text())
        assert json.loads(events[-1][1])["needsClientInstall"] is True

    @pytest.mark.asyncio
    async def test_install_exception_becomes_a_done_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A crash inside the install must still close the stream cleanly —
        # otherwise the dashboard hangs on an open SSE connection.
        _setup_env(tmp_path, monkeypatch)

        async def _boom(name: str, log_lines: Any = None, **kw: Any) -> dict:
            raise RuntimeError("clone exploded")

        monkeypatch.setattr(routes_mod, "install_from_registry", _boom)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/apps/registry/install-stream", json={"name": "some-app"}
            )
            events = _sse_events(await resp.text())
        assert json.loads(events[-1][1])["error"] == "clone exploded"


# ---------------------------------------------------------------------------
# GET /apps/{name}/ui/{path} — path and type validation
# ---------------------------------------------------------------------------


class TestAppUiFile:
    @pytest.mark.asyncio
    async def test_traversal_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                f"/apps/{APP}/ui/..%2Fapp.json", allow_redirects=False
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_disallowed_extension_is_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/apps/{APP}/ui/run.sh")
            assert resp.status == 403
            assert "not allowed" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_missing_file_is_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/apps/{APP}/ui/missing.mjs")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_symlink_escaping_ui_root_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An allowed extension and a real file are not enough — the resolved
        # path must still live under the app's ui/ root.
        home = _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        outside = tmp_path / "outside.json"
        outside.write_text('{"secret": true}', encoding="utf-8")
        ui = home / "apps" / APP / "ui"
        ui.mkdir(parents=True, exist_ok=True)
        (ui / "escape.json").symlink_to(outside)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/apps/{APP}/ui/escape.json")
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid path"


# ---------------------------------------------------------------------------
# POST /api/apps/{name}/dev
# ---------------------------------------------------------------------------


class TestAppDevMode:
    @pytest.mark.asyncio
    async def test_invalid_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                f"/api/apps/{APP}/dev",
                data="{",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["true", 1, None])
    async def test_enabled_must_be_a_boolean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: Any
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                f"/api/apps/{APP}/dev", json={"enabled": value}
            )
            assert resp.status == 400
            assert "boolean" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_not_installed_is_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post("/api/apps/ghost/dev", json={"enabled": True})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_toggle_succeeds_for_installed_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(f"/api/apps/{APP}/dev", json={"enabled": True})
            assert resp.status == 200
            assert "error" not in (await resp.json())


# ---------------------------------------------------------------------------
# GET/PUT /api/apps/{name}/config
# ---------------------------------------------------------------------------


class TestAppConfig:
    @pytest.mark.asyncio
    async def test_not_installed_is_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/apps/ghost/config")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_get_seeds_empty_config_when_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        cfg = home / "apps" / APP / "data" / "config.json"
        if cfg.exists():
            cfg.unlink()
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/apps/{APP}/config")
            assert resp.status == 200
            assert await resp.json() == {}
        # Seeded on disk so the app is not left in a perpetual loading state.
        assert cfg.is_file()

    @pytest.mark.asyncio
    async def test_get_malformed_config_is_500(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        cfg = home / "apps" / APP / "data" / "config.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text("{ not json", encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/apps/{APP}/config")
            assert resp.status == 500
            assert "failed to read config" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_put_invalid_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                f"/api/apps/{APP}/config",
                data="{",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_rejects_non_object(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(f"/api/apps/{APP}/config", json=[1, 2, 3])
            assert resp.status == 400
            assert "JSON object" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_put_then_get_round_trips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                f"/api/apps/{APP}/config", json={"theme": "dark"}
            )
            assert resp.status == 200
            assert (await resp.json())["ok"] is True
            resp = await client.get(f"/api/apps/{APP}/config")
            assert await resp.json() == {"theme": "dark"}


# ---------------------------------------------------------------------------
# No-bundled-entry blob clone-URL resolution (inline URL-form check)
# ---------------------------------------------------------------------------


class TestNoEntryBlobCloneUrlResolution:
    """The no-bundled-entry blob branch resolves the clone URL by an inline
    in-memory URL-form check on the already-validated ``repo`` — NO registry
    read.

    ``_registry_git_url`` (a standalone resolver that re-consulted
    ``get_registry_app_by_repo``) has been DELETED.  The bundled entry lookup
    runs exactly once per request (``get_registry_app_by_repo`` at the top of
    ``handle_blob_proxy``); the no-entry branch then only decides whether the
    validated ``repo`` is itself a clone URL, a pure string-shape test.  That
    removes the second, event-loop-blocking registry read GPT 5.6 flagged and
    the URL-form / no-bundled-entry resolution boundary the old resolver's unit
    tests covered — both are now exercised through the handler here.
    """

    async def _clone_url_for(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        repo: str,
    ) -> tuple[int, str | None]:
        """Drive ``handle_blob_proxy`` for a no-bundled-entry ``repo`` and report
        the HTTP status and the ``git_url`` threaded into ``_fetch_git_blob`` (or
        ``None`` if the fetch was never reached)."""
        _setup_env(tmp_path, monkeypatch)
        # ``repo`` is admitted (known_registry_repos) but has NO bundled entry —
        # the external/federated branch.  Count the registry entry lookups so a
        # reintroduced second read on the clone path fails loudly.
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {repo})

        calls = {"n": 0}

        def _entry_lookup(r: str) -> None:
            calls["n"] += 1
            return None

        monkeypatch.setattr(routes_mod, "get_registry_app_by_repo", _entry_lookup)

        seen: dict[str, Any] = {}

        async def _record(
            repo: str,
            ref: str,
            file_path: str,
            cache_path: Path,
            git_url: str,
            *,
            owner_designated: bool = False,
        ) -> bool:
            seen["git_url"] = git_url
            seen["owner_designated"] = owner_designated
            return False

        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _record)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": repo, "path": "logo.png", "ref": "main"},
            )
        # The registry entry lookup runs EXACTLY once (the pre-read at the top of
        # the handler) — the no-entry branch adds no second read.
        assert calls["n"] == 1, calls["n"]
        return resp.status, seen.get("git_url")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "repo",
        [
            "https://github.com/org/app",
            "git@github.com:org/app.git",
            "ssh://git@example.com:2222/org/app.git",
        ],
    )
    async def test_url_shaped_repo_is_honored_without_a_bundled_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: str
    ) -> None:
        # External (federated) registries never resolve a bundled entry, so a
        # validated URL-form ``repo`` is honored directly — threaded as the clone
        # URL, anonymous + strict (``owner_designated`` False).
        status, git_url = await self._clone_url_for(tmp_path, monkeypatch, repo=repo)
        assert status == 502  # the fake fetch returns False → graceful 502
        assert git_url == repo

    @pytest.mark.asyncio
    async def test_bare_name_without_entry_is_blob_no_git_url_502(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A bare (non-URL) token with no bundled entry has no resolvable clone
        # URL, so the handler short-circuits with the ``blob_no_git_url`` 502 and
        # never reaches ``_fetch_git_blob``.
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"just-a-name"})
        monkeypatch.setattr(routes_mod, "get_registry_app_by_repo", lambda r: None)

        async def _must_not_fetch(*a: Any, **k: Any) -> bool:
            raise AssertionError("no resolvable clone URL must not reach the fetch")

        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _must_not_fetch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "just-a-name", "path": "logo.png", "ref": "main"},
            )
            assert resp.status == 502
            assert (await resp.json())["code"] == "blob_no_git_url"

    @pytest.mark.asyncio
    async def test_no_entry_branch_does_not_re_read_the_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # REGRESSION (PR 5027 round 4 — GPT 5.6 BLOCKING: registry cache re-read
        # blocks the event loop).  Before the subtraction the no-entry branch ran
        # ``clone_url = _registry_git_url(repo)``, which re-consulted
        # ``get_registry_app_by_repo`` — an unbounded SYNCHRONOUS registry
        # JSON read — a SECOND time on the async request path, stalling the
        # gateway loop and heartbeat under a concurrent registry refresh.  The
        # subtraction deletes that resolver and resolves the clone URL by a pure
        # in-memory URL-form check, so the registry entry lookup happens exactly
        # ONCE per request (the pre-read at the top of the handler) and never on
        # the no-entry branch.  ``_clone_url_for`` asserts the lookup call count
        # is exactly 1; a reintroduced second read would make it 2 and fail here.
        status, git_url = await self._clone_url_for(
            tmp_path, monkeypatch, repo="https://github.com/org/external-app.git"
        )
        assert status == 502
        assert git_url == "https://github.com/org/external-app.git"


# ---------------------------------------------------------------------------
# GET /api/apps/blob — SSRF gate + cache
# ---------------------------------------------------------------------------


class TestBlobProxy:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "query,status",
        [
            ({"repo": "", "path": ""}, 400),
            ({"repo": "acme", "path": ""}, 400),
            ({"repo": "a;rm -rf /", "path": "i.png", "ref": "main"}, 400),
            ({"repo": "acme", "path": "assets/i$.png", "ref": "main"}, 400),
            ({"repo": "acme", "path": "i.png", "ref": "bad ref"}, 400),
            ({"repo": "acme", "path": "../../etc/i.png", "ref": "main"}, 400),
            ({"repo": "acme", "path": ".git/config.png", "ref": "main"}, 400),
            ({"repo": "acme", "path": "payload.svg.exe", "ref": "main"}, 403),
        ],
    )
    async def test_validation_rejects_unsafe_inputs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        query: dict[str, str],
        status: int,
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/apps/blob", params=query)
            assert resp.status == status

    @pytest.mark.asyncio
    async def test_repo_outside_registry_is_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The SSRF gate: only repos the registry already knows may be cloned.
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={
                    "repo": "https://evil.example/org/app",
                    "path": "i.png",
                    "ref": "main",
                },
            )
            assert resp.status == 403
            assert (await resp.json())["error"] == "repo not in registry"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "ref",
        [
            "../other-repo-key/main",  # climb out of this repo_key into a sibling
            "../../etc/main",  # deeper traversal
            "/main",  # absolute-form leading slash
            "main/../../secret",  # a ``..`` mid-segment
        ],
    )
    async def test_ref_with_traversal_is_rejected_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ref: str
    ) -> None:
        # REGRESSION (PR 5027 round 6 — GPT 5.6 BLOCKING: ``ref`` cache-path
        # traversal).  ``ref`` becomes a path segment in the blob cache tree
        # (``.../{repo_key}/{ref}/{file_path}``).  ``_SAFE_REF_RE`` permits ``.``
        # and ``/``, so ``../<other-repo-key>/main`` matches the regex; the
        # cache-root containment check catches an escape OUT of the cache root but
        # NOT a ``..`` that stays UNDER the root while crossing into a DIFFERENT
        # repo's cache dir — a crafted ``ref`` then yields a cache hit returning
        # another repo's cached (possibly private) bytes.  The guard: reject any
        # ``..`` segment or leading ``/`` in ``ref`` (mirroring the ``file_path``
        # guard) so a ``ref`` can only name a flat branch subtree under its own
        # ``repo_key``.  A traversal ``ref`` must 400 and never reach a fetch or a
        # sibling repo-key cache read.  Fails at 803dddcb (regex + containment let
        # it through); passes after.
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})

        async def _must_not_fetch(*args: Any, **kwargs: Any) -> bool:
            raise AssertionError("a traversal ref must be rejected before any fetch")

        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _must_not_fetch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "acme", "path": "logo.png", "ref": ref},
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid ref"

    @pytest.mark.asyncio
    async def test_cached_blob_is_served_without_fetching(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})

        async def _must_not_fetch(*args: Any, **kwargs: Any) -> bool:
            raise AssertionError("cache hit must not trigger a clone")

        # The cache key is bound to the resolved clone URL (provenance), so the
        # handler must resolve a URL before it looks up the cache.  Give ``acme``
        # a bundled entry whose clone URL is the same value the cache dir is keyed
        # on, so this exercises a genuine cache HIT on the provenance-bound key.
        clone_url = "ssh://forge.example/org/acme.git"
        monkeypatch.setattr(
            routes_mod,
            "get_registry_app_by_repo",
            lambda repo: {"repo": repo, "gitUrl": clone_url, "branch": "main"},
        )
        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _must_not_fetch)
        cache = (
            routes_mod._blob_cache_dir()
            / routes_mod._blob_cache_key("acme", clone_url)
            / "main"
            / "assets"
        )
        cache.mkdir(parents=True)
        (cache / "logo.png").write_bytes(b"\x89PNG\r\n")

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "acme", "path": "assets/logo.png", "ref": "main"},
            )
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "image/png"
            assert resp.headers["Cache-Control"] == "public, max-age=86400"
            assert await resp.read() == b"\x89PNG\r\n"

    @pytest.mark.asyncio
    async def test_repo_key_reuse_across_registries_does_not_serve_stale_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # REGRESSION (PR 5027 round 6 — GPT 5.6 BLOCKING: private cache entries
        # outlive their provenance).  ``_blob_cache_key`` once keyed the cache dir
        # on the ``repo`` STRING alone.  Chain: registry A (private) caches a blob
        # under repo key X; A is removed and registry B is later configured reusing
        # key X; B's request would then hit A's cached (possibly private) bytes.
        # The fix binds the cache key to the RESOLVED clone URL (provenance), so a
        # repo-key reuse across registries lands in a DISTINCT cache directory (a
        # miss + a fresh clone of B's own URL) rather than serving A's stale bytes.
        #
        # Simulate the swap: A cached bytes for repo key ``acme`` under a dir keyed
        # on A's URL; the resolver now returns B's entry (a DIFFERENT clone URL) for
        # the same key.  The request must NOT serve A's cached bytes — the
        # provenance-bound key differs, so it is a cache miss and the (faked) fetch
        # runs instead.  Fails at 803dddcb (repo-string key → A's bytes served);
        # passes after.
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})

        url_a = "ssh://forge.example/org/registry-a.git"  # the removed, private A
        url_b = "ssh://forge.example/org/registry-b.git"  # B, reusing key ``acme``

        # A's stale private bytes, cached under a dir keyed on A's provenance.
        stale = (
            routes_mod._blob_cache_dir()
            / routes_mod._blob_cache_key("acme", url_a)
            / "main"
            / "assets"
        )
        stale.mkdir(parents=True)
        (stale / "logo.png").write_bytes(b"A-PRIVATE-BYTES")

        # The registry now resolves key ``acme`` to B's clone URL (the swap).
        monkeypatch.setattr(
            routes_mod,
            "get_registry_app_by_repo",
            lambda repo: {"repo": repo, "gitUrl": url_b, "branch": "main"},
        )

        fetched: dict[str, str] = {}

        async def _record(
            repo: str,
            ref: str,
            file_path: str,
            cache_path: Path,
            git_url: str,
            *,
            owner_designated: bool = False,
        ) -> bool:
            # The cache lookup missed (key bound to B's URL, not A's), so the
            # handler fell through to a fresh clone of B's own URL.
            fetched["git_url"] = git_url
            fetched["cache_path"] = str(cache_path)
            return False

        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _record)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "acme", "path": "assets/logo.png", "ref": "main"},
            )
            # Cache miss → the fetch ran and (being faked to fail) yields 502.
            # The load-bearing property: A's private bytes were NOT served.
            assert resp.status == 502
            assert await resp.read() != b"A-PRIVATE-BYTES"

        # The fresh clone targets B's provenance, and the cache path is keyed on
        # B's URL — never A's stale directory.
        assert fetched["git_url"] == url_b
        assert routes_mod._blob_cache_key("acme", url_b) in fetched["cache_path"]
        assert routes_mod._blob_cache_key("acme", url_a) not in fetched["cache_path"]

    @pytest.mark.asyncio
    async def test_failed_fetch_is_502(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})

        async def _fail(*args: Any, **kwargs: Any) -> bool:
            return False

        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _fail)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "acme", "path": "logo.png", "ref": "main"},
            )
            assert resp.status == 502

    @pytest.mark.asyncio
    async def test_ref_defaults_to_registry_entry_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})
        monkeypatch.setattr(
            routes_mod,
            "get_registry_app_by_repo",
            lambda repo: {"repo": repo, "branch": "release"},
        )
        seen: dict[str, str] = {}

        async def _record(
            repo: str,
            ref: str,
            file_path: str,
            cache_path: Path,
            git_url: str,
            *,
            owner_designated: bool = False,
        ) -> bool:
            seen["ref"] = ref
            return False

        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _record)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob", params={"repo": "acme", "path": "logo.png"}
            )
            assert resp.status == 502
        assert seen["ref"] == "release"

    @pytest.mark.asyncio
    async def test_symlinked_cache_dir_escaping_root_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The resolved-path check must fire BEFORE any mkdir, so a symlinked
        # cache subtree cannot be used to write outside the blob cache root.
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})
        # The cache key is provenance-bound, so the handler resolves a clone URL
        # before the containment check runs; give ``acme`` an entry so the URL
        # resolves and the symlinked cache dir is keyed on the same value.
        clone_url = "ssh://forge.example/org/acme.git"
        monkeypatch.setattr(
            routes_mod,
            "get_registry_app_by_repo",
            lambda repo: {"repo": repo, "gitUrl": clone_url, "branch": "main"},
        )

        async def _must_not_fetch(*args: Any, **kwargs: Any) -> bool:
            raise AssertionError("path validation must reject before fetching")

        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _must_not_fetch)
        outside = tmp_path / "outside-cache"
        outside.mkdir()
        cache_root = routes_mod._blob_cache_dir()
        cache_root.mkdir(parents=True, exist_ok=True)
        (cache_root / routes_mod._blob_cache_key("acme", clone_url)).symlink_to(outside)

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "acme", "path": "logo.png", "ref": "main"},
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid path"
        assert not (outside / "main").exists()

    def test_safe_repo_identifier_accepts_vetted_git_forms(self) -> None:
        assert _is_safe_repo_identifier("BareName_1")
        assert _is_safe_repo_identifier("https://github.com/org/app.git")
        assert _is_safe_repo_identifier("git@github.com:org/app.git")
        assert _is_safe_repo_identifier("ssh://git@host:2222/org/app")
        assert _is_safe_repo_identifier("ssh://host/org/app")

    def test_safe_repo_identifier_rejects_untrusted_forms(self) -> None:
        # Plaintext http is refused: registry clones later execute setup code.
        assert not _is_safe_repo_identifier("")
        assert not _is_safe_repo_identifier("http://github.com/org/app")
        assert not _is_safe_repo_identifier("https://host/org/../../app")
        assert not _is_safe_repo_identifier("https://host/org/app;id")
        assert not _is_safe_repo_identifier("org/app")

# ---------------------------------------------------------------------------
# Blob-fetch credential posture (same-repo carve-out, PR 918 extended to the
# third clone chokepoint).  These pin the env + sandbox-mode PAIR the blob
# clone uses per origin, without asserting raw git argv (wrap_argv is patched
# to capture only the mode it was handed).
# ---------------------------------------------------------------------------


class _FakeProc:
    """A create_subprocess_limited stand-in whose clone always 'fails'.

    Returning rc=1 makes ``_fetch_git_blob`` bail right after the clone, so the
    posture is observable with no real subprocess, checkout, or filesystem read.
    """

    returncode = 1

    async def communicate(self) -> tuple[bytes, bytes]:
        return (b"", b"boom")

    def kill(self) -> None:  # pragma: no cover - timeout path only
        pass


class TestFetchGitBlobCredentialPosture:
    """``_fetch_git_blob`` picks env + sandbox mode from ``owner_designated``."""

    def _capture(
        self,
        monkeypatch: pytest.MonkeyPatch,
        git_url: str,
    ) -> dict[str, Any]:
        import kiro_crew.apps.registry as reg_mod

        captured: dict[str, Any] = {}

        # ``_fetch_git_blob`` resolves NO clone URL from ``repo``: the standalone
        # ``_registry_git_url`` resolver has been deleted and the caller threads
        # the once-decided URL in as the required ``git_url`` param.  The
        # no-re-resolution invariant is therefore STRUCTURAL — there is no
        # resolver left for the callee to call — not a monkeypatched tripwire.
        # ``is_clone_host_trusted`` is still imported function-locally inside
        # ``_fetch_git_blob`` (part of the untouched SSRF gate), so it is patched on
        # the registry module.  The credential-posture helpers, by contrast, were
        # hoisted to ``routes`` module scope, so they are patched there — patching
        # ``reg_mod`` would no longer intercept the module-level name.
        monkeypatch.setattr(reg_mod, "is_clone_host_trusted", lambda url: True)
        # Sentinel env dicts so the test asserts WHICH builder was used without
        # depending on the host's real environment contents.
        monkeypatch.setattr(routes_mod, "minimal_env", lambda **extra: {"_env": "minimal"})
        monkeypatch.setattr(routes_mod, "anonymous_git_env", lambda **extra: {"_env": "anonymous"})
        monkeypatch.setattr(routes_mod, "_context_clone_sandbox_mode", lambda url: "context-mode")

        def _fake_wrap(cmd: list[str], *, mode: str) -> tuple[list[str], None]:
            captured["mode"] = mode
            # The clone argv is ``git clone ... <git_url> <tmp_root>`` — the URL
            # the process will actually clone is the second-to-last element.  We
            # DON'T assert the full argv (wrap_argv/cgroup lesson); we read back
            # only the URL to confirm the credential decision and the clone agree.
            captured["cloned_url"] = cmd[-2]
            return (cmd, None)

        monkeypatch.setattr(routes_mod, "wrap_argv", _fake_wrap)
        monkeypatch.setattr(routes_mod, "cgroup_scope_argv", lambda cmd: cmd)

        # Capture the SEL credential-grant audit call (a privilege escalation must
        # leave a record) without depending on the real SEL sink.
        grants: list[tuple[str, str]] = []
        monkeypatch.setattr(
            routes_mod,
            "_sel_credential_grant",
            lambda operation, git_url: grants.append((operation, git_url)),
        )
        captured["grants"] = grants

        async def _fake_create(*args: Any, **kwargs: Any) -> _FakeProc:
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        monkeypatch.setattr(routes_mod, "create_subprocess_limited", _fake_create)
        return captured

    @pytest.mark.asyncio
    async def test_owner_designated_uses_minimal_env_and_context_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        url = "ssh://forge.example/org/registry.git"
        cap = self._capture(monkeypatch, url)

        # Control: an owner-designated entry whose URL is ``url``.  The caller
        # threads ``git_url=url`` — the SAME URL the carve-out was decided for —
        # so the carve-out is honored.  ``_fetch_git_blob`` no longer re-resolves
        # from ``repo``; it uses the threaded value for both the decision and the
        # clone.
        ok = await routes_mod._fetch_git_blob(
            url,
            "main",
            "assets/logo.png",
            tmp_path / "out.png",
            git_url=url,
            owner_designated=True,
        )

        assert ok is False  # the fake clone fails → graceful fallback
        # Same-repo carve-out flips BOTH knobs together.
        assert cap["mode"] == "context-mode"
        assert cap["env"] == {"_env": "minimal"}
        # The privilege escalation left an SEL audit record for the URL cloned.
        assert cap["grants"] == [("app_blob_proxy", url)]
        # The threaded URL is the one actually cloned — decision and clone agree.
        assert cap["cloned_url"] == url

    @pytest.mark.asyncio
    async def test_default_is_anonymous_env_and_strict_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        cap = self._capture(monkeypatch, "ssh://forge.example/org/sibling.git")

        # A sibling repo on the same trusted host — a DIFFERENT URL from the
        # configured registry — never gets the carve-out.  The caller threads the
        # sibling URL and ``owner_designated=False``.
        ok = await routes_mod._fetch_git_blob(
            "ssh://forge.example/org/sibling.git",
            "main",
            "assets/logo.png",
            tmp_path / "out.png",
            git_url="ssh://forge.example/org/sibling.git",
        )

        assert ok is False
        assert cap["mode"] == "strict"
        assert cap["env"] == {"_env": "anonymous"}
        # The default (anonymous) path grants no credentials → no audit record.
        assert cap["grants"] == []
        assert cap["cloned_url"] == "ssh://forge.example/org/sibling.git"

    @pytest.mark.asyncio
    async def test_injected_cloneurl_never_becomes_the_credentialed_clone_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # REGRESSION (PR 5027 cross-registry finding), now enforced by CONSTRUCTION
        # rather than a downgrade recheck.  Two registries are configured — A
        # (repo=urlA) and B (repo=urlB, a separately-configured PRIVATE registry).
        # A's untrusted index injects an app entry that carries an explicit
        # ``cloneUrl=urlB`` alongside A's own ``gitUrl=urlA``.
        #
        # The earlier finding was that a standalone clone-URL resolver honored that
        # injected ``cloneUrl`` while the credential decision used ``_entry_git_url``,
        # so the two resolvers named different URLs and the clone could reach urlB
        # with owner credentials.  The subtraction deletes the divergence: that
        # resolver is gone and ``cloneUrl`` is no longer read anywhere, so the only
        # URL that can reach the clone is the one the caller threads — urlA, the
        # entry's own ``gitUrl``, byte-identical to the URL the carve-out was
        # decided for.  urlB is never the clone target, credentialed or otherwise,
        # so there is no cross-registry leak and nothing to downgrade.  (That
        # ``cloneUrl`` is not read by the entry resolver ``_entry_git_url`` is
        # pinned in test_external_registry.py; here we pin the end-to-end
        # clone-target guarantee through the handler + callee.)
        _setup_env(tmp_path, monkeypatch)
        url_a = "ssh://forge.example/org/registry-a.git"
        url_b = "ssh://forge.example/org/private-b.git"

        # When the carve-out is honored (owner_designated + the caller threads
        # the entry's own urlA), credentials apply to urlA only — the SEL grant
        # names urlA, the clone uses urlA, and urlB never appears as a clone
        # target.  ``git_url`` is the threaded value; the callee does not resolve
        # it from ``repo``, so the injected ``cloneUrl=urlB`` cannot reach the
        # clone even if a resolver returned it.
        cap = self._capture(monkeypatch, url_a)
        ok = await routes_mod._fetch_git_blob(
            "acme",
            "main",
            "assets/logo.png",
            tmp_path / "out.png",
            git_url=url_a,
            owner_designated=True,
        )
        assert ok is False
        assert cap["mode"] == "context-mode"
        assert cap["env"] == {"_env": "minimal"}
        assert cap["grants"] == [("app_blob_proxy", url_a)]
        assert cap["cloned_url"] == url_a
        assert all(url_b not in grant for _op, grant in cap["grants"])

    @pytest.mark.asyncio
    async def test_anonymous_env_suppresses_git_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The load-bearing property of the default posture, checked against the
        # REAL env builders (not the sentinels): the anonymous env disables
        # system+global git config and never prompts, while minimal_env — used
        # for the owner-designated repo — does not strip those.
        _setup_env(tmp_path, monkeypatch)
        import kiro_crew.apps.registry as reg_mod

        anon = reg_mod.anonymous_git_env()
        minimal = reg_mod.minimal_env()
        assert anon.get("GIT_CONFIG_NOSYSTEM") == "1"
        assert anon.get("GIT_TERMINAL_PROMPT") == "0"
        assert "GIT_CONFIG_NOSYSTEM" not in minimal
        assert "GIT_TERMINAL_PROMPT" not in minimal


class TestBlobProxyOwnerDesignatedWiring:
    """The handler decides ``owner_designated`` via ``_is_owner_designated_repo``.

    A same-repo entry (index URL byte-identical to the owner-configured registry
    repo) threads ``owner_designated=True`` into the fetch; a sibling repo on the
    same host and a bundled entry thread ``False``.
    """

    async def _owner_designated_for(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        entry: dict[str, Any] | None,
        repo: str = "acme",
        owner_count: int = 1,
        ref: str = "main",
    ) -> bool:
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {repo})
        monkeypatch.setattr(routes_mod, "get_registry_app_by_repo", lambda r: entry)
        # Provenance: how many configured registry SOURCES publish this ``repo``
        # key.  These cases inject the entry via ``get_registry_app_by_repo``
        # rather than through the real registry files, so pin the count directly.
        # The single-owner default (1) is the control the carve-out is designed
        # for; the multi-owner ambiguity is exercised by the dedicated provenance
        # regression below.
        monkeypatch.setattr(routes_mod, "_repo_key_owner_count", lambda r: owner_count)
        # For the entry-present cases the handler resolves the clone URL from
        # ``_entry_git_url(entry)``.  For the no-entry case the handler resolves
        # it by an inline URL-form check on ``repo`` (no registry read), so the
        # caller must pass a URL-form ``repo`` for the fetch to be reached and
        # ``owner_designated`` observable — a bare name would short-circuit at the
        # ``blob_no_git_url`` 502 before the fetch.
        seen: dict[str, Any] = {}

        async def _record(
            repo: str,
            ref: str,
            file_path: str,
            cache_path: Path,
            git_url: str,
            *,
            owner_designated: bool = False,
        ) -> bool:
            seen["owner_designated"] = owner_designated
            seen["git_url"] = git_url
            return False

        monkeypatch.setattr(routes_mod, "_fetch_git_blob", _record)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": repo, "path": "logo.png", "ref": ref},
            )
            assert resp.status == 502
        return seen["owner_designated"]

    @pytest.mark.asyncio
    async def test_same_repo_entry_is_owner_designated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.apps.registry as reg_mod

        url = "ssh://forge.example/org/registry.git"
        # An external-index entry whose clone URL equals the owner-configured
        # registry repo.  Patch the merged predicate's config source so the
        # byte-identical comparison matches.
        monkeypatch.setattr(
            reg_mod,
            "_effective_registries",
            lambda: [SimpleNamespace(name="corp", repo=url)],
        )
        entry = {"repo": url, "gitUrl": url, "_registry": "corp"}
        assert await self._owner_designated_for(tmp_path, monkeypatch, entry=entry) is True

    @pytest.mark.asyncio
    async def test_configured_branch_ref_is_owner_designated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # CONTROL for the F1 gate: the SAME owner-designated entry whose
        # configured branch is ``main``, served on ``ref=main``, IS credentialed.
        # Paired with the differing-ref regression below — together they pin that
        # ONLY the configured branch attaches credentials.
        import kiro_crew.apps.registry as reg_mod

        url = "ssh://forge.example/org/registry.git"
        monkeypatch.setattr(
            reg_mod,
            "_effective_registries",
            lambda: [SimpleNamespace(name="corp", repo=url)],
        )
        entry = {"repo": url, "gitUrl": url, "branch": "main", "_registry": "corp"}
        assert (
            await self._owner_designated_for(tmp_path, monkeypatch, entry=entry, ref="main") is True
        )

    @pytest.mark.asyncio
    async def test_query_ref_differing_from_configured_branch_is_not_owner_designated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # REGRESSION (PR 5027 round 6 — GPT 5.6 BLOCKING: the credential grant
        # ignored the effective ref).  ``ref`` falls back to the entry's
        # configured branch ONLY when the query param is empty; a caller can
        # otherwise supply any ``_SAFE_REF_RE``-valid ``ref`` (e.g.
        # ``iconPath=logo.png&ref=private``).  Before this gate ``owner_designated``
        # was decided on the ENTRY alone, so a crafted ``ref`` drove an
        # owner-credentialed shallow clone of an UNCONFIGURED (e.g. private) branch
        # of the owner's repo and served its image bytes.  The gate: the owner
        # grant is honored only when the effective ``ref`` equals the entry's
        # configured branch; a differing ``ref`` never attaches credentials
        # (``owner_designated`` False → anonymous+strict, still serving a public
        # branch).  The paired control above pins that ``ref=main`` (the configured
        # branch) on the SAME entry IS credentialed.  Fails at 803dddcb (grant
        # ignores ref); passes after.
        import kiro_crew.apps.registry as reg_mod

        url = "ssh://forge.example/org/registry.git"
        monkeypatch.setattr(
            reg_mod,
            "_effective_registries",
            lambda: [SimpleNamespace(name="corp", repo=url)],
        )
        # Configured branch is ``main``; an attacker-chosen ref (an unconfigured
        # branch) must NOT be credentialed.
        entry = {"repo": url, "gitUrl": url, "branch": "main", "_registry": "corp"}
        assert (
            await self._owner_designated_for(tmp_path, monkeypatch, entry=entry, ref="private")
            is False
        )

    @pytest.mark.asyncio
    async def test_sibling_repo_same_host_is_not_owner_designated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kiro_crew.apps.registry as reg_mod

        registry_url = "ssh://forge.example/org/registry.git"
        sibling_url = "ssh://forge.example/org/sibling.git"
        monkeypatch.setattr(
            reg_mod,
            "_effective_registries",
            lambda: [SimpleNamespace(name="corp", repo=registry_url)],
        )
        # Same trusted host, DIFFERENT URL → carve-out must not apply.
        entry = {"repo": sibling_url, "gitUrl": sibling_url, "_registry": "corp"}
        assert await self._owner_designated_for(tmp_path, monkeypatch, entry=entry) is False

    @pytest.mark.asyncio
    async def test_bundled_entry_is_not_owner_designated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A bundled entry carries no ``_registry`` marker, so the predicate
        # returns False and the blob clone stays anonymous + strict.
        entry = {"repo": "acme", "gitUrl": "ssh://forge.example/org/acme.git"}
        assert await self._owner_designated_for(tmp_path, monkeypatch, entry=entry) is False

    @pytest.mark.asyncio
    async def test_no_entry_is_not_owner_designated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No registry entry at all (a full-URL external/federated repo) → the
        # inline URL-form check resolves the clone URL, and the default posture
        # (anonymous + strict, ``owner_designated`` False) applies.
        assert (
            await self._owner_designated_for(
                tmp_path,
                monkeypatch,
                entry=None,
                repo="https://github.com/org/external-app.git",
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_owner_designation_is_entry_scoped_not_global_membership(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The carve-out decision is entry-scoped, not a membership test over the
        # global set of configured registry repos.  Two registries are configured,
        # A (corp) and B (private); the A-owned entry's own URL is urlA.  The
        # handler decides ``owner_designated`` from ``_is_owner_designated_repo``,
        # which matches the entry's own ``_registry`` (A) — so it is True for the
        # A-owned entry.  The URL threaded to the fetch is resolved from the SAME
        # entry (``_entry_git_url``), so the URL the fetch clones is urlA — never
        # B, even though B is also configured.
        import kiro_crew.apps.registry as reg_mod

        url_a = "ssh://forge.example/org/registry-a.git"
        url_b = "ssh://forge.example/org/private-b.git"
        monkeypatch.setattr(
            reg_mod,
            "_effective_registries",
            lambda: [
                SimpleNamespace(name="corp", repo=url_a),
                SimpleNamespace(name="private", repo=url_b),
            ],
        )
        entry = {"repo": url_a, "gitUrl": url_a, "_registry": "corp"}
        assert await self._owner_designated_for(tmp_path, monkeypatch, entry=entry) is True

    @pytest.mark.asyncio
    async def test_concurrent_refresh_cannot_redirect_credentialed_clone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # REGRESSION (PR 5027 round 3 — TOCTOU between the credential decision and
        # the clone).  ``handle_blob_proxy`` decides ``owner_designated`` and
        # resolves the clone URL from ONE registry entry, then threads that URL
        # into ``_fetch_git_blob``.  The bug this pins: if the callee re-resolved
        # the clone URL from ``repo`` a SECOND time, a concurrent registry refresh
        # landing between the decision and the clone could swap the entry backing
        # ``repo`` to a private sibling — so an owner-credential grant decided for
        # urlA would clone urlB (a different private repo) WITH those credentials.
        #
        # Simulate the concurrent refresh: the entry lookup returns the
        # owner-designated urlA entry (the decision + the threaded URL).  The
        # standalone clone-URL resolver that a callee-side re-resolution would
        # have gone through has been DELETED, so there is structurally no second
        # read to race: the clone uses the THREADED urlA.  At c6fa20c7 the callee
        # re-resolved from ``repo`` and would clone urlB with credentials.  We
        # drive the real ``handle_blob_proxy`` + ``_fetch_git_blob`` with only the
        # subprocess/env/sandbox faked, so the threading is exercised end to end
        # (never the raw git argv — we read back only the cloned URL and the
        # env/mode PAIR).
        import kiro_crew.apps.registry as reg_mod

        _setup_env(tmp_path, monkeypatch)
        url_a = "ssh://forge.example/org/registry-a.git"
        url_b = "ssh://forge.example/org/private-b.git"

        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})
        # The decided entry: owner-designated, its own URL is urlA.  A concurrent
        # refresh could remap ``repo`` to urlB (a swapped-in private sibling), but
        # the callee never re-reads the registry — the deleted resolver leaves no
        # re-resolution path — so the swap cannot reach the clone.
        entry = {"repo": "acme", "gitUrl": url_a, "_registry": "corp"}
        monkeypatch.setattr(routes_mod, "get_registry_app_by_repo", lambda repo: entry)
        # Single unambiguous owner for the served ``repo`` key — the provenance
        # gate is satisfied, so this exercises the surviving credentialed path.
        monkeypatch.setattr(routes_mod, "_repo_key_owner_count", lambda r: 1)
        monkeypatch.setattr(
            reg_mod,
            "_effective_registries",
            lambda: [SimpleNamespace(name="corp", repo=url_a)],
        )
        monkeypatch.setattr(reg_mod, "is_clone_host_trusted", lambda url: True)

        captured: dict[str, Any] = {}
        monkeypatch.setattr(routes_mod, "minimal_env", lambda **extra: {"_env": "minimal"})
        monkeypatch.setattr(routes_mod, "anonymous_git_env", lambda **extra: {"_env": "anonymous"})
        monkeypatch.setattr(routes_mod, "_context_clone_sandbox_mode", lambda url: "context-mode")

        def _fake_wrap(cmd: list[str], *, mode: str) -> tuple[list[str], None]:
            captured["mode"] = mode
            captured["cloned_url"] = cmd[-2]
            return (cmd, None)

        monkeypatch.setattr(routes_mod, "wrap_argv", _fake_wrap)
        monkeypatch.setattr(routes_mod, "cgroup_scope_argv", lambda cmd: cmd)

        grants: list[tuple[str, str]] = []
        monkeypatch.setattr(
            routes_mod,
            "_sel_credential_grant",
            lambda operation, git_url: grants.append((operation, git_url)),
        )

        async def _fake_create(*args: Any, **kwargs: Any) -> _FakeProc:
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        monkeypatch.setattr(routes_mod, "create_subprocess_limited", _fake_create)

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "acme", "path": "logo.png", "ref": "main"},
            )
            # The fake clone fails (rc=1) → graceful 502, but the posture is
            # observable from what reached the subprocess.
            assert resp.status == 502

        # The clone used the THREADED urlA, never the swapped-in urlB.
        assert captured["cloned_url"] == url_a
        assert url_b not in captured["cloned_url"]
        # Owner credentials were granted for urlA — and the SEL grant names urlA,
        # so credentials never reached urlB.
        assert captured["env"] == {"_env": "minimal"}
        assert captured["mode"] == "context-mode"
        assert grants == [("app_blob_proxy", url_a)]
        assert all(url_b not in g for _op, g in grants)

    @pytest.mark.asyncio
    async def test_ambiguous_provenance_downgrades_to_anonymous_strict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # REGRESSION (PR 5027 round 5 — GPT 5.6 BLOCKING: cross-registry confused
        # deputy).  ``get_registry_app_by_repo`` selects the entry by ``repo`` key
        # alone (bundled first, then each external registry), provenance-blind.  If
        # two configured registries — A (owner-designated for repo key X) and B (a
        # separately-configured registry that also lists an app keyed X) — both
        # claim X, a request reachable only through B resolves to A's
        # owner-designated entry.  Before the provenance gate the handler then
        # granted A's owner credentials and cloned A's private repo, serving A's
        # private image bytes to a B-reachable caller.
        #
        # The gate: the credentialed carve-out is reachable ONLY when exactly ONE
        # configured source publishes the ``repo`` key
        # (``_repo_key_owner_count == 1``).  With A and B both claiming X the count
        # is 2, so ``owner_designated`` stays False and the clone is
        # anonymous+strict — A's owner credentials are NEVER used and A's private
        # bytes are never cloned with a grant on a B-reachable request.  We drive
        # the real ``handle_blob_proxy`` + ``_fetch_git_blob`` with only the
        # subprocess/env/sandbox faked, reading back the env/mode PAIR and the SEL
        # grants (never the raw git argv).  Fails at 64df951a (repo-keyed lookup
        # grants A's credentials); passes after.
        import kiro_crew.apps.registry as reg_mod

        _setup_env(tmp_path, monkeypatch)
        url_a = "ssh://forge.example/org/registry-a.git"

        monkeypatch.setattr(routes_mod, "known_registry_repos", lambda: {"acme"})
        # The repo-keyed lookup returns A's owner-designated entry (its own URL is
        # urlA); ``_is_owner_designated_repo`` WOULD return True for it in
        # isolation.  What must stop the grant is provenance, not the entry check.
        entry = {"repo": "acme", "gitUrl": url_a, "_registry": "corp-a"}
        monkeypatch.setattr(routes_mod, "get_registry_app_by_repo", lambda repo: entry)
        # Two configured sources publish the same ``repo`` key → ambiguous.
        monkeypatch.setattr(routes_mod, "_repo_key_owner_count", lambda r: 2)
        monkeypatch.setattr(
            reg_mod,
            "_effective_registries",
            lambda: [SimpleNamespace(name="corp-a", repo=url_a)],
        )
        monkeypatch.setattr(reg_mod, "is_clone_host_trusted", lambda url: True)

        captured: dict[str, Any] = {}
        monkeypatch.setattr(routes_mod, "minimal_env", lambda **extra: {"_env": "minimal"})
        monkeypatch.setattr(routes_mod, "anonymous_git_env", lambda **extra: {"_env": "anonymous"})
        monkeypatch.setattr(routes_mod, "_context_clone_sandbox_mode", lambda url: "context-mode")

        def _fake_wrap(cmd: list[str], *, mode: str) -> tuple[list[str], None]:
            captured["mode"] = mode
            captured["cloned_url"] = cmd[-2]
            return (cmd, None)

        monkeypatch.setattr(routes_mod, "wrap_argv", _fake_wrap)
        monkeypatch.setattr(routes_mod, "cgroup_scope_argv", lambda cmd: cmd)

        grants: list[tuple[str, str]] = []
        monkeypatch.setattr(
            routes_mod,
            "_sel_credential_grant",
            lambda operation, git_url: grants.append((operation, git_url)),
        )

        async def _fake_create(*args: Any, **kwargs: Any) -> _FakeProc:
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        monkeypatch.setattr(routes_mod, "create_subprocess_limited", _fake_create)

        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(
                "/api/apps/blob",
                params={"repo": "acme", "path": "logo.png", "ref": "main"},
            )
            assert resp.status == 502

        # Ambiguous provenance → anonymous + strict.  The load-bearing assertion:
        # owner credentials (minimal_env + context sandbox mode) are NEVER used,
        # so A's private repo is not cloned with a grant on a B-reachable request.
        assert captured["env"] == {"_env": "anonymous"}
        assert captured["mode"] == "strict"
        # No credential grant was made, so nothing is SEL-audited as an escalation.
        assert grants == []

    @pytest.mark.asyncio
    async def test_owner_designated_branch_resolves_sandbox_mode_off_the_event_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # REGRESSION (PR 5027 round 5 — Opus 4.8 BLOCKING: synchronous config-load
        # on the event loop).  Inside ``_fetch_git_blob``'s ``owner_designated``
        # branch, ``_context_clone_sandbox_mode(git_url)`` flows
        # ``_configured_registry_hosts`` -> ``_effective_registries`` ->
        # ``KiroCrewConfig.load`` — an unbounded ``read_text`` + ``json.loads`` +
        # ``jsonschema.validate`` on a cold/invalidated cache (e.g. right after a
        # registry refresh rewrites config).  ``_fetch_git_blob`` runs on the
        # gateway event loop during App Store browsing, so calling it inline would
        # freeze every concurrent chat turn and the liveness heartbeat.  The two
        # sibling reads in the same function are already offloaded via
        # ``asyncio.to_thread``; this one must be too.
        #
        # Assert via thread-identity: the resolver must observe a threadpool-worker
        # thread, NOT the event-loop thread.  At 64df951a the call is synchronous
        # inline, so it runs on the loop thread and this fails; after the offload
        # it runs on a worker and passes.  (Indexing ``ran_on[0]`` is deliberate: a
        # resolver that never ran raises rather than passing vacuously.)
        import kiro_crew.apps.registry as reg_mod

        _setup_env(tmp_path, monkeypatch)
        url = "ssh://forge.example/org/registry.git"
        loop_thread = threading.current_thread().name
        ran_on: list[str] = []

        def _record_sandbox_mode(git_url: str) -> str:
            ran_on.append(threading.current_thread().name)
            return "context-mode"

        monkeypatch.setattr(routes_mod, "_context_clone_sandbox_mode", _record_sandbox_mode)
        monkeypatch.setattr(reg_mod, "is_clone_host_trusted", lambda u: True)
        monkeypatch.setattr(routes_mod, "minimal_env", lambda **extra: {"_env": "minimal"})
        monkeypatch.setattr(routes_mod, "anonymous_git_env", lambda **extra: {"_env": "anonymous"})
        monkeypatch.setattr(routes_mod, "wrap_argv", lambda cmd, *, mode: (cmd, None))
        monkeypatch.setattr(routes_mod, "cgroup_scope_argv", lambda cmd: cmd)
        monkeypatch.setattr(routes_mod, "_sel_credential_grant", lambda operation, git_url: None)

        async def _fake_create(*args: Any, **kwargs: Any) -> _FakeProc:
            return _FakeProc()

        monkeypatch.setattr(routes_mod, "create_subprocess_limited", _fake_create)

        ok = await routes_mod._fetch_git_blob(
            url,
            "main",
            "assets/logo.png",
            tmp_path / "out.png",
            git_url=url,
            owner_designated=True,
        )

        assert ok is False  # the fake clone fails → graceful fallback
        # The resolver ran off the event-loop thread (on a threadpool worker).
        assert ran_on[0] != loop_thread


# ---------------------------------------------------------------------------
# App-secret cache + backend URL resolution
# ---------------------------------------------------------------------------


class TestAppSecretCache:
    def test_missing_secret_is_not_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_env(tmp_path, monkeypatch)
        assert _get_app_secret(APP) == ""
        # A secret provisioned after the first miss must still be picked up.
        app_dir = home / "apps" / APP
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / ".app_secret").write_text("s3cret\n", encoding="utf-8")
        assert _get_app_secret(APP) == "s3cret"

    def test_cached_secret_survives_file_removal_until_invalidated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_env(tmp_path, monkeypatch)
        app_dir = home / "apps" / APP
        app_dir.mkdir(parents=True, exist_ok=True)
        secret_file = app_dir / ".app_secret"
        secret_file.write_text("cached-value", encoding="utf-8")
        assert _get_app_secret(APP) == "cached-value"
        secret_file.unlink()
        assert _get_app_secret(APP) == "cached-value"
        invalidate_app_secret_cache(APP)
        assert _get_app_secret(APP) == ""


class TestResolveAppBackendUrl:
    def test_gateway_tracked_port_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(routes_mod, "get_app_backend_port", lambda n: 7712)
        assert _resolve_app_backend_url(APP) == "http://127.0.0.1:7712"

    def test_no_manifest_is_unresolvable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(routes_mod, "get_app_backend_port", lambda n: None)
        monkeypatch.setattr(routes_mod, "get_app_manifest", lambda n: None)
        assert _resolve_app_backend_url(APP) is None

    def test_self_managed_fixed_port_from_manifest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(routes_mod, "get_app_backend_port", lambda n: None)
        monkeypatch.setattr(
            routes_mod,
            "get_app_manifest",
            lambda n: SimpleNamespace(
                backend=SimpleNamespace(entryPoint="server.py", port="7801"),
                mcpServers={},
            ),
        )
        assert _resolve_app_backend_url(APP) == "http://127.0.0.1:7801"

    def test_auto_port_falls_back_to_mcp_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(routes_mod, "get_app_backend_port", lambda n: None)
        monkeypatch.setattr(
            routes_mod,
            "get_app_manifest",
            lambda n: SimpleNamespace(
                backend=SimpleNamespace(entryPoint="server.py", port="auto"),
                mcpServers={"x": {"url": "http://127.0.0.1:7778/mcp"}},
            ),
        )
        monkeypatch.setattr(
            routes_mod, "resolve_mcp_backend_url", lambda servers: "http://127.0.0.1:7778"
        )
        assert _resolve_app_backend_url(APP) == "http://127.0.0.1:7778"

    def test_non_numeric_port_falls_back_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(routes_mod, "get_app_backend_port", lambda n: None)
        monkeypatch.setattr(
            routes_mod,
            "get_app_manifest",
            lambda n: SimpleNamespace(
                backend=SimpleNamespace(entryPoint="server.py", port="not-a-port"),
                mcpServers={},
            ),
        )
        monkeypatch.setattr(routes_mod, "resolve_mcp_backend_url", lambda s: None)
        assert _resolve_app_backend_url(APP) is None


# ---------------------------------------------------------------------------
# Reverse proxy /apps/{name}/api/{path} — authorization gates
# ---------------------------------------------------------------------------


class _FakeCM:
    """Async context manager whose entry raises, standing in for a dead backend."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def __aenter__(self) -> Any:
        raise self._exc

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakeSession:
    closed = False

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def request(self, **kwargs: Any) -> _FakeCM:
        return _FakeCM(self._exc)

    async def close(self) -> None:
        return None


async def _swap_proxy_session(app: web.Application, exc: BaseException) -> None:
    real = app.get("_proxy_session")
    if real is not None and not real.closed:
        await real.close()
    app["_proxy_session"] = _FakeSession(exc)


class TestApiProxyAuthorization:
    @pytest.mark.asyncio
    async def test_traversal_is_rejected_before_anything_else(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/apps/{APP}/api/..%2Fsecret")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_app_token_cannot_reach_another_apps_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        app = _make_app(app_identity="some-other-app")
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(f"/apps/{APP}/api/ping")
            assert resp.status == 403
            assert "another app's backend" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_own_app_token_passes_the_cross_app_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same identity clears the cross-app guard and is stopped later, by the
        # backend resolution — proving the guard is identity-scoped, not a
        # blanket refusal of app tokens.
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        monkeypatch.setattr(routes_mod, "_resolve_app_backend_url", lambda n: None)
        app = _make_app(app_identity=APP)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(f"/apps/{APP}/api/ping")
            assert resp.status == 502
            assert "no reachable backend" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_disabled_app_is_403_with_error_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)  # installed but never enabled
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/apps/{APP}/api/ping")
            assert resp.status == 403
            assert (await resp.json())["code"] == "app_not_enabled"

    @pytest.mark.asyncio
    async def test_missing_app_secret_is_502(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        secret_file = home / "apps" / APP / ".app_secret"
        if secret_file.exists():
            secret_file.unlink()
        invalidate_app_secret_cache(APP)
        monkeypatch.setattr(
            routes_mod, "_resolve_app_backend_url", lambda n: "http://127.0.0.1:1"
        )
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/apps/{APP}/api/ping")
            assert resp.status == 502
            assert "has no secret" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_unreachable_backend_is_502(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aiohttp

        home = _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        (home / "apps" / APP / ".app_secret").write_text("k", encoding="utf-8")
        invalidate_app_secret_cache(APP)
        monkeypatch.setattr(
            routes_mod, "_resolve_app_backend_url", lambda n: "http://127.0.0.1:1"
        )
        app = _make_app()
        async with TestClient(TestServer(app)) as client:
            await _swap_proxy_session(client.app, aiohttp.ClientError("refused"))
            resp = await client.get(f"/apps/{APP}/api/ping")
            assert resp.status == 502
            assert (await resp.json())["error"] == "backend unreachable"

    @pytest.mark.asyncio
    async def test_backend_timeout_is_504(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        (home / "apps" / APP / ".app_secret").write_text("k", encoding="utf-8")
        invalidate_app_secret_cache(APP)
        monkeypatch.setattr(
            routes_mod, "_resolve_app_backend_url", lambda n: "http://127.0.0.1:1"
        )
        app = _make_app()
        async with TestClient(TestServer(app)) as client:
            await _swap_proxy_session(client.app, asyncio.TimeoutError())
            resp = await client.post(f"/apps/{APP}/api/run", json={"x": 1})
            assert resp.status == 504
            assert (await resp.json())["error"] == "backend timeout"


@pytest.mark.asyncio
async def test_api_proxy_signs_and_forwards_to_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end proxy hop against an in-process loopback backend.

    Verifies the three things the proxy owes the app backend: the
    ``X-KiroCrew-Proxy`` HMAC (validated with the app's own secret by the
    shipped verifier), the preserved ``/api/`` prefix + query string, and that
    user credentials are stripped rather than forwarded.
    """
    from kiro_crew.apps.proxy_auth import verify_proxy_request

    home = _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    enable_app(APP)
    (home / "apps" / APP / ".app_secret").write_text("proxy-key", encoding="utf-8")
    invalidate_app_secret_cache(APP)

    seen: dict[str, Any] = {}

    async def _backend_handler(request: web.Request) -> web.Response:
        body = await request.read()
        seen["path"] = request.path
        seen["query"] = request.query_string
        seen["headers"] = dict(request.headers)
        seen["body"] = body
        return web.json_response({"pong": True}, headers={"X-App": "yes"})

    backend = web.Application()
    backend.router.add_route("*", "/api/{tail:.*}", _backend_handler)
    async with TestServer(backend) as backend_server:
        monkeypatch.setattr(
            routes_mod,
            "_resolve_app_backend_url",
            lambda n: f"http://127.0.0.1:{backend_server.port}",
        )
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                f"/apps/{APP}/api/echo?a=1",
                data=b'{"hello":1}',
                headers={
                    "Content-Type": "application/json",
                    "Cookie": "mc_token_1=leak",
                    "Authorization": "Bearer leak",
                    "X-Custom": "kept",
                },
            )
            assert resp.status == 200
            assert resp.headers["X-App"] == "yes"
            assert await resp.json() == {"pong": True}

    assert seen["path"] == "/api/echo"
    assert seen["query"] == "a=1"
    assert seen["body"] == b'{"hello":1}'
    assert seen["headers"]["X-Custom"] == "kept"
    # User credentials must never reach an app backend.
    assert "Cookie" not in seen["headers"]
    assert "Authorization" not in seen["headers"]
    assert verify_proxy_request(
        seen["headers"]["X-KiroCrew-Proxy"],
        method="POST",
        target="/api/echo?a=1",
        body=b'{"hello":1}',
        secret="proxy-key",
    )


# ---------------------------------------------------------------------------
# DELETE /api/apps/{name}/migrate-cleanup
# ---------------------------------------------------------------------------


class TestMigrateCleanup:
    @pytest.mark.asyncio
    async def test_non_migrated_app_is_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.delete(f"/api/apps/{APP}/migrate-cleanup")
            assert resp.status == 400
            assert (await resp.json())["ok"] is False

    @pytest.mark.asyncio
    async def test_idempotent_when_already_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.delete("/api/apps/deploy-web/migrate-cleanup")
            assert resp.status == 200
            assert (await resp.json())["ok"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error_code,status",
        [
            ("not_orphaned", 400),
            ("replacement_missing", 409),
            ("io_error", 500),
            ("unknown_code", 400),
        ],
    )
    async def test_error_code_maps_to_http_status(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        error_code: str,
        status: int,
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        monkeypatch.setattr(
            routes_mod,
            "cleanup_migrated_builtin",
            lambda name: AppResult(
                ok=False, name=name, error="nope", error_code=error_code
            ),
        )
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.delete("/api/apps/deploy-web/migrate-cleanup")
            assert resp.status == status


# ---------------------------------------------------------------------------
# PUT /api/apps/registries — config-file failure modes
# ---------------------------------------------------------------------------


class TestRegistriesConfigFailures:
    @pytest.mark.asyncio
    async def test_malformed_config_json_is_500(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_env(tmp_path, monkeypatch)
        (home / "config.json").write_text("{ not json", encoding="utf-8")
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "AcmeApps"}]},
            )
            assert resp.status == 500
            assert "malformed" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_null_registries_value_is_repairable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An explicit ``"registries": null`` must not 500 the only endpoint
        # that can fix it.
        home = _setup_env(tmp_path, monkeypatch)
        (home / "config.json").write_text(
            json.dumps({"registries": None}), encoding="utf-8"
        )
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "https://git.example/org/apps"}]},
            )
            assert resp.status == 200
            body = await resp.json()
        assert body["ok"] is True
        assert body["newlyTrustedHosts"] == ["git.example"]


def test_on_disable_hook_resolves_hyphenated_builtin_names():
    """`on_disable` must be reachable for multi-word builtins.

    The disable route receives the MANIFEST name (`code-review-sage`), while
    `BUILTIN_NAMES` and the package dirs use underscores. Without normalizing,
    the membership test fails and the documented hook silently never fires — for
    every builtin whose name has more than one word, which is nearly all of them.
    """
    import importlib

    from kiro_crew.apps.builtins import BUILTIN_NAMES

    hyphenated = [n for n in BUILTIN_NAMES if "_" in n]
    assert hyphenated, "expected multi-word builtins to exist"
    for module_name in hyphenated:
        manifest_name = module_name.replace("_", "-")
        # What the route computes from the manifest name must land on the package.
        assert manifest_name.replace("-", "_") == module_name
        importlib.import_module(f"kiro_crew.apps.builtins.{module_name}")


def test_disable_route_normalizes_the_name_before_the_builtin_lookup():
    """Pins the normalization in the route itself, not just the name algebra."""
    import inspect

    from kiro_crew.apps import routes

    src = inspect.getsource(routes.handle_disable_app)
    assert 'name.replace("-", "_")' in src, (
        "the disable handler must normalize the manifest name before testing "
        "membership in BUILTIN_NAMES / importing the package")
