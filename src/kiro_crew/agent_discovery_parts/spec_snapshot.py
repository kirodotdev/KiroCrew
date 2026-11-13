"""The stat-only signatures of the agents directories, and the caches they validate.

The ``list_agents`` result cache, the dispatchable project-name index and the parsed-spec
snapshot behind the skill globs, each revalidated by a directory signature, and
``clear_list_agents_cache``, the one clear that drops the result cache and the snapshot
and bumps the generation spec-derived caches elsewhere key on.

:mod:`kiro_crew.agent_discovery` stays the import path and the patch surface; see
:mod:`kiro_crew.agent_discovery_parts`.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import threading
from pathlib import Path
from typing import Any, Iterator

from kiro_crew.agent_discovery_parts.spec_fields import AgentInfo
from kiro_crew.agent_discovery_parts.spec_reads import _kiro_agents_dir, _read_agent_spec
from kiro_crew.agent_spec_format import (
    is_agent_spec_name,
    is_native_skill_alias_name,
    iter_agent_spec_files,
)
from kiro_crew.executors import discovery_executor

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.agent_discovery")


# list_agents() result cache: an uncached parse of 100+ agent files on every hot call
# blocks the event loop, so the result is reused while the stat-only signature holds.
# The key carries BOTH scopes: sessions on different checkouts must not share an entry.
_ListAgentsSig = tuple[tuple[str, int], ...]
_LIST_AGENTS_KEY = tuple[str, str]
_LIST_AGENTS_CACHE: dict[_LIST_AGENTS_KEY, tuple[tuple[_ListAgentsSig, ...], list[AgentInfo]]] = {}

# Dispatchable project agent NAMES, keyed by project dir and revalidated by the same
# stat-only signature idea. Separate from the cache above because the per-turn
# resolver needs only the name set: building full AgentInfo rows (and scanning the
# user-level dir alongside) on every turn is the cost this index exists to avoid.
_PROJECT_NAMES_CACHE: dict[str, tuple[tuple[_ListAgentsSig, ...], frozenset[str]]] = {}

# Parsed agent SPECS (raw dict + original path), keyed by directory and
# revalidated by the same stat-only signature. This is the shared snapshot
# behind :func:`agent_skill_globs` and the dashboard's ``loaded_by_agents``
# annotation: both re-read every spec per call without it, and the parse is
# their dominant cost. Guarded by a lock — callers run on executor threads.
# Rows are shared, never copied: treat ``(data, path)`` tuples and the
# ``data`` dicts as read-only.
_PARSED_SPECS_LOCK = threading.Lock()
_PARSED_SPECS_CACHE: dict[str, tuple[_ListAgentsSig, list[tuple[dict[str, Any], Path]]]] = {}
_PARSED_SPECS_REFRESHING: set[str] = set()  # Guarded by _PARSED_SPECS_LOCK.
# Bumped by clear_list_agents_cache() under the lock. A parse snapshot records
# the generation it started under and is discarded instead of stored when a
# clear landed meanwhile — otherwise an in-flight parse could re-publish rows
# read BEFORE the write that the clear announced, inside one mtime tick where
# the signature cannot tell the difference.
_PARSED_SPECS_GEN = 0


def spec_cache_generation() -> int:
    """Return the generation of in-process agent-spec content."""
    with _PARSED_SPECS_LOCK:
        return _PARSED_SPECS_GEN


def parsed_agent_specs(
    agents_dir: Path | None = None,
    *,
    operation: str,
    source: str,
) -> list[tuple[dict[str, Any], Path]]:
    """Return every parsed agent spec in *agents_dir* as ``(data, path)`` pairs.

    ``path`` is the ORIGINAL file path (not the resolved target), in sorted
    filename order, so ``path.stem`` matching and relative ``skill://`` globs
    anchor as a direct scan would. Reads go through :func:`_read_agent_spec`.

    Cached per directory and revalidated by :func:`_dir_signature`;
    :func:`clear_list_agents_cache` covers a write inside one mtime tick. The
    list is a copy but its rows are cached objects: treat them as read-only.

    *operation*/*source* label the SEL denial trail as :func:`_read_agent_spec`
    documents; within one snapshot the first reader's labels win, which is fine
    because every caller reads the same directory for the same catalog purpose.
    """
    d = agents_dir or _kiro_agents_dir()
    key = str(d)
    signature = _dir_signature(d)
    with _PARSED_SPECS_LOCK:
        cached = _PARSED_SPECS_CACHE.get(key)
        gen = _PARSED_SPECS_GEN
    if cached is not None and cached[0] == signature:
        return list(cached[1])
    # Parse OUTSIDE the lock: the lock guards only dict reads/writes, so an
    # event-loop caller of clear_list_agents_cache() can never block behind a
    # worker thread's disk scan. Two threads missing at once parse redundantly
    # and last-write-wins — the same rows, from the same signature-checked
    # directory state, so the duplicate work is bounded and harmless.
    try:
        candidates = iter_agent_spec_files(d)
    except OSError:
        candidates = []
    rows: list[tuple[dict[str, Any], Path]] = []
    for f in candidates:
        data = _read_agent_spec(f, operation=operation, source=source)
        if data is None:
            continue
        rows.append((data, f))
    with _PARSED_SPECS_LOCK:
        # A clear() that landed during the parse announced a write this scan
        # may predate; serve these rows to this caller but do not publish them.
        if _PARSED_SPECS_GEN == gen:
            _PARSED_SPECS_CACHE[key] = (signature, rows)
    return list(rows)


def cached_agent_specs(
    agents_dir: Path | None = None,
    *,
    operation: str,
    source: str,
) -> list[tuple[dict[str, Any], Path]]:
    """Return cached specs without filesystem calls on the caller's thread.

    The caller only reads the snapshot dict; every scandir, stat and parse runs
    on ``mc-discovery``. A cold, just-cleared or changed snapshot serves the
    previous rows (or none) until the worker refresh lands. A loop-thread model
    lookup therefore briefly degrades rather than blocking on filesystem I/O or
    queuing a full parse behind ``mc-pathres`` and triggering the watchdog exit.
    Off-loop callers needing current rows should use :func:`parsed_agent_specs`
    directly.

    Returned lists are copies; their rows remain read-only. Revalidations
    preserve the caller's *operation*/*source* audit labels, with at most one
    in-flight revalidation per directory, even across cache invalidation. A
    warm worker revalidation scans the signature without parsing specs again.
    """
    d = agents_dir or _kiro_agents_dir()
    key = str(d)
    with _PARSED_SPECS_LOCK:
        cached = _PARSED_SPECS_CACHE.get(key)
        rows = list(cached[1]) if cached is not None else []
        if key in _PARSED_SPECS_REFRESHING:
            return rows
        _PARSED_SPECS_REFRESHING.add(key)

    def refresh() -> None:
        try:
            parsed_agent_specs(d, operation=operation, source=source)
        except Exception:
            # The future is fire-and-forget, so nothing else surfaces this:
            # without the log a persistent parse failure degrades silently.
            logger.warning("agent spec snapshot refresh failed for %s", d, exc_info=True)
        finally:
            with _PARSED_SPECS_LOCK:
                _PARSED_SPECS_REFRESHING.discard(key)

    try:
        discovery_executor().submit(refresh)
    except RuntimeError:  # The executor is shutting down; leave the lookup degraded.
        with _PARSED_SPECS_LOCK:
            _PARSED_SPECS_REFRESHING.discard(key)
    return rows


async def warm_agent_specs(
    agents_dir: Path | None = None,
    *,
    operation: str = "warm_agent_specs",
    source: str = "unknown",
) -> None:
    """Publish the :func:`parsed_agent_specs` snapshot of *agents_dir* from the discovery pool.

    The counterpart to :func:`cached_agent_specs`: an async caller awaits this once
    so the on-loop lookup that follows (``_resolve_named_agent_model``) is a snapshot
    HIT. Only the scan runs on ``mc-discovery``; the lookup stays inline, for the
    reason :func:`warm_project_agent_names` gives. One parse, no retry: a clear
    landing mid-parse leaves the rows unpublished, and the lookup's own refresh
    repairs it.

    *operation*/*source* label the SEL denial trail as :func:`_read_agent_spec`
    documents. Never raises: a failed warm only costs the cold answer.
    """
    d = agents_dir or _kiro_agents_dir()
    try:
        await asyncio.get_running_loop().run_in_executor(
            discovery_executor(),
            functools.partial(parsed_agent_specs, d, operation=operation, source=source),
        )
    except Exception:  # noqa: BLE001 — a warm failure only costs the cold answer
        logger.debug("Failed to warm the agent spec snapshot for %s", d, exc_info=True)


def _iter_spec_entries(d: Path) -> Iterator[os.DirEntry[str]]:
    """The one ``scandir`` walk behind the stat-only fingerprints of an agents dir.

    Yields every entry with a spec suffix that is not a skill-view alias, the
    roster's own rules (:func:`iter_agent_spec_files`), so the fingerprint is a
    superset of the roster. The suffix match is case-insensitive because a
    case-insensitive filesystem serves ``Foo.JSON`` to the scans. Aliases are
    dropped by name before any ``stat``: counting them would invalidate on writes
    the roster cannot see, and thousands of alias stats stall the event loop.
    A directory that cannot be listed raises the ``OSError``.
    """
    with os.scandir(d) as it:
        for entry in it:
            if is_agent_spec_name(entry.name) and not is_native_skill_alias_name(entry.name):
                yield entry


def _dir_signature(d: Path) -> _ListAgentsSig:
    """Cheap stat-only signature of the agents dir.

    Each spec entry's name and mtime (JSON and markdown), without reading any
    file: enough to catch adds, removals, edits and renames (a rename changes
    the stem-derived name and relative ``skill://`` anchoring). An edit inside
    one mtime tick is invisible; write paths call :func:`clear_list_agents_cache`.

    Always a tuple, never ``None``. :func:`agents_dir_revision` is the stricter
    fingerprint, for answers that must never be served stale.
    """
    entries: list[tuple[str, int]] = []
    try:
        for entry in _iter_spec_entries(d):
            try:
                m = entry.stat().st_mtime_ns
            except OSError:
                m = 0
            entries.append((entry.name, m))
    except OSError:
        pass
    return tuple(sorted(entries))


def clear_list_agents_cache() -> None:
    """Drop the :func:`list_agents` result cache and the :func:`parsed_agent_specs`
    snapshot (forces a fresh scan next call).

    One invalidation point for those two caches, so the agent write paths that
    already call this also cover a write landing inside one mtime tick. The
    project-names cache is untouched: it revalidates purely by directory
    signature and holds only name sets, never parsed spec content.
    Invalidation is normally automatic via the directory signature; call this
    only to force an immediate refresh (e.g. right after writing an agent
    file).

    The generation invalidates spec-derived caches in other modules.
    """
    _LIST_AGENTS_CACHE.clear()
    global _PARSED_SPECS_GEN
    with _PARSED_SPECS_LOCK:
        _PARSED_SPECS_CACHE.clear()
        _PARSED_SPECS_GEN += 1
