"""Tests for kiro_crew.channel — data models and ChannelManager."""

from __future__ import annotations

import tempfile

import pytest

from kiro_crew.channel import (
    _MAX_A2A_EXCHANGES,
    ApprovalPolicy,
    Channel,
    ChannelAgent,
    ChannelManager,
    ChannelMessage,
)


class TestChannelMessage:
    def test_to_dict(self):
        msg = ChannelMessage(
            id="abc",
            from_id="human",
            from_role="Human",
            content="hello",
            mention="agent1",
            msg_type="broadcast",
        )
        d = msg.to_dict()
        assert d["id"] == "abc"
        assert d["from_id"] == "human"
        assert d["mention"] == "agent1"
        assert d["msg_type"] == "broadcast"

    def test_defaults(self):
        msg = ChannelMessage(id="x", from_id="a", from_role="A", content="hi")
        assert msg.mention is None
        assert msg.msg_type == "progress"
        assert msg.timestamp > 0


class TestChannelAgent:
    def test_defaults(self):
        agent = ChannelAgent(id="a1", role="Tester", agent_name="test-agent", task="do stuff")
        assert agent.state == "pending"
        assert agent.is_orchestrator is False
        assert agent.approval_policy == ApprovalPolicy.WRITES

    def test_to_dict(self):
        agent = ChannelAgent(
            id="a1",
            role="Orchestrator",
            agent_name="kirocrew",
            task="coordinate",
            is_orchestrator=True,
            approval_policy=ApprovalPolicy.TRUSTED,
        )
        d = agent.to_dict()
        assert d["is_orchestrator"] is True
        assert d["approval_policy"] == "trusted"


class TestChannel:
    def _make_channel(self, events=None):
        captured = events if events is not None else []
        ch = Channel(id="ch1", topic="test", _broadcast_fn=lambda t, d: captured.append((t, d)))
        return ch, captured

    def test_add_agent(self):
        ch, events = self._make_channel()
        agent = ch.add_agent(role="Logs", agent_name="logs-agent", task="search logs")
        assert agent is not None
        assert agent.id in ch.members
        assert events[-1][0] == "channel_agent_joined"

    def test_add_agent_capacity(self):
        ch, _ = self._make_channel()
        for i in range(3):
            assert ch.add_agent(role=f"Agent{i}", agent_name="a", task="t") is not None
        assert ch.add_agent(role="Extra", agent_name="a", task="t") is None

    def test_remove_agent(self):
        ch, events = self._make_channel()
        agent = ch.add_agent(role="X", agent_name="a", task="t")
        assert ch.remove_agent(agent.id)
        assert agent.id not in ch.members
        assert agent.state == "done"
        assert events[-1][0] == "channel_agent_left"

    def test_remove_nonexistent(self):
        ch, _ = self._make_channel()
        assert not ch.remove_agent("nope")

    def test_to_dict(self):
        ch, _ = self._make_channel()
        ch.add_agent(role="A", agent_name="a", task="t")
        d = ch.to_dict()
        assert d["id"] == "ch1"
        assert d["topic"] == "test"
        assert len(d["members"]) == 1


class TestChannelRouting:
    """Test orchestrator-centric routing."""

    def _make_channel_with_agents(self):
        ch = Channel(id="ch1", topic="test", _broadcast_fn=lambda t, d: None)
        orch = ch.add_agent(role="Orchestrator", agent_name="m", task="coord", is_orchestrator=True)
        orch.state = "listening"
        ch.orchestrator_id = orch.id
        spec = ch.add_agent(role="Specialist", agent_name="s", task="work")
        spec.state = "listening"
        return ch, orch, spec

    @pytest.mark.asyncio
    async def test_human_no_mention_reaches_orchestrator_only(self):
        ch, orch, spec = self._make_channel_with_agents()
        await ch.post("human", "check everything", from_role="Human")
        assert not orch.inbox.empty()
        assert spec.inbox.empty()  # specialists need @mention

    @pytest.mark.asyncio
    async def test_human_mention_reaches_target(self):
        ch, orch, spec = self._make_channel_with_agents()
        await ch.post("human", "check logs", from_role="Human", mention=spec.id)
        assert not spec.inbox.empty()
        assert orch.inbox.empty()

    @pytest.mark.asyncio
    async def test_agent_mention_reaches_target(self):
        ch, orch, spec = self._make_channel_with_agents()
        await ch.post(orch.id, "check logs", from_role="Orchestrator", mention=spec.id)
        assert not spec.inbox.empty()
        assert orch.inbox.empty()  # sender skipped

    @pytest.mark.asyncio
    async def test_multi_mention(self):
        ch, orch, spec = self._make_channel_with_agents()
        await ch.post("human", "both of you", from_role="Human", mention=[orch.id, spec.id])
        assert not orch.inbox.empty()
        assert not spec.inbox.empty()

    @pytest.mark.asyncio
    async def test_self_mention_filtered(self):
        ch, orch, spec = self._make_channel_with_agents()
        await ch.post(orch.id, "talking to myself", from_role="Orch", mention=orch.id)
        assert orch.inbox.empty()  # self-mention discarded

    @pytest.mark.asyncio
    async def test_a2a_exchange_limit(self):
        ch, orch, spec = self._make_channel_with_agents()
        for _ in range(_MAX_A2A_EXCHANGES):
            await ch.post(orch.id, "msg", from_role="Orch", mention=spec.id)
        while not spec.inbox.empty():
            spec.inbox.get_nowait()
        await ch.post(orch.id, "one more", from_role="Orch", mention=spec.id)
        assert spec.inbox.empty()  # blocked by A2A limit

    @pytest.mark.asyncio
    async def test_done_agents_skipped(self):
        ch, orch, spec = self._make_channel_with_agents()
        spec.state = "done"
        await ch.post("human", "hello", from_role="Human", mention=spec.id)
        assert spec.inbox.empty()

    @pytest.mark.asyncio
    async def test_message_stored(self):
        ch, orch, spec = self._make_channel_with_agents()
        await ch.post("human", "test msg", from_role="Human")
        assert len(ch.messages) == 1
        assert ch.messages[0].content == "test msg"

    @pytest.mark.asyncio
    async def test_broadcast_event_always_sent(self):
        events = []
        ch = Channel(id="ch1", topic="t", _broadcast_fn=lambda t, d: events.append((t, d)))
        agent = ch.add_agent(role="A", agent_name="a", task="t")
        agent.state = "listening"
        ch.orchestrator_id = agent.id
        agent.is_orchestrator = True
        events.clear()
        await ch.post("human", "hi", from_role="Human")
        assert any(e[0] == "channel_message" for e in events)

    @pytest.mark.asyncio
    async def test_thread_routing_to_parent_sender(self):
        ch, orch, spec = self._make_channel_with_agents()
        # Orch posts a message
        msg = await ch.post(orch.id, "initial", from_role="Orch")
        # Human replies in thread without @mention — should go to orch (parent sender)
        await ch.post("human", "reply", from_role="Human", thread_id=msg.id)
        assert not orch.inbox.empty()

    @pytest.mark.asyncio
    async def test_human_message_resets_exchange_counts(self):
        ch, orch, spec = self._make_channel_with_agents()
        # Exhaust the A2A budget
        for _ in range(_MAX_A2A_EXCHANGES):
            await ch.post(orch.id, "msg", from_role="Orch", mention=spec.id)
        while not spec.inbox.empty():
            spec.inbox.get_nowait()
        # Confirm blocked
        await ch.post(orch.id, "blocked", from_role="Orch", mention=spec.id)
        assert spec.inbox.empty()
        # Human message resets the budget
        await ch.post("human", "new direction", from_role="Human")
        while not orch.inbox.empty():
            orch.inbox.get_nowait()
        # Now A2A should work again
        await ch.post(orch.id, "unblocked", from_role="Orch", mention=spec.id)
        assert not spec.inbox.empty()

    @pytest.mark.asyncio
    async def test_configurable_max_exchanges(self):
        ch = Channel(id="ch1", topic="test", max_exchanges=5, _broadcast_fn=lambda t, d: None)
        orch = ch.add_agent(role="Orchestrator", agent_name="m", task="coord", is_orchestrator=True)
        orch.state = "listening"
        ch.orchestrator_id = orch.id
        spec = ch.add_agent(role="Specialist", agent_name="s", task="work")
        spec.state = "listening"
        # Should allow 5 exchanges (not default 3)
        for _ in range(5):
            await ch.post(orch.id, "msg", from_role="Orch", mention=spec.id)
        assert not spec.inbox.empty()  # all 5 delivered
        while not spec.inbox.empty():
            spec.inbox.get_nowait()
        # 6th should be blocked
        await ch.post(orch.id, "too many", from_role="Orch", mention=spec.id)
        assert spec.inbox.empty()


class TestChannelPersistence:
    def test_serialize_deserialize(self):
        ch = Channel(id="ch1", topic="test")
        ch.add_agent(role="A", agent_name="a", task="t", is_orchestrator=True)
        data = ch.serialize()
        restored = Channel.deserialize(data)
        assert restored.id == "ch1"
        assert restored.topic == "test"
        assert len(restored.members) == 1
        agent = list(restored.members.values())[0]
        assert agent.state == "done"  # always restored as done
        assert agent.is_orchestrator is True


class TestChannelManager:
    def test_create(self):
        mgr = ChannelManager(channels_dir=tempfile.mkdtemp())
        ch = mgr.create("test topic")
        assert ch is not None
        assert ch.topic == "test topic"
        assert mgr.count == 1

    def test_create_capacity(self):
        mgr = ChannelManager(channels_dir=tempfile.mkdtemp(), max_channels=2)
        assert mgr.create("a") is not None
        assert mgr.create("b") is not None
        assert mgr.create("c") is None

    def test_get(self):
        mgr = ChannelManager(channels_dir=tempfile.mkdtemp())
        ch = mgr.create("t")
        assert mgr.get(ch.id) is ch
        assert mgr.get("nope") is None

    def test_close(self):
        mgr = ChannelManager(channels_dir=tempfile.mkdtemp())
        ch = mgr.create("t")
        agent = ch.add_agent(role="A", agent_name="a", task="t")
        assert mgr.close(ch.id)
        assert agent.state == "done"
        assert mgr.count == 0
        assert not mgr.close(ch.id)

    def test_list_channels(self):
        mgr = ChannelManager(channels_dir=tempfile.mkdtemp(), max_channels=2)
        mgr.create("a")
        mgr.create("b")
        assert len(mgr.list_channels()) == 2
