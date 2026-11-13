"""A queued send keeps its knowledge envelope mark (``meta.knowledgeEnvelope``).

The dashboard records the injected knowledge envelope's length and hash on
``meta.knowledge.envelope`` at send time. A send that lands on a BUSY slot is
queued, and the queue card finds the envelope by that mark, so the entry, its
``queue_push`` frame and the slot-detail queue carry it: the mark alone, bounded,
never the knowledge text.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app
from chat_test_helpers import _make_state as _make_full_state

from kiro_crew.dashboard.chat_delivery import (
    KNOWLEDGE_ENVELOPE_MAX_LEN,
    KNOWLEDGE_HASH_MAX_LEN,
    knowledge_meta,
    queue_entry_view,
)

_MARK = {"len": 120, "hash": "1x2y3z"}
_RECORD = {"items": 1, "tokens": 9, "titles": ["Doc"], "content": [], "envelope": _MARK}


class TestKnowledgeMeta:
    def test_keeps_only_the_mark_from_a_send_or_an_entry(self):
        assert knowledge_meta({"knowledge": _RECORD}) == {"knowledgeEnvelope": _MARK}
        assert knowledge_meta({"knowledgeEnvelope": dict(_MARK)}) == {"knowledgeEnvelope": _MARK}

    def test_refuses_a_mark_the_card_could_not_use(self):
        for mark in (
            {"len": True, "hash": "a"},
            {"len": 0, "hash": "a"},
            {"len": KNOWLEDGE_ENVELOPE_MAX_LEN + 1, "hash": "a"},
            {"len": 5, "hash": 7},
            {"len": 5, "hash": ""},
            {"len": 5, "hash": "a" * (KNOWLEDGE_HASH_MAX_LEN + 1)},
            "mark",
        ):
            assert knowledge_meta({"knowledgeEnvelope": mark}) == {}
        assert knowledge_meta({"knowledge": {"items": 1}}) == {}
        assert knowledge_meta(None) == {}


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
            json={
                "slot": "busy-chat",
                "message": "kb\n\nwhy?",
                "meta": {"knowledge": _RECORD},
                **body,
            },
        )
        assert resp.status == 200
        return await resp.json()


def _pushes(state):
    return [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_push"]


@pytest.mark.asyncio
async def test_busy_slot_entry_frame_and_queue_view_carry_the_mark(tmp_path, monkeypatch):
    state, slot = _busy_state(tmp_path, monkeypatch)
    await _post(state)
    entry = next(i for i in slot._queue if i["content"] == "kb\n\nwhy?")
    assert entry["meta"]["knowledgeEnvelope"] == _MARK
    assert "knowledge" not in entry["meta"]
    assert _pushes(state)[-1]["meta"] == {"knowledgeEnvelope": _MARK}
    assert queue_entry_view(entry)["meta"] == {"knowledgeEnvelope": _MARK}


@pytest.mark.asyncio
async def test_a_steer_the_turn_never_took_requeues_with_the_mark(tmp_path, monkeypatch):
    from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

    state, slot = _busy_state(tmp_path, monkeypatch)
    slot._acp_client = MagicMock(supports_steer=True, steer=AsyncMock(return_value=True))
    assert (await _post(state, steer=True)).get("steered") is True
    _requeue_unconsumed_steers(state, slot)
    entry = next(i for i in slot._queue if i["content"] == "kb\n\nwhy?")
    assert entry["meta"]["knowledgeEnvelope"] == _MARK
    assert _pushes(state)[-1]["meta"] == {"knowledgeEnvelope": _MARK, "requeuedSteer": True}
