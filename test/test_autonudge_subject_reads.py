"""Between-cycle subject reads of a legacy loop (``autonudge_service.subject_reads``).

Two readers, no model turn either way: an ungated loop's sleep ends early once per
head that needs a re-sync, and an approval-held loop settles when its pull request
ends. Every forge read is replaced by a scripted
observation, so nothing here reaches ``gh``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import MONITOR_TERMINAL_REASON, AutoNudgeService, NudgeLoop
from kiro_crew.autonudge_service import subject_reads
from kiro_crew.autonudge_service.subject_reads import (
    SUBJECT_READ_SECS,
    hold_tick,
    sleep_until_cycle,
)
from kiro_crew.monitoring.models import MonitorOutcome
from kiro_crew.probes import gh_pr
from kiro_crew.slack.gateway import GatewayOrchestrator

PR_URL = "https://github.com/acme/widgets/pull/7"


def _obs(**fields) -> gh_pr.PrObservation:
    base = {
        "repo": "acme/widgets",
        "pr": 7,
        "host": "",
        "status": gh_pr.STATUS_OK,
        "observed_at": 0.0,
        "state": "OPEN",
        "mergeability": "MERGEABLE",
        "merge_state": "CLEAN",
        "head": "a" * 40,
    }
    base.update(fields)
    return gh_pr.PrObservation(**base)


class _Reads:
    """Scripted ``_read_state``: hands out *readings* in order, then repeats the last."""

    def __init__(self, *readings: gh_pr.PrObservation | None) -> None:
        self.readings = list(readings)
        self.messages: list[str] = []

    async def __call__(self, message: str) -> gh_pr.PrObservation | None:
        self.messages.append(message)
        if len(self.readings) > 1:
            return self.readings.pop(0)
        return self.readings[0] if self.readings else None


@pytest.fixture
def slept(monkeypatch) -> list[float]:
    """Record every sleep and return at once, so a chunked sleep is countable."""
    calls: list[float] = []

    async def _sleep(secs: float) -> None:
        calls.append(secs)

    monkeypatch.setattr(subject_reads, "_nap", _sleep)
    return calls


def _fake_svc() -> SimpleNamespace:
    return SimpleNamespace(_subject_wake_fp={})


def _ungated(**overrides) -> NudgeLoop:
    values = {"id": "l1", "slot_key": "chat-1-1", "message": f"watch {PR_URL}", "idle_secs": 1800}
    values.update(overrides)
    return NudgeLoop(**values)


# ── PrObservation.conflict_kind and fetch_state ─────────────────────────────


@pytest.mark.parametrize(
    ("fields", "kind"),
    [
        ({"mergeability": "CONFLICTING", "merge_state": "DIRTY"}, "conflicting"),
        ({"mergeability": "UNKNOWN", "merge_state": "DIRTY"}, "conflicting"),
        ({"merge_state": "BEHIND"}, "behind"),
        ({"mergeability": "UNKNOWN", "merge_state": "UNKNOWN"}, ""),
        ({"merge_state": "BLOCKED"}, ""),
        ({"state": "MERGED", "mergeability": "CONFLICTING"}, ""),
        ({"status": gh_pr.STATUS_UNAVAILABLE, "mergeability": "CONFLICTING"}, ""),
    ],
)
def test_conflict_kind_names_only_an_open_head_that_needs_a_resync(fields, kind) -> None:
    assert _obs(**fields).conflict_kind == kind


def test_fetch_state_is_one_core_read_with_no_board(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []

    def _core(_transport, repo, pr):
        calls.append((repo, pr))
        return {
            "state": "open",
            "mergeable": "conflicting",
            "mergeStateStatus": "dirty",
            "headRefOid": "b" * 40,
            "mergedAt": None,
        }

    monkeypatch.setattr(gh_pr, "_fetch_core", _core)
    observation = gh_pr.fetch_state(json.dumps({"repo": "acme/widgets", "pr": 7}))

    assert calls == [("acme/widgets", 7)]
    assert observation.reached and observation.conflict_kind == "conflicting"
    assert observation.head == "b" * 40 and observation.checks == ()


def test_fetch_state_reports_an_unread_subject_as_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(gh_pr, "_fetch_core", lambda *_a: None)
    observation = gh_pr.fetch_state(json.dumps({"repo": "acme/widgets", "pr": 7}))
    assert not observation.reached and not observation.is_terminal


# ── an ungated loop's sleep ──────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "loop",
    [
        _ungated(gate=True),
        _ungated(message="keep going"),
        _ungated(idle_secs=int(SUBJECT_READ_SECS)),
    ],
    ids=["gated", "no-pull-request", "short-interval"],
)
async def test_these_loops_sleep_exactly_as_before(loop, slept, monkeypatch) -> None:
    reads = _Reads(_obs(mergeability="CONFLICTING"))
    monkeypatch.setattr(subject_reads, "_read_state", reads)
    await sleep_until_cycle(_fake_svc(), loop, float(loop.idle_secs))
    if loop.gate or loop.idle_secs <= SUBJECT_READ_SECS:
        assert slept == [float(loop.idle_secs)]
    assert reads.messages == []


@pytest.mark.asyncio
async def test_a_conflict_ends_the_sleep_once_per_head(slept, monkeypatch) -> None:
    svc, loop = _fake_svc(), _ungated()
    monkeypatch.setattr(
        subject_reads, "_read_state", _Reads(_obs(), _obs(mergeability="CONFLICTING"))
    )

    await sleep_until_cycle(svc, loop, 1800.0)
    assert slept == [SUBJECT_READ_SECS, SUBJECT_READ_SECS], "woke on the second read"
    assert svc._subject_wake_fp["l1"] == f"{'a' * 40}:conflicting"

    slept.clear()
    await sleep_until_cycle(svc, loop, 1800.0)
    assert sum(slept) == pytest.approx(
        1800.0, abs=1.0
    ), "the same conflict on the same head wakes nothing more"

    slept.clear()
    monkeypatch.setattr(
        subject_reads, "_read_state", _Reads(_obs(head="c" * 40, mergeability="CONFLICTING"))
    )
    await sleep_until_cycle(svc, loop, 1800.0)
    assert slept == [SUBJECT_READ_SECS], "a new head that conflicts is news"


@pytest.mark.asyncio
async def test_a_behind_head_ends_the_sleep(slept, monkeypatch) -> None:
    monkeypatch.setattr(subject_reads, "_read_state", _Reads(_obs(merge_state="BEHIND")))
    await sleep_until_cycle(_fake_svc(), _ungated(), 1800.0)
    assert slept == [SUBJECT_READ_SECS]


@pytest.mark.asyncio
async def test_a_settled_clean_read_rearms_the_same_head(slept, monkeypatch) -> None:
    svc, loop = _fake_svc(), _ungated()
    svc._subject_wake_fp["l1"] = f"{'a' * 40}:conflicting"
    monkeypatch.setattr(
        subject_reads, "_read_state", _Reads(_obs(), _obs(mergeability="CONFLICTING"))
    )
    await sleep_until_cycle(svc, loop, 1800.0)
    assert slept == [SUBJECT_READ_SECS, SUBJECT_READ_SECS]


@pytest.mark.asyncio
async def test_an_uncomputed_read_does_not_forget_the_conflict(slept, monkeypatch) -> None:
    svc, loop = _fake_svc(), _ungated()
    svc._subject_wake_fp["l1"] = f"{'a' * 40}:conflicting"
    monkeypatch.setattr(
        subject_reads,
        "_read_state",
        _Reads(
            _obs(mergeability="UNKNOWN", merge_state="UNKNOWN"), _obs(mergeability="CONFLICTING")
        ),
    )
    await sleep_until_cycle(svc, loop, 1800.0)
    assert sum(slept) == pytest.approx(1800.0, abs=1.0), "UNKNOWN is not a settled clean read"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reading",
    [_obs(state="MERGED", merged_at="2026-01-01T00:00:00Z"), _obs(state="CLOSED"), None],
    ids=["merged", "closed", "unread"],
)
async def test_an_ended_or_unread_subject_only_goes_on_sleeping(
    reading, slept, monkeypatch
) -> None:
    loop = _ungated()
    monkeypatch.setattr(subject_reads, "_read_state", _Reads(reading))
    await sleep_until_cycle(_fake_svc(), loop, 1800.0)
    assert sum(slept) == pytest.approx(1800.0, abs=1.0)
    assert loop.active is True, "an ungated loop is never ended by a read"


@pytest.mark.asyncio
async def test_a_held_or_inactive_loop_is_not_read_during_its_sleep(slept, monkeypatch) -> None:
    reads = _Reads(_obs(mergeability="CONFLICTING"))
    monkeypatch.setattr(subject_reads, "_read_state", reads)
    await sleep_until_cycle(_fake_svc(), _ungated(approval_stalled=True), 1800.0)
    await sleep_until_cycle(_fake_svc(), _ungated(active=False), 1800.0)
    assert reads.messages == []


# ── the timer: a conflict wake fires the cycle through the normal ladder ────


@pytest.fixture(autouse=True)
def _enable(_floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture(autouse=True)
def _no_published_service_outlives_the_test():
    yield
    svc = _an.get_instance()
    if svc is None:
        return
    try:
        for task in list(getattr(svc, "_inflight_adds", ())):
            task.cancel()
        svc.stop()
    finally:
        _an._INSTANCE = None


@pytest.fixture
def svc(tmp_path_factory) -> AutoNudgeService:
    return AutoNudgeService(base_dir=tmp_path_factory.mktemp("autonudge-store"))


async def _stop_and_drain(svc: AutoNudgeService) -> None:
    timers = list(svc._timers.values())
    svc.stop()
    if timers:
        await asyncio.gather(*timers, return_exceptions=True)
    inflight = list(svc._inflight_adds)
    if inflight:
        await asyncio.gather(*inflight, return_exceptions=True)


async def _held(svc: AutoNudgeService, monkeypatch, **kwargs) -> tuple[NudgeLoop, list]:
    """A started service with one approval-held loop and a recorded re-arm."""
    await svc.start()
    loop = await svc.add(
        slot_key="chat-1-123", message=f"babysit {PR_URL}", idle_secs=1800, **kwargs
    )
    svc._cancel_timer(loop.id)
    svc.notify_approval_stalled("chat-1-123")
    await asyncio.gather(*list(svc._inflight_adds))
    armed: list = []
    monkeypatch.setattr(svc, "_arm_timer", lambda lp, delay=None: armed.append(delay))
    return svc._loops[loop.id], armed


@pytest.mark.asyncio
async def test_a_conflict_wake_fires_the_ungated_cycle(svc, slept, monkeypatch) -> None:
    fired: list[str] = []

    async def on_fire(loop):
        fired.append(loop.id)
        return True

    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message=f"babysit {PR_URL}", idle_secs=1800)
    svc._cancel_timer(loop.id)
    svc._on_fire = on_fire
    monkeypatch.setattr(subject_reads, "_read_state", _Reads(_obs(mergeability="CONFLICTING")))

    await svc._timer(loop)

    assert fired == [loop.id]
    assert slept[0] == SUBJECT_READ_SECS
    await _stop_and_drain(svc)


# ── a held loop ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reading", "outcome"),
    [
        (_obs(state="MERGED", merged_at="2026-01-01T00:00:00Z"), "success"),
        (_obs(state="CLOSED"), "blocked"),
    ],
    ids=["merged", "closed-unmerged"],
)
async def test_a_held_loop_whose_pull_request_ended_settles(
    svc, monkeypatch, reading, outcome
) -> None:
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    loop, armed = await _held(svc, monkeypatch)
    monkeypatch.setattr(subject_reads, "_read_state", _Reads(reading))

    await hold_tick(svc, loop)

    settled = svc._loops[loop.id]
    assert settled.active is False
    assert settled.stopped_reason == MONITOR_TERMINAL_REASON
    assert settled.subject_outcome == outcome
    assert settled.approval_stalled is False
    assert events.count("expired") == 1
    assert armed == [], "a settled loop is not re-armed"
    stored = json.loads((svc._store.path).read_text())["loops"][0]
    assert stored["active"] is False and stored["subject_outcome"] == outcome
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_held_gated_loop_records_the_end_on_its_monitor(svc, monkeypatch) -> None:
    loop, _armed = await _held(svc, monkeypatch, gate=True)
    assert loop.monitor is not None
    monkeypatch.setattr(
        subject_reads, "_read_state", _Reads(_obs(state="MERGED", merged_at="2026-01-01T00:00:00Z"))
    )

    await hold_tick(svc, loop)

    settled = svc._loops[loop.id]
    assert settled.active is False
    assert settled.monitor is not None and settled.monitor.outcome is MonitorOutcome.SUCCESS
    assert settled.subject_outcome == "", "a monitor already carries the outcome"
    await _stop_and_drain(svc)


@pytest.mark.asyncio
@pytest.mark.parametrize("reading", [_obs(), None], ids=["open", "unread"])
async def test_a_held_loop_with_a_live_or_unread_subject_stays_held(
    svc, monkeypatch, reading
) -> None:
    loop, armed = await _held(svc, monkeypatch)
    monkeypatch.setattr(subject_reads, "_read_state", _Reads(reading))

    await hold_tick(svc, loop)

    assert svc._loops[loop.id].active is True
    assert svc._loops[loop.id].approval_stalled is True
    assert armed == [SUBJECT_READ_SECS]
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_refused_settlement_write_leaves_the_loop_held(svc, monkeypatch) -> None:
    loop, armed = await _held(svc, monkeypatch)
    monkeypatch.setattr(
        subject_reads, "_read_state", _Reads(_obs(state="MERGED", merged_at="2026-01-01T00:00:00Z"))
    )

    async def _refuse(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(svc, "_write_monitor_snapshot_locked", _refuse)
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))

    await hold_tick(svc, loop)

    kept = svc._loops[loop.id]
    assert kept.active is True and kept.approval_stalled is True
    assert kept.stopped_reason == "" and kept.subject_outcome == ""
    assert "expired" not in events
    assert armed == [SUBJECT_READ_SECS]
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_released_hold_is_not_settled_by_a_read_that_raced_it(svc, monkeypatch) -> None:
    loop, _armed = await _held(svc, monkeypatch)

    async def _read_then_release(_message):
        loop.approval_stalled = False
        return _obs(state="MERGED", merged_at="2026-01-01T00:00:00Z")

    monkeypatch.setattr(subject_reads, "_read_state", _read_then_release)
    await hold_tick(svc, loop)

    assert svc._loops[loop.id].active is True
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_cancel_while_waiting_for_the_lock_settles_nothing(svc, monkeypatch) -> None:
    loop, _armed = await _held(svc, monkeypatch)
    monkeypatch.setattr(
        subject_reads, "_read_state", _Reads(_obs(state="MERGED", merged_at="2026-01-01T00:00:00Z"))
    )
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))

    await svc._lock.acquire()
    try:
        tick = asyncio.ensure_future(hold_tick(svc, loop))
        for _ in range(20):
            await asyncio.sleep(0)
        tick.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tick
    finally:
        svc._lock.release()

    assert loop.active is True and loop.approval_stalled is True
    assert loop.stopped_reason == "" and "expired" not in events
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_settlement_is_on_disk_before_any_reader_sees_it(svc, monkeypatch) -> None:
    loop, _armed = await _held(svc, monkeypatch)
    monkeypatch.setattr(
        subject_reads, "_read_state", _Reads(_obs(state="MERGED", merged_at="2026-01-01T00:00:00Z"))
    )
    seen_during_write: list[bool] = []
    real_write = svc._write_monitor_snapshot_locked

    async def _observing_write(payload=None):
        seen_during_write.append(loop.active)
        await real_write(payload)

    monkeypatch.setattr(svc, "_write_monitor_snapshot_locked", _observing_write)
    await hold_tick(svc, loop)

    assert seen_during_write == [True], "the live loop changes only after the write"
    assert loop.active is False
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_time_spent_reading_counts_toward_the_sleep(slept, monkeypatch) -> None:
    async def _slow_read(_message):
        return _obs(), 120.0

    monkeypatch.setattr(subject_reads, "_timed_read", _slow_read)
    await sleep_until_cycle(_fake_svc(), _ungated(), 1800.0)
    assert sum(slept) + 120.0 * (len(slept) - 1) == pytest.approx(1800.0)


# ── the user-facing words ────────────────────────────────────────────────────


def _orch() -> SimpleNamespace:
    return SimpleNamespace(dashboard_state=MagicMock(), _notif_meta=lambda _key: None)


def test_an_ungated_settlement_is_worded_from_its_subject_outcome() -> None:
    orch = _orch()
    loop = NudgeLoop(
        id="l1",
        slot_key="chat-1-1",
        message="m",
        active=False,
        stopped_reason=MONITOR_TERMINAL_REASON,
        subject_outcome="blocked",
    )
    assert GatewayOrchestrator._notify_nudge_expired(orch, loop) is True
    _agent, title, _body = orch.dashboard_state.notify.call_args.args
    assert title == "Monitoring loop stopped — its subject was closed unmerged"


@pytest.mark.asyncio
async def test_a_corrupt_stored_subject_outcome_loads_as_none(svc) -> None:
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="m", idle_secs=1800)
    await _stop_and_drain(svc)
    raw = json.loads(svc._store.path.read_text())
    raw["loops"][0]["subject_outcome"] = "pwned"
    raw["loops"].append(
        dict(raw["loops"][0], id="nullrow1", slot_key="chat-9-9", subject_outcome=None)
    )
    svc._store.path.write_text(json.dumps(raw))

    reloaded = AutoNudgeService(base_dir=svc._store.path.parent)
    reloaded._load()
    assert reloaded._loops[loop.id].subject_outcome == ""
    assert reloaded._loops["nullrow1"].subject_outcome == "", "a stored null is no outcome"
