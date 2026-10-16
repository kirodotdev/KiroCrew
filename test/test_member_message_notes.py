"""A crewmate's message in its DM thread posts one notification.

Before this, a crewmate writing to the user reached them only through the
Crewmates rail dot and the member row's unread marker. The note is published
from the slot's row hook (``DashboardState._record_member_row``), attributed
from the slot's live DM binding, on its own mutable ``system.member`` channel.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard import member_message_notes
from kiro_crew.dashboard.handlers.messaging import api_notification_channels
from kiro_crew.dashboard.member_message_notes import MEMBER_CHANNEL
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.members import member_slot_key, write_dm_binding
from kiro_crew.notifications.bus import SYSTEM_CHANNELS

ADA = {"slug": "ada", "name": "Ada"}


def _make_state(monkeypatch, tmp_path) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _bind(slug: str = "ada", name: str = "Ada") -> str:
    slot = member_slot_key(slug)
    write_dm_binding(slug, member=name, slot_key=slot)
    return slot


def _row(role: str, content: str = "The draft is ready for your review.") -> dict:
    return {"role": role, "content": content, "ts": "1760000000.0"}


def _member_notes(state: DashboardState) -> list[dict]:
    return [n for n in state._notification_log if n.get("channel") == MEMBER_CHANNEL]


class TestMessageNote:
    def test_an_assistant_row_posts_one_attributed_note(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        slot = _bind()
        state._record_member_row(slot, _row("assistant"))

        [note] = _member_notes(state)
        assert note["member"] == ADA
        assert note["title"] == "Ada"
        assert note["body"] == "The draft is ready for your review."
        # Kind stays "agent": the feed badge, stat cards and per-kind sound are
        # the ones every agent note already uses.
        assert note["kind"] == "agent"
        assert note["priority"] == "default"
        assert note["url"] == "/members?member=Ada"
        assert note["slot"] == slot
        assert note["group_key"] == slot
        assert state._unread_count == 1

    def test_the_deep_link_carries_the_exact_name(self, monkeypatch, tmp_path):
        # The Members page selects a thread by the crew's exact name, not its slug.
        state = _make_state(monkeypatch, tmp_path)
        slot = _bind("ada-lovelace", "Ada Lovelace")
        state._record_member_row(slot, _row("assistant"))
        [note] = _member_notes(state)
        assert note["url"] == "/members?member=Ada%20Lovelace"
        assert note["member"] == {"slug": "ada-lovelace", "name": "Ada Lovelace"}

    def test_on_the_event_loop_the_note_is_published_after_the_binding_read(
        self, monkeypatch, tmp_path
    ):
        state = _make_state(monkeypatch, tmp_path)
        slot = _bind()

        async def _drive() -> None:
            state._record_member_row(slot, _row("assistant"))
            # The binding read is off the loop, so nothing is published yet.
            assert _member_notes(state) == []
            await asyncio.gather(*list(member_message_notes._pending))

        asyncio.run(_drive())
        [note] = _member_notes(state)
        assert note["member"] == ADA

    def test_each_message_posts_its_own_note_on_one_stack(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        slot = _bind()
        state._record_member_row(slot, _row("assistant", "First."))
        state._record_member_row(slot, _row("assistant", "Second."))
        notes = _member_notes(state)
        assert [n["body"] for n in notes] == ["First.", "Second."]
        assert {n["group_key"] for n in notes} == {slot}


class TestWhatDoesNotNotify:
    @pytest.mark.parametrize(
        "role", ["user", "tool_call", "tool_result", "inject", "subagent", "error", "permission"]
    )
    def test_only_assistant_rows_notify(self, monkeypatch, tmp_path, role):
        state = _make_state(monkeypatch, tmp_path)
        state._record_member_row(_bind(), _row(role))
        assert _member_notes(state) == []

    @pytest.mark.parametrize("content", ["", "   \n"])
    def test_an_assistant_row_without_text_does_not_notify(self, monkeypatch, tmp_path, content):
        state = _make_state(monkeypatch, tmp_path)
        state._record_member_row(_bind(), _row("assistant", content))
        assert _member_notes(state) == []

    def test_an_ordinary_chat_does_not_notify(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        _bind()
        state._record_member_row("chat-7-1700000000", _row("assistant"))
        assert state._notification_log == []

    def test_a_member_slot_with_no_binding_does_not_notify(self, monkeypatch, tmp_path):
        # Attribution comes only from the live binding; a key alone names no one.
        state = _make_state(monkeypatch, tmp_path)
        state._record_member_row(member_slot_key("ghost"), _row("assistant"))
        assert state._notification_log == []


class TestChannel:
    def test_member_is_a_system_channel_with_the_default_priority(self):
        assert SYSTEM_CHANNELS[MEMBER_CHANNEL] == "default"

    def test_muting_the_channel_silences_the_note(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update(MEMBER_CHANNEL, muted=True)
        state._record_member_row(_bind(), _row("assistant"))
        [note] = _member_notes(state)
        assert note["silenced"] is True
        assert note["priority"] == "passive"
        assert state._unread_count == 0

    def test_muting_agent_notes_leaves_crewmate_messages_alone(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update("system.agent", muted=True)
        state._record_member_row(_bind(), _row("assistant"))
        [note] = _member_notes(state)
        assert "silenced" not in note

    @pytest.mark.asyncio
    async def test_settings_lists_the_channel_as_mutable(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        request = MagicMock()
        request.app = {"state": state}
        resp = await api_notification_channels(request)
        channels = {c["channel"]: c for c in json.loads(resp.body)["channels"]}
        entry = channels[MEMBER_CHANNEL]
        assert entry["registered"] is True
        assert entry["protected"] is False
