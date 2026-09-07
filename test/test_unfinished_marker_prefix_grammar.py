"""``split_trailing_protocol_suffix`` judges occurrences against the marker
grammar, never by bare substring location.

Sibling of the ``preserve_tail_marker`` locate-by-substring defect: ``rfind``
located ``[STEERING``/``[OPTIONS`` anywhere in the buffer, and a mid-prose
mention with no later ASCII ``]`` was detached as a "still-streaming marker".
Consumers (Discord/Telegram rotation, WhatsApp render) drop the detached
suffix from the visible cut, so everything from the mention onward vanished
from display -- prose truncation on an attacker-positionable token.

The fix admits an occurrence only when the tail it starts is a strict PREFIX
of the marker grammar, probing occurrences rightmost-first so label bytes
that merely contain a sentinel cannot shadow the genuine fragment start.
"""

import time

from kiro_crew.constants import split_trailing_protocol_suffix


class TestProseMentionIsNotAMarker:
    def test_prose_mentioning_options_is_left_visible(self):
        """ATTACK (fails before the fix): a tail mentioning ``[OPTIONS`` in
        running prose, with no later ``]``, was detached and the prose lost."""
        text = "Wrap choices with the [OPTIONS marker followed by labels\nMore prose here"
        visible, suffix = split_trailing_protocol_suffix(text)
        assert visible == text
        assert suffix == ""

    def test_prose_mentioning_steering_is_left_visible(self):
        """ATTACK: ``[STEERING`` followed by a word that is not ``steer-<id>``
        reads as prose, not a streaming acknowledgment fragment."""
        text = "The [STEERING acknowledgment renders as a chip"
        visible, suffix = split_trailing_protocol_suffix(text)
        assert visible == text
        assert suffix == ""

    def test_sentinel_glued_to_prose_is_left_visible(self):
        """ATTACK: no colon after ``[OPTIONS`` means it cannot be a marker."""
        text = "see [OPTIONSDOC for details"
        visible, suffix = split_trailing_protocol_suffix(text)
        assert visible == text
        assert suffix == ""


class TestGenuineFragmentsStillDetach:
    def test_streaming_options_fragment_detaches(self):
        """CONTROL: the streaming case the function exists for is unchanged."""
        visible, suffix = split_trailing_protocol_suffix("visible\n\n[OPTIONS: A | Cho")
        assert visible == "visible\n\n"
        assert suffix == "[OPTIONS: A | Cho"

    def test_cut_exactly_at_the_sentinel_detaches(self):
        for frag in ("[OPTIONS", "[STEERING"):
            visible, suffix = split_trailing_protocol_suffix(f"body {frag}")
            assert visible == "body ", frag
            assert suffix == frag, frag

    def test_streaming_steering_ack_detaches_at_every_cut_point(self):
        """CONTROL: a genuine ``[STEERING steer-<id>: <summary>]`` ack cut at
        any byte before its closer is still recognized as unfinished."""
        full = "[STEERING steer-4a2f: rewrote the loop"
        for cut in range(len("[STEERING"), len(full) + 1):
            frag = full[:cut]
            visible, suffix = split_trailing_protocol_suffix(f"prose {frag}")
            assert visible == "prose ", f"cut={cut} -> {visible!r}"
            assert suffix == frag, f"cut={cut} -> {suffix!r}"

    def test_steering_with_empty_id_is_not_a_marker_prefix(self):
        """The ack grammar requires a nonempty id before the colon, so
        ``steer-:`` can never be a prefix of a genuine marker."""
        text = "prose [STEERING steer-: not a real ack"
        visible, suffix = split_trailing_protocol_suffix(text)
        assert visible == text
        assert suffix == ""


class TestBenignCompositions:
    def test_complete_options_block_is_still_pulled(self):
        """BENIGN: the trailer-regex branch is untouched."""
        visible, suffix = split_trailing_protocol_suffix("pick one\n\n[OPTIONS: A | B]")
        assert visible == "pick one\n\n"
        assert suffix == "[OPTIONS: A | B]"

    def test_no_marker_no_change(self):
        text = "plain prose with [brackets] and [links](x) but no markers"
        assert split_trailing_protocol_suffix(text) == (text, "")

    def test_label_bytes_containing_a_sentinel_do_not_shadow_the_fragment(self):
        """Latent sibling cured by the rightmost-READING probe: an inner
        ``[OPTIONS`` (legal label content -- the trailer grammar only forbids
        ``[OPTIONS:``) used to win ``rfind`` and the detach point landed
        MID-LABEL, splitting the genuine marker."""
        text = "prose [OPTIONS: mention [OPTIONS in a label"
        visible, suffix = split_trailing_protocol_suffix(text)
        assert visible == "prose "
        assert suffix == "[OPTIONS: mention [OPTIONS in a label"

    def test_prose_mention_after_a_closed_block_leaves_both_alone(self):
        """A closed block earlier in the buffer plus a later prose mention:
        the mention must not detach (its tail is not a grammar prefix) and the
        closed block is mid-buffer, not a trailer."""
        text = "done [OPTIONS: A | B] and the [OPTIONS token is documented"
        assert split_trailing_protocol_suffix(text) == (text, "")


class TestOccurrenceWalkStaysLinear:
    def test_adversarial_sentinel_repetition_is_linear(self):
        """Rightmost-first probing with match-at-pos must not go quadratic on
        a buffer that repeats failing sentinels."""
        evil = "x[OPTIONSz" * 50_000
        start = time.perf_counter()
        visible, suffix = split_trailing_protocol_suffix(evil)
        elapsed = time.perf_counter() - start
        assert visible == evil
        assert suffix == ""
        assert elapsed < 1.0, f"occurrence walk too slow ({elapsed:.2f}s)"
