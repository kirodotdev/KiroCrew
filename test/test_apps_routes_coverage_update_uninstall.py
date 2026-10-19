"""Coverage tests for the app update, uninstall and migrate-cleanup routes.

Pins the refusal, rollback and re-registration branches of the update route, the
uninstall preview and the uninstall refusals, ``DELETE /api/apps/{name}/migrate-cleanup``,
and the teardown order an update follows before it deregisters an app's resources.
``test_apps_routes_coverage.py`` holds install, registration and the app views, and
names the other route modules; the shared helpers live in
``apps_routes_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import (
    APP,
    _install,
    _make_app,
    _make_app_source,
    _setup_env,
)

import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps.manager import AppResult, disable_app, enable_app, register_external_app
from kiro_crew.apps.routes import register_app_routes

# ---------------------------------------------------------------------------
# POST /api/apps/{name}/update
# ---------------------------------------------------------------------------


class TestUpdateApp:
    @pytest.mark.asyncio
    async def test_not_installed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/ghost/update")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_self_managed_lifecycle_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        register_external_app("ext-app", "1.0.0", "External App", lifecycle="app", resources="app")
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
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
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            # Body is not JSON at all, so the source also cannot come from there.
            resp = await client.post(
                "/api/apps/ext-app/update",
                data="{",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            assert "source path required" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_live_detached_startup_hook_refuses_update(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        calls: list[tuple[str, bool]] = []

        async def _stop_retained(app_name: str, *, bounded: bool) -> bool:
            calls.append((app_name, bounded))
            return False

        monkeypatch.setattr(routes_mod, "stop_retained_startup_hooks", _stop_retained)

        def _must_not_update(*args: Any, **kwargs: Any) -> AppResult:
            raise AssertionError("update must not replace files while old code runs")

        monkeypatch.setattr(routes_mod, "update_app", _must_not_update)

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 409
            body = await resp.json()
        assert body["code"] == "startup_hook_still_running"
        assert body["retryable"] is True
        assert calls == [(APP, True)]

    @pytest.mark.asyncio
    async def test_registry_update_stops_the_backend_before_replacing_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # For an installed app ``install_from_registry`` reaches ``update_app``,
        # which replaces the live tree. On Windows that is a
        # sharing violation while the backend still holds a file open under it,
        # so the backend must be stopped (and its resources scrubbed) BEFORE the
        # install runs -- the same order the local-source branch already uses.
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        calls: list[str] = []

        async def _ok_install(name: str, **kwargs: Any) -> dict[str, Any]:
            calls.append("install")
            return {"ok": True, "name": name}

        monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
        monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
        monkeypatch.setattr(routes_mod, "install_from_registry", _ok_install)
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: calls.append("deregister"))
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: calls.append("stop"))
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append("start"))

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 200
        assert calls == ["stop", "deregister", "install", "start"], calls

    @pytest.mark.asyncio
    async def test_registry_update_failure_restores_an_enabled_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The backend is down when the install fails, so the route must bring the
        # app back itself: re-register the old resources and restart the backend.
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        calls: list[str] = []

        async def _failed_install(name: str, **kwargs: Any) -> dict[str, Any]:
            calls.append("install")
            return {"ok": False, "name": name, "error": "clone failed"}

        monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
        monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
        monkeypatch.setattr(routes_mod, "install_from_registry", _failed_install)
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: calls.append("deregister"))
        monkeypatch.setattr(routes_mod, "register_app", lambda n: calls.append("register"))
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: calls.append("stop"))
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append("start"))

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 400
            assert (await resp.json())["error"] == "clone failed"
        assert calls == ["stop", "deregister", "install", "register", "start"], calls

    @pytest.mark.asyncio
    async def test_registry_update_failure_leaves_a_disabled_app_stopped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Recovery restores what was there. A disabled app had no registered
        # resources and no backend, and ``register_app`` does not consult
        # ``enabled``, so re-registering here would publish its agents, skills,
        # MCP servers and crons with the app still shown as disabled.
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        disable_app(APP)
        calls: list[str] = []

        async def _failed_install(name: str, **kwargs: Any) -> dict[str, Any]:
            calls.append("install")
            return {"ok": False, "name": name, "error": "clone failed"}

        monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
        monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
        monkeypatch.setattr(routes_mod, "install_from_registry", _failed_install)
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: calls.append("deregister"))
        monkeypatch.setattr(routes_mod, "register_app", lambda n: calls.append("register"))
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: calls.append("stop"))
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append("start"))

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 400
        assert calls == ["stop", "deregister", "install"], calls

    @pytest.mark.asyncio
    async def test_local_update_failure_leaves_a_disabled_app_unregistered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same recovery rule on the local-source path: nothing is published for
        # an app the user has disabled.
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        disable_app(APP)
        calls: list[str] = []

        monkeypatch.setattr(
            routes_mod,
            "update_app",
            lambda source, expected_name=None: AppResult(
                ok=False, name=APP, error="source manifest mismatch"
            ),
        )
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: calls.append("deregister"))
        monkeypatch.setattr(routes_mod, "register_app", lambda n: calls.append("register"))
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: calls.append("stop"))
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append("start"))

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 400
        assert calls == ["stop", "deregister"], calls

    @pytest.mark.asyncio
    async def test_registry_update_refuses_before_stopping_while_startup_hook_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The stop runs before ``install_from_registry``, so the route needs its
        # own bounded preflight ahead of it: a retryable refusal must leave app
        # state untouched (no stop, no scrub, no install), as on the local path.
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        calls: list[str] = []

        async def _stop_retained(app_name: str, *, bounded: bool) -> bool:
            calls.append(f"preflight:{bounded}")
            return False

        monkeypatch.setattr(routes_mod, "stop_retained_startup_hooks", _stop_retained)

        async def _must_not_install(name: str, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("files must not be replaced while old code runs")

        monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
        monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
        monkeypatch.setattr(routes_mod, "install_from_registry", _must_not_install)
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: calls.append("deregister"))
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: calls.append("stop"))

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 409
            body = await resp.json()
        assert body["code"] == "startup_hook_still_running"
        assert body["retryable"] is True
        assert calls == ["preflight:True"], calls

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
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: calls.append("deregister"))
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: calls.append("stop"))
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append("start"))

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 200
            data = await resp.json()
        assert data["ok"] is True
        assert "registration" in data
        # The backend is stopped BEFORE its resources are scrubbed, so the health
        # watch has lost its tracking record and cannot re-register the old
        # manifest's MCP servers in between (see app-kit-platform §17); both run
        # before the re-install replaces the files.
        assert calls == ["stop", "deregister", "start"]

    @pytest.mark.asyncio
    async def test_registry_update_that_widens_session_approval_does_not_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``update_app`` drops ``enabled`` when the new version newly asks for
        # session control. The route must read THAT state, not its pre-update
        # snapshot, or it registers resources and starts a backend for an app the
        # user has not re-consented to.
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        calls: list[str] = []

        async def _reconsent_install(name: str, **kwargs: Any) -> dict[str, Any]:
            disable_app(name)  # what update_app does on a widened grant
            return {"ok": True, "name": name, "notice": "session_approval_reconsent"}

        monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
        monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
        monkeypatch.setattr(routes_mod, "install_from_registry", _reconsent_install)
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: calls.append("deregister"))
        monkeypatch.setattr(routes_mod, "register_app", lambda n: calls.append("register"))
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: calls.append("stop"))
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append("start"))

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 200
            data = await resp.json()
        assert data["ok"] is True
        assert data["notice"] == "session_approval_reconsent"
        assert "registration" not in data
        assert calls == ["stop", "deregister"]

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

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 400
            assert (await resp.json())["error"] == "source manifest mismatch"
        # The rollback re-registered what the failed update had torn down.
        assert registered == [APP]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["local", "registry"])
    @pytest.mark.parametrize("broken", ["register", "start"])
    async def test_failed_update_restore_failure_still_audits_and_returns_400(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        source: str,
        broken: str,
    ) -> None:
        # The cause of a failed update (disk full, no free port) often breaks the
        # restore too; that must not turn the 400 into a bare 500 or skip the audit.
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        audit = MagicMock()
        monkeypatch.setattr(routes_mod, "sel", lambda: audit)

        def _raise(exc: Exception) -> Any:
            def _fn(*args: Any, **kwargs: Any) -> Any:
                raise exc

            return _fn

        if source == "registry":

            async def _failed_install(name: str, **kwargs: Any) -> dict[str, Any]:
                return {"ok": False, "name": name, "error": "clone failed"}

            monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
            monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
            monkeypatch.setattr(routes_mod, "install_from_registry", _failed_install)
            error = "clone failed"
        else:
            error = "source manifest mismatch"
            monkeypatch.setattr(
                routes_mod,
                "update_app",
                lambda source, expected_name=None: AppResult(ok=False, name=APP, error=error),
            )
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: None)
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        if broken == "register":
            monkeypatch.setattr(routes_mod, "register_app", _raise(OSError("disk full")))
            monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        else:
            monkeypatch.setattr(routes_mod, "register_app", lambda n: SimpleNamespace(to_dict=dict))
            monkeypatch.setattr(
                routes_mod, "start_app_backend", _raise(RuntimeError("No free ports"))
            )

        with caplog.at_level(logging.WARNING, logger=routes_mod.logger.name):
            async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
                resp = await client.post(f"/api/apps/{APP}/update", json={})
                assert resp.status == 400
                assert (await resp.json())["error"] == error
        audit.log_api_access.assert_any_call(
            caller="dashboard",
            operation="app_update",
            outcome="failed",
            resources=APP,
            error=error,
        )
        assert any(
            r.levelno == logging.WARNING and r.exc_info and APP in r.getMessage()
            for r in caplog.records
        ), caplog.records

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
            lambda source, expected_name=None: AppResult(ok=True, name=APP, message="updated"),
        )
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: None)
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 200
            data = await resp.json()
        assert data["ok"] is True
        assert "registration" in data

    @pytest.mark.asyncio
    async def test_update_registers_crons_with_the_running_scheduler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An update promotes the rewritten app-crons.json into the live scheduler.

        The bridge re-applies a changed manifest entry to a job it already
        registered, but it only runs where it is called -- so an update that did
        not call it left every existing job on its old values until the next
        gateway start.
        """
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        monkeypatch.setattr(
            routes_mod,
            "update_app",
            lambda source, expected_name=None: AppResult(ok=True, name=APP, message="updated"),
        )
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: None)
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        resync = AsyncMock(return_value=[])
        monkeypatch.setattr(routes_mod, "register_app_crons_with_service", resync)

        app = _make_app(dashboard_user="owner")
        scheduler = object()
        app["state"].crons = scheduler
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 200

        resync.assert_awaited_once_with(APP, scheduler)

    @pytest.mark.asyncio
    async def test_update_without_a_scheduler_registers_no_crons(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        monkeypatch.setattr(
            routes_mod,
            "update_app",
            lambda source, expected_name=None: AppResult(ok=True, name=APP, message="updated"),
        )
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: None)
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        resync = AsyncMock(return_value=[])
        monkeypatch.setattr(routes_mod, "register_app_crons_with_service", resync)

        # The route test state carries no ``crons``: nothing to register with.
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 200

        resync.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_app_token_cannot_replace_repository_bound_code_from_local_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The app-owned update API cannot turn repo A's grant into repo B code."""
        home = _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)

        from kiro_crew.apps.manager import get_app, set_app_provenance
        from kiro_crew.config.loader import _invalidate_config_cache
        from kiro_crew.dashboard.token_auth import generate_token, token_auth_middleware

        reviewed = "https://clone.example.test/Owner/reviewed-app"
        assert set_app_provenance(
            APP,
            source=f"registry:{APP}",
            url=reviewed,
        )
        (home / "config.json").write_text(
            json.dumps(
                {
                    "agent": {
                        "apps_allow_third_party": False,
                        "apps_trusted": [APP],
                        "apps_trusted_repositories": {APP: reviewed},
                    }
                }
            ),
            encoding="utf-8",
        )
        _invalidate_config_cache()
        enabled = enable_app(APP)
        assert enabled.ok, enabled.error

        attacker_source = _make_app_source(
            tmp_path / "attacker", version="9.9.9", displayName="Rebound Code"
        )
        (attacker_source / "attacker.py").write_text(
            "raise RuntimeError('repository binding bypassed')\n", encoding="utf-8"
        )

        # This is a real app-claim token, not a handler-only identity stub. The
        # auth middleware deliberately permits an app token on its own
        # /api/apps/<name>/ namespace, so the lifecycle handler must enforce the
        # repository boundary itself.
        token = generate_token(APP, ttl_seconds=300, app=APP)
        app = web.Application(middlewares=[token_auth_middleware()])
        app["state"] = SimpleNamespace(owner_id="owner")
        register_app_routes(app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                f"/api/apps/{APP}/update",
                params={"token": token},
                json={"source": str(attacker_source)},
            )
            assert resp.status == 403
            body = await resp.json()

        assert body["code"] == "owner_only"
        installed = get_app(APP)
        assert installed is not None
        assert installed["version"] == "1.0.0"
        assert installed["sourceUrl"] == reviewed
        assert not (home / "apps" / APP / "attacker.py").exists()


# ---------------------------------------------------------------------------
# Uninstall preview + uninstall refusals
# ---------------------------------------------------------------------------


class TestUninstallPreview:
    """``GET /api/apps/{name}/uninstall/preview`` driven over the router.

    The requests go through a real aiohttp test client against an app built
    by ``register_app_routes``, so every assertion here depends on the route
    registration itself: removing the ``add_get`` turns each of these into a
    404 failure.
    """

    @staticmethod
    async def _preview(name: str) -> tuple[int, dict[str, Any]]:
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get(f"/api/apps/{name}/uninstall/preview")
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_not_installed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
                "permissions": {"cron": True},
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

    @pytest.mark.asyncio
    async def test_app_tokens_are_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An app token reaches its own ``/api/apps/{name}/**`` namespace via
        ``_app_owns_path``, but the preview discloses sibling app names in the
        shared-dependency classification -- so app-identity requests get 403,
        even for the app's own preview."""
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        async with TestClient(TestServer(_make_app(app_identity=APP))) as client:
            resp = await client.get(f"/api/apps/{APP}/uninstall/preview")
            assert resp.status == 403


class TestUninstallRefusals:
    @pytest.mark.asyncio
    async def test_not_installed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
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
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/locked-app/uninstall")
            assert resp.status == 400
            assert "lifecycle=locked" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_live_detached_startup_hook_refuses_uninstall(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        calls: list[tuple[str, bool]] = []

        async def _stop_retained(app_name: str, *, bounded: bool) -> bool:
            calls.append((app_name, bounded))
            return False

        monkeypatch.setattr(routes_mod, "stop_retained_startup_hooks", _stop_retained)

        def _must_not_uninstall(*args: Any, **kwargs: Any) -> AppResult:
            raise AssertionError("uninstall must not delete files while old code runs")

        monkeypatch.setattr(routes_mod, "uninstall_app", _must_not_uninstall)

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/uninstall", json={})
            assert resp.status == 409
            body = await resp.json()
        assert body["code"] == "startup_hook_still_running"
        assert body["retryable"] is True
        assert calls == [(APP, True)]

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

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
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

        monkeypatch.setattr(routes_mod, "classify_and_clean_for_uninstall", _classify)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/uninstall", json={"keep_dependencies": True})
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
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
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
            lambda name, keep_data=True: AppResult(ok=False, name=name, error="permission denied"),
        )
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/uninstall", json={})
            assert resp.status == 400
            assert (await resp.json())["error"] == "permission denied"


# ---------------------------------------------------------------------------
# DELETE /api/apps/{name}/migrate-cleanup
# ---------------------------------------------------------------------------


class TestMigrateCleanup:

    def _retired(self, tmp_path, monkeypatch):
        from kiro_crew.apps.manager import InstalledApp, _write_installed, app_dir

        home = _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, setup={"onUninstall": "remove-data.sh"})
        _write_installed(APP, InstalledApp(name=APP, origin="builtin", lifecycle="locked"))
        dest = app_dir(APP)
        (dest / "data" / "keep.txt").write_text("keep me", encoding="utf-8")
        for leaf in (".kirocrew-deps", ".kirocrew-deps-prior", ".kirocrew-deps-staging-1-12345678"):
            (dest / "data" / leaf).mkdir()
        (home / "config.json").write_text(
            json.dumps({"agent": {"apps_trusted": [APP], "apps_trusted_local": [APP]}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            routes_mod, "_stop_backend_and_observe", AsyncMock(return_value=(None, True))
        )
        monkeypatch.setattr(routes_mod, "_run_lifecycle_script", AsyncMock())
        monkeypatch.setattr(
            routes_mod,
            "_deregister_app_off_loop",
            AsyncMock(return_value=routes_mod.RegistrationResult()),
        )
        return home, dest

    @pytest.mark.asyncio
    async def test_dependency_classification_runs_off_the_event_loop(self, tmp_path, monkeypatch):
        """The ledger classify-and-clean takes a blocking file lock; it must not run on the loop."""
        _, dest = self._retired(tmp_path, monkeypatch)
        seen: list[tuple[threading.Thread, str, tuple[str, ...]]] = []

        def record(name, declared, *, keep_specific):
            seen.append((threading.current_thread(), name, tuple(keep_specific)))
            return {"removable": [], "shared": [], "userInstalled": []}

        monkeypatch.setattr(routes_mod, "classify_and_clean_for_uninstall", record)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            loop_thread = threading.current_thread()
            resp = await client.delete(f"/api/apps/{APP}/migrate-cleanup")
            assert resp.status == 200, await resp.text()
        assert [(n, k) for _, n, k in seen] == [(APP, ())]
        assert seen[0][0] is not loop_thread, "classification must be offloaded from the loop"
        assert {p.name for p in dest.iterdir()} == {"data"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "warning", ["none", "flush", "discard-and-flush", "backend", "deregister"]
    )
    async def test_delegates_to_uninstall_preserving_only_user_data(
        self, tmp_path, monkeypatch, warning
    ):
        home, dest = self._retired(tmp_path, monkeypatch)
        app = _make_app(dashboard_user="owner")
        audit = MagicMock()
        monkeypatch.setattr(routes_mod, "sel", lambda: audit)
        sessions = SimpleNamespace(
            mapped_session_keys=lambda: {"owned"},
            session_keys=lambda: set(),
            discard_conversation=AsyncMock(
                side_effect=OSError("discard") if warning == "discard-and-flush" else None
            ),
            suppress_replay_persistently=MagicMock(),
            aflush=AsyncMock(side_effect=OSError("flush") if "flush" in warning else None),
        )
        app["state"].sessions = sessions
        monkeypatch.setattr(routes_mod, "app_conversation_keys", lambda *a, **k: ["owned"])
        if warning == "backend":
            routes_mod._stop_backend_and_observe.return_value = (12345, True)
        if warning == "deregister":
            routes_mod._deregister_app_off_loop.return_value.errors = [
                "cleanup failed: https://user:secret@example.test/path"
            ]
        from kiro_crew.apps import manager

        monkeypatch.setattr(manager, "_orphaned_builtins_cache", {APP})
        async with TestClient(TestServer(app)) as client:
            assert (await client.post(f"/api/apps/{APP}/uninstall")).status == 400
            resp = await client.delete(f"/api/apps/{APP}/migrate-cleanup")
            body = await resp.json()
        assert resp.status == 200, body
        assert body["ok"] and bool(body.get("notice")) == (warning != "none")
        assert "user:secret" not in body.get("notice", "")
        assert {p.name for p in dest.iterdir()} == {"data"}
        assert (dest / "data" / "keep.txt").read_text(encoding="utf-8") == "keep me"
        assert {p.name for p in (dest / "data").iterdir()} == {"keep.txt", ".kirocrew-deps.lock"}
        assert manager._orphaned_builtins_cache is None
        agent = json.loads((home / "config.json").read_text(encoding="utf-8"))["agent"]
        assert APP not in agent["apps_trusted"] and APP not in agent["apps_trusted_local"]
        routes_mod._run_lifecycle_script.assert_not_awaited()
        routes_mod._stop_backend_and_observe.assert_awaited_once_with(APP)
        sessions.discard_conversation.assert_awaited_once_with("owned", replay=False)
        sessions.aflush.assert_awaited_once()
        audit.log_api_access.assert_any_call(
            caller="dashboard", operation="app_migrate_cleanup", outcome="completed", resources=APP
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("refusal", ["changed", "link", "data-link", "data-file", "shipped"])
    async def test_ineligible_builtin_is_untouched(self, tmp_path, monkeypatch, refusal):
        from contextlib import asynccontextmanager

        from kiro_crew.apps import manager
        from kiro_crew.testing.links import make_dir_link

        home, dest = self._retired(tmp_path, monkeypatch)
        before = (dest / "installed.json").read_bytes()
        config_before = (home / "config.json").read_bytes()
        target = None
        if refusal == "link":
            target = dest.rename(tmp_path / "linked-app")
            make_dir_link(dest, target)
        elif refusal == "data-link":
            target = (dest / "data").rename(tmp_path / "linked-data")
            make_dir_link(dest / "data", target)
            target_before = sorted(p.name for p in target.iterdir())
        elif refusal == "data-file":
            shutil.rmtree(dest / "data")
            (dest / "data").write_text("not a directory", encoding="utf-8")
        elif refusal == "shipped":
            monkeypatch.setattr(manager, "detect_orphaned_builtins", lambda **k: set())
        else:
            held = False

            @asynccontextmanager
            async def lock(name):
                nonlocal held
                held = True
                yield

            def predicate(name):
                with pytest.raises(RuntimeError):
                    asyncio.get_running_loop()
                return not held

            monkeypatch.setattr(routes_mod, "app_lifecycle_lock", lock)
            monkeypatch.setattr(manager, "migrated_builtin_cleanup_applies", predicate)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.delete(f"/api/apps/{APP}/migrate-cleanup")
            body = await resp.json()
        assert resp.status == 400 and body["code"] == "not_orphaned" and not body["ok"]
        assert (dest / "installed.json").read_bytes() == before
        assert (home / "config.json").read_bytes() == config_before
        routes_mod._stop_backend_and_observe.assert_not_awaited()
        routes_mod._run_lifecycle_script.assert_not_awaited()
        routes_mod._deregister_app_off_loop.assert_not_awaited()
        if refusal == "data-link":
            assert (dest / "data").is_symlink() or manager.is_link_or_junction(dest / "data")
            assert sorted(p.name for p in target.iterdir()) == target_before
            assert (target / "keep.txt").read_text(encoding="utf-8") == "keep me"
        if refusal == "data-file":
            assert (dest / "data").read_text(encoding="utf-8") == "not a directory"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("installed", [False, True])
    async def test_idempotent_without_touching_the_successor(
        self, tmp_path, monkeypatch, installed
    ):
        home = _setup_env(tmp_path, monkeypatch)
        if installed:
            _install(tmp_path)
        before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.delete(f"/api/apps/{APP}/migrate-cleanup")
            body = await resp.json()
        assert resp.status == 200 and body["ok"]
        assert ("already migrated" if installed else "nothing to clean up") in body["message"]
        assert {p: p.read_bytes() for p in home.rglob("*") if p.is_file()} == before

    @pytest.mark.asyncio
    @pytest.mark.parametrize("actor", ["app", "non-owner"])
    async def test_requires_owner_not_app_token(self, tmp_path, monkeypatch, actor):
        _, dest = self._retired(tmp_path, monkeypatch)
        app = _make_app(app_identity=APP if actor == "app" else None, dashboard_user="other")
        async with TestClient(TestServer(app)) as client:
            resp = await client.delete(f"/api/apps/{APP}/migrate-cleanup")
            body = await resp.json()
        assert resp.status == 403
        if actor == "app":
            assert body["code"] == "app_token_forbidden"
        assert (dest / "installed.json").is_file()
        routes_mod._stop_backend_and_observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_stops_the_backend_before_deregistering_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Teardown order is load-bearing, not cosmetic.

    `stop_app_backend` pops the tracking record, which is what stops the health watch
    from reconciling MCP for the app. Deregistering first leaves a window in which a
    recovering backend re-registers the OLD manifest's servers, so entries the update
    removed survive it. Uninstall and the disable rollback already stop first; these are
    the paths that did not.
    """
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path)
    order: list[str] = []

    async def _fake_deregister(name: str):
        order.append("deregister")
        return SimpleNamespace(to_dict=lambda: {})

    monkeypatch.setattr(routes_mod, "_deregister_app_off_loop", _fake_deregister)
    monkeypatch.setattr(routes_mod, "stop_app_backend", lambda name: order.append("stop") or True)
    monkeypatch.setattr(
        routes_mod, "update_app", lambda *a, **k: {"success": False, "error": "stop here"}
    )

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        await client.post(f"/api/apps/{APP}/update", json={"source": str(tmp_path / "src")})

    assert order[:2] == [
        "stop",
        "deregister",
    ], f"backend must be stopped before its resources are scrubbed, got {order}"
