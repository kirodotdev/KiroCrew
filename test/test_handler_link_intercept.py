"""Tests for handler.py: !link-to-dashboard command and linked thread intercept."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest


def _make_slack():
    """Create a fully async-mocked Slack client."""
    slack = MagicMock()
    slack.post_message = AsyncMock()
    slack.post_blocks = AsyncMock()
    return slack


# ── !link-to-dashboard command tests ──


class TestLinkToDashboardCommand:
    """Cover handler.py lines 994-1011."""

    @pytest.mark.asyncio
    async def test_no_dashboard_state(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        with (
            patch.object(handler, "_dashboard_state", None),
            patch.object(handler, "is_allowed_user", return_value=True),
        ):
            result = await handler._handle_slash_command(
                "!link-to-dashboard",
                slack,
                MagicMock(),
                "C1",
                "t1",
                "msg1",
                "t1",
                "U1",
            )
        assert result == ""
        assert any("not available" in str(c).lower() for c in slack.post_message.call_args_list)

    @pytest.mark.asyncio
    async def test_not_in_thread(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        ds = MagicMock()
        ds.get_or_create_slot = MagicMock()
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
        ):
            result = await handler._handle_slash_command(
                "!link-to-dashboard",
                slack,
                MagicMock(),
                "C1",
                "msg1",
                "msg1",
                "msg1",
                "U1",
            )
        assert result == ""
        assert any("thread" in str(c).lower() for c in slack.post_message.call_args_list)

    @pytest.mark.asyncio
    async def test_empty_thread_returns_error(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        ds = MagicMock()
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch(
                "kiro_crew.slack.interactions._import_thread_to_slot",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await handler._handle_slash_command(
                "!link-to-dashboard",
                slack,
                MagicMock(),
                "C1",
                "t1",
                "msg1",
                "t1",
                "U1",
            )
        assert result == ""
        assert any("could not" in str(c).lower() for c in slack.post_message.call_args_list)

    @pytest.mark.asyncio
    async def test_unauthorized_user_blocked(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        with patch.object(handler, "is_allowed_user", return_value=False):
            result = await handler._handle_slash_command(
                "!link-to-dashboard",
                slack,
                MagicMock(),
                "C1",
                "t1",
                "msg1",
                "t1",
                "UBAD",
            )
        assert result == ""
        assert any("not authorized" in str(c).lower() for c in slack.post_message.call_args_list)

    @pytest.mark.asyncio
    async def test_success_emits_sel_audit(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        ds = MagicMock()
        slot = MagicMock()
        slot.key = "s1"
        slot.messages = [{"role": "user", "content": "hi"}]
        mock_sel_inst = MagicMock()
        orig_sel = handler.sel
        handler.sel = lambda: mock_sel_inst
        try:
            with (
                patch.object(handler, "_dashboard_state", ds),
                patch.object(handler, "is_allowed_user", return_value=True),
                patch(
                    "kiro_crew.slack.interactions._import_thread_to_slot",
                    new_callable=AsyncMock,
                    return_value=slot,
                ),
            ):
                result = await handler._handle_slash_command(
                    "!link-to-dashboard",
                    slack,
                    MagicMock(),
                    "C1",
                    "t1",
                    "msg1",
                    "t1",
                    "U1",
                )
        finally:
            handler.sel = orig_sel
        assert result == ""
        mock_sel_inst.log_tool_invocation.assert_called_once()
        kw = mock_sel_inst.log_tool_invocation.call_args[1]
        assert kw["tool_name"] == "link_to_dashboard"
        assert kw["outcome"] == "success"


# ── Linked thread intercept tests ──


class TestLinkedThreadIntercept:
    """Cover handler.py lines 1323-1345."""

    @pytest.mark.asyncio
    async def test_unauthorized_user_denied_with_sel(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        ds = MagicMock()
        _slot = MagicMock(key="slot1")
        type(_slot).running = PropertyMock(return_value=False)
        ds.get_linked_slot = MagicMock(return_value=_slot)
        mock_sel_inst = MagicMock()
        orig_sel = handler.sel
        handler.sel = lambda: mock_sel_inst
        try:
            with (
                patch.object(handler, "_dashboard_state", ds),
                patch.object(handler, "is_allowed_user", return_value=False),
            ):
                await handler.handle_message(
                    slack,
                    MagicMock(),
                    "C1",
                    "hello",
                    "t1",
                    "msg1",
                    "UBAD",
                )
                mock_sel_inst.log_tool_invocation.assert_called_once()
                kw = mock_sel_inst.log_tool_invocation.call_args[1]
                assert kw["outcome"] == "denied"
                assert kw["metadata"]["user_id"] == "UBAD"
        finally:
            handler.sel = orig_sel

    @pytest.mark.asyncio
    async def test_authorized_routes_to_slot_not_running(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        slot = MagicMock()
        type(slot).running = PropertyMock(return_value=False)
        slot.key = "slot1"
        slot._queue = []
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=slot)
        ds._background_tasks = set()
        ds.broadcast_ws = MagicMock()
        ds.push_slots_update = MagicMock()

        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as mock_run_chat,
        ):
            await handler.handle_message(
                slack,
                MagicMock(),
                "C1",
                "hello",
                "t1",
                "msg1",
                "U1",
            )
            slot.append.assert_called_once()
            mock_run_chat.assert_called_once()
            ds.broadcast_ws.assert_called_once()
            ds.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_redact_for_ui_original_for_llm(self):
        """Verify redacted text goes to UI (slot.append) but original goes to LLM (_run_chat)."""
        from kiro_crew.slack import handler

        slack = _make_slack()
        slot = MagicMock()
        type(slot).running = PropertyMock(return_value=False)
        slot.key = "slot1"
        slot._queue = []
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=slot)
        ds._background_tasks = set()
        ds.broadcast_ws = MagicMock()
        ds.push_slots_update = MagicMock()

        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as mock_run_chat,
            patch.object(
                handler, "redact_exfiltration_urls", return_value=("[REDACTED-URL]", True)
            ),
            patch.object(handler, "redact_credentials", return_value=("[REDACTED]", True)),
        ):
            await handler.handle_message(
                slack,
                MagicMock(),
                "C1",
                "hello http://evil.com",
                "t1",
                "msg1",
                "U1",
            )
            # UI gets redacted text — via append_and_surface, which passes
            # broadcast_user=True so the channel-typed row (never rendered
            # optimistically here) still reaches open dashboard windows through
            # append's own mid-carrying delivery.
            slot.append.assert_called_once_with(
                "user", "[REDACTED]", "msg msg-u", broadcast_user=True, meta=None
            )
            # LLM gets original text
            assert mock_run_chat.call_args[0][2] == "hello http://evil.com"

    @pytest.mark.asyncio
    async def test_authorized_queues_when_running(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        slot = MagicMock()
        type(slot).running = PropertyMock(return_value=True)
        slot.key = "slot1"
        slot._queue = []

        def queue_append(content, *, meta=None, directive_user_origin, directive_channel_origin):
            assert directive_user_origin is True
            assert directive_channel_origin is True
            # The linked-thread enqueue stamps the admission-time containment
            # snapshot so the drain can re-assert it at delivery.
            from kiro_crew.dashboard.session_control import QUEUED_CONTAINMENT_META_KEY

            assert isinstance(meta, dict) and QUEUED_CONTAINMENT_META_KEY in meta
            slot._queue.append({"id": "test", "content": content})
            return "test"

        slot.queue_append = queue_append
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=slot)
        ds.broadcast_ws = MagicMock()
        ds.push_slots_update = MagicMock()

        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as mock_run_chat,
        ):
            await handler.handle_message(
                slack,
                MagicMock(),
                "C1",
                "hello",
                "t1",
                "msg1",
                "U1",
            )
            assert len(slot._queue) == 1
            mock_run_chat.assert_not_called()


# ── Linked thread intercept on the messaging-transport path ──


class TestTransportLinkedThreadIntercept:
    """The transport path (handle_message_transport) must route linked threads
    to their dashboard slot via the shared maybe_route_linked_thread helper,
    identically to native — otherwise /kirocrew link-to-dashboard silently
    breaks under default-ON."""

    @pytest.mark.asyncio
    async def test_transport_authorized_routes_to_slot(self):
        from kiro_crew.slack import handler, transport_dispatch

        slack = _make_slack()
        slot = MagicMock()
        type(slot).running = PropertyMock(return_value=False)
        slot.key = "slot1"
        slot._queue = []
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=slot)
        ds._background_tasks = set()
        ds.broadcast_ws = MagicMock()
        ds.push_slots_update = MagicMock()
        # Booby-trap: the transport must NOT acquire a session for a linked thread.
        sessions = MagicMock()
        sessions.get_or_create = AsyncMock(side_effect=AssertionError("session acquired"))

        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as mock_run_chat,
        ):
            await transport_dispatch.handle_message_transport(
                slack,
                sessions,
                "C1",
                "hello",
                "t1",
                "msg1",
                "U1",
            )
            slot.append.assert_called_once()
            mock_run_chat.assert_called_once()
            ds.push_slots_update.assert_called_once()
            sessions.get_or_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_transport_unauthorized_denied(self):
        from kiro_crew.slack import handler, transport_dispatch

        slack = _make_slack()
        _slot = MagicMock(key="slot1")
        type(_slot).running = PropertyMock(return_value=False)
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=_slot)
        mock_sel_inst = MagicMock()
        orig_sel = handler.sel
        handler.sel = lambda: mock_sel_inst
        sessions = MagicMock()
        sessions.get_or_create = AsyncMock(side_effect=AssertionError("session acquired"))
        try:
            with (
                patch.object(handler, "_dashboard_state", ds),
                patch.object(handler, "is_allowed_user", return_value=False),
            ):
                await transport_dispatch.handle_message_transport(
                    slack,
                    sessions,
                    "C1",
                    "hello",
                    "t1",
                    "msg1",
                    "UBAD",
                )
                # Denied with SEL audit; no session acquired.
                mock_sel_inst.log_tool_invocation.assert_called_once()
                assert mock_sel_inst.log_tool_invocation.call_args[1]["outcome"] == "denied"
                assert any(
                    "not authorized" in str(c).lower() for c in slack.post_message.call_args_list
                )
                sessions.get_or_create.assert_not_called()
        finally:
            handler.sel = orig_sel


# ── Bare `sessions` keyword fall-through in a linked thread ──


class TestSessionsKeywordFallThrough:
    """The bare ``sessions`` keyword must win over a linked dashboard DM, the
    same way ``!``-bang commands fall through — otherwise the native session
    picker is unreachable in a linked thread."""

    def _linked_ds(self):
        slot = MagicMock()
        type(slot).running = PropertyMock(return_value=False)
        slot.key = "slot1"
        slot._queue = []
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=slot)
        ds._background_tasks = set()
        ds.broadcast_ws = MagicMock()
        ds.push_slots_update = MagicMock()
        return ds, slot

    @pytest.mark.asyncio
    async def test_bare_sessions_falls_through_not_routed(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        ds, slot = self._linked_ds()
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
        ):
            result = await handler.maybe_route_linked_thread(
                "sessions", "slack:t1", "U1", "C1", slack, "t1"
            )
        # Falls through to normal handling: no user row appended, no queueing,
        # no dashboard broadcast — the caller's keyword branch takes over.
        assert result is False
        slot.append.assert_not_called()
        slot.queue_append.assert_not_called()
        ds.push_slots_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_exact_sessions_text_still_routed(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        ds, slot = self._linked_ds()
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock),
        ):
            result = await handler.maybe_route_linked_thread(
                "sessions please", "slack:t1", "U1", "C1", slack, "t1"
            )
        # The predicate is exact-match only: anything else keeps routing to
        # the linked slot, pinning the narrowing.
        assert result is True
        slot.append.assert_called_once()

    @pytest.mark.asyncio
    async def test_unauthorized_sessions_still_denied(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        ds, slot = self._linked_ds()
        mock_sel_inst = MagicMock()
        orig_sel = handler.sel
        handler.sel = lambda: mock_sel_inst
        try:
            with (
                patch.object(handler, "_dashboard_state", ds),
                patch.object(handler, "is_allowed_user", return_value=False),
            ):
                result = await handler.maybe_route_linked_thread(
                    "sessions", "slack:t1", "UBAD", "C1", slack, "t1"
                )
            # The auth deny stays ahead of the keyword fall-through: an
            # unauthorized sender gets the denial, not the session picker.
            assert result is True
            kw = mock_sel_inst.log_tool_invocation.call_args[1]
            assert kw["outcome"] == "denied"
            assert any(
                "not authorized" in str(c).lower() for c in slack.post_message.call_args_list
            )
        finally:
            handler.sel = orig_sel

    @pytest.mark.asyncio
    async def test_pinned_options_answer_sessions_still_delivered(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        ds, slot = self._linked_ds()
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock),
        ):
            result = await handler.maybe_route_linked_thread(
                "sessions",
                "slack:t1",
                "U1",
                "C1",
                slack,
                "t1",
                target_slot=slot,
                route_pinned=True,
            )
        # A pinned OPTIONS answer whose label text is exactly "sessions" is a
        # DELIVERY to the conversation that asked the question — it must reach
        # the pinned slot, not be swallowed by the keyword fall-through.
        assert result is True
        slot.append.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_message_reaches_sessions_command_when_linked(self):
        from kiro_crew.slack import handler

        slack = _make_slack()
        ds, slot = self._linked_ds()
        mock_sel_inst = MagicMock()
        orig_sel = handler.sel
        handler.sel = lambda: mock_sel_inst
        try:
            with (
                patch.object(handler, "_dashboard_state", ds),
                patch.object(handler, "is_allowed_user", return_value=True),
                patch.object(handler, "is_owner", return_value=True),
                patch.object(
                    handler, "_handle_sessions_command", new_callable=AsyncMock
                ) as mock_cmd,
            ):
                await handler.handle_message(
                    slack,
                    MagicMock(),
                    "C1",
                    "sessions",
                    "t1",
                    "msg1",
                    "U1",
                )
            # End to end: the keyword wins over the linked DM — the native
            # session picker path runs and the slot gets no user row.
            mock_cmd.assert_awaited_once()
            slot.append.assert_not_called()
        finally:
            handler.sel = orig_sel


# ── Mid-turn link: turn-end mirror re-resolve ──


class TestMidTurnLinkMirror:
    """The turn-end mirror must see a thread linked DURING the turn.

    ``linked_session_key`` commits before the model runs; the early Link to
    Dashboard button makes a mid-turn link the headline case rather than an
    edge case. The mirror re-resolves the thread owner at delivery time, and
    dedups the user message the click-time import already captured.
    """

    def _make_env(self, slot_messages: list[dict], imported_ts: set[str] | None = None):
        from test_slack_handler import FakeProvider, FakeSessionManager

        from kiro_crew.providers.base import LLMEvent

        class LinkingProvider(FakeProvider):
            """Links the thread to a dashboard slot mid-stream (after routing)."""

            def __init__(self, sessions_ref: list):
                super().__init__([LLMEvent(kind="text_chunk", text="final answer")])
                self._sessions_ref = sessions_ref

            async def stream(self, message, timeout=120.0):
                # First event: simulate the user clicking Link to Dashboard —
                # the import has run and the thread index now names the slot.
                self._sessions_ref[0].thread_link = "dashboard:chat-1"
                async for ev in super().stream(message, timeout):
                    yield ev

        class LinkingSessionManager(FakeSessionManager):
            def __init__(self, provider=None):
                super().__init__(provider)
                self.thread_link: str | None = None

            def get_session_for_thread(self, thread_ts):
                return self.thread_link

        sessions_ref: list = []
        provider = LinkingProvider(sessions_ref)
        sessions = LinkingSessionManager(provider)
        sessions_ref.append(sessions)

        slot = MagicMock()
        slot.key = "chat-1"
        slot.messages = slot_messages
        # Explicit (a bare MagicMock attr is truthy): ``None`` exercises the
        # text-scan fallback; a real set exercises the exact ts dedup.
        slot._imported_slack_ts = imported_ts
        slot._on_message = None
        ds = MagicMock()
        ds._slots = {"chat-1": slot}
        # Entry state: the thread is NOT linked when the message arrives — the
        # link happens mid-turn. A truthy return here would fire the entry
        # intercept and route the message away before the turn ever runs.
        ds.get_linked_slot = MagicMock(return_value=None)
        return sessions, slot, ds

    @pytest.mark.asyncio
    async def test_final_answer_mirrored_into_mid_turn_linked_slot(self):
        from kiro_crew.slack import handler

        # Import captured the triggering user message already.
        sessions, slot, ds = self._make_env([{"role": "user", "content": "the question"}])
        from conftest import MockSlackClient

        slack = MockSlackClient()
        with patch.object(handler, "_dashboard_state", ds):
            await handler.handle_message(slack, sessions, "C1", "the question", "100.0", "m2", "U1")

        appended = [(c.args[0], c.args[1]) for c in slot.append.call_args_list]
        # User message deduped (already imported); answer delivered exactly once.
        assert appended == [("assistant", "final answer")]

    @pytest.mark.asyncio
    async def test_user_message_mirrored_when_import_missed_it(self):
        from conftest import MockSlackClient
        from kiro_crew.slack import handler

        sessions, slot, ds = self._make_env([])
        slack = MockSlackClient()
        with patch.object(handler, "_dashboard_state", ds):
            await handler.handle_message(slack, sessions, "C1", "the question", "100.0", "m2", "U1")

        appended = [(c.args[0], c.args[1]) for c in slot.append.call_args_list]
        assert appended == [("user", "the question"), ("assistant", "final answer")]

    @pytest.mark.asyncio
    async def test_followup_does_not_defeat_ts_dedup(self):
        """A mid-turn follow-up after the trigger must not re-append the trigger.

        The import captured both the trigger and a later follow-up; dedup by
        the trigger's exact Slack ts (recorded by the import) is immune to the
        follow-up sitting last — a last-user-only text comparison would miss
        the trigger and duplicate it out of order.
        """
        from conftest import MockSlackClient
        from kiro_crew.slack import handler

        sessions, slot, ds = self._make_env(
            [
                {"role": "user", "content": "the question"},
                {"role": "user", "content": "also do X please"},
            ],
            imported_ts={"m2", "m3"},
        )
        slack = MockSlackClient()
        with patch.object(handler, "_dashboard_state", ds):
            await handler.handle_message(slack, sessions, "C1", "the question", "100.0", "m2", "U1")

        appended = [(c.args[0], c.args[1]) for c in slot.append.call_args_list]
        assert appended == [("assistant", "final answer")]

    @pytest.mark.asyncio
    async def test_text_fallback_scans_all_user_messages(self):
        """Without the ts record, the fallback must scan every user message —
        stopping at the last one would let a follow-up defeat the dedup."""
        from conftest import MockSlackClient
        from kiro_crew.slack import handler

        sessions, slot, ds = self._make_env(
            [
                {"role": "user", "content": "the question"},
                {"role": "user", "content": "also do X please"},
            ],
            imported_ts=None,
        )
        slack = MockSlackClient()
        with patch.object(handler, "_dashboard_state", ds):
            await handler.handle_message(slack, sessions, "C1", "the question", "100.0", "m2", "U1")

        appended = [(c.args[0], c.args[1]) for c in slot.append.call_args_list]
        assert appended == [("assistant", "final answer")]

    @pytest.mark.asyncio
    async def test_unlinked_thread_still_mirrors_nowhere(self):
        from test_slack_handler import FakeProvider, FakeSessionManager

        from conftest import MockSlackClient
        from kiro_crew.providers.base import LLMEvent
        from kiro_crew.slack import handler

        provider = FakeProvider([LLMEvent(kind="text_chunk", text="answer")])
        sessions = FakeSessionManager(provider)
        slot = MagicMock()
        ds = MagicMock()
        ds._slots = {"chat-1": slot}
        ds.get_linked_slot = MagicMock(return_value=None)
        slack = MockSlackClient()
        with patch.object(handler, "_dashboard_state", ds):
            await handler.handle_message(slack, sessions, "C1", "q", "100.0", "m2", "U1")
        slot.append.assert_not_called()
