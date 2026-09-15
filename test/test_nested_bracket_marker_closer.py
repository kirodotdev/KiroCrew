"""A citation bracket inside an unfinished marker must not close it.

The batch path already applies this predicate (`_unclosed_marker_flags`); the streaming twin
took the first closer, so a citation inside an open head cancelled it and the remainder of the
line streamed to the user as answer text.
"""

from __future__ import annotations

from kiro_crew.constants import (
    OPTION_ACTIONS_RE_TRAILER,
    OPTIONS_RE_LINE,
    OPTIONS_RE_TRAILER,
    _is_inside_unclosed_marker,
    _unclosed_marker_flags,
    excise_marker_spans,
    match_action_markers,
    split_trailing_protocol_suffix,
    strip_action_markers,
)


class TestNestedBracketDoesNotCloseAnUnfinishedMarker:
    def test_unterminated_head_with_a_citation_drops_from_the_head_onward(self):
        """The exact shape GPT graded blocking: no closer of its own, so nothing survives it."""
        text = "Answer.\n[OPTION-ACTIONS: close=See [1] later"
        assert excise_marker_spans(text) == "Answer.\n"

    def test_the_trailing_prose_is_not_released(self):
        """`later` sat after the citation's `]`, which the first-closer scan took as the end."""
        assert " later" not in excise_marker_spans("[OPTION-ACTIONS: close=See [1] later")

    def test_a_terminated_head_containing_a_citation_excises_the_whole_span(self):
        """The marker DOES close here, so only the marker goes and the tail is kept."""
        text = "Answer.\n[OPTION-ACTIONS: close=See [1] now] tail"
        assert excise_marker_spans(text) == "Answer.\n tail"

    def test_a_citation_before_the_head_is_untouched(self):
        """A bracket that never opened a marker is ordinary text."""
        text = "See [1] for context.\n[OPTIONS: Alpha | Bravo]"
        assert excise_marker_spans(text) == "See [1] for context.\n"

    def test_two_nested_citations_still_need_the_marker_s_own_closer(self):
        text = "[OPTIONS: a [1] b [2] c] kept"
        assert excise_marker_spans(text) == " kept"

    def test_a_stray_closer_cannot_end_a_head_it_never_opened(self):
        """Depth cannot go below zero from text preceding the head."""
        text = "prose] more\n[OPTIONS: a [1] b"
        assert excise_marker_spans(text) == "prose] more\n"

    def test_the_seam_case_the_docstring_names_still_holds(self):
        """Excising the inner marker forms `[OPTIONS: b]` at the join; it must also go."""
        assert excise_marker_spans("[OPTI[OPTIONS: a]ONS: b]") == ""

    def test_linear_on_many_markers(self):
        """Each character is scanned by at most one closer walk, so this stays cheap."""
        text = "[OPTIONS: a [1] b] x " * 2000
        assert excise_marker_spans(text) == " x " * 2000


class TestAnchoredMatcherAppliesTheBalanceDecision:
    """The anchored member is a matcher member, so it decides balance like the rest.

    ``split_trailing_protocol_suffix`` asks whether a marker begins EXACTLY at a
    candidate head, which a scan cannot answer — a scan says yes for a marker further
    along. That anchored question still has to reach the balance decision: a candidate
    whose terminating closer belongs to an unmatched opener is not a marker, and
    returning it would delete the prose the closer really belongs to.
    """

    def test_an_unmatched_opener_is_refused_at_the_anchor(self):
        text = "[OPTIONS: A | B then check arr[0]"
        assert OPTIONS_RE_LINE.match(text, 0) is None

    def test_positive_control_a_balanced_candidate_is_accepted_at_the_anchor(self):
        """Without this the refusal above could pass for the wrong reason."""
        matched = OPTIONS_RE_LINE.match("[OPTIONS: A | B]", 0)
        assert matched is not None
        assert matched.group("labels") == " A | B"

    def test_the_anchor_is_respected_rather_than_scanned_for(self):
        """A marker further along must not satisfy an anchored ask at position 0."""
        assert OPTIONS_RE_LINE.match("prose first\n[OPTIONS: A | B]", 0) is None


class TestTheActionFamilyAppliesTheSameBalanceDecision:
    """Both heads reach the balance decision, so neither can take a label's own closer.

    The body admits a bare closer -- its negated class excludes ``[`` and newline but
    not the closers -- so a terminator can be a closer belonging to LABEL CONTENT.
    Labels are free text, so an ordinary ``close=See arr[0]`` supplies exactly that
    shape in ordinary operation.

    MEASURED while the action patterns were raw regexes, with the wrapped ``OPTIONS``
    family as the control: ``OPTIONS_RE_TRAILER.match("[OPTIONS: See arr[0]", 0)``
    refused, while the action trailer ACCEPTED
    ``"[OPTION-ACTIONS: close=See arr[0]"`` with ``labels=" close=See arr[0"`` -- an
    unmatched opener sitting at the terminator. Neither call path failed loudly: the
    ``\\Z`` form let ``split_trailing_protocol_suffix`` detach the run as a protocol
    suffix, and the LINE form let ``strip_action_markers`` return ``""`` for that whole
    input, deleting the prose it exists to preserve.
    """

    def test_an_unmatched_opener_is_refused_at_the_anchor(self):
        text = "[OPTION-ACTIONS: close=See arr[0]"
        assert OPTION_ACTIONS_RE_TRAILER.match(text, 0) is None

    def test_positive_control_a_balanced_action_marker_is_accepted_at_the_anchor(self):
        """Without this the refusal above could pass for the wrong reason."""
        matched = OPTION_ACTIONS_RE_TRAILER.match("[OPTION-ACTIONS: close=Close this tab]", 0)
        assert matched is not None
        assert matched.group("labels") == " close=Close this tab"

    def test_the_two_heads_agree_on_the_same_unbalanced_shape(self):
        """Parity is the property: a shape one family refuses cannot pass in the other."""
        assert OPTIONS_RE_TRAILER.match("[OPTIONS: See arr[0]", 0) is None
        assert OPTION_ACTIONS_RE_TRAILER.match("[OPTION-ACTIONS: close=See arr[0]", 0) is None

    def test_an_unbalanced_label_is_not_detached_as_a_protocol_suffix(self):
        """Detaching it hands the tail to a renderer that reattaches it past the split."""
        text = "Here is the answer.\n[OPTION-ACTIONS: close=See arr[0]"
        assert split_trailing_protocol_suffix(text) == (text, "")

    def test_positive_control_a_balanced_marker_is_still_detached(self):
        marker = "[OPTION-ACTIONS: close=Close this tab]"
        assert split_trailing_protocol_suffix(f"Answer.\n{marker}") == ("Answer.\n", marker)

    def test_an_unbalanced_label_is_not_matched_or_stripped(self):
        """The stripper must leave a refused span visible: it is the only cue to the user."""
        text = "[OPTION-ACTIONS: close=See arr[0]"
        assert match_action_markers(text) == []
        assert strip_action_markers(text) == text

    def test_positive_control_a_bracket_balanced_inside_the_label_still_strips(self):
        """A label may carry brackets; only an UNMATCHED opener disqualifies it."""
        text = "[OPTION-ACTIONS: close=Open [docs] now]"
        assert len(match_action_markers(text)) == 1
        assert strip_action_markers(text) == ""


class TestDepthDecidesWhetherAnActionSitsInsideAnUnclosedHead:
    """Per-line bracket DEPTH, not the last head against the last closer.

    Each branch of the walk is a measured defect. A bare count let a citation inside an
    open head supply the closer that cancelled it, so a live close chip rendered from
    syntax matching no content marker at all. A pairwise last-head/last-closer test was
    wrong for a BALANCED nested pair inside an unclosed head: the pair's own closer
    became the last closer, the head read as closed, and a following sibling was
    accepted while the outer head stayed open.
    """

    def test_a_citation_inside_an_open_head_does_not_release_a_nested_action(self):
        text = "[OPTIONS: see [1] for details [OPTION-ACTIONS: close=X]"
        assert match_action_markers(text) == []

    def test_a_balanced_pair_inside_an_open_head_leaves_it_open(self):
        text = "[OPTIONS: a [1] b [OPTION-ACTIONS: close=X]"
        assert match_action_markers(text) == []

    def test_a_newline_ends_an_open_head_so_the_next_line_is_free(self):
        """Both heads are LINE forms, so an unclosed head cannot reach across a newline."""
        text = "[OPTIONS: unclosed\n[OPTION-ACTIONS: close=X]"
        assert len(match_action_markers(text)) == 1

    def test_a_stray_closer_cannot_cancel_a_head_it_never_opened(self):
        """Popping an empty stack is a no-op, so depth never goes negative."""
        text = "prose] more [OPTION-ACTIONS: close=X]"
        assert len(match_action_markers(text)) == 1

    def test_positive_control_a_closed_head_does_not_suppress_a_sibling(self):
        text = "[OPTIONS: a | b]\n[OPTION-ACTIONS: close=X]"
        assert len(match_action_markers(text)) == 1

    def test_the_single_offset_twin_agrees_with_the_plural_form(self):
        """Two copies of this predicate that disagreed would show a chip for live text."""
        text = "[OPTIONS: see [1] for details [OPTION-ACTIONS: close=X]"
        offset = text.index("[OPTION-ACTIONS")
        assert _is_inside_unclosed_marker(text, offset) is True
        assert _unclosed_marker_flags(text, [offset]) == [True]

    def test_the_twin_agrees_on_the_negative_case_too(self):
        text = "[OPTIONS: a | b]\n[OPTION-ACTIONS: close=X]"
        offset = text.index("[OPTION-ACTIONS")
        assert _is_inside_unclosed_marker(text, offset) is False
        assert _unclosed_marker_flags(text, [offset]) == [False]

    def test_several_ascending_offsets_are_answered_in_one_walk(self):
        """The plural form exists because a per-offset rescan was linear in the prefix."""
        text = "[OPTIONS: open [OPTION-ACTIONS: close=A]\n[OPTION-ACTIONS: close=B]"
        starts = [i for i in range(len(text)) if text.startswith("[OPTION-ACTIONS", i)]
        assert _unclosed_marker_flags(text, starts) == [True, False]
