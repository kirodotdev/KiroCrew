"""GatewayOrchestrator subagent wiring.

Covers ``_init_subagents`` and its debounced slot pushes, how ``_subagent_done``
classifies and routes a completion, the synthesis turn the last completion of a
batch arms, and the recovery retrigger for queued subagent failures.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import (
    _make_orchestrator,
    _mock_context_builder,
    _mock_dashboard_state,
    _mock_sessions,
)

# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_subagents
# ═══════════════════════════════════════════════════════════════════════════


class TestInitSubagents:
    """Subagent manager initialization."""

    @pytest.mark.asyncio
    async def test_init_subagents_creates_manager(self):
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm_inst._max_concurrent = 10
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
        assert orch.subagent_mgr is not None

    @pytest.mark.asyncio
    async def test_init_subagents_respects_max_concurrent(self):
        orch = _make_orchestrator()
        orch._cfg.agent.max_subagents = 5
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = None
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm_inst._max_concurrent = 5
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
        assert orch.subagent_mgr._max_concurrent == 5

    def _capture_on_event(self, orch):
        """Run _init_subagents with SubagentManager patched; return the on_event callback."""
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
                return mock_sm.call_args.kwargs["on_event"]

    @pytest.mark.asyncio
    async def test_subagent_spawn_and_done_push_slots_update_debounced(self):
        """subagents_running flips at spawn/done — the on_event handler schedules a
        debounced push_slots_update so slots-stream consumers (composer busy
        affordance, Board working lane) stay live. Multiple events inside the
        debounce window coalesce into one push. Covers the reaper too, since
        _force_reap fires subagent_done through the same on_event path."""
        from kiro_crew.subagent import SubagentInfo

        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        on_event = self._capture_on_event(orch)

        info = SubagentInfo(id="a1", task="t", parent_session_key="dashboard:s1")
        # Batch: two spawns + one done inside the 0.2s window -> one push.
        await on_event("subagent_spawn", info, {})
        await on_event(
            "subagent_spawn", SubagentInfo(id="a2", task="t", parent_session_key="dashboard:s1"), {}
        )
        await on_event("subagent_done", info, {"elapsed": 1.0})
        assert orch.dashboard_state.push_slots_update.call_count == 0  # debounced, not yet flushed
        # flake-ok: pre-existing sleep past the 0.2 s debounce window, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing debounce barrier, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.3)
        assert orch.dashboard_state.push_slots_update.call_count == 1

        # A later lifecycle event schedules a fresh push.
        await on_event(
            "subagent_done",
            SubagentInfo(id="a2", task="t", parent_session_key="dashboard:s1"),
            {"elapsed": 1.0},
        )
        # flake-ok: pre-existing sleep past the 0.2 s debounce window, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing debounce barrier, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.3)
        assert orch.dashboard_state.push_slots_update.call_count == 2

    @pytest.mark.asyncio
    async def test_subagent_queued_pushes_slots_debounced(self):
        """A queued-depth frame changes slots[].subagents_queued, so it schedules
        the same debounced push: the next slots frame reconciles a client that
        missed the depth frame itself, including one not showing that session."""
        from kiro_crew.subagent import SubagentInfo

        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        on_event = self._capture_on_event(orch)

        info = SubagentInfo(id="_queue", task="", parent_session_key="dashboard:s1")
        await on_event("subagent_queued", info, {"queued": 1})
        await on_event("subagent_queued", info, {"queued": 0})
        assert orch.dashboard_state.push_slots_update.call_count == 0  # debounced
        # flake-ok: pre-existing sleep past the 0.2 s debounce window, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing debounce barrier, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.3)
        assert orch.dashboard_state.push_slots_update.call_count == 1

    @pytest.mark.asyncio
    async def test_queued_frame_carries_its_tabs_sum_across_parents(self):
        """Two parents that route to one cron tab: each frame carries the tab's
        sum, which is what serialize_slots reports, so a frame and the next slots
        push agree -- including after one of the parents publishes 0."""
        from kiro_crew.dashboard.state import _published_queued_by_slot
        from kiro_crew.subagent import SubagentInfo

        def _slot(key: str) -> str:
            return "cron-job7" if key.startswith("cron:job7") else key.removeprefix("dashboard:")

        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        table: dict[str, int] = {}
        with (
            patch("kiro_crew.slack.gateway.subagent_event_slot", _slot),
            patch("kiro_crew.dashboard.chat_utils.subagent_event_slot", _slot),
        ):
            on_event = self._capture_on_event(orch)
            orch.subagent_mgr.published_queued_depths = lambda: dict(table)

            async def publish(parent: str, depth: int) -> int:
                # The manager records before on_event runs (_fire_event).
                table.pop(parent, None)
                if depth:
                    table[parent] = depth
                info = SubagentInfo(id="_queue", task="", parent_session_key=parent)
                await on_event("subagent_queued", info, {"queued": depth})
                frame = orch.dashboard_state.broadcast_ws.call_args.args[1]
                assert frame["slot"] == "cron-job7"
                assert frame["queued"] == _published_queued_by_slot(orch.subagent_mgr).get(
                    "cron-job7", 0
                )
                return frame["queued"]

            assert await publish("cron:job7:run1", 2) == 2
            assert await publish("cron:job7:writer", 1) == 3
            assert await publish("cron:job7:run1", 0) == 1
            assert await publish("cron:job7:writer", 0) == 0

    @pytest.mark.asyncio
    async def test_subagent_tool_event_does_not_push_slots(self):
        """High-frequency subagent_tool events must NOT trigger slots pushes —
        only spawn/done flip the subagents_running truth value (and a queued
        frame moves subagents_queued)."""
        from kiro_crew.subagent import SubagentInfo

        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        on_event = self._capture_on_event(orch)

        info = SubagentInfo(id="a1", task="t", parent_session_key="dashboard:s1")
        await on_event("subagent_tool", info, {"tool": "grep"})
        # flake-ok: pre-existing sleep past the 0.2 s debounce window, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing debounce barrier, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.3)
        assert orch.dashboard_state.push_slots_update.call_count == 0


class TestSubagentDoneStoppedClassification:
    """A user-stopped subagent (error-free record) must never be classified as
    a successful completion by _subagent_done in the announce text."""

    def _capture_on_done(self, orch):
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
                return mock_sm.call_args.kwargs["on_done"]

    def _stopped_info(self):
        from kiro_crew.subagent import SubagentInfo

        info = SubagentInfo(
            id="stop1",
            task="long research task",
            parent_session_key="dashboard:gone",
        )
        info.done = True
        info.user_stopped = True
        info.error = None
        info.result = "partial notes so far"
        return info

    @pytest.mark.asyncio
    async def test_stopped_agent_notification_says_stopped_not_completed(self):
        """Slot-gone path: title carries ⏹ and body frames a stop with partial
        output — never 'completed ✅'."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        orch.dashboard_state.get_slot = MagicMock(return_value=None)  # slot gone
        on_done = self._capture_on_done(orch)

        await on_done(self._stopped_info())

        orch.dashboard_state.notify.assert_called_once()
        args = orch.dashboard_state.notify.call_args
        title, body = args.args[1], args.args[2]
        assert "⏹" in title
        assert "✅" not in title
        assert "Stopped by the user" in body
        assert "partial notes so far" in body

    @pytest.mark.asyncio
    async def test_a_parent_end_stop_is_not_announced_as_the_users(self):
        """The status line and the detail sentence both read the record's own
        stop origin: a run a parent end cancelled is announced as that, never as
        a Stop the user pressed."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.dashboard_state = _mock_dashboard_state()
        orch.dashboard_state.get_slot = MagicMock(return_value=None)  # slot gone
        on_done = self._capture_on_done(orch)
        info = self._stopped_info()
        info._stop_origin = "parent conversation ended (retire_kiro_identity_sessions)"

        await on_done(info)

        orch.dashboard_state.notify.assert_called_once()
        title, body = orch.dashboard_state.notify.call_args.args[1:3]
        assert "⏹" in title
        assert "parent conversation ended (retire_kiro_identity_sessions)" in body
        assert "Stopped by the user" not in body
        assert "stopped by user" not in body
        assert "partial notes so far" in body


class TestSubagentFinalSummaryDirective:
    """Fix 2 (B1): the LAST sub-agent completion ARMS a one-shot synthesis turn
    (slot._pending_synthesis) in chat mode; earlier completions do not."""

    def _capture_on_done(self, orch):
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
                return mock_sm.call_args.kwargs["on_done"]

    async def _done_slot(
        self,
        running_agents_for_return,
        queued: int = 0,
        in_memory: bool = False,
        probe_error: bool = False,
    ):
        """Fire the on_done callback through a dashboard slot and return the slot
        so the caller can inspect _pending_synthesis.

        *queued* is the parent's store count of children the spawn gate still
        holds; the arm must never read it (the fire gate does). *in_memory* is
        the manager's in-memory pending work for the parent."""
        from kiro_crew.subagent import SubagentInfo

        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        ds = _mock_dashboard_state()
        slot = MagicMock()
        slot.running = False
        slot.key = "s1"
        slot.task = None
        slot._pending_synthesis = False  # explicit start (not a MagicMock auto-attr)
        slot._subagent_deliveries_inflight = 0  # real int so the gateway counter works
        slot._subagents_inline_collected = set()
        ds.get_slot = MagicMock(return_value=slot)
        orch.dashboard_state = ds
        on_done = self._capture_on_done(orch)
        orch.subagent_mgr.running_agents_for = MagicMock(return_value=running_agents_for_return)
        orch.subagent_mgr.queued_count_for_async = AsyncMock(return_value=queued)
        orch.subagent_mgr.has_in_memory_pending_work_for = (
            MagicMock(side_effect=RuntimeError("probe gone"))
            if probe_error
            else MagicMock(return_value=in_memory)
        )
        self.mgr = orch.subagent_mgr

        info = SubagentInfo(id="a1", task="do X", parent_session_key="dashboard:s1")
        with patch("kiro_crew.slack.gateway._run_chat", new=AsyncMock()):
            await on_done(info)
            # flake-ok: pre-existing sleep barrier for the injection task, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.05)  # let the injection task settle
        return slot

    @pytest.mark.asyncio
    async def test_last_completion_arms_synthesis(self):
        """No sub-agents left running → the slot is armed for a synthesis turn."""
        slot = await self._done_slot([])
        assert slot._pending_synthesis is True
        # Delivery counter must be balanced back to 0 (no leak → gate not stuck).
        assert slot._subagent_deliveries_inflight == 0

    @pytest.mark.asyncio
    async def test_pending_completion_does_not_arm(self):
        """Another sub-agent still running → synthesis is not armed yet."""
        slot = await self._done_slot([{"id": "a2"}])
        assert slot._pending_synthesis is False

    @pytest.mark.asyncio
    async def test_the_arm_never_reads_the_task_store(self):
        """A sibling only the store holds is the FIRE gate's to see: the arm sits
        on the delivery path and stays in memory, so it does not wait on the
        store's writer (and cannot be overtaken mid-await by a closed tab or a
        sibling registering)."""
        slot = await self._done_slot([], queued=1)
        assert slot._pending_synthesis is True
        self.mgr.queued_count_for_async.assert_not_awaited()
        assert slot._subagent_deliveries_inflight == 0

    @pytest.mark.asyncio
    async def test_in_memory_pending_work_keeps_synthesis_disarmed(self):
        """A sibling in the dispatch window, one whose report still waits on its
        teardown, or a live follow-up watcher: not armed. The finishing child's
        own live task is excluded, or it would always block itself."""
        slot = await self._done_slot([], in_memory=True)
        assert slot._pending_synthesis is False
        self.mgr.has_in_memory_pending_work_for.assert_called_once_with(
            "dashboard:s1", exclude_id="a1"
        )

    @pytest.mark.asyncio
    async def test_a_failing_in_memory_probe_keeps_synthesis_disarmed(self):
        """A probe that raises is unknown pending work, not none."""
        assert (await self._done_slot([]))._pending_synthesis is True  # control
        slot = await self._done_slot([], probe_error=True)
        assert slot._pending_synthesis is False
        assert slot._subagent_deliveries_inflight == 0


class TestSynthesisAfterAOneTurnBatch:
    """The post-fan-out synthesis runs only when the batch's results reached
    the parent in two or more completion turns. Completions go through the
    real ``on_done`` into a real slot, and each completion turn ends through
    ``chat_runner._finish_queue_cycle``, which takes the fire decision."""

    def _wire(self, tmp_path):
        from chat_test_helpers import _make_state

        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        state = _make_state(tmp_path)
        orch.dashboard_state = state
        on_done = TestSubagentFinalSummaryDirective()._capture_on_done(orch)
        mgr = orch.subagent_mgr
        # Every fire-gate probe set: a bare MagicMock probe reads as a child.
        mgr.running_agents_for = MagicMock(return_value=[])
        mgr.has_in_memory_pending_work_for = MagicMock(return_value=False)
        mgr.queued_count_for_async = AsyncMock(return_value=0)
        mgr.queued_count_or_none_async = AsyncMock(return_value=0)
        mgr.published_queued_depths = MagicMock(return_value={})
        state.subagents = mgr
        slot = state.get_or_create_slot("s1")
        slot._titled = True
        return on_done, state, slot, mgr

    @staticmethod
    async def _complete(on_done, slot, agent_id, *, busy=False, **fields):
        """Route one completion; *busy* parks it behind a turn still running."""
        from kiro_crew.subagent import SubagentInfo

        info = SubagentInfo(
            id=agent_id, task=f"do {agent_id}", parent_session_key="dashboard:s1", **fields
        )
        # flake-ok: pre-existing parked stand-in turn the finally cancels, moved verbatim; tracked for a de-flake pass
        blocker = asyncio.create_task(asyncio.sleep(30)) if busy else None
        if blocker is not None:
            slot.task = blocker
        try:
            with (
                patch("kiro_crew.slack.gateway._run_chat", new=AsyncMock()),
                patch("kiro_crew.slack.gateway.INJECTION_TIMEOUT", 0.01),
            ):
                await on_done(info)
                turn = slot.task
                if turn is not None and turn is not blocker:
                    # flake-ok: pre-existing unbounded await of the stub completion turn, moved verbatim; tracked for a de-flake pass
                    await turn  # the completion turn (a stub) runs
        finally:
            if blocker is not None:
                blocker.cancel()
                slot.task = None

    @staticmethod
    async def _turn_end(state, slot) -> bool:
        """End the turn that just ran; True when a synthesis turn was started."""
        from kiro_crew.dashboard import chat_runner

        with (
            patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()) as synth,
            # A completion still queued drains as a stub turn.
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())),
        ):
            await chat_runner._finish_queue_cycle(state, slot)
            # flake-ok: pre-existing event-loop yield after the queue cycle, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)
        return synth.called

    @staticmethod
    async def _user_message_drains(state, slot, *, turn_tail=False, **kwargs):
        """Queue a user message and drain it. *turn_tail* drains it the way a
        turn's end does (``_hand_off_queue``, with the ending turn still holding
        the floor); otherwise the direct drain, as the queued card's Run now."""
        from kiro_crew.dashboard import chat_runner

        qid = slot.queue_append("thanks, carry on")
        if kwargs.get("allow_user_during_subagents"):
            kwargs["required_queue_id"] = qid
        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
            patch.object(chat_runner, "_run_chat", new=MagicMock(return_value=MagicMock())),
        ):
            if not turn_tail:
                assert await chat_runner._start_next_queued_turn(state, slot, **kwargs) is True
                return
            assert not kwargs
            slot.task = ending = asyncio.get_running_loop().create_future()
            try:
                await chat_runner._hand_off_queue(
                    state, slot, drain=True, allow_automatic_successor=True
                )
            finally:
                ending.cancel()
            assert spawn.call_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("busy", [False, True], ids=["launched", "queued"])
    async def test_a_lone_completion_gets_no_synthesis(self, tmp_path, busy):
        on_done, state, slot, _ = self._wire(tmp_path)

        await self._complete(on_done, slot, "a1", busy=busy)
        assert slot._pending_synthesis is True, "the arm itself is unchanged"

        assert await self._turn_end(state, slot) is False
        assert slot._pending_synthesis is False, "the arm is consumed, not left for later"

    @pytest.mark.asyncio
    async def test_a_wave_digest_in_one_turn_gets_no_synthesis(self, tmp_path):
        """Two members of one spawn wave arrive as ONE digest turn."""
        on_done, state, slot, _ = self._wire(tmp_path)

        await self._complete(on_done, slot, "m1", batch_id="w1", batch_total=2)
        assert slot._queue == [] and slot.task is None, "the first member is held"
        await self._complete(on_done, slot, "m2", batch_id="w1", batch_total=2)

        assert await self._turn_end(state, slot) is False

    @pytest.mark.asyncio
    async def test_completions_in_two_turns_still_get_one_synthesis(self, tmp_path):
        on_done, state, slot, mgr = self._wire(tmp_path)

        mgr.running_agents_for.return_value = [{"id": "a2"}]
        await self._complete(on_done, slot, "a1")
        assert await self._turn_end(state, slot) is False, "a2 is still running"
        mgr.running_agents_for.return_value = []
        await self._complete(on_done, slot, "a2")

        assert await self._turn_end(state, slot) is True

    @pytest.mark.asyncio
    async def test_a_user_message_before_the_last_completion_keeps_the_batch(self, tmp_path):
        """A message run beside the fan-out (the queued card's Run now) does not
        split the batch: the two results still earn their synthesis."""
        on_done, state, slot, mgr = self._wire(tmp_path)

        mgr.running_agents_for.return_value = [{"id": "a2"}]
        await self._complete(on_done, slot, "a1")
        await self._turn_end(state, slot)
        await self._user_message_drains(state, slot, allow_user_during_subagents=True)
        await self._turn_end(state, slot)
        mgr.running_agents_for.return_value = []
        await self._complete(on_done, slot, "a2")

        assert await self._turn_end(state, slot) is True

    @pytest.mark.asyncio
    async def test_a_user_message_after_the_arm_ends_the_batch(self, tmp_path):
        """The user replies while the last completion's report streams, so the
        message drains at that turn's end. The user takes over (the armed
        synthesis is disarmed, as before), and the next lone completion starts
        a batch of its own."""
        on_done, state, slot, mgr = self._wire(tmp_path)

        mgr.running_agents_for.return_value = [{"id": "a2"}]
        await self._complete(on_done, slot, "a1")
        await self._turn_end(state, slot)
        mgr.running_agents_for.return_value = []
        await self._complete(on_done, slot, "a2")
        await self._user_message_drains(state, slot, turn_tail=True)
        assert slot._pending_synthesis is False
        assert await self._turn_end(state, slot) is False

        await self._complete(on_done, slot, "a3")
        assert await self._turn_end(state, slot) is False

    @pytest.mark.asyncio
    async def test_a_user_message_during_a_follow_up_batch_keeps_its_synthesis(self, tmp_path):
        """A lone completion's turn spawns b1 and b2, so its arm is still set
        when b1's completion turn and a Run-now message drain. The message
        disarms as before but must not zero the count of a batch that is still
        running: b1 and b2 reached the parent in two turns and earn one
        synthesis."""
        on_done, state, slot, mgr = self._wire(tmp_path)

        await self._complete(on_done, slot, "a1")
        mgr.running_agents_for.return_value = [{"id": "b1"}, {"id": "b2"}]
        assert await self._turn_end(state, slot) is False, "b1 and b2 are running"
        mgr.running_agents_for.return_value = [{"id": "b2"}]
        await self._complete(on_done, slot, "b1")
        assert await self._turn_end(state, slot) is False, "b2 is still running"
        await self._user_message_drains(state, slot, allow_user_during_subagents=True)
        assert await self._turn_end(state, slot) is False
        mgr.running_agents_for.return_value = []
        await self._complete(on_done, slot, "b2")

        assert await self._turn_end(state, slot) is True


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _subagent_done callback (via _init_subagents)
# ═══════════════════════════════════════════════════════════════════════════


class TestSubagentDone:
    """Subagent completion routing."""

    def _setup_orch_with_subagent_mgr(self):
        """Create orchestrator with subagent manager initialized."""
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.dashboard_state = _mock_dashboard_state()
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm_inst.running = []
                mock_sm_inst.queued_count_for = MagicMock(return_value=0)
                mock_sm_inst.queued_count_for_async = AsyncMock(return_value=0)
                mock_sm_inst.has_pending_work_for = MagicMock(return_value=False)
                mock_sm_inst.has_pending_work_for_async = AsyncMock(return_value=False)
                mock_sm_inst.running_agents_for = MagicMock(return_value=[])
                mock_sm_inst.get = MagicMock(return_value=None)
                mock_sm_inst.notify_injection_failed = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
        return orch, mock_sm

    @pytest.mark.asyncio
    async def test_dashboard_slot_idle_triggers_run_chat(self):
        """Subagent done → dashboard slot idle → _run_chat."""
        orch, mock_sm = self._setup_orch_with_subagent_mgr()
        # Get the on_done callback
        on_done = mock_sm.call_args[1]["on_done"]

        slot = MagicMock()
        slot.running = False
        slot.task = None
        slot.key = "test-slot"
        slot.mode = ""
        slot._recovery_chat_triggered = False
        slot._pending_subagent_failures = []
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)

        info = MagicMock()
        info.id = "agent-1"
        info.parent_session_key = "dashboard:test-slot"
        info.error = None
        info.result = "done!"
        info.result_path = ""
        info.task = "do something"
        info.agent = "coder"
        info.silent = False
        info.elapsed = 5.0
        info.started = 0.0

        with patch(
            "kiro_crew.slack.gateway._run_chat", new_callable=AsyncMock, return_value=None
        ) as mock_run_chat:
            await on_done(info)

        mock_run_chat.assert_called_once()
        assert mock_run_chat.call_args.args[1] is slot
        assert "done!" in mock_run_chat.call_args.args[2]
        orch.dashboard_state.notify.assert_not_called()
        orch.dashboard_state.push_slots_update.assert_called()

    @pytest.mark.asyncio
    async def test_dashboard_slot_busy_queues(self):
        """Subagent done → dashboard slot busy → queues message."""
        orch, mock_sm = self._setup_orch_with_subagent_mgr()
        on_done = mock_sm.call_args[1]["on_done"]

        slot = MagicMock()
        slot.running = True
        never_done = asyncio.get_event_loop().create_future()
        never_done.set_result(None)
        # flake-ok: pre-existing event-loop yield standing in for a finished turn, moved verbatim; tracked for a de-flake pass
        slot.task = asyncio.ensure_future(asyncio.sleep(0))
        # flake-ok: pre-existing unbounded await of the finished stand-in turn, moved verbatim; tracked for a de-flake pass
        await slot.task  # let it complete
        # But slot.running stays True (simulating another claim)
        slot.running = True
        slot.key = "busy-slot"
        slot.mode = ""
        slot._recovery_chat_triggered = False
        slot._pending_subagent_failures = []
        slot.queue_append = MagicMock()
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)

        info = MagicMock()
        info.id = "agent-2"
        info.parent_session_key = "dashboard:busy-slot"
        info.error = None
        info.result = "queued result"
        info.result_path = ""
        info.task = "task"
        info.agent = ""
        info.silent = False
        info.elapsed = 1.0
        info.started = 0.0

        await on_done(info)
        from kiro_crew.slack.gateway import SUBAGENT_COMPLETION_KIND

        slot.queue_append.assert_called_once()
        assert "queued result" in slot.queue_append.call_args.args[0]
        assert slot.queue_append.call_args.kwargs["kind"] == SUBAGENT_COMPLETION_KIND
        orch.dashboard_state.notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_cron_parent_injects_result(self):
        """Subagent done → cron parent → injects into session."""
        orch, mock_sm = self._setup_orch_with_subagent_mgr()
        on_done = mock_sm.call_args[1]["on_done"]
        orch.subagent_mgr.running = []

        info = MagicMock()
        info.id = "agent-3"
        info.parent_session_key = "cron:job1"
        info.error = None
        info.result = "cron agent result"
        info.result_path = ""
        info.task = "cron task"
        info.agent = ""
        info.silent = False
        info.elapsed = 2.0
        info.started = 0.0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="llm response",
        ) as mock_stream:
            await on_done(info)

        mock_stream.assert_awaited_once()
        orch.sessions.reset.assert_awaited_with("cron:job1")
        orch.dashboard_state.notify.assert_called_once()
        assert orch.dashboard_state.notify.call_args.args[0] == "subagent"
        assert "llm response" in orch.dashboard_state.notify.call_args.args[2]

    @pytest.mark.asyncio
    async def test_notification_only_for_unknown_parent(self):
        """Subagent done → unknown parent → notification only."""
        orch, mock_sm = self._setup_orch_with_subagent_mgr()
        on_done = mock_sm.call_args[1]["on_done"]

        info = MagicMock()
        info.id = "agent-4"
        info.parent_session_key = "subagent:parent"
        info.error = "something failed"
        info.result = None
        info.result_path = ""
        info.task = "failed task"
        info.agent = ""
        info.silent = False
        info.elapsed = 0.5
        info.started = 0.0

        await on_done(info)
        orch.dashboard_state.notify.assert_called_once()
        assert orch.dashboard_state.notify.call_args.args[0] == "subagent"
        assert info._report_delivered is True

    @pytest.mark.asyncio
    async def test_silent_subagent_no_notification(self):
        """Silent subagent → no dashboard notification."""
        orch, mock_sm = self._setup_orch_with_subagent_mgr()
        on_done = mock_sm.call_args[1]["on_done"]

        info = MagicMock()
        info.id = "agent-5"
        info.parent_session_key = "subagent:x"
        info.error = None
        info.result = "silent"
        info.result_path = ""
        info.task = "quiet task"
        info.agent = ""
        info.silent = True
        info.elapsed = 1.0
        info.started = 0.0

        await on_done(info)
        assert info._report_delivered is True
        orch.dashboard_state.notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_slack_parent_injects(self):
        """Subagent done → Slack parent → injects into session."""
        orch, mock_sm = self._setup_orch_with_subagent_mgr()
        on_done = mock_sm.call_args[1]["on_done"]
        orch.slack = MagicMock()
        orch.slack.open_dm = AsyncMock(return_value="D_U1")
        orch.slack.post_message = AsyncMock()
        orch.slack.post_blocks = AsyncMock(return_value="ts")

        info = MagicMock()
        info.id = "agent-6"
        info.parent_session_key = "C123:1234.567890"
        info.error = None
        info.result = "slack result"
        info.result_path = ""
        info.task = "slack task"
        info.agent = ""
        info.silent = False
        info.elapsed = 3.0
        # flake-ok: pre-existing real-clock start stamp, moved verbatim; tracked for a de-flake pass
        info.started = time.monotonic() - 3.0

        with patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            new_callable=AsyncMock,
            return_value="synthesized response",
        ) as mock_stream:
            await on_done(info)

        mock_stream.assert_awaited_once()
        assert mock_stream.await_args.args[1] == "msg"
        orch.dashboard_state.notify.assert_called_once()
        assert orch.dashboard_state.notify.call_args.args[0] == "subagent"

    @pytest.mark.asyncio
    async def test_dashboard_slot_gone_notification_only(self):
        """Subagent done → dashboard slot gone → notification only."""
        orch, mock_sm = self._setup_orch_with_subagent_mgr()
        on_done = mock_sm.call_args[1]["on_done"]
        orch.dashboard_state.get_slot = MagicMock(return_value=None)

        info = MagicMock()
        info.id = "agent-7"
        info.parent_session_key = "dashboard:gone-slot"
        info.error = None
        info.result = "orphan result"
        info.result_path = ""
        info.task = "orphan task"
        info.agent = ""
        info.silent = False
        info.elapsed = 1.0
        info.started = 0.0

        await on_done(info)
        orch.dashboard_state.notify.assert_called_once()
        assert orch.dashboard_state.notify.call_args.args[0] == "subagent"
        assert info._report_delivered is True


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _retrigger_recovery
# ═══════════════════════════════════════════════════════════════════════════


class TestRetriggerRecovery:
    """Recovery retrigger for queued subagent failures."""

    def _setup(self):
        orch = _make_orchestrator()
        orch.sessions = _mock_sessions()
        orch.ctx_builder = _mock_context_builder()
        orch.ctx_builder.hooks = MagicMock()
        orch.ctx_builder.build_message = MagicMock(return_value=("msg", None))
        orch.dashboard_state = _mock_dashboard_state()
        with patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False):
            with patch("kiro_crew.slack.gateway.SubagentManager") as mock_sm:
                mock_sm_inst = MagicMock()
                mock_sm_inst.start_reaper = MagicMock()
                mock_sm_inst.running = []
                mock_sm_inst.queued_count_for = MagicMock(return_value=0)
                mock_sm_inst.queued_count_for_async = AsyncMock(return_value=0)
                mock_sm_inst.has_pending_work_for = MagicMock(return_value=False)
                mock_sm_inst.has_pending_work_for_async = AsyncMock(return_value=False)
                mock_sm_inst.running_agents_for = MagicMock(return_value=[])
                mock_sm_inst.get = MagicMock(return_value=None)
                mock_sm_inst.notify_injection_failed = MagicMock()
                mock_sm.return_value = mock_sm_inst
                orch._init_subagents()
        return orch, mock_sm

    @pytest.mark.asyncio
    async def test_subagent_event_injection_failed(self):
        """Subagent injection_failed event updates slot."""
        orch, mock_sm = self._setup()
        on_event = mock_sm.call_args[1]["on_event"]

        slot = MagicMock()
        slot.append = MagicMock()
        slot._pending_subagent_failures = []
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)

        info = MagicMock()
        info.id = "agent-fail"
        info.parent_session_key = "dashboard:slot1"
        info.task = "failed task"

        await on_event(
            "subagent_injection_failed",
            info,
            {"error": "timed out", "failure_msg": "Agent failed"},
        )

        slot.append.assert_called_once()
        assert len(slot._pending_subagent_failures) == 1
        orch.dashboard_state.push_slots_update.assert_called()

    @pytest.mark.asyncio
    async def test_subagent_event_injection_failed_notes_the_debt_of_a_completed_run(self):
        """A completed run keeps no delivered tombstone while its report is undelivered.

        The queued notice carries the debt instead, under its own text, for the
        drain that hands that text to the parent to settle.
        """
        from kiro_crew.subagent import SubagentDelivery

        orch, mock_sm = self._setup()
        on_event = mock_sm.call_args[1]["on_event"]
        slot = MagicMock()
        slot._pending_subagent_failures = []
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        info = MagicMock()
        info.id = "agent-done"
        info.parent_session_key = "dashboard:slot1"
        info.task = "finished task"
        info.outcome = "completed"
        info.elapsed = 3.5
        info.credits = 0.25

        await on_event(
            "subagent_injection_failed",
            info,
            {"error": "timed out", "failure_msg": "Agent done"},
        )

        assert slot._pending_subagent_failures == ["Agent done"]
        slot.note_pending_subagent_delivery.assert_called_once_with(
            "Agent done", [SubagentDelivery("agent-done", 3.5, 0.25)]
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", ["failed", "stopped"])
    async def test_subagent_event_injection_failed_notes_no_debt_for_an_unfinished_run(
        self, outcome
    ):
        """Only a completed run was ever going to get the ``delivered`` tombstone."""
        orch, mock_sm = self._setup()
        on_event = mock_sm.call_args[1]["on_event"]
        slot = MagicMock()
        slot._pending_subagent_failures = []
        orch.dashboard_state.get_slot = MagicMock(return_value=slot)
        info = MagicMock()
        info.id = "agent-unfinished"
        info.parent_session_key = "dashboard:slot1"
        info.task = "unfinished task"
        info.outcome = outcome

        await on_event(
            "subagent_injection_failed",
            info,
            {"error": "timed out", "failure_msg": "Agent unfinished"},
        )

        assert slot._pending_subagent_failures == ["Agent unfinished"]
        slot.note_pending_subagent_delivery.assert_not_called()

    @pytest.mark.asyncio
    async def test_subagent_event_chunk(self):
        """Subagent chunk event broadcasts to subscribers."""
        orch, mock_sm = self._setup()
        on_event = mock_sm.call_args[1]["on_event"]

        info = MagicMock()
        info.id = "agent-chunk"
        info.parent_session_key = "dashboard:slot1"

        await on_event("subagent_chunk", info, {"text": "partial"})
        orch.dashboard_state.broadcast_ws_subagent_subscribers.assert_called_once()
        etype, payload = orch.dashboard_state.broadcast_ws_subagent_subscribers.call_args.args
        assert etype == "subagent_chunk"
        assert payload["text"] == "partial"

    @pytest.mark.asyncio
    async def test_subagent_event_status(self):
        """Generic subagent status event broadcasts to all."""
        orch, mock_sm = self._setup()
        on_event = mock_sm.call_args[1]["on_event"]

        info = MagicMock()
        info.id = "agent-status"
        info.parent_session_key = "dashboard:slot1"

        await on_event("subagent_started", info, {})
        etypes = [c.args[0] for c in orch.dashboard_state.broadcast_ws.call_args_list]
        assert "subagent_started" in etypes

    @pytest.mark.asyncio
    async def test_subagent_event_routes_cron_parent_to_the_cron_tab(self):
        """Regression: a cron-born parent's events must carry the TAB's slot
        key (``cron-<id>``), not the raw session key (``cron:<id>``). The
        frontend routes frames by exact slot match, so the raw key left the
        Subagents panel permanently on "No subagents running" for every agent
        spawned from a cron-born session."""
        from kiro_crew.session_surface import set_dashboard_surfaced

        orch, mock_sm = self._setup()
        on_event = mock_sm.call_args[1]["on_event"]

        info = MagicMock()
        info.id = "agent-cron"
        info.parent_session_key = "cron:188f71e5"
        info.batch_id = ""

        set_dashboard_surfaced({"cron:188f71e5"})
        try:
            await on_event("subagent_spawn", info, {"task": "t", "agent": "a"})
        finally:
            set_dashboard_surfaced(())

        orch.dashboard_state.broadcast_ws.assert_called()
        etype, payload = orch.dashboard_state.broadcast_ws.call_args[0]
        assert etype == "subagent_spawn"
        assert payload["slot"] == "cron-188f71e5"
