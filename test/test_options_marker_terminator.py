"""The terminating closer must not be an unmatched opener's PARTNER.

The label body admits a bare ``[`` on purpose, so a stray opener inside a label does
not sink the marker -- ``[OPTIONS: Fix [x logging | Skip]`` parses, and is pinned in
``test_options_marker_label_closers.py``. That same alternative admitted the opener
in a marker the model never closed::

    [OPTIONS: A | B then check arr[0]

The only closer on that line belongs to ``arr[0]``. The body ran on through the
prose, that ``]`` became the terminator, and because every consumer removes the
whole match -- ``parse_options`` replaces, ``slack.format`` and ``messaging.renderer``
cut at ``match.start()``, and ``whatsapp.turn_renderer`` PERSISTS the cut turn -- the
line was deleted from the message and came back as the pill label
``B then check arr[0``.

WHY THE PATTERN CANNOT DECIDE IT. Reduce the shape that must be REFUSED and the one
that must be ACCEPTED to their bracket structure and they are the same string::

    [OPTIONS: Fix | Skip [x logging]        ->  w|w[w]
    [OPTIONS: A | B then check arr[0]       ->  w|w[w]

So no rule over brackets alone separates them. And a lookahead inside the pattern
can only see one nesting level: ``list[dict[str, int]]`` defeats a one-level rule and
``a[b[c[d]]]`` defeats a two-level one. The condition wanted is "the terminator is
not nested", which is bracket BALANCE -- not a regular language at unbounded depth.

So the decision lives OUTSIDE the pattern, in
:func:`kiro_crew.constants._marker_labels_have_unmatched_opener`, and the raw patterns are
private so no caller can obtain an unchecked match. What callers get is
:data:`OPTIONS_RE_LINE` / :data:`OPTIONS_RE_TRAILER`, which apply both.

Two claims are asserted separately throughout, as elsewhere in this grammar's
suites, because only the second is what a user experiences: that no marker is
FOUND, and that the visible text is UNCHANGED.
"""

from __future__ import annotations

import time

import pytest

from kiro_crew.constants import (
    _RAW_OPTIONS_RE_LINE,
    MARKER_CLOSERS,
    OPTIONS_RE_LINE,
    OPTIONS_RE_TRAILER,
    _marker_labels_have_unmatched_opener,
)
from kiro_crew.messaging.renderer import split_options_trailer


def skeleton(text: str) -> str:
    """Bracket/separator structure, with every other run collapsed to ``w``."""
    body = text[len("[OPTIONS:") :] if text.startswith("[OPTIONS:") else text
    out: list[str] = []
    for ch in body:
        if ch in "[]|," or ch in MARKER_CLOSERS:
            out.append(ch)
        elif not out or out[-1] != "w":
            out.append("w")
    return "".join(out)


class TestAnUnterminatedMarkerDoesNotEatItsLine:
    @pytest.mark.parametrize(
        "text",
        [
            "[OPTIONS: A | B then check arr[0]",
            "[OPTIONS: Ship | Hold and read docs[4]",
            "[OPTIONS: Merge | Wait then diff src/app[3]",
            # A comma INSIDE the terminal bracket: ordinary punctuation, not a label
            # boundary, so it must not rescue the shape.
            "[OPTIONS: A | B then inspect dict[str, int]",
            "[OPTIONS: Ship | Hold then arr[i, j]",
            # The wrapper and stray-tic forms reach the same terminator, so the check
            # has to survive both or the shape returns wearing one.
            "**[OPTIONS: A | B then check arr[0]**",
            "[OPTIONS: A | B then check arr[0](OPTIONS)",
        ],
    )
    def test_it_is_refused(self, text: str):
        assert OPTIONS_RE_LINE.search(text) is None, text

    @pytest.mark.parametrize(
        "text",
        [
            "[OPTIONS: A | B then check arr[0]",
            "[OPTIONS: A | B then inspect dict[str, int]",
        ],
    )
    def test_and_therefore_deletes_nothing(self, text: str):
        # The claim that matters, asserted at the consumer: removing the match is
        # what deleted the line.
        assert OPTIONS_RE_LINE.sub("", text) == text, text
        assert split_options_trailer(text) == (text, []), text

    def test_nesting_of_ANY_depth_is_refused(self):
        """The part a lookahead in the pattern could never reach.

        Each of these defeats a rule that looks one level deeper than the last, which
        is why the check counts depth instead of matching a shape.
        """
        for depth in range(1, 7):
            inner = "x"
            for _ in range(depth):
                inner = f"a[{inner}]"
            text = f"[OPTIONS: A | B then see {inner}"[:-1]
            assert OPTIONS_RE_LINE.search(text) is None, (depth, text)

    @pytest.mark.parametrize(
        "text",
        [
            "[OPTIONS: A | B then inspect list[dict[str, int]]",
            "[OPTIONS: A | B then inspect a[b[c[d]]]",
            "[OPTIONS: A | B see a[b[c[d[e]]]]",
        ],
    )
    def test_the_reported_nested_shapes_are_refused_and_nothing_is_deleted(self, text: str):
        assert OPTIONS_RE_LINE.search(text) is None, text
        assert OPTIONS_RE_LINE.sub("", text) == text, text

    def test_the_trailer_grammar_refuses_it_too(self):
        # The TRAILER body spans newlines under DOTALL, so its blast radius was a
        # paragraph rather than a line.
        text = "Here are your choices.\n\n[OPTIONS: A | B then check arr[0]"
        assert OPTIONS_RE_TRAILER.search(text) is None
        assert OPTIONS_RE_TRAILER.sub("", text) == text

    def test_the_trailer_check_reaches_across_a_newline(self):
        # A pattern-level scan that stopped at ``\\n`` was blinded the moment the
        # prose wrapped. Counting depth over the captured labels has no such blind
        # spot, because the labels already span the newlines the body crossed.
        text = "[OPTIONS: A | B then check arr[0\nAnd that is all]"
        assert OPTIONS_RE_TRAILER.search(text) is None
        assert split_options_trailer(text) == (text, [])

    def test_a_marker_with_no_closer_at_all_is_still_refused(self):
        # Unchanged: with no closer anywhere the end anchor was never satisfiable.
        assert OPTIONS_RE_LINE.search("[OPTIONS: A | B then check arr") is None

    def test_the_separator_tail_form_is_refused_rather_than_truncated(self):
        # Documented in ``constants.py`` as unresolved, because ``], `` genuinely
        # continues the list and no guard at the INTERNAL closer can tell it apart.
        # Counting from the other end decides it: the ``[`` of ``CHANGELOG[1]`` is
        # still open at the end of the labels and no ``|`` follows it.
        text = "Done. [OPTIONS: Merge | Wait], details in CHANGELOG[1]"
        assert OPTIONS_RE_LINE.search(text) is None
        assert OPTIONS_RE_LINE.sub("", text) == text
        assert split_options_trailer(text) == (text, [])


class TestTheDecisionCannotLiveInThePattern:
    """Pinned because it is the reason for the indirection.

    If someone later folds this back into the regex, these are the assertions that
    say why it cannot work.
    """

    def test_the_refused_and_accepted_shapes_share_a_skeleton(self):
        assert skeleton("[OPTIONS: A | B then check arr[0]") == "w|w[w]"
        assert skeleton("[OPTIONS: Fix | Skip [x logging]") == "w|w[w]"

    def test_a_separator_after_the_opener_cannot_save_it_either(self):
        """Why the rule is TOTAL rather than carrying an escape hatch.

        The hatch was "an unmatched opener is label text if a ``|`` follows it", and
        it was defeated three times. The last one is the clearest: a ``|`` INSIDE the
        unmatched bracket satisfies it, so ``dict[str | int]`` at the end of a line
        readmitted the whole defect.

        And it could not be narrowed, because the shape it protected and the shape it
        readmitted differ only in where a ``|`` sits relative to an opener that has no
        closer -- which is to say, not structurally at all.
        """
        for text in (
            "[OPTIONS: Fix [x logging | Skip]",  # the hatch protected this
            "[OPTIONS: A | B then inspect dict[str | int]",  # ...and readmitted this
        ):
            assert OPTIONS_RE_LINE.search(text) is None, text
            assert OPTIONS_RE_LINE.sub("", text) == text, text

    def test_the_raw_pattern_still_over_matches_which_is_why_it_is_private(self):
        # The candidate pattern on its own accepts the shape; only the matcher
        # refuses it. Asserted so that "just use the regex" is visibly not an option.
        text = "[OPTIONS: A | B then check arr[0]"
        assert _RAW_OPTIONS_RE_LINE.search(text) is not None
        assert OPTIONS_RE_LINE.search(text) is None


class TestTheBalanceRuleDirectly:
    @pytest.mark.parametrize(
        ("labels", "nested"),
        [
            (" A | B then check arr[0", True),
            (" A | B then inspect list[dict[str, int]", True),
            (" A | B then inspect dict[str | int", True),  # a `|` inside the bracket
            (" Fix [x logging | Skip", True),  # a `|` after it changes nothing
            (" Fix arr[0] | Skip", False),  # balanced
            (" a[1] | b[2]", False),  # balanced
            (" Alpha ] | Bravo ]", False),  # unmatched CLOSERS say nothing
            (" Yes | No", False),  # no brackets
            (" A | B then check arr", False),  # no opener
            (" Merge | Wait], details in CHANGELOG[1", True),
            (" 见【表1】说明 | 跳过", False),  # a lookalike closer closes nothing here
        ],
    )
    def test_the_rule_in_isolation(self, labels: str, nested: bool):
        assert _marker_labels_have_unmatched_opener(labels) is nested


class TestWhatMustNotHaveChanged:
    @pytest.mark.parametrize(
        ("text", "labels"),
        [
            # A closer admitted by CONTINUATION, so its opener is closed by the time
            # the labels end.
            ("[OPTIONS: Fix arr[0] | Skip]", " Fix arr[0] | Skip"),
            ("[OPTIONS: a[1] | b[2]]", " a[1] | b[2]"),
            ("[OPTIONS: Fix list[dict[str, Any]] | Skip]", " Fix list[dict[str, Any]] | Skip"),
            # Matched pairs own their own closer.
            ("[OPTIONS: Fix [x] logging | Skip]", " Fix [x] logging | Skip"),
            ("[OPTIONS: Read arr[0] now | Skip it]", " Read arr[0] now | Skip it"),
            ("[OPTIONS: See [1] above | Skip]", " See [1] above | Skip"),
            ("[OPTIONS: Fix dict[str, Any] now | Skip]", " Fix dict[str, Any] now | Skip"),
            ("[OPTIONS: Refactor arr[i, j] now | Skip]", " Refactor arr[i, j] now | Skip"),
            # No opener at all, so nothing to be unbalanced.
            ("[OPTIONS: Alpha ] | Bravo ]]", " Alpha ] | Bravo ]"),
            ("[OPTIONS: Alpha ], Bravo]", " Alpha ], Bravo"),
            ("[OPTIONS: Yes | No]", " Yes | No"),
            ("**[OPTIONS: Yes | No]**", " Yes | No"),
            ("[OPTIONS: A | B](OPTIONS)", " A | B"),
            # Prose ending in a closer with no opener: that closer genuinely IS the
            # marker's, so this parses as it did before.
            ("[OPTIONS: A | B then check arr]", " A | B then check arr"),
        ],
    )
    def test_every_supported_shape_still_parses(self, text: str, labels: str):
        match = OPTIONS_RE_LINE.search(text)
        assert match is not None, text
        assert match.group("labels") == labels

    @pytest.mark.parametrize(
        "text",
        [
            "Use [OPTIONS: A | B] then check arr[0]",
            "[OPTIONS: Fix ]x logging | Skip]",
            "[OPTIONS: Fix list[dict[str, Any]] now | S]",
            "Note [OPTIONS: see [OPTIONS: x] below | Skip]",
        ],
    )
    def test_every_previously_refused_shape_still_is(self, text: str):
        assert OPTIONS_RE_LINE.search(text) is None, text

    def test_sub_removes_an_accepted_marker_and_leaves_a_refused_one(self):
        # ``sub`` is reimplemented on the matcher, so its two behaviours are pinned:
        # it removes what it accepts and leaves what it refuses.
        assert OPTIONS_RE_LINE.sub("", "Done.\n[OPTIONS: A | B]") == "Done.\n"
        refused = "Done.\n[OPTIONS: A | B then arr[0]"
        assert OPTIONS_RE_LINE.sub("", refused) == refused

    def test_finditer_yields_every_accepted_marker_in_order(self):
        text = "[OPTIONS: A | B]\n[OPTIONS: C | D]"
        assert [m.group("labels") for m in OPTIONS_RE_LINE.finditer(text)] == [" A | B", " C | D"]

    def test_a_refused_candidate_does_not_hide_a_later_accepted_one(self):
        # The body refuses a nested ``[OPTIONS:``, so a candidate never contains
        # another head -- which is what makes filtering candidates safe.
        text = "[OPTIONS: A then arr[0]\n[OPTIONS: C | D]"
        assert [m.group("labels") for m in OPTIONS_RE_LINE.finditer(text)] == [" C | D"]


class TestTheCost:
    """A stray opener in the FINAL label, where no ``|`` follows to make it text.

    It fails toward a VISIBLE marker with nothing removed -- the direction every cost
    in this grammar fails in -- and that is what makes it affordable.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "[OPTIONS: Fix | Skip [x logging]",
            "[OPTIONS: Fix [x logging]",
            # Only a comma follows, and a comma is not enough.
            "[OPTIONS: Fix [x logging, Skip]",
        ],
    )
    def test_it_is_given_up_without_deleting_anything(self, text: str):
        assert OPTIONS_RE_LINE.search(text) is None, text
        assert OPTIONS_RE_LINE.sub("", text) == text, text
        assert split_options_trailer(text) == (text, []), text


class TestCost:
    def test_the_check_is_linear_in_the_label_length(self):
        # One pass over the labels with a stack, so an adversarial run of openers is
        # linear rather than quadratic. Pinned by timing because the stack depth is
        # the only thing that grows.
        labels = "a[" * 200_000
        started = time.perf_counter()
        assert _marker_labels_have_unmatched_opener(labels) is True
        assert time.perf_counter() - started < 1.0

    @pytest.mark.parametrize("reps", [2_000, 10_000, 40_000])
    def test_matching_stays_linear(self, reps: int):
        # The adversarial shape: an unterminated marker made of bare openers, so the
        # pattern scans it all and the check walks it all.
        src = "[OPTIONS:" + ("a[b" * reps)
        started = time.perf_counter()
        assert OPTIONS_RE_LINE.search(src) is None
        assert OPTIONS_RE_TRAILER.search(src) is None
        assert time.perf_counter() - started < 2.0
