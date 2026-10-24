"""The person-wait hold a legacy loop's own agent declares.

A legacy loop that has asked the user something -- to approve a force-push by SHA,
to choose between two fixes -- has nothing to do until they answer, and every cycle
it fires meanwhile spends its cap and its budget on a turn that can only report it
is still waiting. ``monitor_update(waiting_on_person=true)`` declares that wait.
The declared hold reuses the ledger hold's fields (``waiting_on_person`` and its
start stamp), marked ``person_wait_declared``:

* **Held.** ``_timer`` fires no cycle at the hold point, so a declared hold spends
  neither the cycle cap nor the runtime budget while it waits (the span is credited
  back when a person or the agent releases it). A loop that names one pull request
  reads it each held tick, one read interval apart (``subject_reads.hold_tick``),
  and settles when it merges or closes; the reconciler rescues that read like an
  approval hold's. A loop that names none reads nothing, and is armed no later than
  the hold's expiry, which the reconciler also rescues.
* **Bounded.** The hold expires ``PERSON_WAIT_MAX_SECS`` after it began, or when
  the runtime budget left at that moment runs out if that is sooner
  (``model.person_wait_expires_at``). While the declaring process runs, that
  remainder is measured by ``runtime_elapsed``'s monotonic clock and projected
  onto wall time for timers and API readings; after restart, the persisted
  wall-clock budget end is the fallback. An expired hold is charged to the
  budget, not credited, so repeated declarations never outlive
  ``max_runtime_secs``; the tick
  that finds it expired ends it and goes on down the ordinary path, where a spent
  bound stops the loop and otherwise a cycle fires so the agent gets a turn.
* **Released** by a person on the signs of presence that end the approval hold --
  a message typed into the dashboard chat, an answered approval, the popover's Nudge
  now -- through ``release_approval_hold``, and by the agent passing ``false``. A
  work-ledger read neither holds nor releases a declared hold. Why the last one
  ended is kept on ``person_wait_release``.
* **Exclusive with the approval hold.** Both wait for the same person, so a
  declaration while an approval hold is on records nothing, and an unanswered
  approval during a declared wait records no approval hold. Each release by a person
  or the agent credits the one span that held.

Its function that takes the service as ``self`` is an
:class:`~kiro_crew.autonudge.AutoNudgeService` method, bound on the class by name.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.autonudge_service.firing import _commit_person_wait
from kiro_crew.autonudge_service.model import (
    PERSON_WAIT_AGENT,
    PERSON_WAIT_RELEASES,
    _MutationAdmission,
    is_structured_monitor_loop,
)
from kiro_crew.autonudge_service.timers import _held_tick_delay, _run_admitted_transaction

if TYPE_CHECKING:
    from kiro_crew.autonudge import AutoNudgeService

# The service's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.autonudge")


async def declare_person_wait(
    self: AutoNudgeService,
    slot_key: str,
    *,
    held: bool,
    release: str = PERSON_WAIT_AGENT,
    expected_loop_id: str | None = None,
) -> bool:
    """Set (*held*) or end the declared person-wait hold on *slot_key*'s legacy loop.

    The mutation is admitted before its first await and runs in a supervised child,
    so shutdown drains a declaration accepted before closure and refuses one after
    closure. Cancellation cannot abandon its durable write.
    """
    if release not in PERSON_WAIT_RELEASES:
        raise ValueError(f"unknown person-wait release {release!r}")
    admission = self._admit_mutation()
    return await _run_admitted_transaction(
        self,
        _declare_person_wait_admitted(
            self,
            slot_key,
            held=held,
            release=release,
            expected_loop_id=expected_loop_id,
            admission=admission,
        ),
        cancel_callback_on_exit=True,
        on_failure_log="AutoNudge: detached person-wait declaration failed",
    )


async def _declare_person_wait_admitted(
    svc: AutoNudgeService,
    slot_key: str,
    *,
    held: bool,
    release: str,
    expected_loop_id: str | None,
    admission: _MutationAdmission,
) -> bool:
    """Apply one person-wait declaration under its caller's admission lease.

    True once the loop is in the requested state, including when it already was, and
    for a declaration while an approval hold is on, which writes nothing: that hold
    already waits for the person. False for a missing, inactive, replaced or
    structured loop, and for a write the store refused.

    The hold replaces the loop's pending timer with its next held tick: one read
    interval out when the loop names one pull request, and no later than the hold's
    expiry either way -- except mid-fire, where either would abandon the cycle's own
    persist; the tick that runs next returns at the hold point anyway. ``False`` ends
    only a DECLARED hold: a hold the work ledger found stays the ledger's. Its
    release credits the held span back to the budget and records why it ended.
    """
    async with svc._lock:
        loop = svc._find_by_slot(slot_key)
        if (
            loop is None
            or (expected_loop_id is not None and loop.id != expected_loop_id)
            or not loop.active
            or is_structured_monitor_loop(loop)
        ):
            return False
        declared = bool(loop.person_wait_declared and loop.waiting_on_person)
        if held == declared:
            return True
        if held and loop.approval_stalled:
            return True
    if held:
        if not await _commit_person_wait(
            svc,
            loop,
            held=True,
            declared=True,
            expected_loop_id=expected_loop_id,
            admission=admission,
        ):
            return False
        if not loop.person_wait_declared:
            return True
        if loop.id not in svc._firing:
            delay = _held_tick_delay(loop)
            if delay is None:
                svc._cancel_timer(loop.id)
            else:
                svc._arm_timer(loop, delay=delay)
        svc._rearm_pending.discard(loop.id)
        logger.warning(
            "AutoNudge: loop %s holds -- its agent is waiting on a person, so it fires "
            "nothing until someone replies in that session, answers an approval or "
            "fires it, or the hold expires",
            loop.id,
        )
    else:
        if not await _commit_person_wait(
            svc,
            loop,
            held=False,
            release=release,
            expected_loop_id=expected_loop_id,
            admission=admission,
        ):
            return False
        logger.warning("AutoNudge: loop %s released its person-wait hold", loop.id)
        if loop.active and svc._loops.get(loop.id) is loop:
            if loop.id in svc._firing:
                svc._rearm_pending.add(loop.id)
            else:
                svc._arm_from_deadline(loop)
    svc._emit("updated", loop)
    return True
