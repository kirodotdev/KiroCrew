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

from kiro_crew.cloud import ssm as cloud_ssm
from kiro_crew.instances.validation import (
    SshValidationError,
    SsmValidationError,
    validate_aws_profile,
    validate_aws_region,
    validate_ssh_host,
    validate_ssm_run_as,
    validate_ssm_target,
)

logger = logging.getLogger(__name__)

_LOOPBACK = "127.0.0.1"
_SSH_PROBE_TIMEOUT_SECS = 12.0
_REMOTE_CURL_TIMEOUT_SECS = 15.0
_LOCAL_CONNECT_TIMEOUT_SECS = 2.0
# SSM probes call the AWS control plane (describe-instance-information) and then
# run a remote command via send-command, both slower than a direct ssh spawn.
_SSM_PING_TIMEOUT_SECS = 25.0
_SSM_REMOTE_PROBE_TIMEOUT_SECS = 60.0

# Diagnosis codes (stable strings the UI can map to copy/icons).
OK = "ok"
SSH_UNREACHABLE = "ssh_unreachable"
REMOTE_DOWN = "remote_down"
TUNNEL_DOWN = "tunnel_down"
NOT_CONNECTED = "not_connected"
UNKNOWN = "unknown"
# SSM-specific first-rung failure: the target isn't a reachable managed node
# (agent offline, missing instance profile, wrong region/profile, or IAM denial).
SSM_UNREACHABLE = "ssm_unreachable"

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
    SSM_UNREACHABLE: "The SSM target isn't a reachable managed node — check that the "
    "instance is running with the SSM agent online, that its instance profile "
    "grants AmazonSSMManagedInstanceCore, and that your AWS profile/region and "
    "ssm:StartSession permissions are correct.",
}

# SSM variants of the shared rungs, worded for the SSM transport so the UI copy
# never tells an SSM user to "check SSH access".
_SSM_REASONS = {
    OK: "All checks passed — SSM session, remote dashboard, and local forward are healthy.",
    REMOTE_DOWN: "SSM reaches the instance but the remote Kiro Crew dashboard isn't "
    "responding (is the remote gateway running?).",
    TUNNEL_DOWN: "SSM and the remote dashboard are up, but the local forward isn't "
    "reachable (tunnel down — reconnect).",
    NOT_CONNECTED: "SSM and the remote dashboard are up. This instance isn't connected yet "
    "(no local tunnel) — click Connect.",
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


# ── SSM transport ─────────────────────────────────────────────────────────


async def _probe_ssm_managed(ssm_target: str, profile: str = "", region: str = "") -> bool:
    """Return True if SSM reports *ssm_target* as an Online managed node.

    The SSM analogue of :func:`_probe_ssh`: it answers "can the control plane
    reach this box at all?" before we blame the remote gateway. Delegates to
    :func:`kiro_crew.cloud.ssm.instance_is_managed` (read-only
    ``describe-instance-information`` through the launcher's ``run_aws``
    chokepoint) rather than re-implementing the call. A ``False`` covers agent
    offline, wrong region/profile, and an IAM denial alike — all of which are
    "the SSM path can't reach the target", which is what this rung reports.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(cloud_ssm.instance_is_managed, ssm_target, profile, region),
            timeout=_SSM_PING_TIMEOUT_SECS,
        )
    except Exception:  # timeout, AWSError, dispatch failure — all "unreachable"
        return False


async def _probe_remote_dashboard_ssm(
    ssm_target: str, remote_port: int, profile: str = "", region: str = "", run_as: str = ""
) -> bool:
    """Return True if the remote dashboard answers on its loopback port, via SSM.

    Runs the same ``curl`` check as the SSH rung but dispatches it with SSM
    ``send-command`` instead of ``ssh``. Any HTTP status (incl. an auth gate like
    401/403/404) means something is listening; ``000``/empty means nothing is
    bound there.
    """
    remote_cmd = (
        f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 "
        f"http://{_LOOPBACK}:{int(remote_port)}/api/status"
    )
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                cloud_ssm.run_command,
                ssm_target,
                remote_cmd,
                profile,
                region,
                run_as=validate_ssm_run_as(run_as),
                total_wait=int(_SSM_REMOTE_PROBE_TIMEOUT_SECS),
            ),
            timeout=_SSM_REMOTE_PROBE_TIMEOUT_SECS + 15,
        )
    except Exception:  # timeout, AWSError, dispatch failure — treat as no answer
        return False
    code = (result.stdout or "").strip().strip("'\"")
    return bool(code) and code != "000"


async def diagnose_instance_ssm(
    ssm_target: str,
    remote_port: int,
    local_port: int,
    *,
    aws_profile: str = "",
    aws_region: str = "",
    ssm_run_as: str = "",
) -> DiagnosisResult:
    """SSM-transport diagnosis ladder — the SSM sibling of :func:`diagnose_instance`.

        1. SSM node reachable?     ``describe-instance-information`` → no ⇒ ssm_unreachable
        2. Remote dashboard up?    ``send-command`` + curl :RP        → no ⇒ remote_down
        3. Local forward reachable? TCP connect 127.0.0.1:LP          → no ⇒ tunnel_down
        else                                                              ⇒ ok

    Validates the SSM params first; an invalid target short-circuits to UNKNOWN
    with a clear reason rather than dispatching an AWS call. All probes are
    read-only.
    """
    try:
        target = validate_ssm_target(ssm_target)
        profile = validate_aws_profile(aws_profile)
        region = validate_aws_region(aws_region)
        run_as = validate_ssm_run_as(ssm_run_as)
    except SsmValidationError as e:
        return DiagnosisResult(code=UNKNOWN, reason=f"invalid SSM settings: {e}", probes=[])

    probes: list[dict] = []

    managed_ok = await _probe_ssm_managed(target, profile, region)
    probes.append({"name": "ssm_managed_node", "ok": managed_ok})
    if not managed_ok:
        return DiagnosisResult(SSM_UNREACHABLE, _REASONS[SSM_UNREACHABLE], probes)

    remote_ok = await _probe_remote_dashboard_ssm(target, remote_port, profile, region, run_as)
    probes.append({"name": "remote_dashboard", "ok": remote_ok})
    if not remote_ok:
        return DiagnosisResult(REMOTE_DOWN, _SSM_REASONS[REMOTE_DOWN], probes)

    # No local forward to probe means the instance was never connected (or is
    # disconnected) — that's NOT_CONNECTED, not a broken tunnel.
    if not local_port:
        return DiagnosisResult(NOT_CONNECTED, _SSM_REASONS[NOT_CONNECTED], probes)

    forward_ok = await _probe_local_forward(local_port)
    probes.append({"name": "local_forward", "ok": forward_ok})
    if not forward_ok:
        return DiagnosisResult(TUNNEL_DOWN, _SSM_REASONS[TUNNEL_DOWN], probes)

    return DiagnosisResult(OK, _SSM_REASONS[OK], probes)
