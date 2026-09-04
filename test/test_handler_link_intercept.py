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

        def queue_append(content, *, meta=None, directive_user_origin):
            assert directive_user_origin is True
            # The linked-thread enqueue stamps the admission-time containment
            # snapshot (#5911) so the drain can re-assert it at delivery.
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
