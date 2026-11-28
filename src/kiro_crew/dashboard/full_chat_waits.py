"""The slot list's ``needs_full_chat``: a session waits on something ChatEmbed cannot draw.

That is a change card the person can still act on, a live guide offer, or a browser
``ask`` parked on the live card. Each is read from the store that owns it, never from
transcript rows, which are agent-writable. The read runs on every slots frame, so it
does no disk work: the change-card store is loaded at gateway start.

The dashboard state imports this module at boot, so every import of a store module
below is deferred to first use (``no-new-work-on-gateway-boot-path``).
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Imported once and kept: a per-slot ``from`` import probes the ``change_cards``
#: facade's ``__path__`` on every slots frame (``test_facade_read_budget``).
_READERS: tuple[Callable[..., Any], Callable[..., Any], Any] | None = None


def _readers() -> tuple[Callable[..., Any], Callable[..., Any], Any]:
    global _READERS
    if _READERS is None:
        # Deferred: boot path (module docstring).
        import kiro_crew.browser_harness.live_hub as live_hub
        from kiro_crew.dashboard.change_cards import card_store_for
        from kiro_crew.dashboard.guide_runs import guide_store_for

        _READERS = (card_store_for, guide_store_for, live_hub)
    return _READERS


def _parked_browser_ask(slot_key: str) -> bool:
    """An ask bound on *slot_key*'s live card, with no outcome, before its deadline.

    The deadline is checked here because the hub settles a lapsed ask only on its
    tool's next poll, and a tool cut off mid-run never polls again.
    """
    sess = _readers()[2].get_live_hub().get(slot_key)
    ask = getattr(sess, "ask", None) if sess is not None else None
    if ask is None or ask.outcome:
        return False
    return int(time.time() * 1000) < int(ask.expires_at or 0)


#: The start-up load of the change-card store, held so it is not collected while it runs.
_STARTUP: set["asyncio.Task[None]"] = set()


async def _load_cards(state: Any) -> None:
    # Deferred: boot path, and a circular import (the handler module imports this one).
    from kiro_crew.dashboard.handlers.change_cards import warm_after_start

    await warm_after_start(state)


async def startup_warm(app: Any) -> None:
    """``on_startup`` hook: load the change-card store in a task, awaiting nothing."""
    state = app.get("state")
    if state is None:
        return
    task = asyncio.get_running_loop().create_task(_load_cards(state))
    _STARTUP.add(task)
    task.add_done_callback(_STARTUP.discard)


def push_slot_list(state: Any) -> None:
    """Push the slot list after one of the three sources moved. Never raises.

    Every move of a source passes here, so this is also where the lapse timer is
    set: a wait that ends with nobody acting moves no source, and without the timer
    a host would keep its stale ``true`` until an unrelated push. The next lapse is
    read before the push, so a frame taken while a wait still holds always has a
    timer behind it, even when the wait ends during the push.
    """
    push = getattr(state, "push_slots_update", None)
    if push is None:
        return
    lapse = _read_lapse(state)
    try:
        push()
    except Exception:
        logger.debug("full-chat flag: slots push failed", exc_info=True)
    if lapse is not None:
        try:
            _set_lapse(state, *lapse)
        except Exception:
            logger.debug("full-chat flag: lapse timer not set", exc_info=True)


#: How long past a deadline the timer fires, so the wait it ends reads as over even
#: on a coarse clock (a loop timer may fire one ~16 ms tick early on Windows).
LAPSE_SLACK_S = 0.25

#: Each state's one lapse timer and its loop, at the earliest moment a held wait may end alone.
_LAPSE_TIMERS: (
    "weakref.WeakKeyDictionary[Any, tuple[asyncio.AbstractEventLoop, asyncio.TimerHandle]]"
) = weakref.WeakKeyDictionary()


def next_lapse_in(state: Any) -> float | None:
    """Seconds until the first held wait may end with nobody acting, else ``None``.

    Read from the same three stores as :func:`needs_full_chat`, without changing them.
    """
    card_store_for, guide_store_for, live_hub = _readers()
    waits = [card_store_for(state).next_close_in(), guide_store_for(state).next_lapse_in()]
    now = time.time()
    ask_ms = live_hub.get_live_hub().first_ask_deadline_ms(int(now * 1000))
    if ask_ms is not None:
        waits.append(ask_ms / 1000 - now)
    ahead = [w for w in waits if w is not None and w > 0]
    return min(ahead) if ahead else None


def _read_lapse(state: Any) -> tuple[asyncio.AbstractEventLoop, float] | None:
    """*state*'s serving loop and its loop time of the next lapse; ``None`` for neither."""
    loop = getattr(state, "serving_loop", None)
    if loop is None or loop.is_closed():
        return None
    try:
        delay = next_lapse_in(state)
    except Exception:
        logger.debug("full-chat flag: next lapse unreadable", exc_info=True)
        return None
    return None if delay is None else (loop, loop.time() + delay + LAPSE_SLACK_S)


def _set_lapse(state: Any, loop: asyncio.AbstractEventLoop, when: float) -> None:
    try:
        on_loop = asyncio.get_running_loop() is loop
    except RuntimeError:
        on_loop = False
    if on_loop:
        _place(state, loop, when)
    else:
        loop.call_soon_threadsafe(_place, state, loop, when)


def _place(state: Any, loop: asyncio.AbstractEventLoop, when: float) -> None:
    """Move the timer earlier, never later: when it fires, the next lapse is read afresh.

    Pushes from other threads can land out of order, so an older reading must not
    push a timer past a wait a newer one saw. An early fire costs one push. A timer
    on another loop, such as one that has closed, never fires, so it is replaced.
    """
    held = _LAPSE_TIMERS.get(state)
    if held is not None:
        old_loop, old = held
        if old_loop is loop and not old.cancelled() and old.when() <= when:
            return
        old.cancel()
    _LAPSE_TIMERS[state] = (loop, loop.call_at(when, _lapsed, state))


def _lapsed(state: Any) -> None:
    _LAPSE_TIMERS.pop(state, None)
    push_slot_list(state)


def needs_full_chat(state: Any, slot_key: str) -> bool:
    """Whether *slot_key* waits on a change card, a guide offer or a browser ask."""
    try:
        card_store_for, guide_store_for, _ = _readers()
        return (
            card_store_for(state).holds_open_card(slot_key)
            or guide_store_for(state).holds_live_guide(slot_key)
            or _parked_browser_ask(slot_key)
        )
    except Exception:
        logger.debug("full-chat flag unreadable for %s", slot_key, exc_info=True)
        return False
