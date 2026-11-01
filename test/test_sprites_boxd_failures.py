"""Failure paths of the Sprites and boxd providers: refusals that must stay bounded.

The happy paths run end to end in ``test_sprites_session.py`` and
``test_boxd_session.py``. These pin what each layer does when the provider
misbehaves: a token or command never leaks into an error, a failure surfaces as
``SessionEnvironmentUnavailable`` (never a raw transport exception), and a dial
failure reads as a retryable connection error.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import stat
import sys

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from kiro_crew.boxd_session import cli as boxd_cli
from kiro_crew.boxd_session.cli import BoxdCLI, BoxdError
from kiro_crew.coder.remote_worker import RemoteWorkerError
from kiro_crew.session_environment import SessionEnvironmentUnavailable
from kiro_crew.sprites_session import terminal
from kiro_crew.sprites_session.client import SpritesClient, SpritesError, _ws_base
from kiro_crew.sprites_session.shell import SpritesShell, SpritesWorkerLaunch
from kiro_crew.testing.wait import await_until


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments are reachable only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fakes")

TOKEN = "my-org/tok-1/s3cret-value"
NAME = "kc-0123456789abcdef"


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 60))

    return wrapper


# ── boxd CLI ────────────────────────────────────────────────────────────

SCRIPTED = r"""#!{python}
import os, sys, time
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
sys.stdout.write(os.environ.get("FAKE_OUT", "") or "x" * int(os.environ.get("FAKE_SIZE", "0")))
sys.stderr.write(os.environ.get("FAKE_ERR", ""))
sys.exit(int(os.environ.get("FAKE_CODE", "0")))
"""


@pytest.fixture
def scripted(tmp_path, monkeypatch):
    binary = tmp_path / "boxd"
    binary.write_text(SCRIPTED.format(python=sys.executable), encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    def answer(out="", err="", code=0, sleep=0.0, size=0):
        monkeypatch.setenv("FAKE_OUT", out)
        monkeypatch.setenv("FAKE_SIZE", str(size))
        monkeypatch.setenv("FAKE_ERR", err)
        monkeypatch.setenv("FAKE_CODE", str(code))
        monkeypatch.setenv("FAKE_SLEEP", str(sleep))

    return BoxdCLI(str(binary)), answer


def test_a_missing_cli_says_how_to_install_it(monkeypatch):
    monkeypatch.setattr(boxd_cli.shutil, "which", lambda name, path=None: None)
    with pytest.raises(BoxdError, match="boxd auth login"):
        boxd_cli.resolve_boxd_bin()


@sync
async def test_an_unrunnable_binary_is_unavailable_not_an_oserror(tmp_path):
    with pytest.raises(BoxdError, match="could not run the boxd CLI"):
        await BoxdCLI(str(tmp_path / "missing")).list_machines()


@sync
async def test_cli_failures_are_bounded_and_name_the_call(scripted):
    cli, answer = scripted
    answer(err="first\nboom: quota exceeded", code=1)
    with pytest.raises(BoxdError, match="boxd machine list failed: boom: quota exceeded"):
        await cli.list_machines()
    with pytest.raises(BoxdError, match="machine get failed"):
        await cli.get(NAME)
    with pytest.raises(BoxdError, match="machine new failed"):
        await cli.create(NAME)
    with pytest.raises(BoxdError, match="machine remove failed"):
        await cli.remove(NAME)
    with pytest.raises(BoxdError, match="hibernate failed"):
        await cli.hibernate(NAME)
    with pytest.raises(BoxdError, match="config set failed"):
        await cli.set_delete_after(NAME, "3d")
    answer(err="401 unauthorized", code=1)
    with pytest.raises(BoxdError, match="not signed in to boxd"):
        await cli.create(NAME)


@sync
async def test_gone_machines_read_as_absent(scripted):
    cli, answer = scripted
    answer(err="VM 'kc-0123456789abcdef' not found", code=1)
    assert await cli.get(NAME) is None
    assert await cli.remove(NAME) is False
    await cli.hibernate(NAME)  # an already-gone machine needs no hibernate


@sync
async def test_odd_cli_output_is_refused_or_ignored(scripted):
    cli, answer = scripted
    answer(out="not json")
    with pytest.raises(BoxdError, match="invalid JSON"):
        await cli.list_machines()
    answer(out=json.dumps({"not": "a list"}))
    assert await cli.list_machines() == []
    answer(out=json.dumps(["x", {"name": NAME}]))
    assert await cli.list_machines() == [{"name": NAME}]
    answer(out=json.dumps(["a list"]))
    assert await cli.get(NAME) is None
    assert await cli.create(NAME) == {}
    answer(size=boxd_cli._OUTPUT_MAX + 1)
    with pytest.raises(BoxdError, match="too much data"):
        await cli.list_machines()


@sync
async def test_a_hung_cli_is_killed_and_reported_unavailable(scripted):
    cli, answer = scripted
    answer(sleep=30)
    with pytest.raises(BoxdError, match="did not finish in time"):
        await cli._run("machine", "list", timeout=0.5)


@sync
async def test_delete_after_takes_only_a_duration(scripted):
    cli, _answer = scripted
    for bad in ("3 days", "--yes", "1y"):
        with pytest.raises(ValueError):
            await cli.set_delete_after(NAME, bad)


# ── Sprites API client ─────────────────────────────────────────────────


def _server(routes):
    app = web.Application()
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
    return TestServer(app)


async def _lookup(request):
    return web.json_response({"id": "sprite-1", "name": request.match_info["name"]})


#: The bridge's identity lookup, answering as the session's own sprite.
_LOOKUP = ("GET", "/v1/sprites/{name}", _lookup)


async def _read_until(fd: int, want: bytes) -> bytes:
    """Read ``fd`` as it becomes readable until ``want`` arrives (or EOF), under
    ``await_until``'s deadline: never a blocking read that could outlive it."""
    loop = asyncio.get_running_loop()
    os.set_blocking(fd, False)
    got = bytearray()
    done = asyncio.Event()

    def on_ready() -> None:
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            return
        got.extend(chunk)
        if want in got or not chunk:
            done.set()

    loop.add_reader(fd, on_ready)
    try:
        await await_until(done, what=f"terminal output containing {want!r}")
    finally:
        loop.remove_reader(fd)
    return bytes(got)


@sync
async def test_the_bridge_opens_nothing_on_a_sprite_that_is_not_the_recorded_one():
    """Another account's token saved since: the name answers with another id there."""
    opened: list[str] = []

    async def other(request):
        return web.json_response({"id": "someone-elses", "name": request.match_info["name"]})

    async def exec_tty(request):
        opened.append(request.path)
        return web.Response(status=500)

    server = _server(
        [("GET", "/v1/sprites/{name}", other), ("GET", "/v1/sprites/{name}/exec", exec_tty)]
    )
    await server.start_server()
    try:
        with pytest.raises(SessionEnvironmentUnavailable, match="nothing was opened"):
            await terminal.run(
                "kirocrew-0123456789abcdef",
                TOKEN,
                "",
                sprite_id="sprite-1",
                api_url=str(server.make_url("")).rstrip("/"),
            )
    finally:
        await server.close()
    assert opened == []


def test_the_api_url_must_be_http_and_a_token_is_required():
    assert _ws_base("https://a.example") == "wss://a.example"
    assert _ws_base("http://127.0.0.1:1") == "ws://127.0.0.1:1"
    with pytest.raises(ValueError):
        _ws_base("ftp://a.example")
    with pytest.raises(SpritesError, match="no Sprites API token"):
        SpritesClient("")


@sync
async def test_http_failures_carry_the_status_and_detail_never_the_token():
    async def fail(request):
        return web.json_response({"error": "quota reached"}, status=500)

    async def gone(request):
        return web.json_response({}, status=404)

    async def text(request):
        return web.Response(text="<html>", status=200)

    server = _server(
        [
            ("GET", "/v1/sprites", fail),
            ("POST", "/v1/sprites", fail),
            ("GET", "/v1/sprites/broken", fail),
            ("GET", "/v1/sprites/html", text),
            ("DELETE", "/v1/sprites/gone", gone),
            ("DELETE", "/v1/sprites/broken", fail),
        ]
    )
    await server.start_server()
    try:
        client = SpritesClient(TOKEN, base_url=str(server.make_url("")).rstrip("/"))
        for call in (
            client.list_sprites(prefix="kirocrew-"),
            client.create_sprite("kirocrew-x"),
            client.get_sprite("broken"),
            client.get_sprite("html"),
            client.delete_sprite("broken"),
        ):
            with pytest.raises(SpritesError, match=r"HTTP (500|200)") as info:
                await call
            assert TOKEN not in str(info.value)
        assert "quota reached" in str(info.value)
        assert await client.delete_sprite("gone") is False
    finally:
        await server.close()


@sync
async def test_an_unreachable_api_is_unavailable():
    client = SpritesClient(TOKEN, base_url="http://127.0.0.1:9")
    with pytest.raises(SpritesError, match="could not reach the Sprites API"):
        await client.get_sprite("s1")
    with pytest.raises(SpritesError, match="could not reach the Sprites API"):
        await client.exec("s1", ["true"])


@sync
async def test_exec_edge_cases_end_in_a_bounded_error():
    async def exec_ws(request):
        mode = request.match_info["name"]
        if mode == "refused":
            return web.json_response({}, status=401)
        if mode == "forbidden":
            return web.json_response({}, status=403)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if mode == "textexit":
            await ws.send_bytes(bytes([1]) + b"hi")
            await ws.send_str(json.dumps({"type": "exit", "exit_code": 4}))
        elif mode == "flood":
            await ws.send_bytes(bytes([1]) + b"x" * 2048)
        elif mode == "hang":
            # An unresponsive provider: read until the client gives up and closes.
            async for _ in ws:
                pass
        await ws.close()
        return ws

    server = _server([("GET", "/v1/sprites/{name}/exec", exec_ws)])
    await server.start_server()
    try:
        client = SpritesClient(TOKEN, base_url=str(server.make_url("")).rstrip("/"))
        assert await client.exec("textexit", ["sh"]) == (4, b"hi", b"")
        with pytest.raises(SpritesError, match="token was refused"):
            await client.exec("refused", ["sh"])
        with pytest.raises(SpritesError, match="HTTP 403"):
            await client.exec("forbidden", ["sh"])
        with pytest.raises(SpritesError, match="too much data"):
            await client.exec("flood", ["sh"], max_output=1024)
        with pytest.raises(SpritesError, match="before it reported an exit code"):
            await client.exec("closed", ["sh"])
        with pytest.raises(SpritesError, match="did not finish in time"):
            await client.exec("hang", ["sh"], timeout=0.3)
        with pytest.raises(ValueError):
            await client.exec("textexit", [])
    finally:
        await server.close()


@sync
async def test_a_proxy_that_never_connects_is_refused():
    async def proxy(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.receive()
        await ws.send_str(json.dumps({"status": "error"}))
        await ws.close()
        return ws

    server = _server([("GET", "/v1/sprites/{name}/proxy", proxy)])
    await server.start_server()
    try:
        client = SpritesClient(TOKEN, base_url=str(server.make_url("")).rstrip("/"))
        with pytest.raises(SpritesError, match="did not connect to port 7"):
            await client.proxy("s1", 7)
        shell = SpritesShell(client, "s1")
        launch = SpritesWorkerLaunch("sp-0123456789abcdef", shell)
        # The link backs off and retries on a ConnectionError.
        with pytest.raises(ConnectionError):
            await launch.dial(7)()
        with pytest.raises(RemoteWorkerError):
            launch.dial_argv(7)
    finally:
        await server.close()


# ── Sprites shell ───────────────────────────────────────────────────────


class _FakeClient:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    async def exec(self, sprite, argv, stdin=b""):
        if self.error is not None:
            raise self.error
        return self.result


@sync
async def test_the_shell_maps_failures_to_remote_worker_errors():
    ok = SpritesShell(_FakeClient(result=(0, b"out", b"")), "s1")  # type: ignore[arg-type]
    assert await ok.run("true") == b"out"
    failed = SpritesShell(_FakeClient(result=(2, b"", b"one\nlast line")), "s1")  # type: ignore[arg-type]
    with pytest.raises(RemoteWorkerError, match=r"exit 2\): last line"):
        await failed.run("false")
    broken = SpritesShell(_FakeClient(error=SpritesError("api down")), "s1")  # type: ignore[arg-type]
    with pytest.raises(RemoteWorkerError, match="api down"):
        await broken.run("true")
    with pytest.raises(RemoteWorkerError):
        ok.argv("true")


# ── The terminal bridge, in process ─────────────────────────────────────


@sync
async def test_the_bridge_relays_bytes_until_the_panel_closes():
    received: list[bytes] = []
    got_input = asyncio.Event()

    async def exec_tty(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_bytes(b"prompt$ ")
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                received.append(msg.data)
                got_input.set()
        return ws

    server = _server([_LOOKUP, ("GET", "/v1/sprites/{name}/exec", exec_tty)])
    await server.start_server()
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    try:
        bridge = asyncio.ensure_future(
            terminal.run(
                "kirocrew-0123456789abcdef",
                TOKEN,
                "",
                sprite_id="sprite-1",
                api_url=str(server.make_url("")).rstrip("/"),
                stdin_fd=in_r,
                stdout_fd=out_w,
            )
        )
        os.write(in_w, b"ls\n")
        await await_until(got_input, what="the terminal receives panel input")
        # The prompt reaches the panel before the panel closes: read it without
        # blocking, so a lost write fails under the deadline instead of hanging.
        assert await _read_until(out_r, b"prompt$ ") == b"prompt$ "
        os.close(in_w)  # the panel closed: the bridge ends the remote input
        assert await await_until(bridge, what="the terminal bridge closes") == 0
        assert received == [b"ls\n"]
    finally:
        for fd in (in_r, out_r, out_w):
            with contextlib.suppress(OSError):
                os.close(fd)
        await server.close()


def test_window_size_falls_back_off_a_terminal():
    read_fd, write_fd = os.pipe()
    try:
        assert terminal._window_size(read_fd) == (80, 24)
        with terminal._raw(read_fd):
            pass  # not a terminal: raw mode is a no-op
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_the_bridge_entry_point_refuses_bad_input_and_a_missing_token(monkeypatch, capsys):
    from kiro_crew.sprites_session import launch

    good = ["--sprite", "kirocrew-0123456789abcdef", "--sprite-id", "sprite-1"]
    assert terminal.main(["--sprite", "other", "--sprite-id", "s", "--token-secret", "X"]) == 2
    assert terminal.main([*good[:2], "--sprite-id=-x", "--token-secret", "X"]) == 2
    monkeypatch.setattr(launch, "_vault_token", lambda name: "")
    assert terminal.main([*good, "--token-secret", "XNAME"]) == 1
    err = capsys.readouterr().err
    assert "no Sprites API token is saved" in err and "XNAME" not in err
    monkeypatch.setattr(launch, "_vault_token", lambda name: TOKEN)

    async def unavailable(*args, **kwargs):
        raise SessionEnvironmentUnavailable("the sprite is gone")

    monkeypatch.setattr(terminal, "run", unavailable)
    assert terminal.main([*good, "--token-secret", "X"]) == 1
    err = capsys.readouterr().err
    assert "the sprite is gone" in err and TOKEN not in err


def test_window_size_and_raw_mode_on_a_real_pty(monkeypatch):
    import fcntl
    import struct
    import termios

    real_tcsetattr = termios.tcsetattr
    restores = []

    def recording_tcsetattr(fd, when, attributes):
        restores.append((fd, [list(a) if isinstance(a, list) else a for a in attributes]))
        real_tcsetattr(fd, when, attributes)

    monkeypatch.setattr(termios, "tcsetattr", recording_tcsetattr)
    primary, worker = os.openpty()
    try:
        fcntl.ioctl(worker, termios.TIOCSWINSZ, struct.pack("HHHH", 33, 101, 0, 0))
        assert terminal._window_size(worker) == (101, 33)
        before = termios.tcgetattr(worker)
        with terminal._raw(worker):
            assert termios.tcgetattr(worker) != before
        # _raw hands back exactly what it saved, PENDIN included.
        assert restores[-1] == (worker, before)
        after = termios.tcgetattr(worker)
        # The kernel sets PENDIN on restore when input waits to be reprinted
        # (macOS does this on a pty); _raw does not control it, so it is masked.
        pendin = getattr(termios, "PENDIN", 0)
        after[3] &= ~pendin
        before[3] &= ~pendin
        assert after == before
    finally:
        os.close(primary)
        os.close(worker)


@sync
async def test_the_bridge_returns_the_remote_shells_exit_code():
    async def exec_tty(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_bytes(b"bye")
        await ws.send_str("not json")
        await ws.send_str(json.dumps({"type": "exit", "exit_code": 7}))
        await ws.close()
        return ws

    server = _server([_LOOKUP, ("GET", "/v1/sprites/{name}/exec", exec_tty)])
    await server.start_server()
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    try:
        code = await terminal.run(
            "kirocrew-0123456789abcdef",
            TOKEN,
            "/workspace",
            sprite_id="sprite-1",
            api_url=str(server.make_url("")).rstrip("/"),
            stdin_fd=in_r,
            stdout_fd=out_w,
        )
        assert code == 7
        assert await _read_until(out_r, b"bye") == b"bye"
    finally:
        for fd in (in_r, in_w, out_r, out_w):
            with contextlib.suppress(OSError):
                os.close(fd)
        await server.close()


def test_a_full_pty_does_not_stall_the_bridge_loop():
    """A blocked PTY write runs off the loop; the frame lands once the PTY drains."""
    import fcntl
    from types import SimpleNamespace

    out_r, out_w = os.pipe()
    try:
        # Fill the pipe so the next blocking write would stall the thread doing it.
        flags = fcntl.fcntl(out_w, fcntl.F_GETFL)
        fcntl.fcntl(out_w, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        with contextlib.suppress(BlockingIOError):
            while True:
                os.write(out_w, b"\0" * 65536)
        fcntl.fcntl(out_w, fcntl.F_SETFL, flags)
        frame = b"terminal output" * 64

        class FakeWs:
            def __init__(self) -> None:
                self.messages = [
                    SimpleNamespace(type=WSMsgType.BINARY, data=frame),
                    SimpleNamespace(
                        type=WSMsgType.TEXT, data=json.dumps({"type": "exit", "exit_code": 3})
                    ),
                ]

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.messages:
                    raise StopAsyncIteration
                return self.messages.pop(0)

        async def run() -> tuple[bool, int]:
            receiver = asyncio.ensure_future(
                terminal._receive(FakeWs(), SimpleNamespace(WSMsgType=WSMsgType), out_w)
            )
            # The loop keeps serving other work (input, resize, heartbeats).
            ticked = asyncio.Event()
            asyncio.get_running_loop().call_soon(ticked.set)
            await await_until(ticked, what="the loop to run while the PTY is full")
            still_blocked = not receiver.done()
            # Drain the PTY; the blocked write finishes and the exit frame follows.
            # Readiness-driven, non-blocking reads, so a frame the receiver
            # dropped (or a receiver that failed) ends under await_until's bound
            # instead of leaving a read blocked on a pipe no one writes to.
            os.set_blocking(out_r, False)
            drained = bytearray()
            settled = asyncio.Event()
            loop = asyncio.get_running_loop()

            def readable() -> None:
                with contextlib.suppress(BlockingIOError):
                    drained.extend(os.read(out_r, 1 << 20))
                if frame in drained:
                    settled.set()

            receiver.add_done_callback(lambda _task: settled.set())
            loop.add_reader(out_r, readable)
            try:
                await await_until(settled, what="the frame to reach the PTY")
            finally:
                loop.remove_reader(out_r)
            readable()  # the receiver can finish before the last bytes were read
            assert frame in drained, receiver.result()  # a failed receiver raises here
            return still_blocked, await await_until(receiver, what="the receiver to finish")

        still_blocked, code = asyncio.run(run())
        assert still_blocked
        assert code == 3
    finally:
        for fd in (out_r, out_w):
            with contextlib.suppress(OSError):
                os.close(fd)
