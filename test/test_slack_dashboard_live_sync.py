"""A Slack turn must reach its dashboard tab as it happens, both directions.

Two symptoms motivated these tests, and both were delivery bugs rather than
persistence bugs -- in each case the transcript on disk was already correct:

* A Slack-born conversation's tab showed nothing while the agent worked, then
  the reply, then (only after a reload) the message that had prompted it.
* A dashboard session mirrored out to Slack answered in Slack but never updated
  the tab at all.

Every test here fails before the change.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN
from kiro_crew.dashboard.channel_slots import refresh_channel_window, surface_channel_session
from kiro_crew.history import ConversationLog, transcript_sort_key
from kiro_crew.messaging.link import canonical_key
from kiro_crew.slack import transport_dispatch

_test_dir = Path(__file__).parent
if str(_test_dir) not in sys.path:  # pragma: no cover
    sys.path.insert(0, str(_test_dir))
_golden = importlib.import_module("test_slack_golden_transcript")

FakeSessions = _golden.FakeSessions
RecordingSlackClient = _golden.RecordingSlackClient
ScriptedProvider = _golden.ScriptedProvider
make_event = _golden.make_event

_MSG_TS = "1700000000.000100"
_SESSION_KEY = canonical_key(_MSG_TS)

SLACK_KEY = "slack:1785370133.085469"
SLACK_STEM = "slack_1785370133.085469"


def _recorder(slot):
    """Capture what the slot would push to an open tab over the websocket."""
    seen: list[dict] = []
    slot._on_message = lambda key, msg: seen.append(msg)
    slot._has_reader = False
    return seen


class TestAUserRowReachesTheTab:
    """The message a person typed in Slack must render, not just the reply."""

    def test_a_locally_typed_user_row_is_still_not_broadcast(self, tmp_path, monkeypatch):
        # The default must not change: the dashboard composer already rendered
        # its own message optimistically, so broadcasting it would double it.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        seen = _recorder(slot)

        slot.append("user", "typed here")

        assert [m["role"] for m in seen] == []

    def test_a_channel_user_row_is_broadcast_when_requested(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        seen = _recorder(slot)

        slot.append("user", "from slack", broadcast_user=True)

        assert [(m["role"], m["content"]) for m in seen] == [("user", "from slack")]

    def test_assistant_rows_are_unaffected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        seen = _recorder(slot)

        slot.append("assistant", "reply")

        assert [m["role"] for m in seen] == ["assistant"]


class TestBTheWindowRefreshDeliversBothRoles:
    """The refresh that catches a tab up must not drop the user's message."""

    @pytest.fixture
    def log(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.history.config_dir", lambda: tmp_path)
        return ConversationLog()

    def _surfaced_slot(self, tmp_path, monkeypatch, log):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.sessions.channel_key_for_stem = lambda stem: (
            SLACK_KEY if stem == SLACK_STEM else ""
        )
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        path = Path(log._dir)
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{SLACK_STEM}.jsonl").write_text(
            json.dumps({"_type": "metadata", "created_at": "2026-08-01T10:00:00"}) + "\n",
            encoding="utf-8",
        )
        log.append(SLACK_KEY, "user", "first")
        log.append(SLACK_KEY, "assistant", "first reply")
        slot = surface_channel_session(
            state,
            {"key": SLACK_STEM, "modified": 100.0},
            {},
            log.read_messages(SLACK_KEY),
            session_key=SLACK_KEY,
        )
        assert slot is not None
        return slot

    def test_a_later_slack_turn_broadcasts_the_user_row_too(self, tmp_path, monkeypatch, log):
        slot = self._surfaced_slot(tmp_path, monkeypatch, log)
        seen = _recorder(slot)

        # The Slack transport writes the next turn straight to the shared file.
        log.append(SLACK_KEY, "user", "second question")
        log.append(SLACK_KEY, "assistant", "second reply")
        added = refresh_channel_window(slot, log.read_messages(SLACK_KEY), 200.0)

        assert added == 2
        # Before the fix only the assistant row was pushed, so the tab showed a
        # reply to a question that was not on screen.
        assert [(m["role"], m["content"]) for m in seen] == [
            ("user", "second question"),
            ("assistant", "second reply"),
        ]


class TestCTheMirrorIsResolvableInbound:
    """Sending a session to Slack must make replies findable again."""

    def _app(self, state):
        from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_link

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/slack-link", api_chat_slot_slack_link)
        return app

    @pytest.mark.asyncio
    async def test_link_registers_the_inbound_reverse_index(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()
        state.slack_client = MagicMock()
        state.slack_client.open_dm = AsyncMock(return_value="C123")
        state.slack_client.post_message = AsyncMock(return_value="ts123")
        state.owner_id = "U123"
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.sessions.set_slack_link = MagicMock()

        async with TestClient(TestServer(self._app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link", json={})
            assert resp.status == 200

        # The inbound Slack path resolves a thread through get_linked_slot. The
        # old hand-assignment left this index empty, so a reply in the mirrored
        # thread ran the turn but never told the tab.
        assert state.get_linked_slot("ts123") is slot
        # The persisted half must still happen (it survives a restart).
        assert state.sessions.set_slack_link.called

    def test_a_restart_restores_the_index_for_a_mirrored_session(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.sessions.get_slack_link = MagicMock(return_value=("ts777", "C777"))

        slot = state.get_or_create_slot("chat-9-1700000000")

        assert state.get_linked_slot("ts777") is slot

    def test_a_restart_does_not_index_a_channel_born_self_link(self, tmp_path, monkeypatch):
        # A channel-born session's slack_thread_ts points at the thread it LIVES
        # in, not one it mirrors to. Indexing that would route every inbound
        # Slack message into the dashboard chat runner, changing the execution
        # engine and approval semantics of all Slack traffic.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.sessions.get_slack_link = MagicMock(return_value=("1785370133.085469", "C777"))
        state.sessions.channel_key_for_stem = lambda stem: (
            SLACK_KEY if stem == SLACK_STEM else ""
        )

        state.get_or_create_slot(SLACK_STEM, linked_session_key=SLACK_KEY)

        assert state._slack_to_slot == {}
        assert state.get_linked_slot("1785370133.085469") is None

    def test_an_unresolved_channel_stem_is_still_not_indexed(self, tmp_path, monkeypatch):
        # Same danger, harder case: the stem did NOT resolve, so
        # linked_session_key is empty and cannot be the discriminator. The slot
        # is still named for the thread it lives in, which is what catches it.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.sessions.get_slack_link = MagicMock(return_value=("1785370133.085469", "C777"))
        state.sessions.channel_key_for_stem = lambda stem: ""

        state.get_or_create_slot(SLACK_STEM)

        assert state._slack_to_slot == {}

    def test_a_dashboard_slot_named_like_a_channel_is_still_indexed(self, tmp_path, monkeypatch):
        # The guard must not be a name heuristic: a dashboard slot a caller
        # happened to name "slack_notes" is a genuine mirror-out and must still
        # deliver inbound replies to its tab.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.sessions.get_slack_link = MagicMock(return_value=("ts777", "C777"))
        state.sessions.channel_key_for_stem = lambda stem: ""

        slot = state.get_or_create_slot("slack_notes")

        assert state.get_linked_slot("ts777") is slot


def _drive_transport(log, *, driver_cls=None):
    """Run one full Slack transport turn against *log*."""
    slack = RecordingSlackClient()
    provider = ScriptedProvider(
        [
            make_event(EVENT_TEXT_CHUNK, text="the reply"),
            make_event(EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
    )
    sessions = FakeSessions(provider)
    asyncio.run(
        transport_dispatch.handle_message_transport(
            slack=slack,
            sessions=sessions,
            channel="C1",
            text="the question",
            thread_ts=None,
            msg_ts=_MSG_TS,
            user_id="U_OWNER",
            context_builder=None,
            conversation_log=log,
        )
    )


class TestDTheQuestionIsRecordedBeforeTheAnswer:
    """The inbound message must exist while the turn runs, not only after."""

    @pytest.fixture(autouse=True)
    def _quiet_agents(self, monkeypatch):
        monkeypatch.setattr(transport_dispatch, "_get_default_agent", lambda: "")
        monkeypatch.setattr(transport_dispatch, "_hydrate_thread_overrides", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_hydrate_conv_flags", lambda *a, **k: None)
        monkeypatch.setattr(transport_dispatch, "_thread_agents", {})

    def test_the_user_row_is_on_disk_when_the_turn_starts(self, tmp_path, monkeypatch):
        log = ConversationLog(base_dir=tmp_path)
        seen_at_turn_start: list[list[dict]] = []

        real_driver = transport_dispatch.TurnDriver

        class _Snapshotting(real_driver):  # type: ignore[misc, valid-type]
            async def run(self, message):  # type: ignore[override]
                seen_at_turn_start.append(log.read_messages(_SESSION_KEY))
                return await super().run(message)

        monkeypatch.setattr(transport_dispatch, "TurnDriver", _Snapshotting)
        _drive_transport(log)

        assert seen_at_turn_start, "the turn never ran"
        # Before the fix both rows were written together AFTER the turn, so the
        # transcript was empty at this point and the tab had nothing to show.
        assert [(m["role"], m["content"]) for m in seen_at_turn_start[0]] == [
            ("user", "the question")
        ]

    def test_the_finished_turn_has_exactly_one_of_each_row(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)

        _drive_transport(log)

        rows = [(m["role"], m["content"]) for m in log.read_messages(_SESSION_KEY)]
        # Splitting the write must not double the question.
        assert rows == [("user", "the question"), ("assistant", "the reply")]

    def test_the_two_rows_sort_in_the_order_they_happened(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)

        _drive_transport(log)

        rows = log.read_messages(_SESSION_KEY)
        # Asserting the two are merely DIFFERENT was a proxy for this, and it
        # depended on the clock ticking between the two writes -- which on a
        # ~15.6 ms-granularity clock it often does not. Assert what consumers
        # actually need: the question sorts before the answer.
        assert transcript_sort_key(rows[0]["ts"]) < transcript_sort_key(rows[1]["ts"])

    def test_a_failed_receipt_write_still_records_the_whole_turn(self, tmp_path):
        class _FailsFirstAppend(ConversationLog):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.fail_next = True

            def append(self, *a, **k):
                if self.fail_next:
                    self.fail_next = False
                    raise RuntimeError("disk full")
                return super().append(*a, **k)

        log = _FailsFirstAppend(base_dir=tmp_path)

        _drive_transport(log)

        rows = [(m["role"], m["content"]) for m in log.read_messages(_SESSION_KEY)]
        # The fallback exists so a turn is never persisted reply-only.
        assert rows == [("user", "the question"), ("assistant", "the reply")]

    def test_no_transcript_write_happens_on_the_event_loop_thread(self, tmp_path):
        # ConversationLog.append takes a cross-process flock, and on the event
        # loop that primitive makes ONE non-blocking acquire and raises on any
        # concurrent holder -- so an on-loop append both writes to disk on the
        # loop and drops the row under benign contention.
        idents: list[int] = []

        class _ThreadRecordingLog(ConversationLog):
            def append(self, *a, **k):
                idents.append(threading.get_ident())
                return super().append(*a, **k)

        log = _ThreadRecordingLog(base_dir=tmp_path)
        main_ident = threading.get_ident()

        _drive_transport(log)

        assert idents, "no transcript write happened"
        assert main_ident not in idents, (
            "a transcript write ran on the event loop thread: " f"{idents} includes {main_ident}"
        )
