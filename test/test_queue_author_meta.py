"""A queued send's ``queue_push`` frame and slot-detail entry name its author.

A send that lands on a BUSY slot is queued, and the server stamps its
server-derived Shared Crew author on the entry (``meta.author``) for the drain
to write on the row. A chat that draws a waiting send as a message bubble (a
crewmate's chat) reads the frame and the slot-detail queue, so both carry that
same stamp: without it a teammate's waiting message is drawn as the reader's own
until the turn drains it. The stamp is the entry's, never the client's claim.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app
from chat_test_helpers import _make_state as _make_full_state

from kiro_crew.dashboard import chat_delivery
from kiro_crew.dashboard.chat_delivery import queue_entry_view

_AUTHOR = {
    "id": "p_sam",
    "kind": "human",
    "display": {"name": "Sam Avila", "login": "sam", "captured_at": None},
}
_CLAIM = {"id": "p_forged", "kind": "human", "display": {"name": "Not Me", "login": "x"}}


class TestQueueAuthorMeta:
    def test_returns_the_entry_author_in_its_public_shape(self):
        meta = {"author": {**_AUTHOR, "private": "never on the wire"}, "sendId": "s-1"}
        out = chat_delivery.queue_author_meta(meta)
        assert out == {"author": _AUTHOR}
        assert out["author"] is not meta["author"]

    @pytest.mark.parametrize("meta", [None, {}, {"sendId": "s"}, {"author": "p_sam"}, "x"])
    def test_is_empty_without_a_stamped_author(self, meta):
        assert chat_delivery.queue_author_meta(meta) == {}

    @pytest.mark.parametrize("author", [{}, {"id": ""}, {"id": "p_sam", "kind": "nobody"}])
    def test_a_malformed_stamp_reads_as_unknown_like_the_drained_row(self, author):
        from kiro_crew.shared_crew.attribution import message_author

        assert chat_delivery.queue_author_meta({"author": author}) == {
            "author": message_author({"author": author}).to_public_dict()
        }
        assert chat_delivery.queue_author_meta({"author": author})["author"]["kind"] == "unknown"

    def test_the_listing_names_the_author_only_when_the_entry_has_one(self):
        assert queue_entry_view({"id": "q1", "content": "hi", "meta": {"author": _AUTHOR}})[
            "meta"
        ] == {"author": _AUTHOR}
        assert "meta" not in queue_entry_view({"id": "q2", "content": "hi", "meta": {}})


def _busy_state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_full_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot("busy-chat")
    slot._turn_admission_reserved = True  # force the busy queue path
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", MagicMock())
    return state, slot


async def _post(state, **body):
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat",
            # The client names an author: the server must never echo it back.
            json={
                "slot": "busy-chat",
                "message": "second thought",
                "meta": {"author": _CLAIM},
                **body,
            },
        )
        assert resp.status == 200
        return await resp.json()


def _pushes(state):
    return [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_push"]


@pytest.mark.asyncio
async def test_busy_slot_frame_and_queue_view_carry_the_entrys_author(tmp_path, monkeypatch):
    state, slot = _busy_state(tmp_path, monkeypatch)
    await _post(state)
    entry = next(i for i in slot._queue if i["content"] == "second thought")
    stamped = entry["meta"]["author"]
    assert stamped["id"] and stamped != _CLAIM
    assert (_pushes(state)[-1].get("meta") or {}).get("author") == stamped
    assert (queue_entry_view(entry).get("meta") or {}).get("author") == stamped


@pytest.mark.asyncio
async def test_a_steer_the_turn_never_took_requeues_with_its_author(tmp_path, monkeypatch):
    from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

    state, slot = _busy_state(tmp_path, monkeypatch)
    slot._acp_client = MagicMock(supports_steer=True, steer=AsyncMock(return_value=True))
    assert (await _post(state, steer=True)).get("steered") is True
    _requeue_unconsumed_steers(state, slot)
    entry = next(i for i in slot._queue if i["content"] == "second thought")
    stamped = entry["meta"]["author"]
    assert stamped["id"] and stamped != _CLAIM
    assert _pushes(state)[-1]["meta"].get("author") == stamped
