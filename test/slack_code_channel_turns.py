"""Real dashboard turns that raise one tool prompt in a Slack Code channel.

The code-channel approval tests assert what the runner DOES with a prompt --
where the card posts, whether it is kept, how a worker's card is relayed and
retired -- through ``turn_harness.run_turn``, not by reading the runner's text.
The channel itself (binding store, Slack recorder, feature gate) is the
agent-surface ``env`` the calling test's fixture set up; this module only puts
the harness's slots on it.
"""

from __future__ import annotations

import contextlib
from typing import Any

from turn_harness import SlotSpec, TurnContext, TurnRecord, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    STOP_REASON_END_TURN,
    AcpEvent,
)

REQUEST = "req-turn-1"

_PROMPT = AcpEvent(
    kind=EVENT_PERMISSION_REQUEST, request_id=REQUEST, title="fs_write", tool_kind="edit"
)
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)


def _link(ctx: TurnContext, slot_key: str, anchor: str, channel: str) -> None:
    """Link *slot_key* to the channel the way the bind does (``link_slack``).

    The link's own persisted write is batched; that batch is the setup's, not
    the turn's, so it is answered here rather than in the harness's table.
    """
    ctx.state.sessions.batched_save.side_effect = lambda: contextlib.nullcontext()
    ctx.state.link_slack(slot_key, anchor, channel)


async def prompt_in_channel_session(
    env: Any, *, slot_key: str, channel: str, anchor: str, answer: str, during: Any = None
) -> TurnRecord:
    """One turn in *slot_key*, linked to *channel* at *anchor*, raising one prompt.

    *during*, when given, runs right after Slack accepts each block post, with
    the post's ``(channel, ts)``: what a click on the card would do meanwhile.
    """
    posted = env.slack.post_blocks

    async def _post(ch: str, blocks: list, text: str, thread_ts: Any = None) -> str:
        ts = await posted(ch, blocks, text, thread_ts)
        if during is not None:
            during(ch, ts)
        return ts

    def _linked(ctx: TurnContext) -> None:
        env.slack.post_blocks = _post
        env.slack.deleted = []

        async def _delete(ch: str, ts: str) -> None:
            env.slack.deleted.append((ch, ts))

        env.slack.delete_message = _delete
        ctx.state.slack_client = env.slack
        _link(ctx, slot_key, anchor, channel)

    try:
        return await run_turn(
            TurnScript(events=[_PROMPT, _DONE], answers={REQUEST: answer}, setup=_linked),
            slot=SlotSpec(key=slot_key),
        )
    finally:
        env.slack.post_blocks = posted


async def prompt_in_worker(
    env: Any,
    *,
    worker_key: str,
    title: str,
    conductor_key: str,
    channel: str,
    anchor: str,
    answer: str,
    prompt: AcpEvent | None = None,
) -> TurnRecord:
    """One turn in *worker_key*, which *conductor_key* (the channel's session) created.

    *prompt* replaces the default permission event (same request id)."""

    def _worker(ctx: TurnContext) -> None:
        ctx.state.slack_client = env.slack
        ctx.state.get_or_create_slot(conductor_key)
        _link(ctx, conductor_key, anchor, channel)
        ctx.slot._created_by = conductor_key
        ctx.slot.title = title

    return await run_turn(
        TurnScript(events=[prompt or _PROMPT, _DONE], answers={REQUEST: answer}, setup=_worker),
        slot=SlotSpec(key=worker_key),
    )
