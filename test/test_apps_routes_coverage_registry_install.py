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
