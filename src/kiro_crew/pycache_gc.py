"""Garbage collection for the shared CPython bytecode cache.

The packaged desktop app launches the gateway with ``PYTHONPYCACHEPREFIX``
pointing at ``<data home>/cache/pycache`` so the embedded interpreter never
writes ``__pycache__`` inside the signed app bundle (which would break the
codesign seal — see ``website/electron/main.js``). PEP 3147 mirrors every
imported source file's absolute path under that prefix, and CPython only ever
adds entries, so the gateway owns eviction. The sandbox env scrub keeps the
prefix out of the agent subtree (``sandbox._PYTHON_ENV_PREFIXES``), which
removes the unbounded input — ephemeral foreign interpreters each minting a
fresh path-keyed mirror — but the gateway's own interpreter tree still
accretes entries, and installs that predate the scrub carry multi-GB residue.

Deleting any entry is always safe: a ``.pyc`` is a derived artifact and
CPython transparently recompiles it on the next import (at worst a marginally
slower first run). That safety is what lets the GC stay simple and aggressive:
a mtime TTL for staleness plus an oldest-first total-size cap.

The cache root lives under the crew home, which the agent subtree can write.
A path-based walk validated up front is therefore raceable: swap the root (or
any subdirectory) for a symlink between the check and the delete and the GC
follows the replacement out of the cache. The traversal is instead anchored
to *no-follow directory handles*: the root and every subdirectory are opened
with ``O_NOFOLLOW | O_DIRECTORY`` and all stat/unlink/rmdir calls are
``dir_fd``-relative to the already-open handle, so a substituted link at any
level fails the open (``ELOOP``/``ENOTDIR``) instead of being traversed —
the same defense ``shutil.rmtree`` uses against symlink attacks; the root
itself is opened component by component from the filesystem root, since
``O_NOFOLLOW`` only guards the final segment of a single ``open`` and the
agent-writable ancestors (``cache/`` itself) are exactly where a link swap
lands. A legitimately symlinked ancestor therefore makes the prune a
conservative no-op — refusal, not traversal, is the failure mode. On platforms
without ``dir_fd`` support (Windows), the prune is skipped entirely
(fail-closed) rather than falling back to a raceable path walk; the env scrub
half of the fix is unaffected there, and the un-GC'd residue of the gateway's
own tree is a bounded, accepted trade-off.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
import time
from pathlib import Path

from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)

# A ``.pyc`` is written once and only read afterwards, so mtime is its age.
# Entries older than this are stale regardless of use; hot ones regenerate on
# the next import for the cost of one compile.
PYCACHE_MAX_AGE_DAYS = 30

# Ceiling on the whole mirror. The legitimate content — one interpreter's
# stdlib plus Kiro Crew's site-packages — is well under this; anything beyond
# it is leftover foreign-interpreter residue, evicted oldest-first.
PYCACHE_MAX_TOTAL_BYTES = 1 * 1024**3

# Cadence for the periodic-sweep hook in ``session.py``. The prune walks the
# whole cache tree (hundreds of thousands of files on a bloated install), far
# too heavy for the sweep's ~5-minute tick.
PYCACHE_GC_INTERVAL_SECS = 24 * 3600

# Open flags for every directory handle the traversal descends through.
# O_NOFOLLOW makes opening a symlink fail with ELOOP instead of following it;
# O_DIRECTORY makes opening a non-directory fail with ENOTDIR. Together they
# guarantee a handle, once open, refers to a real directory that was reached
# without crossing a link — the property the whole prune is anchored to.
_DIR_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _fd_traversal_supported() -> bool:
    """Whether this platform can anchor the walk to no-follow dir handles."""
    return (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.rmdir in os.supports_dir_fd
        and os.scandir in os.supports_fd
    )


def pycache_cache_dir() -> Path:
    """The bytecode-cache root the GC prunes.

    Only ever returns Kiro Crew's own configured cache directory. The
    interpreter's *active* ``sys.pycache_prefix`` (set from Electron's
    ``PYTHONPYCACHEPREFIX``) is honored **only when it resolves to that same
    configured directory** — its exact path form is then authoritative. An
    arbitrary user-set prefix pointing anywhere else must never become a
    recursive deletion root: their ``.pyc`` mirrors are not Kiro Crew's to
    manage, so the GC sticks to the configured directory (which is where the
    gateway's own historical residue lives regardless of the active prefix).
    """
    configured = config_dir() / "cache" / "pycache"
    active = sys.pycache_prefix
    if active and os.path.realpath(active) == os.path.realpath(configured):
        return Path(active)
    return configured


def prune_pycache(
    root: Path | None = None,
    *,
    max_age_days: float = PYCACHE_MAX_AGE_DAYS,
    max_total_bytes: int = PYCACHE_MAX_TOTAL_BYTES,
    now: float | None = None,
) -> tuple[int, int]:
    """Delete expired ``.pyc`` files under *root* and cap the tree's total size.

    Two fd-anchored sweeps: files whose mtime is past the TTL are removed
    outright; if the survivors still exceed *max_total_bytes*, a second sweep
    removes the oldest survivors until the tree fits (files sharing the
    boundary mtime are all removed — over-deleting a tie is harmless, the
    ``.pyc`` regenerates). Only regular files named ``*.pyc`` are ever
    unlinked — a foreign file is left alone and (via the post-order rmdir)
    pins its directory. Emptied directories are pruned so the mirror doesn't
    decay into a huge bare skeleton.

    Every directory, starting with *root* itself, is opened with
    ``O_NOFOLLOW | O_DIRECTORY`` and operated on via ``dir_fd``-relative
    calls, so a symlink (or Windows junction) substituted at any level at any
    time fails the open instead of redirecting the deletion outside the
    cache. On platforms without ``dir_fd`` support the prune is a no-op.

    Blocking filesystem walk: call from the maintenance executor, never on
    the event loop. Every per-file error is swallowed (the cache regenerates,
    and a file vanishing mid-walk is normal when an interpreter is writing).

    Returns ``(files_removed, bytes_freed)``.
    """
    if not _fd_traversal_supported():
        logger.debug("pycache GC skipped: no dir_fd-anchored traversal on this platform")
        return (0, 0)
    root = pycache_cache_dir() if root is None else root
    now = time.time() if now is None else now
    ttl_cutoff = now - max_age_days * 86400.0

    survivors: list[tuple[float, int]] = []
    result = _sweep_root(root, cutoff=ttl_cutoff, inclusive=False, survivors=survivors)
    if result is None:
        return (0, 0)
    removed, freed = result

    total = sum(size for _mtime, size in survivors)
    if total > max_total_bytes:
        survivors.sort()  # oldest first
        acc = 0
        size_cutoff = ttl_cutoff
        for mtime, size in survivors:
            acc += size
            size_cutoff = mtime
            if total - acc <= max_total_bytes:
                break
        # Re-open the root: the second sweep re-validates the whole chain of
        # handles rather than trusting state from the first pass.
        result = _sweep_root(root, cutoff=size_cutoff, inclusive=True, survivors=None)
        if result is not None:
            removed += result[0]
            freed += result[1]

    return (removed, freed)


def _sweep_root(
    root: Path,
    *,
    cutoff: float,
    inclusive: bool,
    survivors: list[tuple[float, int]] | None,
) -> tuple[int, int] | None:
    """Open *root* as a no-follow handle chain and run one sweep under it.

    ``O_NOFOLLOW`` only protects the *final* component of a single ``open``,
    so a one-shot ``os.open(root, ...)`` would still follow a symlink swapped
    into any writable ancestor (e.g. replacing ``cache/`` itself while
    ``pycache`` stays a real directory inside the link target). The root is
    therefore opened **component by component from the filesystem root**,
    each step an ``O_NOFOLLOW | O_DIRECTORY`` ``openat`` relative to the
    previous handle, so every ancestor is proven to be a real directory at
    open time — no path segment is ever re-resolved through the kernel's
    symlink-following lookup.

    Returns ``None`` when any component cannot be opened as a real directory
    — missing, replaced by a symlink/junction (``ELOOP``/``ENOTDIR``), or
    otherwise unreadable.
    """
    parts = Path(os.path.abspath(root)).parts
    try:
        fd = os.open(parts[0], _DIR_OPEN_FLAGS)
    except OSError:
        return None
    try:
        for part in parts[1:]:
            try:
                next_fd = os.open(part, _DIR_OPEN_FLAGS, dir_fd=fd)
            except OSError:
                return None
            os.close(fd)
            fd = next_fd
        return _sweep(fd, cutoff=cutoff, inclusive=inclusive, survivors=survivors)
    finally:
        os.close(fd)


def _sweep(
    fd: int,
    *,
    cutoff: float,
    inclusive: bool,
    survivors: list[tuple[float, int]] | None,
) -> tuple[int, int]:
    """One post-order sweep of the directory handle *fd*.

    Unlinks regular ``*.pyc`` entries with mtime before *cutoff* (at or
    before, when *inclusive*), records the rest into *survivors* when given,
    recurses into subdirectories through fresh no-follow handles, and rmdirs
    each emptied subdirectory on the way back out (best-effort — a non-empty
    directory just refuses). Recursion depth is bounded by the mirrored
    filesystem path depth.
    """
    removed = 0
    freed = 0
    entries: list[tuple[str, os.stat_result]] = []
    try:
        with os.scandir(fd) as it:
            for entry in it:
                try:
                    entries.append((entry.name, entry.stat(follow_symlinks=False)))
                except OSError:
                    continue
    except OSError:
        return (removed, freed)
    for name, st in entries:
        if stat.S_ISDIR(st.st_mode):
            try:
                child = os.open(name, _DIR_OPEN_FLAGS, dir_fd=fd)
            except OSError:
                # Symlink/junction (ELOOP/ENOTDIR) or vanished: never descend.
                continue
            try:
                sub_removed, sub_freed = _sweep(
                    child, cutoff=cutoff, inclusive=inclusive, survivors=survivors
                )
            finally:
                os.close(child)
            removed += sub_removed
            freed += sub_freed
            try:
                os.rmdir(name, dir_fd=fd)
            except OSError:
                pass
        elif stat.S_ISREG(st.st_mode) and name.endswith(".pyc"):
            expired = st.st_mtime <= cutoff if inclusive else st.st_mtime < cutoff
            if expired:
                try:
                    os.unlink(name, dir_fd=fd)
                except OSError:
                    continue
                removed += 1
                freed += st.st_size
            elif survivors is not None:
                survivors.append((st.st_mtime, st.st_size))
    return (removed, freed)
