"""Supervises ``playwright-cli show``, the CLI's own dashboard, over loopback.

``show --port`` is a blocking HTTP server, so it is a long-lived supervised
child with its own lifecycle, not a call that returns a result. The dashboard it
serves carries the live viewport, the tab bar, and **full remote mouse and
keyboard input** into a browser that holds the operator's logged-in sessions.

Three properties are load-bearing; each was established by running the CLI, and
getting any of them wrong presents as a broken feature rather than as an error:

1. **Bind explicitly to ``127.0.0.1``.** The default listener is IPv6-only, so
   ``http://127.0.0.1:<port>/`` is unreachable and an iframe pointed there gets
   a connection failure while the server is running fine.
2. **Health is "any HTTP response", never "200".** ``/`` answers ``302``.
3. **Never ``--host 0.0.0.0``.** That would publish an interactive
   remote-input browser view, holding live logins, to the whole network. This
   module takes no host parameter at all, so there is no argument through which
   a caller could ask for a non-loopback bind.

The port is chosen by bind-probe rather than hardcoded: a fixed port collides
with whatever else the operator runs, and the collision would surface as an
unexplained dead panel. An operator who NEEDS predictability — the dashboard
viewed through an SSH tunnel that forwards a fixed set of ports — can pin the
public port via ``dashboard.browser_view_port``. The pin is never handed to
the child: this module claims the pinned port itself with a bound listener it
keeps holding (an atomic ownership proof, so the deterministic, operator-named
port is race-free) and relays byte-for-byte to the child's own ephemeral port.
The child port depends on the host's attribution capability. With a usable
attribution path, the supervisor gives the child an advisory OS-assigned port
and verifies the eventual listener PID. On a structurally blind host, the child
receives port 0, lets the kernel choose while it binds, and reports the actual
port on its private stdout pipe; no candidate port is released for a racer to
win. Both the relay and the child bind loopback only.
"""

from __future__ import annotations

import contextlib
import hmac
import http.client
import io
import logging
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Callable
from urllib.parse import urlsplit

from kiro_crew import platform_compat
from kiro_crew.browser_cli.install import (
    cli_command,
    cli_env,
    cli_path,
    installed_cli_version,
)
from kiro_crew.browser_cli.launch import ui_socket_env

logger = logging.getLogger(__name__)

# Loopback IPv4, as a constant rather than a parameter. See property 3 above.
LOOPBACK_HOST = "127.0.0.1"

# The server binds, starts Node, and initializes before it answers, so the
# readiness gate is a poll rather than a single probe.
_STARTUP_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 0.25
_HEALTH_TIMEOUT_S = 2.0
_TERMINATE_GRACE_S = 5.0

# How long relay_authorize will wait for the supervisor lock before refusing.
# ensure_running holds the lock across its whole startup poll (up to
# _STARTUP_TIMEOUT_S). Invalid candidates never reach the lock at all (the
# lock-free token pre-check refuses them first), so the bound protects the
# callers who remain: valid-token requests arriving during a start window are
# refused as retryable ``busy`` instead of parking a shared-pool thread for
# the full 30s poll. Outside a start, every hold is microseconds (the lock
# guards state snapshots only — the OS-level proofs run outside it), so one
# second never spuriously refuses; during one, refusing is correct — the
# token presented belongs to the instance being replaced.
_AUTHORIZE_ACQUIRE_TIMEOUT_S = 1.0

# The child proof is expected in its first few lines. Bound every dimension so
# a malformed or chatty stdout stream cannot keep a daemon reader alive forever
# or grow its pending line without limit. Hosts with working PID attribution do
# not depend on this fallback; an exhausted budget fails closed elsewhere.
_BINDING_PROOF_TIMEOUT_S = _STARTUP_TIMEOUT_S
_BINDING_PROOF_MAX_LINES = 256
_BINDING_PROOF_MAX_BYTES = 64 * 1024
_BINDING_PROOF_READ_SIZE = 4096
_BINDING_PROOF_POLL_S = 0.01

_VERIFIED_BINDING_BANNER_CONTRACT = f"Listening on http://{LOOPBACK_HOST}:<port>"
_VERIFIED_BINDING_BANNER_EXAMPLE = f"Listening on http://{LOOPBACK_HOST}:45613"
_BINDING_URL_RE = re.compile(
    r"https?://(?:\[[^\]\s]+\]|[^\s/:]+):\d+(?:/[^\s]*)?",
    re.IGNORECASE,
)


def _binding_reported_port_on_line(line: bytes, port: int) -> int | None:
    """Return the loopback port reported by trusted child stdout, if valid.

    Require a listener marker, then parse each URL by scheme, host, and port.
    Port 0 lets the child choose its own ephemeral port; every other request
    must report the assigned port exactly. Prefix, punctuation, and path changes
    are harmless, but an unrelated URL, host, or protocol cannot satisfy proof.
    """
    text = line.decode("utf-8", errors="replace")
    if re.search(r"\blisten(?:ing)?\b", text, re.IGNORECASE) is None:
        return None
    for token in _BINDING_URL_RE.findall(text):
        try:
            parsed = urlsplit(token)
            parsed_port = parsed.port
        except ValueError:
            continue
        if (
            parsed.scheme.casefold() == "http"
            and parsed.hostname == LOOPBACK_HOST
            and parsed_port is not None
            and 1 <= parsed_port <= 65535
            and (port == 0 or parsed_port == port)
            and parsed.username is None
            and parsed.password is None
        ):
            return parsed_port
    return None


# Concurrent connections the pinned-port relay will carry. The panel needs a
# handful (page, assets, one screencast/input WebSocket per session view);
# 32 leaves generous headroom while bounding the threads and sockets a local
# process can force the relay to hold — a local-DoS bound, not an auth control
# (the view server itself is equally reachable by any local process).
_RELAY_MAX_CONNS = 32


@dataclass(frozen=True)
class ShowInfo:
    """Where the running dashboard is reachable."""

    url: str
    port: int


@dataclass
class _BindingProof:
    """A child report carrying both the requested and banner-reported ports."""

    #: Port passed to ``show --port``; zero requests a child-selected port.
    port: int
    reported: threading.Event
    root_identity: platform_compat.ProcessDescendantIdentity | None = None
    cli_version: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _reported_port: int | None = field(default=None, init=False, repr=False)

    def record(self, port: int) -> None:
        """Publish the post-bind fact from the child-specific stdout pipe."""
        with self._lock:
            self._reported_port = port
            self.reported.set()

    def reported_port(self) -> int | None:
        """Return the reported port without erasing this handle-bound fact."""
        with self._lock:
            if not self.reported.is_set():
                return None
            return self._reported_port

    def invalidate(self) -> None:
        """Drop an unmatched report after a reader limit or stream failure."""
        with self._lock:
            self._reported_port = None
            self.reported.clear()


_lock = threading.Lock()
_proc: subprocess.Popen[bytes] | None = None
_info: ShowInfo | None = None
_relay: "_Relay | None" = None
#: The child's OWN listening port, which is what ownership is proved against.
#: Distinct from ``_info.port``: on the pinned path that is the operator's port,
#: served by our in-process relay, so it proves nothing about the child.
_child_port: int | None = None
#: Capability token for the dashboard's same-origin relay (``/browser-view/``),
#: minted fresh for each view-server instance. The relay path carries it
#: (``/browser-view/<token>/…``) and the relay handler constant-time-compares
#: it — that token IS the relay's authentication, because the panel frames the
#: relay in an opaque-origin sandbox that sends no cookies. Disclosed only
#: through the cookie-authed, owner-gated status payload, and rotated on every
#: start so a leaked value dies with the instance that leaked it.
_relay_token: str | None = None
# Why the last start attempt failed, surfaced through ``status()``. With an
# ephemeral port a failed bind was a near-impossible edge; with a pinned port
# "already in use" becomes the most likely operator misconfiguration, and a
# silent ``stopped`` presents as a broken panel rather than as an error.
_last_reason: str | None = None

_OWNERSHIP_REASON = "Couldn't confirm the browser view still owns its port"
_RELAY_REASON = "Couldn't start the browser view's connection relay"


def _listener_tool_name() -> str:
    """Name the active listener-attribution path."""
    return (
        "GetExtendedTcpTable"
        if platform_compat.IS_WINDOWS
        else platform_compat.listening_pid_tool()
    )


def _blind_listener_ownership_reason() -> str:
    """Explain why a structurally blind host cannot republish a saved URL."""
    return (
        "listener ownership cannot be re-proved on this host: "
        f"{_listener_tool_name()} is absent or cannot attribute processes"
    )


def _listener_attribution_failure_reason() -> str:
    """Name a present-but-failing attribution path and its operator remedy."""
    tool = _listener_tool_name()
    if platform_compat.IS_WINDOWS:
        return (
            f"{tool} did not attribute the gateway's own control listener; "
            "check its permissions/namespace"
        )
    path = platform_compat.trusted_system_bin(tool)
    if path is None:
        return _blind_listener_ownership_reason()
    return (
        f"{tool} at {path} did not attribute the gateway's own control listener; "
        "check its permissions/namespace"
    )


def _free_port() -> int:
    """An OS-assigned ephemeral loopback port.

    Binding port 0 lets the kernel pick one that is free right now. This is
    advisory: the socket is closed before the child binds it, so there is a
    TOCTOU window in which a local process can take the number instead.

    The window cannot be closed here. Closing it would mean handing the child a
    socket we already hold, and ``playwright-cli show`` takes a port NUMBER, not
    an inherited descriptor — so there is no bind-before-release to perform. What
    contains it is the readiness gate: it requires either PID attribution to the
    process tree we spawned or a recognized post-bind loopback URL reported on
    that child's private stdout pipe. Losing the race is therefore reported as a
    failed start instead of adopting the winner.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK_HOST, 0))
        return int(sock.getsockname()[1])


def _claim_listener(port: int) -> socket.socket | None:
    """Atomically claim *port* with a bound, listening socket, or ``None``.

    Split out of :class:`_Relay` so :func:`ensure_running` can claim the pin
    BEFORE choosing the child's ephemeral port: while the pin is bound,
    ``_free_port()`` structurally cannot hand the same number back, so the
    "child port equals the pin" collision cannot arise by construction.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if platform_compat.IS_POSIX:
        # Allow rebinding through TIME_WAIT after a restart. POSIX-only:
        # on Windows SO_REUSEADDR permits stealing an ACTIVE listener,
        # which is the exact hole the held-listener design exists to close.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    elif hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        # Windows: merely NOT setting SO_REUSEADDR is not enough — another
        # local process can still bind the same port by setting SO_REUSEADDR
        # on ITS socket. SO_EXCLUSIVEADDRUSE is the opt-in that makes our
        # bind exclusive, so the ownership proof holds on Windows too.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        listener.bind((LOOPBACK_HOST, port))
        listener.listen(16)
    except OSError:
        with contextlib.suppress(Exception):
            listener.close()
        return None
    return listener


def _start_daemon_thread(thread: threading.Thread) -> bool:
    """Start *thread*, returning false when the runtime cannot create it."""
    try:
        thread.start()
    except RuntimeError as exc:
        logger.warning("could not start daemon thread %s: %s", thread.name, exc)
        return False
    return True


class _Relay:
    """Holds a pinned loopback port and pumps bytes to the child's real port.

    The pin cannot be handed to the child directly without a race: any probe
    closes its socket before the child binds, and in that window a local
    squatter can take the port — after which :func:`_healthy` would accept the
    squatter's response and the panel would frame arbitrary content WITH remote
    input forwarding. Holding the bound listener ourselves is the only atomic
    ownership proof: ``bind()`` either succeeds (the port is ours until we
    close it) or raises (occupied). This makes the DETERMINISTIC, operator-named
    port race-free; the child then binds an OS-assigned ephemeral port exactly
    as on the unpinned path, whose probe-to-bind window is contained a different
    way -- :func:`_port_owner` proves the responder belongs to the tree we
    spawned before the readiness gate adopts it, so a squatter that wins the
    window is refused rather than framed. This relay forwards
    each accepted connection byte-for-byte, which carries HTTP and WebSocket
    traffic alike. Both sockets stay loopback-only.
    """

    def __init__(self, listener: socket.socket, target_port: int) -> None:
        self._listener = listener
        self._target_port = target_port
        self._closed = threading.Event()
        # Live sockets, guarded by _conn_lock: bounds what a local process can
        # force us to hold, and lets close() tear down in-flight connections
        # instead of leaving them to die with their daemon threads.
        self._conn_lock = threading.Lock()
        self._conns: set[socket.socket] = set()
        self._thread = threading.Thread(
            target=self._accept_loop, name="browser-view-relay", daemon=True
        )

    @classmethod
    def open(cls, port: int, target_port: int) -> "_Relay | None":
        """Claim *port* and relay it to *target_port*, or ``None`` if unavailable."""
        listener = _claim_listener(port)
        if listener is None:
            return None
        return cls.from_listener(listener, target_port)

    @classmethod
    def from_listener(cls, listener: socket.socket, target_port: int) -> "_Relay | None":
        """Start relaying on an already-claimed listener (see _claim_listener)."""
        relay = cls(listener, target_port)
        if _start_daemon_thread(relay._thread):
            return relay
        relay._closed.set()
        with contextlib.suppress(Exception):
            listener.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(Exception):
            listener.close()
        return None

    def close(self) -> None:
        """Stop accepting and wait for the accept thread to exit.

        ``shutdown`` before ``close`` is what reliably unblocks a thread
        parked in ``accept()`` — closing the descriptor alone is not
        guaranteed to wake it on every platform. The bounded ``join`` makes
        teardown deterministic instead of leaving a daemon thread to die on
        its own schedule (a test-visible side effect).
        """
        self._closed.set()
        with contextlib.suppress(Exception):
            self._listener.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(Exception):
            self._listener.close()
        self._thread.join(timeout=_TERMINATE_GRACE_S)
        # Tear down in-flight connections. shutdown() before close(), same as
        # the listener above: the pump threads hold these sockets, and close()
        # alone does not reliably interrupt them or deliver the FIN while
        # another thread is blocked in recv() — shutdown() does both.
        with self._conn_lock:
            conns = list(self._conns)
            self._conns.clear()
        for sock in conns:
            with contextlib.suppress(Exception):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(Exception):
                sock.close()

    def _track(self, sock: socket.socket) -> bool:
        """Register a live socket; ``False`` when the cap refuses it."""
        with self._conn_lock:
            if len(self._conns) >= _RELAY_MAX_CONNS:
                return False
            self._conns.add(sock)
            return True

    def _untrack(self, sock: socket.socket) -> None:
        with self._conn_lock:
            self._conns.discard(sock)

    def _accept_loop(self) -> None:
        while not self._closed.is_set():
            try:
                client, _addr = self._listener.accept()
            except OSError:
                return  # listener closed
            if not self._track(client):
                # At capacity: refuse instead of queueing unbounded threads. A
                # local flooder is bounded; the panel's handful of connections
                # never gets near the cap.
                with contextlib.suppress(Exception):
                    client.close()
                continue
            worker = threading.Thread(
                target=self._serve,
                args=(client,),
                name="browser-view-relay-conn",
                daemon=True,
            )
            if not _start_daemon_thread(worker):
                self._untrack(client)
                with contextlib.suppress(Exception):
                    client.shutdown(socket.SHUT_RDWR)
                with contextlib.suppress(Exception):
                    client.close()

    def _serve(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection(
                (LOOPBACK_HOST, self._target_port), timeout=_HEALTH_TIMEOUT_S
            )
        except OSError:
            self._untrack(client)
            with contextlib.suppress(Exception):
                client.close()
            return
        upstream.settimeout(None)
        # Release the cap slot only when BOTH directions have terminated. A
        # half-closed connection (one pump exited, the other still parked in
        # recv on a peer that stays silent) must keep holding its slot:
        # freeing it on the first exit would let a local flooder accumulate
        # live pump threads beyond the accounting bound. Teardown still
        # reaches a lingering pump: a client-blocked pump is unblocked by
        # close() shutting down the tracked client socket, and an
        # upstream-blocked pump by the supervised child being reaped, which
        # accompanies every relay teardown path in ensure_running/stop.
        remaining = [2]
        remaining_lock = threading.Lock()

        def on_done() -> None:
            with remaining_lock:
                remaining[0] -= 1
                finished = remaining[0] == 0
            if finished:
                self._untrack(client)

        a = threading.Thread(target=_pump, args=(client, upstream, on_done), daemon=True)
        b = threading.Thread(target=_pump, args=(upstream, client, on_done), daemon=True)
        if not _start_daemon_thread(a):
            on_done()
            on_done()
            with contextlib.suppress(Exception):
                client.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(Exception):
                client.close()
            with contextlib.suppress(Exception):
                upstream.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(Exception):
                upstream.close()
            return
        if not _start_daemon_thread(b):
            on_done()
            with contextlib.suppress(Exception):
                client.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(Exception):
                client.close()
            with contextlib.suppress(Exception):
                upstream.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(Exception):
                upstream.close()


def _pump(
    src: socket.socket, dst: socket.socket, on_done: "Callable[[], None] | None" = None
) -> None:
    """Copy bytes from *src* to *dst* until either side closes."""
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        with contextlib.suppress(Exception):
            dst.shutdown(socket.SHUT_WR)
        with contextlib.suppress(Exception):
            src.close()
        if on_done is not None:
            on_done()


def _healthy(port: int) -> bool:
    """Whether the dashboard answers HTTP on *port*.

    ANY status line counts, including the ``302`` that ``/`` actually returns.
    Only a transport-level failure (nothing listening, hang, reset) is unhealthy
    — the question is whether an HTTP server is there, not what it thinks of the
    request.
    """
    conn = http.client.HTTPConnection(LOOPBACK_HOST, port, timeout=_HEALTH_TIMEOUT_S)
    try:
        conn.request("GET", "/")
        conn.getresponse()
        return True
    except (OSError, http.client.HTTPException):
        return False
    finally:
        with contextlib.suppress(Exception):
            conn.close()


#: Proven: a process in the tree we spawned holds the port.
_OWNER_CHILD = "child"
#: Treated as foreign. Either a functional lookup could not attribute the
#: listener to us, or it proved a third party owns the port.
_OWNER_FOREIGN = "foreign"
#: The global port-to-PID lookup is absent or blind. Adoption still requires a
#: per-process ownership result or, when none is available, a startup report.
_OWNER_UNPROVEN = "unproven"


_PORT_OWNER_IDENTITY_PROOF_ATTR = "_kirocrew_browser_view_port_owner_identity"


def _record_port_owner_identity_proof(
    proc: subprocess.Popen[bytes],
    port: int,
    identity: platform_compat.ProcessDescendantIdentity,
) -> None:
    setattr(proc, _PORT_OWNER_IDENTITY_PROOF_ATTR, (port, identity))


def _take_port_owner_identity_proof(
    proc: subprocess.Popen[bytes], port: int
) -> platform_compat.ProcessDescendantIdentity | None:
    proof = getattr(proc, _PORT_OWNER_IDENTITY_PROOF_ATTR, None)
    with contextlib.suppress(Exception):
        delattr(proc, _PORT_OWNER_IDENTITY_PROOF_ATTR)
    if (
        not isinstance(proof, tuple)
        or len(proof) != 2
        or proof[0] != port
        or not isinstance(proof[1], platform_compat.ProcessDescendantIdentity)
    ):
        return None
    return proof[1]


#: Single writer for the per-proc identity-proof slot. The slot protocol is
#: record-then-take on the ``proc`` object, and :func:`_port_owner` OPENS by
#: clearing any stale slot — so two provers interleaving clobber each other:
#: B's clearing take lands inside A's record→take window, A reads ``None`` and
#: returns a definitive FALSE for a live child, and the caller tears the view
#: down (worst case, the ``_recorded_state`` prover misreads and the next
#: ``ensure_running`` reaps the live headed browser). Callers under ``_lock``
#: excluded each other implicitly; the relay's outside-lock provers do not.
#: This gate restores the one-prover-at-a-time invariant for EVERY prover.
#: Lock ordering: ``_lock`` → ``_proof_gate`` only (locked callers enter the
#: gate; the gated prover never acquires ``_lock`` — teardown runs after the
#: gate is released), so no deadlock path exists.
_proof_gate = threading.Lock()

#: Last completed non-report proof verdict: ``(id(proc), pid, child_port,
#: monotonic_started, verdict)``. One page load fans out to many relay
#: requests, each paying process/listener probes that spawn ``ps``/``lsof``;
#: within one TTL those requests are asking about the same instant of the
#: same child, so the first prover through the gate answers for all of them
#: (single-flight — waiters re-check the cache under the gate before
#: proving). Keyed on the exact Popen object AND pid AND port, so a
#: stop/start cycle can never inherit a predecessor's verdict;
#: report-eligible startup proofs (``allow_report=True``) neither read nor
#: write it, keeping stdout reports startup-only evidence. The stamp is the
#: proof's START (captured before any evidence gathering), so it bounds the
#: age of the OLDEST evidence in the verdict. Verdict staleness is bounded by
#: the TTL, which is within the trust envelope of a proof's own
#: multi-subprocess runtime; where staleness would break a bracketing
#: invariant, callers pass ``proof_not_before`` to demand a proof started
#: strictly after their fence.
_proof_cache: tuple[int, int, int, float, bool | None] | None = None

#: Longer than one page load's asset burst, shorter than anything a human can
#: act within. The post-connect re-proof does not rely on this bound — its
#: ``proof_not_before`` fence rejects any proof not started strictly after
#: the connect.
_PROOF_CACHE_TTL_S = 0.5


def _cached_listener_verdict(
    proc: subprocess.Popen[bytes], port: int, not_before: float | None
) -> tuple[bool, bool | None]:
    """Return ``(hit, verdict)`` for a fresh cached proof of this exact child.

    A single reference read of the module global (atomic under the GIL), so
    the lock-free pre-gate check costs nothing when it misses.
    """
    entry = _proof_cache
    if entry is None:
        return False, None
    proc_id, pid, child_port, when, verdict = entry
    if proc_id != id(proc) or pid != proc.pid or child_port != port:
        return False, None
    if time.monotonic() - when > _PROOF_CACHE_TTL_S:
        return False, None
    if not_before is not None and when <= not_before:
        # Strict: a proof STARTED on the fence's own clock reading may have
        # gathered evidence physically before the fence (Windows monotonic
        # ticks at ~15.6ms, so distinct instants share a reading). Equality
        # refuses, and the caller runs a fresh proof — fail-safe, never
        # fail-open.
        return False, None
    return True, verdict


_listener_lookup_self_test_cache: tuple[int, str | None, bool | None] | None = None


def _invalidate_listener_lookup_self_test_cache() -> None:
    """Forget host capability evidence before a replacement child is spawned."""
    global _listener_lookup_self_test_cache
    _listener_lookup_self_test_cache = None


def _invalidate_listener_lookup_cache_for_tool_path(tool_path: str | None) -> None:
    """Drop evidence produced by a different resolved listener-tool path."""
    global _listener_lookup_self_test_cache
    cached = _listener_lookup_self_test_cache
    if cached is not None and (cached[0] != os.getpid() or cached[1] != tool_path):
        _listener_lookup_self_test_cache = None


def _listener_lookup_functional() -> bool | None:
    """Whether the port-to-PID lookup can attribute a known local listener.

    A resolvable ``lsof`` or ``netstat`` can still be unusable from the current
    process namespace. Bind a real loopback control listener and ask the same
    platform helper used for the target port. The result is process-scoped and
    cached by the trusted tool path; a path change or replacement spawn clears
    it. ``None`` means the control listener could not be created or its lookup
    did not complete. Neither is evidence that the tool is blind.
    """
    global _listener_lookup_self_test_cache
    tool_path = (
        "GetExtendedTcpTable"
        if platform_compat.IS_WINDOWS
        else platform_compat.trusted_system_bin(platform_compat.listening_pid_tool())
    )
    _invalidate_listener_lookup_cache_for_tool_path(tool_path)
    cached = _listener_lookup_self_test_cache
    if cached is not None and cached[1] == tool_path:
        return cached[2]

    listener = _claim_listener(0)
    if listener is None:
        result: bool | None = None
    else:
        try:
            control_port = int(listener.getsockname()[1])
            if platform_compat.IS_WINDOWS:
                observed = platform_compat.process_owns_loopback_listener(os.getpid(), control_port)
                result = observed
            else:
                listeners, completed = platform_compat.probe_port_listeners(control_port)
                if not completed:
                    result = None
                else:
                    owners = platform_compat.loopback_owner_pids(listeners)
                    result = os.getpid() in owners
        finally:
            with contextlib.suppress(Exception):
                listener.close()
    _listener_lookup_self_test_cache = (os.getpid(), tool_path, result)
    return result


def _structurally_blind_listener_attribution() -> bool:
    """Whether this host has no listener-attribution path beyond child stdout."""
    if platform_compat.IS_WINDOWS:
        return _listener_lookup_functional() is False
    if platform_compat.IS_LINUX:
        # Linux's per-process procfs probe is independent of lsof.
        return False
    if not platform_compat.listening_pid_tool_available():
        _invalidate_listener_lookup_cache_for_tool_path(None)
        return True
    return _listener_lookup_functional() is False


def _windows_port_owner(port: int, proc: subprocess.Popen[bytes]) -> str:
    """Classify *port* through owner-PID tables without invoking netstat."""
    root_observation = platform_compat.process_owns_loopback_listener(proc.pid, port)
    if root_observation is True:
        proof = _process_binding_proof(proc)
        root_identity = proof.root_identity if proof is not None else None
        root_verdict = _root_process_identity_matches(proc, "after Windows root listener lookup")
        if root_identity is None or root_verdict is False:
            return _OWNER_FOREIGN
        if root_verdict is None:
            return _OWNER_UNPROVEN
        _record_port_owner_identity_proof(proc, port, root_identity)
        return _OWNER_CHILD

    control_observation = _listener_lookup_functional()
    if root_observation is None or control_observation is not True:
        return _OWNER_UNPROVEN
    root_verdict = _root_process_identity_matches(proc, "after Windows control listener lookup")
    if root_verdict is not True:
        return _OWNER_UNPROVEN if root_verdict is None else _OWNER_FOREIGN

    candidate_pids = set(platform_compat.process_descendants(proc.pid))
    identities = platform_compat.process_descendant_identities(
        proc.pid,
        candidate_pids=candidate_pids or None,
    )
    if identities is None:
        return _OWNER_UNPROVEN
    identity_pids = {identity.pid for identity in identities}
    inconclusive = bool(candidate_pids - identity_pids)
    for identity in identities:
        before = _process_descendant_start_identity(identity)
        if not _descendant_identity_matches(identity, before, "before Windows"):
            inconclusive = True
            continue
        observation = platform_compat.process_owns_loopback_listener(identity.pid, port)
        after = _process_descendant_start_identity(identity)
        if not _descendant_identity_matches(identity, after, "after Windows"):
            inconclusive = True
            continue
        if observation is True:
            root_verdict = _root_process_identity_matches(
                proc, "after Windows descendant listener lookup"
            )
            if root_verdict is not True:
                return _OWNER_UNPROVEN if root_verdict is None else _OWNER_FOREIGN
            _record_port_owner_identity_proof(proc, port, identity)
            return _OWNER_CHILD
        if observation is None:
            inconclusive = True

    root_verdict = _root_process_identity_matches(proc, "after Windows listener verification")
    if root_verdict is not True:
        return _OWNER_UNPROVEN if root_verdict is None else _OWNER_FOREIGN
    return _OWNER_UNPROVEN if inconclusive else _OWNER_FOREIGN


def _port_owner(port: int, proc: subprocess.Popen[bytes] | None) -> str:
    """Who holds *port*: our child's tree, foreign, or undecidable on this host.

    :func:`_healthy` answers "is an HTTP server there", which is reachability,
    not identity. That is the whole gap: ``_free_port`` releases its probe socket
    before the child binds, so a local process can take the number in between,
    and a bare health probe then reports the squatter as our server -- after which
    the panel frames arbitrary content with input forwarding attached.

    A lookup that can attribute a known local listener but cannot attribute the
    target is FOREIGN, not undecidable. The caller only asks after a successful
    ``127.0.0.1`` probe, so something is listening. If a functional lookup cannot
    show that it belongs to our tree, "not ours" is the safe reading. This covers
    a squatter owned by another user and invisible on the target port.

    A missing POSIX lookup tool or a completed control lookup that cannot attribute
    a listener this process just bound yields UNPROVEN. Windows uses the in-process
    owner-PID tables and never invokes netstat. UNPROVEN is not an adoption
    decision: the caller must obtain a separate positive proof from the child. The
    self-test runs after a target lookup fails or completes negative. Only a
    completed target negative plus a control lookup that proves the path functional
    can classify the responder as foreign; an incomplete target or control probe
    remains unproven and preserves the live child.

    A listener PID becomes CHILD only when it is the root or an identity-bound
    descendant, keeps the same start ID around a confirming target lookup, and
    leaves a port-bound proof for :func:`_verify_child_listener` to recheck.
    Bare-PID ancestry is never an ownership grant.
    """
    if proc is None:
        return _OWNER_FOREIGN
    _take_port_owner_identity_proof(proc, port)
    root_verdict = _root_process_identity_matches(proc, "before listener lookup")
    if root_verdict is not True:
        return _OWNER_UNPROVEN if root_verdict is None else _OWNER_FOREIGN
    if platform_compat.IS_WINDOWS:
        return _windows_port_owner(port, proc)
    if not platform_compat.listening_pid_tool_available():
        return _OWNER_UNPROVEN
    listeners, completed = platform_compat.probe_port_listeners(port)
    if not completed:
        _listener_lookup_functional()
        return _OWNER_UNPROVEN
    if not listeners:
        if _listener_lookup_functional() is not True:
            return _OWNER_UNPROVEN
        return _OWNER_FOREIGN
    owners = platform_compat.loopback_owner_pids(listeners)
    if not owners:
        return _OWNER_FOREIGN
    owner_start_ids = {pid: _process_start_identity(pid) for pid in owners}
    if any(start_id is None for start_id in owner_start_ids.values()):
        return _OWNER_UNPROVEN
    if platform_compat.IS_WINDOWS:
        identities = platform_compat.process_descendant_identities(
            proc.pid,
            candidate_pids=set(owners),
        )
    else:
        identities = platform_compat.process_descendant_identities(proc.pid)
    if identities is None:
        return _OWNER_UNPROVEN
    root_verdict = _root_process_identity_matches(proc, "after ancestry lookup")
    if root_verdict is not True:
        return _OWNER_UNPROVEN if root_verdict is None else _OWNER_FOREIGN
    if any(_process_start_identity(pid) != start_id for pid, start_id in owner_start_ids.items()):
        return _OWNER_UNPROVEN

    candidates: list[platform_compat.ProcessDescendantIdentity] = []
    if proc.pid in owners:
        proof = _process_binding_proof(proc)
        root_identity = proof.root_identity if proof is not None else None
        if root_identity is None:
            return _OWNER_FOREIGN
        candidates.append(root_identity)
    candidates.extend(identity for identity in identities if identity.pid in owners)
    if not candidates:
        return _OWNER_FOREIGN

    stable_before = [
        identity
        for identity in candidates
        if _process_descendant_start_identity(identity) == identity.start_time
    ]
    if not stable_before:
        return _OWNER_UNPROVEN
    confirming_listeners, confirming_completed = platform_compat.probe_port_listeners(port)
    if not confirming_completed:
        return _OWNER_UNPROVEN
    confirming_owners = platform_compat.loopback_owner_pids(confirming_listeners)
    root_verdict = _root_process_identity_matches(proc, "after confirming listener lookup")
    if root_verdict is not True:
        return _OWNER_UNPROVEN if root_verdict is None else _OWNER_FOREIGN
    for identity in stable_before:
        if (
            identity.pid in confirming_owners
            and _process_descendant_start_identity(identity) == identity.start_time
        ):
            _record_port_owner_identity_proof(proc, port, identity)
            return _OWNER_CHILD
    return _OWNER_UNPROVEN


def _binding_chunk_reader(
    stream: BinaryIO,
) -> tuple[Callable[[int], bytes], int | None] | None:
    """Return a chunk reader and the descriptor it owns, if any.

    ``Popen(stdout=PIPE)`` supplies a real descriptor. Duplicate it before the
    reader starts so the stream owner may close and reuse its descriptor without
    redirecting this thread. ``BytesIO`` remains the finite descriptor-free test
    seam. An unconstrained mock is rejected rather than converted to an fd.
    """
    if isinstance(stream, io.BytesIO):
        return stream.read, None
    try:
        original_fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    if type(original_fd) is not int or original_fd < 0:
        return None
    try:
        owned_fd = os.dup(original_fd)
    except OSError:
        return None
    try:
        os.set_blocking(owned_fd, False)
    except (OSError, ValueError):
        # The duplicate remains safe to own; blocking reads run only in this
        # daemon thread and still drain the child's pipe through EOF.
        pass
    return lambda size: os.read(owned_fd, size), owned_fd


def _discard_child_output(read_chunk: Callable[[int], bytes]) -> None:
    """Drain child stdout until EOF after it stops carrying proof data."""
    while True:
        try:
            chunk = read_chunk(_BINDING_PROOF_READ_SIZE)
        except BlockingIOError:
            time.sleep(_BINDING_PROOF_POLL_S)
            continue
        except (OSError, ValueError):
            return
        if not isinstance(chunk, bytes) or not chunk:
            return


def _drain_child_output(
    stream: BinaryIO,
    port: int,
    proof: _BindingProof,
    cli_version: str | None = None,
) -> None:
    """Scan a bounded prefix for proof, then drain stdout until EOF.

    The real pipe is nonblocking so the wall-clock deadline bounds a silent
    child. Lines and bytes bound chatty or unterminated output. Exhausting a
    proof limit before a match invalidates the report. A match remains valid
    when the same daemon switches to discard mode, so later child output cannot
    fill the pipe. Every descriptor-backed path reads and drains through one
    owned duplicate; if nonblocking setup fails, that duplicate drains in
    blocking mode on this daemon thread.
    """
    reader = _binding_chunk_reader(stream)
    if reader is None:
        proof.invalidate()
        return
    read_chunk, owned_fd = reader
    deadline = time.monotonic() + _BINDING_PROOF_TIMEOUT_S
    pending = bytearray()
    lines = 0
    total = 0
    matched = False
    drift_logged = False

    def _log_drift() -> None:
        nonlocal drift_logged
        if total > 0 and not matched and not drift_logged:
            logger.warning(
                "playwright-cli %s stdout did not contain a recognized listener "
                "address matching the verified banner form %r for port %d; refusing "
                "the report because the upstream banner may have changed",
                cli_version or "unknown-version",
                _VERIFIED_BINDING_BANNER_EXAMPLE,
                port,
            )
            drift_logged = True

    def _discard_after_proof_window() -> None:
        if not matched:
            proof.invalidate()
        _log_drift()
        _discard_child_output(read_chunk)

    try:
        while True:
            now = time.monotonic()
            if (
                now >= deadline
                or lines >= _BINDING_PROOF_MAX_LINES
                or total >= _BINDING_PROOF_MAX_BYTES
            ):
                _discard_after_proof_window()
                return
            remaining = _BINDING_PROOF_MAX_BYTES - total
            try:
                chunk = read_chunk(min(_BINDING_PROOF_READ_SIZE, remaining + 1))
            except BlockingIOError:
                delay = min(_BINDING_PROOF_POLL_S, max(0.0, deadline - time.monotonic()))
                if delay:
                    time.sleep(delay)
                continue
            if not isinstance(chunk, bytes):
                if not matched:
                    proof.invalidate()
                return
            if not chunk:
                if pending:
                    lines += 1
                    if lines > _BINDING_PROOF_MAX_LINES:
                        _discard_after_proof_window()
                        return
                    reported_port = _binding_reported_port_on_line(bytes(pending), port)
                    if reported_port is not None:
                        matched = True
                        proof.record(reported_port)
                return
            total += len(chunk)
            if total > _BINDING_PROOF_MAX_BYTES:
                _discard_after_proof_window()
                return
            pending.extend(chunk)
            while True:
                newline = pending.find(b"\n")
                if newline < 0:
                    break
                line = bytes(pending[:newline])
                del pending[: newline + 1]
                lines += 1
                if lines > _BINDING_PROOF_MAX_LINES:
                    _discard_after_proof_window()
                    return
                reported_port = _binding_reported_port_on_line(line, port)
                if reported_port is not None:
                    matched = True
                    proof.record(reported_port)
    except (OSError, ValueError):
        # Reaping closes the pipe. Preserve a report already tied to this child;
        # handle liveness and identity checks decide whether it can still apply.
        if not matched:
            proof.invalidate()
    finally:
        if owned_fd is not None:
            with contextlib.suppress(OSError):
                os.close(owned_fd)
        _log_drift()


def _child_reported_port(proc: subprocess.Popen[bytes], port: int) -> int | None:
    """Return this child's bound port when it matches the requested or resolved port."""
    proof = getattr(proc, "_kirocrew_browser_view_binding", None)
    if not isinstance(proof, _BindingProof):
        return None
    reported_port = proof.reported_port()
    if reported_port is None:
        return None
    if proof.port == port or (proof.port == 0 and reported_port == port):
        return reported_port
    return None


def _child_reported_binding(proc: subprocess.Popen[bytes], port: int) -> bool:
    """Whether this exact child reported a recognized listener address."""
    return _child_reported_port(proc, port) is not None


def _process_start_identity(pid: int) -> str | None:
    """High-resolution process identity, with the legacy ps value as fallback."""
    return platform_compat.get_process_start_id(pid) or platform_compat.process_start_time(pid)


def _capture_root_process_identity(
    pid: int,
) -> platform_compat.ProcessDescendantIdentity | None:
    """Capture the root PID and identity source used for every later recheck."""
    if platform_compat.IS_WINDOWS:
        sources = [platform_compat.ProcessIdentitySource.WINDOWS]
    else:
        sources = [platform_compat.ProcessIdentitySource.ATOMIC]
        if platform_compat.IS_POSIX:
            sources.append(platform_compat.ProcessIdentitySource.LSTART)
    for source in sources:
        start_id = platform_compat.process_start_id_for_source(pid, source)
        if isinstance(start_id, str) and start_id:
            return platform_compat.ProcessDescendantIdentity(pid, 0, start_id, source)
    return None


def _process_descendant_start_identity(
    identity: platform_compat.ProcessDescendantIdentity,
) -> str | None:
    """Re-read a process through the source that captured its identity."""
    return platform_compat.process_start_id_for_source(identity.pid, identity.source)


def _process_binding_proof(proc: subprocess.Popen[bytes]) -> _BindingProof | None:
    proof = getattr(proc, "_kirocrew_browser_view_binding", None)
    return proof if isinstance(proof, _BindingProof) else None


def _root_process_identity_matches(proc: subprocess.Popen[bytes], phase: str) -> bool | None:
    """Return match, mismatch, or unknown for the spawned root identity."""
    proof = _process_binding_proof(proc)
    captured = proof.root_identity if proof is not None else None
    alive = _alive(proc)
    if captured is None or captured.pid != proc.pid or not alive:
        current = None
        verdict: bool | None = False
    else:
        current = _process_descendant_start_identity(captured)
        verdict = None if current is None else current == captured.start_time
    if verdict is True:
        return True
    logger.debug(
        "browser view root identity %s %s: pid=%d, captured=%r, source=%r, " "current=%r, alive=%s",
        "is inconclusive" if verdict is None else "changed",
        phase,
        proc.pid,
        captured.start_time if captured is not None else None,
        captured.source.value if captured is not None else None,
        current,
        alive,
    )
    return verdict


def _listener_banner_reason(proc: subprocess.Popen[bytes]) -> str:
    proof = _process_binding_proof(proc)
    version = proof.cli_version if proof is not None else None
    return (
        f"The browser CLI {version or 'unknown-version'} did not print a recognizable "
        "listener banner "
        f"(`{_VERIFIED_BINDING_BANNER_CONTRACT}`); a CLI upgrade may have changed it"
    )


def _descendant_identity_matches(
    identity: platform_compat.ProcessDescendantIdentity,
    current_start_time: str | None,
    phase: str,
) -> bool:
    """Refuse a descendant PID that changed identity around its listener probe."""
    if current_start_time == identity.start_time:
        return True
    logger.debug(
        "browser view descendant identity changed %s listener probe: "
        "enumerated=(pid=%d, ppid=%d, start=%r), current=(pid=%d, start=%r)",
        phase,
        identity.pid,
        identity.ppid,
        identity.start_time,
        identity.pid,
        current_start_time,
    )
    return False


def _verify_child_listener(
    proc: subprocess.Popen[bytes],
    port: int,
    *,
    allow_report: bool,
    proof_not_before: float | None = None,
) -> tuple[bool | None, bool]:
    """Serialize and cache the listener proof; see :func:`_verify_child_listener_gated`.

    All provers pass through here, so the per-proc identity-proof slot has
    exactly one writer at a time (``_proof_gate``) — the invariant holds for
    provers running outside ``_lock``, not only under it. Non-report verdicts
    are cached for :data:`_PROOF_CACHE_TTL_S` and shared single-flight:
    waiters re-check the cache under the gate, so a burst of concurrent relay
    requests costs one proof run, not one per request. ``proof_not_before``
    demands a proof STARTED strictly after the caller's fence — the
    post-connect re-proof uses it so no evidence gathered before the connect
    can vouch for the connection. Strictness is what makes the demand hold on
    coarse clocks (Windows monotonic ticks at ~15.6ms): a shared clock
    reading refuses rather than serves. A refused cache never loops — the
    caller falls through to a fresh proof under the gate, which satisfies its
    own fence by program order (the fence is captured before this call
    begins). ``allow_report=True`` (startup adoption) neither reads nor
    writes the cache: a stdout-report-based pass is startup-only evidence,
    and adoption wants a fresh proof anyway.
    """
    global _proof_cache
    if not allow_report:
        hit, verdict = _cached_listener_verdict(proc, port, proof_not_before)
        if hit:
            return verdict, False
    with _proof_gate:
        if not allow_report:
            # Double-checked: the prover we waited behind may have just
            # answered the question we carried.
            hit, verdict = _cached_listener_verdict(proc, port, proof_not_before)
            if hit:
                return verdict, False
        # Stamp BEFORE any evidence gathering: the stamp asserts "no evidence
        # in this verdict predates this instant", which only a start-time can
        # assert. A completion-time stamp would let a proof that began before
        # a caller's fence (its ps/lsof evidence gathered pre-fence) satisfy
        # that fence merely by finishing after it.
        proof_started = time.monotonic()
        verdict, via_report = _verify_child_listener_gated(proc, port, allow_report=allow_report)
        if not allow_report:
            _proof_cache = (id(proc), proc.pid, port, proof_started, verdict)
        return verdict, via_report


def _verify_child_listener_gated(
    proc: subprocess.Popen[bytes], port: int, *, allow_report: bool
) -> tuple[bool | None, bool]:
    """Verify this child owns *port* and say whether stdout was the proof.

    Callers hold ``_proof_gate`` (via :func:`_verify_child_listener`): the
    proof protocol stashes evidence in a per-proc slot that
    :func:`_port_owner` clears on entry, so concurrent provers would clobber
    each other's record→take window into false definitive failures.

    Global attribution remains the strongest result. On a blind host, the child
    and each captured descendant identity are checked independently. A
    descendant must keep the same start time immediately before and after its
    listener probe. The stdout report is accepted only during startup on a
    structurally blind host and is never reused as current listener-ownership
    evidence.
    """
    owner = _port_owner(port, proc)
    fresh_report = _child_reported_binding(proc, port)
    root_verdict = _root_process_identity_matches(proc, "after global ownership lookup")
    if root_verdict is not True:
        return root_verdict, False
    if owner == _OWNER_CHILD:
        identity = _take_port_owner_identity_proof(proc, port)
        if identity is None:
            return False, False
        current = _process_descendant_start_identity(identity)
        if current is None:
            return None, False
        if not _descendant_identity_matches(identity, current, "after global"):
            return False, False
        return True, False
    if owner == _OWNER_FOREIGN:
        return False, False

    per_process = platform_compat.process_owns_loopback_listener(proc.pid, port)
    if per_process is True:
        return _root_process_identity_matches(proc, "after direct listener probe"), False

    descendants = platform_compat.process_descendant_identities(proc.pid)
    # The owner verdict stays UNPROVEN for both tiers. Capability is separate:
    # a structural host has no independent attribution path, so negative
    # per-process answers from the same blind tool cannot become definitive.
    structurally_blind = owner == _OWNER_UNPROVEN and _structurally_blind_listener_attribution()
    # A capable global lookup that observed incomplete or unstable ownership
    # cannot be overwritten by later negative snapshots from a different seam.
    global_owner_inconclusive = owner == _OWNER_UNPROVEN and (
        platform_compat.IS_WINDOWS or platform_compat.listening_pid_tool_available()
    )
    inconclusive = (
        global_owner_inconclusive
        or descendants is None
        or structurally_blind
        or per_process is None
    )

    for identity in descendants or []:
        before = _process_descendant_start_identity(identity)
        if not _descendant_identity_matches(identity, before, "before"):
            inconclusive = True
            continue
        per_process = platform_compat.process_owns_loopback_listener(identity.pid, port)
        after = _process_descendant_start_identity(identity)
        if not _descendant_identity_matches(identity, after, "after"):
            inconclusive = True
            continue
        if per_process is True:
            return _root_process_identity_matches(proc, "after descendant listener probe"), False
        if per_process is None:
            inconclusive = True
    root_verdict = _root_process_identity_matches(proc, "after listener verification")
    if root_verdict is not True:
        return root_verdict, False
    if not inconclusive:
        return False, False
    if allow_report and fresh_report and structurally_blind:
        return True, True
    return None, False


def _recorded_state() -> bool | None:
    """Return True for reusable, False for failed, or None for inconclusive.

    Reuse and status share this decision so they cannot disagree about URL
    publication. A failed health probe or a completed ownership mismatch is a
    definitive negative. A capable host requires a current listener-owner proof
    on every reuse. A structurally blind host may publish only the startup result
    verified from the child report. Every later check preserves the live handle
    but withholds the URL because current ownership is unprovable.

    Callers hold :data:`_lock`.
    """
    global _last_reason
    if _proc is None or not _alive(_proc) or _info is None or _child_port is None:
        return False
    root_verdict = _root_process_identity_matches(_proc, "before reuse health check")
    if root_verdict is not True:
        if root_verdict is None:
            if _last_reason != _OWNERSHIP_REASON:
                logger.warning(
                    "browser view root identity is inconclusive before reuse on port %d; "
                    "keeping the process but withholding its URL",
                    _child_port,
                )
            _last_reason = _OWNERSHIP_REASON
            return None
        logger.warning(
            "browser view root identity changed before reuse on port %d",
            _child_port,
        )
        _last_reason = _OWNERSHIP_REASON
        return False
    if not _healthy(_info.port):
        reason = f"The browser view stopped answering on port {_info.port}"
        if _last_reason != reason:
            logger.warning(
                "live browser view process stopped answering on child port %d",
                _child_port,
            )
        _last_reason = reason
        return False
    root_verdict = _root_process_identity_matches(_proc, "after reuse health check")
    if root_verdict is not True:
        if root_verdict is None:
            if _last_reason != _OWNERSHIP_REASON:
                logger.warning(
                    "browser view root identity is inconclusive after reuse health check "
                    "on port %d; keeping the process but withholding its URL",
                    _child_port,
                )
            _last_reason = _OWNERSHIP_REASON
            return None
        logger.warning(
            "browser view root identity changed during reuse health check on port %d",
            _child_port,
        )
        _last_reason = _OWNERSHIP_REASON
        return False
    # A monotonic fence captured immediately before this call demands a
    # proof started strictly after it: the reuse decision must re-observe
    # the world every poll (a squatter takeover is detected on the very next
    # status call, never masked by a cached verdict), while the fresh
    # verdict it records is what relay_target's immediately-following read
    # consumes — one proof run per poll, not two.
    verified, _via_report = _verify_child_listener(
        _proc, _child_port, allow_report=False, proof_not_before=time.monotonic()
    )
    if verified is True:
        _last_reason = None
        return True
    if verified is False:
        if _last_reason != _OWNERSHIP_REASON:
            logger.warning(
                "browser view process no longer proves ownership of child port %d",
                _child_port,
            )
        _last_reason = _OWNERSHIP_REASON
        return False
    structurally_blind = _structurally_blind_listener_attribution()
    if structurally_blind:
        reason = _blind_listener_ownership_reason()
        if _last_reason != reason:
            logger.warning(
                "browser view listener ownership cannot be re-proved on port %d; "
                "keeping the process but withholding its URL",
                _child_port,
            )
        _last_reason = reason
        return None
    reason = _listener_attribution_failure_reason()
    if _last_reason != reason:
        logger.warning(
            "browser view ownership of child port %d is inconclusive; keeping the process "
            "but withholding its URL",
            _child_port,
        )
    _last_reason = reason
    return None


def _show_argv(command: list[str], port: int) -> list[str]:
    """Argv for the dashboard server.

    ``--host`` is always present and always loopback: omitting it yields an
    IPv6-only listener that ``127.0.0.1`` cannot reach.
    """
    return [*command, "show", "--port", str(port), "--host", LOOPBACK_HOST]


def _alive(proc: subprocess.Popen[bytes] | None) -> bool:
    return proc is not None and proc.poll() is None


def _close_process_pipes(proc: subprocess.Popen[bytes]) -> None:
    """Close every pipe held by the parent for *proc*."""
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(proc, name, None)
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()


def _reap(proc: subprocess.Popen[bytes]) -> None:
    """Terminate *proc* and its descendants, escalating to a kill.

    The CLI spawns a browser and helper processes, so signalling only the direct
    child leaves the tree behind holding the port.
    """
    with contextlib.suppress(Exception):
        platform_compat.kill_process_tree(proc.pid)
    try:
        proc.wait(timeout=_TERMINATE_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        logger.warning("playwright-cli show (pid %s) ignored terminate; killing", proc.pid)
    with contextlib.suppress(Exception):
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=_TERMINATE_GRACE_S)


def _spawn(command: list[str], port: int) -> subprocess.Popen[bytes] | None:
    """Start the dashboard server, or ``None`` if it cannot be spawned.

    Stdout is a child-specific proof channel. Playwright prints its exact
    listening address only after its HTTP server binds, so a daemon reader scans
    a finite nonblocking prefix for that line. Line, byte, and time limits bound
    the proof scan; after any limit, the daemon keeps draining and discarding
    stdout so a chatty long-lived child cannot fill its pipe. Stderr stays on
    ``DEVNULL`` because no readiness or ownership signal is defined there.

    ``start_new_session`` puts the child in its own process group on POSIX so
    the whole tree can be signalled at stop time without touching the gateway's
    own group.

    The child's socket root is the gateway-owned one (:func:`ui_socket_env`):
    the dashboard claims its singleton socket under it, and the Browser panel's
    launcher (:mod:`kiro_crew.browser_cli.launcher`) sends its reveal request
    there, so both must agree on a root the gateway knows.
    """
    env = cli_env()
    env.update(ui_socket_env(env))
    cli_version = installed_cli_version(command)
    try:
        proc = subprocess.Popen(
            _show_argv(command, port),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=platform_compat.IS_POSIX,
            env=env,
        )
    except OSError as exc:
        logger.warning("could not start playwright-cli show: %s", exc)
        return None

    root_identity = _capture_root_process_identity(proc.pid)
    if root_identity is None:
        logger.warning(
            "could not capture playwright-cli show root identity for pid %s",
            proc.pid,
        )
        _close_process_pipes(proc)
        _reap(proc)
        return None

    proof = _BindingProof(
        port=port,
        reported=threading.Event(),
        root_identity=root_identity,
        cli_version=cli_version,
    )
    setattr(proc, "_kirocrew_browser_view_binding", proof)
    stream = getattr(proc, "stdout", None)
    if stream is not None:
        reader = threading.Thread(
            target=_drain_child_output,
            args=(stream, port, proof, cli_version),
            name="browser-view-listener-proof",
            daemon=True,
        )
        if not _start_daemon_thread(reader):
            proof.invalidate()
            _close_process_pipes(proc)
            _reap(proc)
            return None
    return proc


def ensure_running(port: int | None = None) -> ShowInfo | None:
    """Return the running dashboard, starting it if needed.

    Idempotent: a process that is alive, answering, and still owns its listener
    is reused, so repeated calls from a panel mount do not spawn a second server.
    A dead recorded process or one with a definitive health or ownership failure
    is reaped before replacement. A live recorded process with inconclusive
    ownership stays running in a degraded state with no published URL.

    *port* pins the port the dashboard is reachable on; ``None`` (or ``0``)
    keeps the OS-assigned ephemeral default. A pin is never handed to the
    child directly — the module claims the pinned port itself with a bound
    listener (:class:`_Relay`, the atomic ownership proof) and relays to the
    child's own ephemeral port, so the operator-named port itself has no
    probe-to-bind window to race. On a structurally blind host the child gets
    ``--port 0`` and its private startup report supplies the relay target; all
    other hosts keep the fixed child-port attribution path. The pin applies
    when a server is (re)started — an already-healthy server is reused as-is,
    on whatever port it holds. The bind host stays :data:`LOOPBACK_HOST`
    regardless.

    ``None`` means no dashboard is available: the CLI is not installed, the
    pinned port is already taken by something else, or the server did not
    become healthy within the startup budget. ``status()`` carries the reason.
    """
    global _proc, _info, _relay, _last_reason, _child_port, _relay_token, _proof_cache
    with _lock:
        # Ownership is re-proved on reuse, not just at startup. A child that is
        # alive but not listening leaves its port free for a squatter, and
        # without this the next call would hand that squatter back as the panel.
        recorded_state = _recorded_state()
        if recorded_state is True:
            return _info
        if recorded_state is None:
            return None
        if _proc is not None:
            _reap(_proc)
            _proc = None
            _info = None
        if _relay is not None:
            _relay.close()
            _relay = None
        _child_port = None
        _last_reason = None
        # The identity key (id(proc), pid, port) already prevents the
        # replacement from consuming this entry; cleared anyway so every
        # instance-replacement site drops the cache the same way stop() and
        # relay teardown do.
        _proof_cache = None

        cli = cli_path()
        command = cli_command(cli) if cli is not None else None
        if command is None:
            return None

        _invalidate_listener_lookup_self_test_cache()
        structurally_blind = _structurally_blind_listener_attribution()
        relay: _Relay | None = None
        pin_listener: socket.socket | None = None
        if port:
            # Claim the pin BEFORE choosing the child's port. Two things follow
            # by construction: bind() either makes the pin ours until we close
            # it or raises because someone else holds it (no window in which a
            # squatter can be mistaken for us), and while the pin is bound
            # _free_port() cannot hand the same number back, so the child's
            # ephemeral port can never collide with the pin.
            pin_listener = _claim_listener(port)
            if pin_listener is None:
                logger.warning("configured browser view port %d is already in use", port)
                _last_reason = f"configured port {port} is already in use"
                return None
            requested_child_port = 0 if structurally_blind else _free_port()
            if requested_child_port:
                relay = _Relay.from_listener(pin_listener, requested_child_port)
                pin_listener = None
                if relay is None:
                    _last_reason = _RELAY_REASON
                    return None
        else:
            requested_child_port = 0 if structurally_blind else _free_port()
        proc = _spawn(command, requested_child_port)
        if proc is None:
            if relay is not None:
                relay.close()
            if pin_listener is not None:
                with contextlib.suppress(Exception):
                    pin_listener.close()
            _last_reason = "Couldn't start the browser view"
            return None
        if not _root_process_identity_matches(proc, "after spawn"):
            if relay is not None:
                relay.close()
            if pin_listener is not None:
                with contextlib.suppress(Exception):
                    pin_listener.close()
            logger.warning(
                "browser view root identity was unavailable or changed immediately after spawn"
            )
            _last_reason = _OWNERSHIP_REASON
            _reap(proc)
            return None

        child_port: int | None = requested_child_port or None
        public_port: int | None = port if port else child_port
        deadline = time.monotonic() + _STARTUP_TIMEOUT_S
        saw_unproven_responder = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                logger.warning(
                    "playwright-cli show exited during startup (rc=%s) for port %d",
                    proc.returncode,
                    requested_child_port,
                )
                if relay is not None:
                    relay.close()
                if pin_listener is not None:
                    with contextlib.suppress(Exception):
                        pin_listener.close()
                _last_reason = "The browser view stopped while it was starting"
                return None
            if not _root_process_identity_matches(proc, "during startup"):
                logger.warning(
                    "browser view root identity changed during startup for requested port %d",
                    requested_child_port,
                )
                if relay is not None:
                    relay.close()
                if pin_listener is not None:
                    with contextlib.suppress(Exception):
                        pin_listener.close()
                _last_reason = _OWNERSHIP_REASON
                _reap(proc)
                return None
            if child_port is None:
                reported_port = _child_reported_port(proc, requested_child_port)
                if type(reported_port) is not int or not 1 <= reported_port <= 65535:
                    time.sleep(_POLL_INTERVAL_S)
                    continue
                child_port = reported_port
                public_port = port if port else child_port
                if pin_listener is not None:
                    relay = _Relay.from_listener(pin_listener, child_port)
                    pin_listener = None
                    if relay is None:
                        _close_process_pipes(proc)
                        _reap(proc)
                        _last_reason = _RELAY_REASON
                        return None
            if _healthy(child_port):
                # Reachability is not identity. Prove the responder is ours
                # before adopting it; a squatter that won _free_port's window
                # answers this probe exactly as our child would. On a
                # structurally blind host, show selected the port itself and
                # reported it on this exact child's private stdout pipe.
                verified: bool | None
                via_report: bool
                root_identity_ok = _root_process_identity_matches(
                    proc, "after startup health check"
                )
                if not root_identity_ok:
                    verified, via_report = False, False
                else:
                    verified, via_report = _verify_child_listener(
                        proc, child_port, allow_report=True
                    )
                if verified is False:
                    logger.warning(
                        "another local process holds port %d — refusing to adopt "
                        "it as the browser view",
                        child_port,
                    )
                    if relay is not None:
                        relay.close()
                    if not root_identity_ok or not _root_process_identity_matches(
                        proc, "after failed startup ownership check"
                    ):
                        _last_reason = _OWNERSHIP_REASON
                    else:
                        _last_reason = (
                            f"another local process took port {child_port} before the "
                            f"browser view could bind it"
                        )
                    _reap(proc)
                    return None
                if verified is None:
                    # The HTTP responder may be a squatter. Keep polling only so
                    # the trusted child's post-bind stdout line can arrive.
                    saw_unproven_responder = True
                else:
                    if via_report:
                        logger.warning(
                            "cannot verify which process holds port %d (%s is "
                            "unavailable or cannot attribute a known local listener); "
                            "adopting only because the spawned child reported that "
                            "it bound a recognized loopback listener address",
                            child_port,
                            platform_compat.listening_pid_tool(),
                        )
                    _proc = proc
                    _relay = relay
                    _child_port = child_port
                    # New instance, new capability: rotating here (not lazily on
                    # first read) pins the token's lifetime to the instance whose
                    # surface it guards.
                    _relay_token = secrets.token_urlsafe(24)
                    assert public_port is not None
                    _info = ShowInfo(url=f"http://{LOOPBACK_HOST}:{public_port}", port=public_port)
                    return _info
            time.sleep(_POLL_INTERVAL_S)

        if child_port is None:
            logger.warning(
                "playwright-cli show did not report its child-selected loopback port "
                "within the startup budget"
            )
            _last_reason = _listener_banner_reason(proc)
        elif saw_unproven_responder:
            logger.warning(
                "cannot verify which process holds port %d and the spawned child "
                "did not report that it bound the exact loopback address; refusing "
                "to adopt the responder",
                child_port,
            )
            _last_reason = _listener_attribution_failure_reason()
        else:
            logger.warning(
                "playwright-cli show did not answer on port %d within the budget",
                child_port,
            )
            _last_reason = f"The browser view didn't answer on port {child_port} in time"
        if relay is not None:
            relay.close()
        if pin_listener is not None:
            with contextlib.suppress(Exception):
                pin_listener.close()
        _reap(proc)
        return None


def stop() -> None:
    """Stop the supervised dashboard child and its entire process tree.

    Reaping is scoped to the child we spawned: ``_spawn`` places it in its
    own session (``start_new_session=IS_POSIX``), so ``kill_process_tree``
    signals the whole group — the Node server, the browser, and any helpers
    — without touching processes outside that group. A global ``show --kill``
    is deliberately NOT issued because it would terminate an operator's own
    independently-launched ``playwright-cli show`` session, destroying their
    unsaved work.
    """
    global _proc, _info, _relay, _last_reason, _child_port, _relay_token, _proof_cache
    with _lock:
        if _proc is not None:
            _reap(_proc)
        if _relay is not None:
            _relay.close()
        _proc = None
        _info = None
        _relay = None
        _child_port = None
        _relay_token = None
        _last_reason = None
        # A verdict for the stopped instance must not outlive it. The key
        # (proc identity + pid + port) already misses for a replacement; this
        # just removes the stale entry outright.
        _proof_cache = None


#: Compared against the relay-token candidate when no token is recorded, so
#: the deny path costs one ``compare_digest`` whether a view is up or not.
#: ``raw_path`` keeps percent-encoding, so no request can ever spell the
#: literal NUL and accidentally (or deliberately) match it.
_RELAY_TOKEN_PLACEHOLDER = "\x00none"


def _relay_ownership_proof_for(
    proc: subprocess.Popen[bytes],
    child_port: int,
    *,
    proof_not_before: float | None = None,
) -> bool | None:
    """Tri-state ownership proof for a snapshotted relay target.

    Runs WITHOUT the supervisor lock, on values snapshotted under one lock
    hold: the process-liveness, root-identity, and listener-ownership checks
    spawn ``ps``/``lsof`` and cost tens of milliseconds, and holding the lock
    across them would serialize every concurrent asset fetch behind one
    request's probes (and contend with the status poll's own hold). Inside
    the prover, concurrent callers are single-flighted: the first through
    ``_proof_gate`` runs the probes, the rest share its cached verdict
    (:data:`_PROOF_CACHE_TTL_S`), so one page load's fan-out costs one proof
    run rather than dozens of subprocess spawns. ``proof_not_before`` demands
    a proof started strictly after the fence — the post-connect re-proof
    passes its own entry time so no evidence gathered before the connect can
    vouch for a connection established after it. The verdict answers for the SNAPSHOT — a
    caller acting on a definitive failure must re-check under the lock that
    the recorded state still describes this instance (see
    :func:`_teardown_relay_state_if_current`), because a stop/start cycle may
    have replaced it mid-proof. Deliberately no HTTP probe: this runs on the
    relay's per-request path.
    """
    if not _alive(proc):
        return False
    proof = _root_process_identity_matches(proc, "before relay target lookup")
    if proof is not True:
        return proof
    verdict, _via_report = _verify_child_listener(
        proc, child_port, allow_report=False, proof_not_before=proof_not_before
    )
    return verdict


def _teardown_relay_state_if_current(proc: subprocess.Popen[bytes], child_port: int) -> None:
    """Tear down relay state after a definitive proof failure — unless the
    state already moved on to a different instance.

    The proof ran outside the lock on a snapshot; by the time it fails, a
    stop/start cycle may have recorded a NEW child. Tearing down blindly
    would kill the new instance's target on the strength of the old one's
    corpse. The acquire is bounded like the gate's: if a start holds the
    lock past the bound, that start is already replacing the very state this
    teardown wanted gone, so skipping is correct as well as cheap.
    """
    if not _lock.acquire(timeout=_AUTHORIZE_ACQUIRE_TIMEOUT_S):
        return
    try:
        if _proc is proc and _child_port == child_port:
            _teardown_relay_state_locked()
    finally:
        _lock.release()


def _teardown_relay_state_locked() -> None:
    """Invalidate relay state and token after a definitive ownership failure.

    Callers hold ``_lock``. Mirrors :func:`stop` minus process reaping: the
    recorded target and its capability token die before a squatter on the
    released child port can inherit either.
    """
    global _info, _relay, _last_reason, _child_port, _relay_token, _proof_cache
    if _relay is not None:
        _relay.close()
        _relay = None
    _info = None
    _child_port = None
    _relay_token = None
    _last_reason = _OWNERSHIP_REASON
    _proof_cache = None


def relay_target() -> tuple[int, str] | None:
    """Return the current public port and capability only while ownership is proven.

    The trusted read: the owner-authed status payload uses it to build the
    relay path it discloses. The relay's own per-request path must NOT use
    it — :func:`relay_authorize` validates the caller's token BEFORE running
    the ownership proof, so an unauthenticated flood cannot buy these probes.
    A definitive proof failure invalidates the recorded relay state and token
    before a process can squat on the released child port. An inconclusive
    proof withholds the target without destroying state, so a later call can
    retry.

    Port and token come back as one pair snapshotted under one lock
    acquisition, so a caller can never pair one instance's token with
    another's port. The OS-level proofs then run OUTSIDE the lock on that
    snapshot (they spawn ``ps``/``lsof`` — holding the lock across them would
    serialize the relay's concurrent requests behind this poll), share the
    prover's single-flight verdict cache — so the status payload's two reads
    (:func:`status` then this) cost one proof run, not two — and a
    definitive failure tears down state only if it still describes the same
    instance. On the pinned path the port is the operator's (served by the
    in-process TCP relay), on the unpinned path the child's own — either way
    loopback and ours while the proof holds.
    """
    with _lock:
        if _info is None or _relay_token is None:
            return None
        proc = _proc
        child_port = _child_port
        pair = (_info.port, _relay_token)
        if proc is None or child_port is None:
            _teardown_relay_state_locked()
            return None
    proof = _relay_ownership_proof_for(proc, child_port)
    if proof is True:
        return pair
    if proof is not None:
        _teardown_relay_state_if_current(proc, child_port)
    return None


def relay_authorize(
    candidate: str, *, proof_not_before: float | None = None
) -> tuple[str, int | None]:
    """Validate a relay-token candidate, then prove ownership — in that order.

    The relay handler's per-request gate, in three phases:

    1. **Lock-free token pre-check.** The candidate is compared (constant
       time) against a snapshot of the current token read WITHOUT the
       supervisor lock — one module-global reference read, atomic under the
       GIL. An invalid candidate is refused right here, never touching the
       lock: a pre-auth flood of bad tokens therefore costs one
       ``compare_digest`` per request and cannot contend the lock at all, no
       matter how long :func:`ensure_running` holds it across its 30s
       startup poll. This is what keeps the shared thread pool out of reach
       of unauthenticated traffic — the earlier bounded-acquire design still
       let each bad-token request park on the lock for the bound, which a
       flood multiplied into pool exhaustion.

    2. **Bounded acquire + consistent snapshot.** Only a candidate that
       matched the live snapshot proceeds. The compare is REPEATED under the
       lock (the token may have rotated between the lock-free read and the
       acquire), and token, ports and process come from one hold, so the
       proof can never vouch for one instance while the port belongs to
       another. A caller that cannot get the lock within
       :data:`_AUTHORIZE_ACQUIRE_TIMEOUT_S` is refused as ``("busy", None)``
       — reachable only by holders of the current token, so the caller may
       be told to retry (a start window passes) without leaking anything to
       a prober.

    3. **Ownership proof OUTSIDE the lock.** The OS-level process/listener
       probes run on the snapshot with the lock released, so concurrent
       asset fetches don't serialize behind one request's ``lsof`` on the
       supervisor lock — inside the prover they share a single-flight
       verdict (one proof run per :data:`_PROOF_CACHE_TTL_S`, waiters
       consume it) instead of each spawning their own probes. A caller
       whose invariant needs a verdict no older than a specific instant
       passes ``proof_not_before`` (monotonic): the post-connect re-proof
       uses it so a verdict recorded before its connect can never vouch
       for that connection. A definitive failure tears down state and
       invalidates the token only if the recorded state still describes the
       proved instance (:func:`_teardown_relay_state_if_current`).

    Returns ``(outcome, port)``: ``("ok", port)`` on success, otherwise one
    of ``("busy", None)`` (supervisor lock held past the bounded wait — valid
    token, retryable), ``("view_down", None)`` (nothing recorded),
    ``("token_mismatch", None)``, or ``("ownership_unproven", None)`` (token
    matched, but the proof failed — definitive failures also tear down state
    and invalidate the token — or was inconclusive, which preserves state
    for a later retry). Outcomes feed the caller's audit record; every
    unauthenticated miss answers the same uniform wire response, while
    ``busy`` — reachable only with the token — may answer retryable.
    """
    snapshot = _relay_token
    if snapshot is None:
        # Burn the same compare as the recorded-token path, so a prober
        # cannot time the difference between "down" and "wrong token".
        hmac.compare_digest(candidate, _RELAY_TOKEN_PLACEHOLDER)
        return "view_down", None
    if not hmac.compare_digest(candidate, snapshot):
        return "token_mismatch", None

    if not _lock.acquire(timeout=_AUTHORIZE_ACQUIRE_TIMEOUT_S):
        return "busy", None
    try:
        if _info is None or _relay_token is None:
            return "view_down", None
        if not hmac.compare_digest(candidate, _relay_token):
            return "token_mismatch", None
        proc = _proc
        child_port = _child_port
        public_port = _info.port
        if proc is None or child_port is None:
            _teardown_relay_state_locked()
            return "ownership_unproven", None
    finally:
        _lock.release()

    proof = _relay_ownership_proof_for(proc, child_port, proof_not_before=proof_not_before)
    if proof is True:
        return "ok", public_port
    if proof is not None:
        _teardown_relay_state_if_current(proc, child_port)
    return "ownership_unproven", None


def status() -> dict[str, Any]:
    """Current dashboard state, without starting or stopping anything.

    ``unavailable`` is reported when the CLI is absent, and is distinct from
    ``stopped``: the first cannot be fixed by starting the server. A
    ``stopped`` state carries the last start attempt's failure reason when
    one is recorded — with a pinned port, "already in use" is the most likely
    misconfiguration, and reporting it is what separates a fixable setting
    from a mysteriously dead panel.
    """
    with _lock:
        cli = cli_path()
        command = cli_command(cli) if cli is not None else None
        if command is None:
            return {
                "status": "unavailable",
                "url": None,
                "port": None,
                "reason": "playwright-cli is not installed",
            }
        if _recorded_state() is True and _info is not None:
            return {
                "status": "running",
                "url": _info.url,
                "port": _info.port,
                "reason": None,
            }
        return {
            "status": "stopped",
            "url": None,
            "port": None,
            "reason": _last_reason,
        }
