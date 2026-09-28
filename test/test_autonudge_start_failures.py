"""A loop whose cycles never get a model session backs off, then stands down.

A delivered cycle whose turn dies on ``session/new timed out`` spends a turn and
produces nothing, so re-arming on the plain interval buys another identical
failure: observed on an operator host as 15 consecutive cycles all ending in
``session/new timed out after 90s (0/10 MCP server(s) reported)``, stopped only
by ``max_cycles`` running out. These tests pin the streak, the escalating
deferral, and the terminal stand-down -- all driven by recorded evidence, so a
loop that can start sessions is never slowed.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import (
    SESSION_START_FAILURE_REASON,
    AutoNudgeService,
    NudgeLoop,
)

SLOT = "chat-1-987"


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture(autouse=True)
def _no_published_service_outlives_the_test():
    """Unpublish the singleton and bound leftover work after every test.

    ``start()`` publishes the service as the module singleton and only ``stop()``
    clears it, and the chat runner reaches the service exactly that way -- so a
    service left published hands a later test one bound to a store it is finished
    with, on the very path these tests exercise. Sync on purpose: this suite's
    pytest-asyncio pin errors on async-generator fixtures at setup.
    """
    yield
    svc = _an.get_instance()
    if svc is None:
        return
    try:
        inflight = getattr(svc, "_inflight_adds", None)
        if inflight is not None:
            for task in list(inflight):
                task.cancel()
            inflight.clear()
        svc.stop()
    finally:
        _an._INSTANCE = None


@pytest.fixture
def store_dir(tmp_path_factory):
    """A store directory owned by the SESSION, not by one test.

    ``_persist_locked`` hands ``_write_state`` to a thread and a thread cannot be
    cancelled, so a write that lands late against a per-test ``tmp_path``
    re-creates a directory pytest already removed. A session-scoped directory
    keeps each test isolated while nothing is deleted until every task is dead.
    """
    return tmp_path_factory.mktemp("autonudge-start-failures")


@pytest.fixture
def svc(store_dir):
    return AutoNudgeService(base_dir=store_dir)


@pytest.fixture
def _nosleep(monkeypatch):
    """Collapse the timer's idle wait so ``_timer`` runs synchronously."""

    async def _noop(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _noop)


async def _armed(svc, **kwargs) -> NudgeLoop:
    await svc.start()
    loop = await svc.add(slot_key=SLOT, message="go", idle_secs=600, **kwargs)
    await svc._timers[loop.id]
    return loop


async def _stop_and_drain(svc: AutoNudgeService) -> None:
    timers = list(svc._timers.values())
    svc.stop()
    if timers:
        await asyncio.gather(*timers, return_exceptions=True)
    inflight = list(svc._inflight_adds)
    if inflight:
        await asyncio.gather(*inflight, return_exceptions=True)


@pytest.mark.asyncio
async def test_the_hook_records_a_streak_without_stopping_the_loop(svc, _nosleep):
    loop = await _armed(svc)

    svc.notify_cycle_start_failed(SLOT)
    svc.notify_cycle_start_failed(SLOT)

    assert svc._loops[loop.id].consecutive_start_failures == 2
    assert svc._loops[loop.id].active is True
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_landed_turn_clears_the_streak(svc, _nosleep):
    """A turn that completed proves the session can start.

    Any landed turn counts, a human's as much as a cycle's: the streak only ever
    slows or stops a loop, so clearing it on broader evidence can only keep a
    working loop running.
    """
    loop = await _armed(svc)
    svc.notify_cycle_start_failed(SLOT)
    svc.notify_cycle_start_failed(SLOT)

    svc.notify_cycle_landed(SLOT)

    assert svc._loops[loop.id].consecutive_start_failures == 0
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_short_streak_still_fires(svc, _nosleep):
    """Below the deferral threshold nothing changes: two failures are weather."""
    fired: list[NudgeLoop] = []

    async def on_fire(loop, *_args, **_kwargs):
        fired.append(loop)
        return True

    loop = await _armed(svc)
    svc._on_fire = on_fire
    svc._loops[loop.id].consecutive_start_failures = 2

    svc._cancel_timer(loop.id)
    await svc._timer(svc._loops[loop.id])

    assert len(fired) == 1
    assert svc._loops[loop.id].active is True
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_wake_is_deferred_instead_of_spent_past_the_threshold(svc, _nosleep):
    """Three failures in a row: defer the wake, do not spend a turn on it."""
    fired: list[NudgeLoop] = []

    async def on_fire(loop, *_args, **_kwargs):
        fired.append(loop)
        return True

    loop = await _armed(svc)
    svc._on_fire = on_fire
    svc._loops[loop.id].consecutive_start_failures = 3

    svc._cancel_timer(loop.id)
    await svc._timer(svc._loops[loop.id])

    assert fired == [], "a starved loop must not spend another cycle immediately"
    assert svc._loops[loop.id].active is True, "deferral is not a stop"
    assert loop.id in svc._timers, "the loop must stay armed"
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_deferral_is_one_shot_so_the_streak_can_still_advance(svc, _nosleep):
    """The streak only grows on a DELIVERED cycle, so deferring every wake at the
    same value would freeze it below the stand-down and poll forever -- a slower
    version of the loop this whole bound exists to end. One wake per failure is
    deferred; the next one fires."""
    fired: list[NudgeLoop] = []

    async def on_fire(loop, *_args, **_kwargs):
        fired.append(loop)
        return True

    loop = await _armed(svc)
    svc._on_fire = on_fire
    svc._loops[loop.id].consecutive_start_failures = 3

    svc._cancel_timer(loop.id)
    await svc._timer(svc._loops[loop.id])
    assert fired == [], "the first wake at this streak pays the deferral"

    svc._cancel_timer(loop.id)
    await svc._timer(svc._loops[loop.id])

    assert len(fired) == 1, "the next wake must fire so the streak can advance"
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_recovered_then_re_failed_streak_pays_its_deferral_again(svc, _nosleep):
    """A landed turn drops the paid-deferral marker with the streak it belonged to."""
    fired: list[NudgeLoop] = []

    async def on_fire(loop, *_args, **_kwargs):
        fired.append(loop)
        return True

    loop = await _armed(svc)
    svc._on_fire = on_fire
    svc._loops[loop.id].consecutive_start_failures = 3
    svc._cancel_timer(loop.id)
    await svc._timer(svc._loops[loop.id])
    assert fired == []

    svc.notify_cycle_landed(SLOT)
    for _ in range(3):
        svc.notify_cycle_start_failed(SLOT)
    svc._cancel_timer(loop.id)
    await svc._timer(svc._loops[loop.id])

    assert fired == [], "the fresh streak must pay its own deferral"
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_deferral_escalates_with_the_streak(svc, _nosleep):
    """The delay doubles per failure past the threshold, capped by the interval."""
    delays: list[float] = []
    real_arm = svc._arm_timer

    def _record(loop, delay=None):
        delays.append(delay)
        return real_arm(loop, delay=delay)

    loop = await _armed(svc)
    svc._arm_timer = _record  # type: ignore[method-assign]

    for streak, expected in ((3, 15), (4, 30), (5, 60)):
        # Streak 5 is at the stand-down threshold, so raise the ceiling for the
        # escalation reading; the terminal case has its own test below.
        svc._loops[loop.id].active = True
        svc._loops[loop.id].stopped_reason = ""
        svc._loops[loop.id].consecutive_start_failures = streak
        if streak >= 5:
            break
        delays.clear()
        svc._cancel_timer(loop.id)
        await svc._timer(svc._loops[loop.id])
        assert delays == [expected], f"streak {streak}: got {delays}"

    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_deferral_is_capped_by_the_loops_own_interval(svc, _nosleep):
    """A short-interval loop never waits longer than its own cadence."""
    delays: list[float] = []
    real_arm = svc._arm_timer

    def _record(loop, delay=None):
        delays.append(delay)
        return real_arm(loop, delay=delay)

    await svc.start()
    loop = await svc.add(slot_key=SLOT, message="go", idle_secs=15)
    await svc._timers[loop.id]
    svc._arm_timer = _record  # type: ignore[method-assign]
    svc._loops[loop.id].consecutive_start_failures = 4

    svc._cancel_timer(loop.id)
    await svc._timer(svc._loops[loop.id])

    assert delays == [15]
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_five_failures_stand_the_loop_down(svc, _nosleep):
    """Terminal, in the same shape as the other bounds: deactivate + ``expired``."""
    fired: list[NudgeLoop] = []

    async def on_fire(loop, *_args, **_kwargs):
        fired.append(loop)
        return True

    events: list[tuple[str, str]] = []
    svc.subscribe(lambda ev, lp: events.append((ev, lp.id if lp else "")))
    loop = await _armed(svc)
    svc._on_fire = on_fire
    svc._loops[loop.id].consecutive_start_failures = 5

    svc._cancel_timer(loop.id)
    await svc._timer(svc._loops[loop.id])

    refreshed = svc._loops[loop.id]
    assert refreshed.active is False
    assert refreshed.stopped_reason == SESSION_START_FAILURE_REASON
    assert ("expired", loop.id) in events, f"the stop must be user-visible; got {events}"
    assert fired == []
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_cycle_cap_still_wins(svc, _nosleep):
    """The start-failure bound must not relabel an existing terminal outcome."""
    loop = await _armed(svc, max_cycles=1)
    svc._loops[loop.id].cycle_count = 1
    svc._loops[loop.id].consecutive_start_failures = 9

    svc._cancel_timer(loop.id)
    await svc._timer(svc._loops[loop.id])

    assert svc._loops[loop.id].stopped_reason == "cycle_cap"
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_stand_down_is_re_armable(svc, _nosleep):
    """The remedy is host-side, so a directive may revive the loop -- and the
    revival starts a fresh run, which means the old streak must not survive it."""
    loop = await _armed(svc)
    svc._loops[loop.id].consecutive_start_failures = 5
    svc._cancel_timer(loop.id)
    await svc._timer(svc._loops[loop.id])
    assert svc._loops[loop.id].active is False

    revived = await svc.update(loop.id, active=True)

    assert revived is not None and revived.active is True
    assert svc._loops[loop.id].consecutive_start_failures == 0
    assert svc._loops[loop.id].stopped_reason == ""
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_hook_ignores_an_inactive_loop(svc, _nosleep):
    """A paused loop is not accruing evidence; recording would stale-stop it."""
    loop = await _armed(svc)
    await svc.update(loop.id, active=False)

    svc.notify_cycle_start_failed(SLOT)

    assert svc._loops[loop.id].consecutive_start_failures == 0
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_hooks_are_silent_for_an_unknown_slot(svc, _nosleep):
    await _armed(svc)

    svc.notify_cycle_start_failed("chat-9-nobody")
    svc.notify_cycle_landed("chat-9-nobody")  # must not raise

    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_streak_survives_a_restart(svc, store_dir, _nosleep):
    """The host condition that starves a start routinely outlives a restart, so
    a reload that dropped the streak would restart the doomed cycles."""
    loop = await _armed(svc)
    svc.notify_cycle_start_failed(SLOT)
    svc.notify_cycle_start_failed(SLOT)
    await svc._persist_locked()
    await _stop_and_drain(svc)
    _an._INSTANCE = None

    reloaded = AutoNudgeService(base_dir=store_dir)
    await reloaded.start()
    try:
        assert reloaded._loops[loop.id].consecutive_start_failures == 2
    finally:
        await _stop_and_drain(reloaded)


@pytest.mark.parametrize("stored", ["3", None, -5, 2.9, float("nan"), 10**400])
@pytest.mark.asyncio
async def test_a_malformed_persisted_streak_is_normalised_at_load(svc, store_dir, stored, _nosleep):
    """The store is agent-writable and the streak is compared with ``>=`` on every
    wake, so a string or ``null`` would raise TypeError inside ``_timer`` and the
    automation would silently never fire again -- surviving every reload."""
    loop = await _armed(svc)
    await svc._persist_locked()
    await _stop_and_drain(svc)
    _an._INSTANCE = None

    raw = json.loads((store_dir / "autonudge.json").read_text(encoding="utf-8"))
    for row in raw["loops"]:
        row["consecutive_start_failures"] = stored
    (store_dir / "autonudge.json").write_text(json.dumps(raw), encoding="utf-8")

    reloaded = AutoNudgeService(base_dir=store_dir)
    await reloaded.start()
    try:
        value = reloaded._loops[loop.id].consecutive_start_failures
        assert isinstance(value, int) and value >= 0
        # The comparison the finding named must not raise.
        reloaded._cancel_timer(loop.id)
        await reloaded._timer(reloaded._loops[loop.id])
    finally:
        await _stop_and_drain(reloaded)


def test_both_terminal_branches_report_a_tagged_start_failure(monkeypatch):
    """The two ACP exception families land in DIFFERENT terminal branches.

    A dedicated-client start raises ``AcpTimeoutError`` (an ``AcpError``); a start
    on the shared runtime raises ``AcpSessionStartTimeout``, which descends from
    ``AcpRuntimeError`` and not from ``AcpError``. A member slot runs on the shared
    one, so a report present in only the ``AcpError`` branch would miss exactly the
    population this bound exists for. One helper serves both, and this pins that it
    reads the tag rather than the exception type.
    """
    from kiro_crew.acp.client import AcpTimeoutError
    from kiro_crew.acp.runtime import AcpSessionStartTimeout
    from kiro_crew.dashboard import chat_runner

    reported: list[str] = []

    class _Svc:
        # The autouse teardown unpublishes whatever is published, so a double
        # standing in for the singleton has to satisfy that contract too.
        _inflight_adds: set = set()

        def notify_cycle_start_failed(self, slot_key: str) -> None:
            reported.append(slot_key)

        def stop(self) -> None:
            return None

    monkeypatch.setattr(chat_runner, "logger", chat_runner.logger)
    monkeypatch.setattr(_an, "_INSTANCE", _Svc(), raising=False)

    client_side = AcpTimeoutError(message="session/new timed out after 90s")
    client_side.session_start_failed = True
    runtime_side = AcpSessionStartTimeout("session/new timed out after 90s", collector=None)

    chat_runner._note_cycle_start_failure(SLOT, client_side, self_wake=True)
    chat_runner._note_cycle_start_failure(SLOT, runtime_side, self_wake=True)

    assert reported == [SLOT, SLOT]


def test_an_untagged_or_human_turn_reports_nothing(monkeypatch):
    """Two false-positive guards in one place: an ordinary failure is not a starved
    start, and a human turn must not spend an unrelated loop's stand-down budget."""
    from kiro_crew.acp.client import AcpTimeoutError
    from kiro_crew.acp.runtime import AcpSessionStartTimeout
    from kiro_crew.dashboard import chat_runner

    reported: list[str] = []

    class _Svc:
        # The autouse teardown unpublishes whatever is published, so a double
        # standing in for the singleton has to satisfy that contract too.
        _inflight_adds: set = set()

        def notify_cycle_start_failed(self, slot_key: str) -> None:
            reported.append(slot_key)

        def stop(self) -> None:
            return None

    monkeypatch.setattr(_an, "_INSTANCE", _Svc(), raising=False)

    chat_runner._note_cycle_start_failure(
        SLOT, AcpTimeoutError(message="prompt timed out"), self_wake=True
    )
    chat_runner._note_cycle_start_failure(
        SLOT,
        AcpSessionStartTimeout("session/new timed out", collector=None),
        self_wake=False,
    )

    assert reported == []
