"""App backend process management — spawn, health check, stop, and proxy config.

When an app declares a ``backend`` section in its manifest, KiroCrew manages
the backend process lifecycle: spawn on enable, health-check, stop on disable.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps.admission import app_admission_denied
from kiro_crew.apps.execution import (
    app_execution_denied,
    shipped_builtin_app_root,
    shipped_builtin_module_path,
)
from kiro_crew.apps.manager import app_dir, get_app_manifest, list_apps
from kiro_crew.apps.registry import minimal_env
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.sandbox import (
    build_resource_limit_preexec,
    cgroup_scope_argv,
    resource_limit_preexec,
    wrap_argv,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_MIN_PORT = 9100
_MAX_PORT = 9200
_HEALTH_CHECK_TIMEOUT = 5
_HEALTH_CHECK_RETRIES = 15
_HEALTH_CHECK_INTERVAL = 2.0

# Spawn survival check: poll the freshly-spawned child over a short grace window to
# confirm it survived its initial bind (an immediate exit -> EADDRINUSE crash-loop must
# be caught, see _start_app_backend_body). The loop breaks as soon as the process exits,
# so a healthy backend only ever pays the full window on a machine where the child is
# still starting up. Exposed as module constants so the test harness can widen the
# window: under heavy pytest-xdist parallelism (-n auto, ~32 workers) a sandboxed child
# can take longer than the default window just to reach its exit, which would otherwise
# make the immediate-exit detection test flaky.
_SPAWN_SURVIVAL_CHECKS = 8
_SPAWN_SURVIVAL_INTERVAL = 0.2
# Consecutive alive polls that confirm a child cleared its bind. An immediate
# failure (EADDRINUSE) exits within the first poll or two, so this is enough to
# distinguish "survived" from "about to die" without burning the full budget on
# every healthy app — see _survived_spawn.
_PID_ANCESTRY_MAX_DEPTH = 8  # bound the parent walk when proving listener ownership
_PORT_PROBE_TIMEOUT = 0.15  # cheap loopback gate before the costly port->PID lookup
# Ceiling on parallel boot spawns. Each one forks a sandboxed interpreter, so an
# unbounded fan-out on a host with many installed apps would trade boot latency
# for a CPU/memory spike at the worst possible moment.
_BOOT_SPAWN_MAX_WORKERS = 8

# Startup stale-reap timing (see _reap_stale_app_backends). The SIGTERM grace is
# applied PER orphan, not shared across the batch.
_REAP_SIGTERM_GRACE = 3.0  # seconds to wait for an orphan to exit after SIGTERM
_REAP_POLL_INTERVAL = 0.1  # liveness re-poll cadence during the grace window
_PS_TIMEOUT = 2  # seconds before a `ps` start-time probe is abandoned


# ---------------------------------------------------------------------------
# Port allocation
# ---------------------------------------------------------------------------

_allocated_ports: dict[str, int] = {}  # app_name -> port


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
    is healthy. The old loop slept its entire ~1.6s budget on the happy path and
    broke only on death, so every app added ~1.6s of pure boot latency; with
    concurrent boot that was the single largest startup cost.

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
    degrades to polling the full budget exactly as before.

    The ownership probe shells out to lsof (~150ms), so it is gated behind a cheap
    loopback connect and is not run on every poll: the deadline below stays honest
    about wall-clock rather than adding the probe's cost to each interval, which
    would otherwise make the failure path take LONGER than the original budget.
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

    return any(
        _pid_is_self_or_descendant_of(pid, spawn_pid) for pid in _listening_pids(port)
    )


def _reserve_free_port(app_name: str) -> int:
    """Atomically pick a free port and record it against *app_name*.

    Boot starts app backends CONCURRENTLY, so selection and reservation must be
    one critical section. Probing without reserving (the previous behavior, safe
    only while spawns were serialized) lets two apps be handed the same port —
    both children then bind it and the loser dies with EADDRINUSE, which is the
    crash-loop the post-spawn survival check exists to catch. The reservation is
    overwritten with the real port on success and cleared on failure by the
    existing spawn bookkeeping.
    """
    with _lock:
        port = _find_free_port()
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
    with _lock:
        holder = next(
            (name for name, taken in _allocated_ports.items() if taken == port),
            None,
        )
        if holder is not None and holder != app_name:
            raise PortUnavailableError(
                f"app {app_name} declares fixed port {port}, already reserved by {holder}"
            )
        _allocated_ports[app_name] = port


# ---------------------------------------------------------------------------
# Process tracking
# ---------------------------------------------------------------------------

@dataclass
class AppProcess:
    """Tracks a running app backend process."""

    app_name: str = ""
    port: int = 0
    pid: int = 0
    proc: subprocess.Popen | None = field(default=None, repr=False)
    log_fh: Any = field(default=None, repr=False)
    healthy: bool = False
    started_at: float = 0.0
    log_path: str = ""
    adopted_pids: list[int] = field(default_factory=list)
    # True only for the transient placeholder a single-flighting spawn inserts while it
    # allocates a port + launches the process; replaced by the real record on success or
    # popped on failure. Concurrent start_app_backend calls see it and skip duplicate spawn.
    starting: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_name": self.app_name,
            "port": self.port,
            "pid": self.pid,
            "healthy": self.healthy,
            "started_at": self.started_at,
            "log_path": self.log_path,
        }


_processes: dict[str, AppProcess] = {}  # app_name -> AppProcess
# Apps whose backends spawn real build workloads (vite/pip) and need the
# elevated-but-finite NOFILE ceiling as the workload's ANCESTOR. Every other
# app backend keeps the standard (operator-configurable) resource policy.
_BUILD_CAPABLE_APPS = frozenset({"dev-fleet"})

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Node.js binary resolution
# ---------------------------------------------------------------------------

def _resolve_nvm_path(binary_name: str) -> str | None:
    """Resolve a binary via nvm, returning its full path or None.

    Sources ~/.nvm/nvm.sh to find the nvm-managed node path, then resolves
    the requested binary relative to that directory.
    """
    nvm_dir = os.environ.get("NVM_DIR", os.path.expanduser("~/.nvm"))
    nvm_sh = os.path.join(nvm_dir, "nvm.sh")
    if not os.path.isfile(nvm_sh):
        return None
    try:
        result = subprocess.run(
            ["bash", "-c", f'source "{nvm_sh}" --no-use && nvm which current'],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            nvm_node = result.stdout.strip()
            target = os.path.join(os.path.dirname(nvm_node), binary_name)
            if os.path.isfile(target):
                return target
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _find_node_binary() -> str | None:
    """Find a usable node binary.

    Search order:
    1. nvm-managed node (via ~/.nvm/nvm.sh)
    2. System PATH
    """
    nvm_path = _resolve_nvm_path("node")
    if nvm_path:
        return nvm_path
    return shutil.which("node")


def _find_npm_binary() -> str | None:
    """Find npm binary, same search order as node."""
    nvm_path = _resolve_nvm_path("npm")
    if nvm_path:
        return nvm_path
    return shutil.which("npm")


def _is_asgi_entry(entry: Any) -> bool:
    """Heuristic: check if a Python entry point looks like an ASGI app."""
    try:
        content = entry.read_text(encoding="utf-8", errors="replace")
        return "FastAPI(" in content and "uvicorn" in content.lower()
    except OSError:
        return False


def _is_shell_entry(entry: Path) -> bool:
    """Heuristic: is this entry point a shell launcher script?

    True for a ``.sh`` file, or an extensionless executable whose first line
    is a non-Python shebang (e.g. ``bin/<name>`` with
    ``#!/usr/bin/env bash``). Files with any other extension (``.py``,
    ``.js``, ...) and python-shebang launchers are NOT shell entries — they
    keep their existing interpreter branches.
    """
    name = entry.name
    if name.endswith(".sh"):
        return True
    if "." in name:
        return False  # some other extension — not a bare launcher
    if not os.access(entry, os.X_OK):
        return False
    try:
        with open(entry, "rb") as fh:
            first_line = fh.readline(256)
    except OSError:
        return False
    return first_line.startswith(b"#!") and b"python" not in first_line


def _shebang_argv(entry: Path) -> list[str]:
    """Interpreter argv from a script's shebang, or ``["/bin/sh"]`` fallback.

    A non-executable script can't rely on kernel shebang exec, so re-create
    it: parse ``#!<interp> [arg]`` and return ``[interp, arg]`` (the kernel
    passes at most one argument; whitespace-splitting covers the
    ``#!/usr/bin/env bash`` form). Running bash source under ``/bin/sh``
    breaks on bash-isms like ``set -euo pipefail`` wherever sh is dash, so
    /bin/sh is only the last resort for a script with no shebang at all.
    """
    try:
        with open(entry, "rb") as fh:
            first = fh.readline(256)
    except OSError:
        return ["/bin/sh"]
    if not first.startswith(b"#!"):
        return ["/bin/sh"]
    try:
        parts = first[2:].decode("utf-8", "strict").strip().split()
    except UnicodeDecodeError:
        return ["/bin/sh"]
    return parts if parts else ["/bin/sh"]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def start_app_backend(app_name: str) -> AppProcess | None:
    """Start an app's backend process if it declares one.

    Returns the AppProcess on success, None if no backend declared.
    """
    manifest = get_app_manifest(app_name)
    if not manifest or not manifest.backend.entryPoint:
        return None

    await_inflight = False
    with _lock:
        if app_name in _processes:
            existing = _processes[app_name]
            # Already running (spawned proc alive, OR an adopted external instance) — reuse.
            if existing.proc and existing.proc.poll() is None:
                logger.info("App %s backend already running (pid %d)", app_name, existing.pid)
                return existing
            if existing.proc is None and existing.adopted_pids:
                logger.info("App %s backend already adopted (pids %s)", app_name, existing.adopted_pids)
                return existing
            # A concurrent start_app_backend is mid-spawn for this app (placeholder with
            # ``starting=True``). Without this guard two callers (gateway boot-reconcile
            # + an enable event) both passed the check, both allocated the SAME port
            # (the bind-test in _find_free_port closes its probe socket → TOCTOU), both
            # spawned, and the loser crash-looped on EADDRINUSE forever. Defer the wait
            # to OUTSIDE this lock (the await re-acquires _lock — calling it here would
            # self-deadlock the non-reentrant lock), then return the in-flight result.
            if getattr(existing, "starting", False):
                await_inflight = True
        if not await_inflight:
            # Reserve a STARTING placeholder so a concurrent call sees this spawn in flight.
            _processes[app_name] = AppProcess(app_name=app_name, starting=True, started_at=time.time())
    if await_inflight:
        logger.info("App %s backend is already starting — awaiting the in-flight spawn", app_name)
        return _await_inflight_spawn(app_name)

    # From here the spawn is single-flighted for this app. The body returns the real
    # AppProcess on success, or None on any failure / no-op path; in EITHER the None
    # case or an exception we must clear the STARTING placeholder so a later retry isn't
    # permanently blocked (and a success path replaces it with the real record).
    try:
        result = _start_app_backend_body(app_name, manifest)
    except Exception:
        _clear_failed_spawn_state(app_name)
        raise
    if result is None:
        _clear_failed_spawn_state(app_name)
    return result


def _clear_failed_spawn_state(app_name: str) -> None:
    """Release the STARTING placeholder and any port reservation for a failed spawn.

    The port must be released too, not just the placeholder: the spawn body now
    reserves/claims a port BEFORE binding it (so concurrent boot cannot hand the
    same number to two apps), so a failure that left the reservation behind would
    permanently retire that port from the pool for the rest of the process — and a
    long-lived gateway retrying a broken app would leak one port per attempt.
    Only released when the app has no live record, so this can never revoke the
    reservation of a successfully-running backend.
    """
    with _lock:
        cur = _processes.get(app_name)
        if cur is not None and getattr(cur, "starting", False):
            _processes.pop(app_name, None)
            cur = None
        if cur is None:
            _allocated_ports.pop(app_name, None)


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
    # return it. Otherwise the placeholder is still STARTING (a spawn body that hung
    # without raising — its owner's None/exception cleanup never fired) — clear it here
    # so a later retry can attempt a fresh spawn instead of re-entering this 20s wait
    # forever (the app would otherwise be wedged in 'starting' until a gateway restart).
    # If the body does eventually finish it will find the entry gone and its own cleanup
    # is a guarded no-op; the starting= guard ensures we never drop a started real proc.
    with _lock:
        cur = _processes.get(app_name)
        if cur is not None and not getattr(cur, "starting", False):
            return cur  # resolved to a real process at the deadline
        if cur is not None and getattr(cur, "starting", False):
            _processes.pop(app_name, None)
            logger.warning("App %s backend spawn timed out — cleared stale placeholder", app_name)
        return None


def _start_app_backend_body(app_name: str, manifest) -> AppProcess | None:
    """The spawn body, single-flighted by the STARTING placeholder set in
    :func:`start_app_backend`. Returns the real AppProcess on success or None on any
    failure; the caller clears the placeholder on None/exception."""
    root = app_dir(app_name)
    entry_point = manifest.backend.entryPoint
    # Module-style entry point (e.g. "kiro_crew.apps.builtins.<name>"):
    # used by built-in apps that live inside the KiroCrew package itself.
    # Heuristics:
    #   - no path separator,
    #   - no script-file extension (.py/.js/.ts/.mjs/.cjs/.sh) — those are
    #     paths, not module dotted-names,
    #   - has a dot (i.e. is a dotted module path),
    #   - and no file with that literal name exists under the app root.
    is_module_entry = (
        "/" not in entry_point
        and not entry_point.endswith((".py", ".js", ".ts", ".mjs", ".cjs", ".sh"))
        and "." in entry_point
        and not (root / entry_point).exists()
    )

    # Bind the exemption to the code this spawn will actually execute.  A
    # module-style builtin is trusted only when its real package manifest names
    # this app and the ``python -m`` target exists under that package.  File
    # backends execute from the mutable installed-app tree and remain third-party.
    execution_path = (
        shipped_builtin_module_path(app_name, entry_point)
        if is_module_entry
        else root / entry_point
    )
    denied = app_execution_denied(
        app_name,
        action="backend_spawn",
        app_root=execution_path,
        caller="gateway",
    )
    if denied:
        logger.warning("Refusing to spawn third-party app %s backend: %s", app_name, denied)
        return None

    if is_module_entry:
        entry = None  # sentinel; no file path for module-style entries
    else:
        entry = root / entry_point
        if not entry.is_file():
            logger.error("App %s backend entry point not found: %s", app_name, entry)
            return None
        # Path containment backstop (mirrors module_loader hook-path check): the
        # persisted manifest is spawned at boot without re-running validate(), so
        # reject an entryPoint that resolves outside the app root (absolute path
        # or '..' traversal).
        try:
            if not entry.resolve().is_relative_to(root.resolve()):
                logger.error(
                    "App %s backend entry point escapes app root: %s (resolved %s)",
                    app_name, entry, entry.resolve(),
                )
                return None
        except (OSError, ValueError):
            logger.error(
                "App %s backend entry point path resolution failed: %s", app_name, entry,
            )
            return None

    # Resolve port. An auto port is RESERVED under the lock, not merely probed:
    # boot spawns run concurrently, so select-then-spawn would hand the same port
    # to two apps and crash-loop the loser on EADDRINUSE.
    port_str = manifest.backend.port
    if port_str == "auto":
        port = _reserve_free_port(app_name)
    else:
        try:
            port = int(port_str)
            if not (_MIN_PORT <= port <= _MAX_PORT):
                logger.error(
                    "App %s: port %d outside allowed range %d-%d",
                    app_name, port, _MIN_PORT, _MAX_PORT,
                )
                return None
            # Claim it immediately so a concurrently-starting auto-port app cannot
            # be handed this same number before we bind it. If that app already
            # took the port, refuse THIS spawn rather than double-book it: the
            # bind would fail anyway, and reporting it here names the real cause
            # instead of surfacing an opaque EADDRINUSE crash.
            try:
                _claim_port(app_name, port)
            except PortUnavailableError as exc:
                logger.error("App %s backend cannot start: %s", app_name, exc)
                return None
        except ValueError:
            port = _reserve_free_port(app_name)

    # Prepare log directory (needed early for adopt path)
    log_dir = root / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "backend.log"

    # Check if the port is already in use by a healthy instance
    if port_str != "auto":
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(("127.0.0.1", port))
            # Port occupied — probe health endpoint before giving up
            healthy = False
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{manifest.backend.healthCheck}",
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    healthy = resp.status < 400
            except (urllib.error.URLError, OSError):
                pass

            if healthy:
                try:
                    sel().log_api_access(
                        caller="gateway", operation="app_backend_adopt",
                        outcome="adopted", resources=f"{app_name} port={port}",
                    )
                except Exception as exc:
                    logger.debug("SEL audit failed for app %s backend adopt: %s", app_name, exc)
                # Record PIDs listening on this port at adoption time
                adopted_pids: list[int] = []
                try:
                    lsof_result = subprocess.run(
                        ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if lsof_result.returncode == 0 and lsof_result.stdout.strip():
                        for pid_str in lsof_result.stdout.strip().split("\n"):
                            try:
                                adopted_pids.append(int(pid_str.strip()))
                            except ValueError:
                                pass
                except (OSError, subprocess.TimeoutExpired):
                    pass
                if not adopted_pids:
                    logger.warning(
                        "App %s: cannot record PIDs on port %d (lsof unavailable?) — skipping adoption",
                        app_name, port,
                    )
                    return None
                logger.info("App %s: healthy instance already on port %d — adopting (pids=%s)", app_name, port, adopted_pids)
                ap = AppProcess(
                    app_name=app_name, port=port, pid=0, proc=None,
                    healthy=True, started_at=time.time(), log_path=str(log_path),
                    adopted_pids=adopted_pids,
                )
                # Adopted (externally-managed) backends are deliberately NOT
                # recorded for the startup stale-reap: the reap SIGTERMs a whole
                # process GROUP (safe only for our own start_new_session children),
                # whereas an external process's group may hold unrelated processes.
                # If the gateway dies, the external instance keeps running and is
                # simply re-probed and re-adopted on the next start — so reaping it
                # would kill a healthy service we would immediately re-adopt. stop's
                # adopted path kills only the lsof-revalidated PIDs for this reason.
                with _lock:
                    _processes[app_name] = ap
                    _allocated_ports[app_name] = port
                return ap
            else:
                try:
                    sel().log_api_access(
                        caller="gateway", operation="app_backend_spawn",
                        outcome="rejected_port_unhealthy",
                        resources=f"{app_name} port={port}",
                    )
                except Exception as exc:
                    logger.debug("SEL audit failed for app %s port rejection: %s", app_name, exc)
                logger.warning(
                    "App %s: port %d occupied by unhealthy process — "
                    "kill it manually then retry", app_name, port,
                )
                return None
        except OSError:
            pass  # port is free — proceed to spawn

    # Install Python dependencies into a per-app venv (isolated from KiroCrew runtime)
    req_file = root / "requirements.txt"
    if req_file.is_file():
        venv_dir = root / ".venv"
        _env = minimal_env()  # don't leak secrets to pip/venv subprocesses
        try:
            if not venv_dir.exists():
                venv_cmd, _ = wrap_argv(
                    ["python3", "-m", "venv", str(venv_dir)], mode="standard"
                )
                venv_cmd = cgroup_scope_argv(venv_cmd)  # cgroup DoS ceiling
                subprocess.run(
                    venv_cmd,
                    check=True, capture_output=True, timeout=60, env=_env,
                    preexec_fn=resource_limit_preexec(),
                )
            pip_bin = str(venv_dir / "bin" / "pip")
            pip_cmd, _ = wrap_argv(
                [pip_bin, "install", "--quiet", "--disable-pip-version-check",
                 "-r", str(req_file)], mode="standard"
            )
            pip_cmd = cgroup_scope_argv(pip_cmd)  # cgroup DoS ceiling
            subprocess.run(
                pip_cmd,
                capture_output=True, timeout=60, env=_env,
                preexec_fn=resource_limit_preexec(),
            )
        except Exception as exc:
            logger.warning("Failed to install deps for app %s: %s", app_name, exc)

    # Spawn process — use manifest backend type if available, fall back to heuristic
    # Pass the gateway's resolved config home explicitly: under pods or any
    # KIROCREW_HOME override, the backend must read the SAME apps dir the
    # gateway minted the app secret into — minimal_env() strips the var.
    _platform_extra: dict[str, str] = {}
    if os.environ.get("KIROCREW_PROJECT_DIR"):
        # Platform var (same class as KIROCREW_HOME): the resolved project
        # checkout. minimal_env() strips it; backends need it to locate the
        # gateway's source checkout (e.g. dev-fleet worktree discovery).
        _platform_extra["KIROCREW_PROJECT_DIR"] = os.environ["KIROCREW_PROJECT_DIR"]
    if os.environ.get("KIROCREW_EDITION_DIR"):
        # Platform var, same class as the above: whether this gateway is an
        # EDITION composition root. A backend that stages frontend build output
        # into the served static/dist must know, because a rebuild it drives
        # cannot recompose the edition (the build env deliberately withholds the
        # edition opt-in) and staging a stock SPA would silently replace the
        # edition dashboard with upstream's. minimal_env() strips it, so without
        # this the backend cannot tell an edition install from a stock one and
        # any such guard reads as "stock" everywhere. A path, not a secret; the
        # opt-in (KIROCREW_ALLOW_EDITION) is deliberately NOT propagated, so a
        # backend can detect an edition but never manufacture consent to compile
        # one.
        _platform_extra["KIROCREW_EDITION_DIR"] = os.environ["KIROCREW_EDITION_DIR"]
    if os.environ.get("KIROCREW_DEVFLEET_REPO"):
        # Operator-declared main-checkout override (same trust class as the
        # KIROCREW_DEVFLEET_BIN_* overrides below). dev-fleet reads it as the
        # highest-priority repo discovery hint, ahead of KIROCREW_PROJECT_DIR
        # — which packaged installs point at the app bundle (no .git), leaving
        # only the ~/kirocrew fallback. minimal_env() strips the var, so
        # without this forward the documented override silently never reaches
        # the backend and the fleet renders empty. A path, not a secret.
        _platform_extra["KIROCREW_DEVFLEET_REPO"] = os.environ["KIROCREW_DEVFLEET_REPO"]
    for _k, _v in os.environ.items():
        # Operator-declared trusted-binary overrides (unit-file owned):
        # backends resolve credential-bearing tools through these instead of
        # the inherited PATH; minimal_env() would otherwise strip them.
        if _k.startswith("KIROCREW_DEVFLEET_BIN_"):
            _platform_extra[_k] = _v
    env = minimal_env(
        PORT=str(port),
        KIROCREW_APP_NAME=app_name,
        KIROCREW_HOME=str(config_dir()),
        **_platform_extra,
    )
    # Inject the per-app proxy secret so the backend can verify the
    # X-KiroCrew-Proxy HMAC the gateway signs on every forwarded request
    # (CWE-306). Without it the loopback backend would trust any local caller.
    try:
        _proxy_secret = (root / ".app_secret").read_text().strip()
        if _proxy_secret:
            env["KIROCREW_PROXY_SECRET"] = _proxy_secret
    except OSError:
        pass
    entry_str = str(entry) if entry else entry_point

    # Prefer explicit backend type from manifest over content sniffing
    backend_type = manifest.backend.type if manifest.backend else ""

    # --- Node.js backend ---
    # Note: module-style entry points (entry is None) are always Python
    # builtin apps and never declare a Node.js backend, so this branch is
    # safe to evaluate before the module-style branch below.
    if entry is not None and (backend_type == "node" or (
        not backend_type and entry_str.endswith((".js", ".mjs", ".cjs"))
    )):
        node_bin = _find_node_binary()
        if not node_bin:
            logger.error(
                "App %s declares a Node.js backend but no node binary found. "
                "Searched: nvm, PATH.",
                app_name,
            )
            return None
        cmd = [node_bin, entry_str]
        cwd = str(root)
        # Pass PORT as env var — Node.js apps typically read process.env.PORT
        env["NODE_ENV"] = "production"

        # Install npm dependencies if package.json exists and node_modules is missing
        pkg_json = root / "package.json"
        node_modules = root / "node_modules"
        if pkg_json.is_file() and not node_modules.is_dir():
            npm_bin = _find_npm_binary()
            if npm_bin:
                logger.info("Installing npm deps for app %s", app_name)
                try:
                    sel().log_api_access(
                        caller="gateway", operation="app_backend_npm_install",
                        outcome="started", resources=f"{app_name}",
                    )
                except Exception as exc:
                    logger.debug("SEL audit failed for npm install %s: %s", app_name, exc)
                try:
                    sandboxed_npm, _ = wrap_argv(
                        [npm_bin, "install", "--production", "--no-audit", "--no-fund"],
                        mode="standard",
                    )
                    sandboxed_npm = cgroup_scope_argv(
                        sandboxed_npm
                    )  # cgroup DoS ceiling
                    subprocess.run(
                        sandboxed_npm,
                        cwd=str(root), env=env, capture_output=True, timeout=120,
                        preexec_fn=resource_limit_preexec(),
                    )
                except Exception as exc:
                    logger.warning("Failed to install npm deps for app %s: %s", app_name, exc)

    # --- Module-style Python builtin (e.g. kiro_crew.apps.builtins.<name>) ---
    # Module-style entries have no file path — invoke via `python -m <module>`.
    # Run under the gateway's own python interpreter (sys.executable) so the
    # module path resolves against the gateway's installed packages, with
    # cwd at the KiroCrew source root so relative imports inside the module
    # work without venv setup.
    elif entry is None:
        python_bin = sys.executable
        cmd = [python_bin, "-m", entry_point]
        cwd = str(Path(__file__).resolve().parent.parent.parent)

    # --- Exec (shell-launcher) backend ---
    # Explicit `backend.type: "exec"` (exec the entry point file as-is — also
    # the escape hatch for compiled/binary launchers the auto-detect can't
    # identify), a `.sh` entry point, or an extensionless executable with a
    # non-Python shebang (e.g. `bin/<name>` with `#!/usr/bin/env bash` — the
    # common launcher-script pattern) is executed directly rather than
    # falling through to the Python branch (which would run bash source under
    # the Python interpreter and die on `set -euo pipefail`). Same
    # wrap_argv() sandbox + cgroup scope as every other branch.
    elif backend_type == "exec" or (not backend_type and _is_shell_entry(entry)):
        if not platform_compat.IS_POSIX:
            # Exec backends rely on POSIX shebang exec and /bin/sh — neither
            # exists on native Windows. Fail fast with a clear message instead
            # of an undefined Popen crash.
            logger.error(
                "App %s declares an exec (shell launcher) backend (%s) which "
                "is not supported on native Windows. Use a Python or Node "
                "entry point instead.",
                app_name,
                entry_str,
            )
            return None
        if os.access(entry, os.X_OK):
            cmd = [entry_str]
        else:
            # Not executable (e.g. lost the exec bit in transit) — the kernel
            # won't honor the shebang, so invoke its interpreter explicitly.
            # /bin/sh only for a script with no shebang at all (bash source
            # under dash-as-sh dies on `set -euo pipefail`).
            cmd = [*_shebang_argv(entry), entry_str]
        cwd = str(root)

    # --- ASGI (Python) backend ---
    elif backend_type == "asgi" or (
        not backend_type and _is_asgi_entry(entry)
    ):
        venv_python = str(root / ".venv" / "bin" / "python3")
        # Fall back to the gateway's own interpreter (sys.executable) rather than a bare
        # "python3": a bare name relies on PATH, which isn't guaranteed (e.g. some
        # build environments ship only a versioned interpreter, so execvp("python3") raises
        # FileNotFoundError and the backend dies immediately). Matches the module-style
        # branch above.
        python_bin = venv_python if (root / ".venv" / "bin" / "python3").is_file() else sys.executable
        # Derive the module path for uvicorn (e.g. backend.app:app)
        rel = entry.relative_to(root)
        parts = list(rel.parts)
        if len(parts) > 2 and parts[0] == "src":
            cwd = str(root / "src")
            module_path = ".".join(parts[1:]).removesuffix(".py")
        else:
            cwd = str(root)
            module_path = ".".join(parts).removesuffix(".py")
        cmd = [
            python_bin, "-m", "uvicorn",
            f"{module_path}:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ]

    # --- Plain Python backend (default) ---
    else:
        venv_python = str(root / ".venv" / "bin" / "python3")
        # See the ASGI branch: prefer the venv python, else the gateway's own interpreter
        # (sys.executable) — a bare "python3" relies on PATH and isn't always present.
        python_bin = venv_python if (root / ".venv" / "bin" / "python3").is_file() else sys.executable
        cmd = [python_bin, entry_str]
        cwd = str(root)

    # Apply OS-level sandbox to app backend process
    sandboxed_cmd, cleanup_path = wrap_argv(cmd, mode="standard")
    sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling

    logger.info(
        "Spawning app %s backend: %s", app_name, " ".join(sandboxed_cmd),
    )
    try:
        sel().log_api_access(
            caller="gateway", operation="app_backend_spawn",
            outcome="started", resources=f"{app_name} port={port}",
        )
    except Exception as exc:
        logger.debug("SEL audit failed for app %s backend spawn: %s", app_name, exc)

    try:
        log_fh = open(log_path, "w")
        # Process-group isolation so stop_app_backend can tree-kill the app. Pass
        # both flags explicitly (NOT via **dict unpack — that breaks mypy's Popen
        # overload resolution on the build fleet): start_new_session=True is a
        # no-op on Windows, creationflags resolves to 0 (no-op) on POSIX.
        try:
            proc = subprocess.Popen(
                sandboxed_cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                env=env,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
                # Build-capable apps get the elevated-but-finite NOFILE
                # ceiling: the backend is the ANCESTOR of its build workloads
                # (vite/pip) and a 1024 hard cap starves every descendant.
                # All other apps keep the standard configured policy.
                preexec_fn=(build_resource_limit_preexec()
                            if app_name in _BUILD_CAPABLE_APPS
                            else resource_limit_preexec()),
            )
        except OSError:
            log_fh.close()
            raise
    except OSError as exc:
        logger.error("Failed to start app %s backend: %s", app_name, exc)
        return None

    # Verify the child SURVIVED its initial bind. A port collision (e.g. another
    # process grabbed the assigned port between our free-port probe and the child's
    # bind) makes the backend exit almost immediately with EADDRINUSE. Without this
    # check we'd return a 'started' record for a dead pid, the caller would proxy to a
    # dead port (502), and repeated enable/health calls would respawn onto the SAME
    # doomed port forever (the observed crash-loop). Poll over a short grace window
    # (the sandbox launcher adds startup latency, so a single 0.4s check can miss a
    # crash); if it exits, surface the real reason from its log and fail (caller clears
    # the placeholder; a fresh spawn then re-runs free-port selection).
    if not _survived_spawn(proc, port):
        tail = ""
        try:
            with open(log_path, "r") as _lf:
                tail = "".join(_lf.readlines()[-8:]).strip()[-600:]
        except Exception:  # noqa: BLE001
            pass
        log_fh.close()
        collided = "address already in use" in tail.lower() or "errno 98" in tail.lower()
        logger.error(
            "App %s backend exited immediately (rc=%s) on port %d%s — %s",
            app_name, proc.returncode, port,
            " [PORT COLLISION]" if collided else "",
            tail or "(no output)",
        )
        return None

    # Surviving the bind check does not mean the backend is healthy: we have only
    # confirmed it did not crash on startup. It is intentionally returned with
    # healthy=False; the background health-check loop started below flips it to
    # healthy=True once the health endpoint responds.
    ap = AppProcess(
        app_name=app_name,
        port=port,
        pid=proc.pid,
        proc=proc,
        log_fh=log_fh,
        healthy=False,
        started_at=time.time(),
        log_path=str(log_path),
    )

    with _lock:
        _processes[app_name] = ap
        _allocated_ports[app_name] = port

    logger.info("Started app %s backend on port %d (pid %d)", app_name, port, proc.pid)

    # Persist identity for the startup stale-reap (see _reap_stale_app_backends).
    _record_app_pid(app_name, proc.pid, port)

    # Health check in background
    threading.Thread(
        target=_health_check_loop,
        args=(app_name, port, manifest.backend.healthCheck),
        daemon=True,
    ).start()

    return ap


def _wait_for_pids(pids: list[int], timeout: float = 2.0) -> None:
    """Poll until all PIDs have exited or timeout is reached.

    Uses short sleeps (0.1s) to avoid blocking the thread for the full
    timeout duration when processes exit quickly.

    Uses pid_liveness (tri-state), NOT pid_exists (which collapses EPERM to
    True): an adopted app-backend PID can be recycled between kill_pid(SIGTERM)
    and this poll to a different user's process. pid_exists would keep it in
    still_alive for the whole 2.0s deadline; pid_liveness returns UNSIGNALABLE
    for the not-ours case and we treat that as done, restoring the fast-return
    behavior the old ``os.kill(pid, 0) except OSError`` had. Never raw
    ``os.kill(pid, 0)`` — that TERMINATES the target on Windows.
    """
    deadline = time.monotonic() + timeout
    remaining = list(pids)
    while remaining and time.monotonic() < deadline:
        still_alive: list[int] = []
        for pid in remaining:
            if platform_compat.pid_liveness(pid) == platform_compat.PID_ALIVE:
                still_alive.append(pid)
        remaining = still_alive
        if remaining:
            time.sleep(0.1)


def stop_app_backend(app_name: str) -> bool:
    """Stop an app's backend process."""
    with _lock:
        ap = _processes.pop(app_name, None)
        _allocated_ports.pop(app_name, None)

    _forget_app_pid(app_name)

    if not ap:
        return False

    if ap.proc and ap.proc.poll() is None:
        try:
            sel().log_api_access(
                caller="gateway", operation="app_backend_stop",
                outcome="sigterm", resources=f"{app_name} pid={ap.proc.pid}",
            )
        except Exception as exc:
            logger.debug("SEL audit failed for app_backend_stop %s: %s", app_name, exc)
        try:
            # killpg(getpgid) on POSIX, taskkill /T on Windows — via platform_compat.
            platform_compat.kill_process_tree(ap.proc.pid, platform_compat.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            ap.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                platform_compat.kill_process_tree(ap.proc.pid, platform_compat.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                sel().log_api_access(
                    caller="gateway", operation="app_backend_stop",
                    outcome="sigkill_escalation",
                    resources=f"{app_name} pid={ap.proc.pid}",
                )
            except Exception as exc:
                logger.debug("SEL audit failed for sigkill_escalation %s: %s", app_name, exc)
    elif not ap.proc and ap.port:
        # Adopted process (proc=None) — kill only PIDs we recorded at adoption
        if not ap.adopted_pids:
            logger.warning(
                "Cannot stop adopted backend for %s on port %s: no recorded PIDs — "
                "refusing to kill unknown processes",
                app_name, ap.port,
            )
            try:
                sel().log_api_access(
                    caller="gateway", operation="app_backend_stop_adopted",
                    outcome="rejected_no_pids",
                    resources=f"{app_name} port={ap.port}",
                )
            except Exception as exc:
                logger.debug("SEL audit failed for rejected_no_pids %s: %s", app_name, exc)
            # Restore tracking so a retry is possible after re-adoption
            with _lock:
                _processes.setdefault(app_name, ap)
                if ap.port:
                    _allocated_ports.setdefault(app_name, ap.port)
            return False
        try:
            target_pids: set[int] = set(ap.adopted_pids)

            # Verify adopted PIDs still belong to this port (guards against
            # PID recycling between adoption and stop).
            try:
                lsof_result = subprocess.run(
                    ["lsof", "-ti", f":{ap.port}", "-sTCP:LISTEN"],
                    capture_output=True, text=True, timeout=5,
                )
                if lsof_result.returncode == 0 and lsof_result.stdout.strip():
                    current_pids: set[int] = set()
                    for pid_str in lsof_result.stdout.strip().split("\n"):
                        try:
                            current_pids.add(int(pid_str.strip()))
                        except ValueError:
                            pass
                    # Only kill PIDs that are both adopted AND still on this port
                    target_pids = target_pids & current_pids
            except (OSError, subprocess.TimeoutExpired):
                # lsof unavailable at stop time — proceed with adopted PIDs
                # (they were validated at adoption time)
                pass

            pids: list[int] = []
            for pid in target_pids:
                if pid <= 0:
                    continue
                try:
                    # kill_pid: os.kill on POSIX, taskkill /F on Windows.
                    platform_compat.kill_pid(pid, platform_compat.SIGTERM)
                    pids.append(pid)
                except (ProcessLookupError, OSError):
                    pass
            try:
                sel().log_api_access(
                    caller="gateway", operation="app_backend_stop_adopted",
                    outcome="sigterm",
                    resources=f"{app_name} port={ap.port} pids={pids}",
                )
            except Exception as exc:
                logger.debug("SEL log_api_access failed for app_backend_stop_adopted: %s", exc)
            # Wait for graceful shutdown (non-blocking poll)
            _wait_for_pids(pids, timeout=2.0)
            # Escalate to SIGKILL if still alive
            escalated: list[int] = []
            for pid in pids:
                # pid_exists (not os.kill(pid,0), which terminates on Windows);
                # kill_pid dispatches os.kill / taskkill per platform.
                if platform_compat.pid_exists(pid):
                    try:
                        platform_compat.kill_pid(pid, platform_compat.SIGKILL)
                        escalated.append(pid)
                    except (ProcessLookupError, OSError):
                        pass
            if escalated:
                try:
                    sel().log_api_access(
                        caller="gateway", operation="app_backend_stop_adopted",
                        outcome="sigkill_escalation",
                        resources=f"{app_name} port={ap.port} pids={escalated}",
                    )
                except Exception as exc:
                    logger.debug("SEL log_api_access failed for app_backend_stop_adopted sigkill: %s", exc)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            logger.warning(
                "Failed to stop adopted backend for %s on port %s: %s",
                app_name, ap.port, exc,
            )
            # Restore tracking so a retry is possible
            with _lock:
                _processes.setdefault(app_name, ap)
                if ap.port:
                    _allocated_ports.setdefault(app_name, ap.port)
            return False

    if ap.proc:
        logger.info("Stopped app %s backend (pid %d)", app_name, ap.pid)
    else:
        logger.info("Stopped adopted app %s backend on port %s", app_name, ap.port)
    if ap.log_fh:
        try:
            ap.log_fh.close()
        except OSError:
            pass
    return True


def get_app_process(app_name: str) -> AppProcess | None:
    """Get the process info for a running app backend."""
    with _lock:
        return _processes.get(app_name)


def list_app_processes() -> list[dict[str, Any]]:
    """List all running app backend processes."""
    with _lock:
        return [ap.to_dict() for ap in _processes.values()]


def get_app_backend_port(app_name: str) -> int | None:
    """Get the port for a running app backend (used by reverse proxy)."""
    with _lock:
        ap = _processes.get(app_name)
        return ap.port if ap and ap.healthy else None


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


# ---------------------------------------------------------------------------
# Health checking
# ---------------------------------------------------------------------------

def _gate_mcp_registration(app_name: str, port: int, *, healthy: bool) -> None:
    """Register the app's MCP servers once its backend is healthy, or scrub them if not.

    Called from the health-check loop so the global mcp.json never carries an HTTP MCP url
    for an app whose backend isn't actually serving (registering with an optimistic
    pre-health port would leave a dead url for an enabled app whose backend never
    became healthy, breaking every kiro-cli session). On
    health success we (re)register with the confirmed live port; on failure we deregister
    so no dead entry survives. Never raises — registration must not crash the health loop."""
    try:
        if healthy:
            # circular import: bridges imports backend.get_app_backend_port, so deferring
            # this import to call time breaks the backend ↔ bridges module cycle.
            from kiro_crew.apps.bridges import reregister_app_mcp_servers

            reregister_app_mcp_servers(app_name, live_port=port)
        else:
            # circular import: see above — bridges ↔ backend cycle, deferred to call time.
            from kiro_crew.apps.bridges import _deregister_mcp_servers

            removed = _deregister_mcp_servers(app_name)
            if removed:
                logger.warning(
                    "Scrubbed %d MCP server(s) for app %s after backend failed health check",
                    removed, app_name,
                )
    except Exception as exc:  # noqa: BLE001 — health loop must never crash on reconcile
        logger.warning("Health-gated MCP registration failed for app %s: %s", app_name, exc)


def _health_check_loop(app_name: str, port: int, health_path: str) -> None:
    """Poll the health endpoint until it responds or we give up."""
    url = f"http://127.0.0.1:{port}{health_path}"
    for attempt in range(_HEALTH_CHECK_RETRIES):
        time.sleep(_HEALTH_CHECK_INTERVAL)
        with _lock:
            if app_name not in _processes:
                return  # stopped while we were checking
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=_HEALTH_CHECK_TIMEOUT) as resp:
                if resp.status < 400:
                    with _lock:
                        # Stopped/disabled between the health poll and here: do NOT
                        # register MCP for a backend that's no longer tracked — that would
                        # write the exact dead-URL entry this guard prevents.
                        # Mirror the top-of-loop guard.
                        if app_name not in _processes:
                            return
                        _processes[app_name].healthy = True
                    logger.info(
                        "App %s backend healthy (port %d, attempt %d)",
                        app_name, port, attempt + 1,
                    )
                    # Health-gated MCP registration: only now that the
                    # backend has passed /health do we write its HTTP MCP url (live port) to
                    # global mcp.json. Registering before this could leave a dead-but-enabled
                    # url for an app whose backend never became healthy — the kiro-cli outage.
                    _gate_mcp_registration(app_name, port, healthy=True)
                    return
        except (urllib.error.URLError, OSError):
            pass

    logger.warning(
        "App %s backend failed health check after %d attempts",
        app_name, _HEALTH_CHECK_RETRIES,
    )
    # Backend never became healthy: scrub any optimistic/stale MCP entry so kiro-cli does
    # not keep dialing a dead port on every session (the reverted-outage shape).
    _gate_mcp_registration(app_name, port, healthy=False)


# ---------------------------------------------------------------------------
# Gateway startup — start backends for all enabled apps
# ---------------------------------------------------------------------------

# ── App-backend PID persistence + startup stale-reap ──────────────────────────
#
# App backends run in their OWN session (start_new_session=True) and are NOT in
# the gateway's process group, so when the liveness probe SIGKILLs a wedged
# gateway (no on_cleanup runs) they orphan, reparent to PID 1, and accumulate
# across restarts. We persist each spawned backend's (pid, start_time) to a
# pidfile and reap any survivors of a PRIOR generation on the next clean start.
# See docs/system-specs/modules/app-kit-platform.md.


# Serializes the pidfile read-modify-write. _record_app_pid runs on the
# to_thread worker that spawns a backend (both the runtime app-enable path and
# the startup reconcile offload start_app_backend via asyncio.to_thread) while
# _forget_app_pid runs on the to_thread worker that stops one — distinct OS
# threads, so without this lock their non-atomic read-modify-writes of the
# whole JSON dict lose each other's entries.
_pidfile_lock = threading.Lock()


def _pidfile_path() -> Path:
    return config_dir() / "app_backends.pids.json"


def _proc_start_time(pid: int) -> str | None:
    """Stable per-process start time, or None if unavailable.

    PID-reuse guard: a recorded pid whose live start_time no longer matches has
    been recycled to an unrelated process and MUST NOT be killed. The value must
    be stable across gateway restarts (the reap compares a string recorded by a
    prior generation against one read now), so it cannot use ``hash()`` — that
    is salted per interpreter by ``PYTHONHASHSEED``.

    Linux reads ``/proc/<pid>/stat`` field 22 (start time in clock ticks since
    boot): monotonic, locale-independent, and far finer than 1s, so same-second
    PID reuse cannot alias. macOS falls back to ``ps -o lstart=`` (1s resolution,
    locale/TZ-formatted); a format/resolution drift there can only make the guard
    FAIL SAFE (decline to reap → orphan leaks), never kill the wrong process.
    """
    try:
        if sys.platform == "linux":
            stat = Path(f"/proc/{pid}/stat").read_text()
            # The comm field can contain spaces/parens; split after the last ')'.
            fields = stat.rsplit(")", 1)[1].split()
            return fields[19]  # field 22 (1-based) = starttime in clock ticks
        out = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            stderr=subprocess.DEVNULL, timeout=_PS_TIMEOUT,
        )
        return out.decode().strip() or None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` names a live process.

    ``PermissionError`` (EPERM) means the process EXISTS but is owned by another
    uid — alive, not gone — so it must NOT be conflated with
    ``ProcessLookupError``. Treating EPERM as "gone" would skip the SIGKILL of a
    SIGTERM-ignoring orphan whose credentials changed.

    Routed through ``platform_compat.pid_exists`` — a raw ``os.kill(pid, 0)``
    on Windows does NOT probe liveness (sig 0 is CTRL_C_EVENT there); the shim
    uses ``OpenProcess`` on Windows and the identical ``os.kill(pid, 0)`` /
    EPERM-is-alive logic on POSIX, so POSIX behavior is unchanged.
    """
    return platform_compat.pid_exists(pid)


def _read_pidfile() -> dict[str, dict[str, Any]]:
    try:
        with open(_pidfile_path()) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        # A corrupt/half-written pidfile (e.g. a SIGKILL mid-write before atomic
        # writes landed, or a leftover from an older build) silently disabling
        # the reap is exactly the leak this feature exists to prevent — log it.
        logger.warning("App-backend pidfile unreadable (%s); stale-reap skipped this start", exc)
        return {}


def _write_pidfile(data: dict[str, dict[str, Any]]) -> None:
    # Atomic temp-file + rename (fsync): the whole point of the pidfile is to
    # survive a gateway SIGKILL, so a non-atomic open("w") that truncates first
    # would leave an empty/partial file if the kill lands mid-write.
    try:
        atomic_write(_pidfile_path(), json.dumps(data), fsync=True)
    except OSError as exc:
        logger.debug("Could not write app-backend pidfile: %s", exc)


def _record_app_pid(app_name: str, pid: int, port: int) -> None:
    """Persist a spawned backend's identity for the startup stale-reap. Never raises."""
    if pid <= 0:
        return
    try:
        # Compute start_time BEFORE taking the lock: on macOS _proc_start_time
        # shells out to `ps` (up to _PS_TIMEOUT), and holding _pidfile_lock
        # across that slow IO would serialize concurrent enable/stop/uninstall
        # ops behind it. Mirrors the reap path's validate-lock-free /
        # store-under-lock discipline.
        start_time = _proc_start_time(pid)
        with _pidfile_lock:
            data = _read_pidfile()
            data[app_name] = {"pid": pid, "start_time": start_time, "port": port}
            _write_pidfile(data)
    except Exception as exc:  # noqa: BLE001 — persistence must never break a spawn
        logger.debug("Could not record app pid for %s: %s", app_name, exc)


def _forget_app_pid(app_name: str) -> None:
    """Drop an app's pidfile entry (called on a clean stop). Never raises."""
    try:
        with _pidfile_lock:
            data = _read_pidfile()
            if data.pop(app_name, None) is not None:
                _write_pidfile(data)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not forget app pid for %s: %s", app_name, exc)


def _reap_stale_app_backends() -> int:
    """Reap app backends left running by a prior gateway generation.

    Runs at gateway startup BEFORE the new generation spawns (off the event loop
    — see start_enabled_app_backends' caller). A recorded pid is terminated only
    when it is still alive AND its current start_time POSITIVELY matches the
    recorded one (PID-reuse guard); if identity cannot be confirmed the pid is
    left alone — declining to reap leaks a recoverable orphan, whereas killing an
    unverifiable pid could signal an unrelated recycled process group. Returns
    the count terminated.
    """
    with _pidfile_lock:
        data = _read_pidfile()
    if not data:
        return 0
    # ``handled`` = entries we either terminated or confirmed already-gone; they
    # are removed from the pidfile at the end. Entries left out of ``handled``
    # (identity unconfirmed but still alive) are KEPT for a later attempt so a
    # transient ps failure does not permanently abandon a real orphan.
    # ``handled`` maps each handled app_name -> the exact pidfile entry we acted
    # on. The final merge drops an entry ONLY if it is still identical: a
    # concurrent enable that re-recorded the app with a NEW pid mid-scan writes a
    # different entry, which must survive (clobbering it would re-introduce the
    # orphan leak this feature prevents).
    handled: dict[str, Any] = {}
    reaped: list[tuple[str, int, Any]] = []
    for app_name, entry in data.items():
        try:
            pid = int(entry.get("pid", 0))
        except (TypeError, ValueError):
            handled[app_name] = entry  # malformed entry — drop
            continue
        if pid <= 0:
            handled[app_name] = entry
            continue
        # NEVER raw ``os.kill(pid, 0)`` — that TERMINATES the process on Windows.
        # ``pid_liveness`` returns DEAD/ALIVE/UNSIGNALABLE (uid-owned-by-other on
        # POSIX; unknown errno also maps to UNSIGNALABLE). Preserve the original
        # three-way policy: drop-dead, skip-unsignalable, proceed-alive.
        liveness = platform_compat.pid_liveness(pid)
        if liveness == platform_compat.PID_DEAD:
            handled[app_name] = entry  # already gone — drop
            continue
        if liveness == platform_compat.PID_UNSIGNALABLE:
            handled[app_name] = entry
            logger.info("Skipping stale-reap of %s pid %d: not owned by gateway", app_name, pid)
            continue
        recorded_st = entry.get("start_time")
        live_st = _proc_start_time(pid)
        if not recorded_st or live_st is None or live_st != recorded_st:
            # Identity unconfirmed: no baseline captured, ps failed now, or the
            # pid was recycled. Do NOT kill, and KEEP the entry (omit from
            # ``handled``) so a future start can retry once ps recovers.
            logger.info(
                "Skipping stale-reap of %s pid %d: start_time unconfirmed (recycled or unreadable)",
                app_name, pid,
            )
            continue
        try:
            platform_compat.kill_process_tree(pid, platform_compat.SIGTERM)
        except (ProcessLookupError, OSError):
            handled[app_name] = entry  # gone between the probe and the signal
            continue
        handled[app_name] = entry
        # Carry recorded_st so the delayed SIGKILL can re-confirm identity before
        # signalling (PID-reuse guard, below).
        reaped.append((app_name, pid, recorded_st))
        try:
            sel().log_api_access(
                caller="gateway", operation="app_backend_stale_reap",
                outcome="sigterm", resources=f"{app_name} pid={pid}",
            )
        except Exception as exc:
            logger.debug("SEL audit failed for app_backend_stale_reap %s: %s", app_name, exc)
    # Escalate to SIGKILL for any matched orphan that ignored SIGTERM. Each pid
    # gets its OWN grace window — a shared deadline would let the first slow
    # exiter consume the whole budget and SIGKILL the rest instantly. No lock is
    # held here: the kill/poll touches no shared file and can sleep for seconds.
    for app_name, pid, recorded_st in reaped:
        deadline = time.monotonic() + _REAP_SIGTERM_GRACE
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(_REAP_POLL_INTERVAL)
        if not _pid_alive(pid):
            continue
        # Re-confirm identity before the destructive SIGKILL. The pid may have
        # exited and been recycled to an unrelated process during the grace
        # window (macOS's ~99998 PID space makes reuse materially likely within
        # _REAP_SIGTERM_GRACE); without this, os.killpg below could signal an
        # innocent recycled process group. Same PID-reuse guard the SIGTERM path
        # applies — skip the kill on mismatch (leak-not-mis-kill).
        if _proc_start_time(pid) != recorded_st:
            logger.info(
                "Skipping stale-reap SIGKILL of %s pid %d: start_time changed (PID recycled)",
                app_name, pid,
            )
            continue
        try:
            platform_compat.kill_process_tree(pid, platform_compat.SIGKILL)
        except (ProcessLookupError, OSError):
            continue
        try:
            sel().log_api_access(
                caller="gateway", operation="app_backend_stale_reap",
                outcome="sigkill", resources=f"{app_name} pid={pid}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("SEL audit failed for app_backend_stale_reap sigkill %s: %s", app_name, exc)
    # Drop only the entries we handled, re-reading under the lock so a concurrent
    # enable/disable that wrote during the scan is merged, not clobbered. Drop an
    # entry ONLY if it still equals what we handled: a mid-scan re-record (new
    # pid) yields a different entry that must be kept.
    with _pidfile_lock:
        current = _read_pidfile()
        for app_name, handled_entry in handled.items():
            if current.get(app_name) == handled_entry:
                current.pop(app_name, None)
        _write_pidfile(current)
    if reaped:
        logger.info("Startup stale-reap: terminated %d orphaned app backend(s)", len(reaped))
    return len(reaped)


def start_enabled_app_backends() -> list[str]:
    """Start backends for all enabled apps that declare one.

    Called during gateway startup to restore app backends.
    Returns list of app names that were started.
    """
    # Reap app backends left running by a prior (e.g. SIGKILLed) gateway
    # generation before starting the new one. See the RFC,
    # "Apps as supervised sandboxed children".
    _reap_stale_app_backends()

    from kiro_crew.apps.manager import _app_activation_denied

    apps = list_apps()

    # Boot reconcile (regression fix): scrub global
    # mcp.json entries for any installed-but-NOT-enabled app that declares MCP servers.
    # A disabled app's backend is not running, so its HTTP MCP url points at a dead port;
    # left in ~/.kiro/settings/mcp.json it breaks EVERY kiro session (connect failure →
    # "transient 5xx" → 3 retries → hard error). Enable's deregister can be missed (crash
    # mid-enable, a resources-mismatch branch), so reconcile at boot before starting any
    # backend. Enabled apps are (re)registered with their live port via the health-gate.
    for app_info in apps:
        if app_info.get("enabled"):
            continue
        name = app_info.get("name", "")
        manifest = app_info.get("manifest", {})
        if not name or not manifest.get("mcpServers"):
            continue
        try:
            # circular import: bridges imports from backend, so defer to call time.
            from kiro_crew.apps.bridges import _deregister_mcp_servers

            removed = _deregister_mcp_servers(name)
            if removed:
                logger.info(
                    "Boot reconcile: scrubbed %d stale MCP server(s) for disabled app %s",
                    removed, name,
                )
        except Exception as exc:  # noqa: BLE001 — boot must never crash on reconcile
            logger.warning("Boot MCP reconcile failed for disabled app %s: %s", name, exc)

    # Executable-resource reconcile: restore agents, skills, cron definitions,
    # and MCP config only for apps admitted by every activation boundary. A
    # policy tightened after install must revoke stale derivative resources,
    # not merely decline to start the backend.
    for app_info in apps:
        if not app_info.get("enabled"):
            continue
        name = app_info.get("name", "")
        try:
            from kiro_crew.apps.bridges import (
                _deregister_agents,
                _deregister_mcp_servers,
                _deregister_skills,
                reconcile_app_skills,
                register_app,
            )
        except Exception as exc:  # noqa: BLE001 — boot must never crash on reconcile
            logger.warning("Boot resource reconcile unavailable: %s", exc)
            break

        # Governance/admission/execution vetting is deny-by-default. Builtins
        # remain exempt from signature/allowlist admission, but their execution
        # exemption still requires immutable shipped name + path provenance.
        try:
            gov_denied = _app_activation_denied(name)
            adm_denied = None
            if not gov_denied and app_info.get("origin") != "builtin":
                adm_denied = app_admission_denied(
                    name, manifest=get_app_manifest(name), action="boot"
                )
            execution_denied = None
            if not gov_denied and not adm_denied:
                execution_denied = app_execution_denied(
                    name,
                    action="resource_boot_reconcile",
                    app_root=shipped_builtin_app_root(name),
                    caller="gateway",
                )
        except Exception as exc:  # noqa: BLE001 — vetting error == denial
            gov_denied = f"governance/admission/execution vetting raised: {exc}"
            adm_denied = None
            execution_denied = None

        denied = gov_denied or adm_denied or execution_denied
        if denied:
            try:
                _deregister_agents(name)
                _deregister_skills(name)
                _deregister_mcp_servers(name)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Boot resource reconcile: FAILED to revoke resources for "
                    "denied app %s: %s",
                    name,
                    exc,
                )
            else:
                logger.warning(
                    "Boot resource reconcile: revoked executable resources for "
                    "denied app %s: %s",
                    name,
                    denied,
                )
            continue

        try:
            registration = register_app(name)
            if registration.errors:
                logger.warning(
                    "Boot resource reconcile for app %s completed with errors: %s",
                    name,
                    registration.errors,
                )
            reconcile_app_skills(name)
        except Exception as exc:  # noqa: BLE001 — boot must never crash on reconcile
            logger.warning("Boot resource reconcile failed for app %s: %s", name, exc)

    # Vet first, then spawn the admitted set CONCURRENTLY. Vetting is cheap and
    # order-dependent bookkeeping; spawning is the slow part (each child is polled
    # for a grace window), so serializing it made boot latency scale linearly with
    # the number of installed apps.
    admitted: list[str] = []
    for app_info in apps:
        if not app_info.get("enabled"):
            continue
        name = app_info.get("name", "")
        # Governance: the ``apps`` allowlist is an activation ceiling, so it must
        # gate startup re-activation too — not just the manual enable transition.
        # A policy tightened AFTER an app was enabled would otherwise let the app
        # load on the next restart (its persisted enabled=true bypasses the
        # enable_app gate). Re-vet here so a now-forbidden app stays down.
        gov_denied = _app_activation_denied(name)
        if gov_denied:
            logger.warning("App %s not started: blocked by governance policy: %s", name, gov_denied)
            continue
        manifest = app_info.get("manifest", {})
        if not manifest.get("backend", {}).get("entryPoint"):
            continue
        # Re-vet admission at boot: an app enabled before a policy tightened
        # (banned / allowlist-removed / now-unsigned) must NOT keep running
        # across restarts. Builtins (origin == "builtin") are trusted first-party
        # code shipped unsigned, so they are exempt (same carve-out as enable_app)
        # — otherwise a require_signature policy would strand every core app.
        if app_info.get("origin") != "builtin":
            try:
                denied = app_admission_denied(
                    name, manifest=get_app_manifest(name), action="boot"
                )
            except Exception as exc:  # noqa: BLE001 — boot must never crash on re-vet
                # Fail CLOSED: if the re-vet itself errors (transient I/O, a bug
                # in the admission logic), treat the app as denied rather than
                # booting it unchecked. The loop still continues to the next app,
                # so a single failure never crashes boot — it just declines to
                # start the app whose admission we could not confirm.
                logger.error(
                    "Boot admission re-vet failed for app %s: %s — treating as denied "
                    "(fail-closed)",
                    name, exc,
                )
                denied = f"admission re-vet error: {exc}"
            if denied:
                logger.warning(
                    "Boot: skipping enabled app %s — blocked by admission policy: %s",
                    name, denied,
                )
                try:
                    sel().log_api_access(
                        caller="gateway", operation="app_backend_boot",
                        outcome="denied", resources=name, error=denied,
                    )
                except Exception as exc:
                    logger.debug("SEL audit failed for app %s boot deny: %s", name, exc)
                continue
        admitted.append(name)

    return _start_backends_concurrently(admitted)


def _preclaim_fixed_ports(names: list[str]) -> None:
    """Reserve every declared fixed port before concurrent spawns are submitted.

    Best-effort and non-fatal: an unreadable manifest or an out-of-range/duplicate
    port is simply left to the spawn itself, which already validates and reports
    it. This only removes the ordering hazard; it never decides whether an app may
    start.
    """

    for name in names:
        try:
            manifest = get_app_manifest(name)
            if manifest is None:
                continue
            port_str = str(manifest.backend.port)
            if not port_str or port_str == "auto":
                continue
            port = int(port_str)
        except (AttributeError, TypeError, ValueError):
            continue
        if not (_MIN_PORT <= port <= _MAX_PORT):
            continue
        try:
            _claim_port(name, port)
        except PortUnavailableError as exc:
            # Two apps declaring the same fixed port: a real conflict the spawn
            # path reports per app. Log once here for the boot-time picture.
            logger.warning("Boot: fixed-port pre-claim for app %s skipped: %s", name, exc)


def _start_backends_concurrently(names: list[str]) -> list[str]:
    """Spawn the given app backends in parallel; return those that started.

    Each app's spawn blocks on a survival grace window, so starting them one at a
    time made boot cost roughly N x that window. They are independent (ports are
    reserved atomically — see ``_reserve_free_port``), so they run concurrently and
    boot costs about ONE window regardless of app count.

    Declared FIXED ports are reserved up front, before any spawn is submitted.
    A fixed port is a requirement, not a preference, so it must not be lost to an
    auto-port app that merely happened to select it first — pre-claiming removes
    that race entirely, leaving `PortUnavailableError` to signal only a genuine
    conflict (two apps declaring the same port, or a foreign holder).

    Failure isolation matches the previous serial loop exactly: one app's spawn
    raising or returning None must never take down the gateway (Slack + dashboard
    + every session) or affect the other apps.
    """

    if not names:
        return []

    _preclaim_fixed_ports(names)

    started: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(len(names), _BOOT_SPAWN_MAX_WORKERS),
        thread_name_prefix="app-boot",
    ) as pool:
        futures = {pool.submit(start_app_backend, name): name for name in names}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                ap = future.result()
            except Exception as exc:  # noqa: BLE001 — boot must never crash on one app
                # A per-app spawn failure (e.g. sandbox.wrap_argv fail-closing when
                # no OS-level sandbox backend is available — macOS 26 removed
                # sandbox-exec) must NOT take down the whole gateway. Log, audit,
                # and skip this app — same fail-isolated posture as the admission
                # re-vet and MCP reconcile branches above.
                logger.error(
                    "Boot: failed to start backend for app %s: %s — skipping "
                    "(gateway continues)",
                    name, exc,
                )
                try:
                    sel().log_api_access(
                        caller="gateway", operation="app_backend_boot",
                        outcome="error", resources=name, error=str(exc),
                    )
                except Exception as sel_exc:
                    logger.debug("SEL audit failed for app %s boot error: %s", name, sel_exc)
                continue
            if ap:
                started.append(name)
                logger.info("Auto-started backend for app %s on port %d", name, ap.port)
                # MCP re-registration is HEALTH-GATED: the health-check loop started
                # by start_app_backend calls _gate_mcp_registration once /health
                # passes, writing the HTTP MCP url with the real allocated port
                # (which may differ from the manifest's illustrative port).
                # Registering here — before health — is exactly what could leave a
                # dead url for an enabled-but-never-healthy app and break every
                # kiro-cli session. EXCEPTION: an adopted already-healthy instance
                # runs no health loop, so register it synchronously now.
                if ap.healthy:
                    _gate_mcp_registration(name, ap.port, healthy=True)
    return started
