"""Pinned files for Mochi — pin list, JSON persistence, file-change tracking.

Ported from ``src/main/pinnedFilesService.ts``. The pet's side panel shows a
list of files the agent (or user) pinned; the service persists that list and
reports when a pinned file changes or disappears on disk.

PORTING NOTES
-------------

**The clock and both timers are injected.** The original read ``Date.now()`` in
three places (pinnedAt, updatedAt, the corrupted-backup filename) and ran two
kinds of ``setTimeout``: a 100ms per-file debounce on watch events, and a 1s
retry that re-establishes a watcher after a file vanishes (editors that save
via write-temp-then-rename briefly delete the file). As throughout
the ported modules, the owner drives time: mutating entry points take
``now_ms`` and ``tick(now_ms)`` fires whatever timers became due.

**Debounce and retry SHARE the per-file timer slot.** The original stored both
in one ``debounceTimers`` map keyed by path. Consequences that are behaviour,
not accident: ``_stop_watcher`` cancels whichever of the two is pending, and a
new watch event arriving during the retry window REPLACES the pending retry
with a fresh debounce. Both are preserved by keeping the single ``_timers``
dict.

**Replacing a timer moves it to the END of the dict.** ``clearTimeout`` + new
``setTimeout`` yields a new timer id, so when several timers share a deadline
the replaced one fires LAST. A plain ``dict[key] = value`` keeps the original
insertion position and would fire it first — hence pop-then-reinsert in
``on_watch_event``. Observable as broadcast order when two files' events land
on the same tick.

**An absent ``updatedAt`` key is the wire format, not ``None``.**
``JSON.stringify`` drops ``undefined`` properties, so both the persisted file
and every broadcast payload simply omit the key until a change is seen, and
``markSeen`` removes it again. The port stores the key only while set.

**OS-level file watching is the owner's job.** The original called ``fs.watch``
directly; Python has no stdlib equivalent and the gateway will wire its own
mechanism. The port keeps everything *around* the watcher that is observable —
the watched-path set, the exists-on-disk gate, the no-duplicate gate, start on
load/pin, stop on unpin/delete — and the owner feeds raw events into
``on_watch_event``.

**Watch events are NOT gated on a registered watcher.** The original
``handleWatchEvent`` never consulted the watchers map, so an event for a path
that was never pinned and does not exist still broadcasts
``pinned:file-deleted``. Preserved: it ships today, and the retry it schedules
is harmless (the pin check makes it a no-op).

**Garbage pin entries load, persist and round-trip unvalidated.** ``load``
checks only that ``pins`` is an array. JS property access on a non-object
yields ``undefined`` and never throws, so a numeric entry simply never matches
a path and never starts a watcher; ``_entry_path`` mirrors that.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from kiro_crew.atomic_write import atomic_write
from kiro_crew.platform_compat import file_lock

logger = logging.getLogger(__name__)

# Broadcast callback: (channel, *args) -> None, mirroring the original BroadcastFn.
BroadcastFn = Callable[..., None]

# Soft limit to prevent excessive watchers.
MAX_PINS = 50

# Per-file debounce for rapid watch events.
DEBOUNCE_MS = 100

# Delay before attempting to re-establish a watcher after the file vanished
# (handles atomic saves: write-to-temp-then-rename briefly deletes the file).
REWATCH_RETRY_MS = 1_000

DATA_FILE_NAME = "pinned-files.json"
DATA_VERSION = 1

# Timer actions stored in the shared per-file slot.
_ACTION_DEBOUNCE = "debounce"

# Distinguishes "never polled" from "polled, file absent" (both would be None).
_UNSEEN = object()
_ACTION_RETRY = "retry"


def _entry_path(entry: Any) -> Any:
    """JS property access never throws: ``(1).path`` is ``undefined``. Mirror it."""
    return entry.get("path") if isinstance(entry, dict) else None


def _snapshot_pins(pins: list[Any]) -> list[Any]:
    """Copy dict entries for a rollback snapshot; keep any malformed non-dict
    entry (e.g. a stray scalar persisted on disk) AS-IS. ``dict(1)`` raises
    ``TypeError``, so an unconditional ``dict(p)`` would turn a preserved bad
    entry into a crash (HTTP 500) on an otherwise-valid unpin/mark-seen."""
    return [dict(p) if isinstance(p, dict) else p for p in pins]


def _file_exists(path: Any) -> bool:
    """``fs.existsSync`` semantics: any invalid input is simply "does not exist"."""
    if not isinstance(path, str) or not path:
        return False
    try:
        return os.path.exists(path)
    except (OSError, ValueError):
        return False


@contextmanager
def pins_mutation(file_path: str) -> Iterator[None]:
    """Cross-process lock for pin-list read-modify-write sequences.

    Two independent writers touch this file: the gateway's service (which also
    holds the list in memory to watch each path) and the MCP server process (a
    separate process, where the agent's `pin_file` / `unpin_file` land). The write
    itself is atomic, but the sequence is not — and because the service persists
    its WHOLE in-memory list, a pin the agent had just written to disk was deleted
    by the service's next mutation. Same defect, same remedy as the queue and the
    watchlist: take this lock and re-read before applying.

    A sibling ``.lock`` file, so the lock fd is never the file being atomically
    replaced.
    """
    lock_path = Path(file_path).with_name(Path(file_path).name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with file_lock(fd, exclusive=True):
            yield
    finally:
        os.close(fd)


class PinnedFilesService:
    """Persistent pin list with per-file change tracking."""

    def __init__(self, data_dir: str, broadcast: BroadcastFn | None = None) -> None:
        self._file_path = os.path.join(data_dir, DATA_FILE_NAME)
        self._broadcast: BroadcastFn = broadcast or (lambda channel, *args: None)
        self._pins: list[Any] = []
        # file path -> (mtime, size) | None(absent) — baseline for poll_file_changes
        self._mtimes: dict[str, Any] = {}
        self._watched: set[str] = set()
        # Shared per-file timer slot: path -> (deadline_ms, action tuple).
        # Holds EITHER a pending debounce OR a pending re-watch retry — see the
        # module docstring for why sharing the slot is load-bearing.
        self._timers: dict[str, tuple[int, tuple[Any, ...]]] = {}

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def load(self, now_ms: int) -> None:
        """Load pins from disk and start tracking them. Called once at startup.

        ``now_ms`` names the backup file if the on-disk state is corrupt.
        """
        # Clean up watchers/timers from any previous load to prevent leaks.
        self.dispose()

        try:
            # errors="replace" mirrors Node's utf-8 decode (U+FFFD, no throw);
            # corruption then surfaces as a JSON parse failure, same as there.
            raw = Path(self._file_path).read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            self._pins = []
            return
        except OSError as err:
            logger.warning("[PinnedFilesService] Read error: %s", err)
            self._pins = []
            return

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[PinnedFilesService] Corrupted JSON in %s, backing up", self._file_path)
            self._backup_corrupted(now_ms)
            self._pins = []
            return

        if not isinstance(parsed, dict) or not isinstance(parsed.get("pins"), list):
            logger.warning("[PinnedFilesService] Invalid shape in %s, resetting", self._file_path)
            self._backup_corrupted(now_ms)
            self._pins = []
            return

        self._pins = parsed["pins"]

        # Start tracking all loaded pins.
        for entry in self._pins:
            self._start_watcher(_entry_path(entry))

    def dispose(self) -> None:
        """Stop tracking everything and drop pending timers. Called on shutdown."""
        self._watched.clear()
        self._timers.clear()

    def _reload_pins_from_disk(self) -> None:
        """Refresh ``_pins`` from the file without touching watchers or timers.

        Cheaper and narrower than ``load``: it exists so a mutation can pick up
        another process's write before applying its own, without tearing down the
        watcher state ``load`` rebuilds.
        """
        try:
            raw = Path(self._file_path).read_text(encoding="utf-8", errors="replace")
            parsed = json.loads(raw)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        except OSError as err:
            logger.warning("[PinnedFilesService] reload read error: %s", err)
            return
        if isinstance(parsed, dict) and isinstance(parsed.get("pins"), list):
            self._pins = parsed["pins"]

    # ── Public API ─────────────────────────────────────────────────────────

    def add_pin(self, file_path: str, label: str | None = None, *, now_ms: int) -> bool:
        """Add a pin. False if the path is relative, sensitive, duplicate, or the
        list is full."""
        if not os.path.isabs(file_path):
            return False

        # Same gate as the MCP tool that is the live creation path today — see
        # there for why a pin on a credential file is a disclosure channel even
        # though nothing reads the contents. Checked here too so a future caller
        # cannot reach the poller without passing it.
        from kiro_crew.security import is_sensitive_path

        if is_sensitive_path(file_path):
            logger.warning("[PinnedFilesService] Refusing to pin a sensitive path")
            return False

        with pins_mutation(self._file_path):
            # Pick up the other process's writes BEFORE deciding, or a duplicate
            # check against stale state re-adds a pin the agent just removed and
            # the persist below drops the one it just added.
            self._reload_pins_from_disk()

            if len(self._pins) >= MAX_PINS:
                return False

            if any(_entry_path(p) == file_path for p in self._pins):
                return False

            entry: dict[str, Any] = {
                "path": file_path,
                # `label or ...`: an empty string falls back to the basename, matching
                # the original's `label || path.basename(filePath)`.
                "label": label or os.path.basename(file_path),
                "pinnedAt": now_ms,
                # No "updatedAt" key: absence IS the unset state (module docstring).
            }
            before = _snapshot_pins(self._pins)  # pre-mutation snapshot for rollback
            self._pins.append(entry)
            if not self._persist():
                # Write failed — roll the append back so memory matches disk and
                # report failure rather than a phantom pin that vanishes on restart.
                self._pins = before
                return False

        self._broadcast("pinned:files-changed", self.get_pins())
        self._start_watcher(file_path)
        return True

    def remove_pin(self, file_path: str) -> bool:
        """Remove a pin. False if not found."""
        with pins_mutation(self._file_path):
            self._reload_pins_from_disk()
            idx = next((i for i, p in enumerate(self._pins) if _entry_path(p) == file_path), -1)
            if idx == -1:
                return False

            before = _snapshot_pins(self._pins)  # pre-mutation snapshot for rollback
            self._pins.pop(idx)
            if not self._persist():
                # Write failed — restore the pin so memory matches disk and report
                # failure rather than a success that reverts on the next restart.
                self._pins = before
                return False

        self._stop_watcher(file_path)
        self._broadcast("pinned:files-changed", self.get_pins())
        return True

    def get_pins(self) -> list[Any]:
        """Current pin list (shallow copy, like the original's spread)."""
        return list(self._pins)

    def mark_seen(self, file_path: str) -> None:
        """Clear a file's ``updatedAt``. Persists and broadcasts even when it was
        already unset — the original assigned ``undefined`` unconditionally."""
        with pins_mutation(self._file_path):
            self._reload_pins_from_disk()
            entry = next((p for p in self._pins if _entry_path(p) == file_path), None)
            if entry is None:
                return

            before = _snapshot_pins(self._pins)  # pre-mutation snapshot for rollback
            entry.pop("updatedAt", None)
            if not self._persist():
                self._pins = before  # roll back; nothing durably changed
                return
        self._broadcast("pinned:files-changed", self.get_pins())

    def get_watched_paths(self) -> set[str]:
        """The set of paths currently being tracked (copy)."""
        return set(self._watched)

    # ── Watch events (fed by the owner's file-watching mechanism) ─────────

    def on_watch_event(self, file_path: str, event_type: str, *, now_ms: int) -> None:
        """Record a raw watch event; the debounced effect fires via ``tick``.

        Replaces whatever timer the file already had — including a pending
        re-watch retry — and the pop-then-reinsert moves the slot to the end of
        the dict, matching the fresh-``setTimeout``-id ordering (see docstring).
        """
        self._timers.pop(file_path, None)
        self._timers[file_path] = (now_ms + DEBOUNCE_MS, (_ACTION_DEBOUNCE, event_type))

    def tick(self, now_ms: int) -> None:
        """Fire every timer whose deadline has passed, in deadline order.

        Timers scheduled *while firing* (the re-watch retry) use ``now_ms`` as
        their base, so they can never fire within the same tick.
        """
        while True:
            due = [(f, d, a) for f, (d, a) in self._timers.items() if d <= now_ms]
            due.sort(key=lambda item: item[1])
            if not due:
                return
            for file_path, deadline, action in due:
                # Skip if an earlier firing in this batch replaced or cleared it.
                if self._timers.get(file_path) != (deadline, action):
                    continue
                del self._timers[file_path]
                if action[0] == _ACTION_DEBOUNCE:
                    self._process_watch_event(file_path, action[1], now_ms)
                else:
                    self._retry_watch(file_path, now_ms)

    # ── Internal ───────────────────────────────────────────────────────────

    def poll_file_changes(self, now_ms: int) -> None:
        """Detect changes to watched files and feed :meth:`on_watch_event`.

        DEVIATION from the original, which used Node's ``fs.watch`` for push
        notifications. Python has no stdlib file watcher, and the project rule is
        to prefer stdlib over a third-party dependency, so the owner loop polls
        mtime/size instead. Everything downstream is unchanged — this only
        replaces the event SOURCE, so the 100ms debounce, the
        ``pinned:file-updated`` / ``pinned:file-deleted`` broadcasts and the
        atomic-save re-watch retry all still run through ``on_watch_event`` and
        ``tick`` exactly as before.

        Cost of the deviation: detection latency is bounded by the owner loop's
        cadence (1s) instead of being immediate. For the feature this drives —
        marking a pinned file as changed — that is not observable.

        Size is compared alongside mtime because a write inside the same
        coarse mtime tick would otherwise be missed.
        """
        for file_path in list(self._watched):
            try:
                st = os.stat(file_path)
                sig: tuple[float, int] | None = (st.st_mtime, st.st_size)
            except OSError:
                sig = None  # gone (or unreadable) — treat as a delete

            prev = self._mtimes.get(file_path, _UNSEEN)
            if prev is _UNSEEN:
                # First sighting: record the baseline, don't report a change.
                self._mtimes[file_path] = sig
                continue
            if sig == prev:
                continue

            self._mtimes[file_path] = sig
            self.on_watch_event(file_path, "rename" if sig is None else "change", now_ms=now_ms)

    def _start_watcher(self, file_path: Any) -> None:
        # A non-string path can never be watched (existsSync would be false),
        # and JS Map.has tolerates anything — so bail before touching the set.
        if not isinstance(file_path, str):
            return
        # Don't start duplicate watchers.
        if file_path in self._watched:
            return
        # Don't watch files that don't exist on disk.
        if not _file_exists(file_path):
            return
        self._watched.add(file_path)

    def _stop_watcher(self, file_path: str) -> None:
        self._watched.discard(file_path)
        # Clears whichever is pending for this file — debounce OR retry.
        self._timers.pop(file_path, None)

    def _process_watch_event(self, file_path: str, event_type: str, now_ms: int) -> None:
        # Check existence rather than trusting event_type — on macOS both
        # 'rename' and 'change' can fire for deletions.
        if not _file_exists(file_path):
            self._broadcast("pinned:file-deleted", {"path": file_path})
            self._stop_watcher(file_path)
            # Re-check after a delay: atomic saves delete-then-recreate.
            self._timers[file_path] = (now_ms + REWATCH_RETRY_MS, (_ACTION_RETRY,))
            return

        with pins_mutation(self._file_path):
            self._reload_pins_from_disk()
            entry = next((p for p in self._pins if _entry_path(p) == file_path), None)
            if entry is None:
                return
            before = _snapshot_pins(self._pins)  # pre-mutation snapshot for rollback
            entry["updatedAt"] = now_ms
            if not self._persist():
                self._pins = before  # roll back; skip the broadcast
                return
        self._broadcast("pinned:file-updated", {"path": file_path, "updatedAt": now_ms})

    def _retry_watch(self, file_path: str, now_ms: int) -> None:
        if any(_entry_path(p) == file_path for p in self._pins) and _file_exists(file_path):
            self._start_watcher(file_path)
            # Treat reappearance as an update — called directly, not re-debounced.
            self._process_watch_event(file_path, "change", now_ms)

    def _persist(self) -> bool:
        """Atomic write (tmp + rename, 0600). Returns True on success, False on a
        write failure (e.g. a full data home) after logging. The caller MUST treat
        False as "the mutation did not happen" — roll its in-memory change back and
        report failure — otherwise an unpin would return success now yet reappear
        on the next restart, and memory would diverge from disk (atomic_write
        leaves the previous file intact on failure)."""
        data = {"version": DATA_VERSION, "pins": self._pins}
        try:
            atomic_write(self._file_path, json.dumps(data, indent=2), mode=0o600)
            return True
        except OSError as err:
            logger.error("[PinnedFilesService] Persist failed: %s", err)
            return False

    def _backup_corrupted(self, now_ms: int) -> None:
        bak_path = f"{self._file_path}.bak.{now_ms}"
        try:
            os.rename(self._file_path, bak_path)
            logger.warning("[PinnedFilesService] Backed up corrupted file to %s", bak_path)
        except OSError:
            pass  # file may not exist
