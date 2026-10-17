"""Start priority: which waiting session start a bounded start queue serves next.

Three in-process queues bound an agent start, and all three are a
:class:`PrioritySemaphore`: ``SessionManager._start_sem`` (provider cold starts),
the gateway-wide cold-start admission (runtime spawn + ``initialize``) and the
``SessionStartGate`` (``session/new``).

THE RULE (stated here once; call sites and docs point back to it).

* Every start is ``BACKGROUND`` unless its caller claims ``FOREGROUND`` explicitly,
  and a caller claims it only where a person is waiting on that start. Nothing is
  derived from a session key: automation runs on interactive keys too (an auto-nudge
  wake, a cron or sub-agent completion injection, an agent's ``session_send``), so a
  key says where a turn lands, not who is waiting for it.
* The claimers are listed in ``docs/system-specs/modules/acp-client.md``
  § Session-start gate.

Ordering: FIFO within a priority, ``FOREGROUND`` first, bounded so background is
never starved: after :data:`_FOREGROUND_BYPASS_LIMIT` consecutive ``FOREGROUND``
grants that passed a queued ``BACKGROUND`` waiter, the oldest ``BACKGROUND`` waiter is
served next. Strict priority plus a named bypass rather than weighted round-robin,
because a person waiting must win outright whenever the bypass allows.

This module is neutral on purpose: the session layer and the ACP runtime both read
it, and application code may not import ``kiro_crew.acp``
(``scripts/check_agent_sdk_boundary.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
from collections import deque
from typing import AsyncIterator, Callable


class StartPriority(enum.Enum):
    """Who is waiting on a start (rule: the module docstring).

    Exactly two members: :class:`PrioritySemaphore` keeps one queue for each, so a
    third member would have no queue to wait in (pinned by ``test_start_priority``).
    """

    FOREGROUND = "fg"
    BACKGROUND = "bg"


def person_priority(person_waiting: bool) -> StartPriority:
    """FOREGROUND when a caller's own evidence says a person is waiting, else BACKGROUND."""
    return StartPriority.FOREGROUND if person_waiting else StartPriority.BACKGROUND


# How many FOREGROUND grants in a row may pass a queued BACKGROUND waiter before the
# oldest BACKGROUND waiter is served (see the module docstring).
_FOREGROUND_BYPASS_LIMIT = 4

# The three queues, as a start-queue callback names them (``notify_start_queue``).
START_QUEUE_COLD_START = "cold-start"
START_QUEUE_ADMISSION = "spawn admission"
START_QUEUE_SESSION_NEW = "session/new"
# Not a permit queue: the per-parent lock a shared-path subagent waits on while a
# sibling spawns the parent's companion runtime, which it pauses its start clock
# across like one.
START_QUEUE_COMPANION = "companion runtime"

# A queue wait shorter than this is not worth a log line: an uncontended acquire
# still measures a few microseconds.
START_QUEUE_LOG_MIN_MS = 100.0


def notify_start_queue(
    logger: logging.Logger, callback: Callable[..., None] | None, *args: object
) -> None:
    """Fire a caller's start-queue entry/exit callback; an ``Exception`` from it is
    logged, never fatal to the start it describes."""
    if callback is None:
        return
    try:
        callback(*args)
    except Exception:
        logger.debug("start-queue callback raised", exc_info=True)


class PrioritySemaphore:
    """A counting semaphore that serves ``FOREGROUND`` waiters before ``BACKGROUND``.

    The acquire/release shape of :class:`asyncio.Semaphore`, except that ``release``
    takes the priority the permit was acquired at, and one naming a class that holds
    nothing raises rather than corrupting the per-class count (a mismatch while that
    class holds another permit is indistinguishable from that holder's own release,
    so every caller releases at the priority it acquired: through :meth:`held` or
    :meth:`drain`, or an explicit release naming that priority). FIFO within a
    priority, ordering per the module docstring. A freed permit is handed directly
    to the waiter it wakes, so a newcomer cannot take it first; a waiter granted and
    then cancelled before it resumed hands the permit on.

    ``foreground_reserve`` permits are ones ``BACKGROUND`` may never hold, so
    ``BACKGROUND`` gets ``value - foreground_reserve`` and the reserve sits on top of
    that. It exists for a semaphore whose holders wait in further queues while
    holding it (``SessionManager._start_sem``), where ordering alone cannot help a
    person once every permit is held by a background start stuck further in.

    :meth:`drain` takes EVERY permit and outranks both priorities while it waits.

    ``_value`` is the free-permit count, spelled as :class:`asyncio.Semaphore` spells
    it. Loop-agnostic until a waiter queues: a waiter's future is created on the
    running loop, which is why the gateway keys its admission registries by loop.
    """

    def __init__(self, value: int, *, foreground_reserve: int = 0) -> None:
        if value < 1:
            raise ValueError("PrioritySemaphore needs at least one permit")
        if not 0 <= foreground_reserve < value:
            raise ValueError(
                f"foreground_reserve must leave background a permit: got {foreground_reserve} "
                f"of {value}"
            )
        self._limit = value
        self._value = value
        self._background_cap = value - foreground_reserve
        self._held = {StartPriority.FOREGROUND: 0, StartPriority.BACKGROUND: 0}
        self._waiters: dict[StartPriority, deque[asyncio.Future[None]]] = {
            StartPriority.FOREGROUND: deque(),
            StartPriority.BACKGROUND: deque(),
        }
        self._queued = {StartPriority.FOREGROUND: 0, StartPriority.BACKGROUND: 0}
        # FOREGROUND grants since the oldest queued BACKGROUND waiter arrived.
        self._foreground_streak = 0
        #: FOREGROUND grants that passed a queued, grantable BACKGROUND waiter.
        self.bypasses = 0
        self._drain_held: int | None = None
        self._drain_full: asyncio.Future[None] | None = None

    @property
    def held_count(self) -> int:
        """Permits held now, both priorities (a pending drain's excluded)."""
        return self._held[StartPriority.FOREGROUND] + self._held[StartPriority.BACKGROUND]

    @property
    def queued(self) -> int:
        """Acquires waiting now, both priorities."""
        return self._queued[StartPriority.FOREGROUND] + self._queued[StartPriority.BACKGROUND]

    def queued_by_priority(self) -> tuple[int, int]:
        """``(FOREGROUND, BACKGROUND)`` acquires waiting now."""
        return self._queued[StartPriority.FOREGROUND], self._queued[StartPriority.BACKGROUND]

    def describe(self) -> str:
        """The queue's state for a log line: ``queued_fg=… queued_bg=… bypasses=…``."""
        foreground, background = self.queued_by_priority()
        return f"queued_fg={foreground} queued_bg={background} bypasses={self.bypasses}"

    def _live_waiters(self, priority: StartPriority) -> int:
        """Queued futures of *priority* that are still waiting."""
        return sum(1 for w in self._waiters[priority] if not w.done())

    @property
    def limit(self) -> int:
        """Total permits, both priorities (the reserve included)."""
        return self._limit

    def widen_to(self, value: int) -> None:
        """Grow to *value* total permits and hand the new ones on; never shrinks.

        For a bound whose width is only known after the semaphore was built (a
        host reading taken off the boot path). Every added permit is one
        ``BACKGROUND`` may hold: the foreground reserve stays the same size. A
        pending :meth:`drain` takes the new permits like any freed ones, and it
        now waits for the larger total. Call on the loop that owns the waiters.
        """
        extra = int(value) - self._limit
        if extra <= 0:
            return
        self._limit += extra
        self._value += extra
        self._background_cap += extra
        self._dispatch()

    def locked(self, priority: StartPriority = StartPriority.BACKGROUND) -> bool:
        """True when an ``acquire(priority)`` now would wait."""
        return not self._may_take_now(priority)

    def _may_hold(self, priority: StartPriority) -> bool:
        return (
            priority is StartPriority.FOREGROUND
            or self._held[StartPriority.BACKGROUND] < self._background_cap
        )

    def _may_take_now(self, priority: StartPriority) -> bool:
        # While a permit is free nobody grantable is queued (a release hands it on at
        # once), so a newcomer only needs a free permit it is allowed to hold.
        return self._value > 0 and self._drain_held is None and self._may_hold(priority)

    def _grant(self, priority: StartPriority) -> None:
        self._value -= 1
        self._held[priority] += 1

    async def acquire(self, priority: StartPriority = StartPriority.BACKGROUND) -> bool:
        if not isinstance(priority, StartPriority):
            raise TypeError(f"not a StartPriority: {priority!r}")
        if self._may_take_now(priority):
            self._grant(priority)
            return True
        if priority is StartPriority.BACKGROUND and not self._live_waiters(priority):
            # First background waiter in the queue: the streak counts foreground
            # grants since it arrived, so a streak left by an earlier (cancelled)
            # waiter must not carry over. Read from the deque, which a cancel
            # updates at once, rather than from the count a cancelled waiter
            # decrements only when its task resumes.
            self._foreground_streak = 0
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        queue = self._waiters[priority]
        queue.append(waiter)
        self._queued[priority] += 1
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter.cancelled():
                # Never granted, so it stops waiting here: removed at once rather
                # than left for the head sweep, so it cannot linger mid-queue.
                self._queued[priority] -= 1
                with contextlib.suppress(ValueError):
                    queue.remove(waiter)
            else:
                # Granted, then cancelled before this task resumed: pass it on.
                self.release(priority)
            raise
        return True

    def release(self, priority: StartPriority) -> None:
        # A release must name the priority its permit was acquired at, or the
        # per-class counts (and with them the reserve) drift silently. What is
        # catchable here is a release naming a class that holds nothing.
        if self._held[priority] <= 0:
            raise RuntimeError(f"release({priority.name}) with no {priority.name} permit held")
        self._held[priority] -= 1
        self._value += 1
        self._dispatch()

    def _dispatch(self) -> None:
        """Hand free permits on: to a pending drain first, else to the next waiters.

        A loop, not one grant: a drain's exit frees every permit at once, and a
        background release can make a cap-blocked waiter grantable as well.
        """
        if self._drain_held is not None:
            self._drain_held += self._value
            self._value = 0
            full = self._drain_full
            if full is not None and not full.done() and self._drain_held >= self._limit:
                full.set_result(None)
            return
        while self._value > 0:
            picked = self._next_waiter()
            if picked is None:
                return
            waiter, priority = picked
            self._queued[priority] -= 1
            self._grant(priority)
            waiter.set_result(None)

    def _next_waiter(self) -> "tuple[asyncio.Future[None], StartPriority] | None":
        """Pop the waiter the next permit goes to, or None when nobody may take it."""
        foreground = self._waiters[StartPriority.FOREGROUND]
        background = self._waiters[StartPriority.BACKGROUND]
        for queue in (foreground, background):
            while queue and queue[0].done():
                queue.popleft()
        background_waits = bool(background) and self._may_hold(StartPriority.BACKGROUND)
        if background_waits and (
            not foreground or self._foreground_streak >= _FOREGROUND_BYPASS_LIMIT
        ):
            self._foreground_streak = 0
            return background.popleft(), StartPriority.BACKGROUND
        if foreground:
            if background_waits:
                self._foreground_streak += 1
                self.bypasses += 1
            return foreground.popleft(), StartPriority.FOREGROUND
        return None

    @contextlib.asynccontextmanager
    async def held(self, priority: StartPriority) -> AsyncIterator[None]:
        """``async with``, at *priority*, released at that same priority."""
        await self.acquire(priority)
        try:
            yield
        finally:
            self.release(priority)

    @contextlib.asynccontextmanager
    async def drain(self) -> AsyncIterator[None]:
        """Hold every permit for the body: a barrier no start can be inside.

        Takes the free permits at once; while it waits for the rest, every permit
        released goes to it before any waiter of either priority, and the reserve
        does not apply. It therefore waits for every holder already in, background
        ones included. On exit -- cancellation included, part-way through -- it hands
        back exactly what it holds. Not re-entrant: a second concurrent drain raises.
        """
        if self._drain_held is not None:
            raise RuntimeError("PrioritySemaphore.drain is not re-entrant")
        self._drain_held = 0
        self._drain_full = asyncio.get_running_loop().create_future()
        try:
            self._dispatch()
            await self._drain_full
            yield
        finally:
            held, self._drain_held, self._drain_full = self._drain_held or 0, None, None
            self._value += held
            self._dispatch()

    async def __aenter__(self) -> None:
        await self.acquire(StartPriority.BACKGROUND)

    async def __aexit__(self, *exc: object) -> None:
        self.release(StartPriority.BACKGROUND)
