"""Tests for the built-in CLI terminal panel handlers."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.dashboard import terminal_commands
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


def _make_request(
    user="testuser",
    session_id="abc123",
    registry=None,
    cfg=None,
    origin=None,
    remote="127.0.0.1",
):
    """Build a mock aiohttp request with state and match_info.

    ``origin`` is the Origin header the handshake carries. Left unset it is
    absent, which ``check_origin`` trusts from loopback — a browser always
    sends one, so its absence means a local process rather than a page.
    """
    state = MagicMock()
    state._terminal_sessions = registry if registry is not None else {}
    app = {"state": state, "allowed_origins": {"http://localhost:5476"}}
    request = MagicMock()
    request.app = app
    request.get = lambda k, default=None: user if k == "user" else default
    request.match_info = MagicMock()
    request.match_info.get = lambda k, default="": session_id if k == "session_id" else default
    request.remote = remote
    request.headers = {} if origin is None else {"Origin": origin}
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
    # Most unit tests exercise an established terminal. Tests for the startup
    # barrier opt back into the fresh-session state explicitly.
    sess.shell_ready = True
    sess.last_ws_disconnect = disconnect
    sess.reader_task = None
    return sess


# ── _resolve_shell ──


class TestResolveShell:
    """Shell resolution: configured → $SHELL (POSIX) → platform default, each
    candidate validated as an executable, and the value returned is the path
    `which` RESOLVED — never the bare candidate, which the spawn's project cwd
    could re-resolve differently. `which` is pinned in every test so outcomes
    never depend on what the CI host has installed."""

    @pytest.fixture(autouse=True)
    def _posix(self, monkeypatch):
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", False)

    def _pin_which(self, monkeypatch, mapping):
        monkeypatch.setattr(terminal.shutil, "which", lambda c: mapping.get(c))

    def test_configured_executable_wins(self, monkeypatch):
        self._pin_which(monkeypatch, {"/opt/fish": "/opt/fish", "/bin/bash": "/bin/bash"})
        assert terminal._resolve_shell({"shell": "/opt/fish"}) == ("/opt/fish", None)

    def test_bare_name_pinned_to_resolved_path(self, monkeypatch):
        # The RESOLVED path is returned, not the bare name: the spawn runs in
        # the session's project cwd, where a relative PATH entry could resolve
        # the same bare name to a project-planted executable.
        self._pin_which(monkeypatch, {"fish": "/usr/local/bin/fish"})
        assert terminal._resolve_shell({"shell": "fish"}) == ("/usr/local/bin/fish", None)

    def test_relative_which_result_anchored_to_absolute(self, monkeypatch):
        # A RELATIVE PATH entry (PATH=bin:…) makes `which` itself return a
        # relative path, which the spawn cwd would re-resolve — the return
        # must be anchored to the gateway cwd the validation ran in, on both
        # the configured and the fallback branch.
        monkeypatch.setenv("SHELL", "zsh")
        self._pin_which(monkeypatch, {"fish": "bin/fish", "zsh": "bin/zsh"})
        shell, rejected = terminal._resolve_shell({"shell": "fish"})
        assert os.path.isabs(shell) and shell.endswith("/bin/fish")
        assert rejected is None
        shell, rejected = terminal._resolve_shell({})
        assert os.path.isabs(shell) and shell.endswith("/bin/zsh")
        assert rejected is None

    def test_invalid_configured_falls_back_to_env_shell(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/usr/bin/zsh")
        self._pin_which(monkeypatch, {"/usr/bin/zsh": "/usr/bin/zsh", "/bin/bash": "/bin/bash"})
        assert terminal._resolve_shell({"shell": "/opt/typo"}) == (
            "/usr/bin/zsh",
            "/opt/typo",
        )

    def test_unset_uses_env_shell_without_rejection(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/usr/bin/zsh")
        self._pin_which(monkeypatch, {"/usr/bin/zsh": "/usr/bin/zsh"})
        assert terminal._resolve_shell({}) == ("/usr/bin/zsh", None)

    def test_whitespace_configured_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/usr/bin/zsh")
        self._pin_which(monkeypatch, {"/usr/bin/zsh": "/usr/bin/zsh"})
        assert terminal._resolve_shell({"shell": "   "}) == ("/usr/bin/zsh", None)

    def test_invalid_env_shell_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/opt/gone")
        self._pin_which(monkeypatch, {"/bin/bash": "/bin/bash"})
        assert terminal._resolve_shell({}) == ("/bin/bash", None)

    def test_no_env_shell_uses_default(self, monkeypatch):
        monkeypatch.delenv("SHELL", raising=False)
        self._pin_which(monkeypatch, {"/bin/bash": "/bin/bash"})
        assert terminal._resolve_shell({}) == ("/bin/bash", None)

    def test_nothing_resolves_returns_default_unvalidated(self, monkeypatch):
        # A host where no candidate validates keeps the historical behavior:
        # return the default so the spawn's own error surfaces, never a
        # silent no-terminal state.
        monkeypatch.delenv("SHELL", raising=False)
        self._pin_which(monkeypatch, {})  # nothing resolves
        assert terminal._resolve_shell({"shell": "/opt/typo"}) == (
            "/bin/bash",
            "/opt/typo",
        )

    def test_windows_rejects_to_powershell(self, monkeypatch):
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", True)
        self._pin_which(
            monkeypatch, {"powershell.exe": "C:\\WINDOWS\\System32\\powershell.exe"}
        )
        shell, rejected = terminal._resolve_shell({"shell": "C:\\typo.exe"})
        # abspath is host-dependent for a Windows-style fake on a POSIX test
        # host, so assert the wiring (absolute + right program) rather than an
        # exact string.
        assert os.path.isabs(shell)
        assert shell.endswith("powershell.exe")
        assert rejected == "C:\\typo.exe"


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
        reason="POSIX master_fd/proc teardown; Windows sessions use the ConPTY backend",  # wokeignore:rule=master
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
    async def test_uses_configured_shell(self, monkeypatch):
        # The configured shell must actually resolve to an executable to be
        # used — pin `which` so the test does not depend on the host's zsh.
        monkeypatch.setattr(
            terminal.shutil, "which", lambda c: c if c == "/bin/zsh" else None
        )
        req = _make_request()
        with patch.object(
            terminal, "_get_config", return_value={"enabled": True, "shell": "/bin/zsh"}
        ), patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_create(req)
        body = json.loads(resp.body)
        assert body["shell"] == "/bin/zsh"
        assert "shell_fallback" not in body

    @pytest.mark.asyncio
    async def test_surfaces_fallback_when_configured_shell_missing(self, monkeypatch):
        # A configured shell that does not resolve must not fail the create —
        # the response falls back AND says so, so a typo is visible instead of
        # a silently different shell.
        monkeypatch.setattr(terminal.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setenv("SHELL", "/bin/bash")
        monkeypatch.setattr(
            terminal.shutil, "which", lambda c: c if c == "/bin/bash" else None
        )
        req = _make_request()
        with patch.object(
            terminal, "_get_config",
            return_value={"enabled": True, "shell": "/opt/no-such-shell"},
        ), patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_create(req)
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["shell"] == "/bin/bash"
        assert body["shell_fallback"] is True
        assert body["configured_shell"] == "/opt/no-such-shell"


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


class TestSplitPathToken:
    def test_no_slash_is_all_prefix(self):
        assert terminal._split_path_token("Kiro") == ("", "Kiro")

    def test_trailing_slash_has_empty_prefix(self):
        assert terminal._split_path_token("src/") == ("src/", "")

    def test_relative_parent(self):
        assert terminal._split_path_token("../Kiro") == ("../", "Kiro")

    def test_absolute(self):
        assert terminal._split_path_token("/usr/lo") == ("/usr/", "lo")

    def test_empty(self):
        assert terminal._split_path_token("") == ("", "")


class TestResolveCompletionDir:
    def test_empty_dir_part_is_cwd(self):
        assert terminal._resolve_completion_dir("/tmp/work", "") == "/tmp/work"

    def test_relative_resolves_against_cwd(self):
        assert terminal._resolve_completion_dir("/tmp/work", "sub/") == "/tmp/work/sub"

    def test_parent_traversal(self):
        assert terminal._resolve_completion_dir("/tmp/work", "../") == "/tmp"

    def test_absolute_ignores_cwd(self):
        assert terminal._resolve_completion_dir("/tmp/work", "/usr/") == "/usr"

    def test_expands_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert terminal._resolve_completion_dir("/tmp/work", "~/") == str(tmp_path)


class TestListCompletions:
    """Directory listing rules: case-insensitive substring match ranked by match
    offset, dirs first among equals, hidden entries only once the user has typed
    a dot, folders_only narrowing."""

    @pytest.fixture
    def tree(self, tmp_path):
        (tmp_path / "alpha").mkdir()
        (tmp_path / "Beta").mkdir()
        (tmp_path / "aardvark.txt").write_text("x")
        (tmp_path / "zeta.md").write_text("x")
        (tmp_path / ".hidden").mkdir()
        return tmp_path

    def test_lists_dirs_before_files(self, tree):
        entries, truncated = terminal._list_completions(str(tree), "", False, 100)
        assert [e["name"] for e in entries] == ["alpha", "Beta", "aardvark.txt", "zeta.md"]
        assert truncated is False

    def test_folders_only_drops_files(self, tree):
        entries, _ = terminal._list_completions(str(tree), "", True, 100)
        assert [e["name"] for e in entries] == ["alpha", "Beta"]
        assert all(e["dir"] for e in entries)

    def test_prefix_match_is_case_insensitive(self, tree):
        entries, _ = terminal._list_completions(str(tree), "b", False, 100)
        assert [e["name"] for e in entries] == ["Beta"]

    def test_matches_a_fragment_in_the_middle_of_a_name(self, tree):
        # The whole point: a long name is reachable by its distinctive middle.
        entries, _ = terminal._list_completions(str(tree), "dvark", False, 100)
        assert [e["name"] for e in entries] == ["aardvark.txt"]
        assert entries[0]["at"] == 3

    def test_ranks_an_earlier_match_first(self, tree):
        # "a" is a prefix of alpha/aardvark and sits mid-name in Beta/zeta — the
        # prefix hits must lead, and dirs still win among equal offsets.
        entries, _ = terminal._list_completions(str(tree), "a", False, 100)
        assert [(e["name"], e["at"]) for e in entries] == [
            ("alpha", 0), ("aardvark.txt", 0), ("Beta", 3), ("zeta.md", 3),
        ]

    def test_a_dot_fragment_matches_as_a_prefix(self, tree):
        # A leading dot unhides entries; matching it as a substring would drag
        # in every foo.bar and defeat that filter.
        entries, _ = terminal._list_completions(str(tree), ".h", False, 100)
        assert [e["name"] for e in entries] == [".hidden"]

    def test_hides_dotfiles_until_dot_typed(self, tree):
        assert all(not e["name"].startswith(".")
                   for e in terminal._list_completions(str(tree), "", False, 100)[0])
        entries, _ = terminal._list_completions(str(tree), ".", False, 100)
        assert [e["name"] for e in entries] == [".hidden"]

    def test_truncates_at_limit(self, tree):
        entries, truncated = terminal._list_completions(str(tree), "", False, 2)
        assert len(entries) == 2
        assert truncated is True

    def test_ranking_survives_the_retention_cap(self, tree):
        # The size-bounded heap must keep the SAME top-N the full sort would:
        # dirs before files at equal match offset.
        entries, truncated = terminal._list_completions(str(tree), "a", False, 2)
        assert [e["name"] for e in entries] == ["alpha", "aardvark.txt"]
        assert truncated is True

    def test_scan_cap_marks_truncated(self, tmp_path, monkeypatch):
        # A pathological directory must not be walked to the end; `truncated`
        # stays truthful for the SCAN cap too, not just the retention cap.
        for i in range(6):
            (tmp_path / f"f{i}").write_text("x")
        monkeypatch.setattr(terminal, "_COMPLETE_MAX_SCAN", 3)
        entries, truncated = terminal._list_completions(str(tmp_path), "", False, 100)
        assert len(entries) == 3
        assert truncated is True

    def test_scan_cap_not_reported_when_directory_fits(self, tmp_path, monkeypatch):
        (tmp_path / "only").write_text("x")
        monkeypatch.setattr(terminal, "_COMPLETE_MAX_SCAN", 3)
        assert terminal._list_completions(str(tmp_path), "", False, 100) == (
            [{"name": "only", "dir": False, "at": 0}], False,
        )

    def test_excludes_names_with_control_characters(self, tmp_path):
        # The client TYPES the accepted name into the PTY, so a newline would
        # submit a command line. Such names must never reach the client.
        (tmp_path / "safe.txt").write_text("x")
        try:
            (tmp_path / "ev\nil.txt").write_text("x")
            (tmp_path / "esc\x1bape.txt").write_text("x")
        except OSError:  # pragma: no cover — filesystem refuses the name
            pytest.skip("filesystem rejects control characters in names")
        entries, _ = terminal._list_completions(str(tmp_path), "", False, 100)
        assert [e["name"] for e in entries] == ["safe.txt"]

    def test_excludes_names_with_lone_surrogates(self, tmp_path):
        # An undecodable byte in a filename survives as a lone surrogate through
        # Python's surrogateescape decoding and through JSON, but the browser's
        # TextEncoder turns it into U+FFFD — the client would then type a path
        # that does not exist.
        (tmp_path / "safe.txt").write_text("x")
        try:
            (tmp_path / b"bad\xffname.txt".decode("utf-8", "surrogateescape")).write_text("x")
        except (OSError, UnicodeEncodeError):  # pragma: no cover — fs refuses the name
            pytest.skip("filesystem rejects undecodable bytes in names")
        entries, _ = terminal._list_completions(str(tmp_path), "", False, 100)
        assert [e["name"] for e in entries] == ["safe.txt"]

    def test_filters_lone_surrogates_without_touching_the_filesystem(self, tmp_path):
        # Runs everywhere: macOS refuses to CREATE a name with undecodable bytes,
        # but a network/foreign volume can serve one, so the filter itself is
        # asserted against a synthetic directory read.
        names = ["safe.txt", "bad\udcffname.txt", "bell\x07name.txt"]

        class _Entry:
            def __init__(self, name):
                self.name = name

            def is_dir(self):
                return False

        class _Scandir:
            def __enter__(self):
                return iter([_Entry(n) for n in names])

            def __exit__(self, *exc):
                return False

        with patch.object(terminal.os, "scandir", return_value=_Scandir()):
            entries, _ = terminal._list_completions(str(tmp_path), "", False, 100)
        assert [e["name"] for e in entries] == ["safe.txt"]

    def test_missing_directory_yields_nothing(self, tmp_path):
        assert terminal._list_completions(str(tmp_path / "nope"), "", False, 100) == ([], False)

    def test_sensitive_directory_yields_nothing(self, tmp_path):
        # ~/.kiro/crew/profiles is trust-root metadata: enumerating it would
        # disclose profile/policy filenames.
        (tmp_path / "leak").mkdir()
        with _sensitive(always=True):
            assert terminal._list_completions(str(tmp_path), "", False, 100) == ([], False)

    def test_symlink_into_sensitive_directory_yields_nothing(self, tmp_path):
        # The name-based check alone would pass: only the realpath of the link
        # lands in the protected tree.
        secret = tmp_path / "protected"
        secret.mkdir()
        (secret / "profile.json").write_text("x")
        link = tmp_path / "benign"
        link.symlink_to(secret, target_is_directory=True)
        real_check = terminal.is_sensitive_path

        def _fake(path, base_dir=None):
            # Matches ONLY the canonical target, never the link's own name.
            return os.path.realpath(path) == os.path.realpath(str(secret)) or real_check(path)

        with _sensitive(predicate=_fake):
            assert terminal._list_completions(str(link), "", False, 100) == ([], False)


class TestSensitiveEntryGate:
    """Vetting the DIRECTORY is not enough: an allowed directory can hold
    protected children (``~/.kiro/crew`` holds ``security_policy.json``,
    ``profiles/``), so every entry is classified before it is returned.

    These call ``_list_vetted_completions`` so the patched predicate is exercised
    on ENTRIES only — the directory is already vetted by construction."""

    def test_withholds_a_sensitive_child_by_name(self, tmp_path):
        (tmp_path / "safe.txt").write_text("x")
        (tmp_path / "security_policy.json").write_text("x")

        def _fake(path, base_dir=None):
            return os.path.basename(path) == "security_policy.json"

        with _sensitive(predicate=_fake):
            entries, _ = terminal._list_vetted_completions(str(tmp_path), "", False, 100)
        assert [e["name"] for e in entries] == ["safe.txt"]

    def test_withholds_a_child_symlinked_into_a_protected_tree(self, tmp_path):
        # Name-based classification alone would pass this: only the link's
        # canonical target lands in the protected tree.
        protected = tmp_path / "protected"
        protected.mkdir()
        listed = tmp_path / "listed"
        listed.mkdir()
        (listed / "safe.txt").write_text("x")
        (listed / "shortcut").symlink_to(protected, target_is_directory=True)

        def _fake(path, base_dir=None):
            return os.path.realpath(path) == os.path.realpath(str(protected))

        with _sensitive(predicate=_fake):
            entries, _ = terminal._list_vetted_completions(str(listed), "", False, 100)
        assert [e["name"] for e in entries] == ["safe.txt"]

    def test_withholds_an_entry_that_cannot_be_classified(self, tmp_path):
        # Over-refusing what we cannot classify is the safe direction.
        (tmp_path / "safe.txt").write_text("x")
        with _sensitive(raises=True):
            assert terminal._list_vetted_completions(str(tmp_path), "", False, 100) == ([], False)

    def test_classifies_only_entries_that_matched_the_fragment(self, tmp_path):
        # The gate runs at keystroke rate, so it must not be paid for every name
        # in a large directory — only for names the user could receive.
        for name in ("alpha", "beta", "gamma"):
            (tmp_path / name).write_text("x")
        with _sensitive(always=False) as (gate, _):
            terminal._list_vetted_completions(str(tmp_path), "alph", False, 100)
        assert gate.call_count == 1


@contextlib.contextmanager
def _sensitive(*, predicate=None, always=False, raises=False):
    """Force the sensitive-path verdict at BOTH layers that can decide it.

    The DIRECTORY gate runs inside `hooks.validate_file_path` (the required
    chokepoint), while the per-ENTRY gate calls `is_sensitive_path` directly from
    the terminal module. A test that patched only one of them would silently
    exercise half the guard."""
    import kiro_crew.hooks as hooks_mod
    kw = {}
    if raises:
        kw["side_effect"] = OSError
    elif predicate is not None:
        kw["side_effect"] = predicate
    else:
        kw["return_value"] = bool(always)
    with patch.object(terminal, "is_sensitive_path", **kw) as a, \
            patch.object(hooks_mod, "is_sensitive_path", **kw) as b:
        yield a, b


class TestOpenVettedDir:
    """The scan is pinned to a descriptor so a name swap after vetting cannot
    redirect it, and the open itself is verified against the vetted name."""

    def test_returns_a_descriptor_for_the_vetted_directory(self, tmp_path):
        fd = terminal._open_vetted_dir(str(tmp_path))
        assert fd is not None
        try:
            opened = os.fstat(fd)
            named = os.stat(tmp_path)
            assert (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)
        finally:
            os.close(fd)

    def test_refuses_a_missing_directory(self, tmp_path):
        assert terminal._open_vetted_dir(str(tmp_path / "gone")) is None

    def test_refuses_a_file(self, tmp_path):
        target = tmp_path / "f.txt"
        target.write_text("x")
        assert terminal._open_vetted_dir(str(target)) is None

    def test_refuses_a_symlinked_final_component(self, tmp_path):
        # realpath guaranteed at vet time that the last component was not a link;
        # if it is one now, the name was swapped underneath us.
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        assert terminal._open_vetted_dir(str(link)) is None

    def test_refuses_when_the_name_no_longer_matches_the_descriptor(self, tmp_path):
        """The swap window between open() and the check must fail closed."""
        real = tmp_path / "real"
        real.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        real_st = os.stat(real)
        other_st = os.stat(other)
        assert (real_st.st_dev, real_st.st_ino) != (other_st.st_dev, other_st.st_ino)
        # Simulate the name resolving elsewhere after the descriptor was opened.
        with patch.object(terminal.os, "stat", return_value=other_st):
            assert terminal._open_vetted_dir(str(real)) is None

    def test_listing_scans_the_descriptor_not_the_path(self, tmp_path):
        """Regression: a swap after vetting must not redirect the enumeration.

        `scandir` is asserted to receive the pinned fd (an int), never the path
        string — passing the string is what reopened the name and reintroduced
        the race."""
        (tmp_path / "visible").mkdir()
        seen: list[object] = []
        real_scandir = terminal.os.scandir

        def spy(arg):
            seen.append(arg)
            return real_scandir(arg)

        with patch.object(terminal.os, "scandir", spy):
            entries, _ = terminal._list_vetted_completions(str(tmp_path), "", False, 10)
        assert [e["name"] for e in entries] == ["visible"]
        assert seen and all(isinstance(a, int) for a in seen)

    def test_listing_yields_nothing_when_the_directory_cannot_be_pinned(self, tmp_path):
        with patch.object(terminal, "_open_vetted_dir", return_value=None):
            assert terminal._list_vetted_completions(str(tmp_path), "", False, 10) == ([], False)

    def test_listing_closes_the_descriptor(self, tmp_path):
        (tmp_path / "a").mkdir()
        closed: list[int] = []
        real_close = terminal.os.close

        def spy(fd):
            closed.append(fd)
            return real_close(fd)

        with patch.object(terminal.os, "close", spy):
            terminal._list_vetted_completions(str(tmp_path), "", False, 10)
        assert closed, "the pinned descriptor must be closed"


class TestVettedCompletionDir:
    def test_routes_through_the_hooks_chokepoint(self, tmp_path):
        """The backend security rules require file reads to pass through
        hooks.py rather than re-deriving its realpath + is_sensitive_path pair."""
        with patch.object(terminal, "validate_file_path", return_value=None) as gate:
            assert terminal._vetted_completion_dir(str(tmp_path)) is None
        gate.assert_called_once_with(str(tmp_path))

    def test_returns_canonical_path(self, tmp_path):
        assert terminal._vetted_completion_dir(str(tmp_path)) == os.path.realpath(str(tmp_path))

    def test_rejects_sensitive_path(self, tmp_path):
        with _sensitive(always=True):
            assert terminal._vetted_completion_dir(str(tmp_path)) is None

    def test_rejects_when_canonicalization_fails(self, tmp_path):
        # Over-refusing a path we cannot canonicalize is the safe direction.
        with patch.object(terminal.os.path, "realpath", side_effect=OSError):
            assert terminal._vetted_completion_dir(str(tmp_path)) is None


class TestSessionCwdCached:
    @pytest.mark.asyncio
    async def test_probes_once_within_ttl(self):
        sess = _make_session()
        with patch.object(terminal, "_session_cwd", return_value="/tmp/a") as probe:
            assert await terminal._session_cwd_cached(sess) == "/tmp/a"
            assert await terminal._session_cwd_cached(sess) == "/tmp/a"
        assert probe.call_count == 1

    @pytest.mark.asyncio
    async def test_reprobes_after_ttl_expires(self):
        # A `cd` must be visible to the next completion — a stale memo would
        # list the previous directory.
        sess = _make_session()
        sess.cwd_probe = (time.monotonic() - terminal._CWD_PROBE_TTL_S - 1, "/tmp/old")
        with patch.object(terminal, "_session_cwd", return_value="/tmp/new"):
            assert await terminal._session_cwd_cached(sess) == "/tmp/new"


class TestApiTerminalComplete:
    """POST /api/terminal/complete — path completions for a live PTY session."""

    @pytest.fixture(autouse=True)
    def sel_log(self):
        """Every outcome of this route audits, so the sink is patched for the
        whole class rather than per test."""
        with patch.object(terminal, "_sel") as mock_sel:
            log = MagicMock()
            mock_sel.return_value.log_api_access = log
            yield log

    def _req(self, body, user="testuser", registry=None):
        req = _make_request(user=user, registry=registry)
        req.json = AsyncMock(return_value=body)
        return req

    @pytest.mark.asyncio
    async def test_rejects_unauthenticated(self):
        req = self._req({"session_id": "s1"}, user=None)
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_rejects_when_disabled(self):
        req = self._req({"session_id": "s1"})
        with patch.object(terminal, "_is_enabled", return_value=False), \
             patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_rejects_malformed_body(self):
        req = self._req({"session_id": 42})
        resp = await terminal.api_terminal_complete(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rejects_oversized_token(self):
        req = self._req({"session_id": "s1", "token": "x" * (terminal._COMPLETE_TOKEN_MAX + 1)})
        resp = await terminal.api_terminal_complete(req)
        assert resp.status == 413

    @pytest.mark.asyncio
    async def test_unknown_session_is_404(self):
        # Requiring a live PTY is what keeps this from being a general
        # filesystem-enumeration endpoint.
        req = self._req({"session_id": "nope", "token": ""}, registry={})
        resp = await terminal.api_terminal_complete(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_returns_empty_when_cwd_unknown(self):
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "x"}, registry={"s1": sess})
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=None)):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert json.loads(resp.text) == {
            "dir": None, "prefix": "x", "entries": [], "truncated": False,
        }

    @pytest.mark.asyncio
    async def test_lists_matching_entries(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "doc.txt").write_text("x")
        (tmp_path / "other").mkdir()
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "doc"}, registry={"s1": sess})
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))):
            resp = await terminal.api_terminal_complete(req)
        body = json.loads(resp.text)
        assert body["dir"] == str(tmp_path)
        assert body["prefix"] == "doc"
        assert [e["name"] for e in body["entries"]] == ["docs", "doc.txt"]

    @pytest.mark.asyncio
    async def test_folders_only_narrows_listing(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "doc.txt").write_text("x")
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "doc", "folders_only": True}, registry={"s1": sess}
        )
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))):
            resp = await terminal.api_terminal_complete(req)
        assert [e["name"] for e in json.loads(resp.text)["entries"]] == ["docs"]

    @pytest.mark.asyncio
    async def test_resolves_token_directory_against_session_cwd(self, tmp_path):
        (tmp_path / "work").mkdir()
        (tmp_path / "sibling").mkdir()
        sess = _make_session(session_id="s1")
        # `cd ../s` typed from tmp_path/work resolves into tmp_path.
        req = self._req({"session_id": "s1", "token": "../s"}, registry={"s1": sess})
        with patch.object(
            terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path / "work"))
        ):
            resp = await terminal.api_terminal_complete(req)
        body = json.loads(resp.text)
        assert body["dir"] == str(tmp_path)
        assert body["prefix"] == "s"
        assert [e["name"] for e in body["entries"]] == ["sibling"]

    @pytest.mark.asyncio
    async def test_sensitive_directory_returns_empty_listing_shape(self, tmp_path):
        # Same shape as the unknown-cwd branch: the client needs no special case,
        # and the response does not reveal whether the path exists.
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "p"}, registry={"s1": sess})
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))), \
             _sensitive(always=True):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert json.loads(resp.text) == {
            "dir": None, "prefix": "p", "entries": [], "truncated": False,
        }

    @pytest.mark.asyncio
    async def test_audits_sensitive_path_refusal(self, tmp_path, sel_log):
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "p"}, registry={"s1": sess})
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))), \
             _sensitive(always=True):
            await terminal.api_terminal_complete(req)
        assert sel_log.call_args.kwargs == {
            "caller": "testuser",
            "operation": "terminal.complete",
            "outcome": "denied",
            "source": "dashboard",
            "resources": "sensitive_path",
        }

    @pytest.mark.asyncio
    async def test_audits_successful_listing_without_leaking_contents(self, tmp_path, sel_log):
        # The audit payload is deliberately coarse: this route fires per
        # keystroke, so the token and any filenames must stay out of the log.
        (tmp_path / "secretname").mkdir()
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "secret"}, registry={"s1": sess})
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))):
            resp = await terminal.api_terminal_complete(req)
        assert [e["name"] for e in json.loads(resp.text)["entries"]] == ["secretname"]
        kwargs = sel_log.call_args.kwargs
        assert kwargs["operation"] == "terminal.complete"
        assert kwargs["outcome"] == "ok"
        assert kwargs["resources"] == "listed"
        payload = json.dumps(kwargs)
        assert "secret" not in payload
        assert str(tmp_path) not in payload

    @pytest.mark.asyncio
    async def test_audits_unknown_cwd_and_unknown_session(self, sel_log):
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "x"}, registry={"s1": sess})
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=None)):
            await terminal.api_terminal_complete(req)
        assert sel_log.call_args.kwargs["resources"] == "no_cwd"
        await terminal.api_terminal_complete(
            self._req({"session_id": "gone", "token": ""}, registry={})
        )
        assert sel_log.call_args.kwargs["resources"] == "unknown_session"

    @pytest.mark.asyncio
    async def test_lists_on_discovery_pool_not_subprocess_pool(self, tmp_path):
        # subprocess_executor's workers are shared with PTY teardown; a slow
        # directory scan must not be able to occupy one.
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": ""}, registry={"s1": sess})
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))), \
             patch.object(terminal, "discovery_executor") as disc, \
             patch.object(terminal, "subprocess_executor") as sub:
            disc.return_value = None  # None → loop's default executor
            await terminal.api_terminal_complete(req)
        assert disc.called
        assert not sub.called

    @pytest.mark.asyncio
    async def test_resolution_and_vetting_run_off_loop_in_one_hop(self, tmp_path):
        # expanduser (a "~user" form triggers a synchronous name-service lookup)
        # and realpath (can stall on an unresponsive mount) are as blocking as
        # the scan, so neither may run inline in the coroutine — and all three
        # share ONE executor hop so a keystroke costs one thread round-trip.
        loop_thread = threading.current_thread()
        seen: list[tuple[str, object]] = []
        real_resolve = terminal._resolve_completion_dir
        real_vet = terminal._vetted_completion_dir

        def _resolve(cwd, dir_part):
            seen.append(("resolve", threading.current_thread()))
            return real_resolve(cwd, dir_part)

        def _vet(directory):
            seen.append(("vet", threading.current_thread()))
            return real_vet(directory)

        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": ""}, registry={"s1": sess})
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))), \
             patch.object(terminal, "_resolve_completion_dir", side_effect=_resolve), \
             patch.object(terminal, "_vetted_completion_dir", side_effect=_vet), \
             patch.object(terminal, "discovery_executor", return_value=None) as disc:
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert [step for step, _ in seen] == ["resolve", "vet"]
        assert all(thread is not loop_thread for _, thread in seen)
        assert disc.call_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], "0"])
    async def test_rejects_non_boolean_folders_only(self, value):
        # bool("false") is True: coercing would silently drop every file from
        # the listing for a client that sent the JSON string.
        req = self._req({"session_id": "s1", "token": "", "folders_only": value})
        resp = await terminal.api_terminal_complete(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [True, False])
    async def test_accepts_real_boolean_folders_only(self, tmp_path, value):
        (tmp_path / "docs").mkdir()
        (tmp_path / "doc.txt").write_text("x")
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "doc", "folders_only": value}, registry={"s1": sess}
        )
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        names = [e["name"] for e in json.loads(resp.text)["entries"]]
        assert names == (["docs"] if value else ["docs", "doc.txt"])

    @pytest.mark.asyncio
    async def test_omitted_folders_only_defaults_to_files_included(self, tmp_path):
        (tmp_path / "doc.txt").write_text("x")
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "doc"}, registry={"s1": sess})
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))):
            resp = await terminal.api_terminal_complete(req)
        assert [e["name"] for e in json.loads(resp.text)["entries"]] == ["doc.txt"]

    @pytest.mark.asyncio
    async def test_a_path_listing_carries_no_entry_kind(self, tmp_path):
        # The client drops an entry that does not belong to the tier it asked for,
        # so the path tier must not start emitting `kind` — that would silently
        # empty every path menu.
        (tmp_path / "docs").mkdir()
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "doc"}, registry={"s1": sess})
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))):
            resp = await terminal.api_terminal_complete(req)
        assert all("kind" not in e for e in json.loads(resp.text)["entries"])


class TestApiTerminalCompleteCommandTier:
    """POST /api/terminal/complete with `argv` — subcommand and flag completions.

    The engine itself is covered in test_terminal_commands.py; this class covers
    the ROUTE: tier selection, the audit vocabulary, and the response shape.
    """

    @pytest.fixture(autouse=True)
    def sel_log(self):
        with patch.object(terminal, "_sel") as mock_sel:
            log = MagicMock()
            mock_sel.return_value.log_api_access = log
            yield log

    def _req(self, body, user="testuser", registry=None):
        req = _make_request(user=user, registry=registry)
        req.json = AsyncMock(return_value=body)
        return req

    @staticmethod
    def _entries(*names):
        return [
            terminal_commands.CmdEntry(n, f"about {n}", n.startswith("-")) for n in names
        ]

    def _complete(self, entries, reason="cmd_listed"):
        return patch.object(
            terminal_commands, "complete", AsyncMock(return_value=(entries, reason)),
        )

    @pytest.mark.asyncio
    async def test_argv_selects_the_command_tier(self, tmp_path):
        # A real directory is present, so answering with subcommands rather than its
        # contents proves the tier was chosen by `argv`, not by what happened to be
        # on disk.
        (tmp_path / "docs").mkdir()
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["gh", "pr"]}, registry={"s1": sess},
        )
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))), \
             self._complete(self._entries("create", "checkout")):
            resp = await terminal.api_terminal_complete(req)
        body = json.loads(resp.text)
        assert resp.status == 200
        assert [e["name"] for e in body["entries"]] == ["create", "checkout"]
        assert all(e["kind"] == "sub" for e in body["entries"])
        # `dir: null` is the existing "nothing resolved on the filesystem" signal,
        # reused so the response needs no new top-level field.
        assert body["dir"] is None
        assert body["truncated"] is False

    @pytest.mark.asyncio
    async def test_passes_the_token_and_cwd_through_to_the_engine(self, tmp_path):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "cre", "argv": ["gh", "pr"]}, registry={"s1": sess},
        )
        engine = AsyncMock(return_value=(self._entries("create"), "cmd_listed"))
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))), \
             patch.object(terminal_commands, "complete", engine):
            await terminal.api_terminal_complete(req)
        assert engine.await_args.args[:3] == (["gh", "pr"], "cre", str(tmp_path))

    @pytest.mark.asyncio
    async def test_answers_flags_for_a_dash_token(self):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "--ti", "argv": ["gh", "pr", "create"]},
            registry={"s1": sess},
        )
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")), \
             self._complete(self._entries("--title")):
            resp = await terminal.api_terminal_complete(req)
        body = json.loads(resp.text)
        assert [(e["name"], e["kind"]) for e in body["entries"]] == [("--title", "flag")]
        assert body["prefix"] == "--ti"

    @pytest.mark.asyncio
    async def test_answers_even_when_the_cwd_cannot_be_read(self):
        # A subcommand list does not depend on the working directory, so this branch
        # is ordered BEFORE the path tier's unknown-cwd refusal.
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["gh"]}, registry={"s1": sess},
        )
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=None)), \
             self._complete(self._entries("pr")):
            resp = await terminal.api_terminal_complete(req)
        assert [e["name"] for e in json.loads(resp.text)["entries"]] == ["pr"]

    @pytest.mark.asyncio
    async def test_does_not_fall_back_to_a_path_listing(self, tmp_path):
        # The tiers are disjoint. Falling through would list the cwd for a word the
        # client already decided cannot be a path.
        (tmp_path / "docs").mkdir()
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["gh"]}, registry={"s1": sess},
        )
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))), \
             self._complete([], "cmd_none"):
            resp = await terminal.api_terminal_complete(req)
        assert json.loads(resp.text)["entries"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "argv", [[], "gh", ["./gh"], ["/usr/bin/gh"], [""], ["gh\n"], [7], {}]
    )
    async def test_rejects_a_malformed_argv_with_400(self, argv):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": argv}, registry={"s1": sess},
        )
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_the_rejection_carries_a_machine_readable_code(self):
        # The dashboard renders `error` verbatim into a localized UI, so the prose
        # alone is untranslatable by construction; `code` is the contract.
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["./gh"]}, registry={"s1": sess},
        )
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")):
            resp = await terminal.api_terminal_complete(req)
        assert json.loads(resp.text)["code"] == "terminal_invalid_argv"

    @pytest.mark.asyncio
    async def test_a_malformed_argv_never_reaches_the_engine(self):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["./gh"]}, registry={"s1": sess},
        )
        engine = AsyncMock()
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")), \
             patch.object(terminal_commands, "complete", engine):
            await terminal.api_terminal_complete(req)
        engine.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_path_tier_is_untouched_when_argv_is_absent(self, tmp_path):
        (tmp_path / "docs").mkdir()
        sess = _make_session(session_id="s1")
        req = self._req({"session_id": "s1", "token": "doc"}, registry={"s1": sess})
        engine = AsyncMock()
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value=str(tmp_path))), \
             patch.object(terminal_commands, "complete", engine):
            resp = await terminal.api_terminal_complete(req)
        engine.assert_not_awaited()
        assert [e["name"] for e in json.loads(resp.text)["entries"]] == ["docs"]

    @pytest.mark.asyncio
    async def test_passes_the_operator_command_map_to_the_engine(self):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["mytool"]}, registry={"s1": sess},
        )
        engine = AsyncMock(return_value=([], "cmd_unknown"))
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")), \
             patch.object(terminal, "_get_config",
                          return_value={"completion": {"commands": {"mytool": "cobra"}}}), \
             patch.object(terminal_commands, "complete", engine):
            await terminal.api_terminal_complete(req)
        assert engine.await_args.args[3] == {"mytool": "cobra"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [False, True, "yes", 7, [], None])
    async def test_a_non_object_completion_config_is_not_an_error(self, value):
        # config.json is hand-edited: `"completion": false` would make a chained
        # `.get` raise on a boolean — an HTTP 500 on a keystroke from a typo. A
        # non-object value means "no operator additions", which is also the default.
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["gh"]}, registry={"s1": sess},
        )
        engine = AsyncMock(return_value=([], "cmd_unknown"))
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")), \
             patch.object(terminal, "_get_config", return_value={"completion": value}), \
             patch.object(terminal_commands, "complete", engine):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert engine.await_args.args[3] is None

    @pytest.mark.asyncio
    async def test_reads_the_config_off_the_event_loop(self):
        # `_get_config` does a synchronous `read_text()` of config.json and this
        # route fires per keystroke, so an inline read would stall every gateway
        # task on a slow home filesystem. Asserts the thread, not the timing.
        import threading
        seen = {}
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["gh"]}, registry={"s1": sess},
        )

        def spy(_request):
            seen["thread"] = threading.current_thread().name
            return {}

        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")), \
             patch.object(terminal, "_get_config", spy), \
             self._complete([], "cmd_unknown"):
            await terminal.api_terminal_complete(req)
        assert "thread" in seen, "config was never read"
        assert seen["thread"] != threading.current_thread().name

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cfg", [False, True, "yes", 7, [], None])
    async def test_a_non_object_terminal_config_is_not_an_error(self, cfg):
        # Both levels are type-checked: `"terminal": false` would make
        # `.get("completion")` raise on a boolean, an HTTP 500 from a typo.
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["gh"]}, registry={"s1": sess},
        )
        engine = AsyncMock(return_value=([], "cmd_unknown"))
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")), \
             patch.object(terminal, "_get_config", return_value=cfg), \
             patch.object(terminal_commands, "complete", engine):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert engine.await_args.args[3] is None

    @pytest.mark.asyncio
    async def test_a_missing_completion_config_is_not_an_error(self):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["gh"]}, registry={"s1": sess},
        )
        engine = AsyncMock(return_value=([], "cmd_unknown"))
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")), \
             patch.object(terminal, "_get_config", return_value={}), \
             patch.object(terminal_commands, "complete", engine):
            resp = await terminal.api_terminal_complete(req)
        assert resp.status == 200
        assert engine.await_args.args[3] is None

    # ── Audit ──
    # The rule for this route is a FIXED, content-free reason vocabulary: it fires
    # per keystroke, so a reason naming the command or flag would put the user's
    # command line in the audit trail.

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reason,outcome", [("cmd_listed", "ok"), ("cmd_none", "ok"), ("cmd_unknown", "ok")],
    )
    async def test_audits_each_engine_verdict(self, reason, outcome, sel_log):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "cre", "argv": ["gh", "pr"]}, registry={"s1": sess},
        )
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")), \
             self._complete(self._entries("create") if reason == "cmd_listed" else [], reason):
            await terminal.api_terminal_complete(req)
        assert sel_log.call_args.kwargs == {
            "caller": "testuser",
            "operation": "terminal.complete",
            "outcome": outcome,
            "source": "dashboard",
            "resources": reason,
        }

    @pytest.mark.asyncio
    async def test_audits_a_refused_working_directory_as_denied(self, sel_log):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["gh"]}, registry={"s1": sess},
        )
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")), \
             self._complete([], "sensitive_path"):
            await terminal.api_terminal_complete(req)
        assert sel_log.call_args.kwargs["outcome"] == "denied"
        assert sel_log.call_args.kwargs["resources"] == "sensitive_path"

    @pytest.mark.asyncio
    async def test_audits_a_malformed_argv(self, sel_log):
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "", "argv": ["./gh"]}, registry={"s1": sess},
        )
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w")):
            await terminal.api_terminal_complete(req)
        assert sel_log.call_args.kwargs["outcome"] == "denied"
        assert sel_log.call_args.kwargs["resources"] == "invalid_argv"

    @pytest.mark.asyncio
    async def test_the_audit_never_records_what_was_typed(self, sel_log):
        # The anti-leak proof, mirroring the path tier's own. Nothing from the
        # command line — command, subcommand, flag, prefix, or an entry name — may
        # appear anywhere in the event.
        sess = _make_session(session_id="s1")
        req = self._req(
            {"session_id": "s1", "token": "--secret-flag", "argv": ["gh", "pr", "sneaky"]},
            registry={"s1": sess},
        )
        with patch.object(terminal, "_session_cwd_cached", AsyncMock(return_value="/w/proj")), \
             self._complete(self._entries("--secret-flag")):
            await terminal.api_terminal_complete(req)
        blob = repr(sel_log.call_args.kwargs)
        for leaked in ("gh", "sneaky", "secret-flag", "/w/proj"):
            assert leaked not in blob
        assert sel_log.call_args.kwargs["resources"] == "cmd_listed"


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


class TestApiTerminalWsOrigin:
    """The handshake must validate its Origin before anything else.

    A WebSocket upgrade is a GET, and ``csrf_middleware`` checks the origin only
    for unsafe methods, so without an in-handler check the handshake arrives
    unvalidated. The cookie rides along automatically and SameSite=Lax does not
    distinguish ports, so another loopback origin could otherwise drive a PTY
    under the operator's own session.
    """

    @pytest.mark.asyncio
    async def test_rejects_a_cross_origin_handshake(self):
        req = _make_request(origin="http://127.0.0.1:9999")
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            with pytest.raises(web.HTTPForbidden):
                await terminal.api_terminal_ws(req)

    @pytest.mark.asyncio
    async def test_rejects_a_remote_origin(self):
        req = _make_request(origin="https://evil.example")
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            with pytest.raises(web.HTTPForbidden):
                await terminal.api_terminal_ws(req)

    @pytest.mark.asyncio
    async def test_origin_is_checked_before_authentication(self):
        """A cross-origin handshake is refused whether or not it is authorised.

        Ordering matters for the audit trail: the rejection has to name the
        origin rather than report an unauthenticated caller, or a CSWSH attempt
        is indistinguishable from someone opening the panel while logged out.
        """
        req = _make_request(user=None, origin="http://127.0.0.1:9999")
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            with pytest.raises(web.HTTPForbidden):
                await terminal.api_terminal_ws(req)

    @pytest.mark.asyncio
    async def test_allows_the_dashboard_origin(self):
        """An allowed Origin passes the check and reaches the later gates."""
        req = _make_request(user=None, origin="http://localhost:5476")
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_ws(req)
        # Past the origin gate, so it fails on authentication instead.
        assert isinstance(resp, web.Response)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_allows_a_loopback_client_with_no_origin(self):
        """No Origin from loopback is a local process, not a browser page.

        ``check_origin`` trusts this case and local callers depend on it.
        """
        req = _make_request(user=None, origin=None, remote="127.0.0.1")
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_ws(req)
        assert isinstance(resp, web.Response)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_rejects_a_non_loopback_client_with_no_origin(self):
        req = _make_request(user=None, origin=None, remote="10.0.0.5")
        with patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            with pytest.raises(web.HTTPForbidden):
                await terminal.api_terminal_ws(req)


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
    async def test_rejects_when_reservation_placeholder_held(self):
        # A None value under the session id is another handler's in-flight
        # reservation (held across its awaits). A concurrent connect for the
        # same id must get 409 — not read it as absent and double-spawn.
        registry: dict = {"racing": None}
        req = _make_request(registry=registry, session_id="racing")
        with patch.object(terminal, "_sel") as mock_sel, patch.object(
            terminal, "_get_config", return_value={"enabled": True}
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_ws(req)
        assert isinstance(resp, web.Response)
        assert resp.status == 409
        # The loser must not disturb the winner's reservation.
        assert registry == {"racing": None}

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
    async def test_posix_spawn_exports_resolved_shell_env(self, monkeypatch):
        """The POSIX PTY child env must carry SHELL=<resolved shell>.

        A configured shell that differs from the login shell would otherwise
        inherit the login shell's $SHELL, so programs that consult it (vim's
        :sh, tmux default-shell) open the wrong one. The spawn is made to fail
        AFTER the call is recorded so the handler's read loop never starts —
        the assertion is on the captured env, not the failure.
        """
        registry: dict = {}
        req = _make_request(registry=registry, session_id="posix-env")
        req.query = MagicMock()
        req.query.get = lambda *a, **k: None

        ws = AsyncMock()
        ws.closed = False

        # Login shell is bash; configured shell is zsh — the child env must
        # carry zsh, proving the inherited value was overridden.
        monkeypatch.setenv("SHELL", "/bin/bash")
        monkeypatch.setattr(
            terminal.shutil, "which", lambda c: c if c == "/bin/zsh" else None
        )

        fds = os.pipe()  # real fds so the cleanup os.close() calls succeed
        spawn = AsyncMock(side_effect=RuntimeError("stop before read loop"))
        cfg = {"enabled": True, "shell": "/bin/zsh"}
        with patch.object(terminal.platform_compat, "IS_POSIX", True), \
             patch.object(terminal.platform_compat, "IS_WINDOWS", False), \
             patch.object(terminal._pty, "openpty", return_value=fds), \
             patch.object(terminal.fcntl, "ioctl", lambda *a: None), \
             patch.object(terminal.asyncio, "create_subprocess_exec", spawn), \
             patch.object(terminal, "_get_config", return_value=cfg), \
             patch.object(terminal.web, "WebSocketResponse", return_value=ws), \
             patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_ws(req)

        assert resp is ws
        spawn.assert_awaited_once()
        assert spawn.call_args.args[0] == "/bin/zsh"
        assert spawn.call_args.kwargs["env"]["SHELL"] == "/bin/zsh"

    @pytest.mark.asyncio
    async def test_bash_spawn_uses_the_post_profile_init_stream(self, monkeypatch):
        registry: dict = {}
        req = _make_request(registry=registry, session_id="bash-ready")
        req.query = MagicMock()
        req.query.get = lambda *a, **k: None

        ws = AsyncMock()
        ws.closed = False
        monkeypatch.setenv("SHELL", "/bin/bash")
        monkeypatch.setattr(
            terminal.shutil, "which", lambda c: c if c == "/bin/bash" else None
        )

        pty_fds = os.pipe()
        spawn = AsyncMock(side_effect=RuntimeError("stop before read loop"))
        fake_pty = MagicMock()
        fake_pty.openpty.return_value = pty_fds
        fake_fcntl = MagicMock()
        fake_termios = MagicMock(TIOCSWINSZ=1, TIOCSCTTY=2)
        with patch.object(terminal.platform_compat, "IS_POSIX", True), \
             patch.object(terminal.platform_compat, "IS_WINDOWS", False), \
             patch.object(terminal, "_pty", fake_pty), \
             patch.object(terminal, "fcntl", fake_fcntl), \
             patch.object(terminal, "termios", fake_termios), \
             patch.object(terminal.asyncio, "create_subprocess_exec", spawn), \
             patch.object(terminal, "_get_config", return_value={"enabled": True}), \
             patch.object(terminal.web, "WebSocketResponse", return_value=ws), \
             patch.object(terminal, "_sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_ws(req)

        assert resp is ws
        args = spawn.call_args.args
        assert args[0].replace("\\", "/").endswith("/bin/bash")
        assert args[1] == "--init-file"
        assert args[2].startswith("/dev/fd/")
        assert args[3] == "-i"
        inherited = spawn.call_args.kwargs["pass_fds"]
        assert inherited == (int(args[2].rsplit("/", 1)[-1]),)

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


# ── scrollback ring buffer ──


class TestStreamIsForwardedUnscanned:
    """The live PTY stream and its scrollback replay are byte copies.

    Scanning PTY output on its way to the browser was removed. It protected
    nothing the browser does not already show — the user's own shell printed
    those bytes into a panel only that authenticated operator can see, and any
    threat that reaches it reaches their real terminal too — while costing real
    correctness: it hid a token printed on purpose (``gh auth token``), swallowed
    a device-code login or presigned URL mid-flow, and mis-fired on high-entropy
    build output such as an npm ``integrity sha512-…`` line. It also forced a
    decode, and a PTY read ends wherever the kernel had bytes, so a multi-byte
    character split across two reads became two U+FFFD — permanently corrupting
    CJK, emoji, and a TUI's box-drawing glyphs.

    The credential boundary is the selection hand-off, which is where output
    actually reaches a model, and it has no opt-out (``TestApiTerminalRedact``).
    """

    def test_redactors_are_called_only_by_the_selection_handler(self):
        """Source guard for the single-scan-site design. A scan reappearing on the
        streaming path brings back both the U+FFFD corruption and the false
        positives, and leaves two places in the module each claiming to be the
        credential boundary — so the count is pinned rather than reviewed."""
        src = pathlib.Path(terminal.__file__).read_text(encoding="utf-8")
        # The import lists these names without a paren, so a paren-suffixed
        # count counts call sites and never the import.
        assert src.count("redact_credentials(") == 1
        assert src.count("redact_exfiltration_urls(") == 1
        scan = src.index("    def _scan(t: str) -> str:")
        region = src[scan:src.index("\n    try:", scan)]
        assert "redact_credentials(" in region
        assert "redact_exfiltration_urls(" in region

    def test_session_reservation_has_no_await(self):
        """Source guard for the reservation critical section.

        The handler states its own invariant with the comment "Reserve slot
        synchronously before any await to prevent race condition"; from there
        through ``registry[session_id] = None`` the max-sessions check and the
        placeholder assignment are only atomic while nothing suspends between
        them. An await in there lets two concurrent opens for one session id both
        pass the check and both spawn a PTY, leaking one untracked.

        Deliberately starts at that comment, not at ``registry.get``: the
        stale-entry cleanup above it awaits ``_kill_session`` and is pre-existing,
        which is exactly why the authors drew the line where they did."""
        src = pathlib.Path(terminal.__file__).read_text(encoding="utf-8")
        start = src.index("    # Reserve slot synchronously before any await")
        end = src.index("        registry[session_id] = None", start)
        # Strip comments first: the anchor comment itself says the word "await",
        # so a bare substring check matches the prose and never the code.
        region = "\n".join(
            line for line in src[start:end].splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "await" not in region, f"await in the reservation region:\n{region}"

    def test_no_send_through_the_session_field_after_an_await(self):
        """Source guard. ``sess.ws`` is set to None by the WS handler on
        disconnect, so dereferencing it after a suspension point raises
        AttributeError — which ``except OSError`` does NOT catch, killing the PTY
        reader and stopping scrollback capture for a session the client may still
        reconnect to. Every send must go through a captured local that was
        revalidated after the await, so the pattern `sess.ws.send` must not exist
        anywhere in this module."""
        src = pathlib.Path(terminal.__file__).read_text(encoding="utf-8")
        assert "sess.ws.send" not in src
        # And the read loop's send must revalidate the capture after the lock.
        assert src.count("if sess.ws is not live or live.closed:") == 1


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
    # The handshake validates its Origin in the handler, so the app needs the
    # same allowlist the real gateway builds.
    app["allowed_origins"] = {"http://localhost:5476"}
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
    async def test_stream_and_replay_forward_output_verbatim(self, monkeypatch, tmp_path):
        """End-to-end through the real PTY read loop: a credential the user's own
        shell printed reaches the browser unchanged, and so does the scrollback
        replayed on reconnect. Neither path scans or rewrites bytes — the scan
        lives at the selection hand-off (``TestApiTerminalRedact``), which is the
        only place this output can reach a model. Drives the shipped loop rather
        than re-deriving it, so a regression in the wiring fails here too."""
        key = "AKIAIOSFODNN7EXAMPLE"
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_enabled_cache", [True, 0.0])
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())
        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async def _drain(ws, needle, budget=60):
            # The shell echoes the typed line before its output, so read until a
            # marker resolves rather than trusting a frame count. Stops on
            # REDACTED too: if a regression starts scanning this path the needle
            # never arrives, and without that stop the test would burn the whole
            # budget in receive timeouts instead of failing on its assertion.
            seen = b""
            for _ in range(budget):
                msg = await ws.receive(timeout=5)
                if msg.type != web.WSMsgType.BINARY:
                    continue
                seen += msg.data
                if needle in seen or b"REDACTED" in seen:
                    break
            return seen

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/verbatim") as ws:
                # Split the literal in the TYPED line so the shell's echo of it
                # cannot satisfy the needle — only the printf's real output joins
                # the halves. Asserting on the echo would prove far less: it
                # never reaches the redactors' notion of a credential either way.
                await ws.send_bytes(f"printf '{key[:13]}''{key[13:]}\\n'\n".encode())
                live = await _drain(ws, key.encode())
                await ws.close()
            assert key.encode() in live
            assert b"REDACTED" not in live

            # Reconnect: the ring buffer is replayed as the same raw bytes.
            async with client.ws_connect("/api/ws/terminal/verbatim") as ws:
                replay = await _drain(ws, key.encode())
                await ws.close()
            assert key.encode() in replay
            assert b"REDACTED" not in replay
            await terminal._kill_session(registry["verbatim"])

    @pytest.mark.asyncio
    async def test_multibyte_output_survives_read_boundaries(self, monkeypatch, tmp_path):
        """A PTY read ends wherever the kernel had bytes, so a multi-byte character
        is routinely split across two reads. Nothing decodes server-side, so both
        halves are forwarded and the client's own decoder joins them.

        Decoding each read independently (which scanning the stream required)
        turned every such split into two U+FFFD, permanently corrupting CJK,
        emoji and a TUI's box-drawing glyphs — the client cannot recover the
        original bytes from a replacement char.

        The payload is deliberately ONE line with no newline in it, because a
        shell that writes line by line hands the reader whole lines and every
        read then lands on a character boundary by accident — an earlier version
        of this test passed against the corrupting code for exactly that reason.
        A single unbroken run of 3-byte characters longer than one 4096-byte read
        cannot be split cleanly, since 4096 is not a multiple of 3."""
        char = "中"
        count = 3000  # 9000 bytes: at least two reads, neither aligned
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_enabled_cache", [True, 0.0])
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())
        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/multibyte") as ws:
                await ws.send_bytes(
                    f"printf '{char}%.0s' $(seq 1 {count}); printf 'DO''NE\\n'\n".encode()
                )
                seen = b""
                for _ in range(400):
                    msg = await ws.receive(timeout=10)
                    if msg.type != web.WSMsgType.BINARY:
                        continue
                    seen += msg.data
                    if b"DONE" in seen:
                        break
                await ws.close()
            await terminal._kill_session(registry["multibyte"])

        assert "\ufffd".encode() not in seen, "a read boundary corrupted a character"
        assert seen.count(char.encode()) >= count

    @pytest.mark.asyncio
    async def test_submitted_line_invalidates_the_cwd_memo(self, monkeypatch, tmp_path):
        """A submitted line may be a `cd`, so it drops the completion route's cwd
        memo and re-arms the title poller; a keystroke that submits nothing
        leaves the memo intact (otherwise every character typed would force a
        fresh cwd probe)."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async def _drain_to_pong(ws):
            # The write loop handles frames in order, so a pong proves the
            # preceding binary frame has already been processed.
            await ws.send_str(json.dumps({"type": "ping"}))
            for _ in range(40):
                msg = await ws.receive(timeout=3)
                if msg.type == web.WSMsgType.TEXT and json.loads(msg.data).get("type") == "pong":
                    return
            raise AssertionError("no pong received")

        async with TestClient(TestServer(app)) as client:
            async with client.ws_connect("/api/ws/terminal/cwd-memo-sess") as ws:
                sess = registry["cwd-memo-sess"]

                sess.cwd_probe = (time.monotonic(), "/tmp/old")
                await ws.send_bytes(b"c")
                await _drain_to_pong(ws)
                assert sess.cwd_probe is not None

                await ws.send_bytes(b"d /tmp\r")
                await _drain_to_pong(ws)
                assert sess.cwd_probe is None

                # The submitted line is the one input that can change the cwd,
                # so it re-arms the title poller directly rather than relying on
                # the shell's echo to do it. Stop the PTY reader first, since
                # that echo would otherwise re-arm the session either way and
                # the assertion would prove nothing.
                if sess.reader_task is not None:
                    sess.reader_task.cancel()
                sess.frames_dirty = False
                await ws.send_bytes(b"cd /tmp\r")
                await _drain_to_pong(ws)
                assert sess.frames_dirty is True

                await ws.close()

            await terminal._kill_session(registry["cwd-memo-sess"])

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
            # The startup barrier has already completed for this PTY. A new
            # browser must receive the same readiness state after replay.
            sess.shell_ready = True

            # Reconnect
            async with client.ws_connect("/api/ws/terminal/recon-sess") as ws:
                sess = registry["recon-sess"]
                assert terminal._sess_pid(sess) == original_pid  # same PTY
                assert sess.ws is not None  # reconnected
                assert sess.last_ws_disconnect is None
                for _ in range(40):
                    msg = await ws.receive(timeout=3)
                    if msg.type == web.WSMsgType.TEXT:
                        if json.loads(msg.data).get("type") == "ready":
                            break
                else:
                    raise AssertionError("reconnected initialized shell did not send ready")
                await ws.close()

            await terminal._kill_session(registry["recon-sess"])

    @pytest.mark.asyncio
    async def test_ws_takeover_displaced_socket_keeps_new_ws(self, monkeypatch, tmp_path):
        """A displaced socket's cleanup must not clobber the takeover socket.

        A second window (e.g. the terminal panel popping out) reconnects to a
        session WHILE the first window's WS is still attached. The handler
        replaces ``sess.ws`` with the new socket; when the displaced handler's
        write loop then unwinds, its ``finally`` block must leave ``sess.ws``
        pointing at the live takeover socket -- clearing it would silence PTY
        output to the new window even though it is connected.
        """
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"dashboard": {"terminal": {"enabled": True}}}))
        monkeypatch.setattr(terminal, "config_path", lambda: cfg_file)
        monkeypatch.setattr(terminal, "_sel", lambda: MagicMock())

        registry: dict = {}
        app = _make_app(registry=registry)

        from aiohttp.test_utils import TestClient, TestServer

        async with TestClient(TestServer(app)) as client:
            ws1 = await client.ws_connect("/api/ws/terminal/takeover-sess")
            sess = registry["takeover-sess"]
            original_pid = terminal._sess_pid(sess)
            first_ws = sess.ws
            assert first_ws is not None

            # Second client takes over the session while ws1 is still open.
            ws2 = await client.ws_connect("/api/ws/terminal/takeover-sess")
            sess = registry["takeover-sess"]
            assert terminal._sess_pid(sess) == original_pid  # same PTY
            takeover_ws = sess.ws
            assert takeover_ws is not None
            assert takeover_ws is not first_ws  # replaced by the new socket

            # The displaced client closes; its server-side handler unwinds.
            await ws1.close()
            # Give the displaced handler's finally block a chance to run.
            for _ in range(50):
                await asyncio.sleep(0.05)
                if sess.ws is takeover_ws and sess.last_ws_disconnect is None:
                    break
            assert sess.ws is takeover_ws  # NOT clobbered to None
            assert sess.last_ws_disconnect is None

            # And the new socket still works end-to-end (control ping).
            await ws2.send_str(json.dumps({"type": "ping"}))
            got_pong = False
            for _ in range(40):
                msg = await ws2.receive(timeout=3)
                if msg.type == web.WSMsgType.TEXT and json.loads(msg.data).get("type") == "pong":
                    got_pong = True
                    break
            assert got_pong

            await ws2.close()
            await terminal._kill_session(registry["takeover-sess"])

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
                self._reads = iter((b"PS> ", b""))

            def read(self, size=4096):
                return next(self._reads)  # prompt, then EOF

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
                prompt = await ws.receive(timeout=3)
                ready = await ws.receive(timeout=3)
                assert prompt.type == web.WSMsgType.BINARY
                assert prompt.data == b"PS> "
                assert ready.type == web.WSMsgType.TEXT
                assert json.loads(ready.data) == {"type": "ready"}
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
                # The backend's Bash init stream emits readiness only after the
                # login profile chain returns. Wait for that control frame
                # before writing the first probe, exactly as Run in terminal
                # does; prompt text and timing are deliberately irrelevant.
                loop = asyncio.get_event_loop()
                ready_deadline = loop.time() + 15
                ready_seen = False
                while loop.time() < ready_deadline:
                    msg = await ws.receive(timeout=ready_deadline - loop.time())
                    if msg.type == web.WSMsgType.TEXT:
                        if json.loads(msg.data).get("type") == "ready":
                            ready_seen = True
                            break
                    elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                        break
                assert ready_seen, "shell never emitted the post-profile ready frame"

                # Drive the PTY off INPUT ECHO, not an unsolicited prompt. A
                # login shell on a minimal build host (no MOTD, empty PS1) may
                # render no recognizable prompt. The probe confirms the shell
                # is interactive and consuming stdin without depending on one.
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

                # Run a long-lived sleep in the foreground; drain until we see
                # the command echoed back (so we know the shell is processing
                # it, not buffering it pre-prompt). The duration is chosen to be
                # far larger than the SIGINT-verification budget below: a
                # genuinely dropped SIGINT must leave `sleep` running for the
                # whole budget, so it can never end on its own and let the shell
                # run the queued marker echo (which would be a false pass). Here
                # this drain WANTS the line-discipline echo — it only proves the
                # shell received the input, so matching the echoed command text
                # is correct.
                await ws.send_bytes(b"sleep 120\n")
                echoed = await _drain_until(
                    ws,
                    lambda b: b"sleep 120" in b,
                    budget_secs=5,
                )
                assert b"sleep 120" in echoed, (
                    "shell did not echo `sleep 120` within 5s — "
                    "input may not have reached an interactive shell"
                )

                # Deliver SIGINT and confirm the child actually received it.
                # This step is inherently racy against shell scheduling on a
                # loaded CI host, in two ways the old single-shot version did
                # not survive:
                #   * The ``sleep 120`` echo drained above is emitted by the PTY
                #     line discipline the instant the bytes arrive — BEFORE the
                #     shell has necessarily read the line and forked ``sleep``
                #     into the foreground process group. A ``\x03`` that lands in
                #     that window is delivered to the shell sitting at its prompt
                #     (which simply discards the pending line) rather than to
                #     ``sleep``, so the FIRST Ctrl+C can miss the child.
                #   * Under this class's xdist integration group the forked shell
                #     / reader thread can go unscheduled past a fixed budget, so
                #     a single marker drain can time out even when delivery would
                #     eventually succeed.
                # Handle both by re-poking with Ctrl+C and re-probing the marker
                # over a generous overall budget, rather than the old "\x03 once,
                # wait for a prompt redraw, probe once" — a login shell on a
                # minimal host may render no prompt at all (see the readiness-gate
                # note above), which made the prompt drain a pure time sink and
                # left the single probe to absorb the whole race.
                #
                # EXECUTION-only marker: the probe command is written so the PTY
                # line-discipline echo of our own keystrokes never contains the
                # search token. The typed bytes are ``echo SIG''INT_OK`` (an
                # empty '' splits the literal), so the echoed input reads
                # ``SIG''INT_OK`` — no ``SIGINT_OK`` substring — while only the
                # shell's *execution* of the echo emits the concatenated
                # ``SIGINT_OK`` on stdout. A match therefore proves the shell ran
                # a command, i.e. SIGINT killed the foreground ``sleep`` and
                # returned the shell to its prompt. Matching the bare echoed input
                # (the previous version) let the test pass even when SIGINT was
                # never delivered — a false pass that would hide a real
                # terminal-signal regression.
                sess = registry["sigint-sess"]
                loop = asyncio.get_event_loop()
                overall_deadline = loop.time() + 25
                found = False
                while True:
                    remaining = overall_deadline - loop.time()
                    if remaining <= 0:
                        break
                    # Ctrl+C (ETX): kills the foreground `sleep` if it is
                    # running, or harmlessly aborts an empty prompt line if a
                    # previous iteration already recovered the shell.
                    await ws.send_bytes(b"\x03")
                    await ws.send_bytes(b"echo SIG''INT_OK\n")
                    # Clamp each drain to the remaining budget so the overall
                    # wait cannot overshoot ``overall_deadline`` by a full drain.
                    tail = await _drain_until(
                        ws,
                        lambda b: b"SIGINT_OK" in b,
                        budget_secs=min(5.0, remaining),
                    )
                    if b"SIGINT_OK" in tail:
                        found = True
                        break
                    # Signal may instead have torn down the whole session — that
                    # is also a valid "SIGINT was delivered" outcome.
                    if sess.proc.returncode is not None:
                        break

                # Success: the shell executed a command after Ctrl+C (SIGINT
                # killed sleep, shell continued) OR the process exited (signal
                # was delivered, just tore the whole session down). A dropped
                # SIGINT leaves `sleep 120` running for the whole 25s budget, so
                # neither branch can become true — the test correctly fails.
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


# ── shell input readiness ──


class TestBashShellReadiness:
    """Bash readiness is an explicit post-profile signal, never a PTY timing
    or foreground-process-group inference."""

    def test_recognizes_only_bash_executables(self):
        assert terminal._is_bash_shell("/bin/bash") is True
        assert terminal._is_bash_shell("C:\\tools\\bash.exe") is True
        assert terminal._is_bash_shell("/bin/zsh") is False

    def test_init_script_marks_ready_after_the_login_profile_chain(self):
        script = terminal._bash_init_script("abc123")
        marker = b"builtin printf '\\033]697;KiroCrewReady;abc123\\007'"
        assert script.index(b". /etc/profile") < script.index(b'. "$HOME/.bash_profile"')
        assert script.index(b'. "$HOME/.bash_profile"') < script.index(marker)
        assert script.index(b'. "$HOME/.bash_login"') < script.index(marker)
        assert script.index(b'. "$HOME/.profile"') < script.index(marker)

    def test_marker_match_is_split_safe_and_one_shot(self):
        sess = _make_session()
        sess.shell_ready = False
        sess.ready_marker = b"<random-ready-marker>"

        assert terminal._consume_ready_marker(sess, b"output<random-") is False
        assert sess.shell_ready is False
        assert terminal._consume_ready_marker(sess, b"ready-marker>prompt") is True
        assert sess.shell_ready is True
        assert sess.ready_marker is None
        assert sess.ready_probe == bytearray()
        assert terminal._consume_ready_marker(sess, b"<random-ready-marker>") is False

    def test_unrelated_output_never_releases_the_barrier(self):
        sess = _make_session()
        sess.shell_ready = False
        sess.ready_marker = b"<random-ready-marker>"

        assert terminal._consume_ready_marker(sess, b"profile is still waiting") is False
        assert sess.shell_ready is False


# ── one cwd probe per poll tick ──


@pytest.mark.skipif(
    terminal.platform_compat.IS_WINDOWS,
    reason="the cwd/title probes are POSIX-only (os.tcgetpgrp, /proc, libproc); "
    "both helpers return None on Windows",
)
class TestCwdProbeSharing:
    """The title label and the cwd frame are two consumers of ONE answer: with no
    foreground command the title is just the cwd's basename. On a host where
    _proc_cwd has to fork lsof — no /proc and no libproc — asking twice per tick
    is the entire cost of an otherwise idle terminal, so the session memo has to
    collapse them into one probe."""

    def test_memoizes_within_the_ttl(self):
        sess = _make_session()
        with patch.object(terminal.platform_compat, "IS_POSIX", True), \
             patch.object(terminal, "_proc_cwd", return_value="/tmp/a") as probe:
            assert terminal._session_cwd(sess) == "/tmp/a"
            assert terminal._session_cwd(sess) == "/tmp/a"
        assert probe.call_count == 1

    def test_reprobes_once_the_ttl_expires(self):
        # The memo must not outlive one tick, or a `cd` would take two polls to
        # show up in the tab title.
        sess = _make_session()
        sess.cwd_probe = (time.monotonic() - terminal._CWD_PROBE_TTL_S - 1, "/tmp/old")
        with patch.object(terminal.platform_compat, "IS_POSIX", True), \
             patch.object(terminal, "_proc_cwd", return_value="/tmp/new") as probe:
            assert terminal._session_cwd(sess) == "/tmp/new"
        assert probe.call_count == 1

    def test_title_and_cwd_frame_share_one_probe(self):
        # Exactly the pair of calls one poll tick makes, in order.
        sess = _make_session()
        with patch.object(terminal.platform_compat, "IS_POSIX", True), \
             patch("os.tcgetpgrp", return_value=12345), \
             patch.object(terminal, "_proc_cwd", return_value="/home/u/proj") as probe:
            assert terminal._session_title(sess) == "proj"
            assert terminal._session_cwd(sess) == "/home/u/proj"
        assert probe.call_count == 1


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
    async def test_does_not_probe_a_session_with_no_new_output(self):
        # A shell cannot change directory or start a command without writing to
        # the PTY, so a session that has produced nothing since its last probe
        # cannot have gone stale. Probing it anyway is what made an idle
        # terminal cost a forked lsof every second on hosts with no /proc.
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)
        sess.frames_dirty = False
        with patch.object(terminal, "_session_title") as title, \
             patch.object(terminal, "_session_cwd") as cwd, \
             patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.poll_terminal_titles(self._app(sess))
        title.assert_not_called()
        cwd.assert_not_called()
        ws.send_str.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disarms_itself_after_probing(self):
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)
        with patch.object(terminal, "_session_title", return_value="vim"), \
             patch.object(terminal, "_session_cwd", return_value="/tmp/x"), \
             patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.poll_terminal_titles(self._app(sess))
        assert sess.frames_dirty is False

    @pytest.mark.asyncio
    async def test_output_landing_mid_probe_re_arms_the_session(self):
        # The flag is cleared BEFORE the probe, so a write that lands while the
        # probe is in flight is picked up on the next tick. Clearing it after the
        # probe would swallow that write and freeze the title until some later,
        # unrelated output happened to re-arm it.
        ws = AsyncMock()
        ws.closed = False
        sess = _make_session(session_id="s1", ws=ws)

        def output_lands_mid_probe(s):
            s.frames_dirty = True  # what read_pty does on every PTY read
            return "vim"

        with patch.object(terminal, "_session_title", side_effect=output_lands_mid_probe), \
             patch.object(terminal, "_session_cwd", return_value=None), \
             patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.poll_terminal_titles(self._app(sess))
        assert sess.frames_dirty is True

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
