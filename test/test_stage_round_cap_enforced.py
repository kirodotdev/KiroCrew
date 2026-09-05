"""``MAX_STAGE_ROUNDS`` must stop a dashboard plan.

``OrchestrationTracker.record_round()`` returns whether the stage has spent its
round budget, and the dashboard's ``_stage_loop`` recorded the round and threw the
answer away — so the "max 3 rounds per stage" the orchestrator prompt promises
enforced nothing on the dashboard path (issue #1783).

Where the rounds come from matters for what has to be tested. Every round is
recorded by the subagent-completion handler, against ``tracker.current_stage`` as
each spawn wave finishes — i.e. while the stage is still running. So the enforcing
gate is the one AFTER a stage's subagent wave, and the mocked stage turn below
records those rounds the same way the gateway does.

The loop does NOT record one. It enters a stage through ``start_stage``, which
registers the stage and starts its clock but spends no round, so all three the
prompt promises are available to actual waves. Entering through ``record_round``
(which is what the loop used to do, for the side effects rather than the count)
made the enforced cap 2 waves on this path and 3 on the Slack path — stricter than
the promise, and inconsistent between the two. That is what
``test_two_waves_per_stage_is_still_under_the_cap`` guards.

``MAX_STAGE_ESCALATIONS`` is deliberately NOT enforced on this path, and there is
nothing here to test for it. An escalation is only recorded by
``reset_after_guidance``, which zeroes that stage's rounds while KEEPING its key —
so ``current_stage`` does not move, the loop's next entry starts at the stage after
it, and an escalated stage is never re-entered. It stays enforced in the Slack
gateway, where the tracker is not driven by a stage loop.
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


def _make_slot(titles=("First", "Second", "Third")):
    slot = _ChatSlot("round-cap-slot", mode="orchestrator")
    slot._auto_run = True
    slot._stage_titles = list(titles)
    slot._plan_goal = "Test goal"
    # A pre-built tracker keeps the loop off its bootstrap path, so no config
    # load runs and the seeded ledger is the one under test.
    slot._orch_tracker = OrchestrationTracker(stage_timeout_seconds=1800)
    return slot


def _stage_turns(monkeypatch, *, extra_rounds_per_stage=0, texts=None):
    """Mock the stage turn, optionally recording subagent-wave rounds.

    ``extra_rounds_per_stage`` mimics the Slack gateway's subagent-completion
    handler, which records a round against ``tracker.current_stage`` each time a
    spawn wave for the running stage finishes.
    """
    box = {"n": 0}

    async def _mock_run_chat(state, slot, message, **kwargs):
        idx = box["n"]
        box["n"] += 1
        body = (texts or [])[idx] if texts and idx < len(texts) else f"stage {idx + 1} output"
        slot.append("assistant", body, "msg msg-a")
        tracker = slot._orch_tracker
        for _ in range(extra_rounds_per_stage):
            tracker.record_round(tracker.current_stage)

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _mock_run_chat)
    return box


def _assistant_text(slot):
    return "\n".join(m.get("content", "") for m in slot.messages if m.get("role") == "assistant")


def _stages_run(box):
    return box["n"]


# ── The enforcing gate: rounds spent during a stage ──────────────────────────


class TestRoundCapStopsThePlan:
    @pytest.mark.asyncio
    async def test_round_cap_halts_before_the_next_stage(self, monkeypatch):
        """RED BEFORE: the loop advanced through every stage regardless."""
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot()
        # Entry spends nothing, so the cap takes a full MAX_STAGE_ROUNDS waves.
        box = _stage_turns(monkeypatch, extra_rounds_per_stage=MAX_STAGE_ROUNDS)

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert _stages_run(box) == 1, "the plan advanced past a round-capped stage"
        assert "all 3 of its spawn rounds" in _assistant_text(slot)
        assert "✅ All 3 stages complete." not in _assistant_text(slot)

    @pytest.mark.asyncio
    async def test_round_cap_stops_auto_run(self, monkeypatch):
        """A later Go must not silently keep an auto-run plan going."""
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot()
        box = _stage_turns(monkeypatch, extra_rounds_per_stage=MAX_STAGE_ROUNDS)

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert slot._auto_run is False
        # Paired with the halt: a plan that ran to completion also clears the
        # flag, so the flag alone would assert nothing about the cap.
        assert box["n"] == 1

    @pytest.mark.asyncio
    async def test_capped_stage_keeps_its_result_on_disk(self, monkeypatch, tmp_path):
        """The halt is placed AFTER the capture, so the finished stage is not lost.

        Ordering, not mere existence: the cap could have been enforced before the
        capture, which would throw away a stage that had genuinely finished. The
        halt assertion is what makes this test about that ordering.
        """
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot()
        box = _stage_turns(monkeypatch, extra_rounds_per_stage=MAX_STAGE_ROUNDS)

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert box["n"] == 1, "the plan did not halt, so this proves nothing"
        result = tmp_path / "sessions" / slot.key / "stage_1_result.md"
        assert result.exists()
        assert slot._orch_tracker._stage_results.get(1) == str(result)

    @pytest.mark.asyncio
    async def test_round_cap_is_audited(self, monkeypatch):
        """The stop is a security-relevant guard, so it is logged like the others."""
        from kiro_crew.dashboard import chat_orchestrator
        from kiro_crew.dashboard.chat import _stage_loop

        events: list[str] = []
        sink = MagicMock()
        sink.log = MagicMock(side_effect=lambda ev: events.append(ev.operation))
        monkeypatch.setattr(chat_orchestrator, "sel", lambda: sink)

        slot = _make_slot()
        _stage_turns(monkeypatch, extra_rounds_per_stage=MAX_STAGE_ROUNDS)

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert "stage_round_cap" in events

    @pytest.mark.asyncio
    async def test_a_plan_under_its_budget_is_unaffected(self, monkeypatch):
        """Preservation: one round per stage is the normal case and must run through."""
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot()
        box = _stage_turns(monkeypatch, extra_rounds_per_stage=0)

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert _stages_run(box) == 3
        assert "✅ All 3 stages complete." in _assistant_text(slot)

    @pytest.mark.asyncio
    async def test_two_waves_per_stage_is_still_under_the_cap(self, monkeypatch):
        """The budget belongs to the WAVES, and all 3 of it must be spendable.

        This is the off-by-one guard. Entering a stage through ``record_round``
        would leave only two waves before the cut, so a plan the prompt says has
        three rounds per stage would be halted on its second — and the identical
        stage driven from the Slack handler, which has no stage loop, would get
        its third. This fails the moment stage entry starts spending a round
        again.
        """
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot()
        box = _stage_turns(monkeypatch, extra_rounds_per_stage=MAX_STAGE_ROUNDS - 1)

        await _stage_loop(_make_state(), slot, auto_run=True)

        assert _stages_run(box) == 3, (
            "a stage was cut one wave early: something is spending a round that "
            "is not a spawn wave"
        )
        assert "spawn rounds" not in _assistant_text(slot)


class TestAttendedPlansAreNotHalted:
    """The cap bounds UNATTENDED runs; an attended stage at its budget gets Go."""

    @pytest.mark.asyncio
    async def test_attended_stage_at_the_cap_still_offers_go(self, monkeypatch):
        """RED BEFORE: `_halt_plan` fired, said "Auto-run stopped", and skipped the Go row."""
        from kiro_crew.dashboard.chat import _stage_loop

        slot = _make_slot()
        slot._auto_run = False
        box = _stage_turns(monkeypatch, extra_rounds_per_stage=MAX_STAGE_ROUNDS)

        await _stage_loop(_make_state(), slot, auto_run=False)

        assert _stages_run(box) == 1, "an attended Go runs exactly one stage"
        text = _assistant_text(slot)
        assert "[OPTION: Go | Go All | Cancel]" in text, (
            "the step-through was stranded: no Go row after a stage that finished "
            "within its allowed budget"
        )
        assert "Auto-run stopped" not in text, "a plan never in auto-run was told it stopped"


class TestEntryDoesNotSpendARound:
    """The tracker-level contract the loop depends on."""

    def test_start_stage_registers_the_stage_at_zero_rounds(self):
        tracker = OrchestrationTracker(stage_timeout_seconds=1800)
        tracker.start_stage(1)

        assert tracker.round_count(1) == 0, "entering a stage spent a spawn round"
        assert tracker.current_stage == 1, (
            "the stage was not registered, so the Slack handler would record its "
            "waves against the wrong stage and the loop would resume at the wrong one"
        )
        assert tracker.round_limit_reached(1) is False

    def test_the_full_budget_is_available_after_entry(self):
        tracker = OrchestrationTracker(stage_timeout_seconds=1800)
        tracker.start_stage(1)

        for _ in range(MAX_STAGE_ROUNDS - 1):
            assert tracker.round_limit_reached(1) is False
            tracker.record_round(1)

        assert (
            tracker.round_limit_reached(1) is False
        ), f"capped after {MAX_STAGE_ROUNDS - 1} waves; the promise is {MAX_STAGE_ROUNDS}"
        assert tracker.record_round(1) is True
