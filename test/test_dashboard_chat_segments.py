"""Dashboard chat: segment flushes in ``_run_chat`` and native sub-agent attribution."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from dashboard_chat_helpers import _provider_mock


class TestFlushSegment:
    """Unit tests for _flush_segment helper function."""

    def test_flush_segment_persists_and_broadcasts(self, tmp_path, monkeypatch):
        """_flush_segment persists assistant message and broadcasts chat_segment.

        Validates: Requirements 1.1, 1.2, 4.3, 6.3
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.append("chunk", "Hello ")
        slot.append("chunk", "world")

        from kiro_crew.dashboard.chat import _flush_segment

        _flush_segment(state, slot, "Hello world")

        # Chunks should be removed
        chunk_msgs = [m for m in slot.messages if m.get("role") == "chunk"]
        assert len(chunk_msgs) == 0
        # Assistant message should be persisted
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0]["content"] == "Hello world"
        # chat_segment should be broadcast
        state.broadcast_ws.assert_called_once_with("chat_segment", {"slot": "s1"})

    def test_flush_segment_schedules_widget_registration(self, tmp_path, monkeypatch):
        """A segment containing an <mcwidget> auto-registers it as an artifact.

        This is the integration seam for widget-as-artifact-by-default: without
        it nothing registers emitted widgets, and the in-session Artifacts tab
        can never list them.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("s1")

        calls: list = []

        async def _fake_register(text, message_ts, session_key):
            calls.append((text, message_ts, session_key))
            return []

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.register_widgets_off_loop", _fake_register
        )

        from kiro_crew.dashboard.chat import _flush_segment

        async def _run():
            _flush_segment(state, slot, '<mcwidget title="W">body</mcwidget>')
            # The task is detached — let it run before asserting.
            await asyncio.sleep(0)
            for t in list(state._background_tasks):
                await t

        asyncio.run(_run())

        assert len(calls) == 1
        text, message_ts, session_key = calls[0]
        assert "<mcwidget" in text
        # Keyed to the finalized assistant message.
        assert message_ts
        # Attributed with the BARE slot key. Asserting the literal string here
        # would happily pin a prefix the reader never queries, so assert the
        # value the in-session tab actually sends (?session=<activeSlot>, i.e.
        # slot.key) — ArtifactStore.list compares session_key exactly.
        assert session_key == slot.key

    def test_flush_segment_skips_registration_without_a_widget(self, tmp_path, monkeypatch):
        """The common case (no widget) must not touch the artifact store at all."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("s1")

        called = False

        async def _fake_register(text, message_ts, session_key):
            nonlocal called
            called = True
            return []

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.register_widgets_off_loop", _fake_register
        )

        from kiro_crew.dashboard.chat import _flush_segment

        async def _run():
            _flush_segment(state, slot, "just prose, no widget here")
            await asyncio.sleep(0)

        asyncio.run(_run())
        assert called is False

    def test_flush_segment_registers_redacted_text(self, tmp_path, monkeypatch):
        """A credential stripped from chat must not survive into the artifact.

        Registration runs on the POST-redaction text; passing the raw accumulated
        text would persist to disk (and re-surface on the artifact page) exactly
        what the segment redaction just removed.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("s1")

        captured: list = []

        async def _fake_register(text, message_ts, session_key):
            captured.append(text)
            return []

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.register_widgets_off_loop", _fake_register
        )

        from kiro_crew.dashboard.chat import _flush_segment

        secret = "AKIAIOSFODNN7EXAMPLE"

        async def _run():
            _flush_segment(state, slot, f'<mcwidget title="W">key={secret}</mcwidget>')
            await asyncio.sleep(0)
            for t in list(state._background_tasks):
                await t

        asyncio.run(_run())

        assert captured, "registration should have been scheduled"
        assert secret not in captured[0]

    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    def test_flush_segment_skips_registration_in_restricted_sessions(
        self, tmp_path, monkeypatch, mode
    ):
        """Incognito / temporary sessions must not persist widget artifacts.

        Every artifact write is denied for these sessions at the HTTP gate
        (`_is_restricted_session`), so registering from the chat path would be a
        back door around that ceiling: widget HTML from a session the user
        expected to leave no trace would land in `artifacts/<slug>/` and show up
        in the library.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.memory_mode = mode
        assert slot.is_restricted

        called = False

        async def _fake_register(text, message_ts, session_key):
            nonlocal called
            called = True
            return []

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.register_widgets_off_loop", _fake_register
        )

        from kiro_crew.dashboard.chat import _flush_segment

        async def _run():
            _flush_segment(state, slot, '<mcwidget title="W">secret body</mcwidget>')
            await asyncio.sleep(0)
            for t in list(state._background_tasks):
                await t

        asyncio.run(_run())
        assert called is False, f"{mode} session must not register widget artifacts"


class TestRunChatSegmentFlush:
    """Tests for segment flush behavior in _run_chat()."""

    @staticmethod
    def _make_mock_client(events):
        """Create a mock ACP client that yields the given LLMEvent list."""
        client = _provider_mock()
        client.context_usage_pct = MagicMock(return_value=10.0)

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_state_for_run_chat(tmp_path, monkeypatch):
        """Create a DashboardState wired for _run_chat tests."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_credential_split_across_chunks_never_streamed_raw(self, tmp_path, monkeypatch):
        """A credential split across streaming chunks must never appear raw in
        any chat_chunk broadcast (pentest issue 3), while the reassembled stream
        stays lossless and shows the redaction.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        # AKIAIOSFODNN7EXAMPLE split exactly as in the pentest reproduction.
        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="The access key is AKIA"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="IOSFODNN7"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="EXAMPLE"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "echo the key")

        contents = [
            call.args[1]["content"]
            for call in state.broadcast_ws.call_args_list
            if call.args[0] == "chat_chunk"
        ]
        wire = "".join(contents)
        # No individual frame — and not the reassembled wire — leaks the key.
        for frame in contents:
            assert "AKIAIOSFODNN7EXAMPLE" not in frame
        assert "AKIAIOSFODNN7EXAMPLE" not in wire
        # The credential fragments must not be recoverable across frames.
        assert "AKIA" not in wire.replace("[REDACTED: credential]", "")
        assert "[REDACTED: credential]" in wire
        assert wire == "The access key is [REDACTED: credential]"

    @pytest.mark.asyncio
    async def test_credential_split_across_thinking_chunks_not_streamed_raw(
        self, tmp_path, monkeypatch
    ):
        """A credential split across thinking chunks must not appear raw on any
        chat_thinking broadcast (issue 3 parity for the thinking stream)."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_THINKING_CHUNK,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_THINKING_CHUNK, text="the key looks like AKIA"),
            LLMEvent(kind=EVENT_THINKING_CHUNK, text="IOSFODNN7"),
            LLMEvent(kind=EVENT_THINKING_CHUNK, text="EXAMPLE"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="done"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "think about it")

        thinking = [
            call.args[1]["content"]
            for call in state.broadcast_ws.call_args_list
            if call.args[0] == "chat_thinking"
        ]
        wire = "".join(thinking)
        for frame in thinking:
            assert "AKIAIOSFODNN7EXAMPLE" not in frame
        assert "AKIAIOSFODNN7EXAMPLE" not in wire
        assert "AKIA" not in wire.replace("[REDACTED: credential]", "")
        assert "[REDACTED: credential]" in wire

    @pytest.mark.asyncio
    async def test_text_tool_text_complete_produces_two_segments(self, tmp_path, monkeypatch):
        """Mock event stream: text → tool_call → text → complete produces
        two assistant messages and one tool message.

        Validates: Requirements 1.1, 1.2, 1.3, 4.3
        """
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="Before tool"),
            LLMEvent(kind=EVENT_TOOL_CALL, title="read_file", tool_kind="read"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="After tool"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 2
        assert assistant_msgs[0]["content"] == "Before tool"
        assert assistant_msgs[1]["content"] == "After tool"

        ws_calls = [(c.args[0], c.args[1]) for c in state.broadcast_ws.call_args_list]
        ws_types = [t for t, _ in ws_calls]
        assert "chat_segment" in ws_types
        assert "tool_call" in ws_types

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "chunks",
        [("opaque-dashboard-token",), ("\n" * 4096, "**", "opaque-dashboard-token", "**")],
    )
    async def test_notice_predecessor_grades_live_and_saved_answer(
        self, tmp_path, monkeypatch, chunks
    ):
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_NOTICE,
            EVENT_TEXT_CHUNK,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_NOTICE, title="Authorization: Bearer", notice_severity="warning"),
            *(LLMEvent(kind=EVENT_TEXT_CHUNK, text=text) for text in chunks),
            LLMEvent(
                kind=EVENT_TEXT_CHUNK,
                text=" and useful detail. https://example.com/?token=opaque-url-secret",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_or_create = AsyncMock(
            return_value=(self._make_mock_client(events), True, False)
        )
        await chat_runner._run_chat(state, slot, "hello")
        assert "opaque-dashboard-token" not in str(slot.messages)
        assert "opaque-dashboard-token" not in str(state.broadcast_ws.call_args_list)
        answer = next(row for row in slot.messages if row.get("role") == "assistant")
        assert "useful detail." in answer["content"]
        assert "REDACTED" in answer["content"]
        assert answer.get("meta", {}).get("blocked_links") or answer.get("meta", {}).get(
            "redactions"
        )

    @pytest.mark.asyncio
    async def test_provider_notice_overflow_is_bounded_and_logged_once(
        self, tmp_path, monkeypatch, caplog
    ):
        from kiro_crew.acp._dispatch import NOTICE_STATE_MAX
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_NOTICE,
            EVENT_TEXT_CHUNK,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="Answer"),
            *(
                LLMEvent(kind=EVENT_NOTICE, title=f"Warning {i}")
                for i in range(NOTICE_STATE_MAX + 5)
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        flush = chat_runner._flush_provider_notices
        observed = []

        def observe_flush(turn):
            if turn._provider_notices:
                observed.append(
                    (
                        len(turn._provider_notices),
                        turn._provider_notices.maxlen,
                        turn._provider_notice_drops,
                    )
                )
                assert not [r for r in caplog.records if "dropped" in r.getMessage()]
            flush(turn)
            assert turn._provider_notice_drops == 0

        monkeypatch.setattr(chat_runner, "_flush_provider_notices", observe_flush)
        await chat_runner._run_chat(state, slot, "hello")
        assert observed == [(NOTICE_STATE_MAX, NOTICE_STATE_MAX, 5)]
        rows = [
            row["content"]
            for row in slot.messages
            if row.get("meta", {}).get("kind") == "provider_notice"
        ]
        assert rows == [f"Warning {i}" for i in range(5, NOTICE_STATE_MAX + 5)]
        assert [r.getMessage() for r in caplog.records if "dropped" in r.getMessage()] == [
            "Dashboard: dropped 5 provider notices from the full queue"
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("split_credential", [False, True])
    @pytest.mark.parametrize("padding", [None, "", "x" * 260])
    async def test_mid_turn_provider_notice_waits_for_segment_boundary(
        self, tmp_path, monkeypatch, split_credential, padding
    ):
        """A notice waits for the answer's existing boundary and keeps its witness."""
        from kiro_crew.acp._dispatch import parse_session_update
        from kiro_crew.acp.types import EVENT_NOTICE
        from kiro_crew.providers.acp import AcpProvider
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        (notice_event,) = parse_session_update(
            {
                "sessionUpdate": "notice",
                "title": (
                    (padding or "") + "Authorization: Bearer"
                    if split_credential
                    else "Model fallback"
                ),
                "description": "opaque-notice-token" if split_credential else "",
                "severity": "warning",
            }
        )
        provider_notice = (
            LLMEvent(
                kind=EVENT_NOTICE,
                title="Authorization: Bearer" if split_credential else "Model fallback",
                text="opaque-notice-token" if split_credential else "",
                notice_severity="warning",
            )
            if padding is None
            else AcpProvider._to_llm_event(notice_event)
        )
        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="Before notice"),
            provider_notice,
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="After notice"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot._has_reader = split_credential
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert not [m for m in slot.messages if m.get("role") == "chunk"]
        rows = [
            (m["role"], m["content"])
            for m in slot.messages
            if m.get("role") == "assistant"
            or (m.get("role") == "notice" and m.get("meta", {}).get("kind") == "provider_notice")
        ]
        from kiro_crew.acp._dispatch import redact_text

        expected_notice = (
            redact_text(provider_notice.title + "\n" + provider_notice.text)
            if split_credential
            else "Model fallback"
        )
        assert rows == [
            ("assistant", "Before noticeAfter notice"),
            ("notice", expected_notice),
        ]

        if split_credential:
            assert "opaque-notice-token" not in str(slot.messages)
            assert "opaque-notice-token" not in str(state.broadcast_ws.call_args_list)
            notice_frames = [
                call.args[1]
                for call in state.broadcast_ws.call_args_list
                if call.args[0] == "chat_message" and call.args[1].get("role") == "notice"
            ]
            assert any(frame["content"] == expected_notice for frame in notice_frames)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", ["cancel", "process_death", "error"])
    @pytest.mark.parametrize("partial_text", ["", "Partial answer"])
    async def test_abnormal_end_preserves_provider_notice(
        self, tmp_path, monkeypatch, failure, partial_text
    ):
        from kiro_crew.acp.client import AcpProcessDied
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_NOTICE, EVENT_TEXT_CHUNK, LLMEvent

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client([])

        async def stream(_msg):
            if partial_text:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=partial_text)
            yield LLMEvent(kind=EVENT_NOTICE, title="Provider warning", notice_severity="warning")
            if failure == "cancel":
                raise asyncio.CancelledError
            if failure == "process_death":
                raise AcpProcessDied("provider exited")
            raise RuntimeError("provider failed")

        client.stream = stream
        state.sessions.record_failure = AsyncMock()
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        await _run_chat(state, slot, "hello", _prompt_depth=1)
        state.flush_slot_now(slot)
        rows = state.conversation_log.read_messages_chained("dashboard:s1")
        relevant = [
            (row["role"], row["content"])
            for row in rows
            if row.get("content") == partial_text
            or row.get("meta", {}).get("kind") == "provider_notice"
        ]
        assert relevant == ([("assistant", partial_text)] if partial_text else []) + [
            ("notice", "Provider warning")
        ]
        assert not any(row.get("role") == "chunk" for row in slot.messages)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text_kind", ["credential", "oversized_credential", "ordinary"])
    @pytest.mark.parametrize("surface", ["websocket", "sse", "history", "cancelled_history"])
    async def test_notice_keeps_partial_text_redaction_continuous(
        self, tmp_path, monkeypatch, text_kind, surface
    ):
        from kiro_crew.acp.types import EVENT_NOTICE
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        left, right = {
            "credential": ("AKIA", "IOSFODNN7EXAMPLE"),
            "oversized_credential": ("Authorization: Bearer " + "a" * 5000, "continuation"),
            "ordinary": ("ordi", "nary"),
        }[text_kind]
        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="Before " + left),
            LLMEvent(kind=EVENT_NOTICE, title="Warning", notice_severity="warning"),
            LLMEvent(kind=EVENT_NOTICE, title="Another warning", notice_severity="warning"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=right + " after."),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot._has_reader = True
        sse_chunks = []
        client = self._make_mock_client(events)

        async def stream(_msg):
            for event in events:
                if event.kind == EVENT_COMPLETE and surface == "cancelled_history":
                    raise asyncio.CancelledError
                yield event
                sse_chunks.extend(
                    row["content"] for row in slot._pending if row.get("role") == "chunk"
                )
                slot._pending.clear()

        client.stream = stream
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        await _run_chat(state, slot, "hello")
        sse_chunks.extend(row["content"] for row in slot._pending if row.get("role") == "chunk")

        if surface == "websocket":
            contents = [
                call.args[1]["content"]
                for call in state.broadcast_ws.call_args_list
                if call.args[0] == "chat_chunk"
            ]
        elif surface == "sse":
            contents = sse_chunks
        else:
            state.flush_slot_now(slot)
            contents = [
                row["content"]
                for row in state.conversation_log.read_messages_chained("dashboard:s1")
                if row.get("role") == "assistant"
            ]
        expected = (
            "Before ordinary after."
            if text_kind == "ordinary"
            else "Before [REDACTED: credential] after."
        )
        assert "".join(contents) == expected
        assert not any(row.get("role") == "chunk" for row in slot.messages)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("intervening_text", [False, True])
    @pytest.mark.parametrize("boundary", ["tool_call", "permission_request"])
    async def test_notice_prefix_is_cleared_at_tool_boundary(
        self, tmp_path, monkeypatch, intervening_text, boundary
    ):
        from kiro_crew.acp.types import EVENT_NOTICE
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="A "),
            LLMEvent(kind=EVENT_NOTICE, title="Warning", notice_severity="warning"),
            *([LLMEvent(kind=EVENT_TEXT_CHUNK, text="B")] if intervening_text else []),
            LLMEvent(kind=boundary, title="read_file", tool_kind="read", request_id="req-1"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="C"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        state.enable_yolo()
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.get_slack_link = MagicMock(return_value=("ts-1", "C123"))
        state.slack_client = AsyncMock()
        state.slack_client.start_stream = AsyncMock(return_value="")
        mirror = AsyncMock()
        monkeypatch.setattr(chat_runner, "_deliver_cross_surface_reply", mirror)

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        assert [m["content"] for m in slot.messages if m.get("role") == "assistant"] == (
            ["A B", "C"] if intervening_text else ["A ", "C"]
        )
        mirror.assert_awaited_once_with(
            state, "dashboard:s1", ("A B" if intervening_text else "A ") + "\n\nC", slot=slot
        )
        posted = [call.args[1] for call in state.slack_client.post_message.await_args_list]
        assert ("A B" if intervening_text else "A ") + "\n\nC" in posted
        assert "AC" not in posted

    @pytest.mark.asyncio
    @pytest.mark.parametrize("credential", [False, True])
    @pytest.mark.parametrize("notice_after_boundary", [False, True])
    @pytest.mark.parametrize("boundary", ["tool_call", "permission_request"])
    async def test_notice_keeps_text_a_tool_boundary_deferred(
        self, tmp_path, monkeypatch, credential, notice_after_boundary, boundary
    ):
        """A notice after a deferred tool-boundary cut keeps the deferred text.

        An open fence defers the cut at a tool call, so the opening stays in
        `assistant_text` while the tool boundary empties both redactor buffers.
        The notice must finalize that already-approved opening, not replace the
        reply with the (empty) buffers; and a credential split around the
        notice must still never stream or persist raw.
        """
        from kiro_crew.acp.types import EVENT_NOTICE
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        tail = "AKIA" if credential else "x = 1"
        rest = "IOSFODNN7EXAMPLE" if credential else " + 1"
        notice = LLMEvent(kind=EVENT_NOTICE, title="Model fallback", notice_severity="warning")
        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="Opening line\n```python\nprint(0)\n"),
            LLMEvent(kind=boundary, title="read_file", tool_kind="read", request_id="req-1"),
            *([notice] if notice_after_boundary else []),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=tail),
            *([] if notice_after_boundary else [notice]),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=rest + "\n```\nDone."),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        state.enable_yolo()
        slot = state.get_or_create_slot("s1")
        slot._has_reader = True
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        await _run_chat(state, slot, "hello")
        state.flush_slot_now(slot)

        saved = [
            row["content"]
            for row in state.conversation_log.read_messages_chained("dashboard:s1")
            if row.get("role") == "assistant"
        ]
        wire = "".join(
            call.args[1]["content"]
            for call in state.broadcast_ws.call_args_list
            if call.args[0] == "chat_chunk"
        )
        rest_text = "[REDACTED: credential]" if credential else "x = 1 + 1"
        expected = f"Opening line\n```python\nprint(0)\n{rest_text}\n```\nDone."
        assert saved[0].startswith("Opening line\n```python\nprint(0)\n")
        assert "".join(saved) == expected
        assert wire == expected
        if boundary == "tool_call":
            # A permission row stops the trailing-chunk walk with or without a
            # notice; only the tool-call boundary is pinned for stranded rows.
            assert not any(row.get("role") == "chunk" for row in slot.messages)
        if credential:
            for surface in (str(slot.messages), str(state.broadcast_ws.call_args_list), *saved):
                assert "IOSFODNN7EXAMPLE" not in surface

    @pytest.mark.asyncio
    async def test_crewmate_session_keeps_provider_notice_in_history(self, tmp_path, monkeypatch):
        """A crewmate's chat does not draw provider notices (the crewmate-chat
        draws-messages-only decision),
        so the session history is where they stay: the row is persisted with its
        kind for the Sessions page, and the reply around it is unaffected."""
        from kiro_crew import member_memory_auth
        from kiro_crew.acp.types import EVENT_NOTICE
        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.memory_stores import provision_member_memory
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [
            LLMEvent(kind=EVENT_NOTICE, title="Model fallback", notice_severity="warning"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="Answer"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.mode = "member"
        slot.agent = "radar"
        cfg = KiroCrewConfig.load()
        cfg.agents["radar"] = KiroCrewAgentConfig()
        store = provision_member_memory(cfg, "radar")
        cfg.save()
        member_memory_auth.bind_private_session_store(
            chat_runner.effective_session_key(slot), store
        )
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        await _run_chat(state, slot, "hello")
        state.flush_slot_now(slot)

        saved = [
            (row.get("role"), row.get("content"), (row.get("meta") or {}).get("kind"))
            for row in state.conversation_log.read_messages_chained("dashboard:s1")
            if row.get("role") in ("notice", "assistant")
        ]
        assert saved == [
            ("notice", "Model fallback", "provider_notice"),
            ("assistant", "Answer", None),
        ]

    @pytest.mark.asyncio
    async def test_a_notice_after_a_tool_does_not_strand_its_pill(self, tmp_path, monkeypatch):
        """A tool that sent no result frame is completed by the text after it, even
        when a provider notice lands in between."""
        from kiro_crew.acp.types import EVENT_NOTICE
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TOOL_CALL, title="read_file", tool_kind="read"),
            LLMEvent(kind=EVENT_NOTICE, title="Model fallback", notice_severity="warning"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="After the tool"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        tools = [m for m in slot.messages if m.get("role") == "tool"]
        assert tools and all(m.get("meta", {}).get("done") for m in tools)

    @pytest.mark.asyncio
    async def test_tool_turn_progress_claim_surfaces_idle_notice(self, tmp_path, monkeypatch):
        """A mixed turn must not replay tools, but it must not claim it keeps running."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        claim = (
            "I'm continuing with the full local gate run; the new screenshot is "
            "the lower frame above."
        )
        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="Prepared the gate script."),
            LLMEvent(kind=EVENT_TOOL_CALL, title="write_file", tool_kind="write"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=claim),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        state.refresh_slot_source_status = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "run the full gate")

        notices = [m["content"] for m in slot.messages if m.get("role") == "notice"]
        assert any("No further main-agent steps run" in text for text in notices)
        assert any(
            "Stop hook explicitly requests a bounded continuation" in text for text in notices
        )
        assert claim in [m["content"] for m in slot.messages if m.get("role") == "assistant"]
        assert slot._queue == []

    @pytest.mark.asyncio
    async def test_idle_turn_boundary_refreshes_source_status(self, tmp_path, monkeypatch):
        """Reaching idle at a turn boundary must re-read the slot's PR/MR status.

        This pins the production wiring: `_run_chat`'s idle branch calls
        `state.refresh_slot_source_status(slot.key)` so the sidebar chips and the
        detail panel re-read after a turn that may have opened/pushed/merged a
        PR. The state-level tests exercise `refresh_slot_source_status` directly
        and would stay green if this call were deleted, so pin the real
        `_run_chat` path here.
        """
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events = [LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"), LLMEvent(kind=EVENT_COMPLETE)]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        state.refresh_slot_source_status = MagicMock()
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        state.refresh_slot_source_status.assert_called_once_with(slot.key)

    @pytest.mark.asyncio
    async def test_text_permission_request_flushes_segment(self, tmp_path, monkeypatch):
        """Mock event stream: text → permission_request flushes segment
        before permission flow.

        Validates: Requirements 1.4
        """
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_PERMISSION_REQUEST,
            EVENT_TEXT_CHUNK,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="Analyzing..."),
            LLMEvent(
                kind=EVENT_PERMISSION_REQUEST,
                title="bash",
                tool_kind="execute",
                request_id="req-1",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        # Enable YOLO mode so permission auto-approves (simplifies test)
        state.enable_yolo()
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        client.approve_tool = AsyncMock()
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "run ls")

        # Segment should have been flushed before permission flow
        ws_types = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "chat_segment" in ws_types
        # The flushed segment should be persisted as assistant
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert any(m["content"] == "Analyzing..." for m in assistant_msgs)

    @pytest.mark.asyncio
    async def test_text_only_complete_no_segments(self, tmp_path, monkeypatch):
        """Text-only stream → complete produces one assistant message (no segments).

        Validates: Requirements 8.1
        """
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="Just text"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # No chat_segment events
        ws_types = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "chat_segment" not in ws_types
        # One assistant message
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0]["content"] == "Just text"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("second", "streamed", "given_back"),
        [("b ", "ab cd", []), ("b", "cd", ["ab"])],
        ids=["run-ended-before-the-tool", "run-held-over-the-tool"],
    )
    async def test_chunk_seq_monotonically_increasing_across_segments(
        self, tmp_path, monkeypatch, second, streamed, given_back
    ):
        """chunk_seq values in broadcast calls are monotonically increasing
        across segments, and no streamed text is lost at the tool boundary.

        Validates: Requirements 7.1
        """
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="a"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=second),
            LLMEvent(kind=EVENT_TOOL_CALL, title="read_file", tool_kind="read"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="c"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="d"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # Collect all seq values + content from chat_chunk broadcasts
        seq_values: list[int] = []
        contents: list[str] = []
        patches: list[str] = []
        for call in state.broadcast_ws.call_args_list:
            if call.args[0] == "chat_chunk":
                seq_values.append(call.args[1]["seq"])
                contents.append(call.args[1]["content"])
            elif call.args[0] == "chat_message_update" and "tool_call_id" not in call.args[1]:
                patches.append(call.args[1]["content"])

        # The streaming redaction buffer (issue 3) withholds trailing
        # credential-class runs, so contiguous chars ("a"+"b", "c"+"d") are
        # coalesced rather than emitted one-per-input-chunk. A run still held at
        # the tool call waits for the text after it (the two could be halves of
        # one credential); once that text shows it is a word of its own it goes
        # back on the row it was cut from as a content patch, not as a chunk of
        # the next segment. What must hold: seq is strictly monotonic and no
        # content is lost across the buffering.
        assert seq_values, "no chat_chunk broadcasts"
        for i in range(1, len(seq_values)):
            assert seq_values[i] > seq_values[i - 1], f"seq not monotonic: {seq_values}"
        assert "".join(contents) == streamed, f"content lost in stream buffer: {contents}"
        assert patches == given_back, f"held run not given back to its row: {patches}"

    @pytest.mark.asyncio
    async def test_chunk_seq_continues_across_turns(self, tmp_path, monkeypatch):
        """The seq counter is the SLOT's, not the turn's: the second turn's first
        chunk is numbered above the first turn's last. A client's replay floor
        (the newest seq its transcript holds) then orders every later chunk above
        it, whether or not the client saw the turn boundary -- a snapshot or a
        `_done` lost to a reconnect cannot make the next turn's chunks read as
        replays of the finished one."""
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        from kiro_crew.dashboard.chat import _run_chat

        def seqs() -> list[int]:
            return [
                c.args[1]["seq"]
                for c in state.broadcast_ws.call_args_list
                if c.args[0] == "chat_chunk"
            ]

        client = self._make_mock_client(
            [LLMEvent(kind=EVENT_TEXT_CHUNK, text="one"), LLMEvent(kind=EVENT_COMPLETE)]
        )
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        await _run_chat(state, slot, "first")
        first_turn = seqs()
        assert first_turn, "no chat_chunk broadcasts in the first turn"

        client = self._make_mock_client(
            [LLMEvent(kind=EVENT_TEXT_CHUNK, text="two"), LLMEvent(kind=EVENT_COMPLETE)]
        )
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        await _run_chat(state, slot, "second")
        second_turn = seqs()[len(first_turn) :]
        assert second_turn, "no chat_chunk broadcasts in the second turn"
        assert second_turn[0] > first_turn[-1], (first_turn, second_turn)
        # Every frame names the process generation that numbered it, so a client
        # can drop a floor from before a gateway restart instead of comparing.
        from kiro_crew.dashboard.chat_utils import chunk_generation

        gens = {
            c.args[1].get("gen")
            for c in state.broadcast_ws.call_args_list
            if c.args[0] == "chat_chunk"
        }
        assert gens == {chunk_generation()}

    @pytest.mark.asyncio
    async def test_window_chunk_rows_carry_the_wire_seq(self, tmp_path, monkeypatch):
        """Each window ``chunk`` row is stamped with its wire frame's ``seq``.

        A slot snapshot taken mid-stream folds those rows into one ``streaming``
        row carrying the newest seq (chat_utils), which the client seeds its
        replay guard from; a row without the stamp would leave the snapshot
        unable to say how far the stream it holds has advanced.
        """
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="a"),
            LLMEvent(kind=EVENT_TOOL_CALL, title="read_file", tool_kind="read"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="b"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        # Wrapped on the CLASS: `_ChatSlot` is slotted, and the window list is
        # rebuilt at the segment flush (chunk rows are dropped from it), so
        # neither an instance patch nor reading `slot.messages` afterwards sees
        # every chunk row the runner appended.
        from kiro_crew.dashboard.state import _ChatSlot

        appended: list[dict] = []
        real_append = _ChatSlot.append

        def recording_append(self_slot, role, content, cls="", *args, **kwargs):
            row = real_append(self_slot, role, content, cls, *args, **kwargs)
            if role == "chunk":
                appended.append(row)
            return row

        monkeypatch.setattr(_ChatSlot, "append", recording_append)

        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        wire = [
            (c.args[1]["seq"], c.args[1]["content"])
            for c in state.broadcast_ws.call_args_list
            if c.args[0] == "chat_chunk"
        ]
        assert wire, "no chat_chunk broadcasts"
        assert [(r["seq"], r["content"]) for r in appended] == wire


class TestRunChatNativeSubagentAttribution:
    """End-to-end: native (use_subagent) sub-agent tool calls + results are
    attributed to per-sub-agent Activity cards, deduped, and the accumulated
    feed is sent as the card's done ``result``."""

    @staticmethod
    def _make_mock_client(events):
        client = _provider_mock()
        client.context_usage_pct = MagicMock(return_value=10.0)

        async def _stream(msg):
            for ev in events:
                yield ev

        client.stream = _stream
        client.stream_command = _stream
        return client

    @staticmethod
    def _make_state_for_run_chat(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @pytest.mark.asyncio
    async def test_native_tool_calls_attribute_dedupe_and_accumulate(self, tmp_path, monkeypatch):
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_SUBAGENT_ACTIVITY,
            EVENT_SUBAGENT_LIST,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        def _sub(status):
            return {
                "sessionId": "sess-1",
                "sessionName": "explorer",
                "role": "gpu-multiagent-explorer",
                "agentName": "gpu-multiagent-explorer",
                "initialQuery": "explore acp",
                "status": {"type": status, "message": ""},
            }

        events = [
            # 1) crew list → spawn one card
            LLMEvent(kind=EVENT_SUBAGENT_LIST, subagents=[_sub("working")]),
            # 2) private-channel activity maps the inner toolCallId → this card
            LLMEvent(
                kind=EVENT_SUBAGENT_ACTIVITY,
                sub_session_id="sess-1",
                tool_call_id="tc-1",
                title="read",
            ),
            # 3) flat tool_call (full title) → attributed to the card
            LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Reading foo.py:1",
                tool_kind="read",
                tool_call_id="tc-1",
            ),
            # 4) flat tool_result (real output) → attributed + accumulated
            LLMEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="tc-1",
                tool_output="file body XYZ",
                tool_final=True,
            ),
            # 5) duplicate tool_result (kiro sends content + rawOutput) → deduped
            LLMEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="tc-1",
                tool_output="file body XYZ",
                tool_final=True,
            ),
            # 5b) sub-agent text streamed on the private channel (agent_message_chunk)
            LLMEvent(
                kind=EVENT_SUBAGENT_ACTIVITY, sub_session_id="sess-1", text="thinking out loud"
            ),
            # 6) crew list terminal → done with accumulated feed
            LLMEvent(kind=EVENT_SUBAGENT_LIST, subagents=[_sub("terminated")]),
            LLMEvent(kind=EVENT_COMPLETE),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "explore the codebase with 1 subagent")

        calls = [(c.args[0], c.args[1]) for c in state.broadcast_ws.call_args_list]
        card = "native:sess-1"

        # Card spawned with agent + task.
        spawns = [d for t, d in calls if t == "subagent_spawn" and d.get("id") == card]
        assert len(spawns) == 1
        assert spawns[0]["agent"] == "gpu-multiagent-explorer"
        assert spawns[0]["task"] == "explore acp"

        # Tool call + output streamed onto the card.
        chunks = [
            d.get("text", "") for t, d in calls if t == "subagent_chunk" and d.get("id") == card
        ]
        joined = "".join(chunks)
        assert "Reading foo.py:1" in joined  # full tool title from flat tool_call
        assert "file body XYZ" in joined  # real tool output
        assert "thinking out loud" in joined  # sub-agent text (agent_message_chunk)

        # Dedupe: the duplicate tool_result must NOT double-print the output.
        assert joined.count("file body XYZ") == 1

        # Done fires once with the accumulated feed as result (not the sentinel).
        dones = [d for t, d in calls if t == "subagent_done" and d.get("id") == card]
        assert len(dones) == 1
        assert "Reading foo.py:1" in dones[0]["result"]
        assert "file body XYZ" in dones[0]["result"]
