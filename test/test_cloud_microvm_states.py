"""The MicroVM crew state machine: every edge, and the two things it must refuse."""

from __future__ import annotations

import pytest

from kiro_crew.cloud.microvm import states


class TestEdgeTable:
    def test_every_edge_lands_on_a_stored_state(self):
        for (origin, event), target in states.EDGES.items():
            assert target in states.STORED_STATES, f"{origin}/{event} -> {target}"

    def test_every_edge_leaves_a_stored_state_or_nothing(self):
        for origin, _event in states.EDGES:
            assert origin is None or origin in states.STORED_STATES

    def test_every_edge_names_a_declared_event(self):
        for _origin, event in states.EDGES:
            assert event in states.EVENTS

    def test_every_declared_event_is_reachable(self):
        """An event with no row is a name nothing can use."""
        used = {event for _origin, event in states.EDGES}
        assert set(states.EVENTS) == used

    def test_a_crew_can_only_start_by_launching(self):
        starts = {event for origin, event in states.EDGES if origin is None}
        assert starts == {states.EVENT_LAUNCH_STARTED}

    def test_terminal_states_are_derived_and_not_listed(self):
        """The set is read from the table, so a new edge cannot leave it stale."""
        assert states.TERMINATED in states.TERMINAL_STATES
        assert states.LAUNCH_FAILED in states.TERMINAL_STATES
        assert states.RUNNING not in states.TERMINAL_STATES
        assert states.PENDING not in states.TERMINAL_STATES

    def test_a_terminated_crew_cannot_be_reopened(self):
        """The home lived on that VM's disk and went with it, so there is nothing
        to reopen FROM. A crew under the same tag is a fresh launch."""
        for event in states.EVENTS:
            assert (states.TERMINATED, event) not in states.EDGES

    def test_a_launch_that_never_came_online_can_still_be_torn_down(self):
        """The VM may exist even though no guest ever answered, so the teardown
        edge has to leave PENDING as well as RUNNING."""
        assert states.EDGES[(states.PENDING, states.EVENT_TERMINATED)] == states.TERMINATED
        assert states.EDGES[(states.RUNNING, states.EVENT_TERMINATED)] == states.TERMINATED


class TestTransition:
    def test_a_launch_starts_from_no_record(self):
        assert states.transition(None, states.EVENT_LAUNCH_STARTED) == states.PENDING

    def test_the_happy_path(self):
        state = states.transition(None, states.EVENT_LAUNCH_STARTED)
        state = states.transition(state, states.EVENT_ONLINE)
        assert state == states.RUNNING
        state = states.transition(state, states.EVENT_TERMINATED)
        assert state == states.TERMINATED

    def test_a_launch_that_failed_is_not_a_terminated_crew(self):
        """The two differ by whether anything was ever created to tear down."""
        assert states.transition(states.PENDING, states.EVENT_LAUNCH_FAILED) == (
            states.LAUNCH_FAILED
        )

    def test_an_unknown_event_is_refused(self):
        with pytest.raises(states.IllegalTransition, match="unknown event"):
            states.transition(states.RUNNING, "take_a_nap")

    def test_an_unknown_state_is_refused(self):
        with pytest.raises(states.IllegalTransition, match="unknown state"):
            states.transition("sleepy", states.EVENT_ONLINE)

    def test_an_illegal_edge_names_the_events_that_are_legal(self):
        """The message has to be actionable: a caller bug needs the legal set."""
        with pytest.raises(states.IllegalTransition) as exc:
            states.transition(states.RUNNING, states.EVENT_ONLINE)
        assert states.EVENT_TERMINATED in str(exc.value)

    def test_a_terminal_state_accepts_nothing(self):
        for terminal in sorted(states.TERMINAL_STATES):
            for event in states.EVENTS:
                with pytest.raises(states.IllegalTransition):
                    states.transition(terminal, event)


class TestEffectiveState:
    def test_a_fresh_observation_is_repeated(self):
        assert states.effective_state(states.RUNNING, age_seconds=10) == states.RUNNING

    def test_a_stale_live_state_degrades_to_unknown(self):
        """A record saying RUNNING after an hour of silence is a guess, not a fact."""
        assert states.effective_state(states.RUNNING, age_seconds=3600) == states.EFFECTIVE_UNKNOWN

    def test_never_observed_is_unknown(self):
        assert states.effective_state(states.PENDING, age_seconds=None) == states.EFFECTIVE_UNKNOWN

    def test_a_terminal_state_is_reported_however_old(self):
        """Nothing can move a terminated crew, so an old reading IS the answer."""
        assert states.effective_state(states.TERMINATED, age_seconds=10**7) == states.TERMINATED
        assert (
            states.effective_state(states.LAUNCH_FAILED, age_seconds=None) == states.LAUNCH_FAILED
        )

    def test_the_boundary_is_inclusive_of_the_bound(self):
        at_bound = states.DEFAULT_STALE_AFTER_SECONDS
        assert states.effective_state(states.RUNNING, age_seconds=at_bound) == (states.RUNNING)
        assert (
            states.effective_state(states.RUNNING, age_seconds=at_bound + 1)
            == states.EFFECTIVE_UNKNOWN
        )

    def test_unknown_is_not_a_stored_state(self):
        """Storing it would make the next reader think the CREW had moved."""
        assert states.EFFECTIVE_UNKNOWN not in states.STORED_STATES
