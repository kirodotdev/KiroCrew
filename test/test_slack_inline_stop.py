"""Tests for inline stop button and session_task_card End button.

Covers:
- build_working_blocks (blocks.py)
- session_task_card End button (blocks.py)
- _handle_inline_stop denied/allowed/idle paths (interactions.py)
- _handle_session_end key fallback (interactions.py)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.slack.blocks import build_working_blocks, session_task_card
from kiro_crew.slack.format import LINK_DASHBOARD_ACTION

# ---------------------------------------------------------------------------
# build_working_blocks
# ---------------------------------------------------------------------------


class TestBuildWorkingBlocks:
    def test_returns_context_and_actions(self):
        blocks = build_working_blocks("my-session-key")
        assert len(blocks) == 2
        assert blocks[0]["type"] == "context"
        assert blocks[1]["type"] == "actions"

    def test_action_id_contains_session_key(self):
        blocks = build_working_blocks("abc-123")
        btn = blocks[1]["elements"][0]
        assert btn["action_id"] == "mc_inline_stop_abc-123"
        assert btn["value"] == "abc-123"

    def test_stop_button_is_danger(self):
        blocks = build_working_blocks("k")
        btn = blocks[1]["elements"][0]
        assert btn["style"] == "danger"

    def test_no_dashboard_link_by_default(self):
        """Default: Stop button only, no Link to Dashboard control."""
        blocks = build_working_blocks("k")
        elements = blocks[1]["elements"]
        assert len(elements) == 1
        assert all(
            e.get("action_id") != LINK_DASHBOARD_ACTION for e in elements
        ), "default working block must not carry the dashboard link"

    def test_dashboard_link_added_when_requested(self):
        """The turn-start feature: the gated caller adds the Link to Dashboard
        button beside Stop so a long turn can be linked from the start."""
        blocks = build_working_blocks("k", include_dashboard_link=True)
        elements = blocks[1]["elements"]
        assert elements[0]["action_id"] == "mc_inline_stop_k"
        assert any(e.get("action_id") == LINK_DASHBOARD_ACTION for e in elements), elements


# ---------------------------------------------------------------------------
# session_task_card — End button
# ---------------------------------------------------------------------------


class TestSessionTaskCard:
    def test_end_button_present(self):
        blocks = session_task_card(
            idx=0,
            key="sess-1",
            title="Test",
            agent="kirocrew",
            status="active",
            messages=[],
        )
        actions_block = blocks[1]
        assert actions_block["type"] == "actions"
        buttons = actions_block["elements"]
        end_btn = [b for b in buttons if "End" in b["text"]["text"]]
        assert len(end_btn) == 1
        assert end_btn[0]["action_id"] == "mc_session_end_sess-1"
        assert end_btn[0]["value"] == "sess-1"
        assert end_btn[0]["style"] == "danger"

    def test_resume_button_present(self):
        blocks = session_task_card(
            idx=0,
            key="s1",
            title="T",
            agent="a",
            status="paused",
            messages=[],
        )
        buttons = blocks[1]["elements"]
        resume_btn = [b for b in buttons if "Resume" in b["text"]["text"]]
        assert len(resume_btn) == 1

    def test_active_status_emoji(self):
        blocks = session_task_card(
            idx=0,
            key="s",
            title="T",
            agent="a",
            status="active",
            messages=[],
        )
        assert "🟢" in blocks[0]["title"]

    def test_paused_status_emoji(self):
        blocks = session_task_card(
            idx=0,
            key="s",
            title="T",
            agent="a",
            status="paused",
            messages=[],
        )
        assert "⏸️" in blocks[0]["title"]


# ---------------------------------------------------------------------------
# _handle_inline_stop
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_orch():
    """Create a mock orchestrator with sessions and slack."""
    orch = MagicMock()
    orch.slack = MagicMock()
    orch.slack.update_message = AsyncMock()
    orch.sessions = MagicMock()
    orch.sessions.stop_turn = AsyncMock(return_value="soft")
    return orch


@pytest.fixture
def setup_interactions(mock_orch, monkeypatch):
    """Set up interactions module state for testing."""
    import kiro_crew.slack.interactions as interactions

    monkeypatch.setattr(interactions, "_orch", mock_orch)
    # Set owner so is_owner returns True for U_OWNER
    from kiro_crew.slack.handler import set_owner_id

    set_owner_id("U_OWNER")
    return interactions


class TestHandleInlineStop:
    @pytest.mark.asyncio
    async def test_denied_for_non_owner(self, setup_interactions, mock_orch):
        """Non-owner clicks stop → SEL denied log, no stop_turn call."""
        interactions = setup_interactions
        payload = {"user": {"id": "U_OTHER"}}
        action = {"value": "sess-key"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            mock_sel.return_value.log_tool_invocation = MagicMock()
            await interactions._handle_inline_stop(payload, action, "C1", "ts1", "U_OTHER")

        # stop_turn should NOT be called
        mock_orch.sessions.stop_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_allowed_soft_stop(self, setup_interactions, mock_orch):
        """Owner clicks stop → stop_turn called, on_soft callback invoked."""
        interactions = setup_interactions

        # Make stop_turn invoke on_soft callback (simulating soft stop)
        async def _invoke_soft(key, on_soft=None, on_hard=None):
            if on_soft:
                await on_soft()
            return "soft"

        mock_orch.sessions.stop_turn = AsyncMock(side_effect=_invoke_soft)
        payload = {"user": {"id": "U_OWNER"}}
        action = {"value": "my-session"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            mock_sel.return_value.log_tool_invocation = MagicMock()
            await interactions._handle_inline_stop(payload, action, "C1", "msg_ts", "U_OWNER")

        # Immediate feedback: "Stopping..."
        mock_orch.slack.update_message.assert_any_call("C1", "msg_ts", text="⏹ _Stopping…_")
        # on_soft callback: "Execution stopped."
        mock_orch.slack.update_message.assert_any_call("C1", "msg_ts", text="⏹ Execution stopped.")
        # SEL tool invocation logged
        mock_sel.return_value.log_tool_invocation.assert_called_once()

    @pytest.mark.asyncio
    async def test_hard_stop_callback(self, setup_interactions, mock_orch):
        """Hard stop invokes on_hard callback with reset message."""
        interactions = setup_interactions

        async def _invoke_hard(key, on_soft=None, on_hard=None):
            if on_hard:
                await on_hard()
            return "hard"

        mock_orch.sessions.stop_turn = AsyncMock(side_effect=_invoke_hard)
        payload = {"user": {"id": "U_OWNER"}}
        action = {"value": "sess-x"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            mock_sel.return_value.log_tool_invocation = MagicMock()
            await interactions._handle_inline_stop(payload, action, "C1", "ts1", "U_OWNER")

        mock_orch.slack.update_message.assert_any_call(
            "C1", "ts1", text="⛔ Execution stopped — session reset."
        )

    @pytest.mark.asyncio
    async def test_idle_outcome_shows_nothing_running(self, setup_interactions, mock_orch):
        """stop_turn returns 'idle' → shows 'Nothing running.'"""
        interactions = setup_interactions
        mock_orch.sessions.stop_turn.return_value = "idle"
        payload = {"user": {"id": "U_OWNER"}}
        action = {"value": "idle-sess"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            mock_sel.return_value.log_tool_invocation = MagicMock()
            await interactions._handle_inline_stop(payload, action, "C1", "ts1", "U_OWNER")

        # Last update should be "Nothing running."
        calls = mock_orch.slack.update_message.call_args_list
        final_text = calls[-1][1]["text"] if calls[-1][1] else calls[-1][0][2]
        assert "Nothing running" in final_text

    @pytest.mark.asyncio
    async def test_compacting_decline_arms_and_the_repeat_press_forces(
        self, setup_interactions, mock_orch
    ):
        """The decline reply promises a repeat forces: the first press arms the
        marker for this presser, the second press by the same presser inside the
        window passes ``force=True``."""
        from kiro_crew import session_lifecycle as sl

        sl._stop_declined_markers.clear()
        interactions = setup_interactions
        mock_orch.sessions.is_compacting = MagicMock(return_value=True)
        mock_orch.sessions.stop_turn = AsyncMock(side_effect=["compacting", "hard"])
        payload = {"user": {"id": "U_OWNER"}}
        action = {"value": "sess-c"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            mock_sel.return_value.log_tool_invocation = MagicMock()
            await interactions._handle_inline_stop(payload, action, "C1", "ts1", "U_OWNER")
            assert mock_orch.sessions.stop_turn.await_args.kwargs.get("force") is None
            await interactions._handle_inline_stop(payload, action, "C1", "ts1", "U_OWNER")
            assert mock_orch.sessions.stop_turn.await_args.kwargs["force"] is True
            assert mock_orch.sessions.stop_turn.await_args.kwargs["preserve_queue"] is True
        sl._stop_declined_markers.clear()

    @pytest.mark.asyncio
    async def test_a_decline_it_could_not_show_arms_nothing(self, setup_interactions, mock_orch):
        """This reply is a message update guarded by a client and a message id and
        wrapped in its own ``except``. An escalation armed behind a swallowed
        failure would turn the presser's retry into a silent session reset."""
        from kiro_crew import session_lifecycle as sl

        sl._stop_declined_markers.clear()
        interactions = setup_interactions
        mock_orch.sessions.is_compacting = MagicMock(return_value=True)
        mock_orch.sessions.stop_turn = AsyncMock(return_value="compacting")
        mock_orch.slack.update_message = AsyncMock(side_effect=RuntimeError("slack 503"))
        payload = {"user": {"id": "U_OWNER"}}
        action = {"value": "sess-d"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            mock_sel.return_value.log_tool_invocation = MagicMock()
            await interactions._handle_inline_stop(payload, action, "C1", "ts1", "U_OWNER")

        assert sl._stop_declined_markers == {}, "the retry is a first press again"
        sl._stop_declined_markers.clear()

    @pytest.mark.asyncio
    async def test_no_session_key_returns_early(self, setup_interactions, mock_orch):
        """Empty session key → SEL 'invalid' log, no stop_turn."""
        interactions = setup_interactions
        payload = {"user": {"id": "U_OWNER"}}
        action = {"value": ""}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            mock_sel.return_value.log_tool_invocation = MagicMock()
            await interactions._handle_inline_stop(payload, action, "C1", "ts1", "U_OWNER")

        mock_orch.sessions.stop_turn.assert_not_called()
        mock_sel.return_value.log_api_access.assert_called_once()
        assert mock_sel.return_value.log_api_access.call_args[1]["outcome"] == "invalid"


# ---------------------------------------------------------------------------
# _handle_session_end — key fallback logic
# ---------------------------------------------------------------------------


class TestHandleSessionEnd:
    @pytest.mark.asyncio
    async def test_end_by_session_id(self, setup_interactions, mock_orch):
        """Session end uses find_key_by_sid to resolve the key."""
        interactions = setup_interactions
        mock_orch.sessions.find_key_by_sid = MagicMock(return_value="resolved-key")
        mock_orch.sessions.has_session = MagicMock(return_value=False)
        mock_orch.sessions.remove = AsyncMock()
        mock_orch.consolidator = None
        payload = {"user": {"id": "U_OWNER"}, "response_url": ""}
        action = {"value": "sid-abc"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await interactions._handle_session_end(payload, action, "C1", "ts1", "U_OWNER")

        mock_orch.sessions.remove.assert_called_once_with("resolved-key")

    @pytest.mark.asyncio
    async def test_end_falls_back_to_direct_key(self, setup_interactions, mock_orch):
        """When find_key_by_sid returns None, falls back to has_session(value)."""
        interactions = setup_interactions
        mock_orch.sessions.find_key_by_sid = MagicMock(return_value=None)
        mock_orch.sessions.has_session = MagicMock(return_value=True)
        mock_orch.sessions.remove = AsyncMock()
        mock_orch.consolidator = None
        payload = {"user": {"id": "U_OWNER"}, "response_url": ""}
        action = {"value": "direct-key"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await interactions._handle_session_end(payload, action, "C1", "ts1", "U_OWNER")

        mock_orch.sessions.remove.assert_called_once_with("direct-key")

    @pytest.mark.asyncio
    async def test_end_rejected_for_non_owner(self, setup_interactions, mock_orch):
        """Non-owner cannot end sessions."""
        interactions = setup_interactions
        mock_orch.sessions.remove = AsyncMock()
        payload = {"user": {"id": "U_OTHER"}, "response_url": ""}
        action = {"value": "some-key"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await interactions._handle_session_end(payload, action, "C1", "ts1", "U_OTHER")

        mock_orch.sessions.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_end_with_consolidator(self, setup_interactions, mock_orch):
        """Session end triggers consolidator before removal."""
        interactions = setup_interactions
        mock_orch.sessions.find_key_by_sid = MagicMock(return_value="key-1")
        mock_orch.sessions.has_session = MagicMock(return_value=False)
        mock_orch.sessions.remove = AsyncMock()
        mock_orch.consolidator = MagicMock()
        mock_orch.consolidator.consolidate_session = MagicMock()
        payload = {"user": {"id": "U_OWNER"}, "response_url": ""}
        action = {"value": "sid-with-consol"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await interactions._handle_session_end(payload, action, "C1", "ts1", "U_OWNER")

        mock_orch.consolidator.consolidate_session.assert_called_once_with("key-1")
        mock_orch.sessions.remove.assert_called_once_with("key-1")

    @pytest.mark.asyncio
    async def test_end_updates_message_when_no_response_url(self, setup_interactions, mock_orch):
        """Without response_url, falls back to update_message."""
        interactions = setup_interactions
        mock_orch.sessions.find_key_by_sid = MagicMock(return_value="k1")
        mock_orch.sessions.has_session = MagicMock(return_value=False)
        mock_orch.sessions.remove = AsyncMock()
        mock_orch.consolidator = None
        payload = {"user": {"id": "U_OWNER"}, "response_url": ""}
        action = {"value": "sid-123456789abc"}

        with patch("kiro_crew.slack.interactions.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await interactions._handle_session_end(payload, action, "C1", "ts1", "U_OWNER")

        mock_orch.slack.update_message.assert_called_once()
        call_args = mock_orch.slack.update_message.call_args
        assert "ended" in call_args[1]["text"]


# ---------------------------------------------------------------------------
# handle_message — inline stop button post is best-effort
# ---------------------------------------------------------------------------


class TestWorkingBlockPostFailure:
    @pytest.mark.asyncio
    async def test_threaded_turn_runs_when_working_block_post_fails(self):
        """A failed stop-button post in a thread still runs the turn and
        never tries to delete a working block that was never posted."""
        import importlib

        from conftest import MockSlackClient
        from kiro_crew.providers.base import LLMEvent
        from kiro_crew.slack.handler import handle_message

        handler_tests = importlib.import_module("test_slack_handler")

        class _WorkingBlockFailsSlack(MockSlackClient):
            async def post_blocks(self, channel, blocks, text, thread_ts=None):
                if text == "Working…":
                    self.actions.append(("blocks_failed", {"text": text}))
                    raise RuntimeError("slack 503")
                return await super().post_blocks(channel, blocks, text, thread_ts)

        slack = _WorkingBlockFailsSlack()
        provider = handler_tests.FakeProvider(
            [LLMEvent(kind="text_chunk", text="the answer is 42")]
        )
        sessions = handler_tests.FakeSessionManager(provider)

        await handle_message(slack, sessions, "C1", "hi", "thread1", "msg1", "U1")

        assert ("blocks_failed", {"text": "Working…"}) in slack.actions
        assert sessions.keys_seen == ["thread1"]
        rendered = [a[1].get("text", "") for a in slack.actions if a[0] in ("update", "post")]
        assert any("the answer is 42" in r for r in rendered), slack.actions
        posted = {a[1]["ts"] for a in slack.actions if a[0] in ("post", "blocks")}
        deleted = {a[1]["ts"] for a in slack.actions if a[0] == "delete"}
        assert deleted <= posted, "deleted a message that was never posted"


# ---------------------------------------------------------------------------
# handle_message — turn-start "Link to Dashboard" gate
# ---------------------------------------------------------------------------


def _link_ids(blocks):
    return [
        e.get("action_id")
        for b in blocks
        if b.get("type") == "actions"
        for e in b.get("elements", [])
        if e.get("action_id") == LINK_DASHBOARD_ACTION
    ]


class TestWorkingBlockDashboardLinkGate:
    async def _run(self, *, owner_at_start=None, link_slot_mid_turn=False):
        """Run one threaded turn; return (working_blocks, footer_blocks).

        The session map is a real dict: the handler's own ``set_slack_link``
        self-link lands in it mid-turn, exactly as in production.
        """
        import importlib

        from conftest import MockSlackClient
        from kiro_crew.providers.base import LLMEvent
        from kiro_crew.slack import handler

        handler_tests = importlib.import_module("test_slack_handler")
        provider = handler_tests.FakeProvider([LLMEvent(kind="text_chunk", text="answer")])

        class _Sessions(handler_tests.FakeSessionManager):
            def __init__(self, provider):
                super().__init__(provider)
                self.thread_map = {"thread1": owner_at_start} if owner_at_start else {}

            def set_slack_link(self, key, thread_ts, channel_id):
                self.thread_map[thread_ts] = key

            def get_session_for_thread(self, thread_ts):
                return self.thread_map.get(thread_ts)

        sessions = _Sessions(provider)
        ds = MagicMock()
        ds.get_linked_slot = MagicMock(return_value=None)
        slot = MagicMock()
        slot.key = "chat-1"

        class _Slack(MockSlackClient):
            # A click on the turn-start button links the thread to a dashboard
            # slot mid-turn, i.e. once the answer is already streaming.
            def _maybe_click(self):
                if link_slot_mid_turn:
                    ds.get_linked_slot.return_value = slot

            async def post_message(self, channel, text, thread_ts=None):
                self._maybe_click()
                return await super().post_message(channel, text, thread_ts)

            async def update_message(self, channel, ts, text):
                self._maybe_click()
                return await super().update_message(channel, ts, text)

        slack = _Slack()
        with (
            patch.object(handler, "_dashboard_state", ds),
            patch.object(handler, "get_dashboard_state", return_value=ds),
            patch.object(handler, "_mirror_to_dashboard"),
        ):
            await handler.handle_message(slack, sessions, "C1", "hi", "thread1", "msg1", "U1")
        posted = [a[1] for a in slack.actions if a[0] == "blocks"]
        working = [p["blocks"] for p in posted if p.get("text") == "Working…"]
        footers = [p["blocks"] for p in posted if p.get("text") != "Working…"]
        assert working, slack.actions
        return working[0], footers

    @pytest.mark.asyncio
    async def test_unlinked_thread_working_block_carries_link(self):
        working, _ = await self._run()
        assert _link_ids(working), working

    @pytest.mark.asyncio
    async def test_linked_thread_working_block_has_no_link(self):
        working, _ = await self._run(owner_at_start="dashboard:chat-1")
        assert not _link_ids(working), working

    @pytest.mark.asyncio
    async def test_first_turn_footer_keeps_link_despite_self_link(self):
        """The turn's own mid-turn self-link is not a dashboard link: the footer
        of a first turn on an unlinked thread still carries the Link button."""
        _, footers = await self._run()
        assert footers, "footer not posted"
        assert any(_link_ids(f) for f in footers), footers

    @pytest.mark.asyncio
    async def test_mid_turn_link_suppresses_second_footer_link(self):
        """Linked by a click on the Working button: the footer adds no second one."""
        working, footers = await self._run(link_slot_mid_turn=True)
        assert _link_ids(working), working
        assert footers, "footer not posted"
        assert not any(_link_ids(f) for f in footers), footers
