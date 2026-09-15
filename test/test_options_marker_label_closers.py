"""A closer stays in an ``[OPTIONS:]`` label only where it is
MATCHED by an earlier ``[``, or where it CONTINUES the label list.

A label may legitimately carry a closer -- ``[OPTIONS: Alpha ] | Bravo ]]`` is a
supported, tested shape -- so the body has to admit one. Admitting it
UNCONDITIONALLY (the old ``[^[\\n]``, which includes ``]``) made the body run to
the LAST closer in range instead of the first plausible one, so an ordinary final
line that mentions a bracket after the marker matched across BOTH::

    Use [OPTIONS: A | B] then check arr[0]

matched whole. Every consumer removes the whole match -- ``slack.format`` and
``messaging.renderer`` cut the visible text at ``match.start()``, and
``whatsapp.turn_renderer`` PERSISTS the cut turn -- so the sentence vanished from
the message and came back as a pill label. Under TRAILER (``DOTALL``) the body
crossed blank lines too, so the whole final paragraph went with it.

NEITHER condition alone is enough, which is why the rule is a union of two:

===================================== ================== ==========
input                                 admitted by        outcome
===================================== ================== ==========
``[OPTIONS: Fix [x] logging | Skip]``  matched pair       parses
``[OPTIONS: Alpha ] | Bravo ]]``       list continues     parses
``Use [OPTIONS: A | B] ... arr[0]``    neither            declined
===================================== ================== ==========

The first row is pinned independently by ``test_parse_options.py`` and
``test_options_buttons.py``, whose comments call it out as supported: continuation
alone would have broken it, so this suite asserts it here too, as the guard it is.

Two claims are asserted separately throughout, because they are different and only
the second is what the user experiences: that the grammar does not MATCH, and that
``sub`` leaves the text UNCHANGED. ``TestAcceptedCosts`` extends the second claim
one step further down, to ``split_options_trailer``, because narrowing the grammar
is what made that function's partial-cut gate load-bearing: a marker the grammar
declines must not be silently deleted by the consumer instead.

Under an unconditional closer, every case in
``OVERREACH`` and ``TRAILER_OVERREACH`` matches whole and deletes the prose shown.
"""

from __future__ import annotations

import time

from kiro_crew.constants import (
    MARKER_CLOSERS,
    OPTIONS_RE_LINE,
    OPTIONS_RE_TRAILER,
    _marker_labels_have_unmatched_opener,
)
from kiro_crew.messaging.renderer import split_options_trailer

#: Same-line shapes that must NOT match: the swallowed closer has no ``[`` to
#: match it AND is followed by an ordinary word rather than a separator, so
#: neither half of the rule admits it.
OVERREACH = [
    "Use [OPTIONS: A | B] then check arr[0]",
    "Pick [OPTIONS: A | B] and the type is dict[str, Any]",
    "All set [OPTIONS: Ship | Hold] before you diff src/app[0]",
    "Ready [OPTIONS: Yes | No] see the note in docs[2]",
    # The wrapped forms of the same shape, on the leading-wrapper (``lwrap``) path.
    "`[OPTIONS: A | B] then check arr[0]`",
    "**[OPTIONS: Merge | Wait] then read CHANGELOG[1]**",
]

#: End-of-buffer shapes for the DOTALL grammar, where an unconditional closer lets the body cross blank
#: lines and take the whole closing paragraph.
TRAILER_OVERREACH = [
    "[OPTIONS: A | B]\n\nAnd then a whole closing paragraph about arr[0]",
    "Here are your choices.\n\n[OPTIONS: A | B]\n\nRemember to read docs[1]",
]


class TestACloserMustBeMatchedOrContinueTheList:
    def test_a_closer_followed_by_words_ends_the_block(self):
        for text in OVERREACH:
            assert OPTIONS_RE_LINE.search(text) is None, text

    def test_and_therefore_no_prose_is_deleted(self):
        # The claim that actually matters. "Does not match" and "the message is
        # intact" are different assertions; every consumer removes the whole match,
        # so only this one describes what the user sees.
        for text in OVERREACH:
            assert OPTIONS_RE_LINE.sub("", text) == text, text

    def test_the_trailer_grammar_no_longer_eats_the_final_paragraph(self):
        for text in TRAILER_OVERREACH:
            assert OPTIONS_RE_TRAILER.search(text) is None, text
            assert OPTIONS_RE_TRAILER.sub("", text) == text, text

    def test_the_rule_stated_positively(self):
        # An UNMATCHED closer stays inside the label when a separator or another
        # closer follows it -- that is the second half of the rule on its own,
        # with no ``[`` anywhere to satisfy the first.
        match = OPTIONS_RE_LINE.search("[OPTIONS: Alpha ] | Bravo ]]")
        assert match is not None
        assert [s.strip() for s in match.group("labels").split("|")] == ["Alpha ]", "Bravo ]"]

    def test_a_comma_continues_the_list_just_as_well_as_a_pipe(self):
        match = OPTIONS_RE_LINE.search("[OPTIONS: Alpha ], Bravo]")
        assert match is not None
        assert match.group("labels") == " Alpha ], Bravo"

    def test_every_lookalike_closer_continues_the_list_too(self):
        # The rule is spelled over MARKER_CLOSERS, not over ASCII ``]``, so a
        # model that substitutes one codepoint mid-label is treated identically.
        for close in MARKER_CLOSERS:
            text = f"[OPTIONS: Alpha {close} | Bravo]"
            match = OPTIONS_RE_LINE.search(text)
            assert match is not None, text
            assert match.group("labels") == f" Alpha {close} | Bravo", text

    def test_a_bracket_that_ends_a_label_is_unaffected(self):
        # Admitted by the CONTINUATION half only. The pair half is excluded here by
        # its own trailing lookahead, precisely because a ``|`` follows -- which is
        # what keeps the two disjoint, and is why neither alternative is redundant:
        # delete the continuation half and this pinned shape regresses.
        match = OPTIONS_RE_LINE.search("[OPTIONS: Fix arr[0] | Skip]")
        assert match is not None
        assert [s.strip() for s in match.group("labels").split("|")] == ["Fix arr[0]", "Skip"]

    def test_a_matched_pair_mid_label_with_words_after_it_parses(self):
        # The rows continuation ALONE would have broken, and the reason the rule
        # is a union: ``test_parse_options.py`` and ``test_options_buttons.py`` pin
        # ``Fix [x] logging`` as a supported shape, and there the closer is
        # followed by an ordinary word rather than a separator. It parses because
        # the ``[`` before it matches.
        for text in (
            "[OPTIONS: Fix [x] logging | Skip]",
            "[OPTIONS: Fix arr[0] now | Skip]",
            "Pick one.\n[OPTIONS: Fix [x] logging | Skip]",
        ):
            match = OPTIONS_RE_LINE.search(text)
            assert match is not None, text
            assert match.group("labels").startswith(" Fix "), text

    def test_an_unmatched_opener_in_a_label_is_refused(self):
        """The one shape the balanced-labels rule costs.

        A stray ``[`` with no closer of its own cannot be treated as ordinary label
        text, because that shape is indistinguishable from a marker the model never
        closed: in ``[OPTIONS: A | B then check arr[0]`` the only closer belongs to
        ``arr[0]``, and the body would run through the prose to reach it. Both hold
        one unmatched opener and a closer at the end anchor, so accepting either
        accepts both -- and accepting the second deletes a line of prose.

        So the marker now renders as visible text. Nothing is removed, which is the
        direction every cost in this grammar fails in, and the full argument lives at
        :func:`kiro_crew.constants._marker_labels_have_unmatched_opener`.
        """
        text = "[OPTIONS: Fix [x logging | Skip]"
        assert OPTIONS_RE_LINE.search(text) is None
        assert OPTIONS_RE_LINE.sub("", text) == text


class TestAcceptedCosts:
    """Every shape the union rule gives up, enumerated rather than summarised.

    They share one form -- a closer that satisfies NEITHER half, with ordinary
    words after it -- but there is more than one way to be that closer, and all of
    them parsed on the old body. Each fails toward a VISIBLE marker, not toward
    deleted prose, and ``test_a_declined_marker_never_loses_the_option_list``
    below is what holds that asymmetry up.
    """

    def test_an_unmatched_closer_with_words_after_it_is_the_cost(self):
        # The base shape: no ``[`` to match it, no separator after it. There the
        # input is genuinely indistinguishable from "marker ended, prose followed
        # on the same line", which the grammar declines by design.
        assert OPTIONS_RE_LINE.search("[OPTIONS: Fix ]x logging | Skip]") is None

    def test_nesting_deeper_than_one_level_is_also_a_cost(self):
        # The pair form is ONE level deep, so a closer whose nearest preceding
        # ``[`` is separated from it by another bracket has no pair parse either.
        # Depth-general matching is not something a regex can do; the boundary is
        # named here rather than left for a reader to discover.
        for text in (
            "[OPTIONS: Fix list[dict[str, Any]] now | Skip]",
            "[OPTIONS: Update arr[i[0]] then rerun | Skip]",
        ):
            assert OPTIONS_RE_LINE.search(text) is None, text
        # ...and the same nesting with a SEPARATOR after it still parses, because
        # then the continuation half admits it. The cost is the tail, not the depth.
        match = OPTIONS_RE_LINE.search("[OPTIONS: Fix list[dict[str, Any]] | Skip]")
        assert match is not None
        assert match.group("labels") == " Fix list[dict[str, Any]] | Skip"

    def test_a_matched_lookalike_pair_parses_like_ascii(self):
        # ``MARKER_OPENERS`` pairs each lookalike opener with its closer, so a
        # matched lookalike pair inside a label parses exactly as ``[x]`` does.
        # Common in Chinese output, hence stated explicitly for each pair.
        for open_c, close_c in (("【", "】"), ("［", "］"), ("〔", "〕")):
            text = f"[OPTIONS: Fix {open_c}x{close_c} logging | Skip]"
            match = OPTIONS_RE_LINE.search(text)
            assert match is not None, text
            assert [s.strip() for s in match.group("labels").split("|")] == [
                f"Fix {open_c}x{close_c} logging",
                "Skip",
            ], text
        # The label may be entirely a lookalike pair, too.
        match = OPTIONS_RE_LINE.search("[OPTIONS: 【重要】修复 | 跳过】")
        assert match is not None
        assert match.group("labels") == " 【重要】修复 | 跳过"

    def test_a_mismatched_lookalike_pair_is_a_cost(self):
        # A lookalike opener closes ONLY on its paired closer. ``【`` pairs with
        # ``】``, never with ``]``, so ``【 ... ]`` has no pair parse and the ``]``
        # reads as unmatched -- the marker declines rather than deleting prose.
        for text in (
            "[OPTIONS: 见【表1] 说明 | 跳过]",
            "[OPTIONS: Fix [x】 logging | Skip]",
            "[OPTIONS: Fix ［x〕 logging | Skip]",
        ):
            assert OPTIONS_RE_LINE.search(text) is None, text
            assert OPTIONS_RE_LINE.sub("", text) == text, text

    def test_a_pair_spanning_a_newline_is_a_trailer_only_cost(self):
        # The pair interior excludes ``\n`` on BOTH grammars, so under TRAILER
        # (``DOTALL``) a matched pair that spans a line break inside a label has no
        # pair parse even though the body may otherwise cross newlines.
        assert OPTIONS_RE_TRAILER.search("Pick:\n[OPTIONS: Fix [multi\nline] now | Skip]") is None

    def test_a_declined_marker_never_loses_the_option_list(self):
        """The cost is only affordable because declining is NON-DESTRUCTIVE.

        A declined marker falls through to
        ``messaging.renderer.split_options_trailer``, whose ``hide_partial`` gate
        cuts a tail that could still be a marker mid-flight. That gate tested for
        ASCII ``]`` alone, so a marker whose only closers are lookalikes read as
        in-flight and was CUT -- and on WeCom the sealed frame and its persisted
        history entry, and on Discord/Telegram the ``self._buf = [body]`` reseat,
        would then show the leading prose with the whole option list deleted and no
        pills to recover it from. Narrowing the grammar is what made that gate
        load-bearing, so the two are pinned together.
        """
        for text in (
            "请选择：\n[OPTIONS: 见【表1] 说明 | 跳过]",
            "Pick one:\n[OPTIONS: Fix ]x logging | Skip]",
            "Pick one:\n[OPTIONS: Fix list[dict[str, Any]] now | Skip]",
        ):
            assert OPTIONS_RE_TRAILER.search(text) is None, text
            assert split_options_trailer(text, hide_partial=True) == (text, []), text

    def test_a_genuinely_unfinished_lookalike_tail_is_still_hidden(self):
        # The other direction, so the widening above is not mistaken for "never
        # cut": a tail holding NO closer at all really may be a marker in flight,
        # and the streaming surfaces still hide it.
        body, choices = split_options_trailer(
            "Working on it. [OPTIONS: 修复 | 跳", hide_partial=True
        )
        assert body == "Working on it."
        assert choices == []

    def test_a_nested_head_is_not_swallowed_into_a_label(self):
        # The one place this rule could have been LOOSER than the body it replaced:
        # without ``(?!OPTIONS:)`` on the pair form's opener, the pair alternative
        # opens on a nested head and pairs it with that head's own closer, so the
        # OUTER head matches and the pill's label is a raw protocol marker --
        # echoed back as the user's reply when tapped. The old body matched nothing
        # here, and neither does this one.
        assert OPTIONS_RE_LINE.search("Note [OPTIONS: see [OPTIONS: x] below | Skip]") is None

    def test_the_separator_tail_form_is_declined_rather_than_truncated(self):
        # Not reachable by the matched-or-continues rule: ``], `` DOES continue the
        # label list, by the very rule that makes ``[OPTIONS: Alpha ], Bravo]``
        # legal, so no guard applied at the INTERNAL closer can tell the two apart.
        # The terminator gate reaches it from the other end -- the ``[`` of
        # ``CHANGELOG[1]`` is the opener whose partner would end the marker, so the
        # bare form is refused and the line stays whole.
        #
        # Declining is the affordable outcome: truncating deleted ``, details in
        # CHANGELOG[1]`` from the message and handed it back as the pill label
        # ``Wait], details in CHANGELOG[1``.
        text = "Done. [OPTIONS: Merge | Wait], details in CHANGELOG[1]"
        assert OPTIONS_RE_LINE.search(text) is None
        assert OPTIONS_RE_LINE.sub("", text) == text
        assert split_options_trailer(text) == (text, [])


class TestLookalikeBracketPairsParse:
    """A lookalike bracket PAIR parses like an ASCII ``[...]`` pair.

    :data:`MARKER_OPENERS` pairs each opener with its closer positionally, so a
    matched ``【...】`` / ``［...］`` / ``〔...〕`` inside a label renders buttons
    exactly as ``[...]`` does -- WITHOUT reopening the greedy-body / prose-deletion
    defect the disjointness invariant prevents: a MISMATCHED pair still declines,
    and the trailing-prose and bare-opener-terminator declines are preserved.
    """

    def test_each_cjk_pair_parses_on_both_grammars(self):
        for open_c, close_c in (("【", "】"), ("［", "］"), ("〔", "〕")):
            line = f"Pick one.\n[OPTIONS: Fix {open_c}x{close_c} now | Skip]"
            trailer = f"Pick one.\n\n[OPTIONS: Use {open_c}note{close_c} | Skip]"
            m = OPTIONS_RE_LINE.search(line)
            assert m is not None, line
            assert m.group("labels") == f" Fix {open_c}x{close_c} now | Skip", line
            mt = OPTIONS_RE_TRAILER.search(trailer)
            assert mt is not None, trailer
            assert mt.group("labels") == f" Use {open_c}note{close_c} | Skip", trailer

    def test_a_mismatched_pair_declines_and_deletes_no_prose(self):
        # ``【`` never pairs with ``]``; the mismatched marker declines non-destructively.
        text = "[OPTIONS: 见【表1] 说明 | 跳过]"
        assert OPTIONS_RE_LINE.search(text) is None
        assert OPTIONS_RE_LINE.sub("", text) == text

    def test_a_mismatched_pair_declines_even_when_the_closer_ends_the_label(self):
        # The balance check pairs openers by TYPE. On the previous head every
        # closer decremented one shared count, so ``【x]`` read as closed, this
        # candidate parsed, and a label with a half-open lookalike pair rendered
        # as options. The ``【`` (or ``[``) is still open at the terminator here --
        # the bare-opener shape -- so the candidate declines and no prose moves.
        for text in (
            "[OPTIONS: A 【x] | B]",
            "[OPTIONS: A [x】 | B]",
            "[OPTIONS: A ［x〕 | B]",
            "[OPTIONS: A 【x] | B】",
        ):
            assert not _marker_labels_have_unmatched_opener(" A [x] | B")
            assert _marker_labels_have_unmatched_opener(text[len("[OPTIONS:") : -1]), text
            for matcher in (OPTIONS_RE_LINE, OPTIONS_RE_TRAILER):
                assert matcher.search(text) is None, text
                assert matcher.sub("", text) == text, text
        # Typed pairing still accepts nesting across kinds and the unmatched closer.
        for text in ("[OPTIONS: A [x【y】] | B]", "[OPTIONS: Alpha ] | Bravo ]]"):
            assert OPTIONS_RE_LINE.search(text) is not None, text

    def test_a_citation_in_a_label_still_parses(self):
        # A single-closer citation is admitted by the continuation half, unaffected
        # by the new opener set.
        m = OPTIONS_RE_LINE.search("[OPTIONS: see ref[1] | Skip]")
        assert m is not None
        assert [s.strip() for s in m.group("labels").split("|")] == ["see ref[1]", "Skip"]

    def test_trailing_prose_after_a_marker_still_declines(self):
        assert OPTIONS_RE_LINE.search("Use [OPTIONS: A | B] then check arr[0]") is None
        assert OPTIONS_RE_LINE.sub("", "Use [OPTIONS: A | B] then check arr[0]") == (
            "Use [OPTIONS: A | B] then check arr[0]"
        )

    def test_bare_opener_terminator_still_declines(self):
        # The opener whose partner closer would end the marker is refused, so the
        # line stays whole rather than losing its tail to a pill label.
        text = "[OPTIONS: A | B then check arr[0]"
        assert OPTIONS_RE_LINE.search(text) is None
        assert OPTIONS_RE_LINE.sub("", text) == text

    def test_a_bare_lookalike_opener_before_the_terminator_declines_too(self):
        # The same shape through a lookalike: a matched ``【重要】`` pair earlier in
        # the label is fine, but the bare ``【`` before the final ``】`` is the opener
        # whose partner would end the marker. The balance gate counts every opener
        # the grammar knows, so this declines exactly as the ASCII spelling does
        # and the trailing prose is not cut into a pill.
        for opener, closer in zip("\u3010\uff3b\u3014", "\u3011\uff3d\u3015"):
            text = f"[OPTIONS: {opener}重要{closer}修复 | B 详见{opener}0{closer}"
            assert OPTIONS_RE_LINE.search(text) is None, text
            assert OPTIONS_RE_LINE.sub("", text) == text

    def test_a_nested_head_is_still_refused(self):
        assert OPTIONS_RE_LINE.search("Note [OPTIONS: see [OPTIONS: x] below | Skip]") is None


class TestTheWideningIsNotOverlyNarrow:
    """Everything the wrapper tolerance and the closer widening admit must still parse."""

    def test_the_plain_marker_still_parses_on_both_grammars(self):
        for text in ("Done.\n\n[OPTIONS: Merge | Wait]", "Done. [OPTIONS: Merge | Wait]"):
            match = OPTIONS_RE_LINE.search(text)
            assert match is not None, text
            assert match.group("labels") == " Merge | Wait", text
        trailer = OPTIONS_RE_TRAILER.search("Done.\n\n[OPTIONS: Merge | Wait]")
        assert trailer is not None
        assert trailer.group("labels") == " Merge | Wait"

    def test_every_wrapper_still_parses(self):
        for wrap in ("`", "``", "```", "*", "**", "_", "__"):
            text = f"Done.\n\n{wrap}[OPTIONS: Merge | Wait]{wrap}"
            match = OPTIONS_RE_LINE.search(text)
            assert match is not None, text
            assert match.group("labels") == " Merge | Wait", text
            assert match.group("lwrap") == wrap, text

    def test_a_wrapped_marker_with_a_continuing_closer_still_parses(self):
        # The two rules compose: wrapper tolerance and the mid-label closer are independent,
        # and a marker carrying both is still a marker.
        match = OPTIONS_RE_LINE.search("`[OPTIONS: Alpha ] | Bravo]`")
        assert match is not None
        assert match.group("lwrap") == "`"
        assert [s.strip() for s in match.group("labels").split("|")] == ["Alpha ]", "Bravo"]

    def test_prose_emphasis_before_the_marker_is_still_never_eaten(self):
        text = "**Choose:** [OPTIONS: Merge | Wait]"
        match = OPTIONS_RE_LINE.search(text)
        assert match is not None
        assert match.group("lwrap") is None
        assert OPTIONS_RE_LINE.sub("", text) == "**Choose:** "

    def test_a_marker_spanning_newlines_still_closes_the_trailer(self):
        # TRAILER's body omits ``\n`` from its negated class on purpose (DOTALL),
        # so the temper must not have re-introduced a line boundary.
        match = OPTIONS_RE_TRAILER.search("Done.\n\n[OPTIONS: Alpha |\nBravo]")
        assert match is not None
        assert match.group("labels") == " Alpha |\nBravo"

    def test_the_markdown_link_close_tic_still_composes(self):
        match = OPTIONS_RE_LINE.search("[OPTIONS: A | B](OPTIONS)")
        assert match is not None
        assert match.group("labels") == " A | B"

    def test_a_marker_with_same_line_prose_after_it_still_fails_the_anchor(self):
        # Unchanged, and the reason the cost above is bounded: this was already
        # declined before the temper.
        assert OPTIONS_RE_LINE.search("[OPTIONS: A | B] see the note") is None


class TestLinearity:
    """The temper adds a lookahead inside a quantified body. These drive it
    directly -- a quadratic implementation wedges rather than failing an
    assertion, so the bound is generous and the shapes are the adversarial ones."""

    def test_a_long_trailing_whitespace_run_is_linear(self):
        # The marker's OWN closer followed by a 100k-tab run: a legitimate match
        # (the tabs are the trailing ``[ \t]*``), and the scan is single-pass.
        text = "[OPTIONS: A ]" + "\t" * 100_000
        start = time.perf_counter()
        match = OPTIONS_RE_LINE.search(text)
        assert match is not None
        assert match.group("labels") == " A "
        assert time.perf_counter() - start < 5.0

    def test_a_long_FAILING_continuation_scan_is_linear(self):
        # The adversarial direction: the closer is INSIDE the body, so it enters the
        # lookahead, whose whitespace scan runs to the end of a 100k-tab run and then
        # FAILS on ``x``. The body cannot cross the closer either, so the whole match
        # fails -- after the longest scan the lookahead can be made to do.
        text = "[OPTIONS: A ]" + "\t" * 100_000 + "x]"
        start = time.perf_counter()
        assert OPTIONS_RE_LINE.search(text) is None
        assert time.perf_counter() - start < 5.0

    def test_many_failing_closers_are_linear(self):
        # 5,000 closers, each entering the lookahead and each failing it.
        text = "[OPTIONS: " + "] x " * 5_000
        start = time.perf_counter()
        OPTIONS_RE_LINE.search(text)
        assert time.perf_counter() - start < 5.0

    def test_many_continuing_closers_then_a_long_tail_are_linear(self):
        # The other direction: 30,000 closers that all SUCCEED in the lookahead,
        # followed by a 30,000-tab tail that fails the end anchor.
        text = "[OPTIONS: " + "] | " * 30_000 + "\t" * 30_000
        start = time.perf_counter()
        OPTIONS_RE_LINE.search(text)
        assert time.perf_counter() - start < 5.0

    def test_a_run_of_any_opener_costs_the_same_as_a_run_of_ascii_openers(self):
        # A repeated-token degeneration after a head: 20,000 copies of one opener
        # with no partner. Each opener begins a pair attempt whose interior excludes
        # EVERY bracket, so the attempt fails on the very next character and the
        # bare-opener alternative takes it -- one step per character for every
        # opener kind. An interior that admitted the lookalikes would scan the
        # whole remaining run at each position instead, and the ratio below would
        # be in the hundreds rather than near one.
        def cost(opener: str) -> float:
            text = "[OPTIONS: A | B " + opener * 20_000
            start = time.perf_counter()
            OPTIONS_RE_LINE.search(text)
            OPTIONS_RE_TRAILER.search(text)
            return time.perf_counter() - start

        # `perf_counter`, and a baseline taken as the BEST of several runs, because
        # this is the one assertion here that divides two measurements. On Windows
        # `time.monotonic()` is GetTickCount64 with ~15.6ms granularity, so the
        # ASCII baseline reads 0.0, the floor below becomes the whole budget, and
        # the bound collapses to 8ms -- which ordinary scheduling noise exceeds on
        # a loaded shard while the linearity property is perfectly intact
        # (MEASURED: this failed a Windows CI shard that had just run 12,525 tests).
        # `perf_counter` is sub-microsecond on every platform, and the best-of
        # keeps one descheduled sample from shrinking the denominator.
        #
        # The bound stays a measured ratio rather than becoming a step count: a
        # compiled `re` pattern exposes no invocation surface to count, which is
        # the "no observable structure" case testing-conventions reserves a ratio
        # for. It is widened to 40x on that section's own reasoning -- a real
        # regression here is the interior admitting the lookalikes, which makes
        # this quadratic and the ratio "in the hundreds" as the comment above
        # says, so a wide bound still catches it while runner noise cannot.
        ascii_cost = max(min(cost("[") for _ in range(3)), 1e-6)
        for opener in ("\u3010", "\uff3b", "\u3014"):
            assert cost(opener) < 40 * ascii_cost, opener

    def test_the_two_bracket_alternatives_cannot_blow_up_together(self):
        # THE shape that would be exponential if the matched-pair and
        # continuation alternatives could consume the same span: N blocks that
        # each look like both, followed by a tail that fails the whole match, so
        # the engine is forced to exhaust every combination it believes exists.
        # They are disjoint by what follows the closer, so there is only one.
        for block in ("[x] ", "[x] | ", "[a[b] ", "[a] ]a ", "[x", "[] "):
            text = "[OPTIONS: " + block * 20_000 + "z"
            start = time.perf_counter()
            OPTIONS_RE_LINE.search(text)
            OPTIONS_RE_TRAILER.search(text)
            assert time.perf_counter() - start < 5.0, block
