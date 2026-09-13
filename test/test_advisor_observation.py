"""Advisor observation model: checkpoints, epochs, and bounded records.

Contract under test (see docs/system-specs/modules/advisor.md):

- Checkpoints are derived from host-owned facts, never transcript polling:
  finalized text segments and completed tool results coalesce into
  ``in_progress=True`` updates; the turn's completion produces exactly one
  ``in_progress=False`` final update.
- Every update carries the parent turn identity, the observation epoch, and a
  monotonically increasing advisor-update sequence.
- Lifecycle boundaries (reset, compaction, fork/transfer, agent/model/
  workspace/provider switch) start a new epoch: pending updates and dedupe
  state never cross epochs.
- Records are bounded and redacted; a synthetic completion is marked so
  downstream consumers never treat it as a genuine provider terminal.
- A disabled advisor keeps the whole module inert: no observer, no buffered
  state, no import-time side effects.
"""

from __future__ import annotations

import pytest

from kiro_crew.advisor.observation import (
    EPOCH_MAX_RECORDS,
    OBSERVATION_PAYLOAD_MAX_CHARS,
    AdvisorObserver,
    ObservationUpdate,
)


def make_observer(**kwargs) -> AdvisorObserver:
    defaults = dict(parent_session_key="dashboard:slot-1", turn_id="turn-1")
    defaults.update(kwargs)
    return AdvisorObserver(**defaults)


class TestCheckpointBatching:
    def test_tool_results_coalesce_into_one_in_progress_update(self):
        obs = make_observer()
        obs.record_tool_result("grep", "match a")
        obs.record_tool_result("read", "file body")
        update = obs.drain_update()
        assert isinstance(update, ObservationUpdate)
        assert update.in_progress is True
        assert [r.tool_name for r in update.tool_results] == ["grep", "read"]

    def test_tool_results_preserve_order_within_update(self):
        obs = make_observer()
        for i in range(5):
            obs.record_tool_result(f"tool-{i}", f"payload-{i}")
        update = obs.drain_update()
        assert [r.tool_name for r in update.tool_results] == [f"tool-{i}" for i in range(5)]

    def test_finalized_segment_is_included_in_next_update(self):
        obs = make_observer()
        obs.record_segment("I will look at the config first.")
        obs.record_tool_result("read", "config body")
        update = obs.drain_update()
        assert update.segments == ["I will look at the config first."]

    def test_drain_with_nothing_recorded_returns_none(self):
        obs = make_observer()
        assert obs.drain_update() is None

    def test_updates_carry_parent_turn_identity(self):
        obs = make_observer(parent_session_key="dashboard:s9", turn_id="t42")
        obs.record_tool_result("glob", "x")
        update = obs.drain_update()
        assert update.parent_session_key == "dashboard:s9"
        assert update.turn_id == "t42"


class TestSequenceAndEpoch:
    def test_sequence_is_monotonic_across_updates(self):
        obs = make_observer()
        obs.record_tool_result("a", "1")
        first = obs.drain_update()
        obs.record_tool_result("b", "2")
        second = obs.drain_update()
        assert second.seq > first.seq

    def test_new_epoch_discards_pending_records(self):
        obs = make_observer()
        obs.record_tool_result("a", "stale")
        obs.begin_epoch()
        assert obs.drain_update() is None

    def test_new_epoch_changes_epoch_identity_on_updates(self):
        obs = make_observer()
        obs.record_tool_result("a", "1")
        before = obs.drain_update()
        obs.begin_epoch()
        obs.record_tool_result("a", "1")
        after = obs.drain_update()
        assert after.epoch != before.epoch

    def test_dedupe_state_does_not_cross_epochs(self):
        obs = make_observer()
        obs.record_tool_result("a", "same payload")
        obs.drain_update()
        obs.begin_epoch()
        # The identical record in a fresh epoch is new evidence, not a dupe.
        obs.record_tool_result("a", "same payload")
        update = obs.drain_update()
        assert update is not None
        assert len(update.tool_results) == 1


class TestCompletion:
    def test_complete_produces_final_update(self):
        obs = make_observer()
        obs.record_segment("final answer text")
        update = obs.complete(stop_reason="end_turn")
        assert update.in_progress is False
        assert update.stop_reason == "end_turn"
        assert update.segments == ["final answer text"]

    def test_complete_is_idempotent_no_duplicate_replay(self):
        obs = make_observer()
        obs.record_segment("answer")
        first = obs.complete(stop_reason="end_turn")
        assert first is not None
        assert obs.complete(stop_reason="end_turn") is None

    def test_synthetic_completion_is_marked(self):
        obs = make_observer()
        obs.record_segment("answer")
        update = obs.complete(stop_reason="end_turn", synthetic=True)
        assert update.synthetic is True

    def test_records_after_complete_require_new_epoch(self):
        obs = make_observer()
        obs.complete(stop_reason="end_turn")
        with pytest.raises(RuntimeError):
            obs.record_tool_result("late", "payload")
        obs.begin_epoch()
        obs.record_tool_result("ok", "payload")
        assert obs.drain_update() is not None


class TestBoundsAndRedaction:
    def test_tool_payload_is_truncated_to_bound(self):
        obs = make_observer()
        obs.record_tool_result("read", "x" * (OBSERVATION_PAYLOAD_MAX_CHARS + 500))
        update = obs.drain_update()
        payload = update.tool_results[0].payload
        assert len(payload) <= OBSERVATION_PAYLOAD_MAX_CHARS
        assert update.tool_results[0].truncated is True

    def test_clipped_segment_carries_truncation_marker(self):
        """Round-44: a bounded segment record must say it was clipped -- the
        reviewer otherwise reads a cut-off text as complete."""
        from kiro_crew.advisor.observation import TRUNCATION_MARKER

        obs = make_observer()
        obs.record_segment("s" * (OBSERVATION_PAYLOAD_MAX_CHARS + 500))
        update = obs.drain_update()
        assert update.segments[0].endswith(TRUNCATION_MARKER)
        assert len(update.segments[0]) <= OBSERVATION_PAYLOAD_MAX_CHARS + len(TRUNCATION_MARKER)

    def test_short_segment_has_no_truncation_marker(self):
        from kiro_crew.advisor.observation import TRUNCATION_MARKER

        obs = make_observer()
        obs.record_segment("short")
        assert TRUNCATION_MARKER not in obs.drain_update().segments[0]

    def test_short_payload_is_not_marked_truncated(self):
        obs = make_observer()
        obs.record_tool_result("read", "short")
        update = obs.drain_update()
        assert update.tool_results[0].truncated is False

    def test_redactor_is_applied_to_tool_payloads_and_segments(self):
        seen: list[str] = []

        def redactor(text: str) -> str:
            seen.append(text)
            return "[scrubbed]"

        obs = make_observer(redactor=redactor)
        obs.record_segment("segment with secret")
        obs.record_tool_result("read", "payload with secret")
        update = obs.drain_update()
        assert update.segments == ["[scrubbed]"]
        assert update.tool_results[0].payload == "[scrubbed]"
        assert len(seen) == 2


class TestDisabledInertness:
    def test_disabled_service_creates_no_observer(self):
        from kiro_crew.advisor.service import AdvisorService

        service = AdvisorService(enabled=False)
        assert service.attach("dashboard:slot-1") is None
        assert service.observer_count() == 0

    def test_enabled_service_attaches_observer_at_current_boundary(self):
        from kiro_crew.advisor.service import AdvisorService

        service = AdvisorService(enabled=True, reviewer_available=True)
        observer = service.attach("dashboard:slot-1")
        assert observer is not None
        # Mid-session enable starts empty: no historical backfill.
        assert observer.drain_update() is None


class TestCumulativeEpochContext:
    """Live-run finding: checkpoint reviews were myopic -- each drain cleared
    the records, so every review saw one innocuous slice and found nothing
    while a whole-turn review caught real problems. Updates now carry the
    epoch's cumulative evidence (bounded), and the guard dedupes repeats."""

    def test_drain_carries_prior_records_of_the_epoch(self):
        obs = AdvisorObserver(parent_session_key="k", turn_id="t")
        obs.record_segment("step one")
        first = obs.drain_update()
        assert [s for s in first.segments] == ["step one"]
        obs.record_tool_result("write", "denied")
        second = obs.drain_update()
        assert "step one" in second.segments, "prior epoch evidence must persist"
        assert second.tool_results[0].payload == "denied"

    def test_drain_without_new_records_returns_none(self):
        obs = AdvisorObserver(parent_session_key="k", turn_id="t")
        obs.record_segment("only once")
        assert obs.drain_update() is not None
        assert obs.drain_update() is None, "no new evidence, no new review"

    def test_complete_emits_the_full_epoch(self):
        obs = AdvisorObserver(parent_session_key="k", turn_id="t")
        obs.record_segment("early")
        obs.drain_update()
        obs.record_segment("late")
        final = obs.complete(stop_reason="end_turn")
        assert "early" in final.segments and "late" in final.segments

    def test_epoch_records_are_bounded_in_count(self):
        obs = AdvisorObserver(parent_session_key="k", turn_id="t")
        for i in range(EPOCH_MAX_RECORDS + 10):
            obs.record_segment(f"segment {i}")
        update = obs.complete(stop_reason="end_turn")
        assert len(update.segments) <= EPOCH_MAX_RECORDS
        assert (
            f"segment {EPOCH_MAX_RECORDS + 9}" in update.segments
        ), "the newest evidence must survive the bound"

    def test_begin_epoch_clears_cumulative_state(self):
        obs = AdvisorObserver(parent_session_key="k", turn_id="t")
        obs.record_segment("stale")
        obs.drain_update()
        obs.begin_epoch()
        obs.record_segment("fresh")
        update = obs.drain_update()
        assert update.segments == ["fresh"]


class TestCombinedEpochTrim:
    """The documented epoch cap bounds the COMBINED record set, not each list
    independently -- three full lists would triple the reviewer payload."""

    def test_total_records_capped_at_epoch_max(self):
        from kiro_crew.advisor.observation import EPOCH_MAX_RECORDS, AdvisorObserver

        obs = AdvisorObserver(parent_session_key="dashboard:t", turn_id="")
        for i in range(EPOCH_MAX_RECORDS):
            obs.record_segment(f"seg {i}")
            obs.record_tool_result("tool", f"result {i}")
        update = obs.drain_update()
        total = len(update.segments) + len(update.tool_results)
        assert total <= EPOCH_MAX_RECORDS
        # newest records win the trim
        assert update.segments[-1] == f"seg {EPOCH_MAX_RECORDS - 1}"


class TestCompletedUpdateQueue:
    """Round-21: a fast successor turn must not overwrite an unconsumed final
    update -- completed updates queue and drain oldest-first."""

    def test_two_completions_both_survive_oldest_first(self):
        from kiro_crew.advisor.observation import AdvisorObserver

        obs = AdvisorObserver(parent_session_key="dashboard:q", turn_id="turn-1")
        obs.record_segment("turn one work")
        first = obs.complete()
        assert first is not None
        # next turn re-primes (pump has not drained yet) and completes too
        obs.begin_turn()
        obs._turn_id = "turn-2"
        obs.record_segment("turn two work")
        obs.complete()
        taken1 = obs.take_completed()
        taken2 = obs.take_completed()
        assert taken1 is not None and taken2 is not None
        assert taken1.turn_id == "turn-1"
        assert taken2.turn_id == "turn-2"
        assert obs.take_completed() is None
