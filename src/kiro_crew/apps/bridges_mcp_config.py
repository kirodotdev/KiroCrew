"""Kiro Crew's own agent config (``kirocrew.json``), where app MCP servers are written.

Its path, the file lock every reader and writer takes (``_mcp_lock``), the unlocked
reads and writes made under that lock, and the guard that serializes a writer against
backend health transitions (``_health_reconcile_guard``).

Part of the ``kiro_crew.apps.bridges`` family: callers and tests reach every name here
as ``kiro_crew.apps.bridges.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import json
import logging
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from kiro_crew import platform_compat
from kiro_crew.apps.bridges_sources import _kiro_agents_dir
from kiro_crew.atomic_write import atomic_write
from kiro_crew.user_json import loads_mcp_config

logger = logging.getLogger("kiro_crew.apps.bridges")


# ---------------------------------------------------------------------------
# MCP server registration
# ---------------------------------------------------------------------------


# App MCP servers are written to Kiro Crew's OWN agent config, not to the shared
# ~/.kiro/settings/mcp.json. That shared file is read by everything else living
# under ~/.kiro — Kiro IDE and any other kiro-cli agent — so registering an app's
# tools there leaks them into surfaces that never installed the app (and a dead
# HTTP entry there breaks EVERY kiro session, see the boot-reconcile warning in
# backend_runtime/startup.py). Kiro Crew sessions read only the agent config
# (``includeMcpJson`` pinned False in agent.py), so this is both sufficient and
# correctly scoped.
def _mcp_json_path() -> Path:
    """Kiro Crew's own agent config. A function, not an import-time constant:
    the path must track the live data home, and freezing it at import would
    write to the real ``~/.kiro`` from an isolated run. Goes through
    :func:`_kiro_agents_dir` so the module-level override honoured by every
    other agent-dir caller applies here too."""
    return _kiro_agents_dir() / "kirocrew.json"


@contextmanager
def _mcp_lock(*, exclusive: bool = True, target: Optional[Path] = None) -> Iterator[None]:
    """Acquire a lock on an mcp/config file for the duration of the block.

    Uses a single ``.lock`` sidecar file for both shared and exclusive
    locks so that readers and writers coordinate properly. ``target`` selects
    WHICH file's sidecar to lock (default: Kiro Crew's own agent config); pass the
    legacy shared ``mcp.json`` so its read-modify-write serializes against any
    other writer of THAT file, which sits under a different sidecar.

    Both failure modes are REPORTED here before they propagate, mirroring
    :func:`kiro_crew.agent.agents_spec_lock` — this sidecar's neighbour in
    ``~/.kiro/agents`` — because the callers that catch this treat it as
    best-effort work at a level no operator reads:
    :func:`registered_app_mcp_servers` returns ``{}`` on ANY exception and logs
    nothing at all, and the boot path reaches here through
    ``agent._install_worker_agent``, whose failure is caught at
    ``logger.debug``. Without a report at a visible level a gateway that skipped
    its agent-config write reads in the log exactly like one that completed it,
    which is the silence reported in GH-11474. An unwritable lock path (a
    read-only ``~/.kiro/agents`` mount) refuses when the sidecar is opened,
    BEFORE any lock is attempted; ``platform_compat.file_lock`` bounds the
    acquire itself, so neither failure mode can present as a hang.
    """
    base = target if target is not None else _mcp_json_path()
    lock_path = base.with_suffix(".lock")
    with ExitStack() as stack:
        try:
            base.parent.mkdir(parents=True, exist_ok=True)
            # ONE create-or-open syscall instead of touch() + open("r+"): it
            # never truncates, it keeps the fd WRITABLE — Windows
            # msvcrt.locking fails EACCES on a read-only handle, which
            # file_lock would swallow, silently degrading this to a no-op and
            # letting concurrent writers race the atomic mcp.json rename — and
            # it leaves the unwritable-path refusal ONE place to be reported
            # from rather than two. See platform_compat.open_lock_file.
            fd = stack.enter_context(platform_compat.open_lock_file(lock_path))
        except OSError as exc:
            # Naming the path AND the errno is the point: "Read-only file
            # system" on this specific path is what tells the operator what to
            # change, and it is not something retrying can recover.
            #
            # The KIRO_HOME remedy is true only for the DEFAULT sidecar. An
            # explicit ``target`` -- the legacy shared ``mcp.json`` -- resolves
            # from a fixed ``Path.home()`` that ignores KIRO_HOME, so moving it
            # cannot move that lock, and printing the remedy there would send an
            # operator to a setting that changes nothing. Naming the config the
            # lock guards is what stays true for both.
            remedy = (
                " Point KIRO_HOME at a writable directory if the filesystem is read-only."
                if target is None
                else ""
            )
            logger.warning(
                "cannot open the mcp config lock %s (%s) -- writes to %s cannot be "
                "serialized, so this update is being skipped.%s",
                lock_path,
                exc.strerror or exc,
                base,
                remedy,
            )
            raise
        # ``enter_context`` rather than a ``with`` around the yield, so the
        # ``except`` below covers the ACQUIRE ALONE. A caller-body OSError (an
        # atomic mcp.json write hitting ENOSPC, a legacy scrub hitting EACCES)
        # reaches the same handler if the yield sits inside it, and would then
        # be logged as a lock problem — sending an operator after a stuck holder
        # while the real fault is the disk or the permission.
        try:
            stack.enter_context(platform_compat.file_lock(fd, exclusive=exclusive))
        except OSError as exc:
            # A stuck holder calls for a DIFFERENT operator action (find the
            # process still holding it) than an unwritable path, so this must
            # not carry the same remedy as above. No BlockingIOError case: this
            # acquire is always a WAITING one, so a refusal here is the bounded
            # ceiling and never a caller's own "do not wait" choice. A future
            # caller that wants ``wait=False`` has to separate the two, the way
            # ``agent.agents_spec_lock`` does.
            logger.warning("agent-config lock %s: %s", lock_path, exc)
            raise
        yield


def _read_mcp_json_unlocked(*, strict: bool = False) -> dict[str, Any]:
    """Read mcp.json without acquiring a lock (caller must hold lock).

    ``strict`` is for read-MODIFY-write callers: an existing-but-unreadable
    config must NOT be treated as empty and overwritten — that drops the agent's
    entire configuration (tools, allowedTools, prompt, every other server). With
    ``strict=True`` a parse/OS error on a PRESENT file propagates so the writer
    aborts without persisting; a genuinely MISSING file is still an empty map.
    Read-only callers keep the lenient default (degrade to ``{}``).
    """
    if not _mcp_json_path().is_file():
        return {}
    try:
        return loads_mcp_config(_mcp_json_path().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        if strict:
            raise
        logger.warning("Failed to read mcp.json: %s", exc)
        return {}


def _write_mcp_json_unlocked(data: dict[str, Any]) -> None:
    """Write mcp.json without acquiring a lock (caller must hold lock)."""
    _mcp_json_path().parent.mkdir(parents=True, exist_ok=True)
    atomic_write(_mcp_json_path(), json.dumps(data, indent=2) + "\n")


def _read_mcp_json() -> dict[str, Any]:
    """Read mcp.json with a shared lock."""
    with _mcp_lock(exclusive=False):
        return _read_mcp_json_unlocked()


def _health_reconcile_guard() -> Any:
    """Serialize this writer against backend health transitions.

    An app's mcp.json entries and its materialized agent configs are written by two
    independent families: the lifecycle paths here (enable, update, boot reconcile) and
    the backend's health watch. Without a shared lock the two can interleave their
    decisions — each doing a correct read-modify-write, with the STALE one landing last.

    Deferred import: backend imports this module in its boot path, so resolving the lock
    at call time is what keeps the bridges <-> backend cycle from closing at import.
    """
    from kiro_crew.apps.backend import health_reconcile_lock

    return health_reconcile_lock()
