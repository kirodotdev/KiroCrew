"""The pause, drain and fence a restart takes before it may cut the process.

A user-clicked restart (``updates_apply._restart_gateway``, behind the dashboard
and desktop Restart buttons) is the only in-app path that cuts the process; the
scheduler has none (decision entry "updates never restart a running gateway").
``admit_restart`` is the sequence it takes before it saves slots and calls
``close_all()``:

1. take the update pause (new turns are refused and spooled, not started);
2. drain callbacks and refusal writes, bounded (and, unless forced, the running
   turn tasks: a forced restart cuts those, so it never waits on them);
3. unless forced, read the quiescence census and REFUSE if work is in flight,
   reopening admission so the gateway keeps serving;
4. fence the pause so the final drain cannot be raced.

The wait is bounded: the drain by ``_UPDATE_DRAIN_TIMEOUT_SECS`` and the pause
by the session manager's lock. The census is never waited on, because a refusal
is cheap and the caller can ask again; waiting would hold admission closed for
a restart that may never come.

"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from kiro_crew import quiescence, shutdown_event

logger = logging.getLogger(__name__)

#: ``_update_pause_holder`` while a dashboard restart owns the pause.
HOLDER = "dashboard_restart"

REASON_SHUTTING_DOWN = "shutting_down"
REASON_BUSY = "work_in_flight"
REASON_PAUSE_HELD = "pause_held_elsewhere"
REASON_DRAIN = "callbacks_not_drained"
REASON_FENCE = "fence_lost"


@dataclass(frozen=True)
class RestartAdmission:
    """Whether the restart may proceed, and the census that decided it."""

    admitted: bool
    reason: str = ""
    census: quiescence.Quiescence | None = None

    def detail(self) -> str:
        """One sentence for the progress stream and the log."""
        if self.admitted:
            return ""
        if self.reason == REASON_BUSY and self.census is not None:
            c = self.census
            return (
                "Restart postponed so running work is not interrupted "
                f"(turns {c.turns}, background {c.background}, "
                f"queued messages {c.queued_messages}, "
                f"pending approvals {c.pending_approvals}). "
                "Try again when everything is idle."
            )
        return {
            REASON_SHUTTING_DOWN: "Restart skipped: the gateway is shutting down.",
            REASON_PAUSE_HELD: "Restart postponed: another update step holds turn admission.",
            REASON_DRAIN: "Restart postponed: incoming messages did not settle in time.",
            REASON_FENCE: "Restart postponed: the gateway began shutting down.",
        }.get(self.reason, "Restart postponed.")


def preflight_restart(orchestrator: Any) -> RestartAdmission:
    """The refusals that need no side effect, read before the request answers 200."""
    if shutdown_event.is_set():
        return RestartAdmission(False, REASON_SHUTTING_DOWN)
    if getattr(orchestrator, "_update_pause_holder", None) is not None:
        return RestartAdmission(False, REASON_PAUSE_HELD)
    return RestartAdmission(True)


async def admit_restart(orchestrator: Any, *, force: bool = False) -> RestartAdmission:
    """Pause, drain, judge and fence for a dashboard restart.

    ``force`` skips the work judgement only: the pause, the drain and the fence
    still run, so a forced restart cuts running turns but never races an
    inbound message.
    """
    if shutdown_event.is_set():
        return RestartAdmission(False, REASON_SHUTTING_DOWN)
    sessions = getattr(orchestrator, "sessions", None)
    # The explicit restart is itself a person's act, so only the structural half
    # of quiescence applies: the typing window is for the unattended paths.
    if sessions is None:
        census = quiescence.assess(orchestrator, quiet_window_secs=0.0)
        if census.busy > 0 and not force:
            return RestartAdmission(False, REASON_BUSY, census)
        return RestartAdmission(True)

    if getattr(orchestrator, "_update_pause_holder", None) is not None:
        return RestartAdmission(False, REASON_PAUSE_HELD)
    try:
        paused = await sessions.pause_turn_admission_for_update()
    except Exception:
        logger.exception("Restart could not pause turn admission")
        paused = False
    if not paused:
        return RestartAdmission(False, REASON_PAUSE_HELD)
    # Read with no await since the pause returned: the pause is re-entrant,
    # so whoever took it while this waited for the lock still owns it.
    if getattr(orchestrator, "_update_pause_holder", None) is not None:
        return RestartAdmission(False, REASON_PAUSE_HELD)
    orchestrator._update_pause_holder = HOLDER

    async def refuse(reason: str, census: quiescence.Quiescence | None = None) -> RestartAdmission:
        await release_restart(orchestrator)
        return RestartAdmission(False, reason, census)

    # A confirmed restart cuts the running turns (close_all unwinds them), so
    # it waits only for inbound callbacks and refusal writes: that is the part
    # that keeps it from racing an inbound message.
    drained = await orchestrator._drain_update_callback_work(
        timeout=orchestrator._UPDATE_DRAIN_TIMEOUT_SECS, include_turns=not force
    )
    if not drained:
        return await refuse(REASON_DRAIN)
    judged = quiescence.assess(orchestrator, quiet_window_secs=0.0)
    if judged.busy > 0 and not force:
        logger.info("Restart refused: %s", RestartAdmission(False, REASON_BUSY, judged).detail())
        return await refuse(REASON_BUSY, judged)
    if not sessions.fence_update_restart():
        return await refuse(REASON_FENCE)
    return RestartAdmission(True, "", judged)


async def release_restart(orchestrator: Any) -> None:
    """Reopen admission after a restart that did not replace the process."""
    finish = getattr(orchestrator, "_finish_auto_update_apply", None)
    if callable(finish):
        await finish()


def wire(state: Any, orchestrator: Any) -> None:
    """Hand the dashboard the orchestrator's admission and census (the seams on its state)."""
    state._restart_admission = lambda *, force=False: admit_restart(orchestrator, force=force)
    state._restart_preflight = lambda: preflight_restart(orchestrator)
    state._restart_release = lambda: release_restart(orchestrator)
    state._quiescence_probe = lambda *, quiet_window_secs: quiescence.assess(
        orchestrator, quiet_window_secs=quiet_window_secs
    )
