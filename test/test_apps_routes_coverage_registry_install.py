"""Coverage tests for registry installs, their SSE stream and the registries config.

Pins ``_client_install_manifest``, ``POST /api/apps/registry/install``, the
``POST /api/apps/registry/install-stream`` event stream and the config-file failure
modes of ``PUT /api/apps/registries``.
``test_apps_routes_coverage.py`` holds install, registration and the app views, and
names the other route modules; the shared helpers live in
``apps_routes_coverage_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError
from aiohttp.test_utils import TestClient, TestServer
from apps_routes_coverage_helpers import (
    APP,
    _install,
    _make_app,
    _setup_env,
)

import kiro_crew.apps.routes as routes_mod
from kiro_crew.apps.manager import disable_app, enable_app
from kiro_crew.apps.routes import _client_install_manifest


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
        cfg = _client_install_manifest({"platform": {"os": ["macos"], "installMode": "client"}})
        assert cfg is not None
        assert cfg.installMode == "client"
        assert cfg.supports_platform("darwin")
        assert not cfg.supports_platform("linux")


# ---------------------------------------------------------------------------
# POST /api/apps/registry/install
# ---------------------------------------------------------------------------


class TestRegistryInstall:
    @pytest.mark.asyncio
    async def test_invalid_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(
                "/api/apps/registry/install",
                data="{",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_name_required(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
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
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install", json={"name": "some-app"})
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
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install", json={"name": "some-app"})
            assert resp.status == 400
            body = await resp.json()
        assert "ghp_0123456789abcdefghijABCDEFGHIJ0123456789" not in body["log"]
        assert "ghp_0123456789abcdefghijABCDEFGHIJ0123456789" not in body["error"]

    @pytest.mark.asyncio
    async def test_success_registers_and_returns_201(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)

        async def _ok(name: str, **kwargs: Any) -> dict[str, Any]:
            _install(tmp_path)
            return {"ok": True, "name": APP, "log": "done"}

        started: list[str] = []
        monkeypatch.setattr(routes_mod, "install_from_registry", _ok)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: started.append(n))
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install", json={"name": APP})
            assert resp.status == 201
            body = await resp.json()
        assert "registration" in body
        assert started == [APP]

    @pytest.mark.asyncio
    async def test_reconsent_stops_and_deregisters_without_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        calls: list[str] = []

        async def _reconsent(name: str, **kwargs: Any) -> dict[str, Any]:
            disable_app(name)
            return {"ok": True, "name": name}

        monkeypatch.setattr(routes_mod, "install_from_registry", _reconsent)
        monkeypatch.setattr(routes_mod, "register_app", lambda n: calls.append("register"))
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: calls.append("deregister"))
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: calls.append("stop"))
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append("start"))

        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install", json={"name": APP})
            assert resp.status == 201
            body = await resp.json()

        assert body["registration"]["agents"] == []
        assert calls == ["stop", "deregister"]

    @pytest.mark.asyncio
    async def test_replacing_an_installed_app_revokes_provenance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A registry Update (install over an INSTALLED app) revokes the spawn row.

        The Update button reaches the registry-install endpoint, which replaces the
        app's files while reusing its port. Without revoking the pre-update spawn row, a
        detached survivor of the old version would stay attributable and a re-enable
        would adopt it as the updated backend -- the attribution gap this PR closes. So
        a replace runs the provenance transaction: the row is revoked before the
        replace. Removing that transaction (the un-wrapped install the finding reported)
        leaves ``revoke_backend_provenance`` uncalled, turning this red.
        """
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)  # the app is already installed -> was_installed is True
        enable_app(APP)
        revoked: list[str] = []
        monkeypatch.setattr(
            routes_mod, "revoke_backend_provenance", lambda n: revoked.append(n) or {}
        )

        async def _ok(name: str, **kwargs: Any) -> dict[str, Any]:
            return {"ok": True, "name": APP, "log": "updated"}

        monkeypatch.setattr(routes_mod, "install_from_registry", _ok)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install", json={"name": APP})
            assert resp.status == 201
        # The pre-update row was revoked before (and invalidated after) the replace, so
        # the surviving old backend is not attributable.
        assert APP in revoked, "a registry replace of an installed app must revoke provenance"

    @pytest.mark.asyncio
    async def test_a_failed_replace_restores_the_running_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A clone/build failure on a registry Update puts the app back the way it was.

        The transaction stops the backend and deregisters the app's resources before it
        clones. When the clone/build then fails, nothing landed -- so the unchanged app
        must be restored: its resources re-registered and a runnable backend restarted.
        Without that restore the app is left enabled but unavailable, its agents, skills,
        crons and MCP servers gone. The handler re-registers (``register_app``) and
        restarts (``start_app_backend``); removing the restore call turns this red.
        """
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        calls: list[str] = []
        monkeypatch.setattr(routes_mod, "register_app", lambda n: calls.append("register"))
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append("start"))

        async def _failed(name: str, **kw: Any) -> dict[str, Any]:
            return {"ok": False, "name": APP, "error": "build failed"}

        monkeypatch.setattr(routes_mod, "install_from_registry", _failed)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install", json={"name": APP})
            assert resp.status == 400
        assert "register" in calls, "a failed registry replace must re-register the old app"
        assert "start" in calls, "a failed registry replace must restart the old backend"

    @pytest.mark.asyncio
    async def test_a_failed_revoke_restores_the_running_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A revoke that cannot be confirmed refuses the replace AND restores the app.

        The transaction durably revokes the pre-update spawn row before touching files;
        when that revoke cannot be persisted (disk full) the replace is refused with
        nothing destroyed. But the stop+deregister already happened, so the unchanged app
        must be restored -- re-registered and restarted -- not left enabled but
        unavailable. Removing the restore call turns this red.
        """
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        calls: list[str] = []
        monkeypatch.setattr(routes_mod, "register_app", lambda n: calls.append("register"))
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append("start"))

        from kiro_crew.apps.backend_runtime.pidfile import PidfileRevokeFailed

        def _boom(name: str) -> dict[str, Any]:
            raise PidfileRevokeFailed("ENOSPC")

        monkeypatch.setattr(routes_mod, "revoke_backend_provenance", _boom)

        async def _ok(name: str, **kw: Any) -> dict[str, Any]:
            return {"ok": True, "name": APP}

        monkeypatch.setattr(routes_mod, "install_from_registry", _ok)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install", json={"name": APP})
            assert resp.status == 400
            assert (await resp.json())["code"] == "provenance_not_revoked"
        assert (
            "register" in calls and "start" in calls
        ), "a refused (revoke-failed) registry replace must restore the old app"

    @pytest.mark.asyncio
    async def test_a_client_install_result_restores_the_running_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A replace that resolves to client-only install still restores the old app.

        When an installed app's manifest changes to client-only on an unsupported host,
        the replace returns ``needsClientInstall`` -- an uncommitted failure, nothing
        landed. The handler returns that result at an early branch, but the transaction
        already stopped and deregistered the app, so it must be restored first. Without
        the restore the unchanged app is left enabled with its backend stopped and its
        resources removed. Removing the restore call turns this red.
        """
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        calls: list[str] = []
        monkeypatch.setattr(routes_mod, "register_app", lambda n: calls.append("register"))
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append("start"))

        async def _client(name: str, **kw: Any) -> dict[str, Any]:
            return {"ok": False, "name": APP, "needsClientInstall": True}

        monkeypatch.setattr(routes_mod, "install_from_registry", _client)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install", json={"name": APP})
            assert resp.status == 200
            assert (await resp.json())["needsClientInstall"] is True
        assert (
            "register" in calls and "start" in calls
        ), "a client-install replace must restore the old app"


# ---------------------------------------------------------------------------
# POST /api/apps/registry/install-stream (SSE)
# ---------------------------------------------------------------------------


class TestRegistryInstallStream:
    @pytest.mark.asyncio
    async def test_invalid_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(
                "/api/apps/registry/install-stream",
                data="{",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_name_required(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install-stream", json={})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_streams_log_lines_then_done(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)

        async def _streaming(name: str, log_lines: Any = None, **kw: Any) -> dict:
            _install(tmp_path)
            assert log_lines is not None
            log_lines.append("step one")
            # Multi-line output must be reframed as multiple data: lines so a
            # newline in build output cannot break SSE framing.
            log_lines.append("step two\nstep two continued")
            return {"ok": True, "name": APP}

        monkeypatch.setattr(routes_mod, "install_from_registry", _streaming)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install-stream", json={"name": APP})
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
    async def test_streaming_update_that_widens_session_approval_stops_the_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The detail page's Update button goes through this SSE route. When the
        # re-clone takes the update path and ``update_app`` leaves the app
        # disabled pending re-consent, the route must behave like its two
        # siblings: stop and scrub the OLD version, and neither register nor
        # start the new one.
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        calls: list[str] = []

        async def _reconsent(name: str, log_lines: Any = None, **kw: Any) -> dict:
            disable_app(name)  # what update_app does on a widened grant
            return {"ok": True, "name": name}

        monkeypatch.setattr(routes_mod, "install_from_registry", _reconsent)
        monkeypatch.setattr(routes_mod, "register_app", lambda n: calls.append("register"))
        monkeypatch.setattr(routes_mod, "deregister_app", lambda n: calls.append("deregister"))
        monkeypatch.setattr(routes_mod, "stop_app_backend", lambda n: calls.append("stop"))
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append("start"))
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install-stream", json={"name": APP})
            assert resp.status == 200
            events = _sse_events(await resp.text())
        name, payload = events[-1]
        assert name == "done"
        done = json.loads(payload)
        assert done["ok"] is True
        assert done["registration"]["agents"] == []
        assert calls == ["stop", "deregister"]

    @pytest.mark.asyncio
    async def test_streaming_replace_of_an_installed_app_revokes_provenance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The SSE Update route also revokes the spawn row when it replaces an app.

        Same gap as the non-stream route: the detail page's Update button goes through
        this handler, which replaces an installed app's files while reusing its port.
        The replace must run the provenance transaction so a detached survivor of the
        old version is revoked and cannot be adopted as the updated backend. Removing
        the transaction leaves ``revoke_backend_provenance`` uncalled, turning this red.
        """
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)  # already installed -> was_installed is True
        enable_app(APP)
        revoked: list[str] = []
        monkeypatch.setattr(
            routes_mod, "revoke_backend_provenance", lambda n: revoked.append(n) or {}
        )

        async def _ok(name: str, log_lines: Any = None, **kw: Any) -> dict:
            return {"ok": True, "name": name}

        monkeypatch.setattr(routes_mod, "install_from_registry", _ok)
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: None)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install-stream", json={"name": APP})
            assert resp.status == 200
            events = _sse_events(await resp.text())
        name, payload = events[-1]
        assert name == "done"
        assert json.loads(payload)["ok"] is True
        assert APP in revoked, "a streaming registry replace must revoke provenance"

    @pytest.mark.asyncio
    async def test_a_failed_streaming_replace_restores_the_running_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The SSE Update route also restores the old app when the replace fails.

        Same rollback as the non-stream route: the transaction stops and deregisters the
        app before the clone, so a clone/build failure must restore it (re-register +
        restart) rather than leave it enabled but unavailable. Removing the restore call
        turns this red.
        """
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        calls: list[str] = []
        monkeypatch.setattr(routes_mod, "register_app", lambda n: calls.append("register"))
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append("start"))

        async def _failed(name: str, log_lines: Any = None, **kw: Any) -> dict:
            return {"ok": False, "name": APP, "error": "build failed"}

        monkeypatch.setattr(routes_mod, "install_from_registry", _failed)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install-stream", json={"name": APP})
            assert resp.status == 200
            events = _sse_events(await resp.text())
        assert json.loads(events[-1][1])["ok"] is False
        assert (
            "register" in calls and "start" in calls
        ), "a failed streaming registry replace must restore the old app"

    @pytest.mark.asyncio
    async def test_a_failed_streaming_revoke_restores_the_running_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A revoke that cannot be confirmed on the SSE route refuses AND restores.

        The stop+deregister already ran, so a refused (revoke-failed) replace must
        restore the unchanged app. Removing the restore call turns this red.
        """
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        calls: list[str] = []
        monkeypatch.setattr(routes_mod, "register_app", lambda n: calls.append("register"))
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append("start"))

        from kiro_crew.apps.backend_runtime.pidfile import PidfileRevokeFailed

        def _boom(name: str) -> dict[str, Any]:
            raise PidfileRevokeFailed("ENOSPC")

        monkeypatch.setattr(routes_mod, "revoke_backend_provenance", _boom)

        async def _ok(name: str, log_lines: Any = None, **kw: Any) -> dict:
            return {"ok": True, "name": APP}

        monkeypatch.setattr(routes_mod, "install_from_registry", _ok)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install-stream", json={"name": APP})
            assert resp.status == 200
            events = _sse_events(await resp.text())
        done = json.loads(events[-1][1])
        assert done["code"] == "provenance_not_revoked"
        assert (
            "register" in calls and "start" in calls
        ), "a refused (revoke-failed) streaming replace must restore the old app"

    @pytest.mark.asyncio
    async def test_a_streaming_client_install_result_restores_the_running_app(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The SSE route also restores the old app when the replace needs client install.

        Same uncommitted-failure handling as the non-stream route: a replace that
        resolves to client-only install landed nothing, so the stopped/deregistered app
        is restored before the ``done`` event. Removing the restore call turns this red.
        """
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        enable_app(APP)
        calls: list[str] = []
        monkeypatch.setattr(routes_mod, "register_app", lambda n: calls.append("register"))
        monkeypatch.setattr(routes_mod, "start_app_backend", lambda n: calls.append("start"))

        async def _client(name: str, log_lines: Any = None, **kw: Any) -> dict:
            return {"ok": False, "name": APP, "needsClientInstall": True}

        monkeypatch.setattr(routes_mod, "install_from_registry", _client)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install-stream", json={"name": APP})
            assert resp.status == 200
            events = _sse_events(await resp.text())
        assert json.loads(events[-1][1])["needsClientInstall"] is True
        assert (
            "register" in calls and "start" in calls
        ), "a streaming client-install replace must restore the old app"

    @pytest.mark.asyncio
    async def test_failed_install_reports_done_with_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)

        async def _failed(name: str, log_lines: Any = None, **kw: Any) -> dict:
            return {"ok": False, "name": name, "error": "build failed"}

        monkeypatch.setattr(routes_mod, "install_from_registry", _failed)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install-stream", json={"name": "some-app"})
            assert resp.status == 200
            events = _sse_events(await resp.text())
        name, payload = events[-1]
        assert name == "done"
        assert json.loads(payload)["error"] == "build failed"

    @pytest.mark.asyncio
    async def test_the_build_gate_refusal_reaches_the_client_verbatim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reason a refused install failed must travel ON the payload.

        The desktop build-step refusal is logged to ``security_events.jsonl``
        either way, but the dashboard's consent modal can only show a cause the
        ``done`` event carries. Pinned as the sentence, not a truthy ``error``
        key, because the UI renders this string — a generic placeholder
        substituted here would leave the user with a refusal and no stated
        reason, the dead end this test exists to prevent.
        """
        _setup_env(tmp_path, monkeypatch)
        refusal = (
            "Python apps that require a build step are not supported in Kiro Crew's "
            "desktop build: its bundled interpreter is inside the signed "
            "application bundle and cannot install packages"
        )

        async def _refused(name: str, log_lines: Any = None, **kw: Any) -> dict:
            return {"ok": False, "name": name, "error": refusal}

        monkeypatch.setattr(routes_mod, "install_from_registry", _refused)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install-stream", json={"name": "some-app"})
            assert resp.status == 200
            events = _sse_events(await resp.text())
        name, payload = events[-1]
        assert name == "done"
        done = json.loads(payload)
        assert done["ok"] is False
        assert done["error"] == refusal

    @pytest.mark.asyncio
    async def test_needs_client_install_short_circuits_done(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)

        async def _needs_client(name: str, log_lines: Any = None, **kw: Any) -> dict:
            return {"ok": True, "name": name, "needsClientInstall": True}

        monkeypatch.setattr(routes_mod, "install_from_registry", _needs_client)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install-stream", json={"name": "some-app"})
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
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/registry/install-stream", json={"name": "some-app"})
            events = _sse_events(await resp.text())
        assert json.loads(events[-1][1])["error"] == "clone exploded"

    @pytest.mark.asyncio
    async def test_client_gone_at_write_eof_is_not_an_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A browser tab closed mid-install makes the final write_eof raise
        # ClientConnectionResetError ("Cannot write to closing transport").
        # That is a routine disconnect, not a server error — it must not
        # escape the handler, where aiohttp would log an unhandled
        # "Error handling request" traceback.
        _setup_env(tmp_path, monkeypatch)

        async def _failed(name: str, log_lines: Any = None, **kw: Any) -> dict:
            return {"ok": False, "name": name, "error": "build failed"}

        monkeypatch.setattr(routes_mod, "install_from_registry", _failed)

        writes: list[bytes] = []

        async def _prepare(self, request):  # noqa: ANN001 - stub mirrors aiohttp
            return None

        async def _write(self, data):  # noqa: ANN001 - stub mirrors aiohttp
            writes.append(bytes(data))

        async def _gone(self, data=b""):  # noqa: ANN001 - stub mirrors aiohttp
            raise ClientConnectionResetError("Cannot write to closing transport")

        monkeypatch.setattr(web.StreamResponse, "prepare", _prepare)
        monkeypatch.setattr(web.StreamResponse, "write", _write)
        monkeypatch.setattr(web.StreamResponse, "write_eof", _gone)

        request = MagicMock()

        async def _json() -> dict:
            return {"name": "some-app"}

        request.json = _json
        # Owner identity, as token_auth_middleware would set it.
        identity = {"app": "", "user": "owner"}
        request.__contains__.side_effect = identity.__contains__
        request.__getitem__.side_effect = identity.__getitem__
        request.get.side_effect = identity.get
        request.app = {"state": SimpleNamespace(owner_id="owner")}

        resp = await routes_mod.handle_registry_install_stream(request)

        assert isinstance(resp, web.StreamResponse)
        # The done event was still flushed before the client vanished.
        assert any(b"event: done" in w for w in writes)


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
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
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
        (home / "config.json").write_text(json.dumps({"registries": None}), encoding="utf-8")
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.put(
                "/api/apps/registries",
                json={"registries": [{"repo": "https://git.example/org/apps"}]},
            )
            assert resp.status == 200
            body = await resp.json()
        assert body["ok"] is True
        assert body["newlyTrustedHosts"] == ["git.example"]
