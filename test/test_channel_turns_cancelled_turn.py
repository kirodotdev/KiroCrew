"""The shared channel pipeline restores the turn a Stop cut off, once.

A soft Stop sets ``prev_turn_cancelled`` because the backend drops the stopped turn
from its own log. The dashboard and Slack re-send it on the next turn; every channel
on :class:`~kiro_crew.messaging.dispatch.ChannelTurns` must do the same and clear the
flag, or a later reader re-sends a long-stale turn. The channel Stop paths that cancel
the provider directly set the flag through ``arm_cancelled_turn``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from channel_turn_fakes import FakeSessions, Recorder, RecordingCtxBuilder, ScriptedProvider
from channel_turns_helpers import governance_fixture  # noqa: F401
from channel_turns_helpers import published_fixture  # noqa: F401
from channel_turns_helpers import spooled_fixture  # noqa: F401
from channel_turns_helpers import KEY, _answer, _turns

from kiro_crew.executors import EmbedAdmissionTimeout
from kiro_crew.messaging import turn_ceiling
from kiro_crew.messaging.dispatch import Verdict
from kiro_crew.messaging.turn_bracket import arm_cancelled_turn
from kiro_crew.messaging.turn_ceiling import ConversationTurnCeiling


def _stopped(ctx: RecordingCtxBuilder) -> tuple[FakeSessions, ScriptedProvider, SimpleNamespace]:
    log = MagicMock()
    log.get_metadata.return_value = {}
    log.get_metadata_status.return_value = ({}, True)
    log.read_messages.return_value = [
        {"role": "user", "content": "write the release notes"},
        {"role": "assistant", "content": "Here is a first draft of"},
    ]
    ctx.conversation_log = log  # type: ignore[attr-defined]
    provider = ScriptedProvider()
    sessions = FakeSessions(provider)
    session = SimpleNamespace(prev_turn_cancelled=True)
    sessions._sessions = {KEY: session}  # type: ignore[attr-defined]
    return sessions, provider, session


def test_the_next_turn_carries_the_stopped_turn_once_and_clears_the_flag() -> None:
    ctx = RecordingCtxBuilder()
    sessions, provider, session = _stopped(ctx)
    recorder = Recorder()
    turns = _turns(sessions, ctx=ctx, recorder=recorder)

    assert _answer(turns, message="continue").verdict is Verdict.ANSWERED

    sent = provider.prompts[0]
    assert "write the release notes" in sent and "Here is a first draft of" in sent
    assert sent.endswith("continue")
    start, end = ctx.builds[0]["user_text_range"]
    assert sent[start:end] == "continue", "only the typed text is the person's"
    assert session.prev_turn_cancelled is False
    assert [r.user_text for r in recorder.records] == [
        "continue"
    ], "the record keeps what was typed"

    _answer(turns, message="next")
    assert provider.prompts[1] == "next", "the restore is one-shot"
    assert "user_text_range" not in ctx.builds[1]


def test_a_build_the_pool_refused_gives_the_flag_back() -> None:
    ctx = RecordingCtxBuilder(build_raises=EmbedAdmissionTimeout("busy"))
    sessions, provider, session = _stopped(ctx)

    assert _answer(_turns(sessions, ctx=ctx)).verdict is Verdict.MEMORY_REFUSED

    assert provider.prompts == []
    assert session.prev_turn_cancelled is True, "nothing reached the model"


def test_a_mock_session_attribute_is_not_a_stop() -> None:
    ctx = RecordingCtxBuilder()
    provider = ScriptedProvider()
    sessions = FakeSessions(provider)
    sessions._sessions = {KEY: MagicMock()}  # type: ignore[attr-defined]

    _answer(_turns(sessions, ctx=ctx))

    assert provider.prompts == ["hi"]


def test_a_direct_channel_stop_arms_only_a_cancel_that_reached_a_turn() -> None:
    session = SimpleNamespace(prev_turn_cancelled=False)
    sessions = SimpleNamespace(_sessions={KEY: session})

    for outcome in ("no_turn", "error", None):
        arm_cancelled_turn(sessions, KEY, outcome)
        assert session.prev_turn_cancelled is False, outcome
    arm_cancelled_turn(sessions, "weixin:agentA:direct:nobody", "acked")
    arm_cancelled_turn(SimpleNamespace(), KEY, "acked")
    assert session.prev_turn_cancelled is False

    arm_cancelled_turn(sessions, KEY, "timeout")
    assert session.prev_turn_cancelled is True


def test_a_ceiling_refusal_gives_the_flag_back(monkeypatch) -> None:
    monkeypatch.setattr(
        turn_ceiling, "_SHARED", ConversationTurnCeiling(max_turns=1, window_secs=3600.0)
    )
    ctx = RecordingCtxBuilder()
    sessions, provider, session = _stopped(ctx)
    session.prev_turn_cancelled = False
    turns = _turns(sessions, ctx=ctx)
    _answer(turns)
    session.prev_turn_cancelled = True

    assert _answer(turns, message="continue").verdict is Verdict.CEILING

    assert provider.prompts == ["hi"], "the refused turn never opened a prompt"
    assert session.prev_turn_cancelled is True, "the stopped turn is still owed"
