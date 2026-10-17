"""Between-cycle reads of a legacy loop's pull request, costing no model turn.

Two readers of the same one-call fact (``gh_pr.fetch_state``), each covering a
place the observation gate never reaches:

* **An ungated loop's sleep.** ``gate=False`` keeps the loop off the gate, so a
  terminal reading can never end it and a quiet reading can never suppress it --
  its owner reads comments and advisory findings the typed reading cannot see.
  That opt-out also meant nothing watched the pull request between cycles, so a
  head that went conflicting waited out the whole interval before its loop saw
  it. ``sleep_until_cycle`` reads the subject every ``SUBJECT_READ_SECS`` while
  the loop sleeps and ends the sleep early, once per head, when the head needs a
  re-sync. That is the only thing it may do: it never suppresses a cycle, never
  acts on a terminal reading, and an unread subject only goes on sleeping.
* **A held loop.** A loop paused for an unanswered approval fires nothing, which
  also left it blind: a pull request that merged during the hold kept its loop
  active and paused until someone came back. ``hold_tick`` reads the subject on
  every held tick; a merged or closed pull request settles the loop the way a
  gated loop settles on its terminal reading. The hold itself is unchanged: no cycle is
  fired, no bound is spent, and ``release_approval_hold`` re-arms it toward its
  deadline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from copy import deepcopy
from typing import TYPE_CHECKING

from kiro_crew import probes
from kiro_crew.autonudge_service.maintenance import _release_mutation_lock
from kiro_crew.autonudge_service.model import MONITOR_TERMINAL_REASON, NudgeLoop
from kiro_crew.autonudge_service.subject import loop_subject
from kiro_crew.monitoring.models import MonitorOutcome
from kiro_crew.probes import gh_pr

if TYPE_CHECKING:
    from kiro_crew.autonudge import AutoNudgeService

logger = logging.getLogger("kiro_crew.autonudge")

#: How often a sleeping ungated loop, or a held loop, reads its pull request.
SUBJECT_READ_SECS = 300.0


async def _nap(secs: float) -> None:
    """The sleep this module waits on: one seam, so a test counts sleeps without a clock."""
    await asyncio.sleep(secs)


def _pr_message(loop: NudgeLoop) -> str:
    """The probe message for the one pull request *loop* names, or ``""``."""
    target = loop_subject(loop)
    if target is None or target.kind != probes.GH_PR:
        return ""
    return target.message


async def _read_state(message: str) -> gh_pr.PrObservation | None:
    """One off-loop state read, or ``None`` when nothing usable came back."""
    try:
        observation = await asyncio.get_running_loop().run_in_executor(
            None, gh_pr.fetch_state, message
        )
    except Exception:
        # A message that can never parse is the gate's concern, not this read's;
        # here it, like every forge failure, is simply no reading.
        logger.debug("AutoNudge: between-cycle subject read failed", exc_info=True)
        return None
    return observation if observation.reached else None


async def sleep_until_cycle(svc: AutoNudgeService, loop: NudgeLoop, delay: float) -> None:
    """Sleep *delay* seconds, ending early when an ungated loop's head needs a re-sync.

    A loop with a monitor, a gated loop, a loop naming no single pull request and a
    sleep of at most one read interval each take one plain sleep. Time is
    counted from the sleeps themselves rather than a clock, so a cancelled or
    patched sleep cannot leave this spinning. ``CancelledError`` propagates.
    """
    if loop.gate or loop.monitor is not None or delay <= SUBJECT_READ_SECS or not _pr_message(loop):
        await _nap(delay)
        return
    remaining = delay
    while remaining > SUBJECT_READ_SECS:
        await _nap(SUBJECT_READ_SECS)
        remaining -= SUBJECT_READ_SECS
        # Re-derived each read: an instruction edit can retarget the loop mid-sleep.
        message = _pr_message(loop)
        if not message or not loop.active or loop.approval_stalled:
            continue
        observation, took = await _timed_read(message)
        remaining -= took
        if observation is None:
            continue
        kind = observation.conflict_kind
        if not kind:
            if observation.mergeability == "MERGEABLE":
                # Settled clean, so a later conflict on this same head is news again.
                svc._subject_wake_fp.pop(loop.id, None)
            continue
        fingerprint = f"{observation.head}:{kind}"
        if svc._subject_wake_fp.get(loop.id) == fingerprint:
            continue
        svc._subject_wake_fp[loop.id] = fingerprint
        logger.info(
            "AutoNudge: loop %s's pull request %s is %s -- firing its cycle now",
            loop.id,
            observation.subject,
            kind,
        )
        return
    await _nap(max(0.0, remaining))


async def _timed_read(message: str) -> tuple[gh_pr.PrObservation | None, float]:
    """One read and the seconds it took, so a slow forge does not push the cycle late."""
    started = time.monotonic()
    observation = await _read_state(message)
    return observation, max(0.0, time.monotonic() - started)


async def hold_tick(svc: AutoNudgeService, loop: NudgeLoop) -> None:
    """One tick of an approval-held legacy loop: read its subject, maybe settle.

    Fires no cycle. A held loop that is still held afterwards is re-armed one read
    interval out; a release re-arms it toward its own deadline instead, replacing
    this timer.
    """
    message = _pr_message(loop)
    if message:
        observation = await _read_state(message)
        if observation is not None and observation.is_terminal:
            if await _settle_terminal(svc, loop, message, merged=observation.merged):
                return
    if loop.active and loop.approval_stalled and svc._loops.get(loop.id) is loop:
        svc._arm_timer(loop, delay=SUBJECT_READ_SECS)


async def _settle_terminal(
    svc: AutoNudgeService, loop: NudgeLoop, message: str, *, merged: bool
) -> bool:
    """Deactivate a held loop whose pull request ended. True once committed.

    Persist before publishing: the transition is staged on a copy, written under
    ``_lock``, and applied to the live loop only once the awaited write returns, so
    no reader sees a settlement the store has not accepted, and a refused write
    leaves the loop held exactly as the store says. A cancellation while waiting
    for either lock has staged nothing. A CHANNEL loop settles here too, unlike on
    the gate, which defers it to a delivered turn: a held loop delivers no turn, so
    that debt could never be paid.
    """
    settle_lock = await svc._acquire_mutation_lock(loop.id)
    if settle_lock is None:
        return False
    try:
        async with svc._lock:
            # Re-checked under the locks: a release or a retarget may have landed
            # during the read, and a reading about the old state must not settle
            # the new one.
            if (
                svc._loops.get(loop.id) is not loop
                or not loop.active
                or not loop.approval_stalled
                or _pr_message(loop) != message
            ):
                return False
            outcome = MonitorOutcome.SUCCESS if merged else MonitorOutcome.BLOCKED
            staged = deepcopy(loop)
            if staged.monitor is not None:
                staged.monitor.outcome = outcome
                staged.monitor.stopped_reason = MONITOR_TERMINAL_REASON
                staged.monitor.stopped_at = time.time()
            else:
                staged.subject_outcome = outcome.value
            staged.stopped_reason = MONITOR_TERMINAL_REASON
            staged.approval_stalled = False
            staged.approval_stalled_at = 0.0
            staged.active = False
            try:
                await svc._write_monitor_snapshot_locked(
                    svc._monitor_snapshot_with_replacement(loop, staged)
                )
            except asyncio.CancelledError:
                # The writer drains its write before propagating, so by here the
                # settlement is on disk: publish it, tell the user, keep the cancel.
                _apply_settlement(loop, staged)
                svc._emit("expired", loop)
                raise
            except Exception:
                logger.exception(
                    "AutoNudge: could not persist the held loop %s's terminal settlement -- "
                    "it stays held",
                    loop.id,
                )
                return False
            _apply_settlement(loop, staged)
    finally:
        _release_mutation_lock(settle_lock)
    logger.info(
        "AutoNudge: held loop %s's pull request %s -- loop settled",
        loop.id,
        "merged" if merged else "closed unmerged",
    )
    svc._emit("expired", loop)
    return True


def _apply_settlement(loop: NudgeLoop, staged: NudgeLoop) -> None:
    """Copy a committed settlement's fields onto the live loop, keeping its monitor object."""
    loop.active = staged.active
    loop.stopped_reason = staged.stopped_reason
    loop.subject_outcome = staged.subject_outcome
    loop.approval_stalled = staged.approval_stalled
    loop.approval_stalled_at = staged.approval_stalled_at
    if loop.monitor is not None and staged.monitor is not None:
        loop.monitor.outcome = staged.monitor.outcome
        loop.monitor.stopped_reason = staged.monitor.stopped_reason
        loop.monitor.stopped_at = staged.monitor.stopped_at
