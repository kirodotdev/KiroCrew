"""Inbound SSH tunnel manager for the Instances feature.

Adapts the supervised-child + state-machine design of
``kiro_crew.tunnel.manager.TunnelManager`` (which points *outward* to expose the
dashboard) to point *inward*: for each connected remote instance it supervises a
local child process that forwards a loopback port to the remote Kiro Crew's
dashboard port, over one of two transports (``Instance.connection_method``):

* ``"ssh"`` (default): ``ssh -N -L 127.0.0.1:LP:127.0.0.1:RP <ssh_host>``.
* ``"ssm"``: ``aws ssm start-session --document-name
  AWS-StartPortForwardingSession --target <ssm_target> --parameters
  portNumber=RP,localPortNumber=LP`` — no inbound SSH port or SSH key needed,
  only IAM (``ssm:StartSession``) and the SSM agent on the remote box.

Design note: a literal ``ssh -fN`` would make ssh fork into the background and
the foreground process exit immediately, which would leave the gateway unable to
supervise or kill the real forwarder. A gateway-supervised child must stay in the
foreground, so we use ``-N`` (no remote command) *without* ``-f``, mirroring how
``TunnelManager`` supervises its own child. Connection multiplexing is pinned
off in the argv (``ControlPath=none``) for the same reason: it lets a user's
``~/.ssh/config`` recreate that fork-and-exit shape from outside this module.
``ExitOnForwardFailure=yes`` ensures
ssh exits if the local forward can't be bound, so a failed connect is detected
rather than hanging. The SSM transport gets the equivalent detection from the
generic ready-poll (:meth:`_Tunnel._wait_until_ready`) plus a post-hoc ownership
recheck, since the ``session-manager-plugin`` child does not expose an
``ExitOnForwardFailure``-style flag.

Scope (Phase 1 / Stage 4): connect, disconnect, status, and shutdown-all, with
port allocation + token mint wired in. The health-probe loop and 2-tier
self-heal are Phase 3 — this module exposes clean seams (an ``on_exit`` hook and
a per-instance state machine) for that follow-up without implementing it here.
SSM support reuses every one of those seams — it is a second *transport* plugged
into the same tunnel/state-machine/self-heal/token-refresh code, not a parallel
implementation.

Security (standard practices): loopback-bound forwards only (never ``0.0.0.0``);
child spawned via argv list (no local shell) for both transports; ``ssh_host`` /
``remote_bin`` (SSH) and ``ssm_target`` / ``aws_profile`` / ``aws_region`` (SSM)
injection-validated before use; minted tokens held in memory only and never
logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiohttp

from kiro_crew import platform_compat
from kiro_crew.cloud import ssm as cloud_ssm

# The local (embedding) gateway's configured port — carried into the minted
# remote token as the CSP frame-ancestor parent origin so the embedded pane can
# be framed by this desktop app on whatever KIROCREW_PORT it runs on (no
# hardcoded port, no wildcard). See server._extra_frame_ancestors.
from kiro_crew.config.loader import DASHBOARD_PORT as _LOCAL_DASHBOARD_PORT
from kiro_crew.instances.constants import DEFAULT_MAX_RECOVERY_ATTEMPTS as _MAX_RECOVERY
from kiro_crew.instances.constants import DEFAULT_PROBE_FAILURE_THRESHOLD as _PROBE_FAILS
from kiro_crew.instances.constants import DEFAULT_PROBE_INTERVAL_SECS as _PROBE_INTERVAL
from kiro_crew.instances.constants import (
    DEFAULT_RECOVER_BACKOFF_MAX_SECS as _RECOVER_BACKOFF_MAX_SECS,
)
from kiro_crew.instances.constants import DEFAULT_TOKEN_PROBE_TIMEOUT_SECS as _TOKEN_PROBE_TIMEOUT
from kiro_crew.instances.constants import DEFAULT_TOKEN_REFRESH_FRACTION as _REFRESH_FRACTION
from kiro_crew.instances.constants import (
    DEFAULT_TUNNEL_BASE_PORT,
)
from kiro_crew.instances.diagnostics import diagnose_instance, diagnose_instance_ssm
from kiro_crew.instances.port_allocator import PortAllocator, _is_port_free
from kiro_crew.instances.registry import _UNALLOCATED_PORT, Instance, InstancesRegistry
from kiro_crew.instances.ssm_token_mint import (
    mint_remote_token_ssm,
    run_remote_kirocrew_ssm,
)
from kiro_crew.instances.token_mint import (
    TokenMintError,
    mint_remote_token,
    run_remote_kirocrew,
    ttl_to_seconds,
)
from kiro_crew.instances.validation import (
    SshValidationError,
    SsmValidationError,
    validate_aws_profile,
    validate_aws_region,
    validate_remote_bin,
    validate_ssh_host,
    validate_ssm_run_as,
    validate_ssm_target,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

_LOOPBACK = "127.0.0.1"
# How long to wait for the local forward port to start accepting connections
# before declaring the connect attempt failed. SSM's session-manager-plugin
# needs longer to establish (WebSocket handshake to the SSM service) than a
# direct ssh TCP connect, so the SSM transport uses a longer timeout below.
_DEFAULT_CONNECT_TIMEOUT_SECS = 15.0
_DEFAULT_SSM_CONNECT_TIMEOUT_SECS = 25.0
# Poll cadence while waiting for the forward to come up.
_READY_POLL_INTERVAL_SECS = 0.25
# Bound on retained stderr so a chatty/looping ssh can't grow memory unbounded.
_MAX_STDERR_CHARS = 2000

# Self-heal respawn backoff: wait this base (doubled per consecutive attempt,
# capped) before rebuilding a failed tunnel, so a flapping link / bind race
# can't spin a tight respawn loop. Applied in the scheduling seam (_on_tunnel_exit)
# so direct _recover() callers (tests) aren't slowed.
_RECOVER_BACKOFF_BASE_SECS = 1.0

# ssh prints these benign advisory lines to stderr on connect (post-quantum KEX
# warning); they are NOT failures. Strip them from captured stderr so the real
# error (e.g. "bind: Address already in use") isn't masked in logs/status.
_BENIGN_SSH_STDERR_MARKERS = (
    "post-quantum key exchange",
    "store now, decrypt later",
    "server may need to be upgraded",
    "openssh.com/pq",
)


def _recover_backoff_secs(attempt: int, cap: float = _RECOVER_BACKOFF_MAX_SECS) -> float:
    """Exponential backoff before a self-heal rebuild, capped at *cap*. *attempt* is 1-based."""
    base = _RECOVER_BACKOFF_BASE_SECS * (2 ** max(0, attempt - 1))
    return min(base, cap)


def _strip_benign_ssh_noise(text: str) -> str:
    """Drop ssh's benign post-quantum KEX warning lines so a real error shows."""
    kept = [
        ln
        for ln in text.splitlines()
        if ln.strip() and not any(m in ln.lower() for m in _BENIGN_SSH_STDERR_MARKERS)
    ]
    return "\n".join(kept).strip()


# CSI/ANSI escape sequences (WSSH banners carry color + cursor moves such as
# \x1b[31m and \x1b[1G); strip them so a control sequence can't corrupt surfaced
# status text or dashboard tooltips.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _sanitize_banner(text: str) -> str:
    """ANSI-strip + credential/exfil-redact untrusted ssh stderr before it is
    surfaced in status/logs, capped at 200 chars. The banner is external,
    proxy-controlled text, so it is a redacted secondary detail only — never a
    classification signal."""
    cleaned = _ANSI_CSI_RE.sub("", text)
    cleaned = redact_credentials(cleaned)[0]
    cleaned = redact_exfiltration_urls(cleaned)[0]
    return cleaned[:200]


class TunnelState(enum.Enum):
    """Per-instance tunnel states."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass
class TunnelStatus:
    """Serializable snapshot of one instance's tunnel (never holds the token)."""

    instance_id: str
    state: TunnelState = TunnelState.DISCONNECTED
    local_port: int = 0
    remote_port: int = 0
    error: str = ""
    connected_at: float = 0.0
    diagnosis: dict | None = None  # last failure-diagnosis ladder result

    def to_dict(self) -> dict:
        d: dict[str, object] = {
            "instance_id": self.instance_id,
            "state": self.state.value,
            "local_port": self.local_port,
            "remote_port": self.remote_port,
            "error": self.error,
            "connected_at": self.connected_at,
        }
        if self.diagnosis is not None:
            d["diagnosis"] = self.diagnosis
        return d


def _build_ssh_tunnel_argv(
    ssh_host: str, local_port: int, remote_port: int, *, compression: bool = True
) -> list[str]:
    """Build the supervised ``ssh -N -L`` argv (loopback-bound, no local shell).

    ``ssh_host`` must already be validated by :func:`validate_ssh_host`.

    ``compression`` adds ``-C`` (zlib transport compression). The forwarded
    stream carries the remote dashboard SPA bundle + all API/WS traffic, which
    is highly compressible; the gateway does not gzip at the HTTP layer, so this
    is the only compression in the path. See ``instances.ssh_compression``.
    """
    # Windows: not yet supported — requires the OpenSSH client (`ssh`) on PATH,
    # which isn't guaranteed; ssh-process kill handling also needs a Windows audit.
    # Tracked as follow-on work.
    forward = f"{_LOOPBACK}:{local_port}:{_LOOPBACK}:{remote_port}"
    argv = [
        "ssh",
        "-N",  # no remote command; foreground so the gateway can supervise it
    ]
    if compression:
        argv.append("-C")  # compress the forwarded stream (bundle + API/WS)
    argv += [
        "-o",
        "BatchMode=yes",  # never prompt — fail fast if auth is needed
        "-o",
        "ExitOnForwardFailure=yes",  # exit if the local forward can't bind
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "AddressFamily=inet",  # force IPv4 loopback (dodge ::1 fallback)
        # The forward must stay owned by the child this manager supervises.
        # Multiplexing takes it away from the user's ssh_config: ssh hands the
        # forward to an existing shared connection and exits 0, leaving it alive
        # under a process the gateway never spawned, so a tunnel that is in fact
        # serving is reported as dead.
        #
        # Routing and identity (`User`, `IdentityFile`, `Port`,
        # `ProxyJump`/`ProxyCommand`) are deliberately still inherited -- the
        # registry carries no inline equivalents. See §9 of the instances spec.
        "-o",
        "ControlPath=none",  # no socket to share -- this is what disables it
        "-o",
        "ControlMaster=no",  # policy; ControlPath alone suffices  # wokeignore:rule=master
        "-L",
        forward,
        ssh_host,
    ]
    return argv


def _build_ssm_tunnel_argv(
    ssm_target: str, local_port: int, remote_port: int, *, profile: str = "", region: str = ""
) -> list[str]:
    """Build the supervised ``aws ssm start-session`` port-forward argv.

    ``ssm_target``/``profile``/``region`` must already be injection-validated
    (:func:`validate_ssm_target` / :func:`validate_aws_profile` /
    :func:`validate_aws_region`). Delegates to
    :func:`kiro_crew.cloud.ssm.build_port_forward_argv` — the launcher's
    existing, reviewed argv builder — rather than duplicating it, so the two
    features can never drift on the SSM document/parameter shape.
    """
    return cloud_ssm.build_port_forward_argv(ssm_target, remote_port, local_port, profile, region)


class _SshTunnel:
    """Supervises one instance's tunnel child process (SSH or SSM transport).

    ``ssh_target``/``ssm_target`` and friends are transport-specific; exactly
    one of ``transport="ssh"`` (using ``ssh_host``) or ``transport="ssm"``
    (using ``ssm_target``/``aws_profile``/``aws_region``) is active, decided by
    the caller. All state-machine, health-probe, and self-heal behavior below
    is shared between both transports — only argv-building and exit-error
    classification differ.
    """

    def __init__(
        self,
        instance_id: str,
        ssh_host: str,
        local_port: int,
        remote_port: int,
        *,
        connect_timeout_secs: float = _DEFAULT_CONNECT_TIMEOUT_SECS,
        compression: bool = True,
        probe_failure_threshold: int = _PROBE_FAILS,
        on_exit: Callable[[str], None] | None = None,
        transport: str = "ssh",
        ssm_target: str = "",
        aws_profile: str = "",
        aws_region: str = "",
    ) -> None:
        self._id = instance_id
        self._ssh_host = ssh_host
        self._local_port = local_port
        self._remote_port = remote_port
        self._connect_timeout = connect_timeout_secs
        self._compression = compression
        # Consecutive health-probe failures tolerated before this tunnel is torn
        # down to trigger self-heal; the manager threads the config-tunable value.
        self._probe_fails = probe_failure_threshold
        self._on_exit = on_exit  # Phase 3 seam: called(instance_id) on unexpected exit
        self._transport = transport  # "ssh" or "ssm"
        self._ssm_target = ssm_target
        self._aws_profile = aws_profile
        self._aws_region = aws_region

        self._proc: asyncio.subprocess.Process | None = None
        self._monitor_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._probe_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._stop_event = asyncio.Event()
        self._probe_failures = 0
        self._probe_failed = False  # set when the health probe forced teardown
        self._stopping = False
        self._stderr_buf = ""
        self.status = TunnelStatus(
            instance_id=instance_id,
            local_port=local_port,
            remote_port=remote_port,
        )

    def _build_argv(self) -> list[str]:
        """Build the transport-specific supervised child argv."""
        if self._transport == "ssm":
            return _build_ssm_tunnel_argv(
                self._ssm_target,
                self._local_port,
                self._remote_port,
                profile=self._aws_profile,
                region=self._aws_region,
            )
        return _build_ssh_tunnel_argv(
            self._ssh_host, self._local_port, self._remote_port, compression=self._compression
        )

    async def start(self) -> bool:
        """Spawn the tunnel child and wait until the local forward is reachable.

        Returns True on success (state CONNECTED), False on failure (state ERROR
        with ``status.error`` populated). Idempotent guard: a second call while
        CONNECTED is a no-op returning True.
        """
        if self.status.state == TunnelState.CONNECTED:
            return True
        self._stopping = False
        self.status.state = TunnelState.CONNECTING
        self.status.error = ""
        argv = self._build_argv()
        target = self._ssm_target if self._transport == "ssm" else self._ssh_host
        logger.info(
            "Opening %s tunnel for %s: 127.0.0.1:%d -> %s:%d",
            self._transport,
            self._id,
            self._local_port,
            target,
            self._remote_port,
        )
        try:
            ssm = self._transport == "ssm"
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                # SSM tunnels get process-group isolation (mirroring
                # cloud.ssm.open_port_forward) so a later teardown can reap the aws
                # wrapper's session-manager-plugin child too — see _terminate().
                # Both kwargs are passed EXPLICITLY per the platform_compat spawn
                # recipe: on POSIX start_new_session=True calls setsid (killpg reaps
                # the group) and creationflags is 0; on Windows there is no setsid
                # (start_new_session is silently ignored) and
                # CREATE_NEW_PROCESS_GROUP is what makes the tree taskkill /T-reapable.
                start_new_session=(ssm and platform_compat.IS_POSIX),
                creationflags=(platform_compat.CREATE_NEW_PROCESS_GROUP if ssm else 0),
            )
        except OSError as e:
            self.status.state = TunnelState.ERROR
            self.status.error = f"failed to spawn {self._transport} tunnel: {e}"
            logger.error("Tunnel spawn failed for %s: %s", self._id, e)
            return False

        ready = await self._wait_until_ready()
        if not ready:
            await self._terminate()
            if self.status.state != TunnelState.ERROR:
                self.status.state = TunnelState.ERROR
                self.status.error = self.status.error or "tunnel did not become ready"
            return False

        self.status.state = TunnelState.CONNECTED
        self.status.connected_at = time.time()
        self.status.error = ""
        # Supervise for later unexpected exit (Phase 3 self-heal hooks here).
        self._monitor_task = asyncio.create_task(self._monitor())
        # Health probe: detect a tunnel that's alive-but-not-forwarding and tear
        # it down so the monitor's on_exit seam can recover it (Stage 2).
        if _PROBE_INTERVAL > 0:
            self._probe_task = asyncio.create_task(self._probe_loop())
        logger.info("Tunnel connected for %s on 127.0.0.1:%d", self._id, self._local_port)
        return True

    async def _probe_loop(self) -> None:
        """Poll the local forward while CONNECTED; tear down on repeated failure.

        Sleeps ``_PROBE_INTERVAL`` between probes (interruptible by ``stop()``).
        A successful reachability check resets the failure counter; after
        ``_PROBE_FAILS`` consecutive failures the tunnel is treated as a zombie
        (alive child, no forwarding) and the child is terminated — the existing
        ``_monitor`` then fires ``on_exit`` so Stage 2 can rebuild/re-mint.
        Mirrors ``TunnelManager._probe_loop``.
        """
        try:
            while not self._stopping and self.status.state == TunnelState.CONNECTED:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=_PROBE_INTERVAL)
                    return  # stop() was requested during the interval
                except asyncio.TimeoutError:
                    pass  # interval elapsed — time to probe
                if self._stopping or self.status.state != TunnelState.CONNECTED:
                    return
                if await self._port_reachable():
                    self._probe_failures = 0
                    continue
                self._probe_failures += 1
                logger.warning(
                    "Tunnel health probe failed (%d/%d) for %s",
                    self._probe_failures,
                    self._probe_fails,
                    self._id,
                )
                if self._probe_failures >= self._probe_fails:
                    logger.warning(
                        "Tunnel for %s unhealthy after %d probe failures — tearing "
                        "down to trigger recovery",
                        self._id,
                        self._probe_failures,
                    )
                    self._probe_failed = True
                    self._probe_failures = 0
                    # Terminate the child; _monitor (not stopping) marks ERROR and
                    # fires on_exit. Done in a task so we don't await our own
                    # cancellation if stop() races in.
                    asyncio.create_task(self._terminate())
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let the probe loop crash silently
            logger.exception("Tunnel probe loop crashed for %s: %s", self._id, exc)

    async def _wait_until_ready(self) -> bool:
        """Poll the local forward until it accepts a connection or we time out.

        Fails early if the ssh child exits before the port comes up (e.g. auth
        failure, ExitOnForwardFailure), capturing stderr for diagnostics.
        """
        deadline = time.monotonic() + self._connect_timeout
        while time.monotonic() < deadline:
            proc = self._proc
            if proc is not None and proc.returncode is not None:
                await self._capture_stderr()
                self.status.state = TunnelState.ERROR
                self.status.error = self._exit_error(proc.returncode)
                return False
            if await self._port_reachable():
                # A reachable port is NOT proof THIS child bound it: a lingering
                # tunnel or orphaned ssh can answer while our child already lost
                # the bind race (ExitOnForwardFailure -> exit 255). Confirm our
                # child is still alive before declaring the tunnel ready.
                proc = self._proc
                if proc is not None and proc.returncode is not None:
                    await self._capture_stderr()
                    self.status.state = TunnelState.ERROR
                    self.status.error = self._exit_error(proc.returncode)
                    return False
                return True
            await asyncio.sleep(_READY_POLL_INTERVAL_SECS)
        self.status.error = f"timed out after {self._connect_timeout}s waiting for forward"
        return False

    async def _port_reachable(self) -> bool:
        """Return True if something accepts a TCP connect on the local forward."""
        try:
            fut = asyncio.open_connection(_LOOPBACK, self._local_port)
            reader, writer = await asyncio.wait_for(fut, timeout=1.0)
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True

    async def _monitor(self) -> None:
        """Await the child's exit; on unexpected exit mark ERROR and notify."""
        proc = self._proc
        if proc is None:
            return
        try:
            await proc.wait()
        except asyncio.CancelledError:
            raise
        if self._stopping:
            return
        await self._capture_stderr()
        self.status.state = TunnelState.ERROR
        self.status.error = self._exit_error(proc.returncode)
        logger.warning("Tunnel for %s exited unexpectedly: %s", self._id, self.status.error)
        if self._on_exit is not None:
            with contextlib.suppress(Exception):
                self._on_exit(self._id)

    def _exit_error(self, returncode: int | None) -> str:
        """Compose a human error from exit code + captured stderr.

        Classifies on real ssh signals, not on prose the WSSH proxy passes
        through. A genuine auth failure (permission denied / publickey /
        certificate expired) is reported as auth; a WSSH session/transport drop
        (idle timeout, banner-exchange timeout, reset, refused) is reported as a
        transport drop — never as an auth verdict inferred from banner text. The
        raw banner is ANSI-stripped and credential-redacted before it is
        surfaced as a secondary detail.

        The SSM transport has an entirely different error vocabulary (IAM
        denials, a missing session-manager-plugin, an offline SSM agent), so it
        is classified separately by :meth:`_ssm_exit_error` — running SSM stderr
        through the ssh matchers above would mislabel e.g. an ``AccessDenied``
        as an "ssh auth failure".
        """
        if self._probe_failed:
            return "health probe failed — tunnel alive but not forwarding"
        if self._transport == "ssm":
            return self._ssm_exit_error(returncode)
        # Drop ssh's benign post-quantum KEX advisory so it can't mask the real
        # failure (the loop symptom was this warning hiding "bind: ... in use").
        tail = _strip_benign_ssh_noise(self._stderr_buf)
        low = tail.lower()
        detail = _sanitize_banner(tail)
        # Genuine ssh auth signals first, so a real auth failure is never masked
        # by a transport phrase that happens to co-occur in the same banner.
        if (
            "permission denied" in low
            or "publickey" in low
            or "authentication failed" in low
            or "certificate has expired" in low
            or "certificate expired" in low
        ):
            return f"ssh auth failed (check SSH access): {detail}"
        # WSSH / transport session drops — not an auth problem. Worded neutrally
        # because this method is also used for the initial-connect failure path,
        # where no self-heal is armed yet (so it must not promise reconnection).
        if (
            "timed out during banner exchange" in low
            or "session ended unexpectedly" in low
            or "connection timed out" in low
            or "connection reset" in low
            or "closed by remote host" in low
            or "connection refused" in low
        ):
            return f"ssh tunnel transport drop: {detail}"
        if "address already in use" in low or "cannot listen to port" in low:
            return f"ssh forward bind failed (local port already in use): {detail}"
        if tail:
            return f"ssh exited {returncode}: {detail}"
        return f"ssh exited with code {returncode}"

    def _ssm_exit_error(self, returncode: int | None) -> str:
        """Classify an ``aws ssm start-session`` port-forward child's exit.

        Distinguishes the failure modes an operator can actually act on:
        expired/absent AWS credentials, an IAM denial on ``ssm:StartSession``,
        a missing local ``session-manager-plugin``, an instance that is not a
        registered/online SSM managed node, and a local bind conflict. Like the
        ssh classifier, the raw stderr is ANSI-stripped and credential-redacted
        before being surfaced as a secondary detail.
        """
        tail = self._stderr_buf.strip()
        low = tail.lower()
        detail = _sanitize_banner(tail)
        # Credentials first: an expired/absent credential is the most common
        # cause and its message can also contain "not authorized"-adjacent text.
        if (
            "expired" in low
            or "unable to locate credentials" in low
            or "no credentials" in low
            or "credentials not found" in low
        ):
            return (
                "AWS credentials missing or expired (refresh them, e.g. "
                f"`aws sso login --profile <name>`): {detail}"
            )
        if "accessdenied" in low or "not authorized" in low or "unauthorizedoperation" in low:
            return f"IAM denied ssm:StartSession for this target: {detail}"
        if "sessionmanagerplugin" in low or "session-manager-plugin" in low:
            return (
                "session-manager-plugin is not installed locally (install the AWS "
                f"Session Manager plugin, then reconnect): {detail}"
            )
        if (
            "targetnotconnected" in low
            or "not connected" in low
            or "invalidinstanceid" in low
            or "invalidinstanceinformation" in low
        ):
            return (
                "the SSM target is not a connected managed node (is the instance "
                f"running with the SSM agent online and an instance profile?): {detail}"
            )
        if "address already in use" in low or "bind" in low:
            return f"SSM forward bind failed (local port already in use): {detail}"
        if tail:
            return f"SSM session exited {returncode}: {detail}"
        return f"SSM session exited with code {returncode}"

    async def _capture_stderr(self) -> None:
        """Drain whatever the ssh child wrote to stderr (bounded)."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        with contextlib.suppress(Exception):
            data = await proc.stderr.read()
            if data:
                self._stderr_buf = (self._stderr_buf + data.decode("utf-8", "replace"))[
                    -_MAX_STDERR_CHARS:
                ]

    async def stop(self) -> None:
        """Tear down this tunnel (graceful terminate then kill)."""
        self._stopping = True
        self._stop_event.set()
        if self._probe_task and not self._probe_task.done():
            self._probe_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._probe_task
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task
        await self._terminate()
        self.status.state = TunnelState.STOPPED
        logger.info("Tunnel stopped for %s", self._id)

    async def _terminate(self) -> None:
        """Terminate the tunnel child if running (terminate, then kill on timeout).

        For the **SSM** transport the child is the ``aws`` wrapper, and the
        ``session-manager-plugin`` grandchild is what actually holds the
        forwarded local port — ``proc.terminate()`` alone would signal only the
        wrapper and leave the plugin alive still bound to the port (the exact
        leak :func:`kiro_crew.cloud.ssm.kill_port_forward` documents). Since
        :meth:`start` spawns SSM children with process-group isolation, we reap
        the whole tree via :func:`platform_compat.kill_process_tree` — ``killpg``
        on POSIX, ``taskkill /T`` on Windows, so the plugin is reaped on **every**
        supported platform. A tree-kill failure falls back to the single-process
        kill.
        """
        proc = self._proc
        if proc and proc.returncode is None:
            group_signalled = False
            if self._transport == "ssm":
                group_signalled = self._signal_group(proc.pid, platform_compat.SIGTERM)
            try:
                if not group_signalled:
                    proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                if self._transport == "ssm" and self._signal_group(
                    proc.pid, platform_compat.SIGKILL
                ):
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=5)
                else:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
        self._proc = None

    @staticmethod
    def _signal_group(pid: int, sig: int) -> bool:
        """Reap *pid*'s whole process tree. Returns whether it was delivered.

        Routed through :func:`platform_compat.kill_process_tree` rather than a raw
        ``os.killpg``/``os.getpgid`` pair, which exist only on POSIX and would
        leave the ``session-manager-plugin`` grandchild orphaned (still holding
        the forwarded port) on native Windows — a supported platform.

        Best-effort and never raises: the shim propagates exceptions (an
        already-reaped tree, a refused broadcast pgid, a protected Windows
        descendant, a permission error), and all of them mean "not delivered", so
        the caller falls back to the single-process kill.
        """
        try:
            return platform_compat.kill_process_tree(pid, sig)
        except (ProcessLookupError, PermissionError, OSError, ValueError, AttributeError):
            return False

    @property
    def pid(self) -> int | None:
        """PID of the live ssh child, or None if not running."""
        proc = self._proc
        return proc.pid if proc is not None and proc.returncode is None else None


@dataclass
class _TransportParams:
    """Validated, transport-specific connection parameters for one instance.

    Resolved once by :meth:`SshTunnelManager._resolve_transport` so the
    connect / rebuild / self-heal / token-refresh paths all build their tunnel
    and mint their token from the same validated values instead of each
    re-branching on ``connection_method``.
    """

    method: str  # "ssh" | "ssm"
    ssh_host: str = ""
    remote_bin: str = ""
    ssm_target: str = ""
    aws_profile: str = ""
    aws_region: str = ""
    ssm_run_as: str = ""

    @property
    def target(self) -> str:
        """The human-facing target (ssh host or SSM instance id) for messages."""
        return self.ssm_target if self.method == "ssm" else self.ssh_host

    def tunnel_kwargs(self) -> dict:
        """Transport kwargs for the ``_SshTunnel`` constructor."""
        return {
            "transport": self.method,
            "ssm_target": self.ssm_target,
            "aws_profile": self.aws_profile,
            "aws_region": self.aws_region,
        }


class SshTunnelManager:
    """Manages per-instance tunnels (SSH or SSM) keyed by instance id.

    Holds the live tunnels, allocates loopback ports, mints per-instance tokens,
    and keeps the registry's ``was_connected`` / ``last_active`` hints in sync.
    Tokens are kept in memory only (never persisted, never logged) and handed to
    the API layer via :meth:`get_token`.

    The class name is retained (rather than renamed to a transport-neutral one)
    because it is referenced by ``dashboard/server.py`` and the existing test
    suite; it now supervises whichever transport each instance's
    ``connection_method`` selects.
    """

    def __init__(
        self,
        registry: InstancesRegistry,
        *,
        base_port: int = DEFAULT_TUNNEL_BASE_PORT,
        connect_timeout_secs: float = _DEFAULT_CONNECT_TIMEOUT_SECS,
        ssh_compression: bool = True,
        max_recovery_attempts: int = _MAX_RECOVERY,
        recover_backoff_max_secs: float = _RECOVER_BACKOFF_MAX_SECS,
        probe_failure_threshold: int = _PROBE_FAILS,
        mint_token: Callable[..., Awaitable[str]] = mint_remote_token,
        tunnel_factory: Callable[..., _SshTunnel] | None = None,
        parent_port: int | None = None,
    ) -> None:
        self._registry = registry
        # The port the embedding dashboard ACTUALLY bound, carried into every
        # minted remote token as the CSP frame-ancestor parent origin.
        #
        # Falls back to the configured value only when the caller cannot supply
        # the real one. The distinction matters: ``DASHBOARD_PORT`` is derived
        # from env/config at import time, so in the desktop app — which resolves
        # its own port but spawns the backend without passing it through — the
        # two disagree, the claim names a port the parent is not served on, and
        # the remote's ``frame-ancestors`` then blocks the iframe ("Pane failed
        # to load"). The gateway already knows its real port (``app["port"]``,
        # passed to ``_register_instances_hooks``), so it is threaded in here
        # rather than re-derived.
        self._parent_port = parent_port if parent_port else _LOCAL_DASHBOARD_PORT
        self._allocator = PortAllocator(base_port=base_port)
        self._connect_timeout = connect_timeout_secs
        self._ssh_compression = ssh_compression
        # Self-heal tunables (config-tunable via instances.*): max consecutive
        # recovery attempts before give-up, the cap on the per-attempt backoff,
        # and the per-tunnel consecutive-probe-failure teardown threshold.
        self._max_recovery = max_recovery_attempts
        self._recover_backoff_max = recover_backoff_max_secs
        self._probe_fails = probe_failure_threshold
        self._mint_token = mint_token
        self._tunnel_factory = tunnel_factory or _SshTunnel
        # Only the real ssh path reaps OS-level orphans; injected fakes (tests)
        # skip it so unit tests stay hermetic (no `ps`/`kill` side effects).
        self._reaps_orphans = tunnel_factory is None
        self._tunnels: dict[str, _SshTunnel] = {}
        self._tokens: dict[str, str] = {}
        # Last connect/reconnect failure reason per instance, retained after the
        # failed tunnel is popped so a sticky tab whose tunnel is down can still
        # report *why* (e.g. a startup auto-revive that couldn't reach the host).
        # Cleared on a successful connect or an explicit disconnect.
        self._last_error: dict[str, str] = {}
        self._lock = asyncio.Lock()
        # Self-heal: consecutive recovery attempts per instance (reset on a
        # successful rebuild) + live recovery task refs (stored so they aren't
        # GC'd mid-flight; cancelled on shutdown).
        self._recover_attempts: dict[str, int] = {}
        self._recovery_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        # Proactive token refresh: per-instance refresh task + the mint timestamp
        # / ttl so the TTL-remaining can be surfaced (Stage 6).
        self._refresh_tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        self._token_minted_at: dict[str, float] = {}
        self._token_ttl_secs: dict[str, int] = {}

    def _reserved_ports(self) -> set[int]:
        """Ports already taken: live tunnels + local_port set on any instance."""
        reserved: set[int] = {t.status.local_port for t in self._tunnels.values()}
        for inst in self._registry.list():
            if inst.local_port:
                reserved.add(inst.local_port)
        return reserved

    def _connect_timeout_for(self, method: str) -> float:
        """Readiness timeout for *method*, honoring an explicit caller override.

        SSM's ``session-manager-plugin`` has to complete a WebSocket handshake
        with the SSM service before it binds the local port, which routinely
        takes longer than a direct ssh TCP connect — so the SSM default is
        higher. A caller that passed an explicit ``connect_timeout_secs``
        (tests, tuning) wins for both transports.
        """
        if self._connect_timeout != _DEFAULT_CONNECT_TIMEOUT_SECS:
            return self._connect_timeout  # explicit override
        if method == "ssm":
            return _DEFAULT_SSM_CONNECT_TIMEOUT_SECS
        return self._connect_timeout

    async def _ps_lines(self) -> list[str]:
        """Return ``<pid> <command>`` lines for all processes (portable ps).

        Factored out so tests can stub it; best-effort (empty on any failure).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "ps",
                "-axww",
                "-o",
                "pid=,command=",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except Exception:
            return []
        return out.decode("utf-8", "replace").splitlines()

    async def _reap_orphan_forwarder(self, local_port: int) -> int:
        """SIGTERM any stale ssh forwarder still holding *local_port*.

        Graceful shutdown (Ctrl+C / SIGTERM -> on_cleanup -> shutdown()) already
        tears tunnels down, but a hard kill (SIGKILL / crash / hard restart)
        bypasses it and — since macOS has no parent-death signal — leaves the
        ``ssh -N -L 127.0.0.1:<local_port>:...`` child holding the port, so the
        next connect fails ExitOnForwardFailure forever. This clears such an
        orphan before we (re)bind. Matches our forward signature only, skips PIDs
        of live tracked tunnels and our own pid, and never raises.
        """
        signature = f"-L {_LOOPBACK}:{int(local_port)}:"
        live_pids = {p for p in (getattr(t, "pid", None) for t in self._tunnels.values()) if p}
        own = os.getpid()
        reaped = 0
        for line in await self._ps_lines():
            line = line.strip()
            if signature not in line:
                continue
            parts = line.split(None, 2)  # <pid> <exe> <rest>
            if len(parts) < 2:
                continue
            head, exe = parts[0], parts[1]
            if exe.rsplit("/", 1)[-1] != "ssh":  # the forwarder must BE ssh
                continue
            try:
                pid = int(head)
            except ValueError:
                continue
            if pid == own or pid in live_pids:
                continue
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
                reaped += 1
        if reaped:
            logger.warning(
                "Reaped %d orphaned ssh forwarder(s) holding 127.0.0.1:%d "
                "(leftover from an unclean prior exit)",
                reaped,
                local_port,
            )
        return reaped

    def _resolve_transport(self, inst: Instance) -> _TransportParams:
        """Validate + resolve *inst*'s transport params immediately before use.

        Raises :class:`SshValidationError` / :class:`SsmValidationError` so each
        caller can surface a clean per-instance error. Validation happens here —
        right before a command line is built — rather than trusting the
        registry's lighter early-reject charset checks.
        """
        method = (inst.connection_method or "ssh").strip().lower()
        if method == "ssm":
            return _TransportParams(
                method="ssm",
                ssm_target=validate_ssm_target(inst.ssm_target),
                aws_profile=validate_aws_profile(inst.aws_profile),
                aws_region=validate_aws_region(inst.aws_region),
                ssm_run_as=validate_ssm_run_as(inst.ssm_run_as),
                remote_bin=validate_remote_bin(inst.remote_bin),
            )
        return _TransportParams(
            method="ssh",
            ssh_host=validate_ssh_host(inst.ssh_host),
            remote_bin=validate_remote_bin(inst.remote_bin),
        )

    async def _mint_for(self, inst: Instance, params: _TransportParams) -> str:
        """Mint a dashboard token for *inst* over its configured transport.

        The SSH path goes through the injectable ``self._mint_token`` seam (kept
        so the existing tests can substitute a fake mint); the SSM path calls
        :func:`mint_remote_token_ssm`. Never logs the token.
        """
        if params.method == "ssm":
            return await mint_remote_token_ssm(
                params.ssm_target,
                aws_profile=params.aws_profile,
                aws_region=params.aws_region,
                ssm_run_as=params.ssm_run_as,
                remote_bin=params.remote_bin,
                ttl=inst.ttl,
                remote_port=inst.remote_port,
                embed_parent_port=self._parent_port,
            )
        return await self._mint_token(
            params.ssh_host,
            remote_bin=params.remote_bin,
            ttl=inst.ttl,
            remote_port=inst.remote_port,
            embed_parent_port=self._parent_port,
        )

    async def connect(self, instance_id: str) -> TunnelStatus:
        """Open a tunnel + mint a token for *instance_id*; return its status.

        Idempotent: connecting an already-connected instance returns its current
        status. Raises :class:`KeyError` for an unknown instance, or surfaces a
        validation / mint / spawn error via the returned status (state ERROR).
        Works for either ``connection_method`` — the transport is resolved by
        :meth:`_resolve_transport`.
        """
        async with self._lock:
            inst = self._registry.get(instance_id)
            if inst is None:
                raise KeyError(f"no instance with id {instance_id!r}")

            existing = self._tunnels.get(instance_id)
            if existing is not None and existing.status.state == TunnelState.CONNECTED:
                return existing.status
            if existing is not None:
                # Tracked but not CONNECTED: stop it first so its child is
                # terminated and the local forward freed before we spawn a
                # replacement. Otherwise the old child orphans (dropped from
                # _tunnels below, never killed) and keeps the port — every
                # replacement then hits ExitOnForwardFailure while _port_reachable
                # is still satisfied by the orphan -> tight respawn loop.
                with contextlib.suppress(Exception):
                    await existing.stop()

            # Injection-safe validation immediately before building command lines.
            try:
                params = self._resolve_transport(inst)
            except (SshValidationError, SsmValidationError) as e:
                return self._error_status(inst, f"invalid {inst.connection_method} settings: {e}")

            # SSM needs the local session-manager-plugin; fail with an actionable
            # message rather than letting the child exit with a cryptic error.
            if params.method == "ssm":
                if not cloud_ssm.session_manager_plugin_installed():
                    return self._error_status(inst, cloud_ssm.session_manager_plugin_install_hint())

            # Mirror the local forward port to the remote (configured)
            # port. The embedded dashboard runs in an iframe at
            # http://127.0.0.1:<local_port>, and the remote gateway only trusts
            # CSRF/WebSocket Origins on its own configured port. Forcing
            # local_port == remote_port keeps the Origin valid without per-instance
            # allow-listing. Each simultaneously-connected instance must therefore
            # use a distinct remote port (a local port cannot be bound twice).
            local_port = inst.remote_port

            # Clear any orphaned forwarder still holding this port from an
            # unclean prior exit (hard kill bypasses graceful shutdown; macOS has no
            # parent-death signal) so the new tunnel can bind it.
            if self._reaps_orphans:
                await self._reap_orphan_forwarder(local_port)

            # Hard-fail with a clear message if the mirrored port is still occupied
            # (e.g. another instance on the same remote port, or the local gateway).
            # No dynamic fallback — a different local port would break the
            # origin match and leave the embedded dashboard unable to stream/act.
            if not _is_port_free(local_port):
                return self._error_status(
                    inst,
                    f"local port {local_port} is already in use. Each connected "
                    f"instance must use a distinct remote port — change this "
                    f"instance's remote port (and set that same port on the remote "
                    f"host's dashboard.url), or disconnect whatever is holding "
                    f"port {local_port}.",
                )

            # Open the tunnel first so the forward is live.
            tunnel = self._tunnel_factory(
                inst.id,
                params.ssh_host,
                local_port,
                inst.remote_port,
                connect_timeout_secs=self._connect_timeout_for(params.method),
                compression=self._ssh_compression,
                probe_failure_threshold=self._probe_fails,
                on_exit=self._on_tunnel_exit,
                **params.tunnel_kwargs(),
            )
            self._tunnels[instance_id] = tunnel
            ok = await tunnel.start()
            if not ok:
                self._last_error[instance_id] = tunnel.status.error or "tunnel failed to start"
                # Drop the failed tunnel (matching the mint-failure path below) so
                # status() returns None and _status_for surfaces the error via the
                # last_error() fallback, rather than leaving a stale ERROR tunnel
                # lingering in _tunnels (its process never started, so _on_tunnel_exit
                # never fires to clean it up).
                self._tunnels.pop(instance_id, None)
                return tunnel.status

            # Mint a per-instance token over the same transport (never logged).
            try:
                token = await self._mint_for(inst, params)
            except TokenMintError as e:
                await tunnel.stop()
                self._tunnels.pop(instance_id, None)
                return self._error_status(inst, f"token mint failed: {e}")
            self._store_token(instance_id, token, inst.ttl)
            self._schedule_token_refresh(instance_id)

            # Persist hints: port assignment, was_connected, last-active.
            with contextlib.suppress(Exception):
                self._registry.update(instance_id, local_port=local_port, was_connected=True)
                self._registry.set_last_active(instance_id)
            # A successful (re)connect clears any stale give-up counter so the next
            # unexpected drop gets a full fresh recovery budget instead of tripping
            # the cap immediately.
            self._recover_attempts.pop(instance_id, None)
            # Connected cleanly — drop any retained failure reason from a prior
            # attempt so status() no longer reports a stale error.
            self._last_error.pop(instance_id, None)
            return tunnel.status

    async def disconnect(self, instance_id: str) -> bool:
        """Tear down *instance_id*'s tunnel, drop its token, clear its port hint.

        Returns whether a live tunnel existed.

        The persisted ``local_port`` is reset to the unallocated sentinel here —
        symmetric with :meth:`connect` setting it — so a disconnected instance
        never leaves a stale port recorded. Without this the freed port reads as
        perpetually reserved (``_reserved_ports`` / the ``local_port == 0``
        "unallocated" contract), and the instance can't be reconnected. The
        registry cleanup runs even when no live tunnel is tracked, so a port left
        behind by an unclean prior exit can still be cleared by a disconnect.
        """
        async with self._lock:
            tunnel = self._tunnels.pop(instance_id, None)
            self._tokens.pop(instance_id, None)
            self._recover_attempts.pop(instance_id, None)
            self._last_error.pop(instance_id, None)
            self._cancel_token_refresh(instance_id)
            if tunnel is not None:
                await tunnel.stop()
            # Clear the lazy-reconnect hint AND the recorded local port together
            # (one atomic write). local_port must return to the unallocated
            # sentinel so the now-free port is not treated as reserved forever.
            with contextlib.suppress(Exception):
                self._registry.update(
                    instance_id,
                    was_connected=False,
                    local_port=_UNALLOCATED_PORT,
                )
            return tunnel is not None

    async def shutdown(self) -> None:
        """Tear down all tunnels (gateway shutdown). Leaves registry hints intact
        so lazy reconnect can revive the last-active instance next startup."""
        async with self._lock:
            # Cancel any in-flight self-heal so it can't resurrect a tunnel
            # after shutdown.
            for task in list(self._recovery_tasks):
                if not task.done():
                    task.cancel()
            self._recover_attempts.clear()
            for instance_id in list(self._refresh_tasks):
                self._cancel_token_refresh(instance_id)
            ids = list(self._tunnels)
            for instance_id in ids:
                tunnel = self._tunnels.pop(instance_id, None)
                self._tokens.pop(instance_id, None)
                if tunnel is not None:
                    with contextlib.suppress(Exception):
                        await tunnel.stop()
            logger.info("All instance tunnels shut down (%d)", len(ids))

    # ── self-heal ─────────────────────────────────────────────────────────

    def _on_tunnel_exit(self, instance_id: str) -> None:
        """Sync seam invoked by a tunnel's monitor on unexpected exit.

        Schedules the async 2-tier recovery as a tracked task (refs retained so
        it isn't GC'd mid-flight; exceptions logged). A backoff (scaled by the
        consecutive-attempt count) is applied here, in the scheduling seam, so a
        flapping link / bind race can't spin a tight respawn loop — and so direct
        ``_recover`` callers (unit tests) aren't slowed.
        """
        delay = _recover_backoff_secs(
            self._recover_attempts.get(instance_id, 0) + 1, self._recover_backoff_max
        )
        task = asyncio.create_task(self._recover_after(instance_id, delay))
        self._recovery_tasks.add(task)
        task.add_done_callback(self._recovery_tasks.discard)
        task.add_done_callback(
            lambda t: (
                logger.error("Self-heal task crashed: %s", t.exception())
                if not t.cancelled() and t.exception()
                else None
            )
        )

    async def _recover_after(self, instance_id: str, delay: float) -> None:
        """Sleep *delay* (backoff) then run the 2-tier self-heal."""
        if delay > 0:
            await asyncio.sleep(delay)
        await self._recover(instance_id)

    async def _rebuild(self, inst: Instance, params: _TransportParams, local_port: int) -> bool:
        """Build + start a fresh tunnel for *inst*, replacing the live one.

        Stops the existing tunnel first so its child is terminated and the
        local forward port is released before we spawn the replacement. Without
        this the old child orphans (dropped from ``_tunnels`` but never killed)
        and keeps holding the port, so every replacement fails
        ``ExitOnForwardFailure`` while ``_port_reachable`` is still satisfied by
        the orphan — the tight respawn loop this method otherwise produced.
        """
        old = self._tunnels.get(inst.id)
        if old is not None:
            with contextlib.suppress(Exception):
                await old.stop()
        tunnel = self._tunnel_factory(
            inst.id,
            params.ssh_host,
            local_port,
            inst.remote_port,
            connect_timeout_secs=self._connect_timeout_for(params.method),
            compression=self._ssh_compression,
            probe_failure_threshold=self._probe_fails,
            on_exit=self._on_tunnel_exit,
            **params.tunnel_kwargs(),
        )
        self._tunnels[inst.id] = tunnel
        return await tunnel.start()

    async def _mark_recovered(self, instance_id: str) -> None:
        """Reset the attempt counter (under lock, iff still tracked) + persist."""
        async with self._lock:
            if instance_id in self._tunnels:
                self._recover_attempts[instance_id] = 0
        with contextlib.suppress(Exception):
            self._registry.set_was_connected(instance_id, True)

    async def _recover(self, instance_id: str) -> None:
        """2-tier self-heal for an unhealthy tunnel (either transport).

        Tier 1: rebuild the tunnel (reusing the existing token).
        Tier 2: if rebuild fails, re-mint the token over the instance's
        transport, then rebuild.
        Capped at ``_MAX_RECOVERY`` consecutive attempts (reset on success) so a
        persistently-broken host can't churn forever. No-ops if the instance was
        disconnected/removed or has already recovered while we waited for the lock.

        The slow remote I/O (mint; rebuild) runs **without** the manager lock —
        mirroring ``_refresh_token_once`` — so self-heal can't stall concurrent
        connect/disconnect/shutdown. The lock is held only for the
        validation/state checks and to store a freshly minted token.
        """
        # Phase 1 — validate + bump the attempt counter under the lock, then release.
        async with self._lock:
            inst = self._registry.get(instance_id)
            current = self._tunnels.get(instance_id)
            if inst is None or current is None:
                return  # disconnected / removed while we waited
            if current.status.state == TunnelState.CONNECTED:
                self._recover_attempts.pop(instance_id, None)
                return  # already healthy (e.g. user reconnected)

            attempts = self._recover_attempts.get(instance_id, 0) + 1
            self._recover_attempts[instance_id] = attempts
            if attempts > self._max_recovery:
                logger.error(
                    "Giving up self-heal for %s after %d attempts", instance_id, self._max_recovery
                )
                self._schedule_diagnosis(instance_id)
                return

            try:
                params = self._resolve_transport(inst)
            except (SshValidationError, SsmValidationError) as e:
                logger.warning("Self-heal aborted for %s: %s", instance_id, e)
                return

            local_port = current.status.local_port or inst.local_port

        # Phase 2 — slow remote I/O WITHOUT the lock.
        # Tier 1 — rebuild tunnel, reuse existing token.
        logger.info("Self-heal tier 1 (rebuild tunnel) for %s [attempt %d]", instance_id, attempts)
        if await self._rebuild(inst, params, local_port):
            await self._mark_recovered(instance_id)
            logger.info("Self-heal tier 1 succeeded for %s", instance_id)
            return

        # Tier 2 — re-mint the dashboard token, then rebuild.
        logger.info("Self-heal tier 2 (re-mint token) for %s", instance_id)
        try:
            token = await self._mint_for(inst, params)
        except TokenMintError as e:
            logger.warning("Self-heal re-mint failed for %s: %s", instance_id, e)
            return
        async with self._lock:
            if instance_id not in self._tunnels:
                return  # disconnected while minting — discard
            self._store_token(instance_id, token, inst.ttl)
            self._schedule_token_refresh(instance_id)
        if await self._rebuild(inst, params, local_port):
            await self._mark_recovered(instance_id)
            logger.info("Self-heal tier 2 succeeded for %s", instance_id)
        else:
            logger.warning("Self-heal failed for %s even after re-mint", instance_id)

    def status(self, instance_id: str) -> TunnelStatus | None:
        """Return the live tunnel status for *instance_id*, or None if not live."""
        tunnel = self._tunnels.get(instance_id)
        return tunnel.status if tunnel is not None else None

    def last_error(self, instance_id: str) -> str | None:
        """Return the retained connect/reconnect failure reason, or None.

        Set by the connect path when an attempt fails (validation, port
        conflict, tunnel spawn, or token mint) and the failed tunnel is not
        retained as a live ERROR status; cleared on a successful connect or an
        explicit disconnect. Lets a sticky tab whose tunnel is down report *why*
        even though there is no live tunnel object to query.
        """
        return self._last_error.get(instance_id)

    def status_all(self) -> dict[str, TunnelStatus]:
        """Return live tunnel statuses keyed by instance id."""
        return {iid: t.status for iid, t in self._tunnels.items()}

    async def diagnose(self, instance_id: str) -> dict | None:
        """Run the failure-diagnosis ladder for *instance_id*.

        Read-only ordered probes (transport reachability → remote dashboard →
        local forward); the first broken link is the diagnosis. Result is stored
        on the live tunnel's status so it surfaces in ``status()``/``to_dict()``.
        Runs WITHOUT the manager lock (the probes do network I/O). Returns the
        result dict, or None for an unknown instance.
        """
        inst = self._registry.get(instance_id)
        if inst is None:
            return None
        tunnel = self._tunnels.get(instance_id)
        local_port = (tunnel.status.local_port if tunnel else 0) or inst.local_port
        if (inst.connection_method or "ssh").strip().lower() == "ssm":
            result = await diagnose_instance_ssm(
                inst.ssm_target,
                inst.remote_port,
                local_port,
                aws_profile=inst.aws_profile,
                aws_region=inst.aws_region,
                ssm_run_as=inst.ssm_run_as,
            )
        else:
            result = await diagnose_instance(inst.ssh_host, inst.remote_port, local_port)
        diag = result.to_dict()
        # Re-fetch the tunnel (it may have changed during the probes) and attach.
        tunnel = self._tunnels.get(instance_id)
        if tunnel is not None:
            tunnel.status.diagnosis = diag
        logger.info("Instance %s diagnosis: %s", instance_id, diag.get("code"))
        return diag

    async def restart_remote(self, instance_id: str) -> dict:
        """Restart the remote Kiro Crew gateway over the instance's transport.

        Uses the remote ``kirocrew restart`` (itself systemd/launchd-aware),
        resolved via the run-marker first (the running gateway's own launcher,
        keyed by ``remote_port``) and falling back to the bin-candidate ladder —
        so restart works even when ``~/.local/bin/kirocrew`` points at an
        uninstalled worktree. Validates the transport params first. After a
        restart the remote dashboard port bounces, so the local tunnel's health
        probe detects the drop and self-heals (Stage 2) — no manual reconnect
        needed. Returns ``{ok, message}``.
        """
        inst = self._registry.get(instance_id)
        if inst is None:
            return {"ok": False, "message": "unknown instance"}
        try:
            params = self._resolve_transport(inst)
        except (SshValidationError, SsmValidationError) as e:
            return {"ok": False, "message": f"invalid {inst.connection_method} settings: {e}"}
        if params.method == "ssm":
            rc, err = await run_remote_kirocrew_ssm(
                params.ssm_target,
                "restart",
                aws_profile=params.aws_profile,
                aws_region=params.aws_region,
                ssm_run_as=params.ssm_run_as,
                remote_bin=params.remote_bin,
                marker_port=inst.remote_port,
            )
        else:
            rc, err = await run_remote_kirocrew(
                params.ssh_host,
                "restart",
                remote_bin=params.remote_bin,
                marker_port=inst.remote_port,
            )
        if rc == 0:
            logger.info("Restarted remote gateway for %s", instance_id)
            return {"ok": True, "message": "remote gateway restart requested"}
        logger.warning("Remote restart for %s failed (rc=%s): %s", instance_id, rc, err)
        return {"ok": False, "message": err or f"restart exited {rc}"}

    def _schedule_diagnosis(self, instance_id: str) -> None:
        """Fire-and-forget a diagnosis run (tracked so it isn't GC'd)."""
        task = asyncio.create_task(self.diagnose(instance_id))
        self._recovery_tasks.add(task)
        task.add_done_callback(self._recovery_tasks.discard)
        task.add_done_callback(
            lambda t: (
                logger.error("Diagnosis task crashed for %s: %s", instance_id, t.exception())
                if not t.cancelled() and t.exception()
                else None
            )
        )

    def get_token(self, instance_id: str) -> str:
        """Return the in-memory token for a connected instance, or ``""``.

        Callers must not log the result. Exists so the API layer can hand the
        token to the browser for the embedded iframe's first-party cookie.
        """
        return self._tokens.get(instance_id, "")

    async def token_validates(self, local_port: int, token: str) -> bool:
        """Probe whether *token* still authenticates against the live tunnel.

        A cheap loopback ``GET http://127.0.0.1:<local_port>/api/status?token=…``
        through the already-open SSH forward — **no SSH spawn**. Lets the API
        layer validate a *stored* token before handing it to the browser on
        (re)connect: a token can go stale while the tunnel stays CONNECTED (a
        failed self-heal re-mint, or a remote ``kirocrew restart`` that
        invalidates tokens), and an iframe loaded with a stale token gets a
        server-rendered 403 page — the SPA never boots, so the reactive
        ``mc-auth-expired`` recovery can't fire. This closes that initial-load
        gap by catching the bad token *before* the iframe loads.

        Returns ``True`` only on a positive ``2xx`` that confirms the token is
        accepted. Returns ``False`` on 401/403, a missing token, an unknown
        port, **and** on any timeout / connection error — an unconfirmed token
        is never treated as valid (authorization must be positively confirmed,
        deny-by-default). The caller recovers by forcing a fresh mint
        (``refresh_token``); a genuinely unreachable link will fail that mint too
        and the caller surfaces a clean error rather than serving a token it
        could not confirm. The token is sent only over loopback→SSH
        (encrypted)→remote loopback and is never logged.
        """
        if not token or local_port <= 0:
            return False
        url = f"http://{_LOOPBACK}:{int(local_port)}/api/status"
        timeout = aiohttp.ClientTimeout(total=_TOKEN_PROBE_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params={"token": token}) as resp:
                    # Positive confirmation only: 2xx == token accepted.
                    return 200 <= resp.status < 300
        except Exception as e:  # timeout, connection refused, etc.
            # Deny-by-default: we could not positively confirm the token.
            logger.info(
                "Token liveness probe on port %s inconclusive (%s); treating as invalid",
                local_port,
                type(e).__name__,  # never the token
            )
            return False

    def token_ttl_remaining(self, instance_id: str) -> int | None:
        """Seconds until the current token reaches its TTL, or None if unknown.

        Used by the Manage panel (Stage 6) to show "token TTL remaining".
        """
        minted = self._token_minted_at.get(instance_id)
        ttl = self._token_ttl_secs.get(instance_id)
        if minted is None or ttl is None:
            return None
        return max(0, int(ttl - (time.time() - minted)))

    # ── proactive token refresh ────────────────────────────────────────────

    def _store_token(self, instance_id: str, token: str, ttl: str) -> None:
        """Record a freshly-minted token + its mint time/ttl (never logs token)."""
        self._tokens[instance_id] = token
        self._token_minted_at[instance_id] = time.time()
        with contextlib.suppress(Exception):
            self._token_ttl_secs[instance_id] = ttl_to_seconds(ttl)

    def _cancel_token_refresh(self, instance_id: str) -> None:
        """Cancel + drop an instance's refresh task and token metadata."""
        task = self._refresh_tasks.pop(instance_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._token_minted_at.pop(instance_id, None)
        self._token_ttl_secs.pop(instance_id, None)

    def _schedule_token_refresh(self, instance_id: str) -> None:
        """(Re)start the proactive refresh loop for *instance_id*."""
        existing = self._refresh_tasks.get(instance_id)
        if existing is not None and not existing.done():
            existing.cancel()
        task = asyncio.create_task(self._token_refresh_loop(instance_id))
        self._refresh_tasks[instance_id] = task
        task.add_done_callback(
            # False positive (below): only the instance id + exception are logged,
            # never the token. The message contains the word "Token" (the task's
            # name), which trips the heuristic; this module never logs token values
            # (a documented invariant — mint/refresh keep tokens off stderr/logs).
            lambda t: (
                # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
                logger.error("Token refresh task crashed for %s: %s", instance_id, t.exception())
                if not t.cancelled() and t.exception()
                else None
            )
        )

    async def _token_refresh_loop(self, instance_id: str) -> None:
        """Sleep to ~80% of TTL, re-mint, repeat — until cancelled.

        Keeps the in-memory token valid ahead of the 20h cap so reconnects /
        fresh iframe loads always have a usable token. Re-mint failures are
        logged and retried on the next cycle (self-heal also covers tunnel-side
        breakage). Cancelled by disconnect/shutdown.
        """
        ttl_secs = self._token_ttl_secs.get(instance_id)
        if not ttl_secs:
            return
        delay = max(1.0, ttl_secs * _REFRESH_FRACTION)
        try:
            while True:
                await asyncio.sleep(delay)
                refreshed = await self._refresh_token_once(instance_id)
                if not refreshed:
                    # instance gone, or transient mint failure — recompute delay
                    # from the (possibly unchanged) ttl and try again next cycle.
                    if instance_id not in self._tunnels:
                        return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let the refresh loop crash silently
            logger.exception("Token refresh loop crashed for %s: %s", instance_id, exc)

    async def _refresh_token_once(self, instance_id: str) -> bool:
        """Re-mint the token once. Returns True on success.

        The remote mint runs WITHOUT holding the manager lock (so a slow mint
        can't block connect/disconnect); the result is stored under the lock only
        if the instance is still connected (guards a disconnect mid-mint). Uses
        whichever transport the instance is configured for.
        """
        inst = self._registry.get(instance_id)
        if inst is None or instance_id not in self._tunnels:
            return False
        try:
            params = self._resolve_transport(inst)
        except (SshValidationError, SsmValidationError) as e:
            logger.warning("Token refresh aborted for %s: %s", instance_id, e)
            return False
        try:
            token = await self._mint_for(inst, params)
        except TokenMintError as e:
            logger.warning("Proactive token refresh failed for %s: %s", instance_id, e)
            return False
        async with self._lock:
            if instance_id not in self._tunnels:
                return False  # disconnected while minting — discard
            self._store_token(instance_id, token, inst.ttl)
        logger.info("Proactively refreshed token for %s", instance_id)  # no token in logs
        return True

    async def refresh_token(self, instance_id: str) -> str | None:
        """Force a fresh token mint for a connected instance and return it.

        Drives the owner's client-side refresh loop: re-mints over SSH, stores
        the new token, and returns it so the browser can reload the embedded
        iframe with a valid token — either proactively (before the TTL cap) or
        reactively (the embedded dashboard reported an expired session). Returns
        ``None`` if the instance isn't connected or the mint failed. The token
        is never logged.
        """
        if not await self._refresh_token_once(instance_id):
            return None
        return self.get_token(instance_id) or None

    def _error_status(self, inst: Instance, message: str) -> TunnelStatus:
        """Build (and remember) an ERROR status for *inst* without a live tunnel.

        The message is retained in ``_last_error`` so a later :meth:`status`
        lookup — after the failed-connect tunnel has been popped — can still
        report *why* the instance is down. This is what lets a sticky tab whose
        tunnel never came up show its error instead of a bare "disconnected".
        """
        logger.warning("Instance %s connect error: %s", inst.id, message)
        self._last_error[inst.id] = message
        return TunnelStatus(
            instance_id=inst.id,
            state=TunnelState.ERROR,
            local_port=inst.local_port,
            remote_port=inst.remote_port,
            error=message,
        )
