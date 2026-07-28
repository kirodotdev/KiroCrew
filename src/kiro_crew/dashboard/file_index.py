"""Per-project in-memory file index for fast file search."""

from __future__ import annotations

import asyncio
import logging
import os
import time

from kiro_crew import platform_compat
from kiro_crew.security import is_sensitive_path

logger = logging.getLogger(__name__)

# Dot-prefixed dirs (.kirocrew, .kiro, .aim, .git) are already excluded by
# the ``not d.startswith(".")`` guard in _walk(), so only non-dot dirs here.
_SKIP_DIRS = frozenset({
    "node_modules", "__pycache__", "venv",
    "dist", "build", "env", "out", "target",
})

_REFRESH_SECS = 30
_MAX_ENTRIES = 100_000


class FileIndex:
    """In-memory file index for a single project root.

    Lifecycle: call ``start()`` to begin background refresh, ``stop()`` to cancel.
    ``search()`` is synchronous and scans the in-memory list.
    """

    __slots__ = ("root", "_entries", "_task", "_ready", "_truncated")

    def __init__(self, root: str) -> None:
        self.root = root
        self._entries: list[tuple[str, str, str, int, int]] = []  # (path, name, relpath, size, mtime)
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._ready = asyncio.Event()
        self._truncated = False

    async def start(self) -> None:
        """Build initial index and start background refresh loop."""
        await self._rebuild()
        self._ready.set()
        self._task = asyncio.create_task(self._refresh_loop())

    def stop(self) -> None:
        """Cancel the background refresh task."""
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def truncated(self) -> bool:
        return self._truncated

    def search(self, query: str, scorer, max_results: int = 15) -> list[dict]:
        """Search the index using the provided scorer function.

        Args:
            query: lowercased search query (min 2 chars).
            scorer: callable(query, filename, relpath) -> float. 0 = no match.
            max_results: cap on returned results.
        """
        hits: list[dict] = []
        for fpath, fname, rel, size, mtime in self._entries:
            sc = scorer(query, fname, rel)
            if sc <= 0:
                continue
            hits.append({
                "path": fpath, "name": fname,
                "size": size, "mtime": mtime, "_score": sc,
            })
        now = time.time()
        hits.sort(key=lambda r: (-r["_score"], len(r["name"]), now - r["mtime"]))
        return hits[:max_results]

    async def _rebuild(self) -> None:
        entries, truncated = await asyncio.to_thread(self._walk)
        self._entries = entries
        self._truncated = truncated
        logger.debug("FileIndex rebuilt for %s: %d entries%s", self.root, len(entries), " (truncated)" if truncated else "")

    def _walk(self) -> tuple[list[tuple[str, str, str, int, int]], bool]:
        entries: list[tuple[str, str, str, int, int]] = []
        truncated = False
        # macOS: if this index is rooted at bare $HOME, prune the TCC-gated
        # folders. This walk re-runs every _REFRESH_SECS, so without the prune
        # a dismissed consent dialog would be re-triggered on every refresh.
        tcc_skip = platform_compat.tcc_protected_dirs_for_walk(self.root)
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d not in _SKIP_DIRS
                and not (dirpath == self.root and d in tcc_skip)
            ]
            for fname in filenames:
                if len(entries) >= _MAX_ENTRIES:
                    truncated = True
                    break
                if fname.startswith("."):
                    continue
                fpath = os.path.join(dirpath, fname)
                if is_sensitive_path(fpath):
                    continue
                try:
                    st = os.stat(fpath)
                except OSError:
                    continue
                entries.append((fpath, fname, os.path.relpath(fpath, self.root), st.st_size, int(st.st_mtime)))
            if truncated:
                break
        return entries, truncated

    async def _refresh_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(_REFRESH_SECS)
                try:
                    await self._rebuild()
                except Exception:
                    logger.warning("FileIndex refresh failed for %s", self.root, exc_info=True)
        except asyncio.CancelledError:
            pass


class FileIndexRegistry:
    """Manages FileIndex instances keyed by project root.

    Shared across slots — if two slots point at the same project, they
    share one index.  Indexes are stopped and removed when no longer
    referenced (via ``release``).
    """

    __slots__ = ("_indexes", "_refcounts", "_lock")

    def __init__(self) -> None:
        self._indexes: dict[str, FileIndex] = {}
        self._refcounts: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, root: str) -> FileIndex:
        """Get or create an index for *root*, incrementing its refcount."""
        root = os.path.realpath(root)
        async with self._lock:
            if root in self._indexes:
                self._refcounts[root] = self._refcounts.get(root, 0) + 1
                return self._indexes[root]
        # Build outside the lock so other roots aren't blocked
        idx = FileIndex(root)
        try:
            await idx.start()
        except Exception:
            raise
        async with self._lock:
            # Another coroutine may have created one while we awaited
            if root in self._indexes:
                idx.stop()
                self._refcounts[root] = self._refcounts.get(root, 0) + 1
                return self._indexes[root]
            self._indexes[root] = idx
            self._refcounts[root] = 1
        return idx

    async def release(self, root: str) -> None:
        """Decrement refcount; stop and remove index when it hits zero."""
        root = os.path.realpath(root)
        async with self._lock:
            cnt = self._refcounts.get(root, 0)
            if cnt <= 0:
                return  # never acquired or already fully released
            cnt -= 1
            if cnt == 0:
                idx = self._indexes.pop(root, None)
                self._refcounts.pop(root, None)
                if idx:
                    idx.stop()
            else:
                self._refcounts[root] = cnt

    def get(self, root: str) -> FileIndex | None:
        """Return existing index for *root* without changing refcount."""
        return self._indexes.get(os.path.realpath(root))

    def stop_all(self) -> None:
        """Stop all indexes (gateway shutdown)."""
        for idx in self._indexes.values():
            idx.stop()
        self._indexes.clear()
        self._refcounts.clear()
