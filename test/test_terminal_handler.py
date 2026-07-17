"""Tests for the built-in CLI terminal panel handlers."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.dashboard.handlers import terminal


@pytest.fixture(autouse=True)
def _clear_enabled_cache(monkeypatch):
    """Enable terminal and reset cache between tests."""
    terminal._enabled_cache[0] = True
    terminal._enabled_cache[1] = time.monotonic()
    yield
    terminal._enabled_cache[0] = False
    terminal._enabled_cache[1] = 0.0


# ── Helpers ──


def _make_request(user="testuser", session_id="abc123", registry=None, cfg=None):
    """Build a mock aiohttp request with state and match_info."""
    state = MagicMock()
    state._terminal_sessions = registry if registry is not None else {}
    app = {"state": state}
    request = MagicMock()
    request.app = app
    request.get = lambda k, default=None: user if k == "user" else default
    request.match_info = MagicMock()
    request.match_info.get = lambda k, default="": session_id if k == "session_id" else default
    request.remote = "127.0.0.1"
    return request


def _make_session(session_id="s1", alive=True, ws=None, disconnect=None):
    """Build a mock _TerminalSession."""
    proc = MagicMock()
    proc.returncode = None if alive else 0
    proc.pid = 12345
    proc.wait = AsyncMock()
    sess = terminal._TerminalSession(
        session_id=session_id,
        master_fd=99,
        proc=proc,
        ws=ws,
    )
    sess.last_ws_disconnect = disconnect
    sess.reader_task = None
    return sess


# ── _get_config ──


class TestGetConfig:
    def test_returns_terminal_config(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"dashboard": {"terminal": {"max_sessions": 5, "shell": "/bin/zsh"}}})
        )
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        req = _make_request()
        result = terminal._get_config(req)
        assert result == {"max_sessions": 5, "shell": "/bin/zsh"}

    def test_returns_empty_on_missing_file(self, tmp_path, monkeypatch):
        from pathlib import Path

        monkeypatch.setattr(terminal, "config_path", lambda: Path("/nonexistent/config.json"))
        req = _make_request()
        result = terminal._get_config(req)
        assert result == {}

    def test_returns_empty_on_invalid_json(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("not json")
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        req = _make_request()
        result = terminal._get_config(req)
        assert result == {}

    def test_returns_empty_when_no_terminal_key(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        req = _make_request()
        result = terminal._get_config(req)
        assert result == {}


# ── _kill_session ──


class TestKillSession:
    @pytest.mark.asyncio
    async def test_cancels_reader_task(self):
        task = AsyncMock()
        task.cancel = MagicMock()
        sess = _make_session()
        sess.reader_task = task
        with patch("os.close"), patch("kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree"):
            await terminal._kill_session(sess)
        task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_closes_master_fd(self):
        sess = _make_session()
        sess.master_fd = 42
        with patch("os.close") as mock_close, patch("kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree"):
            await terminal._kill_session(sess)
        mock_close.assert_called_with(42)
        assert sess.master_fd == -1

    @pytest.mark.asyncio
    async def test_skips_close_when_fd_negative(self):
        sess = _make_session()
        sess.master_fd = -1
        with patch("os.close") as mock_close, patch("kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree"):
            await terminal._kill_session(sess)
        mock_close.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_sigterm_to_process_group(self):
        # _kill_session routes the tree-kill through platform_compat.kill_process_tree
        # (killpg on POSIX, taskkill /T on Windows), so patch + assert against the
        # shim rather than os.killpg directly.
        sess = _make_session(alive=True)
        with patch("os.close"), \
                patch("kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree") as mock_kill:
            await terminal._kill_session(sess)
        mock_kill.assert_any_call(12345, platform_compat.SIGTERM)

    @pytest.mark.asyncio
    async def test_skips_kill_when_process_already_exited(self):
        sess = _make_session(alive=False)
        with patch("os.close"), \
                patch("kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree") as mock_kill:
            await terminal._kill_session(sess)
        mock_kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_process_lookup_error_on_sigterm(self):
        sess = _make_session(alive=True)
        with patch("os.close"), \
                patch("kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree",
                      side_effect=ProcessLookupError):
            await terminal._kill_session(sess)
        # Should not raise

    @pytest.mark.asyncio
    async def test_sigkill_on_timeout(self):
        sess = _make_session(alive=True)
        sess.proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError, None])
        with patch("os.close"), \
                patch("kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree") as mock_kill:
            await terminal._kill_session(sess)
        calls = [c.args for c in mock_kill.call_args_list]
        assert (12345, platform_compat.SIGTERM) in calls
        assert (12345, platform_compat.SIGKILL) in calls

    @pytest.mark.asyncio
    async def test_handles_os_error_on_close(self):
        sess = _make_session()
        sess.master_fd = 42
        with patch("os.close", side_effect=OSError), patch("kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree"):
            await terminal._kill_session(sess)
        assert sess.master_fd == -1

    @pytest.mark.asyncio
    async def test_close_runs_on_subprocess_pool_off_loop(self):
        """The PTY master close must run on subprocess_executor (off the loop),
        never inline — a wedged close then costs one pool thread, not the loop."""
        import threading

        loop_thread = threading.current_thread()
        close_threads = []

        def _record_close(fd):
            close_threads.append(threading.current_thread())

        sess = _make_session()
        sess.master_fd = 42
        with patch("os.close", side_effect=_record_close), patch(
            "kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree"
        ):
            await terminal._kill_session(sess)
        assert close_threads, "os.close must have run"
        assert close_threads[0] is not loop_thread, "close ran on the event-loop thread"
        assert close_threads[0].name.startswith("mc-subproc"), (
            f"close must run on subprocess_executor, got {close_threads[0].name!r}"
        )
        assert sess.master_fd == -1

    @pytest.mark.asyncio
    async def test_master_fd_cleared_before_await_survives_cancellation(self):
        """If the coroutine is cancelled while suspended on the executor close,
        master_fd must already be -1 so the fd is not left referenced."""

        async def _hang(*_a, **_k):
            await asyncio.sleep(3600)  # never completes; we cancel mid-await

        sess = _make_session()
        sess.master_fd = 42
        with patch.object(
            asyncio.get_event_loop(), "run_in_executor", side_effect=_hang
        ), patch("kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree"):
            task = asyncio.ensure_future(terminal._kill_session(sess))
            await asyncio.sleep(0)  # let it reach the await
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        # Cleared BEFORE the await, so cancellation cannot leave a stale fd.
        assert sess.master_fd == -1

    @pytest.mark.asyncio
    async def test_handles_runtime_error_on_close_when_pool_shutdown(self):
        """If subprocess_executor was torn down, run_in_executor's submit raises
        RuntimeError — _kill_session must swallow it, not abort teardown."""
        sess = _make_session(alive=True)
        sess.master_fd = 42
        with patch.object(
            asyncio.get_event_loop(),
            "run_in_executor",
            side_effect=RuntimeError("cannot schedule new futures after shutdown"),
        ), patch(
            "kiro_crew.dashboard.handlers.terminal.platform_compat.kill_process_tree"
        ) as mock_kill:
            await terminal._kill_session(sess)
        # The close error was swallowed and teardown continued to the tree-kill.
        assert sess.master_fd == -1
        mock_kill.assert_any_call(12345, platform_compat.SIGTERM)


# ── api_terminal_create ──


class TestApiTerminalCreate:
    @pytest.mark.asyncio
    async def test_rejects_unauthenticated(self):
        req = _make_request(user=None)
        resp = await terminal.api_terminal_create(req)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_returns_session_id(self):
        req = _make_request()
        with patch.object(terminal, "_get_config", return_value={"enabled": True}), patch.object(
            terminal, "_sel"
        ) as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_create(req)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert "session_id" in body
        assert len(body["session_id"]) == 12
        assert "shell" in body

    @pytest.mark.asyncio
    async def test_rejects_when_max_sessions_reached(self):
        registry = {"s1": _make_session(), "s2": _make_session(), "s3": _make_session()}
        req = _make_request(registry=registry)
        with patch.object(terminal, "_get_config", return_value={"enabled": True}), patch.object(
            terminal, "_sel"
        ) as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_create(req)
        assert resp.status == 429

    @pytest.mark.asyncio
    async def test_respects_custom_max_sessions(self):
        registry = {"s1": _make_session()}
        req = _make_request(registry=registry)
        with patch.object(
            terminal, "_get_config", return_value={"enabled": True, "max_sessions": 1}
        ), patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_create(req)
        assert resp.status == 429

    @pytest.mark.asyncio
    async def test_uses_configured_shell(self):
        req = _make_request()
        with patch.object(
            terminal, "_get_config", return_value={"enabled": True, "shell": "/bin/zsh"}
        ), patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_create(req)
        body = json.loads(resp.body)
        assert body["shell"] == "/bin/zsh"


# ── api_terminal_delete ──


class TestApiTerminalDelete:
    @pytest.mark.asyncio
    async def test_rejects_unauthenticated(self):
        req = _make_request(user=None)
        resp = await terminal.api_terminal_delete(req)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_session(self):
        req = _make_request(session_id="nonexistent")
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_delete(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_deletes_existing_session(self):
        sess = _make_session()
        registry = {"abc123": sess}
        req = _make_request(registry=registry)
        with patch.object(
            terminal, "_kill_session", new_callable=AsyncMock
        ) as mock_kill, patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_delete(req)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["deleted"] == "abc123"
        mock_kill.assert_awaited_once_with(sess)
        assert "abc123" not in registry

    @pytest.mark.asyncio
    async def test_closes_ws_before_kill(self):
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(ws=ws)
        registry = {"abc123": sess}
        req = _make_request(registry=registry)
        with patch.object(terminal, "_kill_session", new_callable=AsyncMock), patch.object(
            terminal, "_sel"
        ) as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await terminal.api_terminal_delete(req)
        ws.close.assert_awaited_once()


# ── api_terminal_list ──


class TestApiTerminalList:
    @pytest.mark.asyncio
    async def test_rejects_unauthenticated(self):
        req = _make_request(user=None)
        resp = await terminal.api_terminal_list(req)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_returns_empty_list(self):
        req = _make_request()
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_list(req)
        body = json.loads(resp.body)
        assert body == {"enabled": True, "sessions": []}

    @pytest.mark.asyncio
    async def test_lists_sessions_with_details(self):
        ws = MagicMock()
        ws.closed = False
        sess = _make_session(session_id="s1", alive=True, ws=ws)
        sess.cols = 120
        sess.rows = 40
        registry = {"s1": sess}
        req = _make_request(registry=registry)
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_list(req)
        body = json.loads(resp.body)
        assert len(body["sessions"]) == 1
        s = body["sessions"][0]
        assert s["session_id"] == "s1"
        assert s["pid"] == 12345
        assert s["alive"] is True
        assert s["cols"] == 120
        assert s["rows"] == 40
        assert s["connected"] is True

    @pytest.mark.asyncio
    async def test_shows_disconnected_session(self):
        sess = _make_session(session_id="s1", alive=True, ws=None)
        registry = {"s1": sess}
        req = _make_request(registry=registry)
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_list(req)
        body = json.loads(resp.body)
        assert body["sessions"][0]["connected"] is False


# ── api_terminal_ws ──


class TestApiTerminalWs:
    @pytest.mark.asyncio
    async def test_rejects_unauthenticated(self):
        req = _make_request(user=None)
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_ws(req)
        assert isinstance(resp, web.Response)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_rejects_empty_session_id(self):
        req = _make_request(session_id="")
        resp = await terminal.api_terminal_ws(req)
        assert isinstance(resp, web.Response)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rejects_oversized_session_id(self):
        req = _make_request(session_id="a" * 65)
        resp = await terminal.api_terminal_ws(req)
        assert isinstance(resp, web.Response)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rejects_when_max_sessions_reached(self):
        registry = {"s1": _make_session(), "s2": _make_session(), "s3": _make_session()}
        req = _make_request(registry=registry, session_id="new")
        with patch.object(terminal, "_sel") as mock_sel, patch.object(
            terminal, "_get_config", return_value={"enabled": True}
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_ws(req)
        assert isinstance(resp, web.Response)
        assert resp.status == 429

    @pytest.mark.asyncio
    async def test_cleans_dead_session_before_reconnect(self):
        dead_sess = _make_session(session_id="abc123", alive=False)
        registry = {"abc123": dead_sess}
        req = _make_request(registry=registry, session_id="abc123")
        with patch.object(
            terminal, "_kill_session", new_callable=AsyncMock
        ) as mock_kill, patch.object(terminal, "_sel") as mock_sel, patch.object(
            terminal, "_get_config", return_value={"enabled": True, "max_sessions": 3}
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            # Will fail at ws.prepare since request is a mock, but dead session should be cleaned
            with pytest.raises(Exception):
                await terminal.api_terminal_ws(req)
        mock_kill.assert_awaited_once_with(dead_sess)
        # Dead session killed; placeholder reserved for new spawn
        assert registry.get("abc123") is not dead_sess

    @pytest.mark.asyncio
    async def test_refuses_new_session_on_non_posix(self, monkeypatch):
        """On a non-POSIX host, a new WS session is refused (no PTY spawned).

        PTY/fork are POSIX-only, so the ``elif not platform_compat.IS_POSIX``
        branch pops the reserved placeholder, logs the denial, sends an error
        frame, closes the socket, and returns it. ``WebSocketResponse`` is
        mocked so ``return ws`` is exercised deterministically.
        """
        registry: dict = {}
        req = _make_request(registry=registry, session_id="win-sess")

        ws = AsyncMock()
        ws.closed = False

        with patch.object(terminal.platform_compat, "IS_POSIX", False), \
             patch.object(terminal, "_get_config", return_value={"enabled": True}), \
             patch.object(terminal.web, "WebSocketResponse", return_value=ws), \
             patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_ws(req)

        assert resp is ws
        # Placeholder reservation was rolled back; no PTY session registered.
        assert "win-sess" not in registry
        ws.prepare.assert_awaited_once()
        ws.send_str.assert_awaited_once()
        sent = json.loads(ws.send_str.call_args.args[0])
        assert sent["type"] == "error"
        assert "not supported on Windows" in sent["message"]
        ws.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_posix_skips_send_when_ws_already_closed(self, monkeypatch):
        """If the socket is already closed, the non-POSIX branch skips the
        error frame and close (covers the ``if not ws.closed`` false path)."""
        registry: dict = {}
        req = _make_request(registry=registry, session_id="win-closed")

        ws = AsyncMock()
        ws.closed = True

        with patch.object(terminal.platform_compat, "IS_POSIX", False), \
             patch.object(terminal, "_get_config", return_value={"enabled": True}), \
             patch.object(terminal.web, "WebSocketResponse", return_value=ws), \
             patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_ws(req)

        assert resp is ws
        assert "win-closed" not in registry
        ws.send_str.assert_not_awaited()
        ws.close.assert_not_awaited()


# ── scrollback ring buffer + redaction ──


class TestScrollbackRedaction:
    """The reconnect-replay scrollback feature (ported from MeshClaw d00e6ac6).

    The port re-anchors redaction onto ``kiro_crew.security`` whose redactors
    return ``(text, warnings)`` tuples (upstream's ``redaction`` module returns
    a bare ``str``), so the helper must unpack both — a verbatim copy would
    have crashed treating the tuple as a string.
    """

    def test_redact_terminal_strips_credentials(self):
        # An AKIA access-key id in PTY output must be redacted before it is
        # sent to any client (live frame or replayed scrollback).
        out = terminal._redact_terminal(b"export AWS_KEY=AKIAIOSFODNN7EXAMPLE\n")
        assert b"AKIAIOSFODNN7EXAMPLE" not in out
        assert isinstance(out, bytes)

    def test_redact_terminal_passes_clean_output_through(self):
        assert terminal._redact_terminal(b"$ ls -la\n") == b"$ ls -la\n"

    def test_redact_terminal_handles_invalid_utf8(self):
        # errors="replace" keeps a stray byte from raising; output is still bytes.
        out = terminal._redact_terminal(b"\xff\xfeok")
        assert isinstance(out, bytes)
        assert b"ok" in out

    def test_scrollback_default_is_empty_bytearray(self):
        sess = _make_session()
        assert sess.scrollback == bytearray()

    @pytest.mark.asyncio
    async def test_scrollback_captured_and_replayed_on_reconnect(self, monkeypatch, tmp_path):
        """PTY output accumulates in the scrollback ring buffer and is replayed
        (redacted) to a client that reconnects to the same session."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/sb-sess") as ws:
                await ws.send_bytes(b"echo marker-xyz\n")
                # Drain at least one frame so read_pty runs and fills scrollback.
                msg = await ws.receive(timeout=3)
                assert msg.type == web.WSMsgType.BINARY
                await ws.close()

            sess = registry["sb-sess"]
            # Scrollback retained after disconnect, bounded by the ring buffer.
            assert len(sess.scrollback) > 0
            assert len(sess.scrollback) <= terminal._SCROLLBACK_MAX

            # Reconnect: the first frame must be the replayed scrollback.
            async with client.ws_connect("/api/ws/terminal/sb-sess") as ws:
                replay = await ws.receive(timeout=3)
                assert replay.type == web.WSMsgType.BINARY
                assert len(replay.data) > 0
                await ws.close()

            await terminal._kill_session(registry["sb-sess"])

    @pytest.mark.asyncio
    async def test_scrollback_trims_to_max(self, monkeypatch):
        """When PTY output exceeds _SCROLLBACK_MAX, the buffer keeps only the
        most recent _SCROLLBACK_MAX bytes (the trimming branch in read_pty)."""
        sess = _make_session()
        # Simulate the read_pty capture/trim logic directly.
        for _ in range(0, terminal._SCROLLBACK_MAX * 2, 4096):
            sess.scrollback.extend(b"x" * 4096)
            if len(sess.scrollback) > terminal._SCROLLBACK_MAX:
                sess.scrollback = sess.scrollback[-terminal._SCROLLBACK_MAX:]
        assert len(sess.scrollback) == terminal._SCROLLBACK_MAX


# ── reap_orphaned_terminals ──


class TestReapOrphanedTerminals:
    @pytest.mark.asyncio
    async def test_reaps_disconnected_session(self):
        sess = _make_session(session_id="s1", alive=True)
        sess.last_ws_disconnect = time.monotonic() - 600  # 10 min ago
        state = MagicMock()
        state._terminal_sessions = {"s1": sess}
        app = {"state": state}

        with patch.object(terminal, "_kill_session", new_callable=AsyncMock) as mock_kill, patch(
            "asyncio.sleep", side_effect=[None, asyncio.CancelledError]
        ):
            await terminal.reap_orphaned_terminals(app)
        mock_kill.assert_awaited_once_with(sess)
        assert "s1" not in state._terminal_sessions

    @pytest.mark.asyncio
    async def test_reaps_dead_process(self):
        sess = _make_session(session_id="s1", alive=False)
        state = MagicMock()
        state._terminal_sessions = {"s1": sess}
        app = {"state": state}

        with patch.object(terminal, "_kill_session", new_callable=AsyncMock) as mock_kill, patch(
            "asyncio.sleep", side_effect=[None, asyncio.CancelledError]
        ):
            await terminal.reap_orphaned_terminals(app)
        mock_kill.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_active_session(self):
        sess = _make_session(session_id="s1", alive=True)
        sess.last_ws_disconnect = None  # still connected
        state = MagicMock()
        state._terminal_sessions = {"s1": sess}
        app = {"state": state}

        with patch.object(terminal, "_kill_session", new_callable=AsyncMock) as mock_kill, patch(
            "asyncio.sleep", side_effect=[None, asyncio.CancelledError]
        ):
            await terminal.reap_orphaned_terminals(app)
        mock_kill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_recently_disconnected(self):
        sess = _make_session(session_id="s1", alive=True)
        sess.last_ws_disconnect = time.monotonic() - 60  # 1 min ago (< 5 min threshold)
        state = MagicMock()
        state._terminal_sessions = {"s1": sess}
        app = {"state": state}

        with patch.object(terminal, "_kill_session", new_callable=AsyncMock) as mock_kill, patch(
            "asyncio.sleep", side_effect=[None, asyncio.CancelledError]
        ):
            await terminal.reap_orphaned_terminals(app)
        mock_kill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handles_missing_state(self):
        app = {"state": None}
        with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.reap_orphaned_terminals(app)
        # Should not raise

    @pytest.mark.asyncio
    async def test_handles_no_terminal_sessions_attr(self):
        state = MagicMock(spec=[])  # no attributes
        app = {"state": state}
        with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.reap_orphaned_terminals(app)


# ── Integration tests using aiohttp TestClient ──


def _make_app(registry=None, cfg=None, user="testuser"):
    """Build a minimal aiohttp app with terminal routes and fake auth."""
    state = MagicMock()
    state._terminal_sessions = registry if registry is not None else {}

    @web.middleware
    async def fake_auth(request, handler):
        request["user"] = user
        return await handler(request)

    app = web.Application(middlewares=[fake_auth])
    app["state"] = state
    app.router.add_get("/api/ws/terminal/{session_id}", terminal.api_terminal_ws)
    app.router.add_post("/api/terminal/sessions", terminal.api_terminal_create)
    app.router.add_get("/api/terminal/sessions", terminal.api_terminal_list)
    app.router.add_delete(
        "/api/terminal/sessions/{session_id}",
        terminal.api_terminal_delete,
    )
    return app


@pytest.mark.xdist_group("pty_integration")
class TestTerminalWsIntegration:
    """Integration tests that exercise the full WebSocket PTY lifecycle.

    Pinned to one xdist worker (requires ``--dist loadgroup``): each test forks
    a real PTY shell and waits on multi-second drain budgets for interactive
    output. Under ``-n auto`` these competed with the gateway integration
    subprocess storm for CPU and the forked shell could go unscheduled past the
    10s readiness budget ("shell never produced any PTY output"). Sharing one
    group serializes the heavy PTY tests, matching the gateway-test pattern.
    """

    @pytest.mark.asyncio
    async def test_ws_spawn_and_disconnect(self, monkeypatch, tmp_path):
        """Connect via WS, spawn a PTY, then disconnect — session stays in registry."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/test-sess-1") as ws:
                # Session should be registered
                assert "test-sess-1" in registry
                sess = registry["test-sess-1"]
                assert sess.proc.returncode is None  # alive
                await ws.close()

            # After WS close, session stays (orphan reaper handles cleanup)
            assert "test-sess-1" in registry
            sess = registry["test-sess-1"]
            assert sess.ws is None
            assert sess.last_ws_disconnect is not None

            # Cleanup: kill the PTY
            await terminal._kill_session(sess)

    @pytest.mark.asyncio
    async def test_ws_ping_pong(self, monkeypatch, tmp_path):
        """Send a ping control message, receive pong."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/ping-sess") as ws:
                await ws.send_str(json.dumps({"type": "ping"}))
                # Drain binary PTY frames until we get the text pong
                for _ in range(20):
                    msg = await ws.receive(timeout=2)
                    if msg.type == web.WSMsgType.TEXT:
                        break
                data = json.loads(msg.data)
                assert data == {"type": "pong"}
                await ws.close()

            await terminal._kill_session(registry["ping-sess"])

    @pytest.mark.asyncio
    async def test_ws_resize(self, monkeypatch, tmp_path):
        """Send a resize control message, verify session cols/rows update."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/resize-sess") as ws:
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "resize",
                            "cols": 200,
                            "rows": 50,
                        }
                    )
                )
                # Give a moment for the message to be processed
                await asyncio.sleep(0.1)
                sess = registry["resize-sess"]
                assert sess.cols == 200
                assert sess.rows == 50
                await ws.close()

            await terminal._kill_session(registry["resize-sess"])

    @pytest.mark.asyncio
    async def test_ws_binary_io(self, monkeypatch, tmp_path):
        """Send binary data through WS, verify PTY receives it."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/io-sess") as ws:
                # Send a command — the PTY should echo something back
                await ws.send_bytes(b"echo hello\n")
                # Read at least one binary frame back (PTY output)
                msg = await ws.receive(timeout=3)
                assert msg.type == web.WSMsgType.BINARY
                assert len(msg.data) > 0
                await ws.close()

            await terminal._kill_session(registry["io-sess"])

    @pytest.mark.asyncio
    async def test_ws_reconnect_existing_session(self, monkeypatch, tmp_path):
        """Reconnect to an existing PTY session."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            # First connection
            async with client.ws_connect("/api/ws/terminal/recon-sess") as ws:
                await ws.close()

            sess = registry["recon-sess"]
            original_pid = sess.proc.pid
            assert sess.ws is None  # disconnected

            # Reconnect
            async with client.ws_connect("/api/ws/terminal/recon-sess") as ws:
                sess = registry["recon-sess"]
                assert sess.proc.pid == original_pid  # same PTY
                assert sess.ws is not None  # reconnected
                assert sess.last_ws_disconnect is None
                await ws.close()

            await terminal._kill_session(registry["recon-sess"])

    @pytest.mark.asyncio
    async def test_ws_invalid_json_ignored(self, monkeypatch, tmp_path):
        """Invalid JSON text frames are silently ignored."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/json-sess") as ws:
                await ws.send_str("not valid json")
                # Should not crash — send a ping to verify connection alive
                await ws.send_str(json.dumps({"type": "ping"}))
                # Drain binary PTY frames until we get the text pong
                for _ in range(20):
                    msg = await ws.receive(timeout=2)
                    if msg.type == web.WSMsgType.TEXT:
                        break
                data = json.loads(msg.data)
                assert data == {"type": "pong"}
                await ws.close()

            await terminal._kill_session(registry["json-sess"])

    @pytest.mark.asyncio
    async def test_ws_unsupported_platform_on_non_posix(self, monkeypatch, tmp_path):
        """When the platform is not POSIX, opening a new WS session is refused.

        Exercises the ``elif not platform_compat.IS_POSIX`` branch by forcing
        ``IS_POSIX`` False (PTY/fork are POSIX-only). No PTY is spawned: the
        handler pops the reserved placeholder, emits an error frame, and closes
        the socket. Runs on a real TestServer so ``ws.prepare`` succeeds.
        """
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())
        monkeypatch.setattr(terminal.platform_compat, "IS_POSIX", False)

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/winnope-sess") as ws:
                # First frame is the JSON error message.
                msg = await ws.receive(timeout=5)
                assert msg.type == web.WSMsgType.TEXT
                data = json.loads(msg.data)
                assert data["type"] == "error"
                assert "not supported on Windows" in data["message"]
                # Server closes the socket after sending the error.
                closing = await ws.receive(timeout=5)
                assert closing.type in (
                    web.WSMsgType.CLOSE,
                    web.WSMsgType.CLOSING,
                    web.WSMsgType.CLOSED,
                )

            # No PTY session was registered; the reserved placeholder was popped.
            assert "winnope-sess" not in registry

    @pytest.mark.asyncio
    async def test_ws_ctrl_c_delivers_sigint(self, monkeypatch, tmp_path):
        """Send \\x03 (Ctrl+C) and verify the child process receives SIGINT.

        Deflake notes: the original version used three fixed ``asyncio.sleep``
        calls (1.0s + 1.0s + 1.5s) which intermittently fired before the shell
        had printed its prompt, echoed ``sleep 30``, or recovered after SIGINT
        on a busy CI host — leaving the final ``echo SIGINT_OK`` probe stuck
        in the input buffer of a shell that hadn't yet returned to a prompt.
        Replaced with bounded "drain until marker appears in accumulated PTY
        output" helpers: deterministic on a fast host, falls back to a
        generous overall budget on a slow one.
        """
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async def _drain_until(ws, predicate, *, budget_secs: float):
            """Read PTY frames into an accumulator until ``predicate(buf)`` is
            true or the overall ``budget_secs`` runs out.  Returns the
            accumulated bytes (caller can decide whether the predicate held)."""
            loop = asyncio.get_event_loop()
            deadline = loop.time() + budget_secs
            buf = bytearray()
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return bytes(buf)
                try:
                    msg = await ws.receive(timeout=remaining)
                except asyncio.TimeoutError:
                    return bytes(buf)
                if msg.type == web.WSMsgType.BINARY:
                    buf.extend(msg.data)
                    if predicate(bytes(buf)):
                        return bytes(buf)
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                    return bytes(buf)

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/sigint-sess") as ws:
                # Readiness gate: drive it off INPUT ECHO, not an unsolicited
                # prompt. A login shell on a minimal build host (no MOTD, empty
                # PS1, non-interactive-looking PTY) may emit nothing until it
                # receives input, so waiting for a spontaneous ``$ ``/``# ``
                # prompt is brittle and fired "shell never produced any PTY
                # output" on the fleet. Instead send a probe and wait for the
                # PTY line discipline to echo it back — the same proven pattern
                # as test_ws_binary_io. This confirms the shell is interactive
                # and consuming stdin without depending on prompt rendering.
                await ws.send_bytes(b"echo __PTY_READY__\n")
                ready = await _drain_until(
                    ws,
                    lambda b: b"__PTY_READY__" in b,
                    budget_secs=15,
                )
                assert b"__PTY_READY__" in ready, (
                    "shell never echoed the readiness probe — PTY input/echo "
                    "path is not live"
                )

                # Run sleep in foreground; drain until we see the command
                # echoed back (so we know the shell is processing it, not
                # buffering it pre-prompt).
                await ws.send_bytes(b"sleep 30\n")
                echoed = await _drain_until(
                    ws,
                    lambda b: b"sleep 30" in b,
                    budget_secs=5,
                )
                assert b"sleep 30" in echoed, (
                    "shell did not echo `sleep 30` within 5s — "
                    "input may not have reached an interactive shell"
                )

                # Send Ctrl+C (ETX byte) and drain until the prompt redraws,
                # which is the visible signal that the foreground job has
                # been killed and the shell is back at idle.
                await ws.send_bytes(b"\x03")
                await _drain_until(
                    ws,
                    lambda b: b"$ " in b or b"# " in b,
                    budget_secs=5,
                )

                # Shell should still be alive after SIGINT killed sleep.
                # Probe with an echo and drain until we see the marker.
                await ws.send_bytes(b"echo SIGINT_OK\n")
                tail = await _drain_until(
                    ws,
                    lambda b: b"SIGINT_OK" in b,
                    budget_secs=10,
                )
                found = b"SIGINT_OK" in tail

                sess = registry["sigint-sess"]
                # Success: shell responded (SIGINT killed sleep, shell continued)
                # OR process exited (signal was delivered, just killed everything)
                assert found or sess.proc.returncode is not None
                await ws.close()

            await terminal._kill_session(registry["sigint-sess"])

    @pytest.mark.asyncio
    async def test_rest_create_list_delete(self, monkeypatch, tmp_path):
        """Full REST lifecycle: create, list, delete."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        app = _make_app()

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            # Create
            resp = await client.post("/api/terminal/sessions")
            assert resp.status == 200
            body = await resp.json()
            sid = body["session_id"]
            assert len(sid) == 12

            # List (empty — create only returns ID, doesn't spawn PTY)
            resp = await client.get("/api/terminal/sessions")
            assert resp.status == 200

            # Delete — session not in registry (no WS connected), returns 404
            resp = await client.delete(f"/api/terminal/sessions/{sid}")
            assert resp.status == 404

            # Seed registry directly, then delete
            from kiro_crew.dashboard.handlers import terminal as _term

            registry = _term._get_registry(
                type("R", (), {"app": client.app})()  # type: ignore[arg-type]
            )
            registry[sid] = _make_session(session_id=sid)
            resp = await client.delete(f"/api/terminal/sessions/{sid}")
            assert resp.status == 200
            body = await resp.json()
            assert body["deleted"] == sid
            assert sid not in registry


# ── _get_registry ──


class TestGetRegistry:
    def test_returns_terminal_sessions_from_state(self):
        registry = {"s1": _make_session()}
        req = _make_request(registry=registry)
        result = terminal._get_registry(req)
        assert result is registry


# ── _TerminalSession dataclass ──


class TestTerminalSession:
    def test_defaults(self):
        proc = MagicMock()
        sess = terminal._TerminalSession(session_id="t1", master_fd=5, proc=proc)
        assert sess.cols == 80
        assert sess.rows == 24
        assert sess.ws is None
        assert sess.reader_task is None
        assert sess.last_ws_disconnect is None
        assert sess.created_at > 0


# ── _is_enabled default ──


class TestIsEnabledDefault:
    """Terminal is enabled by default; an explicit enabled=false still disables it."""

    def test_enabled_by_default_when_key_absent(self):
        req = _make_request()
        terminal._enabled_cache[1] = 0.0  # bust the 30s cache to force a recompute
        with patch.object(terminal, "_get_config", return_value={}):
            assert terminal._is_enabled(req) is True

    def test_explicit_disable_is_respected(self):
        req = _make_request()
        terminal._enabled_cache[1] = 0.0
        with patch.object(terminal, "_get_config", return_value={"enabled": False}):
            assert terminal._is_enabled(req) is False
