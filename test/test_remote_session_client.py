"""The bridge CLI (``client.py``), its dial, and the worker CLI, driven in-process over loopback."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import socket
import sys
import threading
from types import SimpleNamespace

import pytest

from kiro_crew.remote_session import client as client_mod
from kiro_crew.remote_session import link as link_mod
from kiro_crew.remote_session import wire
from kiro_crew.remote_session import worker as worker_mod
from kiro_crew.testing.wait import async_wait_until, wait_until

pytestmark = pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX guest paths")


@pytest.fixture(autouse=True)
def _drain_asyncio_transports():
    """Collect asyncio transport cycles after each test in this module.

    Tests here spawn asyncio subprocesses via asyncio.run(); a
    BaseSubprocessTransport whose subprocess is not awaited before the loop
    closes forms a reference cycle with its protocol.  Collecting after each
    test keeps such a cycle from surfacing inside the next test, in this file
    or in a later module on the same worker.
    """
    import gc

    yield
    gc.collect()
    gc.collect()


SID = "sess-1"
_ENDED = (link_mod.REFUSED, link_mod.LOST, link_mod.FINISHED)
ECHO = [
    sys.executable,
    "-u",
    "-c",
    "import sys\nfor l in sys.stdin.buffer:\n sys.stdout.buffer.write(l); sys.stdout.buffer.flush()",
]


def _private(tmp_path, name: str, text: str, mode: int = 0o600) -> str:
    path = tmp_path / name
    path.write_text(text)
    os.chmod(path, mode)
    return str(path)


def _stdio(monkeypatch, data: bytes) -> io.TextIOWrapper:
    r, w = os.pipe()
    os.write(w, data)
    os.close(w)
    out = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(fileno=lambda: r))
    monkeypatch.setattr(sys, "stdout", out)
    return out


@contextlib.contextmanager
def _peer(reply=lambda data: data, greeting: bytes = b""):
    """One loopback connection: send ``greeting``, read to EOF, answer ``reply(data)``."""
    srv = socket.create_server(("127.0.0.1", 0))
    got: list[bytes] = []

    def serve() -> None:
        conn, _ = srv.accept()
        with conn:
            conn.sendall(greeting)
            data = b""
            while chunk := conn.recv(65536):
                data += chunk
            got.append(data)
            conn.sendall(reply(data))

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield srv.getsockname()[1], got
    finally:
        srv.close()


@contextlib.contextmanager
def _worker(secret: bytes):
    w = worker_mod.Worker(SID, secret, ECHO, drain_timeout=2.0)
    box: dict = {}

    async def run() -> int:
        box["loop"] = asyncio.get_running_loop()
        return await w.run("127.0.0.1", 0)

    thread = threading.Thread(target=lambda: box.setdefault("code", asyncio.run(run())))
    thread.start()
    wait_until(lambda: getattr(w, "port", None), describe=lambda: "worker listener")
    try:
        yield w.port, box
    finally:
        if thread.is_alive():
            box["loop"].call_soon_threadsafe(w._stopping.set)
        wait_until(lambda: not thread.is_alive(), describe=lambda: "worker exit")


def test_link_through_a_worker_attach_dial_delivers_and_finishes():
    # The relay's dial: a command whose stdio is the tunnel (``worker attach``).
    secret = wire.new_secret()
    attach = [sys.executable, "-m", "kiro_crew.remote_session.worker", "attach"]

    async def run(port: int) -> tuple[bytes, str]:
        # Wrap the dial so we can capture the attach subprocess and await its
        # exit before asyncio.run() closes the loop.  Without this, the
        # _UnixSubprocessTransport for the attach child remains unclosed when
        # the loop shuts down, so its __del__ fires with "Event loop is closed"
        # during the GC pass of a later test — blaming an innocent victim.
        attach_proc: list[asyncio.subprocess.Process] = []
        base_dial = client_mod.command_dial(attach + ["--port", str(port)])

        async def tracked_dial():
            reader, writer = await base_dial()
            if hasattr(writer, "_process"):
                attach_proc.append(writer._process)
            return reader, writer

        link = link_mod.RemoteLink(SID, secret, tracked_dial)
        link.start()
        try:
            await link.write(b"one\ntwo\n")
            link.close_input()
            return await asyncio.wait_for(link.stdout.read(), 30), link.state
        finally:
            await link.aclose()
            # Wait for the attach subprocess to exit so its asyncio transport
            # is properly closed before the event loop shuts down.
            for proc in attach_proc:
                if proc.returncode is None:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(proc.wait(), 30.0)

    with _worker(secret) as (port, box):
        got, state = asyncio.run(run(port))
    assert got == b"one\ntwo\n" and state == link_mod.FINISHED
    assert box["code"] == 0


def test_link_is_refused_by_a_peer_that_fails_authentication():
    async def run(port: int) -> link_mod.RemoteLink:
        async def dial():
            return await asyncio.open_connection("127.0.0.1", port)

        link = link_mod.RemoteLink(SID, b"\xab" * 32, dial)
        link.start()
        try:
            await async_wait_until(lambda: link.state in _ENDED, describe=lambda: link.state)
            return link
        finally:
            await link.aclose()

    with _peer(greeting=b"nope\n") as (port, _):
        link = asyncio.run(run(port))
    assert link.state == link_mod.REFUSED
    assert link.error.startswith("authentication failed")


@pytest.mark.parametrize(
    "argv",
    [[], ["--session-id", SID, "--secret-file", "/nonexistent", "--", "ssh"]],
)
def test_main_requires_a_workspace_relay(argv):
    with pytest.raises(SystemExit):
        client_mod.main(argv)


def test_main_pipe_proves_the_token_and_moves_bytes(tmp_path, monkeypatch):
    out = _stdio(monkeypatch, b"hello")
    token = _private(tmp_path, "t", "tok\n")
    with _peer(lambda d: d.split(b"\n", 1)[1].upper()) as (port, got):
        argv = ["--pipe", f"127.0.0.1:{port}", "--token-file", token]
        assert client_mod.main(argv) == client_mod.EXIT_FINISHED
    assert got == [b"tok\nhello"] and out.buffer.getvalue() == b"HELLO"
    assert os.path.exists(token)
    with pytest.raises(SystemExit):
        client_mod.main(["--pipe", "127.0.0.1:x", "--token-file", token])


def test_pipe_refusals(tmp_path):
    with pytest.raises(PermissionError):
        client_mod._read_token(_private(tmp_path, "t", "tok", 0o644))
    with pytest.raises(ValueError):
        asyncio.run(client_mod.pipe("10.0.0.1", 1, b"t", None, None))
    with socket.create_server(("127.0.0.1", 0)) as srv:
        port = srv.getsockname()[1]
    code = asyncio.run(client_mod.pipe("127.0.0.1", port, b"t", io.BytesIO().read, print))
    assert code == client_mod.EXIT_LOST


def test_command_dial_needs_a_command():
    with pytest.raises(ValueError):
        client_mod.command_dial([])


@pytest.mark.parametrize("own_stdio", [True, False])
def test_worker_attach_pipes_stdio_to_the_port(monkeypatch, own_stdio):
    with _peer(lambda d: d[::-1]) as (port, got):
        if own_stdio:
            sink = io.BytesIO()
            assert worker_mod.attach(port, stdin=io.BytesIO(b"abc"), stdout=sink) == 0
            result = sink.getvalue()
        else:
            out = _stdio(monkeypatch, b"abc")
            assert worker_mod.main(["attach", "--port", str(port)]) == 0
            result = out.buffer.getvalue()
    assert got == [b"abc"] and result == b"cba"


def test_worker_main_runs_the_child_and_publishes_its_port(tmp_path):
    key_file = _private(tmp_path, "s", wire.new_secret().hex())
    port_file = tmp_path / "port"
    argv = ["--session-id", SID, "--secret-file", key_file, "--listen", "127.0.0.1:0"]
    child = ["--", sys.executable, "-c", "raise SystemExit(3)"]
    assert worker_mod.main(argv + ["--port-file", str(port_file)] + child) == 3
    port, pid = port_file.read_text().split()
    assert int(port) > 0 and int(pid) == os.getpid()
    assert not os.path.exists(key_file)


@pytest.mark.parametrize("argv", [["--session-id", SID], ["--listen", "0.0.0.0:1", "--", "x"]])
def test_worker_main_refuses_bad_arguments(tmp_path, argv):
    key_file = _private(tmp_path, "s", wire.new_secret().hex())
    with pytest.raises(SystemExit):
        worker_mod.main(["--session-id", SID, "--secret-file", key_file, *argv])
