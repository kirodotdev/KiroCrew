"""The one change this grammar makes to EXISTING ``[OPTIONS:]`` parsing, and the two
withdrawn ones whose absence the classes below pin.

Shipped, measured on the merge base with the change absent:
``split_trailing_protocol_suffix`` detaches a RUN of trailing markers, where base took
only the last and left the earlier one for a rotation to split. Withdrawn: the line form
stays line-final, so a marker sharing a line is not parsed; and the temper covers only
declared heads, so a label nesting the singular ``[OPTION:`` parses as it does on base.
"""

import pytest

from kiro_crew.constants import (
    _CASE_INSENSITIVE_MARKER_HEADS,
    _MARKER_BODY_TEMPER,
    OPTION_ACTIONS_RE_LINE,
    OPTION_ACTIONS_RE_TRAILER,
    OPTIONS_RE_LINE,
    OPTIONS_RE_TRAILER,
    split_trailing_protocol_suffix,
)


class TestOnlyALineFinalMarkerIsDispatched:
    SHARED = "[OPTIONS: a | b] [OPTIONS: c | d]"
    MID_PROSE = "Choose [OPTIONS: a | b][OPTIONS: c | d] to proceed."

    def test_a_marker_whose_line_continues_in_prose_is_not_dispatched(self):
        # A sibling terminator matches here, excising a mid-sentence marker and
        # raising its labels as live buttons.
        assert OPTIONS_RE_LINE.search(self.MID_PROSE) is None

    def test_the_trailing_marker_of_a_shared_line_is_the_one_found(self):
        found = [m.group("labels") for m in OPTIONS_RE_LINE.finditer(self.SHARED)]
        assert found == [" c | d"]

    def test_the_leading_marker_of_a_shared_line_is_left_unmatched(self):
        # Passed through verbatim; the remedy is consumer-side (select the last
        # marker, strip them all), not in the line terminator.
        assert OPTIONS_RE_LINE.match(self.SHARED, 0, len(self.SHARED)) is None

    def test_positive_control_a_line_final_marker_still_parses(self):
        m = OPTIONS_RE_LINE.search("Pick one.\n[OPTIONS: a | b]")
        assert m is not None and m.group("labels") == " a | b"


class TestABodyRefusesOnlyTheHeadsThisGrammarDefines:
    @pytest.mark.parametrize(
        "text",
        [
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

    def test_the_singular_head_is_not_one_of_them_and_still_parses(self):
        # Refusing it would change shipped [OPTIONS:] parsing.
        text = "[OPTIONS: see [OPTION: x] below | Skip]"
        match = OPTIONS_RE_LINE.match(text, 0, len(text))
        assert match is not None
        assert match.group("labels") == " see [OPTION: x] below | Skip"

    def test_a_refused_outer_marker_does_not_take_the_text_before_it(self):
        # The nested declared head is the located one, so only the complete inner
        # marker detaches and the refused outer text stays visible.
        visible, suffix = split_trailing_protocol_suffix(
            "prose\n[OPTIONS: see [OPTIONS: x] | Skip]"
        )
        assert visible == "prose\n[OPTIONS: see "
        assert suffix == "[OPTIONS: x] | Skip]"


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
    """A head nested BEFORE the content closer reaches neither the line tail nor the
    trailer's refusal, so the temper is the only guard and must match every casing.
    """

    @pytest.mark.parametrize("sibling", ["OPTION-ACTIONS:", "Option-Actions:", "option-actions:"])
    def test_a_head_nested_before_the_closer_is_refused_in_every_casing(self, sibling):
        text = f"[OPTIONS: A [{sibling} close=B] C]"
        assert OPTIONS_RE_LINE.match(text, 0, len(text)) is None

    @pytest.mark.parametrize("sibling", ["OPTION-ACTIONS:", "Option-Actions:", "option-actions:"])
    def test_the_line_form_does_not_reach_a_non_final_leading_marker(self, sibling):
        # The temper, not the terminator, keeps the action head out of a label.
        text = f"[OPTIONS: A] [{sibling} close=B]"
        assert OPTIONS_RE_LINE.match(text, 0, len(text)) is None

    def test_the_temper_covers_every_case_insensitive_head(self):
        # Drift pin: a head added to the set without its scoped (?i:...) branch in
        # the temper reddens here rather than silently widening the body.
        for head in _CASE_INSENSITIVE_MARKER_HEADS:
            assert f"(?i:{head})" in _MARKER_BODY_TEMPER

    @pytest.mark.parametrize("sibling", ["OPTION-ACTIONS:", "Option-Actions:", "option-actions:"])
    def test_the_trailer_declines_while_a_second_marker_follows(self, sibling):
        text = f"[OPTIONS: A]\n[{sibling} close=B]"
        assert OPTIONS_RE_TRAILER.match(text, 0, len(text)) is None

    def test_positive_control_the_trailer_matches_when_it_ends_the_buffer(self):
        text = "[OPTIONS: A]"
        assert OPTIONS_RE_TRAILER.match(text, 0, len(text)) is not None


class TestAnActionMarkerCarriesOneNonemptyCloseEntry:
    """On the content body, a body the action grammar has no meaning for is still
    detached as protocol, dropping the trailing text instead of leaving it as prose.
    """

    @pytest.mark.parametrize(
        "body",
        [
            " close=A | close=B",  # two entries
            " close=A , close=B",
            " open=A",  # a key no consumer implements
            " close=",  # empty label
            "",
        ],
    )
    def test_a_body_that_is_not_one_nonempty_close_entry_is_refused(self, body):
        line = f"[OPTION-ACTIONS:{body}]"
        assert OPTION_ACTIONS_RE_LINE.match(line, 0, len(line)) is None
        assert OPTION_ACTIONS_RE_TRAILER.match(line, 0, len(line)) is None

    @pytest.mark.parametrize(
        "body",
        [" close=Stop", " close=Close this tab", " close=See [1] later", "close=x"],
    )
    def test_positive_control_one_nonempty_close_entry_still_parses(self, body):
        line = f"[OPTION-ACTIONS:{body}]"
        for pattern in (OPTION_ACTIONS_RE_LINE, OPTION_ACTIONS_RE_TRAILER):
            match = pattern.match(line, 0, len(line))
            assert match is not None
            assert match.group("labels") == body

    def test_a_refused_body_is_left_as_prose_rather_than_detached(self):
        text = "prose\n[OPTION-ACTIONS: close=A | close=B]"
        visible, suffix = split_trailing_protocol_suffix(text)
        assert suffix == ""
        assert visible == text

    @pytest.mark.parametrize(
        "tail",
        [
            "[OPTION-ACTIONS: open=Settings",
            "[OPTION-ACTIONS: close=A | close=B",
            "[OPTION-ACTIONS: closet=A",
        ],
    )
    def test_an_unfinished_body_that_can_never_complete_stays_visible(self, tail):
        # Detached as a streaming prefix it is DISCARDED at on_done, silently.
        text = "prose\n" + tail
        visible, suffix = split_trailing_protocol_suffix(text)
        assert suffix == ""
        assert visible == text

    @pytest.mark.parametrize(
        "tail",
        ["[OPTION-ACTIONS", "[OPTION-ACTIONS: clo", "[OPTION-ACTIONS: close=Sto"],
    )
    def test_positive_control_a_completable_prefix_is_still_held(self, tail):
        _, suffix = split_trailing_protocol_suffix("prose\n" + tail)
        assert suffix == tail
