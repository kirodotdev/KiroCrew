"""Persistent skill-usage ledger — hotness ranking for lazy skill injection.

The session-start skills block (``SkillsLoader.get_context``) can only afford a
bounded slice of the context budget, so it injects the *hottest* skills (by how
often they've actually been used) and leaves the long tail discoverable via the
``skill_search`` tool. This module is the hotness store behind that ranking.

Design mirrors an MCP-gateway-style warm-pool ``HotKeyStore``: an in-memory
tally per key with a time-based TTL and atomic persistence — but simplified for
a *synchronous* caller. Skill usage is recorded from the body-delivery loop in the context builder
(sync, once per triggered body actually injected) and from ``load_skill``
via ``resolve_dollar_skills``, not from an async event-loop hot path, so:

* ``record`` bumps an in-memory counter and only touches disk on a debounce
  (at most once per ``_FLUSH_DEBOUNCE_SECS``), so the per-message path stays
  IO-free in the common case.
* ``load`` runs once at ``SkillsLoader`` construction; ``get_context`` reads
  the in-memory tally with zero IO.

Losing up to a debounce window of counts on a hard crash is acceptable — this
is a ranking heuristic, not durable state. A *shutdown* is not a crash, though:
the gateway's exit paths call ``flush_all_for_shutdown`` before ``os._exit``
and, through a pre-exec stop, before an in-place restart's ``os.execv`` (both
skip atexit), so the hits accepted inside the last debounce window reach disk. The file is best-effort
throughout: a missing / corrupt file yields an empty ledger and is self-healed
on next flush.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
import time
import weakref
from pathlib import Path
from typing import Protocol

from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

#: Observer notified when a tool call reads a skill body, so reads that bypass
#: the loader (a file-read tool, or ``cat`` in a shell) still reach the ledger.
#: A module-level slot rather than constructor plumbing, mirroring
#: ``get_global_hook_store``: the ACP layer sees every surface's tool calls but
#: has no route to a SkillsLoader. This holds a process-wide hook, never
#: per-caller data, so it does not make any MCP tool stateful.
#:
#: Two phases on purpose. ``resolve_tool_read_keys`` is filesystem-bound, so the
#: caller runs it off the event loop; ``credit_skill_reads`` is in-memory and
#: runs only once the read is confirmed complete, so a denied or failed tool
#: call leaves no delivery behind.


class SkillReadObserver(Protocol):
    def resolve_tool_read_keys(
        self,
        tool_name: str = "",
        raw_params: dict | None = None,
        command: str | None = None,
    ) -> list[str]: ...

    def credit_skill_reads(self, keys: list[str]) -> None: ...


_global_skill_read_observer: SkillReadObserver | None = None


def set_global_skill_read_observer(observer: SkillReadObserver | None) -> None:
    """Install (or clear, with ``None``) the process-wide skill-read observer."""
    global _global_skill_read_observer
    _global_skill_read_observer = observer


def get_global_skill_read_observer() -> SkillReadObserver | None:
    """The installed skill-read observer, or ``None`` when nothing registered."""
    return _global_skill_read_observer


def register_skill_read_observer(*candidates: object) -> bool:
    """Install the observer from the first candidate exposing a skills loader.

    Credits the ledger for skill bodies the model reads directly — a file-read
    tool or ``cat`` bypasses the loader, so those reads recorded nothing and the
    ledger described one access route only.

    Takes several candidates because the object holding the loader differs by
    runtime: the dashboard has ``state.context_builder``, while the API-server
    path builds its state without one and reaches it through the task runner.
    Returns whether an observer was installed, so a caller can log a miss rather
    than silently recording nothing.

    Lives here rather than in the dashboard: every runtime that owns a
    ContextBuilder calls it, so homing it in one surface's module made the others
    import that surface just to reach it.
    """
    for candidate in candidates:
        skills = getattr(candidate, "skills", None)
        if skills is not None:
            set_global_skill_read_observer(skills)
            return True
    return False


#: Basename every skill body lives under.
_SKILL_FILE = "SKILL.md"


def names_skill_file(raw_params: dict | None, command: str | None) -> bool:
    """Whether a tool call's arguments name a skill body at all.

    A name-agnostic substring scan, so a tool call that touches no skill costs no
    filesystem work. Whether the call is a content-delivering READ is decided
    after this. The scan also looks inside the objects of a sequence, because
    kiro-cli's ``read`` tool batches its targets as
    ``{"operations": [{"mode": "Line", "path": ...}]}``. Arguments are
    model-authored, so any other shape is ignored.

    Lives here so the ACP pre-filter and the loader's rename diagnostic share
    one scan: the ACP layer must not import the skills machinery.
    """
    if isinstance(command, str) and _SKILL_FILE in command:
        return True
    if not isinstance(raw_params, dict):
        return False
    for value in raw_params.values():
        if isinstance(value, str):
            if _SKILL_FILE in value:
                return True
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and _SKILL_FILE in item:
                    return True
                if isinstance(item, dict) and any(
                    isinstance(v, str) and _SKILL_FILE in v for v in item.values()
                ):
                    return True
    return False


#: Basename of the persisted ledger, under ``$KIROCREW_HOME``.
SKILL_USAGE_FILENAME = "skill-usage.json"

#: A skill not used within this window is dropped on the next load / flush, so
#: a skill that's fallen out of use stops occupying a top-K slot. 30 days spans
#: a comfortable work cycle without expiring a still-relevant-but-quiet skill.
_MAX_AGE_SECS = 30 * 24 * 60 * 60

#: Bound the tracked set. Far above any real skill working set; only clips
#: pathological churn. Keeps both RAM and file size bounded.
_MAX_TRACKED = 1024

#: Minimum seconds between disk writes. record() runs per-message; without this
#: a chatty session would fsync on every turn. 60s bounds the write rate while
#: keeping the ledger fresh enough across restarts.
_FLUSH_DEBOUNCE_SECS = 60.0

#: Upper bound on the gateway's shutdown flush. One small JSON file per ledger;
#: the bound only matters for a wedged disk, which must delay the exit, never
#: hold it.
SHUTDOWN_FLUSH_SECS = 2.0

#: Every ledger alive in this process, so the gateway's exit path can flush them
#: without knowing which loader owns which. Weak, so registering never keeps a
#: ledger (or the loader that dropped it) alive.
_live_ledgers: weakref.WeakSet[SkillUsageLedger] = weakref.WeakSet()
_live_ledgers_lock = threading.Lock()


def flush_all_for_shutdown() -> bool:
    """Flush every live ledger. Returns True when none was left unwritten.

    The gateway exits through ``os._exit``, which skips atexit and kills the
    daemon flush thread, so a hit ``record`` accepted inside the debounce window
    would die in memory. This is the shutdown write for that path. Synchronous
    and filesystem-bound: the caller runs it off the loop, under a timeout.
    Never raises.
    """
    with _live_ledgers_lock:
        ledgers = list(_live_ledgers)
    ok = True
    for ledger in ledgers:
        try:
            ledger.flush()
        except Exception:
            logger.debug("skill-usage: shutdown flush failed", exc_info=True)
            ok = False
        else:
            ok = ok and not ledger.dirty
    return ok


async def flush_all_before_exec() -> None:
    """``flush_all_for_shutdown`` off-loop, bounded by ``SHUTDOWN_FLUSH_SECS``.

    The pre-exec stop for the in-place restarts (the dashboard's restart and the
    update coordinator's), which end in ``os.execv``: like ``os._exit``, the exec
    kills the daemon flush thread and skips atexit. Registered at gateway boot
    with ``pre_exec_stops.register_pre_exec_stop``.
    """
    if not await asyncio.wait_for(
        asyncio.to_thread(flush_all_for_shutdown), timeout=SHUTDOWN_FLUSH_SECS
    ):
        logger.warning("skill-usage: the ledger did not fully flush before restart")


class SkillUsageLedger:
    """In-memory per-skill usage tally with debounced, atomic persistence.

    Keyed by the skill's canonical key (its path from the skills root, e.g.
    ``code/builder-toolbox``). Thread-safe: ``record`` may be called from
    different session threads; all shared-state access is guarded by ``_lock``.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._hits: dict[str, int] = {}
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()
        # Held from snapshot to file replacement, so writes land in snapshot
        # order: a shutdown flush cannot be overwritten afterwards by an older
        # snapshot a daemon flush thread took first. Taken before ``_lock``.
        self._write_lock = threading.Lock()
        self._dirty = False
        self._last_flush = 0.0
        self._load()
        with _live_ledgers_lock:
            _live_ledgers.add(self)

    # --- read path (used by get_context ranking) -------------------------

    def score(self, key: str, *, recency_boost: float = 0.0) -> tuple[float, float]:
        """Return a sort key ``(hits, effective_last_seen)`` for ``key``.

        Higher sorts first. ``recency_boost`` (a unix-time value, typically the
        skill file's mtime) lets a freshly-added skill with zero usage still
        rank above stale unused skills — mitigating the rich-get-richer
        cold-start starvation where a new skill would otherwise never surface.
        """
        with self._lock:
            hits = self._hits.get(key, 0)
            last_seen = self._last_seen.get(key, 0.0)
        return (float(hits), max(last_seen, recency_boost))

    def snapshot(self) -> dict[str, tuple[int, float]]:
        """Return a thread-safe copy of ``{key: (hits, last_seen)}`` for all live entries.

        Used by the budget endpoint to compute cross-key alias folds without
        holding the lock for the duration of the filesystem resolution pass.
        """
        with self._lock:
            return {
                k: (self._hits[k], self._last_seen.get(k, 0.0))
                for k in self._hits
            }

    # --- write path (used by body-delivery in context builder / resolve_dollar_skills) ----------

    def record(self, key: str) -> None:
        """Bump ``key``'s usage. In-memory O(1); flushes to disk on a debounce.

        Best-effort: never raises into the caller (recording is telemetry).
        """
        if not key:
            return
        now = time.time()
        should_flush = False
        with self._lock:
            self._hits[key] = self._hits.get(key, 0) + 1
            self._last_seen[key] = now
            self._dirty = True
            if len(self._hits) > _MAX_TRACKED:
                self._prune_locked(now)
            if now - self._last_flush >= _FLUSH_DEBOUNCE_SECS:
                self._last_flush = now
                should_flush = True
        if should_flush:
            # record() runs on the gateway event loop (via the body-delivery loop /
            # resolve_dollar_skills), so the blocking file write is offloaded to a
            # daemon thread. flush() snapshots the tally under the lock before any
            # IO, so a background write is safe and keeps the loop unblocked.
            threading.Thread(
                target=self.flush, name="skill-usage-flush", daemon=True
            ).start()

    def _prune_locked(self, now: float) -> None:
        """Shrink to the hottest ``_MAX_TRACKED`` live keys. Caller holds lock."""
        fresh = {
            k: self._hits[k]
            for k in self._hits
            if now - self._last_seen.get(k, 0.0) <= _MAX_AGE_SECS
        }
        if len(fresh) > _MAX_TRACKED:
            kept = sorted(
                fresh, key=lambda k: (fresh[k], self._last_seen.get(k, 0.0)), reverse=True
            )[:_MAX_TRACKED]
            fresh = {k: fresh[k] for k in kept}
        self._hits = fresh
        self._last_seen = {k: self._last_seen.get(k, 0.0) for k in fresh}

    # --- IO --------------------------------------------------------------

    def _load(self) -> None:
        """Populate from the persisted file, dropping TTL-stale entries."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("skill-usage: could not read %s: %s", self._path, exc)
            return
        try:
            data = json.loads(raw)
            keys = data.get("keys", {}) if isinstance(data, dict) else {}
        except (ValueError, AttributeError, TypeError) as exc:
            logger.warning("skill-usage: ignoring corrupt %s: %s", self._path, exc)
            return
        now = time.time()
        hits: dict[str, int] = {}
        last_seen: dict[str, float] = {}
        if isinstance(keys, dict):
            for key, rec in keys.items():
                if not isinstance(rec, dict):
                    continue
                try:
                    ls = float(rec.get("last_seen", 0.0) or 0.0)
                    h = int(rec.get("hits", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(ls) or now - ls > _MAX_AGE_SECS:
                    # TTL-stale, or a time no TTL can expire (NaN, Infinity).
                    # Dropping it here self-heals the file on the next flush.
                    continue
                hits[key] = h
                last_seen[key] = ls
        with self._lock:
            self._hits = hits
            self._last_seen = last_seen
        logger.info("skill-usage: loaded %d tracked skill(s) from %s", len(hits), self._path)

    @property
    def dirty(self) -> bool:
        """Whether accepted hits are still waiting for a write."""
        with self._lock:
            return self._dirty

    def flush(self) -> bool:
        """Atomically persist the tally if it changed. Returns True on write.

        Serialized on ``_write_lock``, so a call waits for an in-flight daemon
        write and nothing older can land after it.
        """
        with self._write_lock:
            return self._flush_serialized()

    def _flush_serialized(self) -> bool:
        """``flush`` body. Caller holds ``_write_lock``."""
        with self._lock:
            if not self._dirty:
                return False
            now = time.time()
            live = [
                (k, self._hits[k], self._last_seen.get(k, 0.0))
                for k in self._hits
                if now - self._last_seen.get(k, 0.0) <= _MAX_AGE_SECS
            ]
            live.sort(key=lambda t: (t[1], t[2]), reverse=True)
            live = live[:_MAX_TRACKED]
            payload = {
                "version": 1,
                "keys": {k: {"hits": h, "last_seen": ls} for k, h, ls in live},
            }
            self._dirty = False
        try:
            # The shared helper, not a hand-rolled temp-file dance: it applies
            # the mode through ``platform_compat.fchmod_safe`` (a no-op where
            # ``os.fchmod`` does not exist) and retries the rename against the
            # Windows window in which an indexer or AV scanner holds a transient
            # handle on either path. Naming ``os.fchmod`` directly would raise
            # AttributeError there, which the handler below does NOT catch: it
            # would escape the method with ``_dirty`` already cleared, so this
            # snapshot is dropped instead of being retried by that handler.
            atomic_write(self._path, json.dumps(payload), mode=0o600)
        except OSError as exc:
            logger.warning("skill-usage: could not write %s: %s", self._path, exc)
            with self._lock:
                self._dirty = True  # re-arm so the failed write retries
            return False
        return True
