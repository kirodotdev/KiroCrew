"""Failure-diagnosis ladder for an unhealthy instance tunnel.

When an instance won't connect or self-heal gives up, run a small set of probes
**in dependency order** and report the *first* broken link. That tells the user
the actionable cause instead of a generic "tunnel error":

    1. SSH reachable?            ``ssh <host> true``        → no  ⇒ ssh_unreachable
    2. Remote dashboard up?      ``ssh <host> curl …:RP``   → no  ⇒ remote_down
    3. Local forward reachable?  TCP connect 127.0.0.1:LP   → no  ⇒ tunnel_down
    else                                                          ⇒ ok

All probes are **read-only**, run ``ssh`` via an argv list (no local shell), use
short timeouts, and never surface secrets. ``ssh_host`` is validated before use.
The three probe coroutines are module-level so tests can monkeypatch them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

from kiro_crew.instances.validation import SshValidationError, validate_ssh_host

logger = logging.getLogger(__name__)

_LOOPBACK = "127.0.0.1"
_SSH_PROBE_TIMEOUT_SECS = 12.0
_REMOTE_CURL_TIMEOUT_SECS = 15.0
_LOCAL_CONNECT_TIMEOUT_SECS = 2.0

# Diagnosis codes (stable strings the UI can map to copy/icons).
OK = "ok"
SSH_UNREACHABLE = "ssh_unreachable"
REMOTE_DOWN = "remote_down"
TUNNEL_DOWN = "tunnel_down"
NOT_CONNECTED = "not_connected"
UNKNOWN = "unknown"

_REASONS = {
    OK: "All checks passed — SSH, remote dashboard, and local forward are healthy.",
    SSH_UNREACHABLE: "Can't SSH to the host (check SSH access or the host alias).",
    REMOTE_DOWN: "SSH works but the remote Kiro Crew dashboard isn't responding (is the "
    "remote gateway running?).",
    TUNNEL_DOWN: "SSH and the remote dashboard are up, but the local forward isn't "
    "reachable (tunnel down — reconnect).",
    NOT_CONNECTED: "SSH and the remote dashboard are up. This instance isn't connected yet "
    "(no local tunnel) — click Connect.",
    UNKNOWN: "Could not determine the failure cause.",
}


@dataclass
class DiagnosisResult:
    """Outcome of the diagnosis ladder."""

    code: str
    reason: str
    probes: list[dict] = field(default_factory=list)  # ordered [{name, ok}]

    @property
    def ok(self) -> bool:
        return self.code == OK

    def to_dict(self) -> dict:
        return {"code": self.code, "ok": self.ok, "reason": self.reason, "probes": self.probes}


async def _probe_ssh(ssh_host: str) -> bool:
    """Return True if ``ssh <host> true`` succeeds (host reachable + auth ok)."""
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "AddressFamily=inet",
        ssh_host,
        "true",
    ]
    return await _run_ok(argv, _SSH_PROBE_TIMEOUT_SECS)


async def _probe_remote_dashboard(ssh_host: str, remote_port: int) -> bool:
    """Return True if the remote dashboard answers on its loopback port.

    Runs ``curl`` on the *remote* host against ``127.0.0.1:<remote_port>``; any
    HTTP status (incl. an auth gate like 401/403/404) means it's listening. A
    ``000`` code or empty output means nothing is bound there.
    """
    remote_cmd = (
        f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 "
        f"http://{_LOOPBACK}:{int(remote_port)}/api/status"
    )
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "AddressFamily=inet",
        ssh_host,
        remote_cmd,
    ]
    out = await _run_stdout(argv, _REMOTE_CURL_TIMEOUT_SECS)
    if out is None:
        return False
    code = out.strip().strip("'\"")
    return bool(code) and code != "000"


async def _probe_local_forward(local_port: int) -> bool:
    """Return True if something accepts a TCP connect on the local forward."""
    if not local_port:
        return False
    try:
        fut = asyncio.open_connection(_LOOPBACK, int(local_port))
        _reader, writer = await asyncio.wait_for(fut, timeout=_LOCAL_CONNECT_TIMEOUT_SECS)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


async def _run_ok(argv: list[str], timeout: float) -> bool:
    """Run *argv*, return True iff it exits 0 within *timeout*."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return False
    return proc.returncode == 0


async def _run_stdout(argv: list[str], timeout: float) -> str | None:
    """Run *argv*, return decoded stdout on exit 0, else None."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return None
    if proc.returncode != 0:
        return None
    return stdout_b.decode("utf-8", "replace")


async def diagnose_instance(
    ssh_host: str,
    remote_port: int,
    local_port: int,
) -> DiagnosisResult:
    """Run the dependency-ordered probes and return the first broken link.

    Validates ``ssh_host`` first; an invalid host short-circuits to UNKNOWN with
    a clear reason rather than spawning ssh.
    """
    try:
        ssh_host = validate_ssh_host(ssh_host)
    except SshValidationError as e:
        return DiagnosisResult(code=UNKNOWN, reason=f"invalid ssh host: {e}", probes=[])

    probes: list[dict] = []

    ssh_ok = await _probe_ssh(ssh_host)
    probes.append({"name": "ssh", "ok": ssh_ok})
    if not ssh_ok:
        return DiagnosisResult(SSH_UNREACHABLE, _REASONS[SSH_UNREACHABLE], probes)

    remote_ok = await _probe_remote_dashboard(ssh_host, remote_port)
    probes.append({"name": "remote_dashboard", "ok": remote_ok})
    if not remote_ok:
        return DiagnosisResult(REMOTE_DOWN, _REASONS[REMOTE_DOWN], probes)

    # No local forward to probe means the instance was never connected (or is
    # disconnected) — that's NOT_CONNECTED, not a broken tunnel. Reporting
    # "tunnel down — reconnect" here would be misleading (there's nothing to
    # reconnect; the user needs to Connect for the first time).
    if not local_port:
        return DiagnosisResult(NOT_CONNECTED, _REASONS[NOT_CONNECTED], probes)

    forward_ok = await _probe_local_forward(local_port)
    probes.append({"name": "local_forward", "ok": forward_ok})
    if not forward_ok:
        return DiagnosisResult(TUNNEL_DOWN, _REASONS[TUNNEL_DOWN], probes)

    return DiagnosisResult(OK, _REASONS[OK], probes)
