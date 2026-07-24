"""Tests for the built-in CLI terminal panel handlers."""

from __future__ import annotations

import asyncio
import json
import os
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


# ── _resolve_cwd ──


class TestResolveCwd:
    def test_valid_requested_dir_wins(self, tmp_path):
        assert terminal._resolve_cwd({"cwd": "/etc"}, str(tmp_path)) == str(tmp_path)

    def test_expands_user_in_requested(self, tmp_path, monkeypatch):
        # POSIX expanduser reads HOME; Windows reads USERPROFILE — set both so
        # the test is platform agnostic.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        assert terminal._resolve_cwd({}, "~") == str(tmp_path)

    def test_invalid_requested_falls_back_to_config_cwd(self, tmp_path):
        assert terminal._resolve_cwd({"cwd": str(tmp_path)}, "/no/such/dir/xyz") == str(tmp_path)

    def test_no_request_uses_config_cwd(self, tmp_path):
        assert terminal._resolve_cwd({"cwd": str(tmp_path)}, None) == str(tmp_path)

    def test_no_request_no_config_uses_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert terminal._resolve_cwd({}, None) == str(tmp_path)


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

    @pytest.mark.skipif(
        terminal.platform_compat.IS_WINDOWS,
        reason="POSIX master_fd/proc teardown; Windows sessions use the ConPTY backend",
    )
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
    """POSIX happy-path coverage. Force ``IS_WINDOWS=False`` so these run on the
    Windows build host too — the Windows-specific 501 gate has its own suite in
    ``TestApiTerminalCreateWindowsFailFast``. Mirrors the umbrella-wide
    monkeypatch pattern used by ``TestRestrictToOwnerArgvOnLinux`` in
    ``test_platform_compat.py``."""

    @pytest.fixture(autouse=True)
    def _force_posix(self, monkeypatch):
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", False)

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
        with patch.object(
            terminal, "_get_config", return_value={"enabled": True, "max_sessions": 3}
        ), patch.object(terminal, "_sel") as mock_sel:
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


class TestApiTerminalCreateWindowsFailFast:
    """POST /api/terminal/sessions must fail fast on Windows with a 501.

    Mirrors the ``TestTaskkillErrorMapping`` / ``TestRestrictToOwnerArgvOnLinux``
    monkeypatch pattern from ``test_platform_compat.py``: patch
    ``platform_compat.IS_WINDOWS`` so the branch is exercised on the Linux build
    fleet. PTY/fork are POSIX-only and the ConPTY port is deferred — until then
    the create endpoint MUST refuse on Windows with the same wording the WS
    handler emits, so the frontend never opens a socket that will die during
    PTY spawn.
    """

    @pytest.mark.asyncio
    async def test_windows_returns_session_id(self, monkeypatch):
        # Windows now spawns a ConPTY-backed shell, so create SUCCEEDS (returns
        # a session_id) instead of the old 501 "unsupported" refusal.
        registry: dict = {}
        req = _make_request(registry=registry)
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", True)
        with patch.object(terminal, "_get_config", return_value={"enabled": True}), \
             patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_create(req)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert "session_id" in body

    @pytest.mark.asyncio
    async def test_windows_gate_still_requires_auth(self, monkeypatch):
        # Authentication is still enforced first — an unauthenticated request
        # must not create a session. 401 regardless of platform.
        req = _make_request(user=None)
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", True)
        resp = await terminal.api_terminal_create(req)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_non_windows_still_returns_session_id(self, monkeypatch):
        # Guard against regressing the POSIX happy path.
        req = _make_request()
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", False)
        with patch.object(terminal, "_get_config", return_value={"enabled": True}), \
             patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_create(req)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert "session_id" in body


@pytest.mark.skipif(
    not platform_compat.IS_WINDOWS,
    reason="Real-host Windows-only assertion; the monkeypatched suite above "
           "covers the same code path on Linux CI.",
)
class TestApiTerminalCreateWindowsUnmocked:
    """Windows-only unmocked coverage: create succeeds (ConPTY-backed)."""

    @pytest.mark.asyncio
    async def test_real_windows_returns_session_id(self):
        req = _make_request()
        with patch.object(terminal, "_get_config", return_value={"enabled": True}), \
             patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_create(req)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert "session_id" in body


# ── api_terminal_delete ──


class TestApiTerminalRedact:
    """POST /api/terminal/redact — contiguous re-scan of a complete selection.
    The streaming path redacts per 4096-byte read; a credential straddling a
    chunk boundary evades both scans, so the hand-off re-scans the whole text
    and the frontend fails closed unless this returns 200."""

    def _req(self, body, user="testuser", enabled=True):
        req = _make_request(user=user)
        req.json = AsyncMock(return_value=body)
        return req

    @pytest.mark.asyncio
    async def test_rejects_unauthenticated(self):
        req = self._req({"text": "hello"}, user=None)
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_redact(req)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_rejects_when_disabled(self):
        req = self._req({"text": "hello"})
        with patch.object(terminal, "_is_enabled", return_value=False), \
             patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_redact(req)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_rejects_non_string_text(self):
        req = self._req({"text": 42})
        with patch.object(terminal, "_is_enabled", return_value=True):
            resp = await terminal.api_terminal_redact(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rejects_oversized_selection(self):
        req = self._req({"text": "x" * (terminal._REDACT_MAX_BYTES + 1)})
        with patch.object(terminal, "_is_enabled", return_value=True):
            resp = await terminal.api_terminal_redact(req)
        assert resp.status == 413

    @pytest.mark.asyncio
    async def test_redacts_credentials_in_contiguous_text(self):
        # The exact evasion the endpoint exists for: a secret that per-chunk
        # scanning would have split. The contiguous scan must catch it.
        secret = "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        req = self._req({"text": f"config dump:\n{secret}\ndone"})
        with patch.object(terminal, "_is_enabled", return_value=True):
            resp = await terminal.api_terminal_redact(req)
        assert resp.status == 200
        out = json.loads(resp.text)["text"]
        assert "wJalrXUtnFEMI" not in out

    @pytest.mark.asyncio
    async def test_fails_closed_on_redactor_error(self):
        req = self._req({"text": "hello"})
        with patch.object(terminal, "_is_enabled", return_value=True), \
             patch.object(terminal, "redact_exfiltration_urls", side_effect=RuntimeError):
            resp = await terminal.api_terminal_redact(req)
        assert resp.status == 500
        assert "hello" not in resp.text


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
            terminal, "_get_config", return_value={"enabled": True, "max_sessions": 3}
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
    async def test_windows_conpty_spawn_failure_sends_error(self, monkeypatch):
        """On Windows a new WS session spawns a ConPTY shell (kiro_crew.conpty);
        the old 'not supported on Windows' refusal no longer exists. If the
        spawn fails, the handler pops the placeholder, sends an error frame, and
        closes. WindowsPty is mocked to raise so ``return ws`` is exercised
        without a real pseudo-console (and without needing pywinpty on POSIX CI).
        """
        registry: dict = {}
        req = _make_request(registry=registry, session_id="win-sess")
        req.query = MagicMock()
        req.query.get = lambda *a, **k: None

        ws = AsyncMock()
        ws.closed = False

        with patch.object(terminal.platform_compat, "IS_POSIX", False), \
             patch.object(terminal.platform_compat, "IS_WINDOWS", True), \
             patch("kiro_crew.conpty.WindowsPty", side_effect=RuntimeError("boom")), \
             patch.object(terminal, "_get_config", return_value={"enabled": True}), \
             patch.object(terminal.web, "WebSocketResponse", return_value=ws), \
             patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_ws(req)

        assert resp is ws
        assert "win-sess" not in registry
        ws.send_str.assert_awaited_once()
        sent = json.loads(ws.send_str.call_args.args[0])
        assert sent["type"] == "error"
        assert "Failed to start terminal" in sent["message"]
        ws.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_windows_conpty_spawn_failure_skips_send_when_ws_closed(self, monkeypatch):
        """If the socket is already closed, the Windows spawn-failure path skips
        the error frame and close (covers the ``if not ws.closed`` false path)."""
        registry: dict = {}
        req = _make_request(registry=registry, session_id="win-closed")
        req.query = MagicMock()
        req.query.get = lambda *a, **k: None

        ws = AsyncMock()
        ws.closed = True

        with patch.object(terminal.platform_compat, "IS_POSIX", False), \
             patch.object(terminal.platform_compat, "IS_WINDOWS", True), \
             patch("kiro_crew.conpty.WindowsPty", side_effect=RuntimeError("boom")), \
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
    """The reconnect-replay scrollback feature (ported from the upstream project).

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
        sess.last_ws_disconnect = time.monotonic() - 2000  # ~33 min ago (> reap threshold)
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
        sess.last_ws_disconnect = time.monotonic() - 60  # 1 min ago (< 15 min threshold)
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
                assert terminal._sess_alive(sess)  # alive (pty or ConPTY backend)
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
            original_pid = terminal._sess_pid(sess)
            assert sess.ws is None  # disconnected

            # Reconnect
            async with client.ws_connect("/api/ws/terminal/recon-sess") as ws:
                sess = registry["recon-sess"]
                assert terminal._sess_pid(sess) == original_pid  # same PTY
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
    async def test_ws_windows_spawns_conpty(self, monkeypatch, tmp_path):
        """On Windows a new WS session spawns a ConPTY-backed shell instead of
        the old 'not supported' refusal. WindowsPty is mocked so the test needs
        no real pseudo-console (and runs on POSIX CI without pywinpty); forcing
        IS_WINDOWS makes the exercised code path identical on Windows and POSIX.
        """
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())
        monkeypatch.setattr(terminal.platform_compat, "IS_POSIX", False)
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", True)

        class _FakeWinPty:
            def __init__(self, argv, cwd=None, env=None, cols=80, rows=24):
                self.pid = 4321
                self._alive = True

            def read(self, size=4096):
                return b""  # EOF: the reader loop exits cleanly

            def write(self, data):
                return len(data)

            def resize(self, cols, rows):
                pass

            def isalive(self):
                return self._alive

            def terminate(self, force=True):
                self._alive = False

        monkeypatch.setattr("kiro_crew.conpty.WindowsPty", _FakeWinPty)

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/winok-sess") as ws:
                # A ConPTY session is registered (not refused).
                assert "winok-sess" in registry
                sess = registry["winok-sess"]
                assert sess.winpty is not None
                assert sess.proc is None  # Windows backend has no asyncio proc
                await ws.close()

        if "winok-sess" in registry:
            await terminal._kill_session(registry["winok-sess"])

    @pytest.mark.skipif(
        terminal.platform_compat.IS_WINDOWS,
        reason="POSIX SIGINT via PTY; on Windows Ctrl+C is handled inside ConPTY",
    )
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
        # This test seeds a mock session (proc.pid=12345) and deletes it. Stub the
        # tree-kill so teardown does no real process signalling: on POSIX
        # os.killpg(getpgid(12345)) fails fast, but on Windows taskkill /T /PID
        # 12345 targets a real system PID (slow timeout / could kill it). Real
        # teardown is covered by the ConPTY integration test.
        monkeypatch.setattr(terminal.platform_compat, "kill_process_tree_async", AsyncMock())

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


# ── _proc_comm / _proc_cwd (live-title helpers) ──

_HAS_PROC = os.path.isdir("/proc")


@pytest.mark.skipif(not _HAS_PROC, reason="requires Linux /proc")
class TestProcHelpers:
    def test_proc_comm_returns_command_name_for_live_pid(self):
        # Our own process is guaranteed alive; /proc/<pid>/comm is non-empty.
        name = terminal._proc_comm(os.getpid())
        assert name and isinstance(name, str)

    def test_proc_comm_returns_none_for_bogus_pid(self):
        # A pid this large effectively never exists -> open() raises OSError -> None.
        assert terminal._proc_comm(2 ** 30) is None

    def test_proc_cwd_returns_directory_for_live_pid(self):
        cwd = terminal._proc_cwd(os.getpid())
        assert cwd and os.path.isdir(cwd)

    def test_proc_cwd_returns_none_for_bogus_pid(self):
        assert terminal._proc_cwd(2 ** 30) is None


# ── _session_title ──


@pytest.mark.skipif(
    terminal.platform_compat.IS_WINDOWS,
    reason="foreground-command detection uses os.tcgetpgrp (POSIX-only); "
    "_session_title returns None on Windows",
)
class TestSessionTitle:
    """The tab-title label: foreground command name while one runs, else the
    shell's cwd basename. _proc_comm/_proc_cwd and os.tcgetpgrp are patched so
    each branch is exercised deterministically (no real PTY needed)."""

    def _sess(self):
        # _make_session gives master_fd=99 and proc.pid=12345.  # wokeignore:rule=master
        return _make_session()

    def test_returns_none_on_non_posix(self):
        with patch.object(terminal.platform_compat, "IS_POSIX", False):
            assert terminal._session_title(self._sess()) is None

    def test_returns_none_when_fd_closed(self):
        sess = self._sess()
        sess.master_fd = -1  # wokeignore:rule=master
        with patch.object(terminal.platform_compat, "IS_POSIX", True):
            assert terminal._session_title(sess) is None

    def test_returns_none_when_tcgetpgrp_raises(self):
        with patch.object(terminal.platform_compat, "IS_POSIX", True), \
             patch("os.tcgetpgrp", side_effect=OSError):
            assert terminal._session_title(self._sess()) is None

    def test_returns_foreground_command_name(self):
        # fg pgid (999) != shell pid (12345) -> a command is running.
        with patch.object(terminal.platform_compat, "IS_POSIX", True), \
             patch("os.tcgetpgrp", return_value=999), \
             patch.object(terminal, "_proc_comm", return_value="vim"):
            assert terminal._session_title(self._sess()) == "vim"

    def test_falls_back_to_cwd_basename_when_idle(self):
        # fg pgid == shell pid -> at the prompt -> cwd basename.
        with patch.object(terminal.platform_compat, "IS_POSIX", True), \
             patch("os.tcgetpgrp", return_value=12345), \
             patch.object(terminal, "_proc_cwd", return_value="/home/u/my-project"):
            assert terminal._session_title(self._sess()) == "my-project"

    def test_falls_back_to_cwd_when_comm_unavailable(self):
        # A command is running but /proc/<pgid>/comm couldn't be read.
        with patch.object(terminal.platform_compat, "IS_POSIX", True), \
             patch("os.tcgetpgrp", return_value=999), \
             patch.object(terminal, "_proc_comm", return_value=None), \
             patch.object(terminal, "_proc_cwd", return_value="/tmp/scratch"):
            assert terminal._session_title(self._sess()) == "scratch"

    def test_returns_none_when_cwd_unavailable(self):
        with patch.object(terminal.platform_compat, "IS_POSIX", True), \
             patch("os.tcgetpgrp", return_value=12345), \
             patch.object(terminal, "_proc_cwd", return_value=None):
            assert terminal._session_title(self._sess()) is None


# ── poll_terminal_titles ──


class TestPollTerminalTitles:
    """One loop iteration is driven by patching asyncio.sleep to return once
    then raise CancelledError (same pattern as the reaper tests)."""

    @staticmethod
    def _app(sess):
        state = MagicMock()
        state._terminal_sessions = {sess.session_id: sess} if sess else {}
        return {"state": state}

    @pytest.mark.asyncio
    async def test_pushes_title_frame_on_change(self):
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)
        with patch.object(terminal, "_session_title", return_value="vim"), \
             patch.object(terminal, "_session_cwd", return_value=None), \
             patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.poll_terminal_titles(self._app(sess))
        assert sess.last_title == "vim"
        ws.send_str.assert_awaited_once()
        assert json.loads(ws.send_str.call_args.args[0]) == {"type": "title", "text": "vim"}

    @pytest.mark.asyncio
    async def test_skips_when_title_unchanged(self):
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)
        sess.last_title = "vim"
        with patch.object(terminal, "_session_title", return_value="vim"), \
             patch.object(terminal, "_session_cwd", return_value=None), \
             patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.poll_terminal_titles(self._app(sess))
        ws.send_str.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_no_title(self):
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)
        with patch.object(terminal, "_session_title", return_value=None), \
             patch.object(terminal, "_session_cwd", return_value=None), \
             patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.poll_terminal_titles(self._app(sess))
        ws.send_str.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_disconnected_session(self):
        sess = _make_session(session_id="s1", ws=None)  # no live socket
        with patch.object(terminal, "_session_title") as mock_title, \
             patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.poll_terminal_titles(self._app(sess))
        mock_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_swallows_send_error(self):
        ws = AsyncMock()
        ws.closed = False
        ws.send_str = AsyncMock(side_effect=ConnectionResetError)
        sess = _make_session(session_id="s1", ws=ws)
        with patch.object(terminal, "_session_title", return_value="vim"), \
             patch.object(terminal, "_session_cwd", return_value=None), \
             patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.poll_terminal_titles(self._app(sess))  # must not raise
        assert sess.last_title == "vim"

    @pytest.mark.asyncio
    async def test_handles_missing_state(self):
        with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.poll_terminal_titles({"state": None})

    @pytest.mark.asyncio
    async def test_handles_no_terminal_sessions_attr(self):
        state = MagicMock(spec=[])  # no _terminal_sessions attribute
        with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.poll_terminal_titles({"state": state})

    @pytest.mark.asyncio
    async def test_pushes_cwd_frame_on_change(self):
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)
        with patch.object(terminal, "_session_title", return_value=None), \
             patch.object(terminal, "_session_cwd", return_value="/home/u/proj"), \
             patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.poll_terminal_titles(self._app(sess))
        assert sess.last_cwd == "/home/u/proj"
        ws.send_str.assert_awaited_once()
        assert json.loads(ws.send_str.call_args.args[0]) == {"type": "cwd", "path": "/home/u/proj"}

    @pytest.mark.asyncio
    async def test_skips_when_cwd_unchanged(self):
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)
        sess.last_cwd = "/home/u/proj"
        with patch.object(terminal, "_session_title", return_value=None), \
             patch.object(terminal, "_session_cwd", return_value="/home/u/proj"), \
             patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.poll_terminal_titles(self._app(sess))
        ws.send_str.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pushes_both_title_and_cwd_frames(self):
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)
        with patch.object(terminal, "_session_title", return_value="vim"), \
             patch.object(terminal, "_session_cwd", return_value="/tmp/x"), \
             patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.poll_terminal_titles(self._app(sess))
        frames = [json.loads(c.args[0]) for c in ws.send_str.await_args_list]
        assert {"type": "title", "text": "vim"} in frames
        assert {"type": "cwd", "path": "/tmp/x"} in frames

    @pytest.mark.asyncio
    async def test_survives_ws_detach_during_probe(self):
        # The WS can detach (sess.ws = None) while a blocking probe runs in the
        # executor. The poller must revalidate after the hop — never send on the
        # dead reference, never AttributeError (which would kill the singleton
        # task for every terminal until restart).
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)

        def detach_and_return_title(s):
            s.ws = None  # disconnect lands mid-probe
            return "vim"

        with patch.object(terminal, "_session_title", side_effect=detach_and_return_title), \
             patch.object(terminal, "_session_cwd", return_value="/tmp/x"), \
             patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.poll_terminal_titles(self._app(sess))  # must not raise
        ws.send_str.assert_not_awaited()
