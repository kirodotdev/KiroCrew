"""Pause / reconnect semantics for a Slack-linked session.

Pause is not unlink. The thread<->session binding, both coordinate fields and the
``_thread_to_session`` reverse index all survive a pause, so inbound routing is
untouched and a reply resumes the SAME session. Only outbound turn mirroring
stops.

Both halves are pinned here because either one alone is a shipped bug: mirroring
that does not stop is a pause that does nothing, and a binding that does not
survive is an unlink wearing a pause label -- the reply then mints a fresh
``slack:<ts>`` session and steals the thread, which is the fork this feature
exists to prevent.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from test_slack_mirror_unlink import (
    _fake_provider,
    _make_slack_client,
)
from test_slack_mirror_unlink import _make_state as _make_runner_state

from kiro_crew.messaging.link import canonical_key
from kiro_crew.session_map import SessionMap

SLACK_TS = "1785370133.085469"
SLACK_KEY = f"slack:{SLACK_TS}"
SLACK_STEM = f"slack_{SLACK_TS}"


@pytest.fixture
def session_map(tmp_path):
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        return SessionMap()


def _pause_app(state):
    """Just the pause + link + unlink routes, on the {slot} spelling."""
    from kiro_crew.dashboard.chat_slack import (
        api_chat_slot_slack_link,
        api_chat_slot_slack_pause,
        api_chat_slot_slack_unlink,
    )

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/slack-link", api_chat_slot_slack_link)
    app.router.add_post("/api/chat/slots/{slot}/slack-unlink", api_chat_slot_slack_unlink)
    app.router.add_post("/api/chat/slots/{slot}/slack-pause", api_chat_slot_slack_pause)
    return app


class TestSessionMapPause:
    """Storage: a presence flag that survives nothing it should not."""

    def test_absent_reads_as_active(self, session_map):
        session_map.set("dash:1", "sid-abc")
        assert session_map.is_slack_paused("dash:1") is False

    def test_pause_then_read(self, session_map):
        session_map.set("dash:1", "sid-abc")
        session_map.set_slack_link("dash:1", SLACK_TS, "C-1")
        assert session_map.set_slack_paused("dash:1", True) is False
        assert session_map.is_slack_paused("dash:1") is True

    def test_resume_removes_the_key_rather_than_storing_false(self, session_map):
        """A resumed session must leave nothing behind, per the house presence-flag style."""
        session_map.set("dash:1", "sid-abc")
        session_map.set_slack_link("dash:1", SLACK_TS, "C-1")
        session_map.set_slack_paused("dash:1", True)
        assert session_map.set_slack_paused("dash:1", False) is True
        assert "slack_paused" not in session_map._data["dash:1"]

    def test_pause_is_idempotent_and_reports_the_prior_state(self, session_map):
        session_map.set("dash:1", "sid-abc")
        session_map.set_slack_link("dash:1", SLACK_TS, "C-1")
        assert session_map.set_slack_paused("dash:1", True) is False
        assert session_map.set_slack_paused("dash:1", True) is True

    def test_pause_retains_the_link_and_the_reverse_index(self, session_map):
        """The whole point: inbound routing must be untouched.

        ``transport_dispatch._resolve_thread_owner`` reroutes a reply on exactly
        this lookup, and its unclaimed-thread guard only self-claims a thread when
        the lookup comes back empty. If pause evicted the index, a reply would
        fork into a new ``slack:<ts>`` session and claim the thread permanently.
        """
        session_map.set("dashboard:chat-1", "sid-abc")
        session_map.set_slack_link("dashboard:chat-1", SLACK_TS, "C-1")
        session_map.set_slack_paused("dashboard:chat-1", True)

        assert session_map.get_slack_link("dashboard:chat-1") == (SLACK_TS, "C-1")
        assert session_map.get_session_for_thread(SLACK_TS) == "dashboard:chat-1"

    def test_pause_covers_both_key_spellings(self, session_map):
        """chat_runner copies a link bare -> "dashboard:"-prefixed when a turn runs.

        A flag written to one spelling and read from the other would silently
        resume a muted thread on the next turn.
        """
        session_map.set("dashboard:chat-1", "sid-a")
        session_map.set("chat-1", "sid-b")
        session_map.set_slack_link("dashboard:chat-1", SLACK_TS, "C-1")
        session_map.set_slack_link("chat-1", SLACK_TS, "C-1")

        session_map.set_slack_paused("dashboard:chat-1", True)

        assert session_map.is_slack_paused("dashboard:chat-1") is True
        assert session_map.is_slack_paused("chat-1") is True, (
            "the bare twin stayed live, so the next turn re-reads it and resumes"
        )

    def test_channel_key_has_no_twin(self, session_map):
        session_map.set(SLACK_KEY, "sid-abc")
        session_map.set_slack_link(SLACK_KEY, SLACK_TS, "C-1")
        session_map.set_slack_paused(SLACK_KEY, True)
        assert session_map.is_slack_paused(SLACK_KEY) is True
        assert session_map.is_slack_paused(canonical_key(SLACK_TS)) is True

    def test_unlink_pops_the_pause_flag(self, session_map):
        """A pause must not outlive the link it describes."""
        session_map.set("dash:1", "sid-abc")
        session_map.set_slack_link("dash:1", SLACK_TS, "C-1")
        session_map.set_slack_paused("dash:1", True)

        assert session_map.clear_slack_link("dash:1") is True

        assert "slack_paused" not in session_map._data["dash:1"]
        assert session_map.is_slack_paused("dash:1") is False

    def test_relink_after_unlink_is_not_silently_paused(self, session_map):
        """The regression the pop above prevents."""
        session_map.set("dash:1", "sid-abc")
        session_map.set_slack_link("dash:1", SLACK_TS, "C-1")
        session_map.set_slack_paused("dash:1", True)
        session_map.clear_slack_link("dash:1")

        session_map.set_slack_link("dash:1", "1785999999.000001", "C-2")

        assert session_map.is_slack_paused("dash:1") is False

    def test_pause_survives_a_reload_from_disk(self, session_map, tmp_path):
        session_map.set("dash:1", "sid-abc")
        session_map.set_slack_link("dash:1", SLACK_TS, "C-1")
        session_map.set_slack_paused("dash:1", True)

        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            reloaded = SessionMap()

        assert reloaded.is_slack_paused("dash:1") is True
        assert reloaded.get_session_for_thread(SLACK_TS) == "dash:1"


class TestPauseStopsOutboundMirroring:
    """Turn level, driven through the real mirror gate with a real SessionMap."""

    @pytest.mark.asyncio
    async def test_linked_turn_mirrors_then_paused_turn_does_not(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _history_key_for, _run_chat

        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())

        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            smap = SessionMap()

        state = _make_runner_state(tmp_path, smap)
        # The pause accessors are what the gate consults; route them at the real map.
        state.sessions.set_slack_paused = smap.set_slack_paused
        state.sessions.is_slack_paused = smap.is_slack_paused
        state.slack_client = _make_slack_client()
        state.sessions.get_or_create = AsyncMock(
            return_value=(_fake_provider(), False, False)
        )

        slot = state.get_or_create_slot("s1")
        session_key = _history_key_for(slot.key)
        state.sessions.set_slack_link(session_key, "thread-1", "C-1")
        slot._slack_linked = True
        slot._slack_channel = "C-1"
        slot._slack_thread_ts = "thread-1"

        # Live turn: mirrors (status quo, and proves the fixture reaches the gate).
        await _run_chat(state, slot, "first message")
        assert state.slack_client.post_message.await_count >= 1
        assert state.slack_client.start_stream.await_count == 1

        state.sessions.set_slack_paused(session_key, True)
        state.slack_client.post_message.reset_mock()
        state.slack_client.start_stream.reset_mock()

        await _run_chat(state, slot, "second message")
        assert state.slack_client.post_message.await_count == 0, (
            "a paused thread still received the user-message echo"
        )
        assert state.slack_client.start_stream.await_count == 0, (
            "a paused thread still opened a tool stream"
        )

    @pytest.mark.asyncio
    async def test_pause_does_not_evict_the_slot_from_the_thread_index(
        self, tmp_path, monkeypatch
    ):
        """``get_linked_slot`` drops the index entry when ``_slack_linked`` is false.

        Expressing pause by clearing that boolean would make the slot self-purge,
        so a resumed turn would never render in the open dashboard tab.
        """
        from kiro_crew.dashboard.chat import _history_key_for

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            smap = SessionMap()

        state = _make_state(tmp_path)
        state.sessions.set_slack_paused = smap.set_slack_paused
        state.sessions.is_slack_paused = smap.is_slack_paused
        state.sessions.get_slack_link = smap.get_slack_link
        state.sessions.set_slack_link = smap.set_slack_link
        state.push_slots_update = MagicMock()

        slot = state.get_or_create_slot("s1")
        session_key = _history_key_for(slot.key)
        smap.set(session_key, "sid-abc")
        state.link_slack(slot.key, "thread-1", "C-1")

        smap.set_slack_paused(session_key, True)

        assert state.get_linked_slot("thread-1") is slot
        assert slot._slack_linked is True
        assert slot._slack_channel == "C-1"
        assert slot._slack_thread_ts == "thread-1"


class TestReplyResumes:
    @pytest.mark.asyncio
    async def test_resume_helper_lifts_the_flag(self, session_map):
        from kiro_crew.slack.handler import _resume_if_paused

        session_map.set("dashboard:chat-1", "sid-abc")
        session_map.set_slack_link("dashboard:chat-1", SLACK_TS, "C-1")
        session_map.set_slack_paused("dashboard:chat-1", True)

        await _resume_if_paused(session_map, "dashboard:chat-1", SLACK_TS)

        assert session_map.is_slack_paused("dashboard:chat-1") is False

    @pytest.mark.asyncio
    async def test_resume_is_a_noop_on_a_live_link(self, session_map):
        """Guarded on the read so the persisting write runs only on the transition."""
        from kiro_crew.slack.handler import _resume_if_paused

        session_map.set("dashboard:chat-1", "sid-abc")
        session_map.set_slack_link("dashboard:chat-1", SLACK_TS, "C-1")
        session_map.set_slack_paused = MagicMock()  # type: ignore[method-assign]

        await _resume_if_paused(session_map, "dashboard:chat-1", SLACK_TS)

        session_map.set_slack_paused.assert_not_called()

    @pytest.mark.asyncio
    async def test_resume_swallows_a_session_manager_without_the_accessor(self, session_map):
        from kiro_crew.slack.handler import _resume_if_paused

        await _resume_if_paused(object(), "dashboard:chat-1", SLACK_TS)

    def test_both_inbound_paths_call_the_resume_helper(self):
        """The transport path is live by default; the native path is the fallback.

        Wiring only one leaves paused threads permanently muted under the other,
        so pin that both reference the helper.
        """
        import inspect

        from kiro_crew.slack import handler, transport_dispatch

        assert "_resume_if_paused" in inspect.getsource(
            transport_dispatch.handle_message_transport
        )
        assert "_resume_if_paused" in inspect.getsource(handler.handle_message)

    def test_the_resume_sits_between_the_governance_gate_and_the_linked_intercept(self):
        """Order matters, and both neighbours are early-returns.

        `maybe_route_linked_thread` RETURNS for a dashboard-linked thread, so a
        resume placed after it never ran for exactly the case the feature is about:
        the reply was routed into the dashboard slot while Slack stayed muted, so
        the answer never came back and the thread looked dead. And it must stay
        AFTER `channel_inbound_permitted`, because resuming is a persisted side
        effect that must not happen for a message governance goes on to deny.

        Asserted on SOURCE ORDER rather than by driving `handle_message`: reaching
        this point end-to-end needs the whole Slack event scaffold plus a live
        provider, and a harness that elaborate ends up asserting against its own
        stubs. The property here is genuinely positional, so position is what is
        checked.
        """
        import inspect

        from kiro_crew.slack import handler

        src = inspect.getsource(handler.handle_message)
        gate = src.index("channel_inbound_permitted")
        resume = src.index("_resume_if_paused")
        intercept = src.index("maybe_route_linked_thread")

        assert gate < resume, "the resume runs before governance has permitted the message"
        assert resume < intercept, (
            "the resume sits after the linked-thread intercept, which returns first — "
            "a dashboard-linked reply would leave Slack muted and get no answer"
        )


class TestPauseEndpoint:
    @pytest.mark.asyncio
    async def test_pause_posts_a_courtesy_note_once(self, tmp_path, monkeypatch):
        """The note goes INTO the Slack thread, and only on the transition.

        It is the only thing marking why the thread stopped, so a watcher can tell
        a disconnected conversation from a stalled one. It states the fact and
        nothing else: no mention of pausing (a verb no surface uses) and no
        coaching to reply, since resuming that way is treated as a given.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.slack_client = _make_slack_client()
        state.sessions.get_slack_link = MagicMock(return_value=("ts123", "C123"))
        state.sessions.set_slack_paused = MagicMock(side_effect=[False, True])
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(_pause_app(state))) as client:
            first = await client.post("/api/chat/slots/s1/slack-pause")
            assert first.status == 200
            assert (await first.json()) == {"ok": True, "was_paused": False}

            second = await client.post("/api/chat/slots/s1/slack-pause")
            assert second.status == 200
            assert (await second.json()) == {"ok": True, "was_paused": True}

        # Exactly one note across two calls: idempotent re-disconnect stays silent.
        state.slack_client.post_message.assert_awaited_once()
        args = state.slack_client.post_message.await_args.args
        assert args[0] == "C123"
        assert args[2] == "ts123"
        assert "Disconnected" in args[1]
        assert "paus" not in args[1].lower()
        assert "repl" not in args[1].lower()
        assert slot is state.get_or_create_slot("s1")

    @pytest.mark.asyncio
    async def test_pause_on_an_unlinked_session_is_a_conflict(self, tmp_path, monkeypatch):
        """Nothing to mute, and returning ok would leave the UI showing Reconnect."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.slack_client = _make_slack_client()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.sessions.set_slack_paused = MagicMock()
        state.push_slots_update = MagicMock()

        async with TestClient(TestServer(_pause_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-pause")
            assert resp.status == 409

        state.sessions.set_slack_paused.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_slot_is_404(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_pause_app(state))) as client:
            resp = await client.post("/api/chat/slots/nope/slack-pause")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_channel_born_slot_pauses_its_own_session(self, tmp_path, monkeypatch):
        """The key must come from the slot, not its name.

        ``_history_key_for`` would build ``dashboard:slack:<ts>`` here -- a session
        that does not exist -- so the pause would land nowhere and the real thread
        would keep mirroring.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        state.slack_client = _make_slack_client()
        state.sessions.get_slack_link = MagicMock(return_value=(SLACK_TS, "C777"))
        state.sessions.channel_key_for_stem = lambda stem: (
            SLACK_KEY if stem == SLACK_STEM else ""
        )
        state.sessions.set_slack_paused = MagicMock(return_value=False)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot(SLACK_STEM, linked_session_key=SLACK_KEY)

        async with TestClient(TestServer(_pause_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{SLACK_STEM}/slack-pause")
            assert resp.status == 200

        state.sessions.set_slack_paused.assert_called_once_with(SLACK_KEY, True)


class TestReconnect:
    @pytest.mark.asyncio
    async def test_reconnect_reseeds_the_existing_thread(self, tmp_path, monkeypatch):
        """No new thread, pause lifted, backfill fired at the thread we already had."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: MagicMock())
        spawned: list[tuple] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_slack._spawn_slack_backfill",
            lambda state, slot, channel, thread_ts: spawned.append((channel, thread_ts)),
        )
        state = _make_state(tmp_path)
        state.owner_id = "U1"
        state.slack_client = _make_slack_client()
        state.sessions.get_slack_link = MagicMock(return_value=("ts123", "C123"))
        state.sessions.is_slack_paused = MagicMock(return_value=True)
        state.sessions.set_slack_paused = MagicMock(return_value=True)
        state.sessions.set_slack_link = MagicMock()
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_pause_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link")
            assert resp.status == 200
            body = await resp.json()

        assert body["reconnected"] is True
        assert (body["channel"], body["thread_ts"]) == ("C123", "ts123")
        state.sessions.set_slack_paused.assert_called_once_with("dashboard:s1", False)
        assert spawned == [("C123", "ts123")], "reconnect must re-seed the SAME thread"
        # No anchor message: a new thread was never created.
        state.slack_client.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_link_on_a_live_link_is_a_silent_no_op(self, tmp_path, monkeypatch):
        """Not disconnected -> nothing happens, and nothing is said in the thread.

        The note this used to post existed for the "Post reminder in Slack" menu
        item, which called this endpoint on a live link just to ping the thread.
        That item is gone, so the note would only ever appear from a stale tab or a
        direct API call — a stray message that explains nothing to whoever reads it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: MagicMock())
        spawned: list[tuple] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_slack._spawn_slack_backfill",
            lambda state, slot, channel, thread_ts: spawned.append((channel, thread_ts)),
        )
        state = _make_state(tmp_path)
        state.owner_id = "U1"
        state.slack_client = _make_slack_client()
        state.sessions.get_slack_link = MagicMock(return_value=("ts123", "C123"))
        state.sessions.is_slack_paused = MagicMock(return_value=False)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_pause_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link")
            body = await resp.json()

        assert body["already_linked"] is True
        assert "reconnected" not in body
        assert spawned == []
        state.slack_client.post_message.assert_not_awaited()


class TestSerialization:
    def test_channel_born_slot_carries_a_paused_origin_link(self, tmp_path, monkeypatch):
        """The defect this fixes: no Slack row at all, so the UI offered "Send to Slack"."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.sessions.get_slack_link = MagicMock(return_value=(SLACK_TS, "C777"))
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        state.sessions.mirror_accepts_inbound = MagicMock(return_value=False)
        state.sessions.is_slack_paused = MagicMock(return_value=True)
        state.sessions.channel_key_for_stem = lambda stem: (
            SLACK_KEY if stem == SLACK_STEM else ""
        )
        slot = state.get_or_create_slot(SLACK_STEM, linked_session_key=SLACK_KEY)

        payload = state.serialize_slot(slot)

        slack_rows = [x for x in payload["links"] if x["channel"] == "slack"]
        assert len(slack_rows) == 1
        assert slack_rows[0]["direction"] == "origin"
        assert slack_rows[0]["paused"] is True
        # Still false: the frontend rebuilds a phantom mirror row from this flag
        # whenever no Slack wire link is present, and a real row plus a true flag
        # would badge the same conversation twice.
        assert payload["slack_linked"] is False

    def test_a_live_channel_born_slot_reports_not_paused(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.sessions.get_slack_link = MagicMock(return_value=(SLACK_TS, "C777"))
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        state.sessions.mirror_accepts_inbound = MagicMock(return_value=False)
        state.sessions.is_slack_paused = MagicMock(return_value=False)
        state.sessions.channel_key_for_stem = lambda stem: (
            SLACK_KEY if stem == SLACK_STEM else ""
        )
        slot = state.get_or_create_slot(SLACK_STEM, linked_session_key=SLACK_KEY)

        slack_rows = [
            x for x in state.serialize_slot(slot)["links"] if x["channel"] == "slack"
        ]
        assert [x["paused"] for x in slack_rows] == [False]

    def test_a_stubbed_session_manager_never_reads_as_paused(self, tmp_path, monkeypatch):
        """``sessions`` is a bare MagicMock across much of the suite.

        Truthiness on an unstubbed accessor would report every link paused and
        silence mirroring everywhere, so the gate demands identity with ``True``.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.sessions.get_slack_link = MagicMock(return_value=(SLACK_TS, "C777"))
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        state.sessions.mirror_accepts_inbound = MagicMock(return_value=False)
        state.sessions.channel_key_for_stem = lambda stem: (
            SLACK_KEY if stem == SLACK_STEM else ""
        )
        slot = state.get_or_create_slot(SLACK_STEM, linked_session_key=SLACK_KEY)

        slack_rows = [
            x for x in state.serialize_slot(slot)["links"] if x["channel"] == "slack"
        ]
        assert [x["paused"] for x in slack_rows] == [False]


class TestResumeNeverPrecedesTheGovernanceGate:
    """A denied inbound message must not leave the channel connected.

    Lifting the pause PERSISTS, so doing it during owner resolution — before
    `channel_inbound_permitted` has had its say — means a message governance goes
    on to drop has already reconnected the link. The deny is silent and the link is
    live, which is the opposite of what a denial means.

    Asserted STRUCTURALLY, on source call order, rather than by driving the whole
    dispatcher: the defect IS the ordering, and an end-to-end harness for this path
    needs the full Slack event envelope, a conversation log and a governance policy
    — at which point the test asserts against its own stubs instead of the code.
    """

    @staticmethod
    def _source(module) -> str:
        import inspect

        return inspect.getsource(module)

    def test_the_transport_dispatch_resumes_after_the_gate(self):
        from kiro_crew.slack import transport_dispatch

        src = self._source(transport_dispatch)
        gate = src.find('channel_inbound_permitted("slack")')
        # The CALL, not the import line, which also contains the bare name.
        resume = src.find("await _resume_if_paused(")
        assert gate != -1, "governance gate not found — did it move or get renamed?"
        assert resume != -1, "resume call not found — did it move or get renamed?"
        assert resume > gate, (
            "the Slack transport path resumes a paused link BEFORE the inbound "
            "governance gate; a denied message would leave the channel connected"
        )

    def test_the_native_handler_resumes_after_the_gate(self):
        from kiro_crew.slack import handler

        src = self._source(handler)
        gate = src.find('channel_inbound_permitted("slack")')
        resume = src.find("await _resume_if_paused(")
        assert gate != -1 and resume != -1
        assert resume > gate, (
            "the native Slack handler resumes a paused link BEFORE the inbound "
            "governance gate"
        )

    def test_the_discord_dispatch_resumes_after_the_gate(self):
        from kiro_crew.discord import transport_dispatch

        src = self._source(transport_dispatch)
        gate = src.find('channel_inbound_permitted("discord")')
        resume = src.find("resume_if_muted(")
        assert gate != -1 and resume != -1
        assert resume > gate, (
            "the Discord path resumes a muted binding BEFORE the inbound "
            "governance gate"
        )


class TestSlackPauseWritesAreOffLoaded:
    """`set_slack_paused` calls `SessionMap._save()`, which serialises the whole map.

    These are async request handlers on the gateway's event loop, so a synchronous
    write stalls every other task — chat turns, the Slack socket, the watchdog — for
    as long as the filesystem takes.
    """

    @pytest.mark.asyncio
    async def test_the_pause_endpoint_offloads_its_write(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import chat_slack

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.slack_client = _make_slack_client()
        state.sessions.get_slack_link = MagicMock(return_value=("ts123", "C123"))
        state.sessions.set_slack_paused = MagicMock(return_value=False)
        state.push_slots_update = MagicMock()

        offloaded: list[str] = []
        original = chat_slack.asyncio.to_thread

        async def _spy(fn, *args, **kwargs):
            offloaded.append(
                getattr(fn, "_mock_name", None) or getattr(fn, "__name__", str(fn))
            )
            return await original(fn, *args, **kwargs)

        monkeypatch.setattr(chat_slack.asyncio, "to_thread", _spy)

        async with TestClient(TestServer(_pause_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-pause")
            assert resp.status == 200

        assert any("set_slack_paused" in name for name in offloaded), (
            f"the pause write was not offloaded; offloaded calls were {offloaded}"
        )


class TestReconnectFailsClosedOnChannelGovernance:
    """Reconnect re-seeds the thread, so a denied channel must not be resumed.

    This path unpauses AND posts transcript history. A `channels` policy that
    denied `slack` after the link was first made would otherwise get session
    history delivered into a prohibited channel — the connect-time gate never sees
    that change, which is exactly why the inbound gate re-checks per message.
    """

    @staticmethod
    def _state(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        state.owner_id = "U1"  # the handler 500s on an unconfigured owner
        state.get_or_create_slot("s1")
        state.slack_client = _make_slack_client()
        state.sessions.get_slack_link = MagicMock(return_value=("ts123", "C123"))
        state.sessions.is_slack_paused = MagicMock(return_value=True)
        state.sessions.set_slack_paused = MagicMock(return_value=True)
        state.link_slack = MagicMock()
        state.push_slots_update = MagicMock()
        return state

    @pytest.mark.asyncio
    async def test_a_denied_channel_is_refused_before_unpausing_or_seeding(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard import chat_slack

        state = self._state(tmp_path, monkeypatch)
        monkeypatch.setattr(
            chat_slack, "slack_mirror_is_paused", lambda *a, **k: True
        )
        monkeypatch.setattr(
            chat_slack,
            "vet_and_audit",
            lambda *a, **k: SimpleNamespace(permitted=False, rule="", layer="", reason=""),
        )
        spawned: list[tuple] = []
        monkeypatch.setattr(
            chat_slack, "_spawn_slack_backfill", lambda *a: spawned.append(a)
        )

        async with TestClient(TestServer(_pause_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link", json={})
            assert resp.status == 403
            assert (await resp.json())["code"] == "channel_not_permitted"

        # Refused BEFORE any side effect: still muted, no re-bind, nothing seeded.
        state.sessions.set_slack_paused.assert_not_called()
        state.link_slack.assert_not_called()
        assert spawned == []

    @pytest.mark.asyncio
    async def test_a_permitted_channel_still_reconnects(self, tmp_path, monkeypatch):
        """Non-vacuity: the gate must not break the ordinary reconnect."""
        from kiro_crew.dashboard import chat_slack

        state = self._state(tmp_path, monkeypatch)
        monkeypatch.setattr(
            chat_slack, "slack_mirror_is_paused", lambda *a, **k: True
        )
        monkeypatch.setattr(
            chat_slack,
            "vet_and_audit",
            lambda *a, **k: SimpleNamespace(permitted=True, rule="", layer="", reason=""),
        )
        spawned: list[tuple] = []
        monkeypatch.setattr(
            chat_slack, "_spawn_slack_backfill", lambda *a: spawned.append(a)
        )

        async with TestClient(TestServer(_pause_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link", json={})
            assert resp.status == 200
            assert (await resp.json())["reconnected"] is True

        state.sessions.set_slack_paused.assert_called_once()
        assert len(spawned) == 1


class TestTheDisconnectNoteIsGoverned:
    """The courtesy note is egress, so a denied channel must not receive it.

    The mute itself is deliberately NOT gated: disconnecting only reduces what
    leaves the process, and refusing it because the channel is denied would strand
    the user connected to a channel they are trying to leave. So a denial silences
    the note and still performs the disconnect.
    """

    @staticmethod
    def _state(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_slack.sel", lambda: MagicMock())
        state = _make_state(tmp_path)
        state.owner_id = "U1"
        state.get_or_create_slot("s1")
        state.slack_client = _make_slack_client()
        state.sessions.get_slack_link = MagicMock(return_value=("ts123", "C123"))
        state.sessions.set_slack_paused = MagicMock(return_value=False)
        state.push_slots_update = MagicMock()
        return state

    @pytest.mark.asyncio
    async def test_a_denied_channel_gets_no_note_but_is_still_disconnected(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard import chat_slack

        state = self._state(tmp_path, monkeypatch)
        monkeypatch.setattr(
            chat_slack,
            "vet_and_audit",
            lambda *a, **k: SimpleNamespace(permitted=False, rule="", layer="", reason=""),
        )

        async with TestClient(TestServer(_pause_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-pause")
            assert resp.status == 200
            assert (await resp.json())["was_paused"] is False

        # The disconnect happened; the note did not.
        state.sessions.set_slack_paused.assert_called_once()
        state.slack_client.post_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_permitted_channel_still_gets_the_note(self, tmp_path, monkeypatch):
        """Non-vacuity: the gate must not silence the ordinary disconnect note."""
        from kiro_crew.dashboard import chat_slack

        state = self._state(tmp_path, monkeypatch)
        monkeypatch.setattr(
            chat_slack,
            "vet_and_audit",
            lambda *a, **k: SimpleNamespace(permitted=True, rule="", layer="", reason=""),
        )

        async with TestClient(TestServer(_pause_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-pause")
            assert resp.status == 200

        state.slack_client.post_message.assert_awaited_once()
        posted = state.slack_client.post_message.await_args[0][1]
        assert "Disconnected" in posted


class TestSlackTurnEgressIsRecheckedPerSend:
    """A reconnected link must stop posting once the policy denies Slack.

    The mirror path re-resolves governance per send inside
    `_resolve_channel_target`, which deliberately skips Slack (its own client and
    streaming path are not a registered transport). So the Slack leg decided egress
    on the policy as it stood when the link was created — and reconnect re-arms a
    link, which means a link authorised at reconnect time kept posting transcript
    content after the policy stopped allowing it. The backfill drain already
    re-checks before every post; this is the same gap on the turn path.
    """

    @staticmethod
    def _state():
        state = SimpleNamespace(sessions=MagicMock())
        return state

    def test_a_denied_policy_denies_egress(self, monkeypatch):
        from kiro_crew.dashboard import chat_utils

        monkeypatch.setattr(
            chat_utils,
            "vet_and_audit",
            lambda *a, **k: SimpleNamespace(permitted=False),
            raising=False,
        )
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            lambda *a, **k: SimpleNamespace(permitted=False),
        )

        assert chat_utils.slack_turn_egress_denied(self._state(), "dashboard:chat-1") is True

    def test_a_permitted_policy_allows_egress(self, monkeypatch):
        """Non-vacuity: the gate must not mute an ordinary allowed thread."""
        from kiro_crew.dashboard import chat_utils

        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            lambda *a, **k: SimpleNamespace(permitted=True),
        )

        assert chat_utils.slack_turn_egress_denied(self._state(), "dashboard:chat-1") is False

    def test_an_unevaluable_policy_denies(self, monkeypatch):
        """Fail CLOSED. This is a network send, not a UI affordance.

        Deliberately the opposite of `slack_mirror_is_paused`, which fails OPEN so a
        bare MagicMock cannot make every thread look muted. A policy that cannot be
        evaluated is not a policy that permits.
        """
        from kiro_crew.dashboard import chat_utils

        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            MagicMock(side_effect=RuntimeError("policy unreadable")),
        )

        assert chat_utils.slack_turn_egress_denied(self._state(), "dashboard:chat-1") is True

    def test_a_decision_without_permitted_denies(self, monkeypatch):
        """An answer that does not say yes is not a yes."""
        from kiro_crew.dashboard import chat_utils

        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            lambda *a, **k: SimpleNamespace(),
        )

        assert chat_utils.slack_turn_egress_denied(self._state(), "dashboard:chat-1") is True

    def test_every_pause_gated_turn_site_also_asks_the_policy(self):
        """Every outbound Slack op asks BOTH the mute and the policy.

        The streamed tool updates used to ask the mute alone, to avoid a SEL record
        per token batch. That left tool titles appending to the stream after the
        policy had denied Slack, so the full check is now asked per event and the cost
        is bounded by a denial latch instead of by asking a weaker question.
        """
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner)
        # Pinned to the GUARDS, not to the helpers existing: `"_slack_leg_muted()" in
        # src` is satisfied by the `def` line alone, so it passed with every call site
        # deleted — the probe caught that.
        assert "if _mirror_stream_ts and not await _slack_content_denied():" in src, (
            "the streamed tool updates no longer ask mute+policy, so a disconnect or "
            "a policy denial mid-turn would keep appending tool detail"
        )
        assert "and not await _slack_content_denied()\n" in src, (
            "the assistant reply no longer asks the full mute+policy check"
        )
        assert "if _index and await _slack_content_denied():" in src, (
            "the Slack reply's parts after the first no longer recheck, so a policy "
            "denial mid-reply would keep posting the remaining parts"
        )
        assert (
            "if _mirror_options and not _stopped and not await _slack_content_denied():"
            in src
        ), (
            "the options block posts without rechecking, so a denial during the "
            "reply still yields interactive controls in the thread"
        )
        assert "if _mirror_active_task and not await _slack_content_denied():" in src, (
            "the stream teardown no longer asks before its content append"
        )
        # No outbound op may ask the mute alone any more.
        assert "not _slack_leg_muted()" not in src, (
            "an outbound Slack op asks only the mute, so a policy denial mid-turn "
            "would not stop it"
        )
        # The leg also ends when the Slack link MOVES, not only when it is muted or
        # removed: `_mirror_chan`/`_mirror_thread` were captured at the top of the
        # turn, so an unlink-and-rebind leaves them addressing a thread that now
        # belongs to someone else. Pinned as the comparison, because a `return False`
        # there passes every mute-based test.
        assert '!= (_mirror_chan or "")' in src, (
            "the Slack leg stopped comparing its link against the channel this turn "
            "captured, so a rebind mid-turn keeps posting to the old thread"
        )
        # Pinned to the ASSIGNMENT, not the name: `"_slack_policy_denied_latch" in
        # src` is satisfied by the declaration alone, so it passed with the latch
        # never set — the probe caught that (twice now, same trap).
        assert "if denied:\n            _slack_policy_denied_latch = True" in src, (
            "the per-event policy check has no denial latch, so a denied turn keeps "
            "paying for a policy read and a SEL record on every event"
        )
        assert "if _slack_policy_denied_latch:\n            return True" in src, (
            "the latch is set but never read, so it saves nothing"
        )

    def test_the_policy_check_never_runs_on_the_event_loop(self):
        """It reads policy files and writes SEL, so it must be offloaded.

        Called inline it stalls the gateway's chat loop and liveness heartbeat on
        every Slack-linked turn — the same reason the session-map writes and the
        backfill's own governance check in this feature are offloaded.

        Asserted as "no inline call" rather than by counting offloaded ones: a count
        is bookkeeping that breaks on every legitimate addition while still passing
        if someone adds an inline call alongside.
        """
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner)
        assert "slack_turn_egress_denied(state, session_key)" not in src, (
            "the governance check is called inline; it performs filesystem I/O and "
            "must go through asyncio.to_thread"
        )
        assert "asyncio.to_thread(slack_turn_egress_denied" in src or (
            "slack_turn_egress_denied, state, session_key" in src
        )

    def test_the_mute_is_re_asked_after_the_offloaded_policy_read(self):
        """The offload is a window, and the mute answer above it goes stale inside it.

        `_slack_leg_muted()` is checked before the policy read, and that read is disk
        I/O on a worker thread. An unlink or mute landing inside it leaves the earlier
        answer stale, so a "permitted" verdict computed before the disconnect would
        license the send after it. Structural: the gate is a closure over a turn's
        locals that a harness cannot reach without rebuilding the whole turn. Pinned to
        the RETURN, not the helper name, so a mere mention does not satisfy it.
        """
        import inspect

        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner)
        assert (
            "            _slack_policy_denied_latch = True\n            return True\n"
        ) in src, (
            "the policy denial no longer returns immediately, so the recheck below it "
            "is not what decides a permitted send"
        )
        assert "        return _slack_leg_muted()\n" in src, (
            "the gate returns the policy verdict computed BEFORE the await, so a "
            "disconnect during the policy read still licenses the send"
        )
