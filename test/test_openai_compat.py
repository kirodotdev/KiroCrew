"""Tests for OpenAI-compatible /v1/chat/completions endpoint."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.openai_compat import _flatten_messages, _make_id, api_completions
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService


class _ReadyKiroPrerequisiteService(KiroPrerequisiteService):
    async def session_ready(self) -> bool:
        return True


_READY_KIRO_PREREQUISITE = object.__new__(_ReadyKiroPrerequisiteService)


# ---------------------------------------------------------------------------
# Unit: _flatten_messages
# ---------------------------------------------------------------------------


class TestFlattenMessages:
    def test_single_user_message(self):
        msgs = [{"role": "user", "content": "hello"}]
        assert _flatten_messages(msgs) == "hello"

    def test_empty_list(self):
        assert _flatten_messages([]) == ""

    def test_system_prepended(self):
        msgs = [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
        ]
        result = _flatten_messages(msgs)
        assert "[SYSTEM] you are helpful" in result
        assert "hi" in result
        assert "CONTEXT ENTRY" in result

    def test_assistant_included_as_context(self):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "response"},
            {"role": "user", "content": "second"},
        ]
        result = _flatten_messages(msgs)
        assert "second" in result
        assert "[Previous assistant response] response" in result

    def test_multimodal_content_array(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "http://example.com/img.png"}},
                ],
            }
        ]
        assert _flatten_messages(msgs) == "describe this"

    def test_last_user_wins(self):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ]
        result = _flatten_messages(msgs)
        assert "second" in result
        assert "[Previous user message] first" in result


# ---------------------------------------------------------------------------
# Unit: _make_id
# ---------------------------------------------------------------------------


class TestMakeId:
    def test_format(self):
        id_ = _make_id()
        assert id_.startswith("chatcmpl-")
        assert len(id_) == len("chatcmpl-") + 24

    def test_unique(self):
        assert _make_id() != _make_id()


# ---------------------------------------------------------------------------
# Integration: api_completions
# ---------------------------------------------------------------------------


def _make_slot():
    """Create a minimal mock slot that behaves like _ChatSlot."""
    slot = MagicMock()
    slot.key = "test-slot"
    slot.agent = ""
    slot.task = None
    slot.event = asyncio.Event()
    slot._pending = []

    def drain():
        out = slot._pending[:]
        slot._pending.clear()
        slot.event.clear()
        return out

    slot.drain = drain
    return slot


def _make_state(slot):
    state = MagicMock()
    state.get_or_create_slot = MagicMock(return_value=slot)
    state._slots = {slot.key: slot}
    state._background_tasks = set()
    return state


def _make_request(body: dict, state, app: str = ""):
    request = MagicMock()
    request.json = AsyncMock(return_value=body)
    request.app = {
        "state": state,
        "kiro_prerequisite_service": _READY_KIRO_PREREQUISITE,
    }
    request.get = MagicMock(side_effect=lambda k, d="": app if k == "app" else d)
    request.remote = "127.0.0.1"
    return request


@pytest.mark.asyncio
class TestApiCompletionsBlocking:
    async def test_prerequisite_error_uses_openai_schema(self, tmp_path):
        slot = _make_slot()
        state = _make_state(slot)
        request = _make_request(
            {"model": "kiro", "messages": [{"role": "user", "content": "hello"}]},
            state,
        )
        service = KiroPrerequisiteService(
            platform_name="linux",
            environ={"HOME": str(tmp_path), "PATH": ""},
            home=tmp_path,
            audit_writer=AsyncMock(),
        )
        service._has_probed = True
        request.app["kiro_prerequisite_service"] = service

        response = await api_completions(request)
        body = json.loads(response.text)

        assert response.status == 503
        assert body["error"]["code"] == "kiro_prerequisite_required"
        assert body["error"]["type"] == "service_unavailable_error"
        assert isinstance(body["error"]["message"], str)

    async def test_basic_response(self):
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "model": "vanellope",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        request = _make_request(body, state)

        # Simulate the assistant responding then done
        async def fake_run_chat(s, sl, prompt):
            slot._pending.append({"role": "assistant", "content": "hey there"})
            slot._pending.append({"cls": "done"})
            slot.event.set()

        with patch("kiro_crew.dashboard.openai_compat._run_chat", side_effect=fake_run_chat):
            resp = await api_completions(request)

        data = json.loads(resp.body)
        assert data["object"] == "chat.completion"
        assert data["model"] == "vanellope"
        assert data["choices"][0]["message"]["content"] == "hey there"
        assert data["choices"][0]["finish_reason"] == "stop"

    async def test_missing_messages_returns_400(self):
        slot = _make_slot()
        state = _make_state(slot)
        request = _make_request({"model": "x", "messages": []}, state)

        resp = await api_completions(request)
        assert resp.status == 400

    async def test_invalid_json_returns_400(self):
        state = MagicMock()
        request = MagicMock()
        request.json = AsyncMock(side_effect=ValueError("bad json"))
        request.app = {
            "state": state,
            "kiro_prerequisite_service": _READY_KIRO_PREREQUISITE,
        }

        resp = await api_completions(request)
        assert resp.status == 400


@pytest.mark.asyncio
class TestSlotTargeting:
    async def test_id_field_targets_named_slot(self):
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "id": "my-conversation",
            "model": "vanellope",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        request = _make_request(body, state)

        async def fake_run_chat(s, sl, prompt):
            slot._pending.append({"role": "assistant", "content": "yo"})
            slot._pending.append({"cls": "done"})
            slot.event.set()

        with patch("kiro_crew.dashboard.openai_compat._run_chat", side_effect=fake_run_chat):
            await api_completions(request)

        # Should have called get_or_create_slot with the id value
        state.get_or_create_slot.assert_called_with("my-conversation")
        # Named slot should NOT be cleaned up
        assert slot.key in state._slots

    async def test_no_id_creates_ephemeral_slot(self):
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "model": "vanellope",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        request = _make_request(body, state)

        async def fake_run_chat(s, sl, prompt):
            slot._pending.append({"role": "assistant", "content": "yo"})
            slot._pending.append({"cls": "done"})
            slot.event.set()

        with patch("kiro_crew.dashboard.openai_compat._run_chat", side_effect=fake_run_chat):
            await api_completions(request)

        # Ephemeral slot name starts with oai-
        call_arg = state.get_or_create_slot.call_args[0][0]
        assert call_arg.startswith("oai-chatcmpl-")


@pytest.mark.asyncio
class TestAgentMapping:
    async def test_model_sets_agent(self):
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "model": "oncall-triage",
            "messages": [{"role": "user", "content": "check tickets"}],
            "stream": False,
        }
        request = _make_request(body, state)

        async def fake_run_chat(s, sl, prompt):
            slot._pending.append({"role": "assistant", "content": "done"})
            slot._pending.append({"cls": "done"})
            slot.event.set()

        with patch("kiro_crew.dashboard.openai_compat._run_chat", side_effect=fake_run_chat):
            await api_completions(request)

        assert slot.agent == "oncall-triage"


# ---------------------------------------------------------------------------
# Integration: streaming mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestStreamingResponse:
    async def test_stream_sends_sse_chunks(self):
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "model": "vanellope",
            "messages": [{"role": "user", "content": "count"}],
            "stream": True,
        }
        request = _make_request(body, state)
        # Mock resp.prepare and resp.write
        written = []

        async def fake_prepare(req):
            pass

        async def fake_write(data):
            written.append(data)

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock(side_effect=fake_prepare)
        mock_resp.write = AsyncMock(side_effect=fake_write)
        mock_resp.content_type = None
        mock_resp.headers = {}

        async def fake_run_chat(s, sl, prompt):
            slot._pending.append({"role": "assistant", "content": "1 2 3"})
            slot._pending.append({"cls": "done"})
            slot.event.set()

        with (
            patch("kiro_crew.dashboard.openai_compat._run_chat", side_effect=fake_run_chat),
            patch("kiro_crew.dashboard.openai_compat.web.StreamResponse", return_value=mock_resp),
        ):
            await api_completions(request)

        # Should have written content chunk + stop chunk + [DONE]
        text_chunks = [w.decode() for w in written if isinstance(w, bytes)]
        combined = "".join(text_chunks)
        assert "1 2 3" in combined
        assert '"finish_reason": "stop"' in combined
        assert "data: [DONE]" in combined

    async def test_stream_skips_non_assistant_messages(self):
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "model": "vanellope",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        request = _make_request(body, state)
        written = []

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock()
        mock_resp.write = AsyncMock(side_effect=lambda d: written.append(d))
        mock_resp.content_type = None
        mock_resp.headers = {}

        async def fake_run_chat(s, sl, prompt):
            slot._pending.append({"role": "system", "content": "ignored"})
            slot._pending.append({"role": "assistant", "content": ""})  # empty, skipped
            slot._pending.append({"role": "assistant", "content": "hello"})
            slot._pending.append({"cls": "done"})
            slot.event.set()

        with (
            patch("kiro_crew.dashboard.openai_compat._run_chat", side_effect=fake_run_chat),
            patch("kiro_crew.dashboard.openai_compat.web.StreamResponse", return_value=mock_resp),
        ):
            await api_completions(request)

        combined = "".join(w.decode() for w in written if isinstance(w, bytes))
        assert "ignored" not in combined
        assert "hello" in combined

    async def test_stream_ephemeral_cleanup(self):
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "model": "vanellope",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        request = _make_request(body, state)

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock()
        mock_resp.write = AsyncMock()
        mock_resp.content_type = None
        mock_resp.headers = {}

        async def fake_run_chat(s, sl, prompt):
            slot._pending.append({"role": "assistant", "content": "yo"})
            slot._pending.append({"cls": "done"})
            slot.event.set()

        with (
            patch("kiro_crew.dashboard.openai_compat._run_chat", side_effect=fake_run_chat),
            patch("kiro_crew.dashboard.openai_compat.web.StreamResponse", return_value=mock_resp),
        ):
            await api_completions(request)

        # Ephemeral slot should be cleaned up
        assert slot.key not in state._slots

    async def test_stream_named_slot_not_cleaned(self):
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "id": "keep-me",
            "model": "vanellope",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        request = _make_request(body, state)

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock()
        mock_resp.write = AsyncMock()
        mock_resp.content_type = None
        mock_resp.headers = {}

        async def fake_run_chat(s, sl, prompt):
            slot._pending.append({"role": "assistant", "content": "yo"})
            slot._pending.append({"cls": "done"})
            slot.event.set()

        with (
            patch("kiro_crew.dashboard.openai_compat._run_chat", side_effect=fake_run_chat),
            patch("kiro_crew.dashboard.openai_compat.web.StreamResponse", return_value=mock_resp),
        ):
            await api_completions(request)

        # Named slot preserved
        assert slot.key in state._slots

    async def test_stream_connection_reset_cleanup(self):
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "model": "vanellope",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        request = _make_request(body, state)

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock()
        mock_resp.write = AsyncMock(side_effect=ConnectionResetError("client gone"))
        mock_resp.content_type = None
        mock_resp.headers = {}

        async def fake_run_chat(s, sl, prompt):
            slot._pending.append({"role": "assistant", "content": "x" * 300})
            slot.event.set()

        with (
            patch("kiro_crew.dashboard.openai_compat._run_chat", side_effect=fake_run_chat),
            patch("kiro_crew.dashboard.openai_compat.web.StreamResponse", return_value=mock_resp),
        ):
            await api_completions(request)

        # Ephemeral slot cleaned up even on error
        assert slot.key not in state._slots


# ---------------------------------------------------------------------------
# Edge cases: blocking timeout and empty agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBlockingEdgeCases:
    async def test_empty_model_field(self):
        """Empty model returns 400 (model is required per OpenAI contract)."""
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "model": "",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        request = _make_request(body, state)

        resp = await api_completions(request)
        assert resp.status == 400

    async def test_non_assistant_messages_ignored_in_blocking(self):
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "model": "vanellope",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        request = _make_request(body, state)

        async def fake_run_chat(s, sl, prompt):
            slot._pending.append({"role": "tool", "content": "tool output"})
            slot._pending.append({"role": "assistant", "content": "result"})
            slot._pending.append({"cls": "done"})
            slot.event.set()

        with patch("kiro_crew.dashboard.openai_compat._run_chat", side_effect=fake_run_chat):
            resp = await api_completions(request)

        data = json.loads(resp.body)
        assert data["choices"][0]["message"]["content"] == "result"
        assert "tool output" not in data["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Coverage: remaining edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRemainingCoverage:
    async def test_only_system_message_no_user(self):
        """Messages with no user role should return 400."""
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "model": "vanellope",
            "messages": [{"role": "system", "content": "be nice"}],
            "stream": False,
        }
        request = _make_request(body, state)

        with patch("kiro_crew.dashboard.openai_compat._run_chat", new=AsyncMock()):
            resp = await api_completions(request)

        assert resp.status == 400

    async def test_flatten_only_assistant_messages(self):
        """Only assistant messages with no user → empty string."""
        msgs = [{"role": "assistant", "content": "I said something"}]
        assert _flatten_messages(msgs) == ""

    async def test_stream_keepalive_on_timeout(self):
        """When slot.event.wait times out, a keepalive comment is sent."""
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "model": "vanellope",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        request = _make_request(body, state)
        written = []
        call_count = [0]

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock()
        mock_resp.content_type = None
        mock_resp.headers = {}

        async def fake_write(data):
            written.append(data)

        mock_resp.write = AsyncMock(side_effect=fake_write)

        async def fake_run_chat(s, sl, prompt):
            # Don't set event immediately — let the wait timeout once
            pass

        original_wait_for = asyncio.wait_for

        async def patched_wait_for(coro, timeout):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: simulate timeout (keepalive)
                raise asyncio.TimeoutError()
            else:
                # Second call: deliver done
                slot._pending.append({"role": "assistant", "content": "hi"})
                slot._pending.append({"cls": "done"})
                slot.event.set()
                return await original_wait_for(coro, timeout=1)

        with (
            patch("kiro_crew.dashboard.openai_compat._run_chat", side_effect=fake_run_chat),
            patch("kiro_crew.dashboard.openai_compat.web.StreamResponse", return_value=mock_resp),
            patch(
                "kiro_crew.dashboard.openai_compat.asyncio.wait_for", side_effect=patched_wait_for
            ),
        ):
            await api_completions(request)

        combined = b"".join(written)
        assert b": keepalive" in combined

    async def test_blocking_waits_for_event(self):
        """Blocking mode waits through event.wait timeout loops."""
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "model": "vanellope",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        request = _make_request(body, state)
        call_count = [0]

        async def fake_run_chat(s, sl, prompt):
            pass  # Don't deliver immediately

        original_wait_for = asyncio.wait_for

        async def patched_wait_for(coro, timeout):
            call_count[0] += 1
            if call_count[0] == 1:
                raise asyncio.TimeoutError()
            else:
                slot._pending.append({"role": "assistant", "content": "delayed"})
                slot._pending.append({"cls": "done"})
                slot.event.set()
                return await original_wait_for(coro, timeout=1)

        with (
            patch("kiro_crew.dashboard.openai_compat._run_chat", side_effect=fake_run_chat),
            patch(
                "kiro_crew.dashboard.openai_compat.asyncio.wait_for", side_effect=patched_wait_for
            ),
        ):
            resp = await api_completions(request)

        data = json.loads(resp.body)
        assert data["choices"][0]["message"]["content"] == "delayed"

    async def test_stream_connection_reset_named_slot_preserved(self):
        """Named slot survives ConnectionResetError in streaming."""
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "id": "persistent-chat",
            "model": "vanellope",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        request = _make_request(body, state)

        mock_resp = MagicMock()
        mock_resp.prepare = AsyncMock()
        mock_resp.write = AsyncMock(side_effect=ConnectionResetError("gone"))
        mock_resp.content_type = None
        mock_resp.headers = {}

        async def fake_run_chat(s, sl, prompt):
            slot._pending.append({"role": "assistant", "content": "x" * 300})
            slot.event.set()

        with (
            patch("kiro_crew.dashboard.openai_compat._run_chat", side_effect=fake_run_chat),
            patch("kiro_crew.dashboard.openai_compat.web.StreamResponse", return_value=mock_resp),
        ):
            await api_completions(request)

        # Named slot NOT cleaned up
        assert slot.key in state._slots


# ---------------------------------------------------------------------------
# Rev 3 comment fixes: App-Kit, unsupported roles, multimodal, agent mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAppKitOwnership:
    async def test_app_scoped_caller_denied_unscoped_slot(self):
        """App-scoped caller cannot access dashboard-owned (unscoped) slots."""
        slot = _make_slot()
        slot._app = ""  # unscoped
        state = _make_state(slot)

        body = {
            "id": "test-slot",
            "model": "vanellope",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        request = _make_request(body, state, app="app-A")

        resp = await api_completions(request)
        assert resp.status == 403

    async def test_app_scoped_caller_denied_other_app_slot(self):
        """App-A cannot access a slot owned by app-B."""
        slot = _make_slot()
        slot._app = "app-B"
        state = _make_state(slot)

        body = {
            "id": "test-slot",
            "model": "vanellope",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        request = _make_request(body, state, app="app-A")

        resp = await api_completions(request)
        assert resp.status == 403

    async def test_app_scoped_caller_allowed_own_slot(self):
        """App-A can access its own slot."""
        slot = _make_slot()
        slot._app = "app-A"
        state = _make_state(slot)

        body = {
            "id": "test-slot",
            "model": "vanellope",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        request = _make_request(body, state, app="app-A")

        async def fake_run_chat(s, sl, prompt):
            slot._pending.append({"role": "assistant", "content": "yo"})
            slot._pending.append({"cls": "done"})
            slot.event.set()

        with patch("kiro_crew.dashboard.openai_compat._run_chat", side_effect=fake_run_chat):
            resp = await api_completions(request)

        assert resp.status == 200

    async def test_no_app_header_skips_check(self):
        """Non-app callers (dashboard, CLI) skip App-Kit check entirely."""
        slot = _make_slot()
        slot._app = "some-app"
        state = _make_state(slot)

        body = {
            "id": "test-slot",
            "model": "vanellope",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        request = _make_request(body, state, app="")

        async def fake_run_chat(s, sl, prompt):
            slot._pending.append({"role": "assistant", "content": "yo"})
            slot._pending.append({"cls": "done"})
            slot.event.set()

        with patch("kiro_crew.dashboard.openai_compat._run_chat", side_effect=fake_run_chat):
            resp = await api_completions(request)

        assert resp.status == 200


@pytest.mark.asyncio
class TestUnsupportedRoles:
    async def test_tool_role_rejected(self):
        """Messages with role='tool' return 400."""
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "model": "vanellope",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "tool", "content": "tool output", "tool_call_id": "123"},
            ],
            "stream": False,
        }
        request = _make_request(body, state)

        resp = await api_completions(request)
        assert resp.status == 400
        data = json.loads(resp.body)
        assert "tool" in data["error"]["message"]

    async def test_function_role_rejected(self):
        """Messages with role='function' return 400."""
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "model": "vanellope",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "function", "content": "fn output", "name": "get_weather"},
            ],
            "stream": False,
        }
        request = _make_request(body, state)

        resp = await api_completions(request)
        assert resp.status == 400
        data = json.loads(resp.body)
        assert "function" in data["error"]["message"]

    async def test_multimodal_image_rejected(self):
        """Messages with image_url content parts return 400."""
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "model": "vanellope",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {"type": "image_url", "image_url": {"url": "http://x.com/img.png"}},
                    ],
                }
            ],
            "stream": False,
        }
        request = _make_request(body, state)

        resp = await api_completions(request)
        assert resp.status == 400
        data = json.loads(resp.body)
        assert "multimodal" in data["error"]["message"]

    async def test_text_only_content_array_allowed(self):
        """Messages with only text parts in content array are allowed."""
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "model": "vanellope",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "just text"}],
                }
            ],
            "stream": False,
        }
        request = _make_request(body, state)

        async def fake_run_chat(s, sl, prompt):
            slot._pending.append({"role": "assistant", "content": "ok"})
            slot._pending.append({"cls": "done"})
            slot.event.set()

        with patch("kiro_crew.dashboard.openai_compat._run_chat", side_effect=fake_run_chat):
            resp = await api_completions(request)

        assert resp.status == 200


@pytest.mark.asyncio
class TestAgentMismatchFix:
    async def test_continuation_without_model_allowed(self):
        """Omitting model on continuation (model required, so this is N/A).

        Since model is now required, this tests that passing the SAME model
        on a continuation works fine.
        """
        slot = _make_slot()
        slot.agent = "vanellope"
        state = _make_state(slot)

        body = {
            "id": "test-slot",
            "model": "vanellope",
            "messages": [{"role": "user", "content": "continue"}],
            "stream": False,
        }
        request = _make_request(body, state)

        async def fake_run_chat(s, sl, prompt):
            slot._pending.append({"role": "assistant", "content": "continued"})
            slot._pending.append({"cls": "done"})
            slot.event.set()

        with patch("kiro_crew.dashboard.openai_compat._run_chat", side_effect=fake_run_chat):
            resp = await api_completions(request)

        assert resp.status == 200

    async def test_different_agent_on_existing_slot_denied(self):
        """Explicitly supplying a different agent on existing slot returns 409."""
        slot = _make_slot()
        slot.agent = "vanellope"
        state = _make_state(slot)

        body = {
            "id": "test-slot",
            "model": "kirocrew",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        request = _make_request(body, state)

        resp = await api_completions(request)
        assert resp.status == 409

    async def test_null_model_returns_400(self):
        """model=null (None in JSON) returns 400."""
        slot = _make_slot()
        state = _make_state(slot)

        body = {
            "model": None,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }
        request = _make_request(body, state)

        resp = await api_completions(request)
        assert resp.status == 400
