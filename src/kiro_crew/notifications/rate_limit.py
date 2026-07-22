"""Per-app rate limiting for notification pushes (RFC Phase 2).

Token bucket per app: sustained rate of 30 notifications per 5 minutes with a
burst of 10. Deliberately coarse — the goal is stopping a buggy loop from
flooding the notification center, not fine-grained fairness (RFC "Rate
limiting"). System channels never pass through this limiter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# 30 notifications per 5 minutes sustained, burst of 10 (RFC Phase 2).
RATE_LIMIT_TOKENS_PER_WINDOW = 30
RATE_LIMIT_WINDOW_SECS = 300.0
RATE_LIMIT_BURST = 10

_REFILL_RATE = RATE_LIMIT_TOKENS_PER_WINDOW / RATE_LIMIT_WINDOW_SECS  # tokens/sec


@dataclass
class _Bucket:
    tokens: float = float(RATE_LIMIT_BURST)
    last_refill: float = field(default_factory=time.monotonic)


class AppRateLimiter:
    """Token bucket per app name. Single-threaded (asyncio event loop) use.

    Buckets are never evicted; growth is bounded because callers only pass
    names of installed, enabled apps (the push handler denies unknown apps
    with 403 before consulting the limiter), and each bucket is two floats.
    Entries for later-uninstalled apps linger harmlessly for the gateway's
    lifetime -- an accepted trade-off over eviction bookkeeping.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}

    def allow(self, app_name: str) -> bool:
        """Consume one token for *app_name*; False when the budget is exhausted."""
        now = time.monotonic()
        bucket = self._buckets.get(app_name)
        if bucket is None:
            bucket = _Bucket(last_refill=now)
            self._buckets[app_name] = bucket
        elapsed = now - bucket.last_refill
        bucket.tokens = min(float(RATE_LIMIT_BURST), bucket.tokens + elapsed * _REFILL_RATE)
        bucket.last_refill = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True
        return False

    def refund(self, app_name: str) -> None:
        """Return one token to *app_name*'s bucket (capped at burst).

        For callers whose request consumed a token via :meth:`allow` but then
        failed without delivering anything -- the budget caps DELIVERED
        notifications, so non-delivering failures must not drain it.
        """
        bucket = self._buckets.get(app_name)
        if bucket is not None:
            bucket.tokens = min(float(RATE_LIMIT_BURST), bucket.tokens + 1.0)
