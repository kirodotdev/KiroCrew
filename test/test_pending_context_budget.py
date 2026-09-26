"""Seat-ceiling admission for the pending-context queue.

Covers ``_ChatSlot.has_pending_context_seat`` and the three callers of
``append_pending_context``: the queue refuses an arrival over the ceiling
instead of evicting a seated entry the caller already holds a 200 for, and
every caller reports the refusal instead of acknowledging it.
"""

import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_handlers import api_chat_slot_note
from kiro_crew.dashboard.state import (
    _MAX_PENDING_CONTEXT,
    _ChatSlot,
    context_entry_expired,
)


def _entry(
    content: str,
    *,
    source: str = "test",
    max_age: float | None = 86400,
    injected_at: float | None = None,
    **extra: object,
) -> dict:
    """A pending-context entry in the shape `_build_pending_context_entry` produces."""
    e: dict = {
        "content": content,
        "source": source,
        "injectedAt": time.time() if injected_at is None else injected_at,
        "maxAge": max_age,
        "ctxId": uuid.uuid4().hex,
    }
    e.update(extra)
    return e


def _held_note(context: dict | None, *, content: str = "held while running") -> dict:
    """A held note with no session stamp, which the flush's rebind gate lets through."""
    return {
        "id": uuid.uuid4().hex[:12],
        "content": content,
        "cls": "reconcile-note",
        "context": context,
        "session": None,
    }


def test_the_queue_refuses_at_the_seat_ceiling_instead_of_evicting():
    """The 51st entry must be REFUSED, never admitted by evicting the oldest.

    Eviction discarded an entry the caller already had a 200 for, with nothing
    reporting the loss -- "truncate after acknowledgement" on the seat dimension.
    """
    slot = _ChatSlot("chat-ctx-ceiling")
    for i in range(_MAX_PENDING_CONTEXT):
        assert slot.append_pending_context(_entry(f"e{i}", source=f"s{i}")) is True
    assert slot.has_pending_context_seat() is False
    assert slot.append_pending_context(_entry("overflow", source="s99")) is False
    contents = [e["content"] for e in slot._pending_context]
    assert len(contents) == _MAX_PENDING_CONTEXT
    assert "e0" in contents, "the oldest acknowledged entry must NOT have been evicted"
    assert "overflow" not in contents


def test_expired_entries_free_seats_for_a_new_arrival():
    """Refusal is not permanent: pruning a dead entry is what frees capacity.

    Control: the same slot refuses while the seats are LIVE, so the pass below is
    the prune and not an ever-open ceiling.
    """
    slot = _ChatSlot("chat-ctx-prune")
    for i in range(_MAX_PENDING_CONTEXT):
        assert slot.append_pending_context(_entry(f"live{i}", source=f"s{i}")) is True
    assert slot.append_pending_context(_entry("refused", source="sX")) is False

    slot._pending_context[0] = _entry(
        "dead", source="s0", max_age=1, injected_at=time.time() - 10_000
    )
    assert slot.append_pending_context(_entry("arrives", source="sY")) is True
    assert [e["content"] for e in slot._pending_context].count("dead") == 0


def test_an_entry_that_arrives_expired_is_refused():
    """A held note's maxAge can elapse while its turn runs, so an entry can arrive dead."""
    slot = _ChatSlot("chat-ctx-dead-arrival")
    dead = _entry("dead", max_age=1, injected_at=time.time() - 10_000)
    assert context_entry_expired(dead, time.time()), "precondition: the entry is expired"
    assert slot.append_pending_context(dead) is False
    assert slot._pending_context == []


def test_a_held_note_reserves_a_seat_against_a_later_arrival():
    """The flush promotes a held note's context into this queue, so it holds a seat.

    Without the reservation a later /context takes the seat the flush needs and the
    note's context is lost after its 200.
    """
    slot = _ChatSlot("chat-ctx-seat")
    for i in range(_MAX_PENDING_CONTEXT - 1):
        assert slot.append_pending_context(_entry(f"e{i}", source=f"s{i}")) is True
    assert slot.has_pending_context_seat() is True, "control: 49 live entries leave one seat"
    slot._deferred_notes.append(_held_note(_entry("held")))
    assert slot.has_pending_context_seat() is False


def test_an_expired_held_note_reserves_no_seat():
    """Only a LIVE held context is reserved -- a dead one is never promoted."""
    slot = _ChatSlot("chat-ctx-dead-hold")
    for i in range(_MAX_PENDING_CONTEXT - 1):
        assert slot.append_pending_context(_entry(f"e{i}", source=f"s{i}")) is True
    dead = _entry("held", max_age=1, injected_at=time.time() - 10_000)
    assert context_entry_expired(dead, time.time()), "precondition: the held context is expired"
    slot._deferred_notes.append(_held_note(dead))
    assert slot.has_pending_context_seat() is True


def test_the_flush_marks_the_row_when_the_held_context_has_no_seat():
    """A refused context half must be readable on the delivered row, not dropped silently.

    Reachable because the hold is admitted by the deferred-note ceiling, not the
    context seat ceiling: 50 `/context` posts can seat before the note is held. The
    flush pops `context` as its retry marker, so a discarded return leaves no surface
    at all -- the row lands with `ok` and the context is gone.
    """
    slot = _ChatSlot("chat-flush-no-seat")
    slot.linked_session_key = "dashboard_chat-flush-no-seat"
    for i in range(_MAX_PENDING_CONTEXT):
        assert slot.append_pending_context(_entry(f"e{i}", source=f"s{i}")) is True
    slot._deferred_notes.append(_held_note(_entry("held"), content="delivered without context"))

    assert slot.flush_deferred_notes() == 1
    row = [m for m in slot.messages if m.get("role") == "inject"][-1]
    assert row["content"] == "delivered without context"
    assert row["meta"]["contextDropped"] is True
    assert all(e["content"] != "held" for e in slot._pending_context)


def test_the_flush_leaves_the_row_unmarked_when_the_context_seats():
    """Control for the marker: with a seat free, the context lands and nothing is marked."""
    slot = _ChatSlot("chat-flush-seat")
    slot.linked_session_key = "dashboard_chat-flush-seat"
    slot._deferred_notes.append(_held_note(_entry("held"), content="delivered with context"))

    assert slot.flush_deferred_notes() == 1
    row = [m for m in slot.messages if m.get("role") == "inject"][-1]
    assert "contextDropped" not in row["meta"]
    assert [e["content"] for e in slot._pending_context] == ["held"]


class TestImmediateNoteReportsRefusal:
    """`/note` outside a running turn must not answer ok:true over a refused context."""

    @asynccontextmanager
    async def _client(self, state):
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/note", api_chat_slot_note)
        async with TestClient(TestServer(app)) as c:
            yield c

    def _seeded(self, state, name: str) -> _ChatSlot:
        slot = state.get_or_create_slot(name)
        slot._titled = True
        slot.append(role="user", content="a real message", cls="msg msg-u")
        slot.drain()
        return slot

    @pytest.mark.asyncio
    async def test_a_full_queue_answers_context_skipped(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = self._seeded(state, "s-note-full")
        for i in range(_MAX_PENDING_CONTEXT):
            assert slot.append_pending_context(_entry(f"e{i}", source=f"s{i}")) is True
        assert not slot.running, "precondition: the immediate arm, not the hold"

        async with self._client(state) as client:
            resp = await client.post("/api/chat/slots/s-note-full/note", json={"content": "n"})
            assert resp.status == 200
            body = await resp.json()

        assert body["contextSkipped"] is True, (
            "answering contextSkipped=false over a refused append is the "
            "acknowledged-then-discarded loss this endpoint must not have"
        )
        assert body["appended"] is True, "the visible half is still owed"
        assert all(e["content"] != "n" for e in slot._pending_context)
        assert [m for m in slot.messages if m.get("role") == "inject"], "visible row must land"

    @pytest.mark.asyncio
    async def test_room_for_the_context_answers_not_skipped(self, tmp_path: Path, monkeypatch):
        """Control: the same call with a seat free must report the context queued."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = self._seeded(state, "s-note-room")

        async with self._client(state) as client:
            resp = await client.post("/api/chat/slots/s-note-room/note", json={"content": "n"})
            assert resp.status == 200
            body = await resp.json()

        assert body["contextSkipped"] is False
        assert [e["content"] for e in slot._pending_context] == ["n"]
