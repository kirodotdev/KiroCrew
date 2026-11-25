"""Tests for the other pull requests a structured watch also wakes on.

An extra (``also_wake_on``) is read on the target's tick in the same provider call,
wakes the session once when it merges, is closed or is retired as unreadable, and
never ends the watch, writes the target's observation or stamps its one-shot
terminal pivot. Its wake rides the shared claim path with ``also_wake_claim``
naming it. The ``_result`` builder lives in ``monitor_controller_helpers.py``.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest
from monitor_controller_helpers import _result

from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.monitoring.controller import MonitorController
from kiro_crew.monitoring.github_provider_errors import REASON_SHARED_COOLDOWN
from kiro_crew.monitoring.github_pull_request import GitHubPullRequestProbeResult
from kiro_crew.monitoring.models import (
    MonitorActionCompletion,
    MonitorActionDisposition,
    MonitorBudgets,
    MonitorDecision,
    MonitorDispatchResult,
    MonitorObservation,
    MonitorObservationStatus,
    ProviderErrorKind,
    also_wake_fingerprint,
    monitor_state_from_dict,
    monitor_state_public_dict,
    monitor_state_to_dict,
)

A = "https://github.com/acme/widgets/pull/7"
B = "https://github.com/acme/widgets/pull/8"
C = "https://github.com/acme/widgets/pull/9"


def _lifecycle(state: str) -> GitHubPullRequestProbeResult:
    result = _result(MonitorObservationStatus.PENDING, fingerprint=f"fp-{state}")
    return replace(result, canonical={**result.canonical, "state": state})


def _failed_read(*, unattempted: bool = False) -> GitHubPullRequestProbeResult:
    return GitHubPullRequestProbeResult(
        response=None,
        canonical={},
        observation=MonitorObservation(
            "",
            MonitorObservationStatus.PROVIDER_ERROR,
            provider_error=(
                ProviderErrorKind.RATE_LIMITED if unattempted else ProviderErrorKind.TRANSIENT
            ),
            reason_code=REASON_SHARED_COOLDOWN if unattempted else "provider_transient",
        ),
    )


class _Subjects:
    """Answers each subject from a mutable map and records every call's subjects."""

    def __init__(self, answers: dict[str, GitHubPullRequestProbeResult]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, ...]] = []
        self.previous: list[dict[str, object]] = []

    def probe(self, subjects, *, previous_observations=None, use_owner_credentials=True):
        self.calls.append(tuple(subjects))
        self.previous.append(dict(previous_observations or {}))
        return {subject: self.answers[subject] for subject in subjects}


class _Dispatch:
    def __init__(self, *answers: MonitorDispatchResult) -> None:
        self.answers = list(answers)
        self.envelopes: list[str] = []

    async def __call__(self, _loop, envelope):
        self.envelopes.append(envelope)
        return self.answers.pop(0) if self.answers else MonitorDispatchResult.DISPATCHED


async def _arm(tmp_path, answers, dispatch, *, extras=(B,), budgets=None):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add_monitor(
        slot_key="chat-1",
        kind="github_pull_request",
        target=A,
        objective="review_ready",
        cadence_secs=60,
        budgets=budgets or MonitorBudgets(max_runtime_secs=6_000),
        wake_instructions="Check the failed gate.",
        also_wake_on=list(extras),
        now=100.0,
    )
    provider = _Subjects(answers)
    controller = MonitorController(
        service, dispatch, providers={"github_pull_request": provider}, clock=lambda: 130.0
    )
    return service, loop, controller, provider


async def _complete(service, loop, now: float) -> None:
    await service.record_monitor_turn_completion(
        MonitorActionCompletion(
            monitor_id=loop.id,
            fingerprint=loop.monitor.last_wake_fingerprint,
            disposition=MonitorActionDisposition.SUCCESS,
            completed_ts=now,
        )
    )


def _pending() -> GitHubPullRequestProbeResult:
    return _result(MonitorObservationStatus.PENDING)


@pytest.mark.asyncio
async def test_extras_are_read_in_the_targets_probe_call_with_no_borrowed_observation(tmp_path):
    service, loop, controller, provider = await _arm(
        tmp_path, {A: _pending(), B: _lifecycle("open")}, _Dispatch()
    )

    await controller.tick(loop, now=200.0)

    assert provider.calls == [(A, B)]
    assert set(provider.previous[0]) == {A}
    assert loop.monitor.also_wake_state[B]["state"] == "open"


@pytest.mark.asyncio
async def test_an_extra_merge_wakes_once_naming_it_and_the_watch_goes_on(tmp_path):
    dispatch = _Dispatch()
    answers = {A: _pending(), B: _lifecycle("open")}
    service, loop, controller, provider = await _arm(tmp_path, answers, dispatch)
    await controller.tick(loop, now=200.0)
    target_memory = (
        loop.monitor.last_fingerprint,
        loop.monitor.last_observation,
        deepcopy(loop.monitor.coalesce_alerted),
    )

    answers[B] = _lifecycle("merged")
    verdict = await controller.tick(loop, now=300.0)

    state = loop.monitor
    assert verdict.decision is MonitorDecision.WAKE_ACTIONABLE
    assert len(dispatch.envelopes) == 1
    assert f"Also waiting on pull request {B}: it merged." in dispatch.envelopes[0]
    assert "Classification: also_wake_merged." in dispatch.envelopes[0]
    assert state.last_wake_fingerprint == also_wake_fingerprint(B, "merged")
    assert state.also_wake_claim == {
        "target": B,
        "state": "merged",
        "reason_code": "also_wake_merged",
    }
    assert state.last_wake_reason_code == ""
    assert (state.last_fingerprint, state.last_observation, state.coalesce_alerted) == (
        target_memory
    )
    assert state.also_wake_state[B] == {
        "state": "merged",
        "at": 300.0,
        "read_errors": 0,
        "wake_claimed": True,
    }

    await _complete(service, loop, 310.0)
    assert state.also_wake_claim == {}
    for now in (400.0, 500.0, 600.0):
        assert (await controller.tick(loop, now=now)).decision is not (
            MonitorDecision.WAKE_ACTIONABLE
        )
    assert len(dispatch.envelopes) == 1
    assert provider.calls[-3:] == [(A,), (A,), (A,)]
    assert loop.active and state.outcome is None
    assert state.also_wake_on == [B]


@pytest.mark.asyncio
async def test_the_targets_own_wake_goes_first_and_stays_byte_identical(tmp_path):
    actionable = _result(MonitorObservationStatus.ACTIONABLE, fingerprint="fp-red")
    plain_dispatch = _Dispatch()
    _s, plain_loop, plain, _p = await _arm(
        tmp_path / "plain", {A: actionable}, plain_dispatch, extras=()
    )
    await plain.tick(plain_loop, now=200.0)

    dispatch = _Dispatch()
    service, loop, controller, _provider = await _arm(
        tmp_path / "extra", {A: actionable, B: _lifecycle("merged")}, dispatch
    )
    await controller.tick(loop, now=200.0)

    assert dispatch.envelopes == [
        plain_dispatch.envelopes[0].replace(plain_loop.id, loop.id),
    ]
    assert loop.monitor.also_wake_state[B]["wake_claimed"] is False
    await _complete(service, loop, 210.0)
    await controller.tick(loop, now=300.0)
    assert "it merged" in dispatch.envelopes[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("end", "reason", "phrase"),
    [
        ("closed", "also_wake_closed", "it was closed without merging"),
        ("unreadable", "also_wake_unreadable", "it could not be read"),
    ],
)
async def test_a_closed_or_unreadable_extra_wakes_with_its_own_reason(
    tmp_path, end, reason, phrase
):
    dispatch = _Dispatch()
    answers = {A: _pending(), B: _lifecycle("closed") if end == "closed" else _failed_read()}
    service, loop, controller, _provider = await _arm(tmp_path, answers, dispatch)

    now = 200.0
    while not dispatch.envelopes and now < 2_000.0:
        await controller.tick(loop, now=now)
        now += 100.0

    assert f"Classification: {reason}." in dispatch.envelopes[0]
    assert phrase in dispatch.envelopes[0]
    # The extra's failed reads are its own; the target's counters never moved.
    assert loop.monitor.provider_error_count == 0
    assert loop.monitor.consecutive_provider_errors == 0
    assert loop.active


@pytest.mark.asyncio
async def test_an_extras_read_errors_count_only_attempted_consecutive_failures(tmp_path):
    answers = {A: _pending(), B: _failed_read()}
    service, loop, controller, _provider = await _arm(tmp_path, answers, _Dispatch())

    def entry():
        return loop.monitor.also_wake_state[B]

    await controller.tick(loop, now=200.0)
    await controller.tick(loop, now=300.0)
    assert entry()["read_errors"] == 2
    answers[B] = _failed_read(unattempted=True)
    await controller.tick(loop, now=400.0)
    assert entry()["read_errors"] == 2
    answers[B] = _lifecycle("open")
    await controller.tick(loop, now=500.0)
    assert entry()["read_errors"] == 0
    assert entry()["state"] == "open"


@pytest.mark.asyncio
async def test_a_busy_extra_wake_retries_with_the_same_message(tmp_path):
    dispatch = _Dispatch(MonitorDispatchResult.BUSY, MonitorDispatchResult.DISPATCHED)
    service, loop, controller, _provider = await _arm(
        tmp_path, {A: _pending(), B: _lifecycle("merged")}, dispatch
    )

    await controller.tick(loop, now=200.0)
    await controller.tick(loop, now=loop.monitor.next_probe_at)

    assert len(dispatch.envelopes) == 2
    assert dispatch.envelopes[0] == dispatch.envelopes[1]
    assert "it merged" in dispatch.envelopes[1]


@pytest.mark.asyncio
async def test_a_budget_stop_during_a_busy_extra_retry_shows_it_undelivered(tmp_path):
    dispatch = _Dispatch(MonitorDispatchResult.BUSY)
    service, loop, controller, _provider = await _arm(
        tmp_path,
        {A: _pending(), B: _lifecycle("merged")},
        dispatch,
        budgets=MonitorBudgets(max_runtime_secs=300),
    )

    await controller.tick(loop, now=200.0)
    assert loop.monitor.also_wake_state[B]["wake_claimed"] is True
    verdict = await controller.tick(loop, now=450.0)

    assert verdict.decision is MonitorDecision.STOP_BUDGET
    assert not loop.active
    assert loop.monitor.also_wake_claim == {}
    assert loop.monitor.also_wake_state[B]["state"] == "merged"
    assert loop.monitor.also_wake_state[B]["wake_claimed"] is False


@pytest.mark.asyncio
async def test_the_targets_stop_drops_an_undelivered_extra_end_state(tmp_path):
    merged = _result(MonitorObservationStatus.SUCCESS, fingerprint="fp-a-merged")
    service, loop, controller, _provider = await _arm(
        tmp_path, {A: merged, B: _lifecycle("merged")}, _Dispatch()
    )

    verdict = await controller.tick(loop, now=200.0)

    assert verdict.decision is MonitorDecision.STOP_SUCCESS
    assert not loop.active
    assert loop.monitor.also_wake_state[B]["state"] == "merged"
    assert loop.monitor.also_wake_state[B]["wake_claimed"] is False


@pytest.mark.asyncio
async def test_an_extras_update_replaces_the_list_and_keeps_kept_records(tmp_path):
    answers = {A: _pending(), B: _lifecycle("merged"), C: _lifecycle("open")}
    dispatch = _Dispatch()
    service, loop, controller, _provider = await _arm(tmp_path, answers, dispatch)
    await controller.tick(loop, now=200.0)
    assert loop.monitor.wake_in_flight
    generation = loop.monitor.config_generation
    fingerprint = loop.monitor.last_wake_fingerprint

    # Allowed while that wake is in flight: the claim keeps its own extra.
    await service.update_monitor(loop.id, also_wake_on=[B, C, A, C])
    state = loop.monitor
    assert state.also_wake_on == [B, C]
    assert state.also_wake_state[B]["state"] == "merged"
    assert state.also_wake_state[C]["state"] == "open"
    assert state.config_generation == generation + 1
    assert (state.wake_in_flight, state.last_wake_fingerprint) == (True, fingerprint)
    assert state.also_wake_claim["target"] == B

    await service.update_monitor(loop.id, also_wake_on=[])
    assert (state.also_wake_on, state.also_wake_state) == ([], {})
    assert state.also_wake_claim["target"] == B
    await _complete(service, loop, 210.0)

    # Re-added after removal it starts fresh, and a merge already seen wakes once more.
    await service.update_monitor(loop.id, also_wake_on=[B])
    assert state.also_wake_state[B]["state"] == "open"
    await controller.tick(loop, now=state.next_probe_at)
    assert len(dispatch.envelopes) == 2


@pytest.mark.asyncio
async def test_a_retarget_onto_a_listed_extra_removes_it(tmp_path):
    service, loop, _controller, _provider = await _arm(tmp_path, {}, _Dispatch(), extras=(B, C))

    await service.update_monitor(loop.id, target=B)

    assert loop.monitor.target == B
    assert loop.monitor.also_wake_on == [C]
    assert set(loop.monitor.also_wake_state) == {C}


@pytest.mark.asyncio
async def test_also_wake_on_is_refused_for_another_kind(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path)
    with pytest.raises(ValueError, match="github_pull_request"):
        await service.add_monitor(
            slot_key="chat-1",
            kind="bitbucket_pull_request",
            target="https://bitbucket.org/acme/widgets/pull-requests/10",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(max_runtime_secs=600),
            also_wake_on=["https://bitbucket.org/acme/widgets/pull-requests/11"],
            now=100.0,
        )
    loop = await service.add_monitor(
        slot_key="chat-1",
        kind="bitbucket_pull_request",
        target="https://bitbucket.org/acme/widgets/pull-requests/10",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=600),
        now=100.0,
    )
    with pytest.raises(ValueError, match="github_pull_request"):
        await service.update_monitor(
            loop.id, also_wake_on=["https://bitbucket.org/acme/widgets/pull-requests/11"]
        )


def test_a_stale_extra_claim_is_cleared_when_the_record_loads(tmp_path):
    raw = {
        "kind": "github_pull_request",
        "target": A,
        "objective": "review_ready",
        "created_ts": 100.0,
        "also_wake_on": [B],
        "also_wake_claim": {"target": B, "state": "merged", "reason_code": "also_wake_merged"},
        "wake_in_flight": True,
        "last_wake_fingerprint": "a-later-target-wake",
    }

    state = monitor_state_from_dict(raw)

    assert state.also_wake_claim == {}
    assert state.also_wake_state[B]["state"] == "open"
    assert monitor_state_to_dict(state)["also_wake_on"] == [B]
    public = monitor_state_public_dict(state)
    assert (public["also_wake_on"], public["also_wake_claim"]) == ([B], {})
    assert public["also_wake_state"][B]["state"] == "open"


def test_a_stored_record_of_another_kind_with_extras_is_refused_on_load():
    # A row edited to carry extras would read them under the target's own grant.
    raw = {
        "kind": "bitbucket_pull_request",
        "target": "https://bitbucket.org/acme/widgets/pull-requests/10",
        "objective": "review_ready",
        "created_ts": 100.0,
        "also_wake_on": ["https://bitbucket.org/acme/widgets/pull-requests/11"],
    }

    with pytest.raises(ValueError, match="github_pull_request"):
        monitor_state_from_dict(raw)
    assert monitor_state_from_dict({**raw, "also_wake_on": []}).also_wake_on == []


@pytest.mark.asyncio
async def test_a_dashboard_restart_rearms_no_extras(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.handlers import autonudge as handlers
    from kiro_crew.monitoring.models import MonitorOutcome

    service, loop, _controller, _provider = await _arm(tmp_path, {}, _Dispatch())
    try:
        await service.stop_monitor(loop.id, now=150.0)
        assert loop.monitor.outcome is MonitorOutcome.USER_STOP
        assert loop.monitor.also_wake_on == [B]
        monkeypatch.setattr(handlers, "_autonudge_get", lambda: service)
        monkeypatch.setattr(handlers, "sel", lambda: MagicMock())
        monkeypatch.setattr("kiro_crew.autonudge_authz.sel", lambda: MagicMock())
        app = web.Application()
        app["state"] = SimpleNamespace(
            owner_id="",
            _slots={
                "chat-1": SimpleNamespace(
                    workspace="default", is_closing=False, mode="", memory_mode="persistent"
                )
            },
        )
        request = make_mocked_request(
            "POST", f"/api/monitors/{loop.id}/restart", app=app, match_info={"monitor_id": loop.id}
        )
        request["user"] = "local-app"
        request["app"] = ""

        response = await handlers.api_monitor_restart(request)

        assert response.status == 200, response.text
        restarted = service.get_by_slot("chat-1")
        assert restarted is not None and restarted.active and restarted.monitor is not None
        assert restarted.monitor.target == A
        assert restarted.monitor.also_wake_on == []
        assert restarted.monitor.also_wake_state == {}
    finally:
        service.stop()
