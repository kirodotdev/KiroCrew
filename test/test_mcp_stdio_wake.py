"""A finished tool is answered at once, not at the read loop's next timeout.

The stdio loop runs each ``tools/call`` on a worker thread. The worker writes to
a wake pipe the loop selects on, so the answer does not wait for the loop's
``select`` timeout. These run a real server process in which every timed
``select`` of the loop waits an hour: an answer that arrives at all came
through the wake pipe.
Tools start and finish on file handshakes the test drives, never on sleeps.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from kiro_crew import mcp_shared, platform_compat
from kiro_crew.testing.wait import wait_until

SRC = str(Path(mcp_shared.__file__).resolve().parents[1])

SERVER = textwrap.dedent("""
    import os, select, sys, time
    from kiro_crew import mcp_shared

    class _HourLongTimeouts:
        # Every timed wait of the read loop becomes an hour: nothing but the
        # wake pipe can end it in time, whatever the loop's own timeout is.
        def __getattr__(self, name):
            return getattr(select, name)

        @staticmethod
        def select(r, w, x, timeout=None):
            return select.select(r, w, x, None if timeout is None else 3600.0)

    mcp_shared.select = _HourLongTimeouts()
    signals = sys.argv[1]

    def list_tools():
        return [{"name": "echo", "description": "e", "inputSchema": {"type": "object"}}]

    def call_tool(name, args):
        tag = str(args.get("n", ""))
        if args.get("block"):
            open(os.path.join(signals, tag + ".started"), "w").close()
            release = os.path.join(signals, tag + ".release")
            while not os.path.exists(release):
                time.sleep(0.005)
        return "ok " + tag

    mcp_shared.run_mcp_stdio_loop("wake-test", "1.0", list_tools, call_tool)
    """)

pytestmark = pytest.mark.skipif(not platform_compat.IS_POSIX, reason="worker branch is POSIX-only")

#: Bound on any one answer. Far below the hour-long backstop, far above any
#: scheduling delay: it separates "woken" from "waited out", not fast from slow.
_ANSWER_DEADLINE = 30.0


class _Server:
    def __init__(self, tmp_path: Path) -> None:
        self.signals = tmp_path / "signals"
        self.signals.mkdir()
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "KIROCREW_HOME": str(tmp_path / "home"),
            "PYTHONPATH": SRC,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.p = subprocess.Popen(
            [sys.executable, "-c", SERVER, str(self.signals)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=str(tmp_path),
        )
        self.rid = 0
        self._lines: queue.Queue[bytes] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.p.stdout is not None
        for line in self.p.stdout:
            self._lines.put(line)
        self._lines.put(b"")

    def send(self, method: str, params: dict | None = None, *, notify: bool = False) -> int:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if not notify:
            self.rid += 1
            msg["id"] = self.rid
        if params is not None:
            msg["params"] = params
        assert self.p.stdin is not None
        self.p.stdin.write((json.dumps(msg) + "\n").encode())
        self.p.stdin.flush()
        return self.rid

    def call(self, n: str, *, block: bool = False) -> int:
        return self.send("tools/call", {"name": "echo", "arguments": {"n": n, "block": block}})

    def read(self) -> dict:
        line = self._lines.get(timeout=_ANSWER_DEADLINE)
        assert line, "server closed stdout"
        return json.loads(line)

    def started(self, n: str) -> None:
        wait_until(lambda: (self.signals / f"{n}.started").exists(), timeout=_ANSWER_DEADLINE)

    def release(self, n: str) -> None:
        (self.signals / f"{n}.release").touch()

    def close(self) -> None:
        assert self.p.stdin is not None
        try:
            self.p.stdin.close()
            self.p.wait(timeout=30)
        finally:
            if self.p.poll() is None:
                self.p.kill()
                self.p.wait(timeout=30)


@pytest.fixture
def server(tmp_path: Path):
    s = _Server(tmp_path)
    try:
        s.send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        assert "result" in s.read()
        yield s
    finally:
        s.close()


def test_a_tool_finishing_mid_wait_is_answered_by_the_wake(server: _Server) -> None:
    for i in range(3):
        tag = f"w{i}"
        rid = server.call(tag, block=True)
        # The worker is running and the loop is in its select: only the wake
        # pipe can end that wait before the hour-long backstop.
        server.started(tag)
        server.release(tag)
        r = server.read()
        assert r["id"] == rid and r["result"]["content"][0]["text"] == f"ok {tag}"


def test_ping_is_answered_while_a_tool_runs(server: _Server) -> None:
    call = server.call("slow", block=True)
    server.started("slow")
    ping = server.send("ping")
    first = server.read()
    assert first["id"] == ping, first
    server.release("slow")
    second = server.read()
    assert second["id"] == call
    assert second["result"]["content"][0]["text"] == "ok slow"


def test_queued_calls_keep_order_and_each_is_answered(server: _Server) -> None:
    head = server.call("head", block=True)
    server.started("head")
    ids = [server.call(str(i)) for i in range(5)]  # queued behind the running call
    server.release("head")
    got = [server.read() for _ in range(6)]
    assert [r["id"] for r in got] == [head, *ids]
    assert [r["result"]["content"][0]["text"] for r in got[1:]] == [f"ok {i}" for i in range(5)]


def test_cancelled_call_gets_no_answer(server: _Server) -> None:
    call = server.call("gone", block=True)
    server.started("gone")
    server.send("notifications/cancelled", {"requestId": call}, notify=True)
    ping = server.send("ping")
    assert server.read()["id"] == ping
    server.release("gone")
    # Queued behind the cancelled call, so it is answered only after that
    # worker finished: anything the cancelled call emitted would come first.
    after = server.call("after")
    r = server.read()
    assert r["id"] == after, r


def test_wake_pipe_is_closed_when_the_loop_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[mcp_shared._WakePipe] = []
    real_init = mcp_shared._WakePipe.__init__

    def spy(self: mcp_shared._WakePipe) -> None:
        real_init(self)
        made.append(self)

    monkeypatch.setattr(mcp_shared._WakePipe, "__init__", spy)
    r, w = os.pipe()
    with os.fdopen(r, "r") as stdin:
        os.close(w)  # EOF at once: the loop ends without a request
        monkeypatch.setattr(sys, "stdin", stdin)
        assert mcp_shared._run_stdio_dispatch_loop("x", "1", list, lambda n, a: "") is False
    (pipe,) = made
    assert pipe.read_fd is None


def test_wake_after_close_never_writes_a_reused_descriptor() -> None:
    """A worker still running after the loop exits must not write into the
    number the closed pipe freed, which the process may have reused."""
    pipe = mcp_shared._WakePipe()
    assert pipe.read_fd is not None
    fds = (pipe.read_fd, pipe._write_fd)
    pipe.close()
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    r, w = os.pipe()  # takes the freed numbers back
    try:
        pipe.wake()
        os.set_blocking(r, False)
        with pytest.raises(BlockingIOError):
            os.read(r, 1)
    finally:
        os.close(r)
        os.close(w)


def test_drain_empties_the_pipe() -> None:
    pipe = mcp_shared._WakePipe()
    try:
        assert pipe.read_fd is not None
        for _ in range(3):
            pipe.wake()
        pipe.drain()
        with pytest.raises(BlockingIOError):
            os.read(pipe.read_fd, 1)
    finally:
        pipe.close()
    pipe.drain()  # after close: a no-op, never an error
