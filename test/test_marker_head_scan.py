"""The marker head scans, cased per head.

The two heads are not prefixes of one another -- they diverge at ``S`` vs ``-`` -- so a
scan written against the content head alone is blind to an action head rather than
merely imprecise. Each scan is cased by the rule its own pattern uses: the action head
is matched either way, the content head only as spelled.
"""

from __future__ import annotations

from kiro_crew.constants import (
    MARKER_PREFIXES,
    marker_head_len,
    marker_prefix_is_case_insensitive,
    rfind_marker_head,
    starts_with_marker_head,
)

ACTION_HEAD = "[OPTION-ACTIONS"
CONTENT_HEAD = "[OPTIONS"


class TestTheHeadsAreOrderedLongestDistinguishingFirst:
    def test_both_heads_are_present(self):
        assert MARKER_PREFIXES == (ACTION_HEAD, CONTENT_HEAD)

    def test_neither_head_is_a_prefix_of_the_other(self):
        """The whole reason a single-literal scan cannot serve both."""
        assert not ACTION_HEAD.startswith(CONTENT_HEAD)
        assert not CONTENT_HEAD.startswith(ACTION_HEAD)

    def test_only_the_action_head_is_matched_case_insensitively(self):
        assert marker_prefix_is_case_insensitive(ACTION_HEAD)
        assert not marker_prefix_is_case_insensitive(CONTENT_HEAD)


class TestMarkerHeadLen:
    """A caller about to slice past the head needs THIS head's length, not a guess."""

    def test_the_action_head_reports_its_own_length(self):
        assert marker_head_len("[OPTION-ACTIONS: close=X", 0) == len(ACTION_HEAD)

    def test_the_content_head_reports_its_own_length(self):
        assert marker_head_len("[OPTIONS: A | B]", 0) == len(CONTENT_HEAD)

    def test_a_mixed_case_action_head_still_reports_the_action_length(self):
        """Cut by the content length instead, a caller lands inside ``-ACTIONS:``."""
        assert marker_head_len("[option-actions: close=X", 0) == len(ACTION_HEAD)

    def test_the_index_is_honoured_rather_than_assumed_to_be_zero(self):
        assert marker_head_len("prose [OPTION-ACTIONS: close=X", 6) == len(ACTION_HEAD)

    def test_no_head_at_the_index_falls_back_to_the_shortest(self):
        assert marker_head_len("nothing here", 0) == len(CONTENT_HEAD)


class TestRfindMarkerHead:
    def test_the_rightmost_head_wins_when_the_action_head_is_last(self):
        text = "[OPTIONS: A]\n[OPTION-ACTIONS: close=X"
        assert rfind_marker_head(text) == text.index(ACTION_HEAD)

    def test_the_rightmost_head_wins_when_the_content_head_is_last(self):
        """The mirror case, so the result is not an artifact of scan order."""
        text = "[OPTION-ACTIONS: close=X\n[OPTIONS: A]"
        assert rfind_marker_head(text) == text.index("[OPTIONS: A]")

    def test_a_mixed_case_action_head_is_found(self):
        """Missed here, an unfinished mixed-case marker is sealed as raw text."""
        assert rfind_marker_head("[option-actions: close=X") == 0

    def test_a_mixed_case_content_head_is_not_found(self):
        """Its pattern is case-sensitive, so widening the scan would outrun the strip."""
        assert rfind_marker_head("[options: A]") == -1

    def test_absent_is_minus_one(self):
        assert rfind_marker_head("no head at all") == -1


class TestStartsWithMarkerHead:
    def test_each_head_is_recognised_at_the_start(self):
        assert starts_with_marker_head("[OPTIONS: A]")
        assert starts_with_marker_head("[OPTION-ACTIONS: close=X]")

    def test_a_mixed_case_action_head_is_recognised(self):
        """A lowercase action line carries pipes, so a table run would absorb it."""
        assert starts_with_marker_head("[Option-Actions: close=X]")

    def test_a_mixed_case_content_head_is_not(self):
        assert not starts_with_marker_head("[options: A]")

    def test_the_check_is_anchored_rather_than_a_scan(self):
        """Leading whitespace means the line does not START with a head."""
        assert not starts_with_marker_head(" [OPTIONS: A]")

    def test_prose_is_not_a_head(self):
        assert not starts_with_marker_head("prose")
