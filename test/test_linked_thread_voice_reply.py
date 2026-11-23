"""A message in a Slack thread linked to a dashboard session gets a spoken answer.

Both Slack paths hand a linked thread's message to the dashboard slot
(``route_linked_thread``) and return before their own voice step, so the slot's
turn is the one that answers it. The route gives that turn the message's voice
settings (the thread's session key, the sender, whether it was a voice memo), and
the turn's Slack mirror speaks the answer through ``_reply_by_voice`` after posting
it. A turn typed in the dashboard carries none and stays text-only.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from slack_transport_dispatch_helpers import RecordingSlackClient
from turn_harness import TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    AcpEvent,
)

#: Past the fifty-character floor below which ``_reply_by_voice`` stays silent.
_ANSWER = "The gateway restarted cleanly and every channel reconnected within a minute."


def _linked_state() -> MagicMock:
    """Dashboard state whose every thread is linked to one idle slot."""
    slot = MagicMock(key="slot1", _queue=[])
    type(slot).running = PropertyMock(return_value=False)
    ds = MagicMock(_background_tasks=set())
    ds.get_linked_slot = MagicMock(return_value=slot)
    return ds


def _slack() -> MagicMock:
    return MagicMock(post_message=AsyncMock(), post_blocks=AsyncMock())


async def _route_linked(entry: str, **kwargs: Any) -> AsyncMock:
    """Send one message through *entry* into a thread linked to an idle slot."""
    from kiro_crew.slack import handler, transport_dispatch

    sessions = MagicMock()
    sessions.get_session_for_thread = MagicMock(return_value="slack:9.0")
    send = {
        "native": handler.handle_message,
        "transport": transport_dispatch.handle_message_transport,
    }[entry]
    with (
        patch.object(handler, "_dashboard_state", _linked_state()),
        patch.object(handler, "is_allowed_user", return_value=True),
        patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as run_chat,
    ):
        await send(_slack(), sessions, "C1", _ANSWER, "9.0", "9.1", "U1", **kwargs)
    run_chat.assert_called_once()
    return run_chat


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["native", "transport"])
@pytest.mark.parametrize("memo", [True, False])
async def test_the_linked_slot_turn_carries_the_messages_voice_settings(
    entry: str, memo: bool, sel_private_root: Any
) -> None:
    run_chat = await _route_linked(entry, had_voice_input=memo)
    assert run_chat.call_args.kwargs["_slack_voice"] == ("slack:9.0", "U1", memo)


@pytest.mark.asyncio
async def test_a_route_without_voice_settings_starts_a_text_only_turn(
    sel_private_root: Any,
) -> None:
    """A route that passes no voice input (a code channel's) starts a turn that is not spoken."""
    from kiro_crew.slack import handler

    with (
        patch.object(handler, "_dashboard_state", _linked_state()),
        patch.object(handler, "is_allowed_user", return_value=True),
        patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as run_chat,
    ):
        routed = await handler.route_linked_thread(
            _ANSWER, "slack:9.0", "U1", "C1", _slack(), "9.0"
        )
    assert routed is handler.LinkedRoute.ROUTED
    assert run_chat.call_args.kwargs["_slack_voice"] is None


async def _mirrored_turn(
    monkeypatch: pytest.MonkeyPatch, stop_reason: str, message: str = "hello", **run_kwargs: Any
):
    """Run one dashboard turn linked to thread-1 in C-1; return the Slack client and
    every ``_reply_by_voice`` call the mirror made."""
    from kiro_crew.dashboard import chat_runner

    spoken: list[tuple[Any, ...]] = []

    async def _record(*args: Any) -> None:
        spoken.append(args)

    monkeypatch.setattr(chat_runner, "_reply_by_voice", _record)
    slack = RecordingSlackClient()

    def _linked(ctx: Any) -> None:
        ctx.state.slack_client = slack
        ctx.state.sessions.set_slack_link("dashboard:chat-1", "thread-1", "C-1")

    await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text=_ANSWER),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason=stop_reason),
            ],
            message=message,
            setup=_linked,
            run_kwargs=run_kwargs,
        )
    )
    return slack, spoken


@pytest.mark.asyncio
async def test_the_mirror_speaks_a_routed_messages_answer_into_its_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slack, spoken = await _mirrored_turn(
        monkeypatch, STOP_REASON_END_TURN, _slack_voice=("slack:9.0", "U1", True)
    )
    texts = [kw["text"] for name, kw in slack.transcript if name == "post_message"]
    assert any(_ANSWER in text for text in texts)
    assert spoken == [(slack, "C-1", "thread-1", "U1", "slack:9.0", _ANSWER, _ANSWER, True)]


@pytest.mark.asyncio
async def test_a_prompts_get_hand_off_still_speaks(monkeypatch: pytest.MonkeyPatch) -> None:
    """``/prompts get`` re-enters the runner with the expanded prompt, and the nested
    turn that answers carries the routed message's voice settings."""
    from kiro_crew.dashboard import chat_runner

    monkeypatch.setattr(
        chat_runner, "_resolve_prompt_mention", lambda *_a, **_kw: ("the expanded prompt", "ok", "")
    )
    _slack, spoken = await _mirrored_turn(
        monkeypatch,
        STOP_REASON_END_TURN,
        message="/prompts get notes",
        _slack_voice=("slack:9.0", "U1", False),
    )
    assert [args[1:5] for args in spoken] == [("C-1", "thread-1", "U1", "slack:9.0")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_reason", "run_kwargs"),
    [
        # Typed in the dashboard: mirrored as text, as before.
        (STOP_REASON_END_TURN, {}),
        # Cancelled: no finished answer to speak.
        (STOP_REASON_CANCELLED, {"_slack_voice": ("slack:9.0", "U1", False)}),
    ],
)
async def test_the_mirror_stays_text_only_otherwise(
    monkeypatch: pytest.MonkeyPatch, stop_reason: str, run_kwargs: dict[str, Any]
) -> None:
    slack, spoken = await _mirrored_turn(monkeypatch, stop_reason, **run_kwargs)
    texts = [kw["text"] for name, kw in slack.transcript if name == "post_message"]
    assert any(_ANSWER in text for text in texts)
    assert spoken == []


def _voiced_turn(**overrides: Any) -> SimpleNamespace:
    """What ``_speak_linked_reply`` reads off a turn answering a routed message."""
    fields: dict[str, Any] = {
        "_slack_voice": ("slack:9.0", "U1", False),
        "_stop_reason": STOP_REASON_END_TURN,
        "state": SimpleNamespace(slack_client=RecordingSlackClient()),
        "_mirror_chan": "C-1",
        "_mirror_post_ts": "thread-1",
    }
    return SimpleNamespace(**{**fields, **overrides})


@pytest.mark.asyncio
async def test_the_clip_is_made_in_the_channel_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the owner's redaction off, the turn runs in the CHAT seam, where credential
    passes stand down. The clip reaches every member of the thread, so it is made in
    the CHANNELS seam, where they redact, as the thread's text is."""
    from kiro_crew import redaction_seams
    from kiro_crew.dashboard import chat_runner
    from kiro_crew.security.redaction_switch import credential_pass_bypassed

    bypassed: list[bool] = []

    async def _record(*args: Any) -> None:
        bypassed.append(credential_pass_bypassed())

    monkeypatch.setattr(chat_runner, "_reply_by_voice", _record)
    monkeypatch.setattr(redaction_seams, "_redaction_off", True)
    with redaction_seams.output_seam(redaction_seams.CHAT):
        assert credential_pass_bypassed()
        await chat_runner._speak_linked_reply(_voiced_turn(), _ANSWER)
    assert bypassed == [False]


@pytest.mark.asyncio
async def test_a_code_channels_own_session_is_not_spoken(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bound code channel's session posts at the channel's top level (no thread ts),
    and its answers stay text-only, a button answer routed through the message
    handler included."""
    from kiro_crew.dashboard import chat_runner

    calls: list[tuple[Any, ...]] = []

    async def _record(*args: Any) -> None:
        calls.append(args)

    monkeypatch.setattr(chat_runner, "_reply_by_voice", _record)
    await chat_runner._speak_linked_reply(_voiced_turn(_mirror_post_ts=None), _ANSWER)
    await chat_runner._speak_linked_reply(_voiced_turn(), _ANSWER)
    assert [args[2] for args in calls] == ["thread-1"]
