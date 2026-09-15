"""Grapheme-cluster-safe truncation at the three clip sites (#10380).

Cutting by code point inside a grapheme cluster produces a changed or broken
glyph: a dangling joiner, a lone regional indicator (boxed letter, not a
flag), a dropped tone mark that changes the word. All three sites below share
the one guard in ``kiro_crew.imessage.plaintext`` instead of each modeling a
subset of the mechanisms (the title reveal used to extend past combining marks
only, which covers one of six).
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

    def test_wider_limit_keeps_the_whole_family(self) -> None:
        assert render_clip("AB" + FAMILY + "CD", 6) == "AB…"


class TestNotifyClip:
    def test_flag_is_dropped_whole_not_split(self) -> None:
        assert notify_clip("AB" + FLAG + "CD", 4) == "AB…"

    def test_short_text_untouched(self) -> None:
        assert notify_clip("hi", 4) == "hi"

    def test_family_survives_when_it_fits(self) -> None:
        assert notify_clip("AB" + FAMILY + "CD", 8) == "AB" + FAMILY + "…"
