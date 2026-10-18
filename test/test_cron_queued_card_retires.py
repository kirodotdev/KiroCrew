"""A cron notification queued behind a live turn draws a card its drain takes down.

Two injectors queue a cron notification that reaches its origin slot while a turn
runs there, and each draws a ``queued`` card for the waiting entry: a cron's
``send_message`` to its origin session (``handlers.messaging.api_send_message``)
and a script cron's result (``GatewayOrchestrator._deliver_script_result``). The
dashboard takes a card down when the drain's ``queue_pop`` frame names the id the
card carries in ``meta.queueId`` (``website/src/store/chat/queue.ts``,
``removeQueuedMessage``). Each test drives one injector into a live slot, ends the
turn, runs the drain and compares the two ids.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.cron import CronJob
from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard.handlers import api_send_message
from kiro_crew.slack import gateway as gw

SLOT_KEY = "chat-7-origin"
JOB_ID = "job4242"


class _Wire:
    """What the dashboard's sockets receive for the slot: row notes and frames."""

    def __init__(self, state: Any) -> None:
        self.notes: list[dict[str, Any]] = []
        self.frames: list[tuple[str, Any]] = []
        state._broadcast = self.notes.append
        state.broadcast_ws = lambda kind, payload=None: self.frames.append((kind, payload))

    def card_queue_id(self) -> Any:
        cards = [
            note
            for note in self.notes
            if note.get("_type") == "chat_message" and note.get("role") == "queued"
        ]
        assert len(cards) == 1, cards
        return (cards[0].get("meta") or {}).get("queueId")

    def popped_queue_ids(self) -> list[Any]:
        return [payload["queue_id"] for kind, payload in self.frames if kind == "queue_pop"]


def _job(name: str) -> CronJob:
    return CronJob(
        id=JOB_ID, name=name, message="", schedule=None, session_key=f"dashboard:{SLOT_KEY}"
    )


def _live_slot(state: Any) -> Any:
    """The origin slot with a turn running in it, so a delivery waits in its queue."""
    slot = state.get_or_create_slot(SLOT_KEY)
    slot.task = asyncio.get_running_loop().create_future()
    assert slot.running
    return slot


async def _drain(state: Any, slot: Any) -> int:
    """End the running turn and run the drain; return how many turns it started."""
    started: list[None] = []

    def _record(_state: Any, _slot: Any, coro: Any, **_kwargs: Any) -> asyncio.Future[None]:
        coro.close()
        started.append(None)
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        done.set_result(None)
        return done

    slot.task.cancel()
    slot.task = None
    with patch.object(cr, "spawn_guarded_turn", _record):
        assert await cr._start_next_queued_turn(state, slot)
    return len(started)


@pytest.mark.asyncio
async def test_origin_send_card_is_the_entry_the_drain_pops(tmp_path) -> None:
    state = _make_state(tmp_path)
    slot = _live_slot(state)
    wire = _Wire(state)
    # The handler's own collaborators, built the way test_cron_origin_injection
    # builds them; the slot it resolves is the real one above.
    handler_state = MagicMock()
    handler_state.slack_client = None
    handler_state.owner_id = ""
    handler_state.crons.list_jobs.return_value = [_job("SS captain: daily scan")]
    handler_state.get_slot.return_value = slot
    app = web.Application()
    app.router.add_post("/api/send-message", api_send_message)
    app["state"] = handler_state
    with patch("kiro_crew.sel.sel", return_value=MagicMock()):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={
                    "text": "watch report",
                    "session": "origin",
                    "caller_session": f"cron:{JOB_ID}",
                },
            )
            assert (await resp.json())["session"] is True

    assert [entry["kind"] for entry in slot._queue] == ["cron_notification"]
    assert await _drain(state, slot) == 1
    assert wire.popped_queue_ids() == [wire.card_queue_id()]


@pytest.mark.asyncio
async def test_script_cron_card_is_the_entry_the_drain_pops(tmp_path) -> None:
    state = _make_state(tmp_path)
    slot = _live_slot(state)
    wire = _Wire(state)
    orchestrator = MagicMock()
    orchestrator.dashboard_state = state

    await gw.GatewayOrchestrator._deliver_script_result(
        orchestrator, _job("SS captain watch"), "watch report"
    )

    assert [entry["kind"] for entry in slot._queue] == ["cron_notification"]
    assert await _drain(state, slot) == 1
    assert wire.popped_queue_ids() == [wire.card_queue_id()]
