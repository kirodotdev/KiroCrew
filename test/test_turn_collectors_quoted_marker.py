"""Reply paths outside the channel ``TurnDriver`` drop a quoted directive marker line.

A directive tool's result ends in a ``[[KIROCREW_SESSION_DIRECTIVE]]`` marker line the
model reads, so a reply that repeats the result carries that line too. It is protocol:
each test drives one reply path's real code with the marker split across chunks and
reads what a person is shown.

Mutation: drop the ``strip_quoted_markers`` call (monitor wake, channel post) or the
``QuotedDirectiveFilter`` feed (Slack ``_AnswerStream.on_text``, ``kirocrew chat``) and
its test goes red; drop a ``flush`` / ``release_quoted_hold`` call and the last line is
lost, or the line held at a tool card lands after the card, run into the next text.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from slack_gateway_coverage_helpers import _loop, _slack_nudge_orchestrator
from slack_handler_helpers import FakeProvider, FakeSessionManager

from conftest import MockSlackClient
from kiro_crew import channel as channel_mod
from kiro_crew import cli_chat, session_directive
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, EVENT_TOOL_CALL, LLMEvent
from kiro_crew.slack import gateway as gw
from kiro_crew.slack.handler import handle_message

_QUOTED = session_directive.encode("autonudge_stop", {"reason": "done"}, "Stop REQUESTED.")
# Split inside the marker; the reply's last line has no newline.
_CHUNKS = ["Result:\n", _QUOTED[:-9], _QUOTED[-9:] + "\n", "Done."]
_CLEAN = "Result:\nStop REQUESTED.\nDone."
# Not a directive, so shown as written; held to its newline all the same. It ends a plain
# sentence, so the stream's redactor sends it before the card (``release_sentence_end``).
_KEPT = f"{session_directive.SENTINEL} is the tag."
# A streamed reply whose last line the filter holds: only the end of the turn hands it on.
_HELD_LAST = [*_CHUNKS[:-1], _KEPT]
_CLEAN_HELD_LAST = f"Result:\nStop REQUESTED.\n{_KEPT}"


def _texts(chunks: list[str] = _CHUNKS) -> list[LLMEvent]:
    return [LLMEvent(kind=EVENT_TEXT_CHUNK, text=c) for c in chunks]


class _Scripted:
    def __init__(self, events: list[LLMEvent]) -> None:
        self._events = events

    async def stream(self, message: str, **_kw) -> AsyncIterator[LLMEvent]:
        for event in self._events:
            yield event


@pytest.mark.asyncio
async def test_plain_monitor_wake_posts_and_logs_the_reply_without_the_marker(monkeypatch):
    orch = _slack_nudge_orchestrator()
    orch.conv_log = object()
    saved = AsyncMock()
    monkeypatch.setattr(gw, "stream_and_collect", AsyncMock(return_value="".join(_CHUNKS)))
    monkeypatch.setattr(gw, "save_conversation_turn_off_loop", saved)
    monkeypatch.setattr(gw, "is_thread_temporary", lambda _k: False)
    monkeypatch.setattr(gw, "is_thread_incognito", lambda _k: False)

    assert await orch._fire_slack_nudge(_loop()) is True

    posted = "".join(c.args[1] for c in orch.slack.post_message.await_args_list)
    assert session_directive.SENTINEL not in posted
    assert "Stop REQUESTED." in posted and "Done." in posted
    assert saved.await_args is not None
    assert saved.await_args.args[3] == _CLEAN


@pytest.mark.asyncio
async def test_team_channel_agent_post_drops_the_marker():
    agent = SimpleNamespace(
        id="a1",
        role="dev",
        agent_name="dev",
        session_key="channel:c1:a1",
        approval_policy=SimpleNamespace(value="writes"),
        _approval_future=None,
    )
    ch = SimpleNamespace(id="c1", trusted=False, members={}, _broadcast=lambda *_a: None)
    ch.post = AsyncMock()
    events = _texts() + [LLMEvent(kind=EVENT_COMPLETE, text="")]
    await channel_mod._stream_task(agent, ch, _Scripted(events), "hi")
    assert ch.post.await_args is not None
    assert ch.post.await_args.args[1] == _CLEAN


class _StreamingSlack(MockSlackClient):
    def __init__(self) -> None:
        super().__init__()
        self._stream_enabled = True


def _run_slack_turn(events: list[LLMEvent]) -> list[tuple[str, dict]]:
    slack = _StreamingSlack()
    sessions = FakeSessionManager(FakeProvider(events))
    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))
    return slack.actions


def test_slack_thread_turn_streams_the_reply_without_the_marker():
    actions = _run_slack_turn(_texts(_HELD_LAST))
    streamed = "".join(p["text"] for name, p in actions if name == "append_stream")
    final = [p["text"] for name, p in actions if name == "stop_stream"]
    assert streamed == _CLEAN_HELD_LAST
    assert final == [_CLEAN_HELD_LAST]


@pytest.mark.parametrize(
    ("before_card", "shown"),
    [(_QUOTED, "Stop REQUESTED.\n"), (_KEPT, _KEPT)],
    ids=["marker-dropped", "held-line-kept"],
)
def test_slack_thread_turn_settles_the_held_line_ahead_of_a_tool_card(before_card, shown):
    """The line the filter holds when a tool call arrives is settled there: a marker is
    dropped, any other line joins the text ahead of the card instead of the text after it."""
    events = [
        LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"Result:\n{before_card}"),
        LLMEvent(kind=EVENT_TOOL_CALL, tool_call_id="t1", title="Running: grep"),
        LLMEvent(kind=EVENT_TEXT_CHUNK, text="Next."),
    ]
    actions = _run_slack_turn(events)
    card = [name for name, _p in actions].index("append_task")
    ahead = "".join(p["text"] for name, p in actions[:card] if name == "append_stream")
    streamed = "".join(p["text"] for name, p in actions if name == "append_stream")
    assert ahead == f"Result:\n{shown}"
    assert streamed.endswith("Next.") and "autonudge_stop" not in streamed


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [True, False], ids=["complete", "stream-ends"])
async def test_kirocrew_chat_prints_the_reply_without_the_marker(capsys, terminal):
    events = _texts(_HELD_LAST) + (
        [LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")] if terminal else []
    )
    await cli_chat._send_and_print(_Scripted(events), "q")  # type: ignore[arg-type]
    assert capsys.readouterr().out == _CLEAN_HELD_LAST + "\n"
