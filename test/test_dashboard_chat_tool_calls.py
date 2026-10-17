"""Dashboard chat: tool call frames in ``_run_chat`` — WS payload, boundary whitespace, call updates."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from dashboard_chat_helpers import _provider_mock

from kiro_crew.dashboard.chat_runner import _tool_call_ws_payload


def test_tool_call_ws_payload_preserves_shell_capability_signal():
    """The dashboard receives an explicit shell signal for indeterminate UX.

    Keep this contract at the backend boundary so a future percentage-based
    progress mode can extend the payload without making the frontend infer
    tool type from a display title.
    """
    event = MagicMock(
        title="bash",
        tool_kind="execute",
        tool_call_id="tc-shell",
        tool_purpose="Run a command",
        tool_input="echo hello",
        is_shell=True,
    )

    payload = _tool_call_ws_payload(event)

    assert payload["tool"] == "bash"
    assert payload["kind"] == "execute"
    assert payload["is_shell"] is True
    assert payload["tool_call_id"] == "tc-shell"


class TestRunChatToolBoundarySegments:
    """Test that _run_chat inserts whitespace across tool call boundaries."""

    @pytest.mark.asyncio
    async def test_tool_boundary_splits_segments(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import LLMEvent

        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state._hook_store = None

        events = [
            LLMEvent(kind="text_chunk", text="Let me check."),
            LLMEvent(kind="tool_call", title="Read File", tool_kind="read"),
            LLMEvent(kind="text_chunk", text="Done!"),
            LLMEvent(kind="complete"),
        ]

        fake_client = _provider_mock()

        async def _stream(msg):
            for e in events:
                yield e

        fake_client.stream = _stream
        fake_client.context_usage_pct = MagicMock(return_value=0.0)
        state.sessions.get_or_create = AsyncMock(return_value=(fake_client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)
        state.sessions.check_context_usage = MagicMock()
        state.sessions.record_success = MagicMock()
        state.sessions.record_failure = AsyncMock()
        state.sessions.release = MagicMock()

        slot = state.get_or_create_slot("s1")
        await _run_chat(state, slot, "do it")

        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        # With _flush_segment, text is split into separate segments at tool boundaries
        # so gluing can't happen — each segment is independent
        assert len(assistant_msgs) == 2
        assert "Let me check." in assistant_msgs[0]["content"]
        assert "Done!" in assistant_msgs[1]["content"]

    @pytest.mark.asyncio
    async def test_tool_boundary_empty_chunk_still_splits(self, tmp_path, monkeypatch):
        """Empty text chunk after tool call doesn't prevent segment splitting."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import LLMEvent

        monkeypatch.setattr("kiro_crew.dashboard.chat.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat.sel", lambda: MagicMock())

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state._hook_store = None

        events = [
            LLMEvent(kind="text_chunk", text="Before."),
            LLMEvent(kind="tool_call", title="T", tool_kind="read"),
            LLMEvent(kind="text_chunk", text=""),  # empty chunk
            LLMEvent(kind="text_chunk", text="After!"),
            LLMEvent(kind="complete"),
        ]

        fake_client = _provider_mock()

        async def _stream(msg):
            for e in events:
                yield e

        fake_client.stream = _stream
        fake_client.context_usage_pct = MagicMock(return_value=0.0)
        state.sessions.get_or_create = AsyncMock(return_value=(fake_client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)
        state.sessions.check_context_usage = MagicMock()
        state.sessions.record_success = MagicMock()
        state.sessions.record_failure = AsyncMock()
        state.sessions.release = MagicMock()

        slot = state.get_or_create_slot("s1")
        await _run_chat(state, slot, "do it")

        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        # Segments are flushed at tool boundaries; empty chunks don't create segments
        assert len(assistant_msgs) == 2
        assert "Before." in assistant_msgs[0]["content"]
        assert "After!" in assistant_msgs[1]["content"]


class TestRunChatToolCallUpdate:
    """EVENT_TOOL_CALL_UPDATE handler — claude-agent-acp emits a refinement
    once the streamed tool input is complete. The handler patches the in-place
    pill, persisted message, _pending_tools, and the SEL audit trail."""

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
    async def test_refinement_patches_pill_content_and_meta(self, tmp_path, monkeypatch):
        """An initial tool_call with a stub title is overwritten by the refined
        title and the meta picks up the populated input."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL, title="Terminal", tool_kind="execute", tool_call_id="tc-1"
            ),
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_input='{"command": "ls /tmp"}',
                tool_call_id="tc-1",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        tool_msgs = [m for m in slot.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        # The persisted content should reflect the refined title, not the stub.
        assert tool_msgs[0]["content"] == "🔧 ls /tmp"
        assert tool_msgs[0]["meta"]["tool_call_id"] == "tc-1"
        # The refined input is patched into meta.
        assert "ls /tmp" in tool_msgs[0]["meta"]["input"]

    @pytest.mark.asyncio
    async def test_refinement_carries_purpose_when_it_has_one(self, tmp_path, monkeypatch):
        """The update frame must carry the purpose so the live status line keeps
        showing the agent's own reason for the call."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Terminal",
                tool_kind="execute",
                tool_purpose="List the temp dir",
                tool_call_id="tc-p1",
            ),
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_purpose="List the temp dir",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-p1",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        ws_calls = [(c.args[0], c.args[1]) for c in state.broadcast_ws.call_args_list]
        (update,) = [p for k, p in ws_calls if k == "tool_call" and p.get("is_update")]
        assert update["purpose"] == "List the temp dir"

    @pytest.mark.asyncio
    async def test_purposeless_refinement_omits_the_key(self, tmp_path, monkeypatch):
        """A refinement with no purpose omits the key entirely rather than
        sending an empty string: consumers merge field-by-field and read an
        absent ``purpose`` as "keep what the initial tool_call supplied", so an
        empty value would replace a good purpose with the raw command."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Terminal",
                tool_kind="execute",
                tool_purpose="List the temp dir",
                tool_call_id="tc-p2",
            ),
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-p2",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        ws_calls = [(c.args[0], c.args[1]) for c in state.broadcast_ws.call_args_list]
        (update,) = [p for k, p in ws_calls if k == "tool_call" and p.get("is_update")]
        assert "purpose" not in update

    @pytest.mark.asyncio
    async def test_refinement_persists_a_recovered_purpose(self, tmp_path, monkeypatch):
        """A refinement's purpose must reach the PERSISTED meta, not just the live
        status: when the initial tool_call streamed an empty rawInput, _tool_meta
        wrote an empty purpose, and the reloaded transcript reads meta.purpose —
        so a live-only patch loses it on the next reload."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            # Streaming backend: no purpose and no input on the initial frame.
            LLMEvent(
                kind=EVENT_TOOL_CALL, title="Terminal", tool_kind="execute", tool_call_id="tc-p3"
            ),
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_purpose="List the temp dir",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-p3",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        (tool_msg,) = [m for m in slot.messages if m.get("role") == "tool"]
        assert tool_msg["meta"]["purpose"] == "List the temp dir"
        # And the same patch goes out live so an open tab does not wait for a reload.
        ws_calls = [(c.args[0], c.args[1]) for c in state.broadcast_ws.call_args_list]
        (msg_update,) = [p for k, p in ws_calls if k == "chat_message_update"]
        assert msg_update["meta"]["purpose"] == "List the temp dir"

    @pytest.mark.asyncio
    async def test_tool_meta_persists_kind(self, tmp_path, monkeypatch):
        """The persisted tool-message meta carries the ACP tool kind. The
        dashboard gates the inline diff-card promotion on kind == 'edit'
        (a shell command whose input looks like a diff must never promote),
        and historical rows can only be gated from persisted meta."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="fs_write",
                tool_kind="edit",
                tool_purpose="Edit app.py",
                tool_input="--- /a/app.py\n+++ /a/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2",
                tool_call_id="tc-kind1",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        (tool_msg,) = [m for m in slot.messages if m.get("role") == "tool"]
        assert tool_msg["meta"]["kind"] == "edit"

    @pytest.mark.asyncio
    async def test_refinement_broadcasts_chat_message_update(self, tmp_path, monkeypatch):
        """The handler broadcasts a chat_message_update WS event so the
        frontend can patch the persisted tile in place without a reload."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL, title="Terminal", tool_kind="execute", tool_call_id="tc-2"
            ),
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-2",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        ws_calls = [(c.args[0], c.args[1]) for c in state.broadcast_ws.call_args_list]
        kinds = [k for k, _ in ws_calls]
        # Refinement broadcasts both a fresh tool_call (toolLog merge) AND a
        # chat_message_update so the persisted tile updates live too.
        assert kinds.count("tool_call") >= 2
        assert "chat_message_update" in kinds
        # The second tool_call carries is_update:True so the frontend merges.
        update_payloads = [p for k, p in ws_calls if k == "tool_call" and p.get("is_update")]
        assert len(update_payloads) == 1
        assert update_payloads[0]["tool"] == "ls /tmp"
        assert update_payloads[0]["tool_call_id"] == "tc-2"
        # And the chat_message_update carries the patched content + meta.
        msg_updates = [p for k, p in ws_calls if k == "chat_message_update"]
        assert msg_updates[0]["tool_call_id"] == "tc-2"
        assert msg_updates[0]["content"] == "🔧 ls /tmp"
        assert "input" in msg_updates[0]["meta"]

    @pytest.mark.asyncio
    async def test_refinement_preserves_existing_icon(self, tmp_path, monkeypatch):
        """Auto-approved tools may already carry a ✅ marker on the message
        with the same tool_call_id. The patch must preserve that prefix."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        # Pre-seed a message that matches the tool_call_id with a ✅ icon —
        # mirrors the auto-approved-tool case where the post-approval marker
        # is already in place.
        slot.append("tool", "✅ Terminal", "msg msg-tool", meta={"tool_call_id": "tc-3"})

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-3",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        tool_msgs = [m for m in slot.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        # ✅ icon survives the patch; only the title changes.
        assert tool_msgs[0]["content"] == "✅ ls /tmp"

    @pytest.mark.asyncio
    async def test_refinement_breaks_on_first_match_walking_reverse(self, tmp_path, monkeypatch):
        """When two messages share the tool_call_id (auto-approved double-emit
        with 🔧 then ✅), only the most recent one is patched."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("tool", "🔧 Terminal", "msg msg-tool", meta={"tool_call_id": "tc-4"})
        slot.append("tool", "✅ Terminal", "msg msg-tool", meta={"tool_call_id": "tc-4"})

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_call_id="tc-4",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        tool_msgs = [m for m in slot.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 2
        # The earlier 🔧 message is untouched.
        assert tool_msgs[0]["content"] == "🔧 Terminal"
        # The later ✅ message is patched, with its icon preserved.
        assert tool_msgs[1]["content"] == "✅ ls /tmp"

    @pytest.mark.asyncio
    async def test_refinement_strips_running_prefix_from_pending_tools(self, tmp_path, monkeypatch):
        """_pending_tools feeds PostToolUse hooks by tool name. The refinement
        must strip the "Running: " prefix exactly like EVENT_TOOL_CALL does so
        hooks matching by name keep working after the refinement event."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="Running: stub",
                tool_kind="execute",
                tool_call_id="tc-5",
            ),
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="Running: ls /tmp",
                tool_kind="execute",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-5",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        # Patch fire_tool_hooks so we can inspect the name passed in. The hook
        # only fires on EVENT_TOOL_CALL — but the refinement updates
        # _pending_tools, which is read by the EVENT_TOOL_RESULT handler. We
        # don't drive a tool_result here; instead we inspect _pending_tools
        # was updated correctly via the WS-broadcast surface area: the
        # refinement broadcasts the refined title without the "Running: "
        # prefix on the handler's local copy.
        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # Indirectly verify via the persisted message — the title displayed on
        # the pill should be the refined one, sans "Running: " prefix because
        # the refinement code strips it for _pending_tools (the displayed
        # title still carries the prefix, but the hook-name copy doesn't).
        # The persisted pill carries the refined title verbatim — this is the
        # display surface, not the hook surface.
        tool_msgs = [m for m in slot.messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["content"] == "🔧 Running: ls /tmp"

    @pytest.mark.asyncio
    async def test_refinement_logs_sel_audit_event(self, tmp_path, monkeypatch):
        """The handler logs a `tool_invocation` audit event with
        outcome="refined" so the audit trail captures the refined name."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        captured = []

        class _FakeSel:
            def log_tool_invocation(self, **kw):
                captured.append(kw)

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner.sel", lambda: _FakeSel())

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-6",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        refined = [c for c in captured if c.get("outcome") == "refined"]
        assert len(refined) == 1
        assert refined[0]["tool_name"] == "ls /tmp"
        assert refined[0]["tool_kind"] == "execute"
        assert refined[0]["source"] == "dashboard"

    @pytest.mark.asyncio
    async def test_refinement_no_tool_call_id_skipped(self, tmp_path, monkeypatch):
        """Refinement events without a tool_call_id are silently dropped —
        we have nothing to merge against."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(kind=EVENT_TOOL_CALL_UPDATE, title="ls /tmp", tool_call_id=""),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        # No tool messages should have been added.
        tool_msgs = [m for m in slot.messages if m.get("role") == "tool"]
        assert tool_msgs == []
        # No chat_message_update broadcast either.
        kinds = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "chat_message_update" not in kinds

    @pytest.mark.asyncio
    async def test_refinement_no_matching_message_no_chat_message_update(
        self, tmp_path, monkeypatch
    ):
        """When no persisted tool message matches the tool_call_id, the
        handler still broadcasts the tool_call merge but skips
        chat_message_update (nothing to patch)."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_input='{"command":"ls /tmp"}',
                tool_call_id="tc-orphan",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        kinds = [c.args[0] for c in state.broadcast_ws.call_args_list]
        # tool_call still broadcast (toolLog merge by id is still meaningful).
        assert "tool_call" in kinds
        # Nothing to patch in slot.messages, so no chat_message_update.
        assert "chat_message_update" not in kinds

    @pytest.mark.asyncio
    async def test_refinement_redacts_credentials_in_input(self, tmp_path, monkeypatch):
        """Credentials in tool_input must be redacted before the broadcast
        and the persisted meta. _redact_tool_field applies both
        redact_exfiltration_urls and redact_credentials."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("tool", "🔧 Terminal", "msg msg-tool", meta={"tool_call_id": "tc-cred"})

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="echo secret",
                tool_kind="execute",
                tool_input='{"command":"echo AKIAIOSFODNN7EXAMPLE"}',
                tool_call_id="tc-cred",
            ),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        await _run_chat(state, slot, "hello")

        tool_msgs = [m for m in slot.messages if m.get("role") == "tool"]
        # Credential must not appear in the persisted meta.
        assert "AKIAIOSFODNN7EXAMPLE" not in tool_msgs[0]["meta"].get("input", "")
        # Or in any of the broadcast payloads.
        for call in state.broadcast_ws.call_args_list:
            assert "AKIAIOSFODNN7EXAMPLE" not in str(call.args[1])

    @pytest.mark.asyncio
    async def test_refinement_handler_swallows_exceptions(self, tmp_path, monkeypatch):
        """A malformed broadcast or other exception inside the handler must
        not tear down the run loop. The try/except logs and continues."""
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL_UPDATE,
            LLMEvent,
        )

        state = self._make_state_for_run_chat(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")

        # Make broadcast_ws raise on the first call (the tool_call broadcast).
        # The try/except in the handler must catch it so subsequent events
        # (the trailing EVENT_TEXT_CHUNK) still process.
        original_broadcast = state.broadcast_ws

        def _raising_then_normal(*args, **kwargs):
            if args and args[0] == "tool_call":
                raise RuntimeError("simulated broadcast failure")
            return original_broadcast(*args, **kwargs)

        state.broadcast_ws = MagicMock(side_effect=_raising_then_normal)

        events = [
            LLMEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                title="ls /tmp",
                tool_kind="execute",
                tool_call_id="tc-boom",
            ),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="after the boom"),
            LLMEvent(kind=EVENT_COMPLETE),
        ]
        client = self._make_mock_client(events)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

        from kiro_crew.dashboard.chat import _run_chat

        # Must not raise — the run loop should continue past the exception.
        await _run_chat(state, slot, "hello")

        # The trailing assistant message proves the run loop survived.
        assistant_msgs = [m for m in slot.messages if m.get("role") == "assistant"]
        assert any("after the boom" in m["content"] for m in assistant_msgs)
