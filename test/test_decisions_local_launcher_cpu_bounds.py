"""The Plumb and Laya launchers bound their CPU use the way the Strands one does.

Each launcher caps torch at half the CPUs it may run on (at most 16, one
inter-op thread) before the model loads, and admits one decision at a time,
answering 503 to one that cannot start within a short wait instead of queueing
it behind an inference its caller has already given up on. The launchers run in
the model's own environment and import torch and the model library at module
level, so these tests read their source and load only the definitions they
need, then drive Plumb's handler over a real loopback HTTP server and Laya's
wrapper as plain ASGI.
"""

from __future__ import annotations

import ast
import asyncio
import http.client
import io
import json
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.testing.wait import async_wait_until, await_until

_SERVERS = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "decisions" / "local_servers"
_PLUMB = _SERVERS / "plumb_cpu.py"
_LAYA = _SERVERS / "laya_cpu.py"
_STRANDS = _SERVERS / "strands_decider_cpu.py"
_DECIDE = "/v1/systemone"
#: Bounds a step that must finish; every ordering below is driven by events.
_BOUND_SECS = 30.0


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _load(path: Path, names: set[str], namespace: dict) -> dict:
    defs: list[ast.stmt] = [
        node for node in _tree(path).body if isinstance(node, _DEFS) and node.name in names
    ]
    assert {d.name for d in defs if isinstance(d, _DEFS)} == names
    code = compile(ast.Module(body=defs, type_ignores=[]), str(path), "exec", dont_inherit=True)
    # Executes definitions parsed from a tracked repository file, no external input.
    # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
    exec(code, namespace)  # noqa: S102
    return namespace


# --- thread cap ------------------------------------------------------------


def _first_model_import(tree: ast.Module) -> int:
    heavy = {"torch", "jevk5", "laya", "strands_decider", "transformers"}
    for i, node in enumerate(tree.body):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] in heavy for a in node.names):
            return i
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in heavy:
            return i
    raise AssertionError("launcher imports no model library")


def _statement_index(tree: ast.Module, needle: str) -> int:
    for i, node in enumerate(tree.body):
        if needle in ast.unparse(node):
            return i
    raise AssertionError(f"launcher has no module-level statement containing {needle!r}")


@pytest.mark.parametrize("launcher", [_PLUMB, _LAYA, _STRANDS], ids=lambda p: p.stem)
def test_threads_are_capped_before_torch_loads(launcher: Path) -> None:
    tree = _tree(launcher)
    first_import = _first_model_import(tree)

    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        assert _statement_index(tree, f"os.environ.setdefault('{var}', str(TORCH_THREADS))") < (
            first_import
        )
    set_threads = _statement_index(tree, "torch.set_num_threads(TORCH_THREADS)")
    interop = _statement_index(tree, "torch.set_num_interop_threads(1)")
    # Applied once torch is imported and before the model is built or served.
    assert first_import < set_threads and first_import < interop
    assert set_threads < _statement_index(tree, "args = parser.parse_args()")


@pytest.mark.parametrize("launcher", [_PLUMB, _LAYA], ids=lambda p: p.stem)
@pytest.mark.parametrize(
    ("affinity", "cpu_count", "expected"),
    [(12, 64, 6), (64, None, 16), (1, 8, 1), (None, 10, 5), (None, None, 1)],
)
def test_half_the_usable_cpus_at_most_sixteen(
    launcher: Path, affinity: int | None, cpu_count: int | None, expected: int
) -> None:
    fake_os: Any = SimpleNamespace(cpu_count=lambda: cpu_count)
    if affinity is not None:
        fake_os.sched_getaffinity = lambda _pid: set(range(affinity))
    threads = _load(launcher, {"_torch_threads"}, {"os": fake_os})["_torch_threads"]

    assert threads() == expected


# --- Plumb: one decision at a time over HTTP -------------------------------


class _Inference:
    """Counts decisions in flight; a decision holds until ``release`` is set."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.now = 0
        self.peak = 0
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()


def _jevk5_shaped_handler(inference: _Inference) -> type:
    # The shape of jevk5.server.make_handler's handler at the pinned commit: its
    # own do_GET/do_POST, the decision route reading the body by Content-Length.
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._send(200, {"ok": True})

        def do_POST(self) -> None:
            if self.path.rstrip("/") != _DECIDE:
                self._send(404, {"error": "not found"})
                return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            with inference.lock:
                inference.now += 1
                inference.calls += 1
                inference.peak = max(inference.peak, inference.now)
            inference.entered.set()
            try:
                if body.get("hold"):
                    assert inference.release.wait(_BOUND_SECS), "decision was never released"
            finally:
                with inference.lock:
                    inference.now -= 1
            self._send(200, {"answers": {}})

        def log_message(self, *args: object) -> None:
            pass

    return Handler


class _WatchedLock:
    """A real lock that also signals when a caller starts waiting on it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.waiting = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._lock.acquire(blocking=False):
            return True
        self.waiting.set()
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()


class _SteppedClock:
    """A ``time`` stand-in whose ``monotonic`` the test moves forward by hand.

    ``started`` is set on the first ``monotonic`` call, which is the handler
    setting its body deadline, so a test can act after that point instead of
    guessing it from sleeps.
    """

    def __init__(self) -> None:
        self.started = threading.Event()
        self._offset = 0.0
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            now = time.monotonic() + self._offset
        self.started.set()
        return now

    def advance(self, secs: float) -> None:
        with self._lock:
            self._offset += secs

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


def _plumb_gate(
    handler: type,
    wait_secs: float,
    body_wait: float = _BOUND_SECS,
    clock: Any = time,
) -> tuple[type, _WatchedLock]:
    watched = _WatchedLock()
    namespace = {
        "io": io,
        "time": clock,
        "threading": SimpleNamespace(Lock=lambda: watched),
        "DECIDE_PATH": _DECIDE,
        "_BODY_MAX_BYTES": 1 << 20,
        "BODY_WAIT_SECS": body_wait,
    }
    gate = _load(_PLUMB, {"_one_at_a_time"}, namespace)["_one_at_a_time"]
    return gate(handler, wait_secs), watched


class _LoopbackServer(ThreadingHTTPServer):
    # As in plumb_cpu.py: skip the host lookup http.server makes on bind.
    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class _Server:
    def __init__(self, handler: type) -> None:
        self.httpd = _LoopbackServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> _Server:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=_BOUND_SECS)
        try:
            data = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"{}")
        finally:
            conn.close()


def _hold_one(server: _Server, inference: _Inference) -> tuple[threading.Thread, list]:
    outcome: list = []
    held = threading.Thread(
        target=lambda: outcome.append(server.request("POST", _DECIDE, {"hold": True})),
        daemon=True,
    )
    held.start()
    assert inference.entered.wait(_BOUND_SECS)
    return held, outcome


def test_plumb_refuses_a_decision_that_cannot_start_in_time_with_503() -> None:
    inference = _Inference()
    handler, _ = _plumb_gate(_jevk5_shaped_handler(inference), 0.05)
    with _Server(handler) as server:
        held, outcome = _hold_one(server, inference)

        refused = [server.request("POST", _DECIDE, {"state": "x"}) for _ in range(2)]
        inference.release.set()
        held.join(_BOUND_SECS)

    assert [status for status, _ in refused] == [503, 503]
    assert refused[0][1] == {"error": "busy: another decision is running"}
    assert outcome == [(200, {"answers": {}})]
    # The refused decisions never reached the model.
    assert inference.calls == 1 and inference.peak == 1


def test_plumb_admits_a_decision_that_waited_once_the_first_finishes() -> None:
    inference = _Inference()
    handler, gate = _plumb_gate(_jevk5_shaped_handler(inference), _BOUND_SECS)
    with _Server(handler) as server:
        held, outcome = _hold_one(server, inference)
        second: list = []
        waiter = threading.Thread(
            target=lambda: second.append(server.request("POST", _DECIDE, {})), daemon=True
        )
        waiter.start()
        # The second decision is blocked on the held gate before the first ends.
        assert gate.waiting.wait(_BOUND_SECS)
        assert inference.calls == 1
        inference.release.set()
        held.join(_BOUND_SECS)
        waiter.join(_BOUND_SECS)

    assert outcome[0][0] == 200 and second[0][0] == 200
    assert inference.calls == 2 and inference.peak == 1


def test_plumb_gate_is_released_when_a_decision_fails() -> None:
    inference = _Inference()
    handler, _ = _plumb_gate(_jevk5_shaped_handler(inference), 0.05)
    with _Server(handler) as server:
        # A body the handler cannot parse raises inside the gated call.
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=_BOUND_SECS)
        conn.request("POST", _DECIDE, body=b"not json")
        try:
            conn.getresponse().read()
        except (http.client.HTTPException, OSError):
            pass
        conn.close()

        assert server.request("POST", _DECIDE, {})[0] == 200


def _partial_post(port: int, length: int = 100, start: bytes = b'{"state": ') -> socket.socket:
    # Headers promise a body that never fully arrives; the connection stays open.
    sock = socket.create_connection(("127.0.0.1", port), timeout=_BOUND_SECS)
    sock.sendall(f"POST {_DECIDE} HTTP/1.0\r\nContent-Length: {length}\r\n\r\n".encode() + start)
    return sock


def _closed_by_server(sock: socket.socket) -> bytes:
    """What the server sent before closing (b"" for nothing)."""
    received = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return received
            received += chunk
    except ConnectionError:
        # A close with data unread reads as a reset on Linux and an abort
        # (WinError 10053) on Windows; either way the server sent nothing more.
        return received
    finally:
        sock.close()


def test_plumb_a_stalled_body_is_dropped_without_taking_the_gate() -> None:
    inference = _Inference()
    handler, gate = _plumb_gate(_jevk5_shaped_handler(inference), 0.05, body_wait=0.5)
    with _Server(handler) as server:
        stalled = _partial_post(server.port)

        # The stalled request is still reading its body, so a decision runs now.
        assert server.request("POST", _DECIDE, {})[0] == 200
        assert _closed_by_server(stalled) == b""

    assert not gate.waiting.is_set() and inference.calls == 1


def test_plumb_a_trickled_body_is_cut_off_at_its_deadline() -> None:
    inference = _Inference()
    handler, _ = _plumb_gate(_jevk5_shaped_handler(inference), 0.05, body_wait=0.3)
    with _Server(handler) as server:
        trickle = _partial_post(server.port, length=10_000)
        stop = threading.Event()

        def feed() -> None:
            # One byte at a time, each well inside the per-read timeout.
            while not stop.wait(0.02):
                try:
                    trickle.sendall(b" ")
                except OSError:
                    return

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        try:
            assert _closed_by_server(trickle) == b""
        finally:
            stop.set()
            feeder.join(_BOUND_SECS)

    assert inference.calls == 0


def test_plumb_a_body_finished_after_its_deadline_is_dropped() -> None:
    inference = _Inference()
    clock = _SteppedClock()
    handler, _ = _plumb_gate(
        _jevk5_shaped_handler(inference), 0.05, body_wait=_BOUND_SECS, clock=clock
    )
    with _Server(handler) as server:
        late = _partial_post(server.port, length=14)
        late.sendall(b'"x"')
        # Once the handler has set its deadline, move its clock past it; the
        # last byte then arrives well inside any real socket timeout, so only
        # the body's one deadline can refuse it.
        assert clock.started.wait(_BOUND_SECS)
        clock.advance(_BOUND_SECS + 1.0)
        try:
            late.sendall(b"}")
        except OSError:
            pass
        assert _closed_by_server(late) == b""

    assert inference.calls == 0


def test_plumb_refuses_an_oversized_body_without_reading_it() -> None:
    inference = _Inference()
    handler, _ = _plumb_gate(_jevk5_shaped_handler(inference), 0.05)
    with _Server(handler) as server:
        # Headers only: no body bytes are left unread to turn the reply into a reset.
        late = _partial_post(server.port, length=(1 << 20) + 1, start=b"")
        reply = _closed_by_server(late)

    assert reply.startswith(b"HTTP/1.0 413") and inference.calls == 0


def test_plumb_other_routes_bypass_the_gate() -> None:
    inference = _Inference()
    handler, _ = _plumb_gate(_jevk5_shaped_handler(inference), 0.05)
    with _Server(handler) as server:
        held, _ = _hold_one(server, inference)

        health = server.request("GET", "/health")
        other = server.request("POST", "/elsewhere", {})
        inference.release.set()
        held.join(_BOUND_SECS)

    assert health[0] == 200 and other[0] == 404


def test_plumb_launcher_serves_through_the_gate() -> None:
    tree = _tree(_PLUMB)
    handler = _statement_index(tree, "Handler = _one_at_a_time(make_handler(")
    assert "QUEUE_WAIT_SECS" in ast.unparse(tree.body[handler])
    assert handler < _statement_index(tree, "class AttestedHandler(Handler)")


# --- Laya: one decision at a time as ASGI ----------------------------------


def _LayaGate(  # noqa: N802 -- stands in for the class
    inner: Any, wait_secs: float, body_wait: float = _BOUND_SECS, slots: int = 4
) -> Any:
    namespace = {
        "Any": Any,
        "asyncio": asyncio,
        "BODY_WAIT_SECS": body_wait,
        "_BODY_AHEAD_MAX_BYTES": 1 << 20,
        "_READ_AHEAD_SLOTS": slots,
    }
    loaded = _load(_LAYA, {"_OneAtATime", "_read_ahead", "_reply"}, namespace)
    return loaded["_OneAtATime"](inner, wait_secs)


class _AsgiInference:
    def __init__(self) -> None:
        self.now = 0
        self.peak = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        self.now += 1
        self.peak = max(self.peak, self.now)
        self.entered.set()
        try:
            if scope["path"] == _DECIDE:
                await await_until(self.release, what="release signal")
            await send({"type": "http.response.start", "status": 200, "headers": []})
        finally:
            self.now -= 1


async def _body(chunks: tuple = (b"{}",), ended: bool = True) -> Any:
    pending = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1 or not ended}
        for i, c in enumerate(chunks)
    ]

    async def receive():  # type: ignore[no-untyped-def]
        if pending:
            return pending.pop(0)
        await asyncio.Event().wait()  # a client that never sends again

    return receive


async def _call(gate: Any, path: str, receive: Any = None) -> int:
    sent: list = []

    async def send(message):  # type: ignore[no-untyped-def]
        sent.append(message)

    if receive is None:
        receive = await _body()
    await gate({"type": "http", "path": path, "headers": []}, receive, send)
    return sent[0]["status"]


def test_laya_refuses_a_decision_that_cannot_start_in_time_with_503() -> None:
    async def main() -> tuple:
        app = _AsgiInference()
        gate = _LayaGate(app, 0.01)
        first = asyncio.create_task(_call(gate, _DECIDE))
        await app.entered.wait()
        refused = await asyncio.gather(_call(gate, _DECIDE), _call(gate, _DECIDE))
        health = await _call(gate, "/health")
        app.release.set()
        return await first, list(refused), health, app.peak

    first, refused, health, peak = asyncio.run(main())

    assert (first, refused, health) == (200, [503, 503], 200)
    assert peak == 2  # the decision and the ungated health check


def test_laya_admits_a_decision_that_waited_and_releases_on_failure() -> None:
    async def main() -> tuple:
        app = _AsgiInference()
        gate = _LayaGate(app, _BOUND_SECS)
        first = asyncio.create_task(_call(gate, _DECIDE))
        await app.entered.wait()
        second = asyncio.create_task(_call(gate, _DECIDE))
        # The second decision is queued on the held gate, not running.
        await async_wait_until(lambda: bool(gate.lock._waiters))
        running_while_held = app.now
        app.release.set()
        admitted = list(await asyncio.gather(first, second))

        async def fails(scope, receive, send):  # type: ignore[no-untyped-def]
            raise RuntimeError("inference failed")

        failing = _LayaGate(fails, 0.01)
        with pytest.raises(RuntimeError):
            await _call(failing, _DECIDE)
        failing.inner = app
        after_failure = await _call(failing, _DECIDE)
        return admitted, running_while_held, app.peak, after_failure

    admitted, running_while_held, peak, after_failure = asyncio.run(main())

    assert admitted == [200, 200]
    assert running_while_held == 1 and peak == 1
    assert after_failure == 200


def test_laya_reads_the_body_before_the_gate_and_replays_it() -> None:
    async def main() -> tuple:
        seen: list = []

        async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
            while True:
                message = await receive()
                seen.append(message["body"])
                if not message["more_body"]:
                    break
            await send({"type": "http.response.start", "status": 200, "headers": []})

        gate = _LayaGate(inner, 0.01, body_wait=0.05)
        replayed = await _call(gate, _DECIDE, await _body((b'{"a"', b": 1}")))
        # A body that never finishes is answered 408 and never takes the gate.
        stalled = await _call(gate, _DECIDE, await _body((b"{",), ended=False))
        after = await _call(gate, _DECIDE)
        return replayed, seen, stalled, after, gate.lock.locked()

    replayed, seen, stalled, after, locked = asyncio.run(main())

    assert replayed == 200 and seen[:2] == [b'{"a"', b": 1}"]
    assert stalled == 408 and after == 200 and not locked


def test_laya_bounds_buffered_bodies_and_refuses_bad_ones_before_the_gate() -> None:
    async def main() -> tuple:
        app = _AsgiInference()
        gate = _LayaGate(app, _BOUND_SECS, slots=1)
        first = asyncio.create_task(_call(gate, _DECIDE))
        await app.entered.wait()
        # The one read-ahead slot is in use, so another decision is refused at once.
        no_slot = await _call(gate, _DECIDE)
        app.release.set()
        await first

        open_gate = _LayaGate(app, 0.01)
        too_large = await _call(open_gate, _DECIDE, await _body((b"x" * ((1 << 20) + 1),)))
        sent: list = []

        async def send(message):  # type: ignore[no-untyped-def]
            sent.append(message)

        async def gone():  # type: ignore[no-untyped-def]
            return {"type": "http.disconnect"}

        await open_gate({"type": "http", "path": _DECIDE, "headers": []}, gone, send)
        return no_slot, too_large, sent, app.peak, open_gate.lock.locked()

    no_slot, too_large, sent, peak, locked = asyncio.run(main())

    assert no_slot == 503 and too_large == 413
    # A client that left mid-body gets no reply and never reaches the model.
    assert sent == [] and peak == 1 and not locked


def test_laya_launcher_serves_through_the_gate() -> None:
    tree = _tree(_LAYA)
    run = _statement_index(tree, "uvicorn.run(")
    assert "_OneAtATime(app, QUEUE_WAIT_SECS)" in ast.unparse(tree.body[run])


def test_lifespan_and_other_scopes_pass_through_laya_gate() -> None:
    seen: list = []

    async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
        seen.append(scope["type"])

    asyncio.run(_LayaGate(inner, 0.01)({"type": "lifespan"}, None, None))

    assert seen == ["lifespan"]


def test_module_constants_match_the_strands_launcher() -> None:
    for launcher in (_PLUMB, _LAYA):
        assert "QUEUE_WAIT_SECS = 1.0" in launcher.read_text(encoding="utf-8")
    # Laya's read-ahead refuses only what Laya itself would (its MAX_BODY_BYTES).
    assert "_BODY_AHEAD_MAX_BYTES = 2 * 1024 * 1024" in _LAYA.read_text(encoding="utf-8")
