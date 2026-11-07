"""Between-cycle subject reads of a legacy loop (``autonudge_service.subject_reads``).

Two readers, no model turn either way: an ungated loop's sleep ends early once per
head that conflicts with its base, and an approval-held loop settles when its pull
request ends. Every forge read is replaced by a scripted
observation, so nothing here reaches ``gh``.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import MONITOR_TERMINAL_REASON, AutoNudgeService, NudgeLoop
from kiro_crew.autonudge_service import subject_reads, timers
from kiro_crew.autonudge_service.subject_reads import (
    SUBJECT_READ_SECS,
    _wake_fingerprint,
    hold_tick,
    sleep_until_cycle,
)
from kiro_crew.monitoring.models import MonitorOutcome
from kiro_crew.probes import gh_pr

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


@pytest.fixture(autouse=True)
def _no_live_forge(_floor_monkeypatch):
    """Fail any test that reaches the real forge read.

    A real read would run ``gh`` with the operator's login, in an executor thread
    that cancelling the timer cannot stop. Patched on the floor stack, so a test's
    own ``monkeypatch.undo()`` cannot lift it. A test that needs the transport
    patches ``_fetch_core`` itself, which replaces this stand-in for that test.
    """
    reached: list[tuple] = []

    def _refuse(*args):
        reached.append(args[1:])
        return None

    _floor_monkeypatch.setattr(gh_pr, "_fetch_core", _refuse)
    yield
    assert reached == [], f"a test reached the real forge read: {reached!r}"


@pytest.fixture
def slept(monkeypatch) -> list[float]:
    """Record every sleep and return at once, so a chunked sleep is countable."""
    calls: list[float] = []

    async def _sleep(secs: float) -> None:
        calls.append(secs)

    monkeypatch.setattr(subject_reads, "_nap", _sleep)
    # A frozen read clock, so a slow worker cannot shorten the scripted sleep.
    monkeypatch.setattr(
        subject_reads, "time", SimpleNamespace(time=time.time, monotonic=lambda: 1_000.0)
    )
    return calls


def _fake_svc() -> SimpleNamespace:
    return SimpleNamespace(_subject_wake_fp={})


def _ungated(**overrides) -> NudgeLoop:
    values = {"id": "l1", "slot_key": "chat-1-1", "message": f"watch {PR_URL}", "idle_secs": 1800}
    values.update(overrides)
    return NudgeLoop(**values)


# ── PrObservation.is_conflicting and fetch_state ────────────────────────────


@pytest.mark.parametrize(
    ("fields", "conflicting"),
    [
        ({"mergeability": "CONFLICTING", "merge_state": "DIRTY"}, True),
        ({"mergeability": "UNKNOWN", "merge_state": "DIRTY"}, True),
        ({"merge_state": "BEHIND"}, False),
        ({"mergeability": "UNKNOWN", "merge_state": "UNKNOWN"}, False),
        ({"merge_state": "BLOCKED"}, False),
        ({"state": "MERGED", "mergeability": "CONFLICTING"}, False),
        ({"status": gh_pr.STATUS_UNAVAILABLE, "mergeability": "CONFLICTING"}, False),
    ],
)
def test_is_conflicting_names_only_an_open_head_that_conflicts(fields, conflicting) -> None:
    assert _obs(**fields).is_conflicting is conflicting


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
    assert observation.reached and observation.is_conflicting
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
    assert svc._subject_wake_fp["l1"] == _wake_fingerprint(_obs())

    slept.clear()
    await sleep_until_cycle(svc, loop, 1800.0)
    assert sum(slept) == 1800.0, "the same conflict on the same head wakes nothing more"

    slept.clear()
    monkeypatch.setattr(
        subject_reads, "_read_state", _Reads(_obs(head="c" * 40, mergeability="CONFLICTING"))
    )
    await sleep_until_cycle(svc, loop, 1800.0)
    assert slept == [SUBJECT_READ_SECS], "a new head that conflicts is news"


@pytest.mark.asyncio
async def test_a_retarget_to_another_pr_on_the_same_head_is_news(slept, monkeypatch) -> None:
    """One branch, two bases: the second pull request's conflict was never answered."""
    svc, loop = _fake_svc(), _ungated()
    monkeypatch.setattr(subject_reads, "_read_state", _Reads(_obs(mergeability="CONFLICTING")))
    await sleep_until_cycle(svc, loop, 1800.0)
    assert slept == [SUBJECT_READ_SECS]

    slept.clear()
    loop.message = "watch https://github.com/acme/widgets/pull/8"
    monkeypatch.setattr(
        subject_reads, "_read_state", _Reads(_obs(pr=8, mergeability="CONFLICTING"))
    )
    await sleep_until_cycle(svc, loop, 1800.0)
    assert slept == [SUBJECT_READ_SECS], "pull/8 conflicting on the same head still wakes"


@pytest.mark.asyncio
async def test_a_head_behind_after_every_sync_never_ends_the_sleep(slept, monkeypatch) -> None:
    """Up-to-date protection on a busy base: each sync's new head is BEHIND again."""
    monkeypatch.setattr(
        subject_reads,
        "_read_state",
        _Reads(*(_obs(head=f"{n:040x}", merge_state="BEHIND") for n in range(6))),
    )
    await sleep_until_cycle(_fake_svc(), _ungated(), 1800.0)
    assert sum(slept) == 1800.0, "BEHIND is not a wake"


@pytest.mark.asyncio
async def test_a_settled_clean_read_rearms_the_same_head(slept, monkeypatch) -> None:
    svc, loop = _fake_svc(), _ungated()
    svc._subject_wake_fp["l1"] = _wake_fingerprint(_obs())
    monkeypatch.setattr(
        subject_reads, "_read_state", _Reads(_obs(), _obs(mergeability="CONFLICTING"))
    )
    await sleep_until_cycle(svc, loop, 1800.0)
    assert slept == [SUBJECT_READ_SECS, SUBJECT_READ_SECS]


@pytest.mark.asyncio
async def test_an_uncomputed_read_does_not_forget_the_conflict(slept, monkeypatch) -> None:
    svc, loop = _fake_svc(), _ungated()
    svc._subject_wake_fp["l1"] = _wake_fingerprint(_obs())
    monkeypatch.setattr(
        subject_reads,
        "_read_state",
        _Reads(
            _obs(mergeability="UNKNOWN", merge_state="UNKNOWN"), _obs(mergeability="CONFLICTING")
        ),
    )
    await sleep_until_cycle(svc, loop, 1800.0)
    assert sum(slept) == 1800.0, "UNKNOWN is not a settled clean read"


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
    assert sum(slept) == 1800.0
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


async def _held(
    svc: AutoNudgeService,
    monkeypatch,
    *,
    message: str = f"babysit {PR_URL}",
    gate: bool = True,
    **kwargs,
) -> tuple[NudgeLoop, list]:
    """A started service with one approval-held loop and a recorded re-arm.

    Gated by default: only a gated loop's hold reads its pull request.
    """
    await svc.start()
    loop = await svc.add(
        slot_key="chat-1-123", message=message, idle_secs=1800, gate=gate, **kwargs
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
    # Stub the read before ``add`` arms the timer: the slept fixture never yields,
    # so the armed timer reaches its first read before ``add`` returns.
    monkeypatch.setattr(subject_reads, "_read_state", _Reads(_obs(mergeability="CONFLICTING")))
    loop = await svc.add(slot_key="chat-1-123", message=f"babysit {PR_URL}", idle_secs=1800)
    svc._cancel_timer(loop.id)
    svc._subject_wake_fp.clear()
    slept.clear()
    svc._on_fire = on_fire

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
    assert settled.monitor is not None and settled.monitor.outcome.value == outcome
    assert settled.approval_stalled is False
    assert events.count("expired") == 1
    assert armed == [], "a settled loop is not re-armed"
    stored = json.loads((svc._store.path).read_text())["loops"][0]
    assert stored["active"] is False and stored["monitor"]["outcome"] == outcome
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_settled_hold_spends_none_of_the_budget(svc, monkeypatch) -> None:
    """Two hours held after 100 seconds running settles as 100 seconds spent."""
    loop, _armed = await _held(svc, monkeypatch)
    now = 1_800_000_000.0
    # The settlement's wall clock is pinned, so the credit is exact however long
    # the persistence write takes.
    monkeypatch.setattr(
        subject_reads, "time", SimpleNamespace(time=lambda: now, monotonic=time.monotonic)
    )
    loop.created_ts = now - 7300.0
    loop.approval_stalled_at = now - 7200.0
    monkeypatch.setattr(
        subject_reads, "_read_state", _Reads(_obs(state="MERGED", merged_at="2026-01-01T00:00:00Z"))
    )

    await hold_tick(svc, loop)

    settled = svc._loops[loop.id]
    assert settled.active is False
    assert settled.created_ts == now - 100.0
    stored = json.loads((svc._store.path).read_text())["loops"][0]
    assert stored["created_ts"] == pytest.approx(settled.created_ts)
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
    await _stop_and_drain(svc)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reading",
    [_obs(state="MERGED", merged_at="2026-01-01T00:00:00Z"), _obs(state="CLOSED")],
    ids=["merged", "closed-unmerged"],
)
async def test_a_held_ungated_loop_survives_its_pull_request_ending(
    svc, monkeypatch, reading
) -> None:
    """``gate=False`` promises a terminal reading never ends the loop, held or not:
    a held ungated loop reads nothing, settles nothing and is not rescued, as on main."""
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    loop, armed = await _held(svc, monkeypatch, gate=False)
    reads = _Reads(reading)
    monkeypatch.setattr(subject_reads, "_read_state", reads)

    await hold_tick(svc, loop)
    svc._reconcile_once()
    svc._reconcile_once()

    kept = svc._loops[loop.id]
    assert kept.active is True and kept.approval_stalled is True
    assert kept.stopped_reason == ""
    assert reads.messages == [], "an ungated hold does not read its pull request"
    assert "expired" not in events
    assert armed == [], "an ungated hold re-arms nothing and is never rescued"
    stored = json.loads((svc._store.path).read_text())["loops"][0]
    assert stored["active"] is True and stored["stopped_reason"] == ""
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_an_opt_out_landing_during_the_read_settles_nothing(svc, monkeypatch) -> None:
    """The gate is re-checked under the locks: a loop ungated mid-read keeps its loop."""
    loop, _armed = await _held(svc, monkeypatch)

    async def _read_then_opt_out(_message):
        loop.gate = False
        return _obs(state="MERGED", merged_at="2026-01-01T00:00:00Z")

    monkeypatch.setattr(subject_reads, "_read_state", _read_then_opt_out)
    await hold_tick(svc, loop)

    assert svc._loops[loop.id].active is True
    assert svc._loops[loop.id].stopped_reason == ""
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
async def test_a_held_loop_naming_no_pull_request_reads_nothing(svc, monkeypatch) -> None:
    """Nothing to read: the held tick arms nothing, a turn's end arms the deadline,
    and the reconciler leaves it alone."""
    loop, armed = await _held(svc, monkeypatch, message="go")
    reads = _Reads(_obs())
    monkeypatch.setattr(subject_reads, "_read_state", reads)

    await hold_tick(svc, loop)
    assert armed == [], "a held tick with nothing to read re-arms nothing"

    svc.notify_turn_complete(loop.slot_key)
    assert len(armed) == 1 and armed[0] != SUBJECT_READ_SECS, "toward its deadline"

    armed.clear()
    svc._reconcile_once()
    svc._reconcile_once()
    assert armed == [] and loop.id not in svc._reconcile_candidates, "never a rescue"
    assert reads.messages == [], "a loop naming no pull request has nothing to read"
    assert svc._loops[loop.id].approval_stalled is True
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_turn_ending_on_a_held_loop_restores_its_persisted_deadline(
    svc, monkeypatch
) -> None:
    """A held read keeps the cycle deadline that makes a later release overdue."""
    loop, armed = await _held(svc, monkeypatch)
    loop.idle_secs = 86400
    loop.next_due_ts = 0.0
    now = [time.time()]
    monkeypatch.setattr(
        timers,
        "time",
        SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic),
    )

    svc.notify_turn_complete(loop.slot_key)

    expected_deadline = now[0] + loop.idle_secs
    assert loop.next_due_ts == expected_deadline, "held turn completion must restore its deadline"
    assert armed == [SUBJECT_READ_SECS], "a turn's end must arm the held subject read"
    await asyncio.gather(*list(svc._inflight_adds))
    stored = json.loads(svc._store.path.read_text())["loops"][0]
    assert stored["next_due_ts"] == expected_deadline, "the restored deadline must be durable"

    now[0] = expected_deadline + 1.0
    armed.clear()
    assert await svc.release_approval_hold(loop.slot_key, why="test release") is True
    assert armed == [float(_an._OVERDUE_REARM_SECS)], "release must use the overdue beat"
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_reconciler_rescues_a_held_loop_with_no_timer(svc, monkeypatch) -> None:
    """A held loop naming a pull request re-arms itself, so a dead timer is a stranding."""
    loop, armed = await _held(svc, monkeypatch)

    svc._reconcile_once()
    svc._reconcile_once()

    assert armed == [SUBJECT_READ_SECS], "a rescued hold reads one read interval out"
    assert svc._loops[loop.id].approval_stalled is True, "rescued, still held"
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
    assert kept.stopped_reason == ""
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
