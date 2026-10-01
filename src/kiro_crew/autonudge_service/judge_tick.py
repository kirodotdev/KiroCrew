"""The wake judge's screening of one tick, and the labelling of its verdicts.

:func:`_judge_tick_is_quiet` asks the ``nudge.wake`` decision point whether a tick is
worth a turn, commits the verdict, cursors and streak durably before it answers, and
bounds how long a judge may keep a loop quiet. The delivery stamp, the owner-action
label and the calibration rows it writes keep the judge's per-loop hit rate honest.
The decisions graph is imported inside each function, never here, so it stays off the
gateway boot path.

Its functions are :class:`~kiro_crew.autonudge.AutoNudgeService` methods: each is bound
on the class by name and runs against the service's state through ``self``, and a call
to any other service method goes through ``self`` too, so a patch on the instance
reaches it.
"""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from kiro_crew import irq, validation
from kiro_crew.autonudge_service.gate import _JUDGE_QUIET_STREAK_FLOOR_DEFAULT
from kiro_crew.autonudge_service.model import NudgeLoop

if TYPE_CHECKING:
    from kiro_crew.autonudge import AutoNudgeService

# The service's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.autonudge")


def _judge_quiet_streak_floor(self: AutoNudgeService) -> int:
    """Consecutive judge QUIET verdicts allowed before a tick fires anyway.

    Configurable DOWNWARD only -- clamped into ``1 .. the shipped floor`` whatever the config
    holds, because the config is agent-writable and this number bounds how long
    a judge may keep a loop silent. An unreadable value is the default, not an
    error: a broken knob must not change whether a loop is delivered.
    """
    try:
        from kiro_crew.config import live

        snapshot = live.snapshot()
        section = getattr(getattr(snapshot, "decisions", None), "nudge_wake", None)
        raw = getattr(section, "quiet_streak_floor", _JUDGE_QUIET_STREAK_FLOOR_DEFAULT)
        floor = int(raw)
    except Exception:
        return _JUDGE_QUIET_STREAK_FLOOR_DEFAULT
    if floor < 1:
        return _JUDGE_QUIET_STREAK_FLOOR_DEFAULT
    # The shipped floor is itself the ceiling, so this knob only ever shortens the
    # silence window. An upward range would be a second way to keep a loop quiet,
    # and ``config.json`` is agent-writable, which is precisely the threat named
    # above -- a raised floor needs no new code to silence a watch, only a number.
    # Clamped rather than rejected, the way ``decisions.gate.in_bucket`` clamps its
    # sample: a typo must not change whether a loop is delivered.
    return min(floor, _JUDGE_QUIET_STREAK_FLOOR_DEFAULT)


async def _judge_tick_is_quiet(self: AutoNudgeService, loop: NudgeLoop) -> bool | None:
    """The wake judge's answer for this tick, or ``None`` when no judge applies.

    ``None`` -- not ``False`` -- for "no judge applies to this tick", so the caller
    falls through to the typed probe path unchanged. That is what keeps an UNGATED
    loop, an explicitly opted-out one, and every loop on a machine that has not
    granted the evidence scope behaving exactly as it does today.

    Once the scope is granted, a GATED loop is screened on every tick whether or not
    its owner wrote a brief: one that named none runs under
    :func:`~kiro_crew.autonudge_judge.default_spec`. Requiring a brief made the
    saving opt-in per loop, and the loops that most need it are armed by agents
    mid-task who have no reason to spend the extra argument.

    Two bypasses, and they mean different things. ``gate=False`` is a loop whose duty
    is to act WHILE its subject is quiet -- a heartbeat, a reviewer chase -- so
    suppressing its quiet ticks would remove the work rather than the waste.
    ``judge: false`` is an owner who wants observation gating and no judge.

    ``True`` skips the turn. Only a QUIET verdict under the streak floor
    produces it; WAKE, TERMINAL and FALLBACK all return ``False`` and spend the
    tick, which is why a provider outage, a scrub that dropped everything and an
    answer outside its own domain are all indistinguishable from the ungated
    timer.

    The judge runs BEFORE the probe guard on purpose: a conductor watching
    sibling sessions has no ``MonitorState`` at all, so anything gated behind
    ``monitor is not None`` would never reach it.
    """
    if self._collect_judge_evidence is None or not getattr(loop, "gate", False):
        # The cheapest possible exit: two plain attribute reads, before any import.
        # The modules below pull the whole decisions graph, so a build with no
        # collector and an ungated loop must not pay for it. ``gate`` is the
        # faithful record of the arming decision -- monitor_start's directive gates
        # unless told otherwise, the generic REST route does not -- so reading it
        # here is reading what the owner chose, not re-inferring it from the message.
        return None
    if loop.judge_wake_pending:
        # An earlier verdict already decided this loop should fire and that
        # delivery was never confirmed -- this process stopped in between, or the
        # fire was refused. Deliver it WITHOUT asking the judge again: the cursors
        # it advanced are durable, so a second reading finds nothing new, answers
        # quiet, and would suppress the very turn that is owed. The flag stays set
        # until the fire is settled, so a refusal re-owes it rather than dropping
        # it, and the per-failure backoff bounds how fast that retries.
        logger.info("AutoNudge: loop %s owes a judge wake -- delivering it", loop.id)
        return False
    from kiro_crew import autonudge_judge as judge
    from kiro_crew import decisions as core_decisions
    from kiro_crew.decisions.points import nudge_wake as point_nudge_wake

    stored = judge.spec_of(loop)
    # The MESSAGE is captured too, because it is half of what the tick is judging.
    # The spec carries the criteria; the targets are parsed out of the message, so a
    # message-only retarget changes WHAT the judge reads while leaving the spec
    # identical. Comparing the spec alone let such a retarget pass both fences below
    # and commit a verdict reached against the previous target's evidence -- which
    # then suppressed the new target's due turn on a streak it never earned.
    stored_message = loop.message
    if validation.judge_is_off(stored):
        # The explicit bypass. Checked before the default is built, because the
        # marker carries no criteria and would otherwise look like a loop that
        # simply named none.
        return None
    brief_kind = judge.BRIEF_CUSTOM
    spec = stored
    if not judge.criteria_of(stored)[0] and not judge.criteria_of(stored)[1]:
        # No criterion of the owner's own, so the default applies -- but ONLY where
        # they granted this point's own egress scope. The ``is_enabled`` reading
        # below is the wrong question for this case: it is satisfied by the LLM lane
        # on the provider key alone, and ``gate._judge_authority`` documents the
        # loop's own ``judge`` spec as HALF of that lane's authorization. A loop
        # whose owner armed nothing supplies no such half, so screening it on that
        # authority alone would read a watch nobody asked for. Keyed on the
        # CRITERIA rather than on the spec being empty: a brief naming only
        # ``targets`` says which subjects to watch and nothing about when to wake,
        # so it needs the default's sentences and keeps its own targets.
        if not await asyncio.to_thread(
            core_decisions.judge_evidence_scope_granted, session_key=loop.slot_key
        ):
            return None
        spec = {**stored, **judge.default_spec()}
        brief_kind = judge.BRIEF_DEFAULT
    # The seam's OWN authority, and the only reading of it: ``is_enabled`` routes
    # this point through ``gate._judge_authority``, which picks the lane and says
    # whether that lane is armed in one answer -- the Jev lane on the main switch
    # plus the ``nudge_evidence`` scope, the small-model lane on its runner being
    # installed. A second resolution here is a second rule, and it can only differ:
    # one that arms Jev on endpoint consent alone picks that lane on a machine which
    # consented without granting the scope, the gate refuses it, and the tick is
    # skipped where the gate itself picks the lane that needs neither.
    #
    # Threaded because the keystone is a file and this coroutine runs on the
    # gateway's event loop, where a per-tick synchronous read would stall chat and
    # every channel transport behind it. ``is_enabled`` documents itself as running
    # on the caller's thread for exactly this reason.
    if not await asyncio.to_thread(
        core_decisions.is_enabled, point_nudge_wake.POINT, session_key=loop.slot_key
    ):
        # No lane can serve this tick. Deliberately NOT a FALLBACK verdict: nothing
        # was asked, so recording a provider failure would put rows in the
        # calibration log for a call that never happened. Returning ``None`` leaves
        # the tick exactly as it found it, which for a judge-only loop means it
        # fires as it would without a brief and for a gated pull-request loop means
        # the typed probe still gets its say. The spec stays stored, so granting the
        # scope later arms every loop already carrying one.
        return None
    wake_when, quiet_when = judge.criteria_of(spec)
    targets_wanted = judge.parse_targets(spec, loop.message)
    if not targets_wanted:
        logger.debug("AutoNudge: loop %s has a judge spec naming no usable target", loop.id)
        return False
    verdict_trace: dict[str, Any] = {}
    try:
        evidence, dropped, staged_cursors = await self._collect_judge_evidence(loop)
    except Exception:
        logger.debug(
            "AutoNudge: judge evidence collection failed for loop %s -- firing as usual",
            loop.id,
            exc_info=True,
        )
        return False
    # Minted before the call so the decision row and the label row that judges it
    # later carry the same key. Random, because the tick and the turn-complete hook
    # do not lock against each other and a restart between them must not reissue a
    # key a verdict already used.
    verdict_id = judge.new_verdict_id()
    verdict = await point_nudge_wake.judge_tick(
        loop.message,
        wake_when=wake_when,
        quiet_when=quiet_when,
        evidence=evidence,
        dropped=dropped,
        last_verdict=loop.judge_last_verdict or None,
        recent_verdicts=judge.recent_for_state(loop.judge_recent_verdicts),
        since_last_wake_s=judge.since_last_wake_s(loop.last_fire_ts),
        quiet_streak=loop.judge_quiet_streak,
        session_key=loop.slot_key,
        extra={
            "loop": loop.id,
            "targets": len(targets_wanted),
            "dropped_targets": dropped,
            # The join key for this verdict's later label row. On the decision row
            # rather than only on the loop record, because the curve the thresholds
            # are tuned from is read out of this file alone.
            "verdict_id": verdict_id,
        },
        trace=verdict_trace,
    )
    if judge.spec_of(loop) != stored or loop.message != stored_message:
        # The brief was REPLACED, or the message RETARGETED, while this tick was
        # reading and judging, and the
        # reads and the decision both happen outside the service lock. Compared
        # against what was STORED when the tick began, never against the spec the
        # tick ran under: those differ whenever the default supplied the sentences,
        # so comparing the merged form would report every default-brief tick as a
        # replacement and discard a verdict nobody withdrew.
        #
        # Every fact staged here belongs to the brief that is gone: the streak was
        # earned under criteria nobody is asking about any more, the verdict record
        # answers a question that was withdrawn, and the cursors say rows were
        # consumed on a target list that may have changed. The update path clears
        # all three for exactly that reason, so committing them here reinstates
        # state nobody armed and can hold the loop quiet on a withdrawn criterion.
        # Cursors go back to the empty map that path installs, which costs one
        # re-read and lets the next tick judge those rows under the brief that now
        # applies. Firing is the safe direction: a turn nobody needed is cheap, and
        # a suppressed turn on a stale question is not recoverable.
        loop.judge_cursors = {}
        logger.info(
            "AutoNudge: loop %s had its judge brief replaced mid-tick -- "
            "discarding the verdict and firing",
            loop.id,
        )
        return False
    # The count the judge actually received, which the point publishes on every
    # return path. The length of ``evidence`` is the pre-scrub input, so a
    # notice or a verdict record built from it credits the judge with rows the
    # scrub rejected and reads as a calmer tick than the one that happened.
    screened_items = int(verdict_trace.get("evidence_items") or 0)
    # Whether the judge was ASKED, which the point reports on every return path. It
    # decides two things and both keep the calibration curve honest: an unasked
    # verdict has no decision row for a label to join to, and it sat on no evidence,
    # so it must not be scored as a suppression the judge got wrong.
    answered = verdict_trace.get("answered") is True
    # The join key is kept only for a verdict the judge answered. Minting one for an
    # unasked tick would write a label row pointing at a decision row that was never
    # written, which a reader tallying the curve cannot tell from a real one.
    row_id = verdict_id if answered else ""
    # RENDERED here, PUBLISHED at the bottom. One line on the owning session for
    # every verdict including a quiet one, because a tick that spent no turn is
    # otherwise indistinguishable from a loop that died -- and that is exactly why
    # it cannot go out before the write it describes. Emitting first meant the
    # notice was a claim about state that might never land, and it also gave the
    # brief a window to be replaced during the emit await, after which the streak
    # and verdict below were committed against criteria nobody armed.
    notice_text = point_nudge_wake.notice_line(
        verdict,
        verdict_trace.get("answers"),
        screened_items,
        brief_kind,
    )
    # The LAST look before anything is committed. The guard above ran before this
    # tick's awaits; the render has none, but the reads and the decision did, so a
    # brief replaced at any point up to here belongs to a question that is gone,
    # and so does a message retargeted at any point up to here: the verdict answers
    # the old target, so committing it would suppress the new one.
    if judge.spec_of(loop) != stored or loop.message != stored_message:
        loop.judge_cursors = {}
        logger.info(
            "AutoNudge: loop %s had its judge brief replaced before the verdict "
            "was committed -- discarding it and firing",
            loop.id,
        )
        return False
    # The read positions are published HERE, with the verdict, and nowhere earlier.
    # The collector returns them rather than assigning them so that every path which
    # leaves before this line -- a cancelled await, a brief replaced mid-tick, a
    # retargeted message -- leaves the stored cursors exactly as they were. A tick
    # that did not reach a verdict has not consumed anything.
    loop.judge_cursors = staged_cursors
    loop.judge_last_verdict = judge.verdict_record(verdict, screened_items)
    # Every branch below writes durably before the notice goes out, and the answer
    # is carried rather than returned, so one publish serves all of them. The
    # collector may have advanced a read cursor, and rows it consumed must not be
    # re-read on the next tick even if this process stops before the verdict is
    # acted on: re-reading them is harmless for a fire and wrong for a quiet,
    # because the same evidence would be judged twice and could hold a loop quiet
    # on rows it had already passed on.
    answer = False
    if verdict.outcome is irq.Outcome.QUIET:
        loop.judge_quiet_streak += 1
        floor = self._judge_quiet_streak_floor()
        if loop.judge_quiet_streak >= floor:
            # Floor reached: deliver anyway. The judge can only read the
            # evidence it was given, and a loop whose duty is to act while its
            # subjects are quiet is invisible to it. Reset before the persist so
            # a restart cannot re-read a streak that was already spent.
            loop.judge_quiet_streak = 0
            # Owed exactly as a wake is owed. This branch FIRES, and the reset it
            # just persisted is what makes the loss silent: a refused delivery or a
            # process that stops here leaves the next tick re-judging against
            # durable cursors, finding nothing new, answering quiet, and pushing the
            # forced turn out another whole floor with nothing recording that one was
            # due. The monitor path's re-owe and its gate-free retry both need a
            # monitor record, and a judge-only loop -- a conductor watching sibling
            # sessions -- has none, so this flag is the only thing covering it.
            loop.judge_wake_pending = True
            # A floor delivery SPENDS the turn, so it withholds nothing and is
            # labelled by what the woken turn does. That is why the label keys off
            # delivery rather than off the verdict's outcome: this row says quiet
            # and fires, and a reader tallying quiet verdicts as suppressions would
            # count it on the wrong side of the curve.
            self._record_judge_verdict(
                loop, verdict, screened_items, row_id, suppressed=False, answered=answered
            )
            await self._persist_judge_state(loop)
            logger.info(
                "AutoNudge: loop %s hit the judge quiet-streak floor after %d quiet verdicts",
                loop.id,
                floor,
            )
        else:
            self._record_judge_verdict(
                loop, verdict, screened_items, row_id, suppressed=True, answered=answered
            )
            if not await self._persist_judge_state(loop):
                logger.warning(
                    "AutoNudge: loop %s judged quiet but its state did not persist -- firing",
                    loop.id,
                )
                # The write did not land, so nothing on disk claims this verdict
                # suppressed anything -- and this tick now FIRES. The row's claim to
                # have withheld the turn is therefore wrong, and left standing it
                # would take a ``missed`` label from the next delivery for a tick
                # that spent its turn. Withdrawing the claim returns the row to the
                # undecided state, where the fire path's own stamp confirms it.
                self._withdraw_judge_suppression(loop)
            else:
                logger.debug("AutoNudge: loop %s judge quiet (%s)", loop.id, verdict.body)
                answer = True
    else:
        loop.judge_quiet_streak = 0
        # Marked BEFORE the write that carries it, so the flag and the advanced
        # cursors land together or not at all. The cursors are what make this
        # necessary: they have moved, so a process that stops between here and the
        # fire leaves the next tick reading nothing new, answering quiet, and the
        # owed turn gone until the streak floor.
        loop.judge_wake_pending = True
        self._record_judge_verdict(
            loop, verdict, screened_items, row_id, suppressed=False, answered=answered
        )
        await self._persist_judge_state(loop)
        logger.info("AutoNudge: loop %s judge verdict %s", loop.id, verdict.outcome.value)
    if self._emit_judge_notice is not None:
        try:
            await self._emit_judge_notice(loop, notice_text)
        except Exception:
            logger.debug(
                "AutoNudge: could not render the judge notice for loop %s",
                loop.id,
                exc_info=True,
            )
    return answer


def _record_judge_verdict(
    self: AutoNudgeService,
    loop: NudgeLoop,
    verdict: Any,
    evidence_items: int,
    verdict_id: str,
    *,
    suppressed: bool,
    answered: bool,
) -> None:
    """Append one verdict to this loop's labelled history, in memory.

    Called before the durable write that carries it, so the row and the cursors
    and streak it belongs with land together or not at all. It records only what
    the tick knows: the outcome, how much evidence it read, whether it withheld
    the turn, whether the judge was asked at all, and the key its label row will
    join on. Whether a turn actually went out is stamped later, by the fire path.
    """
    # The local import keeps the decisions graph off the gateway boot path.
    from kiro_crew import autonudge_judge as judge

    try:
        loop.judge_recent_verdicts = judge.append_verdict(
            loop.judge_recent_verdicts,
            judge.verdict_entry(
                verdict,
                evidence_items,
                suppressed=suppressed,
                answered=answered,
                verdict_id=verdict_id,
            ),
        )
    except Exception:
        # Calibration history is an observation: losing a row must not cost the
        # tick its verdict, which is the decision the loop actually needs.
        logger.debug(
            "AutoNudge: could not record the judge verdict history for loop %s",
            loop.id,
            exc_info=True,
        )


def _withdraw_judge_suppression(self: AutoNudgeService, loop: NudgeLoop) -> None:
    """Take back the newest row's claim that it withheld the turn."""
    # The local import keeps the decisions graph off the gateway boot path.
    from kiro_crew import autonudge_judge as judge

    try:
        loop.judge_recent_verdicts = judge.mark_fired(loop.judge_recent_verdicts)
    except Exception:
        logger.debug(
            "AutoNudge: could not correct the judge verdict history for loop %s",
            loop.id,
            exc_info=True,
        )


def _confirm_judge_delivery(self: AutoNudgeService, loop: NudgeLoop) -> None:
    """Stamp the verdict whose turn just went out, so its label can be read.

    Called from the fire path at the point delivery is confirmed, which is the only
    place that knows a turn really went out. A tick that decided to wake leaves an
    undecided row behind; until this stamp lands, no label pass will touch it, so a
    refused fire, a cancelled timer or a busy slot cannot hand that verdict the
    actions of whatever turn happens to finish next.
    """
    # The local import keeps the decisions graph off the gateway boot path.
    from kiro_crew import autonudge_judge as judge

    try:
        history, stamped = judge.confirm_delivery(loop.judge_recent_verdicts)
    except Exception:
        logger.debug(
            "AutoNudge: could not confirm the judge delivery for loop %s",
            loop.id,
            exc_info=True,
        )
        return
    if stamped:
        loop.judge_recent_verdicts = history


async def _label_judge_delivery_locked(
    self: AutoNudgeService,
    loop: NudgeLoop,
    acted: bool,
    *,
    tool_calls: int | None,
    reply_chars: int,
    tool_names_known: bool = False,
) -> None:
    """Label this loop's newest unlabelled delivery, and the quiets behind it.

    The rule that decides *acted* lives in one place
    (:func:`~kiro_crew.autonudge_judge.owner_acted`); this applies its answer to
    the history and writes one calibration row per label it changed. The loop record
    is written durably before any calibration row is published, so the log cannot
    claim a label the loop state does not carry.

    *tool_names_known* rides to the log row rather than changing anything here: it
    says whether *acted* was decided from NAMED dispatches or from a bare count, and
    a threshold read excludes the count-only rows instead of pooling two rules.

    Every failure is swallowed. This runs on the gateway's turn-completion hook,
    which re-arms the loop's timer immediately afterwards, and a calibration
    label must never be able to stop that.
    """
    # The local import keeps the decisions graph off the gateway boot path.
    from kiro_crew import autonudge_judge as judge

    snapshot = deepcopy(loop.judge_recent_verdicts)

    def _restore_unless_replaced() -> None:
        """Put the snapshot back, unless this loop's history has moved on.

        An update that lands while the write is in flight clears the history on
        purpose, and the snapshot would resurrect rows belonging to an instruction
        or a brief that is gone, with nothing downstream to purge them. The test is
        the one the publication below makes, and it lives here once because two
        restores with two hand-written conditions is how the third one comes to
        disagree.
        """
        if self._loops.get(loop.id) is loop and loop.judge_recent_verdicts is history:
            loop.judge_recent_verdicts = snapshot

    history = snapshot
    try:
        history, changed = judge.label_latest_delivery(loop.judge_recent_verdicts, acted=acted)
        if not changed:
            # No delivery awaiting a label: this loop has no judge history, or this
            # turn was not the one a verdict delivered. Nothing to write either way.
            return
        loop.judge_recent_verdicts = history
        if not await self._persist_judge_state(loop):
            _restore_unless_replaced()
            logger.debug(
                "AutoNudge: left judge delivery unlabelled for loop %s after "
                "its state did not persist",
                loop.id,
            )
            return
        if self._loops.get(loop.id) is not loop or loop.judge_recent_verdicts is not history:
            logger.debug(
                "AutoNudge: skipped judge label publication for loop %s after "
                "its loop or history changed while state persisted",
                loop.id,
            )
            return
        self._append_judge_labels(
            loop,
            changed,
            tool_calls=tool_calls,
            reply_chars=reply_chars,
            tool_names_known=tool_names_known,
        )
    except Exception:
        _restore_unless_replaced()
        logger.debug(
            "AutoNudge: could not label the judge delivery for loop %s",
            loop.id,
            exc_info=True,
        )


def _append_judge_labels(
    self: AutoNudgeService,
    loop: NudgeLoop,
    changed: Sequence[Mapping[str, Any]],
    *,
    tool_calls: int | None,
    reply_chars: int,
    tool_names_known: bool = False,
) -> None:
    """Write one calibration row per label just earned. Never raises.

    Rows go to the decisions log beside the verdict they judge, keyed by the same
    id, so the thresholds this seam reads can be tuned from one file. A row with no
    id is skipped: the judge was not asked for that verdict, so no decision row
    exists for a label to join to. Offloaded to a thread because the append opens
    and writes a file and this runs on the event loop; called only once the loop
    record's own write has landed, so no row here claims a label the record lacks.
    """
    # The local imports keep the decisions graph off the gateway boot path.
    from kiro_crew.decisions import log as decisions_log
    from kiro_crew.decisions.points import nudge_wake as point_nudge_wake

    rows: list[dict[str, Any]] = []
    for row in changed:
        verdict_id = row.get("id")
        if not isinstance(verdict_id, str) or not verdict_id:
            continue
        label = "owner_acted" if isinstance(row.get("owner_acted"), bool) else "missed"
        value = row.get(label)
        if not isinstance(value, bool):
            continue
        rows.append(
            decisions_log.build_wake_label_row(
                verdict_id=verdict_id,
                point=point_nudge_wake.POINT,
                session_key=loop.slot_key,
                label=label,
                value=value,
                tool_calls=tool_calls,
                reply_chars=reply_chars,
                tool_names_known=tool_names_known,
                position_back=row.get("position_back"),
                age_s=row.get("age_s"),
            )
        )
    if not rows:
        return

    def _write() -> None:
        for row in rows:
            decisions_log.append(row)

    coroutine = asyncio.to_thread(_write)
    try:
        task = asyncio.ensure_future(coroutine)
    except RuntimeError:
        coroutine.close()
        # No running loop -- a synchronous test driving the hook directly. The
        # write is an observation, so doing it inline is correct here and costs
        # nothing a test cannot afford.
        _write()
        return
    self._inflight_adds.add(task)

    def _finish(t: "asyncio.Task[None]") -> None:
        self._inflight_adds.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning(
                "AutoNudge: detached judge-label append failed for loop %s",
                loop.id,
                exc_info=t.exception(),
            )

    task.add_done_callback(_finish)


async def _persist_judge_state(self: AutoNudgeService, loop: NudgeLoop) -> bool:
    """Write this tick's judge state durably. ``True`` when it landed.

    Awaited rather than scheduled, because by the time this runs the read
    cursors, the quiet streak and the verdict record are already published in
    memory. A scheduled write that has not landed when the process stops leaves
    a restart re-reading the rows this tick consumed and recounting a streak it
    had already spent.

    The one answer this gate may not give on state that has not landed is
    "quiet": suppressing a turn is the irreversible direction, since nothing
    later says the turn was owed. A caller that suppresses checks the result and
    fires when it is ``False``; a caller already firing does not need to, and
    gets the durable write anyway.

    CANCELLATION SAFETY: same contract as :meth:`update`, and it is needed here
    for the same reason. This runs on the timer BEFORE the loop enters
    ``_firing``, so ``notify_user_input``'s guard does not cover it and ordinary
    user input cancels this task mid-write. ``_persist_locked`` releases
    ``_lock`` when its awaiting task is cancelled, which would let a later write
    land first and then be clobbered by this one's stale snapshot. So the write
    runs as a SHIELDED supervised task: the cancellation reaches this frame while
    the write keeps the lock and drains through ``_inflight_adds``.
    ``CancelledError`` is not caught -- the caller going away is not a
    persistence failure, and swallowing it would break cancellation.
    """
    inner: "asyncio.Task[None]" = asyncio.ensure_future(self._persist_locked())
    self._inflight_adds.add(inner)

    def _finish(t: "asyncio.Task[None]") -> None:
        self._inflight_adds.discard(t)
        if t.cancelled():
            return
        if t.exception() is not None:
            logger.warning(
                "AutoNudge: detached judge-state persist failed for loop %s",
                loop.id,
                exc_info=t.exception(),
            )

    inner.add_done_callback(_finish)
    try:
        await asyncio.shield(inner)
    except Exception:
        logger.warning(
            "AutoNudge: could not persist judge state for loop %s",
            loop.id,
            exc_info=True,
        )
        return False
    return True
