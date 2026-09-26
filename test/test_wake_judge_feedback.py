"""The wake judge's calibration feedback loop.

Four things are under test and they are separable on purpose: the deterministic
labelling rule, the three states a verdict row can be in, the retroactive pass that
credits a run of suppressed verdicts to the delivery that followed them, and the
elapsed-time context the state carries.

The three states matter most, because two of them look alike and are not. A verdict
that WITHHELD the turn is eligible for a `missed` label. A verdict whose turn was
CONFIRMED delivered is eligible for `owner_acted`. A verdict that decided to wake but
whose fire was refused, cancelled or lost is neither, and labelling it would hand one
turn's actions to a verdict that never produced it.

Every case here drives pure functions with literals. The service wiring is exercised
through ``AutoNudgeService`` only where the behaviour IS the wiring -- a loop record
that survives a round trip, and the turn-completion hook labelling the delivery it
ends.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import threading
import time
from pathlib import Path

import pytest

from kiro_crew import autonudge_judge as judge
from kiro_crew import irq
from kiro_crew.autonudge import (
    _MAX_QUIET_STREAK,
    AutoNudgeService,
    NudgeLoop,
    _bounded_judge_recent,
)
from kiro_crew.config.sections import DecisionsConfig, NudgeWakeConfig
from kiro_crew.decisions import gate as decisions_gate
from kiro_crew.decisions import log as decisions_log
from kiro_crew.decisions.points import nudge_wake as point
from kiro_crew.monitoring.models import MonitorState


def _verdict(outcome: irq.Outcome) -> irq.Verdict:
    return irq.Verdict(outcome, body="")


def _suppressed(at: float, row_id: str = "", answered: bool = True) -> dict:
    """A stored row for a verdict that withheld the turn."""
    row: dict = {"outcome": "quiet", "at": at, "suppressed": True, "answered": answered}
    if row_id:
        row["id"] = row_id
    return row


def _delivered(at: float, row_id: str = "", outcome: str = "wake") -> dict:
    """A stored row for a verdict whose turn was confirmed delivered."""
    row: dict = {"outcome": outcome, "at": at, "delivered": True, "answered": True}
    if row_id:
        row["id"] = row_id
    return row


def _forfeited(at: float, row_id: str = "") -> dict:
    """A stored row for a delivery whose turn this process never saw."""
    row = _delivered(at, row_id)
    row["forfeited"] = True
    return row


class TestOwnerActed:
    """The one function that decides whether a woken turn did anything."""

    def test_a_tool_call_is_action(self):
        assert judge.owner_acted(1, "") is True

    def test_many_tool_calls_are_action(self):
        assert judge.owner_acted(12, "ok") is True

    def test_no_tools_and_a_short_reply_is_the_quiet_shape(self):
        assert judge.owner_acted(0, "Nothing new since the last tick.") is False

    def test_no_tools_and_an_empty_reply_is_the_quiet_shape(self):
        assert judge.owner_acted(0, "") is False

    def test_whitespace_only_reply_is_the_quiet_shape(self):
        assert judge.owner_acted(0, "   \n\t  ") is False

    def test_a_long_reply_with_no_tools_is_action(self):
        assert judge.owner_acted(0, "x" * (judge.QUIET_REPLY_MAX_CHARS + 1)) is True

    def test_a_reply_exactly_at_the_bound_is_the_quiet_shape(self):
        # The bound is inclusive: at the limit the reply is still a quiet cycle.
        assert judge.owner_acted(0, "x" * judge.QUIET_REPLY_MAX_CHARS) is False

    def test_leading_whitespace_does_not_push_a_reply_over_the_bound(self):
        padded = " " * 50 + "x" * judge.QUIET_REPLY_MAX_CHARS + " " * 50
        assert judge.owner_acted(0, padded) is False

    @pytest.mark.parametrize(
        "reply",
        [
            "see https://github.com/o/r/pull/1",
            "http://localhost:7777/schedule",
        ],
    )
    def test_a_link_with_no_tools_is_action(self, reply):
        assert judge.owner_acted(0, reply) is True

    def test_an_unknown_tool_count_reads_as_action(self):
        # The count is the strong half of the rule, so without it the honest answer
        # is the one that does not teach the judge to suppress.
        assert judge.owner_acted(None, "") is True

    @pytest.mark.parametrize("bad", [True, False, "3", 1.5, object()])
    def test_a_non_count_reads_as_action(self, bad):
        assert judge.owner_acted(bad, "") is True

    def test_a_non_string_reply_does_not_raise(self):
        assert judge.owner_acted(0, None) is False
        assert judge.owner_acted(0, 17) is False

    def test_the_reading_takes_its_boolean_from_owner_acted(self, monkeypatch):
        calls: list = []

        def _owner_acted(*args, **kwargs):
            calls.append((args, kwargs))
            return False

        monkeypatch.setattr(judge, "owner_acted", _owner_acted)
        reply = "  a reply that the shared rule alone classifies  "
        reading = judge.owner_action_reading(1, reply, reply_flushed=True)

        assert reading == (False, 1, len(reply.strip()))
        assert calls == [((1, reply), {"reply_flushed": True})]


class TestVerdictEntry:
    """What the tick records, and the one thing it deliberately does not."""

    def test_it_keeps_the_previous_record_shape(self):
        row = judge.verdict_entry(_verdict(irq.Outcome.QUIET), 3, suppressed=True, answered=True)
        # ``verdict_record``'s own three fields are still present and still mean the
        # same thing, so a reader of the older field is not broken by the new one.
        assert row["outcome"] == "quiet"
        assert row["evidence_items"] == 3
        assert isinstance(row["at"], float)

    def test_a_wake_verdict_is_neither_suppressed_nor_delivered(self):
        # The third state, and the whole point of it: the tick decided to wake, and
        # nothing has gone out yet.
        row = judge.verdict_entry(_verdict(irq.Outcome.WAKE), 1, suppressed=False, answered=True)
        assert "suppressed" not in row
        assert "delivered" not in row

    def test_a_quiet_verdict_records_that_it_withheld_the_turn(self):
        row = judge.verdict_entry(_verdict(irq.Outcome.QUIET), 1, suppressed=True, answered=True)
        assert row["suppressed"] is True
        assert "delivered" not in row

    def test_it_records_whether_the_judge_was_asked(self):
        asked = judge.verdict_entry(_verdict(irq.Outcome.QUIET), 2, suppressed=True, answered=True)
        unasked = judge.verdict_entry(
            _verdict(irq.Outcome.QUIET), 0, suppressed=True, answered=False
        )
        assert asked["answered"] is True
        assert unasked["answered"] is False

    def test_it_carries_the_join_key_when_given_one(self):
        row = judge.verdict_entry(
            _verdict(irq.Outcome.WAKE), 0, suppressed=False, answered=True, verdict_id="ab12"
        )
        assert row["id"] == "ab12"

    def test_it_omits_the_join_key_when_given_none(self):
        row = judge.verdict_entry(_verdict(irq.Outcome.WAKE), 0, suppressed=False, answered=True)
        assert "id" not in row

    def test_an_oversized_join_key_is_clipped(self):
        row = judge.verdict_entry(
            _verdict(irq.Outcome.WAKE),
            0,
            suppressed=False,
            answered=True,
            verdict_id="z" * 500,
        )
        assert len(row["id"]) == judge.MAX_VERDICT_ID_CHARS

    def test_a_verdict_with_no_outcome_folds_to_unknown(self):
        row = judge.verdict_entry(object(), 0, suppressed=False, answered=False)
        assert row["outcome"] == "unknown"

    def test_new_verdict_ids_do_not_repeat(self):
        assert len({judge.new_verdict_id() for _ in range(50)}) == 50


class TestStoredWindow:
    """The stored window is sized by the streak floor, not by the request window."""

    def test_it_holds_a_full_streak_and_the_delivery_that_labels_it(self):
        # A full streak is the floor's worth of suppressions followed by the floor
        # delivery. A store smaller than that evicts the earliest suppressions before
        # their label arrives, and those decision rows never get a `missed`.
        assert judge.MAX_STORED_VERDICTS == _MAX_QUIET_STREAK + 1

    def test_it_is_wider_than_the_window_the_judge_reads(self):
        assert judge.MAX_STORED_VERDICTS > point.MAX_RECENT_VERDICTS

    def test_a_full_default_streak_keeps_every_suppression(self):
        history: list = []
        for index in range(_MAX_QUIET_STREAK - 1):
            history = judge.append_verdict(history, _suppressed(float(index), f"q{index}"))
        history = judge.append_verdict(history, _delivered(99.0, "floor", outcome="quiet"))
        labelled, changed = judge.label_latest_delivery(history, acted=True)
        assert len(labelled) == _MAX_QUIET_STREAK
        assert sum(1 for row in labelled if row.get("missed") is True) == _MAX_QUIET_STREAK - 1
        assert len(changed) == _MAX_QUIET_STREAK

    def test_it_keeps_the_newest_rows_and_drops_the_oldest(self):
        history: list = []
        for index in range(judge.MAX_STORED_VERDICTS + 4):
            history = judge.append_verdict(history, _suppressed(float(index)))
        assert len(history) == judge.MAX_STORED_VERDICTS
        assert [row["at"] for row in history] == [
            float(index) for index in range(4, judge.MAX_STORED_VERDICTS + 4)
        ]

    def test_it_tolerates_a_ruined_history(self):
        assert judge.append_verdict([None, 7, "x"], {"outcome": "wake"}) == [{"outcome": "wake"}]

    def test_it_does_not_mutate_the_list_it_was_given(self):
        original: list = [{"outcome": "quiet"}]
        judge.append_verdict(original, {"outcome": "wake"})
        assert original == [{"outcome": "quiet"}]


class TestConfirmDelivery:
    """Only the fire path may say a turn went out."""

    def test_it_stamps_the_undecided_row(self):
        history = [
            _suppressed(1.0, "q1"),
            judge.verdict_entry(
                _verdict(irq.Outcome.WAKE), 2, suppressed=False, answered=True, verdict_id="d1"
            ),
        ]
        stamped, changed = judge.confirm_delivery(history)
        assert changed is True
        assert stamped[-1]["delivered"] is True
        assert stamped[0].get("delivered") is None

    def test_a_suppressed_newest_row_is_not_a_delivery(self):
        stamped, changed = judge.confirm_delivery([_suppressed(1.0, "q1")])
        assert changed is False
        assert "delivered" not in stamped[0]

    def test_an_already_stamped_row_is_not_stamped_twice(self):
        stamped, changed = judge.confirm_delivery([_delivered(1.0, "d1")])
        assert changed is False

    def test_a_forfeited_row_is_a_permanent_boundary(self):
        stamped, changed = judge.confirm_delivery([_forfeited(1.0, "d1")])
        assert changed is False
        assert stamped[0]["delivered"] is True
        assert stamped[0]["forfeited"] is True

    def test_a_re_owed_delivery_stamps_its_newer_row(self):
        pending = judge.verdict_entry(
            _verdict(irq.Outcome.WAKE), 1, suppressed=False, answered=True, verdict_id="d2"
        )
        stamped, changed = judge.confirm_delivery([_forfeited(1.0, "d1"), pending])
        assert changed is True
        assert stamped[0]["forfeited"] is True
        assert stamped[1]["delivered"] is True

        labelled, labels = judge.label_latest_delivery(stamped, acted=True)
        assert "owner_acted" not in labelled[0]
        assert labelled[1]["owner_acted"] is True
        assert [row["id"] for row in labels] == ["d2"]

    def test_it_does_not_reach_past_a_decided_row_to_an_older_wake(self):
        # An older wake whose fire was lost must not be credited to this delivery.
        history = [
            judge.verdict_entry(
                _verdict(irq.Outcome.WAKE), 1, suppressed=False, answered=True, verdict_id="lost"
            ),
            _suppressed(2.0, "q1"),
        ]
        stamped, changed = judge.confirm_delivery(history)
        assert changed is False
        assert "delivered" not in stamped[0]

    def test_an_empty_history_stamps_nothing(self):
        assert judge.confirm_delivery([]) == ([], False)
        assert judge.confirm_delivery(None) == ([], False)

    def test_it_does_not_mutate_the_history_it_was_given(self):
        original = [
            judge.verdict_entry(
                _verdict(irq.Outcome.WAKE), 1, suppressed=False, answered=True, verdict_id="d1"
            )
        ]
        judge.confirm_delivery(original)
        assert "delivered" not in original[0]


class TestMarkFired:
    """A suppression the loop could not persist is withdrawn, not converted."""

    def test_it_withdraws_the_newest_suppression(self):
        rows = judge.mark_fired([_suppressed(1.0, "q1")])
        assert "suppressed" not in rows[0]
        # Undecided, not delivered: the fire path still has to confirm it.
        assert "delivered" not in rows[0]

    def test_a_withdrawn_row_is_then_stampable(self):
        rows = judge.mark_fired([_suppressed(1.0, "q1")])
        stamped, changed = judge.confirm_delivery(rows)
        assert changed is True
        assert stamped[0]["delivered"] is True

    def test_an_empty_history_does_not_raise(self):
        assert judge.mark_fired([]) == []
        assert judge.mark_fired(None) == []


class TestLabelLatestDelivery:
    """The retroactive pass. A delivery's label is also the verdict on the quiets."""

    def _history(self) -> list:
        # An earlier labelled delivery, three suppressed quiets, then the delivery
        # being judged.
        earlier = _delivered(1.0, "d0")
        earlier["owner_acted"] = True
        return [
            earlier,
            _suppressed(2.0, "q1"),
            _suppressed(3.0, "q2"),
            _suppressed(4.0, "q3"),
            _delivered(5.0, "d1"),
        ]

    def test_an_acting_turn_marks_every_quiet_behind_it_missed(self):
        history, changed = judge.label_latest_delivery(self._history(), acted=True)
        assert history[-1]["owner_acted"] is True
        assert [row["missed"] for row in history[1:4]] == [True, True, True]
        assert {row["id"] for row in changed} == {"d1", "q1", "q2", "q3"}

    def test_an_idle_turn_clears_every_quiet_behind_it(self):
        history, changed = judge.label_latest_delivery(self._history(), acted=False)
        assert history[-1]["owner_acted"] is False
        assert [row["missed"] for row in history[1:4]] == [False, False, False]
        assert len(changed) == 4

    def test_the_pass_stops_at_the_previous_delivery(self):
        history, _ = judge.label_latest_delivery(self._history(), acted=True)
        # The earlier delivery keeps its own label and never acquires a ``missed``.
        assert history[0]["owner_acted"] is True
        assert "missed" not in history[0]

    def test_a_second_turn_complete_relabels_nothing(self):
        once, first = judge.label_latest_delivery(self._history(), acted=True)
        twice, second = judge.label_latest_delivery(once, acted=False)
        assert second == []
        assert twice == once
        assert first != []

    def test_an_unstamped_wake_takes_no_label(self):
        # THE finding: a verdict that decided to wake but whose fire was refused,
        # cancelled or lost is not a delivery, so an unrelated turn completing must
        # not label it.
        history = [
            judge.verdict_entry(
                _verdict(irq.Outcome.WAKE), 2, suppressed=False, answered=True, verdict_id="lost"
            )
        ]
        labelled, changed = judge.label_latest_delivery(history, acted=True)
        assert changed == []
        assert "owner_acted" not in labelled[0]

    def test_an_unstamped_wake_takes_no_missed_either(self):
        # It withheld nothing, so the next delivery's label is not its verdict.
        history = [
            judge.verdict_entry(
                _verdict(irq.Outcome.WAKE), 2, suppressed=False, answered=True, verdict_id="lost"
            ),
            _delivered(3.0, "d1"),
        ]
        labelled, changed = judge.label_latest_delivery(history, acted=True)
        assert "missed" not in labelled[0]
        assert [row["id"] for row in changed] == ["d1"]

    def test_a_verdict_the_judge_never_answered_takes_no_missed(self):
        # A tick that read nothing new returns a quiet without asking the judge. It
        # sat on no evidence, so scoring it as a suppression the judge got wrong
        # would bias the very curve the thresholds are read from.
        history = [
            _suppressed(1.0, "unasked", answered=False),
            _suppressed(2.0, "asked"),
            _delivered(3.0, "d1"),
        ]
        labelled, changed = judge.label_latest_delivery(history, acted=True)
        assert "missed" not in labelled[0]
        assert labelled[1]["missed"] is True
        assert {row["id"] for row in changed} == {"d1", "asked"}

    def test_a_forfeited_delivery_is_never_labelled(self):
        # Its turn belongs to a process that is gone, so no ``owner_acted`` for it can
        # be read truthfully.
        labelled, changed = judge.label_latest_delivery([_forfeited(1.0, "lost")], acted=True)
        assert changed == []
        assert "owner_acted" not in labelled[0]

    def test_a_forfeited_delivery_ends_the_cycle_behind_it(self):
        # A lost delivery is a cycle boundary. The quiets it suppressed belong to ITS
        # cycle, so the label of the delivery after it is not their verdict.
        history = [
            _suppressed(1.0, "q1"),
            _forfeited(2.0, "lost"),
            _delivered(3.0, "d1"),
        ]
        labelled, changed = judge.label_latest_delivery(history, acted=True)
        assert labelled[-1]["owner_acted"] is True
        assert "missed" not in labelled[0]
        assert "owner_acted" not in labelled[1]
        assert [row["id"] for row in changed] == ["d1"]

    def test_a_forfeited_boundary_refuses_every_later_turn(self):
        history = [_suppressed(1.0, "q1"), _suppressed(2.0, "q2"), _forfeited(3.0, "lost")]

        for acted in (True, False, True, False):
            history, stamped = judge.confirm_delivery(history)
            assert stamped is False
            history, changed = judge.label_latest_delivery(history, acted=acted)
            assert changed == []

        assert "owner_acted" not in history[-1]
        assert all("missed" not in row for row in history[:-1])

    def test_a_wake_whose_fire_was_lost_ends_the_cycle_behind_it(self):
        # An unstamped wake is a delivery that was owed and never went out. The quiets
        # before it were suppressed inside ITS cycle, so the label of the delivery that
        # follows it is not their verdict.
        lost = judge.verdict_entry(
            _verdict(irq.Outcome.WAKE), 2, suppressed=False, answered=True, verdict_id="lost"
        )
        history = [_suppressed(1.0, "q1"), lost, _delivered(3.0, "d1")]
        labelled, changed = judge.label_latest_delivery(history, acted=True)
        assert labelled[-1]["owner_acted"] is True
        assert "missed" not in labelled[0]
        assert [row["id"] for row in changed] == ["d1"]

    def test_an_unanswered_delivery_takes_no_owner_acted(self):
        # A tick that returned a verdict without asking the judge spends the turn, but
        # the judge earned nothing by it: labelling it would feed the hit rate a
        # verdict that was never given.
        row = _delivered(2.0, "unasked")
        row["answered"] = False
        labelled, changed = judge.label_latest_delivery([row], acted=True)
        assert changed == []
        assert "owner_acted" not in labelled[0]

    def test_an_unanswered_delivery_also_ends_the_cycle_behind_it(self):
        unasked = _delivered(3.0, "unasked")
        unasked["answered"] = False
        labelled, changed = judge.label_latest_delivery(
            [_suppressed(1.0, "q1"), _delivered(2.0, "d0"), unasked], acted=True
        )
        assert changed == []
        assert "missed" not in labelled[0]
        assert "owner_acted" not in labelled[1]

    def test_a_delivery_with_no_quiets_behind_it_labels_only_itself(self):
        earlier = _delivered(1.0, "a")
        earlier["owner_acted"] = False
        history, changed = judge.label_latest_delivery([earlier, _delivered(2.0, "b")], acted=True)
        assert len(changed) == 1
        assert changed[0]["id"] == "b"
        assert history[0]["owner_acted"] is False

    def test_a_history_with_no_delivery_changes_nothing(self):
        rows = [_suppressed(1.0, "q1")]
        history, changed = judge.label_latest_delivery(rows, acted=True)
        assert changed == []
        assert history == rows

    def test_an_empty_history_changes_nothing(self):
        assert judge.label_latest_delivery([], acted=True) == ([], [])
        assert judge.label_latest_delivery(None, acted=False) == ([], [])

    def test_it_does_not_mutate_the_history_it_was_given(self):
        original = self._history()
        judge.label_latest_delivery(original, acted=True)
        assert "owner_acted" not in original[-1]
        assert "missed" not in original[1]

    def test_a_floor_delivery_is_labelled_like_any_other_delivery(self):
        # A quiet verdict that hit the streak floor SPENDS the turn, so it is judged
        # by what that turn did rather than counted as a suppression.
        history, changed = judge.label_latest_delivery(
            [_delivered(1.0, "floor", outcome="quiet")], acted=True
        )
        assert history[0]["owner_acted"] is True
        assert "missed" not in history[0]
        assert len(changed) == 1


class TestSinceLastWake:
    """The elapsed-time reading."""

    def test_it_measures_from_the_last_delivery(self):
        assert judge.since_last_wake_s(100.0, now_ts=460.0) == 360.0

    def test_a_loop_that_never_delivered_has_no_elapsed_time(self):
        # ``None`` and not 0.0: zero would read as a delivery this instant.
        assert judge.since_last_wake_s(0.0) is None
        assert judge.since_last_wake_s(None) is None

    @pytest.mark.parametrize("bad", [True, False, "60", float("nan"), float("inf"), -5.0])
    def test_an_unusable_stamp_has_no_elapsed_time(self, bad):
        assert judge.since_last_wake_s(bad) is None

    def test_a_clock_that_went_backwards_reads_as_now(self):
        assert judge.since_last_wake_s(500.0, now_ts=100.0) == 0.0


class TestRecentForState:
    """Stored absolute times become ages; nothing else is touched."""

    def test_it_turns_stored_times_into_ages(self):
        rows = judge.recent_for_state([{"outcome": "quiet", "at": 40.0}], now_ts=100.0)
        assert rows[0]["age_s"] == 60.0

    def test_a_row_with_no_time_ages_to_zero(self):
        rows = judge.recent_for_state([{"outcome": "wake"}], now_ts=100.0)
        assert rows[0]["age_s"] == 0.0

    def test_it_skips_rows_that_are_not_mappings(self):
        assert judge.recent_for_state([None, "x", {"outcome": "wake"}], now_ts=1.0) == [
            {"outcome": "wake", "age_s": 0.0}
        ]

    def test_it_does_not_mutate_the_stored_rows(self):
        stored = [{"outcome": "quiet", "at": 40.0}]
        judge.recent_for_state(stored, now_ts=100.0)
        assert "age_s" not in stored[0]


class TestRecentVerdictScreening:
    """What the point will and will not send."""

    def test_it_carries_the_outcome_the_age_the_count_and_one_label(self):
        item = point.recent_verdict_item(
            {
                "outcome": "quiet",
                "age_s": 30.0,
                "evidence_items": 4,
                "missed": True,
                "id": "q1",
            }
        )
        assert item == {"outcome": "quiet", "age_s": 30.0, "evidence_items": 4, "missed": True}

    def test_it_keeps_the_field_last_verdict_carried_for_meaning(self):
        # ``evidence_items`` is what lets a judge tell "still nothing" from "the same
        # thing again", so the history that supersedes ``last_verdict`` must carry it.
        item = point.recent_verdict_item({"outcome": "quiet", "age_s": 1.0, "evidence_items": 7})
        assert item["evidence_items"] == 7

    def test_a_missing_count_is_simply_absent(self):
        item = point.recent_verdict_item({"outcome": "quiet", "age_s": 1.0})
        assert "evidence_items" not in item

    @pytest.mark.parametrize("bad", [True, -1, 1.5, "4", None])
    def test_an_unusable_count_is_dropped(self, bad):
        item = point.recent_verdict_item({"outcome": "quiet", "age_s": 1.0, "evidence_items": bad})
        assert "evidence_items" not in item

    def test_the_join_key_never_reaches_the_wire(self):
        item = point.recent_verdict_item({"outcome": "wake", "age_s": 1.0, "id": "secret-key"})
        assert "id" not in item

    def test_bookkeeping_fields_never_reach_the_wire(self):
        item = point.recent_verdict_item(
            {
                "outcome": "wake",
                "age_s": 1.0,
                "delivered": True,
                "suppressed": False,
                "answered": True,
                "at": 1700000000.0,
                "target": "chat-1",
            }
        )
        assert set(item) == {"outcome", "age_s"}

    def test_a_row_naming_no_outcome_is_dropped(self):
        assert point.recent_verdict_item({"age_s": 1.0}) is None
        assert point.recent_verdict_item({"outcome": "", "age_s": 1.0}) is None
        assert point.recent_verdict_item({"outcome": 7}) is None
        assert point.recent_verdict_item(None) is None

    def test_only_one_label_survives_a_row_claiming_both(self):
        item = point.recent_verdict_item(
            {"outcome": "quiet", "age_s": 1.0, "owner_acted": True, "missed": False}
        )
        assert len([key for key in item if key in ("owner_acted", "missed")]) == 1

    def test_a_non_boolean_label_is_dropped(self):
        item = point.recent_verdict_item({"outcome": "quiet", "age_s": 1.0, "missed": "yes"})
        assert set(item) == {"outcome", "age_s"}

    def test_an_unlabelled_row_carries_neither_label(self):
        item = point.recent_verdict_item({"outcome": "wake", "age_s": 2.0})
        assert set(item) == {"outcome", "age_s"}

    def test_an_oversized_outcome_name_is_clipped(self):
        item = point.recent_verdict_item({"outcome": "q" * 400, "age_s": 0.0})
        assert len(item["outcome"]) == 32

    def test_the_window_keeps_the_newest_rows(self):
        rows = [
            {"outcome": "quiet", "age_s": float(age)}
            for age in range(point.MAX_RECENT_VERDICTS + 5)
        ]
        screened = point.screen_recent_verdicts(rows)
        assert len(screened) == point.MAX_RECENT_VERDICTS
        assert [row["age_s"] for row in screened] == [
            float(age) for age in range(point.MAX_RECENT_VERDICTS)
        ]

    def test_it_orders_newest_first(self):
        screened = point.screen_recent_verdicts(
            [{"outcome": "a", "age_s": 90.0}, {"outcome": "b", "age_s": 5.0}]
        )
        assert [row["outcome"] for row in screened] == ["b", "a"]


class TestBuildStateFeedbackFields:
    """The two new loop fields, and the history superseding the single verdict."""

    def test_the_elapsed_fields_ride_on_the_loop_object(self):
        state = point.build_state(
            "watch it",
            evidence=[{"source": "s", "kind": point.KIND_TRANSCRIPT_TAIL, "age_s": 1, "text": "x"}],
            since_last_wake_s=420.0,
            quiet_streak=3,
        )
        assert state["loop"]["since_last_wake_s"] == 420.0
        assert state["loop"]["quiet_streak"] == 3

    def test_the_instruction_is_still_the_first_loop_key(self):
        # Additive, so a reader of the existing shape sees no reordering.
        state = point.build_state("watch it", since_last_wake_s=1.0, quiet_streak=0)
        assert list(state["loop"])[0] == "instruction"

    @pytest.mark.parametrize("bad", [None, -1.0, float("nan"), float("inf"), True, "60"])
    def test_an_unusable_elapsed_reading_is_omitted(self, bad):
        state = point.build_state("watch it", since_last_wake_s=bad)
        assert "since_last_wake_s" not in state["loop"]

    @pytest.mark.parametrize("bad", [None, -1, True, 1.5, "3"])
    def test_an_unusable_streak_is_omitted(self, bad):
        state = point.build_state("watch it", quiet_streak=bad)
        assert "quiet_streak" not in state["loop"]

    def test_a_zero_streak_is_sent_because_zero_is_a_reading(self):
        state = point.build_state("watch it", quiet_streak=0)
        assert state["loop"]["quiet_streak"] == 0

    def test_the_history_supersedes_the_single_verdict(self):
        state = point.build_state(
            "watch it",
            last_verdict={"outcome": "quiet", "evidence_items": 1, "at": 1.0},
            recent_verdicts=[
                {"outcome": "quiet", "age_s": 30.0, "evidence_items": 1, "missed": False}
            ],
        )
        assert "last_verdict" not in state
        assert state["recent_verdicts"] == [
            {"outcome": "quiet", "age_s": 30.0, "evidence_items": 1, "missed": False}
        ]

    def test_superseding_loses_nothing_last_verdict_carried(self):
        # Both fields ``last_verdict`` holds for meaning survive in the newest row, so
        # the supersede is a widening rather than a trade.
        single = {"outcome": "quiet", "evidence_items": 3, "at": 100.0}
        state = point.build_state(
            "watch it",
            last_verdict=single,
            recent_verdicts=judge.recent_for_state([dict(single)], now_ts=160.0),
        )
        newest = state["recent_verdicts"][0]
        assert newest["outcome"] == single["outcome"]
        assert newest["evidence_items"] == single["evidence_items"]
        assert newest["age_s"] == 60.0

    def test_the_single_verdict_still_goes_out_with_no_history(self):
        state = point.build_state(
            "watch it",
            last_verdict={"outcome": "quiet", "evidence_items": 1, "at": 1.0},
        )
        assert state["last_verdict"]["outcome"] == "quiet"
        assert "recent_verdicts" not in state

    def test_the_history_survives_the_char_budget(self, monkeypatch):
        # Evidence is what the budget spends; the history is what the judge needs to
        # read its own hit rate, and a history that thinned out on a busy tick would
        # look like a better rate than the loop earned.
        monkeypatch.setattr(point, "MAX_STATE_CHARS", 900)
        fat = [
            {
                "source": f"session:chat-{index}",
                "kind": point.KIND_TRANSCRIPT_TAIL,
                "age_s": float(index),
                "text": "y" * 400,
            }
            for index in range(6)
        ]
        trace: dict = {}
        state = point.build_state(
            "watch it",
            evidence=fat,
            recent_verdicts=[
                {"outcome": "quiet", "age_s": float(age), "missed": False} for age in range(5)
            ],
            trace=trace,
        )
        assert len(state["recent_verdicts"]) == 5
        assert trace["dropped"] > 0
        assert len(json.dumps(state, ensure_ascii=False)) <= 900

    def test_the_trace_reports_how_much_history_was_sent(self):
        trace: dict = {}
        point.build_state(
            "watch it",
            recent_verdicts=[{"outcome": "quiet", "age_s": 1.0}],
            trace=trace,
        )
        assert trace["recent_verdicts"] == 1

    def test_persisted_absurd_counts_are_clamped_without_dropping_rows(self, tmp_path):
        huge = 10**3999
        stored_rows = [
            {
                "outcome": "quiet",
                "at": float(index),
                "evidence_items": huge,
                "suppressed": True,
                "answered": True,
                "missed": False,
                "id": f"q{index}",
            }
            for index in range(point.MAX_RECENT_VERDICTS)
        ]
        payload = {
            "loops": [
                {
                    "id": "loop-1",
                    "slot_key": "chat-1",
                    "message": "watch it",
                    "judge_quiet_streak": huge,
                    "judge_recent_verdicts": stored_rows,
                }
            ]
        }
        (tmp_path / "autonudge.json").write_text(json.dumps(payload), encoding="utf-8")

        service = AutoNudgeService(base_dir=tmp_path)
        try:
            service._load()
            loaded = service._loops["loop-1"]
        finally:
            service.stop()

        assert loaded.judge_quiet_streak == _MAX_QUIET_STREAK
        assert len(loaded.judge_recent_verdicts) == point.MAX_RECENT_VERDICTS
        assert {row["evidence_items"] for row in loaded.judge_recent_verdicts} == {
            point.MAX_EVIDENCE_ITEMS
        }

        direct = point.recent_verdict_item(
            {"outcome": "quiet", "age_s": 1.0, "evidence_items": huge}
        )
        assert direct["evidence_items"] == point.MAX_EVIDENCE_ITEMS
        assert (
            point.build_state("watch it", quiet_streak=huge)["loop"]["quiet_streak"]
            == _MAX_QUIET_STREAK
        )

        state = point.build_state(
            loaded.message,
            recent_verdicts=judge.recent_for_state(
                loaded.judge_recent_verdicts,
                now_ts=100.0,
            ),
            quiet_streak=loaded.judge_quiet_streak,
        )
        assert len(state["recent_verdicts"]) == point.MAX_RECENT_VERDICTS
        assert len(json.dumps(state, ensure_ascii=False)) <= point.MAX_STATE_CHARS

    def test_a_persisted_negative_count_is_floored_not_retained(self, tmp_path):
        # A ceiling alone is half a bound: on the wire a 4,000-digit negative costs
        # exactly what the positive does, and this store is agent-writable. A count
        # below zero is not a count, so the floor reads it as none rather than keeping
        # the digits. The row itself survives -- one crafted field must not erase it.
        huge_negative = -(10**3999)
        stored_rows = [
            {
                "outcome": "quiet",
                "at": 1.0,
                "evidence_items": huge_negative,
                "suppressed": True,
                "answered": True,
                "id": "q1",
            }
        ]
        payload = {
            "loops": [
                {
                    "id": "loop-1",
                    "slot_key": "chat-1",
                    "message": "watch it",
                    "judge_quiet_streak": huge_negative,
                    "judge_recent_verdicts": stored_rows,
                }
            ]
        }
        (tmp_path / "autonudge.json").write_text(json.dumps(payload), encoding="utf-8")

        service = AutoNudgeService(base_dir=tmp_path)
        try:
            service._load()
            loaded = service._loops["loop-1"]
        finally:
            service.stop()

        assert loaded.judge_quiet_streak == 0
        assert len(loaded.judge_recent_verdicts) == 1
        assert loaded.judge_recent_verdicts[0]["evidence_items"] == 0
        assert loaded.judge_recent_verdicts[0]["outcome"] == "quiet"


class TestJudgeTickReportsWhetherItAsked:
    """``answered`` separates the judge's own verdicts from the loop's."""

    def test_a_tick_with_no_new_evidence_did_not_ask(self):
        trace: dict = {}
        verdict = asyncio.run(point.judge_tick("watch it", evidence=None, trace=trace))
        assert verdict.outcome is irq.Outcome.QUIET
        assert trace["answered"] is False

    def test_a_tick_with_a_dropped_target_did_not_ask(self):
        trace: dict = {}
        verdict = asyncio.run(point.judge_tick("watch it", dropped=1, trace=trace))
        assert verdict.outcome is irq.Outcome.FALLBACK
        assert trace["answered"] is False

    def test_a_refused_decision_append_keeps_no_join_key(self, monkeypatch):
        class _Config:
            decisions = DecisionsConfig(nudge_wake=NudgeWakeConfig(provider="llm"))

        class _InvalidOracle:
            async def ask(self, *_args, **_kwargs):
                return None

        captured_receipt: dict = {}
        original_decide = decisions_gate.decide

        async def _decide(*args, receipt, **kwargs):
            answers = await original_decide(*args, receipt=receipt, **kwargs)
            captured_receipt.update(receipt)
            return answers

        monkeypatch.setattr(decisions_gate, "_snapshot", lambda: _Config())
        monkeypatch.setattr(decisions_gate, "_consented_for", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(decisions_gate, "_capability_denied", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(decisions_gate, "_oracle", lambda *_args, **_kwargs: _InvalidOracle())
        monkeypatch.setattr(decisions_log, "append", lambda _row, **_kwargs: False)
        monkeypatch.setattr(point.core, "decide", _decide)

        trace: dict = {}
        verdict = asyncio.run(
            point.judge_tick(
                "watch it",
                evidence=[
                    {
                        "source": "session:chat-1",
                        "kind": point.KIND_TRANSCRIPT_TAIL,
                        "age_s": 1.0,
                        "text": "new evidence",
                    }
                ],
                trace=trace,
            )
        )
        row = judge.verdict_entry(
            verdict,
            trace["evidence_items"],
            suppressed=False,
            answered=trace["answered"],
            verdict_id="v1" if trace["answered"] else "",
        )
        assert captured_receipt["row_written"] is False
        assert trace["answered"] is False
        assert "id" not in row

    def test_a_committed_row_keeps_the_join_key_when_sweep_outlives_budget(self, monkeypatch):
        class _Config:
            decisions = DecisionsConfig(nudge_wake=NudgeWakeConfig(provider="llm"))

        class _InvalidOracle:
            async def ask(self, *_args, **_kwargs):
                return None

        captured_receipt: dict = {}
        original_decide = decisions_gate.decide

        async def _decide(*args, receipt, **kwargs):
            answers = await original_decide(*args, receipt=receipt, **kwargs)
            captured_receipt.update(receipt)
            return answers

        monkeypatch.setattr(decisions_gate, "_snapshot", lambda: _Config())
        monkeypatch.setattr(decisions_gate, "_consented_for", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(decisions_gate, "_capability_denied", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(decisions_gate, "_oracle", lambda *_args, **_kwargs: _InvalidOracle())
        monkeypatch.setattr(decisions_gate, "_LOG_BUDGET_SECS", 0.01)
        monkeypatch.setattr(decisions_log, "sweep_expired", lambda: time.sleep(0.05))
        monkeypatch.setattr(point.core, "decide", _decide)

        trace: dict = {}
        verdict = asyncio.run(
            point.judge_tick(
                "watch it",
                evidence=[
                    {
                        "source": "session:chat-1",
                        "kind": point.KIND_TRANSCRIPT_TAIL,
                        "age_s": 1.0,
                        "text": "new evidence",
                    }
                ],
                trace=trace,
            )
        )
        row = judge.verdict_entry(
            verdict,
            trace["evidence_items"],
            suppressed=False,
            answered=trace["answered"],
            verdict_id="v1" if trace["answered"] else "",
        )
        assert captured_receipt["row_written"] is True
        assert trace["answered"] is True
        assert row["id"] == "v1"

    def test_a_normalized_row_committing_after_budget_keeps_its_receipt(self, monkeypatch):
        class _Config:
            decisions = DecisionsConfig(nudge_wake=NudgeWakeConfig(provider="llm"))

        class _InvalidOracle:
            async def ask(self, *_args, **_kwargs):
                return None

        captured_receipt: dict = {}
        original_decide = decisions_gate.decide
        original_append = decisions_log.append

        async def _decide(*args, receipt, **kwargs):
            answers = await original_decide(*args, receipt=receipt, **kwargs)
            captured_receipt.update(receipt)
            return answers

        def _late_normalizing_append(row, *, commit_event=None):
            time.sleep(0.07)
            return original_append(dict(row), commit_event=commit_event)

        monkeypatch.setattr(decisions_gate, "_snapshot", lambda: _Config())
        monkeypatch.setattr(decisions_gate, "_consented_for", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(decisions_gate, "_capability_denied", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(decisions_gate, "_oracle", lambda *_args, **_kwargs: _InvalidOracle())
        monkeypatch.setattr(decisions_gate, "_LOG_BUDGET_SECS", 0.05)
        monkeypatch.setattr(decisions_log, "append", _late_normalizing_append)
        monkeypatch.setattr(decisions_log, "sweep_expired", lambda: 0)
        monkeypatch.setattr(point.core, "decide", _decide)

        trace: dict = {}
        verdict = asyncio.run(
            point.judge_tick(
                "watch it",
                evidence=[
                    {
                        "source": "session:chat-1",
                        "kind": point.KIND_TRANSCRIPT_TAIL,
                        "age_s": 1.0,
                        "text": "new evidence",
                    }
                ],
                trace=trace,
            )
        )
        row = judge.verdict_entry(
            verdict,
            trace["evidence_items"],
            suppressed=False,
            answered=trace["answered"],
            verdict_id="v1" if trace["answered"] else "",
        )
        assert captured_receipt["row_written"] is True
        assert trace["answered"] is True
        assert row["id"] == "v1"

    def test_a_verdict_with_no_recorded_decision_keeps_no_join_key(self, monkeypatch):
        async def _decide(*_args, receipt, **_kwargs):
            receipt["row_written"] = False
            return None

        monkeypatch.setattr(point.core, "decide", _decide)
        trace: dict = {}
        verdict = asyncio.run(
            point.judge_tick(
                "watch it",
                evidence=[
                    {
                        "source": "session:chat-1",
                        "kind": point.KIND_TRANSCRIPT_TAIL,
                        "age_s": 1.0,
                        "text": "new evidence",
                    }
                ],
                trace=trace,
            )
        )
        row = judge.verdict_entry(
            verdict,
            trace["evidence_items"],
            suppressed=False,
            answered=trace["answered"],
            verdict_id="v1" if trace["answered"] else "",
        )
        assert trace["answered"] is False
        assert "id" not in row


class TestDecisionLogGrace:
    """Only a caller asking for a receipt waits beyond the write budget."""

    def test_only_a_receipt_request_pays_the_commit_grace(self, monkeypatch):
        class _Config:
            decisions = DecisionsConfig(nudge_wake=NudgeWakeConfig(provider="llm"))

        class _InvalidOracle:
            async def ask(self, *_args, **_kwargs):
                return None

        releases: list[threading.Event] = []
        started: list[threading.Event] = []

        def _late_append(_row, *, commit_event=None):
            # The TEST decides when this returns, so the ordering under assertion is
            # not a race: `started` says the worker thread is running, and `release`
            # is only ever set here by the test itself.
            release = threading.Event()
            started_flag = threading.Event()
            releases.append(release)
            started.append(started_flag)
            started_flag.set()
            release.wait(5.0)
            if commit_event is not None:
                commit_event.set()
            return True

        async def _await_worker(index: int) -> None:
            # A thread this call handed off may not have been scheduled yet, which on
            # some platforms outlasts the write budget. Wait for it rather than assume.
            deadline = time.monotonic() + 5.0
            while len(started) <= index or not started[index].is_set():
                assert time.monotonic() < deadline, "append worker never started"
                await asyncio.sleep(0.005)

        monkeypatch.setattr(decisions_gate, "_consented_for", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(decisions_gate, "_capability_denied", lambda *_args, **_kwargs: False)
        monkeypatch.setattr(decisions_gate, "_oracle", lambda *_args, **_kwargs: _InvalidOracle())
        monkeypatch.setattr(decisions_gate, "_LOG_BUDGET_SECS", 0.005)
        monkeypatch.setattr(decisions_gate, "_LOG_COMMIT_GRACE_SECS", 0.5)
        monkeypatch.setattr(decisions_log, "append", _late_append)

        async def drive() -> None:
            kwargs = {
                "session_key": "chat-1",
                "config": _Config(),
            }
            # Elapsed time is the only thing that can tell a call that skipped the grace
            # from one that paid it, since both return with the worker still blocked.
            # The margin is what makes it sound: the grace is 100x the budget, and the
            # bar sits halfway, so no scheduler or clock granularity can reach it.
            started_at = time.monotonic()
            await decisions_gate.decide(
                point.POINT,
                {"loop": {"instruction": "watch it"}},
                point.build_questions(),
                **kwargs,
            )
            elapsed = time.monotonic() - started_at
            assert elapsed < 0.25, "a call asking for no receipt waited for the commit"
            await _await_worker(0)
            assert len(releases) == 1
            assert releases[0].is_set() is False
            releases[0].set()
            # Let the handed-off worker retire before the loop closes.
            await asyncio.sleep(0.02)

            receipt: dict = {}
            # Driven as a task so the worker can be freed WHILE this call is inside the
            # grace: that is the property under test, and it cannot be observed by a
            # release that only happens after the call has already returned.
            pending = asyncio.create_task(
                decisions_gate.decide(
                    point.POINT,
                    {"loop": {"instruction": "watch it"}},
                    point.build_questions(),
                    receipt=receipt,
                    **kwargs,
                )
            )
            await _await_worker(1)
            assert len(releases) == 2
            releases[1].set()
            await pending
            assert receipt["row_written"] is True

        asyncio.run(drive())


class TestWakeLabelRow:
    """The calibration row, keyed by the verdict it judges."""

    def test_it_joins_on_the_verdict_id(self):
        row = decisions_log.build_wake_label_row(
            verdict_id="ab12",
            point="nudge.wake",
            session_key="chat-1",
            label="owner_acted",
            value=True,
            tool_calls=1,
            reply_chars=12,
        )
        assert row["verdict_id"] == "ab12"
        assert row["kind"] == decisions_log.KIND_WAKE_LABEL
        assert row["value"] is True

    def test_it_is_told_apart_from_a_decision_row_by_its_kind(self):
        decision = decisions_log.build_row(point="nudge.wake", session_key="chat-1", latency_ms=1)
        assert "kind" not in decision
        label = decisions_log.build_wake_label_row(
            verdict_id="a",
            point="nudge.wake",
            session_key="chat-1",
            label="missed",
            value=False,
            tool_calls=0,
            reply_chars=0,
        )
        assert label["kind"] == "wake_label"

    def test_the_session_is_a_digest_and_not_the_key(self):
        row = decisions_log.build_wake_label_row(
            verdict_id="a",
            point="nudge.wake",
            session_key="chat-1953-1790314195",
            label="missed",
            value=False,
            tool_calls=0,
            reply_chars=0,
        )
        assert "chat-1953" not in json.dumps(row)
        assert row["session"] == decisions_log.session_digest("chat-1953-1790314195")

    def test_an_unknown_label_writes_an_empty_name(self):
        row = decisions_log.build_wake_label_row(
            verdict_id="a",
            point="nudge.wake",
            session_key=None,
            label="whatever",
            value=True,
            tool_calls=0,
            reply_chars=0,
        )
        assert row["label"] == ""

    def test_a_truthy_stand_in_is_not_a_true_value(self):
        row = decisions_log.build_wake_label_row(
            verdict_id="a",
            point="nudge.wake",
            session_key=None,
            label="missed",
            value="yes",
            tool_calls=0,
            reply_chars=0,
        )
        assert row["value"] is False

    def test_an_oversized_id_is_clipped(self):
        row = decisions_log.build_wake_label_row(
            verdict_id="z" * 500,
            point="nudge.wake",
            session_key=None,
            label="missed",
            value=True,
            tool_calls=0,
            reply_chars=0,
        )
        assert len(row["verdict_id"]) == 64

    def test_the_row_carries_no_reply_and_no_target(self):
        row = decisions_log.build_wake_label_row(
            verdict_id="a",
            point="nudge.wake",
            session_key="chat-1",
            label="owner_acted",
            value=True,
            tool_calls=0,
            reply_chars=0,
        )
        assert set(row) == {
            "ts",
            "kind",
            "verdict_id",
            "point",
            "session",
            "label",
            "value",
            "tool_calls",
            "reply_chars",
        }

    def test_it_does_not_accept_a_timestamp_override(self):
        with pytest.raises(TypeError):
            decisions_log.build_wake_label_row(
                verdict_id="a",
                point="nudge.wake",
                session_key="chat-1",
                label="owner_acted",
                value=True,
                tool_calls=0,
                reply_chars=0,
                ts=None,
            )


class TestQuietStreakBound:
    """The point bounds what it retains, and that bound tracks the engine's own."""

    def test_the_point_and_the_engine_cap_the_streak_at_the_same_number(self):
        # The point clamps its own state rather than importing the engine's constant,
        # because the engine already imports this point -- borrowing it back would make
        # a cycle and pull the loop engine into this point's import path. The cost of
        # owning the number is that it can drift, which is what this pins.
        assert point.MAX_QUIET_STREAK == _MAX_QUIET_STREAK

    def test_the_point_does_not_import_the_loop_engine(self):
        # A cycle deferred by a function-local import is still a cycle.
        source = Path(point.__file__).read_text(encoding="utf-8")
        assert "from kiro_crew.autonudge import" not in source
        assert "import kiro_crew.autonudge" not in source


class TestVerdictIdBound:
    """One bound for the join key, so the two sides cannot drift apart."""

    def test_both_modules_name_the_same_bound(self):
        assert judge.MAX_VERDICT_ID_CHARS == decisions_log.MAX_VERDICT_ID_CHARS

    def test_a_clipped_id_comes_out_the_same_on_both_sides_of_the_join(self):
        # One population, and the join is by exact string: a bound that differed by a
        # character on either side would make every oversized id stop matching.
        oversized = "z" * 500
        stored = judge.verdict_entry(
            _verdict(irq.Outcome.WAKE),
            0,
            suppressed=False,
            answered=True,
            verdict_id=oversized,
        )
        label = decisions_log.build_wake_label_row(
            verdict_id=oversized,
            point="nudge.wake",
            session_key="chat-1",
            label="owner_acted",
            value=True,
            tool_calls=0,
            reply_chars=0,
        )
        assert len(stored["id"]) == len(label["verdict_id"])
        assert stored["id"] == label["verdict_id"]


class TestStoredHistoryLoader:
    """What survives a round trip through the record, and what is refused on the way in."""

    def test_a_row_is_rebuilt_from_allowlisted_keys(self):
        loaded = _bounded_judge_recent(
            [
                {
                    "outcome": "quiet",
                    "at": 12.0,
                    "evidence_items": 2,
                    "suppressed": True,
                    "answered": True,
                    "missed": True,
                    "id": "q1",
                    "text": "a worker said something",
                    "target": "chat-1953",
                }
            ]
        )
        assert loaded == [
            {
                "outcome": "quiet",
                "at": 12.0,
                "evidence_items": 2,
                "suppressed": True,
                "answered": True,
                "missed": True,
                "id": "q1",
            }
        ]

    def test_a_labelled_delivery_keeps_its_stamp(self):
        row = _delivered(5.0, "d1")
        row["owner_acted"] = True
        loaded = _bounded_judge_recent([row])
        assert loaded[0]["delivered"] is True
        assert loaded[0]["owner_acted"] is True

    def test_an_unlabelled_delivery_is_marked_forfeited(self):
        # Its turn belongs to a process that is gone, so no label can ever be read for
        # it, and the next turn on that slot would be labelled in its place. The stamp
        # stays, so the row still closes its own label cycle.
        loaded = _bounded_judge_recent([_delivered(5.0, "d1")])
        assert loaded[0]["delivered"] is True
        assert loaded[0]["forfeited"] is True
        assert loaded[0]["outcome"] == "wake"

    def test_a_missed_label_on_a_delivery_is_dropped_and_forfeited(self):
        row = _delivered(5.0, "d1")
        row["missed"] = False
        loaded = _bounded_judge_recent([row])
        assert "missed" not in loaded[0]
        assert loaded[0]["forfeited"] is True

    def test_an_owner_acted_label_on_a_suppression_is_dropped(self):
        row = _suppressed(5.0, "q1")
        row["owner_acted"] = True
        loaded = _bounded_judge_recent([row])
        assert "owner_acted" not in loaded[0]

    def test_each_state_keeps_only_its_own_legitimate_label(self):
        delivery = _delivered(5.0, "d1")
        delivery.update({"owner_acted": True, "missed": False})
        suppression = _suppressed(6.0, "q1")
        suppression.update({"missed": True, "owner_acted": False})

        loaded = _bounded_judge_recent([delivery, suppression])

        assert loaded[0]["owner_acted"] is True
        assert "missed" not in loaded[0]
        assert "forfeited" not in loaded[0]
        assert loaded[1]["missed"] is True
        assert "owner_acted" not in loaded[1]

    def test_an_incompatible_label_cannot_capture_a_later_turn(self):
        crafted = _delivered(5.0, "crafted")
        crafted["missed"] = False
        loaded = _bounded_judge_recent([_suppressed(4.0, "q1"), crafted])

        labelled, changed = judge.label_latest_delivery(loaded, acted=True)

        assert changed == []
        assert labelled == loaded
        assert "missed" not in labelled[0]
        assert "owner_acted" not in labelled[1]
        assert labelled[1]["forfeited"] is True

    def test_a_row_claiming_both_states_cannot_defeat_forfeiture(self):
        # The states are exclusive, so only a hand-written row claims both -- and that
        # row is the one shape whose `missed` IS admitted onto a delivery, since the
        # suppressed side of the claim admits it. Forfeiture therefore turns on the
        # presence of a legitimate `owner_acted` and on nothing else: a row carrying
        # only `missed` is still forfeited, and so still takes no later turn's label.
        crafted = _delivered(5.0, "crafted")
        crafted["suppressed"] = True
        crafted["missed"] = False

        loaded = _bounded_judge_recent([_suppressed(4.0, "q1"), crafted])

        assert loaded[1]["missed"] is False
        assert loaded[1]["forfeited"] is True

        labelled, changed = judge.label_latest_delivery(loaded, acted=True)

        assert changed == []
        assert "owner_acted" not in labelled[1]
        assert "missed" not in labelled[0]

    def test_a_forfeited_row_remains_a_boundary(self):
        loaded = _bounded_judge_recent([_delivered(5.0, "d1")])
        stamped, changed = judge.confirm_delivery(loaded)
        assert changed is False
        assert stamped[0]["delivered"] is True
        assert stamped[0]["forfeited"] is True

    def test_a_row_naming_no_outcome_is_dropped(self):
        assert _bounded_judge_recent([{"at": 1.0, "suppressed": True}]) == []

    def test_a_non_list_loads_as_no_history(self):
        assert _bounded_judge_recent({"outcome": "quiet"}) == []
        assert _bounded_judge_recent(None) == []
        assert _bounded_judge_recent("quiet") == []

    def test_rows_that_are_not_objects_are_skipped(self):
        assert _bounded_judge_recent([None, 3, "x", {"outcome": "wake"}]) == [{"outcome": "wake"}]

    def test_a_longer_stored_list_is_trimmed_to_the_newest(self):
        rows = [_suppressed(float(index)) for index in range(judge.MAX_STORED_VERDICTS + 6)]
        loaded = _bounded_judge_recent(rows)
        assert len(loaded) == judge.MAX_STORED_VERDICTS
        assert loaded[-1]["at"] == float(judge.MAX_STORED_VERDICTS + 5)

    def test_a_far_oversized_history_builds_only_the_capped_newest_rows(self, monkeypatch, caplog):
        total = judge.MAX_STORED_VERDICTS * 20
        rows = [_suppressed(float(index)) for index in range(total)]
        built: list[float] = []

        def _build(item):
            built.append(item["at"])
            return dict(item)

        monkeypatch.setattr("kiro_crew.autonudge._bounded_judge_verdict", _build)
        with caplog.at_level("DEBUG", logger="kiro_crew.autonudge"):
            loaded = _bounded_judge_recent(rows)

        assert len(built) == judge.MAX_STORED_VERDICTS
        assert [row["at"] for row in loaded] == [
            float(index) for index in range(total - judge.MAX_STORED_VERDICTS, total)
        ]
        discarded = [message for message in caplog.messages if "discarded" in message]
        assert len(discarded) == 1
        assert str(total - judge.MAX_STORED_VERDICTS) in discarded[0]

    def test_a_non_boolean_flag_does_not_survive(self):
        loaded = _bounded_judge_recent(
            [{"outcome": "quiet", "missed": "yes", "suppressed": 1, "answered": "y"}]
        )
        assert loaded == [{"outcome": "quiet"}]


class TestTurnCompleteLabelling:
    """The hook: a finished turn labels the verdict that delivered it."""

    def test_runner_labels_only_landed_nudge_turns(self):
        runner_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "kiro_crew"
            / "dashboard"
            / "chat_runner.py"
        )
        tree = ast.parse(runner_path.read_text(encoding="utf-8"))
        gates = [
            keyword.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "notify_turn_complete"
            for keyword in node.keywords
            if keyword.arg == "nudge_turn"
        ]
        assert len(gates) == 1
        gate = gates[0]
        assert isinstance(gate, ast.BoolOp)
        assert isinstance(gate.op, ast.And)
        assert [part.id for part in gate.values if isinstance(part, ast.Name)] == [
            "_directive_self_wake",
            "_turn_landed",
        ]
        flushed = [
            keyword.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "notify_turn_complete"
            for keyword in node.keywords
            if keyword.arg == "reply_flushed"
        ]
        assert len(flushed) == 1
        assert isinstance(flushed[0], ast.Name)
        assert flushed[0].id == "_turn_flushed_visible_text"

    @staticmethod
    def _loop(history: list) -> NudgeLoop:
        return NudgeLoop(
            id="loop-1",
            slot_key="chat-1",
            message="watch chat-1953",
            idle_secs=300,
            active=True,
            gate=True,
            judge_recent_verdicts=history,
        )

    def _service(self, tmp_path, history: list):
        service = AutoNudgeService(base_dir=tmp_path)
        loop = self._loop(history)
        service._loops[loop.id] = loop
        return service, loop

    @staticmethod
    def _complete(service: AutoNudgeService, **kwargs) -> None:
        """Drive the hook the way the gateway does: from inside a running loop.

        The hook re-arms the loop's timer, which schedules a task, so it has a running
        loop by contract. Driving it without one would test a shape production never
        reaches and would fail on the re-arm rather than on anything under test.
        """

        async def drive() -> None:
            service.notify_turn_complete("chat-1", **kwargs)
            # Drain the label task and every persist or append task it creates, so
            # nothing is left pending when the loop closes.
            await asyncio.sleep(0)
            while service._inflight_adds:
                for task in list(service._inflight_adds):
                    with contextlib.suppress(Exception):
                        await task
                await asyncio.sleep(0)

        asyncio.run(drive())

    def test_an_acting_turn_labels_the_delivery_and_the_quiets_behind_it(self, tmp_path):
        service, loop = self._service(tmp_path, [_suppressed(1.0, "q1"), _delivered(2.0, "d1")])
        try:
            self._complete(service, tool_calls=3, reply_text="pushed a fix", nudge_turn=True)
        finally:
            service.stop()
        assert loop.judge_recent_verdicts[-1]["owner_acted"] is True
        assert loop.judge_recent_verdicts[0]["missed"] is True

    def test_retroactive_missed_rows_carry_distance_from_the_delivery(self, tmp_path, monkeypatch):
        written: list[dict] = []
        monkeypatch.setattr(decisions_log, "append", lambda row: written.append(row) or True)
        history = [
            _suppressed(100.0, "q-old"),
            _suppressed(195.0, "q-new"),
            _delivered(200.0, "d1"),
        ]
        service, loop = self._service(tmp_path, history)
        try:
            self._complete(
                service,
                tool_calls=3,
                reply_text="  fixed it  ",
                nudge_turn=True,
            )
        finally:
            service.stop()

        missed = {row["verdict_id"]: row for row in written if row["label"] == "missed"}
        assert {
            verdict_id: (row["position_back"], row["age_s"]) for verdict_id, row in missed.items()
        } == {"q-old": (2, 100.0), "q-new": (1, 5.0)}
        assert all(isinstance(row["position_back"], int) for row in missed.values())
        assert all(isinstance(row["age_s"], float) for row in missed.values())
        assert "fixed it" not in json.dumps(written)

    def test_label_rows_carry_rederivable_inputs_without_reply_text(self, tmp_path, monkeypatch):
        written: list[dict] = []
        monkeypatch.setattr(decisions_log, "append", lambda row: written.append(row) or True)
        service, loop = self._service(tmp_path, [_suppressed(1.0, "q1"), _delivered(2.0, "d1")])
        try:
            self._complete(
                service,
                tool_calls=3,
                reply_text="  fixed it  ",
                nudge_turn=True,
            )
        finally:
            service.stop()
        assert len(written) == 2
        assert {row["tool_calls"] for row in written} == {3}
        assert {row["reply_chars"] for row in written} == {8}
        assert "fixed it" not in json.dumps(written)

    def test_an_idle_turn_clears_the_quiets_behind_it(self, tmp_path):
        service, loop = self._service(tmp_path, [_suppressed(1.0, "q1"), _delivered(2.0, "d1")])
        try:
            self._complete(service, tool_calls=0, reply_text="Nothing new.", nudge_turn=True)
        finally:
            service.stop()
        assert loop.judge_recent_verdicts[-1]["owner_acted"] is False
        assert loop.judge_recent_verdicts[0]["missed"] is False

    def test_a_flushed_reply_with_a_short_final_segment_reads_as_acting(self, tmp_path):
        service, loop = self._service(tmp_path, [_delivered(1.0, "d1")])
        try:
            self._complete(
                service,
                tool_calls=0,
                reply_text="Done.",
                reply_flushed=True,
                nudge_turn=True,
            )
        finally:
            service.stop()
        assert loop.judge_recent_verdicts[-1]["owner_acted"] is True

    def test_a_turn_with_no_counts_reads_as_acting(self, tmp_path, monkeypatch):
        # An older runner passes neither, and the rule's safe direction is not to
        # teach the judge that this wake was wasted.
        written: list[dict] = []
        monkeypatch.setattr(decisions_log, "append", lambda row: written.append(row) or True)
        service, loop = self._service(tmp_path, [_delivered(1.0, "d1")])
        try:
            self._complete(service, nudge_turn=True)
        finally:
            service.stop()
        assert loop.judge_recent_verdicts[-1]["owner_acted"] is True
        assert written[0]["tool_calls"] is None
        assert written[0]["reply_chars"] == 0

    def test_an_undelivered_wake_is_not_labelled_by_the_turn_that_refused_it(self, tmp_path):
        # The finding, at the seam: a fire refused because the slot was busy leaves an
        # unstamped row, and the user's own turn must not be scored against it.
        pending = judge.verdict_entry(
            _verdict(irq.Outcome.WAKE), 2, suppressed=False, answered=True, verdict_id="lost"
        )
        service, loop = self._service(tmp_path, [pending])
        try:
            self._complete(
                service,
                tool_calls=9,
                reply_text="the owner did their own work",
                nudge_turn=True,
            )
        finally:
            service.stop()
        assert "owner_acted" not in loop.judge_recent_verdicts[-1]

    @pytest.mark.parametrize("nudge_turn", [False, None])
    def test_a_turn_that_is_not_the_loop_turn_does_not_label(self, tmp_path, nudge_turn):
        service, loop = self._service(tmp_path, [_delivered(1.0, "d1")])
        try:
            self._complete(
                service,
                tool_calls=4,
                reply_text="the owner did unrelated work",
                nudge_turn=nudge_turn,
            )
        finally:
            service.stop()
        assert "owner_acted" not in loop.judge_recent_verdicts[-1]

    def test_a_nudge_turn_that_did_not_land_does_not_label(self, tmp_path):
        service, loop = self._service(tmp_path, [_suppressed(1.0, "q1"), _delivered(2.0, "d1")])
        try:
            self._complete(service, tool_calls=0, reply_text="", nudge_turn=False)
        finally:
            service.stop()
        assert "owner_acted" not in loop.judge_recent_verdicts[-1]
        assert "missed" not in loop.judge_recent_verdicts[0]

    def test_a_loop_with_no_judge_history_is_untouched(self, tmp_path):
        service, loop = self._service(tmp_path, [])
        try:
            self._complete(service, tool_calls=0, reply_text="")
        finally:
            service.stop()
        assert loop.judge_recent_verdicts == []

    def test_the_hook_still_rearms_the_timer(self, tmp_path):
        # The label must never be able to cost the loop its re-arm: that is the hook's
        # original job and everything here is an observation beside it.
        service, loop = self._service(tmp_path, [_delivered(1.0, "d1")])
        armed: list = []
        service._arm_from_deadline = lambda target: armed.append(target.id)  # type: ignore[assignment]
        try:
            service.notify_turn_complete("chat-1", tool_calls=1, reply_text="")
        finally:
            service.stop()
        assert armed == ["loop-1"]

    def test_a_ruined_history_does_not_stop_the_rearm(self, tmp_path):
        service, loop = self._service(tmp_path, [object()])  # type: ignore[list-item]
        armed: list = []
        service._arm_from_deadline = lambda target: armed.append(target.id)  # type: ignore[assignment]
        try:
            service.notify_turn_complete("chat-1", tool_calls=0, reply_text="")
        finally:
            service.stop()
        assert armed == ["loop-1"]

    def test_a_failed_label_persist_publishes_nothing_and_restores_the_row(self, tmp_path):
        service, loop = self._service(tmp_path, [_delivered(1.0, "d1")])
        armed: list = []
        published: list = []
        service._arm_from_deadline = lambda target: armed.append(target.id)  # type: ignore[assignment]

        async def _reject(_loop) -> bool:
            return False

        service._persist_judge_state = _reject  # type: ignore[method-assign]
        service._append_judge_labels = (  # type: ignore[method-assign]
            lambda _loop, changed, *, tool_calls=None, reply_chars=0: published.extend(changed)
        )
        try:
            self._complete(service, tool_calls=4, reply_text="", nudge_turn=True)
        finally:
            service.stop()
        assert armed == ["loop-1"]
        assert published == []
        assert "owner_acted" not in loop.judge_recent_verdicts[-1]

    def test_a_detached_loop_publishes_no_labels(self, tmp_path):
        service, loop = self._service(tmp_path, [_delivered(1.0, "d1")])
        published: list = []

        async def _detach_loop(_loop) -> bool:
            service._loops.pop(loop.id)
            return True

        service._persist_judge_state = _detach_loop  # type: ignore[method-assign]
        service._append_judge_labels = (  # type: ignore[method-assign]
            lambda _loop, changed, *, tool_calls=None, reply_chars=0: published.extend(changed)
        )
        try:
            self._complete(service, tool_calls=1, reply_text="", nudge_turn=True)
        finally:
            service.stop()
        assert published == []

    def test_a_replaced_history_publishes_no_labels(self, tmp_path):
        service, loop = self._service(tmp_path, [_suppressed(1.0, "q1"), _delivered(2.0, "d1")])
        published: list = []

        async def _replace_history(_loop) -> bool:
            loop.judge_recent_verdicts = []
            return True

        service._persist_judge_state = _replace_history  # type: ignore[method-assign]
        service._append_judge_labels = (  # type: ignore[method-assign]
            lambda _loop, changed, *, tool_calls=None, reply_chars=0: published.extend(changed)
        )
        try:
            self._complete(service, tool_calls=1, reply_text="", nudge_turn=True)
        finally:
            service.stop()
        assert loop.judge_recent_verdicts == []
        assert published == []

    def test_a_failed_persist_does_not_resurrect_a_cleared_history(self, tmp_path):
        # An update landing during the write clears the history ON PURPOSE -- a retarget
        # or a criteria change, both of which drop it because its rows answer a question
        # that is gone. An unconditional snapshot restore would put those rows back, and
        # nothing downstream purges them, so the next tick reads a hit rate for the old
        # subject. The restore therefore only fires when the list it labelled is still
        # the one on the loop.
        service, loop = self._service(tmp_path, [_suppressed(1.0, "q1"), _delivered(2.0, "d1")])

        async def _clear_then_fail(_loop) -> bool:
            loop.judge_recent_verdicts = []
            return False

        service._persist_judge_state = _clear_then_fail  # type: ignore[method-assign]
        try:
            self._complete(service, tool_calls=1, reply_text="", nudge_turn=True)
        finally:
            service.stop()
        assert loop.judge_recent_verdicts == [], "the deliberate clear stands"

    def test_a_raising_publication_does_not_resurrect_a_cleared_history(self, tmp_path):
        # The same rule on the other branch, and reaching it takes care: the publication
        # guard returns early on an already-cleared history, so the clear has to happen
        # DURING the publication for the raise to land in the handler at all.
        service, loop = self._service(tmp_path, [_suppressed(1.0, "q1"), _delivered(2.0, "d1")])

        async def _succeed(_loop) -> bool:
            return True

        def _clear_then_raise(_loop, _changed, *, tool_calls=None, reply_chars=0):
            loop.judge_recent_verdicts = []
            raise RuntimeError("publication failed")

        service._persist_judge_state = _succeed  # type: ignore[method-assign]
        service._append_judge_labels = _clear_then_raise  # type: ignore[method-assign]
        try:
            self._complete(service, tool_calls=1, reply_text="", nudge_turn=True)
        finally:
            service.stop()
        assert loop.judge_recent_verdicts == [], "the deliberate clear stands"


class TestVerdictRecordedOnTheTick:
    """The tick records what it decided; the fire path records what went out."""

    @staticmethod
    def _loop() -> NudgeLoop:
        return NudgeLoop(
            id="loop-1",
            slot_key="chat-1",
            message="watch chat-1953",
            idle_secs=300,
            active=True,
            gate=True,
        )

    def test_a_suppressed_verdict_is_recorded_as_withholding_the_turn(self, tmp_path):
        service = AutoNudgeService(base_dir=tmp_path)
        loop = self._loop()
        try:
            service._record_judge_verdict(
                loop, _verdict(irq.Outcome.QUIET), 2, "v1", suppressed=True, answered=True
            )
        finally:
            service.stop()
        row = loop.judge_recent_verdicts[-1]
        assert row["suppressed"] is True
        assert "delivered" not in row
        assert row["id"] == "v1"

    def test_a_wake_verdict_is_recorded_undecided(self, tmp_path):
        service = AutoNudgeService(base_dir=tmp_path)
        loop = self._loop()
        try:
            service._record_judge_verdict(
                loop, _verdict(irq.Outcome.WAKE), 5, "v2", suppressed=False, answered=True
            )
        finally:
            service.stop()
        row = loop.judge_recent_verdicts[-1]
        assert "delivered" not in row
        assert "suppressed" not in row

    def test_the_fire_path_stamps_the_delivery(self, tmp_path):
        service = AutoNudgeService(base_dir=tmp_path)
        loop = self._loop()
        try:
            service._record_judge_verdict(
                loop, _verdict(irq.Outcome.WAKE), 5, "v2", suppressed=False, answered=True
            )
            service._confirm_judge_delivery(loop)
        finally:
            service.stop()
        assert loop.judge_recent_verdicts[-1]["delivered"] is True

    def test_a_failed_persist_withdraws_the_suppression(self, tmp_path):
        service = AutoNudgeService(base_dir=tmp_path)
        loop = self._loop()
        try:
            service._record_judge_verdict(
                loop, _verdict(irq.Outcome.QUIET), 1, "v3", suppressed=True, answered=True
            )
            service._withdraw_judge_suppression(loop)
        finally:
            service.stop()
        row = loop.judge_recent_verdicts[-1]
        assert "suppressed" not in row
        assert "delivered" not in row

    def test_withdrawing_from_an_empty_history_does_not_raise(self, tmp_path):
        service = AutoNudgeService(base_dir=tmp_path)
        loop = self._loop()
        try:
            service._withdraw_judge_suppression(loop)
            service._confirm_judge_delivery(loop)
        finally:
            service.stop()
        assert loop.judge_recent_verdicts == []


class TestProbeFloorFiresAJudgedTick:
    """The probe's own floor can fire the turn a judge row claims it withheld."""

    def test_a_floor_fire_takes_no_missed_label_for_the_turn_it_delivered(
        self, tmp_path, monkeypatch
    ):
        """The row the probe floor fires is labelled by that turn, not as a suppression.

        The judge's streak and the probe's streak are independent fields, so a gated
        loop whose judge arms mid-life reaches the probe's floor with the judge's own
        streak still below its floor. The judge has already stamped this tick's row as
        withholding the turn, and the floor then delivers it anyway. Left standing, that
        row is skipped by the delivery stamp and takes ``missed`` from the retroactive
        pass of the next delivery -- a wrong row in the one log this feature produces.
        """
        monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
        monkeypatch.setattr(
            irq,
            "poll",
            lambda *_a, **_k: irq.Verdict(irq.Outcome.QUIET, "nothing yet", ()),
        )
        monitor = MonitorState(
            kind="gh-pr",
            target="acme/widgets#42",
            objective="review_ready",
            created_ts=1_000.0,
        )
        monitor.quiet_streak = _MAX_QUIET_STREAK - 1
        loop = NudgeLoop(
            id="loop-floor",
            slot_key="chat-1",
            message="watch https://github.com/acme/widgets/pull/42 until green",
            idle_secs=30,
            active=True,
            monitor=monitor,
            gate=True,
        )
        service = AutoNudgeService(base_dir=tmp_path)
        service._loops[loop.id] = loop
        persisted: list[str] = []
        real_persist = service._persist_judge_state

        async def _recording_persist(target_loop: NudgeLoop) -> bool:
            persisted.append("start")
            landed = await real_persist(target_loop)
            persisted.append("done")
            return landed

        service._persist_judge_state = _recording_persist  # type: ignore[method-assign]

        async def _judge_suppresses(target_loop: NudgeLoop) -> bool:
            """What the judge leaves behind when it answers quiet below its own floor."""
            service._record_judge_verdict(
                target_loop,
                _verdict(irq.Outcome.QUIET),
                2,
                "v-floor",
                suppressed=True,
                answered=True,
            )
            return True

        service._judge_tick_is_quiet = _judge_suppresses  # type: ignore[method-assign]

        async def drive() -> None:
            assert await service._monitor_tick_is_quiet(loop) is False, "the floor must fire"
            # Only the floor branch owes this charge, so it is what proves the tick
            # reached the floor rather than returning early on a fallback.
            assert loop.id in service._pending_floor_tick, "the floor branch ran"
            # The withdrawal is a memory edit, so the tick has to await its durable
            # write: a deferred one that never lands leaves a restart reading the row
            # as suppressed and the next delivery writing the false label back.
            assert persisted == ["start", "done"], "the withdrawal landed before the fire"
            # The fire lands, so the corrected row is stamped like any other delivery
            # and the turn it delivered is what labels it.
            service._confirm_judge_delivery(loop)
            history, _ = judge.label_latest_delivery(loop.judge_recent_verdicts, acted=True)
            loop.judge_recent_verdicts = history
            # A later real wake. Its retroactive pass is what writes the wrong label on
            # a row still claiming to have withheld its turn.
            service._record_judge_verdict(
                loop, _verdict(irq.Outcome.WAKE), 3, "v-wake", suppressed=False, answered=True
            )
            service._confirm_judge_delivery(loop)
            history, _ = judge.label_latest_delivery(loop.judge_recent_verdicts, acted=True)
            loop.judge_recent_verdicts = history
            await asyncio.sleep(0)

        try:
            asyncio.run(drive())
        finally:
            service.stop()

        floor_row = next(row for row in loop.judge_recent_verdicts if row.get("id") == "v-floor")
        assert "suppressed" not in floor_row, "the claim to have withheld the turn is withdrawn"
        assert floor_row["owner_acted"] is True, "the turn it delivered is what labels it"
        assert "missed" not in floor_row, "and no later delivery labels it as a suppression"

    def test_a_floor_fire_leaves_an_earlier_tick_s_suppression_alone(self, tmp_path, monkeypatch):
        """With no lane armed, the newest row belongs to an earlier tick and stands.

        Nothing is asked on this tick, so there is no claim of its own to withdraw. The
        newest row is a suppression that is TRUE, and clearing it would return it to the
        undecided state, where the fire path stamps it delivered and the finished turn
        credits it with a delivery it never produced.
        """
        monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
        monkeypatch.setattr(
            irq,
            "poll",
            lambda *_a, **_k: irq.Verdict(irq.Outcome.QUIET, "nothing yet", ()),
        )
        monitor = MonitorState(
            kind="gh-pr",
            target="acme/widgets#42",
            objective="review_ready",
            created_ts=1_000.0,
        )
        monitor.quiet_streak = _MAX_QUIET_STREAK - 1
        loop = NudgeLoop(
            id="loop-floor-unarmed",
            slot_key="chat-1",
            message="watch https://github.com/acme/widgets/pull/42 until green",
            idle_secs=30,
            active=True,
            monitor=monitor,
            gate=True,
        )
        loop.judge_recent_verdicts = [_suppressed(1.0, "v-old")]
        service = AutoNudgeService(base_dir=tmp_path)
        service._loops[loop.id] = loop
        persisted: list[str] = []
        real_persist = service._persist_judge_state

        async def _recording_persist(target_loop: NudgeLoop) -> bool:
            persisted.append("start")
            return await real_persist(target_loop)

        service._persist_judge_state = _recording_persist  # type: ignore[method-assign]

        async def _no_lane_armed(_target_loop: NudgeLoop) -> None:
            return None

        service._judge_tick_is_quiet = _no_lane_armed  # type: ignore[method-assign]

        async def drive() -> None:
            assert await service._monitor_tick_is_quiet(loop) is False, "the floor must fire"
            assert loop.id in service._pending_floor_tick, "the floor branch ran"
            # No row of this tick's to make durable, so the streak reset rides the
            # deferred write exactly as it does without a judge.
            assert persisted == [], "no judge write is forced on a tick that asked nothing"
            service._confirm_judge_delivery(loop)
            history, _ = judge.label_latest_delivery(loop.judge_recent_verdicts, acted=True)
            loop.judge_recent_verdicts = history
            await asyncio.sleep(0)

        try:
            asyncio.run(drive())
        finally:
            service.stop()

        old_row = next(row for row in loop.judge_recent_verdicts if row.get("id") == "v-old")
        assert old_row.get("suppressed") is True, "a suppression this tick did not make stands"
        assert "delivered" not in old_row, "and it is credited with no delivery"
        assert "owner_acted" not in old_row
