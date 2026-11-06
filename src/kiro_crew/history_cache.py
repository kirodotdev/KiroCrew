"""Cache containers and invalidation coordination for conversation history.

The facade ``ConversationLog`` continues to own every cache and the process-wide
generation registry.  This module owns only the cache data structures and the
protocol that coordinates those facade-held objects; it never proxies arbitrary
attributes and never becomes a second repository for transcript state.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable, Container, Iterable
from typing import TYPE_CHECKING, Generic, NamedTuple, TypeVar

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog

logger = logging.getLogger(__name__)

#: Default upper bound on the number of distinct session keys held in the
#: in-memory transcript caches. Unbounded ``dict`` caches grow one entry per
#: session key touched and never evict; a bounded LRU keeps hot sessions
#: resident while giving the working set a deterministic ceiling.
#:
#: This bound does not cover the METADATA cache, which is sized separately by
#: ``_METADATA_CACHE_MAX`` below for the reasons documented there.
_TRANSCRIPT_CACHE_MAX = 256

#: Parsed transcripts unread this long are dropped from the message caches. A
#: boot scan or a restore parses hundreds of transcripts once; holding them all
#: for the life of the gateway was ~150 MB on an idle copy of a real data home.
#: A dropped entry is re-parsed on its next read, as on a cold cache.
_TRANSCRIPT_CACHE_IDLE_TTL = 600.0
_IDLE_SWEEP_SECS = 60.0

#: Byte budget of the parsed-transcript cache (``ConversationLog._msg_cache``).
#: An entry count alone does not bound it: one transcript can be 25,800 rows,
#: and a test gateway on a real data home held ~210 MiB there after opening 15
#: long sessions. Each entry is weighed at :data:`_PARSED_BYTES_PER_FILE_BYTE`
#: times its file size; past the budget the least recently used entries go, and
#: a transcript heavier than the whole budget is not cached (each full read of
#: it parses the file, as on a cold cache).
_TRANSCRIPT_CACHE_MAX_BYTES = 32 * 1024 * 1024
#: Retained bytes of a parsed transcript per byte of its file: 1.58-2.34 on
#: the five largest transcripts of one real data home (35-91 MiB files).
_PARSED_BYTES_PER_FILE_BYTE = 2

_idle_caches: "weakref.WeakSet[_LRUCache]" = weakref.WeakSet()
_idle_sweeper_lock = threading.Lock()
_idle_sweeper: threading.Thread | None = None


def _register_idle_cache(cache: "_LRUCache") -> None:
    """Track a cache for the idle sweep and start the sweeper once per process."""
    global _idle_sweeper
    with _idle_sweeper_lock:
        _idle_caches.add(cache)
        if _idle_sweeper is None or not _idle_sweeper.is_alive():
            _idle_sweeper = threading.Thread(
                target=_idle_sweep_loop, name="kc-history-cache-sweep", daemon=True
            )
            _idle_sweeper.start()


def sweep_idle_caches(now: float | None = None) -> int:
    """Drop idle entries from every registered cache. Returns how many."""
    with _idle_sweeper_lock:
        caches = list(_idle_caches)
    return sum(cache.drop_idle(now) for cache in caches)


def _idle_sweep_loop() -> None:
    while True:
        time.sleep(_IDLE_SWEEP_SECS)
        try:
            sweep_idle_caches()
        except Exception:  # noqa: BLE001 - a cache sweep must never kill its thread
            logger.debug("transcript cache idle sweep failed", exc_info=True)


#: Upper bound for ``ConversationLog._meta_cache``, separate from
#: ``_TRANSCRIPT_CACHE_MAX`` (sized for whole transcript windows). A metadata
#: entry is one parsed first line, hundreds of bytes. ``list_sessions`` scans the
#: whole directory in one cyclic pass, so an LRU smaller than the corpus hits ~0%
#: (an 810-session store: 57.9 ms warm vs 24.5 ms when the corpus fits). Still
#: bounded, so a gateway touching unbounded sessions cannot grow without limit.
_METADATA_CACHE_MAX = 8192


#: ``(mtime_ns, ctime_ns, size, inode, device)`` identifying one file revision.
#: ``ctime_ns`` cannot be set from user space, so a same-size in-place rewrite
#: that restores ``mtime`` still changes the stamp.
FileStamp = tuple[int, int, int, int, int]


class _TranscriptRowIndexEntry(NamedTuple):
    """Sparse byte offsets for one immutable transcript revision.

    ``checkpoints`` stores ``(logical_row, byte_offset)`` pairs for valid,
    non-metadata message rows. The file stamp and process-wide generation are
    both required: atomic replacement changes the inode, while a rewrite that
    restores mtime is exposed by the generation. A key with no transcript file
    carries ``stamp=None``: the same value a re-stat returns, so a not-yet-flushed
    slot is a stable, empty revision rather than a perpetually changing one.
    """

    stamp: FileStamp | None
    generation: int
    row_count: int
    checkpoints: tuple[tuple[int, int], ...]


class _TranscriptIndexBase(NamedTuple):
    """A row index kept across invalidation so an append-only file is not rescanned.

    ``ConversationLog.append`` drops the cached :class:`_TranscriptRowIndexEntry`
    on every write. The base records the index of bytes ``[0, scanned_end)``
    (a record boundary), the first line's length, and a digest of three windows
    after it: body start, last checkpoint, scan end. Identity is the content,
    never the inode, so a copy or atomic rewrite of the same rows still matches.
    A changed first-line length is checked with every offset shifted by it (a
    metadata or marker rewrite). Any other edit changes the digest or moves the
    end window, so the base is refused and the file rescanned.
    """

    head_len: int
    scanned_end: int
    digest: bytes
    row_count: int
    checkpoints: tuple[tuple[int, int], ...]


_V = TypeVar("_V")


class _FileChangeCacheEntry(NamedTuple):
    """Lightweight projection of one unchanged transcript revision."""

    stamp: tuple[int, int, int, int]
    generation: int
    messages: list[dict]


class _LRUCache(Generic[_V]):
    """A tiny bounded LRU cache with a dict-compatible surface.

    Backed by an :class:`collections.OrderedDict`: reads and writes move a key to
    the end, eviction pops the front, so eviction order is deterministic.
    ``maxsize <= 0`` disables bounding. ``max_weight > 0`` with ``weigh`` adds a
    summed-weight bound; a value heavier than ``max_weight`` alone is not stored.

    Thread safety: one :class:`ConversationLog` is used from the event loop and
    from worker threads, so every method takes ``self._lock``. Otherwise a
    concurrent ``pop`` between ``move_to_end`` and the index in ``get`` raises
    ``KeyError``. A plain :class:`threading.Lock` suffices: no method calls
    another locked method.
    """

    def __init__(
        self,
        maxsize: int = _TRANSCRIPT_CACHE_MAX,
        *,
        idle_ttl: float = 0.0,
        max_weight: int = 0,
        weigh: "Callable[[_V], int] | None" = None,
    ) -> None:
        self._maxsize = maxsize
        self._data: "OrderedDict[str, _V]" = OrderedDict()
        self._lock = threading.Lock()
        self._max_weight = max_weight if weigh is not None else 0
        self._weigh = weigh
        self._weights: dict[str, int] = {}
        self._total_weight = 0
        #: Seconds an entry may sit unread before the idle sweeper drops it;
        #: 0 keeps entries until LRU pressure evicts them. Stamps are kept in
        #: the same LRU order as ``_data``, so the oldest entry is always first.
        self._idle_ttl = idle_ttl
        self._stamps: dict[str, float] = {}
        if idle_ttl > 0:
            _register_idle_cache(self)

    def _forget(self, key: str) -> None:
        """Drop *key*'s stamp and weight; the caller holds the lock and removes the value."""
        self._stamps.pop(key, None)
        self._total_weight -= self._weights.pop(key, 0)

    def _touch(self, key: str) -> None:
        if self._idle_ttl > 0:
            self._stamps[key] = time.monotonic()

    def drop_idle(self, now: float | None = None) -> int:
        """Drop entries unread for ``idle_ttl`` seconds. Returns how many."""
        if self._idle_ttl <= 0:
            return 0
        cutoff = (time.monotonic() if now is None else now) - self._idle_ttl
        dropped = 0
        with self._lock:
            for key in list(self._data):
                if self._stamps.get(key, 0.0) > cutoff:
                    break  # LRU order: everything after this is newer
                del self._data[key]
                self._forget(key)
                dropped += 1
        return dropped

    def get(self, key: str, default: _V | None = None) -> _V | None:
        with self._lock:
            try:
                self._data.move_to_end(key)
            except KeyError:
                return default
            self._touch(key)
            return self._data[key]

    def __getitem__(self, key: str) -> _V:
        with self._lock:
            self._data.move_to_end(key)
            self._touch(key)
            return self._data[key]

    def __setitem__(self, key: str, value: _V) -> None:
        weight = self._weigh(value) if self._max_weight > 0 and self._weigh else 0
        with self._lock:
            if key in self._data:
                del self._data[key]
                self._forget(key)
            if self._max_weight > 0 and weight > self._max_weight:
                return  # heavier than the whole budget: never resident
            self._data[key] = value
            self._touch(key)
            if self._max_weight > 0:
                self._weights[key] = weight
                self._total_weight += weight
            # Evict least-recently-used entries until within both bounds.
            while (self._maxsize > 0 and len(self._data) > self._maxsize) or (
                self._max_weight > 0 and self._total_weight > self._max_weight
            ):
                old, _ = self._data.popitem(last=False)
                self._forget(old)

    def pop(self, key: str, default: _V | None = None) -> _V | None:
        with self._lock:
            self._forget(key)
            return self._data.pop(key, default)

    def pop_prefix(self, prefix: str) -> None:
        """Remove every entry whose string key starts with *prefix*.

        Used to invalidate all cached ``recent()`` windows for one session key
        (composite keys are ``"<key>\\x00<max>\\x00<roles>"``) without touching
        other sessions' entries. Atomic under the lock.
        """
        with self._lock:
            doomed = [k for k in self._data if isinstance(k, str) and k.startswith(prefix)]
            for key in doomed:
                del self._data[key]
                self._forget(key)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._stamps.clear()
            self._weights.clear()
            self._total_weight = 0

    def total_weight(self) -> int:
        """Summed weight of the resident entries (0 without a weight bound)."""
        with self._lock:
            return self._total_weight


class _SearchTextCache(Generic[_V]):
    """Byte-budgeted memo for the two derived corpora ``search_sessions`` needs.

    A search walks its session window in the same recency order every query, so
    under LRU a scan larger than the cache evicts each entry just before its next
    read and the hit rate collapses to zero. The bound is bytes (a session can
    approach the transcript byte ceiling), and eviction is replaced by admission
    control: a new entry that would exceed the budget is not stored. Since the
    walk is most-recent-first, the most recent sessions stay cached. A changed
    value for an existing key is still replaced in place.

    ``max_bytes <= 0`` disables the bound. Thread safety mirrors :class:`_LRUCache`.
    """

    def __init__(
        self,
        max_bytes: int,
        sizer: Callable[[_V], int],
        label: str = "",
        *,
        warn_on_refusal: bool = True,
    ) -> None:
        self._max_bytes = max_bytes
        self._sizer = sizer
        self._label = label
        self._data: dict[str, _V] = {}
        self._bytes = 0
        self._admitted = 0
        self._refused = 0
        self._refused_since_prune = 0
        # A memo whose refusals cost only recomputation, never a file read,
        # turns the warning off: reaching its ceiling is its normal state.
        self._warned = not warn_on_refusal
        self._lock = threading.Lock()

    def get(self, key: str, default: _V | None = None) -> _V | None:
        with self._lock:
            return self._data.get(key, default)

    def __setitem__(self, key: str, value: _V) -> None:
        cost = self._sizer(value)
        with self._lock:
            previous = self._data.get(key)
            if previous is not None:
                # Replacement, not growth: release the old cost first so a
                # changing session cannot inflate accounting or be refused
                # admission for its own new value.
                self._bytes -= self._sizer(previous)
                del self._data[key]
            if self._max_bytes > 0 and self._bytes + cost > self._max_bytes:
                self._refused += 1
                self._refused_since_prune += 1
                warn = not self._warned
                self._warned = True
                if warn:
                    logger.warning(
                        "search memo %s hit its %d MB ceiling (%d entries, %d "
                        "admitted); sessions past it fall back to re-reading "
                        "their file, so warm search will be slower on this "
                        "corpus",
                        self._label or "cache",
                        self._max_bytes // (1024 * 1024),
                        len(self._data),
                        self._admitted,
                    )
                return
            self._data[key] = value
            self._bytes += cost
            self._admitted += 1

    def pop(self, key: str, default: _V | None = None) -> _V | None:
        with self._lock:
            if key in self._data:
                value = self._data.pop(key)
                self._bytes -= self._sizer(value)
                return value
            return default

    def retain(self, live_keys: Container[str]) -> int:
        """Drop every entry whose key is not in *live_keys*; return how many.

        Without this, a full cache keeps whatever it held first and refuses every
        new session, so the newest sessions become the cold ones. It drops only
        entries outside the live scan window, which no search can reach. Called
        only under pressure, so an uncontended cache never pays for the scan.
        """
        with self._lock:
            doomed = [key for key in self._data if key not in live_keys]
            for key in doomed:
                self._bytes -= self._sizer(self._data.pop(key))
            self._refused_since_prune = 0
            return len(doomed)

    def refused_since_prune(self) -> int:
        """Admissions turned away since the last :meth:`retain`.

        Nonzero means the budget is binding right now, which is the signal to
        spend a prune. Distinct from the cumulative ``refused`` in
        :meth:`stats`, which never resets so it stays useful for diagnosis.
        """
        with self._lock:
            return self._refused_since_prune

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._bytes = 0

    def stats(self) -> dict[str, int]:
        """Observability for the budget: occupancy and admission refusals.

        ``refused`` rising while ``entries`` is flat means the budget is smaller
        than the working set and warm searches are paying cold-read cost for the
        sessions that did not fit.
        """
        with self._lock:
            return {
                "entries": len(self._data),
                "bytes": self._bytes,
                "max_bytes": self._max_bytes,
                "admitted": self._admitted,
                "refused": self._refused,
            }


class _GroupRecencyMemo(_SearchTextCache[_V]):
    """A :class:`_SearchTextCache` that admits by recency, one key group at a time.

    Plain admission control stops learning once full (later query terms are
    refused forever); per-entry LRU brings back the cyclic-scan cliff. So the
    eviction unit is a GROUP named by ``group_of(key)`` (the query term). A full
    memo evicts whole least-recently-used groups, never one pinned by a running
    query (:meth:`pinned`). With nothing evictable the entry is refused, so a
    query larger than the budget keeps a stable partial hit rate. An eviction
    counts toward :meth:`refused_since_prune` like a refusal.
    """

    def __init__(
        self,
        max_bytes: int,
        sizer: Callable[[_V], int],
        group_of: Callable[[str], str],
        label: str = "",
    ) -> None:
        super().__init__(max_bytes, sizer, label, warn_on_refusal=False)
        self._group_of = group_of
        #: group -> its keys, least recently used group first.
        self._groups: OrderedDict[str, set[str]] = OrderedDict()
        self._pins: dict[str, int] = {}
        self._evicted = 0

    def pinned(self, groups: Iterable[str]) -> _PinnedGroups:
        """Context manager keeping *groups* from eviction while a query runs."""
        return _PinnedGroups(self, tuple(dict.fromkeys(groups)))

    def _pin(self, groups: tuple[str, ...], delta: int) -> None:
        with self._lock:
            for group in groups:
                count = self._pins.get(group, 0) + delta
                if count > 0:
                    self._pins[group] = count
                else:
                    self._pins.pop(group, None)

    def get(self, key: str, default: _V | None = None) -> _V | None:
        with self._lock:
            value = self._data.get(key)
            if value is None:
                return default
            group = self._group_of(key)
            if group in self._groups:
                self._groups.move_to_end(group)
            return value

    def _drop(self, key: str) -> None:
        """Remove *key* and its group bookkeeping. Caller holds the lock."""
        self._bytes -= self._sizer(self._data.pop(key))
        group = self._group_of(key)
        members = self._groups.get(group)
        if members is not None:
            members.discard(key)
            if not members:
                del self._groups[group]

    def __setitem__(self, key: str, value: _V) -> None:
        cost = self._sizer(value)
        group = self._group_of(key)
        with self._lock:
            if key in self._data:
                self._drop(key)
            if self._max_bytes > 0 and self._bytes + cost > self._max_bytes:
                for victim in list(self._groups):
                    if victim == group or self._pins.get(victim):
                        continue
                    for member in list(self._groups[victim]):
                        self._drop(member)
                        self._evicted += 1
                    # Pressure, like a refusal: the signal to spend a prune.
                    self._refused_since_prune += 1
                    if self._bytes + cost <= self._max_bytes:
                        break
                if self._bytes + cost > self._max_bytes:
                    self._refused += 1
                    self._refused_since_prune += 1
                    return
            self._data[key] = value
            self._bytes += cost
            self._admitted += 1
            members = self._groups.get(group)
            if members is None:
                self._groups[group] = {key}
            else:
                members.add(key)
                self._groups.move_to_end(group)

    def pop(self, key: str, default: _V | None = None) -> _V | None:
        with self._lock:
            if key in self._data:
                value = self._data[key]
                self._drop(key)
                return value
            return default

    def retain(self, live_keys: Container[str]) -> int:
        with self._lock:
            doomed = [key for key in self._data if key not in live_keys]
            for key in doomed:
                self._drop(key)
            self._refused_since_prune = 0
            return len(doomed)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._groups.clear()
            self._bytes = 0

    def stats(self) -> dict[str, int]:
        out = super().stats()
        with self._lock:
            out["evicted"] = self._evicted
            out["groups"] = len(self._groups)
        return out


class _PinnedGroups:
    """``with memo.pinned(groups):`` -- see :meth:`_GroupRecencyMemo.pinned`."""

    __slots__ = ("_memo", "_groups")

    def __init__(self, memo: _GroupRecencyMemo, groups: tuple[str, ...]) -> None:
        self._memo = memo
        self._groups = groups

    def __enter__(self) -> None:
        self._memo._pin(self._groups, 1)

    def __exit__(self, *exc: object) -> None:
        self._memo._pin(self._groups, -1)


class HistoryCacheCoordinator:
    """Coordinate facade-held history caches and invalidation generations.

    The coordinator deliberately owns no cache state. ``ConversationLog`` keeps
    its cache attributes directly so existing diagnostics and tests can inspect
    them, and its class retains the process-wide generation registry. Every
    access here goes through the explicit ``self._log`` composition reference.
    """

    def __init__(
        self,
        log: ConversationLog,
        *,
        safe_key: Callable[[str], str],
        registry_owner: type[ConversationLog],
    ) -> None:
        self._log = log
        self._safe_key = safe_key
        self._registry_owner = registry_owner

    def _cache_gen(self, key: str) -> int:
        """Return *key*'s process-wide invalidation generation.

        The registry key is ``(transcript directory, sanitized filename stem)``
        so every ``ConversationLog`` instance over the same directory observes
        a writer's bump. Stem normalization is pure string math: generation
        snapshots never stat a transcript or resolve the legacy-file fallback.
        """
        gen_key = (str(self._log._dir), self._safe_key(key))
        with self._registry_owner._cache_gens_guard:
            return self._registry_owner._cache_gens.get(gen_key, 0)

    def _bump_cache_gen(self, key: str, idents: tuple[str, ...]) -> None:
        """Advance every identity bucket an invalidation closes over.

        *idents* is the facade's precomputed bidirectional identity closure, so
        a caller knowing only a logical key or a legacy/canonical stem moves
        every generation a reader of that transcript can snapshot. Computing
        the closure stays outside the registry lock.
        """
        base = str(self._log._dir)
        gen_keys = [(base, self._safe_key(ident)) for ident in idents]
        with self._registry_owner._cache_gens_guard:
            for gen_key in dict.fromkeys(gen_keys):
                self._registry_owner._cache_gens[gen_key] = (
                    self._registry_owner._cache_gens.get(gen_key, 0) + 1
                )
            epochs = self._registry_owner._dir_write_epochs
            epochs[base] = epochs.get(base, 0) + 1

    def _dir_write_epoch(self) -> int:
        """How many invalidations any instance over this directory has made.

        Moves whenever any session's generation does, so an unmoved value proves
        no write landed in this process between two reads of it.
        """
        with self._registry_owner._cache_gens_guard:
            return self._registry_owner._dir_write_epochs.get(str(self._log._dir), 0)

    def _publish_if_current(
        self,
        cache: _LRUCache[_V] | _SearchTextCache[_V],
        entry_key: str,
        value: _V,
        *,
        key: str,
        gen: int,
    ) -> None:
        """Publish *value* only while *key* remains at generation *gen*.

        The check-store-recheck shape closes every interleaving with
        :meth:`_invalidate_cache`, whose bump happens before its pops. A second
        fill may lose a fresh entry to the cleanup pop in the narrow race, but
        that costs only one re-read and can never return stale transcript data.
        """
        if gen != self._log._cache_gen(key):
            return
        cache[entry_key] = value
        if gen != self._log._cache_gen(key):
            cache.pop(entry_key, None)

    def _invalidate_cache(self, key: str) -> None:
        """Invalidate content caches and advance generations after a write.

        Sparse page indexes are revision-specific derived state, so they are
        invalidated with content caches. A later bounded read rebuilds from byte
        zero unless the exact file stamp and generation still match.
        """
        idents = self._log._cache_key_identities(key)
        # Bump BEFORE dropping entries: a fill publishing between a pop and a
        # later bump would pass its re-check and resurrect the entry just
        # dropped. Bump-first makes every fill storing after a pop self-discard.
        self._log._bump_cache_gen(key, idents)
        # Content-cache pops must be exactly as wide as the generation bump. The
        # writer and reader may use different logical, sanitized, canonical, or
        # legacy spellings for the same transcript; under-popping one spelling
        # leaves content or derived offsets stale after a rewrite.
        for ident in idents:
            self._log._msg_cache.pop(ident, None)
            self._log._page_index_cache.pop(ident, None)
            self._log._meta_cache.pop(ident, None)
            self._log._title_scan_cache.pop(ident, None)
            self._log._has_messages_cache.pop(ident, None)
            self._log._file_change_cache.pop(ident, None)
            self._log._tab_id_by_key.pop(ident, None)
            self._log._folded_cache.pop(ident, None)
            self._log._snippet_cache.pop(ident, None)
            self._log._recent_cache.pop_prefix(f"{ident}\x00")
        # The tab-id rebuild samples this counter before a metadata read. One
        # bump per invalidation prevents a late store from resurrecting a stale
        # id after the identity-wide pops above.
        self._log._tab_id_generation += 1
