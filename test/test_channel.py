"""Tests for kiro_crew.channel — data models and ChannelManager."""

from __future__ import annotations

import os

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
    async def test_a_reply_whose_parent_vanished_is_not_stamped_with_a_dead_thread_id(self):
        """A reply carrying a thread id no reader can resolve is invisible in the dashboard.

        The parent lookup shares the append's lock, so a clear-all can wipe `_msg_index`
        between a reply being accepted and its parent being read. The transcript view filters
        out every message that carries a thread id, and a thread can only be opened from a
        parent that still exists, so keeping the dead id persists the reply -- and the agent's
        answer to it -- where neither view can ever show it.
        """
        ch, orch, spec = self._make_channel_with_agents()
        parent = await ch.post(orch.id, "initial", from_role="Orch")
        # What a concurrent clear-all leaves behind: the parent is gone from the index.
        ch._msg_index.pop(parent.id, None)
        ch.messages.remove(parent)

        reply = await ch.post("human", "reply", from_role="Human", thread_id=parent.id)

        assert reply is not None, "precondition: the post itself must still be accepted"
        assert reply.thread_id is None, (
            "the reply kept a thread id whose parent is gone, so the transcript filters it "
            f"out and no thread pane can reach it; got {reply.thread_id!r}"
        )
        assert reply in ch.messages, "and it must still be in the log, just at top level"

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
    @pytest.fixture(autouse=True)
    def _channels_dir(self, tmp_path):
        self._dir = str(tmp_path / "channels")
        (tmp_path / "channels").mkdir()

    def test_create(self):
        mgr = ChannelManager(channels_dir=self._dir)
        ch = mgr.create("test topic")
        assert ch is not None
        assert ch.topic == "test topic"
        assert mgr.count == 1

    def test_create_capacity(self):
        mgr = ChannelManager(channels_dir=self._dir, max_channels=2)
        assert mgr.create("a") is not None
        assert mgr.create("b") is not None
        assert mgr.create("c") is None

    def test_get(self):
        mgr = ChannelManager(channels_dir=self._dir)
        ch = mgr.create("t")
        assert mgr.get(ch.id) is ch
        assert mgr.get("nope") is None

    def test_close(self):
        mgr = ChannelManager(channels_dir=self._dir)
        ch = mgr.create("t")
        agent = ch.add_agent(role="A", agent_name="a", task="t")
        assert mgr.close(ch.id)
        assert agent.state == "done"
        assert mgr.count == 0
        assert not mgr.close(ch.id)

    def test_list_channels(self):
        mgr = ChannelManager(channels_dir=self._dir, max_channels=2)
        mgr.create("a")
        mgr.create("b")
        assert len(mgr.list_channels()) == 2


class TestAClosedChannelIsNotResurrected:
    """`close` unlinks the channel's file, so nothing may persist it afterwards.

    A writer resolves the channel before taking `_log_lock`, so a close can pop it and delete
    its file in between. Writing the detached object back recreates the file, and `_load_all`
    restores at boot a channel the user deleted, holding whatever state the writer left.
    """

    @pytest.fixture(autouse=True)
    def _channels_dir(self, tmp_path):
        self._dir = str(tmp_path / "channels")
        (tmp_path / "channels").mkdir()

    def _file(self, channel_id: str) -> str:
        return os.path.join(self._dir, f"{channel_id}.json")

    @pytest.mark.asyncio
    async def test_a_post_parked_on_the_lock_does_not_recreate_a_closed_channel(self):
        mgr = ChannelManager(channels_dir=self._dir)
        ch = mgr.create("t")
        ch.add_agent(role="A", agent_name="a", task="t")
        assert os.path.exists(self._file(ch.id)), "precondition: the channel was persisted"

        assert mgr.close(ch.id)
        assert not os.path.exists(self._file(ch.id)), "precondition: close unlinked the file"

        msg = await ch.post("human", "landed after the close")

        assert msg is None, "a post into a closed channel must not be accepted"
        assert not os.path.exists(self._file(ch.id)), (
            "the post recreated the deleted channel file, so `_load_all` restores a channel "
            "the user closed"
        )

    def test_a_save_on_a_detached_channel_is_skipped(self):
        """The catch-all: every `_save()` caller, not only the ones with their own guard."""
        mgr = ChannelManager(channels_dir=self._dir)
        ch = mgr.create("t")
        assert mgr.close(ch.id)
        assert not os.path.exists(self._file(ch.id)), "precondition: close unlinked the file"

        # What `api_channel_update_agent` and `api_channel_approve_agent` reach: a mutation on
        # a channel already popped, followed by its own `_save()`.
        ch.topic = "mutated after the close"
        ch._save()

        assert not os.path.exists(
            self._file(ch.id)
        ), "a detached channel persisted itself, so the close is undone on the next boot"
