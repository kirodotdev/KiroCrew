"""App backend process management — spawn, health check, stop, and proxy config.

When an app declares a ``backend`` section in its manifest, KiroCrew manages
the backend process lifecycle: spawn on enable, health-check, stop on disable.

This module is the backend's only import path and patch surface. Its rules live in
private owners under :mod:`kiro_crew.apps.backend_runtime`, one per responsibility
(``docs/system-specs/modules/app-kit-platform.md`` §21 has the map), and every name
they hold resolves here. Two constructs stay in this file because repository guards
read them here by path: the spawn transaction (:func:`start_app_backend`,
:func:`_start_app_backend` and :func:`_start_app_backend_body`, with the entry-point
helpers only it uses) and :func:`_pid_alive`.
"""

from __future__ import annotations

import builtins as _builtins
import hashlib
import hmac
import importlib as _importlib
import logging
import os
import shutil
import socket
import subprocess
import sys
import sys as _sys
import time
import typing as _typing
import uuid
from pathlib import Path
from types import ModuleType as _ModuleType
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps import deps_boot as _deps_boot_module
from kiro_crew.apps.execution import (
    app_execution_denied,
    is_builtin_app,
    shipped_builtin_module_path,
)
from kiro_crew.apps.interpreter import app_deps_dir, path_command_is_abi_matched, resolve_app_python
from kiro_crew.apps.manager import app_dir, get_app_manifest
from kiro_crew.apps.registry import minimal_env
from kiro_crew.config.loader import config_dir
from kiro_crew.constants import (
    KIROCREW_SPAWN_INSTANCE_ENV,
    KIROCREW_SPAWNED_ENV,
    KIROCREW_SPAWNED_VALUE,
)
from kiro_crew.sandbox import (
    MD_NOTEBOOK_APP_NAME,
    RLIMIT_PROFILE_BUILD,
    RLIMIT_PROFILE_TOOL,
    app_backend_visible_targets,
    carveout_shadowed_by_foreign_mask,
    cgroup_scope_argv,
    popen_limited,
    run_limited,
    wrap_argv,
)
from kiro_crew.sel import sel
from kiro_crew.subprocess_utf8 import UTF8_TEXT

# ``importlib.reload`` of this module re-executes it in its existing namespace, and the
# one-module backend re-evaluated every module-level value when that happened. The
# owners hold those values now, so a reload (the only way ``_PART_MODULES`` is already
# bound at this line) reloads each owner, lowest layer first, and every owner rebinds
# its imports from the freshly executed owners below it.
if "_PART_MODULES" in globals():
    for _reloaded in globals()["_PART_MODULES"]:
        _importlib.reload(_sys.modules[_reloaded])
    del _reloaded

from kiro_crew.apps.backend_runtime.pidfile import _proc_start_time, _record_app_pid  # noqa: E402
from kiro_crew.apps.backend_runtime.ports import (  # noqa: E402
    _MAX_PORT,
    _MIN_PORT,
    PortUnavailableError,
    _allocated_ports,
    _capture_adopted_owners,
    _claim_port,
    _probe_adoption_health,
    _reserve_free_port,
    _SpawnOwnershipLost,
    _survived_spawn,
)
from kiro_crew.apps.backend_runtime.provisioning import (  # noqa: E402
    _deps_tree_stamp_current,
    provision_app_deps,
)
from kiro_crew.apps.backend_runtime.registration import _set_backend_health  # noqa: E402
from kiro_crew.apps.backend_runtime.startup import DEV_FLEET_APP_NAME  # noqa: E402
from kiro_crew.apps.backend_runtime.supervision import (  # noqa: E402
    _start_adopted_health_watch,
    _start_health_supervisor,
)
from kiro_crew.apps.backend_runtime.termination import (  # noqa: E402
    _drain_exited_root_tree,
    _terminate_retired_spawn,
)
from kiro_crew.apps.backend_runtime.tracking import (  # noqa: E402
    _LIFECYCLE_START,
    AppProcess,
    _advance_lifecycle_locked,
    _await_inflight_spawn,
    _health_reconcile_lock,
    _lock,
    _processes,
    _spawn_publication_owner,
    app_backend_lifecycle_flock,
)

logger = logging.getLogger(__name__)


#: Said once per process: a centrally governed host with no OS confinement (see the spawn).
_warned_unconfined_cache = False


# Apps whose backends spawn real build workloads (vite/pip) and need the
# elevated-but-finite NOFILE ceiling as the workload's ANCESTOR. Every other
# app backend keeps the standard (operator-configurable) resource policy.
_BUILD_CAPABLE_APPS = frozenset({"dev-fleet"})


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
# The spawn transaction
# ---------------------------------------------------------------------------


def start_app_backend(app_name: str) -> AppProcess | None:
    """Start an app's backend process if it declares one.

    Returns the AppProcess on success, None if no backend declared.
    """
    # This public entry point represents an explicit enable/boot-reconcile start. It
    # supersedes an older stop racing a health-driven restart. The restart itself calls
    # the internal entry point below so it does not manufacture a lifecycle transition.
    with _health_reconcile_lock:
        with _lock:
            _advance_lifecycle_locked(app_name, _LIFECYCLE_START)
    return _start_app_backend(app_name)


def _start_app_backend(app_name: str) -> AppProcess | None:
    """Single-flight spawn implementation without an external lifecycle transition."""
    manifest = get_app_manifest(app_name)
    if not manifest or not manifest.backend.entryPoint:
        return None

    await_inflight = False
    spawn_placeholder: AppProcess | None = None
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
            spawn_placeholder = AppProcess(
                app_name=app_name, starting=True, started_at=time.time()
            )
            _processes[app_name] = spawn_placeholder
    if await_inflight:
        logger.info("App %s backend is already starting — awaiting the in-flight spawn", app_name)
        return _await_inflight_spawn(app_name)

    assert spawn_placeholder is not None
    # From here the spawn is single-flighted for this app. The body returns the real
    # AppProcess on success, or None on any failure / no-op path; in EITHER the None
    # case or an exception we must clear THIS call's STARTING placeholder so a later
    # retry isn't permanently blocked (and a success path replaces it with the real
    # record). A stop followed by a later start may replace our placeholder while we
    # wait for the cross-process flock; such a retired call must not spawn or clean up
    # the successor's state.
    # Held across the whole body: the backend exists as a PROCESS before its
    # pidfile record does, and a CLI uninstall probing in that window reads
    # "no record" as "no backend". Under this cross-process lock the probe
    # waits until the record is persisted (or the spawn torn down) - see
    # app_backend_lifecycle_flock.
    # Ordering invariant: while holding the lifecycle flock, revalidate ownership,
    # run the body, and clean every failed/retired reservation before release. A
    # successor can reserve only after that cleanup is complete.
    flock_entered = False
    try:
        with app_backend_lifecycle_flock(app_name):
            flock_entered = True
            with _lock:
                still_owner = _processes.get(app_name) is spawn_placeholder
            if not still_owner:
                _clear_failed_spawn_state(app_name, spawn_placeholder)
                return None

            try:
                owner_context = _spawn_publication_owner.set(spawn_placeholder)
                try:
                    result = _start_app_backend_body(app_name, manifest)
                finally:
                    _spawn_publication_owner.reset(owner_context)
            except Exception:
                _clear_failed_spawn_state(app_name, spawn_placeholder)
                raise
            if result is None:
                _clear_failed_spawn_state(app_name, spawn_placeholder)
            return result
    except Exception:
        # If flock acquisition itself failed, no successor was serialized behind
        # this call. Identity/value checks still prevent clearing another caller.
        if not flock_entered:
            _clear_failed_spawn_state(app_name, spawn_placeholder)
        raise


def _clear_failed_spawn_state(app_name: str, spawn_placeholder: AppProcess) -> None:
    """Release this failed spawn's STARTING placeholder and port reservation.

    Identity is load-bearing: a stop followed by a later start can replace the
    placeholder while this call is still inside the lifecycle flock. Clearing by
    app name or by ``starting`` alone would delete that later caller's placeholder
    and reopen the duplicate-spawn race.
    """
    with _lock:
        if _processes.get(app_name) is spawn_placeholder:
            _processes.pop(app_name, None)
            _allocated_ports.pop(app_name, None)


def _abi_shebang_of(root: Path, script: str) -> str | None:
    """The ABI-matched interpreter a script's shebang names, or None.

    Thin composition of the bridges shebang reader (sensitive-path gated,
    bare direct spelling only - argument-bearing shebangs read as None) and
    the ABI match check. Deferred import: bridges imports THIS module's
    symbols lazily, and the apps package initializer loads bridges first.
    """
    from kiro_crew.apps.bridges import _python_shebang_interpreter

    cand = _python_shebang_interpreter(script)
    if cand and path_command_is_abi_matched(root, cand):
        return cand
    return None


def _deps_boot_path() -> Path:
    """Absolute path of the stdlib-only launch shim (see apps.deps_boot)."""
    return Path(os.path.abspath(_deps_boot_module.__file__))


def _start_app_backend_body(app_name: str, manifest: Any) -> AppProcess | None:
    """The spawn body, single-flighted by the STARTING placeholder set in
    :func:`start_app_backend`. Returns the real AppProcess on success or None on any
    failure; the caller clears the placeholder on None/exception."""
    _spawn_owner = _spawn_publication_owner.get()
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

    # Classified HERE, on the same execution target the gate just vetted, and carried on
    # the record so a later ceiling re-check never re-resolves a path the app owns.
    _admitted_builtin = is_builtin_app(app_name=app_name, app_root=execution_path)

    # Whether this spawn executes the SHIPPED md-notebook backend — provenance on the
    # executed path the admission gate above vetted. Only the isolated-startup branch
    # below reads it: that is the one spawn whose namespace holds an unmasked PAT, so
    # interpreter startup hooks must not ride along. The state-file carve-out further
    # down is keyed off the GENERIC ``is_builtin_app`` check instead, because it applies
    # to every app that owns hidden leaves.
    _shipped_md_notebook = app_name == MD_NOTEBOOK_APP_NAME and is_builtin_app(
        app_name=app_name, app_root=execution_path
    )

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
        try:
            port = _reserve_free_port(app_name)
        except _SpawnOwnershipLost:
            return None
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
            except _SpawnOwnershipLost:
                return None
        except ValueError:
            try:
                port = _reserve_free_port(app_name)
            except _SpawnOwnershipLost:
                return None

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
                logger.info("App %s: healthy instance already on port %d — adopting (pids=%s)", app_name, port, adopted_pids)
                ap = AppProcess(
                    app_name=app_name, port=port, pid=0, proc=None,
                    healthy=True, started_at=time.time(), log_path=str(log_path),
                    adopted_pids=adopted_pids,
                    adopted_start_times=adopted_start_times,
                    gateway_started=True,
                    # NOT `_admitted_builtin`. That classification is sound only for a
                    # process the gateway itself launched from the path the gate vetted.
                    # Here the gateway launched nothing: it found a listener already
                    # answering on the port and adopted it, and no check establishes
                    # that the listener is executing the shipped code the manifest
                    # declares. Carrying the exemption across would let anything that
                    # answers a builtin's port inherit "shipped provenance" and be
                    # skipped by the revocation sweep for good -- the next boot re-probes
                    # and re-adopts to the same verdict, so it would never self-correct.
                    # The ceiling therefore applies to an adopted backend. A genuinely
                    # shipped one is stopped and respawned BY the gateway, which vets
                    # its execution path and classifies it correctly on that path.
                    admitted_builtin=False,
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
                    if (
                        _spawn_owner is not None
                        and _processes.get(app_name) is not _spawn_owner
                    ):
                        return None
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
    # wheels load ahead of the trusted module on its PYTHONPATH - a
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
        # These are the operator's local policy sources
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
    # The port the gateway ACTUALLY bound (``dashboard.server._export_bound_port``),
    # handed to the ONE backend that calls back into the gateway: Dev Fleet reads
    # live-target pointer state through an in-gateway route, because the pointer
    # itself is masked from its namespace. Scoped by app name exactly as the
    # ``KIROCREW_DEVFLEET_BIN_`` loop above is — no other backend has a consumer, and
    # ``pod/runtime.py`` deliberately scrubs this variable from spawns that must not
    # aim at the live gateway. Not a secret: it is the port every dashboard client
    # already connects to, and the gateway's own auth governs what a caller may do
    # there. Absent (a foreground gateway before its site is up, or a test) it is not
    # passed and the backend degrades as documented.
    if app_name == DEV_FLEET_APP_NAME:
        _bound = os.environ.get("KIROCREW_BOUND_PORT", "")
        if _bound.isdigit():
            _platform_extra["KIROCREW_BOUND_PORT"] = _bound
    env = minimal_env(
        PORT=str(port),
        KIROCREW_APP_NAME=app_name,
        KIROCREW_HOME=str(config_dir()),
        **_platform_extra,
    )
    # Identity this backend's whole tree carries, so the startup stale-reap can
    # still find it once the LEADER is gone. The backend is spawned with
    # start_new_session=True, so its group outlives it: when the gateway is
    # SIGKILLed the leader can exit while a uvicorn worker or a build child keeps
    # the assigned PORT bound, and the next generation then spawns onto a port an
    # orphan still owns (the observed 502). The group number is the dead leader's
    # pid and a bare number is indistinguishable from a recycled one, so the reap
    # signals VOUCHED MEMBERS instead -- see _reap_orphaned_backend_group and
    # session_pid.signal_orphaned_spawn_group.
    #
    # KIROCREW_SPAWNED says a Kiro Crew spawned the process; the instance says
    # WHICH spawn, and is minted here (before the process exists) because it has
    # to travel in the child's environment where /proc/<pid>/environ can read it
    # back. Random rather than pid-derived so a recycled pid cannot false-match.
    spawn_instance = uuid.uuid4().hex[:16]
    env[KIROCREW_SPAWNED_ENV] = KIROCREW_SPAWNED_VALUE
    env[KIROCREW_SPAWN_INSTANCE_ENV] = spawn_instance
    # Trusted gateway origin for the backend's own callbacks to this gateway
    # (e.g. POST /api/notifications/push on a declared channel). It is injected
    # ONLY from hard evidence of the port THIS gateway actually owns:
    # KIROCREW_BOUND_PORT, which the gateway exports into its own environment
    # the moment it reserves its port (bound and listening, not yet accepting;
    # before this spawn pass can run — dashboard.server._reserve_dashboard_port).
    # We require it to be present and to parse as an integer in 1..65535. We never fall back to KIROCREW_PORT (an
    # inherited/--port guess), the app's own PORT, a config value, a run-marker,
    # or a built-in default: any of those could point the child at a sibling
    # gateway or at a port nothing is listening on. Absent or invalid evidence
    # => omit the origin entirely, so a backend that needs a callback base fails
    # closed (dormant) rather than trusting a guessed address. Generic name
    # only; no app-specific env is set here.
    bound_port = os.environ.get("KIROCREW_BOUND_PORT", "").strip()
    bound_host_env = os.environ.get("KIROCREW_BOUND_HOST", "").strip()
    gateway_origin = ""
    if (
        bound_port.isdigit()
        and 1 <= int(bound_port) <= 65535
        and bound_host_env in ("", "::1")
    ):
        # Host evidence: absent means loopback (the default bind shapes —
        # loopback itself, or a wildcard bind loopback reaches); "::1" is the
        # v6-loopback family marker. Only these two shapes are injected: a
        # backend's callback arrives with no Origin header, and the gateway's
        # CSRF barrier (origin.check_origin) trusts an Origin-less mutating
        # request ONLY from a loopback peer. A gateway bound to a SPECIFIC
        # interface exports that address, but injecting it would mint an
        # origin whose every mutating callback is refused at the barrier —
        # a half-alive backend, worse than designed dormancy — so that shape
        # is omitted (fail closed, warned below) until such a request path is
        # admitted. IPv6 literals are bracketed per RFC 3986.
        bound_host = bound_host_env or "127.0.0.1"
        if ":" in bound_host and not bound_host.startswith("["):
            bound_host = f"[{bound_host}]"
        gateway_origin = f"http://{bound_host}:{int(bound_port)}"
        env["KIROCREW_GATEWAY_ORIGIN"] = gateway_origin
    else:
        # Dormancy is the DESIGNED outcome here, so the operator must be able
        # to see it: an app that declares notification channels but gets no
        # origin will silently never push. Warn for those; stay at debug for
        # apps with no push surface. getattr defense: tests hand this function
        # reduced manifest stand-ins without a notifications field.
        _declares_channels = bool(
            getattr(getattr(manifest, "notifications", None), "channels", None)
        )
        _why = (
            "gateway bound to a specific interface"
            if bound_host_env not in ("", "::1")
            else "no valid KIROCREW_BOUND_PORT"
        )
        (logger.warning if _declares_channels else logger.debug)(
            "%s; omitting KIROCREW_GATEWAY_ORIGIN for %s backend%s",
            _why,
            app_name,
            (
                " -- it declares notification channels and cannot push until"
                " restarted with a valid origin"
                if _declares_channels
                else ""
            ),
        )
    # Inject the per-app proxy secret so the backend can verify the
    # X-KiroCrew-Proxy HMAC the gateway signs on every forwarded request
    # (CWE-306). Without it the loopback backend would trust any local caller.
    # The same secret keys KIROCREW_GATEWAY_ORIGIN_PROOF, an
    # HMAC-SHA256(secret, origin) the backend recomputes to confirm the origin
    # value was minted by the gateway that alone holds this secret, rather than
    # an inherited or spoofed env value. The proof is injected ONLY when the
    # secret is readable AND the origin was injected above; a missing
    # .app_secret is tolerated as before and yields neither the secret nor the
    # proof (a secret-less legacy backend gets the origin only).
    try:
        _proxy_secret = (root / ".app_secret").read_text().strip()
        if _proxy_secret:
            env["KIROCREW_PROXY_SECRET"] = _proxy_secret
            if gateway_origin:
                env["KIROCREW_GATEWAY_ORIGIN_PROOF"] = hmac.new(
                    _proxy_secret.encode("utf-8"),
                    gateway_origin.encode("utf-8"),
                    hashlib.sha256,
                ).hexdigest()
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
    backend_type = manifest.backend.type

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
    # Module-style entries have no file path — the module runs under the gateway's own
    # interpreter (sys.executable) so it resolves against the gateway's installed
    # packages, with cwd at the kiro_crew source root so relative imports inside the
    # module work without venv setup.
    #
    # Bundled app backends start with the user site disabled through the shared
    # helper. A non-bundled module-style child keeps its parent's policy because
    # Kiro Crew may itself be installed in the user site, and this ``-m`` launch
    # supplies no independent import path. md-notebook keeps its stronger ``-I``
    # contract because it re-admits the exact package root in its script body.
    #
    # ``-I`` drops cwd-on-sys.path and ``PYTHONPATH`` (it implies ``-E``), so the
    # import universe md-notebook needs is restated EXPLICITLY: ``runpy`` (the
    # machinery behind ``-m``) runs the module after inserting the root Kiro Crew
    # itself was imported from. Correct across a venv install, a --user install,
    # and a source tree; ``repr`` keeps both injected strings inert literals.
    elif entry is None:
        python_bin = sys.executable
        _import_root = str(Path(__file__).resolve().parent.parent.parent)
        cwd = _import_root
        if _shipped_md_notebook:
            cmd = platform_compat.isolated_python_argv(
                "-I",
                "-c",
                (
                    "import runpy, sys; "
                    f"sys.path.insert(0, {_import_root!r}); "
                    f"runpy.run_module({entry_point!r}, run_name='__main__', alter_sys=True)"
                ),
                executable=python_bin,
            )
        else:
            cmd = platform_compat.isolated_python_argv(
                "-m",
                entry_point,
                executable=python_bin,
            )

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
        cmd = platform_compat.isolated_python_argv(
            "-m",
            "uvicorn",
            f"{module_path}:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            executable=python_bin,
        )

    # --- Plain Python backend (default) ---
    else:
        # See the ASGI branch: venv python first, else the gateway's own interpreter —
        # one policy shared with the stdio MCP registration path.
        python_bin = resolve_app_python(root)
        cmd = platform_compat.isolated_python_argv(
            entry_str,
            executable=python_bin,
        )
        cwd = str(root)

    # Provisioned-deps launch shim: PYTHONPATH entries are not site dirs, so
    # .pth files in the deps tree (editable installs, namespace shims, import
    # hooks) would silently never be processed - packages that rely on them
    # install "successfully" and crash at import. Route the child through
    # deps_boot, which site.addsitedir()s the deps dir (processing .pth) and
    # then runs the original target with an unchanged argv view. Only when
    # the child runs the GATEWAY interpreter (deps pin sys.executable; a venv
    # interpreter means no deps were provisioned) - the shim is gateway code
    # and must not be imported by a foreign interpreter.
    #
    # Shim XOR PYTHONPATH, never both: `python -m kiro_crew.apps.deps_boot`
    # resolves kiro_crew through sys.path, and a deps-provided kiro_crew copy
    # on PYTHONPATH would SHADOW the gateway's shim - app code running as the
    # "shim" on the gateway's own interpreter. A shimmed child therefore gets
    # NO deps PYTHONPATH (addsitedir supplies the deps only after the trusted
    # shim has imported); non-shimmable children (node entries - inert there,
    # and non-gateway interpreters) keep the PYTHONPATH transport.
    if _deps_ready and cmd and cmd[0] == sys.executable:
        # By ABSOLUTE PATH, not -m: the child runs with cwd=app root, and
        # an app-root kiro_crew.py (or kiro_crew/ dir) would shadow the
        # gateway package for `-m` resolution - the backend would die (or
        # run app code as the shim) before startup. deps_boot is
        # stdlib-only, so the path spelling has no import to shadow.
        # Inserted at the python LAUNCH TARGET, never blindly at argv[1]:
        # a shebang can carry interpreter flags (#!<python> -I), and a shim
        # placed before them makes deps_boot read the flag as its script
        # path. The same walk bridges uses finds the target; a shape with
        # no resolvable target keeps its launch untouched.
        from kiro_crew.apps.bridges import _py_target_index

        _ti = _py_target_index(cmd[1:])
        if _ti is not None:
            cmd = platform_compat.isolated_python_argv(
                *cmd[1 : 1 + _ti],
                str(_deps_boot_path()),
                str(_deps_dir),
                *cmd[1 + _ti :],
                executable=cmd[0],
                force_isolation=True,
            )
    elif _deps_ready and cmd and os.path.isabs(cmd[0]) and _abi_shebang_of(root, cmd[0]):
        # An EXECUTABLE python script entry (cmd[0] is the script, not an
        # interpreter): the ABI check on the script path answers no-match,
        # but its shebang can name an ABI-matched interpreter - exactly the
        # bridges stdio case. Launch through deps_boot under that
        # interpreter so the provisioned deps (and their .pth hooks) reach
        # the backend. The shared shebang reader refuses argument-bearing
        # shebangs (#!<python> -I keeps its kernel launch, flags intact)
        # and sensitive paths, so both contracts hold here by construction.
        _si = _abi_shebang_of(root, cmd[0])
        cmd = platform_compat.isolated_python_argv(
            str(_deps_boot_path()),
            str(_deps_dir),
            *cmd,
            executable=_si,
            force_isolation=True,
        )
    elif _deps_ready and path_command_is_abi_matched(root, cmd[0] if cmd else ""):
        # PYTHONPATH transport only on a POSITIVE ABI match: the deps tree
        # is built by the GATEWAY's pip, and an exec backend running a PATH
        # python of another minor version would import mismatched binary
        # wheels and die. Anything not positively matched (foreign pythons,
        # node, shell) gets no deps env at all - the pre-deps status quo.
        _existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{_deps_dir}{os.pathsep}{_existing_pp}" if _existing_pp else str(_deps_dir)
        )
        cmd = platform_compat.isolated_python_argv(
            *cmd[1:],
            executable=cmd[0],
            force_isolation=True,
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
    # The app's OWN hidden state leaves (e.g. md-notebook's vault registry, PAT, and
    # sync settings) are unmasked for exactly this spawn: the mask fences agent
    # subprocesses, but this backend is each leaf's only legitimate reader/writer, and
    # leaving the mask on breaks the app outright. Unlike the governance cache
    # these are the app's own read-write state, so the blanket "visible" meaning is the
    # correct one. Empty for every app without declared owned leaves.
    #
    # Gated on IMMUTABLE PACKAGE PROVENANCE of the code this spawn executes, not on the
    # app name alone: a trusted third-party app that claimed the name could otherwise
    # spawn with the builtin's credential leaves (the Notes PAT) unmasked. The same
    # ``execution_path`` the admission gate above vetted is what the provenance check
    # binds to, so a file-entry install under the builtin's name gets no exemption.
    _cache_visible = bool(_platform_extra.get(POLICY_CACHE_ONLY_ENV))
    _visible: tuple[str, ...] = ()
    if is_builtin_app(app_root=execution_path, app_name=app_name):
        _visible = app_backend_visible_targets(app_name)
    if _cache_visible:
        # SECURITY: refuse rather than carve when the cache sits beneath an
        # independently masked directory (a data home relocated under a credential
        # tree). ``extra_visible_dirs`` cancels any hidden mask entry that CONTAINS
        # a visible path, so carving the cache out would unmask that whole foreign
        # tree for this spawn. With the mask kept, the cache-only child fails
        # closed on the unreadable cache — strictly safer — and the guard's log
        # line names the offending ancestor so the misconfiguration is actionable.
        _cache_target = str(policy_cache_dir())
        if not carveout_shadowed_by_foreign_mask(_cache_target):
            _visible = _visible + (_cache_target,)
    sandboxed_cmd, cleanup_path = wrap_argv(cmd, mode="standard", extra_visible_dirs=_visible)
    if _cache_visible and list(sandboxed_cmd) == list(cmd):
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
        # UTF-8 with replacement, not the locale codec. A text handle opened
        # without ``encoding=`` takes the platform default, which on Windows is
        # the ANSI code page (cp1252), and the provision-error line below can
        # carry non-ASCII text -- a Unicode traceback glyph, an accented
        # install path. Under cp1252 that write raises UnicodeEncodeError and
        # the spawn aborts on the one branch whose whole point is to record
        # why provisioning failed. ``errors="replace"`` keeps the write total
        # for any codepoint; the child's own output is appended as raw bytes
        # through the inherited fd and is not affected by this wrapper.
        log_fh = open(log_path, "w", encoding="utf-8", errors="replace")
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

    # Pin the ROOT's creation identity before the survival check. On Windows a
    # launcher that spawns its real server and exits leaves a tree anchored by an
    # exited root, and the exact-handle drain locates that tree through the root's
    # (pid, creation time) -- which must be read now, while this Popen still holds
    # the process open. POSIX does not need it: the group id IS the leader's pid,
    # and its single identity probe stays in _record_app_pid on the success path.
    root_start_time = _proc_start_time(proc.pid) if platform_compat.IS_WINDOWS else None

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
            # Same codec as the write above; the child's stdout bytes follow
            # the header and may be any encoding, so decode with replacement
            # rather than letting one stray byte turn the tail into "(no output)".
            with open(log_path, "r", encoding="utf-8", errors="replace") as _lf:
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
        # The launcher is dead; whatever it forked may not be. Nothing tracks that
        # tree yet, so it is drained here or never.
        _drain_exited_root_tree(app_name, proc, root_start_time, spawn_instance)
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
        gateway_started=True,
        admitted_builtin=_admitted_builtin,
        spawn_instance=spawn_instance,
    )

    retired = False
    with _lock:
        if _spawn_owner is not None and _processes.get(app_name) is not _spawn_owner:
            retired = True
        else:
            _processes[app_name] = ap
            _allocated_ports[app_name] = port

    if retired:
        logger.info(
            "App %s backend spawn lost publication ownership; terminating pid %d",
            app_name,
            proc.pid,
        )
        _terminate_retired_spawn(app_name, proc, log_fh)
        return None

    logger.info("Started app %s backend on port %d (pid %d)", app_name, port, proc.pid)

    # Persist identity for the startup stale-reap (see _reap_stale_app_backends).
    ap.pid_start_time = _record_app_pid(app_name, proc.pid, port, spawn_instance)

    # Health check in background, then a standing liveness watch for as long as the
    # backend is tracked — see _supervise_backend_health.
    _start_health_supervisor(ap, manifest.backend.healthCheck)

    return ap


# Kept here, beside the spawn, because ``test_windows_kill_probe_audit.py`` reads it in
# this file by path; the tree drain and the stale-reap call it through
# ``backend_runtime._facade()``.
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


# ---------------------------------------------------------------------------
# Composition: one import path and one patch surface over the owners
# ---------------------------------------------------------------------------
# The backend is split by responsibility across ``backend_runtime``, and this module is
# its only import path. Callers and tests reach every name as ``backend.X`` -- private
# helpers and the process table included, because tests read, clear and patch them --
# so two properties hold.
#
# 1. A read answers with the object the owner holds. A name this module does not use
#    itself is NOT bound here: ``__getattr__`` reads it from its owner on each access,
#    through ``sys.modules``, which is the one-storage rule
#    ``test_mirrored_owner_storage.py`` enforces on every module of this shape. The
#    names this module's own functions use are bound here by ordinary imports, the
#    same way each owner binds what it imports from a lower owner.
# 2. A write reaches every binding of the name. An owner resolves a name through its
#    own globals, and so does every owner that imported it, so a patch that landed
#    only on this module would leave the code under test running the unpatched
#    object -- the test would pass while testing nothing. ``_Facade`` therefore
#    writes the value into every module that holds the name, which keeps the backend
#    one namespace for writes: replacing ``_processes`` or shadowing a builtin reaches
#    every owner as well. Patch the facade, never an owner directly: a write into one
#    owner reaches no other holder.
#
# The mutable state (``_processes``, ``_allocated_ports``, the locks, the lifecycle
# generations) is ONE object per name, defined once in its owner and imported by the
# owners that act on it, so a ``.clear()`` through the facade empties the table every
# owner reads. The two flags rebound with ``global`` each have one holder: the spawn
# body's ``_warned_unconfined_cache`` here, and ``startup._DEV_FLEET_DEFERRED``.
#
# Every owner is imported here, at the facade's own import. An owner's
# ``from ... import`` bindings are therefore taken once, as the one-module backend
# took them, and never on a first use that could fall inside a test's patch.
#
# One consequence of (1) is visible to a patch harness. ``mock.patch`` undoes a name
# this module does not bind by deleting it and then writing the original back -- and
# under ``create=True`` it only deletes -- and the delete reaches every holder. So a
# patch of such a name never passes ``create=True``: the composition-contract test
# fails on any test module that patches a forwarded name with ``create=True``, apart
# from its one allowlisted premise case.
#
# The machinery below holds dotted module NAMES, never module objects, and reads
# ``sys``, ``importlib`` and ``builtins`` through private aliases a patch of
# ``backend.sys`` cannot redirect. The composition-contract test pins that every
# module holding a name holds the SAME object, so a name and the symbol it denotes
# cannot come apart.

#: The owners, lowest layer first. An owner imports only owners earlier in this
#: order, so for a name the backend defines, the first owner holding it is its
#: definer; a name an owner imports from outside the backend resolves from its first
#: importer, which holds the same object as every other holder.
_PART_MODULES: tuple[str, ...] = tuple(
    f"{__name__.rpartition('.')[0]}.backend_runtime.{leaf}"
    for leaf in (
        "tracking",
        "probe",
        "pidfile",
        "ports",
        "provisioning",
        "termination",
        "stale_reap",
        "registration",
        "restart",
        "supervision",
        "startup",
    )
)

#: Builtin names, which a module shadows by binding them in its own namespace.
_BUILTIN_NAMES = frozenset(name for name in vars(_builtins) if not name.startswith("__"))


def _part(module: str) -> _ModuleType:
    """Return one owner, read from where modules are stored.

    :data:`sys.modules` answers first, so a purged or replaced owner is seen at once.
    ``importlib.import_module`` answers only a miss: it is an attribute any test can
    patch, and resolving every read through it would reroute this whole surface to
    that patch while it is installed.
    """
    try:
        return _sys.modules[module]
    except KeyError:
        return _importlib.import_module(module)


def _holder_tables() -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """``(exported, also_held)``: the owners holding each name, lowest layer first.

    A name this module binds itself goes to the second table, and only the owners that
    hold the SAME object count as holders of it; every other name an owner holds goes
    to the first.
    """
    own = globals()
    exported: dict[str, list[str]] = {}
    also_held: dict[str, list[str]] = {}
    for module in _PART_MODULES:
        for name, value in list(vars(_part(module)).items()):
            if name.startswith("__"):
                continue
            if name in own:
                if own[name] is value:
                    also_held.setdefault(name, []).append(module)
            else:
                exported.setdefault(name, []).append(module)
    return (
        {name: tuple(holders) for name, holders in exported.items()},
        {name: tuple(holders) for name, holders in also_held.items()},
    )


_holder_split = _holder_tables()

#: Name -> the owners that hold it, lowest layer first, for every name an owner holds
#: and this module does not bind. A read resolves the first; a write reaches them all.
_EXPORTS: dict[str, tuple[str, ...]] = _holder_split[0]

#: Name -> the owners that hold a name this module ALSO binds for its own functions.
#: A read answers from the binding here; a write reaches this module and all of them.
_ALSO_HELD: dict[str, tuple[str, ...]] = _holder_split[1]

del _holder_split


def _holders(name: str) -> tuple[str, ...]:
    """The owners a write of ``name`` through this module has to reach."""
    held = _EXPORTS.get(name) or _ALSO_HELD.get(name)
    if held is not None:
        return held
    return _PART_MODULES if name in _BUILTIN_NAMES else ()


if _typing.TYPE_CHECKING:
    # The exported names, as the type checker sees them: every one of them resolves
    # from its owner at run time through ``__getattr__`` below, which a checker is not
    # shown, so a misspelled or mis-called ``backend.X`` stays a type error. The
    # composition-contract test pins this list equal to ``_EXPORTS``.
    from kiro_crew.apps.backend_runtime.pidfile import (  # noqa: F401
        _forget_app_pid,
        _forget_app_pid_if,
        _pidfile_lock,
        _pidfile_path,
        _read_pidfile,
        _write_pidfile,
        atomic_write,
        json,
        retire_windows_app_tracking,
    )
    from kiro_crew.apps.backend_runtime.ports import (  # noqa: F401
        _PID_ANCESTRY_MAX_DEPTH,
        _PORT_PROBE_TIMEOUT,
        _SPAWN_SURVIVAL_CHECKS,
        _SPAWN_SURVIVAL_INTERVAL,
        _find_free_port,
        _listening_pids,
        _pid_is_self_or_descendant_of,
        _port_is_listening,
        _spawn_owns_listener,
        recorded_backend_port,
        spawned_backend_owns_pid,
        unstopped_backend_port,
    )
    from kiro_crew.apps.backend_runtime.probe import (  # noqa: F401
        _HEALTH_CHECK_TIMEOUT,
        _HEALTH_PATH_RE,
        _PROBE_DETAIL_MAX_CHARS,
        HealthProbeOutcome,
        _health_failure_hint,
        _health_probe,
        _health_probe_url,
        _health_warn_lock,
        _probe_failure_detail,
        _warn_bad_health_path,
        _warned_health_paths,
        http,
        loopback_urlopen,
        urllib,
    )
    from kiro_crew.apps.backend_runtime.provisioning import (  # noqa: F401
        _DEPS_ABI_NAME,
        _DEPS_PIP_STDERR_TAIL,
        _DEPS_PRIOR_NAME,
        _DEPS_REQ_MAX_BYTES,
        _DEPS_SPILL_HARD_CAP,
        _DEPS_STAGING_NAME,
        _DEPS_STAGING_SWEEP_RE,
        _DEPS_STAMP_MAX_BYTES,
        _DEPS_STAMP_NAME,
        _audit_provision_failure,
        _capped_spill,
        _default_marker_environment,
        _deps_abi_tag,
        _deps_digest,
        _open_contained_nofollow,
        _pinned_ancestors,
        _pinned_remove_entry,
        _PinnedDir,
        _provision_app_deps_locked,
        _requirements_volatile,
        _write_staging_marker,
        pinned_fs,
        platform,
        redact_credentials,
        redact_exfiltration_urls,
        stat,
        sysconfig,
        tempfile,
    )
    from kiro_crew.apps.backend_runtime.registration import (  # noqa: F401
        _app_enabled_state,
        _demote,
        _drop_disabled_app_resources,
        _gate_mcp_registration,
        _promote,
        _retry_mcp_reconcile,
        _undo_promotion_of_disabled_app,
        app_enabled_state,
    )
    from kiro_crew.apps.backend_runtime.restart import (  # noqa: F401
        _RESTART_ON_EXIT_FAST_ATTEMPTS,
        _RESTART_ON_EXIT_INITIAL_DELAY,
        _RESTART_ON_EXIT_MAX_DELAY,
        _RESTART_STEADY_INTERVAL,
        _SETTLE_UNRESOLVED_WARN_AFTER,
        GOVERNANCE_ERROR_REASON,
        ActivationVerdict,
        Literal,
        PlatformCompositionError,
        _activation_denied,
        _app_activation_denied,
        _BackendShutdownEvent,
        _gateway_shutdown_event,
        _read_installed,
        _restart_exited_backend,
        _settle_superseding_start,
        app_admission_denied,
        shutdown_event,
    )
    from kiro_crew.apps.backend_runtime.stale_reap import (  # noqa: F401
        _reap_orphaned_backend_group,
        _reap_stale_app_backends,
    )
    from kiro_crew.apps.backend_runtime.startup import (  # noqa: F401
        _BOOT_SPAWN_MAX_WORKERS,
        _DEV_FLEET_DEFERRED,
        _preclaim_fixed_ports,
        _start_backends_concurrently,
        concurrent,
        list_apps,
        shipped_builtin_app_root,
        start_deferred_app_backends,
        start_enabled_app_backends,
    )
    from kiro_crew.apps.backend_runtime.supervision import (  # noqa: F401
        _HEALTH_CHECK_INTERVAL,
        _HEALTH_CHECK_RETRIES,
        _HEALTH_WATCH_FAILURES,
        _HEALTH_WATCH_INTERVAL,
        _RESTART_STABLE_SWEEPS,
        _health_check_loop,
        _rebind_adopted_owners,
        _revoke_if_ceiling_closed,
        _supervise_backend_health,
        _watch_backend_health,
        _watch_backend_health_sweeps,
        third_party_ceiling_closed,
    )
    from kiro_crew.apps.backend_runtime.termination import (  # noqa: F401
        _REAP_POLL_INTERVAL,
        _REAP_SIGTERM_GRACE,
        _facade,
        _signal_backend_tree,
        _wait_for_pids,
        group_vouching_available,
        signal_orphaned_spawn_group,
        stop_app_backend,
    )
    from kiro_crew.apps.backend_runtime.tracking import (  # noqa: F401
        _FACADE,
        _LIFECYCLE_STOP,
        ContextVar,
        Iterator,
        _lifecycle_generation,
        _restart_attempts,
        contextlib,
        dataclass,
        field,
        get_app_backend_port,
        get_app_process,
        health_reconcile_lock,
        list_app_processes,
        re,
        spawned_backend_names,
        threading,
    )
else:

    def __getattr__(name: str) -> Any:
        """Read an exported name from the owner that holds it (:pep:`562`)."""
        holders = _EXPORTS.get(name)
        if holders is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        return getattr(_part(holders[0]), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


class _Facade(_ModuleType):
    """Write a name into every module that holds it.

    An exported name is written to its owners only, so this module never holds a copy
    that would shadow the owner and go stale on the owner's next write. ``monkeypatch``
    and ``mock.patch`` restore by writing the remembered value back through here, so a
    patch and its undo reach the same bindings.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        for module in _holders(name):
            setattr(_part(module), name, value)
        if name not in _EXPORTS:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        for module in _holders(name):
            part = _part(module)
            if name in vars(part):
                delattr(part, name)
        if name not in _EXPORTS:
            super().__delattr__(name)


# ``from ... import *`` consults this list and never reaches ``__getattr__``, so it
# is derived to carry the public names a star import of the one-module backend would:
# the names bound here plus the exported ones, minus the private names a star import
# never carries.
__all__ = sorted(name for name in set(globals()) | set(_EXPORTS) if not name.startswith("_"))

# Installed last, so the forwarding is live for every caller but never runs while this
# module is still binding its own names.
_sys.modules[__name__].__class__ = _Facade
