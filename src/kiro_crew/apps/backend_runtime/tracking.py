"""The backend process table: records, lifecycle generations, and in-flight starts.

``_processes`` maps an app name to the ONE :class:`AppProcess` the gateway currently
tracks for it, and every lifecycle writer mutates it under ``_lock``. A record is
identified by the object, never by its name: a stop/start installs a new record under
the same name, and every retiring thread compares ``_processes.get(name) is ap``
before it acts. ``_lifecycle_generation`` is the per-app token a restart compares
to tell a later deliberate stop from a later start; ``_advance_lifecycle_locked`` is
its only writer. The STARTING placeholder a spawn inserts, the cross-process spawn
flock, and the wait on an in-flight spawn are the single-flight half of the same
table.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.backend_runtime import _FACADE
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(_FACADE)


# Serializes health-driven MCP reconciliation (see _set_backend_health). Deliberately
# NOT `_lock`: the reconcile does manifest + config file I/O, and holding `_lock` across
# it would block the reverse proxy's get_app_backend_port on every request and risk a
# deadlock through the bridges <-> backend import cycle.
# RE-ENTRANT: the health path acquires this and then calls into bridges, whose MCP and
# agent writers acquire it too (see health_reconcile_lock). A plain Lock would deadlock
# on that re-entry, and dropping it from either side would leave the two families of
# writer unordered again.
_health_reconcile_lock = threading.RLock()


@dataclass
class AppProcess:
    """Tracks a running app backend process."""

    app_name: str = ""
    port: int = 0
    pid: int = 0
    proc: subprocess.Popen | None = field(default=None, repr=False)
    log_fh: Any = field(default=None, repr=False)
    healthy: bool = False
    # The `healthy` value last SUCCESSFULLY reconciled into mcp.json, or None if nothing
    # has been written for this record yet. Distinct from `healthy` because the flag
    # moves even when the mcp.json write fails; the gap between them is what the watch
    # retries. Deliberately absent from to_dict(): internal bookkeeping, not API.
    mcp_healthy: bool | None = None
    started_at: float = 0.0
    log_path: str = ""
    # Stable identity persisted beside ``pid``. Restart/stop cleanup removes a pidfile
    # row only while both values still identify this process, so a concurrently-recorded
    # successor under the same app name cannot be forgotten.
    pid_start_time: str | None = None
    # The per-spawn ``KIROCREW_SPAWN_INSTANCE`` token stamped on this backend's
    # environment (None for an adopted record). It is what vouches the members of
    # the backend's process group once the leader itself has exited, so a stop that
    # finds the root dead can still drain the tree the root left behind.
    spawn_instance: str | None = None
    adopted_pids: list[int] = field(default_factory=list)
    # PID-reuse guard for the adopted set: pid -> platform_compat.process_start_time
    # token captured at adoption. stop signals a recorded PID only when its live
    # start time still POSITIVELY matches (same convention as the spawned-backend
    # reap); a missing or mismatched token means the PID may name another process
    # now, and it is never signalled.
    adopted_start_times: dict[int, str] = field(default_factory=dict)
    # True only for the transient placeholder a single-flighting spawn inserts while it
    # allocates a port + launches the process; replaced by the real record on success or
    # popped on failure. Concurrent start_app_backend calls see it and skip duplicate spawn.
    starting: bool = False
    # True when the GATEWAY created this record by starting or adopting the backend,
    # which is what makes the execution ceiling applicable to it. Set by
    # `_start_app_backend_body` alone, so it cannot be influenced by anything the app
    # writes: the alternative, reading the app's `installed.json` to decide whether the
    # ceiling applies, let an app trusted to run code delete its own metadata and have
    # the revocation sweep skip it. A record the gateway did not create carries no claim
    # about a process the gateway started, so the sweep leaves it alone.
    gateway_started: bool = False
    # The builtin CLASSIFICATION the admission gate reached on the validated execution
    # target, decided once when this record was created. A later re-check reads this
    # boolean and never re-resolves anything, which is the point: storing the path
    # instead deferred the decision to a `Path.resolve` at re-check time, and the app
    # owns that filesystem -- replacing its entry point with a symlink into the shipped
    # builtin root would have won the exemption after the fact. Re-deriving it from
    # `installed.json` is worse still, since `origin` is read verbatim from a file the
    # app can write. False denies, so anything unclassified is judged third-party.
    # Deliberately absent from to_dict(): internal bookkeeping.
    admitted_builtin: bool = False

    def is_running(self) -> bool:
        """Whether the tracked process is still alive.

        A backend we spawned answers from its own already-reaped exit status, so this
        costs no syscall to the app and cannot block. An ADOPTED backend belongs to
        another supervisor and we hold no handle for it, so the only honest answer is
        that we still track it — there, ``healthy`` (kept current by
        :func:`_watch_backend_health`) is the load-bearing signal.
        """
        if self.proc is None:
            return True
        return self.proc.poll() is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_name": self.app_name,
            "port": self.port,
            "pid": self.pid,
            "healthy": self.healthy,
            "running": self.is_running(),
            "started_at": self.started_at,
            "log_path": self.log_path,
        }


_processes: dict[str, AppProcess] = {}  # app_name -> AppProcess
# Carries the exact placeholder owned by the current spawn body without widening
# that body's long-standing two-argument seam (many tests replace it directly).
_spawn_publication_owner: ContextVar[AppProcess | None] = ContextVar(
    "app_backend_spawn_publication_owner", default=None
)
# Consecutive restart attempts survive replacement generations and reset only after one
# remains healthy for the sustained liveness window. Protected by `_lock` together with
# `_processes`.
_restart_attempts: dict[str, int] = {}
# Monotonic lifecycle identity for restart handoffs. Every deliberate stop and every
# public/external start advances the generation and records which transition won; the
# restart's own spawn deliberately does neither. Protected by ``_lock``. Transition
# bumps additionally take ``_health_reconcile_lock`` so a post-spawn compare + teardown
# is atomic with respect to a later explicit start.
_LIFECYCLE_START = "start"
_LIFECYCLE_STOP = "stop"
_lifecycle_generation: dict[str, tuple[int, str]] = {}


def _advance_lifecycle_locked(app_name: str, transition: str) -> tuple[int, str]:
    """Advance one app's lifecycle; caller holds ``_lock``."""
    generation = _lifecycle_generation.get(app_name, (0, _LIFECYCLE_START))[0] + 1
    state = (generation, transition)
    _lifecycle_generation[app_name] = state
    return state


_lock = threading.Lock()


def _await_inflight_spawn(app_name: str, timeout: float = 20.0) -> AppProcess | None:
    """Block until the concurrently-running spawn for ``app_name`` resolves — i.e. the
    STARTING placeholder is replaced by a real AppProcess (success) or cleared (failure).
    Returns the resolved process or None. Prevents a second caller from returning the
    bare port-0 placeholder (which would proxy to nothing)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _lock:
            cur = _processes.get(app_name)
            if cur is None:
                return None  # the in-flight spawn failed and cleared the placeholder
            if not getattr(cur, "starting", False):
                return cur  # resolved to a real process
        time.sleep(0.1)
    # Timed out waiting. If the spawn resolved to a real process right at the deadline,
    # return it. Otherwise, before clearing, PROBE the spawn owner's lifecycle
    # flock: provisioning (pip install) routinely outlives this timeout, and
    # clearing a placeholder whose owner is merely SLOW would let a retry
    # spawn a SECOND backend and overwrite the first one's tracking. The
    # owner holds app_backend_lifecycle_flock for the whole body, so a
    # non-blocking acquire failing means "still working" (leave the
    # placeholder, return None - the caller reports not-ready, it does not
    # respawn); acquiring it means the owner is GONE without cleanup (a hang
    # that escaped its own exception handling) - only then clear so a later
    # retry is possible.
    owner_gone = False
    try:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", app_name) or "_"
        lock_dir = config_dir() / "app_backend_locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        _probe_fd = os.open(str(lock_dir / f"{safe}.lock"), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            if platform_compat.try_acquire_lock(_probe_fd, exclusive=True):
                owner_gone = True
        finally:
            os.close(_probe_fd)  # closing releases the probe's own lock
    except OSError:
        owner_gone = False  # cannot prove the owner is gone: do not clear
    with _lock:
        cur = _processes.get(app_name)
        if cur is None:
            return None
        if not getattr(cur, "starting", False):
            return cur  # resolved to a real process at the deadline
        if not owner_gone:
            logger.info(
                "App %s backend spawn still in flight past the wait window "
                "(provisioning?) - leaving the placeholder in place",
                app_name,
            )
            return None
        _processes.pop(app_name, None)
        logger.warning("App %s backend spawn timed out — cleared stale placeholder", app_name)
        return None


def get_app_process(app_name: str) -> AppProcess | None:
    """Get the process info for a running app backend."""
    with _lock:
        return _processes.get(app_name)


def list_app_processes() -> list[dict[str, Any]]:
    """List all running app backend processes."""
    with _lock:
        return [ap.to_dict() for ap in _processes.values()]


def spawned_backend_names() -> list[str]:
    """App names whose backend THIS gateway process spawned (``proc`` set).

    The gateway-shutdown sweep stops exactly these. Adopted records
    (``proc is None``) are deliberately excluded: an adopted backend is an
    externally managed instance whose contract is to SURVIVE gateway exit and
    be re-probed and re-adopted on the next start (see the adoption comment in
    ``_start_app_backend_body``) — signalling it from shutdown would take down
    an independent service. Deriving the sweep from this tracking table rather
    than from persisted ``enabled`` metadata also keeps it honest in both
    directions: a child whose app was disabled cross-process (metadata-only)
    is still stopped, and an app with nothing running is never passed to
    :func:`stop_app_backend`, whose ``_forget_app_pid`` would otherwise erase
    the pidfile record that lets ``_reap_stale_app_backends`` recover a
    prior-generation orphan.
    """
    with _lock:
        return sorted(name for name, ap in _processes.items() if ap.proc is not None)


def get_app_backend_port(app_name: str) -> int | None:
    """Get the port for a running app backend (used by reverse proxy)."""
    with _lock:
        ap = _processes.get(app_name)
        return ap.port if ap and ap.healthy else None


def health_reconcile_lock() -> Any:
    """The serialization every writer of an app's MCP + agent state must hold.

    Exported for ``apps/bridges.py``, which acquires it around its mcp.json and agent
    materialization so a lifecycle registration and a health transition cannot interleave
    their decisions. Held re-entrantly: the health path already owns it before it calls
    into those writers.

    Ordering is always this lock FIRST, then ``_lock`` or bridges' ``_mcp_lock`` — never
    the reverse — so the two families of writer cannot deadlock against each other.
    """
    return _health_reconcile_lock


@contextlib.contextmanager
def app_backend_lifecycle_flock(app_name: str) -> Iterator[None]:
    """CROSS-PROCESS per-app lock over a backend's spawn transaction.

    The spawn path holds this lock across the whole body - provisioning
    (pip can run for minutes) through the pidfile record - and the
    in-flight-spawn waiter probes it non-blockingly: a held lock means the
    spawn owner is still working, so the waiter leaves the STARTING
    placeholder alone instead of clearing it and letting a retry spawn a
    SECOND backend mid-provisioning.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", app_name) or "_"
    lock_dir = config_dir() / "app_backend_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_dir / f"{safe}.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        with platform_compat.flock_exclusive(fd):
            yield
    finally:
        os.close(fd)
