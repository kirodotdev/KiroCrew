"""Slack handling of a V2 content-filter refusal fallback.

On a refusal, kiro-cli streams the refused model's canned explanation as ordinary
text, THEN a ``model_fallback`` hop (``cause: refused``), then the fallback model's
real answer (see ``test/fixtures/acp_frames/kiro/refusal-fallback-live.jsonl``). The
refused text must not reach the delivered reply, and the fallback notice names the
model that answered. When the fallback itself does not answer, the provider's own
words are kept rather than dropped to an empty reply.
"""

from __future__ import annotations

import pytest
from slack_handler_helpers import FakeProvider, FakeSessionManager

from conftest import MockSlackClient
from kiro_crew.acp.engine_fallback import EngineFallbackTracker, ModelFallbackNotice
from kiro_crew.acp.types import STOP_REASON_END_TURN, STOP_REASON_REFUSAL
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack.handler import handle_message

_REFUSED = "I can't help with that."
_ANSWER = "Here is the answer."


def _delivered(slack: MockSlackClient) -> str:
    """The text of the FINAL rendered reply, excluding the ephemeral live stream.

    ``append_stream`` is the live cursor, which may briefly carry the refused text
    before the hop; the delivered copy is the seal's overwrite and the final posts.
    """
    return " ".join(
        str(a[1].get("text") or "")
        for a in slack.actions
        if a[0] in ("post", "update", "stop_stream")
    )


def _refused_hop(*, promoted: bool) -> LLMEvent:
    return LLMEvent(
        kind="model_fallback",
        model_fallback=ModelFallbackNotice(
            from_model="claude-opus-4.6",
            to_model="claude-sonnet-4.6",
            cause="refused",
            promoted=promoted,
        ),
    )


class TestRefusalFallbackDrop:
    @pytest.mark.asyncio
    async def test_refused_text_is_dropped_and_the_fallback_answer_is_kept(self):
        slack = MockSlackClient()
        slack._stream_enabled = True  # exercise the streaming path and its seal
        provider = FakeProvider(
            [
                LLMEvent(kind="text_chunk", text=_REFUSED),
                _refused_hop(promoted=False),
                LLMEvent(kind="text_chunk", text=_ANSWER),
                _refused_hop(promoted=True),
                LLMEvent(kind="complete", stop_reason=STOP_REASON_END_TURN),
            ]
        )
        sessions = FakeSessionManager(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        delivered = _delivered(slack)
        assert _ANSWER in delivered, slack.actions
        assert _REFUSED not in delivered, slack.actions

    @pytest.mark.asyncio
    async def test_the_fallback_notice_names_the_model_that_answered(self):
        slack = MockSlackClient()
        slack._stream_enabled = True
        provider = FakeProvider(
            [
                LLMEvent(kind="text_chunk", text=_REFUSED),
                _refused_hop(promoted=False),
                LLMEvent(kind="text_chunk", text=_ANSWER),
                LLMEvent(kind="complete", stop_reason=STOP_REASON_END_TURN),
            ]
        )
        # The real handle records the turn's fallback walk on the provider's
        # tracker as each notice arrives; mirror that so the shared notice
        # spelling (``annotate_model_fallback``) has a walk to read.
        tracker = EngineFallbackTracker()
        tracker.walk.record(
            ModelFallbackNotice(
                from_model="claude-opus-4.6",
                to_model="claude-sonnet-4.6",
                cause="refused",
                promoted=False,
            )
        )
        provider._engine_fallback = tracker
        sessions = FakeSessionManager(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        delivered = _delivered(slack)
        assert _ANSWER in delivered, slack.actions
        assert _REFUSED not in delivered, slack.actions
        assert "⚠" in delivered, slack.actions
        assert "claude-sonnet-4.6" in delivered, slack.actions

    @pytest.mark.asyncio
    async def test_a_refusal_the_fallback_did_not_recover_keeps_the_providers_words(self):
        slack = MockSlackClient()
        slack._stream_enabled = True
        provider = FakeProvider(
            [
                LLMEvent(kind="text_chunk", text=_REFUSED),
                _refused_hop(promoted=False),
                LLMEvent(kind="complete", stop_reason=STOP_REASON_REFUSAL),
            ]
        )
        sessions = FakeSessionManager(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        # Nothing answered after the hop, so the refusal text is restored rather
        # than dropped to an empty reply.
        assert _REFUSED in _delivered(slack), slack.actions

    @pytest.mark.asyncio
    async def test_a_second_refused_hop_keeps_the_first_refusals_words(self):
        slack = MockSlackClient()
        slack._stream_enabled = True
        first = "The first model declined."
        provider = FakeProvider(
            [
                LLMEvent(kind="text_chunk", text=first),
                _refused_hop(promoted=False),
                # A decline with no text before it: the guard must not blank
                # last_refused with the empty buffer the first hop left.
                _refused_hop(promoted=False),
                LLMEvent(kind="complete", stop_reason=STOP_REASON_REFUSAL),
            ]
        )
        sessions = FakeSessionManager(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        assert first in _delivered(slack), slack.actions

    @pytest.mark.asyncio
    async def test_a_tool_only_fallback_answer_does_not_restore_the_refusal(self):
        slack = MockSlackClient()
        slack._stream_enabled = True
        provider = FakeProvider(
            [
                LLMEvent(kind="text_chunk", text=_REFUSED),
                _refused_hop(promoted=False),
                # The fallback answers with tool work and no text, ending end_turn.
                LLMEvent(
                    kind="tool_call",
                    title="Running: grep",
                    tool_kind="execute",
                    tool_purpose="searching the repo",
                ),
                LLMEvent(kind="complete", stop_reason=STOP_REASON_END_TURN),
            ]
        )
        sessions = FakeSessionManager(provider)

        await handle_message(slack, sessions, "C1", "hello", None, "msg1", "U1")

        # The turn answered (end_turn), so the refusal is NOT restored under a
        # notice that would otherwise read as "another model answered".
        assert _REFUSED not in _delivered(slack), slack.actions
