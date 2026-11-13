"""A note held across a restart reaches a Regenerate, Edit & Resend or rewind turn.

A restart that cuts the turn a ``/note`` was held behind restores the hold into an
idle slot. These three turns start from a history action rather than a send, and
Edit & Resend and rewind build their user row on a shallow copy of the slot that
shares the hold. Each flushes the hold on the live slot first, so the note's line
sits above the row the turn answers (above the new reply, for Regenerate) and its
context is pending when the turn builds its prompt. A refused edit leaves the
note delivered, never lost.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_regenerate_cov80_helpers import state_fixture  # noqa: F401
from chat_regenerate_cov80_helpers import _client
from chat_test_helpers import _make_app

_SLOT = "s1"


def _held(slot, text: str) -> None:
    slot._deferred_notes.append(
        {
            "id": "n1",
            "content": text,
            "cls": "reconcile-note",
            "context": {"source": "note", "content": text},
            "session": None,
        }
    )


def _visible(slot) -> list[tuple[str, str]]:
    return [
        (m["role"], m["content"])
        for m in slot.messages
        if m["role"] in ("user", "assistant", "inject")
    ]


@pytest.fixture
def seen(monkeypatch):
    """The pending-context entries each started turn could read."""
    contexts: list[list[str]] = []

    async def run_chat(_state, slot, _message, **_kwargs):
        contexts.append([entry.get("content") for entry in slot._pending_context])

    monkeypatch.setattr("kiro_crew.dashboard.chat_regenerate._run_chat", run_chat)
    monkeypatch.setattr("kiro_crew.dashboard.chat_rewind._run_chat", run_chat)
    return contexts


def _two_turns(state):
    slot = state.get_or_create_slot(_SLOT)
    slot.append("user", "first", "msg msg-u", ts="2026-05-21T16:00:00Z")
    slot.append("assistant", "one", "msg msg-a", ts="2026-05-21T16:00:01Z")
    slot.append("user", "second", "msg msg-u", ts="2026-05-21T16:00:02Z")
    slot.append("assistant", "two", "msg msg-a", ts="2026-05-21T16:00:03Z")
    slot.drain()
    return slot


@pytest.mark.asyncio
async def test_edit_resend_lands_the_note_above_the_edited_row(state, seen):
    slot = _two_turns(state)
    _held(slot, "deploy finished")

    async with _client(state) as client:
        resp = await client.post(
            f"/api/chat/slots/{_SLOT}/edit-resend", json={"index": 2, "content": "second, edited"}
        )
        assert resp.status == 200
        await slot.task

    assert _visible(slot) == [
        ("user", "first"),
        ("assistant", "one"),
        ("inject", "deploy finished"),
        ("user", "second, edited"),
    ]
    assert slot._deferred_notes == []
    assert seen == [["deploy finished"]]


@pytest.mark.asyncio
async def test_a_refused_edit_keeps_the_note_delivered(state, seen):
    slot = _two_turns(state)
    _held(slot, "deploy finished")
    state.sessions.discard_conversation = AsyncMock(return_value=False)

    async with _client(state) as client:
        resp = await client.post(
            f"/api/chat/slots/{_SLOT}/edit-resend", json={"index": 2, "content": "second, edited"}
        )
        assert resp.status == 409
        await slot.task

    assert _visible(slot)[-1] == ("inject", "deploy finished")
    assert ("user", "second, edited") not in _visible(slot)
    assert slot._deferred_notes == []
    assert [entry.get("content") for entry in slot._pending_context] == ["deploy finished"]
    assert seen == []


@pytest.mark.asyncio
async def test_rewind_lands_the_note_above_the_rewound_row(state, seen):
    slot = _two_turns(state)
    _held(slot, "deploy finished")

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            f"/api/chat/slots/{_SLOT}/rewind",
            json={"at_message_index": 2, "content": "second, rewound"},
        )
        assert resp.status == 200
        assert (await resp.json())["at_message_index"] == 2
        await slot.task

    assert _visible(slot) == [
        ("user", "first"),
        ("assistant", "one"),
        ("inject", "deploy finished"),
        ("user", "second, rewound"),
    ]
    assert slot._deferred_notes == []
    assert seen == [["deploy finished"]]


@pytest.mark.asyncio
async def test_regenerate_lands_the_note_above_the_new_reply(state, monkeypatch):
    slot = _two_turns(state)
    _held(slot, "deploy finished")
    replaced: list[list[str]] = []
    contexts: list[list[str]] = []

    async def run_chat(_state, slot, _message, **_kwargs):
        replaced.append([row["content"] for row in slot._regenerate_target.rows])
        contexts.append([entry.get("content") for entry in slot._pending_context])

    monkeypatch.setattr("kiro_crew.dashboard.chat_regenerate._run_chat", run_chat)
    async with _client(state) as client:
        resp = await client.post(f"/api/chat/slots/{_SLOT}/regenerate")
        assert resp.status == 200
        await slot.task

    # The note follows the reply being replaced and is not one of its rows, so
    # the swap that retires that reply leaves it above the new one.
    assert _visible(slot)[-2:] == [("assistant", "two"), ("inject", "deploy finished")]
    assert replaced == [["two"]]
    assert slot._deferred_notes == []
    assert contexts == [["deploy finished"]]
