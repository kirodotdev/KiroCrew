"""Tests for start_api_server and _register_mcp_routes.

Ensures --slack-only mode has working MCP tool endpoints (spawn, lessons,
crons, taskrunner, send-message, notifications).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.server import _register_mcp_routes
from kiro_crew.dashboard.state import DashboardState


def _make_state(tmp_path, **kwargs):
    """DashboardState with mocked services (mirrors --slack-only init)."""
    monkeypatch_dir = tmp_path
    import kiro_crew.dashboard.state as _st

    orig = _st.config_dir
    _st.config_dir = lambda: monkeypatch_dir
    try:
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]),
                status=MagicMock(return_value={}),
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            **kwargs,
        )
    finally:
        _st.config_dir = orig
    return state


def _make_api_app(state: DashboardState) -> web.Application:
    """Minimal app using only _register_mcp_routes (same as start_api_server)."""
    app = web.Application()
    app["state"] = state
    app["port"] = 5476
    _register_mcp_routes(app)
    return app


class TestRegisterMcpRoutes:
    """Verify _register_mcp_routes registers all expected endpoints."""

    def test_all_mcp_routes_registered(self, tmp_path):
        state = _make_state(tmp_path)
        app = _make_api_app(state)
        routes = {(r.method, r.resource.canonical) for r in app.router.routes()}
        expected = {
            ("POST", "/api/spawn"),
            ("GET", "/api/spawn"),
            ("GET", "/api/spawn/{agent_id}"),
            ("DELETE", "/api/spawn/{agent_id}"),
            ("DELETE", "/api/spawn"),
            ("GET", "/api/lessons"),
            ("POST", "/api/lessons"),
            ("DELETE", "/api/lessons"),
            ("GET", "/api/crons"),
            ("POST", "/api/crons"),
            ("DELETE", "/api/crons/{job_id}"),
            ("POST", "/api/crons/{job_id}/enable"),
            ("POST", "/api/crons/{job_id}/ack"),
            ("GET", "/api/taskrunner"),
            ("POST", "/api/taskrunner"),
            ("POST", "/api/taskrunner/cancel"),
            ("POST", "/api/send-message"),
            ("GET", "/api/notifications"),
            ("POST", "/api/notifications/clear"),
        }
        assert expected.issubset(routes), f"Missing routes: {expected - routes}"


class TestApiServerSpawn:
    """Spawn endpoints work through the API-only server."""

    @pytest.mark.asyncio
    async def test_spawn_returns_503_without_subagent_mgr(self, tmp_path):
        state = _make_state(tmp_path, subagents=None)
        async with TestClient(TestServer(_make_api_app(state))) as client:
            resp = await client.post("/api/spawn", json={"task": "hello"})
            assert resp.status == 503
            data = await resp.json()
            assert "not available" in data["error"]

    @pytest.mark.asyncio
    async def test_spawn_succeeds_with_subagent_mgr(self, tmp_path):
        mock_mgr = MagicMock()
        mock_mgr.spawn.return_value = MagicMock(id="test-123", done=False, error="")
        state = _make_state(tmp_path, subagents=mock_mgr)
        async with TestClient(TestServer(_make_api_app(state))) as client:
            resp = await client.post("/api/spawn", json={"task": "say hello"})
            assert resp.status == 200
            data = await resp.json()
            assert data["id"] == "test-123"
            assert data["status"] == "spawned"

    @pytest.mark.asyncio
    async def test_spawn_passes_max_turns(self, tmp_path):
        mock_mgr = MagicMock()
        mock_mgr.spawn.return_value = MagicMock(id="test-456", done=False, error="")
        state = _make_state(tmp_path, subagents=mock_mgr)
        async with TestClient(TestServer(_make_api_app(state))) as client:
            resp = await client.post("/api/spawn", json={"task": "hi", "max_turns": 50})
            assert resp.status == 200
            assert mock_mgr.spawn.call_args.kwargs.get("max_turns") == 50

    @pytest.mark.asyncio
    async def test_spawn_list_empty(self, tmp_path):
        mock_mgr = MagicMock()
        mock_mgr.list.return_value = []
        state = _make_state(tmp_path, subagents=mock_mgr)
        async with TestClient(TestServer(_make_api_app(state))) as client:
            resp = await client.get("/api/spawn")
            assert resp.status == 200


class TestApiServerLessons:
    """Lesson endpoints work through the API-only server."""

    @pytest.mark.asyncio
    async def test_lessons_get(self, tmp_path):
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_api_app(state))) as client:
            resp = await client.get("/api/lessons")
            assert resp.status == 200


class TestApiServerCrons:
    """Cron endpoints work through the API-only server."""

    @pytest.mark.asyncio
    async def test_crons_get(self, tmp_path):
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_api_app(state))) as client:
            resp = await client.get("/api/crons")
            assert resp.status == 200


class TestApiServerSendMessage:
    """send-message endpoint works through the API-only server."""

    @pytest.mark.asyncio
    async def test_send_message_without_slack(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path, slack_client=None)
        async with TestClient(TestServer(_make_api_app(state))) as client:
            resp = await client.post("/api/send-message", json={"text": "hello"})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["slack"] is False


class TestApiServerNoUiRoutes:
    """API-only server must NOT have dashboard UI routes."""

    @pytest.mark.asyncio
    async def test_no_index_route(self, tmp_path):
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_api_app(state))) as client:
            resp = await client.get("/")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_no_static_route(self, tmp_path):
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_api_app(state))) as client:
            resp = await client.get("/static/foo.js")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_no_websocket_route(self, tmp_path):
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_api_app(state))) as client:
            resp = await client.get("/api/ws")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_no_chat_route(self, tmp_path):
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_api_app(state))) as client:
            resp = await client.post("/api/chat", json={})
            assert resp.status == 404


class TestStartApiServerWiring:
    """Integration test: start_api_server installs middleware and hook store."""

    @pytest.mark.asyncio
    async def test_server_has_audit_middleware_and_hook_store(self, tmp_path, monkeypatch):
        import kiro_crew.config.loader as _loader
        import kiro_crew.dashboard.server as _srv
        import kiro_crew.dashboard.state as _st

        # start_api_server now persists .local_secret via server.config_dir and
        # warms the token_auth revoked-nonce store via loader.config_dir; patch
        # all three sites so the test never writes the real ~/.kirocrew secret.
        monkeypatch.setattr(_st, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(_srv, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(_loader, "config_dir", lambda: tmp_path)

        from kiro_crew.dashboard.server import start_api_server

        runner, state = await start_api_server(
            sessions=MagicMock(count=0),
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]),
                status=MagicMock(return_value={}),
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            port=0,
        )
        try:
            assert state._hook_store is not None
            assert len(runner.app.middlewares) > 0
            # Auth parity with start_dashboard: token_auth_middleware MUST be
            # mounted on the headless server, not just the SEL audit logger.
            assert any(
                getattr(mw, "_is_token_auth", False) for mw in runner.app.middlewares
            ), "start_api_server must mount token_auth_middleware"
        finally:
            await runner.cleanup()


class TestApiServerAuth:
    """--slack-only headless mode must authenticate MCP tool routes at parity
    with the default dashboard mode.

    The gateway binds loopback, but loopback is NOT a trust boundary: local
    port forwarders and any web page the user opens can reach 127.0.0.1. So
    state-changing MCP routes must require the ``X-Internal-Secret`` handshake
    (machine-to-machine) exactly as ``start_dashboard`` enforces it.
    """

    async def _start(self, tmp_path, monkeypatch, **kwargs):
        import kiro_crew.config.loader as _loader
        import kiro_crew.dashboard.server as _srv
        import kiro_crew.dashboard.state as _st

        # Route config_dir() into the test's tmp dir at the sites that resolve it
        # here:
        #  - server.py binds ``from kiro_crew.config import config_dir`` at module
        #    scope (its own name) — used for the .local_secret write.
        #  - state.py binds ``from kiro_crew.config.loader import config_dir``.
        #  - token_auth.py imports ``config_dir`` LAZILY inside functions from
        #    kiro_crew.config.loader, so patch the real ``loader.config_dir``.
        # All three are REAL module attributes (loader has no __getattr__), so
        # monkeypatch restores them cleanly. Do NOT patch
        # ``kiro_crew.config.config_dir`` — that name is served by a PEP-562 lazy
        # ``__getattr__`` (not a real __dict__ entry), so setattr there pollutes
        # the package dict and leaks across tests.
        monkeypatch.setattr(_srv, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(_st, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(_loader, "config_dir", lambda: tmp_path)

        from kiro_crew.dashboard.server import start_api_server

        runner, state = await start_api_server(
            sessions=MagicMock(count=0),
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]),
                status=MagicMock(return_value={}),
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            port=0,
            **kwargs,
        )
        addrs = runner.addresses
        base = f"http://127.0.0.1:{addrs[0][1]}"
        return runner, state, base

    @pytest.mark.asyncio
    async def test_get_crons_denied_without_secret(self, tmp_path, monkeypatch):
        import aiohttp

        runner, _state, base = await self._start(tmp_path, monkeypatch)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/api/crons") as resp:
                    assert resp.status == 403
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_get_spawn_denied_without_secret(self, tmp_path, monkeypatch):
        import aiohttp

        runner, _state, base = await self._start(tmp_path, monkeypatch)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/api/spawn") as resp:
                    assert resp.status == 403
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_post_spawn_denied_without_secret(self, tmp_path, monkeypatch):
        import aiohttp

        mock_mgr = MagicMock()
        mock_mgr.spawn.return_value = MagicMock(id="x", done=False, error="")
        runner, _state, base = await self._start(tmp_path, monkeypatch, subagents=mock_mgr)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base}/api/spawn",
                    json={"task": "noop", "approval_mode": "auto"},
                ) as resp:
                    assert resp.status == 403
            # The denied request must never reach the handler.
            assert not mock_mgr.spawn.called
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_post_lessons_denied_without_secret(self, tmp_path, monkeypatch):
        import aiohttp

        runner, _state, base = await self._start(tmp_path, monkeypatch)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{base}/api/lessons", json={"text": "injected"}) as resp:
                    assert resp.status == 403
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_get_crons_allowed_with_secret(self, tmp_path, monkeypatch):
        import aiohttp

        runner, _state, base = await self._start(tmp_path, monkeypatch)
        try:
            secret = (tmp_path / ".local_secret").read_text().strip()
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{base}/api/crons",
                    headers={"X-Internal-Secret": secret},
                ) as resp:
                    assert resp.status == 200
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_get_crons_denied_with_wrong_secret(self, tmp_path, monkeypatch):
        import aiohttp

        runner, _state, base = await self._start(tmp_path, monkeypatch)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{base}/api/crons",
                    headers={"X-Internal-Secret": "deadbeef"},
                ) as resp:
                    assert resp.status == 403
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_local_secret_file_written(self, tmp_path, monkeypatch):
        runner, state, _base = await self._start(tmp_path, monkeypatch)
        try:
            # Parity with start_dashboard: the secret is exposed on the app and
            # persisted so in-repo MCP callers can read it.
            assert (tmp_path / ".local_secret").is_file()
            assert runner.app["local_secret"]
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_artifact_folders_denied_without_secret(self, tmp_path, monkeypatch):
        # /api/artifact-folders is called by MCP tools (_get/_post w/ secret) AND
        # the browser — classified as a mixed internal path. Must deny an
        # unauthenticated caller.
        import aiohttp

        runner, _state, base = await self._start(tmp_path, monkeypatch)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/api/artifact-folders") as resp:
                    assert resp.status == 403
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_artifact_folders_allowed_with_secret(self, tmp_path, monkeypatch):
        # The MCP artifact-folder tools authenticate via X-Internal-Secret; the
        # request must reach the handler (not be 403'd by auth) with the secret.
        import aiohttp

        runner, _state, base = await self._start(tmp_path, monkeypatch)
        try:
            secret = (tmp_path / ".local_secret").read_text().strip()
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{base}/api/artifact-folders",
                    headers={"X-Internal-Secret": secret},
                ) as resp:
                    # Auth passed → handler reached (200). NOT 403 (auth reject).
                    assert resp.status != 403
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_slack_profile_denied_without_secret(self, tmp_path, monkeypatch):
        # /api/slack-profile is MCP-only (no browser caller) — a strict internal
        # path. Unauthenticated callers are denied at the auth layer.
        import aiohttp

        runner, _state, base = await self._start(tmp_path, monkeypatch)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{base}/api/slack-profile", json={"user": "U123"}) as resp:
                    assert resp.status == 403
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_cross_origin_post_denied_by_csrf(self, tmp_path, monkeypatch):
        # A browser CSRF attempt (cross-site Origin on a state-changing request)
        # is rejected by csrf_middleware before token auth even runs, even if it
        # somehow carried the secret.
        import aiohttp

        runner, _state, base = await self._start(tmp_path, monkeypatch)
        try:
            secret = (tmp_path / ".local_secret").read_text().strip()
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base}/api/spawn",
                    json={"task": "noop"},
                    headers={
                        "Origin": "https://evil.example.com",
                        "X-Internal-Secret": secret,
                    },
                ) as resp:
                    assert resp.status == 403
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_csrf_denial_is_audited(self, tmp_path, monkeypatch):
        # A CSRF rejection is a security-relevant permission decision and must be
        # written to the SEL audit log. ``sel`` is the process-wide singleton
        # (keyed on the home dir, not config_dir), so assert on the call rather
        # than reading a file.
        import aiohttp

        import kiro_crew.dashboard.server as _srv

        fake_sel = MagicMock()
        monkeypatch.setattr(_srv, "sel", lambda: fake_sel)

        runner, _state, base = await self._start(tmp_path, monkeypatch)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base}/api/spawn",
                    json={"task": "noop"},
                    headers={"Origin": "https://evil.example.com"},
                ) as resp:
                    assert resp.status == 403
            denied = [
                c
                for c in fake_sel.log_api_access.call_args_list
                if c.kwargs.get("outcome") == "denied"
                and "CSRF check failed" in c.kwargs.get("error", "")
            ]
            assert denied, "CSRF denial was not logged to SEL"
        finally:
            await runner.cleanup()


class TestApiKirocrewConfig:
    """Tests for PUT /api/config/kirocrew inline validation."""

    @staticmethod
    def _make_app(tmp_path):
        from kiro_crew.dashboard import handlers

        app = web.Application()
        app.router.add_get("/api/config/kirocrew", handlers.api_kirocrew_config)
        app.router.add_put("/api/config/kirocrew", handlers.api_kirocrew_config)
        return app

    @pytest.mark.asyncio
    async def test_put_happy_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: MagicMock())
        (tmp_path / "config.json").write_text('{"agent": {"max_subagents": 3}}')
        async with TestClient(TestServer(self._make_app(tmp_path))) as c:
            resp = await c.put("/api/config/kirocrew", json={"agent": {"subagent_max_turns": 50}})
            assert resp.status == 200
            import json

            saved = json.loads((tmp_path / "config.json").read_text())
            assert saved["agent"]["subagent_max_turns"] == 50
            assert saved["agent"]["max_subagents"] == 3  # preserved

    @pytest.mark.asyncio
    async def test_put_rejects_bool(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: MagicMock())
        (tmp_path / "config.json").write_text('{"agent": {}}')
        async with TestClient(TestServer(self._make_app(tmp_path))) as c:
            resp = await c.put("/api/config/kirocrew", json={"agent": {"subagent_max_turns": True}})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_rejects_out_of_range(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: MagicMock())
        (tmp_path / "config.json").write_text('{"agent": {}}')
        async with TestClient(TestServer(self._make_app(tmp_path))) as c:
            resp = await c.put("/api/config/kirocrew", json={"agent": {"max_subagents": 999}})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_persists_subagent_auto_max(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: MagicMock())
        (tmp_path / "config.json").write_text('{"agent": {"max_subagents": 0}}')
        async with TestClient(TestServer(self._make_app(tmp_path))) as c:
            resp = await c.put("/api/config/kirocrew", json={"agent": {"subagent_auto_max": 32}})
            assert resp.status == 200
            import json

            saved = json.loads((tmp_path / "config.json").read_text())
            assert saved["agent"]["subagent_auto_max"] == 32

    @pytest.mark.asyncio
    async def test_put_rejects_subagent_auto_max_above_ceiling(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: MagicMock())
        (tmp_path / "config.json").write_text('{"agent": {}}')
        async with TestClient(TestServer(self._make_app(tmp_path))) as c:
            resp = await c.put("/api/config/kirocrew", json={"agent": {"subagent_auto_max": 9999}})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_rejects_subagent_auto_max_below_floor(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: MagicMock())
        (tmp_path / "config.json").write_text('{"agent": {}}')
        async with TestClient(TestServer(self._make_app(tmp_path))) as c:
            resp = await c.put("/api/config/kirocrew", json={"agent": {"subagent_auto_max": 1}})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_rejects_max_subagents_below_fixed_floor(self, tmp_path, monkeypatch):
        # A fixed pin of 1 or 2 is rejected: 0 (auto) is the only sub-3 value
        # allowed, and an explicit pin must be >= 3 (below that disables auto-sizing
        # and runs under the default).
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: MagicMock())
        (tmp_path / "config.json").write_text('{"agent": {}}')
        async with TestClient(TestServer(self._make_app(tmp_path))) as c:
            for bad in (1, 2):
                resp = await c.put("/api/config/kirocrew", json={"agent": {"max_subagents": bad}})
                assert resp.status == 400
            # 0 (auto) and a valid pin (>= 3) are accepted.
            for ok in (0, 3):
                resp = await c.put("/api/config/kirocrew", json={"agent": {"max_subagents": ok}})
                assert resp.status == 200

    @pytest.mark.asyncio
    async def test_put_auto_max_cannot_bypass_max_subagents_limit(self, tmp_path, monkeypatch):
        # Raising subagent_auto_max in the same request must NOT let max_subagents
        # exceed the absolute ceiling (64): a 9999 auto_max is rejected first, so
        # the persisted cap stays at the default 16 and max_subagents=9999 fails.
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: MagicMock())
        (tmp_path / "config.json").write_text('{"agent": {}}')
        async with TestClient(TestServer(self._make_app(tmp_path))) as c:
            resp = await c.put(
                "/api/config/kirocrew",
                json={"agent": {"subagent_auto_max": 9999, "max_subagents": 9999}},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_corrupt_persisted_auto_max_clamped_to_ceiling(self, tmp_path, monkeypatch):
        # A hand-edited/corrupt config with subagent_auto_max above the ceiling must
        # NOT be trusted to widen the bound: max_subagents is still capped at 64.
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: MagicMock())
        (tmp_path / "config.json").write_text('{"agent": {"subagent_auto_max": 9999}}')
        async with TestClient(TestServer(self._make_app(tmp_path))) as c:
            resp = await c.put("/api/config/kirocrew", json={"agent": {"max_subagents": 9999}})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_rejects_non_dict_agent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: MagicMock())
        async with TestClient(TestServer(self._make_app(tmp_path))) as c:
            resp = await c.put("/api/config/kirocrew", json={"agent": "not a dict"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_put_corrupt_config_returns_500(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: MagicMock())
        (tmp_path / "config.json").write_text("NOT JSON{{{")
        async with TestClient(TestServer(self._make_app(tmp_path))) as c:
            resp = await c.put("/api/config/kirocrew", json={"agent": {"subagent_max_turns": 50}})
            assert resp.status == 500

    @pytest.mark.asyncio
    async def test_put_rejects_unrecognized_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: MagicMock())
        (tmp_path / "config.json").write_text('{"agent": {}}')
        async with TestClient(TestServer(self._make_app(tmp_path))) as c:
            resp = await c.put("/api/config/kirocrew", json={"agent": {"unknown_key": 42}})
            assert resp.status == 400
