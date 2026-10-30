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

This module keeps the builtin-service and notification-channel helpers, the app list and
detail views, the deploy-web redirect, install validation, self-managed registration and
the owner-only lifecycle gate. Its sibling route modules are
``test_apps_routes_coverage_update_uninstall.py`` (update, uninstall, migrate-cleanup),
``test_apps_routes_coverage_enable_disable.py`` (enable and disable),
``test_apps_routes_coverage_registry_install.py`` (registry install, its SSE stream and
the registries config), ``test_apps_routes_coverage_blob_proxy.py`` (the git-blob proxy)
and ``test_apps_routes_coverage_app_serving.py`` (open, UI files, dev mode, app config, the
app secret, backend URL resolution and the reverse proxy). The helpers they share live in
``apps_routes_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from apps_routes_coverage_helpers import (
    APP,
    _install,
    _make_app,
    _make_app_source,
    _setup_env,
)
from dashboard_owner_helpers import as_owner

import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    AppResult,
    enable_app,
    register_external_app,
)
from kiro_crew.apps.routes import (
    _notify_builtin_service,
    _sync_builtin_config,
    _unregister_notification_channels,
    register_app_routes,
)
from kiro_crew.testing.wait import await_until


async def _no_op_executor(*args: Any, **kwargs: Any) -> None:
    return None


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
    def test_noop_without_state_or_bus(self, caplog: pytest.LogCaptureFixture) -> None:
        # No state at all, and state without a bus, must both be silent.
        caplog.set_level("INFO", logger="kiro_crew.apps.routes")
        _unregister_notification_channels(
            make_mocked_request("POST", "/x", app=web.Application()), APP
        )
        app = web.Application()
        app["state"] = SimpleNamespace()
        _unregister_notification_channels(
            make_mocked_request("POST", "/x", app=app), APP
        )
        assert [r for r in caplog.records if r.name == "kiro_crew.apps.routes"] == []

    def test_unregisters_via_bus(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level("INFO", logger="kiro_crew.apps.routes")
        bus = MagicMock()
        bus.unregister_app_channels.return_value = 2
        app = web.Application()
        app["state"] = SimpleNamespace(notification_bus=bus)
        _unregister_notification_channels(
            make_mocked_request("POST", "/x", app=app), APP
        )
        bus.unregister_app_channels.assert_called_once_with(APP)
        messages = [r.getMessage() for r in caplog.records if r.name == "kiro_crew.apps.routes"]
        assert messages == [f"Unregistered 2 notification channel(s) for app {APP}"]


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
            # Mirrors AppProcess.to_dict(): `running` is a real observation of the
            # tracked process, not something the handler asserts from the row existing.
            {"app_name": APP, "port": 7999, "healthy": True, "pid": 4242,
             "running": True},
            {"app_name": "other-app", "port": 7998, "healthy": False, "pid": 1,
             "running": True},
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


@pytest.mark.asyncio
async def test_list_apps_overwrites_the_trust_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        routes_mod,
        "list_apps",
        lambda: [
            {
                "name": APP,
                "sourceUrl": "HTTPS://Clone.Example.test/Owner/App.git/",
                "trustRepository": "https://evil.example/spoof",
            }
        ],
    )
    monkeypatch.setattr(routes_mod, "list_app_processes", lambda: [])

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps")
        assert resp.status == 200
        [entry] = await resp.json()

    assert entry["trustRepository"] == "https://clone.example.test/Owner/App"


@pytest.mark.asyncio
async def test_list_apps_never_returns_embedded_clone_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    secret = "SuperSecret"
    monkeypatch.setattr(
        routes_mod,
        "list_apps",
        lambda: [
            {
                "name": APP,
                "sourceUrl": f"HTTPS://User:{secret}@Clone.Example.test/Owner/App.git/",
                "manifest": {
                    "name": APP,
                    "repo": f"https://Manifest:{secret}@Clone.Example.test/Owner/App",
                },
            }
        ],
    )
    monkeypatch.setattr(routes_mod, "list_app_processes", lambda: [])

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps")
        assert resp.status == 200
        raw = await resp.text()

    assert secret not in raw
    [entry] = json.loads(raw)
    assert entry["sourceUrl"] == "HTTPS://Clone.Example.test/Owner/App.git/"
    assert entry["manifest"]["repo"] == "https://Clone.Example.test/Owner/App"
    assert entry["trustRepository"] == "https://clone.example.test/Owner/App"


@pytest.mark.asyncio
async def test_list_apps_legacy_query_source_never_exposes_a_trust_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    secret = "legacy-query-secret"
    raw_source = (
        f"https://clone.example.test/Owner/App.git?repo=A&access_token={secret}"
    )
    monkeypatch.setattr(
        routes_mod,
        "list_apps",
        lambda: [
            {
                "name": APP,
                "source": f"registry:{APP}",
                "sourceUrl": raw_source,
                "trustRepository": "https://evil.example/spoof",
            }
        ],
    )
    monkeypatch.setattr(routes_mod, "list_app_processes", lambda: [])

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps")
        body = await resp.text()

    assert resp.status == 200
    assert secret not in body
    assert raw_source not in body
    [entry] = json.loads(body)
    assert entry["sourceUrl"] == "https://clone.example.test/Owner/App.git"
    assert "trustRepository" not in entry


@pytest.mark.asyncio
async def test_get_app_overwrites_the_trust_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        routes_mod,
        "get_app",
        lambda name: {
            "name": name,
            "sourceUrl": "https://clone.example.test/Owner/App.git",
            "trustRepository": "https://evil.example/spoof",
        },
    )

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"/api/apps/{APP}")
        assert resp.status == 200
        entry = await resp.json()

    assert entry["trustRepository"] == "https://clone.example.test/Owner/App"


@pytest.mark.asyncio
async def test_list_apps_resolves_legacy_registry_trust_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-provenance registry install must show the current clone target."""
    _setup_env(tmp_path, monkeypatch)
    clone_target = "https://clone.example.test/Owner/legacy-app"
    monkeypatch.setattr(
        routes_mod,
        "list_apps",
        lambda: [
            {
                "name": APP,
                "source": f"registry:{APP}",
                "trustRepository": "https://evil.example/spoof",
            }
        ],
    )
    monkeypatch.setattr(routes_mod, "list_app_processes", lambda: [])
    monkeypatch.setattr(
        "kiro_crew.apps.registry.get_registry_app",
        lambda name: {"name": name, "gitUrl": f"{clone_target}.git"},
    )

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps")
        assert resp.status == 200
        [entry] = await resp.json()

    assert entry["trustRepository"] == clone_target


@pytest.mark.asyncio
async def test_get_app_keeps_genuinely_local_app_repositoryless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-named registry row must not be attached to a local install."""
    _setup_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        routes_mod,
        "get_app",
        lambda name: {
            "name": name,
            "source": str(tmp_path / "local-source"),
            "origin": "local",
            "trustRepository": "https://evil.example/spoof",
        },
    )

    def _must_not_resolve(_name: str):
        pytest.fail("a local install must not fall through to the registry")

    monkeypatch.setattr("kiro_crew.apps.registry.get_registry_app", _must_not_resolve)

    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get(f"/api/apps/{APP}")
        assert resp.status == 200
        entry = await resp.json()

    assert "trustRepository" not in entry


@pytest.mark.asyncio
async def test_list_apps_reports_a_tracked_but_exited_backend_as_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record outliving its process must not be reported as running.

    The handler must not hardcode ``running: True`` for anything present in the process
    table: a backend that has exited would be reported as up until something popped it.
    """
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    monkeypatch.setattr(
        routes_mod,
        "list_app_processes",
        lambda: [
            {"app_name": APP, "port": 7999, "healthy": False, "pid": 4242,
             "running": False},
        ],
    )
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/apps")
        assert resp.status == 200
        rows = await resp.json()
    entry = next(a for a in rows if a["name"] == APP)
    assert entry["backend_status"]["running"] is False
    assert entry["backend_status"]["healthy"] is False


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
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
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
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/install", json={"source": str(src)})
            assert resp.status == 400
            assert "999.0.0" in (await resp.json())["error"]
        assert not (home / "apps" / APP).exists()

    @pytest.mark.asyncio
    async def test_local_install_declaring_session_approval_does_not_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same gate as the registry paths: a fresh local install whose manifest
        # declares the grant is consent-pending, so nothing is registered and no
        # backend starts until the user enables it from the disclosure surface.
        _setup_env(tmp_path, monkeypatch)
        src = _make_app_source(tmp_path, permissions={"sessionApproval": True})
        started: list[str] = []
        registered: list[str] = []

        async def _register(name: str):
            registered.append(name)
            return routes_mod.RegistrationResult()

        async def _start(name: str) -> None:
            started.append(name)

        monkeypatch.setattr(routes_mod, "_register_app_off_loop", _register)
        monkeypatch.setattr(routes_mod, "_start_backend_after_install", _start)
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/install", json={"source": str(src)})
            assert resp.status == 201
            body = await resp.json()
        assert body["notice"] == "session_approval_reconsent"
        assert started == []
        assert registered == []
        info = routes_mod.get_app(APP)
        assert info["enabled"] is False
        assert info["sessionApprovalConsentPending"] is True

    @pytest.mark.asyncio
    async def test_unreadable_manifest_refuses_without_ownership_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        src = tmp_path / "source" / APP
        src.mkdir(parents=True)
        (src / APP_MANIFEST_FILENAME).write_text("{ corrupt", encoding="utf-8")

        def _must_not_install(*args: Any, **kwargs: Any) -> AppResult:
            raise AssertionError("install must not proceed without a stable app identity")

        monkeypatch.setattr(routes_mod, "install_app", _must_not_install)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/install", json={"source": str(src)})
            assert resp.status == 409
            body = await resp.json()
        assert body["code"] == "app_identity_unavailable"
        assert body["retryable"] is True

    @pytest.mark.asyncio
    async def test_manifest_name_change_refuses_before_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _setup_env(tmp_path, monkeypatch)
        src = _make_app_source(tmp_path)
        real_install = routes_mod.install_app

        def _change_identity_then_install(
            source: str, *, expected_name: str | None = None
        ) -> AppResult:
            manifest_path = Path(source) / APP_MANIFEST_FILENAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["name"] = "other-app"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            return real_install(source, expected_name=expected_name)

        monkeypatch.setattr(routes_mod, "install_app", _change_identity_then_install)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/install", json={"source": str(src)})
            assert resp.status == 400
            body = await resp.json()
        assert body["code"] == "app_identity_changed"
        assert not (home / "apps" / APP).exists()
        assert not (home / "apps" / "other-app").exists()

    @pytest.mark.asyncio
    async def test_install_failure_is_reported_as_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(
                "/api/apps/install", json={"source": str(tmp_path / "nope")}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "source_not_directory"

    @pytest.mark.asyncio
    async def test_live_detached_startup_hook_refuses_fresh_reinstall(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        source = _make_app_source(tmp_path)
        calls: list[tuple[str, bool]] = []

        async def _stop_retained(app_name: str, *, bounded: bool) -> bool:
            calls.append((app_name, bounded))
            return False

        monkeypatch.setattr(
            routes_mod, "stop_retained_startup_hooks", _stop_retained
        )

        def _must_not_install(*args: Any, **kwargs: Any) -> AppResult:
            raise AssertionError("install must not replace files while old code runs")

        monkeypatch.setattr(routes_mod, "install_app", _must_not_install)

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(
                "/api/apps/install", json={"source": str(source)}
            )
            assert resp.status == 409
            body = await resp.json()
        assert body["code"] == "startup_hook_still_running"
        assert body["retryable"] is True
        assert calls == [(APP, True)]


# ---------------------------------------------------------------------------
# POST /api/apps/register — self-managed app registration
# ---------------------------------------------------------------------------


class TestRegisterExternal:
    @pytest.mark.asyncio
    async def test_invalid_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
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
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/register", json=body)
            assert resp.status == 400
            assert "required" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_success_returns_secret(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
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
    async def test_public_registration_cannot_mint_registry_provenance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps.manager import _read_installed

        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            response = await client.post(
                "/api/apps/register",
                json={
                    "name": "ext-app",
                    "version": "1.0.0",
                    "displayName": "External App",
                    "source": "registry:ext-app",
                    "origin": "registry",
                },
            )

        assert response.status == 201
        meta = _read_installed("ext-app")
        assert meta is not None
        assert meta.source == ""
        assert meta.sourceUrl == ""
        assert meta.origin == "external"

    @pytest.mark.asyncio
    async def test_public_registration_cannot_mint_builtin_ownership(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps.manager import _builtin_owns_install, _read_installed

        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            first = await client.post(
                "/api/apps/register",
                json={
                    "name": "ext-app",
                    "version": "1.0.0",
                    "displayName": "External App",
                    "source": "builtin",
                    "origin": "builtin",
                },
            )
            second = await client.post(
                "/api/apps/register",
                json={
                    "name": "ext-app",
                    "version": "2.0.0",
                    "displayName": "External App v2",
                    "source": "builtin-app",
                },
            )

        assert first.status == 201
        assert second.status == 201
        meta = _read_installed("ext-app")
        assert meta is not None
        assert meta.version == "2.0.0"
        assert meta.source == "builtin-app"
        assert meta.origin == "external"
        assert not _builtin_owns_install(meta)

    @pytest.mark.asyncio
    async def test_registration_never_persists_or_returns_source_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps.manager import INSTALLED_META_FILENAME, app_dir

        _setup_env(tmp_path, monkeypatch)
        credential = "register-source-secret"
        source = (
            f"https://register-user:{credential}@clone.example.test/owner/ext-app.git"
        )
        safe_source = "https://clone.example.test/owner/ext-app.git"

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            registered = await client.post(
                "/api/apps/register",
                json={
                    "name": "ext-app",
                    "version": "1.0.0",
                    "displayName": "External App",
                    "source": source,
                    "lifecycle": "app",
                    "resources": "app",
                },
            )
            assert registered.status == 201
            assert credential not in await registered.text()

            persisted = (
                app_dir("ext-app") / INSTALLED_META_FILENAME
            ).read_text(encoding="utf-8")
            assert credential not in persisted
            assert json.loads(persisted)["source"] == safe_source

            detail = await client.get("/api/apps/ext-app")
            listing = await client.get("/api/apps")
            assert detail.status == 200
            assert listing.status == 200
            detail_text = await detail.text()
            listing_text = await listing.text()

        assert credential not in detail_text
        assert credential not in listing_text
        assert json.loads(detail_text)["source"] == safe_source
        [listed] = json.loads(listing_text)
        assert listed["source"] == safe_source

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "source,safe_source,secret",
        [
            (
                "ssh://deploy:register-ssh-password@clone.example.test/owner/ext-app.git",
                "ssh://deploy@clone.example.test/owner/ext-app.git",
                "register-ssh-password",
            ),
            (
                "deploy@clone.example.test:owner/ext-app.git",
                "deploy@clone.example.test:owner/ext-app.git",
                "",
            ),
            (
                "deploy:scp-password@clone.example.test:owner/ext-app.git",
                "deploy:scp-password@clone.example.test:owner/ext-app.git",
                "",
            ),
            (
                "https://clone.example.test/owner/ext-app.git?access_token=query-secret#private",
                "https://clone.example.test/owner/ext-app.git",
                "query-secret",
            ),
            (
                "ftp://user:ftp-secret@clone.example.test/owner/ext-app.git?token=query-secret#private",
                "ftp://clone.example.test/owner/ext-app.git",
                "ftp-secret",
            ),
        ],
    )
    async def test_registration_source_preserves_paths_and_sanitizes_explicit_uris(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        source: str,
        safe_source: str,
        secret: str,
    ) -> None:
        from kiro_crew.apps.manager import INSTALLED_META_FILENAME, app_dir

        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            registered = await client.post(
                "/api/apps/register",
                json={
                    "name": "ext-app",
                    "version": "1.0.0",
                    "displayName": "External App",
                    "source": source,
                    "lifecycle": "app",
                    "resources": "app",
                },
            )
            assert registered.status == 201
            detail = await client.get("/api/apps/ext-app")
            listing = await client.get("/api/apps")
            registered_text = await registered.text()
            detail_text = await detail.text()
            listing_text = await listing.text()

        persisted = (app_dir("ext-app") / INSTALLED_META_FILENAME).read_text(
            encoding="utf-8"
        )
        visible = "\n".join([registered_text, persisted, detail_text, listing_text])
        if secret:
            assert secret not in visible
            assert source not in visible
        assert json.loads(persisted)["source"] == safe_source
        assert json.loads(detail_text)["source"] == safe_source
        assert json.loads(listing_text)[0]["source"] == safe_source

    @pytest.mark.asyncio
    async def test_registration_republishes_declared_config_keys_off_the_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A self-managed app's manifest can declare ``config_keys``. Config validation
        reads them from a snapshot install and update republish; registering must
        republish it too, or the app's section reads as an unrecognized key until a
        restart. The refresh scans the apps directory, so it runs off the loop."""
        from kiro_crew.config import validation

        _setup_env(tmp_path, monkeypatch)
        refresh_threads: list[int] = []
        from kiro_crew.apps.manager import refresh_app_config_keys as real_refresh

        def _observed_refresh() -> frozenset[str]:
            refresh_threads.append(threading.get_ident())
            return real_refresh()

        # raising=False: the route reads the name through this facade, and on a
        # build where the route never refreshes the name is absent.
        monkeypatch.setattr(routes_mod, "refresh_app_config_keys", _observed_refresh, raising=False)
        validation.replace_extension_config_keys("apps", ())
        try:
            async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
                response = await client.post(
                    "/api/apps/register",
                    json={
                        "name": "ext-app",
                        "version": "1.0.0",
                        "displayName": "External App",
                        "manifest": {
                            "name": "ext-app",
                            "version": "1.0.0",
                            "displayName": "External App",
                            "description": "Test app",
                            "config_keys": ["ext_app"],
                        },
                    },
                )
            assert response.status == 201, await response.text()
            assert validation.extension_config_keys("apps") == frozenset({"ext_app"})
            assert len(refresh_threads) == 1
            assert refresh_threads[0] != threading.get_ident()
        finally:
            validation.replace_extension_config_keys("apps", ())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("repository_bound", [False, True])
    async def test_registry_owned_refresh_preserves_server_provenance(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        repository_bound: bool,
    ) -> None:
        from kiro_crew.apps.manager import _read_installed, set_app_provenance
        from kiro_crew.config.loader import _invalidate_config_cache

        home = _setup_env(tmp_path, monkeypatch)
        _invalidate_config_cache()
        repository = "https://clone.example.test/owner/ext-app.git"
        seeded = register_external_app(
            "ext-app",
            "1.0.0",
            "External App",
            source="registry:ext-app",
            origin="registry",
            resources="app",
            lifecycle="app",
            source_repository=repository,
        )
        assert seeded.ok, seeded.error
        assert set_app_provenance(
            "ext-app",
            source="registry:ext-app",
            url=repository,
            registry="registry-A",
            commit="a" * 40,
            signer="release-key",
        )
        if repository_bound:
            (home / "config.json").write_text(
                json.dumps(
                    {
                        "agent": {
                            "apps_allow_third_party": False,
                            "apps_trusted": ["ext-app"],
                            "apps_trusted_repositories": {"ext-app": repository},
                        }
                    }
                ),
                encoding="utf-8",
            )
            _invalidate_config_cache()

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            response = await client.post(
                "/api/apps/register",
                json={
                    "name": "ext-app",
                    "version": "2.0.0",
                    "displayName": "External App v2",
                    "source": "https://caller.example.test/spoof.git",
                    "origin": "external",
                    "resources": "app",
                    "lifecycle": "app",
                },
            )

        assert response.status == 201
        meta = _read_installed("ext-app")
        assert meta is not None
        assert meta.version == "2.0.0"
        assert meta.displayName == "External App v2"
        assert meta.source == "registry:ext-app"
        assert meta.sourceUrl == repository
        assert meta.sourceRegistry == "registry-A"
        assert meta.sourceCommit == "a" * 40
        assert meta.sourceSigner == "release-key"
        assert meta.origin == "registry"

    @pytest.mark.asyncio
    async def test_public_refresh_waits_for_registry_transition_and_keeps_latest_pin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The route lock prevents a stale pre-transition snapshot write-back."""
        from kiro_crew.apps.manager import (
            _read_installed,
            app_lifecycle_lock,
            set_app_provenance,
        )

        _setup_env(tmp_path, monkeypatch)
        initial = register_external_app(
            "ext-app",
            "1.0.0",
            "Local App",
            source="C:/local/source",
            origin="external",
        )
        assert initial.ok, initial.error

        original_register = routes_mod.register_external_app
        route_call_started = threading.Event()

        def _observed_register(*args: Any, **kwargs: Any) -> AppResult:
            route_call_started.set()
            return original_register(*args, **kwargs)

        monkeypatch.setattr(routes_mod, "register_external_app", _observed_register)
        request_entered = asyncio.Event()

        @web.middleware
        async def _mark_request(request: web.Request, handler: Any) -> web.StreamResponse:
            if request.path == "/api/apps/register":
                request_entered.set()
            return await handler(request)

        app = as_owner(web.Application(middlewares=[_mark_request]))
        register_app_routes(app)
        repository = "https://clone.example.test/owner/ext-app.git"

        async with TestClient(TestServer(app)) as client:
            lock = app_lifecycle_lock("ext-app")
            async with lock:
                pending = asyncio.create_task(
                    client.post(
                        "/api/apps/register",
                        json={
                            "name": "ext-app",
                            "version": "3.0.0",
                            "displayName": "Refreshed App",
                            "source": "C:/stale/request-source",
                            "origin": "external",
                        },
                    )
                )
                await await_until(request_entered)
                await asyncio.sleep(0)
                assert not route_call_started.is_set()

                transitioned = original_register(
                    "ext-app",
                    "2.0.0",
                    "Registry App",
                    source="registry:ext-app",
                    origin="registry",
                    source_repository=repository,
                )
                assert transitioned.ok, transitioned.error
                assert set_app_provenance(
                    "ext-app",
                    source="registry:ext-app",
                    url=repository,
                    registry="registry-A",
                    commit="a" * 40,
                    signer="release-key",
                )

            response = await pending

        assert response.status == 201
        assert route_call_started.is_set()
        meta = _read_installed("ext-app")
        assert meta is not None
        assert meta.version == "3.0.0"
        assert meta.displayName == "Refreshed App"
        assert meta.source == "registry:ext-app"
        assert meta.sourceUrl == repository
        assert meta.sourceRegistry == "registry-A"
        assert meta.sourceCommit == "a" * 40
        assert meta.sourceSigner == "release-key"
        assert meta.origin == "registry"

    @pytest.mark.asyncio
    async def test_rejected_name_is_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
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


class TestLifecycleRoutesAreOwnerOnly:
    """Register, uninstall and disable answer 403 to anyone but the owner.

    Rows: a non-owner dashboard subject (``app == ""``) and an app token, both
    before any side effect. The app token row on register names ANOTHER
    installed app in the body, and the app token rows on uninstall and disable
    target the app's own ``/api/apps/<self>/**`` path.
    """

    @pytest.mark.asyncio
    async def test_non_owner_cannot_register(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        body = {"name": "rogue-app", "version": "1.0.0", "displayName": "R"}
        async with TestClient(TestServer(_make_app(dashboard_user="channel-user"))) as client:
            resp = await client.post("/api/apps/register", json=body)
            assert resp.status == 403
            assert (await resp.json())["code"] == "owner_only"
        assert routes_mod.get_app("rogue-app") is None

    @pytest.mark.asyncio
    async def test_app_token_cannot_register_over_another_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.apps.manager import app_dir

        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        before = (app_dir(APP) / APP_MANIFEST_FILENAME).read_text(encoding="utf-8")
        body = {"name": APP, "version": "9.9.9", "displayName": "Other"}
        app = _make_app(app_identity="other-app", dashboard_user="other-app")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/apps/register", json=body)
            assert resp.status == 403
        after = (app_dir(APP) / APP_MANIFEST_FILENAME).read_text(encoding="utf-8")
        assert after == before

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("app_identity", "user"), [(None, "channel-user"), (APP, APP)]
    )
    async def test_uninstall_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        app_identity: str | None,
        user: str,
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        calls: list[str] = []

        def _uninstall(name: str, **_kw: Any) -> AppResult:
            calls.append(name)
            return AppResult(ok=True, name=name, message="stub")

        monkeypatch.setattr(routes_mod, "uninstall_app", _uninstall)
        app = _make_app(app_identity=app_identity, dashboard_user=user)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/apps/{APP}/uninstall", json={"purge_data": True})
            assert resp.status == 403
        assert calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("app_identity", "user"), [(None, "channel-user"), (APP, APP)]
    )
    async def test_disable_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        app_identity: str | None,
        user: str,
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        assert enable_app(APP).ok
        app = _make_app(app_identity=app_identity, dashboard_user=user)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/apps/{APP}/disable")
            assert resp.status == 403
        assert routes_mod.get_app(APP)["enabled"] is True
