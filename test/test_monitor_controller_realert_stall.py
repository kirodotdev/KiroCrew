"""Tests for an unresolved actionable state that repeated probes keep seeing.

Driven through the service's real probe, completion and update writers: the state
re-asserts once per re-alert period and stays suppressed inside it, and a retarget
clears the coalescing window, the re-alert map and the stall streak. A watch that
stalls on it stops as ``BLOCKED`` under the stall's own reason, and neither a later
probe nor a session-close restore can change that outcome. The ``_result``
probe-result builder lives in ``monitor_controller_helpers.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from monitor_controller_helpers import (
    _result,
)

from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.monitoring.github_pull_request import GitHubPullRequestProbeResult
from kiro_crew.monitoring.models import (
    DEFAULT_MONITOR_CADENCE_SECS,
    DEFAULT_MONITOR_REALERT_SECS,
    DEFAULT_MONITOR_STALL_TICKS,
    MONITOR_STOP_VERDICT_STALL,
    MonitorActionCompletion,
    MonitorActionDisposition,
    MonitorBudgets,
    MonitorDecision,
    MonitorObservation,
    MonitorObservationStatus,
    MonitorOutcome,
)


@pytest.mark.asyncio
async def test_an_unresolved_actionable_state_re_asserts_once_per_period(tmp_path):
    """Re-assertion works through the real writers, once per re-alert period.

    The dedup guard, the wake writer and the completion writer are the real
    ones, so this drives the composed path the isolated decision tests cannot:
    apply_monitor_probe records the wake fingerprint, record_monitor_turn_completion
    clears wake_in_flight while leaving that fingerprint set, and a later probe of
    the SAME unresolved actionable state re-asserts once its period has passed and
    stays suppressed before it. Both directions, because a test that only proves
    the late wake cannot tell re-assertion from a wake on every probe.
    """
    service = AutoNudgeService(base_dir=tmp_path, on_monitor_tick=AsyncMock())
    try:
        loop = await service.add_monitor(
            slot_key="chat-1",
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(max_runtime_secs=604_800),
            now=0.0,
        )
        assert loop.monitor is not None
        gen = loop.monitor.config_generation
        red = _result(MonitorObservationStatus.ACTIONABLE, fingerprint="red-1")

        async def _probe_and_complete(now: float) -> MonitorDecision:
            verdict = await service.apply_monitor_probe(
                loop.id, red, now=now, config_generation=gen
            )
            if verdict.decision is MonitorDecision.WAKE_ACTIONABLE:
                await service.record_monitor_turn_completion(
                    MonitorActionCompletion(
                        monitor_id=loop.id,
                        fingerprint="red-1",
                        disposition=MonitorActionDisposition.SUCCESS,
                        completed_ts=now,
                    )
                )
            return verdict.decision

        # First sighting wakes at once, and the turn completes.
        assert await _probe_and_complete(0.0) is MonitorDecision.WAKE_ACTIONABLE

        # Same unresolved state, well inside the period: suppressed.
        assert (
            await _probe_and_complete(DEFAULT_MONITOR_REALERT_SECS * 0.5)
            is MonitorDecision.NO_CHANGE
        )

        # A second probe still inside the period: still suppressed, so the late
        # wake below cannot be a wake on every probe.
        assert (
            await _probe_and_complete(DEFAULT_MONITOR_REALERT_SECS * 0.75)
            is MonitorDecision.NO_CHANGE
        )

        # Past the period: re-asserted exactly once.
        assert (
            await _probe_and_complete(DEFAULT_MONITOR_REALERT_SECS * 1.1)
            is MonitorDecision.WAKE_ACTIONABLE
        )

        # Immediately after that re-assertion, inside the new period: suppressed
        # again, which proves the wake restamped the period rather than leaving it
        # open to fire on the next probe.
        assert (
            await _probe_and_complete(DEFAULT_MONITOR_REALERT_SECS * 1.2)
            is MonitorDecision.NO_CHANGE
        )
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_retarget_clears_the_coalescing_window(tmp_path):
    """Changing the target resets every per-subject field, the window included.

    A retarget is a new subject, so a window opened on the old subject describes
    nothing on the new one. The reset block clears the per-condition window and
    the re-alert map alongside the other per-subject state, so a re-assert
    decision on the new subject reads none of the old subject's alert times.
    """
    service = AutoNudgeService(base_dir=tmp_path, on_monitor_tick=AsyncMock())
    try:
        loop = await service.add_monitor(
            slot_key="chat-1",
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(max_runtime_secs=604_800),
            now=0.0,
        )
        assert loop.monitor is not None
        # Wake on the old subject so the window and re-alert map carry state.
        await service.apply_monitor_probe(
            loop.id,
            _result(MonitorObservationStatus.ACTIONABLE, fingerprint="red-old"),
            now=10.0,
            config_generation=loop.monitor.config_generation,
        )
        assert loop.monitor.coalesce_windows != {}
        assert loop.monitor.coalesce_alerted != {}

        await service.record_monitor_turn_completion(
            MonitorActionCompletion(
                monitor_id=loop.id,
                fingerprint="red-old",
                disposition=MonitorActionDisposition.SUCCESS,
                completed_ts=10.0,
            )
        )
        # One more probe of the same unresolved state is a counted tick, so the
        # stall streak now carries the OLD subject -- which is what the retarget
        # has to clear. A wake alone would not: acting on a subject zeroes it.
        await service.apply_monitor_probe(
            loop.id,
            _result(MonitorObservationStatus.ACTIONABLE, fingerprint="red-old"),
            now=20.0,
            config_generation=loop.monitor.config_generation,
        )
        assert loop.monitor.stall_digest != ""
        assert loop.monitor.stall_streak == 1
        assert loop.monitor.stall_started_at > 0

        updated = await service.update_monitor(
            loop.id, target="https://github.com/acme/widgets/pull/9"
        )
        assert updated is not None and updated.monitor is not None
        assert updated.monitor.coalesce_windows == {}
        assert updated.monitor.coalesce_alerted == {}
        # A streak counted on the old subject describes nothing on the new one,
        # so carrying it over would retire a fresh watch early.
        assert updated.monitor.stall_digest == ""
        assert updated.monitor.stall_streak == 0
        assert updated.monitor.stall_started_at == 0.0
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_a_stalled_watch_is_recorded_as_a_stall_not_as_the_subject(tmp_path):
    """The production writer files a stall under its own reason.

    ``apply_monitor_probe`` otherwise takes ``stopped_reason`` from the
    observation, so a watch retired for repeating itself would land as
    ``checks_failed`` -- indistinguishable from a pull request that really is
    red, and from the convergence and spent-budget stops the same field carries.
    This drives the composed path the isolated decision tests cannot: the real
    wake writer, the real completion writer and the real reason writer.
    """
    service = AutoNudgeService(base_dir=tmp_path, on_monitor_tick=AsyncMock())
    try:
        loop = await service.add_monitor(
            slot_key="chat-1",
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            # The DEFAULT cadence, so the stall ceiling is the tick count rather
            # than the wall-clock floor -- this test is about the recorded reason,
            # and the floor has its own case in test_monitor_decision.
            cadence_secs=DEFAULT_MONITOR_CADENCE_SECS,
            budgets=MonitorBudgets(max_runtime_secs=604_800),
            now=0.0,
        )
        assert loop.monitor is not None
        gen = loop.monitor.config_generation
        base = _result(MonitorObservationStatus.ACTIONABLE, fingerprint="red-1")
        red = GitHubPullRequestProbeResult(
            response=base.response,
            canonical=base.canonical,
            observation=MonitorObservation(
                "red-1",
                MonitorObservationStatus.ACTIONABLE,
                reason_code="checks_failed",
                summary="One check is failing.",
            ),
        )

        async def _probe_and_complete(now: float) -> MonitorDecision:
            verdict = await service.apply_monitor_probe(
                loop.id, red, now=now, config_generation=gen
            )
            if verdict.decision is MonitorDecision.WAKE_ACTIONABLE:
                await service.record_monitor_turn_completion(
                    MonitorActionCompletion(
                        monitor_id=loop.id,
                        fingerprint="red-1",
                        disposition=MonitorActionDisposition.SUCCESS,
                        completed_ts=now,
                    )
                )
            return verdict.decision

        decisions: list[MonitorDecision] = []
        # Ticks advance by the watch's own cadence, because the trip needs elapsed
        # wall-clock as well as a count. Every probe still sits well inside the
        # re-alert period, so the subject keeps deciding NO_CHANGE.
        for tick in range(DEFAULT_MONITOR_STALL_TICKS + 2):
            decision = await _probe_and_complete(DEFAULT_MONITOR_CADENCE_SECS * (tick + 1))
            decisions.append(decision)
            if decision is MonitorDecision.STOP_BLOCKED:
                break

        assert decisions[0] is MonitorDecision.WAKE_ACTIONABLE
        assert set(decisions[1:-1]) == {MonitorDecision.NO_CHANGE}
        assert decisions[-1] is MonitorDecision.STOP_BLOCKED
        assert loop.monitor.stall_streak == DEFAULT_MONITOR_STALL_TICKS
        assert loop.monitor.outcome is MonitorOutcome.BLOCKED
        assert loop.monitor.stopped_reason == MONITOR_STOP_VERDICT_STALL
        assert loop.monitor.stopped_reason != "checks_failed"
        assert not loop.active
    finally:
        service.stop()


@pytest.mark.asyncio
async def test_the_stall_condition_cannot_outlive_the_tick_that_tripped_it(tmp_path):
    """The invariant that lets ``monitor_stall_reason`` ignore the decision.

    Both writers of ``stopped_reason`` compose it in an arm they also reach for
    ``STOP_SUCCESS``, and the reason function reads the streak alone. So a
    ceiling streak surviving onto a later tick would file a MERGED subject as
    ``SUCCESS`` carrying ``verdict_stall`` -- a conclusion wearing a stall's
    reason. This pins the three clauses that make that unreachable, instead of
    guarding the consequence: a guard on a condition nothing can construct is
    the unexercised branch that got a hard cap deleted in this package before.

    Clause 1 is pinned in ``test_monitor_decision``: the call that reaches the
    ceiling returns the stop, so no later tick starts above ``ceiling - 1``.
    """
    service = AutoNudgeService(base_dir=tmp_path, on_monitor_tick=AsyncMock())
    try:
        loop = await service.add_monitor(
            slot_key="chat-1",
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            # The DEFAULT cadence, so the stall ceiling is the tick count rather
            # than the wall-clock floor -- this test is about the recorded reason,
            # and the floor has its own case in test_monitor_decision.
            cadence_secs=DEFAULT_MONITOR_CADENCE_SECS,
            budgets=MonitorBudgets(max_runtime_secs=604_800),
            now=0.0,
        )
        assert loop.monitor is not None
        gen = loop.monitor.config_generation
        base = _result(MonitorObservationStatus.ACTIONABLE, fingerprint="red-1")
        red = GitHubPullRequestProbeResult(
            response=base.response,
            canonical=base.canonical,
            observation=MonitorObservation(
                "red-1",
                MonitorObservationStatus.ACTIONABLE,
                reason_code="checks_failed",
                summary="One check is failing.",
            ),
        )
        for tick in range(DEFAULT_MONITOR_STALL_TICKS + 2):
            verdict = await service.apply_monitor_probe(
                loop.id,
                red,
                now=DEFAULT_MONITOR_CADENCE_SECS * (tick + 1),
                config_generation=gen,
            )
            if verdict.decision is MonitorDecision.WAKE_ACTIONABLE:
                await service.record_monitor_turn_completion(
                    MonitorActionCompletion(
                        monitor_id=loop.id,
                        fingerprint="red-1",
                        disposition=MonitorActionDisposition.SUCCESS,
                        completed_ts=DEFAULT_MONITOR_CADENCE_SECS * (tick + 1),
                    )
                )
            if verdict.decision is MonitorDecision.STOP_BLOCKED:
                break
        assert loop.monitor.stall_streak == DEFAULT_MONITOR_STALL_TICKS

        # CLAUSE 2 -- the stop's outcome was staged onto the same state as the
        # ceiling streak and applied as one unit, so the pair cannot come apart.
        assert loop.monitor.outcome is MonitorOutcome.BLOCKED
        assert loop.monitor.stopped_reason == MONITOR_STOP_VERDICT_STALL

        # CLAUSE 3a -- with an outcome recorded, a later probe never reaches the
        # decision engine, so the ceiling is never read again. A MERGED subject
        # arriving now is refused rather than composed, which is what keeps it
        # from being filed as SUCCESS with the stall's reason.
        merged = GitHubPullRequestProbeResult(
            response=base.response,
            canonical=base.canonical,
            observation=MonitorObservation(
                "green-1",
                MonitorObservationStatus.SUCCESS,
                reason_code="pull_request_merged",
                summary="Pull request merged.",
            ),
        )
        after = await service.apply_monitor_probe(
            loop.id, merged, now=9_000.0, config_generation=gen
        )
        assert after.decision is MonitorDecision.STOP_BLOCKED
        assert after.entries == ()
        assert loop.monitor.outcome is MonitorOutcome.BLOCKED
        assert loop.monitor.stopped_reason == MONITOR_STOP_VERDICT_STALL
        assert loop.monitor.stall_streak == DEFAULT_MONITOR_STALL_TICKS

        # CLAUSE 3b -- the one path that clears an outcome is fenced to
        # SESSION_CLOSE, which a stall never records, so it cannot resurrect a
        # watch carrying a ceiling streak.
        assert await service.restore_monitor_after_failed_session_close(loop.id) is None
        assert loop.monitor.outcome is MonitorOutcome.BLOCKED
        assert loop.monitor.stopped_reason == MONITOR_STOP_VERDICT_STALL
        assert not loop.active
    finally:
        service.stop()
