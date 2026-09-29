"""The stage loop's wait for a spawn wave is event-driven, not a timed poll.

A poll of ``running_agents_for`` — an O(n) scan over every retained agent — on a
two-second timer costs two things, and the tests below separate them: a finished
wave is noticed up to two seconds late, and the scan runs whether anything has
happened or not.

``_stage_loop`` therefore waits on ``SubagentManager.completion_event(parent_key)``,
pulsed once per terminal report, with a coarse fallback timeout. The fallback is
not a detail to be optimised away: a run can reach a terminal state on a path that
never announces (shutdown's ``cancel_all``), so a wait that woke ONLY on the event
could hold a stage for its entire budget. Both halves are pinned here.

``asyncio.sleep`` is poisoned in the tests that assert the mechanism. That is the
sharpest available statement of "not a busy-wait": the module has no other sleep,
so any timer fails rather than merely running slower, which a timing assertion
could not tell apart from a loaded host.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from kiro_crew.context_management import OrchestrationTracker
from kiro_crew.dashboard.state import _ChatSlot


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    for module in ("state", "chat", "chat_orchestrator"):
        monkeypatch.setattr(f"kiro_crew.dashboard.{module}.config_dir", lambda: tmp_path)


class _Subagents:
    """Minimal stand-in for the manager's wave-visible surface.

    A REAL ``asyncio.Event``, because the contract under test is the clear /
    re-read / wait ordering and a mock event cannot express it.

    ``on_wait`` is how a test says "this happens WHILE the loop is waiting". The
    hook fires from ``completion_event``, which the loop calls once, immediately
    before it starts waiting, and it is scheduled with ``call_soon`` so it lands
    at the first suspension rather than during the synchronous approach. Timing
    the arrival off the stage turn instead does not work: the turn is awaited, so
    a callback queued there runs before the loop's first read of the running set
    and the wait is never entered at all.
    """

    def __init__(self, pending: list[dict] | None = None, on_wait=None):
        self.pending = pending if pending is not None else []
        self.event = asyncio.Event()
        self.scans = 0
        self.released: list[str] = []
        self.events_handed_out: list[str] = []
        self._on_wait = on_wait

    def running_agents_for(self, parent_key: str):
        self.scans += 1
        return list(self.pending)

    async def has_pending_work_for_async(self, _parent_key: str) -> bool:
        return False

    async def wait_for_parent_reports(self, _parent_key: str, _owner: str = "") -> bool:
        return False

    def completion_event(self, parent_key: str) -> asyncio.Event:
        self.events_handed_out.append(parent_key)
        if self._on_wait is not None:
            hook, self._on_wait = self._on_wait, None
            asyncio.get_running_loop().call_soon(hook)
        return self.event

    def release_completion_event(self, parent_key: str) -> None:
        self.released.append(parent_key)

    def signal_completion(self, parent_key: str) -> None:
        self.event.set()

    def finish_wave(self) -> None:
        """What a terminal report does: the run leaves the set, then the pulse."""
        self.pending = []
        self.event.set()


def _make_state(subagents):
    state = MagicMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.subagents = subagents
    return state


def _make_slot(*, stage_timeout=1800, titles=("Only",)):
    slot = _ChatSlot("wave-wait-slot", mode="orchestrator")
    slot._auto_run = True
    slot._stage_titles = list(titles)
    slot._plan_goal = "Test goal"
    slot._orch_tracker = OrchestrationTracker(stage_timeout_seconds=stage_timeout)
    return slot


def _stage_turn(monkeypatch, *, on_turn=None):
    async def _mock_run_chat(state, slot, message, **kwargs):
        callback = kwargs.get("_on_consumed")
        if callable(callback):
            callback(True)
        slot.append("assistant", "stage output", "msg msg-a")
        if on_turn is not None:
            on_turn()

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)


def _no_sleeping(monkeypatch):
    """Make any reintroduced timer a failure rather than a slowdown."""

    async def _boom(*a, **k):
        raise AssertionError("the wave wait slept on a timer instead of waiting on the event")

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.asyncio.sleep", _boom)


def _assistant_text(slot):
    return "\n".join(m.get("content", "") for m in slot.messages if m.get("role") == "assistant")


class TestTheWaveWaitIsEventDriven:
    @pytest.mark.asyncio
    async def test_a_pulse_ends_the_wait(self, monkeypatch):
        """RED BEFORE: the loop reached the wait through ``asyncio.sleep(2)``."""
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat import _stage_loop

        # A fallback far longer than this test may take, so only the PULSE can
        # end the wait: a fallback that could fire would prove nothing.
        monkeypatch.setattr(chat_orchestrator, "_SA_FALLBACK_SECS", 30.0)
        _no_sleeping(monkeypatch)

        subagents = _Subagents(pending=[{"id": "a1"}])
        # The wave finishes one event-loop tick into the wait.
        subagents._on_wait = subagents.finish_wave
        _stage_turn(monkeypatch)
        slot = _make_slot()

        await asyncio.wait_for(_stage_loop(_make_state(subagents), slot, auto_run=True), 5)

        text = _assistant_text(slot)
        assert "subagent wait exhausted" not in text
        assert "✅ All 1 stages complete." in text

    @pytest.mark.asyncio
    async def test_a_wave_already_finished_costs_no_wait_at_all(self, monkeypatch):
        """The common case: nothing pending when the stage turn ends."""
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat import _stage_loop

        monkeypatch.setattr(chat_orchestrator, "_SA_FALLBACK_SECS", 30.0)
        _no_sleeping(monkeypatch)

        subagents = _Subagents(pending=[])
        _stage_turn(monkeypatch)
        slot = _make_slot()

        await asyncio.wait_for(_stage_loop(_make_state(subagents), slot, auto_run=True), 5)

        assert "✅ All 1 stages complete." in _assistant_text(slot)

    @pytest.mark.asyncio
    async def test_the_scan_runs_per_completion_not_per_tick(self, monkeypatch):
        """The other half of the finding: ``running_agents_for`` is O(n).

        One wave, one completion — so the whole wait must cost a handful of
        scans, not one per two seconds of wall clock.
        """
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat import _stage_loop

        monkeypatch.setattr(chat_orchestrator, "_SA_FALLBACK_SECS", 30.0)
        _no_sleeping(monkeypatch)

        subagents = _Subagents(pending=[{"id": "a1"}])
        subagents._on_wait = subagents.finish_wave
        _stage_turn(monkeypatch)
        slot = _make_slot()

        await asyncio.wait_for(_stage_loop(_make_state(subagents), slot, auto_run=True), 5)

        assert subagents.scans <= 4, (
            f"the wave was scanned {subagents.scans} times; the event-driven wait "
            "must not add a boundary-settlement rescan"
        )

    @pytest.mark.asyncio
    async def test_the_waiter_registration_is_released(self, monkeypatch):
        """The manager's waiter table is bounded; an unconsumed entry is a leak."""
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat import _stage_loop

        monkeypatch.setattr(chat_orchestrator, "_SA_FALLBACK_SECS", 30.0)

        subagents = _Subagents(pending=[{"id": "a1"}])
        subagents._on_wait = subagents.finish_wave
        _stage_turn(monkeypatch)
        slot = _make_slot()

        await asyncio.wait_for(_stage_loop(_make_state(subagents), slot, auto_run=True), 5)

        assert subagents.released == ["dashboard:wave-wait-slot"]


class TestTheFallbackStillBoundsTheWait:
    @pytest.mark.asyncio
    async def test_a_wave_that_never_announces_is_cut_at_the_deadline(self, monkeypatch):
        """A lost pulse must not hang the stage for its whole budget.

        ``stage_timeout_seconds=2`` puts the wave ceiling at 1s (half the stage
        budget), and the shortened fallback is what makes the loop notice.
        """
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat import _stage_loop

        monkeypatch.setattr(chat_orchestrator, "_SA_FALLBACK_SECS", 0.02)

        subagents = _Subagents(pending=[{"id": "a1"}])  # never finishes, never pulses
        _stage_turn(monkeypatch)
        slot = _make_slot(stage_timeout=2)

        await asyncio.wait_for(_stage_loop(_make_state(subagents), slot, auto_run=True), 20)

        text = _assistant_text(slot)
        assert "subagent wait exhausted" in text
        assert slot._auto_run is False

    @pytest.mark.asyncio
    async def test_the_ceiling_is_half_the_stage_budget(self, monkeypatch):
        """Preserved from the round-based cap it replaces (450 rounds x 2s)."""
        from kiro_crew.dashboard import chat_orchestrator

        assert chat_orchestrator._SA_MAX_WAIT_SECS == 900

    @pytest.mark.asyncio
    async def test_a_plan_cancel_ends_the_wait(self, monkeypatch):
        """Cancel must not wait out a fallback interval before being observed."""
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat import _stage_loop

        monkeypatch.setattr(chat_orchestrator, "_SA_FALLBACK_SECS", 30.0)
        _no_sleeping(monkeypatch)

        slot = _make_slot()

        def _cancel_mid_wave():
            # Exactly what api_chat_plan_action's cancel branch does, in its
            # order: stop the tracker, then wake the wait. Without the wake the
            # loop would sit on its fallback -- 30s here -- before noticing, and
            # the outer wait_for below is what would fail.
            slot._orch_tracker.stop()
            subagents.signal_completion("dashboard:wave-wait-slot")

        subagents = _Subagents(pending=[{"id": "a1"}], on_wait=_cancel_mid_wave)
        _stage_turn(monkeypatch)

        await asyncio.wait_for(_stage_loop(_make_state(subagents), slot, auto_run=True), 5)

        assert "✅ All 1 stages complete." not in _assistant_text(slot)


class TestTheManagerSideOfThePulse:
    """``SubagentManager``'s own contract, independent of the loop."""

    @pytest.mark.asyncio
    async def test_signal_sets_the_event_a_waiter_holds(self):
        from kiro_crew.subagent import SubagentManager

        mgr = SubagentManager.__new__(SubagentManager)
        mgr._completion_waiters = {}

        evt = mgr.completion_event("dashboard:s1")
        assert evt.is_set() is False
        mgr.signal_completion("dashboard:s1")
        assert evt.is_set() is True

    @pytest.mark.asyncio
    async def test_the_same_key_gets_the_same_event(self):
        from kiro_crew.subagent import SubagentManager

        mgr = SubagentManager.__new__(SubagentManager)
        mgr._completion_waiters = {}

        assert mgr.completion_event("k") is mgr.completion_event("k")

    @pytest.mark.asyncio
    async def test_a_signal_with_no_waiter_registers_nothing(self):
        """The announce path must not accumulate entries for parents nobody waits on."""
        from kiro_crew.subagent import SubagentManager

        mgr = SubagentManager.__new__(SubagentManager)
        mgr._completion_waiters = {}

        mgr.signal_completion("dashboard:nobody")

        assert mgr._completion_waiters == {}

    @pytest.mark.asyncio
    async def test_release_is_idempotent(self):
        from kiro_crew.subagent import SubagentManager

        mgr = SubagentManager.__new__(SubagentManager)
        mgr._completion_waiters = {}

        mgr.completion_event("k")
        mgr.release_completion_event("k")
        mgr.release_completion_event("k")

        assert mgr._completion_waiters == {}

    @pytest.mark.asyncio
    async def test_the_table_is_fused_against_a_leak(self):
        """Past the cap a caller gets a detached event and falls back to its timeout.

        Refusing to grow is the point: the caller's own fallback still bounds its
        wait, so a leak degrades latency rather than memory.
        """
        from kiro_crew import subagent as subagent_mod
        from kiro_crew.subagent import SubagentManager

        mgr = SubagentManager.__new__(SubagentManager)
        mgr._completion_waiters = {}

        for i in range(subagent_mod._MAX_COMPLETION_WAITERS):
            mgr.completion_event(f"k{i}")
        overflow = mgr.completion_event("one-too-many")

        assert len(mgr._completion_waiters) == subagent_mod._MAX_COMPLETION_WAITERS
        mgr.signal_completion("one-too-many")
        assert overflow.is_set() is False


class TestGoAfterAWaitExhaustionHalt:
    """What the halt notice promises Go does, pinned against the real stage loop."""

    @pytest.mark.asyncio
    async def test_go_after_exhaustion_advances_to_the_next_stage(self, monkeypatch):
        """Exhaustion cancels the stage's children; Go then runs the NEXT stage.

        The halt notice tells the user "Send Go to continue to the next stage with
        this stage's partial results". If Go instead re-ran the halted stage, or
        waited on the cancelled children again, that notice would be a lie.
        """
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat import _stage_loop

        monkeypatch.setattr(chat_orchestrator, "_SA_FALLBACK_SECS", 0.02)

        subagents = _Subagents(pending=[{"id": "a1"}])  # never finishes on its own
        cancelled: list[tuple[str, str, bool]] = []

        async def _cancel_for_boundary(parent_key, owner, *, retain_scope=True):
            cancelled.append((parent_key, owner, retain_scope))
            subagents.pending = []  # the cancel is what empties the wave
            return (1, 0)

        subagents.cancel_for_boundary = _cancel_for_boundary
        stage_turns: list[str] = []

        async def _mock_run_chat(state, slot, message, **kwargs):
            stage_turns.append(message)
            callback = kwargs.get("_on_consumed")
            if callable(callback):
                callback(True)
            slot.append("assistant", "stage output", "msg msg-a")

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)
        slot = _make_slot(stage_timeout=2, titles=("Collect", "Verify"))
        state = _make_state(subagents)

        await asyncio.wait_for(_stage_loop(state, slot, auto_run=True), 20)

        text = _assistant_text(slot)
        assert "subagent wait exhausted" in text
        assert "Stopped 1 unfinished subagent run(s)" in text
        assert "Send Go to continue to the next stage" in text
        assert slot._auto_run is False
        assert len(cancelled) == 1 and cancelled[0][2] is True
        assert len(stage_turns) == 1 and "Execute Stage 1 of 2 now" in stage_turns[0]

        # Go: the same controller entry the plan-action handler starts.
        await asyncio.wait_for(_stage_loop(state, slot, auto_run=False), 20)

        assert len(stage_turns) == 2, stage_turns
        assert (
            "Execute Stage 2 of 2 now" in stage_turns[1]
        ), "Go re-ran the halted stage instead of advancing"

    @pytest.mark.parametrize(
        ("raised", "notice"),
        [
            (TimeoutError("stage ceiling"), "timed out after"),
            (RuntimeError("provider blew up"), "failed due to an internal error"),
        ],
        ids=["stage-turn-timeout", "stage-turn-error"],
    )
    @pytest.mark.asyncio
    async def test_turn_timeout_and_error_halts_cancel_the_stage_children(
        self, monkeypatch, raised, notice
    ):
        """The stage-turn timeout and error halts cancel the stage's children.

        The cancel runs BEFORE the notice is written and before the boundary is
        preserved, so it reads the still-armed scope and the notice reports the
        real outcome.
        """
        from kiro_crew.dashboard.chat import _stage_loop

        subagents = _Subagents(pending=[{"id": "a1"}])
        cancelled: list[tuple[str, str, bool]] = []

        async def _cancel_for_boundary(parent_key, owner, *, retain_scope=True):
            assert owner, "cancel ran after the boundary lost its owner"
            cancelled.append((parent_key, owner, retain_scope))
            return (1, 0)

        subagents.cancel_for_boundary = _cancel_for_boundary

        async def _mock_run_chat(state, slot, message, **kwargs):
            raise raised

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)
        slot = _make_slot(titles=("Collect", "Verify"))

        await asyncio.wait_for(_stage_loop(_make_state(subagents), slot, auto_run=True), 20)

        text = _assistant_text(slot)
        assert notice in text
        assert "Stopped 1 unfinished subagent run(s) from this stage." in text
        assert slot._auto_run is False
        assert [c[0] for c in cancelled] == ["dashboard:wave-wait-slot"]
        assert cancelled[0][2] is True

    @pytest.mark.parametrize(
        "raised",
        [TimeoutError("stage ceiling"), RuntimeError("provider blew up")],
        ids=["stage-turn-timeout", "stage-turn-error"],
    )
    @pytest.mark.asyncio
    async def test_turn_halts_warn_when_children_were_not_confirmed_stopped(
        self, monkeypatch, raised
    ):
        """A timeout/error halt whose cancel failed warns that Go can wait again."""
        from kiro_crew.dashboard.chat import _stage_loop

        subagents = _Subagents(pending=[{"id": "a1"}])

        async def _cancel_for_boundary(parent_key, owner, *, retain_scope=True):
            raise RuntimeError("cancel exploded")

        subagents.cancel_for_boundary = _cancel_for_boundary

        async def _mock_run_chat(state, slot, message, **kwargs):
            raise raised

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)
        slot = _make_slot(titles=("Collect", "Verify"))

        await asyncio.wait_for(_stage_loop(_make_state(subagents), slot, auto_run=True), 20)

        text = _assistant_text(slot)
        assert "cancelling the rest failed" in text
        assert "until they stop, Go can wait on them again" in text
        assert slot._auto_run is False


class TestAStopDuringTheHaltCancelKeepsTheNotice:
    """A Stop landing while a halt waits on its cancel must not eat the halt.

    Before the halts cancelled their stage's children there was no await
    between deciding to halt and publishing the notice, so the notice and its
    audit event always landed. The bounded cancel wait is now that await: a
    controller cancelled inside it (the user's Stop, a slot close) must still
    publish the notice -- worded from the unfinished outcome -- and then let
    the ``CancelledError`` propagate rather than swallow it.
    """

    @pytest.mark.parametrize(
        ("path", "notice", "event_type"),
        [
            ("wait-exhausted", "subagent wait exhausted", "auto_run_subagent_timeout"),
            ("stage-turn-timeout", "timed out after", "auto_run_timeout"),
            ("stage-turn-error", "failed due to an internal error", "auto_run_stage_error"),
        ],
    )
    @pytest.mark.asyncio
    async def test_cancel_during_halt_cancel_still_publishes_and_propagates(
        self, monkeypatch, path, notice, event_type
    ):
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat import _stage_loop

        monkeypatch.setattr(chat_orchestrator, "_SA_FALLBACK_SECS", 0.02)
        audited: list[str] = []
        monkeypatch.setattr(
            chat_orchestrator,
            "sel",
            lambda: MagicMock(log=lambda event: audited.append(event.event_type)),
        )

        subagents = _Subagents(pending=[{"id": "a1"}])  # never finishes on its own
        cancel_entered = asyncio.Event()
        release = asyncio.Event()
        cancel_was_cancelled = False

        async def _stuck_cancel(parent_key, owner, *, retain_scope=True):
            nonlocal cancel_was_cancelled
            cancel_entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancel_was_cancelled = True
                raise
            return (1, 0)

        subagents.cancel_for_boundary = _stuck_cancel

        if path == "wait-exhausted":
            _stage_turn(monkeypatch)
            slot = _make_slot(stage_timeout=2, titles=("Collect", "Verify"))
        else:
            raised = (
                TimeoutError("stage ceiling")
                if path == "stage-turn-timeout"
                else (RuntimeError("provider blew up"))
            )

            async def _mock_run_chat(state, slot, message, **kwargs):
                raise raised

            monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)
            slot = _make_slot(titles=("Collect", "Verify"))

        state = _make_state(subagents)
        controller = asyncio.ensure_future(_stage_loop(state, slot, auto_run=True))
        await asyncio.wait_for(cancel_entered.wait(), 20)
        assert not _assistant_text(slot).count(notice), "halt published before its cancel"

        controller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(controller, 20)

        text = _assistant_text(slot)
        assert text.count(notice) == 1, text
        assert "still finishing in the background for 1 parent session(s)" in text
        assert "Send Go to continue" not in text
        assert audited.count(event_type) == 1, audited
        assert slot._auto_run is False
        # The halt's own cancel keeps running; the Stop did not cut it mid-reap.
        assert cancel_was_cancelled is False
        release.set()
        for _ in range(20):
            await asyncio.sleep(0)
        assert cancel_was_cancelled is False
