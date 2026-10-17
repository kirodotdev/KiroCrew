"""Supervising one forwarder child, for ``kiro_crew.instances.ssh_tunnel_manager``.

``_SshTunnel`` spawns the ``ssh`` or ``session-manager-plugin`` child, waits for its
forward to accept, probes it end to end, drains its stdout and stderr, composes the
error its exit means, and stops it (the SSM process group included).
``TunnelState`` and ``TunnelStatus`` are what it reports.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import enum
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp

from kiro_crew import platform_compat
from kiro_crew.cloud.connect import FARGATE_HEALTH_PATH
from kiro_crew.deploy.engine import tool_spawn_env
from kiro_crew.instances.constants import (
    DEFAULT_CONNECT_TIMEOUT_SECS as _DEFAULT_CONNECT_TIMEOUT_SECS,
)
from kiro_crew.instances.constants import DEFAULT_PROBE_FAILURE_THRESHOLD as _PROBE_FAILS
from kiro_crew.instances.constants import DEFAULT_PROBE_HEALTH_TIMEOUT_SECS as _PROBE_HEALTH_TIMEOUT
from kiro_crew.instances.constants import DEFAULT_PROBE_INTERVAL_SECS as _PROBE_INTERVAL
from kiro_crew.instances.ssh_tunnel_manager_child_text import (
    _FIRST_CONNECT_DROP_HINT,
    _LOOPBACK,
    _SSH_AUTH_SIGNALS,
    _SSH_BIND_SIGNALS,
    _SSH_TRANSPORT_DROP_SIGNALS,
    _SSM_BIND_SIGNALS,
    _SSM_CREDENTIAL_SIGNALS,
    _SSM_DENIAL_SIGNALS,
    _SSM_PLUGIN_SIGNALS,
    _SSM_TARGET_SIGNALS,
    _build_ssh_tunnel_argv,
    _build_ssm_tunnel_argv,
    _first_hit,
    _sanitize_banner,
    _ssm_close_reason,
    _strip_benign_ssh_noise,
)
from kiro_crew.instances.token_mint import (
    PROXY_TOOL_MISSING_SIGNALS,
    proxy_tool_missing_message,
    ssh_spawn_argv_env,
)

#: The manager's own logger: this code logs under the facade module's name.
logger = logging.getLogger("kiro_crew.instances.ssh_tunnel_manager")


#: Stated-unsupported reason for the SSH tunnel transport on native Windows.
#: The surface is documented "not yet" supported there (``docs/guides/
#: windows-install.md``) and the argv builder notes it in a comment, but without
#: a refusal at the point of use the ``ssh -N -L`` child is spawned anyway and a
#: Windows host without ``ssh`` on PATH fails with a generic spawn error that
#: reads as "ssh is missing" rather than "unsupported here". Refusing before the
#: spawn with this reason mirrors the native-Windows refusal in
#: ``apps/lifecycle_scripts.py``. The SSM/cloud transport is unaffected.
_WINDOWS_SSH_TUNNEL_UNSUPPORTED = "SSH tunnel is not yet supported on Windows"


# Poll cadence while waiting for the forward to come up.
_READY_POLL_INTERVAL_SECS = 0.25
# Bound on retained stderr so a chatty/looping ssh can't grow memory unbounded.
_MAX_STDERR_CHARS = 2000
#: How long an exit path waits for the stderr drain to reach EOF. A grandchild
#: that inherited the pipe can hold it open after the child exits, so the wait
#: is bounded; what the drain captured is kept either way.
_STDERR_DRAIN_GRACE_SECS = 2.0

# Bound on retained SSM stdout, at parity with the stderr cap. The SSM child's
# stdout is drained concurrently for the whole session (see _drain_stdout), so
# this is what keeps a service that streams into that pipe from growing memory.
_MAX_STDOUT_CHARS = 2000

# Chunk size for the concurrent stdout drain. Small enough that a close notice
# lands promptly, large enough that a quiet session costs one pending read.
_STDOUT_READ_CHUNK = 4096


#: Every way the tree-kill shim can fail to deliver: an already-reaped tree, a
#: refused broadcast pgid, a protected Windows descendant, a permission error.
#: Each means "not delivered", so the caller falls back to the single-process kill.
_TREE_KILL_UNDELIVERED = (ProcessLookupError, PermissionError, OSError, ValueError, AttributeError)


class TunnelState(enum.Enum):
    """Per-instance tunnel states."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    STOPPED = "stopped"


# Named by the path callers import it from: a traceback and an error chain render a
# class as ``module.qualname``, so this module's name stays out of that text.
TunnelState.__module__ = "kiro_crew.instances.ssh_tunnel_manager"


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
    # Fargate only: the local URL of the crew's turn API, what the card shows in
    # place of a dashboard. Empty for the dashboard-bearing methods.
    turn_url: str = ""
    # The identity the tunnel was ACTUALLY dialled with, carried so a caller can
    # bind a credential to the live transport rather than to a registry row that
    # was editable after the forward was built. ``transport`` is the dialled
    # ``connection_method`` ("ssh"/"ssm"); ``ssm_target``/``aws_profile``/
    # ``aws_region`` are the SSM coordinates the child was spawned on (empty for
    # an SSH forward). These never change for the life of a tunnel: a row edited
    # after connect does not rebuild the child, so these keep naming the node and
    # account the port actually reaches.
    transport: str = ""
    ssm_target: str = ""
    aws_profile: str = ""
    aws_region: str = ""

    def to_dict(self) -> dict:
        d: dict[str, object] = {
            "instance_id": self.instance_id,
            "state": self.state.value,
            "local_port": self.local_port,
            "remote_port": self.remote_port,
            "error": self.error,
            "connected_at": self.connected_at,
            # The dialled identity, always present so a reader never has to tell
            # "not an ssm tunnel" apart from "field absent". Empty strings on an
            # SSH forward, which is exactly what a mismatch check wants to see.
            "transport": self.transport,
            "ssm_target": self.ssm_target,
            "aws_profile": self.aws_profile,
            "aws_region": self.aws_region,
        }
        if self.diagnosis is not None:
            d["diagnosis"] = self.diagnosis
        if self.turn_url:
            d["turn_url"] = self.turn_url
        return d


TunnelStatus.__module__ = "kiro_crew.instances.ssh_tunnel_manager"


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
        # Optional seam, assigned by the manager after construction (never a
        # constructor kwarg, so a tunnel double need not accept it): called
        # (instance_id) on a proven end-to-end probe success so the manager can
        # clear the self-heal attempt counter once the forward is verified live.
        self._on_healthy: Callable[[str], None] | None = None
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
        # SSM only: the child's close notices go to stdout, so that pipe is
        # drained concurrently for the whole session by _stdout_task rather than
        # read once after exit. A second pipe read only at exit would deadlock a
        # child that fills the OS buffer while still running.
        self._stdout_buf = ""
        self._stdout_task: asyncio.Task | None = None  # type: ignore[type-arg]
        # Both transports: stderr is drained concurrently from spawn by
        # _stderr_task too. ssh writes a line there for every forwarded connection
        # the far end refuses, so a long-lived tunnel fills the OS buffer.
        self._stderr_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._child_path = ""  # the PATH the last spawned child was given
        self.status = TunnelStatus(
            instance_id=instance_id,
            local_port=local_port,
            remote_port=remote_port,
            # The identity the child is dialled with, frozen here at construction.
            # A later edit to the registry row does not reach this object, so a
            # caller comparing against it sees the node and account the port
            # actually reaches. SSM coordinates are empty on an SSH forward.
            transport=transport,
            ssm_target=ssm_target if transport == "ssm" else "",
            aws_profile=aws_profile if transport == "ssm" else "",
            aws_region=aws_region if transport == "ssm" else "",
        )

    @property
    def transport(self) -> str:
        """The child's transport, ``"ssh"`` or ``"ssm"``."""
        return self._transport

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
        argv, _env = ssh_spawn_argv_env(
            _build_ssh_tunnel_argv(
                self._ssh_host, self._local_port, self._remote_port, compression=self._compression
            )
        )
        return argv

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
        # Built in a worker thread: both branches resolve their argv head
        # absolutely (aws: PATH scan + well-known install dirs; ssh: PATH scan),
        # which probes the filesystem — synchronous work that must not run on the
        # gateway event loop, where a stalled network mount on PATH would
        # freeze every request and heartbeat.
        argv = await asyncio.to_thread(self._build_argv)
        target = self._ssm_target if self._transport == "ssm" else self._ssh_host
        logger.info(
            "Opening %s tunnel for %s: 127.0.0.1:%d -> %s:%d",
            self._transport,
            self._id,
            self._local_port,
            target,
            self._remote_port,
        )
        spawn_env = tool_spawn_env(argv[0])
        # Kept for the exit classifier: a ProxyCommand whose program is missing
        # is reported with the PATH this child actually searched.
        self._child_path = spawn_env.get("PATH", "")
        try:
            ssm = self._transport == "ssm"
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                # SSM only: `aws ssm start-session` prints every session-close
                # notice to STDOUT (its own diagnostics go to a rolling log
                # file), so this pipe is the ONLY place the reason a forward
                # closed can be read — see _ssm_close_reason. It is drained
                # concurrently from spawn, so a child that keeps writing cannot
                # fill the buffer and block. ssh writes nothing useful here and
                # keeps its output discarded.
                stdout=(asyncio.subprocess.PIPE if ssm else asyncio.subprocess.DEVNULL),
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
                # Both transports: the argv head is resolved absolutely, but the
                # child then looks a tool up BY NAME on its own PATH — aws finds
                # session-manager-plugin that way, and ssh runs the user's
                # ProxyCommand (an SSM connect helper, `aws ssm start-session`),
                # which does the same. A GUI-launched gateway hands down launchd's
                # minimal PATH, so the tunnel died with "session-manager-plugin is
                # not installed" while the plugin sat in /usr/local/bin. argv[0] is
                # handed over so the widening is withheld for a bare head (see
                # tool_spawn_env): for aws that bare name IS a provenance refusal,
                # for ssh it means no ssh on the inherited PATH, and either way
                # widening would put an unvetted binary within execvp's reach.
                env=spawn_env,
            )
        except OSError as e:
            self.status.state = TunnelState.ERROR
            # The ssh tunnel is documented "not yet" supported on native Windows
            # (``docs/guides/windows-install.md``): the ``ssh -N -L`` child needs
            # the OpenSSH client on PATH, which is not guaranteed there, so the
            # spawn fails with a generic OSError that reads as "ssh is missing"
            # and never says the surface is unsupported. When the spawn is the
            # one that failed on Windows, name the platform constraint instead of
            # the opaque spawn error — mirroring the native-Windows refusal in
            # ``apps/lifecycle_scripts.py``. This does NOT pre-empt a Windows host
            # whose ``ssh`` IS on PATH: that spawn succeeds and never reaches here,
            # so a tunnel that works today is untouched. SSM/POSIX are unaffected.
            if self._transport == "ssh" and platform_compat.IS_WINDOWS:
                self.status.error = f"{_WINDOWS_SSH_TUNNEL_UNSUPPORTED} ({e})"
                logger.warning(
                    "SSH tunnel spawn failed for %s on Windows: %s (%s)",
                    self._id,
                    _WINDOWS_SSH_TUNNEL_UNSUPPORTED,
                    e,
                )
                return False
            self.status.error = f"failed to spawn {self._transport} tunnel: {e}"
            logger.error("Tunnel spawn failed for %s: %s", self._id, e)
            return False

        # Started BEFORE the readiness wait, so a child that closes during
        # startup still has its reason captured: _wait_until_ready can conclude
        # via _failed_on_child_exit, which classifies on this buffer.
        if self._proc.stdout is not None:
            self._stdout_task = asyncio.create_task(self._drain_stdout())
        if self._proc.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr())

        ready = await self._wait_until_ready()
        if not ready:
            await self._terminate()
            # The child is gone, and a timed-out start composes no exit error, so
            # both drains are retired at once with what they captured.
            await self._finish_stdout_drain(timeout=0)
            await self._retire_stderr_drain(timeout=0)
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
        """Poll the forward end-to-end while CONNECTED; tear down on repeated failure.

        Sleeps ``_PROBE_INTERVAL`` between probes (interruptible by ``stop()``).
        A successful check resets the failure counter and fires ``on_healthy``
        (so the manager clears the self-heal attempt counter on proven
        end-to-end health); after ``_PROBE_FAILS`` consecutive failures the
        tunnel is treated as a zombie (alive child, no forwarding) and the child
        is terminated — the existing ``_monitor`` then fires ``on_exit`` so
        Stage 2 can rebuild/re-mint. Mirrors ``TunnelManager._probe_loop``.

        The check is :meth:`_forward_alive`, an end-to-end request through the
        forward — not :meth:`_port_reachable`. A bare TCP connect proves only
        that the local listener is bound, which a zombie forward satisfies while
        relaying nothing to the far end, so a connect-only probe can never
        observe the very stall it exists to catch.
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
                if await self._forward_alive():
                    self._probe_failures = 0
                    # A proven end-to-end round trip is the signal that clears
                    # the self-heal attempt counter — not a bind-only rebuild.
                    # This is what stops one slow probe after a rebuild from
                    # ratcheting the recovery budget down permanently.
                    if self._on_healthy is not None:
                        with contextlib.suppress(Exception):
                            self._on_healthy(self._id)
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
            if await self._failed_on_child_exit():
                return False
            if await self._port_reachable():
                # A reachable port is NOT proof THIS child bound it: a lingering
                # tunnel or orphaned ssh can answer while our child already lost
                # the bind race (ExitOnForwardFailure -> exit 255). Confirm our
                # child is still alive before declaring the tunnel ready.
                if await self._failed_on_child_exit():
                    return False
                return True
            await asyncio.sleep(_READY_POLL_INTERVAL_SECS)
        self.status.error = f"timed out after {self._connect_timeout}s waiting for forward"
        return False

    async def _failed_on_child_exit(self) -> bool:
        """Record an already-exited child as an ERROR status; True if it exited.

        ``self._proc`` is re-read on every call rather than passed in, because
        each caller looks across an await during which the child can have exited.
        Returns False when there is no child at all — a racing ``stop()`` clears
        ``self._proc`` — so that teardown is left to the readiness timeout rather
        than reported as an exit with a returncode nobody captured.
        """
        proc = self._proc
        if proc is None or proc.returncode is None:
            return False
        await self._finish_stdout_drain()
        await self._capture_stderr()
        self.status.state = TunnelState.ERROR
        self.status.error = self._exit_error(proc.returncode, first_connect=True)
        return True

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

    async def _forward_alive(self) -> bool:
        """Return True only when the far end answers through the forward.

        The steady-state health check, distinct from :meth:`_port_reachable`.
        A bare TCP connect is answered by whatever holds the local listening
        socket, which for an SSM forward is ``session-manager-plugin`` on
        loopback. When that plugin survives a dropped forward as a zombie it
        keeps the socket bound while relaying nothing, so a connect-only probe
        passes forever and the tunnel is reported CONNECTED while every request
        through it stalls — the exact failure this loop exists to catch.

        This issues a ``GET`` at the transport's own unauthenticated liveness
        path through the forward and treats ANY completed HTTP response — of any
        status — as alive. A status line is itself proof that bytes traversed to
        the far end and back, which is what a zombie forward cannot produce: it
        accepts the connect, then sends zero bytes until the budget expires. The
        status code is deliberately not inspected. The path is the far end's:
        a gateway forward answers ``/api/health`` credential-free, while a
        fargate crew's container serves its own ``FARGATE_HEALTH_PATH``
        (``turn_url`` is set only for the fargate lane, so it selects the path).
        Probing the wrong path would still prove liveness, but the fargate
        container authorises before it routes and would log a ``control`` deny
        for each probe, so the right path keeps the probe silent in its logs.
        Only a timeout or a connection error (no response at all) is a failure;
        the caller's consecutive-failure threshold keeps one slow round trip
        from tearing down a good tunnel.
        """
        if self._local_port <= 0:
            return False
        # turn_url is populated only for the fargate lane; its container serves a
        # dedicated liveness path and refuses (audited) every other one.
        health_path = FARGATE_HEALTH_PATH if self.status.turn_url else "/api/health"
        url = f"http://{_LOOPBACK}:{self._local_port}{health_path}"
        try:
            timeout = aiohttp.ClientTimeout(total=_PROBE_HEALTH_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, allow_redirects=False) as resp:
                    # Any status line is proof the far end sent bytes back. A
                    # zombie forward never reaches here; it stalls until the
                    # timeout arm below fires.
                    _ = resp.status
                    return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(
                "Tunnel forward liveness probe on port %d failed (%s)",
                self._local_port,
                type(e).__name__,
            )
            return False

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
        await self._finish_stdout_drain()
        await self._capture_stderr()
        self.status.state = TunnelState.ERROR
        self.status.error = self._exit_error(proc.returncode)
        logger.warning("Tunnel for %s exited unexpectedly: %s", self._id, self.status.error)
        if self._on_exit is not None:
            with contextlib.suppress(Exception):
                self._on_exit(self._id)

    def _exit_error(self, returncode: int | None, *, first_connect: bool = False) -> str:
        """Compose a human error from exit code + captured stderr.

        Classifies on real ssh signals, not on prose the WSSH proxy passes
        through. A genuine auth failure (permission denied / publickey /
        certificate expired) is reported as auth; a WSSH session/transport drop
        (idle timeout, banner-exchange timeout, reset, refused) is reported as a
        transport drop — never as an auth verdict inferred from banner text. The
        raw banner is ANSI-stripped and credential-redacted before it is
        surfaced as a secondary detail, with the fixed-width detail window
        centered on the matched classification phrase so preceding benign
        noise (e.g. LocalCommand output) cannot truncate the real reason away.

        The SSM transport has an entirely different error vocabulary (IAM
        denials, a missing session-manager-plugin, an offline SSM agent), so it
        is classified separately by :meth:`_ssm_exit_error` — running SSM stderr
        through the ssh matchers above would mislabel e.g. an ``AccessDenied``
        as an "ssh auth failure".

        *first_connect* is True when the child exited before the forward ever
        came up (:meth:`_failed_on_child_exit`). A transport drop there carries
        :data:`_FIRST_CONNECT_DROP_HINT`, which names ``ssh_host`` as the field
        to check; a drop from :meth:`_monitor`, on a tunnel that was up, does
        not, because that host was reachable under its name.
        """
        if self._probe_failed:
            return "health probe failed — tunnel alive but not forwarding"
        if self._transport == "ssm":
            return self._ssm_exit_error(returncode)
        # Drop ssh's benign post-quantum KEX advisory so it can't mask the real
        # failure (the loop symptom was this warning hiding "bind: ... in use").
        tail = _strip_benign_ssh_noise(self._stderr_buf)
        low = tail.lower()
        # Genuine ssh auth signals first, so a real auth failure is never masked
        # by a transport phrase that happens to co-occur in the same banner.
        hit = _first_hit(low, _SSH_AUTH_SIGNALS)
        if hit is not None:
            return f"ssh auth failed (check SSH access): {_sanitize_banner(tail, anchor=hit)}"
        # A ProxyCommand that could not find its program. Checked before the
        # transport drops: ssh follows it with "Connection closed by ..." once the
        # proxy exits, which would otherwise read as a network drop, and the
        # actionable fact is which PATH the program was missing from.
        # Gated on 255, ssh's own failure status, as the mint's twin is: the
        # wording is shell prose, and only an ssh-level failure is the proxy's.
        hit = _first_hit(low, PROXY_TOOL_MISSING_SIGNALS) if returncode == 255 else None
        if hit is not None:
            detail = _sanitize_banner(tail, anchor=hit)
            return f"{proxy_tool_missing_message(self._child_path)}: {detail}"
        # WSSH / transport session drops — not an auth problem. Worded neutrally
        # because this method is also used for the initial-connect failure path,
        # where no self-heal is armed yet (so it must not promise reconnection).
        hit = _first_hit(low, _SSH_TRANSPORT_DROP_SIGNALS)
        if hit is not None:
            detail = _sanitize_banner(tail, anchor=hit)
            if first_connect:
                return f"ssh tunnel transport drop ({_FIRST_CONNECT_DROP_HINT}): {detail}"
            return f"ssh tunnel transport drop: {detail}"
        hit = _first_hit(low, _SSH_BIND_SIGNALS)
        if hit is not None:
            detail = _sanitize_banner(tail, anchor=hit)
            return f"ssh forward bind failed (local port already in use): {detail}"
        if tail:
            return f"ssh exited {returncode}: {_sanitize_banner(tail)}"
        return f"ssh exited with code {returncode}"

    def _ssm_exit_error(self, returncode: int | None) -> str:
        """Classify an ``aws ssm start-session`` port-forward child's exit.

        Distinguishes the failure modes an operator can actually act on:
        expired/absent AWS credentials, an IAM denial on ``ssm:StartSession``,
        a missing local ``session-manager-plugin``, an instance that is not a
        registered/online SSM managed node, and a local bind conflict. Like the
        ssh classifier, the raw stderr is ANSI-stripped and credential-redacted
        before being surfaced as a secondary detail.

        Those are all stderr signals. A session AWS itself closed writes nothing
        to stderr at all and exits 0, so when no stderr signal matches this falls
        through to :meth:`_ssm_closed_error`, which reads the close notice the
        child prints to stdout. That is the only place the cause of a closed
        forward exists: the child's own diagnostics go to a rolling log file, and
        the exit status is 0 for a clean close, a transport drop and a failed
        start alike.
        """
        tail = self._stderr_buf.strip()
        low = tail.lower()
        # Credentials first: an expired/absent credential is the most common
        # cause and its message can also contain "not authorized"-adjacent text.
        hit = _first_hit(low, _SSM_CREDENTIAL_SIGNALS)
        if hit is not None:
            return (
                "AWS credentials missing or expired (refresh them, e.g. "
                f"`aws sso login --profile <name>`): {_sanitize_banner(tail, anchor=hit)}"
            )
        hit = _first_hit(low, _SSM_DENIAL_SIGNALS)
        if hit is not None:
            detail = _sanitize_banner(tail, anchor=hit)
            return f"IAM denied ssm:StartSession for this target: {detail}"
        hit = _first_hit(low, _SSM_PLUGIN_SIGNALS)
        if hit is not None:
            return (
                "session-manager-plugin is not installed locally (install the AWS "
                f"Session Manager plugin, then reconnect): {_sanitize_banner(tail, anchor=hit)}"
            )
        hit = _first_hit(low, _SSM_TARGET_SIGNALS)
        if hit is not None:
            return (
                "the SSM target is not a connected managed node (is the instance "
                f"running with the SSM agent online and an instance profile?): "
                f"{_sanitize_banner(tail, anchor=hit)}"
            )
        hit = _first_hit(low, _SSM_BIND_SIGNALS)
        if hit is not None:
            detail = _sanitize_banner(tail, anchor=hit)
            return f"SSM forward bind failed (local port already in use): {detail}"
        # Nothing actionable on stderr. The child prints WHY a session closed to
        # stdout, so consult that next — it outranks unclassified stderr noise
        # because a close notice states the cause and the noise does not. Only
        # `kind` is used: the service-supplied reason text stays a classification
        # signal and is never surfaced or logged.
        closed = self._ssm_closed_error()
        if closed:
            return closed
        if tail:
            return f"SSM session exited {returncode}: {_sanitize_banner(tail)}"
        return f"SSM session exited with code {returncode}"

    def _ssm_closed_error(self) -> str:
        """Message for a recognised session-close shape, ``""`` when none matched.

        Every branch is worded against what the code can actually establish:

        * No duration. The ECS Exec documentation calls its idle limit fixed
          while the Session Manager preferences documentation describes an
          adjustable one, and neither settles which governs a port forward at an
          ``ecs:`` target — so the preference is named and no number is.
        * No promise that traffic holds the session open. The documented activity
          list is terminal-centric, and a client holding a forward open while
          making no requests can still idle out.
        * No offer to reconnect. Re-opening is a human action (the cloud lane's
          opener carries :func:`~kiro_crew.cloud.aws.assert_human_action`), and a
          fargate crew deliberately gets no auto-connect at all.
        * Only a real action. A fargate crew has no dashboard pane and no CLI
          connect verb; its card in Settings is what reaches it.
        """
        kind = _ssm_close_reason(self._stdout_buf)
        port = self._local_port
        if kind == "idle":
            return (
                f"the SSM session on local port {port} was closed by AWS after a period "
                "with no activity. Open a new forward with Connect on the crew's card in "
                "Settings. To allow longer idle periods, raise the Session Manager "
                "idle-timeout preference for this account and region "
                "(Systems Manager > Session Manager > Preferences)."
            )
        if kind == "closed":
            # AWS ended it and either gave no reason or gave one this code does
            # not recognise. Say only that, rather than guessing at idleness.
            return (
                f"AWS ended the SSM session on local port {port}. Open a new forward "
                "with Connect on the crew's card in Settings."
            )
        if kind == "resume_timeout":
            return (
                f"the SSM session on local port {port} was lost and AWS ended it before "
                "it could be resumed. Open a new forward with Connect on the crew's "
                "card in Settings."
            )
        if kind == "start_failed":
            return (
                f"the SSM session for local port {port} never opened: the session-manager "
                "plugin reported a start-session failure. Run Diagnose on the crew's "
                "card in Settings for the cause."
            )
        return ""

    async def _drain_stdout(self) -> None:
        """Continuously drain the SSM child's stdout into a bounded buffer.

        Runs for the child's whole life rather than reading once at exit, for two
        reasons. A pipe nobody reads blocks the writer once the OS buffer fills,
        which would hang the tunnel itself; and the notice this buffer exists to
        capture is written immediately before the child exits, so a reader that
        starts at exit can race the pipe's teardown.

        The buffer keeps the most recent ``_MAX_STDOUT_CHARS`` and nothing else,
        so a service that streams into this pipe cannot grow memory. Decoding is
        lossy-by-design (``"replace"``): a split multi-byte sequence must not
        raise in a background task whose failure nobody observes. Sanitizing is
        deliberately NOT done here but at read time, because a credential split
        across two reads would survive a per-chunk pass.
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        with contextlib.suppress(Exception):
            while True:
                data = await stream.read(_STDOUT_READ_CHUNK)
                if not data:
                    return
                self._stdout_buf = (self._stdout_buf + data.decode("utf-8", "replace"))[
                    -_MAX_STDOUT_CHARS:
                ]

    async def _finish_stdout_drain(self, *, timeout: float = 2.0) -> None:
        """Let the stdout drain reach EOF, then retire it.

        Called on every exit path BEFORE the error is composed. The drain is
        concurrent, so a close notice the child wrote immediately before exiting
        can still be unread when ``proc.wait()`` returns; awaiting the task lets
        it observe EOF and land that notice first. Bounded, then cancelled: a
        surviving grandchild can hold the write end open, and teardown must not
        wait on it. Whatever was captured is kept either way.
        """
        task = self._stdout_task
        self._stdout_task = None
        await self._retire_drain(task, timeout)

    @staticmethod
    async def _retire_drain(task: asyncio.Task | None, timeout: float) -> None:  # type: ignore[type-arg]
        """Give a pipe drain up to *timeout* seconds to reach EOF, then cancel it.

        Shared by the stdout and stderr drains. A surviving grandchild can hold
        the write end open, so the wait is bounded; ``timeout=0`` cancels at once.
        What the drain captured is kept either way. The shield protects the drain
        task from the bounded wait, not the caller from cancellation: a monitor
        that ``stop()`` cancels while it waits here leaves through the
        cancellation, so a deliberate stop is never reported as an unexpected
        exit. The drain is retired on that path too.
        """
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except (asyncio.TimeoutError, Exception):
            pass
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _drain_stderr(self) -> None:
        """Continuously drain the child's stderr into a bounded buffer.

        Runs for the child's whole life, on both transports, for the reason the
        stdout drain does: a pipe read only after the child exits blocks the
        child once the OS buffer fills, and ssh writes a stderr line for every
        forwarded connection the far end refuses. A blocked ssh forwards nothing
        while the tunnel still reads CONNECTED.

        The buffer keeps the last ``_MAX_STDERR_CHARS`` of the whole stream,
        which is what the exit classifiers read. The decoder is incremental, so
        a multi-byte sequence split across two reads decodes as it does whole.
        """
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        stream = proc.stderr
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        with contextlib.suppress(Exception):
            while True:
                data = await stream.read(_STDOUT_READ_CHUNK)
                text = decoder.decode(data, final=not data)
                if text:
                    self._stderr_buf = (self._stderr_buf + text)[-_MAX_STDERR_CHARS:]
                if not data:
                    return

    async def _retire_stderr_drain(self, *, timeout: float) -> None:
        """Retire the stderr drain, keeping what it captured."""
        task = self._stderr_task
        self._stderr_task = None
        await self._retire_drain(task, timeout)

    async def _capture_stderr(self) -> None:
        """Land whatever the child wrote to stderr in ``_stderr_buf`` (bounded).

        A child :meth:`start` spawned is drained from spawn, so this lets that
        drain reach EOF, which lands the last lines the child wrote, then
        retires it. A child that runs without the drain is read to EOF here.
        """
        if self._stderr_task is not None:
            await self._retire_stderr_drain(timeout=_STDERR_DRAIN_GRACE_SECS)
            return
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
        """Tear down this tunnel (graceful terminate then kill).

        The state leaves CONNECTED before the FIRST await rather than after the last
        one. Everything that decides whether a credential may be issued for this
        forward re-reads this same live object -- the token/port pair a pane is
        answered with, and the mint paths that require CONNECTED -- and the awaits
        below span seconds (``_terminate`` waits up to 5s per signal). A teardown
        that reports CONNECTED for that span hands a caller this crew's token
        paired with a port that is already being released, and nothing revokes a
        token once the response is sent.

        Narrower than it looks, on purpose. :meth:`_reserved_ports` counts a port by
        tunnel MEMBERSHIP rather than by state and the caller pops this tunnel only
        after this returns, so the port stays withheld across the whole window.
        :meth:`_apply_hop_holds` under-counts ``in_use`` here, which is the direction
        its own contract calls cheap: the bind loses to a still-live forward and the
        port is recorded as owed and retried.

        A failed teardown restores the state, because the forward is then still up. A
        CANCELLED one does not: the child may already be signalled, and refusing to
        mint for a forward that is half-gone is the safe direction.
        """
        previous = self.status.state
        self.status.state = TunnelState.STOPPED
        self._stopping = True
        self._stop_event.set()
        try:
            if self._probe_task and not self._probe_task.done():
                self._probe_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._probe_task
            if self._monitor_task and not self._monitor_task.done():
                self._monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._monitor_task
            # Deliberate teardown: timeout=0 cancels the drain without awaiting
            # EOF (the buffer is kept). On a stop WE initiated there is no AWS
            # close cause to name, so paying the drain's timeout buys nothing.
            await self._finish_stdout_drain(timeout=0)
            await self._terminate()
            # The stderr drain runs through the terminate, so the child cannot
            # block on a full pipe while it is being stopped; a stop composes no
            # exit error, so it is then retired at once.
            await self._retire_stderr_drain(timeout=0)
        except Exception:
            self.status.state = previous
            raise
        logger.info("Tunnel stopped for %s", self._id)

    async def _terminate(self) -> None:
        """Terminate the tunnel child if running (terminate, then kill on timeout).

        For the **SSM** transport the child is the ``aws`` wrapper, and the
        ``session-manager-plugin`` grandchild is what actually holds the
        forwarded local port — ``proc.terminate()`` alone would signal only the
        wrapper and leave the plugin alive still bound to the port (the exact
        leak :func:`kiro_crew.cloud.ssm.kill_port_forward` documents). Since
        :meth:`start` spawns SSM children with process-group isolation, we reap
        the whole tree via :func:`platform_compat.kill_process_tree_async` —
        ``killpg`` on POSIX, ``taskkill /T`` off the loop on Windows, so the plugin
        is reaped on **every** supported platform without stalling the loop. A
        tree-kill failure falls back to the single-process kill.
        """
        proc = self._proc
        if proc and proc.returncode is None:
            group_signalled = False
            if self._transport == "ssm":
                group_signalled = await self._signal_group_async(proc.pid, platform_compat.SIGTERM)
            try:
                if not group_signalled:
                    proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                if self._transport == "ssm" and await self._signal_group_async(
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
        except _TREE_KILL_UNDELIVERED:
            return False

    @staticmethod
    async def _signal_group_async(pid: int, sig: int) -> bool:
        """:meth:`_signal_group` for a caller on the event loop.

        On Windows the tree kill is a ``taskkill /T`` subprocess with its own
        5 s timeout, so :meth:`_terminate` (reached from ``stop()`` under the
        manager lock, and from the pre-exec restart teardown) awaits
        :func:`platform_compat.kill_process_tree_async`, which runs it off the
        loop. Same outcome contract: never raises, ``False`` means not delivered.
        The synchronous :meth:`_signal_group` stays for the reclaim worker, which
        already runs in a thread.
        """
        try:
            return await platform_compat.kill_process_tree_async(pid, sig)
        except _TREE_KILL_UNDELIVERED:
            return False

    @property
    def pid(self) -> int | None:
        """PID of the live ssh child, or None if not running."""
        proc = self._proc
        return proc.pid if proc is not None and proc.returncode is None else None


_SshTunnel.__module__ = "kiro_crew.instances.ssh_tunnel_manager"
