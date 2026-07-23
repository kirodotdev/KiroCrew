"""Regression tests for Channel.subscribe() bounded inbox waits.

An agent blocked on inbox.get() with no timeout would park forever if no
message ever arrives (sender died, channel closed, shutdown), leaking the
task and preventing clean shutdown. subscribe() must re-check the agent's
stop condition on a bounded interval so it can always exit.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.channel import Channel, ChannelAgent, ChannelMessage


def _make_channel_with_agent(state: str = "working") -> tuple[Channel, ChannelAgent]:
    ch = Channel(id="c1", topic="t")
    agent = ChannelAgent(id="a1", role="worker", agent_name="w", task="do")
    agent.state = state
    ch.members["a1"] = agent
    return ch, agent


@pytest.mark.asyncio
async def test_subscribe_exits_when_agent_becomes_done(monkeypatch) -> None:
    """Blocked subscribe() must exit once the agent transitions to done."""
    # Speed up the poll so the test is fast but still exercises the timeout loop.
    monkeypatch.setattr("kiro_crew.channel._INBOX_POLL_SECS", 0.05)
    ch, agent = _make_channel_with_agent("working")

    async def consume() -> None:
        async for _ in ch.subscribe("a1"):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)  # generator is now parked on the bounded get()
    assert not task.done()

    agent.state = "done"
    # Must exit within a few poll intervals; would hang forever before the fix.
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_subscribe_still_yields_messages(monkeypatch) -> None:
    """The timeout loop must not drop delivered messages."""
    monkeypatch.setattr("kiro_crew.channel._INBOX_POLL_SECS", 0.05)
    ch, agent = _make_channel_with_agent("working")
    gen = ch.subscribe("a1")

    msg = ChannelMessage(id="m1", from_id="human", from_role="Human", content="hi")
    await agent.inbox.put(msg)

    got = await asyncio.wait_for(gen.__anext__(), timeout=5)
    assert got is msg
    await gen.aclose()
