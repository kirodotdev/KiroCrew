"""The probe gate: whether one tick of a gated loop is worth a model turn.

:func:`_monitor_tick_is_quiet` observes the subject cheaply and answers True only when
nothing happened; every uncertain path spends the turn. It owns the owed-turn debts
(``floor_fire_pending``, ``poll_in_flight``, ``terminal_pending``), the quiet-streak
floor, the typed terminal settlement that retires a finished watch, and the publication
of each reading before the judge is asked about it.

Its functions are :class:`~kiro_crew.autonudge.AutoNudgeService` methods: each is bound
on the class by name and runs against the service's state through ``self``, and a call
to any other service method goes through ``self`` too, so a patch on the instance
reaches it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from kiro_crew import irq, probes
from kiro_crew.autonudge_service.maintenance import _release_mutation_lock
from kiro_crew.autonudge_service.model import MONITOR_TERMINAL_REASON, NudgeLoop, is_channel_key
from kiro_crew.autonudge_service.subject import (
    _JUDGE_PR_SEEN_REMARKS,
    _pr_facts_digest,
    _pr_observation_of,
    loop_subject,
)
from kiro_crew.monitoring.models import MonitorOutcome, MonitorState

if TYPE_CHECKING:
    from kiro_crew.autonudge import AutoNudgeService

# The service's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.autonudge")


#: Ticks the gate is bypassed for after each wake, so a woken agent gets a
#: second turn to finish. One, because the cost is paid per wake and a second
#: free turn buys progress the probe cannot observe; raising it multiplies the
#: cost of every wake, and lowering it to zero reintroduces the stall.
_WAKE_FOLLOWUP_TICKS = 1


#: Consecutive quiet observations after which a gated loop is delivered anyway.
#:
#: This is what makes "gating slows an act-on-quiet loop" true instead of
#: "gating silences it". Ten keeps the great majority of the saving (nine ticks
#: in ten cost nothing) while bounding how long any loop can go undelivered to
#: ten intervals -- under an hour on the 300s interval agents actually use.
#: Lower wastes the saving on loops that had nothing to do; higher starts to
#: look like silence to whoever armed the watch.
_MAX_QUIET_STREAK = 10


#: Consecutive judge QUIET verdicts before a tick fires anyway, and the default for
#: ``decisions.nudge_wake.quiet_streak_floor``. Bound to the probe's own floor rather
#: than spelled as a second number: both answer the same question -- how long may a
#: watch go undelivered -- so two literals would be two things to keep in step, and
#: the one that drifted would be the one nobody reads.
_JUDGE_QUIET_STREAK_FLOOR_DEFAULT = _MAX_QUIET_STREAK


async def _commit_judge_pr_seen(
    self: AutoNudgeService,
    loop: NudgeLoop,
    staged_pr_seen: dict,
    *,
    staged_for_spec: Any,
    staged_for_message: str,
) -> bool | None:
    """Make *staged_pr_seen* this loop's pull-request baseline, disk first.

    ``True`` once the baseline is durable and published, ``False`` when the record
    refused it, ``None`` when the loop was re-aimed and there is nothing to commit.

    PERSISTED FIRST, the same way the reading itself is published. The stored
    record is this baseline's authority -- the loader restores it from the
    snapshot -- and the awaited judge write that reached the verdict has already
    landed one holding the OLD baseline. So the new one is written on a staged copy
    and copied over the live loop only once that write has landed. Memory never
    runs ahead of disk: a refused write publishes nothing, so the baseline stays
    exactly what the record holds, and the next tick re-reads these remarks --
    which costs a turn and withholds nothing.

    AWAITED under ``_lock`` for both halves of the same reason. The write has to
    land before the tick's own scheduled write, or a process that stops in between
    leaves disk claiming the old baseline while this tick has already screened
    against the new one, and every remark it passed on reads as new after the
    restart. And the snapshot is taken under the lock so no update can land between
    the copy and the write, which would put a stale row on disk. Holding the lock
    across the write is also what keeps every other writer's snapshot honest: none
    can serialize the loop while its baseline is half-committed.

    ONLY THE BASELINE is published, never the whole staged copy. The lock does not
    cover every writer: ``notify_cycle_landed`` clears the start-failure streak on
    the live loop synchronously and lock-free, from the turn-completion path, and
    can land inside the awaited write. Copying every field of the snapshot back over
    the live loop -- what ``_apply_staged_monitor`` does for a full monitor
    transition -- would silently put that streak back and let the loop back off or
    stand down on a failure a completed turn has just disproved. Writing one field
    cannot revert another.

    A write that lands and is then cancelled still publishes: the snapshot writer
    propagates the cancellation only after the executor result is in, so disk holds
    the new baseline, and a memory left on the old one would hand the next tick a
    record it does not match.

    The re-aim check runs under the lock too. An ``update`` that retargets the
    message or replaces the criteria clears the baseline deliberately, because a
    digest earned under the old question would screen the new one quiet; the caller
    checks the same thing before the await, but an update can land while this call
    waits for the lock.
    """
    from kiro_crew import autonudge_judge as judge

    async with self._lock:
        if (
            self._loops.get(loop.id) is not loop
            or judge.spec_of(loop) != staged_for_spec
            or loop.message != staged_for_message
        ):
            return None
        staged = deepcopy(loop)
        staged.judge_pr_seen = staged_pr_seen
        payload = self._monitor_snapshot_with_replacement(loop, staged)
        try:
            await self._write_monitor_snapshot_locked(payload)
        except asyncio.CancelledError:
            loop.judge_pr_seen = staged_pr_seen
            raise
        except Exception:
            logger.warning(
                "AutoNudge: could not persist the pull-request baseline for loop %s",
                loop.id,
                exc_info=True,
            )
            return False
        loop.judge_pr_seen = staged_pr_seen
    return True


async def _publish_pr_observation(
    self: AutoNudgeService, loop: NudgeLoop, monitor: MonitorState, observation: Any
) -> tuple[bool, dict] | None:
    """Make this tick's reading the loop's current pull-request observation.

    PERSISTED FIRST. The durable record owns this state -- it is what a restart
    restores and what the dashboard and Slack projections read -- so the write is
    awaited on a STAGED copy and published to live readers only once it has landed.
    A fire-and-forget write beside the in-memory assignment would let a kill inside
    that window leave the record on the previous tick's facts while readers had
    already been shown the new ones.

    Returns ``(unchanged, staged_baseline)``. ``unchanged`` says whether this
    reading matches the last one a VERDICT was reached on -- ``False`` whenever the
    comparison could not be made, so an unanswerable one resolves toward spending
    the turn. ``staged_baseline`` is what ``judge_pr_seen`` should become, and the
    caller stores it only once a verdict exists. ``None`` says the reading could
    not be kept -- nothing was published and the caller must FIRE without asking
    the judge, because screening against a reading the record refused would report
    a quiet on state that does not exist.

    Two carriers, because the halves have two lifetimes. The typed facts and the
    remark metadata go on the monitor record, which is durable and is what the
    judge collector and the goal popover read. ``monitor_inspect`` does not: a
    gated prompt loop is not a structured monitor, so that endpoint answers it
    with a presence and cadence reading and no observation at all. The remark
    BODIES go in a process-local stash, are picked up by the collector on this
    same tick, and are written nowhere.

    A reading the record refuses is not worth failing the tick over: the collector
    then sees no reading, counts the target as unread, and the tick fires -- the
    direction every uncertain path here resolves toward.
    """
    from kiro_crew import autonudge_judge as judge

    try:
        facts = observation.as_facts()
    except Exception:
        logger.debug("AutoNudge: could not render a pull-request reading", exc_info=True)
        # No reading to keep, so none to screen against: the tick fires.
        return None
    # Both comparisons are against the last reading this loop reached a VERDICT on,
    # never against the last one it merely published. An empty digest means the
    # comparison could not be made, and an unanswerable comparison is not a match.
    # A match also requires the reading to be WHOLE, decided by the one function
    # that already owns that question: two byte-identical PARTIAL readings agree
    # only about the half that was read, so treating them as an unchanged subject
    # would withhold a red lane sitting in the half that was not.
    judged = loop.judge_pr_seen if isinstance(loop.judge_pr_seen, dict) else {}
    this_digest = _pr_facts_digest(facts)
    prior_digest = str(judged.get("digest") or "")
    unchanged = (
        bool(this_digest) and this_digest == prior_digest and not judge.pr_target_is_unread(facts)
    )
    judged_remarks = judged.get("remarks")
    seen = {str(ident) for ident in (judged_remarks if isinstance(judged_remarks, list) else [])}
    # Recorded as DATA on the reading, never acted on here: WHICH remarks are new
    # is a fact the judge weighs against the owner's criterion, and a core that
    # decided on it would be the comparator this design removes.
    ids: list[str] = []
    for row in facts.get("remarks") or []:
        if isinstance(row, dict):
            ident = str(row.get("id", ""))
            ids.append(ident)
            row["first_seen_this_tick"] = ident not in seen
    observed_at = float(getattr(observation, "observed_at", 0.0) or time.time())
    binding = (monitor.kind, monitor.target)
    try:
        async with self._lock:
            # Re-read under the lock. Every check above was made while an update
            # could still land, and this one decides whether the reading belongs to
            # this loop at all: a retarget makes it a reading of a subject the loop
            # does not watch, and publishing it would screen the new subject against
            # the old one's board.
            if (
                loop.monitor is not monitor
                or loop.id not in self._loops
                or (monitor.kind, monitor.target) != binding
            ):
                logger.info(
                    "AutoNudge: loop %s changed before its reading was kept -- firing",
                    loop.id,
                )
                return None
            # STAGED UNDER THE LOCK, and that placement is the whole correctness
            # argument. ``_apply_staged_monitor`` copies EVERY field of the staged
            # loop back over the live one, so a copy taken before the lock would
            # revert any update accepted in between -- in memory and on disk, since
            # the snapshot written would be the stale one too, with nothing to
            # recover from. Copying here means no update can land between the copy
            # and the write.
            staged = deepcopy(loop)
            staged_state = staged.monitor
            if staged_state is None:
                return None
            staged_state.last_observation = facts
            staged_state.last_observed_at = observed_at
            await self._persist_staged_monitor_locked(loop, staged)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Could not keep the reading, so do not act on it: nothing is published and
        # the tick fires, the same answer this method's siblings give when a durable
        # write they depend on will not land.
        logger.warning(
            "AutoNudge: could not persist the pull-request reading for loop %s -- "
            "firing instead of screening",
            loop.id,
            exc_info=True,
        )
        return None
    try:
        judge.publish_pr_bodies(loop.id, observation.bodies())
    except Exception:
        logger.debug("AutoNudge: could not stash remark bodies", exc_info=True)
    # STAGED, not stored: the caller commits it once a verdict exists. Returned
    # rather than assigned for the same reason the evidence collector returns its
    # cursors -- every path that leaves before the commit has to leave the baseline
    # exactly as it was.
    return unchanged, {"digest": this_digest, "remarks": ids[:_JUDGE_PR_SEEN_REMARKS]}


async def _monitor_tick_is_quiet(self: AutoNudgeService, loop: NudgeLoop) -> bool:
    """Observe this loop's subject cheaply; say whether to skip the turn.

    Returns True only when the tick is DEFINITELY not worth a model turn.
    Every other case -- no monitor, no probe for that subject kind, an
    un-inferable target, a probe defect, a kernel that reached no verdict --
    returns False so the caller fires exactly as it does today.

    The asymmetry is the whole safety argument. A wrongly-QUIET tick is
    silence: the loop stops waking and the work it was watching stalls with
    nothing on screen to say why. A wrongly-spent tick costs one turn, which
    is what every tick costs today. So every uncertain path resolves toward
    spending, and only a positive "nothing happened" from the kernel skips.

    The kernel call is offloaded to a thread because observing runs ``gh`` as a
    subprocess -- several of them, since one reading fetches the pull request, its
    check runs and its commit statuses, and paginates the last two. Each call is
    capped individually, but what the thread is held for is the whole reading's
    budget, ``gh_pr._TICK_BUDGET_SECS``. On the event loop that would freeze chat,
    the channel transports and the liveness probes for that long.
    """
    from kiro_crew import autonudge_judge as judge

    monitor = loop.monitor
    # Whether the typed probe below will actually observe this tick -- and so
    # whether a TERMINAL can still be detected for it. Only a loop whose probe
    # runs defers its judge to the quiet return at the end of this method. The
    # probe is the only thing that notices a merged or closed subject and
    # DEACTIVATES the watch; a judge answering ahead of it returns before that
    # runs and never emits a terminal itself, so a judged pull-request loop would
    # keep watching a finished subject until its cycle cap ran out. A loop
    # watching sibling sessions carries no monitor at all, so for that one the
    # judge is the only screen there is and it has to run here or nowhere.
    probe_will_run = monitor is not None and monitor.outcome is None and loop.gate
    if not probe_will_run:
        judged = await self._judge_tick_is_quiet(loop)
        if judged is not None:
            return judged
    if monitor is None or monitor.outcome is not None:
        return False
    if not loop.gate:
        # An opt-out that only SOME paths honour is worse than no opt-out. This
        # check exists because the two can now disagree: a record stored with a
        # monitor but no ``gate`` key -- one armed while the default was True,
        # or upgraded from an earlier build of this branch -- decodes to
        # ``gate=False`` with its monitor intact. Reading only the monitor would
        # poll such a loop anyway and let a terminal verdict DEACTIVATE it,
        # which is exactly the harm the opt-out exists to prevent. The stored
        # decision wins over the presence of the object.
        return False
    if monitor.floor_fire_pending:
        # A floor delivery was decided on an earlier tick and has not been
        # confirmed. Deliver it WITHOUT observing: the streak that earned it is
        # already reset on disk, so a fresh probe reads an unchanged subject,
        # answers quiet, and would suppress the very turn that is owed -- the
        # same reasoning the judge's own owed wake is delivered on.
        #
        # AHEAD of the follow-up allowance below, because a refused floor fire
        # leaves TWO credits standing for ONE owed turn: the allowance, granted so
        # the next tick retries the delivery, and this debt, recording that the
        # delivery is still owed. Behind the allowance a restart spends the bypass
        # with no claim to charge, then spends the debt on the tick after -- two
        # turns for one owed delivery. So the debt is served first and the
        # allowance it duplicates is consumed with it: the retry that allowance
        # exists for IS this fire. Decremented rather than zeroed, so a credit
        # this debt did not create is left to be spent on its own tick.
        #
        # The in-process claim is re-taken because a restart starts with an empty
        # claim set while this flag survives, and the claim is what makes the fire
        # cycle charge the delivery and discharge the debt. Re-taking is
        # idempotent: it is a set, and the fire cycle is the only release.
        if monitor.followup_ticks > 0:
            monitor.followup_ticks -= 1
        self._pending_floor_tick.add(loop.id)
        self._persist_soon()
        logger.info(
            "AutoNudge: loop %s owes a floor delivery -- firing it rather than "
            "re-observing a subject its own reset reads as calm",
            loop.id,
        )
        return False
    # A wake buys the agent one more turn, unconditionally and BEFORE any
    # observation. The probe watches the subject, not the agent: a turn that
    # was woken and has not pushed yet leaves the subject unchanged, so
    # observing here would read "nothing happened" and starve work already in
    # progress. Bounded on purpose -- one tick per wake, spent whether or not
    # it was needed -- because the alternative designs both fail worse: an
    # unbounded allowance driven by a completion signal disables gating
    # entirely on any surface where that signal never arrives, and no
    # allowance at all lets a watch go silent while holding half-finished
    # work. Costing one turn per wake is the cheap failure.
    pushed = loop.id in (getattr(self, "_pushed_running", None) or ())
    if monitor.followup_ticks > 0 and not monitor.terminal_pending and not pushed:
        # NOT while a terminal turn is owed. The allowance exists to protect work
        # already in progress, which is why it skips observation -- but a subject
        # with terminal debt is FINISHED, so there is no in-progress work to
        # protect, and the retry's correctness depends on it still being finished.
        # Skipping the poll here is what let a REOPENED pull request keep its stale
        # debt: the clearing added for that case lives after the poll, so the
        # bypass jumped straight over it and the retried delivery settled a
        # terminal state that had ended. Re-observing costs one probe call on
        # a path that is already firing a turn.
        #
        # NOT on a tick a worker's push brought forward either. The allowance is the
        # loop's own second turn, owed on its cadence; a push landing right after a wake
        # would otherwise arrive on it and buy a turn without the ledger being read --
        # a worker's close after its ``done`` report would buy exactly that. Such a tick is
        # observed like any other and wakes only for an observation the delivered turn
        # did not already carry; the allowance stays for the next scheduled tick.
        monitor.followup_ticks -= 1
        self._persist_soon()
        logger.debug("AutoNudge: loop %s spending a post-wake follow-up tick", loop.id)
        return False
    probe = probes.build(
        monitor.kind,
        worker_running=self._worker_running,
        worker_closed=self._worker_closed,
    )
    if probe is None:
        return False
    # Derive the probe's config from the LOOP'S OWN STRINGS -- its judge brief's
    # target list, else its instruction -- then check the subject it yields
    # against the stored monitor.
    #
    # Not from ``monitor.target``: that is the CANONICAL subject
    # ("owner/name#123"), a shorthand, and a shorthand deliberately carries no
    # host -- so re-inferring from it would discard the github.com pin that a
    # URL-armed watch is entitled to, and on a machine configured for an
    # enterprise server the probe would resolve the slug there. A
    # same-numbered enterprise pull request being merged would then falsely
    # terminate a live public watch.
    #
    # Those strings are the only place the original spelling survives, and
    # storing the host a second time would put one fact in two places that can
    # disagree. So infer from them and REQUIRE the result to name the
    # subject the monitor is bound to; a mismatch means the two have drifted
    # apart, which is not something to resolve by guessing -- fire instead, the
    # same direction every other uncertain path takes. Resolving the subject
    # HERE by a different rule than the arm used is exactly how that mismatch
    # gets manufactured, which is why both go through ``infer_subject``.
    target = loop_subject(loop)
    if target is None or (target.kind, target.subject) != (monitor.kind, monitor.target):
        if target is not None:
            logger.info(
                "AutoNudge: loop %s names %s but its monitor is bound to "
                "%s -- firing instead of observing",
                loop.id,
                target.subject,
                monitor.target,
            )
        return False
    # Captured BEFORE the poll, which awaits: this is what the verdict is
    # about, and it is checked again afterwards. The derived CONFIG is part of
    # it, not just the subject: the stored target is a shorthand, so an
    # instruction edited from an enterprise shorthand to the same public URL
    # leaves kind and target identical while changing which SERVER is being
    # observed. Comparing only the subject would let a verdict about one host
    # settle a watch that now means the other.
    binding = (monitor.kind, monitor.target, target.message)
    # The dedupe memory is keyed on this identity, so it must move when the
    # subject's host does -- otherwise a retargeted watch inherits
    # observations made against a different server and suppresses the first
    # real signal from the new one. Only this driver's identity changes; the
    # cron path keeps the one its persisted state was written under.
    identity = f"{loop.id}:{target.host_key}"
    if monitor.poll_in_flight:
        # A previous poll was interrupted after the kernel may already have
        # committed "reported" for what it saw. That observation reached
        # nobody, and re-observing now would read the same state as unchanged,
        # so this tick must not trust a quiet verdict -- it fires. The flag is
        # cleared first so the doubt is consumed once rather than latching.
        monitor.poll_in_flight = False
        monitor.gate_fallbacks += 1
        self._persist_soon()
        logger.info(
            "AutoNudge: loop %s had a poll interrupted -- firing rather than "
            "trusting a fresh observation of the same state",
            loop.id,
        )
        return False
    # Durable BEFORE the probe runs, because the case it protects against is
    # this coroutine never resuming. ``_persist_soon`` would not do: a
    # scheduled write does not survive the shutdown that causes the problem.
    monitor.poll_in_flight = True
    try:
        # ``_write_monitor_snapshot_locked`` under ``_lock``, NOT
        # ``_persist_locked``: that one releases ``_lock`` if the awaiting task
        # is cancelled while the executor write is still in flight, so a
        # pause or retarget landing here could have this stale snapshot
        # overwrite the newer state it just wrote. The settlements already use
        # the non-releasing writer; these marker writes were left behind.
        async with self._lock:
            await self._write_monitor_snapshot_locked()
    except Exception:
        # Could not record the doubt, so do not incur it: fire this tick
        # rather than run a probe whose interruption would be invisible.
        monitor.poll_in_flight = False
        logger.warning(
            "AutoNudge: could not record the in-flight marker for loop %s -- "
            "firing instead of polling",
            loop.id,
            exc_info=True,
        )
        return False
    try:
        verdict = await asyncio.get_running_loop().run_in_executor(
            None, lambda: irq.poll(identity, target.message, probe)
        )
    except Exception:
        monitor.poll_in_flight = False
        logger.warning(
            "AutoNudge: probe gate raised for loop %s — firing as usual",
            loop.id,
            exc_info=True,
        )
        return False
    if verdict.outcome is not irq.Outcome.WAKE:
        # The doubt is discharged when the thing it protects has happened -- and
        # for a WAKE that is DELIVERY, not the poll returning. The kernel has
        # already committed "reported" for what it saw, so if this process dies
        # between here and the turn landing, a fresh observation reads the same
        # state as unchanged and the signal is gone until the streak floor. The
        # in-process refusal is covered by ``followup_ticks``; a DEATH is covered
        # only by this marker outliving the fire, so a wake keeps it set and the
        # fire cycle clears it where the wake claim is consumed.
        #
        # The asymmetry is deliberate: the SET must be durable because it guards
        # against a death, while a CLEAR may ride the debounced write -- losing a
        # clear costs one unnecessary fire, the direction this design resolves
        # toward anyway.
        monitor.poll_in_flight = False

    # The poll above is a real await -- it runs ``gh`` in a thread for as long as
    # the reading's whole budget, ``gh_pr._TICK_BUDGET_SECS``, not the per-call cap
    # -- so the loop can be RETARGETED while it is in flight:
    # ``update(message=...)`` or a replaced judge brief rebinds the monitor to a
    # different pull request, or clears it. Acting on this verdict now would apply
    # an observation of the OLD subject to the new one, and the terminal branch
    # would deactivate a watch that had just been pointed at a live pull request.
    # Compare the binding, not the object: a retarget mutates the same MonitorState.
    fresh = loop_subject(loop)
    current_binding = (monitor.kind, monitor.target, fresh.message) if fresh is not None else None
    if loop.monitor is not monitor or current_binding != binding:
        # The verdict is thrown away, so no wake is owed and the doubt has
        # nothing left to protect. Clear it, or the next tick would fire a
        # second time on a discharged suspicion and count a phantom fallback.
        monitor.poll_in_flight = False
        logger.info(
            "AutoNudge: loop %s was retargeted while its probe was in flight -- "
            "discarding the stale verdict and firing as usual",
            loop.id,
        )
        return False

    # THE one deterministic mapping, and it lives here rather than in the reader
    # or the judge. The reader fetches and judges nothing; the judge reads prose
    # a third party wrote, so it must never be able to end a watch. Ending one is
    # the single decision an owner cannot recover by waiting, so it is taken from
    # a typed fact, by the layer that can act on it.
    observation = _pr_observation_of(probe)
    # Two kinds reach terminal by two channels and BOTH end the watch here. The
    # gh-pr fetcher exposes its finish as a ``probe.observation`` this reads via
    # ``_pr_observation_of``; the work-ledger probe exposes none and instead lets
    # the kernel attribute a ``Severity.TERMINAL`` observation, which surfaces as
    # ``verdict.outcome is TERMINAL``. Gating on the observation alone left a
    # settled work-ledger loop with ``terminal`` false forever, so it never
    # deactivated and polled its own finished ledger on every interval. The
    # disjunct honours the kernel's typed terminal for the observation-less kind
    # without disturbing the gh-pr path, whose observation still decides it.
    terminal = (
        observation is not None and observation.is_terminal
    ) or verdict.outcome is irq.Outcome.TERMINAL
    #: Whether the reading published this tick says the same thing as the previous
    #: one. Only the publish below can answer it, and only while the previous
    #: reading is still stored, so it is carried from there rather than recomputed.
    reading_unchanged = False
    #: What ``judge_pr_seen`` becomes, but only once a verdict exists.
    staged_pr_seen: dict = {}
    #: The question that baseline answers, captured BEFORE the reading is published
    #: and re-checked before it is committed. An ``update`` that retargets the
    #: message or replaces the criteria CLEARS the baseline, deliberately, because a
    #: digest earned under the old question would screen the new one quiet. The
    #: commit below runs after an await the update lands inside, so committing
    #: unconditionally would put the cleared baseline straight back and suppress the
    #: watch the owner just re-aimed until the streak floor. Same comparison the
    #: judge call makes twice for the same reason, on the verdict rather than on the
    #: baseline.
    staged_for_spec = judge.spec_of(loop)
    staged_for_message = loop.message
    #: The record refused this tick's reading. Carried rather than returned on the
    #: spot, because a terminal debt a live reading DISPROVES has to be cleared on
    #: the way past -- returning here would leave that debt standing and let the
    #: next delivered turn settle a watch whose subject is alive.
    publish_failed = False
    if observation is not None and observation.reached and not terminal:
        # Published BEFORE the judge is asked, because this is the channel the
        # judge reads its pull-request evidence through: the record carries who
        # said what and when, and the bodies ride alongside in memory for this
        # tick only. Written even when the reading is partial -- the collector
        # reads the status and counts a partial reading as a target nobody read
        # whole, which fires -- so a short board is visible rather than absent.
        published = await self._publish_pr_observation(loop, monitor, observation)
        if published is None:
            publish_failed = True
        else:
            reading_unchanged, staged_pr_seen = published

    if terminal:
        # The subject is finished (a merged or closed pull request). Stop the
        # loop rather than firing: there is nothing left to service, and one
        # more turn would only rediscover that. ``expired`` is the existing
        # channel for "this loop stopped rather than the agent finishing",
        # and the emitted payload carries ``stopped_reason``, which is what
        # distinguishes a merged subject from a spent bound.
        #
        # Deliberately NOT counted as a wake: no turn is delivered here. A
        # terminal observation that incremented ``wakes`` would report a turn
        # that never ran, in the very counters this change exists to make
        # trustworthy.
        # Record the finish ON THE MONITOR, not only on the loop. Without
        # this the record reads as merely paused, and the generic resume path
        # -- the goal popover's Save, which is allowed to revive a current
        # unsettled monitor -- would re-arm the watch onto a subject that is
        # already merged. It would then observe TERMINAL, deactivate, and be
        # revivable again: a loop that cannot be told apart from a working
        # one. Reaching the end of the thing you were watching is a SUCCESS,
        # so the outcome says so rather than borrowing a bound's vocabulary.
        # ONE transition, committed once. Four rounds of review landed on this
        # hunk and each earlier shape had a gap: announcing before the write
        # promised a finish the record did not have; announcing after it was
        # swallowed by ``update``'s cancel reaching this very task; and doing
        # both left TWO await points, so a write failure killed the timer with
        # the loop still active, and a retarget landing between them let an old
        # verdict deactivate a subject that had never been observed.
        #
        # So there is no ``update`` call here at all. The marks go on in memory
        # with no await between them, one durable write commits them, and the
        # deactivation is simply ``active = False`` -- which the re-arm guard
        # below already honours, making the timer cancel unnecessary rather
        # than merely deferred.
        # Reaching the end of the thing you were watching is not automatically
        # a success. A MERGED subject is; one CLOSED WITHOUT MERGING ended on a
        # question -- reopen or abandon -- and recording SUCCESS there tells the
        # user "no action needed" about the one case that needs them most. The
        # reading distinguishes the two, and it is read as a typed fact rather
        # than out of delivered prose, which would break the first time that
        # wording is edited.
        merged = observation is not None and observation.merged
        # The probe distinguishes the two, so this reads its KEYS rather than its
        # prose, which would break the first time that wording is edited. No
        # key at all (an unusable target) is also not a success. The gh-pr probe
        # is a FETCHER: its ``observe`` returns ``observations=[]``, so the kernel
        # never attributes a TERMINAL key on that path and ``verdict.keys`` is
        # empty for a pull request. ``merged`` is that kind's own success signal
        # and must stand alongside the keys, or a merged pull request records as
        # blocked. The disjunct cannot fire spuriously for gh-pr (its key set is
        # always empty) and cannot fire for a rejected work ledger (``merged`` is
        # only ever true for a pull request).
        succeeded = merged or probes.terminal_succeeded(verdict.keys)
        # EVERY field this transition writes has to be in here. The loop's own
        # ``stopped_reason`` is written alongside the monitor's, and leaving it
        # out of the rollback left a live loop tagged as terminated -- which the
        # fallback delivery would then persist.
        restore = (
            monitor.outcome,
            monitor.stopped_reason,
            monitor.stopped_at,
            loop.active,
            loop.stopped_reason,
        )
        if merged:
            subject_state = "merged"
        elif observation:
            subject_state = observation.state.lower()
        else:
            subject_state = "unknown"
        logger.info(
            "AutoNudge: loop %s subject reached a terminal state (%s)",
            loop.id,
            subject_state,
        )
        # SERIALIZED against ``update``. This path has no second await of its
        # own; this closes the other side of the same race, which is
        # ``update``'s. That method takes the MAINTENANCE lock (not ``_lock``)
        # and awaits inside it, so a retarget could pass its precheck, yield,
        # let this branch settle the OLD subject with ``active = False``, and
        # then bind the NEW subject onto that inactive loop -- a fresh watch
        # that never ticks. Holding the same lock across revalidate, mutate and
        # persist is what makes the two mutually exclusive; ``_lock`` alone
        # would not, because that is not the lock ``update`` contends for.
        #
        # Lock ORDER matches ``update``'s (maintenance, then ``_lock`` for the
        # write) so the two cannot deadlock against each other.
        # A CHANNEL loop is told by a delivered TURN, not by the dashboard
        # notification -- so for one the settlement must not be committed yet.
        # Committing it means an inactive loop, and if that final fire is
        # refused (a busy thread, the ordinary case) nothing re-arms and the
        # news is lost: exactly the silent ending the previous round added this
        # delivery to prevent. So mark what is OWED, durably, and settle only
        # once the turn has landed. No outcome is recorded in the meantime, so a
        # restart in this window finds a plain live loop rather than one tagged
        # as finished and refused revival.
        if is_channel_key(loop.slot_key):
            if not monitor.terminal_pending:
                monitor.terminal_pending = "success" if succeeded else "blocked"
                try:
                    # Same writer as the settlements, for the same reason: a
                    # cancelled ``_persist_locked`` releases ``_lock`` mid-write.
                    async with self._lock:
                        await self._write_monitor_snapshot_locked()
                except Exception:
                    monitor.terminal_pending = ""
                    logger.exception(
                        "AutoNudge: could not record the owed terminal turn for %s",
                        loop.id,
                    )
            return False
        settle_lock = await self._acquire_mutation_lock(loop.id)
        if settle_lock is None:
            # Maintenance has claimed this loop. Not ours to settle: fire, and
            # the next tick will observe the same terminal state.
            return False
        try:
            # Re-read under the lock. The checks before it were made while a
            # retarget could still land.
            fresh_under_lock = loop_subject(loop)
            if (
                loop.monitor is not monitor
                or loop.id not in self._loops
                or (
                    (monitor.kind, monitor.target, fresh_under_lock.message)
                    if fresh_under_lock is not None
                    else None
                )
                != binding
            ):
                logger.info(
                    "AutoNudge: loop %s changed before its terminal settlement -- firing",
                    loop.id,
                )
                return False
            monitor.outcome = MonitorOutcome.SUCCESS if succeeded else MonitorOutcome.BLOCKED
            monitor.stopped_reason = MONITOR_TERMINAL_REASON
            monitor.stopped_at = time.time()
            loop.stopped_reason = MONITOR_TERMINAL_REASON
            loop.active = False
            try:
                async with self._lock:
                    await self._write_monitor_snapshot_locked()
            except asyncio.CancelledError:
                # The writer drains its executor write before propagating
                # cancellation, so by HERE the settlement is already committed
                # -- and on restart the loop reads as settled, so nothing would
                # ever notify. Tell the user now, then preserve the
                # cancellation. Same shape as ``_apply_staged_monitor``'s.
                self._emit("expired", loop)
                raise
            except Exception:
                # A failed write must not take the watch down with it. Undo the
                # marks and fire: the loop stays watchable, the user gets a
                # turn, and the next tick observes the same terminal state and
                # tries again. Letting this raise would kill the timer task
                # with the loop still active in memory and on disk -- a dead
                # watch that looks exactly like a calm one, which is the
                # failure mode this whole change exists to remove.
                (
                    monitor.outcome,
                    monitor.stopped_reason,
                    monitor.stopped_at,
                    loop.active,
                    loop.stopped_reason,
                ) = restore
                logger.exception(
                    "AutoNudge: could not persist the terminal transition for %s -- "
                    "keeping the watch alive and firing instead",
                    loop.id,
                )
                return False
        finally:
            _release_mutation_lock(settle_lock)
        self._emit("expired", loop)
        return True

    if (
        monitor.terminal_pending
        and observation is not None
        and observation.reached
        and verdict.outcome is not irq.Outcome.FALLBACK
    ):
        # The subject came BACK. A channel loop defers its settlement as a durable
        # debt because only a delivered turn can carry the news, and that debt
        # outlives the observation that created it -- so a closed PR that is
        # REOPENED while the final fire is still owed would have its next
        # delivered turn claim the stale debt and deactivate a watch whose
        # subject is live again. Nothing else cleared it: the marker was written
        # once and read at settlement, which is the same absence-shaped defect
        # this review has now found twelve times.
        #
        # Only a TRUSTWORTHY reading clears it, and that needs BOTH signals to
        # agree: the reading reached the subject, and the kernel reached a verdict
        # about it. A reading that did not reach the subject proves nothing, and a
        # FALLBACK says the kernel could not conclude -- letting either erase real
        # debt would lose the terminal news for good, the opposite of the rule that
        # failure resolves toward spending.
        monitor.terminal_pending = ""
        try:
            async with self._lock:
                await self._write_monitor_snapshot_locked()
        except Exception:
            # NOT rolled back -- and there is deliberately no saved copy to roll
            # back TO. Restoring the debt here to keep memory and disk in
            # agreement is the right instinct almost everywhere and the
            # wrong one here: a trustworthy live observation has just DISPROVED
            # the debt, so restoring it lets the next delivered turn settle a
            # terminal state that has ended and silently stop a watch whose
            # subject is alive. The divergence is safe in exactly one direction --
            # memory saying "no debt" keeps the watch running, and if the process
            # restarts before the write lands, the disk's stale debt comes back and
            # the tick RE-OBSERVES it (an outstanding debt never spends the
            # observation-free follow-up tick), which clears it again. So the
            # failure path converges instead of stopping work.
            logger.exception(
                "AutoNudge: could not persist the cleared terminal debt for %s -- "
                "keeping it cleared in memory so a live subject is not settled",
                loop.id,
            )

    if publish_failed:
        # Nothing durable to screen against, so nothing to screen: fire, the same
        # answer the in-flight marker gives when its own write will not land.
        # Placed after the terminal-debt block so that block still ran, and
        # before the judge so it is never asked about a reading the record
        # refused.
        return False
    if verdict.outcome is irq.Outcome.QUIET:
        # The ONE tick a judge screens on a monitor-backed loop. Every outcome that
        # MEANS something -- a terminal, a wake, a fallback, an interrupted poll, a
        # drifted target -- has returned above this line. What the judge adds here is
        # the evidence no typed reading produces: a review left in prose, a comment
        # thread, a watched session's transcript tail.
        #
        # ASKED BEFORE the quiet counters are charged. A judge that answers wake
        # or fallback makes this a DELIVERED tick, and charging first would book
        # it as a free one -- overstating the very saving this feature exists to
        # report -- while also walking a loop that keeps delivering toward a
        # forced floor tick it never earned.
        judged = await self._judge_tick_is_quiet(loop)
        # PAST the await, so a verdict exists -- including the ``None`` that says no
        # judge ran, which is itself this tick's answer. Cancellation lands inside
        # the await and nothing here runs, which is what leaves the baseline as it
        # was and the new remark still unseen.
        if staged_pr_seen:
            committed = await self._commit_judge_pr_seen(
                loop,
                staged_pr_seen,
                staged_for_spec=staged_for_spec,
                staged_for_message=staged_for_message,
            )
            if committed is None:
                logger.info(
                    "AutoNudge: loop %s was re-aimed while this tick judged -- "
                    "leaving its pull-request baseline cleared",
                    loop.id,
                )
            elif not committed:
                # Nothing was published, so the baseline is exactly what the stored
                # record has: the next tick re-reads these remarks, which costs a
                # turn and withholds nothing, the only safe direction here.
                logger.warning(
                    "AutoNudge: loop %s judged but its pull-request baseline did "
                    "not persist -- leaving the stored baseline in force",
                    loop.id,
                )
        if judged is False:
            # The judge overrides the probe's quiet on evidence the probe cannot
            # read, so this tick spends a turn and is accounted for as one.
            monitor.quiet_streak = 0
            monitor.last_observed_at = time.time()
            self._persist_soon()
            return False
        if judged is None and observation is not None and not reading_unchanged:
            # No judge ran, and this probe is a FETCHER: it published a reading and
            # raised no observations, so the kernel's quiet reports that nothing was
            # DECIDED rather than that nothing is there. Charging it as quiet would
            # withhold a failing check or a reviewer's request until the streak
            # floor, with nothing on screen -- and the judge-less paths are the
            # ordinary ones, not corners: an explicit ``judge: false``, a build with
            # no collector, and this point's egress scope, which is fail-closed and
            # ungranted on a stock install. So an unowned quiet DELIVERS.
            #
            # EXCEPT where the reading is byte-identical to the previous one AND
            # read the subject whole, which is the one judge-less quiet that is
            # earned rather than assumed: no criterion ABOUT the subject can have
            # become true while the subject did not change, so nothing is being
            # withheld. Wholeness is half of that claim -- two identical PARTIAL
            # readings agree only about the part that was read -- so the match is
            # taken against a whole reading or not at all. That keeps the cost
            # promise true for a watch on a machine with no judge -- an unchanged
            # board is still free -- and the streak floor still covers a criterion
            # about elapsed time, which is the one kind a digest cannot see.
            #
            # Keyed on the published reading rather than on the loop's shape,
            # because that is what says this probe judges nothing: a classifying
            # probe reaches the same line having already found nothing, and its
            # quiet is a finding that still stands on its own.
            monitor.quiet_streak = 0
            monitor.last_observed_at = time.time()
            self._persist_soon()
            return False
        monitor.quiet_ticks += 1
        monitor.quiet_streak += 1
        monitor.last_observed_at = time.time()
        if monitor.quiet_streak >= _MAX_QUIET_STREAK:
            # Floor reached: deliver anyway. The gate can only see the
            # SUBJECT, and a loop whose duty is to act while the subject is
            # quiet -- refresh a heartbeat, chase a silent reviewer, rebase
            # onto a moving base -- is invisible to it and would otherwise
            # never be delivered again. Inference cannot read that intent out
            # of the wording, so the honest answer is not to guess it but to
            # bound how long any loop can go undelivered.
            monitor.quiet_streak = 0
            # NOT charged here, for the same reason the WAKE branch is not: this
            # tick has decided to deliver but has not delivered, and the fire can
            # still be refused by a busy slot. Charging now would report a turn
            # that never ran, and since the honest free-tick figure is
            # ``quiet_ticks`` minus ``floor_ticks``, an over-counted floor
            # UNDERSTATES the saving -- the safe direction, but still a wrong
            # number in the one artifact this PR exists to produce.
            #
            # GPT's prescribed remedy was to revert this counter until it could be
            # charged after delivery. Declined: the counter is what separates a
            # quiet verdict from a free tick, so deleting it would remove the
            # subtraction that makes the metering honest. The substance -- charge
            # only on confirmed delivery -- is adopted instead, by the mechanism
            # the wake charge already uses.
            #
            # Two counters now carry the same owed-charge shape through the same
            # fire cycle. They belong in one structure; that is the collapse
            # recommended on the pull request rather than a third set later.
            self._pending_floor_tick.add(loop.id)
            # And durably, because that claim is in memory while the reset above
            # is not: a process that stops between this decision and the turn
            # landing keeps the reset, so the next tick reads the subject as calm
            # and the forced delivery is gone with nothing recording it was due.
            # Set BEFORE either write below, so the debt and the reset that hides
            # it ride ONE snapshot -- they land together or neither lands. A lost
            # write is the converging case: the increment above is memory-only, so
            # the store keeps the pre-increment streak and the next quiet tick
            # reaches the floor again.
            monitor.floor_fire_pending = True
            logger.info(
                "AutoNudge: loop %s hit the quiet-streak floor after %d quiet ticks",
                loop.id,
                _MAX_QUIET_STREAK,
            )
            # Persisted AFTER the reset, not before it: a restart reading a
            # streak that was never reset would deliver one extra turn and
            # under-count the floor.
            if judged is True:
                # This branch FIRES, so a judge row from this tick claiming to have
                # withheld the turn is wrong: left standing it takes a ``missed``
                # label from the next delivery for a tick that actually spent its
                # turn, which is a wrong row in the calibration log. Withdrawing
                # returns the row to the undecided state the fire path stamps, the
                # same correction the judge's own persist-failure path makes. The
                # two streaks are independent fields, which is what makes this
                # reachable: a gated loop whose judge arms mid-life climbs to the
                # probe's floor with the judge's own streak still below its floor.
                #
                # AWAITED rather than scheduled, and paired with the withdrawal
                # here for that reason: the withdrawal is a memory edit and this
                # tick is about to dispatch. A deferred write that has not landed
                # when the process stops leaves a restart reading the row as
                # suppressed, and the next delivery then records exactly the false
                # ``missed`` this exists to prevent -- the stale row is never
                # revisited, because the tick after a restart withdraws its own new
                # row instead. The result is deliberately unchecked: that check
                # belongs to a caller deciding whether to suppress, and this one is
                # already firing.
                self._withdraw_judge_suppression(loop)
                await self._persist_judge_state(loop)
            else:
                # Nothing was asked on this tick, so there is no claim of its own
                # to withdraw: the newest row belongs to an earlier tick, and
                # clearing it would take back a suppression that is TRUE and hand
                # that verdict the actions of whatever turn this fire produces.
                # Only the streak reset has to reach the store.
                self._persist_soon()
            return False
        self._persist_soon()
        logger.debug("AutoNudge: loop %s quiet tick (%s)", loop.id, verdict.body)
        return True

    # WAKE, and FALLBACK, both spend a turn. FALLBACK is counted separately
    # from a wake so the metering cannot flatter itself: a gate that never
    # works would otherwise read as a busy, well-used watch.
    monitor.quiet_streak = 0
    if verdict.outcome is irq.Outcome.WAKE:
        # NOT charged here. A wake is a DELIVERED turn, and this tick has not
        # delivered one yet -- the fire that follows can still be refused (a
        # busy slot, a callback error, a loop deactivated mid-flight). Charging
        # now would report a turn that never ran and would hand out the
        # follow-up allowance for it, so the next tick would skip its
        # observation to protect work that was never started. The charge is
        # claimed at the one point delivery is confirmed, in
        # :meth:`_run_fire_cycle`. A process that dies in between charges
        # nothing, which is the right direction: never invent a turn.
        self._pending_monitor_wake.add(loop.id)
    else:
        # A fallback is an OBSERVATION outcome, not a delivery, so it is
        # counted here where it happened.
        monitor.gate_fallbacks += 1
    monitor.last_observed_at = time.time()
    self._persist_soon()
    return False


async def _terminal_still_holds(
    self: AutoNudgeService, loop: NudgeLoop, monitor: MonitorState
) -> bool:
    """Re-observe the subject and report whether the OWED terminal still holds.

    Used only where a settlement is about to deactivate a loop, because that is the
    one action here that stops work silently. The answer is deliberately asymmetric:
    only a fresh terminal that CARRIES THE SAME CLASSIFICATION returns True, so an
    unobservable subject -- a failed fetch, a probe defect, a binding that stops
    resolving -- keeps the watch alive rather than letting an absence of evidence
    retire it.

    Matching the classification matters as much as matching the outcome. A pull
    request can be closed, reopened and MERGED inside one channel turn, and a
    revalidation that accepted any terminal would then settle the merge under the
    stale "blocked" marker and announce an unmerged close. When the two disagree the
    owed terminal is simply gone: this returns False, the debt is dropped, and the
    next tick records the real one with the right classification.

    Reuses the tick's fetch machinery, and the same merged-versus-closed rule the
    tick's own terminal branch applies, instead of adding a marker to remember what
    was already delivered. This review has paid for a defect at an existing site for
    each new piece of per-loop state, so re-asking is the cheaper way to answer.
    """
    target = loop_subject(loop)
    probe = probes.build(
        monitor.kind,
        worker_running=self._worker_running,
        worker_closed=self._worker_closed,
    )
    if target is None or probe is None:
        # Cannot re-check, so cannot confirm. Keep the loop alive.
        return False
    # A DISTINCT identity, because this call throws its verdict away. ``identity``
    # is the kernel's dedupe key -- ``poll``'s own contract says it "replaces the
    # cron job id in the state digest, so two drivers watching one subject keep
    # independent dedupe memories" -- so sharing the tick's key would let this
    # re-read consume the tick's credit for the consecutive-failure backstop.
    identity = f"{loop.id}:{target.host_key}:terminal-recheck"
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: irq.poll(identity, target.message, probe)
        )
    except Exception:
        logger.warning(
            "AutoNudge: could not revalidate the terminal verdict for %s -- keeping "
            "the watch alive rather than settling on a stale observation",
            loop.id,
            exc_info=True,
        )
        return False
    observation = _pr_observation_of(probe)
    if observation is None or not observation.is_terminal:
        return False
    fresh = "success" if observation.merged else "blocked"
    if fresh != monitor.terminal_pending:
        logger.info(
            "AutoNudge: loop %s owed a %s settlement but now observes %s -- dropping "
            "the owed one rather than announcing the wrong ending",
            loop.id,
            monitor.terminal_pending,
            fresh,
        )
        return False
    return True
