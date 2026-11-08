"""A native child card left open at turn end closes as interrupted unless the
parent turn landed normally.

The turn-end safety net force-closes every still-open native sub-agent card. A
raw ``end_turn`` completion is a normal landing, so the card closes clean. A
cancel, a synthesized completion or a crash cut the child off, so the card must
carry an error rather than read as a successful run.
"""

from __future__ import annotations

import pytest
from turn_harness import Raise, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_SUBAGENT_LIST,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    AcpEvent,
)

# The card text the user sees; pinned literally so a rename cannot hide a change.
_INTERRUPTED = "interrupted (parent turn ended)"

_RUNNING = AcpEvent(
    kind=EVENT_SUBAGENT_LIST,
    subagents=[
        {
            "sessionId": "child-1",
            "role": "general-purpose",
            "initialQuery": "say pong",
            "status": {"type": "working"},
        }
    ],
)


def _done_errors(record) -> list:
    return [frame.payload["error"] for frame in record.frames("subagent_done")]


@pytest.mark.asyncio
async def test_a_turn_that_lands_normally_closes_the_open_card_clean() -> None:
    record = await run_turn(
        TurnScript(
            events=[_RUNNING, AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)]
        )
    )
    assert _done_errors(record) == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion",
    [
        pytest.param(
            AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED), id="cancelled"
        ),
        pytest.param(
            AcpEvent(
                kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN, synthetic_completion=True
            ),
            id="synthetic-end-turn",
        ),
    ],
)
async def test_a_turn_that_completes_abnormally_closes_the_open_card_interrupted(
    completion: AcpEvent,
) -> None:
    record = await run_turn(TurnScript(events=[_RUNNING, completion]))
    assert _done_errors(record) == [_INTERRUPTED]


@pytest.mark.asyncio
async def test_a_turn_that_crashes_before_completing_closes_the_open_card_interrupted() -> None:
    record = await run_turn(TurnScript(events=[_RUNNING, Raise(RuntimeError("backend died"))]))
    assert _done_errors(record) == [_INTERRUPTED]
