"""Every dashboard send to a linked Slack thread waits for a running reconnect.

``POST /api/slack/reconnect`` clears ``DashboardState.slack_client`` for its
window and publishes the new client only behind a connected socket. A send made
in that window waits for the attempt (``state.slack_reconnect_wait``) and goes
out through the client it publishes; ``test_linked_turn_across_reconnect`` covers
the turn's reply, this covers the other senders: the tool-step stream, the turn
end's stream close and placeholder removal, the approval card, system messages,
the owner's direct message and the compaction notice.
"""

from __future__ import annotations

import contextlib
import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from turn_harness import APPROVED, Do, TurnContext, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TOOL_CALL,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.dashboard.slack_reconnect_wait import APPROVAL_NOTICE, TURN_END_NOTICE
from kiro_crew.slack.client import SlackClientOps

_SESSION = "dashboard:chat-1"
_REQUEST = "req-1"


class _Slack:
    """Records every port call by name; each returns a ts named after the call."""

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

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _waits_for(state: Any, publishes: _Slack | None, waited: list[bool]) -> None:
    """Start a reconnect on *state*: no client, and its wait publishes *publishes*."""
    state.slack_client = None

    async def _wait() -> None:
        waited.append(True)
        state.slack_client = publishes

    state.slack_reconnect_wait = _wait


def _reconnect(publishes: _Slack | None, waited: list[bool]) -> Do:
    return Do(lambda ctx: _waits_for(ctx.state, publishes, waited))


def _linked(old: _Slack, notices: list[str]) -> Any:
    def _setup(ctx: TurnContext) -> None:
        ctx.state.slack_client = old
        ctx.state.sessions.set_slack_link(_SESSION, "thread-1", "C-1")
        ctx.state.notify = lambda kind, title, body, **kw: notices.append(title)

    return _setup


def _tool(n: int) -> AcpEvent:
    return AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id=f"t{n}", title=f"read_{n}", tool_kind="read")


_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)


# ── the tool-step stream and the turn end ───────────────────────────────────


@pytest.mark.asyncio
async def test_a_tool_step_during_a_reconnect_streams_through_the_new_client() -> None:
    old, new, waited, notices = _Slack(), _Slack(), [], []

    await run_turn(
        TurnScript(events=[_reconnect(new, waited), _tool(1), _DONE], setup=_linked(old, notices))
    )

    assert waited
    assert "start_stream" in new.names()
    assert "append_task" in new.names()
    assert "start_stream" not in old.names()


@pytest.mark.asyncio
async def test_a_turn_ending_during_a_reconnect_closes_its_stream_through_the_new_client() -> None:
    old, new, waited, notices = _Slack(), _Slack(), [], []

    await run_turn(
        TurnScript(events=[_tool(1), _reconnect(new, waited), _DONE], setup=_linked(old, notices))
    )

    assert waited
    assert "start_stream" in old.names()
    assert "stop_stream" in new.names()
    assert "stop_stream" not in old.names()


@pytest.mark.asyncio
async def test_a_quiet_turn_removes_its_thinking_placeholder_through_the_new_client() -> None:
    old, new, waited, notices = _Slack(), _Slack(), [], []

    await run_turn(TurnScript(events=[_reconnect(new, waited), _DONE], setup=_linked(old, notices)))

    assert waited
    posted = [a for n, a in old.calls if n == "post_message"]
    assert posted, "the turn posted no placeholder to remove"
    assert ("delete_message", {"self": new, "channel": "C-1", "ts": "post_message-ts"}) in new.calls
    assert TURN_END_NOTICE[0] not in notices


@pytest.mark.asyncio
async def test_a_failed_reconnect_tells_the_owner_the_thread_was_not_tidied() -> None:
    old, waited, notices = _Slack(), [], []

    await run_turn(
        TurnScript(events=[_tool(1), _reconnect(None, waited), _DONE], setup=_linked(old, notices))
    )

    assert waited
    assert "stop_stream" not in old.names()
    assert notices.count(TURN_END_NOTICE[0]) == 1


# ── the approval card ───────────────────────────────────────────────────────


def _linked_slot(old: _Slack, notices: list[str]) -> Any:
    def _setup(ctx: TurnContext) -> None:
        ctx.state.slack_client = old
        # The link's own persisted write is the setup's, not the turn's.
        ctx.state.sessions.batched_save.side_effect = lambda: contextlib.nullcontext()
        ctx.state.link_slack(ctx.slot.key, "thread-1", "C-1")
        ctx.state.notify = lambda kind, title, body, **kw: notices.append(title)

    return _setup


_PROMPT = AcpEvent(
    kind=EVENT_PERMISSION_REQUEST, request_id=_REQUEST, title="fs_write", tool_kind="edit"
)


@pytest.mark.asyncio
async def test_an_approval_asked_during_a_reconnect_posts_and_retires_through_the_new_client() -> (
    None
):
    old, new, waited, notices = _Slack(), _Slack(), [], []

    await run_turn(
        TurnScript(
            events=[_reconnect(new, waited), _PROMPT, _DONE],
            answers={_REQUEST: APPROVED},
            setup=_linked_slot(old, notices),
        )
    )

    assert waited
    assert "post_blocks" in new.names()
    assert "post_blocks" not in old.names()
    assert ("delete_message", {"self": new, "channel": "C-1", "ts": "post_blocks-ts"}) in new.calls
    assert APPROVAL_NOTICE[0] not in notices


@pytest.mark.asyncio
async def test_an_approval_after_a_failed_reconnect_tells_the_owner_once() -> None:
    old, waited, notices = _Slack(), [], []

    await run_turn(
        TurnScript(
            events=[_reconnect(None, waited), _PROMPT, _DONE],
            answers={_REQUEST: APPROVED},
            setup=_linked_slot(old, notices),
        )
    )

    assert waited
    assert "post_blocks" not in old.names()
    assert notices.count(APPROVAL_NOTICE[0]) == 1


@pytest.mark.asyncio
async def test_an_approval_waits_for_the_reconnect_before_its_card_is_pending() -> None:
    """A turn cancelled in the wait must leave no approval future behind."""
    new, pending_in_wait, notices = _Slack(), [], []

    def _reconnect_watching_the_slot(ctx: TurnContext) -> None:
        ctx.state.slack_client = None

        async def _wait() -> None:
            pending_in_wait.append(dict(ctx.slot._approval_futures))
            ctx.state.slack_client = new

        ctx.state.slack_reconnect_wait = _wait

    await run_turn(
        TurnScript(
            events=[Do(_reconnect_watching_the_slot), _PROMPT, _DONE],
            answers={_REQUEST: APPROVED},
            setup=_linked_slot(_Slack(), notices),
        )
    )

    assert pending_in_wait == [{}]
    assert "post_blocks" in new.names()


@pytest.mark.asyncio
async def test_an_approval_in_a_turn_that_started_without_slack_is_not_reported() -> None:
    waited, notices = [], []

    def _linked_without_slack(ctx: TurnContext) -> None:
        _linked_slot(_Slack(), notices)(ctx)
        _waits_for(ctx.state, None, waited)

    await run_turn(
        TurnScript(
            events=[_PROMPT, _DONE],
            answers={_REQUEST: APPROVED},
            setup=_linked_without_slack,
        )
    )

    assert APPROVAL_NOTICE[0] not in notices


# ── system messages, the owner DM and the compaction notice ─────────────────


class _Sessions:
    def __init__(self, link: tuple[str, str]) -> None:
        self._link = link

    def get_slack_link(self, key: str) -> tuple[str, str]:
        return self._link

    def is_slack_paused(self, key: str) -> bool:
        return False


@pytest.mark.asyncio
async def test_a_system_message_during_a_reconnect_posts_through_the_new_client() -> None:
    from kiro_crew.dashboard import chat_runner

    new, waited = _Slack(), []
    state = SimpleNamespace(sessions=_Sessions(("thread-1", "C-1")))
    _waits_for(state, new, waited)
    slot = SimpleNamespace(_slack_thread_ts="", _slack_channel="")

    await chat_runner._deliver_linked_slack_message(
        state, slot, state.sessions, _SESSION, "signed out"
    )

    assert waited
    assert new.names() == ["post_message"]
    assert new.calls[0][1]["text"] == "signed out"


@pytest.mark.asyncio
async def test_an_unlinked_system_message_does_not_wait() -> None:
    from kiro_crew.dashboard import chat_runner

    new, waited = _Slack(), []
    state = SimpleNamespace(sessions=_Sessions(("", "")))
    _waits_for(state, new, waited)
    slot = SimpleNamespace(_slack_thread_ts="", _slack_channel="")

    await chat_runner._deliver_linked_slack_message(
        state, slot, state.sessions, _SESSION, "signed out"
    )

    assert waited == []
    assert new.calls == []


@pytest.mark.asyncio
async def test_an_owner_notice_during_a_reconnect_is_a_dm_through_the_new_client() -> None:
    from kiro_crew.dashboard.server import _dm_owner

    new, waited = _Slack(), []
    state = SimpleNamespace(owner_id="U-OWNER")
    _waits_for(state, new, waited)

    await _dm_owner(state, "your grant expires")

    assert waited
    assert new.names() == ["open_dm", "post_message"]
    assert new.calls[1][1]["text"] == "your grant expires"


@pytest.mark.asyncio
async def test_a_compaction_notice_during_a_reconnect_posts_through_the_new_client() -> None:
    from kiro_crew.dashboard import chat_compaction_notice

    new, waited = _Slack(), []
    state = SimpleNamespace(sessions=_Sessions(("thread-1", "C-1")))
    _waits_for(state, new, waited)

    with patch.object(chat_compaction_notice, "_channel_egress_permitted", return_value=True):
        await chat_compaction_notice._deliver_slack(state, "slack:C-1:thread-1", "compacted")

    assert waited
    assert new.names() == ["post_message"]
    assert new.calls[0][1]["text"] == "compacted"
