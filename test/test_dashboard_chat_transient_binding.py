"""Dashboard chat: a preserved-thinking conversation binding mismatch in ``_run_chat``."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_dashboard_chat import TestRunChatTransientRetry as _TransientSuite

from kiro_crew.dashboard.recovery_replays import ReplayFamily


class TestRunChatTransientRetryBindingMismatch:
    """TestRunChatTransientRetry: a thinking-block binding mismatch rebuilds once, then fails the cycle."""

    _make_state = staticmethod(_TransientSuite._make_state)
    _wire_sessions = staticmethod(_TransientSuite._wire_sessions)
    _client = staticmethod(_TransientSuite._client)
    _drain_bg = staticmethod(_TransientSuite._drain_bg)
    _assistant_texts = _TransientSuite._assistant_texts
    _record_queue_inserts = staticmethod(_TransientSuite._record_queue_inserts)
    _binding_failure = staticmethod(_TransientSuite._binding_failure)

    @pytest.mark.asyncio
    async def test_thinking_binding_mismatch_rebuilds_once_on_fresh_conversation(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        events: list[str] = []

        async def poisoned_stream(_message):
            self._binding_failure()
            yield  # pragma: no cover

        async def fresh_stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="recovered-on-fresh-conversation")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        state = self._make_state(tmp_path, monkeypatch)
        poisoned = self._client(poisoned_stream)
        fresh = self._client(fresh_stream)
        self._wire_sessions(state, poisoned)
        clients = iter((poisoned, fresh))

        async def get_or_create(*_args, **_kwargs):
            client = next(clients)
            events.append("get-poisoned" if client is poisoned else "get-fresh")
            return client, True, False

        async def discard(*_args, **_kwargs):
            events.append("discard")
            return True

        state.sessions.get_or_create = AsyncMock(side_effect=get_or_create)
        state.sessions.discard_conversation = AsyncMock(side_effect=discard)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        await asyncio.wait_for(_run_chat(state, slot, "follow up"), timeout=30)
        await self._drain_bg(state)

        # A plain discard, as the image-history recovery does: no durable
        # replay state and no binding-only session-map argument.
        assert events == ["get-poisoned", "discard", "get-fresh"]
        state.sessions.discard_conversation.assert_awaited_once_with("dashboard:s1")
        state.sessions.reset.assert_not_awaited()
        assert any(
            "recovered-on-fresh-conversation" in text for text in self._assistant_texts(slot)
        )
        # The replay ran and its ledger record was consumed with it.
        assert not slot.replays.armed(ReplayFamily.THINKING_BINDING)

    @pytest.mark.asyncio
    async def test_thinking_binding_mismatch_arms_the_ledger_family(self, tmp_path, monkeypatch):
        """The queued retry is a ledger family, so the drain and consume seams own
        its Stop, steer, follow-up and rebind vetoes."""
        from kiro_crew.dashboard.chat import _run_chat

        async def fail(_message):
            self._binding_failure()
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(fail)
        self._wire_sessions(state, client)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr(chat_runner, "_start_next_queued_turn", AsyncMock(return_value=False))

        await asyncio.wait_for(_run_chat(state, slot, "follow up"), timeout=30)

        assert len(slot._queue) == 1
        assert slot.replays.armed(ReplayFamily.THINKING_BINDING)
        assert slot.replays.entry_id(ReplayFamily.THINKING_BINDING) == slot._queue[0]["id"]
        assert slot._poisoned_reset_used is True

    @pytest.mark.asyncio
    async def test_thinking_binding_mismatch_discard_is_one_shot(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _run_chat

        calls = 0

        async def always_fail(_message):
            nonlocal calls
            calls += 1
            self._binding_failure()
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(always_fail)
        self._wire_sessions(state, client)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        await asyncio.wait_for(_run_chat(state, slot, "follow up"), timeout=30)
        await self._drain_bg(state)

        assert calls == 2
        state.sessions.discard_conversation.assert_awaited_once()
        assert slot._poisoned_reset_used is True

    @pytest.mark.asyncio
    async def test_unrecoverable_binding_mismatch_is_a_terminal_cycle_failure(
        self, tmp_path, monkeypatch
    ):
        """A spent one-shot refuses the rebuild; the mismatch falls through to the
        terminal error, so a self-wake loop's stand-down bound sees it."""
        from kiro_crew.dashboard.chat import _run_chat

        async def fail(_message):
            self._binding_failure()
            yield  # pragma: no cover

        noted: list[tuple[str, bool]] = []

        async def _note(slot_key, _exc, *, self_wake, **_kwargs):
            noted.append((slot_key, self_wake))

        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr(chat_runner, "_note_cycle_failure", _note)
        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(fail)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot._poisoned_reset_used = True
        slot._prestream_exhausted_cycles = 2

        await asyncio.wait_for(
            _run_chat(state, slot, "loop cycle", _directive_self_wake=True), timeout=30
        )
        await self._drain_bg(state)

        assert noted == [(slot.key, True)]
        assert slot._queue == []
        assert slot._prestream_exhausted_cycles == 0
        state.sessions.discard_conversation.assert_not_awaited()
        assert not slot.replays.armed(ReplayFamily.THINKING_BINDING)
        assert any(
            m.get("role") == "error" and m.get("content", "").startswith("❌")
            for m in slot.messages
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("produced", ["text", "thought", "tool_call"])
    async def test_binding_mismatch_after_output_never_replays(
        self, tmp_path, monkeypatch, produced
    ):
        """A mismatch that lands after the turn already produced output (a reply
        chunk, a thought or a tool call) must not discard and replay: the replay
        could repeat what the user already saw or a side effect that ran."""
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_TEXT_CHUNK,
            EVENT_THINKING_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        first = {
            "text": LLMEvent(kind=EVENT_TEXT_CHUNK, text="partial reply"),
            "thought": LLMEvent(kind=EVENT_THINKING_CHUNK, text="partial thought"),
            "tool_call": LLMEvent(kind=EVENT_TOOL_CALL, title="read_file", tool_kind="read"),
        }[produced]

        async def produce_then_fail(_message):
            yield first
            self._binding_failure()

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(produce_then_fail)
        self._wire_sessions(state, client)
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        await asyncio.wait_for(_run_chat(state, slot, "follow up"), timeout=30)
        await self._drain_bg(state)

        state.sessions.discard_conversation.assert_not_awaited()
        assert not slot.replays.armed(ReplayFamily.THINKING_BINDING)
        assert slot._poisoned_reset_used is False
        assert slot._queue == []

    @pytest.mark.asyncio
    async def test_an_owed_completion_hit_by_a_binding_mismatch_runs_before_the_user(
        self, tmp_path, monkeypatch
    ):
        """A sub-agent completion the model never consumed is still owed to the
        parent. A newer user message queued before the mismatch must not refuse
        the rebuild, and the replay is queued as the completion it is (no
        THINKING_BINDING record), so nothing can cancel it: the completion runs
        again on the fresh conversation, then the user's message."""
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        calls: list[str] = []

        async def _stream(msg):
            calls.append(msg)
            if len(calls) == 1:
                self._binding_failure()
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="answered")
            yield LLMEvent(kind=EVENT_COMPLETE)

        state = self._make_state(tmp_path, monkeypatch)
        self._wire_sessions(state, self._client(_stream))
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        inserts = self._record_queue_inserts(monkeypatch, slot)
        slot.queue_insert(0, "a newer user message", kind="")
        consumed = MagicMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await _run_chat(
                state,
                slot,
                "[Subagent completion event] the child finished",
                _turn_actor="subagent",
                _on_consumed=consumed,
            )
            await self._drain_bg(state)

        replays = [i for i in inserts if "the child finished" in i["content"]]
        assert replays == [
            {
                "content": "[Subagent completion event] the child finished",
                "kind": chat_runner.SUBAGENT_COMPLETION_KIND,
            }
        ], inserts
        assert not slot.replays.armed(ReplayFamily.THINKING_BINDING)
        state.sessions.discard_conversation.assert_awaited()
        assert ["the child finished" in c for c in calls[:2]] == [True, True], calls
        assert any("a newer user message" in c for c in calls[2:]), calls
        consumed.assert_any_call(True)
        assert not any("recovery cancelled" in m.get("content", "") for m in slot.messages)

    @pytest.mark.asyncio
    async def test_recoverable_binding_mismatch_resets_outage_bookkeeping(
        self, tmp_path, monkeypatch
    ):
        """The recovery clears retry/fallback counters before the discard."""
        from kiro_crew.dashboard.chat import _run_chat

        async def fail(_message):
            self._binding_failure()
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(fail)
        self._wire_sessions(state, client)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        slot._prestream_exhausted_cycles = 2
        slot._transient_5xx_retries = 1
        slot._infra_retries = 1
        seen: dict[str, int] = {}

        async def discard_then_stop(*_args, **_kwargs):
            seen["prestream"] = slot._prestream_exhausted_cycles
            seen["transient"] = slot._transient_5xx_retries
            seen["infra"] = slot._infra_retries
            # Stop the queued retry so only the first mismatch is observed.
            slot._stop_generation += 1
            return True

        state.sessions.discard_conversation = AsyncMock(side_effect=discard_then_stop)

        await asyncio.wait_for(_run_chat(state, slot, "follow up"), timeout=30)
        await self._drain_bg(state)

        assert seen == {"prestream": 0, "transient": 0, "infra": 0}

    @pytest.mark.asyncio
    async def test_thinking_binding_retry_is_dropped_when_stop_lands_during_discard(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.recovery_replays import THINKING_BINDING_CANCELLED_TEXT

        calls = 0

        async def fail(_message):
            nonlocal calls
            calls += 1
            self._binding_failure()
            yield  # pragma: no cover

        state = self._make_state(tmp_path, monkeypatch)
        client = self._client(fail)
        self._wire_sessions(state, client)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        slot = state.get_or_create_slot("s1")
        slot._titled = True

        async def discard_then_stop(*_args, **_kwargs):
            slot._stop_generation += 1
            return True

        state.sessions.discard_conversation = AsyncMock(side_effect=discard_then_stop)

        await asyncio.wait_for(_run_chat(state, slot, "follow up"), timeout=30)
        await self._drain_bg(state)

        assert calls == 1
        assert state.sessions.get_or_create.await_count == 1
        assert slot._queue == []
        assert any(m.get("content") == THINKING_BINDING_CANCELLED_TEXT for m in slot.messages)
        # The ledger refunds the shared one-shot of a replay that never ran.
        assert slot._poisoned_reset_used is False
