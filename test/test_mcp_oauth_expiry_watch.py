"""An open tab hears that an MCP OAuth banner expired when its child ends.

Covers ``kiro_crew.dashboard.mcp_oauth_expiry_watch``: the watch started when an
authorize banner is appended, which pushes the read gate's verdict to open tabs
when the minting child stops being the slot's live child, however it ended.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard import mcp_oauth_expiry_watch as watch
from kiro_crew.dashboard.chat_runner import _emit_mcp_oauth_request
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.testing.wait import await_until


class _Provider:
    def __init__(self, token: str) -> None:
        self.process_instance = token
        self.alive = True

    def is_process_alive(self) -> bool:
        return self.alive


def _state(provider: _Provider | None) -> MagicMock:
    state = MagicMock()
    state.sessions.get_provider.return_value = provider
    return state


def _emit(state: MagicMock, slot: _ChatSlot, child: str, name: str = "miro") -> None:
    _emit_mcp_oauth_request(state, slot, name, f"https://mcp.{name}.com/a?port=1", minted_by=child)


def _expiry_frames(state: MagicMock) -> list[dict]:
    return [
        call.args[1]
        for call in state.broadcast_ws.call_args_list
        if call.args[0] == "chat_message_update" and call.args[1]["meta"].get("expired")
    ]


class TestCheckOnce:
    def test_live_child_keeps_watching_and_sends_nothing(self):
        provider = _Provider("child-a")
        state, slot = _state(provider), _ChatSlot("s1")
        _emit(state, slot, "child-a")
        assert watch.check_once(state, slot, set()) is True
        assert _expiry_frames(state) == []

    def test_child_exit_sends_one_expiry_frame_then_stops(self):
        provider = _Provider("child-a")
        state, slot = _state(provider), _ChatSlot("s1")
        _emit(state, slot, "child-a")
        provider.alive = False
        announced: set[str] = set()
        assert watch.check_once(state, slot, announced) is False
        frames = _expiry_frames(state)
        assert len(frames) == 1
        assert frames[0]["meta"]["oauth_url"] == ""
        assert frames[0]["mid"] == slot.messages[0]["meta"]["mid"]

    def test_a_dead_banner_is_announced_once_while_a_live_one_is_watched(self):
        provider = _Provider("child-b")
        state, slot = _state(provider), _ChatSlot("s1")
        _emit(state, slot, "child-a", "miro")
        _emit(state, slot, "child-b", "linear")
        announced: set[str] = set()
        assert watch.check_once(state, slot, announced) is True
        assert watch.check_once(state, slot, announced) is True
        first, second = (m["meta"]["mid"] for m in slot.messages)
        assert [f["mid"] for f in _expiry_frames(state)] == [first]
        provider.alive = False
        assert watch.check_once(state, slot, announced) is False
        assert [f["mid"] for f in _expiry_frames(state)] == [first, second]

    def test_a_recorded_outcome_ends_the_watch(self):
        state, slot = _state(_Provider("child-a")), _ChatSlot("s1")
        _emit(state, slot, "child-a")
        slot.messages[0]["meta"]["completed"] = True
        assert watch.check_once(state, slot, set()) is False
        assert _expiry_frames(state) == []

    def test_the_stored_row_keeps_its_link(self):
        provider = _Provider("child-a")
        state, slot = _state(provider), _ChatSlot("s1")
        _emit(state, slot, "child-a")
        provider.alive = False
        watch.check_once(state, slot, set())
        assert slot.messages[0]["meta"]["oauth_url"]
        assert "expired" not in slot.messages[0]["meta"]


class TestWatchTask:
    @pytest.fixture(autouse=True)
    def _fast(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(watch, "POLL_INTERVAL_SECS", 0)
        watch._watches.clear()
        yield
        watch._watches.clear()

    @pytest.mark.asyncio
    async def test_emit_starts_one_watch_that_reports_the_exit(self):
        provider = _Provider("child-a")
        state, slot = _state(provider), _ChatSlot("s1")
        _emit(state, slot, "child-a")
        first = watch._watches[slot.key][1]
        _emit(state, slot, "child-a", "linear")
        task = watch._watches[slot.key][1]
        assert task is first
        for _ in range(3):
            await asyncio.sleep(0)
        assert not task.done()
        provider.alive = False
        await await_until(task, what="the expiry watch")
        assert len(_expiry_frames(state)) == 2
        assert slot.key not in watch._watches

    @pytest.mark.asyncio
    async def test_session_leaving_the_pool_is_reported(self):
        state, slot = _state(_Provider("child-a")), _ChatSlot("s1")
        _emit(state, slot, "child-a")
        task = watch._watches[slot.key][1]
        state.sessions.get_provider.return_value = None
        await await_until(task, what="the expiry watch")
        assert len(_expiry_frames(state)) == 1

    @pytest.mark.asyncio
    async def test_a_rebuilt_slot_under_the_same_key_gets_its_own_watch(self):
        provider = _Provider("child-a")
        state = _state(provider)
        old, new = _ChatSlot("s1"), _ChatSlot("s1")
        _emit(state, old, "child-a")
        old_task = watch._watches["s1"][1]
        _emit(state, new, "child-a", "linear")
        entry = watch._watches["s1"]
        assert entry[0] is new
        with pytest.raises(asyncio.CancelledError):
            await await_until(old_task, what="the old slot's watch")
        provider.alive = False
        await await_until(entry[1], what="the expiry watch")
        assert new.messages[0]["meta"]["mid"] in [f["mid"] for f in _expiry_frames(state)]

    @pytest.mark.asyncio
    async def test_the_watch_ends_at_its_time_limit(self, monkeypatch):
        monkeypatch.setattr(watch, "MAX_WATCH_SECS", 0)
        state, slot = _state(_Provider("child-a")), _ChatSlot("s1")
        _emit(state, slot, "child-a")
        await await_until(watch._watches[slot.key][1], what="the expiry watch")
        assert _expiry_frames(state) == []

    def test_no_running_loop_starts_nothing(self):
        state, slot = _state(_Provider("child-a")), _ChatSlot("s1")
        _emit(state, slot, "child-a")
        assert slot.key not in watch._watches
