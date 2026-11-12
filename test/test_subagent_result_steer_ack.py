"""A sub-agent's result does not carry kiro-cli's ``[STEERING steer-…]`` ack.

A host deny steers its reason into the child's running turn, and kiro-cli has the
model acknowledge it with a ``[STEERING steer-<id>: …]`` line. That line is
protocol, not the run's answer: the parent reads the result from the completion
event and, through ``spawn_status``, from ``result.txt``, so neither may hold it.

The run test drives the real ``SubagentManager`` path with only the ACP session
provider faked (the seam ``test_subagent_stop_reason_consistency`` uses).
"""

from __future__ import annotations

import pytest
from idle_tab_drain_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _idle_tab_drains_head_as_turn,
)
from subagent_stop_reason_helpers import _manager, _mock_sessions, _single_turn, _spawn_and_wait

import kiro_crew.subagent_persistence as sp
from kiro_crew.acp.types import STOP_REASON_END_TURN

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.mark.asyncio
async def test_the_result_the_parent_reads_has_no_steer_ack():
    stream, _ = _single_turn(
        STOP_REASON_END_TURN,
        chunks=(
            "The push was refused, so I stopped.\n",
            "[STEERING steer-83cc1f2a: The notice named the rule, so I did not retry.]",
        ),
    )
    info = await _spawn_and_wait(_manager(_mock_sessions(stream)))

    assert info.outcome == "completed"
    assert info.result == "The push was refused, so I stopped.\n"
    result_file = sp._agent_dir(info.id) / "result.txt"
    assert result_file.read_text(encoding="utf-8") == info.result
