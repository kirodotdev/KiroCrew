"""After /clear or Reset conversation the first turn does not quote the forgotten turns.

The reset already suppresses the history replay; the session-start context also
carried a "Recent Session Context" block read from the same session's log. The
dashboard turn tells the builder about the reset so that block is left out too.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class _Reset:
    """Replay-debt seams of a session whose conversation was just reset."""

    def __init__(self, suppressed: bool) -> None:
        self.suppressed = suppressed

    def first_turn_history_owed_pending(self, _key: str) -> bool:
        return False

    def mark_first_turn_history_owed(self, _key: str) -> bool:
        return True

    def consume_first_turn_history_owed(self, _key: str) -> bool:
        return False

    def provider_switch_replay_pending(self, _key: str) -> bool:
        return False

    def consume_replay_suppression(self, _key: str) -> bool:
        suppressed, self.suppressed = self.suppressed, False
        return suppressed


async def _first_turn_build_kwargs(tmp_path, monkeypatch, *, suppressed: bool) -> dict:
    from recovery_l1_chat_runner_helpers import _l1_state, _RecordingSlot

    from kiro_crew.acp.types import EVENT_COMPLETE, STOP_REASON_END_TURN
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.providers.base import LLMEvent

    state, client = _l1_state(tmp_path)
    client.last_infra_error = None
    reset = _Reset(suppressed)
    for name in (
        "first_turn_history_owed_pending",
        "mark_first_turn_history_owed",
        "consume_first_turn_history_owed",
        "provider_switch_replay_pending",
        "consume_replay_suppression",
    ):
        setattr(state.sessions, name, getattr(reset, name))
    monkeypatch.setattr("kiro_crew.context.build_session_replay", lambda *a, **k: "")

    def _stream(*_a, **_kw):
        async def _gen():
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

        return _gen()

    client.stream = MagicMock(side_effect=_stream)
    slot = _RecordingSlot("chat-after-clear")
    with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
        mock_sel.return_value = MagicMock()
        await _run_chat(state, slot, "What code word did I ask you to remember?")
        if slot.task:
            # flake-ok: awaits the turn task _run_chat itself started, on a scripted client that never blocks
            await slot.task
    return state.context_builder.build_message.call_args_list[0].kwargs


@pytest.mark.asyncio
async def test_the_first_turn_after_a_reset_tells_the_builder(tmp_path, monkeypatch):
    kwargs = await _first_turn_build_kwargs(tmp_path, monkeypatch, suppressed=True)
    assert kwargs.get("conversation_reset") is True


@pytest.mark.asyncio
async def test_an_ordinary_cold_start_keeps_the_recent_context(tmp_path, monkeypatch):
    kwargs = await _first_turn_build_kwargs(tmp_path, monkeypatch, suppressed=False)
    assert not kwargs.get("conversation_reset")
