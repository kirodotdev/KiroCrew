"""Inbound SSH tunnel manager for the Instances feature.

Adapts the supervised-child + state-machine design of
``kiro_crew.tunnel.manager.TunnelManager`` (which points *outward* to expose the
dashboard) to point *inward*: for each connected remote instance it supervises a
local child process that forwards a loopback port to the remote Kiro Crew's
dashboard port, over one of three transports (``Instance.connection_method``):

* ``"ssh"`` (default): ``ssh -N -L 127.0.0.1:LP:127.0.0.1:RP <ssh_host>``.
* ``"ssm"``: ``aws ssm start-session --document-name
  AWS-StartPortForwardingSession --target <ssm_target> --parameters
  portNumber=RP,localPortNumber=LP`` — no inbound SSH port or SSH key needed,
  only IAM (``ssm:StartSession``) and the SSM agent on the remote box.
* ``"loopback"``: no child PROCESS. The destination gateway already listens on
  ``127.0.0.1:RP`` on THIS host, so instead of a forwarder child the hub binds
  its own allocated port in-process and relays to that destination, gating each
  connection on the pinned-owner proof — the pane loads from the hub's port, not
  the destination's. This path is admitted only when the process carries the
  exact pod marker and the pod config enables ``instances.allow_loopback_transport``.
  The destination is fixed at ``127.0.0.1`` and no record or config field can
  redirect it.

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
implementation. The loopback transport is a third, and the one that shows what
those seams are really keyed on: the readiness wait, the health probe and the
teardown all watch a PORT, so they carry over to a transport with no process at
all. Only the two things that genuinely depend on a child — spawning it
(:meth:`_SshTunnel._spawn_child`) and reading a failure off its exit
(:meth:`_SshTunnel._fail_unhealthy`) — needed a case for it.

**Watching a port is not enough for the loopback transport.** For ssh and ssm the
credential travels into a child, and that child's exit takes the forwarded socket
with it: there is no window in which the port answers but the peer is somebody
else. The loopback transport's destination is an ordinary port on this host, so
if the gateway exits, any local process may bind the freed port and answer every
later TCP probe — which would leave the tunnel CONNECTED and hand it a reusable
~20h bearer. So the loopback transport carries an IDENTITY as well as a port: the
proven owner pid (:func:`_proven_loopback_owner`, over the address-scoped
:func:`kiro_crew.port_resolution.port_is_gateway_owned_on_loopback`) is pinned at
establishment by the readiness wait, re-proved by every health-probe cycle, and
COMPARED rather than re-adopted. A proof failure or a changed pid condemns the
tunnel one-way: sends are refused immediately and the tunnel fails toward a fresh
``connect``, never rebinding onto the new listener.

Two send classes, two freshness levels. The rare credential-ESTABLISHING send
(:meth:`SshTunnelManager.token_validates`) proves immediately before it, so the
bearer the browser receives is confirmed against a listener checked at that
moment. The per-request proxy / transfer / search path reads the verdict the last
probe established (:meth:`_SshTunnel.loopback_ownership_ok`), because an ``lsof``
per proxied request is not a latency this path can pay. The accepted residual is
therefore one probe interval wide: a listener replaced just after a passing probe
is refused only once the next probe runs. It is bounded by the probe interval
(``constants.DEFAULT_PROBE_INTERVAL_SECS``) and by the condemnation being
synchronous — the
verdict flips the moment a proof fails, before the state machine has finished
tearing the tunnel down — and no send after that point is admitted.

**The pane's own connections are gated on the SOCKET, not on the port.** The
browser reaches the destination through an in-process forward
(:meth:`_SshTunnel._forward_pane_connection`) carrying a query token and a
host-scoped cookie of its own, and it is the one path a port-scoped proof cannot
gate: proving a port and then dialling it are two steps, and a local process
retry-looping the bind eventually wins that gap. So the relay dials FIRST and then
attributes the process holding the far end of THAT established connection
(:func:`kiro_crew.platform_compat.established_peer_pids`, matched on the exact
4-tuple), and carries bytes only when it is the pinned owner. An answer naming
another pid refuses and condemns the tunnel; no answer refuses that connection
alone — an empty attribution is also what an already-closed connection looks
like — except where the destination is this process's own port and there is no
second party to attribute. Every ownership lookup is bounded, so no wedged tool
holds a browser connection — or a teardown — open.

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
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

import aiohttp

from kiro_crew import platform_compat
from kiro_crew.cloud import ssm as cloud_ssm

# The local (embedding) gateway's configured port — carried into the minted
# remote token as the CSP frame-ancestor parent origin so the embedded pane can
# be framed by this desktop app on whatever KIROCREW_PORT it runs on (no
# hardcoded port, no wildcard). See server._extra_frame_ancestors.
from kiro_crew.config import live
from kiro_crew.config.loader import DASHBOARD_PORT as _LOCAL_DASHBOARD_PORT
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.deploy.engine import aws_spawn_env
from kiro_crew.instances.constants import CAPABILITY_REPLY_MAX_BYTES as _CAPABILITY_REPLY_MAX_BYTES
from kiro_crew.instances.constants import (
    DEFAULT_CAPABILITY_PROXY_TIMEOUT_SECS as _CAPABILITY_PROXY_TIMEOUT,
)
from kiro_crew.instances.constants import (
    DEFAULT_CONNECT_TIMEOUT_SECS as _DEFAULT_CONNECT_TIMEOUT_SECS,
)
from kiro_crew.instances.constants import (
    DEFAULT_LOOPBACK_CONNECT_TIMEOUT_SECS as _DEFAULT_LOOPBACK_CONNECT_TIMEOUT_SECS,
)
from kiro_crew.instances.constants import (
    DEFAULT_LOOPBACK_MINT_TIMEOUT_SECS as _DEFAULT_LOOPBACK_MINT_TIMEOUT_SECS,
)
from kiro_crew.instances.constants import DEFAULT_MAX_RECOVERY_ATTEMPTS as _MAX_RECOVERY
from kiro_crew.instances.constants import DEFAULT_MINT_TIMEOUT_SECS as _DEFAULT_MINT_TIMEOUT_SECS
from kiro_crew.instances.constants import DEFAULT_PROBE_FAILURE_THRESHOLD as _PROBE_FAILS
from kiro_crew.instances.constants import DEFAULT_PROBE_INTERVAL_SECS as _PROBE_INTERVAL
from kiro_crew.instances.constants import (
    DEFAULT_PROXY_CONNECT_TIMEOUT_SECS as _PROXY_CONNECT_TIMEOUT,
)
from kiro_crew.instances.constants import (
    DEFAULT_PROXY_READ_IDLE_TIMEOUT_SECS as _PROXY_READ_IDLE_TIMEOUT,
)
from kiro_crew.instances.constants import (
    DEFAULT_RECOVER_BACKOFF_MAX_SECS as _RECOVER_BACKOFF_MAX_SECS,
)
from kiro_crew.instances.constants import DEFAULT_SEARCH_PROXY_TIMEOUT_SECS as _SEARCH_PROXY_TIMEOUT
from kiro_crew.instances.constants import DEFAULT_SESSION_TRANSFER_TIMEOUT_SECS as _TRANSFER_TIMEOUT
from kiro_crew.instances.constants import (
    DEFAULT_SSM_CONNECT_TIMEOUT_SECS as _DEFAULT_SSM_CONNECT_TIMEOUT_SECS,
)
from kiro_crew.instances.constants import (
    DEFAULT_SSM_MINT_TIMEOUT_SECS as _DEFAULT_SSM_MINT_TIMEOUT_SECS,
)
from kiro_crew.instances.constants import DEFAULT_TOKEN_PROBE_TIMEOUT_SECS as _TOKEN_PROBE_TIMEOUT
from kiro_crew.instances.constants import DEFAULT_TOKEN_REFRESH_FRACTION as _REFRESH_FRACTION
from kiro_crew.instances.constants import (
    DEFAULT_TUNNEL_BASE_PORT,
)
from kiro_crew.instances.constants import (
    DIAGNOSTICS_CONNECT_TIMEOUT_CAP_SECS as _DIAGNOSTICS_CONNECT_TIMEOUT_CAP_SECS,
)
from kiro_crew.instances.constants import LOOPBACK_HOST as _LOOPBACK
from kiro_crew.instances.constants import (
    LOOPBACK_TRANSPORT_UNAVAILABLE_CODE as _LOOPBACK_UNAVAILABLE_CODE,
)
from kiro_crew.instances.constants import SEARCH_REPLY_MAX_BYTES as _SEARCH_REPLY_MAX_BYTES
from kiro_crew.instances.diagnostics import (
    diagnose_instance,
    diagnose_instance_loopback,
    diagnose_instance_ssm,
)
from kiro_crew.instances.local_token_mint import _bind_covers_loopback, mint_loopback_token
from kiro_crew.instances.port_allocator import PortAllocator, _is_addr_free, _is_port_free
from kiro_crew.instances.registry import (
    _NO_FORWARDER_PID,
    _UNALLOCATED_PORT,
    CONNECTION_METHOD_LOOPBACK,
    CONNECTION_METHOD_SSH,
    CONNECTION_METHOD_SSM,
    Instance,
    InstancesRegistry,
)
from kiro_crew.instances.run_marker import read_pid as _read_recorded_pid
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
    LoopbackValidationError,
    SshValidationError,
    SsmValidationError,
    validate_aws_profile,
    validate_aws_region,
    validate_remote_bin,
    validate_ssh_host,
    validate_ssm_run_as,
    validate_ssm_target,
)
from kiro_crew.port_resolution import port_is_gateway_owned_on_loopback
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import _HMAC_KEY_MIN_BYTES as _SEL_HMAC_KEY_MIN_BYTES
from kiro_crew.sel import sel, sel_hmac_key_path

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: What :meth:`_SshTunnel._pane_peer_verdict` can say about the process on the far
#: end of one established pane connection. Three states rather than a boolean
#: because only ``"foreign"`` is EVIDENCE: an answer naming a pid that is not the
#: pinned owner means the listener was replaced, and condemns the tunnel.
#: ``"unattributable"`` is absence of evidence — the lookup timed out, the tool
#: cannot see the socket, or the connection is already gone (``lsof`` exits
#: non-zero when nothing matches, which is what a closed connection looks like) —
#: so it refuses THIS connection and leaves the tunnel alone. Collapsing the two
#: would let ordinary browser connection churn condemn a healthy tunnel.
_PaneVerdict = Literal["ours", "unattributable", "foreign"]

#: How long either ownership lookup may hold the coroutine that awaits it — the
#: pinned-owner proof (:meth:`_SshTunnel._loopback_owner_unchanged`) and the
#: established-peer attribution (:meth:`_SshTunnel._socket_peer_is_owner`). Both
#: fork ``lsof``, which carries its own subprocess bound, so this sits just above
#: it: the subprocess bound is what normally fires, and this one catches a worker
#: thread wedged OUTSIDE the subprocess — a stalled filesystem read, or a thread
#: that never gets scheduled. It does not stop the thread, which no cancel can
#: reach; it stops the WAIT, and the late answer is discarded rather than acted
#: on. Not answering in time is a refusal, never a relay on unverified identity.
_OWNERSHIP_LOOKUP_TIMEOUT_SECS = 6.0

#: Upper bound on the final listener close after every pane relay has been
#: cancelled and gathered. Deliberately SHORTER than
#: _OWNERSHIP_LOOKUP_TIMEOUT_SECS: a relay parked in a lookup thread is
#: cancelled at its await, and teardown never waits for that worker thread. The
#: late answer is discarded, and ``_stopping`` keeps it from carrying bytes.
_FORWARDER_DRAIN_TIMEOUT_SECS = 2.0


def _established_peer_pids(near: tuple[str, int], far: tuple[str, int]) -> list[int]:
    """PIDs holding the far end of the established connection *near*→*far*.

    Thin seam over :func:`platform_compat.established_peer_pids` so the relay's
    gate is patchable in tests the same way ``_proven_loopback_owner`` is.
    Synchronous — it forks a port lookup — so callers reach it through
    ``asyncio.to_thread``.
    """
    return platform_compat.established_peer_pids(near, far)


def _proven_loopback_owner(port: int) -> int | None:
    """PID this user's gateway holds ``127.0.0.1:<port>`` with, else ``None``.

    Composes the two halves of a retainable identity: the address-scoped proof
    (:func:`kiro_crew.port_resolution.port_is_gateway_owned_on_loopback`) decides
    whether a loopback connect reaches OUR gateway, and the run-marker sidecar
    names which process that is. ``None`` means the port is not provably ours,
    which every caller treats as a refusal rather than a retry.

    Synchronous: it forks a port lookup and reads a file, so callers reach it
    through ``asyncio.to_thread`` rather than from the event loop.
    """
    if not port_is_gateway_owned_on_loopback(port):
        return None
    return _read_recorded_pid(port)


#: The closed set of peer endpoints :meth:`SshTunnelManager.peer_capability` may
#: read. Every one is a GET that reports what the peer gateway CAN do — its
#: version, its agent roster, its model list, its effort levels, its workspaces —
#: and none of them mutates anything. Keeping the set here (rather than letting
#: the caller name a path) is what makes the method a carrier instead of a second
#: proxy: a tainted string cannot reach a peer route that was never listed, and
#: the ``api/agents`` mutating verbs stay unreachable even though the roster read
#: lives under the same prefix. Adding a row is a security decision — it grants
#: the local gateway a new read against every connected peer.
_PEER_CAPABILITY_PATHS: frozenset[str] = frozenset(
    {
        "/api/version",
        "/api/agents",
        "/api/models",
        "/api/effort-levels",
        "/api/workspaces",
    }
)
# Poll cadence while waiting for the forward to come up.
_READY_POLL_INTERVAL_SECS = 0.25
# Bound on retained stderr so a chatty/looping ssh can't grow memory unbounded.
_MAX_STDERR_CHARS = 2000

# Self-heal respawn backoff: wait this base (doubled per consecutive attempt,
# capped) before rebuilding a failed tunnel, so a flapping link / bind race
# can't spin a tight respawn loop. Applied in the scheduling seam (_on_tunnel_exit)
# so direct _recover() callers (tests) aren't slowed.
_RECOVER_BACKOFF_BASE_SECS = 1.0

# Grace given to a reclaimed (hard-kill-orphaned) forwarder after SIGTERM
# before escalating to SIGKILL, and after SIGKILL before giving up waiting.
# `ssh -N` exits on TERM essentially immediately; the escalation mirrors
# _SshTunnel._terminate's shape with tighter bounds because this runs inside
# connect() under the manager lock. Nothing in the connect depends on the wait
# completing — the recorded port stays excluded from allocation either way —
# so a slow exit costs only these bounded seconds, never correctness.
_RECLAIM_TERM_GRACE_SECS = 2.0
_RECLAIM_KILL_GRACE_SECS = 1.0
# Liveness poll cadence while waiting out the grace windows above.
_RECLAIM_POLL_INTERVAL_SECS = 0.05

# Domain tag for the forwarder-identity MAC subkey. Mirrors the
# ``session_pid_sig`` precedent: the signing key is a one-way derivation of
# the SEL trust root and this domain, so this protocol, the SEL audit chain,
# and the session-pid sidecars never share a signing key and a MAC produced by
# any of them is valueless to the others.
_RECLAIM_SIG_DOMAIN = b"kirocrew-forwarder-identity-v1"


def _reclaim_identity_key() -> bytes | None:
    """Derive the forwarder-identity signing subkey, or ``None`` when absent.

    Anchored on the SEL trust root (``sel_hmac_key_path()``), which only the
    gateway creates and which sits on the sensitive-path deny list — an agent
    can neither read nor replace it, which is the entire point: a signature
    under this key is a claim only the GATEWAY can have made. Never creates
    the key (a first-touch race would mint a root the SEL then distrusts);
    when it cannot be read the reclaim protocol degrades to "never reclaim"
    (fail closed) rather than trusting unsigned registry state.
    """
    try:
        raw = sel_hmac_key_path().read_bytes()
    except OSError:
        return None
    if len(raw) < _SEL_HMAC_KEY_MIN_BYTES:
        return None
    return hmac.new(raw, _RECLAIM_SIG_DOMAIN, hashlib.sha256).digest()


def _forwarder_identity_sig(key: bytes, instance_id: str, pid: int, start: str, port: int) -> str:
    """MAC over one instance's recorded forwarder identity.

    Binds the identity to the INSTANCE as well as to the process attributes,
    so a valid record cannot be replayed under another instance id, and any
    edit to pid, start time, or port invalidates it. NUL joints keep field
    boundaries unambiguous (no recorded field can contain a NUL: the id and
    port are charset/range-validated and the start value is a single
    ``/proc``/``ps``/FILETIME token).
    """
    msg = "\0".join((instance_id, str(pid), start, str(port))).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


# ssh prints these benign advisory lines to stderr on connect (post-quantum KEX
# warning); they are NOT failures. Strip them from captured stderr so the real
# error (e.g. "bind: Address already in use") isn't masked in logs/status.
_BENIGN_SSH_STDERR_MARKERS = (
    "post-quantum key exchange",
    "store now, decrypt later",
    "server may need to be upgraded",
    "openssh.com/pq",
)


# Classification phrases for _exit_error / _ssm_exit_error, matched against the
# lowercased noise-stripped stderr. The first hit ALSO anchors the sanitized
# detail window (see _sanitize_banner) so the classified phrase survives the cap
# even when arbitrary benign stderr (e.g. LocalCommand output) precedes it.
_SSH_AUTH_SIGNALS = (
    "permission denied",
    "publickey",
    "authentication failed",
    "certificate has expired",
    "certificate expired",
)
_SSH_TRANSPORT_DROP_SIGNALS = (
    "timed out during banner exchange",
    "session ended unexpectedly",
    "connection timed out",
    "connection reset",
    "closed by remote host",
    "connection refused",
)
_SSH_BIND_SIGNALS = ("address already in use", "cannot listen to port")
_SSM_CREDENTIAL_SIGNALS = (
    "expired",
    "unable to locate credentials",
    "no credentials",
    "credentials not found",
)
_SSM_DENIAL_SIGNALS = ("accessdenied", "not authorized", "unauthorizedoperation")
_SSM_PLUGIN_SIGNALS = ("sessionmanagerplugin", "session-manager-plugin")
_SSM_TARGET_SIGNALS = (
    "targetnotconnected",
    "not connected",
    "invalidinstanceid",
    "invalidinstanceinformation",
)
_SSM_BIND_SIGNALS = ("address already in use", "bind")


def _first_hit(low: str, phrases: tuple[str, ...]) -> str | None:
    """Return the first of *phrases* present in *low* (already lowercased)."""
    for phrase in phrases:
        if phrase in low:
            return phrase
    return None


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


_BANNER_DETAIL_MAX_CHARS = 200


def _sanitize_banner(text: str, *, anchor: str | None = None) -> str:
    """ANSI-strip + credential/exfil-redact untrusted ssh stderr before it is
    surfaced in status/logs, capped at ``_BANNER_DETAIL_MAX_CHARS``. The banner
    is external, proxy-controlled text, so it is a redacted secondary detail
    only -- never a classification signal.

    When *anchor* names the lowercase classification phrase the exit-error
    classifier matched, the fixed-width window is centered on the first
    occurrence of that phrase instead of taken from the head, so benign stderr
    written earlier (e.g. arbitrary ``LocalCommand`` output such as repeated
    ``tput`` warnings under launchd/systemd where ``TERM`` is unset) cannot
    consume the budget and truncate the classified reason out of the surfaced
    detail. Centering on the phrase itself (never on its line, which the
    proxy-controlled buffer can make arbitrarily long) keeps the phrase inside
    the window unconditionally. Without an anchor, or when sanitization
    removed the phrase, the head slice is unchanged.
    """
    cleaned = _ANSI_CSI_RE.sub("", text)
    cleaned = redact_credentials(cleaned)[0]
    cleaned = redact_exfiltration_urls(cleaned)[0]
    if len(cleaned) <= _BANNER_DETAIL_MAX_CHARS:
        return cleaned
    if anchor:
        hit = cleaned.lower().find(anchor)
        if hit >= 0:
            start = hit + len(anchor) // 2 - _BANNER_DETAIL_MAX_CHARS // 2
            start = max(0, min(start, len(cleaned) - _BANNER_DETAIL_MAX_CHARS))
            return cleaned[start : start + _BANNER_DETAIL_MAX_CHARS]
    return cleaned[:_BANNER_DETAIL_MAX_CHARS]


class ProxyRequestError(Exception):
    """Typed failure from :meth:`SshTunnelManager.proxy_request`.

    Carries a machine-readable ``code`` (mirrors the ``code`` convention of the
    federated-search errors) and a suggested ``http_status`` so the route
    handler can translate a failure without string-matching the message.
    """

    def __init__(self, code: str, message: str, *, http_status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class _PeerUnavailable(Exception):
    """A request to a connected peer could not be attempted at all.

    Raised by :meth:`SshTunnelManager._peer_target` and
    :meth:`SshTunnelManager._peer_cookie_header` so the three public callers can
    share how a peer target is *resolved* without sharing their error contracts:
    each maps ``kind`` onto its own machine-readable code. Those codes are
    deliberately NOT derived from ``kind`` here — the ``proxy_``/``transfer_``/
    ``search_`` families belong to three separate route contracts pinned by
    ``test_error_code_contract.py``, and a reader grepping for one of them must
    land on the site that returns it.

    ``kind`` is ``"not_connected"`` or ``"no_credential"``; ``message`` is the
    caller-facing text, identical across the three families.
    """

    _MESSAGES = {
        "not_connected": "instance is not connected",
        "no_credential": "no live credential for this instance; reconnect it",
    }

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind
        self.message = self._MESSAGES[kind]


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

    ``transport="loopback"`` is the third case and the one with no child
    PROCESS: the destination gateway already listens on
    ``127.0.0.1``:``remote_port`` on this host, so there is nothing to spawn,
    and the hub binds its own allocated ``local_port`` in-process and relays to
    the destination. Everything the class does that
    watches the PORT rather than the process — the readiness wait, the health
    probe, the teardown — is unchanged and is exactly the right supervision for
    it. The one thing that has no analogue is a child exit, which is what
    normally publishes a failure verdict; :meth:`_fail_unhealthy` publishes it
    directly instead.
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
        transport: str = CONNECTION_METHOD_SSH,
        ssm_target: str = "",
        aws_profile: str = "",
        aws_region: str = "",
        own_port: int = 0,
        own_bind_host: str = "",
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
        self._transport = transport  # "ssh", "ssm" or "loopback"
        self._ssm_target = ssm_target
        self._aws_profile = aws_profile
        self._aws_region = aws_region

        self._proc: asyncio.subprocess.Process | None = None
        self._monitor_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._probe_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._stop_event = asyncio.Event()
        self._probe_failures = 0
        self._probe_failed = False  # set when the health probe forced teardown
        # Loopback ownership, retained across the tunnel's life. The proven owner
        # pid is adopted once (at establishment) and thereafter COMPARED: a
        # different pid on the port is a different listener, which is a failure
        # rather than a new identity to adopt. ``_ownership_condemned`` is the
        # verdict every credential-bearing send reads, set the moment a proof
        # fails so it blocks sends without waiting for the state machine.
        self._loopback_owner_pid: int | None = None
        self._ownership_condemned = False
        # This gateway's OWN listening port and bind address, for the one case
        # external attribution is not needed: the destination is this very
        # process. Mirrors the mint's carve-out exactly (see
        # :meth:`_destination_is_self`).
        self._own_port = int(own_port or 0)
        self._own_bind_host = own_bind_host
        # The hub-owned listener the iframe loads from on the loopback transport,
        # plus the relay tasks crossing it. None for ssh/ssm, where the forwarder
        # child owns that socket instead.
        self._forwarder: asyncio.AbstractServer | None = None
        self._forward_tasks: list[asyncio.Task] = []  # type: ignore[type-arg]
        self._stopping = False
        self._stderr_buf = ""
        self.status = TunnelStatus(
            instance_id=instance_id,
            local_port=local_port,
            remote_port=remote_port,
        )

    def _build_argv(self) -> list[str]:
        """Build the transport-specific supervised child argv.

        Never called for the loopback transport, which spawns nothing — see
        :meth:`_spawn_child`.
        """
        if self._transport == CONNECTION_METHOD_SSM:
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
        if not await self._spawn_child():
            return False

        ready = await self._wait_until_ready()
        if not ready:
            await self._terminate()
            # The loopback transport has no child: its listener is what must go.
            await self._stop_loopback_forwarder()
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

    async def _spawn_child(self) -> bool:
        """Spawn the forwarder child, or report success when there is none to spawn.

        The loopback transport forwards nothing: its destination is already
        listening on this host, so there is no child, and ``_proc`` stays None.
        Every later step that reads ``_proc`` already treats None as "no child"
        (``_failed_on_child_exit``, ``_capture_stderr``, ``_terminate``,
        ``pid``), so the readiness wait, health probe and teardown carry over
        unchanged; :meth:`_fail_unhealthy` covers the one that does not.

        Returns False with an ERROR status when the spawn itself fails.
        """
        if self._transport == CONNECTION_METHOD_LOOPBACK:
            return await self._start_loopback_forwarder()
        # Built in a worker thread: the SSM branch resolves the aws CLI
        # absolutely, which probes the filesystem (PATH scan +
        # well-known install dirs) — synchronous work that must not run on the
        # gateway event loop, where a stalled network mount on PATH would
        # freeze every request and heartbeat.
        argv = await asyncio.to_thread(self._build_argv)
        target = self._ssm_target if self._transport == CONNECTION_METHOD_SSM else self._ssh_host
        logger.info(
            "Opening %s tunnel for %s: 127.0.0.1:%d -> %s:%d",
            self._transport,
            self._id,
            self._local_port,
            target,
            self._remote_port,
        )
        try:
            ssm = self._transport == CONNECTION_METHOD_SSM
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
                # SSM only: the argv head is resolved absolutely, but the aws CLI
                # then looks session-manager-plugin up BY NAME on this child's own
                # PATH, which a GUI-launched gateway hands down as the minimal
                # launchd one — so the tunnel dies inside a correctly-resolved aws
                # unless the child's env carries the install dirs. argv[0]
                # is handed over so the widening is withheld for a bare head: that
                # bare name IS a provenance refusal, and widening would put the
                # refused binary back within execvp's reach. None means inherit,
                # which is what the ssh transport wants: its binary lives in the
                # system bin dir and needs no widening.
                env=(aws_spawn_env(argv[0]) if ssm else None),
            )
        except OSError as e:
            self.status.state = TunnelState.ERROR
            self.status.error = f"failed to spawn {self._transport} tunnel: {e}"
            logger.error("Tunnel spawn failed for %s: %s", self._id, e)
            return False
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
                if self._ownership_condemned:
                    # Not a transient: the thing on the port is not the gateway
                    # this tunnel's credential belongs to. Retrying cannot make
                    # it ours again, so fail now instead of spending the failure
                    # budget and leaving sends open across those cycles.
                    self._probe_failed = True
                    self._probe_failures = 0
                    asyncio.create_task(self._fail_unhealthy())
                    return
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
                    # Publish the failure; with a child that means terminating it
                    # so _monitor marks ERROR and fires on_exit. Done in a task so
                    # we don't await our own cancellation if stop() races in.
                    asyncio.create_task(self._fail_unhealthy())
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let the probe loop crash silently
            logger.exception("Tunnel probe loop crashed for %s: %s", self._id, exc)

    async def _wait_until_ready(self) -> bool:
        """Poll the local forward until it is usable for a credential-bearing send.

        Fails early if the ssh child exits before the port comes up (e.g. auth
        failure, ExitOnForwardFailure), capturing stderr for diagnostics. For the
        loopback transport this is also where the listener's identity is PINNED:
        :meth:`_port_reachable` adopts the proven owner pid on its first success,
        so every later probe and send compares against an identity established
        before any credential moved.
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
            if self._ownership_condemned:
                # Something answers on the port and it is provably not our
                # gateway. Polling cannot change that, and the tunnel must not
                # come up at all: a CONNECTED loopback tunnel is a licence to
                # send the bearer there.
                self.status.error = (
                    f"the listener on {_LOOPBACK}:{self._remote_port} is not "
                    f"provably a Kiro Crew gateway of yours; refusing to treat it "
                    f"as this instance"
                )
                return False
            await asyncio.sleep(_READY_POLL_INTERVAL_SECS)
        self.status.error = self._ready_timeout_error()
        return False

    def _ready_timeout_error(self) -> str:
        """Name what the readiness wait was waiting for, per transport.

        A forwarding transport was waiting for its own child to bind. The
        loopback transport's readiness wait watches the DESTINATION port, which
        it does not bind, so a timeout there means the destination gateway is
        simply not listening — reporting that as a
        forward that failed to come up would point the operator at machinery
        this transport does not have.
        """
        if self._transport == CONNECTION_METHOD_LOOPBACK:
            return (
                f"no gateway answered {_LOOPBACK}:{self._remote_port} "
                f"within {self._connect_timeout}s"
            )
        return f"timed out after {self._connect_timeout}s waiting for forward"

    async def _fail_unhealthy(self) -> None:
        """Publish a probe-condemned tunnel as failed so self-heal can fire.

        With a supervised child, terminating it IS the publication: ``_monitor``
        observes the exit, marks ERROR and calls ``on_exit``. The loopback
        transport has no child and therefore no exit to observe, so the same
        verdict is published here directly — otherwise a destination gateway
        that went away would leave the tunnel reported CONNECTED forever, with
        nothing to trigger recovery.
        """
        if self._proc is not None:
            await self._terminate()
            return
        self.status.state = TunnelState.ERROR
        self.status.error = self._exit_error(None)
        logger.warning("Tunnel for %s failed its health probe: %s", self._id, self.status.error)
        if self._on_exit is not None:
            with contextlib.suppress(Exception):
                self._on_exit(self._id)

    async def rearm_loopback(self) -> bool:
        """Re-prove a loopback destination without releasing the pane listener.

        The browser-facing port is a credential boundary: an iframe keeps its
        host-scoped cookie and reconnects while self-heal runs, so closing this
        listener would let another local process bind the origin and receive that
        reconnect. Recovery therefore keeps ``_forwarder`` intact, drops relays
        from the failed destination generation, and pins a fresh owner behind the
        same listener before returning to CONNECTED.
        """
        if self._transport != CONNECTION_METHOD_LOOPBACK:
            return False
        if self._forwarder is None or self._stopping or self._stop_event.is_set():
            return False

        previous_probe = self._probe_task
        if (
            previous_probe is not None
            and previous_probe is not asyncio.current_task()
            and not previous_probe.done()
        ):
            previous_probe.cancel()
            await asyncio.gather(previous_probe, return_exceptions=True)

        relays, self._forward_tasks = list(self._forward_tasks), []
        for relay in relays:
            relay.cancel()
        if relays:
            await asyncio.gather(*relays, return_exceptions=True)

        self._ownership_condemned = False
        self._loopback_owner_pid = None
        self._probe_failed = False
        self._probe_failures = 0
        self.status.state = TunnelState.CONNECTING
        self.status.error = ""

        ready = await self._wait_until_ready()
        if not ready or self._forwarder is None or self._stopping or self._stop_event.is_set():
            self.status.state = TunnelState.ERROR
            self.status.error = self.status.error or "loopback destination did not recover"
            return False

        self.status.state = TunnelState.CONNECTED
        self.status.connected_at = time.time()
        self.status.error = ""
        if _PROBE_INTERVAL > 0:
            self._probe_task = asyncio.create_task(self._probe_loop())
        logger.info(
            "Loopback destination recovered for %s behind retained pane port %d",
            self._id,
            self._local_port,
        )
        return True

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
        await self._capture_stderr()
        self.status.state = TunnelState.ERROR
        self.status.error = self._exit_error(proc.returncode)
        return True

    def loopback_ownership_ok(self) -> bool:
        """Whether this tunnel's listener is still provably our gateway.

        The verdict a credential-bearing send reads. It is a CACHED answer, fresh
        as of the last reachability probe, so the per-request proxy path costs no
        port lookup. Always ``True`` for a forwarding transport, which carries no
        such exposure: its credential goes into an ``ssh`` / ``session-manager``
        child, and that child's exit takes the forwarded socket down with it, so
        there is no freed port for another process to bind.
        """
        if self._transport != CONNECTION_METHOD_LOOPBACK:
            return True
        return not self._ownership_condemned

    def _condemn_ownership(self, reason: str) -> None:
        """Record that this tunnel's listener fails the ownership proof.

        One-way within one destination generation: an ordinary probe or send can
        never adopt whatever replaced the pinned owner. Loopback self-heal is the
        sole reset point; it cancels every relay from the failed generation,
        clears the retained destination identity, and re-proves behind the same
        hub-owned pane listener before returning to CONNECTED.
        """
        if not self._ownership_condemned:
            self._ownership_condemned = True
            logger.warning(
                "Loopback listener on %s:%d is no longer provably this user's "
                "gateway (%s) for %s — refusing further credential-bearing "
                "requests on it",
                _LOOPBACK,
                self._remote_port,
                reason,
                self._id,
            )

    async def revalidate_loopback_owner(self) -> bool:
        """Re-prove the listener now, for a caller about to send a credential.

        The fresh counterpart to :meth:`loopback_ownership_ok`, for the rare
        credential-ESTABLISHING sends. Answers ``False`` for an already-condemned
        tunnel without re-proving, since the verdict is one-way.
        """
        if self._transport != CONNECTION_METHOD_LOOPBACK:
            return True
        if self._ownership_condemned:
            return False
        return await self._loopback_owner_unchanged()

    def _destination_is_self(self) -> bool:
        """Whether the destination listener IS this very process.

        The mint's carve-out, reproduced condition for condition: the same port,
        on this process's exact ``127.0.0.1`` bind
        (:func:`kiro_crew.instances.local_token_mint._bind_covers_loopback`).
        Both halves are required. A gateway bound to one non-loopback interface
        leaves ``127.0.0.1:<port>`` free for any local process. A wildcard
        ``0.0.0.0`` bind is not conclusive either: on macOS/BSD a more-specific
        loopback listener can coexist and wins dispatch. An unstated bind address
        answers False too, so all of those cases run the ordinary proof.

        Deliberately not widened beyond that pair: this is the only case where
        there is no second party for external attribution to speak about.
        """
        return (
            self._own_port > 0
            and self._remote_port == self._own_port
            and _bind_covers_loopback(self._own_bind_host)
        )

    async def _loopback_owner_unchanged(self) -> bool:
        """Re-prove the loopback listener and hold its identity steady.

        Adopts the proven pid the first time (establishment) and compares it on
        every later call. An unprovable listener or a changed pid condemns the
        tunnel and answers ``False``; the retained identity is left as it was, so
        an intruder's pid never becomes the thing we compare against.

        The one exception is :meth:`_destination_is_self`: when the destination is
        this process's own port on its exact loopback bind, attribution has nothing
        to add, so a lookup that cannot answer (an unprivileged caller that cannot
        see its own socket, for instance) does not condemn the tunnel. A wildcard-
        bound hub is not this exception and runs the proof. The identity pinned in
        the exact-bind case is this process's pid, so a later answer naming anything
        else still reads as a change.

        Off the event loop: the proof forks a port lookup. BOUNDED off it too —
        the lookup runs in a worker thread a cancel cannot interrupt, so an
        unbounded wait would let one wedged ``lsof`` park a pane connection, or a
        probe cycle, indefinitely. A lookup that does not answer in time is not a
        different verdict from one that answers nothing: both take the branch
        below, so the ``_destination_is_self`` carve-out still applies and every
        other destination is condemned.
        """
        owner = await self._bounded_lookup(_proven_loopback_owner, self._remote_port)
        if owner is None:
            if self._destination_is_self():
                if self._loopback_owner_pid is None:
                    self._loopback_owner_pid = os.getpid()
                return self._loopback_owner_pid == os.getpid()
            self._condemn_ownership("ownership could not be proven")
            return False
        if self._loopback_owner_pid is None:
            self._loopback_owner_pid = owner
            return True
        if owner != self._loopback_owner_pid:
            self._condemn_ownership(
                f"listener identity changed from pid {self._loopback_owner_pid} to pid {owner}"
            )
            return False
        return True

    async def _start_loopback_forwarder(self) -> bool:
        """Bind the pane's hub-owned port and forward it to the destination.

        The loopback transport's stand-in for an ``ssh -N -L`` child, in-process
        because the destination is on this host and an ssh self-connect fails
        host-key verification by design. What matters is not the mechanism but
        the ownership: the socket the iframe loads from belongs to THIS gateway,
        so it cannot be bound by anything else while the tunnel lives, and every
        connection across it passes a gate the hub controls.

        Binds ``127.0.0.1`` only, and publishes the port the kernel actually gave
        (the caller may pass 0) so the pane's origin and the cookie name derive
        from the real socket.
        """
        try:
            self._forwarder = await asyncio.start_server(
                self._forward_pane_connection, _LOOPBACK, self._local_port
            )
        except OSError as e:
            self.status.state = TunnelState.ERROR
            self.status.error = (
                f"could not bind the local port for {self._id} "
                f"({type(e).__name__}); retry, or move "
                f"instances.tunnel_base_port to a quieter range"
            )
            return False
        bound = self._forwarder.sockets[0].getsockname()[1]
        self._local_port = int(bound)
        self.status.local_port = int(bound)
        logger.info(
            "Serving local gateway for %s at %s:%d -> %s:%d (in-process forward)",
            self._id,
            _LOOPBACK,
            self._local_port,
            _LOOPBACK,
            self._remote_port,
        )
        return True

    async def _stop_loopback_forwarder(self) -> None:
        """Release the pane's port and drop every connection across it.

        Stop accepting first, cancel and gather the relays second, then await the
        listener close. This mirrors ``mcp_gateway.gatewayd``: on Python 3.12+
        ``Server.wait_closed()`` waits for accepted connections, so awaiting it
        before cancelling their handlers deadlocks on a long-lived pane socket.
        A port left bound by an orphaned listener is the squattable socket this
        whole path exists to remove, so the release is unconditional.

        The final listener wait is bounded, and teardown never waits on a lookup
        thread. Cancelling a relay parked in ``asyncio.to_thread`` cancels its
        await immediately; the worker may finish later, but its answer is
        discarded. ``_stopping`` is set HERE rather than relied on from
        ``stop()``, so every relay also finds the door shut after any late answer.
        """
        self._stopping = True
        server, self._forwarder = self._forwarder, None
        if server is not None:
            server.close()

        pumps, self._forward_tasks = list(self._forward_tasks), []
        for task in pumps:
            task.cancel()
        if pumps:
            await asyncio.gather(*pumps, return_exceptions=True)

        if server is not None:
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=_FORWARDER_DRAIN_TIMEOUT_SECS)
            except asyncio.TimeoutError:
                logger.warning(
                    "Timed out after %.1fs waiting for the loopback pane listener "
                    "for %s to close",
                    _FORWARDER_DRAIN_TIMEOUT_SECS,
                    self._id,
                )
            except Exception:
                logger.debug("Loopback pane listener close failed for %s", self._id, exc_info=True)

    async def _forward_pane_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Relay ONE browser connection to the destination, if it is still ours.

        The gate is the peer of the ESTABLISHED socket, not a port lookup taken
        before the dial. Proof-then-dial is two steps: the owner can exit between
        them, and a local attacker retry-looping the bind wins eventually. So the
        dial happens first and the relay only begins once the process holding the
        far end of THAT connection is attributed and matches the pinned owner —
        binding the verdict to the socket bytes will actually cross.

        The cached verdict and the pre-dial proof remain as fast-fails, refusing a
        condemned tunnel without paying for a lookup, but neither licenses a relay
        on its own. An attribution that cannot answer is a refusal, not
        permission, so a platform that cannot name a socket's peer cannot carry
        this transport (the ssh transport is unaffected).

        Registered in ``_forward_tasks`` as the FIRST statement, before any await:
        a task registered after the dial is invisible to
        :meth:`_stop_loopback_forwarder`'s snapshot, so a connection still parked
        in verification would survive the teardown and relay afterwards.
        ``_stopping`` is re-checked after EVERY await that can park — the proof,
        the dial and the attribution all can — because a cancel cannot interrupt
        the worker thread a lookup runs in, so a lookup that returns late must
        find the door already shut rather than proceed to relay. For the same
        reason both endpoints are re-checked for liveness after verification: a
        connection can be gone by the time a slow answer arrives, and there is
        nothing left to relay over.

        Both sockets are closed in one place, in a ``finally`` that does not
        await. Closing is synchronous and therefore runs to completion even while
        a cancellation unwinds this coroutine, where an awaited close could be
        interrupted between the two endpoints and leak the second one.
        """
        task = asyncio.current_task()
        if task is not None:
            self._forward_tasks.append(task)
        dest_writer: asyncio.StreamWriter | None = None
        try:
            if self._stopping:
                self._log_pane_refusal("the tunnel is stopping")
                return
            if not self.loopback_ownership_ok() or not await self._loopback_owner_unchanged():
                self._log_pane_refusal(
                    "the destination listener is not provably this user's gateway"
                )
                return
            if self._stopping:
                self._log_pane_refusal("the tunnel is stopping")
                return
            try:
                dest_reader, dest_writer = await asyncio.open_connection(
                    _LOOPBACK, self._remote_port
                )
            except OSError as e:
                logger.info(
                    "Pane connection for %s could not reach %s:%d (%s)",
                    self._id,
                    _LOOPBACK,
                    self._remote_port,
                    type(e).__name__,
                )
                return
            if self._stopping:
                self._log_pane_refusal("the tunnel is stopping")
                return
            verdict = await self._pane_peer_verdict(dest_writer)
            if verdict == "foreign":
                self._condemn_ownership(
                    "the process holding the far end of an established pane "
                    "connection is not the pinned owner"
                )
                self._log_pane_refusal("its established peer is not the pinned owner")
                return
            if verdict != "ours":
                self._log_pane_refusal("its established peer could not be attributed")
                return
            if self._stopping:
                self._log_pane_refusal("the tunnel is stopping")
                return
            if writer.is_closing() or dest_writer.is_closing():
                self._log_pane_refusal("an endpoint went away during verification")
                return
            await asyncio.gather(
                self._pump(reader, dest_writer),
                self._pump(dest_reader, writer),
            )
        finally:
            if task is not None and task in self._forward_tasks:
                self._forward_tasks.remove(task)
            self._drop_writers(writer, dest_writer)

    async def _pane_peer_verdict(self, dest_writer: asyncio.StreamWriter) -> _PaneVerdict:
        """Attribute the process on the far end of THIS established pane socket.

        Reads both endpoints off the connected socket, so the 4-tuple attributed
        is the one the relay is about to carry bytes over rather than whatever
        holds the port at lookup time. That is the whole point of running after the
        dial: a port-scoped answer describes a listener, and the listener it
        describes need not be the process on the other end of this socket.

        Only ``"ours"`` relays. The split between the two refusals is the split
        between evidence and its absence, and it is load-bearing: an empty
        attribution is also what an already-closed connection looks like, so
        treating it as a replacement would let a browser that opens and drops a
        connection condemn a healthy tunnel.

        One carve-out, the same one the establishment proof already makes
        (:meth:`_destination_is_self`): when the destination is this process's own
        port on its exact ``127.0.0.1`` bind, the far end IS this process, so there
        is no second party for attribution to speak about and an unprivileged
        caller that cannot see its own socket must not lose the pane. Wildcard-
        bound hubs run the proof because a more-specific loopback peer can outrank
        their socket. An answer naming another pid is still ``"foreign"`` in the
        exact-bind case as everywhere.

        Bounded through :meth:`_bounded_lookup`, so a wedged lookup refuses rather
        than holding a browser connection open.
        """
        sock = dest_writer.get_extra_info("socket")
        if sock is None:
            return "unattributable"
        try:
            near = sock.getsockname()
            far = sock.getpeername()
        except OSError:
            return "unattributable"  # already torn down; nothing to relay over
        pids = await self._bounded_lookup(
            _established_peer_pids, (near[0], near[1]), (far[0], far[1])
        )
        if not pids:
            if self._destination_is_self() and self._loopback_owner_pid == os.getpid():
                return "ours"
            return "unattributable"
        return "ours" if self._loopback_owner_pid in pids else "foreign"

    async def _bounded_lookup(self, fn: Callable[..., _T], *args: Any) -> _T | None:
        """Run one synchronous ownership lookup off the loop, under a hard bound.

        Answers ``None`` when the lookup did not answer — it timed out, it raised,
        or the thread could not run. Every caller reads that as "unattributable",
        never as permission.

        The bound does not stop the worker: a thread cannot be cancelled, so the
        lookup runs on and its answer is DISCARDED rather than delivered late.
        What the bound protects is the coroutine WAITING on it — a pane
        connection, a probe cycle, and through them teardown, none of which may be
        held open by one wedged ``lsof``. Cancellation propagates rather than
        being flattened into a verdict, so a cancelled relay dies at this await
        instead of resuming on an answer nobody is still entitled to act on.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fn, *args), timeout=_OWNERSHIP_LOOKUP_TIMEOUT_SECS
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Ownership lookup for %s did not answer within %.0fs; treating the "
                "listener as unattributable",
                self._id,
                _OWNERSHIP_LOOKUP_TIMEOUT_SECS,
            )
            return None
        except asyncio.CancelledError:
            raise
        except Exception:  # never let a lookup failure read as permission
            logger.debug("Ownership lookup for %s failed", self._id, exc_info=True)
            return None

    def _log_pane_refusal(self, reason: str) -> None:
        """Say once why a pane connection is not being relayed.

        Closing is not done here: every exit from
        :meth:`_forward_pane_connection` runs through its ``finally``, so there is
        one close path rather than one per refusal.
        """
        logger.warning("Refusing a pane connection for %s: %s", self._id, reason)

    @staticmethod
    def _drop_writers(*writers: asyncio.StreamWriter | None) -> None:
        """Close every writer given, tolerating one already gone.

        Synchronous on purpose. It runs from a ``finally`` that can be unwinding a
        cancellation, and an ``await`` there could be interrupted after the first
        endpoint and leak the second; ``close()`` flushes and schedules the FIN
        without yielding, so both endpoints are released whatever happens next.
        """
        for w in writers:
            if w is None:
                continue
            with contextlib.suppress(Exception):
                w.close()

    @staticmethod
    async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Copy one direction until EOF, then half-close so the peer sees it.

        Errors are swallowed rather than raised: either side closing is the
        ordinary end of an HTTP exchange, and the caller closes both ends anyway.
        """
        with contextlib.suppress(Exception):
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        with contextlib.suppress(Exception):
            if writer.can_write_eof():
                writer.write_eof()

    async def _port_reachable(self) -> bool:
        """Return True if the local forward is usable for a credential-bearing send.

        For a forwarding transport that is a TCP connect. The loopback transport
        additionally re-proves listener OWNERSHIP: it has no child, so its
        destination is an ordinary port on this host, and a TCP accept there is
        satisfied just as well by a process that bound the port after our gateway
        exited. Each probe cycle therefore re-establishes what the send path
        relies on, and a failure condemns the tunnel rather than counting toward
        recovery as a transient.

        The loopback probe dials the DESTINATION port, not the hub's own pane
        port: the pane port is a socket this process holds, so connecting to it
        would only prove the forwarder is up, never that anything answers behind
        it.
        """
        probe_port = (
            self._remote_port if self._transport == CONNECTION_METHOD_LOOPBACK else self._local_port
        )
        try:
            fut = asyncio.open_connection(_LOOPBACK, probe_port)
            reader, writer = await asyncio.wait_for(fut, timeout=1.0)
        except (OSError, asyncio.TimeoutError):
            return False
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        if self._transport == CONNECTION_METHOD_LOOPBACK:
            return await self._loopback_owner_unchanged()
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
        surfaced as a secondary detail, with the fixed-width detail window
        centered on the matched classification phrase so preceding benign
        noise (e.g. LocalCommand output) cannot truncate the real reason away.

        The SSM transport has an entirely different error vocabulary (IAM
        denials, a missing session-manager-plugin, an offline SSM agent), so it
        is classified separately by :meth:`_ssm_exit_error` — running SSM stderr
        through the ssh matchers above would mislabel e.g. an ``AccessDenied``
        as an "ssh auth failure".

        The loopback transport reaches this method only through
        :meth:`_fail_unhealthy` (there is no child, so no exit code): its single
        verdict is the probe-failure branch below.
        """
        if self._probe_failed:
            if self._transport == CONNECTION_METHOD_LOOPBACK:
                return f"the gateway on {_LOOPBACK}:{self._remote_port} " f"stopped answering"
            return "health probe failed — tunnel alive but not forwarding"
        if self._transport == CONNECTION_METHOD_SSM:
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
        # WSSH / transport session drops — not an auth problem. Worded neutrally
        # because this method is also used for the initial-connect failure path,
        # where no self-heal is armed yet (so it must not promise reconnection).
        hit = _first_hit(low, _SSH_TRANSPORT_DROP_SIGNALS)
        if hit is not None:
            return f"ssh tunnel transport drop: {_sanitize_banner(tail, anchor=hit)}"
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
        if tail:
            return f"SSM session exited {returncode}: {_sanitize_banner(tail)}"
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
        await self._stop_loopback_forwarder()
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


def _verify_and_reclaim_forwarder(
    pid: int,
    expected_start: str,
    expected_argv: list[str],
    port: int,
    tree: bool,
    audit_resources: str,
) -> str:
    """Verify a recorded forwarder's identity, then SIGTERM/SIGKILL-reclaim it.

    Blocking by design — process-attribute reads, signals, liveness polls —
    so callers run it in a worker thread, never on the event loop. Doing the
    verification and the first signal in ONE thread keeps the check-to-signal
    window at in-process microseconds, the same residual the repo's other
    pid-reuse guards accept.

    Identity is pid + start time + exact argv, checked in that order, and the
    start-time comparison is RE-RUN before the destructive SIGKILL: the grace
    window is exactly the interval in which the pid can exit and be recycled,
    and ``pid_exists`` polling cannot observe an exit that is immediately
    followed by reuse. Mirrors the stale-app-backend reaper's guard
    (leak-not-mis-kill): any unconfirmed identity withholds the signal.

    ``tree=True`` for the SSM transport, whose child was spawned into its own
    process group (``start_new_session``, so pgid == pid) — the group signal
    reaps the ``session-manager-plugin`` grandchild still holding the
    forwarded port, and completion is judged by :func:`pgroup_exists` plus the
    port actually releasing, so a wrapper that exits first cannot fake
    success while the plugin keeps the port. If the group leader is already
    reaped by SIGKILL time, the group cannot be re-addressed through the
    existing pid-keyed helpers; the TERM broadcast has already reached every
    member, and a member that ignores it keeps the port — reported truthfully
    as not reclaimed (the port stays excluded from allocation). The ssh child
    is spawned WITHOUT a new group: after a gateway hard-kill it sits in the
    DEAD gateway's process group, where a group signal could hit unrelated
    survivors — so it gets a pid-scoped signal only, which suffices because
    ``ssh -N`` holds the forward itself and spawns no descendants of its own.

    Returns one of: ``"reclaimed"`` (identity confirmed, process/group gone,
    port released), ``"identity_mismatch"`` (nothing was ever signalled),
    ``"recycled_during_grace"`` (SIGKILL withheld: the pid stopped matching
    its recorded identity during the TERM grace), ``"not_gone"`` (signals
    delivered but the process, group, or port is still held at the end).
    Every path that delivered at least one signal emits a SEL audit event.
    """

    def _identity_holds() -> bool:
        now = platform_compat.process_start_time(pid)
        if now is None or now != expected_start:
            return False
        # Both halves, both times: the start token alone is 1s-granular on
        # macOS (``ps -o lstart=``), so a same-second pid reuse could keep it
        # matching while the process is someone else's — the argv half breaks
        # that tie. A mid-death target whose argv is already unreadable reads
        # as not-held and merely withholds the escalation (TERM was already
        # delivered to the verified process).
        return platform_compat.process_argv_matches_exact(pid, list(expected_argv))

    def _alive() -> bool:
        return platform_compat.pgroup_exists(pid) if tree else platform_compat.pid_exists(pid)

    def _gone() -> bool:
        # Single-address on purpose: the question here is "did OUR forwarder let
        # go of the port it held", and an ``ssh -L`` child binds 127.0.0.1 alone.
        # The aggregate ``_is_port_free`` would answer a DIFFERENT question -- "is
        # this port free for a new forward" -- so an unrelated ::1 listener would
        # make a fully reclaimed orphan report not-gone and mis-attribute this
        # reclaim's audited outcome.
        return not _alive() and _is_addr_free(port, "127.0.0.1")

    def _deliver(sig: int) -> None:
        if tree and _SshTunnel._signal_group(pid, sig):
            return
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError, ValueError):
            platform_compat.kill_pid(pid, sig)

    def _wait_gone(grace_secs: float) -> bool:
        deadline = time.monotonic() + grace_secs
        while time.monotonic() < deadline:
            if _gone():
                return True
            time.sleep(_RECLAIM_POLL_INTERVAL_SECS)
        return _gone()

    def _audit(outcome: str) -> None:
        try:
            sel().log_api_access(
                caller="gateway",
                operation="forwarder_orphan_reclaim",
                outcome=outcome,
                resources=audit_resources,
            )
        except Exception as exc:  # noqa: BLE001 — audit must never break reclaim
            logger.debug("SEL audit failed for forwarder_orphan_reclaim: %s", exc)

    if not _identity_holds():
        return "identity_mismatch"
    _deliver(platform_compat.SIGTERM)
    if _wait_gone(_RECLAIM_TERM_GRACE_SECS):
        _audit("sigterm")
        return "reclaimed"
    # Re-confirm identity before the destructive escalation, and withhold the
    # SIGKILL on ANYTHING short of a positive match — including a pid that no
    # longer exists while its group/port linger. A recycled pid would make
    # ``getpgid`` resolve (and the group kill target) the REPLACEMENT process,
    # and ``pid_exists`` polling cannot distinguish "still our child" from
    # "exited and recycled inside a poll gap", so absence of the pid is not a
    # safe fall-through: no verified identity, no SIGKILL
    # (leak-not-mis-kill). The TERM broadcast above already reached every
    # group member while the identity was verified; a member that ignores it
    # keeps the port, which stays excluded from allocation and is reported
    # truthfully below.
    if not _identity_holds():
        _audit("sigkill_withheld_identity_unconfirmed")
        return "recycled_during_grace"
    _deliver(platform_compat.SIGKILL)
    if _wait_gone(_RECLAIM_KILL_GRACE_SECS):
        _audit("sigkill")
        return "reclaimed"
    _audit("not_gone")
    return "not_gone"


@dataclass
class _TransportParams:
    """Validated, transport-specific connection parameters for one instance.

    Resolved once by :meth:`SshTunnelManager._resolve_transport` so the
    connect / rebuild / self-heal / token-refresh paths all build their tunnel
    and mint their token from the same validated values instead of each
    re-branching on ``connection_method``.
    """

    method: str  # "ssh" | "ssm" | "loopback"
    ssh_host: str = ""
    remote_bin: str = ""
    ssm_target: str = ""
    aws_profile: str = ""
    aws_region: str = ""
    ssm_run_as: str = ""

    @property
    def target(self) -> str:
        """The human-facing target (host, SSM instance id, or the loopback address).

        The loopback transport carries no address of its own: its destination is
        the fixed :data:`_LOOPBACK`.
        """
        if self.method == CONNECTION_METHOD_SSM:
            return self.ssm_target
        if self.method == CONNECTION_METHOD_LOOPBACK:
            return _LOOPBACK
        return self.ssh_host

    def tunnel_kwargs(self) -> dict:
        """Transport kwargs for the ``_SshTunnel`` constructor."""
        return {
            "transport": self.method,
            "ssm_target": self.ssm_target,
            "aws_profile": self.aws_profile,
            "aws_region": self.aws_region,
        }


class SshTunnelManager:
    """Manages per-instance tunnels (SSH, SSM or loopback) keyed by instance id.

    Holds the live tunnels, allocates loopback ports, mints per-instance tokens,
    and keeps the registry's ``was_connected`` / ``last_active`` hints in sync.
    Tokens are kept in memory only (never persisted, never logged) and handed to
    the API layer via :meth:`get_token`.

    The class name is retained (rather than renamed to a transport-neutral one)
    because it is referenced by ``dashboard/server.py`` and the existing test
    suite; it now supervises whichever transport each instance's
    ``connection_method`` selects.

    ``allow_loopback`` overrides the pod config flag for unit tests only. The
    exact ``KIROCREW_POD=1`` marker remains the outer gate and cannot be
    overridden. With ``allow_loopback=None``, the pod flag is read at each use
    rather than captured here, so disabling the verification seam takes effect
    without a gateway restart. The API performs the same fresh read.
    """

    def __init__(
        self,
        registry: InstancesRegistry,
        *,
        base_port: int = DEFAULT_TUNNEL_BASE_PORT,
        connect_timeout_secs: float | None = None,
        ssh_compression: bool = True,
        max_recovery_attempts: int = _MAX_RECOVERY,
        recover_backoff_max_secs: float = _RECOVER_BACKOFF_MAX_SECS,
        probe_failure_threshold: int = _PROBE_FAILS,
        mint_timeout_secs: float | None = None,
        allow_loopback: bool | None = None,
        mint_token: Callable[..., Awaitable[str]] = mint_remote_token,
        tunnel_factory: Callable[..., _SshTunnel] | None = None,
        parent_port: int | None = None,
        parent_bind_host: str = "",
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
        # The ADDRESS this gateway bound that port on, threaded in from the same
        # place as the port. The loopback mint needs both to tell "the
        # destination is my own listener" from "the destination is my own port
        # number", which differ on a gateway bound to one interface. Empty means
        # unstated, and the mint then proves the listener instead of assuming.
        self._parent_bind_host = parent_bind_host
        self._allocator = PortAllocator(base_port=base_port)
        self._connect_timeout = connect_timeout_secs
        self._ssh_compression = ssh_compression
        # Self-heal tunables (config-tunable via instances.*): max consecutive
        # recovery attempts before give-up, the cap on the per-attempt backoff,
        # and the per-tunnel consecutive-probe-failure teardown threshold.
        self._max_recovery = max_recovery_attempts
        self._recover_backoff_max = recover_backoff_max_secs
        self._probe_fails = probe_failure_threshold
        self._mint_timeout = mint_timeout_secs
        self._allow_loopback = allow_loopback
        self._mint_token = mint_token
        self._tunnel_factory = tunnel_factory or _SshTunnel
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
        # Same tasks, indexed by instance: a reconfiguration must cancel the
        # recovery in flight for ITS instance only, and a recovery that
        # captured the pre-edit record cannot be allowed to reinstall it.
        self._recovery_by_instance: dict[str, set[asyncio.Task]] = {}  # type: ignore[type-arg]
        # Instances whose coordinates are being rewritten right now. Self-heal
        # reads the record before it takes the lock, so cancelling the recoveries
        # in flight is not enough on its own: a tunnel exiting mid-edit schedules
        # a FRESH recovery that would read the pre-edit record. This barrier is
        # set before the first await of a reconfiguration and cleared after the
        # write, and recovery refuses to run for an instance named in it — which
        # closes the window instead of racing it with a retry loop.
        self._reconfiguring: set[str] = set()
        # Generation counter per instance, bumped every time a tunnel is INSTALLED.
        # A mint runs without the lock, so the tunnel it was minted for can be torn
        # down and replaced while it is in flight; `instance_id in self._tunnels` is
        # then true again and cannot tell the generations apart. The stamp can:
        # a token is stored only if the tunnel it belongs to is still the current
        # one. This covers every mint path, including the request-driven
        # `refresh_token()` the embedded dashboard calls, which is not a task in
        # `_refresh_tasks` and so cannot be cancelled by name.
        self._tunnel_epoch: dict[str, int] = {}
        # Proactive token refresh: per-instance refresh task + the mint timestamp
        # / ttl so the TTL-remaining can be surfaced (Stage 6).
        self._refresh_tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        self._token_minted_at: dict[str, float] = {}
        self._token_ttl_secs: dict[str, int] = {}
        # The transport tunables above are copies of instances.*, so a config write
        # reaches them only through apply_config(). Held on self because the watcher
        # holds the owner weakly. ``fail_closed=False``: the section carries no
        # authorization, so a degraded document's defaults are the right answer.
        self._config_sub = live.watch_section(
            self,
            "instances",
            method="apply_config",
            fail_closed=False,
            name="SshTunnelManager",
        )

    def apply_config(self, instances_cfg: object) -> None:
        """Adopt new ``instances.*`` transport tunables.

        Every value here is consulted per operation -- per connect, per mint, per
        recovery attempt -- so pushing it onto the manager is a genuine hot apply
        rather than a value that only matters at construction. The probe threshold
        is additionally propagated into the tunnels ALREADY running, since each one
        copied it when it was built and would otherwise keep tearing itself down on
        the old count.

        ``tunnel_base_port`` is deliberately left alone: the allocator has already
        handed out ports from the old base and live tunnels hold them, so moving the
        base mid-flight would only fragment the range. It applies to a manager built
        after the change.
        """
        self._connect_timeout = getattr(instances_cfg, "connect_timeout_secs")
        self._mint_timeout = getattr(instances_cfg, "mint_timeout_secs")
        self._ssh_compression = bool(getattr(instances_cfg, "ssh_compression"))
        self._max_recovery = int(getattr(instances_cfg, "max_recovery_attempts"))
        self._recover_backoff_max = float(getattr(instances_cfg, "recover_backoff_max_secs"))
        self._probe_fails = int(getattr(instances_cfg, "probe_failure_threshold"))
        for tunnel in self._tunnels.values():
            # Attribute-set on the live tunnel rather than a restart: the threshold
            # is compared against a running counter, so the new value takes effect on
            # the next probe without dropping a healthy forward.
            tunnel._probe_fails = self._probe_fails

    async def _persist_hint(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Run a registry hint write in a worker thread; return only when it is DONE.

        A cancelled ``await asyncio.to_thread(...)`` abandons only the await —
        the already-submitted worker thread keeps running, and its
        read-modify-rewrite of ``instances.json`` can land AFTER the caller's
        ``async with self._lock`` block has unwound. That late write races the
        next locked write (e.g. a cancelled connect's ``was_connected=True``
        overtaking a disconnect's reset and reviving an instance the user
        disconnected). So on cancellation this helper keeps waiting for the
        worker to finish, then re-raises the cancellation — the caller's lock
        is not released until the write has durably completed. Write FAILURES
        are swallowed: hint persistence is best-effort, matching the
        pre-offload ``contextlib.suppress(Exception)`` semantics.
        """
        task: asyncio.Task[Any] = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
        cancelled: asyncio.CancelledError | None = None
        while True:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as e:
                if task.done():
                    raise  # write already completed; propagate the cancel as-is
                cancelled = e
                continue  # keep waiting: the worker write is still in flight
            except Exception:
                pass  # best-effort hint write
            break
        if cancelled is not None:
            raise cancelled

    def _reserved_ports(self) -> set[int]:
        """Ports unavailable to pane allocation.

        Live and recorded pane ports stay unique. Every configured loopback
        destination is reserved too, even while its sibling gateway is stopped:
        letting another pane bind that port would prevent the sibling from
        starting later.
        """
        reserved: set[int] = {t.status.local_port for t in self._tunnels.values()}
        for inst in self._registry.list():
            if inst.local_port:
                reserved.add(inst.local_port)
            if inst.connection_method == CONNECTION_METHOD_LOOPBACK:
                reserved.add(inst.remote_port)
        return reserved

    async def _reclaim_orphan_forwarder(self, inst: Instance, params: _TransportParams) -> None:
        """Reclaim *inst*'s own forwarder child leaked by a gateway hard-kill.

        A SIGKILLed gateway never runs teardown, so its forwarder child
        survives, reparented to init with nothing left to reap it: one idle
        process, one loopback port, and one live session to the remote per
        hard-kill → reconnect cycle. The restarted manager finds the recorded
        ``local_port`` occupied and (correctly) allocates around it; this hook
        is what turns that permanent leak into a reclaim.

        Reclamation is keyed on OUR OWN recorded identity, never a
        process-table match — matching the table by argv pattern would
        SIGTERM forwards operators opened themselves, and no
        pattern can distinguish our child from a stranger's. The registry
        itself is agent-writable, so a recorded claim is honored only when it
        AUTHENTICATES: the record must carry the gateway's own MAC over
        (instance id, pid, start time, port), computed at spawn under a key
        derived from the SEL trust root — which an agent can neither read nor
        replace — so a record written or re-pointed by anything but this
        gateway fails verification outright. Behind the MAC, defense in depth
        from kernel-owned facts: the candidate must be a genuine ORPHAN — not
        a pid this manager currently supervises, and reparented to init
        (``get_ppid == 1``), which no live gateway's forwarder is. Then the
        recorded pid is trusted only behind a STRICT identity check, both
        halves recorded at spawn: the pid's start time must equal the recorded
        ``forwarder_start``, AND its full argv must exactly equal the forward
        command line this manager would construct for the recorded port (host
        and all). Anything less — either hint missing, process gone,
        attributes unreadable, or any element differing — means the identity
        cannot be confirmed: the process is left alone and connect falls
        through to normal allocation. The start-time half is what defeats pid
        recycling (argv can collide in principle; a recycled pid's start time
        cannot), and it is re-verified before the SIGKILL escalation inside
        the worker. Best-effort: a failed reclaim never fails the connect, it
        only leaves the leak for the next attempt.

        A rebuilt-vs-recorded argv can also drift apart without any foul play
        (an edited compression/host setting, or an ``aws`` entrypoint the
        kernel rewrites through a shebang) — that misses the reclaim, never
        mis-kills, and is logged below so the miss is visible instead of
        silent.

        Runs under the manager lock (its caller ``connect`` holds it); every
        blocking step — the port probe and the verify-and-signal worker — is
        pushed off the event loop via ``asyncio.to_thread``.
        """
        pid = inst.forwarder_pid
        port = inst.local_port
        start = inst.forwarder_start
        sig = inst.forwarder_sig
        if pid <= 1 or port <= 0 or not start or not sig:
            return  # identity not (fully) recorded — nothing we may touch
        # The registry is agent-writable state, so its identity claims are
        # UNTRUSTED until authenticated: the record must carry the MAC this
        # gateway (and only this gateway — the key derives from the SEL trust
        # root, which sits on the sensitive-path deny list) computed when it
        # spawned the child. A record an agent wrote, edited, or re-pointed at
        # someone else's process fails verification and is refused before any
        # process attribute is even read. Key unreadable -> refuse (never
        # trust unsigned state).
        key = await asyncio.to_thread(_reclaim_identity_key)
        if key is None:
            return
        # Both the reconstruction and the comparison are fed agent-writable
        # text: a record can carry arbitrary strings (even lone surrogates
        # that refuse UTF-8 encoding), and compare_digest on str raises
        # TypeError for non-ASCII. Any malformed field IS a verification
        # failure, never a crash on the connect path.
        try:
            expected_sig = _forwarder_identity_sig(key, inst.id, pid, start, port)
            sig_ok = hmac.compare_digest(sig.encode("utf-8"), expected_sig.encode("utf-8"))
        except (TypeError, ValueError, UnicodeError):
            sig_ok = False
        if not sig_ok:
            logger.warning(
                "Recorded forwarder identity for %s failed signature "
                "verification; refusing reclaim (registry edited outside the "
                "gateway?)",
                inst.id,
            )
            return
        # Defense in depth behind the MAC, from gateway-/kernel-owned facts: a
        # pid this manager is CURRENTLY supervising is never a leak candidate,
        # and a genuine hard-kill orphan has been reparented to init — a
        # forwarder whose parent is still alive belongs to a running gateway
        # (this one or another), so it is refused no matter what the registry
        # says. Subreaper hosts read as non-orphaned and merely miss the
        # reclaim (fail closed, leak-not-mis-kill).
        live_pids = {t.pid for t in self._tunnels.values() if t.pid}
        if pid in live_pids:
            return
        if await asyncio.to_thread(platform_compat.get_ppid, pid) != 1:
            return
        if await asyncio.to_thread(_is_port_free, port):
            return  # nothing holds the recorded port — nothing leaked to reclaim
        if params.method == "ssm":
            expected = _build_ssm_tunnel_argv(
                params.ssm_target,
                port,
                inst.remote_port,
                profile=params.aws_profile,
                region=params.aws_region,
            )
        else:
            expected = _build_ssh_tunnel_argv(
                params.ssh_host, port, inst.remote_port, compression=self._ssh_compression
            )
        outcome = await asyncio.to_thread(
            _verify_and_reclaim_forwarder,
            pid,
            start,
            expected,
            port,
            params.method == "ssm",
            f"instance={inst.id} pid={pid} port={port} transport={params.method}",
        )
        if outcome == "reclaimed":
            logger.info(
                "Reclaimed leaked %s forwarder pid %d for %s (released port %d)",
                params.method,
                pid,
                inst.id,
                port,
            )
        elif outcome == "identity_mismatch":
            logger.info(
                "Recorded %s forwarder pid %d for %s no longer matches its "
                "recorded identity (recycled pid, or the rebuilt command line "
                "drifted); leaving it alone (#1972) — port %d stays excluded "
                "from allocation",
                params.method,
                pid,
                inst.id,
                port,
            )
        elif outcome == "recycled_during_grace":
            logger.warning(
                "Withheld SIGKILL for %s forwarder pid %d of %s: the pid "
                "stopped matching its recorded identity during the term grace "
                "(recycled); port %d stays excluded from allocation",
                params.method,
                pid,
                inst.id,
                port,
            )
        else:  # "not_gone"
            logger.warning(
                "Leaked %s forwarder pid %d for %s (or a group member holding "
                "port %d) did not exit within the reclaim grace; leaving it "
                "for the next connect (the port stays excluded from allocation)",
                params.method,
                pid,
                inst.id,
                port,
            )

    def _connect_timeout_for(self, method: str) -> float:
        """Readiness timeout for *method*, honoring an explicit caller override.

        SSM's ``session-manager-plugin`` has to complete a WebSocket handshake
        with the SSM service before it binds the local port, which routinely
        takes longer than a direct ssh TCP connect — so the SSM default is
        higher. A caller that passed an explicit ``connect_timeout_secs``
        (tests, tuning) wins for both transports.
        """
        if self._connect_timeout is not None:
            return self._connect_timeout  # explicit override
        if method == CONNECTION_METHOD_SSM:
            return _DEFAULT_SSM_CONNECT_TIMEOUT_SECS
        if method == CONNECTION_METHOD_LOOPBACK:
            return _DEFAULT_LOOPBACK_CONNECT_TIMEOUT_SECS
        return _DEFAULT_CONNECT_TIMEOUT_SECS

    def _mint_timeout_for(self, method: str) -> float:
        """Token-mint timeout for *method*, honoring an explicit override.

        Mirrors :meth:`_connect_timeout_for`: the SSM mint dispatches
        ``aws ssm send-command`` and polls ``get-command-invocation``, whose
        dispatch latency (agent poll interval) makes its default higher. A
        caller that passed an explicit ``mint_timeout_secs`` (config, tests)
        wins for both transports — including a value equal to either
        transport's default.
        """
        if self._mint_timeout is not None:
            return self._mint_timeout  # explicit override
        if method == CONNECTION_METHOD_SSM:
            return _DEFAULT_SSM_MINT_TIMEOUT_SECS
        if method == CONNECTION_METHOD_LOOPBACK:
            return _DEFAULT_LOOPBACK_MINT_TIMEOUT_SECS
        return _DEFAULT_MINT_TIMEOUT_SECS

    def _loopback_allowed(self) -> bool:
        """Whether the pod-only loopback verification seam is enabled.

        ``KIROCREW_POD=1`` is the outer boundary. The config flag and the test
        override are second keys inside that boundary; neither can make a
        product gateway honor a loopback record.
        """
        if os.environ.get("KIROCREW_POD") != "1":
            return False
        if self._allow_loopback is not None:
            return self._allow_loopback
        try:
            return bool(KiroCrewConfig.load().instances.allow_loopback_transport)
        except Exception:
            # Fail CLOSED: an unreadable config is not consent.
            logger.warning("Could not read instances.allow_loopback_transport; refusing loopback")
            return False

    def _resolve_transport(self, inst: Instance) -> _TransportParams:
        """Validate + resolve *inst*'s transport params immediately before use.

        Raises :class:`SshValidationError` / :class:`SsmValidationError` /
        :class:`LoopbackValidationError` so each caller can surface a clean
        per-instance error. Validation happens here — right before a command
        line is built — rather than trusting the registry's lighter early-reject
        charset checks.

        The loopback transport is refused unless the process carries the exact
        pod marker and the pod config enables the verification seam. This is the
        authoritative gate: the API check runs only on writes through that
        surface, while ``instances.json`` is agent-writable and a hand-written
        record can reach the manager directly.

        Callers on the event loop offload this through :func:`asyncio.to_thread`:
        the policy reads the config file synchronously via
        :meth:`_loopback_allowed`, which must not block the loop.
        """
        method = (inst.connection_method or CONNECTION_METHOD_SSH).strip().lower()
        if method == CONNECTION_METHOD_SSM:
            return _TransportParams(
                method=CONNECTION_METHOD_SSM,
                ssm_target=validate_ssm_target(inst.ssm_target),
                aws_profile=validate_aws_profile(inst.aws_profile),
                aws_region=validate_aws_region(inst.aws_region),
                ssm_run_as=validate_ssm_run_as(inst.ssm_run_as),
                remote_bin=validate_remote_bin(inst.remote_bin),
            )
        if method == CONNECTION_METHOD_LOOPBACK:
            if not self._loopback_allowed():
                raise LoopbackValidationError(
                    "the loopback transport is a pod-only verification seam. It requires "
                    "the Kiro Crew pod runtime and "
                    "instances.allow_loopback_transport=true inside that pod."
                )
            return _TransportParams(method=CONNECTION_METHOD_LOOPBACK)
        return _TransportParams(
            method=CONNECTION_METHOD_SSH,
            ssh_host=validate_ssh_host(inst.ssh_host),
            remote_bin=validate_remote_bin(inst.remote_bin),
        )

    async def _mint_for(self, inst: Instance, params: _TransportParams) -> str:
        """Mint a dashboard token for *inst* over its configured transport.

        The SSH path goes through the injectable ``self._mint_token`` seam (kept
        so the existing tests can substitute a fake mint); the SSM path calls
        :func:`mint_remote_token_ssm`; the loopback path calls
        :func:`mint_loopback_token`, which needs no remote shell at all. Never
        logs the token.
        """
        if params.method == CONNECTION_METHOD_SSM:
            return await mint_remote_token_ssm(
                params.ssm_target,
                aws_profile=params.aws_profile,
                aws_region=params.aws_region,
                ssm_run_as=params.ssm_run_as,
                remote_bin=params.remote_bin,
                ttl=inst.ttl,
                remote_port=inst.remote_port,
                embed_parent_port=self._parent_port,
                timeout_secs=self._mint_timeout_for(params.method),
            )
        if params.method == CONNECTION_METHOD_LOOPBACK:
            return await mint_loopback_token(
                inst.remote_port,
                own_port=self._parent_port,
                own_bind_host=self._parent_bind_host,
                ttl=inst.ttl,
                embed_parent_port=self._parent_port,
                timeout_secs=self._mint_timeout_for(params.method),
            )
        return await self._mint_token(
            params.ssh_host,
            remote_bin=params.remote_bin,
            ttl=inst.ttl,
            remote_port=inst.remote_port,
            embed_parent_port=self._parent_port,
            timeout_secs=self._mint_timeout_for(params.method),
        )

    async def _acquire_local_port(
        self,
        *,
        also_exclude: int | None = None,
    ) -> int:
        """Resolve the loopback port the embedded pane will load from.

        All three transports give the embedded pane a hub-owned loopback port.
        The allocator finds a free port and re-probes it. SSH and SSM hand that
        port to their forwarder child; loopback binds it in-process and relays to
        the destination gateway's separate, already-listening port.

        ``also_exclude`` keeps one extra port out of the allocation on top of
        the reserved set. A rebuild passes the port it just freed, so every
        transport gets a fresh hub-owned pane port and escapes a cause bound to
        the old one.

        Raises :class:`RuntimeError` with a caller-facing message, which is what
        ``connect`` turns into an ERROR status.
        """
        # Allocate a free loopback port for the forward. It deliberately does
        # NOT have to equal ``inst.remote_port``. The embedded dashboard runs
        # in an iframe at http://127.0.0.1:<local_port>, and the remote
        # gateway accepts that because:
        #   * ``check_origin`` has a same-origin loopback branch — a loopback
        #     Origin equal to the request's own Host is trusted at ANY port,
        #     which is exactly the shape the iframe produces (it is served at
        #     127.0.0.1:<local_port> and calls that same location.host); and
        #   * ``build_allowed_hosts`` compares hostname only, so the Host
        #     header matches regardless of port; and
        #   * the session cookie is named from the browser-facing port
        #     (``_cookie_port_from_host``), so distinct local ports get
        #     distinct cookies instead of colliding in the shared 127.0.0.1
        #     jar — that helper exists precisely for tunnels whose local port
        #     differs from the remote's.
        # This does not reopen CSE SEC-016: a malicious local page on an
        # arbitrary port sends its own Origin while the Host stays the
        # gateway's, so the two differ and the same-origin branch rejects it.
        # Browsers forbid scripts from forging either header.
        #
        # Mirroring the remote port is not an option the shipped defaults
        # allow: a stock gateway binds the same default port on both ends, so a
        # stock hub already holds the port a stock remote reports, and two
        # stock installs could never connect under that rule.
        #
        # Every instance's recorded port stays reserved, and the allocator
        # probes each candidate, so a port anything still holds — including a
        # leftover forwarder of our own — is skipped rather than fought over.
        # That skip is why no orphan-reaping step is needed for connect to
        # make PROGRESS: nothing has to be killed to get a working tunnel.
        #
        # The cost that skip alone would carry: an ``ssh -N -L`` child
        # orphaned by a gateway hard-kill keeps its loopback port and its
        # session to the remote until the OS reaps it. That leak is now
        # reclaimed by ``_reclaim_orphan_forwarder`` above — by the child's
        # RECORDED pid behind a strict exact-argv identity check, never by
        # scanning the process table. Argv-pattern matching over the process
        # table is off the table because it can SIGTERM a forward the operator
        # opened themselves; an unrecorded or unverified process is therefore
        # left alone, and allocation simply skips its port.
        #
        # There is deliberately no "take my own previous port back" branch.
        # It reads as free stability, but the case it fires in cannot benefit:
        # ``disconnect`` zeroes the port, while ``shutdown`` documents that it
        # "Leaves registry hints intact", so the recorded port survives a
        # gateway RESTART rather than only a crash — and after any restart the
        # token is re-minted and the pane reloads, so there is no iframe
        # origin or ``mc_token_<port>`` cookie left to keep stable. The
        # in-session case that genuinely wants the same port is already served
        # by ``_recover``, which reuses ``current.status.local_port``.
        #
        # Everything here runs off the event loop: ``_reserved_ports`` reads
        # the registry from disk under its own lock, and the port probe binds
        # a socket. Under the mirror neither happened on this path -- the port
        # was a fixed field read and one probe -- whereas this reads a file
        # and can walk upward past every occupied candidate, all inside the
        # manager lock on the gateway's loop, where a synchronous scan would
        # stall unrelated requests and heartbeats. This matches how the rest
        # of the module already reaches the registry (``asyncio.to_thread``).
        reserved = await asyncio.to_thread(self._reserved_ports)
        if also_exclude is not None:
            reserved = set(reserved) | {also_exclude}
        local_port: int = await asyncio.to_thread(self._allocator.allocate, exclude=reserved)

        # The probe above is advisory — there is an inherent TOCTOU window
        # between probing and ssh actually binding — so re-check immediately
        # before spawning and fail with an actionable message rather than
        # letting the child exit on ExitOnForwardFailure.
        if not await asyncio.to_thread(_is_port_free, local_port):
            raise RuntimeError(
                f"local port {local_port} was taken while connecting. Retry; "
                f"if it keeps happening, disconnect whatever is holding port "
                f"{local_port} or move instances.tunnel_base_port to a "
                f"quieter range."
            )
        return local_port

    async def connect(
        self, instance_id: str, *, rebuild: bool = False, only_if_connected: bool = False
    ) -> TunnelStatus:
        """Open a tunnel + mint a token for *instance_id*; return its status.

        Idempotent: connecting an already-connected instance returns its current
        status. Raises :class:`KeyError` for an unknown instance, or surfaces a
        validation / mint / spawn error via the returned status (state ERROR).
        Works for either ``connection_method`` — the transport is resolved by
        :meth:`_resolve_transport`.

        ``rebuild=True`` breaks the idempotence on purpose: a CONNECTED tunnel is
        torn down first (``keep_intent`` — the user is asking for the crew, not
        turning it off) and a fresh forwarder is spawned on a DIFFERENT local
        port: the port just freed is excluded from the allocation, so both a
        stalled stream on the old forwarder and a cause bound to the old port
        itself are escaped by one Retry. This is the pane's Retry after a load watchdog
        fired on a document that DID navigate: the transport is up by every
        probe the manager runs (``/api/health`` answers, the credential
        validates), yet one stream inside it stalled and the pane's module
        graph will wait on it forever. Nothing short of a new TCP path clears
        that, and the plain connect — which sees CONNECTED and returns — would
        hand the same stalled tunnel back.

        A teardown that fails (the stop raises) is reported as an ERROR status,
        the same way every other connect failure is: the live tunnel is left
        exactly as :meth:`_teardown_locked` leaves it (intact — nothing is
        removed unless the stop succeeded), the reason is retained for
        :meth:`last_error`, and the caller gets a 502 with a message instead of
        a propagated exception turned into an unexplained 500.

        ``only_if_connected=True`` is the opposite restriction: answer a
        CONNECTED tunnel exactly like the plain connect (its status, its cached
        token) but, when the tunnel is not up, spawn nothing and touch nothing
        -- return a DISCONNECTED status and leave ``was_connected`` as the user
        last set it. This is the viewport's auto-warm, whose job is to pre-mount
        panes for tunnels that are ALREADY up, never to bring one up. The check
        runs under the manager lock, the same lock :meth:`disconnect` holds
        while it stops a tunnel, so an auto-warm racing a disconnect either sees
        the tunnel still CONNECTED (and warms a pane the disconnect's
        ``removeWarm`` then drops) or sees it gone and stands down; it can never
        re-open a tunnel the user just closed, which a probe-then-connect from
        the browser could.
        """
        if rebuild and only_if_connected:
            raise ValueError("rebuild and only_if_connected are mutually exclusive")
        async with self._lock:
            inst = await asyncio.to_thread(self._registry.get, instance_id)
            if inst is None:
                raise KeyError(f"no instance with id {instance_id!r}")

            existing = self._tunnels.get(instance_id)
            # The port a rebuild tears down. Kept out of the allocation below so
            # the new forwarder lands on a DIFFERENT local port: the field
            # evidence has every stall on the first allocated port, so a cause
            # bound to the port itself (a stale listener, a local firewall or
            # proxy rule) is a live hypothesis alongside the stalled stream. A
            # first-free allocator would hand the just-freed port straight back
            # and Retry could loop on it forever with no in-product escape.
            rebuild_freed_port: int | None = None
            if only_if_connected and (
                existing is None or existing.status.state != TunnelState.CONNECTED
            ):
                logger.info(
                    "Connected-only connect for %s declined: tunnel is %s",
                    instance_id,
                    existing.status.state.value if existing is not None else "absent",
                )
                return TunnelStatus(
                    instance_id=inst.id,
                    state=TunnelState.DISCONNECTED,
                    local_port=inst.local_port,
                    remote_port=inst.remote_port,
                )
            if rebuild and existing is not None:
                logger.info(
                    "Rebuilding tunnel for %s on request (was %s on 127.0.0.1:%s)",
                    instance_id,
                    existing.status.state.value,
                    existing.status.local_port,
                )
                try:
                    await self._teardown_locked(instance_id, keep_intent=True)
                except Exception as e:  # noqa: BLE001 - reported, not swallowed
                    logger.warning(
                        "Rebuild of %s could not stop the old tunnel: %s", instance_id, e
                    )
                    return self._error_status(
                        inst,
                        f"could not stop the existing tunnel to rebuild it: {e}. "
                        f"Disconnect and connect again, or retry.",
                    )
                if existing.status.local_port:
                    rebuild_freed_port = existing.status.local_port
                existing = None
                # The registry row was just rewritten (local_port reset); re-read
                # so the allocation below skips nothing stale and records fresh.
                inst = await asyncio.to_thread(self._registry.get, instance_id)
                if inst is None:
                    raise KeyError(f"no instance with id {instance_id!r}")
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
                params = await asyncio.to_thread(self._resolve_transport, inst)
            except (SshValidationError, SsmValidationError, LoopbackValidationError) as e:
                return self._error_status(inst, f"invalid {inst.connection_method} settings: {e}")

            # SSM needs the local session-manager-plugin; fail with an actionable
            # message rather than letting the child exit with a cryptic error.
            #
            # Probed in a worker thread: the probe resolves the plugin through the
            # deploy engine's shared resolver, which scans PATH, then the
            # well-known install dirs, then routes a fallback-dir hit through
            # executable-provenance validation — filesystem work that must not run
            # on the gateway event loop, where a stalled network mount would freeze
            # every request and heartbeat. Same reason _build_argv is offloaded
            # below, and the same thing the dashboard's own cloud handler does with
            # this exact call.
            if params.method == CONNECTION_METHOD_SSM:
                if not await asyncio.to_thread(cloud_ssm.session_manager_plugin_installed):
                    return self._error_status(inst, cloud_ssm.session_manager_plugin_install_hint())

            # Reclaim our own forwarder if a prior gateway hard-kill leaked it
            # still holding this instance's recorded port. Keyed on the recorded
            # pid behind a strict exact-argv identity check — see the method for
            # why nothing else is ever signalled. Best-effort: allocation below
            # skips the recorded port whether or not the reclaim succeeded.
            await self._reclaim_orphan_forwarder(inst, params)

            # Resolve the loopback port the embedded pane will load from. Every
            # transport gets a hub-owned allocation: ssh/ssm bind it in their
            # forwarder child, while loopback binds it in-process and relays to
            # the destination's separate port. A rebuild also excludes the port
            # it just freed. See :meth:`_acquire_local_port`.
            try:
                local_port = await self._acquire_local_port(also_exclude=rebuild_freed_port)
            except RuntimeError as e:
                return self._error_status(inst, str(e))

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
                own_port=self._parent_port,
                own_bind_host=self._parent_bind_host,
                **params.tunnel_kwargs(),
            )
            self._tunnels[instance_id] = tunnel
            self._tunnel_epoch[instance_id] = self._tunnel_epoch.get(instance_id, 0) + 1
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

            # Persist hints: port assignment, forwarder identity (pid + start
            # time), was_connected, last-active — ONE
            # read-modify-rewrite of instances.json (fsync), so the set is
            # durable together and the manager lock is held for a single fsync
            # round-trip. The identity pair is what a later connect uses to
            # reclaim this child if a gateway hard-kill orphans it; a start
            # time that cannot be read persists as "" and simply disables the
            # reclaim for this child (fail closed).
            # _persist_hint runs it off the loop and does not
            # return — even under cancellation — until the write completes, so
            # the lock cannot release while the worker write is still in
            # flight (a late hint write would race a subsequent disconnect).
            forwarder_pid = tunnel.pid or _NO_FORWARDER_PID
            forwarder_start = ""
            forwarder_sig = ""
            if forwarder_pid > 0:
                started = await asyncio.to_thread(platform_compat.process_start_time, forwarder_pid)
                forwarder_start = started or ""
            if forwarder_pid > 0 and forwarder_start:
                key = await asyncio.to_thread(_reclaim_identity_key)
                if key is not None:
                    forwarder_sig = _forwarder_identity_sig(
                        key, instance_id, forwarder_pid, forwarder_start, local_port
                    )
            await self._persist_hint(
                self._registry.update,
                instance_id,
                mark_last_active=True,
                local_port=local_port,
                forwarder_pid=forwarder_pid,
                forwarder_start=forwarder_start,
                forwarder_sig=forwarder_sig,
                was_connected=True,
            )
            # A successful (re)connect clears any stale give-up counter so the next
            # unexpected drop gets a full fresh recovery budget instead of tripping
            # the cap immediately.
            self._recover_attempts.pop(instance_id, None)
            # Connected cleanly — drop any retained failure reason from a prior
            # attempt so status() does not report a stale error.
            self._last_error.pop(instance_id, None)
            return tunnel.status

    async def disconnect(self, instance_id: str, *, keep_intent: bool = False) -> bool:
        """Tear down *instance_id*'s tunnel, drop its token, clear its port hint.

        Returns whether a live tunnel existed.

        ``keep_intent`` distinguishes a RECONFIGURATION from a user disconnect.
        ``was_connected`` records that the user wants this instance connected, so
        only an explicit disconnect may clear it; a caller tearing a tunnel down
        in order to rebuild it (an edit that changes the host or port) passes
        ``keep_intent=True`` and leaves that flag alone. Restoring the flag
        afterwards instead would race a real disconnect arriving mid-edit and
        silently revive the instance the user just turned off.

        The persisted ``local_port`` is reset to the unallocated sentinel here —
        symmetric with :meth:`connect` setting it — so a disconnected instance
        never leaves a stale port recorded. Without this the freed port reads as
        perpetually reserved (``_reserved_ports`` / the ``local_port == 0``
        "unallocated" contract), and the instance can't be reconnected. The
        registry cleanup runs even when no live tunnel is tracked, so a port left
        behind by an unclean prior exit can still be cleared by a disconnect.
        """
        async with self._lock:
            return await self._teardown_locked(instance_id, keep_intent=keep_intent)

    async def _teardown_locked(self, instance_id: str, *, keep_intent: bool) -> bool:
        """The body of :meth:`disconnect`, for callers already holding the lock.

        Reconfiguration needs the teardown and the coordinate rewrite to happen
        inside ONE critical section, so it cannot call the public method without
        deadlocking on our own non-reentrant lock.
        """
        # Stop FIRST, discard after. A stop that raises leaves the forward alive,
        # and everything below is what makes that forward usable: its token, its
        # refresh task, its place in `_tunnels`. Clearing any of it before the
        # process is really down leaves a live tunnel with no credential (session
        # transfer then reports `transfer_no_credential`) or, worse, an untracked
        # process holding the port. Nothing is removed unless the stop succeeded.
        tunnel = self._tunnels.get(instance_id)
        if tunnel is not None:
            await tunnel.stop()
        self._tunnels.pop(instance_id, None)
        self._tokens.pop(instance_id, None)
        self._recover_attempts.pop(instance_id, None)
        self._last_error.pop(instance_id, None)
        # Awaited, not just signalled: a refresh already inside its mint would
        # otherwise finish afterwards and store a token for a tunnel this teardown
        # has already removed. Ordered after the stop for the same reason as the
        # token itself — a rejected edit must leave the live tunnel intact.
        await self._cancel_token_refresh_and_wait(instance_id)
        # Clear the lazy-reconnect hint AND the recorded local port together
        # (one atomic write). local_port must return to the unallocated
        # sentinel so the now-free port is not treated as reserved forever,
        # and the forwarder pid goes with it: the child was just stopped, so a
        # retained pid would eventually be recycled by the OS and point a later
        # reclaim at a stranger (the exact-argv guard would refuse it, but a
        # cleared hint never even asks). _persist_hint runs the
        # read-modify-rewrite off the loop and does
        # not return — even if this handler is cancelled (e.g. aiohttp
        # aborting at shutdown) — until the write completes: the in-memory
        # teardown above is already done, so abandoning the persisted reset
        # would leave was_connected=True plus a stale local_port, reviving
        # an instance the user disconnected and pinning the freed port.
        hints: dict[str, object] = {
            "local_port": _UNALLOCATED_PORT,
            "forwarder_pid": _NO_FORWARDER_PID,
            "forwarder_start": "",
            "forwarder_sig": "",
        }
        if not keep_intent:
            hints["was_connected"] = False
        await self._persist_hint(self._registry.update, instance_id, **hints)
        return tunnel is not None

    def _track_recovery(self, instance_id: str, task: asyncio.Task) -> None:  # type: ignore[type-arg]
        """Retain a background task so it is not GC'd, indexed by instance."""
        self._recovery_tasks.add(task)
        per = self._recovery_by_instance.setdefault(instance_id, set())
        per.add(task)

        def _done(t: asyncio.Task) -> None:  # type: ignore[type-arg]
            self._recovery_tasks.discard(t)
            bucket = self._recovery_by_instance.get(instance_id)
            if bucket is not None:
                bucket.discard(t)
                if not bucket:
                    self._recovery_by_instance.pop(instance_id, None)

        task.add_done_callback(_done)

    async def _cancel_token_refresh_and_wait(self, instance_id: str) -> None:
        """Cancel this instance's refresh loop and WAIT for it to unwind.

        ``_cancel_token_refresh`` only signals. A refresh already inside its mint
        would otherwise finish afterwards and store a token minted from the
        pre-edit coordinates against the tunnel the edit rebuilt — the embedded
        dashboard would then be handed a credential the new remote never issued.
        """
        task = self._refresh_tasks.get(instance_id)
        self._cancel_token_refresh(instance_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def _cancel_recovery(self, instance_id: str) -> None:
        """Cancel and AWAIT this instance's in-flight recovery.

        Self-heal reads the instance record before it takes the lock, so a
        recovery already in flight holds the PRE-edit coordinates. Letting it
        proceed would reinstall a tunnel to the old machine after the edit
        landed — and because ``connect()`` is idempotent, that tunnel would then
        be handed out for the new settings. Awaiting the cancellation is the
        point: returning while the task is still unwinding would leave exactly
        the race this closes.
        """
        tasks = list(self._recovery_by_instance.get(instance_id, ()))
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        # A cancelled recovery is expected to raise CancelledError; anything else
        # it raises is its own business and already logged by its done-callback.
        await asyncio.gather(*tasks, return_exceptions=True)

    async def reconfigure(self, instance_id: str, apply: Callable[[], _T]) -> _T:
        """Tear the tunnel down and rewrite its coordinates as ONE operation.

        Editing the host/port/transport of a live instance is two steps that must
        not be observed apart: with the lock released between them, a ``connect``
        can read the OLD record, and whether its tunnel is already CONNECTED or
        still CONNECTING when the write lands decides whether any after-the-fact
        sweep notices it. Holding the lock across both removes the window instead
        of narrowing it — a racing ``connect`` either completes before this (and
        is torn down here) or starts after (and reads the new coordinates).

        ``apply`` performs the persistence and returns whatever the caller needs;
        it runs on a worker thread because the registry write is blocking, so the
        event loop is not held while the lock is.
        """
        # The barrier goes up FIRST, with no await before it, so no recovery can
        # be scheduled for this instance from here on (see `_reconfiguring`).
        # Cancellation then runs OUTSIDE the lock, because a recovery task may
        # itself be waiting for that lock and awaiting it while holding the lock
        # would deadlock.
        self._reconfiguring.add(instance_id)
        try:
            # Self-heal rebuilds a tunnel from the record it read, so it is
            # stopped and unwound before the coordinates move. The refresh loop is
            # unwound by the teardown instead — AFTER the stop succeeds — because a
            # rejected edit must leave the live tunnel with its credential and its
            # refresh intact. The barrier already prevents either from restarting.
            await self._cancel_recovery(instance_id)
            return await self._reconfigure_locked(instance_id, apply)
        finally:
            self._reconfiguring.discard(instance_id)

    async def _reconfigure_locked(self, instance_id: str, apply: Callable[[], _T]) -> _T:
        """The locked half of :meth:`reconfigure` (barrier already raised)."""
        async with self._lock:
            # Deliberately NOT tolerant: a stop that failed leaves the old forward
            # live, so persisting the new coordinates would leave the record
            # pointing at one machine while the still-open tunnel serves another —
            # and that tunnel is the one the user would reach. The edit aborts,
            # the tunnel stays tracked (see _teardown_locked), and the caller is
            # told to disconnect and retry.
            await self._teardown_locked(instance_id, keep_intent=True)
            # The write is shielded: if this request's task is cancelled (the
            # client hung up), the worker thread keeps going regardless, and an
            # unshielded await would unwind the `async with` and release the lock
            # while that write was still in flight — letting a concurrent connect
            # read the pre-edit coordinates. Awaiting it out under the lock keeps
            # the critical section honest, then the cancellation propagates.
            write = asyncio.ensure_future(asyncio.to_thread(apply))
            try:
                return await asyncio.shield(write)
            except asyncio.CancelledError:
                await asyncio.wait({write})
                raise

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
        if instance_id in self._reconfiguring:
            # Its coordinates are being rewritten; whatever this recovery read
            # would already be stale. The reconfiguration tears the tunnel down
            # itself, and the user reconnects against the new record.
            logger.info("Skipping self-heal for %s: reconfiguration in progress", instance_id)
            return
        delay = _recover_backoff_secs(
            self._recover_attempts.get(instance_id, 0) + 1, self._recover_backoff_max
        )
        task = asyncio.create_task(self._recover_after(instance_id, delay))
        self._track_recovery(instance_id, task)
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
        if instance_id in self._reconfiguring:
            # Scheduled just before the barrier went up, or the barrier rose
            # during the backoff: either way this attempt holds stale coordinates.
            return
        await self._recover(instance_id)

    async def _restore_for_recovery(
        self,
        inst: Instance,
        params: _TransportParams,
        current: _SshTunnel,
        local_port: int,
    ) -> bool:
        """Restore one unhealthy tunnel according to its transport contract."""
        if params.method == CONNECTION_METHOD_LOOPBACK:
            if self._tunnels.get(inst.id) is not current:
                return False
            return await current.rearm_loopback()
        return await self._rebuild(inst, params, local_port)

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
            own_port=self._parent_port,
            own_bind_host=self._parent_bind_host,
            **params.tunnel_kwargs(),
        )
        self._tunnels[inst.id] = tunnel
        # A self-heal reinstall is a new generation too: a mint in flight against
        # the tunnel this one replaces must not land on it.
        self._tunnel_epoch[inst.id] = self._tunnel_epoch.get(inst.id, 0) + 1
        return await tunnel.start()

    async def _mark_recovered(self, instance_id: str) -> None:
        """Reset the attempt counter and persist the hints, under lock, iff tracked.

        A rebuild replaced the tunnel child, so the recorded forwarder
        identity (``forwarder_pid`` + ``forwarder_start``) must move with
        ``was_connected`` — a stale identity would point a later hard-kill
        reclaim at a process that does not exist (harmless, the identity
        check refuses it) while the ACTUAL replacement child leaked
        unrecorded. All hints go in one write.

        The persist stays INSIDE the manager lock so write order equals
        lock-acquisition order: a concurrent :meth:`disconnect`'s
        ``was_connected=False`` (also written under the lock) can never be
        overwritten by this recovery write landing late. The tracked-check
        gates the persist — an instance the user disconnected must not be
        re-marked auto-reconnectable. ``_persist_hint`` keeps the lock held
        until the worker write completes even under cancellation; a cancelled
        bare ``to_thread`` await would NOT stop the already-running thread, so
        its write could land after the lock released and break the ordering.
        """
        async with self._lock:
            tunnel = self._tunnels.get(instance_id)
            if tunnel is None:
                return
            self._recover_attempts[instance_id] = 0
            forwarder_pid = tunnel.pid or _NO_FORWARDER_PID
            forwarder_start = ""
            forwarder_sig = ""
            if forwarder_pid > 0:
                started = await asyncio.to_thread(platform_compat.process_start_time, forwarder_pid)
                forwarder_start = started or ""
            if forwarder_pid > 0 and forwarder_start:
                key = await asyncio.to_thread(_reclaim_identity_key)
                if key is not None:
                    forwarder_sig = _forwarder_identity_sig(
                        key,
                        instance_id,
                        forwarder_pid,
                        forwarder_start,
                        tunnel.status.local_port,
                    )
            await self._persist_hint(
                self._registry.update,
                instance_id,
                was_connected=True,
                forwarder_pid=forwarder_pid,
                forwarder_start=forwarder_start,
                forwarder_sig=forwarder_sig,
            )

    async def _recover(self, instance_id: str) -> None:
        """2-tier self-heal for an unhealthy tunnel (either transport).

        Tier 1 restores the transport while reusing the existing token. SSH and
        SSM rebuild their forwarder child; loopback drops old relays and re-proves
        the destination behind its still-bound pane listener. Tier 2 re-mints the
        token, then repeats that transport-specific restore.
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
            inst = await asyncio.to_thread(self._registry.get, instance_id)
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
                params = await asyncio.to_thread(self._resolve_transport, inst)
            except (SshValidationError, SsmValidationError, LoopbackValidationError) as e:
                logger.warning("Self-heal aborted for %s: %s", instance_id, e)
                return

            local_port = current.status.local_port or inst.local_port
            if params.method == CONNECTION_METHOD_LOOPBACK:
                # Same pane listener, new destination generation. A token mint
                # that began against the failed owner must not publish after the
                # re-proof adopts its replacement.
                self._tunnel_epoch[instance_id] = self._tunnel_epoch.get(instance_id, 0) + 1

        # Phase 2 — slow remote I/O WITHOUT the lock.
        # Tier 1 — restore the transport, reusing the existing token.
        logger.info(
            "Self-heal tier 1 (restore transport) for %s [attempt %d]", instance_id, attempts
        )
        if await self._restore_for_recovery(inst, params, current, local_port):
            await self._mark_recovered(instance_id)
            logger.info("Self-heal tier 1 succeeded for %s", instance_id)
            return

        # Tier 2 — re-mint the dashboard token, then restore the transport.
        logger.info("Self-heal tier 2 (re-mint token) for %s", instance_id)
        try:
            token = await self._mint_for(inst, params)
        except TokenMintError as e:
            logger.warning("Self-heal re-mint failed for %s: %s", instance_id, e)
            return
        async with self._lock:
            if self._tunnels.get(instance_id) is not current:
                return  # disconnected or replaced while minting — discard
            self._store_token(instance_id, token, inst.ttl)
            self._schedule_token_refresh(instance_id)
        if await self._restore_for_recovery(inst, params, current, local_port):
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
        inst = await asyncio.to_thread(self._registry.get, instance_id)
        if inst is None:
            return None
        tunnel = self._tunnels.get(instance_id)
        local_port = (tunnel.status.local_port if tunnel else 0) or inst.local_port
        method = (inst.connection_method or CONNECTION_METHOD_SSH).strip().lower()
        if method == CONNECTION_METHOD_SSM:
            result = await diagnose_instance_ssm(
                inst.ssm_target,
                inst.remote_port,
                local_port,
                aws_profile=inst.aws_profile,
                aws_region=inst.aws_region,
                ssm_run_as=inst.ssm_run_as,
            )
        elif method == CONNECTION_METHOD_LOOPBACK:
            try:
                await asyncio.to_thread(self._resolve_transport, inst)
            except LoopbackValidationError as e:
                diag = {
                    "code": _LOOPBACK_UNAVAILABLE_CODE,
                    "ok": False,
                    "reason": str(e),
                    "probes": [],
                }
                if tunnel is not None:
                    tunnel.status.diagnosis = diag
                logger.info("Instance %s diagnosis: %s", instance_id, diag["code"])
                return diag
            result = await diagnose_instance_loopback(inst.remote_port, local_port)
        else:
            result = await diagnose_instance(
                inst.ssh_host,
                inst.remote_port,
                local_port,
                connect_timeout_secs=min(
                    self._connect_timeout_for("ssh"), _DIAGNOSTICS_CONNECT_TIMEOUT_CAP_SECS
                ),
            )
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
        restart the remote dashboard port bounces, so the local tunnel's
        health probe detects the drop and self-heals (Stage 2) — no manual
        reconnect needed. Returns ``{ok, message}``.

        Refused for the loopback transport. There is no remote: the destination
        is a gateway on this host, and restarting it means restarting either this
        very process or a sibling the operator started, which is their own
        ``kirocrew restart`` to run rather than something the dashboard should
        trigger on their behalf.
        """
        inst = await asyncio.to_thread(self._registry.get, instance_id)
        if inst is None:
            return {"ok": False, "message": "unknown instance"}
        try:
            params = await asyncio.to_thread(self._resolve_transport, inst)
        except (SshValidationError, SsmValidationError, LoopbackValidationError) as e:
            return {"ok": False, "message": f"invalid {inst.connection_method} settings: {e}"}
        if params.method == CONNECTION_METHOD_LOOPBACK:
            return {
                "ok": False,
                "message": (
                    "this instance is a gateway on this host, so there is no "
                    "remote to restart — run `kirocrew restart` against it "
                    "directly"
                ),
            }
        if params.method == CONNECTION_METHOD_SSM:
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
                connect_timeout_secs=self._mint_timeout_for(params.method),
            )
        if rc == 0:
            logger.info("Restarted remote gateway for %s", instance_id)
            return {"ok": True, "message": "remote gateway restart requested"}
        logger.warning("Remote restart for %s failed (rc=%s): %s", instance_id, rc, err)
        return {"ok": False, "message": err or f"restart exited {rc}"}

    def _schedule_diagnosis(self, instance_id: str) -> None:
        """Fire-and-forget a diagnosis run (tracked so it isn't GC'd)."""
        task = asyncio.create_task(self.diagnose(instance_id))
        self._track_recovery(instance_id, task)
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
        # The loopback transport dials an ordinary port on this host, so prove
        # RIGHT HERE that the listener is still the gateway this bearer belongs
        # to. This send is rare (once per connect / re-mint) and it is the one
        # that establishes a credential the caller then hands to the browser, so
        # it pays for a fresh proof rather than reading the probe-cycle cache.
        tunnel = self._loopback_tunnel_on(local_port)
        if tunnel is not None and not await tunnel.revalidate_loopback_owner():
            logger.info(
                "Refusing the liveness probe on loopback port %s: the listener "
                "is not provably this user's gateway",
                local_port,
            )
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

    def _loopback_tunnel_on(self, local_port: int) -> "_SshTunnel | None":
        """The LOOPBACK tunnel serving *local_port*, or ``None``.

        Resolves by port because the credential-bearing probe is addressed by
        port, not by instance. Every live tunnel's hub-owned pane port is unique
        because the allocator excludes all live and recorded pane ports, so at
        most one tunnel matches. Returning ``None`` for a forwarding transport
        is what keeps the ownership gate scoped to the transport that needs it.
        """
        for tunnel in self._tunnels.values():
            if tunnel.status.local_port == int(local_port):
                return tunnel if tunnel._transport == CONNECTION_METHOD_LOOPBACK else None
        return None

    def _peer_target(self, instance_id: str, path: str) -> tuple[str, str]:
        """Resolve ``(url, cookie_name)`` for one request to a CONNECTED peer.

        Owns the three rules that every peer request shares and that all of them
        depend on for correctness, so they are stated once instead of per caller:

        * the request is only ever attempted against a ``CONNECTED`` tunnel —
          none of these methods opens one, so a disconnected peer is a refusal,
          not a reconnect;
        * the target is always the loopback end of the already-open forward,
          never a peer-supplied host;
        * the cookie name is **port-scoped**. The dashboard keys its cookie on
          the port the CLIENT connected to (``token_auth._cookie_port_from_host``),
          not on the peer's own listen port, so two remotes both serving 7777
          through different forwards do not collide on one cookie. A bare
          ``mc_token`` is never read and would 403 every call.
        * a LOOPBACK peer must still be provably our own gateway. The verdict is
          the one the last health probe established
          (:meth:`_SshTunnel.loopback_ownership_ok`), so this costs no port
          lookup per request; a failed probe condemns the tunnel synchronously,
          which blocks every subsequent send here before the state machine has
          finished tearing the tunnel down.

        Raises :class:`_PeerUnavailable` instead of returning an error, because
        the callers' failure shapes differ (an exception for ``proxy_request``,
        an ``(ok, payload)`` tuple for the other two).
        """
        st = self.status(instance_id)
        if st is None or st.state is not TunnelState.CONNECTED:
            raise _PeerUnavailable("not_connected")
        tunnel = self._tunnels.get(instance_id)
        if tunnel is not None and not tunnel.loopback_ownership_ok():
            raise _PeerUnavailable("not_connected")
        local_port = st.local_port
        if local_port <= 0:
            raise _PeerUnavailable("no_credential")
        url = f"http://{_LOOPBACK}:{int(local_port)}/{path.lstrip('/')}"
        return url, f"mc_token_{int(local_port)}"

    def _peer_cookie_header(self, instance_id: str, cookie_name: str) -> dict[str, str]:
        """Build the ``Cookie`` header carrying this peer's credential.

        Must be re-read per attempt, not hoisted out of a retry loop: a re-mint
        replaces the credential mid-call and the retry exists to use the fresh
        one.

        **The token never leaves this object.** It travels as a cookie rather
        than a query parameter so it cannot land in the peer's HTTP access log,
        it is never logged here, and issuing the request from the manager is what
        keeps ``connect``/``refresh-token`` the only two routes where a minted
        token crosses the API boundary (instances.md §14.4).
        """
        token = self._tokens.get(instance_id, "")
        if not token:
            raise _PeerUnavailable("no_credential")
        return {"Cookie": f"{cookie_name}={token}"}

    @contextlib.asynccontextmanager
    async def proxy_request(
        self,
        instance_id: str,
        method: str,
        path: str,
        *,
        params: "dict[str, str] | None" = None,
        data: bytes | None = None,
        content_type: str = "",
    ):
        """Open *path* on a connected peer's gateway; yield the live response.

        The generic carrier for remote-crew chat (design: remote-crew-chat).
        Runs entirely over the already-open forward — **no SSH spawn** — and
        follows :meth:`search_sessions_remote`'s credential rules: the token
        never leaves this object, it travels as the port-scoped cookie so it
        cannot land in the peer's access log, and a 401/403 gets exactly one
        transparent re-mint retry.

        Yields the **un-buffered** ``aiohttp.ClientResponse`` so the caller can
        pump a streaming body (a proxied chat turn streams SSE for minutes);
        the response and its session are closed when the context exits. The
        timeout is connect+read-idle rather than total for the same reason: a
        total cap would sever a long turn mid-stream. It is fixed here rather
        than offered as a parameter — the policy is a property of what this
        method is for, not a per-call choice.

        Failures raise :class:`ProxyRequestError` with a machine-readable
        ``code`` and a suggested ``http_status``, so the route handler can
        translate without string-matching.
        """

        def _unavailable(exc: _PeerUnavailable) -> ProxyRequestError:
            if exc.kind == "not_connected":
                return ProxyRequestError("proxy_peer_not_connected", exc.message, http_status=503)
            return ProxyRequestError("proxy_no_credential", exc.message, http_status=503)

        try:
            url, cookie_name = self._peer_target(instance_id, path)
        except _PeerUnavailable as e:
            raise _unavailable(e) from None
        tmo = aiohttp.ClientTimeout(
            total=None,
            sock_connect=_PROXY_CONNECT_TIMEOUT,
            sock_read=_PROXY_READ_IDLE_TIMEOUT,
        )
        reminted = False
        while True:
            try:
                headers = self._peer_cookie_header(instance_id, cookie_name)
            except _PeerUnavailable as e:
                raise _unavailable(e) from None
            if content_type:
                headers["Content-Type"] = content_type
            session = aiohttp.ClientSession(timeout=tmo)
            try:
                resp = await session.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=headers,
                    # The tunnel endpoint is the ONLY legitimate target. A
                    # compromised peer answering 30x would otherwise make
                    # aiohttp fetch an attacker-chosen URL FROM THE HUB (SSRF
                    # into its loopback control planes).
                    allow_redirects=False,
                )
            except Exception as e:  # timeout, connection refused, etc.
                await session.close()
                logger.info(
                    "proxy_request to %s failed before a response (%s)",
                    instance_id,
                    type(e).__name__,  # never the token or the body
                )
                raise ProxyRequestError(
                    "proxy_peer_unreachable",
                    f"peer did not answer ({type(e).__name__})",
                    http_status=502,
                ) from None
            if resp.status in (401, 403):
                resp.release()
                await session.close()
                # One re-mint, then it is a credential failure — never streamed
                # to the caller as a bare peer 401, which would read as "the
                # chat endpoint said no" instead of "the tunnel credential is
                # not working" and lose the coded error the UI keys off.
                if not reminted and await self.refresh_token(instance_id):
                    reminted = True
                    continue  # retry once with the fresh credential
                raise ProxyRequestError(
                    "proxy_unauthorized", "peer rejected the credential", http_status=502
                )
            try:
                yield resp
            finally:
                resp.release()
                await session.close()
            return

    async def send_session_bundle(self, instance_id: str, bundle: dict) -> tuple[bool, dict]:
        """POST a session-transfer *bundle* to a connected instance's importer.

        Returns ``(ok, payload)``: on success *payload* is the peer's JSON reply
        (carrying the new session key); on failure it carries ``error`` and a
        machine-readable ``code`` so the caller can tell a stale token from an
        unreachable peer from a bundle the peer refused from a peer too old to
        have an importer at all.

        Runs entirely over the already-open forward — **no SSH spawn**, same as
        :meth:`token_validates`.

        **The token never leaves this object** — see
        :meth:`_peer_cookie_header`, which owns that rule for every peer request.
        """
        try:
            url, cookie_name = self._peer_target(instance_id, "/api/chat/slots/import")
        except _PeerUnavailable as e:
            return False, {
                "error": e.message,
                "code": (
                    "transfer_peer_not_connected"
                    if e.kind == "not_connected"
                    else "transfer_no_credential"
                ),
            }
        timeout = aiohttp.ClientTimeout(total=_TRANSFER_TIMEOUT)
        # Two INDEPENDENT one-shot retries, tracked by flag rather than by loop
        # index so neither consumes the other's budget:
        #  * ``reminted`` -- a retained credential can go stale while the tunnel
        #    stays CONNECTED (the condition ``token_validates`` exists for: a
        #    failed self-heal re-mint, or a remote restart that invalidates
        #    credentials). One fresh mint turns that into a transparent success.
        #  * ``downgraded`` -- an older peer refuses bundle_version 2; resend the
        #    transcript-only v1 shape it has always accepted.
        # Bounded at 3 attempts so at most one of each can fire plus the original.
        reminted = False
        downgraded = False
        for _attempt in range(3):
            try:
                headers = self._peer_cookie_header(instance_id, cookie_name)
            except _PeerUnavailable as e:
                return False, {"error": e.message, "code": "transfer_no_credential"}
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=bundle, headers=headers) as resp:
                        try:
                            payload = await resp.json()
                        except Exception:
                            payload = {}
                        if 200 <= resp.status < 300:
                            return True, payload if isinstance(payload, dict) else {}
                        if resp.status in (401, 403):
                            if not reminted and await self.refresh_token(instance_id):
                                reminted = True
                                continue  # retry once with the fresh credential
                            return False, {
                                "error": "peer rejected the credential",
                                "code": "transfer_unauthorized",
                            }
                        if resp.status in (404, 405):
                            # A peer with no importer route cannot receive a
                            # session at all, and says so in two different ways
                            # depending on its routing table: 404 when nothing
                            # matches, 405 when the path falls through to
                            # ``/api/chat/slots/{slot}`` (registered GET/DELETE
                            # only) and aiohttp reports the method instead.
                            # Neither is a status the importer itself ever
                            # returns, so both mean the same actionable thing —
                            # surface that rather than a bare status code the
                            # user cannot act on.
                            return False, {
                                "error": (
                                    "instance is running an older Kiro Crew that cannot "
                                    "receive sessions — update it, then reconnect"
                                ),
                                "code": "transfer_peer_too_old",
                            }
                        # Forward the peer's own code when it sent one: a version
                        # mismatch or an oversized bundle is actionable, and
                        # rewriting it here would erase that.
                        code = payload.get("code") if isinstance(payload, dict) else None
                        # An OLDER peer refuses bundle_version 2 outright, even
                        # though its Layer B is purely additive. Downgrade once
                        # and resend the transcript-only v1 shape that peer has
                        # always handled: without this, gaining Layer B would
                        # REMOVE the ability to send to a peer that has not been
                        # upgraded yet.
                        #
                        # Gated on the VERSION, not on Layer B presence: a
                        # context-free session ships a v2 bundle with NO
                        # ``layer_b`` key at all, and a presence check would skip
                        # the downgrade for exactly those transfers and fail them
                        # against a v1 peer. Dropping ``layer_b`` below stays
                        # unconditional because it is simply absent in that case.
                        if (
                            code == "transfer_version_unsupported"
                            and not downgraded
                            and bundle.get("bundle_version") == 2
                        ):
                            downgraded = True
                            bundle = {k: v for k, v in bundle.items() if k != "layer_b"}
                            bundle["bundle_version"] = 1
                            logger.info(
                                "Session transfer to %s: peer refused v2; "
                                "retrying transcript-only at v1",
                                instance_id,
                            )
                            continue
                        return False, {
                            "error": (
                                payload.get("error")
                                if isinstance(payload, dict) and payload.get("error")
                                else f"peer refused the transfer (HTTP {resp.status})"
                            ),
                            "code": code or "transfer_peer_refused",
                        }
            except Exception as e:
                logger.info(
                    "Session transfer to %s failed (%s)",
                    instance_id,
                    type(e).__name__,  # never the credential, never the bundle
                )
                return False, {
                    "error": f"could not reach the instance ({type(e).__name__})",
                    "code": "transfer_unreachable",
                }
        # Both attempts came back unauthorized.
        return False, {
            "error": "peer rejected the credential",
            "code": "transfer_unauthorized",
        }

    async def peer_capability(self, instance_id: str, path: str) -> tuple[bool, Any]:
        """GET one of a connected peer's read-only capability endpoints.

        This is deliberately a NARROW CARRIER, not a general proxy. The generic
        ``/api/instances/{id}/proxy/*`` route forwards a caller-supplied path and
        is therefore fenced to the ``api/chat`` / ``api/stream`` prefixes; the
        five paths a local session needs in order to render a peer-bound header
        (version, agent roster, model list, effort levels, workspaces) sit
        outside those prefixes. Widening the prefix list would have granted the
        whole ``api/agents`` surface — including its mutating ``PUT`` — so the
        capability read gets its own carrier whose target is chosen from a fixed
        set here rather than by the caller.

        Returns ``(ok, payload)``. On success *payload* is the peer's decoded
        JSON, which may be a dict (version, workspaces) or a list (agents,
        models, effort levels) — both shapes are real and returned as-is. On
        failure *payload* is ``{"error", "code"}`` so a caller can tell a stale
        credential from a peer too old to answer.
        """
        if path not in _PEER_CAPABILITY_PATHS:
            # A programming error, not a runtime condition: the path set is
            # closed and every caller passes a literal from it.
            raise ValueError(f"not a peer capability path: {path!r}")
        try:
            url, cookie_name = self._peer_target(instance_id, path)
        except _PeerUnavailable as e:
            return False, {
                "error": e.message,
                "code": (
                    "capability_peer_not_connected"
                    if e.kind == "not_connected"
                    else "capability_no_credential"
                ),
            }
        timeout = aiohttp.ClientTimeout(total=_CAPABILITY_PROXY_TIMEOUT)
        reminted = False
        for _attempt in range(2):
            try:
                headers = self._peer_cookie_header(instance_id, cookie_name)
            except _PeerUnavailable as e:
                return False, {"error": e.message, "code": "capability_no_credential"}
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(
                        url,
                        headers=headers,
                        # Same SSRF reasoning as the search carrier: the tunnel
                        # endpoint is the only legitimate target, so a peer
                        # answering 30x must not redirect the hub anywhere.
                        allow_redirects=False,
                    ) as resp:
                        if resp.status in (401, 403):
                            if not reminted and await self.refresh_token(instance_id):
                                reminted = True
                                continue  # retry once with the fresh credential
                            return False, {
                                "error": "peer rejected the credential",
                                "code": "capability_unauthorized",
                            }
                        if resp.status in (404, 405):
                            # The peer predates this endpoint. Reported as its own
                            # code because it is the actionable case (update the
                            # peer), not a transport fault to retry.
                            return False, {
                                "error": f"peer does not serve {path}",
                                "code": "capability_peer_too_old",
                            }
                        if not 200 <= resp.status < 300:
                            return False, {
                                "error": f"peer refused the read (HTTP {resp.status})",
                                "code": "capability_peer_refused",
                            }
                        chunks: list[bytes] = []
                        received = 0
                        oversized = False
                        async for chunk in resp.content.iter_chunked(65536):
                            received += len(chunk)
                            if received > _CAPABILITY_REPLY_MAX_BYTES:
                                oversized = True
                                break
                            chunks.append(chunk)
                        if oversized:
                            return False, {
                                "error": "peer capability reply exceeds the size cap",
                                "code": "capability_malformed_reply",
                            }
                        try:
                            payload = json.loads(b"".join(chunks))
                        except Exception:
                            return False, {
                                "error": "peer returned a malformed capability reply",
                                "code": "capability_malformed_reply",
                            }
                        if not isinstance(payload, (dict, list)):
                            return False, {
                                "error": "peer returned a malformed capability reply",
                                "code": "capability_malformed_reply",
                            }
                        return True, payload
            except Exception as e:
                logger.info(
                    "Peer capability read %s from %s failed (%s)",
                    path,
                    instance_id,
                    type(e).__name__,  # never the credential
                )
                return False, {
                    "error": f"could not reach the instance ({type(e).__name__})",
                    "code": "capability_unreachable",
                }
        # Both attempts came back unauthorized.
        return False, {
            "error": "peer rejected the credential",
            "code": "capability_unauthorized",
        }

    async def peer_version(self, instance_id: str) -> tuple[bool, str]:
        """The peer gateway's ``kiro_crew.__version__``, or ``(False, code)``.

        Used by the version-equality gate that fences remote execution. A peer
        without ``/api/version`` answers 404 and comes back as
        ``capability_peer_too_old`` — which the gate must treat exactly like a
        mismatch, since an unknown version cannot be proven equal.
        """
        ok, payload = await self.peer_capability(instance_id, "/api/version")
        if not ok:
            code = (
                payload.get("code", "capability_unreachable") if isinstance(payload, dict) else ""
            )
            return False, str(code)
        version = payload.get("version") if isinstance(payload, dict) else None
        if not isinstance(version, str) or not version:
            return False, "capability_malformed_reply"
        return True, version

    async def search_sessions_remote(
        self, instance_id: str, query: str, limit: int
    ) -> tuple[bool, dict]:
        """GET a connected peer's ``/api/sessions/search`` over its tunnel.

        Returns ``(ok, payload)``: on success *payload* is the peer's JSON reply
        (``{"sessions": [...]}``); on failure it carries ``error`` and a
        machine-readable ``code`` so the aggregator can tell a stale credential
        from an unreachable peer.

        Runs entirely over the already-open forward — **no SSH spawn** — and
        follows the shared credential rules in :meth:`_peer_cookie_header`: the
        token never leaves this object and travels as the port-scoped cookie. A
        401/403 gets exactly one transparent re-mint retry — a retained
        credential can go stale while the tunnel stays CONNECTED.
        """
        try:
            url, cookie_name = self._peer_target(instance_id, "/api/sessions/search")
        except _PeerUnavailable as e:
            return False, {
                "error": e.message,
                "code": (
                    "search_peer_not_connected"
                    if e.kind == "not_connected"
                    else "search_no_credential"
                ),
            }
        timeout = aiohttp.ClientTimeout(total=_SEARCH_PROXY_TIMEOUT)
        reminted = False
        for _attempt in range(2):
            try:
                headers = self._peer_cookie_header(instance_id, cookie_name)
            except _PeerUnavailable as e:
                return False, {"error": e.message, "code": "search_no_credential"}
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(
                        url,
                        params={"q": query, "limit": str(int(limit))},
                        headers=headers,
                        # The tunnel endpoint is the ONLY legitimate target. A
                        # compromised peer answering 30x would otherwise make
                        # aiohttp fetch an attacker-chosen URL FROM THE HUB
                        # (SSRF into its loopback control planes).
                        allow_redirects=False,
                    ) as resp:
                        if resp.status in (401, 403):
                            if not reminted and await self.refresh_token(instance_id):
                                reminted = True
                                continue  # retry once with the fresh credential
                            return False, {
                                "error": "peer rejected the credential",
                                "code": "search_unauthorized",
                            }
                        if not 200 <= resp.status < 300:
                            return False, {
                                "error": f"peer refused the search (HTTP {resp.status})",
                                "code": "search_peer_refused",
                            }
                        # Byte-cap BEFORE decoding: resp.json() buffers the whole
                        # body first, so a hostile/broken peer streaming an
                        # unbounded reply could exhaust hub memory before any
                        # per-field clamp runs. StreamReader.read(n) returns as
                        # soon as ANY buffered data exists, so a single call can
                        # yield a prefix of a multi-chunk reply — accumulate to
                        # EOF, refusing the moment the cap is crossed. An honest
                        # reply (<=200 clamped rows) sits far below the cap.
                        chunks: list[bytes] = []
                        received = 0
                        oversized = False
                        async for chunk in resp.content.iter_chunked(65536):
                            received += len(chunk)
                            if received > _SEARCH_REPLY_MAX_BYTES:
                                oversized = True
                                break
                            chunks.append(chunk)
                        if oversized:
                            return False, {
                                "error": "peer search reply exceeds the size cap",
                                "code": "search_malformed_reply",
                            }
                        try:
                            payload = json.loads(b"".join(chunks))
                        except Exception:
                            payload = None
                        if not isinstance(payload, dict):
                            return False, {
                                "error": "peer returned a malformed search reply",
                                "code": "search_malformed_reply",
                            }
                        return True, payload
            except Exception as e:
                logger.info(
                    "Federated session search to %s failed (%s)",
                    instance_id,
                    type(e).__name__,  # never the credential, never the query
                )
                return False, {
                    "error": f"could not reach the instance ({type(e).__name__})",
                    "code": "search_unreachable",
                }
        # Both attempts came back unauthorized.
        return False, {
            "error": "peer rejected the credential",
            "code": "search_unauthorized",
        }

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
        """(Re)start the proactive refresh loop for *instance_id*.

        Refuses while a reconfiguration holds the barrier: a refresh mints against
        the record it read, so one started here would carry the pre-edit
        coordinates and could store that token against the rebuilt tunnel.
        """
        if instance_id in self._reconfiguring:
            logger.info(
                "Skipping proactive refresh for %s: reconfiguration in progress",
                instance_id,
            )
            return
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
                # A failed re-mint is only terminal once the instance is not
                # connected (dropped from _tunnels); a transient mint failure
                # retries on the next cycle at the same interval, since `delay` is
                # derived from the ttl once, before the loop, and never re-derived.
                if (
                    not await self._refresh_token_once(instance_id)
                    and instance_id not in self._tunnels
                ):
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
        # A reconfiguration is about to move the coordinates this mint would read,
        # so starting one now can only produce a token for a machine the user is
        # leaving. The caller reports "no token" and the client retries.
        if instance_id in self._reconfiguring:
            return False
        inst = await asyncio.to_thread(self._registry.get, instance_id)
        if inst is None or instance_id not in self._tunnels:
            return False
        # Which tunnel generation this token is being minted FOR. Captured before
        # the await, compared after.
        epoch = self._tunnel_epoch.get(instance_id, 0)
        try:
            params = await asyncio.to_thread(self._resolve_transport, inst)
        except (SshValidationError, SsmValidationError, LoopbackValidationError) as e:
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
            if self._tunnel_epoch.get(instance_id, 0) != epoch:
                # The tunnel this token was minted for is gone and another has
                # taken its place (an edit + reconnect, or a self-heal reinstall).
                # Storing it would hand the embedded dashboard a credential the
                # CURRENT remote never issued, and the previous token — the valid
                # one — would already be overwritten.
                # Worded without the word for what was minted: the SAST logging
                # rule matches that keyword in a format string, and a nothing-was-
                # leaked log line is not worth a suppression comment.
                logger.info("Discarding a superseded mint for %s", instance_id)
                return False
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
