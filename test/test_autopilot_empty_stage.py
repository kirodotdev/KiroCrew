"""An Autopilot stage that produced nothing must not count as done.

``_stage_loop`` advanced on one condition only — ``_run_chat`` returned without
raising — so a stage whose turn emitted no assistant text moved the plan forward,
was rendered ``✅ completed`` in the next stage's ``status_summary``, and let a Go
All run reach "✅ All N stages complete." having produced none of it.

The verdict is deliberately narrow and mechanical: the text the stage CAPTURED
(``_collect_stage_result_parts``, the same snapshot written to
``stage_N_result.md``) is empty after stripping. That keeps the judgement
identical to the artifact on disk rather than a second opinion about it.

What an empty stage is treated AS is the other half of the contract, and both
halves need pinning:

* a **failed round of that stage** — recorded on the tracker, so three empty
  attempts reach ``MAX_STAGE_ROUNDS`` exactly as three fruitless spawn waves do;
* **not an advance** — the stage latches itself for retry, because the loop
  otherwise resumes at ``tracker.current_stage``, which the empty stage already
  registered on entry. Without the latch "do not advance" would silently mean
  "advance", which is the defect.

Auto-run stops rather than retrying by itself. That matches every other guard in
this loop (stage timeout, round cap, subagent failure): the next attempt is only
worth spending if a human still wants it, and the notice carries the same Go row
those pauses do.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiro_crew.context_management import MAX_STAGE_ROUNDS, OrchestrationTracker
from kiro_crew.dashboard.state import _ChatSlot


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    """Stage results are written under ``config_dir()`` — keep them per-test."""
    for module in ("state", "chat", "chat_orchestrator"):
        monkeypatch.setattr(f"kiro_crew.dashboard.{module}.config_dir", lambda: tmp_path)


def _make_state():
    state = MagicMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.subagents = MagicMock()
    state.subagents.running_agents_for = MagicMock(return_value=[])
    return state


def _make_slot(titles=("First", "Second", "Third"), *, auto_run=True):
    slot = _ChatSlot("empty-stage-slot", mode="orchestrator")
    slot._auto_run = auto_run
    slot._stage_titles = list(titles)
    slot._plan_goal = "Test goal"
    # A pre-built tracker keeps the loop off its bootstrap path, so no config
    # load runs and the seeded ledger is the one under test.
    slot._orch_tracker = OrchestrationTracker(stage_timeout_seconds=1800)
    return slot


def _stage_turns(monkeypatch, *, texts=None):
    """Mock the stage turn. ``texts[i]`` is stage i+1's assistant output."""
    box = {"n": 0}

    async def _mock_run_chat(state, slot, message, **kwargs):
        idx = box["n"]
        box["n"] += 1
        body = (texts or [])[idx] if texts and idx < len(texts) else f"stage {idx + 1} output"
        # An empty body is appended as a real (empty) assistant row rather than
        # skipped: that is what a turn producing no text leaves behind, and the
        # capture walk has to be the thing that reads it as nothing.
        slot.append("assistant", body, "msg msg-a")

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)
    return box


def _assistant_text(slot):
    return "\n".join(m.get("content", "") for m in slot.messages if m.get("role") == "assistant")


class TestAnEmptyStageStopsThePlan:
    @pytest.mark.asyncio
    async def test_empty_first_stage_does_not_advance(self, monkeypatch):
        """RED BEFORE: the loop ran all three stages and claimed completion."""
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot()
        box = _stage_turns(monkeypatch, texts=[""])

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert box["n"] == 1, "the plan advanced past a stage that produced nothing"
        text = _assistant_text(slot)
        assert "produced no output" in text
        assert "✅ All 3 stages complete." not in text
        assert slot._auto_run is False

    @pytest.mark.asyncio
    async def test_whitespace_only_output_is_still_empty(self, monkeypatch):
        """The test is "empty after strip", not "no row was appended"."""
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot()
        box = _stage_turns(monkeypatch, texts=["   \n\n\t "])

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert box["n"] == 1
        assert "produced no output" in _assistant_text(slot)

    @pytest.mark.asyncio
    async def test_the_user_gets_a_go_row_to_retry(self, monkeypatch):
        """A pause, not a dead end: the plan chips have to come back."""
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot()
        _stage_turns(monkeypatch, texts=[""])

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert "[OPTION: Go | Go All | Cancel]" in _assistant_text(slot)

    @pytest.mark.asyncio
    async def test_the_turn_is_closed_out_as_needing_input(self, monkeypatch):
        """Without ``needs_input`` the surface reads the pause as a finished turn."""
        from kiro_crew.dashboard.chat import _stage_loop

        state = _make_state()
        slot = _make_slot()
        _stage_turns(monkeypatch, texts=[""])

        await _stage_loop(state, slot, auto_run=True)

        done = [c for c in state.broadcast_ws.call_args_list if c.args[0] == "chat_done"]
        assert done, "the slot was left with no terminal frame"
        assert done[-1].args[1].get("needs_input") is True

    @pytest.mark.asyncio
    async def test_it_is_audited(self, monkeypatch):
        """Every other advancement guard in this loop logs; so does this one."""
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat import _stage_loop

        events: list[str] = []
        sink = MagicMock()
        sink.log = MagicMock(side_effect=lambda ev: events.append(ev.operation))
        monkeypatch.setattr(chat_orchestrator, "sel", lambda: sink)

        slot = _make_slot()
        _stage_turns(monkeypatch, texts=[""])

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert "stage_produced_nothing" in events


class TestTheNextGoRetriesTheSameStage:
    @pytest.mark.asyncio
    async def test_re_entry_runs_the_empty_stage_again(self, monkeypatch):
        """RED BEFORE: re-entry resumed at `current_stage` and ran stage 2.

        The whole point of "do not advance" is what the NEXT Go does, so the
        latch is checked through a second loop entry rather than by reading the
        tracker.
        """
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot()
        seen: list[str] = []

        async def _mock_run_chat(state, slot_, message, **kwargs):
            seen.append(message.split("## Current Stage — ", 1)[-1].splitlines()[0])
            slot_.append("assistant", "" if len(seen) == 1 else "real work", "msg msg-a")

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)

        state = _make_state()
        await _stage_loop(state, slot, auto_run=False)
        assert seen == ["Stage 1: First"]

        # The user's next Go.
        await _stage_loop(state, slot, auto_run=False)

        assert seen[1] == "Stage 1: First", (
            "the retry ran the stage AFTER the one that produced nothing, so the "
            "empty stage was silently skipped"
        )

    @pytest.mark.asyncio
    async def test_the_latch_is_spent_by_one_re_entry(self, monkeypatch):
        """A retry that succeeded must advance; the latch is not sticky."""
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot(titles=("First", "Second"))
        seen: list[str] = []

        async def _mock_run_chat(state, slot_, message, **kwargs):
            seen.append(message.split("## Current Stage — ", 1)[-1].splitlines()[0])
            slot_.append("assistant", "" if len(seen) == 1 else "real work", "msg msg-a")

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)

        state = _make_state()
        await _stage_loop(state, slot, auto_run=False)  # stage 1, empty
        await _stage_loop(state, slot, auto_run=False)  # stage 1 again, productive
        await _stage_loop(state, slot, auto_run=False)  # stage 2

        assert seen == ["Stage 1: First", "Stage 1: First", "Stage 2: Second"]


class TestEmptyRoundsSpendTheStageBudget:
    @pytest.mark.asyncio
    async def test_each_empty_attempt_records_a_round(self, monkeypatch):
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot()
        _stage_turns(monkeypatch, texts=[""])

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert slot._orch_tracker.round_count(1) == 1

    @pytest.mark.asyncio
    async def test_a_delegated_empty_stage_is_charged_once(self, monkeypatch):
        """RED BEFORE: a tool-only stage paid two rounds for one attempt.

        A stage that delegates and then emits no assistant text is the ordinary
        shape of the empty case, and the subagent-completion handler has ALREADY
        recorded a round for its closed wave against this same tracker. Recording
        another makes one attempt cost two of the stage's three rounds, so two
        attempts exhaust a budget of three.

        The mocked turn records the wave exactly as ``_subagent_done`` does:
        against ``tracker.current_stage``, while the stage is still running.
        """
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot()

        async def _mock_run_chat(state, slot_, message, **kwargs):
            tracker = slot_._orch_tracker
            tracker.record_round(tracker.current_stage)  # the wave closes
            slot_.append("assistant", "", "msg msg-a")  # and emits nothing

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert slot._orch_tracker.round_count(1) == 1, (
            "one attempt was charged twice: the wave's round and the empty-output "
            "gate's round both landed on stage 1"
        )
        assert f"attempt 1 of {MAX_STAGE_ROUNDS}" in _assistant_text(slot)

    @pytest.mark.asyncio
    async def test_three_delegated_empty_attempts_reach_the_cap(self, monkeypatch):
        """And no more than three: the budget must still be reachable.

        The mirror of the test above. Charging once per attempt is only correct if
        an empty stage still runs out of rounds; a gate that never recorded one
        would let a delegated stage retry forever.
        """
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot()

        async def _mock_run_chat(state, slot_, message, **kwargs):
            tracker = slot_._orch_tracker
            tracker.record_round(tracker.current_stage)
            slot_.append("assistant", "", "msg msg-a")

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)

        state = _make_state()
        for _ in range(MAX_STAGE_ROUNDS):
            await _stage_loop(state, slot, auto_run=False)

        assert slot._orch_tracker.round_count(1) == MAX_STAGE_ROUNDS
        assert f"used all {MAX_STAGE_ROUNDS} of its rounds" in _assistant_text(slot)

    @pytest.mark.asyncio
    async def test_the_last_allowed_attempt_says_so(self, monkeypatch):
        """At the cap the notice asks for guidance rather than another bare retry."""
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot()
        # Two empty attempts already spent, latched for retry exactly as those
        # attempts left it: this entry's empty stage 1 is the third.
        slot._orch_tracker.start_stage(1)
        for _ in range(MAX_STAGE_ROUNDS - 1):
            slot._orch_tracker.record_round(1)
        slot._orch_tracker.mark_stage_for_retry(1)
        _stage_turns(monkeypatch, texts=[""])

        await _stage_loop(_make_state(), slot, auto_run=True)

        text = _assistant_text(slot)
        assert f"used all {MAX_STAGE_ROUNDS} of its rounds" in text
        assert "[OPTION: Go | Go All | Cancel]" in text


class TestTheEmptyGateIsAskedBeforeTheRoundCap:
    @pytest.mark.asyncio
    async def test_a_capped_and_empty_stage_is_still_latched_for_retry(self, monkeypatch):
        """Ordering: the cap must not halt an empty stage without latching it.

        With the cap gate first, a stage that spent its waves AND produced nothing
        halted on "used all 3 of its spawn rounds" and was never marked for retry,
        so the user's next Go advanced past a stage that produced nothing — the
        exact defect this item exists to close, reachable through the cap.
        """
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot()
        # The waves are spent, and this entry's stage 1 produces nothing.
        slot._orch_tracker.start_stage(1)
        for _ in range(MAX_STAGE_ROUNDS):
            slot._orch_tracker.record_round(1)
        slot._orch_tracker.mark_stage_for_retry(1)
        seen: list[str] = []

        async def _mock_run_chat(state, slot_, message, **kwargs):
            seen.append(message.split("## Current Stage — ", 1)[-1].splitlines()[0])
            slot_.append("assistant", "" if len(seen) == 1 else "real work", "msg msg-a")

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)

        state = _make_state()
        await _stage_loop(state, slot, auto_run=True)
        assert "produced no output" in _assistant_text(slot)

        await _stage_loop(state, slot, auto_run=False)

        assert seen[1] == "Stage 1: First", (
            "the round cap halted an empty stage without latching it, so the retry "
            f"ran {seen[1]!r}"
        )


class TestALoopExitBeforeAnyStageKeepsTheLatch:
    @pytest.mark.asyncio
    async def test_the_plan_watchdog_does_not_burn_the_retry(self, monkeypatch):
        """RED BEFORE: the watchdog's Go All exit spent the latch; the next Go skipped stage 1.

        Driven through the real loop rather than the tracker alone, because the
        burn happened in the loop's own read at entry.
        """
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot()
        seen: list[str] = []

        async def _mock_run_chat(state, slot_, message, **kwargs):
            seen.append(message.split("## Current Stage — ", 1)[-1].splitlines()[0])
            slot_.append("assistant", "" if len(seen) == 1 else "real work", "msg msg-a")

        monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)

        state = _make_state()
        await _stage_loop(state, slot, auto_run=False)
        assert seen == ["Stage 1: First"], "stage 1 did not run empty"

        # The user idles past the whole-plan budget, then clicks Go All: the
        # watchdog halts at the boundary, before any stage is entered.
        tracker = slot._orch_tracker
        tracker.max_plan_duration_seconds = 1
        monkeypatch.setattr(tracker, "is_plan_timed_out", lambda: True)
        slot._auto_run = True
        await _stage_loop(state, slot, auto_run=True)
        assert len(seen) == 1, "a stage ran despite the plan watchdog"
        assert "exceeded its total budget" in _assistant_text(slot)

        # A later Go, with the budget back within its ceiling.
        monkeypatch.setattr(tracker, "is_plan_timed_out", lambda: False)
        await _stage_loop(state, slot, auto_run=False)

        assert seen[1] == "Stage 1: First", (
            "the watchdog exit spent the retry latch, so the retry ran "
            f"{seen[1]!r} and the empty stage was skipped"
        )


class TestAProductiveStageIsUnaffected:
    @pytest.mark.asyncio
    async def test_a_normal_plan_still_runs_to_completion(self, monkeypatch):
        """Preservation: the gate must not fire on the ordinary case."""
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot()
        box = _stage_turns(monkeypatch)

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert box["n"] == 3
        text = _assistant_text(slot)
        assert "✅ All 3 stages complete." in text
        assert "produced no output" not in text

    @pytest.mark.asyncio
    async def test_a_failed_capture_write_is_not_read_as_an_empty_stage(
        self, monkeypatch, tmp_path
    ):
        """An unwritable session dir must not be mistaken for a stage that idled.

        The message walk is deliberately OUTSIDE the capture's ``try``: with it
        inside, an ``OSError`` from the write left the parts unbound and the
        verdict would have had to guess.
        """
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat import _stage_loop

        def _boom(*a, **k):
            raise OSError("read-only session directory")

        monkeypatch.setattr(chat_orchestrator, "_write_stage_result", _boom)

        slot = _make_slot(titles=("Only",))
        box = _stage_turns(monkeypatch)

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert box["n"] == 1
        text = _assistant_text(slot)
        assert "produced no output" not in text
        assert "✅ All 1 stages complete." in text


class TestTheTrackerLatch:
    """The tracker-level contract the loop depends on."""

    def test_reading_the_latch_does_not_spend_it(self):
        """RED BEFORE: the read consumed it, so a gate that exited early lost it.

        Several gates sit between the loop's read and the stage the latch names
        (the whole-plan watchdog, the stage-timeout check), and each can leave the
        loop without entering any stage. A latch spent at the read is gone on those
        paths, and the next Go resumes from ``current_stage`` — skipping the stage
        that produced nothing, which is the defect the latch exists to prevent.
        """
        tracker = OrchestrationTracker(stage_timeout_seconds=1800)
        tracker.mark_stage_for_retry(2)

        assert tracker.retry_stage == 2
        assert tracker.retry_stage == 2, "reading the latch spent it"

    def test_entering_a_stage_spends_the_latch(self):
        """One latch buys exactly one re-entry, and entry is what pays."""
        tracker = OrchestrationTracker(stage_timeout_seconds=1800)
        tracker.mark_stage_for_retry(2)

        tracker.start_stage(2)

        assert tracker.retry_stage == 0

    def test_no_latch_reads_as_zero(self):
        assert OrchestrationTracker(stage_timeout_seconds=1800).retry_stage == 0

    def test_a_plan_timeout_before_the_retry_keeps_the_latch(self):
        """RED BEFORE: the next Go ran Stage 2 and the empty Stage 1 was skipped.

        The reachable shape GPT 5.6 named: an empty stage latches itself, the user
        leaves the pause sitting longer than the whole-plan budget, then clicks Go
        All. The watchdog halts the plan BEFORE any stage is entered, so the latch
        must survive that exit.
        """
        tracker = OrchestrationTracker(stage_timeout_seconds=1800)
        tracker.start_stage(1)
        tracker.record_round(1)
        tracker.mark_stage_for_retry(1)

        # The loop's read at entry, then an exit with no stage entered.
        assert tracker.retry_stage == 1

        assert tracker.retry_stage == 1, (
            "the latch was spent by a loop entry that never reached a stage, so the "
            "next Go would resume at stage 2 and skip the stage that produced nothing"
        )

    def test_the_stage_keeps_its_ledger_entry(self):
        """A retry is not an un-run: the stage's rounds and place must survive."""
        tracker = OrchestrationTracker(stage_timeout_seconds=1800)
        tracker.start_stage(2)
        tracker.record_round(2)
        tracker.mark_stage_for_retry(2)

        assert tracker.round_count(2) == 1
        assert tracker.current_stage == 2
