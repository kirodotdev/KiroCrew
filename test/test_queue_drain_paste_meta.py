"""A queued send keeps its collapsed-paste blocks through the drain.

A dispatched send persists ``meta.pastes`` on its user row, so the row draws the
paste chip again after a reload or on another device. A queued send's row is
written by the drain from the entry's meta, and a bot's chat queues every send,
so the entry has to carry the blocks or that row comes back as the whole paste.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_paste_meta import PASTE_BLOCKS_MAX, merged_pastes, paste_meta

_PASTE = "| a | b |\n| - | - |\n| 1 | 2 |"


def _block(block_id: str = "p1", content: str = _PASTE, **over) -> dict:
    return {"id": block_id, "seq": 1, "lines": content.count("\n") + 1, "content": content, **over}


async def _drain_once(state, slot) -> None:
    from kiro_crew.dashboard import chat_runner

    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
    ):
        assert await chat_runner._start_next_queued_turn(state, slot) is True


def _last_user_meta(slot) -> dict:
    rows = [m for m in slot.messages if m.get("role") == "user"]
    assert rows, "the drain must have written a user row"
    return rows[-1].get("meta") or {}


class TestPasteMeta:
    def test_keeps_blocks_whose_text_the_message_holds(self):
        assert paste_meta({"pastes": [_block()]}, f"{_PASTE}\nlook") == [_block()]

    @pytest.mark.parametrize("suffix", [" ", "\t", "\n", " \t\n"])
    def test_keeps_a_block_after_the_message_loses_trailing_whitespace(self, suffix):
        block = _block(content=_PASTE + suffix)
        assert paste_meta({"pastes": [block]}, _PASTE) == [{**block, "content": _PASTE}]
        assert paste_meta({"pastes": [block]}, _PASTE + suffix + "look") == [block]
        assert block["content"] == _PASTE + suffix

    @pytest.mark.parametrize("prefix", [" ", "\t", "\n", " \t\n"])
    @pytest.mark.parametrize("suffix", ["", " \t\n"])
    def test_keeps_a_block_after_the_message_loses_leading_whitespace(self, prefix, suffix):
        content = prefix + _PASTE + suffix
        block = _block(content=content)
        assert paste_meta({"pastes": [block]}, content.strip()) == [{**block, "content": _PASTE}]
        assert paste_meta({"pastes": [block]}, "look\n" + content + "\nlook") == [block]
        assert block["content"] == content

    @pytest.mark.parametrize(
        "pastes",
        [
            [_block(content="not in the message")],
            [_block(content="not in the message \t\n")],
            [_block(content=" \t\n")],
            [_block(), {"id": "p2"}],
            [_block(block_id="")],
            [_block(seq="1")],
            "not a list",
            [_block(block_id=f"p{i}") for i in range(PASTE_BLOCKS_MAX + 1)],
        ],
    )
    def test_refuses_a_malformed_list_whole(self, pastes):
        assert paste_meta({"pastes": pastes}, f"{_PASTE}\nlook") == []

    def test_carries_only_the_known_fields(self):
        assert paste_meta({"pastes": [_block(extra="x")]}, _PASTE) == [_block()]

    def test_merges_entries_with_repeated_blocks_in_order(self):
        second = _block("p2", "second paste")
        entries = [
            {"meta": {"pastes": [_block()]}},
            {"meta": {}},
            {"meta": {"pastes": [second, _block()]}},
        ]
        assert merged_pastes(entries) == [_block(), second, _block()]


class TestBusySessionSend:
    @pytest.mark.asyncio
    async def test_a_send_queued_behind_a_running_turn_carries_its_blocks(
        self, tmp_path, monkeypatch
    ):
        """A Sessions chat queues a send while a turn runs, through `/api/chat`."""
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("busy-chat")
        slot.task = MagicMock(done=MagicMock(return_value=False))
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", MagicMock())
        message = f"{_PASTE}\nplease look"
        body = {"message": message, "slot": "busy-chat", "meta": {"pastes": [_block()]}}
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat", json=body)
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        entry = next(i for i in slot._queue if i["content"] == message)
        assert entry["meta"].get("pastes") == [_block()]


class TestDrainedRow:
    @pytest.mark.asyncio
    async def test_the_row_keeps_the_entry_paste_blocks(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.subagents = None
        slot = state.get_or_create_slot("bot-chat")

        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        message = f"{_PASTE}\nplease look"
        queue_for_next_turn(
            state,
            slot,
            message,
            directive_user_origin=True,
            send_id="s-aaa-1",
            pastes=paste_meta({"pastes": [_block()]}, message),
        )
        await _drain_once(state, slot)

        assert _last_user_meta(slot).get("pastes") == [_block()]

    @pytest.mark.asyncio
    async def test_a_merged_row_keeps_every_send_blocks(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.subagents = None
        slot = state.get_or_create_slot("bot-chat")

        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        first, second = _block("p1"), _block("p2", "second\npaste\nhere")
        queue_for_next_turn(state, slot, _PASTE, directive_user_origin=True, pastes=[first])
        queue_for_next_turn(
            state, slot, "second\npaste\nhere", directive_user_origin=True, pastes=[second]
        )
        cfg = MagicMock()
        cfg.load.return_value.dashboard.merge_queued_messages = True
        monkeypatch.setattr(chat_runner, "KiroCrewConfig", cfg)
        await _drain_once(state, slot)

        assert _last_user_meta(slot).get("pastes") == [first, second]
