"""The Code Review Sage peer proof, run for real from INSIDE the Linux sandbox.

The Code Review Sage backend's internal-secret senders go through the gateway
peer proof (``_owner_verified_secret_urlopen``). The Sage backend itself runs
under the namespace sandbox (``apps/backend.py`` -> ``wrap_argv(mode="standard")``
-> ``unshare(CLONE_NEWUSER)`` then ``unshare(CLONE_NEWNS)``), so the premise that
matters for availability is: can the proof reach ``MATCH`` from inside that sealed
mount namespace?  If the seal masks the gateway pidfile (``run/gateway-<port>.pid``
+ its ``.start`` sidecar), the dashboard unix socket, or ``/proc/<peer-pid>`` (the
freshness token read), the proof fails closed and every Sage review spawn and
publish breaks on that platform.

This test plays the gateway in the parent process -- it records the run marker for
its OWN pid (``write_marker``) and serves the dashboard unix socket, so the peer the
child sees over ``SO_PEERCRED`` is this process and the recorded pid matches it --
then runs the proof in a child confined by the real ``standard`` sandbox, against the
real primitive, and asserts the proof returns the gateway's response (a live
``MATCH``), not ``_GatewayPeerRefused``.

The child also reports its mount namespace and the sandbox marker, so a run that
never actually entered the sandbox fails loudly instead of passing on the host's
own view.  Linux needs unprivileged user and mount namespaces, which the
namespace-sandbox CI job provides and the shard runners do not, so there this test
skips with that reason; it is wired into that job's file list in ``ci.yml``.  The
macOS Seatbelt path is NOT covered here (no CI lane builds a sandboxed backend on
Seatbelt); the PR body and its First Principles disposition say so.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from kiro_crew import sandbox
from kiro_crew.dashboard.urls import dashboard_socket_path
from kiro_crew.instances import run_marker
from kiro_crew.sandbox import userns_available

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only"),
    pytest.mark.skipif(
        not userns_available(),
        reason="this host cannot build the namespace sandbox (no unprivileged user and mount "
        "namespaces); the namespace-sandbox CI job provides them and asserts userns_available()",
    ),
]

#: A port number used only to name the run marker and the socket; nothing binds TCP.
_PORT = 48137

# The agent's side, run under the real sandbox. It runs the UNMONKEYPATCHED proof and
# reports the outcome: a response body is a live MATCH; ``_GatewayPeerRefused`` is a
# fail-closed refusal (the thing this test exists to rule out inside the sandbox).
_CHILD = r"""
import json, os, sys, urllib.request

port = int(sys.argv[1])
from kiro_crew.cli_server_peer import _owner_verified_secret_urlopen, _GatewayPeerRefused

def proof():
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/internal/health" % port,
        headers={"X-Internal-Secret": "test-not-a-real-secret"},
    )
    try:
        with _owner_verified_secret_urlopen(req, port, 10, operation="sage_probe") as resp:
            return {"outcome": "match", "status": getattr(resp, "status", None)}
    except _GatewayPeerRefused as exc:
        return {"outcome": "refused", "detail": str(exc)}
    except Exception as exc:  # harness/transport error -- distinct from a refusal
        return {"outcome": "error", "detail": "%s: %s" % (type(exc).__name__, exc)}

report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "proof": proof(),
}
print("REPORT " + json.dumps(report), flush=True)
"""


class _UnixHTTPGateway:
    """A minimal HTTP/1.1 responder on the dashboard AF_UNIX socket.

    The parent process owns it, so ``SO_PEERCRED`` on the child's connection reports
    THIS process -- the same pid ``write_marker`` recorded -- which is what lets the
    real proof reach ``MATCH`` without any peercred monkeypatch.
    """

    def __init__(self, sock_path: Path) -> None:
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(sock_path))
        self._srv.listen(16)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            try:
                conn.settimeout(10)
                # Drain the request head; the body (if any) is irrelevant to MATCH.
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                body = b"{}"
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                    b"Connection: close\r\n\r\n" + body
                )
            except OSError:
                pass
            finally:
                conn.close()

    def stop(self) -> None:
        self._stop = True
        try:
            self._srv.close()
        except OSError:
            pass


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    # A SHORT data home: the AF_UNIX path has a ~108-byte kernel limit, and a pytest
    # tmp_path is long, so bind the socket under a short /tmp home both the parent and
    # the in-sandbox child resolve identically from KIROCREW_HOME. mkdtemp under the
    # suite's short run-root (``short_tmp_base`` / ``SHORT_TMP_PREFIX``) keeps the
    # dashboard AF_UNIX socket path under its ~108-byte kernel limit even when the
    # per-test temp-isolation diagnostic redirects tempfile into a long node-id dir,
    # and the finally's rmtree keeps it inside this fixture's own cleanup contract.
    short = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX, dir=short_tmp_base()))
    monkeypatch.setenv("KIROCREW_HOME", str(short))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    sandbox.reset_backend()
    # Record THIS process as the gateway serving _PORT (pid + .start freshness token).
    run_marker.write_marker(_PORT)
    sock_path = dashboard_socket_path(_PORT)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()
    assert len(str(sock_path)) < 108, f"AF_UNIX path too long: {sock_path}"
    server = _UnixHTTPGateway(sock_path)
    server.start()
    try:
        yield
    finally:
        server.stop()
        sandbox.reset_backend()
        shutil.rmtree(short, ignore_errors=True)


def _run_child() -> dict:
    argv = [sys.executable, "-c", _CHILD, str(_PORT)]
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    proc = subprocess.run(
        sandbox.namespace_argv(argv, "standard"),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    (line,) = [ln for ln in proc.stdout.splitlines() if ln.startswith("REPORT ")]
    report = json.loads(line[len("REPORT ") :])
    # It really ran confined: the launcher marked it, in a mount namespace of its own.
    assert report["marker"] == "1", output
    assert report["mnt_ns"] != os.readlink("/proc/self/ns/mnt"), output
    return report


def test_sage_peer_proof_reaches_match_inside_the_sandbox(gateway):
    """The real proof, run from inside the standard sandbox, reaches a live MATCH.

    A ``refused`` outcome here would mean the mount-ns seal masked the gateway
    pidfile, the dashboard socket, or ``/proc/<peer-pid>``, so the Sage backend
    could not prove its own gateway and every review spawn/publish would break.
    """
    report = _run_child()
    assert report["proof"]["outcome"] == "match", report["proof"]
