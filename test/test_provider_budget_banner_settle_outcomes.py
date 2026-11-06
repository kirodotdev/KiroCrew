"""How a provider-budget-banner turn settles once the banner is classified.

Covers three settle outcomes: the kept answer's last word still reaches the
chunk stream, a banner-only tail whose continuation is refused after a visible
answer settles as a landed turn, and the give-up notice carries its token.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from _provider_budget_banner_helpers import BannerRecoveryHelpers


def _streamed_text(state, slot_key: str) -> str:
    return "".join(
        call.args[1]["content"]
        for call in state.broadcast_ws.call_args_list
        if call.args and call.args[0] == "chat_chunk" and call.args[1].get("slot") == slot_key
    )


class TestProviderBudgetBannerSettleOutcomes(BannerRecoveryHelpers):
    @pytest.mark.parametrize("answer", ["Here is the rest", "Here is the rest.", "ok"])
    @pytest.mark.asyncio
    async def test_kept_answer_streams_its_last_word(self, tmp_path, monkeypatch, answer):
        """The redactor's withheld tail belongs to the kept answer, not the banner."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted tokens left")
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"\n{answer}")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("kept-tail")
        slot._titled = True
        slot._posttoken_retry_used = True

        await _run_chat(
            state,
            slot,
            _POSTTOKEN_RECOVER_MSG,
            _synthetic_payload=True,
            _current_message=self._provider_recovery_row(),
        )
        await self._drain_bg(state)

        assert self._assistant_texts(slot) == [answer]
        assert _streamed_text(state, "kept-tail").endswith(answer)

    @pytest.mark.asyncio
    async def test_refused_continuation_after_visible_answer_settles_as_landed(
        self, tmp_path, monkeypatch
    ):
        """Nothing re-runs, and the answer already showed, so the turn landed."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
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
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="The fix is applied.")
            yield LLMEvent(
                kind=EVENT_TOOL_CALL, title="write_file", tool_kind="write", tool_call_id="tc-1"
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted tokens left")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        reply = MagicMock()
        consolidate = MagicMock()
        monkeypatch.setattr(chat_runner, "post_turn_reply", reply)
        monkeypatch.setattr(chat_runner, "_maybe_consolidate", consolidate)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("landed-banner-tail")
        slot._titled = True
        # This IS the one continuation, and it leaked the banner again after
        # its answer, so nothing can re-run.
        slot._posttoken_retry_used = True
        changed = tmp_path / "changed.py"
        changed.write_text("after", encoding="utf-8")
        slot._file_changes = [{"path": str(changed), "content": "before"}]

        await _run_chat(
            state,
            slot,
            _POSTTOKEN_RECOVER_MSG,
            _synthetic_payload=True,
            _current_message=self._provider_recovery_row(),
        )
        await self._drain_bg(state)

        assert calls == 1
        assert not slot._queue
        assert not any("weighted tokens left" in t for t in self._assistant_texts(slot))
        reply.assert_called_once()
        consolidate.assert_called_once()
        state.sessions.record_success.assert_called_once()
        # The answer was shown, so no notice claims it is missing.
        assert not any(
            m.get("role") == "notice" and "internal status" in m.get("content", "")
            for m in slot.messages
        )

    @pytest.mark.asyncio
    async def test_agent_started_turn_with_a_shown_answer_settles_as_landed(
        self, tmp_path, monkeypatch
    ):
        """An agent-started turn has no prompt to continue, so the banner arm never
        runs; a turn that showed its answer and ended on a stripped banner tail must
        still settle as landed (reply notification, consolidation, success)."""
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            LLMEvent,
        )

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="The fix is applied.")
            yield LLMEvent(
                kind=EVENT_TOOL_CALL, title="write_file", tool_kind="write", tool_call_id="tc-1"
            )
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted tokens left")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        reply = MagicMock()
        consolidate = MagicMock()
        monkeypatch.setattr(chat_runner, "post_turn_reply", reply)
        monkeypatch.setattr(chat_runner, "_maybe_consolidate", consolidate)
        client = self._client(_stream)
        # An agent-started turn reads the session's pending turn, not a prompt.
        client.stream_unsolicited = lambda *_a, **_k: _stream("")
        client.agent_turn_pending = lambda: True
        self._wire(state, client)
        slot = state.get_or_create_slot("agent-started-banner-tail")
        slot._titled = True

        await _run_chat(state, slot, "", _agent_started=True, _synthetic_payload=True)
        await self._drain_bg(state)

        assert not slot._queue
        assert not any("weighted tokens left" in t for t in self._assistant_texts(slot))
        reply.assert_called_once()
        consolidate.assert_called_once()
        state.sessions.record_success.assert_called_once()

    @pytest.mark.asyncio
    async def test_give_up_notice_carries_its_token(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_END_TURN
        from kiro_crew.dashboard import gateway_notice as gn
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.dashboard.chat_runner import _POSTTOKEN_RECOVER_MSG
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def _stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="You have 8154 weighted tokens left")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        state = self._state(tmp_path, monkeypatch)
        client = self._client(_stream)
        self._wire(state, client)
        slot = state.get_or_create_slot("give-up-token")
        slot._titled = True
        slot._posttoken_retry_used = True

        await _run_chat(
            state,
            slot,
            _POSTTOKEN_RECOVER_MSG,
            _synthetic_payload=True,
            _current_message=self._provider_recovery_row(),
        )
        await self._drain_bg(state)

        notices = [
            m
            for m in slot.messages
            if m.get("role") == "notice" and "internal status" in m.get("content", "")
        ]
        assert len(notices) == 1
        assert (
            notices[0]["meta"][gn.GATEWAY_NOTICE_META_KEY]
            == gn.GATEWAY_NOTICE_PROVIDER_BUDGET_ARTIFACT_GIVE_UP
        )
