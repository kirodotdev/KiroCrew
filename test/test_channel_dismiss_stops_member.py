"""Dismissing a working channel member stops its loop.

The rest of the channel model is pinned in ``test_channel.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.channel import Channel


@pytest.mark.asyncio
async def test_dismissing_a_working_member_stops_its_loop_and_keeps_it_done() -> None:
    ch = Channel(id="ch1", topic="test", _broadcast_fn=lambda _t, _d: None)
    agent = ch.add_agent(role="X", agent_name="a", task="t")
    assert agent is not None
    turn_finished = asyncio.Event()
    released: list[str] = []

    async def _loop() -> None:
        # The shape of run_channel_agent: a turn, then "listening", with the
        # session released in a finally.
        try:
            agent.state = "working"
            await turn_finished.wait()
            agent.state = "listening"
        finally:
            if agent.state not in ("done", "failed"):
                agent.state = "done"
            released.append(agent.session_key)

    agent._task = asyncio.create_task(_loop())
    await asyncio.sleep(0)

    assert ch.remove_agent(agent.id)
    turn_finished.set()
    await asyncio.gather(agent._task, return_exceptions=True)

    assert agent._task.cancelled()
    assert agent.state == "done"
    assert released == [agent.session_key]


def test_dismissing_a_member_with_no_running_loop_still_works() -> None:
    ch = Channel(id="ch1", topic="test", _broadcast_fn=lambda _t, _d: None)
    agent = ch.add_agent(role="X", agent_name="a", task="t")

    assert ch.remove_agent(agent.id)
    assert agent.state == "done"
