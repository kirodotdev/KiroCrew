"""A bare opener may not be the one whose partner closer ENDS the marker.

The bare-``[`` alternative in the label body exists so a stray opener does not sink
a whole marker -- ``[OPTIONS: Fix [x logging | Skip]`` parses, and is pinned in
``test_options_marker_label_closers.py``. But that alternative also admitted the
opener in a marker the model never closed::

    [OPTIONS: A | B then check arr[0]

Here the only closer on the line is the one belonging to ``arr[0]``. The body ran
on through the prose, that ``]`` became the marker's terminator, and because every
consumer removes the whole match -- ``parse_options`` replaces, ``slack.format`` and
``messaging.renderer`` cut at ``match.start()``, and ``whatsapp.turn_renderer``
PERSISTS the cut turn -- the line was deleted from the message and came back as the
pill label ``B then check arr[0``.

WHY NO BRACKET RULE CAN SEPARATE THEM. Reduce both to their bracket/separator
skeleton and they are the same string::

    [OPTIONS: Fix | Skip [x logging]        ->  w|w[w]
    [OPTIONS: A | B then check arr[0]       ->  w|w[w]

So the discriminator cannot be the brackets. It is WHERE the opener sits relative
to the END: the bare form is refused exactly when nothing but ordinary text lies
between it and a closer sitting at the end anchor, because that closer is then its
own partner rather than the marker's, which means the marker is unterminated.

Crossing ``|`` clears the gate -- the opener is inside a label and the list
continues past it, which is what keeps the pinned stray-opener shape. Crossing
another BRACKET clears it too, because some other bracket form owns that closer,
which is what keeps ``[OPTIONS: Fix arr[0] | Skip]`` (continuation admits the
``]``) and ``[OPTIONS: a[1] | b[2]]``.

``,`` does NOT clear it. A comma is only the fallback separator -- the frontend's
``parseOptions`` reaches for it just when no ``|`` is present -- and inside brackets
it is ordinary punctuation, so a scan that stopped there halted before the closer
in ``[OPTIONS: A | B then inspect dict[str, int]`` and cleared the gate.

The boundary this does NOT reach is nesting at the end of the line; see
:class:`TestTheResidualThisGateCannotClose` for why no regex closes it.

Two claims are asserted separately throughout, as elsewhere in this grammar's
suites, because only the second is what a user experiences: that the pattern does
not MATCH, and that the visible text is UNCHANGED.
"""

from __future__ import annotations

import re
import time

import pytest

from kiro_crew.constants import (
    MARKER_CLOSERS,
    OPTIONS_RE_LINE,
    OPTIONS_RE_TRAILER,
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


class TestAnUnterminatedMarkerNoLongerEatsItsLine:
    @pytest.mark.parametrize(
        "text",
        [
            "[OPTIONS: A | B then check arr[0]",
            "[OPTIONS: Ship | Hold and read docs[4]",
            "[OPTIONS: Merge | Wait then diff src/app[3]",
            # The wrapper and stray-tic forms reach the same terminator, so the gate
            # has to account for both or the shape simply comes back wearing one.
            "**[OPTIONS: A | B then check arr[0]**",
            "[OPTIONS: A | B then check arr[0](OPTIONS)",
            # A COMMA INSIDE the terminal bracket. The scan has to cross it to reach
            # the partner closer: a comma is only the fallback separator, and inside
            # brackets it is ordinary punctuation, so a scan that stopped there
            # halted before the closer and cleared the gate.
            "[OPTIONS: A | B then inspect dict[str, int]",
            "[OPTIONS: Ship | Hold then arr[i, j]",
        ],
    )
    def test_it_is_declined(self, text: str):
        assert OPTIONS_RE_LINE.search(text) is None, text

    @pytest.mark.parametrize(
        "text",
        [
            "[OPTIONS: A | B then check arr[0]",
            "[OPTIONS: Ship | Hold and read docs[4]",
        ],
    )
    def test_and_therefore_deletes_nothing(self, text: str):
        # The claim that matters. Asserted at the consumer, not only at the regex,
        # because removing the match is what deleted the line.
        assert OPTIONS_RE_LINE.sub("", text) == text, text
        assert split_options_trailer(text) == (text, []), text

    def test_the_trailer_grammar_declines_it_too(self):
        # The TRAILER body spans newlines under DOTALL, so its blast radius was a
        # paragraph rather than a line. Its gate is anchored on ``\\s*\\Z``.
        text = "Here are your choices.\n\n[OPTIONS: A | B then check arr[0]"
        assert OPTIONS_RE_TRAILER.search(text) is None
        assert OPTIONS_RE_TRAILER.sub("", text) == text

    def test_the_trailer_gate_sees_ACROSS_a_newline(self):
        """The TRAILER scan must span newlines, because its body does.

        A scan that stopped at ``\\n`` is blinded the moment the prose wraps: the
        opener is on one line and its partner closer on the next, the gate finds no
        closer before the newline and clears, and the body -- which spans newlines
        under DOTALL -- runs on to that closer anyway. The scan has to see as far as
        the body can reach or it is not looking at the same terminator.
        """
        text = "[OPTIONS: A | B then check arr[0\nAnd that is all]"
        assert OPTIONS_RE_TRAILER.search(text) is None
        assert OPTIONS_RE_TRAILER.sub("", text) == text
        assert split_options_trailer(text) == (text, [])

    def test_the_LINE_scan_stops_at_a_newline_because_its_body_does(self):
        # The mirror of the above, so the asymmetry between the two scans is pinned
        # rather than looking like an oversight: the LINE body cannot cross a newline,
        # so neither should its scan, and the shape above is simply not a LINE match.
        assert "|\\n]*" in OPTIONS_RE_LINE.pattern
        assert "|\\n]*" not in OPTIONS_RE_TRAILER.pattern

    def test_a_marker_with_no_closer_at_all_is_still_declined(self):
        # The neighbouring shape, unchanged: with no closer anywhere the end anchor
        # was never satisfiable, so this never matched and still does not.
        assert OPTIONS_RE_LINE.search("[OPTIONS: A | B then check arr") is None


class TestTheDiscriminatorIsNotBracketStructure:
    """Pinned because it is the reason the gate is shaped the way it is.

    If someone later replaces this with a bracket-balance rule, these assertions
    are what say why that cannot work: the shape the grammar must ACCEPT and the
    shape it must REFUSE have identical bracket structure.
    """

    def test_the_two_shapes_share_a_skeleton(self):
        refused = "[OPTIONS: A | B then check arr[0]"
        accepted = "[OPTIONS: Fix | Skip [x logging]"
        assert skeleton(refused) == skeleton(accepted) == "w|w[w]"

    def test_and_are_nonetheless_decided_differently(self):
        # ...because the gate reads the run between the opener and the end anchor,
        # not the bracket nesting. Both of these end in a closer at the anchor, and
        # both hold one unmatched opener; only the CONTENT of that run differs.
        assert OPTIONS_RE_LINE.search("[OPTIONS: A | B then check arr[0]") is None
        # The accepted twin is the un-pinned tail of the stray-opener family; it is
        # given up by the same rule, and that is the cost recorded below.
        assert OPTIONS_RE_LINE.search("[OPTIONS: Fix | Skip [x logging]") is None

    def test_the_pinned_stray_opener_shape_is_the_one_that_survives(self):
        # A separator after the opener is what clears the gate, and it is exactly
        # what distinguishes the supported shape: the opener is inside a label and
        # the list continues past it.
        match = OPTIONS_RE_LINE.search("[OPTIONS: Fix [x logging | Skip]")
        assert match is not None
        assert match.group("labels") == " Fix [x logging | Skip"
        assert skeleton("[OPTIONS: Fix [x logging | Skip]") == "w[w|w]"


class TestWhatMustNotHaveChanged:
    @pytest.mark.parametrize(
        ("text", "labels"),
        [
            # A closer admitted by CONTINUATION, whose opener therefore is not the
            # terminator's partner. The gate must not reach these.
            ("[OPTIONS: Fix arr[0] | Skip]", " Fix arr[0] | Skip"),
            ("[OPTIONS: a[1] | b[2]]", " a[1] | b[2]"),
            ("[OPTIONS: Fix list[dict[str, Any]] | Skip]", " Fix list[dict[str, Any]] | Skip"),
            # Matched pairs, which own their own closer.
            ("[OPTIONS: Fix [x] logging | Skip]", " Fix [x] logging | Skip"),
            ("[OPTIONS: Read arr[0] now | Skip it]", " Read arr[0] now | Skip it"),
            ("[OPTIONS: See [1] above | Skip]", " See [1] above | Skip"),
            ("[OPTIONS: Fix dict[str, Any] now | Skip]", " Fix dict[str, Any] now | Skip"),
            # No opener at all, so no gate applies.
            ("[OPTIONS: Alpha ] | Bravo ]]", " Alpha ] | Bravo ]"),
            ("[OPTIONS: Alpha ], Bravo]", " Alpha ], Bravo"),
            ("[OPTIONS: Yes | No]", " Yes | No"),
            ("**[OPTIONS: Yes | No]**", " Yes | No"),
            ("[OPTIONS: A | B](OPTIONS)", " A | B"),
            # Prose ending in a closer with no opener: the closer genuinely IS the
            # marker's, so this parses, as it did before.
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
    def test_every_previously_declined_shape_still_declines(self, text: str):
        assert OPTIONS_RE_LINE.search(text) is None, text


class TestTheCost:
    """What the gate gives up, enumerated rather than summarised.

    One family: a stray opener in the FINAL label, where no separator follows to
    clear the gate. It fails toward a VISIBLE marker rather than a deleted line,
    which is what makes it affordable -- and it is the same direction every other
    cost in this grammar fails in.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "[OPTIONS: Fix | Skip [x logging]",
            "[OPTIONS: Fix [x logging]",
            # And the comma variant: only a COMMA follows the stray opener, which
            # does not clear the gate, so this joins the same family. It is the price
            # of seeing through ``dict[str, int]`` to its closer.
            "[OPTIONS: Fix [x logging, Skip]",
        ],
    )
    def test_a_stray_opener_in_the_final_label_is_given_up(self, text: str):
        assert OPTIONS_RE_LINE.search(text) is None, text
        # Affordable because nothing is removed.
        assert OPTIONS_RE_LINE.sub("", text) == text, text
        assert split_options_trailer(text) == (text, []), text


class TestTheResidualThisGateCannotClose:
    """NESTED terminal brackets still reach the terminator, and no regex can stop it.

    The gate clears when the scan meets another bracket, on the grounds that some
    other bracket form owns that closer. With nesting at the end of the line that
    reasoning fails: in ``list[dict[str, int]]`` the OUTER opener's partner is the
    final ``]``, which is the terminator, but the scan halts at the inner ``[`` and
    never sees it.

    Encoding one level of nesting into the scan only moves the boundary -- depth
    three defeats that, depth four the next one, and so on. The condition the gate
    is reaching for is "the terminating closer is not nested", which is bracket
    BALANCE, and balance is not expressible as a regular expression at unbounded
    depth. Closing it means the check cannot live in the pattern at all.

    Pinned so the boundary is a named, measured residual rather than a surprise:
    depth one is fixed, depth two and beyond behave as they do on main.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "[OPTIONS: A | B then inspect list[dict[str, int]]",
            "[OPTIONS: A | B then inspect list[dict[x]]",
            "[OPTIONS: A | B then inspect a[b[c[d]]]",
        ],
    )
    def test_nested_terminal_brackets_still_match(self, text: str):
        # NOT an assertion that this is desirable -- it is the residual, recorded at
        # the depth where the gate stops reaching. If a future change closes it, this
        # test should flip and the docstring above should go with it.
        assert OPTIONS_RE_LINE.search(text) is not None, text

    def test_depth_one_is_the_part_that_is_fixed(self):
        # The boundary, either side of it, in one place.
        assert OPTIONS_RE_LINE.search("[OPTIONS: A | B then inspect dict[str, int]") is None
        assert (
            OPTIONS_RE_LINE.search("[OPTIONS: A | B then inspect list[dict[str, int]]") is not None
        )


class TestLinearity:
    def test_the_gate_scan_is_tempered(self):
        # Read off the compiled pattern so the claim cannot drift from the comment:
        # the scan stops at the first bracket or ``|``, which is what keeps the runs
        # scanned from different openers disjoint and the total work linear.
        assert f"[^[{re.escape(MARKER_CLOSERS)}|\\n]*" in OPTIONS_RE_LINE.pattern
        assert f"[^[{re.escape(MARKER_CLOSERS)}|]*" in OPTIONS_RE_TRAILER.pattern

    def test_it_adds_no_quantifier_over_a_quantifier(self):
        # The shape that backtracks exponentially, and the one the frontend's
        # ``.source`` pin also forbids.
        assert re.search(r"\([^)]*[+*]\)[+*]", OPTIONS_RE_LINE.pattern) is None

    @pytest.mark.parametrize("reps", [2_000, 10_000, 40_000])
    def test_many_bare_openers_stay_linear(self, reps: int):
        # The adversarial shape for this gate specifically: one lookahead entered
        # per opener, on an unterminated marker so every one of them fails.
        src = "[OPTIONS:" + ("a[b" * reps)
        started = time.perf_counter()
        assert OPTIONS_RE_LINE.search(src) is None
        assert OPTIONS_RE_TRAILER.search(src) is None
        assert time.perf_counter() - started < 2.0
