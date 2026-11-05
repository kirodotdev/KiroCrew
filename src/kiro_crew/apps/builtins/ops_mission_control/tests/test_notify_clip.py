"""The notification clip never splits a grapheme cluster.

Cutting by code point inside a grapheme cluster produces a changed or broken glyph: a
dangling joiner, a lone regional indicator (a boxed letter, not a flag), a dropped tone
mark that changes the word. ``notify_out._clip`` cuts through the core's one guard,
``kiro_crew.imessage.plaintext._grapheme_boundary``, which covers all six joining
mechanisms rather than a single subset. A spy on the module's own binding proves the
site still reaches it; the behavioural cases prove what it buys.
"""

from __future__ import annotations

import kiro_crew.apps.builtins.ops_mission_control.backend.notify_out as notify_out

FAMILY = "👨\u200d👩\u200d👧"
FLAG = "🇰🇷"


def _recording(original, calls: list):
    def spy(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    return spy


def test_notify_clip_reaches_the_shared_boundary(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        notify_out, "_grapheme_boundary", _recording(notify_out._grapheme_boundary, calls)
    )

    assert notify_out._clip("AB" + FAMILY + "CD", 4) == "AB…"
    assert calls, "notify_out._clip cut without the shared grapheme boundary"


class TestNotifyClip:
    def test_flag_is_dropped_whole_not_split(self) -> None:
        assert notify_out._clip("AB" + FLAG + "CD", 4) == "AB…"

    def test_short_text_untouched(self) -> None:
        assert notify_out._clip("hi", 4) == "hi"

    def test_family_survives_when_it_fits(self) -> None:
        assert notify_out._clip("AB" + FAMILY + "CD", 8) == "AB" + FAMILY + "…"

    def test_an_oversized_leading_cluster_does_not_defeat_the_limit(self) -> None:
        """The body stays inside the cap, so validation cannot drop the note.

        The body starts in a third-party provider's alarm payload, so it is
        untrusted. A single cluster wider than the limit has no boundary to pull
        back to, and emitting it whole lets a large enough one trip
        ``_MAX_BODY_LEN`` in the notify bus, which drops the one "an incident is
        waiting on a person" note behind a WARNING.
        """
        zalgo = "A" + "\u0301" * 4000
        clipped = notify_out._clip(zalgo, 2000)

        assert len(clipped) <= 2000, len(clipped)
        assert clipped.endswith("…")
