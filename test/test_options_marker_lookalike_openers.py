"""A lookalike bracket PAIR inside an ``[OPTIONS:]`` label parses.

A closer is readmitted to a label when an opener earlier in the same label
MATCHES it, so the rule needs an opener set to match against.
:data:`MARKER_CLOSERS` accepts three lookalikes alongside ASCII ``]``, and a model
writing a label in CJK punctuation writes both halves in it::

    [OPTIONS: 见【表1】说明 | 跳过]

Against ASCII ``[`` alone that ``】`` has no opener the rule recognises: the body
ends there, the line anchor rejects the remainder, and the whole marker reaches
the reader as literal text with no pills. :data:`_MARKER_OPENERS` supplies the
missing half, so each closer the set accepts has an opener that can match it.

THE PAIRING IS THE LOAD-BEARING PART. One branch accepting any opener followed by
any closer would make ``【表1]`` a pair, and that is the greedy body this grammar
exists to prevent, reached by a side door: the label would end at a closer whose
opener never appeared, so the body could run past the marker's real terminator and
delete the prose after it. Each opener therefore pairs ONLY with its positional
partner in :data:`MARKER_CLOSERS`, and the mismatched shapes are asserted here as
explicitly as the matched ones.

Two claims are kept separate, as in ``test_options_marker_label_closers.py``,
because only the second is what a user experiences: that the grammar does not
MATCH, and that removing the match leaves the text UNCHANGED.
"""

from __future__ import annotations

import re
import time

import pytest

from kiro_crew.constants import (
    _MARKER_BRACKETS,
    _MARKER_OPENERS,
    MARKER_CLOSERS,
    OPTIONS_RE_LINE,
    OPTIONS_RE_TRAILER,
)
from kiro_crew.messaging.renderer import split_options_trailer

#: Each opener with the closer it is supposed to pair with.
PAIRS = list(zip(_MARKER_OPENERS, MARKER_CLOSERS, strict=True))


class TestTheConstantsThemselves:
    def test_openers_and_closers_are_the_same_length_and_order(self):
        # The grammar zips them with ``strict=True``, so a length mismatch raises at
        # import rather than dropping the surplus branch. Asserted here as well so
        # the invariant is discoverable from the suite, not only from the zip call.
        assert len(_MARKER_OPENERS) == len(MARKER_CLOSERS)
        assert _MARKER_OPENERS[0] == "[" and MARKER_CLOSERS[0] == "]"

    def test_brackets_is_both_classes(self):
        # The negated class in the body is spelled over this, so a lookalike
        # cannot hide inside a pair interior or an ordinary-text run.
        assert set(_MARKER_BRACKETS) == set(_MARKER_OPENERS) | set(MARKER_CLOSERS)


class TestAMatchedLookalikePairParses:
    @pytest.mark.parametrize(("opener", "closer"), PAIRS)
    def test_every_pair_is_matched_inside_a_label(self, opener: str, closer: str):
        text = f"[OPTIONS: 见{opener}表1{closer}说明 | 跳过]"
        match = OPTIONS_RE_LINE.search(text)
        assert match is not None, text
        assert match.group("labels") == f" 见{opener}表1{closer}说明 | 跳过"

    def test_the_reported_shape_parses_end_to_end(self):
        # The issue's own example, through the consumer rather than the regex:
        # the option list survives AND the leading prose is kept.
        body, choices = split_options_trailer("请选择：\n[OPTIONS: 见【表1】说明 | 跳过]")
        assert body == "请选择："
        assert choices == ["见【表1】说明", "跳过"]

    def test_a_lookalike_pair_AND_a_lookalike_closer_together(self):
        # The marker's own terminator is a lookalike too, so the closer set and the
        # opener set have to compose rather than each work only on its own.
        match = OPTIONS_RE_LINE.search("[OPTIONS: 【重要】修复 | 跳过】")
        assert match is not None
        assert match.group("labels") == " 【重要】修复 | 跳过"

    def test_the_pair_also_works_under_the_trailer_grammar(self):
        match = OPTIONS_RE_TRAILER.search("请选择：\n\n[OPTIONS: 见〔表1〕说明 | 跳过]")
        assert match is not None
        assert match.group("labels") == " 见〔表1〕说明 | 跳过"

    def test_a_pair_may_hold_anything_that_is_not_a_bracket_or_a_separator(self):
        # Interior content is otherwise opaque: spaces, digits, CJK text and
        # punctuation that no splitter looks at all pass through as label text.
        # Asserted at the consumer, because the capture is not the claim.
        match = OPTIONS_RE_LINE.search("[OPTIONS: 见【表 1：注释】说明 | 跳过]")
        assert match is not None
        assert match.group("labels") == " 见【表 1：注释】说明 | 跳过"
        assert split_options_trailer("[OPTIONS: 见【表 1：注释】说明 | 跳过]") == (
            "",
            ["见【表 1：注释】说明", "跳过"],
        )


class TestAPairMayNotHoldASeparator:
    """A LOOKALIKE pair interior refuses ``|`` and ``,``; the ASCII one does not.

    Capturing a pair whole is only safe if what receives the capture can keep it
    whole, and neither splitter has any notion of nesting. The frontend's
    ``parseOptions`` picks its delimiter as ``labels.includes("|") ? "|" : ","``, so
    a comma is load-bearing exactly when no pipe is present -- which is why testing
    only the pipe shape, or only the backend, misses half of this.

    A lookalike pair carrying an internal separator -- an ordinary CJK phrase like
    ``【表1,表2】`` -- would be captured and then torn into three choices with
    unbalanced brackets, echoed back as the user's reply when one is tapped.

    The exclusion stops at the lookalike branches on purpose. Applying it to ASCII
    too declines ``[OPTIONS: Fix dict[str, Any] now | Skip]``, which parses today;
    the lookalike branches have no such history to protect. Both halves are pinned
    here, because the asymmetry is the design and not an oversight.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "[OPTIONS: 见【表1|表2】说明 | 跳过]",
            "[OPTIONS: 见［表1|表2］说明 | 跳过]",
            # The comma shapes, with NO pipe anywhere -- the case the frontend's
            # delimiter fallback makes reachable.
            "[OPTIONS: 见【表1,表2】说明, 跳过]",
            "[OPTIONS: 见〔表1,表2〕说明, 跳过]",
            # ...and with a pipe present, so the rule does not depend on which
            # delimiter the consumer happens to pick.
            "[OPTIONS: 见【表1,表2】说明 | 跳过]",
        ],
    )
    def test_a_lookalike_pair_holding_a_separator_is_declined(self, text: str):
        assert OPTIONS_RE_LINE.search(text) is None, text
        # The claim that matters is at the CONSUMER: no choices, text left intact.
        assert split_options_trailer(text) == (text, []), text

    @pytest.mark.parametrize(
        ("text", "corrupt"),
        [
            ("[OPTIONS: 见【表1|表2】说明 | 跳过]", ["见【表1", "表2】说明", "跳过"]),
            ("[OPTIONS: 见【表1,表2】说明, 跳过]", ["见【表1", "表2】说明", "跳过"]),
        ],
    )
    def test_the_corruption_this_prevents_is_named_explicitly(self, text: str, corrupt: list):
        # What a naive split produces, asserted as the thing that must NOT happen:
        # three fragments, two of them with unbalanced brackets.
        body, choices = split_options_trailer(text)
        assert choices != corrupt
        assert choices == []
        assert body == text

    @pytest.mark.parametrize(
        ("text", "labels"),
        [
            ("[OPTIONS: Fix dict[str, Any] now | Skip]", " Fix dict[str, Any] now | Skip"),
            ("[OPTIONS: Refactor arr[i, j] now | Skip]", " Refactor arr[i, j] now | Skip"),
            ("[OPTIONS: Refactor arr[i, j] | Skip]", " Refactor arr[i, j] | Skip"),
        ],
    )
    def test_the_ASCII_interior_still_admits_separators(self, text: str, labels: str):
        """The asymmetry, pinned as the thing that keeps it.

        Applying the exclusion to the ASCII branch too declines these -- labels a
        model writes constantly, which parse today. The lookalike branches carry no
        such history, so each half is chosen against what it would break.
        """
        match = OPTIONS_RE_LINE.search(text)
        assert match is not None, text
        assert match.group("labels") == labels

    def test_the_exclusion_is_a_MITIGATION_not_a_guarantee(self):
        # The continuation form admits a closer followed by a separator regardless of
        # what preceded it, so a bracket run holding ``|`` still reaches the splitter
        # by that route and still splits into three. Pinned so the interior rule is
        # not read as closing the whole class: removing it needs a nesting-aware
        # splitter. Unchanged from the behaviour before this grammar existed.
        text = "[OPTIONS: 见【表1|表2】 | 跳过]"
        match = OPTIONS_RE_LINE.search(text)
        assert match is not None
        assert split_options_trailer(text) == ("", ["见【表1", "表2】", "跳过"])


class TestAMismatchedPairIsNotAPair:
    """The half that keeps the body from running past its terminator.

    Each of these has an opener and a closer but not a PAIR, so the closer is
    unmatched -- and with ordinary words after it, the marker is declined rather
    than run on to a later closer.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "[OPTIONS: 见【表1] 说明 | 跳过]",  # CJK opener, ASCII closer
            "[OPTIONS: 见[表1】 说明 | 跳过]",  # ASCII opener, CJK closer
            "[OPTIONS: 见【表1〕 说明 | 跳过】",  # two different lookalikes
            "[OPTIONS: 见［表1〕 说明 | 跳过]",  # fullwidth opener, tortoise closer
        ],
    )
    def test_a_crossed_pair_is_declined(self, text: str):
        assert OPTIONS_RE_LINE.search(text) is None, text

    @pytest.mark.parametrize(
        "text",
        [
            "[OPTIONS: 见【表1] 说明 | 跳过]",
            "[OPTIONS: 见[表1】 说明 | 跳过]",
        ],
    )
    def test_declining_a_crossed_pair_does_not_delete_the_line(self, text: str):
        # The claim that makes the cost affordable, asserted at the consumer.
        assert OPTIONS_RE_LINE.sub("", text) == text
        assert split_options_trailer(text, hide_partial=True) == (text, [])

    def test_the_9284_shape_still_declines_with_lookalikes_in_play(self):
        # The defect this whole line of work exists for, spelled in CJK: a closer
        # that is neither matched nor continuing, with prose after it.
        text = "见 [OPTIONS: 保留 | 丢弃] 然后看 arr[0]"
        assert OPTIONS_RE_LINE.search(text) is None
        assert OPTIONS_RE_LINE.sub("", text) == text

    def test_a_pair_interior_may_not_swallow_another_bracket(self):
        # The interior excludes EVERY bracket, so nesting is still one level deep
        # and a lookalike cannot be hidden inside an interior to escape the rule.
        assert OPTIONS_RE_LINE.search("[OPTIONS: 见【表【1】】说明 | 跳过]") is None


class TestWhatMustNotHaveChanged:
    def test_a_stray_lookalike_opener_is_ordinary_label_text(self):
        # No partner closer, so no pair. The bare-opener branch consumes it and
        # the label list is unaffected -- the mirror of a stray ASCII ``[``.
        for opener in _MARKER_OPENERS[1:]:
            text = f"[OPTIONS: Fix {opener} now | Skip]"
            match = OPTIONS_RE_LINE.search(text)
            assert match is not None, text
            assert match.group("labels") == f" Fix {opener} now | Skip"

    @pytest.mark.parametrize(
        ("text", "labels"),
        [
            ("[OPTIONS: Read arr[0] now | Skip it]", " Read arr[0] now | Skip it"),
            ("[OPTIONS: See [1] above | Skip]", " See [1] above | Skip"),
            ("[OPTIONS: Alpha ] | Bravo ]]", " Alpha ] | Bravo ]"),
            ("[OPTIONS: Alpha ], Bravo]", " Alpha ], Bravo"),
            ("[OPTIONS: Yes | No]", " Yes | No"),
            ("**[OPTIONS: Yes | No]**", " Yes | No"),
        ],
    )
    def test_every_previously_supported_shape_still_parses(self, text: str, labels: str):
        match = OPTIONS_RE_LINE.search(text)
        assert match is not None, text
        assert match.group("labels") == labels

    def test_a_nested_head_is_still_refused(self):
        # Only ASCII ``[`` can begin a head, so only its branch carries the
        # ``(?!OPTIONS:)`` guard -- but that is the branch a nested head opens on,
        # so widening the opener set did not reopen this.
        assert OPTIONS_RE_LINE.search("Note [OPTIONS: see [OPTIONS: x] below | Skip]") is None

    def test_the_accepted_cost_table_is_exactly_these_four_rows(self):
        # Asserted so the table cannot quietly grow or shrink. Rows 1-2 predate the
        # opener set and need bracket matching to arbitrary depth, which a regex is
        # the wrong tool for. Rows 3-4 are interior exclusions this grammar takes
        # deliberately -- see TestAMixedBracketInterior and
        # TestAPairMayNotHoldTheSeparator for why each is the right trade.
        for text in (
            "[OPTIONS: Fix ]x logging | Skip]",  # unmatched, no opener at all
            "[OPTIONS: Fix list[dict[str, Any]] now | S]",  # deeper than one level
            "[OPTIONS: See [a【b] ref | Skip]",  # interior holds another bracket kind
            "[OPTIONS: 见【表1,表2】说明, 跳过]",  # interior holds a separator
        ):
            assert OPTIONS_RE_LINE.search(text) is None, text
            # Every row fails toward a VISIBLE marker; that is what makes it a cost
            # rather than a defect.
            assert OPTIONS_RE_LINE.sub("", text) == text, text


class TestAMixedBracketInterior:
    """The one shape this opener set NARROWS rather than widens.

    The pair interior excludes EVERY bracket, both classes. A pair whose interior
    holds a bracket of a different kind therefore has no pair parse, and with
    ordinary words after the closer the marker is declined -- where an interior that
    excluded only ASCII ``[`` and the closers would have admitted it.

    The exclusion is deliberate. Admitting ``【`` in the ASCII branch's interior
    would let that branch consume a character the ``【`` branch could also open on,
    so a span would have two parses -- and single-parse-per-span is exactly what the
    linearity argument reads off the pattern. A shape that fails toward a VISIBLE
    marker is the cheaper thing to give up than an ambiguity in the body.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "[OPTIONS: See [a【b] ref | Skip]",
            "[OPTIONS: See [a［b] ref | Skip]",
            "[OPTIONS: See [a〔b] ref | Skip]",
            "[OPTIONS: 见【表[1】说明 | 跳过]",
        ],
    )
    def test_a_mixed_bracket_interior_is_declined_and_nothing_is_deleted(self, text: str):
        assert OPTIONS_RE_LINE.search(text) is None, text
        assert OPTIONS_RE_LINE.sub("", text) == text, text

    def test_the_same_interior_still_parses_when_the_list_CONTINUES(self):
        # The cost is the tail, not the interior: with a separator after the closer
        # the continuation half admits it and no pair parse is needed.
        match = OPTIONS_RE_LINE.search("[OPTIONS: See [a【b] | Skip]")
        assert match is not None
        assert match.group("labels") == " See [a【b] | Skip"


class TestLinearity:
    """The disjointness argument, exercised rather than asserted in a comment.

    Four pair branches now begin at four different openers, so the claim "at most
    one alternative can consume a given character" is stronger than it was with
    one. What could break it is the catch-all: if it still admitted ``【``, that
    character would have two parses.
    """

    def test_the_catch_all_admits_no_bracket(self):
        # Read off the compiled pattern, so it cannot drift from the comment.
        assert f"[^{re.escape(_MARKER_BRACKETS)}\\n]" in OPTIONS_RE_LINE.pattern

    @pytest.mark.parametrize("reps", [500, 2_000, 8_000])
    def test_lookalike_heavy_adversarial_input_stays_linear(self, reps: int):
        # An unterminated marker whose body is nothing but lookalike pairs: the
        # shape that would blow up if the pair and catch-all branches overlapped.
        src = "[OPTIONS:" + ("【a】 | " * reps)
        started = time.perf_counter()
        assert OPTIONS_RE_LINE.search(src) is None
        assert OPTIONS_RE_TRAILER.search(src) is None
        assert time.perf_counter() - started < 2.0

    def test_a_long_mismatched_run_also_stays_linear(self):
        # Mismatched brackets are the worst case for a pair form: every branch is
        # attempted and every one fails.
        src = "[OPTIONS:" + ("【a〕 | " * 4_000)
        started = time.perf_counter()
        assert OPTIONS_RE_LINE.search(src) is None
        assert time.perf_counter() - started < 2.0
