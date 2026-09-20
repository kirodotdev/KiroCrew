"""The one seam that lets the gateway enumerate its live ``AcpRuntime`` processes.

Nothing else in the codebase lists every live agent runtime with its identity:
chats, channels, review-pool workers, background runtimes and dedicated
subagents each hold their own runtime privately. The chat resource monitor needs
that union to attribute per-process memory and CPU back to a chat, so this module
keeps a single weak registry that every runtime joins on spawn and leaves on
death.

Why a :class:`weakref.WeakSet`: a runtime that dies WITHOUT running its normal
teardown (crashed, garbage-collected) must not linger here and be sampled as a
phantom entry. Weak membership means such a runtime drops out on its own the
moment Python collects it, so the sampler only walks ``/proc`` trees for pids
that map to a tracked runtime.

The registry is deliberately typed by structural use, not by importing
``AcpRuntime``: ``runtime.py`` imports THIS module (task 1.2 wires the two
``register``/``discard`` call sites), so importing it back would be a cycle. A
registered object only needs a readable ``pid`` attribute for ordering.
"""

from __future__ import annotations

import threading
import weakref
from typing import Any

# Guards both mutation and iteration. A WeakSet is not safe to iterate while a
# concurrent thread mutates it (and GC can mutate it at any time), so every read
# copies the members out under the lock before returning.
_lock = threading.Lock()

# Weak membership: a runtime that is collected without calling discard() falls
# out automatically, so a dead runtime is never sampled as a live one.
_runtimes: "weakref.WeakSet[Any]" = weakref.WeakSet()


def register(runtime: Any) -> None:
    """Record a live runtime. Called right after a successful spawn sets its pid.

    Idempotent: a set holds each runtime once, so a repeated call is harmless.
    """
    with _lock:
        _runtimes.add(runtime)


def discard(runtime: Any) -> None:
    """Drop a runtime from the registry. Called from the runtime's death path.

    Idempotent: discarding an absent runtime is a no-op, matching ``set.discard``.
    Weak membership already removes a garbage-collected runtime, so this exists
    for the ordinary case where the runtime object outlives its process.
    """
    with _lock:
        _runtimes.discard(runtime)


def _sort_key(runtime: Any) -> tuple[int, int, int]:
    """Deterministic ordering key: by root pid, then a stable tiebreaker.

    A runtime that has not yet been assigned a pid (``None`` or unreadable) sorts
    LAST, and within a pid bucket ``id()`` breaks ties so the order never wobbles
    between calls for the same set of objects. The leading 0/1 flag is what pushes
    the pid-less runtimes to the end regardless of their ``id()``.
    """
    pid = getattr(runtime, "pid", None)
    if isinstance(pid, int):
        return (0, pid, id(runtime))
    return (1, 0, id(runtime))


def live_runtimes() -> list[Any]:
    """Return a snapshot list of the live runtimes, sorted deterministically.

    The result is a fresh list (never the live WeakSet), so the caller can iterate
    it without holding the lock and without racing GC. Ordering is by root pid
    ascending, pid-less runtimes last, ``id()`` as a stable tiebreaker -- the
    deterministic order the sampler's dedupe pass relies on.
    """
    with _lock:
        snapshot = list(_runtimes)
    snapshot.sort(key=_sort_key)
    return snapshot
