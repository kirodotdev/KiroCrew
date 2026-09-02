"""App backend process management — spawn, health check, stop, and proxy config.

When an app declares a ``backend`` section in its manifest, KiroCrew manages
the backend process lifecycle: spawn on enable, health-check, stop on disable.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import http.client
import json
import logging
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.apps import deps_boot as _deps_boot_module
from kiro_crew.apps.admission import app_admission_denied
from kiro_crew.apps.execution import (
    app_execution_denied,
    shipped_builtin_app_root,
    shipped_builtin_module_path,
)
from kiro_crew.apps.interpreter import app_deps_dir, path_command_is_abi_matched, resolve_app_python
from kiro_crew.apps.manager import app_dir, get_app_manifest, list_apps
from kiro_crew.apps.registry import minimal_env
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.sandbox import (
    RLIMIT_PROFILE_BUILD,
    RLIMIT_PROFILE_TOOL,
    cgroup_scope_argv,
    popen_limited,
    run_limited,
    wrap_argv,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.subprocess_utf8 import UTF8_TEXT

try:  # optional dependency: the digest has a platform-module fallback
    from packaging.markers import default_environment as _default_marker_environment
except Exception:  # pragma: no cover - packaging ships with pip but is not guaranteed
    _default_marker_environment = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_MIN_PORT = 9100
_MAX_PORT = 9200
_HEALTH_CHECK_TIMEOUT = 5
_HEALTH_CHECK_RETRIES = 15
_HEALTH_CHECK_INTERVAL = 2.0
# ``healthCheck`` is app-authored text. A leading slash terminates the URL authority;
# the remaining class is the ordinary RFC 3986 path/query set with characters that
# can be parsed inconsistently (userinfo, fragments, backslashes, brackets) excluded.
_HEALTH_PATH_RE = re.compile(r"\A/[A-Za-z0-9._~!$&'()*+,;=:/?%-]*\Z")
_warned_health_paths: set[str] = set()
_health_warn_lock = threading.Lock()

# Post-startup liveness watch (see _watch_backend_health). The startup poll above only
# establishes that a backend CAME UP; without a standing watch `healthy` would be a
# write-once cache and a backend that died later would keep the reverse proxy routing to
# a dead port. The interval is far coarser than the startup one because it runs for the
# whole life of every backend, and nothing is waiting on it — a demotion that lands one
# interval late costs a few refused requests, whereas a tight poll costs one HTTP round
# trip per backend forever.
_HEALTH_WATCH_INTERVAL = 15.0
# Consecutive failed probes of a process that is still ALIVE before it is demoted. A
# backend can be briefly busy (a slow request, a GC pause), so demoting on a single miss
# would take a working app offline; an exited process needs no threshold at all because
# it cannot recover. See _watch_backend_health.
_HEALTH_WATCH_FAILURES = 3
# Serializes health-driven MCP reconciliation (see _set_backend_health). Deliberately
# NOT `_lock`: the reconcile does manifest + config file I/O, and holding `_lock` across
# it would block the reverse proxy's get_app_backend_port on every request and risk a
# deadlock through the bridges <-> backend import cycle.
# RE-ENTRANT: the health path acquires this and then calls into bridges, whose MCP and
# agent writers acquire it too (see health_reconcile_lock). A plain Lock would deadlock
# on that re-entry, and dropping it from either side would leave the two families of
# writer unordered again.
_health_reconcile_lock = threading.RLock()

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

#: Said once per process: a centrally governed host with no OS confinement (see the spawn).
_warned_unconfined_cache = False

# Startup stale-reap timing (see _reap_stale_app_backends). The SIGTERM grace is
# applied PER orphan, not shared across the batch.
_REAP_SIGTERM_GRACE = 3.0  # seconds to wait for an orphan to exit after SIGTERM
_REAP_POLL_INTERVAL = 0.1  # liveness re-poll cadence during the grace window


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


def _probe_adoption_health(port: int, health_path: str) -> bool:
    """Whether an already-running backend answers its health check."""

    return _health_probe(port, health_path, timeout=3)


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
            app_name, port,
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
            app_name, sorted(set(owners) - set(start_times)), port,
        )
        return None
    if not _probe_adoption_health(port, health_path):
        logger.warning(
            "App %s: backend on port %d stopped answering its health check "
            "while ownership was being recorded — skipping adoption",
            app_name, port,
        )
        return None
    owners_recheck = platform_compat.loopback_owner_pids(platform_compat.find_port_listeners(port))
    if set(owners_recheck) != set(owners):
        logger.warning(
            "App %s: port %d owners changed while ownership was being recorded "
            "(%s -> %s) — skipping adoption",
            app_name, port, owners, owners_recheck,
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
    # The `healthy` value last SUCCESSFULLY reconciled into mcp.json, or None if nothing
    # has been written for this record yet. Distinct from `healthy` because the flag
    # moves even when the mcp.json write fails; the gap between them is what the watch
    # retries. Deliberately absent from to_dict(): internal bookkeeping, not API.
    mcp_healthy: bool | None = None
    started_at: float = 0.0
    log_path: str = ""
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
# Apps whose backends spawn real build workloads (vite/pip) and need the
# elevated-but-finite NOFILE ceiling as the workload's ANCESTOR. Every other
# app backend keeps the standard (operator-configurable) resource policy.
_BUILD_CAPABLE_APPS = frozenset({"dev-fleet"})

# requirements.txt provisioning (pip --target into apps/interpreter.app_deps_dir).
# The stamp records the digest a successful install came from (requirements
# bytes + the installing interpreter's ABI tag — see _deps_digest), so a start
# where neither changed skips pip entirely. Staging/prior are transient swap
# directories: pip fills staging, success renames it live, and prior briefly
# holds the outgoing install so a failure at any point leaves either the old
# tree or the new one — never a half-replaced mix.
_DEPS_STAMP_NAME = ".requirements-sha256"
_DEPS_STAGING_NAME = ".kirocrew-deps-staging"


def _deps_staging_re() -> "re.Pattern[str]":
    """STRICT pattern for gateway-generated staging names.

    Single source of truth lives in manager (deferred import: manager does
    not import backend, but keeping one regex means the install-copy skip,
    the uninstall quarantine and this provisioning sweep can never drift
    apart again — a bare prefix match here deleted app-owned same-prefix
    entries).
    """
    from kiro_crew.apps.manager import _DEPS_STAGING_SWEEP_RE

    return _DEPS_STAGING_SWEEP_RE


#: Read caps for app-controlled provisioning inputs: the gateway buffers
#: these in ITS OWN memory, so an oversized requirements.txt or stamp file
#: (or a build hook flooding stderr) must exhaust a bounded buffer, not the
#: gateway. 1 MiB is orders of magnitude beyond any real requirements.txt.
_DEPS_REQ_MAX_BYTES = 1024 * 1024
_DEPS_STAMP_MAX_BYTES = 4096
_DEPS_PIP_STDERR_TAIL = 16 * 1024
_DEPS_PRIOR_NAME = ".kirocrew-deps-prior"


def _requirements_volatile(requirements: bytes) -> bool:
    """True when the stamp digest cannot prove the resolved set unchanged.

    The digest covers the top-level requirements.txt bytes only, so any line
    whose RESOLUTION can change while the line itself does not defeats the
    stamp: file references (``-r``/``-c``, attached or spaced), editables,
    local paths, VCS and URL requirements, and ``name @ url`` direct
    references. For these the caller disables stamp reuse entirely
    (reprovision every start) rather than re-implementing pip's requirements
    grammar here — over-matching a rare exotic line costs one redundant pip
    run, under-matching serves stale dependencies.
    """
    for raw in requirements.splitlines():
        line = raw.strip()
        if not line or line.startswith(b"#"):
            continue
        if line.startswith(
            (
                b"-r",
                b"-c",
                b"-e",
                b"-f",
                b"--requirement",
                b"--constraint",
                b"--editable",
                b"--find-links",
                b"--no-index",
                b"--index-url",
                b"--extra-index-url",
            )
        ):
            # File/constraint references, editables, and RESOLUTION-LOCATION
            # options: an unchanged `--find-links wheelhouse` line resolves
            # against local wheels whose CONTENT can change — the stamp
            # cannot prove the installed set unchanged for any of these.
            return True
        if b"://" in line or re.search(rb"\s@\s", line):
            return True
        if line.startswith((b".", b"/", b"~")) or re.match(rb"[A-Za-z]:[\\/]", line):
            return True
        # A BARE relative path (wheels/pkg.whl) is a local artifact whose
        # content can change under an unchanged line — any non-option line
        # carrying a path separator is volatile. Over-matching an exotic
        # marker expression costs one redundant pip run; under-matching
        # serves a stale local wheel.
        if b"/" in line or b"\\" in line:
            return True
        # A bare ARCHIVE filename (vendor.whl — no separator at all) is
        # still a local artifact: pip resolves it against the cwd (the app
        # root), and its content can change under an unchanged line.
        if line.lower().endswith(
            (b".whl", b".zip", b".tar.gz", b".tgz", b".tar.bz2", b".tar.xz", b".tar")
        ):
            return True
    return False


def _deps_tree_stamp_current(root: Path, req_file: Path) -> bool:
    """True when the provisioned tree's stamp names the digest for the
    CURRENT interpreter and the CURRENT requirements bytes.

    The activation gate, not the provisioning gate: after a Python upgrade a
    reprovision is attempted, but if it FAILS the stale tree (wheels built
    for the old ABI) is still on disk - injecting it via PYTHONPATH crashes
    the backend at import. The stamp digest folds the interpreter's cache
    tag, platform and full version, so an old-ABI tree can never present a
    matching stamp. Reads are bounded and no-follow, mirroring the
    provisioning path; every failure reads as "not current" (no activation -
    safe direction: the backend runs without the deps and surfaces the
    provisioning error, instead of crashing on foreign wheels).
    """
    try:
        # Same reader shape as provisioning: resolve, containment-check
        # against the app root, then a component-pinned no-follow open. A
        # SUPPORTED in-tree symlink (which provisioning accepts) must also
        # activate - a direct O_NOFOLLOW open on the link name would refuse
        # it and strand a successfully provisioned app without its deps.
        root_resolved = root.resolve(strict=True)
        open_target = req_file.resolve(strict=True)
        if root_resolved != open_target and root_resolved not in open_target.parents:
            return False
        rfd = _open_contained_nofollow(root_resolved, open_target)
        with os.fdopen(rfd, "rb") as rfh:
            if not stat.S_ISREG(os.fstat(rfh.fileno()).st_mode):
                return False
            req_bytes = rfh.read(_DEPS_REQ_MAX_BYTES + 1)
        if len(req_bytes) > _DEPS_REQ_MAX_BYTES:
            return False
        digest = _deps_digest(req_bytes)

        def _read_marker(name: str) -> str | None:
            marker = app_deps_dir(root) / name
            if platform_compat.is_link_or_junction(marker):
                return None
            try:
                mfd = os.open(str(marker), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except OSError:
                return None
            with os.fdopen(mfd, "rb") as mfh:
                if not stat.S_ISREG(os.fstat(mfh.fileno()).st_mode):
                    return None
                return mfh.read(_DEPS_STAMP_MAX_BYTES).decode("utf-8").strip()

        if digest:
            stamp_val = _read_marker(_DEPS_STAMP_NAME)
            if stamp_val is not None and stamp_val == digest:
                return True
        # Fall back to the ABI tag: a stamp that mismatches only because the
        # REQUIREMENTS (or their marker environment) changed still names a
        # tree of importable wheels - the last good install keeps serving
        # when a refresh fails (offline pip), exactly as it did before the
        # stamp gate. A missing or foreign-ABI tag never activates.
        return _read_marker(_DEPS_ABI_NAME) == _deps_abi_tag()
    except (OSError, UnicodeDecodeError):
        return False


_DEPS_ABI_NAME = ".abi-sha256"


def _deps_abi_tag() -> str:
    """ABI identity of the CURRENT interpreter, independent of requirements.

    What makes ``pip --target`` wheels importable or not: the implementation
    cache tag (``cpython-312``) and the build platform. Recorded beside the
    full stamp so activation can tell "stale REQUIREMENTS on the right ABI"
    (the prior tree still serves - a failed refresh must not strand the
    backend without its last good install) from "wrong ABI" (never inject).
    """
    tag = sys.implementation.cache_tag or ""
    plat = sysconfig.get_platform()
    return hashlib.sha256(f"{tag}\n{plat}\n".encode()).hexdigest()


def _deps_digest(requirements: bytes) -> str:
    """Stamp digest for a provisioned deps dir.

    Folds the installing interpreter's cache tag (e.g. ``cpython-312``), the
    platform tag (e.g. ``macosx-11.0-arm64``), AND the full interpreter
    version in with the requirements bytes: wheels installed by
    ``pip --target`` are ABI- and architecture-specific, and a
    requirements.txt can carry ``python_full_version`` environment markers
    that flip on a PATCH upgrade — so after a gateway Python upgrade of any
    granularity, or a cross-architecture home migration, an UNCHANGED
    requirements.txt must still reprovision. A requirements-only stamp would
    skip pip and leave a stale or incompatible install live.

    Scope: the digest covers the top-level requirements.txt bytes only.
    The stamp-skip caller compensates: a requirements.txt that references
    other files (``-r``/``-c``) disables the skip entirely, so a change
    confined to an included file can never be masked by a matching stamp.
    """
    tag = sys.implementation.cache_tag or ""
    plat = sysconfig.get_platform()
    pyver = platform.python_version()
    # The FULL PEP 508 marker environment, not just the interpreter tuple:
    # a requirement conditioned on platform_release / platform_version /
    # implementation details flips on an OS update while the requirements
    # bytes (and interpreter) stay identical — the stamp must not prove
    # such a set unchanged. Sorted key=value lines make the digest stable.
    if _default_marker_environment is not None:
        marker_env = "\n".join(
            f"{k}={v}" for k, v in sorted(_default_marker_environment().items())
        )
    else:  # packaging unavailable: fall back to the platform module
        marker_env = "\n".join(
            (
                f"platform_release={platform.release()}",
                f"platform_version={platform.version()}",
                f"platform_machine={platform.machine()}",
                f"platform_system={platform.system()}",
                f"implementation_name={sys.implementation.name}",
            )
        )
    return hashlib.sha256(
        f"{tag}\n{plat}\n{pyver}\n{marker_env}\n".encode() + requirements
    ).hexdigest()


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
            capture_output=True,
            timeout=10,
            **UTF8_TEXT,
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
                logger.info(
                    "App %s backend already adopted (pids %s)", app_name, existing.adopted_pids
                )
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
    # Held across the whole body: the backend exists as a PROCESS before its
    # pidfile record does, and a CLI uninstall probing in that window reads
    # "no record" as "no backend". Under this cross-process lock the probe
    # waits until the record is persisted (or the spawn torn down) - see
    # app_backend_lifecycle_flock.
    try:
        with app_backend_lifecycle_flock(app_name):
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
        if cur is not None and not getattr(cur, "starting", False):
            return cur  # resolved to a real process at the deadline
        if cur is not None and getattr(cur, "starting", False):
            if not owner_gone:
                logger.info(
                    "App %s backend spawn still in flight past the wait window "
                    "(provisioning?) — leaving the placeholder in place",
                    app_name,
                )
                return None
            _processes.pop(app_name, None)
            logger.warning("App %s backend spawn timed out — cleared stale placeholder", app_name)
        return None


def _deps_boot_path() -> Path:
    """Absolute path of the stdlib-only launch shim (see apps.deps_boot)."""
    return Path(os.path.abspath(_deps_boot_module.__file__))


def _open_contained_nofollow(base: Path, target: Path) -> int:
    """Open ``target`` under ``base`` with every component no-follow.

    A thin consumer of :mod:`kiro_crew.pinned_fs` (see its module docstring
    for why per-site pinning is banned): the parent chain is pinned one
    openat per component and the final name is opened O_NOFOLLOW through
    it, so neither an ancestor swap nor a final-component link can escape
    the app root. Where the platform cannot pin
    (``supports_pinned_walk()`` is False — Windows), the fallback is a
    single O_NOFOLLOW-less open behind the caller's is_symlink pre-check,
    backed by symlink creation being privileged there; junction swaps of
    the DATA dir are separately caught by _PinnedDir.verify.
    """
    rel_parts = target.relative_to(base).parts
    if not rel_parts:
        raise OSError("requirements path resolves to the app root itself")
    if not pinned_fs.supports_pinned_walk():
        return os.open(str(target), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    return pinned_fs.open_in_pinned_parent(
        str(target.parent),
        rel_parts[-1],
        flags=os.O_RDONLY | os.O_NOFOLLOW,
        mode=0o644,
        what="app requirements file",
        refusal=OSError,
    )


class _PinnedDir:
    """Pin the app data dir against link swaps for one provision transaction.

    A path-based check-then-use is a TOCTOU window: a RUNNING app can swap
    ``data/`` for a symlink after the validation and have every later rename
    or delete land in another app's tree. On POSIX the directory is opened
    O_NOFOLLOW|O_DIRECTORY and HELD: renames go through ``dir_fd`` (they are
    the operations with delete/replace power over a victim's live tree), and
    the path-based steps that cannot take a dir_fd (rmtree, mkdir, pip's
    ``--target``, the stamp write) are each preceded by :meth:`verify`, which
    re-checks that the path still names the pinned inode. On Windows there
    is no O_NOFOLLOW or dir_fd; the caller's is_link_or_junction pre-check
    stands, backed by symlink creation being privileged there.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None
        self._win_id: tuple[int, int] | None = None
        if pinned_fs.supports_pinned_walk():
            # pin_parent walks every component openat+O_NOFOLLOW (see
            # kiro_crew.pinned_fs for the invariants); a link anywhere on
            # the way — or at the target — is refused, not followed.
            self.fd = pinned_fs.pin_parent(
                str(path), what="app data directory", refusal=OSError
            )
        else:
            # Windows: capture the directory identity (volume serial + file
            # index via st_dev/st_ino) so verify() can detect a junction
            # swapped in mid-transaction — junction creation needs no
            # privilege, so the pre-check alone is a TOCTOU window there.
            if platform_compat.is_link_or_junction(path):
                raise OSError("app data directory is a symlink/junction; refusing")
            st = os.stat(str(path))
            self._win_id = (st.st_dev, st.st_ino)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def verify(self) -> None:
        """Refuse to proceed when the path no longer names the pinned dir.

        POSIX compares the held fd's identity against a fresh lstat. On
        Windows there is no held fd, but JUNCTION creation needs no
        privilege (unlike symlinks), so the swap threat is real there too:
        re-check that the path is still not a link/junction and still names
        the directory identity captured at pin time (st_dev/st_ino —
        Python's stat on Windows fills these from the volume serial and
        file index).
        """
        if self.fd is not None:
            st_fd = os.fstat(self.fd)
            st_path = os.lstat(str(self.path))
            if (st_fd.st_dev, st_fd.st_ino) != (st_path.st_dev, st_path.st_ino):
                raise OSError("app data directory was replaced mid-provisioning; refusing")
            return
        if platform_compat.is_link_or_junction(self.path):
            raise OSError("app data directory was replaced mid-provisioning; refusing")
        st_now = os.stat(str(self.path))
        if self._win_id is not None and (st_now.st_dev, st_now.st_ino) != self._win_id:
            raise OSError("app data directory was replaced mid-provisioning; refusing")

    def rename(self, src_name: str, dst_name: str) -> None:
        """Rename WITHIN the pinned dir, immune to a swapped path.

        POSIX renames are dir_fd-relative (cannot be redirected at all);
        on Windows the identity is revalidated immediately before the
        path-based rename, shrinking the swap window to the single rename
        syscall.
        """
        if self.fd is not None:
            os.rename(src_name, dst_name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        else:
            self.verify()
            os.rename(str(self.path / src_name), str(self.path / dst_name))

    def rename_out(self, src_name: str, dst: Path) -> None:
        """Move an entry OUT of the pinned dir to a path destination.

        The SOURCE side is the security boundary (it names an entry inside
        the app-writable pinned dir); it goes through the held fd on POSIX
        and an identity re-check on Windows. os.rename never follows the
        final component of the destination, so a link planted at the
        destination name is replaced, not traversed.
        """
        if self.fd is not None:
            os.rename(src_name, str(dst), src_dir_fd=self.fd)
        else:
            self.verify()
            os.rename(str(self.path / src_name), str(dst))


def _pinned_remove_entry(pin: "_PinnedDir", parent: Path, name: str) -> None:
    """Best-effort delete of ``parent/name`` without a path-follow window.

    A thin consumer of :func:`kiro_crew.pinned_fs.remove_tree_pinned`: the
    parent chain is re-pinned, the target opened through it, and the whole
    tree removed by descriptor — approval binds the opened directory to the
    inode this transaction just observed through its own pin, so a swap
    between observation and removal is refused, not followed. Links are
    unlinked via the held descriptor and never traversed. Best-effort by
    contract: every refusal outcome leaves the entry (or its staged rename)
    in place and the transaction continues.
    """
    if pin.fd is not None:
        st = pinned_fs.stat_at(pin.fd, name)
        if st is None:
            return
        if not stat.S_ISDIR(st.st_mode):
            try:
                os.unlink(name, dir_fd=pin.fd)
            except OSError:
                pass
            return
        expect = (st.st_dev, st.st_ino)

        def _approve(root_fd: int, _tree: pinned_fs.PinnedTree) -> str | None:
            opened = os.fstat(root_fd)
            if (opened.st_dev, opened.st_ino) != expect:
                return "identity changed since this transaction observed it"
            return None

        try:
            pinned_fs.remove_tree_pinned(
                str(parent / name),
                what="app generated dependency tree",
                approve=_approve,
                refusal=OSError,
            )
        except OSError:
            pass
        return
    # No pinned walk on this platform: identity re-check plus path delete,
    # behind the privileged-symlink argument (junction swaps of data/ are
    # caught by pin.verify's identity check).
    try:
        pin.verify()
    except OSError:
        return
    target = parent / name
    try:
        if platform_compat.is_link_or_junction(target):
            platform_compat.unlink_link_or_junction(target)
        elif target.exists():
            shutil.rmtree(str(target), ignore_errors=True)
    except OSError:
        pass


def provision_app_deps(app_name: str, root: Path) -> str:
    """Provision ``root/requirements.txt`` into the app's deps dir.

    The entire provision transaction (requirements read, interrupted-swap
    recovery, stamp check, pip into staging, live swap) runs under an
    exclusive per-app file lock: the backend spawn and a backend-less
    registration — or two concurrent registrations — would otherwise delete
    each other's staging tree mid-install and both fail. flock excludes
    across processes AND across threads (each caller opens its own
    descriptor), and the stamp check runs inside the lock, so a waiter that
    blocked behind a successful install skips pip on the stamp it left.
    """
    if not (root / "requirements.txt").is_file():
        # Nothing to provision: skip the pin and the lock entirely — the
        # transaction machinery must not create lock files (or take
        # platform-specific lock paths) for every app without declared
        # dependencies. The locked body re-checks under the lock, so this
        # is only a fast path, never the security boundary.
        return ""
    deps_parent = app_deps_dir(root).parent
    lock_path = deps_parent / ".kirocrew-deps.lock"
    provision_error = ""
    try:
        # The deps dir lives under app-writable data/, and every operation
        # below (lock file, staging, swap) would FOLLOW a link planted
        # there — an app pointing data/ at another app's tree would have
        # this provisioning swap attacker-chosen dependencies into the
        # victim's dir (the same shape the uninstall purge refuses in
        # manager.py). The gateway creates data/ as a real directory, so a
        # link is never legitimate: refuse before touching anything through
        # it.
        if platform_compat.is_link_or_junction(deps_parent):
            raise OSError("app data directory is a symlink/junction; refusing to provision")
        deps_parent.mkdir(parents=True, exist_ok=True)
        pin = _PinnedDir(deps_parent)
        try:
            # The lock file is opened through the PIN (dir_fd on POSIX), so
            # a link swapped in at data/ cannot redirect its creation; the
            # O_NOFOLLOW arm refuses a link planted at the lock name itself.
            # O_RDWR (not read-only): Windows msvcrt.locking requires write
            # access on the fd (same reason as bridges' _mcp_lock).
            lflags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            if pin.fd is not None:
                lfd = os.open(lock_path.name, lflags, 0o644, dir_fd=pin.fd)
            else:
                lfd = os.open(str(lock_path), lflags, 0o644)
            with os.fdopen(lfd, "r+") as lf:
                with platform_compat.file_lock(lf.fileno(), exclusive=True):
                    provision_error = _provision_app_deps_locked(app_name, root, pin)
        finally:
            pin.close()
    except OSError as exc:
        # file_lock fails CLOSED; an unserialized install could corrupt the
        # live deps tree, so surface the failure instead of proceeding.
        provision_error = (
            f"Failed to serialize dependency provisioning for app {app_name}: {exc}"
        )
        logger.error("%s", provision_error)
    if provision_error:
        # One SEL event per failed provisioning, whatever the arm (pip
        # failure, requirements-read refusal, lock failure).
        try:
            sel().log_api_access(
                caller="gateway",
                operation="app_backend_spawn",
                outcome="deps_provision_failed",
                resources=app_name,
            )
        except Exception as sel_exc:
            logger.debug("SEL audit failed for app %s deps failure: %s", app_name, sel_exc)
    return provision_error


def _provision_app_deps_locked(app_name: str, root: Path, pin: _PinnedDir) -> str:
    """The provision transaction body — caller holds the per-app deps lock.

    Shared by the backend spawn and by backend-less stdio registration (an
    app can ship only MCP servers — with no backend start, nothing else ever
    runs pip, and the shim/PYTHONPATH transports would reference a forever-
    empty tree). Stamp-gated, so repeat calls with unchanged requirements do
    no network work. Returns an error message ('' when provisioning
    succeeded or was skipped). CALLERS gate trust: a module-style builtin
    executes trusted code from inside the kiro_crew package, and provisioning
    an app-dir requirements.txt for it would let agent-authored wheels load
    ahead of the trusted module — so this is only ever called for apps whose
    code runs from the writable app dir itself.
    """
    # Install Python dependencies into a per-app deps dir (isolated from the
    # Kiro Crew runtime). `pip install --target` rather than a venv: packaged
    # installs bundle an interpreter that ships pip but no ensurepip, so
    # `-m venv` dies after creating the directory skeleton — and the venv-first
    # interpreter policy would then prefer that skeleton while it holds none of
    # the app's dependencies. A --target install needs no bootstrap and works
    # identically under packaged and source installs; the deps dir reaches the
    # child via PYTHONPATH (set where the spawn env is built below).
    # sys.executable, never a bare "python3": the bare name relies on PATH
    # (absent on some hosts, a Store stub on Windows) — the same policy every
    # app spawn path applies via apps/interpreter.
    #
    # The install is stamp-gated and staged:
    # - A hash of requirements.txt is stamped into the deps dir on success, and
    #   a matching stamp skips pip entirely — so a restart with unchanged
    #   requirements does no network work and an OFFLINE restart of a healthy
    #   backend raises no alarm (pip --target cannot answer "already
    #   satisfied" the way a venv install could).
    # - pip installs into a staging dir that is swapped in only on success, so
    #   an interrupted or failed (re)install can never corrupt the live deps
    #   dir in place — the prior good install keeps serving the spawn below.
    req_file = root / "requirements.txt"
    provision_error = ""
    req_bytes: bytes | None = None
    if req_file.is_file():
        # The app dir is app-writable, so requirements.txt can be a planted
        # symlink — and a resolve-then-read pair would be a TOCTOU window a
        # concurrent writer could race (validate a real file, swap in a
        # symlink, gateway reads protected bytes and stamps their digest).
        # The open is O_NOFOLLOW-bound: for a regular file the kernel refuses
        # any link swapped in before the open, and the fstat regular-file
        # check runs on the very handle the bytes come from. A LINK at
        # requirements.txt is legitimate app layout when it stays in-tree
        # (requirements.txt -> requirements/prod.txt), so a link is accepted
        # ONLY when its strict resolution stays inside the app root — then
        # the RESOLVED path is opened, itself O_NOFOLLOW-bound. Every race
        # collapses to a refusal or to reading a different in-root file
        # (app-controlled either way: no out-of-root bytes can ever be read
        # or digested). On Windows os.O_NOFOLLOW is absent; the is_symlink
        # pre-check substitutes (symlink creation is privileged there).
        try:
            root_resolved = root.resolve(strict=True)
            open_target = req_file.resolve(strict=True)
            if not open_target.is_relative_to(root_resolved):
                raise OSError("requirements.txt resolves outside the app root")
            # Descriptor-relative, every-component-no-follow open: the
            # containment check above is only a fast refusal — an ancestor
            # of the resolved path could be swapped for a link between the
            # check and the open, so the traversal itself is pinned
            # component by component (see _open_contained_nofollow).
            fd = _open_contained_nofollow(root_resolved, open_target)
            with os.fdopen(fd, "rb") as fh:
                if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    raise OSError("requirements.txt is not a regular file")
                # Bounded read: this buffer lives in the GATEWAY's memory
                # and the file is app-controlled — cap it instead of letting
                # a giant file take the gateway down.
                req_bytes = fh.read(_DEPS_REQ_MAX_BYTES + 1)
                if len(req_bytes) > _DEPS_REQ_MAX_BYTES:
                    raise OSError("requirements.txt exceeds the size cap")
        except OSError:
            req_bytes = None
        if req_bytes is None:
            provision_error = (
                f"Refusing requirements.txt for app {app_name}: it is a "
                f"symlink escaping the app directory, not a regular file, or "
                f"unreadable (out-of-root symlinked requirements are not "
                f"installed)"
            )
            logger.error("%s", provision_error)
    if req_bytes is not None:
        deps_dir = app_deps_dir(root)
        prior = deps_dir.parent / _DEPS_PRIOR_NAME
        # Recover from an interrupted swap: a crash between the two renames
        # below leaves only the outgoing tree under the prior name. Put it
        # back before the stamp check, so an offline restart still has its
        # last good install (and a matching stamp skips pip entirely).
        if not deps_dir.exists() and prior.exists():
            try:
                pin.rename(prior.name, deps_dir.name)
            except OSError as exc:
                logger.warning("App %s: could not recover interrupted deps swap: %s", app_name, exc)
        stamp = deps_dir / _DEPS_STAMP_NAME
        digest = _deps_digest(req_bytes)
        # The digest covers the top-level file's bytes only: any requirement
        # whose RESOLUTION can change while its line does not (file
        # references, local paths, VCS/URL and direct references) defeats the
        # stamp, so those disable the skip — reprovision on every start
        # (correct, just slower) instead of silently serving a stale install.
        volatile = _requirements_volatile(req_bytes)
        # The stamp lives in the app-writable tree too, so its read is
        # no-follow-bound exactly like the requirements read above: a
        # planted symlink at the stamp name must not make the gateway read
        # an arbitrary path. Any open/read/decode failure reads as
        # "unprovisioned" (pip runs — safe direction).
        provisioned = False
        if bool(digest) and not volatile:
            try:
                # On Windows os.O_NOFOLLOW is absent; the is_link pre-check
                # substitutes (same pattern as the requirements read) — a
                # planted stamp link must read as "unprovisioned", not
                # through to an arbitrary file.
                if platform_compat.is_link_or_junction(stamp):
                    raise OSError("stamp is a symlink/junction")
                sfd = os.open(str(stamp), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(sfd, "rb") as sfh:
                    if stat.S_ISREG(os.fstat(sfh.fileno()).st_mode):
                        # Bounded read: a real stamp is one digest line; an
                        # oversized file reads its head, fails the compare,
                        # and safely reprovisions.
                        provisioned = (
                            sfh.read(_DEPS_STAMP_MAX_BYTES).decode("utf-8").strip() == digest
                        )
            except (OSError, UnicodeDecodeError):
                provisioned = False
        if not provisioned:
            # UNIQUE staging name per transaction, created through the PIN:
            # a fixed name plus path-based mkdir was the last re-pointable
            # step — a data/ swap after verification would have pip fill (or
            # a cleanup delete) another app's staging. The dir_fd mkdir
            # cannot be redirected; the fresh name means no pre-existing
            # tree to delete through a path; and pip receives the path only
            # after a final verify, with the flock guaranteeing no sibling
            # transaction races the window.
            staging = deps_dir.parent / f"{_DEPS_STAGING_NAME}-{os.getpid()}-{os.urandom(4).hex()}"
            _env = minimal_env()  # don't leak secrets to pip subprocesses
            try:
                # Stale staging trees from crashed transactions (the old
                # fixed name or unique names another pid left) are swept
                # best-effort AFTER a verify; pip --target does not replace
                # a distribution already present, so installs never reuse a
                # stale tree — the fresh unique name guarantees that
                # structurally instead of by strict pre-delete.
                pin.verify()  # path-based steps below cannot take a dir_fd
                # Stale-staging sweep, DESCRIPTOR-relative: a path glob plus
                # path rmtree could follow a data/ swapped in after the
                # verify. Enumerate through the held fd, quarantine each
                # match to a fresh random name via dir_fd rename (cannot be
                # redirected), then delete by path — the random name cannot
                # pre-exist in a victim tree the attacker cannot write, so a
                # post-swap delete is a harmless ENOENT.
                if pin.fd is not None:
                    _stale_names = [
                        e
                        for e in os.listdir(pin.fd)
                        if _deps_staging_re().fullmatch(e) is not None
                        and e != staging.name
                    ]
                else:
                    _stale_names = [
                        p.name
                        for p in deps_dir.parent.glob(f"{_DEPS_STAGING_NAME}*")
                        if _deps_staging_re().fullmatch(p.name) is not None
                        if p.name != staging.name
                    ]
                for _stale in _stale_names:
                    _pinned_remove_entry(pin, deps_dir.parent, _stale)
                if pin.fd is not None:
                    os.mkdir(staging.name, 0o755, dir_fd=pin.fd)
                else:
                    staging.mkdir(parents=True, exist_ok=True)
                pin.verify()  # pip receives a PATH; last re-check before it runs
                # Stamp-vs-install atomicity: pip RE-OPENS the requirements
                # path, and a concurrent rewrite after the hash above would
                # install the replacement while stamping the ORIGINAL digest
                # — later starts then skip repair and serve the wrong deps.
                # When the stamp will be trusted (non-volatile), pip installs
                # from an immutable SNAPSHOT of the very bytes the digest
                # covers. Volatile requirements never take the stamp
                # shortcut, and only they can carry file references whose
                # resolution is relative to the requirements file — so they
                # keep reading the validated live path, where includes
                # resolve correctly, with no stamp to skew.
                req_src = open_target
                if not volatile:
                    req_src = staging / "._kirocrew-requirements.snapshot"
                    # The write is the GATEWAY's own and staging lives in
                    # app-writable data/: a staging dir swapped for a link
                    # after its dir_fd mkdir would have a path write land in
                    # an arbitrary same-user file. Open through the pinned
                    # parent chain (every component O_NOFOLLOW) with O_EXCL,
                    # so neither a swapped ancestor nor a planted entry at
                    # the snapshot name can redirect it. Windows keeps the
                    # verify+path write behind the junction identity check.
                    if pinned_fs.supports_pinned_walk():
                        _sfd = pinned_fs.open_in_pinned_parent(
                            str(staging),
                            req_src.name,
                            flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            mode=0o644,
                            what="requirements snapshot",
                            refusal=OSError,
                        )
                        with os.fdopen(_sfd, "wb") as _sfh:
                            _sfh.write(req_bytes)
                    else:
                        pin.verify()
                        req_src.write_bytes(req_bytes)
                # pip reads the VALIDATED open_target, not the manifest
                # name: for an in-tree symlinked requirements.txt a nested
                # include (`-r base.txt`) resolves relative to the
                # requirements FILE, so handing pip the symlink path would
                # resolve includes beside the LINK instead of its target.
                # The no-follow handle above already refused an out-of-root
                # requirements.txt before any bytes were hashed; pip's own
                # re-open is a follow-open, but by then provisioning is
                # committed to THIS app's tree and the digest was taken from
                # the validated handle. Include-bearing requirements never
                # take the stamp shortcut (_requirements_volatile), so they
                # reprovision on every start — a change confined to an
                # included file cannot be masked.
                pip_cmd, _ = wrap_argv(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--quiet",
                        "--disable-pip-version-check",
                        "--target",
                        str(staging),
                        "-r",
                        str(req_src),
                    ],
                    mode="standard",
                )
                pip_cmd = cgroup_scope_argv(pip_cmd)  # cgroup DoS ceiling
                # check=True: a non-zero pip exit IS a provisioning failure. It
                # must not be discarded — the backend would spawn without its
                # dependencies and die on an import error pointing at the app.
                # cwd=root: relative references (`-e ./lib`) resolve against
                # the app root, not whatever directory the gateway happens to
                # be running from.
                # Bounded capture: capture_output buffers the child's whole
                # stdout/stderr in the GATEWAY's memory, and a noisy build
                # hook can flood it. stderr goes to a temp FILE and only a
                # bounded TAIL is ever read back (attached as exc.stderr for
                # the redaction pipeline below); stdout is discarded.
                with tempfile.TemporaryFile() as _pipbuf:
                    try:
                        run_limited(
                            pip_cmd,
                            check=True,
                            stdout=subprocess.DEVNULL,
                            stderr=_pipbuf,
                            timeout=60,
                            env=_env,
                            cwd=str(root),
                        )
                    except subprocess.CalledProcessError as _pip_exc:
                        # stderr went to the file, so the exception carries
                        # none — attach the bounded tail (never clobber a
                        # stderr some other spawn shape already set).
                        if not getattr(_pip_exc, "stderr", None):
                            _pipbuf.seek(0, os.SEEK_END)
                            _sz = _pipbuf.tell()
                            _start = max(0, _sz - _DEPS_PIP_STDERR_TAIL)
                            _pipbuf.seek(_start)
                            _tail = _pipbuf.read()
                            if _start > 0:
                                # The first line is PARTIAL: the seek can
                                # sever a URL's scheme, and the downstream
                                # exfil/credential redaction anchors on
                                # https?:// - a scheme-less remainder would
                                # carry its query token straight into the
                                # logs. Drop through the first newline; a
                                # tail that is one giant line is dropped
                                # whole (never worth a credential).
                                _nl = _tail.find(b"\n")
                                _tail = (
                                    _tail[_nl + 1 :]
                                    if _nl != -1
                                    else b"[pip stderr tail elided: unterminated first line]\n"
                                )
                            _pip_exc.stderr = _tail
                        raise
                # Editable installs (`-e ./lib`) materialise as
                # __editable__*.pth hooks. They are RETAINED: python children
                # launch through the deps_boot shim, whose site.addsitedir
                # processes .pth files — the reason the refusal that used to
                # live here is gone. (Deps-provided python console scripts
                # route through the same shim via the shebang sniff in
                # bridges, so editables work there too.)
                if digest:
                    # atomic_write, not write_text: it writes a unique temp
                    # file and renames over the destination, so a stamp-named
                    # symlink a malicious sdist build hook planted inside
                    # staging is REPLACED rather than followed (write_text
                    # would write through it into an arbitrary same-user
                    # file). The pip child is sandboxed, but this write is the
                    # gateway's own.
                    atomic_write(staging / _DEPS_STAMP_NAME, digest)
                # ABI tag is written even for volatile requirements (digest
                # empty): activation uses it to keep serving the last good
                # tree when only the requirements resolution went stale, and
                # to refuse a wrong-ABI tree always. Same atomic_write
                # symlink-replacement rationale as the stamp above.
                atomic_write(staging / _DEPS_ABI_NAME, _deps_abi_tag())
                # Swap the fresh install live. Two renames, not an in-place
                # upgrade, so no state mixes old and new trees; the recovery
                # above (and the restore in the except arm) covers the window
                # in which only the prior name exists.
                pin.verify()
                _pinned_remove_entry(pin, deps_dir.parent, prior.name)
                if deps_dir.exists():
                    pin.rename(deps_dir.name, prior.name)
                pin.rename(staging.name, deps_dir.name)
                _pinned_remove_entry(pin, deps_dir.parent, prior.name)
            except Exception as exc:
                _pinned_remove_entry(pin, deps_dir.parent, staging.name)
                # If the failure hit between the swap renames (e.g. a locked
                # directory on Windows), the live name is empty and the good
                # tree sits under the prior name — put it back.
                if not deps_dir.exists() and prior.exists():
                    try:
                        pin.rename(prior.name, deps_dir.name)
                    except OSError as restore_exc:
                        logger.warning(
                            "App %s: could not restore prior deps after failed swap: %s",
                            app_name,
                            restore_exc,
                        )
                detail = str(exc)
                stderr = getattr(exc, "stderr", None)
                if stderr:
                    if isinstance(stderr, bytes):
                        stderr = stderr.decode("utf-8", "replace")
                    # Redact BEFORE truncating: a suffix cut can split a
                    # credential from the marker the redactor matches on,
                    # leaving the secret's tail to survive the pass below —
                    # the same split-across-a-length-cap shape the MCP report
                    # capture guards against. pip errors can echo an index
                    # URL carrying credentials
                    # (`--index-url https://user:token@host/`); this detail
                    # reaches the gateway log and the user-visible backend log
                    # (and /api/logs). Exfiltration-URL redaction runs FIRST:
                    # an agent-authored requirements path can embed a
                    # suspicious URL that pip echoes verbatim, and the
                    # credential/query passes below do not catch a bare
                    # exfil host.
                    stderr, _ = redact_exfiltration_urls(stderr.strip())
                    stderr, _ = redact_credentials(stderr)
                    # Same order rule for the query-strip below: applied to
                    # the FULL stderr before the tail cut, or the cut could
                    # split a URL from its query and leave the token's tail.
                    stderr = re.sub(r"(https?://[^\s?#]+)\?\S+", r"\1?<redacted-query>", stderr)
                    detail = f"{detail}: {stderr[-400:]}"
                detail, _ = redact_exfiltration_urls(detail)
                detail, _ = redact_credentials(detail)
                # redact_credentials catches user:pass@ URL forms; a failed
                # SIGNED or tokenized URL carries its secret in the QUERY
                # STRING (?X-Amz-Signature=..., ?token=...), which pip echoes
                # verbatim. Strip query strings from every URL in the detail
                # (covers URLs arriving via str(exc), not just stderr).
                detail = re.sub(r"(https?://[^\s?#]+)\?\S+", r"\1?<redacted-query>", detail)
                provision_error = (
                    f"Failed to install requirements.txt dependencies for app "
                    f"{app_name}: {detail}"
                )
                # The spawn is still attempted: the deps dir may hold a
                # previous successful install, and some requirements are
                # optional. The failure is surfaced instead of swallowed: an
                # error log here, and a header line in the backend's own log so
                # the import error missing deps produce points back at
                # provisioning.
                logger.error("%s", provision_error)
                # The deps_provision_failed SEL event is emitted by the
                # provision_app_deps wrapper for EVERY nonempty error — this
                # arm, the requirements-read refusal, and a lock failure —
                # so it is not duplicated here.
    return provision_error


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
            healthy = _probe_adoption_health(port, manifest.backend.healthCheck)

            if healthy:
                try:
                    sel().log_api_access(
                        caller="gateway", operation="app_backend_adopt",
                        outcome="adopted", resources=f"{app_name} port={port}",
                    )
                except Exception as exc:
                    logger.debug("SEL audit failed for app %s backend adopt: %s", app_name, exc)
                # Record owning PIDs at adoption time, scoped to the listener
                # the health probe actually reached. The probe above only ever
                # talks to 127.0.0.1:<port>; loopback_owner_pids mirrors the
                # kernel's most-specific-bind dispatch (exact 127.0.0.1 beats a
                # v4 wildcard, which beats a dual-stack v6 one), so a process
                # holding a different local address — or a v6-only wildcard
                # next to the real v4 owner — was never health-checked and is
                # not recorded. Each owner's start-time identity rides along so
                # stop can refuse a recycled PID, and the capture is sandwiched
                # between health checks so a responder that exits mid-capture
                # cannot hand ownership to a bystander.
                adopted = _capture_adopted_owners(app_name, port, manifest.backend.healthCheck)
                if adopted is None:
                    return None
                adopted_pids, adopted_start_times = adopted
                # Persist the adopted identities for CROSS-PROCESS stop: a CLI
                # uninstall consults only the pidfile, and an unrecorded
                # adopted backend would survive uninstall exactly like an
                # unrecorded spawned one. Same fail-loud rule as the spawn
                # site: what cannot be recorded must not be adopted (the next
                # start simply re-probes).
                if not _record_adopted_pids(app_name, port, adopted_pids, adopted_start_times):
                    logger.error(
                        "App %s: could not persist adopted-backend identities — refusing adoption",
                        app_name,
                    )
                    return None
                logger.info("App %s: healthy instance already on port %d — adopting (pids=%s)", app_name, port, adopted_pids)
                ap = AppProcess(
                    app_name=app_name, port=port, pid=0, proc=None,
                    healthy=True, started_at=time.time(), log_path=str(log_path),
                    adopted_pids=adopted_pids,
                    adopted_start_times=adopted_start_times,
                )
                # Adopted (externally-managed) backends are deliberately NOT
                # recorded for the startup stale-reap: the reap SIGTERMs a whole
                # process GROUP (safe only for our own start_new_session children),
                # whereas an external process's group may hold unrelated processes.
                # If the gateway dies, the external instance keeps running and is
                # simply re-probed and re-adopted on the next start — so reaping it
                # would kill a healthy service we would immediately re-adopt. stop's
                # adopted path kills only the re-validated PIDs for this reason.
                with _lock:
                    _processes[app_name] = ap
                    _allocated_ports[app_name] = port
                # Register through the SERIALIZED transition, before the watch is armed.
                # Registering afterwards — from a caller that returns and queues the work
                # — leaves a window in which the watch demotes and scrubs first and the
                # queued registration lands after it, restoring the dead url. Going
                # through _set_backend_health also records `mcp_healthy`, so the watch
                # can tell whether what it believes matches what is on disk.
                _set_backend_health(ap, healthy=True)
                _start_adopted_health_watch(ap, manifest.backend.healthCheck)
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

    req_file = root / "requirements.txt"
    # entry is None means a module-style builtin: it executes TRUSTED code
    # from inside the kiro_crew package, not from this writable app dir.
    # Provisioning a requirements.txt found here (or injecting a
    # .kirocrew-deps the agent could have written) would let agent-authored
    # wheels load ahead of the trusted module on its PYTHONPATH — a
    # trust-boundary crossing. Builtins declare their dependencies in the
    # package's own pyproject, so they never need this path; gate it (and
    # the PYTHONPATH/shim transports below) on a real file entry point.
    provision_error = ""
    if entry is not None:
        provision_error = provision_app_deps(app_name, root)

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
    if os.environ.get("KIROCREW_PROFILE"):
        # Forward the edition-profile override to the backend subprocess.
        # minimal_env() strips it otherwise, so a gateway launched with an
        # explicit KIROCREW_PROFILE (e.g. =standalone to override an installed
        # companion) would have the child re-resolve the profile from on-disk
        # markers and diverge from the parent. Since the backend now boots the
        # platform context at startup (fail-closed), that divergence would make
        # the subprocess refuse to start rather than fail lazily. Forwarding it
        # keeps the child on the SAME profile the gateway resolved. A profile
        # name, not a secret.
        _platform_extra["KIROCREW_PROFILE"] = os.environ["KIROCREW_PROFILE"]
    for _policy_env in ("KIROCREW_SECURITY_POLICY", "KIROCREW_ADMISSION_POLICY"):
        # Forward the governance trust-root path overrides alongside the profile.
        # These are the fleet operator's highest-priority policy sources
        # (governance.load_security_policy / admission), and minimal_env() strips
        # them. Now that the backend boots the platform context itself, dropping
        # them would make the child resolve its ceiling from the on-disk /
        # packaged default instead of the administrator-pinned policy — a looser
        # ceiling for governed app commands.
        #
        # Absolutize against THIS process's cwd before forwarding: the loaders
        # read the value as a bare Path() with no resolve()/expanduser(), and the
        # backend subprocess runs with a different cwd (the package root, set
        # below), so forwarding a RELATIVE override verbatim would make the child
        # look in the wrong directory and fail closed. Resolving here binds the
        # child to the exact file the gateway resolved. A path, not a secret.
        _policy_val = os.environ.get(_policy_env)
        if _policy_val:
            _platform_extra[_policy_env] = os.path.abspath(os.path.expanduser(_policy_val))
    # Imported here rather than at module scope: this module is on the app-serving
    # import path and the policy engine pulls the governance evaluator in behind it.
    from kiro_crew.platform.policy_distribution import (
        POLICY_CACHE_ONLY_ENV,
        POLICY_MAX_AGE_ENV,
    )
    from kiro_crew.platform.policy_distribution import cache_dir as policy_cache_dir
    from kiro_crew.platform.policy_distribution import (
        central_ceiling_installed,
        effective_max_cache_age,
    )

    # The backend boots its own platform context, and minimal_env() strips the
    # central-distribution settings — so on a fleet using that channel the child would
    # resolve its ceiling from the on-disk or packaged default instead of the
    # administrator's published document: the looser-ceiling failure the comment above
    # describes, for exactly the code that most needs a ceiling.
    #
    # It is put in CACHE-ONLY mode rather than handed the source. An app backend is
    # arbitrary third-party code, so giving it the fetch configuration would give it the
    # fleet's control plane: KIROCREW_POLICY_HEADERS is a live bearer token, and a
    # pre-signed KIROCREW_POLICY_URL is itself the credential. Neither is needed — the
    # gateway has already written the last-known-good cache, so the cache IS the
    # administrator's ceiling and the child adopts it with no URL, no token and no
    # network. The staleness bound is forwarded because it is the one setting that
    # decides whether that cached copy is still an acceptable answer.
    # Gated on whether the gateway's OWN ceiling came from that tier, not merely on
    # the variables being set. The child FAILS CLOSED on an absent cache, so the flag
    # must mean "there is a fleet ceiling to inherit" — a gateway that itself degraded
    # to a local tier has nothing to pass on, and flagging that child would refuse to
    # start an app on a host that is running perfectly well.
    if central_ceiling_installed():
        _platform_extra[POLICY_CACHE_ONLY_ENV] = "1"
        # The EFFECTIVE bound, not the env var: a fleet is just as likely to declare
        # max_cache_age_secs in the published document, and reading only the environment
        # would leave this child with no bound at all — accepting an arbitrarily stale
        # ceiling on a fleet that set one.
        _max_age = effective_max_cache_age()
        if _max_age:
            _platform_extra[POLICY_MAX_AGE_ENV] = str(_max_age)
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
    # Expose the provisioned deps dir (pip --target, above) to the child.
    # PYTHONPATH rather than an interpreter switch: it is honored identically
    # by the app's own venv interpreter and the gateway fallback, on every
    # platform. Prepended so the app's pinned requirements win over anything
    # the operator's own PYTHONPATH (passed through by minimal_env) carries.
    # Gated on the dir existing AND a real file entry point: a module-style
    # builtin (entry is None) runs trusted package code, and must not have an
    # agent-writable app dir injected ahead of it (same trust boundary as the
    # provisioning gate above).
    _deps_dir = app_deps_dir(root)
    # Activation additionally requires the stamp to name the digest for the
    # CURRENT interpreter: a failed reprovision after a Python upgrade leaves
    # the old-ABI tree on disk, and injecting it would crash the backend at
    # import (native wheels are ABI-specific).
    _deps_ready = (
        entry is not None
        and _deps_dir.is_dir()
        and req_file.is_file()
        and _deps_tree_stamp_current(root, req_file)
    )
    entry_str = str(entry) if entry else entry_point

    # Prefer explicit backend type from manifest over content sniffing
    backend_type = manifest.backend.type if manifest.backend else ""

    # --- Node.js backend ---
    # Note: module-style entry points (entry is None) are always Python
    # builtin apps and never declare a Node.js backend, so this branch is
    # safe to evaluate before the module-style branch below.
    if entry is not None and (
        backend_type == "node" or (not backend_type and entry_str.endswith((".js", ".mjs", ".cjs")))
    ):
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
                    sandboxed_npm = cgroup_scope_argv(sandboxed_npm)  # cgroup DoS ceiling
                    run_limited(
                        sandboxed_npm,
                        cwd=str(root), env=env, capture_output=True, timeout=120,
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
        # Prefer the app's venv interpreter, else the gateway's own (sys.executable) —
        # never a bare "python3": a bare name relies on PATH, which isn't guaranteed
        # (e.g. some build environments ship only a versioned interpreter, so
        # execvp("python3") raises FileNotFoundError and the backend dies immediately).
        # One policy shared with the stdio MCP registration path — see
        # kiro_crew.apps.interpreter.
        python_bin = resolve_app_python(root)
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
        # See the ASGI branch: venv python first, else the gateway's own interpreter —
        # one policy shared with the stdio MCP registration path.
        python_bin = resolve_app_python(root)
        cmd = [python_bin, entry_str]
        cwd = str(root)

    # Provisioned-deps launch shim: PYTHONPATH entries are not site dirs, so
    # .pth files in the deps tree (editable installs, namespace shims, import
    # hooks) would silently never be processed — packages that rely on them
    # install "successfully" and crash at import. Route the child through
    # deps_boot, which site.addsitedir()s the deps dir (processing .pth) and
    # then runs the original target with an unchanged argv view. Only when
    # the child runs the GATEWAY interpreter (deps pin sys.executable; a venv
    # interpreter means no deps were provisioned) — the shim is gateway code
    # and must not be imported by a foreign interpreter.
    #
    # Shim XOR PYTHONPATH, never both: `python -m kiro_crew.apps.deps_boot`
    # resolves kiro_crew through sys.path, and a deps-provided kiro_crew copy
    # on PYTHONPATH would SHADOW the gateway's shim — app code running as the
    # "shim" on the gateway's own interpreter. A shimmed child therefore gets
    # NO deps PYTHONPATH (addsitedir supplies the deps only after the trusted
    # shim has imported); non-shimmable children (node entries — inert there,
    # and non-gateway interpreters) keep the PYTHONPATH transport.
    if _deps_ready and cmd and cmd[0] == sys.executable:
        # By ABSOLUTE PATH, not -m: the child runs with cwd=app root, and
        # an app-root kiro_crew.py (or kiro_crew/ dir) would shadow the
        # gateway package for `-m` resolution — the backend would die (or
        # run app code as the shim) before startup. deps_boot is
        # stdlib-only, so the path spelling has no import to shadow.
        cmd = [sys.executable, str(_deps_boot_path()), str(_deps_dir), *cmd[1:]]
    elif _deps_ready and path_command_is_abi_matched(root, cmd[0] if cmd else ""):
        # PYTHONPATH transport only on a POSITIVE ABI match: the deps tree
        # is built by the GATEWAY's pip, and an exec backend running a PATH
        # python of another minor version would import mismatched binary
        # wheels and die. Anything not positively matched (foreign pythons,
        # node, shell) gets no deps env at all — the pre-deps status quo.
        _existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{_deps_dir}{os.pathsep}{_existing_pp}" if _existing_pp else str(_deps_dir)
        )

    # Apply OS-level sandbox to app backend process.
    #
    # ``policy_cache`` is bind-mount-hidden in every tier so the AGENT's own
    # subprocesses cannot read or rewrite the ceiling. This child is the one exception
    # that has to see it: cache-only mode makes it resolve the fleet ceiling FROM that
    # file and fail closed without it, so hiding it here would stop every app backend on
    # a centrally-governed host. Reading the ceiling it is about to be bound by is not an
    # escalation — the exposure the mask exists to prevent is the model-driven agent
    # learning the deny patterns, and this is Kiro Crew's own spawn, not a tool call.
    # Passed only when cache-only mode is actually on, so an ungoverned host is unchanged.
    #
    # READ is all it gets WHERE A SANDBOX APPLIES. ``wrap_argv`` seals this particular
    # directory read-only rather than honouring the blanket "visible" meaning, because an
    # app backend is arbitrary third-party code and the cache metadata records the source
    # the next boot trusts — write access here would let an app pick the ceiling for every
    # later boot on the host. That is enforced in ``sandbox``, not here, so this call site
    # cannot widen it.
    #
    # On a host running unconfined — no sandbox backend, or ``agent.sandbox='off'`` with the
    # ``sandbox_allow_no_isolation`` opt-in — there is no seal to apply, and this argument
    # is inert: the child has the whole filesystem, so the cache is one of many things it
    # can write and singling it out would neither restore the seal nor be the tightest
    # control available. What still bounds a forged cache there is provenance rather than
    # permissions: with ``require_policy_signature`` set in the admission policy, a document
    # nobody trusted is refused however it got onto disk.
    _visible: tuple[str, ...] = ()
    if _platform_extra.get(POLICY_CACHE_ONLY_ENV):
        _visible = (str(policy_cache_dir()),)
    sandboxed_cmd, cleanup_path = wrap_argv(cmd, mode="standard", extra_visible_dirs=_visible)
    if _visible and list(sandboxed_cmd) == list(cmd):
        # The wrap was a no-op, so this host has no OS confinement at all: no sandbox backend,
        # or agent.sandbox='off' with the sandbox_allow_no_isolation opt-in. Said once,
        # because the combination is worth naming — a centrally governed host running app code
        # unconfined — and because the actionable answer is not obvious. It is NOT a refusal:
        # the read-only seal is only one of the protections absent here, and an unconfined
        # process can rewrite security_policy.json and the admission policy directly (the
        # keystone gate covers TOOL CALLS, not an arbitrary process's open()), so refusing to
        # let an app read the ceiling while it can replace the ceiling protects nothing.
        global _warned_unconfined_cache
        if not _warned_unconfined_cache:
            _warned_unconfined_cache = True
            logger.warning(
                "SECURITY: this host follows a central governance policy but has no OS "
                "sandbox, so app backends run unconfined and the policy cache is writable by "
                "them. Set require_policy_signature in the admission policy: a signed "
                "document is the control that still holds when confinement does not."
            )
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
        if provision_error:
            # Put the real cause at the top of the backend's own (user-visible)
            # log: the import error missing deps produce reads as an app bug,
            # and this line points it back at provisioning. Written and flushed
            # before the spawn, so the child's inherited fd appends after it.
            log_fh.write(f"[kiro-crew] {provision_error}\n")
            log_fh.flush()
        # Process-group isolation so stop_app_backend can tree-kill the app. Pass
        # both flags explicitly (NOT via **dict unpack — that breaks mypy's Popen
        # overload resolution on the build fleet): start_new_session=True is a
        # no-op on Windows, creationflags resolves to 0 (no-op) on POSIX.
        try:
            proc = popen_limited(
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
                profile=(
                    RLIMIT_PROFILE_BUILD if app_name in _BUILD_CAPABLE_APPS else RLIMIT_PROFILE_TOOL
                ),
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

    # Persist identity for the startup stale-reap (see _reap_stale_app_backends)
    # AND for cross-process stop (stop_recorded_app_backend): without this
    # record a CLI uninstall cannot find the backend, so a backend that cannot
    # be recorded must not be left running.
    if not _record_app_pid(app_name, proc.pid, port):
        logger.error(
            "Could not persist the pid record for app %s backend (pid %d) — "
            "stopping it: an unrecorded backend would be unstoppable from "
            "outside the gateway and would survive uninstall",
            app_name,
            proc.pid,
        )
        stop_app_backend(app_name)
        try:
            log_fh.close()
        except OSError:
            pass
        return None

    # Health check in background, then a standing liveness watch for as long as the
    # backend is tracked — see _supervise_backend_health.
    _start_health_supervisor(ap, manifest.backend.healthCheck)

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
    # Teardown participates in the health serialization, so the pop cannot land in the
    # middle of a reconcile. Without this, a watcher that had already passed its identity
    # check could still be inside `_gate_mcp_registration` when the caller's subsequent
    # `deregister_app` scrubs — and its write would land AFTER, restoring the dead url
    # this whole gate exists to keep out of mcp.json. Holding it across the pop makes the
    # two mutually exclusive: either the reconcile completes and this pop follows it (the
    # caller's scrub then wins), or this pop lands first and the reconcile's identity
    # check fails. Lock order matches `_set_backend_health`: reconcile lock, then `_lock`.
    with _health_reconcile_lock:
        with _lock:
            ap = _processes.pop(app_name, None)
            _allocated_ports.pop(app_name, None)

    # Snapshot the record this stop is acting on: the confirmed-stop removal
    # at the tail deletes ONLY this exact record, so a replacement backend
    # re-recorded concurrently is never erased by a stale stop.
    _rec_snapshot = _read_pidfile().get(app_name)
    if not isinstance(_rec_snapshot, dict):
        _rec_snapshot = None

    # The pidfile record is dropped ONLY on a confirmed stop (see the
    # per-arm _forget_app_pid calls below). Forgetting it up front treated
    # "stop attempted" as "stop confirmed": a dashboard stop of an adopted
    # backend whose identity could not be verified would erase the one
    # cross-process record while the backend kept running, and a later
    # uninstall would read the empty pidfile as proof of termination.
    if not ap:
        return False

    _adopted_stop_confirmed = True
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
            # PID-reuse guard: signal a recorded PID only when its live
            # start-time identity still POSITIVELY matches the token captured
            # at adoption (same convention as the spawned-backend reap). This
            # is process identity, not a port/address heuristic, so every
            # recycling shape — same address, another local address, a
            # v6-only wildcard, or a non-listener — fails the match and is
            # never signalled. A PID with no recorded token (identity was
            # unreadable at adoption) or an unreadable live value reads as
            # "identity unconfirmed" and is skipped, per the
            # process_start_time contract: do not kill what you cannot name.
            target_pids: set[int] = set()
            unconfirmed: list[int] = []
            unverifiable: list[int] = []
            for pid in ap.adopted_pids:
                recorded_st = ap.adopted_start_times.get(pid)
                live_st = _proc_start_time(pid)
                if recorded_st is not None and live_st == recorded_st:
                    target_pids.add(pid)
                elif platform_compat.pid_exists(pid):
                    unconfirmed.append(pid)
                    if recorded_st is None or live_st is None:
                        # A MISMATCH proves the recorded backend is gone (the
                        # pid was recycled); an UNREADABLE side means the live
                        # process might still BE the backend — only the latter
                        # taints the stop's confirmation.
                        unverifiable.append(pid)
            if unconfirmed:
                logger.warning(
                    "Adopted backend for %s on port %s: skipping live PIDs %s — "
                    "start-time identity does not match the adoption record "
                    "(recycled PID or unreadable identity); not signalling them",
                    app_name, ap.port, unconfirmed,
                )
                # A live pid we cannot positively NAME might BE the adopted
                # backend (unreadable identity), so this stop is NOT
                # confirmed: keep the pidfile record and report failure so
                # uninstall aborts instead of proceeding past a possibly
                # live backend. A recycled pid (identity mismatch) is the
                # opposite — positive proof the recorded backend is gone —
                # and does not taint confirmation. The confirmed targets
                # below are still signalled first; a retry has the rest.
                _adopted_stop_confirmed = not unverifiable
            else:
                _adopted_stop_confirmed = True

            pids: list[int] = []
            for pid in target_pids:
                if pid <= 0:
                    continue
                try:
                    # Identity-PINNED (kill_pid_pinned): on Windows the handle
                    # that re-verifies the start time stays open across the
                    # terminate, so the PID taskkill resolves cannot have been
                    # recycled between the identity check above and the signal.
                    # False means the pin could not be established (the process
                    # exited since the check) — nothing to stop, skip it.
                    # POSIX delegates straight through to os.kill.
                    if (
                        platform_compat.kill_pid_pinned(
                            pid, ap.adopted_start_times[pid], platform_compat.SIGTERM
                        )
                        is False
                    ):
                        logger.info(
                            "Adopted backend for %s: pid %d exited before the "
                            "pinned SIGTERM — nothing to signal",
                            app_name, pid,
                        )
                        continue
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
                # pid_exists (not os.kill(pid,0), which terminates on Windows).
                # The graceful-shutdown wait above is exactly the window in
                # which the backend can exit and its PID be recycled, and
                # SIGKILL is the destructive half — so the escalation re-reads
                # the start-time identity here (this is what covers POSIX,
                # where kill_pid_pinned delegates straight through) and the
                # pinned kill then holds the Windows handle across the signal.
                if (
                    platform_compat.pid_exists(pid)
                    and _proc_start_time(pid) == ap.adopted_start_times[pid]
                ):
                    try:
                        if (
                            platform_compat.kill_pid_pinned(
                                pid,
                                ap.adopted_start_times[pid],
                                platform_compat.SIGKILL,
                            )
                            is not False
                        ):
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
                    logger.debug(
                        "SEL log_api_access failed for app_backend_stop_adopted sigkill: %s", exc
                    )
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
    if ap.proc is None and ap.port and not _adopted_stop_confirmed:
        # Adopted stop with live-but-unverifiable pids left behind: the
        # record survives for the retry and the caller learns the truth.
        return False
    _forget_app_pid_if(app_name, _rec_snapshot)
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


def _gate_mcp_registration(app_name: str, port: int, *, healthy: bool) -> bool:
    """Register the app's MCP servers once its backend is healthy, or scrub them if not.

    Called from the health-check loop so the global mcp.json never carries an HTTP MCP url
    for an app whose backend isn't actually serving (registering with an optimistic
    pre-health port would leave a dead url for an enabled app whose backend never
    became healthy, breaking every kiro-cli session). On
    health success we (re)register with the confirmed live port; on failure we deregister
    so no dead entry survives. Never raises — registration must not crash the health loop.

    Returns whether the reconcile landed. The caller records that, because the health
    FLAG moves whether or not mcp.json could be written: without a success signal a
    transient write failure would strand a dead (or missing) entry until the next health
    transition, which for a backend that then stays put never comes."""
    try:
        if healthy:
            # circular import: bridges imports backend.get_app_backend_port, so deferring
            # this import to call time breaks the backend ↔ bridges module cycle.
            from kiro_crew.apps.bridges import reregister_app_mcp_servers

            # Symmetric with the scrub below: the agent JSONs carry the server spec, so a
            # registration whose agent half failed has not made the tools reachable — and
            # letting `mcp_healthy` advance on it would strand the app's agent without
            # its MCP tools, with nothing left to retry.
            register_io_failures: list[str] = []
            reregister_app_mcp_servers(app_name, live_port=port, io_failures=register_io_failures)
            if register_io_failures:
                logger.warning(
                    "App %s: %d agent(s) could not be rewritten after MCP registration "
                    "(%s); reporting the reconcile unlanded so the watch retries",
                    app_name, len(register_io_failures), ", ".join(register_io_failures),
                )
                return False
        else:
            # circular import: see above — bridges ↔ backend cycle, deferred to call time.
            from kiro_crew.apps.bridges import scrub_backend_mcp_url

            # NOT a blanket deregister. An app's stdio MCP servers are launched by
            # kiro-cli itself and have no port to be dead, so dropping them because an
            # HTTP backend died takes working tools away for a reason that has nothing to
            # do with them. scrub_backend_mcp_url pops the HTTP entry and keeps the rest
            # — falling back to removing everything when the manifest cannot say which is
            # which, since the dead url must not survive on the strength of not knowing.
            scrub_unreconciled: list[str] = []
            kept = scrub_backend_mcp_url(app_name, unreconciled=scrub_unreconciled)
            if scrub_unreconciled:
                logger.warning(
                    "App %s: the scrub could not be completed (%s); reporting it "
                    "unlanded so the watch retries",
                    app_name, "; ".join(scrub_unreconciled),
                )
                return False
            if kept:
                logger.info(
                    "App %s: kept %d backend-independent MCP server(s) after the scrub",
                    app_name, len(kept),
                )
            # The scrub is only half the removal: an app's materialized agent JSONs COPY
            # the server's launch spec, and the agent config is what kiro-cli loads. So
            # clearing the global map alone leaves the agents still naming the dead url —
            # the exact outage this gate exists to prevent, just one file over.
            # Registration already refreshes agents for this reason; mirror it here.
            #
            # A failed refresh makes the whole reconcile UNLANDED rather than being
            # swallowed. Registration treats its own refresh as non-fatal, but that path
            # has no retry behind it, so non-fatal there means "do not fail the
            # registration". Here the watch retries, an idempotent re-scrub is cheap, and
            # the alternative is a dead url left permanently in the file kiro-cli reads.
            from kiro_crew.apps.bridges import refresh_app_agents

            # refresh_app_agents, NOT a hand-rolled re-materialization: it already
            # carries the two guards this path must honour — a `resources="app"` app
            # registers its own agents and the gateway must not publish duplicates, and
            # a denied app's agents must be SCRUBBED rather than rewritten back into
            # dispatchable existence. Both return an empty list, which is "nothing for us
            # to do" rather than a failure; only the io_failures collector means retry.
            # The agent refresh RE-MATERIALIZES this app's agent configs. Unlike the
            # scrub above — always safe, and therefore never gated — that is a WRITE, and
            # for an app the operator has disabled it puts back the very files a
            # concurrent `deregister_app` just removed. Checked BEFORE and AFTER: the CLI
            # runs in another process, so neither check can be atomic with the write, and
            # only the pair converges.
            # Deleting happens only on a CONFIRMED disable. `_drop_disabled_app_resources`
            # unlinks materialized agents, taking user-owned fields with them, and
            # `installed.json` can fail to read transiently — so an UNKNOWN state must
            # not be collapsed into "disabled". It reports unlanded instead and the watch
            # retries. The cleanup's own result is the reconcile's result, because
            # deregister_app reports softly and discarding it would record a removal that
            # never happened.
            enabled = _app_enabled_state(app_name)
            if enabled is False:
                return _drop_disabled_app_resources(app_name)
            if enabled is None:
                return False  # unknown: neither refresh nor delete; try again next sweep
            scrub_io_failures: list[str] = []
            refresh_app_agents(app_name, io_failures=scrub_io_failures)
            enabled = _app_enabled_state(app_name)
            if enabled is False:
                return _drop_disabled_app_resources(app_name)
            if enabled is None:
                return False
            if scrub_io_failures:
                logger.warning(
                    "App %s: %d agent(s) could not be rewritten after the MCP scrub (%s); "
                    "reporting the reconcile unlanded so the watch retries",
                    app_name, len(scrub_io_failures), ", ".join(scrub_io_failures),
                )
                return False
        return True
    except Exception as exc:  # noqa: BLE001 — health loop must never crash on reconcile
        logger.warning("Health-gated MCP registration failed for app %s: %s", app_name, exc)
        return False


def _warn_bad_health_path(health_path: str) -> None:
    """Log a rejected manifest health path once per distinct value."""

    with _health_warn_lock:
        if health_path in _warned_health_paths:
            return
        _warned_health_paths.add(health_path)
    logger.warning(
        "App backend health path %r is unsafe; healthCheck must be an absolute "
        "loopback path such as /health",
        health_path,
    )


def _health_probe_url(port: int, health_path: str) -> str | None:
    """Build a loopback health URL without letting app text change its authority."""

    if not 0 < port < 65536:
        return None
    if not _HEALTH_PATH_RE.fullmatch(health_path or ""):
        _warn_bad_health_path(health_path)
        return None
    return f"http://127.0.0.1:{port}{health_path}"


def _health_probe(
    port: int,
    health_path: str,
    *,
    timeout: float = _HEALTH_CHECK_TIMEOUT,
) -> bool:
    """Whether the validated loopback health endpoint answers below 400. Never raises.

    `http.client.HTTPException` is caught alongside the socket errors because it is NOT
    an `OSError` or `URLError` subclass (only `RemoteDisconnected` is, via
    `ConnectionResetError`), and `urllib`'s `do_open` re-raises `getresponse()` failures
    unwrapped. An app backend is arbitrary third-party code — an `exec` backend, or an
    adopted process we do not own — so a non-HTTP first line on the port is a real
    condition, and `BadStatusLine` escaping here would kill the standing watch thread
    and freeze `healthy` at its last value: silently reinstating the write-once bug this
    module's watch exists to prevent.
    """
    url = _health_probe_url(port, health_path)
    if url is None:
        return False
    try:
        req = urllib.request.Request(url, method="GET")
        with loopback_urlopen(req, timeout=timeout) as resp:
            return bool(resp.status < 400)
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return False


def _health_check_loop(ap: AppProcess, health_path: str) -> AppProcess | None:
    """Poll the health endpoint until it responds or we give up.

    Takes the RECORD rather than a name and a port, which is what binds the whole poll to
    one generation. A name plus a port are two independent inputs that can disagree: a
    stop/start landing between the spawn and this thread's first statement would hand a
    name lookup the SUCCESSOR while the port argument still named the predecessor, and
    the poll would then promote — or on exhaustion scrub — a backend whose port it never
    probed. Deriving both from the record leaves nothing to disagree.

    Returns the record if this call promoted it to healthy, else None (never answered, or
    no longer the tracked entry).
    """
    app_name = ap.app_name
    port = ap.port
    for attempt in range(_HEALTH_CHECK_RETRIES):
        time.sleep(_HEALTH_CHECK_INTERVAL)
        with _lock:
            if _processes.get(app_name) is not ap:
                return None  # replaced or stopped — this poll is a retired generation
        if _health_probe(port, health_path):
            # Health-gated MCP registration: only now that the
            # backend has passed /health do we write its HTTP MCP url (live port) to
            # global mcp.json. Registering before this could leave a dead-but-enabled
            # url for an app whose backend never became healthy — the kiro-cli outage.
            # Routed through the shared transition, which re-checks that `ap` is still
            # the tracked record, so this is ordered against the watch's demotions
            # rather than racing them.
            if not _set_backend_health(ap, healthy=True):
                return None
            logger.info(
                "App %s backend healthy (port %d, attempt %d)",
                app_name, port, attempt + 1,
            )
            return ap

    logger.warning(
        "App %s backend failed health check after %d attempts",
        app_name, _HEALTH_CHECK_RETRIES,
    )
    # Backend never became healthy: scrub any optimistic/stale MCP entry so kiro-cli does
    # not keep dialing a dead port on every session (the reverted-outage shape).
    #
    # Identity-guarded like every other transition, on the one record this loop probed.
    # `_deregister_mcp_servers` removes by app NAME, so a restart during the startup
    # window — whose successor may already have come up and registered — would otherwise
    # have this retiring loop scrub the healthy successor's entry. That scrub also
    # bypasses the record, so the successor's `mcp_healthy` would still read True and the
    # watch's retry condition would never fire to put it back.
    _set_backend_health(ap, healthy=False)
    return None


def _rebind_adopted_owners(ap: AppProcess, health_path: str) -> bool:
    """Re-capture an adopted backend's owning PIDs, or refuse to confirm ownership.

    Reuses the adoption-time consistency sandwich (:func:`_capture_adopted_owners`), so a
    responder that exits mid-capture cannot hand ownership to a bystander. Returns False
    when ownership cannot be established, which the caller treats as "do not promote".
    """
    try:
        captured = _capture_adopted_owners(ap.app_name, ap.port, health_path)
    except Exception as exc:  # noqa: BLE001 — the watch must never die on a probe
        logger.warning(
            "App %s: could not re-capture adopted owners on port %d: %s",
            ap.app_name, ap.port, exc,
        )
        return False
    if captured is None:
        logger.warning(
            "App %s: adopted backend on port %d answered again but its ownership could "
            "not be confirmed — leaving it unhealthy", ap.app_name, ap.port,
        )
        return False
    pids, start_times = captured
    with _lock:
        if _processes.get(ap.app_name) is not ap:
            return False
        ap.adopted_pids = pids
        ap.adopted_start_times = start_times
    return True


def _watch_backend_health(ap: AppProcess, health_path: str) -> None:
    """Run the liveness watch, surviving an unexpected fault in any single sweep.

    The whole point of the guard is the failure MODE: an exception escaping the sweep
    kills this daemon thread, and a dead watch freezes ``healthy`` at its last value —
    silently restoring the write-once behaviour this watch exists to remove, with the
    proxy still routing to a port nothing serves. A logged fault that costs one sweep is
    strictly better. Restarting resets the consecutive-failure counter, which is the
    conservative direction (it delays a demotion rather than causing a spurious one), and
    the inner loop sleeps before its first probe, so a persistent fault cannot spin.
    """
    while True:
        try:
            _watch_backend_health_sweeps(ap, health_path)
            return
        except Exception:  # noqa: BLE001 — see the docstring; a dying watch is the bug
            logger.warning(
                "App %s: health watch sweep failed on port %d; restarting the watch",
                ap.app_name, ap.port, exc_info=True,
            )
            with _lock:
                if _processes.get(ap.app_name) is not ap:
                    return  # no longer tracked — nothing left to watch


def _watch_backend_health_sweeps(ap: AppProcess, health_path: str) -> None:
    """Keep re-checking an already-healthy backend so ``healthy`` can go back to False.

    Without this the startup poll would leave ``healthy`` a write-once cache: a backend
    that died an hour later would still be routed to by the reverse proxy
    (:func:`get_app_backend_port` gates purely on the flag) and still reported
    ``healthy`` by ``/api/apps``.

    Liveness is checked cheapest-first. For a backend we spawned, ``Popen.poll()``
    answers from an already-reaped exit status with no syscall to the app at all and is
    DECISIVE — an exited process cannot come back on its own, so one observation demotes
    it and the watch stops. An adopted backend has no ``Popen`` handle (it belongs to
    another supervisor) and is judged by the health endpoint alone.

    An HTTP failure from a process that is still alive is NOT decisive: it may be a slow
    request or a GC pause, so demotion needs `_HEALTH_WATCH_FAILURES` consecutive misses
    and stays REVERSIBLE — the watch keeps running and re-promotes on the next success,
    which is what lets an app that wedged briefly heal without operator action.

    Exits when the record is no longer the tracked one for its app: ``stop_app_backend``
    pops it and a restart replaces it, so this needs no separate teardown — the same
    "no longer tracked" guard the startup poll already uses.
    """
    consecutive_failures = 0
    while True:
        time.sleep(_HEALTH_WATCH_INTERVAL)
        with _lock:
            # Identity, not name: a stop/start under the same name installs a NEW record
            # with its own watch, and demoting that one from here would take a backend
            # offline on evidence gathered about its predecessor.
            if _processes.get(ap.app_name) is not ap:
                return
            was_healthy = ap.healthy
            mcp_healthy = ap.mcp_healthy
            proc = ap.proc

        if proc is not None and proc.poll() is not None:
            # A dead Popen never revives, so there is no health verdict left to reach —
            # but the watch may not leave until the MCP entry is actually out. This is
            # the one place where giving up strands the dead URL permanently: nothing
            # else revisits an exited backend, so a scrub that did not land would stay
            # unlanded and kiro-cli would keep dialing it every session. Keep sweeping
            # (one attempt per interval) until the entry is reconciled or the record is
            # dropped by stop_app_backend.
            #
            # `mcp_healthy` gates this as well as `healthy`: an entry that still says
            # healthy has to come out even when the flag was moved without the write
            # landing. It is TRI-STATE, and only `False` means "confirmed scrubbed" —
            # `None` is *unknown*, which is what a failed startup reconcile leaves
            # behind, and treating it as "nothing to unwind" would abandon exactly the
            # entry that most needs removing.
            if was_healthy:
                _demote(ap, reason=f"process exited (rc={proc.returncode})")
            elif mcp_healthy is not False:
                _retry_mcp_reconcile(ap, healthy=False)
            else:
                return  # confirmed scrubbed — nothing to unwind
            with _lock:
                dropped = _processes.get(ap.app_name) is not ap
                reconciled = ap.mcp_healthy is False
            if dropped or reconciled:
                return
            continue

        if _health_probe(ap.port, health_path):
            consecutive_failures = 0
            healthy = True
        else:
            consecutive_failures += 1
            healthy = was_healthy and consecutive_failures < _HEALTH_WATCH_FAILURES

        if healthy != was_healthy:
            if healthy:
                # An ADOPTED record carries the PIDs `stop_app_backend` will signal and
                # `uninstall` will act behind. Those were captured at adoption, and a
                # recovery means the EXTERNAL supervisor put something back — quite
                # possibly a different process. Promoting without re-binding ownership
                # would mark the record freshly-valid while its identities name a process
                # that is gone, so stop would signal the wrong PIDs (or none) and leave
                # the live replacement running while its files are mutated or removed.
                # Refuse the promotion instead: unhealthy-but-serving is recoverable on
                # the next sweep, a mis-bound owner set is not.
                if ap.proc is None and not _rebind_adopted_owners(ap, health_path):
                    continue
                _promote(ap)
            else:
                _demote(ap, reason=f"{consecutive_failures} consecutive failed health probes")
        elif mcp_healthy != healthy:
            # The verdict is unchanged but mcp.json never caught up — a previous
            # reconcile failed. Retry it here rather than waiting for the next health
            # transition, which for a backend that now stays put would never arrive.
            _retry_mcp_reconcile(ap, healthy=healthy)


def _app_enabled_state(app_name: str) -> bool | None:
    """Tri-state enablement: True, False, or None when it could not be read.

    The distinction is load-bearing, because the two callers want OPPOSITE defaults on an
    unreadable state. Refusing to ADD resources when enablement is unknown is safe — the
    app stays as it is. DELETING them when it is unknown is not: `_drop_disabled_app_resources`
    unlinks materialized agents, taking the user-owned fields `_preserve_user_agent_edits`
    carries, and `installed.json` can fail to read transiently. Collapsing "unknown" into
    "disabled" would destroy data over a temporary fault.
    """
    try:
        # circular import: manager imports from this module's package at call time.
        #
        # `app_enabled_state`, NOT `is_app_enabled`: the latter returns False for BOTH a
        # deliberate disable and an unreadable metadata file, because `_read_installed`
        # answers None to both. Trusting that collapsed False would make this whole
        # tri-state a no-op for the transient fault it exists to catch — the read error
        # never raises, so the `except` below would never see it.
        from kiro_crew.apps.manager import app_enabled_state

        return app_enabled_state(app_name)
    except Exception as exc:  # noqa: BLE001 — unknown is a state, not a crash
        logger.warning(
            "App %s: could not read its enabled state: %s", app_name, exc,
        )
        return None


def _drop_disabled_app_resources(app_name: str) -> bool:
    """Remove everything registered for an app that turned out to be disabled.

    Returns whether the removal COMPLETED. ``deregister_app`` reports most problems
    softly, in ``RegistrationResult.errors`` rather than by raising, so a failed removal
    looks identical to a clean one unless that list is read. Idempotent with the CLI's
    own deregistration, so running it a second time costs nothing.
    """
    try:
        # circular import: bridges ↔ backend, deferred to call time.
        from kiro_crew.apps.bridges import deregister_app

        result = deregister_app(app_name)
    except Exception as exc:  # noqa: BLE001 — must not crash the watch
        logger.warning("App %s: could not drop a disabled app's resources: %s", app_name, exc)
        return False
    errors = list(getattr(result, "errors", None) or [])
    if errors:
        logger.warning(
            "App %s: dropping a disabled app's resources did not complete (%s)",
            app_name, "; ".join(errors),
        )
        return False
    return True


def _undo_promotion_of_disabled_app(ap: AppProcess) -> bool:
    """Remove what a promotion registered for an app disabled while it was being written.

    The enabled check and the write CANNOT be atomic: ``kirocrew app disable`` runs in
    another process, so there is no lock to share with it. Ordering closes the interleave
    where the flag is read after the resources come down; this closes the other one,
    where the check passes and the disable completes before the write lands. Verifying
    afterwards and undoing is the convergence that is actually available.

    ``deregister_app`` is idempotent and removes exactly what the promotion put back, so
    running it a second time after the CLI's own call costs nothing.

    Returns whether the removal COMPLETED. ``deregister_app`` reports most problems
    softly, in ``RegistrationResult.errors`` rather than by raising, so a failed removal
    looks identical to a clean one unless that list is read. ``mcp_healthy`` therefore
    only moves to False on a complete success — leaving it otherwise is what makes the
    next sweep try again, instead of recording a removal that did not happen and letting
    a disabled app stay dispatchable.
    """
    with _lock:
        if _processes.get(ap.app_name) is ap:
            ap.healthy = False
    if not _drop_disabled_app_resources(ap.app_name):
        logger.warning(
            "App %s: undoing the registration of a now-disabled app did not complete; "
            "retrying on the next sweep", ap.app_name,
        )
        return False
    with _lock:
        if _processes.get(ap.app_name) is ap:
            ap.mcp_healthy = False
    logger.warning(
        "App %s was disabled while its recovery was being registered; the registration "
        "has been undone", ap.app_name,
    )
    return True


def _set_backend_health(ap: AppProcess, *, healthy: bool) -> bool:
    """Flip ``ap.healthy`` and move its MCP entry, only while ``ap`` is still tracked.

    The identity re-check has to stay effective THROUGH the reconcile, not merely
    alongside the flag write, because the MCP writers key on the app NAME rather than on
    this record: `_deregister_mcp_servers` removes every ``<app>:`` entry, so a verdict
    formed about a record that has since been replaced would scrub the SUCCESSOR's live
    servers, and a stale re-register would publish the predecessor's dead port — the
    dead-URL outage the health gating exists to prevent.

    `_lock` cannot simply be held across the reconcile (it does manifest and config file
    I/O, and the proxy's get_app_backend_port must not block behind it), so the ordering
    is established with `_health_reconcile_lock` instead. That is sufficient because
    every health-driven reconcile takes it: passing the identity check proves the
    successor is not yet in `_processes`, hence has not registered, and its own
    registration must then queue behind this one — so the last write is always the
    live record's.

    Returns True if the transition was applied.
    """
    with _health_reconcile_lock:
        # IDENTITY FIRST. The undo below deregisters by app NAME, so running it for a
        # record that is no longer the tracked one would delete the SUCCESSOR's
        # resources — and an unreadable enabled state is exactly the case that would
        # send a retired watcher down that path.
        with _lock:
            if _processes.get(ap.app_name) is not ap:
                return False
        # Only a promotion is gated; a demotion's scrub must never be blocked — and must
        # not even READ the enabled state, which is a file access it has no use for.
        enabled = _app_enabled_state(ap.app_name) if healthy else True
        if enabled is not True:
            # Undo ONLY on a CONFIRMED disable. The undo deregisters, which unlinks
            # materialized agents and takes the user-owned fields with them, and
            # `installed.json` can fail to read transiently — an unknown state must not
            # be allowed to destroy data over a temporary fault. Unknown simply refuses
            # the promotion and tries again next sweep.
            #
            # When it IS confirmed disabled and the record still believes something of
            # ours is registered, an earlier undo did not land; this is where it is
            # retried, because nothing else revisits a disabled app.
            if enabled is False and ap.mcp_healthy is not False:
                _undo_promotion_of_disabled_app(ap)
            return False
        with _lock:
            if _processes.get(ap.app_name) is not ap:
                return False
            unchanged = ap.healthy == healthy
            ap.healthy = healthy
            # Short-circuit ONLY when the verdict did not change. `mcp_healthy` can be
            # stale in the other direction after a partial reconcile — an MCP write that
            # landed followed by an agent write that did not leaves it unmoved — so on a
            # TRANSITION the reconcile has to run even when the two happen to agree, or
            # a demotion would skip the scrub and leave the dead url registered.
            if unchanged and ap.mcp_healthy == healthy:
                return True  # already reconciled — nothing to rewrite
        if _gate_mcp_registration(ap.app_name, ap.port, healthy=healthy):
            with _lock:
                # Only a reconcile that LANDED updates the record, so a transient
                # failure leaves `mcp_healthy` out of step with `healthy` and the
                # watch's next sweep tries again. Re-check identity: the write
                # happened outside `_lock`.
                if _processes.get(ap.app_name) is ap:
                    ap.mcp_healthy = healthy
        # VERIFY, because the check above could not be atomic with the write: a disable
        # in the CLI's process can complete in between, and leaving the registration
        # would keep a disabled app dispatchable.
        # Same asymmetry on the verify: only a CONFIRMED disable undoes. An unknown
        # state here leaves the registration standing — the pre-check had confirmed the
        # app enabled, so the likely reading is a momentary read fault, and deleting on
        # that basis is the unrecoverable direction.
        if healthy and _app_enabled_state(ap.app_name) is False:
            _undo_promotion_of_disabled_app(ap)
            return False
        return True


def _demote(ap: AppProcess, *, reason: str) -> None:
    """Mark a backend unhealthy and scrub its MCP entry. Reversible — see _promote."""
    if _set_backend_health(ap, healthy=False):
        logger.warning(
            "App %s backend went unhealthy on port %d — %s", ap.app_name, ap.port, reason,
        )


def _promote(ap: AppProcess) -> None:
    """Mark a backend healthy again after a demotion and re-register its MCP servers."""
    if _set_backend_health(ap, healthy=True):
        logger.info("App %s backend recovered on port %d", ap.app_name, ap.port)


def _retry_mcp_reconcile(ap: AppProcess, *, healthy: bool) -> None:
    """Re-attempt a reconcile that did not land, without re-announcing its transition.

    The health verdict has not changed here — only mcp.json is behind — so this logs at
    debug rather than repeating the demote/recover line every sweep until it succeeds.
    """
    if _set_backend_health(ap, healthy=healthy):
        logger.debug(
            "App %s: retried MCP reconcile (healthy=%s) on port %d",
            ap.app_name, healthy, ap.port,
        )


def _supervise_backend_health(ap: AppProcess, health_path: str) -> None:
    """Thread target: wait for the backend to come up, then watch it for as long as it
    stays tracked."""
    if _health_check_loop(ap, health_path) is not None:
        _watch_backend_health(ap, health_path)


def _start_health_supervisor(ap: AppProcess, health_path: str) -> None:
    """Run :func:`_supervise_backend_health` on this backend's own daemon thread.

    The record is handed over directly, so the supervisor is bound to this generation
    from the moment of the spawn — there is no window between inserting the record and
    the thread resolving it in which a restart could substitute a different one.
    """
    threading.Thread(
        target=_supervise_backend_health,
        args=(ap, health_path),
        daemon=True,
        name=f"app-health-{ap.app_name}",
    ).start()


def _start_adopted_health_watch(ap: AppProcess, health_path: str) -> None:
    """Watch an ADOPTED backend, which skipped the startup poll by already being healthy.

    Adoption proves the instance is serving right now, so there is nothing to wait for —
    but that made ``healthy`` write-once on this path too, and an external instance is
    exactly the kind we do not control the lifetime of. It has no ``Popen`` handle, so
    the watch judges it by its health endpoint alone.
    """
    threading.Thread(
        target=_watch_backend_health,
        args=(ap, health_path),
        daemon=True,
        name=f"app-health-{ap.app_name}",
    ).start()


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


def stop_recorded_app_backend(app_name: str) -> bool:
    """Stop an app's backend from ANY process, confirming termination.

    :func:`stop_app_backend` only reaches backends TRACKED IN THIS PROCESS;
    a CLI uninstall runs outside the gateway, where ``_processes`` is empty,
    so a running backend would survive it and could recreate generated deps
    trees mid-purge. This helper also consults the persisted pidfile record
    (written at spawn or adoption, cross-process), signals it, and WAITS
    for death, PID-reuse-guarded by the start-time token. Returns True only
    when no recorded backend remains alive; a live pid whose identity
    cannot be positively confirmed is REFUSED, not killed (same convention
    as the adopted-stop path), and reads as False so the caller aborts.

    An UNCONFIRMED stop must not consume the record: stop_app_backend
    forgets the pidfile entry unconditionally, so without restoring it a
    failed stop would make the RETRY find no record and report success
    while the backend still runs. On any False return the snapshot is put
    back (only if nothing re-recorded the app meanwhile). An unreadable
    pidfile likewise reads as unconfirmed - it cannot prove absence.

    The whole read-validate-stop runs under the per-app lifecycle flock,
    so a spawn mid-window (process live, record not yet persisted) is
    waited out rather than misread as "no backend".
    """
    with app_backend_lifecycle_flock(app_name):
        return _stop_recorded_outer(app_name)


def stop_recorded_backend_preflight(app_name: str) -> bool:
    """NON-DESTRUCTIVE probe: would a confirmed stop of *app_name* succeed?

    Reads the same evidence :func:`stop_recorded_app_backend` acts on -
    pidfile readability and shape, and the identity-verifiability of every
    recorded live pid - without signalling anything or consuming records.
    Callers use it as an early uninstall precondition so an unconfirmable
    stop aborts BEFORE destructive steps (the onUninstall hook may need the
    still-running backend), while the actual confirmed stop runs after.
    False = a later confirmed stop would refuse; nothing has been changed.
    """
    try:
        with open(_pidfile_path()) as fh:
            _raw = json.load(fh)
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    if not isinstance(_raw, dict):
        return False
    rec = _raw.get(app_name)
    if rec is None:
        return True
    if not isinstance(rec, dict):
        return False
    adopted = rec.get("adopted")
    if isinstance(adopted, list):
        for entry in adopted:
            if not isinstance(entry, dict):
                continue
            apid = entry.get("pid")
            atoken = entry.get("start_time")
            if not isinstance(apid, int) or apid <= 0 or not _pid_alive(apid):
                continue
            if not atoken or not isinstance(atoken, str):
                return False
            live_st = _proc_start_time(apid)
            if not live_st:
                return False
        return True
    pid = rec.get("pid")
    if not isinstance(pid, int) or pid <= 0 or not _pid_alive(pid):
        return True
    token = rec.get("start_time")
    live = _proc_start_time(pid)
    if token and live and token != live:
        return True  # recycled: the recorded backend is gone
    return bool(token and live)


def _stop_recorded_outer(app_name: str) -> bool:
    # Strict read: FileNotFoundError means genuinely no records (True path
    # for an app with no backend), but a CORRUPT or unreadable pidfile
    # cannot prove there is no running backend - that is unconfirmed.
    try:
        with open(_pidfile_path()) as fh:
            _raw = json.load(fh)
    except FileNotFoundError:
        _raw = {}
    except (OSError, ValueError) as exc:
        logger.error(
            "App-backend pidfile unreadable (%s); cannot confirm %s has no backend",
            exc,
            app_name,
        )
        return False
    if not isinstance(_raw, dict):
        # A parseable-but-wrong-shaped pidfile (an app or older build wrote a
        # list or scalar) proves nothing about running backends - treating it
        # as empty would uninstall past a live one. Unconfirmed.
        logger.error(
            "App-backend pidfile has a non-object top level (%s); cannot "
            "confirm %s has no backend",
            type(_raw).__name__,
            app_name,
        )
        return False
    rec = _raw.get(app_name)
    if rec is not None and not isinstance(rec, dict):
        logger.error(
            "App-backend pidfile record for %s is not an object (%s); "
            "cannot confirm termination",
            app_name,
            type(rec).__name__,
        )
        return False

    outcome = _stop_recorded_locked(app_name, rec)
    if not outcome and rec:
        # Put the record back so the retry can still find the backend.
        try:
            with _pidfile_lock, _pidfile_flock():
                data = _read_pidfile()
                if app_name not in data:
                    data[app_name] = rec
                    _write_pidfile(data)
        except Exception as exc:  # noqa: BLE001 - restoration is best-effort
            logger.error("Could not restore pid record for %s: %s", app_name, exc)
    return outcome


def _stop_recorded_locked(app_name: str, rec: dict[str, Any] | None) -> bool:
    """Core of :func:`stop_recorded_app_backend`; may consume the record."""
    stop_app_backend(app_name)
    if not rec:
        return True
    adopted = rec.get("adopted")
    if isinstance(adopted, list):
        # ADOPTED record: kill only the recorded owner pids (pid-scoped, the
        # adopted convention — an external process group may hold unrelated
        # processes), each identity-guarded by its start-time token.
        all_gone = True
        for entry in adopted:
            if not isinstance(entry, dict):
                continue
            apid = entry.get("pid")
            atoken = entry.get("start_time")
            if not isinstance(apid, int) or apid <= 0 or not _pid_alive(apid):
                continue
            if not atoken or not isinstance(atoken, str):
                all_gone = False  # live pid we cannot positively name
                continue
            live_st = _proc_start_time(apid)
            if live_st and live_st != atoken:
                continue  # recycled pid: the adopted owner is gone
            if not live_st:
                all_gone = False
                continue
            for asig in (platform_compat.SIGTERM, platform_compat.SIGKILL):
                try:
                    platform_compat.kill_pid_pinned(apid, atoken, asig)
                except (ProcessLookupError, OSError):
                    pass
                deadline = time.monotonic() + (5.0 if asig == platform_compat.SIGTERM else 3.0)
                dead = False
                while time.monotonic() < deadline:
                    now_st = _proc_start_time(apid)
                    if not _pid_alive(apid) or (now_st and now_st != atoken):
                        dead = True
                        break
                    time.sleep(0.1)
                if dead:
                    break
            else:
                all_gone = False
        if all_gone:
            _forget_app_pid_if(app_name, rec)
        return all_gone
    pid = rec.get("pid")
    if not isinstance(pid, int) or pid <= 0 or not _pid_alive(pid):
        _forget_app_pid_if(app_name, rec)
        return True
    token = rec.get("start_time")
    live = _proc_start_time(pid)
    if token and live and token != live:
        # recycled pid: an unrelated process answers to it now
        _forget_app_pid_if(app_name, rec)
        return True
    if not token or not live:
        # cannot positively confirm the live process IS the recorded
        # backend: refuse to signal an unknown pid
        return False

    def _recorded_backend_gone() -> bool:
        # Dead pid, or a pid recycled onto a different start-time identity:
        # either way the RECORDED backend no longer exists.
        if not _pid_alive(pid):
            return True
        now = _proc_start_time(pid)
        return bool(now) and now != token

    for sig, wait_secs in (
        (platform_compat.SIGTERM, 5.0),
        (platform_compat.SIGKILL, 3.0),
    ):
        try:
            # Pinned: on Windows the verifying handle is held across the
            # taskkill, so the pid cannot be recycled onto an unrelated
            # process between the identity check and the terminate.
            sent = platform_compat.kill_process_tree_pinned(pid, token, sig)
        except (ProcessLookupError, OSError):
            sent = True  # a kill was attempted; fall through to the wait
        if not sent:
            # No kill was sent: the pinned handle could not confirm the
            # recorded identity. That is terminal proof when the recorded
            # backend is simply gone; otherwise it stays unconfirmed.
            if _recorded_backend_gone():
                _forget_app_pid_if(app_name, rec)
                return True
            return False
        deadline = time.monotonic() + wait_secs
        while time.monotonic() < deadline:
            if _recorded_backend_gone():
                _forget_app_pid_if(app_name, rec)
                return True
            time.sleep(0.1)
    if _recorded_backend_gone():
        _forget_app_pid_if(app_name, rec)
        return True
    return False


def _pidfile_path() -> Path:
    return config_dir() / "app_backends.pids.json"


def _proc_start_time(pid: int) -> str | None:
    """Stable per-process start time, or None if unavailable.

    PID-reuse guard: a recorded pid whose live start_time no longer matches has
    been recycled to an unrelated process and MUST NOT be killed. The value must
    be stable across gateway restarts (the reap compares a string recorded by a
    prior generation against one read now), so it cannot use ``hash()`` — that
    is salted per interpreter by ``PYTHONHASHSEED``.

    Per-platform sources live in ``platform_compat.process_start_time``: Linux
    reads ``/proc/<pid>/stat`` field 22, Windows the process creation FILETIME
    through a query-only handle, and other POSIX ``ps -o lstart=``. Resolving it
    there is what keeps the guard alive on Windows — a ``/proc``-or-``ps`` probe
    answers None for every pid there, and a recorded None makes the reap decline
    to confirm ANY backend, so nothing is ever reaped and the entries accumulate.
    """
    return platform_compat.process_start_time(pid)


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


@contextlib.contextmanager
def _pidfile_flock() -> Iterator[None]:
    """CROSS-PROCESS exclusive lock for pidfile read-modify-write.

    ``_pidfile_lock`` serializes only threads of THIS process; the gateway
    and a CLI are different processes, and two unserialized whole-map writes
    lose one side's record (a stale map re-written over a fresh record drops
    a live backend's only cross-process handle). Every RMW must hold BOTH:
    the thread lock (taken by callers) and this file lock.
    """
    lock_path = _pidfile_path().with_name(_pidfile_path().name + ".flock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        with platform_compat.flock_exclusive(fd):
            yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def app_backend_lifecycle_flock(app_name: str) -> Iterator[None]:
    """CROSS-PROCESS per-app lock closing the spawn/record vs stop window.

    A backend exists as a process BEFORE its pidfile record does; a CLI
    uninstall's stop probe landing in that window reads "no record" as "no
    backend" and uninstalls past a live one. The spawn path holds this lock
    from before the fork until the record is persisted (or the spawn is
    torn down), and :func:`stop_recorded_app_backend` holds it across its
    read-validate-stop, so each sees the other's completed state, never the
    window. Lock order: this lock FIRST, pidfile flock inside.
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


def _write_pidfile(data: dict[str, dict[str, Any]]) -> bool:
    # Atomic temp-file + rename (fsync): the whole point of the pidfile is to
    # survive a gateway SIGKILL, so a non-atomic open("w") that truncates first
    # would leave an empty/partial file if the kill lands mid-write.
    # Returns False on failure: _record_app_pid feeds this to the spawn site,
    # which tears the backend down rather than run it unrecorded (a swallowed
    # write failure here would report success up that chain and leave a
    # backend no CLI process can ever find or stop).
    try:
        atomic_write(_pidfile_path(), json.dumps(data), fsync=True)
        return True
    except OSError as exc:
        logger.error("Could not write app-backend pidfile: %s", exc)
        return False


def _record_app_pid(app_name: str, pid: int, port: int) -> bool:
    """Persist a spawned backend's identity for the startup stale-reap.

    Never raises; returns False when the record could NOT be persisted. The
    spawn site MUST treat False as fatal and tear the backend down: this
    record is what lets a CLI process (empty in-memory tracking) find and
    stop the backend later, so a backend running without one would be
    unstoppable from outside the gateway and would survive uninstall.
    """
    if pid <= 0:
        return False
    try:
        # Compute start_time BEFORE taking the lock: the probe is slow on the
        # platforms that cannot answer from memory (a `ps` spawn on macOS, an
        # OpenProcess round trip on Windows), and holding _pidfile_lock across
        # that IO would serialize concurrent enable/stop/uninstall ops behind
        # it. Mirrors the reap path's validate-lock-free / store-under-lock
        # discipline.
        start_time = _proc_start_time(pid)
        with _pidfile_lock, _pidfile_flock():
            data = _read_pidfile()
            data[app_name] = {"pid": pid, "start_time": start_time, "port": port}
            return _write_pidfile(data)
    except Exception as exc:  # noqa: BLE001 — never raises; the caller acts on False
        logger.error("Could not record app pid for %s: %s", app_name, exc)
        return False


def _record_adopted_pids(
    app_name: str,
    port: int,
    adopted_pids: list[int],
    adopted_start_times: dict[int, str],
) -> bool:
    """Persist an ADOPTED backend's owner identities for cross-process stop.

    Shape: ``{"pid": 0, "port": port, "adopted": [{"pid", "start_time"}...]}``.
    ``pid: 0`` keeps the startup stale-reap away (it must never group-SIGTERM
    an external process), while the ``adopted`` list gives
    :func:`stop_recorded_app_backend` the same identity-guarded targets the
    in-process adopted stop uses. Never raises; False means not persisted.
    """
    try:
        entries = [
            {"pid": pid, "start_time": adopted_start_times.get(pid)}
            for pid in adopted_pids
            if pid > 0
        ]
        if not entries:
            return False
        with _pidfile_lock, _pidfile_flock():
            data = _read_pidfile()
            data[app_name] = {"pid": 0, "port": port, "adopted": entries}
            return _write_pidfile(data)
    except Exception as exc:  # noqa: BLE001 — never raises; the caller acts on False
        logger.error("Could not record adopted pids for %s: %s", app_name, exc)
        return False


def _forget_app_pid_if(app_name: str, snapshot: dict[str, Any] | None) -> None:
    """Drop an app's pidfile entry ONLY while it still equals *snapshot*.

    A stop that validated one record must not blindly remove whatever is
    there NOW: a concurrent start (another process, or another thread of the
    gateway) may have re-recorded the app with a REPLACEMENT backend's pid,
    and an unconditional delete would erase the only cross-process handle to
    that live replacement. Never raises.
    """
    if snapshot is None:
        return
    try:
        with _pidfile_lock, _pidfile_flock():
            data = _read_pidfile()
            if data.get(app_name) == snapshot:
                del data[app_name]
                _write_pidfile(data)
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
        logger.debug("Could not conditionally drop app pid record for %s: %s", app_name, exc)


def _forget_app_pid(app_name: str) -> None:
    """Drop an app's pidfile entry (called on a clean stop). Never raises."""
    try:
        with _pidfile_lock, _pidfile_flock():
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
        if isinstance(entry.get("adopted"), list):
            # ADOPTED record: the external backend is not reap's to kill
            # (group-SIGTERM is only safe for our own children) and the
            # record must SURVIVE gateway restarts so a CLI uninstall can
            # still find and stop the adopted owners. Leave it untouched.
            continue
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
            # Identity-PINNED: on Windows the handle that re-verifies the start
            # time stays open across the terminate, so the pid taskkill resolves
            # cannot have been recycled between the check above and the signal.
            # False means the identity could not be pinned -- keep the entry
            # (omit from ``handled``) and retry on a later start, exactly as the
            # unconfirmed-start_time branch above does. POSIX delegates straight
            # through and is unchanged.
            signalled = platform_compat.kill_process_tree_pinned(
                pid, recorded_st, platform_compat.SIGTERM
            )
        except (ProcessLookupError, OSError):
            handled[app_name] = entry  # gone between the probe and the signal
            continue
        if not signalled:
            logger.info(
                "Skipping stale-reap of %s pid %d: identity could not be pinned for the kill",
                app_name, pid,
            )
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
            # Same pinning as the SIGTERM path, and it matters more here: this is
            # the destructive escalation, and the grace window above is exactly
            # the interval in which the pid can be recycled.
            if not platform_compat.kill_process_tree_pinned(
                pid, recorded_st, platform_compat.SIGKILL
            ):
                logger.info(
                    "Skipping stale-reap SIGKILL of %s pid %d: identity could not be pinned",
                    app_name, pid,
                )
                continue
        except (ProcessLookupError, OSError):
            continue
        try:
            sel().log_api_access(
                caller="gateway", operation="app_backend_stale_reap",
                outcome="sigkill", resources=f"{app_name} pid={pid}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "SEL audit failed for app_backend_stale_reap sigkill %s: %s", app_name, exc
            )
    # Drop only the entries we handled, re-reading under the lock so a concurrent
    # enable/disable that wrote during the scan is merged, not clobbered. Drop an
    # entry ONLY if it still equals what we handled: a mid-scan re-record (new
    # pid) yields a different entry that must be kept.
    with _pidfile_lock, _pidfile_flock():
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
                    "Boot resource reconcile: FAILED to revoke resources for " "denied app %s: %s",
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
                denied = app_admission_denied(name, manifest=get_app_manifest(name), action="boot")
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
                # Routed through the shared transition so this shares one order with
                # the watch that _start_adopted_health_watch has by now armed on the
                # same record — the two must not interleave their mcp.json writes.
                if ap.healthy:
                    _set_backend_health(ap, healthy=True)
    return started
