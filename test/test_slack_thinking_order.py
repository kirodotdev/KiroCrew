"""With ``slack.show_thinking`` on, the reasoning reply sits ABOVE the answer.

Slack orders a thread by ts. A tool call opens the answer's stream message, and
reasoning can arrive after it, so the reply must be reserved before that
message opens and filled in place, never posted after it.

The invariant every case here checks, over the messages that are still in the
thread when the turn ends:

* the Thinking reply's ts is earlier than the answer's first message ts, and
* no reasoning text and no Thinking label is inside any answer message.
"""

from __future__ import annotations

import asyncio

import pytest
from redaction_structured_cases import github_classic

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    AcpEvent,
)
from kiro_crew.messaging import TurnDriver
from kiro_crew.slack.agent_sessions import fence
from kiro_crew.slack.format import SLACK_MSG_LIMIT
from kiro_crew.slack.renderer import SlackRenderer
from kiro_crew.testing.wait import default_timeout

REASON_A = "Kyle wants an update on the PR"
REASON_B = "the checks are still running"
ANSWER = "The PR is green."
#: The note that ends a Thinking reply cut to the messages reserved for it.
THINKING_CUT_NOTICE = "\n\n_The rest of the thinking did not fit here._"


class _ThreadSlack:
    """A Slack thread: every message by ts (in posting order) with its final text."""

    def __init__(self, *, stream: bool = True, refuse_thinking: bool = False):
        self._n = 0
        self._stream = stream
        self._refuse_thinking = refuse_thinking
        #: ts -> [kind, text]; kind is "post" or "stream". Deleted ts are removed.
        self.messages: dict[str, list[str]] = {}
        #: Every post_message text, in order, refused and deleted ones included.
        self.posted: list[str] = []
        self.renderer: SlackRenderer | None = None

    def _ts(self) -> str:
        self._n += 1
        return f"1000.{self._n:06d}"

    async def start_stream(self, channel, thread_ts, **kw):
        if not self._stream:
            return None
        ts = self._ts()
        self.messages[ts] = ["stream", kw.get("initial_text") or ""]
        return ts

    async def append_stream(self, channel, ts, text):
        self.messages[ts][1] += text
        return True

    async def stop_stream(self, channel, ts, final_text=None, blocks=None):
        return True

    async def append_task(self, channel, ts, task_id, title, status, details="", output=""):
        return True

    async def update_message(self, channel, ts, text="", blocks=None):
        if fence.turn_muted(channel, ts=ts):
            return  # RealSlackClient drops a stopped turn's edits the same way
        self.messages[ts][1] = text

    async def delete_message(self, channel, ts):
        self.messages.pop(ts, None)

    async def set_thread_status(self, channel, thread_ts, status):
        return True

    async def post_blocks(self, channel, blocks, text, thread_ts=None, **kw):
        ts = self._ts()
        self.messages[ts] = ["footer", text]
        return ts

    async def post_message(self, channel, text, thread_ts=None, **kw):
        self.posted.append(text)
        if self._refuse_thinking and text.startswith("*Thinking"):
            raise RuntimeError("chat.postMessage refused")
        ts = self._ts()
        fence.note_message(channel, ts)
        self.messages[ts] = ["post", text]
        return ts

    def thinking_ts(self) -> list[str]:
        """Thinking messages: headed ``*Thinking``, or a continuation the renderer wrote."""
        written = {ts for ts, _ in getattr(self.renderer, "_thinking_msgs", [])}
        return [
            ts
            for ts, (_, text) in self.messages.items()
            if text.startswith("*Thinking") or ts in written
        ]

    def answer_ts(self) -> list[str]:
        return [
            ts
            for ts, (kind, text) in self.messages.items()
            if kind != "footer" and ts not in self.thinking_ts()
        ]


def _run(
    events, *, show_thinking=True, stream=True, thread_ts="t1", refuse_thinking=False
) -> _ThreadSlack:
    slack = _ThreadSlack(stream=stream, refuse_thinking=refuse_thinking)
    renderer = SlackRenderer(
        slack, "C1", thread_ts, reactions_enabled=False, show_thinking=show_thinking
    )
    slack.renderer = renderer

    class _Provider:
        async def stream(self, message):
            for ev in events:
                yield ev

    asyncio.run(TurnDriver(_Provider(), renderer, approval_mode="auto").run("hi"))
    return slack


def _assert_invariant(slack: _ThreadSlack, *reasoning: str) -> None:
    thinking, answer = slack.thinking_ts(), slack.answer_ts()
    assert answer, slack.messages
    if reasoning:
        assert thinking, slack.messages
        assert max(thinking) < min(answer), slack.messages
        shown = "".join(slack.messages[ts][1] for ts in thinking)
        for part in reasoning:
            assert part in shown, slack.messages
    else:
        assert not thinking, slack.messages
    for ts in answer:
        text = slack.messages[ts][1]
        assert "Thinking" not in text, slack.messages
        for part in (REASON_A, REASON_B):
            assert part not in text, slack.messages


def _think(text):
    return AcpEvent(kind=EVENT_THINKING_CHUNK, text=text)


def _tool(call_id, title="grep"):
    return AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id=call_id, title=title, tool_final=False)


def _text(text):
    return AcpEvent(kind=EVENT_TEXT_CHUNK, text=text)


_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")


class TestThinkingAboveAnswer:
    def test_tool_call_turn_puts_thinking_above_the_answer(self):
        # The reported thread: reasoning, a tool call (which opens the answer's
        # stream), then the answer.
        slack = _run([_think(REASON_A), _tool("a"), _text(ANSWER), _DONE])
        _assert_invariant(slack, REASON_A)

    def test_thinking_between_tool_calls_joins_the_reply_above(self):
        slack = _run(
            [_think(REASON_A), _tool("a"), _think(REASON_B), _tool("b"), _text(ANSWER), _DONE]
        )
        _assert_invariant(slack, REASON_A, REASON_B)

    def test_thinking_that_starts_after_the_first_tool_call(self):
        slack = _run([_tool("a"), _think(REASON_A), _text(ANSWER), _DONE])
        _assert_invariant(slack, REASON_A)

    def test_thinking_after_the_first_answer_chunk(self):
        slack = _run(
            [_think(REASON_A), _text("The PR "), _think(REASON_B), _text("is green."), _DONE]
        )
        _assert_invariant(slack, REASON_A, REASON_B)

    def test_no_tool_streamed_turn(self):
        slack = _run([_think(REASON_A), _text(ANSWER), _DONE])
        _assert_invariant(slack, REASON_A)

    def test_non_streamed_answer(self):
        # chat.startStream refused: the answer is a placeholder message edited
        # at the end of the turn.
        slack = _run([_think(REASON_A), _tool("a"), _text(ANSWER), _DONE], stream=False)
        _assert_invariant(slack, REASON_A)

    def test_answer_split_across_several_messages(self):
        long_answer = ("word " * 1000).strip()  # over one Slack message
        slack = _run([_think(REASON_A), _tool("a"), _text(long_answer), _DONE], stream=False)
        assert len(slack.answer_ts()) > 1, slack.messages
        _assert_invariant(slack, REASON_A)

    def test_tool_turn_with_no_thinking_leaves_no_thinking_reply(self):
        slack = _run([_tool("a"), _text(ANSWER), _DONE])
        _assert_invariant(slack)

    def test_show_thinking_off_posts_no_thinking_reply(self):
        slack = _run([_think(REASON_A), _tool("a"), _text(ANSWER), _DONE], show_thinking=False)
        _assert_invariant(slack)

    def test_wait_tool_turn_keeps_thinking_above_the_first_answer_message(self):
        # A ``wait`` stops the stream; the reply after it opens a second one.
        slack = _run([_think(REASON_A), _tool("w", "wait"), _think(REASON_B), _text(ANSWER), _DONE])
        _assert_invariant(slack, REASON_A, REASON_B)

    def test_reasoning_that_outgrows_its_slot_is_cut_there_not_posted_below(self):
        long_reasoning = "because " * 800  # over one Slack message
        slack = _run([_tool("a"), _think(REASON_A + " " + long_reasoning), _text(ANSWER), _DONE])
        _assert_invariant(slack, REASON_A)
        (only,) = slack.thinking_ts()
        assert slack.messages[only][1].endswith(THINKING_CUT_NOTICE), slack.messages
        assert len(slack.messages[only][1]) <= SLACK_MSG_LIMIT

    def test_long_reasoning_before_the_answer_opens_is_posted_whole(self):
        # Reasoning posted at the first chunk grows past one message, and no
        # answer message exists yet, so the rest goes in further replies above it.
        long_reasoning = "because " * 800
        slack = _run(
            [
                _think(REASON_A),
                _text("The PR "),
                _think(long_reasoning + REASON_B),
                _text("ok"),
                _DONE,
            ]
        )
        _assert_invariant(slack, REASON_A, REASON_B)
        assert len(slack.thinking_ts()) > 1, slack.messages
        assert not any(THINKING_CUT_NOTICE in slack.messages[ts][1] for ts in slack.thinking_ts())

    def test_a_cut_credential_is_not_counted_in_the_redaction_notice(self):
        secret = github_classic(seed=44)
        reasoning = REASON_A + " " + "because " * 800 + f" key {secret}"
        slack = _run([_tool("a"), _think(reasoning), _text(ANSWER), _DONE])
        _assert_invariant(slack, REASON_A)
        assert all(secret not in text for _, text in slack.messages.values())
        assert slack.renderer is not None and slack.renderer._redacted_creds == 0

    def test_a_refused_thinking_post_is_tried_once(self):
        slack = _run(
            [_think(REASON_A), _text("a "), _text("b "), _text("c"), _DONE], refuse_thinking=True
        )
        assert sum(p.startswith("*Thinking") for p in slack.posted) == 1, slack.posted
        assert [slack.messages[ts][1] for ts in slack.answer_ts()] == ["a b c"]

    def test_flat_reply_posts_no_placeholder(self):
        # A flat reply's every post is a channel message that notifies.
        slack = _run([_tool("a"), _text(ANSWER), _DONE], thread_ts=None)
        assert not any(p.startswith("*Thinking") for p in slack.posted), slack.posted
        _assert_invariant(slack)


class TestTurnThatDiesBeforeDone:
    """close() settles the reserved slot; it never posts a new reply."""

    def _died(self, thinking: str) -> _ThreadSlack:
        slack = _ThreadSlack()
        renderer = SlackRenderer(slack, "C1", "t1", reactions_enabled=False, show_thinking=True)

        async def _turn() -> None:
            await renderer.on_turn_start()
            await renderer.on_tool_call("a", "grep")
            if thinking:
                await renderer.on_thinking(thinking)
            await renderer.close()

        asyncio.run(_turn())
        return slack

    def test_placeholder_with_no_reasoning_is_removed(self):
        slack = self._died("")
        assert not slack.thinking_ts(), slack.messages

    def test_placeholder_gets_the_reasoning_that_came(self):
        slack = self._died(REASON_A)
        (only,) = slack.thinking_ts()
        assert REASON_A in slack.messages[only][1]
        assert only < min(slack.answer_ts()), slack.messages

    def test_a_stop_inside_the_end_of_turn_cleanup_leaves_it_to_close(self):
        slack = _ThreadSlack()
        renderer = SlackRenderer(slack, "C1", "t1", reactions_enabled=False, show_thinking=True)
        delete = slack.delete_message
        deletes: list[str] = []

        async def _turn() -> None:
            entered = asyncio.Event()

            async def _held_delete(channel, ts):
                deletes.append(ts)
                if len(deletes) == 1:
                    entered.set()
                    await asyncio.Event().wait()  # the stop lands here
                await delete(channel, ts)

            slack.delete_message = _held_delete
            await renderer.on_turn_start()
            await renderer.on_tool_call("a", "grep")
            done = asyncio.create_task(renderer.on_done("end_turn"))
            await asyncio.wait_for(entered.wait(), default_timeout())
            done.cancel()
            with pytest.raises(asyncio.CancelledError):
                await done
            await renderer.close()

        asyncio.run(_turn())
        assert len(deletes) == 2, deletes
        assert not slack.thinking_ts(), slack.messages

    def test_a_native_stop_removes_the_placeholder_it_cannot_fill(self):
        # Reasoning came after the placeholder, then Slack's Stop muted the
        # turn's edits: the placeholder is deleted rather than left up.
        slack = _ThreadSlack()
        renderer = SlackRenderer(slack, "C1", "t1", reactions_enabled=False, show_thinking=True)

        async def _turn() -> None:
            reset = fence.enter_turn("C1", "t1")
            try:
                await renderer.on_turn_start()
                await renderer.on_tool_call("a", "grep")
                await renderer.on_thinking(REASON_A)
                fence.mute_turns("C1", "t1")
                await renderer.close()
            finally:
                fence.exit_turn(reset)

        asyncio.run(_turn())
        assert not slack.thinking_ts(), slack.messages
