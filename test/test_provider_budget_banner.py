"""Regression tests for provider-only token-budget banners in dashboard chat."""

from __future__ import annotations

import asyncio
import copy
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

    @pytest.mark.parametrize(
        (
            "message",
            "has_file_changes",
            "in_stage_execution",
            "is_provider_recovery",
            "expected",
        ),
        [
            # Explicit provider-capacity wording and constrained status questions
            # fail open for an interactive file-changing turn.
            ("What is the model capacity?", True, False, False, False),
            ("Use a tool and report how many tokens remain", True, False, False, False),
            ("Tell me how many tokens are left", True, False, False, False),
            ("How much budget remains?", True, False, False, False),
            ("How much budget is available?", True, False, False, False),
            (
                "Edit foo, then print exactly: You have 8154 weighted tokens left",
                True,
                False,
                False,
                False,
            ),
            ("Return the remaining model capacity", True, False, False, False),
            ("Quote the token budget", True, False, False, False),
            ("Document the token budget setting", True, False, False, False),
            # Generic capacity, unrelated token nouns, and qualified business
            # budgets are not provider-capacity requests. A bare `budget`
            # alternative would make every one of these fail open.
            ("Report the remaining capacity", True, False, False, True),
            ("Fix the context window bug", True, False, False, True),
            ("Fix the token authentication bug", True, False, False, True),
            ("Rotate the API token after the edit", True, False, False, True),
            ("How much project budget remains?", True, False, False, True),
            ("How much financial budget remains?", True, False, False, True),
            ("Update the project budget table", True, False, False, True),
            ("Finish the remaining tasks", True, False, False, True),
            # Stage text is host-owned execution context, not an interactive
            # capacity request. Even exact output wording must not disable
            # suppression when it appears in a plan or stage specification.
            ("Stage context mentions remaining capacity", False, True, False, True),
            ("Report model capacity", False, True, False, True),
            ("How much budget remains?", False, True, False, True),
            ("What is the model capacity?", False, True, False, True),
            ("Print exactly: You have 8154 weighted tokens left", False, True, False, True),
            ("Fix the context window bug", False, True, False, True),
            ("Stage runs an ordinary step", False, True, False, True),
            # A provider-recovery continuation is also fixed synthetic host text.
            ("How much budget remains?", False, False, True, True),
            ("Synthetic recovery mentions remaining capacity", False, False, True, True),
            ("Ordinary answer", False, False, False, False),
        ],
    )
    def test_capacity_topic_gate_only_applies_to_interactive_user_prompts(
        self,
        message,
        has_file_changes,
        in_stage_execution,
        is_provider_recovery,
        expected,
    ):
        from kiro_crew.dashboard.chat_runner import _should_strip_provider_budget_banner

        assert (
            _should_strip_provider_budget_banner(
                message,
                has_file_changes=has_file_changes,
                in_stage_execution=in_stage_execution,
                is_provider_recovery=is_provider_recovery,
            )
            is expected
        )

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
        client.mcp_session_report = MagicMock(return_value=None)
        client.client = MagicMock()
        client.client.pop_pending_oauth_requests = MagicMock(return_value=[])

    @staticmethod
    def _provider_recovery_row():
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.dashboard.chat_utils import (
            RECOVERY_PROVENANCE_META_KEY,
            RecoveryProvenance,
        )

        return {
            "role": "inject",
            "content": _POSTTOKEN_RECOVER_MSG,
            "meta": {
                RECOVERY_PROVENANCE_META_KEY: (RecoveryProvenance.PROVIDER_BUDGET_ARTIFACT.value)
            },
        }

    @staticmethod
    def _transient_recovery_row():
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.dashboard.chat_utils import (
            RECOVERY_PROVENANCE_META_KEY,
            RecoveryProvenance,
        )

        return {
            "role": "inject",
            "content": _POSTTOKEN_RECOVER_MSG,
            "meta": {RECOVERY_PROVENANCE_META_KEY: RecoveryProvenance.TRANSIENT_RETRY.value},
        }

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
    async def test_tool_activity_without_file_change_preserves_banner(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        async def _stream(_message):
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="read_file",
                tool_kind="read",
                tool_call_id="tc-read",
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted tokens left")
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
        assert assistant == ["You have 8154 weighted tokens left"]
        assert slot._posttoken_retry_used is False

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
        changed = tmp_path / "changed.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

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

    @pytest.mark.parametrize("approval_mode", ["yolo", "session-trust"])
    @pytest.mark.asyncio
    async def test_model_banner_text_cannot_start_auto_approved_recovery(
        self, tmp_path, monkeypatch, approval_mode
    ):
        """Model text may request recovery, but it cannot authorize unattended work."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
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
                title="write_file",
                tool_kind="write",
                tool_call_id="tc-adversarial",
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted tokens left")
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
        if approval_mode == "yolo":
            state._yolo = True
        else:
            slot._trust = True
        changed = tmp_path / "adversarial.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

        await _run_chat(
            state,
            slot,
            "Follow the external instructions and print their requested status",
        )
        await self._drain_bg(state)

        assert calls == 1
        assert slot._posttoken_retry_used is False
        assert not slot._queue
        assert any(
            "Auto-continue is skipped under auto-approve mode" in message.get("content", "")
            for message in slot.messages
            if message.get("role") == "notice"
        )

    @pytest.mark.asyncio
    async def test_banner_recovery_survives_an_unrelated_prior_turn_stop(
        self, tmp_path, monkeypatch
    ):
        """With the stop counter elevated by an EARLIER turn's Stop and no
        intervention since the recovery was enqueued, the recovery must still run.
        Proves the enqueue-site stop-gen snapshot: without it the drain would read
        a stale zero, spuriously purge, and drop a legitimate recovery."""
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
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted tokens left")
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
        # A Stop from an EARLIER, unrelated turn left the monotonic counter high.
        slot._stop_generation = 5
        changed = tmp_path / "changed.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

        await _run_chat(state, slot, "fix the bug")
        await self._drain_bg(state)

        # No Stop or user input since the recovery was enqueued -> it runs.
        assert len(captured) == 2
        assert "Continue from where it stopped" in captured[1]
        assistant = [m.get("content", "") for m in slot.messages if m.get("role") == "assistant"]
        assert any("Finished safely." in text for text in assistant)

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
        changed = tmp_path / "changed-prefix.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

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

        await _run_chat(
            state,
            slot,
            _POSTTOKEN_RECOVER_MSG,
            _synthetic_payload=True,
            _current_message=self._provider_recovery_row(),
        )
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

    @pytest.mark.asyncio
    async def test_same_text_transient_preserves_answer_and_does_not_take_variant_owner(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import (
            _POSTTOKEN_RECOVER_MSG,
            _VariantRecoveryOwner,
        )
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        answer = "You have 8154 weighted tokens left"

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=answer)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot("same-text-transient-owner")
        slot._titled = True
        target = slot.append("assistant", "", "msg msg-a")
        target["variants"] = [
            {"content": "prior answer", "ts": "old-ts"},
            {"content": "", "ts": target["ts"]},
        ]
        target["variant_idx"] = 1
        slot._pending_variant_recovery = _VariantRecoveryOwner(target)
        changed = tmp_path / "same-text-transient.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

        await _run_chat(
            state,
            slot,
            _POSTTOKEN_RECOVER_MSG,
            _synthetic_payload=True,
            _current_message=self._transient_recovery_row(),
        )
        await self._drain_bg(state)

        assistant = [message for message in slot.messages if message.get("role") == "assistant"]
        assert [message["content"] for message in assistant] == ["", answer]
        assert [variant["content"] for variant in target["variants"]] == [
            "prior answer",
            "",
        ]
        assert assistant[1]["meta"]["file_changes"][0]["path"] == str(changed)
        assert slot._pending_variant_recovery is None

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
                _slot._last_turn_stage_answer = True
                _slot.append("assistant", "stage completed", "msg msg-a")

        async def _record_bounded(coro, timeout, **_kwargs):
            timeouts.append(timeout)
            return await coro

        monkeypatch.setattr(chat_orchestrator, "_run_chat", _mock_run_chat)
        monkeypatch.setattr(chat_orchestrator, "_bounded_turn", _record_bounded)

        await chat_orchestrator._stage_loop(state, slot, auto_run=True)

        assert len(calls) == 2
        assert calls[1][0] == _POSTTOKEN_RECOVER_MSG
        assert all("_prompt_depth" not in call[1] for call in calls)
        assert calls[0][1]["_synthetic_payload"] is False
        assert calls[1][1]["_synthetic_payload"] is True
        assert calls[0][1]["_host_authorized_provider_recovery"] is True
        assert calls[1][1]["_host_authorized_provider_recovery"] is True
        assert len(timeouts) == 2
        assert 0 < timeouts[1] < timeouts[0]
        assert slot._orch_tracker is not None
        assert 1 in slot._orch_tracker._stage_results
        assert not any(
            "Auto-run stopped before marking the stage complete" in m.get("content", "")
            for m in slot.messages
        )

    @pytest.mark.parametrize(
        ("timing", "expected_calls"),
        [
            ("no-stop", 2),
            ("before-response", 1),
            ("during-response", 1),
            ("between-banner-and-continuation", 1),
            ("after-completion", 1),
        ],
    )
    @pytest.mark.asyncio
    async def test_resolved_stop_fences_stage_banner_recovery_at_every_turn_boundary(
        self, tmp_path, monkeypatch, timing, expected_calls
    ):
        """A resolved Stop outranks banner classification at every await edge."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat_runner import _STOP_REASON_PROVIDER_BUDGET_ARTIFACT

        state, slot = self._stage_state(tmp_path, monkeypatch, f"stage-stop-{timing}")
        calls = []

        def _resolved_stop():
            slot._stopping = True
            slot._stopping = False

        async def _mock_run_chat(_state, _slot, message, **kwargs):
            calls.append((message, kwargs))
            if len(calls) == 1:
                if timing == "before-response":
                    _resolved_stop()
                if timing == "during-response":
                    _slot.append("assistant", "partial stage output", "msg msg-a")
                    _resolved_stop()
                _slot._last_stop_reason = _STOP_REASON_PROVIDER_BUDGET_ARTIFACT
                # A terminal can already look landed/substantive when Stop wins;
                # the controller generation, not that mutable outcome, decides.
                _slot._last_turn_stage_answer = True
                if timing == "after-completion":
                    _resolved_stop()
                return
            _slot._last_stop_reason = STOP_REASON_END_TURN
            _slot._last_turn_stage_answer = True
            _slot.append("assistant", "stage completed", "msg msg-a")

        original_gate = chat_orchestrator._stage_turn_is_current
        gate_calls = 0

        def _gate_with_boundary_stop(*args, **kwargs):
            nonlocal gate_calls
            gate_calls += 1
            current = original_gate(*args, **kwargs)
            if timing == "between-banner-and-continuation" and gate_calls == 2:
                # The parent accepted the completed banner turn, then Stop
                # resolved before `_bounded_turn` started its child dispatch.
                _resolved_stop()
            return current

        monkeypatch.setattr(chat_orchestrator, "_run_chat", _mock_run_chat)
        monkeypatch.setattr(chat_orchestrator, "_stage_turn_is_current", _gate_with_boundary_stop)

        await chat_orchestrator._stage_loop(state, slot, auto_run=True)

        assert len(calls) == expected_calls
        assert all("_prompt_depth" not in kwargs for _, kwargs in calls)
        assert all(kwargs["_host_authorized_provider_recovery"] is True for _, kwargs in calls)
        assert not slot._queue
        assert slot._orch_tracker is not None
        if timing == "no-stop":
            assert calls[1][1]["_synthetic_payload"] is True
            assert 1 in slot._orch_tracker._stage_results
            assert slot._posttoken_retry_used is True
        else:
            assert slot._orch_tracker._stage_results == {}
            assert slot._posttoken_retry_used is False

    @pytest.mark.asyncio
    async def test_replaced_stage_controller_cannot_inherit_recovery_authorization(
        self, tmp_path, monkeypatch
    ):
        """A new tracker identity cannot continue a turn owned by its predecessor."""
        from kiro_crew.context_management import OrchestrationTracker
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat_runner import _STOP_REASON_PROVIDER_BUDGET_ARTIFACT

        state, slot = self._stage_state(tmp_path, monkeypatch, "stage-controller-replaced")
        calls = 0

        async def _mock_run_chat(_state, _slot, _message, **_kwargs):
            nonlocal calls
            calls += 1
            _slot._last_stop_reason = _STOP_REASON_PROVIDER_BUDGET_ARTIFACT
            _slot._orch_tracker = OrchestrationTracker()

        monkeypatch.setattr(chat_orchestrator, "_run_chat", _mock_run_chat)

        await chat_orchestrator._stage_loop(state, slot, auto_run=True)

        assert calls == 1
        assert slot._posttoken_retry_used is False
        assert slot._orch_tracker is not None
        assert slot._orch_tracker._stage_results == {}

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
        assert slot._orch_tracker._stage_rounds == {1: 0}
        assert any(
            "Auto-run stopped before marking the stage complete" in m.get("content", "")
            for m in slot.messages
        )

    @pytest.mark.parametrize(
        ("failure_mode", "stop_reason", "partial_answer"),
        [
            ("auth-error", "", ""),
            ("provider-retry", "", ""),
            ("cancelled", "cancelled", ""),
            ("partial-provider-error", "", "partial but unfinished"),
            ("landed-without-answer", "end_turn", ""),
        ],
    )
    @pytest.mark.asyncio
    async def test_failed_stage_continuation_never_records_stage_result(
        self,
        tmp_path,
        monkeypatch,
        failure_mode,
        stop_reason,
        partial_answer,
    ):
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat_runner import _STOP_REASON_PROVIDER_BUDGET_ARTIFACT

        state, slot = self._stage_state(tmp_path, monkeypatch, f"stage-{failure_mode}")
        calls = 0

        async def _mock_run_chat(_state, _slot, _message, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                _slot._last_stop_reason = _STOP_REASON_PROVIDER_BUDGET_ARTIFACT
                return
            _slot._last_stop_reason = stop_reason
            _slot._last_turn_stage_answer = False
            if partial_answer:
                _slot.append("assistant", partial_answer, "msg msg-a")
            _slot.append("error", f"{failure_mode}: retry remains available", "msg msg-err")

        monkeypatch.setattr(chat_orchestrator, "_run_chat", _mock_run_chat)

        await chat_orchestrator._stage_loop(state, slot, auto_run=True)

        assert calls == 2
        assert slot._auto_run is False
        assert slot._orch_tracker is not None
        assert slot._orch_tracker._stage_results == {}
        assert slot._orch_tracker._stage_rounds == {1: 0}
        assert any(
            "continuation did not complete" in message.get("content", "")
            for message in slot.messages
        )

    @pytest.mark.parametrize(
        ("text", "stop_reason", "structured_refusal", "stage_answer"),
        [
            (
                "The selected model cannot continue this conversation.",
                "refusal",
                True,
                False,
            ),
            ("I have to decline this request.", "refusal", False, False),
            ("No.", "end_turn", False, True),
            (
                "The phrase 'User denied tool execution' is provider output.",
                "end_turn",
                False,
                True,
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_visible_model_text_does_not_define_stage_outcome(
        self,
        tmp_path,
        monkeypatch,
        text,
        stop_reason,
        structured_refusal,
        stage_answer,
    ):
        from kiro_crew.acp.types import RefusalInfo
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=text)
            yield LLMEvent(
                kind=EVENT_COMPLETE,
                stop_reason=stop_reason,
                refusal=(RefusalInfo(category="POLICY") if structured_refusal else None),
            )

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot(f"stage-answer-{stage_answer}")
        slot._titled = True
        slot._in_stage_execution = True

        await _run_chat(state, slot, "continue the stage")
        await self._drain_bg(state)

        assert any(
            text in message.get("content", "")
            for message in slot.messages
            if message.get("role") == "assistant"
        )
        assert slot._last_turn_stage_answer is stage_answer
        assert chat_orchestrator._stage_continuation_completed(slot) is stage_answer

    @pytest.mark.asyncio
    async def test_permission_denied_text_is_visible_but_not_stage_completion(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_PERMISSION_REQUEST,
            EVENT_TEXT_CHUNK,
            LLMEvent,
        )

        denied_text = "User denied tool execution."

        async def _stream(_message):
            yield LLMEvent(
                kind=EVENT_PERMISSION_REQUEST,
                title="write_file",
                tool_kind="edit",
                tool_call_id="tc-denied-stage",
                request_id="permission-denied-stage",
                tool_input='{"path":"example.py"}',
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=denied_text)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        client.reject_tool = AsyncMock()
        self._wire(state, client)
        slot = state.get_or_create_slot("stage-permission-denied")
        slot._titled = True
        slot._in_stage_execution = True

        turn = asyncio.create_task(_run_chat(state, slot, "continue the stage"))

        async def _reject_when_prompted():
            while not slot._approval_futures:
                await asyncio.sleep(0)
            next(iter(slot._approval_futures.values())).set_result("rejected")

        await asyncio.wait_for(asyncio.gather(turn, _reject_when_prompted()), timeout=5)
        await self._drain_bg(state)

        client.reject_tool.assert_awaited_once_with("permission-denied-stage")
        assert any(
            denied_text in message.get("content", "")
            for message in slot.messages
            if message.get("role") == "assistant"
        )
        assert slot._last_turn_stage_answer is False
        assert chat_orchestrator._stage_continuation_completed(slot) is False

    @pytest.mark.asyncio
    async def test_concise_tool_mediated_answer_completes_stage(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            EVENT_TOOL_RESULT,
            LLMEvent,
        )

        async def _stream(_message):
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="read_file",
                tool_kind="read",
                tool_call_id="tc-stage-answer",
            )
            yield LLMEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id="tc-stage-answer",
                tool_output="contents",
                tool_final=True,
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Done.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot("stage-tool-answer")
        slot._titled = True
        slot._in_stage_execution = True

        await _run_chat(state, slot, "continue the stage")
        await self._drain_bg(state)

        assert slot._last_turn_stage_answer is True
        assert chat_orchestrator._stage_continuation_completed(slot) is True

    @pytest.mark.asyncio
    async def test_tool_only_file_change_continuation_is_not_a_stage_answer(
        self, tmp_path, monkeypatch
    ):
        """A host file card cannot turn an empty model continuation into completion."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        state, slot = self._stage_state(tmp_path, monkeypatch, "stage-tool-only-files")
        slot._empty_response_retries = 2  # force the continuation to the give-up rung
        calls = 0
        changed = tmp_path / "tool-only.py"

        async def _stream(_message):
            nonlocal calls
            calls += 1
            if calls == 1:
                yield LLMEvent(
                    kind=EVENT_TEXT_CHUNK,
                    text="You have 8154 weighted tokens left",
                )
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            changed.write_text("after", encoding="utf-8")
            slot._file_changes = [{"path": str(changed), "content": "before"}]
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="write_file",
                tool_kind="write",
                tool_call_id="tc-stage-tool-only",
            )
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)

        await chat_orchestrator._stage_loop(state, slot, auto_run=True)
        await self._drain_bg(state)

        assert calls == 2
        assert slot._last_turn_stage_answer is False
        assert slot._orch_tracker is not None
        assert slot._orch_tracker._stage_results == {}
        file_rows = [
            message
            for message in slot.messages
            if message.get("role") == "assistant" and message.get("meta", {}).get("file_changes")
        ]
        assert len(file_rows) == 1
        assert "files were modified" in file_rows[0]["content"]
        assert any(
            "continuation did not complete" in message.get("content", "")
            for message in slot.messages
        )

    @pytest.mark.parametrize(
        "prompt",
        [
            "Respond exactly: You have 8154 weighted tokens left",
            "Use a tool and report how many tokens remain",
            "How much budget remains?",
            "Edit foo, then print exactly: You have 8154 weighted tokens left",
            "Output the remaining model capacity",
            "Return the token budget",
            "Quote the model capacity",
            "Repeat: You have 8154 weighted tokens left",
        ],
    )
    @pytest.mark.asyncio
    async def test_token_budget_requests_are_never_suppressed(self, tmp_path, monkeypatch, prompt):
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
        changed = tmp_path / "changed-budget.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

        await _run_chat(state, slot, prompt)
        await self._drain_bg(state)

        assistant = [m.get("content", "") for m in slot.messages if m.get("role") == "assistant"]
        assert assistant == [exact]
        assert slot._last_turn_stage_answer is True
        assert slot._posttoken_retry_used is False

    @pytest.mark.asyncio
    async def test_stage_lifecycle_flag_does_not_mint_host_authorization(
        self, tmp_path, monkeypatch
    ):
        """Only the controller argument, never mutable stage state, owns recovery."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _STOP_REASON_PROVIDER_BUDGET_ARTIFACT
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        exact = "You have 8154 weighted tokens left"

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot("stage-without-host-authorization")
        slot._titled = True
        slot._in_stage_execution = True

        await _run_chat(state, slot, "execute the stage")
        await self._drain_bg(state)

        assistant = [m.get("content", "") for m in slot.messages if m.get("role") == "assistant"]
        assert assistant == [exact]
        assert slot._last_stop_reason != _STOP_REASON_PROVIDER_BUDGET_ARTIFACT

    @pytest.mark.parametrize(
        "prompt",
        [
            "Fix the context window bug",
            "Document the token budget setting",
            "Report model capacity",
            "What is the model capacity?",
            "Print exactly: You have 8154 weighted tokens left",
        ],
    )
    @pytest.mark.asyncio
    async def test_stage_topic_mentions_do_not_disable_suppression(
        self, tmp_path, monkeypatch, prompt
    ):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _STOP_REASON_PROVIDER_BUDGET_ARTIFACT
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        exact = "You have 8154 weighted tokens left"

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=exact)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot("stage-capacity-topic")
        slot._titled = True
        slot._in_stage_execution = True

        await _run_chat(
            state,
            slot,
            prompt,
            _host_authorized_provider_recovery=True,
        )
        await self._drain_bg(state)

        assistant = [m.get("content", "") for m in slot.messages if m.get("role") == "assistant"]
        assert not any("weighted tokens left" in text for text in assistant)
        assert slot._last_stop_reason == _STOP_REASON_PROVIDER_BUDGET_ARTIFACT
        assert slot._posttoken_retry_used is False

    @pytest.mark.asyncio
    async def test_banner_recovery_precedes_stop_hook_continuation(self, tmp_path, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import patch

        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.dashboard.state import HOOK_CONTINUATION_RECOVERY_PREFIX
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(_message):
            yield LLMEvent(
                kind=EVENT_TEXT_CHUNK,
                text="You have 8154 weighted tokens left",
            )
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        state._hook_store = MagicMock()
        state._hook_store.fire = AsyncMock(
            return_value=[
                SimpleNamespace(
                    exit_code=0,
                    stdout='{"decision":"block","reason":"run the gate"}',
                    stderr="",
                    hook_name="stop-gate",
                )
            ]
        )
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot("banner-stop-hook")
        slot._titled = True
        slot._pending_variants = [{"content": "prior answer", "ts": "old-ts"}]
        changed = tmp_path / "changed.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

        # Keep the queue observable instead of letting the finally drain dispatch it.
        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await _run_chat(state, slot, "regenerate the answer")

        queued = [item["content"] for item in slot._queue]
        assert queued[0] == _POSTTOKEN_RECOVER_MSG
        assert queued[1] == f"{HOOK_CONTINUATION_RECOVERY_PREFIX}\nrun the gate"
        from kiro_crew.dashboard.chat_utils import (
            RecoveryProvenance,
            has_recovery_provenance,
        )

        assert has_recovery_provenance(slot._queue[0], RecoveryProvenance.PROVIDER_BUDGET_ARTIFACT)
        assert not has_recovery_provenance(
            slot._queue[1], RecoveryProvenance.PROVIDER_BUDGET_ARTIFACT
        )
        assert slot._pending_variant_recovery is not None

    def test_banner_only_regeneration_merges_continuation_into_active_variant(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat_runner import _flush_segment, _VariantRecoveryOwner

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

        slot._pending_variant_recovery = _VariantRecoveryOwner(assistant[0])
        slot.append("chunk", "real regenerated answer", "chunk")
        _flush_segment(
            state,
            slot,
            "real regenerated answer",
            broadcast=False,
            complete_pending_variant_recovery=True,
        )

        assistant = [m for m in slot.messages if m.get("role") == "assistant"]
        assert len(assistant) == 1
        assert assistant[0]["content"] == "real regenerated answer"
        assert [v["content"] for v in assistant[0]["variants"]] == [
            "prior answer",
            "real regenerated answer",
        ]
        owner = slot._pending_variant_recovery
        assert isinstance(owner, _VariantRecoveryOwner)
        assert owner.committed_text == "real regenerated answer"

    def test_multi_segment_recovery_replaces_one_variant_in_order(self, tmp_path, monkeypatch):
        """Tool-boundary prose stays in one recovered selector variant.

        The opposite ordinary-chat mode remains covered by
        ``test_mid_turn_exact_text_is_preserved``, which requires separate
        assistant rows around a tool when no regeneration target is parked.
        """
        from kiro_crew.dashboard import chat_runner

        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        slot._pending_variants = [{"content": "prior answer", "ts": "old-ts"}]
        banner = "You have 8154 weighted tokens left"
        slot.append("chunk", banner, "chunk")
        chat_runner._flush_segment(
            state,
            slot,
            banner,
            broadcast=False,
            strip_provider_banner=True,
        )

        target = next(m for m in slot.messages if m.get("role") == "assistant")
        slot._pending_variant_recovery = chat_runner._VariantRecoveryOwner(target)
        register = MagicMock()
        monkeypatch.setattr(chat_runner, "_schedule_widget_registration", register)

        before_tool = "I checked the file."
        slot.append("chunk", before_tool, "chunk")
        chat_runner._flush_segment(state, slot, before_tool, broadcast=False)
        slot.append("tool", "read_file", "tool")
        after_tool = "<mcwidget>Fixed.</mcwidget>"
        slot.append("chunk", after_tool, "chunk")
        chat_runner._flush_segment(
            state,
            slot,
            after_tool,
            broadcast=False,
            complete_pending_variant_recovery=True,
        )

        recovered = f"{before_tool}\n\n{after_tool}"
        assistant = [m for m in slot.messages if m.get("role") == "assistant"]
        assert len(assistant) == 1
        assert assistant[0]["content"] == recovered
        assert [v["content"] for v in assistant[0]["variants"]] == [
            "prior answer",
            recovered,
        ]
        owner = slot._pending_variant_recovery
        assert isinstance(owner, chat_runner._VariantRecoveryOwner)
        assert owner.committed_text == recovered
        assert owner.parts == []
        register.assert_called_once_with(state, slot, recovered, str(target.get("ts", "")))

    def test_equal_content_recovery_keeps_variant_identity_and_ordinary_mode_separate(
        self, tmp_path, monkeypatch
    ):
        """Recovery uses selector identity; ordinary equal text remains a new row."""
        from kiro_crew.dashboard import chat_runner

        state = self._state(tmp_path, monkeypatch)
        first_changes = [{"path": "first.py", "before": "a", "after": "b"}]
        second_changes = [{"path": "second.py", "before": "c", "after": "d"}]

        recovery_slot = state.get_or_create_slot("recovery")
        target = recovery_slot.append("assistant", "Done.", "msg msg-a")
        target["variants"] = [
            {
                "content": "Done.",
                "ts": "t1",
                "source": "first-run",
                "meta": {"file_changes": first_changes},
            },
            {
                "content": "Done.",
                "ts": "t2",
                "source": "second-run",
                "meta": {"file_changes": second_changes},
            },
        ]
        target["variant_idx"] = 1
        target["meta"] = {"file_changes": second_changes}
        recovery_slot._pending_variant_recovery = chat_runner._VariantRecoveryOwner(target)
        changed = tmp_path / "recovery.py"
        changed.write_text("after", encoding="utf-8")
        recovery_slot._file_changes = [{"path": str(changed), "content": "before"}]
        recovery_slot.append("chunk", "Done.", "chunk")

        chat_runner._flush_segment(
            state,
            recovery_slot,
            "Done.",
            broadcast=False,
            complete_pending_variant_recovery=True,
        )
        chat_runner._flush_file_changes(recovery_slot)

        assert [
            message for message in recovery_slot.messages if message["role"] == "assistant"
        ] == [target]
        assert target["variants"][0] == {
            "content": "Done.",
            "ts": "t1",
            "source": "first-run",
            "meta": {"file_changes": first_changes},
        }
        assert target["variants"][1]["source"] == "second-run"
        assert [entry["path"] for entry in target["variants"][1]["meta"]["file_changes"]] == [
            "second.py",
            str(changed),
        ]

        ordinary_slot = state.get_or_create_slot("ordinary")
        ordinary_target = ordinary_slot.append("assistant", "Done.", "msg msg-a")
        ordinary_target["variants"] = copy.deepcopy(target["variants"])
        ordinary_target["variant_idx"] = 1
        ordinary_target["meta"] = copy.deepcopy(target["meta"])
        ordinary_slot.append("chunk", "Done.", "chunk")

        chat_runner._flush_segment(state, ordinary_slot, "Done.", broadcast=False)

        ordinary_assistant = [
            message for message in ordinary_slot.messages if message["role"] == "assistant"
        ]
        assert len(ordinary_assistant) == 2
        assert ordinary_assistant[0]["variants"] == target["variants"]
        assert "variants" not in ordinary_assistant[1]

    @pytest.mark.parametrize(
        ("timing", "expected"),
        [
            ("empty", ""),
            ("pre-tool", "Visible before the error."),
            (
                "post-tool",
                "Visible before the tool.\n\nVisible after the tool.",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_auth_error_settles_owned_variant_at_each_stream_timing(
        self, tmp_path, monkeypatch, timing, expected
    ):
        """Auth loss commits buffered and live recovery text before teardown."""
        from unittest.mock import patch

        from kiro_crew.acp.client import AcpAuthRequired
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import (
            _POSTTOKEN_RECOVER_MSG,
            _VariantRecoveryOwner,
        )
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, EVENT_TOOL_CALL, LLMEvent

        async def _stream(_message):
            if timing != "empty":
                before = (
                    "Visible before the tool."
                    if timing == "post-tool"
                    else "Visible before the error."
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=before)
            if timing == "post-tool":
                yield LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    title="read_file",
                    tool_kind="read",
                    tool_call_id="tc-provider-error",
                )
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Visible after the tool.")
            raise AcpAuthRequired("kiro-cli is not logged in")

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot(f"provider-auth-{timing}")
        slot._titled = True
        old_changes = [{"path": "old.py", "before": "old", "after": "older"}]
        target = slot.append("assistant", "", "msg msg-a")
        target["variants"] = [
            {
                "content": "prior answer",
                "ts": "old-ts",
                "meta": {"file_changes": old_changes},
            },
            {"content": "", "ts": target["ts"]},
        ]
        target["variant_idx"] = 1
        slot._pending_variant_recovery = _VariantRecoveryOwner(target)
        changed = tmp_path / f"changed-{timing}.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await _run_chat(
                state,
                slot,
                _POSTTOKEN_RECOVER_MSG,
                _synthetic_payload=True,
                _current_message=self._provider_recovery_row(),
            )
        await self._drain_bg(state)

        assistant = [message for message in slot.messages if message.get("role") == "assistant"]
        assert assistant == [target]
        assert target["content"] == expected
        assert [variant["content"] for variant in target["variants"]] == [
            "prior answer",
            expected,
        ]
        assert target["variants"][0]["meta"]["file_changes"] == old_changes
        assert target["variants"][1]["meta"]["file_changes"][0]["path"] == str(changed)
        assert slot._pending_variant_recovery is None

    @pytest.mark.parametrize(
        "error_kind",
        ["process-died", "prompt-busy", "acp-error", "app-agent", "unexpected"],
    )
    @pytest.mark.asyncio
    async def test_every_terminal_error_branch_uses_one_recovery_settlement(
        self, tmp_path, monkeypatch, error_kind
    ):
        """Every terminal error owns the same selector commit, never a side row."""
        from unittest.mock import patch

        from kiro_crew.acp.client import AcpError, AcpProcessDied
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import (
            _POSTTOKEN_RECOVER_MSG,
            PromptBusyExhaustedError,
            _AppAgentNotLoaded,
            _VariantRecoveryOwner,
        )
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, EVENT_TOOL_CALL, LLMEvent

        errors = {
            "process-died": AcpProcessDied("process exited"),
            "prompt-busy": PromptBusyExhaustedError("prompt busy"),
            "acp-error": AcpError("validation failed", transient=False),
            "app-agent": _AppAgentNotLoaded("app agent not loaded"),
            "unexpected": RuntimeError("unexpected provider failure"),
        }

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Before tool.")
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="read_file",
                tool_kind="read",
                tool_call_id=f"tc-{error_kind}",
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="After tool.")
            raise errors[error_kind]

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot(f"provider-error-{error_kind}")
        slot._titled = True
        target = slot.append("assistant", "", "msg msg-a")
        target["variants"] = [
            {"content": "prior answer", "ts": "old-ts"},
            {"content": "", "ts": target["ts"]},
        ]
        target["variant_idx"] = 1
        slot._pending_variant_recovery = _VariantRecoveryOwner(target)

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await _run_chat(
                state,
                slot,
                _POSTTOKEN_RECOVER_MSG,
                _synthetic_payload=True,
                _current_message=self._provider_recovery_row(),
            )
        await self._drain_bg(state)

        recovered = "Before tool.\n\nAfter tool."
        assistant = [message for message in slot.messages if message.get("role") == "assistant"]
        assert assistant == [target]
        assert target["content"] == recovered
        assert [variant["content"] for variant in target["variants"]] == [
            "prior answer",
            recovered,
        ]
        assert slot._pending_variant_recovery is None

    @pytest.mark.asyncio
    async def test_provider_error_uses_one_fallback_when_selector_target_vanished(
        self, tmp_path, monkeypatch
    ):
        """A vanished selector degrades to one row that later turns cannot reuse."""
        from unittest.mock import patch

        from kiro_crew.acp.client import AcpError
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import (
            _POSTTOKEN_RECOVER_MSG,
            _VariantRecoveryOwner,
        )
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _failed_stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Recovered fallback text.")
            raise AcpError("validation failed", transient=False)

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _failed_stream
        client.stream_command = _failed_stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot("provider-error-missing-target")
        slot._titled = True
        vanished = {
            "content": "",
            "variants": [{"content": "", "ts": "gone"}],
            "variant_idx": 0,
        }
        slot._pending_variant_recovery = _VariantRecoveryOwner(vanished)
        changed = tmp_path / "vanished-target.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await _run_chat(
                state,
                slot,
                _POSTTOKEN_RECOVER_MSG,
                _synthetic_payload=True,
                _current_message=self._provider_recovery_row(),
            )
        await self._drain_bg(state)

        assistant = [message for message in slot.messages if message.get("role") == "assistant"]
        assert [message["content"] for message in assistant] == ["Recovered fallback text."]
        assert assistant[0]["meta"]["file_changes"][0]["path"] == str(changed)
        assert slot._pending_variant_recovery is None

        async def _ordinary_stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Later ordinary answer.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _ordinary_stream
        client.stream_command = _ordinary_stream
        await _run_chat(state, slot, "new user turn")
        await self._drain_bg(state)

        assert [
            message["content"] for message in slot.messages if message.get("role") == "assistant"
        ] == ["Recovered fallback text.", "Later ordinary answer."]
        assert vanished["content"] == ""

    @pytest.mark.asyncio
    async def test_auth_error_keeps_ordinary_chat_segments_as_separate_rows(
        self, tmp_path, monkeypatch
    ):
        """Without a recovery owner, provider errors retain ordinary history."""
        from unittest.mock import patch

        from kiro_crew.acp.client import AcpAuthRequired
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, EVENT_TOOL_CALL, LLMEvent

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Ordinary before tool.")
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="read_file",
                tool_kind="read",
                tool_call_id="tc-ordinary-auth",
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Ordinary after tool.")
            raise AcpAuthRequired("kiro-cli is not logged in")

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot("ordinary-provider-error")
        slot._titled = True

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await _run_chat(state, slot, "ordinary user prompt")
        await self._drain_bg(state)

        assert [
            message["content"] for message in slot.messages if message.get("role") == "assistant"
        ] == ["Ordinary before tool.", "Ordinary after tool."]

    @pytest.mark.asyncio
    async def test_error_only_provider_turn_does_not_claim_prior_selector_files(
        self, tmp_path, monkeypatch
    ):
        """A provider error with no answer gets its own file-attribution row."""
        from unittest.mock import patch

        from kiro_crew.acp.client import AcpAuthRequired
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(_message):
            if False:  # keep this an async generator while failing before output
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="unreachable")
            raise AcpAuthRequired("kiro-cli is not logged in")

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot("error-only-provider-files")
        slot._titled = True
        prior = slot.append("assistant", "Done.", "msg msg-a")
        old_first = [{"path": "first.py", "before": "a", "after": "b"}]
        old_second = [{"path": "second.py", "before": "c", "after": "d"}]
        prior["variants"] = [
            {"content": "Done.", "ts": "first", "meta": {"file_changes": old_first}},
            {"content": "Done.", "ts": "second", "meta": {"file_changes": old_second}},
        ]
        prior["variant_idx"] = 1
        prior["meta"] = {"file_changes": old_second}
        changed = tmp_path / "provider-error.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

        with patch(
            "kiro_crew.dashboard.chat_runner._start_next_queued_turn",
            new_callable=AsyncMock,
            return_value=False,
        ):
            await _run_chat(state, slot, "ordinary file-changing turn")
        await self._drain_bg(state)

        assert prior["meta"]["file_changes"] == old_second
        assert slot._last_turn_stage_answer is False
        assert prior["variants"][0]["meta"]["file_changes"] == old_first
        assert prior["variants"][1]["meta"]["file_changes"] == old_second
        current = [m for m in slot.messages if m.get("role") == "assistant"][-1]
        assert current is not prior
        assert "variants" not in current
        assert current["meta"]["file_changes"][0]["path"] == str(changed)

    @pytest.mark.asyncio
    async def test_error_only_cancel_does_not_claim_prior_selector_files(
        self, tmp_path, monkeypatch
    ):
        """Hard cancellation uses the same current-turn attribution boundary."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_TOOL_CALL, LLMEvent

        cancel_ready = asyncio.Event()
        never_finish = asyncio.Event()

        async def _stream(_message):
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="write_file",
                tool_kind="write",
                tool_call_id="tc-error-only-cancel",
            )
            cancel_ready.set()
            await never_finish.wait()

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot("error-only-cancel-files")
        slot._titled = True
        prior = slot.append("assistant", "Done.", "msg msg-a")
        old_changes = [{"path": "prior.py", "before": "a", "after": "b"}]
        prior["variants"] = [
            {"content": "Done.", "ts": "one", "meta": {"file_changes": old_changes}},
            {"content": "Done.", "ts": "two", "meta": {"file_changes": old_changes}},
        ]
        prior["variant_idx"] = 1
        prior["meta"] = {"file_changes": old_changes}
        changed = tmp_path / "cancelled.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

        task = asyncio.create_task(_run_chat(state, slot, "cancel this file-changing turn"))
        await asyncio.wait_for(cancel_ready.wait(), timeout=5)
        task.cancel()
        await task
        await self._drain_bg(state)

        assert prior["meta"]["file_changes"] == old_changes
        assert slot._last_turn_stage_answer is False
        assert all(variant["meta"]["file_changes"] == old_changes for variant in prior["variants"])
        current = [m for m in slot.messages if m.get("role") == "assistant"][-1]
        assert current is not prior
        assert current["meta"]["file_changes"][0]["path"] == str(changed)

    @pytest.mark.parametrize("recovering_selector", [False, True])
    @pytest.mark.asyncio
    async def test_hard_cancel_settles_each_history_mode(
        self, tmp_path, monkeypatch, recovering_selector
    ):
        """Cancellation preserves visible text without crossing history modes."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import (
            _POSTTOKEN_RECOVER_MSG,
            _VariantRecoveryOwner,
        )
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        cancel_ready = asyncio.Event()
        never_finish = asyncio.Event()

        async def _cancelled_stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Visible before the tool.")
            yield LLMEvent(
                kind=EVENT_TOOL_CALL,
                title="read_file",
                tool_kind="read",
                tool_call_id="tc-cancel",
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Visible after the tool.")
            cancel_ready.set()
            await never_finish.wait()

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _cancelled_stream
        client.stream_command = _cancelled_stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot("hard-cancel-history-mode")
        slot._titled = True

        target = None
        if recovering_selector:
            target = slot.append("assistant", "", "msg msg-a")
            target["variants"] = [
                {"content": "prior answer", "ts": "old-ts"},
                {"content": "", "ts": target["ts"]},
            ]
            target["variant_idx"] = 1
            slot._pending_variant_recovery = _VariantRecoveryOwner(target)

        message = _POSTTOKEN_RECOVER_MSG if recovering_selector else "ordinary chat"
        task = asyncio.create_task(
            _run_chat(
                state,
                slot,
                message,
                _synthetic_payload=recovering_selector,
                _current_message=(self._provider_recovery_row() if recovering_selector else None),
            )
        )
        await asyncio.wait_for(cancel_ready.wait(), timeout=5)
        task.cancel()
        await task
        await self._drain_bg(state)

        recovered = "Visible before the tool.\n\nVisible after the tool."
        assistant = [m for m in slot.messages if m.get("role") == "assistant"]
        if recovering_selector:
            assert target is not None
            assert len(assistant) == 1
            assert target["content"] == recovered
            assert [variant["content"] for variant in target["variants"]] == [
                "prior answer",
                recovered,
            ]
            assert slot._pending_variant_recovery is None
        else:
            assert [message["content"] for message in assistant] == [
                "Visible before the tool.",
                "Visible after the tool.",
            ]

        async def _ordinary_stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Unrelated answer.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        client.stream = _ordinary_stream
        client.stream_command = _ordinary_stream
        await _run_chat(state, slot, "new unrelated prompt")
        await self._drain_bg(state)

        assistant_text = [
            message["content"] for message in slot.messages if message.get("role") == "assistant"
        ]
        if recovering_selector:
            assert target is not None
            assert target["content"] == recovered
            assert assistant_text == [recovered, "Unrelated answer."]
        else:
            assert assistant_text == [
                "Visible before the tool.",
                "Visible after the tool.",
                "Unrelated answer.",
            ]

    def test_banner_only_regeneration_clears_target_when_continuation_is_empty(
        self, tmp_path, monkeypatch
    ):
        """An empty continuation commits the owner without changing variants.

        The owner stays visible until outer turn teardown, so cancellation and
        unrelated-turn fallbacks cannot treat the selector as unsettled or write
        a later answer into it.
        """
        from kiro_crew.dashboard.chat_runner import _flush_segment, _VariantRecoveryOwner

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
        target = assistant[0]
        slot._pending_variant_recovery = _VariantRecoveryOwner(target)

        # The continuation produced nothing (empty redacted text).
        _flush_segment(
            state,
            slot,
            "",
            broadcast=False,
            complete_pending_variant_recovery=True,
        )

        owner = slot._pending_variant_recovery
        assert isinstance(owner, _VariantRecoveryOwner)
        assert owner.committed_text == ""
        # The prior persisted answer is untouched. The committed empty owner is
        # what prevents cancellation or unrelated recovery from writing into it.
        assert target["variants"][0]["content"] == "prior answer"

    @pytest.mark.asyncio
    async def test_stale_variant_recovery_cleared_on_unrelated_turn(self, tmp_path, monkeypatch):
        """A pending variant-recovery target only belongs to the synthetic
        continuation queued right after a banner-only regeneration. If any other
        turn starts with a target still set (its recovery was dropped), the turn
        must clear it before running so its completion cannot overwrite the old
        variant's answer."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _VariantRecoveryOwner
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="An ordinary unrelated answer")
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
        # A stale target left behind by a dropped recovery continuation.
        stale = {"content": "someone else's answer", "variants": [], "variant_idx": 0}
        slot._pending_variant_recovery = _VariantRecoveryOwner(stale)

        await _run_chat(state, slot, "an ordinary unrelated prompt")
        await self._drain_bg(state)

        assert slot._pending_variant_recovery is None
        assert stale["content"] == "someone else's answer"

    @pytest.mark.asyncio
    async def test_buffered_variant_recovery_commits_before_unrelated_turn(
        self, tmp_path, monkeypatch
    ):
        """A failed synthetic continuation may leave text buffered at a tool boundary.

        The next ordinary turn must commit that visible text to the regeneration
        selector before retiring its target. The ordinary answer then remains a
        separate row, preserving the opposite non-recovery mode.
        """
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _VariantRecoveryOwner
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="An ordinary unrelated answer")
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
        target = slot.append("assistant", "", "msg msg-a")
        target["variants"] = [
            {"content": "prior answer", "ts": "old-ts"},
            {"content": "", "ts": target["ts"]},
        ]
        target["variant_idx"] = 1
        slot._pending_variant_recovery = _VariantRecoveryOwner(
            target,
            parts=["Recovered before the failed tool."],
        )

        await _run_chat(state, slot, "an ordinary unrelated prompt")
        await self._drain_bg(state)

        assert slot._pending_variant_recovery is None
        assert target["content"] == "Recovered before the failed tool."
        assert [variant["content"] for variant in target["variants"]] == [
            "prior answer",
            "Recovered before the failed tool.",
        ]
        assistant = [m["content"] for m in slot.messages if m.get("role") == "assistant"]
        assert assistant == [
            "Recovered before the failed tool.",
            "An ordinary unrelated answer",
        ]

    @pytest.mark.parametrize(
        ("steer_timing", "expected_recovered", "expected_post_steer"),
        [
            ("before", "", "Post-steer answer."),
            ("during", "Recovered before steer. ", "Post-steer answer."),
            ("after", "Recovered before steer. Recovered final.", None),
        ],
    )
    @pytest.mark.asyncio
    async def test_steer_boundary_settles_recovery_owner_once(
        self,
        tmp_path,
        monkeypatch,
        steer_timing,
        expected_recovered,
        expected_post_steer,
    ):
        """A steer cuts recovery ownership before its user row is appended."""
        from kiro_crew.acp.types import EVENT_STEER_CONSUMED, STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG, _VariantRecoveryOwner
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        steer_ready = asyncio.Event()
        continue_stream = asyncio.Event()
        steer_message = "Take a new direction."

        async def _stream(_message):
            if steer_timing == "during":
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Recovered before steer. ")
            elif steer_timing == "after":
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Recovered before steer. ")
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Recovered final.")
            steer_ready.set()
            await continue_stream.wait()
            if steer_timing != "after":
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Post-steer answer.")
            yield LLMEvent(
                kind=EVENT_STEER_CONSUMED,
                text=f"<user_message>\n{steer_message}\n</user_message>",
            )
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        inner_client = MagicMock()
        inner_client.supports_steer = True
        inner_client.steer = AsyncMock(return_value=True)
        client.client = inner_client
        slot = state.get_or_create_slot(f"recovery-steer-{steer_timing}")
        slot._titled = True
        target = slot.append("assistant", "", "msg msg-a")
        target["variants"] = [
            {"content": "Prior answer.", "ts": "old-ts"},
            {"content": "", "ts": target["ts"]},
        ]
        target["variant_idx"] = 1
        slot._pending_variant_recovery = _VariantRecoveryOwner(target)

        pre_path = tmp_path / f"pre-{steer_timing}.py"
        pre_path.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(pre_path), "content": "before"}]
        task = asyncio.create_task(
            _run_chat(
                state,
                slot,
                _POSTTOKEN_RECOVER_MSG,
                _synthetic_payload=True,
                _current_message=self._provider_recovery_row(),
            )
        )
        slot.task = task
        await asyncio.wait_for(steer_ready.wait(), timeout=5)

        assert await steer_into_running_turn(state, slot, steer_message) == STEER_STEERED
        assert slot._pending_variant_recovery is None
        assert target["content"] == expected_recovered
        assert target["variants"][1]["content"] == expected_recovered
        assert target["meta"]["file_changes"][0]["path"] == str(pre_path)

        if steer_timing == "during":
            post_path = tmp_path / "post-during.py"
            post_path.write_text("after", encoding="utf-8")
            slot._file_changes = [{"path": str(post_path), "content": "before"}]
        continue_stream.set()
        await task
        await self._drain_bg(state)

        roles = [
            message["role"] for message in slot.messages if message["role"] in {"assistant", "user"}
        ]
        assert roles == ["assistant", "user"] + (["assistant"] if expected_post_steer else [])
        assistant = [message for message in slot.messages if message.get("role") == "assistant"]
        assert assistant[0] is target
        assert assistant[0]["content"] == expected_recovered
        if expected_post_steer:
            assert assistant[1]["content"] == expected_post_steer
        if steer_timing == "during":
            assert target["meta"]["file_changes"][0]["path"] == str(pre_path)
            assert assistant[1]["meta"]["file_changes"][0]["path"] == str(post_path)

    @pytest.mark.asyncio
    async def test_steer_boundary_keeps_ordinary_segments_separate(self, tmp_path, monkeypatch):
        """Without a recovery owner, the existing pre/post-steer history remains."""
        from kiro_crew.acp.types import EVENT_STEER_CONSUMED, STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        steer_ready = asyncio.Event()
        continue_stream = asyncio.Event()
        steer_message = "Take a new direction."

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Ordinary pre-steer answer.")
            steer_ready.set()
            await continue_stream.wait()
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Ordinary post-steer answer.")
            yield LLMEvent(
                kind=EVENT_STEER_CONSUMED,
                text=f"<user_message>\n{steer_message}\n</user_message>",
            )
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        inner_client = MagicMock()
        inner_client.supports_steer = True
        inner_client.steer = AsyncMock(return_value=True)
        client.client = inner_client
        slot = state.get_or_create_slot("ordinary-steer-boundary")
        slot._titled = True
        task = asyncio.create_task(_run_chat(state, slot, "ordinary prompt"))
        slot.task = task
        await asyncio.wait_for(steer_ready.wait(), timeout=5)

        assert await steer_into_running_turn(state, slot, steer_message) == STEER_STEERED
        continue_stream.set()
        await task
        await self._drain_bg(state)

        rows = [
            (message["role"], message.get("content", ""))
            for message in slot.messages
            if message["role"] in {"assistant", "user"}
        ]
        assert rows == [
            ("assistant", "Ordinary pre-steer answer."),
            ("user", steer_message),
            ("assistant", "Ordinary post-steer answer."),
        ]

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

    def test_recovery_preserves_per_answer_file_attribution(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import (
            _flush_file_changes,
            _flush_segment,
            _VariantRecoveryOwner,
        )

        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("selector-file-attribution")
        old_path = str(tmp_path / "old.py")
        old_changes = [{"path": old_path, "before": "old-0", "after": "old-1"}]
        slot._pending_variants = [
            {
                "content": "prior answer",
                "ts": "old-ts",
                "meta": {"file_changes": old_changes},
            }
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

        target = next(message for message in slot.messages if message.get("role") == "assistant")
        slot._pending_variant_recovery = _VariantRecoveryOwner(target)

        first_path = tmp_path / "first.py"
        first_path.write_text("first-after", encoding="utf-8")
        slot._file_changes = [{"path": str(first_path), "content": "first-before"}]
        _flush_file_changes(slot)

        second_path = tmp_path / "second.py"
        second_path.write_text("second-after", encoding="utf-8")
        slot.append("chunk", "recovered answer", "chunk")
        _flush_segment(
            state,
            slot,
            "recovered answer",
            broadcast=False,
            complete_pending_variant_recovery=True,
        )
        slot._file_changes = [{"path": str(second_path), "content": "second-before"}]
        _flush_file_changes(slot)

        assert [variant["content"] for variant in target["variants"]] == [
            "prior answer",
            "recovered answer",
        ]
        assert target["variants"][0]["meta"]["file_changes"] == old_changes
        active_changes = target["variants"][1]["meta"]["file_changes"]
        assert [entry["path"] for entry in active_changes] == [
            str(first_path),
            str(second_path),
        ]
        assert target["meta"]["file_changes"] == active_changes

    @pytest.mark.asyncio
    async def test_late_cancel_after_terminal_recovery_commits_once(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import (
            _POSTTOKEN_RECOVER_MSG,
            _VariantRecoveryOwner,
        )
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the recovered answer")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        delivery_started = asyncio.Event()
        delivery_block = asyncio.Event()

        async def _blocked_delivery(*_args):
            delivery_started.set()
            await delivery_block.wait()

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner._deliver_cross_surface_reply",
            _blocked_delivery,
        )
        state = self._state(tmp_path, monkeypatch)
        client = AsyncMock()
        client.context_usage_pct = MagicMock(return_value=0.0)
        client.stream = _stream
        client.stream_command = _stream
        client.served_model = "gpt-test-model"
        self._wire(state, client)
        slot = state.get_or_create_slot("late-cancel-recovery")
        slot._titled = True
        target = slot.append("assistant", "", "msg msg-a")
        target["variants"] = [
            {"content": "prior answer", "ts": "old-ts"},
            {"content": "", "ts": target["ts"]},
        ]
        target["variant_idx"] = 1
        slot._pending_variant_recovery = _VariantRecoveryOwner(target)

        task = asyncio.create_task(
            _run_chat(
                state,
                slot,
                _POSTTOKEN_RECOVER_MSG,
                _synthetic_payload=True,
                _current_message=self._provider_recovery_row(),
            )
        )
        await asyncio.wait_for(delivery_started.wait(), timeout=5)
        task.cancel()
        await task
        await self._drain_bg(state)

        assistant = [message for message in slot.messages if message.get("role") == "assistant"]
        assert assistant == [target]
        assert target["content"] == "the recovered answer"
        assert [variant["content"] for variant in target["variants"]] == [
            "prior answer",
            "the recovered answer",
        ]
        assert slot._pending_variant_recovery is None

    @pytest.mark.asyncio
    async def test_initial_stage_dispatch_clears_stale_banner_stop_reason(
        self, tmp_path, monkeypatch
    ):
        """A pre-dispatch initial turn cannot reuse a prior banner terminal."""
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat_runner import _STOP_REASON_PROVIDER_BUDGET_ARTIFACT

        state, slot = self._stage_state(tmp_path, monkeypatch, "stage-initial-stale-stop")
        slot._last_stop_reason = _STOP_REASON_PROVIDER_BUDGET_ARTIFACT
        calls: list[str] = []

        async def _pre_dispatch_failure(_state, _slot, message, **_kwargs):
            calls.append(message)

        monkeypatch.setattr(chat_orchestrator, "_run_chat", _pre_dispatch_failure)

        await chat_orchestrator._stage_loop(state, slot, auto_run=True)

        assert len(calls) == 1
        assert slot._last_stop_reason == ""
        assert not any(
            "internal model status" in message.get("content", "") for message in slot.messages
        )

    @pytest.mark.asyncio
    async def test_recovery_dispatch_clears_stale_banner_stop_reason(self, tmp_path, monkeypatch):
        """A pre-dispatch recovery failure cannot be classified as a second banner."""
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat_runner import _STOP_REASON_PROVIDER_BUDGET_ARTIFACT

        state, slot = self._stage_state(tmp_path, monkeypatch, "stage-recovery-stale-stop")
        calls: list[str] = []

        async def _mock_run_chat(_state, _slot, message, **_kwargs):
            calls.append(message)
            if len(calls) == 1:
                _slot._last_stop_reason = _STOP_REASON_PROVIDER_BUDGET_ARTIFACT

        monkeypatch.setattr(chat_orchestrator, "_run_chat", _mock_run_chat)

        await chat_orchestrator._stage_loop(state, slot, auto_run=True)

        assert len(calls) == 2
        assert slot._last_stop_reason == ""
        assert any(
            "continuation did not complete" in message.get("content", "")
            for message in slot.messages
        )
        assert not any(
            "internal model status twice" in message.get("content", "") for message in slot.messages
        )
