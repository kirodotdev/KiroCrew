"""Grapheme-cluster-safe truncation at the three clip sites.

Cutting by code point inside a grapheme cluster produces a changed or broken
glyph: a dangling joiner, a lone regional indicator (boxed letter, not a
flag), a dropped tone mark that changes the word. All three sites below share
the one guard in ``kiro_crew.imessage.plaintext``, which covers all six
joining mechanisms rather than a single subset.
"""

from kiro_crew.apps.builtins.ops_mission_control.backend.notify_out import _clip as notify_clip
from kiro_crew.computer_use.render import _clip as render_clip

FAMILY = "👨\u200d👩\u200d👧"
FLAG = "🇰🇷"


class TestComputerUseClip:
    def test_family_is_dropped_whole_not_split(self) -> None:
        assert render_clip("AB" + FAMILY, 3) == "AB…"

    def test_thai_tone_mark_survives(self) -> None:
        assert render_clip("หน้าก้", 5) == "หน้า…"

    def test_a_class_zero_thai_mark_is_not_separated_from_its_base(self) -> None:
        """U+0E31 MAO EK has combining class 0, so the class alone misses it.

        Testing the canonical combining class declares this mark safe to cut
        after, and the clip lands between the base and the vowel, which the
        reader sees as one character. Class zero is the norm for Thai, Khmer
        and Indic marks, and this text is the user's own desktop. Spelled as
        escapes so the code point under test is unambiguous: KO KAI + MAO EK.
        """
        assert render_clip("ABกัCD", 3) == "AB…"

    def test_a_class_zero_khmer_mark_is_not_separated_from_its_base(self) -> None:
        """KO KHA + U+17C1 CANDRABINDU, a spacing mark (Mc) of class 0."""
        assert render_clip("ABកេCD", 3) == "AB…"

    def test_a_class_zero_indic_matra_is_not_separated_from_its_base(self) -> None:
        """DEVANAGARI KA + U+093E VOWEL AA, a spacing matra (Mc) of class 0."""
        assert render_clip("ABकाCD", 3) == "AB…"

    def test_wider_limit_keeps_the_whole_family(self) -> None:
        assert render_clip("AB" + FAMILY + "CD", 6) == "AB…"

    def test_a_cluster_wider_than_the_limit_cannot_defeat_the_cap(self) -> None:
        """A Zalgo run has no boundary below the limit, and the cap still holds.

        Every index of ``"A" + combining marks`` joins onto the cluster, so
        ``_grapheme_boundary`` has nothing to return and falls to 0. Taking that
        as "emit the cluster whole" lets a 41-code-point run out of a limit of 5,
        and this text is the user's own desktop, so it reaches the model
        unbounded: the accessibility fields ``_clip`` serves are never bounded
        anywhere else.
        """
        zalgo = "A" + "\u0301" * 40
        clipped = render_clip(zalgo, 5)

        assert len(clipped) <= 6, clipped
        assert clipped.endswith("…")
        assert clipped != zalgo


class TestNotifyClip:
    def test_flag_is_dropped_whole_not_split(self) -> None:
        assert notify_clip("AB" + FLAG + "CD", 4) == "AB…"

    def test_short_text_untouched(self) -> None:
        assert notify_clip("hi", 4) == "hi"

    def test_family_survives_when_it_fits(self) -> None:
        assert notify_clip("AB" + FAMILY + "CD", 8) == "AB" + FAMILY + "…"

    def test_an_oversized_leading_cluster_does_not_defeat_the_limit(self) -> None:
        """The body stays inside the cap, so validation cannot drop the note.

        The body starts in a third-party provider's alarm payload, so it is
        untrusted. A single cluster wider than the limit has no boundary to pull
        back to, and emitting it whole lets a large enough one trip
        ``_MAX_BODY_LEN`` in the notify bus, which drops the one "an incident is
        waiting on a person" note behind a WARNING.
        """
        zalgo = "A" + "\u0301" * 4000
        clipped = notify_clip(zalgo, 2000)

        assert len(clipped) <= 2000, len(clipped)
        assert clipped.endswith("…")
