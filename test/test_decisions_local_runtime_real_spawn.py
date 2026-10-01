"""The runtime's process code against a real child, on whichever OS runs the suite.

``test_decisions_local_runtime.py`` fakes the spawn, health and stop seams; these
tests drive the real ones -- the spawn below the sandbox wrapper, the attest probe, the port probe,
the stdin tether and the terminate fallback -- with a stand-in server that keeps
the launcher protocol and needs nothing but the standard library. CI runs the
backend suite on Linux, macOS and Windows, so each platform branch in
``local_runtime`` is exercised where it applies. Weights and the uv environment are
out of scope here: the stand-in replaces the model, and ``sys.executable`` the env.
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import textwrap
import time
from dataclasses import replace
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.decisions import local_models
from kiro_crew.decisions import local_runtime as lr

pytestmark = pytest.mark.timeout(120)

#: The stand-in server: the launcher contract of ``local_servers/*.py`` -- bind
#: 127.0.0.1 on ``--port``, answer ``/kirocrew-attest`` with the secret, exit at
#: end of stdin -- and nothing else.
_STUB = textwrap.dedent("""
    import argparse, os, sys, threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    ATTEST = os.environ.pop("KIROCREW_LOCAL_ATTEST", "")

    def _exit_when_gateway_lets_go():
        sys.stdin.buffer.read()
        os._exit(0)

    threading.Thread(target=_exit_when_gateway_lets_go, daemon=True).start()
    p = argparse.ArgumentParser()
    p.add_argument("--weights")
    p.add_argument("--port", type=int)
    args = p.parse_args()
    print(f"pid={os.getpid()}", flush=True)

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = ATTEST.encode() if self.path == "/kirocrew-attest" else b""
            self.send_response(200 if body else 404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()
    """)

_WEIGHT = b"stand-in"


def _free_port() -> int:
    """A port the OS hands out: the preset's default band is shared by every worker."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _model() -> local_models.LocalModel:
    base = local_models.get("laya")
    assert base is not None
    files = (local_models.ModelFile("w.bin", hashlib.sha256(_WEIGHT).hexdigest(), len(_WEIGHT)),)
    return replace(base, files=files, launcher="stub_server.py")


def make_runtime(tmp_path: Path, setattr) -> lr.LocalModelRuntime:
    """A runtime with the real spawn, health and stop seams; *setattr* patches modules."""
    servers = tmp_path / "servers"
    servers.mkdir()
    (servers / "stub_server.py").write_text(_STUB, encoding="utf-8")
    (servers / _model().requirements).write_text("", encoding="utf-8")
    setattr(lr, "SERVERS_DIR", servers)
    setattr(lr, "STOP_GRACE_SECS", 5)
    # The OS sandbox wrapper is shared infrastructure with its own per-platform suite;
    # everything after it -- the Popen flags, the stdin pipe, the log -- is this module's.
    setattr(lr, "sandboxed_spawn_argv", lambda argv, **_: (list(argv), dict(os.environ), None))

    def download(path, url, *, sha256, size, resume, on_progress, label):
        path.write_bytes(_WEIGHT)
        on_progress(size, size)
        return True, ""

    def run_command(argv, timeout, own_dir, stop=None):
        # uv's two steps, as far as the runtime can observe them: ``venv`` makes the
        # environment directory, ``pip install`` from an empty lock adds nothing.
        if argv[1] == "venv":
            Path(argv[-1]).mkdir(parents=True, exist_ok=True)
        return 0, ""

    rt = lr.LocalModelRuntime(tmp_path / "models", download=download, run_command=run_command)
    # The interpreter running the suite stands in for the preset's uv environment.
    rt._env_python = lambda m: Path(sys.executable)  # type: ignore[method-assign]
    import kiro_crew.env

    setattr(kiro_crew.env, "resolve_uv", lambda: sys.executable)
    return rt


def server_pid(rt: lr.LocalModelRuntime) -> int:
    """The stand-in's own pid, which it logs; the Popen pid may be a sandbox wrapper."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        log = rt.log_path(_model())
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        for line in text.splitlines():
            if line.startswith("pid="):
                return int(line[4:])
        time.sleep(0.05)
    raise AssertionError("the stand-in never logged its pid")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path, monkeypatch.setattr)
    yield rt
    rt.deactivate(wait=True)


def _wait(rt, *states, timeout=30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = rt.status()
        if s["state"] in states:
            return s
        time.sleep(0.05)
    raise AssertionError(f"runtime stayed {rt.status()}; log: {rt._log_tail(_model())}")


def test_a_real_server_is_attested_running_and_stopped_by_its_stdin(runtime):
    port = _free_port()
    runtime.activate(_model(), port)
    s = _wait(runtime, lr.STATE_RUNNING, lr.STATE_ERROR)
    assert s["state"] == lr.STATE_RUNNING, s["error"]
    assert not lr._port_free(port), "the server holds its port"
    pid = server_pid(runtime)
    runtime.deactivate(wait=True)
    deadline = time.monotonic() + 10
    while platform_compat.pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not platform_compat.pid_exists(pid), "deactivate ended the server"
    assert runtime.status()["state"] == lr.STATE_IDLE
    deadline = time.monotonic() + 10
    while not lr._port_free(port) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert lr._port_free(port), "the port is free again"


def test_a_listener_that_does_not_know_the_secret_is_not_attested(runtime):
    port = _free_port()
    assert lr._health_ok(port, "wrong") is False, "nothing listens yet"
    runtime.activate(_model(), port)
    _wait(runtime, lr.STATE_RUNNING)
    assert lr._health_ok(port, "not-the-secret") is False
    assert lr._health_ok(port, runtime._run.nonce) is True


_PARENT = textwrap.dedent("""
    import sys, time
    sys.path[:0] = {paths!r}
    import test_decisions_local_runtime_real_spawn as t
    from kiro_crew.decisions import local_runtime as lr

    from pathlib import Path
    rt = t.make_runtime(Path({tmp!r}), setattr)
    rt.activate(t._model(), {port})
    t._wait(rt, lr.STATE_RUNNING)
    print(t.server_pid(rt), flush=True)
    time.sleep(600)
    """)


def test_the_server_does_not_outlive_a_gateway_that_dies(tmp_path):
    """The stdin tether is what ends the server when the gateway is killed outright."""
    port = _free_port()
    here = str(Path(__file__).resolve().parent)
    src = str(Path(lr.__file__).resolve().parents[2])
    script = _PARENT.format(paths=[here, src], tmp=str(tmp_path), port=port)
    parent = subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        cwd=str(tmp_path),
        start_new_session=platform_compat.IS_POSIX,
        creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        encoding="utf-8",
        errors="replace",
    )
    child = 0
    try:
        line = parent.stdout.readline() if parent.stdout else ""
        assert line.strip().isdigit(), parent.stderr.read() if parent.stderr else line
        child = int(line)
        parent.kill()  # no cleanup runs: the gateway is gone, not stopped
        parent.wait(timeout=30)
        deadline = time.monotonic() + 30
        while platform_compat.pid_exists(child) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not platform_compat.pid_exists(child), "the server outlived its gateway"
    finally:
        # Every exit path reaps both trees: the parent's group, and the server, which
        # the runtime starts in a session of its own.
        for pid, alive in ((parent.pid, parent.poll() is None), (child, bool(child))):
            if alive and platform_compat.pid_exists(pid):
                try:
                    platform_compat.kill_process_tree(pid)
                except (ProcessLookupError, OSError):
                    pass
        try:
            parent.wait(timeout=10)
        except subprocess.TimeoutExpired:
            parent.kill()
            parent.wait(timeout=10)
