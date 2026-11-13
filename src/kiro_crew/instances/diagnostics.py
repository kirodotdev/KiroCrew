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

from kiro_crew.cloud import aws
from kiro_crew.cloud import ssm as cloud_ssm
from kiro_crew.config.paths import CONFIG_DIR_NAME, LEGACY_CONFIG_DIR_NAME

# `_send_over_ssm` is imported rather than re-spelled so this rung's SSM dispatch
# cannot drift from the mint path's on the inner/outer timeout pair -- the same
# reason `_build_ssh_argv` is shared with the SSH rung rather than rebuilt here.
from kiro_crew.instances.ssm_token_mint import _send_over_ssm
from kiro_crew.instances.token_mint import (
    REMOTE_BIN_CANDIDATES,
    _build_ssh_argv,
    custom_remote_bin,
    ssh_spawn_argv_env,
)
from kiro_crew.instances.validation import (
    SshValidationError,
    SsmValidationError,
    split_ecs_target,
    validate_aws_profile,
    validate_aws_region,
    validate_remote_bin,
    validate_ssh_host,
    validate_ssm_run_as,
    validate_ssm_target,
)
from kiro_crew.platform_compat import kill_and_reap

logger = logging.getLogger(__name__)

_LOOPBACK = "127.0.0.1"
# Fallback ssh ConnectTimeout when a caller doesn't pass one (matches
# token_mint._build_ssh_argv's own fallback default). Real callers pass the
# configured instances.connect_timeout_secs, capped for diagnostics use — see
# DIAGNOSTICS_CONNECT_TIMEOUT_CAP_SECS.
_DEFAULT_PROBE_CONNECT_TIMEOUT_SECS = 10.0
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
# (a listed node whose agent is offline, or a read that failed -- no
# credentials, an IAM denial, an invalid region).
SSM_UNREACHABLE = "ssm_unreachable"
# SSM first-rung failure where the read succeeded and SSM lists no managed node
# with the instance's ssm_target at all: what a terminated or replaced instance
# (e.g. after an Auto Scaling instance refresh), an instance whose agent never
# registered, or the wrong account/region looks like, as distinct from a node
# that is listed but whose agent is offline.
SSM_TARGET_MISSING = "ssm_target_missing"
# SSM second-rung failures the probe's own output tells apart from a gateway that
# is simply not running, each worded with the crew setting that fixes it: the
# AWS profile is refused ssm:SendCommand on the instance; sudo knows no Remote
# user of that name; no kirocrew launcher where the mint looks for one; a Kiro
# Crew gateway answering on a port other than the configured Remote port.
SSM_ACCESS_DENIED = "ssm_access_denied"
SSM_USER_UNKNOWN = "ssm_user_unknown"
REMOTE_NOT_INSTALLED = "remote_not_installed"
REMOTE_WRONG_PORT = "remote_wrong_port"

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
    SSM_TARGET_MISSING: "SSM lists no managed node with this id in this AWS account and "
    "region. If the instance was terminated or replaced (for example by an Auto Scaling "
    "instance refresh), edit this instance and set SSM target to the new instance id. "
    "Otherwise check the AWS profile and region, and that the instance has registered "
    "with SSM (agent running, instance profile granting AmazonSSMManagedInstanceCore).",
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
    SSM_ACCESS_DENIED: "Your AWS profile isn't allowed to run commands on this instance over "
    "SSM (ssm:SendCommand was denied). A crew over SSM needs ssm:StartSession, ssm:SendCommand "
    "and ssm:GetCommandInvocation on this instance: add them to the profile's IAM policy.",
    SSM_USER_UNKNOWN: "User {user} doesn't exist on this instance. Ubuntu images use ubuntu. "
    "Set Remote user in Edit settings.",
    REMOTE_NOT_INSTALLED: "Kiro Crew isn't installed for user {user} on this instance. Run the "
    "install there as that user, or set Remote kirocrew path in Edit settings.",
    REMOTE_WRONG_PORT: "A Kiro Crew gateway is listening on port {found}, not {port}. Set "
    "Remote port to {found} in Edit settings.",
}

# Fargate variants: the ladder has no dashboard rung, and the first rung's reason
# comes from the exec-readiness preflight itself, which already names the cause.
_FARGATE_REASONS = {
    OK: "All checks passed -- the task's exec channel and the local forward are healthy.",
    TUNNEL_DOWN: "The task is ready for a forward, but the local forward isn't reachable "
    "(tunnel down -- reconnect).",
    NOT_CONNECTED: "The task is ready for a forward. This instance isn't connected yet "
    "(no local tunnel) -- click Connect.",
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


def _remote_status_probe_command(remote_port: int) -> str:
    """Remote shell snippet that reports whether the dashboard answers.

    Prints the HTTP status ``curl`` saw, which is what both transports' rung 2
    interprets: any status (including an auth gate like 401/403/404) means
    something is listening, while ``000`` — or no output at all, e.g. a remote
    with no ``curl`` — means nothing is bound there.
    """
    return _status_curl(str(int(remote_port)))


def _status_curl(port: str) -> str:
    """The ``curl`` of :func:`_remote_status_probe_command` for a *port* the remote shell reads.

    *port* is an int's digits, or a shell variable the caller's snippet set to digits.
    """
    return (
        f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 "
        f"http://{_LOOPBACK}:{port}/api/status"
    )


def _dashboard_is_listening(output: str | None) -> bool:
    """Interpret rung 2's remote ``curl`` output — see :func:`_remote_status_probe_command`.

    ``None`` is the SSH rung's "the ssh invocation itself failed" and is False,
    same as the no-output case; the SSM rung passes the invocation's stdout, so
    a remote whose ``curl`` never ran reaches here as an empty string.
    """
    if output is None:
        return False
    code = output.strip().strip("'\"")
    return bool(code) and code != "000"


async def _probe_ssh(
    ssh_host: str, connect_timeout_secs: float = _DEFAULT_PROBE_CONNECT_TIMEOUT_SECS
) -> bool:
    """Return True if ``ssh <host> true`` succeeds (host reachable + auth ok).

    ``connect_timeout_secs`` bounds the ssh ``ConnectTimeout`` option (on
    OpenSSH >= 8.6 this also covers the banner/KEX exchange, where a slow
    ProxyCommand spends its time — see token_mint._build_ssh_argv). A caller
    behind a slow proxy that raised ``instances.connect_timeout_secs`` to
    make CONNECT work must pass it here too, or this probe reports a
    reachable-but-slow host as unreachable. The outer wait_for leaves a fixed
    2s margin past the ssh-side timeout for auth + running ``true`` after
    connect succeeds.
    """
    argv = _build_ssh_argv(ssh_host, "true", connect_timeout_secs=connect_timeout_secs)
    return await _run_ok(argv, connect_timeout_secs + 2.0)


async def _probe_remote_dashboard(
    ssh_host: str,
    remote_port: int,
    connect_timeout_secs: float = _DEFAULT_PROBE_CONNECT_TIMEOUT_SECS,
) -> bool:
    """Return True if the remote dashboard answers on its loopback port.

    Runs ``curl`` on the *remote* host against ``127.0.0.1:<remote_port>``; any
    HTTP status (incl. an auth gate like 401/403/404) means it's listening. A
    ``000`` code or empty output means nothing is bound there.

    ``connect_timeout_secs`` — see :func:`_probe_ssh`. The outer wait_for
    leaves a 5s margin past the ssh-side timeout, matching the remote
    ``curl --max-time 5`` this runs after connect succeeds.
    """
    argv = _build_ssh_argv(
        ssh_host,
        _remote_status_probe_command(remote_port),
        connect_timeout_secs=connect_timeout_secs,
    )
    return _dashboard_is_listening(await _run_stdout(argv, connect_timeout_secs + 5.0))


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
    """Run the ssh *argv*, return True iff it exits 0 within *timeout*.

    Spawned through :func:`ssh_spawn_argv_env` like every other ssh child, so a
    ``ProxyCommand`` host probes the same under a GUI-launched gateway as the
    tunnel it is diagnosing.
    """
    argv, env = await asyncio.to_thread(ssh_spawn_argv_env, argv)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except OSError:
        return False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return False
    except asyncio.CancelledError:
        # A cancelled diagnosis (an instance edit, a gateway shutdown) must not
        # leave its ssh child dialing on with no owner.
        await kill_and_reap(proc)
        raise
    return proc.returncode == 0


async def _run_stdout(argv: list[str], timeout: float) -> str | None:
    """Run the ssh *argv*, return decoded stdout on exit 0, else None.

    Same spawn env as :func:`_run_ok`.
    """
    argv, env = await asyncio.to_thread(ssh_spawn_argv_env, argv)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except OSError:
        return None
    try:
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return None
    except asyncio.CancelledError:
        await kill_and_reap(proc)
        raise
    if proc.returncode != 0:
        return None
    return stdout_b.decode("utf-8", "replace")


async def diagnose_instance(
    ssh_host: str,
    remote_port: int,
    local_port: int,
    connect_timeout_secs: float = _DEFAULT_PROBE_CONNECT_TIMEOUT_SECS,
) -> DiagnosisResult:
    """Run the dependency-ordered probes and return the first broken link.

    Validates ``ssh_host`` first; an invalid host short-circuits to UNKNOWN with
    a clear reason rather than spawning ssh.

    ``connect_timeout_secs`` is forwarded to the ssh-based probes — see
    :func:`_probe_ssh`. Callers should pass the configured
    ``instances.connect_timeout_secs``, capped at
    ``DIAGNOSTICS_CONNECT_TIMEOUT_CAP_SECS``.
    """
    try:
        ssh_host = validate_ssh_host(ssh_host)
    except SshValidationError as e:
        return DiagnosisResult(code=UNKNOWN, reason=f"invalid ssh host: {e}", probes=[])

    probes: list[dict] = []

    ssh_ok = await _probe_ssh(ssh_host, connect_timeout_secs)
    probes.append({"name": "ssh", "ok": ssh_ok})
    if not ssh_ok:
        return DiagnosisResult(SSH_UNREACHABLE, _REASONS[SSH_UNREACHABLE], probes)

    remote_ok = await _probe_remote_dashboard(ssh_host, remote_port, connect_timeout_secs)
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


#: :func:`_probe_ssm_managed` answers.
_NODE_ONLINE = "online"
_NODE_MISSING = "missing"
_NODE_UNREACHABLE = "unreachable"


async def _probe_ssm_managed(ssm_target: str, profile: str = "", region: str = "") -> str:
    """Whether SSM reports *ssm_target* as an Online managed node, and if not, why.

    The SSM analogue of :func:`_probe_ssh`: it answers "can the control plane
    reach this box at all?" before we blame the remote gateway. Delegates to
    :func:`kiro_crew.cloud.ssm.managed_node_ping_status` (one read-only
    ``describe-instance-information`` through the launcher's ``run_aws``
    chokepoint) rather than re-implementing the call.

    Returns :data:`_NODE_ONLINE`; :data:`_NODE_MISSING` when the read succeeded
    and SSM lists no node with that id (a terminated or replaced instance, one
    whose agent never registered, or the wrong account/region); or :data:`_NODE_UNREACHABLE` for everything else --
    agent offline, an IAM denial, a timeout -- all of which are "the SSM path
    can't reach the target".
    """
    try:
        status = await asyncio.wait_for(
            asyncio.to_thread(cloud_ssm.managed_node_ping_status, ssm_target, profile, region),
            timeout=_SSM_PING_TIMEOUT_SECS,
        )
    except Exception:  # timeout, AWSError, dispatch failure — all "unreachable"
        return _NODE_UNREACHABLE
    if status == "Online":
        return _NODE_ONLINE
    if status == cloud_ssm.NODE_NOT_REGISTERED:
        return _NODE_MISSING
    return _NODE_UNREACHABLE


def _ssm_remote_probe_command(remote_port: int, remote_bin: str) -> str:
    """Rung 2's SSM snippet: the dashboard check, plus what tells a silent port's causes apart.

    Every line is tagged, so a login shell that prints its own text cannot be read
    as an answer:

    * ``status <code>`` -- :func:`_remote_status_probe_command` on *remote_port*.
    * ``port <p> <code>`` -- the same check on each port a gateway run-marker names
      (``run/gateway-<p>.bin`` under the data homes the mint reads, ``$KIROCREW_HOME``
      then the default and legacy ones, and the default run dir's ``gateway-<p>.home``
      pointer). Only the filename is read, and a marker outlives a crashed gateway,
      so the code says whether one answers.
    * ``bin`` -- the launcher the mint runs for *remote_bin* is executable, a bare
      command name found through the shell's command search.
    """
    run, legacy_run = f"$HOME/{CONFIG_DIR_NAME}/run/", f"$HOME/{LEGACY_CONFIG_DIR_NAME}/run/"
    # Unset, the first home is the default one again: a repeated port changes no verdict.
    override_run = f"${{KIROCREW_HOME:-$HOME/{CONFIG_DIR_NAME}}}/run/"
    markers = (
        f'"{override_run}"gateway-*.bin "{run}"gateway-*.bin "{run}"gateway-*.home '
        f'"{legacy_run}"gateway-*.bin'
    )
    custom = custom_remote_bin(remote_bin)
    bins = " ".join(f'"{b}"' for b in ((custom,) if custom is not None else REMOTE_BIN_CANDIDATES))
    # The mint runs a name without a slash through PATH, so look it up the same way.
    launcher_test = '[ -x "$__b" ]'
    if custom is not None and "/" not in custom:
        launcher_test = 'command -v "$__b" >/dev/null 2>&1'
    return " ".join(
        [
            f'echo "status $({_remote_status_probe_command(remote_port)})";',
            f"for __m in {markers}; do",
            '  __p="${__m##*/gateway-}"; __p="${__p%.*}";',
            '  case "$__p" in ""|*[!0-9]*) continue;; esac;',
            f'  echo "port $__p $({_status_curl("$__p")})";',
            "done;",
            f"for __b in {bins}; do",
            f"  if {launcher_test}; then echo bin; break; fi;",
            "done",
        ]
    )


async def _probe_remote_dashboard_ssm(
    ssm_target: str,
    remote_port: int,
    profile: str = "",
    region: str = "",
    run_as: str = "",
    remote_bin: str = "",
) -> tuple[str, str]:
    """``(code, reason)`` for the remote dashboard on its loopback port, via SSM.

    :data:`OK` when it answers. Otherwise the cause the dispatch or the snippet's
    output names (see :func:`_ssm_remote_probe_command`), in this order: the AWS
    profile refused ``ssm:SendCommand``; sudo knows no *run_as* user; a gateway
    answers on another marker's port and no marker names *remote_port*; no
    launcher and, for the default *remote_bin*, no run-marker either; and
    :data:`REMOTE_DOWN` for everything else, a timeout or a dispatch failure
    included.
    """
    # Built outside the try so a malformed port raises rather than being folded
    # into the "no answer" verdict below.
    remote_cmd = _ssm_remote_probe_command(remote_port, remote_bin)
    run_as = validate_ssm_run_as(run_as)
    down = (REMOTE_DOWN, _SSM_REASONS[REMOTE_DOWN])
    try:
        result = await _send_over_ssm(
            ssm_target, remote_cmd, profile, region, run_as, _SSM_REMOTE_PROBE_TIMEOUT_SECS
        )
    except aws.AWSError as e:
        if aws.is_access_denied(e.stderr):
            return SSM_ACCESS_DENIED, _SSM_REASONS[SSM_ACCESS_DENIED]
        return down
    except Exception:  # timeout, dispatch failure — treat as no answer
        return down
    listening, launcher, marked, own_marker = False, False, False, False
    ports: list[int] = []
    for line in (result.stdout or "").splitlines():
        tag, _, rest = line.strip().partition(" ")
        if tag == "status":
            listening = _dashboard_is_listening(rest)
        elif tag == "port":
            marked = True
            port, _, code = rest.partition(" ")
            if not port.isdigit():
                continue
            if int(port) == int(remote_port):
                own_marker = True
            elif _dashboard_is_listening(code):
                ports.append(int(port))
        elif tag == "bin":
            launcher = True
    if listening:
        return OK, ""
    if "sudo: unknown user" in (result.stderr or ""):
        return SSM_USER_UNKNOWN, _SSM_REASONS[SSM_USER_UNKNOWN].format(user=run_as)
    if not result.ok:  # the snippet did not run to its end, so its silence proves nothing
        return down
    # A live gateway prunes other ports' dead markers, so a marker for remote_port
    # means this crew's own gateway ran here and is down, whoever else answers.
    if ports and not own_marker:
        found = min(ports)
        return REMOTE_WRONG_PORT, _SSM_REASONS[REMOTE_WRONG_PORT].format(
            found=found, port=remote_port
        )
    # Without a custom remote_bin the mint runs a run-marker's launcher before the
    # candidates, and only a gateway that ran writes a marker, so one means installed.
    if not launcher and not (marked and custom_remote_bin(remote_bin) is None):
        return REMOTE_NOT_INSTALLED, _SSM_REASONS[REMOTE_NOT_INSTALLED].format(user=run_as)
    return down


async def diagnose_instance_ssm(
    ssm_target: str,
    remote_port: int,
    local_port: int,
    *,
    aws_profile: str = "",
    aws_region: str = "",
    ssm_run_as: str = "",
    remote_bin: str = "",
) -> DiagnosisResult:
    """SSM-transport diagnosis ladder — the SSM sibling of :func:`diagnose_instance`.

        1. SSM node reachable?     ``describe-instance-information`` → no ⇒ ssm_unreachable,
                                   or ssm_target_missing when SSM lists no such node
        2. Remote dashboard up?    ``send-command`` + curl :RP        → no ⇒ remote_down,
                                   or the cause the probe names (ssm_access_denied,
                                   ssm_user_unknown, remote_wrong_port, remote_not_installed)
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
        remote_bin = validate_remote_bin(remote_bin)
    except (SsmValidationError, SshValidationError) as e:
        return DiagnosisResult(code=UNKNOWN, reason=f"invalid SSM settings: {e}", probes=[])

    probes: list[dict] = []

    node = await _probe_ssm_managed(target, profile, region)
    probes.append({"name": "ssm_managed_node", "ok": node == _NODE_ONLINE})
    if node == _NODE_MISSING:
        return DiagnosisResult(SSM_TARGET_MISSING, _REASONS[SSM_TARGET_MISSING], probes)
    if node != _NODE_ONLINE:
        return DiagnosisResult(SSM_UNREACHABLE, _REASONS[SSM_UNREACHABLE], probes)

    code, reason = await _probe_remote_dashboard_ssm(
        target, remote_port, profile, region, run_as, remote_bin
    )
    probes.append({"name": "remote_dashboard", "ok": code == OK})
    if code != OK:
        return DiagnosisResult(code, reason, probes)

    # No local forward to probe means the instance was never connected (or is
    # disconnected) — that's NOT_CONNECTED, not a broken tunnel.
    if not local_port:
        return DiagnosisResult(NOT_CONNECTED, _SSM_REASONS[NOT_CONNECTED], probes)

    forward_ok = await _probe_local_forward(local_port)
    probes.append({"name": "local_forward", "ok": forward_ok})
    if not forward_ok:
        return DiagnosisResult(TUNNEL_DOWN, _SSM_REASONS[TUNNEL_DOWN], probes)

    return DiagnosisResult(OK, _SSM_REASONS[OK], probes)


async def _probe_task_exec_ready(
    cluster: str, task_id: str, profile: str, region: str
) -> tuple[bool, str]:
    """``(ready, reason)`` from the shared exec-readiness preflight, off the loop.

    The preflight is a synchronous ``aws ecs describe-tasks``, and this ladder is
    where its answer reaches an operator: the ``fargate`` forward opens without
    consulting it, so a task whose execute-command channel was never enabled, or
    whose in-task agent cannot reach ``ssmmessages``, surfaces here as the broken
    link rather than as a forward that just does not come up.
    """
    readiness = await asyncio.to_thread(
        cloud_ssm.task_exec_readiness, cluster, task_id, profile, region
    )
    return readiness.ready, readiness.reason


async def diagnose_instance_fargate(
    ssm_target: str,
    local_port: int,
    *,
    aws_profile: str = "",
    aws_region: str = "",
) -> DiagnosisResult:
    """Fargate diagnosis ladder -- the ECS-task sibling of :func:`diagnose_instance_ssm`.

        1. Task ready for a forward? ``describe-tasks`` exec readiness -> no => ssm_unreachable
        2. Local forward reachable?  TCP connect 127.0.0.1:LP              -> no => tunnel_down
        else                                                                   => ok

    There is no remote-dashboard rung: the task serves a turn API and runs no
    ``kirocrew``, so nothing could be sent over SSM to ask it. The first rung's
    reason is the preflight's own, which already distinguishes a channel that was
    never enabled (relaunch) from an agent still starting (retry). All probes are
    read-only.
    """
    try:
        target = validate_ssm_target(ssm_target)
        profile = validate_aws_profile(aws_profile)
        region = validate_aws_region(aws_region)
    except SsmValidationError as e:
        return DiagnosisResult(code=UNKNOWN, reason=f"invalid fargate settings: {e}", probes=[])
    parts = split_ecs_target(target)
    if parts is None:
        return DiagnosisResult(
            code=UNKNOWN,
            reason=f"invalid fargate settings: {target!r} is not an ECS task target",
            probes=[],
        )
    cluster, task_id, _runtime_id = parts

    probes: list[dict] = []

    ready, reason = await _probe_task_exec_ready(cluster, task_id, profile, region)
    probes.append({"name": "task_exec_ready", "ok": ready})
    if not ready:
        return DiagnosisResult(SSM_UNREACHABLE, reason, probes)

    if not local_port:
        return DiagnosisResult(NOT_CONNECTED, _FARGATE_REASONS[NOT_CONNECTED], probes)

    forward_ok = await _probe_local_forward(local_port)
    probes.append({"name": "local_forward", "ok": forward_ok})
    if not forward_ok:
        return DiagnosisResult(TUNNEL_DOWN, _FARGATE_REASONS[TUNNEL_DOWN], probes)

    return DiagnosisResult(OK, _FARGATE_REASONS[OK], probes)
