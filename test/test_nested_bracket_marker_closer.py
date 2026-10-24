"""A label's own bracket must not close a marker head.

Both marker families go through the balance decision, so a closer that belongs to an
unmatched opener inside a label is never taken as the marker's own terminator.
"""

from __future__ import annotations

from kiro_crew.constants import (
    OPTION_ACTIONS_RE_TRAILER,
    OPTIONS_RE_LINE,
    OPTIONS_RE_TRAILER,
    split_trailing_protocol_suffix,
)


class TestTheContentFamilyAppliesTheBalanceDecision:
    """A candidate whose terminating closer belongs to an unmatched opener inside the
    label is not a marker, and returning it would delete the prose the closer belongs to.
    """

    def test_an_unmatched_opener_is_refused(self):
        assert OPTIONS_RE_LINE.search("[OPTIONS: A | B then check arr[0]") is None

    def test_positive_control_a_balanced_candidate_is_accepted(self):
        """Without this the refusal above could pass for the wrong reason."""
        matched = OPTIONS_RE_LINE.search("[OPTIONS: A | B]")
        assert matched is not None
        assert matched.group("labels") == " A | B"


class TestTheActionFamilyAppliesTheSameBalanceDecision:
    """Both heads reach the balance decision, so neither can take a label's own closer.

    The body admits a bare closer -- its negated class excludes ``[`` and newline but
    not the closers -- so a terminator can be a closer belonging to LABEL CONTENT.
    Labels are free text, so an ordinary ``close=See arr[0]`` supplies exactly that
    shape in ordinary operation.

    MEASURED while the action patterns were raw regexes, with the wrapped ``OPTIONS``
    family as the control: ``OPTIONS_RE_TRAILER.search("[OPTIONS: See arr[0]")``
    refused, while the action trailer ACCEPTED
    ``"[OPTION-ACTIONS: close=See arr[0]"`` with ``labels=" close=See arr[0"`` -- an
    unmatched opener sitting at the terminator. The call path did not fail loudly: the
    ``\\Z`` form let ``split_trailing_protocol_suffix`` detach the tail as a protocol
    suffix, so the prose the closer belongs to was deleted from the rendered cut.
    """

    def test_an_unmatched_opener_is_refused(self):
        text = "[OPTION-ACTIONS: close=See arr[0]"
        assert OPTION_ACTIONS_RE_TRAILER.search(text) is None

    def test_positive_control_a_balanced_action_marker_is_accepted(self):
        """Without this the refusal above could pass for the wrong reason."""
        matched = OPTION_ACTIONS_RE_TRAILER.search("[OPTION-ACTIONS: close=Close this tab]")
        assert matched is not None
        assert matched.group("labels") == " close=Close this tab"

    def test_the_two_heads_agree_on_the_same_unbalanced_shape(self):
        """Parity is the property: a shape one family refuses cannot pass in the other."""
        assert OPTIONS_RE_TRAILER.search("[OPTIONS: See arr[0]") is None
        assert OPTION_ACTIONS_RE_TRAILER.search("[OPTION-ACTIONS: close=See arr[0]") is None

    def test_an_unbalanced_label_is_not_detached_as_a_protocol_suffix(self):
        """Detaching it hands the tail to a renderer that reattaches it past the split."""
        text = "Here is the answer.\n[OPTION-ACTIONS: close=See arr[0]"
        assert split_trailing_protocol_suffix(text) == (text, "")

    def test_positive_control_a_balanced_marker_is_still_detached(self):
        marker = "[OPTION-ACTIONS: close=Close this tab]"
        assert split_trailing_protocol_suffix(f"Answer.\n{marker}") == ("Answer.\n", marker)

    def test_positive_control_a_bracket_balanced_inside_the_label_still_matches(self):
        """A label may carry brackets; only an UNMATCHED opener disqualifies it."""
        text = "[OPTION-ACTIONS: close=Open [docs] now]"
        matched = OPTION_ACTIONS_RE_TRAILER.search(text)
        assert matched is not None
        assert matched.span() == (0, len(text))
