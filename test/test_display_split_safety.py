"""``joins_to_a_credential`` / ``safe_split_offset`` -- the cut-safety primitive.

A message cap cuts RAW text while the reader sees the CANONICAL rendering of each
piece, so a credential the model split with markup can be severed by the cut:
each piece is scrubbed on its own and matches nothing, and the reader's client
renders the markup away and rejoins the halves. These pin the primitive that
decides where a cut may fall.
"""

from __future__ import annotations

import pytest

from conftest import CREDENTIAL_STRADDLE_SHAPES
from kiro_crew.messaging.display_safety import (
    _balanced_link_reading,
    _first_close_reading,
    _link_free_reading,
    canonicalize_display,
    joins_to_a_credential,
    redact_for_display,
    safe_split_offset,
    severs_a_credential,
)
from kiro_crew.messaging.renderer import _default_redactor
from kiro_crew.telegram.renderer import _strip_hr


class TestTheOracleIsAsStrongAsTheSendPath:
    """The cut is CHOSEN with one scrubber and the bytes are SENT through another.

    Every call site hands the oracle the bare ``_default_redactor``, while the outgoing
    slice is scrubbed with the render-aware ``Renderer.redact_for_target``, which is
    ``redact_for_display`` wrapped around that same redactor. Choosing a cut with a WEAKER
    scrubber than the one the bytes are rendered through would approve a cut whose halves
    the send path then leaves intact, so the two must agree.

    They do, because the oracle canonicalizes each reading BEFORE scrubbing it -- it earns
    the display-awareness internally instead of being handed it. That is invisible at the
    call sites, and a future edit that scrubbed raw text inside either primitive would
    break it silently. Hence this test.
    """

    @staticmethod
    def _display_aware(text: str) -> str:
        # What the send path scrubs with, as a plain function.
        safe, _ = redact_for_display(text, _default_redactor)
        return safe

    @pytest.mark.parametrize(("head", "tail"), CREDENTIAL_STRADDLE_SHAPES)
    def test_both_scrubbers_decide_the_same_join(self, head: str, tail: str) -> None:
        assert joins_to_a_credential(head, tail, _default_redactor) == joins_to_a_credential(
            head, tail, self._display_aware
        ), "the oracle disagreed with the scrubber the bytes are sent through"

    @pytest.mark.parametrize(("head", "tail"), CREDENTIAL_STRADDLE_SHAPES)
    def test_the_offsets_do_not_depend_on_which_scrubber_decides(
        self, head: str, tail: str
    ) -> None:
        # The primitive reaches the redactor ONLY through the oracle, so the agreement
        # above has to carry through to the offsets it returns.
        text = head + tail
        for limit in (len(head), len(text), len(text) // 2):
            assert safe_split_offset(text, limit, _default_redactor) == safe_split_offset(
                text, limit, self._display_aware
            ), "a cut offset changed with the scrubber"


class TestRedactionIsAFixedPoint:
    """The keystone `joins_to_a_credential` rests on, pinned on its own.

    The oracle reads a join twice -- canonicalize-then-scan, and scan-each-side-then-join
    -- and both readings assume that once a piece is scrubbed, scrubbing it again is a
    no-op EVEN AFTER the reader's client has rendered the markup away. Nothing else in
    these tests says so out loud, so a change to the tag or to canonicalization could
    quietly turn a scrubbed piece back into something that scans, and every caller that
    inserts the tag (the terminal seam breaker most of all) would be resting on sand.
    """

    @pytest.mark.parametrize(("head", "tail"), CREDENTIAL_STRADDLE_SHAPES)
    def test_scrubbing_a_scrubbed_piece_changes_nothing(self, head: str, tail: str) -> None:
        # Each side alone, the join, and the canonical join: every piece a caller can
        # hand the oracle after a scrub.
        for piece in (head, tail, head + tail, canonicalize_display(head + tail)):
            once, _ = redact_for_display(piece, _default_redactor)
            settled = _default_redactor(canonicalize_display(once))
            twice, _ = redact_for_display(once, _default_redactor)

            assert settled == canonicalize_display(
                once
            ), "a scrubbed piece scanned again after canonicalization"
            assert twice == once, "scrubbing a scrubbed piece was not a no-op"


class TestJoinsToACredential:
    @pytest.mark.parametrize(("head", "tail"), CREDENTIAL_STRADDLE_SHAPES)
    def test_a_severed_key_is_reported(self, head: str, tail: str) -> None:
        # Premise first: neither half is a credential ALONE, which is exactly why
        # scrubbing each piece cannot see this and the CUT is what has to be right.
        # Asserted so a fixture that stops straddling fails loudly instead of
        # passing on a case it does not exercise.
        assert _default_redactor(head) == head, "the head half must be clean alone"
        assert _default_redactor(tail) == tail, "the tail half must be clean alone"

        assert joins_to_a_credential(head, tail, _default_redactor)

    @pytest.mark.parametrize(
        ("head", "tail"),
        [
            pytest.param("plain prose ending here", " and continuing there", id="prose"),
            pytest.param("emphasis **spanning", " the cut** is harmless", id="emphasis-span"),
            pytest.param("a [link](https://ex.test/a,b) then", " more prose", id="whole-link"),
            pytest.param("", "AKIAIOSFODNN7EXAMPLE is scrubbed here", id="key-wholly-in-tail"),
            pytest.param("AKIAIOSFODNN7EXAMPLE is scrubbed here", "", id="key-wholly-in-head"),
        ],
    )
    def test_a_harmless_cut_is_allowed(self, head: str, tail: str) -> None:
        # The allow direction. A key that lies wholly inside one side is redacted by
        # that side's own pass, so it must NOT be reported here -- reporting it would
        # walk the cut back for a boundary that severs nothing, and a guard that
        # refuses everything delivers nothing.
        assert not joins_to_a_credential(head, tail, _default_redactor)

    def test_a_cut_that_closes_a_link_is_reported(self) -> None:
        # Caught ONLY by canonicalising the concatenation: each half alone is an
        # unfinished link, and only together do they form a link that collapses to
        # its label, putting the two halves of the key side by side.
        head = "[AKIA](https://ex.test/a,b"
        tail = ")IOSFODNN7EXAMPLE"
        assert joins_to_a_credential(head, tail, _default_redactor)

    def test_a_cut_inside_a_link_target_is_reported(self) -> None:
        # Caught ONLY by canonicalising each side and then joining. Completing the
        # link makes the concatenation collapse to the label, so the key inside the
        # URL disappears from that reading -- while on screen each half is an
        # unfinished link whose URL text stays visible, and the reader reads through.
        head = "[l](https://ex.test/x/AKIAIOSF"
        tail = "ODNN7EXAMPLE)"
        assert joins_to_a_credential(head, tail, _default_redactor)

    @pytest.mark.parametrize(
        ("head", "tail", "seen_by_the_join"),
        [
            pytest.param(
                "[AKIA](https://ex.test/a,b", ")IOSFODNN7EXAMPLE", True, id="cut-closes-a-link"
            ),
            pytest.param(
                "[l](https://ex.test/x/AKIAIOSF", "ODNN7EXAMPLE)", False, id="cut-inside-a-url"
            ),
        ],
    )
    def test_neither_reading_of_a_join_contains_the_other(
        self, head: str, tail: str, seen_by_the_join: bool
    ) -> None:
        # Why BOTH readings are scanned rather than one. Canonicalising the
        # concatenation is the wider reading for delimiter runs, which concatenation
        # can only extend; canonicalising each side first is wider wherever
        # canonicalising DROPS text, which is what a link does to its target. Each
        # shape here is found by exactly one reading, so dropping either reading
        # ships that shape. Pinned so the day one reading starts covering the other,
        # CI says so instead of the guard quietly narrowing.
        head_safe = redact_for_display(head, _default_redactor)[0]
        tail_safe = redact_for_display(tail, _default_redactor)[0]
        joined = canonicalize_display(head_safe + tail_safe)
        on_screen = canonicalize_display(head_safe) + canonicalize_display(tail_safe)

        assert (_default_redactor(joined) != joined) is seen_by_the_join
        assert (_default_redactor(on_screen) != on_screen) is not seen_by_the_join

    def test_a_heading_marker_dropped_after_the_cut_is_reported(self) -> None:
        # Caught ONLY by a further reading, applied per side. Telegram's fallback
        # and its HTML seal both drop a heading marker, so a field name ending one
        # message and ``#   : <value>`` opening the next read as the assignment on
        # screen. The literal join keeps the ``#`` between them, and the canonical
        # form keeps it too, so both scan clean while the reader sees the key.
        head = "Rotated the key.\n\nSecretAccessKey"
        tail = "#   : wJalrXUtnFEMI-K7MDENG-bPxRfiCYEXAMPLEKEY"
        assert _default_redactor(head) == head
        assert _default_redactor(tail) == tail
        assert _default_redactor(head + tail) == head + tail
        joined = canonicalize_display(head + tail)
        assert _default_redactor(joined) == joined, "the canonical form keeps the marker"

        assert joins_to_a_credential(head, tail, _default_redactor)

    def test_a_heading_the_cut_leaves_whole_is_allowed(self) -> None:
        head = "Rotated the key.\n\nSecretAccessKey rotated."
        tail = "# Next steps\n\nNothing else."
        assert not joins_to_a_credential(head, tail, _default_redactor)


class TestSafeSplitOffset:
    def test_prose_cuts_at_the_limit(self) -> None:
        text = "just some ordinary prose with nothing secret in it at all"
        assert safe_split_offset(text, 20, _default_redactor) == 20

    def test_text_within_the_limit_is_not_cut(self) -> None:
        text = "short"
        assert safe_split_offset(text, 999, _default_redactor) == len(text)

    def test_a_non_positive_limit_yields_nothing(self) -> None:
        assert safe_split_offset("anything", 0, _default_redactor) == 0

    def test_the_offset_moves_back_off_a_severed_key(self) -> None:
        head, tail = "[AKIA](https://ex.test/a,b)", "IOSFODNN7EXAMPLE"
        pad = "x" * 40
        text = pad + head + tail + " tail prose"
        limit = len(pad) + len(head)

        offset = safe_split_offset(text, limit, _default_redactor)

        assert 0 < offset <= len(pad), "the cut must land before the key begins"
        assert not joins_to_a_credential(text[:offset], text[offset:], _default_redactor)

    def test_the_search_is_logarithmic_not_linear(self) -> None:
        # The cost bound is the reason the candidates step back exponentially: this
        # runs on attacker-influenced text on every outgoing frame. Counting the
        # redaction passes is what pins it -- a linear walk would take ~2000 here.
        calls = 0

        def counting_redactor(text: str) -> str:
            nonlocal calls
            calls += 1
            return _default_redactor(text)

        key = "AKIAIOSFODNN7EXAMPLE"
        text = "x" * 2000 + key[:8] + key[8:] + " tail prose"
        limit = 2000 + 8

        offset = safe_split_offset(text, limit, counting_redactor)

        assert offset <= 2000
        # Four candidates (the limit, then 1, 2, 4, 8 back) at a handful of passes
        # each. The ceiling is deliberately loose: the property is the ORDER, and a
        # linear walk cannot fit under it.
        assert calls < 60, calls


class TestSeversACredential:
    """The n-piece case: a rotation delivers a SEQUENCE, not one cut.

    ``joins_to_a_credential`` answers for one boundary. A rotation hands the reader
    several messages in order, and two readings are needed because neither subsumes
    the other -- the whole join catches a key whose MARKUP swallows a whole piece,
    and each boundary against the rest catches a key completed at a piece's end.
    """

    def test_one_piece_has_no_boundary(self) -> None:
        assert not severs_a_credential(["AKIAIOSFODNN7EXAMPLE"], _default_redactor)

    def test_innocent_pieces_stay_clean(self) -> None:
        assert not severs_a_credential(["ordinary ", "prose in ", "three parts"], _default_redactor)

    def test_a_key_severed_between_two_pieces_is_caught(self) -> None:
        key = "AKIAIOSFODNN7EXAMPLE"
        pieces = [key[:8], key[8:]]
        for piece in pieces:
            assert _default_redactor(piece) == piece, "each piece alone must look clean"
        assert severs_a_credential(pieces, _default_redactor)

    def test_a_key_spanning_three_pieces_is_caught_though_no_pair_shows_it(self) -> None:
        """The reading that only the whole-sequence join can produce.

        No NEIGHBOURING pair holds the key, so a scan that walked pairs alone would
        report all three clean. This is why the middle piece being the whole of one
        message is not a defence.
        """
        key = "AKIAIOSFODNN7EXAMPLE"
        pieces = [key[:6], key[6:13], key[13:]]
        assert severs_a_credential(pieces, _default_redactor)

    def test_a_markup_span_covering_a_whole_piece_is_caught(self) -> None:
        """Canonicalising DROPS a link's target, so a piece of any size can vanish.

        Split ``AKIA[label](url)`` so the closing bracket lands in the third piece:
        no neighbouring pair canonicalises to anything, while the full join
        collapses the url away and puts the label straight against ``AKIA``.
        """
        pieces = ["AKIA", "[IOSFODNN7EXAMPLE](https://ex.test/", "padpadpad)"]
        assert severs_a_credential(pieces, _default_redactor)

    @pytest.mark.parametrize(
        "pieces",
        [
            [
                "Rotated the key.\n\nSecretAccessKey",
                "#   : wJalrXUtnFEMI-K7MDENG-bPxRfiCYEXAMPLEKEY",
            ],
            [
                "Rotated the key.\n\nSecretAccess",
                "Key",
                "#   : wJalrXUtnFEMI-K7MDENG-bPxRfiCYEXAMPLEKEY",
            ],
        ],
    )
    def test_further_readings_grade_the_full_sequence(self, pieces) -> None:
        assert severs_a_credential(pieces, _default_redactor)

    def test_a_clean_heading_sequence_stays_clean(self) -> None:
        pieces = [
            "Rotated.\n\nSecretAccessKey rotated.",
            "# Next steps\n\nNothing else.",
        ]
        assert not severs_a_credential(pieces, _default_redactor)

    def test_balanced_link_reading_grades_the_full_sequence(self) -> None:
        pieces = ["[AKIA](https://x/((a)))", "IOSFODNN7EXAMPLE"]
        assert severs_a_credential(pieces, _default_redactor)

    def test_the_pieces_are_graded_as_delivered_not_as_split(self) -> None:
        """Whitespace the sender trims is whitespace the reader never sees.

        The raw pair is kept apart by a trailing newline, which no credential
        pattern tolerates; the delivered pair sits flush. Grading the raw form is
        exactly the miss this function's contract warns callers about.
        """
        key = "AKIAIOSFODNN7EXAMPLE"
        raw = [key[:8] + "\n\n", "   " + key[8:]]
        delivered = [piece.strip() for piece in raw]

        assert not severs_a_credential(raw, _default_redactor)
        assert severs_a_credential(delivered, _default_redactor)

    def test_a_key_completed_before_the_last_piece_is_caught(self) -> None:
        """A RUN of messages, with a message after it that spoils the pattern.

        The patterns anchor at their edges: this token's second segment is exactly
        43 characters followed by ``(?![A-Za-z0-9_-])``, so ONE alphanumeric
        character after it kills the match. The key is complete once the reader has
        read messages 1 and 2; message 3 starts with a letter. Every reading that
        runs to the END of the sequence therefore carries that letter and reports
        clean -- the whole join, the per-piece join, and each suffix. Only the run
        that STOPS at the second message sees the key, and on screen the reader has
        a message break exactly there.
        """
        segment = "eyJ" + "".join("abcdefghi-"[index % 10] for index in range(96))
        token = segment + "." + "".join("ABCDEFGHI-"[index % 10] for index in range(43))
        assert _default_redactor(token) != token, "fixture is not a credential"

        pieces = [token[:80], token[80:], "x"]
        for piece in pieces:
            assert _default_redactor(piece) == piece, "each piece alone must look clean"
        whole = canonicalize_display("".join(pieces))
        assert _default_redactor(whole) == whole, "fixture must escape the whole-join reading"

        assert severs_a_credential(pieces, _default_redactor)


class TestSafeSplitOffsetGradesTheDeliveredForm:
    """``present`` is what makes the returned offset one the caller can take.

    A renderer that trims on the way out delivers something shorter than the raw
    slice, so an offset whose RAW halves are safe can still put the delivered halves
    flush together. And because the search is deterministic, a caller that graded raw
    and re-checked the answer in delivered form would reject the same offset every
    rotation and never send the segment at all.
    """

    #: One trailing space is all it takes: the raw halves are separated by it and
    #: the delivered halves are not.
    _KEY = "AKIAIOSFODNN7EXAMPLE"

    def _text(self) -> tuple[str, int]:
        pad = "x" * 40
        text = pad + self._KEY[:8] + " " + self._KEY[8:] + " tail prose"
        return text, len(pad) + 8 + 1

    def test_the_raw_grade_accepts_an_offset_the_reader_can_rejoin(self) -> None:
        text, limit = self._text()
        offset = safe_split_offset(text, limit, _default_redactor)
        assert offset == limit, "raw halves are kept apart by the space at the cut"
        assert severs_a_credential(
            [text[:offset].strip(), text[offset:].strip()], _default_redactor
        ), "yet the DELIVERED halves rejoin the key"

    def test_the_delivered_grade_moves_the_offset_back(self) -> None:
        text, limit = self._text()
        offset = safe_split_offset(text, limit, _default_redactor, lambda piece: piece.strip())
        assert offset != limit, "the offset the raw grade accepted must be rejected"
        assert not severs_a_credential(
            [text[:offset].strip(), text[offset:].strip()], _default_redactor
        )

    def test_the_default_is_identity(self) -> None:
        text = "just some ordinary prose with nothing secret in it at all"
        assert safe_split_offset(text, 20, _default_redactor) == safe_split_offset(
            text, 20, _default_redactor, lambda piece: piece
        )


class TestTheDeliveredReadingCatchesWhatAPreSplitRedactionCannot:
    """Why the rotation gate exists at all, now the splitter redacts before cutting.

    ``_split_markdown_bounded`` reduces the text before choosing any boundary, so a
    key lying plainly across the budget is replaced and no chunk boundary severs it.
    That pre-split pass reads the text AS WRITTEN. A horizontal rule standing between
    the two halves keeps them apart there -- and the Telegram seal strips horizontal
    rules, so the halves sit flush on screen. Only a reading of the DELIVERED form
    sees it, which is the boundary the gate owns.
    """

    def test_a_rule_between_the_halves_hides_the_key_from_a_pre_split_scan(self) -> None:
        key = "AKIAIOSFODNN7EXAMPLE"
        halves = [key[:8] + "\n\n---\n", "\n" + key[8:] + " tail prose"]
        delivered = [_strip_hr(piece).strip() for piece in halves]

        assert _default_redactor("".join(halves)) == "".join(halves), (
            "a scan of the text as written must find nothing, or the pre-split "
            "redaction would already have caught this"
        )
        assert not severs_a_credential(halves, _default_redactor)
        assert severs_a_credential(halves, _default_redactor, _strip_hr_then_strip)
        assert _default_redactor("".join(delivered)) != "".join(delivered)


def _strip_hr_then_strip(piece: str) -> str:
    """The Telegram seal's own transform, as the gate hands it to the grader."""
    return _strip_hr(piece).strip()


class TestAnInteriorRunIsRead:
    """A credential can sit with a spoiling message on EACH side of it.

    Every whole, prefix and suffix reading of the sequence carries at least one of
    those two frames, and the patterns anchor at both ends, so each of those
    readings is spoiled and reports clean. The key is only visible in a reading
    bounded on both sides -- an INTERIOR run -- which is why those are read too.

    A rotation of model text makes both frames ordinary rather than crafted: the
    splitter rstrips each chunk, so a chunk ending on a letter is the common case.
    """

    #: A link-spanning JWT. Its last segment is a FIXED 43 characters, which is what
    #: makes an alphanumeric frame on the right genuinely spoil the match: a pattern
    #: whose tail is open-ended just absorbs the frame and matches anyway, so a
    #: variable-length token would be caught by a suffix reading and prove nothing.
    _FIRST = "eyJ" + "abcdefghij" * 10
    _LAST = "Z" * 43
    #: Two interior pieces whose canonical join is that token: canonicalising DROPS a
    #: link's target, so each label lands against the next one.
    _INTERIOR = [f"[{_FIRST}](https://q/", f"aaa)[.{_LAST}](https://q/bbb)"]
    _FRAME = "zzz"

    def test_an_interior_run_between_two_spoiling_frames_is_read(self) -> None:
        pieces = [self._FRAME, *self._INTERIOR, self._FRAME]

        joined = canonicalize_display("".join(pieces))
        assert _default_redactor(joined) == joined, "the frames no longer spoil the whole reading"
        inner = canonicalize_display("".join(self._INTERIOR))
        assert _default_redactor(inner) != inner, "the interior pieces no longer form a key"

        assert severs_a_credential(pieces, _default_redactor), "the interior run was not read"

    def test_the_same_run_unframed_is_still_caught(self) -> None:
        """Control: the reading is added, not swapped for the ones already there."""
        assert severs_a_credential(self._INTERIOR, _default_redactor)

    def test_a_clean_sequence_of_many_pieces_stays_clean(self) -> None:
        """Control: the windows refuse runs, they do not reject ordinary text."""
        assert not severs_a_credential(["word " * 4 for _ in range(40)], _default_redactor)


class TestLinkReadingsAgreeWithoutAnOpener:
    """Without a ``](`` opener every link reading IS the canonical rendering.

    Each link collapse is the identity on text with no ``](``, and the canonical
    link grammar cannot match without it, so the balanced, first-close and
    link-free readings all reduce to the same joining passes the canonical form
    applies. ``_sequence_readings`` relies on this equality: it renders such a
    window once and grades that one string for all four readings. A reading that
    starts to differ here would have to be rendered on its own again.
    """

    _WINDOWS = [
        "plain words and digits 123",
        "AKIA**IOSF**ODNN__7EXA__MPLE",
        "AKIA`IOSF`ODNN``7EXA``MPLE ```fenced```",
        "AKIA~~IOSF~~ODNN||7EXA||MPLE",
        "<https://x/y|AKIAIOSF>ODNN<https://q|7EXAMPLE>",
        "AKIA\u200bIOSF\u200dODNN\ufeff7EXA\u00adMPLE",
        "[AKIAIOSF] (ODNN) [7EXA] (MPLE) and no opener",
        "[label] (https://x/y) [other][ref] ((nested)) )(",
        "\\[AKIA\\]\\(IOSF\\) \\*ODNN\\* \\`7EXA\\` \\<MPLE\\>",
        "line one **AKIA\nIOSF** line two <u|l>\n\n*_~`|",
        "the opener held apart: ] ( and ]\n( and ]\u200b(",
        "",
    ]

    @pytest.mark.parametrize("window", _WINDOWS)
    def test_every_link_reading_equals_the_canonical_form(self, window: str) -> None:
        assert "](" not in window
        canonical = canonicalize_display(window)
        assert _balanced_link_reading(window) == canonical
        assert _first_close_reading(window) == canonical
        assert _link_free_reading(window) == canonical

    def test_the_equality_is_specific_to_the_missing_opener(self) -> None:
        """Control: with the opener present the readings genuinely diverge."""
        window = "[AKIAIOSF](https://x/a_(b))ODNN7EXAMPLE"
        readings = {
            canonicalize_display(window),
            _first_close_reading(window),
            _link_free_reading(window),
        }
        assert len(readings) == 3
