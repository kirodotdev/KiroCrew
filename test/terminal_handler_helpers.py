"""Shared builders and the autouse fixture for the ``test_terminal_handler*.py`` family.

Every file of the family imports ``_clear_enabled_cache``, so the terminal is
enabled for each of its tests. ``_make_request``, ``_make_session`` and
``_make_app`` build the mock request, session and aiohttp app the handlers
take. The ``_drain_ws_*`` and ``_recv_*`` helpers read frames off a live
terminal WebSocket for ``test_terminal_handler_pty_integration.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import terminal
from kiro_crew.testing.ids import UNALLOCATABLE_PID


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim; no test of the family calls monkeypatch.undo()
def _clear_enabled_cache(monkeypatch):
    """Enable terminal and reset cache between tests."""
    terminal._enabled_cache[0] = True
    # flake-ok: pre-existing real-clock stamp that marks the terminal-enabled cache fresh so each test reads the panel as enabled, moved verbatim; tracked for a de-flake pass
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
    owner_id=None,
    app_claim="",
):
    """Build a mock aiohttp request with state and match_info.

    ``origin`` is the Origin header the handshake carries. Left unset it is
    absent, which ``check_origin`` trusts from loopback — a browser always
    sends one, so its absence means a local process rather than a page.
    """
    state = MagicMock()
    state._terminal_sessions = registry if registry is not None else {}
    state.owner_id = user if owner_id is None else owner_id
    app = {"state": state, "allowed_origins": {"http://localhost:5476"}}
    request = MagicMock()
    request.app = app
    request.get = lambda k, default=None: user if k == "user" else default
    request.__contains__.side_effect = lambda key: key == "app"
    request.__getitem__.side_effect = lambda key: app_claim if key == "app" else KeyError(key)
    request.match_info = MagicMock()
    request.match_info.get = lambda k, default="": session_id if k == "session_id" else default
    request.remote = remote
    request.headers = {} if origin is None else {"Origin": origin}
    request.query = {}
    return request


def _make_session(session_id="s1", alive=True, ws=None, disconnect=None):
    """Build a mock _TerminalSession.

    The mock child's pid is ``UNALLOCATABLE_PID`` (testing-conventions D6), never
    a plausible live pid. ``_kill_session`` signals ``sess.proc.pid`` for real
    unless a test stubs ``platform_compat.kill_process_tree(_async)``, and a
    plausible number names whatever the host runs at that pid: under xdist, a
    sibling worker or one of its threads, which the SIGHUP meant for the mock
    then kills mid-test. A pid above every ``pid_max`` cannot be delivered
    anywhere: ``os.getpgid`` raises ``OverflowError`` before any syscall.
    """
    proc = MagicMock()
    proc.returncode = None if alive else 0
    proc.pid = UNALLOCATABLE_PID
    proc.wait = AsyncMock()
    sess = terminal._TerminalSession(
        session_id=session_id,
        master_fd=99,  # wokeignore:rule=master
        proc=proc,
        ws=ws,
    )
    # Most unit tests exercise an established terminal. Tests for the startup
    # barrier opt back into the fresh-session state explicitly.
    sess.shell_ready = True
    sess.last_ws_disconnect = disconnect
    sess.reader_task = None
    return sess


def _make_app(registry=None, cfg=None, user="testuser", owner_id=None):
    """Build a minimal aiohttp app with terminal routes and fake auth."""
    state = MagicMock()
    state._terminal_sessions = registry if registry is not None else {}
    state.owner_id = user if owner_id is None else owner_id

    @web.middleware
    async def fake_auth(request, handler):
        request["user"] = request.headers.get("X-Test-User", user)
        request["app"] = ""
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


def _unwrapped(buf: bytes) -> bytes:
    """Return *buf* with the PTY's line-wrap artifacts removed.

    The PTY is 80 columns (``TIOCSWINSZ`` 24x80 at spawn) and the host's own
    prompt eats part of that row, so a command that reaches the right margin
    comes back split: bash redraws the wrap point and ``sleep 120`` arrives as
    ``sleep 12 \\r0\\r\\n``. Deleting CR, LF and spaces makes a match
    independent of where the row broke, so every needle compared through this
    helper is written space-free (``sleep120``).

    Squashing cannot turn a miss into a false pass for the SIGINT probe: the
    typed ``SIG''INT_OK`` keeps its quotes here, so ``SIGINT_OK`` still appears
    only in the shell's own execution output.
    """
    return buf.replace(b"\r", b"").replace(b"\n", b"").replace(b" ", b"")


async def _drain_ws_until(ws, predicate, *, budget_secs: float) -> bytes:
    """Read PTY frames into an accumulator until ``predicate(buf)`` is true or
    the overall ``budget_secs`` runs out. Returns the accumulated bytes (the
    caller decides whether the predicate held)."""
    loop = asyncio.get_event_loop()
    # flake-ok: pre-existing loop-clock deadline that bounds the frame drain to budget_secs of real time, moved verbatim; tracked for a de-flake pass
    deadline = loop.time() + budget_secs
    buf = bytearray()
    while True:
        # flake-ok: pre-existing loop-clock read of the remaining drain budget that sizes each bounded receive, moved verbatim; tracked for a de-flake pass
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


async def _drain_ws_while(ws, condition, *, budget_secs: float) -> bool:
    """Keep consuming PTY frames until ``condition()`` holds or the budget runs out.

    The condition is about the PTY's PROCESS state (who owns the foreground),
    not its output, so it is re-read on every short receive slice rather than
    only when a frame happens to arrive; the frames are still consumed so the
    shell is never blocked writing into a full controller buffer. Returns
    whether the condition held.
    """
    loop = asyncio.get_event_loop()
    # flake-ok: pre-existing loop-clock deadline that bounds the foreground-condition poll to budget_secs of real time, moved verbatim; tracked for a de-flake pass
    deadline = loop.time() + budget_secs
    while not condition():
        # flake-ok: pre-existing loop-clock read of the remaining budget that caps each 20 ms receive slice of the poll, moved verbatim; tracked for a de-flake pass
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        try:
            msg = await ws.receive(timeout=min(0.02, remaining))
        except asyncio.TimeoutError:
            continue
        if msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
            return condition()
    return True


def _pty_foreground_pgid(sess) -> int | None:
    """The process group the PTY delivers Ctrl+C to, or None once the controller is gone."""
    try:
        return os.tcgetpgrp(sess.master_fd)  # wokeignore:rule=master
    except OSError:
        return None


async def _recv_matching(ws, predicate, what: str, *, frames: int = 40, timeout: float = 3):
    """Return the first frame satisfying *predicate*, skipping the ones it does not.

    The socket carries three interleaved things, and only one of them is what any given
    test is about: the reply to what the test just sent, raw binary PTY output the shell
    produced on its own, and a ``ready`` control frame.

    ``ready``'s arrival is NOT ordered against the test's own send. When the resolved
    shell has no ready-marker to inject, ``api_terminal_ws`` sets ``shell_ready`` and
    emits ``ready`` while still setting the connection up, so it precedes any reply;
    when the marker IS injected (Bash), ``ready`` waits for the marker to appear in PTY
    output and so usually arrives after. Taking the first TEXT frame, or assuming the
    first frame is BINARY, therefore asserts on which shell the host resolved rather
    than on the behaviour under test -- and fails on a host whose shell is not Bash.

    Bounded by a frame COUNT, not a sleep: every iteration consumes a real frame, so
    this cannot pass by waiting.
    """
    for _ in range(frames):
        msg = await ws.receive(timeout=timeout)
        if msg.type in (
            web.WSMsgType.CLOSE,
            web.WSMsgType.CLOSING,
            web.WSMsgType.CLOSED,
            web.WSMsgType.ERROR,
        ):
            raise AssertionError(f"socket closed ({msg.type!r}) before {what} arrived")
        if predicate(msg):
            return msg
    raise AssertionError(f"{what} did not arrive within {frames} frames")


async def _recv_control(ws, wanted: str, **kw):
    """The JSON control frame whose ``type`` is *wanted*. See :func:`_recv_matching`."""

    def _is_wanted(msg) -> bool:
        if msg.type is not web.WSMsgType.TEXT:
            return False
        try:
            return json.loads(msg.data).get("type") == wanted
        except (ValueError, AttributeError):
            return False

    return json.loads((await _recv_matching(ws, _is_wanted, f"a {wanted!r} frame", **kw)).data)


async def _recv_pty_output(ws, **kw) -> bytes:
    """The first BINARY frame — bytes the PTY itself produced. See :func:`_recv_matching`."""
    msg = await _recv_matching(
        ws, lambda m: m.type is web.WSMsgType.BINARY, "binary PTY output", **kw
    )
    return msg.data
