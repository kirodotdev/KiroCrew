"""App dev mode — live-reload support for app UI development.

When an installed app has ``dev: true`` in its ``installed.json``:

* ``handle_app_ui_file`` serves its UI files with ``Cache-Control: no-store``
  (never cached, not even with revalidation), and
* a gateway-side watcher polls the app's ``ui/`` directory (symlinks followed —
  the recommended dev setup symlinks ``ui/`` to the developer's source tree)
  and broadcasts an ``app_reload`` WebSocket event whenever any file changes.
  The dashboard's AppHost reloads so edits appear without a manual refresh.

Toggling dev mode is a metadata-only change (``installed.json``), picked up by
the watcher within one poll interval — no gateway restart needed.

Cost model (dev mode is off for essentially all production gateways):
  The watcher must NOT make every always-on gateway pay for a dev-only feature.
  ``set_dev_mode`` maintains a tiny sentinel file (:data:`_DEV_SENTINEL`) listing
  the app names currently in dev mode. Each tick the watcher ``stat()``s only
  that one file and re-reads it solely when it changes — so the zero-dev-apps
  steady state is a single ``stat()`` per second with no ``list_apps()`` call
  (which reads every app's manifest and can even *write* ``installed.json``) and
  no ``ui/`` walks. Only when at least one app is in dev mode does it walk that
  app's ``ui/`` tree. The set is also mirrored into an in-memory cache
  (:data:`_dev_apps_cache`) so ``handle_app_ui_file`` can decide the cache header
  without any per-request disk IO on the event loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from kiro_crew import platform_compat
from kiro_crew.apps.manager import (
    _check_path_safety,
    _read_installed,
    _write_installed,
    app_dir,
    apps_dir,
)
from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECS = 1.0
#: Safety cap on files scanned per app per tick — a runaway ui/ dir (e.g.
#: node_modules symlinked in) must not stall the loop.
_MAX_SCAN_FILES = 2000

#: Mask folding each file hash into a fixed-width non-negative int so the
#: accumulated digest stays bounded regardless of file count.
_DIGEST_MASK = (1 << 63) - 1

#: Sentinel file (a JSON array of app names in dev mode) under ``apps_dir()``.
#: ``list_apps()`` skips non-directory entries, so this file is invisible to it.
_DEV_SENTINEL = ".dev-apps.json"

_watch_task: asyncio.Task | None = None

#: In-memory mirror of the dev-app set, refreshed by the watcher and by
#: ``set_dev_mode`` (in-process). Lets the UI-serving hot path avoid a disk read
#: on every request. Empty until first seeded — a newly-started gateway may
#: serve a dev app cached for up to one poll interval, which is harmless.
_dev_apps_cache: set[str] = set()


def _sentinel_path() -> Path:
    return apps_dir() / _DEV_SENTINEL


@contextmanager
def _sentinel_lock() -> Iterator[None]:
    """Hold a cross-process exclusive lock guarding the sentinel read-modify-write.

    The lock is taken on a DEDICATED ``.lock`` file, never on the sentinel
    itself: :func:`_write_dev_sentinel` replaces the sentinel via
    ``atomic_write`` (write-temp + rename), so a lock held on the sentinel's own
    fd would guard a soon-to-be-orphaned inode and let a concurrent toggle race
    in. Concurrent gateway/CLI toggles of different apps otherwise read the same
    sentinel, mutate private copies, and last-writer-wins clobbers the others'
    entries — silently dropping an app from the watched/no-store set. Serializing
    the read → mutate → write → cache-update sequence closes that race.
    """
    lock_path = apps_dir() / (_DEV_SENTINEL + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as fh:
        with platform_compat.file_lock(fh.fileno(), exclusive=True):
            yield


def _read_dev_sentinel() -> set[str]:
    """Read the raw dev-app-name set from the sentinel file (empty on any error)."""
    try:
        data = json.loads(_sentinel_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if isinstance(data, list):
        return {str(x) for x in data}
    return set()


def _load_dev_apps() -> set[str]:
    """Read the sentinel and drop stale entries not backed by a dev-mode install.

    Defense-in-depth against a stale sentinel: an app uninstalled (or its
    ``installed.json`` ``dev`` flag cleared) out-of-band may leave its name in
    the sentinel. Left unfiltered, a DIFFERENT app later reinstalled under that
    same name would silently inherit dev-mode ``no-store`` serving and file
    watching despite its own metadata saying ``dev: false``. Filtering each
    candidate against the authoritative on-disk metadata prevents that
    misattribution even if the uninstall-time cleanup was skipped (e.g. a crash
    mid-uninstall). Off the hot path — called only by the watcher when the
    sentinel changes and at seeding, never per UI request.
    """
    keep: set[str] = set()
    for name in _read_dev_sentinel():
        try:
            meta = _read_installed(name)
        except Exception:
            continue
        if meta is not None and meta.dev:
            keep.add(name)
    return keep


def _write_dev_sentinel(names: set[str]) -> None:
    """Atomically write the dev-app-name set to the sentinel file."""
    path = _sentinel_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(sorted(names), indent=2) + "\n")


def _scan_installed_dev_apps() -> set[str]:
    """Return the dev-app set derived authoritatively from every ``installed.json``.

    Unlike :func:`_load_dev_apps` (which only *filters* the sentinel against
    on-disk metadata and can never *add* an entry the sentinel is missing), this
    walks the apps directory and reads each app's ``installed.json`` ``dev``
    flag directly. It is the source of truth for reconciling the sentinel at
    startup, so a ``dev: true`` written to ``installed.json`` out-of-band
    (snapshot restore, hand-edit, a crash between the metadata write and the
    sentinel write) is actually honored — the documented contract field, not
    the internal sentinel, decides. Blocking filesystem IO — offload to a
    thread; called only once at watcher init, never on the hot path.
    """
    keep: set[str] = set()
    root = apps_dir()
    if not root.is_dir():
        return keep
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        try:
            meta = _read_installed(entry.name)
        except Exception:
            continue
        if meta is not None and meta.dev:
            keep.add(entry.name)
    return keep


def _reconcile_sentinel_from_installed() -> set[str]:
    """Rebuild the sentinel from ``installed.json`` so the docs' field is authoritative.

    Runs the authoritative scan and, only when it diverges from the current
    sentinel, rewrites the sentinel to match. Returns the reconciled dev-app
    set. Blocking IO — offload to a thread.

    The scan MUST run *inside* the cross-process lock, atomic with the sentinel
    read → compare → write → cache-update. If the scan ran before the lock, a
    concurrent :func:`set_dev_mode` toggle (which itself holds the lock) could
    land between the scan and the lock acquisition: the toggle writes
    ``installed.json`` ``dev: true`` and adds the app to the sentinel, then
    reconcile acquires the lock with its *stale* scan (missing that app), sees a
    divergence, and rewrites the sentinel to exclude it — silently dropping the
    just-toggled app from the watched/no-store set until the next restart
    reconcile, even though the toggle reported success.
    """
    with _sentinel_lock():
        installed = _scan_installed_dev_apps()
        current = _read_dev_sentinel()
        if current != installed:
            logger.info(
                "app dev-mode: reconciling sentinel from installed.json "
                "(sentinel=%s -> installed=%s)",
                sorted(current),
                sorted(installed),
            )
            _write_dev_sentinel(installed)
        _set_dev_cache(installed)
    return installed


def _stat_sentinel() -> tuple[float, int] | None:
    """Return the sentinel's (mtime, size), or None if it doesn't exist."""
    try:
        st = _sentinel_path().stat()
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def _set_dev_cache(names: set[str]) -> None:
    global _dev_apps_cache
    _dev_apps_cache = set(names)


def set_dev_mode(name: str, enabled: bool) -> dict[str, Any]:
    """Toggle dev mode for an installed app. Returns a result dict.

    Blocking filesystem IO — callers on the event loop MUST offload this to a
    thread (``await asyncio.to_thread(set_dev_mode, ...)``).
    """
    if not _check_path_safety(name):
        return {"error": f"invalid app name {name!r}"}
    # Cheap validation read outside the lock — builtin/not-installed status does
    # not change under us. The authoritative read → mutate → write happens again
    # INSIDE the lock below so the installed.json write is atomic with the
    # sentinel update.
    meta = _read_installed(name)
    if meta is None:
        return {"error": f"app {name!r} is not installed"}
    if meta.origin == "builtin":
        return {"error": "builtin apps cannot be put in dev mode"}
    # The installed.json write AND the sentinel read-modify-write run under one
    # cross-process lock so a concurrent gateway POST + CLI toggle of the same
    # app cannot interleave (write-meta A, write-meta B, sentinel B, sentinel A)
    # and leave installed.json saying dev:true while the sentinel excludes it —
    # silently disabling watching + no-store serving until a restart reconcile.
    # meta is re-read inside the lock so we never clobber a concurrent writer's
    # other installed.json fields (e.g. an update_app version bump).
    with _sentinel_lock():
        meta = _read_installed(name)
        if meta is None:
            return {"error": f"app {name!r} is not installed"}
        meta.dev = enabled
        _write_installed(name, meta)
        names = _read_dev_sentinel()
        if enabled:
            names.add(name)
        else:
            names.discard(name)
        _write_dev_sentinel(names)
        # Update the in-process cache immediately so a same-process POST toggle
        # takes effect on the very next UI request (no wait for a watcher tick).
        _set_dev_cache(names)
    return {"name": name, "dev": enabled}


def remove_dev_app(name: str) -> None:
    """Drop *name* from the dev sentinel + in-memory cache (idempotent, best-effort).

    Called from :func:`kiro_crew.apps.manager.uninstall_app` so an app removed
    while in dev mode does not leave a stale sentinel entry that a later app
    reinstalled under the same name would inherit. Runs under the same
    cross-process lock as :func:`set_dev_mode`; never raises.
    """
    try:
        with _sentinel_lock():
            names = _read_dev_sentinel()
            if name not in names:
                return
            names.discard(name)
            _write_dev_sentinel(names)
            _set_dev_cache(names)
    except Exception:
        logger.debug("dev-mode sentinel cleanup for %r failed", name, exc_info=True)


def is_dev_mode(name: str) -> bool:
    """Whether an app is in dev mode, read authoritatively from disk.

    Blocking IO — do NOT call on the event loop per-request; use
    :func:`is_dev_mode_cached` on hot paths.
    """
    if not _check_path_safety(name):
        return False
    meta = _read_installed(name)
    return bool(meta and meta.dev)


def is_dev_mode_cached(name: str) -> bool:
    """Whether an app is in dev mode, from the in-memory cache (no disk IO).

    Safe to call on the event loop per-request. The cache is seeded at watcher
    init and refreshed each tick, so it lags disk by at most one poll interval.
    """
    return name in _dev_apps_cache


def _scan_ui_mtimes(ui_dir: Path) -> tuple[int, int]:
    """Return (file_count, digest) summarizing the app's ui/ tree.

    Follows the ui/ symlink (Path.rglob resolves through it) so the
    symlink-to-source dev setup is watched at the real source. Bounded by
    _MAX_SCAN_FILES; errors count as "no change" rather than crashing the loop.

    ``digest`` folds every file's (relative path, mtime, size) into a single
    order-independent accumulator (XOR of per-file hashes). Incorporating each
    file's own mtime and size means any edit that changes a file's metadata
    changes the digest — a bare ``(count, max_mtime)`` signature would miss a
    rewrite whose new mtime stays below another file's mtime (``cp -p``/
    ``rsync -a`` of an older bundle, a clock skew, a future-dated pin), leaving
    both count and max unchanged. XOR keeps the digest insensitive to rglob's
    traversal order.
    """
    digest = 0
    count = 0
    try:
        for p in ui_dir.rglob("*"):
            if count >= _MAX_SCAN_FILES:
                break
            try:
                if p.is_file():
                    count += 1
                    st = p.stat()
                    rel = p.relative_to(ui_dir).as_posix()
                    digest ^= hash((rel, st.st_mtime_ns, st.st_size)) & _DIGEST_MASK
            except (OSError, ValueError):
                continue
    except OSError:
        return (0, 0)
    return (count, digest)


async def _watch_loop(broadcast_fn: Callable[[str, dict], None]) -> None:
    """Poll dev-mode apps' ui/ dirs; broadcast ``app_reload`` on change.

    Steady-state cost when NO app is in dev mode: one ``stat()`` of the sentinel
    per tick — no ``list_apps()``, no ``ui/`` walks, no writes. The dev-app set
    is re-read (off the event loop) only when the sentinel's stat changes.

    Per-app state is (file_count, digest): a changed count catches
    adds/deletes, a changed digest catches edits (each file's mtime + size is
    folded in). The first observation of an app only seeds state (no reload
    storm at startup or on dev-mode enable).
    """
    state: dict[str, tuple[int, int]] = {}
    sentinel_sig: tuple[float, int] | None = None
    dev_apps: set[str] = set()
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_SECS)
            # Cheap change-detection: stat one small file off the event loop.
            sig = await asyncio.to_thread(_stat_sentinel)
            if sig != sentinel_sig:
                sentinel_sig = sig
                dev_apps = await asyncio.to_thread(_load_dev_apps)
                _set_dev_cache(dev_apps)
                # Drop state for apps that left dev mode so re-enabling re-seeds.
                for gone in set(state) - dev_apps:
                    state.pop(gone, None)
            # Walk ui/ trees ONLY for dev apps (inherent to the feature; zero
            # dev apps => zero walks). Off the event loop — the rglob/stat walk
            # is synchronous filesystem IO (no-blocking-call-on-event-loop rule).
            for name in dev_apps:
                sig2 = await asyncio.to_thread(_scan_ui_mtimes, app_dir(name) / "ui")
                prev = state.get(name)
                state[name] = sig2
                if prev is not None and sig2 != prev and sig2 != (0, 0):
                    logger.info("app dev mode: %s ui changed — broadcasting reload", name)
                    try:
                        broadcast_fn("app_reload", {"app": name, "ts": time.time()})
                    except Exception:
                        logger.debug("app_reload broadcast failed", exc_info=True)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("app dev-mode watcher error")
            await asyncio.sleep(POLL_INTERVAL_SECS)


async def init_dev_mode_watcher(broadcast_fn: Callable[[str, dict], None]) -> None:
    """Start the singleton watcher task (idempotent). Called at gateway startup.

    Async because the one-time startup seed touches the filesystem: it walks
    every app's ``installed.json`` to reconcile the sentinel (so the documented
    ``dev`` field is authoritative even after an out-of-band write) and seeds
    the UI-serving cache. That read is offloaded via ``asyncio.to_thread`` so it
    never blocks the gateway's event loop during startup
    (``no-blocking-call-on-event-loop``).
    """
    global _watch_task
    if _watch_task is not None and not _watch_task.done():
        return
    # Reconcile the sentinel from installed.json and seed the in-memory cache
    # once, off the event loop, so the UI hot path is correct immediately at
    # startup rather than after the first tick.
    await asyncio.to_thread(_reconcile_sentinel_from_installed)
    _watch_task = asyncio.get_running_loop().create_task(_watch_loop(broadcast_fn))
    logger.info("app dev-mode watcher started (%.0fs cadence)", POLL_INTERVAL_SECS)


async def stop_dev_mode_watcher() -> None:
    """Cancel the watcher task and await its teardown (shutdown / tests).

    Awaits the cancelled task so the coroutine has actually unwound before we
    return — an in-process restart can then start a fresh watcher without the
    old one lingering on the event loop with a stale ``broadcast_ws``.
    """
    global _watch_task
    task = _watch_task
    _watch_task = None
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
