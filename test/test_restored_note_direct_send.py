"""A note held across a restart reaches the next direct send's turn.

A ``/note`` posted while a turn runs is held, and the queue drain or the cycle
end delivers it. A restart that cuts that turn restores the hold into an idle
slot, where neither seam runs before the next send. The direct ``POST /api/chat``
path flushes such a hold above its own user row, so the note's line comes first
and its context half is in ``_pending_context`` when the turn builds its prompt.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

_SLOT = "restored-note"


def _held(slot, note_id: str, text: str) -> None:
    slot._deferred_notes.append(
        {
            "id": note_id,
            "content": text,
            "cls": "reconcile-note",
            "context": {"source": "note", "content": text},
            "session": None,
        }
    )


@pytest.fixture
def idle_state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.get_or_create_slot(_SLOT)
    seen: list[list[dict]] = []

    async def run_chat(st, slot, message, **_kwargs):
        seen.append([dict(entry) for entry in slot._pending_context])
        slot.append("done", "", "done", broadcast=False)

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", run_chat)
    return state, seen


async def _send(state, message: str) -> int:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat?ws=1", json={"slot": _SLOT, "message": message})
        slot = state.get_slot(_SLOT)
        if resp.status == 200 and slot.task is not None:
            await slot.task
        return resp.status


@pytest.mark.asyncio
async def test_a_restored_hold_lands_above_the_next_sends_row(idle_state):
    state, seen = idle_state
    slot = state.get_slot(_SLOT)
    _held(slot, "n1", "deploy finished")

    assert await _send(state, "anything new?") == 200

    roles = [(m["role"], m["content"]) for m in slot.messages if m["role"] in ("inject", "user")]
    assert roles == [("inject", "deploy finished"), ("user", "anything new?")]
    assert slot._deferred_notes == []
    [context] = seen
    assert [entry.get("content") for entry in context] == ["deploy finished"]


@pytest.mark.asyncio
async def test_an_sse_reader_receives_the_restored_note_frame(idle_state):
    # An SSE caller reads `_pending`, which the send's stale-frame drain empties;
    # the note's frame must survive that drain, while a frame left over from an
    # earlier turn still does not.
    state, _seen = idle_state
    slot = state.get_slot(_SLOT)
    slot.append("assistant", "stale frame from before the restart", "msg msg-a")
    _held(slot, "n1", "deploy finished")

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat", json={"slot": _SLOT, "message": "anything new?"})
        body = await asyncio.wait_for(resp.read(), 60)

    assert resp.status == 200
    assert body.count(b"deploy finished") == 1
    assert b"stale frame from before the restart" not in body


@pytest.mark.asyncio
async def test_a_send_with_no_hold_writes_only_its_own_row(idle_state):
    state, seen = idle_state
    slot = state.get_slot(_SLOT)

    assert await _send(state, "hello") == 200

    assert [m["role"] for m in slot.messages if m["role"] in ("inject", "user")] == ["user"]
    assert seen == [[]]
