"""Per-project in-memory file index for fast file search."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict

from kiro_crew import executors
from kiro_crew.security import is_sensitive_canonical_path

logger = logging.getLogger(__name__)

# Directory names never offered as @-mention candidates and never descended
# into, by either search path. This is the SINGLE source of truth: the walk
# fallback in dashboard/file_api/search.py imports it, so the indexed fast path
# and the fallback cannot diverge. Dot-prefixed noise dirs (.git, .cache,
# .venv) are listed EXPLICITLY here -- the candidate collection does not filter
# on a leading dot, which would hide the .github/.kiro/.claude dirs that must be
# offered, so each dot-dir to suppress must be named.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".cache",
        ".venv",
        "node_modules",
        "__pycache__",
        "venv",
        "dist",
        "build",
        "env",
        "out",
        "target",
    }
)

# How long a built index is trusted before a search triggers a background
# rebuild. The index refreshes on demand -- when a search finds it older than
# this -- rather than on a timer, so a root nobody searches never costs a walk.
_REFRESH_TTL_SECS = 30
_MAX_ENTRIES = 100_000

# Execution ceiling for one background index walk on the path-transfer pool.
# The walk is bounded by entry count, but a dead or slow mount can still make
# each os.stat hang; this bounds how long one rebuild may hold its pool worker.
_WALK_EXEC_CEILING_SECS = 30.0

# How many distinct roots the registry keeps indexed at once. Each ``?project=``
# the picker sends is a root, so a user browsing many folders would otherwise
# accumulate one up-to-100k-entry index per folder forever. The registry evicts
# the least-recently-searched root past this bound (an evicted root rebuilds on
# its next search).
_MAX_CACHED_ROOTS = 8

# (path, name, relpath, size, mtime, kind) where kind is "file" or "dir".
# Directory entries carry size 0.
_Entry = tuple[str, str, str, int, int, str]


class FileIndex:
    """In-memory file index for a single project root.

    Lifecycle: call ``build()`` once to populate it, ``stop()`` to cancel an
    in-flight background rebuild. ``search()`` is synchronous and scans the
    in-memory list. There is no background timer: ``maybe_refresh()`` rebuilds
    in the background only when a search finds the index older than the TTL, so
    a root nobody searches is never walked again after its first build.
    """

    __slots__ = ("root", "_entries", "_task", "_ready", "_truncated", "_built_at", "_walk_only")

    def __init__(self, root: str) -> None:
        self.root = root
        self._entries: list[_Entry] = []
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._ready = asyncio.Event()
        self._truncated = False
        self._built_at = 0.0
        # Set when a build timed out or failed: the root is served from the walk
        # fallback from then on and never re-indexed, exactly as a truncated
        # root is. This is what stops a slow/dead mount from retrying the walk
        # on every search and piling abandoned walks onto the shared pool.
        self._walk_only = False

    async def build(self) -> None:
        """Build (or rebuild) the index now, then mark it ready."""
        await self._rebuild()
        self._ready.set()

    def stop(self) -> None:
        """Cancel an in-flight background rebuild, if any."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    def maybe_refresh(self) -> None:
        """Rebuild in the background when the index is older than the TTL.

        Called by the registry on each search. A rebuild already in flight is
        left alone, so concurrent searches coalesce onto one walk, and the
        current entries keep serving until the fresh walk replaces them -- so a
        stale-but-ready index never blocks a search on a filesystem walk. The
        cost is bounded staleness: a file created since the last build appears
        only after the next refresh completes, which the search endpoint
        accepts for a typeahead (and the walk fallback covers the cold case
        before the first build finishes).

        A truncated or walk-only index is never refreshed: the search endpoint
        does not use it (it falls back to the walk), so rebuilding it would
        re-walk the tree every TTL for nothing -- and for a walk-only root (a
        build that timed out or failed) retrying is exactly the pile-up of
        abandoned walks on the shared pool this flag exists to prevent.
        """
        if self._truncated or self._walk_only:
            return
        if self._task and not self._task.done():
            return
        if time.monotonic() - self._built_at < _REFRESH_TTL_SECS:
            return
        self._task = asyncio.create_task(self._refresh_once())

    async def _refresh_once(self) -> None:
        try:
            await self._rebuild()
            self._ready.set()
        except asyncio.CancelledError:
            pass
        except Exception:
            # A build that timed out or failed marks the root walk-only, so the
            # endpoint serves it from the walk and maybe_refresh never retries.
            # The abandoned walk thread (run_in_cron_pool's timeout bounds only
            # the await, not the thread) is left to finish on its own pool worker
            # rather than being respawned on the next search.
            self._walk_only = True
            logger.warning("FileIndex build failed for %s; serving from walk fallback", self.root, exc_info=True)

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def usable(self) -> bool:
        """True when the endpoint should serve from this index rather than walk.

        False while the index is still building (not ready), when it is
        truncated (exceeded the entry cap), or when a build timed out or failed
        (walk-only) -- in every one of those the search falls back to the walk.
        """
        return self.is_ready and not self._truncated and not self._walk_only

    def search(
        self,
        query: str,
        scorer,
        max_results: int = 15,
        kinds: str = "all",
    ) -> list[dict]:
        """Search the index using the provided scorer function.

        Args:
            query: lowercased search query (min 2 chars).
            scorer: callable(query, filename, relpath) -> float. 0 = no match.
            max_results: cap on returned results.
            kinds: ``"all"`` (default), ``"files"``, or ``"dirs"``.
        """
        want_files = kinds in ("all", "files")
        want_dirs = kinds in ("all", "dirs")
        hits: list[dict] = []
        for fpath, fname, rel, size, mtime, kind in self._entries:
            if kind == "dir":
                if not want_dirs:
                    continue
            elif not want_files:
                continue
            sc = scorer(query, fname, rel)
            if sc <= 0:
                continue
            hits.append({
                "path": fpath, "name": fname, "kind": kind,
                "size": size, "mtime": mtime, "_score": sc,
            })
        now = time.time()
        # Files rank above dirs on an otherwise equal score so directory
        # entries never crowd out the file the user is most likely after.
        hits.sort(key=lambda r: (
            -r["_score"], r["kind"] == "dir", len(r["name"]), now - r["mtime"],
        ))
        return hits[:max_results]

    async def _rebuild(self) -> None:
        # Run the walk on the dedicated path-transfer pool, NOT the loop's
        # default executor (a bare ``asyncio.to_thread``). The walk has no
        # wall-clock limit, so on a dead or slow mount it can occupy its worker
        # for a long time; on the default executor that worker is shared with
        # every other ``to_thread`` caller, so a stuck build would starve them.
        # The request-path walk fallback takes this same pool through
        # ``_run_path_probe`` for the same reason. ``run_in_cron_pool`` waits
        # (untimed) for a worker, then applies ``timeout`` to the AWAIT only --
        # a thread cannot be interrupted, so on a timeout the walk keeps running
        # on its pool worker until it finishes and ``run_in_cron_pool`` raises.
        # ``_refresh_once`` catches that, marks the root walk-only, and never
        # retries, so a slow mount costs at most one abandoned walk per root
        # rather than a fresh one on every search.
        entries, truncated = await executors.run_in_cron_pool(
            self._walk,
            timeout=_WALK_EXEC_CEILING_SECS,
            executor=executors.path_transfer_executor(),
        )
        self._entries = entries
        self._truncated = truncated
        self._built_at = time.monotonic()
        logger.debug("FileIndex rebuilt for %s: %d entries%s", self.root, len(entries), " (truncated)" if truncated else "")

    def _walk(self) -> tuple[list[_Entry], bool]:
        entries: list[_Entry] = []
        truncated = False
        # Every root the registry indexes is a deliberately SCOPED root (the
        # search endpoint only builds an index for an explicit ``?project=`` /
        # ``?workspace=``), so -- exactly like the scoped walk fallback in
        # search.py -- the TCC-gated folders are NOT pruned: the access is
        # deliberate and macOS's own one-time prompt is the expected contract.
        # Pruning here would silently drop Documents/Desktop/Downloads from an
        # explicit ``?project=$HOME`` search while the walk fallback kept them,
        # so the two result paths must stay symmetric.
        for dirpath, dirnames, filenames in os.walk(self.root):
            # A dot-prefixed directory (.github, .kiro, .claude) should be
            # OFFERED as a search candidate even though we must not DESCEND into
            # it -- pruning ``dirnames`` in place before the collection loop below
            # would conflate the two concerns.
            #
            # Build the candidate list (what we offer AND stat) first, then
            # derive the narrower descent list from it. Both drop _SKIP_DIRS
            # (.git, node_modules, ...). Only the leading-dot rule differs: a
            # dot-dir is a valid candidate but must not be descended into, so it
            # is removed from the descent list alone.
            candidate_dirs = [d for d in dirnames if d not in _SKIP_DIRS]
            dirnames[:] = [d for d in candidate_dirs if not d.startswith(".")]
            for dname in candidate_dirs:
                if len(entries) >= _MAX_ENTRIES:
                    truncated = True
                    break
                dfull = os.path.join(dirpath, dname)
                # Resolve symlinks before the sensitivity check so a link
                # pointing into a sensitive tree cannot slip through. The
                # canonical gate matches inline on this worker thread: the
                # bounded gate would submit one more resolution per entry to the
                # small ``mc-pathres`` pool the event loop waits on, and a
                # rebuild of this size fills its queue.
                if is_sensitive_canonical_path(os.path.realpath(dfull)):
                    continue
                try:
                    st = os.stat(dfull)
                except OSError:
                    continue
                entries.append((
                    dfull, dname, os.path.relpath(dfull, self.root),
                    0, int(st.st_mtime), "dir",
                ))
            if truncated:
                break
            for fname in filenames:
                if len(entries) >= _MAX_ENTRIES:
                    truncated = True
                    break
                if fname.startswith("."):
                    continue
                fpath = os.path.join(dirpath, fname)
                if is_sensitive_canonical_path(os.path.realpath(fpath)):
                    continue
                try:
                    st = os.stat(fpath)
                except OSError:
                    continue
                entries.append((
                    fpath, fname, os.path.relpath(fpath, self.root),
                    st.st_size, int(st.st_mtime), "file",
                ))
            if truncated:
                break
        return entries, truncated


class FileIndexRegistry:
    """Lazily builds and caches up to ``_MAX_CACHED_ROOTS`` :class:`FileIndex`.

    Shared across slots -- two slots pointing at the same project share one
    index. An index is built in the BACKGROUND the first time a scoped search
    names its root (``ensure``), so no index exists until someone searches that
    root, and the search that triggers the build still returns walk results
    while it runs. Roots are held in least-recently-searched order and the
    oldest is evicted (and its in-flight build cancelled) once the cache is
    full, so a user browsing many folders cannot accumulate an unbounded number
    of up-to-100k-entry indexes. ``stop_all`` cancels every in-flight build at
    shutdown.
    """

    __slots__ = ("_indexes", "_lock")

    def __init__(self) -> None:
        # Ordered least- to most-recently-searched; the first item is the
        # eviction candidate.
        self._indexes: "OrderedDict[str, FileIndex]" = OrderedDict()
        self._lock = asyncio.Lock()

    async def ensure(self, root: str) -> FileIndex | None:
        """Return the ready index for *root*, or ``None`` while one is built.

        First call for a root starts a background build and returns ``None``, so
        the triggering search falls back to a walk rather than blocking on the
        initial walk. Once the index is ready, subsequent calls return it and
        trigger a background TTL refresh if it has gone stale. A build still in
        flight also returns ``None``. The build runs outside the lock so other
        roots are not blocked behind one large tree.
        """
        root = os.path.realpath(root)
        async with self._lock:
            idx = self._indexes.get(root)
            if idx is None:
                idx = FileIndex(root)
                self._indexes[root] = idx
                self._evict_locked()
            else:
                # Mark this root most-recently-searched.
                self._indexes.move_to_end(root)
        # ``maybe_refresh`` starts the background build when none is in flight
        # and the index is older than the TTL. A never-built index (built_at 0)
        # is always older than the TTL, so the first call kicks the initial
        # build; later calls only rebuild once the TTL lapses.
        idx.maybe_refresh()
        return idx if idx.is_ready else None

    def _evict_locked(self) -> None:
        """Drop least-recently-searched roots until within the cache bound.

        Caller must hold ``self._lock``. An evicted index's in-flight build is
        cancelled; the root rebuilds from scratch on its next search.
        """
        while len(self._indexes) > _MAX_CACHED_ROOTS:
            _root, idx = self._indexes.popitem(last=False)
            idx.stop()

    def stop_all(self) -> None:
        """Cancel every index's in-flight build (gateway shutdown)."""
        for idx in self._indexes.values():
            idx.stop()
        self._indexes.clear()
