"""Cache containers and invalidation coordination for conversation history.

The facade ``ConversationLog`` continues to own every cache and the process-wide
generation registry.  This module owns only the cache data structures and the
protocol that coordinates those facade-held objects; it never proxies arbitrary
attributes and never becomes a second repository for transcript state.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Container
from typing import TYPE_CHECKING, Generic, NamedTuple, TypeVar

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog

logger = logging.getLogger(__name__)

#: Default upper bound on the number of distinct session keys held in the
#: in-memory transcript caches. Unbounded ``dict`` caches grow one entry per
#: session key touched and never evict; a bounded LRU keeps hot sessions
#: resident while giving the working set a deterministic ceiling.
#:
#: This bound no longer covers the METADATA cache, which is sized separately by
#: ``_METADATA_CACHE_MAX`` below for the reasons documented there.
_TRANSCRIPT_CACHE_MAX = 256

#: Upper bound for ``ConversationLog._meta_cache`` specifically, kept separate
#: from ``_TRANSCRIPT_CACHE_MAX`` above.
#:
#: That bound is sized for the PARSED MESSAGE caches, whose entries are whole
#: transcript windows. A metadata entry is one parsed first line — title, agent,
#: created_at, tab_id, folder_id — measured in hundreds of bytes, so 256 buys
#: almost nothing in RAM terms and costs a great deal in I/O: ``list_sessions``
#: scans the WHOLE session directory in one cyclic pass, so an LRU smaller than
#: the corpus is evicted in exactly the order it will next be read and its hit
#: rate collapses to ~0. Measured on an 810-session store: a warm
#: ``list_sessions`` re-opened and re-parsed ~554 first lines on every call
#: (57.9 ms); with the cache able to hold the corpus the same call costs 24.5 ms,
#: all of it the unavoidable ``glob`` + one ``stat`` per file.
#:
#: This is the same failure mode ``_SearchTextCache`` already documents and
#: fixes — an LRU collapsing to a zero hit rate against the cyclic scan order —
#: applied to the memo every sidebar page fetch depends on. Still bounded, so a
#: gateway touching an unbounded number of sessions cannot grow without limit.
_METADATA_CACHE_MAX = 8192

_V = TypeVar("_V")


class _FileChangeCacheEntry(NamedTuple):
    """Lightweight projection of one unchanged transcript revision."""

    stamp: tuple[int, int, int, int]
    generation: int
    messages: list[dict]


class _LRUCache(Generic[_V]):
    """A tiny bounded LRU cache with a dict-compatible surface.

    Backed by an :class:`collections.OrderedDict`; the most recently
    accessed key is kept at the end and eviction pops from the front
    (least-recently-used), so eviction order is fully deterministic for a
    given access sequence. Supports the subset of the mapping protocol the
    caller relies on (``get`` / ``__getitem__`` / ``__setitem__`` / ``pop`` /
    ``__contains__`` / ``__len__`` / ``clear``). Both reads (``get`` /
    ``__getitem__``) and writes mark a key as recently used.

    ``maxsize <= 0`` disables bounding (behaves like an ordinary dict) so a
    caller can opt out without a code-path split.

    Thread safety: the same :class:`ConversationLog` instance is touched from
    the event loop *and* from worker threads (``chat_persistence`` flush /
    restore, ``chat_regenerate`` / ``chat_rewind`` via ``asyncio.to_thread``,
    ``handlers/cron`` and ``slack/gateway`` off-loop ``read_messages`` calls).
    Every method therefore takes ``self._lock`` so each is atomic and the
    compound read-modify-write sequences (``move_to_end`` + index in ``get`` /
    ``__getitem__``; the eviction ``len()`` + ``popitem`` loop in
    ``__setitem__``) cannot interleave with a concurrent ``pop`` / ``clear``.
    Without it a concurrent ``pop`` landing in the bytecode gap between a
    successful ``move_to_end`` and the following index raised ``KeyError``
    (crashing the request/background task) instead of returning the default.
    The operations are tiny in-memory dict ops, so lock contention is
    negligible. A plain :class:`threading.Lock` suffices -- no method calls
    another locked method, so the lock is never re-entered.
    """

    def __init__(self, maxsize: int = _TRANSCRIPT_CACHE_MAX) -> None:
        self._maxsize = maxsize
        self._data: "OrderedDict[str, _V]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str, default: _V | None = None) -> _V | None:
        with self._lock:
            try:
                self._data.move_to_end(key)
            except KeyError:
                return default
            return self._data[key]

    def __getitem__(self, key: str) -> _V:
        with self._lock:
            self._data.move_to_end(key)
            return self._data[key]

    def __setitem__(self, key: str, value: _V) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            # Evict least-recently-used entries until within the bound.
            if self._maxsize > 0:
                while len(self._data) > self._maxsize:
                    self._data.popitem(last=False)

    def pop(self, key: str, default: _V | None = None) -> _V | None:
        with self._lock:
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

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class _SearchTextCache(Generic[_V]):
    """Byte-budgeted memo for the two derived corpora ``search_sessions`` needs.

    Distinct from :class:`_LRUCache` because the access pattern is different in
    the one way that decides an eviction policy. A search walks its bounded
    session window **in the same recency order every query**, so the working set
    is cyclic. Under LRU a cyclic scan larger than the cache evicts each entry
    exactly one step before its next read: the hit rate does not degrade, it
    collapses to zero, and it does so for the users with the most sessions --
    the ones the memo exists for.

    An entry *count* is the wrong bound to carry that guarantee, because a
    session can approach the transcript byte ceiling. The bound here is
    therefore **bytes**, which is what actually has to fit in the gateway's RSS.

    A byte budget reintroduces the cliff, so eviction is replaced by
    **admission control**: when a new entry would exceed the budget it is simply
    not stored, and the entries already held are kept. For a cyclic scan that
    turns 0% into (whatever fraction fits)% -- and because ``search_sessions``
    walks most-recent-first, the fraction that stays cached is the most recently
    active sessions, which is the half worth keeping. Existing entries are still
    replaced in place on a content change (same key, new value), so a session
    that is being written to never gets stuck on a stale value.

    ``max_bytes <= 0`` disables the bound (behaves like a plain dict).

    Thread safety mirrors :class:`_LRUCache`: one lock, every method atomic, no
    method calls another locked method.
    """

    def __init__(self, max_bytes: int, sizer: Callable[[_V], int], label: str = "") -> None:
        self._max_bytes = max_bytes
        self._sizer = sizer
        self._label = label
        self._data: dict[str, _V] = {}
        self._bytes = 0
        self._admitted = 0
        self._refused = 0
        self._refused_since_prune = 0
        self._warned = False
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

        The release valve that keeps admission control from becoming a one-way
        ratchet. Without it, a cache that fills freezes on whatever it happened
        to hold first: entries that have since aged out of the scan window keep
        their budget forever, while every newly created session is refused --
        so the newest and most searched sessions become exactly the cold ones,
        and warm latency regresses over process lifetime until a restart.

        Safe against the LRU cliff this class exists to avoid, because it only
        drops entries a search can no longer reach: anything outside the live
        scan window is dead weight rather than an entry one step from its next
        read. Called only under pressure so an uncontended cache never pays for
        the scan.
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
        """Invalidate every facade-held cache spelling after a write."""
        idents = self._log._cache_key_identities(key)
        # Bump BEFORE dropping entries: a fill publishing between a pop and a
        # later bump would pass its re-check and resurrect the entry just
        # dropped. Bump-first makes every fill storing after a pop self-discard.
        self._log._bump_cache_gen(key, idents)
        # Pops must be exactly as wide as the generation bump. The writer and
        # reader may use different logical, sanitized, canonical, or legacy
        # spellings for the same transcript; under-popping one spelling leaves
        # a warm entry permanently stale after an mtime-preserving rewrite.
        for ident in idents:
            self._log._msg_cache.pop(ident, None)
            self._log._meta_cache.pop(ident, None)
            self._log._file_change_cache.pop(ident, None)
            self._log._tab_id_by_key.pop(ident, None)
            self._log._folded_cache.pop(ident, None)
            self._log._snippet_cache.pop(ident, None)
            self._log._recent_cache.pop_prefix(f"{ident}\x00")
        # The tab-id rebuild samples this counter before a metadata read. One
        # bump per invalidation prevents a late store from resurrecting a stale
        # id after the identity-wide pops above.
        self._log._tab_id_generation += 1
