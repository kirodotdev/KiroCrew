"""Hold a loop's scheduled tick while host memory is critical, and run it once memory recovers.

Every tick can start a turn, and a turn can start a session runtime and its MCP
servers; a conductor's turn can start or steer its workers too. On a host already at
critical memory that is how a fleet of loops tips it into a stall. So a scheduled tick
that comes due while :func:`resource_status.admission_check` refuses fires nothing: it
re-arms :data:`CRITICAL_HOLD_RECHECK_SECS` out and asks again. The loop stays active
and spends no cycle, and the first tick after memory leaves critical runs as usual.
The held time counts toward ``max_runtime_secs``, which bounds wall time, as the boot
pacer's wait does: a loop whose budget ran out while held stops as ``runtime_budget``
on its first admitted tick.

Only prompt loops are held, gated work-ledger watches included. A structured
monitor (:func:`~kiro_crew.autonudge_service.model.is_structured_monitor_loop`)
never is: its tick runs the typed controller, which mostly reads a provider and
settles claims, and holding it would keep a watch ``wake_in_flight`` past its
completion-evidence deadline and refuse its replacement until memory recovers.

Only the loop's own scheduled ticks are held. A tick a person's Fire now press armed
(``fire_now`` marks it in ``_manual_ticks``) runs at once: the press is their own
call, like a chat turn, which is never held either. A pushed tick (a worker's report,
or the posture-change wake of rfc-host-memory-guard §5.2, both marked in
``_pushed_ticks``) runs too: those wakes exist so a conductor can act at critical.
So does any tick of a loop that owes a wake (an entry in ``_pending_monitor_wake``,
such as a pushed report whose delivery was refused and is now retried): that wake
already saw news, and holding it would only delay the report.

The check is the same one scheduled cron firings and TaskRunner steps pass: only a
critical posture refuses; ``ample``, ``tight`` and an unreadable probe admit, and
``agent.admission_gate: false`` turns the hold off.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from kiro_crew.autonudge_service.model import is_structured_monitor_loop

if TYPE_CHECKING:
    from kiro_crew.autonudge import AutoNudgeService
    from kiro_crew.autonudge_service.model import NudgeLoop

# Logged under the service's name, as every autonudge owner is.
logger = logging.getLogger("kiro_crew.autonudge")

#: How long a held tick waits before it reads memory again. The notifier samples the
#: posture on the same cadence, so a shorter wait would mostly re-read the same value.
CRITICAL_HOLD_RECHECK_SECS = 30.0


async def probe_admission() -> tuple[bool, str]:
    """``(admitted, reason)`` from a fresh ``resource_status.admission_check``.

    The check reads ``/proc`` and the config, so it runs off the loop. It never
    raises and fails open; anything escaping it here admits too.
    """
    from kiro_crew import resource_status

    try:
        decision = await asyncio.to_thread(resource_status.admission_check)
    except Exception:  # noqa: BLE001 - the hold must never strand a loop
        logger.debug("AutoNudge: memory admission check failed", exc_info=True)
        return True, ""
    return decision.admitted, decision.reason


#: The reader :func:`holds_for_critical_memory` uses, looked up at each read.
read_admission = probe_admission


async def holds_for_critical_memory(svc: AutoNudgeService, loop: NudgeLoop) -> bool:
    """True when *loop*'s tick was held and re-armed; False when it may run now."""
    manual = loop.id in svc._manual_ticks
    svc._manual_ticks.discard(loop.id)
    # A structured monitor's tick runs the typed controller, not a prompt turn.
    if is_structured_monitor_loop(loop):
        return False
    # Read, never consumed: ``_run_timer_callback`` moves the push mark right after.
    pushed = loop.id in svc._pushed_ticks
    # An owed wake already saw news (its push mark may be spent on a refused
    # delivery); firing.py owns the entry, so it is only read here.
    owed = loop.id in svc._pending_monitor_wake
    # A person's press and a pushed or owed wake run now. An inactive loop's tick
    # only settles it, and holding it would re-arm it forever.
    exempt = manual or pushed or owed or not loop.active
    admitted, reason = (True, "") if exempt else await read_admission()
    if admitted:
        if svc._memory_held.pop(loop.id, None) is not None:
            logger.info("AutoNudge: loop %s released from the memory hold", loop.id)
        return False
    if loop.id not in svc._memory_held:
        logger.info(
            "AutoNudge: holding loop %s -- %s; it runs once memory leaves critical",
            loop.id,
            reason or "host memory is critical",
        )
        svc._memory_held[loop.id] = reason
    svc._arm_timer(loop, delay=CRITICAL_HOLD_RECHECK_SECS)
    return True
