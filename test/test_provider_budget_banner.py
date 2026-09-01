"""Regression tests for provider-only token-budget banners in dashboard chat."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state


class TestProviderBudgetBannerRecovery:
    """A backend-only context-budget reminder must not become chat content."""

    @pytest.mark.parametrize(
        ("raw", "expected", "removed"),
        [
            ("You have 8154 weighted tokens left", "", True),
            ("  You have 8,154 weighted tokens left.\nFinal answer", "Final answer", True),
            ("You have 1461 weighted tokens leftTwo tasks remain", "Two tasks remain", True),
            ("You have weighted tokens left", "You have weighted tokens left", False),
            (
                "The provider said: You have 8154 weighted tokens left",
                "The provider said: You have 8154 weighted tokens left",
                False,
            ),
            (
                "You have 8154 weighted tokens left for this operation",
                "You have 8154 weighted tokens left for this operation",
                False,
            ),
            ("`You have 8154 weighted tokens left`", "`You have 8154 weighted tokens left`", False),
        ],
    )
    def test_strip_is_narrow(self, raw, expected, removed):
        from kiro_crew.dashboard import chat_runner

        assert hasattr(chat_runner, "_strip_provider_budget_banner")
        strip_banner = chat_runner._strip_provider_budget_banner
        assert strip_banner(raw) == (expected, removed)

    @staticmethod
    def _state(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.push_refresh = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @staticmethod
    def _wire(state, client):
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.get_pid = MagicMock(return_value=None)
        state.sessions.check_context_usage = MagicMock()
        state.sessions.record_success = MagicMock()
        state.sessions.record_failure = AsyncMock()
        state.sessions.release = MagicMock()
        state.sessions.reset = AsyncMock()
        state.sessions.discard_conversation = AsyncMock()
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        client.context_window_tokens = MagicMock(return_value=0)
        client.context_used_tokens = MagicMock(return_value=0)
        client.client = MagicMock()
        client.client.pop_pending_oauth_requests = MagicMock(return_value=[])

    @staticmethod
    async def _drain_bg(state, limit=30):
        for _ in range(limit):
            pending = [task for task in list(state._background_tasks) if not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_mid_turn_exact_text_is_preserved(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted tokens left")
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="read_file",
                tool_kind="read",
                tool_call_id="tc-1",
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Final answer")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        await _run_chat(state, slot, "answer")
        await self._drain_bg(state)

        assistant = [m.get("content", "") for m in slot.messages if m.get("role") == "assistant"]
        assert assistant == ["You have 8154 weighted tokens left", "Final answer"]

    @pytest.mark.asyncio
    async def test_banner_only_final_segment_is_suppressed_and_continued_once(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        captured: list[str] = []

        async def _stream(message):
            captured.append(message)
            if len(captured) == 1:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Applying the fix.")
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="write_file",
                    tool_kind="write",
                    tool_call_id="tc-1",
                )
                # Split the artifact across chunks to match real provider streaming.
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted ")
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="tokens left")
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Finished safely.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        await _run_chat(state, slot, "fix the bug")
        await self._drain_bg(state)

        assistant = [m.get("content", "") for m in slot.messages if m.get("role") == "assistant"]
        assert not any("weighted tokens left" in text for text in assistant)
        assert any("Applying the fix." in text for text in assistant)
        assert any("Finished safely." in text for text in assistant)
        assert len(captured) == 2
        assert "Continue from where it stopped" in captured[1]
        assert "fix the bug" not in captured[1]
        frames = state.broadcast_ws.call_args_list
        empty_frame = next(
            i
            for i, call in enumerate(frames)
            if call.args
            == (
                "chat_message",
                {"slot": "s1", "role": "assistant", "content": ""},
            )
        )
        segment_frame = next(
            i for i, call in enumerate(frames) if i > empty_frame and call.args[0] == "chat_segment"
        )
        assert empty_frame < segment_frame
        assert slot._posttoken_retry_used is True
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_banner_prefix_keeps_real_final_text_without_recovery(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        calls = 0

        async def _stream(_message):
            nonlocal calls
            calls += 1
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="read_file",
                tool_kind="read",
                tool_call_id="tc-prefix",
            )
            yield LLMEvent(
                kind=EVENT_TEXT_CHUNK,
                text="You have 1461 weighted tokens leftFinal answer",
            )
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot._empty_response_retries = 1

        await _run_chat(state, slot, "answer")
        await self._drain_bg(state)

        assistant = [m.get("content", "") for m in slot.messages if m.get("role") == "assistant"]
        assert assistant == ["Final answer"]
        assert calls == 1
        assert slot._empty_response_retries == 0
        assert slot._posttoken_retry_used is False
        state.sessions.record_success.assert_called_once()

    @pytest.mark.asyncio
    async def test_repeated_banner_does_not_loop(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        calls = 0

        async def _stream(_message):
            nonlocal calls
            calls += 1
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted tokens left")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        consolidate = MagicMock()
        monkeypatch.setattr(chat_runner, "_maybe_consolidate", consolidate)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot._posttoken_retry_used = True
        slot._empty_response_retries = 1

        await _run_chat(state, slot, _POSTTOKEN_RECOVER_MSG)
        await self._drain_bg(state)

        assert calls == 1
        assert not any("weighted tokens left" in m.get("content", "") for m in slot.messages)
        assert any(
            "automatic continuation is unavailable or already spent" in m.get("content", "")
            for m in slot.messages
            if m.get("role") == "notice"
        )
        assert slot._empty_response_retries == 1
        consolidate.assert_not_called()
        state.sessions.record_success.assert_not_called()

    @staticmethod
    def _stage_state(tmp_path, monkeypatch, slot_key):
        from kiro_crew.dashboard import chat_orchestrator

        monkeypatch.setattr(chat_orchestrator, "config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=[])
        state.subagents._tasks = {}
        slot = state.get_or_create_slot(slot_key, mode="orchestrator")
        slot._stage_titles = ["Only stage"]
        slot._plan_goal = "Test provider artifact recovery"
        slot._auto_run = True
        return state, slot

    @pytest.mark.asyncio
    async def test_stage_retries_banner_before_result_capture(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat_runner import (
            _POSTTOKEN_RECOVER_MSG,
            _STOP_REASON_PROVIDER_BUDGET_ARTIFACT,
        )

        state, slot = self._stage_state(tmp_path, monkeypatch, "stage-retry")
        calls = []
        timeouts = []

        async def _mock_run_chat(_state, _slot, message, **kwargs):
            calls.append((message, kwargs))
            if len(calls) == 1:
                await asyncio.sleep(0.01)
                _slot._last_stop_reason = _STOP_REASON_PROVIDER_BUDGET_ARTIFACT
            else:
                _slot._last_stop_reason = STOP_REASON_END_TURN
                _slot.append("assistant", "stage completed", "msg msg-a")

        async def _record_bounded(coro, timeout, **_kwargs):
            timeouts.append(timeout)
            return await coro

        monkeypatch.setattr(chat_orchestrator, "_run_chat", _mock_run_chat)
        monkeypatch.setattr(chat_orchestrator, "_bounded_turn", _record_bounded)

        await chat_orchestrator._stage_loop(state, slot, auto_run=True)

        assert len(calls) == 2
        assert calls[1][0] == _POSTTOKEN_RECOVER_MSG
        assert calls[1][1]["_synthetic_payload"] is True
        assert len(timeouts) == 2
        assert 0 < timeouts[1] < timeouts[0]
        assert slot._orch_tracker is not None
        assert 1 in slot._orch_tracker._stage_results
        assert not any(
            "Auto-run stopped before marking the stage complete" in m.get("content", "")
            for m in slot.messages
        )

    @pytest.mark.asyncio
    async def test_repeated_stage_banner_stops_before_result_capture(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat_runner import _STOP_REASON_PROVIDER_BUDGET_ARTIFACT

        state, slot = self._stage_state(tmp_path, monkeypatch, "stage-repeat")
        calls = 0

        async def _mock_run_chat(_state, _slot, _message, **_kwargs):
            nonlocal calls
            calls += 1
            _slot._last_stop_reason = _STOP_REASON_PROVIDER_BUDGET_ARTIFACT

        monkeypatch.setattr(chat_orchestrator, "_run_chat", _mock_run_chat)

        await chat_orchestrator._stage_loop(state, slot, auto_run=True)

        assert calls == 2
        assert slot._auto_run is False
        assert slot._orch_tracker is not None
        assert slot._orch_tracker._stage_results == {}
        assert slot._orch_tracker._stage_rounds == {}
        assert any(
            "Auto-run stopped before marking the stage complete" in m.get("content", "")
            for m in slot.messages
        )

    @pytest.mark.parametrize("stop_on_call", [1, 2])
    @pytest.mark.asyncio
    async def test_stage_stop_generation_blocks_retry_and_capture(
        self, tmp_path, monkeypatch, stop_on_call
    ):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat_runner import _STOP_REASON_PROVIDER_BUDGET_ARTIFACT

        state, slot = self._stage_state(tmp_path, monkeypatch, f"stage-stop-{stop_on_call}")
        calls = 0

        async def _mock_run_chat(_state, _slot, _message, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                _slot._last_stop_reason = _STOP_REASON_PROVIDER_BUDGET_ARTIFACT
            else:
                _slot._last_stop_reason = STOP_REASON_END_TURN
                _slot.append("assistant", "stage completed", "msg msg-a")
            if calls == stop_on_call:
                # Model a Stop that has already resolved its point-in-time state
                # back to idle; only the monotonic generation preserves it.
                _slot._stop_generation += 1
                _slot._stop_state = "idle"

        monkeypatch.setattr(chat_orchestrator, "_run_chat", _mock_run_chat)

        await chat_orchestrator._stage_loop(state, slot, auto_run=True)

        assert calls == stop_on_call
        assert slot._orch_tracker is not None
        assert slot._orch_tracker._stage_results == {}
        assert slot._orch_tracker._stage_rounds == {}
        assert not any("All 1 stages complete" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_stage_stop_during_subagent_wait_blocks_capture(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard import chat_orchestrator

        state, slot = self._stage_state(tmp_path, monkeypatch, "stage-stop-subagent")
        pending_reads = 0

        def _running_agents(_session_key):
            nonlocal pending_reads
            pending_reads += 1
            return [{"id": "agent-1"}] if pending_reads == 1 else []

        state.subagents.running_agents_for = MagicMock(side_effect=_running_agents)

        async def _mock_run_chat(_state, _slot, _message, **_kwargs):
            _slot._last_stop_reason = STOP_REASON_END_TURN
            _slot.append("assistant", "stage completed", "msg msg-a")

        real_asyncio = asyncio

        class _AsyncioWithStop:
            async def sleep(self, _delay):
                slot._stop_generation += 1
                slot._stop_state = "idle"
                await real_asyncio.sleep(0)

            def __getattr__(self, name):
                return getattr(real_asyncio, name)

        monkeypatch.setattr(chat_orchestrator, "_run_chat", _mock_run_chat)
        monkeypatch.setattr(chat_orchestrator, "asyncio", _AsyncioWithStop())

        await chat_orchestrator._stage_loop(state, slot, auto_run=True)

        assert pending_reads == 2
        assert slot._orch_tracker is not None
        assert slot._orch_tracker._stage_results == {}
        assert slot._orch_tracker._stage_rounds == {}
        assert not any("All 1 stages complete" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_stage_stop_during_context_prep_blocks_dispatch(self, tmp_path, monkeypatch):
        from kiro_crew.context_management import OrchestrationTracker
        from kiro_crew.dashboard import chat_orchestrator

        state, slot = self._stage_state(tmp_path, monkeypatch, "stage-stop-context")
        slot._stage_titles = ["Completed stage", "Interrupted stage"]
        tracker = OrchestrationTracker()
        tracker.record_round(1)
        slot._orch_tracker = tracker
        calls = 0

        async def _stop_during_context(_slot, _tracker, _stage_idx):
            _slot._stop_generation += 1
            _slot._stop_state = "idle"
            await asyncio.sleep(0)
            return "must not dispatch"

        async def _mock_run_chat(_state, _slot, _message, **_kwargs):
            nonlocal calls
            calls += 1

        monkeypatch.setattr(chat_orchestrator, "_build_stage_context", _stop_during_context)
        monkeypatch.setattr(chat_orchestrator, "_run_chat", _mock_run_chat)

        await chat_orchestrator._stage_loop(state, slot, auto_run=True)

        assert calls == 0
        assert slot._orch_tracker is tracker
        assert slot._orch_tracker._stage_results == {}
        assert slot._orch_tracker._stage_rounds == {1: 1}
        assert not any(m.get("role") == "user" for m in slot.messages)
        assert not any("All 2 stages complete" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_explicit_requested_text_is_never_suppressed(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        exact = "You have 8154 weighted tokens left"

        async def _stream(_message):
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="read_file",
                tool_kind="read",
                tool_call_id="tc-exact",
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        await _run_chat(state, slot, f"Respond exactly: {exact}")
        await self._drain_bg(state)

        assistant = [m.get("content", "") for m in slot.messages if m.get("role") == "assistant"]
        assert assistant == [exact]
        assert slot._posttoken_retry_used is False

    def test_banner_only_regeneration_consumes_pending_variants(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _flush_segment

        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot._pending_variants = [{"content": "prior answer", "ts": "old-ts"}]
        banner = "You have 8154 weighted tokens left"
        slot.append("chunk", banner, "chunk")

        _flush_segment(
            state,
            slot,
            banner,
            broadcast=False,
            strip_provider_banner=True,
        )

        assistant = [m for m in slot.messages if m.get("role") == "assistant"]
        assert len(assistant) == 1
        assert assistant[0]["content"] == ""
        assert assistant[0]["variants"][0]["content"] == "prior answer"
        assert slot._pending_variants == []

    def test_banner_only_file_changes_use_current_turn_placeholder(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _flush_file_changes, _flush_segment

        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "preceding turn", "msg msg-a")
        changed = tmp_path / "changed.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [
            {"path": str(changed), "content": "before"},
        ]
        banner = "You have 8154 weighted tokens left"
        slot.append("chunk", banner, "chunk")

        _flush_segment(
            state,
            slot,
            banner,
            broadcast=False,
            strip_provider_banner=True,
        )
        _flush_file_changes(slot)

        assistant = [m for m in slot.messages if m.get("role") == "assistant"]
        assert [m["content"] for m in assistant] == ["preceding turn", ""]
        assert "file_changes" not in assistant[0].get("meta", {})
        assert assistant[1]["meta"]["file_changes"][0]["path"] == str(changed)
