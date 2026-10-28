"""The Code Review Sage peer proof, run for real from INSIDE the agent sandbox.

The Code Review Sage backend's internal-secret senders go through the gateway
peer proof (``_owner_verified_secret_urlopen``). The Sage backend itself runs
confined (``apps/backend.py`` -> ``wrap_argv(mode="standard")``): under the Linux
namespace sandbox (``unshare(CLONE_NEWUSER)`` then ``unshare(CLONE_NEWNS)``) or,
on macOS, under ``sandbox-exec`` with the Seatbelt profile. So the premise that
matters for availability is: can the proof reach ``MATCH`` from inside that
confinement?  If it hides the gateway pidfile (``run/gateway-<port>.pid`` + its
``.start`` sidecar), the dashboard unix socket, or the peer's freshness token, the
proof fails closed and every Sage review spawn and publish breaks on that platform.

These tests play the gateway in the parent process -- it records the run marker for
its OWN pid (``write_marker``) and serves the dashboard unix socket, so the peer the
child sees over the socket's peer credentials is this process and the recorded pid
matches it -- then run the proof in a child confined by the real ``standard``
sandbox, against the real primitive, and assert the proof returns the gateway's
response (a live ``MATCH``), not ``_GatewayPeerRefused``.

The Linux TCP-only case plays a gateway that degraded to loopback TCP (no dashboard
socket, as when the socket path exceeds the AF_UNIX limit). Its proof takes the TCP
arm, which does not reach MATCH from inside the child's user namespace, so that case
pins the fail-closed refusal and reports each input of the TCP verdict.

The child also reports evidence that it really ran confined, so a run that never
entered the sandbox fails loudly instead of passing on the host's own view.

* Linux needs unprivileged user and mount namespaces. The namespace-sandbox CI job
  provides them and asserts ``userns_available()`` before running this file (it is
  in that job's file list in ``ci.yml``); elsewhere the Linux case skips with that
  reason.
* macOS runs the Seatbelt case wherever ``/usr/bin/sandbox-exec`` exists, which
  includes the macOS CI lane, and skips only where it is absent. The case also
  fails if ``wrap_argv`` did not put ``sandbox-exec`` in front of the child.
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
import warnings
from pathlib import Path

import pytest
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from kiro_crew import sandbox
from kiro_crew.dashboard.urls import dashboard_socket_path
from kiro_crew.instances import run_marker
from kiro_crew.sandbox import userns_available

pytestmark = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"),
    reason="the agent sandbox this proves is the Linux namespace launcher or macOS Seatbelt",
)

#: A port number used only to name the run marker and the socket in the unix-socket
#: cases; nothing binds TCP there. The TCP-only case binds an ephemeral port instead.
_PORT = 48137

#: ``sun_path`` capacity: 108 bytes on Linux, 104 on macOS.
_SUN_PATH_MAX = 104 if sys.platform == "darwin" else 108

_SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# The agent's side, run under the real sandbox. It runs the UNMONKEYPATCHED proof and
# reports the outcome: a response body is a live MATCH; ``_GatewayPeerRefused`` is a
# fail-closed refusal (the thing these tests exist to rule out inside the sandbox).
# ``confinement`` is the platform's own evidence that the child is confined: its
# mount namespace on Linux, and the kernel's ``sandbox_check`` answer on macOS
# (non-zero for a process running under a Seatbelt profile).
_CHILD = r"""
import json, os, sys, urllib.request

port = int(sys.argv[1])
from kiro_crew.cli_server_peer import _owner_verified_secret_urlopen, _GatewayPeerRefused
from kiro_crew.dashboard.urls import dashboard_socket_path

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

def confinement():
    if sys.platform == "darwin":
        import ctypes
        libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        check = libsystem.sandbox_check
        check.restype = ctypes.c_int
        check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        return {"sandbox_check": check(os.getpid(), None, 0)}
    return {"mnt_ns": os.readlink("/proc/self/ns/mnt")}

def tcp_stages():
    # Each input of the TCP arm's verdict, read on a connection of this process's
    # own, so a refusal names the stage that failed. ``argv[2]`` is the gateway's
    # pid; the stages are reported, not asserted, by the caller.
    if len(sys.argv) < 3:
        return None
    import socket
    from kiro_crew import cli_server_peer, platform_compat

    out = {"gateway_pid": int(sys.argv[2]), "own_uid": os.getuid()}
    fd_dir = "/proc/%s/fd" % sys.argv[2]
    try:
        os.readlink(os.path.join(fd_dir, sorted(os.listdir(fd_dir), key=int)[0]))
        out["fd_readlink"] = "ok"
    except OSError as exc:
        out["fd_readlink"] = "%s: %s" % (type(exc).__name__, exc.strerror)
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        out["peer_pid"] = cli_server_peer._connected_tcp_peer_pid(sock)
    out["recorded_pid"] = cli_server_peer._recorded_gateway_pid(port)
    out["owner_uid"] = platform_compat.process_owner_uid(int(sys.argv[2]))
    return out

report = {
    "marker": os.environ.get("KIROCREW_SANDBOX_ACTIVE"),
    "confinement": confinement(),
    "socket_present": dashboard_socket_path(port).exists(),
    "tcp_stages": tcp_stages(),
    "proof": proof(),
}
print("REPORT " + json.dumps(report), flush=True)
"""


class _HTTPGateway:
    """A minimal HTTP/1.1 responder on a listening socket the parent bound.

    The parent process owns it, so the peer the child's connection resolves to
    (the AF_UNIX peer credentials, or the owner of the loopback TCP 4-tuple) is
    THIS process -- the same pid ``write_marker`` recorded -- which is what lets the
    real proof reach ``MATCH`` without any peer-resolution monkeypatch.
    """

    def __init__(self, srv: socket.socket) -> None:
        self._srv = srv
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
    # A SHORT data home: the AF_UNIX path has a small kernel limit (``_SUN_PATH_MAX``)
    # and a pytest tmp_path is long, so bind the socket under a short home that both
    # the parent and the sandboxed child resolve identically from KIROCREW_HOME.
    # mkdtemp under the suite's short run-root (``short_tmp_base`` /
    # ``SHORT_TMP_PREFIX``) keeps the path short even when the per-test
    # temp-isolation diagnostic redirects tempfile into a long node-id dir, and the
    # finally's rmtree keeps it inside this fixture's own cleanup contract.
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
    assert len(str(sock_path)) < _SUN_PATH_MAX, f"AF_UNIX path too long: {sock_path}"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    server = _HTTPGateway(srv)
    server.start()
    try:
        yield
    finally:
        server.stop()
        sandbox.reset_backend()
        shutil.rmtree(short, ignore_errors=True)


@pytest.fixture
def tcp_only_gateway(tmp_path, monkeypatch):
    """This process as a TCP-only gateway: a loopback listener, no dashboard socket.

    This is the configuration the listener degrades to when the unix socket cannot
    be bound (for example a data home longer than the AF_UNIX limit), so the proof
    takes its TCP arm and must resolve the gateway from the connection's 4-tuple.
    Yields the bound port.
    """
    short = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX, dir=short_tmp_base()))
    monkeypatch.setenv("KIROCREW_HOME", str(short))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    sandbox.reset_backend()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    run_marker.write_marker(port)
    assert not dashboard_socket_path(port).exists()
    server = _HTTPGateway(srv)
    server.start()
    try:
        yield port
    finally:
        server.stop()
        sandbox.reset_backend()
        shutil.rmtree(short, ignore_errors=True)


def _child_argv(port: int = _PORT, gateway_pid: int | None = None) -> list[str]:
    argv = [sys.executable, "-c", _CHILD, str(port)]
    if gateway_pid is not None:
        argv.append(str(gateway_pid))
    return argv


def _run(wrapped: list[str]) -> tuple[dict, str]:
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    proc = subprocess.run(
        wrapped,
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
    # The launcher marked the child as running confined.
    assert report["marker"] == "1", output
    return report, output


@pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only")
@pytest.mark.skipif(
    not userns_available(),
    reason="this host cannot build the namespace sandbox (no unprivileged user and mount "
    "namespaces); the namespace-sandbox CI job provides them and asserts userns_available()",
)
def test_sage_peer_proof_reaches_match_inside_the_sandbox(gateway):
    """The real proof, run from inside the standard namespace sandbox, reaches MATCH.

    A ``refused`` outcome here would mean the mount-ns seal masked the gateway
    pidfile, the dashboard socket, or ``/proc/<peer-pid>``, so the Sage backend
    could not prove its own gateway and every review spawn/publish would break.
    """
    report, output = _run(sandbox.namespace_argv(_child_argv(), "standard"))
    # It ran in a mount namespace of its own.
    assert report["confinement"]["mnt_ns"] != os.readlink("/proc/self/ns/mnt"), output
    assert report["proof"]["outcome"] == "match", report["proof"]


@pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only")
@pytest.mark.skipif(
    not userns_available(),
    reason="this host cannot build the namespace sandbox (no unprivileged user and mount "
    "namespaces); the namespace-sandbox CI job provides them and asserts userns_available()",
)
def test_a_tcp_only_gateway_is_refused_inside_the_sandbox(tcp_only_gateway):
    """With a TCP-only gateway, the proof run from inside the sandbox fails closed.

    No dashboard socket exists, so the proof takes its TCP arm, which names the
    gateway's pid from the connection's 4-tuple (``/proc/net/tcp`` and the owner's
    ``/proc/<pid>/fd`` links), then checks it against the recorded pid and uid.
    From inside the Linux namespace sandbox that verdict is not reached, so the
    proof refuses before any byte is sent. This pins that documented limit: the
    sandboxed Sage backend proves its gateway only over the unix socket, and
    ``review_driver`` reports the shorter-data-home remedy for this case. The
    child reports each input of the verdict (``tcp_stages``), so the failure
    output names the stage that did not resolve.
    """
    port = tcp_only_gateway
    report, output = _run(sandbox.namespace_argv(_child_argv(port, os.getpid()), "standard"))
    # Surfaced in pytest's warnings summary, which a passing -q run still prints.
    warnings.warn("TCP_STAGES " + json.dumps(report["tcp_stages"]), stacklevel=1)
    assert report["confinement"]["mnt_ns"] != os.readlink("/proc/self/ns/mnt"), output
    assert report["socket_present"] is False, output
    assert report["proof"]["outcome"] == "refused", output


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS-only")
@pytest.mark.skipif(
    not os.path.exists(_SANDBOX_EXEC),
    reason=f"{_SANDBOX_EXEC} is absent on this host, so no Seatbelt profile can be applied",
)
def test_sage_peer_proof_reaches_match_inside_seatbelt(gateway):
    """The real proof, run from inside the standard Seatbelt profile, reaches MATCH.

    The child is wrapped by ``wrap_argv(mode="standard")``, the call the Sage backend
    launch uses. A ``refused`` outcome here would mean the profile denied the gateway
    pidfile, its ``.start`` sidecar or the dashboard socket, so the Sage backend could
    not prove its own gateway and every review spawn/publish would break on macOS.
    """
    wrapped, cleanup = sandbox.wrap_argv(_child_argv(), mode="standard")
    try:
        # The wrap really applied Seatbelt: a no-op wrap would run the child on the
        # host's own view and could reach MATCH without proving anything.
        assert any(os.path.basename(a) == "sandbox-exec" for a in wrapped), wrapped
        report, output = _run(wrapped)
    finally:
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)
    # The kernel reports the child as running under a sandbox profile.
    assert report["confinement"]["sandbox_check"] != 0, output
    assert report["proof"]["outcome"] == "match", report["proof"]
