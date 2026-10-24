"""The Fargate guest bootstrap's protocol, exercised in this process.

``test_fargate_session`` drives the real bootstrap as a subprocess (POSIX only,
and invisible to coverage); these call its functions directly over socket
pairs, with the shell and the OS calls faked, so every platform checks the
enrolment, MAC, run, pipe, reaper and startup rules.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import socket
import subprocess
import threading
from types import SimpleNamespace

import pytest

from kiro_crew.remote_session import fargate_bootstrap as boot

SECRET = bytes(range(32))
#: Bounds every socket wait and join: only ever reached by a hang, which it
#: turns into a failure that names itself instead of a stalled suite.
HANG_SECS = 60.0


@pytest.fixture
def fresh(monkeypatch):
    """A bootstrap that has enrolled nothing, committed to ``SECRET``."""
    monkeypatch.setattr(boot, "_state", {"secret": None})
    monkeypatch.setattr(boot, "_running", set())
    monkeypatch.setattr(boot, "_activity", {"last": 0.0, "pipes": 0})
    monkeypatch.setattr(boot, "COMMIT", hashlib.sha256(SECRET).hexdigest())
    return boot


def _exchange(*lines: bytes) -> tuple[bytes, list[bytes]]:
    """Serve one connection whose client sent ``lines``; return (nonce, replies)."""
    server, client = socket.socketpair()
    try:
        for end in (server, client):
            end.settimeout(HANG_SECS)
        client.sendall(b"".join(lines))
        client.shutdown(socket.SHUT_WR)
        boot._serve(server)
        data = b""
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        client.close()
    hello, *replies = data.split(b"\n")
    assert hello.startswith(b"KCB1 ")
    return hello[5:], [reply for reply in replies if reply]


def _request(nonce: bytes, req: dict, secret: bytes = SECRET) -> bytes:
    body = base64.b64encode(json.dumps(req).encode())
    mac = hmac.new(secret, nonce + body, hashlib.sha256).hexdigest().encode()
    return b"REQ " + body + b" " + mac + b"\n"


def _signed_serve(req: dict, *, secret: bytes = SECRET, extra: bytes = b"") -> list[bytes]:
    """Serve one REQ; the nonce is read from the hello before the request is sent."""
    server, client = socket.socketpair()
    replies: list[bytes] = []
    for end in (server, client):
        end.settimeout(HANG_SECS)  # a lost reply fails the test, never hangs it
    worker = threading.Thread(target=boot._serve, args=(server,))
    worker.start()
    try:
        hello, rest = boot._line(client, bytearray())
        client.sendall(_request(hello[5:], req, secret) + extra)
        client.shutdown(socket.SHUT_WR)
        data = bytes(rest)
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
        replies = [line for line in data.split(b"\n") if line]
    finally:
        client.close()  # unblocks the server side on any early exit
        worker.join(HANG_SECS)
    assert not worker.is_alive()
    return replies


def test_lines_end_at_lf_or_cr_and_refuse_eof_and_overlong(monkeypatch):
    a, b = socket.socketpair()
    a.settimeout(HANG_SECS)
    try:
        b.sendall(b"one\r\ntwo\rthree\n")
        line, rest = boot._line(a, bytearray())
        assert line == b"one" and bytes(rest).startswith(b"two")
        line, rest = boot._line(a, rest)
        assert line == b"two"
        line, rest = boot._line(a, rest)
        assert line == b"three" and not rest
        monkeypatch.setattr(boot, "MAX_LINE", 4)
        b.sendall(b"abcdefgh")
        with pytest.raises(ValueError, match="too long"):
            boot._line(a, bytearray())
        b.close()
        with pytest.raises(EOFError):
            boot._line(a, bytearray())
    finally:
        a.close()


def test_enrolment_takes_only_the_committed_secret_and_only_once(fresh):
    _, replies = _exchange(b"ENROLL " + (b"ab" * 8) + b"\n")
    assert replies == [b"ERR short"]
    _, replies = _exchange(b"ENROLL " + bytes(range(1, 33)).hex().encode() + b"\n")
    assert replies == [b"ERR commit"] and fresh._state["secret"] is None
    nonce, replies = _exchange(b"ENROLL " + SECRET.hex().encode() + b"\n")
    expected = hmac.new(SECRET, nonce + b"enrolled", hashlib.sha256).hexdigest().encode()
    assert replies == [b"ENROLLED " + expected] and fresh._state["secret"] == SECRET
    assert fresh._activity["last"] > 0.0
    _, replies = _exchange(b"ENROLL " + SECRET.hex().encode() + b"\n")
    assert replies == [b"ERR enrolled"]


def test_no_commitment_means_nothing_can_enrol(fresh, monkeypatch):
    monkeypatch.setattr(boot, "COMMIT", "")
    _, replies = _exchange(b"ENROLL " + SECRET.hex().encode() + b"\n")
    assert replies == [b"ERR commit"] and fresh._state["secret"] is None


def test_requests_need_an_enrolled_secret_and_a_valid_mac(fresh):
    _, replies = _exchange(b"REQ e30= 00\n")
    assert replies == [b"ERR refused"]  # nothing enrolled yet
    fresh._state["secret"] = SECRET
    _, replies = _exchange(b"HELLO\n")
    assert replies == [b"ERR refused"]
    assert _signed_serve({"op": "run"}, secret=b"x" * 32) == [b"ERR mac"]
    assert fresh._activity["last"] == 0.0  # an unauthenticated line is not use
    assert _signed_serve({"op": "nope"}) == [b"ERR op"]
    assert fresh._activity["last"] > 0.0


class _FakeProcess:
    pid = 4242

    def __init__(self, argv, *, timeout_first: bool, **_kwargs):
        self.argv = argv
        self.returncode = None
        self._timeout_first = timeout_first
        self.killed = False
        self.inputs: list = []

    def communicate(self, data=None, timeout=None):
        self.inputs.append((data, timeout))
        assert boot._running == {self.pid}  # registered while it runs
        if self._timeout_first and len(self.inputs) == 1:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        self.returncode = -9 if self.killed else 0
        return b"x" * 10 + b"out", "é err".encode()

    def kill(self):
        self.killed = True


@pytest.mark.parametrize("timeout_first", [False, True])
def test_a_run_answers_signed_output_and_a_timeout_kills_it(fresh, monkeypatch, timeout_first):
    fresh._state["secret"] = SECRET
    made: list[_FakeProcess] = []

    def popen(argv, **kwargs):
        made.append(_FakeProcess(argv, timeout_first=timeout_first, **kwargs))
        return made[-1]

    fake_subprocess = SimpleNamespace(
        Popen=popen, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired
    )
    monkeypatch.setattr(boot, "subprocess", fake_subprocess)
    monkeypatch.setattr(boot, "OUT_MAX", 3)
    stdin = base64.b64encode(b"input").decode()
    replies = _signed_serve({"op": "run", "command": "true", "stdin": stdin})
    assert len(replies) == 1 and replies[0].startswith(b"RES ")
    _, body, _mac = replies[0].split(b" ")
    answer = json.loads(base64.b64decode(body))
    assert base64.b64decode(answer["stdout"]) == b"out"  # the tail, capped at OUT_MAX
    assert answer["stderr"] == "é err"
    assert answer["code"] == (-9 if timeout_first else 0)
    process = made[0]
    assert process.argv == ["/bin/sh", "-c", "true"]
    assert process.inputs[0] == (b"input", boot.RUN_TIMEOUT)
    assert process.killed is timeout_first
    assert boot._running == set()  # released once it ends


def test_a_pipe_refuses_its_own_port_and_splices_to_the_worker(fresh, monkeypatch):
    fresh._state["secret"] = SECRET
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.settimeout(HANG_SECS)  # a missed connect fails the test, never hangs it
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    # The bootstrap's own port is any port but the worker's, so the refusal
    # below can never be the reason the worker is not reached.
    own = port + 1 if port < 65535 else port - 1
    monkeypatch.setattr(boot, "PORT", own)
    received: list[bytes] = []
    pipes_while_open: list[int] = []

    def worker():
        try:
            conn, _ = listener.accept()
        except OSError:
            return  # the test failed before connecting; its assertions say why
        with conn:
            conn.settimeout(HANG_SECS)
            data = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            received.append(data)
            pipes_while_open.append(boot._activity["pipes"])
            conn.sendall(b"pong\n")

    echo = threading.Thread(target=worker)
    echo.start()
    try:
        assert _signed_serve({"op": "pipe", "port": own}) == [b"ERR port"]
        assert _signed_serve({"op": "pipe", "port": 0}) == [b"ERR port"]
        replies = _signed_serve({"op": "pipe", "port": port}, extra=b"ping\n")
    finally:
        listener.close()
        echo.join(HANG_SECS)
    assert not echo.is_alive()
    assert replies[0].startswith(b"OK ") and replies[1:] == [b"pong"]
    assert received == [b"ping\n"]  # bytes sent after the request reach the worker
    assert pipes_while_open == [1] and boot._activity["pipes"] == 0


def test_a_pipe_to_a_closed_port_ends_quietly(fresh):
    fresh._state["secret"] = SECRET
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens there now
    assert _signed_serve({"op": "pipe", "port": port}) == []
    assert boot._activity["pipes"] == 0


class _Stop(Exception):
    pass


def test_the_reaper_collects_orphans_and_never_a_running_command(fresh, monkeypatch):
    fresh._running.add(22)
    script = [
        SimpleNamespace(si_pid=11),
        SimpleNamespace(si_pid=22),
        ChildProcessError(),
        OSError("interrupted"),
        None,
    ]
    reaped: list[int] = []
    sleeps = []

    def waitid(*_args):
        item = script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def sleep(_seconds):
        sleeps.append(_seconds)
        if not script:
            raise _Stop  # every scripted answer was consumed

    fake_os = SimpleNamespace(
        waitid=waitid,
        waitpid=lambda pid, _flags: reaped.append(pid),
        P_ALL=0,
        WEXITED=4,
        WNOHANG=1,
        WNOWAIT=0x01000000,
    )
    monkeypatch.setattr(boot, "os", fake_os)
    monkeypatch.setattr(boot, "time", SimpleNamespace(sleep=sleep, monotonic=lambda: 0.0))
    with pytest.raises(_Stop):
        boot._reap()
    assert reaped == [11]  # 22 is a "run" child: left for its own communicate()
    assert not script


def test_main_listens_on_loopback_and_hands_each_connection_to_a_thread(
    fresh, monkeypatch, tmp_path
):
    started: list = []
    bound: list = []
    accepted = [(object(), ("127.0.0.1", 5555))]

    class FakeThread:
        def __init__(self, target, args=(), daemon=False):
            assert daemon
            self.call = (target, args)

        def start(self):
            started.append(self.call)

    class FakeServer:
        def setsockopt(self, *args):
            pass

        def bind(self, address):
            bound.append(address)

        def getsockname(self):
            return ("127.0.0.1", 47821)

        def listen(self, backlog):
            assert backlog > 0

        def accept(self):
            if not accepted:
                raise _Stop
            return accepted.pop()

    fake_socket = SimpleNamespace(
        socket=lambda *_args: FakeServer(),
        AF_INET=socket.AF_INET,
        SOCK_STREAM=socket.SOCK_STREAM,
        SOL_SOCKET=socket.SOL_SOCKET,
        SO_REUSEADDR=socket.SO_REUSEADDR,
    )
    workdir = tmp_path / "work"
    monkeypatch.setattr(boot, "socket", fake_socket)
    monkeypatch.setattr(boot, "threading", SimpleNamespace(Thread=FakeThread))
    monkeypatch.setattr(boot.os, "getpid", lambda: 1)
    monkeypatch.setattr(boot.os, "waitid", lambda *_a: None, raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("KIROCREW_WORKDIR", str(workdir))
    with pytest.raises(_Stop):
        boot.main()
    targets = [target for target, _args in started]
    assert targets == [boot._reap, boot._idle_watch, boot._serve]  # PID 1 reaps
    assert started[1][1] == (boot.IDLE_SECS,)
    assert bound == [("127.0.0.1", boot.PORT)]
    assert workdir.is_dir() and boot.os.environ["HOME"]
