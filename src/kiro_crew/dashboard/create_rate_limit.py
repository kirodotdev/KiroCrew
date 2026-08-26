"""Per-caller rate limiting for the dashboard's *creation* verbs.

The capacity ceilings elsewhere (``MAX_LIVE_SLOTS``, ``MAX_SLOTS_PER_CREATOR``,
``MAX_CHAT_FOLDERS``) bound how much of a resource can exist at once. They do not
bound the RATE, and rate is the property that matters for an auto-approved verb: a
caller whose approval prompt has been waived has nothing throttling a loop, so the
only cost of attempting exhaustion is time.

This is the primary control for that, and it is deliberately the one that needs no
durable state. A lifetime quota has to survive restarts to mean anything -- every
path that rehydrates a session would have to carry its attribution, and there are
27 such call sites -- whereas a window measured in minutes is not meaningfully
resettable: a restart buys the caller one window, not a clean slate. So the guard
that has to hold under adversarial conditions is the one with no persistence
requirement, and the capacity ceilings sit behind it as secondary bounds.

Why this cannot live in the agent's own instructions: the conductor reads untrusted
text by design, and content that can drive it into a creation loop can equally
override a "create at most N per round" line in its skill. Self-restraint is a
convention; this is a control. It is enforced here, for every caller, so it holds
regardless of which agent is calling or what it was told.

Budgets are per (verb, caller) so a folder burst cannot consume a caller's session
budget, and they are sized to leave honest work untouched: a decomposed goal files
one folder and opens on the order of ten sessions in its dispatch round, which fits
inside a single window with headroom. At the session rate, filling
``MAX_LIVE_SLOTS`` from empty takes over two hours of uninterrupted looping, every
step of it visible in the sidebar -- which converts a silent exhaustion into
something a person watching the dashboard sees long before it lands.

Follows ``handlers/auth_refresh.py``'s bucket shape rather than
``notifications.rate_limit.AppRateLimiter``: that limiter never evicts, which is
sound for a bounded set of installed app names but would leak here, where the key
space is session keys that churn for the gateway's lifetime.
"""

from __future__ import annotations

import threading
import time
from collections import deque

#: Window both budgets are measured over.
WINDOW_SECS = 300.0

#: Creates allowed per window, per caller, per verb. Sessions get the larger budget
#: because a dispatch round opens one per work item; a goal needs exactly one folder,
#: so that budget only has to absorb retries and a nested tree.
MAX_SESSION_CREATES_PER_WINDOW = 20
MAX_FOLDER_CREATES_PER_WINDOW = 10

#: How often stale buckets are swept, so the map cannot grow without bound across
#: the many distinct session keys a long-lived gateway sees.
_SWEEP_INTERVAL_SECS = 60.0

_lock = threading.Lock()
_buckets: dict[tuple[str, str], deque[float]] = {}
_last_sweep = 0.0

SESSION_CREATE = "session_create"
FOLDER_CREATE = "folder_create"

_BUDGETS = {
    SESSION_CREATE: MAX_SESSION_CREATES_PER_WINDOW,
    FOLDER_CREATE: MAX_FOLDER_CREATES_PER_WINDOW,
}


def _sweep(now: float, *, force: bool = False) -> None:
    """Drop buckets whose every timestamp has aged out. Caller holds ``_lock``."""
    global _last_sweep
    if not force and now - _last_sweep < _SWEEP_INTERVAL_SECS:
        return
    _last_sweep = now
    cutoff = now - WINDOW_SECS
    stale: list[tuple[str, str]] = []
    for key, bucket in _buckets.items():
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if not bucket:
            stale.append(key)
    for key in stale:
        _buckets.pop(key, None)


def allow_create(verb: str, caller_key: str, *, now: float | None = None) -> bool:
    """Consume one unit of *caller_key*'s budget for *verb*.

    Returns ``False`` when the caller is over its budget for the current window,
    which the endpoint renders as a 429.

    Fails CLOSED on an unknown verb or an empty caller key. An empty key means the
    request cannot be attributed and therefore cannot be rate-limited at all;
    bucketing those together under one sentinel would still let the whole budget
    through on a key an attacker can trivially blank, so the request is refused
    instead. Every caller this guards resolves its identity strictly before
    reaching here, so this denies no legitimate traffic.
    """
    budget = _BUDGETS.get(verb)
    if budget is None or not caller_key:
        return False
    if now is None:
        now = time.monotonic()
    cutoff = now - WINDOW_SECS
    key = (verb, caller_key)
    with _lock:
        _sweep(now)
        bucket = _buckets.get(key)
        if bucket is None:
            bucket = deque()
            _buckets[key] = bucket
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= budget:
            return False
        bucket.append(now)
        return True


def reset_for_tests() -> None:
    """Clear all buckets. Test-only: module state would otherwise leak across tests."""
    global _last_sweep
    with _lock:
        _buckets.clear()
        _last_sweep = 0.0
