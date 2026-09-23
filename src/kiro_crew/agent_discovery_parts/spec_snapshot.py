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
import itertools
import logging
import os
import threading
from pathlib import Path
from typing import Any, Iterable, Iterator

from kiro_crew.agent_discovery_parts.spec_fields import AgentInfo
from kiro_crew.agent_discovery_parts.spec_reads import (
    _DIR_ID_DEV_ROW,
    _DIR_ID_INO_ROW,
    _DirIdentity,
    _kiro_agents_dir,
    _opened_dir_identity,
    _read_agent_spec,
)
from kiro_crew.agent_spec_format import (
    is_agent_spec_name,
    is_native_skill_alias_name,
    iter_agent_spec_files,
)
from kiro_crew.executors import discovery_executor
from kiro_crew.hooks import validate_file_path

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.agent_discovery")


# ── list_agents() result cache ──
# list_agents() reads and JSON-parses every ~/.kiro/agents/*.json on each call.
# Hot callers (agent picker, per-turn agent resolution) call it repeatedly, so an
# uncached scan over 100+ AIM-installed agent files blocks the asyncio event loop.
# Cache the parsed result keyed by directory and reuse it while a cheap stat-only
# directory signature (file count + newest mtime) is unchanged — that signature
# detects adds, removals, and in-place edits.
#
# The key carries BOTH scopes because the project scope varies per session: two
# sessions on different checkouts must not serve each other's agents from one
# entry. The signature is the pair of per-directory signatures, so an edit in
# either scope invalidates.
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
    filename order, so ``path.stem`` matching and relative ``skill://`` glob
    anchoring behave exactly as a direct scan would. Reads go through
    :func:`_read_agent_spec`, so sidecars, symlink loops, sensitive targets,
    oversized files, and invalid JSON are skipped best-effort.

    Cached per directory and revalidated by :func:`_dir_signature`, so a warm
    call costs one ``scandir`` instead of parsing every spec. The cache is
    also dropped by :func:`clear_list_agents_cache` — the agent write paths
    already call it, which covers a write landing inside one mtime tick. The
    returned list is a fresh copy but its rows are the cached objects: treat
    them as read-only.

    *operation*/*source* label the SEL denial trail exactly as
    :func:`_read_agent_spec` documents; the cache makes the labels
    first-reader-wins for the lifetime of one snapshot, which is acceptable
    because every current caller reads the same user-level directory for the
    same catalog purpose.
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

    The counterpart to :func:`cached_agent_specs`, as :func:`warm_project_agent_names`
    is to :func:`cached_project_agent_names`: an async caller awaits this once, and
    the synchronous, on-loop lookup that follows (``_resolve_named_agent_model``
    through the provider factory) is a snapshot HIT instead of the cold "no rows
    yet" answer. Only the scan is offloaded: every ``scandir``, stat and parse runs
    on ``mc-discovery`` and nothing touches the filesystem on the caller's thread,
    while the lookup itself stays inline, for the reason
    :func:`warm_project_agent_names` gives.

    One parse, awaited; no polling and no retry. A snapshot that is already warm
    costs one ``scandir`` here and no parses. A :func:`clear_list_agents_cache`
    landing during the parse keeps its rows unpublished (the generation guard), so
    the lookup then degrades to the cold answer exactly as a concurrent agent write
    does today; the worker refresh that lookup schedules repairs it.

    *operation*/*source* label the SEL denial trail as :func:`_read_agent_spec`
    documents; the defaults name this hop truthfully. Best-effort and never raises:
    failing to warm costs the caller the cold answer and must not break what it is
    starting.
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

    Yields every entry with a recognised spec suffix that is not a managed
    skill-view alias, and nothing else; the caller decides what to ``stat`` and
    how. Both rules are the roster's own (:func:`iter_agent_spec_files`); the one
    entry a fingerprint built here keeps and the roster drops is a Markdown spec
    shadowed by its JSON twin, so the fingerprint is a superset of the roster,
    never less.
    Case-insensitive on the suffix: a case-insensitive filesystem serves
    ``Foo.JSON`` to ``glob("*.json")`` consumers, so a case-sensitive suffix here
    would omit from a fingerprint a file the scans include -- its edits would
    never invalidate. Alias-free by name, before any ``stat``: the roster drops
    the aliases, so a fingerprint that counted them would move on writes the
    roster cannot see -- invalidating every cache it guards on each alias write
    -- and would cost one ``stat`` per alias per call on the event loop, which
    with thousands of aliases is the walk that stalls the loop. A directory that
    cannot be listed raises the ``OSError``; each caller decides what that means.
    """
    with os.scandir(d) as it:
        for entry in it:
            if is_agent_spec_name(entry.name) and not is_native_skill_alias_name(entry.name):
                yield entry


def _dir_signature(d: Path) -> _ListAgentsSig:
    """Cheap stat-only signature of the agents dir.

    Captures each spec entry's name and mtime (both forms; a markdown edit
    that went unfingerprinted would serve a stale roster forever) — enough to detect adds,
    removals, renames, and any edit that changes a file's mtime, without
    reading or parsing any file. A skill-view alias is not a spec entry here
    (:func:`_iter_spec_entries`), so an alias write leaves the signature, and
    the caches it guards, untouched. An edit landing inside the same mtime tick
    is invisible here; :func:`clear_list_agents_cache` is the escape hatch
    the write paths use for exactly that case. Naming the files matters: a
    rename changes neither the file count nor any file's mtime, but does
    change the stem-derived agent name and the anchoring of relative
    ``skill://`` globs. Invalidates the :func:`list_agents`, project-names,
    and parsed-specs caches.

    Always a tuple, never ``None``: the catalog caches it revalidates stay on
    for a symlinked or freshly edited directory. :func:`agents_dir_revision`
    is the stricter fingerprint, for answers that must never be served stale.
    """
    try:
        return _entries_signature(_iter_spec_entries(d))
    except OSError:
        return ()


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


# A signature row that marks a listing as a PREFIX, not the directory. Paired
# with a counter rather than a constant on purpose: a partial signature must not
# compare equal to a later partial one either, or two identical failures would
# reuse a stale roster. See the ``except OSError`` arm of ``_entries_signature``.
_DIR_TRUNCATED_ROW = "\0dir-truncated"


_truncation_counter = itertools.count()


def _entries_signature(
    entries: Iterable[os.DirEntry[str]], *, dir_fd: int | None = None
) -> _ListAgentsSig:
    """Signature of an ALREADY-OPENED listing, so the caller can compute it
    without re-resolving the directory by name.

    Split out of :func:`_dir_signature` so a pinned scan can hand over the
    entries it already validated (see :func:`_pinned_scan_dir_fd`): re-opening the
    directory by path to stat it would reintroduce exactly the check-to-use
    window the pin exists to close. When *dir_fd* is available, link targets are
    read relative to that same validated directory; platforms without
    descriptor-relative ``readlink`` retain the by-name behaviour.
    """
    out: list[tuple[str, int]] = []
    identity = _opened_dir_identity(dir_fd)
    if identity is not None:
        out.extend(((_DIR_ID_DEV_ROW, identity[0]), (_DIR_ID_INO_ROW, identity[1])))
    try:
        for entry in entries:
            # Both spec forms, case-insensitively: a case-insensitive filesystem
            # serves ``Foo.JSON`` to ``*.json`` consumers, so a case-sensitive
            # suffix here would omit from the signature a file the scans include
            # — its edits would never invalidate. A markdown spec that went
            # unfingerprinted would likewise serve a stale roster forever, so the
            # predicate is the scans' own (:func:`is_agent_spec_name`), not a
            # second copy of it.
            if not is_agent_spec_name(entry.name):
                continue
            try:
                # follow_symlinks=False, i.e. lstat semantics: ``DirEntry.stat()``
                # follows by DEFAULT, and on Windows statting a name that is a
                # symlink to ``\\host\share`` IS the outbound SMB/NTLM
                # authentication -- so fingerprinting a planted link would
                # authenticate to a caller-chosen host before the reader's
                # resolved-target guard in ``_read_agent_spec`` ever runs. The
                # directory-level hold protects the scan DIRECTORY from being
                # swapped; it says nothing about a child entry inside it, and an
                # attacker who can write the scanned path is this feature's own
                # threat model. The LINK's own mtime detects a REPOINT of the
                # link, which a followed stat would miss.
                m = entry.stat(follow_symlinks=False).st_mtime_ns
            except OSError:
                m = 0
            out.append((entry.name, m))
            # The link mtime alone is NOT enough for a symlinked spec: it catches
            # a repoint but not an edit to the file the link resolves to (the
            # common ``~/.kiro/agents/mine.json`` -> a dotfiles copy). Without the
            # target's mtime folded in, such an edit leaves this signature
            # identical and both ``_LIST_AGENTS_CACHE`` and ``_PARSED_SPECS_CACHE``
            # serve the pre-edit model/tools/prompt until a write path clears the
            # cache or the gateway restarts -- the very staleness
            # :func:`agents_dir_revision` returns ``None`` for on a symlinked spec.
            # Recorded under a ``\0target`` key: a NUL cannot appear in a real
            # filename, so this synthetic entry can never collide with another
            # file's ``(name, mtime)`` pair, exactly as :func:`_sensitive_dir_sig`
            # keeps its sentinel outside the filename space.
            #
            # The FOLLOWED stat carries the SMB/NTLM cost the non-following stat
            # above exists to avoid, so the link's stored target (read locally by
            # ``os.readlink``, which connects to no host) first passes
            # :func:`kiro_crew.hooks.validate_file_path` -- the raw UNC gate plus
            # the Windows link screen over every ancestor and hop of the chain. A
            # link whose chain reaches an untrusted UNC share is therefore
            # fingerprinted by its link mtime alone and never followed; a repoint
            # to such a share still changes the link mtime and invalidates.
            #
            # Accepted cost of reusing the public validator: it also refuses a
            # SENSITIVE resolved target, so such a link gets the link-mtime-only
            # fingerprint too. The agent list is unchanged by that, because the
            # spec reader refuses a sensitive target anyway. It also costs one
            # extra ``realpath`` per symlinked entry on Linux/macOS, where the
            # UNC half of the gate never refuses.
            if entry.is_symlink():
                try:
                    if dir_fd is not None and os.readlink in os.supports_dir_fd:
                        _target = os.readlink(entry.name, dir_fd=dir_fd)
                    else:
                        _target = os.readlink(entry.path)
                except OSError:
                    # A failed descriptor-relative read must not retry by name:
                    # that would re-resolve a directory the caller pinned.
                    _target = None
                if _target is not None:
                    _target_path = (
                        _target
                        if os.path.isabs(_target)
                        else os.path.join(os.path.dirname(entry.path), _target)
                    )
                    target_safe_to_follow = validate_file_path(_target_path) is not None
                else:
                    target_safe_to_follow = False
                if target_safe_to_follow:
                    try:
                        fm = entry.stat(follow_symlinks=True).st_mtime_ns
                    except OSError:
                        fm = 0
                    out.append((entry.name + "\0target", fm))
    except OSError:
        # The listing aborted part-way, so ``out`` describes a PREFIX of the
        # directory rather than the directory. Returning it bare would let a
        # truncated scan be cached as a complete one, and a second identical
        # failure would then hit that entry and hand back names the directory
        # has stopped declaring -- including an agent a folder's saved default
        # still points at. Appending a row that is unique per call is what the
        # oversized-name path does with ``_oversized_name_sig``, one step
        # further: a partial signature compares equal to NOTHING, not even to
        # the next partial one, so it is never reused and the next read
        # rescans. The type and arity are unchanged, so ``_dir_signature``'s
        # own callers keep their contract and simply rescan on a truncated
        # read instead of trusting a prefix.
        #
        # Only a listing that PRODUCED something is a prefix. A walk that fails
        # before collecting any row describes no directory at all, and an empty
        # ``out`` is the documented signature for an absent or unlistable one
        # (:func:`agents_dir_revision` returns ``None`` beside it) -- marking
        # that case would turn "there is nothing here" into a value that
        # differs on every call.
        if out:
            out.append((_DIR_TRUNCATED_ROW, next(_truncation_counter)))
    return tuple(sorted(out))


def _signature_dir_identity(signature: _ListAgentsSig) -> _DirIdentity | None:
    """Read the directory identity recorded in *signature*, when one exists."""
    rows = dict(signature)
    dev = rows.get(_DIR_ID_DEV_ROW)
    ino = rows.get(_DIR_ID_INO_ROW)
    return None if dev is None or ino is None else (dev, ino)
