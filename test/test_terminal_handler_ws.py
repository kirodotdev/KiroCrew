"""Tests for the terminal WebSocket handler against mocked sessions.

Covers the handshake's Origin check, ``api_terminal_ws``, the verbatim
forwarding of the live stream and its scrollback replay, and the
orphaned-terminal reaper.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import pathlib
import sys
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
    async def test_at_the_cap_the_slot_changes_hands_before_any_await(self):
        # The evicted shell's slot must already be this opener's placeholder at
        # the first await (here the socket handshake), or another opener could
        # take it meanwhile. Detached a billion seconds before the clock's origin:
        # past the 60 s floor against any real monotonic reading the handler takes.
        old = _make_session(session_id="old", alive=True, disconnect=-1e9)
        registry = {"old": old, "busy": _make_session(session_id="busy")}
        req = _make_request(registry=registry, session_id="new")
        seen = {}
        ws = AsyncMock()

        async def handshake(_request):
            seen["at_first_await"] = dict(registry)

        ws.prepare.side_effect = handshake

        class _Stop(Exception):
            pass

        async def end_evicted(_app, evicted, cap):
            seen["evicted"], seen["cap"] = evicted[0], cap
            raise _Stop

        with (
            patch.object(terminal, "_sel") as mock_sel,
            patch.object(
                terminal, "_get_config", return_value={"enabled": True, "max_sessions": 2}
            ),
            patch.object(terminal.web, "WebSocketResponse", return_value=ws),
            patch.object(terminal, "_end_evicted_shell", side_effect=end_evicted),
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            with pytest.raises(_Stop):
                await terminal.api_terminal_ws(req)
        assert seen["at_first_await"] == {"busy": registry["busy"], "new": None}
        assert seen["evicted"] == "old" and seen["cap"] == 2
        # A teardown that fails still releases this opener's reservation, and a
        # shell whose teardown began is not put back.
        assert registry == {"busy": registry["busy"]}

    @staticmethod
    def _at_the_cap(cwd_probe):
        """Two shells at a cap of two, one detached long enough to give way, and a
        request for a new id whose ``?cwd=`` resolves through ``cwd_probe``."""
        old = _make_session(session_id="old", alive=True, disconnect=-1e9)
        busy = _make_session(session_id="busy")
        registry = {"old": old, "busy": busy}
        req = _make_request(registry=registry, session_id="new")
        req.query = {"cwd": "/requested"}
        patches = (
            patch.object(terminal, "_sel"),
            patch.object(
                terminal, "_get_config", return_value={"enabled": True, "max_sessions": 2}
            ),
            patch.object(terminal.web, "WebSocketResponse", return_value=AsyncMock()),
            patch.object(terminal, "_close_terminal_ws_bounded", AsyncMock()),
            patch.object(terminal, "_run_path_probe", AsyncMock(side_effect=cwd_probe)),
            patch.object(terminal, "_end_evicted_shell", AsyncMock()),
        )
        return old, busy, registry, req, patches

    @pytest.mark.asyncio
    async def test_an_unavailable_cwd_at_the_cap_puts_the_displaced_shell_back(self):
        # The open is refused before it goes ahead, so the detached shell and its
        # jobs survive, back under their own id, and no reap is announced.
        old, busy, registry, req, patches = self._at_the_cap(OSError("no such directory"))
        with patches[0], patches[1], patches[2], patches[3] as close, patches[4]:
            with patches[5] as end_evicted:
                await terminal.api_terminal_ws(req)
        assert close.await_args.kwargs["error_code"] == "terminal_invalid_cwd"
        end_evicted.assert_not_awaited()
        assert registry == {"old": old, "busy": busy}
        assert registry["old"] is old and old.last_ws_disconnect == -1e9

    @pytest.mark.asyncio
    async def test_an_unavailable_cwd_at_the_cap_returns_the_slot_before_the_refusal(self):
        # The refusal's handshake and close await. The placeholder and the
        # displaced shell share one slot, so by the first of those awaits the
        # shell is back and the placeholder gone: an opener arriving then sees
        # the cap full, not a free slot it could take while the shell returns.
        old, busy, registry, req, patches = self._at_the_cap(OSError("no such directory"))
        seen = []
        ws = AsyncMock()

        async def handshake(_request):
            seen.append(("prepare", dict(registry)))

        async def close(_ws, **_kwargs):
            seen.append(("close", dict(registry)))

        ws.prepare.side_effect = handshake
        with (
            patches[0],
            patches[1],
            patches[4],
            patches[5] as end_evicted,
            patch.object(terminal.web, "WebSocketResponse", return_value=ws),
            patch.object(terminal, "_close_terminal_ws_bounded", side_effect=close),
        ):
            await terminal.api_terminal_ws(req)
        cap_full = {"old": old, "busy": busy}
        assert seen == [("prepare", cap_full), ("close", cap_full)]
        end_evicted.assert_not_awaited()
        assert len(registry) == 2 and registry == cap_full

    @pytest.mark.asyncio
    async def test_a_cancelled_open_at_the_cap_puts_the_displaced_shell_back(self):
        old, busy, registry, req, patches = self._at_the_cap(asyncio.CancelledError())
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with patches[5] as end_evicted, pytest.raises(asyncio.CancelledError):
                await terminal.api_terminal_ws(req)
        end_evicted.assert_not_awaited()
        assert registry == {"old": old, "busy": busy}

    @pytest.mark.asyncio
    async def test_a_refused_open_ends_the_displaced_shell_once_its_id_is_reopened(self):
        # While the probe waited, a dial for the displaced shell's own id opened a
        # fresh entry there: the shell cannot go back, so it ends instead of
        # running on untracked.
        fresh = _make_session(session_id="old")
        holder = {}

        async def reopened_then_refused(*_args, **_kwargs):
            holder["registry"]["old"] = fresh
            raise OSError("no such directory")

        old, busy, registry, req, patches = self._at_the_cap(reopened_then_refused)
        holder["registry"] = registry
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with patches[5] as end_evicted:
                await terminal.api_terminal_ws(req)
        end_evicted.assert_awaited_once()
        assert end_evicted.await_args.args[1][:2] == ("old", old)
        assert end_evicted.await_args.args[2] == 2
        assert registry == {"old": fresh, "busy": busy}

    @pytest.mark.asyncio
    async def test_an_open_that_goes_ahead_ends_the_displaced_shell_before_spawning(self):
        # The directory resolves, so the displaced shell ends ahead of the new
        # shell's spawn. The spawn then fails only to stop before the read loop.
        order = []

        async def end_evicted(_app, evicted, cap):
            order.append(("end", evicted[0], cap))

        async def spawn(*_args, **_kwargs):
            order.append(("spawn",))
            raise RuntimeError("stop before read loop")

        old, busy, registry, req, patches = self._at_the_cap(lambda *_a, **_k: "/resolved")
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patch.object(terminal, "_end_evicted_shell", side_effect=end_evicted),
            patch.object(terminal.platform_compat, "IS_POSIX", True),
            patch.object(terminal.platform_compat, "IS_WINDOWS", False),
            patch.object(terminal._pty, "openpty", return_value=os.pipe()),
            patch.object(terminal.fcntl, "ioctl", lambda *a: None),
            patch.object(terminal.asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn)),
        ):
            await terminal.api_terminal_ws(req)
        assert order == [("end", "old", 2), ("spawn",)]
        assert registry == {"busy": busy}

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
    async def test_unavailable_shim_opens_without_a_terminal_rather_than_forking(self, caplog):
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
            patch.object(terminal, "_sel") as mock_sel,
            caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers.terminal"),
        ):
            mock_sel.return_value.log_api_access = MagicMock()
            await terminal.api_terminal_ws(req)

        spawn.assert_awaited_once()
        assert "preexec_fn" not in spawn.call_args.kwargs
        # Straight to the shell: no interpreter prefix to strip.
        assert strip_spawn_shim(spawn.call_args.args) == spawn.call_args.args
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.name == "kiro_crew.dashboard.handlers.terminal" and r.levelno == logging.WARNING
        ]
        assert any(
            "controlling terminal" in m for m in warnings
        ), "the degraded terminal must be reported"

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


_DETACHED_AT = 1_000_000.0  # an arbitrary monotonic reading: the sweep is handed `now`


def _sweep_app(*sessions, bus=None):
    state = MagicMock()
    state._terminal_sessions = {s.session_id: s for s in sessions}
    state.notification_bus = bus
    return {"state": state}, state._terminal_sessions


async def _sweep(app, idle_s, cfg=None):
    """Run one sweep ``idle_s`` after ``_DETACHED_AT`` with ``cfg`` as the terminal
    config; returns the ``_kill_session`` and ``_sel`` mocks."""
    with (
        patch.object(terminal, "_read_terminal_config", return_value=dict(cfg or {})),
        patch.object(terminal, "_kill_session", new_callable=AsyncMock) as mock_kill,
        patch.object(terminal, "_sel") as mock_sel,
    ):
        await terminal._sweep_terminals(app, _DETACHED_AT + idle_s)
    return mock_kill, mock_sel


class TestOrphanTimeout:
    def test_default_keeps_a_detached_shell_for_twelve_hours(self):
        assert terminal._orphan_timeout_s({}) == 12 * 60 * 60

    def test_a_configured_value_is_used(self):
        assert terminal._orphan_timeout_s({"orphan_timeout_s": 600}) == 600

    @pytest.mark.parametrize("raw", [0, 5, -100, 59.5])
    def test_a_value_below_the_floor_reads_as_the_floor(self, raw):
        assert terminal._orphan_timeout_s({"orphan_timeout_s": raw}) == 60

    @pytest.mark.parametrize("raw", ["600", True, False, None, [600], float("nan"), float("inf")])
    def test_a_malformed_value_reads_as_the_default(self, raw):
        assert terminal._orphan_timeout_s({"orphan_timeout_s": raw}) == 12 * 60 * 60

    @pytest.mark.parametrize(
        ("seconds", "text"),
        [
            (30, "1 minute"),
            (61, "1 minute"),
            (900, "15 minutes"),
            (3600, "1 hour"),
            (43200, "12 hours"),
        ],
    )
    def test_idle_spans_read_in_whole_hours_else_minutes(self, seconds, text):
        assert terminal._format_idle(seconds) == text


class TestSweepTerminals:
    @pytest.mark.asyncio
    async def test_ends_a_shell_detached_past_the_default_and_says_so(self, caplog):
        sess = _make_session(session_id="s1", alive=True, disconnect=_DETACHED_AT)
        bus = MagicMock()
        app, registry = _sweep_app(sess, bus=bus)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.handlers.terminal"):
            mock_kill, mock_sel = await _sweep(app, terminal._ORPHAN_TIMEOUT_S + 1)
        mock_kill.assert_awaited_once_with(sess)
        assert registry == {}
        assert any("no browser attached" in r.getMessage() for r in caplog.records)
        audit = mock_sel.return_value.log_api_access.call_args.kwargs
        assert audit["operation"] == "terminal.session.reap"
        assert "session=s1" in audit["resources"]
        note = bus.push.call_args.args[0]
        assert note.channel == "system.terminal"
        assert note.title == "Terminal closed"
        assert "12 hours" in note.body
        # The feed reaches every dashboard session: no session id in the note.
        assert "s1" not in note.body and "s1" not in json.dumps(note.meta)

    @pytest.mark.asyncio
    async def test_keeps_a_shell_detached_for_longer_than_a_lunch_break(self):
        # Two hours: a laptop closed over lunch must find its shell still running.
        sess = _make_session(session_id="s1", alive=True, disconnect=_DETACHED_AT)
        app, registry = _sweep_app(sess, bus=MagicMock())
        mock_kill, _ = await _sweep(app, 2 * 60 * 60)
        mock_kill.assert_not_awaited()
        assert registry == {"s1": sess}

    @pytest.mark.asyncio
    async def test_honours_a_configured_timeout(self):
        kept = _make_session(session_id="kept", alive=True, disconnect=_DETACHED_AT + 2)
        ended = _make_session(session_id="ended", alive=True, disconnect=_DETACHED_AT)
        app, registry = _sweep_app(kept, ended, bus=MagicMock())
        mock_kill, _ = await _sweep(app, 601, cfg={"orphan_timeout_s": 600})
        mock_kill.assert_awaited_once_with(ended)
        assert registry == {"kept": kept}

    @pytest.mark.asyncio
    async def test_a_tiny_configured_timeout_still_survives_a_reload(self):
        sess = _make_session(session_id="s1", alive=True, disconnect=_DETACHED_AT)
        app, registry = _sweep_app(sess, bus=MagicMock())
        mock_kill, _ = await _sweep(app, 30, cfg={"orphan_timeout_s": 1})
        mock_kill.assert_not_awaited()
        assert registry == {"s1": sess}

    @pytest.mark.asyncio
    async def test_keeps_a_connected_shell(self):
        sess = _make_session(session_id="s1", alive=True, disconnect=None)
        app, registry = _sweep_app(sess, bus=MagicMock())
        mock_kill, _ = await _sweep(
            app, 10 * terminal._ORPHAN_TIMEOUT_S, cfg={"orphan_timeout_s": 60}
        )
        mock_kill.assert_not_awaited()
        assert registry == {"s1": sess}

    @pytest.mark.asyncio
    async def test_cleans_up_an_exited_shell_without_a_reap_notice(self):
        sess = _make_session(session_id="s1", alive=False, disconnect=None)
        bus = MagicMock()
        app, registry = _sweep_app(sess, bus=bus)
        mock_kill, mock_sel = await _sweep(app, 0)
        mock_kill.assert_awaited_once_with(sess)
        assert registry == {}
        mock_sel.return_value.log_api_access.assert_not_called()
        bus.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_refusing_feed_does_not_stop_the_sweep(self):
        first = _make_session(session_id="a", alive=True, disconnect=_DETACHED_AT)
        second = _make_session(session_id="b", alive=True, disconnect=_DETACHED_AT)
        bus = MagicMock()
        bus.push.side_effect = RuntimeError("feed closed")
        app, registry = _sweep_app(first, second, bus=bus)
        mock_kill, _ = await _sweep(app, terminal._ORPHAN_TIMEOUT_S + 1)
        assert mock_kill.await_count == 2
        assert registry == {}

    @pytest.mark.asyncio
    async def test_reads_config_off_the_default_executor(self):
        # Every live PTY reader parks a blocking read on the default executor, so a
        # config read queued there could wait behind the very shells it would end.
        sess = _make_session(session_id="s1", alive=True, disconnect=_DETACHED_AT)
        app, registry = _sweep_app(sess, bus=MagicMock())
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            with patch.object(terminal, "maintenance_executor", return_value=pool) as chosen:
                mock_kill, _ = await _sweep(app, terminal._ORPHAN_TIMEOUT_S + 1)
        finally:
            pool.shutdown(wait=True)
        chosen.assert_called_once_with()
        mock_kill.assert_awaited_once_with(sess)
        assert registry == {}

    @pytest.mark.asyncio
    async def test_no_feed_still_ends_the_shell(self):
        sess = _make_session(session_id="s1", alive=True, disconnect=_DETACHED_AT)
        app, registry = _sweep_app(sess, bus=None)
        mock_kill, _ = await _sweep(app, terminal._ORPHAN_TIMEOUT_S + 1)
        mock_kill.assert_awaited_once_with(sess)
        assert registry == {}


class TestEvictionAtTheCap:
    """At the cap, the longest-detached live shell gives up its slot."""

    _NOW = _DETACHED_AT + 2 * 60 * 60

    def test_takes_the_longest_detached_shell(self):
        older = _make_session(session_id="older", alive=True, disconnect=_DETACHED_AT)
        newer = _make_session(session_id="newer", alive=True, disconnect=_DETACHED_AT + 100)
        connected = _make_session(session_id="connected", alive=True, disconnect=None)
        registry = {"older": older, "newer": newer, "connected": connected}
        evicted = terminal._take_shell_to_evict(registry, 3, self._NOW)
        assert evicted == ("older", older, 2 * 60 * 60)
        assert set(registry) == {"newer", "connected"}

    def test_below_the_cap_nothing_is_taken(self):
        detached = _make_session(session_id="d", alive=True, disconnect=_DETACHED_AT)
        registry = {"d": detached}
        assert terminal._take_shell_to_evict(registry, 2, self._NOW) is None
        assert registry == {"d": detached}

    @pytest.mark.parametrize("cap", [0, 1, 2])
    def test_above_the_cap_nothing_is_taken(self, cap):
        # max_sessions lowered while more shells run: one eviction frees no slot,
        # the opener refuses anyway, and a shell taken here would never be ended.
        registry = {
            f"s{i}": _make_session(session_id=f"s{i}", alive=True, disconnect=_DETACHED_AT + i)
            for i in range(3)
        }
        before = dict(registry)
        assert terminal._take_shell_to_evict(registry, cap, self._NOW) is None
        assert registry == before

    @pytest.mark.asyncio
    async def test_never_takes_a_connected_reloading_returning_or_exited_shell(self):
        returning = _make_session(session_id="returning", alive=True, disconnect=_DETACHED_AT)
        registry = {
            "connected": _make_session(session_id="connected", alive=True, disconnect=None),
            # Detached 30 s ago: a tab mid-reload, inside the floor.
            "reloading": _make_session(
                session_id="reloading", alive=True, disconnect=self._NOW - 30
            ),
            "exited": _make_session(session_id="exited", alive=False, disconnect=_DETACHED_AT),
            "returning": returning,
            "opening": None,
        }
        # A reconnect holds replace_lock for its whole takeover.
        async with returning.replace_lock:
            assert terminal._take_shell_to_evict(registry, 5, self._NOW) is None
        assert set(registry) == {"connected", "reloading", "exited", "returning", "opening"}

    @pytest.mark.asyncio
    async def test_ending_the_shell_says_why(self):
        sess = _make_session(session_id="older", alive=True, disconnect=_DETACHED_AT)
        bus = MagicMock()
        state = MagicMock()
        state.notification_bus = bus
        with (
            patch.object(terminal, "_kill_session", new_callable=AsyncMock) as mock_kill,
            patch.object(terminal, "_sel") as mock_sel,
        ):
            await terminal._end_evicted_shell({"state": state}, ("older", sess, 7200.0), 3)
        mock_kill.assert_awaited_once_with(sess)
        audit = mock_sel.return_value.log_api_access.call_args.kwargs
        assert audit["operation"] == "terminal.session.reap"
        assert "max_sessions=3" in audit["resources"]
        note = bus.push.call_args.args[0]
        assert "dashboard.terminal.max_sessions" in note.body
        assert "2 hours" in note.body
        assert "older" not in note.body


class TestReapOrphanedTerminals:
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
