"""A ceiling on how many turns one chat conversation can drive.

The loss this closes
--------------------
A chat channel can be made to answer itself: the agent replies, the reply
arrives back as inbound, the agent answers it, and that repeats without end at
one real model turn per cycle. Each channel that has met this ships a
per-message echo guard, and every one of those guards recognises a SHAPE -- an
exact body match inside a short TTL, or a remembered message id. A guard that
recognises a shape fails open on the variant it does not recognise, and the loop
then runs again at full cost with nothing on any surface saying so.

This module bounds the COST of that class instead of recognising its members, so
an echo variant no guard knows is still bounded.

Why a count over a long window, and not a rate
----------------------------------------------
A loop's rate is set by how long a turn takes, which puts it in the same band as
a person firing off quick notes: very roughly three to twelve messages a minute.
Any threshold tight enough to catch a slow loop also refuses a real person, and
on a chat channel a refusal the user cannot see is silence. Over an hour the two
populations separate: a loop sustains a hundred and eighty turns or more, while
a fast human in a busy thread stays in the tens. So the bound is a COUNT over a
long window, and it needs no rate threshold at all.

Why it never asks who sent the message
--------------------------------------
In a self-chat a channel cannot tell its own message from the peer's, which is
the same limit the per-message guards run into. This ceiling does not ask. It
counts turns in a conversation, so a loop and a very busy human are bounded by
one rule and neither has to be identified.

Why the ceiling LATCHES
-----------------------
A rolling window on its own lets a loop resume the moment the window slides,
which bounds the burn rate but never ends it. So the first refusal latches the
conversation, and every later turn on that key is refused until the latch clears.

Why the session key is the right key, and what clears a latch
------------------------------------------------------------
The gate this composes into already receives the session key, so keying on it
adds no identity plumbing to any channel.

Resetting the conversation clears a latch, and every verb the product calls a
reset does it: the session lifecycle's conversation discard, ``remove``,
``remove_if_unclaimed`` and ``destroy`` all release the key they act on. That is
the resume path, it is what the refusal text names, and it is reachable from the
dashboard -- Slack's ``!agent default`` reaches ``remove`` and answers "Reset to
default agent", so a latch surviving it would refuse the first message of a
conversation the user was just told had been reset. A gateway restart clears one
too, because the store is in memory.

A loop reaches none of them. It cannot reset its own conversation, and a restart ends
it anyway. The channel key itself does NOT change on a discard -- channel linkage
is retained by design -- so the explicit reset, not a key change, is what makes
the documented remedy work, and a test pins that the reset releases a latched
conversation.

Why the refusal names no resume command
---------------------------------------
In a self-chat the text the agent emits is exactly what comes back as inbound.
A refusal that quoted the command for resuming would hand the loop its own way
out, so the message describes the resume surface and quotes no command for it.

Why this is not enforced inside the session gate itself
------------------------------------------------------
The dashboard calls the same ``begin_turn`` gate, and it drives turns a human is
watching and clicking through. Composing at the channel turn sites keeps the
ceiling on inbound chat traffic and off dashboard, cron and subagent turns.

Why a monitor turn is not counted
---------------------------------
Two of the channel turn sites are the arm that drives an armed monitor loop, not
an inbound message. A monitor already carries its own cycle cap and its own
runtime budget, so it is bounded by construction, and counting its cycles here
would let a legitimately armed long watch latch the conversation and then refuse
the human's next message in it. The ceiling therefore composes onto the inbound
arm only, and a test names every site so a new one has to be classified on
purpose rather than inherit a default.

Bounding the bookkeeping
------------------------
The store is in memory and holds two bounded maps: one timestamp window per
tracked conversation and one latch set. Both are newest-wins and capped at
:data:`MAX_TRACKED_CONVERSATIONS`, so a host meeting many conversations cannot
turn a loop guard into a memory leak. In memory is deliberate: a loop is a live
phenomenon, a restart ends it, and a restart is also the one event after which a
latch should not survive.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import threading
import time
from collections import OrderedDict, deque
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Deque, Iterator, Optional

from kiro_crew.messaging.renderer import TEXT_CHUNK, OutputEvent

logger = logging.getLogger(__name__)

#: Set for the span of a turn this gateway GENERATED rather than received, and
#: spent by the FIRST ceiling gate inside that span.
#:
#: A nudge loop has its own cycle cap and runtime budget; counting its cycles
#: here would let a long watch spend the budget and latch the conversation
#: against the human too.
#:
#: SINGLE-USE is the safety property: a dispatcher drains messages queued during
#: a turn in the same task, so a span-wide exemption would also exempt every
#: drained inbound message, which is the self-driving loop this module bounds.
#:
#: A ContextVar, not a parameter, so the fact need not cross every channel's
#: ``handle_message``; it is copied per task, so a concurrent human turn is
#: unaffected.
_generated_turn: ContextVar[Optional["_GeneratedTurn"]] = ContextVar(
    "kirocrew_turn_ceiling_generated", default=None
)


class _GeneratedTurn:
    """One generated turn's unspent exemption."""

    __slots__ = ("unspent",)

    def __init__(self) -> None:
        self.unspent = True


@contextmanager
def generated_turn() -> Iterator[None]:
    """Mark the enclosed dispatch as a turn this gateway generated.

    Wrap the ``await`` that drives one nudge cycle. The first ceiling gate
    entered inside it passes without counting, on every channel, so a new
    channel's nudge path inherits the exemption instead of re-deriving it.

    Only the first: see :data:`_generated_turn` for why a span-wide exemption
    would hand the same pass to every message a queue drain replays.
    """
    token = _generated_turn.set(_GeneratedTurn())
    try:
        yield
    finally:
        _generated_turn.reset(token)


def _spend_generated_turn() -> bool:
    """Spend the exemption if one is in scope, reporting whether it applied."""
    marker = _generated_turn.get()
    if marker is None or not marker.unspent:
        return False
    marker.unspent = False
    return True


#: Turns one conversation may drive inside :data:`DEFAULT_WINDOW_SECS` before the
#: ceiling latches. A loop sustains at least twice this in the same hour, so it
#: latches inside roughly half a window, while a fast human in a busy thread
#: stays well below it.
DEFAULT_MAX_TURNS = 90

#: Width of the rolling window, in seconds.
DEFAULT_WINDOW_SECS = 3600.0

#: Highest ceiling an operator may set. Each window keeps one timestamp per
#: counted turn, so this is the deque's only length bound: about 16 MB across
#: MAX_TRACKED_CONVERSATIONS rows at this value, 160 MB at ten times it.
MAX_CONFIGURABLE_CEILING = 1000

#: Conversations tracked at once, for the window map and the latch set alike.
MAX_TRACKED_CONVERSATIONS = 2048

#: Shortest gap between two overflow announcements. Eviction runs on every
#: counted turn, so a host in steady state above the cap evicts continuously and
#: an announcement per event would itself be the flood. The COUNTS stay exact;
#: only how often they are spoken is bounded.
OVERFLOW_REPORT_SECS = 60.0

#: Longest session key either store keeps verbatim, so the row cap bounds memory.
#: A longer key is stored as a digest, not truncated: keys sharing a prefix must
#: not merge into one count.
MAX_RETAINED_KEY_CHARS = 256


def _store_key(session_key: str) -> str:
    """The bounded form of *session_key*, as both stores retain it."""
    if len(session_key) <= MAX_RETAINED_KEY_CHARS:
        return session_key
    return "sha256:" + hashlib.sha256(session_key.encode("utf-8", "surrogatepass")).hexdigest()


#: Operator overrides. Read per store construction rather than at import, so a
#: host can retune the ceiling without a code change.
ENV_MAX_TURNS = "KIROCREW_CHANNEL_TURN_CEILING"
ENV_WINDOW_SECS = "KIROCREW_CHANNEL_TURN_WINDOW_SECS"

#: What the user is told. It names the resume SURFACE and quotes no command,
#: because in a self-chat a quoted command returns as inbound and would let the
#: loop resume itself.
REFUSAL_TEXT = (
    "This conversation hit its turn limit and is paused, so no further "
    "messages here will be answered. Reset the conversation from the Kiro Crew "
    "dashboard to continue."
)


class TurnCeilingExceeded(Exception):
    """A conversation is at its turn ceiling, so the turn was refused pre-stream.

    No prompt was registered and no model turn spent. Treat it like the shutdown
    refusal: terminal for the message, and not charged to the circuit breaker.

    ``announce`` is true only on the turn that CLOSES the latch; later refusals are
    silent. In a self-chat the notice returns as inbound and is refused in turn, so
    a notice per refusal would loop forever.
    """

    def __init__(self, message: str, *, announce: bool = True) -> None:
        super().__init__(message)
        self.announce = announce


def _env_ceiling(name: str, default: int) -> int:
    """The ceiling an operator asked for, where ``0`` means no ceiling at all.

    ``0`` is the off switch, and it exists because this guard is ON by default on
    every channel: a default nobody chose has to stay declinable, or the old
    unbounded behaviour is not selectable at any setting. A NEGATIVE value is not
    a request for that -- it is a typo -- so it still falls back with a warning.

    Clamped to :data:`MAX_CONFIGURABLE_CEILING`, and clamped HERE rather than
    where a window is read, because the ceiling is the only length bound on each
    conversation's retained deque: a bound applied downstream leaves the store
    itself unbounded. Said out loud when it binds, so an operator who set a higher
    number learns which one is in force.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s is not an integer (%r); using %d", name, raw, default)
        return default
    if value < 0:
        logger.warning("%s cannot be negative (%r); using %d", name, raw, default)
        return default
    if value > MAX_CONFIGURABLE_CEILING:
        logger.warning(
            "%s of %d exceeds the retained-window maximum; using %d",
            name,
            value,
            MAX_CONFIGURABLE_CEILING,
        )
        return MAX_CONFIGURABLE_CEILING
    return value


def _env_positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s is not a number (%r); using %s", name, raw, default)
        return default
    # Finiteness BEFORE positivity, because a positivity test alone admits both
    # non-finite values: `inf > 0` and every comparison against `nan` is False.
    # Either one silently turns the rolling window into a lifetime counter -- the
    # expiry comparison never becomes true, so nothing ever ages out and the
    # conversation latches on its total turn count instead of a window's. An
    # operator writing `inf` to mean "a very wide window" is an ordinary intent,
    # so this refuses it loudly rather than accepting it as that.
    if not math.isfinite(value):
        logger.warning("%s must be finite (%r); using %s", name, raw, default)
        return default
    if value <= 0:
        logger.warning("%s must be positive (%r); using %s", name, raw, default)
        return default
    return value


class ConversationTurnCeiling:
    """Bounded per-conversation turn counter with a latching refusal.

    One instance is shared by every channel through :data:`_SHARED`. Guarded by a
    lock because the channel transports do not all run on one event loop, and the
    critical section is a few list operations.
    """

    def __init__(
        self,
        *,
        max_turns: Optional[int] = None,
        window_secs: Optional[float] = None,
        max_tracked: int = MAX_TRACKED_CONVERSATIONS,
    ) -> None:
        self.max_turns = (
            _env_ceiling(ENV_MAX_TURNS, DEFAULT_MAX_TURNS) if max_turns is None else max_turns
        )
        self.window_secs = (
            _env_positive_float(ENV_WINDOW_SECS, DEFAULT_WINDOW_SECS)
            if window_secs is None
            else window_secs
        )
        # The env reader falls back with a warning on an unusable value, because an
        # operator's typo must not stop the gateway. An explicit ARGUMENT is a
        # different thing -- a caller in this codebase -- so it raises: a window
        # that is not finite and positive makes the expiry comparison never true,
        # which silently turns the rolling window into a lifetime counter, and a
        # silent wrong bound is the failure this whole module exists to remove.
        if not math.isfinite(self.window_secs) or self.window_secs <= 0:
            raise ValueError(f"window_secs must be finite and positive, got {self.window_secs!r}")
        # Zero reaches here only from the env off switch, never from a caller: an
        # explicit argument of 0 is a programming error, while the operator who
        # typed it is asking for the unbounded behaviour on purpose.
        if max_turns is not None and self.max_turns <= 0:
            raise ValueError(f"max_turns must be positive, got {self.max_turns!r}")
        # The env reader clamps; a caller raises, for the same reason the window
        # does. The ceiling is the only length bound on each retained deque, so it
        # has to hold whoever set it.
        if self.max_turns > MAX_CONFIGURABLE_CEILING:
            raise ValueError(
                f"max_turns must be at most {MAX_CONFIGURABLE_CEILING}, got {self.max_turns!r}"
            )
        self.max_tracked = max_tracked
        self._lock = threading.Lock()
        self._windows: "OrderedDict[str, Deque[float]]" = OrderedDict()
        self._latched: "OrderedDict[str, float]" = OrderedDict()
        #: Rows dropped to hold the cap, cumulative and exact. Read by the
        #: overflow warning so the truncation is a number rather than an absence.
        self.windows_evicted = 0
        self.latches_evicted = 0
        self._last_overflow_report = float("-inf")

    def check(self, session_key: str) -> None:
        """Count this turn, or refuse it.

        Raises :class:`TurnCeilingExceeded` when the conversation is latched, or
        when counting this turn would carry it past the ceiling. The registered
        notification sink is called once, at the moment the latch closes, so an
        observer hears about the pause without being told again by every refused
        turn queued behind it.
        """
        if not session_key:
            # No key means nothing to attribute the count to, and refusing an
            # unattributable turn would be a silent drop with no conversation to
            # explain it in. Counting it under one shared empty key would bound
            # unrelated conversations together, which is worse.
            return
        if not self.max_turns:
            # The operator turned the ceiling off. Nothing is counted rather than
            # counted-and-never-refused, so no memory is spent tracking windows
            # that can never reach a bound.
            return
        now = time.monotonic()
        latched_now = False
        stored = _store_key(session_key)
        with self._lock:
            if stored in self._latched:
                self._latched.move_to_end(stored)
                already_latched = True
            else:
                already_latched = False
                window = self._windows.get(stored)
                if window is None:
                    window = deque()
                    self._windows[stored] = window
                else:
                    self._windows.move_to_end(stored)
                horizon = now - self.window_secs
                while window and window[0] <= horizon:
                    window.popleft()
                if len(window) >= self.max_turns:
                    # Latch, and drop the window: the key is refused from here on
                    # regardless of what the window would say next.
                    self._windows.pop(stored, None)
                    self._latched[stored] = now
                    latched_now = True
                else:
                    window.append(now)
            self._evict_locked()
        if not latched_now and not already_latched:
            return
        if latched_now:
            logger.warning(
                "channel turn ceiling reached: pausing session=%s after %d turns in %ss",
                session_key,
                self.max_turns,
                self.window_secs,
            )
            _emit_latched_note(session_key)
        else:
            logger.info("channel turn ceiling still latched: refusing turn session=%s", session_key)
        raise TurnCeilingExceeded(REFUSAL_TEXT, announce=latched_now)

    def is_latched(self, session_key: str) -> bool:
        with self._lock:
            return _store_key(session_key) in self._latched

    def reset(self, session_key: str) -> None:
        """Forget a conversation, clearing its count and any latch.

        This IS the resume path, so it carries an obligation: the session
        lifecycle's conversation discard calls it, and the refusal text tells the
        user to reset the conversation. A discard path that stops calling this
        leaves a latched conversation releasable only by a gateway restart, while
        the notice still names the reset as the remedy.
        """
        stored = _store_key(session_key)
        with self._lock:
            self._windows.pop(stored, None)
            self._latched.pop(stored, None)

    def clear(self) -> None:
        with self._lock:
            self._windows.clear()
            self._latched.clear()

    def _evict_locked(self) -> None:
        """Hold both stores at the cap, and say so when that drops a latch.

        A dropped window gives that conversation extra headroom; a dropped latch
        lifts its refusal and gives it a fresh window. Both are counted exactly,
        never silently. Eviction is least-recently-used; a refused turn moves its
        key to the end, so a quiet conversation loses its latch first.

        Announced at most once per :data:`OVERFLOW_REPORT_SECS`: this runs on every
        ``check``, so a host above the cap would otherwise log on every turn.
        """
        windows_dropped = 0
        while len(self._windows) > self.max_tracked:
            self._windows.popitem(last=False)
            windows_dropped += 1
        latches_dropped = 0
        while len(self._latched) > self.max_tracked:
            self._latched.popitem(last=False)
            latches_dropped += 1
        if not (windows_dropped or latches_dropped):
            return
        self.windows_evicted += windows_dropped
        self.latches_evicted += latches_dropped
        now = time.monotonic()
        if now - self._last_overflow_report < OVERFLOW_REPORT_SECS:
            return
        self._last_overflow_report = now
        logger.warning(
            "turn-ceiling tracking cap reached (cap=%d): %d window(s) and %d latch(es) "
            "dropped since start -- an evicted conversation answers again and is "
            "counted from zero",
            self.max_tracked,
            self.windows_evicted,
            self.latches_evicted,
        )


#: The process-wide store. Channels share it so one conversation is one count
#: however many dispatch objects a host builds.
_SHARED = ConversationTurnCeiling()


def shared_ceiling() -> ConversationTurnCeiling:
    return _SHARED


#: Set once, by whoever owns a notification feed. Module-level rather than
#: per-channel, so a channel that forgets to wire it cannot hide its pauses.
_notification_sink: Optional[Callable[[str, str], None]] = None


def set_notification_sink(sink: Optional[Callable[[str, str], None]]) -> None:
    """Register the ``(session_key, surface) -> None`` observer for a pause.

    Called by the process that owns a notification feed. Passing ``None``
    unregisters, which is what a test does to leave no sink behind. A host with
    no sink still logs every pause at WARNING, and the user is still told in the
    conversation by the refusing channel, so the sink adds the operator's feed
    rather than being the only place a pause appears.
    """
    global _notification_sink
    _notification_sink = sink


def surface_of(session_key: str) -> str:
    """The channel surface a session key addresses.

    The key's first ``:``-segment (the channel's governance name). Only a label,
    so an unreadable key degrades to ``"channel"`` instead of raising in a gate. A
    key with no ``:`` is normal: a resumed dashboard session runs under the
    dashboard key, whose spelling is an opaque id.
    """
    head, sep, _ = session_key.partition(":")
    if not sep:
        return "channel"
    return head.strip() or "channel"


def _emit_latched_note(session_key: str) -> None:
    sink = _notification_sink
    if sink is None:
        return
    try:
        sink(session_key, surface_of(session_key))
    except Exception:
        # Best effort by construction: this runs inside a gate whose only job is
        # to refuse the turn, so a failing observer must not become a different
        # exception on the way out.
        logger.debug("turn-ceiling notification failed session=%s", session_key, exc_info=True)


async def render_refusal(renderer: Any, exc: TurnCeilingExceeded) -> None:
    """Put the refusal into a channel's OWN output stream, and end that stream.

    ``on_done`` is required: a channel's ``close()`` flushes nothing, and a
    throttled stream holds back the trailing word, so without it the notice is
    one word short and the stream never sealed. ``on_done`` is idempotent under
    the later ``close()``.

    Silent unless ``exc.announce`` (see :class:`TurnCeilingExceeded`). Best
    effort: a renderer failure must not replace a clean refusal with a traceback.
    """
    if not exc.announce:
        return
    try:
        await renderer.dispatch(OutputEvent(kind=TEXT_CHUNK, text=str(exc)))
        await renderer.on_done()
    except Exception:
        logger.debug("turn-ceiling notice could not be rendered", exc_info=True)


def gate(
    session_key: str,
    inner: Optional[Callable[[], None]] = None,
    *,
    ceiling: Optional[ConversationTurnCeiling] = None,
) -> Callable[[], None]:
    """Compose the ceiling onto a channel's existing pre-stream gate.

    The returned callable is what a channel passes as its pre-stream gate. It
    runs *inner* FIRST and the ceiling second, which is load-bearing in both
    directions: a shutdown refusal (or any other reason the channel's own gate
    raises) keeps behaving exactly as it does without a ceiling, and a turn the
    channel is not going to run is not counted against the conversation.

    A turn inside :func:`generated_turn` runs *inner* and skips the ceiling, but
    only the FIRST such gate in that span: see :data:`_generated_turn` for why a
    span-wide pass would also exempt a queue drain's replayed inbound messages.

    Yield-free, because the gate it replaces must not await: the gate, monitor
    acceptance, and the stream's turn registration are one event-loop span.
    """

    def _gate() -> None:
        if inner is not None:
            inner()
        if _spend_generated_turn():
            return
        (ceiling or _SHARED).check(session_key)

    return _gate
