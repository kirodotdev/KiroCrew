"""Sending the timed-out continuation inside the turn whose call timed out.

A turn that ends because a call's own silence timer ran out (``acp.tool_silence``)
has not failed: the model has not yet heard that the call timed out. A consumer that
runs the turn without a recovery queue of its own (a messaging channel turn, the
native Slack turn, an agent channel member's turn, the CLI chat, the task runner;
``stream_and_collect`` applies the same rule itself) streams it through
:func:`continue_timed_out_calls`, which sends the same session
:func:`kiro_crew.timed_out_call.build_timed_out_continuation` and goes on with the
continuation's events. The dashboard and a sub-agent run queue
the same prompt as their own tool-stall recovery instead.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Callable
from typing import Any

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    STOP_REASON_TOOL_STALL,
    STOP_RECOVERY_MAX_RETRIES,
    TurnUsage,
)
from kiro_crew.timed_out_call import (
    build_timed_out_continuation,
    evidence_int,
    is_silence_timeout,
)


def timed_out_continuation(event: Any) -> str:
    """The continuation for *event* when it ended a turn on a silence timeout, else "".

    *event* is a turn's completion. Only a tool stall whose evidence says the call's
    own silence timer ran out qualifies; an oracle verdict (a dead shell child, a
    tool stuck on input, an opaque call past its window) keeps each surface's own
    tool-stall handling.
    """
    if str(getattr(event, "stop_reason", "") or "") != STOP_REASON_TOOL_STALL:
        return ""
    evidence = str(getattr(event, "text", "") or "")
    if not is_silence_timeout(evidence):
        return ""
    return build_timed_out_continuation(
        str(getattr(event, "title", "") or ""), evidence_int(evidence, "idle_secs")
    )


def _add_usage(left: TurnUsage | None, right: object) -> TurnUsage | None:
    """*left* plus *right*, field by field; *left* when *right* is no ``TurnUsage``."""
    if not isinstance(right, TurnUsage):
        return left
    if left is None:
        return right
    return TurnUsage(
        **{
            f.name: getattr(left, f.name) + getattr(right, f.name)
            for f in dataclasses.fields(TurnUsage)
        }
    )


def _with_usage(event: Any, usage: TurnUsage) -> Any:
    """*event* carrying *usage*; *event* unchanged when it cannot carry one."""
    if dataclasses.is_dataclass(event) and not isinstance(event, type):
        try:
            return dataclasses.replace(event, usage=usage)
        except (TypeError, ValueError):
            return event
    return event


async def continue_timed_out_calls(
    open_stream: Callable[[str], AsyncIterator[Any]],
    message: str,
    *,
    budget: int = STOP_RECOVERY_MAX_RETRIES,
) -> AsyncIterator[Any]:
    """Yield one turn's events, sending the timed-out continuation inside the turn.

    *open_stream* sends one prompt on the session and yields its events (a
    provider's ``stream``). When a prompt ends because a call's silence timer ran
    out, its completion is held back, the rest of that prompt's stream is read to
    its end and dropped, and the same session is sent :func:`timed_out_continuation`
    for it, at most *budget* times per turn (``STOP_RECOVERY_MAX_RETRIES``, the
    budget every other tool-stall recovery spends). The continuation's events
    follow, so the consumer sees one turn the model finished after the timeout,
    and the completion it ends on carries the usage of every prompt the turn sent.
    Any other completion, and one past the budget, is yielded as it came.
    """
    prompt = message
    used = 0
    carried: TurnUsage | None = None
    while True:
        follow_up = ""
        async for event in open_stream(prompt):
            if follow_up:
                continue
            if getattr(event, "kind", "") == EVENT_COMPLETE:
                if used < budget:
                    follow_up = timed_out_continuation(event)
                if follow_up:
                    carried = _add_usage(carried, getattr(event, "usage", None))
                    continue
                total = _add_usage(carried, getattr(event, "usage", None))
                if carried is not None and total is not None:
                    event = _with_usage(event, total)
            yield event
        if not follow_up:
            return
        used += 1
        prompt = follow_up
