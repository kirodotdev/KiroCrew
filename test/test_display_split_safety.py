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
    canonicalize_display,
    delivered_window,
    joins_to_a_credential,
    redact_across_delivery,
    redact_for_display,
    safe_split_offset,
    severs_a_credential,
)
from kiro_crew.messaging.renderer import _default_redactor


class TestTheGradingWindowEvictsByVisibleDistanceOnly:
    """What may be evicted is decided by distance from the pending text, not by count.

    A message-count cap evicts while the visible-distance budget is unspent, and the
    shape that reaches it first is a run of deliveries whose markup canonicalises to
    nothing -- which is both the construction this module defends against and one the
    model on the other side can drive. Every message is charged at least one character
    so the count stays bounded as a consequence of the distance rule.
    """

    HEAD = "the note above ends AKIAIOSF"
    PENDING = "ODNN7EXAMPLE and more prose"

    def _pads(self, count: int) -> list[str]:
        pads = [f"[](https://example.com/pad{index})" for index in range(count)]
        assert all(
            canonicalize_display(pad) == "" for pad in pads
        ), "fixture pads are not invisible, so they do not exercise eviction"
        return pads

    def test_a_run_of_invisible_deliveries_does_not_evict_a_visible_head(self) -> None:
        delivered = [self.HEAD, *self._pads(12)]
        window: list[str] = []
        for piece in delivered:
            window = delivered_window(window, piece)

        out = redact_across_delivery(window, self.PENDING, _default_redactor)

        # The screen is every message DELIVERED plus what goes out, never the retained
        # window: reading the field under test would drop the evicted head from the
        # assertion too, and the leak would pass by agreeing with the defect.
        screen = [*delivered, out]
        for reading in (
            canonicalize_display("".join(screen)),
            "".join(canonicalize_display(f) for f in screen),
        ):
            assert _default_redactor(reading) == reading, f"key readable across frames: {screen}"
        assert self.HEAD in window, "a visible head was evicted by invisible deliveries"

    def test_older_messages_are_merged_rather_than_dropped(self) -> None:
        """The bound is met by MERGING, so a message's characters stay within reach.

        Dropping would throw away the fragment a run of near-invisible deliveries pushes
        out. Merging gives up only the boundaries inside the merged block -- the ones
        furthest from the pending text -- and keeps the characters in every reading.
        """
        window = delivered_window([self.HEAD, *self._pads(3000)], "the newest message")

        assert len(window) <= 5, f"the window grew to {len(window)} pieces"
        assert window[-1] == "the newest message", "the nearest message was lost"
        assert "AKIAIOSF" in "".join(window), "the head's characters were dropped, not merged"

    def test_two_byte_identical_deliveries_are_two_messages(self) -> None:
        """A repeat is two messages on screen, and its boundary is as real as any other.

        An edit pair that returns not-modified falls through to a duplicate send, and a
        degraded segment can repeat a header row. Collapsing equal text understates the
        screen, and the cost is MEASURED here rather than argued: two 15-character copies
        plus a 15-character pending message reach the 40-character bare-secret-run floor
        while ONE copy plus that message does not, so the collapse loses the detection
        outright -- and nothing recalls a message already sent.
        """
        piece = "Ab3Cd4Ef5Gh6Jk7"
        pending = "Mn8Pq9Rs0Tu1Vw2"
        assert not severs_a_credential(
            [piece, pending], _default_redactor
        ), "one copy already severs, so this fixture does not exercise the repeat"

        window = delivered_window(delivered_window([], piece), piece)
        out = redact_across_delivery(window, pending, _default_redactor)

        screen = [piece, piece, out]
        reading = canonicalize_display("".join(screen))
        assert _default_redactor(reading) == reading, f"secret readable across frames: {screen}"
        assert window == [piece, piece], "a repeat was collapsed into one message"


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
