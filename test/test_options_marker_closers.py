"""Tests for lookalike closing brackets on the ``[OPTIONS:]`` follow-up marker.

The prompt only ever specifies an ASCII ``]``, but a model intermittently
substitutes a fullwidth / CJK lookalike. Observed in the wild: one session
emitted ``[OPTIONS: … | …】`` (U+3011) three times, and because a single wrong
codepoint broke the end anchor, the whole marker leaked into the visible message
as literal text and those turns silently lost their follow-up pills. Worse, the
failure self-reinforces — the bad character lands in the session transcript, and
the model then copies its own prior formatting on later turns.

``MARKER_CLOSERS`` is shared by both regexes. ``split_trailing_protocol_suffix``'s
unfinished-marker check stays ASCII-only ON PURPOSE -- a closer inside a
still-streaming label must not read as complete (see the comment there, and
``test_closer_inside_an_unfinished_label_is_still_unfinished`` below).
``OPTIONS_RE_TRAILER`` then reclassifies a complete lookalike-closed tail, so the
two agree on the final split WITHOUT sharing the closer set. Do not "fix" that
asymmetry by widening the check: revision 7115a04a did exactly that and the GPT
gate blocked it.

These tests lock that agreement in, and lock in that widening the closer did NOT
widen anything else.
"""

from __future__ import annotations

import time

from kiro_crew.constants import (
    MARKER_CLOSERS,
    OPTIONS_RE_LINE,
    OPTIONS_RE_TRAILER,
    split_trailing_protocol_suffix,
)


class TestLookalikeClosersAccepted:
    def test_every_closer_parses_identical_labels(self):
        for close in MARKER_CLOSERS:
            text = f"body prose\n\n[OPTIONS: Alpha | Beta{close}"
            match = OPTIONS_RE_LINE.search(text)
            assert match is not None, f"U+{ord(close):04X} not accepted by LINE"
            labels = [s.strip() for s in match.group(1).split("|")]
            assert labels == ["Alpha", "Beta"], f"U+{ord(close):04X} -> {labels}"

    def test_trailer_grammar_agrees_with_line_grammar(self):
        for close in MARKER_CLOSERS:
            text = f"body prose\n\n[OPTIONS: Alpha | Beta{close}"
            assert OPTIONS_RE_TRAILER.search(text) is not None, f"U+{ord(close):04X}"

    def test_ascii_closer_is_still_first_class(self):
        assert MARKER_CLOSERS[0] == "]"
        match = OPTIONS_RE_LINE.search("[OPTIONS: A | B]")
        assert match is not None
        assert [s.strip() for s in match.group(1).split("|")] == ["A", "B"]


class TestClosersNotOverlyBroad:
    def test_unrelated_cjk_punctuation_does_not_close(self):
        # U+300D RIGHT CORNER BRACKET and U+FF09 FULLWIDTH RIGHT PAREN are NOT
        # square-bracket lookalikes; widening the class must not have swept in
        # every CJK closing glyph.
        for ch in ("\u300d", "\uff09", "\u3009"):
            text = f"[OPTIONS: A | B{ch}"
            assert OPTIONS_RE_LINE.search(text) is None, f"U+{ord(ch):04X} closed the marker"

    def test_marker_must_still_end_its_line(self):
        # The deliberate "trailing note on a later line" behaviour is unchanged:
        # a lookalike-closed marker followed by more prose on the SAME line fails
        # the anchor exactly as an ASCII-closed one would.
        assert OPTIONS_RE_LINE.search("[OPTIONS: A | B\u3011 and then more") is None

    def test_label_may_contain_a_closer_block_ends_at_the_last_one(self):
        # Tempered-body property: the block ends at the LAST closer that ends the
        # line, not the first, so a label may itself contain one.
        match = OPTIONS_RE_LINE.search("[OPTIONS: a] | b\u3011")
        assert match is not None
        assert [s.strip() for s in match.group(1).split("|")] == ["a]", "b"]


class TestStreamingAgreesWithTheRegexes:
    def test_complete_lookalike_marker_detached_before_an_unfinished_fragment(self):
        """The case that actually regresses without the fix.

        ``split_trailing_protocol_suffix`` exists so that a COMPLETE options block
        sitting in front of a still-streaming ``[STEERING`` fragment is detached
        with it, rather than left in the visible text. Before the fix the trailer
        regex could not close on a lookalike, so the complete block stayed in the
        visible half and leaked into the message as literal text:

            pre-fix : ('body [OPTIONS: A | B\u3011 ', '[STEERING')
            post-fix: ('body ', '[OPTIONS: A | B\u3011 [STEERING')

        Verified against the pre-fix module: this assertion fails there, which is
        what makes it a real regression guard rather than a restatement.
        """
        for close in MARKER_CLOSERS:
            visible, suffix = split_trailing_protocol_suffix(f"body [OPTIONS: A | B{close} [STEERING")
            assert visible == "body ", f"U+{ord(close):04X} -> {visible!r}"
            assert suffix == f"[OPTIONS: A | B{close} [STEERING", f"U+{ord(close):04X} -> {suffix!r}"

    def test_lookalike_closed_marker_reads_as_finished(self):
        """Contract guard, NOT a fix-discriminator.

        The pre-fix code reaches the same answer here by a different route, so
        this passes on unfixed code and proves nothing on its own. Kept because
        it pins the intended split point for a complete lookalike-closed tail.
        """
        for close in MARKER_CLOSERS:
            visible, suffix = split_trailing_protocol_suffix(f"visible text\n\n[OPTIONS: A | B{close}")
            assert visible == "visible text\n\n", f"U+{ord(close):04X} -> {visible!r}"
            assert suffix == f"[OPTIONS: A | B{close}", f"U+{ord(close):04X} -> {suffix!r}"

    def test_closer_inside_an_unfinished_label_is_still_unfinished(self):
        """Regression: presence of a closer is NOT completeness.

        Caught by the server GPT gate on 7115a04a. An earlier revision widened
        the unfinished-marker check in ``split_trailing_protocol_suffix`` to the
        whole ``MARKER_CLOSERS`` set, so a closer appearing INSIDE a
        still-streaming label made the fragment read as finished. It was then
        never detached, and a length rotation could split the marker -- raw
        fragments render and the pills are lost. The check must stay ASCII-only;
        completeness is the trailer regex's job.
        """
        for close in "\u3011\uff3d\u3015":
            text = f"body prose [OPTIONS: Use {close} the bracket"
            visible, suffix = split_trailing_protocol_suffix(text)
            assert visible == "body prose ", f"U+{ord(close):04X} -> {visible!r}"
            assert suffix == f"[OPTIONS: Use {close} the bracket", f"U+{ord(close):04X} -> {suffix!r}"

    def test_genuinely_unfinished_marker_is_still_detached(self):
        visible, suffix = split_trailing_protocol_suffix("visible\n\n[OPTIONS: A | B")
        assert visible == "visible\n\n"
        assert suffix == "[OPTIONS: A | B"


class TestNoRedosRegression:
    def test_unterminated_marker_with_long_run_stays_linear(self):
        """Widening ``\\]`` to a character class must not introduce ambiguity with
        the trailing ``[ \\t]*`` / ``\\s*`` (CWE-1333). No closer shares a
        character with either, so the body stays unambiguous."""
        evil = "[OPTIONS:" + ("\t" * 200_000) + "x"
        start = time.perf_counter()
        assert OPTIONS_RE_LINE.search(evil) is None
        assert OPTIONS_RE_TRAILER.search(evil) is None
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"marker match too slow ({elapsed:.2f}s) — may backtrack"
