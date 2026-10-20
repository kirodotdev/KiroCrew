"""Coverage tests for the routes that open and serve an installed app.

Pins ``POST /api/apps/{name}/open``, UI-file serving, dev mode, the app config routes,
the app-secret cache, backend URL resolution, the reverse proxy's authorization gates
and signed forward, and that a client disconnecting mid-stream from a UI file or the
proxy stays quiet.
``test_apps_routes_coverage.py`` holds install, registration and the app views, and
names the other route modules; the shared helpers live in
``apps_routes_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import platform as platform_mod
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
from test_update_provider import _UNALLOCATABLE_PID

import kiro_crew.apps.routes as routes_mod
from conftest import requires_symlinks
from kiro_crew.apps.manager import enable_app
from kiro_crew.apps.routes import (
    _get_app_secret,
    _resolve_app_backend_url,
    invalidate_app_secret_cache,
)

# ---------------------------------------------------------------------------
# POST /api/apps/{name}/open
# ---------------------------------------------------------------------------


class TestOpenApp:
    @pytest.mark.asyncio
    async def test_not_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_env(tmp_path, monkeypatch)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post("/api/apps/ghost/open")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_disabled_app_is_409(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, openCommand="true")
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
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
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
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
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
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
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
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
            # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing spawn stand-in moved verbatim; the open route only logs and returns this pid and nothing signals it
            return SimpleNamespace(pid=4321)

        monkeypatch.setattr(routes_mod, "wrap_argv", _wrap)
        monkeypatch.setattr(routes_mod, "cgroup_scope_argv", lambda argv: argv)
        monkeypatch.setattr(routes_mod, "create_subprocess_limited", _spawn)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
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
        monkeypatch.setattr(routes_mod, "wrap_argv", lambda argv, mode="standard": (argv, None))
        monkeypatch.setattr(routes_mod, "cgroup_scope_argv", lambda argv: argv)

        async def _boom(*argv: str, **kwargs: Any) -> Any:
            raise OSError("no such executable")

        monkeypatch.setattr(routes_mod, "create_subprocess_limited", _boom)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.post(f"/api/apps/{APP}/open")
            assert resp.status == 500
            assert "failed to launch" in (await resp.json())["error"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("app_identity", "dashboard_user"),
        [(None, "channel-user"), (APP, "owner")],
        ids=["non_owner_dashboard_subject", "app_token"],
    )
    async def test_non_owner_cannot_open(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        app_identity: str | None,
        dashboard_user: str,
    ) -> None:
        # Opening spawns the manifest's openCommand on the host, so it is an
        # owner action like enable and disable.
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path, openCommand="open-my-app")
        enable_app(APP)
        monkeypatch.setattr(routes_mod, "app_execution_denied", lambda n, **k: None)
        monkeypatch.setattr(platform_mod, "system", lambda: "Darwin")
        monkeypatch.setattr(routes_mod, "wrap_argv", lambda argv, mode="standard": (argv, None))
        monkeypatch.setattr(routes_mod, "cgroup_scope_argv", lambda argv: argv)
        spawned: list[tuple[str, ...]] = []

        async def _spawn(*argv: str, **kwargs: Any) -> Any:
            spawned.append(argv)
            return SimpleNamespace(pid=_UNALLOCATABLE_PID)

        monkeypatch.setattr(routes_mod, "create_subprocess_limited", _spawn)
        app = _make_app(app_identity=app_identity, dashboard_user=dashboard_user)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/apps/{APP}/open")
            assert resp.status == 403
            assert (await resp.json())["code"] == "owner_only"
        assert spawned == []


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
            resp = await client.get(f"/apps/{APP}/ui/..%2Fapp.json", allow_redirects=False)
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

    @requires_symlinks
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
    async def test_invalid_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
            resp = await client.post(f"/api/apps/{APP}/dev", json={"enabled": value})
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
    async def test_put_invalid_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
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
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.put(f"/api/apps/{APP}/config", json=[1, 2, 3])
            assert resp.status == 400
            assert "JSON object" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_put_then_get_round_trips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _setup_env(tmp_path, monkeypatch)
        _install(tmp_path)
        async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
            resp = await client.put(f"/api/apps/{APP}/config", json={"theme": "dark"})
            assert resp.status == 200
            assert (await resp.json())["ok"] is True
            resp = await client.get(f"/api/apps/{APP}/config")
            assert await resp.json() == {"theme": "dark"}


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
    def test_gateway_tracked_port_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(routes_mod, "get_app_backend_port", lambda n: 7712)
        assert _resolve_app_backend_url(APP) == "http://127.0.0.1:7712"

    def test_no_manifest_is_unresolvable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(routes_mod, "get_app_backend_port", lambda n: None)
        monkeypatch.setattr(routes_mod, "get_app_manifest", lambda n: None)
        assert _resolve_app_backend_url(APP) is None

    def test_self_managed_fixed_port_from_manifest(self, monkeypatch: pytest.MonkeyPatch) -> None:
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

    def test_auto_port_falls_back_to_mcp_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
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


def _fail_proxy_backend_with(app: web.Application, exc: BaseException) -> None:
    """Make the proxy's outbound session raise ``exc``, installed BEFORE start.

    ``register_app_routes`` creates the real ``ClientSession`` in an
    ``on_startup`` hook; hooks run in registration order, so this one runs
    right after it, closes the real session (its connector would otherwise
    outlive the test) and installs the fake while the app is still mutable.
    An ``app[...]`` write after the test server has started is deprecated by
    aiohttp.
    """

    async def _swap(app_: web.Application) -> None:
        real = app_.get("_proxy_session")
        if real is not None and not real.closed:
            await real.close()
        app_["_proxy_session"] = _FakeSession(exc)

    app.on_startup.append(_swap)


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
        monkeypatch.setattr(routes_mod, "_resolve_app_backend_url", lambda n: "http://127.0.0.1:1")
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
        monkeypatch.setattr(routes_mod, "_resolve_app_backend_url", lambda n: "http://127.0.0.1:1")
        app = _make_app()
        _fail_proxy_backend_with(app, aiohttp.ClientError("refused"))
        async with TestClient(TestServer(app)) as client:
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
        monkeypatch.setattr(routes_mod, "_resolve_app_backend_url", lambda n: "http://127.0.0.1:1")
        app = _make_app()
        _fail_proxy_backend_with(app, asyncio.TimeoutError())
        async with TestClient(TestServer(app)) as client:
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


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["prepare", "write", "write_eof"])
async def test_app_ui_stream_client_disconnect_is_quiet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    """A closed tab is routine at every UI-file response boundary."""
    home = _setup_env(tmp_path, monkeypatch)
    ui = home / "apps" / APP / "ui"
    ui.mkdir(parents=True)
    (ui / "app.js").write_bytes(b"console.log('ok')")

    calls: list[str] = []

    async def _prepare(self: web.StreamResponse, request: web.Request) -> None:
        calls.append("prepare")
        if boundary == "prepare":
            raise ClientConnectionResetError("Cannot write to closing transport")

    async def _write(self: web.StreamResponse, data: bytes) -> None:
        calls.append("write")
        if boundary == "write":
            raise ClientConnectionResetError("Cannot write to closing transport")

    async def _write_eof(self: web.StreamResponse, data: bytes = b"") -> None:
        calls.append("write_eof")
        if boundary == "write_eof":
            raise ClientConnectionResetError("Cannot write to closing transport")

    monkeypatch.setattr(web.StreamResponse, "prepare", _prepare)
    monkeypatch.setattr(web.StreamResponse, "write", _write)
    monkeypatch.setattr(web.StreamResponse, "write_eof", _write_eof)

    request = MagicMock()
    request.match_info = {"name": APP, "path": "app.js"}
    request.if_none_match = ()
    request.if_modified_since = None

    response = await routes_mod.handle_app_ui_file(request)

    assert isinstance(response, web.StreamResponse)
    expected = {
        "prepare": ["prepare"],
        "write": ["prepare", "write"],
        "write_eof": ["prepare", "write", "write_eof"],
    }
    assert calls == expected[boundary]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["prepare", "write", "write_eof"])
async def test_app_proxy_stream_client_disconnect_is_quiet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    """A closed browser must not turn a successful upstream stream into 502."""
    _setup_env(tmp_path, monkeypatch)
    monkeypatch.setattr(routes_mod, "is_app_enabled", lambda name: True)
    monkeypatch.setattr(
        routes_mod,
        "_resolve_app_backend_url",
        lambda name: "http://127.0.0.1:7777",
    )
    monkeypatch.setattr(routes_mod, "_get_app_secret", lambda name: "proxy-secret")

    class _Content:
        async def iter_any(self):  # noqa: ANN202 - aiohttp stream stub
            yield b"upstream payload"

    class _Upstream:
        status = 200
        headers: dict[str, str] = {}
        content = _Content()

    class _Context:
        async def __aenter__(self) -> _Upstream:
            return _Upstream()

        async def __aexit__(self, *args: Any) -> None:
            return None

    class _Session:
        closed = False

        def request(self, **kwargs: Any) -> _Context:
            return _Context()

    calls: list[str] = []

    async def _prepare(self: web.StreamResponse, request: web.Request) -> None:
        calls.append("prepare")
        if boundary == "prepare":
            raise ClientConnectionResetError("Cannot write to closing transport")

    async def _write(self: web.StreamResponse, data: bytes) -> None:
        calls.append("write")
        if boundary == "write":
            raise ClientConnectionResetError("Cannot write to closing transport")

    async def _write_eof(self: web.StreamResponse, data: bytes = b"") -> None:
        calls.append("write_eof")
        if boundary == "write_eof":
            raise ClientConnectionResetError("Cannot write to closing transport")

    monkeypatch.setattr(web.StreamResponse, "prepare", _prepare)
    monkeypatch.setattr(web.StreamResponse, "write", _write)
    monkeypatch.setattr(web.StreamResponse, "write_eof", _write_eof)

    request = MagicMock()
    request.match_info = {"name": APP, "path": "ping"}
    request.get = lambda key, default="": default
    request.rel_url = routes_mod.yarl.URL("/apps/cov-test-app/api/ping")
    request.headers = {}
    request.can_read_body = False
    request.method = "GET"
    request.app = {"_proxy_session": _Session()}

    response = await routes_mod.handle_app_api_proxy(request)

    assert isinstance(response, web.StreamResponse)
    assert response.status == 200
    expected = {
        "prepare": ["prepare"],
        "write": ["prepare", "write"],
        "write_eof": ["prepare", "write", "write_eof"],
    }
    assert calls == expected[boundary]
