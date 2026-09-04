"""Warm-pool prewarming for the KiroCrew MCP gateway.

The gateway spawns a backend lazily on a stub's first non-register frame
(:func:`gatewayd._acquire_backend`). That lazy path costs a full
spawn + ``initialize`` round-trip — tens to hundreds of ms — on the
*first* new-chat after the daemon (re)starts or after every backend has
idled out. Repeated new-chats inside ``idle_timeout_secs`` already hit a
warm backend; only the cold-after-restart / cold-after-idle case is slow.

Prewarming closes that gap by spawning the most-used backends *before*
the first stub connects, so their PoolKeys are already in the pool and a
matching stub register short-circuits straight to the warm backend.

Two halves, both deliberately cheap on the event loop:

* **Observation** — :class:`HotKeyStore` keeps an in-memory hit count per
  PoolKey, updated on every register (O(1), zero IO — recording must
  never block the loop). A periodic flush task persists the counts to
  ``hot-keys.json`` via ``asyncio.to_thread`` so the on-loop path stays
  IO-free.
* **Prewarm** — at startup :func:`prewarm_from_payloads` reads the top-N
  persisted register payloads, rebuilds each :class:`PoolKey` via
  :meth:`PoolKey.from_register`, and spawns the backend through the same
  ``acquire`` callable the live path uses, so a prewarmed backend is
  byte-identical to a lazily-spawned one.

A PoolKey carries no channel dimension (see :mod:`kiro_crew.mcp_gateway.pool`),
so a hot key is not tied to one conversation surface: a backend prewarmed from
an observed key is hit by every later session that matches on agent, server and
the execution/security dimensions, whichever channel it arrives from.
Prewarming by observed hot key is therefore sound without any shared-fallback
path.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from kiro_crew.atomic_write import atomic_write
from kiro_crew.mcp_gateway.pool import PoolKey
from kiro_crew.metrics.provider import get_recorder

logger = logging.getLogger(__name__)

#: Default basename for the persisted hot-key file. Lives beside the
#: gateway socket (``$KIROCREW_HOME/mcp-gateway/``) so it shares the
#: 0700-protected directory and travels with the daemon's runtime state.
HOT_KEYS_FILENAME = "hot-keys.json"

#: Cap on distinct keys retained in the store / persisted file. Bounds
#: both memory and file size; far above any real (agent x server x channel)
#: working set, so it only ever clips pathological churn.
_MAX_TRACKED_KEYS = 512

#: Time-based expiry. A key not seen within this window is dropped on the
#: next load() and flush(), so a backend that's fallen out of use stops being
#: prewarmed and stops occupying a slot — without waiting for 512 hotter keys
#: to evict it. 14 days comfortably spans a work cycle (a vacation, a sprint
#: boundary) so a still-active-but-quiet key is never expired prematurely.
_MAX_KEY_AGE_SECS = 14 * 24 * 60 * 60  # 14 days

#: In-memory high-water mark. record() runs on the event loop and does no IO,
#: but the flush slice above bounds only the *file* — _entries itself would
#: grow with every distinct PoolKey the daemon ever sees. When the live map
#: crosses this ceiling, record() prunes it back to _MAX_TRACKED_KEYS in-memory
#: (TTL-stale first, then coldest) — pure CPU, no disk IO — so RAM is bounded
#: between flushes, not just on disk. 2x gives headroom so the prune amortizes
#: (it runs at most once per _MAX_TRACKED_KEYS newly-seen keys).
_PRUNE_HIGH_WATER = _MAX_TRACKED_KEYS * 2


class _Entry:
    """Mutable per-PoolKey record: the original register payload (needed to
    rebuild the key at prewarm time) plus a hit count and last-seen stamp.
    """

    __slots__ = ("register", "hits", "last_seen")

    def __init__(self, register: dict[str, Any], hits: int, last_seen: float) -> None:
        self.register = register
        self.hits = hits
        self.last_seen = last_seen


class HotKeyStore:
    """In-memory tally of how often each PoolKey registers, with periodic
    persistence to disk.

    Keyed by :meth:`PoolKey.stable_hash` so the same logical backend
    (same agent x server x channel x config) accumulates across new-chats.
    :meth:`record` / :meth:`record_outcome` are touched from the event loop
    and do no IO; :meth:`flush` / :meth:`load` do the IO and run via
    ``asyncio.to_thread`` (a thread-pool thread). Because those two sides run
    on different threads, all access to the shared mutable state (``_entries``,
    ``_hits``, ``_misses``, ``_dirty``) is guarded by ``_lock``. The lock is
    held only for in-memory work — never across the file write — so the
    event-loop path is never blocked on disk IO: :meth:`flush` snapshots under
    the lock, releases it, then writes.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: dict[str, _Entry] = {}
        self._dirty = False
        # Guards _entries / _hits / _misses / _dirty against the event-loop
        # writers (record / record_outcome) racing the thread-pool readers
        # (flush / load). Held briefly for memory ops only; the file write in
        # flush() happens after the lock is released.
        self._lock = threading.Lock()
        # Cumulative warm-hit tally, persisted alongside the keys so the hit
        # rate survives a gatewayd respawn (the daemon restarts on idle/crash,
        # which would otherwise zero an in-memory counter). A "hit" is a stub
        # register that found a live backend already in the pool (prewarmed,
        # or warm from a prior chat); a "miss" is a register that fell through
        # to a lazy spawn. Surfaced to the dashboard via the stats frame.
        self._hits = 0
        self._misses = 0

    @property
    def path(self) -> Path:
        return self._path

    def record_outcome(self, *, hit: bool) -> None:
        """Tally one warm-pool outcome for a stub register. On-loop, no IO.

        ``hit=True`` when the register matched a live pooled backend;
        ``hit=False`` when it had to spawn. Cheap counter bump only — the
        flush sweeper persists it, so this never blocks the handshake.
        """
        with self._lock:
            if hit:
                self._hits += 1
            else:
                self._misses += 1
            self._dirty = True
        # OTEL metric: warm-pool acquire counter (hit vs miss).
        get_recorder().counter(
            "kirocrew.mcp.warm_pool.acquire",
            attrs={"result": "hit" if hit else "miss"},
        )

    def hit_stats(self) -> dict[str, int]:
        """Point-in-time warm-pool hit tally for the dashboard / tests.

        ``rate_pct`` is the integer hit percentage (0 when no registers seen
        yet), pre-computed here so every consumer reports it identically.
        """
        with self._lock:
            hits, misses = self._hits, self._misses
        total = hits + misses
        return {
            "warm_pool_hits": hits,
            "warm_pool_misses": misses,
            "warm_pool_hit_rate_pct": round(100 * hits / total) if total else 0,
        }

    def record(self, register: dict[str, Any]) -> None:
        """Bump the hit count for ``register``'s PoolKey. On-loop, no IO.

        Malformed payloads are ignored — recording is best-effort
        telemetry and must never raise into the connection handler.
        """
        try:
            digest = PoolKey.from_register(register).stable_hash()
        except Exception:
            # A payload that won't parse can't be prewarmed anyway; drop it.
            return
        now = time.time()
        with self._lock:
            entry = self._entries.get(digest)
            if entry is None:
                # Store a shallow copy so later mutation of the caller's dict
                # (the handler reuses the frame) can't corrupt our snapshot.
                self._entries[digest] = _Entry(dict(register), 1, now)
            else:
                entry.hits += 1
                entry.last_seen = now
            # Bound RAM between flushes: the flush slice caps only the file, so
            # without this the live map grows with every distinct key the daemon
            # ever sees. Prune in-memory (no IO) once we cross the high-water
            # mark. Cheap: triggers at most once per _MAX_TRACKED_KEYS new keys.
            if len(self._entries) > _PRUNE_HIGH_WATER:
                self._prune_locked(now)
            self._dirty = True

    def _prune_locked(self, now: float) -> None:
        """Shrink ``_entries`` to the hottest ``_MAX_TRACKED_KEYS`` live keys.

        Drops TTL-expired keys first (they'd never be prewarmed anyway), then,
        if still over the cap, the coldest by ``(hits, last_seen)``. Caller must
        hold ``_lock``; does no IO.
        """
        fresh = {
            digest: entry
            for digest, entry in self._entries.items()
            if now - entry.last_seen <= _MAX_KEY_AGE_SECS
        }
        if len(fresh) > _MAX_TRACKED_KEYS:
            kept = sorted(
                fresh.items(),
                key=lambda kv: (kv[1].hits, kv[1].last_seen),
                reverse=True,
            )[:_MAX_TRACKED_KEYS]
            fresh = dict(kept)
        self._entries = fresh

    def top_register_payloads(self, count: int) -> list[dict[str, Any]]:
        """Return up to ``count`` register payloads, most-hit first.

        Ties broken by most-recently-seen so a recently-active key wins a
        prewarm slot over a stale one with equal hits.
        """
        if count <= 0:
            return []
        with self._lock:
            ordered = sorted(
                self._entries.values(),
                key=lambda e: (e.hits, e.last_seen),
                reverse=True,
            )
            return [e.register for e in ordered[:count]]

    # --- IO (run via asyncio.to_thread — never on the event loop) --------

    def load(self) -> None:
        """Populate the store from the persisted file, if present.

        Never raises: a missing, empty, or corrupt file just yields an
        empty store. Blocking IO — call from a thread.
        """
        try:
            raw = self._path.read_text()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("hot-keys: could not read %s: %s", self._path, exc)
            return
        try:
            data = json.loads(raw)
            records = data["keys"] if isinstance(data, dict) else data
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("hot-keys: ignoring corrupt %s: %s", self._path, exc)
            return
        # Restore the totals under the lock too (a concurrent record_outcome
        # could otherwise interleave with the assignment above on the no-GIL
        # build). Parse the records into a local dict first (no shared state),
        # then publish entries + totals atomically under the lock.
        parsed: dict[str, _Entry] = {}
        now = time.time()
        if isinstance(records, list):
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                register = rec.get("register")
                if not isinstance(register, dict):
                    continue
                try:
                    digest = PoolKey.from_register(register).stable_hash()
                    hits = int(rec.get("hits", 1) or 1)
                    last_seen = float(rec.get("last_seen", 0.0) or 0.0)
                except Exception:
                    # A corrupt record (bad register, or non-numeric hits /
                    # last_seen) is skipped, not raised — load() promises a
                    # corrupt file "just yields an empty store" and runs at
                    # daemon startup, so it must degrade rather than propagate.
                    continue
                # TTL: a key not seen within the window is stale — don't load it
                # (so we never prewarm a backend that's fallen out of use). The
                # next flush rewrites the file without it, self-healing on disk.
                if now - last_seen > _MAX_KEY_AGE_SECS:
                    continue
                parsed[digest] = _Entry(register, hits, last_seen)
        # Defense-in-depth: cap loaded entries to _MAX_TRACKED_KEYS so a
        # corrupted/bloated file cannot blow up memory on start.
        if len(parsed) > _MAX_TRACKED_KEYS:
            top = sorted(
                parsed.items(),
                key=lambda kv: (kv[1].hits, kv[1].last_seen),
                reverse=True,
            )[:_MAX_TRACKED_KEYS]
            parsed = dict(top)
        with self._lock:
            if isinstance(data, dict) and isinstance(data.get("totals"), dict):
                try:
                    self._hits = max(0, int(data["totals"].get("hits", 0)))
                    self._misses = max(0, int(data["totals"].get("misses", 0)))
                except (TypeError, ValueError):
                    self._hits = self._misses = 0
            self._entries.update(parsed)
        logger.info("hot-keys: loaded %d tracked key(s) from %s", len(parsed), self._path)

    def flush(self) -> bool:
        """Atomically persist the current tally to disk if it changed.

        Writes to a temp file in the same directory then ``os.replace``s
        it into place so a reader never sees a half-written file. Returns
        ``True`` if a write happened. Blocking IO — call from a thread.

        The shared state is snapshotted under ``_lock`` (guarding against a
        concurrent ``record`` mutating ``_entries`` mid-iteration — which would
        raise ``RuntimeError: dictionary changed size during iteration`` even
        under the GIL); ``_dirty`` is cleared under the same lock BEFORE the
        write so a ``record`` that fires during the write re-arms the dirty
        flag and is captured by the next flush, never silently dropped. The
        file IO itself happens after the lock is released.
        """
        with self._lock:
            if not self._dirty:
                return False
            # TTL: drop keys not seen within the window so the persisted file
            # never carries stale targets (mirrors the load() filter, so a key
            # expired here stays gone on the next startup).
            now = time.time()
            live = [
                e for e in self._entries.values()
                if now - e.last_seen <= _MAX_KEY_AGE_SECS
            ]
            # Keep only the hottest _MAX_TRACKED_KEYS so the file stays bounded.
            ordered = sorted(
                live,
                key=lambda e: (e.hits, e.last_seen),
                reverse=True,
            )[:_MAX_TRACKED_KEYS]
            payload = {
                "version": 1,
                "totals": {"hits": self._hits, "misses": self._misses},
                "keys": [
                    {"register": e.register, "hits": e.hits, "last_seen": e.last_seen}
                    for e in ordered
                ],
            }
            # Clear BEFORE the write (under the lock): a record() during the
            # write re-sets _dirty and is caught next flush. Re-arm on IO
            # failure below so a failed write is retried.
            self._dirty = False
        try:
            # Keep the explicit mode: atomic_write's own parent mkdir does not
            # set one, and this directory holds identity-bearing keys.
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # restrict_to_owner is passed unconditionally where this used to
            # guard it behind `not IS_POSIX`. On POSIX it is os.chmod(0o600),
            # which the fchmod_safe(0o600) it replaces already achieved, so the
            # resulting mode is identical; the helper applies it to the temp
            # file before any content reaches it, which is the ordering this
            # site already used on Windows. It stays fail-loud, and the OSError
            # still lands in the handler below that re-arms _dirty, so a failed
            # write is retried rather than silently dropped.
            #
            # json.dumps, not json.dump into the handle: the helper owns the
            # file object. Output is ASCII-only (ensure_ascii defaults to True)
            # and carries no newline, so neither the switch to utf-8 nor
            # universal-newline translation can change a byte.
            atomic_write(self._path, json.dumps(payload), restrict_to_owner=True)
        except OSError as exc:
            logger.warning("hot-keys: could not write %s: %s", self._path, exc)
            # Re-arm so a failed write is retried on the next flush rather than
            # lost (we cleared _dirty optimistically before the write).
            with self._lock:
                self._dirty = True
            return False
        return True


def default_hot_keys_path(socket_path: Path) -> Path:
    """Hot-keys file location derived from the gateway socket path — the
    sibling ``hot-keys.json`` in the runtime directory.
    """
    return Path(socket_path).parent / HOT_KEYS_FILENAME


#: An ``acquire`` callable spawns/returns the backend for a PoolKey. This is
#: the gateway's spawn-or-reuse closure wrapping :func:`gatewayd._acquire_backend`
#: (it returns just the backend, unpacking that function's
#: ``(backend, was_spawned)`` tuple), typed loosely here to avoid importing the
#: Backend type from gatewayd and creating a circular import.
AcquireFn = Callable[[PoolKey], Awaitable[Any]]


async def prewarm_from_payloads(
    payloads: list[dict[str, Any]],
    acquire: AcquireFn,
    *,
    limit: int,
    unreserve: Callable[[PoolKey], None] | None = None,
) -> int:
    """Spawn backends for up to ``limit`` register payloads, hottest first.

    Each payload is turned back into a :class:`PoolKey` and handed to
    ``acquire`` (the gateway's own spawn-or-reuse path), so the resulting
    backend is identical to one the lazy path would have produced and is
    deduped by the pool if it somehow already exists.

    The warmed backend is then pinned (``Backend.pinned = True``) so the idle
    sweeper and LRU victim selection leave it in place — a prewarmed backend
    sits at ``refcount == 0`` until a stub attaches, so without pinning the
    next idle sweep would reclaim it and the warm slot would be wasted.

    One payload failing (unknown target, spawn error, malformed key) is
    logged and skipped — it must not abort the remaining prewarms. Returns
    the count successfully warmed.
    """
    if limit <= 0 or not payloads:
        return 0
    warmed = 0
    for register in payloads[:limit]:
        try:
            pool_key = PoolKey.from_register(register)
        except Exception as exc:
            logger.warning("prewarm: skipping malformed key: %s", exc)
            continue
        try:
            backend = await acquire(pool_key)
            if backend is None:
                # Nothing was spawned/reused — do NOT count it as warmed, or
                # the reported total would overstate the live warm set.
                logger.warning(
                    "prewarm: acquire returned no backend for %s; skipping",
                    pool_key.human_readable(),
                )
                continue
            # Pin so the idle/LRU reclaim paths keep this warm backend ready.
            # Best-effort: a resolver/test double may return something without
            # the attribute, which must not abort the prewarm pass.
            try:
                backend.pinned = True
            except AttributeError:
                pass
            # Release the reservation acquire() took. The backend is pinned
            # so the idle/LRU sweeper still leaves it in place — but a
            # reserved digest is skipped by evict_idle regardless of pin, so
            # without this the later unpin/reclaim pass could never reclaim a
            # prewarmed backend that no live stub ever attached to.
            if unreserve is not None:
                unreserve(pool_key)
            warmed += 1
            logger.info("prewarm: warmed %s", pool_key.human_readable())
        except Exception as exc:
            # Unknown target / spawn failure / breaker-open: skip, keep going.
            logger.warning(
                "prewarm: could not warm %s: %s", pool_key.human_readable(), exc
            )
    logger.info("prewarm: warmed %d/%d backend(s)", warmed, min(limit, len(payloads)))
    return warmed
