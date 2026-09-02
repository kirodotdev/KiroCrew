"""Retry de-duplication for the ``session_stop`` RPC's escalation.

``stop_slot_turn`` hard-kills when a second stop arrives while the first is still
pending, and that is right for the Stop BUTTON it was written for: a person
pressing again has watched the cooperative stop fail to take, so the second press
is a decision. It is wrong for an RPC. A client that does not get a response
inside its request timeout re-sends, and a retry is not a second decision -- it is
the same request again. The kill path clears the target's ``_queue`` and
``_pending_steers``, so on that path a caller whose first request merely timed out
silently gets the destructive variant of a verb it asked for once (issue #5074).

This module holds the one fact that tells the two apart: whether this caller has
already asked to stop this target recently. ``stop_target`` consults it and
withholds escalation for a repeat, which lands the call on the existing
"stop already in progress" no-op instead of a kill. Nothing else changes -- a stop
that finds the target running still stops it, so withholding escalation never
costs the caller the stop it asked for.

**The window is anchored at the caller's FIRST stop of a target and is not
extended by the repeats it absorbs.** That bounds how long escalation is
suppressed: a client that retries forever is absorbed for one window, after which
a stop that still finds the target pending escalates as before. A sliding window
would instead put escalation out of reach of any client polling faster than the
window, trading one silent failure for another.

:data:`WINDOW_SECS` is sized against the thing it has to outlast. ``mcp_core._post``
times out at 30s, so the retry this exists to absorb cannot arrive sooner than
that; a window at or under the request timeout would expire before its own retry
and fix nothing. 120s leaves room for that timeout plus the caller's own turn.
Nothing is lost at the far end either, because the only call the window can delay
is one that finds the target STILL winding down two minutes after a cooperative
stop -- which is exactly the case where escalating is the right answer.

Keyed per (caller, target): a retry comes from the caller that made the original
request. Two different callers stopping one target are two independent decisions,
and widening the key to the target alone would suppress the second caller's FIRST
call -- removing escalation from the RPC altogether rather than making a retry
safe.

Follows ``create_rate_limit``'s in-memory shape, and for the same reason it gives:
a guard measured in minutes needs no durable state, because a restart buys a
caller one window rather than a clean slate. It differs in sweeping on every call
instead of on an interval -- stop is a low-frequency verb and the map is bounded by
the pairs of live slots that stopped each other inside the window, so there is no
burst for an interval guard to amortize.
"""

from __future__ import annotations

import threading
import time

#: How long after a caller's first stop of a target a repeat is read as a retry
#: rather than as an escalation. See the module docstring for the sizing.
WINDOW_SECS = 120.0

_lock = threading.Lock()

#: (caller slot key, target slot key) -> monotonic time of the FIRST stop in the
#: current window. Deliberately not refreshed by later stops; see the docstring.
_windows: dict[tuple[str, str], float] = {}


def allow_escalation(caller_key: str, target_key: str, *, now: float | None = None) -> bool:
    """Record a stop of *target_key* by *caller_key*; say whether it may escalate.

    ``False`` means this caller already stopped this target inside
    :data:`WINDOW_SECS`, so the call cannot be told apart from a retry of that
    request and must not be read as "escalate".

    Recording and deciding are one operation under one lock, so two concurrent
    stops cannot both observe an empty window and both escalate.

    Fails CLOSED (no escalation) on an unattributable pair: an empty key cannot be
    matched against a first call, so a hard kill would be granted to precisely the
    caller whose retries cannot be recognized. Withholding costs only the kill --
    the cooperative stop still lands -- so the safe direction is the one that
    cannot discard a queue. Every caller reaching here has already resolved its
    identity strictly, so this refuses nothing legitimate.
    """
    if not caller_key or not target_key:
        return False
    if now is None:
        now = time.monotonic()
    cutoff = now - WINDOW_SECS
    key = (caller_key, target_key)
    with _lock:
        for stale in [k for k, first in _windows.items() if first <= cutoff]:
            del _windows[stale]
        if key in _windows:
            return False
        _windows[key] = now
        return True


def reset_for_tests() -> None:
    """Clear every window. Test-only: module state would otherwise leak across tests."""
    with _lock:
        _windows.clear()
