"""A note held across a restart keeps its place ahead of the next immediate note.

A ``/note`` posted while a turn runs is held, and the queue drain or the cycle end
delivers it. A restart that cuts that turn restores the hold into an idle slot.
A new ``/note`` on that idle slot is delivered immediately, so the older, already
acknowledged note must be flushed first: otherwise the newer line lands above it
in the transcript and its context half is queued ahead of the older one.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from gateway_appkit_endpoints_helpers import _make_state

from kiro_crew.dashboard.chat import api_chat_slot_note
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


def _restored_hold(slot: _ChatSlot, content: str, context: str) -> None:
    """Seat a hold the way the restore paths do: on a slot no turn owns."""
    slot._deferred_notes = [
        {
            "id": "a1b2c3d4e5f6",
            "content": content,
            "cls": "reconcile-note",
            "context": {"content": context, "source": "board-sync", "ephemeral": True},
            "source": "",
            "session": effective_session_key(slot),
        }
    ]


@asynccontextmanager
async def _note_client(state: DashboardState):
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/note", api_chat_slot_note)
    async with TestClient(TestServer(app)) as client:
        yield client


@pytest.mark.asyncio
async def test_an_immediate_note_lands_below_the_older_restored_hold(tmp_path: Path):
    state = _make_state(tmp_path)
    slot = _ChatSlot("s1")
    state._slots["s1"] = slot
    _restored_hold(slot, "older held note", context="older context")
    assert not slot.running

    async with _note_client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/note",
            json={"content": "newer note", "source": "board-sync"},
        )
        assert resp.status == 200
        assert (await resp.json())["appended"] is True

    assert [m["content"] for m in slot.messages] == ["older held note", "newer note"]
    assert slot._deferred_notes == []
    assert [e["content"] for e in slot._pending_context] == ["older context", "newer note"]


class _EmptyWindow:
    """A restored sidebar row's pending window: an empty transcript on disk."""

    def __init__(self) -> None:
        self.reads = 0

    def read(self, slot: _ChatSlot) -> tuple[list[dict], list[set[str]]]:
        self.reads += 1
        return [], []

    def __call__(self, slot: _ChatSlot, messages=None, claims=None) -> None:
        slot._messages = list(messages or [])


@pytest.mark.asyncio
async def test_a_restored_sidebar_row_flushes_its_hold_before_an_immediate_note(tmp_path: Path):
    # A tab past the eager restores comes back as a row whose window loads on
    # first read; the hold must still be delivered ahead of the newer note.
    state = _make_state(tmp_path)
    slot = _ChatSlot("s1")
    state._slots["s1"] = slot
    _restored_hold(slot, "older held note", context="older context")
    window = _EmptyWindow()
    slot._lazy_window = window
    assert slot.window_pending

    async with _note_client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/note",
            json={"content": "newer note", "source": "board-sync"},
        )
        assert resp.status == 200
        assert (await resp.json())["appended"] is True

    assert window.reads == 1
    assert not slot.window_pending
    assert [m["content"] for m in slot.messages] == ["older held note", "newer note"]
    assert slot._deferred_notes == []
    assert [e["content"] for e in slot._pending_context] == ["older context", "newer note"]


@pytest.mark.asyncio
async def test_an_immediate_note_with_no_hold_writes_only_itself(tmp_path: Path):
    state = _make_state(tmp_path)
    slot = _ChatSlot("s1")
    state._slots["s1"] = slot

    async with _note_client(state) as client:
        resp = await client.post(
            "/api/chat/slots/s1/note",
            json={"content": "only note", "source": "board-sync"},
        )
        assert resp.status == 200

    assert [m["content"] for m in slot.messages] == ["only note"]
    assert [e["content"] for e in slot._pending_context] == ["only note"]
