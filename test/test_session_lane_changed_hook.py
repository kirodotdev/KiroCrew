"""Contract tests for the ``SessionLaneChanged`` hook event.

The matcher grammar is FROZEN CONTRACT from the first subscriber onward: the token
spelling is what a hook author writes a selector against, so changing it breaks every
registered hook silently rather than loudly. Before this file the grammar was pinned
only by a docstring, which no test reads.

Pure functions only: no filesystem, no subprocess, no event loop.
"""

from __future__ import annotations

from kiro_crew.hooks import (
    HOOK_EVENT_SESSION_LANE_CHANGED,
    HOOK_EVENTS,
    _session_lane_matcher_context,
)
from kiro_crew.validation import ALLOWED_HOOK_EVENTS


class TestTheEventIsRegistered:
    def test_the_wire_value_is_the_name_a_hook_author_writes(self):
        assert HOOK_EVENT_SESSION_LANE_CHANGED == "SessionLaneChanged"

    def test_both_registries_admit_it_so_the_api_accepts_a_hook_on_it(self):
        assert HOOK_EVENT_SESSION_LANE_CHANGED in HOOK_EVENTS
        assert HOOK_EVENT_SESSION_LANE_CHANGED in ALLOWED_HOOK_EVENTS


class TestTheTokenGrammar:
    """Each bound earns its place: without the ``;`` a selector for a short id also
    fires for an id it PREFIXES, and without the ``:`` for one it is a SUFFIX of. For
    a close-out hook either is an irreversible action on the wrong session."""

    def test_a_token_is_direction_tagged_and_terminated(self):
        assert _session_lane_matcher_context(["done"], []) == "added:done;"
        assert _session_lane_matcher_context([], ["done"]) == "removed:done;"

    def test_both_directions_join_with_a_space_added_first(self):
        assert _session_lane_matcher_context(["done"], ["review"]) == "added:done; removed:review;"

    def test_the_terminator_stops_a_short_id_matching_one_it_prefixes(self):
        assert "added:rev;" not in _session_lane_matcher_context(["review"], [])

    def test_the_colon_stops_a_short_id_matching_one_it_suffixes(self):
        assert ":done;" not in _session_lane_matcher_context(["xdone"], [])

    def test_a_repeated_id_yields_one_token(self):
        assert _session_lane_matcher_context(["done", "done"], []) == "added:done;"

    def test_a_name_is_never_a_token_only_the_id_is(self):
        assert _session_lane_matcher_context(["in_review"], []) == "added:in_review;"


class TestAnUnsafeIdIsSkippedNotRewritten:
    """An id is skipped rather than sanitized: a collapsing rewrite is many-to-one, so
    two distinct lanes can end up sharing one token and each fire the other's hook."""

    def test_a_glob_metacharacter_id_cannot_widen_the_selector(self):
        assert _session_lane_matcher_context(["*"], []) == ""

    def test_an_uppercase_id_is_skipped_because_matching_folds_case(self):
        assert _session_lane_matcher_context(["Done"], []) == ""

    def test_a_dot_is_admitted_so_a_hand_named_lane_still_matches(self):
        assert _session_lane_matcher_context(["in.review"], []) == "added:in.review;"

    def test_a_non_string_id_from_a_hand_edited_store_does_not_raise(self):
        assert _session_lane_matcher_context([None, 7], []) == ""  # type: ignore[list-item]

    def test_one_bad_id_degrades_only_itself(self):
        assert _session_lane_matcher_context(["Done", "done"], []) == "added:done;"


class TestAnEmptyContextMeansDoNotFire:
    """``fire`` consults a matcher ONLY when the context is non-empty, so handing it an
    empty context skips filtering and runs EVERY hook registered for the event, on a
    lane none of them named."""

    def test_no_ids_at_all_yields_empty(self):
        assert _session_lane_matcher_context([], []) == ""

    def test_every_id_invalid_yields_empty_so_the_caller_refuses(self):
        assert _session_lane_matcher_context(["Done", "*"], ["In Review"]) == ""
