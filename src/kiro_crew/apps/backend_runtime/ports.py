"""Port reservation and listener attribution.

A backend's port is reserved BEFORE its child binds it, under ``_lock`` and gated on the
spawn still owning its STARTING placeholder, so concurrent boot spawns can never be
handed one number. Whether a listener on a port is OURS is answered by process
identity -- our spawn or a descendant of it, or for an adopted backend owner PIDs whose
start-time identities were captured inside a health-check consistency sandwich --
never by the port merely being open.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.backend_runtime import _FACADE
from kiro_crew.apps.backend_runtime.pidfile import _proc_start_time, _read_pidfile
from kiro_crew.apps.backend_runtime.probe import _health_probe
from kiro_crew.apps.backend_runtime.tracking import _lock, _processes, _spawn_publication_owner
from kiro_crew.apps.manager import get_app_manifest

logger = logging.getLogger(_FACADE)


_MIN_PORT = 9100
_MAX_PORT = 9200

# Spawn survival check: poll the freshly-spawned child over a short grace window to
# confirm it survived its initial bind (an immediate exit -> EADDRINUSE crash-loop must
# be caught, see _start_app_backend_body). The loop returns early on either outcome -
# the child exiting, or our own child owning the listener - so a healthy backend pays
# the full window only where ownership cannot be proven (no port to observe, or no
# port->PID tool on the host); see _survived_spawn. Exposed as module constants so the
# test harness can widen the window: under heavy pytest-xdist parallelism (-n auto, ~32
# workers) a sandboxed child can take longer than the default window just to reach its
# exit, which would otherwise make the immediate-exit detection test flaky.
_SPAWN_SURVIVAL_CHECKS = 8
_SPAWN_SURVIVAL_INTERVAL = 0.2
_PID_ANCESTRY_MAX_DEPTH = 8  # bound the parent walk when proving listener ownership
_PORT_PROBE_TIMEOUT = 0.15  # cheap loopback gate before the costly port->PID lookup


_allocated_ports: dict[str, int] = {}  # app_name -> port


@dataclass(frozen=True)
class BackendTargetLease:
    """A verified backend record pinned against teardown."""

    port: int
    pid: int
    _process: Any = field(repr=False, compare=False)


_forward_leases_changed = threading.Condition(_lock)


def acquire_app_backend_target(app_name: str, secret: str) -> BackendTargetLease | None:
    """Lease the healthy gateway child spawned with *secret*, or fail closed."""
    if not secret:
        return None
    actual_digest = hashlib.sha256(secret.encode("utf-8")).digest()
    with _lock:
        ap = _processes.get(app_name)
        if (
            ap is None
            or not ap.healthy
            or ap.retiring
            or ap.proc is None
            or ap.proc.poll() is not None
            or not ap.proxy_secret_digest
            or not hmac.compare_digest(ap.proxy_secret_digest, actual_digest)
        ):
            return None
        ap.forward_leases += 1
        return BackendTargetLease(port=ap.port, pid=ap.pid, _process=ap)


def _adopted_record_still_bound(ap: Any) -> bool:
    """Whether current listener identities still match an adopted record."""
    try:
        current_owners = platform_compat.loopback_owner_pids(
            platform_compat.find_port_listeners(ap.port)
        )
    except Exception:  # noqa: BLE001 -- an unreadable identity fails closed
        return False
    if not current_owners:
        return False
    recorded = set(ap.adopted_pids)
    for pid in current_owners:
        if pid not in recorded:
            return False
        live_start = _proc_start_time(pid)
        if live_start is None or live_start != ap.adopted_start_times.get(pid):
            return False
    return True


def acquire_adopted_app_backend_target(app_name: str) -> BackendTargetLease | None:
    """Lease a healthy backend through its tracked adoption record."""
    with _lock:
        ap = _processes.get(app_name)
        if (
            ap is None
            or not ap.healthy
            or ap.retiring
            or ap.starting
            or ap.proc is not None
            or not ap.adopted_pids
            or set(ap.adopted_pids) != set(ap.adopted_start_times)
            or not _adopted_record_still_bound(ap)
        ):
            return None
        ap.forward_leases += 1
        return BackendTargetLease(port=ap.port, pid=ap.adopted_pids[0], _process=ap)


def release_app_backend_target(lease: BackendTargetLease) -> None:
    """Return a target lease and wake teardown waiting on the record."""
    with _forward_leases_changed:
        ap = lease._process
        if ap.forward_leases <= 0:
            logger.error("App %s backend target lease released twice", ap.app_name)
            return
        ap.forward_leases -= 1
        _forward_leases_changed.notify_all()


class PortUnavailableError(RuntimeError):
    """A fixed manifest port is already reserved by a different app."""


def _find_free_port() -> int:
    """Find a free TCP port in the app range.

    Callers that go on to SPAWN must use ``_reserve_free_port`` instead: this
    function only probes, so two concurrent callers can be handed the same port.
    """
    for port in range(_MIN_PORT, _MAX_PORT):
        if port in _allocated_ports.values():
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"No free ports in range {_MIN_PORT}-{_MAX_PORT}")


def _survived_spawn(proc: Any, port: int | None = None) -> bool:
    """Return whether a just-spawned child survived its initial bind.

    Detects the failure this guards against — an immediate exit, e.g. EADDRINUSE
    from a port collision — while NOT paying the full grace window when the child
    is healthy. Sleeping the whole ~1.6s budget on the happy path would add that
    much pure boot latency per app, which under concurrent boot is the single
    largest startup cost.

    The early exit is driven by POSITIVE evidence: once OUR OWN child owns the
    listening socket on *port*, it has completed the very bind whose failure this
    function exists to catch, so waiting longer cannot change the answer.

    Two things are deliberately NOT accepted as success:

    * **Elapsed liveness alone** — a child that crashes a few polls in (slow
      sandboxed interpreter, loaded host) would be mis-reported as started.
    * **Someone else's listener** — "the port is open" is not the same claim as
      "our child bound it". With a fixed manifest port, another app (or any
      unrelated process) can already hold it, and our child is then the one about
      to die of EADDRINUSE; treating that as survival would report a doomed pid as
      started and route two apps at one backend.

    Ownership accepts our pid OR any descendant of it, because the sandbox
    launcher execs the real server as a child. When ownership cannot be
    established at all (no port to observe, or no port->PID tool on the host), it
    degrades to polling the full budget.

    The ownership probe shells out to lsof (~150ms), so it is gated behind a cheap
    loopback connect and is not run on every poll: the deadline below stays honest
    about wall-clock rather than adding the probe's cost to each interval, which
    would otherwise make the failure path take LONGER than that budget.
    """

    can_check_owner = port is not None and platform_compat.listening_pid_tool_available()
    deadline = time.monotonic() + _SPAWN_SURVIVAL_CHECKS * _SPAWN_SURVIVAL_INTERVAL
    while True:
        time.sleep(_SPAWN_SURVIVAL_INTERVAL)
        if proc.poll() is not None:
            return False
        if (
            can_check_owner
            # Cheap gate first: no listener at all means there is nothing to
            # attribute, so skip the expensive port->PID lookup entirely.
            and _port_is_listening(port)  # type: ignore[arg-type]
            and _spawn_owns_listener(port, proc.pid)  # type: ignore[arg-type]
        ):
            return True
        if time.monotonic() >= deadline:
            return proc.poll() is None


def _port_is_listening(port: int) -> bool:
    """Whether anything accepts TCP connections on *port* (loopback, cheap)."""

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=_PORT_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def _listening_pids(port: int) -> list[int]:
    """PIDs holding a LISTEN socket on *port* (best-effort, never raises)."""

    try:
        return platform_compat.find_listening_pids(port)
    except Exception:  # noqa: BLE001 — a probe failure must never fail a spawn
        return []


def _probe_adoption_health(port: int, health_path: str) -> bool:
    """Whether an already-running backend answers its health check."""

    return _health_probe(port, health_path, timeout=3).healthy


def _capture_adopted_owners(
    app_name: str, port: int, health_path: str
) -> tuple[list[int], dict[int, str]] | None:
    """Owner PIDs + start-time identities for the backend answering on loopback.

    The health probe and the owner lookup are separate observations, so the
    responder can exit between them and the lookup would attribute ownership to
    a bystander (e.g. a coexisting v6-only wildcard listener the probe never
    reached). Close that window with a consistency sandwich: capture owners and
    identities, then require the health check to STILL answer and the owner set
    to read back unchanged. Any drift means the observations do not describe one
    stable backend — refuse adoption; the next start simply re-probes.

    Returns ``None`` (with a logged reason) when no owner is attributable, an
    owner's start-time identity is unreadable (an owner that cannot be
    positively named later cannot be stopped — refuse rather than adopt a
    backend the gateway could never revoke), or the sandwich detects drift.
    """
    owners: list[int] = platform_compat.loopback_owner_pids(
        platform_compat.find_port_listeners(port)
    )
    if not owners:
        logger.warning(
            "App %s: cannot record owning PIDs on 127.0.0.1:%d "
            "(port->PID tool unavailable?) — skipping adoption",
            app_name,
            port,
        )
        return None
    start_times: dict[int, str] = {}
    for pid in owners:
        st = _proc_start_time(pid)
        if st is not None:
            start_times[pid] = st
    if set(start_times) != set(owners):
        # An owner with no readable identity could never be signalled later —
        # stop and uninstall would skip it, leaving a third-party backend
        # running after its trust was revoked. Adoption is only offered when
        # every owner can be positively named, so refusal here fails closed:
        # the gateway declines to manage what it could not later stop.
        logger.warning(
            "App %s: start-time identity unreadable for owner PID(s) %s on "
            "port %d — refusing adoption (an owner that cannot be identified "
            "cannot be stopped later)",
            app_name,
            sorted(set(owners) - set(start_times)),
            port,
        )
        return None
    if not _probe_adoption_health(port, health_path):
        logger.warning(
            "App %s: backend on port %d stopped answering its health check "
            "while ownership was being recorded — skipping adoption",
            app_name,
            port,
        )
        return None
    owners_recheck = platform_compat.loopback_owner_pids(platform_compat.find_port_listeners(port))
    if set(owners_recheck) != set(owners):
        logger.warning(
            "App %s: port %d owners changed while ownership was being recorded "
            "(%s -> %s) — skipping adoption",
            app_name,
            port,
            owners,
            owners_recheck,
        )
        return None
    return owners, start_times


def _pid_is_self_or_descendant_of(pid: int, ancestor: int) -> bool:
    """Whether *pid* is *ancestor* or is descended from it (bounded walk)."""

    if pid == ancestor:
        return True
    current = pid
    for _ in range(_PID_ANCESTRY_MAX_DEPTH):
        try:
            parent = platform_compat.get_ppid(current)
        except Exception:  # noqa: BLE001
            return False
        if parent <= 0:
            return False
        if parent == ancestor:
            return True
        current = parent
    return False


def _spawn_owns_listener(port: int, spawn_pid: int) -> bool:
    """Whether the listener on *port* is our spawn (or one of its descendants)."""

    return any(_pid_is_self_or_descendant_of(pid, spawn_pid) for pid in _listening_pids(port))


class _SpawnOwnershipLost(RuntimeError):
    """The spawn does not own the placeholder that authorizes reservation."""


def _reserve_free_port(app_name: str) -> int:
    """Atomically pick a free port and record it against *app_name*.

    Boot starts app backends CONCURRENTLY, so selection and reservation must be
    one critical section. Probing without reserving lets two apps be handed the
    same port — both children then bind it and the loser dies with EADDRINUSE,
    which is the crash-loop the post-spawn survival check exists to catch. The
    reservation is overwritten with the real port on success and cleared on
    failure by the existing spawn bookkeeping.

    When the current spawn has an owner, its placeholder identity is checked in
    the same critical section as the reservation. A retired spawn therefore cannot
    claim a port after a later start has replaced its placeholder.
    """
    _spawn_owner = _spawn_publication_owner.get()
    with _lock:
        if _spawn_owner is not None and _processes.get(app_name) is not _spawn_owner:
            raise _SpawnOwnershipLost(app_name)
        port = _find_free_port()
        # Reservation ownership follows the process-table slot: a runtime writer must
        # own `_processes[app_name]`. Boot pre-claims run before placeholders exist;
        # the first owning spawn's identity-gated failure cleanup releases that claim.
        _allocated_ports[app_name] = port
    return port


def _claim_port(app_name: str, port: int) -> None:
    """Reserve a FIXED manifest port, refusing one another app already holds.

    ``_find_free_port`` skips ports already in ``_allocated_ports``, but
    without this up-front claim a fixed-port app's port would be recorded only
    AFTER spawning. During concurrent
    boot an auto-port app selecting inside that window could be handed the same
    number, so one of the two children would die of EADDRINUSE and its backend
    would stay unavailable. Claiming the fixed port up front closes that window.

    The claim must also FAIL when the port is already reserved: fixed ports are
    required to sit inside the auto range, so the reverse race is real (the auto
    app gets there first). Recording it anyway would map two apps to one port and
    reintroduce exactly the EADDRINUSE crash this is meant to prevent. Re-claiming
    the SAME app's own port is idempotent, so a retry/restart is never refused.

    Raises:
        PortUnavailableError: another app already holds *port*.
    """
    _spawn_owner = _spawn_publication_owner.get()
    with _lock:
        if _spawn_owner is not None and _processes.get(app_name) is not _spawn_owner:
            raise _SpawnOwnershipLost(app_name)
        holder = next(
            (name for name, taken in _allocated_ports.items() if taken == port),
            None,
        )
        if holder is not None and holder != app_name:
            raise PortUnavailableError(
                f"app {app_name} declares fixed port {port}, already reserved by {holder}"
            )
        # Reservation ownership follows the process-table slot: a runtime writer must
        # own `_processes[app_name]`. Boot pre-claims run before placeholders exist;
        # the first owning spawn's identity-gated failure cleanup releases that claim.
        _allocated_ports[app_name] = port


def spawned_backend_owns_pid(pid: int) -> bool:
    """Whether a backend THIS gateway spawned owns *pid*.

    Owning means *pid* is the spawned root or descends from it, because
    ``wrap_argv`` places a sandbox launcher between us and the real server —
    the same ownership shape :func:`_spawn_owns_listener` reads off a listener.

    Only a record holding a LIVE ``Popen`` answers, and that is the whole
    security value: an unreaped child's pid cannot be recycled by the kernel, so
    a root that answers here is a process this gateway started and still owns.
    ``poll() is None`` is what carries that, not ``proc is not None`` on its own —
    once a child exits and is reaped its pid is free for anyone. An ADOPTED
    backend belongs to another supervisor and carries no handle at all (see
    :func:`spawned_backend_names`), so it is refused rather than trusted on a pid
    this gateway cannot vouch for.

    The ancestry walk runs OUTSIDE ``_lock``: it reads ``/proc`` per candidate,
    and the snapshot taken under the lock is all the registry state it needs.
    """
    with _lock:
        roots = [
            ap.pid
            for ap in _processes.values()
            if ap.proc is not None and ap.pid > 0 and ap.proc.poll() is None
        ]
    return any(_pid_is_self_or_descendant_of(pid, root) for root in roots)


def recorded_backend_port(app_name: str) -> int | None:
    """The port THIS GATEWAY recorded for *app_name*'s backend, or None.

    Gateway-owned provenance, in preference order: the live tracking entry, then
    the pidfile written at spawn/adoption. Neither is reachable by the app — the
    pidfile lives under ``KIROCREW_HOME``, not in the app directory — which is
    what makes this usable as evidence when the app's own manifest is not.

    Must be read BEFORE :func:`stop_app_backend`, which drops both records.
    """
    with _lock:
        ap = _processes.get(app_name)
        if ap and ap.port:
            return int(ap.port)
    entry = _read_pidfile().get(app_name)
    if isinstance(entry, dict):
        port = entry.get("port")
        if isinstance(port, int) and _MIN_PORT <= port <= _MAX_PORT:
            return port
    return None


def unstopped_backend_port(app_name: str, *, port_hint: int | None = None) -> int | None:
    """The port *app_name*'s backend is still listening on after a stop, else None.

    Answers the one question :func:`stop_app_backend`'s boolean cannot: it returns
    ``False`` both for "there was nothing to stop" (never started, already dead,
    crashed) and for "something is running that I did not stop" (never adopted at
    boot, or adopted with no usable PIDs) — and ``True`` only means the process it
    was TRACKING is gone, which says nothing about a detached worker the app
    spawned for itself. Those need opposite handling, so the caller observes the
    port instead of reading a flag.

    ``port_hint`` is the gateway-recorded port from :func:`recorded_backend_port`,
    captured before the stop. It is preferred over the manifest because the
    manifest is ``app.json`` INSIDE the app directory — writable by any app trusted
    to run code, so an app could otherwise relabel its port (or claim ``auto``) to
    hide from this probe. The hint also covers ``port: auto`` backends, whose real
    port only the gateway ever knew.

    The manifest is the fallback for the case the hint cannot cover: a fixed-port
    backend this gateway never tracked at all (adoption skipped at boot), where the
    declared port is the only lead available. Only ``backend.entryPoint`` apps are
    considered there — an app whose backend is a loopback ``mcpServers`` URL is a
    process the gateway never spawned and does not own, so a listener on it is not
    an unstopped child. ``None`` means "nothing observed", not "definitely stopped".
    """
    if port_hint is not None:
        return port_hint if _port_is_listening(port_hint) else None
    try:
        manifest = get_app_manifest(app_name)
        if manifest is None or not manifest.backend.entryPoint:
            return None
        port_str = str(manifest.backend.port)
        if not port_str or port_str == "auto":
            return None
        port = int(port_str)
    except (AttributeError, TypeError, ValueError):
        return None
    if not (_MIN_PORT <= port <= _MAX_PORT):
        return None
    return port if _port_is_listening(port) else None
