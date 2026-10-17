"""Bound on the Slack handler's per-thread override state.

``slack.handler`` keeps three per-thread containers: the hydration guard
``_hydrated_sessions`` and the ``!ta`` / ``!project`` overrides ``_thread_agents``
and ``_thread_projects``. Each gains an entry per thread the bot hears from, so this
module holds them to :data:`THREAD_SLOTS` threads, in the least-recently-used shape
of ``thread_replies``' watermark (``_last_turn``). Past the bound, the oldest
thread loses all three entries together, and its next message runs hydration
again, which reads the overrides back from the conversation metadata. Only a read
the conversation log reports as readable marks a thread hydrated; after one it could
not make, the thread's next message reads the metadata again.

An override a person set with ``!ta`` or ``!project`` in this process is pinned
instead: hydration may not read it back, because the command's metadata write is
skipped without a conversation log and only logged when it fails, and hydration
prefers a recorded execution template over the ``agent`` field. A pinned thread
is never evicted while the override is set, and hydration never publishes over it. A
reset (``!ta off``, ``!project off``) that hydration would undo, because it would read
a value back or cannot read the metadata back, keeps its thread pinned with nothing
set, for the same reason. Pinned threads count inside
:data:`THREAD_SLOTS`, at most :data:`MAX_PINNED_THREADS` of them, so one slot always
stays evictable; a command that would pin a thread past that is refused with a reply
that says so, so the pins cannot grow past the bound either.

Hydration never publishes what it read before a newer ``!ta`` or ``!project`` metadata
write: the commands count their writes (:func:`note_command_write`), and a hydration
whose read the count moved across reads again, at most :data:`HYDRATION_READ_ATTEMPTS`
times. Eviction would otherwise let a read that began before a command's write publish
the old value after the command replied.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable, MutableMapping, MutableSet
from typing import Any

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

#: How many ``!ta`` / ``!project`` metadata writes this process has finished, for any
#: thread. One integer for the process, so there is nothing to bound. Hydration compares
#: it before and after its own read of a thread's metadata (:func:`command_writes`): if it
#: moved, a write may have landed after the read began, so the read is not published.
_command_writes = 0

#: How many times hydration reads a thread's metadata while command writes keep landing
#: during its reads. After that many it publishes nothing and leaves the thread
#: unhydrated, so the thread's next message hydrates it.
HYDRATION_READ_ATTEMPTS = 3


def note_command_write() -> None:
    """Record that a ``!ta`` or ``!project`` metadata write has returned or raised.

    The command calls it on the event loop as soon as its write's worker returns,
    before it awaits anything else, so a hydration whose read began before the write
    landed finds the count moved once that read is back, and reads again.
    """
    global _command_writes
    _command_writes += 1


def command_writes() -> int:
    """How many ``!ta`` / ``!project`` metadata writes :func:`note_command_write` recorded."""
    return _command_writes


def _is_pinned(session_key: str) -> bool:
    return (session_key, "agent") in _pinned or (session_key, "project") in _pinned


def _pinned_threads() -> int:
    return len({session_key for session_key, _kind in _pinned})


def kind_is_pinned(session_key: str, kind: str) -> bool:
    """True when a command set or reset this thread's *kind*: hydration never publishes it."""
    return (session_key, kind) in _pinned


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
    this one, its other override included. So the command hydrates the thread again just
    before it pins, with no await between the two: an evicted thread reads both overrides
    back from the conversation metadata, keeping any value set meanwhile. The pin leaves
    the guard *hydrated* to that hydration, so one that gave up leaves the thread for its
    next message to hydrate, and evicts the oldest unpinned threads from the guard and
    *overrides* until the pins and the rest fit :data:`THREAD_SLOTS` again, before the
    caller publishes the override.
    """
    if not _is_pinned(session_key) and _pinned_threads() >= MAX_PINNED_THREADS:
        return False
    _pinned.add((session_key, kind))
    _order.pop(session_key, None)
    _evict_over_bound(hydrated, overrides)
    return True


def refusal_text() -> str:
    """What ``!ta`` and ``!project`` reply when :func:`pin` refuses."""
    return (
        f"❌ Not set: {MAX_PINNED_THREADS} threads already have a thread agent or project in "
        "this process. Clear one with `!ta off` or `!project off` in its thread, then try again."
    )


def unheld_reset_text(kind: str) -> str:
    """What ``!ta off`` and ``!project off`` reply when the reset applies but cannot be held.

    Hydration would read the old value back (a recorded execution template, or a
    metadata write that failed), or the metadata could not be read back to show it
    would not, so the reset needs a pin, and :func:`pin` refused one. When the read-back
    was unreadable the thread is not hydrated either, so the value can come back with
    its next message, not only after an eviction.
    """
    noun, command = ("agent", "!ta off") if kind == "agent" else ("project", "!project off")
    return (
        f"🔄 Thread {noun} reset for now. {MAX_PINNED_THREADS} threads already have a thread "
        f"agent or project in this process, so this thread cannot be kept, and its recorded "
        f"{noun} can come back. Clear one in another thread, then run "
        f"`{command}` here again."
    )


class _OneRead:
    """A metadata line already read, served to the reader in place of the conversation log."""

    def __init__(self, meta: dict) -> None:
        self._meta = meta

    def get_metadata(self, _session_key: str) -> dict:
        return self._meta


def read_back(
    session_key: str,
    conversation_log: Any,
    resolve: Callable[[str, Any], tuple[str, str]],
) -> tuple[str, str] | None:
    """``(agent, project)`` as hydration reads them back now, or None if unreadable.

    Hydration and both resets read through it, once each. ``get_metadata`` answers an
    unreadable line with empty metadata, like a thread with nothing set;
    ``get_metadata_status`` keeps the difference. *resolve* is hydration's reader.
    """
    if not conversation_log:
        return "", ""
    try:
        meta, readable = conversation_log.get_metadata_status(session_key)
        return resolve(session_key, _OneRead(meta)) if readable else None
    except Exception:
        logger.debug("Could not read back the overrides of %s", session_key, exc_info=True)
        return None


def unpin(session_key: str, kind: str) -> None:
    """Release a cleared override; the thread is evictable again once unpinned.

    Only releasing the thread's last existing pin puts it back in the evictable order:
    a pinned thread was never evicted, so it is still counted. A thread that held
    no pin is only touched, so one evicted while its command awaited is not tracked
    again outside the bound.
    """
    was_pinned = _is_pinned(session_key)
    _pinned.discard((session_key, kind))
    if was_pinned and not _is_pinned(session_key):
        _order[session_key] = None
    else:
        touch(session_key)
