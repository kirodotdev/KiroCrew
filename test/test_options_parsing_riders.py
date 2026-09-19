"""The three changes this grammar makes to EXISTING ``[OPTIONS:]`` parsing.

They are not additive. Each is pinned here against the base defect it fixes, measured on
the merge base with the change absent:

1. A leading marker on a SHARED line now parses. Base found only the trailing one, and
   an unmatched marker is not merely unparsed -- it is passed through VERBATIM, posted
   raw into a channel body, spoken by TTS and left in the sidebar preview.
2. A body now refuses the singular ``[OPTION:`` head. Base consumed it, so an
   ``[OPTIONS:]`` label holding one matched from the OUTER head and rendered a pill whose
   label was a raw protocol marker -- which the frontend declines to parse at all, so the
   two sides disagreed about the same string.
3. ``split_trailing_protocol_suffix`` detaches a RUN of trailing markers. Base detached
   only the last, leaving the earlier one in the visible half where a rotation splits it.
"""

import pytest

from kiro_crew.constants import (
    OPTIONS_RE_LINE,
    OPTIONS_RE_TRAILER,
    split_trailing_protocol_suffix,
)


class TestALeadingMarkerOnASharedLineParses:
    SHARED = "[OPTIONS: a | b] [OPTIONS: c | d]"

    def test_both_markers_are_found(self):
        found = [m.group("labels") for m in OPTIONS_RE_LINE.finditer(self.SHARED)]
        assert found == [" a | b", " c | d"]

    def test_the_leading_marker_is_no_longer_left_verbatim(self):
        leading = OPTIONS_RE_LINE.match(self.SHARED, 0, len(self.SHARED))
        assert leading is not None
        assert leading.group("labels") == " a | b"

    def test_a_sibling_terminates_the_body_rather_than_being_eaten(self):
        leading = OPTIONS_RE_LINE.match(self.SHARED, 0, len(self.SHARED))
        assert "OPTIONS" not in leading.group("labels")

    def test_positive_control_trailing_prose_still_does_not_terminate(self):
        # Only a SIBLING marker ends a line-form body; a sentence discussing the
        # syntax is still left alone.
        assert OPTIONS_RE_LINE.match("[OPTIONS: a | b] and then some prose", 0, 36) is None


class TestABodyRefusesTheSingularHead:
    @pytest.mark.parametrize(
        "text",
        [
            "[OPTIONS: see [OPTION: x] below | Skip]",
            "[OPTIONS: see [OPTIONS: x] below | Skip]",
            "[OPTIONS: see [OPTION-ACTIONS: close=x] | Skip]",
        ],
    )
    def test_a_label_holding_a_head_is_refused_by_both_forms(self, text):
        assert OPTIONS_RE_LINE.match(text, 0, len(text)) is None
        assert OPTIONS_RE_TRAILER.match(text, 0, len(text)) is None

    @pytest.mark.parametrize(
        "text,labels",
        [
            ("[OPTIONS: fix [x] logging | Skip]", " fix [x] logging | Skip"),
            ("[OPTIONS: check arr[0] | Skip]", " check arr[0] | Skip"),
            (
                "[OPTIONS: an OPTION: without a bracket | Skip]",
                " an OPTION: without a bracket | Skip",
            ),
        ],
    )
    def test_positive_control_ordinary_nesting_still_parses(self, text, labels):
        # The refusal is keyed on a bracket that BEGINS a head, never on the word
        # appearing in prose, so an ordinary nested pair is untouched.
        match = OPTIONS_RE_LINE.match(text, 0, len(text))
        assert match is not None
        assert match.group("labels") == labels

    def test_the_refused_shape_is_not_detached_as_a_protocol_suffix(self):
        text = "prose\n[OPTIONS: see [OPTION: x] | Skip]"
        visible, suffix = split_trailing_protocol_suffix(text)
        assert suffix == ""
        assert visible == text


class TestARunOfTrailingMarkersIsDetachedWhole:
    def test_two_markers_are_kept_together_on_the_tail(self):
        visible, suffix = split_trailing_protocol_suffix(
            "prose\n[OPTIONS: a | b]\n[OPTIONS: c | d]"
        )
        assert visible == "prose\n"
        assert suffix == "[OPTIONS: a | b]\n[OPTIONS: c | d]"

    def test_the_earlier_marker_is_no_longer_exposed_to_a_split(self):
        _, suffix = split_trailing_protocol_suffix(
            "prose\n[OPTIONS: a | b]\n[OPTION-ACTIONS: close=Close]"
        )
        assert suffix.count("[OPTION") == 2

    def test_the_walk_stops_at_the_first_non_marker(self):
        visible, suffix = split_trailing_protocol_suffix("prose\nnot a marker\n[OPTIONS: c | d]")
        assert visible == "prose\nnot a marker\n"
        assert suffix == "[OPTIONS: c | d]"

    def test_positive_control_a_lone_trailing_marker_is_unchanged(self):
        visible, suffix = split_trailing_protocol_suffix("prose\n[OPTIONS: a | b]")
        assert visible == "prose\n"
        assert suffix == "[OPTIONS: a | b]"


class TestMixedCaseActionSiblingIsNotCapturedAsALabel:
    """Why the temper may stay case-sensitive on the action head.

    Neither rescue is the temper. If a later change removes the LINE tail's
    sibling lookahead, or lets the TRAILER match while another marker follows,
    these turn red -- which is the point.
    """

    @pytest.mark.parametrize("sibling", ["OPTION-ACTIONS:", "Option-Actions:", "option-actions:"])
    def test_the_line_tail_terminates_the_body_at_the_sibling(self, sibling):
        text = f"[OPTIONS: A] [{sibling} close=B]"
        match = OPTIONS_RE_LINE.match(text, 0, len(text))
        assert match is not None
        assert match.group("labels") == " A"

    @pytest.mark.parametrize("sibling", ["OPTION-ACTIONS:", "Option-Actions:", "option-actions:"])
    def test_the_trailer_declines_while_a_second_marker_follows(self, sibling):
        text = f"[OPTIONS: A]\n[{sibling} close=B]"
        assert OPTIONS_RE_TRAILER.match(text, 0, len(text)) is None

    def test_positive_control_the_trailer_matches_when_it_ends_the_buffer(self):
        text = "[OPTIONS: A]"
        assert OPTIONS_RE_TRAILER.match(text, 0, len(text)) is not None
