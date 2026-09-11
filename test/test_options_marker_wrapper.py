"""Tests for Markdown-wrapped ``[OPTIONS:]`` markers.

A model sometimes wraps the whole marker line in inline code or emphasis --
``` `[OPTIONS: A | B]` ``` or ``**[OPTIONS: A | B]**``. The wrapper character
lands AFTER the closing bracket, breaks the end-of-line anchor, and the marker
is neither parsed nor stripped: the turn loses its follow-up pills and the raw
marker leaks to the user as literal (code-styled) text. Same class of model tic
as the stray ``](OPTIONS)`` suffix the grammar already absorbs.

The widening is deliberately tight, and the negatives here are the real
regression risk (a widened grammar swallowing genuine prose):

* a LEADING wrapper is accepted only at line start (after optional indent), so
  emphasis belonging to preceding prose is never eaten;
* a TRAILING wrapper only when it abuts the closer (or its ``(...)`` tic);
* runs are capped at 3 (``***`` is the longest CommonMark emphasis run);
* the ReDoS-linearity guard stays green -- the wrapper class shares no
  character with the trailing whitespace classes.
"""

from __future__ import annotations

import time

import pytest

from kiro_crew.constants import (
    MARKER_WRAPPERS,
    OPTIONS_RE_LINE,
    OPTIONS_RE_TRAILER,
    split_trailing_protocol_suffix,
)
from kiro_crew.messaging.renderer import split_options_trailer

WRAPPED = [
    "`[OPTIONS: Alpha | Beta]`",  # backtick-wrapped (the reported shape)
    "**[OPTIONS: Alpha | Beta]**",  # emphasis-wrapped
    "_[OPTIONS: Alpha | Beta]_",  # underscore emphasis
    "**[OPTIONS: Alpha | Beta]",  # leading wrapper only (nothing to steal)
    "***[OPTIONS: Alpha | Beta]***",  # longest legal run
    "  `[OPTIONS: Alpha | Beta]`",  # indented, wrapper still at line start
    "`[OPTIONS: Alpha | Beta](OPTIONS)`",  # wrapper around the paren tic too
]

NOT_A_MARKER = [
    "[OPTIONS: Alpha | Beta].",  # trailing prose: a period
    "[OPTIONS: Alpha | Beta] pick one",  # trailing prose: words
    "[OPTIONS: Alpha | Beta] **",  # wrapper must ABUT the closer
    "[OPTIONS: Alpha | Beta]****",  # a 4+ run is not a wrapper
    "[OPTIONS: Alpha | Beta]` and more",  # wrapper then prose breaks the anchor
    # The trailing tolerance requires the marker to have OPENED a wrapper
    # (nonempty ``lwrap``): a run the marker did not open belongs to the
    # enclosing Markdown and must survive. These are the three reviewed
    # corruption shapes (GPT rounds 1/3/4, span f634241daeea).
    "`Use [OPTIONS: Alpha | Beta]`",  # inline code span quoting a marker
    "Pick **[OPTIONS: Alpha | Beta]**",  # mid-line emphasis-wrapped marker
    "x [OPTIONS: Alpha | Beta]**",  # mid-line marker, trailing wrapper
    "[OPTIONS: Alpha | Beta]**",  # trailing-only: nothing opened it, prose
]


class TestWrappedMarkersParse:
    @pytest.mark.parametrize("text", WRAPPED)
    def test_line_grammar_accepts_and_labels_are_clean(self, text):
        match = OPTIONS_RE_LINE.search(text)
        assert match is not None, text
        labels = [s.strip() for s in match.group("labels").split("|")]
        assert labels == ["Alpha", "Beta"], (text, labels)

    @pytest.mark.parametrize("text", WRAPPED)
    def test_trailer_grammar_agrees(self, text):
        match = OPTIONS_RE_TRAILER.search(text)
        assert match is not None, text
        labels = [s.strip() for s in match.group("labels").split("|")]
        assert labels == ["Alpha", "Beta"], (text, labels)

    def test_strip_removes_the_wrapper_with_the_marker(self):
        # The dashboard/Slack surfaces cut at match.start(); a line-leading
        # wrapper must go with the marker, not survive as stray punctuation.
        text = "All done.\n\n**[OPTIONS: Alpha | Beta]**"
        match = OPTIONS_RE_LINE.search(text)
        assert match is not None
        assert text[: match.start()] == "All done.\n\n"


class TestScopeStaysTight:
    @pytest.mark.parametrize("text", NOT_A_MARKER)
    def test_negative_rows_stay_negative(self, text):
        assert OPTIONS_RE_LINE.search(text) is None, text
        assert OPTIONS_RE_TRAILER.search(text) is None, text

    def test_emphasis_belonging_to_preceding_prose_is_not_eaten(self):
        # The leading wrapper is only legal at line start: here the ``**``
        # pair closes real emphasis, and the match must start at the bracket.
        text = "**Choose one:** [OPTIONS: Alpha | Beta]"
        match = OPTIONS_RE_LINE.search(text)
        assert match is not None
        assert match.start() == text.index("[OPTIONS")

    def test_mid_line_bare_marker_still_matches(self):
        # The mid-line branch is the pre-widening grammar, byte for byte: a
        # bare marker ending its line still parses wherever it sits.
        text = "Pick one [OPTIONS: Alpha | Beta]"
        match = OPTIONS_RE_LINE.search(text)
        assert match is not None
        assert match.start() == text.index("[OPTIONS")
        assert [s.strip() for s in match.group("labels").split("|")] == ["Alpha", "Beta"]

    def test_multiline_emphasis_closer_is_not_consumed(self):
        # Emphasis opened on a PRIOR line, closed abutting the marker's closer:
        # the trailing run was not opened by the marker, so the marker must not
        # match at all -- the emphasis pair survives.
        text = "**Choose one\n[OPTIONS: Alpha | Beta]**"
        assert OPTIONS_RE_LINE.search(text) is None
        assert OPTIONS_RE_TRAILER.search(text) is None

    def test_inline_code_span_keeps_its_closing_backtick(self):
        # The GPT server finding on e0709b843: with an unconditional trailing
        # tolerance, this stripped the marker AND the span's closing backtick,
        # corrupting the prose and emitting bogus buttons. It must not match.
        text = "`Use [OPTIONS: Alpha | Beta]`"
        assert OPTIONS_RE_LINE.search(text) is None
        assert OPTIONS_RE_TRAILER.search(text) is None

    def test_labels_group_contract(self):
        # The ``lwrap`` conditional group precedes ``labels``, so positional
        # ``group(1)`` is not the labels: every consumer reads
        # ``group("labels")`` and iterates with ``finditer``.
        for pattern in (OPTIONS_RE_LINE, OPTIONS_RE_TRAILER):
            match = pattern.search("`[OPTIONS: A | B]`")
            assert match is not None
            assert match.group("labels") == " A | B"
        hits = list(OPTIONS_RE_LINE.finditer("[OPTIONS: A]\n`[OPTIONS: B | C]`"))
        assert [m.group("labels") for m in hits] == [" A", " B | C"]


class TestStreamingSplitAgrees:
    def test_complete_wrapped_marker_is_detached_whole(self):
        visible, suffix = split_trailing_protocol_suffix("body\n**[OPTIONS: A | B]**")
        assert visible == "body\n"
        assert suffix == "**[OPTIONS: A | B]**"

    def test_unfinished_wrapped_marker_takes_its_wrapper_along(self):
        # Without the widening the leading wrapper stays in the visible half,
        # where a length rotation can split it from the marker it belongs to.
        visible, suffix = split_trailing_protocol_suffix("body\n**[OPTIONS: A | ")
        assert visible == "body\n"
        assert suffix == "**[OPTIONS: A | "

    def test_indent_before_the_wrapper_is_still_line_leading(self):
        visible, suffix = split_trailing_protocol_suffix("body\n  `[OPTIONS: A | ")
        assert visible == "body\n  "
        assert suffix == "`[OPTIONS: A | "

    def test_mid_line_wrapper_stays_in_the_visible_half(self):
        visible, suffix = split_trailing_protocol_suffix("pick **[OPTIONS: A")
        assert visible == "pick **"
        assert suffix == "[OPTIONS: A"

    def test_a_four_char_run_is_not_a_wrapper(self):
        visible, suffix = split_trailing_protocol_suffix("body\n****[OPTIONS: A")
        assert visible == "body\n****"
        assert suffix == "[OPTIONS: A"

    def test_hide_partial_cut_takes_a_line_leading_wrapper_along(self):
        # The streaming trim can cut a partial marker at its ``[``; it must not
        # publish the stray leading wrapper for the frame, so the cut applies
        # the same line-leading rule as the grammar.
        visible, choices = split_options_trailer("body\n**[OPTIONS: A", hide_partial=True)
        assert visible == "body"
        assert choices == []

    def test_hide_partial_keeps_a_mid_line_wrapper_as_prose(self):
        visible, choices = split_options_trailer("pick **[OPTIONS: A", hide_partial=True)
        assert visible == "pick **"
        assert choices == []

    def test_buffered_default_still_keeps_the_whole_tail(self):
        text = "see the **[OPTIONS section"
        visible, choices = split_options_trailer(text)
        assert visible == text
        assert choices == []


class TestNoRedosRegression:
    def test_wrapper_classes_share_no_character_with_whitespace(self):
        # The structural invariant the linearity below rests on: neither the
        # indent class nor the trailing run can also be read as a wrapper.
        assert not set(MARKER_WRAPPERS) & set(" \t\n\r\f\v")

    def test_adversarial_wrapper_runs_stay_linear(self):
        # CWE-1333: the new optional groups must not create a second parse of
        # long runs around a marker that never completes.
        evil = ("`" * 100_000) + "[OPTIONS:" + ("\t" * 100_000) + "x"
        start = time.perf_counter()
        assert OPTIONS_RE_LINE.search(evil) is None
        assert OPTIONS_RE_TRAILER.search(evil) is None
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"marker match too slow ({elapsed:.2f}s) — may backtrack"
