"""When dashboard state reaches websocket clients, and how.

The slot patch fields, the dynamic card store, the broadcast clock and the
deferred-flush delay.
"""

from __future__ import annotations

import time
from typing import Any

#: Fields whose dashboard-user projection is identical for every slot-patch
#: audience. Per-audience fields such as ``source_links`` require a full frame.
_SLOT_PATCH_FIELDS = frozenset({"pinned", "mutes_opened", "muted", "title", "folder_id"})


def _new_card_store(state: Any) -> Any:
    """*state*'s dynamic-card store, constructed on first use. THE one constructor.

    A module-level function rather than a method, because both callers need it and one of them
    (``set_dynamic_cards_enabled``) is exercised against bare stub objects that carry only
    ``_dynamic_cards`` -- a sibling method call would not resolve on those.

    The import stays INSIDE the function: it is the deferred one that keeps the card lifecycle
    off the gateway's boot stack, and a test asserts the module is not imported until a caller
    actually needs a store.
    """
    if state._dynamic_cards is None:
        from kiro_crew.dashboard.card_lifecycle import CardLifecycle

        state._dynamic_cards = CardLifecycle(state)
    return state._dynamic_cards


def _slots_broadcast_clock() -> float:
    """The one clock the slots coalescing window is measured on.

    Every read that stamps or measures the window goes through here, so a test
    can drive the window on a fake clock without replacing ``time.monotonic``
    for the event loop and every other caller in the process.
    """
    return time.monotonic()


# A successful plain persistent-memory create hands its full-list publication past
# the HTTP response by this fixed interval. Callers may name the operation only.
_DEFERRED_SLOTS_FLUSH_DELAY_S: float = 0.01
