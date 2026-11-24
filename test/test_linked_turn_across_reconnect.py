"""A Slack-linked dashboard turn keeps its thread across a Slack reconnect.

``POST /api/slack/reconnect`` clears ``DashboardState.slack_client`` for its
window and publishes the new client only behind a connected socket. These drive
a real turn through ``turn_harness.run_turn`` with the gateway's reconnect wait
(``state.slack_reconnect_wait``) scripted: it publishes a new client, or none
for a failed attempt, and the owner may pause the link while it runs.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from turn_harness import Do, TurnContext, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.slack.client import SlackClientOps

_SESSION = "dashboard:chat-1"
_ANSWER = "The answer the thread is owed."


class _Slack:
    """Records every port call by argument name; posts answer with a ts."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        declared = getattr(SlackClientOps, name, None) if not name.startswith("_") else None
        if not inspect.iscoroutinefunction(declared):
            raise AttributeError(name)
        signature = inspect.signature(declared)

        async def _call(*args: Any, **kwargs: Any) -> Any:
            bound = signature.bind(self, *args, **kwargs)
            self.calls.append((name, dict(bound.arguments)))
            return f"{name}-ts"

        return _call

    def thread_text(self) -> str:
        return "\n".join(str(a.get("text", "")) for n, a in self.calls if n == "post_message")


def _reconnect(ctx: TurnContext, publishes: _Slack | None, waited: list[bool]) -> None:
    """Start a reconnect: the client is cleared, and its wait publishes *publishes*."""
    ctx.state.slack_client = None

    async def _wait() -> None:
        waited.append(True)
        ctx.state.slack_client = publishes

    ctx.state.slack_reconnect_wait = _wait


def _answer(*steps: Any) -> list[Any]:
    return [
        *steps,
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=_ANSWER),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
    ]


@pytest.mark.asyncio
async def test_a_reply_finishing_during_a_reconnect_posts_with_the_new_client() -> None:
    old, new, waited = _Slack(), _Slack(), []

    def _linked(ctx: TurnContext) -> None:
        ctx.state.slack_client = old
        ctx.state.sessions.set_slack_link(_SESSION, "thread-1", "C-1")

    await run_turn(
        TurnScript(events=_answer(Do(lambda ctx: _reconnect(ctx, new, waited))), setup=_linked)
    )

    assert waited
    assert _ANSWER in new.thread_text()
    assert _ANSWER not in old.thread_text()


@pytest.mark.asyncio
async def test_a_failed_reconnect_ends_the_reply_in_a_not_delivered_notice() -> None:
    old, waited = _Slack(), []
    notices: list[tuple[str, dict[str, Any]]] = []

    def _linked(ctx: TurnContext) -> None:
        ctx.state.slack_client = old
        ctx.state.sessions.set_slack_link(_SESSION, "thread-1", "C-1")
        ctx.state.notify = lambda kind, title, body, **kw: notices.append((title, kw))

    await run_turn(
        TurnScript(events=_answer(Do(lambda ctx: _reconnect(ctx, None, waited))), setup=_linked)
    )

    assert waited
    assert _ANSWER not in old.thread_text()
    assert [title for title, _ in notices] == ["Slack: reply not delivered"]
    assert notices[0][1]["meta"] == {"session_key": _SESSION, "channel": "C-1"}


@pytest.mark.asyncio
async def test_a_link_paused_while_the_reply_waits_gets_neither_reply_nor_notice() -> None:
    old, new, waited = _Slack(), _Slack(), []
    notices: list[tuple[str, dict[str, Any]]] = []

    def _linked(ctx: TurnContext) -> None:
        ctx.state.slack_client = old
        ctx.state.sessions.set_slack_link(_SESSION, "thread-1", "C-1")
        ctx.state.notify = lambda kind, title, body, **kw: notices.append((title, kw))

    def _reconnect_while_the_owner_pauses(ctx: TurnContext) -> None:
        _reconnect(ctx, new, waited)
        publish_new = ctx.state.slack_reconnect_wait

        async def _wait() -> None:
            # The owner disconnects the conversation while the reply is held.
            ctx.state.sessions.is_slack_paused.side_effect = lambda key: key == _SESSION
            await publish_new()

        ctx.state.slack_reconnect_wait = _wait

    await run_turn(TurnScript(events=_answer(Do(_reconnect_while_the_owner_pauses)), setup=_linked))

    assert waited
    assert _ANSWER not in new.thread_text()
    assert _ANSWER not in old.thread_text()
    assert "Slack: reply not delivered" not in [title for title, _ in notices]


@pytest.mark.asyncio
async def test_a_turn_started_during_a_reconnect_is_mirrored_once_it_succeeds() -> None:
    new, waited = _Slack(), []

    def _linked_while_reconnecting(ctx: TurnContext) -> None:
        ctx.state.sessions.set_slack_link(_SESSION, "thread-1", "C-1")
        _reconnect(ctx, new, waited)

    await run_turn(
        TurnScript(events=_answer(), message="ship it", setup=_linked_while_reconnecting)
    )

    assert waited
    assert "ship it" in new.thread_text()
    assert _ANSWER in new.thread_text()


@pytest.mark.asyncio
async def test_an_unlinked_turn_does_not_wait_for_a_reconnect() -> None:
    new, waited = _Slack(), []

    await run_turn(TurnScript(events=_answer(), setup=lambda ctx: _reconnect(ctx, new, waited)))

    assert waited == []
    assert new.calls == []
