"""A real dashboard turn reaches the session release with the starter its entry declared.

``SessionManager.release`` stamps ``last_turn_automated`` from the running turn's declared
trigger, so the stamp is only right if ``_run_chat``'s entry (``set_current_trigger``) and its
release run in one context. These drive the real ``_run_chat`` and read
``turn_was_automated()`` at the moment the release is called.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from slack_gateway_coverage_helpers import _loop, _slack_nudge_orchestrator

import kiro_crew.slack.gateway as gw
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.messaging import turn_ceiling
from kiro_crew.messaging.channel_fair_admission import run_admitted
from kiro_crew.session_idle_clock import turn_was_automated
from kiro_crew.shared_crew import turn_gate


def _state_and_slot(tmp_path, seen: list[bool]):
    state = _make_state(tmp_path)
    client = MagicMock()
    client.session_id = "acp-automated-idle"
    client.shutdown = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=10.0)

    async def _stream(*_a, **_kw):
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="Done.")
        yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock(
        side_effect=lambda *_a, **_kw: seen.append(turn_was_automated())
    )
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.compaction_due_after_turn = MagicMock(return_value=False)
    state.sessions.effective_autocompact_pct = MagicMock(return_value=70.0)
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.record_failure = AsyncMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = state.get_or_create_slot("automated-idle-slot")
    slot.append("user", "the message", "msg msg-u")
    return state, slot


async def _released_as(tmp_path, **turn_kwargs) -> list[bool]:
    seen: list[bool] = []
    state, slot = _state_and_slot(tmp_path, seen)
    # Its own task, as a dispatched turn runs, so one turn's trigger cannot reach the next.
    await asyncio.wait_for(
        asyncio.ensure_future(_run_chat(state, slot, "the message", **turn_kwargs)), 60
    )
    return seen


@pytest.mark.asyncio
async def test_a_persons_dashboard_send_releases_as_a_person(tmp_path):
    seen = await _released_as(tmp_path, _directive_user_origin=True)

    assert seen, "the turn never released its session"
    assert seen == [False] * len(seen)


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["cron", "subagent", "autonudge", "app"])
async def test_an_automated_dashboard_turn_releases_as_automation(tmp_path, actor):
    seen = await _released_as(tmp_path, _directive_user_origin=False, _turn_actor=actor)

    assert seen, "the turn never released its session"
    assert seen == [True] * len(seen)


@pytest.mark.asyncio
async def test_a_slack_loop_wake_releases_as_automation(monkeypatch):
    seen: list[bool] = []
    orch = _slack_nudge_orchestrator()
    orch.sessions.release = MagicMock(
        side_effect=lambda *_a, **_kw: seen.append(turn_was_automated())
    )
    monkeypatch.setattr(gw, "stream_and_collect", AsyncMock(return_value="done"))

    await asyncio.wait_for(orch._fire_slack_nudge(_loop(message="check the queue")), 60)

    assert seen, "the nudge never released its session"
    assert seen == [True] * len(seen)
    assert turn_was_automated() is False, "the loop's caller inherited the declared trigger"


@pytest.mark.asyncio
@pytest.mark.parametrize("wake_kwargs", [{}, {"monitor": MagicMock()}], ids=["plain", "monitor"])
async def test_a_dm_nudge_releases_as_automation_and_a_drained_message_as_a_person(
    wake_kwargs, monkeypatch
):
    # Discord's and Webex's plain nudge carries no monitor: only ``generated_turn`` marks it.
    # A monitor wake never enters the ceiling gate, so it leaves that mark unspent.
    seen: list[bool] = []
    charged: list[bool] = []
    monkeypatch.setattr(
        turn_gate,
        "record_channel_message",
        lambda *_a, automation, **_kw: charged.append(automation),
    )

    async def _run(*_a, **kw):
        seen.append(turn_was_automated())
        if "monitor" not in kw:
            turn_ceiling.gate("discord:a:direct:u")()

    turns = SimpleNamespace(channel="discord", _outcome=MagicMock(), _run=_run)
    asker = SimpleNamespace(session_key="discord:a:direct:u")

    async def _nudge_then_drain():
        with turn_ceiling.generated_turn():
            await run_admitted(turns, asker, "nudge", MagicMock(), **wake_kwargs)
            await run_admitted(turns, asker, "queued by a person", MagicMock())

    await asyncio.wait_for(asyncio.ensure_future(_nudge_then_drain()), 60)

    assert seen == [True, False]
    # Admission charges a plain nudge as it does with the key off: only a monitor wake is automation.
    assert charged == ["monitor" in wake_kwargs, False]
