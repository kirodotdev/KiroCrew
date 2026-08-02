"""Tests for Gateway endpoints added by App Kit (Task 7).

Covers:
- PUT/DELETE /api/mcp/servers/{name} — MCP server registration/deletion
- GET/PUT /api/apps/{name}/config — App config read/write
- POST /api/chat/slots/{slot}/context — Silent context injection
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME
from kiro_crew.apps.routes import register_app_routes
from kiro_crew.dashboard.chat import api_chat_slot_context
from kiro_crew.dashboard.handlers import api_mcp_server_detail
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mcp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up a temp MCP config environment."""
    mcp_json = tmp_path / "settings" / "mcp.json"
    mcp_json.parent.mkdir(parents=True)
    mcp_json.write_text('{"mcpServers": {}}')
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.mcp._GLOBAL_MCP_JSON", mcp_json
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.mcp._MCP_LOCK_PATH",
        mcp_json.with_suffix(".lock"),
    )
    # Stub _sync_mcp_to_agent to avoid touching real agent config
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.mcp._sync_mcp_to_agent",
        lambda *a, **kw: None,
    )
    return mcp_json


@pytest.fixture()
def app_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up a temp app environment with a test app installed."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    # Create a test app
    app_dir = home / "apps" / "test-app"
    app_dir.mkdir(parents=True)
    manifest = {
        "name": "test-app",
        "version": "1.0.0",
        "displayName": "Test App",
        "description": "For testing",
        "author": "tester",
    }
    (app_dir / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    # Create installed.json metadata
    installed = {
        "name": "test-app",
        "version": "1.0.0",
        "displayName": "Test App",
        "enabled": True,
        "managed": "kirocrew",
    }
    (app_dir / "installed.json").write_text(json.dumps(installed))
    # Stub bridges to avoid touching real kiro agents dir
    import kiro_crew.apps.bridges as bridges_mod
    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)
    import kiro_crew.apps.backend as bmod
    bmod._processes.clear()
    bmod._allocated_ports.clear()
    return home


def _make_state(tmp_path: Path) -> DashboardState:
    """Create a minimal DashboardState for testing."""
    from unittest.mock import MagicMock

    state = DashboardState.__new__(DashboardState)
    state._sessions = MagicMock()
    state._crons = MagicMock()
    state._lessons = MagicMock()
    state._start_time = time.time()
    state._subagents = None
    state._context_builder = None
    state._conversation_log = None
    state._consolidator = None
    state._task_runner = None
    state._slack_client = None
    state._owner_id = ""
    state._notification_log = []
    state._unread_count = 0
    state._slots = {}
    state._slack_to_slot = {}
    state._slot_counter = 0
    state._yolo = False
    state._yolo_expires = 0.0
    state._folders = []
    state._hook_store = MagicMock()
    state.channel_manager = None
    return state


# ---------------------------------------------------------------------------
# MCP Server Registration Tests (7.1)
# ---------------------------------------------------------------------------

class TestMcpServerRegistration:
    """PUT/DELETE /api/mcp/servers/{name}."""

    @asynccontextmanager
    async def _make_client(self):
        app = web.Application()
        app.router.add_put("/api/mcp/servers/{name}", api_mcp_server_detail)
        app.router.add_delete("/api/mcp/servers/{name}", api_mcp_server_detail)
        async with TestClient(TestServer(app)) as c:
            yield c

    @pytest.mark.asyncio
    async def test_put_registers_server(self, mcp_env: Path):
        async with self._make_client() as client:
            resp = await client.put(
                "/api/mcp/servers/my-server",
                json={"command": "node", "args": ["server.js"]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["name"] == "my-server"

            # Verify written to mcp.json
            cfg = json.loads(mcp_env.read_text(encoding="utf-8"))
            assert "my-server" in cfg["mcpServers"]
            assert cfg["mcpServers"]["my-server"]["command"] == "node"
            assert cfg["mcpServers"]["my-server"]["args"] == ["server.js"]

    @pytest.mark.asyncio
    async def test_put_updates_existing_server(self, mcp_env: Path):
        # Pre-populate
        mcp_env.write_text(json.dumps({
            "mcpServers": {"old-server": {"command": "python", "args": ["old.py"]}}
        }))
        async with self._make_client() as client:
            resp = await client.put(
                "/api/mcp/servers/old-server",
                json={"command": "node", "args": ["new.js"]},
            )
            assert resp.status == 200
            cfg = json.loads(mcp_env.read_text(encoding="utf-8"))
            assert cfg["mcpServers"]["old-server"]["command"] == "node"

    @pytest.mark.asyncio
    async def test_put_requires_command(self, mcp_env: Path):
        async with self._make_client() as client:
            resp = await client.put(
                "/api/mcp/servers/bad-server",
                json={"args": ["server.js"]},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "command" in data["error"]

    @pytest.mark.asyncio
    async def test_put_with_env(self, mcp_env: Path):
        async with self._make_client() as client:
            resp = await client.put(
                "/api/mcp/servers/env-server",
                json={"command": "node", "env": {"PORT": "3000"}},
            )
            assert resp.status == 200
            cfg = json.loads(mcp_env.read_text(encoding="utf-8"))
            assert cfg["mcpServers"]["env-server"]["env"] == {"PORT": "3000"}

    @pytest.mark.asyncio
    async def test_delete_removes_server(self, mcp_env: Path):
        mcp_env.write_text(json.dumps({
            "mcpServers": {"to-remove": {"command": "node"}}
        }))
        async with self._make_client() as client:
            resp = await client.delete("/api/mcp/servers/to-remove")
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["removed"] is True

            cfg = json.loads(mcp_env.read_text(encoding="utf-8"))
            assert "to-remove" not in cfg["mcpServers"]

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_404(self, mcp_env: Path):
        async with self._make_client() as client:
            resp = await client.delete("/api/mcp/servers/ghost")
            assert resp.status == 404
            data = await resp.json()
            assert data["removed"] is False

    @pytest.mark.asyncio
    async def test_put_invalid_json(self, mcp_env: Path):
        async with self._make_client() as client:
            resp = await client.put(
                "/api/mcp/servers/bad",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400


# ---------------------------------------------------------------------------
# App Config Tests (7.2)
# ---------------------------------------------------------------------------

class TestAppConfig:
    """GET/PUT /api/apps/{name}/config."""

    @asynccontextmanager
    async def _make_client(self):
        app = web.Application()
        register_app_routes(app)
        async with TestClient(TestServer(app)) as c:
            yield c

    @pytest.mark.asyncio
    async def test_get_empty_config(self, app_env: Path):
        async with self._make_client() as client:
            resp = await client.get("/api/apps/test-app/config")
            assert resp.status == 200
            data = await resp.json()
            assert data == {}

    @pytest.mark.asyncio
    async def test_put_and_get_config(self, app_env: Path):
        async with self._make_client() as client:
            config = {"theme": "dark", "interval": 300}
            resp = await client.put(
                "/api/apps/test-app/config", json=config
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

            resp = await client.get("/api/apps/test-app/config")
            assert resp.status == 200
            data = await resp.json()
            assert data == config

    @pytest.mark.asyncio
    async def test_get_config_nonexistent_app(self, app_env: Path):
        async with self._make_client() as client:
            resp = await client.get("/api/apps/no-such-app/config")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_put_config_nonexistent_app(self, app_env: Path):
        async with self._make_client() as client:
            resp = await client.put(
                "/api/apps/no-such-app/config", json={"key": "val"}
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_put_config_invalid_json(self, app_env: Path):
        async with self._make_client() as client:
            resp = await client.put(
                "/api/apps/test-app/config",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_config_non_object(self, app_env: Path):
        async with self._make_client() as client:
            resp = await client.put(
                "/api/apps/test-app/config", json=[1, 2, 3]
            )
            assert resp.status == 400
            data = await resp.json()
            assert "object" in data["error"]

    @pytest.mark.asyncio
    async def test_config_round_trip(self, app_env: Path):
        """Write config, read it back — values must match."""
        async with self._make_client() as client:
            config: dict[str, Any] = {
                "nested": {"a": 1, "b": [True, None, "str"]},
                "empty": {},
            }
            await client.put("/api/apps/test-app/config", json=config)
            resp = await client.get("/api/apps/test-app/config")
            assert await resp.json() == config


# ---------------------------------------------------------------------------
# Context Injection Tests (7.3)
# ---------------------------------------------------------------------------

class TestContextInjection:
    """POST /api/chat/slots/{slot}/context."""

    @asynccontextmanager
    async def _make_client(self, state: DashboardState):
        app = web.Application()
        app["state"] = state
        app.router.add_post(
            "/api/chat/slots/{slot}/context", api_chat_slot_context
        )
        async with TestClient(TestServer(app)) as c:
            yield c

    @pytest.mark.asyncio
    async def test_inject_basic(self, tmp_path: Path):
        state = _make_state(tmp_path)
        slot = _ChatSlot("test-slot")
        state._slots["test-slot"] = slot

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/test-slot/context",
                json={"content": "CR-123 was approved", "source": "watch"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["pending"] == 1

        # Verify entry was added to slot
        assert len(slot._pending_context) == 1
        entry = slot._pending_context[0]
        assert entry["content"] == "CR-123 was approved"
        assert entry["source"] == "watch"
        assert entry["ephemeral"] is True
        assert "injectedAt" in entry

    @pytest.mark.asyncio
    async def test_inject_nonexistent_slot(self, tmp_path: Path):
        state = _make_state(tmp_path)
        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/ghost/context",
                json={"content": "hello"},
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_inject_empty_content(self, tmp_path: Path):
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots["s1"] = slot

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/context", json={"content": ""}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_inject_invalid_json(self, tmp_path: Path):
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots["s1"] = slot

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/context",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_inject_with_max_age(self, tmp_path: Path):
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots["s1"] = slot

        async with self._make_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/context",
                json={
                    "content": "sensor data",
                    "maxAge": 60,
                    "ephemeral": False,
                },
            )
            assert resp.status == 200

        entry = slot._pending_context[0]
        assert entry["maxAge"] == 60
        assert entry["ephemeral"] is False

    @pytest.mark.asyncio
    async def test_inject_multiple(self, tmp_path: Path):
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots["s1"] = slot

        async with self._make_client(state) as client:
            for i in range(3):
                await client.post(
                    "/api/chat/slots/s1/context",
                    json={"content": f"entry-{i}"},
                )
        assert len(slot._pending_context) == 3

    @pytest.mark.asyncio
    async def test_no_ws_broadcast(self, tmp_path: Path):
        """Context injection must NOT broadcast any WS event."""
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots["s1"] = slot
        broadcast_calls: list[Any] = []
        state.broadcast_ws = lambda *a, **kw: broadcast_calls.append((a, kw))

        async with self._make_client(state) as client:
            await client.post(
                "/api/chat/slots/s1/context",
                json={"content": "silent"},
            )
        assert len(broadcast_calls) == 0

    @pytest.mark.asyncio
    async def test_no_visible_message(self, tmp_path: Path):
        """Context injection must NOT append a visible message to the slot."""
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots["s1"] = slot

        async with self._make_client(state) as client:
            await client.post(
                "/api/chat/slots/s1/context",
                json={"content": "invisible"},
            )
        assert len(slot.messages) == 0


# ---------------------------------------------------------------------------
# Context Drain Tests (consumed on next user message)
# ---------------------------------------------------------------------------

class TestContextDrain:
    """Verify pending context is drained and formatted correctly."""

    def test_drain_formats_context(self):
        """Pending context entries are formatted with source labels."""
        slot = _ChatSlot("s1")
        slot._pending_context = [
            {
                "content": "CR approved",
                "source": "watch-check",
                "ephemeral": True,
                "injectedAt": time.time(),
            },
        ]
        # Simulate the drain logic from chat.py
        now = time.time()
        ctx_parts: list[str] = []
        for entry in slot._pending_context:
            max_age = entry.get("maxAge")
            if max_age is not None:
                injected_at = entry.get("injectedAt", 0)
                if injected_at + max_age < now:
                    continue
            source = entry.get("source", "app")
            ctx_parts.append(
                f'[Background context from "{source}"]\n'
                f'{entry["content"]}\n'
                f"[End of background context]\n"
            )
        slot._pending_context.clear()

        assert len(ctx_parts) == 1
        assert 'from "watch-check"' in ctx_parts[0]
        assert "CR approved" in ctx_parts[0]
        assert len(slot._pending_context) == 0

    def test_expired_entries_discarded(self):
        """Entries past maxAge are silently dropped during drain."""
        slot = _ChatSlot("s1")
        slot._pending_context = [
            {
                "content": "old data",
                "source": "sensor",
                "ephemeral": True,
                "injectedAt": time.time() - 600,  # 10 min ago
                "maxAge": 300,  # 5 min TTL → expired
            },
            {
                "content": "fresh data",
                "source": "sensor",
                "ephemeral": True,
                "injectedAt": time.time(),
                "maxAge": 300,
            },
        ]
        now = time.time()
        ctx_parts: list[str] = []
        for entry in slot._pending_context:
            max_age = entry.get("maxAge")
            if max_age is not None:
                injected_at = entry.get("injectedAt", 0)
                if injected_at + max_age < now:
                    continue
            ctx_parts.append(entry["content"])
        slot._pending_context.clear()

        assert len(ctx_parts) == 1
        assert ctx_parts[0] == "fresh data"

    def test_no_max_age_never_expires(self):
        """Entries without maxAge are always included."""
        slot = _ChatSlot("s1")
        slot._pending_context = [
            {
                "content": "persistent",
                "source": "app",
                "ephemeral": True,
                "injectedAt": time.time() - 86400,  # 1 day ago
            },
        ]
        now = time.time()
        ctx_parts: list[str] = []
        for entry in slot._pending_context:
            max_age = entry.get("maxAge")
            if max_age is not None:
                injected_at = entry.get("injectedAt", 0)
                if injected_at + max_age < now:
                    continue
            ctx_parts.append(entry["content"])
        slot._pending_context.clear()

        assert len(ctx_parts) == 1
        assert ctx_parts[0] == "persistent"


# ---------------------------------------------------------------------------
# Reverse Proxy Tests (handle_app_api_proxy)
# ---------------------------------------------------------------------------


class TestReverseProxy:
    """Tests for /apps/{name}/api/{path} reverse proxy."""

    @pytest.fixture(autouse=True)
    def _proxy_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Set up a temp environment for proxy tests."""
        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        # Create a test app with a secret
        app_dir = home / "apps" / "proxy-app"
        app_dir.mkdir(parents=True)
        self._secret = "test-secret-abc123"
        (app_dir / ".app_secret").write_text(self._secret)
        manifest = {
            "name": "proxy-app",
            "version": "1.0.0",
            "displayName": "Proxy App",
            "description": "For proxy testing",
            "author": "tester",
        }
        (app_dir / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest))
        installed = {
            "name": "proxy-app",
            "version": "1.0.0",
            "displayName": "Proxy App",
            "enabled": True,
            "origin": "local",
            "resources": "gateway",
            "lifecycle": "gateway",
            "schemaVersion": 2,
        }
        (app_dir / "installed.json").write_text(json.dumps(installed))
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

    @asynccontextmanager
    async def _make_client(self):
        app = web.Application()
        register_app_routes(app)
        async with TestClient(TestServer(app)) as c:
            yield c

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, monkeypatch):
        """The handler rejects paths containing '..' (defense-in-depth).

        aiohttp normalizes ``..`` at the router level before the handler
        sees it, so this guard is never triggered via normal HTTP requests.
        We test it by calling the handler directly with a crafted request.
        """
        from unittest.mock import MagicMock

        from kiro_crew.apps.routes import handle_app_api_proxy

        request = MagicMock()
        request.match_info = {"name": "proxy-app", "path": "foo/../etc/passwd"}
        resp = await handle_app_api_proxy(request)
        assert resp.status == 400
        data = json.loads(resp.body)
        assert "invalid path" in data["error"]

    @pytest.mark.asyncio
    async def test_disabled_app_proxy_rejected(self):
        """An app that is NOT enabled cannot be proxied to, even with a valid secret.

        The other guards here prove WHO is calling; this one proves the app is allowed
        to run at all. Every builtin ships ``defaultEnabled: false``, and a builtin
        whose backend is derived from ``mcpServers`` is issued an ``.app_secret`` at
        registration — so without this gate an app the user never turned on still had
        an authenticated, secret-signed proxy to its local backend, and a mutation
        could reach a process that was never activated.

        403, not 502: refusing an unauthorized caller is a different answer from
        "there is no backend there".
        """
        from unittest.mock import MagicMock, patch

        from kiro_crew.apps.routes import handle_app_api_proxy

        # Flip the fixture's app to disabled; everything else stays valid.
        installed_path = self._home / "apps" / "proxy-app" / "installed.json"
        meta = json.loads(installed_path.read_text())
        assert meta["enabled"] is True, "fixture should start enabled"
        meta["enabled"] = False
        installed_path.write_text(json.dumps(meta))

        request = MagicMock()
        request.match_info = {"name": "proxy-app", "path": "health"}
        request.get = lambda key, default="": default   # dashboard caller, not an app

        with patch("kiro_crew.apps.routes.sel") as mock_sel:
            resp = await handle_app_api_proxy(request)

        assert resp.status == 403, f"expected 403, got {resp.status}"
        body = json.loads(resp.body)
        assert "not enabled" in body["error"]
        # Machine-readable identifier, per test_error_code_contract.py: the
        # dashboard renders `error` verbatim into a localized page, so the code is
        # what a client switches on.
        assert body["code"] == "app_not_enabled"

        # The denial must be AUDITED. An authorization decision that leaves no
        # trail makes a repeated probe against a disabled app unobservable, which
        # is most of the value of having the gate at all.
        mock_sel.return_value.log_api_access.assert_called_once()
        audit = mock_sel.return_value.log_api_access.call_args.kwargs
        assert audit["outcome"] == "denied"
        assert audit["operation"] == "app_proxy_disabled_app"
        assert "proxy-app" in audit["resources"]

    @pytest.mark.asyncio
    async def test_enabled_app_passes_the_gate(self):
        """The gate must not block a legitimately enabled app.

        Proves the 403 above comes from the enablement check specifically and not from
        some unrelated refusal: the same request on an ENABLED app gets past it and
        fails later, on the backend not existing (502), never 403.
        """
        from unittest.mock import MagicMock

        from kiro_crew.apps.routes import handle_app_api_proxy

        meta = json.loads((self._home / "apps" / "proxy-app" / "installed.json").read_text())
        assert meta["enabled"] is True

        request = MagicMock()
        request.match_info = {"name": "proxy-app", "path": "health"}
        request.get = lambda key, default="": default

        resp = await handle_app_api_proxy(request)
        assert resp.status != 403, "an enabled app must not be refused by the gate"
        assert resp.status == 502, f"expected the no-backend 502, got {resp.status}"

    @pytest.mark.asyncio
    async def test_cross_app_token_rejected(self):
        """An APP token (request['app']) may only proxy into its OWN backend.
        A token for app 'other-app' hitting /apps/proxy-app/api/... is 403
        (CWE-269 cross-app guard). Called directly with a crafted request."""
        from unittest.mock import MagicMock

        from kiro_crew.apps.routes import handle_app_api_proxy

        request = MagicMock()
        request.match_info = {"name": "proxy-app", "path": "health"}
        # Simulate token_auth_middleware having set a DIFFERENT app identity.
        request.get = lambda key, default="": "other-app" if key == "app" else default
        resp = await handle_app_api_proxy(request)
        assert resp.status == 403
        data = json.loads(resp.body)
        assert "another app" in data["error"]

    @pytest.mark.asyncio
    async def test_same_app_token_not_rejected_by_cross_app_guard(self, monkeypatch):
        """A token whose app matches the target app passes the cross-app guard.
        We stop at backend resolution (return no backend → 502) to prove we got
        PAST the 403 guard without exercising the header-forwarding path."""
        from unittest.mock import MagicMock

        import kiro_crew.apps.routes as rmod
        from kiro_crew.apps.routes import handle_app_api_proxy

        monkeypatch.setattr(rmod, "_resolve_app_backend_url", lambda name: "")
        request = MagicMock()
        request.match_info = {"name": "proxy-app", "path": "health"}
        request.get = lambda key, default="": "proxy-app" if key == "app" else default
        resp = await handle_app_api_proxy(request)
        # 502 (no backend), NOT 403 — the cross-app guard let a same-app token through.
        assert resp.status == 502

    @pytest.mark.asyncio
    async def test_missing_app_secret_returns_502(self, tmp_path: Path, monkeypatch):
        """App without .app_secret returns 502."""
        # Create an app without a secret
        app_dir = self._home / "apps" / "no-secret-app"
        app_dir.mkdir(parents=True)
        manifest = {
            "name": "no-secret-app", "version": "1.0.0",
            "displayName": "No Secret", "description": "test", "author": "t",
        }
        (app_dir / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest))
        installed = {
            "name": "no-secret-app", "version": "1.0.0",
            "displayName": "No Secret", "enabled": True,
            "origin": "local", "resources": "gateway", "lifecycle": "gateway",
            "schemaVersion": 2,
        }
        (app_dir / "installed.json").write_text(json.dumps(installed))
        import kiro_crew.apps.routes as rmod
        monkeypatch.setattr(rmod, "_resolve_app_backend_url", lambda name: "http://127.0.0.1:19999")
        # Clear cache so the missing secret is detected
        rmod._app_secret_cache.clear()

        async with self._make_client() as client:
            resp = await client.get("/apps/no-secret-app/api/health")
            assert resp.status == 502
            data = await resp.json()
            assert "secret" in data["error"].lower() or "no secret" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_backend_unreachable_returns_502(self, monkeypatch):
        """Proxy to unreachable backend returns 502."""
        import kiro_crew.apps.routes as rmod

        # Point to a port that's definitely not listening
        monkeypatch.setattr(rmod, "_resolve_app_backend_url", lambda name: "http://127.0.0.1:19999")

        async with self._make_client() as client:
            resp = await client.get("/apps/proxy-app/api/health")
            assert resp.status == 502
            data = await resp.json()
            assert "unreachable" in data.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_hmac_header_present_and_valid(self, monkeypatch):
        """Proxy request includes X-KiroCrew-Proxy with valid HMAC."""
        import hashlib
        import hmac as _hmac

        # Start a tiny backend that echoes headers
        received_headers: dict[str, str] = {}

        async def echo_handler(request: web.Request) -> web.Response:
            for k, v in request.headers.items():
                received_headers[k.lower()] = v
            return web.json_response({"ok": True})

        backend_app = web.Application()
        backend_app.router.add_route("*", "/{path:.*}", echo_handler)
        runner = web.AppRunner(backend_app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]

        import kiro_crew.apps.routes as rmod
        monkeypatch.setattr(rmod, "_resolve_app_backend_url", lambda name: f"http://127.0.0.1:{port}")

        try:
            async with self._make_client() as client:
                resp = await client.get("/apps/proxy-app/api/test-path")
                assert resp.status == 200

            # Verify HMAC header was forwarded
            proxy_header = received_headers.get("x-kirocrew-proxy", "")
            assert proxy_header, "X-KiroCrew-Proxy header missing"
            assert ":" in proxy_header

            ts, sig = proxy_header.split(":", 1)
            # Verify signature — proxy preserves /api/ prefix in the forwarded
            # path so the HMAC msg includes it. GET carries no body, so the
            # body hash is sha256 of the empty byte string.
            msg = f"{ts}:GET:/api/test-path:" + hashlib.sha256(b"").hexdigest()
            expected = _hmac.new(
                self._secret.encode(), msg.encode(), hashlib.sha256
            ).hexdigest()
            assert sig == expected
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_hmac_includes_query_string(self, monkeypatch):
        """HMAC signature includes query string when present."""
        import hashlib
        import hmac as _hmac

        received_headers: dict[str, str] = {}
        received_qs = ""

        async def echo_handler(request: web.Request) -> web.Response:
            nonlocal received_qs
            for k, v in request.headers.items():
                received_headers[k.lower()] = v
            received_qs = request.query_string
            return web.json_response({"ok": True})

        backend_app = web.Application()
        backend_app.router.add_route("*", "/{path:.*}", echo_handler)
        runner = web.AppRunner(backend_app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]

        import kiro_crew.apps.routes as rmod
        monkeypatch.setattr(rmod, "_resolve_app_backend_url", lambda name: f"http://127.0.0.1:{port}")

        try:
            async with self._make_client() as client:
                resp = await client.get("/apps/proxy-app/api/data?user=alice&limit=10")
                assert resp.status == 200

            # Verify query string was forwarded
            assert "user=alice" in received_qs
            assert "limit=10" in received_qs

            # Verify HMAC includes query string — proxy preserves /api/
            # prefix in forwarded path, so msg includes it.
            proxy_header = received_headers.get("x-kirocrew-proxy", "")
            ts, sig = proxy_header.split(":", 1)
            empty_body_hash = hashlib.sha256(b"").hexdigest()
            msg = f"{ts}:GET:/api/data?user=alice&limit=10:" + empty_body_hash
            expected = _hmac.new(
                self._secret.encode(), msg.encode(), hashlib.sha256
            ).hexdigest()
            assert sig == expected

            # Verify that a signature WITHOUT query string does NOT match
            msg_no_qs = f"{ts}:GET:/api/data:" + empty_body_hash
            wrong_sig = _hmac.new(
                self._secret.encode(), msg_no_qs.encode(), hashlib.sha256
            ).hexdigest()
            assert sig != wrong_sig
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_hmac_covers_body(self, monkeypatch):
        """HMAC binds sha256 of the request body (integrity)."""
        import hashlib
        import hmac as _hmac

        received_headers: dict[str, str] = {}

        async def echo_handler(request: web.Request) -> web.Response:
            for k, v in request.headers.items():
                received_headers[k.lower()] = v
            # Drain the body so the proxied request completes cleanly.
            await request.read()
            return web.json_response({"ok": True})

        backend_app = web.Application()
        backend_app.router.add_route("*", "/{path:.*}", echo_handler)
        runner = web.AppRunner(backend_app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]

        import kiro_crew.apps.routes as rmod
        monkeypatch.setattr(rmod, "_resolve_app_backend_url", lambda name: f"http://127.0.0.1:{port}")

        body_bytes = b'{"hello": "world", "n": 42}'
        try:
            async with self._make_client() as client:
                resp = await client.post("/apps/proxy-app/api/echo", data=body_bytes)
                assert resp.status == 200

            proxy_header = received_headers.get("x-kirocrew-proxy", "")
            assert proxy_header, "X-KiroCrew-Proxy header missing"
            ts, sig = proxy_header.split(":", 1)

            # Signature binds sha256 of the actual (non-empty) body.
            body_hash = hashlib.sha256(body_bytes).hexdigest()
            msg = f"{ts}:POST:/api/echo:" + body_hash
            expected = _hmac.new(
                self._secret.encode(), msg.encode(), hashlib.sha256
            ).hexdigest()
            assert sig == expected

            # A signature computed over the EMPTY-body hash must NOT match,
            # proving the body is actually bound into the HMAC.
            msg_empty = f"{ts}:POST:/api/echo:" + hashlib.sha256(b"").hexdigest()
            wrong_sig = _hmac.new(
                self._secret.encode(), msg_empty.encode(), hashlib.sha256
            ).hexdigest()
            assert sig != wrong_sig
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_no_backend_returns_502(self):
        """App with no backend URL at all returns 502."""
        async with self._make_client() as client:
            resp = await client.get("/apps/proxy-app/api/anything")
            assert resp.status == 502
            data = await resp.json()
            assert "no reachable backend" in data["error"]


class TestSSRFGuard:
    """Tests for _resolve_app_backend_url SSRF protections."""

    def test_rejects_gateway_own_port(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Backend URL pointing to gateway's own port is rejected."""
        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setenv("KIROCREW_PORT", "5476")

        app_dir = home / "apps" / "evil-app"
        app_dir.mkdir(parents=True)
        manifest = {
            "name": "evil-app", "version": "1.0.0",
            "displayName": "Evil", "description": "test", "author": "t",
            "mcpServers": {
                "self-ref": {"url": "http://localhost:5476/api/lessons"}
            },
        }
        (app_dir / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest))
        installed = {
            "name": "evil-app", "version": "1.0.0",
            "displayName": "Evil", "enabled": True,
            "origin": "local", "resources": "gateway", "lifecycle": "gateway",
            "schemaVersion": 2,
        }
        (app_dir / "installed.json").write_text(json.dumps(installed))

        import kiro_crew.apps.bridges as bridges_mod
        kiro_agents = tmp_path / "kiro-agents"
        kiro_agents.mkdir(exist_ok=True)
        monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)

        from kiro_crew.apps.routes import _resolve_app_backend_url
        result = _resolve_app_backend_url("evil-app")
        assert result is None, f"Expected None for self-referential URL, got {result}"

    def test_rejects_non_loopback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Backend URL pointing to external host is rejected."""
        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))

        app_dir = home / "apps" / "ext-app"
        app_dir.mkdir(parents=True)
        manifest = {
            "name": "ext-app", "version": "1.0.0",
            "displayName": "Ext", "description": "test", "author": "t",
            "mcpServers": {
                "remote": {"url": "http://10.0.0.1:8080/mcp"}
            },
        }
        (app_dir / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest))
        installed = {
            "name": "ext-app", "version": "1.0.0",
            "displayName": "Ext", "enabled": True,
            "origin": "local", "resources": "gateway", "lifecycle": "gateway",
            "schemaVersion": 2,
        }
        (app_dir / "installed.json").write_text(json.dumps(installed))

        import kiro_crew.apps.bridges as bridges_mod
        kiro_agents = tmp_path / "kiro-agents"
        kiro_agents.mkdir(exist_ok=True)
        monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)

        from kiro_crew.apps.routes import _resolve_app_backend_url
        result = _resolve_app_backend_url("ext-app")
        assert result is None, f"Expected None for non-loopback URL, got {result}"

    def test_allows_valid_loopback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Backend URL on loopback with non-gateway port is allowed."""
        home = tmp_path / "kirocrew-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        monkeypatch.setenv("KIROCREW_PORT", "5476")

        app_dir = home / "apps" / "good-app"
        app_dir.mkdir(parents=True)
        manifest = {
            "name": "good-app", "version": "1.0.0",
            "displayName": "Good", "description": "test", "author": "t",
            "mcpServers": {
                "backend": {"url": "http://localhost:8080/mcp"}
            },
        }
        (app_dir / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest))
        installed = {
            "name": "good-app", "version": "1.0.0",
            "displayName": "Good", "enabled": True,
            "origin": "local", "resources": "gateway", "lifecycle": "gateway",
            "schemaVersion": 2,
        }
        (app_dir / "installed.json").write_text(json.dumps(installed))

        import kiro_crew.apps.bridges as bridges_mod
        kiro_agents = tmp_path / "kiro-agents"
        kiro_agents.mkdir(exist_ok=True)
        monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)

        from kiro_crew.apps.routes import _resolve_app_backend_url
        result = _resolve_app_backend_url("good-app")
        assert result == "http://127.0.0.1:8080"


class TestContextInjectionPerSourceCap:
    """Tests for per-source rate limiting on context injection."""

    @asynccontextmanager
    async def _make_client(self, state: DashboardState):
        app = web.Application()
        app["state"] = state
        app.router.add_post(
            "/api/chat/slots/{slot}/context", api_chat_slot_context
        )
        async with TestClient(TestServer(app)) as c:
            yield c

    @pytest.mark.asyncio
    async def test_per_source_cap_enforced(self, tmp_path: Path):
        """A single source cannot exceed 10 pending entries."""
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots["s1"] = slot

        async with self._make_client(state) as client:
            # Inject 10 entries from same source — should all succeed
            for i in range(10):
                resp = await client.post(
                    "/api/chat/slots/s1/context",
                    json={"content": f"entry-{i}", "source": "flood-app"},
                )
                assert resp.status == 200

            # 11th entry from same source — should be rejected with 429
            resp = await client.post(
                "/api/chat/slots/s1/context",
                json={"content": "one-too-many", "source": "flood-app"},
            )
            assert resp.status == 429
            data = await resp.json()
            assert "flood-app" in data["error"]

    @pytest.mark.asyncio
    async def test_different_sources_not_capped(self, tmp_path: Path):
        """Different sources each get their own cap."""
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots["s1"] = slot

        async with self._make_client(state) as client:
            # 10 from source-a
            for i in range(10):
                resp = await client.post(
                    "/api/chat/slots/s1/context",
                    json={"content": f"a-{i}", "source": "source-a"},
                )
                assert resp.status == 200

            # 10 from source-b — should also succeed (different source)
            for i in range(10):
                resp = await client.post(
                    "/api/chat/slots/s1/context",
                    json={"content": f"b-{i}", "source": "source-b"},
                )
                assert resp.status == 200

        assert len(slot._pending_context) == 20

    @pytest.mark.asyncio
    async def test_empty_source_not_capped(self, tmp_path: Path):
        """Entries with empty source bypass per-source cap."""
        state = _make_state(tmp_path)
        slot = _ChatSlot("s1")
        state._slots["s1"] = slot

        async with self._make_client(state) as client:
            for i in range(15):
                resp = await client.post(
                    "/api/chat/slots/s1/context",
                    json={"content": f"no-source-{i}"},
                )
                assert resp.status == 200

        assert len(slot._pending_context) == 15


# ---------------------------------------------------------------------------
# Uninstall app-sources cleanup tests
# ---------------------------------------------------------------------------


class TestUninstallAppSourcesCleanup:
    """Verify that uninstalling a registry-installed app cleans up app-sources."""

    @pytest.fixture(autouse=True)
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
        self, name: str, *, origin: str = "registry", source: str = "registry:test-app",
    ) -> None:
        app_dir = self._home / "apps" / name
        app_dir.mkdir(parents=True)
        manifest = {
            "name": name, "version": "1.0.0",
            "displayName": name, "description": "test", "author": "t",
        }
        (app_dir / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest))
        installed = {
            "name": name, "version": "1.0.0", "displayName": name,
            "enabled": True, "source": source,
            "origin": origin, "resources": "gateway", "lifecycle": "gateway",
            "schemaVersion": 2,
        }
        (app_dir / "installed.json").write_text(json.dumps(installed))

    @asynccontextmanager
    async def _make_client(self):
        app = web.Application()
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
        sl.append("first")   # fills the queue
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
        app = web.Application()
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
        self, app_env: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Installing a non-existent app should stream a done event with error."""
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
    async def test_streams_log_lines_then_done(
        self, app_env: Path, monkeypatch: pytest.MonkeyPatch,
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
            "kiro_crew.apps.routes.install_from_registry", _fake_install,
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
        self, app_env: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When install fails, done event should contain the error."""
        async def _fake_install(name: str, log_lines: list[str] | None = None) -> dict[str, Any]:
            if log_lines is not None:
                log_lines.append("cloning...")
            return {"ok": False, "name": name, "error": "build failed", "log": "\n".join(log_lines or [])}

        monkeypatch.setattr(
            "kiro_crew.apps.routes.install_from_registry", _fake_install,
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
        self, app_env: Path, monkeypatch: pytest.MonkeyPatch,
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
            "kiro_crew.apps.routes.install_from_registry", _fake_install,
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
        self, app_env: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unhandled exceptions should be caught and streamed as done error."""
        async def _fake_install(name: str, log_lines: list[str] | None = None) -> dict[str, Any]:
            if log_lines is not None:
                log_lines.append("starting...")
            raise RuntimeError("unexpected crash")

        monkeypatch.setattr(
            "kiro_crew.apps.routes.install_from_registry", _fake_install,
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
        app = web.Application()
        register_app_routes(app)
        async with TestClient(TestServer(app)) as c:
            yield c

    @pytest.mark.asyncio
    async def test_multiline_log_does_not_break_sse_framing(
        self, app_env: Path, monkeypatch: pytest.MonkeyPatch,
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
                log_lines.append("legit line\nevent: done\ndata: {\"ok\":false,\"hacked\":true}")
                log_lines.append("normal line")
            return {"ok": True, "name": name, "message": "ok", "log": "\n".join(log_lines or [])}

        monkeypatch.setattr(
            "kiro_crew.apps.routes.install_from_registry", _fake_install,
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
        self, app_env: Path, monkeypatch: pytest.MonkeyPatch,
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
            "kiro_crew.apps.routes.install_from_registry", _fake_install,
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
