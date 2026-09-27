"""``_finish_queue_cycle``'s terminal order is a dashboard-frontend contract.

The dashboard finalizes a turn when the live ``slots`` frame reports the active
slot ``running: false`` (``website/src/store/chatSlice.ts``
``settleEndedActiveTurn``): the streaming row is frozen into an assistant bubble
and the composer goes idle. That works on an ordinary turn only because the
server's terminal step, the non-synthesis branch of ``chat_runner.py``
``_finish_queue_cycle``, runs in ONE order:

1. every content frame of the turn has already been emitted (``_run_chat``
   flushes chunks, segments and tool rows before its ``finally`` reaches here);
2. ``slot.append("done", ...)``;
3. ``slot.task = None`` -- so ``slot.turn_running`` is False and the projected
   row (``slot_projection.py`` ``"running": slot.turn_running``) reads
   ``running: false``;
4. ``state.push_slots_update()`` -- the frame the frontend settles on;
5. exactly one ``chat_done`` broadcast.

A later backend change that pushed a slots update across a momentary
``task = None`` gap, or reordered this sequence, would finalize the streaming row
mid-turn on EVERY turn (split replies, a prematurely idle composer) with nothing
server-side going red. These tests are that red. The frontend comments in
``chatSlice.ts`` cite this file by name.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard import chat_utils as cu
from kiro_crew.dashboard.chat_utils import chunk_generation
from kiro_crew.dashboard.slot_projection import SlotProjection

_FRONTEND = "the dashboard frontend depends on this (chatSlice settleEndedActiveTurn)"


@pytest.fixture(autouse=True)
def _no_cycle_background_tasks(monkeypatch):
    """Neutralize ``_finish_queue_cycle``'s detached tasks (title refresh,
    session summary): both hop to executor threads (config load, transcript
    flush) that would outlive the test and can re-create the per-test dir
    after teardown."""
    monkeypatch.setattr(cr, "title_then_refresh", AsyncMock())
    monkeypatch.setattr(cr, "generate_session_summary", AsyncMock())


def _live_task():
    """Stand-in for the ``_run_chat`` task that is still executing when its own
    ``finally`` reaches ``_finish_queue_cycle``: not done, so ``turn_running``
    reads True until the cycle clears ``slot.task``."""
    task = MagicMock()
    task.done.return_value = False
    return task


def _recorded_cycle(tmp_path, monkeypatch, *, subagents=None):
    """A real slot inside a real state, with every terminal-order participant
    recording into ONE ordered event list.

    ``push_slots_update`` snapshots what the projected row would say at the
    moment it is called -- ``slot.task``, ``slot.turn_running`` and the real
    ``slot.to_dict()["running"]`` -- because that snapshot IS what the frontend
    receives in the ``slots`` frame.
    """
    state = _make_state(tmp_path)
    state.subagents = subagents
    slot = state.get_or_create_slot("chat-terminal-order")
    slot._titled = True  # keep the cycle off the real auto-title path
    slot.task = _live_task()
    events: list[tuple] = []

    real_append = type(slot).append

    def _recording_append(self, role, content, cls="", *args, **kwargs):
        if self is slot:
            events.append(("append", role))
        return real_append(self, role, content, cls, *args, **kwargs)

    monkeypatch.setattr(type(slot), "append", _recording_append)

    def _recording_push():
        projected = slot.to_dict()
        events.append(
            (
                "push_slots_update",
                {
                    "task_is_none": slot.task is None,
                    "turn_running": slot.turn_running,
                    "projected_running": projected["running"],
                    "turn": projected["turn"],
                    "turn_gen": projected["turn_gen"],
                },
            )
        )

    state.push_slots_update = _recording_push  # type: ignore[method-assign]
    state.broadcast_ws = MagicMock(
        side_effect=lambda msg_type, data: events.append(("broadcast_ws", msg_type, data))
    )
    return state, slot, events


async def _drain_background(state):
    tasks = list(state._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)


# ── The ordinary (non-synthesis) cycle ──────────────────────────────────────


@pytest.mark.asyncio
async def test_ordinary_cycle_emits_done_then_idle_slots_then_one_chat_done(tmp_path, monkeypatch):
    """append("done") -> slot.task = None -> push_slots_update -> chat_done,
    with the slots push carrying ``running: false`` -- the exact sequence the
    frontend's settle-on-idle-slot finalize is built on."""
    state, slot, events = _recorded_cycle(tmp_path, monkeypatch)
    # The fake live task really does make the projection report a running
    # turn, so the False asserted below is a state change, not a default.
    assert slot.turn_running is True
    assert slot.to_dict()["running"] is True

    await cr._finish_queue_cycle(state, slot)
    await _drain_background(state)

    kinds = [kind for kind, *_ in events]
    dones = [i for i, event in enumerate(events) if event == ("append", "done")]
    pushes = [i for i, (kind, *_) in enumerate(events) if kind == "push_slots_update"]
    chat_dones = [
        i
        for i, event in enumerate(events)
        if event[0] == "broadcast_ws" and event[1] == "chat_done"
    ]

    assert len(dones) == 1, f"one terminal done row per cycle; {_FRONTEND}: {kinds}"
    assert pushes, f"the cycle must push a slots frame; {_FRONTEND}: {kinds}"
    assert len(chat_dones) == 1, f"exactly one chat_done per cycle; {_FRONTEND}: {kinds}"

    done_at, first_push, chat_done_at = dones[0], pushes[0], chat_dones[0]
    assert done_at < first_push, f"append('done') must precede the slots push; {_FRONTEND}: {kinds}"
    assert (
        first_push < chat_done_at
    ), f"push_slots_update must precede the chat_done broadcast; {_FRONTEND}: {kinds}"

    snapshot = events[first_push][1]
    assert (
        snapshot["task_is_none"] is True
    ), f"slot.task must be None when the slots frame is pushed; {_FRONTEND}: {snapshot}"
    assert (
        snapshot["turn_running"] is False
    ), f"slot.turn_running must be False when the slots frame is pushed; {_FRONTEND}: {snapshot}"
    assert (
        snapshot["projected_running"] is False
    ), f"the projected slots row must read running=false at the push; {_FRONTEND}: {snapshot}"
    done_payload = events[chat_done_at][2]
    assert snapshot["turn"] == slot._turn_generation
    assert done_payload["turn"] == snapshot["turn"]
    assert snapshot["turn_gen"] == chunk_generation()
    assert done_payload["turn_gen"] == snapshot["turn_gen"]
    # Every slots frame the cycle pushes after the done row reports the turn
    # over: a running=true frame AFTER the idle one would flip the composer
    # back to busy with no turn left to end it.
    assert all(
        events[i][1]["projected_running"] is False for i in pushes
    ), f"no post-done slots frame may report running=true; {_FRONTEND}: {events}"
    # Nothing content-bearing follows the idle frame: the frontend has frozen
    # the streaming row by then, so a late chunk/segment/row would render as a
    # split reply.
    content_after_push = [
        events[i]
        for i in range(first_push + 1, len(events))
        if events[i][0] == "append"
        or (events[i][0] == "broadcast_ws" and events[i][1] in {"chat_chunk", "chat_segment"})
    ]
    assert (
        content_after_push == []
    ), f"no content frame may follow the idle slots push; {_FRONTEND}: {content_after_push}"


# ── The synthesis branch never shows the frontend an idle slot ──────────────


@pytest.mark.asyncio
async def test_synthesis_dispatch_keeps_slot_running_and_emits_no_chat_done(tmp_path, monkeypatch):
    """When a synthesis successor is dispatched, the cycle's slots push carries
    the LIVE synthesis task (``running: true``) and no ``chat_done`` follows:
    the frontend must not finalize at this boundary, because the synthesis
    turn's text is about to stream into the same session."""
    parked = asyncio.Event()

    async def _parked_synthesis(_state, _slot):
        await parked.wait()

    monkeypatch.setattr(cr, "_run_pending_synthesis", _parked_synthesis)
    subagents = MagicMock(running_agents_for=MagicMock(return_value=[]))
    state, slot, events = _recorded_cycle(tmp_path, monkeypatch, subagents=subagents)
    slot._pending_synthesis = True
    state._slots[slot.key] = slot

    try:
        await cr._finish_queue_cycle(state, slot)

        kinds = [kind for kind, *_ in events]
        no_done = f"a synthesis dispatch is not the turn's end; {_FRONTEND}: {kinds}"
        assert ("append", "done") not in events, no_done
        no_chat_done = f"no chat_done before the synthesis turn runs; {_FRONTEND}: {kinds}"
        assert not any(
            event[0] == "broadcast_ws" and event[1] == "chat_done" for event in events
        ), no_chat_done
        pushes = [event for event in events if event[0] == "push_slots_update"]
        assert pushes, f"the dispatch pushes the new live task; {_FRONTEND}: {kinds}"
        assert all(snapshot["projected_running"] is True for _, snapshot in pushes), (
            f"the slots frame must keep running=true across a synthesis dispatch; "
            f"{_FRONTEND}: {pushes}"
        )
        assert slot.task is not None and not slot.task.done()
    finally:
        parked.set()
        await _drain_background(state)


# ── The projection column the frontend reads is ``turn_running`` ────────────


def _projected_row(*, turn_running: bool, task, admission_running: bool) -> dict:
    """Run the real projection over a minimal fake slot."""
    slot = MagicMock()
    slot.messages = []
    slot._approval_futures = {}
    slot._queue = []
    slot._question_pending = None
    slot._turn_generation = 7
    slot.turn_running = turn_running
    slot.running = admission_running
    slot.task = task
    row = SlotProjection.to_dict(
        slot,
        include_check_status=False,
        source_links=[],
        prompt_roles=frozenset(),
        redact=str,
        parse_options=lambda text: [],
        strip_options=lambda text: text,
        parse_cls_meta=lambda cls: None,
        is_turn_interrupted=lambda messages: False,
        is_system_notice=lambda role, meta: False,
        latest_transcript_ts=lambda *stamps: "",
        strip_markdown_preview=lambda text: text,
        resolve_effective_agent=lambda agent, project: agent,
        budget_source_links=lambda links: links,
        project_source_links=lambda links, include_check_status: links,
    )
    return row


def test_projected_running_is_turn_running_not_task_or_admission_running():
    """``slots[].running`` mirrors ``slot.turn_running`` alone. It must not be
    re-derived from ``slot.task`` (``task is not None`` reads a finished but
    not-yet-cleared task as running) or from the admission predicate
    ``slot.running`` (true across a stage boundary), or ``_finish_queue_cycle``'s
    ``slot.task = None`` would stop producing the ``running: false`` frame the
    frontend settles on."""
    assert (
        _projected_row(turn_running=False, task=_live_task(), admission_running=True)["running"]
        is False
    ), f"running must follow turn_running, not task/running; {_FRONTEND}"
    assert (
        _projected_row(turn_running=True, task=None, admission_running=False)["running"] is True
    ), f"running must follow turn_running, not task/running; {_FRONTEND}"


def test_projected_row_carries_turn_identity():
    """The idle slots row identifies which gateway turn it ends."""
    row = _projected_row(turn_running=False, task=None, admission_running=False)
    assert row["turn"] == 7
    assert row["turn_gen"] == chunk_generation()


@pytest.mark.asyncio
async def test_slot_detail_history_carries_turn_identity(tmp_path):
    """History and live frames identify turns in the same wire vocabulary."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-history-turn")
    slot.task = _live_task()
    expected_turn = slot._turn_generation
    slot.task = None

    async with TestClient(TestServer(_make_app(state))) as client:
        response = await client.get("/api/chat/slots/chat-history-turn")
        assert response.status == 200
        body = await response.json()

    assert body["turn"] == expected_turn
    assert body["turn_gen"] == chunk_generation()


@pytest.mark.asyncio
async def test_chat_done_identity_is_captured_before_async_probe(tmp_path, monkeypatch):
    """A successor starting during the probe cannot claim the prior done frame."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-done-snapshot")
    slot.task = _live_task()
    ended_turn = slot._turn_generation
    slot.task = None

    async def _start_successor(*_args, **_kwargs):
        slot.task = _live_task()
        return False

    monkeypatch.setattr(cu, "subagents_attached_async", _start_successor)
    payload = await cu.chat_done_payload(state, slot)

    assert slot._turn_generation == ended_turn + 1
    assert payload["turn"] == ended_turn
    assert payload["turn_gen"] == chunk_generation()


@pytest.mark.asyncio
async def test_a_frame_that_does_not_end_the_turn_carries_no_identity(tmp_path):
    """The dashboard records a frame's ``turn`` as ended; a mid-turn frame must
    not name the live turn, or later history replies would read it idle."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-done-mid-turn")
    slot.task = _live_task()

    payload = await cu.chat_done_payload(state, slot, continuing=True, ends_turn=False)

    assert "turn" not in payload
    assert "turn_gen" not in payload
    assert payload["continuing"] is True


def test_the_deferred_compact_acknowledgement_does_not_end_the_turn():
    """The deferred ``/compact`` arm keeps streaming under the same turn after
    its ``chat_done``, so it is the one caller that must pass ``ends_turn=False``."""
    source = inspect.getsource(cr)
    assert source.count("ends_turn=False") == 1
    arm = source[source.index('if first_word == "/compact" and not saw_compaction:') :]
    assert arm.index("ends_turn=False") < arm.index("wait_for_compaction")
