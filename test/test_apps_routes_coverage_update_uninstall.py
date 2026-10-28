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
from kiro_crew.apps.backend import clear_backend_notice
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

    @pytest.mark.asyncio
    async def test_provenance_invalidation_reports_confirmed_vs_unconfirmed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The update seam durably REVOKES the pre-update spawn record before any
        restart (retaining the revoked tombstone, not deleting it) and reports whether
        the revoke is CONFIRMED. A confirmed revoke returns True (restart allowed); an
        unconfirmed one (ENOSPC/EDQUOT: revoke raises) returns False so the caller does
        NOT restart and adopt the stale-row survivor as the new code."""

        monkeypatch.setattr(routes_mod, "revoke_backend_provenance", lambda n: {"pid": 1})
        monkeypatch.setattr(routes_mod, "quarantine_backend", lambda n, reason: None)
        assert await routes_mod._invalidate_provenance_before_restart("app") is True

        def _raise(n: str) -> None:
            raise routes_mod.PidfileRevokeFailed("disk full")

        monkeypatch.setattr(routes_mod, "revoke_backend_provenance", _raise)
        assert await routes_mod._invalidate_provenance_before_restart("app") is False

    @pytest.mark.asyncio
    async def test_registry_update_committed_but_provenance_write_fails_keeps_the_row_revoked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression: a registry update can land on disk (durable_success) and THEN
        # fail a post-commit step (set_app_provenance cannot persist the new row),
        # so install_from_registry returns ok=False WITH committed=True. The route
        # must NOT treat that as a rollback: restoring the pre-update spawn record
        # would let the recovery adopt an old-code survivor as the updated backend.
        # The row stays revoked and the backend is not restarted (fail-closed). It is
        # also not a clean success: the failure is surfaced to the UI as a coded 500
        # with the error, and a notice names the provenance write as the cause.
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        restored: list[str] = []
        started: list[str] = []
        notices: list[str] = []

        async def _committed_then_failed(name: str, **kwargs: Any) -> dict[str, Any]:
            # ok=False but the install already committed; the new code is in place.
            return {"ok": False, "name": name, "error": "disk full", "committed": True}

        async def _spy_restore(name: str, preserved_row: Any) -> None:
            restored.append(name)

        async def _register(name: str) -> Any:
            return SimpleNamespace(to_dict=lambda: {"name": name})

        monkeypatch.setattr(routes_mod, "is_registry_source", lambda s: True)
        monkeypatch.setattr(routes_mod, "registry_name_from_source", lambda s: APP)
        monkeypatch.setattr(routes_mod, "install_from_registry", _committed_then_failed)
        monkeypatch.setattr(routes_mod, "_restore_provenance_after_failed_update", _spy_restore)
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: None)
        monkeypatch.setattr(routes_mod, "_register_app_off_loop", _register)
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: started.append(n))
        monkeypatch.setattr(routes_mod, "record_backend_notice", lambda n, msg: notices.append(msg))

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            # Not a clean success: the update's files landed but its backend could not
            # be registered, so the UI gets a coded 500 carrying the error.
            assert resp.status == 500
            body = await resp.json()
            assert body["ok"] is False
            assert body["error"] == "disk full"
            assert body["code"] == "provenance_not_recorded"

        # The pre-update spawn record is NOT restored over committed new code, and
        # the backend is NOT restarted — nothing may be adopted against the stale row.
        assert restored == [], "a committed update must not restore the old spawn record"
        assert started == [], "a committed-but-provenance-failed update must not restart"
        # A notice is recorded that names the failed provenance write (not a port-holder).
        assert notices, "a committed-but-provenance-failed update must record a notice"
        assert "could not be registered" in notices[-1]

    @pytest.mark.asyncio
    async def test_local_update_refuses_when_pre_replacement_revoke_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT F2: the durable revoke happens BEFORE update_app replaces files. When it
        cannot be confirmed (ENOSPC/EDQUOT), the update is REFUSED — update_app is
        never called, so no new code is committed over a row that would still vouch for
        an old-code survivor, and the response is a 400."""
        from kiro_crew.apps.backend import PidfileRevokeFailed

        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        replaced: list[str] = []

        def _boom_revoke(_name: str) -> None:
            raise PidfileRevokeFailed("disk full")

        monkeypatch.setattr(routes_mod, "revoke_backend_provenance", _boom_revoke)
        monkeypatch.setattr(
            routes_mod,
            "update_app",
            lambda source, expected_name=None: replaced.append(source)
            or AppResult(ok=True, name=APP),
        )
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: None)
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 400
            data = await resp.json()
        assert data["ok"] is False
        assert "durably revoked" in data["error"]
        assert replaced == [], "update_app must NOT replace files when the revoke could not persist"

    @pytest.mark.asyncio
    async def test_update_holds_the_lifecycle_flock_across_revoke_and_replacement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT 6.1: the stop → revoke → replace → final-revoke span runs under the
        cross-process backend lifecycle flock, so a racing spawn's ``_record_app_pid``
        cannot overwrite the revoked tombstone — and the flock is RELEASED before the
        restart (which re-acquires it in ``start_app_backend``, else a self-deadlock).
        """
        import contextlib as _ctxlib

        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        events: list[str] = []

        @_ctxlib.contextmanager
        def _recording_flock(name: str):
            events.append(f"flock-enter:{name}")
            try:
                yield
            finally:
                events.append(f"flock-exit:{name}")

        monkeypatch.setattr(routes_mod, "app_backend_lifecycle_flock", _recording_flock)
        monkeypatch.setattr(
            routes_mod,
            "revoke_backend_provenance",
            lambda n: events.append("revoke"),
        )
        monkeypatch.setattr(
            routes_mod,
            "update_app",
            lambda source, expected_name=None: events.append("replace")
            or AppResult(ok=True, name=APP),
        )
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: events.append("stop"))
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: events.append("restart"))
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: None)
        monkeypatch.setattr(routes_mod, "register_app", lambda n: None)

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 200, await resp.text()

        # The flock wraps stop/revoke/replace/revoke; both revocations and the file
        # replacement land BETWEEN enter and exit, and the restart lands AFTER exit.
        assert f"flock-enter:{APP}" in events and f"flock-exit:{APP}" in events, events
        enter = events.index(f"flock-enter:{APP}")
        exit_ = events.index(f"flock-exit:{APP}")
        protected = events[enter + 1 : exit_]
        assert "stop" in protected, events
        assert "replace" in protected, events
        assert protected.count("revoke") >= 1, events
        # Released before the restart — holding it across start_app_backend self-deadlocks.
        assert "restart" in events[exit_ + 1 :], events

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", ["revoke", "replace"])
    async def test_a_failed_update_restores_the_row_before_releasing_the_flock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
    ) -> None:
        """GPT 6.1: once the flock is released, a CLI uninstall queued on it revokes
        this spawn's row and removes the app at once. A rollback restore landing after
        that would clear the uninstall's revocation, so the restore runs while the
        update still holds the flock, and the post-release rollback does not repeat it."""
        import contextlib as _ctxlib

        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        events: list[str] = []
        row = {"pid": 7, "start_time": "st-7", "port": 9001, "spawn_instance": "sp-7"}

        @_ctxlib.contextmanager
        def _recording_flock(name: str):
            events.append("flock-enter")
            try:
                yield
            finally:
                events.append("flock-exit")

        def _revoke(_name: str) -> None:
            if failure == "revoke":
                raise routes_mod.PidfileRevokeFailed("disk full")

        monkeypatch.setattr(routes_mod, "app_backend_lifecycle_flock", _recording_flock)
        monkeypatch.setattr(routes_mod, "capture_backend_provenance", lambda _n: dict(row))
        monkeypatch.setattr(routes_mod, "revoke_backend_provenance", _revoke)
        monkeypatch.setattr(
            routes_mod,
            "update_app",
            lambda source, expected_name=None: AppResult(ok=False, error="copy failed"),
        )
        monkeypatch.setattr(
            routes_mod, "_unrevoke_app_pid", lambda _n, _row: events.append("restore")
        )
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: events.append("restart"))
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: None)
        monkeypatch.setattr(routes_mod, "register_app", lambda n: None)

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/update", json={})
            assert resp.status == 400, await resp.text()

        assert events.count("restore") == 1, events
        assert events.index("restore") < events.index("flock-exit"), events
        assert "restart" in events[events.index("flock-exit") + 1 :], events

    @pytest.mark.asyncio
    async def test_an_update_that_skips_its_restart_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unconfirmed provenance drop skips the restart (it could adopt the old
        version). The update still succeeds, and the user is told the backend was
        not restarted and what to do -- on the response and on the app's status."""

        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        restarts: list[str] = []
        monkeypatch.setattr(routes_mod, "revoke_backend_provenance", lambda n: None)

        async def _unconfirmed(_name: str) -> bool:
            return False

        monkeypatch.setattr(routes_mod, "_invalidate_provenance_before_restart", _unconfirmed)
        monkeypatch.setattr(
            routes_mod,
            "update_app",
            lambda source, expected_name=None: AppResult(ok=True, name=APP),
        )
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: None)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: restarts.append(n))
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: None)
        monkeypatch.setattr(routes_mod, "register_app", lambda n: None)
        clear_backend_notice(APP)
        try:
            async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
                resp = await client.post(f"/api/apps/{APP}/update", json={})
                assert resp.status == 200, await resp.text()
                body = await resp.json()
                status = await client.get(f"/api/apps/{APP}")
                detail = await status.json()
        finally:
            clear_backend_notice(APP)
        assert restarts == []
        assert "was not restarted" in body["backend_notice"]
        assert detail["backend_notice"] == body["backend_notice"]
        # The machine-read token field is left alone.
        assert body.get("notice") != body["backend_notice"]


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

    @pytest.mark.asyncio
    async def test_provenance_deleted_only_at_the_commit_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Provenance invalidation lives INSIDE uninstall_app (the shared transaction
        the dashboard handler and the offline CLI both go through), not in the handler.
        The handler must NOT separately delete the spawn record: a second delete would
        remove the revoked tombstone uninstall_app leaves as its durable fence. So on a
        successful uninstall the handler calls uninstall_app and does not touch the
        provenance record itself."""

        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        order: list[str] = []

        def _real_removal(name: str, keep_data: bool = False) -> AppResult:
            order.append("uninstall_app")
            return AppResult(ok=True)

        monkeypatch.setattr(routes_mod, "uninstall_app", _real_removal)

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/uninstall", json={})
            assert resp.status == 200
        # The handler delegates removal+invalidation to uninstall_app and does NOT
        # run its own provenance delete (which would wipe the durable tombstone).
        assert order == ["uninstall_app"]

    @pytest.mark.asyncio
    async def test_failed_uninstall_never_touches_provenance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed uninstall_app leaves the app installed and returns 400; invalidation
        lives inside uninstall_app (not the handler), so a failed removal leaves the
        record untouched and the app keeps attributing its backend."""

        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)

        def _fail_removal(*args: Any, **kwargs: Any) -> AppResult:
            return AppResult(ok=False, error="rmtree failed")

        monkeypatch.setattr(routes_mod, "uninstall_app", _fail_removal)

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/uninstall", json={})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_interruption_before_commit_leaves_the_provenance_row_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Crash-safety: an interruption anywhere before the commit (here onUninstall
        raises) never reaches uninstall_app, so the app and its record stay intact and
        the next-boot reap handles the orphan. No window where the row is gone while
        the app survives."""

        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, setup={"onUninstall": "teardown.sh"})

        async def _blow_up_mid_window(*args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("gateway interrupted during onUninstall")

        monkeypatch.setattr(routes_mod, "_run_lifecycle_script", _blow_up_mid_window)

        def _uninstall_must_not_run(*args: Any, **kwargs: Any) -> AppResult:
            raise AssertionError("uninstall_app ran after the mid-window interruption")

        monkeypatch.setattr(routes_mod, "uninstall_app", _uninstall_must_not_run)

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/uninstall", json={})
            assert resp.status == 500

    @pytest.mark.asyncio
    async def test_unconfirmed_delete_at_commit_is_logged_not_aborted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Provenance invalidation lives INSIDE uninstall_app, so the handler does not
        delete the spawn record itself (that would wipe the revoked tombstone the
        shared transaction leaves as its fence). The handler therefore never calls
        quarantine_backend; the uninstall's success is governed by uninstall_app's own
        result (a durable-revoke failure aborts THERE, covered by the manager tests).
        Here a successful uninstall_app yields 200 and the handler touches the
        quarantine helper not at all."""

        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)

        monkeypatch.setattr(routes_mod, "uninstall_app", lambda *a, **k: AppResult(ok=True))

        quarantined: list[str] = []
        monkeypatch.setattr(
            routes_mod, "quarantine_backend", lambda name, reason: quarantined.append(name)
        )

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/uninstall", json={})
            assert resp.status == 200
            # The handler delegates invalidation to uninstall_app and does not run its
            # own quarantine.
            assert quarantined == []


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


class TestInvalidateProvenanceBeforeRestart:
    """GPT 6.1: a successful update durably REVOKES the pre-update spawn record and
    RETAINS the revoked tombstone (it is not deleted) — a fresh spawn's
    _record_app_pid overwrites it. Retaining it closes the resurrection race a delete
    would reopen (a stop racing the update could restore an unrevoked row). When the
    durable revoke cannot be confirmed (ENOSPC/EDQUOT), the update must NOT restart AND
    must quarantine the app so a later enable does not adopt the old-code survivor the
    stale row still vouches for."""

    @pytest.mark.asyncio
    async def test_confirmed_revoke_returns_true_and_does_not_quarantine_or_delete(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        quarantined: list[str] = []
        revoked: list[str] = []
        monkeypatch.setattr(
            routes_mod, "revoke_backend_provenance", lambda name: revoked.append(name)
        )
        monkeypatch.setattr(
            routes_mod, "quarantine_backend", lambda name, reason: quarantined.append(name)
        )
        ok = await routes_mod._invalidate_provenance_before_restart("ext")
        assert ok is True
        # Tombstone retained: a confirmed revoke is enough; nothing is deleted and the
        # app is not quarantined.
        assert revoked == ["ext"]
        assert quarantined == []

    @pytest.mark.asyncio
    async def test_unconfirmed_revoke_returns_false_and_quarantines(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        quarantined: list[tuple[str, str]] = []

        def _raise(_name: str) -> None:
            raise routes_mod.PidfileRevokeFailed("ENOSPC")

        monkeypatch.setattr(routes_mod, "revoke_backend_provenance", _raise)
        monkeypatch.setattr(
            routes_mod,
            "quarantine_backend",
            lambda name, reason: quarantined.append((name, reason)),
        )
        ok = await routes_mod._invalidate_provenance_before_restart("ext")
        assert ok is False
        assert len(quarantined) == 1 and quarantined[0][0] == "ext"

    @pytest.mark.asyncio
    async def test_successful_invalidation_retains_the_revoked_tombstone_on_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT 6.1: a successful update invalidation must LEAVE the revoked row on disk
        (the durable fence), not delete it. A delete would let a stop racing the update
        restore an unrevoked row and a later enable adopt the pre-update old-code child.
        After invalidation the row is still present AND revoked, so _restore_app_pid
        refuses (name present) and _adoption_provenance refuses (revoked)."""
        _setup_env(tmp_path, monkeypatch)
        from kiro_crew.apps.backend_runtime import pidfile as pf

        pf._write_pidfile(
            {"ext": {"pid": 31337, "start_time": "st-31337", "port": 9321, "spawn_instance": "pre"}}
        )
        ok = await routes_mod._invalidate_provenance_before_restart("ext")
        assert ok is True
        live = pf._read_pidfile().get("ext")
        assert live is not None, "the update must NOT delete the row — the tombstone is the fence"
        assert live.get("revoked") is True, live
        # A racing stop's later restore cannot resurrect an unrevoked row.
        pf._restore_app_pid(
            "ext", {"pid": 31337, "start_time": "st-31337", "port": 9321, "spawn_instance": "pre"}
        )
        attributed, _ = pf._adoption_provenance("ext", [31337])
        assert attributed is False


class TestRestoreAppAfterFailedUpdate:
    """GPT F1: a failed update's rollback must restore the pre-stop spawn row
    BEFORE the may-run check, so a DISABLED app whose update failed still keeps the
    recovery row for a surviving port-holder.

    The update's stop forgets a tracked backend's row. If the app is disabled, the
    rollback returns without restarting -- but a detached survivor can still be
    holding the port, and the row is the only handle a later re-enable has to
    re-attribute it. Restoring only after the may-run check (as the code did before
    this fix) skipped the restore entirely for a disabled app, stranding the
    survivor.
    """

    @pytest.mark.asyncio
    async def test_disabled_app_still_restores_the_preserved_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        preserved = {"pid": 4242, "start_time": "st-4242", "port": 9000, "spawn_instance": "sp"}
        restored: list[tuple[str, dict[str, Any]]] = []
        registered: list[str] = []
        started: list[str] = []

        monkeypatch.setattr(
            routes_mod, "_unrevoke_app_pid", lambda n, row: restored.append((n, row))
        )
        # The rollback restored the app's record, so the row is un-revoked.
        monkeypatch.setattr(routes_mod, "get_app", lambda n: {"name": n})

        async def _may_run(_name: str) -> bool:
            return False  # app is DISABLED -> early return after the restore

        async def _register(name: str) -> None:
            registered.append(name)

        monkeypatch.setattr(routes_mod, "_app_may_run_after_install", _may_run)
        monkeypatch.setattr(routes_mod, "_register_app_off_loop", _register)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda name: started.append(name))

        await routes_mod._restore_provenance_after_failed_update("ext", preserved)
        await routes_mod._restore_app_after_failed_update("ext")

        # The row is restored even though the app stays disabled...
        assert restored == [("ext", preserved)]
        # ...and the disabled app is NOT re-registered or restarted.
        assert registered == []
        assert started == []

    @pytest.mark.asyncio
    async def test_enabled_app_restores_then_restarts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The enabled path still restores the row and goes on to re-register and
        restart -- the restore moving ahead of the may-run check must not change the
        enabled behaviour."""

        preserved = {"pid": 1, "start_time": "s", "port": 9000, "spawn_instance": "sp"}
        order: list[str] = []

        monkeypatch.setattr(routes_mod, "_unrevoke_app_pid", lambda n, row: order.append("restore"))
        # The rollback restored the app's record, so the row is un-revoked.
        monkeypatch.setattr(routes_mod, "get_app", lambda n: {"name": n})

        async def _may_run(_name: str) -> bool:
            return True

        async def _register(_name: str) -> None:
            order.append("register")

        monkeypatch.setattr(routes_mod, "_app_may_run_after_install", _may_run)
        monkeypatch.setattr(routes_mod, "_register_app_off_loop", _register)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda _name: order.append("start"))

        await routes_mod._restore_provenance_after_failed_update("ext", preserved)
        await routes_mod._restore_app_after_failed_update("ext")

        # Restore happens first, then register, then restart, and the rollback does
        # not put the row back a second time.
        assert order == ["restore", "register", "start"]

    @pytest.mark.asyncio
    async def test_a_failed_rollback_keeps_the_row_revoked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When update_app's own rollback also failed, the row stays revoked.

        A double fault: the update's copy fails AND the rollback cannot undo it, leaving
        the record (installed.json) ABSENT -- the app is not installed. Un-revoking
        the saved row then would leave a live row for a name with no app, and a same-name
        reinstall (which does not revoke or replace the row before starting) would adopt
        the surviving old backend against it. So the un-revoke is gated on the record
        still being present: a rollback that left it gone keeps the row REVOKED, which is
        what refuses the survivor on the next install.
        """
        preserved = {"pid": 7, "start_time": "s", "port": 9000, "spawn_instance": "sp"}
        restored: list[Any] = []
        monkeypatch.setattr(routes_mod, "_unrevoke_app_pid", lambda n, row: restored.append(row))
        # The rollback FAILED: installed.json is gone, so the app is not installed.
        monkeypatch.setattr(routes_mod, "get_app", lambda n: None)

        await routes_mod._restore_provenance_after_failed_update("ext", preserved)

        # The row is NOT un-revoked -- it stays revoked to refuse a survivor adoption.
        assert restored == [], "a failed rollback must keep the spawn row revoked"

    @pytest.mark.asyncio
    async def test_no_preserved_row_restores_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the update captured no row (nothing was tracked), there is nothing
        to restore -- the rollback must not call _unrevoke_app_pid at all."""

        restored: list[Any] = []
        monkeypatch.setattr(routes_mod, "_unrevoke_app_pid", lambda n, row: restored.append(row))

        async def _may_run(_name: str) -> bool:
            return False

        monkeypatch.setattr(routes_mod, "_app_may_run_after_install", _may_run)

        await routes_mod._restore_provenance_after_failed_update("ext", None)
        await routes_mod._restore_app_after_failed_update("ext")
        assert restored == []
