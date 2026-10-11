"""Bound on the Slack handler's per-thread override state.

``slack.handler`` keeps three per-thread containers: the hydration guard
``_hydrated_sessions`` and the ``!ta`` / ``!project`` overrides ``_thread_agents``
and ``_thread_projects``. Each gains an entry per thread the bot hears from, so this
module holds them to :data:`THREAD_SLOTS` threads, in the least-recently-used shape
of ``thread_replies``' watermark (``_last_turn``). Past the bound, the oldest
thread loses all three entries together, and its next message runs hydration
again, which reads the overrides back from the conversation metadata.

An override a person set with ``!ta`` or ``!project`` in this process is pinned
instead: hydration may not read it back, because the command's metadata write is
skipped without a conversation log and only logged when it fails, and hydration
prefers a recorded execution template over the ``agent`` field. A pinned thread
is never evicted while the override is set. Pinned threads count inside
:data:`THREAD_SLOTS`, at most :data:`MAX_PINNED_THREADS` of them, so one slot always
stays evictable; a command that would pin a thread past that is refused with a reply
that says so, so the pins cannot grow past the bound either.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import MutableMapping, MutableSet

from kiro_crew.slack.thread_replies import _WATERMARK_SLOTS

logger = logging.getLogger(__name__)

#: Threads whose override state is kept, pinned threads included. It is the replies
#: watermark's own bound (``thread_replies._WATERMARK_SLOTS``, the same threads), so
#: the two cannot drift.
THREAD_SLOTS = _WATERMARK_SLOTS

#: The most threads ``!ta`` and ``!project`` may pin. One slot always stays unpinned, so
#: hydrating a new thread never evicts that same thread, which would leave its override
#: rows outside every count. Pinned threads count inside :data:`THREAD_SLOTS`.
MAX_PINNED_THREADS = THREAD_SLOTS - 1

#: The longest agent name or project path the two maps keep, whichever path writes it:
#: ``!ta`` and ``!project`` refuse a longer value before they write (:func:`setter_refusal`),
#: and hydration skips one it reads back from the conversation metadata
#: (:func:`bounded_override`). So the maps keep at most THREAD_SLOTS threads x 2 values x
#: this many characters. It is Linux's PATH_MAX, so it admits every project directory a
#: Linux or macOS host (1,024) can resolve, and Windows without long paths (260). An
#: agent name is usually far shorter, but a project agent's is whatever its spec file
#: declares, which nothing else limits.
OVERRIDE_VALUE_MAX_CHARS = 4096

#: Evictable session keys, oldest first. Pinned keys are kept out of it.
_order: OrderedDict[str, None] = OrderedDict()

#: ``(session_key, kind)`` for each override a command set in this process.
_pinned: set[tuple[str, str]] = set()


def _is_pinned(session_key: str) -> bool:
    return (session_key, "agent") in _pinned or (session_key, "project") in _pinned


def _pinned_threads() -> int:
    return len({session_key for session_key, _kind in _pinned})


def setter_refusal(kind: str, value: str) -> str | None:
    """The reply ``!ta`` or ``!project`` sends instead of keeping *value*, or None to keep it.

    The setters keep a value only within :data:`OVERRIDE_VALUE_MAX_CHARS`, the limit
    hydration applies to the same maps, and refuse before they pin or write anything.
    The reply names the length and the limit, never the value.
    """
    if len(value) <= OVERRIDE_VALUE_MAX_CHARS:
        return None
    noun = "agent name" if kind == "agent" else "project path"
    return (
        f"❌ Not set: that {noun} is {len(value)} characters, over the "
        f"{OVERRIDE_VALUE_MAX_CHARS}-character limit."
    )


def bounded_override(session_key: str, kind: str, value: object) -> str:
    """*value* when it is a ``str`` within :data:`OVERRIDE_VALUE_MAX_CHARS`, else ``""``.

    For a value hydration read back from the conversation metadata, which nothing
    bounds: a skipped value is logged once, by its type or its length, never its text,
    and the thread keeps no override of that *kind* (the default applies).
    """
    if isinstance(value, str) and len(value) <= OVERRIDE_VALUE_MAX_CHARS:
        return value
    if isinstance(value, str):
        logger.warning(
            "Thread override for %s: the %s value in the conversation metadata is %d "
            "characters, over the %d-character limit; it is not kept",
            session_key,
            kind,
            len(value),
            OVERRIDE_VALUE_MAX_CHARS,
        )
    else:
        logger.warning(
            "Thread override for %s: the %s value in the conversation metadata is a %s, "
            "not text; it is not kept",
            session_key,
            kind,
            type(value).__name__,
        )
    return ""


def touch(session_key: str) -> None:
    """Mark *session_key* as recently used, if it is tracked."""
    if session_key in _order:
        _order.move_to_end(session_key)


def _evict_over_bound(
    hydrated: MutableSet[str], overrides: tuple[MutableMapping[str, str], ...]
) -> None:
    """Evict the oldest unpinned threads until the pins and the rest fit :data:`THREAD_SLOTS`.

    An evicted thread leaves the guard and every map in *overrides* at once.
    """
    evictable = max(THREAD_SLOTS - _pinned_threads(), 0)
    while len(_order) > evictable:
        oldest, _ = _order.popitem(last=False)
        hydrated.discard(oldest)
        for mapping in overrides:
            mapping.pop(oldest, None)


def note_hydrated(
    session_key: str,
    hydrated: MutableSet[str],
    *overrides: MutableMapping[str, str],
) -> None:
    """Add *session_key* to the guard, then evict the oldest unpinned threads.

    Pinned threads count inside :data:`THREAD_SLOTS`, so the evictable ones are held
    to what the pins leave. An evicted thread leaves the guard and every map in
    *overrides* at once.
    """
    hydrated.add(session_key)
    if not _is_pinned(session_key):
        _order[session_key] = None
        _order.move_to_end(session_key)
    _evict_over_bound(hydrated, overrides)


def pin(
    session_key: str,
    kind: str,
    hydrated: MutableSet[str],
    *overrides: MutableMapping[str, str],
) -> bool:
    """Keep a thread whose *kind* override a command set from being evicted.

    Refused (False) for a thread not pinned yet when :data:`MAX_PINNED_THREADS` threads
    already are: the command then sets nothing and says why (:func:`refusal_text`).

    The command awaits before it pins, and other threads hydrating meanwhile can evict
    this one. So the pin puts the thread back in the guard *hydrated*, which keeps its
    next message from hydrating over the override, and evicts the oldest unpinned
    threads from the guard and *overrides* until the pins and the rest fit
    :data:`THREAD_SLOTS` again, before the caller publishes the override.
    """
    if not _is_pinned(session_key) and _pinned_threads() >= MAX_PINNED_THREADS:
        return False
    _pinned.add((session_key, kind))
    _order.pop(session_key, None)
    hydrated.add(session_key)
    _evict_over_bound(hydrated, overrides)
    return True


def refusal_text() -> str:
    """What ``!ta`` and ``!project`` reply when :func:`pin` refuses."""
    return (
        f"❌ Not set: {MAX_PINNED_THREADS} threads already have a thread agent or project in "
        "this process. Clear one with `!ta off` or `!project off` in its thread, then try again."
    )


def unpin(session_key: str, kind: str) -> None:
    """Release a cleared override; the thread is evictable again once unpinned."""
    _pinned.discard((session_key, kind))
    if not _is_pinned(session_key):
        _order[session_key] = None
        _order.move_to_end(session_key)
