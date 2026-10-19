"""The private generation tree: each helper process's project scope, its owner marker, its lifetime.

Each helper process gets one random, owner-only directory under
``<data home>/run/connections-warm/generations``. It is the runtime's cwd, so both the initial
``--agent`` and every later ``session/set_mode`` resolve that directory's ``.kiro/agents`` before
the user's global scope. A PID-reuse-resistant owner marker is written before the spawn -- with
``runtime_pid: 0``, provisional -- and completed with the child's identity once the spawn returns
(:func:`_bind_warm_generation`). Parking or a failed kill keeps the directory attached; only a
proven death, or a spawn that never produced a process (:func:`_spawn_left_no_process`), releases
it (:func:`_release_runtime_generation`).

The marker's constants live here, the size bound the reader applies included. The marker READER
and the plain-directory screen stay in the facade (``warm._read_warm_generation_owner``,
``warm._is_plain_warm_generation_dir``), with the death verdict and the boot scavenger built on
them (``warm._recorded_runtime_is_dead``, ``warm._scavenge_warm_generation_dirs``): the
link-screen baseline and the composition contract pin those to the facade's file. This module
reads the reader, the screen and the death verdict, and the patchable ``data_home``, through the
facade at call time (:func:`_warm_facade`).
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

from kiro_crew import agent as _agent
from kiro_crew import platform_compat
from kiro_crew.config.paths import project_agents_dir
from kiro_crew.connections.warm_runtime.start_identity import _marker_process_start_id

logger = logging.getLogger("kiro_crew.connections.warm")


def _warm_facade() -> ModuleType:
    """The warm facade, resolved per call because it holds the names this module reads late.

    Imported here rather than at module scope because the facade imports this module: a
    top-level import would make a cold import of either module circular.
    """
    from kiro_crew.connections import warm  # circular import: the facade composes this module

    return warm


#: Each helper process resolves its internal modes from one private project scope. The
#: directory name is random rather than a provider or agent name, so no stable internal
#: mode appears under the user's global ``~/.kiro/agents`` scope.
_WARM_GENERATION_PREFIX = "generation-"
_WARM_GENERATION_MARKER = ".owner.json"
_WARM_GENERATION_SENTINEL = "kirocrew.connections.warm.generation"
_WARM_GENERATION_MARKER_VERSION = 1
_WARM_GENERATION_MARKER_MAX_BYTES = 4096
_WARM_RUNTIME_DIR_ATTR = "_kirocrew_warm_generation_dir"


def _warm_work_dir() -> Path:
    """Managed root for private warm generations, inside the protected run tree.

    Specs never live directly here. Each process gets one direct child under
    :func:`_warm_generations_dir`, and that child becomes its cwd so kiro-cli resolves
    ``<cwd>/.kiro/agents`` before the user's global agent directory.
    """
    return _warm_facade().data_home() / "run" / "connections-warm"


def _warm_generations_dir() -> Path:
    """Root whose direct children are independently owned helper generations."""
    return _warm_work_dir() / "generations"


def _warm_generation_agents_dir(work_dir: Path) -> Path:
    """The project-local agent scope kiro-cli resolves for one helper process."""
    return project_agents_dir(work_dir)


def _warm_generation_marker_path(work_dir: Path) -> Path:
    return work_dir / _WARM_GENERATION_MARKER


def _warm_generation_owner(runtime: Any | None = None) -> dict[str, Any]:
    """Ownership record for one generation, with PID-reuse-resistant identities.

    Both identities are taken from ``_marker_process_start_id`` because that is
    the helper ``_process_identity_live`` compares them against on read. The
    neighbouring ``own_process_start_time`` answers a deliberately different,
    reboot-unique format -- start ticks joined to the boot UUID on Linux, a
    ``proc_pidinfo`` microtime on macOS -- so a gateway token taken from it
    could never equal what the reader recomputes, and the live gateway would
    read as dead. Scavenging removes a generation only once BOTH identities are
    proven dead, so that mismatch would not merely lose a signal: it would
    forfeit the gateway's veto and delete a directory still in use the moment its
    short-lived runtime exited.
    """
    gateway_pid = os.getpid()
    runtime_pid = int(getattr(runtime, "pid", 0) or 0)
    runtime_started = _marker_process_start_id(runtime_pid) if runtime_pid > 0 else None
    return {
        "sentinel": _WARM_GENERATION_SENTINEL,
        "version": _WARM_GENERATION_MARKER_VERSION,
        "gateway_pid": gateway_pid,
        "gateway_started": _marker_process_start_id(gateway_pid) or "",
        "runtime_pid": runtime_pid,
        "runtime_started": runtime_started or "",
    }


def _write_warm_generation_owner(work_dir: Path, runtime: Any | None = None) -> None:
    """Atomically publish the process identities that make boot scavenging safe."""
    _agent._atomic_json_write(
        _warm_generation_marker_path(work_dir), _warm_generation_owner(runtime)
    )


def _remove_warm_generation_dir(work_dir: Path) -> bool:
    """Remove one sentinel-owned generation tree without following a link.

    ``False`` means ownership or removal could not be proved. That direction leaves clutter
    but never deletes an arbitrary run-tree entry.
    """
    try:
        work_dir.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not _warm_facade()._is_plain_warm_generation_dir(work_dir):
        return False
    if _warm_facade()._read_warm_generation_owner(work_dir) is None:
        return False
    try:
        shutil.rmtree(work_dir)
    except OSError:
        logger.debug("warm generation removal failed for %s", work_dir.name, exc_info=True)
        return False
    return True


def _create_warm_generation_dir() -> Path:
    """Create one owner-only project root and its private ``.kiro/agents`` scope.

    ``make_owner_only_dir``'s tighten step is best-effort by contract, so it alone cannot
    carry this module's owner-only guarantee: on a filesystem where the ACL write fails the
    directory would exist but be open to other local users, and the agent specs written into
    it are injectable. Each level is therefore re-tightened with the fail-loud
    :func:`platform_compat.restrict_dir_to_owner` — a refused ACL raises, the cleanup below
    removes the loose directory, and the caller gets an error instead of a false private
    scope.
    """
    generations = _warm_generations_dir()
    platform_compat.make_owner_only_dir(generations)
    platform_compat.restrict_dir_to_owner(generations)
    work_dir = generations / f"{_WARM_GENERATION_PREFIX}{uuid.uuid4().hex}"
    try:
        platform_compat.make_owner_only_dir(work_dir)
        platform_compat.restrict_dir_to_owner(work_dir)
        platform_compat.make_owner_only_dir(work_dir / ".kiro")
        platform_compat.restrict_dir_to_owner(work_dir / ".kiro")
        agents_dir = _warm_generation_agents_dir(work_dir)
        platform_compat.make_owner_only_dir(agents_dir)
        platform_compat.restrict_dir_to_owner(agents_dir)
        _write_warm_generation_owner(work_dir)
    except BaseException:
        # The path was generated in this call and has not been handed to a process. Cleanup
        # may therefore remove it without the marker proof normal retirement requires.
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    return work_dir


def _bind_warm_generation(runtime: Any, work_dir: Path) -> None:
    """Attach a spawned process to its directory and publish its runtime identity."""
    setattr(runtime, _WARM_RUNTIME_DIR_ATTR, str(work_dir))
    _write_warm_generation_owner(work_dir, runtime)


def _runtime_generation_dir(runtime: Any) -> Path | None:
    raw = getattr(runtime, _WARM_RUNTIME_DIR_ATTR, "")
    return Path(raw) if raw else None


def _spawn_left_no_process(runtime: Any, work_dir: Path) -> bool:
    """Whether this tree's marker never named a process and no process exists to name it.

    True only for the PROVISIONAL marker :func:`_create_warm_generation_dir` writes before a
    spawn, on a runtime that never reached a PID -- the state a spawn refused outright leaves
    behind. Both halves are required: the marker says no identity was ever published, and the
    runtime says there is no child that could be reading the tree.
    """
    if int(getattr(runtime, "pid", 0) or 0) > 0:
        return False
    owner = _warm_facade()._read_warm_generation_owner(work_dir)
    if owner is None:
        return False
    try:
        return int(owner.get("runtime_pid") or 0) <= 0
    except (TypeError, ValueError):
        return False


def _release_runtime_generation(runtime: Any) -> bool:
    """Release a killed process's generation tree, once that death is actually proven.

    ``_kill_quietly`` answering ``True`` is not that proof. ``AcpRuntime`` reports a survivor
    by LOGGING one and returning normally -- both of its ``kill_process_tree`` calls swallow
    ``OSError`` by design, so a signal-delivery failure (EPERM through a launcher wrapper,
    pgid drift) is indistinguishable from success at that layer, and it deliberately leaves
    the PID tracked for a sweep rather than raising. Removing on the caller's word alone
    would therefore ``rmtree`` the cwd and private agent scope of a process still reading
    them, which is the one loss this whole ownership marker exists to prevent.

    A marker that never named a process is the one case that proof cannot judge, and it is
    accepted through :func:`_spawn_left_no_process` instead. :func:`_bind_warm_generation`
    publishes the runtime identity only after ``spawn()`` returns, so an attempt that failed
    earlier keeps the provisional ``runtime_pid: 0`` marker -- which
    :func:`_recorded_runtime_is_dead` and the scavenger's :func:`_process_identity_live` both
    read as UNPROVED, so neither ever released the tree and every failed spawn left another
    one behind. A runtime with no PID never had a child to read that tree, which is the same
    evidence the ``runtime is None`` branch of
    :meth:`_WarmMintRuntime._abandon_spawn_locked` already removes on. One that does carry a
    PID still needs the identity proof: a spawn that failed after its child started may leave
    it running.

    ``False`` leaves the tree attached. Startup scavenging reclaims it once both recorded
    identities are dead, so an unkillable child costs clutter until the next gateway start
    instead of costing the running process its specs.
    """
    work_dir = _runtime_generation_dir(runtime)
    if work_dir is None:
        return True
    if not _warm_facade()._recorded_runtime_is_dead(work_dir) and not _spawn_left_no_process(
        runtime, work_dir
    ):
        return False
    removed = _remove_warm_generation_dir(work_dir)
    if removed:
        try:
            delattr(runtime, _WARM_RUNTIME_DIR_ATTR)
        except AttributeError:
            pass
    return removed
