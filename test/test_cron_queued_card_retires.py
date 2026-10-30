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
from kiro_crew.dashboard.slot_queue_repository import MAX_LIVE_QUEUE_ENTRIES
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


@pytest.mark.asyncio
async def test_script_cron_on_a_full_live_queue_drops_the_oldest_entry_and_card(
    tmp_path,
) -> None:
    # A script that fires faster than its notification turns run must not grow
    # the live queue past its cap: like a cron's send_message, the newest
    # report waits and the oldest one gives way, card included.
    state = _make_state(tmp_path)
    slot = _live_slot(state)
    wire = _Wire(state)
    orchestrator = MagicMock()
    orchestrator.dashboard_state = state
    job = _job("SS captain watch")

    for n in range(MAX_LIVE_QUEUE_ENTRIES):
        await gw.GatewayOrchestrator._deliver_script_result(orchestrator, job, f"report {n}")
    oldest_id = slot._queue[0]["id"]
    await gw.GatewayOrchestrator._deliver_script_result(
        orchestrator, job, f"report {MAX_LIVE_QUEUE_ENTRIES}"
    )

    reports = [entry["content"].splitlines()[1] for entry in slot._queue]
    assert reports == [f"report {n}" for n in range(1, MAX_LIVE_QUEUE_ENTRIES + 1)]
    cards = [m["content"].splitlines()[1] for m in slot.messages if m.get("role") == "queued"]
    assert cards == reports
    # An open dashboard draws cards from frames, so the evicted card is retracted
    # there too rather than lingering as a report that will never run.
    cancelled = [payload for kind, payload in wire.frames if kind == "queue_cancel"]
    assert cancelled == [{"slot": SLOT_KEY, "queue_id": oldest_id}]


@pytest.mark.asyncio
async def test_a_full_live_queue_drops_a_cron_report_before_a_persons_prompt(tmp_path) -> None:
    # A person's prompt held at the head (sub-agents running drain only system
    # entries past it) must not be what a fast script cron pushes out.
    state = _make_state(tmp_path)
    slot = _live_slot(state)
    wire = _Wire(state)
    orchestrator = MagicMock()
    orchestrator.dashboard_state = state
    job = _job("SS captain watch")
    prompt_id = slot.queue_append("my own question", directive_user_origin=True)

    for n in range(MAX_LIVE_QUEUE_ENTRIES - 1):
        await gw.GatewayOrchestrator._deliver_script_result(orchestrator, job, f"report {n}")
    oldest_report_id = slot._queue[1]["id"]
    await gw.GatewayOrchestrator._deliver_script_result(orchestrator, job, "report new")

    assert slot._queue[0]["id"] == prompt_id
    assert len(slot._queue) == MAX_LIVE_QUEUE_ENTRIES
    reports = [entry["content"].splitlines()[1] for entry in slot._queue[1:]]
    assert reports == [f"report {n}" for n in range(1, MAX_LIVE_QUEUE_ENTRIES - 1)] + ["report new"]
    cancelled = [payload for kind, payload in wire.frames if kind == "queue_cancel"]
    assert cancelled == [{"slot": SLOT_KEY, "queue_id": oldest_report_id}]


@pytest.mark.asyncio
async def test_a_live_queue_full_of_other_entries_drops_the_new_cron_report(tmp_path) -> None:
    state = _make_state(tmp_path)
    slot = _live_slot(state)
    wire = _Wire(state)
    orchestrator = MagicMock()
    orchestrator.dashboard_state = state
    held = [
        slot.queue_append(f"question {n}", directive_user_origin=True)
        for n in range(MAX_LIVE_QUEUE_ENTRIES)
    ]

    await gw.GatewayOrchestrator._deliver_script_result(
        orchestrator, _job("SS captain watch"), "report"
    )

    assert [entry["id"] for entry in slot._queue] == held
    assert not [m for m in slot.messages if m.get("role") == "queued"]
    assert not [payload for kind, payload in wire.frames if kind == "queue_cancel"]


@pytest.mark.asyncio
async def test_a_cron_card_a_person_edited_is_not_evicted(tmp_path) -> None:
    # An edit makes the card's words the person's own, though it keeps the cron
    # kind, so the next oldest unedited report gives way instead.
    state = _make_state(tmp_path)
    slot = _live_slot(state)
    wire = _Wire(state)
    orchestrator = MagicMock()
    orchestrator.dashboard_state = state
    job = _job("SS captain watch")
    for n in range(MAX_LIVE_QUEUE_ENTRIES):
        await gw.GatewayOrchestrator._deliver_script_result(orchestrator, job, f"report {n}")
    edited_id = slot._queue[0]["id"]
    assert slot.queue_edit_by_id(edited_id, "do this instead", directive_user_origin=True)
    next_oldest_id = slot._queue[1]["id"]

    await gw.GatewayOrchestrator._deliver_script_result(orchestrator, job, "report new")

    assert slot._queue[0]["id"] == edited_id
    assert next_oldest_id not in [entry["id"] for entry in slot._queue]
    cancelled = [payload for kind, payload in wire.frames if kind == "queue_cancel"]
    assert cancelled == [{"slot": SLOT_KEY, "queue_id": next_oldest_id}]


@pytest.mark.asyncio
async def test_a_script_cron_result_the_full_queue_refuses_goes_out_as_a_notification(
    tmp_path,
) -> None:
    state = _make_state(tmp_path)
    slot = _live_slot(state)
    for n in range(MAX_LIVE_QUEUE_ENTRIES):
        slot.queue_append(f"question {n}", directive_user_origin=True)
    notified: list[tuple[Any, ...]] = []
    state.notify = lambda *args, **kwargs: notified.append(args)
    orchestrator = MagicMock()
    orchestrator.dashboard_state = state

    await gw.GatewayOrchestrator._deliver_script_result(
        orchestrator, _job("SS captain watch"), "watch report"
    )

    assert len(slot._queue) == MAX_LIVE_QUEUE_ENTRIES
    assert [(kind, message) for kind, _title, message in notified] == [("cron", "watch report")]


@pytest.mark.asyncio
async def test_a_send_message_the_full_queue_refuses_takes_the_fallback(tmp_path) -> None:
    from kiro_crew.dashboard.handlers import messaging

    state = _make_state(tmp_path)
    slot = _live_slot(state)
    for n in range(MAX_LIVE_QUEUE_ENTRIES):
        slot.queue_append(f"question {n}", directive_user_origin=True)
    handler_state = MagicMock()
    handler_state.slack_client = None
    handler_state.owner_id = ""
    handler_state.crons.list_jobs.return_value = [_job("SS captain: daily scan")]
    handler_state.get_slot.return_value = slot
    fallbacks: list[None] = []

    async def _fallback(*_args: Any, **_kwargs: Any) -> None:
        fallbacks.append(None)

    app = web.Application()
    app.router.add_post("/api/send-message", api_send_message)
    app["state"] = handler_state
    with (
        patch("kiro_crew.sel.sel", return_value=MagicMock()),
        patch.object(messaging, "_deliver_send_message_fallback", _fallback),
    ):
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/api/send-message",
                json={
                    "text": "watch report",
                    "session": "origin",
                    "caller_session": f"cron:{JOB_ID}",
                },
            )

    assert len(slot._queue) == MAX_LIVE_QUEUE_ENTRIES
    assert fallbacks == [None]
