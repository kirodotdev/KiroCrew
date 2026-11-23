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
    """Push the slot list after one of the three sources moved. Never raises."""
    push = getattr(state, "push_slots_update", None)
    if push is None:
        return
    try:
        push()
    except Exception:
        logger.debug("full-chat flag: slots push failed", exc_info=True)


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
