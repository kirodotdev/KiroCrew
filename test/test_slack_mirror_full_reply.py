"""The linked-slot Slack mirror publishes the turn's WHOLE answer.

A dashboard turn linked to a Slack thread flushes its answer to a finalized
transcript row at every tool boundary and resets ``turn.assistant_text``
(``_on_tool_call``). The reply-publication block (``_turn_stop_hook``) must
mirror the turn's full answer, not ``turn.assistant_text`` alone: a turn that
answers BEFORE its last tool call keeps only the trailing fragment (or nothing)
in that accumulator, so mirroring it would drop the rest of the reply while the
dashboard keeps the whole thing. These drive a REAL turn through
``turn_harness.run_turn`` with a Slack client whose ``start_stream`` returns
``None`` (a workspace that requires a stream recipient) and assert the full
answer reaches the thread.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
from turn_harness import Do, TurnContext, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.slack.client import SlackClientOps

_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)

# The answer is emitted BEFORE a tool call, so the tool boundary flushes it to a
# finalized row and resets assistant_text; only the tail + OPTIONS follow.
_ANSWER_BODY = "Here is the full analysis the user asked for.\n\n" + ("detail line. " * 40)
_TAIL = "Done.\n\n[OPTIONS: Merge it now | Show me the diff]"


def _tool(n: int) -> AcpEvent:
    return AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id=f"t{n}", title=f"read_{n}", tool_kind="read")


class _NoRecipientSlack:
    """Slack client whose ``start_stream`` returns ``None`` (missing recipient).

    Records each call bound to the real port signature so arguments are read by
    name; ``post_message``/``post_blocks`` return a ts, ``start_stream`` None.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        declared = getattr(SlackClientOps, name, None) if not name.startswith("_") else None
        if not inspect.iscoroutinefunction(declared):
            raise AttributeError(name)
        signature = inspect.signature(declared)

        async def _call(*args: Any, **kwargs: Any) -> Any:
            bound = signature.bind(self, *args, **kwargs)
            bound.apply_defaults()
            self.calls.append((name, dict(bound.arguments)))
            return None if name == "start_stream" else f"{name}-ts"

        return _call

    def thread_text(self) -> str:
        """Everything post_message put on the thread, concatenated in order."""
        return "\n".join(a.get("text", "") for n, a in self.calls if n == "post_message")

    def posted_blocks_json(self) -> str:
        """Every post_blocks payload rendered to JSON, for choice assertions."""
        import json

        return json.dumps([a.get("blocks") for n, a in self.calls if n == "post_blocks"])


async def _run_linked_turn_with_answer_before_last_tool() -> _NoRecipientSlack:
    slack = _NoRecipientSlack()

    def _linked(ctx: TurnContext) -> None:
        ctx.state.slack_client = slack
        ctx.state.sessions.set_slack_link("dashboard:chat-1", "thread-1", "C-1")

    await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text=_ANSWER_BODY),
                _tool(1),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text=_TAIL),
                _DONE,
            ],
            setup=_linked,
        )
    )
    return slack


class TestMirrorReplyPublishesFullAnswer:
    @pytest.mark.asyncio
    async def test_full_answer_reaches_thread_when_answer_preceded_last_tool(self) -> None:
        slack = await _run_linked_turn_with_answer_before_last_tool()
        names = [n for n, _ in slack.calls]
        # The no-recipient stream was attempted and demoted (returned None)...
        assert "start_stream" in names
        posted = slack.thread_text()
        # ...but the WHOLE answer still reaches the thread, not just the remnant.
        assert "full analysis" in posted  # the body emitted before the last tool
        assert "Done." in posted  # the trailing segment after it
        # The OPTIONS block still posts.
        assert "post_blocks" in names

    @pytest.mark.asyncio
    async def test_options_tag_is_not_mirrored_as_literal_text(self) -> None:
        slack = await _run_linked_turn_with_answer_before_last_tool()
        # The OPTIONS tag is pulled into Block Kit, never posted as literal text.
        assert "[OPTIONS:" not in slack.thread_text()


async def _run_linked_turn_that_wrote_a_file(tmp_path: Path) -> _NoRecipientSlack:
    """A turn whose answer precedes a tracked file write, then a short tail.

    The injected ``_file_changes`` entry makes ``_flush_file_changes`` CLEAR the
    live ``slot._turn_reply_mids`` before the mirror leg runs, so the mirror
    must read the pre-flush snapshot of those ids rather than the emptied list.
    """
    slack = _NoRecipientSlack()
    changed = tmp_path / "touched.txt"
    changed.write_text("after content\n", encoding="utf-8")

    def _linked(ctx: TurnContext) -> None:
        ctx.state.slack_client = slack
        ctx.state.sessions.set_slack_link("dashboard:chat-1", "thread-1", "C-1")

    def _inject_file_change(ctx: TurnContext) -> None:
        fc = getattr(ctx.slot, "_file_changes", None)
        if not isinstance(fc, list):
            fc = []
            ctx.slot._file_changes = fc
        fc.append({"path": str(changed), "content": ""})

    await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text=_ANSWER_BODY),
                _tool(1),
                # The write happened during that tool call: record its change.
                Do(_inject_file_change),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text=_TAIL),
                _DONE,
            ],
            setup=_linked,
        )
    )
    return slack


class TestMirrorReplySurvivesFileChangeFlush:
    @pytest.mark.asyncio
    async def test_full_answer_reaches_thread_on_a_turn_that_wrote_a_file(self, tmp_path) -> None:
        # _flush_file_changes clears slot._turn_reply_mids before the mirror leg
        # runs on a file-writing turn. Without the pre-flush snapshot the mirror
        # falls back to the trailing fragment and the body is lost.
        slack = await _run_linked_turn_that_wrote_a_file(tmp_path)
        posted = slack.thread_text()
        assert "full analysis" in posted
        assert "Done." in posted


async def _run_linked_turn_with_two_options_markers() -> _NoRecipientSlack:
    """A turn with an OPTIONS line in an early segment and another in the final.

    The early marker precedes a tool call (so it lands in its own reply row);
    the final segment carries the real ask. Only the final choices may become
    actionable controls, and neither marker may post as literal text.
    """
    slack = _NoRecipientSlack()

    def _linked(ctx: TurnContext) -> None:
        ctx.state.slack_client = slack
        ctx.state.sessions.set_slack_link("dashboard:chat-1", "thread-1", "C-1")

    await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="First pass.\n\n[OPTIONS: Alpha | Beta]"),
                _tool(1),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Final answer.\n\n[OPTIONS: Gamma | Delta]"),
                _DONE,
            ],
            setup=_linked,
        )
    )
    return slack


class TestMirrorReplyOptionsComeFromTheFinalSegment:
    @pytest.mark.asyncio
    async def test_only_the_final_options_render_and_no_marker_is_literal(self) -> None:
        slack = await _run_linked_turn_with_two_options_markers()
        # No OPTIONS marker is posted to the thread as literal text.
        assert "[OPTIONS:" not in slack.thread_text()
        # The actionable control carries the FINAL segment's choices, not the
        # earlier segment's stale ones.
        blocks = slack.posted_blocks_json()
        assert "Gamma" in blocks and "Delta" in blocks
        assert "Alpha" not in blocks and "Beta" not in blocks
        # Both bodies still reach the thread.
        posted = slack.thread_text()
        assert "First pass." in posted
        assert "Final answer." in posted


async def _run_linked_turn_options_then_markerless_tail() -> _NoRecipientSlack:
    """A turn that asks via OPTIONS in an early segment, calls a tool, then ends
    with plain text and NO marker (the turn moved past the question).

    No actionable control may be posted: the final segment carries no ask, and a
    button for the earlier choices would let a click send a superseded answer.
    """
    slack = _NoRecipientSlack()

    def _linked(ctx: TurnContext) -> None:
        ctx.state.slack_client = slack
        ctx.state.sessions.set_slack_link("dashboard:chat-1", "thread-1", "C-1")

    await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Which one? [OPTIONS: Alpha | Beta]"),
                _tool(1),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Done, I picked Alpha."),
                _DONE,
            ],
            setup=_linked,
        )
    )
    return slack


class TestMirrorReplyOptionsOnlyFromFinalSegment:
    @pytest.mark.asyncio
    async def test_a_markerless_final_segment_posts_no_control(self) -> None:
        slack = await _run_linked_turn_options_then_markerless_tail()
        names = [n for n, _ in slack.calls]
        # The turn ended without an OPTIONS line, so no control is minted...
        assert "post_blocks" not in names
        # ...and the earlier marker is stripped, never posted as literal text,
        # while both bodies still reach the thread.
        posted = slack.thread_text()
        assert "[OPTIONS:" not in posted
        assert "Which one?" in posted
        assert "Done, I picked Alpha." in posted


async def _run_linked_turn_final_options_then_textless_tool() -> _NoRecipientSlack:
    """The turn's LAST text is an OPTIONS ask, then a tool runs and the turn
    ends with no further text.

    `_on_tool_call` flushes the ask to a reply row and clears
    ``turn.assistant_text``, so the control must come from the newest reply row,
    not the emptied accumulator.
    """
    slack = _NoRecipientSlack()

    def _linked(ctx: TurnContext) -> None:
        ctx.state.slack_client = slack
        ctx.state.sessions.set_slack_link("dashboard:chat-1", "thread-1", "C-1")

    await run_turn(
        TurnScript(
            events=[
                AcpEvent(
                    kind=EVENT_TEXT_CHUNK, text="Which target? [OPTIONS: Staging | Production]"
                ),
                _tool(1),
                _DONE,
            ],
            setup=_linked,
        )
    )
    return slack


class TestMirrorReplyOptionsFromNewestRowEvenWhenFlushed:
    @pytest.mark.asyncio
    async def test_final_options_flushed_before_a_textless_tool_still_render(self) -> None:
        slack = await _run_linked_turn_final_options_then_textless_tool()
        names = [n for n, _ in slack.calls]
        # The ask IS the turn's final reply row, so the control is minted...
        assert "post_blocks" in names
        blocks = slack.posted_blocks_json()
        assert "Staging" in blocks and "Production" in blocks
        # ...and the marker is not posted as literal text, while the question is.
        posted = slack.thread_text()
        assert "[OPTIONS:" not in posted
        assert "Which target?" in posted
