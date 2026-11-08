"""A quiet end reaches a linked Slack thread as a normal reply.

A patrol or auto-nudge turn that finds nothing ends on ``nothing_to_do`` with no
assistant text. The dashboard shows the outcome on the tool card, but a linked
Slack thread only had the collapsed stream's task step, so a quiet cycle looked
like one that never ran. These drive a REAL turn through ``turn_harness.run_turn``
as a monitor wake and assert the outcome line is posted to the thread.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from turn_harness import TurnContext, TurnScript, run_turn

from kiro_crew import session_directive
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.dashboard.session_directive_apply_outcome import QUIET_END_OUTCOME_PREFIX
from kiro_crew.slack.client import SlackClientOps


class _RecordingSlack:
    """Slack client that records every port call by argument name."""

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
            return f"{name}-ts"

        return _call

    def posts(self) -> list[str]:
        return [a.get("text", "") for n, a in self.calls if n == "post_message"]


def _quiet_end(note: str | None) -> list[AcpEvent]:
    payload = {"note": note} if note is not None else {}
    marker = session_directive.encode("nothing_to_do", payload, "Quiet end requested.")
    return [
        AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id="tc-quiet",
            title="nothing_to_do",
            tool_name="nothing_to_do",
            mcp_server_name=session_directive.CORE_MCP_SERVER,
        ),
        AcpEvent(
            kind=EVENT_TOOL_RESULT, tool_call_id="tc-quiet", tool_output=marker, tool_final=True
        ),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
    ]


async def _run_linked_wake(events: list[AcpEvent]) -> _RecordingSlack:
    slack = _RecordingSlack()

    def _linked(ctx: TurnContext) -> None:
        ctx.state.slack_client = slack
        ctx.state.sessions.set_slack_link("dashboard:chat-1", "thread-1", "C-1")

    await run_turn(
        TurnScript(
            events=events,
            message="[auto-nudge cycle 3]\ncheck the runs",
            user_row=False,
            run_kwargs={"_directive_self_wake": True},
            setup=_linked,
        )
    )
    return slack


class TestQuietEndReachesOtherLinkedChannels:
    @pytest.mark.asyncio
    async def test_the_cross_surface_leg_gets_the_outcome_line(self, monkeypatch) -> None:
        from kiro_crew.dashboard import chat_runner

        sent: list[str] = []

        async def _capture(_state: Any, _key: str, text: str, **_kw: Any) -> None:
            sent.append(text)

        monkeypatch.setattr(chat_runner, "_deliver_cross_surface_reply", _capture)
        await _run_linked_wake(_quiet_end("Cycle 3: unchanged."))
        assert any(QUIET_END_OUTCOME_PREFIX in t and "Cycle 3: unchanged." in t for t in sent), sent


class TestQuietEndReachesTheThread:
    @pytest.mark.asyncio
    async def test_the_note_is_posted_as_the_reply(self) -> None:
        slack = await _run_linked_wake(_quiet_end("Cycle 3: unchanged; same reported condition."))
        replies = [p for p in slack.posts() if QUIET_END_OUTCOME_PREFIX in p]
        assert len(replies) == 1, slack.posts()
        assert "Cycle 3: unchanged" in replies[0]
        # The outcome line asks nothing, so no options control is minted.
        assert "post_blocks" not in [n for n, _ in slack.calls]

    @pytest.mark.asyncio
    async def test_a_quiet_end_without_a_note_still_posts_the_outcome(self) -> None:
        slack = await _run_linked_wake(_quiet_end(None))
        assert any(QUIET_END_OUTCOME_PREFIX in p for p in slack.posts()), slack.posts()

    @pytest.mark.asyncio
    async def test_a_turn_with_its_own_answer_posts_only_that_answer(self) -> None:
        slack = await _run_linked_wake(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="PR #7 went red on lint."),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )
        posts = slack.posts()
        assert any("went red on lint" in p for p in posts), posts
        assert not any(QUIET_END_OUTCOME_PREFIX in p for p in posts), posts
