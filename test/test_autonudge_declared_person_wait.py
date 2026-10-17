"""A legacy loop's agent declares it is waiting on a person (``waiting_on_person``).

The declared hold fires no cycle, arms no timer and spends neither the cycle cap nor
the runtime budget. A person's return ends it -- a message typed into the dashboard
chat, an answered approval, the popover's Fire now -- and so does the agent passing
``false``. The work ledger neither holds nor releases it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.autonudge_service import firing, timers
from kiro_crew.dashboard import responsive_chat
from kiro_crew.dashboard.handlers import autonudge as autonudge_routes
from kiro_crew.dashboard.responsive_chat import turn_state
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.monitoring.models import MonitorBudgets
from kiro_crew.testing.wait import until_parked

SLOT = "chat-7-777"
SESSION = f"dashboard:{SLOT}"


@pytest.fixture(autouse=True)
def _enable(_floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def _unpublish():
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
def base(tmp_path_factory):
    return tmp_path_factory.mktemp("autonudge-declared-wait")


@pytest.fixture
def svc(base):
    return AutoNudgeService(base_dir=base)


@pytest.fixture
def _nosleep(monkeypatch):
    """The timer's own wait, skipped: each test drives ticks by hand."""

    async def _noop(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _noop)  # flake-ok: no wall clock is read


async def _armed(svc, fired: list, **kwargs) -> NudgeLoop:
    async def on_fire(loop):
        fired.append(loop.cycle_count)
        return True

    await svc.start()
    svc._on_fire = on_fire
    loop = await svc.add(slot_key=SLOT, message="babysit", idle_secs=300, **kwargs)
    await svc._timers[loop.id]
    return loop


async def _tick(svc, loop) -> None:
    svc._cancel_timer(loop.id)
    await svc._timer(loop)


async def _settle(svc) -> None:
    """Await every detached release the service scheduled."""
    tasks = list(svc._inflight_adds)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)


async def _drain(svc: AutoNudgeService) -> None:
    pending = list(svc._timers.values())
    svc.stop()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _no_live_timer(svc, loop) -> bool:
    timer = svc._timers.get(loop.id)
    return timer is None or timer.done() or timer.cancelled()


def _pin_clock(monkeypatch, now: float) -> None:
    """Pin the holds' own clock -- the ``time`` bindings in ``firing`` and ``timers``."""
    real = firing.time
    pinned = SimpleNamespace(**{n: getattr(real, n) for n in dir(real) if not n.startswith("__")})
    pinned.time = lambda: now
    monkeypatch.setattr(firing, "time", pinned)
    monkeypatch.setattr(timers, "time", pinned)


# --------------------------------------------------------------------------- #
# The hold on the timer
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_declared_hold_fires_nothing_and_spends_nothing(svc, _nosleep):
    fired: list = []
    loop = await _armed(svc, fired, max_cycles=3, max_runtime_secs=3600)
    assert len(fired) == 1
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))

    assert await svc.declare_person_wait(SLOT, held=True)
    held = svc._loops[loop.id]
    assert held.waiting_on_person is True and held.person_wait_declared is True
    assert held.waiting_on_person_at > 0
    assert "updated" in events
    assert _no_live_timer(svc, loop), "the declaration cancels the pending timer"

    cycles = held.cycle_count
    held.cycle_count = held.max_cycles  # a spent cap does not stop a held loop
    await _tick(svc, loop)
    await _tick(svc, loop)
    assert len(fired) == 1, "a declared hold fires no cycle"
    assert held.active is True and held.stopped_reason == ""
    assert _no_live_timer(svc, loop), "and arms nothing"
    held.cycle_count = cycles

    reading = autonudge_routes._autonudge_loop_reading(held)
    assert reading["waiting_on_person"] is True
    assert reading["person_wait_declared"] is True
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_long_declared_hold_hands_its_time_back(svc, _nosleep, monkeypatch):
    fired: list = []
    loop = await _armed(svc, fired, max_runtime_secs=3600)
    t = 2_000_000_000.0
    _pin_clock(monkeypatch, t - 7200)
    assert await svc.declare_person_wait(SLOT, held=True)
    held = svc._loops[loop.id]
    created = held.created_ts

    _pin_clock(monkeypatch, t)
    assert await svc.release_approval_hold(SLOT, why="test", arm=False)
    assert held.created_ts - created == 7200.0, "the whole wait is credited"
    assert held.waiting_on_person is False and held.person_wait_declared is False
    assert held.waiting_on_person_at == 0.0
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_person_s_message_releases_and_an_app_s_does_not(svc, _nosleep):
    fired: list = []
    loop = await _armed(svc, fired)
    assert await svc.declare_person_wait(SLOT, held=True)

    svc.notify_user_input(SLOT, human=False)
    await _settle(svc)
    assert svc._loops[loop.id].person_wait_declared is True, "an app token is not a person"

    svc.notify_user_input(SLOT, human=True)
    await _settle(svc)
    refreshed = svc._loops[loop.id]
    assert refreshed.person_wait_declared is False and refreshed.waiting_on_person is False

    # The person's own turn ends and re-arms the loop, which then fires as usual.
    svc.notify_turn_complete(SLOT)
    await svc._timers[loop.id]
    assert len(fired) == 2
    await _drain(svc)


@pytest.mark.asyncio
async def test_an_answered_approval_releases(svc, _nosleep):
    fired: list = []
    loop = await _armed(svc, fired)
    assert await svc.declare_person_wait(SLOT, held=True)
    assert _an.get_instance() is svc

    _an.release_approval_hold_for(SLOT, why="an approval was answered")
    await _settle(svc)
    assert svc._loops[loop.id].person_wait_declared is False
    await _drain(svc)


@pytest.mark.asyncio
async def test_fire_now_releases_through_the_handler_order(svc, _nosleep):
    fired: list = []
    loop = await _armed(svc, fired)
    assert await svc.declare_person_wait(SLOT, held=True)

    # The route's own order: release the hold unarmed, then bring the cycle forward.
    assert await svc.release_approval_hold(SLOT, why="fired by hand", arm=False)
    _loop, _err, status = await svc.fire_now(loop.id)
    assert status == 200
    await svc._timers[loop.id]
    assert len(fired) == 2
    await _drain(svc)


@pytest.mark.asyncio
async def test_the_ledger_neither_holds_nor_releases_a_declared_hold(svc, _nosleep, monkeypatch):
    state = {"fp": None, "reads": 0}

    def _fp(_key, *_a):
        state["reads"] += 1
        return state["fp"]

    monkeypatch.setattr(firing, "_ledger_person_wait_fp", _fp)
    monkeypatch.setattr(AutoNudgeService, "_observes_work_ledger", lambda self, loop: True)
    fired: list = []
    loop = await _armed(svc, fired)
    assert await svc.declare_person_wait(SLOT, held=True)
    reads = state["reads"]

    state["fp"] = "fp-moved"
    await _tick(svc, loop)
    assert svc._loops[loop.id].person_wait_declared is True
    assert state["reads"] == reads, "a declared hold never reads the ledger"
    assert await svc._holds_for_person_wait(svc._loops[loop.id]) is False
    assert len(fired) == 1
    await _drain(svc)


@pytest.mark.asyncio
async def test_the_reconciler_leaves_a_declared_hold_unarmed(svc, _nosleep):
    fired: list = []
    loop = await _armed(svc, fired)
    assert await svc.declare_person_wait(SLOT, held=True)
    for _ in range(3):
        svc._reconcile_once()
    assert _no_live_timer(svc, loop)
    assert loop.id not in svc._reconcile_candidates
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_revival_clears_the_declaration(svc, _nosleep):
    fired: list = []
    loop = await _armed(svc, fired)
    assert await svc.declare_person_wait(SLOT, held=True)
    await svc.update(loop.id, active=False)
    await svc.update(loop.id, active=True)
    refreshed = svc._loops[loop.id]
    assert refreshed.person_wait_declared is False and refreshed.waiting_on_person is False
    await _drain(svc)


@pytest.mark.asyncio
async def test_the_hold_survives_a_restart(base, svc, _nosleep):
    fired: list = []
    loop = await _armed(svc, fired)
    assert await svc.declare_person_wait(SLOT, held=True)
    await _drain(svc)
    _an._INSTANCE = None

    reloaded = AutoNudgeService(base_dir=base)
    reloaded._load()
    again = reloaded._loops[loop.id]
    assert again.person_wait_declared is True and again.waiting_on_person is True
    refired: list = []

    async def on_fire(lp):
        refired.append(lp.cycle_count)
        return True

    reloaded._on_fire = on_fire
    await reloaded._timer(again)
    assert refired == [], "a reloaded declared hold still fires nothing"
    reloaded.stop()


@pytest.mark.asyncio
async def test_an_overlapping_approval_hold_is_credited_once(svc, _nosleep, monkeypatch):
    fired: list = []
    loop = await _armed(svc, fired)
    assert await svc.declare_person_wait(SLOT, held=True)
    held = svc._loops[loop.id]
    t = 2_000_000_000.0
    _pin_clock(monkeypatch, t)
    held.waiting_on_person_at = t - 300
    held.approval_stalled = True
    held.approval_stalled_at = t - 100
    created = held.created_ts

    assert await svc.release_approval_hold(SLOT, why="test", arm=False)
    assert held.created_ts - created == 300.0, "300s held, credited once"
    assert held.approval_stalled is False and held.person_wait_declared is False
    await _drain(svc)


@pytest.mark.asyncio
async def test_an_agent_release_under_an_approval_hold_leaves_the_overlap_to_it(
    svc, _nosleep, monkeypatch
):
    fired: list = []
    loop = await _armed(svc, fired)
    assert await svc.declare_person_wait(SLOT, held=True)
    held = svc._loops[loop.id]
    t = 2_000_000_000.0
    _pin_clock(monkeypatch, t)
    held.waiting_on_person_at = t - 300
    held.approval_stalled = True
    held.approval_stalled_at = t - 100
    created = held.created_ts

    assert await svc.declare_person_wait(SLOT, held=False)
    assert held.created_ts - created == 200.0, "only the span before the approval hold"
    assert held.approval_stalled is True, "the approval hold is not the agent's to end"
    assert await svc.release_approval_hold(SLOT, why="test", arm=False)
    assert held.created_ts - created == 300.0
    await _drain(svc)


@pytest.mark.asyncio
async def test_a_structured_monitor_cannot_declare(svc):
    await svc.add_monitor(
        slot_key=SLOT,
        kind="github_pull_request",
        target="https://github.com/octo/repo/pull/1",
        objective="review_ready",
        cadence_secs=300,
        budgets=MonitorBudgets(),
    )
    assert await svc.declare_person_wait(SLOT, held=True) is False
    svc.stop()


# --------------------------------------------------------------------------- #
# monitor_update(waiting_on_person=...) through the directive applier
# --------------------------------------------------------------------------- #


def _state():
    return SimpleNamespace(_slots={SLOT: SimpleNamespace(workspace="default", is_closing=False)})


def _slot(generation: int = 1):
    return SimpleNamespace(key=SLOT, _app="", _turn_generation=generation, _queue=[])


async def _apply(svc, patch_, slot=None, **producer):
    with (
        patch("kiro_crew.autonudge.get_instance", return_value=svc),
        patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
    ):
        return await apply_session_directive(
            _state(),
            slot if slot is not None else _slot(),
            SESSION,
            "monitor_update",
            {"patch": patch_},
            **producer,
        )


@pytest.mark.asyncio
async def test_a_human_turn_declares_the_wait(svc):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    try:
        result = await _apply(svc, {"waiting_on_person": True}, producer_is_user_facing=True)
        assert "waiting on a person" in result
        assert loop.person_wait_declared is True and loop.waiting_on_person is True

        result = await _apply(svc, {"waiting_on_person": False}, producer_is_user_facing=True)
        assert "no longer waiting" in result
        assert loop.person_wait_declared is False and loop.waiting_on_person is False
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_the_loop_s_own_wake_declares_the_wait(svc):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    try:
        result = await _apply(
            svc,
            {"waiting_on_person": True},
            producer_is_self_wake=True,
            producer_wake_loop_id=loop.id,
        )
        assert "waiting on a person" in result
        assert loop.person_wait_declared is True
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_cron_or_app_turn_may_not_declare(svc):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    try:
        result = await _apply(svc, {"waiting_on_person": True})
        assert loop.person_wait_declared is False
        assert "needs a turn a person sent" in result
        # Ending a wait is open to any producer that may revise the loop.
        assert "no longer waiting" in await _apply(svc, {"waiting_on_person": False})
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_channel_binding_may_not_declare(svc):
    channel_key = "slack:C123:1712793600.123456"
    loop = await svc.add(slot_key=channel_key, message="check", idle_secs=600)
    try:
        with (
            patch("kiro_crew.autonudge.get_instance", return_value=svc),
            patch("kiro_crew.autonudge_authz.sel", return_value=MagicMock()),
        ):
            result = await apply_session_directive(
                SimpleNamespace(_slots={}),
                None,
                channel_key,
                "monitor_update",
                {"patch": {"waiting_on_person": True}},
                producer_is_user_facing=True,
                producer_is_channel=True,
            )
        assert loop.person_wait_declared is False
        assert "dashboard chat" in result
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_structured_monitor_is_refused(svc):
    loop = await svc.add_monitor(
        slot_key=SLOT,
        kind="github_pull_request",
        target="https://github.com/octo/repo/pull/1",
        objective="review_ready",
        cadence_secs=300,
        budgets=MonitorBudgets(),
    )
    try:
        result = await _apply(svc, {"waiting_on_person": True}, producer_is_user_facing=True)
        assert "monitor_start loop" in result
        assert loop.person_wait_declared is False
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_an_inactive_loop_is_refused(svc):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    await svc.update(loop.id, active=False)
    try:
        result = await _apply(svc, {"waiting_on_person": True}, producer_is_user_facing=True)
        assert "PAUSED" in result
        assert loop.person_wait_declared is False and loop.active is False
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_it_combines_with_a_message_edit(svc):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    try:
        result = await _apply(
            svc,
            {"message": "check again once they approve", "waiting_on_person": True},
            producer_is_user_facing=True,
        )
        assert "updated" in result and "waiting on a person" in result
        assert loop.message == "check again once they approve"
        assert loop.person_wait_declared is True
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_ending_a_ledger_hold_reports_it_as_the_ledger_s(svc, monkeypatch):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    assert await firing._commit_person_wait(svc, loop, held=True)
    declare = AsyncMock(wraps=svc.declare_person_wait)
    monkeypatch.setattr(svc, "declare_person_wait", declare)
    try:
        result = await _apply(svc, {"waiting_on_person": False}, producer_is_user_facing=True)
        assert "work ledger" in result and "no longer waiting" not in result
        declare.assert_not_awaited()
        assert loop.waiting_on_person is True and loop.person_wait_declared is False
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_refused_hold_after_a_message_edit_says_the_edit_landed(svc, monkeypatch):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    monkeypatch.setattr(svc, "declare_person_wait", AsyncMock(return_value=False))
    try:
        result = await _apply(
            svc,
            {"message": "check again once they approve", "waiting_on_person": True},
            producer_is_user_facing=True,
        )
        assert "could not be recorded" in result
        assert "other fields were applied" in result and "updated" in result
        assert loop.message == "check again once they approve"
        assert loop.person_wait_declared is False
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_a_person_who_already_answered_is_not_waited_on(svc):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    slot = _slot(generation=4)
    turn_state(slot).human_input_generation = 4  # they sent a message during this turn
    try:
        result = await _apply(
            svc, {"waiting_on_person": True}, slot=slot, producer_is_user_facing=True
        )
        assert "NOT held" in result
        assert loop.person_wait_declared is False

        queued = _slot(generation=5)
        queued._queue = [{"id": "q1", "_directive_user_origin": True}]
        result = await _apply(
            svc, {"waiting_on_person": True}, slot=queued, producer_is_user_facing=True
        )
        assert "NOT held" in result
        assert loop.person_wait_declared is False

        earlier = _slot(generation=6)
        turn_state(earlier).human_input_generation = 5  # the message that started this turn
        result = await _apply(
            svc, {"waiting_on_person": True}, slot=earlier, producer_is_user_facing=True
        )
        assert loop.person_wait_declared is True
    finally:
        svc.stop()


def test_the_tool_puts_the_field_in_the_patch_and_validates_it():
    from kiro_crew import validation

    validated = validation.validate_tool_args(
        {"waiting_on_person": True}, validation.MONITOR_UPDATE_SCHEMA
    )
    assert validated["waiting_on_person"] is True
    with pytest.raises(validation.ValidationError):
        validation.validate_tool_args(
            {"waiting_on_person": "yes"}, validation.MONITOR_UPDATE_SCHEMA
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [True, False])
async def test_a_stop_queued_before_a_declaration_publishes_no_hold(svc, monkeypatch, held):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    if not held:
        assert await svc.declare_person_wait(SLOT, held=True)
    before = (loop.waiting_on_person, loop.person_wait_declared, loop.created_ts)
    writer = AsyncMock(wraps=svc._write_monitor_snapshot_locked)
    monkeypatch.setattr(svc, "_write_monitor_snapshot_locked", writer)
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    tasks: list[asyncio.Task] = []
    try:
        async with svc._lock:
            stop = asyncio.create_task(svc.remove(loop.id))
            tasks.append(stop)
            await until_parked(svc._lock, timeout=30)
            declaration = asyncio.create_task(svc.declare_person_wait(SLOT, held=held))
            tasks.append(declaration)
            await until_parked(svc._lock, count=2, timeout=30)
        stopped, declared = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)
        assert stopped is True and svc.get_by_slot(SLOT) is None
        assert declared is False
        writer.assert_not_awaited()
        assert events == ["removed"], "a deleted loop publishes no updated frame"
        assert (loop.waiting_on_person, loop.person_wait_declared, loop.created_ts) == before
        assert _no_live_timer(svc, loop)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=30)
        svc.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [True, False])
async def test_a_refused_ledger_hold_write_keeps_its_requested_state_unpublished(
    svc, monkeypatch, held
):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    if not held:
        assert await firing._commit_person_wait(svc, loop, held=True)
    loop.ledger_seen_fp = "seen"
    monkeypatch.setattr(svc, "_observes_work_ledger", lambda lp: True)
    monkeypatch.setattr(firing, "_ledger_person_wait_fp", lambda *_a: "seen" if held else "moved")
    writer = AsyncMock(side_effect=OSError("write refused"))
    monkeypatch.setattr(svc, "_write_monitor_snapshot_locked", writer)
    arm = MagicMock()
    monkeypatch.setattr(svc, "_arm_from_deadline", arm)
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    before = (loop.waiting_on_person, loop.waiting_on_person_at, loop.created_ts)
    try:
        assert await svc._holds_for_person_wait(loop) is (not held)
        writer.assert_awaited_once()
        assert events == []
        assert (loop.waiting_on_person, loop.waiting_on_person_at, loop.created_ts) == before
        if held:
            arm.assert_not_called()
        else:
            arm.assert_called_once_with(loop)
    finally:
        svc.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["during-turn", "queued"])
async def test_a_reply_during_the_declaration_write_releases_the_hold(svc, monkeypatch, reply):
    loop = await svc.add(slot_key=SLOT, message="check", idle_secs=600)
    slot = _slot(generation=4)
    write = svc._write_monitor_snapshot_locked
    events: list[bool] = []
    writes = 0
    svc.subscribe(lambda ev, lp: events.append(lp.waiting_on_person))

    async def _write_with_reply(payload):
        nonlocal writes
        writes += 1
        if writes == 1:
            assert loop.waiting_on_person is False
            if reply == "during-turn":
                responsive_chat.notify_user_input({"is_dashboard_user": True}, slot)
                assert turn_state(slot).human_input_generation == slot._turn_generation
            else:
                slot._queue.append({"id": "q1", "_directive_user_origin": True})
                svc.notify_user_input(SLOT, human=True)
            assert not svc._inflight_adds, "the person's release finds no published hold"
        await write(payload)

    monkeypatch.setattr(svc, "_write_monitor_snapshot_locked", _write_with_reply)
    try:
        result = await _apply(
            svc, {"waiting_on_person": True}, slot=slot, producer_is_user_facing=True
        )
        assert writes == 2, "the hold and its release both commit"
        assert events == [True, False]
        assert "NOT held" in result and "already sent" in result
        assert loop.person_wait_declared is False and loop.waiting_on_person is False
        assert loop.waiting_on_person_at == 0.0
    finally:
        svc.stop()
