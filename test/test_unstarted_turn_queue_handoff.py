"""A dispatch that ends without running a turn still hands its slot's queue off.

``_run_chat`` hands the queue off on every exit (``_hands_off_queue_on_exit``),
but two turn holders can end without entering it while they hold ``slot.task``,
so a send arriving meanwhile queues behind them:

* a dashboard monitor wake refused at its pre-runner authorization
  (``nudge_dashboard_fire._run_dashboard_turn``);
* an unattended turn that waits out ``DashboardState._BACKGROUND_QUEUE_WAIT_SECS``
  for a background-turn permit (``DashboardState.run_background_turn``).

Each exit marks its turn unstarted (``turn_dispatch.mark_turn_unstarted``), and
``spawn_guarded_turn``'s done callback hands the queue off in its place
(``chat_runner._hand_off_unstarted_turn``). Without that, the entry waits for the
next send -- on an app-owned slot with no other sender, indefinitely -- and the
cycle never sends ``chat_done``. The chat-send test also fails while the abandoned
wait is reported as a turn-ceiling timeout.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_mode_security_helpers import (  # noqa: F401 -- fixtures are requested by name
    _app_client,
    _hermetic_home,
    _isolate_safety_override,
    state_fixture,
)

from kiro_crew.autonudge import NudgeLoop
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_delivery import queue_for_next_turn
from kiro_crew.dashboard.turn_dispatch import (
    TURN_TIMEOUT_KIND,
    mark_turn_unstarted,
    spawn_guarded_turn,
)
from kiro_crew.monitoring.models import MonitorState
from kiro_crew.slack import gateway as gw
from kiro_crew.testing.wait import async_wait_until, await_until

_GRANT_CHECK = "kiro_crew.apps.permissions.app_can_manage_session_approvals"


class _TurnRecorder:
    """Stands in for ``chat_runner._run_chat`` for the turns the hand-off starts."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.ran = asyncio.Event()

    async def __call__(self, _state, _slot, message: str, **_kwargs) -> None:
        self.messages.append(message)
        self.ran.set()


def _structured_wake(slot_key: str) -> NudgeLoop:
    loop = NudgeLoop(
        id="loop-abc",
        slot_key=slot_key,
        message="check the PR",
        idle_secs=300,
        max_cycles=24,
        cycle_count=3,
    )
    loop.monitor = MonitorState(
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        created_ts=1_000.0,
        last_wake_fingerprint="failure-a",
        wake_in_flight=True,
    )
    return loop


def _orchestrator(state) -> gw.GatewayOrchestrator:
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        orch = gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)
    orch.dashboard_state = state
    orch.autonudge_svc = MagicMock()
    orch.autonudge_svc.remove = AsyncMock()
    orch._session_tasks = {}
    return orch


def _chat_done_frames(state, slot_key: str) -> list[dict]:
    return [
        call.args[1]
        for call in state.broadcast_ws.call_args_list
        if call.args[0] == "chat_done" and call.args[1].get("slot") == slot_key
    ]


@pytest.mark.asyncio
async def test_a_wake_refused_before_its_runner_hands_off_the_queued_send(
    state, monkeypatch, caplog
) -> None:
    slot = state.get_or_create_slot("chat-1-1785")
    orch = _orchestrator(state)
    successor = _TurnRecorder()
    monkeypatch.setattr(chat_runner, "_run_chat", successor)

    async def _a_send_queues_then_the_claim_is_revoked(_monitor_id, _fingerprint) -> bool:
        # The wake's task holds the slot while it authorizes, so a person's send
        # takes the queue path, as the composer's busy branch does.
        assert slot.running is True
        queue_for_next_turn(state, slot, "queued while the wake waited", directive_user_origin=True)
        return False

    orch.autonudge_svc.monitor_dispatch_is_authorized = AsyncMock(
        side_effect=_a_send_queues_then_the_claim_is_revoked
    )
    wake_runner = AsyncMock()
    with (
        caplog.at_level(logging.INFO, logger=gw.logger.name),
        patch("kiro_crew.dashboard.chat._run_chat", new=wake_runner),
    ):
        result = await orch._fire_dashboard_nudge(_structured_wake(slot.key), "[Monitor wake]")
        await await_until(successor.ran, what="the queued send's turn")

    assert result is gw.MonitorDispatchResult.UNAVAILABLE
    wake_runner.assert_not_called()
    assert successor.messages == ["queued while the wake waited"]
    assert slot._queue == []
    refusals = [
        r.getMessage() for r in caplog.records if "dashboard wake refused" in r.getMessage()
    ]
    assert refusals == [
        f"AutoNudge: loop loop-abc dashboard wake refused on slot {slot.key}: "
        "its wake claim was revoked"
    ]


@pytest.mark.asyncio
async def test_a_wake_refused_for_a_replaced_slot_logs_why(state, monkeypatch, caplog) -> None:
    slot = state.get_or_create_slot("chat-1-1785")
    orch = _orchestrator(state)
    orch.autonudge_svc.monitor_dispatch_is_authorized = AsyncMock(return_value=True)
    resolve = state.get_slot
    replacement = MagicMock()
    # The tab is replaced once the wake is dispatched, before its authorization.
    monkeypatch.setattr(
        state, "get_slot", lambda key: replacement if slot.task is not None else resolve(key)
    )
    with (
        caplog.at_level(logging.INFO, logger=gw.logger.name),
        patch("kiro_crew.dashboard.chat._run_chat", new=AsyncMock()) as wake_runner,
    ):
        result = await orch._fire_dashboard_nudge(_structured_wake(slot.key), "[Monitor wake]")

    assert result is gw.MonitorDispatchResult.UNAVAILABLE
    wake_runner.assert_not_called()
    orch.autonudge_svc.monitor_dispatch_is_authorized.assert_not_awaited()
    refusals = [
        r.getMessage() for r in caplog.records if "dashboard wake refused" in r.getMessage()
    ]
    assert refusals == [
        f"AutoNudge: loop loop-abc dashboard wake refused on slot {slot.key}: "
        "its slot was replaced"
    ]


@pytest.mark.asyncio
async def test_a_turn_that_never_gets_a_permit_hands_off_the_queued_send(
    state, monkeypatch
) -> None:
    state.effective_max_background_turns = lambda: 1  # type: ignore[method-assign]
    state._BACKGROUND_QUEUE_WAIT_SECS = 0.05  # type: ignore[misc]
    busy = state.get_or_create_slot("w1", app="crew-keyboard")
    slot = state.get_or_create_slot("w2", app="crew-keyboard")
    assert slot.unattended is True
    successor = _TurnRecorder()
    monkeypatch.setattr(chat_runner, "_run_chat", successor)

    holding = asyncio.Event()
    release = asyncio.Event()

    async def _hold_the_only_permit() -> None:
        holding.set()
        await release.wait()

    held = asyncio.ensure_future(state.run_background_turn(busy, _hold_the_only_permit()))
    try:
        await await_until(holding, what="the turn holding the only permit")
        # The dispatch the composer, the hidden gateway turn and a plain nudge
        # loop share: the cap's wait runs inside the guarded turn.
        slot.task = spawn_guarded_turn(
            state,
            slot,
            state.run_background_turn(slot, chat_runner._run_chat(state, slot, "abandoned")),
        )
        assert slot.running is True
        queue_for_next_turn(state, slot, "queued behind the cap", turn_actor="app")

        await await_until(successor.ran, what="the queued send's turn")
    finally:
        release.set()
        await await_until(held, what="the permit holder")

    assert successor.messages == ["queued behind the cap"]
    assert slot._queue == []


@pytest.mark.asyncio
async def test_a_chat_send_abandoned_at_the_cap_is_not_a_turn_timeout(state, monkeypatch) -> None:
    state.effective_max_background_turns = lambda: 1  # type: ignore[method-assign]
    state._BACKGROUND_QUEUE_WAIT_SECS = 0.05  # type: ignore[misc]
    busy = state.get_or_create_slot("w1", app="crew-keyboard")
    slot = state.get_or_create_slot("s1", app="crew-keyboard")
    holding = asyncio.Event()
    release = asyncio.Event()

    async def _hold_the_only_permit() -> None:
        holding.set()
        await release.wait()

    held = asyncio.ensure_future(state.run_background_turn(busy, _hold_the_only_permit()))
    send_runner = AsyncMock()
    try:
        await await_until(holding, what="the turn holding the only permit")
        with (
            patch(_GRANT_CHECK, return_value=True),
            patch("kiro_crew.dashboard.chat_handlers._run_chat", new=send_runner),
            patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()),
        ):
            async with _app_client(state) as client:
                resp = await client.post("/api/chat?ws=1", json={"slot": "s1", "message": "hi"})
                assert resp.status == 200
            await async_wait_until(
                lambda: bool(_chat_done_frames(state, slot.key)),
                describe=lambda: f"the cycle's chat_done: {slot.messages!r}",
            )
    finally:
        release.set()
        await await_until(held, what="the permit holder")

    errors = [m for m in slot.messages if m.get("role") == "error"]
    assert not any((m.get("meta") or {}).get("kind") == TURN_TIMEOUT_KIND for m in errors)
    assert [m["content"] for m in errors if "never started" in m["content"]], errors
    assert slot.task is None
    assert any(m.get("role") == "done" for m in slot.messages)


def _ended_holder(state, slot, *, unstarted: bool = True) -> asyncio.Future:
    holder = asyncio.get_running_loop().create_future()
    holder.set_result(None)
    slot.task = holder  # type: ignore[assignment]
    if unstarted:
        mark_turn_unstarted(slot)
    state._slots[slot.key] = slot
    return holder


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["unmarked", "send_reserved", "slot_replaced", "slot_closing", "remote", "successor"],
)
async def test_the_hand_off_is_owed_only_to_a_holder_that_never_started(
    state, monkeypatch, case: str
) -> None:
    slot = state.get_or_create_slot("chat-gate")
    holder = _ended_holder(state, slot, unstarted=case != "unmarked")
    slot.queue_append("queued")
    hand_off = AsyncMock()
    monkeypatch.setattr(chat_runner, "_hand_off_queue", hand_off)
    if case == "send_reserved":
        slot._turn_admission_reserved = True
    elif case == "slot_replaced":
        state._slots[slot.key] = MagicMock()
    elif case == "slot_closing":
        slot.begin_close()
    elif case == "remote":
        slot.executor = "remote"
    elif case == "successor":
        slot.task = asyncio.get_running_loop().create_future()  # type: ignore[assignment]

    before = slot.task
    tasks_before = set(state._background_tasks)
    chat_runner._hand_off_unstarted_turn(state, slot, holder)  # type: ignore[arg-type]

    # The hand-off takes the floor synchronously when it is owed, so an
    # unchanged task and no new background task is the whole answer.
    assert slot.task is before
    assert set(state._background_tasks) == tasks_before


@pytest.mark.asyncio
async def test_the_hand_off_takes_the_floor_before_it_yields(state, monkeypatch) -> None:
    """A send arriving before the hand-off task runs queues behind it, never beside it."""
    slot = state.get_or_create_slot("chat-floor")
    holder = _ended_holder(state, slot)
    proceed = asyncio.Event()
    handed_off = asyncio.Event()
    drains: list[bool] = []

    async def _hand_off(_state, s, **kwargs) -> None:
        await proceed.wait()
        drains.append(kwargs["drain"])
        s.task = None
        handed_off.set()

    monkeypatch.setattr(chat_runner, "_hand_off_queue", _hand_off)
    chat_runner._hand_off_unstarted_turn(state, slot, holder)  # type: ignore[arg-type]

    assert slot.task is not holder
    assert slot.running is True
    proceed.set()
    await await_until(handed_off, what="the hand-off")
    assert drains == [True]
