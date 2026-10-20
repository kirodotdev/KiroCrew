"""A crewmate's message in its DM thread posts one notification.

Before this, a crewmate writing to the user reached them only through the
Crewmates rail dot and the member row's unread marker. The note is published
once per landed turn (``member_message_notes.post_turn_reply``, called by the
chat runner), for the turn's last reply row, attributed from the slot's live DM
binding, on its own mutable ``system.member`` channel. A row the gateway wrote
into the thread is not the crewmate speaking and never notifies.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.types import STOP_REASON_CANCELLED, STOP_REASON_END_TURN
from kiro_crew.dashboard import member_message_notes
from kiro_crew.dashboard.handlers.messaging import api_notification_channels
from kiro_crew.dashboard.member_message_notes import MEMBER_CHANNEL, post_turn_reply
from kiro_crew.dashboard.state import DashboardState, row_mid
from kiro_crew.members import DM_SLOT_MODE, member_slot_key, write_dm_binding
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


def _land(state: DashboardState, slot_key: str, *rows: dict, gateway: tuple = ()) -> None:
    """Append *rows* as one turn's own replies and land the turn.

    *gateway* rows are appended to the same thread first but not recorded as the
    turn's replies, as a compaction notice or an injected result is.
    """
    slot = _slot(state, slot_key)
    for row in gateway:
        slot.append(row["role"], row["content"], "msg msg-a")
    mids = [row_mid(slot.append(row["role"], row["content"], "msg msg-a")) for row in rows]
    post_turn_reply(state, slot, [m for m in mids if m])


def _slot(state: DashboardState, slot_key: str):
    if slot_key.startswith("member-"):
        # A member thread runs pinned to its crewmate's dispatchable name.
        return state.get_or_create_slot(slot_key, agent="Ada", mode=DM_SLOT_MODE)
    return state.get_or_create_slot(slot_key)


def _member_notes(state: DashboardState) -> list[dict]:
    return [n for n in state._notification_log if n.get("channel") == MEMBER_CHANNEL]


class TestMessageNote:
    def test_a_landed_turn_posts_one_attributed_note(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        slot = _bind()
        _land(state, slot, _row("assistant"))

        [note] = _member_notes(state)
        assert note["member"] == ADA
        assert note["title"] == "Ada"
        assert note["body"] == "The draft is ready for your review."
        # Kind stays "agent": the feed badge, stat cards and per-kind sound are
        # the ones every agent note already uses.
        assert note["kind"] == "agent"
        # Bell only by default: no banner, sound or OS toast unasked.
        assert note["priority"] == "passive"
        assert note["url"] == "/members?member=Ada"
        assert note["slot"] == slot
        assert note["group_key"] == slot
        # Passive notes do not raise the unread count either.
        assert state._unread_count == 0

    def test_the_deep_link_carries_the_exact_name(self, monkeypatch, tmp_path):
        # The Members page selects a thread by the crew's exact name, not its slug.
        state = _make_state(monkeypatch, tmp_path)
        slot = _bind("ada-lovelace", "Ada Lovelace")
        _land(state, slot, _row("assistant"))
        [note] = _member_notes(state)
        assert note["url"] == "/members?member=Ada%20Lovelace"
        assert note["member"] == {"slug": "ada-lovelace", "name": "Ada Lovelace"}

    def test_on_the_event_loop_the_note_is_published_after_the_binding_read(
        self, monkeypatch, tmp_path
    ):
        state = _make_state(monkeypatch, tmp_path)
        slot = _bind()

        async def _drive() -> None:
            _land(state, slot, _row("assistant"))
            # The binding read is off the loop, so nothing is published yet.
            assert _member_notes(state) == []
            await asyncio.gather(*list(member_message_notes._pending))

        asyncio.run(_drive())
        [note] = _member_notes(state)
        assert note["member"] == ADA

    def test_a_turn_of_several_segments_posts_one_note_for_its_answer(self, monkeypatch, tmp_path):
        # One reply row per model call: the text before a tool call is its own
        # segment. The user is pinged once, with the answer.
        state = _make_state(monkeypatch, tmp_path)
        slot = _bind()
        _land(state, slot, _row("assistant", "Let me check."), _row("assistant", "Done: 3 files."))
        [note] = _member_notes(state)
        assert note["body"] == "Done: 3 files."

    def test_an_empty_last_segment_falls_back_to_the_last_text(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        slot = _bind()
        _land(state, slot, _row("assistant", "The answer."), _row("tool_call", "{}"))
        [note] = _member_notes(state)
        assert note["body"] == "The answer."

    def test_each_turn_posts_its_own_note_on_one_stack(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        slot = _bind()
        _land(state, slot, _row("assistant", "First."))
        _land(state, slot, _row("assistant", "Second."))
        notes = _member_notes(state)
        assert [n["body"] for n in notes] == ["First.", "Second."]
        assert {n["group_key"] for n in notes} == {slot}


class TestWhatDoesNotNotify:
    @pytest.mark.parametrize(
        "role", ["user", "tool_call", "tool_result", "inject", "subagent", "error", "permission"]
    )
    def test_only_assistant_rows_notify(self, monkeypatch, tmp_path, role):
        state = _make_state(monkeypatch, tmp_path)
        _land(state, _bind(), _row(role))
        assert _member_notes(state) == []

    def test_a_row_the_gateway_wrote_is_not_the_crewmate_speaking(self, monkeypatch, tmp_path):
        # A compaction notice, a cleared-conversation line or an injected result
        # lands in the thread as an assistant row the turn did not record.
        state = _make_state(monkeypatch, tmp_path)
        slot = _bind()
        _land(state, slot, gateway=(_row("assistant", "Context compacted at 80%."),))
        assert _member_notes(state) == []
        _land(
            state,
            slot,
            _row("assistant", "Here is the summary."),
            gateway=(_row("assistant", "Context compacted at 80%."),),
        )
        [note] = _member_notes(state)
        assert note["body"] == "Here is the summary."

    def test_appending_a_row_alone_does_not_notify(self, monkeypatch, tmp_path):
        # The row hook records the row for the roster; the note waits for the turn.
        state = _make_state(monkeypatch, tmp_path)
        state._record_member_row(_bind(), _row("assistant"))
        assert _member_notes(state) == []

    # "\u200b" is the say-nothing reply a quiet check-in turn ends on: truthy,
    # untouched by str.strip(), and never drawn by the thread.
    @pytest.mark.parametrize("content", ["", "   \n", "\u200b"])
    def test_an_assistant_row_without_text_does_not_notify(self, monkeypatch, tmp_path, content):
        state = _make_state(monkeypatch, tmp_path)
        _land(state, _bind(), _row("assistant", content))
        assert _member_notes(state) == []

    def test_an_ordinary_chat_does_not_notify(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        _bind()
        _land(state, "chat-7-1700000000", _row("assistant"))
        assert state._notification_log == []

    def test_a_member_slot_with_no_binding_does_not_notify(self, monkeypatch, tmp_path):
        # Attribution comes only from the live binding; a key alone names no one.
        state = _make_state(monkeypatch, tmp_path)
        _land(state, member_slot_key("ghost"), _row("assistant"))
        assert state._notification_log == []


class TestChannel:
    def test_member_is_a_system_channel_with_passive_priority(self):
        assert SYSTEM_CHANNELS[MEMBER_CHANNEL] == "passive"

    def test_muting_the_channel_silences_the_note(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update(MEMBER_CHANNEL, muted=True)
        _land(state, _bind(), _row("assistant"))
        [note] = _member_notes(state)
        assert note["silenced"] is True
        assert note["priority"] == "passive"
        assert state._unread_count == 0

    def test_raising_the_channel_gives_the_note_that_priority(self, monkeypatch, tmp_path):
        # Passive is the channel's default, not a cap: the user can opt in.
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update(MEMBER_CHANNEL, priority="default")
        _land(state, _bind(), _row("assistant"))
        [note] = _member_notes(state)
        assert note["priority"] == "default"
        assert "silenced" not in note
        assert state._unread_count == 1

    def test_muting_agent_notes_leaves_crewmate_messages_alone(self, monkeypatch, tmp_path):
        state = _make_state(monkeypatch, tmp_path)
        state.notification_channel_settings.update("system.agent", muted=True)
        _land(state, _bind(), _row("assistant"))
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


def _runner_state(monkeypatch, tmp_path) -> DashboardState:
    """A state a real turn can run on, with crewmate Ada configured.

    Mirrors the member-thread runner harness in ``test_members_dm_thread.py``:
    the crew entry and its private memory are real, the provider is a stream.
    """
    from chat_test_helpers import _make_state as _chat_state

    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
    from kiro_crew.memory_stores import provision_member_memory

    cfg = KiroCrewConfig.load()
    cfg.agents["Ada"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    provision_member_memory(cfg, "Ada")
    cfg.save()
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.title_then_refresh", AsyncMock())
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.generate_session_summary", AsyncMock())
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent", lambda name, project_dir=None: ""
    )
    context = SimpleNamespace(
        ensure_store=AsyncMock(return_value=object()),
        build_message=lambda text, *args, **kwargs: (text, None),
        conversation_log=None,
        hooks=SimpleNamespace(auto_approve_subagent_tools=False),
    )
    state = _chat_state(tmp_path, context_builder=context)
    state._ada_memory_store = cfg.agents["Ada"].memory_store
    return state


def _turn_harness(state: DashboardState, slot_key: str, events: list):
    """A real ``_run_chat`` turn in *slot_key* whose model streams *events*."""
    from kiro_crew.dashboard.chat_runner import _run_chat
    from kiro_crew.member_memory_auth import bind_private_session_store

    state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = _slot(state, slot_key)
    if slot_key.startswith("member-"):
        bind_private_session_store(f"dashboard:{slot_key}", state._ada_memory_store)
    slot._titled = True
    slot.append("user", "Summarize the repo.", "msg msg-u")
    client = state.sessions.get_or_create.return_value[0]
    client.shutdown = AsyncMock()

    async def _stream(_msg):
        for event in events:
            yield event

    client.stream = _stream
    client.stream_command = _stream
    return slot, _run_chat


class TestThroughTheRunner:
    """The runner lands a turn and posts its note: once, with the answer."""

    @pytest.mark.asyncio
    async def test_a_turn_with_a_tool_call_between_segments_posts_one_note(
        self, monkeypatch, tmp_path
    ):
        from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_CALL_UPDATE
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        state = _runner_state(monkeypatch, tmp_path)
        slot_key = _bind()
        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="Let me look."),
            LLMEvent(
                kind=EVENT_TOOL_CALL, title="Terminal", tool_kind="execute", tool_call_id="tc-1"
            ),
            LLMEvent(kind=EVENT_TOOL_CALL_UPDATE, tool_call_id="tc-1", status="completed"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="It has 3 packages."),
        ]
        slot, run_chat = _turn_harness(state, slot_key, events)
        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await run_chat(state, slot, "Summarize the repo.")
        await asyncio.gather(*list(member_message_notes._pending))
        texts = [m["content"] for m in slot.messages if m.get("role") == "assistant"]
        assert "Let me look." in texts and "It has 3 packages." in texts
        [note] = _member_notes(state)
        assert note["body"] == "It has 3 packages."
        assert note["member"] == ADA

    @pytest.mark.parametrize(
        ("stop_reason", "notes"), [(STOP_REASON_END_TURN, 1), (STOP_REASON_CANCELLED, 0)]
    )
    @pytest.mark.asyncio
    async def test_a_turn_the_user_stopped_posts_no_note(
        self, monkeypatch, tmp_path, stop_reason, notes
    ):
        # The user who pressed Stop is on the thread; the end-of-turn row is the control.
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        state = _runner_state(monkeypatch, tmp_path)
        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="Half an answer."),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=stop_reason),
        ]
        slot, run_chat = _turn_harness(state, _bind(), events)
        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await run_chat(state, slot, "Summarize the repo.")
        await asyncio.gather(*list(member_message_notes._pending))
        assert "Half an answer." in [m["content"] for m in slot.messages]
        assert len(_member_notes(state)) == notes

    @pytest.mark.asyncio
    async def test_an_ordinary_chat_turn_posts_no_member_note(self, monkeypatch, tmp_path):
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        state = _runner_state(monkeypatch, tmp_path)
        _bind()
        slot, run_chat = _turn_harness(
            state, "chat-7-1700000000", [LLMEvent(kind=EVENT_TEXT_CHUNK, text="Hi.")]
        )
        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await run_chat(state, slot, "Summarize the repo.")
        await asyncio.gather(*list(member_message_notes._pending))
        assert _member_notes(state) == []
