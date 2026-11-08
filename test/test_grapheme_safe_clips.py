"""Grapheme-cluster-safe truncation at the computer-use clip site.

Cutting by code point inside a grapheme cluster produces a changed or broken
glyph: a dangling joiner, a lone regional indicator (boxed letter, not a
flag), a dropped tone mark that changes the word. The site below shares the
one guard in ``kiro_crew.imessage.plaintext``, which covers all six joining
mechanisms rather than a single subset. Ops Mission Control's notification
clip shares it too; its cases run in that app's own ``tests/``.
"""

from kiro_crew.computer_use.render import _clip as render_clip

FAMILY = "👨\u200d👩\u200d👧"


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
