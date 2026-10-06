"""The queued depth each parent last published, read back by the slot list.

The ``subagent_queued`` event is the dashboard's fast signal for "N waiting to
start", and nothing re-sends a frame a client missed: a socket that dropped one,
or a reducer that never applied it, keeps the count it last saw until a
reconnect. The slot list therefore carries the depth the manager last
PUBLISHED for each slot (``subagents_queued`` beside ``subagents_running``,
routed to the tab the frames went to), so every slots push reconciles a client
to the frame stream, including a client that is not showing that session.

It is the published value, never a new read: ``serialize_slots`` runs on the
event loop for every tab, and the store half of the depth is a task-store
count. Every publish already goes through ``_fire_event``, so recording there
keeps the slot list and the frame stream on one value by construction.

A held entry is re-derived from state: every child start or end for a parent
with a non-zero entry re-reads the count (``_fire_event``) and publishes it when
it differs from the entry, so a path that pops or ends a row without emitting
cannot leave a count in the table for the slot list to keep re-sending, and a
path that did emit is not answered with a second, identical frame.

Bounded: a depth of 0 is not stored, a parent end forgets its entry
(``SubagentManager.cancel_for_teardown``), and past :data:`MAX_PUBLISHED_PARENTS`
the oldest entry goes. An evicted entry reads as 0 until that parent's next
frame, which re-records it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Parents with a non-zero published depth kept at once. Each entry is one short
#: key and an int; the bound only matters for a gateway with that many parents
#: holding queued spawns at the same moment.
MAX_PUBLISHED_PARENTS = 1024


class PublishedQueueDepths:
    """Last published ``subagent_queued`` depth per parent session key."""

    __slots__ = ("_depths",)

    def __init__(self) -> None:
        self._depths: dict[str, int] = {}

    def record(self, parent_session_key: str, depth: object) -> None:
        """Remember the depth a frame just published for *parent_session_key*.

        A depth that is not an int (a malformed frame) leaves the entry as it
        was, rather than guessing a count for it.
        """
        if not parent_session_key or not isinstance(depth, int) or isinstance(depth, bool):
            return
        # Re-inserted, not updated in place, so the dict's order is recency and
        # the eviction below drops the parent that published longest ago.
        self._depths.pop(parent_session_key, None)
        if depth <= 0:
            return
        self._depths[parent_session_key] = depth
        while len(self._depths) > MAX_PUBLISHED_PARENTS:
            evicted = next(iter(self._depths))
            del self._depths[evicted]
            logger.debug("Published queue depth table full; dropped %s", evicted)

    def get(self, parent_session_key: str) -> int:
        """The depth last published for *parent_session_key*, 0 when none is held."""
        return self._depths.get(parent_session_key, 0)

    def snapshot(self) -> dict[str, int]:
        """Every held entry (parent session key -> depth), as a copy."""
        return dict(self._depths)

    def forget(self, parent_session_key: str) -> None:
        """Drop *parent_session_key*'s entry (its parent ended)."""
        self._depths.pop(parent_session_key, None)
