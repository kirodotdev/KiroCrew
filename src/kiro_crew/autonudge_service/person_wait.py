"""The person-wait hold a legacy loop's own agent declares.

A legacy loop that has asked the user something -- to approve a force-push by SHA,
to choose between two fixes -- has nothing to do until they answer, and every cycle
it fires meanwhile spends its cap and its budget on a turn that can only report it
is still waiting. ``monitor_update(waiting_on_person=true)`` declares that wait.
The declared hold reuses the ledger hold's fields (``waiting_on_person`` and its
start stamp), marked ``person_wait_declared``:

* **Held.** ``_timer`` returns at the hold point without arming anything and the
  reconciler skips the loop, so a declared hold fires no cycle, spends neither the
  cycle cap nor the runtime budget (the span is credited back on release), and is
  never stopped by a bound while it waits.
* **Released** by a person on the signs of presence that end the approval hold --
  a message typed into the dashboard chat, an answered approval, the popover's Fire
  now -- through ``release_approval_hold``, and by the agent passing ``false``. A
  work-ledger read neither holds nor releases a declared hold.

Its function that takes the service as ``self`` is an
:class:`~kiro_crew.autonudge.AutoNudgeService` method, bound on the class by name.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.autonudge_service.firing import _commit_person_wait
from kiro_crew.autonudge_service.model import is_structured_monitor_loop

if TYPE_CHECKING:
    from kiro_crew.autonudge import AutoNudgeService

# The service's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.autonudge")


async def declare_person_wait(self: AutoNudgeService, slot_key: str, *, held: bool) -> bool:
    """Set (*held*) or end the declared person-wait hold on *slot_key*'s legacy loop.

    True once the loop is in the requested state, including when it already was.
    False for a slot with no loop, an inactive loop or a structured monitor (none of
    them can hold this way), and for a write the store refused, which leaves the
    loop exactly as the store has it. The commit rechecks the captured row under the
    service lock, so a concurrent stop or replacement publishes no hold.

    The hold cancels the loop's pending timer, except mid-fire, where cancelling
    would abandon the cycle's own persist; the tick that runs next returns at the
    hold point anyway. ``False`` ends only a DECLARED hold: a hold the work ledger
    found stays the ledger's. Its release credits the held span back to the budget
    and re-arms toward the loop's deadline.
    """
    loop = self._find_by_slot(slot_key)
    if loop is None or not loop.active or is_structured_monitor_loop(loop):
        return False
    declared = bool(loop.person_wait_declared and loop.waiting_on_person)
    if held == declared:
        return True
    if held:
        if not await _commit_person_wait(self, loop, held=True, declared=True):
            return False
        if loop.id not in self._firing:
            self._cancel_timer(loop.id)
        self._rearm_pending.discard(loop.id)
        logger.warning(
            "AutoNudge: loop %s holds -- its agent is waiting on a person, so it fires "
            "nothing until someone replies in that session, answers an approval or "
            "fires it",
            loop.id,
        )
    else:
        if not await _commit_person_wait(self, loop, held=False):
            return False
        logger.warning("AutoNudge: loop %s released its person-wait hold", loop.id)
        if loop.active and self._loops.get(loop.id) is loop:
            if loop.id in self._firing:
                self._rearm_pending.add(loop.id)
            else:
                self._arm_from_deadline(loop)
    self._emit("updated", loop)
    return True
