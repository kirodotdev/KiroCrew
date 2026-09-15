"""Circuit breaker for fast-dying MCP backends.

When a backend repeatedly crashes within seconds of spawning, the spawn
loop becomes a livelock: every register triggers a respawn that triggers
another crash. The circuit breaker stops that loop by tripping OPEN
after N short-lived deaths in a sliding window and refusing further
:meth:`CircuitBreaker.allow` probes for a cooldown period.

State is keyed by an opaque string (typically ``server_name`` from
:class:`kiro_crew.mcp_gateway.pool.PoolKey`) so a misbehaving server
isolates only its own pool slot rather than blocking the whole gateway.

Threading: methods are sync and assume single-threaded callers — asyncio
tasks on one event loop. The gateway's existing call sites already meet
this contract; if the breaker is ever shared across OS threads, wrap
calls with an external :class:`threading.Lock`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# A backend that exits within this many seconds of spawn is treated as
# a "fast death" — the host process didn't even reach steady idle.
# Longer-lived backends that crash later are addressed by the heartbeat
# path, not the breaker.
FAST_DEATH_THRESHOLD_SECS = 5.0

# Number of fast deaths within the sliding window before the breaker
# trips OPEN for the affected key.
FAST_DEATH_LIMIT = 5

# Sliding-window length used when counting fast deaths. Deaths older
# than this are pruned and stop contributing to the trip count.
WINDOW_SECS = 60.0

# Upper bound on distinct keys tracked in ``_states``. The gateway keys the
# breaker by ``PoolKey.stable_hash()`` (agent x server x execution x security x
# config), an open-ended space, so a long-lived daemon must prune fully-inactive
# keys to avoid unbounded growth (mirrors HotKeyStore's cap).
MAX_TRACKED_KEYS = 512

# Time the breaker stays OPEN once tripped. ``allow()`` returns False
# for the duration; after it elapses, the next call transitions back to
# CLOSED and the death history is cleared.
OPEN_COOLDOWN_SECS = 60.0


@dataclass
class _State:
    """Per-key bookkeeping. Internal — never expose to callers."""

    deaths: list[float] = field(default_factory=list)
    opened_at: Optional[float] = None


class CircuitBreaker:
    """Per-key breaker for fast-dying backends.

    See module docstring for the threading contract. ``now_fn`` lets
    tests inject a deterministic clock (a :func:`time.monotonic`-shaped
    callable returning seconds); production callers should leave it at
    the default.

    All tunables are constructor-injectable so the gateway can override
    them via config without monkey-patching module globals.
    """

    def __init__(
        self,
        *,
        fast_death_threshold_secs: float = FAST_DEATH_THRESHOLD_SECS,
        fast_death_limit: int = FAST_DEATH_LIMIT,
        window_secs: float = WINDOW_SECS,
        open_cooldown_secs: float = OPEN_COOLDOWN_SECS,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        if fast_death_limit < 1:
            raise ValueError(f"fast_death_limit must be >= 1, got {fast_death_limit}")
        if fast_death_threshold_secs <= 0:
            raise ValueError(
                f"fast_death_threshold_secs must be > 0, got {fast_death_threshold_secs}"
            )
        if window_secs <= 0:
            raise ValueError(f"window_secs must be > 0, got {window_secs}")
        if open_cooldown_secs <= 0:
            raise ValueError(
                f"open_cooldown_secs must be > 0, got {open_cooldown_secs}"
            )
        self._fast_threshold = fast_death_threshold_secs
        self._limit = fast_death_limit
        self._window = window_secs
        self._cooldown = open_cooldown_secs
        self._now = now_fn or time.monotonic
        self._states: dict[str, _State] = {}

    # --- public API --------------------------------------------------------

    def record_death(self, key: str, uptime_secs: float) -> None:
        """Record that the backend identified by ``key`` has exited.

        Only deaths with ``uptime_secs < fast_death_threshold_secs`` are
        counted toward the trip limit; longer-lived deaths are ignored
        here (they belong to other lifecycle paths). Once accumulated
        fast deaths in the window reach the limit, the key transitions
        to OPEN.
        """
        if uptime_secs >= self._fast_threshold:
            return
        state = self._states.setdefault(key, _State())
        now = self._now()
        # Prune deaths that have aged out of the sliding window so old
        # crashes from a previous incident do not contribute to trips.
        cutoff = now - self._window
        state.deaths = [t for t in state.deaths if t >= cutoff]
        state.deaths.append(now)
        if state.opened_at is None and len(state.deaths) >= self._limit:
            state.opened_at = now
            logger.warning(
                "circuit breaker OPEN key=%s deaths=%d window=%.1fs cooldown=%.1fs",
                key,
                len(state.deaths),
                self._window,
                self._cooldown,
            )
        if len(self._states) > MAX_TRACKED_KEYS:
            self._prune(now)

    def _prune(self, now: float) -> None:
        """Bound ``_states`` under an open-ended key space
        (``PoolKey.stable_hash()``). First drop fully-inactive keys (all
        deaths aged out of the sliding window and not currently OPEN); if still
        over the cap, hard-evict the oldest-by-``opened_at`` OPEN keys. An OPEN
        key that is never probed again is otherwise unreachable by ``allow()``
        / ``record_healthy()`` (they only touch a key on a later call for that
        exact key), so without this it would pin memory for the daemon
        lifetime. Dropping the oldest OPEN key just gives its next spawn a clean
        slate — and the oldest is the most likely to be past its cooldown."""
        cutoff = now - self._window
        for k in [
            k
            for k, s in self._states.items()
            if s.opened_at is None and all(t < cutoff for t in s.deaths)
        ]:
            del self._states[k]
        if len(self._states) <= MAX_TRACKED_KEYS:
            return
        oldest_open = sorted(
            (k for k, s in self._states.items() if s.opened_at is not None),
            key=lambda k: self._states[k].opened_at or 0.0,
        )
        for k in oldest_open:
            if len(self._states) <= MAX_TRACKED_KEYS:
                break
            del self._states[k]

    def record_healthy(self, key: str) -> None:
        """Mark ``key`` as having survived the fast-death window.

        Clears accumulated deaths and any OPEN state for the key. A
        no-op if the key has never been seen, so callers can wire this
        unconditionally on backend uptime crossing the threshold.
        """
        state = self._states.get(key)
        if state is None:
            return
        if state.opened_at is not None:
            logger.info("circuit breaker CLOSE-on-healthy key=%s", key)
        # Fully recovered: drop the entry rather than keep an empty _State
        # forever (the breaker is keyed by PoolKey.stable_hash(), an
        # open-ended space, so lingering empty entries leak memory).
        del self._states[key]

    def allow(self, key: str) -> bool:
        """Return ``True`` if the gateway should attempt a spawn for
        ``key``, ``False`` if the breaker is OPEN.

        Lazily auto-closes when the cooldown has elapsed: the first
        call after the timer expires transitions the key back to CLOSED
        and wipes its death history, so the next fast-death sequence
        starts from a clean slate.
        """
        state = self._states.get(key)
        if state is None or state.opened_at is None:
            return True
        if self._now() - state.opened_at >= self._cooldown:
            logger.info("circuit breaker CLOSE-on-cooldown key=%s", key)
            # Clean slate: drop the entry (next fast-death sequence recreates it).
            self._states.pop(key, None)
            return True
        return False
