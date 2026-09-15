"""Tests for the (always-on) queue-during-subagents behavior.

Covers the drain-filter primitive (_dequeue_next_system_message) that keeps a
tangential user message queued while background sub-agents run, the api_chat
ingest gate (unconditional: queues whenever sub-agents run for the slot), and
the board's subagents_running slot annotation. There is no config toggle —
steering is the effective opt-out.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat_delivery import queue_for_next_turn
from kiro_crew.dashboard.chat_runner import _start_next_queued_turn
from kiro_crew.dashboard.chat_utils import (
    CRON_NOTIFICATION_KIND,
    SUBAGENT_COMPLETION_KIND,
    _dequeue_next_system_message,
)
from kiro_crew.dashboard.state import (
    CRON_NOTIFY_PREFIX,
    SUBAGENT_COMPLETION_PREFIX,
    _ChatSlot,
)

# ── Unit tests: _dequeue_next_system_message ──


class TestDequeueNextSystemMessage:
    """The helper drains system injections while keeping plain user messages queued."""

    def test_only_user_messages_holds_all(self):
        """With only user messages queued, nothing drains and the queue is intact."""
        slot = _ChatSlot("s1")
        slot._queue = [{"id": "a", "content": "keep working"}, {"id": "b", "content": "and this too"}]

        next_msg, consumed = _dequeue_next_system_message(slot)

        assert next_msg is None
        assert consumed == []
        assert [q["content"] for q in slot._queue] == ["keep working", "and this too"]

    def test_empty_queue(self):
        """Empty queue drains nothing."""
        slot = _ChatSlot("s1")
        slot._queue = []

        next_msg, consumed = _dequeue_next_system_message(slot)

        assert next_msg is None
        assert consumed == []

    def test_drains_subagent_completion_holds_user(self):
        """A queued sub-agent completion drains; a leading user message stays queued."""
        sa = f"{SUBAGENT_COMPLETION_PREFIX}\nAgent `a1` completed \u2705\nResult"
        slot = _ChatSlot("s1")
        slot._queue = [{"id": "a", "content": "tangential question"}, {"id": "b", "content": sa, "kind": SUBAGENT_COMPLETION_KIND}]

        next_msg, consumed = _dequeue_next_system_message(slot)

        assert next_msg == sa
        assert [c["content"] for c in consumed] == [sa]
        # The user message stays queued.
        assert [q["content"] for q in slot._queue] == ["tangential question"]

    def test_drains_cron_holds_user(self):
        """A queued cron notification drains; user messages stay queued."""
        cron = f"{CRON_NOTIFY_PREFIX}daily]: run report"
        slot = _ChatSlot("s1")
        slot._queue = [{"id": "a", "content": "hi there"}, {"id": "b", "content": cron, "kind": CRON_NOTIFICATION_KIND}]

        next_msg, consumed = _dequeue_next_system_message(slot)

        assert next_msg == cron
        assert [c["content"] for c in consumed] == [cron]
        assert [q["content"] for q in slot._queue] == ["hi there"]

    def test_subagent_first_drains_first(self):
        """A leading sub-agent completion drains directly."""
        sa = f"{SUBAGENT_COMPLETION_PREFIX}\nAgent `x` completed \u2705\nDone"
        slot = _ChatSlot("s1")
        slot._queue = [{"id": "a", "content": sa, "kind": SUBAGENT_COMPLETION_KIND}, {"id": "b", "content": "user follow-up"}]

        next_msg, consumed = _dequeue_next_system_message(slot)

        assert next_msg == sa
        assert [q["content"] for q in slot._queue] == ["user follow-up"]


class TestDequeueExcludeCron:
    """`exclude_cron=True` holds cron notifications while a multi-stage plan runs.

    Each stage is its own ``_run_chat`` whose tail-drain fires while
    ``_in_stage_execution`` is still set (chat_runner ``_start_next_queued_turn``
    passes ``exclude_cron=in_stage``). A cron queued mid-plan must NOT be pulled
    between stages; sub-agent completions and recovery still flow.
    """

    def test_holds_cron_when_only_cron_queued(self):
        """A lone cron notification is held (drains nothing) under exclude_cron."""
        cron = f"{CRON_NOTIFY_PREFIX}daily]: run report"
        slot = _ChatSlot("s1")
        slot._queue = [{"id": "a", "content": cron, "kind": CRON_NOTIFICATION_KIND}]

        next_msg, consumed = _dequeue_next_system_message(slot, exclude_cron=True)

        assert next_msg is None
        assert consumed == []
        # Cron stays queued for the end-of-plan drain.
        assert [q["content"] for q in slot._queue] == [cron]

    def test_still_drains_subagent_completion(self):
        """A sub-agent completion still flows even with exclude_cron=True."""
        sa = f"{SUBAGENT_COMPLETION_PREFIX}\nAgent `a1` completed ✅\nResult"
        slot = _ChatSlot("s1")
        slot._queue = [{"id": "a", "content": sa, "kind": SUBAGENT_COMPLETION_KIND}]

        next_msg, consumed = _dequeue_next_system_message(slot, exclude_cron=True)

        assert next_msg == sa
        assert slot._queue == []

    def test_skips_cron_drains_later_subagent(self):
        """Cron ahead of a sub-agent completion is skipped; the completion drains."""
        cron = f"{CRON_NOTIFY_PREFIX}daily]: run report"
        sa = f"{SUBAGENT_COMPLETION_PREFIX}\nAgent `a1` completed ✅\nResult"
        slot = _ChatSlot("s1")
        slot._queue = [
            {"id": "a", "content": cron, "kind": CRON_NOTIFICATION_KIND},
            {"id": "b", "content": sa, "kind": SUBAGENT_COMPLETION_KIND},
        ]

        next_msg, consumed = _dequeue_next_system_message(slot, exclude_cron=True)

        assert next_msg == sa
        # The cron is left queued; only the completion was consumed.
        assert [q["content"] for q in slot._queue] == [cron]

    def test_default_still_drains_cron(self):
        """Default (exclude_cron=False) keeps the pre-fix behavior: cron drains."""
        cron = f"{CRON_NOTIFY_PREFIX}daily]: run report"
        slot = _ChatSlot("s1")
        slot._queue = [{"id": "a", "content": cron, "kind": CRON_NOTIFICATION_KIND}]

        next_msg, consumed = _dequeue_next_system_message(slot)

        assert next_msg == cron
        assert slot._queue == []


# ── API test: api_chat ingest gate (idle + sub-agents running) ──


@pytest.mark.asyncio
class TestApiChatSubagentQueueGate:
    """The idle-path ingest gate queues a message whenever sub-agents are
    running for the slot (always on), querying the correct parent key."""

    async def test_queues_when_subagents_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        ran = {"called": False}

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            assert _directive_user_origin is True
            ran["called"] = True

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=[{"id": "a1"}])
        state = _make_state(tmp_path, subagents=subs)
        slot = state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "tangential q", "slot": "s1"})
            assert resp.status == 200
            data = await resp.json()

        assert data.get("queued") is True
        assert ran["called"] is False  # gate returned before starting a turn
        assert slot.queue_depth == 1
        # The gate must query the slot's parent key, not a bare/mismatched one.
        subs.running_agents_for.assert_any_call("dashboard:s1")

    async def test_not_queued_when_no_subagents_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            assert _directive_user_origin is True
            return None

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=[])  # no agents running
        state = _make_state(tmp_path, subagents=subs)
        slot = state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "go on", "slot": "s1"})
            assert resp.status == 200
            data = await resp.json()

        assert data.get("queued") is not True  # not held → normal dispatch
        assert slot.queue_depth == 0


# ── API test: api_chat idle send vs a DEFERRED teardown ──


@pytest.mark.asyncio
class TestApiChatDeferredTeardownGate:
    """An idle send must not overtake a held queue, nor run against a
    conversation awaiting discard."""

    @staticmethod
    def _state(tmp_path, *, discard_lands: bool):
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=[])
        # Left truthy, `subagents_attached` answers True and the teardown defers on
        # THAT branch instead -- a refusal test would never reach the refusal.
        subs._queued_depth = MagicMock(return_value=0)
        state = _make_state(tmp_path, subagents=subs)
        state.sessions.discard_conversation = AsyncMock(return_value=discard_lands)
        return state

    async def test_an_idle_send_queues_behind_a_held_prompt(self, tmp_path, monkeypatch):
        """A refused discard leaves the queue held, so a fresh send joins its BACK.

        Dispatching instead would run this send ahead of the held prompt and
        against the conversation the user asked to discard -- the start-of-turn
        consume does not apply a discard, so the turn would append to it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        ran = {"called": False}

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            ran["called"] = True

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
        state = self._state(tmp_path, discard_lands=False)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "dashboard:s1"
        slot.queue_append("held B")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "new C", "slot": "s1"})
            assert resp.status == 200
            data = await resp.json()

        # The refusal branch actually ran, rather than deferring on attached children.
        state.sessions.discard_conversation.assert_awaited()
        assert data.get("queued") is True
        assert ran["called"] is False  # no turn dispatched past the hold
        assert [q["content"] for q in slot._queue] == ["held B", "new C"]
        assert slot._pending_discard_conversation_key == "dashboard:s1"
        # Derived, not merely left alone: the retry's release requires it SET.
        assert slot._queue_held is True

    async def test_a_still_pending_teardown_marks_the_queue_held(self, tmp_path, monkeypatch):
        """The queued send must be reachable by the retry that lands the teardown.

        An idle channel-linked slot has no turn to set the hold -- a project set
        through ``api_chat_slot_project`` leaves it False -- so a gate that queued
        without deriving the hold left ``_release_and_drain`` short-circuiting on
        ``not slot._queue_held``, parking this entry until the user's next send,
        which then ran ahead of it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._start_next_queued_turn",
            AsyncMock(return_value=True),
        )
        state = self._state(tmp_path, discard_lands=False)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "dashboard:s1"
        # Nothing queued and the hold unset, so only the gate can set it.
        assert slot._queue_held is False

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "only A", "slot": "s1"})
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        assert slot._queue_held is True
        assert [q["content"] for q in slot._queue] == ["only A"]

    async def test_a_queued_send_behind_a_pending_discard_has_an_owned_retry(
        self, tmp_path, monkeypatch
    ):
        """The 200 receipt must be backed by something that will actually drain it.

        A channel-linked slot passes no dashboard turn boundary, so without an owned
        retry the accepted entry parks until the user happens to send again -- the
        receipt promises a delivery nothing is responsible for.
        """
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._state(tmp_path, discard_lands=False)
        slot = state.get_or_create_slot("s1")
        slot.linked_session_key = "slack:123.456"
        slot._pending_discard_conversation_key = "dashboard:s1"
        chat_runner._pending_reset_retries.pop(slot.key, None)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "only A", "slot": "s1"})
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True

        entry = chat_runner._pending_reset_retries.get(slot.key)
        assert entry is not None and entry[0] is slot
        assert not entry[1].done()
        entry[1].cancel()
        try:
            await entry[1]
        except asyncio.CancelledError:
            pass
        chat_runner._pending_reset_retries.pop(slot.key, None)

    async def test_a_landed_teardown_releases_the_held_head_first(self, tmp_path, monkeypatch):
        """Positive control: landing the teardown releases the queue HEAD.

        Without this the gate above could be satisfied by a slot that queues
        forever -- an idle slot has no later turn boundary of its own to land the
        teardown, so refusing to dispatch without releasing would strand both.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        dispatched: list[str] = []

        async def fake_drain(st, sl):
            dispatched.append(sl._queue[0]["content"] if sl._queue else "")
            return True

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._start_next_queued_turn", fake_drain)
        state = self._state(tmp_path, discard_lands=True)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "dashboard:s1"
        slot._queue_held = True
        slot.queue_append("held B")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "new C", "slot": "s1"})
            assert resp.status == 200
            data = await resp.json()

        assert data.get("queued") is True
        assert slot._pending_discard_conversation_key is None
        assert slot._queue_held is False
        # The HELD prompt, not the send that just arrived.
        assert dispatched == ["held B"]

    async def test_an_idle_steer_is_routed_through_the_gate(self, tmp_path, monkeypatch):
        """A steer with no live turn is an ordinary send, and the discard still stands.

        The sibling subagent hold exempts steers to spare them a wait; here the
        exemption would spend the steer on the conversation being discarded instead.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        ran = {"called": False}

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            ran["called"] = True

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
        state = self._state(tmp_path, discard_lands=False)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "dashboard:s1"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1", json={"message": "steered", "slot": "s1", "steer": True}
            )
            assert resp.status == 200
            data = await resp.json()

        state.sessions.discard_conversation.assert_awaited()
        assert data.get("queued") is True
        assert ran["called"] is False
        assert slot._pending_discard_conversation_key == "dashboard:s1"

    async def test_a_landed_teardown_keeps_a_signed_out_hold(self, tmp_path, monkeypatch):
        """The discard landing says nothing about the CLI, which still cannot answer.

        Releasing on it would hand the held prompt to the same auth wall the turn
        already hit, so the hold outlives the cause that this send settled.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        dispatched: list[str] = []

        async def fake_drain(st, sl):
            dispatched.append(sl._queue[0]["content"] if sl._queue else "")
            return True

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._start_next_queued_turn", fake_drain)
        state = self._state(tmp_path, discard_lands=True)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "dashboard:s1"
        slot._queue_held = True
        slot._queue_held_auth = True
        slot.queue_append("held B")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "new C", "slot": "s1"})
            assert resp.status == 200
            data = await resp.json()

        assert data.get("queued") is True
        assert slot._pending_discard_conversation_key is None
        assert slot._queue_held is True
        assert dispatched == []
        assert [q["content"] for q in slot._queue] == ["held B", "new C"]

    async def test_a_broken_remote_binding_is_refused_not_queued(self, tmp_path, monkeypatch):
        """`is_remote` reads False on a broken binding, so keying on it admits one here.

        The 409 downstream exists to stop a named crew's work running on this machine,
        and queueing ahead of it would drain the turn locally instead.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._state(tmp_path, discard_lands=True)
        slot = state.get_or_create_slot("s1")
        slot.executor = "remote"  # no instance_id / remote_slot: binding incomplete
        slot._pending_reset_history_key = "dashboard:s1"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "new C", "slot": "s1"})
            assert resp.status == 409
            data = await resp.json()

        assert data.get("code") == "remote_binding_incomplete"
        assert slot.queue_depth == 0
        assert slot._pending_reset_history_key == "dashboard:s1"

    async def test_a_turn_starting_during_the_await_queues_instead_of_draining(
        self, tmp_path, monkeypatch
    ):
        """A racing send that takes the slot across the await must not be overrun.

        The busy check upstream ran before the await, so its answer is stale by
        the time the teardown lands; draining on it would put two turns on one
        slot and interleave their rows.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        drained: list[str] = []

        async def fake_drain(st, sl):
            drained.append("drained")
            return True

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._start_next_queued_turn", fake_drain)
        state = self._state(tmp_path, discard_lands=True)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "dashboard:s1"
        slot._queue_held = True
        slot.queue_append("held B")

        async def racing_consume(st, sl, *, allow_discard=False):
            """Land the teardown, then take the slot as a concurrent send would."""
            sl._pending_discard_conversation_key = None
            # `running` is derived from the task, so this is the only honest way to
            # stage the race -- a pending task is exactly what a live turn leaves.
            sl.task = asyncio.get_running_loop().create_future()
            return True

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._consume_pending_reset", racing_consume
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "new C", "slot": "s1"})
            assert resp.status == 200
            data = await resp.json()

        assert slot.running is True  # the race was actually staged
        assert data.get("queued") is True
        assert drained == []  # the stale admission must not dispatch
        assert [q["content"] for q in slot._queue] == ["held B", "new C"]
        slot.task.cancel()

    async def test_a_slot_replaced_during_the_await_is_refused_not_queued(
        self, tmp_path, monkeypatch
    ):
        """Queueing onto a retired slot loses the message behind a 200.

        The busy sibling above shares this staleness check but must QUEUE: it still
        owns the key, so that turn's own drain reads the queue this entry joined. A
        replacement retires the object instead, and nothing ever drains it -- the
        client is told ``queued`` and has no failure to retry, so the 409 is the only
        disposition that keeps the send recoverable.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        drained: list[str] = []

        async def fake_drain(st, sl):
            drained.append("drained")
            return True

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._start_next_queued_turn", fake_drain)
        state = self._state(tmp_path, discard_lands=True)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "dashboard:s1"
        slot._queue_held = True
        slot.queue_append("held B")
        replacement = []

        async def replacing_consume(st, sl, *, allow_discard=False):
            """Land the teardown, then delete and recreate the slot under its key."""
            sl._pending_discard_conversation_key = None
            del st._slots[sl.key]
            replacement.append(st.get_or_create_slot("s1"))
            return True

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._consume_pending_reset", replacing_consume
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "new C", "slot": "s1"})
            assert resp.status == 409
            data = await resp.json()

        # The race was actually staged: a different object now holds the key.
        assert replacement and replacement[0] is not slot
        assert state._slots.get("s1") is replacement[0]
        assert data.get("code") == "session_rebound"
        assert drained == []
        # Nothing stranded on the retired slot, and nothing smuggled onto its successor.
        assert [q["content"] for q in slot._queue] == ["held B"]
        assert replacement[0].queue_depth == 0

    async def test_a_racing_turn_with_an_empty_queue_still_queues(self, tmp_path, monkeypatch):
        """With nothing queued, falling through would put a SECOND turn on the slot.

        The teardown landing clears the hold, so only the staleness check stands
        between this send and a dispatch onto a slot another send already owns.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        started: list[str] = []

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            started.append(msg)

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
        state = self._state(tmp_path, discard_lands=True)
        slot = state.get_or_create_slot("s1")
        slot._pending_discard_conversation_key = "dashboard:s1"

        async def racing_consume(st, sl, *, allow_discard=False):
            sl._pending_discard_conversation_key = None
            sl.task = asyncio.get_running_loop().create_future()
            return True

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._consume_pending_reset", racing_consume
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "new C", "slot": "s1"})
            assert resp.status == 200
            data = await resp.json()

        assert slot.running is True
        assert data.get("queued") is True
        assert started == []  # no second turn on a slot already taken
        assert [q["content"] for q in slot._queue] == ["new C"]
        slot.task.cancel()


# ── Unit tests: the drain choke point honours the hold ──


class TestDrainChokePointHonoursHold:
    """`_start_next_queued_turn` asks the hold itself, so no caller can skip it."""

    def test_a_held_plain_prompt_does_not_drain(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        dispatched: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.spawn_guarded_turn",
            lambda *a, **k: dispatched.append("go"),
        )
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        # Enqueued the way production does, so the admission sweep above the gate
        # keeps it: an unstamped plain entry is dropped and never reaches the gate.
        queue_for_next_turn(state, slot, "queued prompt")
        slot._queue_held = True

        assert asyncio.run(_start_next_queued_turn(state, slot)) is False
        assert dispatched == []
        assert [q["content"] for q in slot._queue] == ["queued prompt"]

    def test_a_held_system_entry_is_still_delivered(self, tmp_path, monkeypatch):
        """A hold withholds prompts, not injections: a completion event still lands.

        Gating these too would strand every sub-agent result behind the hold, and
        the completion is what releases the work the user is waiting on.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        dispatched: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.spawn_guarded_turn",
            lambda *a, **k: dispatched.append("go"),
        )
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        sa = f"{SUBAGENT_COMPLETION_PREFIX}\nAgent `a1` completed \u2705\nResult"
        slot._queue_held = True
        slot.queue_append(sa, SUBAGENT_COMPLETION_KIND)

        assert asyncio.run(_start_next_queued_turn(state, slot)) is True
        assert dispatched == ["go"]

    def test_a_held_prompt_does_not_strand_a_later_injection(self, tmp_path, monkeypatch):
        """Selection scans the whole queue, so a held prompt at the HEAD blocks nothing.

        Testing only the head entry would hold the injection behind the prompt
        until the hold cleared, which is the wait the exemption exists to avoid.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        dispatched: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.spawn_guarded_turn",
            lambda *a, **k: dispatched.append("go"),
        )
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        queue_for_next_turn(state, slot, "held prompt")
        sa = f"{SUBAGENT_COMPLETION_PREFIX}\nAgent `a1` completed \u2705\nResult"
        slot.queue_append(sa, SUBAGENT_COMPLETION_KIND)
        slot._queue_held = True

        assert asyncio.run(_start_next_queued_turn(state, slot)) is True
        assert dispatched == ["go"]
        assert [q["content"] for q in slot._queue] == ["held prompt"]


# ── API test: api_chat busy-slot queue branch (receipt honesty) ──


@pytest.mark.asyncio
class TestApiChatBusySlotEmptyMessage:
    """The busy-slot queue branch never answers `queued: true` for a send it
    did not queue: an empty-message send (e.g. attachments only in `meta`)
    gets an honest 400 with a stable code, and nothing is queued or
    broadcast."""

    async def _busy_client(self, tmp_path, monkeypatch):
        """Real state + slot; the slot is made busy via a live, never-done task."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        pushes: list[tuple[str, dict]] = []
        state.broadcast_ws = lambda kind, payload, **kw: pushes.append((kind, payload))
        return state, slot, pushes

    async def test_attachment_only_send_gets_honest_400(self, tmp_path, monkeypatch):
        """Busy slot + empty message + meta attachments → 4xx with stable code,
        nothing appended to the queue, no queue_push broadcast."""
        state, slot, pushes = await self._busy_client(tmp_path, monkeypatch)
        gate = asyncio.Event()

        async with TestClient(TestServer(_make_app(state))) as client:
            slot.task = asyncio.get_running_loop().create_task(gate.wait())
            try:
                assert slot.running is True  # precondition: authentically busy
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={
                        "message": "",
                        "slot": "s1",
                        "meta": {"files": [{"name": "diagram.png"}]},
                    },
                )
                assert resp.status == 400
                data = await resp.json()
            finally:
                gate.set()
                await slot.task

        assert data.get("error") == "message is required"
        assert data.get("code") == "message_required"
        assert data.get("queued") is not True
        assert slot.queue_depth == 0
        assert [p for p in pushes if p[0] == "queue_push"] == []

    async def test_nonempty_message_still_queued(self, tmp_path, monkeypatch):
        """Busy slot + non-empty message → `queued: true`, one queue entry,
        one queue_push broadcast (the receipt implies a real enqueue)."""
        state, slot, pushes = await self._busy_client(tmp_path, monkeypatch)
        gate = asyncio.Event()

        async with TestClient(TestServer(_make_app(state))) as client:
            slot.task = asyncio.get_running_loop().create_task(gate.wait())
            try:
                resp = await client.post(
                    "/api/chat?ws=1", json={"message": "still here", "slot": "s1"}
                )
                assert resp.status == 200
                data = await resp.json()
            finally:
                gate.set()
                await slot.task

        assert data.get("queued") is True
        assert slot.queue_depth == 1
        assert len([p for p in pushes if p[0] == "queue_push"]) == 1


# ── Board annotation: DashboardState.serialize_slots subagents_running ──


@pytest.mark.asyncio
class TestSerializeSlotsSubagentsRunning:
    """serialize_slots() annotates each slot dict with subagents_running so the
    Board shows 'Working' (not 'Your turn') while background sub-agents run.

    Async because get_or_create_slot() can trigger push_slots_update() ->
    _send_ws_all() -> asyncio.ensure_future(), which needs a running loop
    (see precedent)."""

    async def test_flag_true_when_agents_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=[{"id": "a1"}])
        state = _make_state(tmp_path, subagents=subs)
        state.get_or_create_slot("s1")

        slots = state.serialize_slots()

        assert slots, "expected at least one serialized slot"
        assert all(d["subagents_running"] is True for d in slots)
        subs.running_agents_for.assert_any_call("dashboard:s1")

    async def test_flag_false_when_no_agents_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=[])
        state = _make_state(tmp_path, subagents=subs)
        state.get_or_create_slot("s1")

        slots = state.serialize_slots()

        assert slots, "expected at least one serialized slot"
        assert all(d["subagents_running"] is False for d in slots)
