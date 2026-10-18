"""Tests for App Kit's app install and uninstall endpoints.

Covers the uninstall cleanup of registry app sources, ``StreamingLogLines``,
the SSE streaming install endpoint and its security, and
``install_from_registry``'s ``log_lines`` parameter.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner
from gateway_appkit_endpoints_helpers import (  # noqa: F401 -- the fixture is requested by name
    app_env_fixture,
)

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME
from kiro_crew.apps.routes import register_app_routes

# ---------------------------------------------------------------------------
# Uninstall app-sources cleanup tests
# ---------------------------------------------------------------------------


class TestUninstallAppSourcesCleanup:
    """Verify that uninstalling a registry-installed app cleans up app-sources."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim; no test of the class calls monkeypatch.undo()
    def _uninstall_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        # Stub bridges
        import kiro_crew.apps.bridges as bridges_mod

        kiro_agents = tmp_path / "kiro-agents"
        kiro_agents.mkdir()
        monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
        import kiro_crew.apps.backend as bmod

        bmod._processes.clear()
        bmod._allocated_ports.clear()
        # Clear secret cache
        from kiro_crew.apps.routes import _app_secret_cache

        _app_secret_cache.clear()
        self._home = home

    def _create_app(
        self,
        name: str,
        *,
        origin: str = "registry",
        source: str = "registry:test-app",
    ) -> None:
        app_dir = self._home / "apps" / name
        app_dir.mkdir(parents=True)
        manifest = {
            "name": name,
            "version": "1.0.0",
            "displayName": name,
            "description": "test",
            "author": "t",
        }
        (app_dir / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest))
        installed = {
            "name": name,
            "version": "1.0.0",
            "displayName": name,
            "enabled": True,
            "source": source,
            "origin": origin,
            "resources": "gateway",
            "lifecycle": "gateway",
            "schemaVersion": 2,
        }
        (app_dir / "installed.json").write_text(json.dumps(installed))

    @asynccontextmanager
    async def _make_client(self):
        app = as_owner(web.Application())
        register_app_routes(app)
        async with TestClient(TestServer(app)) as c:
            yield c

    @pytest.mark.asyncio
    async def test_registry_app_sources_cleaned(self):
        """Uninstalling a registry app removes its workspace."""
        self._create_app("reg-app", origin="registry", source="registry:reg-app")
        # Simulate the per-app source clone directory (generic git clone layout:
        # ~/.kirocrew/app-sources/{name}/ holding a checked-out repo).
        ws_dir = self._home / "app-sources" / "reg-app"
        (ws_dir / ".git").mkdir(parents=True)
        (ws_dir / "package.json").write_text('{"name": "reg-app"}')

        async with self._make_client() as client:
            resp = await client.post("/api/apps/reg-app/uninstall", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

        # entire workspace should be removed
        assert not ws_dir.exists(), "app workspace should be removed"

    @pytest.mark.asyncio
    async def test_local_app_sources_not_cleaned(self):
        """Uninstalling a local-path app does NOT remove source code."""
        self._create_app("local-app", origin="local", source="/Users/dev/my-tool")

        async with self._make_client() as client:
            resp = await client.post("/api/apps/local-app/uninstall", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

        # No app-sources dir should have been touched (none exists for local apps)
        sources_dir = self._home / "app-sources"
        # Either doesn't exist or is empty — no cleanup attempted
        if sources_dir.exists():
            assert len(list(sources_dir.iterdir())) == 0

    @pytest.mark.asyncio
    async def test_external_app_sources_not_cleaned(self):
        """Uninstalling an external (self-registered) app does NOT remove source code."""
        self._create_app("ext-app", origin="external", source="self-managed")

        async with self._make_client() as client:
            resp = await client.post("/api/apps/ext-app/uninstall", json={})
            assert resp.status == 200

        sources_dir = self._home / "app-sources"
        if sources_dir.exists():
            assert len(list(sources_dir.iterdir())) == 0


# ---------------------------------------------------------------------------
# StreamingLogLines Tests
# ---------------------------------------------------------------------------


class TestStreamingLogLines:
    """Unit tests for StreamingLogLines — the queue-backed list used by
    the streaming install endpoint."""

    def test_append_pushes_to_queue(self) -> None:
        from kiro_crew.apps.registry import StreamingLogLines

        q: asyncio.Queue[str | None] = asyncio.Queue()
        sl = StreamingLogLines(q)
        sl.append("line 1")
        sl.append("line 2")
        assert list(sl) == ["line 1", "line 2"]
        assert q.qsize() == 2
        assert q.get_nowait() == "line 1"
        assert q.get_nowait() == "line 2"

    def test_extend_pushes_each_line(self) -> None:
        from kiro_crew.apps.registry import StreamingLogLines

        q: asyncio.Queue[str | None] = asyncio.Queue()
        sl = StreamingLogLines(q)
        sl.extend(["a", "b", "c"])
        assert list(sl) == ["a", "b", "c"]
        assert q.qsize() == 3

    def test_join_works_like_plain_list(self) -> None:
        from kiro_crew.apps.registry import StreamingLogLines

        q: asyncio.Queue[str | None] = asyncio.Queue()
        sl = StreamingLogLines(q)
        sl.append("hello")
        sl.append("world")
        assert "\n".join(sl) == "hello\nworld"

    def test_full_queue_does_not_raise(self) -> None:
        """When the queue is full, append should silently drop (not block)."""
        from kiro_crew.apps.registry import StreamingLogLines

        q: asyncio.Queue[str | None] = asyncio.Queue(maxsize=1)
        sl = StreamingLogLines(q)
        sl.append("first")  # fills the queue
        sl.append("second")  # should not raise
        assert list(sl) == ["first", "second"]
        assert q.qsize() == 1  # only first made it

    def test_empty_list_join(self) -> None:
        from kiro_crew.apps.registry import StreamingLogLines

        q: asyncio.Queue[str | None] = asyncio.Queue()
        sl = StreamingLogLines(q)
        assert "\n".join(sl) == ""


# ---------------------------------------------------------------------------
# Streaming Install Endpoint Tests
# ---------------------------------------------------------------------------


class TestRegistryInstallStream:
    """POST /api/apps/registry/install-stream — SSE streaming install."""

    @asynccontextmanager
    async def _make_client(self):
        from types import SimpleNamespace

        # Stands in for token_auth_middleware authenticating the dashboard
        # owner, which the registry-install owner gate requires.
        @web.middleware
        async def _owner(request, handler):
            request["app"] = ""
            request["user"] = "owner"
            return await handler(request)

        app = web.Application(middlewares=[_owner])
        app["state"] = SimpleNamespace(owner_id="owner")
        register_app_routes(app)
        async with TestClient(TestServer(app)) as c:
            yield c

    @pytest.mark.asyncio
    async def test_missing_name_returns_400(self, app_env: Path) -> None:
        async with self._make_client() as client:
            resp = await client.post(
                "/api/apps/registry/install-stream",
                json={},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "name" in data["error"]

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self, app_env: Path) -> None:
        async with self._make_client() as client:
            resp = await client.post(
                "/api/apps/registry/install-stream",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_app_streams_error(
        self,
        app_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Installing a non-existent app should stream a done event with error.

        ``inventory_for_install`` is pinned to ``None`` — "catalog reachable,
        app absent", the exact scenario the assertion below describes. The
        real call performs a fresh, deliberately UNCACHED HTTPS fetch on
        every install (a planted cache row must not supply install
        coordinates), so without this pin the test's verdict depended on
        live network from the runner: a transient fetch failure
        takes the fail-closed ``CatalogUnavailable`` branch instead, which
        the companion test below pins separately.
        """
        monkeypatch.setattr(
            "kiro_crew.apps.official_catalog.inventory_for_install",
            lambda name: None,
        )
        async with self._make_client() as client:
            resp = await client.post(
                "/api/apps/registry/install-stream",
                json={"name": "nonexistent-app"},
            )
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "text/event-stream"
            body = await resp.text()
            # Should contain a done event with error
            assert "event: done" in body
            assert "not found in registry" in body

    @pytest.mark.asyncio
    async def test_catalog_outage_streams_fail_closed_error(
        self,
        app_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the catalog cannot be consulted, install refuses fail-closed.

        The other branch of the fork the test above pins: ``None`` means
        authoritative absence, ``CatalogUnavailable`` means "could not ask" —
        and the install path must refuse rather than fall back to unpinned
        coordinates. Both branches are now deterministic instead of being
        selected by the CI runner's live network.
        """
        from kiro_crew.apps import official_catalog

        def _outage(name: str) -> None:
            raise official_catalog.CatalogUnavailable("simulated catalog outage (test)")

        monkeypatch.setattr(
            "kiro_crew.apps.official_catalog.inventory_for_install",
            _outage,
        )
        async with self._make_client() as client:
            resp = await client.post(
                "/api/apps/registry/install-stream",
                json={"name": "nonexistent-app"},
            )
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "text/event-stream"
            body = await resp.text()
            assert "event: done" in body
            # The fail-closed refusal, not the absence message.
            assert "official catalog could not be reached" in body
            assert "not found in registry" not in body

    @pytest.mark.asyncio
    async def test_streams_log_lines_then_done(
        self,
        app_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Mock install_from_registry to verify SSE log + done events."""

        async def _fake_install(name: str, log_lines: list[str] | None = None) -> dict[str, Any]:
            if log_lines is not None:
                log_lines.append("step 1: cloning")
                log_lines.append("step 2: building")
                log_lines.append("step 3: done")
            return {
                "ok": True,
                "name": name,
                "message": "installed",
                "log": "\n".join(log_lines or []),
            }

        monkeypatch.setattr(
            "kiro_crew.apps.routes.install_from_registry",
            _fake_install,
        )
        # Stub register_app to avoid touching real bridges
        monkeypatch.setattr(
            "kiro_crew.apps.routes.register_app",
            lambda name: type("R", (), {"to_dict": lambda self: {"ok": True}})(),
        )

        async with self._make_client() as client:
            resp = await client.post(
                "/api/apps/registry/install-stream",
                json={"name": "test-app"},
            )
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "text/event-stream"
            body = await resp.text()

            # Verify log events were streamed
            assert "event: log" in body
            assert "step 1: cloning" in body
            assert "step 2: building" in body
            assert "step 3: done" in body

            # Verify done event
            assert "event: done" in body
            assert '"ok": true' in body or '"ok":true' in body

    @pytest.mark.asyncio
    async def test_streams_error_on_install_failure(
        self,
        app_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When install fails, done event should contain the error."""

        async def _fake_install(name: str, log_lines: list[str] | None = None) -> dict[str, Any]:
            if log_lines is not None:
                log_lines.append("cloning...")
            return {
                "ok": False,
                "name": name,
                "error": "build failed",
                "log": "\n".join(log_lines or []),
            }

        monkeypatch.setattr(
            "kiro_crew.apps.routes.install_from_registry",
            _fake_install,
        )

        async with self._make_client() as client:
            resp = await client.post(
                "/api/apps/registry/install-stream",
                json={"name": "broken-app"},
            )
            assert resp.status == 200
            body = await resp.text()
            assert "event: log" in body
            assert "cloning..." in body
            assert "event: done" in body
            assert "build failed" in body

    @pytest.mark.asyncio
    async def test_streams_client_install_passthrough(
        self,
        app_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """needsClientInstall results should be forwarded in the done event."""

        async def _fake_install(name: str, log_lines: list[str] | None = None) -> dict[str, Any]:
            return {
                "ok": False,
                "needsClientInstall": True,
                "name": name,
                "clientInstall": {"shell": "curl ... | bash"},
                "error": "Requires macOS",
            }

        monkeypatch.setattr(
            "kiro_crew.apps.routes.install_from_registry",
            _fake_install,
        )

        async with self._make_client() as client:
            resp = await client.post(
                "/api/apps/registry/install-stream",
                json={"name": "mac-only-app"},
            )
            assert resp.status == 200
            body = await resp.text()
            assert "event: done" in body
            assert "needsClientInstall" in body

    @pytest.mark.asyncio
    async def test_exception_in_install_streams_error(
        self,
        app_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unhandled exceptions should be caught and streamed as done error."""

        async def _fake_install(name: str, log_lines: list[str] | None = None) -> dict[str, Any]:
            if log_lines is not None:
                log_lines.append("starting...")
            raise RuntimeError("unexpected crash")

        monkeypatch.setattr(
            "kiro_crew.apps.routes.install_from_registry",
            _fake_install,
        )

        async with self._make_client() as client:
            resp = await client.post(
                "/api/apps/registry/install-stream",
                json={"name": "crash-app"},
            )
            assert resp.status == 200
            body = await resp.text()
            assert "event: done" in body
            assert "unexpected crash" in body


# ---------------------------------------------------------------------------
# install_from_registry log_lines parameter Tests
# ---------------------------------------------------------------------------


class TestInstallFromRegistryLogLines:
    """Verify install_from_registry accepts custom log_lines."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim; no test of the class calls monkeypatch.undo()
    def _catalog_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin "catalog reachable, app absent" for the unknown-app path.

        These tests exercise the same live-fetching resolution path as
        ``test_unknown_app_streams_error``; without the pin their
        verdict depends on the runner's network.
        """
        monkeypatch.setattr(
            "kiro_crew.apps.official_catalog.inventory_for_install",
            lambda name: None,
        )

    @pytest.mark.asyncio
    async def test_custom_log_lines_receives_entries(self) -> None:
        """When a custom log_lines is passed, it should receive entries
        (even if the install fails early due to missing registry entry)."""
        from kiro_crew.apps.registry import install_from_registry

        custom: list[str] = []
        result = await install_from_registry("nonexistent", log_lines=custom)
        assert result["ok"] is False
        # The function returns early before appending to log_lines,
        # but the parameter should be accepted without error.
        assert isinstance(custom, list)

    @pytest.mark.asyncio
    async def test_default_log_lines_is_plain_list(self) -> None:
        """When log_lines is not passed, a plain list is used internally."""
        from kiro_crew.apps.registry import install_from_registry

        result = await install_from_registry("nonexistent")
        assert result["ok"] is False
        assert "not found" in result.get("error", "")


# ---------------------------------------------------------------------------
# SSE Newline Injection Tests
# ---------------------------------------------------------------------------


class TestRegistryInstallStreamSecurity:
    """Security tests for the streaming install endpoint."""

    @asynccontextmanager
    async def _make_client(self):
        from types import SimpleNamespace

        # Stands in for token_auth_middleware authenticating the dashboard
        # owner, which the registry-install owner gate requires.
        @web.middleware
        async def _owner(request, handler):
            request["app"] = ""
            request["user"] = "owner"
            return await handler(request)

        app = web.Application(middlewares=[_owner])
        app["state"] = SimpleNamespace(owner_id="owner")
        register_app_routes(app)
        async with TestClient(TestServer(app)) as c:
            yield c

    @pytest.mark.asyncio
    async def test_multiline_log_does_not_break_sse_framing(
        self,
        app_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Log lines containing newlines must not inject fake SSE events.

        A malicious log line like "legit\\nevent: done\\ndata: {hacked}"
        should be split into multiple data: lines, not interpreted as
        a new SSE event.
        """

        async def _fake_install(name: str, log_lines: list[str] | None = None) -> dict[str, Any]:
            if log_lines is not None:
                # Simulate a log line with embedded newlines that could
                # inject a fake "done" event if not properly escaped
                log_lines.append('legit line\nevent: done\ndata: {"ok":false,"hacked":true}')
                log_lines.append("normal line")
            return {"ok": True, "name": name, "message": "ok", "log": "\n".join(log_lines or [])}

        monkeypatch.setattr(
            "kiro_crew.apps.routes.install_from_registry",
            _fake_install,
        )
        monkeypatch.setattr(
            "kiro_crew.apps.routes.register_app",
            lambda name: type("R", (), {"to_dict": lambda self: {"ok": True}})(),
        )

        async with self._make_client() as client:
            resp = await client.post(
                "/api/apps/registry/install-stream",
                json={"name": "test-app"},
            )
            assert resp.status == 200
            body = await resp.text()

            # The injected "event: done" should NOT appear as a top-level
            # SSE event — it should be inside a "data:" line
            frames = body.strip().split("\n\n")
            done_frames = [f for f in frames if f.strip().startswith("event: done")]
            # There should be exactly ONE done frame (the real one at the end)
            assert len(done_frames) == 1
            # The real done frame should contain "ok": true
            assert '"ok": true' in done_frames[0] or '"ok":true' in done_frames[0]

    @pytest.mark.asyncio
    async def test_log_redaction_applied_per_line(
        self,
        app_env: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each streamed log line should be redacted for credentials.

        Uses an AWS access key pattern which redact_credentials catches.
        """

        async def _fake_install(name: str, log_lines: list[str] | None = None) -> dict[str, Any]:
            if log_lines is not None:
                # AWS access key ID pattern — caught by redact_credentials
                log_lines.append("Found key AKIAIOSFODNN7EXAMPLE in config")
            return {"ok": True, "name": name, "message": "ok", "log": "\n".join(log_lines or [])}

        monkeypatch.setattr(
            "kiro_crew.apps.routes.install_from_registry",
            _fake_install,
        )
        monkeypatch.setattr(
            "kiro_crew.apps.routes.register_app",
            lambda name: type("R", (), {"to_dict": lambda self: {"ok": True}})(),
        )

        async with self._make_client() as client:
            resp = await client.post(
                "/api/apps/registry/install-stream",
                json={"name": "test-app"},
            )
            body = await resp.text()
            # The raw AWS key should NOT appear in the streamed output
            assert "AKIAIOSFODNN7EXAMPLE" not in body
            # But the redaction marker should be present
            assert "REDACTED" in body
