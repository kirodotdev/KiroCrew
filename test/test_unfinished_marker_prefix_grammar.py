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

from conftest import assert_rejected_without_backtracking
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
        ``[OPTIONS:``) must not win ``rfind`` and land the detach point
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
        a buffer that repeats failing sentinels. Ramped on thread CPU via
        ``conftest.assert_rejected_without_backtracking`` rather than a 1.0 s
        wall clock: the bound must fail under the regression, not hang."""

        def untouched(evil: str) -> None:
            visible, suffix = split_trailing_protocol_suffix(evil)
            assert visible == evil
            assert suffix == ""

        assert_rejected_without_backtracking(untouched, lambda n: "x[OPTIONSz" * n)


class TestAnImpossibleNestedHeadIsNotHeldBack:
    """A nested head sits at a position already in the buffer, so no later byte
    can rescue it -- the finished body refuses it forever. A probe tempering
    against only its OWN head held such a fragment as still-streaming, and the
    detach walk withheld it from the visible cut. On WhatsApp the final render
    discards that suffix and never re-renders, so the text was lost silently.
    Both probes must refuse exactly what ``_MARKER_BODY_TEMPER`` refuses.
    """

    def test_an_action_label_nesting_the_singular_head_stays_visible(self):
        text = "Answer [OPTION-ACTIONS: close=x [OPTION:"
        assert split_trailing_protocol_suffix(text) == (text, "")

    def test_an_options_label_nesting_the_singular_head_stays_visible(self):
        text = "Answer [OPTIONS: see [OPTION:"
        assert split_trailing_protocol_suffix(text) == (text, "")

    def test_the_inner_completable_prefix_is_still_held(self):
        """CONTROL: only the impossible OUTER fragment stays visible. The inner
        ``[OPTIONS: menu`` can still finish with ``| x]``, so it is withheld."""
        visible, suffix = split_trailing_protocol_suffix(
            "Answer [OPTION-ACTIONS: close=Return to [OPTIONS: menu"
        )
        assert visible == "Answer [OPTION-ACTIONS: close=Return to "
        assert suffix == "[OPTIONS: menu"

    def test_control_ordinary_bracket_nesting_is_still_held(self):
        """CONTROL against over-tightening: ``arr[0`` opens no head, so the
        temper admits it and the fragment must remain a streaming candidate."""
        visible, suffix = split_trailing_protocol_suffix("body [OPTIONS: check arr[0")
        assert visible == "body "
        assert suffix == "[OPTIONS: check arr[0"

    def test_control_a_headless_bracket_word_is_still_held(self):
        """CONTROL: ``[OPTIONS`` with no colon is not a head, so the existing
        detach behaviour for it is unchanged by the widened temper."""
        visible, suffix = split_trailing_protocol_suffix(
            "prose [OPTIONS: mention [OPTIONS in a label"
        )
        assert visible == "prose "
        assert suffix == "[OPTIONS: mention [OPTIONS in a label"
