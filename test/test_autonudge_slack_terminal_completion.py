"""A Slack monitor wake that ends its own monitor still finishes its turn.

A structured monitor on a ``slack:`` slot runs its wake turn inside the loop's
timer task: ``_timer`` -> ``_run_timer_callback`` -> the controller's tick ->
``_dispatch_claimed`` -> ``_fire_slack_nudge``. The turn's completion is charged
through ``record_monitor_turn_completion``, whose accounting runs in a detached
child. When that charge ends the monitor -- the turn spent its budget, an approval
went unanswered during it, or a person stopped the watch while it ran -- the child
retires the loop's timer. That timer is the task awaiting the child, so retiring it
must count as self-cancellation: a ``CancelledError`` there passes the gateway's
``except Exception`` around the completion report, and the wake never writes its
turn row, posts its reply or saves it for replay.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import pytest

from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.monitoring.completion import MonitorCompletionHook
from kiro_crew.monitoring.controller import MonitorController
from kiro_crew.monitoring.github_pull_request import GitHubPullRequestProbeResult
from kiro_crew.monitoring.models import (
    MONITOR_STOP_AGENT_TURN_BUDGET,
    MONITOR_STOP_APPROVAL_STALL,
    MONITOR_STOP_USER,
    MonitorActionDisposition,
    MonitorBudgets,
    MonitorCreationSurface,
    MonitorDispatchResult,
    MonitorObservation,
    MonitorObservationStatus,
    MonitorOutcome,
)

#: Lost-run ceiling for a wait the test itself must see resolve.
_LOST_RUN_SECS = 10.0
_SLOT = "slack:111.222"
_TARGET = "https://github.com/acme/widgets/pull/7"
_FINGERPRINT = "red-1"

#: Terminal evidence recorded while the wake's turn runs, before its completion.
_Evidence = Callable[[AutoNudgeService, NudgeLoop], Awaitable[None]]


class _FailingCheckProvider:
    """A pull request whose required check failed: actionable on the first probe."""

    def probe(self, subjects, *, previous_observations=None, use_owner_credentials=True):
        canonical = {
            "kind": "github_pull_request",
            "target": "github.com/acme/widgets#7",
            "state": "open",
            "draft": False,
            "head_revision": "abc123",
            "mergeability": "mergeable",
            "review_decision": "approved",
            "blocking_review": "none",
            "unresolved_review_threads": 0,
            "review_threads_complete": True,
            "checks": {"failed": ["ci"], "passed": [], "pending": [], "unknown": []},
            "checks_complete": True,
        }
        result = GitHubPullRequestProbeResult(
            response=None,
            canonical=canonical,
            observation=MonitorObservation(
                _FINGERPRINT,
                MonitorObservationStatus.ACTIONABLE,
                reason_code="checks_failed",
                summary="A required check failed.",
            ),
        )
        return {subject: result for subject in subjects}


async def _no_further_evidence(_svc: AutoNudgeService, _loop: NudgeLoop) -> None:
    """The completion alone spends the one-turn budget."""


async def _approval_unanswered(svc: AutoNudgeService, loop: NudgeLoop) -> None:
    """The turn's tool approval timed out: Slack's approval wait runs in the turn."""
    before = set(svc._inflight_adds)
    svc.notify_approval_stalled(loop.slot_key)
    (recorder,) = set(svc._inflight_adds) - before
    await asyncio.wait_for(recorder, timeout=_LOST_RUN_SECS)
    assert loop.approval_stalled


async def _person_stops_the_watch(svc: AutoNudgeService, loop: NudgeLoop) -> None:
    """A dashboard stop lands from its own handler task while the turn runs."""
    stopped = await asyncio.wait_for(
        asyncio.create_task(svc.stop_monitor(loop.id)), timeout=_LOST_RUN_SECS
    )
    assert stopped is loop
    assert loop.monitor is not None and loop.monitor.outcome is MonitorOutcome.USER_STOP


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budgets", "evidence", "outcome", "stopped_reason"),
    [
        pytest.param(
            MonitorBudgets(max_agent_turns=1),
            _no_further_evidence,
            MonitorOutcome.BUDGET,
            MONITOR_STOP_AGENT_TURN_BUDGET,
            id="agent_turn_budget",
        ),
        pytest.param(
            MonitorBudgets(),
            _approval_unanswered,
            MonitorOutcome.BLOCKED,
            MONITOR_STOP_APPROVAL_STALL,
            id="approval_stall",
        ),
        pytest.param(
            MonitorBudgets(),
            _person_stops_the_watch,
            MonitorOutcome.USER_STOP,
            MONITOR_STOP_USER,
            id="user_stop",
        ),
    ],
)
async def test_a_slack_wake_that_ends_its_monitor_still_posts_its_reply(
    tmp_path,
    budgets: MonitorBudgets,
    evidence: _Evidence,
    outcome: MonitorOutcome,
    stopped_reason: str,
) -> None:
    """The completion's child retires the timer it runs under without cancelling it."""
    controller: MonitorController | None = None
    dispatched_in: list[asyncio.Task | None] = []
    report_failures: list[Exception] = []
    steps: list[str] = []

    async def _monitor_tick(loop: NudgeLoop) -> None:
        # The gateway's own wiring of the controller (``_init_autonudge``).
        assert controller is not None
        await controller.tick(loop, now=time.time())

    svc = AutoNudgeService(base_dir=tmp_path, on_monitor_tick=_monitor_tick)

    async def _fire_slack_wake(loop: NudgeLoop, _envelope: str) -> MonitorDispatchResult:
        """``_fire_slack_nudge``'s structured path, reduced to its accounting order."""
        dispatched_in.append(asyncio.current_task())
        assert loop.monitor is not None
        fingerprint = loop.monitor.last_wake_fingerprint
        hook = MonitorCompletionHook(
            loop.id,
            fingerprint,
            svc.record_monitor_turn_completion,
            acceptance_callback=lambda: svc.mark_monitor_turn_accepted(loop.id, fingerprint),
        )
        # The turn crossed the provider boundary, then ran to its end.
        hook.mark_accepted()
        await evidence(svc, loop)
        # ``_report_monitor_completion``: best-effort accounting that cannot change
        # the delivery, so it catches ``Exception`` -- and a cancellation passes it.
        try:
            await hook.complete(MonitorActionDisposition.SUCCESS, None)
        except Exception as exc:
            report_failures.append(exc)
        steps.append("turn row")  # _persist_turn_row
        steps.append("reply post")  # slack.post_message into the thread
        steps.append("replay save")  # save_conversation_turn_off_loop
        return MonitorDispatchResult.DISPATCHED

    controller = MonitorController(
        svc,
        _fire_slack_wake,
        providers={"github_pull_request": _FailingCheckProvider()},
    )
    await svc.start()
    try:
        loop = await svc.add_monitor(
            slot_key=_SLOT,
            kind="github_pull_request",
            target=_TARGET,
            objective="review_ready",
            cadence_secs=60,
            budgets=budgets,
            creation_surface=MonitorCreationSurface.CHANNEL,
        )
        # Fire the real timer now rather than at the cadence it armed for.
        svc._arm_timer(loop, delay=0.0)
        timer = svc._timers[loop.id]

        done, _pending = await asyncio.wait({timer}, timeout=_LOST_RUN_SECS)

        assert timer in done, "the wake's timer callback never finished"
        assert dispatched_in == [timer], "the wake did not run inside its timer task"
        cut_short = "the wake never wrote its turn row or posted its reply"
        assert steps == ["turn row", "reply post", "replay save"], cut_short
        assert not timer.cancelled(), "the completion cancelled the timer task it ran in"
        assert timer.exception() is None
        assert report_failures == []
        assert loop.monitor is not None
        assert not loop.active
        assert loop.monitor.outcome is outcome
        assert loop.monitor.stopped_reason == stopped_reason
        assert loop.monitor.agent_turns == 1
        assert not loop.monitor.wake_in_flight
        assert loop.id not in svc._timers

        restarted = AutoNudgeService(base_dir=tmp_path)
        await asyncio.to_thread(restarted._load)
        stored = restarted.get_by_id(loop.id)
        assert stored is not None and stored.monitor is not None
        assert not stored.active
        assert stored.monitor.outcome is outcome
        assert stored.monitor.stopped_reason == stopped_reason
        assert stored.monitor.agent_turns == 1
    finally:
        svc.stop()
