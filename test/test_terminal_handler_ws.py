"""Tests for the terminal WebSocket handler against mocked sessions.

Covers the handshake's Origin check, ``api_terminal_ws``, the verbatim
forwarding of the live stream and its scrollback replay, and the
orphaned-terminal reaper.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from spawn_test_helpers import strip_spawn_shim
from terminal_handler_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _clear_enabled_cache,
    _make_request,
    _make_session,
)

from kiro_crew.dashboard.handlers import terminal

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
        with (
            patch.object(terminal, "_sel") as mock_sel,
            patch.object(
                terminal, "_get_config", return_value={"enabled": True, "max_sessions": 3}
            ),
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
        with (
            patch.object(terminal, "_sel") as mock_sel,
            patch.object(terminal, "_get_config", return_value={"enabled": True}),
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
        with (
            patch.object(terminal, "_kill_session", new_callable=AsyncMock) as mock_kill,
            patch.object(terminal, "_sel") as mock_sel,
            patch.object(
                terminal, "_get_config", return_value={"enabled": True, "max_sessions": 3}
            ),
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            # Will fail at ws.prepare since request is a mock, but dead session should be cleaned
            with pytest.raises(Exception):
                await terminal.api_terminal_ws(req)
        mock_kill.assert_awaited_once_with(dead_sess)
        # Dead session killed; placeholder reserved for new spawn
        assert registry.get("abc123") is not dead_sess

    @pytest.mark.asyncio
    async def test_cancels_the_dead_sessions_reader_before_killing_it(self):
        # The kill yields; a reader still running would reach its exit handling
        # with the entry registered and reap it a second time as a shell exit.
        dead_sess = _make_session(session_id="abc123", alive=False)
        reader = MagicMock()
        dead_sess.reader_task = reader
        order: list[str] = []
        reader.cancel.side_effect = lambda: order.append("cancel")

        async def kill(_sess):
            order.append("kill")

        registry = {"abc123": dead_sess}
        req = _make_request(registry=registry, session_id="abc123")
        with (
            patch.object(terminal, "_kill_session", side_effect=kill),
            patch.object(terminal, "_sel") as mock_sel,
            patch.object(
                terminal, "_get_config", return_value={"enabled": True, "max_sessions": 3}
            ),
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            with pytest.raises(Exception):
                await terminal.api_terminal_ws(req)
        assert order == ["cancel", "kill"]
        assert registry.get("abc123") is not dead_sess

    @pytest.mark.asyncio
    async def test_a_reconnect_the_exit_reaped_while_it_queued_is_closed_4001(self):
        # The reconnect queues on replace_lock while the exit handler holds it;
        # the exit pops the entry and releases, and the reconnect then
        # publishes its socket on the dead session. It must be refused with the
        # shell-exited code rather than left attached to an unregistered PTY.
        live = _make_session(session_id="abc123", alive=True)
        registry = {"abc123": live}
        req = _make_request(registry=registry, session_id="abc123")
        ws = MagicMock()
        ws.prepare = AsyncMock()

        async def exit_wins_then_publish(sess, _ws):
            registry.pop(sess.session_id, None)
            return True

        with (
            patch.object(terminal.web, "WebSocketResponse", return_value=ws),
            patch.object(terminal, "_replace_terminal_ws", side_effect=exit_wins_then_publish),
            patch.object(terminal, "_close_terminal_ws_bounded", AsyncMock()) as close,
            patch.object(terminal, "_sel") as mock_sel,
            patch.object(
                terminal, "_get_config", return_value={"enabled": True, "max_sessions": 3}
            ),
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_ws(req)
        assert resp is ws
        close.assert_awaited_once_with(ws, close_code=terminal._TERMINAL_WS_CLOSE_SHELL_EXITED)
        outcomes = [
            (c.kwargs["operation"], c.kwargs["outcome"])
            for c in mock_sel.return_value.log_api_access.call_args_list
        ]
        assert ("terminal.ws.reconnect", "error") in outcomes
        assert ("terminal.ws.reconnect", "ok") not in outcomes
        assert "abc123" not in registry

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
        monkeypatch.setattr(terminal.shutil, "which", lambda c: c if c == "/bin/zsh" else None)

        fds = os.pipe()  # real fds so the cleanup os.close() calls succeed
        spawn = AsyncMock(side_effect=RuntimeError("stop before read loop"))
        cfg = {"enabled": True, "shell": "/bin/zsh"}
        with (
            patch.object(terminal.platform_compat, "IS_POSIX", True),
            patch.object(terminal.platform_compat, "IS_WINDOWS", False),
            patch.object(terminal._pty, "openpty", return_value=fds),
            patch.object(terminal.fcntl, "ioctl", lambda *a: None),
            patch.object(terminal.asyncio, "create_subprocess_exec", spawn),
            patch.object(terminal, "_get_config", return_value=cfg),
            patch.object(terminal.web, "WebSocketResponse", return_value=ws),
            patch.object(terminal, "_sel") as mock_sel,
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_ws(req)

        assert resp is ws
        spawn.assert_awaited_once()
        assert strip_spawn_shim(spawn.call_args.args)[0] == "/bin/zsh"
        assert spawn.call_args.kwargs["env"]["SHELL"] == "/bin/zsh"

    @pytest.mark.asyncio
    async def test_bash_spawn_is_a_login_shell_with_a_prompt_command_marker(
        self,
        monkeypatch,
    ):
        registry: dict = {}
        req = _make_request(registry=registry, session_id="bash-ready")
        req.query = MagicMock()
        req.query.get = lambda *a, **k: None

        ws = AsyncMock()
        ws.closed = False
        monkeypatch.setenv("SHELL", "/bin/bash")
        monkeypatch.setattr(terminal.shutil, "which", lambda c: c if c == "/bin/bash" else None)

        pty_fds = os.pipe()
        spawn = AsyncMock(side_effect=RuntimeError("stop before read loop"))
        fake_pty = MagicMock()
        fake_pty.openpty.return_value = pty_fds
        fake_fcntl = MagicMock()
        fake_termios = MagicMock(TIOCSWINSZ=1, TIOCSCTTY=2)
        with (
            patch.object(terminal.platform_compat, "IS_POSIX", True),
            patch.object(terminal.platform_compat, "IS_WINDOWS", False),
            patch.object(terminal, "_pty", fake_pty),
            patch.object(terminal, "fcntl", fake_fcntl),
            patch.object(terminal, "termios", fake_termios),
            patch.object(terminal.asyncio, "create_subprocess_exec", spawn),
            patch.object(terminal, "_get_config", return_value={"enabled": True}),
            patch.object(terminal.web, "WebSocketResponse", return_value=ws),
            patch.object(terminal, "_sel") as mock_sel,
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_ws(req)

        assert resp is ws
        # The controlling terminal is claimed by the post-exec shim, so the argv
        # the handler builds sits after the shim prefix.
        args = strip_spawn_shim(spawn.call_args.args)
        assert args[0].replace("\\", "/").endswith("/bin/bash")
        # A real login shell, so `shopt -q login_shell` is true and every
        # profile stanza guarded on login-ness runs. The readiness
        # marker rides an inherited PROMPT_COMMAND instead of an rc file,
        # which Bash reads only for NON-login shells.
        assert args[1] == "-l"
        assert len(args) == 2
        assert "--init-file" not in args
        assert "pass_fds" not in spawn.call_args.kwargs
        child_env = spawn.call_args.kwargs["env"]
        assert child_env["PROMPT_COMMAND"] == child_env[terminal._READY_HOOK_VAR]
        assert (
            child_env[terminal._READY_TOKEN_VAR] in child_env["PROMPT_COMMAND"]
            or "%s" in child_env["PROMPT_COMMAND"]
        )

    @pytest.mark.asyncio
    async def test_spawn_asks_the_shim_for_the_terminal_and_passes_no_preexec_fn(self):
        """The controlling terminal is requested post-exec, not in a fork.

        ``preexec_fn`` is what makes CPython fork this whole threaded process, and
        the parent then waits for the clone to ``exec`` inside an un-awaitable
        ``os.read`` on the event loop thread. Its absence here is the fix; the
        ``--ctty-fd=0`` flag is what carries the claim instead.
        """
        registry: dict = {}
        req = _make_request(registry=registry, session_id="ctty-shim")
        req.query = MagicMock()
        req.query.get = lambda *a, **k: None

        ws = AsyncMock()
        ws.closed = False
        fds = os.pipe()
        spawn = AsyncMock(side_effect=RuntimeError("stop before read loop"))
        with (
            patch.object(terminal.platform_compat, "IS_POSIX", True),
            patch.object(terminal.platform_compat, "IS_WINDOWS", False),
            patch.object(terminal._pty, "openpty", return_value=fds),
            patch.object(terminal.fcntl, "ioctl", lambda *a: None),
            patch.object(terminal.asyncio, "create_subprocess_exec", spawn),
            patch.object(terminal, "_get_config", return_value={"enabled": True}),
            patch.object(terminal.web, "WebSocketResponse", return_value=ws),
            patch.object(terminal, "_sel") as mock_sel,
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            await terminal.api_terminal_ws(req)

        spawn.assert_awaited_once()
        assert "preexec_fn" not in spawn.call_args.kwargs
        args = spawn.call_args.args
        assert args[0] == sys.executable
        assert "--ctty-fd=0" in args, "fd 0 is the PTY the child must claim"
        # start_new_session is the setsid the claim depends on, and it is applied
        # by _posixsubprocess in C rather than by Python in a fork child.
        assert spawn.call_args.kwargs["start_new_session"] is True

    @pytest.mark.asyncio
    async def test_unavailable_shim_opens_without_a_terminal_rather_than_forking(self):
        """No fallback to ``preexec_fn`` when the shim source is missing.

        Reintroducing the fork is the defect being fixed, and a gateway the
        loop-stall watchdog kills is a larger harm than a shell whose Ctrl+C does
        not work. Only reachable on a truncated install.
        """
        registry: dict = {}
        req = _make_request(registry=registry, session_id="ctty-no-shim")
        req.query = MagicMock()
        req.query.get = lambda *a, **k: None

        ws = AsyncMock()
        ws.closed = False
        fds = os.pipe()
        spawn = AsyncMock(side_effect=RuntimeError("stop before read loop"))
        with (
            patch.object(terminal.platform_compat, "IS_POSIX", True),
            patch.object(terminal.platform_compat, "IS_WINDOWS", False),
            patch.object(terminal._pty, "openpty", return_value=fds),
            patch.object(terminal.fcntl, "ioctl", lambda *a: None),
            patch.object(terminal, "spawn_shim_argv", return_value=()),
            patch.object(terminal.asyncio, "create_subprocess_exec", spawn),
            patch.object(terminal, "_get_config", return_value={"enabled": True}),
            patch.object(terminal.web, "WebSocketResponse", return_value=ws),
            patch.object(terminal, "logger") as mock_logger,
            patch.object(terminal, "_sel") as mock_sel,
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            await terminal.api_terminal_ws(req)

        spawn.assert_awaited_once()
        assert "preexec_fn" not in spawn.call_args.kwargs
        # Straight to the shell: no interpreter prefix to strip.
        assert strip_spawn_shim(spawn.call_args.args) == spawn.call_args.args
        assert mock_logger.warning.called, "the degraded terminal must be reported"
        assert "controlling terminal" in str(mock_logger.warning.call_args)

    @pytest.mark.asyncio
    async def test_posix_spawn_failure_is_audited(self):
        """The POSIX twin of the ConPTY spawn-failure audit: a shell that fails
        to start records ``terminal.ws.open`` with ``outcome=error`` and a
        ``pty_spawn_failed=`` reason before the error frame is sent."""
        registry: dict = {}
        req = _make_request(registry=registry, session_id="posix-fail")
        req.query = MagicMock()
        req.query.get = lambda *a, **k: None

        ws = AsyncMock()
        ws.closed = False
        fds = os.pipe()
        spawn = AsyncMock(side_effect=RuntimeError("spawn boom"))
        with (
            patch.object(terminal.platform_compat, "IS_POSIX", True),
            patch.object(terminal.platform_compat, "IS_WINDOWS", False),
            patch.object(terminal._pty, "openpty", return_value=fds),
            patch.object(terminal.fcntl, "ioctl", lambda *a: None),
            patch.object(terminal.asyncio, "create_subprocess_exec", spawn),
            patch.object(terminal, "_get_config", return_value={"enabled": True}),
            patch.object(terminal.web, "WebSocketResponse", return_value=ws),
            patch.object(terminal, "_sel") as mock_sel,
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            resp = await terminal.api_terminal_ws(req)

        assert resp is ws
        assert "posix-fail" not in registry
        calls = [
            c.kwargs
            for c in mock_sel.return_value.log_api_access.call_args_list
            if c.kwargs.get("operation") == "terminal.ws.open"
        ]
        assert calls == [
            {
                "caller": "testuser",
                "operation": "terminal.ws.open",
                "outcome": "error",
                "source": "dashboard",
                "resources": "pty_spawn_failed=spawn boom",
            }
        ]

    @pytest.mark.asyncio
    async def test_windows_conpty_spawn_failure_sends_error(self, monkeypatch):
        """On Windows a new WS session spawns a ConPTY shell (kiro_crew.conpty);
        the old 'not supported on Windows' refusal is gone. If the
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

        with (
            patch.object(terminal.platform_compat, "IS_POSIX", False),
            patch.object(terminal.platform_compat, "IS_WINDOWS", True),
            patch("kiro_crew.conpty.WindowsPty", side_effect=RuntimeError("boom")),
            patch.object(terminal, "_get_config", return_value={"enabled": True}),
            patch.object(terminal.web, "WebSocketResponse", return_value=ws),
            patch.object(terminal, "_sel") as mock_sel,
        ):
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

        with (
            patch.object(terminal.platform_compat, "IS_POSIX", False),
            patch.object(terminal.platform_compat, "IS_WINDOWS", True),
            patch("kiro_crew.conpty.WindowsPty", side_effect=RuntimeError("boom")),
            patch.object(terminal, "_get_config", return_value={"enabled": True}),
            patch.object(terminal.web, "WebSocketResponse", return_value=ws),
            patch.object(terminal, "_sel") as mock_sel,
        ):
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
        src = "\n".join(
            pathlib.Path(m.__file__).read_text(encoding="utf-8")
            for m in (terminal, *(sys.modules[o] for o in terminal._OWNERS))
        )
        # The import lists these names without a paren, so a paren-suffixed
        # count counts call sites and never the import.
        assert src.count("redact_credentials(") == 1
        assert src.count("redact_exfiltration_urls(") == 1
        scan = src.index("    def _scan(t: str) -> str:")
        region = src[scan : src.index("\n    try:", scan)]
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
            line for line in src[start:end].splitlines() if not line.lstrip().startswith("#")
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
        src = "\n".join(
            pathlib.Path(m.__file__).read_text(encoding="utf-8")
            for m in (terminal, *(sys.modules[o] for o in terminal._OWNERS))
        )
        assert "sess.ws.send" not in src
        # And the read loop's send must revalidate the capture after the lock.
        assert src.count("if sess.ws is not live or live.closed:") == 1


# ── reap_orphaned_terminals ──


class TestReapOrphanedTerminals:
    @pytest.mark.asyncio
    async def test_reaps_disconnected_session(self):
        sess = _make_session(session_id="s1", alive=True)
        # flake-ok: pre-existing real-clock stamp 2000 s in the past so the session reads as disconnected beyond the reap threshold, moved verbatim; tracked for a de-flake pass
        sess.last_ws_disconnect = time.monotonic() - 2000  # ~33 min ago (> reap threshold)
        state = MagicMock()
        state._terminal_sessions = {"s1": sess}
        app = {"state": state}

        # flake-ok: pre-existing patch of asyncio.sleep that ends the reaper loop with CancelledError on its second tick, moved verbatim; tracked for a de-flake pass
        with (
            patch.object(terminal, "_kill_session", new_callable=AsyncMock) as mock_kill,
            patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
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

        # flake-ok: pre-existing patch of asyncio.sleep that ends the reaper loop with CancelledError on its second tick, moved verbatim; tracked for a de-flake pass
        with (
            patch.object(terminal, "_kill_session", new_callable=AsyncMock) as mock_kill,
            patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
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

        # flake-ok: pre-existing patch of asyncio.sleep that ends the reaper loop with CancelledError on its second tick, moved verbatim; tracked for a de-flake pass
        with (
            patch.object(terminal, "_kill_session", new_callable=AsyncMock) as mock_kill,
            patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
        ):
            await terminal.reap_orphaned_terminals(app)
        mock_kill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_recently_disconnected(self):
        sess = _make_session(session_id="s1", alive=True)
        # flake-ok: pre-existing real-clock stamp 60 s in the past so the session reads as recently disconnected and is kept, moved verbatim; tracked for a de-flake pass
        sess.last_ws_disconnect = time.monotonic() - 60  # 1 min ago (< 15 min threshold)
        state = MagicMock()
        state._terminal_sessions = {"s1": sess}
        app = {"state": state}

        # flake-ok: pre-existing patch of asyncio.sleep that ends the reaper loop with CancelledError on its second tick, moved verbatim; tracked for a de-flake pass
        with (
            patch.object(terminal, "_kill_session", new_callable=AsyncMock) as mock_kill,
            patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
        ):
            await terminal.reap_orphaned_terminals(app)
        mock_kill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handles_missing_state(self):
        app = {"state": None}
        # flake-ok: pre-existing patch of asyncio.sleep that ends the reaper loop with CancelledError on its second tick, moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.reap_orphaned_terminals(app)
        # Should not raise

    @pytest.mark.asyncio
    async def test_handles_no_terminal_sessions_attr(self):
        state = MagicMock(spec=[])  # no attributes
        app = {"state": state}
        # flake-ok: pre-existing patch of asyncio.sleep that ends the reaper loop with CancelledError on its second tick, moved verbatim; tracked for a de-flake pass
        with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError]):
            await terminal.reap_orphaned_terminals(app)
